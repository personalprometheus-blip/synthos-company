"""
tests/unit/macro_regime_agent/test_macro_regime_agent.py

Unit tests for macro_regime_agent.process_snapshot (aliased as run_agent).
"""

import datetime
import hashlib
import json
import sys
import os

import pytest

# conftest.py adds agents/ to sys.path; import the alias defined at module level.
from macro_regime_agent import run_agent


# ============================================================
# SNAPSHOT BUILDER
# ============================================================

def _snapshot(
    macro_data_status="online",
    spx_status="online",
    minutes_old=5,
    payload_overrides=None,
):
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes_old)
    ).isoformat()

    payload = {
        "cpi_yoy": 0.025,
        "cpi_mom": 0.002,
        "pce_yoy": 0.022,
        "gdp_growth_annualized": 2.2,
        "manufacturing_pmi": 52.0,
        "unemployment_rate": 0.039,
        "nonfarm_payrolls": 180000,
        "fed_funds_rate": 5.25,
        "rate_change_trend": "stable",
        "yield_10y": 4.2,
        "yield_2y": 4.8,
        "yield_spread_10y_2y": -0.6,
        "hy_spread": 3.5,
        "ig_spread": 1.2,
        "m2_growth": 0.02,
        "reserve_balances": 3.5e12,
        "dxy_change": 0.001,
        "em_equity_return": -0.01,
        "oil_change": 0.0,
        "gold_change": 0.0,
        "macro_news_sentiment": 0.1,
        "fed_communication_tone": "neutral",
        "spx_price": 4800,
        "spx_drawdown": 0.02,
        "spx_realized_vol_20d": 0.12,
        "vix_level": 15.0,
    }
    if payload_overrides:
        payload.update(payload_overrides)

    return {
        "snapshot_id": "test",
        "timestamp": ts,
        "macro_data_status": macro_data_status,
        "SPX_feed_status": spx_status,
        "processed_snapshot_store": [],
        "snapshot_payload": payload,
    }


def _payload_hash(snapshot):
    """Reproduce the SHA-256 hash gate_1_system computes for duplicate detection."""
    payload = snapshot["snapshot_payload"]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# ============================================================
# TESTS
# ============================================================

class TestMacroRegimeAgent:

    def test_output_is_dict(self):
        result = run_agent(_snapshot())
        assert isinstance(result, dict)

    def test_neutral_run(self):
        result = run_agent(_snapshot())
        assert "final_macro_state" in result
        assert "macro_confidence" in result
        assert "decision_log" in result

    def test_output_keys_present(self):
        result = run_agent(_snapshot())
        required_keys = [
            "final_macro_state",
            "macro_confidence",
            "macro_regime_score",
            "warning_states",
            "final_macro_signal",
            "decision_log",
        ]
        for key in required_keys:
            assert key in result, f"Missing output key: {key}"

    def test_offline_status_halts(self):
        result = run_agent(_snapshot(macro_data_status="offline"))
        # Agent must signal a halt: either halted=True or halt_reason is set.
        halted = result.get("halted") is True
        halt_reason_present = bool(result.get("halt_reason"))
        assert halted or halt_reason_present, (
            "Expected halt indicator when macro_data_status='offline'. "
            f"Got: halted={result.get('halted')}, halt_reason={result.get('halt_reason')}"
        )

    def test_stale_snapshot_halts(self):
        result = run_agent(_snapshot(minutes_old=120))
        halted = result.get("halted") is True
        halt_reason_present = bool(result.get("halt_reason"))
        assert halted or halt_reason_present, (
            "Expected halt indicator for stale snapshot (120 min old). "
            f"Got: halted={result.get('halted')}, halt_reason={result.get('halt_reason')}"
        )

    def test_duplicate_snapshot_suppressed(self):
        snap = _snapshot()
        first = run_agent(snap)
        # Gate 1 must not have halted on the first run.
        assert not first.get("halted"), "First run unexpectedly halted."

        snap["processed_snapshot_store"] = [_payload_hash(snap)]
        second = run_agent(snap)

        halted = second.get("halted") is True
        halt_reason_present = bool(second.get("halt_reason"))
        assert halted or halt_reason_present, (
            "Expected duplicate suppression on second identical snapshot. "
            f"Got: halted={second.get('halted')}, halt_reason={second.get('halt_reason')}"
        )

    def test_decision_log_nonempty(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        dlog = result.get("decision_log")
        assert isinstance(dlog, list), "decision_log should be a list."
        assert len(dlog) >= 1, "decision_log should contain at least one record."

    def test_confidence_in_range(self):
        result = run_agent(_snapshot())
        assert not result.get("halted"), "Run halted unexpectedly."
        confidence = result.get("macro_confidence")
        assert confidence is not None, "macro_confidence should not be None."
        assert 0.0 <= confidence <= 1.0, (
            f"macro_confidence={confidence} is outside [0.0, 1.0]."
        )
