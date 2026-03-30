"""
tests/unit/conftest.py — Pre-import stubs for unit tests.

The trading agents call get_db_helpers() and get_paths() at module level.
Those factory functions do not exist in the real utils — they are called
via a compatibility shim that was never added to the production modules.

This conftest injects lightweight fakes into sys.modules *before* any agent
module is imported, so the module-level calls succeed without touching the
filesystem or a database.

Applies to: all tests under tests/unit/
"""

import sys
import os
import types
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Stub: db_helpers
# ---------------------------------------------------------------------------

class _StubDB:
    """Silent no-op stand-in for utils/db_helpers.DB."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append({"method": method, **kwargs})
        return True

    def log_event(self, **kwargs):
        return self._record("log_event", **kwargs)

    def post_suggestion(self, **kwargs):
        return self._record("post_suggestion", **kwargs)

    def get_recent_events(self, **kwargs):
        return []

    def __getattr__(self, name):
        def _stub(*args, **kwargs):
            return self._record(name, args=args, kwargs=kwargs)
        return _stub


def _get_db_helpers():
    return _StubDB()


_db_helpers_mod = types.ModuleType("db_helpers")
_db_helpers_mod.DB = _StubDB
_db_helpers_mod.get_db_helpers = _get_db_helpers

sys.modules.setdefault("db_helpers", _db_helpers_mod)


# ---------------------------------------------------------------------------
# Stub: synthos_paths
# ---------------------------------------------------------------------------

def _get_paths():
    """Return a minimal paths object with string attributes."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent
    obj = MagicMock()
    obj.BASE_DIR    = root
    obj.DATA_DIR    = root / "data"
    obj.LOGS_DIR    = root / "logs"
    obj.AGENTS_DIR  = root / "agents"
    obj.DB_PATH     = root / "data" / "company.db"
    return obj


_synthos_paths_mod = types.ModuleType("synthos_paths")
_synthos_paths_mod.get_paths = _get_paths

# Also expose the module-level names that agents may import directly
from pathlib import Path as _Path
_root = _Path(__file__).resolve().parent.parent.parent
_synthos_paths_mod.BASE_DIR   = _root
_synthos_paths_mod.DATA_DIR   = _root / "data"
_synthos_paths_mod.LOGS_DIR   = _root / "logs"
_synthos_paths_mod.AGENTS_DIR = _root / "agents"
_synthos_paths_mod.DB_PATH    = _root / "data" / "company.db"
_synthos_paths_mod.ENV_PATH   = _root / ".env"

sys.modules.setdefault("synthos_paths", _synthos_paths_mod)
