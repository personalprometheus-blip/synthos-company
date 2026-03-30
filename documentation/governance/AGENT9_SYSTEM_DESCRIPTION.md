# AGENT 9 — MASTER MARKET-STATE AGGREGATOR: SYSTEM DESCRIPTION AND GOVERNANCE RECORD

| Field            | Value                                              |
|------------------|----------------------------------------------------|
| Agent Name       | Master Market-State Aggregator                     |
| File             | agents/market_state_aggregator.py                  |
| Agent Number     | 9                                                  |
| Version          | 1.0                                                |
| Status           | PRODUCTION CANDIDATE                               |
| Date             | 2026-03-30                                         |
| Author           | Synthos Internal                                   |
| Classification   | Governance — Internal Use Only                     |
| Benchmark        | S&P 500 (SPX)                                      |

---

## Purpose and Scope

The Master Market-State Aggregator is the top-level integration layer of the Synthos agent hierarchy. It accepts outputs from up to seven upstream agents — macro regime, sentiment, positioning/flow, news, social/rumor, benchmark technical state, and validator — maps each into a common state vocabulary, scores each component, aggregates into a weighted composite market-state score, detects directional and informational alignment or conflict, classifies the market regime, applies emergency overrides, and emits a final market-state signal with a downstream route for the trade logic agent.

The aggregator does not generate trade signals directly. It determines the market environment bias that the trade logic agent uses to calibrate its decision-making. All logic is rule-based and deterministic. No ML/AI inference is performed anywhere in this agent.

---

## Operational Schedule

The aggregator is invoked on demand whenever a new upstream snapshot batch is available. It does not run on a fixed cron schedule. Callers supply a snapshot dict containing all available upstream agent outputs. The agent returns a complete decision log with the final market-state classification, signal, and downstream route.

---

## Gate Overview

| Gate | Name                              | Type        | Halting |
|------|-----------------------------------|-------------|---------|
| 1    | System Gate                       | Control     | Yes     |
| 2    | Upstream Availability Controls    | Control     | No      |
| 3    | Benchmark Anchor Controls         | Component   | No      |
| 4    | Macro Input Mapping Controls      | Mapping     | No      |
| 5    | Sentiment Input Mapping Controls  | Mapping     | No      |
| 6    | Positioning/Flow Input Mapping    | Mapping     | No      |
| 7    | News Input Mapping Controls       | Mapping     | No      |
| 8    | Social/Rumor Input Mapping        | Mapping     | No      |
| 9    | Validator Input Controls          | Mapping     | No      |
| 10   | Directional Alignment Controls    | Alignment   | No      |
| 11   | Information Alignment Controls    | Alignment   | No      |
| 12   | Benchmark Consistency Controls    | Alignment   | No      |
| 13   | Composite Component Scoring       | Aggregation | No      |
| 14   | Weighting Controls                | Aggregation | No      |
| 15   | Composite Market-State Score      | Aggregation | No      |
| 16   | Confidence Controls               | Aggregation | No      |
| 17   | Divergence Warning Controls       | Aggregation | No      |
| 18   | Market Regime Classification      | Output      | No      |
| 19   | Override Controls                 | Output      | No      |
| 20   | Action Classification Controls    | Output      | No      |
| 21   | Risk Discount Controls            | Output      | No      |
| 22   | Temporal Persistence Controls     | Output      | No      |
| 23   | Evaluation Loop Controls          | Feedback    | No      |
| 24   | Output Controls                   | Output      | No      |
| 25   | Final Composite Market-State Signal | Output    | No      |
| 26   | Downstream Routing Controls       | Output      | No      |

---

## Upstream Agent Mapping

| Input State Key  | Source Agent                         | Status               |
|------------------|--------------------------------------|----------------------|
| macro            | Agent 8 — Macro Regime Agent         | Available            |
| news             | Agent 2 — Research Agent             | Available            |
| rumor            | Agent 4 — Social Rumor Agent         | Available            |
| validator        | Agent 7 — Audit Stack Agent          | Available            |
| sentiment        | Future — Sentiment Agent             | TODO:DATA_DEPENDENCY |
| flow             | Future — Positioning/Flow Agent      | TODO:DATA_DEPENDENCY |
| benchmark        | SPX technical data or future agent   | TODO:DATA_DEPENDENCY |

---

## Gate-by-Gate Description

### Gate 1 — System Gate

Gate 1 is the only halting gate. Conditions are evaluated in order; the first halting condition terminates the run.

**Missing payload.** If the aggregator input payload is null, `state = reject_snapshot` and the agent halts.

**Benchmark unavailable.** If `SPX_feed_status != "online"`, `state = benchmark_context_disabled`. This is non-halting. Gate 3 is skipped and the benchmark component scores as inactive.

