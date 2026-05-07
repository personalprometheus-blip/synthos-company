# Tier 4: Operations & Queue Status endpoints
# Reads scoop_queue from company.db, backup log timestamps, system_architecture.json
# for scheduled runs, and env vars for system controls. Returns empty arrays
# when source data is missing — does NOT substitute fake values.

import sqlite3
import time
import json
import os
from datetime import datetime
from flask import jsonify

_COMPANY_DB_PATH = "/home/pi/synthos-company/data/company.db"
import os as _os_audit
_AUDITOR_DB_PATH = _os_audit.getenv("AUDITOR_DB_PATH", "/home/pi/synthos-company/data/auditor.db")
_SYSTEM_ARCH_PATH = "/home/pi/synthos/synthos_build/data/system_architecture.json"
_BACKUP_LOG_PATH = "/home/pi/synthos-company/logs/backup.log"


def api_queues_status(request):
    """GET /api/queues/status — Email queue depth + backup status."""
    result = {
        'email_queue': {'pending': 0, 'sent_today': 0, 'failed': 0},
        'backups': {'last_at': None, 'last_status': 'unknown', 'next_scheduled': None},
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }

    # Email queue (from company.db.scoop_queue) — failures here are non-fatal;
    # the panel just shows zeros if the DB or table is missing.
    try:
        conn = sqlite3.connect(_COMPANY_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM scoop_queue WHERE status='pending'").fetchone()
            result['email_queue']['pending'] = (row['c'] if row else 0) or 0

            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM scoop_queue WHERE status='sent' AND created_at > ?",
                [today_start]
            ).fetchone()
            result['email_queue']['sent_today'] = (row['c'] if row else 0) or 0

            row = conn.execute("SELECT COUNT(*) AS c FROM scoop_queue WHERE status='failed'").fetchone()
            result['email_queue']['failed'] = (row['c'] if row else 0) or 0
        finally:
            conn.close()
    except Exception as e:
        result['email_queue']['error'] = str(e)

    # Backup status from log mtime; unknown if no log exists.
    if os.path.exists(_BACKUP_LOG_PATH):
        try:
            mtime = os.path.getmtime(_BACKUP_LOG_PATH)
            result['backups']['last_at'] = datetime.utcfromtimestamp(mtime).isoformat() + 'Z'
            with open(_BACKUP_LOG_PATH, 'r') as f:
                last_line = ''
                for line in f:
                    if line.strip():
                        last_line = line.strip()
                lower = last_line.lower()
                if 'success' in lower or 'completed' in lower:
                    result['backups']['last_status'] = 'success'
                elif 'fail' in lower or 'error' in lower:
                    result['backups']['last_status'] = 'failed'
        except Exception as e:
            result['backups']['error'] = str(e)

    return jsonify(result), 200


def api_schedule_next_runs(request):
    """GET /api/schedule/next-runs — Upcoming enrichment + backup runs.

    Reads from data/system_architecture.json. Returns empty arrays if the
    JSON is missing or doesn't carry the expected fields — never fabricates.
    """
    result = {
        'enrichments': [],
        'backups': [],
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }

    if not os.path.exists(_SYSTEM_ARCH_PATH):
        result['source'] = 'missing'
        return jsonify(result), 200

    try:
        with open(_SYSTEM_ARCH_PATH, 'r') as f:
            arch = json.load(f)
    except Exception as e:
        result['error'] = f'failed to parse system_architecture.json: {e}'
        return jsonify(result), 200

    timeline = (arch.get('data_flow') or {}).get('daily_timeline') or []
    for event in timeline:
        agent = event.get('agent') or ''
        if 'enricher' in agent:
            result['enrichments'].append({
                'agent': agent,
                'scheduled_time': event.get('time'),
                'cadence': event.get('cadence', 'daily'),
                'type': 'enrichment',
            })

    backup = (arch.get('operations') or {}).get('backup_schedule')
    if backup:
        result['backups'].append({
            'name': backup.get('name', 'backup'),
            'scheduled_time': backup.get('time'),
            'cadence': backup.get('cadence', 'daily'),
            'destination': backup.get('destination'),
            'type': 'backup',
        })

    result['source'] = 'system_architecture.json'
    return jsonify(result), 200


def api_system_controls(request):
    """GET /api/system/controls — Current operating-mode flags.

    These reflect the env vars on the synthos_monitor process. The actual
    runtime state on pi5 (trading mode, kill switch) is controlled via the
    /api/command/* endpoints elsewhere; this is a snapshot of what the
    monitor process sees at boot.
    """
    return jsonify({
        'kill_switch': os.getenv('KILL_SWITCH', '0') == '1',
        'trading_mode': os.getenv('TRADING_MODE', 'PAPER'),
        'dispatch_mode': os.getenv('DISPATCH_MODE', 'distributed'),
        'operating_mode': os.getenv('OPERATING_MODE', 'normal'),
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }), 200
