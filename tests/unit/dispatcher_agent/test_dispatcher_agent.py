"""
Unit tests for dispatcher_agent.run_agent().

Each test exercises a single named scenario from synthetic_snapshots.FIXTURES
and asserts the expected output shape and field values.  No external I/O is
performed — db_helpers is patched to a no-op mock by conftest.patch_db_helpers.
"""

import sys
import os
import json
import hashlib
import copy

import pytest

# Ensure fixtures package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fixtures.synthetic_snapshots import make_fixture, FIXTURES
from dispatcher_agent import run_agent, DISPATCH_SCORE_NORMAL, DISPATCH_SCORE_STAGED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route_hash(route_type: str, asset_class: str) -> str:
    """Reproduce the SHA-256 hash used by gate_3_cycle_detection."""
    route_tuple = json.dumps([route_type, asset_class], sort_keys=True)
    return hashlib.sha256(route_tuple.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_normal_route():
    """Valid equity/single_stock input → route resolved to normal_route."""
    snapshot = make_fixture("dispatcher_valid_normal")
    result = run_agent(snapshot)

    assert result.get("dispatch_state") == "valid_route"
    assert result.get("final_dispatch_signal") == "normal_route"
    assert result.get("dispatch_score") == DISPATCH_SCORE_NORMAL
    assert result.get("halted") is not True


def test_valid_staged_route():
    """Valid equity/etf input → route resolved to staged_route."""
    snapshot = make_fixture("dispatcher_valid_staged")
    result = run_agent(snapshot)

    assert result.get("final_dispatch_signal") == "staged_route"
    assert result.get("dispatch_score") == DISPATCH_SCORE_STAGED
    assert result.get("halted") is not True


def test_invalid_route_halts():
    """Unknown route_type/asset_class → pipeline halted with invalid_route."""
    snapshot = make_fixture("dispatcher_invalid_route")
    result = run_agent(snapshot)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "invalid_route"


def test_null_input_halts():
    """parsed_input=None → pipeline halted with reject_input at gate 1."""
    snapshot = make_fixture("dispatcher_null_input")
    result = run_agent(snapshot)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "reject_input"


def test_no_routing_fields_halts():
    """parsed_input present but contains no routing fields → halted."""
    snapshot = make_fixture("dispatcher_no_routing_fields")
    result = run_agent(snapshot)

    assert result.get("halted") is True


def test_cycle_detection_halts():
    """
    When cycle_detection_store already contains the hash for the route,
    gate 3 must halt with cycle_break.
    """
    snapshot = copy.deepcopy(FIXTURES["dispatcher_valid_normal"])
    pi = snapshot["parsed_input"]
    existing_hash = _route_hash(pi["route_type"], pi["asset_class"])
    snapshot["cycle_detection_store"] = [existing_hash]
    snapshot["run_id"] = "test-cycle-001"

    result = run_agent(snapshot)

    assert result.get("halted") is True
    assert result.get("halt_reason") == "cycle_break"


def test_decision_log_present():
    """Any completed run must produce a non-empty decision_log list."""
    snapshot = make_fixture("dispatcher_valid_normal")
    result = run_agent(snapshot)

    log = result.get("decision_log")
    assert isinstance(log, list)
    assert len(log) >= 1


def test_output_is_plain_dict():
    """run_agent() must return a plain dict, not a class instance."""
    snapshot = make_fixture("dispatcher_valid_normal")
    result = run_agent(snapshot)

    assert type(result) is dict
