# Trader Restructure Plan

**Status:** DRAFT · research + spec phase · not approved for implementation
**Created:** 2026-04-20
**Branch:** `patch/logic-review-2026-04-20` (synthos-company)
**Owner:** Project lead
**Companion:** evidence from pi5 `logs/logic_audits/*_bolt_decisions.log`

---

## Why this plan exists

The 12-day "stability window" that started ~2026-04-18 measures hardware stability (no crashes, clean pipeline). It does not measure **trader readiness**. A structured review of the decision logs on 2026-04-20 revealed the trader has several interconnected logic-quality issues that a stability clock cannot surface:

1. Bot sits idle most market days — rarely fires a buy
2. When it buys, entries are often at local highs
3. Tight initial stops get chopped on normal intraday noise
4. Trader decision cadence (30 min) cannot catch intraday dips

Hardware-level monitoring reports all-green through all of these.

---

## Evidence from logs

### E1 — Gate 5 threshold is structurally unreachable without news

From `2026-04-16_bolt_decisions.log`, MSFT decision:

```
  composite_score : 0.6350
  threshold       : 0.75
  rel_strength_5d : 7.10%            ← MSFT outperforming SPY by 7.1% over 5d
  tier_score      : 0.70 × 0.25  = 0.175
  politician      : 0.50 × 0.20  = 0.100
  staleness       : 1.00 × 0.15  = 0.150
  interrogation   : 0.50 × 0.20  = 0.100
  sentiment       : 0.55 × 0.20  = 0.110
  screening_adj   : +0.00 (neutral)
  ──────────────────────────────────────
                   TOTAL  0.635
  result          : SKIP (0.635 < 0.75)
```

**Ceiling analysis:** with all inputs at "neutral" (0.5-ish), composite maxes at ~0.635. With threshold 0.75, *only strong-news signals can pass*. MSFT at +7.1% relative strength was skipped. This is the mathematical cause of the idle-too-often symptom.

### E2 — Initial stop too tight on fresh entries

From `2026-04-20_bolt_decisions.log`, morning MSFT stop-out:

```
  entry       : $424.23
  stop_level  : $420.75            ← 0.82% below entry
  exit        : $419.74            ← exited at stop 1 bar later
  hold_time   : < 1 trading day
```

MSFT's typical intraday range is 1–2%. A 0.82% stop on a fresh entry is *inside* the ticker's normal noise. Bought at top of intraday move → normal pullback → stop.

Contrast with trailing stops that *have* moved up:
```
  entry       : $392.59
  stop_level  : $422.44            ← trailed up, protecting +$29.85 profit
  exit        : $420.09
```

Working correctly. Problem is specifically the initial-placement stop, not the trailing mechanism.

### E3 — Cadence mismatch

`retail_market_daemon.py`:
```python
ENRICHMENT_INTERVAL_MIN = 30    # full enrichment + trade
RECON_INTERVAL_MIN      = 10
PRICE_POLL_INTERVAL_SEC = 60
```

Trader runs every 30 minutes. Intraday pullbacks often complete in 5–10 minutes. The trader cannot see them. By the time it ticks, the pullback has already reverted or deepened.

### E4 — Known gate gaps flagged in logs

- `GATE 4_EVENT_RISK — NOT_CHECKED` — TODO: EVENT_CALENDAR not yet integrated. Blind to earnings dates / scheduled events.
- `GATE 4_SPREAD — SKIP_CHECK` — "no quote data" — spread sanity check silently inoperative.
- Signal ID 394 (MSFT) appearing multiple times — dedup behavior under a same-ticker-repeated-buy flow worth verifying.

---

## Problems, ranked

| # | Problem | Severity | Evidence |
|---|---------|----------|----------|
| 1 | Gate 5 threshold structurally requires news | **HIGH** | E1 — maths |
| 2 | Trader can't watch intraday dips (30-min cadence) | **HIGH** | E3 — interval constants |
| 3 | Initial stop too tight for entry volatility | **HIGH** | E2 — MSFT $424→$420 0.82% |
| 4 | No entry-timing filter (buys extended moves) | MEDIUM | E2 — implied by stop-out pattern |
| 5 | Event calendar integration missing (Gate 4) | MEDIUM | E4 — TODO in code |
| 6 | Spread check silently skipped | LOW | E4 — "no quote data" |

---

## Proposed Restructure

### Change 1 — Split trader into its own continuous daemon

**Current:**
```
retail_market_daemon (30-min orchestrator)
  ├─ screener
  ├─ news
  ├─ sentiment
  └─ trade_logic (runs ONCE per tick)
```

**Proposed:**
```
retail_market_daemon (30-min enrichment only)
  ├─ screener
  ├─ news
  ├─ sentiment
  └─ writes VALIDATED signals + scores to signals.db

retail_trade_daemon (NEW — continuous during market hours)
  ├─ polls signals.db every N seconds
  ├─ reads live_prices (already 60s-fresh)
  ├─ per-signal state: "watching, ready-to-buy, triggered, cooling-off"
  ├─ continuous evaluation of entry conditions (pullback filters, etc.)
  └─ executes buys when conditions fire
```

