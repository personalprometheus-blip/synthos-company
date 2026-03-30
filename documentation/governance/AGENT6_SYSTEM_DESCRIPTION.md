# Synthos — Agent 6 (BiasAuditAgent) System Description
## Regulatory Reference Document

**Document Version:** 1.0
**Effective Date:** 2026-03-30
**Status:** Active
**Audience:** Regulators, compliance reviewers, auditors

---

## 1. Purpose and Scope

Agent 6 (BiasAuditAgent) is the bias-detection and fairness-audit layer of the
Synthos system. It accepts any structured result payload along with group attribute
data and runs it through a 20-gate deterministic bias-detection spine. It produces
a classification, a set of remediation directives, and a composite bias score for
every result submitted.

**Agent 6 does not use machine learning or AI inference to detect bias.**
All decisions are rule-based, deterministic, and fully traceable. Every result
that passes Gate 1 produces a complete `BiasAuditDecisionLog` entry recording
each gate's name, inputs, detected bias faults, severity assignments, root causes,
and reason codes.

**Strict scope:** Agent 6 detects bias in result payloads. It does not originate
signals, make trade decisions, or modify the results it audits. Bias faults are
reported; all remediation actions are recommendations, except when classification
is `block_output`, in which case downstream release is suppressed.

**Audit modes:** Agent 6 supports six bias audit modes — representation, selection,
label, measurement, outcome, and language. Modes may be activated individually or
in combination (multi-pass audit). Default: all six modes active.

---

## 2. Operational Schedule

| Trigger                | When                                   | Primary Purpose                                     |
|------------------------|----------------------------------------|-----------------------------------------------------|
| On-demand (per result) | Whenever a result is submitted         | Real-time gate-by-gate bias detection               |
| Batch audit            | Configurable schedule or manual        | Periodic audit of accumulated result payloads       |
| Post-remediation check | After remediation action is applied    | Confirm bias fault is resolved (Gate 18 loop)       |

Agent 6 has no fixed clock schedule. It is invoked by submitting a result payload
via the `audit_bias()` interface or directly from the command line.

---

## 3. Bias Detection Spine — Overview

Agent 6 operates a 20-gate sequential bias-detection spine. Each gate checks one
category of bias conditions. All bias faults found across all gates accumulate into
a bias fault register. Gates do not halt the pipeline on fault detection — they
record the fault and continue so that all applicable biases are visible in a single
audit pass.

The only gate that halts processing is Gate 1 (System Gate), where the input itself
is unworkable.

```
GATE 1  — System Gate               (missing output, subject data, comparison groups, parse failure)
GATE 2  — Bias Audit Mode           (mode selection: representation / selection / label / measurement / outcome / language)
GATE 3  — Group Mapping             (protected group identification, proxy detection, direct attribute violation)
GATE 4  — Representation Bias       (underrepresentation, overrepresentation, omitted subgroups, skew drift)
GATE 5  — Selection Bias            (selection distortion, access barriers, differential missingness, filter stage bias)
GATE 6  — Label / Annotation Bias   (inconsistent labeling, severity bias, annotation confidence gaps, biased ground truth)
GATE 7  — Measurement Bias          (differential measurement error, weak proxies, collection bias, normalization bias)
GATE 8  — Outcome / Decision Bias   (approval rate disparity, FPR/FNR gaps, calibration gap, threshold bias, ranking exposure)
GATE 9  — Language Bias             (loaded language, stereotyped framing, tone disparity, agency bias, exclusionary phrasing)
GATE 10 — Counterfactual Fairness   (group-swap decision instability, score shift, explanation shift, justified differences)
GATE 11 — Proxy / Leakage           (ZIP/location proxies, name-based inference, linguistic proxy, protected attribute leakage)
GATE 12 — Context / Justification   (unjustified disparity, prohibited rationale, vague justification, circular historical bias)
GATE 13 — Temporal Bias             (worsening trend, ineffective remediation, deployment-induced bias, fairness drift)
GATE 14 — Severity Controls         (low / medium / high / critical / escalated_high / systemic)
GATE 15 — Root Cause Attribution    (sampling / annotation / feature / threshold / rule design / unresolved)
GATE 16 — Action Classification     (pass / pass_with_bias_warning / fairness_review_recommended / fail / block / escalate)
GATE 17 — Remediation Controls      (remediation directive per bias fault type)
GATE 18 — Evaluation Loop           (bias record storage, pattern library update, threshold adjustment)
GATE 19 — Output Controls           (approve / approve_with_warning / fairness_review / reject / block / escalate)
GATE 20 — Final Composite Bias Score(weighted bias score → final_bias_signal: clean / warning / biased / blocked / systemic)
```

