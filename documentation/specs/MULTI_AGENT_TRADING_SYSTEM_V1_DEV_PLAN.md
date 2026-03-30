# Multi-Agent Trading System V1 Development Plan

| Field           | Value                                              |
|-----------------|----------------------------------------------------|
| Document        | Multi-Agent Trading System V1 Development Plan     |
| Version         | 1.0                                                |
| Status          | ACTIVE — CONTROLS IMPLEMENTATION                   |
| Date            | 2026-03-30                                         |
| Author          | Synthos Internal                                   |
| Classification  | Engineering — Internal Use Only                    |

---

## 1. Program Purpose

### What V1 Is

V1 is a deterministic, rule-based multi-agent system that takes a raw market input, routes it through a structured agent pipeline, produces a classified trade decision, subjects that decision to post-decision audit, and releases or suppresses the output based on audit results.

V1 is complete when all twelve components are individually unit-tested, integration-tested end-to-end, and the pipeline produces a traceable, deterministic output for any valid input.

### What V1 Is Not

- V1 does not learn, adapt, or modify its own thresholds at runtime.
- V1 does not execute orders against a live broker or exchange.
- V1 does not include a UI, dashboard, or alerting front-end.
- V1 does not include backtesting infrastructure.
- V1 does not include position sizing, portfolio optimization, or risk management beyond audit gating.

### Design Philosophy

**Deterministic.** The same input always produces the same output. No stochastic elements, no runtime inference, no sampling.

**Modular.** Each agent has a defined input contract, output contract, and internal logic boundary. No agent reaches into another agent's internals.

**Testable.** Every decision branch is reachable by a synthetic input. Every gate can be exercised in isolation without the full pipeline.

**Auditable.** Every decision is recorded with its inputs, result, and reason code. No decision is taken without a trace entry.

---

## 2. System Inventory

| # | Agent                          | Role                                                                                              | Upstream Dependencies                                     | Downstream Dependencies                          |
|---|-------------------------------|---------------------------------------------------------------------------------------------------|-----------------------------------------------------------|--------------------------------------------------|
| 1 | Dispatcher Agent               | Routes parsed input to the correct pipeline mode (normal, staged, fallback). Detects invalid routes and cycles. | Raw parsed input                                         | All pipeline sections                            |
| 2 | News Agent                     | Classifies structured news data into a signal category (bullish, bearish, benchmark-regime, uncertain, ignore). | Normalized news data; optional promoted rumor context     | Master Market-State Aggregator, Market Sentiment Agent |
| 3 | Social / Rumor Agent           | Classifies social signal data for credibility, direction, and manipulation risk. Promotes confirmed events to news path. | Normalized social data                                    | News Agent (promotion), Master Market-State Aggregator |
| 4 | Market Sentiment Agent         | Classifies market sentiment from price/vol/flow data combined with news and rumor context.        | Normalized market data, News Agent output, Rumor Agent output | Master Market-State Aggregator                |
| 5 | Macro Regime Agent             | Classifies macroeconomic regime from multi-component economic data against SPX benchmark.         | Normalized macro data, normalized market data (SPX)       | Master Market-State Aggregator                   |
| 6 | Positioning / Flow Agent       | Classifies institutional positioning and flow data for supportive, fragile, or destabilizing conditions. | Normalized flow data, normalized market data              | Master Market-State Aggregator                   |
| 7 | Master Market-State Aggregator | Integrates all upstream agent outputs into a weighted composite market-state signal and final classification. | All upstream agent outputs (agents 2–6, benchmark anchor) | Trade Logic Agent                                |
| 8 | Trade Logic Agent              | Produces a trade decision from market state and trade bias. Does not touch raw data.              | Aggregator output, normalized market data, trade bias     | Fault Agent, Bias Agent, Validator Stack         |
| 9 | Fault Detection Agent          | Audits trade output for correctness violations: missing fields, invalid ranges, logic errors.     | Trade Logic Agent output                                  | Audit Fusion (Orchestration Layer)               |
| 10| Bias Detection Agent           | Audits trade output for fairness violations when the output is fairness-sensitive.                | Trade Logic Agent output (conditional)                    | Audit Fusion (Orchestration Layer)               |
| 11| Validator Stack                | Multi-lane audit across correctness, compliance, anomaly, bias, robustness, traceability.         | Trade Logic Agent output                                  | Audit Fusion, Specialist Re-routing (Orchestration Layer) |
| 12| Master Orchestration Layer     | Sequences all agents, enforces gate logic, accumulates state, controls trade release.             | Raw input                                                 | All agents; terminal release decision            |

---

## 3. V1 Completion Criteria

An agent is V1-ready when it satisfies all of the following:

### 3.1 Input Contract

- All accepted input fields are named and typed in a schema definition.
- All required fields are documented. Missing required fields must produce a defined error state, not an exception.
- All optional fields have documented handling when absent.
- The agent does not accept raw data types that belong to a different normalization stage.

### 3.2 Output Contract

- All output fields are named and typed.
- All possible output state values are enumerated. No undocumented output states.
- Output is a plain dict. No class instances, no lazy evaluation, no generators.
- Null outputs (agent failure) are distinguishable from empty/inactive outputs.

### 3.3 Config Separation

- All thresholds, weights, and named constants are defined at module level.
- No threshold value appears as a literal inside a gate function.
- All config values have a name that describes their purpose.
- Config is importable and inspectable without running the agent.

### 3.4 Traceability

- Every gate decision writes one record containing: gate number, gate name, inputs dict, result string, reason_code string, UTC timestamp.
- The decision log is included in the output dict under the key `decision_log`.
- The agent name and run ID are present in every log record or in the output root.

### 3.5 Deterministic Execution

