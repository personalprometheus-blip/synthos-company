"""
result_audit_agent.py — Result Audit Agent
Synthos Company Pi | /home/<user>/synthos-company/agents/result_audit_agent.py

Role:
  Accept any structured result payload and run it through a 23-gate deterministic
  fault-detection spine. Produce a classification, remediation directives, and a
  composite fault score for every result submitted.

  This agent is accountable for:
    - Detecting faults across structure, logic, correctness, completeness, policy,
      provenance, timing, explanation quality, and statistical properties
    - Assigning severity and root cause to every fault detected
    - Classifying the overall result and recommending remediation
    - Writing a complete AuditDecisionLog for every result audited

  This agent does NOT modify the result payload it audits.
  This agent does NOT apply remediation actions automatically.
  The exception: classification = block_output suppresses downstream release.

  This agent does NOT make trade decisions or generate market signals.
  All output routes through db_helpers.

  Human-readable logic specification:
    documentation/governance/AGENT5_SYSTEM_DESCRIPTION.md

Audit modes:
  correctness   — ground truth comparison, labels, numerics
  consistency   — field conflicts, totals, chronology, reasoning chains
  completeness  — missing sections, evidence, edge case coverage
  compliance    — hard rules, policies, disclaimers, audit trail
  robustness    — reproducibility, boundary cases, load sensitivity
  anomaly       — statistical outliers, drift, rate anomalies

  Default: all six modes active if none specified.

USAGE:
  python3 result_audit_agent.py --mode audit --payload '{"result": ...}'
  python3 result_audit_agent.py --mode audit --file result.json
  python3 result_audit_agent.py --mode status
"""

import os
import sys
import json
import hashlib
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── PATH BOOTSTRAP ─────────────────────────────────────────────────────────────

import os.path as _osp
_AGENTS_DIR  = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in sys.path:
    sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import BASE_DIR, DATA_DIR, LOGS_DIR, ENV_PATH
from db_helpers import DB as _DB

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
except ImportError:
    pass

_db = _DB()

# ── LOGGING ────────────────────────────────────────────────────────────────────

LOG_FILE = LOGS_DIR / "result_audit_agent.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s result_audit_agent: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("result_audit_agent")

AGENT_VERSION = "1.0.0"

# ── CONSTANTS — GATE 1: SYSTEM GATE ───────────────────────────────────────────

# (Gate 1 has no numeric thresholds — all checks are null/hash comparisons)

# ── CONSTANTS — GATE 6: CORRECTNESS ───────────────────────────────────────────

CORRECTNESS_TOLERANCE         = 0.05    # fractional distance to ground truth
NUMERIC_TOLERANCE             = 0.01    # absolute numeric error allowed
MIN_PLAUSIBLE_BOUND           = 1e-9    # lower bound for non-zero magnitude check
MAX_PLAUSIBLE_BOUND           = 1e12    # upper bound for magnitude check

# ── CONSTANTS — GATE 7: REFERENCE COMPARISON ──────────────────────────────────

PARTIAL_MATCH_THRESHOLD       = 0.70    # minimum match score vs reference
DEVIATION_THRESHOLD           = 0.20    # maximum deviation from benchmark
CONSENSUS_THRESHOLD           = 0.60    # minimum ensemble agreement ratio
FAULT_PATTERN_SIMILARITY      = 0.75    # similarity to fault library → known signature
CORRECT_PATTERN_SIMILARITY    = 0.75    # similarity to correct library floor

# ── CONSTANTS — GATE 8: STATISTICAL / DISTRIBUTION ────────────────────────────

OUTLIER_THRESHOLD             = 3.0     # z-score → statistical outlier
MIN_VARIANCE_THRESHOLD        = 1e-6    # variance floor below → suspicious uniformity
DRIFT_THRESHOLD               = 0.25    # distribution distance → drift
MEAN_SHIFT_THRESHOLD          = 0.15    # mean difference → mean shift
CLUSTER_THRESHOLD             = 0.40    # fault density in window → clustered failure
MAX_DOMAIN_FREQUENCY          = 1000    # events per unit → rate anomaly

# ── CONSTANTS — GATE 9: ROBUSTNESS ────────────────────────────────────────────

INSTABILITY_THRESHOLD         = 0.30    # sensitivity metric → unstable result
REPRODUCIBILITY_THRESHOLD     = 0.05    # repeat variance → non-reproducible
ADVERSARIAL_THRESHOLD         = 0.10    # adversarial success rate
LOAD_PERFORMANCE_THRESHOLD    = 0.85    # correctness under load minimum

# ── CONSTANTS — GATE 10: REASONING / CAUSALITY ────────────────────────────────

SUPPORT_THRESHOLD             = 0.60    # inference path score minimum
IMPORTANCE_THRESHOLD          = 0.15    # feature importance → must be mentioned
RATIONALE_MATCH_FLOOR         = 0.40    # rationale match score floor (post-hoc check)

# ── CONSTANTS — GATE 11: CONSTRAINT / POLICY ──────────────────────────────────

SOFT_PREFERENCE_THRESHOLD     = 0.50    # preference score minimum
TRACE_MINIMUM                 = 0.80    # audit trail completeness minimum

# ── CONSTANTS — GATE 12: DATA PROVENANCE ──────────────────────────────────────

PROVENANCE_THRESHOLD          = 0.60    # source provenance confidence minimum
FRESHNESS_LIMIT_HOURS         = 24      # source age limit in hours
CITATION_SUPPORT_THRESHOLD    = 0.55    # citation-claim support score minimum

# ── CONSTANTS — GATE 13: TEMPORAL ─────────────────────────────────────────────

MAX_AUDIT_LATENCY_SECONDS     = 300     # 5 minutes maximum audit completion time
CADENCE_TOLERANCE             = 0.10    # fractional interval deviation allowed

# ── CONSTANTS — GATE 14: EXPLANATION QUALITY ──────────────────────────────────

SPECIFICITY_THRESHOLD         = 0.40    # explanation specificity score minimum
CONFIDENCE_GAP_THRESHOLD      = 0.25    # language certainty minus evidence strength
DENSITY_THRESHOLD             = 0.10    # information density minimum
VERBOSITY_THRESHOLD           = 500     # character length → verbose if also low density

# ── CONSTANTS — GATE 16: CONFIDENCE CALIBRATION ───────────────────────────────

HIGH_CONFIDENCE_THRESHOLD     = 0.80    # above this + fault → overconfidence
LOW_CONFIDENCE_THRESHOLD      = 0.30    # below this + correct → underconfidence
CALIBRATION_THRESHOLD         = 0.15    # calibration error → miscalibrated
CONFIDENCE_EVIDENCE_GAP       = 0.25    # abs diff confidence vs evidence quality

# ── CONSTANTS — GATE 17: FAULT SEVERITY ───────────────────────────────────────

HARM_THRESHOLD                = 0.50    # risk of harm → critical
ESCALATION_COUNT              = 3       # medium faults co-occurring → escalated_high
REPEAT_THRESHOLD              = 5       # same fault pattern count → systemic

# ── CONSTANTS — GATE 18: ROOT CAUSE ───────────────────────────────────────────

INPUT_QUALITY_THRESHOLD       = 0.60    # input quality score minimum
ROOT_CAUSE_CONFIDENCE_FLOOR   = 0.50    # max root cause probability minimum

# ── CONSTANTS — GATE 19: ACTION CLASSIFICATION ────────────────────────────────

ACCEPT_THRESHOLD              = 0.10    # total fault score → pass if below

# ── CONSTANTS — GATE 21: EVALUATION LOOP ──────────────────────────────────────

MISSED_FAULT_THRESHOLD        = 0.05    # missed fault rate → tighten rules
FALSE_POSITIVE_THRESHOLD      = 0.10    # false positive rate → relax rules
GROWTH_THRESHOLD              = 0.20    # fault pattern trend growth → emerging issue

# ── CONSTANTS — GATE 22: OUTPUT ───────────────────────────────────────────────

AUDIT_CONFIDENCE_THRESHOLD    = 0.50    # below → mark_audit_uncertain

# ── CONSTANTS — GATE 23: COMPOSITE FAULT SCORE ────────────────────────────────

W1_CORRECTNESS                = 0.25
W2_CONSISTENCY                = 0.20
W3_COMPLETENESS               = 0.15
W4_POLICY                     = 0.15
W5_ROBUSTNESS                 = 0.10
W6_PROVENANCE                 = 0.10
W7_CALIBRATION                = 0.05
WARNING_THRESHOLD             = 0.20    # fault score → warning
FAIL_THRESHOLD                = 0.50    # fault score → faulty

# ── AUDIT MODES ────────────────────────────────────────────────────────────────

ALL_MODES = {"correctness", "consistency", "completeness", "compliance",
             "robustness", "anomaly"}

# ── FAULT SEVERITY SCOPE MAPPING ──────────────────────────────────────────────

# Maps fault_state → (impact_scope, default_severity)
# impact_scope: "presentation_only", "explanation_or_format_only", "decision_output"
FAULT_SCOPE = {
    # Low severity — presentation only
    "extra_fields_detected":        ("presentation_only",          "low"),
    "ordering_error":               ("presentation_only",          "low"),
    "vague_explanation":            ("explanation_or_format_only", "medium"),
    "low_information_explanation":  ("explanation_or_format_only", "medium"),
    "explanation_format_failure":   ("explanation_or_format_only", "medium"),
    "missing_explanation":          ("explanation_or_format_only", "medium"),
    "no_explanation":               ("explanation_or_format_only", "medium"),
    "hidden_uncertainty":           ("explanation_or_format_only", "medium"),
    "overclaimed_certainty":        ("explanation_or_format_only", "medium"),
    # Medium severity — explanation/format impact
    "format_error":                 ("explanation_or_format_only", "medium"),
    "missing_disclaimer":           ("explanation_or_format_only", "medium"),
    "soft_preference_failure":      ("explanation_or_format_only", "medium"),
    "underconfidence":              ("explanation_or_format_only", "medium"),
    "posthoc_justification_risk":   ("explanation_or_format_only", "medium"),
    # High severity — decision output impact
    "missing_required_fields":      ("decision_output", "high"),
    "type_mismatch":                ("decision_output", "high"),
    "null_mandatory_value":         ("decision_output", "high"),
    "malformed_substructure":       ("decision_output", "high"),
    "internal_conflict":            ("decision_output", "high"),
    "summary_detail_mismatch":      ("decision_output", "high"),
    "reconciliation_error":         ("decision_output", "high"),
    "temporal_inconsistency":       ("decision_output", "high"),
    "label_evidence_conflict":      ("decision_output", "high"),
    "status_conflict":              ("decision_output", "high"),
    "domain_logic_failure":         ("decision_output", "high"),
    "incomplete_result":            ("decision_output", "high"),
    "unsupported_conclusion":       ("decision_output", "high"),
    "truncated_output":             ("decision_output", "high"),
    "incorrect_result":             ("decision_output", "high"),
    "misclassification":            ("decision_output", "high"),
    "numeric_error":                ("decision_output", "high"),
    "sign_error":                   ("decision_output", "high"),
    "directional_error":            ("decision_output", "high"),
    "benchmark_deviation":          ("decision_output", "high"),
    "ensemble_outlier":             ("decision_output", "high"),
    "statistical_outlier":          ("decision_output", "high"),
    "distribution_drift":           ("decision_output", "high"),
    "clustered_failure":            ("decision_output", "high"),
    "unsupported_inference":        ("decision_output", "high"),
    "reasoning_gap":                ("decision_output", "high"),
    "rationale_output_conflict":    ("decision_output", "high"),
    "omitted_key_factor":           ("decision_output", "high"),
    "top_rank_failure":             ("decision_output", "high"),
    "recall_failure_topk":          ("decision_output", "high"),
    "overconfidence":               ("decision_output", "high"),
    "miscalibrated_confidence":     ("decision_output", "high"),
    # Critical — potential harm
    "hard_rule_violation":          ("decision_output", "critical"),
    "prohibited_output":            ("decision_output", "critical"),
    "restricted_recommendation":    ("decision_output", "critical"),
    "audit_trail_failure":          ("decision_output", "critical"),
    "data_leakage":                 ("decision_output", "critical"),
    "adversarial_vulnerability":    ("decision_output", "critical"),
    "causal_overreach":             ("decision_output", "critical"),
}

# ── V1 SCHEMA DEFINITIONS ─────────────────────────────────────────────────────

