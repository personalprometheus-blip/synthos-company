#!/usr/bin/env python3
"""
master_orchestration_layer.py — Agent 7.12
Master Orchestration Layer. 20-section deterministic, rule-based sequencing
layer. Imports and calls all 11 upstream agents in dependency order, enforces
gate logic at each section, accumulates run state into a structured
OrchestrationLog, performs audit fusion, and emits the final release
classification, health state, execution state, and release action.

This is the ONLY agent that imports other agents directly.

Section 1  : Intake — deduplication, timestamp validation, structural check.
Section 2  : Parse — extract and validate all required snapshot fields.
Section 3  : Dispatcher — route and cycle validation.
Section 4  : Upstream readiness — minimum data-type gate.
Section 5  : News agent execution.
Section 6  : Social rumor agent execution + upgrade detection.
Section 7  : News–rumor integration (conditional re-run).
Section 8  : Macro regime agent execution.
Section 9  : Market sentiment agent execution.
Section 10 : Flow positioning agent execution.
Section 11 : Market state aggregation — blocked_override halt.
Section 12 : Trade logic — null output halt.
Section 13 : Fault audit.
Section 14 : Bias audit (conditional).
Section 15 : Validator (audit stack).
Section 16 : Audit fusion — priority ordering.
Section 17 : Specialist reroute — single-lane failure retry.
Section 18 : Execution state derivation.
Section 19 : Health scoring.
Section 20 : Release decision and final action mapping.
"""

import sys
import os
import json
import logging
import argparse
import datetime
import hashlib
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("master_orchestration_layer")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION                = "1.0.0"
ORCHESTRATION_MAX_AGE        = 60    # minutes — max allowed snapshot age
ORCHESTRATION_MIN_READY_BLOCKS = 2  # minimum upstream data types that must be present
HIGH_HEALTH_THRESHOLD        = 0.80
MEDIUM_HEALTH_THRESHOLD      = 0.50
TRACEABILITY_MIN_RECORDS     = 1     # minimum decision_log records required per agent

# ============================================================
# AGENT IMPORTS — wrapped so individual failures are non-fatal
# ============================================================

try:
    from agents.company_dispatcher_agent import run_agent as run_dispatcher
    _dispatcher_available = True
except Exception as _e:
    log.warning("dispatcher_agent import failed: %s", _e)
    run_dispatcher = None
    _dispatcher_available = False

try:
    from agents.process_news_agent import run_agent as run_news
    _news_available = True
except Exception as _e:
    log.warning("news_agent import failed: %s", _e)
    run_news = None
    _news_available = False

try:
    from agents.company_social_rumor_agent import run_agent as run_social_rumor
    _social_rumor_available = True
except Exception as _e:
    log.warning("social_rumor_agent import failed: %s", _e)
    run_social_rumor = None
    _social_rumor_available = False

try:
    from agents.process_market_sentiment_agent import run_agent as run_sentiment
    _sentiment_available = True
except Exception as _e:
    log.warning("market_sentiment_agent import failed: %s", _e)
    run_sentiment = None
    _sentiment_available = False

try:
    from agents.company_macro_regime_agent import run_agent as run_macro
    _macro_available = True
except Exception as _e:
    log.warning("macro_regime_agent import failed: %s", _e)
    run_macro = None
    _macro_available = False

try:
    from agents.company_flow_positioning_agent import run_agent as run_flow
    _flow_available = True
except Exception as _e:
    log.warning("flow_positioning_agent import failed: %s", _e)
    run_flow = None
    _flow_available = False

try:
    from agents.company_market_state_aggregator import run_agent as run_aggregator
    _aggregator_available = True
except Exception as _e:
    log.warning("market_state_aggregator import failed: %s", _e)
    run_aggregator = None
    _aggregator_available = False

try:
    from agents.process_trade_logic_agent import run_agent as run_trade
    _trade_available = True
except Exception as _e:
    log.warning("trade_logic_agent import failed: %s", _e)
    run_trade = None
    _trade_available = False

try:
    from agents.company_result_audit_agent import run_agent as run_fault
    _fault_available = True
except Exception as _e:
    log.warning("result_audit_agent import failed: %s", _e)
    run_fault = None
    _fault_available = False

try:
    from agents.company_bias_audit_agent import run_agent as run_bias
    _bias_available = True
except Exception as _e:
    log.warning("bias_audit_agent import failed: %s", _e)
    run_bias = None
    _bias_available = False

try:
    from agents.company_audit_stack_agent import run_agent as run_validator
    _validator_available = True
except Exception as _e:
    log.warning("audit_stack_agent import failed: %s", _e)
    run_validator = None
    _validator_available = False

# ============================================================
# ORCHESTRATION LOG
# ============================================================