---

## 4. Bias Audit Modes

Six bias audit modes are supported. Modes are specified at submission time.

| Mode             | Bias State                   | Primary Focus                                          |
|------------------|------------------------------|--------------------------------------------------------|
| representation   | representation_check         | Group distribution vs. reference population            |
| selection        | selection_check              | Sampling and filtering distortion by group             |
| label            | label_check                  | Annotation consistency and ground truth validity       |
| measurement      | measurement_check            | Feature quality and proxy validity by group            |
| outcome          | outcome_check                | Decision rate and error rate parity across groups      |
| language         | language_check               | Loaded terms, tone, framing, and agency attribution    |

Multiple modes may be activated simultaneously, producing a `multi_pass_bias_audit`.
Default: all six modes active if none are specified.

---

## 5. Gate-by-Gate Description

### Gate 1 — System Gate

**Purpose:** Reject the submission before analysis if the input is unworkable.
This is the only gate that halts the pipeline.

**Checks performed:**
- **Output missing:** If `result_payload` is null → halt with `state = reject_result`.
- **Subject/entity information missing:** If `subject_attributes` is null and
  `attribute_context_required = true` → halt with `state = incomplete_bias_audit`.
- **Comparison group data missing:** If `comparison_group_payload` is null and
  `parity_test_required = true` → halt with `state = insufficient_comparison_data`.
- **Parse failure:** If the result payload cannot be parsed → halt with
  `state = reject_result`.
- **Fairness rules unavailable:** If `bias_rule_set` is null → halt with
  `state = halt_bias_audit`.
- **Provenance missing:** Non-halting. If `source_provenance` is null →
  `bias_fault = low_traceability` added to the fault register; processing continues.

**Outcome:** PROCEED or HALT. Any HALT is logged with the specific condition.

---

### Gate 2 — Bias Audit Mode Controls

**Purpose:** Establish which bias audit modes are active for this pass. Mode
selection drives which gate checks are executed in Gates 4–13.

**Mode activation:**
- Each mode in the submission's `bias_modes` list is activated.
- Single mode: `bias_state` = that mode's label.
- Multiple modes: `bias_state = multi_pass_bias_audit`. All checks run in one pass.
- No modes specified: all six modes activated (full bias audit).

**Outcome:** Active bias modes recorded and applied to all subsequent gates.

---

### Gate 3 — Group Mapping Controls

**Purpose:** Identify the groups present in the result, detect proxy variables,
and flag direct use of prohibited attributes.

**Checks performed:**
- **Mapped group:** If a protected or monitored group attribute is detected →
  `group_state = mapped_group`.
- **Multiple comparison groups:** If more than one comparison group is present →
  `group_state = multi_group`.
- **No group mapping possible:** If group mapping confidence falls below
  `group_mapping_threshold` → `group_state = unmapped`. Downstream parity
  checks are degraded but not skipped.
- **Proxy variable detected:** If a feature's proxy score for a protected attribute
  exceeds `proxy_threshold` → `group_state = proxy_risk`. Forwarded to Gate 11.
- **Direct attribute used where prohibited:** If a protected attribute is used
  directly in the result and policy disallows direct use →
  `group_state = direct_attribute_violation`.

**Outcome:** Group states and proxy risk flags written to decision log.
Used by Gates 4–13 for group-level comparisons.

---

### Gate 4 — Representation Bias Controls

**Purpose:** Detect whether groups are proportionally represented relative to
the reference population.

