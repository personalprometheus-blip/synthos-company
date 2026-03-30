# Synthos — Agent 4 (RumorAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 4 (RumorAgent) is the social and rumor flow analysis layer of the Synthos
system. It runs every 15 minutes during market hours and every 30 minutes during
extended pre- and post-market windows. Its function is to ingest social posts from
monitored feeds, classify every post through a 24-gate deterministic decision spine,
and forward high-confidence signals to the signal queue for Agent 1 (ExecutionAgent)
evaluation.

**Agent 4 does not use machine learning or AI inference to make classification
decisions.** All decisions are rule-based, deterministic, and fully traceable. Every
post that advances past Gate 1 produces a complete `RumorDecisionLog` entry recording
each gate's inputs, evaluated values, result, and reason code. No post is classified,
forwarded, or suppressed without passing through all applicable gates documented below.

**Strict scope:** Agent 4 processes social media posts and rumor flows only.
Political and legislative news signals are delegated to Agent 2 (ResearchAgent).
Open position sentiment monitoring is delegated to Agent 3 (SentimentAgent).

**Benchmark context:** The S&P 500 (SPX) is used throughout as the operating
environment anchor. Gates 14, 18, and 24 reference benchmark state directly.

---

## 2. Operational Schedule

| Session      | Frequency                        | Primary Purpose                              |
|--------------|----------------------------------|----------------------------------------------|
| Pre-market   | Every 30 min, 7:00am–9:30am ET   | Early rumor detection before open            |
| Market hours | Every 15 min, 9:30am–4:00pm ET   | Full real-time scan with benchmark context   |
| Post-market  | Every 30 min, 4:00pm–8:00pm ET   | Extended session, earnings rumor monitoring  |
| Overnight    | No scan                          | Social feed rate limits; low signal value    |

---

## 3. Classification Spine — Overview

Agent 4 operates a 24-gate sequential classification spine. Each gate is a binary
or categorical check. A failure at any gate halts progression and records the reason
code. Every post that clears Gate 1 produces a complete `RumorDecisionLog` record.

```
GATE 1  — System Gate               (feed health, parse failure, staleness, duplicates)
GATE 2  — Source Identity           (verification, credibility, role, prior accuracy)
GATE 3  — Engagement / Propagation  (repost velocity, breadth, bot detection)
GATE 4  — Content Classification    (rumor / leak / opinion / originating / secondary / refutation / satire)
GATE 5  — Claim Detection           (market-relevant claim extraction, scope, specificity)
GATE 6  — Entity Mapping            (company, sector, regulator, benchmark constituent)
GATE 7  — Rumor Confirmation        (independent confirmations, official filings, denial, contradiction)
GATE 8  — Sentiment Extraction      (keyword-based: positive / negative / neutral / mixed / panic)
GATE 9  — Novelty / Surprise        (semantic distance, incremental information, already-priced check)
GATE 10 — Manipulation / Abuse      (pump/dump language, coordination, bot amplification, impersonation)
GATE 11 — Media / Attachment        (evidence type, parse status, forensic edit check)
GATE 12 — Market Impact Scope       (single_name / peer_group / sector / marketwide)
GATE 13 — Time Horizon              (intraday / multi-day / persistent; decay rate)
GATE 14 — Benchmark-Relative        (aligned / countertrend; alpha vs. beta; dominance)
GATE 15 — Crowding / Saturation     (cluster volume, attention, one-sided sentiment, exhaustion)
GATE 16 — Contradiction / Ambiguity (internal conflict, hedged language, sarcasm, cluster conflict)
GATE 17 — Timing Controls           (premarket / intraday / postmarket; expired; burst vs. fading)
GATE 18 — Impact Magnitude          (high / medium / low; benchmark linkage)
GATE 19 — Action Classification     (bullish / bearish / ignore / manipulation_watch / freeze / confirmed)
GATE 20 — Risk Discounts            (confidence scaling: anonymity, vol, manipulation, bot)
GATE 21 — Persistence Controls      (expected decay rate: rapid / slow / persistent_candidate)
GATE 22 — Evaluation Loop           (realized vs. predicted move; source score update)
GATE 23 — Output Controls           (output mode: decisive / probabilistic / uncertain; routing)
GATE 24 — Final Composite Score     (weighted score → final_signal: bullish / bearish / neutral / override)
```

---

## 4. Data Sources

Agent 4 uses publicly available social data sources:

| Source              | Type                        | Coverage            | Market | Extended |
|---------------------|-----------------------------|---------------------|--------|----------|
| StockTwits API      | Market-focused social posts | Per-ticker streams  | Yes    | Yes      |
| Reddit API (free)   | Community discussion        | r/wallstreetbets, r/investing, r/stocks | Yes | Yes |
| Yahoo Finance RSS   | News volume proxy           | Ticker headlines    | Yes    | No       |

> **DATA NOTE:** Twitter/X API access requires a paid tier. Integration is flagged for
> future implementation once a suitable API tier is available. Tracked as `TODO: DATA_DEPENDENCY`.

---

