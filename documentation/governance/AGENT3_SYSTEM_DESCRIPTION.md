# Synthos — Agent 3 (SentimentAgent) System Description
## Regulatory Reference Document

**Document Version:** 2.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 3 (SentimentAgent) is the market sentiment monitoring layer of the Synthos
system. It runs every 30 minutes during market hours. Its function is to:

1. Classify market-wide sentiment through a 27-gate deterministic decision spine
   (Phase 1 — new in v2.0)
2. Watch open positions for signs of market deterioration and cascade patterns
   (Phase 2 — unchanged from v1.0)
3. Check queued signals for sentiment conditions before Agent 1 acts
   (Phase 3 — unchanged from v1.0)

**Agent 3 does not use machine learning or AI inference.** All decisions are
rule-based, deterministic, and fully traceable. Every market snapshot produces a
structured `SentimentDecisionLog` entry recording all data inputs, gate-by-gate
evaluations, and the final sentiment classification.

**Agent 3 does not trade.** It does not issue buy or sell orders. It logs a market
sentiment state that Agent 1 reads when managing positions and evaluating signals.
Agent 1 makes all final trade decisions.

---

## 2. Operational Schedule

| Session | Frequency | Primary Purpose |
|---------|-----------|-----------------|
| Market hours | Every 30 minutes, 9am–4pm ET | 27-gate market sentiment scan + per-position cascade check |

---

## 3. Three-Phase Structure

```
PHASE 1 — Market Sentiment Spine (27 gates, new in v2.0)
  Gates 1–2:   System check + input universe classification
  Gates 3–14:  Component analysis (benchmark, price, breadth, volume, volatility,
               options, safe-haven, credit, sector, macro, news, social)
  Gate 15:     Breadth-price divergence detection
  Gates 16–18: Composite score construction and weighting
  Gate 19:     Confidence scoring
  Gate 20:     Regime classification
  Gate 21:     Divergence warnings
  Gate 22:     Action classification
  Gate 23:     Risk discounts (multiplicative)
  Gate 24:     Temporal persistence (prior session comparison)
  Gate 25:     Evaluation loop
  Gate 26:     Output controls
  Gate 27:     Final signal write (scan_log ticker="MARKET")

PHASE 2 — Per-Position Cascade Scan (unchanged from v1.0)
  STAGE 1 — Data Collection     (put/call ratio, insider transactions, volume)
  STAGE 2 — Cascade Detection   (rule-based multi-signal evaluation)
  STAGE 3 — Scan Analysis       (structured analysis of aligned/misaligned signals)
  STAGE 4 — Scan Log Write      (all results written to scan_log table)

PHASE 3 — Pre-Trade Queue Check (unchanged from v1.0)
  STAGE 1 — Data Collection     (same three signals, 7-day lookback for insider data)
  STAGE 2 — Cascade Detection   (same rules)
  STAGE 3 — Signal Annotation   (tier and summary written back to signal record)
```

---

## 4. Data Sources

### Phase 1 — Market Sentiment Spine

| Source | Data Fetched | Limitation |
|--------|-------------|-----------|
| Alpaca Data API (IEX free tier) | SPY daily bars (220 days), sector ETF bars | IEX feed — limited universe |
| Yahoo Finance /v8/finance/chart | VIX current level and history | Unofficial endpoint — may break |
| CBOE Daily Statistics | Market-wide equity put/call ratio | Market-wide only, not per-ticker |
| Internal DB (Agent 2 output) | News sentiment scores (4-hour lookback) | Requires Agent 2 running |

### Phase 2 & 3 — Per-Position / Queue Checks (unchanged)

| Source | Data Fetched | Limitation |
|--------|-------------|-----------|
| CBOE Daily Statistics | Market-wide equity put/call ratio | Market-wide only — not per-ticker |
| SEC EDGAR (Form 4) | Insider transaction filings (past 30 days) | Filing count used as proxy |
| Finviz (free HTML) | Relative volume, price change % | Scraped HTML — may break on layout change |
| Yahoo Finance RSS | News volume as market attention proxy | Indirect proxy only |

---

## 5. Phase 1 — 27-Gate Market Sentiment Spine

All thresholds are configurable via environment variables. Defaults are listed below.

### Gate 1 — System Check

IF market data feed unavailable → `system_status = halt_sentiment_calc`
IF snapshot hash already processed → `system_status = duplicate_skip`
IF snapshot age > MAX_SNAPSHOT_AGE_MIN (60 min) → `system_status = stale_skip`
IF system operational → `system_status = operational`

### Gate 2 — Input Universe Classification