**Checks performed:**
- **Underrepresentation:** Group share in the sample falls below
  `underrepresentation_threshold` × its share in the reference population →
  `bias_fault = underrepresentation`.
- **Overrepresentation:** Group share in the sample exceeds
  `overrepresentation_threshold` × its share in the reference population →
  `bias_fault = overrepresentation`.
- **Omitted subgroup:** A subgroup expected to appear has zero representation →
  `bias_fault = omitted_subgroup`.
- **Worsening representation skew:** The representation gap has grown beyond
  `drift_margin` compared to the prior measurement period →
  `bias_fault = worsening_representation_skew`.
- **Weak reference baseline:** The reference population quality score falls below
  `reference_quality_threshold` → `bias_fault = weak_reference_baseline`.
  Representation checks run with this caveat noted.

**Active only when:** `representation` mode is active AND group distribution data
is available.

**Outcome:** All representation faults added to fault register with group names,
observed proportions, and reference proportions.

---

### Gate 5 — Selection Bias Controls

**Purpose:** Detect distortions in how individuals or records were selected into
the sample or result set.

**Checks performed:**
- **Selection bias:** Distribution distance between the selected population and
  the eligible population exceeds `selection_bias_threshold` →
  `bias_fault = selection_bias`.
- **Access bias:** Selection probability for one group is substantially lower than
  for another without a justified constraint → `bias_fault = access_bias`.
- **Differential missingness:** Missing data rate gap between groups exceeds
  `missingness_gap_threshold` → `bias_fault = differential_missingness`.
- **Self-selection bias:** Self-selection effect size exceeds `self_selection_threshold`
  → `bias_fault = self_selection_bias`.
- **Filter stage bias:** Drop-off rate gap between groups at any filtering stage
  exceeds `dropoff_gap_threshold` → `bias_fault = filter_stage_bias`.

**Active only when:** `selection` mode is active AND selection rate data is available.

**Outcome:** All selection faults added to fault register with group-level rates.

---

### Gate 6 — Label / Annotation Bias Controls

**Purpose:** Detect bias in how labels or annotations were assigned across groups.

**Checks performed:**
- **Inconsistent labeling:** Label disagreement on similar cases across groups
  exceeds `label_bias_threshold` → `bias_fault = inconsistent_labeling`.
- **Severity label bias:** The rate of harsh or severe labels assigned to one group
  exceeds that of another group by more than `severity_gap_threshold` →
  `bias_fault = severity_label_bias`.
- **Annotation uncertainty bias:** Annotation confidence gap between groups exceeds
  `annotation_confidence_threshold` → `bias_fault = annotation_uncertainty_bias`.
- **Biased ground truth:** The ground truth source is flagged as historically biased
  → `bias_fault = biased_ground_truth`. This fault escalates severity automatically.
- **Label guideline risk:** Label guideline ambiguity score exceeds
  `ambiguity_threshold` → `bias_fault = label_guideline_risk`.

**Active only when:** `label` mode is active AND annotation metadata is available.

**Outcome:** All label faults added to fault register.

---

### Gate 7 — Measurement Bias Controls

**Purpose:** Detect whether the quality of features or measurements differs
across groups in ways that introduce systematic bias.

**Checks performed:**
- **Differential measurement error:** Measurement error gap between groups exceeds
  `measurement_gap_threshold` → `bias_fault = differential_measurement_error`.
- **Weak proxy measure:** A proxy feature is used for a latent trait AND the proxy's
  validity score falls below `validity_threshold` →
  `bias_fault = weak_proxy_measure`.
- **Collection bias:** Capture quality for one group falls below that of another
  group by more than `capture_gap_threshold` → `bias_fault = collection_bias`.
- **Normalization bias:** A shared normalization standard causes group distortion
  exceeding `normalization_bias_threshold` → `bias_fault = normalization_bias`.
- **Imputation bias:** Imputation error gap between groups exceeds
  `imputation_gap_threshold` → `bias_fault = imputation_bias`.

**Active only when:** `measurement` mode is active AND per-group measurement
quality data is available.

**Outcome:** All measurement faults added to fault register.

---