- The agent produces the same output for the same input on every call.
- No randomness, no time-dependent logic outside of timestamp age checks, no external state reads during gate execution.
- All time-dependent checks use UTC and accept the timestamp as an input field, not from `datetime.now()` inside a gate.

### 3.6 Test Coverage

- Every defined output state has at least one unit test that exercises it directly.
- Every halt condition has a unit test confirming the halt fires and the correct reason code is produced.
- Every override condition (where a higher-priority rule replaces a lower-priority result) has a unit test confirming priority order.
- All tests pass with no external dependencies (no database, no network).

### 3.7 Integration Readiness

- The agent can be imported and called with a synthetic snapshot dict with no additional setup.
- The agent does not import or call any other agent.
- The agent does not write to the database during unit tests (db_helpers must be mockable).
- The agent's output dict keys are stable and match the aggregator's or orchestrator's expected field names exactly.

---

## 4. Global Build Standards

### 4.1 Input/Output Schema Requirements

Every agent must define its input and output schemas as module-level dicts or dataclasses. Schema must specify field name, type, required/optional, and valid value set or range. Schema definitions are the source of truth for integration contracts. They are not comments — they are code-level assertions.

### 4.2 Config Registry Requirements

Every agent has a module-level config block clearly separated from gate logic. Config block contains: all numeric thresholds, all weight values, all enumerated string constants used in gate comparisons, and all named defaults. Config block is the first non-import code in the file. Config values are UPPER_SNAKE_CASE. Gate functions reference config names, never literals.

### 4.3 No Hidden Thresholds

A threshold is hidden if it appears as a literal inside a conditional, as a default argument value, or inside a helper function that is not documented in the config block. Zero tolerance for hidden thresholds. Code review must verify config completeness before any agent is marked V1-ready.

### 4.4 No Cross-Agent Logic Leakage

An agent must not import another agent. An agent must not reproduce another agent's classification logic internally. An agent must not interpret another agent's internal state — it reads the other agent's output fields only. If an agent needs to re-classify an upstream result, it uses a local vocabulary translation table (a dict lookup), not conditional logic that mirrors the upstream agent's gates.

### 4.5 Deterministic Evaluation

Gate evaluation is a pure function: same inputs, same output, always. Gates do not read from files, databases, environment variables, or external services during evaluation. All external data is injected via the snapshot dict. If a gate needs a historical value, the caller supplies it.

### 4.6 Separation of Stages

Each agent has three distinct code sections:

1. **Config block** — all constants and thresholds.
2. **Gate functions** — one function per gate, named `gate_N_name`. Each gate reads from the decision log object and writes back to it. Gates do not call other gates directly.
3. **Pipeline orchestrator** — a single function (e.g., `run_agent(snapshot)`) that calls gates in sequence and returns the completed output dict.

No gate function may contain another gate's logic. No pipeline orchestrator may contain gate logic. The three sections are enforced by code structure, not convention.

---

## 5. Architecture Rules

The following rules are absolute. They may not be violated by any agent or by the orchestration layer.

**Rule A — Aggregator input boundary.**
The Master Market-State Aggregator consumes agent output dicts only. It does not accept raw market data, raw macro data, or normalized data blocks as direct inputs. All market interpretation is performed by upstream agents before reaching the aggregator.

**Rule B — Trade logic input boundary.**
The Trade Logic Agent consumes the aggregator output and normalized market data only. It does not accept outputs from any individual upstream agent (news, macro, sentiment, flow, rumor) directly. The aggregator is the sole synthesis layer between upstream agents and trade logic.

**Rule C — Validator does not modify decisions.**
The Validator Stack, Fault Agent, and Bias Agent are read-only consumers of trade output. They classify the output. They do not alter, supplement, or replace the trade decision. Their output feeds the audit fusion gate in the orchestration layer.

**Rule D — Dispatcher does not analyze.**
The Dispatcher Agent reads structural properties of the input (routing fields, type flags, presence of fields) to determine the pipeline route. It does not interpret market data, classify signals, or set market state. Any field set by the dispatcher is a routing or mode field, not a market classification.

**Rule E — Orchestration layer does not make market judgements.**
The Master Orchestration Layer sequences agents, enforces gates, and controls release. It does not classify market conditions, interpret agent outputs beyond defined state fields, or apply additional market-state logic. All market intelligence is encapsulated in agents.

**Rule F — Audit agents run on trade output, not upstream outputs.**
Fault, Bias, and Validator receive `trade_output` only. They do not receive aggregator output, raw data, or intermediate agent outputs. If audit logic requires market context, trade_output must carry that context.

**Rule G — Validator runs post-trade, never pre-aggregation.**
The orchestration layer passes `validator_output=null` to the Master Market-State Aggregator. The validator is a post-decision audit gate, not a market-state input.

**Rule H — All halt paths return structured output.**
No agent and no orchestration gate raises an unhandled exception as its failure mode. Every halt condition returns a structured dict with at minimum: `halted: true`, `halt_reason: <reason_code>`, and the partial decision log accumulated to that point.

**Rule I — State accumulates forward, never backward.**
A gate may read state set by earlier gates. A gate may not modify state set by an earlier gate. State flows in one direction: gate 1 → gate 2 → ... → gate N. Post-processing corrections are applied in a dedicated correction gate, not by retroactively modifying prior gate state.

**Rule J — No agent is optional at runtime.**
If an agent cannot run (insufficient data, upstream failure), it returns a defined inactive or unavailable output. The orchestration layer handles absent outputs via its upstream readiness check. Agents are not conditionally imported or skipped at the code level.

---

## 6. Agent Development Template

This template is applied to every agent in Section 7. All fields are required.

---

### AGENT DEVELOPMENT TEMPLATE

