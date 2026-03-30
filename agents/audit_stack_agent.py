#!/usr/bin/env python3
"""
audit_stack_agent.py — Agent 7
Multi-lane audit stack for result quality evaluation.
18-section classification spine. Deterministic, rule-based, no ML/AI inference.

Sections 1-2  : Intake and lane activation controls.
Sections 3-8  : Six independent audit lanes (correctness, compliance, anomaly,
                bias, robustness, traceability). All non-halting after Section 1.
Sections 9-18 : Aggregation, severity, scoring, classification, root cause
                fusion, remediation routing, output controls, feedback loop,
                and final composite scoring.
"""

import sys
import os
import json
import logging
import argparse
import datetime
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("audit_stack_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

# Default active lanes when none are specified by the caller
DEFAULT_ACTIVE_LANES = {"correctness", "compliance", "anomaly", "bias", "robustness"}

# Correctness lane
CORRECTNESS_TOLERANCE   = 0.05    # max allowed distance from ground truth
NUMERIC_TOLERANCE       = 0.01    # max allowed absolute numeric deviation

# Anomaly lane
OUTLIER_THRESHOLD       = 3.0     # z-score magnitude that flags statistical outlier
MIN_VARIANCE_THRESHOLD  = 1e-6    # batch variance below this flags suspicious uniformity
DRIFT_THRESHOLD         = 0.10    # distribution distance above this flags drift
MEAN_SHIFT_THRESHOLD    = 0.05    # absolute mean shift above this flags mean_shift
CLUSTER_THRESHOLD       = 0.40    # fault density above this flags clustered_failure

# Bias lane
REPRESENTATION_GAP_THRESHOLD    = 0.10   # group representation gap threshold
SELECTION_GAP_THRESHOLD         = 0.10   # selection probability gap threshold
FAIRNESS_ERROR_GAP_THRESHOLD    = 0.05   # max group error rate gap (FPR/FNR)
PROXY_THRESHOLD                 = 0.70   # proxy risk score above this flags proxy_bias
LANGUAGE_BIAS_THRESHOLD         = 0.30   # language bias score above this flags language_bias

# Robustness lane
REPRODUCIBILITY_THRESHOLD  = 0.05    # repeat-run variance above this flags non_reproducible
INSTABILITY_THRESHOLD      = 0.15    # perturbation sensitivity above this flags unstable_result
ADVERSARIAL_THRESHOLD      = 0.10    # attack success rate above this flags adversarial_vulnerability

# Traceability lane
FRESHNESS_LIMIT_HOURS  = 24     # source older than this flags stale_source
SUPPORT_THRESHOLD      = 0.60   # citation support score below this is unsupported

# Severity controls
HARM_THRESHOLD      = 0.70   # harm_risk above this overrides all faults to critical
SYSTEMIC_THRESHOLD  = 1      # repeat fault count above this overrides to systemic

# Cross-lane confidence
CONFIDENCE_THRESHOLD = 0.60   # correctness_confidence below this triggers unverifiable_correctness

# Lane classification thresholds
LANE_WARNING_THRESHOLD = 0.25
LANE_FAIL_THRESHOLD    = 0.50

# Stack composite thresholds
STACK_WARNING_THRESHOLD = 0.20
STACK_FAIL_THRESHOLD    = 0.45

# Root cause fusion
FUSION_THRESHOLD            = 2     # minimum votes for a root cause to win
FUSION_CONFIDENCE_THRESHOLD = 0.40  # minimum vote share for a root cause to win

# Feedback loop recurrence
RECURRENCE_THRESHOLD = 3

# Stack composite weights (must sum to 1.0)
A1_CORRECTNESS  = 0.25
A2_COMPLIANCE   = 0.25
A3_ANOMALY      = 0.15
A4_BIAS         = 0.15
A5_ROBUSTNESS   = 0.10
A6_TRACEABILITY = 0.10

# Severity weights for lane scoring
SEVERITY_WEIGHT = {
    "critical": 1.00,
    "systemic": 1.00,
    "high":     0.70,
    "medium":   0.40,
    "low":      0.15,
}

# ============================================================
# FAULT CATALOGUE
# Maps fault_type → (impact_scope, default_severity)
# ============================================================

FAULT_SCOPE = {
    # Correctness
    "incorrect_result":           ("decision_output",           "high"),
    "numeric_error":              ("decision_output",           "high"),
    "misclassification":          ("decision_output",           "high"),
    "reconciliation_error":       ("decision_output",           "high"),
    "temporal_inconsistency":     ("explanation_or_visibility", "medium"),
    # Compliance
    "hard_rule_violation":        ("decision_output",           "critical"),
    "prohibited_output":          ("decision_output",           "critical"),
    "missing_disclaimer":         ("presentation_only",         "medium"),
    "audit_trail_failure":        ("explanation_or_visibility", "high"),
    "restricted_recommendation":  ("decision_output",           "critical"),
    # Anomaly
    "statistical_outlier":        ("decision_output",           "medium"),
    "suspicious_uniformity":      ("explanation_or_visibility", "medium"),
    "distribution_drift":         ("decision_output",           "high"),
    "mean_shift":                 ("decision_output",           "medium"),
    "clustered_failure":          ("decision_output",           "high"),
    # Bias
    "representation_bias":        ("decision_output",           "high"),
    "selection_bias":             ("decision_output",           "high"),
    "outcome_bias":               ("decision_output",           "critical"),
    "proxy_bias":                 ("decision_output",           "high"),
    "language_bias":              ("explanation_or_visibility", "medium"),
    # Robustness
    "non_reproducible":           ("decision_output",           "high"),
    "unstable_result":            ("decision_output",           "high"),
    "boundary_failure":           ("decision_output",           "medium"),
    "adversarial_vulnerability":  ("decision_output",           "critical"),
    "brittle_dependency":         ("explanation_or_visibility", "medium"),
    # Traceability
    "missing_provenance":         ("explanation_or_visibility", "high"),
    "stale_source":               ("decision_output",           "medium"),
    "missing_citation":           ("explanation_or_visibility", "low"),
    "unsupported_citation":       ("explanation_or_visibility", "medium"),
    "unresolved_source_conflict": ("decision_output",           "high"),
}

# Maps fault_type → root cause category for Section 14 fusion voting
FAULT_ROOT_CAUSE = {
    "incorrect_result":           "input_failure",
    "numeric_error":              "input_failure",
    "misclassification":          "rule_design",
    "reconciliation_error":       "input_failure",
    "temporal_inconsistency":     "input_failure",
    "hard_rule_violation":        "rule_design",
    "prohibited_output":          "rule_design",
    "missing_disclaimer":         "rule_design",
    "audit_trail_failure":        "rule_design",
    "restricted_recommendation":  "rule_design",
    "statistical_outlier":        "model_instability",
    "suspicious_uniformity":      "model_instability",
    "distribution_drift":         "stale_data",
    "mean_shift":                 "stale_data",
    "clustered_failure":          "model_instability",
    "representation_bias":        "input_failure",
    "selection_bias":             "input_failure",
    "outcome_bias":               "rule_design",
    "proxy_bias":                 "rule_design",
    "language_bias":              "rule_design",
    "non_reproducible":           "model_instability",
    "unstable_result":            "model_instability",
    "boundary_failure":           "model_instability",
    "adversarial_vulnerability":  "model_instability",
    "brittle_dependency":         "model_instability",
    "missing_provenance":         "stale_data",
    "stale_source":               "stale_data",
    "missing_citation":           "stale_data",
    "unsupported_citation":       "stale_data",
    "unresolved_source_conflict": "stale_data",
}

# ============================================================
# STACK DECISION LOG
# ============================================================

class StackDecisionLog:
    """
    Accumulates all section records and lane faults produced during a
    single audit_stack() invocation. Serialises to JSON and human-readable text.
    """

    def __init__(self, submission_id: str):
        self.submission_id  = submission_id
        self.timestamp      = datetime.datetime.utcnow().isoformat()
        self.records        = []

        # Fault lists per lane
        self.lane_faults = {
            "correctness":  [],
            "compliance":   [],
            "anomaly":      [],
            "bias":         [],
            "robustness":   [],
            "traceability": [],
        }

        # Severity dict per lane: fault_type → severity string
        self.lane_severities = {k: {} for k in self.lane_faults}

        self.lane_scores          = {}    # lane → float score 0.0–1.0
        self.lane_states          = {}    # lane → state string
        self.stack_state          = "initialized"
        self.cross_lane_flags     = []    # list of flag strings from Section 9
        self.master_classification = None
        self.master_root_cause    = None
        self.remediation_route    = []
        self.output_action        = []
        self.stack_score          = None
        self.final_stack_signal   = None
        self.halted               = False
        self.halt_reason          = None

    def record(self, section_num: int, section_name: str,
               inputs: dict, result: str, reason_code: str):
        self.records.append({
            "section":     section_num,
            "name":        section_name,
            "inputs":      inputs,
            "result":      result,
            "reason_code": reason_code,
            "ts":          datetime.datetime.utcnow().isoformat(),
        })

    def add_lane_fault(self, lane: str, fault_type: str, fault_inputs: dict):
        self.lane_faults[lane].append({
            "fault_type": fault_type,
            "inputs":     fault_inputs,
            "ts":         datetime.datetime.utcnow().isoformat(),
        })

    def halt(self, section_num: int, reason_code: str, inputs: dict) -> dict:
        self.halted      = True
        self.halt_reason = reason_code
        self.record(section_num, "HALT", inputs, "halted", reason_code)
        return {"halted": True, "reason": reason_code}

    def to_dict(self) -> dict:
        return {
            "submission_id":        self.submission_id,
            "timestamp":            self.timestamp,
            "stack_state":          self.stack_state,
            "cross_lane_flags":     self.cross_lane_flags,
            "lane_faults":          self.lane_faults,
            "lane_severities":      self.lane_severities,
            "lane_scores":          self.lane_scores,
            "lane_states":          self.lane_states,
            "master_classification": self.master_classification,
            "master_root_cause":    self.master_root_cause,
            "remediation_route":    self.remediation_route,
            "output_action":        self.output_action,
            "stack_score":          self.stack_score,
            "final_stack_signal":   self.final_stack_signal,
            "halted":               self.halted,
            "halt_reason":          self.halt_reason,
            "decision_log":         self.records,
        }

    def to_human_readable(self) -> str:
        lines = [
            "=" * 72,
            "AUDIT STACK DECISION LOG",
            f"Submission ID  : {self.submission_id}",
            f"Timestamp      : {self.timestamp}",
            f"Stack State    : {self.stack_state}",
            "=" * 72,
        ]
        if self.cross_lane_flags:
            lines.append(f"\nCross-Lane Flags : {', '.join(self.cross_lane_flags)}")
        lines.append("\nLane Results:")
        for lane in ["correctness", "compliance", "anomaly", "bias", "robustness", "traceability"]:
            state  = self.lane_states.get(lane, "inactive")
            score  = self.lane_scores.get(lane, "N/A")
            faults = [f["fault_type"] for f in self.lane_faults.get(lane, [])]
            lines.append(f"  [{lane.upper():14s}] state={state:10s} score={str(score):6s}  faults={faults}")
        lines.append(f"\nMaster Classification : {self.master_classification}")
        lines.append(f"Master Root Cause     : {self.master_root_cause}")
        lines.append(f"Remediation Route     : {self.remediation_route}")
        lines.append(f"Output Action         : {self.output_action}")
        lines.append(f"Stack Score           : {self.stack_score}")
        lines.append(f"Final Stack Signal    : {self.final_stack_signal}")
        lines.append("\nSection Records:")
        for r in self.records:
            lines.append(f"  S{r['section']:02d} {r['name']}: result={r['result']}  reason={r['reason_code']}")
        lines.append("=" * 72)
        return "\n".join(lines)


# ============================================================
# SECTION 1 — INTAKE ROUTER
# ============================================================

def section_1_intake_router(submission: dict, dlog: StackDecisionLog) -> bool:
    """
    SECTION 1 — INTAKE ROUTER  [HALTING]
    Validates payload presence and schema. Sets active_lanes on submission dict.
    Returns True if processing should continue; False if the run halts here.
    """
    result_payload = submission.get("result_payload")

    # Rule: Missing payload
    if result_payload is None:
        dlog.stack_state = "reject_input"
        dlog.halt(1, "reject_input", {"result_payload": None})
        return False

    # Rule: Schema validation
    if not _validate_schema(result_payload):
        dlog.stack_state = "invalid_structure"
        dlog.halt(1, "invalid_structure",
                  {"schema_validation": False, "result_payload_type": type(result_payload).__name__})
        return False

    # Rule: Lane selection
    requested_lanes = submission.get("requested_lanes")
    if requested_lanes is not None:
        submission["active_lanes"] = set(requested_lanes)
        dlog.record(1, "intake_router",
                    {"requested_lanes": list(requested_lanes)},
                    "lanes_from_request", "requested_lanes_applied")
    else:
        submission["active_lanes"] = set(DEFAULT_ACTIVE_LANES)
        dlog.record(1, "intake_router",
                    {"requested_lanes": None, "active_lanes": list(DEFAULT_ACTIVE_LANES)},
                    "lanes_default", "default_lanes_applied")

    # Rule: Reference data availability
    reference_payload = submission.get("reference_payload")
    if reference_payload is not None:
        dlog.stack_state = "reference_enabled"
        dlog.record(1, "intake_router",
                    {"reference_payload": "present"},
                    "reference_enabled", "reference_data_available")
    else:
        dlog.stack_state = "reference_limited"
        dlog.record(1, "intake_router",
                    {"reference_payload": None},
                    "reference_limited", "no_reference_data")

    return True


def _validate_schema(payload: Any) -> bool:
    """Schema validation: payload must be a non-empty dict."""
    if not isinstance(payload, dict):
        return False
    if len(payload) == 0:
        return False
    return True


# ============================================================
# SECTION 2 — LANE ACTIVATION CONTROLS
# ============================================================

def section_2_lane_activation(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 2 — LANE ACTIVATION CONTROLS
    Sets each lane to active or inactive based on the active_lanes set.
    """
    active_lanes = submission.get("active_lanes", DEFAULT_ACTIVE_LANES)
    all_lanes = ["correctness", "compliance", "anomaly", "bias", "robustness", "traceability"]

    for lane in all_lanes:
        if lane in active_lanes:
            dlog.lane_states[lane] = "active"
            dlog.record(2, "lane_activation",
                        {"lane": lane, "in_active_lanes": True},
                        "active", f"lane_{lane}_enabled")
        else:
            dlog.lane_states[lane] = "inactive"
            dlog.record(2, "lane_activation",
                        {"lane": lane, "in_active_lanes": False},
                        "inactive", f"lane_{lane}_disabled")


# ============================================================
# SECTION 3 — CORRECTNESS LANE
# ============================================================

def section_3_correctness_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 3 — CORRECTNESS LANE
    Checks result accuracy against ground truth and expected values.
    Five checks; all non-halting. Each check is skipped if required inputs are absent.
    """
    if dlog.lane_states.get("correctness") != "active":
        return

    payload = submission.get("result_payload", {})
    ref     = submission.get("reference_payload") or {}
    ctx     = submission.get("audit_context", {})

    result_value     = payload.get("result_value")
    ground_truth     = ref.get("ground_truth_value")

    # Check 1: Ground truth distance
    if result_value is not None and ground_truth is not None:
        try:
            dist = abs(float(result_value) - float(ground_truth))
            if dist > CORRECTNESS_TOLERANCE:
                dlog.add_lane_fault("correctness", "incorrect_result", {
                    "result_value":      result_value,
                    "ground_truth_value": ground_truth,
                    "distance":          dist,
                    "threshold":         CORRECTNESS_TOLERANCE,
                })
                dlog.record(3, "correctness_ground_truth",
                            {"dist": dist, "threshold": CORRECTNESS_TOLERANCE},
                            "fault", "incorrect_result")
            else:
                dlog.record(3, "correctness_ground_truth",
                            {"dist": dist, "threshold": CORRECTNESS_TOLERANCE},
                            "pass", "within_tolerance")
        except (TypeError, ValueError):
            dlog.record(3, "correctness_ground_truth",
                        {"result_value": result_value, "ground_truth": ground_truth},
                        "skip", "non_numeric_values")

    # Check 2: Numeric deviation from expected
    expected_value = ctx.get("expected_value")
    if result_value is not None and expected_value is not None:
        try:
            dev = abs(float(result_value) - float(expected_value))
            if dev > NUMERIC_TOLERANCE:
                dlog.add_lane_fault("correctness", "numeric_error", {
                    "result_value":  result_value,
                    "expected_value": expected_value,
                    "deviation":     dev,
                    "threshold":     NUMERIC_TOLERANCE,
                })
                dlog.record(3, "correctness_numeric",
                            {"deviation": dev, "threshold": NUMERIC_TOLERANCE},
                            "fault", "numeric_error")
            else:
                dlog.record(3, "correctness_numeric",
                            {"deviation": dev},
                            "pass", "within_numeric_tolerance")
        except (TypeError, ValueError):
            dlog.record(3, "correctness_numeric",
                        {"result_value": result_value, "expected_value": expected_value},
                        "skip", "non_numeric_values")

    # Check 3: Label classification
    predicted_class = payload.get("predicted_class")
    actual_class    = ref.get("actual_class") or ctx.get("actual_class")
    if predicted_class is not None and actual_class is not None:
        if predicted_class != actual_class:
            dlog.add_lane_fault("correctness", "misclassification", {
                "predicted_class": predicted_class,
                "actual_class":    actual_class,
            })
            dlog.record(3, "correctness_label",
                        {"predicted": predicted_class, "actual": actual_class},
                        "fault", "misclassification")
        else:
            dlog.record(3, "correctness_label",
                        {"predicted": predicted_class, "actual": actual_class},
                        "pass", "labels_match")

    # Check 4: Reconciliation
    reported_total   = payload.get("reported_total")
    component_values = payload.get("component_values")
    if reported_total is not None and isinstance(component_values, list) and len(component_values) > 0:
        try:
            computed_sum = sum(float(v) for v in component_values)
            if abs(float(reported_total) - computed_sum) > NUMERIC_TOLERANCE:
                dlog.add_lane_fault("correctness", "reconciliation_error", {
                    "reported_total": reported_total,
                    "computed_sum":   computed_sum,
                    "gap":            abs(float(reported_total) - computed_sum),
                })
                dlog.record(3, "correctness_reconciliation",
                            {"reported_total": reported_total, "computed_sum": computed_sum},
                            "fault", "reconciliation_error")
            else:
                dlog.record(3, "correctness_reconciliation",
                            {"reported_total": reported_total, "computed_sum": computed_sum},
                            "pass", "totals_reconcile")
        except (TypeError, ValueError):
            dlog.record(3, "correctness_reconciliation",
                        {"reported_total": reported_total},
                        "skip", "non_numeric_components")

    # Check 5: Chronology
    temporal_order_constraint = ctx.get("temporal_order_constraint")
    if temporal_order_constraint is not None:
        if not temporal_order_constraint:
            dlog.add_lane_fault("correctness", "temporal_inconsistency", {
                "temporal_order_constraint": temporal_order_constraint,
            })
            dlog.record(3, "correctness_temporal",
                        {"temporal_order_constraint": temporal_order_constraint},
                        "fault", "temporal_inconsistency")
        else:
            dlog.record(3, "correctness_temporal",
                        {"temporal_order_constraint": temporal_order_constraint},
                        "pass", "temporal_order_valid")


# ============================================================
# SECTION 4 — COMPLIANCE LANE
# ============================================================

def section_4_compliance_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 4 — COMPLIANCE LANE
    Checks policy rules, prohibited content, disclaimers, audit trail, restricted actions.
    Five checks; all non-halting.
    """
    if dlog.lane_states.get("compliance") != "active":
        return

    ctx = submission.get("audit_context", {})

    # Check 1: Hard rule
    hard_constraint_result = ctx.get("hard_constraint_result")
    if hard_constraint_result is not None:
        if not hard_constraint_result:
            dlog.add_lane_fault("compliance", "hard_rule_violation", {
                "hard_constraint_result": hard_constraint_result,
            })
            dlog.record(4, "compliance_hard_rule",
                        {"hard_constraint_result": hard_constraint_result},
                        "fault", "hard_rule_violation")
        else:
            dlog.record(4, "compliance_hard_rule",
                        {"hard_constraint_result": hard_constraint_result},
                        "pass", "hard_constraint_satisfied")

    # Check 2: Prohibited content
    content_policy_match = ctx.get("content_policy_match", False)
    if content_policy_match:
        dlog.add_lane_fault("compliance", "prohibited_output", {
            "content_policy_match": content_policy_match,
        })
        dlog.record(4, "compliance_content_policy",
                    {"content_policy_match": content_policy_match},
                    "fault", "prohibited_output")
    else:
        dlog.record(4, "compliance_content_policy",
                    {"content_policy_match": content_policy_match},
                    "pass", "no_prohibited_content")

    # Check 3: Disclaimer
    disclaimer_required = ctx.get("disclaimer_required", False)
    disclaimer_present  = ctx.get("disclaimer_present", True)
    if disclaimer_required and not disclaimer_present:
        dlog.add_lane_fault("compliance", "missing_disclaimer", {
            "disclaimer_required": disclaimer_required,
            "disclaimer_present":  disclaimer_present,
        })
        dlog.record(4, "compliance_disclaimer",
                    {"disclaimer_required": disclaimer_required,
                     "disclaimer_present":  disclaimer_present},
                    "fault", "missing_disclaimer")
    else:
        dlog.record(4, "compliance_disclaimer",
                    {"disclaimer_required": disclaimer_required,
                     "disclaimer_present":  disclaimer_present},
                    "pass", "disclaimer_check_clear")

    # Check 4: Audit trail completeness
    trace_log_completeness = ctx.get("trace_log_completeness")
    trace_minimum          = ctx.get("trace_minimum", 0.80)
    if trace_log_completeness is not None:
        if trace_log_completeness < trace_minimum:
            dlog.add_lane_fault("compliance", "audit_trail_failure", {
                "trace_log_completeness": trace_log_completeness,
                "trace_minimum":          trace_minimum,
            })
            dlog.record(4, "compliance_audit_trail",
                        {"trace_log_completeness": trace_log_completeness,
                         "trace_minimum": trace_minimum},
                        "fault", "audit_trail_failure")
        else:
            dlog.record(4, "compliance_audit_trail",
                        {"trace_log_completeness": trace_log_completeness},
                        "pass", "audit_trail_complete")

    # Check 5: Restricted action
    restricted_action_flag = ctx.get("restricted_action_flag", False)
    if restricted_action_flag:
        dlog.add_lane_fault("compliance", "restricted_recommendation", {
            "restricted_action_flag": restricted_action_flag,
        })
        dlog.record(4, "compliance_restricted_action",
                    {"restricted_action_flag": restricted_action_flag},
                    "fault", "restricted_recommendation")
    else:
        dlog.record(4, "compliance_restricted_action",
                    {"restricted_action_flag": restricted_action_flag},
                    "pass", "no_restricted_action")


# ============================================================
# SECTION 5 — ANOMALY LANE
# ============================================================

def section_5_anomaly_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 5 — ANOMALY LANE
    Statistical outlier, variance collapse, distribution drift, mean shift,
    and clustered failure detection. Five checks; all non-halting.
    All metrics must be pre-computed and supplied in audit_context.
    (TODO:DATA_DEPENDENCY — live distribution store not yet integrated.)
    """
    if dlog.lane_states.get("anomaly") != "active":
        return

    ctx = submission.get("audit_context", {})

    # Check 1: Statistical outlier
    z_score = ctx.get("z_score")
    if z_score is not None:
        if abs(z_score) > OUTLIER_THRESHOLD:
            dlog.add_lane_fault("anomaly", "statistical_outlier", {
                "z_score":           z_score,
                "outlier_threshold": OUTLIER_THRESHOLD,
            })
            dlog.record(5, "anomaly_outlier",
                        {"z_score": z_score, "threshold": OUTLIER_THRESHOLD},
                        "fault", "statistical_outlier")
        else:
            dlog.record(5, "anomaly_outlier",
                        {"z_score": z_score, "threshold": OUTLIER_THRESHOLD},
                        "pass", "z_score_within_range")

    # Check 2: Variance collapse
    batch_variance = ctx.get("batch_variance")
    if batch_variance is not None:
        if batch_variance < MIN_VARIANCE_THRESHOLD:
            dlog.add_lane_fault("anomaly", "suspicious_uniformity", {
                "batch_variance":       batch_variance,
                "min_variance_threshold": MIN_VARIANCE_THRESHOLD,
            })
            dlog.record(5, "anomaly_variance",
                        {"batch_variance": batch_variance, "threshold": MIN_VARIANCE_THRESHOLD},
                        "fault", "suspicious_uniformity")
        else:
            dlog.record(5, "anomaly_variance",
                        {"batch_variance": batch_variance},
                        "pass", "variance_normal")

    # Check 3: Distribution drift
    distribution_distance = ctx.get("distribution_distance")
    if distribution_distance is not None:
        if distribution_distance > DRIFT_THRESHOLD:
            dlog.add_lane_fault("anomaly", "distribution_drift", {
                "distribution_distance": distribution_distance,
                "drift_threshold":       DRIFT_THRESHOLD,
            })
            dlog.record(5, "anomaly_drift",
                        {"distribution_distance": distribution_distance, "threshold": DRIFT_THRESHOLD},
                        "fault", "distribution_drift")
        else:
            dlog.record(5, "anomaly_drift",
                        {"distribution_distance": distribution_distance},
                        "pass", "no_drift_detected")

    # Check 4: Mean shift
    mean_shift = ctx.get("mean_shift")
    if mean_shift is not None:
        if abs(mean_shift) > MEAN_SHIFT_THRESHOLD:
            dlog.add_lane_fault("anomaly", "mean_shift", {
                "mean_shift": mean_shift,
                "threshold":  MEAN_SHIFT_THRESHOLD,
            })
            dlog.record(5, "anomaly_mean_shift",
                        {"mean_shift": mean_shift, "threshold": MEAN_SHIFT_THRESHOLD},
                        "fault", "mean_shift")
        else:
            dlog.record(5, "anomaly_mean_shift",
                        {"mean_shift": mean_shift},
                        "pass", "mean_stable")

    # Check 5: Clustered failures
    fault_density = ctx.get("fault_density")
    if fault_density is not None:
        if fault_density > CLUSTER_THRESHOLD:
            dlog.add_lane_fault("anomaly", "clustered_failure", {
                "fault_density":   fault_density,
                "cluster_threshold": CLUSTER_THRESHOLD,
            })
            dlog.record(5, "anomaly_clustered_failure",
                        {"fault_density": fault_density, "threshold": CLUSTER_THRESHOLD},
                        "fault", "clustered_failure")
        else:
            dlog.record(5, "anomaly_clustered_failure",
                        {"fault_density": fault_density},
                        "pass", "no_failure_cluster")


# ============================================================
# SECTION 6 — BIAS LANE
# ============================================================

def section_6_bias_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 6 — BIAS LANE
    Representation, selection, outcome, proxy, and language bias checks.
    Five checks; all non-halting.
    All bias metrics must be pre-computed and supplied in audit_context.
    (TODO:DATA_DEPENDENCY — bias measurement pipeline not yet integrated.)
    """
    if dlog.lane_states.get("bias") != "active":
        return

    ctx = submission.get("audit_context", {})

    # Check 1: Representation disparity
    representation_gap = ctx.get("representation_gap")
    if representation_gap is not None:
        if representation_gap > REPRESENTATION_GAP_THRESHOLD:
            dlog.add_lane_fault("bias", "representation_bias", {
                "representation_gap": representation_gap,
                "threshold":          REPRESENTATION_GAP_THRESHOLD,
            })
            dlog.record(6, "bias_representation",
                        {"representation_gap": representation_gap,
                         "threshold": REPRESENTATION_GAP_THRESHOLD},
                        "fault", "representation_bias")
        else:
            dlog.record(6, "bias_representation",
                        {"representation_gap": representation_gap},
                        "pass", "representation_gap_acceptable")

    # Check 2: Selection disparity
    selection_probability_gap = ctx.get("selection_probability_gap")
    if selection_probability_gap is not None:
        if selection_probability_gap > SELECTION_GAP_THRESHOLD:
            dlog.add_lane_fault("bias", "selection_bias", {
                "selection_probability_gap": selection_probability_gap,
                "threshold":                 SELECTION_GAP_THRESHOLD,
            })
            dlog.record(6, "bias_selection",
                        {"selection_probability_gap": selection_probability_gap,
                         "threshold": SELECTION_GAP_THRESHOLD},
                        "fault", "selection_bias")
        else:
            dlog.record(6, "bias_selection",
                        {"selection_probability_gap": selection_probability_gap},
                        "pass", "selection_gap_acceptable")

    # Check 3: Outcome disparity (FPR/FNR gap across groups)
    group_error_rate_gap = ctx.get("group_error_rate_gap")
    if isinstance(group_error_rate_gap, list) and len(group_error_rate_gap) > 0:
        max_gap = max(group_error_rate_gap)
        if max_gap > FAIRNESS_ERROR_GAP_THRESHOLD:
            dlog.add_lane_fault("bias", "outcome_bias", {
                "max_group_error_rate_gap": max_gap,
                "threshold":               FAIRNESS_ERROR_GAP_THRESHOLD,
            })
            dlog.record(6, "bias_outcome",
                        {"max_gap": max_gap, "threshold": FAIRNESS_ERROR_GAP_THRESHOLD},
                        "fault", "outcome_bias")
        else:
            dlog.record(6, "bias_outcome",
                        {"max_gap": max_gap},
                        "pass", "error_rate_gaps_acceptable")

    # Check 4: Proxy feature risk
    proxy_scores = ctx.get("proxy_scores", {})
    if proxy_scores:
        proxy_faults = [(feat, score) for feat, score in proxy_scores.items()
                        if score > PROXY_THRESHOLD]
        if proxy_faults:
            dlog.add_lane_fault("bias", "proxy_bias", {
                "proxy_features": proxy_faults,
                "threshold":      PROXY_THRESHOLD,
            })
            dlog.record(6, "bias_proxy",
                        {"proxy_faults_count": len(proxy_faults), "threshold": PROXY_THRESHOLD},
                        "fault", "proxy_bias")
        else:
            dlog.record(6, "bias_proxy",
                        {"proxy_scores_checked": len(proxy_scores)},
                        "pass", "no_proxy_features_detected")

    # Check 5: Language bias
    language_bias_score = ctx.get("language_bias_score")
    if language_bias_score is not None:
        if language_bias_score > LANGUAGE_BIAS_THRESHOLD:
            dlog.add_lane_fault("bias", "language_bias", {
                "language_bias_score": language_bias_score,
                "threshold":           LANGUAGE_BIAS_THRESHOLD,
            })
            dlog.record(6, "bias_language",
                        {"language_bias_score": language_bias_score,
                         "threshold": LANGUAGE_BIAS_THRESHOLD},
                        "fault", "language_bias")
        else:
            dlog.record(6, "bias_language",
                        {"language_bias_score": language_bias_score},
                        "pass", "language_bias_acceptable")


# ============================================================
# SECTION 7 — ROBUSTNESS LANE
# ============================================================

def section_7_robustness_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 7 — ROBUSTNESS LANE
    Reproducibility, sensitivity, boundary case, adversarial input,
    and brittle dependency checks. Five checks; all non-halting.
    All robustness metrics must be pre-computed and supplied in audit_context.
    (TODO:DATA_DEPENDENCY — robustness test harness not yet integrated.)
    """
    if dlog.lane_states.get("robustness") != "active":
        return

    ctx = submission.get("audit_context", {})

    # Check 1: Reproducibility
    same_input_repeat_variance = ctx.get("same_input_repeat_variance")
    if same_input_repeat_variance is not None:
        if same_input_repeat_variance > REPRODUCIBILITY_THRESHOLD:
            dlog.add_lane_fault("robustness", "non_reproducible", {
                "same_input_repeat_variance": same_input_repeat_variance,
                "threshold":                  REPRODUCIBILITY_THRESHOLD,
            })
            dlog.record(7, "robustness_reproducibility",
                        {"variance": same_input_repeat_variance, "threshold": REPRODUCIBILITY_THRESHOLD},
                        "fault", "non_reproducible")
        else:
            dlog.record(7, "robustness_reproducibility",
                        {"variance": same_input_repeat_variance},
                        "pass", "reproducible")

    # Check 2: Input perturbation sensitivity
    sensitivity_metric = ctx.get("sensitivity_metric")
    if sensitivity_metric is not None:
        if sensitivity_metric > INSTABILITY_THRESHOLD:
            dlog.add_lane_fault("robustness", "unstable_result", {
                "sensitivity_metric": sensitivity_metric,
                "threshold":          INSTABILITY_THRESHOLD,
            })
            dlog.record(7, "robustness_sensitivity",
                        {"sensitivity_metric": sensitivity_metric, "threshold": INSTABILITY_THRESHOLD},
                        "fault", "unstable_result")
        else:
            dlog.record(7, "robustness_sensitivity",
                        {"sensitivity_metric": sensitivity_metric},
                        "pass", "sensitivity_acceptable")

    # Check 3: Boundary cases
    boundary_case_test = ctx.get("boundary_case_test")
    if boundary_case_test is not None:
        if boundary_case_test == "fail":
            dlog.add_lane_fault("robustness", "boundary_failure", {
                "boundary_case_test": boundary_case_test,
            })
            dlog.record(7, "robustness_boundary",
                        {"boundary_case_test": boundary_case_test},
                        "fault", "boundary_failure")
        else:
            dlog.record(7, "robustness_boundary",
                        {"boundary_case_test": boundary_case_test},
                        "pass", "boundary_cases_pass")

    # Check 4: Adversarial input
    attack_case_success_rate = ctx.get("attack_case_success_rate")
    if attack_case_success_rate is not None:
        if attack_case_success_rate > ADVERSARIAL_THRESHOLD:
            dlog.add_lane_fault("robustness", "adversarial_vulnerability", {
                "attack_case_success_rate": attack_case_success_rate,
                "threshold":               ADVERSARIAL_THRESHOLD,
            })
            dlog.record(7, "robustness_adversarial",
                        {"attack_case_success_rate": attack_case_success_rate,
                         "threshold": ADVERSARIAL_THRESHOLD},
                        "fault", "adversarial_vulnerability")
        else:
            dlog.record(7, "robustness_adversarial",
                        {"attack_case_success_rate": attack_case_success_rate},
                        "pass", "adversarial_resilient")

    # Check 5: Brittle dependency
    optional_input_removed = ctx.get("optional_input_removed", False)
    output_invalid         = ctx.get("output_invalid", False)
    if optional_input_removed and output_invalid:
        dlog.add_lane_fault("robustness", "brittle_dependency", {
            "optional_input_removed": optional_input_removed,
            "output_invalid":         output_invalid,
        })
        dlog.record(7, "robustness_dependency",
                    {"optional_input_removed": optional_input_removed,
                     "output_invalid": output_invalid},
                    "fault", "brittle_dependency")
    elif optional_input_removed:
        dlog.record(7, "robustness_dependency",
                    {"optional_input_removed": optional_input_removed,
                     "output_invalid": output_invalid},
                    "pass", "optional_removal_handled_gracefully")


# ============================================================
# SECTION 8 — TRACEABILITY LANE
# ============================================================

def section_8_traceability_lane(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 8 — TRACEABILITY LANE
    Provenance, source freshness, citations, citation support, source conflicts.
    Five checks; all non-halting.
    (TODO:DATA_DEPENDENCY — citation support scoring pipeline not yet integrated.)
    """
    if dlog.lane_states.get("traceability") != "active":
        return

    payload = submission.get("result_payload", {})
    ctx     = submission.get("audit_context", {})

    # Check 1: Provenance
    source_provenance = payload.get("source_provenance")
    if source_provenance is None:
        dlog.add_lane_fault("traceability", "missing_provenance", {
            "source_provenance": None,
        })
        dlog.record(8, "traceability_provenance",
                    {"source_provenance": None},
                    "fault", "missing_provenance")
    else:
        dlog.record(8, "traceability_provenance",
                    {"source_provenance": "present"},
                    "pass", "provenance_present")

    # Check 2: Source freshness
    source_age_hours = ctx.get("source_age_hours")
    if source_age_hours is not None:
        if source_age_hours > FRESHNESS_LIMIT_HOURS:
            dlog.add_lane_fault("traceability", "stale_source", {
                "source_age_hours":    source_age_hours,
                "freshness_limit_hours": FRESHNESS_LIMIT_HOURS,
            })
            dlog.record(8, "traceability_freshness",
                        {"source_age_hours": source_age_hours, "limit": FRESHNESS_LIMIT_HOURS},
                        "fault", "stale_source")
        else:
            dlog.record(8, "traceability_freshness",
                        {"source_age_hours": source_age_hours},
                        "pass", "source_fresh")

    # Check 3: Missing citation
    external_claim_count = ctx.get("external_claim_count", 0)
    cited_claim_count    = ctx.get("cited_claim_count", 0)
    if external_claim_count > cited_claim_count:
        dlog.add_lane_fault("traceability", "missing_citation", {
            "external_claim_count": external_claim_count,
            "cited_claim_count":    cited_claim_count,
            "gap":                  external_claim_count - cited_claim_count,
        })
        dlog.record(8, "traceability_citation",
                    {"external_claim_count": external_claim_count,
                     "cited_claim_count": cited_claim_count},
                    "fault", "missing_citation")
    elif external_claim_count > 0:
        dlog.record(8, "traceability_citation",
                    {"external_claim_count": external_claim_count,
                     "cited_claim_count": cited_claim_count},
                    "pass", "all_claims_cited")

    # Check 4: Citation support scores
    citation_support_scores = ctx.get("citation_support_scores", [])
    if citation_support_scores:
        unsupported = [s for s in citation_support_scores if s < SUPPORT_THRESHOLD]
        if unsupported:
            dlog.add_lane_fault("traceability", "unsupported_citation", {
                "unsupported_count": len(unsupported),
                "threshold":         SUPPORT_THRESHOLD,
                "min_score":         min(unsupported),
            })
            dlog.record(8, "traceability_citation_support",
                        {"unsupported_count": len(unsupported), "threshold": SUPPORT_THRESHOLD},
                        "fault", "unsupported_citation")
        else:
            dlog.record(8, "traceability_citation_support",
                        {"scores_checked": len(citation_support_scores),
                         "all_above_threshold": True},
                        "pass", "citations_supported")

    # Check 5: Conflicting sources
    source_conflict_count = ctx.get("source_conflict_count", 0)
    resolution_note       = ctx.get("resolution_note")
    if source_conflict_count > 0:
        if resolution_note is None:
            dlog.add_lane_fault("traceability", "unresolved_source_conflict", {
                "source_conflict_count": source_conflict_count,
                "resolution_note":       None,
            })
            dlog.record(8, "traceability_conflicts",
                        {"source_conflict_count": source_conflict_count,
                         "resolution_note": None},
                        "fault", "unresolved_source_conflict")
        else:
            dlog.record(8, "traceability_conflicts",
                        {"source_conflict_count": source_conflict_count,
                         "resolution_note": "present"},
                        "pass", "conflicts_documented_and_resolved")


# ============================================================
# SECTION 9 — CROSS-LANE CONFLICT CONTROLS
# ============================================================

def section_9_cross_lane_conflict(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 9 — CROSS-LANE CONFLICT CONTROLS
    Detects compound multi-lane failure patterns. Runs on raw fault counts
    before scoring and classification. Results written to cross_lane_flags.
    """
    ctx = submission.get("audit_context", {})

    # Preliminary: lanes with any faults (before scoring)
    has_fault = {lane: len(faults) > 0 for lane, faults in dlog.lane_faults.items()}
    correctness_confidence = ctx.get("correctness_confidence", 1.0)

    found_any = False

    # Flag 1: Correctness passes but anomaly fails → suspicious_pass
    if not has_fault.get("correctness") and has_fault.get("anomaly"):
        dlog.cross_lane_flags.append("suspicious_pass")
        dlog.record(9, "cross_lane_correctness_anomaly",
                    {"correctness_faults": 0,
                     "anomaly_faults": len(dlog.lane_faults["anomaly"])},
                    "suspicious_pass",
                    "correctness_pass_anomaly_fail")
        found_any = True

    # Flag 2: Both correctness and compliance fail → compound_critical_issue
    if has_fault.get("correctness") and has_fault.get("compliance"):
        dlog.cross_lane_flags.append("compound_critical_issue")
        dlog.record(9, "cross_lane_correctness_compliance",
                    {"correctness_faults": len(dlog.lane_faults["correctness"]),
                     "compliance_faults":  len(dlog.lane_faults["compliance"])},
                    "compound_critical_issue",
                    "correctness_and_compliance_both_fail")
        found_any = True

    # Flag 3: Bias fails but compliance passes → fairness_specific_failure
    if has_fault.get("bias") and not has_fault.get("compliance"):
        dlog.cross_lane_flags.append("fairness_specific_failure")
        dlog.record(9, "cross_lane_bias_compliance",
                    {"bias_faults":       len(dlog.lane_faults["bias"]),
                     "compliance_faults": 0},
                    "fairness_specific_failure",
                    "bias_fail_compliance_pass")
        found_any = True

    # Flag 4: Robustness and anomaly both fail → instability_pattern
    if has_fault.get("robustness") and has_fault.get("anomaly"):
        dlog.cross_lane_flags.append("instability_pattern")
        dlog.record(9, "cross_lane_robustness_anomaly",
                    {"robustness_faults": len(dlog.lane_faults["robustness"]),
                     "anomaly_faults":    len(dlog.lane_faults["anomaly"])},
                    "instability_pattern",
                    "robustness_and_anomaly_both_fail")
        found_any = True

    # Flag 5: Traceability fails and correctness confidence low → unverifiable_correctness
    if has_fault.get("traceability") and correctness_confidence < CONFIDENCE_THRESHOLD:
        dlog.cross_lane_flags.append("unverifiable_correctness")
        dlog.record(9, "cross_lane_traceability_correctness",
                    {"traceability_faults":      len(dlog.lane_faults["traceability"]),
                     "correctness_confidence":   correctness_confidence,
                     "confidence_threshold":     CONFIDENCE_THRESHOLD},
                    "unverifiable_correctness",
                    "traceability_fail_low_correctness_confidence")
        found_any = True

    if not found_any:
        dlog.record(9, "cross_lane_conflict",
                    {"lanes_with_faults": [k for k, v in has_fault.items() if v]},
                    "no_flags",
                    "no_cross_lane_conflicts_detected")


# ============================================================
# SECTION 10 — LANE SEVERITY CONTROLS
# ============================================================

def section_10_lane_severity(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 10 — LANE SEVERITY CONTROLS
    Assigns severity to each detected fault.
    Priority: harm_risk/policy_breach override → default scope severity → systemic repeat override.
    """
    ctx           = submission.get("audit_context", {})
    harm_risk     = ctx.get("harm_risk", 0.0)
    policy_breach = ctx.get("policy_breach", False)
    repeat_lane_fault_counts = ctx.get("repeat_lane_fault_counts", {})

    for lane, faults in dlog.lane_faults.items():
        for fault_entry in faults:
            fault_type = fault_entry["fault_type"]
            impact_scope, default_severity = FAULT_SCOPE.get(fault_type, ("unknown", "medium"))

            # Priority 1: Harm risk or policy breach overrides to critical
            if harm_risk > HARM_THRESHOLD or policy_breach:
                severity = "critical"
                reason   = ("harm_risk_override" if harm_risk > HARM_THRESHOLD
                            else "policy_breach_override")
            else:
                severity = default_severity
                reason   = f"default_scope_{impact_scope}"

            # Priority 2 (after critical): Systemic repeat pattern overrides to systemic
            repeat_count = repeat_lane_fault_counts.get(fault_type, 0)
            if repeat_count > SYSTEMIC_THRESHOLD and severity != "critical":
                severity = "systemic"
                reason   = "systemic_repeat_pattern"

            dlog.lane_severities[lane][fault_type] = severity
            dlog.record(10, "lane_severity",
                        {"lane": lane, "fault": fault_type,
                         "impact_scope": impact_scope,
                         "harm_risk": harm_risk,
                         "repeat_count": repeat_count},
                        severity, reason)


# ============================================================
# SECTION 11 — LANE SCORING CONTROLS
# ============================================================

def section_11_lane_scoring(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 11 — LANE SCORING CONTROLS
    Computes a normalized score per active lane as the weighted sum of
    fault severities, capped at 1.0.
    """
    active_lanes = submission.get("active_lanes", DEFAULT_ACTIVE_LANES)

    for lane in active_lanes:
        if dlog.lane_states.get(lane) != "active":
            continue

        faults     = dlog.lane_faults.get(lane, [])
        severities = dlog.lane_severities.get(lane, {})

        raw_score = 0.0
        for fault_entry in faults:
            fault_type = fault_entry["fault_type"]
            severity   = severities.get(fault_type, "medium")
            raw_score += SEVERITY_WEIGHT.get(severity, 0.40)

        lane_score = round(min(raw_score, 1.0), 4)
        dlog.lane_scores[lane] = lane_score
        dlog.record(11, "lane_scoring",
                    {"lane": lane, "fault_count": len(faults), "raw_score": raw_score},
                    str(lane_score), f"lane_{lane}_scored")


# ============================================================
# SECTION 12 — LANE CLASSIFICATION CONTROLS
# ============================================================

def section_12_lane_classification(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 12 — LANE CLASSIFICATION CONTROLS
    Assigns state to each active lane.
    Priority: systemic (escalate) > critical (block) > score-based fail/warning/pass.
    """
    active_lanes = submission.get("active_lanes", DEFAULT_ACTIVE_LANES)

    for lane in active_lanes:
        if dlog.lane_states.get(lane) != "active":
            continue

        score      = dlog.lane_scores.get(lane, 0.0)
        severities = dlog.lane_severities.get(lane, {})

        critical_count = sum(1 for s in severities.values() if s == "critical")
        systemic_count = sum(1 for s in severities.values() if s == "systemic")

        if systemic_count > 0:
            dlog.lane_states[lane] = "escalate"
            dlog.record(12, "lane_classification",
                        {"lane": lane, "systemic_count": systemic_count},
                        "escalate", "systemic_fault_override")
        elif critical_count > 0:
            dlog.lane_states[lane] = "block"
            dlog.record(12, "lane_classification",
                        {"lane": lane, "critical_count": critical_count},
                        "block", "critical_fault_override")
        elif score >= LANE_FAIL_THRESHOLD:
            dlog.lane_states[lane] = "fail"
            dlog.record(12, "lane_classification",
                        {"lane": lane, "score": score, "fail_threshold": LANE_FAIL_THRESHOLD},
                        "fail", "score_above_fail_threshold")
        elif score >= LANE_WARNING_THRESHOLD:
            dlog.lane_states[lane] = "warning"
            dlog.record(12, "lane_classification",
                        {"lane": lane, "score": score,
                         "warning_threshold": LANE_WARNING_THRESHOLD},
                        "warning", "score_above_warning_threshold")
        else:
            dlog.lane_states[lane] = "pass"
            dlog.record(12, "lane_classification",
                        {"lane": lane, "score": score},
                        "pass", "score_below_warning_threshold")


# ============================================================
# SECTION 13 — MASTER AGGREGATION CONTROLS
# ============================================================

def section_13_master_aggregation(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 13 — MASTER AGGREGATION CONTROLS
    Reduces all active lane states to a single master_classification.
    Priority: block > escalate > fail > warning > pass.
    """
    active_lanes  = submission.get("active_lanes", DEFAULT_ACTIVE_LANES)
    active_states = [
        dlog.lane_states.get(lane)
        for lane in active_lanes
        if dlog.lane_states.get(lane) not in (None, "inactive", "active")
    ]

    if "block" in active_states:
        dlog.master_classification = "block_output"
        dlog.record(13, "master_aggregation",
                    {"active_states": active_states},
                    "block_output", "at_least_one_lane_blocked")

    elif "escalate" in active_states:
        dlog.master_classification = "escalate_systemic_issue"
        dlog.record(13, "master_aggregation",
                    {"active_states": active_states},
                    "escalate_systemic_issue", "no_block_but_escalate_present")

    elif "fail" in active_states:
        dlog.master_classification = "fail"
        dlog.record(13, "master_aggregation",
                    {"active_states": active_states},
                    "fail", "at_least_one_lane_failed")

    elif "warning" in active_states:
        dlog.master_classification = "review_recommended"
        dlog.record(13, "master_aggregation",
                    {"active_states": active_states},
                    "review_recommended", "at_least_one_lane_warning")

    else:
        dlog.master_classification = "pass"
        dlog.record(13, "master_aggregation",
                    {"active_states": active_states},
                    "pass", "all_active_lanes_pass")


# ============================================================
# SECTION 14 — ROOT CAUSE FUSION CONTROLS
# ============================================================

def section_14_root_cause_fusion(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 14 — ROOT CAUSE FUSION CONTROLS
    Votes across all lane faults to identify the dominant root cause.
    Requires at least FUSION_THRESHOLD votes and FUSION_CONFIDENCE_THRESHOLD
    vote share to declare a winner; otherwise master_root_cause = unresolved.
    """
    vote_counts  = {}
    total_faults = 0

    for lane_faults in dlog.lane_faults.values():
        for fault_entry in lane_faults:
            fault_type = fault_entry["fault_type"]
            root_cause = FAULT_ROOT_CAUSE.get(fault_type, "unresolved")
            vote_counts[root_cause] = vote_counts.get(root_cause, 0) + 1
            total_faults += 1

    if total_faults == 0:
        dlog.master_root_cause = "no_faults"
        dlog.record(14, "root_cause_fusion",
                    {"total_faults": 0},
                    "no_faults", "no_faults_to_fuse")
        return

    max_cause  = max(vote_counts, key=vote_counts.get)
    max_votes  = vote_counts[max_cause]
    confidence = max_votes / total_faults

    if max_votes >= FUSION_THRESHOLD and confidence >= FUSION_CONFIDENCE_THRESHOLD:
        dlog.master_root_cause = max_cause
        dlog.record(14, "root_cause_fusion",
                    {"vote_counts": vote_counts, "max_cause": max_cause,
                     "max_votes": max_votes, "confidence": round(confidence, 4)},
                    max_cause, "fusion_threshold_met")
    else:
        dlog.master_root_cause = "unresolved"
        dlog.record(14, "root_cause_fusion",
                    {"vote_counts": vote_counts, "max_votes": max_votes,
                     "confidence": round(confidence, 4),
                     "fusion_threshold": FUSION_THRESHOLD,
                     "confidence_threshold": FUSION_CONFIDENCE_THRESHOLD},
                    "unresolved", "insufficient_vote_convergence")


# ============================================================
# SECTION 15 — REMEDIATION ROUTING CONTROLS
# ============================================================

def section_15_remediation_routing(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 15 — REMEDIATION ROUTING CONTROLS
    Appends a remediation route for each lane that classifies as fail or block.
    """
    remediation_map = {
        "correctness":  "recompute_or_relabel",
        "compliance":   "policy_rewrite_or_suppress",
        "anomaly":      "drift_investigation",
        "bias":         "fairness_review_and_mitigation",
        "robustness":   "stress_test_and_hardening",
        "traceability": "provenance_refresh",
    }

    for lane, route in remediation_map.items():
        state = dlog.lane_states.get(lane)
        if state in ("fail", "block"):
            dlog.remediation_route.append(route)
            dlog.record(15, "remediation_routing",
                        {"lane": lane, "state": state},
                        route, f"{lane}_lane_triggered_remediation")
        elif state is not None and state not in ("inactive", "active"):
            dlog.record(15, "remediation_routing",
                        {"lane": lane, "state": state},
                        "no_action", f"{lane}_no_remediation_required")


# ============================================================
# SECTION 16 — OUTPUT CONTROLS
# ============================================================

def section_16_output_controls(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 16 — OUTPUT CONTROLS
    Maps master_classification to output_action.
    Appends request_investigation if master_root_cause is unresolved.
    """
    action_map = {
        "pass":                    "approve_result",
        "review_recommended":      "send_to_manual_review",
        "fail":                    "reject_result",
        "block_output":            "block_downstream_use",
        "escalate_systemic_issue": "trigger_system_escalation",
    }

    mc     = dlog.master_classification
    action = action_map.get(mc)
    if action:
        dlog.output_action.append(action)
        dlog.record(16, "output_controls",
                    {"master_classification": mc},
                    action, f"output_action_from_{mc}")

    if dlog.master_root_cause == "unresolved":
        dlog.output_action.append("request_investigation")
        dlog.record(16, "output_controls",
                    {"master_root_cause": "unresolved"},
                    "request_investigation",
                    "unresolved_root_cause_triggers_investigation")


# ============================================================
# SECTION 17 — FEEDBACK LOOP CONTROLS
# ============================================================

def section_17_feedback_loop(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 17 — FEEDBACK LOOP CONTROLS
    Records feedback events for threshold calibration.
    All write-back operations are TODO:DATA_DEPENDENCY pending integration
    with the platform threshold management and audit case management layers.
    """
    ctx            = submission.get("audit_context", {})
    feedback_event = ctx.get("feedback_event")

    if feedback_event is None:
        dlog.record(17, "feedback_loop",
                    {"feedback_event": None},
                    "no_feedback", "no_feedback_event_in_submission")
        return

    # Rule: Manual review confirms finding
    if feedback_event == "confirmed_fault":
        dlog.record(17, "feedback_loop",
                    {"feedback_event": feedback_event},
                    "threshold_tighten_candidate",
                    "confirmed_fault_tighten_lane_thresholds")
        # TODO:DATA_DEPENDENCY — write confirmed_fault event to threshold calibration store

    # Rule: Manual review rejects finding (false positive)
    elif feedback_event == "false_positive":
        dlog.record(17, "feedback_loop",
                    {"feedback_event": feedback_event},
                    "threshold_relax_candidate",
                    "false_positive_relax_relevant_lane_thresholds")
        # TODO:DATA_DEPENDENCY — write false_positive event to threshold calibration store

    # Rule: Post-release incident
    elif feedback_event == "post_release_incident":
        dlog.record(17, "feedback_loop",
                    {"feedback_event": feedback_event},
                    "threshold_tighten_urgent",
                    "post_release_incident_tighten_thresholds")
        # TODO:DATA_DEPENDENCY — write post_release_incident; trigger urgent alert

    # Rule: Remediation succeeded
    elif feedback_event == "remediation_success":
        dlog.record(17, "feedback_loop",
                    {"feedback_event": feedback_event},
                    "resolved",
                    "post_remediation_pass_marks_resolved")
        # TODO:DATA_DEPENDENCY — mark case as resolved in audit case management system

    # Rule: Remediation failed
    elif feedback_event in ("remediation_fail", "remediation_block", "remediation_escalate"):
        dlog.record(17, "feedback_loop",
                    {"feedback_event": feedback_event},
                    "unresolved",
                    "post_remediation_fail_marks_unresolved")
        # TODO:DATA_DEPENDENCY — mark case as unresolved; escalate to human oversight queue

    # Rule: Same remediation route recurs
    repeat_pattern_count = ctx.get("repeat_pattern_count", 0)
    route_id             = ctx.get("route_id")
    if repeat_pattern_count > RECURRENCE_THRESHOLD:
        dlog.record(17, "feedback_loop",
                    {"repeat_pattern_count": repeat_pattern_count,
                     "route_id":             route_id,
                     "recurrence_threshold": RECURRENCE_THRESHOLD},
                    "systemic_pipeline_issue",
                    "recurrence_threshold_exceeded")
        # TODO:DATA_DEPENDENCY — escalate systemic_pipeline_issue to platform operations log


# ============================================================
# SECTION 18 — FINAL STACK COMPOSITE SCORE
# ============================================================

def section_18_composite_score(submission: dict, dlog: StackDecisionLog):
    """
    SECTION 18 — FINAL STACK COMPOSITE SCORE
    Weighted sum across active lane scores.
    Block and systemic overrides take precedence over score-based signals.

    stack_score = a1*correctness + a2*compliance + a3*anomaly
                + a4*bias + a5*robustness + a6*traceability
    """
    active_lanes = submission.get("active_lanes", DEFAULT_ACTIVE_LANES)

    s_correctness  = dlog.lane_scores.get("correctness",  0.0) if "correctness"  in active_lanes else 0.0
    s_compliance   = dlog.lane_scores.get("compliance",   0.0) if "compliance"   in active_lanes else 0.0
    s_anomaly      = dlog.lane_scores.get("anomaly",      0.0) if "anomaly"      in active_lanes else 0.0
    s_bias         = dlog.lane_scores.get("bias",         0.0) if "bias"         in active_lanes else 0.0
    s_robustness   = dlog.lane_scores.get("robustness",   0.0) if "robustness"   in active_lanes else 0.0
    s_traceability = dlog.lane_scores.get("traceability", 0.0) if "traceability" in active_lanes else 0.0

    stack_score = (
        A1_CORRECTNESS  * s_correctness  +
        A2_COMPLIANCE   * s_compliance   +
        A3_ANOMALY      * s_anomaly      +
        A4_BIAS         * s_bias         +
        A5_ROBUSTNESS   * s_robustness   +
        A6_TRACEABILITY * s_traceability
    )
    stack_score       = round(stack_score, 4)
    dlog.stack_score  = stack_score

    dlog.record(18, "composite_score",
                {"s_correctness":  s_correctness,
                 "s_compliance":   s_compliance,
                 "s_anomaly":      s_anomaly,
                 "s_bias":         s_bias,
                 "s_robustness":   s_robustness,
                 "s_traceability": s_traceability,
                 "weights": {"a1": A1_CORRECTNESS, "a2": A2_COMPLIANCE,
                             "a3": A3_ANOMALY,     "a4": A4_BIAS,
                             "a5": A5_ROBUSTNESS,  "a6": A6_TRACEABILITY}},
                str(stack_score), "stack_composite_calculated")

    # Override: block takes precedence over score signal
    if dlog.master_classification == "block_output":
        dlog.final_stack_signal = "blocked"
        dlog.record(18, "composite_signal",
                    {"master_classification": "block_output"},
                    "blocked", "block_override_active")
        return

    # Override: systemic escalation takes precedence
    if dlog.master_classification == "escalate_systemic_issue":
        dlog.final_stack_signal = "systemic_failure"
        dlog.record(18, "composite_signal",
                    {"master_classification": "escalate_systemic_issue"},
                    "systemic_failure", "systemic_override_active")
        return

    # Score-based signal
    if stack_score < STACK_WARNING_THRESHOLD:
        dlog.final_stack_signal = "clean"
        dlog.record(18, "composite_signal",
                    {"stack_score": stack_score,
                     "stack_warning_threshold": STACK_WARNING_THRESHOLD},
                    "clean", "score_below_warning_threshold")
    elif stack_score < STACK_FAIL_THRESHOLD:
        dlog.final_stack_signal = "warning"
        dlog.record(18, "composite_signal",
                    {"stack_score":             stack_score,
                     "stack_warning_threshold": STACK_WARNING_THRESHOLD,
                     "stack_fail_threshold":    STACK_FAIL_THRESHOLD},
                    "warning", "score_between_warning_and_fail_thresholds")
    else:
        dlog.final_stack_signal = "faulty"
        dlog.record(18, "composite_signal",
                    {"stack_score":          stack_score,
                     "stack_fail_threshold": STACK_FAIL_THRESHOLD},
                    "faulty", "score_above_fail_threshold")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def audit_stack(submission: dict) -> dict:
    """
    Orchestrates the 18-section audit stack pipeline.

    Args:
        submission: dict with keys:
            result_payload   (required) — the result to audit
            reference_payload (optional) — ground truth or reference data
            requested_lanes  (optional) — list of lanes to activate
            audit_context    (optional) — pre-computed metrics and flags

    Returns:
        Complete decision log dict produced by StackDecisionLog.to_dict().
    """
    submission_id = submission.get("submission_id", "unknown")
    dlog          = StackDecisionLog(submission_id)

    # Section 1: Intake router (halting)
    if not section_1_intake_router(submission, dlog):
        return dlog.to_dict()

    # Section 2: Lane activation
    section_2_lane_activation(submission, dlog)

    # Sections 3–8: Lane processing (all non-halting)
    section_3_correctness_lane(submission, dlog)
    section_4_compliance_lane(submission, dlog)
    section_5_anomaly_lane(submission, dlog)
    section_6_bias_lane(submission, dlog)
    section_7_robustness_lane(submission, dlog)
    section_8_traceability_lane(submission, dlog)

    # Section 9: Cross-lane conflict detection
    section_9_cross_lane_conflict(submission, dlog)

    # Section 10: Fault severity assignment
    section_10_lane_severity(submission, dlog)

    # Section 11: Lane scoring
    section_11_lane_scoring(submission, dlog)

    # Section 12: Lane classification
    section_12_lane_classification(submission, dlog)

    # Section 13: Master aggregation
    section_13_master_aggregation(submission, dlog)

    # Section 14: Root cause fusion
    section_14_root_cause_fusion(submission, dlog)

    # Section 15: Remediation routing
    section_15_remediation_routing(submission, dlog)

    # Section 16: Output controls
    section_16_output_controls(submission, dlog)

    # Section 17: Feedback loop
    section_17_feedback_loop(submission, dlog)

    # Section 18: Final composite score
    section_18_composite_score(submission, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_stack_log(result: dict):
    """Writes the audit stack decision log to the database event log."""
    _db.log_event(
        event_type="audit_stack_decision",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for block_output and escalate_systemic_issue
    master classifications, and for warning-level final signals.
    """
    mc  = result.get("master_classification")
    fss = result.get("final_stack_signal")
    sid = result.get("submission_id")

    if mc == "block_output":
        _db.post_suggestion(
            agent="audit_stack_agent",
            classification="block_output",
            risk_level="HIGH",
            payload=result,
            note="Audit stack blocked output — downstream use suppressed pending human review.",
        )
        log.warning("BLOCK: audit stack blocked output for submission %s", sid)

    elif mc == "escalate_systemic_issue":
        _db.post_suggestion(
            agent="audit_stack_agent",
            classification="escalate_systemic_issue",
            risk_level="HIGH",
            payload=result,
            note="Audit stack detected systemic issue — escalated for platform-level review.",
        )
        log.warning("ESCALATE: systemic issue flagged for submission %s", sid)

    elif fss == "warning":
        _db.post_suggestion(
            agent="audit_stack_agent",
            classification="review_recommended",
            risk_level="MEDIUM",
            payload=result,
            note="Audit stack warning — manual review recommended before approval.",
        )


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Audit Stack Agent — 18-section multi-lane result audit"
    )
    parser.add_argument(
        "--submission-id", default="cli-run",
        help="Submission ID for this audit run"
    )
    parser.add_argument(
        "--output", choices=["json", "human"], default="human",
        help="Output format for decision log"
    )
    args = parser.parse_args()

    # Minimal CLI test submission — no faults expected
    test_submission = {
        "submission_id":   args.submission_id,
        "result_payload":  {
            "result_value":      0.95,
            "source_provenance": "internal_model_v1",
        },
        "reference_payload": None,
        "requested_lanes":   None,
        "audit_context":     {},
    }

    log.info("Running audit stack for submission: %s", args.submission_id)
    result = audit_stack(test_submission)
    write_stack_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"AUDIT STACK — submission: {result.get('submission_id')}")
        print(f"{'='*72}")
        print(f"Stack State           : {result.get('stack_state')}")
        print(f"Cross-Lane Flags      : {result.get('cross_lane_flags')}")
        print(f"\nLane Results:")
        for lane in ["correctness", "compliance", "anomaly", "bias", "robustness", "traceability"]:
            state  = result.get("lane_states", {}).get(lane, "inactive")
            score  = result.get("lane_scores", {}).get(lane, "N/A")
            faults = [f["fault_type"] for f in result.get("lane_faults", {}).get(lane, [])]
            print(f"  [{lane.upper():14s}] state={state:10s} score={str(score):6s}  faults={faults}")
        print(f"\nMaster Classification : {result.get('master_classification')}")
        print(f"Master Root Cause     : {result.get('master_root_cause')}")
        print(f"Remediation Route     : {result.get('remediation_route')}")
        print(f"Output Action         : {result.get('output_action')}")
        print(f"Stack Score           : {result.get('stack_score')}")
        print(f"Final Stack Signal    : {result.get('final_stack_signal')}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