For each of 10 input channels (price, volume, breadth, volatility, options,
safe_haven, credit, macro, news, social), sets `input_<channel> = active | inactive`.
IF active input count < MIN_REQUIRED_INPUTS (3) → halt; too few signals to continue.

### Gate 3 — Benchmark Classification

Uses SPY daily bars to compute:
- 20-day and 50-day SMA crossover with TREND_NEUTRAL_BAND (0.002) dead zone:
  `benchmark_state = bullish | bearish | neutral`
- 5-bar ROC for directional momentum:
  `benchmark_momentum_state = rising | falling | flat`
- Drawdown from 20-day high vs. DRAWDOWN_THRESHOLD (5%):
  `benchmark_risk_state = drawdown | normal`
- Intraday volatility vs. SPX_VOL_THRESHOLD (1.5%):
  `benchmark_vol_state = elevated | normal`

### Gate 4 — Price Action

Evaluates price sentiment, structure, momentum, and dispersion across sector ETFs:

| State | Description |
|-------|-------------|
| `price_sentiment_state` | bullish / bearish / neutral (% of up vs. down sectors) |
| `price_structure_state` | accelerating / decelerating / neutral (ROC acceleration) |
| `price_momentum_state`  | strong / weak / neutral |
| `price_dispersion_state`| high / low (cross-sector dispersion) |

→ `price_score` ∈ [-1.0, +1.0]

### Gate 5 — Breadth

Uses sector ETF returns as breadth proxy (advance/decline line requires exchange
data — see DATA_DEPENDENCY):

| State | Description |
|-------|-------------|
| `breadth_state`          | broad / narrow / neutral (>60% sectors positive → broad) |
| `breadth_momentum_state` | improving / deteriorating / neutral |
| `breadth_quality_state`  | confirmed / unconfirmed (RSP vs. SPY spread) |

→ `breadth_score` ∈ [-1.0, +1.0]

### Gate 6 — Volume

Analyses SPY bar volume sequence:

| State | Description |
|-------|-------------|
| `volume_sentiment_state`    | buying_pressure / selling_pressure / neutral |
| `volume_pattern_state`      | trend_confirmed / distribution / accumulation / neutral |
| `volume_confirmation_state` | confirmed / unconfirmed / conflicted |

→ `volume_score` ∈ [-1.0, +1.0]

### Gate 7 — Volatility

Combines VIX level, VIX ROC, and realized volatility:

| State | Description |
|-------|-------------|
| `vol_sentiment_state`   | risk_on / risk_off / neutral |
| `vol_structure_state`   | spiking / elevated / calm |
| `realized_vol_state`    | contracting / expanding / neutral |
| `vol_instability_state` | unstable / normal |

→ `vol_score` ∈ [-1.0, +1.0]
Note: VIX at threshold > 20 classified as elevated; ROC > 10% as spiking.

### Gate 8 — Options Sentiment

Uses CBOE put/call ratio (per-ticker data — see DATA_DEPENDENCY):

| State | Description |
|-------|-------------|
| `options_sentiment_state`   | fearful / complacent / neutral (contrarian signals) |
| `options_tail_risk_state`   | elevated / normal |
| `options_speculation_state` | call_speculation / neutral |
| `options_flow_state`        | smart_money_bearish / retail_driven / neutral |
| `options_corr_state`        | elevated_corr / normal |

→ `options_score` ∈ [-1.0, +1.0]
Note: high put/call (fearful) → contrarian bullish score.

### Gate 9 — Safe-Haven / Cross-Asset

Analyses ETF return relationships (GLD, TLT, UUP vs. SPY):

| State | Description |
|-------|-------------|
| `cross_asset_state`   | risk_off_confirmed / risk_on_confirmed / mixed / neutral |
| `rotation_state`      | into_safety / into_risk / neutral |
| `risk_appetite_state` | low / high / neutral |

→ `cross_asset_score` ∈ [-1.0, +1.0]

### Gate 10 — Credit

Uses HYG vs. LQD return spread as credit proxy (CDS/CDX — see DATA_DEPENDENCY):

| State | Description |
|-------|-------------|
| `credit_state`        | tightening / widening / neutral |
| `credit_risk_state`   | high / low / neutral |
| `credit_stress_state` | stressed / normal |
| `liquidity_state`     | strained / normal |

→ `credit_score` ∈ [-1.0, +1.0]

### Gate 11 — Sector Rotation

Evaluates sector ETF return leadership pattern (XLK, XLF, XLE, XLV, etc.):

