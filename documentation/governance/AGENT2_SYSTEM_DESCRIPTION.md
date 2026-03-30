# Synthos — Agent 2 (ResearchAgent) System Description
## Regulatory Reference Document

**Document Version:** 3.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 2 (ResearchAgent) is the signal sourcing and classification layer of the Synthos
system. It runs every hour during market hours and every four hours overnight. Its
function is to locate political and legislative disclosures and news, classify every
article through a 22-gate deterministic decision spine, and forward credible signals
to Agent 1 (ExecutionAgent) for trade evaluation.

**Agent 2 does not use machine learning or AI inference to make classification
decisions.** All decisions are rule-based, deterministic, and fully traceable. Every
article processed produces a structured `NewsDecisionLog` entry recording each gate's
inputs, evaluation result, and reason code. All 22 gate outputs are stored in an
`ArticleState` dataclass, which is the sole input to Gate 22's composite scoring.

**Strict scope:** Agent 2 processes political and legislative signals only. Sentiment
analysis of open positions is delegated to Agent 3 (SentimentAgent).

---

## 2. Operational Schedule

| Session  | Frequency            | Primary Purpose                                 |
|----------|---------------------|-------------------------------------------------|
| Market   | Hourly, 9am–4pm ET  | Full scan: all four source types                |
| Overnight| Every 4 hours       | Capitol Trades + Congress.gov only (reduced API)|

---

## 3. Classification Spine — Overview

Agent 2 operates a 22-gate sequential classification spine. Each gate is a binary
or categorical check. A failure at any gate halts progression and records the reason.
All gate outputs are recorded in `ArticleState` for use by Gate 22 (composite scoring).

```
GATE 1  — System Gate             (parse failure, timestamp, duplicate, body length)
GATE 2  — Benchmark Gate          (SPX regime: trend bullish/bearish/neutral, vol, drawdown, ROC momentum)
GATE 3  — Source & Relevance      (credibility_score float, relevance_score float, opinion flag)
GATE 4  — Topic Classification    (macro/earnings/geopolitical/regulatory/sector/company/market_structure)
GATE 5  — Entity Mapping          (company_linked/multi_company/sector_linked/benchmark_relevant/non_actionable)
GATE 6  — Event Detection         (scheduled/official/breaking/follow_up/rumor/unscheduled/updated_event)
GATE 7  — Sentiment Extraction    (positive/negative/neutral/uncertain/mixed + exaggeration flag)
GATE 8  — Surprise / Novelty      (positive_surprise/negative_surprise/novelty_high/incremental_update/repetitive)
GATE 9  — Scope of Impact         (marketwide/sector_only/single_name/peer_group/unclear + benchmark_corr)
GATE 10 — Time Horizon            (intraday/multi_day/structural + fast_decay/medium_decay/persistent)
GATE 11 — Benchmark-Relative      (aligned_positive/negative, countertrend_positive/negative, alpha/beta)
GATE 12 — Confirmation Controls   (primary_confirmed/strong/weak/contradictory/high_misinformation_risk)
GATE 13 — Timing Controls         (premarket/intraday/postmarket/expired/delayed_distribution/active_flow)
GATE 14 — Crowding / Saturation   (still_open/crowded/exhausted/extreme_attention + crowding_discount)
GATE 15 — Contradiction / Ambiguity (clear/uncertain_language/headline_body_mismatch/internally_conflicted)
GATE 16 — Impact Magnitude        (high/medium/low + benchmark_linked/benchmark_weak + boost logic)
GATE 17 — Action Classification   (bullish/bearish/benchmark_regime/relative_alpha/provisional_watch/freeze/watch/ignore)
GATE 18 — Risk Discounts          (multiplicative: impact_score × discount factors per condition)
GATE 19 — Persistence Controls    (structural/dynamically_updated/medium_or_fast/rapid/slow)
GATE 20 — Evaluation Loop         (relevance recompute, accuracy tracking placeholder)
GATE 21 — Output Controls         (decisive/probabilistic/uncertain; benchmark_first/article_first priority)
GATE 22 — Composite Score         (weighted sum of 7 state dimensions → final_signal + routing override)
```

---