class OrchestrationLog:
    """
    Accumulates run state across all 20 sections. Holds all agent outputs,
    all section decision records, and computes the final output dict.
    """

    def __init__(self):
        # --- Section halt tracking ---
        self.halted            = False
        self.halt_reason       = None
        self.halt_section      = None

        # --- Run metadata ---
        self.run_id            = None
        self.timestamp         = None
        self.request_hash      = None

        # --- Parsed snapshot ---
        self.snapshot          = {}

        # --- Agent outputs (all 11 always present, may be None) ---
        self.dispatcher_output    = None
        self.news_output          = None
        self.social_rumor_output  = None
        self.sentiment_output     = None
        self.macro_output         = None
        self.flow_output          = None
        self.aggregator_output    = None
        self.trade_output         = None
        self.fault_output         = None
        self.bias_output          = None
        self.validator_output     = None

        # --- Intermediate flags ---
        self.rumor_promoted             = False
        self.promoted_rumor_context     = None
        self.news_rerun_output          = None   # result of section 7 re-run if triggered
        self.bias_rerun_output          = None   # result of section 17 bias re-run
        self.fault_rerun_output         = None   # result of section 17 fault re-run

        # --- Audit fusion ---
        self.audit_fusion_result        = None   # "approved" | "caution" | "failed" | "escalate" | "block"
        self.audit_fusion_detail        = {}

        # --- Final outputs ---
        self.classification             = None   # release | release_with_caution | reject | block | escalate | halt
        self.health_state               = None   # healthy | watch | degraded | failed
        self.execution_state            = None   # eligible | conditional_eligible | ineligible | blocked | escalate
        self.release_action             = None   # RELEASE_ORDER | RELEASE_ORDER_WITH_CAUTION | SUPPRESS_ORDER_RELEASE | BLOCK_ORDER_RELEASE | TRIGGER_ESCALATION_PROTOCOL
        self.orchestration_flags        = {}
        self.decision_log               = []

    # ----------------------------------------------------------
    # Logging helpers
    # ----------------------------------------------------------

    def record(self, section: str, event: str, detail: Any = None):
        """Append a structured entry to the decision log."""
        entry = {
            "section":    section,
            "event":      event,
            "detail":     detail,
            "ts":         datetime.datetime.utcnow().isoformat() + "Z",
        }
        self.decision_log.append(entry)
        log.info("[%s] %s — %s", section, event, detail)

    def halt(self, section: str, reason: str, detail: Any = None):
        """Mark the orchestration as halted and record the cause."""
        self.halted       = True
        self.halt_reason  = reason
        self.halt_section = section
        self.record(section, "HALT", {"reason": reason, "detail": detail})

    # ----------------------------------------------------------
    # Output assembly
    # ----------------------------------------------------------

    def to_dict(self) -> dict:
        """Assemble and return the final output dictionary."""
        return {
            # Final decisions
            "classification":        self.classification,
            "health_state":          self.health_state,
            "execution_state":       self.execution_state,
            "release_action":        self.release_action,
            # All 11 agent outputs
            "dispatcher_output":     self.dispatcher_output,
            "news_output":           self.news_output,
            "social_rumor_output":   self.social_rumor_output,
            "sentiment_output":      self.sentiment_output,
            "macro_output":          self.macro_output,
            "flow_output":           self.flow_output,
            "aggregator_output":     self.aggregator_output,
            "trade_output":          self.trade_output,
            "fault_output":          self.fault_output,
            "bias_output":           self.bias_output,
            "validator_output":      self.validator_output,
            # Metadata
            "orchestration_flags":   self.orchestration_flags,
            "decision_log":          self.decision_log,
            "agent_version":         AGENT_VERSION,
            "run_id":                self.run_id,
            "halt_reason":           self.halt_reason,
            "halt_section":          self.halt_section,
        }


# ============================================================
# HELPER: safe agent call
# ============================================================

def _call_agent(section: str, label: str, fn, payload: dict, orc: OrchestrationLog):
    """
    Call an agent function with payload. Returns the agent's output dict on
    success, or None on any exception or if the function is unavailable.
    Appends a decision_log record either way.
    """
    if fn is None:
        orc.record(section, f"{label}_unavailable", "import failed at startup")
        return None
    try:
        result = fn(payload)
        orc.record(section, f"{label}_success", {
            "keys": list(result.keys()) if isinstance(result, dict) else type(result).__name__
        })
        return result
    except Exception as exc:
        orc.record(section, f"{label}_exception", str(exc))
        log.warning("[%s] %s raised exception: %s", section, label, exc)
        return None


# ============================================================
# SECTION IMPLEMENTATIONS
# ============================================================

