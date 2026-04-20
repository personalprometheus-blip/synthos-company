# Trader Restructure Plan

**Status:** DRAFT · consolidated + decisions locked 2026-04-20 · 10 changes (+ 1a/1b window-calculator refinement) · 5 phases · not yet approved for implementation
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

**The deeper issue — a flawed premise, not just a tuning problem.**

The current trader was designed on the assumption that news signals would arrive with enough frequency and quality to keep trading activity meaningful. That assumption is wrong in practice. Most market days produce *no* actionable news signal that crosses a 0.75 composite threshold (see E1). Meanwhile:

- Sector momentum is real, persistent, and ignored as a *trigger* (only used as a small G5 boost)
- Market regime / macro context is evaluated but never originates a buy
- Cash sits idle waiting for news that doesn't come

**Lotto-ticket analogy:** the current system is designed like playing the lottery 3 times a year — it only fires when a rare news signal jackpots the composite past the threshold. Instead, it should play the market like a **professional portfolio manager** — positioning based on sector strength and market regime, using news to *inform* already-rational positions rather than *trigger* positions from nothing.

This restructure is not a re-tuning. It is a reframe of what drives buys.

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
- Polls `signals.db` + `live_prices` during market hours only (9:30-16:00 ET)
- **Cycle time**: bounded by customer count × per-customer evaluation time, running 3-at-a-time per existing `MAX_TRADE_PARALLEL=3`. Target cycle time ≤ 30s. Measure during Phase 1 build.
- Inherits: halt check (4-layer), heartbeat, watchdog integration
- **Approval notifications stay in enrichment daemon** (time-based 9:30 / 12:00 / 15:30, not event-driven)
- **Decision log shared**: trade daemon writes to `bolt_decisions.log` alongside enrichment decisions — one search surface

**Design constraints:**
- Halt check (4-layer) must run in the trade daemon too; not just market daemon
- SQLite busy_timeout already present, but two writers instead of one increases contention probability — verify during build
- Each daemon needs its own heartbeat + watchdog restart path
- Alpaca rate limit headroom with continuous polling — implement backoff

### 1a. Window Calculator (new agent in enrichment daemon)

Keeps the trade daemon lean by precomputing entry/exit conditions per candidate.

For each candidate signal, the Window Calculator computes:
```
entry_low       — lowest acceptable entry price (pullback target)
entry_high      — highest acceptable entry price (above = too extended)
initial_stop    — stop level if filled (ATR-based, per Change 6)
take_profit     — optional TP level
valid_until     — TTL for this window set
```

Runs as part of each enrichment tick. Writes to a new `trade_windows` table keyed by (signal_id, customer_id). Recomputed every tick so windows stay fresh.

### 1b. Trade daemon reads windows, not raw logic

Trade daemon's per-tick behavior becomes:
1. Read `trade_windows` for not-yet-filled candidates
2. For each candidate, compare `live_prices.price` against `(entry_low, entry_high)`
3. If in window + all downstream gates pass → fire order
4. Otherwise, move on

This makes the trade daemon O(candidates × customers) per tick with mostly integer comparisons. No heavy computation. The expensive work (ATR, rel_strength, gate scoring) lives in the enrichment tick where it belongs.

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

Each phase is its own patch branch off main. No phase merges without a validation window using the cutover strategy above. Phases 2 and 5 are low-risk and could theoretically ship independently; phase 3 and 4 are the ones that materially change trader behavior.