AGENT_VERSION = "1.0.0"

INPUT_SCHEMA = {
    "trade_output": {"type": "dict", "required": True,  "description": "Trade Logic Agent output"},
    "run_id":       {"type": "str",  "required": True},
}

OUTPUT_SCHEMA = {
    "classification": {"type": "str",  "values": ["block_output", "fail", "pass_with_warnings", "pass"]},
    "fault_list":     {"type": "list", "description": "Identified fault codes"},
    "decision_log":   {"type": "list", "description": "Full gate trace"},
}

# ── AUDIT DECISION LOG ─────────────────────────────────────────────────────────

class AuditDecisionLog:
    """
    Accumulates all gate inputs, fault detections, severity assignments,
    root causes, and output controls for a single result audit.
    Serialised to JSON and written to the database after Gate 23.
    """

    def __init__(self, result_id: str, result_type: str,
                 audit_modes: list, run_ts: datetime):
        self.run_ts        = run_ts.isoformat()
        self.result_id     = result_id
        self.result_type   = result_type
        self.audit_modes   = audit_modes
        self.gates         = {}
        self.fault_register = []   # list of {fault_state, field, detail, severity, scope}
        self.halted_at     = None
        self.halt_reason   = None
        # Summary fields populated at end of pipeline
        self.classification    = None
        self.final_audit_signal = None
        self.fault_score       = None
        self.output_action     = None
        self.root_causes       = []
        self.remediation       = []

    def record(self, gate: int, name: str, inputs: dict,
               result: str, reason: str, faults: list = None):
        self.gates[gate] = {
            "name":   name,
            "inputs": inputs,
            "result": result,
            "reason": reason,
            "faults": faults or [],
        }

    def add_fault(self, fault_state: str, field: str = None,
                  detail: str = None):
        scope, severity = FAULT_SCOPE.get(fault_state, ("decision_output", "high"))
        self.fault_register.append({
            "fault_state": fault_state,
            "field":       field,
            "detail":      detail,
            "severity":    severity,
            "impact_scope": scope,
        })

    def halt(self, gate: int, name: str, reason: str):
        self.halted_at  = gate
        self.halt_reason = reason
        self.record(gate, name, {}, "HALT", reason)

    def to_dict(self) -> dict:
        return {
            "run_ts":              self.run_ts,
            "result_id":           self.result_id,
            "result_type":         self.result_type,
            "audit_modes":         self.audit_modes,
            "halted_at":           self.halted_at,
            "halt_reason":         self.halt_reason,
            "fault_register":      self.fault_register,
            "classification":      self.classification,
            "final_audit_signal":  self.final_audit_signal,
            "fault_score":         self.fault_score,
            "output_action":       self.output_action,
            "root_causes":         self.root_causes,
            "remediation":         self.remediation,
            "gates":               self.gates,
        }

    def to_human_readable(self) -> str:
        lines = [
            f"=== AuditDecisionLog ===",
            f"Run:         {self.run_ts}",
            f"Result ID:   {self.result_id}",
            f"Result Type: {self.result_type}",
            f"Audit Modes: {self.audit_modes}",
            f"",
        ]
        for gate_num in sorted(self.gates.keys()):
            g = self.gates[gate_num]
            lines.append(f"  Gate {gate_num:>2} — {g['name']}")
            for k, v in g["inputs"].items():
                lines.append(f"             {k}: {v}")
            lines.append(f"           → result: {g['result']}  ({g['reason']})")
            for f in g.get("faults", []):
                lines.append(f"           ⚠ FAULT: {f}")
        lines += [
            f"",
            f"Fault register ({len(self.fault_register)} faults):",
        ]
        for f in self.fault_register:
            lines.append(f"  [{f['severity'].upper():8}] {f['fault_state']}"
                         f"  field={f['field']}  {f['detail']}")
        lines += [
            f"",
            f"Root causes:    {self.root_causes}",
            f"Classification: {self.classification}",
            f"Fault score:    {self.fault_score}",
            f"Final signal:   {self.final_audit_signal}",
            f"Output action:  {self.output_action}",
            f"Remediation:    {self.remediation}",
        ]
        if self.halted_at:
            lines.append(f"HALTED at gate {self.halted_at}: {self.halt_reason}")
        return "\n".join(lines)


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _result_hash(payload: dict) -> str:
    serialised = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()

def _is_mode_active(modes: list, mode: str) -> bool:
    return mode in modes or len(modes) == 0  # empty → all modes active


# ── GATE IMPLEMENTATIONS ───────────────────────────────────────────────────────

def gate_1_system(submission: dict, audited_store: set,
                  adl: AuditDecisionLog) -> bool:
    """
    Gate 1 — System Gate
    Rejects the submission if the input is unworkable.
    Returns True (PROCEED) or False (HALT).
    Non-halting faults (low_traceability, unverifiable_origin) are added to
    the fault register and processing continues.
    """
    result_payload    = submission.get("result_payload")
    reference_payload = submission.get("reference_payload")
    reference_required = submission.get("reference_required", False)
    evaluation_rules  = submission.get("evaluation_rule_set")
    result_timestamp  = submission.get("result_timestamp")
    source_provenance = submission.get("source_provenance")

    # IF result input missing
    if result_payload is None:
        adl.halt(1, "System Gate", "reject_result: result_payload is null")
        return False

    # IF reference input missing when required
    if reference_required and reference_payload is None:
        adl.halt(1, "System Gate",
                 "incomplete_evaluation: reference_payload required but null")
        return False

    # IF parse failure on result
    if not isinstance(result_payload, (dict, list)):
        adl.halt(1, "System Gate", "reject_result: result_payload cannot be parsed as structure")
        return False

    # IF schema invalid
    declared_schema = submission.get("schema")
    if declared_schema and not _validate_schema(result_payload, declared_schema):
        adl.halt(1, "System Gate", "invalid_structure: schema_validation failed")
        return False

    # IF evaluation rules unavailable
    if evaluation_rules is None:
        adl.halt(1, "System Gate", "halt_audit: evaluation_rule_set is null")
        return False

    # IF duplicate result detected
    payload_hash = _result_hash(result_payload) if isinstance(result_payload, dict) else ""
    if payload_hash and payload_hash in audited_store:
        adl.halt(1, "System Gate",
                 f"suppress_duplicate_audit: hash {payload_hash[:16]}... already audited")
        return False

    # Non-halting: IF result timestamp missing
    if result_timestamp is None:
        adl.add_fault("low_traceability", field="result_timestamp",
                      detail="result_timestamp is null; temporal gates degraded")

    # Non-halting: IF provenance missing
    if source_provenance is None:
        adl.add_fault("unverifiable_origin", field="source_provenance",
                      detail="source_provenance is null")

    adl.record(1, "System Gate",
               {"result_payload_present": True,
                "reference_required": reference_required,
                "reference_present": reference_payload is not None,
                "evaluation_rules_present": True,
                "has_timestamp": result_timestamp is not None,
                "has_provenance": source_provenance is not None},
               "PASS", "system checks passed")
    return True


def _validate_schema(payload: dict, schema: dict) -> bool:
    """
    Minimal schema validation: checks required keys and type declarations.
    Full JSON Schema validation is a TODO: DATA_DEPENDENCY.
    """
    for field, spec in schema.items():
        if spec.get("required") and field not in payload:
            return False
        if field in payload and "type" in spec:
            if not isinstance(payload[field], spec["type"]):
                return False
    return True


def gate_2_audit_mode(submission: dict, adl: AuditDecisionLog) -> list:
    """
    Gate 2 — Scope & Audit Mode Controls
    Establishes which audit modes are active for this pass.
    Returns the active modes list.
    """
    requested = submission.get("audit_modes", [])
    if not requested:
        active_modes = list(ALL_MODES)
        audit_state  = "multi_pass_audit"
    elif len(requested) == 1:
        active_modes = list(requested)
        mode_state_map = {
            "correctness":  "factual_validation",
            "consistency":  "internal_consistency_check",
            "completeness": "coverage_check",
            "compliance":   "rule_conformance_check",
            "robustness":   "stress_check",
            "anomaly":      "outlier_scan",
        }
        audit_state = mode_state_map.get(requested[0], "unknown_mode")
    else:
        active_modes = list(requested)
        audit_state  = "multi_pass_audit"

    adl.record(2, "Scope & Audit Mode",
               {"requested_modes": requested,
                "active_modes": active_modes},
               audit_state, f"active_modes: {active_modes}")
    return active_modes


def gate_3_structure(submission: dict, active_modes: list,
                     adl: AuditDecisionLog) -> None:
    """
    Gate 3 — Result Structure Controls
    Checks required fields, types, nulls, format, and nested schema validity.
    Adds all faults to the fault register. Does not halt.
    """
    result      = submission.get("result_payload", {})
    rule_set    = submission.get("evaluation_rule_set", {})
    schema      = submission.get("schema", {})

    required_fields = rule_set.get("required_fields", [])
    unexpected_ok   = rule_set.get("allow_extra_fields", True)
    expected_types  = rule_set.get("field_types", {})
    expected_order  = rule_set.get("field_order", [])
    faults = []

    if not isinstance(result, dict):
        # Non-dict payloads cannot be field-checked; skip gate
        adl.record(3, "Result Structure", {}, "SKIP", "result is not a dict; field checks skipped")
        return

    # IF required field missing
    present = [f for f in required_fields if f in result]
    if len(present) < len(required_fields):
        missing = [f for f in required_fields if f not in result]
        for f in missing:
            adl.add_fault("missing_required_fields", field=f,
                          detail=f"required field '{f}' absent")
            faults.append(f"missing_required_fields: {f}")

    # IF unexpected field present
    if not unexpected_ok:
        declared = set(schema.keys()) | set(required_fields)
        extras   = [k for k in result if k not in declared]
        if extras:
            for e in extras:
                adl.add_fault("extra_fields_detected", field=e,
                              detail=f"undeclared field '{e}'")
                faults.append(f"extra_fields_detected: {e}")

    # IF field type mismatch
    for field, expected_type in expected_types.items():
        if field in result and not isinstance(result[field], expected_type):
            actual = type(result[field]).__name__
            adl.add_fault("type_mismatch", field=field,
                          detail=f"expected {expected_type.__name__}, got {actual}")
            faults.append(f"type_mismatch: {field}")

    # IF null value in mandatory field
    for field in required_fields:
        if field in result and result[field] is None:
            adl.add_fault("null_mandatory_value", field=field,
                          detail=f"mandatory field '{field}' is null")
            faults.append(f"null_mandatory_value: {field}")

    # IF field format invalid
    format_checks = rule_set.get("field_formats", {})
    for field, fmt in format_checks.items():
        if field in result:
            if not _check_format(result[field], fmt):
                adl.add_fault("format_error", field=field,
                              detail=f"field '{field}' failed format check '{fmt}'")
                faults.append(f"format_error: {field}")

    # IF nested structure malformed
    nested_schemas = rule_set.get("nested_schemas", {})
    for field, sub_schema in nested_schemas.items():
        if field in result and isinstance(result[field], dict):
            if not _validate_schema(result[field], sub_schema):
                adl.add_fault("malformed_substructure", field=field,
                              detail=f"nested structure '{field}' failed schema validation")
                faults.append(f"malformed_substructure: {field}")

    # IF key ordering matters and is wrong
    if expected_order and isinstance(result, dict):
        actual_keys = [k for k in result if k in expected_order]
        expected_present = [k for k in expected_order if k in result]
        if actual_keys != expected_present:
            adl.add_fault("ordering_error", field="key_order",
                          detail=f"expected order {expected_present}, got {actual_keys}")
            faults.append("ordering_error")

    adl.record(3, "Result Structure",
               {"required_field_count": len(required_fields),
                "present_count": len(present),
                "extra_fields_checked": not unexpected_ok},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} structural faults", faults)


def _check_format(value, fmt: str) -> bool:
    """Check a value against a named format rule."""
    import re
    format_patterns = {
        "iso8601":    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?",
        "ticker":     r"^[A-Z]{1,5}$",
        "uuid":       r"^[0-9a-f-]{36}$",
        "positive":   None,  # numeric check
        "non_empty":  None,
    }
    if fmt == "positive":
        return isinstance(value, (int, float)) and value > 0
    if fmt == "non_empty":
        return bool(value)
    pattern = format_patterns.get(fmt)
    if pattern:
        return bool(re.match(pattern, str(value), re.IGNORECASE))
    return True


