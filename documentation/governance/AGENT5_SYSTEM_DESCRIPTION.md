# Synthos — Agent 5 (ResultAuditAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 5 (ResultAuditAgent) is the result-auditing and failure-detection layer of
the Synthos system. It accepts any structured result payload, runs it through a
23-gate deterministic fault-detection spine, and produces a classification, a
remediation directive, and a composite fault score.

**Agent 5 does not use machine learning or AI inference to detect faults.**
All decisions are rule-based, deterministic, and fully traceable. Every result
submitted for audit that passes Gate 1 produces a complete `AuditDecisionLog`
entry recording each gate's name, inputs, detected faults, severity assignments,
and reason codes.

**Strict scope:** Agent 5 audits result payloads. It does not originate signals,
make trade decisions, or modify the results it audits. Faults are reported; all
remediation actions are recommended, not automatically applied, unless the
classification is `block_output`, in which case downstream release is suppressed.

**Audit modes:** Agent 5 supports six audit modes — correctness, consistency,
completeness, compliance, robustness, and anomaly detection. Modes may be
activated individually or in combination (multi-pass audit).

---

## 2. Operational Schedule

| Trigger                | When                                   | Primary Purpose                                     |
|------------------------|----------------------------------------|-----------------------------------------------------|
| On-demand (per result) | Whenever a result is submitted         | Real-time gate-by-gate fault detection              |
| Batch audit            | Configurable schedule or manual        | Periodic audit of accumulated result payloads       |
| Post-remediation check | After remediation action is applied    | Confirm fault is resolved (Gate 21 loop)            |

Agent 5 has no fixed clock schedule. It is invoked by submitting a result payload
via the `audit_result()` interface or directly from the command line.

---

## 3. Fault Detection Spine — Overview

Agent 5 operates a 23-gate sequential fault-detection spine. Each gate checks
one category of fault conditions. All faults found across all gates accumulate
into a fault register. Gates do not halt the pipeline on fault detection —
they record the fault and proceed so that all applicable faults are visible
in a single audit pass.

The only gate that halts processing is Gate 1 (System Gate), where the input
itself is unworkable.

```
GATE 1  — System Gate               (missing inputs, parse failures, schema, duplicates)
GATE 2  — Scope & Audit Mode        (mode selection: correctness / consistency / completeness / compliance / robustness / anomaly)
GATE 3  — Result Structure          (required fields, types, nulls, format, nested schema)
GATE 4  — Logical Consistency       (field conflicts, summary/detail mismatch, totals, chronology, label/evidence)
GATE 5  — Completeness              (missing sections, explanations, evidence, edge cases, truncation)
GATE 6  — Correctness               (ground truth distance, label accuracy, numeric tolerance, units, signs, magnitude)
GATE 7  — Reference Comparison      (exact/partial mismatch, benchmark deviation, fault pattern match, ensemble outlier)
GATE 8  — Statistical / Distribution(outliers, variance collapse, drift, mean shift, error clustering, rate anomaly)
GATE 9  — Robustness                (instability, non-reproducibility, adversarial failure, boundary failure, load sensitivity)
GATE 10 — Reasoning / Causality     (unsupported inference, reasoning gaps, causal overreach, omitted factors, rationale conflict)
GATE 11 — Constraint / Policy       (hard rule violations, soft preference failures, prohibited content, disclaimers, audit trail)
GATE 12 — Data Provenance           (unverifiable source, stale source, unresolved conflicts, missing or unsupported citations)
GATE 13 — Temporal Controls         (outdated assumptions, data leakage, late detection, window misapplication, cadence failure)
GATE 14 — Explanation Quality       (absent, vague, overclaimed, hidden uncertainty, low-density, format mismatch)
GATE 15 — Ranking / Prioritization  (top-rank failure, score/rank inconsistency, recall failure, precision failure, tie-break)
GATE 16 — Confidence Calibration    (overconfidence, underconfidence, calibration drift, missing confidence, evidence mismatch)
GATE 17 — Fault Severity            (severity assignment: low / medium / high / critical / escalated_high / systemic)
GATE 18 — Root Cause Attribution    (input failure / rules failure / model instability / stale data / missing domain logic / unhandled edge case)
GATE 19 — Action Classification     (pass / pass_with_warnings / review_recommended / fail / block_output / escalate / manual_investigation)
GATE 20 — Remediation Controls      (remediation directive per fault type)
GATE 21 — Evaluation Loop           (audit record storage, fault library update, threshold adjustment)
GATE 22 — Output Controls           (output action: approve / approve_with_warning / reject / block / escalate / mark_uncertain)
GATE 23 — Final Composite Fault Score (weighted fault score → final_audit_signal: clean / warning / faulty / blocked / systemic_failure)
```

