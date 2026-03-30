# AGENT 8 — MACRO REGIME AGENT: SYSTEM DESCRIPTION AND GOVERNANCE RECORD

| Field            | Value                                              |
|------------------|----------------------------------------------------|
| Agent Name       | Macro Regime Agent                                 |
| File             | agents/macro_regime_agent.py                       |
| Agent Number     | 8                                                  |
| Version          | 1.0                                                |
| Status           | PRODUCTION CANDIDATE                               |
| Date             | 2026-03-30                                         |
| Author           | Synthos Internal                                   |
| Classification   | Governance — Internal Use Only                     |
| Benchmark        | S&P 500 (SPX)                                      |

---

## Purpose and Scope

The Macro Regime Agent classifies the current macroeconomic operating environment into a named regime — expansion, contraction, stagflation, reflation, disinflationary slowdown, or no clear signal — using a deterministic, rule-based 23-gate classification spine. It accepts a macro snapshot containing pre-computed economic series and metrics, activates up to ten input components (inflation, growth, labor, policy, yield curve, credit, liquidity, FX/global, commodity, macro news), scores each active component, aggregates into a weighted composite regime score, computes a confidence level, detects divergence warnings, classifies the regime, and emits a final macro signal.

The agent does not make trade decisions. It produces a regime classification with a confidence score and final signal that downstream agents use as environmental context. The S&P 500 serves as the benchmark reference for all relative trend and volatility assessments.

All logic is rule-based and deterministic. No ML/AI inference is performed anywhere in this agent.

---

## Operational Schedule

The Macro Regime Agent is invoked on demand whenever a new macro snapshot is available. It does not run on a fixed cron schedule. Callers supply a snapshot dict containing all available macro metrics. The agent returns a complete decision log containing the regime classification, component scores, confidence, warnings, and final signal.

---

## Gate Overview

| Gate | Name                              | Type        | Halting |
|------|-----------------------------------|-------------|---------|
| 1    | System Gate                       | Control     | Yes     |
| 2    | Input Universe Controls           | Control     | No      |
| 3    | Benchmark Gate                    | Component   | No      |
| 4    | Inflation Regime Controls         | Component   | No      |
| 5    | Growth Regime Controls            | Component   | No      |
| 6    | Labor Regime Controls             | Component   | No      |
| 7    | Policy Regime Controls            | Component   | No      |
| 8    | Yield Curve Regime Controls       | Component   | No      |
| 9    | Credit Regime Controls            | Component   | No      |
| 10   | Liquidity Regime Controls         | Component   | No      |
| 11   | FX / Global Transmission Controls | Component   | No      |
| 12   | Commodity Regime Controls         | Component   | No      |
| 13   | Macro News Integration Controls   | Component   | No      |
| 14   | Composite Regime Construction     | Aggregation | No      |
| 15   | Weighting Controls                | Aggregation | No      |
| 16   | Composite Macro Regime Score      | Aggregation | No      |
| 17   | Confidence Controls               | Aggregation | No      |
| 18   | Divergence Warning Controls       | Aggregation | No      |
| 19   | Action Classification Controls    | Output      | No      |
| 20   | Temporal Persistence Controls     | Output      | No      |
| 21   | Evaluation Loop Controls          | Feedback    | No      |
| 22   | Output Controls                   | Output      | No      |
| 23   | Final Composite Macro Signal      | Output      | No      |

---

## Gate-by-Gate Description

### Gate 1 — System Gate

Gate 1 is the only halting gate. It validates the snapshot before any regime processing begins. Conditions are evaluated in order; the first halting condition reached terminates the run.

**Macro data unavailable.** If `macro_data_status` is not `"online"`, the agent sets `state = halt_regime_calc` and halts. No subsequent gates run.

**Benchmark data unavailable.** If `SPX_feed_status` is not `"online"`, the agent sets `state = benchmark_context_disabled`. This is a non-halting condition. The benchmark gate (Gate 3) is skipped, but all macro component gates continue.

**Insufficient inputs.** The agent counts the number of usable macro input fields present in the snapshot. If this count is below `MIN_REQUIRED_MACRO_INPUTS` (default 3), the agent sets `state = insufficient_inputs` and halts.

**Timestamp missing.** If `timestamp` is null, the agent sets `state = reject_snapshot` and halts.

**Timestamp stale.** If `now - timestamp > MAX_SNAPSHOT_AGE_MINUTES` (default 60 minutes), the agent sets `state = stale_snapshot` and halts.

