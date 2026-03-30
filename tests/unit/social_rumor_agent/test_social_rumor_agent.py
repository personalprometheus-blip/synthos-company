"""
Unit tests for social_rumor_agent.run_agent().

Exercises the V1 entry point with synthetic snapshot dicts.  No external
I/O is performed — db_helpers is patched to a no-op mock by the
conftest.patch_db_helpers fixture that applies to all tests/unit/ tests.
"""

import sys
import os

import pytest

# Ensure fixtures package is importable (mirrors dispatcher_agent pattern)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from social_rumor_agent import run_agent


# ---------------------------------------------------------------------------
# Valid classification / confirmation values
# ---------------------------------------------------------------------------

VALID_CLASSIFICATIONS = {
    "bullish_rumor_signal",
    "bearish_rumor_signal",
    "relative_alpha_signal",
    "manipulation_watch",
    "benchmark_regime_signal",
    "upgraded_to_confirmed_event",
    "ignore",
}

VALID_CONFIRMATION_STATES = {
    "confirmed",
    "provisional",
    "contradictory",
    "unresolved",
}


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _snapshot(minutes_old=5, **social_overrides):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    post_dt = now - datetime.timedelta(minutes=minutes_old)
    ts = post_dt.isoformat()
    social = {
        "text": "Major earnings beat expected for AAPL this quarter",
        "source_id": "stocktwits",
        # post_time must be a datetime object: gate_1_system calls
        # (now - post_time).total_seconds() and RumorDecisionLog calls
        # post_time.isoformat().  run_agent passes the field through raw.
        "post_time": post_dt,
        "credibility_score": 75,
        "language": "en",
        "repost_count": 15,
        "engagement_count": 45,
        "bot_probability": 0.10,
        "confirmation_count": 3,
        "sentiment_score": 0.65,
        "benchmark_context": {},
        **social_overrides,
    }
    return {"normalized_social": social, "run_id": "test", "timestamp": ts}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_social_input_halts():
    """normalized_social=None → halted=True, halt_reason='no_social_input'."""
    snap = _snapshot()
    snap["normalized_social"] = None
    result = run_agent(snap)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "no_social_input"


def test_valid_input_returns_dict():
    """Valid snapshot → result is a dict."""
    result = run_agent(_snapshot())
    assert isinstance(result, dict)


def test_classification_is_string():
    """Valid snapshot → classification is a non-None string."""
    result = run_agent(_snapshot())
    classification = result.get("classification")
    assert isinstance(classification, str)


def test_confidence_in_range():
    """Valid snapshot → 0.0 <= overall_confidence <= 1.0."""
    result = run_agent(_snapshot())
    confidence = result.get("overall_confidence")
    assert confidence is not None, "overall_confidence should not be None"
    assert 0.0 <= confidence <= 1.0, (
        f"overall_confidence={confidence} is outside [0.0, 1.0]"
    )


def test_confirmation_state_valid():
    """Valid snapshot → confirmation_state is one of the four valid values."""
    result = run_agent(_snapshot())
    assert result.get("confirmation_state") in VALID_CONFIRMATION_STATES, (
        f"Unexpected confirmation_state: {result.get('confirmation_state')}"
    )


def test_decision_log_present():
    """Valid snapshot → decision_log is a list."""
    result = run_agent(_snapshot())
    log = result.get("decision_log")
    assert isinstance(log, list), "decision_log should be a list"


def test_output_keys_present():
    """Valid snapshot → all required V1 output keys are present."""
    result = run_agent(_snapshot())
    required_keys = [
        "classification",
        "overall_confidence",
        "confirmation_state",
        "decision_log",
    ]
    for key in required_keys:
        assert key in result, f"Missing output key: {key}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known agent bug: when gate_1_system halts on empty text, rdl.to_dict() "
        "returns composite_score=None, then run_agent calls float(None) and raises "
        "TypeError.  Desired behaviour: returns a dict with halted=True.  "
        "Remove xfail once the agent guards composite_score against None."
    ),
)
def test_short_text_filtered():
    """Empty text → result is a dict (no exception; agent halts or ignores gracefully)."""
    result = run_agent(_snapshot(text=""))
    assert isinstance(result, dict)
