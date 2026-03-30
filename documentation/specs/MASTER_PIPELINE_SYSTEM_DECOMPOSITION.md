# MASTER PIPELINE — SYSTEM DECOMPOSITION MAP

| Field           | Value                                      |
|-----------------|--------------------------------------------|
| Document        | Master Pipeline System Decomposition Map   |
| File            | MASTER_PIPELINE_SYSTEM_DECOMPOSITION.md   |
| Version         | 1.0                                        |
| Status          | DRAFT                                      |
| Date            | 2026-03-30                                 |
| Author          | Synthos Internal                           |
| Classification  | Governance — Internal Use Only             |

---

## 1. Pipeline Overview

`RUN_MASTER_PIPELINE` is a 20-section linear orchestration spine. It is not an agent. It sequences agents, enforces gate logic, accumulates state, and controls trade release. Every agent in the system is either a direct child of this pipeline or feeds into one.

```
raw_input
    │
    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 1   Intake / Basic Gates                                   │
│              parse → timestamp → age → dedup → traceability flag    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ parsed_input
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 2   Dispatch                                               │
│              DISPATCHER_AGENT → orchestration_mode                  │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ dispatch_output, orchestration_mode
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 3   Normalization                                          │
│              detect + normalize: market, macro, flow,               │
│              news, social, result                                    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ normalized{}
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 4   Upstream Readiness Check                               │
│              benchmark, macro, sentiment, flow,                     │
│              news, rumor, validator                                  │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ upstream_ready{}, ready_count
              ┌────────────────────┴──────────────────────┐
              │                                           │
              ▼                                           ▼
┌─────────────────────────┐               ┌──────────────────────────┐
│  SECTION 5              │               │  SECTION 6               │
│  Information            │               │  Core Market Agents      │
│  Ingestion Stack        │               │                          │
│                         │               │  COMPUTE_BENCHMARK_ANCHOR│
│  SOCIAL_RUMOR_AGENT     │               │  MACRO_REGIME_AGENT      │
│      │ promote?         │               │  MARKET_SENTIMENT_AGENT  │
│      ▼                  │               │  POSITIONING_FLOW_AGENT  │
│  NEWS_AGENT             │               │                          │
│      │                  │               │                          │
│  BUILD_INFO_CONTEXT     │               │                          │
└────────────┬────────────┘               └──────────────┬───────────┘
             │ information_flow_context                   │ core market outputs
             └─────────────────┬──────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 7   Master Market-State Aggregation                        │
│              MASTER_MARKET_STATE_AGGREGATOR                         │
│              validator_output = null (always at this stage)         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ aggregator_output
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECTION 8   Trade Logic Execution                                  │
│              trade_bias ← final_market_state mapping                │
│              TRADE_LOGIC_AGENT                                       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ trade_output
              ┌────────────────┴───────────────────┐
              │                │                   │
              ▼                ▼                   ▼
    FAULT_AGENT         BIAS_AGENT         VALIDATOR_STACK_AGENT
   (Section 10)       (Section 11)           (Section 12)
   fault_state         bias_state           validator_state
              │                │                   │
              └────────────────┴─────────┬─────────┘
                                         │
                                         ▼ specialist re-routing (Section 13)
                               ┌──────────────────────┐
                               │  SECTION 14          │
                               │  Audit Fusion        │
                               │  audit_fusion_state  │
                               └──────────┬───────────┘
                                          ▼
                               ┌──────────────────────┐
                               │  SECTION 15          │
                               │  Execution           │
                               │  Eligibility         │
                               │  execution_state     │
                               └──────────┬───────────┘
                                          ▼
                               ┌──────────────────────┐
                               │  SECTION 16          │
                               │  Order Release       │
                               │  release_action      │
                               └──────────┬───────────┘
                                          ▼
                               ┌──────────────────────┐
                               │  SECTION 17          │
                               │  Feedback Records    │
                               │  STORE_RUN_RECORD×11 │
                               └──────────┬───────────┘
                                          ▼
                              Sections 18–20: classification,
                              health score, return dict
```