**Duplicate snapshot.** The SHA-256 hash of `snapshot_payload` is checked against `processed_snapshot_store`. If already present, the agent sets `state = suppress_duplicate` and halts.

---

### Gate 2 — Input Universe Controls

Gate 2 activates each of the ten macro input components based on availability flags in the snapshot. Each component is set to `active` or `inactive`. Inactive components do not contribute scores or states. This gate records one entry per component.

| Component  | Availability Flag              |
|------------|-------------------------------|
| inflation  | inflation_series_available     |
| growth     | growth_series_available        |
| labor      | labor_series_available         |
| policy     | policy_series_available        |
| curve      | curve_series_available         |
| credit     | credit_series_available        |
| liquidity  | liquidity_series_available     |
| fx         | fx_series_available            |
| commodity  | commodity_series_available     |
| news       | macro_news_available           |

---

### Gate 3 — Benchmark Gate

Gate 3 establishes the S&P 500 context. It is skipped entirely if `benchmark_context_disabled` was set in Gate 1.

**Trend.** If `MA_short(SPX) > MA_long(SPX)`, `benchmark_state = bullish`. If `MA_short(SPX) < MA_long(SPX)`, `benchmark_state = bearish`. Short MA uses a 50-period window; long MA uses a 200-period window. (TODO:DATA_DEPENDENCY — MA values must be pre-computed and supplied in the snapshot.)

**Drawdown.** If `(SPX_current - rolling_peak_SPX) / rolling_peak_SPX <= -SPX_DRAWDOWN_THRESHOLD` (default -10%), `benchmark_risk_state = drawdown`.

**Volatility.** If `realized_vol(SPX) > SPX_VOL_THRESHOLD` (default 20% annualized) OR `VIX > VIX_THRESHOLD` (default 25), `benchmark_vol_state = high`. Otherwise `benchmark_vol_state = normal`.

---

### Gate 4 — Inflation Regime Controls

Gate 4 classifies inflation level, trend, quality, and surprise. All checks are skipped individually if required data is absent.

**Level.** If `headline_CPI_yoy > INFLATION_HIGH_THRESHOLD` (default 4.0%) OR `core_CPI_yoy > INFLATION_HIGH_THRESHOLD`, `inflation_state = high`. If both are below `INFLATION_LOW_THRESHOLD` (default 2.0%), `inflation_state = low`.

**Trend.** If `CPI_yoy_t > CPI_yoy_prev + INFLATION_ACCEL_THRESHOLD` (default 0.5 pp), `inflation_trend_state = accelerating`. If `CPI_yoy_t < CPI_yoy_prev - INFLATION_DECEL_THRESHOLD` (default 0.5 pp), `inflation_trend_state = decelerating`.

**Quality.** If `services_inflation > SERVICES_STICKY_THRESHOLD` (default 5.0%), `inflation_quality_state = sticky_services`. If `goods_inflation_change < -GOODS_DISINFLATION_THRESHOLD` (default -1.0 pp), `inflation_quality_state = goods_disinflation`.

**Surprise.** If `inflation_actual - inflation_consensus > INFLATION_SURPRISE_THRESHOLD` (default 0.2 pp), `inflation_surprise_state = upside`. If below `-INFLATION_SURPRISE_THRESHOLD`, `inflation_surprise_state = downside`.

---

### Gate 5 — Growth Regime Controls

Gate 5 classifies growth level, trend, quality, and recession risk.

**Level.** If `GDP_nowcast > GROWTH_STRONG_THRESHOLD` (default 2.5%) OR `composite_growth_index > GROWTH_STRONG_THRESHOLD`, `growth_state = strong`. If either is below `GROWTH_WEAK_THRESHOLD` (default 1.0%), `growth_state = weak`.

**Trend.** If `growth_nowcast_t > growth_nowcast_prev + GROWTH_ACCEL_THRESHOLD` (default 0.5 pp), `growth_trend_state = accelerating`. If below `growth_nowcast_prev - GROWTH_DECEL_THRESHOLD`, `growth_trend_state = decelerating`.

**Quality.** If `PMI_manufacturing < MANUFACTURING_CONTRACTION_THRESHOLD` (default 50.0), `growth_quality_state = manufacturing_weakness`. If `PMI_services > SERVICES_EXPANSION_THRESHOLD` (default 50.0), `growth_quality_state = services_strength`. Services-strength takes priority if both conditions are met.

**Recession risk.** If `recession_probability > RECESSION_PROB_THRESHOLD` (default 0.30), `growth_risk_state = recession_risk`.