## 5. Gate-by-Gate Description

### Gate 1 — System Gate

**Purpose:** Reject posts before analysis if data quality or feed health conditions
are not met. No subsequent processing occurs for any post that fails this gate.

**Checks performed:**
- **Feed unavailability:** If the social data source is offline or returns an error
  status, ingestion halts for that source. Other sources continue independently.
- **Parse failure:** If the post text field is null or unparseable, the post is
  rejected immediately.
- **Missing timestamp:** If the post has no parseable publication timestamp, the
  post is rejected — timing controls throughout the pipeline depend on this field.
- **Staleness:** If the time elapsed since post publication exceeds `MAX_SOCIAL_AGE_HOURS`
  (default: 4 hours), the post is marked stale and discarded. Social signals decay
  rapidly; stale posts offer no actionable value.
- **Duplicate detection:** Word-level Jaccard similarity is computed between the
  current post and all posts processed in the current run. If similarity exceeds
  `DUPLICATE_THRESHOLD` (default: 0.75), the post is suppressed as a duplicate.
- **Benchmark data unavailable:** If SPX price data cannot be retrieved for Gates
  14 and 18, the benchmark context is disabled but processing continues with
  benchmark-dependent gates bypassed.
- **Unsupported language:** If the detected post language is not in `SUPPORTED_LANGUAGES`
  (default: `{"en"}`), the post is rejected.
- **Insufficient content:** If the post character count is below `MIN_CHARACTER_COUNT`
  (default: 20 characters), the post is rejected as too short for meaningful analysis.

**Outcome:** PROCEED or one of: `halt_ingestion`, `reject_post`, `stale_post`,
`suppress_duplicate`, `benchmark_context_disabled`, `insufficient_content`. Reason logged.

---

### Gate 2 — Source Identity Controls

**Purpose:** Classify the source account's identity type, credibility standing, and
historical accuracy. These source states are used in Gates 10, 19, 20, and 24
to scale confidence and apply discounts.

**Checks performed:**
- **Verification flag:** Whether the platform has verified the account identity.
  Verified accounts receive `source_state = verified`.
- **Credibility score:** A composite score (0–100) derived from follower count,
  account age, and engagement ratio. Scores at or above `CREDIBILITY_THRESHOLD`
  (default: 60) → `source_state = credible`. Scores below → `source_state = low_credibility`.
- **Account type:** Accounts classified as `company_official`, `regulator_official`,
  or `government_official` → `source_state = official`.
- **Professional role:** Accounts classified as `journalist`, `analyst`, `researcher`,
  or `industry_specialist` → `source_state = professional`.
- **Identity confidence:** If the platform's identity confidence score for the account
  falls below `IDENTITY_THRESHOLD` (default: 50) → `source_state = anonymous`. Anonymous
  sources propagate to Gate 20 (anonymity discount).
- **Historical claim accuracy:** If the source's tracked historical claim accuracy
  is at or above `SOURCE_ACCURACY_THRESHOLD` (default: 0.60) → `source_state = historically_reliable`.
  If below → `source_state = historically_unreliable`.

> **DATA NOTE:** Per-account historical claim accuracy tracking requires a running
> source accuracy ledger. Current implementation initialises all unknown sources at
> a neutral 0.50 accuracy score. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** One or more source state labels assigned. Multiple states can coexist
(e.g., `verified` AND `historically_reliable`). All states written to decision log.

---

### Gate 3 — Engagement / Propagation Controls

**Purpose:** Characterise how the post is spreading. Propagation patterns identify
whether interest is organic and broad or concentrated and potentially artificial.

**Checks performed:**
- **Repost velocity:** If `reposts_per_minute > REPOST_VELOCITY_THRESHOLD` (default: 10)
  → `propagation_state = fast_spread`.
- **Engagement velocity:** If `engagements_per_minute > ENGAGEMENT_VELOCITY_THRESHOLD`
  (default: 20) → `propagation_state = high_attention`.
- **Unique account breadth:** If `unique_accounts_sharing > BREADTH_THRESHOLD`
  (default: 25) → `propagation_state = broad_distribution`. If at or below →
  `propagation_state = narrow_distribution`.
- **Amplification concentration:** If the top-K accounts account for more than
  `CONCENTRATION_THRESHOLD` (default: 0.80) of total amplification →
  `propagation_state = concentrated_boost`. Concentrated amplification is a
  prerequisite for the bot and manipulation checks in Gate 10.
- **Bot-like amplification:** If the estimated bot probability of amplifying accounts
  exceeds `BOT_THRESHOLD` (default: 0.70) → `propagation_state = synthetic_amplification`.
  This state propagates to Gate 20 (bot discount).
- **Propagation decay:** If current engagement velocity has fallen by more than
  `DECAY_THRESHOLD_ENGAGE` (default: 0.25) relative to the prior measurement interval
  → `propagation_state = decaying`.

> **DATA NOTE:** Bot probability computation requires a dedicated bot-detection
> API or trained classifier. Current implementation uses a heuristic proxy
> (account age, post frequency, follower/following ratio). Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Propagation state(s) written to decision log. Multiple states can apply.

