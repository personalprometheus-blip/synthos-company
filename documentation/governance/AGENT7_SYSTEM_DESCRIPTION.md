# AGENT 7 — AUDIT STACK AGENT: SYSTEM DESCRIPTION AND GOVERNANCE RECORD

| Field            | Value                                              |
|------------------|----------------------------------------------------|
| Agent Name       | Audit Stack Agent                                  |
| File             | agents/audit_stack_agent.py                        |
| Agent Number     | 7                                                  |
| Version          | 1.0                                                |
| Status           | PRODUCTION CANDIDATE                               |
| Date             | 2026-03-30                                         |
| Author           | Synthos Internal                                   |
| Classification   | Governance — Internal Use Only                     |

---

## Purpose and Scope

The Audit Stack Agent is a multi-lane result evaluation system. It accepts a result payload — any structured output produced by a Synthos agent or external system — and passes it through up to six independent audit lanes: **correctness**, **compliance**, **anomaly**, **bias**, **robustness**, and **traceability**. Each lane checks a specific class of failure. Faults from all active lanes are scored, classified, and combined into a single master classification and composite stack score.

This agent does not produce trading signals or operational instructions. It produces a structured audit verdict for human review, pipeline gating, or downstream suppression.

**Architecture note:** Unlike Agents 1–6 which run a sequential numbered gate spine, the Audit Stack Agent runs lanes in parallel and applies a fixed post-lane aggregation sequence (Sections 9–18). Sections 1–2 are intake controls. Sections 3–8 are the lane implementations. Sections 9–18 are aggregation, scoring, classification, root cause fusion, remediation routing, output control, feedback recording, and composite scoring.

All logic is rule-based and deterministic. No ML/AI inference is performed anywhere in this agent.

---

## Operational Schedule

The Audit Stack Agent is invoked on demand. It does not run on a cron schedule. Callers pass a `submission` dict containing the result payload, optional reference data, and optional lane selection. The agent returns a complete decision log.

---

## Section Overview

| Section | Name                       | Type          | Halting |
|---------|----------------------------|---------------|---------|
| 1       | Intake Router              | Control       | Yes     |
| 2       | Lane Activation Controls   | Control       | No      |
| 3       | Correctness Lane           | Lane          | No      |
| 4       | Compliance Lane            | Lane          | No      |
| 5       | Anomaly Lane               | Lane          | No      |
| 6       | Bias Lane                  | Lane          | No      |
| 7       | Robustness Lane            | Lane          | No      |
| 8       | Traceability Lane          | Lane          | No      |
| 9       | Cross-Lane Conflict Controls | Aggregation | No      |
| 10      | Lane Severity Controls     | Aggregation   | No      |
| 11      | Lane Scoring Controls      | Aggregation   | No      |
| 12      | Lane Classification Controls | Aggregation | No      |
| 13      | Master Aggregation Controls | Aggregation  | No      |
| 14      | Root Cause Fusion Controls | Aggregation   | No      |
| 15      | Remediation Routing Controls | Output      | No      |
| 16      | Output Controls            | Output        | No      |
| 17      | Feedback Loop Controls     | Feedback      | No      |
| 18      | Final Stack Composite Score | Output       | No      |

---

## Default Lane Activation

When no lanes are explicitly requested, the following five lanes activate by default:

| Lane          | Default Active |
|---------------|---------------|
| correctness   | Yes           |
| compliance    | Yes           |
| anomaly       | Yes           |
| bias          | Yes           |
| robustness    | Yes           |
| traceability  | No (opt-in)   |

The traceability lane must be explicitly included in `requested_lanes` to activate.

---

## Section-by-Section Description

### Section 1 — Intake Router

Section 1 is the only halting section. It performs two structural checks before any lane runs.

**Missing payload.** If `result_payload` is absent or null, the agent sets `stack_state = reject_input` and halts immediately. No lanes run.

**Schema validation.** If `result_payload` is present but fails structural validation — the payload must be a non-empty dict — the agent sets `stack_state = invalid_structure` and halts. No lanes run.