### Gate 8 — Outcome / Decision Bias Controls

**Purpose:** Detect disparities in decision outcomes, error rates, and ranking
exposure across groups.

**Checks performed:**
- **Outcome rate disparity:** Absolute difference in approval or positive outcome
  rates between groups exceeds `approval_gap_threshold` →
  `bias_fault = outcome_rate_disparity`.
- **False positive disparity:** Absolute difference in false positive rates between
  groups exceeds `fpr_gap_threshold` → `bias_fault = false_positive_disparity`.
- **False negative disparity:** Absolute difference in false negative rates between
  groups exceeds `fnr_gap_threshold` → `bias_fault = false_negative_disparity`.
- **Group calibration gap:** Absolute difference in calibration error between groups
  exceeds `calibration_gap_threshold` → `bias_fault = group_calibration_gap`.
- **Threshold bias:** The decision threshold effect gap between groups exceeds
  `threshold_effect_threshold` → `bias_fault = threshold_bias`.
- **Ranking exposure bias:** Exposure share difference between groups in a ranked
  list exceeds `exposure_gap_threshold` → `bias_fault = ranking_exposure_bias`.

**Active only when:** `outcome` mode is active AND per-group outcome data is
available.

**Outcome:** All outcome faults added to fault register with group rates and gap values.

---

### Gate 9 — Language Bias Controls

**Purpose:** Detect bias in the language used to describe, frame, or reference
groups within the result.

**Checks performed:**
- **Loaded language:** Loaded term density exceeds `loaded_language_threshold` →
  `bias_fault = loaded_language`.
- **Stereotyped framing:** Stereotype score exceeds `stereotype_threshold` →
  `bias_fault = stereotyped_framing`.
- **Tone disparity:** Tone gap between descriptions of different groups exceeds
  `tone_gap_threshold` → `bias_fault = tone_disparity`.
- **Agency bias:** Agency term gap between groups exceeds `agency_gap_threshold` →
  `bias_fault = agency_bias`. Agency terms are those that ascribe active decision-making
  or initiative to a subject.
- **Unnecessary attribute reference:** A group attribute is mentioned in the result
  and it is not relevant to the task → `bias_fault = unnecessary_attribute_reference`.
- **Exclusionary language:** Demeaning language score exceeds `demeaning_threshold`
  → `bias_fault = exclusionary_language`. This fault escalates to critical severity.

**Active only when:** `language` mode is active AND result text is present.

**Outcome:** All language faults added to fault register with term-level evidence.

---

### Gate 10 — Counterfactual Fairness Controls

**Purpose:** Test whether swapping a subject's group membership while holding all
other task-relevant features constant changes the decision or score.

**Checks performed:**
- **Counterfactual instability:** Decision changes when group attribute is swapped
  → `bias_fault = counterfactual_instability`.
- **Counterfactual score bias:** Absolute score difference under group swap exceeds
  `counterfactual_tolerance` → `bias_fault = counterfactual_score_bias`.
- **Counterfactual explanation bias:** The explanation changes materially under
  group swap (explanation distance exceeds `explanation_shift_threshold`) →
  `bias_fault = counterfactual_explanation_bias`.
- **Justified difference:** If the counterfactual difference is entirely explained
  by allowed, task-valid feature differences → `bias_state = justified_difference`.
  No fault raised.

**Active only when:** Counterfactual test cases are provided in the submission.

> **DATA NOTE:** Generating counterfactual pairs requires a controlled input generation
> mechanism. Current implementation uses pre-computed counterfactual pairs supplied
> in the submission. Automated generation is flagged for future implementation.
> Tracked as `TODO: DATA_DEPENDENCY`.

**Outcome:** All counterfactual faults added to fault register. Justified differences
are logged and excluded from fault scoring.

---

### Gate 11 — Proxy / Leakage Controls

**Purpose:** Detect features that act as proxies for protected attributes or that
leak protected attribute information into the result.

**Checks performed:**
- **Sensitive proxy risk:** A geographic or demographic feature's proxy score for
  a protected attribute exceeds `proxy_threshold` →
  `bias_fault = sensitive_proxy_risk`.