---

### Gate 6 — Labor Regime Controls

Gate 6 classifies labor market tightness, momentum, wage pressure, and layoff risk.

**Tightness.** If `unemployment_rate < UNEMPLOYMENT_LOW_THRESHOLD` (default 4.0%) AND `job_openings_ratio > LABOR_TIGHT_THRESHOLD` (default 1.2), `labor_state = tight`. If `unemployment_rate_t > unemployment_rate_prev + LABOR_LOOSEN_THRESHOLD` (default 0.2 pp), `labor_state = loosening`.

**Momentum.** If `nonfarm_payrolls_3m_avg > PAYROLL_STRONG_THRESHOLD` (default 200,000), `labor_momentum_state = strong`. If below `PAYROLL_WEAK_THRESHOLD` (default 75,000), `labor_momentum_state = weak`.

**Wages.** If `average_hourly_earnings_yoy > WAGE_GROWTH_HIGH_THRESHOLD` (default 4.5%), `wage_state = inflationary`. If below `WAGE_GROWTH_MODERATION_THRESHOLD` (default 3.5%), `wage_state = easing`.

**Layoffs.** If `initial_claims_4w_avg > CLAIMS_RISING_THRESHOLD` (default 250,000), `labor_risk_state = weakening`.

---

### Gate 7 — Policy Regime Controls

Gate 7 classifies the policy stance, direction, balance sheet direction, and any surprise.

**Stance.** If `real_policy_rate > RESTRICTIVE_REAL_RATE_THRESHOLD` (default 1.5%), `policy_state = restrictive`. If `real_policy_rate < ACCOMMODATIVE_REAL_RATE_THRESHOLD` (default 0.0%), `policy_state = accommodative`.

**Cycle.** If `policy_rate_change_6m > HIKING_CYCLE_THRESHOLD` (default 25 bps), `policy_trend_state = tightening`. If `policy_rate_change_6m < -EASING_CYCLE_THRESHOLD` (default -25 bps), `policy_trend_state = easing`.

**Balance sheet.** If `central_bank_balance_sheet_change < -QT_THRESHOLD` (default -$50B), `liquidity_policy_state = tightening`. If greater than `QE_THRESHOLD` (default +$50B), `liquidity_policy_state = easing`.

**Surprise.** If `policy_market_implied_shift > HAWKISH_SURPRISE_THRESHOLD` (default 10 bps), `policy_surprise_state = hawkish`. If below `-DOVISH_SURPRISE_THRESHOLD`, `policy_surprise_state = dovish`.

---

### Gate 8 — Yield Curve Regime Controls

Gate 8 classifies the yield curve level, direction, regime type, and rate stress.

**Inversion.** If `yield_10y - yield_2y < CURVE_INVERSION_THRESHOLD` (default 0.0%), `curve_state = inverted`.

**Steepening.** If the current 10y-2y spread is greater than the prior spread by more than `STEEPENING_THRESHOLD` (default 10 bps), `curve_trend_state = steepening`.

**Regime type.** If `yield_10y_change - yield_2y_change > BEAR_STEEPENING_THRESHOLD` (default 10 bps) and the 10y is rising, `curve_regime_state = bear_steepener`. If `yield_2y_change - yield_10y_change < -BULL_STEEPENING_THRESHOLD` (default 10 bps) and the 2y is falling, `curve_regime_state = bull_steepener`. Bear steepening is checked before bull steepening.

**Front-end stress.** If `yield_2y_change > FRONTEND_STRESS_THRESHOLD` (default 50 bps), `rate_stress_state = frontend_stress`.

**Term premium.** If `term_premium_change > TERM_PREMIUM_THRESHOLD` (default 10 bps), `long_end_state = inflation_premium_rise`.

---

### Gate 9 — Credit Regime Controls

Gate 9 classifies credit spread direction, high-yield stress, default risk, and funding stress.

**IG spread direction.** If `IG_spread_change > IG_SPREAD_WIDEN_THRESHOLD` (default 10 bps), `credit_state = deteriorating`.

**HY stress.** If `HY_spread_change > HY_SPREAD_WIDEN_THRESHOLD` (default 50 bps), `credit_risk_state = high_yield_stress`.

**Tightening.** If `HY_spread_change < -SPREAD_TIGHTENING_THRESHOLD` (default -10 bps) AND `IG_spread_change < -SPREAD_TIGHTENING_THRESHOLD`, `credit_state = improving`.