---

## 4. Audit Modes

Six audit modes are supported. Modes are specified at submission time and
drive which gate checks are activated during a pass.

| Mode               | Audit State                   | Primary Focus                                    |
|--------------------|-------------------------------|--------------------------------------------------|
| correctness        | factual_validation            | Ground truth comparison, label accuracy, numerics |
| consistency        | internal_consistency_check    | Field conflicts, totals, chronology              |
| completeness       | coverage_check                | Missing sections, evidence, edge case coverage   |
| compliance         | rule_conformance_check        | Hard rules, policies, disclaimers, audit trail   |
| robustness         | stress_check                  | Reproducibility, boundary cases, load sensitivity|
| anomaly            | outlier_scan                  | Statistical outliers, drift, rate anomalies      |

Multiple modes may be activated simultaneously, producing a `multi_pass_audit`.
Each active mode activates additional checks within its corresponding gate group.

---

## 5. Gate-by-Gate Description

### Gate 1 — System Gate

**Purpose:** Reject the audit submission before any analysis if the input itself
is unworkable. This is the only gate that halts the pipeline.

**Checks performed:**
- **Result input missing:** If `result_payload` is null, halt immediately with
  `state = reject_result`.
- **Reference input missing:** If `reference_payload` is null and
  `reference_required = true`, halt with `state = incomplete_evaluation`.
- **Parse failure:** If the result payload cannot be parsed into a structured
  representation, halt with `state = reject_result`.
- **Schema invalid:** If the result payload fails schema validation, halt with
  `state = invalid_structure`.
- **Evaluation rules unavailable:** If no evaluation rule set is available for
  the declared result type, halt with `state = halt_audit`.
- **Duplicate result:** If the hash of the result payload is already present in
  the audited result store, halt with `state = suppress_duplicate_audit`.
- **Timestamp missing:** If the result has no timestamp, processing continues but
  `fault_state = low_traceability` is added to the fault register. Temporal gates
  will be degraded.
- **Provenance missing:** If source provenance is null, processing continues but
  `fault_state = unverifiable_origin` is added to the fault register.

**Outcome:** PROCEED or HALT. Any HALT is logged with the specific condition.
Low-traceability and unverifiable-origin faults are non-halting but are carried
forward through all subsequent gates.

---

### Gate 2 — Scope & Audit Mode Controls

**Purpose:** Establish which audit modes are active for this pass. Mode selection
drives which specific checks are executed in Gates 3–16.

**Mode activation:**
- Each mode specified in the submission's `audit_modes` list is activated.
- If a single mode is specified, `audit_state` = that mode's label.
- If more than one mode is specified, `audit_state = multi_pass_audit`. All active
  mode checks run in a single pass.

**Default behaviour:** If no mode is specified, all six modes are activated
(full audit).

**Outcome:** Active audit modes recorded. Used by all subsequent gates to determine
which checks to execute.

---

### Gate 3 — Result Structure Controls

**Purpose:** Verify the structural integrity of the result payload — required
fields, types, nulls, format compliance, and nested schema validity.

**Checks performed:**
- **Missing required field:** Count of required fields present is less than the
  total required field count → `fault_state = missing_required_fields`.
- **Unexpected field present:** One or more fields exist that are not in the
  declared schema → `fault_state = extra_fields_detected`.
- **Field type mismatch:** Any field's actual type differs from its expected type
  → `fault_state = type_mismatch`.