**Why:**
- Decision cadence (seconds) decouples from intel cadence (minutes)
- Enables stateful "waiting for pullback" behavior
- Matches existing `retail_price_poller` pattern — sibling continuous loop

**Risks / design constraints:**
- Halt check (4-layer) must run in the trade daemon too; not just market daemon
- Approval notification logic (9:30, 12:00, 15:30) currently lives in market daemon — decide whether it stays there or moves
- SQLite busy_timeout already present, but two writers instead of one increases contention probability — verify
- Each daemon needs its own heartbeat + watchdog restart path
- Alpaca rate limit headroom with continuous polling — implement backoff
- Decision log currently shared in `bolt_decisions.log` — decide whether trade daemon writes to same file or its own

**Open questions:**
- What's the right polling cadence for the trade daemon — 5s? 30s? 60s?
- Should the enrichment daemon signal the trade daemon when new VALIDATED signals land, or is pure polling fine?
- Does the trade daemon watch exit conditions too (trailing stops, take-profits)? Alpaca trailing stops are server-side, so technically no — but human-in-loop exits (approval queue execution) still need a runner.

### Change 2 — Gate 5 composite rebalance

**Current behavior:** max neutral-news composite = 0.635, threshold 0.75 → news required.

**Proposed:**
- Either lower threshold (e.g. 0.55) and tighten downstream gates (G7 sizing, G8 stop) to compensate for weaker signals getting through
- Or increase the weight of `screening_adj` (currently neutral = +0.00) so sector momentum can meaningfully boost composite
- **Preferred:** introduce `sector_momentum_component` as a first-class scored input, weighted ~0.20, so a +0.8 momentum ticker adds ~0.16 to composite → brings a neutral-news signal into the 0.75 threshold if momentum is present

**Companion change:** introduce a **Candidate Generator** step upstream of the trader (either in enrichment daemon or trade daemon, TBD) that emits sector-driven candidates with no news required. News becomes a veto/flag, not the trigger.

### Change 3 — ATR-based initial stop

**Current:** stop distance appears to be fixed percent.

**Proposed:**
- Initial stop distance = max(1.5 × ATR_14, hardcoded floor %)
- Lets the thesis breathe within the ticker's normal noise
- Trailing stop mechanics unchanged — only initial placement changes

**Sizing coupling:** with ATR-based stops, risk-per-trade (dollars lost at stop) should drive sizing, not fixed-percent equity. This means G7 Model B needs to become `position_size = risk_per_trade_dollars / stop_distance_dollars`.

### Change 4 — Entry-timing filter (pullback-only)

**Proposed new gate or addition to G6 (Entry decision):**
- Reject entry if price within X% of 5-day high
- Or require RSI_14 < 70 at entry
- Or require price within 0.5 × ATR of 20-day SMA

Goal: stop buying extended moves. Prefer dips within established momentum.

### Change 5 — Resolve Gate 4 gaps

- `EVENT_RISK`: integrate earnings calendar (free APIs exist — FMP, Finnhub free tier). Refuse entries within N days of earnings.
- `SPREAD`: ensure live quote data reaches the gate; currently silently skipping.

---

## [RESERVED] User-proposed structural changes

*Space below for additional proposals — will be filled in next conversation.*

- Proposal A: —
- Proposal B: —

---

## Implementation phasing (draft)

| Phase | Scope | Depends on |
|---|---|---|
| 0 | This spec reviewed + approved | — |
| 1 | Trade daemon split (Change 1) — trader moves to its own process, no behavior change | 0 |
| 2 | Candidate Generator (Change 2 companion) + Gate 5 rebalance | 1 |
| 3 | ATR-based stop + sizing refactor (Change 3) | 1 |
| 4 | Pullback entry filter (Change 4) | 1, 3 |
| 5 | Gate 4 gap fills (Change 5) | independent — can happen any time |

Each phase is its own patch branch off main. No phase merges without its predecessor in place.

---

## Validation criteria before any phase ships

- [ ] Paper-trading run of at least 5 market days with new logic vs old logic side-by-side
- [ ] Compare: buys/day, avg hold time, stop-out rate, win/loss ratio, max drawdown
- [ ] Log inspection of at least 20 representative decisions, including at least 3 stop-outs and 3 winners
- [ ] No regression in false-positive rate (entries that should not have fired)

---

## Open questions (for next pass)

1. Should the trade daemon also handle exit monitoring beyond Alpaca trailing stops? Approval-queue execution, user-triggered exits, etc.
2. Does the trade daemon need its own "cooling off" table to prevent immediate re-entry after a stop-out on the same ticker?
3. What's the right place for "no news" candidate emission — enrichment daemon (emits candidates as part of its 30-min tick) or trade daemon (pulls sector_scores + synthesizes candidates continuously)?
4. How do we A/B test a phase safely when it's on a single pi?

---

## Related specs

- `AUTO_USER_TAGGING.md` — per-position management (already patch-complete)
- `HALT_AGENT_REWRITE.md` — halt system v2 (already patch-complete)
- `BACKUP_ENCRYPT_AND_SPLIT_PLAN.md` — deferred, unrelated
