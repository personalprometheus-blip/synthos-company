# Synthos — Agent 1 (ExecutionAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 1 (ExecutionAgent) is the trade execution layer of the Synthos system. It runs
three times per trading day and is responsible for determining whether a queued signal
warrants a trade, sizing that trade, managing open positions, and exiting positions
under defined conditions.

**Agent 1 does not use machine learning or AI inference to make trading decisions.**
All decisions are rule-based, deterministic, and fully traceable. Every decision
produces a structured human-readable audit log. No trade is entered, modified, or
exited without passing through all applicable control gates documented below.

---

## 2. Operational Schedule

| Session | Time (ET) | Primary Purpose |
|---------|-----------|-----------------|
| Open    | 09:30     | New signal evaluation, approved trade execution, position reconciliation |
| Midday  | 12:30     | Position management, re-evaluation of WATCH queue |
| Close   | 15:30     | Conservative review, monthly tax sweep, daily report |

---

## 3. Control Architecture — Overview

Agent 1 operates a sequential 14-gate decision spine. Each gate is a binary or
categorical check. A failure at any gate halts progression to the next gate.
Every gate records its input values, evaluation result, and reason code to the
trade decision log.

```
GATE 1  — System Gate          (hard stops — market hours, loss limits, API health)
GATE 2  — Benchmark Gate       (S&P 500 anchor — sets operating mode)
GATE 3  — Regime Detection     (volatility, trend, macro environment)
GATE 4  — Trade Eligibility    (liquidity, spread, event risk, correlation)
GATE 5  — Signal Evaluation    (confidence scoring, multi-factor weighting)
GATE 6  — Entry Decision       (momentum / mean-reversion / breakout selection)
GATE 7  — Position Sizing      (risk-adjusted, mode-adjusted, cap-enforced)
GATE 8  — Risk Setup           (stop loss, profit target, trailing stop)
GATE 9  — Execution            (order placement, slippage, fill handling)
GATE 10 — Active Management    (ongoing position monitoring every session)
GATE 11 — Portfolio Controls   (exposure, sector limits, leverage)
GATE 12 — Adaptive Layer       (performance-driven threshold adjustment)
GATE 13 — Stress Overrides     (flash crash, liquidity crisis, benchmark crash)
GATE 14 — Evaluation Loop      (post-trade metric update, kill condition)
```

---

## 4. Gate-by-Gate Description

### Gate 1 — System Gate (Hard Stops)

**Purpose:** Prevent any trading activity under conditions where the system cannot
operate safely or where predefined loss limits have been reached.

**Checks performed:**
- **Market hours:** Trading is only permitted during defined session windows. Any
  session triggered outside configured hours is halted immediately with no action taken.
- **Daily loss limit:** If realized P&L for the current calendar day has reached or
  exceeded the configured maximum daily loss, all new trade entry is blocked for the
  remainder of the day.
- **Portfolio drawdown limit:** If current account equity has fallen below the
  peak equity high-water mark by more than the configured drawdown threshold, the
  system enters a halt state and no new positions are opened.
- **Data integrity:** If required market data is stale beyond the configured latency
  tolerance or if the missing data ratio exceeds threshold, the system halts.
- **API health:** Repeated order acknowledgement failures or connection errors trigger
  a halt. The system does not proceed without confirmed broker connectivity.

**Outcome:** PROCEED or HALT. Any HALT is logged with the specific condition that
triggered it and an alert is dispatched via the company node notification pipeline.

---

### Gate 2 — Benchmark Gate (S&P 500 Anchor)

**Purpose:** Calibrate the system's operating mode — DEFENSIVE, NEUTRAL, or AGGRESSIVE —
based on the current state of the S&P 500. The benchmark acts as the primary
environmental filter. All subsequent sizing and entry decisions are scaled to this mode.

**Benchmark instrument:** Configurable (default: SPY). Evaluated using price bars
fetched from the broker data API.

**Mode determination logic:**
- **DEFENSIVE:** Benchmark rolling drawdown exceeds the configured threshold. The
  system reduces position sizes, tightens stops, and becomes highly selective on entries.
- **AGGRESSIVE:** Short-term moving average is above long-term moving average AND
  benchmark volatility (ATR/price) is within normal bounds. The system may deploy
  capital more actively.
- **NEUTRAL:** All other conditions. Default behavior applies.

**Benchmark trend detection:** Based on comparison of short-window and long-window
simple moving averages of the benchmark's closing price. No forecasting is used.
The trend is a backward-looking factual statement about price behaviour.