**All agents unavailable.** If `available_upstream_agent_count = 0` (no upstream agent outputs present), `state = halt_aggregation` and the agent halts.

**Insufficient agents.** If `available_upstream_agent_count < MIN_REQUIRED_AGENTS` (default 2), `state = insufficient_inputs` and the agent halts.

**Timestamp missing.** If `timestamp = null`, `state = reject_snapshot` and the agent halts.

**Timestamp stale.** If `now - timestamp > MAX_SNAPSHOT_AGE_MINUTES` (default 60), `state = stale_snapshot` and the agent halts.

**Duplicate snapshot.** If the SHA-256 hash of the aggregator input is in `processed_snapshot_store`, `state = suppress_duplicate` and the agent halts.

---

### Gate 2 — Upstream Availability Controls

Gate 2 checks each upstream agent output field for presence. Each slot is set to `active` or `inactive`. Inactive slots contribute 0.0 to the composite score. Records one entry per upstream slot.

| Slot       | Field Checked              |
|------------|---------------------------|
| macro      | macro_agent_output         |
| sentiment  | sentiment_agent_output     |
| flow       | flow_agent_output          |
| news       | news_agent_output          |
| rumor      | rumor_agent_output         |
| benchmark  | benchmark_state_output     |
| validator  | validator_output           |

---

### Gate 3 — Benchmark Anchor Controls

Gate 3 is skipped if `benchmark_context_disabled` was set in Gate 1.

**Trend.** If `MA_short(SPX) > MA_long(SPX)`, `benchmark_state = bullish`. If `MA_short < MA_long`, `benchmark_state = bearish`. If the absolute difference is within `TREND_NEUTRAL_BAND` (default 0.5%), `benchmark_state = neutral`.

**Drawdown.** If `(SPX_current - rolling_peak_SPX) / rolling_peak_SPX <= -SPX_DRAWDOWN_THRESHOLD` (default -10%), `benchmark_risk_state = drawdown`.

**Volatility.** If `realized_vol(SPX) > SPX_VOL_THRESHOLD` (default 20%) OR `VIX > VIX_THRESHOLD` (default 25), `benchmark_vol_state = high`. Otherwise `normal`.

**Momentum.** Rate of change over `ROC_LOOKBACK` periods. If `ROC(SPX) > 0`, `benchmark_momentum_state = positive`. If `ROC(SPX) < 0`, `benchmark_momentum_state = negative`.

---

### Gate 4 — Macro Input Mapping Controls

Gate 4 translates `macro_agent_output.final_macro_state` (from Agent 8) into the aggregator's state vocabulary. Gate is skipped if the macro slot is inactive.

| final_macro_state (Agent 8)               | macro_state (Aggregator) |
|-------------------------------------------|--------------------------|
| strong_expansion, mild_expansion          | pro_growth               |
| strong_contraction, mild_contraction      | defensive_growth         |
| neutral                                   | neutral                  |
| stagflation_override                      | stagflation              |

**Confidence.** If `macro_agent_output.macro_confidence >= MACRO_HIGH_CONF_THRESHOLD` (default 0.70), `macro_confidence_state = high`. If below `MACRO_LOW_CONF_THRESHOLD` (default 0.40), `macro_confidence_state = low`.

**Warning.** If `macro_agent_output.warning_states` is non-empty, `macro_warning_state = active`.

---

### Gate 5 — Sentiment Input Mapping Controls

Gate 5 translates `sentiment_agent_output.final_market_state` into the aggregator vocabulary. Gate is skipped if sentiment slot is inactive. (TODO:DATA_DEPENDENCY — Sentiment Agent not yet built.)

| final_market_state (Sentiment Agent) | sentiment_state |
|--------------------------------------|-----------------|
| strong_bullish                       | bullish         |
| mild_bullish                         | mildly_bullish  |
| neutral                              | neutral         |
| mild_bearish                         | mildly_bearish  |
| strong_bearish                       | bearish         |
| panic_override                       | panic           |
| euphoric_warning_override            | euphoric        |

**Confidence.** If `sentiment_confidence >= SENTIMENT_HIGH_CONF_THRESHOLD` (default 0.70), `sentiment_confidence_state = high`. Below `SENTIMENT_LOW_CONF_THRESHOLD` (default 0.40), `low`.

**Warning.** Non-empty `warning_state` from sentiment agent sets `sentiment_warning_state = active`.

---

### Gate 6 — Positioning/Flow Input Mapping Controls

Gate 6 translates `flow_agent_output.final_flow_state` into the aggregator vocabulary. Gate is skipped if flow slot is inactive. (TODO:DATA_DEPENDENCY — Positioning/Flow Agent not yet built.)

