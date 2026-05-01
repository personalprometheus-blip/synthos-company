"""
scoop.py -- Scoop Mail Agent
Synthos Company Pi

Role:
  Single delivery channel for ALL outbound communication.
  No other agent sends email, SMS, or external notifications directly.
  Everything goes through Scoop.

  Scoop is accountable for:
    - Every alert reaching its intended recipient
    - Priority dispatch (P0 first, always)
    - Queuing and retrying failed sends (max 5 attempts)
    - Formatting messages correctly for internal vs. customer audiences
    - Tracking delivery success rate

  Event sources:
    - scoop_queue table in company.db (primary -- DB-backed, priority-ordered)
    - Legacy scoop_trigger.json shim (backward compat -- drained once at startup)

  Priority classes:
    P0 = emergency    (PROTECTIVE_EXIT_TRIGGERED, CASCADE_DETECTED, ...)
    P1 = important    (HEARTBEAT_SILENCE_ALERT, VALIDATION_FAILURE, ...)
    P2 = transactional (TRADE_NOTIFICATION, APPROVAL_REQUEST, ...)
    P3 = background   (DAILY_REPORT, MORNING_DIGEST, ...)

  Transport:
    Primary:  Resend API
    Fallback: Gmail SMTP (uncomment .env keys + code block below when ready)
    Toggle:   Command portal controls active transport (future)

Schedule:
  Poll DB queue every 30s
  Retry failures every 5 minutes
  Morning report: daily at 8am ET

USAGE:
  python3 scoop.py           # start polling loop
  python3 scoop.py --status  # show delivery queue status
  python3 scoop.py --test    # send test email to project lead
"""

import os
import sys
import json
import uuid
import time
import signal
import logging
import argparse
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# -- CONFIG -------------------------------------------------------------------

_HERE    = Path(__file__).resolve().parent
BASE_DIR = _HERE.parent

DB_PATH        = BASE_DIR / "data" / "company.db"
LEGACY_TRIGGER = BASE_DIR / "data" / "scoop_trigger.json"
MORNING_REPORT = BASE_DIR / "data" / "morning_report.json"
LOG_FILE       = BASE_DIR / "logs" / "mail_agent.log"

ET = ZoneInfo("America/New_York")

load_dotenv(BASE_DIR / "company.env", override=True)

RESEND_API_KEY     = os.environ.get("RESEND_API_KEY", "")
ALERT_FROM         = os.environ.get("ALERT_FROM", "alerts@synthos.app")
ALERT_FROM_NAME    = os.environ.get("ALERT_FROM_NAME", "Synthos")
PROJECT_LEAD_EMAIL = os.environ.get("OPERATOR_EMAIL", "")

# Gmail SMTP config -- uncomment when command portal transport toggle is ready
# GMAIL_USER         = os.environ.get("GMAIL_USER", "")
# GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

POLL_INTERVAL_SEC   = 30
RETRY_INTERVAL_SEC  = 300
MAX_RETRIES         = 5
MORNING_REPORT_HOUR = 8

SYNTHOS_VERSION = "2.1"

# -- PORTAL NOTIFICATION CONFIG -----------------------------------------------
# In-app notifications via retail portal API. Agents include customer_id and
# optional notify_channels hint in payload to control delivery.
PORTAL_URL    = (os.environ.get("PORTAL_URL") or os.environ.get("RETAIL_PORTAL_URL", "")).rstrip("/")
PORTAL_TOKEN  = os.environ.get("PORTAL_TOKEN", "")
PREFER_PORTAL = os.environ.get("PREFER_PORTAL_NOTIFICATIONS", "false").lower() in ("1", "true", "yes")
BATCH_SIZE    = int(os.environ.get("SCOOP_BATCH_SIZE", "50"))

# -- LOGGING ------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s scoop: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("scoop")


# -- TIME HELPERS -------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_et() -> datetime:
    return datetime.now(ET)