**Lane selection.** If the caller specifies `requested_lanes`, those lanes become the active set. If none are specified, the default set — correctness, compliance, anomaly, bias, robustness — activates.

**Reference data.** If `reference_payload` is present, `stack_state = reference_enabled`. If absent, `stack_state = reference_limited`. Reference data is optional. Its absence limits certain correctness checks but does not prevent the agent from running.

---

### Section 2 — Lane Activation Controls

Section 2 iterates all six lane names and sets each lane's state to `active` or `inactive` based on the active set determined in Section 1. Inactive lanes are not executed and contribute no faults or scores to the final result.

---

### Section 3 — Correctness Lane

The correctness lane checks whether the result value is accurate relative to expected or ground-truth values.

**Ground truth distance.** If both `result_value` and `reference_payload.ground_truth_value` are present, the lane computes `abs(result_value - ground_truth_value)`. If this distance exceeds `CORRECTNESS_TOLERANCE` (default 0.05), fault `incorrect_result` is recorded.

**Numeric deviation.** If `audit_context.expected_value` is provided, the lane checks `abs(result_value - expected_value)` against `NUMERIC_TOLERANCE` (default 0.01). Excess deviation records fault `numeric_error`.

**Label classification.** If `predicted_class` and `actual_class` are both present and differ, fault `misclassification` is recorded.

**Reconciliation.** If `reported_total` and `component_values` are both present, the lane checks whether `reported_total` equals the sum of component_values within numeric tolerance. Discrepancy records fault `reconciliation_error`.

**Chronology.** If `audit_context.temporal_order_constraint` is present and evaluates to false, fault `temporal_inconsistency` is recorded.

Each check is skipped individually if the required input fields are absent. Non-numeric values that cannot be compared are recorded as skipped with reason `non_numeric_values`.

---

### Section 4 — Compliance Lane

The compliance lane checks whether the result satisfies policy rules and regulatory obligations.

**Hard rule.** If `audit_context.hard_constraint_result` is present and false, fault `hard_rule_violation` is recorded. This is the compliance lane's most severe check.

**Prohibited content.** If `audit_context.content_policy_match` is true, fault `prohibited_output` is recorded.

**Disclaimer.** If `audit_context.disclaimer_required` is true and `audit_context.disclaimer_present` is false, fault `missing_disclaimer` is recorded.

**Audit trail.** If `audit_context.trace_log_completeness` is present and falls below `audit_context.trace_minimum` (default 0.80), fault `audit_trail_failure` is recorded.

**Restricted action.** If `audit_context.restricted_action_flag` is true, fault `restricted_recommendation` is recorded.

---

### Section 5 — Anomaly Lane

The anomaly lane checks whether the result exhibits statistical properties inconsistent with expected distributions.

**Statistical outlier.** If `audit_context.z_score` is present and its absolute value exceeds `OUTLIER_THRESHOLD` (default 3.0), fault `statistical_outlier` is recorded.

**Variance collapse.** If `audit_context.batch_variance` is present and falls below `MIN_VARIANCE_THRESHOLD` (default 1e-6), fault `suspicious_uniformity` is recorded.

**Distribution drift.** If `audit_context.distribution_distance` is present and exceeds `DRIFT_THRESHOLD` (default 0.10), fault `distribution_drift` is recorded.

**Mean shift.** If `audit_context.mean_shift` is present and its absolute value exceeds `MEAN_SHIFT_THRESHOLD` (default 0.05), fault `mean_shift` is recorded.

**Clustered failures.** If `audit_context.fault_density` is present and exceeds `CLUSTER_THRESHOLD` (default 0.40), fault `clustered_failure` is recorded.

Each anomaly check requires a pre-computed metric provided by the caller in `audit_context`. The agent does not compute distributions from raw data internally.

---

### Section 6 — Bias Lane

The bias lane checks whether the result exhibits discriminatory patterns across protected or demographic groups.

