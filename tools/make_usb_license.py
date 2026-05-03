"""
make_usb_license.py — Operator-side license + USB key builder
================================================================
Run on the operator's Mac. Generates Ed25519 keypair (one-time), signs a
license.json, and assembles the USB key contents for v2 installer:

USB layout produced (relative to USB mountpoint):
    synthos-key/
    ├── license.json        (signed, with embedded .signature field)
    ├── license_public.ed25519  (public key — for signature verification by installer)
    ├── r2_credentials.json (R2 keys, for company-node only)
    ├── backup_key.txt      (BACKUP_ENCRYPTION_KEY, single line)
    ├── cloudflared/
    │   ├── credentials.json
    │   └── config.yml
    └── README.txt          (operator-facing reminder)

Key locations on Mac:
    Private signing key  ~/.synthos/keys/license_private.ed25519  (chmod 0600)
    Public verification  synthos-company/installers/license_public.ed25519  (committed; installer reads at build time)

Workflow:
    First time on a new Mac:
        python3 make_usb_license.py --generate-keypair
            (writes private to ~/.synthos/keys/, public to repo installers/)

    Building a USB key for first-time setup:
        python3 make_usb_license.py \\
            --deployment-id synthos-prod-001 \\
            --expires 2027-05-03 \\
            --max-customers 50 \\
            --permitted-nodes company,process,retail-1,retail-2,retail-3 \\
            --usb-path /Volumes/SYNTHOS_KEY \\
            --r2-creds ~/.synthos/r2_credentials.json \\
            --backup-key ~/.synthos/backup_key.txt \\
            --cloudflared-creds ~/.synthos/cloudflared/

    Inspect a previously signed license:
        python3 make_usb_license.py --inspect /path/to/license.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
        load_pem_private_key, load_pem_public_key,
    )
except ImportError:
    print("ERROR: missing 'cryptography' package. pip install cryptography", file=sys.stderr)
    sys.exit(1)


# ── PATHS ──────────────────────────────────────────────────────────────────────
HOME = Path.home()
KEY_DIR = HOME / ".synthos" / "keys"
PRIV_KEY_PATH = KEY_DIR / "license_private.ed25519"

THIS_FILE = Path(__file__).resolve()
SYNTHOS_COMPANY = THIS_FILE.parent.parent  # tools/ is at repo root
PUB_KEY_PATH = SYNTHOS_COMPANY / "installers" / "license_public.ed25519"

LICENSE_VERSION = "1.0"


# ── KEYPAIR ────────────────────────────────────────────────────────────────────

def cmd_generate_keypair(force: bool = False) -> int:
    """Generate Ed25519 keypair if absent."""
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    KEY_DIR.chmod(0o700)

    if PRIV_KEY_PATH.exists() and not force:
        print(f"Private key already exists at {PRIV_KEY_PATH}")
        print("Use --force to overwrite (DESTRUCTIVE — invalidates all existing licenses)")
        return 1

    print(f"Generating Ed25519 keypair...")
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_pem = priv.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    PRIV_KEY_PATH.write_bytes(priv_pem)
    PRIV_KEY_PATH.chmod(0o600)
    print(f"  private: {PRIV_KEY_PATH} (chmod 0600)")

    PUB_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUB_KEY_PATH.write_bytes(pub_pem)
    print(f"  public:  {PUB_KEY_PATH} (commit to repo)")

    print()
    print("NEXT STEPS:")
    print(f"  1. Back up the private key to OneDrive: cp {PRIV_KEY_PATH} ~/OneDrive/synthos/")
    print(f"  2. Commit the public key: cd {SYNTHOS_COMPANY} && git add {PUB_KEY_PATH.relative_to(SYNTHOS_COMPANY)}")
    print( "  3. The installer will embed this public key at build time and verify license.json offline.")
    return 0


def _load_private_key() -> Ed25519PrivateKey:
    if not PRIV_KEY_PATH.exists():
        print(f"ERROR: private key not found at {PRIV_KEY_PATH}", file=sys.stderr)
        print(f"Run: {sys.argv[0]} --generate-keypair", file=sys.stderr)
        sys.exit(2)
    pem = PRIV_KEY_PATH.read_bytes()
    return load_pem_private_key(pem, password=None)


def _load_public_key() -> Ed25519PublicKey:
    if not PUB_KEY_PATH.exists():
        print(f"ERROR: public key not found at {PUB_KEY_PATH}", file=sys.stderr)
        sys.exit(2)
    return load_pem_public_key(PUB_KEY_PATH.read_bytes())


# ── SIGN / VERIFY ──────────────────────────────────────────────────────────────

def _canonical_json(payload: dict) -> bytes:
    """Deterministic byte form of the license body for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_license(payload: dict) -> dict:
    """Sign a license payload. Returns the signed envelope with .signature embedded."""
    priv = _load_private_key()
    canonical = _canonical_json(payload)
    sig = priv.sign(canonical).hex()

    signed = dict(payload)
    signed["signature"] = {
        "algorithm":   "ed25519",
        "value_hex":   sig,
        "signed_over": "deterministic JSON of license fields excluding signature; "
                       "json.dumps(payload, sort_keys=True, separators=(',', ':'))",
    }
    return signed