| final_flow_state (Flow Agent) | flow_state        |
|-------------------------------|-------------------|
| strong_supportive             | supportive        |
| mild_supportive               | mildly_supportive |
| neutral                       | neutral           |
| mild_fragile                  | fragile           |
| strong_destabilizing          | destabilizing     |
| squeeze_override              | squeeze_prone     |
| liquidation_override          | liquidation_prone |

**Confidence.** If `flow_confidence >= FLOW_HIGH_CONF_THRESHOLD` (default 0.70), `flow_confidence_state = high`. Below `FLOW_LOW_CONF_THRESHOLD` (default 0.40), `low`.

**Warning.** Non-empty `warning_state` from flow agent sets `flow_warning_state = active`.

---

### Gate 7 — News Input Mapping Controls

Gate 7 translates `news_agent_output.classification` (from Agent 2 — Research Agent) into the aggregator vocabulary. Gate is skipped if news slot is inactive.

| classification (Agent 2)                          | news_state               |
|---------------------------------------------------|--------------------------|
| bullish_signal, relative_alpha_signal             | positive                 |
| bearish_signal                                    | negative                 |
| benchmark_regime_signal                           | macro_benchmark_relevant |
| watch_only, provisional_watch                     | uncertain                |
| ignore                                            | inactive                 |

**Confidence.** Based on `news_agent_output.overall_confidence` against `NEWS_HIGH_CONF_THRESHOLD` (default 0.70) and `NEWS_LOW_CONF_THRESHOLD` (default 0.40).

**Contradiction warning.** If `news_agent_output.classification = freeze` OR `confirmation_state = contradictory`, `news_warning_state = contradiction`.

---

### Gate 8 — Social/Rumor Input Mapping Controls

Gate 8 translates `rumor_agent_output.classification` (from Agent 4 — Social Rumor Agent) into the aggregator vocabulary. Gate is skipped if rumor slot is inactive.

| classification (Agent 4)                          | rumor_state               |
|---------------------------------------------------|---------------------------|
| bullish_rumor_signal, relative_alpha_signal       | positive                  |
| bearish_rumor_signal                              | negative                  |
| manipulation_watch                                | low_trust                 |
| benchmark_regime_signal                           | macro_benchmark_relevant  |
| upgraded_to_confirmed_event                       | confirmed_transition      |
| ignore                                            | inactive                  |

**Confidence.** Based on `rumor_agent_output.overall_confidence` against `RUMOR_HIGH_CONF_THRESHOLD` (default 0.70) and `RUMOR_LOW_CONF_THRESHOLD` (default 0.40).

**Contradiction warning.** If `rumor_agent_output.classification = freeze` OR `confirmation_state = contradictory`, `rumor_warning_state = contradiction`.

---

### Gate 9 — Validator Input Controls

Gate 9 translates `validator_output.master_classification` (from Agent 7 — Audit Stack Agent) into the aggregator's validation state. Gate is skipped if validator slot is inactive.

| master_classification (Agent 7) | validation_state   |
|---------------------------------|--------------------|
| pass                            | clean              |
| review_recommended              | caution            |
| fail                            | failed             |
| block_output                    | blocked            |
| escalate_systemic_issue         | systemic_failure   |
| unresolved (root cause)         | unresolved_risk    |

---

### Gate 10 — Directional Alignment Controls

Gate 10 evaluates the directional consistency of macro, sentiment, and flow inputs. Checked in priority order; first match wins.

| Priority | Condition                                                                                            | alignment_state            |
|----------|------------------------------------------------------------------------------------------------------|----------------------------|
| 1        | macro = pro_growth AND sentiment ∈ {bullish, mildly_bullish} AND flow ∈ {supportive, mildly_supportive} | bullish_alignment        |
| 2        | macro = defensive_growth AND sentiment ∈ {bearish, mildly_bearish, panic} AND flow ∈ {fragile, destabilizing, liquidation_prone} | bearish_alignment |
| 3        | macro = pro_growth AND sentiment ∈ {bearish, mildly_bearish, panic}                                  | macro_sentiment_conflict   |
| 4        | macro = defensive_growth AND sentiment ∈ {bullish, mildly_bullish, euphoric}                         | market_macro_conflict      |
| 5        | sentiment ∈ {bullish, mildly_bullish} AND flow ∈ {destabilizing, liquidation_prone}                  | sentiment_flow_conflict    |
| 6        | sentiment ∈ {bearish, mildly_bearish} AND flow ∈ {supportive, mildly_supportive, squeeze_prone}      | downside_squeeze_conflict  |
| 7        | (default)                                                                                            | mixed                      |

---

### Gate 11 — Information Alignment Controls