def _section_1_intake(raw_input: Any, processed_request_store: list, orc: OrchestrationLog):
    """
    Hash raw_input, check for duplicates, validate timestamp age.
    Halts on: duplicate_request, stale_request, invalid_input.
    """
    section = "section_1_intake"

    # --- Basic structural check ---
    if not isinstance(raw_input, dict):
        orc.halt(section, "invalid_input", "raw_input must be a dict")
        return

    # --- Hash the raw input ---
    try:
        raw_bytes  = json.dumps(raw_input, sort_keys=True, default=str).encode("utf-8")
        req_hash   = hashlib.sha256(raw_bytes).hexdigest()
    except Exception as exc:
        orc.halt(section, "invalid_input", f"hash failed: {exc}")
        return

    orc.request_hash = req_hash
    orc.record(section, "request_hash_computed", req_hash)

    # --- Duplicate check ---
    if isinstance(processed_request_store, list) and req_hash in processed_request_store:
        orc.halt(section, "duplicate_request", f"hash={req_hash}")
        return

    # --- Timestamp presence ---
    ts_raw = raw_input.get("timestamp")
    if ts_raw is None:
        orc.halt(section, "invalid_input", "timestamp missing from raw_input")
        return

    # --- Timestamp age check ---
    try:
        if isinstance(ts_raw, (int, float)):
            ts_dt = datetime.datetime.utcfromtimestamp(float(ts_raw))
        else:
            ts_dt = datetime.datetime.fromisoformat(str(ts_raw).replace("Z", ""))
        age_minutes = (datetime.datetime.utcnow() - ts_dt).total_seconds() / 60.0
        if age_minutes > ORCHESTRATION_MAX_AGE:
            orc.halt(section, "stale_request", f"age={age_minutes:.1f}m limit={ORCHESTRATION_MAX_AGE}m")
            return
        orc.record(section, "timestamp_valid", f"age={age_minutes:.1f}m")
    except Exception as exc:
        orc.halt(section, "invalid_input", f"timestamp parse error: {exc}")
        return

    orc.timestamp = ts_raw
    orc.record(section, "intake_passed", req_hash)


def _section_2_parse(raw_input: dict, orc: OrchestrationLog):
    """
    Extract and validate all required fields from raw_input into a structured
    snapshot dict stored on orc.snapshot.
    Halts on: missing_required_fields.
    """
    section = "section_2_parse"

    required_keys = ["snapshot_payload", "route_registry", "cycle_detection_store", "run_id", "timestamp"]
    missing = [k for k in required_keys if k not in raw_input]
    if missing:
        orc.halt(section, "missing_required_fields", {"missing": missing})
        return

    orc.run_id   = raw_input["run_id"]
    orc.snapshot = {
        "snapshot_payload":      raw_input["snapshot_payload"],
        "route_registry":        raw_input["route_registry"],
        "cycle_detection_store": raw_input["cycle_detection_store"],
        "run_id":                raw_input["run_id"],
        "timestamp":             raw_input["timestamp"],
    }

    # Optional fields forwarded verbatim
    for opt in ["news_fields", "social_fields", "macro_fields", "flow_fields", "market_data", "bias_context"]:
        orc.snapshot[opt] = raw_input.get(opt, {})

    orc.record(section, "parse_passed", {"run_id": orc.run_id, "optional_fields_present": [
        k for k in ["news_fields", "social_fields", "macro_fields", "flow_fields", "market_data", "bias_context"]
        if raw_input.get(k)
    ]})


def _section_3_dispatcher(orc: OrchestrationLog):
    """
    Call run_dispatcher() with routing context from snapshot.
    Halts on invalid_route or cycle_break returned by the dispatcher, or if
    dispatcher output indicates halt.
    Halt key: dispatcher_halt.
    """
    section = "section_3_dispatcher"

    payload = {
        "snapshot":             orc.snapshot.get("snapshot_payload", {}),
        "route_registry":       orc.snapshot.get("route_registry", {}),
        "cycle_detection_store": orc.snapshot.get("cycle_detection_store", {}),
        "run_id":               orc.run_id,
        "timestamp":            orc.timestamp,
    }

    result = _call_agent(section, "dispatcher", run_dispatcher, payload, orc)
    orc.dispatcher_output = result

    if result is None:
        # Dispatcher unavailable — log warning but do not halt (non-critical routing)
        orc.record(section, "dispatcher_null_output", "proceeding without dispatcher validation")
        return

    # Check for halt signals in dispatcher output
    halt_signals = {"invalid_route", "cycle_break"}
    status = result.get("status") or result.get("route_status") or ""
    if status in halt_signals:
        orc.halt(section, "dispatcher_halt", {"dispatcher_status": status, "detail": result.get("detail")})
        return

    # Also check if dispatcher itself flagged a halt
    if result.get("halt") or result.get("halt_reason"):
        orc.halt(section, "dispatcher_halt", {"reason": result.get("halt_reason"), "detail": result.get("detail")})
        return

    orc.record(section, "dispatcher_passed", {"route_status": status})