def verify_license(signed: dict) -> tuple[bool, str]:
    """Verify a signed license against the public key. Returns (ok, reason)."""
    sig_obj = signed.get("signature")
    if not sig_obj or "value_hex" not in sig_obj:
        return False, "no signature present"
    payload = {k: v for k, v in signed.items() if k != "signature"}
    canonical = _canonical_json(payload)
    try:
        pub = _load_public_key()
        from cryptography.exceptions import InvalidSignature
        try:
            pub.verify(bytes.fromhex(sig_obj["value_hex"]), canonical)
        except InvalidSignature:
            return False, "signature does not match public key"
    except Exception as e:
        return False, f"verify error: {e}"

    expires = signed.get("expires_at", "")
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                return False, f"license expired at {expires}"
        except ValueError:
            return False, f"unparseable expires_at: {expires!r}"

    return True, "OK"


# ── BUILDERS ───────────────────────────────────────────────────────────────────

def build_license(deployment_id: str, expires_at: str, max_customers: int,
                  permitted_nodes: list, plan_tier: str = "operator") -> dict:
    return {
        "license_version": LICENSE_VERSION,
        "deployment_id":   deployment_id,
        "issued_at":       datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at":      expires_at,
        "max_customers":   max_customers,
        "plan_tier":       plan_tier,
        "permitted_nodes": permitted_nodes,
    }


def write_usb_layout(usb_path: Path, signed_license: dict,
                     r2_creds: Path | None,
                     backup_key: Path | None,
                     cloudflared_dir: Path | None) -> None:
    """Assemble the USB stick layout under <usb_path>/synthos-key/."""
    if not usb_path.exists() or not usb_path.is_dir():
        raise SystemExit(f"USB path not found or not a directory: {usb_path}")

    target = usb_path / "synthos-key"
    target.mkdir(exist_ok=True)
    target.chmod(0o700)

    # license.json (signed, with embedded signature object)
    (target / "license.json").write_text(json.dumps(signed_license, indent=2))
    (target / "license.json").chmod(0o600)

    # public key (so an operator can manually verify the license file too)
    if PUB_KEY_PATH.exists():
        shutil.copy2(PUB_KEY_PATH, target / "license_public.ed25519")

    if r2_creds:
        if not r2_creds.exists():
            raise SystemExit(f"--r2-creds path not found: {r2_creds}")
        shutil.copy2(r2_creds, target / "r2_credentials.json")
        (target / "r2_credentials.json").chmod(0o600)

    if backup_key:
        if not backup_key.exists():
            raise SystemExit(f"--backup-key path not found: {backup_key}")
        # Strip whitespace
        key_text = backup_key.read_text().strip()
        if not key_text:
            raise SystemExit(f"--backup-key file is empty: {backup_key}")
        (target / "backup_key.txt").write_text(key_text + "\n")
        (target / "backup_key.txt").chmod(0o600)

    if cloudflared_dir:
        cf_dir = cloudflared_dir
        if not cf_dir.exists() or not cf_dir.is_dir():
            raise SystemExit(f"--cloudflared-creds dir not found: {cf_dir}")
        target_cf = target / "cloudflared"
        target_cf.mkdir(exist_ok=True)
        copied = 0
        for src in cf_dir.iterdir():
            if src.is_file():
                shutil.copy2(src, target_cf / src.name)
                (target_cf / src.name).chmod(0o600)
                copied += 1
        print(f"  cloudflared/: copied {copied} file(s)")

    readme = (
        "Synthos USB Key — Operator Reminder\n"
        "====================================\n\n"
        f"Created: {datetime.now(timezone.utc).isoformat()[:19]}Z\n"
        f"Deployment: {signed_license['deployment_id']}\n"
        f"Expires: {signed_license['expires_at']}\n\n"
        "Contents:\n"
        "  license.json              Signed deployment license (Ed25519)\n"
        "  license_public.ed25519    Public key used to sign this license\n"
        "  r2_credentials.json       Cloudflare R2 keys (company node only)\n"
        "  backup_key.txt            BACKUP_ENCRYPTION_KEY (Fernet, base64)\n"
        "  cloudflared/              Cloudflare tunnel creds (company node only)\n\n"
        "Keep this USB physically secure. If lost, regenerate via:\n"
        "  ssh you@mac\n"
        "  python3 ~/synthos-company/tools/make_usb_license.py ...\n\n"
        "To use during install:\n"
        "  Insert into a fresh Pi BEFORE running install.sh.\n"
        "  Installer auto-detects /media/<...>/synthos-key/.\n"
    )
    (target / "README.txt").write_text(readme)
    print(f"\nUSB layout written to: {target}")