Gate 11 compares news and rumor states. Checked in priority order; first match wins.

| Priority | Condition                                               | info_state                  |
|----------|---------------------------------------------------------|-----------------------------|
| 1        | rumor = low_trust                                       | rumor_discounted            |
| 2        | rumor = confirmed_transition                            | rumor_upgraded_to_news_path |
| 3        | news = positive AND rumor ∈ {positive, confirmed_transition} | positive_confirmation  |
| 4        | news = negative AND rumor = negative                    | negative_confirmation       |
| 5        | news = positive AND rumor = negative                    | information_conflict        |
| 6        | news = negative AND rumor = positive                    | information_conflict        |
| 7        | news = inactive AND rumor = inactive                    | low_information_flow        |
| 8        | (default)                                               | mixed_information           |

---

### Gate 12 — Benchmark Consistency Controls

Gate 12 evaluates whether the S&P 500 trend confirms or contradicts the aggregate input direction. Checked in priority order.

| Priority | Condition                                                                                         | benchmark_consistency_state |
|----------|---------------------------------------------------------------------------------------------------|-----------------------------|
| 1        | benchmark = bullish AND alignment = bullish_alignment                                             | confirmed_uptrend           |
| 2        | benchmark = bearish AND alignment = bearish_alignment                                             | confirmed_downtrend         |
| 3        | benchmark = bullish AND alignment ∈ {macro_sentiment_conflict, sentiment_flow_conflict, market_macro_conflict} | fragile_uptrend |
| 4        | benchmark = bearish AND alignment ∈ {macro_sentiment_conflict, downside_squeeze_conflict, market_macro_conflict} | unstable_downtrend |
| 5        | benchmark = neutral AND alignment ∉ {bullish_alignment, bearish_alignment}                        | indecisive_market           |
| 6        | (default)                                                                                         | unresolved_consistency      |

---

### Gate 13 — Composite Component Scoring Controls

Gate 13 converts each active upstream component into a numeric score in [-1.0, +1.0]. Positive = risk-on / bullish. Negative = risk-off / bearish.

#### Scoring Functions

**Macro** (weights: state 0.70, confidence 0.15, warning 0.15):

| State / Sub-state         | Contribution |
|---------------------------|-------------|
| macro_state = pro_growth  | +1.0        |
| macro_state = neutral     |  0.0        |
| macro_state = defensive_growth | -1.0   |
| macro_state = stagflation | -1.0        |
| confidence_state = high   | +0.10       |
| confidence_state = low    | -0.10       |
| warning_state = active    | -0.20       |

**Sentiment** (weights: state 0.70, confidence 0.15, warning 0.15):

| State / Sub-state          | Contribution |
|----------------------------|-------------|
| bullish                    | +1.0        |
| mildly_bullish             | +0.5        |
| neutral                    |  0.0        |
| mildly_bearish             | -0.5        |
| bearish                    | -1.0        |
| panic                      | -1.0        |
| euphoric                   | +0.7        |
| confidence high            | +0.10       |
| confidence low             | -0.10       |
| warning active             | -0.20       |

**Flow** (weights: state 0.70, confidence 0.15, warning 0.15):

| State / Sub-state          | Contribution |
|----------------------------|-------------|
| supportive                 | +1.0        |
| mildly_supportive          | +0.5        |
| neutral                    |  0.0        |
| squeeze_prone              |  0.0        |
| fragile                    | -0.5        |
| destabilizing              | -1.0        |
| liquidation_prone          | -1.0        |
| confidence high            | +0.10       |
| confidence low             | -0.10       |
| warning active             | -0.20       |

**News** (weights: state 0.75, confidence 0.15, contradiction 0.10):

| State / Sub-state              | Contribution |
|--------------------------------|-------------|
| positive                       | +0.8        |
| macro_benchmark_relevant       |  0.0        |
| uncertain                      |  0.0        |
| inactive                       |  0.0        |
| negative                       | -0.8        |
| confidence high                | +0.10       |
| confidence low                 | -0.10       |
| warning = contradiction        | -0.30       |

**Rumor** (weights: state 0.75, confidence 0.15, contradiction 0.10):

| State / Sub-state              | Contribution |
|--------------------------------|-------------|
| confirmed_transition           | +0.8        |
| positive                       | +0.6        |
| macro_benchmark_relevant       |  0.0        |
| low_trust                      |  0.0        |
| inactive                       |  0.0        |
| negative                       | -0.6        |
| confidence high                | +0.10       |
| confidence low                 | -0.10       |
| warning = contradiction        | -0.30       |

**Benchmark** (weights: state 0.45, momentum 0.25, risk 0.20, vol 0.10):

