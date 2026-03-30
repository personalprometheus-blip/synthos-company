"""
bias_audit_agent.py — Bias Audit Agent
Synthos Company Pi | /home/<user>/synthos-company/agents/bias_audit_agent.py

Role:
  Accept any structured result payload along with group attribute data and run
  it through a 20-gate deterministic bias-detection spine. Produce a
  classification, remediation directives, and a composite bias score.

  This agent is accountable for:
    - Detecting bias across representation, selection, labeling, measurement,
      outcomes, language, proxy features, and counterfactual fairness
    - Assigning severity and root cause to every bias fault detected
    - Classifying the overall result and recommending remediation
    - Writing a complete BiasAuditDecisionLog for every result audited

  This agent does NOT modify the result payload it audits.
  This agent does NOT apply remediation actions automatically.
  Exception: classification = block_output suppresses downstream release.

  This agent does NOT make trade decisions or generate market signals.
  All output routes through db_helpers.

  Human-readable logic specification:
    documentation/governance/AGENT6_SYSTEM_DESCRIPTION.md

Bias audit modes:
  representation  — group distribution vs. reference population
  selection       — sampling and filtering distortion by group
  label           — annotation consistency and ground truth validity
  measurement     — feature quality and proxy validity by group
  outcome         — decision rate and error rate parity across groups
  language        — loaded terms, tone, framing, and agency attribution

  Default: all six modes active if none are specified.

USAGE:
  python3 bias_audit_agent.py --mode audit --payload '{"result_payload": ...}'
  python3 bias_audit_agent.py --mode audit --file submission.json
  python3 bias_audit_agent.py --mode status
"""

import os
import sys
import json
import re
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

LOG_FILE = LOGS_DIR / "bias_audit_agent.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s bias_audit_agent: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("bias_audit_agent")

AGENT_VERSION = "1.0.0"

# ── CONSTANTS — GATE 3: GROUP MAPPING ─────────────────────────────────────────

GROUP_MAPPING_THRESHOLD       = 0.50    # confidence minimum for group mapping
PROXY_THRESHOLD               = 0.65    # feature proxy score → proxy_risk

# ── CONSTANTS — GATE 4: REPRESENTATION BIAS ───────────────────────────────────

UNDERREPRESENTATION_THRESHOLD = 0.80    # group_share < threshold * ref_share → under
OVERREPRESENTATION_THRESHOLD  = 1.25    # group_share > threshold * ref_share → over
DRIFT_MARGIN                  = 0.05    # representation gap growth → worsening skew
REFERENCE_QUALITY_THRESHOLD   = 0.60    # reference population quality minimum

# ── CONSTANTS — GATE 5: SELECTION BIAS ────────────────────────────────────────

SELECTION_BIAS_THRESHOLD      = 0.20    # distribution distance → selection_bias
MISSINGNESS_GAP_THRESHOLD     = 0.10    # missing rate gap → differential_missingness
SELF_SELECTION_THRESHOLD      = 0.15    # self-selection effect size
DROPOFF_GAP_THRESHOLD         = 0.10    # filter stage drop-off gap

# ── CONSTANTS — GATE 6: LABEL / ANNOTATION BIAS ───────────────────────────────

LABEL_BIAS_THRESHOLD          = 0.15    # label disagreement → inconsistent_labeling
SEVERITY_GAP_THRESHOLD        = 0.10    # harsh label rate gap → severity_label_bias
ANNOTATION_CONFIDENCE_THRESHOLD = 0.15  # annotation confidence gap
AMBIGUITY_THRESHOLD           = 0.40    # guideline ambiguity score

# ── CONSTANTS — GATE 7: MEASUREMENT BIAS ──────────────────────────────────────

MEASUREMENT_GAP_THRESHOLD     = 0.10    # measurement error gap
PROXY_VALIDITY_THRESHOLD      = 0.60    # proxy feature validity minimum
CAPTURE_GAP_THRESHOLD         = 0.10    # capture quality gap
NORMALIZATION_BIAS_THRESHOLD  = 0.15    # normalization distortion
IMPUTATION_GAP_THRESHOLD      = 0.10    # imputation error gap

# ── CONSTANTS — GATE 8: OUTCOME / DECISION BIAS ───────────────────────────────

APPROVAL_GAP_THRESHOLD        = 0.10    # approval rate gap → outcome_rate_disparity
FPR_GAP_THRESHOLD             = 0.05    # false positive rate gap
FNR_GAP_THRESHOLD             = 0.05    # false negative rate gap
CALIBRATION_GAP_THRESHOLD     = 0.10    # calibration error gap
THRESHOLD_EFFECT_THRESHOLD    = 0.10    # threshold effect gap
EXPOSURE_GAP_THRESHOLD        = 0.10    # ranking exposure share gap

# ── CONSTANTS — GATE 9: LANGUAGE BIAS ─────────────────────────────────────────

LOADED_LANGUAGE_THRESHOLD     = 0.05    # loaded term density
STEREOTYPE_THRESHOLD          = 0.40    # stereotype score
TONE_GAP_THRESHOLD            = 0.20    # tone gap between groups
AGENCY_GAP_THRESHOLD          = 0.15    # agency term gap
DEMEANING_THRESHOLD           = 0.20    # demeaning language score

# ── CONSTANTS — GATE 10: COUNTERFACTUAL FAIRNESS ──────────────────────────────

COUNTERFACTUAL_TOLERANCE      = 0.05    # score shift under group swap
EXPLANATION_SHIFT_THRESHOLD   = 0.30    # explanation distance under group swap

# ── CONSTANTS — GATE 11: PROXY / LEAKAGE ──────────────────────────────────────

NAME_BIAS_THRESHOLD           = 0.10    # name signal importance
STYLE_PROXY_THRESHOLD         = 0.50    # linguistic style proxy score
LEAKAGE_THRESHOLD             = 0.20    # mutual information with protected attribute

# ── CONSTANTS — GATE 12: CONTEXT / JUSTIFICATION ──────────────────────────────

JUSTIFICATION_SPECIFICITY_THRESHOLD = 0.40  # specificity score minimum
JUSTIFICATION_VALIDITY_THRESHOLD    = 0.70  # legitimate constraint validity minimum

# ── CONSTANTS — GATE 13: TEMPORAL BIAS ────────────────────────────────────────

FAIRNESS_DRIFT_THRESHOLD      = 0.15    # distribution distance → fairness_drift
RESIDUAL_GAP_THRESHOLD        = 0.05    # gap after remediation → ineffective
DEPLOYMENT_GAP_THRESHOLD      = 0.05    # post vs pre-deploy gap increase

# ── CONSTANTS — GATE 14: SEVERITY ─────────────────────────────────────────────

HARM_THRESHOLD                = 0.50    # harm risk → critical
ESCALATION_COUNT              = 3       # co-occurring medium faults → escalated_high
SYSTEMIC_THRESHOLD            = 5       # repeat bias pattern count → systemic

# ── CONSTANTS — GATE 15: ROOT CAUSE ───────────────────────────────────────────

ROOT_CAUSE_CONFIDENCE_FLOOR   = 0.50    # max root cause probability minimum

# ── CONSTANTS — GATE 16: ACTION CLASSIFICATION ────────────────────────────────

ACCEPT_THRESHOLD              = 0.10    # total bias score → pass if below

# ── CONSTANTS — GATE 18: EVALUATION LOOP ──────────────────────────────────────

MISSED_BIAS_THRESHOLD         = 0.05
FALSE_POSITIVE_THRESHOLD      = 0.10

# ── CONSTANTS — GATE 20: COMPOSITE BIAS SCORE ─────────────────────────────────

W1_REPRESENTATION             = 0.20
W2_SELECTION                  = 0.20
W3_LABEL                      = 0.15
W4_MEASUREMENT                = 0.15
W5_OUTCOME                    = 0.15
W6_LANGUAGE                   = 0.10
W7_PROXY                      = 0.05
WARNING_THRESHOLD             = 0.20
FAIL_THRESHOLD                = 0.50

# ── BIAS AUDIT MODES ───────────────────────────────────────────────────────────

ALL_MODES = {"representation", "selection", "label",
             "measurement", "outcome", "language"}

# ── BIAS FAULT SEVERITY MAP ────────────────────────────────────────────────────
# Maps bias_fault → (impact_scope, default_severity)
# impact_scope: "language_only", "ranking_or_exposure", "decision_output"

