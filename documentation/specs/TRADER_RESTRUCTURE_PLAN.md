# Trader Restructure Plan

**Status:** DRAFT · consolidated 2026-04-20 · 10 changes · phases defined · not approved for implementation
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
- Signal ID 394 (MSFT) appearing multiple times — buy-stop-buy cycle; dedup/cooldown behavior worth verifying.

---

## Problems, ranked

| # | Problem | Severity | Evidence |
|---|---------|----------|----------|
| 1 | Gate 5 threshold structurally requires news | **HIGH** | E1 — maths |
| 2 | Trader can't watch intraday dips (30-min cadence) | **HIGH** | E3 — interval constants |
| 3 | Initial stop too tight for entry volatility | **HIGH** | E2 — MSFT $424→$420 0.82% |
| 4 | No entry-timing filter (buys extended moves) | MEDIUM | E2 — implied by stop-out pattern |
| 5 | Event calendar integration missing (Gate 4) | MEDIUM | E4 — TODO in code |
| 6 | Buy-stop-buy-stop cycles (no cooldown) | MEDIUM | E4 — Signal ID 394 recurrence |
| 7 | Spread check silently skipped | LOW | E4 — "no quote data" |

---

## Architecture — the shape we're building toward

- **Enrichment daemon** (30-min cadence) produces *intel*: sector scores, regime, sentiment, news_flags. No buys fired here.
- **Trade daemon** (continuous, market hours only) produces *action*: watches candidates for entry, executes, monitors exits.
- **Validation Stack** (Fault / Bias / Market State / Macro) runs at trader gate-time — **source-agnostic**, applies to every trade regardless of origin.
- **News annotates, never triggers.**

---

## Proposed Changes (10 items)

### 1. Split trader to its own continuous daemon
- Extract trade logic from `retail_market_daemon.py` into new `retail_trade_daemon.py`
- Polls `signals.db` + `live_prices` every ~30–60s during market hours only (9:30-16:00 ET)
- Inherits: halt check, approval notifications, heartbeat, watchdog integration
- Enrichment daemon keeps: screener, news, sentiment, validator stack — writes `signals` + `sector_scores` + `news_flags`

**Design constraints:**
- Halt check (4-layer) must run in the trade daemon too; not just market daemon
- Approval notification logic (9:30, 12:00, 15:30) currently lives in market daemon — decide whether it stays there or moves
- SQLite busy_timeout already present, but two writers instead of one increases contention probability — verify
- Each daemon needs its own heartbeat + watchdog restart path
- Alpaca rate limit headroom with continuous polling — implement backoff
- Decision log: decide whether trade daemon writes to `bolt_decisions.log` or its own

### 2. Candidate Generator (new component in enrichment daemon)
- Reads `sector_scores` for sectors with positive momentum
- Top-N per strong sector, filtered: validator = GO, not already held, passes liquidity floor
- Ranks by `sector_momentum × relative_strength × regime_match`
- Emits candidate signals marked `source='candidate'` into `signals.db`
- Runs as part of every enrichment tick (30 min)

### 3. Gate 5 composite rebalance
- Today: news inputs ceiling-limit composite to 0.635 (below 0.75 threshold) = news-required-to-buy
- Add `sector_momentum_component` as first-class scored input, weight ~0.20
- Reduce news component weights (tier / politician / interrogation / sentiment) combined to ~±0.2
- Lower threshold from 0.75 → ~0.55 (compensated by stricter downstream gates)
- Result: sector-strong tickers pass without news

### 4. News output reshape — from trigger to annotation

New table:
```sql
CREATE TABLE news_flags (
  ticker        TEXT NOT NULL,
  category      TEXT NOT NULL,   -- 'earnings_raise', 'analyst_upgrade', etc.
  severity      TEXT NOT NULL,   -- 'positive' | 'negative'
  score         REAL NOT NULL,   -- -1.0 to +1.0
  fresh_until   TEXT NOT NULL,   -- ISO timestamp, category-specific TTL
  notes         TEXT,
  created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_news_flags_ticker_fresh ON news_flags(ticker, fresh_until);
```

- Categories positive: `earnings_raise`, `analyst_upgrade`, `guidance_raise`, `breakout`, `catalyst`
- Categories negative: `earnings_miss`, `guidance_cut`, `regulatory_probe`, `management_change`, `litigation`
- TTL per category (e.g. earnings_raise = 5 days, regulatory_probe = 30 days)
- News agent writes to `news_flags` instead of creating VALIDATED signals directly

### 5. News integration at trader — 3-point touch

| Touch point | Role | Effect |
|---|---|---|
| **Gate 4 EVENT_RISK** | event detection | Reject entry if ticker has upcoming earnings or active event |
| **Gate 5 composite** | modifier | news_flags score adds/subtracts to composite (±0.2 typical) |
| **NEW Gate 5.5 VETO** | safety | Any flag with `severity score < −0.7` → reject regardless of composite |

**Exit logic:** news_flags with score < −0.5 on held positions triggers position review (not auto-sell; logged for admin decision or future auto-rule).

### 6. ATR-based initial stop + risk-per-trade sizing
- **Initial stop**: `max(1.5 × ATR_14, floor_pct)` — lets the thesis breathe within the ticker's normal noise
- **Sizing (G7 Model B overhaul)**: `position_size = risk_per_trade_dollars / stop_distance_dollars`
- Total risk per trade capped at ~0.5% of equity (configurable)
- Trailing stop mechanics unchanged (they work correctly per E2 evidence)