**Purpose**
One paragraph. What this agent classifies and why. What downstream system depends on its output.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| ...   | ...  | Yes/No   | ...         |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| ...   | ...  | ...             | ...         |

**Internal Stages**
Numbered list. One line per gate. Name only — do not describe logic.

**Precedence Rules**
List any priority-ordered condition sets where first match wins. Reference gate numbers.

**Config Requirements**
List all constants that must appear in the config block. Include type and description.

**Trace Requirements**
- Minimum fields per record: gate (int), name (str), inputs (dict), result (str), reason_code (str), ts (ISO UTC str).
- Every gate writes at least one record.
- Output dict includes `decision_log` key containing the full record list.

**Test Requirements**
- One test per enumerated output state.
- One test per halt condition (confirm reason code).
- One test per override/priority rule (confirm priority is enforced).
- All tests use synthetic snapshot dicts. No file I/O, no DB, no network.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| Required input missing | Halt with defined reason code |
| Input stale | Halt with defined reason code |
| All upstream slots inactive | Halt with defined reason code |
| Agent internal error | Return structured error dict, log exception |

**V1 Acceptance Checklist**
- [ ] Input schema documented and enforced
- [ ] Output schema documented; all states enumerated
- [ ] Config block complete; no hidden literals in gate functions
- [ ] Decision log written for every gate
- [ ] Every output state covered by at least one unit test
- [ ] Every halt condition covered by at least one unit test
- [ ] No cross-agent imports
- [ ] db_helpers mockable in tests
- [ ] Output dict keys verified against downstream consumer's expected field names
- [ ] Code review confirms no hidden thresholds

---

## 7. Per-Agent V1 Requirements

---

### 7.1 Dispatcher Agent

**Purpose**
Determines the pipeline route and orchestration mode from the parsed input's structural properties. Detects invalid routes and routing cycles before any agent runs.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| parsed_input | dict | Yes | Parsed, validated input payload |
| route_registry | dict | Yes | Valid routes and their modes |
| cycle_detection_store | list | Yes | Previously seen route hashes |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| dispatch_state | str | valid_route, invalid_route, cycle_break | Route validity result |
| final_dispatch_signal | str | normal_route, staged_route, fallback_or_triage | Mode assigned |
| dispatch_score | float | [0.0, 1.0] | Route quality score for health scoring |
| decision_log | list | — | Full gate trace |

**Internal Stages**
1. Gate 1 — Route extraction: read routing fields from parsed_input
2. Gate 2 — Route validation: check against route_registry
3. Gate 3 — Cycle detection: check route hash against cycle_detection_store
4. Gate 4 — Mode assignment: map valid route to orchestration_mode
5. Gate 5 — Score calculation: compute dispatch_score from route confidence

**Precedence Rules**
Gate 2: invalid_route halts before Gate 3 is evaluated.
Gate 3: cycle_break halts before Gate 4 is evaluated.

**Config Requirements**
- `CYCLE_HASH_WINDOW`: int — number of recent route hashes retained
- `DISPATCH_SCORE_NORMAL`: float — score for normal route
- `DISPATCH_SCORE_STAGED`: float — score for staged route
- `DISPATCH_SCORE_FALLBACK`: float — score for fallback route

**Trace Requirements**
Standard. Every gate writes one record minimum.

**Test Requirements**
Standard plus: test cycle detection with a route hash already in the store; test all three mode assignments.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| parsed_input null | Return halt dict, reason: reject_input |
| No routing fields present | dispatch_state = invalid_route, halt |
| Route hash in cycle store | dispatch_state = cycle_break, halt |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.2 News Agent

**Purpose**
Classifies structured news data into a signal category used by the Market Sentiment Agent and the Master Market-State Aggregator. Handles promoted rumor context as an optional supplement to normalized news data.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| normalized_news | dict | No | Normalized news fields |
| promoted_rumor_context | dict | No | Promoted rumor if upgraded_to_confirmed_event |
| run_id | str | Yes | Unique run identifier |
| timestamp | str | Yes | ISO UTC timestamp of the snapshot |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| classification | str | bullish_signal, relative_alpha_signal, bearish_signal, benchmark_regime_signal, watch_only, provisional_watch, freeze, ignore | Signal category |
| overall_confidence | float | [0.0, 1.0] | Confidence in classification |
| confirmation_state | str | confirmed, provisional, contradictory, unresolved | Corroboration status |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification. Gate names and count are established there.

**Precedence Rules**
Defined by the agent's external control logic specification.

**Config Requirements**
All numeric thresholds used in confidence scoring, signal classification, and confirmation state assignment must appear in the config block by name.

**Trace Requirements**
Standard.

**Test Requirements**
Standard plus: test promoted rumor context as sole input (no normalized_news); test MERGE behavior when both inputs are present; test contradictory confirmation_state.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| Both inputs null | Halt, reason: no_news_input |
| Timestamp missing | Halt, reason: reject_snapshot |
| Timestamp stale | Halt, reason: stale_snapshot |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.3 Social / Rumor Agent

**Purpose**
Classifies social signal data for direction and credibility. Identifies manipulation risk. Promotes confirmed events to the news path for re-processing by the News Agent.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| normalized_social | dict | Yes | Normalized social/rumor fields |
| run_id | str | Yes | Unique run identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| classification | str | bullish_rumor_signal, bearish_rumor_signal, relative_alpha_signal, manipulation_watch, benchmark_regime_signal, upgraded_to_confirmed_event, ignore | Signal category |
| overall_confidence | float | [0.0, 1.0] | Confidence in classification |
| confirmation_state | str | confirmed, provisional, contradictory, unresolved | Corroboration status |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Defined by the agent's external control logic specification.