FAULT_SEVERITY = {
    # Low — language only
    "unnecessary_attribute_reference": ("language_only",        "low"),
    "weak_justification":              ("language_only",        "low"),
    "label_guideline_risk":            ("language_only",        "low"),
    # Medium — ranking or exposure
    "loaded_language":                 ("ranking_or_exposure",  "medium"),
    "stereotyped_framing":             ("ranking_or_exposure",  "medium"),
    "tone_disparity":                  ("ranking_or_exposure",  "medium"),
    "agency_bias":                     ("ranking_or_exposure",  "medium"),
    "ranking_exposure_bias":           ("ranking_or_exposure",  "medium"),
    "annotation_uncertainty_bias":     ("ranking_or_exposure",  "medium"),
    "counterfactual_explanation_bias": ("ranking_or_exposure",  "medium"),
    "weak_proxy_measure":              ("ranking_or_exposure",  "medium"),
    "normalization_bias":              ("ranking_or_exposure",  "medium"),
    "self_selection_bias":             ("ranking_or_exposure",  "medium"),
    "worsening_representation_skew":   ("ranking_or_exposure",  "medium"),
    "fairness_drift":                  ("ranking_or_exposure",  "medium"),
    "circular_historical_bias":        ("ranking_or_exposure",  "medium"),
    "unjustified_disparity":           ("ranking_or_exposure",  "medium"),
    # High — decision output
    "underrepresentation":             ("decision_output",      "high"),
    "overrepresentation":              ("decision_output",      "high"),
    "omitted_subgroup":                ("decision_output",      "high"),
    "weak_reference_baseline":         ("decision_output",      "high"),
    "selection_bias":                  ("decision_output",      "high"),
    "access_bias":                     ("decision_output",      "high"),
    "differential_missingness":        ("decision_output",      "high"),
    "filter_stage_bias":               ("decision_output",      "high"),
    "inconsistent_labeling":           ("decision_output",      "high"),
    "severity_label_bias":             ("decision_output",      "high"),
    "differential_measurement_error":  ("decision_output",      "high"),
    "collection_bias":                 ("decision_output",      "high"),
    "imputation_bias":                 ("decision_output",      "high"),
    "outcome_rate_disparity":          ("decision_output",      "high"),
    "false_positive_disparity":        ("decision_output",      "high"),
    "false_negative_disparity":        ("decision_output",      "high"),
    "group_calibration_gap":           ("decision_output",      "high"),
    "threshold_bias":                  ("decision_output",      "high"),
    "counterfactual_instability":      ("decision_output",      "high"),
    "counterfactual_score_bias":       ("decision_output",      "high"),
    "sensitive_proxy_risk":            ("decision_output",      "high"),
    "name_proxy_bias":                 ("decision_output",      "high"),
    "linguistic_proxy_bias":           ("decision_output",      "high"),
    "worsening_bias_trend":            ("decision_output",      "high"),
    "ineffective_bias_remediation":    ("decision_output",      "high"),
    "deployment_induced_bias":         ("decision_output",      "high"),
    "prohibited_justification":        ("decision_output",      "high"),
    # Critical — harm risk
    "biased_ground_truth":             ("decision_output",      "critical"),
    "protected_attribute_leakage":     ("decision_output",      "critical"),
    "direct_attribute_violation":      ("decision_output",      "critical"),
    "exclusionary_language":           ("decision_output",      "critical"),
}

# ── LANGUAGE TERM SETS ─────────────────────────────────────────────────────────
# All language bias detection uses keyword matching — no ML/AI inference.

LOADED_TERMS = {
    "radical", "extremist", "thug", "criminal", "illegal", "alien",
    "invasion", "swarm", "infestation", "predatory", "aggressive",
    "dangerous", "threatening", "suspicious", "deviant",
}

STEREOTYPE_TERMS = {
    "always", "never", "all of them", "they all", "these people",
    "you know how they are", "typical", "of course they",
    "as expected from", "just like all",
}

DEMEANING_TERMS = {
    "inferior", "primitive", "savage", "uncivilised", "uncivilized",
    "backward", "subhuman", "lesser", "weak", "incapable",
    "uneducated", "ignorant", "filthy",
}

AGENCY_POSITIVE_TERMS = {
    "decided", "chose", "led", "created", "built", "achieved",
    "earned", "won", "succeeded", "initiated", "developed",
}

AGENCY_PASSIVE_TERMS = {
    "was given", "was allowed", "was helped", "received",
    "was selected", "was chosen", "was placed", "was assigned",
}

PROHIBITED_JUSTIFICATION_TERMS = {
    "because of their race", "due to their gender", "because of their religion",
    "due to their nationality", "because of their age",
    "because of their disability", "due to their sexual orientation",
}

# ── V1 SCHEMA DEFINITIONS ─────────────────────────────────────────────────────

INPUT_SCHEMA = {
    "trade_output": {"type": "dict", "required": True,  "description": "Trade Logic Agent output"},
    "run_id":       {"type": "str",  "required": True},
}

OUTPUT_SCHEMA = {
    "classification": {"type": "str",  "values": ["block_output", "fail_bias_audit", "pass_with_bias_warning", "fairness_review_recommended", "pass"]},
    "bias_findings":  {"type": "list", "description": "Identified bias codes"},
    "decision_log":   {"type": "list", "description": "Full gate trace"},
}

# ── BIAS AUDIT DECISION LOG ────────────────────────────────────────────────────

class BiasAuditDecisionLog:
    """
    Accumulates all gate inputs, bias fault detections, severity assignments,
    root causes, and output controls for a single bias audit pass.
    """

    def __init__(self, result_id: str, result_type: str,
                 bias_modes: list, run_ts: datetime):
        self.run_ts        = run_ts.isoformat()
        self.result_id     = result_id
        self.result_type   = result_type
        self.bias_modes    = bias_modes
        self.gates         = {}
        self.fault_register = []
        self.halted_at     = None
        self.halt_reason   = None
        self.classification    = None
        self.final_bias_signal = None
        self.bias_score        = None
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

    def add_fault(self, bias_fault: str, group: str = None,
                  detail: str = None):
        scope, severity = FAULT_SEVERITY.get(bias_fault, ("decision_output", "high"))
        self.fault_register.append({
            "bias_fault":   bias_fault,
            "group":        group,
            "detail":       detail,
            "severity":     severity,
            "impact_scope": scope,
        })

    def halt(self, gate: int, name: str, reason: str):
        self.halted_at   = gate
        self.halt_reason = reason
        self.record(gate, name, {}, "HALT", reason)

    def to_dict(self) -> dict:
        return {
            "run_ts":             self.run_ts,
            "result_id":          self.result_id,
            "result_type":        self.result_type,
            "bias_modes":         self.bias_modes,
            "halted_at":          self.halted_at,
            "halt_reason":        self.halt_reason,
            "fault_register":     self.fault_register,
            "classification":     self.classification,
            "final_bias_signal":  self.final_bias_signal,
            "bias_score":         self.bias_score,
            "output_action":      self.output_action,
            "root_causes":        self.root_causes,
            "remediation":        self.remediation,
            "gates":              self.gates,
        }

    def to_human_readable(self) -> str:
        lines = [
            f"=== BiasAuditDecisionLog ===",
            f"Run:         {self.run_ts}",
            f"Result ID:   {self.result_id}",
            f"Result Type: {self.result_type}",
            f"Bias Modes:  {self.bias_modes}",
            f"",
        ]
        for gate_num in sorted(self.gates.keys()):
            g = self.gates[gate_num]
            lines.append(f"  Gate {gate_num:>2} — {g['name']}")
            for k, v in g["inputs"].items():
                lines.append(f"             {k}: {v}")
            lines.append(f"           → result: {g['result']}  ({g['reason']})")
            for f in g.get("faults", []):
                lines.append(f"           ⚠ BIAS: {f}")
        lines += [
            f"",
            f"Bias fault register ({len(self.fault_register)} faults):",
        ]
        for f in self.fault_register:
            lines.append(f"  [{f['severity'].upper():8}] {f['bias_fault']}"
                         f"  group={f['group']}  {f['detail']}")
        lines += [
            f"",
            f"Root causes:     {self.root_causes}",
            f"Classification:  {self.classification}",
            f"Bias score:      {self.bias_score}",
            f"Final signal:    {self.final_bias_signal}",
            f"Output action:   {self.output_action}",
            f"Remediation:     {self.remediation}",
        ]
        if self.halted_at:
            lines.append(f"HALTED at gate {self.halted_at}: {self.halt_reason}")
        return "\n".join(lines)


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _is_mode_active(modes: list, mode: str) -> bool:
    return mode in modes or len(modes) == 0

def _tokenize(text: str) -> set:
    return set(re.findall(r"\b[a-z]{2,}\b", text.lower()))

def _keyword_density(tokens: set, term_set: set) -> float:
    if not tokens:
        return 0.0
    return len(tokens & term_set) / len(tokens)

def _rate_gap(rate_a: float, rate_b: float) -> float:
    return abs(rate_a - rate_b)


# ── GATE IMPLEMENTATIONS ───────────────────────────────────────────────────────

def gate_1_system(submission: dict, badl: BiasAuditDecisionLog) -> bool:
    """
    Gate 1 — System Gate
    Rejects the submission if the input is unworkable.
    Returns True (PROCEED) or False (HALT).
    """
    result_payload      = submission.get("result_payload")
    subject_attributes  = submission.get("subject_attributes")
    attribute_required  = submission.get("attribute_context_required", False)
    comparison_payload  = submission.get("comparison_group_payload")
    parity_required     = submission.get("parity_test_required", False)
    bias_rule_set       = submission.get("bias_rule_set")
    source_provenance   = submission.get("source_provenance")

    # IF output missing
    if result_payload is None:
        badl.halt(1, "System Gate", "reject_result: result_payload is null")
        return False

    # IF subject/entity information missing where required
    if attribute_required and subject_attributes is None:
        badl.halt(1, "System Gate",
                  "incomplete_bias_audit: subject_attributes required but null")
        return False

    # IF comparison group data missing
    if parity_required and comparison_payload is None:
        badl.halt(1, "System Gate",
                  "insufficient_comparison_data: comparison_group_payload required but null")
        return False

    # IF parse failure
    if not isinstance(result_payload, (dict, list, str)):
        badl.halt(1, "System Gate", "reject_result: result_payload cannot be parsed")
        return False

    # IF fairness rules unavailable
    if bias_rule_set is None:
        badl.halt(1, "System Gate", "halt_bias_audit: bias_rule_set is null")
        return False

    # Non-halting: IF provenance missing
    if source_provenance is None:
        badl.add_fault("low_traceability", group=None,
                       detail="source_provenance is null; traceability degraded")

    badl.record(1, "System Gate",
                {"result_payload_present": True,
                 "attribute_context_required": attribute_required,
                 "attributes_present": subject_attributes is not None,
                 "parity_required": parity_required,
                 "comparison_present": comparison_payload is not None,
                 "has_provenance": source_provenance is not None},
                "PASS", "system checks passed")
    return True