### 7. Pullback entry filter (new rule in Gate 6)
- Reject entry if ANY of:
  - price > `day_high − (0.5 × day_range)`  (within 0.5% of day-high)
  - `RSI_14 > 70` at entry
  - price more than `0.5 × ATR_14` above 20-day SMA
- Goal: stop buying extended moves; wait for pullbacks within established trend

### 8. Gate 4 EVENT_RISK — integrate earnings calendar
- Current: `NOT_CHECKED — TODO: DATA_DEPENDENCY`
- Free API options: FMP Basic, Finnhub free, Alpha Vantage
- Refuse entries within 2 days before / 1 day after earnings
- Pairs with `news_flags` for non-scheduled events

### 9. Gate 4 SPREAD — fix silent skip
- Current: `SKIP_CHECK — no quote data`
- Investigate why live quote data isn't reaching the gate (likely Alpaca snapshot timing or stale data path)
- Ensure spread check runs; add fallback to `(ask − bid) / mid` from `live_prices` if primary source fails

### 10. Cooling-off after stop-out

New table:
```sql
CREATE TABLE position_cooldown (
  ticker          TEXT NOT NULL,
  customer_id     TEXT NOT NULL,
  cooldown_until  TEXT NOT NULL,
  reason          TEXT,
  created_at      TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (ticker, customer_id)
);
```

- After a stop-out, ticker enters cooldown for N hours (default 24h)
- Prevents buy-stop-buy-stop cycles (observed with MSFT Signal ID 394 recurrence)
- Per-customer — different customers can hit different stops
- Trade daemon consults at Gate 2 (Dedup) — treats ticker-in-cooldown as already-held for entry purposes

---

## Implementation phasing

Each phase is its own patch branch off main. No phase merges without paper-trading validation. Phases 2 and 5 are low-risk and could theoretically ship independently; phase 3 and 4 are the ones that materially change trader behavior.

| Phase | Scope | Risk | Depends on |
|---|---|---|---|
| **1** | Trade daemon split (#1) — pure refactor, no behavior change | LOW | — |
| **2** | News output reshape (#4) — `news_flags` table, news agent writes flags | LOW | — (independent of Phase 1) |
| **3** | Candidate Generator (#2) + Gate 5 rebalance (#3) + News integration (#5) | MEDIUM | 1, 2 |
| **4** | ATR stops + sizing refactor (#6) + Pullback filter (#7) | MEDIUM-HIGH | 1 |
| **5** | Gate 4 gap fills (#8, #9) + Cooling-off (#10) | LOW | 1 |

---

## Validation criteria before any phase ships

- [ ] Paper-trading run of at least 5 market days with new logic vs old logic side-by-side (A/B mechanism TBD — see Open Questions)
- [ ] Compare: buys/day, avg hold time, stop-out rate, win/loss ratio, max drawdown
- [ ] Log inspection of at least 20 representative decisions, including at least 3 stop-outs and 3 winners
- [ ] No regression in false-positive rate (entries that should not have fired)
- [ ] System-map's "Pipeline & Gates" view updated to reflect new gate flow before Phase 3 ships

---

## Deferred (on the TODO list, not in this patch queue)

- **News companion daemon** — continuous news-watching daemon on its own process. Future optimization for when 30-min news cadence becomes the bottleneck for breaking-news-sensitive strategies. Defer until baseline v2 is stable and we can measure whether latency is actually costing trades.
- **A/B testing framework** — how to run new logic side-by-side with old on a single pi before full cutover. Needed before Phase 3 ships. Open question below.
- **Premium event calendar** — if free APIs (FMP/Finnhub/AlphaVantage) prove unreliable at earnings prediction, upgrade path.

---

## Open questions (for next pass)

1. **Trade daemon polling cadence** — 5s? 30s? 60s? Tradeoff: lower = more responsive, more Alpaca calls; higher = miss fast moves. Probably 30s during Phase 1 baseline, tune after measurement.
2. **Approval notification location** — stays in enrichment daemon or moves to trade daemon? Leaning enrichment (time-based, not event-based).
3. **Does the trade daemon handle exit monitoring beyond Alpaca trailing stops?** Approval-queue execution, user-triggered exits, news-driven exit triggers. Yes for execution, no for monitoring (Alpaca handles price).
4. **Decision log location** — `bolt_decisions.log` stays or new log per daemon? Leaning same file with a daemon-tag column; single search surface.
5. **Cooldown scope** — 10's per-customer model correct, or should cooldown apply fleet-wide (if one customer stops MSFT, everyone pauses)? Per-customer is more flexible.
6. **A/B mechanism** — how to run new trader logic side-by-side with old on one pi. Options:
   - Shadow mode: new logic runs read-only, writes decisions to separate log; compare vs actual
   - Customer-level split: half customers on old, half on new — requires customer-level feature flag
7. **Sector momentum scoring origin** — reuses `sector_scores` table (already written by screener) or does Candidate Generator compute its own? Leaning reuse.

---

## Related specs

- `AUTO_USER_TAGGING.md` — per-position management (already patch-complete, awaiting merge)
- `HALT_AGENT_REWRITE.md` — halt system v2 (already patch-complete, awaiting merge)
- `BACKUP_ENCRYPT_AND_SPLIT_PLAN.md` — deferred, unrelated