- **Null in mandatory field:** Any mandatory field contains a null value →
  `fault_state = null_mandatory_value`.
- **Field format invalid:** Any field fails its format check (e.g., date format,
  enum membership, regex pattern) → `fault_state = format_error`.
- **Nested structure malformed:** Recursive schema validation fails on any subtree
  → `fault_state = malformed_substructure`.
- **Key ordering wrong:** If field ordering is declared as significant and the
  actual order does not match the expected order → `fault_state = ordering_error`.

**Outcome:** All structural faults added to the fault register with the specific
field name and expected vs. actual value.

---

### Gate 4 — Logical Consistency Controls

**Purpose:** Verify that the values within the result payload are internally
coherent — no conflicting fields, summary/detail agreement, reconciled totals,
valid chronology, and consistent labels.

**Checks performed:**
- **Field conflict:** A declared constraint between two fields is violated →
  `fault_state = internal_conflict`. Both field names and the violated constraint
  are logged.
- **Summary/detail mismatch:** A summary value does not equal the aggregate of
  its constituent detail values → `fault_state = summary_detail_mismatch`.
- **Reconciliation error:** A reported total does not equal the sum of component
  values → `fault_state = reconciliation_error`.
- **Temporal inconsistency:** A timestamp that should follow another timestamp
  is earlier → `fault_state = temporal_inconsistency`. Both timestamps logged.
- **Label/evidence conflict:** The assigned classification label is inconsistent
  with what the input evidence implies → `fault_state = label_evidence_conflict`.
- **Status conflict:** The declared status label is incompatible with the state
  variables present in the payload → `fault_state = status_conflict`.
- **Domain logic failure:** The result violates a domain-specific constraint
  defined in the rule set → `fault_state = domain_logic_failure`.

**Outcome:** All consistency faults added to fault register with full context.

---

### Gate 5 — Completeness Controls

**Purpose:** Verify that the result contains all required content — sections,
explanations, evidence, edge case coverage, and mode-specific fields.

**Checks performed:**
- **Missing section:** A required section is absent → `fault_state = incomplete_result`.
- **Missing explanation:** Explanation is required and null →
  `fault_state = missing_explanation`.
- **Missing supporting evidence:** Evidence is required and count is zero →
  `fault_state = unsupported_conclusion`.
- **Edge case coverage gap:** The number of expected edge cases addressed is
  less than the number required → `fault_state = edge_case_coverage_gap`. The
  gap count is logged.
- **Truncated output:** Output length equals the maximum length limit AND a
  truncation marker is detected → `fault_state = truncated_output`.
- **Mode completeness failure:** The active audit mode requires fields that are
  absent → `fault_state = mode_completeness_failure`.
- **Missing confidence:** Confidence is required and null →
  `fault_state = missing_confidence`.

**Outcome:** All completeness faults added to fault register.

---

### Gate 6 — Correctness Controls

**Purpose:** Verify that the result values are factually correct relative to
known ground truth, within configured tolerances.

**Checks performed:**
- **Incorrect result:** Distance between the result value and ground truth exceeds
  `correctness_tolerance` → `fault_state = incorrect_result`.
- **Misclassification:** Predicted class does not match actual class →
  `fault_state = misclassification`.
- **Numeric error:** Absolute difference between result value and expected value
  exceeds `numeric_tolerance` → `fault_state = numeric_error`.
- **Unit error:** Reported unit does not match expected unit →
  `fault_state = unit_error`.
- **Sign error:** Sign of result value differs from sign of expected value →
  `fault_state = sign_error`.
- **Magnitude error:** Absolute result value exceeds `max_plausible_bound` or
  is below `min_plausible_bound` where a non-zero result is expected →
  `fault_state = magnitude_error`.
- **Directional error:** Sign of the predicted change does not match sign of
  the actual change → `fault_state = directional_error`.

**Active only when:** `correctness` mode is active AND ground truth is available.

**Outcome:** All correctness faults added to fault register with expected vs.
actual values.

---

### Gate 7 — Reference Comparison Controls

**Purpose:** Compare the result against a reference payload, benchmark, known
fault patterns, and ensemble consensus.

