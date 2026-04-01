#!/usr/bin/env python3
"""
trade_logic_agent.py — Agent 7.8
Trade logic classification spine. 10-gate deterministic, rule-based spine.

Produces a classified trade decision from the aggregator's market state and
the assigned trade bias. Operates exclusively on post-synthesis state —
does not re-interpret raw or upstream agent data.

Gates 1-2   : System validation and market state extraction.
Gates 3-4   : Override checks (block state, panic state).
Gates 5-6   : Bias resolution and signal alignment.
Gates 7-8   : Confidence check and trade parameter derivation.
Gates 9-10  : Rationale assembly and output finalization.
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
log = logging.getLogger("trade_logic_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION = "1.0.0"
MAX_SNAPSHOT_AGE_MINUTES = 60

# Bias-to-decision mapping table (primary driver)
BIAS_DECISION_MAP = {
    "long_bias":    {"primary": "long",   "fallback": "hold"},
    "short_bias":   {"primary": "short",  "fallback": "hold"},
    "neutral_bias": {"primary": "hold",   "fallback": "hold"},
    "exit_bias":    {"primary": "exit",   "fallback": "exit"},
    "hold_bias":    {"primary": "hold",   "fallback": "hold"},
}
DEFAULT_BIAS_DECISION = "hold"
UNRECOGNIZED_BIAS_REASON = "unrecognized_bias"

# State override thresholds
PANIC_STATES = {"panic_override", "deleveraging_override"}
BLOCK_STATES = {"blocked_override"}
STRONG_RISK_OFF_THRESHOLD = -0.70   # signal below this forces no_trade
HIGH_CONFIDENCE_THRESHOLD = 0.70
LOW_CONFIDENCE_THRESHOLD  = 0.35

# Duration classification
DURATION_SHORT_VOL_THRESHOLD  = 0.25    # high vol → intraday
DURATION_MEDIUM_VOL_THRESHOLD = 0.15    # mid vol → swing
# else → positional

# Conviction levels
CONVICTION_HIGH_CONFIDENCE = 0.70
CONVICTION_LOW_CONFIDENCE  = 0.40


# ============================================================
# TRADE DECISION LOG
# ============================================================

class TradeDecisionLog:
    """
    Accumulates all gate records and decision states produced during a
    single run_agent() run. Serialises to dict for downstream consumption.
    """

    def __init__(self, run_id: str, timestamp: str):
        self.run_id    = run_id
        self.timestamp = timestamp
        self.records   = []

        # Gate 1 — system validation
        self.halted      = False
        self.halt_reason = None

        # Gate 2 — market state read
        self.market_state_read = {
            "final_market_state":        None,
            "final_market_state_signal": None,
            "aggregate_confidence":      None,
            "downstream_route":          None,
        }

        # Gate 3 — block check
        self.block_check = None        # "blocked" | "clear"

        # Gate 4 — panic check
        self.panic_check = None        # "panic_override_applied" | "clear"

        # Gate 5 — bias resolution
        self.bias_input    = None
        self.bias_resolved = None      # resolved decision string

        # Gate 6 — signal alignment
        self.signal_alignment = None   # "aligned" | "misaligned" | "neutral"

        # Gate 7 — confidence check
        self.confidence_check = None   # "sufficient" | "low_confidence_override"

        # Gate 8 — trade parameters
        self.trade_parameters = None   # {entry_type, duration_class, conviction_level, risk_note}

        # Gate 9 — rationale assembly
        self.decision_rationale = []

        # Gate 10 — output
        self.trade_decision = None

        # Accumulated warnings across gates
        self.warning_states = []

    def record(self, gate: int, name: str, inputs: dict, result: str, reason_code: str):
        self.records.append({
            "gate":        gate,
            "name":        name,
            "inputs":      inputs,
            "result":      result,
            "reason_code": reason_code,
            "ts":          datetime.datetime.utcnow().isoformat(),
        })

    def halt(self, gate: int, reason_code: str, inputs: dict):
        self.halted      = True
        self.halt_reason = reason_code
        self.record(gate, "HALT", inputs, "halted", reason_code)

    def to_dict(self) -> dict:
        return {
            "run_id":               self.run_id,
            "timestamp":            self.timestamp,
            "agent_version":        AGENT_VERSION,
            "halted":               self.halted,
            "halt_reason":          self.halt_reason,
            "bias_input":           self.bias_input,
            "market_state_read":    self.market_state_read,
            "block_check":          self.block_check,
            "panic_check":          self.panic_check,
            "bias_resolved":        self.bias_resolved,
            "signal_alignment":     self.signal_alignment,
            "confidence_check":     self.confidence_check,
            "trade_decision":       self.trade_decision,
            "trade_parameters":     self.trade_parameters,
            "decision_rationale":   self.decision_rationale,
            "warning_states":       self.warning_states,
            "decision_log":         self.records,
        }


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: TradeDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates required inputs and snapshot freshness before any
    trade logic processing begins.
    Returns True if the run should continue; False if halted.
    """
    market_state = snapshot.get("market_state")
    market_data  = snapshot.get("market_data")

    # Check 1: market_state must be present and non-null
    if market_state is None:
        dlog.halt(1, "null_market_state",
                  {"market_state": None})
        return False

    # Check 2: market_data must be present and non-null
    if market_data is None:
        dlog.halt(1, "null_market_data",
                  {"market_data": None})
        return False

    # Check 3: Timestamp present
    timestamp_str = snapshot.get("timestamp")
    if timestamp_str is None:
        dlog.halt(1, "null_timestamp", {"timestamp": None})
        return False

    # Check 4: Timestamp freshness
    try:
        snap_time   = datetime.datetime.fromisoformat(timestamp_str)
        age_minutes = (datetime.datetime.utcnow() - snap_time).total_seconds() / 60.0
        if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
            dlog.halt(1, "stale_snapshot",
                      {"timestamp": timestamp_str,
                       "age_minutes": round(age_minutes, 2),
                       "max_age_minutes": MAX_SNAPSHOT_AGE_MINUTES})
            return False
    except (ValueError, TypeError) as exc:
        dlog.halt(1, "invalid_timestamp_format",
                  {"timestamp": timestamp_str, "error": str(exc)})
        return False

    dlog.record(1, "system_gate",
                {"market_state_present": True,
                 "market_data_present": True,
                 "timestamp": timestamp_str},
                "passed", "system_gate_passed")
    return True


