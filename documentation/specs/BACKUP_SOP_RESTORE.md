# SOP: Recover from R2 (Disaster Recovery)

**Purpose:** Restore a node's data from R2 backups when its local storage is
lost, corrupted, or being migrated.

**Scope:** retail/process node (synthos-pi-retail) and company node (company-pi).

**Time estimate:** 30 minutes for retail node. 60+ minutes for company node
including SSH/tunnel reconfiguration.

**Prerequisites:**

- R2 contains a recent encrypted backup for the target stream + pi_id.
- `BACKUP_ENCRYPTION_KEY` is available (USB stick, OneDrive, or already on a
  surviving node).
- Mac and SSH access to remaining live nodes.

---

## Quick reference

| Failure | Path | Run on |
|---|---|---|
| pi5 dies; pi4b alive | Section A — restore process node via pi4b proxy | new pi5 (after install) |
| pi4b dies; pi5 alive | Section B — restore company node from R2 directly | new pi4b (after install) |
| Both dead | Section C — full rebuild from USB key only | new pi4b first, then new pi5 |
| Single customer's data corrupted | Section D — surgical restore of one customer | live pi5 |

---

## A. Restore process node (pi5) when pi4b is alive

This is the common case: a single Pi failed.

### A1. Flash and boot a fresh Pi5

Pi OS Lite, network configured, SSH key authorized for the operator account.
Network must reach pi4b.

### A2. Run the v2 installer

(Once v2 installer ships. Until then, follow the legacy install procedure first.)

```bash
ssh pi516gb@<new-pi5-ip>
git clone <synthos repo>
cd synthos/synthos_build
./install.sh --node=process \
    --license-file=/media/<USB>/synthos-key/license.json \
    --restore=via-company
```

The installer's RESTORING phase will call `retail_restore.py` internally. If
running outside the installer, do A3-A4 manually.

### A3. Manually copy minimum .env on the new pi5

The new pi5 needs `COMPANY_URL`, `SECRET_TOKEN`, `PI_ID`, and
`BACKUP_ENCRYPTION_KEY` to call back to pi4b. Copy these from USB:

```bash
ssh pi516gb@<new-pi5-ip>
mkdir -p ~/synthos/synthos_build/user
cat > ~/synthos/synthos_build/user/.env << 'EOF'
COMPANY_URL=http://10.0.0.10:5050
SECRET_TOKEN=<from USB or pi4b>
PI_ID=synthos-pi-retail
BACKUP_ENCRYPTION_KEY=<from USB synthos-key/backup_key.txt>
EOF
chmod 600 ~/synthos/synthos_build/user/.env
```

### A4. Run retail_restore for both streams

```bash
ssh pi516gb@<new-pi5-ip>
cd ~/synthos/synthos_build

# Customer stream first (auth.db + customers/)
python3 src/retail_restore.py \
    --source via-company \
    --stream customer \
    --pi-id synthos-pi-retail \
    --apply

# Then retail stream (.env + signals.db + agreements/)
python3 src/retail_restore.py \
    --source via-company \
    --stream retail \
    --pi-id synthos-pi-retail \
    --apply
```

Each call prints the manifest details, verifies content checksum, and applies
the restore (replacing local files for `replace`-strategy entries, merging
for `merge`-strategy).

### A5. Verify

```bash
sqlite3 ~/synthos/synthos_build/data/auth.db ".tables" | head
ls ~/synthos/synthos_build/data/customers/ | wc -l   # expected ≈ 14
sqlite3 ~/synthos/synthos_build/user/signals.db ".tables" | head
```

### A6. Bring services back

Restart any running services (systemd timers, market_daemon, trade_daemon).
Confirm pi5 is heartbeating to pi4b.

### A7. Restore distributed-trader components (added 2026-05-04, Phase D)

Backups are data-only. Source code added by the Tier 1-7 distributed-
trader migration (work_packet, work_packet_db, mqtt_client, heartbeat,
dispatch_mode, gate14_evaluator, async_alpaca_client, synthos_dispatcher,
synthos_trader_server, synthos_migration) is recovered from git, not
from the tarball.

