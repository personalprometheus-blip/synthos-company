"""
Unit tests for audit_stack_agent.run_agent().

No DB, network, or file I/O is performed.
audit_stack_agent calls get_db_helpers() and get_paths() at module level;
these functions are injected into the real db_helpers / synthos_paths modules
before the agent is imported so no SQLite connection is opened.
conftest.py (tests/) additionally patches _db on already-imported modules.
"""

import sys
import os
from unittest.mock import MagicMock

# ── Path setup ──────────────────────────────────────────────────────────────────
_TESTS_DIR  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT_DIR   = os.path.dirname(_TESTS_DIR)
_AGENTS_DIR = os.path.join(_ROOT_DIR, "agents")
_UTILS_DIR  = os.path.join(_ROOT_DIR, "utils")

for _p in (_ROOT_DIR, _AGENTS_DIR, _UTILS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Pre-import injection of missing helper functions ───────────────────────────
if "audit_stack_agent" not in sys.modules:
    import db_helpers as _dbh_mod
    import synthos_paths as _sp_mod

    _mock_db = MagicMock()
    _mock_db.log_event = MagicMock(return_value=True)
    _mock_db.post_suggestion = MagicMock(return_value=True)

    if not hasattr(_dbh_mod, "get_db_helpers"):
        _dbh_mod.get_db_helpers = lambda: _mock_db  # type: ignore[attr-defined]

    if not hasattr(_sp_mod, "get_paths"):
        _sp_mod.get_paths = lambda: MagicMock()  # type: ignore[attr-defined]

from audit_stack_agent import run_agent  # noqa: E402


# ── Snapshot helper ─────────────────────────────────────────────────────────────

def _snapshot(trade_output=None, run_id="test"):
    if trade_output is None:
        trade_output = {
            "trade_decision": "long",
            "trade_parameters": {"entry_type": "limit", "duration_class": "swing", "conviction_level": "high"},
            "decision_rationale": ["long_bias", "aligned_signal"],
            "decision_log": [{"gate": 1, "name": "system", "result": "pass", "reason_code": "ok", "ts": "2026-01-01T00:00:00+00:00", "inputs": {}}],
        }
    return {"trade_output": trade_output, "run_id": run_id}


# ── Tests ───────────────────────────────────────────────────────────────────────

def test_clean_trade_output_passes():
    """Clean trade_output → master_classification in {pass, review_recommended}."""
    snap = _snapshot()
    result = run_agent(snap)
    assert result.get("master_classification") in {"pass", "review_recommended"}


def test_null_trade_output_blocks():
    """trade_output=None → halted=True (intake router rejects null payload)."""
    # Build snapshot manually so trade_output is truly None.
    snap = {"trade_output": None, "run_id": "test"}
    result = run_agent(snap)
    assert result.get("halted") is True


def test_only_bias_lane_failed_flag():
    """only_bias_lane_failed is always present as a bool in the output."""
    snap = _snapshot()
    result = run_agent(snap)
    assert "only_bias_lane_failed" in result
    assert isinstance(result["only_bias_lane_failed"], bool)


def test_only_correctness_lane_failed_flag():
    """only_correctness_lane_failed is always present as a bool in the output."""
    snap = _snapshot()
    result = run_agent(snap)
    assert "only_correctness_lane_failed" in result
    assert isinstance(result["only_correctness_lane_failed"], bool)


def test_decision_log_present():
    """decision_log is always a list."""
    snap = _snapshot()
    result = run_agent(snap)
    assert isinstance(result.get("decision_log"), list)


def test_final_stack_signal_present():
    """final_stack_signal is in {clean, warning, faulty, blocked, systemic_failure}."""
    snap = _snapshot()
    result = run_agent(snap)
    valid = {"clean", "warning", "faulty", "blocked", "systemic_failure"}
    assert result.get("final_stack_signal") in valid


def test_lane_states_present():
    """lane_states is a dict."""
    snap = _snapshot()
    result = run_agent(snap)
    assert isinstance(result.get("lane_states"), dict)
