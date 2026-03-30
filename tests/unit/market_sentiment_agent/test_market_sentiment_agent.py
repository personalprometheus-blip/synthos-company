"""
tests/unit/market_sentiment_agent/test_market_sentiment_agent.py

Unit tests for market_sentiment_agent.run_agent (Agent 7.4).
"""

import datetime
import pytest

from market_sentiment_agent import run_agent


# ============================================================
# SNAPSHOT BUILDER
# ============================================================

def _snapshot(
    vix=18.0,
    return_1d=0.0,
    return_5d=0.0,
    vol=0.15,
    minutes_old=5,
    **overrides,
):
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes_old)
    ).isoformat()

    market = {
        "price_return_1d": return_1d,
        "price_return_5d": return_5d,
        "realized_vol_20d": vol,
        "vix_level": vix,
        "put_call_ratio": 0.95,
        "advance_decline_ratio": 1.1,
        "volume_ratio": 1.0,
        **overrides,
    }

    return {
        "normalized_market": market,
        "news_output": None,
        "rumor_output": None,
        "run_id": "test",
        "timestamp": ts,
    }


# ============================================================
# TESTS
# ============================================================

class TestMarketSentimentAgent:

    def test_neutral_output(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_market_state") == "neutral", (
            f"Expected 'neutral', got '{result.get('final_market_state')}'."
        )

    def test_panic_override(self):
        # VIX > 35 and 1d return < -3% => panic_override
        result = run_agent(_snapshot(vix=40, return_1d=-0.05))
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_market_state") == "panic_override", (
            f"Expected 'panic_override', got '{result.get('final_market_state')}'."
        )

    def test_euphoria_override(self):
        # VIX < 12 and 1d return > 2.5% => euphoric_warning_override
        result = run_agent(_snapshot(vix=10, return_1d=0.03))
        assert not result.get("halted"), "Run halted unexpectedly."
        assert result.get("final_market_state") == "euphoric_warning_override", (
            f"Expected 'euphoric_warning_override', got '{result.get('final_market_state')}'."
        )

    def test_no_market_input_halts(self):
        snap = _snapshot()
        snap["normalized_market"] = None
        result = run_agent(snap)
        assert result.get("halted") is True, (
            "Expected halted=True when normalized_market=None."
        )
        assert result.get("halt_reason") == "no_market_input", (
            f"Expected halt_reason='no_market_input', got '{result.get('halt_reason')}'."
        )

    def test_stale_snapshot_halts(self):
        result = run_agent(_snapshot(minutes_old=90))
        assert result.get("halted") is True, (
            "Expected halted=True for a 90-minute-old snapshot."
        )

    def test_confidence_in_range(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        confidence = result.get("sentiment_confidence")
        assert confidence is not None, "sentiment_confidence should not be None."
        assert 0.0 <= confidence <= 1.0, (
            f"sentiment_confidence={confidence} is outside [0.0, 1.0]."
        )

    def test_warning_state_is_list(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        assert isinstance(result.get("warning_state"), list), (
            "warning_state should be a list."
        )

    def test_decision_log_present(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        dlog = result.get("decision_log")
        assert isinstance(dlog, list), "decision_log should be a list."
        assert len(dlog) >= 1, "decision_log should be non-empty."

    def test_output_keys_present(self):
        result = run_agent(_snapshot())
        required_keys = [
            "final_market_state",
            "sentiment_confidence",
            "warning_state",
            "decision_log",
        ]
        for key in required_keys:
            assert key in result, f"Missing output key: {key}"

    def test_news_context_accepted(self):
        snap = _snapshot()
        snap["news_output"] = {
            "sentiment_score": 0.2,
            "headline_count": 5,
            "dominant_tone": "positive",
        }
        result = run_agent(snap)
        assert not result.get("halted"), (
            "Run halted unexpectedly when news_output was provided."
        )
        assert result.get("final_market_state") is not None, (
            "final_market_state should be set when news context is provided."
        )