def _section_4_upstream_readiness(orc: OrchestrationLog):
    """
    Count how many upstream data-type blocks are present and populated in the
    snapshot. Requires at least ORCHESTRATION_MIN_READY_BLOCKS.
    Does not halt — records a warning flag if below threshold.
    """
    section = "section_4_upstream_readiness"

    data_blocks = ["news_fields", "social_fields", "macro_fields", "flow_fields", "market_data"]
    ready = [b for b in data_blocks if orc.snapshot.get(b)]
    ready_count = len(ready)

    orc.record(section, "upstream_readiness_check", {
        "ready_blocks": ready,
        "ready_count":  ready_count,
        "minimum":      ORCHESTRATION_MIN_READY_BLOCKS,
    })

    if ready_count < ORCHESTRATION_MIN_READY_BLOCKS:
        orc.orchestration_flags["low_upstream_readiness"] = True
        orc.record(section, "upstream_readiness_warning", {
            "ready_count": ready_count,
            "minimum":     ORCHESTRATION_MIN_READY_BLOCKS,
        })
    else:
        orc.record(section, "upstream_readiness_passed", f"{ready_count}/{len(data_blocks)} blocks ready")


def _section_5_news(orc: OrchestrationLog):
    """Call run_news() with news fields from snapshot."""
    section = "section_5_news"
    payload = {
        **(orc.snapshot.get("news_fields") or {}),
        "run_id":    orc.run_id,
        "timestamp": orc.timestamp,
        "snapshot":  orc.snapshot.get("snapshot_payload", {}),
    }
    orc.news_output = _call_agent(section, "news", run_news, payload, orc)


def _section_6_social_rumor(orc: OrchestrationLog):
    """
    Call run_social_rumor() with social fields.
    Detect rumor upgrade: if the output contains upgraded_to_confirmed_event=True,
    set orc.rumor_promoted and store promoted_rumor_context.
    """
    section = "section_6_social_rumor"
    payload = {
        **(orc.snapshot.get("social_fields") or {}),
        "run_id":    orc.run_id,
        "timestamp": orc.timestamp,
        "snapshot":  orc.snapshot.get("snapshot_payload", {}),
    }
    result = _call_agent(section, "social_rumor", run_social_rumor, payload, orc)
    orc.social_rumor_output = result

    if result and result.get("upgraded_to_confirmed_event"):
        orc.rumor_promoted          = True
        orc.promoted_rumor_context  = result.get("promoted_context") or result
        orc.record(section, "rumor_promoted", {"promoted_context_keys": list(orc.promoted_rumor_context.keys())
                                                if isinstance(orc.promoted_rumor_context, dict) else None})
    else:
        orc.record(section, "rumor_not_promoted", None)


def _section_7_news_rumor_integration(orc: OrchestrationLog):
    """
    If a rumor was promoted in section 6, re-run the news agent with the
    promoted_rumor_context merged into the news payload. Store result as
    orc.news_rerun_output and update orc.news_output to the re-run result
    so downstream sections use the enriched news signal.
    """
    section = "section_7_news_rumor_integration"

    if not orc.rumor_promoted:
        orc.record(section, "skipped_no_promotion", None)
        return

    orc.record(section, "rerunning_news_with_promoted_rumor", None)
    payload = {
        **(orc.snapshot.get("news_fields") or {}),
        "promoted_rumor_context": orc.promoted_rumor_context,
        "run_id":                 orc.run_id,
        "timestamp":              orc.timestamp,
        "snapshot":               orc.snapshot.get("snapshot_payload", {}),
    }
    rerun_result = _call_agent(section, "news_rerun", run_news, payload, orc)
    orc.news_rerun_output = rerun_result
    if rerun_result is not None:
        orc.news_output = rerun_result   # replace with enriched signal
        orc.record(section, "news_output_updated_from_rerun", None)
    else:
        orc.record(section, "news_rerun_null_keeping_original", None)


def _section_8_macro(orc: OrchestrationLog):
    """Call run_macro() with macro fields from snapshot."""
    section = "section_8_macro"
    payload = {
        **(orc.snapshot.get("macro_fields") or {}),
        "run_id":    orc.run_id,
        "timestamp": orc.timestamp,
        "snapshot":  orc.snapshot.get("snapshot_payload", {}),
    }
    orc.macro_output = _call_agent(section, "macro", run_macro, payload, orc)


def _section_9_sentiment(orc: OrchestrationLog):
    """
    Call run_sentiment() with market data plus news and rumor context from
    earlier sections.
    """
    section = "section_9_sentiment"
    payload = {
        **(orc.snapshot.get("market_data") or {}),
        "news_output":         orc.news_output,
        "social_rumor_output": orc.social_rumor_output,
        "run_id":              orc.run_id,
        "timestamp":           orc.timestamp,
        "snapshot":            orc.snapshot.get("snapshot_payload", {}),
    }
    orc.sentiment_output = _call_agent(section, "sentiment", run_sentiment, payload, orc)


