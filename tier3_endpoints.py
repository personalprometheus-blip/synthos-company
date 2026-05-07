# Tier 3: Enhanced Alerts Center endpoints
# Handles alert filtering, search, deduplication, details, and bulk actions

import sqlite3
import time
import json
from datetime import datetime, timedelta

_AUDITOR_DB_PATH = "/home/pi/synthos-company/auditor.db"
_RETENTION_DAYS = 7

def _parse_query_params(request):
    """Extract and validate query parameters."""
    severity = request.args.get('severity', 'all').lower()
    allowed_sevs = {'critical', 'high', 'medium', 'low', 'all'}
    if severity not in allowed_sevs:
        severity = 'all'

    time_range = request.args.get('time_range', '24h')
    if time_range == '1h':
        hours = 1
    elif time_range == 'all':
        hours = _RETENTION_DAYS * 24
    else:
        hours = 24

    search = request.args.get('search', '').strip()
    deduplicate = request.args.get('deduplicate', '1') == '1'
    limit = int(request.args.get('limit', '500'))

    return {
        'severity': severity,
        'hours': hours,
        'search': search,
        'deduplicate': deduplicate,
        'limit': limit
    }

def _build_alert_query(params):
    """Build SQL query with filters."""
    cutoff_ts = time.time() - (params['hours'] * 3600)

    query = "SELECT id, pattern, source_file, severity, first_seen, last_seen, hit_count, context FROM detected_issues WHERE resolved = 0"
    values = []

    if params['severity'] != 'all':
        query += " AND severity = ?"
        values.append(params['severity'])

    if params['search']:
        search_term = f"%{params['search']}%"
        query += " AND (pattern LIKE ? OR source_file LIKE ? OR context LIKE ?)"
        values.extend([search_term, search_term, search_term])

    query += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, last_seen DESC LIMIT ?"
    values.append(params['limit'])

    return query, values

def _deduplicate_alerts(alerts):
    """Group alerts by pattern and count occurrences."""
    dedup_map = {}
    for alert in alerts:
        key = alert['pattern']
        if key not in dedup_map:
            dedup_map[key] = {
                'id': alert['id'],
                'pattern': alert['pattern'],
                'source_file': alert['source_file'],
                'severity': alert['severity'],
                'context': alert['context'],
                'first_seen': alert['first_seen'],
                'last_seen': alert['last_seen'],
                'hit_count': alert['hit_count'] or 1,
            }
        else:
            dedup_map[key]['hit_count'] += (alert['hit_count'] or 1)
            dedup_map[key]['last_seen'] = max(dedup_map[key]['last_seen'], alert['last_seen'])

    return list(dedup_map.values())

def _severity_order(sev):
    """Return sort order for severity."""
    order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    return order.get(sev, 99)

def api_alerts(request):
    """GET /api/alerts - Enhanced alert list with filtering and dedup."""
    try:
        params = _parse_query_params(request)
        query, values = _build_alert_query(params)

        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        alerts = [dict(row) for row in conn.execute(query, values).fetchall()]
        conn.close()

        if params['deduplicate']:
            alerts = _deduplicate_alerts(alerts)
        else:
            alerts.sort(key=lambda a: (_severity_order(a['severity']), -a['last_seen']))

        summary = {
            'total': len(alerts),
            'critical': sum(1 for a in alerts if a['severity'] == 'critical'),
            'high': sum(1 for a in alerts if a['severity'] == 'high'),
            'medium': sum(1 for a in alerts if a['severity'] == 'medium'),
            'low': sum(1 for a in alerts if a['severity'] == 'low'),
        }

        from flask import jsonify
        return jsonify({
            'alerts': alerts,
            'summary': summary,
            'time_range_hours': params['hours'],
            'deduplicated': params['deduplicate']
        }), 200
    except Exception as e:
        from flask import jsonify
        return jsonify({'error': str(e)}), 500

def api_alert_detail(request, alert_id):
    """GET /api/alerts/<id> - Full alert details with timeline."""
    try:
        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row

        alert = conn.execute(
            "SELECT id, pattern, source_file, severity, first_seen, last_seen, hit_count, context FROM detected_issues WHERE id = ? AND resolved = 0",
            [alert_id]
        ).fetchone()

        if not alert:
            from flask import jsonify
            conn.close()
            return jsonify({'error': 'Alert not found'}), 404

        alert_dict = dict(alert)

        from flask import jsonify
        return jsonify({
            'alert': alert_dict,
            'similar_count': alert_dict.get('hit_count', 1)
        }), 200
    except Exception as e:
        from flask import jsonify
        return jsonify({'error': str(e)}), 500

def api_alert_resolve(request, alert_id):
    """POST /api/alerts/<id>/resolve - Mark alert as resolved."""
    try:
        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)
        conn.execute(
            "UPDATE detected_issues SET resolved = 1 WHERE id = ?",
            [alert_id]
        )
        conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) as count FROM detected_issues WHERE resolved = 0"
        ).fetchone()[0]

        conn.close()

        from flask import jsonify
        return jsonify({'success': True, 'remaining_unresolved': remaining}), 200
    except Exception as e:
        from flask import jsonify
        return jsonify({'error': str(e)}), 500

def api_alerts_bulk_resolve(request):
    """POST /api/alerts/bulk-resolve - Resolve multiple alerts."""
    try:
        data = request.get_json()
        alert_ids = data.get('alert_ids', [])

        if not alert_ids:
            from flask import jsonify
            return jsonify({'error': 'No alert IDs provided'}), 400

        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)

        placeholders = ','.join('?' * len(alert_ids))
        conn.execute(
            f"UPDATE detected_issues SET resolved = 1 WHERE id IN ({placeholders})",
            alert_ids
        )
        conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) as count FROM detected_issues WHERE resolved = 0"
        ).fetchone()[0]

        conn.close()

        from flask import jsonify
        return jsonify({
            'success': True,
            'resolved_count': len(alert_ids),
            'remaining_unresolved': remaining
        }), 200
    except Exception as e:
        from flask import jsonify
        return jsonify({'error': str(e)}), 500
