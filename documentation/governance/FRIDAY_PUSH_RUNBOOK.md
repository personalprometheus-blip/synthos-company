# FRIDAY PUSH RUNBOOK

**Version:** 1.0
**Date:** 2026-03-30
**Owner:** Project lead
**Authority:** SYNTHOS_OPERATIONS_SPEC.md §3–4 (this document is the executable version of that spec)
**Phase note:** Phase 1 hardware reality — single Pi 2W serving as dev + production combined. Pipeline steps that reference "beta Pis" or "customer Pis" are not yet active. This runbook reflects what is actually executable today.

---

## Overview

The Friday push moves approved changes from `update-staging` into `main` and deploys them to the retail Pi. Blueprint builds during the week; the project lead is the only one who approves and ships on Friday.

**Friday is a push day, not a build day.** Blueprint does no new work on Fridays. If something is not ready by Thursday EOD, it waits for next week.

```
update-staging  ──► (project lead approves) ──► main ──► git push ──► qpull.sh on Pi
```

---

## Pre-Push Checklist (Friday morning, before market close)

Do not push after market close until all of these are confirmed:

- [ ] Morning report received from Scoop — no CRITICAL items outstanding
- [ ] Patches sign-off: `Ready for Friday push: YES` in morning report
- [ ] No open crash alerts from Sentinel
- [ ] Blueprint has staged all approved suggestions to `update-staging`
- [ ] No `.staged` or `.tmp` artifacts left in `.blueprint_staging/` (indicates a failed mid-run)

If any item is not green, delay the push. An unresolved CRITICAL item is a hard stop — do not push over it.

---

## Push Sequence (after market close — 4pm ET)

### Step 1 — Review pending changes in the command portal

Open the command portal and review the Pending Changes queue. For each item:

- Read the suggestion, Blueprint's implementation notes, and Patches' review
- **Approve** → change proceeds to merge
- **Reject** → item returns to update-staging with your rejection notes; Blueprint addresses next week

If the queue is empty and the morning report said there was nothing to push, the week had no shippable changes. That is fine — skip to post-push verification anyway to confirm system health.

### Step 2 — Merge update-staging → main (on company Pi)

Blueprint performs the merge as part of finalizing approved suggestions. Confirm it completed:

```bash
# On company Pi
cd /home/pi/synthos-company
git log --oneline -5   # should show Blueprint's merge commit at top
```

If Blueprint did not merge (e.g., no changes were approved or Blueprint did not run), merge manually:

```bash
# On company Pi — only if Blueprint did not handle it
cd /path/to/synthos-retail-repo   # wherever the retail repo lives on company Pi
git checkout main
git merge update-staging --no-ff -m "deploy: Friday push $(date +%Y-%m-%d)"
```

### Step 3 — Push to GitHub

```bash
git push origin main
```

Confirm the push succeeds before touching the retail Pi. If the push fails, diagnose before proceeding — do not attempt to deploy from an unpushed state.

### Step 4 — Deploy to retail Pi

SSH to the retail Pi and run the pull script:

```bash
ssh pi@<retail-pi-ip>
cd /home/pi/synthos/synthos_build
bash src/qpull.sh
```

`qpull.sh` does three things:
1. `git pull` from GitHub
2. Kills the running `portal.py` process
3. Restarts `portal.py` in the background

Expected output:
```
Pulling from GitHub...
[fast-forward output]
Restarting portal...
✓ Portal restarted
✓ Synthos updated
```

If `git pull` fails (merge conflict, auth issue, dirty working tree), **stop here**. Do not force-resolve on the retail Pi — diagnose on the company Pi first.

### Step 5 — Verify portal is up

From the retail Pi (or via browser if the portal is accessible):

```bash
curl -s http://localhost:5001 | head -5   # expect HTML response
```

Or open the portal in a browser. If the portal is not responding within 60 seconds, check the log:

```bash
tail -50 logs/portal.log
```

### Step 6 — Confirm watchdog post-deploy watch is active

Blueprint should have posted a `deploy_watches` record to `company.db` when it finalized the merge. Watchdog reads this and enters 48-hour heightened monitoring automatically on its next cycle.

Verify the record exists on the company Pi:

```bash
sqlite3 /home/pi/synthos-company/data/company.db \
  "SELECT suggestion_id, deployed_at, watch_until FROM deploy_watches ORDER BY deployed_at DESC LIMIT 3;"
```

If no record is present, Blueprint did not post the watch entry. Create it manually:

```bash
sqlite3 /home/pi/synthos-company/data/company.db \
  "INSERT INTO deploy_watches (suggestion_id, deployed_at, watch_until, status)
   VALUES ('manual-$(date +%Y%m%d)', datetime('now'), datetime('now', '+48 hours'), 'ACTIVE');"
```

### Step 7 — Log the push

Note the push in the day's log or morning report. No special tooling required — a simple note that the push happened, what was included, and that watchdog is active is sufficient.

---

## Post-Push Window (Friday evening through Sunday)

Watchdog runs its heightened scan every hour for 48 hours after the deploy record timestamp. Patches monitors for regressions.

**Saturday 3:55am ET:** `shutdown.py` runs the weekly maintenance shutdown.
**Saturday 4:00am ET:** Pi reboots (`sudo reboot` via cron). All services restart via `@reboot` cron entries — no manual intervention needed.

After the reboot, confirm services are back up:

```bash
# Saturday morning — verify post-reboot health
ssh pi@<retail-pi-ip>
tail -20 /home/pi/synthos/synthos_build/logs/boot.log
```

---

## Rollback Procedure

**Automatic (post-trading mode only):** Watchdog triggers rollback automatically if a deployed agent crashes repeatedly within the 48-hour watch window. This requires a deploy_watches record and a `.known_good/` snapshot to exist. Not yet runtime-tested — see Phase 5 task list.

**Manual (current, always available):**

```bash
# On retail Pi — roll back to the known-good snapshot
cd /home/pi/synthos/synthos_build
python3 src/patch.py --rollback <filename>   # per-file rollback

# Or: revert the git commit and re-deploy
git revert HEAD --no-edit
git push origin main
bash src/qpull.sh
```

If the regression is severe and the Pi is unresponsive, the Saturday reboot (or a manual `sudo reboot`) will restart all services from the current `main`. If `main` is the problem, the operator must SSH in and manually revert before rebooting.

**Sunday morning deadline:** Any regression that cannot be resolved by Sunday morning is a mandatory rollback. Do not hold a broken Friday push into the Monday build window.

---

## What Each Agent Does Automatically

| Agent | Friday action |
|-------|--------------|
| **Blueprint** | Finalizes approved suggestions; merges update-staging → main; posts deploy_watches record; marks suggestions as `implemented` |
| **Patches** | Activates post-deploy watch mode; hourly regression scans for 48h; writes anomalies to suggestions queue |
| **Watchdog** | Reads deploy_watches table; enters heightened monitoring; triggers rollback if crash threshold exceeded (post-trading mode only) |
| **Scoop** | Delivers any CRITICAL alerts from Patches or Sentinel during the post-deploy window |
| **Sentinel** | Continues normal heartbeat monitoring; any silence triggers escalation to Scoop as usual |

Blueprint and Patches are the active participants on Friday. The others react to events.

---

## What Is Not Automated (Phase 1)

- **No automated git pull on the retail Pi** — `qpull.sh` must be run manually by the project lead after pushing to GitHub. There is no cron entry for git pull.
- **No automated rollback in pre-trading mode** — watchdog can detect a crash but will not auto-rollback until trading mode is post-trading and the rollback has been runtime-tested (Phase 5).
- **No staging→production pipeline** — single Pi 2W only; beta/customer split is not active until Phase 2 hardware.

---

## Quick Reference

```
Friday after 4pm ET:
  1. Check morning report + Patches sign-off
  2. Review + approve/reject in command portal
  3. Confirm Blueprint merged update-staging → main
  4. git push origin main       (company Pi)
  5. ssh pi@<retail>
     bash src/qpull.sh          (retail Pi)
  6. Verify portal up: curl localhost:5001
  7. Verify deploy_watches record in company.db
  8. Done — watchdog takes over for 48h

Emergency rollback:
  python3 src/patch.py --rollback <file>
  OR: git revert HEAD + qpull.sh
```