def _section_10_flow(orc: OrchestrationLog):
    """Call run_flow() with flow fields and market data."""
    section = "section_10_flow"
    payload = {
        **(orc.snapshot.get("flow_fields") or {}),
        **(orc.snapshot.get("market_data") or {}),
        "run_id":    orc.run_id,
        "timestamp": orc.timestamp,
        "snapshot":  orc.snapshot.get("snapshot_payload", {}),
    }
    orc.flow_output = _call_agent(section, "flow", run_flow, payload, orc)


def _section_11_aggregation(orc: OrchestrationLog):
    """
    Call run_aggregator() with all upstream outputs.
    Halt if aggregator returns blocked_override.
    Halt key: aggregation_blocked.
    """
    section = "section_11_aggregation"
    payload = {
        "dispatcher_output":   orc.dispatcher_output,
        "news_output":         orc.news_output,
        "social_rumor_output": orc.social_rumor_output,
        "sentiment_output":    orc.sentiment_output,
        "macro_output":        orc.macro_output,
        "flow_output":         orc.flow_output,
        "run_id":              orc.run_id,
        "timestamp":           orc.timestamp,
        "snapshot":            orc.snapshot.get("snapshot_payload", {}),
        "market_data":         orc.snapshot.get("market_data", {}),
    }
    result = _call_agent(section, "aggregator", run_aggregator, payload, orc)
    orc.aggregator_output = result

    if result and result.get("blocked_override"):
        orc.halt(section, "aggregation_blocked", {
            "blocked_override": result.get("blocked_override"),
            "detail":           result.get("detail"),
        })
        return

    orc.record(section, "aggregation_passed", {
        "aggregator_classification": (result or {}).get("classification"),
    })


def _section_12_trade_logic(orc: OrchestrationLog):
    """
    Call run_trade() with aggregator output, market data, and bias context.
    Null trade_output triggers halt: null_trade_output.
    """
    section = "section_12_trade_logic"
    payload = {
        "aggregator_output": orc.aggregator_output,
        "market_data":       orc.snapshot.get("market_data", {}),
        "bias_context":      orc.snapshot.get("bias_context", {}),
        "run_id":            orc.run_id,
        "timestamp":         orc.timestamp,
        "snapshot":          orc.snapshot.get("snapshot_payload", {}),
    }
    result = _call_agent(section, "trade", run_trade, payload, orc)
    orc.trade_output = result

    if result is None:
        orc.halt(section, "null_trade_output", "run_trade returned None")
        return

    orc.record(section, "trade_logic_passed", {
        "trade_classification": result.get("classification"),
    })


def _section_13_fault_audit(orc: OrchestrationLog):
    """Call run_fault() (result_audit_agent) with trade_output."""
    section = "section_13_fault_audit"
    payload = {
        "trade_output": orc.trade_output,
        "run_id":       orc.run_id,
        "timestamp":    orc.timestamp,
    }
    orc.fault_output = _call_agent(section, "fault", run_fault, payload, orc)


def _section_14_bias_audit(orc: OrchestrationLog):
    """
    Conditionally call run_bias() with trade_output.
    The bias audit runs when trade_output is present (already guaranteed post
    section 12 halt logic) and bias_context is non-empty, or unconditionally
    as a default audit path. Recorded as skipped only if trade_output is absent.
    """
    section = "section_14_bias_audit"
    if orc.trade_output is None:
        orc.record(section, "skipped_no_trade_output", None)
        return

    payload = {
        "trade_output":  orc.trade_output,
        "bias_context":  orc.snapshot.get("bias_context", {}),
        "run_id":        orc.run_id,
        "timestamp":     orc.timestamp,
    }
    orc.bias_output = _call_agent(section, "bias", run_bias, payload, orc)


def _section_15_validator(orc: OrchestrationLog):
    """Call run_validator() (audit_stack_agent) with trade_output."""
    section = "section_15_validator"
    payload = {
        "trade_output": orc.trade_output,
        "run_id":       orc.run_id,
        "timestamp":    orc.timestamp,
    }
    orc.validator_output = _call_agent(section, "validator", run_validator, payload, orc)


