# AUTO / USER per-position management

**Status:** built on `patch/2026-05-03-auto-user-tagging`, merge target 2026-05-03
**Design session:** 2026-04-19
**Related:** `synthos_build/docs/backlog.md` (C10 centralize price polling, deferred)

---

## What this feature does

Customers share one Alpaca account with the bot. They can buy anything they
want directly in Alpaca, and the bot wants to manage a hard-capped number of
positions (v1: **12 per customer**) without interfering with positions the
customer chose themselves.

Every position gets a **`managed_by`** tag:

| Tag | Meaning | Who sells it |
|---|---|---|
| `bot`  | Bot opened this via a signal; bot manages exits | Bot (trailing stop, stop-loss, protective exit) |
| `user` | Customer opened this directly via Alpaca | Customer only — bot skips all exit logic for this row |

Plus a per-ticker **sticky USER** preference: operator can mark a specific
ticker (e.g. long-term BRK.B holding) so the bot SKIPS all future signals for
it. The bot never takes AUTO positions in sticky-USER tickers.

## Core semantics

### Tag assignment
- Bot's own buys (rotation, supervised-approval, automatic) → `managed_by='bot'`
- Alpaca orphans (discovered on Alpaca but not in our DB) → `managed_by='user'`
- User can flip via the portal's AUTO/USER toggle per-position

### What trader does with each tag
| Action | `managed_by='bot'` | `managed_by='user'` |
|---|---|---|
| Read current price | ✅ (for dashboard) | ✅ (for dashboard) |
| Apply trailing stop ratchet | ✅ | ❌ skipped |
| Apply late-day stop tightening | ✅ | ❌ skipped |
| Enforce protective/pulse exit | ✅ | ❌ skipped |
| Sell on rotation | ✅ | ❌ (row is untouchable) |

### Sticky preference
- Operator can mark a ticker as **sticky USER** via the lock icon in the UI
- Bot's signal-evaluation loop checks `position_preferences` first; if
  `sticky='user'`, logs `SIGNAL_SKIPPED_STICKY_USER` and skips the signal
- Does NOT affect existing positions — operator can still hand off an
  existing AUTO position via the row toggle independently

### Sizing (Model B + C hybrid)
- Risk math computed off **total account equity** (Alpaca ground truth,
  cached to `_ALPACA_EQUITY` setting by GATE 0) — Model B
- Order size capped at **available cash** — Model C guard
- If the cash cap shrinks the order below 70% of intended → SKIP with
  `SKIP_INSUFFICIENT_CASH_AFTER_MANUAL` decision, signal marked evaluated
- Fallback: if `_ALPACA_EQUITY` unavailable, falls back to cash-based
  (preserves pre-feature behavior)

### Cap enforcement
- Server-side hard cap at 12 AUTO positions per customer
- UI disables USER→AUTO promotion when cap full
- No queue — user tries again after closing an AUTO position

## Data model

### `positions.managed_by` — TEXT NOT NULL DEFAULT 'bot'
Added via migration on next customer DB open. Domain `'bot'|'user'` enforced
application-side (SQLite ALTER can't add CHECK to existing tables). Default
preserves existing positions as bot-managed.

### `position_preferences`
```sql
CREATE TABLE position_preferences (
    ticker   TEXT PRIMARY KEY,
    sticky   TEXT NOT NULL CHECK (sticky IN ('user','bot')),
    set_by   TEXT NOT NULL,            -- 'user' | 'system'
    set_at   TEXT NOT NULL
);
```

`sticky='bot'` reserved for a future "pre-approve this ticker for AUTO"
preference — **not used in v1**. Only `'user'` is wired up.

## API endpoints (retail portal)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/positions/<pos_id>/managed-by` | POST | Flip a position's tag. Body: `{managed_by: 'bot'\|'user'}`. Rejects with 409 if trying to promote above cap or when ticker has sticky-user preference. |
| `/api/ticker-preferences/<ticker>` | POST | Set/clear sticky preference. Body: `{sticky: 'user' \| null}`. |
| `/api/auto-slots` | GET | `{auto, user, capacity, can_promote}` — for header counter + promotion UX |
| `/api/status` | GET | Existing endpoint, now includes `positions[i].managed_by`, `positions[i].sticky`, and `user_warnings[]` |

## UI surface

### Open Positions card header
- Counter: `X/12 auto · Y user [· N orphan]`
- Tooltip: "Auto: bot manages · User: you manage · Cap is 12 auto positions per customer at current capacity; future iterations may raise this."

### Per-row
- AUTO/USER pill (click to flip). Disabled when orphan OR at-cap-and-trying-to-promote.
- Lock icon next to pill: 🔓 (no sticky) vs 🔒 amber (sticky USER). Click opens native `confirm()` with explicit copy.

### Warning banners (above Open Positions card)
Two soft warnings, no notification spam:
- **Cap-underutilized (info):** bot managing < 40% of cap, and customer has ≥1 position
- **Cash-starved (warn):** ≥2 `SKIP_INSUFFICIENT_CASH_AFTER_MANUAL` in last 7 days

## Prefetch — price poller covers validated signals

`retail_price_poller._get_held_tickers()` now unions:
- OPEN positions across all customer DBs (existing)
- VALIDATED signals from the shared master DB (new)

All tickers trader might act on in the next dispatch cycle have a price
≤60s old in the `live_prices` table. Removes a previously-missing spot-
price Alpaca call from the trader's hot path.

## Not in v1

| Feature | Reason | Future consideration |
|---|---|---|
| Sticky BOT preference | Deferred — minimal added value, most of the behavior is default already | Add if operators ask for pre-approval flow |
| Auto promotion queue | Simplified to "disabled toggle" UX per operator preference 2026-04-19 | - |
| Email notifications on slot-free | In-app visual feedback sufficient | - |
| Raising the 12 cap | Waiting for Friday's dispatch-time measurements | Compute measured throughput and pick tier-dependent caps |

## Merge checklist (from DEPLOY_NOTES.md)

See `DEPLOY_NOTES.md` on the patch branch for the full pre-merge checklist.
Summary:

- [ ] News baseline cycles 01-05 all IDENTICAL (no regression)
- [ ] `system_health_daily` clean 3 days prior to merge
- [ ] Schema migration applied on a test customer DB copy
- [ ] UI QA on a test customer end-to-end
- [ ] Dispatch cycle time non-regressed on pi5
- [ ] `DEPLOY_NOTES.md` deleted
- [ ] `/system-architecture` portal page updated

## Rollback

`git revert <merge commit>` → push main → pull pi5 → restart portal. Schema
is additive (ADD COLUMN, IF NOT EXISTS CREATE TABLE), so rollback leaves
orphan columns but doesn't break older code paths.