**Representation disparity.** If `audit_context.representation_gap` exceeds `REPRESENTATION_GAP_THRESHOLD` (default 0.10), fault `representation_bias` is recorded.

**Selection disparity.** If `audit_context.selection_probability_gap` exceeds `SELECTION_GAP_THRESHOLD` (default 0.10), fault `selection_bias` is recorded.

**Outcome disparity.** If `audit_context.group_error_rate_gap` is a list and its maximum value exceeds `FAIRNESS_ERROR_GAP_THRESHOLD` (default 0.05), fault `outcome_bias` is recorded.

**Proxy feature risk.** `audit_context.proxy_scores` is a dict mapping feature names to proxy risk scores. Any feature whose score exceeds `PROXY_THRESHOLD` (default 0.70) is recorded as a proxy fault. A single `proxy_bias` fault entry lists all offending features.

**Language bias.** If `audit_context.language_bias_score` exceeds `LANGUAGE_BIAS_THRESHOLD` (default 0.30), fault `language_bias` is recorded.

---

### Section 7 — Robustness Lane

The robustness lane checks whether the result would hold under variation in inputs or operating conditions.

**Reproducibility.** If `audit_context.same_input_repeat_variance` exceeds `REPRODUCIBILITY_THRESHOLD` (default 0.05), fault `non_reproducible` is recorded.

**Input sensitivity.** If `audit_context.sensitivity_metric` exceeds `INSTABILITY_THRESHOLD` (default 0.15), fault `unstable_result` is recorded.

**Boundary cases.** If `audit_context.boundary_case_test` equals the string `"fail"`, fault `boundary_failure` is recorded.

**Adversarial input.** If `audit_context.attack_case_success_rate` exceeds `ADVERSARIAL_THRESHOLD` (default 0.10), fault `adversarial_vulnerability` is recorded.

**Brittle dependency.** If `audit_context.optional_input_removed` is true and `audit_context.output_invalid` is also true, fault `brittle_dependency` is recorded.

---

### Section 8 — Traceability Lane

The traceability lane checks whether the result's sources and claims are documented, fresh, and internally consistent.

**Missing provenance.** If `result_payload.source_provenance` is null, fault `missing_provenance` is recorded.

**Source freshness.** If `audit_context.source_age_hours` exceeds `FRESHNESS_LIMIT_HOURS` (default 24), fault `stale_source` is recorded.

**Missing citation.** If `audit_context.external_claim_count` exceeds `audit_context.cited_claim_count`, fault `missing_citation` is recorded for the gap.

**Unsupported citation.** `audit_context.citation_support_scores` is a list of per-citation support scores. Any score below `SUPPORT_THRESHOLD` (default 0.60) counts as an unsupported citation. If any unsupported citations exist, fault `unsupported_citation` is recorded.

**Source conflicts.** If `audit_context.source_conflict_count` is greater than zero and `audit_context.resolution_note` is null, fault `unresolved_source_conflict` is recorded. A non-null resolution_note indicates the conflict was documented and resolved.

---

### Section 9 — Cross-Lane Conflict Controls

Section 9 examines the combination of active lane fault counts to detect compound failure patterns. It operates on raw fault counts before scoring and classification run.

**Suspicious pass.** If the correctness lane has no faults but the anomaly lane has faults, flag `suspicious_pass` is raised. A result that passes correctness checks but triggers anomaly checks may have a correct value that is nonetheless inconsistent with historical distributions.

**Compound critical issue.** If both the correctness lane and the compliance lane have faults, flag `compound_critical_issue` is raised. The result is simultaneously factually and policy-non-compliant.

**Fairness-specific failure.** If the bias lane has faults but the compliance lane has none, flag `fairness_specific_failure` is raised. The result may be policy-compliant under current rules but still exhibits measurable disparity.

**Instability pattern.** If both the robustness lane and the anomaly lane have faults, flag `instability_pattern` is raised. The result is statistically abnormal and also non-reproducible or fragile.

