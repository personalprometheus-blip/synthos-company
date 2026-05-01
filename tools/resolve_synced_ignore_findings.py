#!/usr/bin/env python3
"""
resolve_synced_ignore_findings.py — One-shot cleanup after syncing
company_auditor.IGNORE_PATTERNS with pi5's retail_portal /api/logs-audit
ignore list.

Before this sync, the pi4b auditor was intaking known-noise lines
(price_poller Alpaca fetch failed, watchdog interrogation not running,
[HB] POST failed, trader-timeout follow-ups, etc.) because they only
had IGNORE coverage on the pi5 portal side, not in company_auditor.py.
The 24h auto-resolve rule never caught up because the conditions kept
re-firing every screener/poller cycle.

This script walks the open detected_issues, runs each context against
the (now-correct) IGNORE_PATTERNS, and marks resolved=1 for matches.
After this runs, the auditor backlog will be clean and the patched
filter will keep new noise out at intake.

Idempotent — safe to re-run. Run once after the auditor.py change
deploys; future scans handle their own filtering.
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone

# Make the company_auditor module importable for IGNORE_PATTERNS.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..'))

from company_auditor import IGNORE_PATTERNS, DB_PATH


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f'ERROR: auditor DB not found at {DB_PATH}', file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat()
    resolved = 0
    inspected = 0

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source_file, severity, context FROM detected_issues "
            "WHERE resolved = 0"
        ).fetchall()

        to_resolve = []
        for r in rows:
            inspected += 1
            ctx = r['context'] or ''
            if any(p.search(ctx) for p in IGNORE_PATTERNS):
                to_resolve.append((r['id'], r['source_file'], r['severity'],
                                    ctx[:80]))

        for issue_id, _, _, _ in to_resolve:
            conn.execute(
                "UPDATE detected_issues SET resolved = 1, last_seen = ? "
                "WHERE id = ?",
                (now, issue_id),
            )
            resolved += 1
        conn.commit()

    print(f'Inspected {inspected} open finding(s)')
    print(f'Resolved {resolved} matching the synced IGNORE patterns')
    if to_resolve:
        print()
        print('Resolved findings:')
        for issue_id, src, sev, ctx in to_resolve:
            print(f'  #{issue_id:>4}  [{sev}]  {src}  {ctx}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
