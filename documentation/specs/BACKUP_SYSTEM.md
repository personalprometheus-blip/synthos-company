# BACKUP SYSTEM (v2)

**Version:** 2.0
**Status:** Active — supersedes BACKUP_STRATEGY_INITIAL.md (v1, 2026-03-29)
**Last updated:** 2026-05-03
**Owner:** Patrick McGuire
**Implementation:** retail_backup.py (pi5), company_strongbox.py (pi4b),
synthos_monitor.py (`/receive_backup` + `/restore_backup`),
retail_restore.py (operator/installer), make_usb_license.py (operator/Mac).
**Schema:** [backup_manifest_schema.json](backup_manifest_schema.json) (v1.0)
**Contract:** `.claude/BACKUP_MANIFEST_CONTRACT.md`

---

## 1. Threat Model

The v2 system is designed against **adversarial threats**, not just operational
failures. Ranked by primary concern:

1. **Network interception** — Pi5 ↔ Pi4B traffic over LAN must not expose customer
   PII or operator secrets even to a rogue device on the same network.
2. **Device theft / SD seizure** — physical loss of pi4b or pi5 must not leak
   customer data or operator credentials.
3. **Insider abuse / credential compromise** — anyone reading `company.env` from
   pi4b should not, by that alone, be able to access older R2 backups
   (constrained today by single-key model — see §4 Future Work).
4. **Data corruption / accidental deletion** — drift, signals.db corruption,
   accidental customer removal. Backups capture point-in-time state with
   24h granularity.
5. **Device hardware failure** — SD card or HDD dies. Restore from R2.
6. **Ransomware on a single node** — does not block recovery because R2 is
   separate, encryption keys are off-device (USB + OneDrive).

---

## 2. RTO / RPO

| Window | Target |
|---|---|
| **RPO** (data loss) | 24 hours. New customer signups + profile modifications between 1:30am snapshots can be lost. Risk acknowledged; mitigation = admin notification on signup with explicit "in-window" warning (planned). |
| **RTO** during market hours | ASAP — minutes to hours. Requires operator availability + USB key. |
| **RTO** off-hours | Hours OK. |

The backup pipeline does **not** guarantee zero data loss. Customers who sign up
between 1:30am snapshots may have to be manually re-created if pi5 fails before
the next snapshot.

---

## 3. Architecture (v2)

### 3.1 Three-stream split

| Stream | Source node | Contents | R2 prefix |
|---|---|---|---|
| `company` | pi4b | data/company.db + auditor.db + monitor.db + support.db + data/archives/ + company.env + agents/ + config/ + (legacy) user/ | `company/<pi_id>/<date>/` |
| `customer` | pi5 | data/auth.db + data/customers/<uuid>/ | `customer/<pi_id>/<date>/` |
| `retail` | pi5 | user/.env + user/signals.db + user/agreements/ | `retail/<pi_id>/<date>/` |

The 3-stream split limits the blast radius of a credential leak. A leak of the
customer-stream R2 path doesn't expose operator config; a leak of retail-stream
keys doesn't expose customer PII.

### 3.2 Encrypt-on-source

```
pi5 (1:30am ET cron)
  └─ retail_backup.py
      ├─ Build customer + retail stream tarballs (each with manifest.json at root)
      ├─ Compute SHA-256 over content (sorted by path), embed in manifest
      ├─ Fernet-encrypt with BACKUP_ENCRYPTION_KEY
      ├─ Round-trip self-verify (decrypt + re-checksum + abort on mismatch)
      ├─ Save .enc local copy (7-day retention)
      └─ POST .enc to pi4b /receive_backup with form fields {pi_id, stream}
                                                                        │
pi4b (2am ET cron, after pi5 finishes at ~1:35am)                       │
  └─ /receive_backup endpoint                                          ◄┘
      ├─ Validate stream ∈ {customer, retail}
      ├─ Validate filename ends in .enc
      └─ Save to ~/.backup_staging/<stream>/<pi_id>/<filename>
                                          │
  └─ company_strongbox.py                ◄┘
      ├─ Build company stream locally (same approach: manifest, content sha256,
      │   Fernet-encrypt, round-trip verify)
      ├─ Walk staging dir; for each .enc:
      │     - Extract manifest (peek-only) to get expected content sha256
      │     - Upload to R2 at <stream>/<pi_id>/<date>/
      │     - Post-upload validate: download, decrypt, parse manifest, recompute
      │       content sha256, compare. Alert Scoop on mismatch.
      ├─ Enforce 30-day retention across whole bucket
      ├─ Stale-check: alert if any stream older than 48h
      └─ Persist data/backup_status.json
```