# ============================================================
# GATE 2 — MARKET STATE READ
# ============================================================

def gate_2_market_state_read(snapshot: dict, dlog: TradeDecisionLog):
    """
    GATE 2 — MARKET STATE READ
    Extracts final_market_state, final_market_state_signal, aggregate_confidence,
    and downstream_route from the market_state payload.
    """
    market_state = snapshot.get("market_state", {})

    final_market_state        = market_state.get("final_market_state")
    final_market_state_signal = market_state.get("final_market_state_signal")
    aggregate_confidence      = market_state.get("aggregate_confidence")
    downstream_route          = market_state.get("downstream_route")

    dlog.market_state_read = {
        "final_market_state":        final_market_state,
        "final_market_state_signal": final_market_state_signal,
        "aggregate_confidence":      aggregate_confidence,
        "downstream_route":          downstream_route,
    }

    dlog.record(2, "market_state_read",
                {"final_market_state":        final_market_state,
                 "final_market_state_signal": final_market_state_signal,
                 "aggregate_confidence":      aggregate_confidence,
                 "downstream_route":          downstream_route},
                "read", "market_state_extracted")


# ============================================================
# GATE 3 — BLOCK CHECK
# ============================================================

def gate_3_block_check(dlog: TradeDecisionLog) -> bool:
    """
    GATE 3 — BLOCK CHECK  [EARLY EXIT — no_trade]
    If final_market_state is in BLOCK_STATES, set trade_decision to no_trade
    and append reason code. Returns True if blocked (pipeline should stop),
    False if clear.
    """
    final_market_state = dlog.market_state_read["final_market_state"]

    if final_market_state in BLOCK_STATES:
        dlog.block_check    = "blocked"
        dlog.trade_decision = "no_trade"
        dlog.decision_rationale.append("blocked_by_market_state")
        dlog.record(3, "block_check",
                    {"final_market_state": final_market_state},
                    "blocked", "blocked_by_market_state")
        return True

    dlog.block_check = "clear"
    dlog.record(3, "block_check",
                {"final_market_state": final_market_state},
                "clear", "block_check_clear")
    return False


# ============================================================
# GATE 4 — PANIC CHECK
# ============================================================