| State / Sub-state              | Contribution |
|--------------------------------|-------------|
| benchmark_state = bullish      | +1.0        |
| benchmark_state = neutral      |  0.0        |
| benchmark_state = bearish      | -1.0        |
| momentum = positive            | +0.3        |
| momentum = negative            | -0.3        |
| risk_state = drawdown          | -0.5        |
| vol_state = high               | -0.2        |
| vol_state = normal             |  0.0        |

**Validation** (single state, weight 1.0):

| validation_state   | Score |
|--------------------|-------|
| clean              | +0.2  |
| caution            |  0.0  |
| unresolved_risk    | -0.2  |
| failed             | -0.3  |
| blocked            | -1.0  |
| systemic_failure   | -1.0  |

All scores clamped to [-1.0, +1.0].

**Base component weights** (sum to 1.0):

| Component  | Base Weight |
|------------|-------------|
| macro      | 0.30        |
| sentiment  | 0.25        |
| flow       | 0.20        |
| benchmark  | 0.10        |
| news       | 0.07        |
| rumor      | 0.04        |
| validation | 0.04        |

---

### Gate 14 — Weighting Controls

Gate 14 applies dynamic upshifts to base weights, then re-normalises the weight vector to sum to 1.0.

| Condition                                                        | Upshift Applied                         |
|------------------------------------------------------------------|-----------------------------------------|
| macro_confidence_state = high                                    | macro weight × 1.25                     |
| sentiment_confidence_state = high                                | sentiment weight × 1.20                 |
| flow_confidence_state = high                                     | flow weight × 1.20                      |
| news_confidence_state = high AND rumor_state = low_trust         | news weight × 1.30                      |
| rumor_state = confirmed_transition                               | rumor weight × 1.25                     |
| benchmark_risk_state = drawdown                                  | benchmark weight × 1.30                 |
| benchmark_vol_state = high                                       | flow weight × 1.20 (additional)         |
| validation_state ∈ {caution, failed, blocked, systemic_failure, unresolved_risk} | validation weight × 1.50 |

If multiple upshifts apply to the same component, they are multiplicative.

---

### Gate 15 — Composite Market-State Score Controls

Gate 15 computes the weighted sum of active component scores:

```
aggregate_market_score = Σ(weight_i × component_score_i)
```

Inactive components contribute 0.0. Score is bounded to [-1.0, +1.0].

**Aggregate state classification:**

| Condition                                                              | aggregate_state  |
|------------------------------------------------------------------------|-----------------|
| aggregate_market_score >= STRONG_BULL_THRESHOLD (0.55)                 | strong_bullish  |
| BULL_THRESHOLD (0.20) <= score < 0.55                                  | mild_bullish    |
| NEUTRAL_LOW (-0.15) <= score < 0.20                                    | neutral         |
| STRONG_BEAR_THRESHOLD (-0.50) < score < -0.15                         | mild_bearish    |
| score <= -0.50                                                         | strong_bearish  |

---

### Gate 16 — Confidence Controls

**Input sufficiency.** If `available_upstream_agent_count >= MIN_CONFIDENT_AGENT_COUNT` (default 3), `confidence_input_state = sufficient`. Otherwise `weak`.

**Agreement.** Standard deviation of active component scores. If `dispersion < AGREEMENT_THRESHOLD` (default 0.30), `confidence_state = high_agreement`. If `dispersion >= DISAGREEMENT_THRESHOLD` (default 0.45), `confidence_state = conflicted`.

**Data quality.** If `mean(upstream_quality_scores) >= DATA_QUALITY_THRESHOLD` (default 0.70), `data_confidence_state = high`.

**Aggregate confidence score:**

```
aggregate_confidence = 0.35 × (active_agent_count / 7)
                     + 0.30 × (1.0 - dispersion)
                     + 0.20 × mean(upstream_quality_scores)
                     + 0.15 × mean(upstream_confidence_scores)
```

Clamped to [0.0, 1.0].

---

### Gate 17 — Divergence Warning Controls

Gate 17 detects cross-component inconsistencies. Multiple warnings may be active.

| Condition                                                                                     | warning_state                       |
|-----------------------------------------------------------------------------------------------|-------------------------------------|
| macro = pro_growth AND benchmark_consistency = fragile_uptrend                                | macro_not_confirmed_by_market       |
| sentiment ∈ {bullish, mildly_bullish} AND validation ∈ {caution, failed, unresolved_risk}    | signal_quality_risk                 |
| flow ∈ {supportive, mildly_supportive} AND macro ∈ {defensive_growth, stagflation}           | positioning_outrunning_macro        |
| info_state = information_conflict                                                             | information_conflict                |
| benchmark = bearish AND sentiment = euphoric                                                  | euphoric_mispricing                 |
| benchmark_risk_state = drawdown AND flow = liquidation_prone                                  | cascade_risk                        |
| sentiment = panic AND flow ∈ {supportive, squeeze_prone}                                     | panic_vs_positioning_conflict       |