**Config Requirements**
All thresholds for bot probability, source credibility, manipulation flags, and confirmation scoring must appear in the config block by name.

**Trace Requirements**
Standard.

**Test Requirements**
Standard plus: test upgraded_to_confirmed_event path; test manipulation_watch discount flag; test ignore classification with full input present.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| normalized_social null | Halt, reason: no_social_input |
| Timestamp stale | Halt, reason: stale_snapshot |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.4 Market Sentiment Agent

**Purpose**
Classifies market sentiment from price, volatility, and flow data combined with news and rumor context. Produces a directional sentiment state for the Master Market-State Aggregator.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| normalized_market | dict | Yes | Normalized market price/vol fields |
| news_output | dict | No | News Agent output dict |
| rumor_output | dict | No | Social Rumor Agent output dict |
| run_id | str | Yes | Unique run identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| final_market_state | str | strong_bullish, mild_bullish, neutral, mild_bearish, strong_bearish, panic_override, euphoric_warning_override | Sentiment classification |
| sentiment_confidence | float | [0.0, 1.0] | Confidence in classification |
| warning_state | list[str] | — | Active warning labels |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Defined by the agent's external control logic specification.

**Config Requirements**
All thresholds for panic detection, euphoria detection, volatility bands, and confidence scoring must appear in the config block by name.

**Trace Requirements**
Standard.

**Test Requirements**
Standard plus: test with news_output null and rumor_output null; test panic_override trigger; test euphoric_warning_override trigger.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| normalized_market null | Halt, reason: no_market_input |
| Timestamp stale | Halt, reason: stale_snapshot |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.5 Macro Regime Agent

**Purpose**
Classifies the macroeconomic regime across 10 component inputs (inflation, growth, labor, policy, yield curve, credit, liquidity, FX/global, commodity, macro news) benchmarked against the S&P 500. Applies overlay regime labels before score-based classification.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| normalized_macro | dict | Yes | Normalized macroeconomic data fields |
| normalized_market | dict | No | SPX benchmark data |
| snapshot_id | str | Yes | Snapshot identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |
| processed_snapshot_store | list | Yes | Previously processed hashes |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| final_macro_state | str | strong_expansion, mild_expansion, neutral, mild_contraction, strong_contraction, stagflation_override | Regime classification |
| macro_confidence | float | [0.0, 1.0] | Confidence score |
| macro_regime_score | float | [-1.0, 1.0] | Weighted composite regime score |
| warning_states | list[str] | — | Active divergence warnings |
| final_macro_signal | float | [-1.0, 1.0] | Confidence-adjusted final signal |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Gates 1–23 as defined in the agent's external control logic specification (agents/macro_regime_agent.py).

**Precedence Rules**
Gate 16 overlay priority: stagflation → reflation → disinflationary_slowdown → score-based.
Gate 23 override: stagflation_warning classification forces final_macro_state = stagflation_override before score-based state.

**Config Requirements**
All thresholds per component gate and all base weights must be in the config block. Existing implementation is the reference.

**Trace Requirements**
Standard. Existing implementation satisfies this.

**Test Requirements**
Standard plus: test each overlay (stagflation, reflation, disinflationary_slowdown) fires before score-based path; test each escalation condition triggers post_suggestion call.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| macro_data_status != online | Halt, reason: halt_regime_calc |
| Insufficient macro inputs | Halt, reason: insufficient_inputs |
| Duplicate snapshot | Halt, reason: suppress_duplicate |

**V1 Acceptance Checklist** — Standard template checklist. Existing implementation must be verified against checklist before integration, not assumed complete.

---

### 7.6 Positioning / Flow Agent

**Purpose**
Classifies institutional positioning and flow conditions as supportive, fragile, or destabilizing. Identifies squeeze and liquidation risk states for the Master Market-State Aggregator.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| normalized_flow | dict | Yes | Normalized positioning/flow data |
| normalized_market | dict | No | Market reference data |
| run_id | str | Yes | Unique run identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| final_flow_state | str | strong_supportive, mild_supportive, neutral, mild_fragile, strong_destabilizing, squeeze_override, liquidation_override | Flow classification |
| flow_confidence | float | [0.0, 1.0] | Confidence in classification |
| warning_state | list[str] | — | Active warning labels |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Defined by the agent's external control logic specification.

**Config Requirements**
All thresholds for squeeze detection, liquidation detection, net flow bands, and confidence scoring must appear in the config block by name.

**Trace Requirements**
Standard.

**Test Requirements**
Standard plus: test squeeze_override and liquidation_override paths; test strong_destabilizing with high warning load.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| normalized_flow null | Halt, reason: no_flow_input |
| Timestamp stale | Halt, reason: stale_snapshot |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.7 Master Market-State Aggregator

**Purpose**
Integrates outputs from up to seven upstream agents into a weighted composite market-state score. Applies dynamic weight adjustments, alignment checks, regime classification, override controls, and emits a final market-state signal with a downstream route.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| macro_agent_output | dict | No | Macro Regime Agent output |
| sentiment_agent_output | dict | No | Market Sentiment Agent output |
| flow_agent_output | dict | No | Positioning/Flow Agent output |
| news_agent_output | dict | No | News Agent output |
| rumor_agent_output | dict | No | Social Rumor Agent output |
| benchmark_state_output | dict | No | Benchmark anchor data |
| validator_output | dict | No | Must be null at this stage |
| snapshot_id | str | Yes | Snapshot identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |
| processed_snapshot_store | list | Yes | Previously processed hashes |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| final_market_state | str | strong_risk_on, mild_risk_on, neutral, mild_risk_off, strong_risk_off, panic_override, deleveraging_override, blocked_override | Market state |
| final_market_state_signal | float | [-1.0, 1.0] | Confidence-adjusted signal |
| aggregate_market_score | float | [-1.0, 1.0] | Weighted composite score |
| aggregate_confidence | float | [0.0, 1.0] | Aggregate confidence |
| market_regime_state | str | (7 states) | Named market regime |
| classification | str | (12 states) | Action classification |
| warning_states | list[str] | — | Active divergence warnings |
| downstream_route | str | — | Bias instruction for Trade Logic Agent |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Gates 1–26 as defined in the agent's external control logic specification (agents/market_state_aggregator.py).