---

## 2. Agent Inventory

| # | Agent / Component              | Type             | Status               | File (target)                     |
|---|-------------------------------|------------------|----------------------|-----------------------------------|
| 0 | RUN_MASTER_PIPELINE           | Orchestrator     | NOT YET BUILT        | pipeline/master_pipeline.py       |
| 1 | DISPATCHER_AGENT              | Router           | NOT YET BUILT        | agents/dispatcher_agent.py        |
| 2 | SOCIAL_RUMOR_AGENT            | Agent 4          | EXISTS               | agents/social_rumor_agent.py      |
| 3 | NEWS_AGENT                    | Agent 2          | EXISTS               | agents/research_agent.py          |
| 4 | MACRO_REGIME_AGENT            | Agent 8          | EXISTS               | agents/macro_regime_agent.py      |
| 5 | MARKET_SENTIMENT_AGENT        | Future           | TODO:DATA_DEPENDENCY | agents/market_sentiment_agent.py  |
| 6 | POSITIONING_FLOW_AGENT        | Future           | TODO:DATA_DEPENDENCY | agents/positioning_flow_agent.py  |
| 7 | COMPUTE_BENCHMARK_ANCHOR      | Utility          | TODO:DATA_DEPENDENCY | pipeline/benchmark_anchor.py      |
| 8 | MASTER_MARKET_STATE_AGGREGATOR| Agent 9          | EXISTS               | agents/market_state_aggregator.py |
| 9 | TRADE_LOGIC_AGENT             | Future           | TODO:DATA_DEPENDENCY | agents/trade_logic_agent.py       |
| 10| FAULT_AGENT                   | Future           | TODO:DATA_DEPENDENCY | agents/fault_agent.py             |
| 11| BIAS_AGENT                    | Future           | TODO:DATA_DEPENDENCY | agents/bias_agent.py              |
| 12| VALIDATOR_STACK_AGENT         | Agent 7          | EXISTS               | agents/audit_stack_agent.py       |

---

## 3. Data Flow Map

### 3.1 Inputs to Each Agent

| Agent                        | Inputs                                                                   |
|------------------------------|--------------------------------------------------------------------------|
| DISPATCHER_AGENT             | parsed_input                                                             |
| SOCIAL_RUMOR_AGENT           | normalized.social                                                        |
| NEWS_AGENT                   | normalized.news, promoted_news_context (optional)                        |
| MACRO_REGIME_AGENT           | normalized.macro, normalized.market (benchmark data)                     |
| MARKET_SENTIMENT_AGENT       | normalized.market, news_output (optional), rumor_output (optional)       |
| POSITIONING_FLOW_AGENT       | normalized.flow, normalized.market                                       |
| COMPUTE_BENCHMARK_ANCHOR     | normalized.market                                                        |
| MASTER_MARKET_STATE_AGGREGATOR | macro_output, sentiment_output, flow_output, news_output, rumor_output, benchmark_anchor, validator_output=null |
| TRADE_LOGIC_AGENT            | aggregator_output, normalized.market, trade_bias                         |
| FAULT_AGENT                  | trade_output                                                             |
| BIAS_AGENT                   | trade_output                                                             |
| VALIDATOR_STACK_AGENT        | trade_output                                                             |

### 3.2 Outputs from Each Agent (Key Fields)

