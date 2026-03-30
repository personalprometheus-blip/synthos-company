# Synthos — Agent 2 (ResearchAgent) System Description
## Regulatory Reference Document

**Document Version:** 2.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 2 (ResearchAgent) is the signal sourcing and classification layer of the Synthos
system. It runs every hour during market hours and every four hours overnight. Its
function is to locate political and legislative disclosures and news, classify every
article through an 18-gate deterministic decision spine, and forward credible signals
to Agent 1 (ExecutionAgent) for trade evaluation.

**Agent 2 does not use machine learning or AI inference to make classification
decisions.** All decisions are rule-based, deterministic, and fully traceable. Every
article processed produces a structured `NewsDecisionLog` entry recording each gate's
inputs, evaluation result, and reason code. No signal is queued, watched, or discarded
without passing through all applicable gates documented below.

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

Agent 2 operates an 18-gate sequential classification spine. Each gate is a binary
or categorical check. A failure at any gate halts progression and records the reason.
Every article that passes Gate 3 produces a complete `NewsDecisionLog` record.

```
GATE 1  — System Gate          (parse failure, timestamp, duplicate detection)
GATE 2  — Benchmark Gate       (SPX regime — trend, volatility, drawdown)
GATE 3  — Eligibility Filter   (source tier, word count)
GATE 4  — News Classification  (topic: macro / earnings / geopolitical / regulatory / company / sector)
GATE 5  — Event Detection      (scheduled / breaking / follow-up / rumor)
GATE 6  — Sentiment Extraction (keyword-based: POSITIVE / NEGATIVE / NEUTRAL / MIXED)
GATE 7  — Novelty Controls     (repetition detection, incremental information check)
GATE 8  — Market Impact        (scope: broad_market / sector_subset / single_name; horizon)
GATE 9  — Benchmark Relative   (alignment, alpha vs. beta, overwhelm check)
GATE 10 — Credibility          (source count, primary source signals, misinformation risk)
GATE 11 — Timing Controls      (staleness, premarket / intraday / postmarket)
GATE 12 — Crowding             (cluster volume, saturation detection)
GATE 13 — Contradiction        (uncertainty density, headline/body mismatch)
GATE 14 — Action Classification(bullish / bearish / relative_alpha / spx_regime / watch / ignore)
GATE 15 — Risk Controls        (confidence discounts: vol, rumor, low sentiment confidence)
GATE 16 — Persistence          (expected decay rate: HIGH / MEDIUM / LOW / DYNAMIC)
GATE 17 — Evaluation Loop      (relevance recompute, accuracy tracking placeholder)
GATE 18 — Output Controls      (routing: QUEUE / WATCH / DISCARD; explanation framing)
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

## 5. Gate-by-Gate Description

### Gate 1 — System Gate

**Purpose:** Reject articles before analysis if basic data quality conditions are not met.

**Checks performed:**
- **Parse failure:** If headline is null or shorter than 5 characters, halt immediately.
- **Timestamp / staleness:** If the disclosure date is more than `MAX_NEWS_AGE_HOURS`
  (default 24 hours) in the past, halt.
- **Duplicate detection:** Compute word-level Jaccard similarity between the current
  article and all articles processed in the current run. If similarity exceeds
  `DUPLICATE_THRESHOLD` (default 0.60), halt as duplicate.

**Outcome:** PROCEED or HALT. Reason logged.

---

### Gate 2 — Benchmark Gate (SPX Context)

**Purpose:** Determine the current S&P 500 market regime for use in all downstream
gates that consider benchmark context. Called once per run; regime applied to all
articles in that session.

**Checks performed:**
- **Trend:** If `SMA_short(SPX) > SMA_long(SPX)` → UP. If below → DOWN. Otherwise NEUTRAL.
- **Volatility:** If ATR/price ratio exceeds `SPX_VOL_THRESHOLD` → HIGH. Otherwise NORMAL.
- **Drawdown:** If `(SPX_current − rolling_peak) / rolling_peak ≤ −SPX_DRAWDOWN_THRESH` → active.

> **DATA NOTE:** VIX integration is flagged for future implementation. Current
> volatility regime uses realized ATR/price as proxy. Tracked as `TODO: DATA_DEPENDENCY`.

**Benchmark sensitivity:** The regime is used in Gates 8, 9, 14, 15, and 18 to
adjust signal interpretation and apply risk discounts.

**Outcome:** `BenchmarkRegime` (trend, volatility, drawdown_active) applied session-wide.

---

### Gate 3 — Eligibility Filter

**Purpose:** Reject articles that do not meet minimum quality standards for signal extraction.

**Checks performed:**
- **Source tier:** Tier 4 (opinion/social) excluded unconditionally. Any tier above
  `CREDIBILITY_TIER_MAX` (default 3) is excluded.
- **Word count:** Articles with fewer than `MIN_WORD_COUNT` (default 8) words are
  too short for meaningful signal extraction.

> **DATA NOTE:** Language detection and topic universe filtering are flagged for
> future implementation. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** PROCEED or SKIP.

---

### Gate 4 — News Classification

**Purpose:** Identify the article's primary topic category.

**Topic classification (priority order):**
1. **Company-specific:** Named ticker found in SECTOR_TICKER_MAP.
2. **Sector-specific:** Sector name found in headline/subhead.
3. **Regulatory:** Regulatory terms (SEC, DOJ, FTC, legislation, compliance, etc.).
4. **Earnings:** Earnings terms (revenue, EPS, guidance, quarterly results, etc.).
5. **Geopolitical:** Geopolitical terms (sanctions, tariffs, elections, military, etc.).
6. **Macro:** Macro terms (Fed, interest rates, inflation, GDP, unemployment, etc.).
7. **Unknown:** No term matches.

**Impact scope assignment:**
- Company → `single_name`
- Sector, Regulatory, Earnings → `sector_subset`
- Macro, Geopolitical → `broad_market`

**Outcome:** Topic label and impact scope written to decision context.

---

### Gate 5 — Event Detection

**Purpose:** Classify the nature of the event described in the article.

**Event types:**
- **Breaking:** Source tier ≤ 2 AND article is brief (< 50 words) — wire breaking news pattern.
- **Follow-up:** Jaccard similarity to recent articles in current run > `FOLLOW_UP_SIMILARITY`.
- **Rumor:** Source tier = 3 AND uncertainty term density > 0.10.
- **Scheduled:** Tier 1 government publications (assumed scheduled).
- **Unscheduled:** All other cases.

> **DATA NOTE:** Automated event calendar integration (FOMC, CPI, earnings) is flagged
> for future implementation. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Event type label applied. Rumor flag propagates to Gate 15 (risk discount).

---

### Gate 6 — Sentiment Extraction

**Purpose:** Score the directional sentiment of the article using keyword matching.

**Method:**
- Tokenise headline + subhead.
- Count matches against POSITIVE and NEGATIVE term sets.
- `sentiment_score = (positive_count − negative_count) / total_words`
- `sentiment_confidence = (positive_count + negative_count) / (total_words / 15)`, capped at 1.0.

**Sentiment direction rules:**
- `score > POSITIVE_THRESHOLD (0.10)` → POSITIVE
- `score < NEGATIVE_THRESHOLD (−0.10)` → NEGATIVE
- Both positive and negative counts exceed `MIXED_MIN_THRESHOLD (0.05)` of total → MIXED
- All other cases → NEUTRAL

**Congressional disclosure override:** For congressional trade disclosures, the
transaction type (BUY vs SELL) is the primary directional signal. Keyword sentiment
provides corroborating context.

**Outcome:** Sentiment direction, score, and confidence. Low-confidence sentiment
propagates to Gate 15 (discount).

---

### Gate 7 — Novelty Controls

**Purpose:** Detect repetitive content and articles that add no incremental information.

**Checks performed:**
- **Batch novelty:** Jaccard similarity against all articles processed in the current run.
- **DB novelty:** Jaccard similarity against signals in the database created within
  the last 4 hours.
- `novelty_score = 1 − max_similarity`
- **Repetition:** `novelty_score < MIN_INCREMENTAL_INFO (0.25)` → flag as repetition.
- **Novelty threshold:** `novelty_score < NOVELTY_THRESHOLD (0.40)` → new_info_ok = False.

> **DATA NOTE:** 'Already priced' detection — comparing novelty to prior market price
> moves — requires post-trade outcome correlation. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Novelty score and repetition flag. Repetition propagates to Gate 14 (discard).

---

### Gate 8 — Market Impact Estimation

**Purpose:** Assess the expected breadth and duration of the article's market impact.

**Impact scope:** Derived from Gate 4 topic classification.

**Impact horizon:**
- Regulatory, geopolitical, macro → multi-day
- Earnings → multi-day
- All other → intraday

**Benchmark correlation estimate (heuristic):**
- Macro, geopolitical → HIGH
- Sector → MEDIUM
- Company → LOW

**Magnitude:** Elevated to HIGH if benchmark volatility is HIGH.

> **DATA NOTE:** Historical correlation between topic type and realized asset/sector
> moves is not yet computed. Current heuristics are conservative defaults.
> Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Scope, horizon, benchmark_corr, magnitude written to decision context.

---

### Gate 9 — Benchmark-Relative Interpretation

**Purpose:** Adjust signal interpretation based on whether the article's sentiment
aligns with or contradicts the current SPX regime.

**Interpretations:**
- Positive sentiment + DOWN trend → `momentum_headwind`
- Negative sentiment + UP trend → `counter_trend`
- Sentiment aligned with trend → `aligned`
- Otherwise → `neutral_backdrop`

**Alpha signal:** Article is company-specific (single_name scope) with LOW benchmark
correlation → classified as potential alpha opportunity.

**Overwhelmed by benchmark:** Article effect likely dominated by regime (HIGH benchmark
correlation in strong trend) → flagged.

**Outcome:** Interpretation, alpha_signal flag, overwhelmed flag.

---

### Gate 10 — Credibility and Confirmation

**Purpose:** Assess source credibility, confirmation count, and misinformation risk.

**Checks performed:**
- **Primary source indicators:** Article text contains filing, press release,
  official statement, or regulatory record references → `has_primary_source = True`.
- **Source count:** Query recent signals for same ticker (last 8 hours). Count as
  proxy for independent confirmations.
- **Confirmed:** `source_count ≥ MIN_CONFIRMATIONS (2)`.
- **Misinformation risk:** Tier 3 source + no primary source + uncertainty density > 0.08.

**Confidence adjustment by source:**
- Tier 1 → HIGH
- Tier 2 + (primary source OR confirmed) → HIGH
- Tier 2 → MEDIUM
- Tier 3 + primary source + confirmed → MEDIUM
- All other → LOW

**Outcome:** `conf_adj` (HIGH/MEDIUM/LOW) used in Gate 14. Misinformation risk
propagates to Gate 14 (discard).

---

### Gate 11 — Timing Controls

**Purpose:** Reject articles that are too old to be tradeable or classify publication timing.

**Checks performed:**
- **Staleness:** If article age > `TRADEABLE_WINDOW_HOURS` (default 8) → NOT tradeable → halt.
- **Publication timing:** Classified as premarket / intraday / postmarket relative to
  9:30am–4:00pm ET window.

**Outcome:** tradeable flag. Non-tradeable articles are discarded before Gate 14.

---

### Gate 12 — Crowding / Saturation

**Purpose:** Detect topic saturation — too many articles on the same subject in a
short window.

**Check:** Count articles in the signals database (last 4 hours) that share ≥ 3
keyword tokens with the current article. If count ≥ `CLUSTER_VOL_THRESHOLD (8)` →
`saturated = True`.

> **DATA NOTE:** Social/news mention count for public attention measurement requires
> external API. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Cluster volume and saturation flag. Saturation annotates the decision log.

---

### Gate 13 — Contradiction / Ambiguity

**Purpose:** Detect internally inconsistent or highly ambiguous articles.

**Checks performed:**
- **Uncertainty density:** Count uncertainty terms / total words. If density >
  `UNCERTAINTY_DENSITY_MAX (0.12)` → high uncertainty.
- **Headline/body mismatch:** If headline sentiment direction differs from subhead
  sentiment direction and both have meaningful term counts → mismatch detected.

**Outcome:** `has_contradiction` flag. Contradiction propagates to Gate 14 (watch_only).

---

### Gate 14 — Action Classification

**Purpose:** Combine all upstream gate results into a single classification decision.

**Classification rules (in priority order):**

| Condition | Classification |
|-----------|---------------|
| repetition (Gate 7) | ignore |
| misinformation_risk (Gate 10) | ignore |
| low_credibility AND low_novelty | ignore |
| contradiction detected (Gate 13) | watch_only |
| NEUTRAL or MIXED sentiment OR low sentiment confidence | watch_only |
| broad_market scope AND HIGH benchmark correlation | spx_regime_signal |
| single_name scope AND LOW benchmark correlation | relative_alpha |
| POSITIVE sentiment AND credible AND novel | bullish_signal |
| NEGATIVE sentiment AND credible AND novel | bearish_signal |
| all other | watch_only |

**Outcome:** Classification label and confidence. Used by Gate 18 for routing.

---

### Gate 15 — Risk Controls

**Purpose:** Apply downward confidence adjustments without changing the classification label.

**Discounts applied:**
- Low sentiment confidence (Gate 6) → downgrade confidence one step.
- Benchmark volatility HIGH (Gate 2) → downgrade confidence one step.
- Rumor source (Gate 5) → downgrade confidence one step.

Each downgrade moves through the sequence: HIGH → MEDIUM → LOW → NOISE.

**Outcome:** `final_confidence` after all discounts applied.

---

### Gate 16 — Persistence Controls

**Purpose:** Classify the expected decay rate of the signal's market impact.

| Topic | Persistence | Decay Rate | Reason |
|-------|------------|-----------|--------|
| Regulatory | HIGH | Low | Structural repricing |
| Macro | HIGH | Low | Persistent shift |
| Geopolitical | DYNAMIC | Variable | Updated per follow-up |
| Earnings | MEDIUM | Medium | One-off unless guidance broadly revised |
| Breaking / other | LOW | High | Transient by default |

**Outcome:** Persistence label and decay_rate annotated to decision log.

---

### Gate 17 — Evaluation Loop

**Purpose:** Recompute relevance and record the classification for post-hoc accuracy tracking.

**Relevance recompute:** Query signals database for an active, non-expired signal for
the same ticker. If found → ticker is relevant to current portfolio.

> **DATA NOTE:** Comparing predicted vs. realized market response requires post-trade
> outcome data correlation. Accuracy tracking by event class is flagged for future
> implementation. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Relevance flag annotated. Placeholder for accuracy tracking.

---

### Gate 18 — Output Controls

**Purpose:** Shape the final output and determine routing.

**Output type by confidence:**
- HIGH → decisive classification
- MEDIUM → probabilistic classification
- LOW / NOISE → uncertain / monitor

**Explanation framing:**
- HIGH benchmark correlation → benchmark-first explanation
- LOW benchmark correlation → asset-first explanation

**Routing:**

| Classification | Routing |
|---------------|---------|
| bullish_signal | QUEUE |
| relative_alpha | QUEUE |
| watch_only | WATCH |
| spx_regime_signal | WATCH |
| bearish_signal | WATCH |
| ignore | DISCARD |

**Outcome:** Final classification, confidence, explanation, and routing decision.

---

## 6. Member Weight Application

After Gate 18, the base confidence label is adjusted by the historical accuracy of the
Congress member associated with the disclosure (where applicable).

**Mechanism:** `adjusted_numeric = base_numeric × member_weight`. Adjusted label is
recomputed from the numeric result. Weight range: 0.5 (floor) to 1.5 (ceiling).
Minimum 5 tracked trades before weight deviates from 1.0 (neutral).

> **FLAG:** Member weight is currently applied after Gate 18 as a final adjustment.
> Future integration: incorporate directly into Gate 10 (credibility). Tracked as
> a design improvement item.

---

## 7. News Decision Log

Every article that advances past Gate 3 produces a `NewsDecisionLog` entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Session timestamp
- Ticker, headline, and source details
- Each gate's name, inputs evaluated, result, and reason code
- Final decision (QUEUE / WATCH / DISCARD)
- Confidence level and explanation

> **FLAG — LOG WRITE LOCATION:** Currently written to `system_log` table via
> `db.log_event()`. A dedicated `news_decisions` table is recommended to support
> regulatory export and volume management. Tracked as future work item.

---

## 8. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| VIX-based volatility regime | CBOE VIX data feed | TODO: DATA_DEPENDENCY |
| Automated event calendar | FOMC/CPI/earnings calendar API | TODO: DATA_DEPENDENCY |
| Already-priced detection | Post-trade outcome correlation | TODO: DATA_DEPENDENCY |
| Social mention count | External social/news API | TODO: DATA_DEPENDENCY |
| Analyst view dispersion | Aggregated consensus data | TODO: DATA_DEPENDENCY |
| Accuracy tracking by event class | Post-trade comparison | TODO: DATA_DEPENDENCY |
| Language detection | NLP library | TODO: DATA_DEPENDENCY |

---

## 9. What Agent 2 Does Not Do

- Agent 2 does not use any AI language model to classify signals.
- Agent 2 does not make trade entry, sizing, or exit decisions. All trade decisions
  are made by Agent 1 (ExecutionAgent).
- Agent 2 does not analyse open position sentiment. That is Agent 3 (SentimentAgent).
- Agent 2 does not send communications directly. All metadata is posted to the company
  node via fire-and-forget POST. All alerts route through the company node notification
  pipeline (Scoop agent).

---

## 10. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| All data sources return empty | Pipeline exits cleanly; logged | Investigate API connectivity |
| Benchmark data unavailable | Gate 2 defaults to NEUTRAL regime | No immediate action; review at next session |
| Cluster saturation reached | Annotated in log; signal still processed | Review for topic crowding risk |
| Member weight at floor (0.5) | Applied to all signals from that member | Review member's trading history |
| All signals routing to DISCARD | Logged; no signals queued for Bolt | Check source quality and thresholds |