**Default risk.** If `default_rate_nowcast > DEFAULT_RISK_THRESHOLD` (default 4.0%), `credit_default_state = elevated`.

**Funding stress.** If `funding_spread > FUNDING_STRESS_THRESHOLD` (default 50 bps), `funding_liquidity_state = funding_stress`.

---

### Gate 10 — Liquidity Regime Controls

Gate 10 classifies system-level liquidity direction, quality, and supply pressure.

**Direction.** If `net_liquidity_change > LIQUIDITY_IMPROVING_THRESHOLD` (default +$50B), `system_liquidity_state = improving`. If `net_liquidity_change < -LIQUIDITY_DETERIORATING_THRESHOLD` (default -$50B), `system_liquidity_state = deteriorating`.

**Reserve quality.** If `bank_reserves_metric < RESERVE_TIGHT_THRESHOLD` (default $3.0T), `liquidity_quality_state = tight_reserves`. If `short_term_funding_metric > MONEY_MARKET_STRESS_THRESHOLD` (default 20 bps), `liquidity_quality_state = stressed`.

**Supply pressure.** If `net_treasury_supply_change > ISSUANCE_PRESSURE_THRESHOLD` (default $100B), `liquidity_supply_state = supply_pressure`.

---

### Gate 11 — FX / Global Transmission Controls

Gate 11 classifies the dollar direction and its cross-asset implications, EM stress, and global growth stabilization.

**Dollar direction.** If `DXY_return > USD_STRENGTH_THRESHOLD` (default 2.0%), `fx_state = strong_dollar`. If `DXY_return < -USD_WEAKNESS_THRESHOLD`, `fx_state = weak_dollar`.

**Financial conditions.** If `fx_state = strong_dollar` AND `growth_state = weak` (from Gate 5), `global_pressure_state = tightening_financial_conditions`.

**EM stress.** If `EM_fx_stress_index > EM_STRESS_THRESHOLD` (default 0.70), `global_risk_state = EM_stress`.

**Global growth.** If `global_PMI_change > GLOBAL_STABILIZATION_THRESHOLD` (default 0.5 pp), `global_growth_state = stabilizing`.

---

### Gate 12 — Commodity Regime Controls

Gate 12 classifies the commodity environment. Four states are checked in priority order (energy inflation, food inflation, growth confirmation, deflationary signal).

**Energy inflation risk.** If `WTI_return > OIL_SPIKE_THRESHOLD` (default 10%), `commodity_state = energy_inflation_risk`.

**Food inflation risk.** If `food_commodity_index_change > FOOD_PRESSURE_THRESHOLD` (default 5%) and energy inflation is not already flagged, `commodity_state = food_inflation_risk`.

**Growth confirmation.** If `industrial_metals_return > METALS_GROWTH_THRESHOLD` (default 5%) AND `growth_state IN {strong, accelerating}` (from Gate 5), `commodity_state = growth_confirmation`.

**Deflationary signal.** If `broad_commodity_index_return < COMMODITY_WEAKNESS_THRESHOLD` (default -5%) and no other state is set, `commodity_state = deflationary_signal`.

---

### Gate 13 — Macro News Integration Controls

Gate 13 classifies macro news sentiment, central bank communication tone, and news conviction. Gate 13 is skipped if the news input is inactive.

**Sentiment.** If `macro_news_sentiment_score > MACRO_NEWS_POSITIVE_THRESHOLD` (default 0.30), `news_state = supportive`. If below `-MACRO_NEWS_NEGATIVE_THRESHOLD` (default -0.30), `news_state = unsupportive`.

**CB communication.** If `hawkish_term_density > HAWKISH_COMM_THRESHOLD` (default 0.15), `policy_comm_state = hawkish`. If `dovish_term_density > DOVISH_COMM_THRESHOLD` (default 0.15), `policy_comm_state = dovish`. Hawkish is checked first.

**Conviction.** If `negative_macro_news_confirmations >= MIN_CONFIRMATIONS` (default 2), `news_conviction_state = strong_negative`. If `positive_macro_news_confirmations >= MIN_CONFIRMATIONS`, `news_conviction_state = strong_positive`.

---

### Gate 14 — Composite Regime Construction Controls

Gate 14 converts each active component's state variables into a single numeric score in the range [-1.0, +1.0]. Positive values indicate expansionary/supportive conditions. Negative values indicate contractionary/restrictive conditions. Each component score is a weighted sum of its sub-state score contributions.

#### Sub-state Score Maps

**Inflation** (weights: level 0.40, trend 0.30, quality 0.20, surprise 0.10):

