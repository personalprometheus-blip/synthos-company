"""Synthetic snapshot fixtures for V1 unit and integration tests."""
import datetime


def _ts(minutes_ago=0):
    """Return ISO UTC timestamp N minutes in the past."""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat()


FIXTURES = {
    # ------------------------------------------------------------------
    # Dispatcher scenarios
    # ------------------------------------------------------------------
    "dispatcher_valid_normal": {
        "parsed_input": {"route_type": "equity", "asset_class": "single_stock", "mode_flag": "normal"},
        # Registry values must be the mode keyword ("normal" | "staged" | "fallback" | "triage")
        # so that gate_4 MODE_MAP resolves them to the correct final_dispatch_signal.
        "route_registry": {"equity:single_stock": "normal"},
        "cycle_detection_store": [],
        "run_id": "test-001",
    },
    "dispatcher_valid_staged": {
        "parsed_input": {"route_type": "equity", "asset_class": "etf", "mode_flag": "staged"},
        "route_registry": {"equity:etf": "staged"},
        "cycle_detection_store": [],
        "run_id": "test-002",
    },
    "dispatcher_invalid_route": {
        "parsed_input": {"route_type": "unknown_type", "asset_class": "unknown_class"},
        "route_registry": {"equity:single_stock": "normal"},
        "cycle_detection_store": [],
        "run_id": "test-003",
    },
    "dispatcher_null_input": {
        "parsed_input": None,
        "route_registry": {"equity:single_stock": "normal"},
        "cycle_detection_store": [],
        "run_id": "test-004",
    },
    "dispatcher_no_routing_fields": {
        "parsed_input": {"some_other_field": "value"},
        "route_registry": {"equity:single_stock": "normal"},
        "cycle_detection_store": [],
        "run_id": "test-005",
    },
    # ------------------------------------------------------------------
    # News agent scenarios
    # ------------------------------------------------------------------
    "news_bullish": {
        "normalized_news": {
            "headline_sentiment": 0.75,
            "article_count": 5,
            "source_credibility": 0.80,
            "market_relevance": 0.85,
            "confirmation_count": 4,
            "contradiction_count": 0,
        },
        "promoted_rumor_context": None,
        "run_id": "news-001",
        "timestamp": None,  # will be set dynamically
    },
    "news_bearish": {
        "normalized_news": {
            "headline_sentiment": -0.70,
            "article_count": 3,
            "source_credibility": 0.75,
            "market_relevance": 0.80,
            "confirmation_count": 3,
            "contradiction_count": 0,
        },
        "promoted_rumor_context": None,
        "run_id": "news-002",
        "timestamp": None,
    },
    "news_freeze": {
        "normalized_news": {
            "headline_sentiment": 0.10,
            "article_count": 8,
            "source_credibility": 0.70,
            "market_relevance": 0.75,
            "confirmation_count": 2,
            "contradiction_count": 6,
        },
        "promoted_rumor_context": None,
        "run_id": "news-003",
        "timestamp": None,
    },
    "news_no_input": {
        "normalized_news": None,
        "promoted_rumor_context": None,
        "run_id": "news-004",
        "timestamp": None,
    },
    "news_rumor_only": {
        "normalized_news": None,
        "promoted_rumor_context": {
            "classification": "upgraded_to_confirmed_event",
            "overall_confidence": 0.85,
            "headline_sentiment": 0.65,
            "source_credibility": 0.78,
            "market_relevance": 0.90,
            "confirmation_count": 3,
            "contradiction_count": 0,
        },
        "run_id": "news-005",
        "timestamp": None,
    },
    "news_stale": {
        "normalized_news": {
            "headline_sentiment": 0.50,
            "article_count": 2,
            "source_credibility": 0.65,
            "market_relevance": 0.70,
            "confirmation_count": 1,
            "contradiction_count": 0,
        },
        "promoted_rumor_context": None,
        "run_id": "news-006",
        "timestamp": None,  # will be set to stale value in test
    },
}


def make_fixture(name: str, stale: bool = False) -> dict:
    """Return a copy of the named fixture with a fresh or stale timestamp."""
    import copy
    f = copy.deepcopy(FIXTURES[name])
    if "timestamp" in f:
        f["timestamp"] = _ts(minutes_ago=90 if stale else 5)
    return f