---

### Gate 18 — Market Regime Classification Controls

Gate 18 maps aggregate state and alignment into a named market regime. Evaluated in priority order; first match wins.

| Priority | Condition                                                                          | market_regime_state     |
|----------|------------------------------------------------------------------------------------|-------------------------|
| 1        | macro = stagflation AND sentiment ∈ {bearish, mildly_bearish, panic}               | stagflationary_stress   |
| 2        | aggregate ∈ {strong_bullish, mild_bullish} AND alignment = bullish_alignment        | coordinated_risk_on     |
| 3        | aggregate ∈ {strong_bearish, mild_bearish} AND alignment = bearish_alignment        | coordinated_risk_off    |
| 4        | aggregate ∈ {strong_bullish, mild_bullish} AND benchmark_consistency = fragile_uptrend | fragile_risk_on      |
| 5        | aggregate ∈ {strong_bearish, mild_bearish} AND flow = squeeze_prone                 | unstable_risk_off       |
| 6        | aggregate = neutral AND confidence_state = conflicted                               | indecisive_transition   |
| 7        | sentiment = euphoric AND benchmark_consistency = fragile_uptrend                   | unstable_euphoria       |
| 8        | (default)                                                                          | indecisive_transition   |

---

### Gate 19 — Override Controls

Gate 19 may replace the market_regime_state set in Gate 18. Evaluated in priority order; highest priority override wins.

| Priority | Condition                                                                    | market_regime_state override       |
|----------|------------------------------------------------------------------------------|------------------------------------|
| 1        | validation_state = blocked                                                   | blocked_output_override            |
| 2        | validation_state = systemic_failure                                          | systemic_risk_override             |
| 3        | benchmark_risk_state = drawdown AND sentiment_state = panic                  | panic_override                     |
| 4        | flow_state = liquidation_prone AND benchmark_vol_state = high                | forced_deleveraging_override       |
| 5        | rumor_state = low_trust AND news_state = inactive AND info_state ≠ positive_confirmation | low_trust_information_override |

---

### Gate 20 — Action Classification Controls

Gate 20 maps market_regime_state and aggregate_confidence to a classification. Evaluated in priority order.

| Priority | Condition                                                                                         | classification            |
|----------|---------------------------------------------------------------------------------------------------|---------------------------|
| 1        | market_regime ∈ {blocked_output_override, systemic_risk_override}                                | suppress_or_escalate      |
| 2        | market_regime = panic_override                                                                    | panic_alert               |
| 3        | market_regime = forced_deleveraging_override                                                      | deleveraging_alert        |
| 4        | market_regime = coordinated_risk_on AND aggregate_confidence >= HIGH_CONF_THRESHOLD (0.70)        | strong_risk_on_signal     |
| 5        | market_regime = coordinated_risk_off AND aggregate_confidence >= HIGH_CONF_THRESHOLD              | strong_risk_off_signal    |
| 6        | market_regime = fragile_risk_on                                                                   | cautious_risk_on_signal   |
| 7        | market_regime = unstable_risk_off                                                                 | unstable_risk_off_signal  |
| 8        | market_regime = indecisive_transition                                                             | transition_state          |
| 9        | market_regime = stagflationary_stress                                                             | stagflation_stress_signal |
| 10       | market_regime = unstable_euphoria                                                                 | euphoria_warning_signal   |
| 11       | aggregate_confidence < LOW_CONF_THRESHOLD (0.40)                                                  | no_clear_market_state     |
| 12       | (default)                                                                                         | no_clear_market_state     |

---

### Gate 21 — Risk Discount Controls

Gate 21 applies multiplicative discounts to aggregate_market_score, aggregate_confidence, and a tracked `bullish_strength_multiplier` that Gate 25 reads.

| Condition                                                      | Discount Applied                                       |
|----------------------------------------------------------------|--------------------------------------------------------|
| aggregate_confidence < LOW_CONF_THRESHOLD (0.40)               | aggregate_market_score × LOW_CONFIDENCE_DISCOUNT (0.80)|
| active_warning_count > WARNING_COUNT_THRESHOLD (2)             | aggregate_confidence × WARNING_DISCOUNT (0.90)         |
| validation_state = caution                                     | aggregate_confidence × VALIDATION_CAUTION_DISCOUNT (0.90) |
| info_state = information_conflict                              | aggregate_confidence × INFO_CONFLICT_DISCOUNT (0.85)   |
| benchmark_vol_state = high                                     | bullish_strength_multiplier × HIGH_VOL_DISCOUNT (0.85) |
| benchmark_risk_state = drawdown                                | bullish_strength_multiplier × DRAWDOWN_DISCOUNT (0.80) |