def gate_4_panic_check(snapshot: dict, dlog: TradeDecisionLog):
    """
    GATE 4 — PANIC CHECK
    If final_market_state is in PANIC_STATES, override the incoming bias to
    exit_bias regardless of orchestration layer assignment.
    Appends warning: panic_state_active.
    """
    final_market_state = dlog.market_state_read["final_market_state"]

    if final_market_state in PANIC_STATES:
        original_bias = snapshot.get("bias")
        snapshot["bias"] = "exit_bias"
        dlog.panic_check = "panic_override_applied"
        dlog.warning_states.append("panic_state_active")
        dlog.decision_rationale.append("panic_state_active")
        dlog.record(4, "panic_check",
                    {"final_market_state": final_market_state,
                     "original_bias": original_bias,
                     "override_bias": "exit_bias"},
                    "panic_override_applied", "panic_state_active")
    else:
        dlog.panic_check = "clear"
        dlog.record(4, "panic_check",
                    {"final_market_state": final_market_state},
                    "clear", "panic_check_clear")


# ============================================================
# GATE 5 — BIAS RESOLUTION
# ============================================================

def gate_5_bias_resolution(snapshot: dict, dlog: TradeDecisionLog) -> str:
    """
    GATE 5 — BIAS RESOLUTION
    Resolves the incoming bias to a primary trade decision using
    BIAS_DECISION_MAP. Unrecognized biases fall back to DEFAULT_BIAS_DECISION
    with reason code UNRECOGNIZED_BIAS_REASON.
    Returns the resolved decision string.
    """
    bias = snapshot.get("bias")
    dlog.bias_input = bias

    if bias in BIAS_DECISION_MAP:
        resolved = BIAS_DECISION_MAP[bias]["primary"]
        dlog.bias_resolved = resolved
        dlog.decision_rationale.append(f"bias_resolved:{bias}")
        dlog.record(5, "bias_resolution",
                    {"bias": bias, "resolved_decision": resolved},
                    "resolved", f"bias_resolved:{bias}")
    else:
        resolved = DEFAULT_BIAS_DECISION
        dlog.bias_resolved = resolved
        dlog.decision_rationale.append(UNRECOGNIZED_BIAS_REASON)
        dlog.record(5, "bias_resolution",
                    {"bias": bias, "resolved_decision": resolved},
                    "default_applied", UNRECOGNIZED_BIAS_REASON)

    return resolved


# ============================================================
# GATE 6 — SIGNAL ALIGNMENT
# ============================================================

def gate_6_signal_alignment(resolved_decision: str, dlog: TradeDecisionLog) -> str:
    """
    GATE 6 — SIGNAL ALIGNMENT
    Checks whether final_market_state_signal aligns with the resolved bias
    direction. Applies a confidence discount when misaligned.
    Returns the (possibly adjusted) trade decision.
    """
    signal     = dlog.market_state_read["final_market_state_signal"]
    confidence = dlog.market_state_read["aggregate_confidence"]

    # Directional alignment mapping: which signal values support which decisions
    LONG_ALIGNED_SIGNALS  = {"risk_on", "bullish", "expansion", "strong_expansion",
                              "recovery", "growth_positive"}
    SHORT_ALIGNED_SIGNALS = {"risk_off", "bearish", "contraction", "strong_contraction",
                              "defensive", "deleveraging"}
    NEUTRAL_SIGNALS       = {"neutral", "transitional", "indeterminate", None}

    # Determine alignment
    if resolved_decision in ("long",):
        if signal in LONG_ALIGNED_SIGNALS:
            alignment = "aligned"
        elif signal in SHORT_ALIGNED_SIGNALS:
            alignment = "misaligned"
        else:
            alignment = "neutral"
    elif resolved_decision in ("short",):
        if signal in SHORT_ALIGNED_SIGNALS:
            alignment = "aligned"
        elif signal in LONG_ALIGNED_SIGNALS:
            alignment = "misaligned"
        else:
            alignment = "neutral"
    else:
        # hold, exit, no_trade — no directional alignment check needed
        alignment = "neutral"

    dlog.signal_alignment = alignment

    # Apply confidence discount on misalignment
    if alignment == "misaligned" and confidence is not None:
        discounted = max(0.0, confidence - 0.15)
        dlog.market_state_read["aggregate_confidence"] = discounted
        dlog.decision_rationale.append("signal_misalignment_confidence_discount")
        dlog.record(6, "signal_alignment",
                    {"signal": signal, "resolved_decision": resolved_decision,
                     "alignment": alignment,
                     "confidence_before": confidence,
                     "confidence_after": discounted},
                    "misaligned", "signal_misalignment_confidence_discount")
    else:
        dlog.record(6, "signal_alignment",
                    {"signal": signal, "resolved_decision": resolved_decision,
                     "alignment": alignment},
                    alignment, f"signal_alignment:{alignment}")

    # Strong risk-off override: if signal is deeply negative, force no_trade on long
    if (signal is not None
            and isinstance(signal, (int, float))
            and signal < STRONG_RISK_OFF_THRESHOLD
            and resolved_decision == "long"):
        resolved_decision = "no_trade"
        dlog.decision_rationale.append("strong_risk_off_override")
        dlog.record(6, "signal_alignment_override",
                    {"signal": signal,
                     "threshold": STRONG_RISK_OFF_THRESHOLD},
                    "no_trade", "strong_risk_off_override")

    return resolved_decision