---

### Gate 4 — Content Classification

**Purpose:** Identify the nature of the post content — is it a rumor, a leak, an
opinion, a primary originating claim, a repost, a denial, or satire?

**Classification logic (evaluated in priority order):**
1. **Refutation:** Post contains denial, false, or inaccurate terms → `content_state = refutation`.
2. **Satire:** Satire probability exceeds `SATIRE_THRESHOLD` (default: 0.70) → `content_state = satire`.
   Satire posts are logged and discarded; they are not forwarded as signals.
3. **Leak:** Post contains leak, insider, or "source says" terms → `content_state = leak`.
4. **Rumor:** Post contains unconfirmed or speculative terms AND no official source
   is present → `content_state = rumor`.
5. **Originating claim:** Claim novelty exceeds `NOVELTY_THRESHOLD_CONTENT` (default: 0.60)
   AND no prior claim references detected → `content_state = originating_claim`.
6. **Secondary flow:** Post references an existing claim → `content_state = secondary_flow`.
7. **Opinion:** Post contains opinion markers AND factual claim density is below
   `FACT_THRESHOLD` (default: 0.05) → `content_state = opinion`.
8. **Uncertain:** If the maximum probability across all content type classifiers is
   below `CONTENT_CONFIDENCE_THRESHOLD` (default: 0.55) → `content_state = uncertain`.

**Outcome:** Content state assigned. Satire and opinion posts are retained in the
decision log but do not advance to action classification.

---

### Gate 5 — Claim Detection

**Purpose:** Determine whether the post contains a specific, market-relevant claim,
characterise that claim's scope, and assess its specificity.

**Checks performed:**
- **Market-relevant claim:** If the post explicitly references a corporate action,
  earnings event, regulatory change, macro event, or geopolitical event, the claim
  is flagged `claim_state = market_relevant`.
- **Company scope:** If one or more recognized company names are detected →
  `claim_scope = company`.
- **Sector scope:** If one or more recognized sector names are detected and no
  company names are detected → `claim_scope = sector`.
- **Market-wide scope:** If the post contains index terms, macro terms, or broad
  policy terms → `claim_scope = marketwide`.
- **Non-actionable:** If claim extraction confidence falls below `CLAIM_THRESHOLD`
  (default: 0.50) → `claim_state = non_actionable`. Non-actionable posts do not
  advance to signal forwarding.
- **Multi-claim:** If more than one distinct claim is detected → `claim_state = multi_claim`.
  Each claim is evaluated separately.
- **Specificity — high:** If the claim contains named entities AND time or metric
  detail → `claim_state = specific`.
- **Specificity — low:** If the claim lacks named entities or has detail density
  below `DETAIL_THRESHOLD` (default: 0.08) → `claim_state = vague`.

**Outcome:** Claim state, scope, and specificity written to decision log.

---

### Gate 6 — Entity Mapping

**Purpose:** Map the detected claim to specific tradeable entities. Determines
whether the post concerns a benchmark constituent, creating downstream relevance
in Gates 14 and 24.

**Checks performed:**
- **Company entity:** If one or more recognized company names detected →
  `entity_state = company_linked`.
- **Benchmark constituent:** If the recognized company appears in the SPX constituent
  list → `entity_state = benchmark_constituent`. Benchmark constituent posts receive
  elevated weight in Gate 14.
- **Multiple companies:** If more than one company detected →
  `entity_state = multi_company`.
- **Sector entity:** If one or more recognized sector names detected →
  `entity_state = sector_linked`.
- **Regulatory or government entity:** If a recognized regulatory or government body
  detected → `entity_state = policy_linked`.
- **No tradeable entity:** If no tradeable entity can be mapped →
  `entity_state = non_actionable`. Post does not advance to signal forwarding.

**Outcome:** Entity state(s) and entity list written to decision log.

---

### Gate 7 — Rumor Confirmation Controls

**Purpose:** Assess the level of independent confirmation or denial for the claim.
Confirmation state is the primary input to action classification in Gate 19.

**Confirmation states (evaluated in priority order):**
- **Official denial:** An official regulatory filing, company statement, or official
  press release directly refutes the claim → `confirmation_state = officially_denied`.
  This state triggers the denial override in Gate 24.
- **Primary source confirmed:** An official filing, official statement, or primary
  document confirms the claim → `confirmation_state = primary_confirmed`.
- **Contradictory:** Claim variance across independent sources exceeds
  `CONTRADICTION_THRESHOLD` (default: 0.40) → `confirmation_state = contradictory`.
- **Strong:** At least `MIN_CONFIRMATIONS` (default: 2) independent confirmations
  are present → `confirmation_state = strong`.
- **Weak:** Exactly one indirect confirmation, no primary source →
  `confirmation_state = weak`.
- **Unconfirmed:** No independent confirmations detected →
  `confirmation_state = unconfirmed`.