**Checks performed:**
- **Exact mismatch:** Under exact match mode, result payload does not equal
  reference payload → `fault_state = exact_mismatch`.
- **Partial mismatch:** Match score between result and reference falls below
  `partial_match_threshold` → `fault_state = partial_mismatch`. Score logged.
- **Benchmark deviation:** Deviation metric between result and benchmark exceeds
  `deviation_threshold` → `fault_state = benchmark_deviation`.
- **Known fault signature:** Similarity of result to fault pattern library
  exceeds similarity to the correct pattern library → `fault_state = known_fault_signature`.
  The matching pattern identifier is logged.
- **Ensemble outlier:** The result disagrees with the majority of an ensemble —
  ensemble agreement ratio falls below `consensus_threshold` →
  `fault_state = ensemble_outlier`.
- **Outdated alignment:** Result is more similar to a stale reference than the
  current reference → `fault_state = outdated_alignment`.

**Active only when:** A reference or benchmark payload is available.

**Outcome:** All reference comparison faults added to fault register.

---

### Gate 8 — Statistical / Distribution Controls

**Purpose:** Detect statistical anomalies — outliers, variance collapse, drift,
mean shifts, error clustering, and impossible frequencies.

**Checks performed:**
- **Statistical outlier:** Z-score of the result value against the historical
  distribution exceeds `outlier_threshold` → `fault_state = statistical_outlier`.
- **Suspicious uniformity:** Variance of a result batch falls below
  `min_variance_threshold` → `fault_state = suspicious_uniformity`. Indicates
  collapsed or non-diverse outputs.
- **Distribution drift:** Distribution distance between the current batch and
  baseline batch exceeds `drift_threshold` → `fault_state = distribution_drift`.
- **Mean shift:** Absolute difference between current batch mean and baseline mean
  exceeds `mean_shift_threshold` → `fault_state = mean_shift`.
- **Clustered failure:** Fault density within a window of similar results exceeds
  `cluster_threshold` → `fault_state = clustered_failure`.
- **Rate anomaly:** Event frequency in the result exceeds `max_domain_frequency`
  → `fault_state = rate_anomaly`.

**Active only when:** `anomaly` mode is active AND historical distribution data
is available.

**Outcome:** All statistical faults added to fault register with relevant metric values.

---

### Gate 9 — Robustness Controls

**Purpose:** Detect brittleness — results that break under small perturbations,
are non-reproducible, fail on boundary inputs, or degrade under load.

**Checks performed:**
- **Unstable result:** A small input perturbation produces a disproportionately
  large output change — sensitivity metric exceeds `instability_threshold` →
  `fault_state = unstable_result`.
- **Non-reproducible:** The same input repeated produces output variance exceeding
  `reproducibility_threshold` → `fault_state = non_reproducible`.
- **Adversarial vulnerability:** Adversarial input cases succeed at breaking the
  output at a rate exceeding `adversarial_threshold` →
  `fault_state = adversarial_vulnerability`.
- **Brittle dependency:** Removing an optional input causes the output to become
  invalid → `fault_state = brittle_dependency`.
- **Boundary failure:** A boundary condition test case fails →
  `fault_state = boundary_failure`.
- **Load sensitivity:** Correctness under stress load falls below
  `load_performance_threshold` → `fault_state = load_sensitivity`.

**Active only when:** `robustness` mode is active AND test cases are provided.

**Outcome:** All robustness faults added to fault register.

---

### Gate 10 — Reasoning / Causality Controls

**Purpose:** Audit the logical chain from premises to conclusion — unsupported
inferences, reasoning gaps, causal overreach, omitted factors, and rationale
that contradicts the output.

**Checks performed:**
- **Unsupported inference:** Inference path score falls below `support_threshold`
  → `fault_state = unsupported_inference`.
- **Reasoning gap:** A reasoning step is logically disconnected from the preceding
  step → `fault_state = reasoning_gap`. The step index is logged.
- **Causal overreach:** Causal language is present but causal evidence is absent
  → `fault_state = causal_overreach`.