**Unverifiable correctness.** If the traceability lane has faults and `audit_context.correctness_confidence` is below `CONFIDENCE_THRESHOLD` (default 0.60), flag `unverifiable_correctness` is raised. The result cannot be verified because its sources are incomplete and correctness confidence is low.

Cross-lane flags are recorded on the decision log but do not independently alter master classification. They are available for human review and downstream analysis.

---

### Section 10 — Lane Severity Controls

Section 10 assigns a severity level to each fault in every active lane. Severity is determined by a three-level priority:

1. **Harm risk or policy breach override.** If `audit_context.harm_risk` exceeds `HARM_THRESHOLD` (default 0.70) or `audit_context.policy_breach` is true, all faults in the current run are overridden to `critical`.

2. **Default scope-based severity.** If no override applies, each fault receives the default severity mapped in the `FAULT_SCOPE` catalogue. Scope categories and their default severities are:

| Impact Scope              | Default Severity |
|---------------------------|-----------------|
| presentation_only         | low             |
| explanation_or_visibility | medium or high  |
| decision_output           | high or critical|

3. **Systemic repeat override.** If `audit_context.repeat_lane_fault_counts[fault_type]` exceeds `SYSTEMIC_THRESHOLD` (default 1), that fault is reclassified to `systemic`.

---

### Section 11 — Lane Scoring Controls

Section 11 computes a normalized score for each active lane. The score is the sum of severity weights for all faults detected in that lane, capped at 1.0.

Severity weights used in scoring:

| Severity  | Weight |
|-----------|--------|
| critical  | 1.00   |
| systemic  | 1.00   |
| high      | 0.70   |
| medium    | 0.40   |
| low       | 0.15   |

A lane with no faults receives a score of 0.0. A lane with one critical fault scores 1.0 (capped). A lane with two medium faults scores 0.80. Scores are rounded to four decimal places.

---

### Section 12 — Lane Classification Controls

Section 12 assigns a state to each active lane based on its score and fault composition. Priority order (highest first):

1. **escalate** — one or more systemic faults present (`systemic_count > 0`)
2. **block** — one or more critical faults present (`critical_count > 0`)
3. **fail** — lane score ≥ `LANE_FAIL_THRESHOLD` (default 0.50)
4. **warning** — lane score ≥ `LANE_WARNING_THRESHOLD` (default 0.25) and < fail threshold
5. **pass** — lane score < warning threshold

Overrides (escalate, block) take precedence over score thresholds. A lane with two medium faults (score 0.80) and no critical faults classifies as `fail`, not `block`.

---

### Section 13 — Master Aggregation Controls

Section 13 reduces all active lane states to a single master classification using strict priority ordering:

| Priority | Condition                             | master_classification      |
|----------|---------------------------------------|----------------------------|
| 1        | Any active lane = block               | block_output               |
| 2        | No block; any active lane = escalate  | escalate_systemic_issue    |
| 3        | No block/escalate; any lane = fail    | fail                       |
| 4        | No fail; any lane = warning           | review_recommended         |
| 5        | All active lanes = pass               | pass                       |

---

### Section 14 — Root Cause Fusion Controls

Section 14 examines all faults across all active lanes and votes on the most likely systemic root cause. Each fault type is pre-mapped to one of four root cause categories:

| Root Cause         | Example Faults                                                |
|--------------------|---------------------------------------------------------------|
| input_failure      | incorrect_result, numeric_error, reconciliation_error, representation_bias, selection_bias |
| stale_data         | distribution_drift, mean_shift, stale_source, missing_provenance, missing_citation |
| rule_design        | hard_rule_violation, prohibited_output, misclassification, outcome_bias, proxy_bias |
| model_instability  | statistical_outlier, suspicious_uniformity, non_reproducible, unstable_result, adversarial_vulnerability |

The root cause with the most votes wins if two conditions are met:
- **Vote count** ≥ `FUSION_THRESHOLD` (default 2 votes)
- **Confidence** (winning votes ÷ total faults) ≥ `FUSION_CONFIDENCE_THRESHOLD` (default 0.40)