- **Expired unconfirmed:** Elapsed time since first claim exceeds
  `CONFIRMATION_TIMEOUT_HOURS` (default: 24) AND state is still `unconfirmed` or
  `weak` → `confirmation_state = expired_unconfirmed`.

**Outcome:** Confirmation state written to decision log. Used directly by Gates 19 and 24.

---

### Gate 8 — Sentiment Extraction

**Purpose:** Score the directional sentiment of the post using keyword matching.

**Method:**
- Tokenise post text.
- Count matches against POSITIVE and NEGATIVE term sets.
- `sentiment_score = (positive_count − negative_count) / total_words`
- `sentiment_confidence = (positive_count + negative_count) / (total_words / 15)`, capped at 1.0.
- `emotion_score = emotion_term_count / total_words`
- `panic_term_density = panic_term_count / total_words`

**Sentiment direction rules:**
- `score > POSITIVE_THRESHOLD (0.08)` → `sentiment_state = positive`
- `score < NEGATIVE_THRESHOLD (−0.08)` → `sentiment_state = negative`
- Both positive and negative density exceed `MIXED_FLOOR (0.03)` → `sentiment_state = mixed`
- `sentiment_confidence < SENTIMENT_CONFIDENCE_THRESHOLD (0.40)` → `sentiment_state = uncertain`
- `emotion_score > EMOTION_THRESHOLD (0.15)` → `sentiment_state = emotionally_charged`
- `panic_term_density > PANIC_THRESHOLD (0.10)` → `sentiment_state = panic_flag`
- All other cases → `sentiment_state = neutral`

**Outcome:** Sentiment state, score, and confidence. Low-confidence sentiment
propagates to Gate 20 (confidence discount). Panic flag annotates the decision log.

---

### Gate 9 — Novelty / Surprise Controls

**Purpose:** Detect whether the post adds new information relative to the existing
claim cluster, and whether any implied event represents a meaningful surprise.

**Checks performed:**
- **Semantic novelty:** Jaccard distance between the current post and the most similar
  prior claim cluster. If distance exceeds `NOVELTY_THRESHOLD_SEMANTIC` (default: 0.65)
  → `novelty_state = high`. Otherwise → `novelty_state = low`.
- **Incremental information:** If the new information score meets or exceeds
  `MIN_INCREMENTAL_INFO` (default: 0.25) → `information_state = incremental`.
  Otherwise → `information_state = repetitive`.
- **Surprise level:** If the implied or alleged event delta exceeds `SURPRISE_THRESHOLD`
  (default: 0.20) → `surprise_state = high`.
- **Already priced:** If the pre/post-market price move for the entity exceeds
  `PRICED_MOVE_THRESHOLD` (default: 0.5%) AND novelty is low →
  `pricing_state = already_priced`.

> **DATA NOTE:** 'Already priced' detection requires real-time price data.
> Current implementation checks the broker data API. If unavailable, this check
> is skipped. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Novelty state, information state, and surprise state written to decision
log. Repetitive or already-priced posts are retained in the log but annotated
for deprioritisation.

---

### Gate 10 — Manipulation / Abuse Controls

**Purpose:** Detect coordinated manipulation, automated amplification, and
deceptive content patterns that should suppress or discount the signal.

**Checks performed:**
- **Pump language:** Post contains exaggerated upside terms or coordinated buy terms
  → `abuse_state = pump_risk`.
- **Dump language:** Post contains fear trigger terms or coordinated sell terms
  → `abuse_state = dump_risk`.
- **Coordination pattern:** Message similarity across accounts in the same cluster
  exceeds `COORDINATION_THRESHOLD` (default: 0.85) → `abuse_state = coordinated_campaign`.
- **Bot probability:** If bot probability for the source or cluster exceeds
  `BOT_THRESHOLD` (default: 0.70) → `abuse_state = automation_risk`.
- **Suspicious amplification:** Follower count is below `FOLLOWER_FLOOR` (default: 100)
  AND propagation is fast spread → `abuse_state = suspicious_amplification`.
- **Impersonation risk:** Identity similarity to a known verified account exceeds
  `IMPERSONATION_THRESHOLD` (default: 0.85) AND the source is unverified →
  `abuse_state = impersonation_risk`.
- **Fabricated media:** If an attached image or document scores below
  `AUTHENTICITY_THRESHOLD` (default: 0.40) on forensic authenticity →
  `abuse_state = fabricated_media_risk`.

**Outcome:** Abuse state written to decision log. Any active abuse state triggers
`classification = manipulation_watch` in Gate 19 and a discount in Gate 20.

---

### Gate 11 — Media / Attachment Controls

**Purpose:** Classify any attached evidence and flag authenticity risks.

**Checks performed:**
- **No attachment:** `attachment_count = 0` → `media_state = text_only`.
- **Attached evidence:** Attachment type is image, PDF, screenshot, or document
  → `media_state = attached_evidence`.
- **Unreadable attachment:** Attachment parse fails → `media_state = unreadable_attachment`.
  Unreadable attachments cannot support claim confirmation.