def gate_2_audit_mode(submission: dict, badl: BiasAuditDecisionLog) -> list:
    """
    Gate 2 — Bias Audit Mode Controls
    Establishes which bias audit modes are active for this pass.
    """
    requested = submission.get("bias_modes", [])
    if not requested:
        active_modes = list(ALL_MODES)
        bias_state   = "multi_pass_bias_audit"
    elif len(requested) == 1:
        active_modes = list(requested)
        mode_state_map = {
            "representation": "representation_check",
            "selection":      "selection_check",
            "label":          "label_check",
            "measurement":    "measurement_check",
            "outcome":        "outcome_check",
            "language":       "language_check",
        }
        bias_state = mode_state_map.get(requested[0], "unknown_mode")
    else:
        active_modes = list(requested)
        bias_state   = "multi_pass_bias_audit"

    badl.record(2, "Bias Audit Mode",
                {"requested_modes": requested,
                 "active_modes": active_modes},
                bias_state, f"active_modes: {active_modes}")
    return active_modes


def gate_3_group_mapping(submission: dict, badl: BiasAuditDecisionLog) -> dict:
    """
    Gate 3 — Group Mapping Controls
    Identifies groups, detects proxy variables, and flags direct attribute use.
    """
    subject_attributes  = submission.get("subject_attributes", {})
    comparison_groups   = submission.get("comparison_groups", [])
    mapping_confidence  = submission.get("group_mapping_confidence", 1.0)
    proxy_scores        = submission.get("feature_proxy_scores", {})   # {feature: score}
    protected_used      = submission.get("protected_attribute_used", False)
    policy_disallows    = submission.get("policy_disallows_direct_use", False)
    faults              = []

    group_states = []

    # IF protected or monitored group identified
    if subject_attributes:
        group_states.append("mapped_group")

    # IF multiple comparison groups identified
    if len(comparison_groups) > 1:
        group_states.append("multi_group")

    # IF no group mapping possible
    if mapping_confidence < GROUP_MAPPING_THRESHOLD:
        group_states.append("unmapped")

    # IF proxy variable detected
    proxy_risks = []
    for feature, score in proxy_scores.items():
        if score > PROXY_THRESHOLD:
            group_states.append("proxy_risk")
            proxy_risks.append(feature)

    # IF direct attribute used where prohibited
    if protected_used and policy_disallows:
        badl.add_fault("direct_attribute_violation", group=None,
                       detail="protected attribute used directly; policy disallows this")
        faults.append("direct_attribute_violation")

    if not group_states:
        group_states.append("unmapped")

    badl.record(3, "Group Mapping",
                {"mapping_confidence": mapping_confidence,
                 "comparison_group_count": len(comparison_groups),
                 "proxy_features_flagged": proxy_risks,
                 "protected_attribute_used": protected_used},
                str(group_states), f"group_states: {group_states}", faults)
    return {"group_states": group_states, "proxy_risks": proxy_risks,
            "comparison_groups": comparison_groups}


def gate_4_representation(submission: dict, active_modes: list,
                           badl: BiasAuditDecisionLog) -> None:
    """
    Gate 4 — Representation Bias Controls
    Detects group proportionality failures vs. reference population.
    """
    if not _is_mode_active(active_modes, "representation"):
        badl.record(4, "Representation Bias", {}, "SKIP", "representation mode not active")
        return

    group_shares     = submission.get("group_shares", {})        # {group: share_in_sample}
    ref_shares       = submission.get("reference_shares", {})    # {group: share_in_population}
    ref_quality      = submission.get("reference_population_quality", 1.0)
    prior_gaps       = submission.get("prior_representation_gaps", {})  # {group: prior_gap}
    expected_subgroups = submission.get("expected_subgroups", [])
    faults           = []

    # IF reference population invalid
    if ref_quality < REFERENCE_QUALITY_THRESHOLD:
        badl.add_fault("weak_reference_baseline", group=None,
                       detail=f"reference_population_quality={ref_quality:.3f} < {REFERENCE_QUALITY_THRESHOLD}")
        faults.append("weak_reference_baseline")

    for group, sample_share in group_shares.items():
        ref_share = ref_shares.get(group)
        if ref_share is None:
            continue

        # IF group underrepresented
        if sample_share < UNDERREPRESENTATION_THRESHOLD * ref_share:
            badl.add_fault("underrepresentation", group=group,
                           detail=f"sample_share={sample_share:.3f}, "
                                  f"ref_share={ref_share:.3f}, "
                                  f"threshold_floor={UNDERREPRESENTATION_THRESHOLD * ref_share:.3f}")
            faults.append(f"underrepresentation: {group}")

        # IF group overrepresented
        elif sample_share > OVERREPRESENTATION_THRESHOLD * ref_share:
            badl.add_fault("overrepresentation", group=group,
                           detail=f"sample_share={sample_share:.3f}, "
                                  f"ref_share={ref_share:.3f}, "
                                  f"threshold_ceil={OVERREPRESENTATION_THRESHOLD * ref_share:.3f}")
            faults.append(f"overrepresentation: {group}")

        # IF representation skew worsening
        if group in prior_gaps:
            current_gap = abs(sample_share - ref_share)
            prior_gap   = prior_gaps[group]
            if current_gap > prior_gap + DRIFT_MARGIN:
                badl.add_fault("worsening_representation_skew", group=group,
                               detail=f"current_gap={current_gap:.3f}, prior_gap={prior_gap:.3f}")
                faults.append(f"worsening_representation_skew: {group}")

    # IF subgroup omitted entirely
    all_groups_in_sample = set(group_shares.keys())
    for subgroup in expected_subgroups:
        if subgroup not in all_groups_in_sample or group_shares.get(subgroup, 0) == 0:
            badl.add_fault("omitted_subgroup", group=subgroup,
                           detail=f"subgroup '{subgroup}' expected but absent from sample")
            faults.append(f"omitted_subgroup: {subgroup}")

    badl.record(4, "Representation Bias",
                {"groups_checked": len(group_shares),
                 "reference_quality": ref_quality,
                 "expected_subgroups": len(expected_subgroups)},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} representation faults", faults)