**Precedence Rules**
Gate 19 override priority: validation_blocked → systemic_failure → drawdown+panic → liquidation+high_vol → low_trust_info.
Gate 25 override classification: panic_alert → deleveraging_alert → suppress_or_escalate take priority over score-based final state.

**Config Requirements**
All scoring tables, base weights, upshift multipliers, and signal thresholds must be in the config block. Existing implementation is the reference.

**Trace Requirements**
Standard. Existing implementation satisfies this.

**Test Requirements**
Standard plus: confirm validator_output=null is enforced (passing a non-null validator_output must have no effect on pre-Gate-19 state); test each Gate-19 override fires in priority order; test blocked_override halts the orchestration layer.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| Zero upstream agents active | Halt, reason: halt_aggregation |
| Fewer than MIN_REQUIRED_AGENTS | Halt, reason: insufficient_inputs |
| Duplicate snapshot | Halt, reason: suppress_duplicate |

**V1 Acceptance Checklist** — Standard template checklist. Existing implementation must be verified against checklist before integration.

---

### 7.8 Trade Logic Agent

**Purpose**
Produces a classified trade decision from the aggregator's market state and the assigned trade bias. Operates exclusively on post-synthesis state — does not re-interpret raw or upstream agent data.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| market_state | dict | Yes | Master Market-State Aggregator output |
| market_data | dict | Yes | Normalized market data |
| bias | str | Yes | Trade bias from orchestration layer |
| run_id | str | Yes | Unique run identifier |
| timestamp | str | Yes | ISO UTC snapshot timestamp |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| trade_decision | str | long, short, hold, exit, no_trade | Trade direction |
| trade_parameters | dict | — | Entry, sizing context, duration class |
| decision_rationale | list[str] | — | Ordered list of reason codes |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Defined by the agent's external control logic specification.

**Config Requirements**
All bias-to-decision mapping tables and all parameter derivation thresholds must appear in the config block by name.

**Trace Requirements**
Standard plus: trade_parameters and decision_rationale must be traceable to specific gate decisions. Every parameter must have a reason code.

**Test Requirements**
Standard plus: test each trade_bias value produces the correct decision class; test null trade_output path (what causes it and how it is structured).

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| market_state null | Return null trade_output (triggers orchestration halt) |
| bias unrecognized | Default to hold with reason: unrecognized_bias |
| Timestamp stale | Halt, reason: stale_snapshot |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.9 Fault Detection Agent

**Purpose**
Audits trade output for correctness violations: structural errors, missing required fields, value range violations, and internal logic inconsistencies. Always runs. Does not modify the trade decision.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| trade_output | dict | Yes | Trade Logic Agent output |
| run_id | str | Yes | Unique run identifier |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| classification | str | block_output, fail, pass_with_warnings, pass | Fault audit result |
| fault_list | list[str] | — | Identified fault codes |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Classification priority: block_output > fail > pass_with_warnings > pass.

**Config Requirements**
All field requirement rules and value range bounds must appear in the config block by name.

**Trace Requirements**
Standard plus: each identified fault must produce a distinct log record with the fault code as reason_code.

**Test Requirements**
Standard plus: test pass with a clean trade_output; test each individual fault code in isolation; test the block_output path.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| trade_output null | Return classification=block_output, fault: null_trade_output |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.10 Bias Detection Agent

**Purpose**
Audits trade output for fairness violations when the output is classified as fairness-sensitive. Runs conditionally. Does not modify the trade decision.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| trade_output | dict | Yes | Trade Logic Agent output |
| run_id | str | Yes | Unique run identifier |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| classification | str | block_output, fail_bias_audit, pass_with_bias_warning, fairness_review_recommended, pass | Bias audit result |
| bias_findings | list[str] | — | Identified bias codes |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Defined by the agent's external control logic specification.

**Precedence Rules**
Classification priority: block_output > fail_bias_audit > pass_with_bias_warning = fairness_review_recommended > pass.

**Config Requirements**
All fairness criteria, bias detection thresholds, and protected dimension definitions must appear in the config block by name.

**Trace Requirements**
Standard plus: each bias finding must produce a distinct log record.

**Test Requirements**
Standard plus: test pass when not fairness-sensitive; test each bias code in isolation; test block_output path.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| trade_output null | Return classification=block_output, bias_finding: null_trade_output |

**V1 Acceptance Checklist** — Standard template checklist.

---

### 7.11 Validator Stack

**Purpose**
Multi-lane audit of trade output across correctness, compliance, anomaly, bias, robustness, and traceability lanes. Aggregates lane results into a master classification. Identifies lane-specific failures for specialist re-routing.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| trade_output | dict | Yes | Trade Logic Agent output |
| run_id | str | Yes | Unique run identifier |
| active_lanes | list[str] | No | Override active lane set (default: 5 lanes) |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| master_classification | str | pass, review_recommended, fail, block_output, escalate_systemic_issue | Stack-level verdict |
| final_stack_signal | str | clean, warning, faulty, blocked, systemic_failure | Signal category |
| only_bias_lane_failed | bool | true, false | For specialist re-routing |
| only_correctness_lane_failed | bool | true, false | For specialist re-routing |
| lane_states | dict | per-lane state | Each lane's individual result |
| decision_log | list | — | Full gate trace |