def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


# -- DATABASE -----------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_customer_email(pi_id: str):
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT email, mail_alerts_enabled FROM customers WHERE pi_id=?",
                (pi_id,)
            ).fetchone()
            if row and row["email"] and row["mail_alerts_enabled"]:
                return row["email"]
    except Exception as e:
        log.warning(f"Could not look up email for {pi_id}: {e}")
    return None


def db_get_next_event():
    """Fetch single highest-priority PENDING or RETRY event.
    RETRY items are only eligible after exponential backoff:
    delay = RETRY_INTERVAL_SEC * 2^(retry_count-1)
    """
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM scoop_queue
                WHERE UPPER(status) IN ('PENDING', 'RETRY')
                ORDER BY priority ASC, created_at ASC
                LIMIT ?
            """, (BATCH_SIZE,)).fetchall()
            now_ts = now_utc().timestamp()
            row = None
            for r in rows:
                # Case-insensitive status check — db_helpers writes lowercase
                # 'retry'; _shared_scoop and scoop's own writes are uppercase.
                if (r["status"] or "").upper() == "RETRY" and r["last_attempt"]:
                    try:
                        last = datetime.fromisoformat(
                            r["last_attempt"].replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        last = 0
                    retries = r["retry_count"] or 1
                    backoff = RETRY_INTERVAL_SEC * (2 ** (retries - 1))
                    if now_ts - last < backoff:
                        continue  # not yet eligible
                row = r
                break
            return dict(row) if row else None
    except Exception as e:
        log.error(f"db_get_next_event error: {e}")
        return None


def db_mark_processing(event_id: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE scoop_queue SET status='PROCESSING', started_at=? WHERE id=?",
                (now_iso(), event_id)
            )
    except Exception as e:
        log.error(f"db_mark_processing error: {e}")


def db_mark_sent(event_id: str) -> None:
    now = now_iso()
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE scoop_queue SET status='SENT', sent_at=?, completed_at=? WHERE id=?",
                (now, now, event_id)
            )
    except Exception as e:
        log.error(f"db_mark_sent error: {e}")


def db_requeue(event_id: str, error_msg: str) -> None:
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE scoop_queue
                SET status='RETRY', retry_count=retry_count+1,
                    last_attempt=?, error_msg=?
                WHERE id=?
            """, (now_iso(), error_msg[:200], event_id))
    except Exception as e:
        log.error(f"db_requeue error: {e}")