| Sub-state                | Value  |
|--------------------------|--------|
| inflation_state = high   | -1.0   |
| inflation_state = low    | +1.0   |
| accelerating             | -1.0   |
| decelerating             | +1.0   |
| sticky_services          | -0.5   |
| goods_disinflation       | +0.5   |
| surprise upside          | -0.5   |
| surprise downside        | +0.5   |

**Growth** (weights: level 0.35, trend 0.30, quality 0.20, risk 0.15):

| Sub-state                | Value  |
|--------------------------|--------|
| growth_state = strong    | +1.0   |
| growth_state = weak      | -1.0   |
| accelerating             | +1.0   |
| decelerating             | -1.0   |
| services_strength        | +0.5   |
| manufacturing_weakness   | -0.5   |
| recession_risk           | -1.0   |

**Labor** (weights: tightness 0.25, momentum 0.35, wages 0.25, risk 0.15):

| Sub-state                | Value  |
|--------------------------|--------|
| labor_state = tight      | +0.5   |
| labor_state = loosening  | -0.5   |
| momentum = strong        | +1.0   |
| momentum = weak          | -1.0   |
| wage_state = inflationary| -0.5   |
| wage_state = easing      | +0.5   |
| labor_risk = weakening   | -1.0   |

**Policy** (weights: stance 0.35, cycle 0.30, balance sheet 0.25, surprise 0.10):

| Sub-state                        | Value  |
|----------------------------------|--------|
| policy_state = accommodative     | +1.0   |
| policy_state = restrictive       | -1.0   |
| policy_trend = easing            | +1.0   |
| policy_trend = tightening        | -1.0   |
| liquidity_policy = easing        | +1.0   |
| liquidity_policy = tightening    | -1.0   |
| policy_surprise = dovish         | +0.5   |
| policy_surprise = hawkish        | -0.5   |

**Curve** (weights: level 0.30, trend 0.25, regime 0.30, stress 0.15):

| Sub-state                  | Value  |
|----------------------------|--------|
| curve_state = inverted     | -1.0   |
| curve_trend = steepening   | +0.5   |
| bull_steepener             | +1.0   |
| bear_steepener             | -0.5   |
| rate_stress = frontend     | -1.0   |

**Credit** (weights: direction 0.35, HY risk 0.25, default 0.20, funding 0.20):

| Sub-state                   | Value  |
|-----------------------------|--------|
| credit_state = improving    | +1.0   |
| credit_state = deteriorating| -1.0   |
| high_yield_stress           | -1.0   |
| credit_default = elevated   | -1.0   |
| funding_liquidity = stress  | -1.0   |

**Liquidity** (weights: direction 0.50, quality 0.30, supply 0.20):

| Sub-state                        | Value  |
|----------------------------------|--------|
| system_liquidity = improving     | +1.0   |
| system_liquidity = deteriorating | -1.0   |
| liquidity_quality = tight_reserves| -0.5  |
| liquidity_quality = stressed     | -1.0   |
| liquidity_supply = supply_pressure| -0.5  |

**FX/Global** (weights: dollar 0.25, conditions 0.35, EM 0.20, global growth 0.20):

| Sub-state                                  | Value  |
|--------------------------------------------|--------|
| fx_state = weak_dollar                     | +0.5   |
| fx_state = strong_dollar                   | -0.5   |
| global_pressure = tightening_fin_conditions| -1.0   |
| global_risk = EM_stress                    | -0.5   |
| global_growth = stabilizing                | +0.5   |

**Commodity** (single state, weight 1.0):

| Sub-state               | Value  |
|-------------------------|--------|
| growth_confirmation     | +1.0   |
| deflationary_signal     | -0.5   |
| energy_inflation_risk   | -0.5   |
| food_inflation_risk     | -0.3   |

**News** (weights: sentiment 0.35, policy comm 0.30, conviction 0.35):

| Sub-state                  | Value  |
|----------------------------|--------|
| news_state = supportive    | +1.0   |
| news_state = unsupportive  | -1.0   |
| policy_comm = dovish       | +0.5   |
| policy_comm = hawkish      | -0.5   |
| conviction = strong_positive| +1.0  |
| conviction = strong_negative| -1.0  |

Inactive components contribute a score of 0.0. All component scores are clamped to [-1.0, +1.0].

---

### Gate 15 — Weighting Controls

Gate 15 starts from the base component weight vector and applies dynamic upshifts when specific stress or transition conditions are active. The weight vector is re-normalised to sum to 1.0 after all adjustments.