| Agent                        | Key Output Fields                                                        |
|------------------------------|--------------------------------------------------------------------------|
| DISPATCHER_AGENT             | dispatch_state, final_dispatch_signal, dispatch_score                    |
| SOCIAL_RUMOR_AGENT           | classification, overall_confidence, confirmation_state                   |
| NEWS_AGENT                   | classification, overall_confidence, confirmation_state                   |
| MACRO_REGIME_AGENT           | final_macro_state, macro_confidence, warning_states                      |
| MARKET_SENTIMENT_AGENT       | final_market_state, sentiment_confidence, warning_state                  |
| POSITIONING_FLOW_AGENT       | final_flow_state, flow_confidence, warning_state                         |
| COMPUTE_BENCHMARK_ANCHOR     | benchmark_state, benchmark_risk_state, benchmark_vol_state, benchmark_momentum_state |
| MASTER_MARKET_STATE_AGGREGATOR | final_market_state, aggregate_confidence, warning_states, aggregate_market_score |
| TRADE_LOGIC_AGENT            | trade_decision, trade_parameters, decision_rationale                     |
| FAULT_AGENT                  | classification (block_output, fail, pass_with_warnings, pass)            |
| BIAS_AGENT                   | classification (block_output, fail_bias_audit, pass_with_bias_warning, fairness_review_recommended, pass) |
| VALIDATOR_STACK_AGENT        | master_classification, only_bias_lane_failed, only_correctness_lane_failed |

---

## 4. Execution Paths

### 4.1 Happy Path (Full Data, Clean Audit)

```
raw_input
→ parsed_input (valid, fresh, unique)
→ dispatch_output (valid_route, normal mode)
→ normalized {market, macro, flow, news, social, result}
→ upstream_ready {all true}, ready_count = 7
→ rumor_output (valid classification)
→ news_output (bullish_signal or similar)
→ information_flow_context (not conflicted)
→ benchmark_anchor (bullish, normal vol)
→ macro_output (mild_expansion or similar)
→ sentiment_output (mildly_bullish)
→ flow_output (mildly_supportive)
→ aggregator_output (mild_risk_on, high confidence)
→ trade_bias = "pro_risk"
→ trade_output (valid decision)
→ fault_state = "clean"
→ bias_state = "clean" (or bias not required)
→ validator_state = "clean"
→ audit_fusion_state = "approved"
→ execution_state = "eligible"
→ release_action = RELEASE_ORDER
→ classification = "release"
→ health_state = "healthy"
```

### 4.2 Audit Block Path

```
...
→ trade_output (valid decision)
→ fault_state = "blocked"   ← FAULT_AGENT returned block_output
→ audit_fusion_state = "block_trade_output"
→ execution_state = "blocked"
→ release_action = BLOCK_ORDER_RELEASE
→ classification = "block"
→ health_state = "failed"   ← override: block triggers failed health
```

### 4.3 Minimal Data Path (Only Macro + News Available)

```
...
→ upstream_ready {macro=true, news=true, others=false}, ready_count = 2
→ rumor_output = null (rumor not ready)
→ news_output = (valid)
→ information_flow_context = (news only, no rumor)
→ macro_output = (valid)
→ sentiment_output = null (not ready)
→ flow_output = null (not ready)
→ core_ready = false
→ information_flow_context != null → pipeline does NOT halt
→ aggregator_output (low confidence, many inactive slots)
→ trade_bias derived from aggregator
→ ... (continues to audit path)
→ health_state likely "watch" or "degraded" due to low upstream_availability
```

### 4.4 Aggregator Blocked Path

```
...
→ aggregator_output.final_market_state = "blocked_override"
→ HALT("suppress_trade_decision", "aggregator blocked downstream trade logic")
→ Pipeline terminates. No trade_output. No audit agents run.
```

### 4.5 Rumor Promotion Path

```
...
→ rumor_output.classification = "upgraded_to_confirmed_event"
→ promoted_news_context = PROMOTE_RUMOR_TO_NEWS_CONTEXT(rumor_output)
→ IF news ready: NEWS_AGENT(MERGE(normalized.news, promoted_news_context))
→ IF news not ready: NEWS_AGENT(promoted_news_context)
→ information_flow_context reflects promoted confirmation
```

### 4.6 Specialist Re-Routing Path

```
...
→ validator_output.only_bias_lane_failed = true
→ bias_output was null (bias not required initially)
→ BIAS_AGENT(trade_output) triggered by validator re-route
→ bias_state updated
→ audit_fusion re-evaluated with new bias_state
```

---

## 5. Halt Condition Map

