"""
Unit tests for market_state_aggregator.run_agent() (alias: aggregate_market_state).

Exercises the 26-gate pipeline with synthetic snapshot dicts.  No external
I/O is performed — db_helpers is patched to a no-op mock by the
conftest.patch_db_helpers fixture that applies to all tests/unit/ tests.
"""

import sys
import os
import json
import hashlib

import pytest

# Ensure fixtures package is importable (mirrors dispatcher_agent pattern)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from market_state_aggregator import run_agent


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _snapshot(minutes_old=5, macro_state="neutral", news_conf=0.7, include_agents=True):
    import datetime
    # The agent parses the timestamp with fromisoformat() then does
    # datetime.utcnow() - snap_time (naive arithmetic).  A timezone-aware
    # ISO string ("+00:00") produces an aware datetime that raises TypeError
    # on naive subtraction, which the agent catches as reject_snapshot.
    # Provide a naive UTC timestamp to match what the agent expects.
    ts = (
        datetime.datetime.utcnow()
        - datetime.timedelta(minutes=minutes_old)
    ).isoformat()
    snap = {
        "snapshot_id": "test-agg",
        "timestamp": ts,
        "processed_snapshot_store": [],
    }
    if include_agents:
        snap["macro_agent_output"] = {
            "final_macro_state": macro_state,
            "macro_confidence": 0.72,
            "macro_regime_score": 0.10,
            "final_macro_signal": 0.07,
            "warning_states": [],
        }
        snap["news_agent_output"] = {
            "classification": "bullish_signal",
            "overall_confidence": news_conf,
            "confirmation_state": "confirmed",
        }
        snap["sentiment_agent_output"] = None
        snap["flow_agent_output"] = None
        snap["rumor_agent_output"] = None
        snap["benchmark_state_output"] = None
        snap["validator_output"] = None
    else:
        for k in [
            "macro_agent_output",
            "news_agent_output",
            "sentiment_agent_output",
            "flow_agent_output",
            "rumor_agent_output",
            "benchmark_state_output",
            "validator_output",
        ]:
            snap[k] = None
    return snap


def _upstream_hash(snap: dict) -> str:
    """Reproduce the SHA-256 hash gate_1_system uses for duplicate detection.

    The aggregator hashes the values of the seven upstream output fields
    (not the full snapshot).
    """
    upstream_fields = {
        "macro":     "macro_agent_output",
        "sentiment": "sentiment_agent_output",
        "flow":      "flow_agent_output",
        "news":      "news_agent_output",
        "rumor":     "rumor_agent_output",
        "benchmark": "benchmark_state_output",
        "validator": "validator_output",
    }
    hash_payload = {field: snap.get(field) for field in upstream_fields.values()}
    return hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_run_returns_dict():
    """Valid snapshot → result is a dict."""
    result = run_agent(_snapshot())
    assert isinstance(result, dict)


def test_output_keys_present():
    """Valid snapshot → all V1 output keys are present."""
    result = run_agent(_snapshot())
    required_keys = [
        "final_market_state",
        "final_market_state_signal",
        "aggregate_market_score",
        "aggregate_confidence",
        "decision_log",
    ]
    for key in required_keys:
        assert key in result, f"Missing output key: {key}"


def test_no_upstream_agents_halts():
    """All agent outputs None → pipeline halts (halted=True or halt_reason set)."""
    result = run_agent(_snapshot(include_agents=False))
    halted = result.get("halted") is True
    halt_reason_present = bool(result.get("halt_reason"))
    assert halted or halt_reason_present, (
        "Expected halt when all upstream agents are None. "
        f"Got: halted={result.get('halted')}, halt_reason={result.get('halt_reason')}"
    )


def test_duplicate_snapshot_suppressed():
    """Running with the same upstream data twice → second run halts with suppress_duplicate."""
    snap = _snapshot()

    first = run_agent(snap)
    assert not first.get("halted"), "First run unexpectedly halted."

    snap["processed_snapshot_store"] = [_upstream_hash(snap)]
    second = run_agent(snap)

    halted = second.get("halted") is True
    halt_reason_present = bool(second.get("halt_reason"))
    assert halted or halt_reason_present, (
        "Expected duplicate suppression on second identical snapshot. "
        f"Got: halted={second.get('halted')}, halt_reason={second.get('halt_reason')}"
    )
    # Also confirm the reason code is the expected one when available
    if second.get("halt_reason"):
        assert second.get("halt_reason") == "suppress_duplicate", (
            f"Expected halt_reason='suppress_duplicate', got {second.get('halt_reason')!r}"
        )


def test_stale_snapshot_halts():
    """Snapshot 90 minutes old → pipeline halts."""
    result = run_agent(_snapshot(minutes_old=90))
    halted = result.get("halted") is True
    halt_reason_present = bool(result.get("halt_reason"))
    assert halted or halt_reason_present, (
        "Expected halt for 90-minute-old snapshot. "
        f"Got: halted={result.get('halted')}, halt_reason={result.get('halt_reason')}"
    )


def test_decision_log_nonempty():
    """Valid snapshot → decision_log is a non-empty list."""
    result = run_agent(_snapshot())
    assert not result.get("halted"), "Run halted unexpectedly."
    dlog = result.get("decision_log")
    assert isinstance(dlog, list), "decision_log should be a list"
    assert len(dlog) >= 1, "decision_log should contain at least one record"


def test_confidence_in_range():
    """Valid snapshot → 0.0 <= aggregate_confidence <= 1.0."""
    result = run_agent(_snapshot())
    assert not result.get("halted"), "Run halted unexpectedly."
    confidence = result.get("aggregate_confidence")
    assert confidence is not None, "aggregate_confidence should not be None"
    assert 0.0 <= confidence <= 1.0, (
        f"aggregate_confidence={confidence} is outside [0.0, 1.0]"
    )


def test_validator_output_ignored():
    """
    V1 rule: validator_output must be null at aggregation stage.
    Passing a non-null validator_output should not affect the pre-trade
    output keys — the result must still be a dict with all required keys
    and must not cause any exception.
    """
    snap = _snapshot()
    snap["validator_output"] = {
        "validation_state": "approved",
        "validator_confidence": 0.90,
        "validator_flags": [],
    }
    result = run_agent(snap)
    # Pipeline should still complete (validator_output is a no-op in V1)
    assert isinstance(result, dict)
    required_keys = [
        "final_market_state",
        "final_market_state_signal",
        "aggregate_market_score",
        "aggregate_confidence",
        "decision_log",
    ]
    for key in required_keys:
        assert key in result, (
            f"Missing output key '{key}' even with non-null validator_output"
        )