Multiple discounts are multiplicative if both conditions apply to the same target variable.

---

### Gate 22 — Temporal Persistence Controls

Gate 22 evaluates regime history from caller-supplied counts in the snapshot.

**Persistence.** If `coordinated_risk_on consecutive windows >= PERSISTENCE_THRESHOLD` (default 3), `persistence_state = persistent_risk_on`. If `coordinated_risk_off` consecutive windows >= threshold, `persistence_state = persistent_risk_off`. If `indecisive_transition` consecutive windows >= threshold, `persistence_state = persistent_transition`.

**Instability.** If `state_change_count > FLIP_THRESHOLD` (default 3), `persistence_state = unstable_regime`.

**Score trend.** If `aggregate_market_score_t > aggregate_market_score_prev + IMPROVEMENT_THRESHOLD` (default 0.05), `aggregate_trend_state = improving`. If below `aggregate_market_score_prev - DETERIORATION_THRESHOLD`, `aggregate_trend_state = worsening`.

---

### Gate 23 — Evaluation Loop Controls

Gate 23 records feedback events for aggregator calibration. All write-back operations are `TODO:DATA_DEPENDENCY`.

**Retention.** If classification is not a reject/suppress state, the aggregate state record is stored. (TODO:DATA_DEPENDENCY)

**Accuracy.** If `aggregate_prediction_accuracy = true` (caller-supplied post-hoc signal), `aggregator_model_score += reward`. If false, `aggregator_model_score -= penalty`. (TODO:DATA_DEPENDENCY)

**False signal correction.** If `risk_on_false_positive_rate > risk_on_fp_threshold`, bullish aggregation thresholds are flagged for recalibration. If `risk_off_false_positive_rate > risk_off_fp_threshold`, bearish thresholds are flagged. (TODO:DATA_DEPENDENCY)

**Warning precision.** If `warning_precision > warning_precision_threshold`, warning weights are flagged to increase. If below threshold, flagged to decrease. (TODO:DATA_DEPENDENCY)

---

### Gate 24 — Output Controls

| classification             | output_action                   |
|----------------------------|---------------------------------|
| strong_risk_on_signal      | emit_strong_risk_on_state       |
| strong_risk_off_signal     | emit_strong_risk_off_state      |
| cautious_risk_on_signal    | emit_cautious_risk_on_state     |
| unstable_risk_off_signal   | emit_unstable_risk_off_state    |
| transition_state           | emit_transition_state           |
| stagflation_stress_signal  | emit_stagflation_stress_state   |
| euphoria_warning_signal    | emit_euphoria_warning           |
| deleveraging_alert         | emit_deleveraging_alert         |
| panic_alert                | emit_panic_alert                |
| suppress_or_escalate       | suppress_release_or_escalate    |
| no_clear_market_state      | emit_uncertain_market_state     |

---

### Gate 25 — Final Composite Market-State Signal

Gate 25 computes `final_market_state_signal` and assigns `final_market_state`.

```
base = aggregate_market_score × aggregate_confidence
warn_penalty = WARN_PENALTY (0.05) × len(warning_states)
persistence_adj:
  persistent_risk_on = +0.05
  persistent_risk_off = -0.05
  persistent_transition = 0
  unstable_regime = -0.08
trend_adj:
  improving = +0.03
  worsening = -0.03
  else = 0
validation_adj:
  blocked or systemic_failure = -0.25
  failed = -0.10
  caution = -0.05
  else = 0

final_market_state_signal = (base × bullish_strength_multiplier if bullish, else base)
                          - warn_penalty + persistence_adj + trend_adj + validation_adj
```

Clamped to [-1.0, +1.0].

**Override classification first:**

| Override Condition                        | final_market_state       |
|-------------------------------------------|--------------------------|
| classification = panic_alert              | panic_override           |
| classification = deleveraging_alert       | deleveraging_override    |
| classification = suppress_or_escalate     | blocked_override         |

**Score-based final state:**

| Signal Range                                              | final_market_state |
|-----------------------------------------------------------|--------------------|
| >= FINAL_STRONG_RISK_ON_THRESHOLD (0.55)                  | strong_risk_on     |
| FINAL_RISK_ON_THRESHOLD (0.15) to 0.55                    | mild_risk_on       |
| FINAL_NEUTRAL_LOW (-0.10) to 0.15                         | neutral            |
| FINAL_STRONG_RISK_OFF_THRESHOLD (-0.45) to -0.10          | mild_risk_off      |
| <= -0.45                                                  | strong_risk_off    |