# ============================================================
# GATE 7 — CONFIDENCE CHECK
# ============================================================

def gate_7_confidence_check(resolved_decision: str, snapshot: dict,
                             dlog: TradeDecisionLog) -> str:
    """
    GATE 7 — CONFIDENCE CHECK
    If aggregate_confidence falls below LOW_CONFIDENCE_THRESHOLD, override
    the resolved decision to the bias fallback (hold for most, exit for exit_bias).
    Appends reason: low_confidence_override.
    """
    confidence = dlog.market_state_read["aggregate_confidence"]
    bias       = snapshot.get("bias")

    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        # Use bias fallback when available, else default to hold
        if bias in BIAS_DECISION_MAP:
            fallback = BIAS_DECISION_MAP[bias]["fallback"]
        else:
            fallback = "hold"
        resolved_decision = fallback
        dlog.confidence_check = "low_confidence_override"
        dlog.decision_rationale.append("low_confidence_override")
        dlog.record(7, "confidence_check",
                    {"aggregate_confidence": confidence,
                     "threshold": LOW_CONFIDENCE_THRESHOLD,
                     "fallback_decision": fallback},
                    "low_confidence_override", "low_confidence_override")
    else:
        dlog.confidence_check = "sufficient"
        dlog.record(7, "confidence_check",
                    {"aggregate_confidence": confidence,
                     "threshold": LOW_CONFIDENCE_THRESHOLD},
                    "sufficient", "confidence_sufficient")

    return resolved_decision


# ============================================================
# GATE 8 — TRADE PARAMETERS
# ============================================================

def gate_8_trade_parameters(resolved_decision: str, snapshot: dict,
                              dlog: TradeDecisionLog):
    """
    GATE 8 — TRADE PARAMETERS
    Derives entry_type, duration_class, conviction_level, and risk_note
    from market data and confidence level.
    """
    market_data  = snapshot.get("market_data", {})
    confidence   = dlog.market_state_read["aggregate_confidence"]
    vol          = market_data.get("realized_vol_20d")
    vix          = market_data.get("vix_level")
    final_state  = dlog.market_state_read["final_market_state"]

    # Entry type: use limit orders when confidence is high; market otherwise
    if confidence is not None and confidence >= HIGH_CONFIDENCE_THRESHOLD:
        entry_type = "limit"
    else:
        entry_type = "market"

    # Duration class from realized vol
    if vol is not None:
        if vol >= DURATION_SHORT_VOL_THRESHOLD:
            duration_class = "intraday"
        elif vol >= DURATION_MEDIUM_VOL_THRESHOLD:
            duration_class = "swing"
        else:
            duration_class = "positional"
    else:
        # Fallback to VIX-derived heuristic when vol unavailable
        if vix is not None and vix >= 25.0:
            duration_class = "intraday"
        elif vix is not None and vix >= 15.0:
            duration_class = "swing"
        else:
            duration_class = "positional"

    # Conviction level from confidence
    if confidence is not None and confidence >= CONVICTION_HIGH_CONFIDENCE:
        conviction_level = "high"
    elif confidence is not None and confidence >= CONVICTION_LOW_CONFIDENCE:
        conviction_level = "medium"
    else:
        conviction_level = "low"

    # Risk note: brief contextual annotation
    risk_notes = []
    if final_state in PANIC_STATES:
        risk_notes.append("panic_state_context")
    if vol is not None and vol >= DURATION_SHORT_VOL_THRESHOLD:
        risk_notes.append("elevated_realized_vol")
    if vix is not None and vix >= 30.0:
        risk_notes.append("elevated_vix")
    if resolved_decision in ("exit", "no_trade"):
        risk_notes.append("defensive_posture")
    risk_note = "|".join(risk_notes) if risk_notes else "standard"

    dlog.trade_parameters = {
        "entry_type":      entry_type,
        "duration_class":  duration_class,
        "conviction_level": conviction_level,
        "risk_note":       risk_note,
    }

    dlog.record(8, "trade_parameters",
                {"resolved_decision": resolved_decision,
                 "confidence": confidence,
                 "realized_vol_20d": vol,
                 "vix_level": vix,
                 "entry_type": entry_type,
                 "duration_class": duration_class,
                 "conviction_level": conviction_level,
                 "risk_note": risk_note},
                "derived", "trade_parameters_derived")