- **Name proxy bias:** Name-based signal importance exceeds `name_bias_threshold`
  → `bias_fault = name_proxy_bias`.
- **Linguistic proxy bias:** A language style feature's proxy score for a demographic
  attribute exceeds `style_proxy_threshold` → `bias_fault = linguistic_proxy_bias`.
- **Protected attribute leakage:** Mutual information between the feature set and
  a protected attribute exceeds `leakage_threshold` without task necessity →
  `bias_fault = protected_attribute_leakage`.

**Outcome:** All proxy and leakage faults added to fault register. Proxy faults
also update the Gate 3 group mapping record.

---

### Gate 12 — Context / Justification Controls

**Purpose:** Verify that any disparate outcome has a documented, valid, and
non-circular justification.

**Checks performed:**
- **Unjustified disparity:** An outcome gap is detected AND no justification text
  is present → `bias_fault = unjustified_disparity`.
- **Prohibited justification:** The justification text contains a prohibited basis
  (e.g., references a protected attribute as the reason for a disparity) →
  `bias_fault = prohibited_justification`.
- **Weak justification:** The specificity score of the justification text falls
  below `specificity_threshold` → `bias_fault = weak_justification`.
- **Circular historical bias:** The justification basis is identified as
  `legacy_pattern_only` (i.e., "we have always done it this way") →
  `bias_fault = circular_historical_bias`.
- **Justified constraint present:** If a documented legitimate constraint has a
  validity score at or above `validity_threshold` → `bias_state = justified_constraint_present`.
  No fault raised.

**Outcome:** All justification faults added to fault register.

---

### Gate 13 — Temporal Bias Controls

**Purpose:** Detect whether bias is worsening over time, whether prior remediation
efforts have failed, or whether bias emerged after deployment.

**Checks performed:**
- **Worsening bias trend:** The current disparity gap exceeds the prior measurement
  by more than `drift_margin` → `bias_fault = worsening_bias_trend`.
- **Ineffective bias remediation:** A remediation was previously applied AND the
  current disparity gap still exceeds `residual_gap_threshold` →
  `bias_fault = ineffective_bias_remediation`.
- **Deployment-induced bias:** Post-deployment disparity gap exceeds pre-deployment
  gap by more than `deployment_gap_threshold` →
  `bias_fault = deployment_induced_bias`.
- **Fairness drift:** Distribution distance between current group metrics and
  baseline group metrics exceeds `fairness_drift_threshold` →
  `bias_fault = fairness_drift`.

**Outcome:** All temporal bias faults added to fault register with gap values
and timestamps.

---

### Gate 14 — Severity Controls

**Purpose:** Assign a severity level to the overall bias fault picture.

**Severity assignment (evaluated in priority order — highest wins):**

| Condition | Severity |
|-----------|----------|
| Any fault with `harm_risk > harm_threshold` | `critical` |
| `count(medium_bias_faults) >= escalation_count` | `escalated_high` |
| `repeat_bias_pattern_count(pattern_i) > systemic_threshold` | `systemic` |
| Any fault with `bias_impact_scope = decision_output` | `high` |
| Any fault with `bias_impact_scope = ranking_or_exposure` | `medium` |
| All faults with `bias_impact_scope = language_only` | `low` |
| No faults detected | `none` |

**Outcome:** Maximum severity level and per-fault severity tags written to decision log.

---

### Gate 15 — Root Cause Attribution Controls

**Purpose:** Attribute the detected bias faults to one or more root causes.

**Attribution rules (all applicable root causes are recorded):**
- **Sampling bias:** Any representation or selection fault is present →
  `root_cause = sampling_bias`.
- **Annotation bias:** Any label fault is present → `root_cause = annotation_bias`.
- **Feature bias:** Any proxy or leakage fault is present → `root_cause = feature_bias`.
- **Decision threshold bias:** Any threshold bias fault is present →
  `root_cause = decision_threshold_bias`.
- **Rule design bias:** A hard rule produces an unjustified disparity →
  `root_cause = rule_design_bias`.