| Condition                                             | Upshift Applied                       |
|-------------------------------------------------------|---------------------------------------|
| inflation_trend_state = accelerating OR surprise = upside | inflation weight × 1.30           |
| credit_risk_state = high_yield_stress OR funding_stress   | credit weight × 1.40              |
| policy_trend_state IN {tightening, easing} AND policy_surprise != null | policy weight × 1.25 |
| system_liquidity_state = deteriorating                | liquidity weight × 1.30               |
| benchmark_risk_state = drawdown                       | credit weight × 1.20 (additional)     |

If both the high_yield_stress upshift and the drawdown upshift apply to credit, they are multiplicative.

**Base component weights** (sum to 1.0):

| Component  | Base Weight |
|------------|-------------|
| growth     | 0.25        |
| policy     | 0.20        |
| inflation  | 0.15        |
| curve      | 0.10        |
| credit     | 0.10        |
| labor      | 0.08        |
| liquidity  | 0.05        |
| fx_global  | 0.04        |
| commodity  | 0.02        |
| news       | 0.01        |

---

### Gate 16 — Composite Macro Regime Score Controls

Gate 16 computes the weighted sum of active component scores:

```
macro_regime_score = Σ(weight_i × component_score_i)
```

Inactive components contribute 0.0 to the sum regardless of their weight. The score is bounded to [-1.0, +1.0].

**Score-based regime classification** (evaluated in order; overlays checked first):

| Priority | Condition                                                          | macro_regime_state       |
|----------|--------------------------------------------------------------------|--------------------------|
| 1        | growth_state = weak AND inflation_state = high                     | stagflation              |
| 2        | (growth_state = strong OR growth_trend_state = accelerating) AND inflation_trend_state = accelerating | reflation |
| 3        | growth_trend_state = decelerating AND inflation_trend_state = decelerating | disinflationary_slowdown |
| 4        | macro_regime_score >= REGIME_EXPANSION_THRESHOLD (0.30)            | expansion                |
| 5        | macro_regime_score <= REGIME_CONTRACTION_THRESHOLD (-0.30)         | contraction              |
| 6        | (default — between thresholds)                                     | slowdown                 |

Overlays (stagflation, reflation, disinflationary_slowdown) take priority over score-based labels regardless of the numeric score.

---

### Gate 17 — Confidence Controls

Gate 17 computes a confidence score for the regime assessment.

**Sufficient inputs.** If `active_component_count >= MIN_CONFIDENT_COMPONENT_COUNT` (default 5), `confidence_input_state = sufficient`.

**Agreement.** Dispersion is computed as the standard deviation of all active component scores. If `dispersion < AGREEMENT_THRESHOLD` (default 0.30), `confidence_state = high_agreement`. If `dispersion >= DISAGREEMENT_THRESHOLD` (default 0.40), `confidence_state = conflicted`.

**Data quality.** If `mean(input_quality_scores) >= DATA_QUALITY_THRESHOLD` (default 0.70), `data_confidence_state = high`. Otherwise `data_confidence_state = low`.

**Confidence score.** The composite macro confidence is computed as:

```
macro_confidence = 0.40 × (active_component_count / 10)
                 + 0.35 × (1.0 - dispersion)
                 + 0.25 × mean(input_quality_scores)
```

The result is clamped to [0.0, 1.0].

---

### Gate 18 — Divergence Warning Controls

Gate 18 detects compound cross-component inconsistencies that are not captured by the composite score. Multiple warnings may be active simultaneously.

| Condition                                                                | warning_state                      |
|--------------------------------------------------------------------------|------------------------------------|
| growth_state = strong AND credit_state = deteriorating                   | growth_credit_divergence           |
| inflation_trend_state = decelerating AND policy_trend_state = tightening | policy_lag_risk                    |
| benchmark_state = bullish AND macro_regime_state = contraction           | market_macro_divergence            |
| curve_trend_state = steepening AND growth_state = weak                   | recession_transition_risk          |
| fx_state = strong_dollar AND credit_state = deteriorating                | financial_conditions_tightening    |

All matching warnings are accumulated. If no conditions match, `warning_states` is empty and no flag is set.

---

### Gate 19 — Action Classification Controls

Gate 19 maps the regime state and confidence to a final classification. Rules are evaluated in priority order; the first match wins.

