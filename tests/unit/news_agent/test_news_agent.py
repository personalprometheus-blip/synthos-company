"""
Unit tests for news_agent.run_agent().

Each test exercises a named scenario from synthetic_snapshots.FIXTURES and
asserts the expected output shape and field values.  No external I/O is
performed — db_helpers is patched to a no-op mock by conftest.patch_db_helpers.
"""

import sys
import os

import pytest

# Ensure fixtures package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fixtures.synthetic_snapshots import make_fixture
from news_agent import run_agent

# ---------------------------------------------------------------------------
# Valid classification values defined by the agent spec
# ---------------------------------------------------------------------------

VALID_CLASSIFICATIONS = {
    "bullish_signal",
    "bearish_signal",
    "freeze",
    "relative_alpha_signal",
    "benchmark_regime_signal",
    "watch_only",
    "provisional_watch",
    "ignore",
}

VALID_CONFIRMATION_STATES = {
    "confirmed",
    "provisional",
    "contradictory",
    "unresolved",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bullish_signal():
    """High positive sentiment, high credibility/relevance → bullish_signal."""
    snapshot = make_fixture("news_bullish")
    result = run_agent(snapshot)

    assert result.get("halted") is not True
    assert result.get("classification") == "bullish_signal"


def test_bearish_signal():
    """High negative sentiment, high credibility/relevance → bearish_signal."""
    snapshot = make_fixture("news_bearish")
    result = run_agent(snapshot)

    assert result.get("halted") is not True
    assert result.get("classification") == "bearish_signal"


def test_freeze_state():
    """
    High contradiction count relative to confirmations (ratio >= 0.60)
    → classification=freeze.
    """
    snapshot = make_fixture("news_freeze")
    result = run_agent(snapshot)

    assert result.get("halted") is not True
    assert result.get("classification") == "freeze"


def test_no_input_halts():
    """Both normalized_news and promoted_rumor_context absent → halt no_news_input."""
    snapshot = make_fixture("news_no_input")
    result = run_agent(snapshot)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "no_news_input"


def test_rumor_only_input():
    """
    promoted_rumor_context present, normalized_news absent → pipeline runs and
    produces a valid (non-None) classification string.
    """
    snapshot = make_fixture("news_rumor_only")
    result = run_agent(snapshot)

    assert result.get("halted") is not True
    classification = result.get("classification")
    assert isinstance(classification, str)
    assert classification in VALID_CLASSIFICATIONS


def test_stale_timestamp_halts():
    """Snapshot timestamp 90 minutes old → halt stale_snapshot."""
    snapshot = make_fixture("news_bullish", stale=True)
    result = run_agent(snapshot)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "stale_snapshot"


def test_confidence_in_range():
    """Successful bullish run → overall_confidence is a float in [0.0, 1.0]."""
    snapshot = make_fixture("news_bullish")
    result = run_agent(snapshot)

    confidence = result.get("overall_confidence")
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0


def test_confirmation_state_valid():
    """Successful run → confirmation_state is one of the four valid values."""
    snapshot = make_fixture("news_bullish")
    result = run_agent(snapshot)

    assert result.get("confirmation_state") in VALID_CONFIRMATION_STATES


def test_decision_log_present():
    """Any completed run must produce a non-empty decision_log list."""
    snapshot = make_fixture("news_bullish")
    result = run_agent(snapshot)

    log = result.get("decision_log")
    assert isinstance(log, list)
    assert len(log) >= 1