### 3.3 Why encrypt-on-source

1. **No plaintext PII on the LAN.** Even on internal Wi-Fi, customer data does
   not transit unencrypted.
2. **No plaintext at rest on pi4b.** The staging directory holds .enc files
   only; pi4b never decrypts during normal backup flow.
3. **Decoupled blast radius.** A pi4b compromise + R2 access still requires the
   Fernet key to read backups.

---

## 4. Encryption Keys

| Key | Purpose | Algorithm | Where it lives |
|---|---|---|---|
| `BACKUP_ENCRYPTION_KEY` | Fernet symmetric key for all backups | Fernet (AES-128-CBC + HMAC-SHA256) | pi4b `company.env`, pi5 `user/.env`, USB stick (`backup_key.txt`), OneDrive offline copy |
| `ENCRYPTION_KEY` | auth.db column encryption (Alpaca creds, customer PII) | Fernet | pi5 `user/.env` only — NOT a backup key |
| Ed25519 license keypair | Signs `license.json` for v2 installer offline verification | Ed25519 | private: `~/.synthos/keys/license_private.ed25519` on operator Mac + OneDrive backup; public: `synthos-company/installers/license_public.ed25519` (committed to repo) |

### 4.1 Backup encryption key — operating rules

- **Same key on pi4b and pi5.** Validated by `make_usb_license.py` and during
  install (key is one of the values written from USB).
- **Single key for v1.0** (`encryption.key_id = "primary"`). Future per-customer
  derived keys would be a v1.1 schema bump.
- **No active rotation** today. If rotation becomes necessary, the procedure is:
  1. Generate new Fernet key.
  2. Update both pi4b `company.env` and pi5 `user/.env` simultaneously.
  3. New backups encrypt with new key (`key_id` field bumped, e.g. `"primary-2"`).
  4. Old R2 backups remain decryptable with old key (do not delete the old
     key copy until 30 days after the last old-key backup falls out of retention).

### 4.2 Loss recovery

- If the USB copy is lost: regenerate USB from Mac OneDrive backup
  (`make_usb_license.py --generate-keypair` REGENERATES — destructive — and
  invalidates existing licenses; do NOT use this command for key recovery; use a
  copy from OneDrive directly).
- If both USB AND OneDrive are lost: existing R2 backups become unrecoverable.
  (This is the single point of failure the threat model accepts in v1.0.)

---

## 5. Manifest Contract (v1.0)

Every v2 tarball has a `manifest.json` at the tar root. Schema:
[backup_manifest_schema.json](backup_manifest_schema.json).

The manifest describes:
- Schema + Synthos version
- Source pi_id + node_type (legacy pi_id allowed; node_type is forward-looking)
- Stream (`company` | `customer` | `retail`)
- ISO UTC `created_at`
- `checksum_sha256`: SHA-256 over content members in arcname-sorted order
  (excludes manifest.json itself)
- `size_bytes_decrypted`: total content size for installer disk pre-check
- `encryption.algorithm` + `encryption.key_id`
- `contents[]`: ordered list of (src, dest, type, permissions, required, merge_strategy)

### 5.1 Why a manifest

Without a manifest, the v2 installer would have to hardcode "file X goes to
path Y" rules, which break silently when backup contents change. With a
manifest, the installer is data-driven: read the entries, dispatch each to its
declared destination, honor declared permissions and merge strategy, abort if
any required entry is missing.

---

## 6. R2 Bucket

| Property | Value |
|---|---|
| Bucket | `synthos-backups` |
| Endpoint | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` |
| Region | `auto` (Cloudflare) |
| Layout (v2) | `<stream>/<pi_id>/<date>/synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc` |
| Layout (legacy v1) | `<pi_id>/<date>/synthos_backup_<pi_id>_<date>.tar.gz.enc` (phase out via 30-day retention) |
| Retention | 30 days, enforced by strongbox `enforce_retention()` (no R2-side lifecycle policy yet — see §10 future work) |
| Credentials | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` in pi4b `company.env` |

---

## 7. Local Staging

| Node | Path | Retention | Cleanup |
|---|---|---|---|
| pi5 | `synthos_build/backups/staging/` | 7 days | `retail_backup.cleanup_old_local_copies()` runs after every cron cycle |
| pi4b | `~/.backup_staging/<stream>/<pi_id>/` (v2) and `~/.backup_staging/<pi_id>/` (legacy v1) | Removed after successful R2 upload | `company_strongbox.run()` deletes per-archive on success |

---

## 8. Cron Schedule

