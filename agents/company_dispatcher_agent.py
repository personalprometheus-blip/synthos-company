#!/usr/bin/env python3
"""
dispatcher_agent.py — Agent 7.1
Pipeline dispatch spine. 5-gate deterministic, rule-based spine.

Gates 1-2   : Route extraction and registry validation.
Gates 3     : Cycle detection via route hash.
Gates 4-5   : Mode assignment and dispatch score calculation.
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
log = logging.getLogger("dispatcher_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION            = "1.0.0"
CYCLE_HASH_WINDOW        = 100    # number of recent route hashes retained
DISPATCH_SCORE_NORMAL    = 1.0
DISPATCH_SCORE_STAGED    = 0.75
DISPATCH_SCORE_FALLBACK  = 0.40
MAX_SNAPSHOT_AGE_MINUTES = 60

# ============================================================
# SCHEMAS
# ============================================================

INPUT_SCHEMA = {
    "parsed_input": {
        "type": "dict",
        "required": True,
        "description": "Parsed, validated input payload",
    },
    "route_registry": {
        "type": "dict",
        "required": True,
        "description": "Valid routes and their modes",
    },
    "cycle_detection_store": {
        "type": "list",
        "required": True,
        "description": "Previously seen route hashes",
    },
    "run_id": {
        "type": "str",
        "required": True,
        "description": "Unique identifier for this dispatch run",
    },
}

OUTPUT_SCHEMA = {
    "dispatch_state": {
        "type": "str",
        "values": ["valid_route", "invalid_route", "cycle_break"],
        "description": "Route validation outcome",
    },
    "final_dispatch_signal": {
        "type": "str",
        "values": ["normal_route", "staged_route", "fallback_or_triage"],
        "description": "Orchestration mode for downstream pipeline",
    },
    "dispatch_score": {
        "type": "float",
        "values": "[0.0, 1.0]",
        "description": "Confidence score for the dispatched route",
    },
    "decision_log": {
        "type": "list",
        "description": "Ordered list of gate records produced during the run",
    },
    "halted": {
        "type": "bool",
        "description": "True only when the pipeline was halted before completion",
    },
    "halt_reason": {
        "type": "str",
        "description": "Machine-readable halt reason code (only present when halted)",
    },
}

# ============================================================
# DISPATCHER DECISION LOG
# ============================================================

class DispatcherDecisionLog:
    """
    Accumulates all gate records produced during a single run_agent() run.
    Serialises to a dict that satisfies OUTPUT_SCHEMA.
    """

    def __init__(self, run_id: str):
        self.run_id    = run_id
        self.timestamp = datetime.datetime.utcnow().isoformat()
        self.records   = []

        # Halt state
        self.halted      = False
        self.halt_reason = None

        # Gate 1 — extracted routing fields
        self.route_type  = None
        self.asset_class = None
        self.mode_flag   = None

        # Gate 2 — validation outcome
        self.dispatch_state = None

        # Gate 3 — cycle detection
        self.route_hash = None

        # Gate 4 — mode assignment
        self.final_dispatch_signal = None

        # Gate 5 — scoring
        self.dispatch_score = None

    def add_record(self, gate: int, name: str, inputs: dict,
                   result: str, reason_code: str):
        self.records.append({
            "gate":        gate,
            "name":        name,
            "inputs":      inputs,
            "result":      result,
            "reason_code": reason_code,
            "ts":          datetime.datetime.utcnow().isoformat(),
        })

    def to_dict(self) -> dict:
        out = {
            "run_id":               self.run_id,
            "timestamp":            self.timestamp,
            "dispatch_state":       self.dispatch_state,
            "final_dispatch_signal":self.final_dispatch_signal,
            "dispatch_score":       self.dispatch_score,
            "route_type":           self.route_type,
            "asset_class":          self.asset_class,
            "mode_flag":            self.mode_flag,
            "route_hash":           self.route_hash,
            "decision_log":         self.records,
        }
        if self.halted:
            out["halted"]      = True
            out["halt_reason"] = self.halt_reason
        return out


# ============================================================
# GATE 1 — ROUTE EXTRACTION
# ============================================================

def gate_1_route_extraction(snapshot: dict, dlog: DispatcherDecisionLog) -> bool:
    """
    GATE 1 — ROUTE EXTRACTION  [HALTING on null parsed_input]
    Reads routing fields from parsed_input.
    Returns True to continue; False if halted.
    """
    parsed_input = snapshot.get("parsed_input")

    if parsed_input is None:
        dlog.halted      = True
        dlog.halt_reason = "reject_input"
        dlog.add_record(
            gate=1,
            name="gate_1_route_extraction",
            inputs={"parsed_input": None},
            result="halted",
            reason_code="reject_input",
        )
        log.warning("Gate 1 HALT: parsed_input is null — run_id=%s", dlog.run_id)
        return False

    route_type  = parsed_input.get("route_type")
    asset_class = parsed_input.get("asset_class")
    mode_flag   = parsed_input.get("mode_flag")

    dlog.route_type  = route_type
    dlog.asset_class = asset_class
    dlog.mode_flag   = mode_flag

    # If none of the routing fields are present, treat as invalid route
    if route_type is None and asset_class is None and mode_flag is None:
        dlog.halted         = True
        dlog.halt_reason    = "invalid_route"
        dlog.dispatch_state = "invalid_route"
        dlog.add_record(
            gate=1,
            name="gate_1_route_extraction",
            inputs={
                "route_type":  route_type,
                "asset_class": asset_class,
                "mode_flag":   mode_flag,
            },
            result="halted",
            reason_code="invalid_route",
        )
        log.warning("Gate 1 HALT: no routing fields present — run_id=%s", dlog.run_id)
        return False

    dlog.add_record(
        gate=1,
        name="gate_1_route_extraction",
        inputs={
            "route_type":  route_type,
            "asset_class": asset_class,
            "mode_flag":   mode_flag,
        },
        result="extracted",
        reason_code="ok",
    )
    log.info("Gate 1 OK: route_type=%s  asset_class=%s  mode_flag=%s",
             route_type, asset_class, mode_flag)
    return True


# ============================================================
# GATE 2 — ROUTE VALIDATION
# ============================================================

def gate_2_route_validation(snapshot: dict, dlog: DispatcherDecisionLog) -> bool:
    """
    GATE 2 — ROUTE VALIDATION  [HALTING on unknown route]
    Checks (route_type, asset_class) against route_registry.
    Returns True to continue; False if halted.
    """
    route_registry = snapshot.get("route_registry", {})
    route_key      = (dlog.route_type, dlog.asset_class)
    route_key_str  = f"{dlog.route_type}:{dlog.asset_class}"

    found = route_key_str in route_registry or route_key in route_registry

    if not found:
        dlog.halted         = True
        dlog.halt_reason    = "invalid_route"
        dlog.dispatch_state = "invalid_route"
        dlog.add_record(
            gate=2,
            name="gate_2_route_validation",
            inputs={
                "route_key":      route_key_str,
                "registry_keys":  list(route_registry.keys()),
            },
            result="halted",
            reason_code="invalid_route",
        )
        log.warning("Gate 2 HALT: route %s not in registry — run_id=%s",
                    route_key_str, dlog.run_id)
        return False

    dlog.dispatch_state = "valid_route"
    dlog.add_record(
        gate=2,
        name="gate_2_route_validation",
        inputs={
            "route_key":   route_key_str,
            "found":       True,
        },
        result="valid_route",
        reason_code="ok",
    )
    log.info("Gate 2 OK: route %s validated — run_id=%s", route_key_str, dlog.run_id)
    return True


# ============================================================
# GATE 3 — CYCLE DETECTION
# ============================================================

def gate_3_cycle_detection(snapshot: dict, dlog: DispatcherDecisionLog) -> bool:
    """
    GATE 3 — CYCLE DETECTION  [HALTING on hash collision]
    Hashes (route_type, asset_class) with SHA-256 and checks against
    cycle_detection_store.  Returns True to continue; False if halted.
    """
    cycle_store = snapshot.get("cycle_detection_store", [])

    route_tuple  = json.dumps([dlog.route_type, dlog.asset_class], sort_keys=True)
    route_hash   = hashlib.sha256(route_tuple.encode("utf-8")).hexdigest()
    dlog.route_hash = route_hash

    # Trim window — only consider the most recent CYCLE_HASH_WINDOW entries
    recent_store = cycle_store[-CYCLE_HASH_WINDOW:]

    if route_hash in recent_store:
        dlog.halted         = True
        dlog.halt_reason    = "cycle_break"
        dlog.dispatch_state = "cycle_break"
        dlog.add_record(
            gate=3,
            name="gate_3_cycle_detection",
            inputs={
                "route_hash":    route_hash,
                "store_size":    len(recent_store),
                "window":        CYCLE_HASH_WINDOW,
            },
            result="halted",
            reason_code="cycle_break",
        )
        log.warning("Gate 3 HALT: routing cycle detected hash=%s — run_id=%s",
                    route_hash[:12], dlog.run_id)
        return False

    dlog.add_record(
        gate=3,
        name="gate_3_cycle_detection",
        inputs={
            "route_hash":  route_hash,
            "store_size":  len(recent_store),
            "window":      CYCLE_HASH_WINDOW,
        },
        result="no_cycle",
        reason_code="ok",
    )
    log.info("Gate 3 OK: no cycle detected hash=%s — run_id=%s",
             route_hash[:12], dlog.run_id)
    return True


# ============================================================
# GATE 4 — MODE ASSIGNMENT
# ============================================================

def gate_4_mode_assignment(snapshot: dict, dlog: DispatcherDecisionLog) -> None:
    """
    GATE 4 — MODE ASSIGNMENT  [NON-HALTING]
    Maps the validated route to a final_dispatch_signal using the
    mode entry in route_registry.  Unknown modes fall back to fallback_or_triage.
    """
    route_registry = snapshot.get("route_registry", {})
    route_key_str  = f"{dlog.route_type}:{dlog.asset_class}"

    # Support both string-keyed and tuple-keyed registries
    registry_entry = route_registry.get(route_key_str) or route_registry.get(
        (dlog.route_type, dlog.asset_class), {}
    )

    mode = registry_entry.get("mode") if isinstance(registry_entry, dict) else registry_entry

    MODE_MAP = {
        "normal":   "normal_route",
        "staged":   "staged_route",
        "fallback": "fallback_or_triage",
        "triage":   "fallback_or_triage",
    }

    signal = MODE_MAP.get(str(mode).lower() if mode else "", "fallback_or_triage")
    dlog.final_dispatch_signal = signal

    dlog.add_record(
        gate=4,
        name="gate_4_mode_assignment",
        inputs={
            "route_key":        route_key_str,
            "registry_mode":    mode,
        },
        result=signal,
        reason_code="ok",
    )
    log.info("Gate 4 OK: mode=%s → signal=%s — run_id=%s", mode, signal, dlog.run_id)


# ============================================================
# GATE 5 — SCORE CALCULATION
# ============================================================

def gate_5_score_calculation(snapshot: dict, dlog: DispatcherDecisionLog) -> None:
    """
    GATE 5 — SCORE CALCULATION  [NON-HALTING]
    Computes dispatch_score from route confidence based on assigned signal.
    """
    signal_score_map = {
        "normal_route":      DISPATCH_SCORE_NORMAL,
        "staged_route":      DISPATCH_SCORE_STAGED,
        "fallback_or_triage":DISPATCH_SCORE_FALLBACK,
    }

    score = signal_score_map.get(dlog.final_dispatch_signal, DISPATCH_SCORE_FALLBACK)
    dlog.dispatch_score = score

    dlog.add_record(
        gate=5,
        name="gate_5_score_calculation",
        inputs={
            "final_dispatch_signal": dlog.final_dispatch_signal,
        },
        result=str(score),
        reason_code="ok",
    )
    log.info("Gate 5 OK: dispatch_score=%.2f — run_id=%s", score, dlog.run_id)


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    Orchestrates the 5-gate dispatcher pipeline.

    Args:
        snapshot: dict with keys:
            run_id                  — unique identifier for this dispatch run
            parsed_input            — parsed, validated input payload (dict)
            route_registry          — valid routes mapped to their modes (dict)
            cycle_detection_store   — list of previously seen route hashes

    Returns:
        Complete decision log dict produced by DispatcherDecisionLog.to_dict().
    """
    run_id = snapshot.get("run_id", "unknown")
    dlog   = DispatcherDecisionLog(run_id)

    # Gate 1: Route extraction (halting on null input or missing fields)
    if not gate_1_route_extraction(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Route validation (halting on unknown route)
    if not gate_2_route_validation(snapshot, dlog):
        return dlog.to_dict()

    # Gate 3: Cycle detection (halting on hash collision)
    if not gate_3_cycle_detection(snapshot, dlog):
        return dlog.to_dict()

    # Gate 4: Mode assignment (non-halting)
    gate_4_mode_assignment(snapshot, dlog)

    # Gate 5: Score calculation (non-halting)
    gate_5_score_calculation(snapshot, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_dispatch_log(result: dict):
    """Writes the dispatcher decision log to the database event log."""
    _db.log_event(
        event_type="dispatcher_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity dispatch conditions.
    """
    run_id      = result.get("run_id")
    halt_reason = result.get("halt_reason")
    dispatch    = result.get("final_dispatch_signal")

    if halt_reason == "cycle_break":
        _db.post_suggestion(
            agent="dispatcher_agent",
            classification="cycle_break",
            risk_level="HIGH",
            payload=result,
            note="Routing cycle detected — pipeline halted before any agent ran. Investigate cycle_detection_store.",
        )
        log.warning("ESCALATE: cycle_break for run_id=%s", run_id)

    elif halt_reason == "invalid_route":
        _db.post_suggestion(
            agent="dispatcher_agent",
            classification="invalid_route",
            risk_level="MEDIUM",
            payload=result,
            note="Invalid route submitted — not present in route_registry. Verify upstream input.",
        )
        log.warning("ESCALATE: invalid_route for run_id=%s", run_id)

    elif halt_reason == "reject_input":
        _db.post_suggestion(
            agent="dispatcher_agent",
            classification="reject_input",
            risk_level="HIGH",
            payload=result,
            note="Null parsed_input received — dispatcher rejected before gate evaluation.",
        )
        log.warning("ESCALATE: reject_input for run_id=%s", run_id)

    elif dispatch == "fallback_or_triage":
        _db.post_suggestion(
            agent="dispatcher_agent",
            classification="fallback_or_triage",
            risk_level="MEDIUM",
            payload=result,
            note="Route dispatched to fallback/triage mode — review route_registry mode assignment.",
        )
        log.warning("ESCALATE: fallback_or_triage dispatch for run_id=%s", run_id)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Dispatcher Agent — 5-gate pipeline routing spine"
    )
    parser.add_argument("--run-id", default="cli-run",
                        help="Run ID for this dispatch")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Minimal test snapshot — valid equity/spot route, normal mode, no cycle history
    test_snapshot = {
        "run_id":    args.run_id,
        "parsed_input": {
            "route_type":  "equity",
            "asset_class": "spot",
            "mode_flag":   "live",
        },
        "route_registry": {
            "equity:spot":   {"mode": "normal"},
            "equity:futures":{"mode": "staged"},
            "fx:spot":       {"mode": "normal"},
            "crypto:spot":   {"mode": "fallback"},
            "unknown:unknown":{"mode": "triage"},
        },
        "cycle_detection_store": [],
    }

    log.info("Running dispatcher agent for run_id: %s", args.run_id)
    result = run_agent(test_snapshot)
    write_dispatch_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"DISPATCHER AGENT — run_id: {result.get('run_id')}")
        print(f"{'='*72}")
        print(f"Dispatch State         : {result.get('dispatch_state')}")
        print(f"Final Dispatch Signal  : {result.get('final_dispatch_signal')}")
        print(f"Dispatch Score         : {result.get('dispatch_score')}")
        print(f"Route Type             : {result.get('route_type')}")
        print(f"Asset Class            : {result.get('asset_class')}")
        print(f"Mode Flag              : {result.get('mode_flag')}")
        print(f"Route Hash             : {result.get('route_hash')}")
        if result.get("halted"):
            print(f"Halted                 : {result.get('halted')}")
            print(f"Halt Reason            : {result.get('halt_reason')}")
        print(f"\nDecision Log ({len(result.get('decision_log', []))} records):")
        for rec in result.get("decision_log", []):
            print(f"  Gate {rec['gate']} [{rec['name']}]  result={rec['result']}  "
                  f"reason={rec['reason_code']}  ts={rec['ts']}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