| Section | Condition                                      | Halt Reason Code              |
|---------|------------------------------------------------|-------------------------------|
| 1       | raw_input is null                              | reject_input                  |
| 1       | parse failure                                  | reject_input                  |
| 1       | timestamp missing                              | reject_input                  |
| 1       | input too old                                  | stale_request                 |
| 1       | duplicate                                      | suppress_duplicate            |
| 2       | dispatch_state = invalid_route                 | routing_failure               |
| 2       | dispatch_state = cycle_break                   | routing_halted                |
| 3       | required normalization block failed            | normalization_failure         |
| 4       | ready_count < min                              | insufficient_upstream_inputs  |
| 6       | core not ready AND no information flow         | aggregation_insufficient      |
| 7       | aggregator_output is null                      | aggregation_failure           |
| 7       | aggregator blocked_override                    | suppress_trade_decision       |
| 8       | trade_output is null                           | trade_logic_failure           |

All halt paths: **pipeline terminates immediately with a structured HALT dict. No partial result is returned.**

---

## 6. State Variable Lifecycle

| Variable                   | Set In    | Read In              | Notes                                     |
|----------------------------|-----------|----------------------|-------------------------------------------|
| parsed_input               | §1        | §2, §3               |                                           |
| orchestration_flags        | §1        | §20 (return)         | low_traceability flag                     |
| dispatch_output            | §2        | §17, §19, §20        |                                           |
| orchestration_mode         | §2        | Routing context      |                                           |
| normalized                 | §3        | §4, §5, §6, §7, §8   |                                           |
| upstream_ready             | §4        | §5, §6               |                                           |
| ready_count                | §4        | §4 (halt), §19       |                                           |
| rumor_output               | §5        | §5, §6, §7, §17, §20 |                                           |
| news_output                | §5        | §5, §6, §7, §17, §20 |                                           |
| information_flow_context   | §5        | §6                   | Not in return dict — consider adding      |
| benchmark_anchor           | §6        | §7                   |                                           |
| macro_output               | §6        | §7, §17, §19, §20    |                                           |
| sentiment_output           | §6        | §7, §17, §19, §20    |                                           |
| flow_output                | §6        | §7, §17, §19, §20    |                                           |
| aggregator_output          | §7        | §8, §17, §19, §20    |                                           |
| trade_bias                 | §8        | §8                   | Consumed immediately, not stored          |
| trade_output               | §8        | §9–13, §15, §16, §17 |                                           |
| fault_output / fault_state | §10, §13  | §14, §17, §19, §20   |                                           |
| bias_output / bias_state   | §11, §13  | §14, §17, §19, §20   |                                           |
| validator_output / state   | §12, §13  | §14, §17, §19, §20   |                                           |
| audit_fusion_state         | §14       | §15                  |                                           |
| execution_state            | §15       | §16, §18, §19        |                                           |
| release_action             | §16       | §17, §20             |                                           |
| orchestration_classification | §18     | §19, §20             |                                           |
| orchestration_health_score | §19       | §19                  | Not in return dict — only health_state is |
| orchestration_health_state | §19       | §20                  |                                           |

---

## 7. Component Dependency Graph

```
DISPATCHER_AGENT
└── parsed_input (raw)

SOCIAL_RUMOR_AGENT
└── normalized.social

NEWS_AGENT
├── normalized.news
└── promoted_news_context (from SOCIAL_RUMOR_AGENT, optional)

MACRO_REGIME_AGENT
├── normalized.macro
└── normalized.market (SPX benchmark data)

MARKET_SENTIMENT_AGENT
├── normalized.market
├── news_output (optional)
└── rumor_output (optional)

POSITIONING_FLOW_AGENT
├── normalized.flow
└── normalized.market

COMPUTE_BENCHMARK_ANCHOR
└── normalized.market

MASTER_MARKET_STATE_AGGREGATOR
├── macro_output
├── sentiment_output
├── flow_output
├── news_output
├── rumor_output
├── benchmark_anchor
└── validator_output = null (pipeline enforced)

TRADE_LOGIC_AGENT
├── aggregator_output (market_state)
├── normalized.market
└── trade_bias (derived from aggregator_output.final_market_state)

FAULT_AGENT
└── trade_output

BIAS_AGENT
└── trade_output (conditional)

VALIDATOR_STACK_AGENT
└── trade_output
```

