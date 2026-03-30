"""
Unit tests for trade_logic_agent.run_agent().

No DB, network, or file I/O is performed.
trade_logic_agent calls get_db_helpers() and get_paths() at module level;
these functions are injected into the real db_helpers / synthos_paths modules
before the agent is imported so no SQLite connection is opened.
conftest.py (tests/) additionally patches _db on already-imported modules.
"""

import sys
import os
import datetime
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
# db_helpers and synthos_paths exist as real modules but do not export
# get_db_helpers() / get_paths(). Inject no-op stubs before the agent is
# imported so the module-level calls succeed without disk I/O.
if "trade_logic_agent" not in sys.modules:
    import db_helpers as _dbh_mod
    import synthos_paths as _sp_mod

    _mock_db = MagicMock()
    _mock_db.log_event = MagicMock(return_value=True)
    _mock_db.post_suggestion = MagicMock(return_value=True)

    if not hasattr(_dbh_mod, "get_db_helpers"):
        _dbh_mod.get_db_helpers = lambda: _mock_db  # type: ignore[attr-defined]

    if not hasattr(_sp_mod, "get_paths"):
        _sp_mod.get_paths = lambda: MagicMock()  # type: ignore[attr-defined]

from trade_logic_agent import run_agent  # noqa: E402


# ── Snapshot helper ─────────────────────────────────────────────────────────────

def _snapshot(market_state=None, bias="neutral_bias", return_1d=0.0, vol=0.15,
              confidence=0.75, minutes_old=5):
    # Use naive UTC so gate_1_system's datetime.utcnow() comparison works.
    ts = (datetime.datetime.utcnow() -
          datetime.timedelta(minutes=minutes_old)).isoformat()
    if market_state is None:
        market_state = {
            "final_market_state": "mild_risk_on",
            "final_market_state_signal": 0.4,
            "aggregate_confidence": confidence,
            "downstream_route": "long_bias",
        }
    return {
        "market_state": market_state,
        "market_data": {"price_return_1d": return_1d, "realized_vol_20d": vol, "vix_level": 18.0},
        "bias": bias,
        "run_id": "test",
        "timestamp": ts,
    }


# ── Tests ───────────────────────────────────────────────────────────────────────

def test_long_bias_produces_long():
    """bias=long_bias with mild_risk_on state → trade_decision=long."""
    snap = _snapshot(bias="long_bias")
    result = run_agent(snap)
    assert result["trade_decision"] == "long"


def test_short_bias_produces_short():
    """bias=short_bias → trade_decision=short."""
    snap = _snapshot(bias="short_bias")
    result = run_agent(snap)
    assert result["trade_decision"] == "short"


def test_null_market_state_halts():
    """market_state=None → halted=True, halt_reason=null_market_state, trade_decision=None."""
    snap = _snapshot()
    snap["market_state"] = None
    result = run_agent(snap)
    assert result.get("halted") is True
    assert result.get("halt_reason") == "null_market_state"
    assert result.get("trade_decision") is None


def test_blocked_state_produces_no_trade():
    """final_market_state=blocked_override → trade_decision=no_trade."""
    market_state = {
        "final_market_state": "blocked_override",
        "final_market_state_signal": 0.0,
        "aggregate_confidence": 0.75,
        "downstream_route": "long_bias",
    }
    snap = _snapshot(market_state=market_state, bias="long_bias")
    result = run_agent(snap)
    assert result["trade_decision"] == "no_trade"


def test_panic_state_overrides_to_exit():
    """final_market_state=panic_override with long_bias → trade_decision=exit."""
    market_state = {
        "final_market_state": "panic_override",
        "final_market_state_signal": -0.8,
        "aggregate_confidence": 0.75,
        "downstream_route": "long_bias",
    }
    snap = _snapshot(market_state=market_state, bias="long_bias")
    result = run_agent(snap)
    assert result["trade_decision"] == "exit"


def test_low_confidence_fallback_to_hold():
    """aggregate_confidence=0.20 (below LOW_CONFIDENCE_THRESHOLD=0.35) → trade_decision=hold."""
    market_state = {
        "final_market_state": "mild_risk_on",
        "final_market_state_signal": 0.4,
        "aggregate_confidence": 0.20,
        "downstream_route": "long_bias",
    }
    snap = _snapshot(market_state=market_state, bias="long_bias", confidence=0.20)
    result = run_agent(snap)
    assert result["trade_decision"] == "hold"


def test_stale_timestamp_halts():
    """minutes_old=90 (beyond MAX_SNAPSHOT_AGE_MINUTES=60) → halted=True."""
    snap = _snapshot(minutes_old=90)
    result = run_agent(snap)
    assert result.get("halted") is True


def test_unrecognized_bias_holds():
    """bias=unknown_bias_xyz (not in BIAS_DECISION_MAP) → trade_decision=hold."""
    snap = _snapshot(bias="unknown_bias_xyz")
    result = run_agent(snap)
    assert result["trade_decision"] == "hold"


def test_trade_parameters_present():
    """Valid run → trade_parameters is a dict with entry_type, duration_class, conviction_level."""
    snap = _snapshot(bias="long_bias")
    result = run_agent(snap)
    params = result.get("trade_parameters")
    assert isinstance(params, dict)
    assert "entry_type" in params
    assert "duration_class" in params
    assert "conviction_level" in params


def test_decision_rationale_is_list():
    """decision_rationale is always a list of strings."""
    snap = _snapshot(bias="long_bias")
    result = run_agent(snap)
    rationale = result.get("decision_rationale")
    assert isinstance(rationale, list)
    assert all(isinstance(item, str) for item in rationale)


def test_decision_log_present():
    """decision_log is a non-empty list."""
    snap = _snapshot(bias="long_bias")
    result = run_agent(snap)
    log = result.get("decision_log")
    assert isinstance(log, list)
    assert len(log) > 0