# ============================================================
# GATE 9 — RATIONALE ASSEMBLY
# ============================================================

def gate_9_rationale_assembly(resolved_decision: str, dlog: TradeDecisionLog):
    """
    GATE 9 — RATIONALE ASSEMBLY
    Finalises the decision_rationale list, ensuring all accumulated reason
    codes are present and adding a terminal decision code.
    """
    # Append final decision as the closing reason code
    dlog.decision_rationale.append(f"final_decision:{resolved_decision}")

    # Append confidence band annotation
    confidence = dlog.market_state_read["aggregate_confidence"]
    if confidence is not None:
        if confidence >= HIGH_CONFIDENCE_THRESHOLD:
            dlog.decision_rationale.append("confidence_band:high")
        elif confidence >= LOW_CONFIDENCE_THRESHOLD:
            dlog.decision_rationale.append("confidence_band:medium")
        else:
            dlog.decision_rationale.append("confidence_band:low")

    dlog.record(9, "rationale_assembly",
                {"rationale_count": len(dlog.decision_rationale),
                 "rationale": list(dlog.decision_rationale)},
                "assembled", "rationale_assembled")


# ============================================================
# GATE 10 — OUTPUT
# ============================================================

def gate_10_output(resolved_decision: str, dlog: TradeDecisionLog):
    """
    GATE 10 — OUTPUT
    Sets the final trade_decision on the log object.
    """
    dlog.trade_decision = resolved_decision
    dlog.record(10, "output",
                {"trade_decision": resolved_decision,
                 "trade_parameters": dlog.trade_parameters,
                 "decision_rationale": list(dlog.decision_rationale)},
                "finalized", "output_finalized")


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_trade_log(result: dict):
    """Writes the trade decision log to the database event log."""
    _db.log_event(
        event_type="trade_logic_decision",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity trade decisions
    (panic overrides, blocked states, or low-conviction exits).
    """
    decision       = result.get("trade_decision")
    warnings       = result.get("warning_states", [])
    run_id         = result.get("run_id")
    rationale      = result.get("decision_rationale", [])

    if "panic_state_active" in warnings:
        _db.post_suggestion(
            agent="trade_logic_agent",
            classification="panic_override",
            risk_level="HIGH",
            payload=result,
            note="Panic state detected — bias overridden to exit_bias. Immediate review required.",
        )
        log.warning("ESCALATE: panic_override for run_id %s", run_id)

    elif decision == "no_trade" and "blocked_by_market_state" in rationale:
        _db.post_suggestion(
            agent="trade_logic_agent",
            classification="blocked_trade",
            risk_level="MEDIUM",
            payload=result,
            note="Trade blocked by market state override. Review downstream_route.",
        )
        log.warning("ESCALATE: blocked_trade for run_id %s", run_id)

    elif decision == "exit" and "low_confidence_override" in rationale:
        _db.post_suggestion(
            agent="trade_logic_agent",
            classification="low_confidence_exit",
            risk_level="MEDIUM",
            payload=result,
            note="Exit decision triggered by low confidence fallback. Review aggregate_confidence.",
        )
        log.warning("ESCALATE: low_confidence_exit for run_id %s", run_id)


# ============================================================
# MAIN ORCHESTRATOR
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    Orchestrates the 10-gate trade logic classification pipeline.

    Args:
        snapshot: dict with keys:
            market_state  — Master Market-State Aggregator output dict
            market_data   — Normalized market data dict
            bias          — Trade bias string from orchestration layer
            run_id        — Identifier for this run
            timestamp     — ISO UTC timestamp

    Returns:
        dict with keys: trade_decision, trade_parameters, decision_rationale,
        decision_log, and full TradeDecisionLog fields.
    """
    run_id    = snapshot.get("run_id", "unknown")
    timestamp = snapshot.get("timestamp", datetime.datetime.utcnow().isoformat())
    dlog      = TradeDecisionLog(run_id, timestamp)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        # Null market_state produces specific structured null output
        if dlog.halt_reason == "null_market_state":
            return {
                "trade_decision":    None,
                "trade_parameters":  None,
                "decision_rationale": ["null_market_state"],
                "decision_log":      dlog.records,
                "halted":            True,
                "halt_reason":       "null_market_state",
            }
        # Other gate-1 halts (stale_snapshot, null_market_data, etc.)
        result = dlog.to_dict()
        result["trade_decision"]    = None
        result["trade_parameters"]  = None
        result["decision_rationale"] = [dlog.halt_reason]
        return result

    # Gate 2: Market state read
    gate_2_market_state_read(snapshot, dlog)

    # Gate 3: Block check (early exit to no_trade)
    if gate_3_block_check(dlog):
        gate_10_output("no_trade", dlog)
        result = dlog.to_dict()
        write_trade_log(result)
        escalate_if_needed(result)
        return result

    # Gate 4: Panic check (may override bias in snapshot)
    gate_4_panic_check(snapshot, dlog)

    # Gate 5: Bias resolution
    resolved_decision = gate_5_bias_resolution(snapshot, dlog)

    # Gate 6: Signal alignment (may adjust confidence or force no_trade)
    resolved_decision = gate_6_signal_alignment(resolved_decision, dlog)

    # Gate 7: Confidence check (may override to fallback)
    resolved_decision = gate_7_confidence_check(resolved_decision, snapshot, dlog)

    # Gate 8: Trade parameter derivation
    gate_8_trade_parameters(resolved_decision, snapshot, dlog)

    # Gate 9: Rationale assembly
    gate_9_rationale_assembly(resolved_decision, dlog)

    # Gate 10: Output finalization
    gate_10_output(resolved_decision, dlog)

    result = dlog.to_dict()
    write_trade_log(result)
    escalate_if_needed(result)
    return result


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Trade Logic Agent 7.8 — 10-gate trade decision classification spine"
    )
    parser.add_argument("--run-id", default="cli-run",
                        help="Run ID for this agent invocation")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Minimal test snapshot — neutral market state, moderate confidence, long bias
    test_snapshot = {
        "run_id":    args.run_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "bias":      "long_bias",
        "market_state": {
            "final_market_state":        "risk_on_regime",
            "final_market_state_signal": "risk_on",
            "aggregate_confidence":      0.72,
            "downstream_route":          "trade_logic",
        },
        "market_data": {
            "price_return_1d":  0.008,
            "realized_vol_20d": 0.18,
            "vix_level":        16.5,
        },
    }

    log.info("Running trade logic agent for run_id: %s", args.run_id)
    result = run_agent(test_snapshot)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"TRADE LOGIC AGENT 7.8 — run_id: {result.get('run_id')}")
        print(f"Agent Version        : {result.get('agent_version')}")
        print(f"{'='*72}")
        print(f"Trade Decision       : {result.get('trade_decision')}")
        params = result.get("trade_parameters") or {}
        print(f"Entry Type           : {params.get('entry_type')}")
        print(f"Duration Class       : {params.get('duration_class')}")
        print(f"Conviction Level     : {params.get('conviction_level')}")
        print(f"Risk Note            : {params.get('risk_note')}")
        print(f"\nDecision Rationale   : {result.get('decision_rationale')}")
        print(f"Warning States       : {result.get('warning_states')}")
        msr = result.get("market_state_read", {})
        print(f"\nMarket State         : {msr.get('final_market_state')}")
        print(f"Market Signal        : {msr.get('final_market_state_signal')}")
        print(f"Aggregate Confidence : {msr.get('aggregate_confidence')}")
        print(f"Downstream Route     : {msr.get('downstream_route')}")
        print(f"\nBias Input           : {result.get('bias_input')}")
        print(f"Bias Resolved        : {result.get('bias_resolved')}")
        print(f"Signal Alignment     : {result.get('signal_alignment')}")
        print(f"Confidence Check     : {result.get('confidence_check')}")
        print(f"Block Check          : {result.get('block_check')}")
        print(f"Panic Check          : {result.get('panic_check')}")
        print(f"Halted               : {result.get('halted')}  "
              f"Halt Reason: {result.get('halt_reason')}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