## 4. Data Sources

Agent 2 uses four free, publicly available data sources:

| Source | Type | Tier | Market | Overnight |
|--------|------|------|--------|-----------|
| Capitol Trades API | Congressional trade disclosures | 1 | Yes | Yes |
| Congress.gov API | Bill activity, committee actions | 1 | Yes | Yes |
| Federal Register API | Procurement, regulatory notices | 1 | Yes | No |
| RSS feeds (Reuters, AP, Politico, The Hill, Roll Call, Bloomberg) | Wire / press | 2–3 | Yes | No |

Custom RSS feeds can be injected via the `RSS_FEEDS_JSON` environment variable.

---

## 5. ArticleState — State Machine

All 22 gate outputs are stored in `ArticleState`, a dataclass instantiated per article.
Gate 22 reads directly from `ArticleState` to compute the composite score. This ensures
that no intermediate result is lost and every scoring input is traceable.

Key fields used in Gate 22 composite:

| Field | Source Gate | Role in Composite |
|-------|------------|-------------------|
| `credibility_score` | Gate 3 | w2 — source quality |
| `relevance_score` | Gate 3 | w3 — topic relevance |
| `novelty_score` | Gate 8 | w4 — new information |
| `sentiment_confidence` | Gate 7 | w5 — signal clarity |
| `confirmation_score` | Gate 12 | w6 — independent confirmation |
| `crowding_discount` | Gate 14 | w7 (penalty) — saturation |
| `ambiguity_score` | Gate 15 | w8 (penalty) — contradiction |
| `impact_score` | Gate 18 | w1 — magnitude after discounts |

---

## 6. Gate-by-Gate Description

### Gate 1 — System Gate

**Purpose:** Reject articles before analysis if basic data quality conditions are not met.

**IF→state logic:**
- `IF headline null or len < 5` → `system_status = parse_failure` → HALT
- `IF disc_date > MAX_NEWS_AGE_HOURS (24h) ago` → `system_status = timestamp_rejected` → HALT
- `IF Jaccard(headline, seen_headlines) > DUPLICATE_THRESHOLD (0.60)` → `system_status = duplicate` → HALT
- `IF word_count < MIN_WORD_COUNT (8)` → `system_status = body_too_short` → HALT
- `ELSE` → `system_status = system_ok` → PROCEED

> **DATA NOTE:** Language detection not yet implemented. Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 2 — Benchmark Gate (SPX Context)

**Purpose:** Determine the current S&P 500 market regime. Called once per run.

**IF→state logic:**
- `IF SMA_short(SPX) > SMA_long(SPX) × (1 + TREND_NEUTRAL_BAND)` → `trend_state = bullish`
- `IF SMA_short(SPX) < SMA_long(SPX) × (1 − TREND_NEUTRAL_BAND)` → `trend_state = bearish`
- `IF abs gap ≤ TREND_NEUTRAL_BAND (0.2%)` → `trend_state = neutral`
- `IF ATR/price > SPX_VOL_THRESHOLD (0.018)` → `volatility_state = high_vol`
- `IF (SPX − rolling_peak) / rolling_peak ≤ −SPX_DRAWDOWN_THRESH (0.05)` → `drawdown_state = True`
- `IF ROC(5 bars) > 0.001` → `momentum_state = positive`
- `IF ROC(5 bars) < −0.001` → `momentum_state = negative`
- `ELSE` → `momentum_state = flat`

> **DATA NOTE:** VIX integration not yet available; ATR/price used as proxy.
> Tracked as `TODO: DATA_DEPENDENCY`.

**Benchmark sensitivity:** Used in Gates 11, 14, 16, 18, 22.

---

### Gate 3 — Source & Relevance Filter

**Purpose:** Compute numeric credibility and relevance scores. Reject below-threshold sources.

**IF→state logic (credibility_score):**
- `IF source_tier == 1` → `credibility_score = 1.0` (official government/regulatory)
- `IF source_tier == 2` → `credibility_score = 0.7` (wire service)
- `IF source_tier == 3` → `credibility_score = 0.4` (press/aggregator)
- `IF source_tier >= 4` → `credibility_score = 0.1, opinion_flag = True` → SKIP
- `IF primary_source_signals found in text` → `credibility_score = min(credibility_score + 0.1, 1.0)`
- `IF opinion/analysis keywords in headline` → `opinion_flag = True, credibility_score -= 0.1`
- `IF credibility_score < MIN_CREDIBILITY (0.35)` → SKIP