| Priority | Condition                                                                       | classification              |
|----------|---------------------------------------------------------------------------------|-----------------------------|
| 1        | macro_regime_state = stagflation                                                | stagflation_warning         |
| 2        | macro_regime_state = expansion AND macro_confidence >= HIGH_CONF_THRESHOLD (0.70) | pro_growth_regime         |
| 3        | macro_regime_state = contraction AND macro_confidence >= HIGH_CONF_THRESHOLD    | defensive_regime            |
| 4        | macro_regime_state = reflation                                                  | reflation_signal            |
| 5        | macro_regime_state = disinflationary_slowdown                                   | disinflation_slowdown_signal|
| 6        | warning_states not empty AND macro_confidence >= DIVERGENCE_CONF_THRESHOLD (0.50) | divergence_watch          |
| 7        | macro_confidence < LOW_CONF_THRESHOLD (0.40)                                    | no_clear_macro_signal       |
| 8        | (default)                                                                       | no_clear_macro_signal       |

---

### Gate 20 — Temporal Persistence Controls

Gate 20 evaluates the regime against its history. It requires the caller to supply historical counts in the snapshot (`consecutive_expansion_count`, `consecutive_contraction_count`, `recent_state_change_count`, `macro_regime_score_prev`).

**Persistence.** If `expansion_state_count >= PERSISTENCE_THRESHOLD` (default 3 consecutive windows), `persistence_state = persistent_expansion`. If `contraction_state_count >= PERSISTENCE_THRESHOLD`, `persistence_state = persistent_contraction`.

**Instability.** If `state_change_count(window) > FLIP_THRESHOLD` (default 3), `persistence_state = unstable_regime`.

**Trend.** If `macro_regime_score_t > macro_regime_score_prev + IMPROVEMENT_THRESHOLD` (default 0.05), `macro_trend_state = improving`. If `macro_regime_score_t < macro_regime_score_prev - DETERIORATION_THRESHOLD`, `macro_trend_state = worsening`.

---

### Gate 21 — Evaluation Loop Controls

Gate 21 records feedback events for model calibration. All threshold write-back operations are marked `TODO:DATA_DEPENDENCY` pending integration with the platform calibration layer.

**Snapshot retention.** If the classification is not `reject_snapshot` or `stale_snapshot`, the macro state record is stored. (TODO:DATA_DEPENDENCY — persistent macro state store not yet integrated.)

**Accuracy tracking.** If `regime_prediction_accuracy = true` (caller-supplied post-hoc signal), `macro_model_score += reward`. If false, `macro_model_score -= penalty`. (TODO:DATA_DEPENDENCY — model score tracking and reward/penalty values require calibration store.)

**False positive correction.** If `contraction_false_positive_rate > CONTRACTION_FP_THRESHOLD`, contraction thresholds are flagged for human review. If `expansion_false_positive_rate > EXPANSION_FP_THRESHOLD`, expansion thresholds are flagged. (TODO:DATA_DEPENDENCY — FP rate computation requires historical outcome tracking.)

---

### Gate 22 — Output Controls

Gate 22 maps the action classification to an output action.

| classification              | output_action                     |
|-----------------------------|-----------------------------------|
| pro_growth_regime           | emit_growth_regime                |
| defensive_regime            | emit_defensive_regime             |
| stagflation_warning         | emit_stagflation_alert            |
| reflation_signal            | emit_reflation_signal             |
| disinflation_slowdown_signal| emit_disinflation_slowdown_signal |
| divergence_watch            | emit_macro_divergence_warning     |
| no_clear_macro_signal       | emit_uncertain_macro_state        |

---

### Gate 23 — Final Composite Macro Signal

Gate 23 computes a final numeric macro signal and assigns a named final state. The signal function is:

```
final_macro_signal = (macro_regime_score × macro_confidence)
                   - (WARN_PENALTY × len(warning_states))
                   + persistence_adjustment
```

Where:
- `WARN_PENALTY = 0.05` per active warning
- `persistence_adjustment`: `persistent_expansion = +0.05`, `persistent_contraction = -0.05`, `unstable_regime = -0.10`, `macro_trend_state = improving = +0.03`, `macro_trend_state = worsening = -0.03`, else `0.0`

The result is clamped to [-1.0, +1.0].

**Final state assignment** (overrides applied first):

| Priority | Condition                                                             | final_macro_state    |
|----------|-----------------------------------------------------------------------|----------------------|
| 1        | classification = stagflation_warning                                  | stagflation_override |
| 2        | final_macro_signal >= FINAL_STRONG_EXPANSION_THRESHOLD (0.60)         | strong_expansion     |
| 3        | FINAL_EXPANSION_THRESHOLD (0.20) <= signal < 0.60                    | mild_expansion       |
| 4        | FINAL_NEUTRAL_LOW (-0.15) <= signal < 0.20                           | neutral              |
| 5        | FINAL_STRONG_CONTRACTION_THRESHOLD (-0.50) < signal < -0.15          | mild_contraction     |
| 6        | signal <= -0.50                                                       | strong_contraction   |

