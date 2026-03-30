#!/usr/bin/env python3
"""
create_user.py — Operator CLI for managing Synthos customer accounts.

Usage:
  python3 create_user.py --add --email user@example.com --password secret --node-url https://app.synth-cloud.com --node-id retail_1
  python3 create_user.py --list
  python3 create_user.py --deactivate --email user@example.com
  python3 create_user.py --reset-password --email user@example.com --password newpassword
"""

import os
import sys
import argparse
import sqlite3

from werkzeug.security import generate_password_hash

# Load company.env
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "company.env")
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/home/pi/synthos/synthos_build/data/company.db")


def get_db():
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    return db


def ensure_table(db):
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


def add_user(email, password, node_url, node_id):
    db = get_db()
    ensure_table(db)
    email = email.strip().lower()
    try:
        db.execute(
            "INSERT INTO users (email, password_hash, node_url, node_id) VALUES (?, ?, ?, ?)",
            (email, generate_password_hash(password), node_url.rstrip("/"), node_id)
        )
        db.commit()
        print(f"[OK] User created: {email} → {node_id} ({node_url})")
    except sqlite3.IntegrityError:
        print(f"[ERROR] User already exists: {email}")
    finally:
        db.close()


def list_users():
    db = get_db()
    ensure_table(db)
    rows = db.execute("SELECT id, email, node_id, node_url, active, created_at FROM users ORDER BY id").fetchall()
    db.close()
    if not rows:
        print("No users found.")
        return
    print(f"\n{'ID':<4} {'Email':<35} {'Node ID':<15} {'Active':<8} {'Created'}")
    print("-" * 90)
    for r in rows:
        status = "yes" if r["active"] else "no"
        print(f"{r['id']:<4} {r['email']:<35} {r['node_id']:<15} {status:<8} {r['created_at']}")
        print(f"     URL: {r['node_url']}")
    print()


def deactivate_user(email):
    db = get_db()
    ensure_table(db)
    email = email.strip().lower()
    c = db.execute("UPDATE users SET active = 0 WHERE email = ?", (email,))
    db.commit()
    db.close()
    if c.rowcount:
        print(f"[OK] Deactivated: {email}")
    else:
        print(f"[ERROR] User not found: {email}")


def reset_password(email, new_password):
    db = get_db()
    ensure_table(db)
    email = email.strip().lower()
    c = db.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (generate_password_hash(new_password), email)
    )
    db.commit()
    db.close()
    if c.rowcount:
        print(f"[OK] Password reset for: {email}")
    else:
        print(f"[ERROR] User not found: {email}")


def main():
    parser = argparse.ArgumentParser(description="Synthos user management CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add",            action="store_true", help="Add a new user")
    group.add_argument("--list",           action="store_true", help="List all users")
    group.add_argument("--deactivate",     action="store_true", help="Deactivate a user")
    group.add_argument("--reset-password", action="store_true", help="Reset a user's password")

    parser.add_argument("--email",    help="User email address")
    parser.add_argument("--password", help="User password")
    parser.add_argument("--node-url", help="Retail portal URL (e.g. https://app.synth-cloud.com)")
    parser.add_argument("--node-id",  help="Node identifier (e.g. retail_1)")

    args = parser.parse_args()

    if args.list:
        list_users()

    elif args.add:
        missing = [f for f, v in [("--email", args.email), ("--password", args.password),
                                   ("--node-url", args.node_url), ("--node-id", args.node_id)] if not v]
        if missing:
            print(f"[ERROR] Missing required args for --add: {', '.join(missing)}")
            sys.exit(1)
        add_user(args.email, args.password, args.node_url, args.node_id)

    elif args.deactivate:
        if not args.email:
            print("[ERROR] --email required")
            sys.exit(1)
        deactivate_user(args.email)

    elif args.reset_password:
        if not args.email or not args.password:
            print("[ERROR] --email and --password required")
            sys.exit(1)
        reset_password(args.email, args.password)


if __name__ == "__main__":
    main()