def _section_16_audit_fusion(orc: OrchestrationLog):
    """
    Combine fault, bias, and validator results using priority ordering.
    Priority (highest to lowest):
      1. any result contains "blocked"         → block
      2. validator systemic_failure            → escalate
      3. any result contains "failed"          → reject
      4. any result contains "caution"         → release_with_caution
      5. all approved (or all None)            → release

    Stores result in orc.audit_fusion_result and orc.audit_fusion_detail.
    Also maps directly to orc.classification.
    """
    section = "section_16_audit_fusion"

    def _extract_verdict(output: Any) -> str:
        """Pull the audit verdict string from an agent output dict."""
        if output is None:
            return "none"
        if isinstance(output, dict):
            for key in ("verdict", "audit_result", "result", "classification", "status"):
                val = output.get(key)
                if val:
                    return str(val).lower()
        return "none"

    fault_verdict     = _extract_verdict(orc.fault_output)
    bias_verdict      = _extract_verdict(orc.bias_output)
    validator_verdict = _extract_verdict(orc.validator_output)

    detail = {
        "fault_verdict":     fault_verdict,
        "bias_verdict":      bias_verdict,
        "validator_verdict": validator_verdict,
    }
    orc.audit_fusion_detail = detail
    orc.record(section, "audit_verdicts_collected", detail)

    all_verdicts = [fault_verdict, bias_verdict, validator_verdict]

    # Priority 1 — any blocked
    if any("blocked" in v for v in all_verdicts):
        fusion = "block"
        classification = "block"

    # Priority 2 — validator systemic_failure
    elif "systemic_failure" in validator_verdict:
        fusion = "escalate"
        classification = "escalate"

    # Priority 3 — any failed
    elif any("failed" in v for v in all_verdicts):
        fusion = "failed"
        classification = "reject"

    # Priority 4 — any caution
    elif any("caution" in v for v in all_verdicts):
        fusion = "caution"
        classification = "release_with_caution"

    # Priority 5 — all approved (or all None/unknown)
    else:
        fusion = "approved"
        classification = "release"

    orc.audit_fusion_result = fusion
    orc.classification      = classification
    orc.record(section, "audit_fusion_result", {"fusion": fusion, "classification": classification})


def _section_17_specialist_reroute(orc: OrchestrationLog):
    """
    Single-lane failure retry logic.
    - If only_bias_lane_failed: re-run bias agent and update orc.bias_output.
    - If only_correctness_lane_failed: re-run fault agent and update orc.fault_output.
    After any re-run, re-execute audit fusion to update classification.
    """
    section = "section_17_specialist_reroute"

    fault_failed     = "failed" in (orc.audit_fusion_detail.get("fault_verdict") or "")
    bias_failed      = "failed" in (orc.audit_fusion_detail.get("bias_verdict") or "")
    validator_failed = "failed" in (orc.audit_fusion_detail.get("validator_verdict") or "")

    only_bias_lane_failed        = bias_failed and not fault_failed and not validator_failed
    only_correctness_lane_failed = fault_failed and not bias_failed and not validator_failed

    if only_bias_lane_failed:
        orc.record(section, "reroute_bias_lane", "re-running bias agent")
        payload = {
            "trade_output":  orc.trade_output,
            "bias_context":  orc.snapshot.get("bias_context", {}),
            "run_id":        orc.run_id,
            "timestamp":     orc.timestamp,
            "reroute":       True,
        }
        rerun = _call_agent(section, "bias_rerun", run_bias, payload, orc)
        orc.bias_rerun_output = rerun
        if rerun is not None:
            orc.bias_output = rerun
            orc.record(section, "bias_rerun_applied", None)
            _section_16_audit_fusion(orc)   # re-fuse with updated bias result

    elif only_correctness_lane_failed:
        orc.record(section, "reroute_fault_lane", "re-running fault agent")
        payload = {
            "trade_output": orc.trade_output,
            "run_id":       orc.run_id,
            "timestamp":    orc.timestamp,
            "reroute":      True,
        }
        rerun = _call_agent(section, "fault_rerun", run_fault, payload, orc)
        orc.fault_rerun_output = rerun
        if rerun is not None:
            orc.fault_output = rerun
            orc.record(section, "fault_rerun_applied", None)
            _section_16_audit_fusion(orc)   # re-fuse with updated fault result

    else:
        orc.record(section, "no_reroute_needed", {
            "fault_failed":     fault_failed,
            "bias_failed":      bias_failed,
            "validator_failed": validator_failed,
        })


def _section_18_execution_state(orc: OrchestrationLog):
    """
    Derive execution_state from audit_fusion_result and classification.

    Mapping:
      block   → blocked
      escalate → escalate
      reject  → ineligible
      release_with_caution → conditional_eligible
      release → eligible
      halt    → blocked  (defensive fallback)
    """
    section = "section_18_execution_state"

    mapping = {
        "block":                "blocked",
        "escalate":             "escalate",
        "reject":               "ineligible",
        "release_with_caution": "conditional_eligible",
        "release":              "eligible",
        "halt":                 "blocked",
    }

    classification = orc.classification or "halt"
    execution_state = mapping.get(classification, "ineligible")
    orc.execution_state = execution_state
    orc.record(section, "execution_state_derived", {
        "classification":  classification,
        "execution_state": execution_state,
    })