**Internal Stages**
Sections 1–18 as defined in the agent's external control logic specification (agents/audit_stack_agent.py).

**Precedence Rules**
Master aggregation priority: block > escalate > fail > warning > pass.
Lane classification priority: systemic_count > 0 → escalate; critical_count > 0 → block; score ≥ 0.50 → fail; score ≥ 0.25 → warning; else → pass.

**Config Requirements**
All fault severity weights, aggregation thresholds, and lane weights must be in the config block. Existing implementation is the reference.

**Trace Requirements**
Standard. Existing implementation satisfies this.

**Test Requirements**
Standard plus: test only_bias_lane_failed is true when exactly one lane fails and it is the bias lane; test only_correctness_lane_failed equivalently; test systemic escalation path; test all six lanes individually.

**Failure Modes**
| Condition | Behavior |
|-----------|----------|
| trade_output null | master_classification = block_output, fault: null_submission |

**V1 Acceptance Checklist** — Standard template checklist. Existing implementation must be verified against checklist before integration.

---

### 7.12 Master Orchestration Layer

**Purpose**
Sequences all agents in the defined order, enforces intake and gate logic, accumulates run state, controls audit fusion, and emits the final release classification and health state.

**Inputs**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| raw_input | any | Yes | Raw unprocessed input |
| processed_request_store | list | Yes | Previously seen request hashes |
| orchestration_config | dict | Yes | All named constants for the orchestration layer |

**Outputs**
| Field | Type | Possible Values | Description |
|-------|------|-----------------|-------------|
| classification | str | release, release_with_caution, reject, block, escalate, halt | Final pipeline verdict |
| health_state | str | healthy, watch, degraded, failed | Pipeline health |
| execution_state | str | eligible, conditional_eligible, ineligible, blocked, escalate | Pre-release state |
| release_action | str | RELEASE_ORDER, RELEASE_ORDER_WITH_CAUTION, SUPPRESS_ORDER_RELEASE, BLOCK_ORDER_RELEASE, TRIGGER_ESCALATION_PROTOCOL | Action taken |
| All 11 agent outputs | dict\|null | — | Full output of every agent run |
| orchestration_flags | dict | — | low_traceability and similar flags |

**Internal Stages**
Sections 1–20 as defined in MASTER_PIPELINE_REQUIREMENTS.md.

**Precedence Rules**
Audit fusion priority: blocked (any) > systemic_failure (validator) > failed (any) > caution (any) > approved.
Health state override: classification in {block, halt, escalate} forces health_state = failed.

**Config Requirements**
- `ORCHESTRATION_MAX_AGE`: int (minutes)
- `ORCHESTRATION_MIN_READY_BLOCKS`: int
- `HIGH_HEALTH_THRESHOLD`: float
- `MEDIUM_HEALTH_THRESHOLD`: float

**Trace Requirements**
Standard plus: every section boundary produces a trace record. STORE_RUN_RECORD is called for all 11 agent outputs including nulls.

**Test Requirements**
Standard plus: test each halt condition in Section 1; test audit fusion priority for all combinations of blocked/failed/caution states; test health state override on block/halt/escalate classification; test specialist re-routing paths.

**Failure Modes**
Per MASTER_PIPELINE_REQUIREMENTS.md halt condition map (14 defined halt paths). All halt paths return structured output.

**V1 Acceptance Checklist** — Standard template checklist plus: all 8 logic audit gaps (XS-01 through XS-08 in MASTER_PIPELINE_LOGIC_AUDIT.md) resolved before V1 sign-off.

---

## 8. Integration Order

Build and integrate agents in the following order. Each agent is unit-tested and marked V1-ready before the next is started.

| Phase | Agent(s)                          | Rationale |
|-------|-----------------------------------|-----------|
| 1     | Dispatcher Agent                  | Gate 1 of the orchestration layer. All subsequent pipeline integration depends on a working dispatcher. No agent dependencies. |
| 2     | News Agent                        | No dependency on other agents. Earliest information path component. Required by Sentiment Agent and Aggregator. |
| 3     | Social / Rumor Agent              | No dependency on other agents. Output feeds the News Agent promotion path. Must be V1-ready before News Agent integration can be tested with promotion. |
| 4     | News + Rumor integration test     | Verify promotion path: rumor upgraded_to_confirmed_event → NEWS_AGENT receives promoted context. Verify MERGE field priority. |
| 5     | Macro Regime Agent                | Existing implementation. Verify against V1 checklist. No dependency on information agents. Feeds aggregator. |
| 6     | Market Sentiment Agent            | Depends on News Agent and Rumor Agent outputs (as optional context). Must be built after both are V1-ready. |
| 7     | Positioning / Flow Agent          | No dependency on information agents or macro agent. Feeds aggregator only. |
| 8     | Master Market-State Aggregator    | Existing implementation. Depends on all upstream agents. Verify against V1 checklist only after all upstream agents are V1-ready. |
| 9     | Trade Logic Agent                 | Depends exclusively on aggregator output. Can begin implementation after aggregator is V1-ready. |
| 10    | Fault Detection Agent             | Depends on trade output only. No upstream agent dependencies. |
| 11    | Bias Detection Agent              | Depends on trade output only. Can be built in parallel with Fault Agent. |
| 12    | Validator Stack                   | Existing implementation. Depends on trade output only. Verify against V1 checklist. Must expose only_bias_lane_failed and only_correctness_lane_failed before orchestration integration. |
| 13    | Master Orchestration Layer        | Built last. Depends on all agents. All 8 logic audit gaps (MASTER_PIPELINE_LOGIC_AUDIT.md) must be resolved before orchestration layer implementation begins. |
| 14    | End-to-end integration test       | Full pipeline run with synthetic snapshot. All 11 agent outputs verified in the return dict. Release path, block path, and halt path each exercised. |