- **Unresolved:** Maximum root cause probability falls below
  `root_cause_confidence_threshold` → `root_cause = unresolved`.

**Outcome:** All applicable root causes recorded. Unresolved root cause propagates
to Gate 19 (`output_action = request_investigation`).

---

### Gate 16 — Action Classification

**Purpose:** Combine all gate results into a single classification decision.

**Classification rules (evaluated in priority order):**

| Condition | Classification |
|-----------|---------------|
| `critical_bias_count > 0` | `block_output` |
| `severity = systemic` | `escalate_systemic_bias_issue` |
| `root_cause = unresolved` AND `max_severity >= medium` | `manual_fairness_investigation` |
| `max_severity = high` | `fail_bias_audit` |
| `max_severity = medium` | `fairness_review_recommended` |
| `max_severity = low` | `pass_with_bias_warning` |
| `high_severity_bias_count = 0` AND `critical_bias_count = 0` AND `total_bias_score < accept_threshold` | `pass` |

**Outcome:** Classification label written to decision log. Used by Gates 17 and 19.

---

### Gate 17 — Remediation Controls

**Purpose:** Assign a remediation directive for each active bias fault type.
Directives are recommendations; `block_output` → `suppress_release` is immediate.

**Remediation mapping:**

| Bias Fault | Remediation Directive |
|------------|-----------------------|
| `underrepresentation` / `overrepresentation` | `rebalance_or_reweight_sample` |
| `selection_bias` / `access_bias` | `redesign_selection_process` |
| `inconsistent_labeling` / `severity_label_bias` | `relabel_with_revised_guidelines` |
| `sensitive_proxy_risk` / `name_proxy_bias` / `linguistic_proxy_bias` / `protected_attribute_leakage` | `remove_or_constrain_proxy_features` |
| `threshold_bias` | `recalibrate_thresholds` |
| `loaded_language` / `stereotyped_framing` / `unnecessary_attribute_reference` / `exclusionary_language` | `rewrite_output_with_neutral_language_rules` |
| Classification = `block_output` | `suppress_release` |

Multiple faults produce multiple directives. `suppress_release` takes precedence
when classification is `block_output`.

**Outcome:** Remediation directive list written to decision log.

---

### Gate 18 — Evaluation Loop

**Purpose:** Store the bias audit record, update the bias pattern library with
confirmed bias patterns, and adjust thresholds based on observed false positive
and missed bias rates.

**Actions performed:**
- **Bias audit record storage:** All classifications are stored in the bias audit
  record store.
- **Pattern library update:** If manual review confirms bias → `bias_pattern_library`
  is updated.
- **False alarm adjustment:** If manual review identifies a false positive →
  bias thresholds are adjusted to reduce future false positives.
- **Missed bias tightening:** If missed bias rate exceeds `missed_bias_threshold`
  → bias detection rules are tightened.
- **False positive relaxation:** If false positive rate exceeds
  `false_positive_threshold` → bias detection rules are relaxed.
- **Resolution tracking:** Post-remediation audit `pass` → `state = resolved`.
  Post-remediation `fail_bias_audit` or `block_output` → `state = unresolved_bias`.

**Outcome:** Bias audit record stored. Threshold adjustments logged.

---

### Gate 19 — Output Controls

**Purpose:** Determine the final output action based on classification.

**Output action by classification:**

| Classification | Output Action |
|----------------|--------------|
| `pass` | `approve_result` |
| `pass_with_bias_warning` | `approve_with_fairness_warning` |
| `fairness_review_recommended` | `send_to_fairness_review` |
| `fail_bias_audit` | `reject_result` |
| `block_output` | `block_downstream_use` |
| `escalate_systemic_bias_issue` | `trigger_systemic_fairness_escalation` |
| `manual_fairness_investigation` | `request_investigation` |

**Outcome:** Output action written to decision log. Audit result dispatched.

---

### Gate 20 — Final Composite Bias Score

**Purpose:** Compute a single weighted bias score that summarises fault severity
across all seven bias fault categories.

**Score formula:**