| Node | Time (ET) | What |
|---|---|---|
| pi5 | `30 1 * * *` | `retail_backup.py` builds + encrypts + POSTs both streams |
| pi4b | `0 2 * * *` | `company_strongbox.py` builds company + processes staged customer/retail + uploads + post-upload validates + retention |

The 30-minute gap (1:30 → 2:00) gives pi5 time to upload before pi4b starts.

---

## 9. Restore Procedure

Three sources, in declining preference for a fresh node:

### 9.1 Via local file (operator hand-carries .enc to a node)
```bash
python3 retail_restore.py --source file:/path/to/backup.tar.gz.enc --apply
```

### 9.2 Via pi4b proxy (fresh node has no R2 creds yet)
```bash
python3 retail_restore.py --source via-company \
    --stream customer --pi-id synthos-pi-retail --apply
```
Hits `POST /restore_backup` on pi4b which downloads from R2 and streams the .enc back.

### 9.3 Via direct R2 (any node with R2 creds in .env)
```bash
python3 retail_restore.py --source via-r2 \
    --stream customer --pi-id synthos-pi-retail --apply
```

In all cases:
- Manifest is validated before any file extraction.
- Content checksum is verified.
- Refuses to overwrite without `--apply` (dry-run by default).
- Honors per-entry merge_strategy (`replace` vs `merge`).
- Honors per-entry permissions.

For a full process-node restore (post-installer), call retail_restore once
per stream:
```bash
python3 retail_restore.py --source via-r2 --stream customer --pi-id synthos-pi-retail --apply
python3 retail_restore.py --source via-r2 --stream retail   --pi-id synthos-pi-retail --apply
```

For company-node restore, use `strongbox.py --restore company company-pi`.

---

## 10. Health & Monitoring

| Signal | Source | Channel |
|---|---|---|
| Per-stream success/failure | `data/backup_status.json` | Auditor page widget (`/audit`) + `python3 strongbox.py --status` |
| Stale (>48h) backup | `_check_staleness()` in strongbox | Scoop event `backup_stale` (audience=internal) |
| Failed upload / config error / verify fail | strongbox catch blocks | Scoop CRITICAL events |
| Round-trip self-verify mismatch | retail_backup before POST | Aborts run; logged + cron mail |
| Post-upload validation mismatch | strongbox after upload | `backup_verify_failed` Scoop event |

The auditor page (`/audit`) shows a live "Backup Health" panel listing every
stream:pi_id pair with last backup time, age, size, and outcome. Refreshes
every 5 minutes; auto-colors stale (>26h) amber and failed red.

---

## 11. Disaster Recovery Drill

Documented below. **Never run** as of 2026-05-03; planned but not yet executed.

### Drill steps (paper exercise)

1. Operator confirms USB key + OneDrive copies of `BACKUP_ENCRYPTION_KEY` are
   accessible on Mac.
2. From Mac: download a recent customer-stream backup with `aws s3 cp` (or via
   the Cloudflare R2 dashboard).
3. Decrypt on Mac: `python3 -c "from cryptography.fernet import Fernet; from
   pathlib import Path; key=open('~/.synthos/backup_key.txt').read().strip();
   Path('out.tar.gz').write_bytes(Fernet(key.encode()).decrypt(Path('in.enc').read_bytes()))"`
4. Inspect: `tar -tzf out.tar.gz | head` — confirm manifest.json + expected files.
5. Optional: extract to a scratch dir and inspect a customer signals.db with
   `sqlite3 customers/<uuid>/signals.db ".tables"`.

### Live drill (eventual)

To be planned and scheduled separately. Will involve a spare Pi flashed fresh,
running the v2 installer with `--restore=via-r2`, and verifying the V2 test
customer's data round-trips intact.

---

## 12. Future Work

- **Per-customer encryption sub-keys** — would let us narrow blast radius and
  enable per-customer "right to be forgotten" deletion. Requires manifest
  schema bump to v1.1.
- **R2-side lifecycle policy** — defense-in-depth duplicate of strongbox's
  retention. Configure in Cloudflare dashboard.
- **Hourly incremental of auth.db** during business hours — reduces RPO on
  customer-signup data to ~1h. Adds R2 cost.
- **SD-image snapshot to R2** — separate from this pipeline; periodic full
  device image (`dd | gzip | rclone`) for 5-minute restore vs minutes-to-hours
  via reflash + retail_restore.
- **Live disaster recovery drill** on a spare Pi.
- **Key rotation procedure** — codify in this doc once executed once.

---

## 13. Operational SOPs

See companion docs:

- `BACKUP_SOP_USB_KEY.md` — how to create or refresh the operator USB stick.
- `BACKUP_SOP_RESTORE.md` — how to recover from R2 if a node fails.

---

## 14. Distributed-Trader Migration Impact (added 2026-05-04, Phase D)

The Tier 1-7 distributed-trader migration changed what's running on
which node + added ~15 new files. Here's how that affects backup:

### 14.1 New source files are NOT backed up (intentional)

The backup streams (customer + retail + company) are **data-only**.
These new files (Tier 4-7 source code) are recovered from git on
restore, not from the backup tarballs:

```
src/work_packet.py
src/work_packet_db.py        ← NEW Phase A 2026-05-04
src/mqtt_client.py
src/heartbeat.py
src/dispatch_mode.py
src/gate14_evaluator.py
src/async_alpaca_client.py
agents/synthos_dispatcher.py
agents/synthos_trader_server.py
agents/synthos_migration.py
config/mosquitto/synthos.conf
```

Rationale: source code is in the synthos repo on GitHub. Backups
recover *state*; git recovers *behavior*. After a node restore the
operator runs `git pull && pip install -r requirements.txt` to bring
the new agents back to life — see BACKUP_SOP_RESTORE.md step A7.

### 14.2 Mosquitto broker config NOT in backup (regenerate from repo)

`/etc/mosquitto/conf.d/synthos.conf` lives outside the backup tree.
The canonical version is in `synthos_build/config/mosquitto/synthos.conf`
in the repo. Restore: `git pull` → `sudo cp ...synthos.conf
/etc/mosquitto/conf.d/` → restart mosquitto. The MQTT password file
at `/etc/mosquitto/passwd` is NOT backed up either; regenerate from
`MQTT_PASS` in user/.env via `mosquitto_passwd -b -c`.

### 14.3 auditor.db schema change

Phase 4 added a new `mqtt_observations` table to auditor.db on the
company node:

```sql
CREATE TABLE IF NOT EXISTS mqtt_observations (
    topic         TEXT PRIMARY KEY,
    last_seen_ts  REAL NOT NULL,
    last_payload  TEXT,
    msg_count     INTEGER NOT NULL DEFAULT 0
);
```

`company_mqtt_listener.py` creates this table on startup via
`CREATE TABLE IF NOT EXISTS`, so restoring an OLD auditor.db
(pre-2026-05-04) onto a new system: the table is auto-created on
first listener run — no manual migration.

### 14.4 Profile-aware backup scope (future)

When retail-N hardware exists (separate from process node), backup
scope per node should be:

| Profile | What to back up | What to skip |
|---|---|---|
| process (today's Pi5) | All current streams (customer + retail per 3-stream split) | — |
| retail-N (future hardware) | NOTHING — stateless workers; no persistent state | — |
| company (pi4b) | company.db, auditor.db, login.db, support.db, monitor.db | — |

When retail-N is installed via `install_retail_node.py`,
`retail_backup.py` is NOT in `REQUIRED_FILES` so it isn't deployed
there. No additional gating needed.

### 14.5 New env vars to capture for restore

user/.env on each node now contains additional secrets:

| Var | Where set | Restore source |
|---|---|---|
| `MQTT_USER` / `MQTT_PASS` | All nodes | Operator USB or operator memory |
| `DISPATCH_AUTH_TOKEN` | process + retail-N | Must match across both nodes |
| `RETAIL_URL` | process node | Set after retail-N IP is known |
| `TRADER_DB_MODE` | retail-N | `packet` on dedicated retail; `local` on loopback |
| `NODE_TYPE` / `NODE_ID` | All nodes | Set per profile |

Capture on operator USB alongside the existing backup encryption key.
See BACKUP_SOP_USB_KEY.md §1.5 (added).

---

## Appendix: File Locations

| File | Repo | Path |
|---|---|---|
| retail_backup.py | synthos | `synthos_build/src/retail_backup.py` |
| retail_restore.py | synthos | `synthos_build/src/retail_restore.py` |
| company_strongbox.py | synthos-company | `agents/company_strongbox.py` |
| /receive_backup | synthos-company | `synthos_monitor.py` |
| /restore_backup | synthos-company | `synthos_monitor.py` |
| /api/backup_health | synthos-company | `synthos_monitor.py` |
| make_usb_license.py | synthos-company | `tools/make_usb_license.py` |
| backup_manifest_schema.json | synthos-company | `documentation/specs/backup_manifest_schema.json` |
| BACKUP_SYSTEM.md (this doc) | synthos-company | `documentation/specs/BACKUP_SYSTEM.md` |