**IF→state logic (relevance_score):**
- Count topic keyword hits (macro + earnings + geo + reg + sector + ticker)
- `relevance_score = min(total_hits / 3.0, 1.0)`
- `relevance_ok = relevance_score >= MIN_RELEVANCE (0.20)` (soft flag, not a hard stop)

> **DATA NOTE:** Language detection and topic universe filtering tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 4 — Topic Classification

**Purpose:** Identify the article's primary topic category.

**IF→state logic (priority order):**
1. `IF ticker in known SECTOR_TICKER_MAP` → `topic_state = company`
2. `IF sector name in text` → `topic_state = sector`
3. `IF regulatory terms ≥ 1` → `topic_state = regulatory`
4. `IF earnings terms ≥ 1` → `topic_state = earnings`
5. `IF geopolitical terms ≥ 1` → `topic_state = geopolitical`
6. `IF macro terms ≥ 1` → `topic_state = macro`
7. `IF market_structure terms (HFT, circuit breaker, liquidity, etc.)` → `topic_state = market_structure`
8. `ELSE` → `topic_state = uncertain`

---

### Gate 5 — Entity Mapping

**Purpose:** Classify what entities the article is linked to.

**IF→state logic:**
- `IF ticker in known tickers AND is primary ticker` → `entity_state = company_linked`
- `IF multiple known tickers found in text` → `entity_state = multi_company`
- `IF sector keyword found but no specific ticker` → `entity_state = sector_linked`
- `IF topic_state in (macro, geopolitical)` → `entity_state = benchmark_relevant`
- `ELSE` → `entity_state = non_actionable`

---

### Gate 6 — Event Detection

**Purpose:** Classify the nature of the event described.

**IF→state logic:**
- `IF source_tier ≤ 2 AND word_count < 50` → `event_state = breaking`
- `IF Jaccard(headline, recent_headlines) > FOLLOW_UP_SIMILARITY (0.50)` → `event_state = follow_up`
- `IF source_tier == 3 AND uncertainty_density > 0.10` → `event_state = rumor`
- `IF source_tier == 1 AND NOT breaking` → `event_state = official` (scheduled government source)
- `ELSE` → `event_state = unscheduled`

> **DATA NOTE:** Automated event calendar (FOMC, CPI, earnings dates) tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 7 — Sentiment Extraction

**Purpose:** Score directional sentiment via keyword matching. Detect headline exaggeration.

**IF→state logic:**
- `IF sentiment_score > POSITIVE_THRESHOLD (0.10)` → `sentiment_state = positive`
- `IF sentiment_score < NEGATIVE_THRESHOLD (−0.10)` → `sentiment_state = negative`
- `IF both pos_count and neg_count > MIXED_MIN_THRESHOLD (0.05) × total` → `sentiment_state = mixed`
- `IF uncertainty_density > UNCERTAINTY_DENSITY_MAX (0.12) AND direction = neutral` → `sentiment_state = uncertain`
- `ELSE` → `sentiment_state = neutral`
- `IF abs(headline_score − full_text_score) > EXAGGERATION_DELTA (0.15)` → `headline_exaggeration = True`

`sentiment_confidence` = proportion of sentiment-bearing tokens (capped at 1.0).

---

### Gate 8 — Surprise / Novelty

**Purpose:** Detect whether the article provides new information and whether it is a market surprise.

**IF→state logic:**
- `IF novelty_score > SURPRISE_THRESHOLD (0.65) AND sentiment_state = positive` → `novelty_state = positive_surprise`
- `IF novelty_score > SURPRISE_THRESHOLD AND sentiment_state = negative` → `novelty_state = negative_surprise`
- `IF novelty_score > NOVELTY_THRESHOLD (0.40)` → `novelty_state = novelty_high`
- `IF novelty_score < MIN_INCREMENTAL_INFO (0.25)` → `novelty_state = repetitive`
- `ELSE` → `novelty_state = incremental_update`