If neither condition is met, `master_root_cause = unresolved`. If there are no faults at all, `master_root_cause = no_faults`.

---

### Section 15 — Remediation Routing Controls

Section 15 maps each lane that classifies as `fail` or `block` to a remediation action. All active remediations accumulate in the `remediation_route` list.

| Lane          | Remediation Route                    |
|---------------|--------------------------------------|
| correctness   | recompute_or_relabel                 |
| compliance    | policy_rewrite_or_suppress           |
| anomaly       | drift_investigation                  |
| bias          | fairness_review_and_mitigation       |
| robustness    | stress_test_and_hardening            |
| traceability  | provenance_refresh                   |

Lanes in `warning`, `pass`, `escalate`, or `inactive` states do not generate remediation routes in this section. Escalated lanes are routed via the escalation pathway in Section 16.

---

### Section 16 — Output Controls

Section 16 maps `master_classification` to a concrete output action:

| master_classification     | output_action              |
|---------------------------|----------------------------|
| pass                      | approve_result             |
| review_recommended        | send_to_manual_review      |
| fail                      | reject_result              |
| block_output              | block_downstream_use       |
| escalate_systemic_issue   | trigger_system_escalation  |

Additionally, if `master_root_cause = unresolved`, the action `request_investigation` is appended to `output_action` regardless of master classification. A passing result with an unresolved root cause is a pass that warrants investigation of why any faults were present.

The output actions are written to the decision log. Downstream pipeline gating is the responsibility of the caller.

---

### Section 17 — Feedback Loop Controls

Section 17 records feedback events that enable threshold calibration over time. It reads `audit_context.feedback_event` and records a disposition for the threshold management layer.

| Feedback Event            | Disposition                  | Threshold Effect           |
|---------------------------|------------------------------|----------------------------|
| confirmed_fault           | threshold_tighten_candidate  | Tighten relevant thresholds |
| false_positive            | threshold_relax_candidate    | Relax relevant thresholds  |
| post_release_incident     | threshold_tighten_urgent     | Urgent tighten             |
| remediation_success       | resolved                     | Mark case resolved         |
| remediation_fail/block/escalate | unresolved             | Mark case unresolved       |

Additionally, if `audit_context.repeat_pattern_count` for a given `route_id` exceeds `RECURRENCE_THRESHOLD` (default 3), the disposition `systemic_pipeline_issue` is recorded.

**All threshold write-back and case management operations are marked `TODO:DATA_DEPENDENCY` pending integration with the platform threshold management layer.** Section 17 records the event and its disposition to the decision log but does not mutate any threshold at runtime.

---

### Section 18 — Final Stack Composite Score

Section 18 computes the weighted stack score:

```
stack_score = a1*correctness_score + a2*compliance_score + a3*anomaly_score
            + a4*bias_score + a5*robustness_score + a6*traceability_score
```

Default weights:

| Weight | Lane          | Value |
|--------|---------------|-------|
| a1     | correctness   | 0.25  |
| a2     | compliance    | 0.25  |
| a3     | anomaly       | 0.15  |
| a4     | bias          | 0.15  |
| a5     | robustness    | 0.10  |
| a6     | traceability  | 0.10  |

Inactive lanes contribute a score of 0.0 to the weighted sum.

After computing the score, the section assigns `final_stack_signal`. Override conditions take precedence:

| Condition                              | final_stack_signal |
|----------------------------------------|--------------------|
| master_classification = block_output   | blocked            |
| master_classification = escalate_systemic_issue | systemic_failure |
| stack_score < STACK_WARNING_THRESHOLD  | clean              |
| STACK_WARNING_THRESHOLD ≤ stack_score < STACK_FAIL_THRESHOLD | warning |
| stack_score ≥ STACK_FAIL_THRESHOLD     | faulty             |

Default thresholds: `STACK_WARNING_THRESHOLD = 0.20`, `STACK_FAIL_THRESHOLD = 0.45`.