| Phase | Scope | Risk | Depends on |
|---|---|---|---|
| **1** | Trade daemon split (#1) — pure refactor, no behavior change. Trade daemon still runs same 14 gates as today, just in its own process. | LOW | — |
| **2** | News output reshape (#4) — `news_flags` table, news agent writes flags | LOW | — (independent of Phase 1) |
| **3** | Window Calculator agent (#1a) + Candidate Generator (#2) + Gate 5 rebalance (#3) + News integration (#5) + Trade daemon reads windows (#1b) | MEDIUM-HIGH | 1, 2 |
| **4** | ATR stops + sizing refactor (#6) + Pullback filter (#7) | MEDIUM | 1, 3 |
| **5** | Gate 4 gap fills (#8, #9) + Cooling-off (#10) + `daily_master.log` end-of-day archival task | LOW | 1 |

---

## Validation criteria before any phase ships

- [ ] Paper-trading run of at least 5 market days with new logic vs old logic side-by-side (A/B mechanism TBD — see Open Questions)
- [ ] Compare: buys/day, avg hold time, stop-out rate, win/loss ratio, max drawdown
- [ ] Log inspection of at least 20 representative decisions, including at least 3 stop-outs and 3 winners
- [ ] No regression in false-positive rate (entries that should not have fired)
- [ ] System-map's "Pipeline & Gates" view updated to reflect new gate flow before Phase 3 ships

---

## Cutover strategy — full replacement, git is the rollback

Decision 2026-04-20: full conversion to new logic. No A/B, no env-var toggle, no dual-runtime code paths.

**Rationale:**
- The premise of v1 is wrong, not just its tuning. It's not a candidate for "maybe revert to." Keeping v1 code around alongside v2 invites reverting to a known-broken state under stress.
- Git history IS the rollback mechanism. If v2 ships and is materially worse, `git revert <merge_commit>` + redeploy restores v1 in minutes. Same outcome as a feature flag, without the dead-code maintenance tax.
- Dual code paths encourage "just flip the switch" thinking. Fix-forward discipline is healthier for logic that's fundamentally being rethought.

**Post-cutover discipline (not rollback, but measurement):**
- Track specific outcomes for 2-3 weeks: buys/day, hold time, win rate, avg win $, avg loss $, max drawdown
- Baseline = pre-cutover 2-3 weeks of v1 metrics
- If v2 is clearly worse on multiple metrics after 2 weeks → decide between `git revert` (return to v1 while redesigning) or forward patch (fix what's broken in v2)

The env var pattern is explicitly rejected. v1 code is deleted in the Phase 1 commit that introduces v2.

---

## Master daily archive log

Separate from `bolt_decisions.log` (per-decision file, live during the day). End-of-day process writes `daily_master.log` that fuses ALL agent decisions in chronological order for the day:

```
daily_master_YYYY-MM-DD.log
  06:45 [screener]     XLK published top-10, +0.82 momentum
  07:00 [news]         AAPL earnings_raise flag written, score +0.7
  08:00 [auditor]      morning report ...
  09:32 [trader/CUST1] AAPL candidate: entry_low=184.20 entry_high=185.80
  09:35 [trader/CUST1] AAPL BUY @ 184.55 (entry window matched)
  ...
```

- Written at end-of-day by a cron task (say 16:30 ET, after market close)
- Read-only archival artifact; not touched during live trading hours
- Enables "what happened today, in order" review without grepping 5+ log files
- Kept 90 days, then rotated per existing log rotation policy

---

## Deferred (on the TODO list, not in this patch queue)

- **News companion daemon** — continuous news-watching daemon on its own process. Future optimization for when 30-min news cadence becomes the bottleneck for breaking-news-sensitive strategies. Defer until baseline v2 is stable and we can measure whether latency is actually costing trades.
- **Premium event calendar** — if free APIs (FMP/Finnhub/AlphaVantage) prove unreliable at earnings prediction, upgrade path.

---

## Resolved — decisions locked in 2026-04-20

| # | Question | Decision |
|---|----------|----------|
| 2 | Approval notification location | Stays in enrichment daemon |
| 3 | Trade daemon exit monitoring | Watches prices via window comparison (Change 1b); Alpaca still handles server-side trailing stops |
| 4 | Decision log location | Shared `bolt_decisions.log`; new `daily_master.log` for end-of-day fused archive |
| 5 | Cooldown scope | Per-customer — customer situations diverge quickly |
| 6 | A/B mechanism | No A/B, no env-var toggle. Full replacement; git is the rollback (see Cutover strategy) |
| 7 | Sector momentum scoring origin | Reuses `sector_scores` table; Candidate Generator does not recompute |

## Open questions (still to design)

1. **Trade daemon cycle time** — bounded by customer count × per-customer evaluation time. Measure during Phase 1 build; target ≤30s full cycle. How does this scale if customer count grows past `MAX_TRADE_PARALLEL=3`?
2. **Window Calculator frequency** — recomputed every enrichment tick (30 min), but do volatile-ticker windows need more frequent refresh? Possibly — could trigger window recomputation on large intraday moves, not only on cadence.
3. **Post-cutover measurement thresholds** — what specific metric thresholds on buys/day, win rate, drawdown etc. would trigger a `git revert` decision vs a forward patch? Need to define before cutover so the decision isn't made in a panic.

---

## Related specs

- `AUTO_USER_TAGGING.md` — per-position management (already patch-complete, awaiting merge)
- `HALT_AGENT_REWRITE.md` — halt system v2 (already patch-complete, awaiting merge)
- `BACKUP_ENCRYPT_AND_SPLIT_PLAN.md` — deferred, unrelated