`novelty_score = 1 − max_similarity` against current run batch and recent DB headlines.

> **DATA NOTE:** `already_priced` detection requires price-article correlation.
> Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 9 — Scope of Impact

**Purpose:** Classify the breadth of the article's market impact and estimate benchmark correlation.

**IF→state logic:**
- `IF topic_state in (macro, geopolitical) OR entity_state = benchmark_relevant` → `scope_state = marketwide, benchmark_corr = HIGH`
- `IF topic_state = sector OR entity_state = sector_linked` → `scope_state = sector_only, benchmark_corr = MEDIUM`
- `IF topic_state = company AND entity_state = company_linked` → `scope_state = single_name, benchmark_corr = LOW`
- `IF entity_state = multi_company` → `scope_state = peer_group, benchmark_corr = MEDIUM`
- `ELSE` → `scope_state = unclear, benchmark_corr = MEDIUM`

---

### Gate 10 — Time Horizon

**Purpose:** Classify expected duration of market impact and decay rate.

**IF→state logic:**
- `IF topic_state in (regulatory, macro)` → `horizon_state = structural, decay_state = persistent`
- `IF topic_state = earnings` → `horizon_state = multi_day, decay_state = medium_decay`
- `IF topic_state = geopolitical` → `horizon_state = multi_day, decay_state = medium_decay`
- `IF event_state = breaking` → `horizon_state = intraday, decay_state = fast_decay`
- `ELSE` → `horizon_state = multi_day, decay_state = medium_decay`

---

### Gate 11 — Benchmark-Relative Interpretation

**Purpose:** Interpret the article's signal value relative to the current SPX regime.

**IF→state logic (benchmark_rel_state):**
- `IF sentiment_state = positive AND trend_state = bullish` → `benchmark_rel_state = aligned_positive`
- `IF sentiment_state = negative AND trend_state = bearish` → `benchmark_rel_state = aligned_negative`
- `IF sentiment_state = positive AND trend_state = bearish` → `benchmark_rel_state = countertrend_positive`
- `IF sentiment_state = negative AND trend_state = bullish` → `benchmark_rel_state = countertrend_negative`
- `ELSE` → `benchmark_rel_state = neutral`

**IF→state logic (signal_type, dominance_state):**
- `IF scope_state = single_name AND benchmark_corr = LOW` → `signal_type = alpha, dominance_state = idiosyncratic_dominant`
- `ELSE` → `signal_type = beta, dominance_state = benchmark_dominant`

---

### Gate 12 — Confirmation Controls

**Purpose:** Assess source confirmation and misinformation risk.

**IF→state logic:**
- `IF source_tier == 1` → `confirmation_state = primary_confirmed, confirmation_score = 1.0`
- `IF source_count ≥ MIN_CONFIRMATIONS (2) AND has_primary_source` → `confirmation_state = strong, confirmation_score = 0.7`
- `IF source_tier == 3 AND no primary source AND uncertainty_density > 0.08` → `confirmation_state = high_misinformation_risk, confirmation_score = 0.0`
- `IF source_count < MIN_CONFIRMATIONS AND article is stale` → `confirmation_state = expired_unconfirmed, confirmation_score = 0.1`
- `IF source_count < MIN_CONFIRMATIONS` → `confirmation_state = weak, confirmation_score = 0.4`

`confirmation_score` is stored in `ArticleState` for Gate 22 composite.

---

### Gate 13 — Timing Controls

**Purpose:** Assess article timing relative to market windows. Reject expired articles.

**IF→state logic:**
- `IF article_age > TRADEABLE_WINDOW_HOURS (8)` → `timing_state = expired, timing_tradeable = False` → HALT
- `IF publication_time < market_open (9:30am ET)` → `timing_state = premarket`
- `IF publication_time > market_close (4:00pm ET)` → `timing_state = postmarket`
- `IF source_tier == 3 (aggregator)` → `timing_state = delayed_distribution`
- `ELSE` → `timing_state = active_flow`

Non-tradeable articles are discarded before Gate 14.

---