---

## 9. Testing Strategy

### 9.1 Unit Testing

- Scope: single agent, single gate function, or single helper function.
- Environment: no database, no network, no file I/O. All dependencies injected via snapshot dict.
- db_helpers replaced with a mock that records calls and returns success.
- Each test is independent. No shared state between tests.
- Tests are located in `tests/unit/<agent_name>/`.

### 9.2 Branch Coverage

- Target: 100% of defined output states covered by at least one test.
- Target: 100% of halt conditions covered by at least one test.
- Target: 100% of override/priority rules covered by at least one test confirming priority order.
- Coverage is verified by inspection of the output state enumeration, not by a coverage tool alone. Every enumerated value must have a corresponding test name that is auditable.

### 9.3 Synthetic Scenario Testing

Synthetic scenarios test multi-gate interactions within a single agent. Defined scenarios:

| Scenario | Description |
|----------|-------------|
| happy_path | All inputs present, clean data, no warnings, expected clean output |
| minimal_input | Only required fields present, all optional absent |
| stale_timestamp | Input age exactly at threshold; input age one minute over threshold |
| all_inactive | All upstream slots inactive — confirms halt or inactive-state handling |
| override_priority | Conditions for each override met simultaneously — confirms only highest priority fires |
| manipulation_watch | Rumor flagged as manipulation — confirms discount is applied |
| rumor_promotion | Rumor upgraded_to_confirmed_event — confirms news agent receives promoted context |
| audit_block | Trade output structured to trigger block classification in each audit agent |
| specialist_reroute | Validator flags only_bias_lane_failed; confirm Bias Agent is triggered |

Each scenario must be defined as a named fixture. Fixtures are stored in `tests/fixtures/`.

### 9.4 Integration Testing

- Scope: two or more agents called in sequence with the real agent implementations (not mocks).
- Focus areas: output key names — the downstream consumer's expected field names must match exactly; null propagation — null outputs from one agent must be handled correctly by the next; promotion path — Social Rumor Agent → News Agent with promoted context.
- Integration tests are located in `tests/integration/`.
- Database writes are intercepted by a test db_helpers implementation that logs calls to an in-memory list.

### 9.5 Replay Testing

- Replay testing replays a stored pipeline run record through the current implementation and verifies that the output matches the stored output.
- Stored run records are produced by STORE_RUN_RECORD during any valid integration test run.
- Replay tests detect regressions caused by config changes or gate logic changes.
- A replay test failure requires a documented explanation before any code change is merged.
- Replay test records are stored in `tests/replay/`.

### 9.6 Audit Trace Validation

- Every integration test verifies that the decision_log field is present in the output.
- Every integration test verifies that the decision_log contains at least one record per gate.
- A subset of integration tests verify specific record content: that the reason_code for a known input matches the expected reason_code exactly.
- Audit trace validation tests are co-located with integration tests and named `test_trace_<scenario>.py`.

---

## 10. Configuration & Threshold Governance

### 10.1 Where Configs Live

Each agent's config block is located at the top of its implementation file, after imports, before class and function definitions. Config values are module-level constants in UPPER_SNAKE_CASE. No config values are stored in external files, environment variables, or databases for V1.

### 10.2 How Thresholds Are Changed

Changes to any threshold follow this procedure:

1. Identify the constant name and the agents that reference it.
2. Document the proposed new value, the reason for the change, and the expected effect on output states.
3. Run the full unit test suite for the affected agent.
4. Run replay tests for any stored scenarios that exercise the affected gate.
5. If replay tests fail: document the expected regression and update stored fixtures only after human review.
6. Record the change in the agent's change log (Section 11).

No threshold value is changed without completing all five steps.

### 10.3 Versioning Rules

- Agent implementation files are versioned by git commit. The commit message must reference the specific constant changed and the reason.
- Each agent file carries a module-level `AGENT_VERSION` string in `MAJOR.MINOR.PATCH` format.
- PATCH: threshold value change with no logic change.
- MINOR: new config constant added, or output state added.
- MAJOR: input contract or output contract changed.

### 10.4 Testing Requirements After Changes

| Change Type | Required Tests |
|-------------|----------------|
| Threshold value change | Unit tests for all gates that use the constant; replay tests for affected scenarios |
| New config constant | Unit tests for all gates that use the constant |
| Output state added | Unit test for the new state; update integration tests that enumerate output states |
| Input contract change | Full unit test suite; all integration tests involving the agent |
| Output contract change | Full unit test suite; all integration tests involving the agent; update all downstream agents' integration tests |

---

## 11. Change Log Framework

Each agent maintains a change log section in its governance document. Every change is recorded as a structured entry.

### Required Fields per Entry

| Field | Description |
|-------|-------------|
| Date | ISO date (YYYY-MM-DD) |
| Agent | Agent name |
| Version | New AGENT_VERSION after change |
| Change Type | threshold / logic / contract / config / test |
| Description | What changed, stated precisely (e.g., "Raised INFLATION_HIGH_THRESHOLD from 0.040 to 0.045") |
| Reason | Why the change was made |
| Affected Gates | Comma-separated list of gate numbers affected |
| Tests Run | List of test files or scenarios run to verify the change |
| Approved By | Name or role of person who approved the change |

### Change Log Format

```
## Change Log

### 2026-03-30 | v1.0.1 | macro_regime_agent
- Type: threshold
- Description: Raised INFLATION_HIGH_THRESHOLD from 0.040 to 0.045
- Reason: CPI data consistently triggering high-inflation classification at 4.1% during test validation; threshold misaligned with current regime definition
- Affected Gates: 4
- Tests Run: test_inflation_level_high, test_inflation_level_moderate, replay_stagflation_scenario
- Approved By: Engineering Lead
```

