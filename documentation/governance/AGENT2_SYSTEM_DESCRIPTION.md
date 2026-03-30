# Synthos — Agent 2 (ResearchAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 2 (ResearchAgent) is the signal sourcing and pre-scoring layer of the Synthos
system. It runs every hour during market hours and every four hours overnight. Its
sole function is to locate political and legislative disclosures, score their quality
using defined rules, and forward credible signals to Agent 1 (ExecutionAgent) for
trade evaluation.

**Agent 2 does not use machine learning or AI inference to make classification
decisions.** All signal classification decisions are rule-based, deterministic, and
fully traceable. Every signal processed produces a structured human-readable
classification log. No signal is queued for trading, watched, or discarded without
passing through the documented classification rules below.

**Strict scope:** Agent 2 processes political and legislative signals only. It does
not analyse market sentiment or price action. Sentiment analysis is delegated to
Agent 3 (SentimentAgent).

---

## 2. Operational Schedule

| Session  | Frequency            | Primary Purpose                                 |
|----------|---------------------|-------------------------------------------------|
| Market   | Hourly, 9am–4pm ET  | Full scan: all four source types                |
| Overnight| Every 4 hours       | Capitol Trades + Congress.gov only (reduced API)|

---

## 3. Signal Pipeline — Overview

Agent 2 operates a sequential 8-stage pipeline. Each stage is deterministic.
Every signal that advances past Stage 3 produces a `SignalDecisionLog` entry
recording all inputs, intermediate results, and the final classification decision.

```
STAGE 1 — Source Intake        (fetch from up to 4 data sources)
STAGE 2 — Ticker Resolution    (identify the relevant instrument)
STAGE 3 — Tier Classification  (assign quality tier based on source)
STAGE 4 — Staleness Assessment (score disclosure timeliness)
STAGE 5 — Corroboration Check  (verify against Tier 1 official sources)
STAGE 6 — Member Weight        (apply per-politician reliability factor)
STAGE 7 — Queue/Watch/Discard  (final routing decision)
STAGE 8 — Interrogation Broadcast (peer validation via UDP)
```

---

## 4. Stage-by-Stage Description

### Stage 1 — Source Intake

**Purpose:** Retrieve raw disclosures and legislative signals from configured
data sources.

**Sources polled:**

| Source | Type | Tier | Market Session | Overnight |
|--------|------|------|---------------|-----------|
| Capitol Trades API | Congressional trade disclosures | 1 | Yes | Yes |
| Congress.gov API | Bill activity, committee actions | 1 | Yes | Yes |
| Federal Register API | Procurement, regulatory notices | 1 | Yes | No |
| RSS feeds (Reuters, AP, Politico, The Hill, Roll Call, Bloomberg) | Wire and press | 2–3 | Yes | No |

**Tier assignment at source:**
- **Tier 1 (Official):** Government APIs with verifiable legislative or regulatory basis.
- **Tier 2 (Wire):** Established wire services (Reuters, Associated Press).
- **Tier 3 (Press):** Political and financial press publications.
- **Tier 4 (Opinion):** Any opinion, social, or unverified source — immediately discarded.

Custom RSS feeds can be injected via the `RSS_FEEDS_JSON` environment variable by
the portal operator.

**Outcome:** List of raw signal objects, each tagged with source name and tier.

---

### Stage 2 — Ticker Resolution

**Purpose:** Identify the relevant equity instrument for each signal.

**Resolution method:**
1. If the source record provides an explicit ticker symbol (e.g., Capitol Trades),
   that ticker is used directly.
2. If no ticker is present, the headline is compared against a sector-to-ticker
   lookup table to infer the most likely instrument.
3. If no ticker can be resolved, the signal is discarded at this stage with a
   logged reason (`No ticker resolved`).

**Outcome:** Signal with a resolved ticker symbol, or DISCARD.

---

### Stage 3 — Tier Classification and Base Confidence

**Purpose:** Assign a base confidence level to the signal based entirely on
the quality of its source.