```
bias_score = (w1 × representation_bias)
           + (w2 × selection_bias)
           + (w3 × label_bias)
           + (w4 × measurement_bias)
           + (w5 × outcome_bias)
           + (w6 × language_bias)
           + (w7 × proxy_bias)
```

A higher bias score indicates a more severely biased result. Score is normalised
to [0.0, 1.0].

**Default weights:**

| Component | Weight |
|-----------|--------|
| representation_bias | 0.20 |
| selection_bias | 0.20 |
| label_bias | 0.15 |
| measurement_bias | 0.15 |
| outcome_bias | 0.15 |
| language_bias | 0.10 |
| proxy_bias | 0.05 |

All weights are configurable module-level constants.

**Final bias signal rules (evaluated in priority order):**

| Condition | Final Bias Signal |
|-----------|------------------|
| `critical_bias_count > 0` | `blocked` |
| `classification = escalate_systemic_bias_issue` | `systemic_bias_failure` |
| `bias_score >= fail_threshold` | `biased` |
| `warning_threshold <= bias_score < fail_threshold` | `warning` |
| `bias_score < warning_threshold` | `clean` |

**Outcome:** Composite bias score and final bias signal written to decision log
and to the output record.

---

## 6. Bias Audit Decision Log

Every result that advances past Gate 1 produces a `BiasAuditDecisionLog` entry.

**Log format:** Human-readable structured text + machine-readable JSON record.

**Contents per decision:**
- Run timestamp and result identifier
- Bias audit modes active
- Each gate's name, inputs evaluated, faults detected, severity, and reason code
- Full bias fault register (all fault states, group names, gap values)
- Root cause list
- Classification and remediation directives
- Final bias signal and composite bias score

> **FLAG — LOG WRITE LOCATION:** Currently written via `db_helpers.DB.log_event()`
> to the `system_log` table. A dedicated `bias_audit_decisions` table is recommended
> to support regulatory export and fairness reporting. Tracked as future work item.

---

## 7. Controls Not Yet Implemented (Data Dependencies)

| Control | Dependency | Status |
|---------|-----------|--------|
| Automated counterfactual pair generation | Controlled input generation mechanism | TODO: DATA_DEPENDENCY |
| Per-group ground truth quality scoring | Labeled historical outcome data | TODO: DATA_DEPENDENCY |
| Proxy score computation (automated) | Feature correlation analysis at scale | TODO: DATA_DEPENDENCY |
| Language tone and agency scoring | NLP scoring pipeline | TODO: DATA_DEPENDENCY |
| Fairness drift baseline (historical) | Accumulated bias audit history | TODO: DATA_DEPENDENCY |
| Bias pattern library | Populated from confirmed bias reviews | TODO: DATA_DEPENDENCY |
| Post-remediation feedback loop | Remediation pipeline integration | TODO: DATA_DEPENDENCY |

---

## 8. What Agent 6 Does Not Do

- Agent 6 does not use any AI language model to detect or classify bias.
- Agent 6 does not modify the result payload it is auditing.
- Agent 6 does not automatically apply remediation actions. Directives are
  recommendations only, except that `block_output` suppresses downstream release.
- Agent 6 does not make trade decisions or generate market signals.
- Agent 6 does not send communications directly. All output is written to the
  company database via `db_helpers`. Escalations route through the company node
  notification pipeline (Scoop agent).

---

## 9. Human Oversight Points

| Condition | System Action | Human Action Required |
|-----------|--------------|----------------------|
| `classification = block_output` | Downstream release suppressed | Review bias fault before releasing result |
| `classification = escalate_systemic_bias_issue` | Escalation triggered | Investigate pipeline segment producing pattern |
| `classification = manual_fairness_investigation` | Flagged; not auto-resolved | Human review of unresolved root cause |
| `bias_fault = biased_ground_truth` | High-severity fault logged | Review ground truth labeling process |
| `bias_fault = exclusionary_language` | Critical fault logged; release suppressed | Review output text and rewrite |
| `state = unresolved_bias` (post-remediation) | Logged | Escalate — remediation did not resolve bias |
| `bias_fault = circular_historical_bias` | Logged | Review whether justification is substantively valid |
