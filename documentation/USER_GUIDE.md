# SYNTHOS USER GUIDE

**Version:** 1.0
**Last Updated:** 2026-03-30
**Audience:** Project lead — the person who approves trades and manages the system week to week
**Status:** Living document — add a new section each time a new user-facing ability is shipped

---

## What This Guide Covers

This guide is organized around what you actually do with Synthos, not how it works internally. Each section covers one ability: what it is, how to use it, and what to do when something goes wrong.

**Current abilities:**
1. [The Portal — your daily dashboard](#1-the-portal)
2. [Approving and rejecting trade signals](#2-trade-approvals)
3. [Email alerts and notifications](#3-email-alerts)
4. [The Friday push — deploying updates](#4-the-friday-push)
5. [System health — checking that everything is running](#5-system-health)
6. [Emergency rollback](#6-emergency-rollback)
7. [The morning report](#7-the-morning-report)

---

## 1. The Portal

The portal is your primary interface. It runs on the retail Pi and shows everything happening in the trading system.

**Access:**
```
http://<retail-pi-ip>:5001
```
Or via Cloudflare tunnel: `ssh2.synth-cloud.com` (SSH) — the portal is local to the Pi.

**What you'll see:**

| Section | What it shows |
|---------|---------------|
| Dashboard | Portfolio value, open positions, P&L, agent status |
| Pending Approvals | Trades waiting for your decision |
| Positions | Open positions with entry price and trailing stop |
| Signal Feed | All signals scored by the agents, approved and rejected |
| News Feed | Congressional disclosure articles driving signals |
| Member Weights | Per-politician win rate history |
| Settings | Portal config (password, API keys, risk parameters) |

**Login:**
The portal requires a password set in `user/.env` as `PORTAL_PASSWORD`. If you've forgotten it:
```bash
# On retail Pi
grep PORTAL_PASSWORD /home/pi/synthos/synthos_build/user/.env
```

**If the portal is not responding:**
```bash
# Check if it's running
ps aux | grep portal.py

# Check logs
tail -50 /home/pi/synthos/synthos_build/logs/portal.log

# Restart manually
cd /home/pi/synthos/synthos_build
pkill -f portal.py
nohup python3 src/portal.py >> logs/portal.log 2>&1 &
```

---

## 2. Trade Approvals

Synthos operates in supervised mode. No trade executes without your approval. When agent1 (Bolt) scores a congressional signal above the confidence threshold, it queues the trade and emails you.

### The email

When a trade is queued you'll receive an email from `Synth_Alerts@synth-cloud.com` with the subject `[Synthos] Trade approval required — TICKER`. It contains:

- Ticker, company, politician
- Proposed price, shares, and max trade size
- Volatility label and confidence score
- The disclosure headline that triggered the signal
- Bolt's reasoning

### Approving or rejecting

Open the portal → **Pending Approvals** tab.

Each queued trade shows:
- Signal details (same as the email)
- Bolt's full reasoning
- Scout's research notes
- Pulse's sentiment assessment

**Approve** → Bolt executes the trade via Alpaca (paper mode). You'll see it move to Positions.

**Reject** → Signal is archived. Bolt notes the rejection and adjusts future scoring for that member/sector.

### What happens after approval

1. Bolt submits a bracket order via Alpaca: entry + trailing stop
2. Position appears in the Positions tab
3. Watchdog begins monitoring that position for protective exit conditions
4. Outcome is recorded in the ledger when the position closes

### Approval expiry

Approvals expire after 48 hours. Stale signals (price moved significantly, session changed) are auto-expired and removed from the queue. You don't need to act on them.

### If you miss an approval email

Check the Pending Approvals tab in the portal directly. The queue is always the source of truth.

---

## 3. Email Alerts

All alerts route through Scoop (`scoop.py`) on the company Pi. Scoop sends from `Synth_Alerts@synth-cloud.com` to `personal_prometheus@icloud.com`.

### Alert types you'll receive

| Priority | Event | When |
|----------|-------|------|
| P0 | Protective exit triggered | A position hit its safety exit — needs your attention |
| P1 | Heartbeat silence | Retail Pi has gone quiet during market hours |
| P2 | Trade approval required | New signal queued for your decision |
| P3 | Morning report | Daily at 8am ET (see §7) |

### Checking Scoop's status

```bash
# On company Pi
python3 /home/pi/synthos-company/agents/scoop.py --status
```

Output shows queue counts by status (PENDING, SENT, FAILED, RETRY).

### If emails stop arriving

**Step 1 — Check Scoop is running:**
```bash
ps aux | grep scoop.py
```

**Step 2 — Check the log for errors:**
```bash
tail -50 /home/pi/synthos-company/logs/mail_agent.log
```

**Step 3 — Send a test:**
```bash
python3 /home/pi/synthos-company/agents/scoop.py --test
```

**Step 4 — If test fails with 403:** SendGrid sender verification may have lapsed. Log into sendgrid.com → Settings → Sender Authentication → confirm `synth-cloud.com` is verified.

**Step 5 — Restart Scoop:**
```bash
pkill -f scoop.py
nohup python3 /home/pi/synthos-company/agents/scoop.py >> /home/pi/synthos-company/logs/mail_agent.log 2>&1 &
```

### Configuring alert preferences

Alert recipient and sender are in `company.env`:
```
OPERATOR_EMAIL=personal_prometheus@icloud.com
SENDGRID_FROM=Synth_Alerts@synth-cloud.com
```

Edit and restart Scoop to change them.

---

## 4. The Friday Push

New code ships on Fridays after market close (4pm ET). Blueprint builds during the week; you approve and ship on Friday.

**Friday is a push day, not a build day.** Nothing new is built on Fridays.

### Full runbook

See: `documentation/governance/FRIDAY_PUSH_RUNBOOK.md`

### Quick reference

```
Friday after 4pm ET:
  1. Check morning report — no CRITICAL items, Patches sign-off present
  2. Portal → Pending Changes → approve or reject each item
  3. Confirm Blueprint merged update-staging → main on company Pi
  4. git push origin main          (company Pi)
  5. ssh pi@<retail-pi-ip>
     bash src/qpull.sh             (retail Pi)
  6. curl http://localhost:5001    (verify portal up)
  7. Verify deploy_watches record in company.db
```

### When to skip the push

Skip entirely if:
- Any CRITICAL item in the morning report is unresolved
- Patches sign-off says `Ready for Friday push: NO`
- An open crash alert from Sentinel hasn't been cleared
- The `.blueprint_staging/` directory has stale `.staged` or `.tmp` files

A skipped Friday push is not a problem. Changes wait until next week.

### Post-push window

Watchdog runs heightened monitoring for 48 hours after each push. Patches scans for regressions hourly. Saturday at 4am ET the Pi reboots automatically — all services restart, no manual action needed.

---

## 5. System Health

### Quick health check

```bash
# On company Pi — check all agents
python3 /home/pi/synthos-company/agents/health_check.py 2>/dev/null || \
  ps aux | grep -E "blueprint|sentinel|patches|scoop|vault"

# On retail Pi — check trading agents
ps aux | grep -E "agent1|agent2|agent3|portal"
tail -20 /home/pi/synthos/synthos_build/logs/boot.log
```

### Agent roster and what each one does

**Retail Pi (trading):**

| Agent | Name | Role |
|-------|------|------|
| trade_logic_agent.py | Bolt | Scores signals, queues approvals, executes trades |
| news_agent.py | Scout | Fetches congressional disclosures, scores members |
| market_sentiment_agent.py | Pulse | Market sentiment scan, issues pulse_warning |
| portal.py | Portal | Your web interface on port 5001 |
| watchdog.py | Watchdog | Monitors for crashes, triggers rollback if needed |

**Company Pi (operations):**

| Agent | Role |
|-------|------|
| blueprint.py | Builds and stages approved code changes |
| sentinel.py | Heartbeat monitor — alerts if retail Pi goes quiet |
| patches.py | Continuous code audit, regression detection |
| scoop.py | All outbound email |
| vault.py | License key management |
| librarian.py | CVE scanning |
| strongbox.py | Automated backups |
| timekeeper.py | DB slot coordination |

### Heartbeat monitoring

Sentinel checks for heartbeats from the retail Pi every few minutes. If the Pi goes silent during market hours, Sentinel escalates to Scoop, which emails you.

To check heartbeat history:
```bash
sqlite3 /home/pi/synthos-company/data/company.db \
  "SELECT pi_id, last_seen, status FROM heartbeats ORDER BY last_seen DESC LIMIT 5;"
```

### Checking signals.db (retail Pi)

```bash
# Recent signals
sqlite3 /home/pi/synthos/synthos_build/signals.db \
  "SELECT ticker, adjusted_score, decision, created_at FROM signals ORDER BY created_at DESC LIMIT 10;"

# Pending approvals
sqlite3 /home/pi/synthos/synthos_build/signals.db \
  "SELECT ticker, price, max_trade, confidence, created_at FROM pending_approvals WHERE status='PENDING';"

# Open positions
sqlite3 /home/pi/synthos/synthos_build/signals.db \
  "SELECT ticker, entry_price, shares, created_at FROM positions WHERE status='OPEN';"
```

---

## 6. Emergency Rollback

If a Friday push breaks something and the portal won't restart or agents are crashing:

### Option A — Per-file rollback (surgical)

```bash
# On retail Pi
cd /home/pi/synthos/synthos_build
python3 src/patch.py --rollback <filename>
```

Use this when you know which file is broken. Patch restores from the `.known_good/` snapshot taken before the push.

### Option B — Git revert (full push rollback)

```bash
# On company Pi
cd /path/to/synthos-retail-repo
git revert HEAD --no-edit
git push origin main

# On retail Pi
cd /home/pi/synthos/synthos_build
bash src/qpull.sh
```

### Option C — Manual reboot

If the Pi is unresponsive and you can't SSH in:
- Physical power cycle
- All services restart automatically via `@reboot` cron entries
- If main branch is still broken after reboot, use Option B before rebooting again

### Deadline

**Sunday morning.** Any regression not resolved by Sunday morning is a mandatory rollback. Do not hold a broken push into the Monday build window.

---

## 7. The Morning Report

Every day at 8am ET, Blueprint (or Patches) generates `data/morning_report.json` on the company Pi, and Scoop delivers it to your inbox.

### What the morning report contains

- Portfolio value and today's realized P&L
- Trade count (wins / losses)
- Open positions summary
- Any CRITICAL items from Patches or Sentinel
- Friday push readiness: `Ready for Friday push: YES/NO`

### If the morning report doesn't arrive

**Most common cause:** Scoop is not running. Check:
```bash
ps aux | grep scoop.py
tail -20 /home/pi/synthos-company/logs/mail_agent.log
```

**Second cause:** `morning_report.json` wasn't generated (Blueprint didn't run or errored):
```bash
ls -la /home/pi/synthos-company/data/morning_report.json
cat /home/pi/synthos-company/data/morning_report.json
```

**Third cause:** The report date doesn't match today — Scoop only sends reports dated for the current day.

---

## Appendix — Quick Reference

### SSH access

```
Company Pi (Pi 4B):  ssh pi@ssh.synth-cloud.com
Retail Pi (Pi 2W):   ssh pi@ssh2.synth-cloud.com
Local (from Pi 4B):  ssh pi@10.0.0.121
```

### Key file locations

| File | Location | Purpose |
|------|----------|---------|
| Retail config | `synthos_build/user/.env` | API keys, trading mode, portal password |
| Company config | `synthos-company/company.env` | SendGrid, operator email, service ports |
| Trading DB | `synthos_build/signals.db` | All signals, positions, approvals, ledger |
| Company DB | `synthos-company/data/company.db` | Heartbeats, suggestions, scoop queue |
| Portal log | `synthos_build/logs/portal.log` | Portal errors |
| Mail log | `synthos-company/logs/mail_agent.log` | Scoop send/fail history |
| Boot log | `synthos_build/logs/boot.log` | Agent startup on reboot |

### Restart any agent

```bash
# Pattern for retail Pi
pkill -f <agent_file.py>
nohup python3 /home/pi/synthos/synthos_build/src/<agent_file.py> >> logs/<name>.log 2>&1 &

# Pattern for company Pi
pkill -f <agent_file.py>
nohup python3 /home/pi/synthos-company/agents/<agent_file.py> >> logs/<name>.log 2>&1 &
```

### Trading mode

```
TRADING_MODE=PAPER    ← current, always; in user/.env
TRADING_MODE=LIVE     ← Phase 6 only; explicit human action required
```

Never change `TRADING_MODE` to LIVE without completing the Phase 6 gate (30-day clean paper run, full validation, project lead sign-off).

---

*Add a new section to this guide each time a new user-facing ability ships.*
