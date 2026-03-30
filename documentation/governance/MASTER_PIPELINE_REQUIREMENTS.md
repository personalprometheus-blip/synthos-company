# MASTER PIPELINE — REQUIREMENTS AND LOGIC SPECIFICATION

| Field           | Value                                      |
|-----------------|--------------------------------------------|
| Document        | Master Pipeline Requirements               |
| File            | MASTER_PIPELINE_REQUIREMENTS.md           |
| Version         | 1.0                                        |
| Status          | DRAFT — PENDING IMPLEMENTATION             |
| Date            | 2026-03-30                                 |
| Author          | Synthos Internal                           |
| Classification  | Governance — Internal Use Only             |
| Companion Spec  | MASTER_PIPELINE_LOGIC_AUDIT.md            |

---

## 1. Purpose

This document defines the formal requirements for `RUN_MASTER_PIPELINE`, the top-level orchestration function of the Synthos agent hierarchy. The pipeline takes a raw input, routes it through the full agent stack, and produces a final trade decision with an audit trail and release classification.

The pipeline is not an agent itself. It is the orchestration spine that sequences agents, enforces gate logic, accumulates state, and controls order release.

---

## 2. Governing Constraint

> **Only release a trade when the market-state synthesis is usable, the trade logic produces a valid decision, and post-decision audits do not reject, block, or escalate the output.**

All release eligibility logic must be traceable back to this constraint.

---

## 3. Functional Requirements

### 3.1 Intake and Basic Gates

| ID     | Requirement                                                                                 | Halting |
|--------|---------------------------------------------------------------------------------------------|---------|
| REQ-01 | If raw_input is null, halt with reason `reject_input`.                                      | Yes     |
| REQ-02 | If raw_input cannot be parsed, halt with reason `reject_input`.                             | Yes     |
| REQ-03 | If parsed_input.timestamp is null, halt with reason `reject_input`.                         | Yes     |
| REQ-04 | If input age exceeds `orchestration_max_age`, halt with reason `stale_request`.             | Yes     |
| REQ-05 | If SHA-256 hash of parsed_input is in `processed_request_store`, halt with `suppress_duplicate`. | Yes |
| REQ-06 | If source_metadata is null, set `orchestration_flags.low_traceability = true`. Non-halting. | No      |

### 3.2 Dispatch

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-07 | DISPATCHER_AGENT must be invoked on every parsed input that passes Section 1.                 | —       |
| REQ-08 | If dispatch_state == `invalid_route`, halt with reason `routing_failure`.                     | Yes     |
| REQ-09 | If dispatch_state == `cycle_break`, halt with reason `routing_halted`.                        | Yes     |
| REQ-10 | final_dispatch_signal must be translated into orchestration_mode: `fallback`, `staged`, or `normal`. | —  |

### 3.3 Normalization

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-11 | Each data type (market, macro, flow, news, social, result) must be detected and normalized independently. | — |
| REQ-12 | Detection must precede normalization; normalization must not be attempted on absent fields.   | —       |
| REQ-13 | If any required normalization block fails, halt with reason `normalization_failure`.          | Yes     |
| REQ-14 | Normalized blocks that are absent or failed must contribute a null slot, not a partial struct. | —      |

### 3.4 Upstream Readiness Check

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-15 | Readiness must be checked for all 7 upstream slots: benchmark, macro, sentiment, flow, news, rumor, validator. | — |
| REQ-16 | Each readiness check must be independent and non-halting.                                     | —       |
| REQ-17 | If ready_count < `orchestration_min_ready_blocks`, halt with `insufficient_upstream_inputs`.  | Yes     |
| REQ-18 | `orchestration_min_ready_blocks` must be a named constant, not a hardcoded literal.           | —       |