- **Manipulated attachment risk:** Forensic edit score exceeds `EDIT_THRESHOLD`
  (default: 0.60) → `media_state = manipulated_attachment_risk`. Flagged for review.
- **Plausible official format:** Template similarity to known official document formats
  exceeds `FORMAT_THRESHOLD` (default: 0.75) → `media_state = plausible_official_format`.
  Supports the confirmation assessment in Gate 7.
- **Unknown provenance:** Original source of attachment cannot be determined →
  `media_state = unknown_provenance`.

> **DATA NOTE:** Forensic image analysis requires a dedicated media authenticity
> service. Current implementation uses metadata inspection only (file size patterns,
> EXIF consistency). Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Media state written to decision log.

---

### Gate 12 — Market Impact Scope

**Purpose:** Classify the expected breadth of market impact — is this a
single-name event, a peer group effect, a sector move, or a broad market signal?

**Classification:**
- **Single name:** Claim scope = company AND exactly one company detected →
  `impact_scope = single_name`.
- **Peer group:** Peer transfer likelihood exceeds `PEER_THRESHOLD` (default: 0.50)
  → `impact_scope = peer_group`. Triggered when the claim, while company-specific,
  is likely to reprice peers (e.g., an acquisition claim).
- **Sector:** Claim scope = sector → `impact_scope = sector_only`.
- **Broad market:** Claim scope is marketwide OR a policy entity is present →
  `impact_scope = marketwide`.
- **Unclear:** Scope confidence falls below `SCOPE_THRESHOLD` (default: 0.55) →
  `impact_scope = unclear`.

**Outcome:** Impact scope written to decision log. Used in Gates 14, 19, and 24.

---

### Gate 13 — Time Horizon

**Purpose:** Classify the expected duration of the rumor's market impact and
its likely decay rate.

**Horizon classification:**
- **Intraday:** Historical median decay for this rumor event class is at or below
  `IMMEDIATE_WINDOW_HOURS` (default: 4 hours) → `horizon_state = intraday`.
- **Multi-day:** Median decay is between `IMMEDIATE_WINDOW_HOURS` and
  `SHORT_WINDOW_HOURS` (default: 48 hours) → `horizon_state = multi_day`.
- **Persistent:** Median decay exceeds `SHORT_WINDOW_HOURS` → `horizon_state = persistent`.

**Decay rate:**
- Persistence score below `TRANSIENT_THRESHOLD` (default: 0.35) → `decay_state = fast_decay`.
- Persistence score at or above `PERSISTENT_THRESHOLD` (default: 0.65) → `decay_state = slow_decay`.

> **DATA NOTE:** Historical rumor decay by event class requires a populated
> outcome tracking ledger. Current implementation uses static heuristics by
> claim type (M&A → slow, product chatter → fast). Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Horizon state and decay state written to decision log.

---

### Gate 14 — Benchmark-Relative Interpretation

**Purpose:** Calibrate the rumor's interpretation against the current S&P 500
regime. Determines whether the signal is aligned with or contrary to the
prevailing benchmark trend, and whether it represents an alpha or beta opportunity.

**Interpretations:**
- Positive sentiment + bullish benchmark → `relative_state = aligned_positive`
- Negative sentiment + bearish benchmark → `relative_state = aligned_negative`
- Positive sentiment + bearish benchmark → `relative_state = countertrend_positive`
- Negative sentiment + bullish benchmark → `relative_state = countertrend_negative`

**Signal type:**
- Predicted asset move minus predicted SPX move exceeds `ALPHA_THRESHOLD` (default: 0.5%)
  → `signal_type = alpha`.
- Absolute difference between asset and SPX predicted move falls within `BETA_BAND`
  (default: 0.3%) → `signal_type = beta`.

**Dominance:**
- If benchmark weight exceeds the rumor-specific weight in the interpretation
  → `interpretation_state = benchmark_dominant`.
- If rumor-specific weight exceeds benchmark weight
  → `interpretation_state = idiosyncratic_dominant`.

**Outcome:** Relative state, signal type, and interpretation state written to
decision log. Used in Gate 19 and Gate 24.

---

### Gate 15 — Crowding / Saturation Controls

**Purpose:** Detect when a rumor cluster has become overcrowded, when attention
is extreme, or when the signal may be exhausted.

**Checks performed:**
- **Crowded cluster:** Posts in the claim cluster within the evaluation window exceed
  `CLUSTER_VOLUME_THRESHOLD` (default: 20) → `crowding_state = crowded`.
- **Extreme attention:** Combined mention count across social and news exceeds
  `ATTENTION_THRESHOLD` (default: 1,000) → `crowding_state = extreme_attention`.
- **One-sided sentiment:** Sentiment dispersion across posts in the cluster falls
  below `LOW_DISPERSION_THRESHOLD` (default: 0.10) → `crowding_state = one_sided`.
  One-sided crowds can indicate herding rather than independent analysis.
- **Exhausted:** Realized absolute market move exceeds `EXHAUSTION_MOVE_THRESHOLD`
  (default: 2%) AND novelty is low → `crowding_state = exhausted`.