```bash
# Pull latest source — gets every file added since the backup was taken
cd ~/synthos
git pull origin main

# Make sure new pip deps are installed (paho-mqtt, httpx, fastapi, uvicorn
# are all in install_retail.py's APT_DEPS — apt install -f if missing)
sudo apt-get install -y python3-paho-mqtt python3-httpx python3-fastapi python3-uvicorn

# Restore mosquitto config (NOT in backup tarballs)
sudo cp ~/synthos/synthos_build/config/mosquitto/synthos.conf /etc/mosquitto/conf.d/synthos.conf

# Regenerate the mosquitto password file from MQTT_PASS in user/.env
MQTT_PASS=$(grep '^MQTT_PASS=' ~/synthos/synthos_build/user/.env | cut -d= -f2)
sudo mosquitto_passwd -b -c /etc/mosquitto/passwd synthos_broker "$MQTT_PASS"
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd
sudo chmod 600 /etc/mosquitto/passwd

# Drop the new systemd units (per CUTOVER_RUNBOOK.md)
sudo systemctl daemon-reload
sudo systemctl enable --now mosquitto.service synthos-trader-server.service \
                            synthos-dispatcher.service

# Verify
systemctl is-active mosquitto.service synthos-trader-server.service synthos-dispatcher.service
curl -sf http://127.0.0.1:8443/readyz
```

Without these steps, the restored node trades correctly via the daemon
path but cannot serve any customer migrated to distributed mode.

If a customer was on `_DISPATCH_MODE=distributed` at the time of backup,
their setting is preserved in their signals.db. After restore + the
above steps, the dispatcher will pick them up on the next cycle without
operator intervention.

---

## B. Restore company node (pi4b) when pi5 is alive

### B1. Flash a fresh Pi 4B with Pi OS

Network must reach the internet (R2). Cloudflare tunnel will need
reconfiguration after restore.

### B2. Set the BACKUP_ENCRYPTION_KEY and R2 creds early

The new pi4b needs R2 creds to reach R2 in the first place. Before installing
synthos-company, prepare:

```bash
# On new pi4b, create the dir and put company.env in place from USB
mkdir -p ~/synthos-company
cp /media/<USB>/synthos-key/r2_credentials.json /tmp/r2.json
KEY=$(cat /media/<USB>/synthos-key/backup_key.txt)
ACCOUNT=$(jq -r .R2_ACCOUNT_ID /tmp/r2.json)
ACCESS=$(jq -r .R2_ACCESS_KEY_ID /tmp/r2.json)
SECRET=$(jq -r .R2_SECRET_ACCESS_KEY /tmp/r2.json)
cat > ~/synthos-company/company.env << EOF
BACKUP_ENCRYPTION_KEY=$KEY
R2_ACCOUNT_ID=$ACCOUNT
R2_ACCESS_KEY_ID=$ACCESS
R2_SECRET_ACCESS_KEY=$SECRET
R2_BUCKET_NAME=synthos-backups
EOF
chmod 600 ~/synthos-company/company.env
```

### B3. Clone synthos-company

```bash
git clone <synthos-company repo> ~/synthos-company
cd ~/synthos-company
pip install -r requirements.txt   # or your usual install
```

### B4. Run strongbox restore

```bash
cd ~/synthos-company
python3 agents/company_strongbox.py --restore company company-pi
```

This downloads the latest company-stream backup from R2, decrypts it, and
writes the decrypted .tar.gz to
`data/restore_staging/company/company-pi/...tar.gz`.

### B5. Extract

```bash
cd ~
tar -xzf ~/synthos-company/data/restore_staging/company/company-pi/synthos_backup_company_company-pi_<DATE>.tar.gz \
    -C ~/synthos-company/
```

This places company.db at `data/company.db`, restored config at `config/`,
agents at `agents/`, and `company.env` at the root (overwriting the partial
one we created in B2 — this is intentional, the backup-stored env is
canonical).

### B6. Bring services back

Restart synthos-company services. The pi5 backup pipeline picks up
automatically on the next 1:30am cron run.

---

## C. Full rebuild from scratch

Both pi4b and pi5 are dead. You have only the USB key.

1. Do Section B (restore pi4b first — it's the destination of pi5's daily
   uploads).
2. Wait for pi4b to be fully back online.
3. Do Section A (restore pi5 via pi4b proxy).

Order matters because pi5 needs pi4b's `/restore_backup` endpoint to fetch
its data without R2 creds.