| State | Description |
|-------|-------------|
| `sector_leadership_state`   | cyclical / defensive / mixed / neutral |
| `sector_rotation_state`     | risk_on / risk_off / neutral |
| `sector_confirmation_state` | confirmed / unconfirmed |

→ `sector_score` ∈ [-1.0, +1.0]

### Gate 12 — Macro

Uses Agent 2 news outputs for macro/policy signals:

| State | Description |
|-------|-------------|
| `macro_state`           | inflationary_fear / growth_fear / neutral |
| `macro_policy_state`    | easing_expected / tightening_expected / neutral |
| `macro_sentiment_state` | hawkish / dovish / neutral |

→ `macro_score` ∈ [-1.0, +1.0]
Note: economic surprise indices require FRED/Bloomberg — see DATA_DEPENDENCY.

### Gate 13 — News Sentiment

Reads Agent 2 output from DB (4-hour lookback on news_feed table):

| State | Description |
|-------|-------------|
| `news_sentiment_state`  | positive / negative / neutral |
| `news_driver_state`     | single_driver / multi_driver / neutral |
| `news_conviction_state` | high / low |

→ `news_score` ∈ [-1.0, +1.0]

### Gate 14 — Social Sentiment

Uses low-trust data from external social feed (DATA_DEPENDENCY — returns 0.0 if
feed unavailable):

| State | Description |
|-------|-------------|
| `social_sentiment_state` | positive / negative / neutral |
| `social_attention_state` | extreme / normal |
| `social_quality_state`   | low_trust / normal |

→ `social_score` = 0.0 if low_trust (quality gate prevents noise injection)

### Gate 15 — Breadth-Price Divergence

Detects structural divergences between price and breadth signals:

| Condition | `divergence_state` |
|-----------|-------------------|
| Price bullish AND breadth narrow | `price_ahead_of_breadth` |
| Price bearish AND breadth broad  | `price_lagging_breadth` |
| Otherwise                         | `none` |

### Gate 16 — Component Score Collection

Collects component scores from Gates 4–14 into `component_scores` dict.
Each score ∈ [-1.0, +1.0] (inactive channels contribute 0.0).

### Gate 17 — Weighting

Default component weights (should sum to ~1.0):

| Component | Weight |
|-----------|--------|
| price | 0.20 |
| breadth | 0.15 |
| volatility | 0.15 |
| volume | 0.10 |
| options | 0.10 |
| cross_asset | 0.10 |
| credit | 0.10 |
| macro | 0.05 |
| news | 0.03 |
| social | 0.02 |

Effective weights are normalised by the sum of active-channel weights so the
composite always spans the full [-1.0, +1.0] range regardless of which inputs
are available.

### Gate 18 — Composite Sentiment Score

```
raw_sentiment_score = Σ (effective_weight[i] × component_score[i])
```

Thresholds:
| Score | `market_sentiment_state` |
|-------|--------------------------|
| ≥ 0.20 | bullish |
| ≤ −0.20 | bearish |
| otherwise | neutral |
| ≥ 0.60 | euphoric |
| ≤ −0.60 | panic |

### Gate 19 — Confidence Scoring

Evaluates signal reliability across three dimensions:

| Dimension | Measure |
|-----------|---------|
| Input quality | active input count vs. MIN_CONFIDENT_COMPONENTS (4) |
| Signal agreement | standard deviation of component scores vs. AGREEMENT_THRESHOLD (0.20) |
| Data quality | fraction of components with reliable data |

→ `sentiment_confidence` ∈ [0.0, 1.0]
→ `confidence_state` = high (≥0.70) / neutral / low (≤0.40)

### Gate 20 — Regime Classification

Cross-references benchmark state, breadth, and volatility to assign:

| `regime_state` | Condition |
|----------------|-----------|
| `trending_bull` | benchmark bullish + breadth broad + vol normal |
| `trending_bear` | benchmark bearish + breadth narrow + vol elevated |
| `choppy_bull`   | benchmark bullish + breadth narrow |
| `choppy_bear`   | benchmark bearish + breadth broad |
| `risk_off`      | benchmark drawdown + vol spiking |
| `indecisive`    | otherwise |

### Gate 21 — Divergence Warnings

Flags structural inconsistencies:

| Warning | Trigger |
|---------|---------|
| `vix_spike_divergence` | VIX spiking but price not yet declining |
| `breadth_collapse` | breadth narrow + price still bullish |
| `extreme_fear` | panic-level score + very high put/call |
| `extreme_greed` | euphoria-level score + very low put/call |

→ `active_warning_count` = number of active flags
→ `warning_state` = specific warning name or `none`

### Gate 22 — Action Classification