def db_mark_failed(event_id: str, error_msg: str) -> None:
    now = now_iso()
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE scoop_queue
                SET status='FAILED', failed_at=?, last_attempt=?, error_msg=?
                WHERE id=?
            """, (now, now, error_msg[:200], event_id))
    except Exception as e:
        log.error(f"db_mark_failed error: {e}")


def db_queue_stats() -> dict:
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            p0 = conn.execute("""
                SELECT COUNT(*) FROM scoop_queue
                WHERE priority=0 AND status IN ('PENDING', 'RETRY')
            """).fetchone()[0]
            stats["P0_pending"] = p0
            return stats
    except Exception as e:
        log.error(f"db_queue_stats error: {e}")
        return {}


# -- TRANSPORT ----------------------------------------------------------------

def send_via_resend(to_email: str, subject: str, body_text: str,
                    body_html: str = None) -> bool:
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not configured")
        return False
    if not to_email:
        log.warning(f"No recipient for: {subject[:60]}")
        return False

    import requests as _req
    from_field = f"{ALERT_FROM_NAME} <{ALERT_FROM}>" if ALERT_FROM_NAME else ALERT_FROM
    payload_dict = {
        "from":    from_field,
        "to":      [to_email],
        "subject": subject,
        "text":    body_text,
    }
    if body_html:
        payload_dict["html"] = body_html

    idem_key = f"scoop-{subject[:30]}-{to_email}-{int(time.time() // 60)}"

    try:
        r = _req.post(
            "https://api.resend.com/emails",
            json=payload_dict,
            headers={
                "Authorization":   f"Bearer {RESEND_API_KEY}",
                "Idempotency-Key": idem_key,
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            log.info(f"[RESEND] Sent: {subject[:60]} -> {to_email}")
            return True
        log.warning(f"[RESEND] HTTP {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        log.error(f"[RESEND] Error: {e}")
        return False


# Gmail SMTP transport -- uncomment when command portal toggle is ready
# def send_via_gmail(to_email: str, subject: str, body_text: str) -> bool:
#     if not GMAIL_USER or not GMAIL_APP_PASSWORD:
#         return False
#     try:
#         import smtplib
#         from email.mime.text import MIMEText
#         msg = MIMEText(body_text)
#         msg['Subject'] = subject
#         msg['From']    = GMAIL_USER
#         msg['To']      = to_email
#         with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
#             s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
#             s.send_message(msg)
#         log.info(f"[GMAIL] Sent: {subject[:60]} -> {to_email}")
#         return True
#     except Exception as e:
#         log.error(f"[GMAIL] Error: {e}")
#         return False


def send_email(to_email: str, subject: str, body_text: str,
               body_html: str = None) -> bool:
    """Unified send. Resend primary. Gmail secondary (uncomment above)."""
    if send_via_resend(to_email, subject, body_text, body_html):
        return True
    # if send_via_gmail(to_email, subject, body_text):
    #     return True
    return False


# -- RECIPIENT RESOLUTION -----------------------------------------------------

def resolve_recipient(event: dict):
    eid = event.get("id", "?")[:8]
    audience = event.get("audience", "internal")
    # 2026-04-28 — explicit recipient_email in payload wins over the
    # pi_id → company.db.customers lookup. Lets the retail trader on
    # pi5 push customer-facing emails through without requiring every
    # retail customer to ALSO have a row in pi4b's customers table.
    # Producer fetches the email from auth.db (where it actually lives)
    # and passes it directly. Bell-notification path is independent of
    # this and always fires regardless of email config.
    if audience == "customer":
        try:
            payload = event.get("payload") or "{}"
            if isinstance(payload, str):
                payload = json.loads(payload) if payload else {}
            explicit_email = (payload or {}).get("recipient_email")
            if explicit_email:
                log.debug(f"Recipient: {explicit_email} (payload-explicit)  id={eid}")
                return explicit_email
        except Exception as e:
            log.debug(f"recipient_email payload parse failed: {e}")
        pi_id = event.get("pi_id")
        if pi_id:
            email = get_customer_email(pi_id)
            if email:
                log.debug(f"Recipient: {email} (customer lookup)  id={eid}")
                return email
        log.warning(
            f"audience=customer but no email for pi_id={event.get('pi_id')} "
            f"-- falling back to project lead"
        )
    if PROJECT_LEAD_EMAIL:
        log.debug(f"Recipient: {PROJECT_LEAD_EMAIL} (project lead fallback)  id={eid}")
    return PROJECT_LEAD_EMAIL or None




# -- PORTAL NOTIFICATION DISPATCH ---------------------------------------------

_EVENT_CATEGORY_MAP = {
    "trade_executed": "trade", "trade_signal": "trade", "order_filled": "trade",
    "TRADE_NOTIFICATION": "trade",
    "kill_switch": "alert", "portfolio_alert": "alert", "watchdog_alert": "alert",
    "PROTECTIVE_EXIT_TRIGGERED": "alert", "CASCADE_DETECTED": "alert",
    "HEARTBEAT_SILENCE_ALERT": "alert",
    "daily_digest": "daily", "weekly_report": "daily", "performance": "daily",
    "DAILY_REPORT": "daily", "MORNING_DIGEST": "daily",
    "account_approved": "account", "account_issue": "account",
    "APPROVAL_REQUEST": "account",
    "maintenance": "system", "system_alert": "system",
}

def _event_to_category(event_type: str) -> str:
    return _EVENT_CATEGORY_MAP.get(event_type, "system")

def _portal_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if PORTAL_TOKEN:
        headers["X-Service-Token"] = PORTAL_TOKEN
    return headers

def dispatch_portal_notification(event: dict) -> bool:
    """Send in-app notification to a specific customer via portal API.

    Short-circuits when PORTAL_TOKEN is not set. The retail portal's
    /api/notifications/send endpoint requires admin-session auth or a
    service-token auth path that has not yet been wired up — without
    PORTAL_TOKEN we know in advance the call would 401, so we skip
    silently rather than burning HTTP requests on guaranteed failures.
    Startup logs the disabled state once via run_loop().
    """
    if not PORTAL_URL or not PORTAL_TOKEN:
        return False

    try:
        payload = json.loads(event.get("payload") or "{}")
    except Exception:
        payload = {}
    customer_id = payload.get("customer_id", "")
    if not customer_id:
        log.debug(f"No customer_id in payload -- skipping portal notification")
        return False

    subject  = event.get("subject") or event.get("event_type", "Notification")
    body     = event.get("body") or ""
    category = _event_to_category(event.get("event_type", ""))

    notif_data = {
        "customer_id": customer_id,
        "category":    category,
        "title":       subject,
        "body":        body,
        "meta": {
            "source_agent": event.get("source_agent", ""),
            "priority":     event.get("priority"),
            "event_type":   event.get("event_type", ""),
            "scoop_id":     event.get("id", ""),
        },
    }

    import requests as _req
    try:
        r = _req.post(
            f"{PORTAL_URL}/api/notifications/send",
            headers=_portal_headers(), json=notif_data, timeout=10,
        )
        if r.status_code in (200, 201):
            log.info(f"[PORTAL] Notification sent: {subject[:50]} -> customer {customer_id[:8]}")
            return True
        log.warning(f"[PORTAL] HTTP {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        log.warning(f"[PORTAL] Error: {e}")
        return False


def dispatch_broadcast(event: dict) -> bool:
    """Send system-wide notification to ALL active customers via portal broadcast.

    Same short-circuit as dispatch_portal_notification — without
    PORTAL_TOKEN the broadcast endpoint will 401, so we skip rather than
    fail noisily for every event.
    """
    if not PORTAL_URL or not PORTAL_TOKEN:
        return False

    subject  = event.get("subject") or event.get("event_type", "System Alert")
    body     = event.get("body") or ""
    category = _event_to_category(event.get("event_type", ""))

    broadcast_data = {
        "category": category,
        "title":    subject,
        "body":     body,
        "meta": {
            "source_agent": event.get("source_agent", ""),
            "priority":     event.get("priority"),
            "event_type":   event.get("event_type", ""),
        },
    }

    import requests as _req
    try:
        r = _req.post(
            f"{PORTAL_URL}/api/notifications/broadcast",
            headers=_portal_headers(), json=broadcast_data, timeout=15,
        )
        if r.status_code in (200, 201):
            sent = r.json().get("sent", "?")
            log.info(f"[PORTAL] Broadcast to {sent} customers: {subject[:50]}")
            return True
        log.warning(f"[PORTAL] Broadcast HTTP {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        log.warning(f"[PORTAL] Broadcast error: {e}")
        return False

# -- DISPATCH -----------------------------------------------------------------

def dispatch_event(event: dict) -> bool:
    """
    Attempt to send a single queued event.
    Uses subject/body DB columns if present; falls back to payload formatting.
    Returns True on success.
    """
    event_id   = event["id"]
    event_type = event["event_type"]
    priority   = event.get("priority", 2)

    db_mark_processing(event_id)

    recipient = resolve_recipient(event)
    if not recipient:
        log.error(f"No recipient for {event_id} ({event_type}) -- marking failed")
        db_mark_failed(event_id, "no_recipient")
        return False

    subject = event.get("subject") or ""
    body    = event.get("body") or ""

    if not subject or not body:
        try:
            payload = json.loads(event.get("payload") or "{}")
        except Exception:
            payload = {}
        subject, body = _format_legacy_event(event_type, payload)

    # -- Determine channels --------------------------------------------------
    try:
        payload_data = json.loads(event.get("payload") or "{}")
    except Exception:
        payload_data = {}

    is_broadcast = payload_data.get("broadcast", False)
    channels = payload_data.get("notify_channels")
    if channels is None:
        if PREFER_PORTAL and PORTAL_URL and payload_data.get("customer_id"):
            channels = ["in_app", "email"]
        else:
            channels = ["email"]

    log.info(
        f"Dispatching P{priority} {event_type} -> {recipient} "
        f"(retry={event.get('retry_count', 0)}, channels={channels})"
    )

    # -- Broadcast path -------------------------------------------------------
    if is_broadcast:
        portal_ok = dispatch_broadcast(event)
        email_ok  = send_email(recipient, subject, body) if recipient else False
        success   = portal_ok or email_ok
    else:
        success = False
        if "in_app" in channels:
            if dispatch_portal_notification(event):
                success = True
        if "email" in channels and recipient:
            if send_email(recipient, subject, body):
                success = True

    if success:
        db_mark_sent(event_id)
    else:
        retry_count = (event.get("retry_count") or 0) + 1
        # Permanent errors (no recipient, config missing) skip retries
        is_permanent = (recipient is None) or (not RESEND_API_KEY and "email" in channels)
        if is_permanent or retry_count >= MAX_RETRIES:
            reason = "permanent error" if is_permanent else f"max retries ({MAX_RETRIES})"
            db_mark_failed(event_id, reason)
            log.error(
                f"FAILED ({reason}): "
                f"{event_type} -> {recipient}"
            )
        else:
            db_requeue(event_id, "send_failed")
            log.warning(
                f"Send failed, requeued ({retry_count}/{MAX_RETRIES}): {event_type}"
            )

    return success


def _format_legacy_event(event_type: str, payload: dict):
    """Format subject/body for events that predate subject/body columns."""
    if event_type == "silence_alert":
        pi_id     = payload.get("pi_id", "unknown")
        age_hours = payload.get("age_hours", 0)
        severity  = payload.get("severity", "WARN")
        subject = f"[Synthos {severity}] {pi_id} -- silent {age_hours:.1f}h"
        market_note = (
            "Market hours -- trades may be missed."
            if payload.get("market_hours")
            else "Outside market hours."
        )
        body = (
            f"Synthos Alert\n\nPi ID: {pi_id}\n"
            f"Status: SILENT -- {age_hours:.1f} hours since last heartbeat\n"
            f"Severity: {severity}\n\n{market_note}\n\n"
            f"Check Pi power, network, and agent logs."
        )
        return subject, body

    if event_type == "agent_error":
        pi_id = payload.get("pi_id", "unknown")
        error = payload.get("error", "Unknown error")
        subject = f"[Synthos WARN] Agent error on {pi_id}"
        body = (
            f"Synthos Alert -- Agent Error\n\nPi ID: {pi_id}\nError: {error}\n\n"
            f"Agent statuses:\n{json.dumps(payload.get('agents', {}), indent=2)}"
        )
        return subject, body

    # Generic fallback
    subject = f"[Synthos] {event_type.replace('_', ' ').title()}"
    body    = json.dumps(payload, indent=2)
    return subject, body


# -- LEGACY TRIGGER DRAIN -----------------------------------------------------
#
# 2026-05-01 rebuild — drain is now re-pollable (every cycle), accepts
# both the historical strongbox `delivered:false` schema AND the
# `status:pending|retry` schema, and archives processed entries to
# data/scoop_trigger_processed/ instead of leaving them stuck in the
# active file.
#
# Producers should write directly to scoop_queue via _shared_scoop now;
# the trigger file remains as:
#   1. A defensive fallback inside _shared_scoop when DB writes fail
#   2. Backward-compat for any pre-rebuild straggler events still on disk
#
# A `_DEFERRED_PERMANENT` set tracks entries the drain saw and explicitly
# skipped (e.g. matching neither schema, malformed) so we don't re-log
# them every 30s.

PROCESSED_DIR = BASE_DIR / "data" / "scoop_trigger_processed"
_drain_skip_log_sent: set = set()


def _entry_is_pending(e: dict) -> bool:
    """An entry is drainable if EITHER schema marker is set."""
    if not isinstance(e, dict):
        return False
    status = (e.get("status") or "").lower()
    if status in ("pending", "retry"):
        return True
    # Old strongbox schema: {"delivered": false} with no status field
    if "delivered" in e and not e.get("delivered"):
        return True
    return False


def drain_legacy_trigger_file() -> int:
    """
    Drain scoop_trigger.json into the scoop_queue DB table.

    Called every cycle from run_loop. If the file has any drainable
    entries (pending/retry status, OR delivered:false from the old
    strongbox schema), each is migrated to scoop_queue with status
    'PENDING'. After successful migration, the file is moved to
    data/scoop_trigger_processed/<UTC timestamp>.json so the audit trail
    is preserved and the active file resets to empty.
    """
    if not LEGACY_TRIGGER.exists():
        return 0

    try:
        raw = LEGACY_TRIGGER.read_text() or "[]"
        events = json.loads(raw)
        if not isinstance(events, list):
            log.warning("scoop_trigger.json is not a list — leaving alone")
            return 0
    except json.JSONDecodeError as e:
        log.warning(f"scoop_trigger.json malformed JSON ({e}) — leaving alone")
        return 0
    except Exception as e:
        log.warning(f"Could not read scoop_trigger.json: {e}")
        return 0

    if not events:
        return 0

    drainable = [e for e in events if _entry_is_pending(e)]
    if not drainable:
        # File has entries but nothing drainable (e.g., already-delivered,
        # or unknown schema). Log once per file-content-hash to avoid spam.
        sig = hash(json.dumps(events, sort_keys=True))
        if sig not in _drain_skip_log_sent:
            log.info(
                f"scoop_trigger.json has {len(events)} entries, none drainable "
                f"(no status:pending|retry and no delivered:false markers) — "
                f"leaving in place"
            )
            _drain_skip_log_sent.add(sig)
        return 0

    migrated = 0
    for e in drainable:
        try:
            # Both schemas use 'type' (not 'event_type'). Sentinel/vault wrote
            # event_type-style names like 'silence_alert'; strongbox wrote
            # 'backup_failed' style. Both fit.
            event_type = e.get("type") or e.get("event_type") or "legacy_event"
            payload    = e.get("payload", {}) if isinstance(e.get("payload"), dict) else {}
            pi_id      = e.get("pi_id") or payload.get("pi_id")
            audience   = e.get("audience", "internal")
            subject    = e.get("subject", "")
            body       = e.get("body", "")
            source     = e.get("source") or e.get("source_agent") or "legacy_trigger"
            priority   = e.get("priority")
            if priority is None or not isinstance(priority, int) or priority not in (0, 1, 2, 3):
                priority = 2
            # Strongbox-old-schema entries had a top-level 'message' field
            # but no subject/body. Fold that into the body so dispatch
            # produces something readable.
            if not subject and not body and "message" in e:
                subject = f"[Synthos] {source} {event_type} — {pi_id or '?'}"
                body    = f"{source} alert\n\nType: {event_type}\nPi ID: {pi_id}\nMessage: {e['message']}\n"

            eid = str(uuid.uuid4())
            with get_db() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO scoop_queue
                    (id, event_type, priority, audience, pi_id, payload,
                     subject, body, source_agent, status, queued_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """, (eid, event_type, priority, audience, pi_id,
                      json.dumps(payload), subject, body, source,
                      now_iso()))
            migrated += 1
        except Exception as ex:
            log.warning(f"Could not migrate legacy event {e.get('id', '?')[:8]}: {ex}")

    if migrated:
        # Archive the file we just processed and reset the active one.
        try:
            PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
            archive_name = f"scoop_trigger_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json"
            (PROCESSED_DIR / archive_name).write_text(json.dumps(events, indent=2))
            LEGACY_TRIGGER.write_text("[]")
            log.info(
                f"Drained {migrated} legacy event(s) from scoop_trigger.json -> "
                f"scoop_queue; archived original to data/scoop_trigger_processed/"
                f"{archive_name}"
            )
        except Exception as ex:
            # Migration succeeded but archive failed — the entries are in
            # the DB now, so we still want to reset the file to avoid
            # double-processing on next cycle.
            log.warning(f"Archive write failed ({ex}); resetting trigger file anyway")
            try:
                LEGACY_TRIGGER.write_text("[]")
            except Exception:
                pass

    return migrated