### 3.5 Information Ingestion Stack

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-19 | SOCIAL_RUMOR_AGENT must only run if upstream_ready["rumor"] is true.                          | —       |
| REQ-20 | If rumor_output.classification == `upgraded_to_confirmed_event`, promote to news context before NEWS_AGENT runs. | — |
| REQ-21 | NEWS_AGENT receives promoted_news_context merged with normalized.news if both are available.  | —       |
| REQ-22 | NEWS_AGENT receives promoted_news_context alone if normalized.news is unavailable.            | —       |
| REQ-23 | NEWS_AGENT runs from normalized.news alone if rumor was not promoted.                         | —       |
| REQ-24 | BUILD_INFORMATION_CONTEXT must be called with both news_output and rumor_output (either may be null). | — |
| REQ-25 | If rumor_output.classification == `manipulation_watch`, DISCOUNT_RUMOR_WEIGHT must be applied to information_flow_context. | — |
| REQ-26 | If news_output.classification == `freeze` OR news_output.confirmation_state == `contradictory`, set `information_flow_context.flags.conflicted = true`. | — |

### 3.6 Core Market Agents

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-27 | COMPUTE_BENCHMARK_ANCHOR must only run if upstream_ready["benchmark"] is true.                | —       |
| REQ-28 | MACRO_REGIME_AGENT must only run if upstream_ready["macro"] is true.                          | —       |
| REQ-29 | MARKET_SENTIMENT_AGENT must only run if upstream_ready["sentiment"] is true.                  | —       |
| REQ-30 | POSITIONING_FLOW_AGENT must only run if upstream_ready["flow"] is true.                       | —       |
| REQ-31 | MACRO_REGIME_AGENT receives both normalized.macro and normalized.market.                      | —       |
| REQ-32 | MARKET_SENTIMENT_AGENT receives normalized.market, news_output, and rumor_output.             | —       |
| REQ-33 | POSITIONING_FLOW_AGENT receives normalized.flow and normalized.market.                        | —       |
| REQ-34 | If core_ready is false AND information_flow_context is null, halt with `aggregation_insufficient`. | Yes |

### 3.7 Master Market-State Aggregation

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-35 | MASTER_MARKET_STATE_AGGREGATOR must always be invoked if Section 6 did not halt.              | —       |
| REQ-36 | validator_output must be null at this stage; validator runs post-trade, not pre-aggregation.  | —       |
| REQ-37 | If aggregator_output is null, halt with `aggregation_failure`.                                | Yes     |
| REQ-38 | If aggregator_output.final_market_state == `blocked_override`, halt with `suppress_trade_decision`. | Yes |

### 3.8 Trade Logic Execution

| ID     | Requirement                                                                                   | Halting |
|--------|-----------------------------------------------------------------------------------------------|---------|
| REQ-39 | trade_bias must be derived exclusively from aggregator_output.final_market_state using the defined mapping. | — |
| REQ-40 | Trade bias mapping must cover all valid final_market_state values.                            | —       |
| REQ-41 | TRADE_LOGIC_AGENT receives aggregator_output (market state), normalized.market, and trade_bias. | —     |
| REQ-42 | If trade_output is null, halt with `trade_logic_failure`.                                     | Yes     |

### 3.9 Post-Decision Audit Routing

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-43 | fault_required must always be true.                                                           |
| REQ-44 | bias_required must be computed from FAIRNESS_SENSITIVE(trade_output). It is not always true.  |
| REQ-45 | validator_required must always be true.                                                       |
| REQ-46 | All three audit agents run on trade_output, not on raw input or aggregator output.            |

### 3.10 Fault Audit

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-47 | FAULT_AGENT must run if fault_required is true.                                               |
| REQ-48 | fault_state must be one of: `blocked`, `failed`, `caution`, `clean`.                         |
| REQ-49 | fault_state derivation must follow classification priority: `block_output` → blocked, `fail` → failed, `pass_with_warnings` → caution, else → clean. |

### 3.11 Bias Audit

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-50 | BIAS_AGENT must run if bias_required is true.                                                 |
| REQ-51 | bias_state must be one of: `blocked`, `failed`, `caution`, `clean`.                          |
| REQ-52 | bias_state derivation: `block_output` → blocked, `fail_bias_audit` → failed, `pass_with_bias_warning` or `fairness_review_recommended` → caution, else → clean. |

