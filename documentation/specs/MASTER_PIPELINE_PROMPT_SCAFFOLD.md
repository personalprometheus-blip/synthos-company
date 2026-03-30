# MASTER PIPELINE — IMPLEMENTATION PROMPT SCAFFOLD

| Field           | Value                                      |
|-----------------|--------------------------------------------|
| Document        | Master Pipeline Prompt Scaffold            |
| File            | MASTER_PIPELINE_PROMPT_SCAFFOLD.md        |
| Version         | 1.0                                        |
| Status          | DRAFT                                      |
| Date            | 2026-03-30                                 |
| Author          | Synthos Internal                           |
| Classification  | Governance — Internal Use Only             |

---

## Purpose

This scaffold provides prompt templates for each implementation task within `RUN_MASTER_PIPELINE`. Each template can be given to an implementation model (or developer) to produce a specific component. Every template includes: the task scope, the governing constraint, the interface contract, the rules the implementor must follow, and what must not be introduced.

---

## How to Use This Scaffold

1. Select the template for the component you are implementing.
2. Read the full context block before writing any code.
3. Do not add logic beyond what is stated in the rules section.
4. Where TODO:DATA_DEPENDENCY is marked, write a named stub — do not guess at data shapes.
5. After writing the component, run it against the acceptance tests section before submitting.

---

## Template 0 — Full Pipeline Orchestrator

```
TASK: Implement RUN_MASTER_PIPELINE, the top-level orchestration function.

GOVERNING CONSTRAINT:
Only release a trade when the market-state synthesis is usable, the trade logic
produces a valid decision, and post-decision audits do not reject, block, or
escalate the output.

SCOPE:
Implement the orchestration spine only. Do not implement any agent. Call agents
by their interface contracts. The pipeline sequences agents, enforces gate logic,
accumulates state, and controls order release. It does not make market judgements.

INTERFACE CONTRACT — INPUT:
raw_input: any (may be null)

INTERFACE CONTRACT — OUTPUT (must return this exact dict structure):
{
  "classification":      str,    # release | release_with_caution | reject | block | escalate | halt
  "health_state":        str,    # healthy | watch | degraded | failed
  "dispatch_output":     dict | null,
  "rumor_output":        dict | null,
  "news_output":         dict | null,
  "macro_output":        dict | null,
  "sentiment_output":    dict | null,
  "flow_output":         dict | null,
  "aggregator_output":   dict | null,
  "trade_output":        dict | null,
  "fault_output":        dict | null,
  "bias_output":         dict | null,
  "validator_output":    dict | null,
  "execution_state":     str,
  "release_action":      str
}

RULES:
1. All halt paths must return a structured HALT dict, not raise an exception.
2. Agents are called by name only. Do not inline agent logic.
3. All named constants (orchestration_max_age, orchestration_min_ready_blocks,
   high_health_threshold, medium_health_threshold) must be defined at module level.
4. STORE_RUN_RECORD must be called for all 11 agent outputs including null outputs.
5. No agent output is assumed; every agent return must be checked for null before use.
6. audit_fusion priority is strict: blocked > systemic_failure > failed > caution > approved.
7. If validator_required is true but validator_output is null, validator_state must be
   "audit_unavailable", not "clean".
8. Same null-guard rule applies to fault and bias audit outputs.

DO NOT:
- Write agent logic inside this function
- Hardcode any threshold values
- Skip STORE_RUN_RECORD for null outputs
- Return partial results from halt paths
- Add features not in the spec
```

---

## Template 1 — Intake Gate

```
TASK: Implement the intake gate (Section 1 of RUN_MASTER_PIPELINE).

SCOPE:
Validate raw input before dispatch. Five conditions halt the pipeline. One
condition sets a non-halting flag.

RULES:
1. Check conditions in this exact order:
   a. raw_input is null → HALT("reject_input", "raw input missing")
   b. PARSE(raw_input) returns null → HALT("reject_input", "parse failure")
   c. parsed_input.timestamp is null → HALT("reject_input", "timestamp missing")
   d. age > orchestration_max_age → HALT("stale_request", "input too old")
   e. HASH(parsed_input) in processed_request_store → HALT("suppress_duplicate", "duplicate request")
2. source_metadata null sets orchestration_flags.low_traceability = true. Non-halting.
3. Use SHA-256 for deduplication hashing. Hash the parsed input, not the raw bytes.
4. Age is computed as UTC now minus parsed_input.timestamp in minutes.

INTERFACE:
Input:  raw_input (any), processed_request_store (list of hash strings)
Output: (parsed_input dict, orchestration_flags dict) OR HALT dict

DO NOT:
- Halt on missing source_metadata (it is non-halting)
- Use wall clock for age calculation (use UTC)
- Add additional validation beyond what is specified
```