# -- MORNING REPORT -----------------------------------------------------------

_morning_report_sent_date = None

def maybe_send_morning_report() -> bool:
    global _morning_report_sent_date
    now   = now_et()
    today = now.date()

    if now.hour != MORNING_REPORT_HOUR:
        return False
    if _morning_report_sent_date == today:
        return False
    if not MORNING_REPORT.exists():
        return False

    try:
        report = json.loads(MORNING_REPORT.read_text())
    except Exception as e:
        log.error(f"Could not read morning_report.json: {e}")
        return False

    if report.get("date", "") != today.isoformat():
        return False

    subject, body = _format_morning_report(report)
    success = send_email(PROJECT_LEAD_EMAIL, subject, body)
    if success:
        _morning_report_sent_date = today
        log.info(f"Morning report delivered for {today}")
    return success


def _format_morning_report(report: dict):
    date    = report.get("date", "?")
    pv      = report.get("portfolio_value", 0)
    pnl     = report.get("realized_pnl", 0)
    wins    = report.get("wins", 0)
    losses  = report.get("losses", 0)
    trades  = report.get("trades_today", 0)
    summary = report.get("summary", "")
    sign    = "+" if pnl >= 0 else ""
    subject = f"[Synthos] Morning Report -- {date}"
    body = (
        f"Synthos Daily Report -- {date}\n\n"
        f"Portfolio:  ${pv:,.2f}\n"
        f"P&L today:  {sign}${pnl:,.2f}\n"
        f"Trades:     {trades} ({wins}W / {losses}L)\n\n"
        f"{summary}\n"
    )
    return subject, body