### 3.12 Validator Stack

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-53 | VALIDATOR_STACK_AGENT must run if validator_required is true.                                 |
| REQ-54 | validator_state must be one of: `blocked`, `systemic_failure`, `failed`, `caution`, `clean`. |
| REQ-55 | validator_state derivation priority: `block_output` → blocked, `escalate_systemic_issue` → systemic_failure, `fail` → failed, `review_recommended` → caution, else → clean. |

### 3.13 Specialist Re-Routing from Validator

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-56 | If validator_output.only_bias_lane_failed == true AND bias_output is null, BIAS_AGENT must be run and bias_state updated. |
| REQ-57 | If validator_output.only_correctness_lane_failed == true AND fault_output is null, FAULT_AGENT must be run and fault_state updated. |
| REQ-58 | Re-routed specialist audit results follow the same state derivation rules as Sections 10–11.  |

### 3.14 Audit Fusion

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-59 | audit_fusion_state must be derived in strict priority order.                                  |
| REQ-60 | Priority order: `blocked` in any state → `block_trade_output`; `systemic_failure` in validator → `escalate_system_issue`; `failed` in any → `reject_trade_output`; `caution` in any → `approve_with_review_note`; else → `approved`. |
| REQ-61 | A blocked audit must never produce an approved or approve_with_review_note fusion state.      |

### 3.15 Execution Eligibility

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-62 | execution_state must map 1:1 from audit_fusion_state with no additional logic.               |
| REQ-63 | The mapping is: `approved` → eligible; `approve_with_review_note` → conditional_eligible; `reject_trade_output` → ineligible; `block_trade_output` → blocked; `escalate_system_issue` → escalate; else → blocked. |

### 3.16 Order Release

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-64 | release_action must be set for every execution_state. No execution_state may have a null release_action. |
| REQ-65 | Only `eligible` calls RELEASE_ORDER. Only `conditional_eligible` calls RELEASE_ORDER_WITH_CAUTION. |
| REQ-66 | `ineligible` calls SUPPRESS_ORDER_RELEASE. `blocked` calls BLOCK_ORDER_RELEASE. `escalate` calls TRIGGER_ESCALATION_PROTOCOL. |

### 3.17 Feedback Records

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-67 | STORE_RUN_RECORD must be called for all 11 agent outputs (null outputs must be stored as null, not skipped). |
| REQ-68 | STORE_RUN_RECORD must be called after release_action is determined, not before.               |

### 3.18 Final Classification

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-69 | orchestration_classification must map 1:1 from execution_state.                              |
| REQ-70 | The mapping is: eligible → `release`; conditional_eligible → `release_with_caution`; ineligible → `reject`; blocked → `block`; escalate → `escalate`; else → `halt`. |

### 3.19 Health Score

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-71 | CALC_HEALTH_SCORE must receive all 8 named inputs. No input may be silently dropped.          |
| REQ-72 | warning_load must aggregate warnings from all four sources: aggregator, macro, sentiment, flow (null sources contribute 0). |
| REQ-73 | If orchestration_classification is in {`block`, `halt`, `escalate`}, orchestration_health_state must be `failed` regardless of the numeric health score. |
| REQ-74 | orchestration_health_state thresholds must be named constants: `high_health_threshold` and `medium_health_threshold`. |

### 3.20 Return Contract

| ID     | Requirement                                                                                   |
|--------|-----------------------------------------------------------------------------------------------|
| REQ-75 | The return dict must include all 15 named fields defined in Section 20 of the spec.           |
| REQ-76 | Null outputs must be returned as explicit null values, not omitted keys.                      |
| REQ-77 | orchestration_classification and health_state must always be present and non-null in a non-halting return. |

---

## 4. Invariants

The following invariants must hold across every pipeline run:

| ID    | Invariant                                                                                                    |
|-------|--------------------------------------------------------------------------------------------------------------|
| INV-1 | No trade output is released unless execution_state ∈ {eligible, conditional_eligible}.                      |
| INV-2 | No trade output is released if any audit state is `blocked`.                                                 |
| INV-3 | No trade output is released if validator_state == `systemic_failure`.                                        |
| INV-4 | MASTER_MARKET_STATE_AGGREGATOR always receives validator_output = null. Validator runs post-trade only.      |
| INV-5 | All halt states terminate the pipeline with a structured HALT return. No partial result is returned.         |
| INV-6 | The pipeline never skips STORE_RUN_RECORD for any agent, including agents that returned null.                |
| INV-7 | Normalization blocks are always attempted before readiness checks. Readiness is a function of normalization output. |
| INV-8 | The pipeline is stateless between runs. All state is derived from the current snapshot and stored records.   |

---

## 5. Named Constants (Must Be Defined at Config Level)

| Constant                        | Used In    | Description                                    |
|---------------------------------|------------|------------------------------------------------|
| orchestration_max_age           | Section 1  | Maximum input age in minutes before rejection  |
| orchestration_min_ready_blocks  | Section 4  | Minimum ready upstream blocks before halt      |
| high_health_threshold           | Section 19 | Minimum score for `healthy` health state       |
| medium_health_threshold         | Section 19 | Minimum score for `watch` health state         |

---

## 6. Agent Dependency Summary

| Agent                        | Depends On                                           | Output Consumer(s)                         |
|------------------------------|------------------------------------------------------|--------------------------------------------|
| DISPATCHER_AGENT             | parsed_input                                         | orchestration_mode, all downstream         |
| SOCIAL_RUMOR_AGENT           | normalized.social                                    | NEWS_AGENT, information_flow_context       |
| NEWS_AGENT                   | normalized.news, rumor promotion context             | information_flow_context, MARKET_SENTIMENT_AGENT |
| MACRO_REGIME_AGENT           | normalized.macro, normalized.market                  | MASTER_MARKET_STATE_AGGREGATOR             |
| MARKET_SENTIMENT_AGENT       | normalized.market, news_output, rumor_output         | MASTER_MARKET_STATE_AGGREGATOR             |
| POSITIONING_FLOW_AGENT       | normalized.flow, normalized.market                   | MASTER_MARKET_STATE_AGGREGATOR             |
| COMPUTE_BENCHMARK_ANCHOR     | normalized.market                                    | MASTER_MARKET_STATE_AGGREGATOR             |
| MASTER_MARKET_STATE_AGGREGATOR | all upstream outputs                               | TRADE_LOGIC_AGENT, trade_bias              |
| TRADE_LOGIC_AGENT            | aggregator_output, normalized.market, trade_bias     | FAULT_AGENT, BIAS_AGENT, VALIDATOR_STACK_AGENT |
| FAULT_AGENT                  | trade_output                                         | audit_fusion                               |
| BIAS_AGENT                   | trade_output (conditional)                           | audit_fusion                               |
| VALIDATOR_STACK_AGENT        | trade_output                                         | audit_fusion, specialist re-routing        |

---

## 7. Data Flow Paths

**Information path:** social → SOCIAL_RUMOR_AGENT → (promote) → NEWS_AGENT → information_flow_context

**Core market path:** macro + market data → MACRO_REGIME_AGENT; market + news + rumor → MARKET_SENTIMENT_AGENT; flow + market → POSITIONING_FLOW_AGENT; market → COMPUTE_BENCHMARK_ANCHOR

**Fusion path:** information_flow_context + core market outputs → MASTER_MARKET_STATE_AGGREGATOR

**Decision path:** aggregator_output + trade_bias → TRADE_LOGIC_AGENT

**Audit path:** trade_output → FAULT_AGENT + BIAS_AGENT + VALIDATOR_STACK_AGENT → audit_fusion

**Control path:** audit_fusion → execution_state → release_action → STORE_RUN_RECORD → orchestration_classification

---

## 8. Out of Scope

The following are explicitly outside the scope of `RUN_MASTER_PIPELINE`:

- Internal gate logic of any individual agent (governed by each agent's own spec)
- Actual order routing to exchange or broker infrastructure
- Post-run calibration and model scoring (handled by agent evaluation loops)
- User authentication and session management
- Historical data retrieval (callers must supply normalized data)
