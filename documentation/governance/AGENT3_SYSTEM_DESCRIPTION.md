# Synthos — Agent 3 (SentimentAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 3 (SentimentAgent) is the market sentiment monitoring layer of the Synthos
system. It runs every 30 minutes during market hours. Its function is to watch open
positions and queued signals for signs of market deterioration, detect cascade
patterns, and log warnings that Agent 1 (ExecutionAgent) reads when managing
positions.

**Agent 3 does not use machine learning or AI inference.** All cascade detection
and scan analysis decisions are rule-based, deterministic, and fully traceable. Every
position scan produces a structured human-readable `ScanDecisionLog` entry recording
all data inputs, intermediate signal evaluations, and the final tier classification.

**Agent 3 does not trade.** It does not issue buy or sell orders. It logs warnings
and annotates signals. Agent 1 makes all final trade decisions.

---

## 2. Operational Schedule

| Session | Frequency | Primary Purpose |
|---------|-----------|-----------------|
| Market hours | Every 30 minutes, 9am–4pm ET | Scan open positions + pre-trade queue |

---

## 3. Sentiment Pipeline — Overview

Agent 3 operates two sequential scans per run:

```
SCAN A — Open Position Monitor
  STAGE 1 — Data Collection     (put/call ratio, insider transactions, volume)
  STAGE 2 — Cascade Detection   (rule-based multi-signal evaluation)
  STAGE 3 — Scan Analysis       (structured analysis of aligned/misaligned signals)
  STAGE 4 — Scan Log Write      (all results written to scan_log table)

SCAN B — Pre-Trade Queue Check
  STAGE 1 — Data Collection     (same three signals, 7-day lookback for insider data)
  STAGE 2 — Cascade Detection   (same rules)
  STAGE 3 — Signal Annotation   (tier and summary written back to signal record)
```

---

## 4. Data Sources

Agent 3 uses three free, publicly available data sources:

| Source | Data Fetched | Limitation |
|--------|-------------|-----------|
| CBOE Daily Statistics | Market-wide equity put/call ratio | Market-wide only — not per-ticker |
| SEC EDGAR (Form 4) | Insider transaction filings (past 30 days) | Filing count used as proxy; full XML parsing is future work |
| Finviz (free HTML) | Relative volume, price change % | Scraped HTML — may break if Finviz layout changes |
| Yahoo Finance RSS | News volume as market attention proxy | Indirect proxy only |

> **DATA NOTE:** Per-ticker options put/call data requires a paid data feed.
> Current implementation uses market-wide CBOE ratio as a proxy for all positions.
> This is documented as a known limitation. Tracked as a DATA_DEPENDENCY.

---

## 5. Stage-by-Stage Description — Open Position Monitor

### Stage 1 — Data Collection

For each open position, three independent signals are fetched:

1. **Put/call ratio:** Retrieved from CBOE public statistics page. The market-wide
   equity put/call ratio is used as a proxy. A 30-day historical average of
   approximately 0.62 is used as the baseline (CBOE long-term average).
2. **Insider transactions:** SEC EDGAR Form 4 filings for the ticker over the
   past 30 days. The count of filing records is used as a proxy for buy/sell counts.
   Net dollar value is estimated from filing counts (conservative estimate).
3. **Volume profile:** Relative volume (today vs. average) and price change direction
   from Finviz. Seller dominance is estimated from price change magnitude combined
   with relative volume.

---

### Stage 2 — Cascade Detection

**Purpose:** Classify the combined sentiment picture into one of four tiers.

**Critical distinction:** A single large sell order is normal profit-taking behaviour.
A cascade requires multiple independent sellers acting simultaneously. This rule is
applied to prevent over-reaction to single-actor events.

**Signal thresholds:**

| Signal | Critical Threshold | Elevated Threshold |
|--------|-------------------|--------------------|
| Put/call ratio | > 110% of 30d average | > 120% of average |
| Insider selling | ≥ 4 sells with 0 buys | Sells exceed buys by > 2 |
| Volume cascade | > 250% average volume AND seller dominance > 70% | Volume > 80% above average |

**Tier classification logic:**

| Critical Signals | Elevated Signals | Tier | Label |
|-----------------|-----------------|------|-------|
| ≥ 2 | Any | 1 | CRITICAL |
| 1 | Any | 2 | ELEVATED |
| 0 | ≥ 2 | 2 | ELEVATED |
| 0 | 1 | 3 | NEUTRAL |
| 0 | 0 | 4 | QUIET |

All thresholds are configurable in the agent source via module-level constants.

