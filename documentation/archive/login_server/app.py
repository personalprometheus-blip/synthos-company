#!/usr/bin/env python3
"""
login_server/app.py — Synthos Customer Login Server
Customer-facing authentication gateway. Validates credentials, issues a
short-lived signed SSO token, and redirects the customer to their chosen
retail node portal.

Runs on company node (port 5050).
Customers hit: portal.synth-cloud.com → this app → <node_url>/sso?t=...
"""

import os
import sys
import sqlite3
import secrets
import logging
from datetime import timedelta

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, g, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("login_server")

# ── Config ────────────────────────────────────────────────────────────────────

_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "company.env")
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

SSO_SECRET     = os.environ.get("SSO_SECRET", "")
DATABASE_PATH  = os.environ.get("DATABASE_PATH", "/home/pi/synthos/synthos_build/data/company.db")
PORT           = int(os.environ.get("LOGIN_SERVER_PORT", 5050))
SESSION_SECRET = os.environ.get("LOGIN_SESSION_SECRET", secrets.token_hex(32))
SSO_TOKEN_TTL  = 900  # 15 minutes

if not SSO_SECRET:
    log.warning("SSO_SECRET not set — SSO tokens will not be verifiable by retail portals")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder='../static', static_url_path='/static')
app.secret_key = SESSION_SECRET
app.permanent_session_lifetime = timedelta(hours=12)

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_tables():
    """Create or migrate tables. Safe to call on every start."""
    try:
        db = sqlite3.connect(DATABASE_PATH)
        db.row_factory = sqlite3.Row

        # Check if users table exists and if it has the old schema (node_url column)
        existing = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()

        if existing:
            cols = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
            if "node_url" in cols:
                log.info("Migrating users table from single-node to multi-node schema")
                old_rows = db.execute(
                    "SELECT id, email, password_hash, node_url, node_id, active, created_at FROM users"
                ).fetchall()
                db.execute("ALTER TABLE users RENAME TO users_old")
                db.execute("""
                    CREATE TABLE users (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        email         TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        active        INTEGER DEFAULT 1,
                        created_at    TEXT DEFAULT (datetime('now'))
                    )
                """)
                for r in old_rows:
                    db.execute(
                        "INSERT INTO users (id, email, password_hash, active, created_at) VALUES (?, ?, ?, ?, ?)",
                        (r["id"], r["email"], r["password_hash"], r["active"], r["created_at"])
                    )
                db.execute("DROP TABLE users_old")
                db.commit()
                log.info(f"Migrated {len(old_rows)} user(s) — node data moved to nodes table below")

                # Seed nodes from migrated user data
                db.execute("""
                    CREATE TABLE IF NOT EXISTS nodes (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    INTEGER NOT NULL REFERENCES users(id),
                        node_id    TEXT NOT NULL,
                        node_url   TEXT NOT NULL,
                        nickname   TEXT DEFAULT '',
                        active     INTEGER DEFAULT 1,
                        created_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                for r in old_rows:
                    if r["node_url"] and r["node_id"]:
                        db.execute(
                            "INSERT INTO nodes (user_id, node_id, node_url) VALUES (?, ?, ?)",
                            (r["id"], r["node_id"], r["node_url"])
                        )
                db.commit()
                log.info("Node data seeded from old users table")
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    active        INTEGER DEFAULT 1,
                    created_at    TEXT DEFAULT (datetime('now'))
                )
            """)
            db.commit()

        db.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                node_id    TEXT NOT NULL,
                node_url   TEXT NOT NULL,
                nickname   TEXT DEFAULT '',
                active     INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()
        db.close()
        log.info("Tables ready")
    except Exception as e:
        log.error(f"Failed to init tables: {e}")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user():
    """Return user row from DB if logged in, else None."""
    email = session.get("user_email")
    if not email:
        return None
    try:
        return get_db().execute(
            "SELECT * FROM users WHERE email = ? AND active = 1", (email,)
        ).fetchone()
    except Exception:
        return None

def get_user_nodes(user_id):
    """Return all active nodes for a user."""
    return get_db().execute(
        "SELECT * FROM nodes WHERE user_id = ? AND active = 1 ORDER BY created_at",
        (user_id,)
    ).fetchall()

def make_sso_token(email: str) -> str:
    s = URLSafeTimedSerializer(SSO_SECRET)
    return s.dumps(email, salt="sso-login")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            error = "Email and password are required."
        else:
            try:
                user = get_db().execute(
                    "SELECT * FROM users WHERE email = ? AND active = 1", (email,)
                ).fetchone()
            except Exception as e:
                log.error(f"DB error on login: {e}")
                user = None

            if user and check_password_hash(user["password_hash"], password):
                session.permanent = True
                session["user_email"] = email
                log.info(f"Login: {email}")
                return redirect(url_for("dashboard"))
            else:
                error = "Incorrect email or password."
                log.warning(f"Failed login attempt: {email}")

    return render_template("login.html", error=error)


@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    nodes = get_user_nodes(user["id"])
    return render_template("dashboard.html", user=user, nodes=nodes)


@app.route("/launch")
def launch():
    """Issues SSO token and redirects customer to their chosen retail node portal."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    node_id = request.args.get("node", "").strip()
    if not node_id:
        return redirect(url_for("dashboard"))

    node = get_db().execute(
        "SELECT * FROM nodes WHERE user_id = ? AND node_id = ? AND active = 1",
        (user["id"], node_id)
    ).fetchone()

    if not node:
        log.warning(f"Launch failed: node '{node_id}' not found for {user['email']}")
        return redirect(url_for("dashboard"))

    if not SSO_SECRET:
        log.error("SSO_SECRET not configured — cannot issue token")
        nodes = get_user_nodes(user["id"])
        return render_template("dashboard.html", user=user, nodes=nodes,
                               error="Portal access is not configured yet. Contact support.")

    token = make_sso_token(user["email"])
    target = f"{node['node_url'].rstrip('/')}/sso?t={token}"
    log.info(f"SSO redirect: {user['email']} → {node['node_id']} ({node['node_url']})")
    return redirect(target)


@app.route("/rename", methods=["POST"])
def rename():
    """Update nickname for a node."""
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "not logged in"}), 401

    node_id  = request.form.get("node_id", "").strip()
    nickname = request.form.get("nickname", "").strip()

    if not node_id:
        return jsonify({"ok": False, "error": "missing node_id"}), 400

    c = get_db().execute(
        "UPDATE nodes SET nickname = ? WHERE user_id = ? AND node_id = ?",
        (nickname, user["id"], node_id)
    )
    get_db().commit()

    if c.rowcount:
        log.info(f"Renamed: {user['email']} → {node_id} = '{nickname}'")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "node not found"}), 404


@app.route("/logout")
def logout():
    email = session.get("user_email", "unknown")
    session.clear()
    log.info(f"Logout: {email}")
    return redirect(url_for("login"))


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_tables()
    log.info(f"Login server starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