---

## Audit Trail

Each section records at minimum one entry to `StackDecisionLog.records` containing:

| Field       | Description                                      |
|-------------|--------------------------------------------------|
| section     | Section number (1–18)                            |
| name        | Section name or sub-check label                  |
| inputs      | All evaluated values with their names            |
| result      | Classification outcome or computed value         |
| reason_code | Machine-readable label explaining the decision   |
| ts          | UTC timestamp of the record                      |

Lane faults are recorded separately in `StackDecisionLog.lane_faults`, keyed by lane name. Each fault entry includes `fault_type`, `inputs`, and `ts`.

The complete log is serialised to JSON via `to_dict()` and written to the database event log by `write_stack_log()`.

### Escalation

`escalate_if_needed()` posts to `_db.post_suggestion()` under the following conditions:

| Condition                       | risk_level | Note                                          |
|---------------------------------|------------|-----------------------------------------------|
| master_classification = block_output | HIGH  | Downstream use suppressed                     |
| master_classification = escalate_systemic_issue | HIGH | Platform-level review required   |
| final_stack_signal = warning    | MEDIUM     | Manual review recommended                     |

---

## Controls Not Yet Implemented

| Control                                | Section | Status                   |
|----------------------------------------|---------|--------------------------|
| Pre-computed anomaly metrics (z-score, variance, drift, mean_shift, fault_density) | 5 | TODO:DATA_DEPENDENCY — caller must supply in audit_context |
| Pre-computed bias metrics (representation_gap, selection_probability_gap, group_error_rate_gap) | 6 | TODO:DATA_DEPENDENCY — caller must supply in audit_context |
| Pre-computed robustness metrics (sensitivity_metric, attack_case_success_rate, repeat_variance) | 7 | TODO:DATA_DEPENDENCY — caller must supply in audit_context |
| Citation support score computation     | 8       | TODO:DATA_DEPENDENCY — caller must supply citation_support_scores |
| Threshold write-back after feedback    | 17      | TODO:DATA_DEPENDENCY — platform threshold management layer not yet integrated |
| Case management system integration     | 17      | TODO:DATA_DEPENDENCY — audit case store not yet integrated |
| Repeat lane fault count tracking       | 10      | TODO:DATA_DEPENDENCY — requires historical fault store; caller must supply repeat_lane_fault_counts |
| Repeat route recurrence tracking       | 17      | TODO:DATA_DEPENDENCY — requires route history store; caller must supply repeat_pattern_count |

---

## What This Agent Does Not Do

- It does not generate trading signals or operational recommendations.
- It does not compute statistical distributions from raw data — all anomaly metrics must be pre-computed and supplied by the caller.
- It does not perform NLP or ML inference of any kind. Language bias scoring must be pre-computed and supplied in `audit_context.language_bias_score`.
- It does not mutate thresholds at runtime. Threshold calibration events are recorded but not applied until a human-supervised threshold management process acts on them.
- It does not contact external APIs or data sources.
- It does not modify the result payload. The payload is read-only within this agent.
- It does not suppress output directly — it sets `output_action = block_downstream_use`, but enforcement is the responsibility of the caller.

---

## Human Oversight Points

| Trigger                                           | Action Required                                              |
|---------------------------------------------------|--------------------------------------------------------------|
| master_classification = block_output              | Human must review before downstream use is permitted        |
| master_classification = escalate_systemic_issue   | Platform-level review; pattern may require system changes   |
| final_stack_signal = warning                      | Manual review of lane faults recommended before approval    |
| master_root_cause = unresolved                    | Investigation required even if master_classification = pass |
| Cross-lane flag = suspicious_pass                 | Verify correctness check inputs; result may have passed on incomplete data |
| Cross-lane flag = compound_critical_issue         | Both factual accuracy and policy compliance are implicated  |
| feedback_event = post_release_incident            | Urgent threshold review required                            |
| disposition = systemic_pipeline_issue             | Recurrence of same route — platform architecture review     |
