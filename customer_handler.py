# Tier 5: Active Customers endpoint
# Returns customer roster with status and activity

import sqlite3
import time
from datetime import datetime

_COMPANY_DB_PATH = "/home/pi/synthos-company/data/company.db"

def api_customers_active(request):
    """GET /api/customers/active - Active customer list with status."""
    try:
        result = {
            'customers': [],
            'summary': {
                'total': 0,
                'online': 0,
                'trading': 0,
                'paused': 0
            },
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }

        conn = sqlite3.connect(_COMPANY_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row

        # Get customer list from customers table
        customers = conn.execute(
            "SELECT id, email, customer_name, status, last_heartbeat, created_at FROM customers WHERE status IN ('active', 'paused') ORDER BY last_heartbeat DESC LIMIT 100"
        ).fetchall()

        now = time.time()
        online_threshold = 3600  # 1 hour

        for cust in customers:
            last_activity = cust['last_heartbeat'] or 0
            age_sec = now - (last_activity or 0)
            is_online = age_sec < online_threshold

            customer_obj = {
                'id': cust['id'],
                'email': cust['email'],
                'name': cust['customer_name'] or 'N/A',
                'status': cust['status'],
                'last_activity_age': age_sec,
                'last_activity_str': _fmt_age(age_sec),
                'is_online': is_online,
                'created_at': cust['created_at']
            }

            result['customers'].append(customer_obj)

            result['summary']['total'] += 1
            if is_online:
                result['summary']['online'] += 1
            if cust['status'] == 'active':
                result['summary']['trading'] += 1
            elif cust['status'] == 'paused':
                result['summary']['paused'] += 1

        conn.close()

        from flask import jsonify
        return jsonify(result), 200
    except Exception as e:
        from flask import jsonify
        return jsonify({'error': str(e)}), 500

def _fmt_age(seconds):
    """Format age in seconds to human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    elif seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    else:
        return f"{int(seconds / 86400)}d ago"