**Outcome:** Operating mode (DEFENSIVE / NEUTRAL / AGGRESSIVE) applied to all
downstream gates in this session.

---

### Gate 3 — Regime Detection

**Purpose:** Characterize the current market environment across three dimensions —
volatility regime, trend regime, and macro risk posture. These classifications
modify the behaviour of entry, sizing, and exit gates.

**Volatility regime:**
- Assessed using the benchmark's average true range (ATR) relative to its price level.
- HIGH volatility → position sizes are reduced and stops are widened.
- LOW/NORMAL volatility → standard parameters apply.

> **DATA NOTE:** VIX (CBOE Volatility Index) integration is flagged for future
> implementation. Current volatility regime uses realized ATR as proxy.

**Trend regime:**
- BULL / BEAR / SIDEWAYS based on moving average separation.
- SIDEWAYS conditions enable mean-reversion entry logic and disable breakout logic.
- BULL/BEAR conditions favour momentum and directional strategies.

**Macro risk posture (risk-on / risk-off):**
- Assessed using a composite of benchmark direction, Treasury ETF (TLT) direction,
  and credit spread proxy behaviour.
- RISK-OFF → gross exposure reduced, no new breakout entries, hedge allocation reviewed.

> **DATA NOTE:** Credit spread data integration is flagged for future implementation.
> Current risk-off detection uses benchmark + TLT as available proxies.

**Outcome:** Regime state (volatility level, trend label, risk posture) written to
decision context. Subsequent gates read from this context.

---

### Gate 4 — Trade Eligibility Filter

**Purpose:** Reject signals before analysis if the surrounding conditions make a
trade inadvisable regardless of signal quality.

**Checks performed:**
- **Liquidity:** Average daily volume of the target instrument must exceed the
  configured minimum. Low-volume instruments are skipped.
- **Spread:** The current bid-ask spread as a percentage of mid-price must not exceed
  the configured maximum. Wide spreads indicate poor execution conditions.
- **Event risk:** If the current time falls within a pre-event exclusion window
  (e.g., FOMC decision, CPI release, earnings announcement), entries are blocked or
  size-reduced per configuration.

> **DATA NOTE:** Automated event calendar integration is flagged for future
> implementation. Current implementation uses a manually maintained exclusion list.

- **Correlated exposure:** If adding this position would result in high correlation
  to the existing portfolio, the signal is skipped to maintain diversification.

**Outcome:** ELIGIBLE or SKIP. SKIP signals are logged with the specific filter
condition that triggered the rejection.

---

### Gate 5 — Signal Evaluation

**Purpose:** Score the incoming signal and determine whether its quality meets
the minimum threshold for consideration.