---

## 8. Normalization Block Map

| Normalized Block  | Detection Function       | Normalizer                | Downstream Consumers                          |
|-------------------|--------------------------|---------------------------|-----------------------------------------------|
| normalized.market | DETECT_MARKET_FIELDS     | NORMALIZE_MARKET_DATA     | benchmark, macro, sentiment, flow, trade_logic|
| normalized.macro  | DETECT_MACRO_FIELDS      | NORMALIZE_MACRO_DATA      | MACRO_REGIME_AGENT                            |
| normalized.flow   | DETECT_FLOW_FIELDS       | NORMALIZE_FLOW_DATA       | POSITIONING_FLOW_AGENT                        |
| normalized.news   | DETECT_NEWS_FIELDS       | NORMALIZE_NEWS_DATA       | NEWS_AGENT                                    |
| normalized.social | DETECT_SOCIAL_FIELDS     | NORMALIZE_SOCIAL_DATA     | SOCIAL_RUMOR_AGENT                            |
| normalized.result | DETECT_RESULT_FIELDS     | NORMALIZE_RESULT_DATA     | VALIDATOR_STACK_AGENT (readiness check only)  |

---

## 9. Audit Fusion Truth Table

| fault_state | bias_state | validator_state    | audit_fusion_state         |
|-------------|------------|--------------------|----------------------------|
| blocked     | any        | any                | block_trade_output         |
| any         | blocked    | any                | block_trade_output         |
| any         | any        | blocked            | block_trade_output         |
| clean       | clean      | systemic_failure   | escalate_system_issue      |
| failed      | clean      | clean              | reject_trade_output        |
| clean       | failed     | clean              | reject_trade_output        |
| clean       | clean      | failed             | reject_trade_output        |
| caution     | clean      | clean              | approve_with_review_note   |
| clean       | caution    | clean              | approve_with_review_note   |
| clean       | clean      | caution            | approve_with_review_note   |
| clean       | clean      | clean              | approved                   |

Note: `audit_unavailable` propagates as `caution` in fusion logic.

---

## 10. Release Classification Cross-Reference

| audit_fusion_state         | execution_state      | release_action                   | orchestration_classification |
|----------------------------|----------------------|----------------------------------|------------------------------|
| approved                   | eligible             | RELEASE_ORDER                    | release                      |
| approve_with_review_note   | conditional_eligible | RELEASE_ORDER_WITH_CAUTION       | release_with_caution         |
| reject_trade_output        | ineligible           | SUPPRESS_ORDER_RELEASE           | reject                       |
| block_trade_output         | blocked              | BLOCK_ORDER_RELEASE              | block                        |
| escalate_system_issue      | escalate             | TRIGGER_ESCALATION_PROTOCOL      | escalate                     |
| (any other)                | blocked              | BLOCK_ORDER_RELEASE              | halt                         |

---

## 11. Open Items from Logic Audit

The following items are flagged as gaps in the logic audit (MASTER_PIPELINE_LOGIC_AUDIT.md) and must be resolved before implementation:

| Gap   | Item                                                                  | Affects                        |
|-------|-----------------------------------------------------------------------|--------------------------------|
| XS-01 | Null audit output when required=true → must not produce `clean`       | Templates 9, 10                |
| XS-02 | DISPATCHER_AGENT must expose dispatch_score                           | Section 2, Section 19          |
| XS-03 | Agent 7 must expose only_bias_lane_failed / only_correctness_lane_failed | Section 13                  |
| XS-04 | MERGE field priority for promoted rumor + news must be defined        | Template 5                     |
| XS-05 | Required normalization set must be documented                         | Template 3                     |
| XS-06 | FAIRNESS_SENSITIVE criteria must be documented                        | Template 9                     |
| XS-07 | CALC_HEALTH_SCORE formula must be finalized                           | Template 11                    |
| XS-08 | orchestration_flags must appear in return dict                        | Template 12                    |
