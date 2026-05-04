# SOP: Create or Refresh the Operator USB Key

**Purpose:** Build the USB stick used by the v2 installer to bootstrap a
new node. Contains the signed deployment license, R2 credentials,
BACKUP_ENCRYPTION_KEY, and Cloudflare tunnel creds.

**Run on:** Operator's Mac (the only machine with the Ed25519 license private key).

**Required before first install of any v2 node.** Refresh annually (when the
license expires) or when adding new permitted node names.

---

## Prerequisites

- macOS with Python 3 + `cryptography` package (`pip install cryptography`).
- Local clone of `synthos-company` (the tool lives at
  `synthos-company/tools/make_usb_license.py`).
- A blank, formatted USB stick. Recommended: FAT32 or ExFAT; mountpoint typically
  `/Volumes/<LABEL>`.
- Optional but recommended: OneDrive folder for offline backup of the private key.

---

## What goes on the USB (added 2026-05-04, Phase D)

The USB now carries TWO classes of secret material:

1. **License key + Ed25519 keypair** — covered by §1.1–§1.5 below
2. **MQTT + dispatcher credentials** — needed for retail-N installs
   (`install_retail_node.py`) and for re-pairing a restored node

The MQTT credentials must match the values configured on the process
node's Mosquitto broker. If you change the password on the broker,
regenerate the USB.

### Layout for credentials directory

```
/Volumes/<LABEL>/
├── synthos_license.json              # signed license (existing)
├── license_public.ed25519            # public key (existing)
└── credentials/                      # NEW 2026-05-04
    ├── mqtt_credentials.json         # MQTT_HOST/PORT/USER/PASS for retail nodes
    ├── dispatch_credentials.json     # DISPATCH_AUTH_TOKEN (must match process)
    └── nodes.json                    # IPs of process + retail-N + company
```

### mqtt_credentials.json template

```json
{
  "MQTT_HOST": "10.0.0.11",
  "MQTT_PORT": 1883,
  "MQTT_USER": "synthos_broker",
  "MQTT_PASS": "<paste from process node's user/.env>"
}
```

### dispatch_credentials.json template

```json
{
  "DISPATCH_AUTH_TOKEN": "<paste from process node's user/.env>"
}
```

### nodes.json template

```json
{
  "process":   {"ip": "10.0.0.11", "user": "pi516gb"},
  "company":   {"ip": "10.0.0.10", "user": "pi"},
  "retail-1":  {"ip": "10.0.0.20", "user": "pi516gb"},
  "retail-2":  {"ip": "10.0.0.21", "user": "pi516gb"}
}
```

The retail-N installer (`install_retail_node.py`) reads these files
from the USB if mounted at `~/.synthos/credentials/` to skip the
interactive prompts. Without USB, the installer prompts for each
value at install time.

---

## One-time setup (first Mac ever)

Generate the Ed25519 keypair. **Do this exactly once, ever.** If you regenerate
the keypair, every license signed with the old private key is invalidated and
the v2 installer will reject them.

```bash
cd ~/synthos-company
python3 tools/make_usb_license.py --generate-keypair
```

This writes:
- Private key to `~/.synthos/keys/license_private.ed25519` (chmod 0600)
- Public key to `~/synthos-company/installers/license_public.ed25519`

Now back up the private key offline:

```bash
mkdir -p ~/OneDrive/synthos
cp ~/.synthos/keys/license_private.ed25519 ~/OneDrive/synthos/
chmod 600 ~/OneDrive/synthos/license_private.ed25519
```

Commit the public key to the repo so the installer build process can embed it:

```bash
cd ~/synthos-company
git add installers/license_public.ed25519
git commit -m "feat(installer): embed license verification public key"
git push
```

---

## Building a USB key for first-time setup

### Step 1 — Gather inputs on Mac

Place the operator-side material in a known location (e.g. `~/.synthos/`):

```
~/.synthos/
├── r2_credentials.json     # {"R2_ACCOUNT_ID":"...","R2_ACCESS_KEY_ID":"...","R2_SECRET_ACCESS_KEY":"..."}
├── backup_key.txt          # the BACKUP_ENCRYPTION_KEY value, single line, no quotes
└── cloudflared/
    ├── credentials.json    # from Cloudflare dashboard (tunnel creds)
    └── config.yml          # tunnel routing config
```

If you don't yet have a `backup_key.txt`, retrieve from pi4b:

```bash
ssh pi4b 'grep "^BACKUP_ENCRYPTION_KEY=" ~/synthos-company/company.env | cut -d= -f2-' \
    > ~/.synthos/backup_key.txt
chmod 600 ~/.synthos/backup_key.txt
```

### Step 2 — Insert and identify the USB stick

Insert your USB. Find its mountpoint:

```bash
ls /Volumes/
# e.g. /Volumes/SYNTHOS_KEY
```

### Step 3 — Generate the signed license + write the USB layout

```bash
cd ~/synthos-company
python3 tools/make_usb_license.py \
    --deployment-id synthos-prod-001 \
    --expires 2027-05-03 \
    --max-customers 50 \
    --permitted-nodes company,process,retail-1,retail-2,retail-3 \
    --usb-path /Volumes/SYNTHOS_KEY \
    --r2-creds ~/.synthos/r2_credentials.json \
    --backup-key ~/.synthos/backup_key.txt \
    --cloudflared-creds ~/.synthos/cloudflared/
```

Substitute the values for your deployment. `--permitted-nodes` is the comma-
separated list of node names this license authorizes. Add `retail-N` entries
ahead of time for any node you might install during the license's validity.

The tool will print the full signed license, then assemble the USB layout under
`/Volumes/SYNTHOS_KEY/synthos-key/`.

### Step 4 — Verify

Inspect the signed license:

```bash
python3 tools/make_usb_license.py --inspect /Volumes/SYNTHOS_KEY/synthos-key/license.json
```

You should see:
```
✓ signature OK; license valid
```

Confirm the layout:

```bash
ls -la /Volumes/SYNTHOS_KEY/synthos-key/
```

Should show `license.json`, `license_public.ed25519`, `r2_credentials.json`,
`backup_key.txt`, `cloudflared/`, `README.txt`.

### Step 5 — Eject and store

Eject the USB and store somewhere physically secure. Treat it like cash — anyone
with this stick can install a new node and read all R2 backups.

---

## Refresh / renewal

When the license is approaching expiry, re-run Step 3 with a new `--expires`
date and overwrite the USB. The installer will reject expired licenses.

To rotate the BACKUP_ENCRYPTION_KEY (rare; see BACKUP_SYSTEM.md §4.1), update
`~/.synthos/backup_key.txt` first, then re-run Step 3.

---

## Recovery if USB is lost

1. Confirm OneDrive copy of `~/.synthos/keys/license_private.ed25519` exists.
2. Get a new USB.
3. Re-run Step 3 with the same parameters.

If both USB AND OneDrive are lost, you have lost the ability to issue new
licenses. Existing installed nodes continue to work, but you cannot add new
ones. Run `--generate-keypair --force` to start over (every license previously
issued becomes invalid; existing installs continue running until their license
expires, but cannot re-verify).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `private key not found` | First run on new Mac | Run `--generate-keypair` |
| `signature does not match public key` on `--inspect` | The public key in repo doesn't match the private key used to sign | Either re-sign with the right private key, or commit a different `installers/license_public.ed25519` |
| Installer rejects license at install time | License expired, or node name not in `permitted_nodes` | Re-run Step 3 with updated `--expires` and/or `--permitted-nodes` |
| `--backup-key file is empty` | The file was created by `>` redirect that swallowed the key | Re-extract with the SSH command above |