---

## Template 2 — Dispatcher Integration

```
TASK: Implement the dispatch section (Section 2 of RUN_MASTER_PIPELINE).

SCOPE:
Invoke DISPATCHER_AGENT and translate its output into orchestration_mode.

RULES:
1. DISPATCHER_AGENT receives parsed_input.
2. Two dispatch states halt: "invalid_route" → HALT("routing_failure", ...) and
   "cycle_break" → HALT("routing_halted", ...).
3. orchestration_mode mapping:
   - final_dispatch_signal == "fallback_or_triage" → "fallback"
   - final_dispatch_signal == "staged_route"       → "staged"
   - anything else                                 → "normal"
4. orchestration_mode is used by downstream routing logic. Pass it through.

INTERFACE:
Input:  parsed_input (dict)
Output: (dispatch_output dict, orchestration_mode str) OR HALT dict

DO NOT:
- Interpret the contents of parsed_input in this section
- Add routing logic beyond the mode mapping above
```

---

## Template 3 — Normalization

```
TASK: Implement the normalization section (Section 3 of RUN_MASTER_PIPELINE).

SCOPE:
Detect and normalize each of the six data type blocks independently.

RULES:
1. Each block follows the pattern: IF DETECT_X_FIELDS → normalized.x = NORMALIZE_X_DATA.
2. Detection must use field inspection only. Do not attempt normalization on absent fields.
3. Each normalizer returns a clean typed dict or null on failure. Treat each block as atomic.
4. REQUIRED_NORMALIZATION_FAILED checks whether the minimum required blocks are present.
   Required set: at minimum one of {market, macro, flow} AND at least one of {news, social}
   must succeed. This must be a named function, not inline logic.
5. If REQUIRED_NORMALIZATION_FAILED returns true → HALT("normalization_failure", ...).

INTERFACE:
Input:  parsed_input (dict)
Output: normalized dict with keys: market, macro, flow, news, social, result (any may be absent/null)

DO NOT:
- Merge normalization blocks
- Assume field names are stable across input sources
- Proceed past a required block failure
```

---

## Template 4 — Upstream Readiness Check

```
TASK: Implement the upstream readiness check (Section 4 of RUN_MASTER_PIPELINE).

SCOPE:
Evaluate whether each of the 7 upstream agent inputs has sufficient data to run.

RULES:
1. Readiness checks are: benchmark=BENCHMARK_READY(normalized.market),
   macro=MACRO_READY(normalized.macro), sentiment=SENTIMENT_READY(normalized.market),
   flow=FLOW_READY(normalized.flow), news=NEWS_READY(normalized.news),
   rumor=RUMOR_READY(normalized.social), validator=VALIDATOR_READY(normalized.result).
2. Each check is independent. A null normalized block must not cause an exception — it returns false.
3. ready_count = count of True values across all 7 checks.
4. If ready_count < orchestration_min_ready_blocks → HALT("insufficient_upstream_inputs", ...).

INTERFACE:
Input:  normalized (dict)
Output: (upstream_ready dict[str→bool], ready_count int) OR HALT dict

DO NOT:
- Short-circuit readiness checks
- Combine readiness checks into a single function
```

---

## Template 5 — Information Ingestion Stack

```
TASK: Implement the information ingestion stack (Section 5 of RUN_MASTER_PIPELINE).

SCOPE:
Run the social rumor agent and news agent in the correct sequence. Handle rumor
promotion and conflict flagging.

RULES:
1. SOCIAL_RUMOR_AGENT runs only if upstream_ready["rumor"] is true.
2. If rumor_output.classification == "upgraded_to_confirmed_event":
   a. Call PROMOTE_RUMOR_TO_NEWS_CONTEXT(rumor_output).
   b. If upstream_ready["news"]: NEWS_AGENT(MERGE(normalized.news, promoted_news_context)).
      MERGE field priority: normalized.news fields take precedence over promoted context
      for fields that exist in both. Promoted context fills gaps only.
   c. If NOT upstream_ready["news"]: NEWS_AGENT(promoted_news_context).
3. If rumor was NOT upgraded and upstream_ready["news"]: NEWS_AGENT(normalized.news).
4. BUILD_INFORMATION_CONTEXT(news_output, rumor_output) — either argument may be null.
5. If rumor_output.classification == "manipulation_watch":
   DISCOUNT_RUMOR_WEIGHT(information_flow_context).
6. If news_output is not null AND (news_output.classification == "freeze" OR
   news_output.confirmation_state == "contradictory"):
   information_flow_context.flags["conflicted"] = true.

INTERFACE:
Input:  upstream_ready (dict), normalized (dict)
Output: (rumor_output dict|null, news_output dict|null, information_flow_context dict|null)

DO NOT:
- Run NEWS_AGENT before SOCIAL_RUMOR_AGENT
- Run NEWS_AGENT if upstream_ready["news"] is false AND no promoted context exists
- Discard the manipulation_watch discount step
```

