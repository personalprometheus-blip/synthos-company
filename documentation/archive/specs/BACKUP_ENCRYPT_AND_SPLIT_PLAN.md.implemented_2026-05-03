# Backup Encryption + 3-Stream Split — Execution Plan

**Status:** DEFERRED — not to be executed until current phase work stabilizes
**Created:** 2026-04-18
**Owner:** Project lead decision to schedule
**Criticality:** HIGH (recovery-critical; lockout risk if done carelessly)

---

## Why this plan exists

Two independent problems with today's backup pipeline, identified 2026-04-18:

1. **Plaintext LAN transit.** `retail_backup.py` POSTs an unencrypted `.tar.gz` to `pi4b:/receive_backup`. `company_strongbox.py` then Fernet-encrypts before R2 upload. The LAN leg is a plaintext gap — the docstring explicitly says "acceptable if internal network" but the risks (WiFi compromise, rogue LAN device, ARP spoof, future shared-network use) are real.

2. **Co-mingled data.** `retail_backup.py` bundles into one tarball: `auth.db` (customer PII), `data/customers/` (customer trading history), `user/signals.db` (admin state), `user/.env` (API keys + secrets). Customer PII and operator config share one R2 object, one access path, one blast radius.

Neither is immediately blocking — retail_backup isn't even on cron yet (separate gap noted in PROJECT_STATUS.md Phase 6 hardening). But fixing both together is cleaner than fixing each later and re-touching the same code.

---

## Design

### Three backup streams

| Stream | Source node | Contents | Purpose |
|---|---|---|---|
| `company` | pi4b | `company.db` + `company.env` + `agents/` + `config/` | Operator node state + secrets |
| `customer` | pi5 | `data/auth.db` + `data/customers/` | Customer PII — isolated blast radius |
| `retail` | pi5 | `user/.env` + `user/signals.db` | Retail framework config + admin trading state |

### R2 key layout

```
synthos-backups/<stream>/<pi_id>/<date>/synthos_backup_<stream>_<pi_id>_<date>.tar.gz.enc
```

Example:
```
synthos-backups/company/company-pi/2026-04-18/synthos_backup_company_company-pi_2026-04-18.tar.gz.enc
synthos-backups/customer/synthos-pi-1/2026-04-18/synthos_backup_customer_synthos-pi-1_2026-04-18.tar.gz.enc
synthos-backups/retail/synthos-pi-1/2026-04-18/synthos_backup_retail_synthos-pi-1_2026-04-18.tar.gz.enc
```

### Encryption location

Fernet encryption with `BACKUP_ENCRYPTION_KEY` happens **on the source Pi**, not on pi4b. Pi4b stages already-encrypted `.enc` files and uploads them to R2 without touching plaintext.

- For `company` stream: pi4b encrypts its own data locally (same as today — no change)
- For `customer` and `retail` streams: pi5 encrypts, POSTs `.enc` to pi4b, pi4b uploads

The `BACKUP_ENCRYPTION_KEY` lives in both `synthos-company/company.env` and `synthos_build/user/.env`. Both Pis must have identical keys. Key is never transmitted between Pis — operator pastes it into both .envs during install.

### Staging layout on pi4b

```
~/.backup_staging/
  customer/<retail-pi-id>/synthos_backup_customer_<retail-pi-id>_<date>.tar.gz.enc
  retail/<retail-pi-id>/synthos_backup_retail_<retail-pi-id>_<date>.tar.gz.enc
```

No plaintext ever lands on pi4b disk.

### Data flow

```
pi5 cron 10pm ET → retail_backup.py:
  1. Create customer_archive.tar.gz (auth.db + data/customers/)
  2. Create retail_archive.tar.gz (user/.env + user/signals.db)
  3. Fernet-encrypt each with BACKUP_ENCRYPTION_KEY from .env
  4. Round-trip self-verify: decrypt in memory, compare SHA-256 to plaintext — abort if mismatch
  5. POST each .enc to pi4b /receive_backup with form field stream=<customer|retail>

pi4b /receive_backup:
  - Validate stream ∈ {customer, retail}
  - Save to ~/.backup_staging/<stream>/<pi_id>/*.tar.gz.enc (no decrypt, no re-encrypt)

pi4b cron 11pm ET → company_strongbox.py:
  1. Create company archive locally, encrypt, upload to company/<pi_id>/<date>/
  2. Scan ~/.backup_staging/customer/  → upload each .enc to customer/<pi_id>/<date>/
  3. Scan ~/.backup_staging/retail/    → upload each .enc to retail/<pi_id>/<date>/
  4. Retention + staleness checks, per-stream status
```