# ── COMMANDS ───────────────────────────────────────────────────────────────────

def cmd_inspect(license_path: Path) -> int:
    if not license_path.exists():
        print(f"ERROR: not found: {license_path}", file=sys.stderr); return 1
    try:
        signed = json.loads(license_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr); return 1

    print("=== License contents ===")
    payload = {k: v for k, v in signed.items() if k != "signature"}
    print(json.dumps(payload, indent=2))
    print()
    print("=== Signature ===")
    sig = signed.get("signature")
    if not sig:
        print("(no signature)"); return 1
    print(json.dumps(sig, indent=2))
    print()
    print("=== Verification ===")
    ok, reason = verify_license(signed)
    if ok:
        print("✓ signature OK; license valid")
        return 0
    else:
        print(f"✗ {reason}")
        return 2


def cmd_make_license(args: argparse.Namespace) -> int:
    if not all([args.deployment_id, args.expires, args.permitted_nodes]):
        print("ERROR: --deployment-id, --expires, --permitted-nodes required",
              file=sys.stderr)
        return 2

    # Normalize expires_at
    expires = args.expires
    if "T" not in expires:
        expires = expires + "T00:00:00Z"
    if not expires.endswith("Z"):
        expires = expires + "Z"

    nodes = [n.strip() for n in args.permitted_nodes.split(",") if n.strip()]

    payload = build_license(
        deployment_id=args.deployment_id,
        expires_at=expires,
        max_customers=args.max_customers,
        permitted_nodes=nodes,
        plan_tier=args.plan_tier,
    )
    signed = sign_license(payload)

    print("Signed license:")
    print(json.dumps(signed, indent=2))

    if args.usb_path:
        usb = Path(args.usb_path).resolve()
        write_usb_layout(
            usb,
            signed,
            r2_creds=Path(args.r2_creds).expanduser().resolve() if args.r2_creds else None,
            backup_key=Path(args.backup_key).expanduser().resolve() if args.backup_key else None,
            cloudflared_dir=Path(args.cloudflared_creds).expanduser().resolve() if args.cloudflared_creds else None,
        )
    elif args.output:
        out = Path(args.output).resolve()
        out.write_text(json.dumps(signed, indent=2))
        out.chmod(0o600)
        print(f"\nSigned license written to {out}")
    else:
        print("\n(no --usb-path or --output specified; license printed above only)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="make_usb_license.py — operator USB key builder")
    ap.add_argument("--generate-keypair", action="store_true",
                    help="Generate Ed25519 keypair (one-time)")
    ap.add_argument("--force", action="store_true",
                    help="With --generate-keypair: overwrite existing keypair (DESTRUCTIVE)")
    ap.add_argument("--inspect", metavar="PATH",
                    help="Inspect + verify a signed license.json file")

    ap.add_argument("--deployment-id", help="e.g. synthos-prod-001")
    ap.add_argument("--expires", help="YYYY-MM-DD or full ISO 8601")
    ap.add_argument("--max-customers", type=int, default=50)
    ap.add_argument("--permitted-nodes",
                    help="comma-separated list (e.g. company,process,retail-1,retail-2)")
    ap.add_argument("--plan-tier", default="operator")

    ap.add_argument("--usb-path", help="USB mountpoint (e.g. /Volumes/SYNTHOS_KEY)")
    ap.add_argument("--output", help="Write signed license.json to this path (instead of USB)")
    ap.add_argument("--r2-creds",      help="Path to r2_credentials.json")
    ap.add_argument("--backup-key",    help="Path to file with BACKUP_ENCRYPTION_KEY")
    ap.add_argument("--cloudflared-creds", help="Path to dir with cloudflared credentials.json+config.yml")

    args = ap.parse_args()

    if args.generate_keypair:
        sys.exit(cmd_generate_keypair(force=args.force))
    if args.inspect:
        sys.exit(cmd_inspect(Path(args.inspect).expanduser().resolve()))
    if any([args.deployment_id, args.expires, args.permitted_nodes,
            args.usb_path, args.output]):
        sys.exit(cmd_make_license(args))

    ap.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