- **Omitted key factor:** An important feature has importance score above
  `importance_threshold` but is not mentioned in the reasoning →
  `fault_state = omitted_key_factor`. The feature is named.
- **Rationale/output conflict:** The explanation's logical implication differs
  from the stated result → `fault_state = rationale_output_conflict`.
- **Post-hoc justification risk:** The output was generated before the rationale
  evidence linkage was established AND the rationale match score is low →
  `fault_state = posthoc_justification_risk`.

**Active only when:** `consistency` mode is active AND reasoning chain is present.

**Outcome:** All reasoning faults added to fault register with step-level detail.

---

### Gate 11 — Constraint / Policy Controls

**Purpose:** Verify that the result complies with all hard rules, soft preferences,
content policies, and audit trail requirements.

**Checks performed:**
- **Hard rule violation:** The result payload fails a hard constraint defined in
  the active rule set → `fault_state = hard_rule_violation`. The violated rule
  is named.
- **Soft preference failure:** The preference score for the result falls below the
  soft threshold → `fault_state = soft_preference_failure`.
- **Prohibited content:** The result matches a prohibited content pattern →
  `fault_state = prohibited_output`. Pattern identifier logged.
- **Missing disclaimer:** A required disclaimer is absent from the result →
  `fault_state = missing_disclaimer`. The required disclaimer type is named.
- **Restricted recommendation:** The result recommends or implies a restricted
  action → `fault_state = restricted_recommendation`.
- **Audit trail noncompliant:** Trace log completeness falls below the minimum
  required → `fault_state = audit_trail_failure`. Completeness ratio logged.

**Active only when:** `compliance` mode is active.

**Outcome:** All constraint and policy faults added to fault register.

---

### Gate 12 — Data Provenance Controls

**Purpose:** Verify the quality, freshness, and consistency of the data sources
used to produce the result.

**Checks performed:**
- **Unverifiable source:** Source provenance confidence falls below
  `provenance_threshold` → `fault_state = unverifiable_source`.
- **Stale source:** Elapsed time since source timestamp exceeds `freshness_limit`
  → `fault_state = stale_source`. Elapsed time logged.
- **Unresolved source conflict:** One or more conflicting sources were used without
  a resolution note → `fault_state = unresolved_source_conflict`. Conflict count logged.
- **Missing citation:** External claim count exceeds cited claim count →
  `fault_state = missing_citation`. Gap count logged.
- **Unsupported citation:** A citation's support score for its associated claim
  falls below `support_threshold` → `fault_state = unsupported_citation`. The
  citation and claim are named.
- **Source priority violation:** A lower-priority source was used while a
  higher-priority source was available → `fault_state = source_priority_failure`.

**Outcome:** All provenance faults added to fault register.

---

### Gate 13 — Temporal Controls

**Purpose:** Detect time-related faults — outdated assumptions, data leakage,
audit latency, window misapplication, and cadence failures.

**Checks performed:**
- **Outdated assumption:** An assumption's timestamp precedes the validity window
  start → `fault_state = outdated_assumption`. The assumption and its timestamp
  are logged.
- **Data leakage:** A feature's timestamp is later than the decision timestamp →
  `fault_state = data_leakage`. The leaked feature is named.
- **Late detection:** Audit completion time exceeds `max_audit_latency` →
  `fault_state = late_detection`.
- **Window misapplication:** The evaluation window used does not match the intended
  window → `fault_state = window_misapplication`. Both windows are logged.
- **Cadence failure:** For periodic results, the actual interval deviates from the
  expected interval beyond `cadence_tolerance` → `fault_state = cadence_failure`.

**Outcome:** All temporal faults added to fault register.

---

### Gate 14 — Explanation Quality Controls

**Purpose:** Audit the quality of any explanation or rationale attached to the result.

**Checks performed:**
- **No explanation:** Explanation text is null → `fault_state = no_explanation`.
- **Vague explanation:** Specificity score of explanation falls below
  `specificity_threshold` → `fault_state = vague_explanation`.
- **Overclaimed certainty:** Language certainty in the explanation exceeds
  evidence strength by more than `confidence_gap_threshold` →
  `fault_state = overclaimed_certainty`.
