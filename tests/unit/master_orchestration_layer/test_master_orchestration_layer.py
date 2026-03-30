"""
Unit tests for master_orchestration_layer.run_agent().

All 11 upstream agent callables (run_dispatcher, run_news, run_social_rumor,
run_sentiment, run_macro, run_flow, run_aggregator, run_trade, run_fault,
run_bias, run_validator) are patched at the module level so no real agents
execute.  DB I/O is suppressed by conftest.patch_db_helpers.
"""

import sys
import os
import datetime

import pytest
from unittest.mock import patch, MagicMock

# Ensure the tests root is importable (mirrors the pattern in test_dispatcher_agent.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import master_orchestration_layer as _mol

# ============================================================
# SNAPSHOT / OUTPUT FACTORIES
# ============================================================

def _make_dispatcher_output(state="valid_route", signal="normal_route"):
    return {
        "dispatch_state": state,
        "final_dispatch_signal": signal,
        "dispatch_score": 1.0,
        "decision_log": [],
        "halted": False,
    }


def _make_agent_output(classification="pass", **extras):
    return {"classification": classification, "decision_log": [], **extras}


def _make_market_state(state="mild_risk_on", signal=0.4, confidence=0.75):
    return {
        "final_market_state": state,
        "final_market_state_signal": signal,
        "aggregate_confidence": confidence,
        "downstream_route": "long_bias",
        "decision_log": [],
        "halted": False,
    }


def _make_trade_output():
    return {
        "trade_decision": "long",
        "trade_parameters": {"entry_type": "limit"},
        "decision_rationale": ["long_bias"],
        "decision_log": [],
    }


def _snapshot():
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "raw_input": {
            "snapshot_id": "orch-test",
            "run_id": "orch-test-001",
            "timestamp": ts,
            "route_registry": {"equity:single_stock": "normal"},
            "cycle_detection_store": [],
            "snapshot_payload": {},
        },
        "processed_request_store": [],
        "orchestration_config": {},
    }


# ============================================================
# PATCH CONTEXT MANAGER — replaces all 11 run_* callables
# ============================================================

_MOL_PATH = "master_orchestration_layer"

def _all_agents_patch(
    dispatcher_out=None,
    news_out=None,
    social_out=None,
    sentiment_out=None,
    macro_out=None,
    flow_out=None,
    aggregator_out=None,
    trade_out=None,
    fault_out=None,
    bias_out=None,
    validator_out=None,
):
    """
    Return a list of patch() objects that replace every module-level run_*
    callable in master_orchestration_layer with a fake that returns the
    given output dict (or a sensible default).
    """
    dispatcher_out  = dispatcher_out  or _make_dispatcher_output()
    news_out        = news_out        or _make_agent_output("confirmed_event",
                                            overall_confidence=0.80,
                                            confirmation_state="confirmed")
    social_out      = social_out      or _make_agent_output("noise",
                                            upgraded_to_confirmed=False,
                                            rumor_confidence=0.3)
    sentiment_out   = sentiment_out   or _make_agent_output("bullish",
                                            sentiment_score=0.6)
    macro_out       = macro_out       or _make_agent_output("expansion",
                                            macro_score=0.55)
    flow_out        = flow_out        or _make_agent_output("net_long",
                                            flow_score=0.5)
    aggregator_out  = aggregator_out  or _make_market_state()
    trade_out       = trade_out       or _make_trade_output()
    fault_out       = fault_out       or _make_agent_output("clean",
                                            fault_flags=[])
    bias_out        = bias_out        or _make_agent_output("unbiased",
                                            bias_flags=[])
    validator_out   = validator_out   or _make_agent_output("valid",
                                            audit_passed=True)

    return [
        patch(f"{_MOL_PATH}.run_dispatcher",  return_value=dispatcher_out),
        patch(f"{_MOL_PATH}.run_news",         return_value=news_out),
        patch(f"{_MOL_PATH}.run_social_rumor", return_value=social_out),
        patch(f"{_MOL_PATH}.run_sentiment",    return_value=sentiment_out),
        patch(f"{_MOL_PATH}.run_macro",        return_value=macro_out),
        patch(f"{_MOL_PATH}.run_flow",         return_value=flow_out),
        patch(f"{_MOL_PATH}.run_aggregator",   return_value=aggregator_out),
        patch(f"{_MOL_PATH}.run_trade",        return_value=trade_out),
        patch(f"{_MOL_PATH}.run_fault",        return_value=fault_out),
        patch(f"{_MOL_PATH}.run_bias",         return_value=bias_out),
        patch(f"{_MOL_PATH}.run_validator",    return_value=validator_out),
    ]


def _run_with_defaults():
    """Helper: enter all patches and call run_agent with a clean snapshot."""
    patches = _all_agents_patch()
    # Start all patches
    mocks = [p.start() for p in patches]
    try:
        result = _mol.run_agent(_snapshot())
    finally:
        for p in patches:
            p.stop()
    return result


# ============================================================
# TESTS
# ============================================================

def test_valid_run_returns_classification():
    """Mock all agents to return clean outputs — result must have 'classification'."""
    result = _run_with_defaults()
    assert "classification" in result, "result missing 'classification' key"


def test_release_action_present():
    """Final output must include a 'release_action' key."""
    result = _run_with_defaults()
    assert "release_action" in result, "result missing 'release_action' key"


def test_health_state_present():
    """Final output must include a 'health_state' key."""
    result = _run_with_defaults()
    assert "health_state" in result, "result missing 'health_state' key"


def test_all_agent_outputs_in_result():
    """
    The orchestration result must contain output records for all 11 agents.
    Each is expected to appear under a recognised key or within agent_outputs.
    """
    result = _run_with_defaults()
    # The orchestration layer must surface agent_outputs or equivalent keys.
    # Accept either a top-level 'agent_outputs' dict or individual keys.
    agent_output_keys = [
        "dispatcher_output",
        "news_output",
        "social_rumor_output",
        "sentiment_output",
        "macro_output",
        "flow_output",
        "aggregator_output",
        "trade_output",
        "fault_output",
        "bias_output",
        "validator_output",
    ]
    agent_outputs_block = result.get("agent_outputs", result)
    present = [k for k in agent_output_keys if k in agent_outputs_block]
    assert len(present) == len(agent_output_keys), (
        f"Missing agent output keys: {set(agent_output_keys) - set(agent_outputs_block.keys())}"
    )


def test_decision_log_present():
    """Final output must include a 'decision_log' key that is a list."""
    result = _run_with_defaults()
    log = result.get("decision_log")
    assert isinstance(log, list), f"'decision_log' must be a list, got {type(log)}"


def test_dispatcher_halt_halts_pipeline():
    """
    When run_dispatcher returns halted=True, the orchestration pipeline must
    short-circuit and return a terminal classification.
    """
    halted_dispatcher = _make_dispatcher_output(state="invalid_route", signal="halt")
    halted_dispatcher["halted"] = True
    halted_dispatcher["halt_reason"] = "invalid_route"

    patches = _all_agents_patch(dispatcher_out=halted_dispatcher)
    mocks = [p.start() for p in patches]
    try:
        result = _mol.run_agent(_snapshot())
    finally:
        for p in patches:
            p.stop()

    classification = result.get("classification")
    assert classification in {"halt", "reject", "block"}, (
        f"Expected terminal classification after dispatcher halt, got {classification!r}"
    )