---

## Audit Trail

Each gate writes at minimum one entry to `RegimeDecisionLog.records` containing:

| Field       | Description                                     |
|-------------|-------------------------------------------------|
| gate        | Gate number (1–23)                              |
| name        | Gate name or sub-check label                    |
| inputs      | All evaluated values with their names           |
| result      | Computed state or classification                |
| reason_code | Machine-readable label explaining the decision  |
| ts          | UTC timestamp of the record                     |

The complete log is serialised to JSON by `to_dict()` and written to the database event log via `write_regime_log()`.

### Escalation

`escalate_if_needed()` posts to `_db.post_suggestion()` under the following conditions:

| Condition                               | risk_level | Note                                              |
|-----------------------------------------|------------|---------------------------------------------------|
| classification = stagflation_warning    | HIGH       | Stagflation regime requires immediate review      |
| classification = defensive_regime       | HIGH       | Contraction regime with high confidence           |
| classification = divergence_watch       | MEDIUM     | Market-macro divergence detected                  |
| final_macro_state = strong_contraction  | HIGH       | Final signal strongly contractionary              |

---

## Controls Not Yet Implemented

| Control                                      | Gate | Status                            |
|----------------------------------------------|------|-----------------------------------|
| Live macro data feed integration             | 1    | TODO:DATA_DEPENDENCY              |
| Live SPX feed integration                    | 1, 3 | TODO:DATA_DEPENDENCY              |
| Pre-computed MA short/long for SPX           | 3    | TODO:DATA_DEPENDENCY — caller must supply |
| Realized vol (SPX) computation               | 3    | TODO:DATA_DEPENDENCY — caller must supply |
| GDP nowcast live feed                        | 5    | TODO:DATA_DEPENDENCY              |
| Composite growth index                       | 5    | TODO:DATA_DEPENDENCY              |
| Recession probability model                  | 5    | TODO:DATA_DEPENDENCY              |
| Net liquidity change computation             | 10   | TODO:DATA_DEPENDENCY              |
| EM FX stress index                           | 11   | TODO:DATA_DEPENDENCY              |
| Processed snapshot hash store                | 1    | TODO:DATA_DEPENDENCY              |
| Input quality scores per series              | 17   | TODO:DATA_DEPENDENCY — caller must supply |
| Historical state counts (persistence)        | 20   | TODO:DATA_DEPENDENCY — caller must supply |
| Regime prediction accuracy feedback          | 21   | TODO:DATA_DEPENDENCY              |
| False positive rate computation              | 21   | TODO:DATA_DEPENDENCY              |
| Threshold calibration write-back             | 21   | TODO:DATA_DEPENDENCY              |
| Macro state record persistence               | 21   | TODO:DATA_DEPENDENCY              |

---

## What This Agent Does Not Do

- It does not make trade decisions or emit buy/sell signals.
- It does not compute raw economic series from source data. All macro metrics must be pre-computed and supplied in the snapshot by the caller.
- It does not perform NLP or ML inference. Sentiment scores, term densities, and all language-derived inputs must be pre-computed.
- It does not manage positions or interact with execution infrastructure.
- It does not mutate historical threshold calibration values at runtime. All calibration events are recorded to the decision log pending human-supervised write-back.
- It does not contact external APIs or data sources directly.

---

## Human Oversight Points

| Trigger                                      | Action Required                                              |
|----------------------------------------------|--------------------------------------------------------------|
| classification = stagflation_warning         | Immediate review; significant regime shift with inflation and growth risk |
| classification = defensive_regime            | Review downstream risk controls; contraction confirmed with high confidence |
| classification = divergence_watch            | Investigate which markets or data sources are out of sync    |
| final_macro_state = strong_contraction       | Review all open positions and exposure limits                |
| confidence_state = conflicted                | Components disagree strongly; regime signal is unreliable    |
| benchmark_state = bearish AND classification = pro_growth_regime | Market and macro are diverging; investigate data lag |
| warning_state = market_macro_divergence      | Market pricing contradicts macro fundamentals; either model or market is wrong |
| evaluation loop flags FP rate breach         | Contraction or expansion thresholds need human-supervised recalibration |