### Gate 14 — Crowding / Saturation

**Purpose:** Detect topic saturation and assign crowding discount for Gate 22.

**IF→state logic:**
- `IF cluster_volume ≥ CLUSTER_VOL_THRESHOLD (8) × EXTREME_ATTENTION_MULT (2.0)` → `crowding_state = exhausted, crowding_discount = 0.9`
- `IF cluster_volume ≥ CLUSTER_VOL_THRESHOLD (8)` → `crowding_state = crowded, crowding_discount = 0.5`
- `ELSE` → `crowding_state = still_open, crowding_discount = 0.0`

`cluster_volume` = count of articles in DB (last 4 hours) sharing ≥ 3 keyword tokens.

> **DATA NOTE:** `one_sided` and `extreme_attention` sub-states require multi-article
> sentiment aggregation. Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 15 — Contradiction / Ambiguity

**Purpose:** Detect internally inconsistent or ambiguous articles. Assign ambiguity score.

**IF→state logic:**
- `IF both pos_count ≥ 2 AND neg_count ≥ 2 in same article` → `ambiguity_state = internally_conflicted, ambiguity_score = 1.0`
- `IF headline_sentiment ≠ body_sentiment AND both have signal tokens` → `ambiguity_state = headline_body_mismatch, ambiguity_score = 0.8`
- `IF uncertainty_density > UNCERTAINTY_DENSITY_MAX (0.12)` → `ambiguity_state = uncertain_language, ambiguity_score = 0.5`
- `ELSE` → `ambiguity_state = clear, ambiguity_score = 0.0`

> **DATA NOTE:** `externally_contested` detection requires cross-source claim aggregation.
> Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 16 — Impact Magnitude Estimation

**Purpose:** Estimate the size of the article's potential market impact. Compute base_impact_score.

**IF→state logic:**
- `IF scope_state = marketwide AND topic_state in (macro, geopolitical)` → `impact_magnitude = high, impact_link_state = benchmark_linked, base_impact_score = 1.0`
- `IF scope_state = sector_only OR peer_group` → `impact_magnitude = medium, impact_link_state = benchmark_weak, base_impact_score = 0.5`
- `IF scope_state = single_name AND topic_state = earnings` → `impact_magnitude = medium, impact_link_state = benchmark_weak, base_impact_score = 0.5`
- `IF scope_state = single_name` → `impact_magnitude = low, impact_link_state = benchmark_weak, base_impact_score = 0.2`
- **Boost:** `IF volatility_state = high_vol OR drawdown_state = True` → magnitude upgrades one step (low→medium, medium→high); base_impact_score updated accordingly.

`base_impact_score` is passed to Gate 18 for multiplicative discount application.

---

### Gate 17 — Action Classification

**Purpose:** Combine upstream results into a single action classification.

**IF→state logic (priority order):**

| Condition | action_state |
|-----------|-------------|
| `novelty_state = repetitive` | `ignore` |
| `confirmation_state = high_misinformation_risk` | `ignore` |
| `credibility_score LOW AND novelty_score LOW` | `ignore` |
| `ambiguity_state = internally_conflicted AND confirmation_state in (weak, contradictory)` | `freeze` |
| `event_state = rumor AND confirmation_state = weak` | `provisional_watch` |
| `ambiguity_state in (headline_body_mismatch, uncertain_language)` | `watch_only` |
| `sentiment_state in (neutral, mixed, uncertain) OR sentiment_confidence < threshold` | `watch_only` |
| `scope_state = marketwide AND benchmark_corr = HIGH AND sentiment_confidence > threshold` | `benchmark_regime_signal` |
| `scope_state = single_name AND signal_type = alpha AND novelty_ok AND credibility > 0.5` | `relative_alpha_signal` |
| `sentiment_state = positive AND credibility > 0.5 AND novelty_ok AND ambiguity_state = clear` | `bullish_signal` |
| `sentiment_state = negative AND credibility > 0.5 AND novelty_ok AND ambiguity_state = clear` | `bearish_signal` |
| `all other` | `watch_only` |

**New states vs prior version:**
- `freeze` — article has internal contradictions AND weak confirmation → hold for resolution before acting
- `provisional_watch` — rumor with weak confirmation → watch until confirmed