| Active Warnings | Composite Score | `classification` |
|-----------------|-----------------|------------------|
| ≥ WARNING_COUNT_THRESHOLD (2) | any | `warning_signal` |
| 0 | bullish | `bullish_signal` |
| 0 | bearish | `bearish_signal` |
| 0 | euphoric | `euphoria_warning` |
| 0 | panic | `panic_signal` |
| 0 | neutral | `no_clear_signal` |

### Gate 23 — Risk Discounts (Multiplicative)

Discount factors are applied multiplicatively to both sentiment score and confidence:

| Condition | Discount |
|-----------|---------|
| Social data unreliable (low_trust) | 0.90 |
| Active input count below confident threshold | 0.80 |
| Active divergence warnings present | 0.85 |
| Benchmark in drawdown state | 0.80 |
| Volatility instability detected | 0.90 |

→ `discounted_sentiment_score` and `discounted_confidence` after all applicable
discounts are applied.

### Gate 24 — Temporal Persistence

Queries `system_log` for prior `MARKET_SENTIMENT_CLASSIFIED` records to determine
whether the current signal is a new reading or continuation:

| Condition | `persistence_state` |
|-----------|---------------------|
| Same direction for ≥ PERSISTENCE_THRESHOLD (3) sessions | `persistent` |
| Direction reversed for ≥ FLIP_THRESHOLD (3) sessions | `reversal` |
| Insufficient history | `unknown` |

→ `sentiment_trend_state` = improving / deteriorating / stable / reversing

### Gate 25 — Evaluation Loop

Determines whether the current snapshot adds information beyond prior sessions:

IF score change < IMPROVEMENT_THRESHOLD (0.05) AND persistence = persistent
  → `snapshot_retained = False` (no DB write for market-wide record)
IF score change > DETERIORATION_THRESHOLD (0.05)
  → `snapshot_retained = True` (always write when conditions are worsening)
OTHERWISE
  → `snapshot_retained = True`

### Gate 26 — Output Controls

Sets output action and priority:

| `output_action` | Condition |
|-----------------|-----------|
| `emit_panic_override` | classification = panic_signal |
| `emit_euphoria_warning` | classification = euphoria_warning |
| `emit_warning_state` | active_warning_count ≥ threshold |
| `emit_sentiment_state` | normal result |
| `emit_neutral_state` | low confidence or no clear signal |

→ `output_priority` = article_first (default) or market_first (warnings/panic)

### Gate 27 — Final Signal Write

Applies final classification thresholds to `discounted_sentiment_score`:

| Score | `final_market_state` |
|-------|---------------------|
| ≥ 0.50 | `strong_bullish` |
| ≥ 0.20 | `mild_bullish` |
| ≤ −0.50 | `strong_bearish` |
| ≤ −0.20 | `mild_bearish` |
| otherwise | `neutral` |

Panic/euphoria overrides:
- `classification = panic_signal` → `final_market_state = panic_override`
- `classification = euphoria_warning` → `final_market_state = euphoric_warning_override`

**Output:** Written to `scan_log` table with `ticker="MARKET"`. Agent 1 reads this
record at Gate 10 (Active Management) and Gate 5/6 (Signal Evaluation / Entry Decision).

---

## 6. Phase 2 — Per-Position Cascade Detection (unchanged from v1.0)

### Stage 1 — Data Collection

For each open position, three independent signals are fetched:
1. **Put/call ratio:** CBOE market-wide equity put/call as proxy.
2. **Insider transactions:** SEC EDGAR Form 4 filings (30-day lookback). Filing
   count used as proxy for buy/sell counts.
3. **Volume profile:** Relative volume and price change direction from Finviz.

### Stage 2 — Cascade Detection

A cascade requires multiple independent sellers acting simultaneously (not a single
large profit-taking sell).

**Signal thresholds:**

| Signal | Critical Threshold | Elevated Threshold |
|--------|-------------------|--------------------|
| Put/call ratio | > 110% of 30d average | > 120% of average |
| Insider selling | ≥ 4 sells with 0 buys | Sells exceed buys by > 2 |
| Volume cascade | > 250% avg volume AND seller dominance > 70% | Volume > 80% above average |

**Tier classification:**

| Critical Signals | Elevated Signals | Tier | Label |
|-----------------|-----------------|------|-------|
| ≥ 2 | Any | 1 | CRITICAL |
| 1 | Any | 2 | ELEVATED |
| 0 | ≥ 2 | 2 | ELEVATED |
| 0 | 1 | 3 | NEUTRAL |
| 0 | 0 | 4 | QUIET |