- **Hidden uncertainty:** Uncertainty is required but uncertainty terms are absent
  → `fault_state = hidden_uncertainty`.
- **Low-information explanation:** Information density falls below `density_threshold`
  AND length exceeds `verbosity_threshold` → `fault_state = low_information_explanation`.
- **Explanation format failure:** Explanation does not match the required format →
  `fault_state = explanation_format_failure`.

**Active only when:** `completeness` or `compliance` mode is active AND an
explanation field is present or required.

**Outcome:** All explanation quality faults added to fault register.

---

### Gate 15 — Ranking / Prioritization Controls

**Purpose:** Audit the correctness and internal consistency of any ranked output.

**Checks performed:**
- **Top-rank failure:** The item ranked first is not the true best item →
  `fault_state = top_rank_failure`.
- **Score/rank inconsistency:** The ranking order does not correspond to
  descending score order → `fault_state = score_rank_inconsistency`.
- **Recall failure in top-k:** A true relevant item is absent from the top-k
  results → `fault_state = recall_failure_topk`. The missing item is named.
- **Precision failure in top-k:** An irrelevant item is included in the top-k
  results → `fault_state = precision_failure_topk`. Count logged.
- **Tie-break failure:** Ties exist in the score vector and the tie-break rule
  was not applied → `fault_state = tie_break_failure`.

**Active only when:** The result contains a ranked list or scored candidates.

**Outcome:** All ranking faults added to fault register.

---

### Gate 16 — Confidence Calibration Controls

**Purpose:** Detect miscalibrated confidence — overconfidence in wrong results,
underconfidence in correct results, and calibration drift over time.

**Checks performed:**
- **Overconfidence:** Confidence score is at or above `high_confidence_threshold`
  AND a fault is detected → `fault_state = overconfidence`.
- **Underconfidence:** Confidence score is at or below `low_confidence_threshold`
  AND no fault is detected → `fault_state = underconfidence`.
- **Calibration drift:** Calibration error over the current window exceeds
  `calibration_threshold` → `fault_state = miscalibrated_confidence`.
- **Missing confidence:** Confidence is required and null →
  `fault_state = missing_confidence`.
- **Confidence/evidence mismatch:** The absolute difference between confidence
  score and evidence quality score exceeds `confidence_evidence_gap` →
  `fault_state = confidence_evidence_mismatch`.

**Outcome:** All calibration faults added to fault register.

---

### Gate 17 — Fault Severity Controls

**Purpose:** Assign a severity level to the overall fault picture. Severity
drives action classification in Gate 19.

**Severity assignment (evaluated in priority order — highest severity wins):**

| Condition | Severity |
|-----------|----------|
| Any fault's `risk_of_harm > harm_threshold` | `critical` |
| `count(medium_severity_faults) >= escalation_count` | `escalated_high` |
| `repeat_fault_count(pattern_i) > repeat_threshold` | `systemic` |
| Any fault with `fault_impact_scope = decision_output` | `high` |
| Any fault with `fault_impact_scope = explanation_or_format_only` | `medium` |
| All faults with `fault_impact_scope = presentation_only` | `low` |
| No faults detected | `none` |

**Outcome:** Maximum severity level written to decision log. All faults are
individually tagged with their own scope impact, but the Gate 17 outcome is
the overall maximum across all faults in the register.

---

### Gate 18 — Root Cause Attribution Controls

**Purpose:** Attribute the detected faults to one or more root causes.

**Attribution rules (all applicable root causes are recorded — not mutually exclusive):**
- **Input failure:** Input quality score falls below `input_quality_threshold` →
  `root_cause = input_failure`.
- **Rules failure:** Rule set version is incompatible with the expected rule version
  → `root_cause = rules_failure`.
- **Model instability:** Repeat variance exceeds `reproducibility_threshold` →
  `root_cause = model_instability`.
- **Stale data:** Source freshness state is stale → `root_cause = stale_data`.
- **Missing domain logic:** A domain constraint is absent and a domain failure
  is detected → `root_cause = missing_domain_logic`.