def gate_4_consistency(submission: dict, active_modes: list,
                       adl: AuditDecisionLog) -> None:
    """
    Gate 4 — Logical Consistency Controls
    Checks for field conflicts, summary/detail mismatches, totals reconciliation,
    temporal consistency, label/evidence alignment, and domain rule compliance.
    """
    if not _is_mode_active(active_modes, "consistency"):
        adl.record(4, "Logical Consistency", {}, "SKIP", "consistency mode not active")
        return

    result   = submission.get("result_payload", {})
    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    if not isinstance(result, dict):
        adl.record(4, "Logical Consistency", {}, "SKIP", "result is not a dict")
        return

    # IF two fields conflict
    constraints = rule_set.get("field_constraints", [])
    for c in constraints:
        field_a, field_b, constraint_type = c.get("a"), c.get("b"), c.get("type")
        if field_a in result and field_b in result:
            if not _evaluate_constraint(result[field_a], result[field_b], constraint_type):
                adl.add_fault("internal_conflict", field=f"{field_a},{field_b}",
                              detail=f"constraint '{constraint_type}' violated")
                faults.append(f"internal_conflict: {field_a} vs {field_b}")

    # IF summary conflicts with detailed values
    summaries = rule_set.get("summary_detail_checks", [])
    for s in summaries:
        summary_field = s.get("summary")
        detail_fields = s.get("details", [])
        agg_fn        = s.get("aggregate", "sum")
        if summary_field in result:
            detail_vals = [result[f] for f in detail_fields
                           if f in result and isinstance(result[f], (int, float))]
            agg_val = sum(detail_vals) if agg_fn == "sum" else None
            if agg_val is not None and isinstance(result[summary_field], (int, float)):
                if abs(result[summary_field] - agg_val) > NUMERIC_TOLERANCE:
                    adl.add_fault("summary_detail_mismatch", field=summary_field,
                                  detail=f"summary={result[summary_field]}, aggregate={agg_val}")
                    faults.append(f"summary_detail_mismatch: {summary_field}")

    # IF totals do not reconcile
    totals = rule_set.get("total_checks", [])
    for t in totals:
        total_field      = t.get("total")
        component_fields = t.get("components", [])
        if total_field in result:
            component_vals = [result[f] for f in component_fields
                              if f in result and isinstance(result[f], (int, float))]
            computed_total = sum(component_vals)
            if isinstance(result[total_field], (int, float)):
                if abs(result[total_field] - computed_total) > NUMERIC_TOLERANCE:
                    adl.add_fault("reconciliation_error", field=total_field,
                                  detail=f"reported={result[total_field]}, computed={computed_total}")
                    faults.append(f"reconciliation_error: {total_field}")

    # IF chronology impossible
    chrono_checks = rule_set.get("chronology_checks", [])
    for ch in chrono_checks:
        t1_field, t2_field = ch.get("before"), ch.get("after")
        if t1_field in result and t2_field in result:
            t1 = result[t1_field]
            t2 = result[t2_field]
            if isinstance(t1, str) and isinstance(t2, str):
                if t2 < t1:  # lexicographic ISO 8601 comparison
                    adl.add_fault("temporal_inconsistency",
                                  field=f"{t1_field}→{t2_field}",
                                  detail=f"{t2_field}={t2} < {t1_field}={t1}")
                    faults.append(f"temporal_inconsistency: {t2_field} before {t1_field}")

    # IF classification conflicts with evidence
    label_checks = rule_set.get("label_evidence_checks", [])
    for lc in label_checks:
        label_field = lc.get("label_field")
        inferred    = lc.get("inferred_value")
        if label_field in result and result.get(label_field) != inferred:
            adl.add_fault("label_evidence_conflict", field=label_field,
                          detail=f"assigned={result[label_field]}, inferred={inferred}")
            faults.append(f"label_evidence_conflict: {label_field}")

    # IF declared status conflicts with state variables
    status_checks = rule_set.get("status_checks", [])
    for sc in status_checks:
        status_field  = sc.get("status_field")
        state_fields  = sc.get("state_fields", [])
        invalid_combo = sc.get("invalid_combinations", [])
        if status_field in result:
            state_vals = {f: result.get(f) for f in state_fields}
            combo      = (result[status_field],) + tuple(state_vals.values())
            if list(combo) in invalid_combo:
                adl.add_fault("status_conflict", field=status_field,
                              detail=f"status={result[status_field]} incompatible with state")
                faults.append(f"status_conflict: {status_field}")

    # IF result violates domain rule
    domain_constraints = rule_set.get("domain_constraints", [])
    for dc in domain_constraints:
        field = dc.get("field")
        rule  = dc.get("rule")
        if field in result:
            if not _check_domain_rule(result[field], rule):
                adl.add_fault("domain_logic_failure", field=field,
                              detail=f"domain rule '{rule}' violated")
                faults.append(f"domain_logic_failure: {field}")

    adl.record(4, "Logical Consistency",
               {"constraints_checked": len(constraints),
                "total_checks": len(totals),
                "chrono_checks": len(chrono_checks)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} consistency faults", faults)


def _evaluate_constraint(val_a, val_b, constraint_type: str) -> bool:
    """Evaluate a two-field constraint."""
    try:
        if constraint_type == "a_less_than_b":    return val_a < val_b
        if constraint_type == "a_greater_than_b": return val_a > val_b
        if constraint_type == "a_not_equal_b":    return val_a != val_b
        if constraint_type == "a_plus_b_positive":return (val_a + val_b) > 0
        if constraint_type == "a_equals_b":       return val_a == val_b
    except (TypeError, ValueError):
        return False
    return True


def _check_domain_rule(value, rule: str) -> bool:
    """Check a value against a named domain rule."""
    try:
        if rule == "non_negative":          return isinstance(value, (int, float)) and value >= 0
        if rule == "positive":              return isinstance(value, (int, float)) and value > 0
        if rule == "between_0_and_1":       return isinstance(value, (int, float)) and 0 <= value <= 1
        if rule == "non_empty_string":      return isinstance(value, str) and len(value) > 0
        if rule == "non_empty_list":        return isinstance(value, list) and len(value) > 0
    except (TypeError, ValueError):
        return False
    return True


def gate_5_completeness(submission: dict, active_modes: list,
                         adl: AuditDecisionLog) -> None:
    """
    Gate 5 — Completeness Controls
    Checks for missing sections, explanations, evidence, edge cases, and truncation.
    """
    if not _is_mode_active(active_modes, "completeness"):
        adl.record(5, "Completeness", {}, "SKIP", "completeness mode not active")
        return

    result   = submission.get("result_payload", {})
    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    if not isinstance(result, dict):
        adl.record(5, "Completeness", {}, "SKIP", "result is not a dict")
        return

    # IF expected section missing
    required_sections = rule_set.get("required_sections", [])
    for section in required_sections:
        if section not in result:
            adl.add_fault("incomplete_result", field=section,
                          detail=f"required section '{section}' absent")
            faults.append(f"incomplete_result: {section}")

    # IF expected explanation missing
    explanation_required = rule_set.get("explanation_required", False)
    explanation_text     = result.get("explanation") or result.get("rationale")
    if explanation_required and not explanation_text:
        adl.add_fault("missing_explanation", field="explanation",
                      detail="explanation required but absent")
        faults.append("missing_explanation")

    # IF supporting evidence missing
    evidence_required = rule_set.get("evidence_required", False)
    evidence_count    = len(result.get("evidence", result.get("sources", [])))
    if evidence_required and evidence_count == 0:
        adl.add_fault("unsupported_conclusion", field="evidence",
                      detail="evidence required but count is 0")
        faults.append("unsupported_conclusion")

    # IF edge cases not addressed
    expected_edge_cases = rule_set.get("expected_edge_case_count", 0)
    addressed_edge_cases = result.get("edge_cases_addressed", 0)
    if isinstance(addressed_edge_cases, list):
        addressed_edge_cases = len(addressed_edge_cases)
    if expected_edge_cases > addressed_edge_cases:
        gap = expected_edge_cases - addressed_edge_cases
        adl.add_fault("edge_case_coverage_gap", field="edge_cases",
                      detail=f"expected {expected_edge_cases}, addressed {addressed_edge_cases}, gap {gap}")
        faults.append(f"edge_case_coverage_gap: gap={gap}")

    # IF output truncated
    max_length = rule_set.get("max_output_length")
    output_str = json.dumps(result)
    if max_length and len(output_str) >= max_length:
        # Check for truncation markers
        truncation_markers = ["...", "[truncated]", "[cut]", "TRUNCATED"]
        if any(m in output_str for m in truncation_markers):
            adl.add_fault("truncated_output", field="output",
                          detail=f"output at max length {max_length} with truncation marker")
            faults.append("truncated_output")

    # IF optional fields absent but required by mode
    mode_required = rule_set.get("mode_required_fields", {})
    for mode in active_modes:
        for field in mode_required.get(mode, []):
            if field not in result:
                adl.add_fault("mode_completeness_failure", field=field,
                              detail=f"field '{field}' required by mode '{mode}'")
                faults.append(f"mode_completeness_failure: {field} ({mode})")

    # IF confidence absent
    confidence_required = rule_set.get("confidence_required", False)
    if confidence_required and result.get("confidence") is None:
        adl.add_fault("missing_confidence", field="confidence",
                      detail="confidence required but null")
        faults.append("missing_confidence")

    adl.record(5, "Completeness",
               {"required_sections": len(required_sections),
                "evidence_count": evidence_count,
                "edge_case_gap": max(0, expected_edge_cases - addressed_edge_cases)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} completeness faults", faults)


def gate_6_correctness(submission: dict, active_modes: list,
                        adl: AuditDecisionLog) -> None:
    """
    Gate 6 — Correctness Controls
    Compares result values against known ground truth.
    Only active when correctness mode is active AND ground truth is available.
    """
    if not _is_mode_active(active_modes, "correctness"):
        adl.record(6, "Correctness", {}, "SKIP", "correctness mode not active")
        return

    ground_truth = submission.get("ground_truth")
    if ground_truth is None:
        adl.record(6, "Correctness", {}, "SKIP",
                   "ground_truth not provided; correctness checks skipped — TODO: DATA_DEPENDENCY")
        return

    result   = submission.get("result_payload", {})
    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    if not isinstance(result, dict) or not isinstance(ground_truth, dict):
        adl.record(6, "Correctness", {}, "SKIP", "non-dict payload; correctness skipped")
        return

    correctness_checks = rule_set.get("correctness_checks", [])
    for check in correctness_checks:
        field     = check.get("field")
        check_type = check.get("type")
        if field not in result or field not in ground_truth:
            continue

        result_val = result[field]
        truth_val  = ground_truth[field]

        # IF result differs from known truth
        if check_type == "distance" and isinstance(result_val, (int, float)):
            if truth_val != 0:
                dist = abs(result_val - truth_val) / abs(truth_val)
            else:
                dist = abs(result_val - truth_val)
            if dist > CORRECTNESS_TOLERANCE:
                adl.add_fault("incorrect_result", field=field,
                              detail=f"result={result_val}, truth={truth_val}, dist={dist:.4f}")
                faults.append(f"incorrect_result: {field}")

        # IF categorical label wrong
        if check_type == "classification":
            if result_val != truth_val:
                adl.add_fault("misclassification", field=field,
                              detail=f"predicted={result_val}, actual={truth_val}")
                faults.append(f"misclassification: {field}")

        # IF numeric output outside tolerance
        if check_type == "numeric" and isinstance(result_val, (int, float)):
            if abs(result_val - truth_val) > NUMERIC_TOLERANCE:
                adl.add_fault("numeric_error", field=field,
                              detail=f"result={result_val}, expected={truth_val}, "
                                     f"error={abs(result_val - truth_val):.6f}")
                faults.append(f"numeric_error: {field}")

        # IF unit incorrect
        if check_type == "unit":
            expected_unit = check.get("expected_unit")
            actual_unit   = result.get(f"{field}_unit")
            if actual_unit != expected_unit:
                adl.add_fault("unit_error", field=field,
                              detail=f"reported_unit={actual_unit}, expected={expected_unit}")
                faults.append(f"unit_error: {field}")

        # IF sign incorrect
        if check_type in {"numeric", "distance"} and isinstance(result_val, (int, float)):
            if isinstance(truth_val, (int, float)) and truth_val != 0:
                if (result_val >= 0) != (truth_val >= 0):
                    adl.add_fault("sign_error", field=field,
                                  detail=f"result sign={'+' if result_val >= 0 else '-'}, "
                                         f"expected sign={'+' if truth_val >= 0 else '-'}")
                    faults.append(f"sign_error: {field}")

        # IF magnitude implausible
        if check_type == "magnitude" and isinstance(result_val, (int, float)):
            if abs(result_val) > MAX_PLAUSIBLE_BOUND or (
                abs(result_val) < MIN_PLAUSIBLE_BOUND and result_val != 0
            ):
                adl.add_fault("magnitude_error", field=field,
                              detail=f"value={result_val} outside plausible bounds "
                                     f"[{MIN_PLAUSIBLE_BOUND}, {MAX_PLAUSIBLE_BOUND}]")
                faults.append(f"magnitude_error: {field}")

        # IF direction-of-change wrong
        if check_type == "directional" and isinstance(result_val, (int, float)):
            if isinstance(truth_val, (int, float)):
                result_sign = 1 if result_val > 0 else (-1 if result_val < 0 else 0)
                truth_sign  = 1 if truth_val  > 0 else (-1 if truth_val  < 0 else 0)
                if result_sign != truth_sign and truth_sign != 0:
                    adl.add_fault("directional_error", field=field,
                                  detail=f"predicted_delta_sign={result_sign}, "
                                         f"actual_delta_sign={truth_sign}")
                    faults.append(f"directional_error: {field}")

    adl.record(6, "Correctness",
               {"checks_run": len(correctness_checks),
                "ground_truth_available": True},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} correctness faults", faults)


def gate_7_reference(submission: dict, active_modes: list,
                      adl: AuditDecisionLog) -> None:
    """
    Gate 7 — Reference Comparison Controls
    Compares result against reference payload, benchmark, and fault pattern library.
    """
    reference_payload = submission.get("reference_payload")
    benchmark_payload = submission.get("benchmark_payload")
    fault_library     = submission.get("fault_pattern_library", [])
    correct_library   = submission.get("correct_pattern_library", [])
    ensemble_results  = submission.get("ensemble_results", [])
    stale_reference   = submission.get("stale_reference_payload")
    result            = submission.get("result_payload", {})
    rule_set          = submission.get("evaluation_rule_set", {})
    exact_match_mode  = rule_set.get("exact_match_mode", False)
    faults            = []

    if not isinstance(result, dict):
        adl.record(7, "Reference Comparison", {}, "SKIP", "non-dict payload")
        return

    if reference_payload is None and benchmark_payload is None and not ensemble_results:
        adl.record(7, "Reference Comparison", {}, "SKIP",
                   "no reference or benchmark provided; skipping — TODO: DATA_DEPENDENCY")
        return

    # IF reference available and exact mismatch
    if reference_payload is not None and exact_match_mode:
        if result != reference_payload:
            adl.add_fault("exact_mismatch", field="result_payload",
                          detail="result does not exactly match reference under exact_match_mode")
            faults.append("exact_mismatch")

    # IF reference available and partial mismatch
    if reference_payload is not None and not exact_match_mode:
        match_score = _dict_match_score(result, reference_payload)
        if match_score < PARTIAL_MATCH_THRESHOLD:
            adl.add_fault("partial_mismatch", field="result_payload",
                          detail=f"match_score={match_score:.3f} < threshold {PARTIAL_MATCH_THRESHOLD}")
            faults.append(f"partial_mismatch: score={match_score:.3f}")

    # IF high deviation from benchmark result
    if benchmark_payload is not None:
        deviation = _compute_deviation(result, benchmark_payload)
        if deviation > DEVIATION_THRESHOLD:
            adl.add_fault("benchmark_deviation", field="result_payload",
                          detail=f"deviation={deviation:.3f} > threshold {DEVIATION_THRESHOLD}")
            faults.append(f"benchmark_deviation: {deviation:.3f}")

    # IF result is closer to prior faulty pattern than correct pattern
    if fault_library and correct_library:
        fault_sim   = max((_dict_match_score(result, p) for p in fault_library), default=0.0)
        correct_sim = max((_dict_match_score(result, p) for p in correct_library), default=0.0)
        if fault_sim > FAULT_PATTERN_SIMILARITY and fault_sim > correct_sim:
            adl.add_fault("known_fault_signature", field="result_payload",
                          detail=f"fault_similarity={fault_sim:.3f} > correct_similarity={correct_sim:.3f}")
            faults.append(f"known_fault_signature: fault_sim={fault_sim:.3f}")

    # IF result disagrees with majority ensemble
    if ensemble_results:
        agree = sum(1 for e in ensemble_results if e == result)
        agreement_ratio = agree / len(ensemble_results)
        if agreement_ratio < CONSENSUS_THRESHOLD:
            adl.add_fault("ensemble_outlier", field="result_payload",
                          detail=f"agreement_ratio={agreement_ratio:.3f} < {CONSENSUS_THRESHOLD}")
            faults.append(f"ensemble_outlier: ratio={agreement_ratio:.3f}")

    # IF result matches stale reference not current reference
    if stale_reference is not None and reference_payload is not None:
        stale_sim   = _dict_match_score(result, stale_reference)
        current_sim = _dict_match_score(result, reference_payload)
        if stale_sim > current_sim:
            adl.add_fault("outdated_alignment", field="result_payload",
                          detail=f"stale_similarity={stale_sim:.3f} > current_similarity={current_sim:.3f}")
            faults.append("outdated_alignment")

    adl.record(7, "Reference Comparison",
               {"reference_available": reference_payload is not None,
                "benchmark_available": benchmark_payload is not None,
                "ensemble_size": len(ensemble_results),
                "fault_library_size": len(fault_library)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} reference faults", faults)


def _dict_match_score(a: dict, b: dict) -> float:
    """
    Compute a simple overlap match score between two dicts.
    Fraction of keys in a that exist in b with matching values.
    """
    if not a or not b:
        return 0.0
    matches = sum(1 for k, v in a.items() if b.get(k) == v)
    return matches / len(a)


def _compute_deviation(result: dict, benchmark: dict) -> float:
    """
    Compute normalised absolute deviation between numeric fields.
    """
    total_dev = 0.0
    count     = 0
    for k in result:
        if k in benchmark:
            r, b = result[k], benchmark[k]
            if isinstance(r, (int, float)) and isinstance(b, (int, float)) and b != 0:
                total_dev += abs(r - b) / abs(b)
                count += 1
    return (total_dev / count) if count > 0 else 0.0


def gate_8_statistical(submission: dict, active_modes: list,
                        adl: AuditDecisionLog) -> None:
    """
    Gate 8 — Statistical / Distribution Controls
    Detects outliers, variance collapse, drift, mean shifts, and rate anomalies.
    Only active when anomaly mode is active AND historical baseline is available.
    """
    if not _is_mode_active(active_modes, "anomaly"):
        adl.record(8, "Statistical / Distribution", {}, "SKIP", "anomaly mode not active")
        return

    historical = submission.get("historical_distribution")
    if historical is None:
        adl.record(8, "Statistical / Distribution", {}, "SKIP",
                   "historical_distribution not provided — TODO: DATA_DEPENDENCY")
        return

    result_batch  = submission.get("result_batch", [])
    baseline_batch = historical.get("baseline_batch", [])
    numeric_value  = submission.get("result_payload", {}).get("value")
    hist_mean      = historical.get("mean", 0.0)
    hist_std       = historical.get("std", 1.0)
    faults         = []

    # IF result is an outlier
    if numeric_value is not None and isinstance(numeric_value, (int, float)) and hist_std > 0:
        z_score = abs(numeric_value - hist_mean) / hist_std
        if z_score > OUTLIER_THRESHOLD:
            adl.add_fault("statistical_outlier", field="value",
                          detail=f"z_score={z_score:.2f} > threshold {OUTLIER_THRESHOLD}")
            faults.append(f"statistical_outlier: z={z_score:.2f}")

    # IF variance collapse detected
    if len(result_batch) >= 3:
        vals    = [r for r in result_batch if isinstance(r, (int, float))]
        mean_v  = sum(vals) / len(vals) if vals else 0
        var_v   = sum((v - mean_v) ** 2 for v in vals) / len(vals) if vals else 0
        if var_v < MIN_VARIANCE_THRESHOLD and len(vals) >= 3:
            adl.add_fault("suspicious_uniformity", field="result_batch",
                          detail=f"variance={var_v:.2e} < min {MIN_VARIANCE_THRESHOLD}")
            faults.append(f"suspicious_uniformity: var={var_v:.2e}")

        # IF batch mean shifted unexpectedly
        baseline_vals = [r for r in baseline_batch if isinstance(r, (int, float))]
        if baseline_vals:
            batch_mean    = mean_v
            baseline_mean = sum(baseline_vals) / len(baseline_vals)
            if abs(batch_mean - baseline_mean) > MEAN_SHIFT_THRESHOLD:
                adl.add_fault("mean_shift", field="result_batch",
                              detail=f"current_mean={batch_mean:.4f}, "
                                     f"baseline_mean={baseline_mean:.4f}")
                faults.append("mean_shift")

    # IF drift detected from baseline
    drift_score = submission.get("distribution_distance", 0.0)
    if drift_score > DRIFT_THRESHOLD:
        adl.add_fault("distribution_drift", field="result_batch",
                      detail=f"distribution_distance={drift_score:.3f} > {DRIFT_THRESHOLD}")
        faults.append(f"distribution_drift: {drift_score:.3f}")

    # IF error cluster detected
    fault_density = submission.get("fault_density_in_window", 0.0)
    if fault_density > CLUSTER_THRESHOLD:
        adl.add_fault("clustered_failure", field="recent_results",
                      detail=f"fault_density={fault_density:.3f} > threshold {CLUSTER_THRESHOLD}")
        faults.append(f"clustered_failure: density={fault_density:.3f}")

    # IF result frequency impossible
    event_frequency = submission.get("event_frequency", 0)
    if event_frequency > MAX_DOMAIN_FREQUENCY:
        adl.add_fault("rate_anomaly", field="event_frequency",
                      detail=f"frequency={event_frequency} > max {MAX_DOMAIN_FREQUENCY}")
        faults.append(f"rate_anomaly: {event_frequency}")

    adl.record(8, "Statistical / Distribution",
               {"numeric_value": numeric_value,
                "hist_mean": hist_mean,
                "hist_std": hist_std,
                "batch_size": len(result_batch)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} statistical faults", faults)


def gate_9_robustness(submission: dict, active_modes: list,
                       adl: AuditDecisionLog) -> None:
    """
    Gate 9 — Robustness Controls
    Checks reproducibility, boundary failures, adversarial vulnerability, and load sensitivity.
    Only active when robustness mode is active AND test cases are provided.
    """
    if not _is_mode_active(active_modes, "robustness"):
        adl.record(9, "Robustness", {}, "SKIP", "robustness mode not active")
        return

    test_results = submission.get("robustness_test_results")
    if test_results is None:
        adl.record(9, "Robustness", {}, "SKIP",
                   "robustness_test_results not provided — TODO: DATA_DEPENDENCY")
        return

    faults = []

    sensitivity        = test_results.get("sensitivity_metric", 0.0)
    repeat_variance    = test_results.get("repeat_variance", 0.0)
    adversarial_rate   = test_results.get("adversarial_success_rate", 0.0)
    optional_removed   = test_results.get("optional_input_removed_output_invalid", False)
    boundary_failed    = test_results.get("boundary_case_fail", False)
    load_correctness   = test_results.get("correctness_under_load", 1.0)

    # IF small input perturbation causes large output change
    if sensitivity > INSTABILITY_THRESHOLD:
        adl.add_fault("unstable_result", field="sensitivity_metric",
                      detail=f"sensitivity={sensitivity:.3f} > threshold {INSTABILITY_THRESHOLD}")
        faults.append(f"unstable_result: sensitivity={sensitivity:.3f}")

    # IF repeated run yields inconsistent result
    if repeat_variance > REPRODUCIBILITY_THRESHOLD:
        adl.add_fault("non_reproducible", field="repeat_variance",
                      detail=f"variance={repeat_variance:.4f} > threshold {REPRODUCIBILITY_THRESHOLD}")
        faults.append(f"non_reproducible: variance={repeat_variance:.4f}")

    # IF adversarial input breaks output
    if adversarial_rate > ADVERSARIAL_THRESHOLD:
        adl.add_fault("adversarial_vulnerability", field="adversarial_success_rate",
                      detail=f"adversarial_rate={adversarial_rate:.3f} > threshold {ADVERSARIAL_THRESHOLD}")
        faults.append(f"adversarial_vulnerability: rate={adversarial_rate:.3f}")

    # IF missing optional input causes catastrophic failure
    if optional_removed:
        adl.add_fault("brittle_dependency", field="optional_input",
                      detail="output invalid when optional input removed")
        faults.append("brittle_dependency")

    # IF boundary condition fails
    if boundary_failed:
        adl.add_fault("boundary_failure", field="boundary_case",
                      detail="boundary condition test case failed")
        faults.append("boundary_failure")

    # IF stress load degrades correctness
    if load_correctness < LOAD_PERFORMANCE_THRESHOLD:
        adl.add_fault("load_sensitivity", field="correctness_under_load",
                      detail=f"load_correctness={load_correctness:.3f} < {LOAD_PERFORMANCE_THRESHOLD}")
        faults.append(f"load_sensitivity: correctness={load_correctness:.3f}")

    adl.record(9, "Robustness",
               {"sensitivity": sensitivity,
                "repeat_variance": repeat_variance,
                "adversarial_rate": adversarial_rate,
                "boundary_failed": boundary_failed},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} robustness faults", faults)


def gate_10_reasoning(submission: dict, active_modes: list,
                       adl: AuditDecisionLog) -> None:
    """
    Gate 10 — Reasoning / Causality Controls
    Audits the logical chain from premises to conclusion.
    Only active when consistency mode is active AND reasoning chain is present.
    """
    if not _is_mode_active(active_modes, "consistency"):
        adl.record(10, "Reasoning / Causality", {}, "SKIP", "consistency mode not active")
        return

    reasoning = submission.get("result_payload", {}).get("reasoning_chain")
    if not reasoning:
        adl.record(10, "Reasoning / Causality", {}, "SKIP",
                   "no reasoning_chain present; skipping")
        return

    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    inference_score     = submission.get("inference_path_score", 1.0)
    causal_language     = submission.get("causal_language_present", False)
    causal_evidence     = submission.get("causal_evidence_present", True)
    feature_importances = submission.get("feature_importances", {})
    stated_result       = submission.get("result_payload", {}).get("result")
    output_before_rationale = submission.get("output_generated_before_rationale", False)
    rationale_match_score   = submission.get("rationale_match_score", 1.0)
    steps               = reasoning if isinstance(reasoning, list) else []

    # IF conclusion unsupported by premises
    if inference_score < SUPPORT_THRESHOLD:
        adl.add_fault("unsupported_inference", field="reasoning_chain",
                      detail=f"inference_path_score={inference_score:.3f} < {SUPPORT_THRESHOLD}")
        faults.append(f"unsupported_inference: score={inference_score:.3f}")

    # IF chain contains non sequitur (step disconnected from prior step)
    gaps = rule_set.get("reasoning_gaps", [])
    for gap_index in gaps:
        adl.add_fault("reasoning_gap", field=f"step_{gap_index}",
                      detail=f"step {gap_index} logically disconnected from step {gap_index - 1}")
        faults.append(f"reasoning_gap: step {gap_index}")

    # IF causal claim made from correlation only
    if causal_language and not causal_evidence:
        adl.add_fault("causal_overreach", field="reasoning_chain",
                      detail="causal language present but causal evidence absent")
        faults.append("causal_overreach")

    # IF explanation omits decisive variable
    mentioned_features = set(rule_set.get("mentioned_features", []))
    for feature, importance in feature_importances.items():
        if importance > IMPORTANCE_THRESHOLD and feature not in mentioned_features:
            adl.add_fault("omitted_key_factor", field=feature,
                          detail=f"feature '{feature}' importance={importance:.3f} > {IMPORTANCE_THRESHOLD} but not mentioned")
            faults.append(f"omitted_key_factor: {feature}")

    # IF rationale contradicts output
    rationale_implication = rule_set.get("rationale_implication")
    if rationale_implication is not None and rationale_implication != stated_result:
        adl.add_fault("rationale_output_conflict", field="result",
                      detail=f"rationale implies '{rationale_implication}', result is '{stated_result}'")
        faults.append("rationale_output_conflict")

    # IF explanation post-hoc only
    if output_before_rationale and rationale_match_score < RATIONALE_MATCH_FLOOR:
        adl.add_fault("posthoc_justification_risk", field="rationale",
                      detail=f"output generated before rationale; match_score={rationale_match_score:.3f}")
        faults.append(f"posthoc_justification_risk: match={rationale_match_score:.3f}")

    adl.record(10, "Reasoning / Causality",
               {"inference_score": inference_score,
                "causal_language": causal_language,
                "reasoning_steps": len(steps)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} reasoning faults", faults)


def gate_11_policy(submission: dict, active_modes: list,
                    adl: AuditDecisionLog) -> None:
    """
    Gate 11 — Constraint / Policy Controls
    Checks hard rules, soft preferences, prohibited content, disclaimers, and audit trail.
    Only active when compliance mode is active.
    """
    if not _is_mode_active(active_modes, "compliance"):
        adl.record(11, "Constraint / Policy", {}, "SKIP", "compliance mode not active")
        return

    result   = submission.get("result_payload", {})
    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    # IF output violates hard rule
    hard_rules = rule_set.get("hard_rules", [])
    for rule in hard_rules:
        field      = rule.get("field")
        constraint = rule.get("constraint")
        if field in result:
            if not _check_domain_rule(result[field], constraint):
                adl.add_fault("hard_rule_violation", field=field,
                              detail=f"hard rule '{constraint}' violated on field '{field}'")
                faults.append(f"hard_rule_violation: {field}")

    # IF output violates soft preference threshold
    preference_score = submission.get("preference_score", 1.0)
    if preference_score < SOFT_PREFERENCE_THRESHOLD:
        adl.add_fault("soft_preference_failure", field="preference_score",
                      detail=f"preference_score={preference_score:.3f} < {SOFT_PREFERENCE_THRESHOLD}")
        faults.append(f"soft_preference_failure: {preference_score:.3f}")

    # IF prohibited content present
    prohibited_patterns = rule_set.get("prohibited_content_patterns", [])
    result_text = json.dumps(result).lower()
    for pattern in prohibited_patterns:
        if pattern.lower() in result_text:
            adl.add_fault("prohibited_output", field="result_payload",
                          detail=f"prohibited pattern '{pattern}' found in output")
            faults.append(f"prohibited_output: pattern='{pattern}'")

    # IF required disclaimer missing
    required_disclaimers = rule_set.get("required_disclaimers", [])
    for disc in required_disclaimers:
        if disc.lower() not in result_text:
            adl.add_fault("missing_disclaimer", field="disclaimer",
                          detail=f"required disclaimer '{disc}' absent")
            faults.append(f"missing_disclaimer: '{disc}'")

    # IF restricted action recommended
    restricted_flag = submission.get("restricted_action_flag", False)
    if restricted_flag:
        adl.add_fault("restricted_recommendation", field="action",
                      detail="result recommends a restricted action")
        faults.append("restricted_recommendation")

    # IF audit trail noncompliant
    trace_completeness = submission.get("trace_log_completeness", 1.0)
    if trace_completeness < TRACE_MINIMUM:
        adl.add_fault("audit_trail_failure", field="trace_log",
                      detail=f"trace_completeness={trace_completeness:.3f} < min {TRACE_MINIMUM}")
        faults.append(f"audit_trail_failure: completeness={trace_completeness:.3f}")

    adl.record(11, "Constraint / Policy",
               {"hard_rules_checked": len(hard_rules),
                "prohibited_patterns_checked": len(prohibited_patterns),
                "trace_completeness": trace_completeness},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} policy faults", faults)


def gate_12_provenance(submission: dict, run_ts: datetime,
                        adl: AuditDecisionLog) -> None:
    """
    Gate 12 — Data Provenance Controls
    Checks source quality, freshness, conflict resolution, and citation support.
    """
    sources   = submission.get("sources", [])
    citations = submission.get("citations", [])
    external_claim_count = submission.get("external_claim_count", 0)
    cited_claim_count    = submission.get("cited_claim_count", 0)
    rule_set  = submission.get("evaluation_rule_set", {})
    faults    = []

    for i, src in enumerate(sources):
        provenance_conf = src.get("provenance_confidence", 1.0)
        src_timestamp   = src.get("timestamp")
        priority        = src.get("priority", 1)
        available_higher_priority = src.get("higher_priority_available", False)

        # IF source provenance unverifiable
        if provenance_conf < PROVENANCE_THRESHOLD:
            adl.add_fault("unverifiable_source", field=f"source[{i}]",
                          detail=f"provenance_confidence={provenance_conf:.3f} < {PROVENANCE_THRESHOLD}")
            faults.append(f"unverifiable_source: source[{i}]")

        # IF source freshness insufficient
        if src_timestamp:
            try:
                if isinstance(src_timestamp, str):
                    src_dt = datetime.fromisoformat(src_timestamp.replace("Z", "+00:00"))
                else:
                    src_dt = src_timestamp
                if src_dt.tzinfo is None:
                    src_dt = src_dt.replace(tzinfo=timezone.utc)
                age_hours = (run_ts - src_dt).total_seconds() / 3600
                if age_hours > FRESHNESS_LIMIT_HOURS:
                    adl.add_fault("stale_source", field=f"source[{i}]",
                                  detail=f"source age {age_hours:.1f}h > limit {FRESHNESS_LIMIT_HOURS}h")
                    faults.append(f"stale_source: source[{i}]")
            except (ValueError, TypeError):
                pass

        # IF source hierarchy violated
        if available_higher_priority:
            adl.add_fault("source_priority_failure", field=f"source[{i}]",
                          detail=f"lower-priority source used while higher-priority available")
            faults.append(f"source_priority_failure: source[{i}]")

    # IF conflicting sources used without resolution
    conflict_count = submission.get("source_conflict_count", 0)
    resolution_note = submission.get("source_resolution_note")
    if conflict_count > 0 and not resolution_note:
        adl.add_fault("unresolved_source_conflict", field="sources",
                      detail=f"{conflict_count} source conflict(s) without resolution note")
        faults.append(f"unresolved_source_conflict: count={conflict_count}")

    # IF citation missing for external claim
    if external_claim_count > cited_claim_count:
        gap = external_claim_count - cited_claim_count
        adl.add_fault("missing_citation", field="citations",
                      detail=f"external_claims={external_claim_count}, cited={cited_claim_count}, gap={gap}")
        faults.append(f"missing_citation: gap={gap}")

    # IF citation does not support claim
    for j, citation in enumerate(citations):
        support_score = citation.get("support_score", 1.0)
        if support_score < CITATION_SUPPORT_THRESHOLD:
            adl.add_fault("unsupported_citation", field=f"citation[{j}]",
                          detail=f"support_score={support_score:.3f} < {CITATION_SUPPORT_THRESHOLD}")
            faults.append(f"unsupported_citation: citation[{j}]")

    adl.record(12, "Data Provenance",
               {"source_count": len(sources),
                "citation_count": len(citations),
                "external_claims": external_claim_count,
                "cited_claims": cited_claim_count},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} provenance faults", faults)


def gate_13_temporal(submission: dict, run_ts: datetime,
                      audit_start_ts: datetime, adl: AuditDecisionLog) -> None:
    """
    Gate 13 — Temporal Controls
    Detects outdated assumptions, data leakage, audit latency, window issues,
    and cadence failures.
    """
    result   = submission.get("result_payload", {})
    rule_set = submission.get("evaluation_rule_set", {})
    faults   = []

    validity_window_start = rule_set.get("validity_window_start")
    decision_timestamp    = submission.get("decision_timestamp")
    intended_window       = rule_set.get("intended_evaluation_window")
    actual_window         = submission.get("actual_evaluation_window")
    expected_interval     = rule_set.get("expected_interval_seconds")
    actual_interval       = submission.get("actual_interval_seconds")
    assumptions           = result.get("assumptions", [])
    features              = submission.get("features", {})

    # IF result uses outdated assumption
    if validity_window_start and isinstance(validity_window_start, str):
        for i, assumption in enumerate(assumptions):
            ts = assumption.get("timestamp")
            if ts and isinstance(ts, str) and ts < validity_window_start:
                adl.add_fault("outdated_assumption", field=f"assumptions[{i}]",
                              detail=f"assumption_ts={ts} < validity_window_start={validity_window_start}")
                faults.append(f"outdated_assumption: assumptions[{i}]")

    # IF future information leaked into historical result
    if decision_timestamp and isinstance(decision_timestamp, str):
        for feature, meta in features.items():
            feature_ts = meta.get("timestamp") if isinstance(meta, dict) else None
            if feature_ts and isinstance(feature_ts, str) and feature_ts > decision_timestamp:
                adl.add_fault("data_leakage", field=feature,
                              detail=f"feature_timestamp={feature_ts} > decision_timestamp={decision_timestamp}")
                faults.append(f"data_leakage: feature='{feature}'")

    # IF latency exceeds acceptable audit window
    audit_elapsed = (run_ts - audit_start_ts).total_seconds()
    if audit_elapsed > MAX_AUDIT_LATENCY_SECONDS:
        adl.add_fault("late_detection", field="audit_latency",
                      detail=f"audit_elapsed={audit_elapsed:.1f}s > max {MAX_AUDIT_LATENCY_SECONDS}s")
        faults.append(f"late_detection: elapsed={audit_elapsed:.1f}s")

    # IF time window misapplied
    if intended_window and actual_window and intended_window != actual_window:
        adl.add_fault("window_misapplication", field="evaluation_window",
                      detail=f"intended='{intended_window}', actual='{actual_window}'")
        faults.append("window_misapplication")

    # IF periodic result missing expected cadence
    if expected_interval and actual_interval:
        if expected_interval > 0:
            deviation = abs(actual_interval - expected_interval) / expected_interval
            if deviation > CADENCE_TOLERANCE:
                adl.add_fault("cadence_failure", field="interval",
                              detail=f"expected={expected_interval}s, actual={actual_interval}s, "
                                     f"deviation={deviation:.3f}")
                faults.append(f"cadence_failure: deviation={deviation:.3f}")

    adl.record(13, "Temporal Controls",
               {"audit_elapsed_s": round(audit_elapsed, 1),
                "data_leakage_features_checked": len(features),
                "assumptions_checked": len(assumptions)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} temporal faults", faults)


def gate_14_explanation(submission: dict, active_modes: list,
                         adl: AuditDecisionLog) -> None:
    """
    Gate 14 — Explanation Quality Controls
    Audits explanation presence, specificity, certainty, and format compliance.
    """
    if not (_is_mode_active(active_modes, "completeness") or
            _is_mode_active(active_modes, "compliance")):
        adl.record(14, "Explanation Quality", {}, "SKIP",
                   "completeness/compliance mode not active")
        return

    result    = submission.get("result_payload", {})
    rule_set  = submission.get("evaluation_rule_set", {})
    faults    = []

    explanation_text    = result.get("explanation") or result.get("rationale") or ""
    explanation_required = rule_set.get("explanation_required", False)
    uncertainty_required = rule_set.get("uncertainty_required", False)
    required_format     = rule_set.get("explanation_format")
    specificity_score   = submission.get("explanation_specificity_score", 1.0)
    language_certainty  = submission.get("language_certainty_score", 0.5)
    evidence_strength   = submission.get("evidence_strength_score", 0.5)

    # IF explanation absent
    if explanation_required and not explanation_text:
        adl.add_fault("no_explanation", field="explanation",
                      detail="explanation required but absent")
        faults.append("no_explanation")

    if explanation_text:
        # IF explanation too vague
        if specificity_score < SPECIFICITY_THRESHOLD:
            adl.add_fault("vague_explanation", field="explanation",
                          detail=f"specificity_score={specificity_score:.3f} < {SPECIFICITY_THRESHOLD}")
            faults.append(f"vague_explanation: specificity={specificity_score:.3f}")

        # IF explanation too confident for evidence level
        if language_certainty > evidence_strength + CONFIDENCE_GAP_THRESHOLD:
            gap = language_certainty - evidence_strength
            adl.add_fault("overclaimed_certainty", field="explanation",
                          detail=f"language_certainty={language_certainty:.3f}, "
                                 f"evidence_strength={evidence_strength:.3f}, gap={gap:.3f}")
            faults.append(f"overclaimed_certainty: gap={gap:.3f}")

        # IF explanation omits uncertainty
        uncertainty_terms = {"may", "might", "could", "possibly", "uncertain",
                             "approximately", "estimated", "approximately"}
        has_uncertainty = any(t in explanation_text.lower() for t in uncertainty_terms)
        if uncertainty_required and not has_uncertainty:
            adl.add_fault("hidden_uncertainty", field="explanation",
                          detail="uncertainty required but no uncertainty language found")
            faults.append("hidden_uncertainty")

        # IF explanation overly long but low information
        char_len = len(explanation_text)
        word_count = len(explanation_text.split())
        info_density = specificity_score * (min(word_count, 100) / 100.0)
        if info_density < DENSITY_THRESHOLD and char_len > VERBOSITY_THRESHOLD:
            adl.add_fault("low_information_explanation", field="explanation",
                          detail=f"info_density={info_density:.3f} < {DENSITY_THRESHOLD}, "
                                 f"length={char_len}")
            faults.append(f"low_information_explanation: density={info_density:.3f}")

        # IF explanation not aligned with requested format
        if required_format:
            if not _check_explanation_format(explanation_text, required_format):
                adl.add_fault("explanation_format_failure", field="explanation",
                              detail=f"format mismatch: required '{required_format}'")
                faults.append(f"explanation_format_failure: required '{required_format}'")

    adl.record(14, "Explanation Quality",
               {"explanation_length": len(explanation_text),
                "specificity_score": specificity_score,
                "language_certainty": language_certainty,
                "evidence_strength": evidence_strength},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} explanation faults", faults)


def _check_explanation_format(text: str, required_format: str) -> bool:
    """Check explanation against a named format requirement."""
    if required_format == "bullet_points":
        return "•" in text or text.strip().startswith("-")
    if required_format == "numbered_steps":
        import re
        return bool(re.search(r"\d+\.", text))
    if required_format == "json":
        try:
            json.loads(text)
            return True
        except (json.JSONDecodeError, ValueError):
            return False
    return True


def gate_15_ranking(submission: dict, adl: AuditDecisionLog) -> None:
    """
    Gate 15 — Ranking / Prioritization Controls
    Audits ranked outputs for correctness, internal consistency, and completeness.
    Only active when the result contains a ranked list.
    """
    result = submission.get("result_payload", {})
    ranked = result.get("ranked_results") or result.get("top_k") or result.get("ranking")

    if not ranked or not isinstance(ranked, list):
        adl.record(15, "Ranking / Prioritization", {}, "SKIP",
                   "no ranked list found in result")
        return

    rule_set      = submission.get("evaluation_rule_set", {})
    true_best     = rule_set.get("true_best_item")
    true_relevant = set(rule_set.get("true_relevant_items", []))
    faults        = []

    # IF top-ranked item incorrect
    if true_best and ranked and ranked[0].get("id") != true_best:
        adl.add_fault("top_rank_failure", field="ranked_results[0]",
                      detail=f"rank_1={ranked[0].get('id')}, true_best={true_best}")
        faults.append("top_rank_failure")

    # IF ranking order inconsistent with scores
    scores = [r.get("score") for r in ranked if r.get("score") is not None]
    if scores and scores != sorted(scores, reverse=True):
        adl.add_fault("score_rank_inconsistency", field="ranked_results",
                      detail=f"ranking order does not match descending score order")
        faults.append("score_rank_inconsistency")

    # IF important item omitted from top-k
    top_k_ids = {r.get("id") for r in ranked}
    missing   = true_relevant - top_k_ids
    if missing:
        adl.add_fault("recall_failure_topk", field="ranked_results",
                      detail=f"true relevant items absent from top-k: {missing}")
        faults.append(f"recall_failure_topk: missing={missing}")

    # IF irrelevant item included in top-k
    irrelevant_ids = rule_set.get("irrelevant_items", set())
    irrelevant_present = top_k_ids & set(irrelevant_ids)
    if irrelevant_present:
        adl.add_fault("precision_failure_topk", field="ranked_results",
                      detail=f"irrelevant items in top-k: {irrelevant_present}")
        faults.append(f"precision_failure_topk: {irrelevant_present}")

    # IF score ties unresolved improperly
    unique_scores = set(scores)
    tie_count = len(scores) - len(unique_scores)
    tie_break_applied = submission.get("tie_break_applied", True)
    if tie_count > 0 and not tie_break_applied:
        adl.add_fault("tie_break_failure", field="ranked_results",
                      detail=f"{tie_count} tied score(s) without tie-break rule applied")
        faults.append(f"tie_break_failure: ties={tie_count}")

    adl.record(15, "Ranking / Prioritization",
               {"ranked_count": len(ranked),
                "true_relevant_count": len(true_relevant),
                "tie_count": tie_count},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} ranking faults", faults)


def gate_16_calibration(submission: dict, adl: AuditDecisionLog) -> None:
    """
    Gate 16 — Confidence Calibration Controls
    Detects overconfidence, underconfidence, calibration drift, and evidence mismatch.
    """
    result           = submission.get("result_payload", {})
    confidence       = result.get("confidence")
    rule_set         = submission.get("evaluation_rule_set", {})
    has_fault        = len(adl.fault_register) > 0
    calibration_err  = submission.get("calibration_error", 0.0)
    evidence_quality = submission.get("evidence_quality_score", confidence or 0.5)
    faults           = []

    confidence_required = rule_set.get("confidence_required", False)

    # IF confidence missing where required
    if confidence_required and confidence is None:
        adl.add_fault("missing_confidence", field="confidence",
                      detail="confidence required but null")
        faults.append("missing_confidence")

    if confidence is not None:
        # IF confidence high and result wrong
        if confidence >= HIGH_CONFIDENCE_THRESHOLD and has_fault:
            adl.add_fault("overconfidence", field="confidence",
                          detail=f"confidence={confidence:.3f} >= {HIGH_CONFIDENCE_THRESHOLD} but faults detected")
            faults.append(f"overconfidence: confidence={confidence:.3f}")

        # IF confidence low and result correct
        if confidence <= LOW_CONFIDENCE_THRESHOLD and not has_fault:
            adl.add_fault("underconfidence", field="confidence",
                          detail=f"confidence={confidence:.3f} <= {LOW_CONFIDENCE_THRESHOLD} but no faults")
            faults.append(f"underconfidence: confidence={confidence:.3f}")

        # IF confidence inconsistent with evidence quality
        if abs(confidence - evidence_quality) > CONFIDENCE_EVIDENCE_GAP:
            gap = abs(confidence - evidence_quality)
            adl.add_fault("confidence_evidence_mismatch", field="confidence",
                          detail=f"confidence={confidence:.3f}, evidence_quality={evidence_quality:.3f}, gap={gap:.3f}")
            faults.append(f"confidence_evidence_mismatch: gap={gap:.3f}")

    # IF calibration drift detected
    if calibration_err > CALIBRATION_THRESHOLD:
        adl.add_fault("miscalibrated_confidence", field="calibration_error",
                      detail=f"calibration_error={calibration_err:.3f} > {CALIBRATION_THRESHOLD}")
        faults.append(f"miscalibrated_confidence: error={calibration_err:.3f}")

    adl.record(16, "Confidence Calibration",
               {"confidence": confidence,
                "evidence_quality": evidence_quality,
                "calibration_error": calibration_err,
                "faults_so_far": len(adl.fault_register)},
               "faults_detected" if faults else "PASS",
               f"{len(faults)} calibration faults", faults)


def gate_17_severity(adl: AuditDecisionLog) -> dict:
    """
    Gate 17 — Fault Severity Controls
    Assigns the maximum severity level across all detected faults.
    """
    severity_rank = {"none": 0, "low": 1, "medium": 2,
                     "high": 3, "escalated_high": 4,
                     "critical": 5, "systemic": 6}
    fault_register = adl.fault_register

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in fault_register:
        sev = f.get("severity", "low")
        if sev in counts:
            counts[sev] += 1

    # Risk of harm → critical overrides everything
    harm_flags = [f for f in fault_register
                  if f.get("fault_state") in {
                      "hard_rule_violation", "prohibited_output",
                      "restricted_recommendation", "adversarial_vulnerability",
                      "data_leakage",
                  }]

    # Determine max severity
    if harm_flags or counts["critical"] > 0:
        max_severity = "critical"
    elif counts["medium"] >= ESCALATION_COUNT:
        max_severity = "escalated_high"
    elif counts["high"] > 0:
        max_severity = "high"
    elif counts["medium"] > 0:
        max_severity = "medium"
    elif counts["low"] > 0:
        max_severity = "low"
    else:
        max_severity = "none"

    # Systemic check (requires repeat_fault_count from evaluation ledger)
    # Applied in gate 21 when historical pattern data is available

    adl.record(17, "Fault Severity",
               {"critical_count": counts["critical"],
                "high_count": counts["high"],
                "medium_count": counts["medium"],
                "low_count": counts["low"],
                "total_faults": len(fault_register)},
               max_severity,
               f"max_severity: {max_severity}")
    return {"max_severity": max_severity, "fault_counts": counts}


def gate_18_root_cause(submission: dict, active_modes: list,
                        adl: AuditDecisionLog) -> list:
    """
    Gate 18 — Root Cause Attribution Controls
    Attributes detected faults to one or more root causes.
    """
    rule_set             = submission.get("evaluation_rule_set", {})
    input_quality        = submission.get("input_quality_score", 1.0)
    rule_version         = submission.get("rule_set_version")
    expected_rule_version = rule_set.get("expected_version")
    repeat_variance      = submission.get("robustness_test_results", {}).get("repeat_variance", 0.0)
    has_stale_source     = any(f["fault_state"] == "stale_source"
                               for f in adl.fault_register)
    has_domain_failure   = any(f["fault_state"] == "domain_logic_failure"
                               for f in adl.fault_register)
    domain_constraint_absent = rule_set.get("domain_constraint_absent", False)
    edge_case_match      = submission.get("edge_case_signature_match", False)
    root_cause_probs     = submission.get("root_cause_probabilities", {})

    root_causes = []

    # IF fault linked to bad input
    if input_quality < INPUT_QUALITY_THRESHOLD:
        root_causes.append("input_failure")

    # IF fault linked to rule mismatch
    if rule_version and expected_rule_version and rule_version != expected_rule_version:
        root_causes.append("rules_failure")

    # IF fault linked to model instability
    if repeat_variance > REPRODUCIBILITY_THRESHOLD:
        root_causes.append("model_instability")

    # IF fault linked to stale data
    if has_stale_source:
        root_causes.append("stale_data")

    # IF fault linked to missing business logic
    if domain_constraint_absent and has_domain_failure:
        root_causes.append("missing_domain_logic")

    # IF fault linked to unsupported edge case
    if edge_case_match:
        root_causes.append("unhandled_edge_case")

    # IF root cause unclear
    if not root_causes:
        max_prob = max(root_cause_probs.values(), default=0.0)
        if max_prob < ROOT_CAUSE_CONFIDENCE_FLOOR:
            root_causes.append("unresolved")

    adl.root_causes = root_causes
    adl.record(18, "Root Cause Attribution",
               {"input_quality": input_quality,
                "rule_version_match": rule_version == expected_rule_version,
                "stale_source": has_stale_source,
                "edge_case_match": edge_case_match},
               str(root_causes),
               f"root_causes: {root_causes}")
    return root_causes


def gate_19_classification(max_severity: str, fault_counts: dict,
                            root_causes: list, total_fault_score: float,
                            adl: AuditDecisionLog) -> str:
    """
    Gate 19 — Action Classification
    Combines severity, fault counts, and root cause into a single classification.
    Priority order matches AGENT5_SYSTEM_DESCRIPTION.md Gate 19 table.
    """
    critical_count = fault_counts.get("critical", 0)

    # Priority 1: critical fault
    if critical_count > 0:
        classification = "block_output"

    # Priority 2: systemic (evaluated in gate 21; placeholder here)
    elif max_severity == "systemic":
        classification = "escalate_systemic_issue"

    # Priority 3: unresolved root cause with medium+ severity
    elif "unresolved" in root_causes and max_severity in {"medium", "escalated_high", "high"}:
        classification = "manual_investigation"

    # Priority 4: high severity
    elif max_severity in {"high", "escalated_high"}:
        classification = "fail"

    # Priority 5: medium severity
    elif max_severity == "medium":
        classification = "review_recommended"

    # Priority 6: low severity
    elif max_severity == "low":
        classification = "pass_with_warnings"

    # Priority 7: pass
    elif fault_counts.get("critical", 0) == 0 and fault_counts.get("high", 0) == 0 \
            and total_fault_score < ACCEPT_THRESHOLD:
        classification = "pass"

    else:
        classification = "review_recommended"

    adl.classification = classification
    adl.record(19, "Action Classification",
               {"max_severity": max_severity,
                "critical_count": critical_count,
                "total_fault_score": round(total_fault_score, 4),
                "root_causes": root_causes},
               classification, f"classification: {classification}")
    return classification


def gate_20_remediation(classification: str, adl: AuditDecisionLog) -> list:
    """
    Gate 20 — Remediation Controls
    Assigns remediation directives to all active fault types.
    Directives are recommendations; block_output → suppress_release is immediate.
    """
    FAULT_REMEDIATION = {
        "missing_required_fields":  "request_or_reconstruct_missing_fields",
        "null_mandatory_value":     "request_or_reconstruct_missing_fields",
        "type_mismatch":            "coerce_or_reparse_types",
        "format_error":             "coerce_or_reparse_types",
        "unsupported_conclusion":   "require_evidence_attachment",
        "missing_explanation":      "require_evidence_attachment",
        "unsupported_citation":     "require_evidence_attachment",
        "stale_source":             "refresh_sources",
        "outdated_assumption":      "refresh_sources",
        "stale_data":               "refresh_sources",
        "reconciliation_error":     "recompute_aggregates",
        "summary_detail_mismatch":  "recompute_aggregates",
        "miscalibrated_confidence": "recalibrate_confidence_model",
        "overconfidence":           "recalibrate_confidence_model",
        "underconfidence":          "recalibrate_confidence_model",
    }

    directives = set()

    for fault in adl.fault_register:
        fs = fault.get("fault_state", "")
        if fs in FAULT_REMEDIATION:
            directives.add(FAULT_REMEDIATION[fs])

    # Classification-level overrides
    if classification == "escalate_systemic_issue":
        directives.add("quarantine_pipeline_segment")
    if classification == "block_output":
        directives.add("suppress_release")

    directive_list = sorted(directives)
    adl.remediation = directive_list
    adl.record(20, "Remediation Controls",
               {"fault_count": len(adl.fault_register),
                "classification": classification},
               "directives_assigned", f"directives: {directive_list}")
    return directive_list


def gate_21_evaluation_loop(submission: dict, classification: str,
                              adl: AuditDecisionLog) -> None:
    """
    Gate 21 — Evaluation Loop
    Stores the audit record, updates fault library with confirmed patterns,
    and adjusts thresholds based on observed false positive / missed fault rates.

    Threshold adjustment updates are logged as suggestions via db_helpers.
    TODO: DATA_DEPENDENCY — threshold update feedback requires accumulated audit history.
    """
    # IF audited result stored
    store_record = classification not in set()  # all classifications are stored

    # Emerging system issue detection
    fault_pattern_trend = submission.get("fault_pattern_trend", 0.0)
    if fault_pattern_trend > GROWTH_THRESHOLD:
        adl.add_fault("clustered_failure", field="fault_pattern_trend",
                      detail=f"emerging_system_issue: trend={fault_pattern_trend:.3f}")
        adl.classification = "escalate_systemic_issue"
        classification     = "escalate_systemic_issue"

    # Missed fault / false positive rate checks
    missed_fault_rate   = submission.get("missed_fault_rate", 0.0)
    false_positive_rate = submission.get("false_positive_rate", 0.0)
    threshold_note      = "no_adjustment_needed"

    if missed_fault_rate > MISSED_FAULT_THRESHOLD:
        threshold_note = "tighten_audit_rules: missed_fault_rate exceeded"
        _db.post_suggestion(
            created_by="result_audit_agent",
            target_files=["result_audit_agent.py"],
            description=f"Audit threshold review: missed_fault_rate={missed_fault_rate:.3f} "
                        f"> threshold {MISSED_FAULT_THRESHOLD}. Consider tightening rules.",
            risk_level="LOW",
            impact="audit_calibration",
            metadata=json.dumps({"missed_fault_rate": missed_fault_rate}),
        )
    elif false_positive_rate > FALSE_POSITIVE_THRESHOLD:
        threshold_note = "relax_audit_rules: false_positive_rate exceeded"
        _db.post_suggestion(
            created_by="result_audit_agent",
            target_files=["result_audit_agent.py"],
            description=f"Audit threshold review: false_positive_rate={false_positive_rate:.3f} "
                        f"> threshold {FALSE_POSITIVE_THRESHOLD}. Consider relaxing rules.",
            risk_level="LOW",
            impact="audit_calibration",
            metadata=json.dumps({"false_positive_rate": false_positive_rate}),
        )

    adl.record(21, "Evaluation Loop",
               {"store_record": store_record,
                "missed_fault_rate": missed_fault_rate,
                "false_positive_rate": false_positive_rate,
                "fault_pattern_trend": fault_pattern_trend},
               "stored", threshold_note)


def gate_22_output(classification: str, root_causes: list,
                    audit_confidence: float, adl: AuditDecisionLog) -> str:
    """
    Gate 22 — Output Controls
    Determines the final output action.
    """
    ACTION_MAP = {
        "pass":                    "approve_result",
        "pass_with_warnings":      "approve_with_warning_log",
        "review_recommended":      "send_to_manual_review",
        "fail":                    "reject_result",
        "block_output":            "block_downstream_use",
        "escalate_systemic_issue": "trigger_system_escalation",
        "manual_investigation":    "request_investigation",
    }
    output_action = ACTION_MAP.get(classification, "send_to_manual_review")

    # IF confidence in audit low → override
    if audit_confidence < AUDIT_CONFIDENCE_THRESHOLD:
        output_action = "mark_audit_uncertain"

    # IF root cause unresolved → request investigation (appended, not overriding block)
    if "unresolved" in root_causes and output_action != "block_downstream_use":
        output_action = "request_investigation"

    adl.output_action = output_action
    adl.record(22, "Output Controls",
               {"classification": classification,
                "audit_confidence": round(audit_confidence, 3),
                "root_causes": root_causes},
               output_action, f"output_action: {output_action}")
    return output_action


def gate_23_composite_score(adl: AuditDecisionLog) -> dict:
    """
    Gate 23 — Final Composite Fault Score
    Computes a weighted fault score across all fault categories.
    Higher score = more severely faulty.
    """
    fault_register = adl.fault_register

    # Map each fault to its category
    FAULT_CATEGORY = {
        "incorrect_result":           "correctness",
        "misclassification":          "correctness",
        "numeric_error":              "correctness",
        "unit_error":                 "correctness",
        "sign_error":                 "correctness",
        "magnitude_error":            "correctness",
        "directional_error":          "correctness",
        "internal_conflict":          "consistency",
        "summary_detail_mismatch":    "consistency",
        "reconciliation_error":       "consistency",
        "temporal_inconsistency":     "consistency",
        "label_evidence_conflict":    "consistency",
        "status_conflict":            "consistency",
        "domain_logic_failure":       "consistency",
        "unsupported_inference":      "consistency",
        "reasoning_gap":              "consistency",
        "rationale_output_conflict":  "consistency",
        "incomplete_result":          "completeness",
        "missing_explanation":        "completeness",
        "unsupported_conclusion":     "completeness",
        "edge_case_coverage_gap":     "completeness",
        "truncated_output":           "completeness",
        "mode_completeness_failure":  "completeness",
        "missing_confidence":         "completeness",
        "hard_rule_violation":        "policy",
        "prohibited_output":          "policy",
        "restricted_recommendation":  "policy",
        "missing_disclaimer":         "policy",
        "soft_preference_failure":    "policy",
        "audit_trail_failure":        "policy",
        "unstable_result":            "robustness",
        "non_reproducible":           "robustness",
        "adversarial_vulnerability":  "robustness",
        "brittle_dependency":         "robustness",
        "boundary_failure":           "robustness",
        "load_sensitivity":           "robustness",
        "unverifiable_source":        "provenance",
        "stale_source":               "provenance",
        "unresolved_source_conflict": "provenance",
        "missing_citation":           "provenance",
        "unsupported_citation":       "provenance",
        "source_priority_failure":    "provenance",
        "overconfidence":             "calibration",
        "underconfidence":            "calibration",
        "miscalibrated_confidence":   "calibration",
        "confidence_evidence_mismatch": "calibration",
    }

    SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.70, "medium": 0.40,
                       "low": 0.15, "none": 0.0}

    category_scores = {
        "correctness":  0.0,
        "consistency":  0.0,
        "completeness": 0.0,
        "policy":       0.0,
        "robustness":   0.0,
        "provenance":   0.0,
        "calibration":  0.0,
    }

    for fault in fault_register:
        fs  = fault.get("fault_state", "")
        sev = fault.get("severity", "low")
        cat = FAULT_CATEGORY.get(fs, "consistency")
        category_scores[cat] = min(category_scores[cat] + SEVERITY_WEIGHT.get(sev, 0.15), 1.0)

    fault_score = (
        W1_CORRECTNESS  * category_scores["correctness"]
      + W2_CONSISTENCY  * category_scores["consistency"]
      + W3_COMPLETENESS * category_scores["completeness"]
      + W4_POLICY       * category_scores["policy"]
      + W5_ROBUSTNESS   * category_scores["robustness"]
      + W6_PROVENANCE   * category_scores["provenance"]
      + W7_CALIBRATION  * category_scores["calibration"]
    )
    fault_score = max(0.0, min(fault_score, 1.0))

    # Final audit signal
    critical_count = sum(1 for f in fault_register if f.get("severity") == "critical")
    if critical_count > 0:
        final_signal = "blocked"
    elif adl.classification == "escalate_systemic_issue":
        final_signal = "systemic_failure"
    elif fault_score >= FAIL_THRESHOLD:
        final_signal = "faulty"
    elif fault_score >= WARNING_THRESHOLD:
        final_signal = "warning"
    else:
        final_signal = "clean"

    adl.fault_score         = round(fault_score, 4)
    adl.final_audit_signal  = final_signal

    components = {
        "correctness":  round(W1_CORRECTNESS  * category_scores["correctness"],  4),
        "consistency":  round(W2_CONSISTENCY  * category_scores["consistency"],  4),
        "completeness": round(W3_COMPLETENESS * category_scores["completeness"], 4),
        "policy":       round(W4_POLICY       * category_scores["policy"],       4),
        "robustness":   round(W5_ROBUSTNESS   * category_scores["robustness"],   4),
        "provenance":   round(W6_PROVENANCE   * category_scores["provenance"],   4),
        "calibration":  round(W7_CALIBRATION  * category_scores["calibration"],  4),
    }

    adl.record(23, "Final Composite Fault Score",
               {"components": components,
                "fault_score": round(fault_score, 4),
                "critical_count": critical_count},
               final_signal,
               f"fault_score: {round(fault_score, 4)}, signal: {final_signal}")
    return {"fault_score": fault_score, "final_audit_signal": final_signal}


# ── PIPELINE ORCHESTRATOR ──────────────────────────────────────────────────────

def audit_result(submission: dict, audited_store: set = None,
                 audit_start_ts: datetime = None) -> dict:
    """
    Run a single result submission through all 23 gates.

    submission keys (all optional except result_payload and evaluation_rule_set):
      result_payload         — the result to audit (required)
      evaluation_rule_set    — rule set dict (required)
      reference_payload      — reference for comparison
      reference_required     — bool, default False
      ground_truth           — ground truth dict for correctness checks
      audit_modes            — list of mode names; empty = all modes
      result_timestamp       — ISO 8601 string
      source_provenance      — provenance identifier
      schema                 — schema dict for structural validation
      sources                — list of source dicts
      citations              — list of citation dicts
      robustness_test_results — dict of robustness test outputs
      historical_distribution — dict with mean, std, baseline_batch
      ... (see individual gate implementations for full key list)

    Returns the complete AuditDecisionLog as a dict.
    """
    run_ts = _now_utc()
    if audit_start_ts is None:
        audit_start_ts = run_ts
    if audited_store is None:
        audited_store = set()

    result_id   = submission.get("result_id",
                                  _result_hash(submission.get("result_payload", {}))[:12]
                                  if isinstance(submission.get("result_payload"), dict) else "unknown")
    result_type = submission.get("result_type", "unknown")
    requested_modes = submission.get("audit_modes", [])

    adl = AuditDecisionLog(result_id, result_type, requested_modes, run_ts)

    # ── GATE 1: System Gate ────────────────────────────────────────────────────
    if not gate_1_system(submission, audited_store, adl):
        return adl.to_dict()

    # ── GATE 2: Scope & Audit Mode ─────────────────────────────────────────────
    active_modes = gate_2_audit_mode(submission, adl)

    # ── GATE 3–16: Fault Detection Gates (non-halting) ────────────────────────
    gate_3_structure(submission, active_modes, adl)
    gate_4_consistency(submission, active_modes, adl)
    gate_5_completeness(submission, active_modes, adl)
    gate_6_correctness(submission, active_modes, adl)
    gate_7_reference(submission, active_modes, adl)
    gate_8_statistical(submission, active_modes, adl)
    gate_9_robustness(submission, active_modes, adl)
    gate_10_reasoning(submission, active_modes, adl)
    gate_11_policy(submission, active_modes, adl)
    gate_12_provenance(submission, run_ts, adl)
    gate_13_temporal(submission, run_ts, audit_start_ts, adl)
    gate_14_explanation(submission, active_modes, adl)
    gate_15_ranking(submission, adl)
    gate_16_calibration(submission, adl)

    # ── GATE 17: Fault Severity ────────────────────────────────────────────────
    g17 = gate_17_severity(adl)
    max_severity = g17["max_severity"]
    fault_counts = g17["fault_counts"]

    # ── GATE 18: Root Cause Attribution ───────────────────────────────────────
    root_causes = gate_18_root_cause(submission, active_modes, adl)

    # ── GATE 19: Action Classification ────────────────────────────────────────
    # Compute preliminary fault score for accept threshold check
    total_fault_score = len(adl.fault_register) * 0.05  # rough proxy pre-gate-23
    classification = gate_19_classification(
        max_severity, fault_counts, root_causes, total_fault_score, adl)

    # ── GATE 20: Remediation Controls ─────────────────────────────────────────
    gate_20_remediation(classification, adl)

    # ── GATE 21: Evaluation Loop ───────────────────────────────────────────────
    gate_21_evaluation_loop(submission, classification, adl)
    # Classification may have been updated to escalate_systemic_issue in gate 21
    classification = adl.classification

    # ── GATE 22: Output Controls ───────────────────────────────────────────────
    audit_confidence = submission.get("audit_confidence", 0.80)
    gate_22_output(classification, root_causes, audit_confidence, adl)

    # ── GATE 23: Final Composite Fault Score ──────────────────────────────────
    gate_23_composite_score(adl)

    # Store hash to prevent duplicate audits in this session
    if isinstance(submission.get("result_payload"), dict):
        audited_store.add(_result_hash(submission["result_payload"]))

    return adl.to_dict()


# ── DATABASE WRITE ─────────────────────────────────────────────────────────────

def write_audit_log(audit: dict) -> None:
    """
    Write a completed AuditDecisionLog to the database.

    FLAG: A dedicated audit_decisions table is recommended for regulatory export.
    TODO: tracked in AGENT5_SYSTEM_DESCRIPTION.md section 6.
    """
    summary = (
        f"[result_audit_agent] result={audit['result_id']} "
        f"type={audit['result_type']} "
        f"classification={audit.get('classification')} "
        f"final_signal={audit.get('final_audit_signal')} "
        f"fault_score={audit.get('fault_score')} "
        f"faults={len(audit.get('fault_register', []))}"
    )
    _db.log_event(
        agent="result_audit_agent",
        event_type="audit_decision",
        message=summary,
        payload=json.dumps(audit),
    )


def escalate_if_needed(audit: dict) -> None:
    """
    If classification is block_output or escalate_systemic_issue,
    post a high-priority suggestion for human review.
    """
    cls = audit.get("classification")
    if cls in {"block_output", "escalate_systemic_issue"}:
        _db.post_suggestion(
            created_by="result_audit_agent",
            target_files=[],
            description=(
                f"AUDIT ESCALATION: result={audit['result_id']} "
                f"classification={cls} "
                f"fault_score={audit.get('fault_score')} "
                f"signal={audit.get('final_audit_signal')} "
                f"root_causes={audit.get('root_causes')}"
            ),
            risk_level="HIGH" if cls == "block_output" else "MEDIUM",
            impact="audit_escalation",
            metadata=json.dumps(audit),
        )
        log.warning(f"escalation posted: result={audit['result_id']} cls={cls}")


# ── STATUS ─────────────────────────────────────────────────────────────────────

def show_status() -> None:
    """Print a brief status summary from the last run."""
    try:
        rows = _db.get_recent_events(agent="result_audit_agent", limit=10)
        if not rows:
            print("No recent audit runs found.")
            return
        print(f"Last {len(rows)} events for result_audit_agent:")
        for r in rows:
            print(f"  {r}")
    except Exception as exc:
        print(f"Status unavailable: {exc}")


# ── ENTRYPOINT ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Result Audit Agent — runs a result payload through a "
                    "23-gate deterministic fault-detection spine."
    )
    parser.add_argument("--mode", choices=["audit", "status"], default="audit",
                        help="audit: run a single audit pass. status: show last run summary.")
    parser.add_argument("--payload", type=str, default=None,
                        help="JSON string of the full audit submission dict.")
    parser.add_argument("--file", type=str, default=None,
                        help="Path to a JSON file containing the audit submission dict.")
    args = parser.parse_args()

    if args.mode == "status":
        show_status()
        return

    # Load submission
    submission = None
    if args.file:
        with open(args.file, "r") as fh:
            submission = json.load(fh)
    elif args.payload:
        submission = json.loads(args.payload)
    else:
        print("ERROR: --payload or --file required for audit mode.")
        sys.exit(1)

    audit = audit_result(submission)
    write_audit_log(audit)
    escalate_if_needed(audit)

    # Print human-readable summary
    adl = AuditDecisionLog.__new__(AuditDecisionLog)
    adl.__dict__.update(audit)
    adl.gates = audit.get("gates", {})
    print(f"\nAudit result: {audit.get('classification')} | "
          f"signal: {audit.get('final_audit_signal')} | "
          f"fault_score: {audit.get('fault_score')} | "
          f"faults: {len(audit.get('fault_register', []))}")
    if audit.get("halt_reason"):
        print(f"Halted: {audit['halt_reason']}")


# ── V1 ENTRY POINT ────────────────────────────────────────────────────────────

def run_agent(snapshot: dict) -> dict:
    """
    V1 standard entry point for the Fault Detection Agent (7.9).

    snapshot keys:
        trade_output  (required) — Trade Logic Agent output dict
        run_id        (required) — unique run identifier

    Returns:
        classification  — block_output | fail | pass_with_warnings | pass
        fault_list      — list of fault code strings
        decision_log    — list of gate trace records
    """
    trade_output = snapshot.get("trade_output")
    run_id       = snapshot.get("run_id", "unknown")

    submission = {
        "result_id":           run_id,
        "result_type":         "trade_output",
        "result_payload":      trade_output if trade_output is not None else {},
        "evaluation_rule_set": {"mode": "trade_output_audit"},
    }
    if trade_output is None:
        submission["result_payload"] = None

    result = audit_result(submission)

    # Map internal classification values to V1 output states
    raw_cls = result.get("classification", "block_output")
    cls_map = {
        "block_output":            "block_output",
        "escalate_systemic_issue": "block_output",
        "fail":                    "fail",
        "manual_investigation":    "fail",
        "review_recommended":      "pass_with_warnings",
        "pass_with_warnings":      "pass_with_warnings",
        "pass":                    "pass",
    }

    fault_list = [f.get("fault_state", "unknown")
                  for f in result.get("fault_register", [])]

    # Convert gates dict to decision_log list for V1 contract
    gates = result.get("gates", {})
    decision_log = [
        {
            "gate":        gnum,
            "name":        gdata.get("name", ""),
            "inputs":      gdata.get("inputs", {}),
            "result":      gdata.get("result", ""),
            "reason_code": gdata.get("reason", ""),
            "ts":          gdata.get("ts", ""),
        }
        for gnum, gdata in sorted(gates.items())
    ]

    return {
        "classification": cls_map.get(raw_cls, "fail"),
        "fault_list":     fault_list,
        "decision_log":   decision_log,
    }


if __name__ == "__main__":
    main()