---

## Template 6 — Core Market Agents

```
TASK: Implement the core market agents section (Section 6 of RUN_MASTER_PIPELINE).

SCOPE:
Run macro regime, sentiment, flow, and benchmark agents with the correct inputs.
Check for minimum aggregation sufficiency.

RULES:
1. COMPUTE_BENCHMARK_ANCHOR(normalized.market) runs only if upstream_ready["benchmark"].
2. MACRO_REGIME_AGENT(macro_data=normalized.macro, benchmark_data=normalized.market)
   runs only if upstream_ready["macro"].
3. MARKET_SENTIMENT_AGENT(market_data=normalized.market, news_context=news_output,
   rumor_context=rumor_output) runs only if upstream_ready["sentiment"].
4. POSITIONING_FLOW_AGENT(flow_data=normalized.flow, market_data=normalized.market)
   runs only if upstream_ready["flow"].
5. core_ready = (macro_output != null AND sentiment_output != null AND flow_output != null).
6. If NOT core_ready AND information_flow_context is null:
   HALT("aggregation_insufficient", "no usable market core or information flow").

INTERFACE:
Input:  upstream_ready, normalized, news_output, rumor_output, information_flow_context
Output: (benchmark_anchor, macro_output, sentiment_output, flow_output) OR HALT dict

DO NOT:
- Pass information_flow_context to MACRO_REGIME_AGENT (it uses raw macro data)
- Run agents when their upstream_ready flag is false
- Halt on partial core_ready if information_flow_context is non-null
```

---

## Template 7 — Master Market-State Aggregation

```
TASK: Implement the aggregation section (Section 7 of RUN_MASTER_PIPELINE).

SCOPE:
Invoke MASTER_MARKET_STATE_AGGREGATOR with all upstream outputs and handle two
halt conditions.

RULES:
1. MASTER_MARKET_STATE_AGGREGATOR receives all 7 named inputs. Pass null for missing outputs.
   validator_output MUST be null here. Do not pass the pipeline's validator output.
2. If aggregator_output is null → HALT("aggregation_failure", ...).
3. If aggregator_output.final_market_state == "blocked_override" →
   HALT("suppress_trade_decision", "aggregator blocked downstream trade logic").
4. No other aggregator output value is halting in this section.

INTERFACE:
Input:  macro_output, sentiment_output, flow_output, news_output, rumor_output,
        benchmark_anchor
Output: aggregator_output dict OR HALT dict

DO NOT:
- Pass validator_output to the aggregator
- Halt on panic_override or deleveraging_override (those proceed to trade logic)
```

---

## Template 8 — Trade Logic Execution

```
TASK: Implement the trade logic section (Section 8 of RUN_MASTER_PIPELINE).

SCOPE:
Derive trade_bias from aggregator output and invoke TRADE_LOGIC_AGENT.

RULES:
1. trade_bias defaults to "neutral" before the mapping cascade.
2. Mapping (evaluated in order):
   - final_market_state IN {strong_risk_on, mild_risk_on}      → trade_bias = "pro_risk"
   - final_market_state == neutral                             → trade_bias = "neutral"
   - final_market_state IN {mild_risk_off, strong_risk_off}    → trade_bias = "defensive"
   - final_market_state == panic_override                      → trade_bias = "crisis_protocol"
   - final_market_state == deleveraging_override               → trade_bias = "forced_deleveraging_protocol"
3. TRADE_LOGIC_AGENT receives: market_state=aggregator_output, market_data=normalized.market,
   bias=trade_bias.
4. If trade_output is null → HALT("trade_logic_failure", ...).

INTERFACE:
Input:  aggregator_output, normalized.market
Output: (trade_output dict, trade_bias str) OR HALT dict

DO NOT:
- Pass raw market data as market_state (pass aggregator_output)
- Modify trade_bias after TRADE_LOGIC_AGENT runs
```