- **Active development:** Cluster growth rate exceeds `GROWTH_THRESHOLD` (default: 5
  posts/min) → `crowding_state = active_development`. Growing clusters receive
  elevated urgency in output routing.

**Outcome:** Crowding state written to decision log. Exhausted state annotates signal
for deprioritisation. Active development elevates output priority.

---

### Gate 16 — Contradiction / Ambiguity Controls

**Purpose:** Detect internally inconsistent, heavily hedged, sarcastic, or
cluster-conflicted posts that reduce classification confidence.

**Checks performed:**
- **Internal inconsistency:** Claim conflict score within the post exceeds
  `INTERNAL_CONFLICT_THRESHOLD` (default: 0.50) → `ambiguity_state = internally_conflicted`.
- **Hedged language:** Uncertainty term density exceeds `UNCERTAINTY_THRESHOLD_AMB`
  (default: 0.12) → `ambiguity_state = hedged_language`.
- **Sarcasm risk:** Sarcasm score exceeds `SARCASM_THRESHOLD` (default: 0.60)
  → `ambiguity_state = sarcasm_risk`.
- **Cluster conflict:** Cross-post claim variance within the cluster exceeds
  `CLUSTER_CONFLICT_THRESHOLD` (default: 0.40) → `ambiguity_state = cluster_conflicted`.
  This state propagates to Gate 19 (`classification = freeze`).
- **Clear:** All ambiguity checks pass → `ambiguity_state = clear`.

**Outcome:** Ambiguity state written to decision log. Any non-clear state suppresses
the signal to at most `watch_only` classification.

---

### Gate 17 — Timing Controls

**Purpose:** Classify the timing of the post relative to the trading session
and determine whether the signal is still actionable.

**Checks performed:**
- **Premarket:** Post time is before market open → `timing_state = premarket`.
- **Intraday:** Post time is within market session → `timing_state = intraday`.
- **Postmarket:** Post time is after market close → `timing_state = postmarket`.
- **Expired:** Elapsed time since post publication exceeds `TRADEABLE_SOCIAL_WINDOW_HOURS`
  (default: 2 hours) → `timing_state = expired`. Expired posts are not forwarded as signals.
- **Active flow:** Posts per minute in the claim cluster exceed `BURST_THRESHOLD`
  (default: 10) → `timing_state = active_flow`.
- **Fading flow:** Posts per minute in the cluster fall below `DECAY_FLOW_THRESHOLD`
  (default: 2) → `timing_state = fading`.

**Outcome:** Timing state written to decision log. Expired posts do not proceed to
action classification.

---

### Gate 18 — Impact Magnitude Estimation

**Purpose:** Estimate the expected absolute price impact of the rumor, and assess
how strongly that impact is correlated to S&P 500 movement.

**Magnitude classification:**
- Predicted absolute move exceeds `HIGH_IMPACT_THRESHOLD` (default: 3%) →
  `impact_state = high`.
- Predicted absolute move is between `MEDIUM_IMPACT_THRESHOLD` (default: 1%)
  and the high threshold → `impact_state = medium`.
- Predicted absolute move is at or below the medium threshold → `impact_state = low`.

**Benchmark linkage:**
- Historical correlation between this rumor class's response and SPX response
  exceeds `SPX_LINK_THRESHOLD` (default: 0.60) → `impact_link_state = benchmark_linked`.
- At or below threshold → `impact_link_state = benchmark_weak`.

> **DATA NOTE:** Predicted move estimation uses static heuristics by claim type
> and entity market cap tier. A data-driven model requires post-trade outcome
> correlation history. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Impact state and benchmark linkage written to decision log.

---

### Gate 19 — Action Classification

**Purpose:** Combine all upstream gate results into a single classification decision.

**Classification rules (evaluated in priority order):**

| Condition | Classification |
|-----------|---------------|
| `confirmation_state = officially_denied` | `deny_and_discount` |
| `classification = manipulation_watch` OR any `abuse_state` present | `manipulation_watch` |
| `confirmation_state = contradictory` OR `ambiguity_state = cluster_conflicted` | `freeze` |
| `confirmation_state = primary_confirmed` AND `claim_state = market_relevant` | `upgraded_to_confirmed_event` |
| `impact_scope = marketwide` AND `impact_link_state = benchmark_linked` | `benchmark_regime_signal` |
| `impact_scope = single_name` AND `benchmark_state = neutral` AND `signal_type = alpha` | `relative_alpha_signal` |
| `source_score >= credibility_threshold` AND `novelty_state = high` AND `claim_state = market_relevant` AND `sentiment_state = positive` | `bullish_rumor_signal` |
| `source_score >= credibility_threshold` AND `novelty_state = high` AND `claim_state = market_relevant` AND `sentiment_state = negative` | `bearish_rumor_signal` |
| `sentiment_state = mixed` AND `ambiguity_state != clear` | `watch_only` |
| `source_score < credibility_threshold` AND `confirmation_state IN {unconfirmed, weak}` | `ignore` |
| All other | `watch_only` |

