#!/usr/bin/env python3
"""
create_user.py — Operator CLI for managing Synthos customer accounts.

Usage:
  # Add a user (no node required at creation)
  python3 create_user.py --add --email user@example.com --password secret

  # Add a node to an existing user
  python3 create_user.py --add-node --email user@example.com --node-url https://app.synth-cloud.com --node-id retail_1 [--nickname "Home Pi"]

  # List all users and their nodes
  python3 create_user.py --list

  # Deactivate a user account
  python3 create_user.py --deactivate --email user@example.com

  # Reset password
  python3 create_user.py --reset-password --email user@example.com --password newpassword

  # Remove a node from a user
  python3 create_user.py --remove-node --email user@example.com --node-id retail_1
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


def ensure_tables(db):
    # Check for old single-node schema and migrate if needed
    existing = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if existing:
        cols = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        if "node_url" in cols:
            print("[MIGRATE] Upgrading users table to multi-node schema...")
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
            print(f"[MIGRATE] Done. {len(old_rows)} user(s) migrated.")
            return

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            active        INTEGER DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
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


def add_user(email, password):
    db = get_db()
    ensure_tables(db)
    email = email.strip().lower()
    try:
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, generate_password_hash(password))
        )
        db.commit()
        print(f"[OK] User created: {email}")
        print(f"     Use --add-node to attach a Pi to this account.")
    except sqlite3.IntegrityError:
        print(f"[ERROR] User already exists: {email}")
    finally:
        db.close()


def add_node(email, node_url, node_id, nickname=""):
    db = get_db()
    ensure_tables(db)
    email = email.strip().lower()
    user = db.execute("SELECT id FROM users WHERE email = ? AND active = 1", (email,)).fetchone()
    if not user:
        print(f"[ERROR] User not found or inactive: {email}")
        db.close()
        return
    try:
        db.execute(
            "INSERT INTO nodes (user_id, node_id, node_url, nickname) VALUES (?, ?, ?, ?)",
            (user["id"], node_id, node_url.rstrip("/"), nickname)
        )
        db.commit()
        label = f" ('{nickname}')" if nickname else ""
        print(f"[OK] Node added: {node_id}{label} → {node_url} for {email}")
    except Exception as e:
        print(f"[ERROR] {e}")
    finally:
        db.close()


def remove_node(email, node_id):
    db = get_db()
    ensure_tables(db)
    email = email.strip().lower()
    user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not user:
        print(f"[ERROR] User not found: {email}")
        db.close()
        return
    c = db.execute(
        "UPDATE nodes SET active = 0 WHERE user_id = ? AND node_id = ?",
        (user["id"], node_id)
    )
    db.commit()
    db.close()
    if c.rowcount:
        print(f"[OK] Node removed: {node_id} from {email}")
    else:
        print(f"[ERROR] Node not found: {node_id} for {email}")


def list_users():
    db = get_db()
    ensure_tables(db)
    users = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    db.close()
    if not users:
        print("No users found.")
        return
    for u in users:
        status = "active" if u["active"] else "inactive"
        print(f"\n  [{u['id']}] {u['email']}  ({status})  created: {u['created_at']}")
        db2 = get_db()
        nodes = db2.execute(
            "SELECT * FROM nodes WHERE user_id = ? ORDER BY created_at", (u["id"],)
        ).fetchall()
        db2.close()
        if nodes:
            for n in nodes:
                nstatus = "active" if n["active"] else "inactive"
                nick = f" '{n['nickname']}'" if n["nickname"] else ""
                print(f"       node: {n['node_id']}{nick}  {nstatus}")
                print(f"        url: {n['node_url']}")
        else:
            print("       (no nodes attached)")
    print()


def deactivate_user(email):
    db = get_db()
    ensure_tables(db)
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
    ensure_tables(db)
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
    group.add_argument("--add-node",       action="store_true", help="Add a node to an existing user")
    group.add_argument("--remove-node",    action="store_true", help="Remove a node from a user")
    group.add_argument("--list",           action="store_true", help="List all users and their nodes")
    group.add_argument("--deactivate",     action="store_true", help="Deactivate a user account")
    group.add_argument("--reset-password", action="store_true", help="Reset a user's password")

    parser.add_argument("--email",    help="User email address")
    parser.add_argument("--password", help="User password")
    parser.add_argument("--node-url", help="Retail portal URL (e.g. https://app.synth-cloud.com)")
    parser.add_argument("--node-id",  help="Node identifier (e.g. retail_1)")
    parser.add_argument("--nickname", default="", help="Human-readable name for this node")

    args = parser.parse_args()

    if args.list:
        list_users()

    elif args.add:
        missing = [f for f, v in [("--email", args.email), ("--password", args.password)] if not v]
        if missing:
            print(f"[ERROR] Missing required args for --add: {', '.join(missing)}")
            sys.exit(1)
        add_user(args.email, args.password)

    elif args.add_node:
        missing = [f for f, v in [("--email", args.email), ("--node-url", args.node_url),
                                   ("--node-id", args.node_id)] if not v]
        if missing:
            print(f"[ERROR] Missing required args for --add-node: {', '.join(missing)}")
            sys.exit(1)
        add_node(args.email, args.node_url, args.node_id, args.nickname or "")

    elif args.remove_node:
        if not args.email or not args.node_id:
            print("[ERROR] --email and --node-id required")
            sys.exit(1)
        remove_node(args.email, args.node_id)

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