# -- MAIN LOOP ----------------------------------------------------------------

def run_loop() -> None:
    log.info(f"Scoop v{SYNTHOS_VERSION} started -- polling DB every {POLL_INTERVAL_SEC}s")

    # -- Startup validation ---------------------------------------------------
    fatal = False
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set -- email dispatch will fail")
    if not PROJECT_LEAD_EMAIL:
        log.warning("PROJECT_LEAD_EMAIL not set -- internal alerts will not be delivered")
    if PREFER_PORTAL and not PORTAL_URL:
        log.error("PORTAL_URL required when PREFER_PORTAL_NOTIFICATIONS=true")
        fatal = True
    if fatal:
        log.error("Refusing to start -- fix configuration errors above")
        sys.exit(1)

    if PORTAL_URL:
        mode = "PRIMARY" if PREFER_PORTAL else "SECONDARY"
        if PORTAL_TOKEN:
            log.info(f"Portal: {PORTAL_URL}  mode={mode}  auth=service-token")
        else:
            log.warning(
                f"Portal dispatch DISABLED — PORTAL_URL={PORTAL_URL} "
                f"is configured but PORTAL_TOKEN is not. Retail portal's "
                f"/api/notifications/* endpoints require service-token auth "
                f"that has not been wired up yet (tracked separately). "
                f"Running in EMAIL-ONLY mode for now."
            )
    else:
        log.info("Email-only mode (no PORTAL_URL configured)")

    # First-cycle drain of any stragglers in scoop_trigger.json. The drain
    # runs again every cycle below — no longer one-shot.
    drain_legacy_trigger_file()

    # Ensure composite index for efficient polling
    try:
        with get_db() as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scoop_dispatch "
                "ON scoop_queue(status, priority, created_at)"
            )
    except Exception:
        pass

    while True:
        try:
            # Re-pollable trigger drain — picks up any stragglers from the
            # _shared_scoop fallback path or pre-rebuild legacy writes.
            drain_legacy_trigger_file()

            # Dispatch next highest-priority event
            event = db_get_next_event()
            if event:
                priority = event.get("priority", 2)
                log.info(
                    f"Processing P{priority} {event['event_type']} "
                    f"(retry={event.get('retry_count', 0)})"
                )
                dispatch_event(event)

                # Drain all remaining P0s without sleeping
                if priority == 0:
                    next_event = db_get_next_event()
                    while next_event and next_event.get("priority") == 0:
                        log.info(f"P0 drain: {next_event['event_type']}")
                        dispatch_event(next_event)
                        next_event = db_get_next_event()

            # Morning report
            maybe_send_morning_report()

            # Recover stuck PROCESSING events (crash recovery -- >2 min)
            try:
                stale = (
                    now_utc() - timedelta(minutes=2)
                ).isoformat().replace("+00:00", "Z")
                with get_db() as conn:
                    conn.execute("""
                        UPDATE scoop_queue
                        SET status='RETRY', retry_count=retry_count+1,
                            error_msg='stuck_in_processing'
                        WHERE status='PROCESSING' AND started_at < ?
                    """, (stale,))
            except Exception:
                pass

        except Exception as e:
            log.error(f"Scoop loop error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL_SEC)


# -- CLI ----------------------------------------------------------------------

def show_status() -> None:
    stats = db_queue_stats()
    print(f"\n{'='*50}")
    print(f"SCOOP v{SYNTHOS_VERSION} STATUS")
    print(f"{'='*50}")
    for status, count in sorted(stats.items()):
        print(f"  {status:<20} {count}")
    print(f"  {'Resend':<20} {'configured' if RESEND_API_KEY else 'NOT CONFIGURED'}")
    print(f"  {'Lead email':<20} {PROJECT_LEAD_EMAIL or 'NOT SET'}")
    print(f"{'='*50}\n")


def send_test_email() -> None:
    if not PROJECT_LEAD_EMAIL:
        print("PROJECT_LEAD_EMAIL not set -- cannot send test")
        return
    success = send_email(
        PROJECT_LEAD_EMAIL,
        "[Synthos] Test email from Scoop",
        f"Scoop v{SYNTHOS_VERSION} is configured and working.\n\n"
        f"Company Pi: {os.environ.get('PI_ID', 'unknown')}\n"
        f"Timestamp:  {now_iso()}\n"
    )
    print(f"Test email {'sent' if success else 'FAILED'} to {PROJECT_LEAD_EMAIL}")


# -- GRACEFUL SHUTDOWN --------------------------------------------------------

def handle_shutdown(signum, frame):
    log.info(f"Scoop received signal {signum} -- shutting down")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)


# -- ENTRY POINT --------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scoop -- Mail Agent")
    parser.add_argument("--status", action="store_true",
                        help="Show delivery queue status")
    parser.add_argument("--test", action="store_true",
                        help="Send test email to project lead")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.test:
        send_test_email()
    else:
        run_loop()
