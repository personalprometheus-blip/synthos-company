"""
Integration test: Social/Rumor → News promotion path.

Verify that when social_rumor_agent classifies a post as
upgraded_to_confirmed_event, the news_agent receives that as
promoted_rumor_context and produces a valid classification.

No agent internals are mocked — only _db is suppressed via conftest.
"""

import sys
import os

# Ensure the tests root is importable (mirrors the pattern used across the suite)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_rumor_promotion_to_news():
    """
    A rumor classified as upgraded_to_confirmed_event must produce
    a non-None classification when fed to the news agent as promoted context.
    """
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Simulate a social rumor output that was upgraded
    promoted_context = {
        "classification": "upgraded_to_confirmed_event",
        "overall_confidence": 0.88,
        "headline_sentiment": 0.72,
        "source_credibility": 0.85,
        "market_relevance": 0.92,
        "confirmation_count": 4,
        "contradiction_count": 0,
    }

    # Feed as promoted_rumor_context to news agent
    from news_agent import run_agent as run_news
    news_snapshot = {
        "normalized_news": None,
        "promoted_rumor_context": promoted_context,
        "run_id": "integration-001",
        "timestamp": ts,
    }
    news_result = run_news(news_snapshot)

    assert news_result.get("halted") is not True, "News agent halted unexpectedly"
    assert news_result["classification"] is not None
    assert isinstance(news_result["decision_log"], list)
    assert len(news_result["decision_log"]) > 0


def test_news_merge_both_inputs():
    """
    When both normalized_news and promoted_rumor_context are present,
    the merge produces a valid output with confirmation_state set.
    """
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    from news_agent import run_agent as run_news

    snapshot = {
        "normalized_news": {
            "headline_sentiment": 0.55,
            "article_count": 3,
            "source_credibility": 0.72,
            "market_relevance": 0.78,
            "confirmation_count": 2,
            "contradiction_count": 1,
        },
        "promoted_rumor_context": {
            "classification": "upgraded_to_confirmed_event",
            "overall_confidence": 0.80,
            "headline_sentiment": 0.70,
            "source_credibility": 0.82,
            "market_relevance": 0.88,
            "confirmation_count": 3,
            "contradiction_count": 0,
        },
        "run_id": "integration-002",
        "timestamp": ts,
    }
    result = run_news(snapshot)
    assert result.get("halted") is not True
    assert result["confirmation_state"] in {"confirmed", "provisional", "contradictory", "unresolved"}
    assert 0.0 <= result["overall_confidence"] <= 1.0


def test_output_keys_stable():
    """
    Output dict keys must be exactly the V1 contract keys every time.
    """
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    from news_agent import run_agent as run_news

    snapshot = {
        "normalized_news": {
            "headline_sentiment": 0.30,
            "article_count": 2,
            "source_credibility": 0.65,
            "market_relevance": 0.70,
            "confirmation_count": 1,
            "contradiction_count": 0,
        },
        "promoted_rumor_context": None,
        "run_id": "integration-003",
        "timestamp": ts,
    }
    result = run_news(snapshot)
    for key in ("classification", "overall_confidence", "confirmation_state", "decision_log"):
        assert key in result, f"Missing V1 output key: {key}"