---

### Gate 26 — Downstream Routing Controls

Gate 26 maps `final_market_state` to a downstream route and bias instruction for the trade logic agent.

| final_market_state       | downstream_route                                           |
|--------------------------|------------------------------------------------------------|
| strong_risk_on           | trade_logic_agent: pro_risk_bias                           |
| mild_risk_on             | trade_logic_agent: cautious_pro_risk_bias                  |
| neutral                  | trade_logic_agent: neutral_bias                            |
| mild_risk_off            | trade_logic_agent: defensive_bias                          |
| strong_risk_off          | trade_logic_agent: high_defensive_bias                     |
| panic_override           | trade_logic_agent: crisis_protocol                         |
| deleveraging_override    | trade_logic_agent: forced_deleveraging_protocol            |
| blocked_override         | validator_stack_agent OR suppress                          |

---

## Audit Trail

Each gate writes at minimum one entry to `AggregatorDecisionLog.records` containing:

| Field       | Description                                     |
|-------------|-------------------------------------------------|
| gate        | Gate number (1–26)                              |
| name        | Gate name or sub-check label                    |
| inputs      | All evaluated values with their names           |
| result      | Computed state or classification                |
| reason_code | Machine-readable label                          |
| ts          | UTC timestamp                                   |

### Escalation

`escalate_if_needed()` posts to `_db.post_suggestion()` for:

| Condition                               | risk_level | Note                                              |
|-----------------------------------------|------------|---------------------------------------------------|
| classification = panic_alert            | HIGH       | Drawdown active with panic sentiment              |
| classification = suppress_or_escalate  | HIGH       | Validator blocked or systemic failure             |
| classification = deleveraging_alert     | HIGH       | Forced deleveraging pattern detected              |
| classification = stagflation_stress_signal | HIGH    | Stagflation regime with bearish sentiment         |
| classification = euphoria_warning_signal | MEDIUM    | Euphoric market with fragile underpinning         |

---

## Controls Not Yet Implemented

| Control                              | Gate  | Status                            |
|--------------------------------------|-------|-----------------------------------|
| Sentiment Agent output               | 2, 5  | TODO:DATA_DEPENDENCY — agent not yet built |
| Positioning/Flow Agent output        | 2, 6  | TODO:DATA_DEPENDENCY — agent not yet built |
| Benchmark technical state agent      | 2, 3  | TODO:DATA_DEPENDENCY — SPX data must be supplied by caller |
| Pre-computed SPX MA, ROC values      | 3     | TODO:DATA_DEPENDENCY — caller must supply |
| Upstream quality scores              | 16    | TODO:DATA_DEPENDENCY — caller must supply |
| Upstream confidence scores           | 16    | TODO:DATA_DEPENDENCY — caller must supply |
| Temporal persistence counters        | 22    | TODO:DATA_DEPENDENCY — caller must supply |
| Aggregator model score tracking      | 23    | TODO:DATA_DEPENDENCY              |
| Warning precision tracking           | 23    | TODO:DATA_DEPENDENCY              |
| Processed snapshot hash store        | 1     | TODO:DATA_DEPENDENCY              |

---

## What This Agent Does Not Do

- It does not generate trade signals. It emits a market-state bias for the trade logic agent to consume.
- It does not call upstream agents directly. It receives their outputs as input fields.
- It does not perform NLP, ML inference, or sentiment computation. All upstream outputs must be pre-computed.
- It does not modify positions or interact with execution infrastructure.
- It does not mutate upstream agent thresholds or configurations.
- It does not suppress downstream output by itself — it sets `output_action = suppress_release_or_escalate` and routes to the validator stack; enforcement is the caller's responsibility.

---

## Human Oversight Points

| Trigger                                        | Action Required                                                  |
|------------------------------------------------|------------------------------------------------------------------|
| classification = panic_alert                   | Immediate review; drawdown + panic sentiment detected            |
| classification = suppress_or_escalate          | Validator has blocked or escalated; no downstream release without human clearance |
| classification = deleveraging_alert            | Forced deleveraging pattern; review all open positions           |
| classification = stagflation_stress_signal     | Stagflation regime with sentiment confirmation; review hedges    |
| classification = euphoria_warning_signal       | Euphoric sentiment with fragile confirmation; consider reducing exposure |
| warning_state = cascade_risk                   | Drawdown + liquidation flow; investigate structural fragility    |
| warning_state = euphoric_mispricing            | Market rising against bearish benchmark; assess divergence cause |
| confidence_state = conflicted                  | Upstream agents strongly disagree; signal reliability is low     |
| validation_state = blocked                     | Audit stack has blocked; no output without validator clearance   |