---

## Template 9 — Audit Stack

```
TASK: Implement the post-decision audit sections (Sections 9–13 of RUN_MASTER_PIPELINE).

SCOPE:
Run fault, bias, and validator audits. Handle specialist re-routing. All three
audits receive trade_output.

RULES — FAULT AUDIT:
1. fault_required is always true.
2. FAULT_AGENT(trade_output) always runs.
3. fault_state: null fault_output → "audit_unavailable"; block_output → "blocked";
   fail → "failed"; pass_with_warnings → "caution"; else → "clean".

RULES — BIAS AUDIT:
1. bias_required = FAIRNESS_SENSITIVE(trade_output). May be false.
2. BIAS_AGENT runs only if bias_required is true.
3. bias_state: null bias_output → "clean" IF NOT bias_required, else "audit_unavailable";
   block_output → "blocked"; fail_bias_audit → "failed";
   pass_with_bias_warning or fairness_review_recommended → "caution"; else → "clean".

RULES — VALIDATOR STACK:
1. validator_required is always true.
2. VALIDATOR_STACK_AGENT(trade_output) always runs.
3. validator_state: null output → "audit_unavailable"; block_output → "blocked";
   escalate_systemic_issue → "systemic_failure"; fail → "failed";
   review_recommended → "caution"; else → "clean".

RULES — SPECIALIST RE-ROUTING:
1. If validator_output.only_bias_lane_failed == true AND bias_output is null:
   run BIAS_AGENT and re-derive bias_state.
2. If validator_output.only_correctness_lane_failed == true AND fault_output is null:
   run FAULT_AGENT and re-derive fault_state.
3. Re-routing state derivation follows the same rules as the initial audit sections.

INTERFACE:
Input:  trade_output (dict)
Output: (fault_output, fault_state, bias_output, bias_state, validator_output, validator_state)

DO NOT:
- Map null audit output to "clean" when the audit was required
- Run specialist re-routing if the specialist output already exists
```

---

## Template 10 — Audit Fusion and Release

```
TASK: Implement audit fusion, execution eligibility, and order release
(Sections 14–16 of RUN_MASTER_PIPELINE).

SCOPE:
Combine three audit states into a single fusion verdict. Map to execution eligibility.
Map to order release action.

RULES — AUDIT FUSION (strict priority, first match wins):
1. "blocked" IN {fault_state, bias_state, validator_state}    → "block_trade_output"
2. validator_state == "systemic_failure"                      → "escalate_system_issue"
3. "failed" IN {fault_state, bias_state, validator_state}     → "reject_trade_output"
4. "caution" IN {fault_state, bias_state, validator_state}    → "approve_with_review_note"
5. else                                                       → "approved"
Note: "audit_unavailable" in any state must propagate as "caution" for fusion purposes.

RULES — EXECUTION ELIGIBILITY:
approved                 → eligible
approve_with_review_note → conditional_eligible
reject_trade_output      → ineligible
block_trade_output       → blocked
escalate_system_issue    → escalate
else                     → blocked

RULES — ORDER RELEASE:
eligible             → RELEASE_ORDER(trade_output)
conditional_eligible → RELEASE_ORDER_WITH_CAUTION(trade_output)
ineligible           → SUPPRESS_ORDER_RELEASE(trade_output)
blocked              → BLOCK_ORDER_RELEASE(trade_output)
escalate             → TRIGGER_ESCALATION_PROTOCOL(trade_output)

INTERFACE:
Input:  fault_state, bias_state, validator_state, trade_output
Output: (audit_fusion_state, execution_state, release_action)

DO NOT:
- Introduce additional fusion conditions beyond the five defined
- Override the priority order
- Skip order release for any execution_state
```

---

## Template 11 — Health Score

