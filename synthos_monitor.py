"""
Synthos Monitor Server
=====================
Runs on a dedicated Pi. Receives heartbeats from all Synthos instances,
serves a command console dashboard, and sends Resend alerts when a Pi goes silent.

.env required:
    RESEND_API_KEY=re_...
    ALERT_FROM=alerts@yourdomain.com
    ALERT_TO=you@youremail.com
    SECRET_TOKEN=some_random_string
    PORT=5000

Client Pi .env:
    MONITOR_URL=http://your-monitor-ip:5000
    MONITOR_TOKEN=same_random_string_as_above
    PI_ID=synthos-pi-1

Heartbeat POST body (JSON):
    {
        "pi_id": "synthos-pi-1",
        "portfolio": 1042.50,
        "agents": { "trend": "active", "momentum": "idle" },
        "email": "customer@example.com",       # optional, stored on first seen
        "label": "John's Pi"                   # optional display name
    }
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, render_template, render_template_string, redirect, session, url_for, make_response
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, "company.env"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit (backup archives grow with customer count)

# ── Config ────────────────────────────────────────────────────────────────────
RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
ALERT_FROM           = os.getenv("ALERT_FROM", "alerts@example.com")
ALERT_TO             = os.getenv("ALERT_TO", "you@example.com")
# SECRET_TOKEN is the server-side env var name.
# MONITOR_TOKEN is the client-side env var name — accept both so
# operators who set only one side don't get silent 401s.
SECRET_TOKEN         = os.getenv("SECRET_TOKEN") or os.getenv("MONITOR_TOKEN", "")
RETAIL_PORTAL_URL    = os.getenv("RETAIL_PORTAL_URL", "http://10.0.0.11:5000")
PORT                 = int(os.getenv("PORT", 5050))
CF_ADMIN_EMAIL = os.getenv("OPERATOR_EMAIL", "").lower().strip()
ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", CF_ADMIN_EMAIL).lower().strip()
ADMIN_PW_HASH  = os.getenv("ADMIN_PASSWORD_HASH", "")
app.secret_key = os.getenv("FLASK_SECRET_KEY", SECRET_TOKEN or __import__('os').urandom(24).hex())
COMPANY_URL          = os.getenv("COMPANY_URL", "").rstrip("/")
PORTAL_TOKEN         = os.getenv('PORTAL_TOKEN', '')
SILENCE_WINDOW_HOURS = 4
ALERT_START_HOUR     = 8
ALERT_END_HOUR       = 20
ET                   = ZoneInfo("America/New_York")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
REGISTRY_FILE = os.path.join(DATA_DIR, ".monitor_registry.json")

# ── Company DB Path ──────────────────────────────────────────────────────────
DB_PATH  = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))
LOG_DIR  = os.path.join(_HERE, "logs")   # synthos-company/logs/

# ── Sentinel Display Bridge ───────────────────────────────────────────────────
SENTINEL_URL   = os.getenv("SENTINEL_URL", "").rstrip("/")
SENTINEL_TOKEN = os.getenv("SENTINEL_TOKEN", "")

_display_bridge = None
try:
    import sentinel_bridge as _display_bridge
    _display_bridge.start_watcher()  # Start drop folder monitor
except ImportError:
    pass  # sentinel_bridge.py not present — display features disabled

DISPLAY_DROP_DIR = os.path.join(os.path.dirname(_HERE), "data", "display_uploads")
os.makedirs(DISPLAY_DROP_DIR, exist_ok=True)


# ── Company Database ──────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    """Thread-safe SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Support DB (separate from company.db to avoid daemon lock contention) ──
SUPPORT_DB_PATH = os.path.join(DATA_DIR, "support.db")

@contextmanager
def _support_conn():
    """Dedicated connection for support/admin tools — never competes with daemon agents."""
    conn = sqlite3.connect(SUPPORT_DB_PATH, timeout=30, check_same_thread=False)
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


def init_support_db():
    """Create support database schema. Separate from company.db."""
    with _support_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS beta_tests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                required_confirmations INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                cleared_at TEXT,
                cancelled_at TEXT,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS company_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                date TEXT NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 0,
                frequency TEXT DEFAULT 'one-time',
                next_renewal TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_key_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL,
                key_name TEXT NOT NULL,
                expires_at TEXT,
                backup_value TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(node, key_name)
            );
            CREATE TABLE IF NOT EXISTS invite_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                recipient_name TEXT,
                recipient_email TEXT,
                sent_at TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );
        """)
    # Migrate data from company.db if support.db tables are empty
    try:
        with _support_conn() as sconn:
            count = sconn.execute("SELECT COUNT(*) FROM beta_tests").fetchone()[0]
        if count == 0:
            with _db_conn() as cconn:
                try:
                    rows = cconn.execute("SELECT * FROM beta_tests").fetchall()
                    if rows:
                        with _support_conn() as sconn:
                            for r in rows:
                                try:
                                    sconn.execute(
                                        "INSERT OR IGNORE INTO beta_tests (id,title,description,required_confirmations,status,created_at,cleared_at) VALUES (?,?,?,?,?,?,?)",
                                        (r['id'],r['title'],r['description'],r['required_confirmations'],r['status'],r['created_at'],r['cleared_at']))
                                except Exception:
                                    pass
                        print(f"[Support DB] Migrated {len(rows)} beta tests from company.db")
                except Exception:
                    pass
            with _db_conn() as cconn:
                try:
                    rows = cconn.execute("SELECT * FROM company_expenses").fetchall()
                    if rows:
                        with _support_conn() as sconn:
                            for r in rows:
                                try:
                                    sconn.execute(
                                        "INSERT OR IGNORE INTO company_expenses (id,category,description,amount,date,recurring,frequency,next_renewal,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                        (r['id'],r['category'],r['description'],r['amount'],r['date'],r['recurring'],r['frequency'],r['next_renewal'],r['created_at']))
                                except Exception:
                                    pass
                        print(f"[Support DB] Migrated {len(rows)} expenses from company.db")
                except Exception:
                    pass
    except Exception as e:
        print(f"[Support DB] Migration check: {e}")
    print(f"[Support DB] Initialized: {SUPPORT_DB_PATH}")


def init_db():
    """Create company node database schema. Idempotent — safe to call on every startup."""
    with _db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scoop_queue (
                id                TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                priority          INTEGER NOT NULL DEFAULT 1,
                subject           TEXT NOT NULL,
                body              TEXT NOT NULL,
                source_agent      TEXT NOT NULL,
                pi_id             TEXT,
                audience          TEXT NOT NULL DEFAULT 'customer',
                correlation_id    TEXT,
                related_ticker    TEXT,
                related_signal_id TEXT,
                payload           TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                queued_at         TEXT NOT NULL,
                dispatched_at     TEXT,
                dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                error_msg         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scoop_status   ON scoop_queue(status);

            CREATE TABLE IF NOT EXISTS pi_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id           TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                portfolio_value REAL,
                cash            REAL,
                realized_gains  REAL,
                open_positions  INTEGER,
                trades_today    INTEGER,
                operating_mode  TEXT,
                trading_mode    TEXT,
                kill_switch     INTEGER,
                payload         TEXT,
                recorded_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pi_events_pi   ON pi_events(pi_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_pi_events_type ON pi_events(event_type, recorded_at);

            CREATE TABLE IF NOT EXISTS api_key_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL,
                key_name TEXT NOT NULL,
                expires_at TEXT,
                backup_value TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(node, key_name)
            );
            CREATE TABLE IF NOT EXISTS invite_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                recipient_name TEXT,
                recipient_email TEXT,
                sent_at TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );
        """)
        # Migration: add columns to scoop_queue that may be missing from older schemas
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scoop_queue)").fetchall()}
        migrations = [
            ("priority",          "ALTER TABLE scoop_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 1"),
            ("audience",          "ALTER TABLE scoop_queue ADD COLUMN audience TEXT NOT NULL DEFAULT 'customer'"),
            ("correlation_id",    "ALTER TABLE scoop_queue ADD COLUMN correlation_id TEXT"),
            ("related_ticker",    "ALTER TABLE scoop_queue ADD COLUMN related_ticker TEXT"),
            ("related_signal_id", "ALTER TABLE scoop_queue ADD COLUMN related_signal_id TEXT"),
            ("payload",           "ALTER TABLE scoop_queue ADD COLUMN payload TEXT"),
            ("subject",           "ALTER TABLE scoop_queue ADD COLUMN subject TEXT NOT NULL DEFAULT ''"),
            ("body",              "ALTER TABLE scoop_queue ADD COLUMN body TEXT NOT NULL DEFAULT ''"),
            ("source_agent",      "ALTER TABLE scoop_queue ADD COLUMN source_agent TEXT NOT NULL DEFAULT ''"),
            ("started_at",        "ALTER TABLE scoop_queue ADD COLUMN started_at TEXT"),
            ("queued_at",         "ALTER TABLE scoop_queue ADD COLUMN queued_at TEXT"),
            ("dispatched_at",     "ALTER TABLE scoop_queue ADD COLUMN dispatched_at TEXT"),
            ("dispatch_attempts", "ALTER TABLE scoop_queue ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0"),
            ("error_msg",         "ALTER TABLE scoop_queue ADD COLUMN error_msg TEXT"),
        ]
        for col, sql in migrations:
            if col not in existing_cols:
                conn.execute(sql)
                print(f"[Monitor] Migration: added scoop_queue.{col}")
        # Refresh column list and create indexes only for columns that exist
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scoop_queue)").fetchall()}
        if "priority" in existing_cols and "queued_at" in existing_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scoop_priority ON scoop_queue(priority, queued_at)")
        elif "priority" in existing_cols and "created_at" in existing_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scoop_priority ON scoop_queue(priority, created_at)")
        if "pi_id" in existing_cols and "queued_at" in existing_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scoop_pi ON scoop_queue(pi_id, queued_at)")
        elif "pi_id" in existing_cols and "created_at" in existing_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scoop_pi ON scoop_queue(pi_id, created_at)")
    print(f"[Monitor] Company DB initialized: {DB_PATH}")


# ── State ─────────────────────────────────────────────────────────────────────
pi_registry   = {}
registry_lock = threading.Lock()
OVERRIDES_FILE = os.path.join(DATA_DIR, ".admin_overrides.json")
admin_overrides = {"trading_gate": "ALL", "operating_mode": "ALL"}

# ── Global Commands ──────────────────────────────────────────────────────────
# Pending commands are stored per-pi_id and popped on next heartbeat response.
pending_commands = {}          # {pi_id: [{"type": "...", "value": "..."}]}
commands_lock    = threading.Lock()


def save_registry():
    """Persist registry to disk so Pi state survives monitor restarts.
    Uses atomic write (temp file + rename) to prevent corruption on crash/restart."""
    try:
        import json as _json
        import tempfile
        serializable = {}
        for pi_id, data in pi_registry.items():
            entry = dict(data)
            entry['last_seen']  = data['last_seen'].isoformat()
            entry['first_seen'] = data.get('first_seen', data['last_seen']).isoformat()
            if 'last_report' in entry:
                entry['last_report'] = entry['last_report']  # already serializable
            serializable[pi_id] = entry
        # Atomic write: write to temp file, then rename (POSIX rename is atomic)
        reg_dir = os.path.dirname(REGISTRY_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=reg_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                _json.dump(serializable, f, indent=2)
            os.replace(tmp_path, REGISTRY_FILE)
        except Exception:
            # Clean up temp file on failure
            try: os.unlink(tmp_path)
            except OSError: pass
            raise
    except Exception as e:
        print(f"[Registry] Save failed: {e}")


def load_registry():
    """Load persisted registry on startup — restores Pi list after reboot."""
    import json as _json
    if not os.path.exists(REGISTRY_FILE):
        return
    try:
        with open(REGISTRY_FILE, 'r') as f:
            data = _json.load(f)
        for pi_id, entry in data.items():
            pi_registry[pi_id] = {
                **entry,
                'last_seen':  datetime.fromisoformat(entry['last_seen']).replace(tzinfo=timezone.utc)
                              if entry['last_seen'].endswith('+00:00') or 'Z' in entry['last_seen']
                              else datetime.fromisoformat(entry['last_seen']).replace(tzinfo=timezone.utc),
                'first_seen': datetime.fromisoformat(entry.get('first_seen', entry['last_seen'])).replace(tzinfo=timezone.utc),
                'alerted':    False,  # reset on restart — re-evaluate silence fresh
            }
        print(f"[Registry] Loaded {len(pi_registry)} Pi(s) from disk")
    except Exception as e:
        print(f"[Registry] Load failed (starting fresh): {e}")


# ── Admin Overrides ──────────────────────────────────────────────────────────
def save_overrides():
    try:
        import json as _json
        with open(OVERRIDES_FILE, 'w') as f:
            _json.dump(admin_overrides, f)
    except Exception as e:
        print(f"[Overrides] Save failed: {e}")

def load_overrides():
    global admin_overrides
    try:
        import json as _json
        with open(OVERRIDES_FILE, 'r') as f:
            admin_overrides.update(_json.load(f))
        print(f"[Overrides] Loaded: trading_gate={admin_overrides['trading_gate']} operating_mode={admin_overrides['operating_mode']}")
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)

def in_alert_window():
    now_et = datetime.now(ET)
    return ALERT_START_HOUR <= now_et.hour < ALERT_END_HOUR

def send_alert(pi_id, last_seen):
    if not RESEND_API_KEY:
        print(f"[ALERT] No Resend key — would have alerted for {pi_id}")
        return
    import json as _json
    elapsed = round((now_utc() - last_seen).total_seconds() / 3600, 1)
    try:
        import requests as _req
        r = _req.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'from':    ALERT_FROM,
                'to':      [ALERT_TO],
                'subject': f"⚠️ Synthos Alert — {pi_id} is silent",
                'html': (
                    f"<h2>Synthos Monitor Alert</h2>"
                    f"<p><strong>{pi_id}</strong> has not sent a heartbeat in "
                    f"<strong>{elapsed} hours</strong>.</p>"
                    f"<p>Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
                    f"<p>Check your Pi.</p>"
                ),
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            print(f"[ALERT] Sent alert for {pi_id}")
        else:
            print(f"[ALERT] Resend error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[ALERT] Resend error: {e}")

def pi_status(data):
    """Returns 'active', 'fault', or 'offline'"""
    age = (now_utc() - data["last_seen"]).total_seconds()
    if age > SILENCE_WINDOW_HOURS * 3600:
        return "offline"
    agents = data.get("agents", {})
    if any(v == "fault" or v == "error" for v in agents.values()):
        return "fault"
    return "active"


# ── Company Auth Helpers ─────────────────────────────────────────────────────
def _cf_authorized():
    """Trust Cloudflare Access — checks Cf-Access-Authenticated-User-Email header."""
    if not CF_ADMIN_EMAIL:
        return False
    cf_email = request.headers.get("Cf-Access-Authenticated-User-Email", "").lower().strip()
    return cf_email == CF_ADMIN_EMAIL


def _token_authorized():
    """Check X-Token header, ?token= query param, or cookie. Timing-safe."""
    import hmac as _hmac_mod
    token = (
        request.headers.get("X-Token", "")
        or request.args.get("token", "")
        or request.cookies.get("company_token", "")
    )
    if not SECRET_TOKEN or not token:
        return False
    return _hmac_mod.compare_digest(token, SECRET_TOKEN)


def _session_authorized():
    """Check Flask session login."""
    return session.get("logged_in") is True

def _authorized():
    """Browser routes: accept session login, Cloudflare Access, or SECRET_TOKEN."""
    return _session_authorized() or _cf_authorized() or _token_authorized()


# ── Silence detection loop ────────────────────────────────────────────────────
def silence_detector():
    while True:
        time.sleep(300)
        if not in_alert_window():
            continue
        with registry_lock:
            for pi_id, data in pi_registry.items():
                age_hours = (now_utc() - data["last_seen"]).total_seconds() / 3600
                if age_hours >= SILENCE_WINDOW_HOURS and not data["alerted"]:
                    if not data.get("silenced"):
                        send_alert(pi_id, data["last_seen"])
                    data["alerted"] = True
                elif age_hours < SILENCE_WINDOW_HOURS and data["alerted"]:
                    data["alerted"] = False


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    pi_id = data.get("pi_id", "unknown")

    with registry_lock:
        existing = pi_registry.get(pi_id, {})
        pi_registry[pi_id] = {
            "last_seen":         now_utc(),
            "first_seen":        existing.get("first_seen", now_utc()),
            "alerted":           False,
            "silenced":          existing.get("silenced", False),
            # Identity + network
            "ip":                request.remote_addr or existing.get("ip", ""),
            "label":             data.get("label",          existing.get("label", pi_id)),
            "email":             data.get("email",          existing.get("email", "")),
            "pi_id":             pi_id,
            # Summary stats
            "portfolio_value":   data.get("portfolio_value", data.get("portfolio", existing.get("portfolio_value", 0.0))),
            "cash":              data.get("cash",            existing.get("cash", 0.0)),
            "realized_gains":    data.get("realized_gains",  existing.get("realized_gains", 0.0)),
            "open_positions":    data.get("open_positions",  existing.get("open_positions", 0)),
            "positions":         data.get("positions",       existing.get("positions", [])),
            "pending_approvals": data.get("pending_approvals", existing.get("pending_approvals", 0)),
            "urgent_flags":      data.get("urgent_flags",   existing.get("urgent_flags", 0)),
            "trades_today":      data.get("trades_today",   existing.get("trades_today", 0)),
            # System
            "agents":            {**existing.get("agents", {}), **data.get("agents", {})},
            "uptime":            data.get("uptime",         existing.get("uptime", None)),
            "uptime_secs":       data.get("uptime_secs",    existing.get("uptime_secs", 0)),
            "operating_mode":    data.get("operating_mode", existing.get("operating_mode", "MANAGED")),
            "trading_mode":      data.get("trading_mode",   existing.get("trading_mode", "PAPER")),
            "kill_switch":       data.get("kill_switch",    existing.get("kill_switch", False)),
            "last_errors":       data.get("last_errors",    existing.get("last_errors", [])),
            # Hardware metrics
            "cpu_percent":    data.get("cpu_percent",    existing.get("cpu_percent")),
            "cpu_count":      data.get("cpu_count",      existing.get("cpu_count")),
            "load_avg":       data.get("load_avg",        existing.get("load_avg")),
            "ram_percent":    data.get("ram_percent",    existing.get("ram_percent")),
            "ram_total_gb":   data.get("ram_total_gb",   existing.get("ram_total_gb")),
            "ram_used_gb":    data.get("ram_used_gb",    existing.get("ram_used_gb")),
            "ram_avail_gb":   data.get("ram_avail_gb",   existing.get("ram_avail_gb")),
            "ram_cached_gb":  data.get("ram_cached_gb",  existing.get("ram_cached_gb")),
            "disk_percent":   data.get("disk_percent",   existing.get("disk_percent")),
            "disk_total_gb":  data.get("disk_total_gb",  existing.get("disk_total_gb")),
            "disk_used_gb":   data.get("disk_used_gb",   existing.get("disk_used_gb")),
            "disk_free_gb":   data.get("disk_free_gb",   existing.get("disk_free_gb")),
            "net_bytes_sent": data.get("net_bytes_sent", existing.get("net_bytes_sent")),
            "net_bytes_recv": data.get("net_bytes_recv", existing.get("net_bytes_recv")),
            "cpu_temp":       data.get("cpu_temp",       existing.get("cpu_temp")),
            "pi_ip":          data.get("pi_ip",          existing.get("pi_ip", request.remote_addr)),
            # History — keep last 48 heartbeat samples for time-series graphs
            "history":           (existing.get("history", []) + [{
                "t":   now_utc().isoformat(),
                "v":   data.get("portfolio_value", data.get("portfolio", 0.0)),
                "cpu": data.get("cpu_percent"),
                "ram": data.get("ram_percent"),
            }])[-1440:],
        }
        save_registry()

    # Deliver any pending global commands to this Pi
    with commands_lock:
        cmds = pending_commands.pop(pi_id, [])

    return jsonify({"status": "ok", "commands": cmds}), 200


@app.route("/api/pi/<pi_id>", methods=["GET"])
def api_pi_detail(pi_id):
    """Full detail for a single Pi — used by modal on click."""
    with registry_lock:
        data = pi_registry.get(pi_id)
    if not data:
        return jsonify({"error": "Pi not found"}), 404
    age_secs = int((now_utc() - data["last_seen"]).total_seconds())
    return jsonify({
        **data,
        "last_seen":  data["last_seen"].isoformat(),
        "first_seen": data["first_seen"].isoformat(),
        "age_secs":   age_secs,
        "status":     pi_status(data),
    }), 200


@app.route("/api/status", methods=["GET"])
def api_status():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    with registry_lock:
        out = {}
        for pi_id, data in pi_registry.items():
            age_secs = int((now_utc() - data["last_seen"]).total_seconds())
            out[pi_id] = {
                "pi_id":             pi_id,
                "label":             data.get("label", pi_id),
                "email":             data.get("email", ""),
                "last_seen":         data["last_seen"].isoformat(),
                "age_secs":          age_secs,
                "status":            pi_status(data),
                "portfolio_value":   data.get("portfolio_value", data.get("portfolio", 0.0)),
                "cash":              data.get("cash", 0.0),
                "realized_gains":    data.get("realized_gains", 0.0),
                "open_positions":    data.get("open_positions", 0),
                "pending_approvals": data.get("pending_approvals", 0),
                "urgent_flags":      data.get("urgent_flags", 0),
                "trades_today":      data.get("trades_today", 0),
                "agents":            data.get("agents", {}),
                "uptime":            data.get("uptime", None),
                "operating_mode":    data.get("operating_mode", "MANAGED"),
                "trading_mode":      data.get("trading_mode", "PAPER"),
                "kill_switch":       data.get("kill_switch", False),
                "cpu_percent":    data.get("cpu_percent"),
                "cpu_count":      data.get("cpu_count"),
                "load_avg":       data.get("load_avg"),
                "ram_percent":    data.get("ram_percent"),
                "ram_total_gb":   data.get("ram_total_gb"),
                "ram_used_gb":    data.get("ram_used_gb"),
                "ram_avail_gb":   data.get("ram_avail_gb"),
                "ram_cached_gb":  data.get("ram_cached_gb"),
                "disk_percent":   data.get("disk_percent"),
                "disk_total_gb":  data.get("disk_total_gb"),
                "disk_used_gb":   data.get("disk_used_gb"),
                "disk_free_gb":   data.get("disk_free_gb"),
                "net_bytes_sent": data.get("net_bytes_sent"),
                "net_bytes_recv": data.get("net_bytes_recv"),
                "cpu_temp":       data.get("cpu_temp"),
                "history":        data.get("history", []),
                "silenced":       data.get("silenced", False),
            }
    return jsonify(out), 200


@app.route("/api/delete/<pi_id>", methods=["DELETE"])
def delete_pi(pi_id):
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    with registry_lock:
        if pi_id in pi_registry:
            del pi_registry[pi_id]
            save_registry()
            return jsonify({"deleted": pi_id}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/report", methods=["POST"])
def receive_report():
    """
    Receive a daily performance report POST from a Synthos Pi.
    Stores the latest report per pi_id for display in the console.
    Client Pi posts this at end of trading day with portfolio summary.

    Expected JSON body:
    {
        "pi_id": "synthos-pi-1",
        "date": "2026-03-22",
        "portfolio_value": 107.34,
        "realized_pnl": 4.21,
        "open_positions": 2,
        "trades_today": 1,
        "wins": 1,
        "losses": 0,
        "summary": "Free-text summary from agent"
    }
    """
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    pi_id = data.get("pi_id", "unknown")

    with registry_lock:
        if pi_id not in pi_registry:
            pi_registry[pi_id] = {
                "last_seen":  now_utc(),
                "portfolio":  data.get("portfolio_value", 0.0),
                "agents":     {},
                "email":      "",
                "label":      pi_id,
                "alerted":    False,
                "silenced":   False,
                "first_seen": now_utc(),
            }
        pi_registry[pi_id]["last_report"] = {
            "received_at":    now_utc().isoformat(),
            "date":           data.get("date", now_utc().strftime("%Y-%m-%d")),
            "portfolio_value": data.get("portfolio_value", 0.0),
            "realized_pnl":   data.get("realized_pnl", 0.0),
            "open_positions": data.get("open_positions", 0),
            "trades_today":   data.get("trades_today", 0),
            "wins":           data.get("wins", 0),
            "losses":         data.get("losses", 0),
            "summary":        data.get("summary", ""),
        }

    return jsonify({"status": "ok"}), 200


@app.route("/api/reports", methods=["GET"])
def api_reports():
    """Return latest daily report for each Pi."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    with registry_lock:
        out = {}
        for pi_id, data in pi_registry.items():
            if "last_report" in data:
                out[pi_id] = data["last_report"]
    return jsonify(out), 200


@app.route("/api/enqueue", methods=["POST"])
def api_enqueue():
    """
    Receive a Scoop queue event from a retail Pi agent or monitor proxy.
    Auth: X-Token header must match SECRET_TOKEN.
    Required fields: event_type, priority, subject, body, source_agent
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    required = ["event_type", "priority", "subject", "body", "source_agent"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        priority = int(data["priority"])
    except (ValueError, TypeError):
        return jsonify({"error": "priority must be an integer 0-3"}), 400

    if priority not in (0, 1, 2, 3):
        return jsonify({"error": "priority must be 0, 1, 2, or 3"}), 400

    eid       = str(uuid.uuid4())
    queued_at = datetime.now(timezone.utc).isoformat()
    payload   = data.get("payload", {})

    try:
        with _db_conn() as conn:
            conn.execute(
                """INSERT INTO scoop_queue
                   (id, event_type, priority, subject, body, source_agent,
                    pi_id, audience, correlation_id, related_ticker,
                    related_signal_id, payload, status, queued_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eid,
                    str(data["event_type"]),
                    priority,
                    str(data["subject"]),
                    str(data["body"]),
                    str(data["source_agent"]),
                    data.get("pi_id"),
                    data.get("audience", "customer"),
                    data.get("correlation_id"),
                    data.get("related_ticker"),
                    data.get("related_signal_id"),
                    json.dumps(payload) if isinstance(payload, dict) else "{}",
                    "pending",
                    queued_at,
                ),
            )
        print(
            f"[ENQUEUE] {data['event_type']} P{priority} from {data['source_agent']} "
            f"pi={data.get('pi_id', '?')} id={eid[:8]}"
        )
        return jsonify({"ok": True, "id": eid, "priority": priority}), 200

    except Exception as e:
        print(f"[ENQUEUE] DB write failed: {e}")
        return jsonify({"ok": False, "error": f"DB write failed: {str(e)[:120]}"}), 500

# ── Global Command Routes ────────────────────────────────────────────────────
def _queue_command(cmd_type, value, targets="all"):
    """Queue a command for target Pis. Delivered on next heartbeat response."""
    cmd = {"type": cmd_type, "value": value, "queued_at": now_utc().isoformat()}
    with commands_lock:
        if targets == "all":
            with registry_lock:
                target_ids = list(pi_registry.keys())
        else:
            target_ids = targets if isinstance(targets, list) else [targets]
        for pid in target_ids:
            pending_commands.setdefault(pid, []).append(cmd)
    return target_ids


@app.route("/api/command/trading-mode", methods=["POST"])
def cmd_trading_mode():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").upper()
    if mode not in ("PAPER", "LIVE"):
        return jsonify({"error": "mode must be PAPER or LIVE"}), 400
    targets = _queue_command("set_trading_mode", mode, data.get("targets", "all"))
    return jsonify({"ok": True, "command": "set_trading_mode", "value": mode,
                    "queued_for": targets}), 200


@app.route("/api/command/kill-switch", methods=["POST"])
def cmd_kill_switch():
    """Admin kill switch — halt v2.

    Posts directly to each retail Pi's /api/admin/halt-agent endpoint so
    the halt takes effect on the NEXT trader invocation (seconds). Also
    queues a legacy set_kill_switch command for any retail Pi running
    pre-halt-v2 code (backwards-compat; scheduled to be removed once
    fleet is fully migrated).

    Accepts:
        active (bool)             required
        reason (str)              optional, recorded in system_halt + log
        expected_return (str)     optional, shown to customers in banner
        targets (list|"all")      legacy — which Pis to target for queue
    """
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    active = bool(data.get("active", True))
    reason = (data.get("reason") or "").strip()[:300] or None
    expected_return = (data.get("expected_return") or "").strip()[:80] or None

    # Fire the direct halt-v2 POST to every known retail Pi
    direct_results = []
    try:
        import requests as _req
        with registry_lock:
            _pis = dict(pi_registry)
        for pi_id, pi in _pis.items():
            pi_ip = pi.get("pi_ip")
            if not pi_ip or pi_id.startswith("pi4b") or pi_id.startswith("pi2w"):
                continue
            try:
                r = _req.post(
                    f"http://{pi_ip}:5001/api/admin/halt-agent",
                    json={
                        "active": active,
                        "reason": reason,
                        "expected_return": expected_return,
                        "admin_id": "monitor",
                    },
                    headers={"X-Token": SECRET_TOKEN},
                    timeout=5,
                )
                direct_results.append({
                    "pi_id": pi_id, "pi_ip": pi_ip,
                    "status": r.status_code,
                    "body":   (r.text or "")[:200],
                })
            except Exception as _e:
                direct_results.append({
                    "pi_id": pi_id, "pi_ip": pi_ip, "error": str(_e)[:120],
                })
    except Exception:
        pass

    # Legacy queue — backwards-compat for any Pi on old code
    try:
        targets = _queue_command("set_kill_switch", active, data.get("targets", "all"))
    except Exception:
        targets = []

    return jsonify({
        "ok": True,
        "command": "set_kill_switch",
        "value": active,
        "direct": direct_results,        # halt-v2 POST results per Pi
        "queued_for": targets,           # legacy queue, secondary
    }), 200


@app.route("/api/command/operating-mode", methods=["POST"])
def cmd_operating_mode():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").upper()
    if mode not in ("MANAGED", "AUTOMATIC"):
        return jsonify({"error": "mode must be MANAGED or AUTOMATIC"}), 400
    targets = _queue_command("set_operating_mode", mode, data.get("targets", "all"))
    return jsonify({"ok": True, "command": "set_operating_mode", "value": mode,
                    "queued_for": targets}), 200


@app.route("/api/commands/pending", methods=["GET"])
def cmd_pending():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    with commands_lock:
        return jsonify(dict(pending_commands)), 200




# ── Command Center — Manual Agent Triggers ────────────────────────────────────
import subprocess as _sp

_AGENT_DIR = "/home/pi516gb/synthos/synthos_build/agents"
_SRC_DIR   = "/home/pi516gb/synthos/synthos_build/src"
_LOG_FILE  = "/home/pi516gb/synthos/synthos_build/logs/manual_run.log"

_COMMAND_WHITELIST = {
    "news_overnight":  {"cmd": f"cd {_AGENT_DIR} && python3 retail_news_agent.py --session=overnight",
                        "label": "News Agent (Overnight)"},
    "news_market":     {"cmd": f"cd {_AGENT_DIR} && python3 retail_news_agent.py --session=open",
                        "label": "News Agent (Market)"},
    "sentiment":       {"cmd": f"cd {_AGENT_DIR} && python3 retail_market_sentiment_agent.py",
                        "label": "Sentiment Agent"},
    "trade":           {"cmd": f"cd {_SRC_DIR} && python3 retail_scheduler.py --session=trade",
                        "label": "Trade Logic"},
    "screener":        {"cmd": f"cd {_AGENT_DIR} && python3 retail_sector_screener.py",
                        "label": "Sector Screener"},
    "prep_session":    {"cmd": f"cd {_SRC_DIR} && python3 retail_scheduler.py --session=prep",
                        "label": "Prep Session"},
    "open_session":    {"cmd": f"cd {_SRC_DIR} && python3 retail_scheduler.py --session=open",
                        "label": "Open Session"},
    "midday_session":  {"cmd": f"cd {_SRC_DIR} && python3 retail_scheduler.py --session=midday",
                        "label": "Midday Session"},
    "close_session":   {"cmd": f"cd {_SRC_DIR} && python3 retail_scheduler.py --session=close",
                        "label": "Close Session"},
}


@app.route("/api/command/run-agent", methods=["POST"])
def cmd_run_agent():
    """Fire-and-forget: launch a whitelisted agent on SentinelRetail via SSH."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    if action not in _COMMAND_WHITELIST:
        return jsonify({"error": f"unknown action: {action}",
                        "allowed": list(_COMMAND_WHITELIST.keys())}), 400
    entry = _COMMAND_WHITELIST[action]
    ssh_cmd = f"nohup bash -c '{entry['cmd']}' >> {_LOG_FILE} 2>&1 &"
    try:
        _sp.Popen(
            ["ssh", "-o", "ConnectTimeout=5", "SentinelRetail", ssh_cmd],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        return jsonify({"ok": True, "action": action,
                        "message": f"{entry['label']} started"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/command/wave-override", methods=["POST"])
def cmd_wave_override():
    """Push wave animation override to retail portal."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    override_json = json.dumps({
        "override": data.get("override", False),
        "color": data.get("color"),
        "amplitude": data.get("amplitude"),
        "speed": data.get("speed"),
        "frequency": data.get("frequency"),
        "direction": data.get("direction"),
    })
    import subprocess as _sp
    try:
        # Pipe JSON via stdin to avoid shell quote escaping
        _sp.run(
            ["ssh", "-o", "ConnectTimeout=5", "SentinelRetail",
             "tee", "/home/pi516gb/synthos/synthos_build/.wave_override"],
            input=override_json.encode(), capture_output=True, timeout=10,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/command/agent-status", methods=["GET"])
def cmd_agent_status():
    """Return recent AGENT_START / AGENT_COMPLETE events from pi5."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        cid = "30eff008-c27a-4c71-a788-05f883e4e3a0"
        db_path = f"/home/pi516gb/synthos/synthos_build/data/customers/{cid}/signals.db"
        sql = (
            "SELECT event, agent, timestamp FROM system_log "
            "WHERE event IN ('AGENT_START','AGENT_COMPLETE') "
            "ORDER BY timestamp DESC LIMIT 10"
        )
        result = _sp.run(
            ["ssh", "-o", "ConnectTimeout=5", "SentinelRetail",
             f"sqlite3 -json '{db_path}' \"{sql}\""],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json as _json
            events = _json.loads(result.stdout)
            return jsonify({"ok": True, "events": events}), 200
        return jsonify({"ok": True, "events": []}), 200
    except Exception as e:
        return jsonify({"ok": True, "events": [], "error": str(e)}), 200

# ── Admin Override API ────────────────────────────────────────────────────────
@app.route("/api/admin-override", methods=["GET", "POST"])
def api_admin_override():
    if request.method == "GET":
        return jsonify(admin_overrides), 200

    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not (request.cookies.get("auth") == SECRET_TOKEN):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    tg = data.get("trading_gate", admin_overrides["trading_gate"]).upper()
    om = data.get("operating_mode", admin_overrides["operating_mode"]).upper()

    if tg not in ("PAPER", "LIVE", "ALL"):
        return jsonify({"ok": False, "error": "trading_gate must be PAPER, LIVE, or ALL"}), 400
    if om not in ("MANAGED", "AUTOMATIC", "ALL"):
        return jsonify({"ok": False, "error": "operating_mode must be MANAGED, AUTOMATIC, or ALL"}), 400

    admin_overrides["trading_gate"] = tg
    admin_overrides["operating_mode"] = om
    admin_overrides["updated_at"] = now_utc().isoformat()
    save_overrides()

    # Push to all registered retail Pis
    import requests as _req
    pushed = []
    errors = []
    with registry_lock:
        pis = list(pi_registry.items())
    for pi_id, pi_data in pis:
        if pi_id == os.environ.get("PI_ID", ""):
            continue
        ip = pi_data.get("ip", "")
        if not ip:
            continue
        port = 5001
        try:
            r = _req.post(
                f"http://{ip}:{port}/api/admin-override",
                headers={"X-Token": SECRET_TOKEN, "Content-Type": "application/json"},
                json={"trading_gate": tg, "operating_mode": om},
                timeout=5,
            )
            if r.ok:
                pushed.append(pi_id)
            else:
                errors.append(f"{pi_id}: {r.status_code}")
        except Exception as e:
            errors.append(f"{pi_id}: {e}")

    print(f"[Override] trading_gate={tg} operating_mode={om} pushed={pushed} errors={errors}")
    return jsonify({"ok": True, "pushed_to": pushed, "errors": errors}), 200


# ── Silence Toggle API ────────────────────────────────────────────────────────
@app.route("/api/silence/<pi_id>", methods=["POST"])
def api_silence_toggle(pi_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    with registry_lock:
        if pi_id not in pi_registry:
            return jsonify({"error": "not found"}), 404
        pi_registry[pi_id]["silenced"] = not pi_registry[pi_id].get("silenced", False)
        silenced = pi_registry[pi_id]["silenced"]
        save_registry()
    print(f"[Silence] {pi_id} silenced={silenced}")
    return jsonify({"ok": True, "silenced": silenced}), 200


# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --green:#00f5d4;--red:#ff4b6e;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

/* HEADER */
.header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:56px;
  display:flex;align-items:center;gap:12px;
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
          color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4);flex-shrink:0}
.header-sub{font-size:11px;color:var(--muted);font-family:var(--mono)}
.header-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
           background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
           font-size:10px;font-weight:600;color:var(--teal);letter-spacing:0.05em}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);
          box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(0.8)}}

/* PAGE */
.page{max-width:1400px;margin:0 auto;padding:20px 24px}

/* FLEET STATS */
.fleet-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.fleet-card{
  padding:14px 16px;border-radius:14px;
  border:1px solid var(--border);background:var(--surface);
  position:relative;overflow:hidden;
}
.fleet-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.fc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.fc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.fc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.fc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.fleet-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.fleet-val{font-size:24px;font-weight:700;letter-spacing:-0.5px}
.fc-teal .fleet-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.fc-purple .fleet-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.fc-amber .fleet-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.fc-pink .fleet-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.fleet-sub{font-size:10px;color:var(--muted);margin-top:3px}

/* TWO COLUMN */
.two-col{display:grid;grid-template-columns:1fr 380px;gap:16px;margin-bottom:20px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* GLOBAL COMMANDS */
.cmd-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-top:14px}
.cmd-panel-hdr{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.cmd-panel-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.cmd-section{padding:10px 14px;border-bottom:1px solid var(--border)}
.cmd-section:last-child{border-bottom:none}
.cmd-label{font-size:10px;font-weight:600;color:var(--muted);letter-spacing:0.04em;text-transform:uppercase;margin-bottom:6px}
.cmd-row{display:flex;gap:6px}
.cmd-btn{flex:1;padding:6px 10px;font-size:10px;font-weight:700;font-family:var(--mono);color:var(--text);
         border:1px solid var(--border);border-radius:8px;background:var(--surface2);color:var(--muted);
         cursor:pointer;transition:all 0.15s;text-transform:uppercase;letter-spacing:0.05em}
.cmd-btn:hover{border-color:var(--teal);color:var(--teal);background:rgba(0,245,212,0.06)}
.cmd-btn.active-teal{border-color:var(--teal);color:var(--teal);background:rgba(0,245,212,0.1);box-shadow:0 0 8px rgba(0,245,212,0.15)}
.cmd-btn.active-amber{border-color:var(--amber);color:var(--amber);background:rgba(255,179,71,0.1);box-shadow:0 0 8px rgba(255,179,71,0.15)}
.cmd-btn.active-pink{border-color:var(--pink);color:var(--pink);background:rgba(255,75,110,0.1);box-shadow:0 0 8px rgba(255,75,110,0.15)}
.cmd-btn.active-purple{border-color:var(--purple);color:var(--purple);background:rgba(123,97,255,0.1);box-shadow:0 0 8px rgba(123,97,255,0.15)}
.cmd-btn.danger{border-color:rgba(255,75,110,0.3);color:var(--pink)}
.cmd-btn.danger:hover{background:rgba(255,75,110,0.12);border-color:var(--pink)}
.cmd-btn.danger.active-pink{background:rgba(255,75,110,0.18);animation:pulse-pink 2s infinite}
@keyframes pulse-pink{0%,100%{box-shadow:0 0 8px rgba(255,75,110,0.15)}50%{box-shadow:0 0 16px rgba(255,75,110,0.35)}}

/* AGENT FLEET TABLE */
.aft-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-top:14px}
.aft-hdr{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.aft-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.aft-count{font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--teal2);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.aft-scroll{max-height:320px;overflow-y:auto}
.aft-row{display:grid;grid-template-columns:1fr 100px 70px 70px;gap:4px;padding:7px 14px;border-bottom:1px solid var(--border);align-items:center;font-size:11px}
.aft-row:last-child{border-bottom:none}
.aft-row.aft-thead{position:sticky;top:0;background:var(--surface);z-index:1;font-size:9px;font-weight:700;
                   letter-spacing:0.06em;text-transform:uppercase;color:var(--dim);padding:8px 14px}
.aft-agent{font-weight:600;font-family:var(--mono);color:var(--text)}
.aft-node{font-size:10px;color:var(--muted);font-family:var(--mono)}
.aft-status{display:flex;align-items:center;gap:5px}
.aft-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.aft-dot.s-active{background:var(--teal);box-shadow:0 0 5px var(--teal)}
.aft-dot.s-idle{background:var(--amber);box-shadow:0 0 4px var(--amber)}
.aft-dot.s-fault{background:var(--pink);box-shadow:0 0 5px var(--pink)}
.aft-dot.s-inactive{background:var(--dim)}
.aft-st{font-size:10px;font-family:var(--mono)}
.aft-st.s-active{color:var(--teal)}.aft-st.s-idle{color:var(--amber)}.aft-st.s-fault{color:var(--pink)}.aft-st.s-inactive{color:var(--dim)}
.aft-time{font-size:10px;color:var(--dim);font-family:var(--mono)}

/* PI GRID */
.pi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}

/* PI CARD */
.pi-card{
  border-radius:18px;border:1px solid var(--border);
  background:var(--surface);
  cursor:pointer;transition:transform 0.18s,box-shadow 0.18s;
  position:relative;overflow:hidden;
}
.pi-card:hover{transform:translateY(-3px);box-shadow:0 12px 40px rgba(0,0,0,0.3)}
.pi-card.online{border-color:rgba(0,245,212,0.15)}
.pi-card.online::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(0,245,212,0.5),transparent);
  box-shadow:0 0 8px rgba(0,245,212,0.3)}
.pi-card.offline{border-color:rgba(255,75,110,0.15)}
.pi-card.offline::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,75,110,0.4),transparent)}
.pi-card.warning{border-color:rgba(255,179,71,0.15)}
.pi-card.warning::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,179,71,0.4),transparent)}

.pi-card-top{padding:14px 14px 10px;display:flex;align-items:flex-start;gap:10px}
.pi-avatar{
  width:42px;height:42px;border-radius:12px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  position:relative;overflow:hidden;
  background:rgba(255,255,255,0.03);
  border:1px solid rgba(255,255,255,0.09);
}
.pi-avatar::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(145deg,rgba(255,255,255,0.13) 0%,transparent 50%)}
/* glass cloud fleet decorations */
.fleet-cloud{position:absolute;bottom:-4px;right:4px;opacity:0.14;pointer-events:none}

.pi-info{flex:1;min-width:0}
.pi-name{font-size:13px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-email{font-size:10px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-id-tag{font-size:9px;font-family:var(--mono);color:var(--dim);margin-top:2px}

.status-dot-wrap{display:flex;align-items:center;gap:4px;flex-shrink:0}
.sdot{width:7px;height:7px;border-radius:50%}
.sdot.online{background:var(--teal);box-shadow:0 0 6px var(--teal)}
.sdot.offline{background:var(--pink);box-shadow:0 0 6px var(--pink)}
.sdot.warning{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.sdot.unknown{background:var(--muted)}
.status-text{font-size:9px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase}
.st-online{color:var(--teal)}
.st-offline{color:var(--pink)}
.st-warning{color:var(--amber)}

.pi-stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border);border-top:1px solid var(--border)}
.pi-stat{padding:9px 12px;background:var(--surface)}
.psl{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.psv{font-size:14px;font-weight:700;color:var(--text)}
.psv.teal{color:var(--teal)}
.psv.amber{color:var(--amber)}
.psv.pink{color:var(--pink)}

.pi-footer{padding:8px 14px;display:flex;align-items:center;gap:8px;
           border-top:1px solid var(--border);background:rgba(255,255,255,0.02)}
.pi-badge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:99px;
          letter-spacing:0.05em;border:1px solid}
.pb-supervised{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.pb-auto{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.2);color:var(--amber)}
.pb-paper{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--muted)}
.pb-kill{background:rgba(255,75,110,0.12);border-color:rgba(255,75,110,0.3);color:var(--pink)}
.pb-pend{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.pi-uptime{margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)}

/* TODO PANEL */
.todo-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden}
.todo-header{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.todo-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.todo-count{font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;
            background:var(--pink2);border:1px solid rgba(255,75,110,0.25);color:var(--pink)}
.todo-count.clear{background:var(--teal2);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.todo-scroll{max-height:400px;overflow-y:auto}
.todo-item{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:8px}
.todo-item:last-child{border-bottom:none}
.tsev{width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:4px}
.ts-crit{background:var(--pink);box-shadow:0 0 4px var(--pink)}
.ts-high{background:var(--amber);box-shadow:0 0 4px var(--amber)}
.ts-med{background:var(--purple)}
.ts-low{background:var(--muted)}
.todo-body{flex:1;min-width:0}
.todo-title-t{font-size:11px;font-weight:600;color:var(--text);margin-bottom:2px}
.todo-meta{font-size:9px;color:var(--muted);font-family:var(--mono)}
.todo-action{font-size:10px;color:rgba(255,255,255,0.45);margin-top:3px;font-style:italic}
.resolve-btn{font-size:9px;font-weight:700;padding:2px 8px;border-radius:6px;
             background:transparent;border:1px solid var(--border);color:var(--muted);
             cursor:pointer;font-family:var(--sans);transition:all 0.15s;flex-shrink:0}
.resolve-btn:hover{border-color:rgba(0,245,212,0.4);color:var(--teal)}
.todo-empty{padding:24px;text-align:center;font-size:11px;color:var(--muted)}

/* SECTION TITLE */
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:var(--muted);margin-bottom:12px;
           display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* MODAL */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);
  z-index:500;display:flex;align-items:center;justify-content:center;
  opacity:0;pointer-events:none;transition:opacity 0.2s;
}
.modal-overlay.show{opacity:1;pointer-events:all}
.modal{
  background:var(--surface);border:1px solid var(--border2);border-radius:24px;
  width:min(860px,95vw);max-height:88vh;overflow:hidden;
  display:flex;flex-direction:column;
  box-shadow:0 24px 80px rgba(0,0,0,0.6);
  transform:scale(0.95);transition:transform 0.2s;
}
.modal-overlay.show .modal{transform:scale(1)}

.modal-header{padding:18px 22px 0;display:flex;align-items:flex-start;gap:14px;flex-shrink:0}
.modal-avatar{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;
              justify-content:center;flex-shrink:0;
              position:relative;overflow:hidden;
              background:rgba(255,255,255,0.03);
              border:1px solid rgba(255,255,255,0.09)}
.modal-avatar::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(145deg,rgba(255,255,255,0.16) 0%,transparent 50%)}
.modal-title-wrap{flex:1}
.modal-name{font-size:18px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}
.modal-email{font-size:12px;color:var(--muted);margin-top:2px}
.modal-id{font-size:10px;font-family:var(--mono);color:var(--dim);margin-top:1px}
.modal-status-row{display:flex;align-items:center;gap:6px;margin-top:6px}
.modal-close{width:32px;height:32px;border-radius:8px;background:rgba(255,255,255,0.06);
             border:1px solid var(--border);color:var(--muted);font-size:16px;
             cursor:pointer;display:flex;align-items:center;justify-content:center;
             flex-shrink:0;transition:all 0.15s}
.modal-close:hover{background:rgba(255,255,255,0.1);color:var(--text)}

.modal-tabs{display:flex;gap:2px;padding:14px 22px 0;border-bottom:1px solid var(--border);flex-shrink:0}
.mtab{padding:7px 14px;border-radius:8px 8px 0 0;font-size:11px;font-weight:600;
      cursor:pointer;border:none;background:transparent;color:var(--muted);
      font-family:var(--sans);transition:all 0.15s;border-bottom:2px solid transparent}
.mtab.active{color:var(--teal);border-bottom-color:var(--teal);background:rgba(0,245,212,0.05)}
.mtab:hover:not(.active){color:var(--text);background:rgba(255,255,255,0.04)}

.modal-body{flex:1;overflow-y:auto;padding:18px 22px}

/* Modal stats */
.modal-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.mstat{padding:12px 14px;border-radius:12px;background:var(--surface2);border:1px solid var(--border)}
.mstat-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.mstat-val{font-size:20px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}
.mstat-sub{font-size:10px;color:var(--muted);margin-top:2px}
.mv-teal{color:var(--teal);text-shadow:0 0 16px rgba(0,245,212,0.3)}
.mv-pink{color:var(--pink);text-shadow:0 0 16px rgba(255,75,110,0.3)}
.mv-amber{color:var(--amber)}

/* Modal graph */
.modal-graph-wrap{border-radius:12px;background:var(--surface2);border:1px solid var(--border);
                  padding:14px 16px;margin-bottom:14px}
.modal-graph-title{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.06em;
                   text-transform:uppercase;margin-bottom:10px}
.modal-graph-canvas{height:100px;position:relative}

/* Positions */
.pos-row{display:flex;align-items:center;gap:10px;padding:8px 0;
         border-bottom:1px solid var(--border)}
.pos-row:last-child{border-bottom:none}
.pos-chip{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;
          justify-content:center;font-size:9px;font-weight:800;flex-shrink:0;
          background:rgba(123,97,255,0.2);border:1px solid rgba(123,97,255,0.25);color:#a78bfa}
.pos-ticker-t{font-size:12px;font-weight:700;color:var(--text)}
.pos-shares-t{font-size:10px;color:var(--muted)}
.pos-pnl-t{margin-left:auto;font-size:13px;font-weight:700}

/* Agent status */
.agent-row{display:flex;align-items:center;gap:8px;padding:7px 0;
           border-bottom:1px solid var(--border)}
.agent-row:last-child{border-bottom:none}
.agent-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.agent-name{font-size:11px;font-weight:600;color:var(--text);flex:1;font-family:var(--mono)}
.agent-status{font-size:10px;color:var(--muted)}

/* Error log */
.error-log{background:rgba(0,0,0,0.3);border-radius:10px;padding:12px;font-family:var(--mono);
           font-size:10px;line-height:1.7;color:#ff9999;max-height:180px;overflow-y:auto;
           border:1px solid rgba(255,75,110,0.15)}
.error-log.empty{color:var(--teal);font-size:11px;text-align:center;padding:20px}

/* NODE ROSTER TABLE */
/* ROSTER + COMMANDS SIDE-BY-SIDE */
.roster-cmd-row{display:grid;grid-template-columns:1fr 256px;gap:16px;margin-bottom:20px;align-items:start}
@media(max-width:960px){.roster-cmd-row{grid-template-columns:1fr}}
.roster-col{min-width:0}
.cmd-col{min-width:0}
.cmd-panel{margin-top:14px}

.node-table-wrap{overflow-x:auto;border-radius:14px;border:1px solid var(--border);background:var(--surface);margin-bottom:0}
.node-thead{display:grid;grid-template-columns:180px 88px 58px 58px 62px 58px 58px 80px 72px 70px;
            padding:8px 14px;background:rgba(255,255,255,0.025);min-width:680px;border-bottom:1px solid var(--border)}
.node-th{font-size:9px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;color:var(--muted)}
.node-row{display:grid;grid-template-columns:180px 88px 58px 58px 62px 58px 58px 80px 72px 70px;
          padding:10px 14px;border-top:1px solid var(--border);align-items:center;
          cursor:pointer;transition:background 0.15s;min-width:680px}
.node-row:hover{background:rgba(255,255,255,0.025)}
.node-cell{font-size:12px;font-family:var(--mono)}
.node-name-cell{display:flex;align-items:center;gap:8px}
.node-micro-av{width:28px;height:28px;border-radius:8px;flex-shrink:0;
               display:flex;align-items:center;justify-content:center;
               background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.09)}
.node-lbl{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:116px}
.node-id-tag{font-size:9px;color:var(--dim);font-family:var(--mono)}
.mc-ok{color:var(--teal)}.mc-warn{color:var(--amber)}.mc-crit{color:var(--pink)}.mc-na{color:var(--dim)}
.node-power{display:flex;gap:4px;align-items:center}
.pwr-btn{width:22px;height:22px;border-radius:6px;border:1px solid var(--border2);
  background:var(--surface2);cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:all .15s;padding:0}
.pwr-btn:hover{border-color:var(--amber);background:rgba(245,166,35,0.08)}
.pwr-btn svg{width:11px;height:11px;color:var(--muted);transition:color .15s}
.pwr-btn:hover svg{color:var(--amber)}
.pwr-btn.danger:hover{border-color:var(--pink);background:rgba(255,75,110,0.08)}
.pwr-btn.danger:hover svg{color:var(--pink)}
.mute-btn{background:none;border:none;cursor:pointer;font-size:13px;padding:2px 3px;border-radius:6px;opacity:0.4;transition:opacity 0.15s;line-height:1}
.mute-btn:hover{opacity:1}
.mute-btn.muted{opacity:1}
/* GRAPH CARDS */
.graph-card{border-radius:14px;border:1px solid var(--border);background:var(--surface);
            padding:16px 16px 10px;margin-bottom:14px}
.graph-card-title{font-size:10px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;
                  color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.graph-canvas-wrap{height:170px;position:relative}

/* Toast */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(60px);
       padding:10px 20px;border-radius:12px;font-size:12px;font-weight:600;
       background:var(--surface);border:1px solid var(--border2);color:var(--text);
       z-index:1000;transition:transform 0.25s;pointer-events:none;
       box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:rgba(0,245,212,0.4);color:var(--teal)}
.toast.err{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* Confirm overlay */
.confirm-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);
                 z-index:600;display:none;align-items:center;justify-content:center}
.confirm-overlay.show{display:flex}
.confirm-box{background:var(--surface);border:1px solid var(--border2);border-radius:16px;
             padding:24px;width:340px;text-align:center}
.confirm-msg{font-size:13px;color:var(--text);margin-bottom:16px;line-height:1.5}
.confirm-btns{display:flex;gap:10px;justify-content:center}
.cbtn{padding:8px 20px;border-radius:9px;font-size:12px;font-weight:600;cursor:pointer;
      font-family:var(--sans);transition:all 0.15s}
.cbtn-cancel{background:transparent;border:1px solid var(--border2);color:var(--muted)}
.cbtn-cancel:hover{color:var(--text)}
.cbtn-confirm{background:var(--pink2);border:1px solid rgba(255,75,110,0.3);color:var(--pink)}
.cbtn-confirm:hover{background:rgba(255,75,110,0.2)}
.appr-filter{
  padding:3px 10px;border-radius:6px;font-size:10px;font-weight:600;
  background:transparent;border:1px solid var(--border);color:var(--muted);
  cursor:pointer;font-family:var(--sans,Inter,system-ui,sans-serif);transition:all 0.15s;
}
.appr-filter:hover{background:rgba(255,255,255,0.04);color:var(--text)}
.appr-filter.active{background:rgba(245,166,35,0.08);border-color:rgba(245,166,35,0.25);color:var(--amber)}
.hamburger-wrap{position:relative}
.hamburger-btn{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;width:32px;height:32px;background:transparent;border:1px solid var(--border);border-radius:8px;cursor:pointer;padding:0}
.hamburger-btn span{display:block;width:14px;height:1.5px;background:var(--muted);border-radius:2px;transition:all .2s}
.hamburger-btn:hover{border-color:var(--border2)}
.hamburger-btn:hover span{background:var(--text)}
.hamburger-menu{display:none;position:absolute;top:calc(100% + 8px);right:0;min-width:180px;background:var(--surface);border:1px solid var(--border2);border-radius:12px;padding:6px;z-index:999;box-shadow:0 12px 40px rgba(0,0,0,0.5)}
.hamburger-menu.open{display:flex;flex-direction:column}
.hmenu-item{padding:8px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s;letter-spacing:0.03em}
.hmenu-item:hover{background:rgba(255,255,255,0.05);color:var(--text)}

/* COMMAND PANEL */
.cmd-trigger{padding:4px 10px;border-radius:99px;background:rgba(123,97,255,0.08);
  border:1px solid rgba(123,97,255,0.25);color:#a78bfa;font-size:10px;font-weight:600;
  cursor:pointer;font-family:var(--sans);letter-spacing:0.05em;transition:all .15s}
.cmd-trigger:hover{background:rgba(123,97,255,0.15);border-color:rgba(123,97,255,0.4);color:#c4b5fd}
.cmd-overlay{position:fixed;top:0;right:0;bottom:0;left:0;background:rgba(0,0,0,0.4);
  z-index:500;opacity:0;pointer-events:none;transition:opacity .2s}
.cmd-overlay.open{opacity:1;pointer-events:all}
.cc-slideout{position:fixed;top:0;right:0;bottom:0;width:320px;background:var(--surface);
  border-left:1px solid var(--border2);z-index:501;
  transform:translateX(100%);transition:transform .25s ease;
  display:flex;flex-direction:column;overflow-y:auto}
.cc-slideout.open{transform:translateX(0)}
.cc-slideout-header{padding:16px 18px 12px;display:flex;align-items:center;gap:8px;
  border-bottom:1px solid var(--border);flex-shrink:0}
.cc-slideout-title{font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  color:var(--text);flex:1}
.cc-slideout-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;
  padding:4px 8px;border-radius:6px;transition:all .15s}
.cc-slideout-close:hover{color:var(--text);background:rgba(255,255,255,0.05)}
.cc-section{padding:14px 18px 8px}
.cc-section-title{font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:10px}
.cc-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cc-btn{padding:10px 12px;border-radius:10px;border:1px solid var(--border);
  background:var(--surface2);cursor:pointer;text-align:left;transition:all .15s;
  font-family:var(--sans);display:flex;flex-direction:column;gap:3px}
.cc-btn:hover{border-color:var(--border2);background:rgba(255,255,255,0.04)}
.cc-btn:active{transform:scale(0.97)}
.cc-btn-label{font-size:11px;font-weight:600;color:var(--text)}
.cc-btn-sub{font-size:9px;color:var(--muted)}
.cc-btn.running{border-color:rgba(123,97,255,0.3);background:rgba(123,97,255,0.06)}
.cc-btn.running .cc-btn-label{color:#a78bfa}
.cc-btn.full{grid-column:1/-1}
.cc-status{padding:14px 18px;border-top:1px solid var(--border);flex-shrink:0}
.cc-status-title{font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:8px}
.cc-event{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:10px;font-family:var(--mono)}
.cc-event-time{color:var(--dim);min-width:42px}
.cc-event-icon{font-size:10px}
.cc-event-name{color:var(--muted);flex:1}

.bell-wrap{position:relative;cursor:pointer}
.bell-btn{background:none;border:1px solid var(--border);border-radius:8px;padding:4px 8px;cursor:pointer;font-size:16px;line-height:1;transition:all .15s;color:var(--muted)}
.bell-btn:hover{border-color:var(--border2);color:var(--text)}
.bell-badge{position:absolute;top:-4px;right:-4px;min-width:16px;height:16px;border-radius:99px;background:var(--pink);color:#fff;font-size:8px;font-weight:800;align-items:center;justify-content:center;padding:0 4px;display:none}
.bell-badge.active{display:flex}
.bell-dropdown{display:none;position:absolute;top:calc(100% + 8px);right:0;min-width:280px;max-height:360px;overflow-y:auto;background:var(--surface);border:1px solid var(--border2);border-radius:12px;padding:6px;z-index:999;box-shadow:0 12px 40px rgba(0,0,0,0.5)}
.bell-dropdown.open{display:flex;flex-direction:column}
.bell-section{font-size:8px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim);padding:6px 10px 4px}
.bell-item{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;font-size:11px;color:var(--muted);text-decoration:none;transition:all .15s}
.bell-item:hover{background:rgba(255,255,255,0.04);color:var(--text)}
.bell-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.bell-dot.pink{background:var(--pink)}.bell-dot.amber{background:var(--amber)}.bell-dot.teal{background:var(--teal)}
.bell-empty{padding:16px;text-align:center;font-size:11px;color:var(--dim)}


/* MONITOR BELL + SLIDE-OUT PANEL */
.mon-bell-wrap{position:relative;width:32px;height:32px;border-radius:50%;background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.2);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .15s}
.mon-bell-wrap:hover{border-color:rgba(255,179,71,0.3);box-shadow:0 0 10px rgba(255,179,71,0.08)}
.mon-bell-wrap:hover svg{color:rgba(255,255,255,0.7)}
.mon-bell-badge{position:absolute;top:-3px;right:-3px;min-width:16px;height:16px;border-radius:99px;background:var(--amber);color:#000;font-size:8px;font-weight:800;line-height:16px;text-align:center;padding:0 4px;display:none;box-shadow:0 0 8px rgba(255,179,71,0.4)}
.mon-bell-badge.active{display:block}
.mon-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:500;display:none}
.mon-overlay.open{display:block}
.mon-panel{position:fixed;top:0;right:0;bottom:0;width:360px;background:var(--surface);border-left:1px solid var(--border2);z-index:501;transform:translateX(100%);transition:transform .25s ease;display:flex;flex-direction:column}
.mon-panel.open{transform:translateX(0)}
.mon-panel-head{padding:14px 18px 0;display:flex;align-items:center;gap:10px;flex-shrink:0}
.mon-panel-title{font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--text);flex:1}
.mon-panel-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:4px 8px;border-radius:6px;transition:all .15s}
.mon-panel-close:hover{color:var(--text);background:rgba(255,255,255,0.05)}
.mon-tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin:10px 18px 0;flex-shrink:0}
.mon-tab{flex:1;padding:8px 0;font-size:10px;font-weight:600;text-align:center;color:var(--muted);background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;font-family:var(--sans);transition:all .15s}
.mon-tab:hover{color:var(--text)}
.mon-tab.active{color:var(--amber);border-bottom-color:var(--amber)}
.mon-tab-body{flex:1;overflow-y:auto;padding:14px 18px}
.mon-notif{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04);cursor:pointer;transition:opacity .15s}
.mon-notif:hover{opacity:0.8}
.mon-notif-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:5px}
.mon-notif-body{flex:1;min-width:0}
.mon-notif-title{font-size:11px;font-weight:600;color:var(--text)}
.mon-notif-sub{font-size:10px;color:var(--muted);margin-top:2px}
/* 2026-04-28 — visual cue when an item has been "marked read".
   Dims it without removing — operator can still see what was in
   the queue, just understands the badge has acknowledged it. */
.mon-notif.mon-notif-read{opacity:0.40}
.mon-notif.mon-notif-read .mon-notif-dot{background:rgba(255,255,255,0.20) !important;box-shadow:none !important}
.mon-notif.mon-notif-read .mon-notif-title{color:var(--muted)}
.mon-empty{text-align:center;padding:30px 0;color:var(--dim);font-size:11px}
/* MARKET ACTIVITY CHART */
.mkt-section{margin-bottom:20px}
.mkt-card{border-radius:14px;border:1px solid var(--border);background:var(--surface);padding:16px}
.mkt-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.mkt-title{font-size:10px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;color:var(--muted)}
.mkt-toggles{display:flex;gap:5px}
.mkt-tog{padding:4px 10px;font-size:9px;font-weight:700;font-family:var(--mono);
  border:1px solid var(--border);border-radius:6px;background:transparent;
  color:var(--dim);cursor:pointer;transition:all .15s;letter-spacing:0.04em}
.mkt-tog:hover{border-color:rgba(255,255,255,0.15);color:var(--muted)}
.mkt-tog.on{color:var(--teal);border-color:rgba(0,245,212,0.25);background:rgba(0,245,212,0.06)}
.mkt-tog.on-pink{color:var(--pink);border-color:rgba(255,75,110,0.25);background:rgba(255,75,110,0.06)}
.mkt-tog.on-amber{color:var(--amber);border-color:rgba(255,179,71,0.25);background:rgba(255,179,71,0.06)}
.mkt-tog.on-purple{color:var(--purple);border-color:rgba(123,97,255,0.25);background:rgba(123,97,255,0.06)}
.mkt-daynav{display:flex;gap:4px}
.mkt-navbtn{padding:4px 10px;font-size:10px;font-weight:700;font-family:var(--mono);
  border:1px solid var(--border);border-radius:6px;background:transparent;
  color:var(--muted);cursor:pointer;transition:all .15s;letter-spacing:0.04em;min-width:30px}
.mkt-navbtn:hover:not(:disabled){border-color:rgba(255,255,255,0.2);color:var(--text);background:rgba(255,255,255,0.03)}
.mkt-navbtn:disabled{opacity:0.3;cursor:not-allowed}
.mkt-canvas{height:220px;position:relative}
.mkt-summary{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px}
@media(max-width:800px){.mkt-summary{grid-template-columns:repeat(3,1fr)}}
.mkt-stat{padding:10px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,0.02);text-align:center}
.mkt-stat-label{font-size:8px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
.mkt-stat-val{font-size:16px;font-weight:700;font-family:var(--mono);letter-spacing:-0.3px}
</style>
</head>
<body>

<!-- DEBUG BANNER — remove after console is confirmed working -->
<div id="dbg-banner" style="background:#1a0a2e;border:2px solid #7b61ff;color:#fff;padding:10px 16px;font-family:monospace;font-size:13px;position:fixed;bottom:0;left:0;right:0;z-index:99999">
  <b>DEBUG</b> | Server rendered: {{ build_ts }} |
  JS status: <span id="dbg-js" style="color:#ff4b6e">NOT RUNNING</span> |
  Fetch status: <span id="dbg-fetch" style="color:#ff4b6e">NOT CALLED</span> |
  piData keys: <span id="dbg-keys" style="color:#ff4b6e">—</span>
</div>
<script>
document.getElementById('dbg-js').textContent = 'RUNNING';
document.getElementById('dbg-js').style.color = '#00f5d4';
</script>

<!-- HEADER -->
<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="header-sub">Monitor</div>
  <div class="header-right">
    <div class="clock" id="clock">--:--:-- ET</div>
    <div class="live-pill"><div class="live-dot"></div><span id="pi-count">No Nodes</span></div>
    <div class="mon-bell-wrap" id="mon-bell-wrap" onclick="toggleMonPanel()">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:rgba(255,179,71,0.7)"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
      <div class="mon-bell-badge" id="mon-bell-badge"></div>
    </div>
    <div class="hamburger-wrap">    <div class="hamburger-wrap">
      <button class="hamburger-btn" onclick="toggleMenu()" id="hbtn" aria-label="Menu">
        <span></span><span></span><span></span>
      </button>
      <div class="hamburger-menu" id="hmenu">
        <a href="/monitor" class="hmenu-item">Monitor</a>
        <a href="/console" class="hmenu-item">Scoop Queue</a>
        <a href="/maintenance" class="hmenu-item">Maintenance</a>
        <a href="/project-status" class="hmenu-item">Project Status</a>
        <a href="/system-architecture" class="hmenu-item">System Architecture</a>
        <a href="/display" class="hmenu-item">Sentinel Display</a>
        <a href="/audit" class="hmenu-item">Auditor</a>
        <a href="/logs" class="hmenu-item">Logs</a>
        <a href="/support-queue" class="hmenu-item">Customer Support</a>
        <a href="/customer-billing" class="hmenu-item">Customer Billing</a>
        <a href="/customer-activity" class="hmenu-item">Customer Activity</a>
        <a href="/pill-usage" class="hmenu-item">Pill Usage</a>
        <a href="/company-finances" class="hmenu-item">Company Finances</a>
        <a href="/reports" class="hmenu-item">Reports</a>
        <div style="height:1px;background:rgba(255,255,255,0.07);margin:4px 0"></div>
        <a href="/logout" class="hmenu-item" style="color:var(--pink)">Sign Out</a>
        <a href="/approvals" class="hmenu-item">Approvals <span id="appr-badge" style="display:none;background:var(--amber);color:#000;font-size:9px;font-weight:800;padding:1px 5px;border-radius:99px;margin-left:3px"></span></a>
      </div>
    </div>
  </div>
</header>


<!-- COMMAND PANEL -->
<div class="cc-overlay" id="cc-overlay" onclick="closeCmdPanel()"></div>
<div class="cc-slideout" id="cc-slideout">
  <div class="cc-slideout-header">
    <div class="cc-slideout-title">Command Center</div>
    <button class="cc-slideout-close" onclick="closeCmdPanel()">&#x2715;</button>
  </div>
  <div class="cc-section">
    <div class="cc-section-title">Agents</div>
    <div class="cc-grid">
      <button class="cc-btn" data-action="news_overnight" onclick="runCommand(this)">
        <span class="cc-btn-label">News</span><span class="cc-btn-sub">Overnight scan</span>
      </button>
      <button class="cc-btn" data-action="news_market" onclick="runCommand(this)">
        <span class="cc-btn-label">News</span><span class="cc-btn-sub">Market hours</span>
      </button>
      <button class="cc-btn" data-action="sentiment" onclick="runCommand(this)">
        <span class="cc-btn-label">Sentiment</span><span class="cc-btn-sub">The Pulse</span>
      </button>
      <button class="cc-btn" data-action="screener" onclick="runCommand(this)">
        <span class="cc-btn-label">Screener</span><span class="cc-btn-sub">Sector scan</span>
      </button>
      <button class="cc-btn full" data-action="trade" onclick="runCommand(this)">
        <span class="cc-btn-label">Trade Logic</span><span class="cc-btn-sub">Run trade evaluation for all customers</span>
      </button>
    </div>
  </div>
  <div class="cc-section">
    <div class="cc-section-title">Sessions</div>
    <div class="cc-grid">
      <button class="cc-btn" data-action="prep_session" onclick="runCommand(this)">
        <span class="cc-btn-label">Prep</span><span class="cc-btn-sub">Screener + News + Sentiment</span>
      </button>
      <button class="cc-btn" data-action="open_session" onclick="runCommand(this)">
        <span class="cc-btn-label">Open</span><span class="cc-btn-sub">Full market open</span>
      </button>
      <button class="cc-btn" data-action="midday_session" onclick="runCommand(this)">
        <span class="cc-btn-label">Midday</span><span class="cc-btn-sub">Sentiment + Trade</span>
      </button>
      <button class="cc-btn" data-action="close_session" onclick="runCommand(this)">
        <span class="cc-btn-label">Close</span><span class="cc-btn-sub">End-of-day wrap</span>
      </button>
    </div>
  </div>
  <div class="cc-status" id="cc-status">
    <div class="cc-status-title">Recent Activity</div>
    <div id="cc-events" style="color:var(--dim);font-size:10px">Loading...</div>
  </div>
</div>

  <!-- MARKET ACTIVITY CHART — today's trading session, 10-min bins -->
  <div class="mkt-section">
    <div class="sec-title">Market Activity
      <span id="mkt-session-label" style="font-size:9px;color:var(--dim);font-weight:400;margin-left:6px">Session</span>
    </div>
    <div class="mkt-card">
      <div class="mkt-header">
        <div class="mkt-toggles">
          <button class="mkt-tog on" id="mt-buys" onclick="mktToggle('buys',this,'on')">Buys</button>
          <button class="mkt-tog on-pink" id="mt-sells" onclick="mktToggle('sells',this,'on-pink')">Sells</button>
          <button class="mkt-tog on-purple" id="mt-net" onclick="mktToggle('net',this,'on-purple')">Net Flow</button>
        </div>
        <div class="mkt-daynav" style="display:flex;gap:4px;align-items:center;margin-left:auto">
          <button class="mkt-navbtn" id="mkt-prev"  onclick="mktPrevDay()"  title="Previous session">&#x25C0;</button>
          <button class="mkt-navbtn" id="mkt-today" onclick="mktToday()"    title="Jump to current session" style="min-width:84px">Today</button>
          <button class="mkt-navbtn" id="mkt-next"  onclick="mktNextDay()"  title="Next session">&#x25B6;</button>
        </div>
      </div>
      <div class="mkt-canvas"><canvas id="mkt-chart"></canvas></div>
      <div class="mkt-summary">
        <div class="mkt-stat"><div class="mkt-stat-label">Total Buys</div><div class="mkt-stat-val" id="ms-buys" style="color:var(--teal)">$0</div></div>
        <div class="mkt-stat"><div class="mkt-stat-label">Total Sells</div><div class="mkt-stat-val" id="ms-sells" style="color:var(--pink)">$0</div></div>
        <div class="mkt-stat"><div class="mkt-stat-label">Net Flow</div><div class="mkt-stat-val" id="ms-net" style="color:var(--muted)">$0</div></div>
        <div class="mkt-stat"><div class="mkt-stat-label">Active Now</div><div class="mkt-stat-val" id="ms-active" style="color:var(--teal)">0</div></div>
        <div class="mkt-stat"><div class="mkt-stat-label">Peak Sessions</div><div class="mkt-stat-val" id="ms-peak" style="color:var(--amber)">0</div></div>
      </div>
    </div>
  </div>

  <!-- USER SESSIONS CHART — 24h rolling by default, prior-day when
       the market card's date nav is on a previous session -->
  <div class="mkt-section">
    <div class="sec-title">User Sessions
      <span id="sess-window-label" style="font-size:9px;color:var(--dim);font-weight:400;margin-left:6px">24h</span>
    </div>
    <div class="mkt-card">
      <div class="mkt-canvas"><canvas id="sess-chart"></canvas></div>
    </div>
  </div>

<!-- MONITOR SLIDE-OUT PANEL -->
<div class="mon-overlay" id="mon-overlay" onclick="closeMonPanel()"></div>
<div class="mon-panel" id="mon-panel">
  <div class="mon-panel-head">
    <div class="mon-panel-title">Control Center</div>
    <button class="mon-panel-close" id="mon-mark-all-read" onclick="markAllBellRead()"
            style="background:none;border:1px solid rgba(255,255,255,0.10);color:var(--muted);font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;padding:4px 10px;margin-right:4px;display:none"
            title="Hide the bell badge until something new arrives. Items stay visible in the list.">Mark read</button>
    <button class="mon-panel-close" onclick="closeMonPanel()">&#x2715;</button>
  </div>
  <div class="mon-tabs">
    <button class="mon-tab active" id="mon-tab-notif" onclick="switchMonTab('notif')">Notifications</button>
    <button class="mon-tab" id="mon-tab-cmd" onclick="switchMonTab('cmd')">Commands</button>
    <button class="mon-tab" id="mon-tab-wave" onclick="switchMonTab('wave')">Wave</button>
  </div>
  <div class="mon-tab-body" id="mon-tab-content"></div>
</div>

<!-- TOAST -->
</div><!-- /controls-wrap -->
<div class="toast" id="toast"></div>


<!-- CONFIRM -->
<div class="confirm-overlay" id="confirm-overlay">
  <div class="confirm-box">
    <div class="confirm-msg" id="confirm-msg"></div>
    <div class="confirm-btns">
      <button class="cbtn cbtn-cancel" onclick="cancelDelete()">Cancel</button>
      <button class="cbtn cbtn-confirm" onclick="confirmDelete()">Remove</button>
    </div>
  </div>
</div>

<!-- MODAL -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal" id="modal">
    <div class="modal-header" id="modal-header">
      <div class="modal-avatar av-teal" id="modal-avatar">--</div>
      <div class="modal-title-wrap">
        <div class="modal-name" id="modal-name">Loading...</div>
        <div class="modal-email" id="modal-email"></div>
        <div class="modal-id" id="modal-id"></div>
        <div class="modal-status-row" id="modal-status-row"></div>
      </div>
      <button class="modal-close" onclick="closeModalBtn()">✕</button>
    </div>
    <div class="modal-tabs">
      <button class="mtab active" onclick="switchTab('overview',this)">Overview</button>
      <button class="mtab" onclick="switchTab('performance',this)">Performance</button>
      <button class="mtab" onclick="switchTab('logs',this)">Logs</button>
      <button class="mtab" onclick="switchTab('admin',this)">Admin</button>
    </div>
    <div class="modal-body" id="modal-body">
      <div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>
    </div>
  </div>
</div>

<!-- PAGE -->
<div class="page">

  <!-- FLEET STATS -->
  <div class="fleet-grid">
    <!-- Nodes Online -->
    <div class="fleet-card fc-teal">
      <div class="fleet-label">Nodes Online</div>
      <div class="fleet-val" id="fl-online">0</div>
      <div class="fleet-sub" id="fl-total">of 0 registered</div>
      <svg class="fleet-cloud" viewBox="0 0 54 38" xmlns="http://www.w3.org/2000/svg" width="54" height="38">
        <circle cx="38" cy="12" r="9" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.2"/>
        <g stroke="rgba(255,255,255,1)" stroke-width="1.1" stroke-linecap="round">
          <line x1="38" y1="1" x2="38" y2="0"/><line x1="44.5" y1="5.5" x2="45.5" y2="4.5"/>
          <line x1="48" y1="12" x2="50" y2="12"/><line x1="31.5" y1="5.5" x2="30.5" y2="4.5"/>
          <line x1="28" y1="12" x2="26" y2="12"/>
        </g>
        <path d="M3,29 Q3,22 10,22 Q9,15 18,15 Q24,15 26,19 Q33,18 33,24 Q37,24 37,29 Q37,33 33,33 L7,33 Q3,33 3,29 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
      </svg>
    </div>
    <!-- Active Alerts -->
    <div class="fleet-card fc-pink">
      <div class="fleet-label">Active Alerts</div>
      <div class="fleet-val" id="fl-issues">0</div>
      <div class="fleet-sub">Open issues</div>
      <svg class="fleet-cloud" viewBox="0 0 44 40" xmlns="http://www.w3.org/2000/svg" width="44" height="40">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <path d="M21,28 L18,35 L21,34.5 L19.5,40 L25.5,32 L22,32.5 Z" fill="rgba(255,255,255,1)" stroke="none"/>
      </svg>
    </div>
    <!-- Fleet Agents -->
    <div class="fleet-card fc-teal">
      <div class="fleet-label">Fleet Agents</div>
      <div class="fleet-val" id="fl-agents">—</div>
      <div class="fleet-sub" id="fl-agents-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <g fill="rgba(255,255,255,0.85)" stroke="none">
          <circle cx="14" cy="18" r="2.5"/><circle cx="22" cy="18" r="2.5"/><circle cx="30" cy="18" r="2.5"/>
        </g>
      </svg>
    </div>
    <!-- Trading Mode -->
    <div class="fleet-card fc-amber">
      <div class="fleet-label">Trading Mode</div>
      <div class="fleet-val" id="fl-trading" style="font-size:20px">—</div>
      <div class="fleet-sub" id="fl-trading-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <g stroke="rgba(255,255,255,0.9)" stroke-width="1.2" fill="none" stroke-linecap="round">
          <polyline points="11,24 16,17 21,21 29,13"/><circle cx="29" cy="13" r="1.5" fill="rgba(255,255,255,0.9)"/>
        </g>
      </svg>
    </div>
  </div>

  <!-- ROSTER + COMMANDS ROW -->
  <div class="roster-cmd-row">

    <!-- ACCOUNT APPROVALS -->
    <div id="approvals-section" style="display:none;margin-bottom:20px">
      <div class="sec-title" style="display:flex;align-items:center;justify-content:space-between">
        Account Approval Queue
        <div style="display:flex;gap:6px">
          <button class="appr-filter active" id="af-PENDING" onclick="filterAppr('PENDING')">Pending</button>
          <button class="appr-filter" id="af-APPROVED" onclick="filterAppr('APPROVED')">Approved</button>
          <button class="appr-filter" id="af-REJECTED" onclick="filterAppr('REJECTED')">Rejected</button>
          <button class="appr-filter" id="af-ALL" onclick="filterAppr(null)">All</button>
        </div>
      </div>
      <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;background:var(--surface)">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Name</th>
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Email</th>
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Phone</th>
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Submitted</th>
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Status</th>
            <th style="text-align:left;padding:10px 14px;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted)">Actions</th>
          </tr></thead>
          <tbody id="approvals-tbody">
            <tr><td colspan="6" style="padding:20px;text-align:center;color:var(--muted)">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- NODE ROSTER -->
    <div class="roster-col">
      <div class="sec-title">Node Roster <span id="sync-label" style="font-size:9px;color:var(--dim);font-weight:400;letter-spacing:0;text-transform:none">syncing...</span></div>
      <div id="node-roster">
        <div class="node-table-wrap">
          <div style="color:var(--muted);font-size:12px;padding:24px;text-align:center">Waiting for first heartbeat…</div>
        </div>
      </div>
    </div>

    <!-- GLOBAL COMMANDS -->
    <div class="cmd-col">
      <div class="sec-title">Commands</div>
      <div class="cmd-panel" style="margin-top:0">
        <div class="cmd-panel-hdr">
          <span class="cmd-panel-title">Global Commands</span>
          <span style="font-size:9px;color:var(--dim);font-family:var(--mono)" id="cmd-status"></span>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Trading Mode Gate</div>
          <div class="cmd-row">
            <button class="cmd-btn" id="cmd-paper" onclick="setAdmOverride('trading_gate','PAPER')">Paper</button>
            <button class="cmd-btn" id="cmd-live" onclick="confirmCmd('trading_gate','LIVE','Switch ALL nodes to LIVE trading?')">Live</button>
            <button class="cmd-btn" id="cmd-gate-all" onclick="setAdmOverride('trading_gate','ALL')">All</button>
          </div>
          <div style="font-size:9px;color:var(--dim);margin-top:4px" id="adm-gate-sub">Customers choose</div>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Operating Mode</div>
          <div class="cmd-row">
            <button class="cmd-btn" id="cmd-managed" onclick="setAdmOverride('operating_mode','MANAGED')">Managed</button>
            <button class="cmd-btn" id="cmd-automatic" onclick="confirmCmd('operating_mode','AUTOMATIC','Grant AUTOMATIC mode to ALL nodes?')">Automatic</button>
            <button class="cmd-btn" id="cmd-mode-all" onclick="setAdmOverride('operating_mode','ALL')">All</button>
          </div>
          <div style="font-size:9px;color:var(--dim);margin-top:4px" id="adm-mode-sub">Customers choose</div>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Emergency</div>
          <div class="cmd-row">
            <button class="cmd-btn danger" id="cmd-kill-on" onclick="confirmCmd('kill-switch',true,'ACTIVATE kill switch on ALL nodes?')">Kill Switch ON</button>
            <button class="cmd-btn" id="cmd-kill-off" onclick="sendGlobalCmd('kill-switch',false)">Kill Switch OFF</button>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- TWO COLUMN: GRAPHS + ISSUES -->
  <div class="two-col">

    <!-- SYSTEM HEALTH GRAPHS -->
    <div>
      <div class="sec-title">System Health Over Time</div>
      <div class="graph-card">
        <div class="graph-card-title">CPU Usage %</div>
        <div class="graph-canvas-wrap"><canvas id="cpu-chart"></canvas></div>
      </div>
      <div class="graph-card">
        <div class="graph-card-title">Memory Usage %</div>
        <div class="graph-canvas-wrap"><canvas id="ram-chart"></canvas></div>
      </div>
    </div>

    <!-- RIGHT COLUMN: BEHAVIOR BASELINE + ISSUES + AGENT FLEET -->
    <div>
      <!-- Behavior baseline counter (Phase 7L+, 2026-04-26) — moved from
           customer dashboard 2026-04-26. Shows how long the trader has
           been running on stable decision-logic; useful for deciding
           when the paper-trading observation window is enough to flip
           LIVE. Populated via /api/behavior-baseline which proxies to
           pi5. -->
      <div class="sec-title">Trader Behavior</div>
      <div class="todo-panel" id="bb-panel" style="margin-bottom:14px">
        <div class="todo-header">
          <span class="todo-title">Behavior Baseline</span>
          <span class="todo-count clear" id="bb-days">—</span>
        </div>
        <div style="padding:10px 14px 12px;font-size:11px;color:var(--text);line-height:1.5">
          <div id="bb-line1" style="font-family:var(--mono);color:var(--muted)">Loading…</div>
          <div id="bb-line2" style="margin-top:4px;color:var(--muted);font-size:10px;line-height:1.4"></div>
          <div id="bb-line3" style="margin-top:6px;font-size:9px;color:var(--dim);font-family:var(--mono)"></div>
        </div>
      </div>

      <div class="sec-title">Open Issues</div>
      <div class="todo-panel">
        <div class="todo-header">
          <span class="todo-title">AI Triage</span>
          <span class="todo-count clear" id="todo-badge">Loading</span>
        </div>
        <div class="todo-scroll" id="todo-list">
          <div class="todo-empty">Loading issues...</div>
        </div>
      </div>

      <!-- AGENT FLEET OVERVIEW -->
      <div class="aft-panel">
        <div class="aft-hdr">
          <span class="aft-title">Agent Fleet</span>
          <span class="aft-count" id="aft-badge">0</span>
        </div>
        <div class="aft-scroll" id="aft-body">
          <div style="color:var(--muted);font-size:11px;padding:16px;text-align:center">Waiting for heartbeat data...</div>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
/* DBG */ try { document.getElementById('dbg-fetch').textContent = 'MAIN SCRIPT STARTED'; } catch(e){}
const SECRET_TOKEN = '{{ secret_token }}';
let piData = {};
let _approvalFilter = 'PENDING';
let _approvalsOpen = false;

function toggleApprovals() {
  _approvalsOpen = !_approvalsOpen;
  document.getElementById('approvals-section').style.display = _approvalsOpen ? '' : 'none';
  if (_approvalsOpen) loadApprovals();
}

async function generateInvite(){
  const r=await fetch('/api/proxy/generate-invite',{method:'POST',headers:{'X-Token':SECRET_TOKEN,'Content-Type':'application/json'}});
  const d=await r.json();
  if(d.ok){
    document.getElementById('gen-code').textContent=d.code;
    var gb=document.getElementById('gen-box');if(gb)gb.style.display='block';
    toast('Invite code generated: '+d.code,'ok');
    loadInvites();
  } else toast('Error: '+(d.error||'Unknown'),'err');
}

function copyCode(){
  const code=document.getElementById('gen-code').textContent;
  if(code) navigator.clipboard.writeText(code).then(()=>toast('Copied to clipboard','ok'));
}

async function loadInvites(){
  try{
    const r=await fetch('/api/proxy/invite-codes',{headers:{'X-Token':SECRET_TOKEN}});
    const d=await r.json();
    const el=document.getElementById('invite-list');
    if(!el) return;
    if(!d.codes||!d.codes.length){el.innerHTML='<div style="text-align:center;padding:20px;color:rgba(255,255,255,0.3)">No invite codes yet</div>';return}
    el.innerHTML=d.codes.map(function(c){
      var ts=c.created_at?new Date(c.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'\u2014';
      var used=c.is_used;
      var sc=used?'rgba(255,255,255,0.3)':'#00f5d4';
      var bg=used?'rgba(255,255,255,0.02)':'rgba(0,245,212,0.04)';
      var stClr=used?'rgba(255,75,110,0.7)':'rgba(0,245,212,0.7)';
      var stTxt=used?'Used'+(c.used_by?' by '+c.used_by:''):'Available';
      return '<div style="display:flex;align-items:center;gap:14px;padding:10px 16px;border:1px solid rgba(255,255,255,0.05);border-radius:10px;margin-bottom:6px;background:'+bg+'">'
        +'<span style="font-family:monospace;font-size:13px;font-weight:700;color:'+sc+';letter-spacing:0.04em;min-width:120px">'+c.code+'</span>'
        +'<span style="font-size:10px;color:rgba(255,255,255,0.3)">'+ts+'</span>'
        +'<span style="font-size:10px;color:'+stClr+'">'+stTxt+'</span>'
        +'</div>';
    }).join('');
  }catch(e){
    console.error('loadApprovals error:',e);
    var list=document.getElementById('appr-list');
    if(list) list.innerHTML='<div class="appr-empty">Failed to load signups</div>';
  }
}

async function loadApprovals() {
  try {
    let url = '/api/proxy/pending-signups';
    if (_approvalFilter) url += '?status=' + _approvalFilter;
    const r = await fetch(url, {headers:{'X-Token':SECRET_TOKEN}});
    const d = await r.json();
    const tbody = document.getElementById('approvals-tbody');
    if (!d.signups || !d.signups.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;color:var(--muted)">No signups found</td></tr>';
      return;
    }
    tbody.innerHTML = d.signups.map(s => {
      const ts = s.created_at ? new Date(s.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '\u2014';
      const sc = s.status==='PENDING'?'var(--amber)':s.status==='APPROVED'?'var(--teal)':'var(--pink)';
      const sbg = s.status==='PENDING'?'rgba(245,166,35,0.08)':s.status==='APPROVED'?'rgba(0,245,212,0.08)':'rgba(255,75,110,0.08)';
      let actions = '';
      if (s.status==='PENDING') {
        actions = '<button onclick="approveSignup('+s.id+')" style="padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:var(--teal);cursor:pointer;margin-right:4px">Approve</button>'
                + '<button onclick="rejectSignup('+s.id+')" style="padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);color:var(--pink);cursor:pointer">Reject</button>';
      } else if (s.status==='APPROVED' && s.customer_id) {
        actions = '<span style="font-size:10px;color:var(--muted);font-family:monospace">'+s.customer_id.slice(0,8)+'...</span>';
      } else { actions = '<span style="font-size:10px;color:var(--dim)">\u2014</span>'; }
      return '<tr style="border-bottom:1px solid var(--border)">'
        +'<td style="padding:10px 14px;font-weight:600">'+(s.name||'\u2014')+'</td>'
        +'<td style="padding:10px 14px;font-family:monospace;font-size:11px">'+(s.email||'\u2014')+'</td>'
        +'<td style="padding:10px 14px;font-size:11px">'+(s.phone||'\u2014')+'</td>'
        +'<td style="padding:10px 14px;font-size:11px;color:var(--muted)">'+ts+'</td>'
        +'<td style="padding:10px 14px"><span style="background:'+sbg+';color:'+sc+';padding:2px 8px;border-radius:99px;font-size:10px;font-weight:700">'+s.status+'</span></td>'
        +'<td style="padding:10px 14px">'+actions+'</td></tr>';
    }).join('');
  } catch(e) { console.error('loadApprovals', e); }
}

function filterAppr(status) {
  _approvalFilter = status;
  document.querySelectorAll('.appr-filter').forEach(b=>b.classList.remove('active'));
  document.getElementById(status?'af-'+status:'af-ALL')?.classList.add('active');
  loadApprovals();
}

async function approveSignup(id) {
  if (!confirm('Approve this signup? Creates their account and database.')) return;
  const r = await fetch('/api/proxy/approve-signup',{method:'POST',headers:{'Content-Type':'application/json','X-Token':SECRET_TOKEN},body:JSON.stringify({signup_id:id})});
  const d = await r.json();
  if (d.ok) { toast('Account approved: '+(d.email||''),'ok'); loadApprovals(); checkPendingBadge();
// Auto-open approvals if ?approvals=1 in URL
if (new URLSearchParams(window.location.search).get("approvals") === "1") {
  toggleApprovals();
} }
  else toast('Error: '+(d.error||'Unknown'),'err');
}

async function rejectSignup(id) {
  if (!confirm('Reject this signup request?')) return;
  const r = await fetch('/api/proxy/reject-signup',{method:'POST',headers:{'Content-Type':'application/json','X-Token':SECRET_TOKEN},body:JSON.stringify({signup_id:id})});
  const d = await r.json();
  if (d.ok) { toast('Signup rejected','ok'); loadApprovals(); checkPendingBadge(); }
  else toast('Error: '+(d.error||'Unknown'),'err');
}

async function checkPendingBadge() {
  try {
    const r = await fetch('/api/proxy/pending-signups?status=PENDING',{headers:{'X-Token':SECRET_TOKEN}});
    const d = await r.json();
    const badge = document.getElementById('appr-badge');
    if (badge && d.signups && d.signups.length>0) { badge.textContent=d.signups.length; badge.style.display='inline'; }
    else if (badge) { badge.style.display='none'; }
  } catch(e) {}
}
checkPendingBadge();
let allTodos = [];
let pendingDelete = null;
let modalPiId = null;
let modalChartInst = null;
let currentModalTab = 'overview';
const AVATAR_COLORS = ['av-teal','av-purple','av-amber','av-pink'];
const SEV_ORDER = {CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3};

// ── CLOCK ──
function updateClock() {
  const t = new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false});
  document.getElementById('clock').textContent = t + ' ET';
}
updateClock();
setInterval(updateClock, 1000);

// ── TOAST ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── STATUS HELPERS ──
function statusClass(s) { return (s === 'online' || s === 'active') ? 'online' : s === 'offline' ? 'offline' : (s === 'fault' || s === 'warning') ? 'warning' : 'warning'; }
function dotClass(s) { return (s === 'online' || s === 'active') ? 'online' : s === 'offline' ? 'offline' : (s === 'fault' || s === 'warning') ? 'warning' : 'unknown'; }
function ageSince(isoStr) {
  const secs = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs/60) + 'm ago';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ago';
  return Math.floor(secs/86400) + 'd ago';
}
function initials(label) {
  return label.split(' ').filter(Boolean).map(w=>w[0]||'').join('').toUpperCase().slice(0,2) || '??';
}
function avatarColor(piId) {
  let h = 0;
  for (let i=0; i<piId.length; i++) h = (h*31 + piId.charCodeAt(i)) & 0xFFFFFF;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

// ── GLASS CLOUD WEATHER ICONS ──
function weatherIcon(status) {
  const cp = 'M3,21 Q3,14 10,14 Q9,7 18,7 Q24,7 26,11 Q33,10 33,16 Q37,16 37,21 Q37,25 33,25 L7,25 Q3,25 3,21 Z';
  const hl = 'M7,14 Q7,9 13,9 Q12,4 19,4';
  if (status === 'online' || status === 'active') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 7px rgba(0,245,212,0.4))">
      <circle cx="27" cy="10" r="8" fill="rgba(0,245,212,0.07)"/>
      <circle cx="27" cy="10" r="5.5" fill="rgba(0,245,212,0.15)" stroke="rgba(0,245,212,0.72)" stroke-width="1.3"/>
      <g stroke="rgba(0,245,212,0.62)" stroke-width="1.2" stroke-linecap="round">
        <line x1="27" y1="2.5" x2="27" y2="0.5"/>
        <line x1="32.5" y1="4.5" x2="34" y2="3"/>
        <line x1="36" y1="10" x2="38" y2="10"/>
        <line x1="21.5" y1="4.5" x2="20" y2="3"/>
        <line x1="18" y1="10" x2="16" y2="10"/>
      </g>
      <path d="${cp}" fill="rgba(255,255,255,0.08)" stroke="rgba(255,255,255,0.75)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.28)" stroke-width="1" stroke-linecap="round"/>
    </svg>`;
  }
  if (status === 'fault' || status === 'warning') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 7px rgba(255,179,71,0.4))">
      <path d="${cp}" fill="rgba(255,179,71,0.05)" stroke="rgba(255,255,255,0.68)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.22)" stroke-width="1" stroke-linecap="round"/>
      <path d="M21,27 L18,34 L21,33.5 L19.5,39 L25.5,31 L22,31.5 Z" fill="rgba(255,179,71,0.95)" stroke="rgba(255,179,71,0.4)" stroke-width="0.5" stroke-linejoin="round"/>
    </svg>`;
  }
  if (status === 'offline') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 5px rgba(255,75,110,0.25))">
      <path d="${cp}" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.52)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.18)" stroke-width="1" stroke-linecap="round"/>
      <g stroke="rgba(140,200,255,0.65)" stroke-width="1.6" stroke-linecap="round">
        <line x1="13" y1="28" x2="11.5" y2="37"/>
        <line x1="20" y1="28" x2="18.5" y2="37"/>
        <line x1="27" y1="28" x2="25.5" y2="37"/>
      </g>
    </svg>`;
  }
  return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 4px rgba(255,255,255,0.1))">
    <path d="${cp}" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.38)" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.14)" stroke-width="1" stroke-linecap="round"/>
  </svg>`;
}

// ── FETCH STATUS ──
async function fetchStatus() {
  const dbgFetch = document.getElementById('dbg-fetch');
  const dbgKeys  = document.getElementById('dbg-keys');
  try {
    if (dbgFetch) dbgFetch.textContent = 'FETCHING...';
    const r = await fetch('/api/status');
    if (!r.ok) { if (dbgFetch) dbgFetch.textContent = 'HTTP ' + r.status; return; }
    piData = await r.json();
    if (dbgFetch) { dbgFetch.textContent = 'OK (' + Object.keys(piData).length + ' nodes)'; dbgFetch.style.color = '#00f5d4'; }
    if (dbgKeys) dbgKeys.textContent = Object.keys(piData).join(', ') || 'empty';
    renderNodeRoster();
    updateFleetStats();
    buildFleetCharts();
    renderAgentFleet();
    document.getElementById('sync-label').textContent = 'synced ' + new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'});
  } catch(e) {
    console.error('[fetchStatus]', e);
    if (dbgFetch) { dbgFetch.textContent = 'ERROR: ' + e.message; dbgFetch.style.color = '#ff4b6e'; }
  }
}

// ── METRIC COLOR HELPERS ──
function metricClass(v, warn, crit) {
  if (v == null) return 'mc-na';
  if (v >= crit) return 'mc-crit';
  if (v >= warn) return 'mc-warn';
  return 'mc-ok';
}
function fmtMetric(v, unit, dec=0) {
  return v != null ? v.toFixed(dec) + unit : '—';
}
function colorWithAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1,3),16);
  const g = parseInt(hex.slice(3,5),16);
  const b = parseInt(hex.slice(5,7),16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// ── FLEET STATS ──
function updateFleetStats() {
  const pis   = Object.values(piData);
  const total  = pis.length;
  const online = pis.filter(p => p.status === 'online' || p.status === 'active').length;
  const notOk  = pis.filter(p => p.status !== 'online' && p.status !== 'active').length;

  // Agent counts across all nodes (including expected but unreported)
  let agActive = 0, agIdle = 0, agFault = 0, agInactive = 0, agTotal = 0;
  pis.forEach(p => {
    const reported = p.agents || {};
    const ageSecs = p.age_secs || 0;
    const role = detectNodeRole(p);
    const merged = {};
    if (role && EXPECTED_AGENTS[role]) {
      EXPECTED_AGENTS[role].forEach(k => { merged[k] = null; });
    }
    Object.entries(reported).forEach(([k, v]) => { merged[k] = v; });
    Object.values(merged).forEach(s => {
      agTotal++;
      const cls = agentStatusClass(s, ageSecs);
      if (cls === 'fault') agFault++;
      else if (cls === 'idle') agIdle++;
      else if (cls === 'inactive') agInactive++;
      else agActive++;
    });
  });

  // Trading mode counts (from customer data, not Pi nodes)
  var _tm = (_mktData && _mktData.trading_modes) || {};
  const paperCount = _tm.PAPER || 0;
  const liveCount  = _tm.LIVE || 0;
  const custTotal  = _tm.total || 0;

  const sv = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };

  sv('fl-online',  online + (total ? ' / ' + total : ''));
  sv('fl-total',   total === 0 ? 'No nodes registered' : notOk === 0 ? 'All reporting' : notOk + ' not reporting');
  sv('fl-issues',  allTodos.filter(t=>!t.resolved).length);
  sv('fl-agents',  agTotal > 0 ? agActive + ' / ' + agTotal : '—');
  sv('fl-agents-sub', agTotal > 0 ? agActive + ' active' + (agIdle ? ', ' + agIdle + ' idle' : '') + (agFault ? ', ' + agFault + ' fault' : '') + (agInactive ? ', ' + agInactive + ' off' : '') : 'Awaiting data');
  sv('fl-trading',    custTotal > 0 ? paperCount + 'P / ' + liveCount + 'L' : '—');
  sv('fl-trading-sub', custTotal > 0 ? custTotal + ' customers — ' + paperCount + ' paper, ' + liveCount + ' live' : 'Awaiting data');

  // Header pill
  if (total === 0)       sv('pi-count', 'No Nodes');
  else if (notOk === 0)  sv('pi-count', 'All Nodes Online');
  else                   sv('pi-count', notOk + ' not reporting');

  // Update command button highlights
  updateCommandState(pis);
}

// ── NODE ROSTER TABLE ──
function renderNodeRoster() {
  const wrap = document.getElementById('node-roster');
  const pis  = Object.values(piData);

  if (!pis.length) {
    wrap.innerHTML = '<div class="node-table-wrap"><div style="color:var(--muted);font-size:12px;padding:24px;text-align:center">No nodes registered yet. Waiting for first heartbeat\u2026</div></div>';
    return;
  }

  pis.sort((a,b) => {
    const ord = {online:0,warning:1,fault:1,offline:2};
    return (ord[a.status]??3) - (ord[b.status]??3);
  });

  const rows = pis.map(pi => {
    const sc   = statusClass(pi.status);
    const dc   = dotClass(pi.status);
    const load1 = pi.load_avg && pi.load_avg[0] != null ? pi.load_avg[0] : null;
    return '<div class="node-row" data-piid="' + pi.pi_id + '" onclick="openModal(this.dataset.piid)">'
      + '<div class="node-name-cell">'
          + '<div class="node-micro-av">' + weatherIcon(pi.status) + '</div>'
          + '<div><div class="node-lbl">' + escHtml(pi.label || pi.pi_id) + '</div>'
              + '<div class="node-id-tag">' + pi.pi_id + '</div></div>'
      + '</div>'
      + '<div><div class="status-dot-wrap">'
          + '<div class="sdot ' + dc + '"></div>'
          + '<span class="status-text st-' + sc + '">' + pi.status + '</span>'
      + '</div></div>'
      + '<div class="node-cell ' + (sc==='offline'?'mc-na':metricClass(pi.cpu_percent, 70, 90)) + '">'
          + (sc==='offline'?'\u2014':fmtMetric(pi.cpu_percent, '%')) + '</div>'
      + '<div class="node-cell ' + (sc==='offline'?'mc-na':metricClass(pi.ram_percent, 75, 90)) + '">'
          + (sc==='offline'?'\u2014':fmtMetric(pi.ram_percent, '%')) + '</div>'
      + '<div class="node-cell ' + (sc==='offline'?'mc-na':metricClass(load1, 1.5, 3.0)) + '">'
          + (sc==='offline'?'\u2014':fmtMetric(load1, '', 2)) + '</div>'
      + '<div class="node-cell ' + (sc==='offline'?'mc-na':metricClass(pi.cpu_temp, 65, 80)) + '">'
          + (sc==='offline'?'\u2014':fmtMetric(pi.cpu_temp, '\u00b0', 1)) + '</div>'
      + '<div class="node-cell ' + (sc==='offline'?'mc-na':metricClass(pi.disk_percent, 75, 90)) + '">'
          + (sc==='offline'?'\u2014':fmtMetric(pi.disk_percent, '%')) + '</div>'
      + '<div class="node-cell mc-na">' + (pi.uptime || '\u2014') + '</div>'
      + '<div class="node-cell mc-na">' + ageSince(pi.last_seen) + '</div>'
    + '<div class="node-power">'
      + '<button class="mute-btn' + (pi.silenced ? ' muted' : '') + '" '
      + 'onclick="event.stopPropagation();toggleSilence(\\'' + escHtml(pi.pi_id) + '\\')" '
      + 'title="' + (pi.silenced ? 'Unmute alerts' : 'Mute alerts') + '">'
      + (pi.silenced ? '\U0001F507' : '\U0001F514')
      + '</button>'
      + '<button class="pwr-btn" title="Reboot" data-piid="' + pi.pi_id + '" data-act="reboot" onclick="nodePower(this.dataset.piid,this.dataset.act,event)">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>'
      + '</button>'
      + '<button class="pwr-btn danger" title="Shutdown" data-piid="' + pi.pi_id + '" data-act="shutdown" onclick="nodePower(this.dataset.piid,this.dataset.act,event)">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>'
      + '</button>'
    + '</div>'
    + '</div>';
  }).join('');

  wrap.innerHTML =
    '<div class="node-table-wrap">'
    + '<div class="node-thead">'
        + '<div class="node-th">Node</div>'
        + '<div class="node-th">Status</div>'
        + '<div class="node-th">CPU</div>'
        + '<div class="node-th">RAM</div>'
        + '<div class="node-th">Load</div>'
        + '<div class="node-th">Temp</div>'
        + '<div class="node-th">Disk</div>'
        + '<div class="node-th">Uptime</div>'
        + '<div class="node-th">Last Seen</div>'
        + '<div class="node-th">Actions</div>'
    + '</div>'
    + rows
    + '</div>';
}


// ── MODAL ──
async function openModal(piId) {
  modalPiId = piId;
  currentModalTab = 'overview';
  document.getElementById('modal-overlay').classList.add('show');
  document.body.style.overflow = 'hidden';

  // Set header from cached data immediately
  const pi = piData[piId] || {};
  document.getElementById('modal-avatar').className = 'modal-avatar';
  document.getElementById('modal-avatar').innerHTML = weatherIcon(pi.status || 'unknown');
  document.getElementById('modal-name').textContent = pi.label || piId;
  document.getElementById('modal-email').textContent = pi.email || 'No email';
  document.getElementById('modal-id').textContent = piId;

  const sc = statusClass(pi.status||'unknown');
  document.getElementById('modal-status-row').innerHTML =
    '<div class="sdot ' + dotClass(pi.status||'unknown') + '"></div>'
    + '<span style="font-size:10px;color:var(--' + (sc==='online'?'teal':sc==='offline'?'pink':'amber') + ')">'
    + (pi.status||'unknown').toUpperCase() + '</span>'
    + '<span style="font-size:10px;color:var(--muted);margin-left:4px">· last seen ' + ageSince(pi.last_seen||new Date().toISOString()) + '</span>';

  // Reset tabs
  document.querySelectorAll('.mtab').forEach((b,i) => b.classList.toggle('active', i===0));

  // Fetch full detail
  try {
    const r = await fetch('/api/pi/' + encodeURIComponent(piId));
    const detail = r.ok ? await r.json() : pi;
    renderModalTab('overview', detail);
  } catch(e) {
    renderModalTab('overview', pi);
  }
}

function closeModal(e) {
  if (e.target.id === 'modal-overlay') closeModalBtn();
}

function closeModalBtn() {
  document.getElementById('modal-overlay').classList.remove('show');
  document.body.style.overflow = '';
  if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
  modalPiId = null;
}

async function switchTab(tab, btn) {
  currentModalTab = tab;
  document.querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('modal-body').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
  try {
    const r = await fetch('/api/pi/' + encodeURIComponent(modalPiId));
    const detail = r.ok ? await r.json() : (piData[modalPiId] || {});
    renderModalTab(tab, detail);
  } catch(e) {}
}

function renderModalTab(tab, pi) {
  const body = document.getElementById('modal-body');

  const mc  = (v,w,c) => v==null?'mc-na':v>=c?'mc-crit':v>=w?'mc-warn':'mc-ok';
  const fmt = (v,u,d=0) => v!=null ? v.toFixed(d)+u : '\u2014';
  const gb  = (v) => v!=null ? v.toFixed(2)+' GB' : '\u2014';

  if (tab === 'overview') {
    // ── Processor panel data ──
    const cpuCls  = mc(pi.cpu_percent, 70, 90);
    const load    = pi.load_avg || [];
    const cores   = pi.cpu_count || '\u2014';
    // ── Memory panel data ──
    const ramTot  = pi.ram_total_gb  || 0;
    const ramUsed = pi.ram_used_gb   || 0;
    const ramCach = pi.ram_cached_gb || 0;
    const ramFree = pi.ram_avail_gb  || 0;
    const ramUsedPct  = ramTot ? Math.round(ramUsed / ramTot * 100) : 0;
    const ramCachPct  = ramTot ? Math.round(ramCach / ramTot * 100) : 0;
    const ramFreePct  = Math.max(0, 100 - ramUsedPct - ramCachPct);
    // ── Disk panel data ──
    const dskCls  = mc(pi.disk_percent, 75, 90);
    const dskUsed = pi.disk_used_gb  || 0;
    const dskTot  = pi.disk_total_gb || 0;
    const dskFree = pi.disk_free_gb  || 0;
    const dskPct  = pi.disk_percent  || 0;
    // ── Temp panel data ──
    const tmpCls  = mc(pi.cpu_temp, 65, 80);

    body.innerHTML =
      // ── 2x2 Panel Grid ──────────────────────────────────────────────────
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">'

        // PROCESSOR panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Processor</div>'
            + '<div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:8px">'
                + '<div class="' + cpuCls + '" style="font-size:32px;font-weight:700;line-height:1;font-family:var(--mono)">' + fmt(pi.cpu_percent,'%') + '</div>'
                + '<div style="padding-bottom:4px">'
                    + '<div style="font-size:10px;color:var(--muted)">' + cores + ' cores</div>'
                    + '<div style="font-size:10px;color:var(--dim)">load ' + (load[0]!=null?load[0].toFixed(2):'\u2014') + '</div>'
                + '</div>'
            + '</div>'
            + '<div style="height:40px;position:relative"><canvas id="mc-cpu-spark"></canvas></div>'
            + '<div style="display:flex;gap:16px;margin-top:6px">'
                + '<div><div style="font-size:8px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em">5m avg</div>'
                    + '<div style="font-size:11px;font-family:var(--mono);color:var(--muted)">' + (load[1]!=null?load[1].toFixed(2):'\u2014') + '</div></div>'
                + '<div><div style="font-size:8px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em">15m avg</div>'
                    + '<div style="font-size:11px;font-family:var(--mono);color:var(--muted)">' + (load[2]!=null?load[2].toFixed(2):'\u2014') + '</div></div>'
            + '</div>'
        + '</div>'

        // MEMORY panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Memory</div>'
            + '<div style="display:flex;align-items:center;gap:12px">'
                // Donut chart
                + '<div style="position:relative;width:72px;height:72px;flex-shrink:0">'
                    + '<canvas id="mc-ram-donut" width="72" height="72"></canvas>'
                    + '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none">'
                        + '<div style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--teal)">' + ramUsedPct + '%</div>'
                    + '</div>'
                + '</div>'
                // Breakdown table
                + '<div style="flex:1;display:flex;flex-direction:column;gap:4px">'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:var(--teal);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Used</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--teal)">' + gb(pi.ram_used_gb) + '</div>'
                    + '</div>'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:var(--purple);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Cached</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--purple)">' + gb(pi.ram_cached_gb) + '</div>'
                    + '</div>'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:rgba(255,255,255,0.1);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Free</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--dim)">' + gb(pi.ram_avail_gb) + '</div>'
                    + '</div>'
                    + '<div style="margin-top:2px;padding-top:4px;border-top:1px solid var(--border);display:flex;justify-content:space-between">'
                        + '<div style="font-size:9px;color:var(--dim)">Total</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--muted)">' + gb(pi.ram_total_gb) + '</div>'
                    + '</div>'
                + '</div>'
            + '</div>'
        + '</div>'

        // STORAGE panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Storage</div>'
            + '<div style="display:flex;justify-content:space-between;margin-bottom:8px">'
                + '<div class="' + dskCls + '" style="font-size:28px;font-weight:700;font-family:var(--mono);line-height:1">' + fmt(pi.disk_percent,'%') + '</div>'
                + '<div style="text-align:right">'
                    + '<div style="font-size:10px;color:var(--muted)">Used: ' + gb(pi.disk_used_gb) + '</div>'
                    + '<div style="font-size:10px;color:var(--dim)">Free: ' + gb(pi.disk_free_gb) + '</div>'
                    + '<div style="font-size:10px;color:var(--dim)">Total: ' + gb(pi.disk_total_gb) + '</div>'
                + '</div>'
            + '</div>'
            // Fill bar
            + '<div style="height:6px;border-radius:99px;background:rgba(255,255,255,0.07);overflow:hidden;margin-bottom:4px">'
                + '<div style="height:100%;width:' + dskPct + '%;border-radius:99px;background:' + (dskPct>=90?'var(--pink)':dskPct>=75?'var(--amber)':'var(--teal)') + ';transition:width 0.4s"></div>'
            + '</div>'
            + '<div style="font-size:9px;color:var(--dim)">/ (root filesystem)</div>'
        + '</div>'

        // THERMAL & UPTIME panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Thermal &amp; Uptime</div>'
            + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'
                + '<div style="width:36px;height:36px;border-radius:9px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center">'
                    + '<svg viewBox="0 0 20 20" width="18" height="18" fill="none" xmlns="http://www.w3.org/2000/svg">'
                        + '<rect x="8.5" y="2" width="3" height="11" rx="1.5" fill="rgba(255,255,255,0.15)"/>'
                        + '<rect x="9" y="2.5" width="2" height="' + (pi.cpu_temp!=null?Math.min(10,pi.cpu_temp/10).toFixed(1):'5') + '" rx="1" fill="' + (tmpCls==='mc-crit'?'#ff4b6e':tmpCls==='mc-warn'?'#ffb347':'#00f5d4') + '"/>'
                        + '<circle cx="10" cy="14.5" r="2.5" fill="' + (tmpCls==='mc-crit'?'#ff4b6e':tmpCls==='mc-warn'?'#ffb347':'#00f5d4') + '"/>'
                    + '</svg>'
                + '</div>'
                + '<div>'
                    + '<div class="' + tmpCls + '" style="font-size:22px;font-weight:700;font-family:var(--mono);line-height:1">' + fmt(pi.cpu_temp,'\u00b0C',1) + '</div>'
                    + '<div style="font-size:9px;color:var(--dim);margin-top:2px">CPU Temperature</div>'
                + '</div>'
            + '</div>'
            + '<div style="border-top:1px solid var(--border);padding-top:10px">'
                + '<div style="font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:4px">Uptime</div>'
                + '<div style="font-size:16px;font-weight:600;color:var(--text);font-family:var(--mono)">' + (pi.uptime||'N/A') + '</div>'
                + '<div style="font-size:9px;color:var(--dim);margin-top:6px">Mode: ' + (pi.operating_mode||'MANAGED') + ' &nbsp;&middot;&nbsp; ' + (pi.trading_mode||'PAPER') + '</div>'
            + '</div>'
        + '</div>'

      + '</div>'

      // ── Agents / Process List ──────────────────────────────────────────────
      + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">Agents</div>'
      + renderAgents(pi.agents||{});

    // Draw micro charts after DOM ready
    setTimeout(() => {
      // CPU sparkline
      const cpuCtx = document.getElementById('mc-cpu-spark');
      if (cpuCtx) {
        const hist = (pi.history||[]).filter(h=>h.cpu!=null);
        const vals = hist.map(h=>h.cpu);
        if (vals.length > 1) {
          const g = cpuCtx.getContext('2d').createLinearGradient(0,0,0,40);
          g.addColorStop(0,'rgba(0,245,212,0.25)'); g.addColorStop(1,'rgba(0,245,212,0)');
          new Chart(cpuCtx, { type:'line', data:{ labels:vals.map((_,i)=>i),
            datasets:[{data:vals,borderColor:'#00f5d4',borderWidth:1.5,fill:true,
              backgroundColor:g,tension:0.4,pointRadius:0}]},
            options:{animation:false,responsive:true,maintainAspectRatio:false,
              plugins:{legend:{display:false},tooltip:{enabled:false}},
              scales:{x:{display:false},y:{display:false,min:0,max:100}}}});
        }
      }
      // RAM donut
      const ramCtx = document.getElementById('mc-ram-donut');
      if (ramCtx) {
        new Chart(ramCtx, { type:'doughnut',
          data:{ datasets:[{
            data:[ramUsedPct, ramCachPct, ramFreePct],
            backgroundColor:['rgba(0,245,212,0.85)','rgba(123,97,255,0.75)','rgba(255,255,255,0.07)'],
            borderWidth:0, hoverOffset:0,
          }]},
          options:{cutout:'68%',animation:false,
            plugins:{legend:{display:false},tooltip:{enabled:false}}}});
      }
    }, 30);

  } else if (tab === 'performance') {
    body.innerHTML =
      '<div class="modal-graph-wrap">'
        + '<div class="modal-graph-title">CPU Usage %</div>'
        + '<div class="modal-graph-canvas"><canvas id="modal-chart-cpu"></canvas></div>'
      + '</div>'
      + '<div class="modal-graph-wrap" style="margin-top:12px">'
        + '<div class="modal-graph-title">Memory Usage %</div>'
        + '<div class="modal-graph-canvas"><canvas id="modal-chart-ram"></canvas></div>'
      + '</div>';

    setTimeout(() => {
      const hist = (pi.history || []).filter(h => h.cpu != null || h.ram != null);
      if (!hist.length) {
        body.innerHTML += '<div style="color:var(--muted);font-size:11px;text-align:center;padding:12px">No metric history yet \u2014 awaiting next heartbeat</div>';
        return;
      }
      const labels = hist.map(h => new Date(h.t).toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'}));
      const mkChart = (canvasId, data, color, unit) => {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return null;
        const grad = ctx.getContext('2d').createLinearGradient(0,0,0,100);
        grad.addColorStop(0, colorWithAlpha(color, 0.18));
        grad.addColorStop(1, colorWithAlpha(color, 0.0));
        return new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{
            data, borderColor: color, borderWidth: 2,
            fill: true, backgroundColor: grad, tension: 0.4,
            pointRadius: 0, pointHitRadius: 8, spanGaps: true,
          }]},
          options: {
            responsive:true, maintainAspectRatio:false,
            plugins:{legend:{display:false},tooltip:{
              backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
              titleColor:'rgba(255,255,255,0.5)',bodyColor:color,bodyFont:{weight:'bold'},
              callbacks:{label:c=>c.parsed.y.toFixed(1)+unit}
            }},
            scales:{
              x:{stacked:true,grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:6}},
              y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:v=>v+unit},min:0,max:100,position:'right'}
            }
          }
        });
      };
      if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
      modalChartInst = mkChart('modal-chart-cpu', hist.map(h=>h.cpu), '#00f5d4', '%');
      mkChart('modal-chart-ram', hist.map(h=>h.ram), '#7b61ff', '%');
    }, 50);

  } else if (tab === 'logs') {
    const errors = pi.last_errors || [];
    body.innerHTML =
      '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Recent Errors</div>'
      + (errors.length
          ? '<div class="error-log">' + errors.map(e => escHtml(e)).join('\\n') + '</div>'
          : '<div class="error-log empty">\u2713 No recent errors</div>')
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin:14px 0 8px">Agent Status</div>'
      + renderAgents(pi.agents||{})
      + '<div style="font-size:10px;color:var(--muted);margin-top:12px">Full logs: ' + (pi.pi_id||'') + ':5001/logs</div>';

  } else if (tab === 'admin') {
    body.innerHTML =
      '<div class="modal-stats" style="grid-template-columns:1fr 1fr;margin-bottom:16px">'
        + '<div class="mstat"><div class="mstat-label">Node</div><div class="mstat-val" style="font-size:14px;word-break:break-all">' + (pi.label||'\u2014') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">Contact</div><div class="mstat-val" style="font-size:12px;word-break:break-all"><a href="mailto:' + (pi.email||'') + '" style="color:var(--teal)">' + (pi.email||'\u2014') + '</a></div></div>'
        + '<div class="mstat"><div class="mstat-label">Pi ID</div><div class="mstat-val" style="font-size:11px;font-family:var(--mono)">' + (pi.pi_id||'\u2014') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">First Seen</div><div class="mstat-val" style="font-size:12px">' + (pi.first_seen||'\u2014').slice(0,10) + '</div></div>'
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Update Keys on Pi</div>'
      + '<div style="font-size:11px;color:var(--amber);background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.15);border-radius:8px;padding:8px 10px;margin-bottom:12px">'
        + '&#9888; Keys are sent directly to the Pi portal at ' + (pi.pi_id||'?') + ':5001 and written to .env'
      + '</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Anthropic API Key</div>'
          + '<input id="adm-anthropic" type="password" placeholder="sk-ant-..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alpaca API Key</div>'
          + '<input id="adm-alpaca-key" type="password" placeholder="PK..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alpaca Secret</div>'
          + '<input id="adm-alpaca-secret" type="password" placeholder="Secret..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alert Email</div>'
          + '<input id="adm-alert-to" type="email" placeholder="node@email.com" style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
      + '</div>'
      + '<div style="display:flex;gap:8px;margin-bottom:16px">'
        + '<button data-piid="' + (pi.pi_id||'') + '" onclick="pushKeysToPi(this.dataset.piid)" style="padding:9px 18px;border-radius:10px;background:var(--teal2);border:1px solid rgba(0,245,212,0.3);color:var(--teal);font-size:11px;font-weight:700;cursor:pointer;font-family:var(--sans)">Push Keys to Pi</button>'
        + '<div id="adm-key-result-' + (pi.pi_id||'') + '" style="font-size:11px;color:var(--muted);align-self:center"></div>'
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Danger Zone</div>'
      + '<div style="display:flex;flex-direction:column;gap:8px">'
        + '<button data-piid="' + pi.pi_id + '" onclick="promptDelete(this.dataset.piid)" style="padding:10px 16px;border-radius:10px;background:var(--pink2);border:1px solid rgba(255,75,110,0.25);color:var(--pink);font-size:12px;font-weight:600;cursor:pointer;text-align:left;font-family:var(--sans)">Remove Node from Registry</button>'
      + '</div>';
  }
}


function renderAgents(agents) {
  // Known agent descriptive names — add entries as agents report in
  const knownNames = {
    'retail_trade_logic_agent':     'Trade Logic',
    'retail_news_agent':            'News',
    'retail_market_sentiment_agent':'Market Sentiment',
    'retail_sector_screener':       'Screener',
    'retail_scheduler':             'Scheduler',
    'retail_heartbeat':             'Heartbeat',
    'retail_watchdog':              'Watchdog',
    'retail_health_check':          'Health Check',
    // Legacy names (pre-rename)
    'trade_logic_agent':            'Trade Logic',
    'news_agent':                   'News',
    'market_sentiment_agent':       'Market Sentiment',
  };
  // Render whatever agents are reported (fall back to raw key if name unknown)
  const keys = Object.keys(agents);
  if (!keys.length) return '<div style="color:var(--muted);font-size:11px;padding:12px 0;text-align:center">No agent data received yet</div>';
  return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;overflow:hidden">'
    + keys.map((k,i) => {
      const status = agents[k];
      const label  = knownNames[k] || k;
      const isOk   = status && status !== 'fault' && status !== 'error';
      const isFault= status === 'fault' || status === 'error';
      const dotClr = isFault ? 'var(--pink)' : isOk ? 'var(--teal)' : 'var(--muted)';
      const dotGlw = isFault ? '0 0 5px var(--pink)' : isOk ? '0 0 5px var(--teal)' : 'none';
      const stClr  = isFault ? 'var(--pink)' : isOk ? 'rgba(255,255,255,0.45)' : 'var(--dim)';
      return '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;'
          + (i > 0 ? 'border-top:1px solid var(--border);' : '')
          + '">'
        + '<div style="width:7px;height:7px;border-radius:50%;flex-shrink:0;background:' + dotClr + ';box-shadow:' + dotGlw + '"></div>'
        + '<span style="font-size:11px;font-weight:600;font-family:var(--mono);color:var(--text);flex:1">' + label + '</span>'
        + '<span style="font-size:10px;font-family:var(--mono);color:' + stClr + '">' + (status||'—') + '</span>'
      + '</div>';
    }).join('')
    + '</div>';
}

function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── AGENT FLEET OVERVIEW ──
const AGENT_NAMES = {
  'retail_trade_logic_agent':'Trade Logic','retail_news_agent':'News',
  'retail_market_sentiment_agent':'Market Sentiment','retail_sector_screener':'Screener',
  'retail_scheduler':'Scheduler','retail_heartbeat':'Heartbeat',
  'retail_watchdog':'Watchdog','retail_health_check':'Health Check',
  'retail_boot_sequence':'Boot Sequence','retail_shutdown':'Shutdown',
  'retail_interrogation_listener':'Listener','retail_patch':'Patcher',
  'retail_backup':'Backup',
  'trade_logic_agent':'Trade Logic','news_agent':'News','market_sentiment_agent':'Market Sentiment',
  'synthos_monitor':'Monitor','scoop':'Scoop','strongbox':'Strongbox',
  'company_server':'Server','company_vault':'Vault','company_archivist':'Librarian',
  'company_sentinel':'Sentinel','company_keepalive':'Keepalive','company_auditor':'Auditor'
};

// Expected agents per node role — used to show inactive agents that haven't reported
const EXPECTED_AGENTS = {
  retail: [
    'retail_trade_logic_agent','retail_news_agent','retail_market_sentiment_agent',
    'retail_sector_screener','retail_scheduler','retail_heartbeat',
    'retail_watchdog','retail_health_check','retail_boot_sequence',
    'retail_shutdown','retail_interrogation_listener','retail_patch','retail_backup'
  ],
  company: [
    'company_server','scoop','strongbox','company_vault',
    'company_archivist','company_sentinel','company_keepalive','company_auditor'
  ],
  monitor: ['synthos_monitor']
};

function detectNodeRole(pi) {
  const agents = Object.keys(pi.agents || {});
  const id = (pi.pi_id || '').toLowerCase();
  const label = (pi.label || '').toLowerCase();
  if (agents.some(a => a.startsWith('retail_')) || id.includes('retail') || label.includes('retail'))
    return 'retail';
  if (agents.some(a => a.startsWith('company_') || a === 'scoop' || a === 'strongbox')
      || id.includes('company') || id.includes('pi4b') || label.includes('company'))
    return 'company';
  if (agents.includes('synthos_monitor') || id.includes('monitor') || label.includes('monitor'))
    return 'monitor';
  // Fallback: check for legacy agent names
  if (agents.some(a => a.includes('trade') || a.includes('news') || a.includes('sentiment')))
    return 'retail';
  return null;  // unknown role — only show reported agents
}

function agentStatusClass(s, ageSecs) {
  if (s === 'fault' || s === 'error') return 'fault';
  if (!s) return 'inactive';
  if (ageSecs > 900) return 'idle';  // >15 min since last heartbeat
  return 'active';
}

function renderAgentFleet() {
  const body  = document.getElementById('aft-body');
  const badge = document.getElementById('aft-badge');
  if (!body) return;

  const pis = Object.values(piData);
  const rows = [];
  pis.forEach(pi => {
    const reported = pi.agents || {};
    const ageSecs = pi.age_secs || 0;
    const lastSeen = pi.last_seen || '';
    const role = detectNodeRole(pi);

    // Start with all expected agents for this node role (marked inactive)
    const merged = {};
    if (role && EXPECTED_AGENTS[role]) {
      EXPECTED_AGENTS[role].forEach(k => { merged[k] = null; });
    }
    // Overlay reported agents on top
    Object.entries(reported).forEach(([k, v]) => { merged[k] = v; });

    Object.entries(merged).forEach(([key, status]) => {
      rows.push({
        agent: AGENT_NAMES[key] || key,
        agentKey: key,
        node: pi.label || pi.pi_id,
        status: agentStatusClass(status, ageSecs),
        rawStatus: status,
        lastSeen: status ? lastSeen : ''
      });
    });
  });

  if (!rows.length) {
    body.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:16px;text-align:center">No nodes registered yet</div>';
    badge.textContent = '0';
    return;
  }

  // Sort: fault first, then active, idle, inactive
  const sord = {fault:0, active:1, idle:2, inactive:3};
  rows.sort((a,b) => (sord[a.status]||9) - (sord[b.status]||9) || a.node.localeCompare(b.node));

  const activeCount = rows.filter(r => r.status === 'active').length;
  badge.textContent = activeCount + ' / ' + rows.length;
  body.innerHTML =
    '<div class="aft-row aft-thead"><div>Agent</div><div>Node</div><div>Status</div><div>Last</div></div>'
    + rows.map(r =>
      '<div class="aft-row">'
        + '<div class="aft-agent">' + escHtml(r.agent) + '</div>'
        + '<div class="aft-node">' + escHtml(r.node) + '</div>'
        + '<div class="aft-status"><div class="aft-dot s-' + r.status + '"></div><span class="aft-st s-' + r.status + '">' + r.status + '</span></div>'
        + '<div class="aft-time">' + (r.lastSeen ? ageSince(r.lastSeen) : '\u2014') + '</div>'
      + '</div>'
    ).join('');
}

// ── GLOBAL COMMANDS ──
let cmdConfirmType = null;
let cmdConfirmValue = null;

function confirmCmd(type, value, msg) {
  cmdConfirmType = type;
  cmdConfirmValue = value;
  document.getElementById('confirm-msg').textContent = msg;
  document.getElementById('confirm-overlay').classList.add('show');
}

async function sendGlobalCmd(type, value) {
  const statusEl = document.getElementById('cc-status');
  try {
    statusEl.textContent = 'sending...';
    statusEl.style.color = 'var(--amber)';
    const body = type === 'kill-switch' ? {active: value} : {mode: value};
    const r = await fetch('/api/command/' + type, {
      method: 'POST',
      headers: {'X-Token': SECRET_TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    if (r.ok) {
      const d = await r.json();
      toast('\u2713 ' + type + ' \u2192 ' + value + ' queued for ' + (d.queued_for||[]).length + ' nodes', 'ok');
      statusEl.textContent = 'queued';
      statusEl.style.color = 'var(--teal)';
    } else {
      toast('Command failed: HTTP ' + r.status, 'err');
      statusEl.textContent = 'failed';
      statusEl.style.color = 'var(--pink)';
    }
  } catch(e) {
    toast('Command error: ' + e.message, 'err');
    statusEl.textContent = 'error';
    statusEl.style.color = 'var(--pink)';
  }
  setTimeout(() => { statusEl.textContent = ''; }, 5000);
}

function updateCommandState(pis) {
  // Admin override state drives button highlights
  var tg = admOverrides.trading_gate || 'ALL';
  var om = admOverrides.operating_mode || 'ALL';

  // Kill switch from Pi data
  var ks = pis.map(function(p) { return !!p.kill_switch; });
  var anyKill = ks.some(function(k) { return k; });
  var noKill  = ks.every(function(k) { return !k; });

  var cls = function(id, c, on) {
    var el = document.getElementById(id);
    if (el) {
      el.classList.remove('active-teal','active-amber','active-pink','active-purple');
      if (on) el.classList.add(c);
    }
  };
  cls('cmd-paper',      'active-teal',   tg === 'PAPER');
  cls('cmd-live',       'active-amber',  tg === 'LIVE');
  cls('cmd-gate-all',   'active-purple', tg === 'ALL');
  cls('cmd-managed',    'active-teal',   om === 'MANAGED');
  cls('cmd-automatic',  'active-amber',  om === 'AUTOMATIC');
  cls('cmd-mode-all',   'active-purple', om === 'ALL');
  cls('cmd-kill-on',    'active-pink',   anyKill);
  cls('cmd-kill-off',   'active-teal',   noKill && pis.length > 0);

  // Subtitle text
  var gs = document.getElementById('adm-gate-sub');
  var ms = document.getElementById('adm-mode-sub');
  if (gs) gs.textContent = tg === 'ALL' ? 'Customers choose' : 'Forced: ' + tg;
  if (ms) ms.textContent = om === 'ALL' ? 'Customers choose' : 'Forced: ' + om;
}

// ── DELETE ──
function promptDelete(piId) {
  pendingDelete = piId;
  document.getElementById('confirm-msg').textContent = 'Remove "' + piId + '" from the registry?';
  document.getElementById('confirm-overlay').classList.add('show');
}
async function toggleSilence(piId) {
  try {
    const r = await fetch('/api/silence/' + encodeURIComponent(piId), {method:'POST'});
    if (!r.ok) { toast('Failed to toggle silence', 'err'); return; }
    const d = await r.json();
    if (piData[piId]) piData[piId].silenced = d.silenced;
    renderNodeRoster();
    const lbl = (piData[piId] && piData[piId].label) || piId;
    toast(d.silenced ? '\U0001F507 Alerts muted for ' + lbl : '\U0001F514 Alerts unmuted for ' + lbl, 'ok');
  } catch(e) { toast('Error: ' + e, 'err'); }
}
function cancelDelete() {
  pendingDelete=null;
  cmdConfirmType=null;
  cmdConfirmValue=null;
  document.getElementById('confirm-overlay').classList.remove('show');
}
async function confirmDelete() {
  // Handle global command confirmation
  if (cmdConfirmType) {
    const t = cmdConfirmType, v = cmdConfirmValue;
    cmdConfirmType = null; cmdConfirmValue = null;
    document.getElementById('confirm-overlay').classList.remove('show');
    if (t === 'trading_gate' || t === 'operating_mode') {
      await setAdmOverride(t, v);
    } else {
      await sendGlobalCmd(t, v);
    }
    return;
  }
  // Handle Pi delete confirmation
  if (!pendingDelete) return;
  try {
    await fetch('/api/delete/' + encodeURIComponent(pendingDelete), {
      method:'DELETE', headers:{'X-Token':SECRET_TOKEN}
    });
    toast('\u2713 Pi removed', 'ok');
  } catch(e) {}
  cancelDelete();
  closeModalBtn();
  fetchStatus();
}

// ── TODOS ──
async function fetchTodos() {
  try {
    const r = await fetch('/api/auditor/findings');
    if (!r.ok) return;
    const data = await r.json();
    allTodos = (data.issues || []).map(function(i) {
      return {
        id: i.id, title: i.context ? i.context.substring(0, 120) : 'Unknown',
        severity: (i.severity || 'low').toUpperCase(),
        category: i.source_file || '', pi_id: '',
        date: i.last_seen ? i.last_seen.substring(0,10) : '',
        action: 'Hits: ' + (i.hit_count || 1),
        resolved: false
      };
    });
    allTodos.sort((a,b) => (SEV_ORDER[a.severity]??9) - (SEV_ORDER[b.severity]??9));
    renderTodos();
    updateFleetStats();
  } catch(e) {}
}

function renderTodos() {
  const el    = document.getElementById('todo-list');
  const badge = document.getElementById('todo-badge');
  const open  = allTodos.filter(t=>!t.resolved);
  badge.textContent = open.length > 0 ? open.length + ' open' : 'All clear';
  badge.className   = 'todo-count ' + (open.length > 0 ? '' : 'clear');
  if (!open.length) { el.innerHTML = '<div class="todo-empty">✓ No open issues</div>'; return; }
  const sevDot = {CRITICAL:'ts-crit',HIGH:'ts-high',MEDIUM:'ts-med',LOW:'ts-low'};
  el.innerHTML = open.slice(0,15).map(t =>
    '<div class="todo-item">'
      + '<div class="tsev ' + (sevDot[t.severity]||'ts-low') + '"></div>'
      + '<div class="todo-body">'
        + '<div class="todo-title-t">' + escHtml(t.title||'') + '</div>'
        + '<div class="todo-meta">' + (t.pi_id||'') + ' · ' + (t.date||'') + ' · ' + (t.category||'') + '</div>'
        + (t.action ? '<div class="todo-action">→ ' + escHtml(t.action) + '</div>' : '')
      + '</div>'
      + '<button class="resolve-btn" data-todoid="' + CSS.escape(t.id) + '" onclick="resolveTodo(this.dataset.todoid,event)">Done</button>'
    + '</div>'
  ).join('');
}

async function nodePower(piId, action, e) {
  e.stopPropagation();
  var verb = action === "shutdown" ? "shut down" : "reboot";
  if (!confirm("Are you sure you want to " + verb + " " + piId + "?")) return;
  try {
    var r = await fetch("/api/node/" + encodeURIComponent(piId) + "/power", {
      method: "POST",
      headers: {"X-Token": SECRET_TOKEN, "Content-Type": "application/json"},
      body: JSON.stringify({action: action})
    });
    var d = await r.json();
    if (d.ok) {
      alert(piId + " " + verb + " command sent.");
    } else {
      alert("Error: " + (d.error || "Unknown"));
    }
  } catch(err) {
    alert("Request failed: " + err.message);
  }
}

// ── PUSH KEYS TO PI ──
async function pushKeysToPi(piId) {
  const pi = piData[piId] || {};
  // Build Pi portal URL from known port
  const piUrl = 'http://' + (pi.pi_ip || piId.replace('synthos-','').replace(/-/g,'.')) + ':5001';

  const fields = {
    'ANTHROPIC_API_KEY': document.getElementById('adm-anthropic')?.value || '',
    'ALPACA_API_KEY':    document.getElementById('adm-alpaca-key')?.value || '',
    'ALPACA_SECRET_KEY': document.getElementById('adm-alpaca-secret')?.value || '',
    'ALERT_TO':          document.getElementById('adm-alert-to')?.value || '',
  };
  const data = Object.fromEntries(Object.entries(fields).filter(([,v]) => v.trim()));
  if (!Object.keys(data).length) {
    toast('Fill in at least one key field', 'err');
    return;
  }

  const result = document.getElementById('adm-key-result-' + piId);
  if (result) result.textContent = 'Pushing...';

  try {
    // POST directly to Pi portal's /api/keys endpoint
    const r = await fetch(piUrl + '/api/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
      mode: 'cors',
    });
    const d = await r.json();
    if (d.ok) {
      if (result) { result.textContent = '✓ Updated: ' + d.updated.join(', '); result.style.color = 'var(--teal)'; }
      toast('✓ Keys pushed to ' + (pi.label||piId), 'ok');
      // Clear fields
      ['adm-anthropic','adm-alpaca-key','adm-alpaca-secret','adm-alert-to'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
    } else {
      if (result) { result.textContent = '✗ ' + d.errors.join(', '); result.style.color = 'var(--pink)'; }
      toast('Push failed: ' + d.errors.join(', '), 'err');
    }
  } catch(e) {
    if (result) { result.textContent = '✗ Could not reach Pi portal'; result.style.color = 'var(--pink)'; }
    toast('Could not reach ' + piUrl, 'err');
  }
}

async function resolveTodo(id, e) {
  e.stopPropagation();
  await fetch('/api/auditor/resolve/' + encodeURIComponent(id) + '', {
    method:'POST', headers:{'X-Token':SECRET_TOKEN}
  });
  await fetchTodos();
  toast('✓ Issue resolved', 'ok');
}

// ── FLEET CHARTS ──
const CHART_COLORS = ['#00f5d4','#7b61ff','#ffb347','#ff4b6e','#a78bfa','#67e8f9'];
let cpuChartInst = null;
let ramChartInst = null;

function buildFleetCharts() {
  const pis  = Object.values(piData).filter(p => p.history && p.history.length > 1);
  if (!pis.length) return;

  const cpuCtx = document.getElementById('cpu-chart');
  const ramCtx = document.getElementById('ram-chart');
  if (!cpuCtx || !ramCtx) return;

  // Use the longest history for labels
  const refPi = pis.reduce((a,b) => a.history.length >= b.history.length ? a : b);
  const labels = refPi.history.map(h =>
    new Date(h.t).toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'})
  );

  const mkDatasets = (histKey, alpha) => pis
    .filter(p => p.history.some(h => h[histKey] != null))
    .map((pi, i) => {
      const color = CHART_COLORS[i % CHART_COLORS.length];
      return {
        label: pi.label || pi.pi_id,
        data:  pi.history.map(h => h[histKey] != null ? h[histKey] : null),
        borderColor: color, borderWidth: 2,
        fill: true, backgroundColor: colorWithAlpha(color, alpha),
        tension: 0.4, pointRadius: 0, pointHitRadius: 8, spanGaps: true,
      };
    });

  const chartOpts = unit => ({
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { display: pis.length > 1, position: 'bottom',
                labels:{color:'rgba(255,255,255,0.4)',font:{size:9},boxWidth:8,padding:8}},
      tooltip: {
        backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
        titleColor:'rgba(255,255,255,0.5)',bodyColor:'rgba(255,255,255,0.85)',
        callbacks:{label:c=>(c.dataset.label||'')+': '+c.parsed.y.toFixed(1)+unit}
      }
    },
    scales: {
      x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:8}},
      y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:v=>v+unit},min:0,max:100,position:'right'}
    }
  });

  if (cpuChartInst) cpuChartInst.destroy();
  if (ramChartInst) ramChartInst.destroy();

  const cpuDs = mkDatasets('cpu', 0.1);
  const ramDs = mkDatasets('ram', 0.1);
  if (cpuDs.length) cpuChartInst = new Chart(cpuCtx, {type:'line', data:{labels,datasets:cpuDs}, options:chartOpts('%')});
  if (ramDs.length) ramChartInst = new Chart(ramCtx, {type:'line', data:{labels,datasets:ramDs}, options:chartOpts('%')});
}

// ── COUNTDOWN ──
let countdown = 10;
function tickCountdown() {
  countdown--;
  if (countdown <= 0) { countdown = 10; fetchStatus(); }
}


// ── COMMAND PANEL ──
function toggleCmdPanel() {
  var p = document.getElementById('cc-slideout');
  var o = document.getElementById('cc-overlay');
  var isOpen = p.classList.contains('open');
  if (isOpen) { closeCmdPanel(); }
  else { p.classList.add('open'); o.classList.add('open'); loadCmdStatus(); }
}
function closeCmdPanel() {
  document.getElementById('cc-slideout').classList.remove('open');
  document.getElementById('cc-overlay').classList.remove('open');
  if (window._cmdInterval) { clearInterval(window._cmdInterval); window._cmdInterval = null; }
}
async function runCommand(btn) {
  var action = btn.dataset.action;
  btn.classList.add('running');
  var origLabel = btn.querySelector('.cc-btn-label').textContent;
  btn.querySelector('.cc-btn-label').textContent = 'Starting...';
  try {
    var r = await fetch('/api/command/run-agent', {
      method: 'POST',
      headers: {'X-Token': SECRET_TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    });
    var d = await r.json();
    if (d.ok) {
      toast(d.message, 'ok');
      btn.querySelector('.cc-btn-label').textContent = 'Running...';
      setTimeout(function() {
        btn.classList.remove('running');
        btn.querySelector('.cc-btn-label').textContent = origLabel;
      }, 15000);
    } else {
      toast(d.error || 'Command failed', 'err');
      btn.classList.remove('running');
      btn.querySelector('.cc-btn-label').textContent = origLabel;
    }
  } catch(e) {
    toast('Could not reach server', 'err');
    btn.classList.remove('running');
    btn.querySelector('.cc-btn-label').textContent = origLabel;
  }
  // Refresh status after short delay
  setTimeout(loadCmdStatus, 3000);
  // Start auto-refresh while panel is open
  if (!window._cmdInterval) {
    window._cmdInterval = setInterval(loadCmdStatus, 10000);
  }
}
async function loadCmdStatus() {
  try {
    var r = await fetch('/api/command/agent-status', {
      headers: {'X-Token': SECRET_TOKEN}
    });
    var d = await r.json();
    var el = document.getElementById('cc-events');
    if (!d.events || !d.events.length) {
      el.innerHTML = '<div style="color:var(--dim);font-size:10px;padding:4px 0">No recent activity</div>';
      return;
    }
    el.innerHTML = d.events.map(function(ev) {
      var time = (ev.timestamp || '').slice(11, 16);
      var isComplete = ev.event === 'AGENT_COMPLETE';
      var icon = isComplete ? '<span style="color:var(--teal)">&#x2713;</span>' : '<span style="color:var(--amber)">&#x25B6;</span>';
      var label = (isComplete ? '' : '') + (ev.agent || 'Agent') + (isComplete ? ' complete' : ' started');
      return '<div class="cmd-event">'
        + '<span class="cmd-event-time">' + time + '</span>'
        + '<span class="cmd-event-icon">' + icon + '</span>'
        + '<span class="cmd-event-name">' + label + '</span>'
        + '</div>';
    }).join('');
  } catch(e) {
    document.getElementById('cc-events').innerHTML = '<div style="color:var(--pink);font-size:10px">Status unavailable</div>';
  }
}


// ── MARKET ACTIVITY + USER SESSIONS CHARTS ──
//
// The endpoint returns two separate visualizations in one response:
//   market_activity — today's session (9:30-16:00 ET), 10-min bins,
//                     buy / sell / net flow in dollars.
//   user_sessions   — 24h rolling window, hourly bins, user-count only.
// We render both from one fetch so the panel stays in sync.
let _mktChart = null;
let _sessChart = null;
let _mktData = null;
// Sessions stays on by default; the user-count line lives on its own
// chart now, so the mkt toggles cover only the three money series.
let _mktVis = {buys:true, sells:true, net:true};
// Session-date paging state. null = "current session" (default).
// A YYYY-MM-DD string = view that historical session.
let _mktViewDate = null;

function mktToggle(key, btn, cls) {
  _mktVis[key] = !_mktVis[key];
  if (_mktVis[key]) btn.classList.add(cls);
  else btn.classList.remove(cls);
  buildMktChart();
}

// ── SESSION NAVIGATION ──
// Prev / Today / Next buttons next to the session label. Backend
// returns prev_session_date and next_session_date in each response so
// we don't need to implement weekday-skip logic client-side. The
// "Today" button is hidden when already on the current session.
function mktPrevDay() {
  var ma = _mktData && _mktData.market_activity;
  // Prefer backend-provided prev_session_date; fall back to simple
  // -1 day on client if payload is missing it (e.g. first render).
  if (ma && ma.prev_session_date) {
    _mktViewDate = ma.prev_session_date;
  } else {
    var base = _mktViewDate ? new Date(_mktViewDate) : new Date();
    base.setDate(base.getDate() - 1);
    _mktViewDate = base.toISOString().slice(0, 10);
  }
  fetchMktActivity();
}

function mktNextDay() {
  var ma = _mktData && _mktData.market_activity;
  if (!ma || !ma.next_session_date) return;  // already on current session
  _mktViewDate = ma.next_session_date;
  fetchMktActivity();
}

function mktToday() {
  _mktViewDate = null;
  fetchMktActivity();
}

async function fetchMktActivity() {
  try {
    var url = '/api/proxy/market-activity?hours=24';
    if (_mktViewDate) url += '&date=' + encodeURIComponent(_mktViewDate);
    var r = await fetch(url, {headers:{'X-Token':SECRET_TOKEN}});
    if (!r.ok) return;
    _mktData = await r.json();
    buildMktChart();
    buildSessionsChart();
    updateMktSummary();
    updateSessionNav();
  } catch(e) { console.error('fetchMktActivity:', e); }
}

function updateSessionNav() {
  // Middle button always shows the session being viewed. When on the
  // current session, text is "Today" and clicking is a no-op; when on
  // a prior session, text is that date (e.g. "Thu Apr 17") and clicking
  // returns to today. User asked for the middle button to reflect the
  // day they're currently looking at rather than always reading "Today".
  var ma = _mktData && _mktData.market_activity;
  if (!ma) return;
  var nextBtn  = document.getElementById('mkt-next');
  var todayBtn = document.getElementById('mkt-today');
  if (nextBtn)  nextBtn.disabled = !ma.next_session_date;
  if (todayBtn) {
    if (ma.is_current_session) {
      todayBtn.textContent = 'Today';
      todayBtn.title       = 'Viewing current session';
      todayBtn.disabled    = true;
    } else if (ma.session_date) {
      try {
        var d = new Date(ma.session_date + 'T12:00');
        todayBtn.textContent = d.toLocaleDateString(undefined,
          {weekday:'short', month:'short', day:'numeric'});
      } catch(e) {
        todayBtn.textContent = ma.session_date;
      }
      todayBtn.title    = 'Click to return to current session';
      todayBtn.disabled = false;
    }
  }
}

// ── MARKET-HOURS CHART (today's session, 10-min bins) ──
function buildMktChart() {
  if (!_mktData || !_mktData.market_activity) return;
  var ctx = document.getElementById('mkt-chart');
  if (!ctx) return;
  var ma = _mktData.market_activity;

  // Session label e.g. "Fri Apr 18, 9:30–16:00 ET"
  try {
    var dStr = ma.session_date;
    var lblEl = document.getElementById('mkt-session-label');
    if (lblEl && dStr) {
      var d = new Date(dStr + 'T12:00');
      var opts = {weekday:'short', month:'short', day:'numeric'};
      lblEl.textContent = d.toLocaleDateString(undefined, opts) + ', 9:30–16:00 ET';
    }
  } catch(e) {}

  var labels = ma.bins || [];
  var datasets = [];
  var custColors = ['#00f5d4','#7b61ff','#22d3ee','#a78bfa','#67e8f9','#f0abfc','#fbbf24','#34d399'];
  var customers  = ma.customers || {};
  var custIds    = Object.keys(customers);

  // 2026-04-30 — V2 customers (parallel-test bot) get pinned to purple
  // and a [V2] suffix in the legend so they're immediately distinguishable
  // from the v1 fleet in the stacked chart. The v2 SELL color is purple-
  // toned too so when the test bot exits a position it doesn't blur into
  // the fleet's pink sells.
  function _custColor(c, i, side) {
    if (c.variant === 'v2') {
      return side === 'sell' ? '#c084fc' : '#a78bfa';   // purple shades
    }
    return side === 'sell' ? '#ff4b6e' : custColors[i % custColors.length];
  }
  function _custLabel(c, side) {
    var base = c.name + ' ' + side + 's';
    return c.variant === 'v2' ? c.name + ' [V2] ' + side + 's' : base;
  }
  if (_mktVis.buys) {
    if (custIds.length > 0) {
      custIds.forEach(function(cid, i) {
        var c = customers[cid];
        var color = _custColor(c, i, 'buy');
        datasets.push({
          type:'bar', label:_custLabel(c, 'buy'), data:c.buys, stack:'buys',
          backgroundColor:colorWithAlpha(color,0.7), borderColor:color,
          borderWidth:1, borderRadius:2, yAxisID:'y', order:2
        });
      });
    } else {
      datasets.push({
        type:'bar', label:'Buys', data:ma.buys, stack:'buys',
        backgroundColor:colorWithAlpha('#00f5d4',0.65), borderColor:'#00f5d4',
        borderWidth:1, borderRadius:3, yAxisID:'y', order:2
      });
    }
  }
  if (_mktVis.sells) {
    if (custIds.length > 0) {
      custIds.forEach(function(cid, i) {
        var c = customers[cid];
        var negSells = c.sells.map(function(v){return -v;});
        var color = _custColor(c, i, 'sell');
        datasets.push({
          type:'bar', label:_custLabel(c, 'sell'), data:negSells, stack:'sells',
          backgroundColor:colorWithAlpha(color,0.5), borderColor:color,
          borderWidth:1, borderRadius:2, yAxisID:'y', order:2
        });
      });
    } else {
      datasets.push({
        type:'bar', label:'Sells', data:ma.sells.map(function(v){return -v;}), stack:'sells',
        backgroundColor:colorWithAlpha('#ff4b6e',0.65), borderColor:'#ff4b6e',
        borderWidth:1, borderRadius:3, yAxisID:'y', order:2
      });
    }
  }
  if (_mktVis.net) {
    // Net series comes pre-computed from the backend now (buys - sells).
    var netD = ma.net || [];
    datasets.push({
      type:'bar', label:'Net Flow', data:netD,
      backgroundColor:netD.map(function(v){return v>=0?colorWithAlpha('#7b61ff',0.6):colorWithAlpha('#ff4b6e',0.6);}),
      borderColor:netD.map(function(v){return v>=0?'#7b61ff':'#ff4b6e';}),
      borderWidth:1, borderRadius:3, yAxisID:'y', order:1
    });
  }

  if (_mktChart) _mktChart.destroy();
  _mktChart = new Chart(ctx, {
    type:'bar',
    data:{labels:labels, datasets:datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        // Bottom legend disabled 2026-04-28 — operator note: it grew
        // proportionally with the customer count and dominated the
        // chart's vertical space. Tooltip on hover already gives the
        // per-customer label + dollar amount, so the legend was
        // redundant. Re-enable here if a non-hover identification
        // path is ever needed.
        legend:{display:false},
        tooltip:{
          backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
          titleColor:'rgba(255,255,255,0.5)',bodyColor:'rgba(255,255,255,0.85)',
          callbacks:{
            title:function(items){
              // Tooltip title shows the 10-min window, e.g. "10:30–10:40 ET"
              if (!items.length) return '';
              var idx = items[0].dataIndex;
              var start = (ma.bins||[])[idx] || '';
              var end   = (ma.bins||[])[idx+1] || '16:00';
              return start + '–' + end + ' ET';
            },
            label:function(c){
              var v = c.parsed.y;
              if (!v) return null;
              return c.dataset.label+': $'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
            }
          }
        }
      },
      scales:{
        x:{
          grid:{color:'rgba(255,255,255,0.04)'},
          ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:14,autoSkip:true}
        },
        y:{
          stacked:true,
          position:'left', grid:{color:'rgba(255,255,255,0.04)'},
          ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:function(v){return(v<0?'-':'')+'$'+Math.abs(v).toLocaleString();}},
          title:{display:true,text:'Dollars',color:'rgba(255,255,255,0.15)',font:{size:8}}
        }
      }
    }
  });
}

// ── USER SESSIONS CHART ──
// Responds to the same date-nav as the Market Activity chart above.
//   - no date selected → rolling last 24h (hours like "3am", "4am", …)
//   - date selected    → ET 0:00-23:59 of that date, same hour labels,
//                        data from the session_history DB table
function buildSessionsChart() {
  if (!_mktData || !_mktData.user_sessions) return;
  var ctx = document.getElementById('sess-chart');
  if (!ctx) return;
  var us = _mktData.user_sessions;

  // Label next to "User Sessions" reflects what we're looking at:
  //   historical → localized short date (matches market chart label)
  //   rolling    → "24h"
  try {
    var lbl = document.getElementById('sess-window-label');
    if (lbl) {
      if (us.mode === 'historical' && us.session_date) {
        var d = new Date(us.session_date + 'T12:00');
        lbl.textContent = d.toLocaleDateString(undefined,
          {weekday:'short', month:'short', day:'numeric'}) + ', ET';
      } else {
        lbl.textContent = '24h';
      }
    }
  } catch(e) {}

  var labels = (us.hours || []).map(function(h) {
    var d = new Date(h + ':00');
    var hr = d.getHours();
    var ampm = hr >= 12 ? 'pm' : 'am';
    hr = hr % 12 || 12;
    return hr + ampm;
  });
  var counts = us.counts || [];

  if (_sessChart) _sessChart.destroy();
  _sessChart = new Chart(ctx, {
    type:'line',
    data:{
      labels: labels,
      datasets:[{
        label:'Active Users',
        data: counts,
        borderWidth:3, tension:0.35, pointRadius:2, pointHitRadius:8,
        fill:true,
        backgroundColor: colorWithAlpha('#ffb347', 0.1),
        pointBackgroundColor: counts.map(function(v){
          if (v>=10) return '#ff4b6e';
          if (v>=3)  return '#ffb347';
          return '#ffb347';
        }),
        segment:{
          borderColor:function(c){
            var v=c.p0.parsed.y;
            if (v>=10) return '#ff4b6e';
            return '#ffb347';
          }
        },
        borderColor:'#ffb347'
      }]
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
          titleColor:'rgba(255,255,255,0.5)',bodyColor:'rgba(255,255,255,0.85)',
          callbacks:{
            label:function(c){ return 'Users: ' + c.parsed.y; },
            afterBody:function(items){
              if (!_mktData || !items.length) return '';
              var idx = items[0].dataIndex;
              var hourKey = (_mktData.user_sessions.hours || [])[idx];
              var users = (_mktData.user_sessions.names || {})[hourKey];
              if (!users || !users.length) return '';
              return ['', 'Active this hour:'].concat(users.map(function(n){return '  ● '+n;}));
            }
          }
        }
      },
      scales:{
        x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:12}},
        y:{
          beginAtZero:true,
          grid:{color:'rgba(255,255,255,0.04)'},
          ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},stepSize:1,precision:0},
          title:{display:true,text:'Users',color:'rgba(255,255,255,0.15)',font:{size:8}}
        }
      }
    }
  });
}

function updateMktSummary() {
  if (!_mktData || !_mktData.summary) return;
  var s = _mktData.summary;
  var fmt = function(v){return '$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});};
  var el = function(id){return document.getElementById(id);};
  el('ms-buys').textContent = fmt(s.total_buys);
  el('ms-sells').textContent = fmt(s.total_sells);
  var netEl = el('ms-net');
  netEl.textContent = (s.net_flow>=0?'+':'-') + fmt(s.net_flow);
  netEl.style.color = s.net_flow>=0?'var(--teal)':'var(--pink)';
  el('ms-active').textContent = s.active_now;
  el('ms-peak').textContent = s.peak_sessions;
}

// ── MONITOR CONTROL CENTER ──
var _monPanelOpen=false, _monTab='notif';
function toggleMonPanel(){_monPanelOpen=!_monPanelOpen;document.getElementById('mon-panel').classList.toggle('open',_monPanelOpen);document.getElementById('mon-overlay').classList.toggle('open',_monPanelOpen);if(_monPanelOpen){switchMonTab(_monTab);_updateBellBadge();}}
function closeMonPanel(){_monPanelOpen=false;document.getElementById('mon-panel').classList.remove('open');document.getElementById('mon-overlay').classList.remove('open');}
function switchMonTab(tab){_monTab=tab;document.querySelectorAll('.mon-tab').forEach(function(t){t.classList.remove('active');});document.getElementById('mon-tab-'+tab).classList.add('active');if(tab==='notif')loadMonNotifications();else if(tab==='cmd')loadMonCommands();else if(tab==='wave')loadWaveControls();}
async function loadMonNotifications(){
  var el = document.getElementById('mon-tab-content');
  el.innerHTML = '<div class="mon-empty">Loading...</div>';
  var html = '';

  // Helper — read styling when item is older than ack timestamp.
  // _isAfterAck returns true for "still unread"; invert for the class.
  function _readClass(ts){ return _isAfterAck(ts) ? '' : ' mon-notif-read'; }

  try {
    var r1 = await fetch('/api/proxy/pending-signups?status=PENDING',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r1.ok) {
      var d1 = await r1.json();
      var sg = d1.pending || d1.signups || [];
      if (Array.isArray(sg) && sg.length) {
        sg.slice(0, 3).forEach(function(s){
          var rc = _readClass(s.created_at || s.signup_date || s.timestamp);
          html += '<a href="/approvals" style="text-decoration:none">'
            + '<div class="mon-notif' + rc + '">'
            + '<div class="mon-notif-dot" style="background:var(--amber)"></div>'
            + '<div class="mon-notif-body">'
            + '<div class="mon-notif-title">' + (s.name || 'New Signup') + '</div>'
            + '<div class="mon-notif-sub">Pending approval</div>'
            + '</div></div></a>';
        });
      }
    }
  } catch(e) {}

  try {
    var r2 = await fetch('/api/proxy/support/all-tickets',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r2.ok) {
      var d2 = await r2.json();
      var tks = (d2.tickets || []).filter(function(t){ return t.status === 'open'; });
      if (tks.length) {
        tks.slice(0, 5).forEach(function(t){
          var dc = t.category === 'direct_message' ? 'var(--teal)' : 'var(--pink)';
          var rc = _readClass(t.created_at || t.updated_at || t.timestamp);
          html += '<a href="/support-queue" style="text-decoration:none">'
            + '<div class="mon-notif' + rc + '">'
            + '<div class="mon-notif-dot" style="background:' + dc + '"></div>'
            + '<div class="mon-notif-body">'
            + '<div class="mon-notif-title">' + (t.customer_name || 'Customer') + '</div>'
            + '<div class="mon-notif-sub">' + (t.subject || 'Support ticket') + '</div>'
            + '</div></div></a>';
        });
      }
    }
  } catch(e) {}

  try {
    var r3 = await fetch('/api/queue?status=pending',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r3.ok) {
      var d3 = await r3.json();
      var nc = (d3.queue || []).filter(function(e){ return e.event_type === 'NEW_CUSTOMER'; });
      if (nc.length) {
        nc.slice(0, 5).forEach(function(n){
          var rc = _readClass(n.queued_at || n.created_at || n.timestamp);
          html += '<div class="mon-notif' + rc + '">'
            + '<div class="mon-notif-dot" style="background:var(--teal)"></div>'
            + '<div class="mon-notif-body">'
            + '<div class="mon-notif-title">' + (n.subject || 'New customer') + '</div>'
            + '<div class="mon-notif-sub">' + (n.body || 'Auto-approved').slice(0, 60) + '</div>'
            + '</div></div>';
        });
      }
    }
  } catch(e) {}

  el.innerHTML = html || '<div class="mon-empty">No notifications</div>';
}
function loadMonCommands(){var el=document.getElementById('mon-tab-content');function cb(a,l,s,f){return '<button data-action="'+a+'" onclick="runMonCmd(this)" style="padding:10px 12px;border-radius:10px;border:1px solid var(--border);background:var(--surface2);cursor:pointer;text-align:left;font-family:var(--sans);'+(f?'grid-column:1/-1;':'')+'" onmouseover="this.style.borderColor=&#39;rgba(255,255,255,0.15)&#39;" onmouseout="this.style.borderColor=&#39;var(--border)&#39;">'+'<div style="font-size:11px;font-weight:600;color:var(--text)">'+l+'</div><div style="font-size:9px;color:var(--muted)">'+s+'</div></button>';}el.innerHTML='<div style="margin-bottom:14px"><div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Agents</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+cb('news_overnight','News','Overnight')+cb('news_market','News','Market')+cb('sentiment','Sentiment','The Pulse')+cb('screener','Screener','Sectors')+cb('trade','Trade Logic','All customers',true)+'</div></div><div><div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Sessions</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+cb('prep_session','Prep','Full prep')+cb('open_session','Open','Market open')+cb('midday_session','Midday','Mid check')+cb('close_session','Close','End of day')+'</div></div>';}
async function runMonCmd(btn){var action=btn.dataset.action;var orig=btn.querySelector('div').textContent;btn.querySelector('div').textContent='Starting...';btn.style.borderColor='rgba(123,97,255,0.3)';try{var r=await fetch('/api/command/run-agent',{method:'POST',headers:{'X-Token':SECRET_TOKEN,'Content-Type':'application/json'},body:JSON.stringify({action:action})});var d=await r.json();if(d.ok){toast(d.message,'ok');}else{toast(d.error||'Failed','err');}}catch(e){toast('Error','err');}setTimeout(function(){btn.querySelector('div').textContent=orig;btn.style.borderColor='var(--border)';},10000);}
var _prevPendingSignups=-1;var _prevNewCustomers=-1;

// 2026-04-28 — bell "Mark all as read" semantics. Operator note:
// without this, every signup / open ticket / queued NEW_CUSTOMER
// shows on the badge forever until you act on it. The bell's job
// is "tell me about NEW things"; once seen, badge can dim. Local
// dismiss only — doesn't acknowledge the underlying records on the
// backend (a pending signup stays pending until approved/rejected
// on /approvals). New items past the ack timestamp re-light the
// badge naturally.
function _bellAckIso(){return localStorage.getItem('mon_bell_ack_at') || '';}
function _isAfterAck(ts){
  // Returns true if the item's timestamp is strictly newer than the
  // last ack. Items missing all timestamps default to TRUE (= count
  // them) so we never silently hide something we can't time.
  var ack = _bellAckIso();
  if (!ack) return true;
  if (!ts) return true;
  return String(ts) > ack;
}
function markAllBellRead(){
  localStorage.setItem('mon_bell_ack_at', new Date().toISOString());
  _updateBellBadge();
  // Re-render the panel so items visually flip to read-styled
  // (dimmed + grey dots). Without this, click "Mark read" looked
  // like a no-op — items stayed bright, button vanished.
  if (_monPanelOpen && _monTab === 'notif') {
    loadMonNotifications();
  }
  toast('Marked as read', 'ok');
}
function _updateMarkReadButton(unread){
  var btn = document.getElementById('mon-mark-all-read');
  if (!btn) return;
  btn.style.display = (unread > 0 ? 'inline-block' : 'none');
}

function _updateBellBadge(){(async function(){
  var total = 0;
  var signupCount = 0;

  try {
    var r1 = await fetch('/api/proxy/pending-signups?status=PENDING',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r1.ok) {
      var d1 = await r1.json();
      var sg = d1.pending || d1.signups || [];
      // For toast (genuine new arrivals) we still use raw count delta —
      // toast should fire on actual new customers, not on browser-tab
      // re-acks. For badge we filter by ack timestamp.
      signupCount = sg.length;
      total += sg.filter(function(s){
        return _isAfterAck(s.created_at || s.signup_date || s.timestamp);
      }).length;
      if (signupCount > _prevPendingSignups && _prevPendingSignups >= 0
          && _prevPendingSignups !== -1) {
        var newest = sg[sg.length - 1];
        var name = (newest && (newest.name || newest.display_name)) || 'New customer';
        toast(name + ' just signed up — pending approval', 'ok');
      }
      _prevPendingSignups = signupCount;
    }
  } catch(e) {}

  try {
    var r2 = await fetch('/api/proxy/support/all-tickets',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r2.ok) {
      var d2 = await r2.json();
      total += (d2.tickets || [])
        .filter(function(t){ return t.status === 'open'; })
        .filter(function(t){
          return _isAfterAck(t.created_at || t.updated_at || t.timestamp);
        }).length;
    }
  } catch(e) {}

  try {
    var r3 = await fetch('/api/queue?status=pending',
                         {headers:{'X-Token':SECRET_TOKEN}});
    if (r3.ok) {
      var d3 = await r3.json();
      var ncEvents = (d3.queue || [])
        .filter(function(e){ return e.event_type === 'NEW_CUSTOMER'; });
      total += ncEvents.filter(function(e){
        return _isAfterAck(e.queued_at || e.created_at || e.timestamp);
      }).length;
      if (ncEvents.length > _prevNewCustomers && _prevNewCustomers >= 0) {
        var ne = ncEvents[ncEvents.length - 1];
        toast((ne.subject || 'New customer joined'), 'ok');
      }
      _prevNewCustomers = ncEvents.length;
    }
  } catch(e) {}

  var badge = document.getElementById('mon-bell-badge');
  if (total > 0) {
    badge.textContent = total;
    badge.classList.add('active');
  } else {
    badge.classList.remove('active');
  }
  _updateMarkReadButton(total);
})();}
setInterval(_updateBellBadge,60000);
setTimeout(_updateBellBadge,2000);

// ── INIT ──
/* DBG */ try { document.getElementById('dbg-keys').textContent = 'INIT REACHED'; } catch(e){}

function loadWaveControls(){
  var el=document.getElementById('mon-tab-content');
  el.innerHTML='<div style="margin-bottom:16px">'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:12px">Wave Override</div>'
    +'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'
    +'<label style="font-size:11px;color:var(--text);display:flex;align-items:center;gap:6px;cursor:pointer">'
    +'<input type="checkbox" id="wave-override" onchange="toggleWaveOverride()"'+(window._waveOverride?' checked':'')+'>Override animation</label></div>'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Agent Color</div>'
    +'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:16px">'
    +'<button onclick="setWaveAgent(&#39;teal&#39;)" style="padding:8px;border-radius:8px;border:1px solid rgba(0,245,212,0.3);background:rgba(0,245,212,0.1);color:#00f5d4;font-size:10px;font-weight:700;cursor:pointer">Trade</button>'
    +'<button onclick="setWaveAgent(&#39;purple&#39;)" style="padding:8px;border-radius:8px;border:1px solid rgba(123,97,255,0.3);background:rgba(123,97,255,0.1);color:#7b61ff;font-size:10px;font-weight:700;cursor:pointer">News</button>'
    +'<button onclick="setWaveAgent(&#39;amber&#39;)" style="padding:8px;border-radius:8px;border:1px solid rgba(255,179,71,0.3);background:rgba(255,179,71,0.1);color:#ffb347;font-size:10px;font-weight:700;cursor:pointer">Sentiment</button>'
    +'<button onclick="setWaveAgent(&#39;pink&#39;)" style="padding:8px;border-radius:8px;border:1px solid rgba(255,75,110,0.3);background:rgba(255,75,110,0.1);color:#ff4b6e;font-size:10px;font-weight:700;cursor:pointer">Screener</button>'
    +'</div>'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Amplitude</div>'
    +'<input type="range" id="wave-amp" min="5" max="40" value="'+(window._waveAmpOverride||30)+'" oninput="setWaveAmp(this.value)" style="width:100%;accent-color:var(--teal)">'
    +'<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:2px"><span>Idle</span><span id="wave-amp-val">'+(window._waveAmpOverride||30)+'</span><span>Max</span></div>'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-top:14px;margin-bottom:8px">Speed</div>'
    +'<input type="range" id="wave-speed" min="3" max="30" value="'+(window._waveSpeedOverride||10)+'" oninput="setWaveSpeed(this.value)" style="width:100%;accent-color:var(--teal)">'
    +'<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:2px"><span>Slow</span><span id="wave-speed-val">'+(window._waveSpeedOverride||10)+'</span><span>Fast</span></div>'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-top:14px;margin-bottom:8px">Frequency</div>'
    +'<input type="range" id="wave-freq" min="3" max="30" value="'+(window._waveFreqOverride||10)+'" oninput="setWaveFreq(this.value)" style="width:100%;accent-color:var(--teal)">'
    +'<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:2px"><span>Low</span><span id="wave-freq-val">'+(window._waveFreqOverride||10)+'</span><span>High</span></div>'
    +'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-top:14px;margin-bottom:8px">Direction</div>'
    +'<div style="display:flex;gap:8px">'
    +'<button onclick="setWaveDir(1)" style="flex:1;padding:8px;border-radius:8px;border:1px solid rgba(0,245,212,0.3);background:rgba(0,245,212,0.1);color:#00f5d4;font-size:10px;font-weight:700;cursor:pointer">&#x25B6; Forward</button>'
    +'<button onclick="setWaveDir(-1)" style="flex:1;padding:8px;border-radius:8px;border:1px solid rgba(255,75,110,0.3);background:rgba(255,75,110,0.1);color:#ff4b6e;font-size:10px;font-weight:700;cursor:pointer">&#x25C0; Reverse</button>'
    +'</div>'
    +'</div>';
}
window._waveOverride=false;window._waveColorOverride=null;window._waveAmpOverride=null;window._waveSpeedOverride=10;window._waveFreqOverride=10;window._waveDirOverride=1;
function toggleWaveOverride(){window._waveOverride=document.getElementById('wave-override').checked;if(!window._waveOverride){window._waveColorOverride=null;window._waveAmpOverride=null;}pushWaveOverride();}
function setWaveAgent(color){window._waveColorOverride=color;window._waveOverride=true;var cb=document.getElementById('wave-override');if(cb)cb.checked=true;pushWaveOverride();toast('Wave: '+color,'ok');}
function setWaveAmp(val){window._waveAmpOverride=parseInt(val);document.getElementById('wave-amp-val').textContent=val;pushWaveOverride();}
function setWaveSpeed(val){window._waveSpeedOverride=parseInt(val);document.getElementById('wave-speed-val').textContent=val;pushWaveOverride();}
function setWaveFreq(val){window._waveFreqOverride=parseInt(val);document.getElementById('wave-freq-val').textContent=val;pushWaveOverride();}
function setWaveDir(d){window._waveDirOverride=d;window._waveOverride=true;var cb=document.getElementById('wave-override');if(cb)cb.checked=true;pushWaveOverride();loadWaveControls();}
function pushWaveOverride(){fetch('/api/command/wave-override',{method:'POST',headers:{'X-Token':SECRET_TOKEN,'Content-Type':'application/json'},body:JSON.stringify({override:window._waveOverride,color:window._waveColorOverride,amplitude:window._waveAmpOverride,speed:window._waveSpeedOverride/10,frequency:window._waveFreqOverride/10,direction:window._waveDirOverride})}).catch(function(){});}

// ── ADMIN OVERRIDES ──
let admOverrides = {trading_gate:'ALL', operating_mode:'ALL'};
async function fetchAdminOverrides() {
  try {
    const r = await fetch('/api/admin-override');
    if (r.ok) { admOverrides = await r.json(); updateCommandState(Object.values(piData)); }
  } catch(e) {}
}
async function setAdmOverride(field, val) {
  var payload = Object.assign({}, admOverrides);
  payload[field] = val;
  try {
    var r = await fetch('/api/admin-override', {
      method:'POST',
      headers:{'Content-Type':'application/json', 'X-Token': SECRET_TOKEN},
      body: JSON.stringify(payload)
    });
    var d = await r.json();
    if (d.ok) {
      admOverrides = payload;
      updateCommandState(Object.values(piData));
      toast((field==='trading_gate'?(val==='ALL'?'Trading gate unlocked':'Trading forced to '+val):(val==='ALL'?'Operating mode unlocked':'Mode forced to '+val))+' · pushed to '+(d.pushed_to?d.pushed_to.length:0)+' node(s)', 'ok');
    }
  } catch(e) { toast('Override push failed', 'err'); }
}

// Behavior baseline counter (Phase 7L+ 2026-04-26) — calls
// /api/behavior-baseline which proxies to pi5. Populates the small
// panel above the AI Triage list. Operator-only — moved off the
// customer portal because it doesn't help end users.
async function fetchBehaviorBaseline() {
  try {
    const r = await fetch('/api/behavior-baseline');
    if (!r.ok) return;
    const d = await r.json();
    const days  = document.getElementById('bb-days');
    const line1 = document.getElementById('bb-line1');
    const line2 = document.getElementById('bb-line2');
    const line3 = document.getElementById('bb-line3');
    if (!days || !line1) return;
    const b = d.baseline;
    if (!b) {
      days.textContent  = '—';
      line1.textContent = 'No baseline set';
      line2.textContent = 'Call db.set_behavior_baseline(reason, commit_sha) on pi5 to record one.';
      line3.textContent = '';
      return;
    }
    const td = (typeof d.trading_days_since === 'number') ? d.trading_days_since : null;
    const cd = (typeof d.calendar_days_since === 'number') ? d.calendar_days_since : null;
    days.textContent  = (td !== null) ? `${td}td` : '—';
    days.title        = (td !== null) ? `${td} trading day${td===1?'':'s'} stable` : '';
    line1.innerHTML   = `<span style="color:var(--teal);font-weight:700;letter-spacing:0.06em">STABLE</span> — last change ${(b.set_at||'').slice(0,10)}`
                      + (b.commit_sha ? ` <span style="opacity:0.5">(${b.commit_sha.slice(0,7)})</span>` : '')
                      + ` · ${td !== null ? td + ' trading day' + (td===1?'':'s') : '—'}`
                      + (cd !== null ? ` <span style="opacity:0.5">(${cd}d cal)</span>` : '');
    line2.textContent = b.reason || '';
    line3.textContent = `set_by=${b.set_by||'?'}`;
  } catch (e) { /* silent */ }
}

fetchStatus();
fetchTodos();
fetchMktActivity();
fetchAdminOverrides();
fetchBehaviorBaseline();
setInterval(tickCountdown, 1000);
setInterval(fetchTodos, 30000);
setInterval(fetchMktActivity, 60000);
setInterval(fetchBehaviorBaseline, 60000);
function toggleMenu(){const m=document.getElementById('hmenu');m.classList.toggle('open')}
document.addEventListener('click',function(e){if(!document.getElementById('hbtn').contains(e.target)&&!document.getElementById('hmenu').contains(e.target)){document.getElementById('hmenu').classList.remove('open')}});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# COMPANY SERVER ROUTES (merged from company_server.py)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/queue/stats", methods=["GET"])
def api_queue_stats():
    """Status-bucketed counts for the /console stat-card grid.

    Added 2026-04-28 — operator caught that the cards at the top of
    the page (Pending / Sent / Failed / Skipped / Total) were all
    showing '—' or 0. Root cause: the page's refresh() was reading
    counts from /health, but /health doesn't expose queue counts
    (intentionally — it's unauthenticated and used by external
    uptime monitors). Added a dedicated auth-gated endpoint here.

    Returns:
      {
        total: int                      — every row in scoop_queue
        by_status: {pending,sent,failed,skipped,...}
                   — exact-status counts, lowercased
        sent_combined: int              — sent + SENT + dispatched,
                   the page uses this for the "Sent" card since
                   'dispatched' is a successful in-flight state
                   (daemon already submitted to Resend, awaiting
                   final confirmation)
        skipped_combined: int           — skipped + expired,
                   both are administratively cleared
      }
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT LOWER(status) AS status, COUNT(*) AS n "
                "FROM scoop_queue GROUP BY LOWER(status)"
            ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        sent_combined = (by_status.get("sent", 0)
                         + by_status.get("dispatched", 0))
        skipped_combined = (by_status.get("skipped", 0)
                            + by_status.get("expired", 0))
        return jsonify({
            "total":             total,
            "by_status":         by_status,
            "sent_combined":     sent_combined,
            "skipped_combined":  skipped_combined,
            "pending":           by_status.get("pending", 0),
            "failed":            by_status.get("failed", 0),
        })
    except Exception as e:
        log.warning(f"/api/queue/stats failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue", methods=["GET"])
def api_queue():
    """
    Inspect the scoop_queue.

    Query params:
      status  — filter by status (default: pending)
      pi_id   — filter by source Pi
      limit   — max rows (default: 50, max: 200)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    status = request.args.get("status", "pending")
    pi_id  = request.args.get("pi_id")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    try:
        with _db_conn() as conn:
            if pi_id:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? AND pi_id=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, pi_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, limit),
                ).fetchall()

            counts = {
                r["status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
                ).fetchall()
            }

        return jsonify({
            "queue":  [dict(r) for r in rows],
            "counts": counts,
            "filter": {"status": status, "pi_id": pi_id, "limit": limit},
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/skip", methods=["POST"])
def api_queue_skip(item_id):
    """Mark a pending queue item as skipped (won't be dispatched by Scoop)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='skipped', dispatched_at=? "
                "WHERE id=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(), item_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in pending state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "skipped"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/retry", methods=["POST"])
def api_queue_retry(item_id):
    """Reset a failed item back to pending so Scoop will retry it."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='pending', dispatch_attempts=0, "
                "error_msg=NULL, dispatched_at=NULL "
                "WHERE id=? AND status='failed'",
                (item_id,),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in failed state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "pending"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Node</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --green:#00f5d4;--red:#ff4b6e;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:56px;
  display:flex;align-items:center;gap:12px;
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
          color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
              padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);
              background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
           background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
           font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);
          box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* STAT CARDS */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.stat-card{
  padding:16px;border-radius:14px;
  border:1px solid var(--border);background:var(--surface);
  position:relative;overflow:hidden;
}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* TOOLBAR */
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:var(--muted);display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}
.tab-row{display:flex;gap:4px}
.tab-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
         cursor:pointer;border:1px solid var(--border);background:transparent;
         color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.tab-btn.active,.tab-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal);background:rgba(0,245,212,0.06)}
.ml-auto{margin-left:auto}
.refresh-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
             cursor:pointer;border:1px solid var(--border);background:transparent;
             color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.refresh-btn:hover{border-color:var(--border2);color:var(--text)}

/* TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{
  padding:10px 14px;text-align:left;
  font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);border-bottom:1px solid var(--border);
  white-space:nowrap;
}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:10px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px}
.empty-row td{text-align:center;color:var(--muted);padding:32px;font-style:italic}

/* BADGES */
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
       padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-pending{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-sent{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.b-failed{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.25);color:var(--pink)}
.b-skipped{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}
.b-p0{background:rgba(255,75,110,0.15);border-color:rgba(255,75,110,0.35);color:var(--pink)}
.b-p1{background:rgba(255,179,71,0.12);border-color:rgba(255,179,71,0.3);color:var(--amber)}
.b-p2{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-p3{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* ACTION BUTTONS */
.act-btn{font-size:9px;font-weight:700;padding:2px 8px;border-radius:6px;
         background:transparent;border:1px solid var(--border);color:var(--muted);
         cursor:pointer;font-family:var(--sans);transition:all 0.15s}
.act-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal)}
.act-btn.danger:hover{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* TOAST */
#toast{
  position:fixed;bottom:20px;right:20px;z-index:999;
  padding:10px 16px;border-radius:10px;font-size:12px;font-weight:600;
  background:var(--surface);border:1px solid var(--border2);color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,0.4);
  transform:translateY(60px);opacity:0;transition:all 0.3s;
  pointer-events:none;
}
#toast.show{transform:translateY(0);opacity:1}
#toast.ok{border-color:rgba(0,245,212,0.3);color:var(--teal)}
#toast.err{border-color:rgba(255,75,110,0.3);color:var(--pink)}
</style>
</head>
<body>

{{ subpage_hdr|safe }}


<div class="page">

  <!-- STAT CARDS -->
  <div class="stat-grid" id="stat-grid">
    <div class="stat-card sc-purple">
      <div class="stat-label">Pending</div>
      <div class="stat-val" id="cnt-pending">—</div>
      <div class="stat-sub">awaiting Scoop</div>
    </div>
    <div class="stat-card sc-teal">
      <div class="stat-label">Sent</div>
      <div class="stat-val" id="cnt-sent">—</div>
      <div class="stat-sub">dispatched ok</div>
    </div>
    <div class="stat-card sc-pink">
      <div class="stat-label">Failed</div>
      <div class="stat-val" id="cnt-failed">—</div>
      <div class="stat-sub">dispatch errors</div>
    </div>
    <div class="stat-card sc-muted">
      <div class="stat-label">Skipped</div>
      <div class="stat-val" id="cnt-skipped">—</div>
      <div class="stat-sub">manually resolved</div>
    </div>
    <div class="stat-card sc-amber">
      <div class="stat-label">Total</div>
      <div class="stat-val" id="cnt-total">—</div>
      <div class="stat-sub">all time</div>
    </div>
  </div>

  <!-- QUEUE TABLE -->
  <div style="margin-bottom:12px">
    <div class="sec-title" style="margin-bottom:12px">Scoop Queue</div>
    <div class="toolbar">
      <div class="tab-row" id="status-tabs">
        <button class="tab-btn active" onclick="setStatus('pending',this)">Pending</button>
        <button class="tab-btn" onclick="setStatus('failed',this)">Failed</button>
        <button class="tab-btn" onclick="setStatus('sent',this)">Sent</button>
        <button class="tab-btn" onclick="setStatus('skipped',this)">Skipped</button>
      </div>
      <button class="refresh-btn ml-auto" onclick="refresh()">↻ Refresh</button>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Priority</th>
          <th>Event Type</th>
          <th>Subject</th>
          <th>Source Agent</th>
          <th>Pi</th>
          <th>Audience</th>
          <th>Status</th>
          <th>Queued</th>
          <th>Attempts</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="queue-body">
        <tr class="empty-row"><td colspan="10">Loading…</td></tr>
      </tbody>
    </table>
  </div>

</div>

<div id="toast"></div>

<script>
const TOKEN = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('company_token='))?.split('=')[1] || '';
const SECRET_TOKEN = '{{ secret_token }}';
let currentStatus = 'pending';

function clock(){
  const now = new Date();
  var _ck = document.getElementById('clock') || document.getElementById('syn-clk');
  if(!_ck) return;
  _ck.textContent =
    now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false}) + ' ET';
}
clock(); setInterval(clock,1000);

function toast(msg, type='ok'){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + type;
  setTimeout(()=>{ t.className = ''; }, 3000);
}

function priorityBadge(p){
  const labels = {0:'P0 CRIT',1:'P1 HIGH',2:'P2 MED',3:'P3 LOW'};
  const cls    = {0:'b-p0',1:'b-p1',2:'b-p2',3:'b-p3'};
  return `<span class="badge ${cls[p]||'b-p3'}">${labels[p]||'P'+p}</span>`;
}

function statusBadge(s){
  const cls = {pending:'b-pending',sent:'b-sent',failed:'b-failed',skipped:'b-skipped'};
  return `<span class="badge ${cls[s]||''}">${s.toUpperCase()}</span>`;
}

function fmtTime(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-US',{timeZone:'America/New_York',hour12:false,
    month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function actionBtns(item){
  const id = item.id;
  if(item.status === 'pending'){
    return `<button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  if(item.status === 'failed'){
    return `<button class="act-btn" onclick="retryItem('${id}')">Retry</button>
            <button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  return '—';
}

async function fetchQueue(status){
  const r = await fetch(`/api/queue?status=${status}&limit=100`,{headers:{'X-Token':SECRET_TOKEN}});
  return r.json();
}

async function fetchHealth(){
  const r = await fetch('/health');
  return r.json();
}

async function refresh(){
  // Update counts.
  // 2026-04-28 — switched from /health (returns no queue field, so
  // the cards always showed 0) to /api/queue/stats which is purpose-
  // built. 'dispatched' rolled into Sent (successful in-flight to
  // Resend); 'expired' rolled into Skipped (admin-cleared).
  try {
    const r = await fetch('/api/queue/stats',{headers:{'X-Token':SECRET_TOKEN}});
    if (r.ok) {
      const s = await r.json();
      const set = (id,v)=>{var el=document.getElementById(id);if(el)el.textContent=v;};
      set('cnt-pending', s.pending           || 0);
      set('cnt-sent',    s.sent_combined     || 0);
      set('cnt-failed',  s.failed            || 0);
      set('cnt-skipped', s.skipped_combined  || 0);
      set('cnt-total',   s.total             || 0);
    }
  } catch(e){}

  // Update queue table
  try {
    const data = await fetchQueue(currentStatus);
    const items = data.queue || [];
    const tbody = document.getElementById('queue-body');
    if(!items.length){
      tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No ${currentStatus} items</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(item=>`
      <tr>
        <td>${priorityBadge(item.priority)}</td>
        <td class="mono">${item.event_type||'—'}</td>
        <td style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
            title="${(item.subject||'').replace(/"/g,'&quot;')}">${item.subject||'—'}</td>
        <td>${item.source_agent||'—'}</td>
        <td class="mono" style="font-size:10px">${item.pi_id||'—'}</td>
        <td>${item.audience||'customer'}</td>
        <td>${statusBadge(item.status)}</td>
        <td class="mono" style="font-size:10px">${fmtTime(item.queued_at)}</td>
        <td style="text-align:center">${item.dispatch_attempts||0}</td>
        <td>${actionBtns(item)}</td>
      </tr>
    `).join('');
  } catch(e){
    document.getElementById('queue-body').innerHTML =
      `<tr class="empty-row"><td colspan="10">Failed to load queue</td></tr>`;
  }
}

async function skipItem(id){
  const r = await fetch(`/api/queue/${id}/skip`,{method:'POST',headers:{'X-Token':SECRET_TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item skipped'); refresh(); }
  else toast(j.error||'Skip failed','err');
}

async function retryItem(id){
  const r = await fetch(`/api/queue/${id}/retry`,{method:'POST',headers:{'X-Token':SECRET_TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item queued for retry'); refresh(); }
  else toast(j.error||'Retry failed','err');
}

function setStatus(status, btn){
  currentStatus = status;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  refresh();
}

// Auto-refresh every 15 seconds
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


# ── Logs page ─────────────────────────────────────────────────────────────────
_COMPANY_LOGS_CSS = (
    '<style>'
    '*{box-sizing:border-box;margin:0;padding:0}'
    'body{background:#080b12;color:#e0ddd8;font-family:sans-serif;min-height:100vh}'
    'header{background:#0e1220;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;'
    '       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;'
    '       border-bottom:1px solid #1a2030}'
    '.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}'
    '.nav{display:flex;gap:1rem;align-items:center}'
    '.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}'
    '.nav a:hover{color:#aaa}'
    '.tabs{display:flex;gap:0;border-bottom:1px solid #1a2030;padding:0 2rem;'
    '      background:#0e1220;overflow-x:auto;flex-wrap:nowrap}'
    '.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;'
    '          background:#0e1220;border-bottom:1px solid #1a2030}'
    '.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}'
    'select{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1a2030;'
    '       border-radius:6px;color:#e0ddd8}'
    '.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;'
    '         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;'
    '         min-height:calc(100vh - 140px)}'
    '.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;'
    '             padding:0.3rem 0.75rem;border:1px solid #1a2030;'
    '             border-radius:6px;cursor:pointer;background:transparent;color:#556}'
    '.refresh-btn:hover{background:#1a2030;color:#e0ddd8}'
    '</style>'
)

_LOG_SOURCES = {
    # ── PI4B (Company Node) — local files ──
    'auditor':     {'node': 'pi4b', 'file': 'auditor_daemon.log', 'label': 'Auditor'},
    'scoop':       {'node': 'pi4b', 'file': 'scoop.log',         'label': 'Scoop'},
    'sentinel':    {'node': 'pi4b', 'file': 'sentinel.log',      'label': 'Sentinel'},
    'vault':       {'node': 'pi4b', 'file': 'vault.log',         'label': 'Vault'},
    'fidget':      {'node': 'pi4b', 'file': 'fidget.log',        'label': 'Fidget'},
    'librarian':   {'node': 'pi4b', 'file': 'librarian.log',     'label': 'Librarian'},
    'archivist':   {'node': 'pi4b', 'file': 'archivist.log',     'label': 'Archivist'},
    'heartbeat':   {'node': 'pi4b', 'file': 'heartbeat.log',     'label': 'Heartbeat'},
    # ── PI5 (Retail Node) — via SSH ──
    'scheduler':   {'node': 'pi5',  'file': 'scheduler.log',     'label': 'Scheduler'},
    'portal':      {'node': 'pi5',  'file': 'portal.log',        'label': 'Portal'},
    'poller':      {'node': 'pi5',  'file': 'price_poller.log',  'label': 'Price Poller'},
    'backup':      {'node': 'pi5',  'file': 'retail_backup.log', 'label': 'Backup'},
    'watchdog':    {'node': 'pi5',  'file': 'watchdog.log',      'label': 'Watchdog'},
    'boot':        {'node': 'pi5',  'file': 'boot.log',          'label': 'Boot Seq'},
    'pi5-hb':      {'node': 'pi5',  'file': 'heartbeat.log',     'label': 'Heartbeat'},
}
_PI5_LOG_DIR = '/home/pi516gb/synthos/synthos_build/logs'
# Backward compat
_COMPANY_LOG_FILES = {k: v['file'] for k, v in _LOG_SOURCES.items() if v['node'] == 'pi4b'}


# ── Retail backup receiver ────────────────────────────────────────────────────
_BUILD_DIR    = os.path.dirname(_HERE)   # synthos_build/ (parent of src/)
_STAGING_ROOT = os.path.join(_BUILD_DIR, ".backup_staging")


@app.route("/receive_backup", methods=["POST"])
def receive_backup():
    """
    Accept a .tar.gz backup archive from a retail Pi and stage it for Strongbox.
    Auth: X-Token header or token cookie (same as /console).
    Form fields: pi_id (str), archive (file)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    pi_id = (request.form.get("pi_id") or "").strip()
    if not pi_id or "/" in pi_id or ".." in pi_id:
        return jsonify({"error": "valid pi_id required"}), 400

    f = request.files.get("archive")
    if not f:
        return jsonify({"error": "archive file required"}), 400

    staging_dir = os.path.join(_STAGING_ROOT, pi_id)
    os.makedirs(staging_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = f"synthos_backup_{pi_id}_{date_str}.tar.gz"
    fpath = os.path.join(staging_dir, fname)
    f.save(fpath)

    size_kb = os.path.getsize(fpath) / 1024
    print(f"[Company] Staged backup: {fname} ({size_kb:.1f} KB) from pi_id={pi_id}")
    return jsonify({"ok": True, "staged": fname, "size_kb": round(size_kb, 1)}), 200



def _subpage_header(page_name):
    """Return a complete sub-page header HTML block. Works in Jinja, f-strings, and plain strings."""
    return (
        '<title>Synthos — ' + page_name + '</title>'
        
        '<style>'
        '.syn-hdr{position:sticky;top:0;z-index:200;background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.07);padding:0 24px;height:52px;display:flex;align-items:center;gap:12px}'
        '.syn-wm{font-family:var(--mono,"JetBrains Mono",monospace);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4;text-shadow:0 0 20px rgba(0,245,212,0.4);flex-shrink:0}'
        '.syn-sub{font-size:11px;color:rgba(255,255,255,0.4);font-family:var(--mono,"JetBrains Mono",monospace)}'
        '.syn-right{margin-left:auto;display:flex;align-items:center;gap:8px}'
        '.syn-clk{font-family:var(--mono,"JetBrains Mono",monospace);font-size:11px;color:rgba(255,255,255,0.4)}'
        '.syn-back{padding:5px 12px;border-radius:8px;font-size:11px;font-weight:600;background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.15);color:#00f5d4;text-decoration:none;letter-spacing:0.04em}'
        '.syn-back:hover{background:rgba(0,245,212,0.12)}'
        '.syn-hb-wrap{position:relative}'
        '.syn-hb-btn{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;width:32px;height:32px;background:transparent;border:1px solid rgba(255,255,255,0.07);border-radius:8px;cursor:pointer;padding:0}'
        '.syn-hb-btn:hover{border-color:rgba(255,255,255,0.13)}'
        '.syn-hb-btn span{display:block;width:14px;height:1.5px;background:rgba(255,255,255,0.4);border-radius:2px}'
        '.syn-hm{display:none;position:absolute;top:calc(100% + 8px);right:0;min-width:180px;background:#111520;border:1px solid rgba(255,255,255,0.13);border-radius:12px;padding:6px;z-index:999;box-shadow:0 12px 40px rgba(0,0,0,0.5);flex-direction:column}'
        '.syn-hm.open{display:flex}'
        '.syn-hm a{display:block;padding:8px 14px;border-radius:8px;font-size:12px;font-weight:500;color:rgba(255,255,255,0.4);text-decoration:none;letter-spacing:0.03em;transition:all .15s}'
        '.syn-hm a:hover{background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.88)}'
        '</style>'
        '<header class="syn-hdr">'
        '<div class="syn-wm">SYNTHOS</div>'
        '<div class="syn-sub">' + page_name + '</div>'
        '<div class="syn-right">'
        '<div class="syn-clk" id="syn-clk">--:--:-- ET</div>'
        '<a href="/monitor" class="syn-back">&#8592; Monitor</a>'
        '<div class="syn-hb-wrap" id="_synwrap">'
        '<button class="syn-hb-btn" onclick="document.getElementById(\'_synhm\').classList.toggle(\'open\')" aria-label="Menu">'
        '<span></span><span></span><span></span></button>'
        '<div class="syn-hm" id="_synhm">'
        '<a href="/monitor">Monitor</a>'
        '<a href="/console">Scoop Queue</a>'
        '<a href="/maintenance">Maintenance</a>'
        '<a href="/project-status">Project Status</a>'
        '<a href="/system-architecture">System Architecture</a>'
        '<a href="/display">Sentinel Display</a>'
        '<a href="/audit">Auditor</a>'
        '<a href="/logs">Logs</a>'
        '<a href="/approvals">Approvals</a>'
        '<a href="/support-queue">Customer Support</a>'
        '<a href="/customer-billing">Customer Billing</a>'
        '<a href="/customer-activity">Customer Activity</a>'
        '<a href="/pill-usage">Pill Usage</a>'
        '<a href="/company-finances">Company Finances</a>'
        '<a href="/reports">Reports</a>'
        '<div style="height:1px;background:rgba(255,255,255,0.07);margin:4px 0"></div>'
        '<a href="/logout" style="color:#ff4b6e">Sign Out</a>'
        '</div></div></div></header>'
        '<script>'
        'document.addEventListener("click",function(e){var w=document.getElementById("_synwrap");var m=document.getElementById("_synhm");if(w&&m&&!w.contains(e.target))m.classList.remove("open")});'
        'function _synClk(){var t=new Date().toLocaleTimeString("en-US",{timeZone:"America/New_York",hour12:false});var c=document.getElementById("syn-clk");if(c)c.textContent=t+" ET"}_synClk();setInterval(_synClk,1000);'
        '</script>'
    )

_LOGS_HEADER = _subpage_header('Logs')


@app.route("/logs")
def company_logs():
    """Tail log files across all nodes — pi4b local + pi5 via SSH."""
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos Logs</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> "
            "or set <code>X-Token</code> header to access logs.</p>"
            "</body></html>"
        ), 401

    selected = request.args.get('file', 'auditor')
    try:
        lines = int(request.args.get('lines', 100))
    except (ValueError, TypeError):
        lines = 100

    src_info = _LOG_SOURCES.get(selected, _LOG_SOURCES.get('auditor'))
    node = src_info['node']
    fname = src_info['file']

    content = ''
    if node == 'pi4b':
        fpath = os.path.join(LOG_DIR, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    all_lines = f.readlines()
                content = ''.join(all_lines[-lines:])
            except Exception as e:
                content = f'Error reading log: {e}'
        else:
            content = f'Log file not found: {fpath}'
    elif node == 'pi5':
        import subprocess
        remote_path = f'{_PI5_LOG_DIR}/{fname}'
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', 'SentinelRetail',
                 f'tail -{lines} {remote_path} 2>/dev/null || echo "[Log file not found: {remote_path}]"'],
                capture_output=True, text=True, timeout=15,
            )
            content = result.stdout or f'No output from {remote_path}'
        except Exception as e:
            content = f'SSH error reading pi5 log: {e}'

    # Build tabs grouped by node
    pi4b_tabs = []
    pi5_tabs = []
    for k, info in _LOG_SOURCES.items():
        style = (
            f'padding:5px 12px;font-family:monospace;font-size:0.68rem;'
            f'letter-spacing:0.06em;text-decoration:none;border-radius:6px;'
            f'margin:2px;display:inline-block;'
        )
        if k == selected:
            style += f'background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.25);color:#00f5d4;'
        else:
            style += f'background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);color:#556;'
        tab = f'<a href="/logs?file={k}&lines={lines}" style="{style}">{info["label"]}</a>'
        if info['node'] == 'pi4b':
            pi4b_tabs.append(tab)
        else:
            pi5_tabs.append(tab)

    tabs = (
        '<div style="padding:12px 24px 4px">'
        '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.25);margin-bottom:6px">PI4B — Company Node</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:0">' + ''.join(pi4b_tabs) + '</div>'
        '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.25);margin:10px 0 6px">PI5 — Retail Node</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:0">' + ''.join(pi5_tabs) + '</div>'
        '</div>'
    )

    line_opts = ''.join(
        f'<option value="{n}" {"selected" if n == lines else ""}>{n} lines</option>'
        for n in [50, 100, 200, 500]
    )

    node_label = f'{src_info["label"]} ({node})'
    log_escaped = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Logs</title>
{_COMPANY_LOGS_CSS}
</head>
<body>
{_LOGS_HEADER}
<div style="padding:4px 24px 0"><a href="/logs?file={selected}&lines={lines}" onclick="location.reload();return false" style="font-size:11px;color:rgba(255,255,255,0.4);text-decoration:none">&#8635; Refresh</a></div>
{tabs}
<div class="controls">
  <label>Lines</label>
  <select onchange="window.location='/logs?file={selected}&lines='+this.value">{line_opts}</select>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  <span style="font-size:0.72rem;color:#556;margin-left:auto">{node_label}</span>
</div>
<div class="log-box" id="log-content">{log_escaped}</div>
<script>
  document.getElementById('log-content').scrollIntoView({{block:'end'}});
</script>
</body>
</html>"""

    return html


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Command</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c14;--surface:#111520;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.13);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;--mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:2rem}
.card{width:100%;max-width:340px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:2rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:20%;right:20%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,245,212,0.3),transparent)}
.logo{font-family:var(--mono);font-size:0.85rem;font-weight:500;letter-spacing:0.18em;color:var(--teal);text-shadow:0 0 18px rgba(0,245,212,0.35);margin-bottom:0.25rem}
.sub{font-size:0.75rem;color:var(--muted);margin-bottom:1.75rem}
label{display:block;font-size:0.62rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem}
input{font-family:var(--mono);font-size:0.82rem;width:100%;padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:7px;color:var(--text);margin-bottom:0.9rem;transition:border-color .15s}
input:focus{outline:none;border-color:rgba(0,245,212,0.4);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
input::placeholder{color:var(--dim)}
.btn{font-family:var(--mono);font-size:0.75rem;font-weight:500;letter-spacing:0.07em;width:100%;padding:0.55rem;background:rgba(0,245,212,0.1);color:var(--teal);border:1px solid rgba(0,245,212,0.25);border-radius:7px;cursor:pointer;transition:all .15s}
.btn:hover{background:rgba(0,245,212,0.18);box-shadow:0 0 14px rgba(0,245,212,0.15)}
.err{font-size:0.72rem;color:#ff4b6e;margin-bottom:0.8rem;padding:0.4rem 0.6rem;background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);border-radius:6px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">SYNTHOS</div>
  <div class="sub">Command Node</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" autocomplete="current-password" required>
    <button class="btn" type="submit">Sign In →</button>
  </form>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if _authorized():
        return redirect(url_for("monitor_dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        # Validate credentials
        email_ok = email == ADMIN_EMAIL
        pw_ok = (ADMIN_PW_HASH and check_password_hash(ADMIN_PW_HASH, password)) or \
                (not ADMIN_PW_HASH and password == SECRET_TOKEN)
        if email_ok and pw_ok:
            session.clear()
            session["logged_in"] = True
            session["email"] = email
            session.permanent = True
            return redirect(url_for("monitor_dashboard"))
        return render_template_string(_LOGIN_HTML, error="Incorrect email or password")
    return render_template_string(_LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def index():
    if not _authorized():
        return redirect(url_for("login"))
    return redirect(url_for("monitor_dashboard"))


@app.route("/scoop")
def scoop_redirect():
    return redirect("/monitor")


# ── SIGNUP APPROVAL PROXY (forward to retail portal on pi5) ─────────────────

@app.route("/api/proxy/pending-signups")
def proxy_pending_signups():
    """Proxy pending signups request to retail portal on pi5."""
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        status_q = request.args.get('status', '')
        url = f"{RETAIL_PORTAL_URL}/api/pending-signups"
        if status_q:
            url += f"?status={status_q}"
        r = _req.get(url, timeout=8, cookies={'synthos_s': _get_admin_session_cookie()})
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/approve-signup", methods=["POST"])
def proxy_approve_signup():
    """Proxy signup approval to retail portal on pi5."""
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/approve-signup",
            json=request.get_json(force=True),
            timeout=15,
            cookies={'synthos_s': _get_admin_session_cookie()},
            headers={'Content-Type': 'application/json'}
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/reject-signup", methods=["POST"])
def proxy_reject_signup():
    """Proxy signup rejection to retail portal on pi5."""
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/reject-signup",
            json=request.get_json(force=True),
            timeout=10,
            cookies={'synthos_s': _get_admin_session_cookie()},
            headers={'Content-Type': 'application/json'}
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/generate-invite", methods=["POST"])
def proxy_generate_invite():
    """Proxy invite code generation to retail portal on pi5."""
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/generate-invite",
            json={},
            timeout=10,
            cookies={'synthos_s': _get_admin_session_cookie()},
            headers={'Content-Type': 'application/json'}
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/invite-codes")
def proxy_invite_codes():
    """Proxy invite code listing to retail portal on pi5."""
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        r = _req.get(
            f"{RETAIL_PORTAL_URL}/api/invite-codes",
            timeout=8,
            cookies={'synthos_s': _get_admin_session_cookie()},
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502



# ── Invite Code Notes + Email ─────────────────────────────────────────────────

@app.route("/api/invite-notes")
def api_invite_notes():
    """Return all invite code notes."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _support_conn() as conn:
            rows = conn.execute(
                "SELECT code, recipient_name, recipient_email, sent_at, note, created_at "
                "FROM invite_notes ORDER BY created_at DESC"
            ).fetchall()
        return jsonify({"ok": True, "notes": {r["code"]: dict(r) for r in rows}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/invite-note", methods=["POST"])
def api_invite_note_save():
    """Save or update a note for an invite code."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip()
    note = (data.get("note") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    from datetime import datetime as _dt
    try:
        with _support_conn() as conn:
            existing = conn.execute("SELECT id FROM invite_notes WHERE code=?", (code,)).fetchone()
            if existing:
                conn.execute("UPDATE invite_notes SET note=? WHERE code=?", (note, code))
            else:
                conn.execute(
                    "INSERT INTO invite_notes (code, note, created_at) VALUES (?,?,?)",
                    (code, note, _dt.now().strftime("%Y-%m-%d %H:%M:%S")))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/invite-send-email", methods=["POST"])
def api_invite_send_email():
    """Send an invite code to a recipient via Resend."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip()
    email = (data.get("email") or "").strip()
    recipient_name = (data.get("recipient_name") or "").strip()
    if not code or not email:
        return jsonify({"error": "code and email required"}), 400
    if not RESEND_API_KEY:
        return jsonify({"error": "Resend API key not configured"}), 500
    import requests as _req
    from datetime import datetime as _dt
    try:
        greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"
        r = _req.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": ALERT_FROM,
                "to": [email],
                "subject": "Your Synthos Access Code",
                "html": (
                    '<div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px">'
                    '<h2 style="color:#0a0c14;margin-bottom:8px">Synthos Invite</h2>'
                    f"<p>{greeting}</p>"
                    "<p>You&#39;ve been invited to join Synthos. Use the code below when signing up:</p>"
                    '<div style="background:#0a0c14;color:#00f5d4;font-family:monospace;font-size:22px;'
                    'font-weight:700;letter-spacing:0.08em;padding:16px 24px;border-radius:10px;'
                    f'text-align:center;margin:16px 0">{code}</div>'
                    '<p style="color:#666;font-size:13px">This code is single-use and will expire once redeemed.</p>'
                    "</div>"
                ),
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            with _support_conn() as conn:
                existing = conn.execute("SELECT id FROM invite_notes WHERE code=?", (code,)).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE invite_notes SET recipient_email=?, recipient_name=?, sent_at=? WHERE code=?",
                        (email, recipient_name, now_str, code))
                else:
                    conn.execute(
                        "INSERT INTO invite_notes (code, recipient_email, recipient_name, sent_at, created_at) VALUES (?,?,?,?,?)",
                        (code, email, recipient_name, now_str, now_str))
            return jsonify({"ok": True, "message": f"Sent to {email}"})
        else:
            return jsonify({"error": f"Resend API error {r.status_code}: {r.text}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/proxy/market-activity")
def proxy_market_activity():
    """Proxy market activity data from retail portal for the dashboard chart."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        hours = request.args.get('hours', '24')
        # Pass through ?date=YYYY-MM-DD when the client is paging
        # backward through previous trading days. Omit from upstream
        # request when not provided so the backend's default (current
        # session) kicks in.
        params = {"hours": hours}
        date_arg = request.args.get('date', '').strip()
        if date_arg:
            params["date"] = date_arg
        cookie = _get_admin_session_cookie()
        r = _req.get(
            f"{RETAIL_PORTAL_URL}/api/admin/market-activity",
            params=params,
            cookies={"synthos_s": cookie},
            timeout=15,
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        print(f"[Monitor] market-activity proxy error: {e}")
        # Empty fallback in the new split shape. Chart builders on the
        # frontend handle empty arrays and render placeholder axes.
        return jsonify({
            "error": str(e),
            "market_activity": {
                "bins": [], "bin_starts": [], "buys": [], "sells": [], "net": [],
                "customers": {},
                "session_date": None,
            },
            "user_sessions": {
                "hours": [], "counts": [], "names": {},
                "active_now": 0, "active_customers": [], "peak": 0,
            },
            "summary": {
                "total_buys": 0, "total_sells": 0, "net_flow": 0,
                "buy_count": 0, "sell_count": 0,
                "active_now": 0, "peak_sessions": 0,
            },
            "trading_modes": {"PAPER": 0, "LIVE": 0, "total": 0},
        }), 502


def _get_admin_session_cookie():
    """
    Get a valid admin session cookie from the retail portal.
    Caches the session to avoid re-authenticating on every request.
    """
    if not hasattr(_get_admin_session_cookie, '_cache'):
        _get_admin_session_cookie._cache = {'session': None, 'expires': 0}

    import time, requests as _req
    cache = _get_admin_session_cookie._cache
    now = time.time()
    if cache['session'] and now < cache['expires']:
        return cache['session']

    admin_email = os.getenv('ADMIN_EMAIL', '')
    admin_pw    = os.getenv('ADMIN_PASSWORD', '')
    if not admin_email or not admin_pw:
        raise RuntimeError("ADMIN_EMAIL / ADMIN_PASSWORD not set in env")

    s = _req.Session()
    r = s.post(f"{RETAIL_PORTAL_URL}/login",
               data={'email': admin_email, 'password': admin_pw},
               allow_redirects=False, timeout=10)
    cookie = s.cookies.get('synthos_s') or s.cookies.get('session')
    if not cookie:
        raise RuntimeError("Failed to authenticate with retail portal")

    cache['session'] = cookie
    cache['expires'] = now + 3500  # ~1 hour
    return cookie

@app.route("/console")
def console():
    """Ops dashboard — requires login, Cloudflare Access, or SECRET_TOKEN."""
    # Legacy token-in-URL still works (sets cookie for API calls)
    if request.args.get("token"):
        resp = redirect(url_for("console"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return redirect(url_for("login"))
    return render_template_string(DASHBOARD_HTML, subpage_hdr=_subpage_header('Scoop Queue'), secret_token=SECRET_TOKEN)


# ── Project Status ────────────────────────────────────────────────────────────
_STATUS_JSON        = os.path.join(os.path.dirname(_HERE), "data", "project_status.json")
_GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "")
_GITHUB_OWNER       = os.getenv("GITHUB_REPO_OWNER", "personalprometheus-blip")
_GITHUB_STATUS_REPO = os.getenv("GITHUB_STATUS_REPO", "synthos")
_GITHUB_STATUS_PATH = os.getenv("GITHUB_STATUS_PATH", "synthos_build/data/project_status.json")
_STATUS_CACHE_TTL   = int(os.getenv("PROJECT_STATUS_TTL", "300"))

# System Architecture (same GitHub pull pattern)
_ARCH_JSON          = os.path.join(os.path.dirname(_HERE), "data", "system_architecture.json")
_GITHUB_ARCH_PATH   = os.getenv("GITHUB_ARCH_PATH", "synthos_build/data/system_architecture.json")
_arch_cache: dict   = {"data": None, "fetched_at": None, "source": "none"}   # seconds (default 5 min)

_status_cache: dict = {"data": None, "fetched_at": None, "source": "none"}


def _fetch_status_from_github():
    """
    Fetch project_status.json from the GitHub API.
    Returns the parsed dict on success, None on failure.
    Requires GITHUB_TOKEN in .env (read:contents scope is sufficient).
    Works for both public and private repos.
    """
    import urllib.request as _urllib_req
    import base64 as _b64
    import time as _time

    if not _GITHUB_TOKEN:
        return None

    url = (
        f"https://api.github.com/repos/{_GITHUB_OWNER}"
        f"/{_GITHUB_STATUS_REPO}/contents/{_GITHUB_STATUS_PATH}"
    )
    req = _urllib_req.Request(url, headers={
        "Authorization": f"token {_GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "synthos-company-server/1.0",
    })
    try:
        with _urllib_req.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        content = _b64.b64decode(payload["content"]).decode("utf-8")
        data = json.loads(content)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "github"
        print(f"[Company] project_status.json refreshed from GitHub")
        return data
    except Exception as exc:
        print(f"[Company] GitHub status fetch failed: {exc}")
        return None


def _get_status_data():
    """
    Return (data, source, cache_age_seconds).
    Priority: warm cache → GitHub API → local file → None.
    """
    import time as _time

    # Serve warm cache if within TTL
    if _status_cache["data"] and _status_cache["fetched_at"]:
        age = _time.time() - _status_cache["fetched_at"]
        if age < _STATUS_CACHE_TTL:
            return _status_cache["data"], _status_cache["source"], age

    # Try GitHub
    data = _fetch_status_from_github()
    if data:
        return data, "github", 0

    # Fall back to local file
    try:
        with open(_STATUS_JSON, "r") as fh:
            data = json.load(fh)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "local"
        print("[Company] project_status.json loaded from local file (GitHub unavailable)")
        return data, "local", 0
    except Exception as exc:
        print(f"[Company] project_status.json load failed: {exc}")
        return None, "error", 0

_STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Project Status</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{position:sticky;top:0;z-index:200;background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:16px}
.nav-link{font-size:11px;letter-spacing:0.06em;color:var(--muted);text-decoration:none;transition:color 0.15s}
.nav-link:hover{color:var(--text)}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
  background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* HERO PROGRESS */
.hero{padding:28px;border-radius:16px;border:1px solid var(--border);background:var(--surface);margin-bottom:24px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.hero-title{font-size:1.4rem;font-weight:700;letter-spacing:-0.3px}
.hero-meta{font-size:11px;color:var(--muted);margin-top:4px}
.phase-badge{font-family:var(--mono);font-size:11px;font-weight:600;padding:6px 14px;border-radius:8px;
  background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.milestone-chip{font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:4px 10px;border-radius:99px;background:rgba(255,179,71,0.1);border:1px solid rgba(255,179,71,0.25);color:var(--amber)}
.progress-label{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.progress-label span{font-size:11px;color:var(--muted)}
.progress-label strong{font-family:var(--mono);font-size:13px;color:var(--teal)}
.progress-track{height:6px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden}
.progress-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),rgba(0,245,212,0.6));transition:width 0.6s ease}

/* STAT GRID */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
.stat-card{padding:16px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* SECTION */
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);
  display:flex;align-items:center;gap:8px;margin-bottom:12px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* PHASE GRID */
.phase-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin-bottom:24px}
.phase-card{padding:18px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.phase-card.active{border-color:rgba(0,245,212,0.2);background:rgba(0,245,212,0.03)}
.phase-card.active::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.phase-card.not_started{opacity:0.5}
.phase-header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.phase-num{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 7px;border-radius:6px;
  background:rgba(255,255,255,0.05);color:var(--muted)}
.phase-name{font-size:13px;font-weight:600;flex:1}
.phase-status{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 8px;border-radius:99px;border:1px solid}
.ps-complete{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.ps-in_progress{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.ps-not_started{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.task-list{list-style:none;display:flex;flex-direction:column;gap:5px}
.task-item{display:flex;align-items:flex-start;gap:8px;font-size:11px;line-height:1.4}
.task-check{flex-shrink:0;width:14px;height:14px;margin-top:1px;border-radius:3px;border:1px solid;
  display:flex;align-items:center;justify-content:center;font-size:8px}
.task-check.done{background:rgba(0,245,212,0.15);border-color:rgba(0,245,212,0.4);color:var(--teal)}
.task-check.pending{background:rgba(255,255,255,0.03);border-color:var(--dim);color:transparent}
.task-text.done{color:var(--muted)}
.task-text.pending{color:var(--text)}
.phase-prog{margin-top:12px;padding-top:10px;border-top:1px solid var(--border)}
.phase-prog-track{height:3px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;margin-top:4px}
.phase-prog-fill{height:100%;border-radius:99px}
.ppf-complete{background:var(--teal)}
.ppf-in_progress{background:linear-gradient(90deg,var(--teal),var(--amber))}
.ppf-not_started{background:rgba(255,255,255,0.1)}
.phase-prog-label{font-size:10px;color:var(--dim)}

/* AGENT TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{padding:10px 14px;text-align:left;font-size:9px;font-weight:700;letter-spacing:0.1em;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:9px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
  padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-built{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-planned{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.b-done{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-pending{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.b-hold{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* SECURITY GRID */
.sec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-bottom:24px}
.sec-item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;
  border:1px solid var(--border);background:var(--surface)}
.sec-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sec-dot.done{background:var(--teal);box-shadow:0 0 6px rgba(0,245,212,0.5)}
.sec-dot.pending{background:var(--amber);box-shadow:0 0 6px rgba(255,179,71,0.4)}
.sec-dot.hold{background:var(--dim)}
.sec-info{flex:1;min-width:0}
.sec-label{font-size:11px;font-weight:500}
.sec-meta{font-size:10px;color:var(--muted);margin-top:1px}

/* BLOCKERS */
.blocker-empty{padding:24px;text-align:center;color:var(--muted);font-size:12px;font-style:italic}

/* FOOTER */
.footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.footer-left{display:flex;flex-direction:column;gap:3px}
.footer-note{font-size:10px;color:var(--dim)}
.source-bar{display:flex;align-items:center;gap:8px}
.source-chip{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;border:1px solid}
.source-github{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.source-local{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.source-error{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.3);color:var(--pink)}
.source-age{font-size:10px;color:var(--dim)}
.footer-right{display:flex;align-items:center;gap:10px}
.refresh-gh-btn{font-size:10px;font-weight:600;padding:5px 12px;border-radius:7px;cursor:pointer;
  border:1px solid rgba(0,245,212,0.25);background:rgba(0,245,212,0.05);color:var(--teal);
  font-family:var(--sans);transition:all 0.15s}
.refresh-gh-btn:hover{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.4)}
.refresh-gh-btn:disabled{opacity:0.4;cursor:not-allowed}
.footer-link{font-size:10px;color:var(--muted);text-decoration:none}
.footer-link:hover{color:var(--text)}
</style>
</head>
<body>

{{ subpage_hdr|safe }}


<div class="page" id="root">
  <p style="color:var(--muted);font-size:12px;padding:40px;text-align:center">Loading…</p>
</div>

<script>
const ET_ZONE = 'America/New_York';

function fmtClock(){
  const s=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:ET_ZONE,hour12:false});
  var _el=document.getElementById('clock')||document.getElementById('syn-clk');
  if(_el)_el.textContent=s+' ET';
}
setInterval(fmtClock,1000); fmtClock();

function statusCls(s){return{complete:'ps-complete',in_progress:'ps-in_progress',not_started:'ps-not_started'}[s]||'ps-not_started'}
function statusLabel(s){return{complete:'Complete',in_progress:'In Progress',not_started:'Not Started'}[s]||s}

function phaseCard(p){
  const done=p.tasks.filter(t=>t.done).length, total=p.tasks.length;
  const pct=total?Math.round(done/total*100):0;
  const active=p.status==='in_progress';
  const tasks=p.tasks.map(t=>`
    <li class="task-item">
      <span class="task-check ${t.done?'done':'pending'}">${t.done?'✓':''}</span>
      <span class="task-text ${t.done?'done':'pending'}">${t.label}</span>
    </li>`).join('');
  return `
    <div class="phase-card ${p.status}${active?' active':''}">
      <div class="phase-header">
        <span class="phase-num">P${p.id}</span>
        <span class="phase-name">${p.name}</span>
        <span class="phase-status ${statusCls(p.status)}">${statusLabel(p.status)}</span>
      </div>
      <ul class="task-list">${tasks}</ul>
      <div class="phase-prog">
        <div class="phase-prog-label">${done} / ${total} tasks · ${pct}%</div>
        <div class="phase-prog-track">
          <div class="phase-prog-fill ppf-${p.status}" style="width:${pct}%"></div>
        </div>
      </div>
    </div>`;
}

function agentRows(agents, label){
  return agents.map(a=>`
    <tr>
      <td><span class="badge ${a.status==='built'?'b-built':'b-planned'}">${a.status}</span></td>
      <td class="mono">${a.alias}</td>
      <td class="mono">${a.file}</td>
      <td style="color:var(--muted);font-size:11px">${a.job}</td>
    </tr>`).join('');
}

function secItems(items){
  return items.map(s=>`
    <div class="sec-item">
      <div class="sec-dot ${s.status}"></div>
      <div class="sec-info">
        <div class="sec-label">${s.item}</div>
        <div class="sec-meta">${s.repo} &middot; <span class="badge b-${s.status}" style="font-size:8px">${s.status}</span></div>
      </div>
    </div>`).join('');
}

function fmtAge(s){
  if(s<60) return `${Math.round(s)}s ago`;
  if(s<3600) return `${Math.round(s/60)}m ago`;
  return `${Math.round(s/3600)}h ago`;
}

async function refreshFromGitHub(){
  const btn=document.getElementById('gh-refresh-btn');
  if(btn){btn.disabled=true;btn.textContent='Refreshing…'}
  try{
    const r=await fetch('/api/project-status/refresh',{method:'POST',headers:{}});
    const d=await r.json();
    if(d.ok) await render();
    else console.warn('Refresh failed:',d.error);
  }catch(e){console.error(e)}
  finally{if(btn){btn.disabled=false;btn.textContent='↻ Refresh from GitHub'}}
}

// Pull token from cookie for XHR auth
window._token='';

async function render(){
  const d=await fetch('/api/project-status',{headers:{}}).then(r=>r.json());
  const m=d.meta;
  const phases=d.phases;
  const phasesComplete=phases.filter(p=>p.status==='complete').length;
  const phasePct=Math.round(phasesComplete/m.total_phases*100);
  const currentPhase=phases.find(p=>p.status==='in_progress')||phases[m.current_phase-1];
  const allTasks=phases.flatMap(p=>p.tasks);
  const tasksDone=allTasks.filter(t=>t.done).length;
  const agentBuilt=[...d.agents.retail_pi,...d.agents.company_pi].filter(a=>a.status==='built').length;
  const agentTotal=[...d.agents.retail_pi,...d.agents.company_pi].length;
  const secDone=d.security.filter(s=>s.status==='done').length;
  const secTotal=d.security.length;
  const blockers=d.blockers||[];

  document.getElementById('root').innerHTML = `
    <!-- HERO -->
    <div class="hero">
      <div class="hero-top">
        <div>
          <div class="hero-title">Synthos Build Progress</div>
          <div class="hero-meta">Last updated ${m.last_updated} &middot; v${m.version}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          ${m.next_milestone?`<span class="milestone-chip">Next: ${m.next_milestone}</span>`:''}
          <span class="phase-badge">Phase ${m.current_phase} of ${m.total_phases}</span>
        </div>
      </div>
      <div class="progress-label">
        <span>Phase progress</span>
        <strong>${phasesComplete} / ${m.total_phases} phases complete</strong>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${phasePct}%"></div>
      </div>
    </div>

    <!-- STATS -->
    <div class="stat-grid">
      <div class="stat-card sc-teal">
        <div class="stat-label">Phases Done</div>
        <div class="stat-val">${phasesComplete}</div>
        <div class="stat-sub">of ${m.total_phases} total</div>
      </div>
      <div class="stat-card sc-purple">
        <div class="stat-label">Tasks Done</div>
        <div class="stat-val">${tasksDone}</div>
        <div class="stat-sub">of ${allTasks.length} total</div>
      </div>
      <div class="stat-card sc-amber">
        <div class="stat-label">Agents Built</div>
        <div class="stat-val">${agentBuilt}</div>
        <div class="stat-sub">of ${agentTotal} total</div>
      </div>
      <div class="stat-card ${secDone===secTotal?'sc-teal':'sc-pink'}">
        <div class="stat-label">Security</div>
        <div class="stat-val">${secDone}</div>
        <div class="stat-sub">of ${secTotal} items done</div>
      </div>
      <div class="stat-card ${blockers.length?'sc-pink':'sc-muted'}">
        <div class="stat-label">Blockers</div>
        <div class="stat-val">${blockers.length}</div>
        <div class="stat-sub">${blockers.length?'active':'all clear'}</div>
      </div>
    </div>

    <!-- PHASES -->
    <div class="sec-title">Build Phases</div>
    <div class="phase-grid">${phases.map(phaseCard).join('')}</div>

    <!-- SECURITY -->
    <div class="sec-title">Security Checklist</div>
    <div class="sec-grid">${secItems(d.security)}</div>

    <!-- AGENTS -->
    <div class="sec-title">Agent Registry — Retail Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.retail_pi)}</tbody>
      </table>
    </div>

    <div class="sec-title">Agent Registry — Company Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.company_pi)}</tbody>
      </table>
    </div>

    <!-- BLOCKERS -->
    <div class="sec-title">Active Blockers</div>
    <div class="table-wrap">
      ${blockers.length?`<table><thead><tr><th>ID</th><th>Severity</th><th>Description</th></tr></thead><tbody>
        ${blockers.map(b=>`<tr><td class="mono">${b.id}</td><td><span class="badge b-pending">${b.severity}</span></td><td>${b.description}</td></tr>`).join('')}
      </tbody></table>`:`<div class="blocker-empty">No active blockers</div>`}
    </div>

    <div class="footer">
      <div class="footer-left">
        <div class="source-bar">
          <span class="source-chip source-${d._source||'error'}">${d._source==='github'?'GitHub live':d._source==='local'?'Local file':'Error'}</span>
          <span class="source-age">${d._cache_age_s>0?'cached '+fmtAge(d._cache_age_s):'just fetched'}</span>
        </div>
        <span class="footer-note">Auto-refreshes every 60s &middot; Push <code>data/project_status.json</code> to GitHub to update</span>
      </div>
      <div class="footer-right">
        <a href="/api/project-status" class="footer-link">Raw JSON</a>
        <button class="refresh-gh-btn" id="gh-refresh-btn" onclick="refreshFromGitHub()">↻ Refresh from GitHub</button>
      </div>
    </div>
  `;
}

render();
setInterval(render, 60000);
</script>
</body>
</html>"""





@app.route("/system-architecture")
def system_architecture_page():
    """Interactive system map — Topology, Pipeline & Gates, 24h Timeline.

    Served from templates/system_map.html (file template, not embedded string).
    Self-hosted fonts at /static/fonts/. Topology data pulled at runtime via
    /api/system-architecture (GitHub-backed). Flows, scenarios, and pipeline
    gate definitions are embedded in the template for editorial control.
    """
    if not _authorized():
        return ("<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
                "<h2>Synthos — System Architecture</h2>"
                "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> to access.</p>"
                "</body></html>"), 401
    # 2026-04-28 — pass subpage_hdr so the system map page gets the
    # standard sticky header (back-to-Monitor + hamburger menu) like
    # every other subpage. Operator caught: this page used to be a
    # nav dead-end, no way to jump to other pages without browser
    # back button.
    return render_template('system_map.html',
                           subpage_hdr=_subpage_header('System Architecture'))



def _fetch_arch_from_github():
    """Fetch system_architecture.json from GitHub API."""
    import urllib.request as _urllib_req
    import base64 as _b64
    import time as _time

    if not _GITHUB_TOKEN:
        return None
    url = (f"https://api.github.com/repos/{_GITHUB_OWNER}"
           f"/{_GITHUB_STATUS_REPO}/contents/{_GITHUB_ARCH_PATH}")
    req = _urllib_req.Request(url, headers={
        "Authorization": f"token {_GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "synthos-company-server/1.0",
    })
    try:
        with _urllib_req.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        content = _b64.b64decode(payload["content"]).decode("utf-8")
        data = json.loads(content)
        _arch_cache["data"] = data
        _arch_cache["fetched_at"] = _time.time()
        _arch_cache["source"] = "github"
        return data
    except Exception as e:
        print(f"[Company] architecture.json GitHub fetch failed: {e}")
        return None


def _get_arch_data():
    """Return architecture data from cache, GitHub, or local file."""
    import time as _time
    if _arch_cache["data"] and _arch_cache["fetched_at"]:
        if (_time.time() - _arch_cache["fetched_at"]) < _STATUS_CACHE_TTL:
            return _arch_cache
    data = _fetch_arch_from_github()
    if data:
        return _arch_cache
    # Fallback to local file
    try:
        if os.path.exists(_ARCH_JSON):
            with open(_ARCH_JSON, "r") as f:
                data = json.load(f)
            _arch_cache["data"] = data
            _arch_cache["fetched_at"] = _time.time()
            _arch_cache["source"] = "local"
            return _arch_cache
    except Exception:
        pass
    return {"data": None, "fetched_at": None, "source": "none"}


@app.route("/api/system-architecture")
def api_system_architecture():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    cache = _get_arch_data()
    if cache["data"]:
        return jsonify({**cache["data"], "_meta": {"source": cache["source"], "fetched_at": cache["fetched_at"]}})
    return jsonify({"error": "No architecture data available"}), 404


@app.route("/api/system-architecture/refresh", methods=["POST"])
def api_system_architecture_refresh():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    _arch_cache["fetched_at"] = None
    data = _fetch_arch_from_github()
    if data:
        return jsonify({"ok": True, "source": "github"})
    return jsonify({"ok": False, "error": "GitHub fetch failed"}), 502


@app.route("/project-status")
def project_status_dashboard():
    """Project build progress dashboard — requires token auth."""
    if request.args.get("token"):
        resp = redirect(url_for("project_status_dashboard"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos — Project Status</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> to access.</p>"
            "</body></html>"
        ), 401
    return render_template_string(_STATUS_HTML, subpage_hdr=_subpage_header('Project Status'))


@app.route("/api/project-status")
def api_project_status():
    """
    Return project_status.json merged with cache metadata.
    Source priority: warm cache → GitHub API → local file.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data, source, age = _get_status_data()
    if data is None:
        return jsonify({"error": "status data unavailable — check GITHUB_TOKEN or local file"}), 503
    return jsonify({**data, "_source": source, "_cache_age_s": round(age)}), 200


@app.route("/api/project-status/refresh", methods=["POST"])
def api_project_status_refresh():
    """
    Force an immediate re-fetch from GitHub, bypassing the cache.
    Returns the source used and last_updated from the freshly fetched data.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    # Bust the cache so _get_status_data() goes straight to GitHub
    _status_cache["fetched_at"] = None
    data, source, _ = _get_status_data()
    if data is None:
        return jsonify({"ok": False, "error": "fetch failed — check GITHUB_TOKEN or local file"}), 503
    return jsonify({
        "ok":          True,
        "source":      source,
        "last_updated": data.get("meta", {}).get("last_updated"),
    }), 200


# ── Sentinel Display Bridge ──────────────────────────────────────────────────
_display_bridge = None
try:
    import sentinel_bridge as _display_bridge
    _display_bridge.start_watcher()  # Start drop folder monitor
except ImportError:
    pass  # sentinel_bridge.py not present — display features disabled

DISPLAY_DROP_DIR = os.path.join(os.path.dirname(_HERE), "data", "display_uploads")
os.makedirs(DISPLAY_DROP_DIR, exist_ok=True)

# ── Sentinel Display ──────────────────────────────────────────────────────────

DISPLAY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sentinel Display — Synthos Company</title>
<style>
:root{--bg:#0e0f11;--card:#16181d;--border:#23262e;--text:#c9cdd5;--dim:#556;
--accent:#4fc3f7;--accent2:#81c784;--warn:#ffb74d;--err:#ef5350;--radius:10px;
--font:'SF Mono',ui-monospace,'Cascadia Code','Fira Code',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:0.82rem;line-height:1.6;padding:1.5rem}
a{color:var(--accent);text-decoration:none}
h1{font-size:1.1rem;font-weight:600;margin-bottom:1rem;letter-spacing:0.03em}
h2{font-size:0.88rem;font-weight:600;margin:1.5rem 0 0.5rem;letter-spacing:0.05em;text-transform:uppercase;color:var(--dim)}

.topbar{display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}
.topbar a{font-size:0.72rem;letter-spacing:0.08em;color:var(--dim)}
.topbar a:hover{color:var(--accent)}
.topbar a.active{color:var(--accent);border-bottom:1px solid var(--accent)}

.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1rem;margin-bottom:1rem}
.status-row{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
.status-pill{display:inline-flex;align-items:center;gap:0.4rem;padding:0.3rem 0.7rem;border-radius:20px;font-size:0.75rem;background:var(--bg);border:1px solid var(--border)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.green{background:#4caf50}.dot.red{background:#ef5350}.dot.yellow{background:#ffb74d}.dot.blue{background:var(--accent)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}
.scene-grid{display:flex;gap:0.5rem;flex-wrap:wrap}
.scene-btn{padding:0.4rem 0.9rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:var(--font);font-size:0.78rem;transition:all 0.2s}
.scene-btn:hover{border-color:var(--accent);color:var(--accent)}
.scene-btn.active{background:var(--accent);color:var(--bg);border-color:var(--accent);font-weight:600}

.theme-dots{display:flex;gap:0.6rem;align-items:center;margin-top:0.5rem}
.theme-dot{width:28px;height:28px;border-radius:50%;cursor:pointer;border:2px solid var(--border);transition:all 0.2s}
.theme-dot:hover{transform:scale(1.15)}.theme-dot.active{border-color:var(--accent);box-shadow:0 0 8px var(--accent)}

.slider-row{display:flex;align-items:center;gap:0.8rem;margin:0.5rem 0}
.slider-row label{min-width:60px;font-size:0.75rem;color:var(--dim)}
.slider-row input[type=range]{flex:1;accent-color:var(--accent)}
.slider-row .val{min-width:30px;text-align:right;font-size:0.78rem}

.chip{display:inline-block;padding:0.25rem 0.6rem;border-radius:4px;border:1px solid var(--border);background:var(--bg);font-size:0.75rem;cursor:pointer;margin:0.2rem}
.chip:hover{border-color:var(--accent)}.chip.active{background:var(--accent);color:var(--bg);border-color:var(--accent)}

.dropzone{border:2px dashed var(--border);border-radius:var(--radius);padding:2rem;text-align:center;color:var(--dim);transition:all 0.3s;cursor:pointer;margin:0.5rem 0}
.dropzone.dragover{border-color:var(--accent);background:rgba(79,195,247,0.05);color:var(--accent)}
.dropzone input{display:none}

.asset-table{width:100%;border-collapse:collapse;margin-top:0.5rem}
.asset-table th,.asset-table td{text-align:left;padding:0.4rem 0.6rem;border-bottom:1px solid var(--border);font-size:0.78rem}
.asset-table th{color:var(--dim);font-weight:500}

.btn{padding:0.35rem 0.8rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:var(--font);font-size:0.75rem;transition:all 0.2s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.danger{color:var(--err)}.btn.danger:hover{border-color:var(--err)}

.log-box{background:#0a0b0d;border:1px solid var(--border);border-radius:6px;padding:0.8rem;max-height:200px;overflow-y:auto;font-size:0.72rem;line-height:1.5;white-space:pre-wrap;color:#8a8}
.toast{position:fixed;bottom:1.5rem;right:1.5rem;padding:0.6rem 1.2rem;border-radius:8px;background:#1e1e1e;border:1px solid var(--border);color:var(--text);font-size:0.78rem;opacity:0;transition:opacity 0.3s;z-index:999;pointer-events:none}
.toast.show{opacity:1}
select{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:0.3rem 0.5rem;font-family:var(--font);font-size:0.78rem}
#wave-fullscreen{position:fixed;inset:0;z-index:500;background:#080b12;display:flex;flex-direction:column;align-items:center;justify-content:center;transform:rotate(0deg)}
#wave-fullscreen canvas{width:100%;height:100%;display:block}
#wave-label{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;font-family:var(--font);pointer-events:none}
#wave-exit{position:absolute;top:12px;right:12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:4px 12px;font-size:10px;color:rgba(255,255,255,0.3);cursor:pointer;font-family:var(--font);z-index:501}
#wave-exit:hover{color:rgba(255,255,255,0.7);border-color:rgba(255,255,255,0.3)}
.controls-wrap{transition:opacity 0.3s}
</style>
</head>
<body>
{{ subpage_hdr|safe }}


<div id="wave-fullscreen">
  <canvas id="sentinel-wave"></canvas>
  <div id="wave-label" style="color:rgba(0,245,212,0.5)">MONITORING</div>
  <button id="wave-exit" onclick="exitWaveMode()">EXIT WAVE</button>
  <button id="wave-sleep-btn" onclick="_wvSleepOn=!_wvSleepOn;this.textContent=_wvSleepOn?'SLEEP 9PM-9AM: ON':'SLEEP: OFF';this.style.color=_wvSleepOn?'rgba(0,245,212,0.5)':'rgba(255,75,110,0.5)'" style="position:absolute;top:12px;right:130px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:4px 12px;font-size:10px;color:rgba(0,245,212,0.5);cursor:pointer;font-family:var(--font);z-index:501">SLEEP 9PM-9AM: ON</button>
</div>

<div class="controls-wrap" id="controls-wrap" style="display:none">
<div style="margin-bottom:0.8rem"><button class="btn" onclick="enterWaveMode()" style="background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:#4fc3f7">&#x25B6; Wave Mode</button></div>
<h1>Sentinel Display Control</h1>

<div class="status-row" id="statusRow">
  <span class="status-pill"><span class="dot" id="connDot"></span><span id="connLabel">Checking…</span></span>
  <span class="status-pill"><span class="dot" id="svcDot"></span><span id="svcLabel">Service: —</span></span>
  <span class="status-pill"><span class="dot blue"></span><span id="sceneLabel">Scene: —</span></span>
  <span class="status-pill"><span class="dot blue"></span><span id="themeLabel">Theme: —</span></span>
</div>

<div class="grid">
  <!-- Scene Selector -->
  <div class="card">
    <h2>Active Scene</h2>
    <div class="scene-grid" id="sceneGrid"></div>
  </div>

  <!-- Theme Selector -->
  <div class="card">
    <h2>Theme</h2>
    <div class="theme-dots" id="themeDots"></div>
  </div>

  <!-- Brightness -->
  <div class="card">
    <h2>Brightness</h2>
    <div class="slider-row">
      <label>Day</label>
      <input type="range" min="0" max="100" id="dayBri" oninput="deBri('day',this.value)">
      <span class="val" id="dayBriVal">—</span>
    </div>
    <div class="slider-row">
      <label>Night</label>
      <input type="range" min="0" max="100" id="nightBri" oninput="deBri('night',this.value)">
      <span class="val" id="nightBriVal">—</span>
    </div>
  </div>

  <!-- Day/Night Mode -->
  <div class="card">
    <h2>Day / Night Mode</h2>
    <div id="dnChips" style="margin-top:0.4rem"></div>
  </div>

  <!-- Animations -->
  <div class="card">
    <h2>Boot Animation</h2>
    <div id="bootChips" style="margin-top:0.3rem"></div>
    <h2>Idle Animation</h2>
    <div id="idleChips" style="margin-top:0.3rem"></div>
  </div>
</div>

<!-- Upload -->
<div class="card" style="margin-top:0.5rem">
  <h2>Upload Display Assets</h2>
  <div style="display:flex;align-items:center;gap:0.8rem;margin-bottom:0.5rem">
    <label style="font-size:0.75rem;color:var(--dim)">Category:</label>
    <select id="uploadCat">
      <option value="auto">Auto-detect</option>
      <option value="boot">Boot</option>
      <option value="idle">Idle</option>
      <option value="informational">Informational</option>
      <option value="theme">Theme</option>
    </select>
  </div>
  <div class="dropzone" id="dropzone">
    <div>Drop files here or <strong>click to browse</strong></div>
    <div style="font-size:0.7rem;color:var(--dim);margin-top:0.3rem">PNG, JPG, BMP, GIF, JSON, PY — max 8 MB</div>
    <input type="file" id="fileInput" multiple accept=".png,.jpg,.jpeg,.bmp,.gif,.json,.py">
  </div>
</div>

<!-- Assets Table -->
<div class="card">
  <h2>Installed Assets</h2>
  <table class="asset-table">
    <thead><tr><th>File</th><th>Category</th><th>Size</th><th></th></tr></thead>
    <tbody id="assetBody"><tr><td colspan="4" style="color:var(--dim)">Loading…</td></tr></tbody>
  </table>
</div>

<!-- Service & Logs -->
<div class="card">
  <h2>Service</h2>
  <div style="display:flex;gap:0.5rem;margin-bottom:0.8rem">
    <button class="btn" onclick="svcAction('restart')">Restart Display</button>
    <button class="btn" onclick="loadLogs()">Refresh Logs</button>
  </div>
  <div class="log-box" id="logBox">No logs loaded.</div>
</div>

<div class="toast" id="toast"></div>

<script>
const T='"""+"""{{TOKEN}}"""+"""';
const H={headers:{'X-Token':T,'Content-Type':'application/json'}};
let _briTimer=null;

function toast(msg,err){const t=document.getElementById('toast');t.textContent=msg;t.style.borderColor=err?'var(--err)':'var(--accent)';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}

async function api(path,opts){try{const r=await fetch(path,{...opts,headers:{...opts?.headers,'X-Token':T}});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}catch(e){toast(e.message,true);throw e}}

async function loadStatus(){
  try{
    const s=await api('/api/display/status');
    document.getElementById('connDot').className='dot '+(s.display_detected?'green':'red');
    document.getElementById('connLabel').textContent=s.display_detected?'Connected':'No Display';
    document.getElementById('svcDot').className='dot '+(s.service_active?'green':'yellow');
    document.getElementById('svcLabel').textContent='Service: '+(s.service_active?'Running':'Stopped');
    document.getElementById('sceneLabel').textContent='Scene: '+(s.current_scene||'—');
    document.getElementById('themeLabel').textContent='Theme: '+(s.config?.theme?.active||'—');

    // Scenes
    const scenes=['idle','boot','trade','news','weather','alert','settings'];
    const sg=document.getElementById('sceneGrid');
    sg.innerHTML=scenes.map(sc=>`<button class="scene-btn ${sc===s.current_scene?'active':''}" onclick="setScene('${sc}')">${sc}</button>`).join('');

    // Themes
    const colors={retro:'#00ff41',amber:'#ffb300',arctic:'#4fc3f7',ghost:'#e0e0e0'};
    const td=document.getElementById('themeDots');
    const active=s.config?.theme?.active||'retro';
    td.innerHTML=Object.entries(colors).map(([n,c])=>`<div class="theme-dot ${n===active?'active':''}" style="background:${c}" title="${n}" onclick="setTheme('${n}')"></div>`).join('');

    // Brightness
    const dayB=s.config?.day_brightness??90, nightB=s.config?.night_brightness??40;
    document.getElementById('dayBri').value=dayB;document.getElementById('dayBriVal').textContent=dayB;
    document.getElementById('nightBri').value=nightB;document.getElementById('nightBriVal').textContent=nightB;

    // Day/Night
    const dnMode=s.config?.daynight_mode||'auto';
    const dnEl=document.getElementById('dnChips');
    dnEl.innerHTML=['auto','force_day','force_night'].map(m=>`<span class="chip ${m===dnMode?'active':''}" onclick="setDN('${m}')">${m.replace('_',' ')}</span>`).join('');

    // Animations
    const bootAnims=s.config?.animations?.available_boot||['retro_terminal'];
    const idleAnims=s.config?.animations?.available_idle||['retro_matrix'];
    const curBoot=s.config?.animations?.boot||'';
    const curIdle=s.config?.animations?.idle||'';
    document.getElementById('bootChips').innerHTML=bootAnims.map(a=>`<span class="chip ${a===curBoot?'active':''}" onclick="setAnim('boot','${a}')">${a}</span>`).join('');
    document.getElementById('idleChips').innerHTML=idleAnims.map(a=>`<span class="chip ${a===curIdle?'active':''}" onclick="setAnim('idle','${a}')">${a}</span>`).join('');

    // Assets
    loadAssets();
  }catch(e){
    document.getElementById('connDot').className='dot red';
    document.getElementById('connLabel').textContent='Error';
  }
}

async function loadAssets(){
  try{
    const a=await api('/api/display/assets');
    const tb=document.getElementById('assetBody');
    if(!a.assets||!a.assets.length){tb.innerHTML='<tr><td colspan="4" style="color:var(--dim)">No assets installed</td></tr>';return}
    tb.innerHTML=a.assets.map(f=>`<tr><td>${f.name}</td><td>${f.category}</td><td>${f.size_kb} KB</td><td><button class="btn danger" onclick="removeAsset('${f.name}')">Remove</button></td></tr>`).join('');
  }catch(e){}
}

async function setScene(sc){await api('/api/display/scene',{method:'POST',body:JSON.stringify({scene:sc}),...H});toast('Scene → '+sc);loadStatus()}
async function setTheme(th){await api('/api/display/theme',{method:'POST',body:JSON.stringify({theme:th}),...H});toast('Theme → '+th);loadStatus()}
async function setDN(mode){await api('/api/display/daynight',{method:'POST',body:JSON.stringify({mode}),...H});toast('Day/Night → '+mode);loadStatus()}
async function setAnim(kind,name){await api('/api/display/animation',{method:'POST',body:JSON.stringify({kind,name}),...H});toast(kind+' → '+name);loadStatus()}

function deBri(which,val){
  document.getElementById(which+'BriVal').textContent=val;
  clearTimeout(_briTimer);
  _briTimer=setTimeout(()=>{
    api('/api/display/brightness',{method:'POST',body:JSON.stringify({[which+'_brightness']:parseInt(val)}),...H});
    toast('Brightness updated');
  },400);
}

async function removeAsset(name){if(!confirm('Remove '+name+'?'))return;await api('/api/display/assets/'+encodeURIComponent(name),{method:'DELETE'});toast('Removed '+name);loadStatus()}
async function svcAction(act){await api('/api/display/restart',{method:'POST',...H});toast('Service restarting…');setTimeout(loadStatus,3000)}

async function loadLogs(){
  try{const d=await api('/api/display/logs');document.getElementById('logBox').textContent=d.logs||'No logs available.'}catch(e){document.getElementById('logBox').textContent='Failed to load logs.'}
}

// Drag-and-drop upload
const dz=document.getElementById('dropzone'), fi=document.getElementById('fileInput');
dz.addEventListener('click',()=>fi.click());
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover')});
dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('dragover');uploadFiles(e.dataTransfer.files)});
fi.addEventListener('change',()=>{uploadFiles(fi.files);fi.value=''});

async function uploadFiles(files){
  const cat=document.getElementById('uploadCat').value;
  for(const f of files){
    const fd=new FormData();fd.append('file',f);fd.append('category',cat);
    try{
      const r=await fetch('/api/display/upload',{method:'POST',headers:{'X-Token':T},body:fd});
      const j=await r.json();if(!r.ok)throw new Error(j.error||'Upload failed');
      toast('Uploaded: '+f.name);
    }catch(e){toast('Upload failed: '+e.message,true)}
  }
  loadStatus();
}

// ── WAVE MODE ──
var _wvFrame=0,_wvId=null,_wvRunning=null;
var _wvColors={teal:{r:0,g:245,b:212},purple:{r:123,g:97,b:255},amber:{r:255,g:179,b:71},pink:{r:255,g:75,b:110}};
var _wvCur={r:0,g:245,b:212};
var _wvNames={'retail_trade_logic_agent.py':'Trade Logic','retail_news_agent.py':'News','retail_market_sentiment_agent.py':'Sentiment','retail_sector_screener.py':'Screener'};

function enterWaveMode(){
  document.getElementById('wave-fullscreen').style.display='flex';
  document.getElementById('controls-wrap').style.display='none';
  if(!_wvId)_wvId=requestAnimationFrame(drawWave);
  pollAgentPulse();
}
function exitWaveMode(){
  document.getElementById('wave-fullscreen').style.display='none';
  document.getElementById('controls-wrap').style.display='';
  if(_wvId){cancelAnimationFrame(_wvId);_wvId=null;}
}

function drawWave(){
  var canvas=document.getElementById('sentinel-wave');
  if(!canvas){_wvId=requestAnimationFrame(drawWave);return;}
  var ctx=canvas.getContext('2d');
  var dpr=window.devicePixelRatio||1;
  var w=canvas.clientWidth,h=canvas.clientHeight;
  canvas.width=w*dpr;canvas.height=h*dpr;
  ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);

  var active=!!_wvRunning;
  var colorKey=active?(_wvRunning.color||'teal'):'teal';
  var tgt=_wvColors[colorKey]||_wvColors.teal;
  _wvCur.r+=((tgt.r-_wvCur.r)*0.04);
  _wvCur.g+=((tgt.g-_wvCur.g)*0.04);
  _wvCur.b+=((tgt.b-_wvCur.b)*0.04);
  var c={r:Math.round(_wvCur.r),g:Math.round(_wvCur.g),b:Math.round(_wvCur.b)};

  var ovSpeed=(active&&_wvRunning.speed)?_wvRunning.speed:1;
  var ovDir=(active&&_wvRunning.direction)?_wvRunning.direction:1;
  var t=_wvFrame*0.015*ovDir*ovSpeed;
  var heartbeat=Math.sin(t*0.4)*0.5+0.5;
  var ampOverride=(active&&_wvRunning.amplitude)?_wvRunning.amplitude:null;
  var baseAmp=ampOverride?ampOverride:(active?50:(6+heartbeat*20));
  var numWaves=active?9:5;
  var speed=active?1.0:0.2;
  var midY=h/2;
  var freqMult=(active&&_wvRunning.frequency)?_wvRunning.frequency:1.0;

  for(var i=0;i<numWaves;i++){
    var alpha=active?(0.1+(i/numWaves)*0.2):(0.04+heartbeat*0.03+(i/numWaves)*0.1);
    var amp=baseAmp*(0.4+(i/numWaves)*0.6);
    var freq=(0.006+i*0.002)*freqMult;
    var phase=t*speed+i*0.8;
    ctx.beginPath();ctx.moveTo(0,midY);
    for(var x=0;x<=w;x+=2){
      var y=midY+Math.sin(x*freq-phase)*amp+Math.sin(x*freq*1.5-phase*0.7)*amp*0.3;
      ctx.lineTo(x,y);
    }
    ctx.strokeStyle='rgba('+c.r+','+c.g+','+c.b+','+alpha+')';
    ctx.lineWidth=active?2.0:1.5;ctx.stroke();
    if(i===numWaves-1){
      ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();
      var grad=ctx.createLinearGradient(0,midY,0,h);
      grad.addColorStop(0,'rgba('+c.r+','+c.g+','+c.b+',0.05)');
      grad.addColorStop(1,'rgba('+c.r+','+c.g+','+c.b+',0)');
      ctx.fillStyle=grad;ctx.fill();
    }
  }

  // Label
  var lbl=document.getElementById('wave-label');
  if(lbl){
    if(active&&_wvRunning.agent){
      var name=_wvNames[_wvRunning.agent]||_wvRunning.agent;
      lbl.textContent=name.toUpperCase()+' \u00B7 SCANNING';
      lbl.style.color='rgba('+c.r+','+c.g+','+c.b+',0.6)';
    }else{
      lbl.textContent='MONITORING';
      lbl.style.color='rgba('+c.r+','+c.g+','+c.b+','+(0.15+heartbeat*0.1)+')';
    }
  }

  _wvFrame++;
  _wvId=requestAnimationFrame(drawWave);
}

var _wvSleepOn=true; // auto sleep 21:00-09:00 ET
function isWvSleepTime(){
  if(!_wvSleepOn)return false;
  var now=new Date();
  var et=new Date(now.toLocaleString('en-US',{timeZone:'America/New_York'}));
  var h=et.getHours();
  return h>=21||h<9;
}

async function pollAgentPulse(){
  if(document.getElementById('wave-fullscreen').style.display==='none')return;
  // Sleep schedule: dim screen 21:00-09:00 ET
  var canvas=document.getElementById('sentinel-wave');
  var lbl=document.getElementById('wave-label');
  if(isWvSleepTime()){
    if(canvas)canvas.style.opacity='0';
    if(lbl)lbl.style.opacity='0';
    setTimeout(pollAgentPulse,30000);
    return;
  }else{
    if(canvas)canvas.style.opacity='1';
    if(lbl)lbl.style.opacity='1';
  }
  try{
    var r=await fetch('/api/proxy/agent-pulse');
    if(r.ok){var d=await r.json();_wvRunning=d.running||null;}
  }catch(e){}
  setTimeout(pollAgentPulse,5000);
}

// Start in wave mode by default
enterWaveMode();

// Auto-refresh (controls mode)
loadStatus();
setInterval(loadStatus,10000);
</script>
</body>
</html>
"""

# Inject the token into DISPLAY_HTML at render time
def _render_display_html():
    token = request.args.get("token", "") or request.cookies.get("company_token", "")
    return DISPLAY_HTML.replace("{{TOKEN}}", token).replace("{{ subpage_hdr|safe }}", _subpage_header("Sentinel Display"))


@app.route("/api/proxy/agent-pulse")
def proxy_agent_pulse():
    """Proxy agent pulse from retail portal for sentinel display."""
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        r = _req.get(f"{RETAIL_PORTAL_URL}/api/agent-pulse",
                     cookies={"synthos_s": cookie}, timeout=8)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"running": None, "error": str(e)}), 502


@app.route("/display")
def display_page():
    """Render the Sentinel display control panel."""
    return _render_display_html()


@app.route("/api/display/status")
def api_display_status():
    """Full display status JSON."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    # Try sentinel_bridge first (when present), then fall back to direct HTTP
    if _display_bridge is not None:
        try:
            return jsonify(_display_bridge.get_display_status()), 200
        except Exception as e:
            return jsonify({"error": str(e), "display_detected": False}), 200
    if SENTINEL_URL:
        try:
            import urllib.request as _ur
            headers = {"Accept": "application/json"}
            if SENTINEL_TOKEN:
                headers["X-Token"] = SENTINEL_TOKEN
            req = _ur.Request(f"{SENTINEL_URL}/api/status", headers=headers)
            with _ur.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read())
            data.setdefault("display_detected", True)
            return jsonify(data), 200
        except Exception as e:
            return jsonify({"error": str(e), "display_detected": False,
                            "sentinel_url": SENTINEL_URL}), 200
    return jsonify({"error": "No sentinel bridge or SENTINEL_URL configured",
                    "display_detected": False}), 200


@app.route("/api/display/scene", methods=["POST"])
def api_display_scene():
    """Change the active scene."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    scene = data.get("scene")
    if not scene:
        return jsonify({"error": "missing 'scene'"}), 400
    try:
        _display_bridge.set_scene(scene)
        return jsonify({"ok": True, "scene": scene}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/brightness", methods=["POST"])
def api_display_brightness():
    """Adjust day/night brightness."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        cfg = _display_bridge.read_config()
        changed = False
        if "day_brightness" in data:
            cfg["day_brightness"] = int(data["day_brightness"])
            changed = True
        if "night_brightness" in data:
            cfg["night_brightness"] = int(data["night_brightness"])
            changed = True
        if changed:
            _display_bridge.write_config(cfg)
            _display_bridge.set_brightness(cfg.get("day_brightness", 90))
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/theme", methods=["POST"])
def api_display_theme():
    """Change the display theme."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    theme = data.get("theme")
    if not theme:
        return jsonify({"error": "missing 'theme'"}), 400
    try:
        _display_bridge.set_theme(theme)
        return jsonify({"ok": True, "theme": theme}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/daynight", methods=["POST"])
def api_display_daynight():
    """Set day/night mode: auto, force_day, force_night."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("auto", "force_day", "force_night"):
        return jsonify({"error": "invalid mode — use auto, force_day, or force_night"}), 400
    try:
        _display_bridge.set_daynight_mode(mode)
        return jsonify({"ok": True, "mode": mode}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/animation", methods=["POST"])
def api_display_animation():
    """Set boot or idle animation."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    name = data.get("name")
    if kind not in ("boot", "idle") or not name:
        return jsonify({"error": "need 'kind' (boot|idle) and 'name'"}), 400
    try:
        if kind == "boot":
            _display_bridge.set_boot_animation(name)
        else:
            _display_bridge.set_idle_animation(name)
        return jsonify({"ok": True, "kind": kind, "name": name}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/assets")
def api_display_assets():
    """List installed display assets."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        assets = _display_bridge.list_assets()
        return jsonify({"assets": assets}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


DISPLAY_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".json", ".py"}

@app.route("/api/display/upload", methods=["POST"])
def api_display_upload():
    """Upload a display asset (drag-and-drop or form)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    safe = secure_filename(f.filename)
    ext = os.path.splitext(safe)[1].lower()
    if ext not in DISPLAY_ALLOWED_EXT:
        return jsonify({"error": f"file type {ext} not allowed"}), 400
    category = request.form.get("category", "auto")
    # Save to drop dir — watcher will pick it up, or install directly
    dest = os.path.join(DISPLAY_DROP_DIR, safe)
    f.save(dest)
    try:
        result = _display_bridge.install_asset(dest, category=category if category != "auto" else None)
        return jsonify({"ok": True, "installed": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/assets/<filename>", methods=["DELETE"])
def api_display_remove_asset(filename):
    """Remove an installed display asset."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    safe = secure_filename(filename)
    try:
        _display_bridge.remove_asset(safe)
        return jsonify({"ok": True, "removed": safe}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/restart", methods=["POST"])
def api_display_restart():
    """Restart the Sentinel display service."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        _display_bridge.restart_display_service()
        return jsonify({"ok": True, "message": "restart requested"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/logs")
def api_display_logs():
    """Get recent display log output."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    lines = int(request.args.get("lines", 50))
    try:
        logs = _display_bridge.get_display_logs(lines=lines)
        return jsonify({"logs": logs}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Auditor Findings ──────────────────────────────────────────────────────────
_AUDITOR_DB_PATH = os.getenv('AUDITOR_DB_PATH', '/home/pi/synthos-company/data/auditor.db')

@app.route("/api/auditor/findings")
def api_auditor_findings():
    """
    Return live auditor findings from auditor.db.
    Called by the monitor dashboard — no auth required (internal network only).
    """
    try:
        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row

        issues = conn.execute(
            "SELECT id, first_seen, last_seen, source_file, severity, pattern, "
            "       context, hit_count "
            "FROM detected_issues WHERE resolved = 0 "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "         WHEN 'medium' THEN 2 ELSE 3 END, hit_count DESC LIMIT 200"
        ).fetchall()

        by_sev: dict = {}
        for row in issues:
            by_sev[row['severity']] = by_sev.get(row['severity'], 0) + 1

        scan_state = conn.execute(
            "SELECT log_file, last_offset, file_size, last_scanned "
            "FROM scan_state ORDER BY last_scanned DESC"
        ).fetchall()

        report_row = conn.execute(
            "SELECT report FROM morning_reports ORDER BY date DESC LIMIT 1"
        ).fetchone()

        conn.close()

        return jsonify({
            'issues':          [dict(r) for r in issues],
            'by_severity':     by_sev,
            'total_unresolved': len(issues),
            'scan_state':      [dict(r) for r in scan_state],
            'morning_report':  json.loads(report_row['report']) if report_row else None,
        })
    except FileNotFoundError:
        return jsonify({
            'issues': [], 'by_severity': {}, 'total_unresolved': 0,
            'scan_state': [], 'morning_report': None,
            'error': 'Auditor DB not found — auditor may not have run yet',
        })
    except Exception as e:
        return jsonify({
            'issues': [], 'by_severity': {}, 'total_unresolved': 0,
            'scan_state': [], 'morning_report': None,
            'error': str(e),
        })

@app.route("/api/auditor/resolve", methods=["POST"])
def api_auditor_resolve():
    """Mark auditor issues as resolved. Accepts {ids: [1,2,3]} or {all: true} or {pattern: 'STALE_ACTIVITY'}."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    try:
        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=10)
        if data.get('all'):
            conn.execute("UPDATE detected_issues SET resolved = 1")
            msg = "all issues resolved"
        elif data.get('ids'):
            ids = data['ids']
            conn.execute(f"UPDATE detected_issues SET resolved = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
            msg = f"{len(ids)} issues resolved"
        elif data.get('pattern'):
            conn.execute("UPDATE detected_issues SET resolved = 1 WHERE pattern = ?", (data['pattern'],))
            msg = f"pattern '{data['pattern']}' resolved"
        elif data.get('source'):
            conn.execute("UPDATE detected_issues SET resolved = 1 WHERE source_file LIKE ?", ('%' + data['source'] + '%',))
            msg = f"source '{data['source']}' resolved"
        else:
            conn.close()
            return jsonify({"ok": False, "error": "Provide ids, all, pattern, or source"}), 400
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM detected_issues WHERE resolved = 0").fetchone()[0]
        conn.close()
        return jsonify({"ok": True, "message": msg, "remaining": remaining})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




# ── Retention ─────────────────────────────────────────────────────────────────
_PI_EVENTS_RETAIN_DAYS = int(os.getenv("PI_EVENTS_RETAIN_DAYS", "30"))

def trim_pi_events():
    """
    Delete pi_events rows older than PI_EVENTS_RETAIN_DAYS (default 30 days).
    Run on startup to prevent unbounded table growth.
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=_PI_EVENTS_RETAIN_DAYS)).isoformat()
    try:
        with _db_conn() as conn:
            result  = conn.execute(
                "DELETE FROM pi_events WHERE recorded_at < ?", (cutoff_iso,)
            )
            deleted = result.rowcount
        if deleted:
            print(f"[Company] Trimmed {deleted} pi_events rows older than {_PI_EVENTS_RETAIN_DAYS} days")
    except Exception as e:
        print(f"[Company] Warning: pi_events trim failed: {e}")



# ─────────────────────────────────────────────────────────────────────────────
# MONITOR SETTINGS PAGE
# ─────────────────────────────────────────────────────────────────────────────
import re as _re

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'company.env')

def _read_env():
    """Read company.env and return a dict of current values (strips quotes)."""
    vals = {}
    try:
        for line in open(_ENV_PATH).read().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return vals

def _write_env_key(key, value):
    """
    Write or update a single key in company.env.
    If the key exists (even commented), updates it in-place.
    Otherwise appends it.
    """
    try:
        content = open(_ENV_PATH).read()
    except FileNotFoundError:
        content = ''

    pattern = rf'^({_re.escape(key)}\s*=).*$'
    replacement = rf'\g<1>{value}'
    new_content, n = _re.subn(pattern, replacement, content, flags=_re.MULTILINE)
    if n == 0:
        # Key not found — append
        if new_content and not new_content.endswith('\n'):
            new_content += '\n'
        new_content += f'{key}={value}\n'
    with open(_ENV_PATH, 'w') as f:
        f.write(new_content)


_SETTINGS_ALLOWED_KEYS = {
    'ANTHROPIC_API_KEY', 'RESEND_API_KEY', 'ALERT_FROM',
    'COMPANY_URL', 'SECRET_TOKEN', 'LIVE_TRADING_ENABLED',
    'R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY',
    'R2_BUCKET_NAME', 'BACKUP_ENCRYPTION_KEY',
}

SETTINGS_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monitor Settings · Synthos</title>
<style>
  :root{--bg:#0b0f17;--card:#131929;--border:#1e2d42;--text:#c8d6e5;--muted:#4a6280;--teal:#00f5d4;--pink:#ff4b6e;--amber:#f5a623;--green:#00c896}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'SF Pro Display',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
  .header{background:var(--card);border-bottom:1px solid var(--border);padding:0 2rem;display:flex;align-items:center;justify-content:space-between;height:52px;position:sticky;top:0;z-index:100}
  .header-left{display:flex;align-items:center;gap:12px}
  .logo{font-size:1rem;font-weight:700;letter-spacing:0.06em;color:var(--teal)}
  .header-badge{font-size:0.62rem;letter-spacing:0.1em;text-transform:uppercase;background:#1e2d42;color:var(--muted);padding:2px 7px;border-radius:4px}
  .nav-back{font-size:0.72rem;letter-spacing:0.06em;color:var(--muted);text-decoration:none}
  .nav-back:hover{color:var(--teal)}
  .page{max-width:760px;margin:0 auto;padding:2.5rem 1.5rem}
  h1{font-size:1.3rem;font-weight:700;letter-spacing:0.04em;margin-bottom:2rem;color:var(--text)}
  .section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:1.5rem;margin-bottom:1.5rem}
  .section-title{font-size:0.7rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:1.25rem;padding-bottom:0.6rem;border-bottom:1px solid var(--border)}
  .field-row{display:grid;grid-template-columns:180px 1fr auto;align-items:center;gap:10px;margin-bottom:10px}
  .field-label{font-size:0.75rem;font-weight:600;color:var(--text)}
  .field-hint{font-size:0.65rem;color:var(--muted);margin-top:2px}
  .field-current{font-size:0.68rem;color:var(--muted);font-family:monospace;margin-top:3px;letter-spacing:0.03em}
  input.s-input{width:100%;background:#0b0f17;border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.8rem;padding:7px 10px;outline:none;transition:border 0.15s}
  input.s-input:focus{border-color:var(--teal)}
  .btn-save{background:var(--teal);color:#0b0f17;border:none;border-radius:6px;font-size:0.75rem;font-weight:700;padding:7px 14px;cursor:pointer;white-space:nowrap;transition:opacity 0.15s}
  .btn-save:hover{opacity:0.85}
  .btn-danger{background:transparent;color:var(--pink);border:1px solid var(--pink);border-radius:6px;font-size:0.75rem;font-weight:700;padding:7px 14px;cursor:pointer;transition:all 0.15s}
  .btn-danger:hover{background:var(--pink);color:#fff}
  .gate-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 0}
  .gate-label{flex:1}
  .gate-title{font-size:0.85rem;font-weight:600}
  .gate-desc{font-size:0.72rem;color:var(--muted);margin-top:3px}
  .toggle-wrap{display:flex;align-items:center;gap:10px}
  .toggle{position:relative;display:inline-block;width:44px;height:24px}
  .toggle input{opacity:0;width:0;height:0}
  .slider{position:absolute;inset:0;background:#1e2d42;border-radius:12px;cursor:pointer;transition:background 0.2s}
  .slider:before{content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:transform 0.2s}
  input:checked + .slider{background:var(--teal)}
  input:checked + .slider:before{transform:translateX(20px)}
  .toggle-state{font-size:0.75rem;font-weight:700;min-width:40px}
  .on{color:var(--teal)} .off{color:var(--muted)}
  .push-result{font-size:0.72rem;color:var(--muted);margin-top:6px;min-height:16px}
  .toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:10px 18px;border-radius:8px;font-size:0.8rem;opacity:0;pointer-events:none;transition:opacity 0.2s;z-index:9999}
  .toast.show{opacity:1}
  .divider{border:none;border-top:1px solid var(--border);margin:12px 0}
  @media(max-width:600px){.field-row{grid-template-columns:1fr;}.btn-save{width:100%;}}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <div class="logo">SYNTHOS</div>
    <div class="header-badge">Monitor Settings</div>
  </div>
  <a href="/monitor" class="nav-back">&#8592; Monitor</a>
</div>

<div class="page">
  <h1>Monitor Settings</h1>

  <!-- LIVE TRADING GATE -->
  <div class="section">
    <div class="section-title">Live Trading Gate</div>
    <div class="gate-row">
      <div class="gate-label">
        <div class="gate-title">Enable Live Trading</div>
        <div class="gate-desc">When ON, the Live option becomes available in all customer portals. Toggle OFF at any time to lock everyone to Paper mode. New users always start on Paper.</div>
      </div>
      <div class="toggle-wrap">
        <span class="toggle-state" id="gate-state">—</span>
        <label class="toggle">
          <input type="checkbox" id="live-gate-toggle" onchange="handleGateToggle()">
          <span class="slider"></span>
        </label>
      </div>
    </div>
    <div class="push-result" id="gate-result"></div>
  </div>

  <!-- OPERATOR API KEYS -->
  <div class="section">
    <div class="section-title">Operator API Keys</div>
    <p style="font-size:0.72rem;color:var(--muted);margin-bottom:1rem">Changes are written to company.env and take effect on next restart. Keys marked ● have a current value.</p>

    <div class="field-row">
      <div>
        <div class="field-label">Anthropic API Key</div>
        <div class="field-current" id="cur-ANTHROPIC_API_KEY">—</div>
      </div>
      <input class="s-input" id="val-ANTHROPIC_API_KEY" type="password" placeholder="sk-ant-…" autocomplete="off">
      <button class="btn-save" onclick="saveKey('ANTHROPIC_API_KEY')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">Resend API Key</div>
        <div class="field-current" id="cur-RESEND_API_KEY">—</div>
      </div>
      <input class="s-input" id="val-RESEND_API_KEY" type="password" placeholder="re_…" autocomplete="off">
      <button class="btn-save" onclick="saveKey('RESEND_API_KEY')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">Alert From</div>
        <div class="field-current" id="cur-ALERT_FROM">—</div>
      </div>
      <input class="s-input" id="val-ALERT_FROM" type="email" placeholder="alerts@synth-cloud.com">
      <button class="btn-save" onclick="saveKey('ALERT_FROM')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">Company URL</div>
        <div class="field-current" id="cur-COMPANY_URL">—</div>
      </div>
      <input class="s-input" id="val-COMPANY_URL" type="url" placeholder="https://…">
      <button class="btn-save" onclick="saveKey('COMPANY_URL')">Update</button>
    </div>

    <hr class="divider">

    <div class="field-row">
      <div>
        <div class="field-label">Monitor Token</div>
        <div class="field-current" id="cur-SECRET_TOKEN">—</div>
      </div>
      <input class="s-input" id="val-SECRET_TOKEN" type="password" placeholder="used by retail portals to authenticate" autocomplete="off">
      <button class="btn-save" onclick="saveKey('SECRET_TOKEN')">Update</button>
    </div>

    <hr class="divider">
    <p style="font-size:0.72rem;color:var(--muted);margin-bottom:1rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase">Cloudflare R2 Backup</p>

    <div class="field-row">
      <div>
        <div class="field-label">R2 Account ID</div>
        <div class="field-current" id="cur-R2_ACCOUNT_ID">—</div>
      </div>
      <input class="s-input" id="val-R2_ACCOUNT_ID" placeholder="Cloudflare account ID" autocomplete="off">
      <button class="btn-save" onclick="saveKey('R2_ACCOUNT_ID')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">R2 Access Key ID</div>
        <div class="field-current" id="cur-R2_ACCESS_KEY_ID">—</div>
      </div>
      <input class="s-input" id="val-R2_ACCESS_KEY_ID" type="password" placeholder="R2 API access key" autocomplete="off">
      <button class="btn-save" onclick="saveKey('R2_ACCESS_KEY_ID')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">R2 Secret Access Key</div>
        <div class="field-current" id="cur-R2_SECRET_ACCESS_KEY">—</div>
      </div>
      <input class="s-input" id="val-R2_SECRET_ACCESS_KEY" type="password" placeholder="R2 API secret" autocomplete="off">
      <button class="btn-save" onclick="saveKey('R2_SECRET_ACCESS_KEY')">Update</button>
    </div>

    <div class="field-row">
      <div>
        <div class="field-label">R2 Bucket Name</div>
        <div class="field-current" id="cur-R2_BUCKET_NAME">—</div>
      </div>
      <input class="s-input" id="val-R2_BUCKET_NAME" placeholder="synthos-backups" autocomplete="off">
      <button class="btn-save" onclick="saveKey('R2_BUCKET_NAME')">Update</button>
    </div>

    <hr class="divider">
    <p style="font-size:0.72rem;color:var(--muted);margin-bottom:1rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase">Backup Encryption</p>

    <div class="field-row">
      <div>
        <div class="field-label">Backup Encryption Key</div>
        <div class="field-current" id="cur-BACKUP_ENCRYPTION_KEY">—</div>
      </div>
      <input class="s-input" id="val-BACKUP_ENCRYPTION_KEY" type="password" placeholder="Fernet key (Base64)" autocomplete="off">
      <button class="btn-save" onclick="saveKey('BACKUP_ENCRYPTION_KEY')">Update</button>
    </div>
    <p style="font-size:0.65rem;color:rgba(255,75,110,0.6);margin-top:4px">Store this key safely outside the system. Without it, R2 backups cannot be decrypted.</p>

  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const TOKEN = '{{ secret_token }}';

function toast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = type === 'ok' ? 'var(--teal)' : type === 'err' ? 'var(--pink)' : 'var(--border)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function obfuscate(v) {
  if (!v) return '—';
  if (v.length <= 8) return '●●●●●●●●';
  return v.slice(0,4) + '●●●●●●●●' + v.slice(-4);
}

async function loadCurrentValues() {
  try {
    const r = await fetch('/api/monitor-settings/current', {headers:{}});
    if (!r.ok) return;
    const d = await r.json();
    ['ANTHROPIC_API_KEY','RESEND_API_KEY','ALERT_FROM','COMPANY_URL','SECRET_TOKEN','R2_ACCOUNT_ID','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET_NAME','BACKUP_ENCRYPTION_KEY'].forEach(k => {
      const el = document.getElementById('cur-' + k);
      if (el) el.textContent = d[k] ? obfuscate(d[k]) : '— not set';
    });
    // Gate
    const live = d['LIVE_TRADING_ENABLED'] === 'true';
    document.getElementById('live-gate-toggle').checked = live;
    const gs = document.getElementById('gate-state');
    gs.textContent = live ? 'ON' : 'OFF';
    gs.className = 'toggle-state ' + (live ? 'on' : 'off');
  } catch(e) { console.warn('Could not load current settings', e); }
}

async function saveKey(key) {
  const val = document.getElementById('val-' + key)?.value?.trim();
  if (!val) { toast('Enter a value first', 'err'); return; }
  try {
    const r = await fetch('/api/monitor-settings', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[key]: val})
    });
    const d = await r.json();
    if (d.ok) {
      toast('✓ ' + key + ' updated (restart to apply)', 'ok');
      document.getElementById('val-' + key).value = '';
      loadCurrentValues();
    } else {
      toast('Error: ' + (d.error||'unknown'), 'err');
    }
  } catch(e) { toast('Save failed', 'err'); }
}

async function handleGateToggle() {
  const enabled = document.getElementById('live-gate-toggle').checked;
  const result  = document.getElementById('gate-result');
  const gs      = document.getElementById('gate-state');
  result.textContent = 'Saving & pushing to portals…';
  gs.textContent = enabled ? 'ON' : 'OFF';
  gs.className = 'toggle-state ' + (enabled ? 'on' : 'off');
  try {
    const r = await fetch('/api/monitor-settings', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({'LIVE_TRADING_ENABLED': enabled ? 'true' : 'false', '_push_to_portals': true})
    });
    const d = await r.json();
    if (d.ok) {
      const pushed = d.pushed || [];
      const failed = d.push_failed || [];
      let msg = '✓ Gate ' + (enabled ? 'opened' : 'locked') + '.';
      if (pushed.length) msg += ' Pushed to: ' + pushed.join(', ');
      if (failed.length) msg += '  Could not reach: ' + failed.join(', ');
      result.style.color = failed.length ? 'var(--amber)' : 'var(--teal)';
      result.textContent = msg;
      toast(msg, failed.length ? 'warn' : 'ok');
    } else {
      result.style.color = 'var(--pink)';
      result.textContent = '✗ ' + (d.error||'unknown');
    }
  } catch(e) {
    result.style.color = 'var(--pink)';
    result.textContent = '✗ Save failed';
  }
}

loadCurrentValues();
</script>
</body>
</html>"""


@app.route("/settings")
def monitor_settings():
    if not _authorized():
        return redirect(url_for("login"))
    return render_template_string(SETTINGS_PAGE_HTML, secret_token=SECRET_TOKEN)


@app.route("/api/monitor-settings/current", methods=["GET"])
def api_monitor_settings_current():
    """Return current values from company.env (requires session or token)."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    vals = _read_env()
    safe = {}
    for k in _SETTINGS_ALLOWED_KEYS:
        safe[k] = vals.get(k, "")
    return jsonify(safe)


@app.route("/api/monitor-settings", methods=["POST"])
def api_monitor_settings():
    """
    Write one or more operator keys to company.env.
    If _push_to_portals is True and LIVE_TRADING_ENABLED is in the payload,
    also push LIVE_TRADING_ENABLED to all registered retail portals.
    """
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    push_to_portals = data.pop("_push_to_portals", False)

    # Write allowed keys to company.env
    written = []
    for k, v in data.items():
        if k not in _SETTINGS_ALLOWED_KEYS:
            continue
        _write_env_key(k, str(v))
        written.append(k)

    if not written:
        return jsonify({"ok": False, "error": "No allowed keys in request"}), 400

    pushed = []
    push_failed = []

    if push_to_portals and "LIVE_TRADING_ENABLED" in written:
        live_val = data.get("LIVE_TRADING_ENABLED", "false")
        import requests as _req
        with registry_lock:
            pis = list(pi_registry.values())
        for pi in pis:
            pi_ip = pi.get("pi_ip")
            if not pi_ip:
                continue
            portal_url = f"http://{pi_ip}:5001/api/keys"
            try:
                r = _req.post(
                    portal_url,
                    json={"LIVE_TRADING_ENABLED": live_val},
                    headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
                    timeout=5,
                )
                if r.ok and r.json().get("ok"):
                    pushed.append(pi.get("label") or pi.get("pi_id") or pi_ip)
                else:
                    push_failed.append(pi.get("label") or pi_ip)
            except Exception:
                push_failed.append(pi.get("label") or pi_ip)

    return jsonify({"ok": True, "written": written, "pushed": pushed, "push_failed": push_failed})


# ── APPROVALS PAGE ────────────────────────────────────────────────────────────

@app.route("/approvals")
def approvals_page():
    """Standalone account approval queue page."""
    if not _authorized():
        return redirect(url_for("login"))
    return _subpage_header('Approvals') + _APPROVALS_BODY


_APPROVALS_BODY = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}
.appr-page{max-width:1000px;margin:0 auto;padding:20px 24px}
.appr-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.appr-title::after{content:'';flex:1;height:1px;background:rgba(255,255,255,0.07)}
.appr-filters{display:flex;gap:6px;margin-bottom:16px}
.af{padding:5px 14px;border-radius:8px;font-size:11px;font-weight:600;background:transparent;border:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.4);cursor:pointer;font-family:inherit;transition:all .15s;letter-spacing:0.03em}
.af:hover{background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.88)}
.af.active{background:rgba(245,166,35,0.08);border-color:rgba(245,166,35,0.25);color:#f5a623}
.appr-card{background:rgba(17,21,32,0.8);border:1px solid rgba(255,255,255,0.07);border-radius:14px;overflow:hidden;margin-bottom:10px}
.appr-card-hdr{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.05)}
.appr-avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);display:flex;align-items:center;justify-content:center;flex-shrink:0}
.appr-avatar svg{width:18px;height:18px;color:rgba(255,255,255,0.3)}
.appr-name{font-size:14px;font-weight:600}
.appr-email{font-size:12px;color:rgba(255,255,255,0.4);font-family:'JetBrains Mono',monospace}
.appr-meta{display:flex;gap:16px;padding:10px 18px;font-size:11px;color:rgba(255,255,255,0.4)}
.appr-meta span{display:flex;align-items:center;gap:4px}
.appr-actions{display:flex;gap:8px;padding:10px 18px 14px}
.appr-btn{padding:6px 18px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s;letter-spacing:0.03em}
.appr-approve{background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:#00f5d4}
.appr-approve:hover{background:rgba(0,245,212,0.15)}
.appr-reject{background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);color:#ff4b6e}
.appr-reject:hover{background:rgba(255,75,110,0.15)}
.appr-status{padding:2px 10px;border-radius:99px;font-size:10px;font-weight:700;margin-left:auto}
.appr-empty{text-align:center;padding:40px;color:rgba(255,255,255,0.3);font-size:13px}
.toast{position:fixed;bottom:24px;right:24px;padding:10px 20px;border-radius:10px;font-size:12px;font-weight:600;opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
.toast.show{opacity:1}
.toast.ok{background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.2);color:#00f5d4}
.toast.err{background:rgba(255,75,110,0.1);border:1px solid rgba(255,75,110,0.2);color:#ff4b6e}
</style>

<div id="toast" class="toast"></div>
<div class="appr-page">
  <div class="appr-title">Account Approval Queue</div>
  <div class="appr-filters">
    <button class="af active" id="af-PENDING" onclick="filterAppr('PENDING')">Pending</button>
    <button class="af" id="af-APPROVED" onclick="filterAppr('APPROVED')">Approved</button>
    <button class="af" id="af-REJECTED" onclick="filterAppr('REJECTED')">Rejected</button>
    <button class="af" id="af-ALL" onclick="filterAppr(null)">All</button>
  </div>
  <div id="appr-list"><div class="appr-empty">Loading...</div></div>

  <div class="appr-title" style="margin-top:28px">Invite Codes</div>
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
    <button class="af" onclick="generateInvite()" style="background:rgba(0,245,212,0.06);border-color:rgba(0,245,212,0.15);color:#00f5d4">Generate New Code</button>
  </div>
  <div id="gen-box" style="display:none;margin-bottom:14px;padding:14px 18px;background:rgba(0,245,212,0.04);border:1px solid rgba(0,245,212,0.15);border-radius:10px">
    <div style="font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:rgba(255,255,255,0.35);margin-bottom:6px">Generated Invite Code</div>
    <div style="display:flex;align-items:center;gap:12px">
      <span id="gen-code" style="font-family:monospace;font-size:18px;font-weight:700;color:#00f5d4;letter-spacing:0.08em"></span>
      <button id="copy-btn" style="padding:4px 12px;border-radius:6px;font-size:10px;font-weight:600;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:rgba(255,255,255,0.5);cursor:pointer" onclick="copyCode()">Copy</button>
    </div>
    <div style="font-size:10px;color:rgba(255,255,255,0.25);margin-top:6px">Share this code with the user to include on their signup form</div>
  </div>
  <div id="invite-list"></div>
</div>

<script>
const TOKEN = '';
let _filter = 'PENDING';

function toast(msg,type){
  const el=document.getElementById('toast');
  el.textContent=msg;el.className='toast show '+type;
  setTimeout(()=>el.classList.remove('show'),2800);
}

async function loadApprovals(){
  try{
    let url='/api/proxy/pending-signups';
    if(_filter) url+='?status='+_filter;
    const r=await fetch(url,{headers:{}});
    const d=await r.json();
    const list=document.getElementById('appr-list');
    if(!d.signups||!d.signups.length){
      list.innerHTML='<div class="appr-empty">No '+(_filter?_filter.toLowerCase()+' ':'')+'signups found</div>';
      return;
    }
    list.innerHTML=d.signups.map(s=>{
      const ts=s.created_at?new Date(s.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}):'\u2014';
      const sc=s.status==='PENDING'?'#f5a623':s.status==='APPROVED'?'#00f5d4':'#ff4b6e';
      const sbg=s.status==='PENDING'?'rgba(245,166,35,0.08)':s.status==='APPROVED'?'rgba(0,245,212,0.08)':'rgba(255,75,110,0.08)';
      let actions='';
      if(s.status==='PENDING'){
        actions='<button class="appr-btn appr-approve" onclick="approve('+s.id+')">Approve</button>'
               +'<button class="appr-btn appr-reject" onclick="reject('+s.id+')">Reject</button>';
      }
      const custId=s.customer_id?'<span>ID: '+s.customer_id.slice(0,8)+'...</span>':'';
      return '<div class="appr-card">'
        +'<div class="appr-card-hdr">'
        +'<div class="appr-avatar"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg></div>'
        +'<div><div class="appr-name">'+s.name+'</div><div class="appr-email">'+s.email+(s.email_verified?'<span style="margin-left:6px;font-size:9px;font-weight:700;color:#00f5d4;background:rgba(0,245,212,0.1);padding:1px 6px;border-radius:99px">&check; Verified</span>':'<span style="margin-left:6px;font-size:9px;font-weight:700;color:#ff4b6e;background:rgba(255,75,110,0.1);padding:1px 6px;border-radius:99px">&cross; Unverified</span>')+'</div></div>'
        +'<span class="appr-status" style="background:'+sbg+';color:'+sc+'">'+s.status+'</span>'
        +'</div>'
        +'<div class="appr-meta"><span>\u260E '+s.phone+'</span><span>\u23F0 '+ts+'</span>'+custId+'</div>'
        +(actions?'<div class="appr-actions">'+actions+'</div>':'')
        +'</div>';
    }).join('');
  }catch(e){console.error(e)}
}

function filterAppr(s){
  _filter=s;
  document.querySelectorAll('.af').forEach(b=>b.classList.remove('active'));
  document.getElementById(s?'af-'+s:'af-ALL').classList.add('active');
  loadApprovals();
}
var allSignups=[];
async function approve(id){
  var _s=allSignups.find(function(x){return x.id===id});
  if(_s && !_s.email_verified){
    if(!confirm('WARNING: Email NOT verified. Approve anyway?'))return;
  } else {
    if(!confirm('Approve this signup? Creates customer account and database.'))return;
  }
  const r=await fetch('/api/proxy/approve-signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signup_id:id})});
  const d=await r.json();
  if(d.ok){toast('Account approved: '+(d.email||''),'ok');loadApprovals()}
  else toast('Error: '+(d.error||'Unknown'),'err');
}

async function reject(id){
  if(!confirm('Reject this signup request?'))return;
  const r=await fetch('/api/proxy/reject-signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signup_id:id})});
  const d=await r.json();
  if(d.ok){toast('Signup rejected','ok');loadApprovals()}
  else toast('Error: '+(d.error||'Unknown'),'err');
}

async function generateInvite(){
  const r=await fetch('/api/proxy/generate-invite',{method:'POST',headers:{'Content-Type':'application/json'}});
  const d=await r.json();
  if(d.ok){
    document.getElementById('gen-code').textContent=d.code;
    document.getElementById('copy-btn').style.display='inline';
    toast('Invite code generated: '+d.code,'ok');
    loadInvites();
  } else toast('Error: '+(d.error||'Unknown'),'err');
}

function copyCode(){
  const code=document.getElementById('gen-code').textContent;
  if(code) navigator.clipboard.writeText(code).then(function(){toast('Copied to clipboard','ok')});
}

var _inviteNotes={};

async function loadInviteNotes(){
  try{
    const r=await fetch('/api/invite-notes');
    const d=await r.json();
    if(d.ok) _inviteNotes=d.notes||{};
  }catch(e){}
}

async function loadInvites(){
  try{
    await loadInviteNotes();
    const r=await fetch('/api/proxy/invite-codes');
    const d=await r.json();
    const el=document.getElementById('invite-list');
    if(!el) return;
    if(!d.codes||!d.codes.length){el.innerHTML='<div style="text-align:center;padding:20px;color:rgba(255,255,255,0.3)">No invite codes yet</div>';return}
    el.innerHTML=d.codes.map(function(c){
      var ts=c.created_at?new Date(c.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'\u2014';
      var used=c.is_used;
      var sc=used?'rgba(255,255,255,0.3)':'#00f5d4';
      var bg=used?'rgba(255,255,255,0.02)':'rgba(0,245,212,0.04)';
      var stTxt=used?'Used':'Available';
      var stClr=used?'rgba(255,75,110,0.7)':'rgba(0,245,212,0.7)';
      var n=_inviteNotes[c.code]||{};
      var noteTxt=n.note||n.recipient_name||'';
      var sentBadge=n.sent_at?'<span style="font-size:9px;color:rgba(0,245,212,0.6);background:rgba(0,245,212,0.08);padding:1px 6px;border-radius:99px;margin-left:4px">&check; Sent'+(n.recipient_email?' to '+n.recipient_email:'')+'</span>':'';
      return '<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border:1px solid rgba(255,255,255,0.05);border-radius:10px;margin-bottom:6px;background:'+bg+';flex-wrap:wrap">'
        +'<span style="font-family:monospace;font-size:13px;font-weight:700;color:'+sc+';letter-spacing:0.04em;min-width:120px">'+c.code+'</span>'
        +'<span style="font-size:10px;color:rgba(255,255,255,0.3);min-width:90px">'+ts+'</span>'
        +'<span style="font-size:10px;color:'+stClr+';min-width:55px">'+stTxt+'</span>'
        +sentBadge
        +'<button onclick="promptSendEmail(&#39;'+c.code+'&#39;)" style="padding:3px 10px;border-radius:6px;font-size:10px;font-weight:600;background:rgba(138,92,246,0.08);border:1px solid rgba(138,92,246,0.2);color:#8a5cf6;cursor:pointer;font-family:inherit;white-space:nowrap">Send To Email</button>'
        +'<input id="note-'+c.code+'" value="'+noteTxt.replace(/"/g,'&quot;')+'" placeholder="Recipient note\u2026" onblur="saveInviteNote(&#39;'+c.code+'&#39;)" style="flex:1;min-width:140px;padding:4px 10px;border-radius:6px;font-size:11px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:rgba(255,255,255,0.8);font-family:inherit;outline:none">'
        +'<span id="note-status-'+c.code+'" style="font-size:9px;font-weight:600;min-width:50px;opacity:0;transition:opacity .3s"></span>'
        +'</div>';
    }).join('');
  }catch(e){console.error(e)}
}

function promptSendEmail(code){
  var n=_inviteNotes[code]||{};
  var defEmail=n.recipient_email||'';
  var email=prompt('Send invite code '+code+' to email address:',defEmail);
  if(!email) return;
  email=email.trim();
  if(!email||!email.includes('@')){toast('Invalid email address','err');return}
  var name=document.getElementById('note-'+code)?.value||'';
  sendInviteEmail(code,email,name);
}

async function sendInviteEmail(code,email,recipientName){
  try{
    toast('Sending\u2026','ok');
    const r=await fetch('/api/invite-send-email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,email:email,recipient_name:recipientName})});
    const d=await r.json();
    if(d.ok){toast('Invite sent to '+email,'ok');loadInvites()}
    else toast('Error: '+(d.error||'Send failed'),'err');
  }catch(e){toast('Network error','err')}
}

async function saveInviteNote(code){
  var el=document.getElementById('note-'+code);
  if(!el) return;
  var note=el.value.trim();
  var prev=(_inviteNotes[code]||{}).note||(_inviteNotes[code]||{}).recipient_name||'';
  if(note===prev) return;
  var st=document.getElementById('note-status-'+code);
  try{
    const r=await fetch('/api/invite-note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,note:note})});
    const d=await r.json();
    if(d.ok){
      if(!_inviteNotes[code]) _inviteNotes[code]={};
      _inviteNotes[code].note=note;
      if(st){st.textContent='Saved \u2713';st.style.color='rgba(0,245,212,0.7)';st.style.opacity='1';setTimeout(function(){st.style.opacity='0'},2500)}
    } else {
      if(st){st.textContent='Error';st.style.color='rgba(255,75,110,0.7)';st.style.opacity='1';setTimeout(function(){st.style.opacity='0'},3000)}
    }
  }catch(e){
    if(st){st.textContent='Error';st.style.color='rgba(255,75,110,0.7)';st.style.opacity='1';setTimeout(function(){st.style.opacity='0'},3000)}
  }
}

loadApprovals();
loadInvites();
</script>
"""

@app.route("/monitor")
def monitor_dashboard():
    import datetime as _dt
    from flask import make_response
    resp = make_response(render_template_string(DASHBOARD, secret_token=SECRET_TOKEN, build_ts=_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route("/health")
def health():
    """
    Unauthenticated health check for the monitor node itself.
    Returns a compact status snapshot used by retail_patch.py --check-nodes
    and any external uptime monitor.
    """
    with registry_lock:
        pi_count = len(pi_registry)
        pis      = []
        for pi_id, data in pi_registry.items():
            age_s = int((now_utc() - data["last_seen"]).total_seconds())
            pis.append({
                "pi_id":   pi_id,
                "label":   data.get("label", pi_id),
                "status":  pi_status(data),
                "age_secs": age_s,
            })
    return jsonify({
        "status":   "ok",
        "pi_count": pi_count,
        "pis":      pis,
    }), 200


_TODO_PATH = os.path.join(os.path.dirname(_HERE), 'TODO.md')


@app.route("/api/todos", methods=["GET"])
def api_todos():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    """Parse TODO.md from the repo and return unresolved items as JSON."""
    import re as _re
    try:
        with open(_TODO_PATH, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return jsonify([])

    todos = []
    section = 'Pending'
    item_re = _re.compile(r'^\s*-\s+\[([ xX])\]\s+(?:\[([^\]]+)\]\s+)?(.+)')

    for line in lines:
        heading = _re.match(r'^#+\s+(.+)', line.strip())
        if heading:
            section = heading.group(1).strip()
            continue
        m = item_re.match(line)
        if not m:
            continue
        checked  = m.group(1).lower() == 'x'
        category = (m.group(2) or section).strip()
        rest     = m.group(3).strip()
        if ' — ' in rest:
            title, action = rest.split(' — ', 1)
        else:
            title, action = rest, ''
        todos.append({
            'id':       str(len(todos)),
            'title':    title.strip(),
            'category': category,
            'action':   action.strip(),
            'section':  section,
            'resolved': checked,
            'date':     '',
            'pi_id':    '',
        })

    section_order = {'Pending': 0, 'In Progress': 1}
    unresolved = [t for t in todos if not t['resolved']]
    unresolved.sort(key=lambda t: section_order.get(t['section'], 99))
    return jsonify(unresolved)


@app.route("/api/auditor")
def api_auditor():
    """Return auditor findings by reading auditor.db directly."""
    return api_auditor_findings()


@app.route("/api/behavior-baseline")
def api_behavior_baseline_proxy():
    """Proxy to the retail node's /api/behavior-baseline endpoint.

    Phase 7L+ (2026-04-26) — the trader-behavior baseline counter moved
    off the customer dashboard (it doesn't help end users) and lives on
    the command portal instead. Same proxy pattern as /api/audit/<pi_id>:
    cmd portal forwards the SECRET_TOKEN-bearing request to pi5 over
    the local network.

    pi5's IP is read from the heartbeat registry (no IP guessing).
    """
    # Find the retail Pi from the registry. The pi_id is set by the
    # heartbeat poster (retail_heartbeat.py) — current value is
    # 'synthos-pi-retail' but match liberally on 'retail' in either
    # pi_id or label so a future rename doesn't break this lookup.
    retail_pi = None
    with registry_lock:
        for pid, p in pi_registry.items():
            if 'retail' in str(p.get("pi_id", "")).lower() \
               or 'retail' in str(p.get("label", "")).lower():
                retail_pi = p
                break
    if not retail_pi:
        return jsonify({"error": "Retail Pi not found in registry — waiting for heartbeat"}), 503
    # Heartbeat stores requester IP under key 'ip' (set from
    # request.remote_addr at heartbeat time). Older code paths
    # used 'pi_ip' — check both for compatibility.
    pi_ip = retail_pi.get("ip") or retail_pi.get("pi_ip")
    if not pi_ip:
        return jsonify({"error": "Retail Pi IP unknown — waiting for heartbeat"}), 503
    try:
        import requests as _req
        r = _req.get(
            f"http://{pi_ip}:5001/api/behavior-baseline",
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
            timeout=5,
        )
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({"error": f"Retail returned {r.status_code}"}), 503
    except Exception as e:
        return jsonify({"error": f"Could not reach retail at {pi_ip}:5001 — {e}"}), 503


@app.route("/api/audit/<pi_id>")
def api_audit_for_pi(pi_id):
    """
    Fetch log-scan audit data from a retail Pi portal.
    Uses pi_ip stored in registry from heartbeat — no IP guessing.
    """
    with registry_lock:
        pi = pi_registry.get(pi_id)
    if not pi:
        return jsonify({"error": "Pi not found"}), 404
    pi_ip = pi.get("pi_ip")
    if not pi_ip:
        return jsonify({"error": "Pi IP unknown — waiting for heartbeat", "pi_id": pi_id}), 503
    try:
        import requests as _req
        r = _req.get(
            f"http://{pi_ip}:5001/api/logs-audit",
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
            timeout=10,
        )
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({"error": f"Portal returned {r.status_code}", "pi_id": pi_id}), 503
    except Exception as e:
        return jsonify({"error": f"Could not reach {pi_ip}:5001 — {e}", "pi_id": pi_id}), 503


@app.route("/api/backlog/<pi_id>")
def api_backlog_for_pi(pi_id):
    """Fetch improvement backlog from a Pi's portal."""
    with registry_lock:
        pi = pi_registry.get(pi_id)
    if not pi:
        return jsonify({"error": "Pi not found"}), 404
    try:
        import requests as _req
        portal_url = f"http://{pi_id.replace('synthos-','').replace('-','.')}:5001/api/improvement-backlog"
        r = _req.get(portal_url, timeout=5)
        if r.status_code == 200:
            return jsonify(r.json())
    except Exception:
        pass
    return jsonify({"tasks": [], "error": "Could not reach Pi portal"}), 200




# ── Maintenance Alert Page ────────────────────────────────────────────────────

MAINTENANCE_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Maintenance</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--pink:#ff4b6e;--purple:#7b61ff;--amber:#ffb347;--signal:#f5a623;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{position:sticky;top:0;z-index:100;background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
        border-bottom:1px solid var(--border);padding:0 24px;height:56px;
        display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal)}
.nav-back{color:var(--muted);font-size:11px;text-decoration:none;padding:5px 12px;
          border-radius:8px;border:1px solid var(--border);margin-left:auto;transition:all 0.15s}
.nav-back:hover{color:var(--text);border-color:var(--border2)}

.page{max-width:720px;margin:0 auto;padding:32px 24px}
.title{font-size:20px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,var(--amber),var(--signal));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:11px;color:var(--muted);margin-bottom:28px;font-family:var(--mono)}

.card{background:var(--surface);border:1px solid var(--border2);border-radius:14px;padding:24px;margin-bottom:20px}
.card-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:14px}

.form-row{display:flex;gap:14px;margin-bottom:16px;flex-wrap:wrap}
.form-group{display:flex;flex-direction:column;gap:5px;flex:1;min-width:140px}
.form-group label{font-size:10px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted)}
.form-group select,.form-group input,.form-group textarea{
  background:var(--surface2);border:1px solid var(--border2);border-radius:8px;
  padding:9px 12px;color:var(--text);font-family:var(--sans);font-size:13px;
  outline:none;transition:border-color .15s;width:100%}
.form-group select:focus,.form-group input:focus,.form-group textarea:focus{border-color:var(--amber)}
.form-group textarea{min-height:90px;resize:vertical;font-family:var(--mono);font-size:12px;line-height:1.6}
.tz-label{font-size:11px;color:var(--dim);align-self:flex-end;padding-bottom:10px}

.preview{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:8px}
.preview-cat{display:inline-block;font-size:8px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 8px;border-radius:99px;background:rgba(123,97,255,0.1);color:var(--purple);margin-bottom:8px}
.preview-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px}
.preview-time{font-size:10px;color:var(--dim);font-family:var(--mono);margin-bottom:10px}
.preview-body{font-size:12px;color:var(--muted);line-height:1.7;white-space:pre-wrap}

.btn-send{
  display:block;width:100%;padding:13px;margin-top:20px;border:none;border-radius:10px;
  background:linear-gradient(135deg,var(--amber),var(--signal));color:#000;
  font-size:13px;font-weight:700;letter-spacing:0.04em;cursor:pointer;
  transition:opacity .15s,transform .1s;font-family:var(--sans)}
.btn-send:hover{opacity:0.9}
.btn-send:active{transform:scale(0.98)}
.btn-send:disabled{opacity:0.4;cursor:not-allowed;transform:none}

.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  padding:10px 22px;border-radius:10px;font-size:12px;font-weight:600;
  z-index:999;opacity:0;transition:opacity .3s;pointer-events:none;font-family:var(--sans)}
.toast.show{opacity:1}
.toast.ok{background:var(--teal);color:#000}
.toast.err{background:var(--pink);color:#fff}

.km-table{margin-bottom:10px}
.km-loading{color:var(--dim);font-size:11px;padding:12px 0}
.km-row{display:grid;grid-template-columns:1fr 160px 90px 70px;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.km-row:last-child{border-bottom:none}
.km-name{font-family:var(--mono);font-size:10px;font-weight:600;color:var(--text);letter-spacing:0.02em}
.km-val{font-family:var(--mono);font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.km-exp{font-size:10px;font-weight:600;text-align:center}
.km-exp.green{color:var(--teal)}.km-exp.amber{color:var(--amber)}.km-exp.red{color:var(--pink)}
.km-exp.blink{animation:blinker 1s linear infinite}
@keyframes blinker{50%{opacity:0.3}}
.km-noexp{color:var(--dim);font-size:10px;text-align:center}
.km-actions{display:flex;gap:4px;justify-content:flex-end}
.km-btn{font-size:9px;padding:3px 8px;border-radius:5px;border:1px solid var(--border2);background:var(--surface2);color:var(--muted);cursor:pointer;font-family:var(--sans);transition:all .15s}
.km-btn:hover{color:var(--text);border-color:var(--amber)}
.km-btn.rotate{border-color:var(--purple);color:var(--purple)}
.km-btn.rotate:hover{background:rgba(123,97,255,0.1)}
.km-edit-panel{padding:10px 0;display:grid;gap:8px}
.km-edit-panel input{background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:6px 10px;color:var(--text);font-family:var(--mono);font-size:11px;width:100%;outline:none}
.km-edit-panel input:focus{border-color:var(--amber)}
.km-edit-row{display:flex;gap:8px;align-items:center}
.km-edit-label{font-size:9px;color:var(--muted);min-width:60px;text-transform:uppercase;font-weight:600;letter-spacing:0.04em}
.km-save-row{display:flex;gap:8px;justify-content:flex-end;margin-top:4px}
.km-save{font-size:10px;padding:5px 14px;border-radius:6px;border:none;background:var(--teal);color:#000;font-weight:700;cursor:pointer;font-family:var(--sans)}
.km-cancel{font-size:10px;padding:5px 14px;border-radius:6px;border:1px solid var(--border2);background:transparent;color:var(--muted);cursor:pointer;font-family:var(--sans)}
.km-add-btn{font-size:10px;padding:5px 12px;border-radius:6px;border:1px dashed var(--border2);background:transparent;color:var(--dim);cursor:pointer;width:100%;margin-top:4px;font-family:var(--sans);transition:all .15s}
.km-add-btn:hover{border-color:var(--amber);color:var(--amber)}
</style>
</head>
<body>

{{ subpage_hdr|safe }}

<div class="page">
  <div class="title"><span>Schedule Maintenance</span></div>
  <div class="subtitle">Broadcast a maintenance alert to all customer accounts</div>

  <div class="card">
    <div class="card-label">Maintenance Window</div>

    <div class="form-row">
      <div class="form-group" style="flex:2">
        <label>Type</label>
        <select id="mtype" onchange="updatePreview()">
          <option value="Scheduled Maintenance">Scheduled Maintenance</option>
          <option value="Emergency Maintenance">Emergency Maintenance</option>
          <option value="System Update">System Update</option>
        </select>
      </div>
      <div class="form-group">
        <label>Duration</label>
        <select id="mduration" onchange="updatePreview()">
          <option value="~15 minutes">~15 minutes</option>
          <option value="~30 minutes">~30 minutes</option>
          <option value="~1 hour">~1 hour</option>
          <option value="~2 hours">~2 hours</option>
        </select>
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Date</label>
        <input type="date" id="mdate" onchange="updatePreview()">
      </div>
      <div class="form-group">
        <label>Time</label>
        <input type="time" id="mtime" value="03:55" onchange="updatePreview()">
      </div>
      <span class="tz-label">ET</span>
    </div>

    <div class="form-group">
      <label>Message</label>
      <textarea id="mbody" oninput="updatePreview()"></textarea>
    </div>
  </div>

  <div class="card">
    <div class="card-label">Notification Preview</div>
    <div class="preview">
      <div class="preview-cat">system</div>
      <div class="preview-title" id="prev-title">Scheduled Maintenance</div>
      <div class="preview-time" id="prev-time">—</div>
      <div class="preview-body" id="prev-body"></div>
    </div>
  </div>

  <button class="btn-send" id="btn-send" onclick="sendMaintenance()">Broadcast to All Customers</button>

  <div style="height:1px;background:var(--border);margin:32px 0"></div>

  <div class="title" style="margin-top:8px"><span>Key Management</span></div>
  <div class="subtitle">API keys across all nodes &mdash; update, track expiration, rotate</div>

  <div id="km-pi4b" class="card"><div class="card-label">pi4b &middot; Company Server</div><div class="km-table" id="km-table-pi4b"><div class="km-loading">Loading&hellip;</div></div><button class="km-add-btn" onclick="kmAddRow('pi4b')">+ Add Key</button></div>
  <div id="km-pi5" class="card"><div class="card-label">pi5 &middot; Retail Node</div><div class="km-table" id="km-table-pi5"><div class="km-loading">Loading&hellip;</div></div><button class="km-add-btn" onclick="kmAddRow('pi5')">+ Add Key</button></div>
  <div id="km-pi2w" class="card"><div class="card-label">pi2w &middot; Monitor Node</div><div class="km-table" id="km-table-pi2w"><div class="km-loading">Loading&hellip;</div></div></div>

</div>

<div class="toast" id="toast"></div>

<script>
const TOKEN = '{{ secret_token }}';

function defaultDate() {
  var d = new Date();
  var day = d.getDay();
  var diff = (6 - day + 7) % 7;
  if (diff === 0) diff = 7;
  d.setDate(d.getDate() + diff);
  return d.toISOString().split('T')[0];
}

document.getElementById('mdate').value = defaultDate();

function buildMessage() {
  var type = document.getElementById('mtype').value;
  var date = document.getElementById('mdate').value;
  var time = document.getElementById('mtime').value;
  var dur  = document.getElementById('mduration').value;

  var dateStr = '\u2014';
  if (date) {
    var parts = date.split('-');
    var dt = new Date(parts[0], parts[1]-1, parts[2]);
    var days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    dateStr = days[dt.getDay()] + ', ' + months[dt.getMonth()] + ' ' + dt.getDate();
  }

  var timeStr = time || '03:55';
  var h = parseInt(timeStr.split(':')[0]);
  var m = timeStr.split(':')[1];
  var ampm = h >= 12 ? 'PM' : 'AM';
  var h12 = h % 12 || 12;
  var timeFmt = h12 + ':' + m + ' ' + ampm;

  return type + ' is scheduled for ' + dateStr + ' at ' + timeFmt + ' ET.\\n'
       + 'All trading will be paused during this window.\\n'
       + 'Expected duration: ' + dur + '.';
}

function updatePreview() {
  var type = document.getElementById('mtype').value;
  var body = document.getElementById('mbody');
  if (!body._userEdited) {
    body.value = buildMessage();
  }
  document.getElementById('prev-title').textContent = type;
  document.getElementById('prev-body').textContent = body.value;
  document.getElementById('prev-time').textContent = 'just now';
}

document.getElementById('mbody').addEventListener('input', function() {
  this._userEdited = true;
});
document.getElementById('mbody').addEventListener('change', function() {
  if (!this.value.trim()) { this._userEdited = false; updatePreview(); }
});

function showToast(msg, ok) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(function() { t.className = 'toast'; }, 4000);
}

async function sendMaintenance() {
  var btn = document.getElementById('btn-send');
  btn.disabled = true;
  btn.textContent = 'Sending...';

  var type = document.getElementById('mtype').value;
  var body = document.getElementById('mbody').value;
  var date = document.getElementById('mdate').value;
  var time = document.getElementById('mtime').value;

  if (!body.trim()) {
    showToast('Message is required', false);
    btn.disabled = false;
    btn.textContent = 'Broadcast to All Customers';
    return;
  }

  try {
    var r = await fetch('/api/maintenance/notify', {
      method: 'POST',
      headers: {'X-Token': TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify({
        title: type,
        body: body,
        scheduled_at: date + 'T' + (time || '03:55')
      })
    });
    var d = await r.json();
    if (d.ok) {
      showToast('Sent to ' + d.sent + ' customer' + (d.sent === 1 ? '' : 's'), true);
    } else {
      showToast(d.error || 'Failed to send', false);
    }
  } catch(e) {
    showToast('Network error: ' + e.message, false);
  }

  btn.disabled = false;
  btn.textContent = 'Broadcast to All Customers';
}


// ── KEY MANAGEMENT ──
var kmData = {};

async function kmLoadNode(node) {
  try {
    var r = await fetch('/api/node-keys/' + node);
    if (!r.ok) return;
    var d = await r.json();
    kmData[node] = d.keys || [];
    kmRender(node);
  } catch(e) {
    document.getElementById('km-table-' + node).innerHTML = '<div class="km-loading" style="color:var(--pink)">Failed to load</div>';
  }
}

function kmRender(node) {
  var el = document.getElementById('km-table-' + node);
  var keys = kmData[node] || [];
  if (!keys.length) { el.innerHTML = '<div class="km-loading">No keys configured</div>'; return; }
  el.innerHTML = keys.map(function(k) {
    var expHtml = '';
    if (k.expires_at) {
      var days = k.countdown_days;
      var cls = days > 30 ? 'green' : days > 7 ? 'amber' : days > 0 ? 'red' : 'red blink';
      var label = days > 0 ? days + 'd' : (days === 0 ? 'TODAY' : Math.abs(days) + 'd ago');
      expHtml = '<div class="km-exp ' + cls + '">' + label + '</div>';
    } else {
      expHtml = '<div class="km-noexp">&mdash;</div>';
    }
    var btns = '<button class="km-btn" data-node="' + node + '" data-key="' + k.key_name + '" onclick="kmEdit(this.dataset.node,this.dataset.key)">Edit</button>';
    if (k.has_backup) btns += '<button class="km-btn rotate" data-node="' + node + '" data-key="' + k.key_name + '" onclick="kmRotate(this.dataset.node,this.dataset.key)">Rotate</button>';
    return '<div class="km-row">'
      + '<div class="km-name">' + k.key_name + '</div>'
      + '<div class="km-val">' + (k.has_value ? k.value_obfuscated : '<span style="color:var(--pink)">NOT SET</span>') + '</div>'
      + expHtml + '<div class="km-actions">' + btns + '</div>'
      + '</div>'
      + '<div id="km-edit-' + node + '-' + k.key_name + '"></div>';
  }).join('');
}

function kmEdit(node, keyName) {
  var el = document.getElementById('km-edit-' + node + '-' + keyName);
  if (el.innerHTML) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="km-edit-panel">'
    + '<div class="km-edit-row"><span class="km-edit-label">New Key</span><input type="password" id="km-val-' + node + '-' + keyName + '" placeholder="Paste new key value"></div>'
    + '<div class="km-edit-row"><span class="km-edit-label">Expires</span><input type="date" id="km-exp-' + node + '-' + keyName + '"></div>'
    + '<div class="km-edit-row"><span class="km-edit-label">Backup</span><input type="password" id="km-bak-' + node + '-' + keyName + '" placeholder="Optional backup key for rotation"></div>'
    + '<div class="km-save-row"><button class="km-cancel" data-target="km-edit-' + node + '-' + keyName + '" onclick="kmCancel(this.dataset.target)">Cancel</button>'
    + '<button class="km-save" data-node="' + node + '" data-key="' + keyName + '" onclick="kmSave(this.dataset.node,this.dataset.key)">Save</button></div>'
    + '</div>';
}

async function kmSave(node, keyName) {
  var val = document.getElementById('km-val-' + node + '-' + keyName).value;
  var exp = document.getElementById('km-exp-' + node + '-' + keyName).value;
  var bak = document.getElementById('km-bak-' + node + '-' + keyName).value;
  if (!val && !exp && !bak) { showToast('Nothing to save', false); return; }
  try {
    var body = {key_name: keyName};
    if (val) body.value = val;
    if (exp) body.expires_at = exp;
    if (bak) body.backup_value = bak;
    var r = await fetch('/api/node-keys/' + node, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var d = await r.json();
    if (d.ok) {
      showToast(keyName + ' updated on ' + node, true);
      kmLoadNode(node);
    } else {
      showToast(d.error || 'Failed', false);
    }
  } catch(e) { showToast('Error: ' + e.message, false); }
}

async function kmRotate(node, keyName) {
  if (!confirm('Rotate ' + keyName + ' on ' + node + '? Primary and backup will swap.')) return;
  try {
    var r = await fetch('/api/node-keys/' + node + '/rotate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key_name: keyName})
    });
    var d = await r.json();
    if (d.ok) {
      showToast(keyName + ' rotated on ' + node, true);
      kmLoadNode(node);
    } else { showToast(d.error || 'Rotate failed', false); }
  } catch(e) { showToast('Error: ' + e.message, false); }
}

function kmCancel(id) { document.getElementById(id).innerHTML = ""; }

function kmAddRow(node) {
  var name = prompt('Enter the env var name (e.g. NEW_API_KEY):');
  if (!name) return;
  name = name.trim().toUpperCase();
  if (!kmData[node]) kmData[node] = [];
  if (!kmData[node].find(function(k) { return k.key_name === name; })) {
    kmData[node].push({key_name: name, value_obfuscated: '', has_value: false, expires_at: null, countdown_days: null, has_backup: false, notes: ''});
    kmRender(node);
  }
  kmEdit(node, name);
}

// Load all nodes on page init
kmLoadNode('pi4b');
kmLoadNode('pi5');
kmLoadNode('pi2w');

updatePreview();
</script>
</body>
</html>
"""


@app.route("/maintenance")
def maintenance_page():
    if not _authorized():
        return redirect(url_for("login"))
    return render_template_string(MAINTENANCE_PAGE_HTML, secret_token=SECRET_TOKEN, subpage_hdr=_subpage_header('Maintenance'))


@app.route("/api/maintenance/notify", methods=["POST"])
def api_maintenance_notify():
    """Broadcast a maintenance notification to all retail portal customers."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    if not RETAIL_PORTAL_URL:
        return jsonify({"error": "RETAIL_PORTAL_URL not configured on monitor"}), 500

    data  = request.get_json(force=True)
    title = data.get("title", "Scheduled Maintenance")
    body  = data.get("body", "")
    sched = data.get("scheduled_at", "")

    if not body.strip():
        return jsonify({"error": "body is required"}), 400

    payload = {
        "category": "system",
        "title":    title,
        "body":     body,
        "meta":     {"source": "monitor", "type": "maintenance", "scheduled_at": sched},
    }

    import requests as _req
    try:
        session_cookie = _get_admin_session_cookie()
    except Exception as e:
        return jsonify({"error": f"Portal auth failed: {str(e)[:200]}"}), 502

    try:
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/notifications/broadcast",
            headers={"Content-Type": "application/json"},
            cookies={"synthos_s": session_cookie},
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            resp = r.json()
            sent = resp.get("sent", 0)
            print(f"[Synthos Monitor] Maintenance broadcast -> {sent} customers: {title}")
            return jsonify({"ok": True, "sent": sent})
        try:
            err = r.json().get("error", r.text[:200])
        except Exception:
            err = r.text[:200]
        return jsonify({"error": f"Portal returned {r.status_code}: {err}"}), 502
    except _req.Timeout:
        return jsonify({"error": "Portal request timed out"}), 504
    except Exception as e:
        return jsonify({"error": f"Portal unreachable: {str(e)[:200]}"}), 502




# ── NODE POWER MANAGEMENT ─────────────────────────────────────────────────────

# SSH alias map: pi_id → (ssh_host, user)
_NODE_SSH_MAP = {
    "pi4b-company":      ("localhost", "pi"),
    "synthos-pi-retail":  ("10.0.0.11", "pi516gb"),
    "pi2w-monitor":       ("10.0.0.12", "pi-02w"),
}

@app.route("/api/node/<pi_id>/power", methods=["POST"])
def api_node_power(pi_id):
    """Reboot or shutdown a node via SSH."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    action = data.get("action", "")
    if action not in ("reboot", "shutdown"):
        return jsonify({"error": "action must be reboot or shutdown"}), 400

    # Look up SSH target
    ssh_info = _NODE_SSH_MAP.get(pi_id)
    if not ssh_info:
        # Try to find from registry
        with registry_lock:
            pi = pi_registry.get(pi_id)
        if pi and pi.get("pi_ip"):
            ssh_info = (pi["pi_ip"], "pi")
        else:
            return jsonify({"error": f"Unknown node: {pi_id}"}), 404

    host, user = ssh_info
    cmd = "sudo reboot" if action == "reboot" else "sudo poweroff"

    import subprocess
    try:
        if host == "localhost":
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=10
            )
        else:
            result = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 f"{user}@{host}", cmd],
                capture_output=True, text=True, timeout=15
            )
        print(f"[Synthos Monitor] Power {action} sent to {pi_id} ({host})")
        return jsonify({"ok": True, "action": action, "pi_id": pi_id})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": True, "action": action, "pi_id": pi_id,
                        "note": "Command sent (timeout expected during shutdown)"})
    except Exception as e:
        return jsonify({"error": f"SSH failed: {str(e)[:200]}"}), 502


# ── KEY MANAGEMENT API ────────────────────────────────────────────────────────

_KEY_FILTER = {
    'pi4b': {'ANTHROPIC_API_KEY','RESEND_API_KEY','GITHUB_TOKEN','SECRET_TOKEN','SSO_SECRET','PORTAL_TOKEN'},
    'pi5':  {'ANTHROPIC_API_KEY','ALPACA_API_KEY','ALPACA_SECRET_KEY','RESEND_API_KEY','GITHUB_TOKEN',
             'STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','STRIPE_PRICE_ID','STRIPE_EARLY_ADOPTER_PRICE_ID',
             'PORTAL_SECRET_KEY','ENCRYPTION_KEY',
             # 2026-05-01 — FMP API key slot. Consumed by retail_sector_screener
             # for ETF holdings refresh once the user signs up. Free tier doesn't
             # include holdings; Ultimate ($149/mo) does. Slot is here so the
             # row shows up as NOT SET on the maintenance page until filled.
             'FMP_API_KEY'},
    'pi2w': {'SECRET_TOKEN'},
}

def _obfuscate(val):
    if not val or len(val) < 8:
        return val or ''
    return val[:4] + '\u2022' * min(len(val)-8, 12) + val[-4:]


def _read_pi5_keys():
    """Read keys from pi5 via portal API using admin session."""
    try:
        import requests as _req
        cookie = _get_admin_session_cookie()
        r = _req.get(f"{RETAIL_PORTAL_URL}/api/get-keys",
                     cookies={"synthos_s": cookie}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[Monitor] pi5 key read failed: {e}")
    return {}


def _read_pi2w_keys():
    """Read keys from pi2w via SSH."""
    import subprocess
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "pi-02w@10.0.0.12", "cat /home/pi-02w/synthos/.env"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            vals = {}
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                vals[k.strip()] = v.strip().strip('"').strip("'")
            return vals
    except Exception as e:
        print(f"[Monitor] pi2w key read failed: {e}")
    return {}


def _get_key_metadata():
    """Get all expiration metadata from DB."""
    try:
        with _support_conn() as conn:
            rows = conn.execute(
                "SELECT node, key_name, expires_at, backup_value, notes, updated_at "
                "FROM api_key_metadata"
            ).fetchall()
            return {(r['node'], r['key_name']): dict(r) for r in rows}
    except Exception:
        return {}


@app.route("/api/node-keys/<node>", methods=["GET"])
def api_node_keys(node):
    """Fetch API keys for a node with obfuscated values and expiration metadata.
    No auth required — values are obfuscated. Page-level auth gates access to /maintenance."""

    allowed = _KEY_FILTER.get(node)
    if allowed is None:
        return jsonify({"error": f"Unknown node: {node}"}), 404

    # Read raw keys from node
    if node == 'pi4b':
        raw = _read_env()
    elif node == 'pi5':
        raw = _read_pi5_keys()
    elif node == 'pi2w':
        raw = _read_pi2w_keys()
    else:
        raw = {}

    metadata = _get_key_metadata()
    from datetime import datetime as _dt, timezone as _tz

    keys = []
    for kname in sorted(allowed):
        val = raw.get(kname, '')
        meta = metadata.get((node, kname), {})
        exp = meta.get('expires_at')
        countdown = None
        if exp:
            try:
                exp_date = _dt.fromisoformat(exp)
                diff = (exp_date - _dt.now(_tz.utc).replace(tzinfo=None)).days
                countdown = diff
            except Exception:
                pass
        keys.append({
            'key_name': kname,
            'value_obfuscated': _obfuscate(val),
            'has_value': bool(val),
            'expires_at': exp,
            'countdown_days': countdown,
            'has_backup': bool(meta.get('backup_value')),
            'notes': meta.get('notes', ''),
        })

    return jsonify({"node": node, "keys": keys})


@app.route("/api/node-keys/<node>", methods=["POST"])
def api_node_keys_update(node):
    """Write a key to a node's .env and update metadata."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    key_name = data.get('key_name', '')
    value = data.get('value', '')
    expires_at = data.get('expires_at')
    backup_value = data.get('backup_value')
    notes = data.get('notes')

    if node not in _KEY_FILTER:
        return jsonify({"error": f"Unknown node: {node}"}), 404

    # Write key value to the node's .env
    write_ok = False
    if value:
        if node == 'pi4b':
            _write_env_key(key_name, value)
            write_ok = True
        elif node == 'pi5':
            try:
                import requests as _req
                cookie = _get_admin_session_cookie()
                r = _req.post(f"{RETAIL_PORTAL_URL}/api/keys",
                              json={key_name: value},
                              cookies={"synthos_s": cookie}, timeout=10)
                write_ok = r.ok
            except Exception as e:
                return jsonify({"error": f"pi5 write failed: {e}"}), 502
        elif node == 'pi2w':
            import subprocess
            try:
                # Use sed to update in-place on remote
                cmd = f"sed -i 's|^{key_name}=.*|{key_name}={value}|' /home/pi-02w/synthos/.env"
                r = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "pi-02w@10.0.0.12", cmd],
                    capture_output=True, text=True, timeout=10)
                write_ok = (r.returncode == 0)
            except Exception as e:
                return jsonify({"error": f"pi2w write failed: {e}"}), 502

    # Update metadata in DB
    from datetime import datetime as _dt, timezone as _tz
    now_iso = _dt.now(_tz.utc).isoformat()
    try:
        with _support_conn() as conn:
            conn.execute("""
                INSERT INTO api_key_metadata (node, key_name, expires_at, backup_value, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node, key_name) DO UPDATE SET
                    expires_at=COALESCE(excluded.expires_at, expires_at),
                    backup_value=COALESCE(excluded.backup_value, backup_value),
                    notes=COALESCE(excluded.notes, notes),
                    updated_at=excluded.updated_at
            """, (node, key_name, expires_at, backup_value, notes, now_iso))
    except Exception as e:
        return jsonify({"error": f"Metadata save failed: {e}"}), 500

    return jsonify({"ok": True, "written": write_ok, "key_name": key_name, "node": node})


@app.route("/api/node-keys/<node>/rotate", methods=["POST"])
def api_node_keys_rotate(node):
    """Swap primary key with backup."""
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    key_name = data.get('key_name', '')

    # Get current value
    if node == 'pi4b':
        raw = _read_env()
    elif node == 'pi5':
        raw = _read_pi5_keys()
    elif node == 'pi2w':
        raw = _read_pi2w_keys()
    else:
        return jsonify({"error": f"Unknown node: {node}"}), 404

    current_val = raw.get(key_name, '')

    # Get backup from metadata
    try:
        with _support_conn() as conn:
            row = conn.execute(
                "SELECT backup_value FROM api_key_metadata WHERE node=? AND key_name=?",
                (node, key_name)).fetchone()
    except Exception:
        row = None

    if not row or not row['backup_value']:
        return jsonify({"error": "No backup key to rotate"}), 400

    backup_val = row['backup_value']

    # Write backup as new primary
    if node == 'pi4b':
        _write_env_key(key_name, backup_val)
    elif node == 'pi5':
        try:
            import requests as _req
            cookie = _get_admin_session_cookie()
            _req.post(f"{RETAIL_PORTAL_URL}/api/keys",
                      json={key_name: backup_val},
                      cookies={"synthos_s": cookie}, timeout=10)
        except Exception as e:
            return jsonify({"error": f"Rotate write failed: {e}"}), 502
    elif node == 'pi2w':
        import subprocess
        cmd = f"sed -i 's|^{key_name}=.*|{key_name}={backup_val}|' /home/pi-02w/synthos/.env"
        subprocess.run(["ssh", "-o", "ConnectTimeout=5", "pi-02w@10.0.0.12", cmd],
                       capture_output=True, text=True, timeout=10)

    # Move old primary to backup
    from datetime import datetime as _dt, timezone as _tz
    with _support_conn() as conn:
        conn.execute(
            "UPDATE api_key_metadata SET backup_value=?, updated_at=? WHERE node=? AND key_name=?",
            (current_val, _dt.now(_tz.utc).isoformat(), node, key_name))

    print(f"[Monitor] Rotated {key_name} on {node}")
    return jsonify({"ok": True, "key_name": key_name, "node": node})






@app.route("/api/proxy/billing/all-customers")
def proxy_billing_all():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        r = _req.get(f"{RETAIL_PORTAL_URL}/api/billing/all-customers",
                     cookies={'synthos_s': cookie}, timeout=15)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/customer/<customer_id>/trading-mode", methods=["POST"])
def proxy_customer_trading_mode(customer_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    data = request.get_json(silent=True) or {}
    try:
        cookie = _get_admin_session_cookie()
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/admin/customers/{customer_id}/trading-mode",
            json=data, cookies={'synthos_s': cookie}, timeout=15
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/company-expenses", methods=["GET"])
def api_company_expenses():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _support_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM company_expenses ORDER BY date DESC LIMIT 100"
            ).fetchall()
            return jsonify({"expenses": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"expenses": [], "error": str(e)})


@app.route("/api/company-expenses", methods=["POST"])
def api_company_expenses_add():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    cat = data.get('category', '').strip()
    desc = data.get('description', '').strip()
    amount = float(data.get('amount', 0))
    date = data.get('date', '')
    recurring = int(data.get('recurring', 0))
    frequency = data.get('frequency', 'one-time') if recurring else 'one-time'
    next_renewal = data.get('next_renewal', '')
    if recurring and not next_renewal and date:
        # Auto-calculate next renewal
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(date, '%Y-%m-%d')
            if frequency == 'monthly':
                nr = d.replace(month=d.month % 12 + 1) if d.month < 12 else d.replace(year=d.year+1, month=1)
            elif frequency == 'yearly':
                nr = d.replace(year=d.year + 1)
            else:
                nr = d + timedelta(days=30)
            next_renewal = nr.strftime('%Y-%m-%d')
        except Exception:
            pass
    if not cat or not desc or not amount or not date:
        return jsonify({"error": "All fields required"}), 400
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _support_conn() as conn:
            conn.execute(
                "INSERT INTO company_expenses (category, description, amount, date, recurring, frequency, next_renewal, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cat, desc, amount, date, recurring, frequency, next_renewal or None, now))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/company-expenses/<int:expense_id>", methods=["DELETE"])
def api_company_expenses_delete(expense_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _support_conn() as conn:
            conn.execute("DELETE FROM company_expenses WHERE id=?", (expense_id,))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/company-expenses/<int:expense_id>", methods=["PUT"])
def api_company_expenses_update(expense_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    fields = []
    values = []
    for key in ('category', 'description', 'amount', 'date', 'recurring', 'frequency', 'next_renewal'):
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(expense_id)
    try:
        with _support_conn() as conn:
            conn.execute(f"UPDATE company_expenses SET {','.join(fields)} WHERE id=?", values)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/proxy/send-notification", methods=["POST"])
def proxy_send_notification():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        data = request.get_json(force=True)
        cookie = _get_admin_session_cookie()
        r = _req.post(f"{RETAIL_PORTAL_URL}/api/notifications/send",
                      json=data, cookies={'synthos_s': cookie}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# ── CUSTOMER SUPPORT QUEUE ────────────────────────────────────────────────────

@app.route("/api/proxy/support/all-tickets")
def proxy_support_all_tickets():
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        params = {}
        if request.args.get('status'): params['status'] = request.args['status']
        if request.args.get('category'): params['category'] = request.args['category']
        cookie = _get_admin_session_cookie()
        r = _req.get(f"{RETAIL_PORTAL_URL}/api/support/all-tickets",
                     params=params, cookies={'synthos_s': cookie}, timeout=15)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/support/ticket/<ticket_id>")
def proxy_support_ticket_detail(ticket_id):
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        params = {}
        if request.args.get('customer_id'):
            params['customer_id'] = request.args['customer_id']
        r = _req.get(f"{RETAIL_PORTAL_URL}/api/support/tickets/{ticket_id}",
                     params=params, cookies={'synthos_s': cookie}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/support/reply/<ticket_id>", methods=["POST"])
def proxy_support_reply(ticket_id):
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        data = request.get_json(force=True)
        data['sender'] = 'admin'
        r = _req.post(f"{RETAIL_PORTAL_URL}/api/support/tickets/{ticket_id}/reply",
                      json=data, cookies={'synthos_s': cookie}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxy/support/status/<ticket_id>", methods=["POST"])
def proxy_support_status(ticket_id):
    token = request.headers.get('X-Token', '')
    if token != SECRET_TOKEN and not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        data = request.get_json(force=True)
        r = _req.post(f"{RETAIL_PORTAL_URL}/api/support/tickets/{ticket_id}/status",
                      json=data, cookies={'synthos_s': cookie}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/beta-tests", methods=["GET"])
def api_beta_tests():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _support_conn() as conn:
            rows = conn.execute("SELECT * FROM beta_tests ORDER BY created_at DESC").fetchall()
            return jsonify({"tests": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"tests": [], "error": str(e)})


@app.route("/api/beta-tests", methods=["POST"])
def api_beta_tests_create():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    required = int(data.get('required_confirmations', 2))
    if not title or not description:
        return jsonify({"error": "title and description required"}), 400

    import secrets
    test_id = 'QA-' + secrets.token_hex(3).upper()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    try:
        with _support_conn() as conn:
            conn.execute(
                "INSERT INTO beta_tests (id, title, description, required_confirmations, status, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (test_id, title, description, required, now))

        # Broadcast notification to all customers
        import requests as _req
        cookie = _get_admin_session_cookie()
        _req.post(f"{RETAIL_PORTAL_URL}/api/notifications/broadcast",
                  json={
                      "category": "system",
                      "title": f"Beta Test: {title}",
                      "body": f"{description}\n\nOpen the Support panel to submit your response.",
                      "meta": {"beta_test_id": test_id, "type": "beta_test"}
                  },
                  cookies={'synthos_s': cookie}, timeout=15)

        print(f"[Monitor] Beta test {test_id} created and broadcast: {title}")
        return jsonify({"ok": True, "test_id": test_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/beta-tests/<test_id>/status", methods=["POST"])
def api_beta_test_status(test_id):
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    status = data.get('status', '').lower()
    if status not in ('cleared', 'cancelled', 'archived', 'active'):
        return jsonify({"error": "status must be cleared, cancelled, archived, or active"}), 400
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    try:
        with _support_conn() as conn:
            ts_col = {'cleared': 'cleared_at', 'cancelled': 'cancelled_at', 'archived': 'archived_at'}.get(status)
            conn.execute("UPDATE beta_tests SET status = ? WHERE id = ?", (status, test_id))
            if ts_col:
                try:
                    conn.execute(f"UPDATE beta_tests SET {ts_col} = ? WHERE id = ?", (now, test_id))
                except Exception:
                    pass  # column may not exist yet
        return jsonify({"ok": True, "test_id": test_id, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/proxy/direct-message", methods=["POST"])
def proxy_direct_message():
    """Send a direct message to a customer — creates a ticket in their portal DB."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    import requests as _req
    try:
        cookie = _get_admin_session_cookie()
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/support/direct-message",
            json=data,
            cookies={'synthos_s': cookie},
            timeout=15,
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/support-queue")
def support_queue_page():
    if not _authorized():
        return redirect(url_for("login"))
    resp = make_response(
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Synthos — Customer Support</title>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">'
        '</head><body>'
        + _subpage_header('Customer Support')
        + _SUPPORT_QUEUE_BODY
        + '</body></html>'
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


_SUPPORT_QUEUE_BODY = r"""
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.sq-page{max-width:1000px;margin:0 auto;padding:20px 24px}
.sq-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.sq-title::after{content:'';flex:1;height:1px;background:rgba(255,255,255,0.07)}
.sq-stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.sq-stat{border-radius:12px;padding:14px 16px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02)}
.sq-stat-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:4px}
.sq-stat-val{font-size:28px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1}
.sq-stat-sub{font-size:9px;color:rgba(255,255,255,0.25);margin-top:4px}
.sq-stat.amber .sq-stat-label{color:#f5a623}.sq-stat.amber .sq-stat-val{color:#f5a623}
.sq-stat.purple .sq-stat-label{color:#7b61ff}.sq-stat.purple .sq-stat-val{color:#7b61ff}
.sq-stat.pink .sq-stat-label{color:#ff4b6e}.sq-stat.pink .sq-stat-val{color:#ff4b6e}
.sq-stat.teal .sq-stat-label{color:#00f5d4}.sq-stat.teal .sq-stat-val{color:#00f5d4}
.sq-filters{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.sq-f{padding:5px 14px;border-radius:8px;font-size:11px;font-weight:600;background:transparent;border:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.4);cursor:pointer;font-family:inherit;transition:all .15s}
.sq-f.active{background:rgba(0,245,212,0.06);border-color:rgba(0,245,212,0.15);color:#00f5d4}
.sq-f.f-archived.active{background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.12);color:rgba(255,255,255,0.5)}
.sq-card{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;margin-bottom:8px;cursor:pointer;transition:border-color .15s;border-left:3px solid rgba(255,255,255,0.08)}
.sq-card:hover{border-color:rgba(255,255,255,0.15)}
.sq-card.st-open{border-left-color:#f5a623}
.sq-card.st-in_progress{border-left-color:#7b61ff}
.sq-card.st-resolved{border-left-color:#00f5d4}
.sq-card.st-archived{border-left-color:rgba(255,255,255,0.15);opacity:0.6}
.sq-card.st-closed{border-left-color:rgba(255,255,255,0.1)}
.sq-card-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.sq-subj{font-size:13px;font-weight:600}
.sq-badge{font-size:8px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase;letter-spacing:0.06em}
.sq-badge.portal{background:rgba(0,245,212,0.08);color:#00f5d4}
.sq-badge.account{background:rgba(123,97,255,0.08);color:#7b61ff}
.sq-badge.suggestion{background:rgba(245,166,35,0.08);color:#f5a623}
.sq-badge.beta_test{background:rgba(255,75,110,0.08);color:#ff4b6e}
.sq-badge.direct_message{background:rgba(123,97,255,0.08);color:#7b61ff}
.sq-badge.system{background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.5)}
.sq-status{font-size:8px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase}
.sq-status.open{background:rgba(245,166,35,0.1);color:#f5a623}
.sq-status.in_progress{background:rgba(123,97,255,0.1);color:#7b61ff}
.sq-status.resolved{background:rgba(0,245,212,0.1);color:#00f5d4}
.sq-status.archived{background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.35)}
.sq-status.closed{background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.3)}
.sq-meta{font-size:10px;color:rgba(255,255,255,0.35);margin-top:2px}
.sq-preview{font-size:11px;color:rgba(255,255,255,0.25);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sq-archive-btn{padding:3px 10px;border-radius:6px;font-size:9px;font-weight:600;border:1px solid rgba(255,255,255,0.08);background:transparent;color:rgba(255,255,255,0.3);cursor:pointer;margin-left:6px;transition:all .15s}
.sq-archive-btn:hover{border-color:rgba(255,255,255,0.2);color:rgba(255,255,255,0.6)}
.sq-detail{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:16px;margin-bottom:16px}
.sq-msg{padding:8px 10px;border-radius:8px;margin-bottom:6px}
.sq-msg.customer{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06)}
.sq-msg.admin{background:rgba(123,97,255,0.06);border:1px solid rgba(123,97,255,0.12)}
.sq-msg-head{font-size:9px;color:rgba(255,255,255,0.35);margin-bottom:3px}
.sq-msg-body{font-size:12px;color:rgba(255,255,255,0.88);line-height:1.5;white-space:pre-wrap}
.sq-reply-box{width:100%;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 10px;color:rgba(255,255,255,0.88);font-size:12px;font-family:'Inter',sans-serif;min-height:60px;resize:vertical;outline:none;margin-top:8px}
.sq-reply-btn{padding:6px 16px;border-radius:6px;border:none;background:#00f5d4;color:#000;font-size:11px;font-weight:700;cursor:pointer;margin-top:6px}
.sq-status-btns{display:flex;gap:6px;margin-top:8px}
.sq-st-btn{padding:4px 10px;border-radius:6px;font-size:9px;font-weight:600;border:1px solid rgba(255,255,255,0.1);background:transparent;color:rgba(255,255,255,0.4);cursor:pointer;transition:all .15s}
.sq-st-btn:hover{border-color:rgba(255,255,255,0.2);color:rgba(255,255,255,0.7)}
.sq-panel-tab{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:300;background:#0d1120;border:1px solid rgba(255,255,255,0.1);border-right:none;border-radius:10px 0 0 10px;padding:12px 8px;cursor:pointer;transition:transform .15s;display:flex;flex-direction:column;align-items:center;gap:6px}
.sq-panel-tab:hover{transform:translateY(-50%) translateX(-4px)}
.sq-panel-tab svg{width:16px;height:16px;opacity:0.5}
.sq-panel-tab span{writing-mode:vertical-rl;text-orientation:mixed;font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4)}
.sq-panel-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);backdrop-filter:blur(4px);z-index:350;opacity:0;pointer-events:none;transition:opacity .2s}
.sq-panel-overlay.open{opacity:1;pointer-events:all}
.sq-panel{position:fixed;right:0;top:0;bottom:0;width:min(480px,90vw);background:#0d1120;border-left:1px solid rgba(255,255,255,0.1);z-index:400;transform:translateX(100%);transition:transform .3s cubic-bezier(0.4,0,0.2,1);display:flex;flex-direction:column}
.sq-panel.open{transform:translateX(0)}
.sq-panel-hdr{padding:16px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,0.07)}
.sq-panel-close{width:28px;height:28px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);background:transparent;color:rgba(255,255,255,0.5);cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center}
.sq-panel-close:hover{border-color:rgba(255,255,255,0.2);color:rgba(255,255,255,0.8)}
.sq-tab-bar{display:flex;border-bottom:1px solid rgba(255,255,255,0.07);padding:0 20px}
.sq-tab{padding:10px 16px;font-size:11px;font-weight:600;color:rgba(255,255,255,0.35);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.sq-tab.active{color:#00f5d4;border-bottom-color:#00f5d4}
.sq-tab:hover{color:rgba(255,255,255,0.7)}
.sq-tab-body{flex:1;overflow-y:auto;padding:16px 20px}
.sq-tab-content{display:none}.sq-tab-content.active{display:block}
.sq-input{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 12px;color:rgba(255,255,255,0.88);font-size:12px;outline:none;font-family:'Inter',sans-serif;width:100%}
.sq-input:focus{border-color:rgba(0,245,212,0.3)}
.sq-create-btn{padding:8px 16px;border-radius:8px;border:none;font-size:11px;font-weight:700;cursor:pointer;transition:all .15s}
.sq-create-btn.pink{background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);color:#ff4b6e}
.sq-create-btn.teal{background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.15);color:#00f5d4}
.sq-create-btn:hover{filter:brightness(1.2)}
.sq-test-card{padding:12px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;margin-bottom:8px}
.sq-test-title{font-size:12px;font-weight:600}
.sq-test-desc{font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px}
.sq-test-meta{font-size:10px;color:rgba(255,255,255,0.3);margin-top:4px}
.sq-test-actions{display:flex;gap:6px;margin-top:8px}
.sq-test-btn{padding:4px 12px;border-radius:6px;font-size:9px;font-weight:600;cursor:pointer;border:1px solid;transition:all .15s}
.sq-test-btn.archive{border-color:rgba(255,255,255,0.1);background:transparent;color:rgba(255,255,255,0.4)}
.sq-test-btn.archive:hover{border-color:rgba(255,255,255,0.25);color:rgba(255,255,255,0.7)}
.sq-test-btn.cancel{border-color:rgba(255,75,110,0.2);background:rgba(255,75,110,0.06);color:#ff4b6e}
.sq-test-btn.cancel:hover{background:rgba(255,75,110,0.12)}
.sq-test-btn.clear{border-color:rgba(0,245,212,0.2);background:rgba(0,245,212,0.06);color:#00f5d4}
.sq-test-btn.clear:hover{background:rgba(0,245,212,0.12)}
.sq-resp{padding:10px 12px;border:1px solid rgba(255,255,255,0.06);border-radius:8px;margin-bottom:6px;cursor:pointer;transition:border-color .15s}
.sq-resp:hover{border-color:rgba(255,255,255,0.15)}
.sq-resp-head{font-size:10px;color:rgba(255,255,255,0.35);margin-bottom:3px}
.sq-resp-body{font-size:11px;color:rgba(255,255,255,0.7);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sq-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:8px 20px;border-radius:8px;font-size:12px;font-weight:600;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.sq-toast.show{opacity:1}.sq-toast.ok{background:#00f5d4;color:#000}.sq-toast.err{background:#ff4b6e;color:#fff}
</style>
<div class="sq-page">
  <div class="sq-stat-grid">
    <div class="sq-stat amber"><div class="sq-stat-label">Open</div><div class="sq-stat-val" id="sq-s-open">0</div><div class="sq-stat-sub">awaiting review</div></div>
    <div class="sq-stat purple"><div class="sq-stat-label">In Progress</div><div class="sq-stat-val" id="sq-s-progress">0</div><div class="sq-stat-sub">being handled</div></div>
    <div class="sq-stat pink"><div class="sq-stat-label">Beta Tests</div><div class="sq-stat-val" id="sq-s-beta">0</div><div class="sq-stat-sub">active tests</div></div>
    <div class="sq-stat teal"><div class="sq-stat-label">Resolved</div><div class="sq-stat-val" id="sq-s-resolved">0</div><div class="sq-stat-sub">completed</div></div>
  </div>
  <div class="sq-filters">
    <button class="sq-f active" data-filter="all">All</button>
    <button class="sq-f" data-filter="open">Open</button>
    <button class="sq-f" data-filter="in_progress">In Progress</button>
    <button class="sq-f" data-filter="resolved">Resolved</button>
    <button class="sq-f" data-filter="beta_test" data-cat="beta_test">Beta Tests</button>
    <button class="sq-f f-archived" data-filter="archived">Archived</button>
  </div>
  <div id="sq-list"><div style="color:rgba(255,255,255,0.3);text-align:center;padding:30px">Loading...</div></div>
  <div id="sq-detail"></div>
</div>
<div class="sq-panel-tab" id="sq-panel-tab">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
  <span>Tools</span>
</div>
<div class="sq-panel-overlay" id="sq-panel-overlay"></div>
<div class="sq-panel" id="sq-panel">
  <div class="sq-panel-hdr"><div style="font-size:14px;font-weight:700">Support Tools</div><button class="sq-panel-close" id="sq-panel-close-btn">&times;</button></div>
  <div class="sq-tab-bar" id="sq-tab-bar">
    <div class="sq-tab active" data-tab="beta">Beta Tests</div>
    <div class="sq-tab" data-tab="dm">Direct Message</div>
    <div class="sq-tab" data-tab="resp">Responses</div>
  </div>
  <div class="sq-tab-body">
    <div class="sq-tab-content active" id="sq-tc-beta">
      <div style="margin-bottom:16px">
        <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.4);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px">Create New Test</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <input class="sq-input" id="bt-title" aria-label="Beta test title" placeholder="Test title (e.g. Signup flow verification)">
          <textarea class="sq-input" id="bt-desc" aria-label="Beta test description" placeholder="Description of what to test..." style="min-height:60px;resize:vertical"></textarea>
          <div style="display:flex;gap:8px;align-items:center">
            <span style="font-size:10px;color:rgba(255,255,255,0.35)">Required:</span>
            <input class="sq-input" id="bt-required" aria-label="Required tester count" type="number" value="2" min="1" max="10" style="width:50px">
            <button type="button" class="sq-create-btn pink" id="bt-create-btn">Create &amp; Broadcast</button>
          </div>
        </div>
      </div>
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.4);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Active Tests</div>
      <div id="bt-list"><div style="font-size:11px;color:rgba(255,255,255,0.2);text-align:center;padding:20px">Loading...</div></div>
    </div>
    <div class="sq-tab-content" id="sq-tc-dm">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.4);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px">Send Direct Message</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <select class="sq-input" id="dm-customer" aria-label="Customer"><option value="">Loading customers...</option></select>
        <select class="sq-input" id="dm-category" aria-label="Message category"><option value="account">Account</option><option value="system">System</option><option value="alert">Alert</option><option value="daily">Daily</option></select>
        <input class="sq-input" id="dm-title" aria-label="Message title" placeholder="Message title">
        <textarea class="sq-input" id="dm-body" aria-label="Message body" placeholder="Message body..." style="min-height:80px;resize:vertical"></textarea>
        <button type="button" class="sq-create-btn teal" id="dm-send-btn">Send to Customer</button>
      </div>
    </div>
    <div class="sq-tab-content" id="sq-tc-resp">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.4);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px">Recent Customer Messages</div>
      <div id="resp-list"><div style="font-size:11px;color:rgba(255,255,255,0.2);text-align:center;padding:20px">Loading...</div></div>
    </div>
  </div>
</div>
<div class="sq-toast" id="sq-toast"></div>
<script>
var _sqFilter=null,_sqCatFilter=null,_allTickets=[];

function sqToast(m,t){var e=document.getElementById("sq-toast");e.textContent=m;e.className="sq-toast show "+(t||"ok");setTimeout(function(){e.className="sq-toast"},3000)}

// ── PANEL ──
function openSqPanel(){document.getElementById("sq-panel").classList.add("open");document.getElementById("sq-panel-overlay").classList.add("open");document.getElementById("sq-panel-tab").style.display="none";sqLoadBetaTests();sqLoadCustomerList()}
function closeSqPanel(){document.getElementById("sq-panel").classList.remove("open");document.getElementById("sq-panel-overlay").classList.remove("open");document.getElementById("sq-panel-tab").style.display=""}
document.getElementById("sq-panel-tab").addEventListener("click",openSqPanel);
document.getElementById("sq-panel-overlay").addEventListener("click",closeSqPanel);
document.getElementById("sq-panel-close-btn").addEventListener("click",closeSqPanel);

// ── TABS ──
document.getElementById("sq-tab-bar").addEventListener("click",function(e){
  var tab=e.target.dataset.tab;if(!tab)return;
  document.querySelectorAll(".sq-tab").forEach(function(t){t.classList.remove("active")});
  document.querySelectorAll(".sq-tab-content").forEach(function(t){t.classList.remove("active")});
  e.target.classList.add("active");
  document.getElementById("sq-tc-"+tab).classList.add("active");
  if(tab==="resp")sqLoadResponses();if(tab==="beta")sqLoadBetaTests();
});

// ── FILTERS ──
document.querySelector(".sq-filters").addEventListener("click",function(e){
  var btn=e.target.closest(".sq-f");if(!btn)return;
  var f=btn.dataset.filter;
  _sqFilter=(f==="all")?null:(f==="beta_test"?null:f);
  _sqCatFilter=btn.dataset.cat||null;
  document.querySelectorAll(".sq-f").forEach(function(b){b.classList.remove("active")});
  btn.classList.add("active");
  document.getElementById("sq-detail").innerHTML="";
  sqRenderTickets();
});

// ── STATS ──
function sqUpdateStats(){var o=0,p=0,r=0;_allTickets.forEach(function(t){if(t.status==="open")o++;else if(t.status==="in_progress")p++;else if(t.status==="resolved")r++});document.getElementById("sq-s-open").textContent=o;document.getElementById("sq-s-progress").textContent=p;document.getElementById("sq-s-resolved").textContent=r}

// ── TICKETS ──
async function sqLoadTickets(){
  var el=document.getElementById("sq-list");
  try{
    var r=await fetch("/api/proxy/support/all-tickets");
    var d=await r.json();
    _allTickets=d.tickets||[];
    sqUpdateStats();
    sqRenderTickets();
  }catch(e){el.innerHTML="<div style='color:#ff4b6e;padding:20px;text-align:center'>Error loading tickets: "+e.message+"</div>"}
}

function sqRenderTickets(){
  var el=document.getElementById("sq-list");
  var tickets=_allTickets.filter(function(t){
    if(_sqFilter==="archived")return t.status==="archived";
    if(_sqFilter&&t.status!==_sqFilter)return false;
    if(_sqCatFilter&&t.category!==_sqCatFilter)return false;
    if(!_sqFilter&&!_sqCatFilter&&t.status==="archived")return false;
    return true;
  });
  if(!tickets.length){el.innerHTML="<div style='color:rgba(255,255,255,0.3);text-align:center;padding:30px'>No tickets found</div>";return}
  el.innerHTML=tickets.map(function(t){
    var last=t.last_message?t.last_message.message.slice(0,80):"";
    var name=t.customer_name||t.customer_email||(t.customer_id||"").slice(0,8);
    var archBtn=t.status!=="archived"?" <button class='sq-archive-btn' data-action='archive' data-tid='"+t.ticket_id+"' data-cid='"+(t.customer_id||"")+"'>Archive</button>":"";
    return "<div class='sq-card st-"+t.status+"' data-action='view' data-tid='"+t.ticket_id+"' data-cid='"+(t.customer_id||"")+"'>"
      +"<div class='sq-card-top'>"
      +"<div class='sq-subj'>"+(t.subject||"")+"</div>"
      +"<div style='display:flex;gap:4px;align-items:center'><span class='sq-badge "+t.category+"'>"+t.category+"</span>"
      +"<span class='sq-status "+t.status+"'>"+t.status.replace("_"," ")+"</span>"+archBtn+"</div></div>"
      +"<div class='sq-meta'>"+name+" &middot; "+(t.updated_at||"").slice(0,16)+" &middot; "+(t.message_count||0)+" msg</div>"
      +(last?"<div class='sq-preview'>"+last+"</div>":"")
      +"</div>";
  }).join("");
}

// ── EVENT DELEGATION for tickets ──
document.getElementById("sq-list").addEventListener("click",function(e){
  var archBtn=e.target.closest("[data-action='archive']");
  if(archBtn){e.stopPropagation();sqArchive(archBtn.dataset.tid,archBtn.dataset.cid);return}
  var card=e.target.closest("[data-action='view']");
  if(card)sqViewTicket(card.dataset.tid,card.dataset.cid);
});

async function sqArchive(tid,cid){
  await fetch("/api/proxy/support/status/"+tid,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:"archived",customer_id:cid})});
  sqToast("Archived: "+tid);sqLoadTickets();
}

async function sqViewTicket(ticketId,customerId){
  var el=document.getElementById("sq-detail");
  try{
    var r=await fetch("/api/proxy/support/ticket/"+ticketId+"?customer_id="+customerId);
    var d=await r.json();
    if(!d.ticket){el.innerHTML="Ticket not found";return}
    var t=d.ticket;var msgs=d.messages||[];
    var html="<div class='sq-detail'>";
    html+="<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px'>";
    html+="<div style='font-size:14px;font-weight:700'>"+t.subject+"</div>";
    html+="<div style='display:flex;gap:4px;align-items:center'><span class='sq-status "+t.status+"'>"+t.status.replace("_"," ")+"</span>";
    if(t.status!=="archived")html+="<button class='sq-archive-btn' data-action='detail-archive' data-tid='"+ticketId+"' data-cid='"+customerId+"'>Archive</button>";
    html+="</div></div>";
    html+="<div style='font-size:10px;color:rgba(255,255,255,0.35);margin-bottom:12px'>"+t.ticket_id+" &middot; "+t.category+"</div>";
    msgs.forEach(function(m){
      var cls=m.sender==="admin"?"admin":"customer";
      html+="<div class='sq-msg "+cls+"'>";
      html+="<div class='sq-msg-head'>"+(m.sender==="admin"?"You":"Customer")+" &middot; "+(m.created_at||"").slice(0,16)+"</div>";
      html+="<div class='sq-msg-body'>"+m.message+"</div></div>";
    });
    html+="<textarea class='sq-reply-box' id='sq-reply' aria-label='Reply to customer' placeholder='Write a reply...'></textarea>";
    html+="<div style='display:flex;justify-content:space-between;align-items:center'>";
    html+="<button class='sq-reply-btn' data-action='reply' data-tid='"+ticketId+"' data-cid='"+customerId+"'>Send Reply</button>";
    html+="<div class='sq-status-btns'>";
    html+="<button class='sq-st-btn' data-action='status' data-tid='"+ticketId+"' data-cid='"+customerId+"' data-st='in_progress'>In Progress</button>";
    html+="<button class='sq-st-btn' data-action='status' data-tid='"+ticketId+"' data-cid='"+customerId+"' data-st='resolved'>Resolved</button>";
    html+="<button class='sq-st-btn' data-action='status' data-tid='"+ticketId+"' data-cid='"+customerId+"' data-st='archived'>Archive</button>";
    html+="</div></div></div>";
    el.innerHTML=html;
    el.scrollIntoView({behavior:"smooth"});
  }catch(e){el.innerHTML="Error loading ticket"}
}

// ── EVENT DELEGATION for detail view ──
document.getElementById("sq-detail").addEventListener("click",function(e){
  var btn=e.target.closest("[data-action]");if(!btn)return;
  var action=btn.dataset.action;
  if(action==="detail-archive"){sqArchive(btn.dataset.tid,btn.dataset.cid);return}
  if(action==="reply"){var msg=document.getElementById("sq-reply").value.trim();if(msg)sqReply(btn.dataset.tid,btn.dataset.cid,msg);return}
  if(action==="status"){sqSetStatus(btn.dataset.tid,btn.dataset.cid,btn.dataset.st);return}
});

async function sqReply(tid,cid,msg){
  await fetch("/api/proxy/support/reply/"+tid,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:msg,customer_id:cid})});
  sqToast("Reply sent");sqViewTicket(tid,cid);
}

async function sqSetStatus(tid,cid,st){
  await fetch("/api/proxy/support/status/"+tid,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:st,customer_id:cid})});
  sqToast("Status: "+st.replace("_"," "));sqLoadTickets();
  if(st!=="archived")sqViewTicket(tid,cid);else document.getElementById("sq-detail").innerHTML="";
}

// ── BETA TESTS ──
document.getElementById("bt-create-btn").addEventListener("click",sqCreateBetaTest);
async function sqCreateBetaTest(){
  var title=document.getElementById("bt-title").value.trim();
  var desc=document.getElementById("bt-desc").value.trim();
  var req=parseInt(document.getElementById("bt-required").value)||2;
  if(!title||!desc){sqToast("Title and description required","err");return}
  var r=await fetch("/api/beta-tests",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title,description:desc,required_confirmations:req})});
  var d=await r.json();
  if(d.ok){sqToast("Beta test created: "+d.test_id);document.getElementById("bt-title").value="";document.getElementById("bt-desc").value="";sqLoadBetaTests()}
  else{sqToast(d.error||"Failed","err")}
}

async function sqLoadBetaTests(){
  var el=document.getElementById("bt-list");
  try{
    var r=await fetch("/api/beta-tests");var d=await r.json();var tests=d.tests||[];
    var active=tests.filter(function(t){return t.status==="active"}).length;
    document.getElementById("sq-s-beta").textContent=active;
    if(!tests.length){el.innerHTML="<div style='font-size:11px;color:rgba(255,255,255,0.2);text-align:center;padding:20px'>No beta tests yet</div>";return}
    el.innerHTML=tests.map(function(t){
      var stColor=t.status==="cleared"?"#00f5d4":t.status==="cancelled"?"#ff4b6e":t.status==="archived"?"rgba(255,255,255,0.35)":"#f5a623";
      var actions="";
      if(t.status==="active"){
        actions="<div class='sq-test-actions'>"
          +"<button class='sq-test-btn clear' data-action='beta-status' data-id='"+t.id+"' data-st='cleared'>Clear Test</button>"
          +"<button class='sq-test-btn archive' data-action='beta-status' data-id='"+t.id+"' data-st='archived'>Archive</button>"
          +"<button class='sq-test-btn cancel' data-action='beta-status' data-id='"+t.id+"' data-st='cancelled'>Cancel Test</button>"
          +"</div>";
      }
      return "<div class='sq-test-card'>"
        +"<div style='display:flex;justify-content:space-between;align-items:center'>"
        +"<div class='sq-test-title'>"+t.id+": "+t.title+"</div>"
        +"<span style='font-size:9px;font-weight:700;color:"+stColor+";text-transform:uppercase'>"+t.status+"</span></div>"
        +"<div class='sq-test-desc'>"+(t.description||"").slice(0,100)+"</div>"
        +"<div class='sq-test-meta'>Required: "+t.required_confirmations+" confirmations</div>"
        +actions+"</div>";
    }).join("");
  }catch(e){}
}

document.getElementById("bt-list").addEventListener("click",function(e){
  var btn=e.target.closest("[data-action='beta-status']");if(!btn)return;
  sqBetaStatus(btn.dataset.id,btn.dataset.st);
});

async function sqBetaStatus(testId,status){
  try{var r=await fetch("/api/beta-tests/"+testId+"/status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:status})});
  var d=await r.json();if(d.ok){sqToast("Test "+testId+": "+status);sqLoadBetaTests()}else{sqToast(d.error||"Failed","err")}}
  catch(e){sqToast("Error: "+e.message,"err")}
}

// ── DIRECT MESSAGE ──
document.getElementById("dm-send-btn").addEventListener("click",sqSendDirect);
async function sqSendDirect(){
  var cid=document.getElementById("dm-customer").value;
  var title=document.getElementById("dm-title").value.trim();
  var body=document.getElementById("dm-body").value.trim();
  if(!cid){sqToast("Select a customer first","err");return}
  if(!title){sqToast("Enter a message title","err");return}
  if(!body){sqToast("Enter a message body","err");return}
  try{var r=await fetch("/api/proxy/direct-message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({customer_id:cid,title:title,message:body})});
  if(!r.ok){sqToast("Server error: HTTP "+r.status,"err");return}
  var d=await r.json();if(d.ok){sqToast("Message sent");document.getElementById("dm-title").value="";document.getElementById("dm-body").value="";sqLoadTickets()}
  else{sqToast(d.error||"Send failed","err")}}catch(e){sqToast("Network error: "+e.message,"err")}
}

async function sqLoadCustomerList(){
  try{var r=await fetch("/api/proxy/billing/all-customers");var d=await r.json();
  var sel=document.getElementById("dm-customer");if(!sel)return;
  var custs=d.customers||[];sel.innerHTML="<option value=''>Select customer...</option>";
  custs.forEach(function(c){var opt=document.createElement("option");opt.value=c.id;opt.textContent=(c.name||c.email||c.id.slice(0,8));sel.appendChild(opt)})}catch(e){}
}

// ── RESPONSES ──
async function sqLoadResponses(){
  var el=document.getElementById("resp-list");
  try{var r=await fetch("/api/proxy/support/all-tickets");var d=await r.json();var tickets=d.tickets||[];
  var responses=[];
  tickets.forEach(function(t){if(t.last_message&&t.last_message.sender==="customer"){
    responses.push({ticket_id:t.ticket_id,customer_id:t.customer_id,subject:t.subject,message:t.last_message.message,time:t.last_message.created_at,name:t.customer_name||t.customer_email||(t.customer_id||"").slice(0,8)})}});
  responses.sort(function(a,b){return(b.time||"").localeCompare(a.time||"")});
  if(!responses.length){el.innerHTML="<div style='font-size:11px;color:rgba(255,255,255,0.2);text-align:center;padding:20px'>No customer messages yet</div>";return}
  el.innerHTML=responses.slice(0,20).map(function(r){
    return "<div class='sq-resp' data-action='view-resp' data-tid='"+r.ticket_id+"' data-cid='"+r.customer_id+"'>"
      +"<div class='sq-resp-head'>"+r.name+" &middot; "+r.subject+" &middot; "+(r.time||"").slice(0,16)+"</div>"
      +"<div class='sq-resp-body'>"+(r.message||"").slice(0,120)+"</div></div>";
  }).join("")}catch(e){el.innerHTML="<div style='color:#ff4b6e;text-align:center;padding:20px'>Error</div>"}
}

document.getElementById("resp-list").addEventListener("click",function(e){
  var el=e.target.closest("[data-action='view-resp']");if(!el)return;
  closeSqPanel();sqViewTicket(el.dataset.tid,el.dataset.cid);
});

// ── INIT ──
sqLoadTickets();
sqLoadBetaTests();
setInterval(sqLoadTickets,15000);
</script>
"""







# ── COMPANY FINANCES PAGE ─────────────────────────────────────────────────────

@app.route("/company-finances")
def company_finances_page():
    if not _authorized():
        return redirect(url_for("login"))
    return _subpage_header('Company Finances') + """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.fin-card{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:20px;margin-bottom:16px}
.fin-title{font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px}
.fin-input{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 12px;color:rgba(255,255,255,0.88);font-size:12px;outline:none;width:100%}
.fin-input:focus{border-color:rgba(0,245,212,0.3)}
.fin-btn{padding:8px 16px;border-radius:8px;border:none;font-size:12px;font-weight:700;cursor:pointer}
.fin-btn-teal{background:#00f5d4;color:#000}
.fin-btn-sm{padding:4px 8px;font-size:9px;font-weight:700;border-radius:5px;border:1px solid rgba(255,255,255,0.08);background:transparent;cursor:pointer;font-family:inherit}
.fin-btn-edit{color:rgba(255,255,255,0.4)}
.fin-btn-edit:hover{color:#00f5d4;border-color:rgba(0,245,212,0.3)}
.fin-btn-del{color:rgba(255,75,110,0.5)}
.fin-btn-del:hover{color:#ff4b6e;border-color:rgba(255,75,110,0.3)}
.renewal-badge{font-size:8px;font-weight:700;padding:2px 6px;border-radius:99px;letter-spacing:0.04em}
.rb-monthly{background:rgba(123,97,255,0.1);border:1px solid rgba(123,97,255,0.2);color:#a78bfa}
.rb-yearly{background:rgba(255,179,71,0.1);border:1px solid rgba(255,179,71,0.2);color:#ffb347}
</style>
<div style="max-width:1000px;margin:0 auto;padding:20px 24px">
  <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px">Revenue &amp; Expenses</div>

  <!-- SUMMARY CARDS -->
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px">
    <div class="fin-card" style="text-align:center;margin-bottom:0">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Monthly Recurring</div>
      <div style="font-size:22px;font-weight:700;color:#ff4b6e" id="fin-monthly">$0</div>
    </div>
    <div class="fin-card" style="text-align:center;margin-bottom:0">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Yearly Recurring</div>
      <div style="font-size:22px;font-weight:700;color:#ffb347" id="fin-yearly">$0</div>
    </div>
    <div class="fin-card" style="text-align:center;margin-bottom:0">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">One-Time Total</div>
      <div style="font-size:22px;font-weight:700;color:rgba(255,255,255,0.5)" id="fin-onetime">$0</div>
    </div>
    <div class="fin-card" style="text-align:center;margin-bottom:0">
      <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">All-Time Total</div>
      <div style="font-size:22px;font-weight:700;color:#ff4b6e" id="fin-total">$0</div>
    </div>
  </div>

  <!-- RECURRING SUBSCRIPTIONS -->
  <div class="fin-card">
    <div class="fin-title">Recurring Expenses &amp; Renewals</div>
    <div id="fin-recurring"><div style="text-align:center;padding:20px;color:rgba(255,255,255,0.2);font-size:12px">No recurring expenses</div></div>
  </div>

  <!-- ALL EXPENSES -->
  <div class="fin-card">
    <div class="fin-title">Expense Log</div>
    <div id="exp-list"><div style="text-align:center;padding:20px;color:rgba(255,255,255,0.2);font-size:12px">Loading...</div></div>
  </div>

  <!-- ADD EXPENSE FORM -->
  <div class="fin-card">
    <div class="fin-title">Add Expense</div>
    <div style="display:grid;gap:8px;max-width:600px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <select id="exp-cat" class="fin-input">
          <option value="infrastructure">Infrastructure</option>
          <option value="api_costs">API Costs</option>
          <option value="hardware">Hardware</option>
          <option value="software">Software/Licenses</option>
          <option value="hosting">Hosting</option>
          <option value="subscription">Subscription</option>
          <option value="other">Other</option>
        </select>
        <select id="exp-freq" class="fin-input" onchange="toggleRenewal()">
          <option value="one-time">One-time</option>
          <option value="monthly">Monthly</option>
          <option value="yearly">Yearly</option>
        </select>
      </div>
      <input id="exp-desc" class="fin-input" placeholder="Description (e.g. Cloudflare Pro, Resend API)">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        <input id="exp-amount" type="number" step="0.01" class="fin-input" placeholder="Amount ($)">
        <input id="exp-date" type="date" class="fin-input">
        <input id="exp-renewal" type="date" class="fin-input" placeholder="Next renewal" style="display:none">
      </div>
      <button onclick="addExpense()" class="fin-btn fin-btn-teal">Add Expense</button>
    </div>
  </div>

  <!-- EDIT MODAL -->
  <div id="edit-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:999;align-items:center;justify-content:center">
    <div style="background:#111520;border:1px solid rgba(255,255,255,0.12);border-radius:14px;padding:24px;max-width:400px;width:90%">
      <div class="fin-title">Edit Expense</div>
      <input type="hidden" id="edit-id">
      <div style="display:grid;gap:8px">
        <select id="edit-cat" class="fin-input"></select>
        <input id="edit-desc" class="fin-input" placeholder="Description">
        <input id="edit-amount" type="number" step="0.01" class="fin-input" placeholder="Amount">
        <input id="edit-date" type="date" class="fin-input">
        <select id="edit-freq" class="fin-input">
          <option value="one-time">One-time</option>
          <option value="monthly">Monthly</option>
          <option value="yearly">Yearly</option>
        </select>
        <input id="edit-renewal" type="date" class="fin-input" placeholder="Next renewal">
        <div style="display:flex;gap:8px;margin-top:4px">
          <button onclick="saveEdit()" class="fin-btn fin-btn-teal" style="flex:1">Save</button>
          <button onclick="closeEdit()" class="fin-btn" style="flex:1;background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.5)">Cancel</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
function toggleRenewal() {
  var freq = document.getElementById('exp-freq').value;
  document.getElementById('exp-renewal').style.display = freq === 'one-time' ? 'none' : 'block';
}
function fmt(v) { return '$' + Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }

async function loadExpenses() {
  try {
    var r = await fetch('/api/company-expenses');
    var d = await r.json();
    var exps = d.expenses || [];

    // Summary — both monthly + yearly cards now sum the SAME pool of
    // recurring obligations, just in different time units. Bug 2026-04-28:
    // previously the yearly card filtered for frequency==='yearly' only,
    // so a $50/mo subscription contributed $0 to the yearly total instead
    // of $600. Symmetric fix on the monthly card so both views stay
    // consistent. one-time and total cards unchanged.
    var monthlySum = exps.filter(function(e){return e.frequency==='monthly'}).reduce(function(s,e){return s+e.amount},0);
    var yearlySum  = exps.filter(function(e){return e.frequency==='yearly'}).reduce(function(s,e){return s+e.amount},0);
    var onetime    = exps.filter(function(e){return !e.frequency||e.frequency==='one-time'}).reduce(function(s,e){return s+e.amount},0);
    var total      = exps.reduce(function(s,e){return s+e.amount},0);
    var mrr_eq     = monthlySum + yearlySum / 12;   // monthly recurring (yearly prorated to /mo)
    var arr_eq     = monthlySum * 12 + yearlySum;   // yearly recurring (monthly annualized)
    document.getElementById('fin-monthly').textContent = fmt(mrr_eq) + '/mo';
    document.getElementById('fin-yearly').textContent  = fmt(arr_eq) + '/yr';
    document.getElementById('fin-onetime').textContent = fmt(onetime);
    document.getElementById('fin-total').textContent   = fmt(total);

    // Recurring section
    var recurring = exps.filter(function(e){return e.frequency && e.frequency !== 'one-time'});
    var recEl = document.getElementById('fin-recurring');
    if (!recurring.length) {
      recEl.innerHTML = '<div style="text-align:center;padding:16px;color:rgba(255,255,255,0.2);font-size:11px">No recurring expenses</div>';
    } else {
      recEl.innerHTML = recurring.map(function(e){
        var badge = e.frequency==='monthly' ? '<span class="renewal-badge rb-monthly">MONTHLY</span>' : '<span class="renewal-badge rb-yearly">YEARLY</span>';
        var renewal = e.next_renewal ? '<span style="font-size:10px;color:rgba(255,255,255,0.35)">Renews: '+e.next_renewal+'</span>' : '';
        return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04)">'
          + '<div style="flex:1"><div style="font-size:12px;font-weight:600">'+e.description+'</div>'
          + '<div style="font-size:10px;color:rgba(255,255,255,0.4)">'+e.category+' '+badge+' '+renewal+'</div></div>'
          + '<div style="font-size:14px;font-weight:700;color:#ff4b6e">'+fmt(e.amount)+'</div>'
          + '<button class="fin-btn-sm fin-btn-edit" onclick="openEdit('+JSON.stringify(e).replace(/"/g,'&quot;')+')">Edit</button>'
          + '<button class="fin-btn-sm fin-btn-del" onclick="delExpense('+e.id+')">Delete</button>'
          + '</div>';
      }).join('');
    }

    // All expenses table
    var el = document.getElementById('exp-list');
    if (!exps.length) { el.innerHTML = '<div style="text-align:center;padding:20px;color:rgba(255,255,255,0.2)">No expenses recorded</div>'; return; }
    el.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px">'
      + '<tr style="border-bottom:1px solid rgba(255,255,255,0.08);font-size:9px;color:rgba(255,255,255,0.3);text-transform:uppercase;letter-spacing:0.06em">'
      + '<th style="padding:6px;text-align:left">Date</th><th style="padding:6px;text-align:left">Category</th>'
      + '<th style="padding:6px;text-align:left">Description</th><th style="padding:6px;text-align:center">Type</th>'
      + '<th style="padding:6px;text-align:right">Amount</th><th style="padding:6px"></th></tr>'
      + exps.map(function(e){
        var freq = e.frequency || 'one-time';
        var badge = freq==='monthly'?'<span class="renewal-badge rb-monthly">MO</span>':freq==='yearly'?'<span class="renewal-badge rb-yearly">YR</span>':'';
        return '<tr style="border-bottom:1px solid rgba(255,255,255,0.04)">'
          + '<td style="padding:6px;font-size:10px;color:rgba(255,255,255,0.4)">'+e.date+'</td>'
          + '<td style="padding:6px;font-size:11px;color:rgba(255,255,255,0.5)">'+e.category+'</td>'
          + '<td style="padding:6px">'+e.description+'</td>'
          + '<td style="padding:6px;text-align:center">'+badge+'</td>'
          + '<td style="padding:6px;text-align:right;color:#ff4b6e;font-weight:600">'+fmt(e.amount)+'</td>'
          + '<td style="padding:6px;text-align:right;white-space:nowrap">'
          + '<button class="fin-btn-sm fin-btn-edit" onclick="openEdit('+JSON.stringify(e).replace(/"/g,'&quot;')+')">Edit</button> '
          + '<button class="fin-btn-sm fin-btn-del" onclick="delExpense('+e.id+')">Del</button></td></tr>';
      }).join('') + '</table>';
  } catch(e) { console.error('loadExpenses:', e); }
}

async function addExpense() {
  var freq = document.getElementById('exp-freq').value;
  var data = {
    category: document.getElementById('exp-cat').value,
    description: document.getElementById('exp-desc').value.trim(),
    amount: parseFloat(document.getElementById('exp-amount').value),
    date: document.getElementById('exp-date').value,
    recurring: freq !== 'one-time' ? 1 : 0,
    frequency: freq,
    next_renewal: document.getElementById('exp-renewal').value || ''
  };
  if (!data.description || !data.amount || !data.date) { alert('All fields required'); return; }
  await fetch('/api/company-expenses', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  document.getElementById('exp-desc').value = '';
  document.getElementById('exp-amount').value = '';
  loadExpenses();
}

async function delExpense(id) {
  if (!confirm('Delete this expense?')) return;
  await fetch('/api/company-expenses/' + id, {method:'DELETE'});
  loadExpenses();
}

function openEdit(e) {
  document.getElementById('edit-id').value = e.id;
  document.getElementById('edit-cat').innerHTML = document.getElementById('exp-cat').innerHTML;
  document.getElementById('edit-cat').value = e.category;
  document.getElementById('edit-desc').value = e.description;
  document.getElementById('edit-amount').value = e.amount;
  document.getElementById('edit-date').value = e.date;
  document.getElementById('edit-freq').value = e.frequency || 'one-time';
  document.getElementById('edit-renewal').value = e.next_renewal || '';
  document.getElementById('edit-overlay').style.display = 'flex';
}
function closeEdit() { document.getElementById('edit-overlay').style.display = 'none'; }
async function saveEdit() {
  var id = document.getElementById('edit-id').value;
  var freq = document.getElementById('edit-freq').value;
  var data = {
    category: document.getElementById('edit-cat').value,
    description: document.getElementById('edit-desc').value.trim(),
    amount: parseFloat(document.getElementById('edit-amount').value),
    date: document.getElementById('edit-date').value,
    frequency: freq,
    recurring: freq !== 'one-time' ? 1 : 0,
    next_renewal: document.getElementById('edit-renewal').value || ''
  };
  await fetch('/api/company-expenses/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  closeEdit();
  loadExpenses();
}

document.getElementById('exp-date').value = new Date().toISOString().slice(0,10);
loadExpenses();
</script>
"""


# ── REPORTS PAGE"""


# ── REPORTS PAGE ──────────────────────────────────────────────────────────────

@app.route("/reports")
def reports_page():
    if not _authorized():
        return redirect(url_for("login"))
    return _subpage_header('Reports') + """
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}</style>
<div style="max-width:800px;margin:0 auto;padding:20px 24px">
  <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px">Exportable Reports</div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:24px">
      <div style="font-size:14px;font-weight:700;color:rgba(255,255,255,0.88);margin-bottom:4px">Revenue Report</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-bottom:16px">Monthly/quarterly revenue by customer, subscription tier, and payment status.</div>
      <div style="font-size:10px;color:rgba(255,255,255,0.2)">PDF + Spreadsheet — Module pending</div>
    </div>
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:24px">
      <div style="font-size:14px;font-weight:700;color:rgba(255,255,255,0.88);margin-bottom:4px">Expense Report</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-bottom:16px">API costs, infrastructure, manual entries. Categorized by type.</div>
      <div style="font-size:10px;color:rgba(255,255,255,0.2)">PDF + Spreadsheet — Module pending</div>
    </div>
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:24px">
      <div style="font-size:14px;font-weight:700;color:rgba(255,255,255,0.88);margin-bottom:4px">Tax Summary</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-bottom:16px">Georgia sales tax collected, quarterly estimates, filing-ready export.</div>
      <div style="font-size:10px;color:rgba(255,255,255,0.2)">PDF — Module pending</div>
    </div>
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:24px">
      <div style="font-size:14px;font-weight:700;color:rgba(255,255,255,0.88);margin-bottom:4px">Customer Activity</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-bottom:16px">Login history, trades, agent usage per customer.</div>
      <div style="font-size:10px;color:rgba(255,255,255,0.2)">Spreadsheet — Module pending</div>
    </div>
  </div>
</div>
"""


# ── CUSTOMER ACTIVITY REPORT (V1, 2026-04-28) ────────────────────────────
#
# Operator-facing cross-customer trading activity report.
#
# Data flow:
#   browser → /customer-activity page (form: dates + customer scope)
#           → POST /api/proxy/activity-report
#           → forwards to RETAIL_PORTAL_URL/api/admin/activity-report
#               (with Bearer SECRET_TOKEN; bypasses customer-session auth)
#           → retail engine reads stored data, returns structured JSON
#           → cmd portal renders as HTML tables.
#
# Engine: synthos_build/tools/customer_activity_report.py on pi5.
# Why on pi5: the per-customer signals.db files live there; pi4b is
# a thin viewing surface.
# No live Alpaca calls — everything from stored data per operator
# preference.


@app.route("/api/proxy/activity-report", methods=["POST"])
def proxy_activity_report():
    """Forward an activity-report request to retail portal.  POST body
    is passed through unchanged; auth converted from session-cookie
    (cmd portal) to Bearer SECRET_TOKEN (retail portal monitor auth)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    body = request.get_json(silent=True) or {}
    try:
        r = _req.post(
            f"{RETAIL_PORTAL_URL}/api/admin/activity-report",
            json=body,
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
            timeout=30,
        )
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"error": f"retail returned non-JSON ({r.status_code})",
                            "body": r.text[:500]}), 502
    except Exception as e:
        return jsonify({"error": f"proxy failed: {e}"}), 502


_CUSTOMER_ACTIVITY_TEMPLATE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.ca-wrap{max-width:1200px;margin:0 auto;padding:20px 24px}
.ca-h{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px}
.ca-form{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:16px;margin-bottom:18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:end}
.ca-form label{display:block;font-size:10px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px}
.ca-form input,.ca-form select{width:100%;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:8px 10px;color:rgba(255,255,255,0.88);font-family:'JetBrains Mono',monospace;font-size:12px}
.ca-form input:focus,.ca-form select:focus{outline:none;border-color:rgba(0,245,212,0.4)}
.ca-form button{background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.3);color:#00f5d4;border-radius:6px;padding:8px 16px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer}
.ca-form button:hover{background:rgba(0,245,212,0.18)}
.ca-presets{display:flex;gap:6px;flex-wrap:wrap;grid-column:1/-1}
.ca-presets button{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:rgba(255,255,255,0.6);font-size:10px;padding:5px 10px;text-transform:none;letter-spacing:0}
.ca-presets button:hover{background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.88)}
.ca-customer-list{max-height:140px;overflow-y:auto;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:6px;background:rgba(255,255,255,0.02)}
.ca-customer-list label{display:flex;align-items:center;gap:6px;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:10px;color:rgba(255,255,255,0.6);text-transform:none;letter-spacing:0;margin:0}
.ca-customer-list label:hover{background:rgba(255,255,255,0.04)}
.ca-customer-list input[type=checkbox]{margin-right:4px;width:auto}
.ca-customer-list .ca-bulk{padding:4px 6px;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:4px;display:flex;gap:8px}
.ca-customer-list .ca-bulk a{font-size:10px;color:rgba(0,245,212,0.7);text-decoration:none;cursor:pointer}
.ca-customer-list .ca-bulk a:hover{color:#00f5d4}
.ca-out{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px;margin-bottom:14px}
.ca-customer-block{margin-bottom:18px;padding:12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:8px}
.ca-cust-h{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid rgba(255,255,255,0.06)}
.ca-cust-name{font-size:14px;font-weight:600;color:rgba(255,255,255,0.95)}
.ca-cust-cid{font-family:'JetBrains Mono',monospace;font-size:10px;color:rgba(255,255,255,0.3)}
.ca-stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:10px}
.ca-stat{padding:8px 10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:6px}
.ca-stat-label{font-size:9px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:3px}
.ca-stat-val{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700}
.ca-pos{color:#00f5d4}.ca-neg{color:#ff4b6e}.ca-mut{color:rgba(255,255,255,0.5)}
.ca-table{width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace;margin-top:6px}
.ca-table th{text-align:left;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,0.1);font-weight:600;color:rgba(255,255,255,0.4);font-size:10px;text-transform:uppercase;letter-spacing:0.06em}
.ca-table td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,0.04);color:rgba(255,255,255,0.85)}
.ca-table tr:hover td{background:rgba(255,255,255,0.02)}
.ca-section-h{font-size:10px;font-weight:700;color:rgba(255,255,255,0.55);letter-spacing:0.08em;text-transform:uppercase;margin:14px 0 6px}
.ca-empty{text-align:center;padding:30px;color:rgba(255,255,255,0.25);font-size:12px}
.ca-cluster{padding:10px 12px;background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.2);border-radius:6px;margin-bottom:6px}
.ca-cluster-header{font-size:11px;font-weight:600;color:#f5a623;margin-bottom:4px}
.ca-tools{display:flex;gap:8px;justify-content:flex-end;margin-bottom:8px}
.ca-tools button{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:rgba(255,255,255,0.6);font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:inherit;letter-spacing:0.04em}
.ca-tools button:hover{background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.88)}
</style>

<div class="ca-wrap">
  <div class="ca-h">Customer Activity Report</div>

  <form class="ca-form" id="ca-form" onsubmit="return runCAReport(event)">
    <div>
      <label>Start date</label>
      <input type="date" id="ca-start" required>
    </div>
    <div>
      <label>End date</label>
      <input type="date" id="ca-end" required>
    </div>
    <div style="grid-column:1/-1">
      <label>Customers</label>
      <div class="ca-customer-list" id="ca-customers">
        <div class="ca-bulk">
          <a onclick="caCustToggle(true)">all</a>
          <a onclick="caCustToggle(false)">none</a>
        </div>
        <label><input type="checkbox" value="owner" checked> owner only (Patrick)</label>
        <label><input type="checkbox" value="all"> ALL customers</label>
        <div id="ca-customer-checkboxes"><div style="font-size:10px;color:rgba(255,255,255,0.3);padding:4px">loading customer list...</div></div>
      </div>
    </div>
    <div class="ca-presets">
      <button type="button" onclick="caPreset('today')">Today</button>
      <button type="button" onclick="caPreset('yesterday')">Yesterday</button>
      <button type="button" onclick="caPreset('this_week')">This week</button>
      <button type="button" onclick="caPreset('last_week')">Last week</button>
      <button type="button" onclick="caPreset('mtd')">MTD</button>
      <button type="button" onclick="caPreset('since_baseline')">Since 2026-04-23</button>
    </div>
    <div style="grid-column:1/-1;display:flex;justify-content:flex-end">
      <button type="submit">Run report</button>
    </div>
  </form>

  <div id="ca-output">
    <div class="ca-empty">Pick a date range + customers above and click "Run report".</div>
  </div>
</div>

<script>
const SECRET_TOKEN = '{{ secret_token }}';

function ymd(d){
  const y=d.getFullYear(), m=String(d.getMonth()+1).padStart(2,'0'), dd=String(d.getDate()).padStart(2,'0');
  return y+'-'+m+'-'+dd;
}

function caPreset(kind){
  const today = new Date();
  let s, e;
  if (kind === 'today')         { s = today; e = today; }
  else if (kind === 'yesterday'){ s = new Date(today); s.setDate(today.getDate()-1); e = s; }
  else if (kind === 'this_week'){
    s = new Date(today);
    const dow = today.getDay() || 7; // Mon=1..Sun=7 → 7
    s.setDate(today.getDate() - (dow-1));
    e = today;
  } else if (kind === 'last_week'){
    e = new Date(today); const dow = today.getDay() || 7; e.setDate(today.getDate() - dow);
    s = new Date(e); s.setDate(e.getDate() - 6);
  } else if (kind === 'mtd'){
    s = new Date(today.getFullYear(), today.getMonth(), 1); e = today;
  } else if (kind === 'since_baseline'){
    s = new Date('2026-04-23'); e = today;
  }
  document.getElementById('ca-start').value = ymd(s);
  document.getElementById('ca-end').value   = ymd(e);
}

function caCustToggle(on){
  document.querySelectorAll('#ca-customer-checkboxes input').forEach(cb=>cb.checked=on);
}

async function loadCustomerList(){
  // Pull from existing billing endpoint to populate the customer multi-select.
  try {
    const r = await fetch('/api/proxy/billing/all-customers', {headers:{'X-Token':SECRET_TOKEN}});
    if (!r.ok) throw new Error('billing fetch '+r.status);
    const d = await r.json();
    const custs = (d.customers || []).filter(c => c.id);
    const el = document.getElementById('ca-customer-checkboxes');
    if (!custs.length) { el.innerHTML = '<div style="font-size:10px;color:rgba(255,255,255,0.3);padding:4px">no customers found</div>'; return; }
    el.innerHTML = custs.map(c => {
      const name = c.display_name || c.email || c.id.slice(0,8);
      return `<label><input type="checkbox" value="${c.id}"> ${name} <span style="color:rgba(255,255,255,0.3);margin-left:auto">${c.id.slice(0,8)}</span></label>`;
    }).join('');
  } catch(e) {
    document.getElementById('ca-customer-checkboxes').innerHTML =
      '<div style="font-size:10px;color:#ff4b6e;padding:4px">failed to load customer list ('+e+'). You can still use "owner" or "ALL".</div>';
  }
}

function fmtDollar(n, sign){
  if (n === null || n === undefined) return '—';
  const abs = Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  if (sign) return (n >= 0 ? '+' : '-') + '$' + abs;
  return '$' + abs;
}

function fmtPct(n){
  if (n === null || n === undefined) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function classOf(n){ return n > 0 ? 'ca-pos' : (n < 0 ? 'ca-neg' : 'ca-mut'); }

function renderCustomerBlock(cr){
  const a = cr.activity, p = cr.positions;
  const equity = (a.equity_start !== null && a.equity_end !== null)
    ? `<div class="ca-stat"><div class="ca-stat-label">Equity</div><div class="ca-stat-val ${classOf(a.equity_change||0)}">${fmtDollar(a.equity_start)} → ${fmtDollar(a.equity_end)}</div><div style="font-size:10px;color:rgba(255,255,255,0.4);margin-top:2px">${fmtDollar(a.equity_change, true)} (${fmtPct(a.equity_change_pct)})</div></div>`
    : `<div class="ca-stat"><div class="ca-stat-label">Equity</div><div class="ca-stat-val ca-mut">no snapshots</div></div>`;
  const stats = `
    ${equity}
    <div class="ca-stat"><div class="ca-stat-label">Trades opened</div><div class="ca-stat-val">${a.opened_count}</div><div style="font-size:10px;color:rgba(255,255,255,0.4)">${fmtDollar(a.opened_volume)}</div></div>
    <div class="ca-stat"><div class="ca-stat-label">Trades closed</div><div class="ca-stat-val">${a.closed_count}</div><div style="font-size:10px;color:rgba(255,255,255,0.4)">${a.wins}W / ${a.losses}L · ${a.win_rate_pct}%</div></div>
    <div class="ca-stat"><div class="ca-stat-label">Realized P&amp;L</div><div class="ca-stat-val ${classOf(a.realized_pnl)}">${fmtDollar(a.realized_pnl, true)}</div></div>
    <div class="ca-stat"><div class="ca-stat-label">Open at end</div><div class="ca-stat-val">${a.open_count}</div><div style="font-size:10px;color:rgba(255,255,255,0.4)">${fmtDollar(a.open_value)}</div></div>
  `;
  let closedTable = '';
  if (p.closed && p.closed.length) {
    const rows = p.closed.slice(0,30).map(t => `
      <tr>
        <td>${t.ticker}</td>
        <td style="color:rgba(255,255,255,0.5)">${t.sector || '—'}</td>
        <td style="text-align:right">${(t.shares||0).toFixed(2)}</td>
        <td style="text-align:right">${fmtDollar(t.entry)}</td>
        <td style="text-align:right">${fmtDollar(t.exit)}</td>
        <td style="text-align:right" class="${classOf(t.pnl)}">${fmtDollar(t.pnl, true)}</td>
        <td style="text-align:right" class="${classOf(t.pnl)}">${fmtPct(t.ret_pct)}</td>
        <td style="text-align:right">${t.days_held !== null ? t.days_held.toFixed(1) + 'd' : '—'}</td>
        <td style="color:rgba(255,255,255,0.5)">${t.exit_reason || '—'}</td>
      </tr>
    `).join('');
    closedTable = `
      <div class="ca-section-h">Closed trades (${p.closed.length})</div>
      <table class="ca-table">
        <thead><tr><th>Ticker</th><th>Sector</th><th style="text-align:right">Shares</th><th style="text-align:right">Entry</th><th style="text-align:right">Exit</th><th style="text-align:right">P&amp;L</th><th style="text-align:right">Return</th><th style="text-align:right">Held</th><th>Exit</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }
  let sectorTable = '';
  if (p.sectors && p.sectors.length) {
    sectorTable = `
      <div class="ca-section-h">Sectors traded</div>
      <table class="ca-table">
        <thead><tr><th>Sector</th><th style="text-align:right">Trades</th><th style="text-align:right">P&amp;L</th></tr></thead>
        <tbody>${p.sectors.map(s => `
          <tr><td>${s.sector}</td><td style="text-align:right">${s.trades}</td><td style="text-align:right" class="${classOf(s.pnl)}">${fmtDollar(s.pnl, true)}</td></tr>
        `).join('')}</tbody>
      </table>
    `;
  }
  return `
    <div class="ca-customer-block">
      <div class="ca-cust-h">
        <div class="ca-cust-name">${cr.display_name}</div>
        <div class="ca-cust-cid">${cr.customer_id.slice(0,8)}</div>
      </div>
      <div class="ca-stat-row">${stats}</div>
      ${closedTable}
      ${sectorTable}
    </div>
  `;
}

function renderAggregate(agg){
  if (!agg) return '';
  const ft = agg.fleet_totals;
  let clusterHtml = '';
  if (agg.signal_clusters && agg.signal_clusters.length) {
    clusterHtml = '<div class="ca-section-h">⚠ Signal clusters (≥3 customers traded)</div>' +
      agg.signal_clusters.map(t => `
        <div class="ca-cluster">
          <div class="ca-cluster-header">${t.ticker} — ${t.customers} customers, ${t.trades} trades</div>
          <div style="font-size:10px;color:rgba(255,255,255,0.5)">Net P&amp;L: <span class="${classOf(t.pnl)}">${fmtDollar(t.pnl, true)}</span></div>
        </div>
      `).join('');
  }
  let topTickers = '';
  if (agg.top_tickers && agg.top_tickers.length) {
    topTickers = `
      <div class="ca-section-h">Top tickers across fleet</div>
      <table class="ca-table">
        <thead><tr><th>Ticker</th><th style="text-align:right">Customers</th><th style="text-align:right">Trades</th><th style="text-align:right">P&amp;L</th></tr></thead>
        <tbody>${agg.top_tickers.slice(0,15).map(t => `
          <tr><td>${t.ticker}</td><td style="text-align:right">${t.customers}</td><td style="text-align:right">${t.trades}</td><td style="text-align:right" class="${classOf(t.pnl)}">${fmtDollar(t.pnl, true)}</td></tr>
        `).join('')}</tbody>
      </table>
    `;
  }
  let topSectors = '';
  if (agg.top_sectors && agg.top_sectors.length) {
    topSectors = `
      <div class="ca-section-h">Top sectors across fleet</div>
      <table class="ca-table">
        <thead><tr><th>Sector</th><th style="text-align:right">Customers</th><th style="text-align:right">Trades</th><th style="text-align:right">P&amp;L</th></tr></thead>
        <tbody>${agg.top_sectors.slice(0,10).map(s => `
          <tr><td>${s.sector}</td><td style="text-align:right">${s.customers}</td><td style="text-align:right">${s.trades}</td><td style="text-align:right" class="${classOf(s.pnl)}">${fmtDollar(s.pnl, true)}</td></tr>
        `).join('')}</tbody>
      </table>
    `;
  }
  return `
    <div class="ca-customer-block" style="border-color:rgba(0,245,212,0.2);background:rgba(0,245,212,0.02)">
      <div class="ca-cust-h"><div class="ca-cust-name" style="color:#00f5d4">Fleet aggregate</div></div>
      <div class="ca-stat-row">
        <div class="ca-stat"><div class="ca-stat-label">Active customers</div><div class="ca-stat-val">${ft.active_customers}</div></div>
        <div class="ca-stat"><div class="ca-stat-label">Trades opened</div><div class="ca-stat-val">${ft.trades_opened}</div></div>
        <div class="ca-stat"><div class="ca-stat-label">Trades closed</div><div class="ca-stat-val">${ft.trades_closed}</div></div>
        <div class="ca-stat"><div class="ca-stat-label">Realized P&amp;L</div><div class="ca-stat-val ${classOf(ft.realized_pnl)}">${fmtDollar(ft.realized_pnl, true)}</div></div>
      </div>
      ${clusterHtml}
      ${topTickers}
      ${topSectors}
    </div>
  `;
}

async function runCAReport(ev){
  ev.preventDefault();
  const start = document.getElementById('ca-start').value;
  const end   = document.getElementById('ca-end').value;
  if (!start || !end) return false;
  const checked = Array.from(document.querySelectorAll('#ca-customers input:checked')).map(cb=>cb.value);
  const body = {start, end, customer: checked.length ? checked : ['all']};

  const out = document.getElementById('ca-output');
  out.innerHTML = '<div class="ca-empty">Running report...</div>';

  try {
    const r = await fetch('/api/proxy/activity-report', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Token': SECRET_TOKEN},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || d.error) {
      out.innerHTML = '<div class="ca-empty" style="color:#ff4b6e">' + (d.error || ('HTTP ' + r.status)) + '</div>';
      return false;
    }
    let html = '<div class="ca-tools"><button onclick="caCopyJSON()">Copy JSON</button><button onclick="caDownloadCSV()">Download CSV</button></div>';
    html += '<div style="font-size:10px;color:rgba(255,255,255,0.4);margin-bottom:10px;font-family:JetBrains Mono,monospace">' +
            d.meta.start + ' → ' + d.meta.end + ' · ' + d.meta.customer_count + ' customers · generated ' + d.meta.generated_at + '</div>';
    html += renderAggregate(d.aggregate);
    html += (d.customers || []).map(renderCustomerBlock).join('');
    out.innerHTML = html;
    window._caLastReport = d;
  } catch(e) {
    out.innerHTML = '<div class="ca-empty" style="color:#ff4b6e">' + e + '</div>';
  }
  return false;
}

function caCopyJSON(){
  if (!window._caLastReport) return;
  navigator.clipboard.writeText(JSON.stringify(window._caLastReport, null, 2));
}

function caDownloadCSV(){
  const r = window._caLastReport;
  if (!r) return;
  const rows = [['customer','ticker','sector','shares','entry','exit','pnl','ret_pct','days_held','exit_reason','opened_at','closed_at']];
  (r.customers || []).forEach(cr => {
    (cr.positions.closed || []).forEach(t => {
      rows.push([cr.display_name, t.ticker, t.sector, t.shares, t.entry, t.exit, t.pnl, t.ret_pct, t.days_held, t.exit_reason, t.opened_at, t.closed_at]);
    });
  });
  const csv = rows.map(r => r.map(c => '"' + String(c == null ? '' : c).replace(/"/g, '""') + '"').join(',')).join('\\n');
  const blob = new Blob([csv], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'activity-' + r.meta.start + '_' + r.meta.end + '.csv';
  a.click();
}

// Default to today on first load
caPreset('today');
loadCustomerList();
</script>
"""


@app.route("/customer-activity")
def customer_activity_page():
    if not _authorized():
        return redirect(url_for("login"))
    return (_subpage_header('Customer Activity')
            + render_template_string(_CUSTOMER_ACTIVITY_TEMPLATE,
                                     secret_token=SECRET_TOKEN))


# ── PILL USAGE TELEMETRY (Phase G, 2026-04-27) ────────────────────────────────
# Operator-facing rollup of which drawer/screener pills users actually
# click on. Drives the "ship a generous pill set, prune by usage" plan.

@app.route("/api/proxy/pill-usage")
def proxy_pill_usage():
    """Forward a pill-usage request to retail portal. Auth converted
    from session-cookie (cmd portal) to Bearer SECRET_TOKEN."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    import requests as _req
    days = request.args.get("days", "7")
    try:
        r = _req.get(
            f"{RETAIL_PORTAL_URL}/api/admin/pill-usage",
            params={"days": days},
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
            timeout=15,
        )
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"error": f"retail returned non-JSON ({r.status_code})",
                            "body": r.text[:500]}), 502
    except Exception as e:
        return jsonify({"error": f"proxy failed: {e}"}), 502


_PILL_USAGE_TEMPLATE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.pu-wrap{max-width:1100px;margin:0 auto;padding:20px 24px}
.pu-h{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px}
.pu-controls{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.pu-controls button{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:rgba(255,255,255,0.6);font-size:11px;padding:6px 14px;border-radius:6px;cursor:pointer;font-family:inherit;letter-spacing:0.04em;text-transform:uppercase}
.pu-controls button:hover{background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.88)}
.pu-controls button.active{background:rgba(0,245,212,0.10);border-color:rgba(0,245,212,0.3);color:#00f5d4}
.pu-controls .pu-meta{font-size:11px;color:rgba(255,255,255,0.4);font-family:'JetBrains Mono',monospace;margin-left:auto}
.pu-totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.pu-stat{padding:14px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;text-align:center}
.pu-stat-val{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;color:#00f5d4}
.pu-stat-label{font-size:9px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:0.08em;margin-top:4px}
.pu-section{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.pu-section-h{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:rgba(255,255,255,0.55);margin-bottom:10px}
.pu-table{width:100%;border-collapse:collapse;font-size:12px;font-family:'JetBrains Mono',monospace}
.pu-table th{text-align:left;padding:6px 10px;border-bottom:1px solid rgba(255,255,255,0.1);font-weight:600;color:rgba(255,255,255,0.4);font-size:10px;text-transform:uppercase;letter-spacing:0.06em}
.pu-table td{padding:7px 10px;border-bottom:1px solid rgba(255,255,255,0.04);color:rgba(255,255,255,0.85)}
.pu-table tr:hover td{background:rgba(255,255,255,0.02)}
.pu-table td.pu-num{text-align:right;color:#00f5d4;font-weight:700}
.pu-table td.pu-num-mut{text-align:right;color:rgba(255,255,255,0.5)}
.pu-pill-tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;font-weight:700;letter-spacing:0.05em;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.10);color:rgba(255,255,255,0.7)}
.pu-empty{text-align:center;padding:30px;color:rgba(255,255,255,0.25);font-size:12px}
.pu-bar{display:inline-block;height:5px;background:linear-gradient(90deg,rgba(0,245,212,0.6),rgba(0,245,212,0.25));border-radius:99px;vertical-align:middle;margin-left:8px;min-width:4px}
</style>

<div class="pu-wrap">
  <div class="pu-h">Pill Usage Telemetry</div>

  <div class="pu-controls">
    <button data-days="1"  onclick="loadPillUsage(this)">1d</button>
    <button data-days="7"  onclick="loadPillUsage(this)" class="active">7d</button>
    <button data-days="30" onclick="loadPillUsage(this)">30d</button>
    <button data-days="90" onclick="loadPillUsage(this)">90d</button>
    <span class="pu-meta" id="pu-meta">Loading…</span>
  </div>

  <div class="pu-totals" id="pu-totals"></div>

  <div class="pu-section">
    <div class="pu-section-h">By Pill Type · ranked by clicks</div>
    <div id="pu-pills">Loading…</div>
  </div>

  <div class="pu-section">
    <div class="pu-section-h">By Drawer / Surface</div>
    <div id="pu-drawers"></div>
  </div>

  <div class="pu-section">
    <div class="pu-section-h">By Customer · top 50</div>
    <div id="pu-customers"></div>
  </div>
</div>

<script>
function _esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}

async function loadPillUsage(btn) {
  const days = btn ? btn.getAttribute('data-days') : '7';
  document.querySelectorAll('.pu-controls button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById('pu-meta').textContent = 'Loading…';

  try {
    const r = await fetch('/api/proxy/pill-usage?days=' + encodeURIComponent(days));
    const d = await r.json();
    if (d.error) {
      document.getElementById('pu-meta').textContent = 'Error: ' + d.error;
      return;
    }
    const tot = d.total || {n:0, users:0, pill_types:0};
    document.getElementById('pu-meta').textContent =
      'Window: ' + d.days + 'd · ' + tot.n + ' clicks · ' + tot.users + ' users · ' + tot.pill_types + ' pill types';

    document.getElementById('pu-totals').innerHTML =
      '<div class="pu-stat"><div class="pu-stat-val">' + tot.n + '</div><div class="pu-stat-label">Clicks</div></div>'
    + '<div class="pu-stat"><div class="pu-stat-val">' + tot.users + '</div><div class="pu-stat-label">Distinct users</div></div>'
    + '<div class="pu-stat"><div class="pu-stat-val">' + tot.pill_types + '</div><div class="pu-stat-label">Distinct pill types</div></div>'
    + '<div class="pu-stat"><div class="pu-stat-val">' + d.days + '</div><div class="pu-stat-label">Window (days)</div></div>';

    // By pill type
    const pills = d.by_pill_type || [];
    if (!pills.length) {
      document.getElementById('pu-pills').innerHTML = '<div class="pu-empty">No clicks yet in this window.</div>';
    } else {
      const maxClicks = pills[0].clicks || 1;
      let html = '<table class="pu-table"><thead><tr>'
        + '<th>Type</th><th>Label</th>'
        + '<th style="text-align:right">Clicks</th>'
        + '<th style="text-align:right">Users</th>'
        + '<th style="text-align:right">Tickers</th>'
        + '<th style="width:25%"></th></tr></thead><tbody>';
      pills.forEach(p => {
        const w = Math.round(80 * p.clicks / maxClicks);
        html += '<tr>'
          + '<td><span class="pu-pill-tag">' + _esc(p.pill_type) + '</span></td>'
          + '<td>' + _esc(p.pill_label || '—') + '</td>'
          + '<td class="pu-num">' + p.clicks + '</td>'
          + '<td class="pu-num-mut">' + p.distinct_users + '</td>'
          + '<td class="pu-num-mut">' + p.distinct_tickers + '</td>'
          + '<td><span class="pu-bar" style="width:' + w + '%"></span></td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      document.getElementById('pu-pills').innerHTML = html;
    }

    // By drawer
    const drawers = d.by_drawer || [];
    if (!drawers.length) {
      document.getElementById('pu-drawers').innerHTML = '<div class="pu-empty">No drawer data yet.</div>';
    } else {
      let html = '<table class="pu-table"><thead><tr>'
        + '<th>Surface</th>'
        + '<th style="text-align:right">Clicks</th>'
        + '<th style="text-align:right">Users</th></tr></thead><tbody>';
      drawers.forEach(d => {
        html += '<tr><td><span class="pu-pill-tag">' + _esc(d.drawer_kind) + '</span></td>'
              + '<td class="pu-num">' + d.clicks + '</td>'
              + '<td class="pu-num-mut">' + d.distinct_users + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('pu-drawers').innerHTML = html;
    }

    // By customer
    const custs = d.by_customer || [];
    if (!custs.length) {
      document.getElementById('pu-customers').innerHTML = '<div class="pu-empty">No customer data yet.</div>';
    } else {
      let html = '<table class="pu-table"><thead><tr>'
        + '<th>Customer ID</th>'
        + '<th style="text-align:right">Clicks</th>'
        + '<th style="text-align:right">Distinct pills</th></tr></thead><tbody>';
      custs.forEach(c => {
        html += '<tr><td>' + _esc((c.customer_id || '').slice(0, 36)) + '</td>'
              + '<td class="pu-num">' + c.clicks + '</td>'
              + '<td class="pu-num-mut">' + c.distinct_pills + '</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('pu-customers').innerHTML = html;
    }
  } catch (e) {
    document.getElementById('pu-meta').textContent = 'Failed: ' + e.message;
  }
}

loadPillUsage();
</script>
"""


@app.route("/pill-usage")
def pill_usage_page():
    if not _authorized():
        return redirect(url_for("login"))
    return _subpage_header('Pill Usage') + _PILL_USAGE_TEMPLATE


# ── CUSTOMER BILLING PAGE ─────────────────────────────────────────────────────

@app.route("/customer-billing")
def customer_billing_page():
    if not _authorized():
        return redirect(url_for("login"))
    return _subpage_header('Customer Billing') + """
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',system-ui,sans-serif;font-size:14px;min-height:100vh}::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}</style>
<div style="max-width:1000px;margin:0 auto;padding:20px 24px">
  <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:14px">All Customer Payment Status</div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:20px">
    <div style="background:rgba(0,245,212,0.04);border:1px solid rgba(0,245,212,0.12);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:20px;font-weight:700;color:#00f5d4">—</div>
      <div style="font-size:9px;color:rgba(255,255,255,0.35);text-transform:uppercase;letter-spacing:0.06em;margin-top:4px">Active</div>
    </div>
    <div style="background:rgba(245,166,35,0.04);border:1px solid rgba(245,166,35,0.12);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:20px;font-weight:700;color:#f5a623">—</div>
      <div style="font-size:9px;color:rgba(255,255,255,0.35);text-transform:uppercase;letter-spacing:0.06em;margin-top:4px">Past Due</div>
    </div>
    <div style="background:rgba(255,75,110,0.04);border:1px solid rgba(255,75,110,0.12);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:20px;font-weight:700;color:#ff4b6e">—</div>
      <div style="font-size:9px;color:rgba(255,255,255,0.35);text-transform:uppercase;letter-spacing:0.06em;margin-top:4px">Cancelled</div>
    </div>
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:20px;font-weight:700;color:rgba(255,255,255,0.5)">—</div>
      <div style="font-size:9px;color:rgba(255,255,255,0.35);text-transform:uppercase;letter-spacing:0.06em;margin-top:4px">MRR</div>
    </div>
  </div>

  <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:20px">
    <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px">Customer List</div>
    <div id="cb-list"><div style="text-align:center;padding:30px 0;color:rgba(255,255,255,0.2);font-size:12px">Loading...</div></div>
  </div>
</div>
<script>
async function loadCustBilling() {
  try {
    var r = await fetch('/api/proxy/billing/all-customers');
    var d = await r.json();
    // Summary cards
    var s = d.summary || {};
    var cards = document.querySelectorAll('[style*="text-align:center"] > div:first-child');
    // Update customer list
    var el = document.getElementById('cb-list');
    var custs = d.customers || [];
    if (!custs.length) { el.innerHTML = '<div style="text-align:center;padding:20px;color:rgba(255,255,255,0.2)">No customers</div>'; return; }
    var stColors = {active:'#00f5d4',trialing:'#7b61ff',past_due:'#f5a623',cancelled:'#ff4b6e',inactive:'rgba(255,255,255,0.35)'};
    el.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px">'
      + '<tr style="border-bottom:1px solid rgba(255,255,255,0.08);font-size:10px;color:rgba(255,255,255,0.35);text-transform:uppercase;letter-spacing:0.06em">'
      + '<th style="padding:8px;text-align:left">Customer</th>'
      + '<th style="padding:8px;text-align:left">Email</th>'
      + '<th style="padding:8px;text-align:center">Status</th>'
      + '<th style="padding:8px;text-align:center">Tier</th>'
      + '<th style="padding:8px;text-align:center">Alpaca</th>'
      + '<th style="padding:8px;text-align:center">Trading</th>'
      + '<th style="padding:8px;text-align:right">Since</th></tr>'
      + custs.map(function(c) {
        var st = c.subscription_status || 'inactive';
        var col = stColors[st] || stColors.inactive;
        return '<tr style="border-bottom:1px solid rgba(255,255,255,0.05)">'
          + '<td style="padding:8px">' + (c.name||'—') + '</td>'
          + '<td style="padding:8px;color:rgba(255,255,255,0.5);font-size:11px">' + (c.email||'—') + '</td>'
          + '<td style="padding:8px;text-align:center"><span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;background:' + col + '20;color:' + col + '">' + st + '</span></td>'
          + '<td style="padding:8px;text-align:center;font-size:11px;color:rgba(255,255,255,0.5)">' + (c.pricing_tier||'standard') + '</td>'
          + '<td style="padding:8px;text-align:center">' + (c.has_alpaca?'<span style="color:#00f5d4">\u2713</span>':'<span style="color:rgba(255,255,255,0.2)">\u2014</span>') + '</td>'
          + '<td style="padding:8px;text-align:center"><span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;cursor:pointer;' + ((c.trading_mode||'PAPER')==='LIVE' ? 'background:rgba(0,245,212,0.15);color:#00f5d4;border:1px solid rgba(0,245,212,0.3)' : 'background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.4);border:1px solid rgba(255,255,255,0.1)') + '" onclick="toggleTradingMode(&#39;' + c.id + '&#39;,&#39;' + (c.trading_mode||'PAPER') + '&#39;,&#39;' + (c.name||'').replace(/'/g,'') + '&#39;)">' + (c.trading_mode||'PAPER') + '</span></td>'
          + '<td style="padding:8px;text-align:right;font-size:10px;color:rgba(255,255,255,0.35)">' + (c.created_at||'').slice(0,10) + '</td></tr>';
      }).join('')
      + '</table>';
  } catch(e) { document.getElementById('cb-list').innerHTML = '<div style="color:#ff4b6e;text-align:center;padding:20px">Error loading billing data</div>'; }
}
async function toggleTradingMode(id, current, name) {
  var next = current === 'LIVE' ? 'PAPER' : 'LIVE';
  var warning = next === 'LIVE'
    ? 'Switch ' + name + ' to LIVE trading? Real money will be used.'
    : 'Switch ' + name + ' back to PAPER trading?';
  if (!confirm(warning)) return;
  try {
    var r = await fetch('/api/proxy/customer/' + id + '/trading-mode', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: next})
    });
    var d = await r.json();
    if (d.ok) { loadCustBilling(); }
    else { alert(d.error || 'Failed'); }
  } catch(e) { alert('Error: ' + e); }
}
loadCustBilling();
</script>
  </div>
</div>
"""



AUDIT_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Auditor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--pink:#ff4b6e;--purple:#7b61ff;--amber:#ffb347;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.header{position:sticky;top:0;z-index:100;background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
        border-bottom:1px solid var(--border);padding:0 24px;height:56px;
        display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal)}
.nav-back{color:var(--muted);font-size:11px;text-decoration:none;padding:5px 12px;
          border-radius:8px;border:1px solid var(--border);margin-left:auto;transition:all 0.15s}
.nav-back:hover{color:var(--text);border-color:var(--border2)}
.page{max-width:1200px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,var(--purple),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:12px;color:var(--muted);margin-bottom:20px}
.node-tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.node-tab{padding:6px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;
          color:var(--muted);font-size:11px;font-weight:600;cursor:pointer;transition:all 0.15s;font-family:var(--sans)}
.node-tab:hover{border-color:var(--border2);color:var(--text)}
.node-tab.active{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.3);color:var(--teal)}
.node-tab .tab-badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:99px;
                      font-size:9px;font-weight:700;background:rgba(255,75,110,0.15);color:var(--pink)}
.node-tab .tab-badge.clean{background:rgba(0,245,212,0.1);color:var(--teal)}
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat-mini{padding:14px 16px;border-radius:12px;border:1px solid var(--border);background:var(--surface)}
.sm-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.sm-val{font-size:26px;font-weight:800;letter-spacing:-1px}
.sm-sub{font-size:10px;color:var(--muted);margin-top:3px}
.two-col{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
.panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:16px}
.panel:last-child{margin-bottom:0}
.panel-header{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.panel-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);flex:1}
.panel-badge{padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;border:1px solid}
.pb-purple{background:rgba(123,97,255,0.08);border-color:rgba(123,97,255,0.25);color:var(--purple)}
.pb-teal{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.pb-amber{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.2);color:var(--amber)}
.pb-pink{background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.2);color:var(--pink)}
.panel-scroll{max-height:480px;overflow-y:auto}
.issue-row{padding:11px 16px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start}
.issue-row:last-child{border-bottom:none}
.sev-badge{flex-shrink:0;padding:2px 7px;border-radius:5px;font-size:9px;font-weight:800;
           letter-spacing:0.06em;text-transform:uppercase;margin-top:1px}
.sev-critical{background:rgba(255,75,110,0.12);color:var(--pink);border:1px solid rgba(255,75,110,0.25)}
.sev-high{background:rgba(255,179,71,0.12);color:var(--amber);border:1px solid rgba(255,179,71,0.25)}
.sev-medium{background:rgba(123,97,255,0.12);color:var(--purple);border:1px solid rgba(123,97,255,0.25)}
.sev-low{background:rgba(255,255,255,0.05);color:var(--muted);border:1px solid var(--border)}
.issue-body{flex:1;min-width:0}
.issue-file{font-size:10px;font-family:var(--mono);color:var(--purple);margin-bottom:3px}
.issue-ctx{font-size:11px;color:var(--text);line-height:1.5;word-break:break-all}
.issue-meta{font-size:9px;color:var(--dim);font-family:var(--mono);margin-top:4px}
.scan-row{padding:8px 16px;border-bottom:1px solid var(--border);font-size:10px;
          display:flex;gap:8px;align-items:center;font-family:var(--mono)}
.scan-row:last-child{border-bottom:none}
.scan-file{color:var(--text);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scan-age{color:var(--muted);flex-shrink:0}
.report-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:14px 16px}
.rg-cell{text-align:center}
.rg-val{font-size:22px;font-weight:800;letter-spacing:-1px}
.rg-lab{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--muted);margin-top:2px}
.empty{padding:32px;text-align:center;color:var(--muted);font-size:12px}
.empty-icon{font-size:28px;margin-bottom:10px}
.error-bar{padding:12px 16px;font-size:11px;color:var(--pink);background:rgba(255,75,110,0.06);border-bottom:1px solid rgba(255,75,110,0.15)}
.loading-bar{padding:12px 16px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border)}
.auditor-resolve-btn{flex-shrink:0;align-self:flex-start;padding:4px 10px;border-radius:6px;
  border:1px solid var(--border2);background:transparent;color:var(--muted);font-family:var(--sans);
  font-size:10px;font-weight:600;letter-spacing:0.04em;cursor:pointer;transition:all 0.15s}
.auditor-resolve-btn:hover{background:rgba(0,245,212,0.06);border-color:rgba(0,245,212,0.3);color:var(--teal)}
.auditor-resolve-btn:disabled{opacity:0.4;cursor:wait}
.issue-row.resolving{opacity:0.5}
</style>
</head>
<body>

{{ subpage_hdr|safe }}


<div class="page">
  <div class="title">Auditor &#x2014; <span>All Nodes</span></div>
  <div class="subtitle" id="page-sub">Loading nodes...</div>

  <div class="node-tabs" id="node-tabs">
    <button class="node-tab active" data-node="company" onclick="selectNode('company',this)">
      pi4b &#x2014; Company <span class="tab-badge" id="badge-company">&#x2014;</span>
    </button>
  </div>

  <div class="stats-row">
    <div class="stat-mini"><div class="sm-label">Critical</div><div class="sm-val" id="stat-crit" style="color:var(--pink)">&#x2014;</div><div class="sm-sub">Unresolved</div></div>
    <div class="stat-mini"><div class="sm-label">High</div><div class="sm-val" id="stat-high" style="color:var(--amber)">&#x2014;</div><div class="sm-sub">Unresolved</div></div>
    <div class="stat-mini"><div class="sm-label">Medium</div><div class="sm-val" id="stat-med" style="color:var(--purple)">&#x2014;</div><div class="sm-sub">Unresolved</div></div>
    <div class="stat-mini"><div class="sm-label">Total</div><div class="sm-val" id="stat-total" style="color:var(--text)">&#x2014;</div><div class="sm-sub">Unresolved</div></div>
  </div>

  <div class="two-col">
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Unresolved Issues</span>
          <span class="panel-badge pb-pink" id="issues-badge">Loading</span>
        </div>
        <div class="panel-scroll" id="issues-list">
          <div class="empty"><div class="empty-icon">&#x23F3;</div>Fetching findings...</div>
        </div>
      </div>
    </div>

    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Scan Coverage</span>
          <span class="panel-badge pb-teal" id="scan-badge">&#x2014;</span>
        </div>
        <div id="scan-list"><div class="empty" style="padding:20px">Loading...</div></div>
      </div>
      <div class="panel" id="report-panel">
        <div class="panel-header">
          <span class="panel-title">Last Morning Report</span>
          <span class="panel-badge pb-purple" id="report-badge">&#x2014;</span>
        </div>
        <div id="report-body"><div class="empty" style="padding:20px">No reports yet</div></div>
      </div>
    </div>
  </div>
</div>

<script>
const TOKEN = '{{ secret_token }}';
let currentNode = 'company';
let nodeCache = {};

function escHtml(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function ageSince(isoStr){
  if(!isoStr) return '—';
  const secs=Math.floor((Date.now()-new Date(isoStr).getTime())/1000);
  if(secs<60) return secs+'s ago';
  if(secs<3600) return Math.floor(secs/60)+'m ago';
  if(secs<86400) return Math.floor(secs/3600)+'h ago';
  return Math.floor(secs/86400)+'d ago';
}

function selectNode(node, el) {
  currentNode = node;
  document.querySelectorAll('.node-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('report-panel').style.display = node === 'company' ? '' : 'none';
  if (nodeCache[node]) render(nodeCache[node]);
  else loadNode(node);
}

async function loadNode(node) {
  if (node === currentNode)
    document.getElementById('issues-list').innerHTML = '<div class="loading-bar">Fetching ' + escHtml(node) + ' findings…</div>';
  try {
    const url = node === 'company' ? '/api/auditor' : '/api/audit/' + encodeURIComponent(node);
    const r = await fetch(url, {headers: {'X-Token': TOKEN}});
    const d = await r.json();
    nodeCache[node] = d;
    if (node === currentNode) render(d);
    const badge = document.getElementById('badge-' + CSS.escape(node));
    if (badge) {
      const total = d.total_unresolved != null ? d.total_unresolved : (d.issues ? d.issues.length : 0);
      badge.textContent = total || '✓';
      badge.className = 'tab-badge' + (total ? '' : ' clean');
    }
  } catch(e) {
    if (node === currentNode) {
      document.getElementById('page-sub').textContent = 'Could not reach ' + node;
      document.getElementById('issues-list').innerHTML =
        '<div class="empty"><div class="empty-icon">⚠</div>' + escHtml(e.message) + '</div>';
    }
  }
}

function render(d){
  const sev    = d.by_severity || {};
  const issues = d.issues || [];
  const total  = d.total_unresolved != null ? d.total_unresolved : issues.length;

  document.getElementById('page-sub').textContent =
    (d.error && !issues.length) ? 'Error: ' + d.error :
    total + ' unresolved issue' + (total!==1?'s':'') +
    (d.scan_state && d.scan_state.length ? ' · ' + d.scan_state.length + ' log files monitored' : '') +
    ' · refreshes every 60s';

  document.getElementById('stat-crit').textContent  = sev.critical || 0;
  document.getElementById('stat-high').textContent  = sev.high     || 0;
  document.getElementById('stat-med').textContent   = sev.medium   || 0;
  document.getElementById('stat-total').textContent = total;

  const issuesEl = document.getElementById('issues-list');
  const badge    = document.getElementById('issues-badge');
  if (d.error && !issues.length) {
    issuesEl.innerHTML = '<div class="error-bar">'+escHtml(d.error)+'</div>'
      + '<div class="empty">Check that the node is reachable and has run at least one scan.</div>';
    badge.textContent = 'Error'; badge.className = 'panel-badge pb-pink';
  } else if (!issues.length) {
    issuesEl.innerHTML = '<div class="empty"><div class="empty-icon">✓</div>No unresolved issues — node healthy</div>';
    badge.textContent = 'All clear'; badge.className = 'panel-badge pb-teal';
  } else {
    badge.textContent = total + ' issue' + (total!==1?'s':'');
    badge.className = 'panel-badge ' + (sev.critical?'pb-pink':sev.high?'pb-amber':'pb-purple');
    issuesEl.innerHTML = issues.map(iss => {
      const sc = 'sev-' + (iss.severity||'low');
      const hits = iss.hit_count > 1 ? ' <span style="color:var(--dim)">×'+iss.hit_count+'</span>' : '';
      // Only company-side issues have a DB id we can resolve. Per-Pi live
      // scans return synthetic ids that don't map to a stored row, so skip
      // the button there (iss.id will be present but meaningless).
      const canResolve = (currentNode === 'company') && iss.id != null;
      const resolveBtn = canResolve
        ? '<button class="auditor-resolve-btn" data-issid="'+CSS.escape(String(iss.id))+'" onclick="resolveAuditorIssue(this.dataset.issid,event)">Resolve</button>'
        : '';
      return '<div class="issue-row" data-issue-id="'+CSS.escape(String(iss.id || ''))+'">'
        + '<div class="sev-badge '+sc+'">'+escHtml(iss.severity)+'</div>'
        + '<div class="issue-body">'
          + '<div class="issue-file">'+escHtml(iss.source_file)+hits+'</div>'
          + '<div class="issue-ctx">'+escHtml(iss.context||'')+'</div>'
          + '<div class="issue-meta">first: '+ageSince(iss.first_seen)+' · last: '+ageSince(iss.last_seen)+'</div>'
        + '</div>'
        + resolveBtn
        + '</div>';
    }).join('');
  }

  const scanEl    = document.getElementById('scan-list');
  const scanBadge = document.getElementById('scan-badge');
  const scanState = d.scan_state || [];
  scanBadge.textContent = scanState.length + ' files';
  scanEl.innerHTML = !scanState.length
    ? '<div class="empty" style="padding:16px">No log files tracked yet</div>'
    : scanState.map(s => {
        const fname = s.log_file ? s.log_file.split('/').pop() : '?';
        const pct = s.file_size > 0 ? Math.round(s.last_offset / s.file_size * 100) : 100;
        return '<div class="scan-row">'
          + '<span class="scan-file">'+escHtml(fname)+'</span>'
          + '<span class="scan-age" style="color:'+(pct<100?'var(--amber)':'var(--teal)')+'">'+pct+'%</span>'
          + '<span class="scan-age">'+ageSince(s.last_scanned)+'</span>'
        + '</div>';
      }).join('');

  const rpt = d.morning_report;
  const rb = document.getElementById('report-badge');
  const rbody = document.getElementById('report-body');
  if (rb && rbody) {
    if (!rpt) {
      rb.textContent = 'None yet';
      rbody.innerHTML = '<div class="empty" style="padding:16px">Daily report generated at 6 AM ET</div>';
    } else {
      const last24 = rpt.last_24h || {};
      rb.textContent = rpt.date || '?';
      rb.className = 'panel-badge ' + (rpt.status==='healthy' ? 'pb-teal' : 'pb-pink');
      rbody.innerHTML = '<div class="report-grid">'
        + '<div class="rg-cell"><div class="rg-val" style="color:var(--pink)">'+(last24.critical&&last24.critical.unique!=null?last24.critical.unique:(last24.critical||0))+'</div><div class="rg-lab">Critical</div></div>'
        + '<div class="rg-cell"><div class="rg-val" style="color:var(--amber)">'+(last24.high&&last24.high.unique!=null?last24.high.unique:(last24.high||0))+'</div><div class="rg-lab">High</div></div>'
        + '<div class="rg-cell"><div class="rg-val" style="color:var(--purple)">'+(last24.medium&&last24.medium.unique!=null?last24.medium.unique:(last24.medium||0))+'</div><div class="rg-lab">Medium</div></div>'
        + '<div class="rg-cell"><div class="rg-val" style="color:var(--text)">'+(rpt.total_unresolved||0)+'</div><div class="rg-lab">Unresolved</div></div>'
      + '</div>';
    }
  }
}

async function buildNodeTabs() {
  try {
    const r = await fetch('/api/status', {headers:{}});
    const pis = await r.json();
    const tabsEl = document.getElementById('node-tabs');
    const skip = new Set(['pi4b-company','pi2w-monitor','pi2w-sentinel']);
    Object.entries(pis).forEach(([pi_id, pi]) => {
      if (skip.has(pi_id)) return;
      const label = pi.label || pi_id;
      const tab = document.createElement('button');
      tab.className = 'node-tab';
      tab.dataset.node = pi_id;
      tab.innerHTML = escHtml(label) + ' <span class="tab-badge" id="badge-' + CSS.escape(pi_id) + '">—</span>';
      tab.onclick = function(){ selectNode(pi_id, this); };
      tabsEl.appendChild(tab);
      loadNode(pi_id);
    });
  } catch(e) { console.warn('Could not build node tabs', e); }
}

async function resolveAuditorIssue(issueId, ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const idNum = parseInt(issueId, 10);
  if (!idNum) return;
  const btn = ev && ev.currentTarget;
  const row = document.querySelector('.issue-row[data-issue-id="'+issueId+'"]');
  if (btn) btn.disabled = true;
  if (row) row.classList.add('resolving');
  try {
    // Use session-cookie auth (the TOKEN placeholder on this page is not
    // substituted server-side). fetch default is credentials: 'same-origin'
    // which sends the session cookie; _session_authorized accepts that.
    const r = await fetch('/api/auditor/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ids: [idNum]}),
    });
    const d = await r.json().catch(() => ({ok: false, error: 'invalid JSON'}));
    if (!r.ok || !d.ok) {
      if (btn) btn.disabled = false;
      if (row) row.classList.remove('resolving');
      alert('Resolve failed: ' + (d.error || r.statusText || 'unknown'));
      return;
    }
    // Drop from local cache + re-render; also bust cache for currentNode
    if (nodeCache[currentNode] && Array.isArray(nodeCache[currentNode].issues)) {
      nodeCache[currentNode].issues = nodeCache[currentNode].issues.filter(i => i.id !== idNum);
      if (nodeCache[currentNode].total_unresolved != null)
        nodeCache[currentNode].total_unresolved = Math.max(0, nodeCache[currentNode].total_unresolved - 1);
      render(nodeCache[currentNode]);
    }
  } catch(e) {
    if (btn) btn.disabled = false;
    if (row) row.classList.remove('resolving');
    alert('Resolve failed: ' + e.message);
  }
}

buildNodeTabs();
loadNode('company');
setInterval(() => loadNode(currentNode), 15000);
</script>
</body>
</html>
"""


@app.route("/audit")
def audit_page():
    if not _authorized():
        return redirect(url_for("login"))
    return AUDIT_PAGE_HTML.replace("{{ subpage_hdr|safe }}", _subpage_header("Auditor"))


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    trim_pi_events()
    if not SECRET_TOKEN:
        print("[Synthos Monitor] ✗ FATAL: SECRET_TOKEN is not set in .env — refusing to start.")
        print("[Synthos Monitor]   Run install_monitor.py to generate one.")
        raise SystemExit(1)

    load_registry()   # restore Pi state from last run
    load_overrides()  # restore admin override state
    init_support_db()  # support/admin tools DB (separate from company.db)

    # Register digest agent blueprint
    try:
        from digest_agent import digest_bp
        app.register_blueprint(digest_bp)
        print(f"[Synthos Monitor] Digest agent registered — /digest endpoint active")
    except ImportError:
        print(f"[Synthos Monitor] digest_agent.py not found — /digest endpoint unavailable")

    t = threading.Thread(target=silence_detector, daemon=True)
    t.start()

    # ── Self-heartbeat: monitor node reports its own metrics to itself ─────────
    def _self_heartbeat_loop():
        """
        Post this monitor node's own system metrics to /heartbeat every 5 minutes.
        Allows the node roster to show pi2w_monitor_node's CPU/RAM/temp inline
        with all other nodes — no external agent needed.
        """
        self_pi_id    = os.getenv("PI_ID",    "pi2w-monitor")
        self_pi_label = os.getenv("PI_LABEL", "Monitor Node")
        self_url      = f"http://127.0.0.1:{PORT}/heartbeat"
        interval      = int(os.getenv("SELF_HB_INTERVAL", "60"))  # default 5 min

        time.sleep(10)  # let Flask finish starting
        while True:
            try:
                import psutil as _ps
                vm   = _ps.virtual_memory()
                du   = _ps.disk_usage('/')
                net  = _ps.net_io_counters()
                load = os.getloadavg()
                gb   = 1024 ** 3

                cpu_t = None
                try:
                    with open('/sys/class/thermal/thermal_zone0/temp') as _f:
                        cpu_t = round(int(_f.read().strip()) / 1000, 1)
                except Exception:
                    pass

                cached_bytes = getattr(vm, 'cached', 0) + getattr(vm, 'buffers', 0)

                payload = {
                    "pi_id":          self_pi_id,
                    "label":          self_pi_label,
                    "agents":         {"synthos_monitor": "active"},
                    "operating_mode": "MANAGED",
                    "trading_mode":   "PAPER",
                    "kill_switch":    False,
                    # CPU
                    "cpu_percent":    round(_ps.cpu_percent(interval=0.5), 1),
                    "cpu_count":      _ps.cpu_count(logical=True),
                    "load_avg":       [round(load[0],2), round(load[1],2), round(load[2],2)],
                    # RAM
                    "ram_percent":    round(vm.percent, 1),
                    "ram_total_gb":   round(vm.total     / gb, 2),
                    "ram_used_gb":    round(vm.used      / gb, 2),
                    "ram_avail_gb":   round(vm.available / gb, 2),
                    "ram_cached_gb":  round(cached_bytes / gb, 2),
                    # Disk
                    "disk_percent":   round(du.percent, 1),
                    "disk_total_gb":  round(du.total / gb, 1),
                    "disk_used_gb":   round(du.used  / gb, 1),
                    "disk_free_gb":   round(du.free  / gb, 1),
                    # Network
                    "net_bytes_sent": net.bytes_sent,
                    "net_bytes_recv": net.bytes_recv,
                    # Temp
                    "cpu_temp":       cpu_t,
                }
                import requests as _req
                _req.post(self_url, json=payload,
                          headers={"X-Token": SECRET_TOKEN}, timeout=5)
                print(f"[SelfHB] Posted — CPU {payload['cpu_percent']}%  "
                      f"RAM {payload['ram_percent']}%  Temp {cpu_t}°C")
            except Exception as _e:
                print(f"[SelfHB] Failed: {_e}")
            time.sleep(interval)

    sh = threading.Thread(target=_self_heartbeat_loop, daemon=True)
    sh.start()
    # ──────────────────────────────────────────────────────────────────────────

    print(f"[Synthos Monitor] Running on port {PORT}")
    print(f"[Synthos Monitor] Console at http://0.0.0.0:{PORT}/console")
    if COMPANY_URL:
        print(f"[Synthos Monitor] Scoop events → Company Node at {COMPANY_URL}")
    else:
        print(f"[Synthos Monitor] COMPANY_URL not set — enqueue events will not be persisted")
    print(f"[Synthos Monitor] Tracking {len(pi_registry)} Pi(s) from persistent state")
    app.run(host="0.0.0.0", port=PORT)
