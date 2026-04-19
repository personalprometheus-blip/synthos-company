# Halt Agent rewrite (kill switch v2)

**Status:** plan approved 2026-04-19, build queued for next patch branch
**Session:** 2026-04-19 (after AUTO/USER tagging)
**Target branch name:** `patch/<date>-halt-agent-rewrite`

---

## Why this rewrite

The existing kill-switch system has three real issues:

1. **Admin kill halts the whole system**, not just trading. Dispatch stops launching subprocesses entirely. This kills heartbeats, delays observability recovery, and makes resumption slower than it needs to be.

2. **Skip check runs too late** in the trader. Kill is checked inside Gate 1, which means the trader has already done the Gate 0 account health work (orphan adoption, price sync, etc.) before noticing it should have skipped.

3. **No operator-facing signal.** The portal shows `kill_switch: true` in the status JSON but doesn't surface a banner. Customers don't know their trading is halted without checking details.

## What the rewrite does

### Customer-facing UI language
- "Halt Agent" / "Resume Agent" (replaces "kill switch" user-facing)
- Internal code keeps `kill_switch` variable/setting names (minimize churn)

### Two halt sources, same skip-customer flow

| Source | Flag storage | UI banner |
|---|---|---|
| Customer halt | `KILL_SWITCH='1'` in customer's signals.db settings (existing) | Amber banner — "You activated the Halt. Click Resume to reactivate." |
| Admin halt | DB-backed in a system-settings table; legacy `.kill_switch` file still honored as fallback | Red banner — "Admin has activated the Halt for maintenance. Service will return shortly." |

Both result in: **trader subprocess exits cleanly at entry; daemon iterates to next customer; all other subsystems keep running.**

### Entry check placement
At the very top of `retail_trade_logic_agent.run()`, before any DB connection or Alpaca call:

```python
def run(session="open"):
    # Halt check — before ANY work
    admin_halt = _read_admin_halt()       # single DB read, file fallback
    customer_halt = _read_customer_halt() # single DB read from this customer
    if admin_halt or customer_halt:
        kind = 'admin' if admin_halt else 'customer'
        _log_halt_skip(kind)
        sys.exit(0)
    # ... rest of run() ...
```

### Daemon change
Remove the dispatch-stopping check at `run_trade_all_customers` line 408 so dispatch iterates every customer. Each subprocess checks at entry and skips. Heartbeats, scheduler, news/sentiment/screener, price poller, portal — all continue normally.

### Reason comment box
When a customer or admin clicks Halt or Resume, a modal prompts for a reason (optional). Submitted to the API, logged alongside the halt event.

Format in system_log:
```
HALT_ACTIVATED   src=customer  reason="market felt too volatile"
HALT_DEACTIVATED src=customer  reason="resuming after the Fed news"
HALT_ACTIVATED   src=admin     reason="deploying fix for ORM bug"
```

### Collapsible banner

Default state: thin horizontal strip across the top of the dashboard, ~10pt font, scrolling text.

```
⚠ Trade Agent Halted — until User Resumes                   [ ∧ ]
```

Click expands to full details:

```
You activated the Halt on 2026-04-19 at 14:32 ET
Reason: "Market felt too volatile"

✓ Existing Alpaca stop-loss orders remain active — positions are still protected
✓ You can still trade directly on Alpaca
✓ Your portfolio values and prices continue to update

✗ The bot will not open, close, or manage any positions
✗ Approval-queue trades will not execute until you Resume

                                                  [ Resume Agent ]  [ ∧ ]
```