```
TASK: Implement CALC_HEALTH_SCORE and its helper functions (Section 19).

SCOPE:
Compute a numeric orchestration health score from 8 named inputs.
Map to a categorical health state.

FORMULA (to be finalized — implement as placeholder with TODOs):
orchestration_health_score = weighted sum of:
  - dispatch_quality        (weight: 0.15) — dispatch_output.dispatch_score, normalized to [0,1]
  - upstream_availability   (weight: 0.20) — ready_count / 7
  - aggregation_confidence  (weight: 0.25) — aggregator_output.aggregate_confidence
  - audit_cleanliness       (weight: 0.20) — AUDIT_SCORE(fault_state, bias_state, validator_state)
  - execution_eligibility   (weight: 0.10) — EXECUTION_SCORE(execution_state)
  - warning_load            (weight: 0.05) — 1 - (warning_count / max_warning_count)
  - validation_risk         (weight: 0.03) — VALIDATION_RISK_SCORE(validator_state)
  - route_instability       (weight: 0.02) — 1 - ROUTE_INSTABILITY_SCORE(dispatch_output)
Note: Weights are a placeholder. Mark with TODO:CALIBRATION_REQUIRED.

HELPER FUNCTION SPECS:
AUDIT_SCORE({fault_state, bias_state, validator_state}) → float [0, 1]:
  clean=1.0, caution=0.6, failed=0.2, blocked=0.0, systemic_failure=0.0, audit_unavailable=0.4

EXECUTION_SCORE(execution_state) → float [0, 1]:
  eligible=1.0, conditional_eligible=0.7, ineligible=0.2, blocked=0.0, escalate=0.0

VALIDATION_RISK_SCORE(validator_state) → float [0, 1]:
  clean=1.0, caution=0.6, failed=0.2, blocked=0.0, systemic_failure=0.0, audit_unavailable=0.5

ROUTE_INSTABILITY_SCORE(dispatch_output) → float [0, 1]:
  0.0 = fully stable (normal mode, no warnings), 1.0 = maximally unstable
  TODO:DATA_DEPENDENCY — requires dispatch_output instability field definition

HEALTH STATE MAPPING:
  If orchestration_classification IN {block, halt, escalate} → "failed" (override)
  Elif health_score >= high_health_threshold  → "healthy"
  Elif health_score >= medium_health_threshold → "watch"
  Else                                        → "degraded"

RULES:
1. All weights must sum to 1.0.
2. All helper functions return values in [0.0, 1.0].
3. Null inputs to helper functions must be handled — return 0.5 for unknown states.
4. Mark formula weights with TODO:CALIBRATION_REQUIRED.

DO NOT:
- Use ML or statistical models for health scoring
- Include fields not in the 8 named inputs
```

---

## Template 12 — Feedback Records and Final Return

```
TASK: Implement feedback record storage and final return (Sections 17–20).

SCOPE:
Store all agent outputs. Compute final classification. Return complete pipeline result.

RULES — FEEDBACK:
1. STORE_RUN_RECORD must be called for all 11 agents in this order:
   dispatcher, rumor, news, macro, sentiment, flow, aggregator,
   trade_logic, fault, bias, validator.
2. Null outputs must be stored as null, not skipped.
3. STORE_RUN_RECORD is called after release_action is determined.

RULES — FINAL CLASSIFICATION (1:1 from execution_state):
  eligible             → "release"
  conditional_eligible → "release_with_caution"
  ineligible           → "reject"
  blocked              → "block"
  escalate             → "escalate"
  else                 → "halt"

RULES — RETURN:
Return the 15-field dict exactly as specified. No field may be omitted.
All null agent outputs must appear as explicit null values in the dict.
orchestration_classification and health_state must never be null in a non-halting return.
Include orchestration_flags in the return dict.

INTERFACE:
Input:  all agent outputs, execution_state, release_action, orchestration_classification,
        orchestration_health_state, orchestration_flags
Output: complete pipeline result dict (15+ fields)
```

---

## Global Implementation Rules (Apply to All Templates)

These rules apply to every component implemented from this scaffold:

1. **No ML or AI inference.** All logic is rule-based and deterministic.
2. **No hardcoded thresholds.** All numeric values must be named constants.
3. **No silent failures.** Every exception path must produce a structured result, not raise.
4. **No extra features.** Implement exactly what is specified. No convenience wrappers, no pre-emptive abstractions.
5. **All TODO markers must be preserved.** Do not remove TODO:DATA_DEPENDENCY or TODO:CALIBRATION_REQUIRED comments.
6. **Decision log pattern.** Every gate decision must write a record with: gate name, inputs, result, reason_code, and UTC timestamp.
7. **Path bootstrap.** All files must use `synthos_paths` for path resolution and `db_helpers` for all database writes.
8. **No direct JSON writes.** All suggestion writes go via `db_helpers.post_suggestion()`.