**Outcome:** Tier (1–4), label (CRITICAL/ELEVATED/NEUTRAL/QUIET), and cascade flag.

---

### Stage 3 — Scan Analysis

**Purpose:** Produce a structured analysis of aligned and conflicting signals for
logging and Trader review. No external calls are made at this stage.

**Analysis components:**
1. **Signal status per source:** Each of the three signals is evaluated against
   its threshold and assigned a status (CRITICAL / ELEVATED / ABOVE_AVG / NORMAL).
2. **Actor assessment:** Whether multiple independent signals are aligning (cascade)
   or a single signal is elevated (isolated actor, likely profit-taking).
3. **Stop level context:** Current trailing stop fire level, computed from position's
   current price and trailing stop distance.
4. **Recommendation by tier:**
   - **CRITICAL:** Identifies aligning signals and their names. Recommends tightening
     trailing stop or preparing protective exit.
   - **ELEVATED:** Identifies the specific elevated signal. Recommends holding stops
     and monitoring for additional confirmation.
   - **NEUTRAL/QUIET:** No action required.

**Outcome:** Analysis string written to `ScanDecisionLog` and included in the
`event_summary` field of the `scan_log` table.

---

### Stage 4 — Scan Log Write

**Purpose:** Record all scan results to the database.

**Fields written:**
- Ticker
- Put/call ratio (current and 30d average)
- Insider net value
- Volume vs. average
- Seller dominance estimate
- Cascade flag
- Tier (1–4)
- Event summary (includes full analysis string)

> **FLAG — LOG WRITE LOCATION:** Scan results are written to the `scan_log` table.
> `ScanDecisionLog` entries are additionally written via `db.log_event()` to
> the `system_log` table. A dedicated `sentiment_decisions` table is recommended
> for regulatory export. Tracked as future work.

**Urgent flag protocol:** If a position scans at Tier 1 (CRITICAL), Agent 1's
active management gate (Gate 10) treats any CRITICAL flag on a held position as
a pre-authorized protective exit trigger. Agent 1 reads the `scan_log` table
each session. No additional signalling is required between agents.

---

## 6. Stage-by-Stage Description — Pre-Trade Queue Check

**Purpose:** Before Agent 1 acts on a queued signal, Agent 3 checks whether
current sentiment conditions are deteriorating for that instrument.

**Scope:** Same three signals as the open position scan. Insider lookback window
reduced to 7 days for queued signals (focusing on most recent activity).

**Outcome:** Tier and summary written back to the signal record via
`db.annotate_signal_pulse()`. Agent 1 reads this annotation as the
`sentiment_corroboration` input to Gate 5 (Signal Evaluation) and Gate 6
(Entry Decision).

---

## 7. Scan Decision Log

Every position and queued signal scanned at Tier 1 or Tier 2 produces a
`ScanDecisionLog` entry. All tiers are also recorded in `scan_log`.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per scan:**
- Session timestamp
- Ticker
- Each signal's name, value, and status
- Tier and cascade flag
- Full analysis string

> **FLAG — LOG WRITE LOCATION:** Currently written via `db.log_event()` to
> the `system_log` table. A dedicated `sentiment_decisions` table is recommended.
> Tracked as future work item.

---

## 8. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Per-ticker put/call ratio | Paid options data feed | TODO: DATA_DEPENDENCY |
| Full insider transaction parsing | SEC EDGAR Form 4 XML download | TODO: DATA_DEPENDENCY |
| Real-time volume data | Broker intraday bar feed | TODO: DATA_DEPENDENCY |
| Dark pool activity monitoring | Broker dark pool data | TODO: DATA_DEPENDENCY |

---

## 9. What Agent 3 Does Not Do

- Agent 3 does not use any AI language model to evaluate sentiment.
- Agent 3 does not place, modify, or cancel any trade orders.
- Agent 3 does not independently trigger position exits. It logs warnings.
  Agent 1 reads those warnings and makes the exit decision within its Gate 10
  active management logic.
- Agent 3 does not communicate directly. All findings are written to the database
  and read by Agent 1 at its next session.

---

## 10. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| Tier 1 CRITICAL on open position | Flag written to scan_log; Agent 1 reads at next session | Review protective exit if triggered |
| All data sources unavailable | Scan exits cleanly; positions not flagged | Investigate data source connectivity |
| CBOE page layout change | put/call returns None; treated as UNKNOWN | Update CBOE parser |
| Finviz blocks scraping | Volume data unavailable; cascade detection degrades | Consider alternative data source |