---

### Gate 18 — Risk Discounts (Multiplicative)

**Purpose:** Apply multiplicative discount factors to `base_impact_score`. Factors stack.

**IF→state logic:**
- `IF sentiment_confidence < SENTIMENT_CONF_MIN (0.25)` → `impact_score *= DISCOUNT_SENTIMENT_CONF (0.70)`
- `IF volatility_state = high_vol` → `impact_score *= DISCOUNT_BENCHMARK_VOL (0.80)`
- `IF event_state = rumor` → `impact_score *= DISCOUNT_NOISY_EVENT (0.60)`
- `IF credibility_score < 0.5` → `impact_score *= DISCOUNT_SOURCE_LOW (0.50)`
- `IF ambiguity_state ≠ clear` → `impact_score *= DISCOUNT_CONTRADICTION (0.50)`

**Impact score → legacy confidence label (backward compat):**
- `impact_score > 0.70` → `final_confidence = HIGH`
- `impact_score > 0.30` → `final_confidence = MEDIUM`
- `ELSE` → `final_confidence = LOW`

> **DATA NOTE:** `noisy_event_class` discount (based on historical event accuracy) requires
> realized move tracking. Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 19 — Persistence Controls

**Purpose:** Classify expected decay rate of the signal's market impact.

**IF→state logic:**
- `IF topic_state in (regulatory, macro)` → `persistence_state = structural`
- `IF topic_state = geopolitical` → `persistence_state = dynamically_updated`
- `IF topic_state = earnings` → `persistence_state = medium_or_fast`
- `IF event_state = breaking` → `persistence_state = rapid`
- `ELSE` → `persistence_state = slow`

---

### Gate 20 — Evaluation Loop

**Purpose:** Recompute relevance and record classification for post-hoc accuracy tracking.

**Relevance recompute:** Query signals DB for active, non-expired signal for same ticker.
If found → ticker is relevant to current portfolio.

> **DATA NOTE:** Predicted vs. realized market response comparison requires post-trade
> outcome data. Event class accuracy tracking flagged for future implementation.
> Tracked as `TODO: DATA_DEPENDENCY`.

---

### Gate 21 — Output Controls

**Purpose:** Determine output mode, explanation priority, and routing (pre-Gate 22).

**IF→state logic (output_mode):**
- `IF impact_score > 0.70` → `output_mode = decisive`
- `IF impact_score > 0.30` → `output_mode = probabilistic`
- `ELSE` → `output_mode = uncertain`

**IF→state logic (output_priority):**
- `IF benchmark_corr = HIGH` → `output_priority = benchmark_first`
- `ELSE` → `output_priority = article_first`

**IF→state logic (output_action):**
- `freeze` → `wait_for_confirmation`
- `ignore` → `no_signal`
- `benchmark_regime_signal` → `benchmark_context_signal`
- `relative_alpha_signal` → `idiosyncratic_alpha_signal`
- `bullish_signal` → `positive_signal`
- `bearish_signal` → `negative_signal`

**Routing (pre-Gate 22):**
- `QUEUE`: bullish_signal, relative_alpha_signal
- `WATCH`: freeze, provisional_watch, watch_only, benchmark_regime_signal, bearish_signal
- `DISCARD`: ignore

Gate 22 may override this routing.

---

### Gate 22 — Final Composite Score

**Purpose:** Compute a weighted combination of all signal quality dimensions. Final routing arbiter.

**Formula:**
```
composite_score = W1 × impact_score
               + W2 × credibility_score
               + W3 × novelty_score
               + W4 × sentiment_confidence
               + W5 × confirmation_score
               + W6 × (1 − crowding_discount)
               + W7 × (1 − ambiguity_score)
```

**Default weights:**
| Weight | Dimension | Default |
|--------|-----------|---------|
| W1 | impact_score (after Gate 18 discounts) | 0.20 |
| W2 | credibility_score (Gate 3) | 0.15 |
| W3 | novelty_score (Gate 8) | 0.15 |
| W4 | sentiment_confidence (Gate 7) | 0.20 |
| W5 | confirmation_score (Gate 12) | 0.15 |
| W6 | 1 − crowding_discount (Gate 14) | 0.10 |
| W7 | 1 − ambiguity_score (Gate 15) | 0.05 |

