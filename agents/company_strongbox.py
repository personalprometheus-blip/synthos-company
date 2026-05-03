"""
strongbox.py — Agent 12: Backup Manager (v2)
=============================================
Company Pi only. Run via cron daily at 2am ET.

v2 architecture:
  - 3-stream R2 layout: backup/<stream>/<pi_id>/<date>/synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc
  - Streams: company (pi4b-built locally), customer + retail (pi5-built, encrypt-on-source)
  - Already-encrypted .enc archives from pi5 are passed through unchanged (no re-encrypt)
  - manifest.json embedded in every tarball (per BACKUP_MANIFEST_CONTRACT v1.0)
  - Post-upload validation: for each successful upload, decrypt + verify manifest checksum
  - Retention: 30-day rolling on all R2 objects regardless of layout (handles legacy v1 too)

Responsibilities:
  - Build + encrypt + upload company stream (company.db + supporting DBs + company.env + agents/ + config/)
  - Pick up customer + retail .enc archives from pi5 staging, validate, upload
  - Verify post-upload (decrypt, manifest check, sample file presence)
  - Enforce 30-day retention across whole bucket
  - Health reporting to data/backup_status.json
  - Scoop alerts on CRITICAL failures

R2 key format (v2):
  <stream>/<pi_id>/<date>/synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc

Legacy v1 R2 key format (still in bucket; will phase out via 30-day retention):
  <pi_id>/<date>/synthos_backup_<pi_id>_<date>.tar.gz.enc

Usage:
  python3 strongbox.py              # full daily cycle
  python3 strongbox.py --status     # print backup_status.json summary
  python3 strongbox.py --verify     # spot-check most recent backup per stream
  python3 strongbox.py --restore <stream> <pi_id> [--date YYYY-MM-DD]
  python3 strongbox.py --dry-run    # plan only

.env keys (synthos-company/company.env):
  R2_ACCOUNT_ID          Cloudflare account ID
  R2_ACCESS_KEY_ID       R2 API token (read+write on backup bucket)
  R2_SECRET_ACCESS_KEY   R2 API secret
  R2_BUCKET_NAME         R2 bucket (default: synthos-backups)
  BACKUP_ENCRYPTION_KEY  Base64 Fernet key — same value lives on pi5
  COMPANY_DB             Override path to company.db (optional)
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import tarfile
import hashlib
import logging
import argparse
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


# ── PATH RESOLUTION ───────────────────────────────────────────────────────────
AGENT_DIR    = Path(__file__).resolve().parent
SYNTHOS_HOME = AGENT_DIR.parent
DATA_DIR     = SYNTHOS_HOME / "data"
LOG_DIR      = SYNTHOS_HOME / "logs"
USER_DIR     = SYNTHOS_HOME / "user"            # legacy; preserved if present
STAGING_DIR  = Path(os.path.expanduser("~/.backup_staging"))

BACKUP_STATUS_FILE = DATA_DIR / "backup_status.json"

load_dotenv(SYNTHOS_HOME / "company.env", override=True)


# ── CONSTANTS ─────────────────────────────────────────────────────────────────
RETENTION_DAYS     = 30
R2_BUCKET          = os.environ.get("R2_BUCKET_NAME", "synthos-backups")
R2_ACCOUNT_ID      = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY      = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY      = os.environ.get("R2_SECRET_ACCESS_KEY", "")
ENCRYPTION_KEY_B64 = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
STALE_HOURS        = 48

COMPANY_DB    = Path(os.environ.get("COMPANY_DB", str(DATA_DIR / "company.db")))
COMPANY_PI_ID = "company-pi"

MANIFEST_VERSION = "1.0"
SYNTHOS_VERSION  = "3.0"

VALID_STREAMS = ("company", "customer", "retail")


# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s strongbox: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_DIR / "strongbox.log")],
)
log = logging.getLogger("strongbox")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso_z() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _r2_client():
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY]):
        raise EnvironmentError(
            "R2 credentials missing — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY in company.env"
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def _fernet() -> Fernet:
    if not ENCRYPTION_KEY_B64:
        raise EnvironmentError(
            "BACKUP_ENCRYPTION_KEY not set in company.env — generate with: "
            "python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(ENCRYPTION_KEY_B64.encode())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _r2_object_key(stream: str, pi_id: str, date_str: str) -> str:
    return f"{stream}/{pi_id}/{date_str}/synthos_backup_{stream}_{pi_id}_{date_str}.tar.gz.enc"


def _load_status() -> dict:
    if BACKUP_STATUS_FILE.exists():
        try:
            return json.loads(BACKUP_STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_status(status: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_STATUS_FILE.write_text(json.dumps(status, indent=2))


def _alert_scoop(alert_type: str, key: str, message: str) -> None:
    """Queue a Scoop event. `key` is stream:pi_id (or pi_id for legacy)."""
    _CRIT = {"backup_failed", "backup_upload_failed", "backup_config_error",
             "backup_verify_failed", "retention_failed"}
    severity = "CRITICAL" if alert_type in _CRIT else "WARN"

    subject = f"[Synthos {severity}] strongbox {alert_type} — {key}"
    body = (f"Strongbox alert\n\nType:    {alert_type}\nKey:     {key}\n"
            f"Message: {message}\n")
    try:
        from _shared_scoop import enqueue_scoop_event
    except ImportError:
        from agents._shared_scoop import enqueue_scoop_event  # type: ignore
    enqueue_scoop_event(
        event_type=alert_type,
        subject=subject,
        body=body,
        audience="internal",
        pi_id=key,
        source_agent="strongbox",
        payload={"message": message, "severity": severity},
    )
    log.warning("Scoop alert: [%s] %s — %s", alert_type, key, message)


_NOISE_DIRS = {"__pycache__", ".ruff_cache", ".git", ".pytest_cache", ".mypy_cache"}


def _is_noise(rel_parts) -> bool:
    if any(p in _NOISE_DIRS for p in rel_parts):
        return True
    return False


def _exclude_backup_noise(tarinfo):
    """tarfile filter: skip recreatable artifacts."""
    parts = tarinfo.name.split("/")
    if any(p in _NOISE_DIRS for p in parts):
        return None
    if tarinfo.name.endswith((".pyc", ".pyo", ".swp", ".swo", "~")):
        return None
    return tarinfo


# ── COMPANY STREAM (build locally on pi4b) ────────────────────────────────────

def _company_stream_contents() -> tuple[list[dict], list[tuple[Path, str]]]:
    """
    Define + collect the company stream's contents.
    Returns (manifest_contents_entries, [(absolute_src, arcname), ...]) for files only.
    Directories are recursed file-by-file with noise excluded.
    """
    if not COMPANY_DB.exists():
        raise FileNotFoundError(f"company.db not found at {COMPANY_DB}")

    candidates = [
        # (label, abs_src, arcname, type, perms, required, merge)
        ("data/company.db",  DATA_DIR / "company.db",  "data/company.db",  "file",      "0644", True,  "replace"),
        ("data/auditor.db",  DATA_DIR / "auditor.db",  "data/auditor.db",  "file",      "0644", True,  "replace"),
        ("data/monitor.db",  DATA_DIR / "monitor.db",  "data/monitor.db",  "file",      "0644", False, "replace"),
        ("data/support.db",  DATA_DIR / "support.db",  "data/support.db",  "file",      "0644", False, "replace"),
        ("data/archives",    DATA_DIR / "archives",    "data/archives",    "directory", "0755", False, "merge"),
        ("company.env",      SYNTHOS_HOME / "company.env", "company.env",  "file",      "0600", True,  "replace"),
        ("user",             USER_DIR,                 "user",             "directory", "0755", False, "merge"),
        ("config",           SYNTHOS_HOME / "config",  "config",           "directory", "0755", False, "merge"),
        ("agents",           AGENT_DIR,                "agents",           "directory", "0755", False, "merge"),
    ]

    manifest_entries: list[dict] = []
    file_pairs: list[tuple[Path, str]] = []

    for label, abs_src, arcname, ftype, perms, required, merge in candidates:
        if not abs_src.exists():
            if required:
                raise FileNotFoundError(f"Required {label} not found at {abs_src}")
            log.info("[company] optional %s missing — skipping", label)
            continue

        if abs_src.exists():
            entry = {
                "src": arcname + ("/" if ftype == "directory" else ""),
                "dest": arcname + ("/" if ftype == "directory" else ""),
                "type": ftype,
                "permissions": perms,
                "required": required,
                "merge_strategy": merge,
            }
            manifest_entries.append(entry)

        if ftype == "file":
            file_pairs.append((abs_src, arcname))
        else:
            for p in abs_src.rglob("*"):
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(abs_src).parts
                if _is_noise(rel_parts):
                    continue
                # Skip .swp/.pyc/etc
                name = p.name
                if name.endswith((".pyc", ".pyo", ".swp", ".swo", "~")):
                    continue
                arc = arcname + "/" + "/".join(rel_parts)
                file_pairs.append((p, arc))

    # Sort by arcname for deterministic order
    file_pairs.sort(key=lambda x: x[1])
    return manifest_entries, file_pairs


def _content_checksum_from_pairs(file_pairs: list[tuple[Path, str]]) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    for abs_src, _arc in file_pairs:
        size = abs_src.stat().st_size
        total += size
        with open(abs_src, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return h.hexdigest(), total


def _build_company_archive(tmp_dir: Path) -> tuple[Path, dict]:
    """
    Build the company-stream tarball with manifest.json, encrypt with Fernet.
    Returns (encrypted_path, info_dict).
    """
    date_str = _now_utc().strftime("%Y-%m-%d")
    manifest_entries, file_pairs = _company_stream_contents()

    content_checksum, total_bytes = _content_checksum_from_pairs(file_pairs)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "synthos_version":  SYNTHOS_VERSION,
        "node_type":        "company",
        "stream":           "company",
        "pi_id":            COMPANY_PI_ID,
        "created_at":       _now_iso_z(),
        "date":             date_str,
        "checksum_sha256":  content_checksum,
        "size_bytes_decrypted": total_bytes,
        "encryption":       {"algorithm": "fernet", "key_id": "primary"},
        "contents":         manifest_entries,
    }
    manifest_path = tmp_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    plaintext_name = f"synthos_backup_company_{COMPANY_PI_ID}_{date_str}.tar.gz"
    plaintext_path = tmp_dir / plaintext_name
    with tarfile.open(plaintext_path, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        for abs_src, arcname in file_pairs:
            tar.add(abs_src, arcname=arcname)

    plaintext_size = plaintext_path.stat().st_size

    # Encrypt + round-trip
    f = _fernet()
    plaintext_bytes = plaintext_path.read_bytes()
    ciphertext = f.encrypt(plaintext_bytes)
    if f.decrypt(ciphertext) != plaintext_bytes:
        raise RuntimeError("[company] round-trip BYTE MISMATCH — aborting")

    enc_path = tmp_dir / (plaintext_name + ".enc")
    enc_path.write_bytes(ciphertext)
    enc_size = enc_path.stat().st_size
    enc_checksum = hashlib.sha256(ciphertext).hexdigest()

    log.info("[company] archive built — plaintext %.1f KB, encrypted %.1f KB, files=%d",
             plaintext_size / 1024, enc_size / 1024, len(file_pairs))

    info = {
        "stream":            "company",
        "pi_id":             COMPANY_PI_ID,
        "date":              date_str,
        "encrypted_path":    enc_path,
        "encrypted_size":    enc_size,
        "encrypted_checksum": enc_checksum,
        "decrypted_content_checksum": content_checksum,
        "decrypted_total_bytes":      total_bytes,
        "plaintext_size":    plaintext_size,
    }
    return enc_path, info


# ── R2 UPLOAD + POST-UPLOAD VALIDATION ────────────────────────────────────────

def _upload_to_r2(enc_path: Path, stream: str, pi_id: str, date_str: str,
                  dry_run: bool = False) -> str:
    """Upload encrypted archive to R2. Returns the R2 object key."""
    object_key = _r2_object_key(stream, pi_id, date_str)
    if dry_run:
        log.info("[%s:%s] [DRY RUN] would upload → s3://%s/%s",
                 stream, pi_id, R2_BUCKET, object_key)
        return object_key
    client = _r2_client()
    client.upload_file(str(enc_path), R2_BUCKET, object_key)
    log.info("[%s:%s] uploaded → s3://%s/%s", stream, pi_id, R2_BUCKET, object_key)
    return object_key


def _validate_uploaded(object_key: str, expected_cipher_checksum: str,
                       expected_content_checksum: str) -> tuple[bool, str]:
    """
    Post-upload validation: download from R2, verify ciphertext checksum,
    decrypt, parse manifest, verify content checksum from tar.

    Returns (ok, reason). Reason is empty on success.
    """
    client = _r2_client()
    f = _fernet()
    with tempfile.TemporaryDirectory() as tmp:
        dl = Path(tmp) / "x.tar.gz.enc"
        try:
            client.download_file(R2_BUCKET, object_key, str(dl))
        except (ClientError, NoCredentialsError) as e:
            return False, f"R2 download error: {e}"

        actual_cipher = _sha256_file(dl)
        if actual_cipher != expected_cipher_checksum:
            return False, (f"ciphertext sha256 mismatch — expected {expected_cipher_checksum[:16]}, "
                           f"got {actual_cipher[:16]}")

        try:
            plaintext = f.decrypt(dl.read_bytes())
        except InvalidToken:
            return False, "decryption failed (Fernet InvalidToken)"

        tar_path = Path(tmp) / "x.tar.gz"
        tar_path.write_bytes(plaintext)
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                names = tar.getnames()
                if "manifest.json" not in names:
                    return False, "manifest.json absent in tar"
                manifest_member = tar.getmember("manifest.json")
                manifest_bytes = tar.extractfile(manifest_member).read()
                manifest = json.loads(manifest_bytes)

                # Verify content checksum
                h = hashlib.sha256()
                content_members = sorted(
                    (m for m in tar.getmembers()
                     if m.isfile() and m.name != "manifest.json"),
                    key=lambda m: m.name,
                )
                for m in content_members:
                    fh = tar.extractfile(m)
                    if fh is None:
                        continue
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                actual_content = h.hexdigest()
                if actual_content != expected_content_checksum:
                    return False, (f"content sha256 mismatch — expected {expected_content_checksum[:16]}, "
                                   f"got {actual_content[:16]}")
                if manifest.get("checksum_sha256") != expected_content_checksum:
                    return False, (f"manifest.checksum_sha256 disagrees with provided expected — "
                                   f"manifest says {manifest.get('checksum_sha256','')[:16]}")
        except tarfile.TarError as e:
            return False, f"tar error: {e}"

    return True, ""


def _process_one_archive(stream: str, pi_id: str, date_str: str,
                         encrypted_path: Path,
                         expected_content_checksum: str | None,
                         dry_run: bool = False) -> dict:
    """
    Upload one .enc archive to R2 (no re-encrypt — input is already encrypted),
    then post-upload-validate. Returns a status record.

    expected_content_checksum: if known (e.g. company stream we built ourselves),
      pass through to _validate_uploaded. For pi5-encrypted streams we recompute
      from the manifest.json inside the decrypted tar (so pass None).
    """
    status = {
        "stream":      stream,
        "pi_id":       pi_id,
        "date":        date_str,
        "last_backup": None,
        "r2_key":      None,
        "checksum":    None,
        "size_bytes":  None,
        "outcome":     "failed",
        "error":       None,
    }

    key = f"{stream}:{pi_id}"
    try:
        cipher_checksum = _sha256_file(encrypted_path)
        size = encrypted_path.stat().st_size

        # Upload
        object_key = _upload_to_r2(encrypted_path, stream, pi_id, date_str, dry_run)

        if dry_run:
            status.update({
                "last_backup": _now_iso_z(),
                "r2_key":      object_key,
                "checksum":    cipher_checksum,
                "size_bytes":  size,
                "outcome":     "dry_run",
            })
            log.info("[%s] dry-run upload plan recorded", key)
            return status

        # If we don't have an expected content checksum, peek inside the .enc to extract
        # manifest and use its declared checksum as the expected value
        if expected_content_checksum is None:
            try:
                f = _fernet()
                plaintext = f.decrypt(encrypted_path.read_bytes())
                with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tf:
                    tf.write(plaintext); tf.flush()
                    with tarfile.open(tf.name, "r:gz") as tar:
                        if "manifest.json" not in tar.getnames():
                            raise ValueError("staged archive has no manifest.json — "
                                             "v1 retail backup detected (legacy path)")
                        mb = tar.extractfile(tar.getmember("manifest.json")).read()
                        man = json.loads(mb)
                        expected_content_checksum = man.get("checksum_sha256")
                        if not expected_content_checksum:
                            raise ValueError("manifest missing checksum_sha256")
            except (InvalidToken, tarfile.TarError, json.JSONDecodeError, ValueError) as e:
                # Legacy v1 staged tar without manifest? Skip post-upload validation
                # but still log so we know the upload happened.
                log.warning("[%s] could not extract manifest for validation: %s — "
                            "upload completed but post-validation skipped (legacy v1?)",
                            key, e)
                expected_content_checksum = None

        if expected_content_checksum:
            ok, reason = _validate_uploaded(object_key, cipher_checksum,
                                            expected_content_checksum)
            if not ok:
                log.error("[%s] post-upload validation FAILED: %s", key, reason)
                _alert_scoop("backup_verify_failed", key, reason)
                status.update({
                    "last_backup": _now_iso_z(),
                    "r2_key":      object_key,
                    "checksum":    cipher_checksum,
                    "size_bytes":  size,
                    "outcome":     "uploaded_validation_failed",
                    "error":       reason,
                })
                return status
            log.info("[%s] post-upload validation OK", key)

        status.update({
            "last_backup": _now_iso_z(),
            "r2_key":      object_key,
            "checksum":    cipher_checksum,
            "size_bytes":  size,
            "outcome":     "success",
        })
        log.info("[%s] backup success — %.1f KB", key, size / 1024)

    except EnvironmentError as e:
        status["error"] = str(e)
        log.error("[%s] config error: %s", key, e)
        _alert_scoop("backup_config_error", key, str(e))
    except (ClientError, NoCredentialsError) as e:
        status["error"] = str(e)
        log.error("[%s] R2 upload failed: %s", key, e)
        _alert_scoop("backup_upload_failed", key, str(e))
    except Exception as e:
        status["error"] = str(e)
        log.error("[%s] unexpected error: %s", key, e, exc_info=True)
        _alert_scoop("backup_failed", key, str(e))

    return status


# ── STAGED ARCHIVES SCAN (3-stream + legacy) ──────────────────────────────────

def _scan_staged_archives() -> dict:
    """
    Walk STAGING_DIR. Return {stream: [(pi_id, archive_path), ...]}.

    v2 layout:  ~/.backup_staging/<stream>/<pi_id>/synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc
    legacy v1:  ~/.backup_staging/<pi_id>/synthos_backup_<pi_id>_<date>.tar.gz

    For legacy v1 entries, we map them to a synthetic stream "_legacy" so they
    can be processed and uploaded under the old R2 path (preserves backward compat
    until pi5 cuts over to v2).
    """
    found = {s: [] for s in VALID_STREAMS}
    found["_legacy"] = []

    if not STAGING_DIR.exists():
        return found

    for top in sorted(STAGING_DIR.iterdir()):
        if not top.is_dir():
            continue

        # v2 stream directories: customer/, retail/  (company never staged here)
        if top.name in ("customer", "retail"):
            stream = top.name
            for pi_dir in sorted(top.iterdir()):
                if not pi_dir.is_dir():
                    continue
                archives = sorted(pi_dir.glob("*.tar.gz.enc"),
                                  key=lambda p: p.stat().st_mtime)
                if archives:
                    found[stream].append((pi_dir.name, archives[-1]))
                    for stale in archives[:-1]:
                        log.info("[%s:%s] removing stale staged archive: %s",
                                 stream, pi_dir.name, stale.name)
                        stale.unlink()
        else:
            # Legacy v1: top is a pi_id dir directly with .tar.gz inside
            archives = sorted(top.glob("*.tar.gz"),
                              key=lambda p: p.stat().st_mtime)
            if archives:
                found["_legacy"].append((top.name, archives[-1]))
                for stale in archives[:-1]:
                    log.info("[legacy:%s] removing stale staged archive: %s",
                             top.name, stale.name)
                    stale.unlink()

    return found


def _process_legacy_v1(pi_id: str, archive_path: Path, dry_run: bool = False) -> dict:
    """
    Legacy v1 path: archive is plaintext .tar.gz. Encrypt with Fernet, upload to
    legacy R2 path (<pi_id>/<date>/...). NO manifest, NO post-upload manifest validation.
    Use a stream='_legacy' status key so it doesn't collide with v2.
    """
    date_str = _now_utc().strftime("%Y-%m-%d")
    key = f"_legacy:{pi_id}"
    status = {
        "stream":      "_legacy",
        "pi_id":       pi_id,
        "date":        date_str,
        "last_backup": None,
        "r2_key":      None,
        "checksum":    None,
        "size_bytes":  None,
        "outcome":     "failed",
        "error":       None,
    }

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f = _fernet()
            plaintext = archive_path.read_bytes()
            ciphertext = f.encrypt(plaintext)
            enc_path = tmp_path / (archive_path.name + ".enc")
            enc_path.write_bytes(ciphertext)
            cipher_checksum = hashlib.sha256(ciphertext).hexdigest()

            # Legacy R2 path: <pi_id>/<date>/...
            object_key = f"{pi_id}/{date_str}/{archive_path.name}.enc"
            if dry_run:
                log.info("[%s] [DRY RUN] would upload legacy → s3://%s/%s",
                         key, R2_BUCKET, object_key)
                status.update({"last_backup": _now_iso_z(), "r2_key": object_key,
                               "checksum": cipher_checksum,
                               "size_bytes": enc_path.stat().st_size,
                               "outcome": "dry_run"})
                return status

            client = _r2_client()
            client.upload_file(str(enc_path), R2_BUCKET, object_key)
            log.info("[%s] legacy v1 upload → s3://%s/%s", key, R2_BUCKET, object_key)

            status.update({
                "last_backup": _now_iso_z(),
                "r2_key":      object_key,
                "checksum":    cipher_checksum,
                "size_bytes":  enc_path.stat().st_size,
                "outcome":     "success",
            })

    except EnvironmentError as e:
        status["error"] = str(e); log.error("[%s] config error: %s", key, e)
        _alert_scoop("backup_config_error", key, str(e))
    except (ClientError, NoCredentialsError) as e:
        status["error"] = str(e); log.error("[%s] upload failed: %s", key, e)
        _alert_scoop("backup_upload_failed", key, str(e))
    except Exception as e:
        status["error"] = str(e); log.error("[%s] unexpected: %s", key, e, exc_info=True)
        _alert_scoop("backup_failed", key, str(e))

    return status


# ── RETENTION ─────────────────────────────────────────────────────────────────

def enforce_retention(dry_run: bool = False) -> None:
    """Delete ALL R2 objects older than RETENTION_DAYS days (any layout)."""
    log.info("Enforcing %d-day retention...", RETENTION_DAYS)
    cutoff = _now_utc() - timedelta(days=RETENTION_DAYS)
    try:
        client = _r2_client()
        paginator = client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=R2_BUCKET):
            for obj in page.get("Contents", []):
                last_mod = obj["LastModified"]
                if last_mod.tzinfo is None:
                    last_mod = last_mod.replace(tzinfo=timezone.utc)
                if last_mod < cutoff:
                    if dry_run:
                        log.info("[DRY RUN] would delete: %s", obj["Key"])
                    else:
                        client.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                        log.info("Deleted stale object: %s", obj["Key"])
                    deleted += 1
        log.info("Retention: %d object(s) %s.", deleted,
                 "would be removed" if dry_run else "removed")
    except (ClientError, NoCredentialsError) as e:
        log.error("Retention enforcement failed: %s", e)
        _alert_scoop("retention_failed", "all", str(e))


# ── VERIFICATION (--verify command) ───────────────────────────────────────────

def verify_backups(status: dict) -> None:
    """Spot-check most recent backup per stream:pi_id key by full round-trip."""
    log.info("Verifying recent backups...")
    if not status:
        log.info("No backups recorded — nothing to verify.")
        return

    try:
        client = _r2_client()
        f = _fernet()
    except EnvironmentError as e:
        log.error("Verify aborted — config error: %s", e)
        return

    for key, record in status.items():
        if record.get("outcome") not in ("success",):
            log.info("[%s] skipping verify — last outcome: %s", key, record.get("outcome"))
            continue
        object_key = record.get("r2_key")
        if not object_key:
            log.warning("[%s] no R2 key recorded — skip", key)
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                dl = Path(tmp) / "x.tar.gz.enc"
                client.download_file(R2_BUCKET, object_key, str(dl))

                actual_cipher = _sha256_file(dl)
                if record.get("checksum") and actual_cipher != record["checksum"]:
                    raise ValueError(f"ciphertext sha256 mismatch (recorded vs actual)")

                plaintext = f.decrypt(dl.read_bytes())
                tar_path = Path(tmp) / "x.tar.gz"
                tar_path.write_bytes(plaintext)
                with tarfile.open(tar_path, "r:gz") as tar:
                    names = tar.getnames()
                    if "manifest.json" in names:
                        manifest = json.loads(
                            tar.extractfile(tar.getmember("manifest.json")).read()
                        )
                        log.info("[%s] VERIFY PASS — manifest v%s, %d entries, %d bytes",
                                 key, manifest.get("manifest_version"),
                                 len(manifest.get("contents", [])),
                                 manifest.get("size_bytes_decrypted", 0))
                    else:
                        # Legacy v1: just check key files
                        expected = (
                            ["company.db", "company.env"] if key.endswith(":company-pi") or key == "company-pi"
                            else ["signals.db"]
                        )
                        missing = [e for e in expected if not any(m.endswith(e) for m in names)]
                        if missing:
                            raise ValueError(f"legacy archive missing: {missing}")
                        log.info("[%s] VERIFY PASS (legacy v1) — required files present", key)

        except InvalidToken:
            msg = "decryption failed — corrupt archive or key mismatch"
            log.error("[%s] VERIFY FAIL — %s", key, msg)
            _alert_scoop("backup_verify_failed", key, msg)
        except (ClientError, NoCredentialsError) as e:
            log.error("[%s] VERIFY FAIL — R2 download error: %s", key, e)
            _alert_scoop("backup_verify_failed", key, str(e))
        except Exception as e:
            log.error("[%s] VERIFY FAIL — %s", key, e)
            _alert_scoop("backup_verify_failed", key, str(e))


# ── RESTORE ───────────────────────────────────────────────────────────────────

def restore_backup(stream: str, pi_id: str, date_str: str | None = None) -> None:
    """Download + decrypt a v2 backup from R2 into data/restore_staging/<stream>/<pi_id>/."""
    if stream not in VALID_STREAMS:
        log.error("Invalid stream: %s — must be one of %s", stream, VALID_STREAMS)
        sys.exit(1)
    if date_str is None:
        date_str = _now_utc().strftime("%Y-%m-%d")

    object_key = _r2_object_key(stream, pi_id, date_str)
    restore_dir = DATA_DIR / "restore_staging" / stream / pi_id
    restore_dir.mkdir(parents=True, exist_ok=True)
    output_path = restore_dir / f"synthos_backup_{stream}_{pi_id}_{date_str}.tar.gz"

    log.info("Restore: downloading s3://%s/%s", R2_BUCKET, object_key)
    try:
        client = _r2_client()
        enc_path = restore_dir / f"synthos_backup_{stream}_{pi_id}_{date_str}.tar.gz.enc"
        client.download_file(R2_BUCKET, object_key, str(enc_path))
        log.info("Downloaded %s (%.1f KB)", enc_path.name, enc_path.stat().st_size / 1024)

        f = _fernet()
        plaintext = f.decrypt(enc_path.read_bytes())
        output_path.write_bytes(plaintext)
        enc_path.unlink()

        log.info("Decrypted backup written to: %s", output_path)
        log.info("Contents preview:")
        with tarfile.open(output_path, "r:gz") as tar:
            for m in tar.getmembers()[:30]:
                log.info("  %s (%d B)", m.name, m.size)

    except (ClientError, NoCredentialsError) as e:
        log.error("Restore failed — R2 error: %s", e); sys.exit(1)
    except InvalidToken:
        log.error("Restore failed — decryption error. Wrong BACKUP_ENCRYPTION_KEY?"); sys.exit(1)
    except Exception as e:
        log.error("Restore failed: %s", e, exc_info=True); sys.exit(1)


# ── HEALTH REPORTING ─────────────────────────────────────────────────────────

def _check_staleness(status: dict) -> None:
    threshold = _now_utc() - timedelta(hours=STALE_HOURS)
    for key, record in status.items():
        last = record.get("last_backup")
        if last is None:
            log.warning("[%s] never backed up", key)
            _alert_scoop("backup_missing", key, "no backup on record")
            continue
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if last_dt < threshold:
            age_h = (_now_utc() - last_dt).total_seconds() / 3600
            msg = f"last backup {age_h:.1f}h ago > {STALE_HOURS}h threshold"
            log.warning("[%s] %s", key, msg)
            _alert_scoop("backup_stale", key, msg)


def print_status() -> None:
    status = _load_status()
    if not status:
        print("No backup status recorded yet.")
        return
    print(f"\n{'Key':<35} {'Last Backup (UTC)':<22} {'Outcome':<14} {'Size'}")
    print("-" * 90)
    for key, r in sorted(status.items()):
        last = r.get("last_backup", "never")
        if last and last != "never":
            last = last[:19]
        outcome = r.get("outcome", "unknown")
        size = f"{r['size_bytes'] / 1024:.1f} KB" if r.get("size_bytes") else "—"
        print(f"{key:<35} {last:<22} {outcome:<14} {size}")
    print()


# ── MAIN CYCLE ────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    log.info("=== Strongbox v2 daily run started%s ===", " [DRY RUN]" if dry_run else "")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    status = _load_status()

    # 1. Build + upload company stream
    log.info("--- company stream ---")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            enc_path, info = _build_company_archive(Path(tmp))
            record = _process_one_archive(
                "company", info["pi_id"], info["date"], enc_path,
                expected_content_checksum=info["decrypted_content_checksum"],
                dry_run=dry_run,
            )
            status[f"company:{info['pi_id']}"] = record
    except FileNotFoundError as e:
        log.error("[company] required file missing: %s", e)
        status[f"company:{COMPANY_PI_ID}"] = {
            "stream": "company", "pi_id": COMPANY_PI_ID,
            "outcome": "failed", "error": str(e),
            "last_backup": status.get(f"company:{COMPANY_PI_ID}", {}).get("last_backup"),
        }
        _alert_scoop("backup_failed", f"company:{COMPANY_PI_ID}", str(e))

    # 2. Process staged customer/retail/legacy archives
    staged = _scan_staged_archives()
    for stream in ("customer", "retail"):
        for pi_id, archive_path in staged.get(stream, []):
            log.info("--- %s:%s ---", stream, pi_id)
            log.info("[%s:%s] processing staged %s", stream, pi_id, archive_path.name)
            # pi5-built .enc — already encrypted, validate manifest from inside
            # Extract date from filename: synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc
            date_str = _now_utc().strftime("%Y-%m-%d")
            try:
                stem = archive_path.stem.replace(".tar.gz", "").replace(".tar", "")
                # synthos_backup_<stream>_<pi_id>_<YYYY-MM-DD>
                parts = stem.split("_")
                if len(parts) >= 5:
                    date_str = parts[-1]
            except Exception:
                pass
            record = _process_one_archive(
                stream, pi_id, date_str, archive_path,
                expected_content_checksum=None,  # extract from inside the .enc
                dry_run=dry_run,
            )
            status[f"{stream}:{pi_id}"] = record
            if record["outcome"] in ("success",):
                if not dry_run:
                    archive_path.unlink()
                    log.info("[%s:%s] staging archive removed", stream, pi_id)

    for pi_id, archive_path in staged.get("_legacy", []):
        log.info("--- legacy:%s ---", pi_id)
        log.info("[legacy:%s] processing v1 staged %s", pi_id, archive_path.name)
        record = _process_legacy_v1(pi_id, archive_path, dry_run=dry_run)
        status[f"_legacy:{pi_id}"] = record
        if record["outcome"] in ("success",):
            if not dry_run:
                archive_path.unlink()
                log.info("[legacy:%s] staging archive removed", pi_id)

    # 3. Retention
    enforce_retention(dry_run)

    # 4. Stale check
    _check_staleness(status)

    # 5. Persist status
    if not dry_run:
        _save_status(status)
        log.info("Backup status written to %s", BACKUP_STATUS_FILE)

    log.info("=== Strongbox v2 daily run complete ===")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Strongbox v2 — Synthos Backup Manager")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan without uploading or deleting")
    parser.add_argument("--status", action="store_true",
                        help="Print last backup status and exit")
    parser.add_argument("--verify", action="store_true",
                        help="Spot-check most recent backup per stream and exit")
    parser.add_argument("--restore", nargs=2, metavar=("STREAM", "PI_ID"),
                        help="Download + decrypt backup for STREAM PI_ID")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="Backup date for --restore (default: today)")

    args = parser.parse_args()

    if args.status:
        print_status(); sys.exit(0)
    if args.verify:
        verify_backups(_load_status()); sys.exit(0)
    if args.restore:
        restore_backup(args.restore[0], args.restore[1], args.date); sys.exit(0)

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