---

## Files to change

### synthos_build (pi5)

| File | Change |
|---|---|
| `src/retail_backup.py` | Split archive creation into `create_customer_archive()` + `create_retail_archive()`. Add `encrypt_archive()` using Fernet + `BACKUP_ENCRYPTION_KEY`. Add `verify_roundtrip()` guard. Change `upload_archive()` to take a `stream` arg. `run()` becomes: build both archives, encrypt both, verify both, POST both. Add `--stream <name>` CLI arg for testing a single stream. Refuse to start if `BACKUP_ENCRYPTION_KEY` is empty. |
| `installers/common/env_writer.py` | Add `BACKUP_ENCRYPTION_KEY=` line to `build_retail_env()` (currently only in company). |
| `src/install_retail.py` | Add input field to install form for `BACKUP_ENCRYPTION_KEY` (paste from pi4b company.env). Add cron entry: `0 22 * * *  python3 retail_backup.py >> logs/retail_backup.log 2>&1` |

### synthos-company (pi4b)

| File | Change |
|---|---|
| `company_server.py` | `/receive_backup`: accept + validate `stream` form field (∈ {customer, retail}); reject with 400 if missing or invalid; save to `~/.backup_staging/<stream>/<pi_id>/` instead of `~/.backup_staging/<pi_id>/`. Filename must end in `.enc`. |
| `agents/company_strongbox.py` | `_create_retail_archive_list()` → `_list_staged_archives_by_stream()` returning `{stream: [(pi_id, path), ...]}`. In `_backup_single()`, skip `_encrypt_archive()` if input already ends in `.enc`. `_r2_object_key()` prepends stream: `<stream>/<pi_id>/<date>/...`. Status dict key becomes `<stream>:<pi_id>` to avoid collision. Update `enforce_retention()` and `verify_backups()` to walk the new layout. |

---

## Safety discipline — mandatory order of operations

### Step 0 — prove current state is recoverable (before ANY code change)

This is the safety net for everything downstream.

1. Confirm `BACKUP_ENCRYPTION_KEY` from pi4b `company.env` is stored in password manager AND written on paper (physical backup).
2. Download one existing `synthos-backups/company-pi/<date>/…tar.gz.enc` from R2 to Mac.
3. Decrypt it manually on Mac using the key:
   ```python
   from cryptography.fernet import Fernet
   from pathlib import Path
   key = "<paste>"
   enc = Path("downloaded.tar.gz.enc").read_bytes()
   plaintext = Fernet(key.encode()).decrypt(enc)
   Path("restored.tar.gz").write_bytes(plaintext)
   ```
4. Untar `restored.tar.gz` and confirm readable `company.db`.

**If Step 0 fails, STOP. The existing backup is already un-recoverable and must be fixed before new work.**

### Step 1 — build and test new encrypt path on Mac, zero Pi impact

1. Implement new `retail_backup.py` encryption + split.
2. Built-in self-test: after encrypt, decrypt in-memory bytes, compare SHA-256 to plaintext. Abort run if mismatch.
3. Run on Mac against a dummy `data/` and `user/` tree. Verify: two `.enc` files produced, round-trip on each passes, files decrypt cleanly with manual command from Step 0.
4. `py_compile` all changed files.

### Step 2 — deploy pi4b changes, write to TEST R2 prefix only

1. Deploy strongbox + company_server changes.
2. Temporarily hardcode strongbox R2 path prefix to `synthos-backups-test/` (or add an env var `R2_PATH_PREFIX` with default `""` and set to `test/` during rollout).
3. Old `company-pi/<date>/` nightly continues untouched on old code path (keep as known-good safety net for this period).
4. Manually run strongbox — verify test-prefix upload succeeds.
5. Download from test prefix, decrypt on Mac, verify.

### Step 3 — verify pi5 → pi4b round trip to test prefix