- **Unhandled edge case:** The fault signature matches a known edge case pattern
  → `root_cause = unhandled_edge_case`.
- **Unresolved:** Maximum root cause probability falls below
  `root_cause_confidence_threshold` → `root_cause = unresolved`.

**Outcome:** All applicable root causes recorded. Unresolved root cause
propagates to Gate 22 (`output_action = request_investigation`).

---

### Gate 19 — Action Classification

**Purpose:** Combine all gate results into a single classification decision.

**Classification rules (evaluated in priority order):**

| Condition | Classification |
|-----------|---------------|
| `critical_fault_count > 0` | `block_output` |
| `severity = systemic` | `escalate_systemic_issue` |
| `root_cause = unresolved` AND `max_severity >= medium` | `manual_investigation` |
| `max_severity = high` | `fail` |
| `max_severity = medium` | `review_recommended` |
| `max_severity = low` | `pass_with_warnings` |
| `high_severity_fault_count = 0` AND `critical_fault_count = 0` AND `total_fault_score < accept_threshold` | `pass` |

**Outcome:** Classification label written to decision log. Used by Gates 20 and 22.

---

### Gate 20 — Remediation Controls

**Purpose:** Assign a remediation directive to the audit. The directive is a
recommendation — it is not automatically executed.

**Remediation mapping (all active fault types receive a directive):**

| Fault State | Remediation Directive |
|-------------|----------------------|
| `missing_required_fields` | `request_or_reconstruct_missing_fields` |
| `type_mismatch` | `coerce_or_reparse_types` |
| `unsupported_conclusion` | `require_evidence_attachment` |
| `stale_source` | `refresh_sources` |
| `reconciliation_error` | `recompute_aggregates` |
| `miscalibrated_confidence` | `recalibrate_confidence_model` |
| Classification = `escalate_systemic_issue` | `quarantine_pipeline_segment` |
| Classification = `block_output` | `suppress_release` |

Multiple faults produce multiple remediation directives, all listed in the
audit output. Where classification is `block_output`, `suppress_release` takes
precedence and is flagged for immediate action.

**Outcome:** Remediation directive list written to decision log.

---

### Gate 21 — Evaluation Loop

**Purpose:** Store the audit record, update the fault library with confirmed
patterns, and adjust audit thresholds based on observed false positive and
missed fault rates.

**Actions performed:**
- **Audit record storage:** Every classification except pre-Gate-1 halts is
  stored in the audit record store.
- **Fault library update:** If manual review confirms the fault →
  `fault_library` is updated with the confirmed pattern.
- **False alarm adjustment:** If manual review identifies a false positive →
  audit thresholds are adjusted to reduce future false positives.
- **Missed fault tightening:** If missed fault rate exceeds `missed_fault_threshold`
  → audit rules are tightened.
- **False positive relaxation:** If false positive rate exceeds
  `false_positive_threshold` → audit rules are relaxed.
- **Emerging system issue detection:** If the trend of a specific fault pattern
  exceeds `growth_threshold` → `state = emerging_system_issue`.
- **Resolution tracking:** Post-remediation audit result of `pass` →
  `state = resolved`. Result of `fail` or `block_output` → `state = unresolved_fault`.

**Outcome:** Audit record stored. Fault library and threshold adjustments logged.

---

### Gate 22 — Output Controls

**Purpose:** Determine the output action based on classification and audit confidence.

**Output action by classification:**

| Classification | Output Action |
|----------------|--------------|
| `pass` | `approve_result` |
| `pass_with_warnings` | `approve_with_warning_log` |
| `review_recommended` | `send_to_manual_review` |
| `fail` | `reject_result` |
| `block_output` | `block_downstream_use` |
| `escalate_systemic_issue` | `trigger_system_escalation` |
| `manual_investigation` | `request_investigation` |

**Audit confidence overrides:**
- If audit confidence falls below `audit_confidence_threshold` →
  `output_action = mark_audit_uncertain` (overrides classification-based action).
- If root cause is unresolved → `output_action = request_investigation`
  (applies in addition to classification-based action).