**Outcome:** Classification label written to decision log. Used by Gates 20, 23, and 24.

---

### Gate 20 — Risk Discounts

**Purpose:** Apply downward scaling to the impact score to reflect uncertainty
and risk factors identified in prior gates. Does not change the classification
label — adjusts the numeric confidence used in Gate 24.

**Discounts applied (multiplicative, in sequence):**
- `sentiment_confidence < SENTIMENT_CONFIDENCE_THRESHOLD` → multiply by
  `LOW_CONFIDENCE_DISCOUNT` (default: 0.75).
- `source_state = anonymous` → multiply by `ANONYMITY_DISCOUNT` (default: 0.60).
- Benchmark volatility is HIGH → multiply by `HIGH_VOL_DISCOUNT` (default: 0.80).
- Any active `abuse_state` → multiply by `MANIPULATION_DISCOUNT` (default: 0.40).
- `propagation_state = synthetic_amplification` → multiply by `BOT_DISCOUNT` (default: 0.50).
- New conflicting posts arriving exceed `CONFLICT_UPDATE_THRESHOLD` (default: 3)
  → `classification = freeze` (overrides prior classification).

**Outcome:** Discounted impact score written to decision log. All discount
factors and their reasons are individually logged.

---

### Gate 21 — Persistence Controls

**Purpose:** Classify the expected decay rate of the rumor's market impact
to inform how long the signal should remain active.

**Persistence rules:**
- `persistence_score < TRANSIENT_THRESHOLD (0.35)` → `expected_decay = rapid`.
- `persistence_score >= STRUCTURAL_THRESHOLD (0.70)` → `expected_decay = slow`.
- Claim contains M&A terms, financing terms, or regulation terms →
  `expected_decay = persistent_candidate`.
- Claim contains minor product terms AND no official confirmation →
  `expected_decay = likely_fast`.
- `confirmation_state = primary_confirmed` → `expected_decay = recompute_persistence`.
  Confirmed signals are re-evaluated against their event class decay profile.

**Outcome:** Expected decay classification written to decision log.
Used to set signal expiry in the output record.

---

### Gate 22 — Evaluation Loop

**Purpose:** Update model scores and source credibility weights based on the
accuracy of prior predictions. Enables the system to learn which rumor classes
and which sources are historically reliable.

**Updates performed:**
- **Signal retention:** If `classification NOT IN {ignore, reject_post}` →
  rumor state is stored in the evaluation ledger for later comparison.
- **Prediction tracking:** If a predicted move exists, the realized move over the
  `evaluation_window` is retrieved and compared.
- **Error high:** `abs(predicted_move − realized_move) > PREDICTION_ERROR_THRESHOLD`
  (default: 1.5%) → `rumor_class_model_score -= ERROR_PENALTY (0.05)`.
- **Error low:** `abs(predicted_move − realized_move) <= PREDICTION_ERROR_THRESHOLD`
  → `rumor_class_model_score += ACCURACY_REWARD (0.03)`.
- **Rolling class error high:** `rolling_mean_prediction_error(rumor_class) > ROLLING_ERROR_THRESHOLD`
  (default: 2.0%) → downweight that rumor class in composite scoring.
- **Rolling class accuracy high:** Rolling mean error at or below threshold →
  upweight that rumor class.
- **Source accuracy update:** If source has been repeatedly accurate →
  `source_score += SOURCE_REWARD`. If repeatedly inaccurate →
  `source_score -= SOURCE_PENALTY`.

> **DATA NOTE:** Post-trade outcome data must be fed back to the evaluation ledger
> by Agent 1 after trade resolution. This integration is flagged for future
> implementation. Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** Updated model scores and source scores written to decision log
and to the evaluation ledger.

---

### Gate 23 — Output Controls

**Purpose:** Determine the output mode (how confident the output statement is)
and the output action (what the downstream system should do with this signal).

**Output mode by confidence:**
- `overall_confidence >= HIGH_CONFIDENCE_THRESHOLD (0.70)` → `output_mode = decisive`.
- `MEDIUM_CONFIDENCE_THRESHOLD (0.45) <= overall_confidence < HIGH_CONFIDENCE_THRESHOLD`
  → `output_mode = probabilistic`.
- `overall_confidence < MEDIUM_CONFIDENCE_THRESHOLD` → `output_mode = uncertain`.

**Output priority by interpretation state:**
- `interpretation_state = benchmark_dominant` → `output_priority = benchmark_first`.
- `interpretation_state = idiosyncratic_dominant` → `output_priority = rumor_first`.

**Output action by classification:**

| Classification | Output Action |
|----------------|--------------|
| `freeze` | `wait_for_confirmation` |
| `ignore` | `no_signal` |
| `bullish_rumor_signal` | `positive_signal` |
| `bearish_rumor_signal` | `negative_signal` |
| `benchmark_regime_signal` | `benchmark_context_signal` |
| `relative_alpha_signal` | `idiosyncratic_alpha_signal` |
| `manipulation_watch` | `monitor_for_abuse` |
| `upgraded_to_confirmed_event` | `transition_to_news_event_logic` |
| `deny_and_discount` | `no_signal` |
| `watch_only` | `monitor` |