Admin-halt variant: red accent; reason prefixed "Admin note: ..."; no Resume button for customers (they can't clear an admin halt). Optional "expected return time" if admin filled one in.

### Approval queue during halt

**Decision:** option B — allow Approve clicks during halt, but trader skips at entry, so approved trades sit in the queue untouched. On resume, the trader's next dispatch processes them normally. Customer doesn't have to wait for resumption to click Approve; their queue is pre-staged.

Implication: the collapsible banner's "what happens" list needs the bullet "Approval-queue trades will not execute until you Resume."

## Code surface

### New DB helpers (retail_database.py)
- `get_admin_halt() -> dict | None` — returns `{active, reason, set_by, set_at}` or None
- `set_admin_halt(active: bool, reason: str, set_by: str) -> None`
- `get_customer_halt() -> dict | None` — same shape for customer halt
- `set_customer_halt(active: bool, reason: str, set_by: str) -> None`

Admin halt lives in a new `system_settings` table (or reuses customer_settings with a reserved row). Confirmed plan: new small table `system_halt` (singleton row) for auditability.

### System halt table (new)

```sql
CREATE TABLE IF NOT EXISTS system_halt (
    id             INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton row
    active         INTEGER NOT NULL DEFAULT 0,            -- boolean 0/1
    reason         TEXT,
    set_by         TEXT,
    set_at         TEXT,
    expected_return TEXT                                   -- admin-only optional
);
```

Lives on the master customer's signals.db (so trader subprocesses, already reading shared DB, have a consistent place to check).

### New API endpoints

Customer-facing (retail portal):
- `POST /api/halt-agent`      `{active: bool, reason?: str}`  — sets customer halt
- `GET /api/halt-status`      — returns `{customer_halt, admin_halt}` for banner render

Admin-facing (command portal / pi4b):
- `POST /api/admin/halt-agent`  `{active: bool, reason?: str, expected_return?: str}` — sets admin halt
- rewire existing monitor-page kill button to this endpoint

### Trader / daemon changes
- Trader `run()`: new first-line halt check → clean exit if halted
- Trader Gate 1: remove redundant kill check (first-line already caught it)
- Daemon `run_trade_all_customers`: remove dispatch-stopping file check at line 408
- Daemon writes a `DAEMON_DISPATCH_HALTED_N_CUSTOMERS` heartbeat annotation when many customers in a row skip due to admin halt (so the auditor can see it)

### Banner UI (retail portal)
- New `<div id="halt-banner">` at the very top of every dashboard-ish page (collapsible)
- New `renderHaltBanner(haltStatus)` JS function
- `haltStatus` fetched from `/api/halt-status` alongside `/api/status` polling
- Collapse state persisted in localStorage per customer so it stays collapsed between visits if the customer wants it collapsed

## Phased build plan

| Phase | Content | Visible to customer? |
|---|---|---|
| 0 | Investigation — map all current kill-switch call sites, confirm admin portal button wiring, identify legacy comment drift (72h doc) | No |
| 1 | DB schema — `system_halt` table + DB helpers (`get_admin_halt`, `set_admin_halt`, customer equivalents) | No |
| 2 | Trader entry check — first-line skip, clean exit before any work | Invisible unless halt state changes |
| 3 | Daemon rewire — remove dispatch-halting file check, add per-customer-in-session heartbeat annotation | Invisible unless admin halts |
| 4 | Customer halt API + endpoint — `POST /api/halt-agent`, `GET /api/halt-status` | Invisible (no UI yet) |
| 5 | Admin halt API + rewire existing monitor button on command portal | Admin-visible |
| 6 | Collapsible banner UI (retail portal) — thin strip + expanded view + reason modal | Customer-visible |
| 7 | Reason logging to system_log with src + reason on activate/deactivate | Admin-visible in logs |
| 8 | Docs — fix stale 72h comment at retail_trade_logic_agent.py:124; update CLAUDE.md + system architecture page; spec update | — |

Estimated ~12 hours focused work.

## Pre-merge checklist

- [ ] Halt fires → trader subprocess exits in <50ms on next launch (measure with a timing test)
- [ ] Halt fires → heartbeats keep firing, scheduler keeps running, news/sentiment/screener keep writing to shared DB
- [ ] Alpaca trailing-stop orders remain visible on Alpaca account (not cancelled by halt)
- [ ] Customer can't clear admin halt; admin can clear customer halt (admin override capability)
- [ ] Reason appears in system_log with correct `src` and timestamp
- [ ] Banner collapse state persists across page reloads (localStorage)
- [ ] Approval-queue items survive halt cleanly — approved trades execute on resume
- [ ] News baseline cycles all still IDENTICAL
- [ ] `DEPLOY_NOTES.md` deleted or rolled into merge commit

## Open items not decided

- Admin-halt `expected_return` field — is this rendered in the banner, or admin-log only? Low priority, can default to showing if populated.
- Should Resume action require a confirmation dialog? ("Are you sure? Trading will resume immediately.") My vote: yes for admin; no for customer (customer can re-halt easily).

## Things noted for documentation cleanup

- `retail_trade_logic_agent.py:124` comment mentions "72-hour expiry" — actual expiry is **tier-dependent** (30d/7d/2d/1d). Fix in Phase 8.
- Check `synthos-company/documentation/` for other references to "72 hour" and update.

## Deferred — not in this rewrite

- Per-customer "pause for N hours" auto-resume — interesting future feature; keeps a user from forgetting they halted and wondering why the bot is quiet.
- Email / SMS alert on halt/resume — uses existing scoop channel, deferred until after v1 ships.
- Halt history view (dashboard widget showing recent halt/resume events) — nice-to-have, not in v1.