**Classification rules:**

| Source Tier | Base Confidence | Initial Routing |
|-------------|----------------|-----------------|
| Tier 1 | HIGH | Proceed immediately — no corroboration required |
| Tier 2 | Subject to Stage 5 corroboration | WATCH by default |
| Tier 3 | MEDIUM | WATCH by default |
| Tier 4 | NOISE | DISCARD immediately |

Tier 1 signals bypass corroboration and proceed directly to Stage 6.
Tier 4 signals are discarded immediately with no further processing.

**Outcome:** Base confidence (HIGH / MEDIUM / LOW / NOISE) and initial routing.

---

### Stage 4 — Staleness Assessment

**Purpose:** Penalise signals where the time between the transaction date and
the disclosure date is excessive.

**Staleness scoring:**

| Days Between Transaction and Disclosure | Label   | Score Discount |
|-----------------------------------------|---------|---------------|
| 0–3 days                               | Fresh   | 0%            |
| 4–7 days                               | Aging   | 15%            |
| 8–14 days                              | Stale   | 30%            |
| >14 days                               | Expired | 50% — DISCARD  |

**Additional discard conditions at this stage:**
- **Spousal/dependent filings:** If the disclosing party is identified as a
  spouse or dependent of the Congress member, the signal is discarded.
  Rationale: spousal filings carry reduced legislative foreknowledge signal.
- **Amended filings:** Flagged for awareness. Amended filings are not
  automatically discarded but are annotated in the signal record.

**Outcome:** Staleness label applied. Expired signals and spousal filings discarded.

---

### Stage 5 — Corroboration Check

**Purpose:** For Tier 2 and Tier 3 signals, verify whether a corresponding
Tier 1 official signal exists for the same ticker. This prevents acting on
press or wire stories that are not backed by an official legislative or
regulatory record.

**Check method:** Query the signals database for a non-expired Tier 1 record
with a matching ticker symbol. This is a database lookup — no external API
call is made at this stage.

**Corroboration outcomes by tier:**

| Tier | Corroborated | Staleness | Decision |
|------|-------------|-----------|----------|
| 2 (Wire) | Yes | Any | QUEUE / HIGH |
| 2 (Wire) | No | Fresh or Aging | WATCH / MEDIUM |
| 2 (Wire) | No | Stale | DISCARD / LOW |
| 3 (Press) | Yes | Fresh or Aging | QUEUE / MEDIUM |
| 3 (Press) | No | Fresh or Aging | WATCH / MEDIUM |
| 3 (Press) | Any | Stale or Expired | DISCARD / LOW |

**Outcome:** QUEUE, WATCH, or DISCARD decision with reason code.
All inputs and the decision are written to the `SignalDecisionLog`.

---

### Stage 6 — Member Weight Application

**Purpose:** Adjust the base confidence score using the historical accuracy
of the specific Congress member associated with the disclosure.

**Weight mechanics:**
- Each member has a reliability weight stored in the `member_weights` database table.
- New members default to a weight of 1.0 (neutral).
- Weight range: 0.5 (minimum) to 1.5 (maximum).
- Weight changes require a minimum of 5 tracked trades before the weight
  deviates from 1.0.
- Adjusted numeric score = base numeric × member weight.

**Adjusted confidence thresholds:**

| Adjusted Numeric | Adjusted Text |
|-----------------|--------------|
| ≥ 0.85 | HIGH |
| ≥ 0.45 | MEDIUM |
| ≥ 0.10 | LOW |
| < 0.10 | NOISE → DISCARD |

**Outcome:** Adjusted confidence label and numeric score.

---

### Stage 7 — Queue / Watch / Discard

**Purpose:** Apply the minimum signal threshold and route the signal.

**Minimum threshold:** If the adjusted numeric score falls below the configured
minimum (`MIN_SIGNAL_THRESHOLD`, default 0.1), the signal is dropped regardless
of tier or corroboration.