1. Deploy retail_backup changes to pi5.
2. Run `retail_backup.py --local` on pi5 — produces `.enc` files in `backups/staging/` only, no network.
3. Copy to Mac. Decrypt each. Untar. Verify `auth.db` and `.env` readable respectively.
4. Run `retail_backup.py --dry-run` — confirms POST plan without sending.
5. Run real `retail_backup.py` — files land in pi4b staging under correct stream subdir.
6. Run strongbox — pushes to `test/customer/…` and `test/retail/…`.
7. Download from test prefix, decrypt, verify.

### Step 4 — flip company stream to production path, retire test prefix

1. Only after Steps 1–3 all pass: remove test prefix, strongbox writes to production `company/`, `customer/`, `retail/` paths.
2. Old `company-pi/<date>/` R2 objects remain untouched — historical record, not pruned by new retention code (only walks new layout).
3. Manually delete old objects after 30 days of new path working cleanly.

### Step 5 — enable cron on pi5 (last)

1. Run `retail_backup.py` manually 3 nights in a row, verify each via strongbox status and R2.
2. Add cron line to `install_retail.py`.
3. Redeploy installer (or manually add cron on pi5 for this install).
4. Monitor first week of cron runs via logs + `backup_status.json`.

---

## Built-in code safeguards (not process — enforced in code)

These MUST be in the new code, not checklist items:

1. **`retail_backup.py` refuses to run if `BACKUP_ENCRYPTION_KEY` is empty or not a valid base64 Fernet key.** Never falls back to plaintext. Logs a clear error and exits non-zero.
2. **`retail_backup.py` round-trip-verifies its own ciphertext before POST.** Encrypt → decrypt → SHA-256 compare → abort if mismatch. Prevents shipping corrupt/un-decryptable blobs.
3. **`company_server.py` rejects `/receive_backup` without a valid `stream` field with HTTP 400.** No silent legacy fallback — forces the schema.
4. **`company_strongbox.py` new retention only prunes within its new layout.** Old `company-pi/<date>/` objects in R2 are off-limits to this change.

---

## Rollback

- All changes are git-committed incrementally; revert reverses cleanly.
- Old `company-pi/<date>/` R2 history preserved throughout transition — worst case, `git revert` + run old strongbox and you're back to current behavior with no data loss.
- Key never rotates in this plan, so restore from any old backup works at any time.

---

## Validation gates before calling this done

- [ ] Step 0 complete — existing backup decrypts on Mac with saved key
- [ ] `py_compile` passes on all 5 changed files
- [ ] Mac-side self-test of retail_backup round-trip passes
- [ ] Strongbox writes to test prefix, verified downloadable + decryptable
- [ ] pi5 → pi4b live run to test prefix, verified decryptable per stream
- [ ] Production paths enabled, first real 3-file backup decrypts on Mac
- [ ] Cron enabled, 3 consecutive nights successful via `backup_status.json`
- [ ] Old `company-pi/<date>/` prefix retained as safety net for ≥30 days
- [ ] `/system-architecture` portal page updated to reflect new flow
- [ ] STATUS.md updated on both repos

---

## Companion task — restore UI in installer

Raised 2026-04-18 in same conversation. Tracked separately because it can ship independently of the encrypt/split work above, but shares the recovery-hardening theme.

**Scope:** Add a post-install page (after `install_retail.py` completes portal setup) offering:
- Restore from local `.enc` file (uploaded via browser)
- Restore from R2 (given R2 creds + `BACKUP_ENCRYPTION_KEY`, list available backups per stream, select one to restore)

**Dependencies:** R2 key layout from this plan determines what the restore UI queries. Build this AFTER the encrypt/split work is in place, or it'll get reworked.

---

## Why this is deferred (2026-04-18 decision)

Project lead decision: current phase has significant in-flight work (trailing-stop optimizer, approval notification system, dynamic exit params — recent commits). Introducing a 2-repo backup-pipeline change now adds coordination risk and recovery-critical cognitive load at a bad time. Plan is captured here so work can resume without re-discovery when scheduling allows.

**Resumption trigger:** When current phase stabilizes, or when a recovery scenario forces the issue (e.g. retail Pi failure exposing the missing retail_backup cron).