Change log entries are append-only. Existing entries are never modified or deleted.

---

## 12. Risk Register

| Risk | Description | Likelihood | Impact | Mitigation |
|------|-------------|------------|--------|------------|
| Conflicting upstream states | Two upstream agents emit contradictory classifications (e.g., macro=pro_growth, flow=liquidation_prone). The aggregator handles this via alignment gates, but unexpected combinations may produce unintuitive composite scores. | Medium | Medium | Test all pairwise conflict scenarios in Aggregator unit tests. Log alignment_state in every run. Review aggregate_state distribution in replay tests. |
| Threshold instability | A threshold set for one market regime degrades performance in another. Thresholds are static in V1; market regimes are not. | Medium | High | All thresholds named and documented. Replay testing detects regressions. Change procedure in Section 10 enforces human review before threshold changes merge. |
| Schema drift | An upstream agent's output key is renamed or removed. The downstream consumer silently reads null. | Low | High | Integration tests verify key-by-key that downstream consumers find expected fields. AGENT_VERSION MAJOR bump required for contract changes. |
| Hidden threshold reintroduction | A developer adds a literal value inside a gate function during a feature change, bypassing the config block. | Medium | Medium | Config block completeness is a V1 acceptance checklist item. Code review must verify no literals in gate functions before merge. |
| Audit bottleneck | All three post-trade audit agents (Fault, Bias, Validator) run sequentially. Under high load, audit latency dominates total run time. | Low for V1 | Low for V1 | V1 runs on-demand, not in a latency-sensitive path. Document as a known V2 concern. |
| Specialist re-routing loop | Validator triggers specialist re-routing for Bias Agent; Bias Agent result is still caution or failed; orchestration layer has no loop limit. | Low | Medium | Re-routing fires at most once per run per specialist (guarded by `bias_output == null` check). Confirm this guard in Orchestration Layer integration tests. |
| Null audit output masking fault | If a required audit agent produces null output (e.g., due to an internal exception), and null is mapped to `clean`, a faulty trade output may be released. | Low | High | Logic audit gap XS-01 — null audit output when required must map to `audit_unavailable`, not `clean`. Blocking gap; must be resolved before V1. |
| Overfitting pipeline logic to test data | Synthetic test scenarios become the implicit definition of correct behavior. Real inputs that differ structurally from test fixtures are mishandled. | Medium | Medium | Test fixtures must represent edge cases, not just expected cases. Replay tests must include runs from diverse input shapes. Fixture review is part of agent sign-off. |
| Config divergence across environments | Config constants are modified in a local environment and not propagated to the canonical implementation file. | Low | High | V1 has one canonical config per agent: the module-level config block. No environment-specific config files in V1. |

---

## 13. Immediate Next Steps

The current state is: Agents 7, 8, and 9 (Validator Stack, Macro Regime Agent, Aggregator) have implementations. A dispatcher design is absent. Four agents are unbuilt (Sentiment, Flow, Trade Logic, Fault, Bias). The orchestration layer is specced but unbuilt. Eight logic audit gaps are open.

Execute in this sequence:

**Step 1 — Resolve logic audit gaps (blocking).**
Work through gaps XS-01 through XS-07 in MASTER_PIPELINE_LOGIC_AUDIT.md before writing any new agent code. XS-01 (null audit safety) and XS-07 (health score formula) affect every agent integration. XS-03 (Validator Stack specialist re-routing flags) requires an Agent 7 output contract change — MINOR version bump.

**Step 2 — Audit existing implementations against V1 checklist.**
Apply the V1 acceptance checklist (Section 3) to agents/macro_regime_agent.py, agents/audit_stack_agent.py, and agents/market_state_aggregator.py before treating them as integration-ready. Do not assume existing code satisfies V1 criteria.

**Step 3 — Write first test cases against existing agents.**
Before building any new agent, write unit tests for Macro Regime Agent covering: stagflation overlay fires before score-based path; each escalation condition triggers post_suggestion; stale_snapshot halt; suppress_duplicate halt. These tests establish the test pattern used for all subsequent agents.

**Step 4 — Build Dispatcher Agent.**
Dispatcher is the entry point for the orchestration layer. No downstream integration test can run without it. Build it first among the unbuilt agents. Follow the development template exactly.

**Step 5 — Build News Agent and Social / Rumor Agent in parallel.**
These two have no inter-agent dependencies. Build them together. Write unit tests for each independently, then write the integration test for the promotion path (Step 4 in integration order).

**Step 6 — Build Market Sentiment Agent.**
After News and Rumor agents are V1-ready. Sentiment Agent depends on both as optional context inputs.

**Step 7 — Build Positioning / Flow Agent.**
No information agent dependencies. Can begin after Step 4.

**Step 8 — Verify Aggregator against V1 checklist.**
All upstream agents V1-ready at this point. Run Aggregator integration tests with all six upstream outputs populated.

**Step 9 — Build Trade Logic Agent.**
Aggregator must be V1-verified before Trade Logic Agent integration begins.

**Step 10 — Build Fault Detection Agent and Bias Detection Agent.**
Trade Logic Agent must be V1-ready. Both audit agents can be built in parallel.

**Step 11 — Verify Validator Stack against V1 checklist, add specialist re-routing flags.**
Requires Agent 7 output contract change (gap XS-03). MINOR version bump.

**Step 12 — Build Master Orchestration Layer.**
All agents V1-ready. All logic audit gaps resolved. End-to-end integration test is the final V1 gate.
