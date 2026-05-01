"""
agents/_shared_scoop.py — single source of truth for queueing Scoop events.

Replaces the three near-duplicate writer functions that diverged into two
schemas (strongbox._alert_scoop wrote {"delivered":false}; sentinel/vault
._trigger_scoop wrote {"status":"pending"}; scoop's drain only matched the
second). Plus the trigger-file path was one-shot at startup, so anything
written after scoop boots was silently lost.

Producers (strongbox, sentinel, anything else) call enqueue_scoop_event()
which writes directly to scoop_queue in company.db. The legacy trigger
file is no longer used by producers — kept readable on the consumer side
for one-time backfill of stragglers, then deprecated.

Schema (canonical going forward):
    status         — UPPERCASE: PENDING / PROCESSING / SENT / RETRY / FAILED
                     scoop.py uses UPPER(status) on read so legacy
                     lowercase rows still match, but new writes are
                     UPPERCASE for consistency.
    priority       — int 0-3 (0=emergency, 1=important, 2=transactional,
                     3=background). Default derived from event_type.
    audience       — 'internal' (operator) | 'customer' (specific user) |
                     'broadcast' (all customers)
    pi_id          — for customer audience: the customer pi_id; for
                     internal: the source pi_id (which Pi raised the event)
    subject/body   — pre-formatted email content; if empty, scoop's
                     _format_legacy_event() builds them from payload.

The event_type → priority map mirrors scoop's _EVENT_CATEGORY_MAP intent
but maps to numeric priority instead of category. event_types not in the
map default to priority 2 (transactional) which is safe — never P0 by
accident.

Usage:
    from agents._shared_scoop import enqueue_scoop_event

    enqueue_scoop_event(
        event_type="backup_failed",
        subject=f"[Synthos CRITICAL] Backup failed for {pi_id}",
        body=f"Backup error: {error_msg}",
        audience="internal",
        pi_id=pi_id,
        source_agent="strongbox",
    )

Failure mode: if DB write fails for any reason (schema corruption, lock
contention beyond busy_timeout), the helper writes to scoop_trigger.json
as a defensive fallback. Scoop's polling loop drains the trigger file
every cycle (no longer one-shot), so the event still reaches dispatch on
the next cycle.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


log = logging.getLogger("shared_scoop")


# Resolved relative to repo root: <repo>/agents/_shared_scoop.py → repo = parents[1]
_HERE     = Path(__file__).resolve().parent
_REPO_DIR = _HERE.parent
_DB_PATH  = _REPO_DIR / "data" / "company.db"
_TRIGGER  = _REPO_DIR / "data" / "scoop_trigger.json"


# Event type → numeric priority. Mirrors scoop._EVENT_CATEGORY_MAP intent
# but with priority instead of category. event_type missing from this map
# defaults to 2 (transactional) — safe.
_EVENT_PRIORITY = {
    # P0 — emergency
    "PROTECTIVE_EXIT_TRIGGERED":   0,
    "CASCADE_DETECTED":            0,
    "kill_switch":                 0,
    # P1 — important
    "HEARTBEAT_SILENCE_ALERT":     1,
    "VALIDATION_FAILURE":          1,
    "silence_alert":               1,
    "agent_error":                 1,
    "agent_error_in_heartbeat":    1,
    "watchdog_alert":              1,
    "backup_failed":               1,
    "backup_upload_failed":        1,
    "backup_verify_failed":        1,
    "backup_config_error":         1,
    "backup_missing":              1,
    "retention_failed":            1,
    "portfolio_alert":             1,
    # P2 — transactional
    "TRADE_NOTIFICATION":          2,
    "trade_executed":              2,
    "trade_signal":                2,
    "order_filled":                2,
    "APPROVAL_REQUEST":            2,
    "account_approved":            2,
    "account_issue":               2,
    "customer_inactive":           2,
    "backup_stale":                2,
    "NEW_CUSTOMER":                2,
    # P3 — background
    "DAILY_REPORT":                3,
    "MORNING_DIGEST":              3,
    "daily_digest":                3,
    "weekly_report":               3,
    "performance":                 3,
}

_VALID_AUDIENCES = ("internal", "customer", "broadcast")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_priority(event_type: str, override: Optional[int]) -> int:
    """Validate explicit priority or look up from event_type. Default 2."""
    if override is not None:
        if override not in (0, 1, 2, 3):
            log.warning(
                f"enqueue_scoop_event: priority={override} out of range 0-3 — "
                f"clamping to 2 (transactional)"
            )
            return 2
        return override
    return _EVENT_PRIORITY.get(event_type, 2)


def _connect() -> sqlite3.Connection:
    """Short-timeout connect with WAL — never blocks the producer."""
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.OperationalError:
        pass  # PRAGMAs are best-effort
    return conn


def _fallback_to_trigger_file(
    event_id: str,
    event_type: str,
    subject: str,
    body: str,
    priority: int,
    audience: str,
    pi_id: Optional[str],
    source_agent: str,
    payload: dict,
) -> None:
    """Last-resort write when DB is unreachable. Scoop's drain loop will
    pick it up on the next cycle — no longer one-shot at startup."""
    try:
        existing = []
        if _TRIGGER.exists():
            try:
                existing = json.loads(_TRIGGER.read_text() or "[]")
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append({
            "id":           event_id,
            "type":         event_type,
            "subject":      subject,
            "body":         body,
            "priority":     priority,
            "audience":     audience,
            "pi_id":        pi_id,
            "source_agent": source_agent,
            "payload":      payload,
            "queued_at":    _now_iso(),
            "status":       "pending",  # canonical schema for new writes
        })
        _TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        _TRIGGER.write_text(json.dumps(existing, indent=2))
        log.warning(
            f"DB write failed; event {event_id[:8]} ({event_type}) "
            f"queued to scoop_trigger.json as fallback"
        )
    except Exception as e:
        log.error(
            f"Both DB and trigger-file writes failed for {event_id[:8]} "
            f"({event_type}): {e}"
        )


def enqueue_scoop_event(
    event_type: str,
    subject: str = "",
    body: str = "",
    *,
    priority: Optional[int] = None,
    audience: str = "internal",
    pi_id: Optional[str] = None,
    source_agent: str = "",
    payload: Optional[dict] = None,
    correlation_id: Optional[str] = None,
    related_ticker: Optional[str] = None,
    related_signal_id: Optional[str] = None,
) -> str:
    """
    Insert a pending event into scoop_queue. Returns the event id.

    Required:
        event_type — short symbolic identifier (e.g. "backup_failed",
                     "silence_alert", "TRADE_NOTIFICATION")

    Recommended:
        subject, body — pre-formatted email content. If empty, scoop's
                        _format_legacy_event() will construct from payload.

    Optional:
        priority — 0..3, defaults to map lookup or 2
        audience — 'internal' (operator), 'customer', 'broadcast'
        pi_id    — for customer audience or source-Pi attribution
        source_agent — name of agent queuing the event
        payload  — extra structured data for the dispatcher
        correlation_id, related_ticker, related_signal_id — for trade
                  notifications
    """
    if audience not in _VALID_AUDIENCES:
        log.warning(
            f"enqueue_scoop_event: audience={audience!r} not in "
            f"{_VALID_AUDIENCES} — defaulting to 'internal'"
        )
        audience = "internal"

    eid = str(uuid.uuid4())
    pri = _resolve_priority(event_type, priority)
    payload_json = json.dumps(payload or {})
    queued_at = _now_iso()

    try:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO scoop_queue
                  (id, event_type, priority, audience, pi_id, payload,
                   subject, body, source_agent, status, queued_at,
                   correlation_id, related_ticker, related_signal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    eid, event_type, pri, audience, pi_id, payload_json,
                    subject or "", body or "", source_agent or "",
                    queued_at,
                    correlation_id, related_ticker, related_signal_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        log.debug(
            f"queued P{pri} {event_type} id={eid[:8]} audience={audience} "
            f"src={source_agent}"
        )
        return eid

    except sqlite3.OperationalError as e:
        # Schema mismatch (column doesn't exist) — fall through to defensive
        # fallback so the event isn't lost. synthos_monitor migrations should
        # add the missing columns on next portal start.
        log.warning(f"scoop_queue INSERT failed (schema?): {e}")
        _fallback_to_trigger_file(
            eid, event_type, subject, body, pri, audience, pi_id,
            source_agent, payload or {},
        )
        return eid

    except Exception as e:
        log.error(f"scoop_queue INSERT failed: {e}")
        _fallback_to_trigger_file(
            eid, event_type, subject, body, pri, audience, pi_id,
            source_agent, payload or {},
        )
        return eid