def _section_19_health_scoring(orc: OrchestrationLog):
    """
    Compute health_state from agent success rates and classification.

    Success rate = count of non-None agent outputs / 11 total agents.
    Thresholds:
      >= HIGH_HEALTH_THRESHOLD    → healthy (unless classification forces lower)
      >= MEDIUM_HEALTH_THRESHOLD  → watch
      < MEDIUM_HEALTH_THRESHOLD   → degraded

    Override: if classification is block, halt, or escalate → health_state = failed
    Also check low_traceability: flag if decision_log has fewer than
    TRACEABILITY_MIN_RECORDS per agent that ran.
    """
    section = "section_19_health_scoring"

    all_outputs = {
        "dispatcher":   orc.dispatcher_output,
        "news":         orc.news_output,
        "social_rumor": orc.social_rumor_output,
        "sentiment":    orc.sentiment_output,
        "macro":        orc.macro_output,
        "flow":         orc.flow_output,
        "aggregator":   orc.aggregator_output,
        "trade":        orc.trade_output,
        "fault":        orc.fault_output,
        "bias":         orc.bias_output,
        "validator":    orc.validator_output,
    }
    total          = len(all_outputs)
    success_count  = sum(1 for v in all_outputs.values() if v is not None)
    success_rate   = success_count / total if total > 0 else 0.0

    orc.record(section, "agent_success_rate", {
        "success_count": success_count,
        "total":         total,
        "rate":          round(success_rate, 3),
    })

    # Base health from success rate
    if success_rate >= HIGH_HEALTH_THRESHOLD:
        health_state = "healthy"
    elif success_rate >= MEDIUM_HEALTH_THRESHOLD:
        health_state = "watch"
    else:
        health_state = "degraded"

    # Override for terminal classifications
    if orc.classification in ("block", "halt", "escalate"):
        health_state = "failed"

    # Traceability check
    agents_ran = success_count
    log_records = len(orc.decision_log)
    expected_min = agents_ran * TRACEABILITY_MIN_RECORDS
    if log_records < expected_min:
        orc.orchestration_flags["low_traceability"] = True
        orc.record(section, "low_traceability_flag", {
            "log_records":   log_records,
            "expected_min":  expected_min,
        })
    else:
        orc.orchestration_flags["low_traceability"] = False

    orc.health_state = health_state
    orc.record(section, "health_state_derived", {
        "health_state":  health_state,
        "success_rate":  round(success_rate, 3),
        "classification": orc.classification,
    })


def _section_20_release_decision(orc: OrchestrationLog):
    """
    Map execution_state + classification to final release_action.
    Enforce: block / halt / escalate classifications → health_state = failed.

    Mapping:
      eligible + release              → RELEASE_ORDER
      conditional_eligible + *        → RELEASE_ORDER_WITH_CAUTION
      ineligible                      → SUPPRESS_ORDER_RELEASE
      blocked                         → BLOCK_ORDER_RELEASE
      escalate                        → TRIGGER_ESCALATION_PROTOCOL
      halt (any)                      → BLOCK_ORDER_RELEASE
    """
    section = "section_20_release_decision"

    execution_state = orc.execution_state
    classification  = orc.classification

    if execution_state == "escalate" or classification == "escalate":
        release_action = "TRIGGER_ESCALATION_PROTOCOL"
    elif execution_state == "blocked" or classification in ("block", "halt"):
        release_action = "BLOCK_ORDER_RELEASE"
    elif execution_state == "conditional_eligible":
        release_action = "RELEASE_ORDER_WITH_CAUTION"
    elif execution_state == "eligible" and classification == "release":
        release_action = "RELEASE_ORDER"
    elif execution_state == "ineligible":
        release_action = "SUPPRESS_ORDER_RELEASE"
    else:
        # Defensive fallback
        release_action = "SUPPRESS_ORDER_RELEASE"

    # Enforce health_state=failed for terminal actions
    if classification in ("block", "halt", "escalate"):
        orc.health_state = "failed"
        orc.record(section, "health_state_forced_failed", {
            "reason": f"classification={classification}"
        })

    orc.release_action = release_action
    orc.record(section, "release_decision_final", {
        "execution_state": execution_state,
        "classification":  classification,
        "release_action":  release_action,
        "health_state":    orc.health_state,
    })


# ============================================================
# HALT FINALISER — assign terminal output fields after a halt
# ============================================================

def _apply_halt_output(orc: OrchestrationLog):
    """
    When the orchestration halts mid-run, set classification, health_state,
    execution_state, and release_action to safe terminal values.
    """
    orc.classification  = orc.classification  or "halt"
    orc.execution_state = "blocked"
    orc.health_state    = "failed"
    orc.release_action  = "BLOCK_ORDER_RELEASE"
    orc.orchestration_flags["halt_applied"] = True
    orc.record("halt_finaliser", "halt_output_applied", {
        "halt_reason":  orc.halt_reason,
        "halt_section": orc.halt_section,
    })


