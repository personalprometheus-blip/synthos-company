#!/usr/bin/env python3
"""
login_server/app.py — Synthos Customer Login Server
Customer-facing authentication gateway. Validates credentials, issues a
short-lived signed SSO token, and redirects the customer to their assigned
retail node portal.

Runs on company node (port 5050).
Customers hit: portal.synth-cloud.com → this app → app.synth-cloud.com/sso?t=...
"""

import os
import sys
import sqlite3
import secrets
import logging
from datetime import timedelta

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, g, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("login_server")

# ── Config ────────────────────────────────────────────────────────────────────

# Load company.env if present
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "company.env")
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

SSO_SECRET       = os.environ.get("SSO_SECRET", "")
DATABASE_PATH    = os.environ.get("DATABASE_PATH", "/home/pi/synthos/synthos_build/data/company.db")
PORT             = int(os.environ.get("LOGIN_SERVER_PORT", 5050))
SESSION_SECRET   = os.environ.get("LOGIN_SESSION_SECRET", secrets.token_hex(32))
SSO_TOKEN_TTL    = 900  # 15 minutes

if not SSO_SECRET:
    log.warning("SSO_SECRET not set — SSO tokens will not be verifiable by the retail portal")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
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

def init_users_table():
    """Create users table if it doesn't exist. Safe to call on every start."""
    try:
        db = sqlite3.connect(DATABASE_PATH)
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                node_url     TEXT NOT NULL,
                node_id      TEXT NOT NULL,
                active       INTEGER DEFAULT 1,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()
        db.close()
        log.info("users table ready")
    except Exception as e:
        log.error(f"Failed to init users table: {e}")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user():
    """Return user row from DB if logged in, else None."""
    email = session.get("user_email")
    if not email:
        return None
    try:
        row = get_db().execute(
            "SELECT * FROM users WHERE email = ? AND active = 1", (email,)
        ).fetchone()
        return row
    except Exception:
        return None

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
                log.info(f"Login: {email} → {user['node_id']}")
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
    return render_template("dashboard.html", user=user)


@app.route("/launch")
def launch():
    """Issues SSO token and redirects customer to their retail node portal."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    if not SSO_SECRET:
        log.error("SSO_SECRET not configured — cannot issue token")
        return render_template("dashboard.html", user=user,
                               error="Portal access is not configured yet. Contact support.")

    token = make_sso_token(user["email"])
    target = f"{user['node_url'].rstrip('/')}/sso?t={token}"
    log.info(f"SSO redirect: {user['email']} → {user['node_id']}")
    return redirect(target)


@app.route("/logout")
def logout():
    email = session.get("user_email", "unknown")
    session.clear()
    log.info(f"Logout: {email}")
    return redirect(url_for("login"))


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_users_table()
    log.info(f"Login server starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
