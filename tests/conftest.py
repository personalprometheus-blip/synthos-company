"""
conftest.py — pytest configuration for the Synthos V1 test suite.

Adds the synthos-company root and agents/ to sys.path so agents
can be imported without installation. Provides a db_helpers mock
that records calls but performs no I/O.
"""

import sys
import os
import pytest
from unittest.mock import MagicMock

# ── Path setup ─────────────────────────────────────────────────────────────────

_TESTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR    = os.path.dirname(_TESTS_DIR)
_AGENTS_DIR  = os.path.join(_ROOT_DIR, "agents")
_UTILS_DIR   = os.path.join(_ROOT_DIR, "utils")

for p in (_ROOT_DIR, _AGENTS_DIR, _UTILS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── DB helpers mock ────────────────────────────────────────────────────────────

class MockDB:
    """Records all calls. Never writes to disk or network."""

    def __init__(self):
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append({"method": method, **kwargs})
        return True

    def post_suggestion(self, **kwargs):
        return self._record("post_suggestion", **kwargs)

    def log_event(self, **kwargs):
        return self._record("log_event", **kwargs)

    def get_recent_events(self, **kwargs):
        return []

    def __getattr__(self, name):
        def _stub(*args, **kwargs):
            return self._record(name, args=args, kwargs=kwargs)
        return _stub


@pytest.fixture
def mock_db():
    return MockDB()


@pytest.fixture(autouse=True)
def patch_db_helpers(monkeypatch):
    """
    Replace all db_helpers module-level singletons with MockDB instances
    so no agent writes to the real database during any test.
    """
    db = MockDB()
    mock_module = MagicMock()
    mock_module.get_db_helpers.return_value = db
    mock_module.DB.return_value = db
    mock_module.post_suggestion = db.post_suggestion
    mock_module.log_event = db.log_event

    # Patch db_helpers in every already-imported agent module
    for mod_name in list(sys.modules.keys()):
        if mod_name.endswith("_agent") or mod_name in (
            "macro_regime_agent", "market_state_aggregator",
            "audit_stack_agent", "result_audit_agent",
            "bias_audit_agent", "social_rumor_agent",
            "dispatcher_agent", "news_agent",
            "market_sentiment_agent", "flow_positioning_agent",
            "trade_logic_agent", "master_orchestration_layer",
        ):
            mod = sys.modules[mod_name]
            if hasattr(mod, "_db"):
                monkeypatch.setattr(mod, "_db", db, raising=False)