**Inputs:**
- Source tier (quality tier of the disclosure source)
- Politician weight (adjusted for historical accuracy of this member's disclosures)
- Staleness (time since the disclosure was made)
- Interrogation status (whether peer agents have validated or challenged the signal)
- Sentiment corroboration (whether market sentiment supports the signal direction)
- Benchmark-relative strength (whether the target asset is outperforming the benchmark
  over the configured rolling window)

**Scoring:**
- A composite score is computed as a weighted sum of the above factors.
- Weights are configurable and documented in the system parameters file.
- If the final score is below the configured minimum confidence threshold, the signal
  is rejected at this gate.

**Multi-signal handling:** If multiple signals for the same or correlated instruments
are active simultaneously, scores are weighted and the highest-scoring candidate is
selected for evaluation.

**Outcome:** Numeric confidence score. Signals below threshold → SKIP. Signals at
or above threshold → proceed to Gate 6.

---

### Gate 6 — Entry Decision

**Purpose:** Classify the type of entry condition and select the highest-scoring
trade candidate when multiple entry types are valid.

**Entry types evaluated:**
- **Momentum entry:** Price above moving average AND rate of change exceeds threshold.
  Suitable for trending instruments in BULL regime.
- **Mean-reversion entry:** Price has deviated below the mean by more than the
  configured Z-score threshold. Suitable for SIDEWAYS regime.
- **Breakout entry:** Price has exceeded the rolling N-period high. Suitable for
  BULL/NEUTRAL regime.
- **Pullback entry:** Price has retraced a defined percentage within an established
  uptrend. Suitable when trend is confirmed.

**Selection:** If multiple entry types qualify, the candidate with the highest
composite score is selected. Entry type is recorded in the decision log.

**Regime constraints:** Breakout logic is disabled in SIDEWAYS regime. Mean-reversion
logic is disabled in strong BULL/BEAR regimes to avoid counter-trend entries.

**Outcome:** Entry candidate (ticker, entry type, score) or NO ENTRY.

---

### Gate 7 — Position Sizing

**Purpose:** Determine the number of shares/units to trade, ensuring the position
is appropriately sized for the current risk environment.

**Base size calculation:**
- Risk per trade is a configured percentage of portfolio equity.
- Base size = risk amount / stop distance (ATR-based).

**Adjustments applied in sequence:**
1. Volatility adjustment: size scaled inversely to asset volatility relative to target.
2. Mode adjustment: DEFENSIVE → multiply by defensive factor (<1.0).
   AGGRESSIVE → multiply by aggressive factor (>1.0).
3. Drawdown scaling: size reduced proportionally as portfolio drawdown increases.
4. Maximum cap: final size may not exceed configured maximum position percentage
   of total portfolio value.

**Outcome:** Final position size in shares/units. All intermediate calculations
are logged.

---

### Gate 8 — Risk Setup

**Purpose:** Define the exit parameters for the position before order placement.
These parameters are fixed at entry and cannot be overridden without a logged
exception.

**Parameters set:**
- **Stop loss:** Entry price minus K × ATR (long positions). K is configurable.
- **Profit target:** Entry price plus reward multiple × stop distance.
- **Trailing stop:** Updated each session as max(price seen) minus K × ATR.

**Overnight risk assessment:**
- If the session is approaching close and the position would be held overnight,
  size may be reduced or entry skipped per configuration.

**Gap risk assessment:**
- If historical gap standard deviation for this instrument exceeds threshold,
  stops are widened or entry is skipped.

**Outcome:** Risk parameters (stop level, target level, trailing stop level)
attached to the trade record.

---

### Gate 9 — Execution

**Purpose:** Place the order and confirm fill. Handle execution exceptions.

**Slippage check:** If fill price deviates from expected price beyond the configured
tolerance, the order is cancelled or retried per configuration.

**Partial fill handling:** Defined behaviour for cases where only part of the order
fills. Options: accept partial fill, cancel remainder, retry.

**Time window validation:** Orders are only placed within the configured execution
time window. Orders outside this window are deferred or cancelled.

**Outcome:** ORDER PLACED with confirmed fill details, or ORDER FAILED with reason.
All execution events are logged.

---

### Gate 10 — Active Trade Management

**Purpose:** Monitor open positions every session and apply exit logic.

**Exit conditions evaluated each session:**
- **Stop loss hit:** Current price at or below stop level → immediate exit.
- **Profit target hit:** Current price at or above target → exit or partial exit
  per profit-taking rules.
- **Trailing stop triggered:** Current price at or below updated trailing stop → exit.
- **Signal reversal:** Original signal has flipped or confidence score has fallen
  below exit threshold → exit.
- **Benchmark underperformance:** Trade return minus benchmark return over holding
  period is below configured limit → exit.
- **Maximum holding time:** Position has been held beyond configured maximum → exit.

**Protective exit:** Cascade signals from Agent 3 (MarketSentimentAgent) that produce
an urgent flag on a held position trigger an immediate exit without per-trade approval.
This is a pre-authorized protective exit. The user is notified immediately.

**Outcome:** EXIT, PARTIAL EXIT, or HOLD for each open position. All decisions logged.

---

### Gate 11 — Portfolio-Level Controls

**Purpose:** Enforce portfolio-wide limits that supersede individual signal attractiveness.

**Controls enforced:**
- **Gross exposure cap:** Sum of all position values as a percentage of equity must not
  exceed the configured maximum. New entries blocked if cap would be breached.
- **Sector exposure limit:** No single sector may represent more than the configured
  maximum of total portfolio value.
- **Correlation spike protection:** If the mean pairwise correlation across positions
  exceeds threshold (indicating the portfolio has become concentrated in a single
  factor), new entries are blocked until correlation normalises.
- **Leverage limit:** Gross exposure divided by equity must not exceed configured
  maximum leverage.

**Outcome:** NEW ENTRIES PERMITTED or BLOCKED. Reason logged.

---

### Gate 12 — Adaptive Layer

**Purpose:** Adjust operating parameters when performance deteriorates, regime
changes, or model confidence drops. The system does not continue trading at
normal parameters when its own performance signals it should not.

**Adjustment triggers:**
- **Performance below threshold:** If rolling Sharpe ratio falls below minimum,
  risk per trade is reduced and entry frequency is lowered.
- **Parameter drift:** If optimal parameters have shifted materially from current
  settings, a recalibration flag is raised for human review.
- **Regime change:** If the regime classification changes between sessions, the
  applicable parameter set is switched automatically.

> **NOTE:** All adaptive parameter changes are logged. No parameter changes are
> made silently. Human review is flagged when thresholds are breached.

**Outcome:** Updated parameter set for current session, or FLAG FOR HUMAN REVIEW.

---

### Gate 13 — Stress Overrides

**Purpose:** Override all normal operating logic under extreme market conditions.
These overrides are pre-authorized and execute without approval queuing.

**Stress conditions and responses:**

| Condition | Trigger | Response |
|-----------|---------|----------|
| Flash crash | Price drop > X% within Y minutes | Force exit all OR set hedge ratio = 1 |
| Liquidity collapse | Spread explosion AND volume collapse | Halt all new entries |
| Benchmark crash | S&P 500 intraday drop > configured threshold | Force DEFENSIVE mode, de-risk |
| Forced de-risk | Any combination of above | Close all positions OR maximum hedge |

**Outcome:** Stress state recorded. All exits under stress override are logged
with the specific trigger condition.

---

### Gate 14 — Evaluation Loop

**Purpose:** Update performance metrics after each trade and session. Trigger
strategy-level risk reduction if metrics breach defined limits.

**Metrics updated:**
- Sharpe ratio (rolling window)
- Sortino ratio (rolling window)
- Maximum drawdown (running)
- Win/loss ratio
- Trade expectancy (average win × win rate − average loss × loss rate)
- Benchmark-relative performance (alpha over rolling window)

**Kill condition:** If Sharpe ratio falls below minimum AND drawdown exceeds maximum
allowed simultaneously, the strategy enters a suspended state. No new trades are
placed until human review clears the suspension.

**Outcome:** Updated metrics written to database. Kill condition checked. Suspension
flag set if applicable.

---

## 5. Audit Trail

Every execution of Agent 1 produces a structured trade decision log. This log is
written for each signal evaluated and each open position reviewed.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Session timestamp and session type
- Each gate's name, inputs evaluated, result (PASS/FAIL/VALUE), and reason code
- Final decision (MIRROR / WATCH / SKIP / EXIT / HOLD)
- Position sizing calculation with all intermediate values
- Risk parameters (stop, target, trailing stop)
- Execution outcome (order ID, fill price, fill quantity)

> **FLAG — LOG WRITE LOCATION:** Currently written to `system_log` table in
> `signals.db`. A dedicated `trade_decisions` table or separate log file is
> recommended to support regulatory export and volume management.
> Tracked as future work item.

---

## 6. Controls Not Yet Implemented (Data Dependencies)

The following controls are defined in the system specification but require
additional data integration before they can be activated:

| Control | Dependency | Status |
|---------|-----------|--------|
| VIX-based volatility regime | CBOE VIX data feed | TODO: DATA_DEPENDENCY |
| Credit spread monitoring | Credit spread data provider | TODO: DATA_DEPENDENCY |
| Automated event calendar | FOMC/CPI/earnings calendar API | TODO: DATA_DEPENDENCY |
| Cross-asset correlation matrix | Multi-asset price data | TODO: DATA_DEPENDENCY |
| Dark pool routing | Broker support required | TODO: DATA_DEPENDENCY |
| VWAP/TWAP execution | Intraday bar data | TODO: DATA_DEPENDENCY |
| Kelly criterion sizing | Requires stable win-rate history | TODO: FUTURE_WORK |
| Reinforcement learning adaptation | Training data volume required | TODO: FUTURE_WORK |

---

## 7. What Agent 1 Does Not Do

- Agent 1 does not use any AI language model to make trading decisions.
- Agent 1 does not access the internet directly.
- Agent 1 does not send communications directly. All alerts route through the
  company node notification pipeline (Scoop agent), except for the documented
  boot-time SMS exception in Agent boot_sequence.py.
- Agent 1 does not modify its own parameters without logging the change.
- Agent 1 does not execute live trades without explicit configuration of
  `TRADING_MODE=LIVE` by the project lead. Default mode is PAPER.

---

## 8. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| SUPERVISED mode (default) | Queue trade for portal approval | User approves or rejects each trade |
| Strategy kill condition triggered | Suspend new entries | Human review required to clear |
| Parameter drift flag raised | Log and alert | Human review and recalibration |
| Orphan/ghost position detected | Halt new entries, alert | Human reconciliation required |
| Stress override activated | De-risk / halt | Human review before resuming |
| TRADING_MODE = LIVE | (requires explicit set) | Project lead authorization required |