**Routing outcomes:**
- **QUEUE:** Signal forwarded to Agent 1 (ExecutionAgent) for trade evaluation.
- **WATCH:** Signal written to the signals database with status WATCHING.
  Re-evaluated in subsequent sessions if corroboration arrives.
- **DISCARD:** Signal written to database as NOISE. No trade evaluation performed.

All signals — including discarded ones — are written to the `news_feed` table
for portal display. The portal shows all signals regardless of routing decision.

**Outcome:** Signal routed to Bolt queue, WATCH list, or discarded.

---

### Stage 8 — Interrogation Broadcast

**Purpose:** Announce signals that proceed to the queue via UDP broadcast,
allowing peer agents to corroborate or challenge the signal within a 30-second
window.

**Mechanism:** A `HAS_DATA_FOR_INTERROGATION` message is broadcast on the
local network. Peer agents may respond with `INTERROGATION_ACK` within the
timeout window.

**Outcomes:**
- **VALIDATED:** A peer agent acknowledged the signal. Interrogation status
  written to the signal record.
- **UNVALIDATED:** No peer responded within the timeout. Signal proceeds
  without peer corroboration. This is the expected state when no peer is present.

**Post-broadcast:** Price history (1-year daily bars for the ticker, industry ETF,
and sector ETF) is fetched from Alpaca and attached to the interrogation announcement.
This data is not stored in the signal record after broadcast — it is held in memory
only for the duration of the announcement.

---

## 5. Signal Decision Log

Every signal processed by Agent 2 (Tier 2 and Tier 3) produces a `SignalDecisionLog`
entry. Tier 1 and Tier 4 signals are handled by fixed rules and logged directly.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Session timestamp
- Ticker and source details
- Each classification stage's name, value, and note
- Final decision (QUEUE / WATCH / DISCARD)
- Confidence level
- Reason code

> **FLAG — LOG WRITE LOCATION:** Currently written to `system_log` table via
> `db.log_event()`. A dedicated `signal_decisions` table is recommended to
> support regulatory export and volume management.
> Tracked as future work item.

---

## 6. Re-Evaluation of WATCH Signals

Signals that were previously placed in WATCH status are re-evaluated each
session using the same `classify_signal()` rules. The primary mechanism by
which a WATCH signal is promoted is the arrival of a Tier 1 corroboration
signal for the same ticker (detected at Stage 5).

Re-evaluation uses identical deterministic logic — no separate rules or
exceptions apply. The re-evaluation decision is logged identically to the
initial classification.

---

## 7. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Automated event calendar exclusion | FOMC/CPI/earnings calendar API | TODO: DATA_DEPENDENCY |
| Cross-ticker correlation for disclosure clustering | Multi-ticker price data | TODO: DATA_DEPENDENCY |
| Dark pool volume detection | Broker dark pool data feed | TODO: DATA_DEPENDENCY |
| Multi-member clustering (coordinated trades) | Historical member trade data | TODO: FUTURE_WORK |

---

## 8. What Agent 2 Does Not Do

- Agent 2 does not use any AI language model to classify signals.
- Agent 2 does not make trade entry, sizing, or exit decisions. All trade
  decisions are made by Agent 1 (ExecutionAgent).
- Agent 2 does not analyse market sentiment. That is the role of Agent 3
  (SentimentAgent).
- Agent 2 does not send communications directly. All metadata is posted to
  the company node via a fire-and-forget POST. All alerts route through the
  company node notification pipeline (Scoop agent).
- Agent 2 does not access the internet for signal validation — corroboration
  is performed exclusively against the local signals database.

---

## 9. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| Tier 1 signal for new instrument | Auto-HIGH / QUEUE | None — fully automated |
| Member weight drops to floor (0.5) | Weight floor applied; signal annotated | Review of member's trading history recommended |
| All sources return empty | Logged; pipeline exits cleanly | Investigate API connectivity |
| Interrogation port in use | Skips reply listener; marks UNVALIDATED | Check for port conflict if persistent |
| RSS feed parse error | Logged; feed skipped | Review feed URL if persistent |
