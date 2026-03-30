"""
tests/unit/flow_positioning_agent/test_flow_positioning_agent.py

Unit tests for flow_positioning_agent.run_agent (Agent 7.6).
"""

import datetime
import pytest

from flow_positioning_agent import run_agent


# ============================================================
# SNAPSHOT BUILDER
# ============================================================

def _snapshot(minutes_old=5, **flow_overrides):
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes_old)
    ).isoformat()

    flow = {
        "net_institutional_flow": 0.1,
        "positioning_score": 0.1,
        "short_interest_ratio": 0.05,
        "margin_debt_change": 0.02,
        "etf_flow_ratio": 0.05,
        "futures_positioning_net": 0.1,
        "options_skew": 0.05,
        **flow_overrides,
    }

    return {
        "normalized_flow": flow,
        "normalized_market": None,
        "run_id": "test",
        "timestamp": ts,
    }


# ============================================================
# TESTS
# ============================================================

class TestFlowPositioningAgent:

    def test_neutral_output(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        state = result.get("final_flow_state")
        assert isinstance(state, str) and len(state) > 0, (
            f"Expected a non-empty string for final_flow_state, got {state!r}."
        )

    def test_squeeze_override(self):
        # short_interest_ratio > 0.20 and positioning_score < -0.60 => squeeze_override
        result = run_agent(_snapshot(
            short_interest_ratio=0.30,
            positioning_score=-0.75,
        ))
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_flow_state") == "squeeze_override", (
            f"Expected 'squeeze_override', got '{result.get('final_flow_state')}'."
        )

    def test_liquidation_override(self):
        # margin_debt_change > 0.15 and net_institutional_flow < -0.70 => liquidation_override
        result = run_agent(_snapshot(
            margin_debt_change=0.25,
            net_institutional_flow=-0.80,
        ))
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_flow_state") == "liquidation_override", (
            f"Expected 'liquidation_override', got '{result.get('final_flow_state')}'."
        )

    def test_supportive_state(self):
        # High net institutional flow, low short interest => strong or mild supportive
        result = run_agent(_snapshot(
            net_institutional_flow=0.75,
            positioning_score=0.60,
            short_interest_ratio=0.02,
            futures_positioning_net=0.60,
            etf_flow_ratio=0.50,
            options_skew=0.02,
        ))
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_flow_state") in {"strong_supportive", "mild_supportive"}, (
            f"Expected a supportive state, got '{result.get('final_flow_state')}'."
        )

    def test_no_flow_input_halts(self):
        snap = _snapshot()
        snap["normalized_flow"] = None
        result = run_agent(snap)
        assert result.get("halted") is True, (
            "Expected halted=True when normalized_flow=None."
        )

    def test_stale_snapshot_halts(self):
        result = run_agent(_snapshot(minutes_old=90))
        assert result.get("halted") is True, (
            "Expected halted=True for a 90-minute-old snapshot."
        )

    def test_confidence_in_range(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        confidence = result.get("flow_confidence")
        assert confidence is not None, "flow_confidence should not be None."
        assert 0.0 <= confidence <= 1.0, (
            f"flow_confidence={confidence} is outside [0.0, 1.0]."
        )

    def test_warning_state_is_list(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        assert isinstance(result.get("warning_state"), list), (
            "warning_state should be a list."
        )

    def test_output_keys_present(self):
        result = run_agent(_snapshot())
        required_keys = [
            "final_flow_state",
            "flow_confidence",
            "warning_state",
            "decision_log",
        ]
        for key in required_keys:
            assert key in result, f"Missing output key: {key}"