# ============================================================
# PRIMARY ENTRY POINT
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    Master orchestration entry point.

    Parameters
    ----------
    snapshot : dict
        Must contain:
          - raw_input              : dict with snapshot_payload, route_registry,
                                     cycle_detection_store, run_id, timestamp
          - processed_request_store: list of previously seen request hashes
          - orchestration_config   : dict of constant overrides (may be empty)

    Returns
    -------
    dict
        Complete orchestration output — see module docstring for schema.
    """
    orc = OrchestrationLog()

    # --- Apply config overrides ---
    config = snapshot.get("orchestration_config") or {}
    if config:
        global ORCHESTRATION_MAX_AGE, ORCHESTRATION_MIN_READY_BLOCKS
        global HIGH_HEALTH_THRESHOLD, MEDIUM_HEALTH_THRESHOLD, TRACEABILITY_MIN_RECORDS
        if "ORCHESTRATION_MAX_AGE"         in config: ORCHESTRATION_MAX_AGE         = config["ORCHESTRATION_MAX_AGE"]
        if "ORCHESTRATION_MIN_READY_BLOCKS" in config: ORCHESTRATION_MIN_READY_BLOCKS = config["ORCHESTRATION_MIN_READY_BLOCKS"]
        if "HIGH_HEALTH_THRESHOLD"          in config: HIGH_HEALTH_THRESHOLD          = config["HIGH_HEALTH_THRESHOLD"]
        if "MEDIUM_HEALTH_THRESHOLD"        in config: MEDIUM_HEALTH_THRESHOLD        = config["MEDIUM_HEALTH_THRESHOLD"]
        if "TRACEABILITY_MIN_RECORDS"       in config: TRACEABILITY_MIN_RECORDS       = config["TRACEABILITY_MIN_RECORDS"]
        orc.record("config", "overrides_applied", list(config.keys()))

    raw_input               = snapshot.get("raw_input")
    processed_request_store = snapshot.get("processed_request_store") or []

    # ---- Section 1 ----
    _section_1_intake(raw_input, processed_request_store, orc)
    if orc.halted:
        _apply_halt_output(orc)
        return orc.to_dict()

    # ---- Section 2 ----
    _section_2_parse(raw_input, orc)
    if orc.halted:
        _apply_halt_output(orc)
        return orc.to_dict()

    # ---- Section 3 ----
    _section_3_dispatcher(orc)
    if orc.halted:
        _apply_halt_output(orc)
        return orc.to_dict()

    # ---- Section 4 ----
    _section_4_upstream_readiness(orc)
    # Non-halting section — continue regardless

    # ---- Section 5 ----
    _section_5_news(orc)

    # ---- Section 6 ----
    _section_6_social_rumor(orc)

    # ---- Section 7 ----
    _section_7_news_rumor_integration(orc)

    # ---- Section 8 ----
    _section_8_macro(orc)

    # ---- Section 9 ----
    _section_9_sentiment(orc)

    # ---- Section 10 ----
    _section_10_flow(orc)

    # ---- Section 11 ----
    _section_11_aggregation(orc)
    if orc.halted:
        _apply_halt_output(orc)
        return orc.to_dict()

    # ---- Section 12 ----
    _section_12_trade_logic(orc)
    if orc.halted:
        _apply_halt_output(orc)
        return orc.to_dict()

    # ---- Section 13 ----
    _section_13_fault_audit(orc)

    # ---- Section 14 ----
    _section_14_bias_audit(orc)

    # ---- Section 15 ----
    _section_15_validator(orc)

    # ---- Section 16 ----
    _section_16_audit_fusion(orc)

    # ---- Section 17 ----
    _section_17_specialist_reroute(orc)

    # ---- Section 18 ----
    _section_18_execution_state(orc)

    # ---- Section 19 ----
    _section_19_health_scoring(orc)

    # ---- Section 20 ----
    _section_20_release_decision(orc)

    log.info(
        "Orchestration complete | run_id=%s classification=%s execution_state=%s "
        "health=%s action=%s",
        orc.run_id,
        orc.classification,
        orc.execution_state,
        orc.health_state,
        orc.release_action,
    )

    return orc.to_dict()


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Master Orchestration Layer (7.12) — sequences all 11 agents "
                    "and emits final release classification."
    )
    parser.add_argument(
        "--snapshot", type=str, default=None,
        help="Path to a JSON file containing the orchestration snapshot dict "
             "(raw_input, processed_request_store, orchestration_config)."
    )
    parser.add_argument(
        "--run-id", type=str, default=None,
        help="Override run_id embedded in snapshot (useful for ad-hoc testing)."
    )
    args = parser.parse_args()

    if args.snapshot:
        with open(args.snapshot, "r") as fh:
            snapshot = json.load(fh)
    else:
        # Minimal smoke-test payload
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        run_id  = args.run_id or f"test-{hashlib.md5(now_iso.encode()).hexdigest()[:8]}"
        snapshot = {
            "raw_input": {
                "snapshot_payload":      {},
                "route_registry":        {},
                "cycle_detection_store": {},
                "run_id":                run_id,
                "timestamp":             now_iso,
            },
            "processed_request_store": [],
            "orchestration_config":    {},
        }

    result = run_agent(snapshot)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