### Stage 3 — Scan Analysis

Produces a structured `ScanDecisionLog` entry with per-signal status, actor
assessment (cascade vs. isolated), stop level context, and tier recommendation.

### Stage 4 — Scan Log Write

Written to `scan_log` table per ticker. Tier 1 (CRITICAL) flags are consumed by
Agent 1 Gate 10 as pre-authorized protective exit triggers.

---

## 7. Phase 3 — Pre-Trade Queue Check (unchanged from v1.0)

Before Agent 1 acts on a queued signal, Agent 3 checks current sentiment for that
instrument using the same three signals as Phase 2 but with a 7-day insider lookback.

**Output:** Tier and summary written to the signal record via `db.annotate_signal_pulse()`.
Agent 1 reads this annotation as `sentiment_corroboration` input to Gate 5
(Signal Evaluation) and Gate 6 (Entry Decision).

---

## 8. SentimentDecisionLog

Every Phase 1 market snapshot and every Tier 1/2 position scan produces a
`SentimentDecisionLog` (or `ScanDecisionLog`) entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per snapshot:**
- Session timestamp and snapshot hash
- Active input count
- Each gate's name, inputs, result, and reason code
- All component scores and effective weights
- Final sentiment score, confidence, discounts applied, and final_market_state

> **FLAG — LOG WRITE LOCATION:** Market-wide sentiment results are written to the
> `scan_log` table (ticker="MARKET") and gate-level decisions are written via
> `db.log_event()` to the `system_log` table. A dedicated `sentiment_decisions`
> table is recommended for regulatory export. Tracked as future work.

---

## 9. SentimentControls — Configuration

All thresholds are configurable via environment variables. Defaults are production-
validated values set at module level in `SentimentControls`. No restart required
for env var changes if the agent process is restarted.

Key tunable groups: system (gate 1–2), benchmark (gate 3), price/breadth/volume/
volatility/options/safe-haven/credit/sector/macro/news/social (gates 4–14),
composite weights (gate 17), confidence (gate 19), action (gate 22), discounts
(gate 23), persistence (gate 24), final thresholds (gate 27).

---

## 10. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Per-ticker put/call ratio | Paid options data feed | TODO: DATA_DEPENDENCY |
| Full insider transaction parsing | SEC EDGAR Form 4 XML download | TODO: DATA_DEPENDENCY |
| Real-time volume data | Broker intraday bar feed | TODO: DATA_DEPENDENCY |
| Dark pool activity monitoring | Broker dark pool data | TODO: DATA_DEPENDENCY |
| Advance/decline line | Exchange breadth data feed | TODO: DATA_DEPENDENCY |
| VIX real-time integration | Paid/premium VIX feed | TODO: DATA_DEPENDENCY |
| VVIX / vol term structure | Options data feed | TODO: DATA_DEPENDENCY |
| Dealer gamma / skew / implied correlation | Paid options data | TODO: DATA_DEPENDENCY |
| CDS/CDX index / funding spreads | Credit data feed | TODO: DATA_DEPENDENCY |
| Economic surprise indices | FRED/Bloomberg API | TODO: DATA_DEPENDENCY |
| Social sentiment feed | StockTwits / Twitter X API | TODO: DATA_DEPENDENCY |

---

## 11. What Agent 3 Does Not Do

- Agent 3 does not use any AI language model to evaluate sentiment.
- Agent 3 does not place, modify, or cancel any trade orders.
- Agent 3 does not independently trigger position exits. It logs sentiment states
  and cascade warnings. Agent 1 reads those and makes all exit decisions.
- Agent 3 does not communicate directly with Agent 1 at runtime. All findings are
  written to the database and read by Agent 1 at its next session.

---

## 12. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| Gate 1 halt (data unavailable) | Phase 1 skipped cleanly; per-position scans still run | Investigate data source connectivity |
| Tier 1 CRITICAL on open position | Flag written to scan_log; Agent 1 reads at next session | Review protective exit if triggered |
| panic_override final state | Written to scan_log ticker=MARKET at highest priority | Review all open positions |
| All data sources unavailable | Scan exits cleanly; positions not flagged | Investigate data source connectivity |
| CBOE page layout change | put/call returns None; treated as UNKNOWN | Update CBOE parser |
| Finviz blocks scraping | Volume data unavailable; cascade detection degrades | Consider alternative data source |
| VIX Yahoo Finance endpoint breaks | VIX returns None; gate 7 degrades gracefully | Update VIX fetcher |
| Alpaca IEX feed unavailable | SPY bars unavailable; gates 3-6 inactive | Investigate Alpaca connection |