If you have R2 creds available on pi5 too (via USB), you can skip step 2 and
restore both nodes in parallel using `--source via-r2` for pi5.

---

## D. Surgical restore: single customer

Customer's signals.db got corrupted. You only want to roll back that one
customer.

```bash
ssh pi516gb@10.0.0.11
cd ~/synthos/synthos_build

# Download yesterday's customer-stream backup to a scratch dir
python3 src/retail_restore.py \
    --source via-r2 \
    --stream customer \
    --pi-id synthos-pi-retail \
    --date 2026-05-02 \
    --target /tmp/restore_scratch
# (without --apply — produces decrypted tar but doesn't overwrite anything)
```

Wait — `retail_restore.py --target /tmp/restore_scratch` (no `--apply`) is a
dry-run. Use `--apply` to extract to the target directory:

```bash
python3 src/retail_restore.py \
    --source via-r2 \
    --stream customer \
    --pi-id synthos-pi-retail \
    --date 2026-05-02 \
    --target /tmp/restore_scratch \
    --apply
```

Then manually copy the one file you want:

```bash
cp /tmp/restore_scratch/data/customers/<UUID>/signals.db \
   ~/synthos/synthos_build/data/customers/<UUID>/signals.db
chmod 644 ~/synthos/synthos_build/data/customers/<UUID>/signals.db
```

---

## Verification after any restore

| Check | Command | Expected |
|---|---|---|
| auth.db tables | `sqlite3 ~/synthos/synthos_build/data/auth.db ".tables"` | customers, sessions, etc. |
| Customer count | `ls ~/synthos/synthos_build/data/customers/ \| wc -l` | matches pre-failure count |
| .env permissions | `stat -c %a ~/synthos/synthos_build/user/.env` | 600 |
| Manifest sanity | `python3 src/retail_restore.py --source file:/path/to/.enc --stream <S> --pi-id <P>` | "Manifest OK", "Content checksum verified" |
| Service health | curl portal health endpoint | 200 OK |
| Heartbeat to pi4b | check pi4b /monitor page | pi5 active within last few minutes |

---

## Common pitfalls

- **Wrong stream/pi_id combination → 404 from `/restore_backup`.** Check
  `python3 strongbox.py --status` on pi4b for the exact keys.
- **`InvalidToken` during decrypt.** Wrong `BACKUP_ENCRYPTION_KEY`. Verify
  it matches between USB and the .env where you wrote it.
- **`--apply` writes to the wrong directory.** `--target` defaults to the
  synthos_build/ directory the tool lives in. Always pass `--target
  ~/synthos/synthos_build` explicitly when restoring on a fresh node where
  the script may not be at the canonical path.
- **`merge_strategy: replace` deletes existing data.** This is intentional —
  the backup is canonical for that path. If you need a non-destructive
  merge, restore to a scratch dir first and copy by hand.

---

## Last-resort fallback: download + decrypt manually on Mac

If the Pis are unrecoverable but R2 still has data and you have
`BACKUP_ENCRYPTION_KEY` from USB/OneDrive:

```bash
# Configure aws CLI for R2
aws configure set aws_access_key_id <R2_ACCESS_KEY> --profile r2
aws configure set aws_secret_access_key <R2_SECRET> --profile r2
aws configure set region auto --profile r2

# List
aws s3 ls --profile r2 --endpoint-url https://<R2_ACCOUNT>.r2.cloudflarestorage.com \
    s3://synthos-backups/customer/synthos-pi-retail/

# Download latest
aws s3 cp --profile r2 --endpoint-url https://<R2_ACCOUNT>.r2.cloudflarestorage.com \
    s3://synthos-backups/customer/synthos-pi-retail/2026-05-03/synthos_backup_customer_synthos-pi-retail_2026-05-03.tar.gz.enc \
    ./local.enc

# Decrypt + extract
python3 << EOF
from cryptography.fernet import Fernet
from pathlib import Path
key = open('/Users/patrickmcguire/.synthos/backup_key.txt').read().strip()
Path('out.tar.gz').write_bytes(Fernet(key.encode()).decrypt(Path('local.enc').read_bytes()))
EOF
tar -xzf out.tar.gz -C /tmp/restore_out/
ls /tmp/restore_out/
```

You now have the customer data on Mac and can salvage what you need.
