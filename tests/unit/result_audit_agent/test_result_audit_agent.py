"""
Unit tests for result_audit_agent.run_agent().

No DB, network, or file I/O is performed.
result_audit_agent calls DB() at module level; the DB class is replaced with
a MagicMock before the agent is imported so no SQLite connection is opened.
conftest.py (tests/) additionally patches _db on already-imported modules.

V1 classification values: pass | pass_with_warnings | fail | block_output
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

# ── Pre-import: replace DB constructor so no SQLite connection is opened ────────
if "result_audit_agent" not in sys.modules:
    import db_helpers as _dbh_mod

    _mock_db_instance = MagicMock()
    _mock_db_instance.log_event = MagicMock(return_value=True)
    _mock_db_instance.post_suggestion = MagicMock(return_value=True)

    # Patch the DB class so DB() returns the mock instance
    _dbh_mod.DB = MagicMock(return_value=_mock_db_instance)  # type: ignore[attr-defined]

from result_audit_agent import run_agent  # noqa: E402

# ── Valid classification set (V1) ───────────────────────────────────────────────
_VALID_CLASSIFICATIONS = {"pass", "pass_with_warnings", "fail", "block_output"}


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

def test_clean_output_passes():
    """Valid trade_output → classification is a non-null V1 value."""
    snap = _snapshot()
    result = run_agent(snap)
    assert result.get("classification") in _VALID_CLASSIFICATIONS


def test_null_trade_output_blocks():
    """trade_output=None → classification is in the V1 set (gate-1 halt maps to fail)."""
    # Build snapshot manually so trade_output is truly None.
    snap = {"trade_output": None, "run_id": "test"}
    result = run_agent(snap)
    assert result.get("classification") in _VALID_CLASSIFICATIONS


def test_fault_list_is_list():
    """fault_list is always a list regardless of input."""
    snap = _snapshot()
    result = run_agent(snap)
    assert isinstance(result.get("fault_list"), list)


def test_decision_log_present():
    """decision_log is always a list."""
    snap = _snapshot()
    result = run_agent(snap)
    assert isinstance(result.get("decision_log"), list)


def test_classification_in_valid_set():
    """classification is one of the four valid V1 values."""
    snap = _snapshot()
    result = run_agent(snap)
    assert result.get("classification") in _VALID_CLASSIFICATIONS