def gate_5_selection(submission: dict, active_modes: list,
                      badl: BiasAuditDecisionLog) -> None:
    """
    Gate 5 — Selection Bias Controls
    Detects distortions in how individuals were selected into the sample.
    """
    if not _is_mode_active(active_modes, "selection"):
        badl.record(5, "Selection Bias", {}, "SKIP", "selection mode not active")
        return

    selection_distance   = submission.get("selection_distribution_distance", 0.0)
    selection_probs      = submission.get("selection_probabilities", {})   # {group: prob}
    missing_rates        = submission.get("missing_rates", {})             # {group: rate}
    self_selection_effect = submission.get("self_selection_effect_size", 0.0)
    dropoff_rates        = submission.get("filter_dropoff_rates", {})      # {group: rate}
    faults               = []

    # IF included sample differs materially from eligible population
    if selection_distance > SELECTION_BIAS_THRESHOLD:
        badl.add_fault("selection_bias", group=None,
                       detail=f"selection_distribution_distance={selection_distance:.3f} > {SELECTION_BIAS_THRESHOLD}")
        faults.append("selection_bias")

    # IF access barrier pattern detected
    groups = list(selection_probs.keys())
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            g1, g2 = groups[i], groups[j]
            p1, p2 = selection_probs[g1], selection_probs[g2]
            if p1 > 0 and p2 > 0 and (p1 / p2 > 3.0 or p2 / p1 > 3.0):
                low_group = g1 if p1 < p2 else g2
                badl.add_fault("access_bias", group=low_group,
                               detail=f"selection_prob({g1})={p1:.3f}, selection_prob({g2})={p2:.3f}")
                faults.append(f"access_bias: {low_group}")

    # IF missingness is group-skewed
    missing_group_list = list(missing_rates.keys())
    for i in range(len(missing_group_list)):
        for j in range(i + 1, len(missing_group_list)):
            g1, g2 = missing_group_list[i], missing_group_list[j]
            gap = _rate_gap(missing_rates[g1], missing_rates[g2])
            if gap > MISSINGNESS_GAP_THRESHOLD:
                badl.add_fault("differential_missingness", group=f"{g1} vs {g2}",
                               detail=f"missing_rate({g1})={missing_rates[g1]:.3f}, "
                                      f"missing_rate({g2})={missing_rates[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"differential_missingness: {g1} vs {g2}")

    # IF opt-in mechanism distorts sample
    if self_selection_effect > SELF_SELECTION_THRESHOLD:
        badl.add_fault("self_selection_bias", group=None,
                       detail=f"self_selection_effect={self_selection_effect:.3f} > {SELF_SELECTION_THRESHOLD}")
        faults.append("self_selection_bias")

    # IF filtering stage disproportionately excludes group
    dropoff_groups = list(dropoff_rates.keys())
    for i in range(len(dropoff_groups)):
        for j in range(i + 1, len(dropoff_groups)):
            g1, g2 = dropoff_groups[i], dropoff_groups[j]
            gap = _rate_gap(dropoff_rates[g1], dropoff_rates[g2])
            if gap > DROPOFF_GAP_THRESHOLD:
                high_drop = g1 if dropoff_rates[g1] > dropoff_rates[g2] else g2
                badl.add_fault("filter_stage_bias", group=high_drop,
                               detail=f"dropoff_rate({g1})={dropoff_rates[g1]:.3f}, "
                                      f"dropoff_rate({g2})={dropoff_rates[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"filter_stage_bias: {high_drop}")

    badl.record(5, "Selection Bias",
                {"selection_distance": selection_distance,
                 "self_selection_effect": self_selection_effect,
                 "groups_checked": len(selection_probs)},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} selection faults", faults)


def gate_6_label_bias(submission: dict, active_modes: list,
                       badl: BiasAuditDecisionLog) -> None:
    """
    Gate 6 — Label / Annotation Bias Controls
    Detects inconsistency and systematic skew in labels across groups.
    """
    if not _is_mode_active(active_modes, "label"):
        badl.record(6, "Label / Annotation Bias", {}, "SKIP", "label mode not active")
        return

    label_disagreement   = submission.get("label_disagreement_across_groups", 0.0)
    severity_label_rates = submission.get("severity_label_rates", {})  # {group: rate}
    annotation_conf      = submission.get("annotation_confidence_by_group", {})  # {group: score}
    ground_truth_biased  = submission.get("ground_truth_source_historically_biased", False)
    guideline_ambiguity  = submission.get("label_guideline_ambiguity_score", 0.0)
    faults               = []

    # IF labels differ across similar cases by group
    if label_disagreement > LABEL_BIAS_THRESHOLD:
        badl.add_fault("inconsistent_labeling", group=None,
                       detail=f"label_disagreement={label_disagreement:.3f} > {LABEL_BIAS_THRESHOLD}")
        faults.append("inconsistent_labeling")

    # IF harsher labels assigned to one group
    groups = list(severity_label_rates.keys())
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            g1, g2 = groups[i], groups[j]
            gap = _rate_gap(severity_label_rates[g1], severity_label_rates[g2])
            if gap > SEVERITY_GAP_THRESHOLD:
                harsher = g1 if severity_label_rates[g1] > severity_label_rates[g2] else g2
                badl.add_fault("severity_label_bias", group=harsher,
                               detail=f"severity_rate({g1})={severity_label_rates[g1]:.3f}, "
                                      f"severity_rate({g2})={severity_label_rates[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"severity_label_bias: {harsher}")

    # IF annotation confidence differs materially by group
    conf_values = list(annotation_conf.values())
    if len(conf_values) >= 2:
        conf_gap = max(conf_values) - min(conf_values)
        if conf_gap > ANNOTATION_CONFIDENCE_THRESHOLD:
            low_conf_group = min(annotation_conf, key=annotation_conf.get)
            badl.add_fault("annotation_uncertainty_bias", group=low_conf_group,
                           detail=f"annotation_confidence_gap={conf_gap:.3f} > {ANNOTATION_CONFIDENCE_THRESHOLD}")
            faults.append(f"annotation_uncertainty_bias: gap={conf_gap:.3f}")

    # IF ground truth reflects historical bias
    if ground_truth_biased:
        badl.add_fault("biased_ground_truth", group=None,
                       detail="ground_truth_source flagged as historically biased")
        faults.append("biased_ground_truth")

    # IF label guideline ambiguity high
    if guideline_ambiguity > AMBIGUITY_THRESHOLD:
        badl.add_fault("label_guideline_risk", group=None,
                       detail=f"guideline_ambiguity={guideline_ambiguity:.3f} > {AMBIGUITY_THRESHOLD}")
        faults.append("label_guideline_risk")

    badl.record(6, "Label / Annotation Bias",
                {"label_disagreement": label_disagreement,
                 "guideline_ambiguity": guideline_ambiguity,
                 "ground_truth_biased": ground_truth_biased},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} label faults", faults)


def gate_7_measurement(submission: dict, active_modes: list,
                        badl: BiasAuditDecisionLog) -> None:
    """
    Gate 7 — Measurement Bias Controls
    Detects differential data quality and invalid proxy measures by group.
    """
    if not _is_mode_active(active_modes, "measurement"):
        badl.record(7, "Measurement Bias", {}, "SKIP", "measurement mode not active")
        return

    measurement_errors  = submission.get("measurement_errors_by_group", {})  # {group: error}
    proxy_validity      = submission.get("proxy_feature_validity", 1.0)
    proxy_used          = submission.get("proxy_feature_used", False)
    capture_quality     = submission.get("capture_quality_by_group", {})  # {group: quality}
    normalization_distortion = submission.get("normalization_distortion", 0.0)
    imputation_errors   = submission.get("imputation_errors_by_group", {})  # {group: error}
    faults              = []

    # IF feature quality differs by group
    groups = list(measurement_errors.keys())
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            g1, g2 = groups[i], groups[j]
            gap = _rate_gap(measurement_errors[g1], measurement_errors[g2])
            if gap > MEASUREMENT_GAP_THRESHOLD:
                worse = g1 if measurement_errors[g1] > measurement_errors[g2] else g2
                badl.add_fault("differential_measurement_error", group=worse,
                               detail=f"error({g1})={measurement_errors[g1]:.3f}, "
                                      f"error({g2})={measurement_errors[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"differential_measurement_error: {worse}")

    # IF proxy measurement used for latent trait
    if proxy_used and proxy_validity < PROXY_VALIDITY_THRESHOLD:
        badl.add_fault("weak_proxy_measure", group=None,
                       detail=f"proxy_validity={proxy_validity:.3f} < {PROXY_VALIDITY_THRESHOLD}")
        faults.append(f"weak_proxy_measure: validity={proxy_validity:.3f}")

    # IF sensor / collection system performs worse for one group
    cap_groups = list(capture_quality.keys())
    for i in range(len(cap_groups)):
        for j in range(i + 1, len(cap_groups)):
            g1, g2 = cap_groups[i], cap_groups[j]
            gap = _rate_gap(capture_quality[g1], capture_quality[g2])
            if gap > CAPTURE_GAP_THRESHOLD:
                worse = g1 if capture_quality[g1] < capture_quality[g2] else g2
                badl.add_fault("collection_bias", group=worse,
                               detail=f"capture_quality({g1})={capture_quality[g1]:.3f}, "
                                      f"capture_quality({g2})={capture_quality[g2]:.3f}")
                faults.append(f"collection_bias: {worse}")

    # IF normalization standard inappropriate across groups
    if normalization_distortion > NORMALIZATION_BIAS_THRESHOLD:
        badl.add_fault("normalization_bias", group=None,
                       detail=f"normalization_distortion={normalization_distortion:.3f} > {NORMALIZATION_BIAS_THRESHOLD}")
        faults.append("normalization_bias")

    # IF missing values imputed unevenly across groups
    imp_groups = list(imputation_errors.keys())
    for i in range(len(imp_groups)):
        for j in range(i + 1, len(imp_groups)):
            g1, g2 = imp_groups[i], imp_groups[j]
            gap = _rate_gap(imputation_errors[g1], imputation_errors[g2])
            if gap > IMPUTATION_GAP_THRESHOLD:
                worse = g1 if imputation_errors[g1] > imputation_errors[g2] else g2
                badl.add_fault("imputation_bias", group=worse,
                               detail=f"imputation_error_gap={gap:.3f} > {IMPUTATION_GAP_THRESHOLD}")
                faults.append(f"imputation_bias: {worse}")

    badl.record(7, "Measurement Bias",
                {"measurement_error_groups": len(measurement_errors),
                 "proxy_used": proxy_used,
                 "proxy_validity": proxy_validity,
                 "normalization_distortion": normalization_distortion},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} measurement faults", faults)


def gate_8_outcome(submission: dict, active_modes: list,
                    badl: BiasAuditDecisionLog) -> None:
    """
    Gate 8 — Outcome / Decision Bias Controls
    Detects disparities in decision outcomes, error rates, and exposure across groups.
    """
    if not _is_mode_active(active_modes, "outcome"):
        badl.record(8, "Outcome / Decision Bias", {}, "SKIP", "outcome mode not active")
        return

    approval_rates   = submission.get("approval_rates", {})      # {group: rate}
    fpr_by_group     = submission.get("fpr_by_group", {})         # {group: fpr}
    fnr_by_group     = submission.get("fnr_by_group", {})         # {group: fnr}
    calibration_errs = submission.get("calibration_errors_by_group", {})  # {group: err}
    threshold_effects = submission.get("decision_threshold_effects", {}) # {group: effect}
    exposure_shares  = submission.get("ranking_exposure_shares", {})  # {group: share}
    faults           = []

    def _check_gap(metric_dict: dict, threshold: float, fault_name: str) -> None:
        groups = list(metric_dict.keys())
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                g1, g2 = groups[i], groups[j]
                gap = _rate_gap(metric_dict[g1], metric_dict[g2])
                if gap > threshold:
                    badl.add_fault(fault_name, group=f"{g1} vs {g2}",
                                   detail=f"{fault_name}: {g1}={metric_dict[g1]:.3f}, "
                                          f"{g2}={metric_dict[g2]:.3f}, gap={gap:.3f}")
                    faults.append(f"{fault_name}: {g1} vs {g2}")

    # IF approval rate differs materially by group
    _check_gap(approval_rates, APPROVAL_GAP_THRESHOLD, "outcome_rate_disparity")

    # IF false positive rate differs by group
    _check_gap(fpr_by_group, FPR_GAP_THRESHOLD, "false_positive_disparity")

    # IF false negative rate differs by group
    _check_gap(fnr_by_group, FNR_GAP_THRESHOLD, "false_negative_disparity")

    # IF calibration differs by group
    _check_gap(calibration_errs, CALIBRATION_GAP_THRESHOLD, "group_calibration_gap")

    # IF thresholding affects groups unevenly
    _check_gap(threshold_effects, THRESHOLD_EFFECT_THRESHOLD, "threshold_bias")

    # IF ranking exposure differs materially by group
    _check_gap(exposure_shares, EXPOSURE_GAP_THRESHOLD, "ranking_exposure_bias")

    badl.record(8, "Outcome / Decision Bias",
                {"approval_rate_groups": len(approval_rates),
                 "fpr_groups": len(fpr_by_group),
                 "exposure_groups": len(exposure_shares)},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} outcome faults", faults)


def gate_9_language(submission: dict, active_modes: list,
                     badl: BiasAuditDecisionLog) -> None:
    """
    Gate 9 — Language Bias Controls
    Detects loaded language, stereotyped framing, tone disparities, and exclusionary phrasing.
    All detection uses keyword matching — no ML/AI inference.
    """
    if not _is_mode_active(active_modes, "language"):
        badl.record(9, "Language Bias", {}, "SKIP", "language mode not active")
        return

    result = submission.get("result_payload", {})
    text   = result if isinstance(result, str) else json.dumps(result)
    tokens = _tokenize(text)
    total  = len(tokens) or 1
    faults = []

    # Agency terms by group (from structured metadata if available)
    agency_by_group = submission.get("agency_scores_by_group", {})  # {group: score}
    tone_by_group   = submission.get("tone_scores_by_group", {})    # {group: score}
    group_refs      = submission.get("group_references_in_result", {})  # {group: relevant}

    # IF loaded language detected
    loaded_density = _keyword_density(tokens, LOADED_TERMS)
    if loaded_density > LOADED_LANGUAGE_THRESHOLD:
        badl.add_fault("loaded_language", group=None,
                       detail=f"loaded_term_density={loaded_density:.4f} > {LOADED_LANGUAGE_THRESHOLD}")
        faults.append(f"loaded_language: density={loaded_density:.4f}")

    # IF stereotyped framing detected
    stereotype_density = _keyword_density(tokens, STEREOTYPE_TERMS)
    if stereotype_density > STEREOTYPE_THRESHOLD:
        badl.add_fault("stereotyped_framing", group=None,
                       detail=f"stereotype_score={stereotype_density:.4f} > {STEREOTYPE_THRESHOLD}")
        faults.append("stereotyped_framing")

    # IF one group described with harsher tone
    tone_groups = list(tone_by_group.keys())
    for i in range(len(tone_groups)):
        for j in range(i + 1, len(tone_groups)):
            g1, g2 = tone_groups[i], tone_groups[j]
            gap = _rate_gap(tone_by_group[g1], tone_by_group[g2])
            if gap > TONE_GAP_THRESHOLD:
                harsher = g1 if tone_by_group[g1] > tone_by_group[g2] else g2
                badl.add_fault("tone_disparity", group=harsher,
                               detail=f"tone({g1})={tone_by_group[g1]:.3f}, "
                                      f"tone({g2})={tone_by_group[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"tone_disparity: {harsher}")

    # IF agency assigned unevenly
    agency_groups = list(agency_by_group.keys())
    for i in range(len(agency_groups)):
        for j in range(i + 1, len(agency_groups)):
            g1, g2 = agency_groups[i], agency_groups[j]
            gap = _rate_gap(agency_by_group[g1], agency_by_group[g2])
            if gap > AGENCY_GAP_THRESHOLD:
                low_agency = g1 if agency_by_group[g1] < agency_by_group[g2] else g2
                badl.add_fault("agency_bias", group=low_agency,
                               detail=f"agency({g1})={agency_by_group[g1]:.3f}, "
                                      f"agency({g2})={agency_by_group[g2]:.3f}, gap={gap:.3f}")
                faults.append(f"agency_bias: {low_agency}")

    # IF unnecessary group mention present
    for group, relevant in group_refs.items():
        if not relevant:
            badl.add_fault("unnecessary_attribute_reference", group=group,
                           detail=f"group '{group}' referenced but not relevant to task")
            faults.append(f"unnecessary_attribute_reference: {group}")

    # IF demeaning or exclusionary phrasing present
    demeaning_density = _keyword_density(tokens, DEMEANING_TERMS)
    if demeaning_density > DEMEANING_THRESHOLD:
        badl.add_fault("exclusionary_language", group=None,
                       detail=f"demeaning_language_score={demeaning_density:.4f} > {DEMEANING_THRESHOLD}")
        faults.append("exclusionary_language")

    badl.record(9, "Language Bias",
                {"loaded_density": round(loaded_density, 4),
                 "stereotype_density": round(stereotype_density, 4),
                 "demeaning_density": round(demeaning_density, 4),
                 "tone_groups": len(tone_by_group),
                 "agency_groups": len(agency_by_group)},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} language faults", faults)


def gate_10_counterfactual(submission: dict, badl: BiasAuditDecisionLog) -> None:
    """
    Gate 10 — Counterfactual Fairness Controls
    Tests whether swapping group membership changes decision or score.
    Requires pre-computed counterfactual pairs in the submission.
    """
    counterfactuals = submission.get("counterfactual_pairs", [])
    if not counterfactuals:
        badl.record(10, "Counterfactual Fairness", {}, "SKIP",
                    "no counterfactual_pairs provided — TODO: DATA_DEPENDENCY")
        return

    faults = []

    for i, pair in enumerate(counterfactuals):
        decision_a      = pair.get("decision_a")
        decision_b      = pair.get("decision_b")
        score_a         = pair.get("score_a", 0.0)
        score_b         = pair.get("score_b", 0.0)
        explanation_a   = pair.get("explanation_a", "")
        explanation_b   = pair.get("explanation_b", "")
        group_a         = pair.get("group_a", "group_a")
        group_b         = pair.get("group_b", "group_b")
        allowed_features_explain = pair.get("difference_explained_by_allowed_features", False)
        explanation_dist = pair.get("explanation_distance", 0.0)

        # IF justified by allowed features — no fault
        if allowed_features_explain:
            badl.record(10, f"Counterfactual Fairness — pair {i}",
                        {"group_a": group_a, "group_b": group_b},
                        "justified_difference",
                        "difference explained by allowed task-valid features")
            continue

        # IF counterfactual group swap changes decision
        if decision_a is not None and decision_b is not None and decision_a != decision_b:
            badl.add_fault("counterfactual_instability",
                           group=f"{group_a} vs {group_b}",
                           detail=f"decision({group_a})={decision_a}, decision({group_b})={decision_b}")
            faults.append(f"counterfactual_instability: pair {i}")

        # IF counterfactual score shift exceeds tolerance
        score_diff = abs(score_a - score_b)
        if score_diff > COUNTERFACTUAL_TOLERANCE:
            badl.add_fault("counterfactual_score_bias",
                           group=f"{group_a} vs {group_b}",
                           detail=f"score_diff={score_diff:.4f} > {COUNTERFACTUAL_TOLERANCE}")
            faults.append(f"counterfactual_score_bias: pair {i}")

        # IF explanation changes materially under group swap
        if explanation_dist > EXPLANATION_SHIFT_THRESHOLD:
            badl.add_fault("counterfactual_explanation_bias",
                           group=f"{group_a} vs {group_b}",
                           detail=f"explanation_distance={explanation_dist:.3f} > {EXPLANATION_SHIFT_THRESHOLD}")
            faults.append(f"counterfactual_explanation_bias: pair {i}")

    badl.record(10, "Counterfactual Fairness",
                {"pairs_evaluated": len(counterfactuals)},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} counterfactual faults", faults)


def gate_11_proxy_leakage(submission: dict, badl: BiasAuditDecisionLog) -> None:
    """
    Gate 11 — Proxy / Leakage Controls
    Detects features that encode protected attributes or leak protected information.
    """
    proxy_scores        = submission.get("feature_proxy_scores", {})    # {feature: score}
    name_signal_imp     = submission.get("name_signal_importance", 0.0)
    style_proxy_score   = submission.get("style_feature_proxy_score", 0.0)
    mutual_info         = submission.get("mutual_information_protected", 0.0)
    task_necessity      = submission.get("feature_task_necessity", True)
    faults              = []

    # IF ZIP code or similar feature behaves as sensitive proxy
    for feature, score in proxy_scores.items():
        if score > PROXY_THRESHOLD:
            badl.add_fault("sensitive_proxy_risk", group=None,
                           detail=f"feature='{feature}', proxy_score={score:.3f} > {PROXY_THRESHOLD}")
            faults.append(f"sensitive_proxy_risk: {feature}")

    # IF name-based inference affects decision
    if name_signal_imp > NAME_BIAS_THRESHOLD:
        badl.add_fault("name_proxy_bias", group=None,
                       detail=f"name_signal_importance={name_signal_imp:.3f} > {NAME_BIAS_THRESHOLD}")
        faults.append(f"name_proxy_bias: importance={name_signal_imp:.3f}")

    # IF language style acts as demographic proxy
    if style_proxy_score > STYLE_PROXY_THRESHOLD:
        badl.add_fault("linguistic_proxy_bias", group=None,
                       detail=f"style_feature_proxy_score={style_proxy_score:.3f} > {STYLE_PROXY_THRESHOLD}")
        faults.append(f"linguistic_proxy_bias: score={style_proxy_score:.3f}")

    # IF feature leakage from protected attribute detected
    if mutual_info > LEAKAGE_THRESHOLD and not task_necessity:
        badl.add_fault("protected_attribute_leakage", group=None,
                       detail=f"mutual_information={mutual_info:.3f} > {LEAKAGE_THRESHOLD} "
                              f"without task necessity")
        faults.append(f"protected_attribute_leakage: MI={mutual_info:.3f}")

    badl.record(11, "Proxy / Leakage",
                {"proxy_features_checked": len(proxy_scores),
                 "name_signal_importance": name_signal_imp,
                 "style_proxy_score": style_proxy_score,
                 "mutual_information": mutual_info},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} proxy/leakage faults", faults)


def gate_12_justification(submission: dict, badl: BiasAuditDecisionLog) -> None:
    """
    Gate 12 — Context / Justification Controls
    Verifies that disparities have valid, non-circular, non-prohibited justifications.
    """
    outcome_gap_detected    = submission.get("outcome_gap_detected", False)
    justification_text      = submission.get("justification_text") or ""
    justification_basis     = submission.get("justification_basis", "")
    justification_specificity = submission.get("justification_specificity_score", 1.0)
    constraint_validity     = submission.get("documented_constraint_validity_score", 0.0)
    faults                  = []

    # IF documented legitimate constraint exists — no fault
    if constraint_validity >= JUSTIFICATION_VALIDITY_THRESHOLD:
        badl.record(12, "Context / Justification",
                    {"constraint_validity": constraint_validity},
                    "justified_constraint_present",
                    "documented legitimate constraint is valid")
        return

    # IF disparate outcome has no documented justification
    if outcome_gap_detected and not justification_text:
        badl.add_fault("unjustified_disparity", group=None,
                       detail="outcome gap detected but justification_text is absent")
        faults.append("unjustified_disparity")

    if justification_text:
        # IF justification uses prohibited rationale
        just_tokens = _tokenize(justification_text)
        for term in PROHIBITED_JUSTIFICATION_TERMS:
            if term in justification_text.lower():
                badl.add_fault("prohibited_justification", group=None,
                               detail=f"prohibited basis found: '{term}'")
                faults.append("prohibited_justification")
                break

        # IF justification is too vague
        if justification_specificity < JUSTIFICATION_SPECIFICITY_THRESHOLD:
            badl.add_fault("weak_justification", group=None,
                           detail=f"specificity_score={justification_specificity:.3f} < {JUSTIFICATION_SPECIFICITY_THRESHOLD}")
            faults.append(f"weak_justification: specificity={justification_specificity:.3f}")

        # IF justification relies on historical practice alone
        if justification_basis == "legacy_pattern_only":
            badl.add_fault("circular_historical_bias", group=None,
                           detail="justification_basis = legacy_pattern_only")
            faults.append("circular_historical_bias")

    badl.record(12, "Context / Justification",
                {"outcome_gap_detected": outcome_gap_detected,
                 "justification_present": bool(justification_text),
                 "justification_specificity": justification_specificity,
                 "justification_basis": justification_basis},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} justification faults", faults)


def gate_13_temporal_bias(submission: dict, badl: BiasAuditDecisionLog) -> None:
    """
    Gate 13 — Temporal Bias Controls
    Detects bias trends, failed remediations, deployment-induced bias, and drift.
    """
    prior_gap              = submission.get("prior_disparity_gap", 0.0)
    current_gap            = submission.get("current_disparity_gap", 0.0)
    remediation_applied    = submission.get("prior_remediation_applied", False)
    post_deploy_gap        = submission.get("post_deploy_disparity_gap", 0.0)
    pre_deploy_gap         = submission.get("pre_deploy_disparity_gap", 0.0)
    fairness_drift_score   = submission.get("fairness_drift_score", 0.0)
    faults                 = []

    # IF bias disparity worsening over time
    if current_gap > prior_gap + DRIFT_MARGIN:
        badl.add_fault("worsening_bias_trend", group=None,
                       detail=f"current_gap={current_gap:.3f}, prior_gap={prior_gap:.3f}, "
                              f"increase={current_gap - prior_gap:.3f} > drift_margin={DRIFT_MARGIN}")
        faults.append("worsening_bias_trend")

    # IF remediation previously applied but disparity remains
    if remediation_applied and current_gap > RESIDUAL_GAP_THRESHOLD:
        badl.add_fault("ineffective_bias_remediation", group=None,
                       detail=f"remediation applied but gap={current_gap:.3f} > residual_threshold={RESIDUAL_GAP_THRESHOLD}")
        faults.append("ineffective_bias_remediation")

    # IF bias appears only after deployment
    if post_deploy_gap > pre_deploy_gap + DEPLOYMENT_GAP_THRESHOLD:
        badl.add_fault("deployment_induced_bias", group=None,
                       detail=f"post_deploy_gap={post_deploy_gap:.3f}, pre_deploy_gap={pre_deploy_gap:.3f}")
        faults.append("deployment_induced_bias")

    # IF fairness drift detected
    if fairness_drift_score > FAIRNESS_DRIFT_THRESHOLD:
        badl.add_fault("fairness_drift", group=None,
                       detail=f"fairness_drift_score={fairness_drift_score:.3f} > {FAIRNESS_DRIFT_THRESHOLD}")
        faults.append("fairness_drift")

    badl.record(13, "Temporal Bias",
                {"current_gap": current_gap,
                 "prior_gap": prior_gap,
                 "fairness_drift_score": fairness_drift_score,
                 "remediation_applied": remediation_applied},
                "faults_detected" if faults else "PASS",
                f"{len(faults)} temporal bias faults", faults)


def gate_14_severity(badl: BiasAuditDecisionLog) -> dict:
    """
    Gate 14 — Severity Controls
    Assigns the maximum severity level across all detected bias faults.
    """
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in badl.fault_register:
        sev = f.get("severity", "low")
        if sev in counts:
            counts[sev] += 1

    # Determine max severity
    if counts["critical"] > 0:
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

    badl.record(14, "Severity Controls",
                {"critical_count": counts["critical"],
                 "high_count": counts["high"],
                 "medium_count": counts["medium"],
                 "low_count": counts["low"],
                 "total_faults": len(badl.fault_register)},
                max_severity,
                f"max_severity: {max_severity}")
    return {"max_severity": max_severity, "fault_counts": counts}


def gate_15_root_cause(submission: dict, badl: BiasAuditDecisionLog) -> list:
    """
    Gate 15 — Root Cause Attribution Controls
    Attributes detected bias faults to root causes.
    """
    REPRESENTATION_FAULTS = {"underrepresentation", "overrepresentation",
                              "omitted_subgroup", "worsening_representation_skew",
                              "weak_reference_baseline"}
    SELECTION_FAULTS      = {"selection_bias", "access_bias", "differential_missingness",
                              "self_selection_bias", "filter_stage_bias"}
    LABEL_FAULTS          = {"inconsistent_labeling", "severity_label_bias",
                              "annotation_uncertainty_bias", "biased_ground_truth",
                              "label_guideline_risk"}
    PROXY_FAULTS          = {"sensitive_proxy_risk", "name_proxy_bias",
                              "linguistic_proxy_bias", "protected_attribute_leakage",
                              "weak_proxy_measure"}
    THRESHOLD_FAULTS      = {"threshold_bias"}

    detected = {f["bias_fault"] for f in badl.fault_register}
    root_causes = []

    if detected & (REPRESENTATION_FAULTS | SELECTION_FAULTS):
        root_causes.append("sampling_bias")
    if detected & LABEL_FAULTS:
        root_causes.append("annotation_bias")
    if detected & PROXY_FAULTS:
        root_causes.append("feature_bias")
    if detected & THRESHOLD_FAULTS:
        root_causes.append("decision_threshold_bias")

    # IF bias linked to policy/rule design
    rule_disparity = submission.get("hard_rule_produces_unjustified_disparity", False)
    if rule_disparity:
        root_causes.append("rule_design_bias")

    # IF root cause unclear
    root_cause_probs = submission.get("root_cause_probabilities", {})
    if not root_causes:
        max_prob = max(root_cause_probs.values(), default=0.0)
        if max_prob < ROOT_CAUSE_CONFIDENCE_FLOOR:
            root_causes.append("unresolved")

    badl.root_causes = root_causes
    badl.record(15, "Root Cause Attribution",
                {"detected_fault_types": len(detected),
                 "rule_design_disparity": rule_disparity},
                str(root_causes),
                f"root_causes: {root_causes}")
    return root_causes


def gate_16_classification(max_severity: str, fault_counts: dict,
                            root_causes: list, total_bias_score: float,
                            badl: BiasAuditDecisionLog) -> str:
    """
    Gate 16 — Action Classification
    Combines severity, fault counts, and root cause into a single classification.
    """
    critical_count = fault_counts.get("critical", 0)

    if critical_count > 0:
        classification = "block_output"
    elif max_severity == "systemic":
        classification = "escalate_systemic_bias_issue"
    elif "unresolved" in root_causes and max_severity in {"medium", "escalated_high", "high"}:
        classification = "manual_fairness_investigation"
    elif max_severity in {"high", "escalated_high"}:
        classification = "fail_bias_audit"
    elif max_severity == "medium":
        classification = "fairness_review_recommended"
    elif max_severity == "low":
        classification = "pass_with_bias_warning"
    elif (critical_count == 0 and fault_counts.get("high", 0) == 0
          and total_bias_score < ACCEPT_THRESHOLD):
        classification = "pass"
    else:
        classification = "fairness_review_recommended"

    badl.classification = classification
    badl.record(16, "Action Classification",
                {"max_severity": max_severity,
                 "critical_count": critical_count,
                 "total_bias_score": round(total_bias_score, 4),
                 "root_causes": root_causes},
                classification, f"classification: {classification}")
    return classification


def gate_17_remediation(classification: str, badl: BiasAuditDecisionLog) -> list:
    """
    Gate 17 — Remediation Controls
    Assigns remediation directives to all active bias fault types.
    """
    FAULT_REMEDIATION = {
        "underrepresentation":            "rebalance_or_reweight_sample",
        "overrepresentation":             "rebalance_or_reweight_sample",
        "omitted_subgroup":               "rebalance_or_reweight_sample",
        "worsening_representation_skew":  "rebalance_or_reweight_sample",
        "selection_bias":                 "redesign_selection_process",
        "access_bias":                    "redesign_selection_process",
        "filter_stage_bias":              "redesign_selection_process",
        "inconsistent_labeling":          "relabel_with_revised_guidelines",
        "severity_label_bias":            "relabel_with_revised_guidelines",
        "biased_ground_truth":            "relabel_with_revised_guidelines",
        "sensitive_proxy_risk":           "remove_or_constrain_proxy_features",
        "name_proxy_bias":                "remove_or_constrain_proxy_features",
        "linguistic_proxy_bias":          "remove_or_constrain_proxy_features",
        "protected_attribute_leakage":    "remove_or_constrain_proxy_features",
        "threshold_bias":                 "recalibrate_thresholds",
        "loaded_language":                "rewrite_output_with_neutral_language_rules",
        "stereotyped_framing":            "rewrite_output_with_neutral_language_rules",
        "unnecessary_attribute_reference": "rewrite_output_with_neutral_language_rules",
        "exclusionary_language":          "rewrite_output_with_neutral_language_rules",
    }

    directives = set()
    for fault in badl.fault_register:
        fs = fault.get("bias_fault", "")
        if fs in FAULT_REMEDIATION:
            directives.add(FAULT_REMEDIATION[fs])

    if classification == "block_output":
        directives.add("suppress_release")

    directive_list = sorted(directives)
    badl.remediation = directive_list
    badl.record(17, "Remediation Controls",
                {"fault_count": len(badl.fault_register),
                 "classification": classification},
                "directives_assigned", f"directives: {directive_list}")
    return directive_list


def gate_18_evaluation_loop(submission: dict, classification: str,
                              badl: BiasAuditDecisionLog) -> None:
    """
    Gate 18 — Evaluation Loop
    Stores the bias audit record and adjusts thresholds based on observed rates.
    TODO: DATA_DEPENDENCY — threshold update feedback requires accumulated audit history.
    """
    missed_bias_rate    = submission.get("missed_bias_rate", 0.0)
    false_positive_rate = submission.get("false_positive_rate", 0.0)
    threshold_note      = "no_adjustment_needed"

    if missed_bias_rate > MISSED_BIAS_THRESHOLD:
        threshold_note = "tighten_bias_rules: missed_bias_rate exceeded"
        _db.post_suggestion(
            created_by="bias_audit_agent",
            target_files=["bias_audit_agent.py"],
            description=f"Bias threshold review: missed_bias_rate={missed_bias_rate:.3f} "
                        f"> threshold {MISSED_BIAS_THRESHOLD}. Consider tightening rules.",
            risk_level="LOW",
            impact="bias_calibration",
            metadata=json.dumps({"missed_bias_rate": missed_bias_rate}),
        )
    elif false_positive_rate > FALSE_POSITIVE_THRESHOLD:
        threshold_note = "relax_bias_rules: false_positive_rate exceeded"
        _db.post_suggestion(
            created_by="bias_audit_agent",
            target_files=["bias_audit_agent.py"],
            description=f"Bias threshold review: false_positive_rate={false_positive_rate:.3f} "
                        f"> threshold {FALSE_POSITIVE_THRESHOLD}. Consider relaxing rules.",
            risk_level="LOW",
            impact="bias_calibration",
            metadata=json.dumps({"false_positive_rate": false_positive_rate}),
        )

    badl.record(18, "Evaluation Loop",
                {"missed_bias_rate": missed_bias_rate,
                 "false_positive_rate": false_positive_rate,
                 "classification_stored": classification},
                "stored", threshold_note)


def gate_19_output(classification: str, badl: BiasAuditDecisionLog) -> str:
    """
    Gate 19 — Output Controls
    Determines the final output action based on classification.
    """
    ACTION_MAP = {
        "pass":                          "approve_result",
        "pass_with_bias_warning":        "approve_with_fairness_warning",
        "fairness_review_recommended":   "send_to_fairness_review",
        "fail_bias_audit":               "reject_result",
        "block_output":                  "block_downstream_use",
        "escalate_systemic_bias_issue":  "trigger_systemic_fairness_escalation",
        "manual_fairness_investigation": "request_investigation",
    }
    output_action = ACTION_MAP.get(classification, "send_to_fairness_review")
    badl.output_action = output_action
    badl.record(19, "Output Controls",
                {"classification": classification},
                output_action, f"output_action: {output_action}")
    return output_action


def gate_20_composite_score(badl: BiasAuditDecisionLog) -> dict:
    """
    Gate 20 — Final Composite Bias Score
    Computes weighted bias score across all seven categories.
    Higher score = more severely biased.
    """
    FAULT_CATEGORY = {
        "underrepresentation":            "representation",
        "overrepresentation":             "representation",
        "omitted_subgroup":               "representation",
        "worsening_representation_skew":  "representation",
        "weak_reference_baseline":        "representation",
        "selection_bias":                 "selection",
        "access_bias":                    "selection",
        "differential_missingness":       "selection",
        "self_selection_bias":            "selection",
        "filter_stage_bias":              "selection",
        "inconsistent_labeling":          "label",
        "severity_label_bias":            "label",
        "annotation_uncertainty_bias":    "label",
        "biased_ground_truth":            "label",
        "label_guideline_risk":           "label",
        "differential_measurement_error": "measurement",
        "weak_proxy_measure":             "measurement",
        "collection_bias":                "measurement",
        "normalization_bias":             "measurement",
        "imputation_bias":                "measurement",
        "outcome_rate_disparity":         "outcome",
        "false_positive_disparity":       "outcome",
        "false_negative_disparity":       "outcome",
        "group_calibration_gap":          "outcome",
        "threshold_bias":                 "outcome",
        "ranking_exposure_bias":          "outcome",
        "loaded_language":                "language",
        "stereotyped_framing":            "language",
        "tone_disparity":                 "language",
        "agency_bias":                    "language",
        "unnecessary_attribute_reference": "language",
        "exclusionary_language":          "language",
        "sensitive_proxy_risk":           "proxy",
        "name_proxy_bias":                "proxy",
        "linguistic_proxy_bias":          "proxy",
        "protected_attribute_leakage":    "proxy",
        "counterfactual_instability":     "outcome",
        "counterfactual_score_bias":      "outcome",
        "counterfactual_explanation_bias": "language",
        "worsening_bias_trend":           "outcome",
        "ineffective_bias_remediation":   "outcome",
        "deployment_induced_bias":        "outcome",
        "fairness_drift":                 "outcome",
    }

    SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.70, "medium": 0.40,
                       "low": 0.15, "none": 0.0}

    category_scores = {
        "representation": 0.0,
        "selection":      0.0,
        "label":          0.0,
        "measurement":    0.0,
        "outcome":        0.0,
        "language":       0.0,
        "proxy":          0.0,
    }

    for fault in badl.fault_register:
        bf  = fault.get("bias_fault", "")
        sev = fault.get("severity", "low")
        cat = FAULT_CATEGORY.get(bf, "outcome")
        category_scores[cat] = min(category_scores[cat] + SEVERITY_WEIGHT.get(sev, 0.15), 1.0)

    bias_score = (
        W1_REPRESENTATION * category_scores["representation"]
      + W2_SELECTION      * category_scores["selection"]
      + W3_LABEL          * category_scores["label"]
      + W4_MEASUREMENT    * category_scores["measurement"]
      + W5_OUTCOME        * category_scores["outcome"]
      + W6_LANGUAGE       * category_scores["language"]
      + W7_PROXY          * category_scores["proxy"]
    )
    bias_score = max(0.0, min(bias_score, 1.0))

    # Final bias signal
    critical_count = sum(1 for f in badl.fault_register if f.get("severity") == "critical")
    if critical_count > 0:
        final_signal = "blocked"
    elif badl.classification == "escalate_systemic_bias_issue":
        final_signal = "systemic_bias_failure"
    elif bias_score >= FAIL_THRESHOLD:
        final_signal = "biased"
    elif bias_score >= WARNING_THRESHOLD:
        final_signal = "warning"
    else:
        final_signal = "clean"

    components = {
        "representation": round(W1_REPRESENTATION * category_scores["representation"], 4),
        "selection":      round(W2_SELECTION      * category_scores["selection"],      4),
        "label":          round(W3_LABEL          * category_scores["label"],          4),
        "measurement":    round(W4_MEASUREMENT    * category_scores["measurement"],    4),
        "outcome":        round(W5_OUTCOME        * category_scores["outcome"],        4),
        "language":       round(W6_LANGUAGE       * category_scores["language"],       4),
        "proxy":          round(W7_PROXY          * category_scores["proxy"],          4),
    }

    badl.bias_score        = round(bias_score, 4)
    badl.final_bias_signal = final_signal

    badl.record(20, "Final Composite Bias Score",
                {"components": components,
                 "bias_score": round(bias_score, 4),
                 "critical_count": critical_count},
                final_signal,
                f"bias_score: {round(bias_score, 4)}, final_signal: {final_signal}")
    return {"bias_score": bias_score, "final_bias_signal": final_signal}


# ── PIPELINE ORCHESTRATOR ──────────────────────────────────────────────────────

def audit_bias(submission: dict) -> dict:
    """
    Run a single submission through all 20 bias-detection gates.

    submission keys (all optional except result_payload and bias_rule_set):
      result_payload               — the result to audit (required)
      bias_rule_set                — rule set dict (required)
      subject_attributes           — group attributes for subjects
      attribute_context_required   — bool, default False
      comparison_group_payload     — comparison group data
      parity_test_required         — bool, default False
      bias_modes                   — list of mode names; empty = all modes
      group_shares                 — {group: share_in_sample}
      reference_shares             — {group: share_in_population}
      approval_rates               — {group: approval_rate}
      fpr_by_group / fnr_by_group  — {group: rate}
      counterfactual_pairs         — list of pair dicts
      feature_proxy_scores         — {feature: proxy_score}
      ... (see individual gate implementations for full key list)

    Returns the complete BiasAuditDecisionLog as a dict.
    """
    run_ts      = _now_utc()
    result_id   = submission.get("result_id", "unknown")
    result_type = submission.get("result_type", "unknown")
    bias_modes  = submission.get("bias_modes", [])

    badl = BiasAuditDecisionLog(result_id, result_type, bias_modes, run_ts)

    # ── GATE 1: System Gate ────────────────────────────────────────────────────
    if not gate_1_system(submission, badl):
        return badl.to_dict()

    # ── GATE 2: Bias Audit Mode ────────────────────────────────────────────────
    active_modes = gate_2_audit_mode(submission, badl)

    # ── GATE 3: Group Mapping ──────────────────────────────────────────────────
    gate_3_group_mapping(submission, badl)

    # ── GATES 4–13: Bias Detection Gates (non-halting) ────────────────────────
    gate_4_representation(submission, active_modes, badl)
    gate_5_selection(submission, active_modes, badl)
    gate_6_label_bias(submission, active_modes, badl)
    gate_7_measurement(submission, active_modes, badl)
    gate_8_outcome(submission, active_modes, badl)
    gate_9_language(submission, active_modes, badl)
    gate_10_counterfactual(submission, badl)
    gate_11_proxy_leakage(submission, badl)
    gate_12_justification(submission, badl)
    gate_13_temporal_bias(submission, badl)

    # ── GATE 14: Severity Controls ─────────────────────────────────────────────
    g14 = gate_14_severity(badl)
    max_severity = g14["max_severity"]
    fault_counts = g14["fault_counts"]

    # ── GATE 15: Root Cause Attribution ───────────────────────────────────────
    root_causes = gate_15_root_cause(submission, badl)

    # ── GATE 16: Action Classification ────────────────────────────────────────
    total_bias_score = len(badl.fault_register) * 0.05  # rough proxy pre-gate-20
    classification = gate_16_classification(
        max_severity, fault_counts, root_causes, total_bias_score, badl)

    # ── GATE 17: Remediation Controls ─────────────────────────────────────────
    gate_17_remediation(classification, badl)

    # ── GATE 18: Evaluation Loop ───────────────────────────────────────────────
    gate_18_evaluation_loop(submission, classification, badl)

    # ── GATE 19: Output Controls ───────────────────────────────────────────────
    gate_19_output(classification, badl)

    # ── GATE 20: Final Composite Bias Score ────────────────────────────────────
    gate_20_composite_score(badl)

    return badl.to_dict()


# ── DATABASE WRITE ─────────────────────────────────────────────────────────────

def write_bias_audit_log(audit: dict) -> None:
    """
    Write a completed BiasAuditDecisionLog to the database.
    FLAG: A dedicated bias_audit_decisions table is recommended for regulatory export.
    TODO: tracked in AGENT6_SYSTEM_DESCRIPTION.md section 6.
    """
    summary = (
        f"[bias_audit_agent] result={audit['result_id']} "
        f"type={audit['result_type']} "
        f"classification={audit.get('classification')} "
        f"final_signal={audit.get('final_bias_signal')} "
        f"bias_score={audit.get('bias_score')} "
        f"faults={len(audit.get('fault_register', []))}"
    )
    _db.log_event(
        agent="bias_audit_agent",
        event_type="bias_audit_decision",
        message=summary,
        payload=json.dumps(audit),
    )


def escalate_if_needed(audit: dict) -> None:
    """
    Post a high-priority suggestion if classification requires escalation.
    """
    cls = audit.get("classification")
    if cls in {"block_output", "escalate_systemic_bias_issue"}:
        _db.post_suggestion(
            created_by="bias_audit_agent",
            target_files=[],
            description=(
                f"BIAS ESCALATION: result={audit['result_id']} "
                f"classification={cls} "
                f"bias_score={audit.get('bias_score')} "
                f"signal={audit.get('final_bias_signal')} "
                f"root_causes={audit.get('root_causes')}"
            ),
            risk_level="HIGH" if cls == "block_output" else "MEDIUM",
            impact="bias_escalation",
            metadata=json.dumps(audit),
        )
        log.warning(f"bias escalation posted: result={audit['result_id']} cls={cls}")


# ── STATUS ─────────────────────────────────────────────────────────────────────

def show_status() -> None:
    try:
        rows = _db.get_recent_events(agent="bias_audit_agent", limit=10)
        if not rows:
            print("No recent bias audit runs found.")
            return
        print(f"Last {len(rows)} events for bias_audit_agent:")
        for r in rows:
            print(f"  {r}")
    except Exception as exc:
        print(f"Status unavailable: {exc}")


# ── ENTRYPOINT ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bias Audit Agent — runs a result payload through a "
                    "20-gate deterministic bias-detection spine."
    )
    parser.add_argument("--mode", choices=["audit", "status"], default="audit")
    parser.add_argument("--payload", type=str, default=None,
                        help="JSON string of the full audit submission dict.")
    parser.add_argument("--file", type=str, default=None,
                        help="Path to a JSON file containing the audit submission dict.")
    args = parser.parse_args()

    if args.mode == "status":
        show_status()
        return

    if args.file:
        with open(args.file, "r") as fh:
            submission = json.load(fh)
    elif args.payload:
        submission = json.loads(args.payload)
    else:
        print("ERROR: --payload or --file required for audit mode.")
        sys.exit(1)

    audit = audit_bias(submission)
    write_bias_audit_log(audit)
    escalate_if_needed(audit)

    print(f"\nBias audit result: {audit.get('classification')} | "
          f"signal: {audit.get('final_bias_signal')} | "
          f"bias_score: {audit.get('bias_score')} | "
          f"faults: {len(audit.get('fault_register', []))}")
    if audit.get("halt_reason"):
        print(f"Halted: {audit['halt_reason']}")


# ── V1 ENTRY POINT ────────────────────────────────────────────────────────────

def run_agent(snapshot: dict) -> dict:
    """
    V1 standard entry point for the Bias Detection Agent (7.10).

    snapshot keys:
        trade_output  (required) — Trade Logic Agent output dict
        run_id        (required) — unique run identifier

    Returns:
        classification — block_output | fail_bias_audit | pass_with_bias_warning |
                         fairness_review_recommended | pass
        bias_findings  — list of bias code strings
        decision_log   — list of gate trace records
    """
    trade_output = snapshot.get("trade_output")
    run_id       = snapshot.get("run_id", "unknown")

    submission = {
        "result_id":      run_id,
        "result_type":    "trade_output",
        "result_payload": trade_output if trade_output is not None else {},
        "bias_rule_set":  {"mode": "trade_output_bias_audit"},
    }
    if trade_output is None:
        submission["result_payload"] = None

    result = audit_bias(submission)

    # Map internal classification values to V1 output states
    raw_cls = result.get("classification", "block_output")
    cls_map = {
        "block_output":               "block_output",
        "fail_bias_audit":            "fail_bias_audit",
        "pass_with_bias_warning":     "pass_with_bias_warning",
        "fairness_review_recommended":"fairness_review_recommended",
        "pass":                       "pass",
        # Internal names that map to V1 states
        "escalate_systemic_issue":    "block_output",
        "review_recommended":         "fairness_review_recommended",
        "pass_with_warnings":         "pass_with_bias_warning",
    }

    bias_findings = [f.get("fault_state", "unknown")
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
        "classification": cls_map.get(raw_cls, "fail_bias_audit"),
        "bias_findings":  bias_findings,
        "decision_log":   decision_log,
    }


if __name__ == "__main__":
    main()