**Outcome:** Output mode, priority, and action written to decision log.
Used by the pipeline dispatcher to route to the signal queue or discard.

---

### Gate 24 — Final Composite Score

**Purpose:** Compute a single composite score that synthesises all gate outputs.
The composite score drives the `final_signal` classification.

**Score formula:**

```
composite_score = (w1 × credibility)
               + (w2 × relevance)
               + (w3 × novelty)
               + (w4 × confirmation)
               + (w5 × propagation_quality)
               + (w6 × impact)
               − (w7 × ambiguity)
               − (w8 × manipulation_risk)
               − (w9 × bot_risk)
```

**Default weights:**

| Component | Weight |
|-----------|--------|
| credibility | 0.20 |
| relevance | 0.15 |
| novelty | 0.15 |
| confirmation | 0.15 |
| propagation_quality | 0.10 |
| impact | 0.10 |
| ambiguity (penalty) | 0.07 |
| manipulation_risk (penalty) | 0.05 |
| bot_risk (penalty) | 0.03 |

All weights are configurable module-level constants.

**Final signal rules (evaluated in priority order):**

| Condition | Final Signal |
|-----------|-------------|
| `confirmation_state = officially_denied` | `denial_override` |
| `classification = manipulation_watch` AND any `abuse_state` present | `suppress_or_discount` |
| `benchmark_risk_state = drawdown` AND `impact_link_state = benchmark_linked` | `benchmark_override` |
| `composite_score >= BULLISH_SCORE_THRESHOLD (0.60)` AND `sentiment_state = positive` | `bullish` |
| `composite_score <= BEARISH_SCORE_THRESHOLD (0.30)` AND `sentiment_state = negative` | `bearish` |
| All other | `neutral_or_watch` |

**Outcome:** Composite score and final signal written to decision log and to the
output record. The output record is dispatched to the signal queue or discarded
per Gate 23 routing.

---

## 6. Rumor Decision Log

Every post that advances past Gate 1 produces a `RumorDecisionLog` entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Run timestamp and session type
- Post ID, source identifier, and post timestamp
- Each gate's name, inputs evaluated, result (PASS / FAIL / STATE), and reason code
- Final classification and final signal
- Composite score with all component values
- Output mode and output action
- Expiry timestamp based on persistence classification

> **FLAG — LOG WRITE LOCATION:** Currently written via `db_helpers.DB.log_event()`
> to the `system_log` table. A dedicated `rumor_decisions` table is recommended
> to support regulatory export and volume management. Tracked as future work item.

---

## 7. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Twitter/X social feed | Paid API tier | TODO: DATA_DEPENDENCY |
| Per-account bot probability model | Trained classifier or API | TODO: DATA_DEPENDENCY |
| Per-account historical claim accuracy | Running source accuracy ledger | TODO: DATA_DEPENDENCY |
| Forensic image authenticity | Dedicated media analysis service | TODO: DATA_DEPENDENCY |
| Predicted move estimation (data-driven) | Post-trade outcome correlation | TODO: DATA_DEPENDENCY |
| Historical rumor decay by event class | Outcome tracking ledger | TODO: DATA_DEPENDENCY |
| Post-trade outcome feedback to Gate 22 | Agent 1 integration | TODO: DATA_DEPENDENCY |
| Already-priced detection (real-time price) | Broker intraday API | TODO: DATA_DEPENDENCY |
| VIX-based benchmark volatility regime | CBOE VIX data feed | TODO: DATA_DEPENDENCY |

---

## 8. What Agent 4 Does Not Do

- Agent 4 does not use any AI language model to classify social posts or rumors.
- Agent 4 does not make trade entry, sizing, or exit decisions. All trade decisions
  are made by Agent 1 (ExecutionAgent).
- Agent 4 does not analyse news or legislative signals. That is Agent 2 (ResearchAgent).
- Agent 4 does not monitor open position sentiment. That is Agent 3 (SentimentAgent).
- Agent 4 does not send communications directly. All output is written to the
  company database via `db_helpers`. Alerts route through the company node
  notification pipeline (Scoop agent).
- Agent 4 does not independently trigger position exits or trade actions.

---

## 9. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| `classification = manipulation_watch` | Flagged in decision log; not forwarded as signal | Review flagged post cluster for coordinated abuse |
| `classification = freeze` | Signal held; `output_action = wait_for_confirmation` | Monitor for clarification before acting |
| `confirmation_state = officially_denied` | Denial override applied; signal suppressed | Review if denial itself is contested |
| `classification = upgraded_to_confirmed_event` | Forwarded to news event logic | Confirm transition to Agent 2 handling is correct |
| All social feeds unavailable | Run exits cleanly; logged | Investigate feed connectivity |
| Source score floor reached | Applied to all posts from that source; logged | Review source credibility and consider removal |
| Repeated prediction error high (Gate 22) | Rumor class downweighted | Review evaluation ledger; consider threshold adjustment |