All weights configurable via environment variables `COMPOSITE_W1` through `COMPOSITE_W7`.

**IF→state logic (final_signal and routing override):**
- `IF action_state = ignore` → `final_signal = no_signal, routing = DISCARD` (composite cannot override ignore)
- `IF composite_score >= COMPOSITE_QUALITY_THRESH (0.45)` AND `action_state = bullish_signal` → `final_signal = bullish_signal, routing = QUEUE`
- `IF composite_score >= COMPOSITE_QUALITY_THRESH` AND `action_state = bearish_signal` → `final_signal = bearish_signal, routing = WATCH`
- `IF composite_score < COMPOSITE_QUALITY_THRESH` AND `routing = QUEUE` → downgrade to `WATCH` or `DISCARD`
- `IF action_state in (freeze, provisional_watch)` → `final_signal = neutral_or_watch, routing = WATCH`

**Benchmark override:**
- `IF drawdown_state = True AND impact_link_state = benchmark_linked` → `final_signal = benchmark_override, routing = WATCH`

---

## 7. Member Weight Application

After Gate 22, the base confidence label is adjusted by the historical accuracy of the
Congress member associated with the disclosure (where applicable).

**Mechanism:** `adjusted_numeric = base_numeric × member_weight`. Weight range: 0.5 (floor)
to 1.5 (ceiling). Minimum 5 tracked trades before weight deviates from 1.0 (neutral).

> **FLAG:** Member weight is currently applied after Gate 22 as a final adjustment.
> Future integration: incorporate directly into Gate 12 (confirmation). Tracked as
> a design improvement item.

---

## 8. News Decision Log

Every article that advances past Gate 3 produces a `NewsDecisionLog` entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Session timestamp, ticker, headline, and source details
- Each of the 22 gates: name, inputs evaluated, result, reason code
- `ArticleState` composite_score and final_signal
- Final decision (QUEUE / WATCH / DISCARD), confidence level, explanation

> **FLAG — LOG WRITE LOCATION:** Currently written to `system_log` table via
> `db.log_event()`. A dedicated `news_decisions` table is recommended to support
> regulatory export and volume management. Tracked as future work item.

---

## 9. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| VIX-based volatility regime | CBOE VIX data feed | TODO: DATA_DEPENDENCY |
| Automated event calendar | FOMC/CPI/earnings calendar API | TODO: DATA_DEPENDENCY |
| Already-priced detection | Post-trade outcome correlation | TODO: DATA_DEPENDENCY |
| Social mention count | External social/news API | TODO: DATA_DEPENDENCY |
| Analyst view dispersion | Aggregated consensus data | TODO: DATA_DEPENDENCY |
| Event class accuracy tracking | Post-trade comparison | TODO: DATA_DEPENDENCY |
| Language detection | NLP library | TODO: DATA_DEPENDENCY |
| Noisy event class discount | Realized move history vs predictions | TODO: DATA_DEPENDENCY |
| Externally contested detection | Cross-source claim aggregation | TODO: DATA_DEPENDENCY |

---

## 10. What Agent 2 Does Not Do

- Agent 2 does not use any AI language model to classify signals.
- Agent 2 does not make trade entry, sizing, or exit decisions.
- Agent 2 does not analyse open position sentiment (Agent 3 scope).
- Agent 2 does not send communications directly. Metadata is posted to the company
  node via fire-and-forget POST. All alerts route through the company node pipeline.

---

## 11. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| All data sources return empty | Pipeline exits cleanly; logged | Investigate API connectivity |
| Benchmark data unavailable | Gate 2 defaults to neutral regime | Review at next session |
| All signals routing to DISCARD | Logged; nothing queued for Agent 1 | Check source quality and thresholds |
| composite_score persistently low | Gate 22 downgrades QUEUE to WATCH | Review weight configuration |
| Member weight at floor (0.5) | Applied to all signals from that member | Review member's trading history |
| freeze action state high volume | Articles held for confirmation | Investigate signal quality at source |