**Outcome:** Output action written to decision log. Used by the caller to route
the result.

---

### Gate 23 — Final Composite Fault Score

**Purpose:** Compute a single weighted fault score that summarises the severity
across all fault categories.

**Score formula:**

```
fault_score = (w1 × correctness_fault)
            + (w2 × consistency_fault)
            + (w3 × completeness_fault)
            + (w4 × policy_fault)
            + (w5 × robustness_fault)
            + (w6 × provenance_fault)
            + (w7 × calibration_fault)
```

A higher fault score indicates a more severely faulty result. The score is
normalised to the range [0.0, 1.0].

**Default weights:**

| Component | Weight |
|-----------|--------|
| correctness_fault | 0.25 |
| consistency_fault | 0.20 |
| completeness_fault | 0.15 |
| policy_fault | 0.15 |
| robustness_fault | 0.10 |
| provenance_fault | 0.10 |
| calibration_fault | 0.05 |

All weights are configurable module-level constants.

**Final audit signal rules (evaluated in priority order):**

| Condition | Final Audit Signal |
|-----------|-------------------|
| `critical_fault_count > 0` | `blocked` |
| `classification = escalate_systemic_issue` | `systemic_failure` |
| `fault_score >= fail_threshold` | `faulty` |
| `warning_threshold <= fault_score < fail_threshold` | `warning` |
| `fault_score < warning_threshold` | `clean` |

**Outcome:** Composite fault score and final audit signal written to decision log
and to the output record.

---

## 6. Audit Decision Log

Every result that advances past Gate 1 produces an `AuditDecisionLog` entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Run timestamp and result identifier
- Audit modes active
- Each gate's name, inputs evaluated, faults detected, severity, and reason code
- Full fault register (all fault states, field names, and values)
- Root cause list
- Classification and remediation directives
- Final audit signal and composite fault score

> **FLAG — LOG WRITE LOCATION:** Currently written via `db_helpers.DB.log_event()`
> to the `system_log` table. A dedicated `audit_decisions` table is recommended
> to support regulatory export and volume management. Tracked as future work item.

---

## 7. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Ground truth retrieval | Ground truth store or oracle | TODO: DATA_DEPENDENCY |
| Historical distribution baseline | Accumulated result history | TODO: DATA_DEPENDENCY |
| Fault pattern library | Populated from prior confirmed faults | TODO: DATA_DEPENDENCY |
| Ensemble comparison | Parallel result set from multiple runs | TODO: DATA_DEPENDENCY |
| Adversarial test case library | Curated adversarial inputs | TODO: DATA_DEPENDENCY |
| Feature importance scores | Model explanation layer | TODO: DATA_DEPENDENCY |
| Calibration error tracking | Rolling calibration history | TODO: DATA_DEPENDENCY |
| Post-remediation loop trigger | Remediation pipeline integration | TODO: DATA_DEPENDENCY |

---

## 8. What Agent 5 Does Not Do

- Agent 5 does not use any AI language model to detect faults or classify results.
- Agent 5 does not modify the result payload it is auditing.
- Agent 5 does not automatically apply remediation actions. Remediation directives
  are recommendations only, with the exception that `block_output` suppresses
  downstream release.
- Agent 5 does not make trade decisions or generate market signals.
- Agent 5 does not send communications directly. All output is written to the
  company database via `db_helpers`. Escalations route through the company node
  notification pipeline (Scoop agent).

---

## 9. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| `classification = block_output` | Downstream release suppressed | Review fault before releasing result |
| `classification = escalate_systemic_issue` | System escalation triggered; quarantine recommended | Investigate pipeline segment producing pattern |
| `classification = manual_investigation` | Flagged in audit log; not auto-resolved | Human review of unresolved root cause |
| `root_cause = unresolved` | Logged; investigation requested | Determine root cause and update fault library |
| `state = emerging_system_issue` | Trend logged; alert dispatched | Investigate growing fault pattern |
| `state = unresolved_fault` (post-remediation) | Logged | Escalate — remediation did not resolve the fault |
| All audit modes return `pass` consistently | No action | Periodic threshold review to avoid staleness |
