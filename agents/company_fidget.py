"""
fidget.py — Fidget v2.0 · API Cost Monitor
Synthos Company Pi

Role:
  Track API usage and costs across all agents. Detect anomalies.
  Report to company finances dashboard. Flag spikes.

Data sources:
  - company.db api_usage table
  - company.db token_ledger table

Schedule:
  Hourly:   quick anomaly check + write latest report
  Daily:    full report at 6:30am ET
  Monthly:  rollup on 1st of month

USAGE:
  python3 company_fidget.py --mode hourly
  python3 company_fidget.py --mode daily
  python3 company_fidget.py --mode monthly
  python3 company_fidget.py --status
"""

import json
import os
import sys
import signal
import sqlite3
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────

_HERE    = Path(__file__).resolve().parent
BASE_DIR = _HERE.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH  = DATA_DIR / "company.db"

load_dotenv(BASE_DIR / "company.env", override=True)

# Anthropic pricing (per million tokens)
PRICING = {
    "sonnet":  {"input": 3.00,  "output": 15.00},
    "haiku":   {"input": 0.25,  "output": 1.25},
    "opus":    {"input": 15.00, "output": 75.00},
    "default": {"input": 3.00,  "output": 15.00},
}

# Thresholds
SPIKE_MULTIPLIER    = 10.0
DAILY_COST_WARN     = 5.00
DAILY_COST_CRITICAL = 20.00
MONTHLY_COST_WARN   = 50.00

REPORT_FILE = DATA_DIR / "fidget_latest.json"

VERSION = "2.0"

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s fidget: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOGS_DIR / "fidget.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("fidget")


# ── DATABASE ──────────────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)

def _today():
    return _now().strftime("%Y-%m-%d")

def _month():
    return _now().strftime("%Y-%m")


# ── COST CALCULATION ──────────────────────────────────────────────────────────

def estimate_cost(tokens_input, tokens_output, model="sonnet"):
    model = model.lower()
    rates = PRICING.get(model, PRICING["default"])
    for key in PRICING:
        if key in model:
            rates = PRICING[key]
            break
    return round(
        tokens_input * rates["input"] / 1_000_000 +
        tokens_output * rates["output"] / 1_000_000, 6
    )


# ── QUERIES ───────────────────────────────────────────────────────────────────

def get_daily_usage(date=None):
    """API usage grouped by agent for a given date."""
    date = date or _today()
    with _db() as conn:
        rows = conn.execute("""
            SELECT agent_name,
                   SUM(token_count) as total_tokens,
                   SUM(call_count) as total_calls,
                   SUM(cost_estimate) as total_cost
            FROM api_usage
            WHERE DATE(timestamp) = ?
            GROUP BY agent_name
            ORDER BY total_cost DESC
        """, (date,)).fetchall()
        return [dict(r) for r in rows]


def get_monthly_usage(month=None):
    """Token ledger summary for a month."""
    month = month or _month()
    with _db() as conn:
        rows = conn.execute("""
            SELECT agent_name,
                   SUM(tokens_used) as total_tokens,
                   SUM(tokens_input) as total_input,
                   SUM(tokens_output) as total_output,
                   SUM(cost_estimate) as total_cost,
                   COUNT(*) as total_calls
            FROM token_ledger
            WHERE month = ?
            GROUP BY agent_name
            ORDER BY total_cost DESC
        """, (month,)).fetchall()
        return [dict(r) for r in rows]


def get_rolling_avg(agent_name, days=7):
    """Rolling daily average tokens for an agent."""
    cutoff = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _db() as conn:
        row = conn.execute("""
            SELECT AVG(daily_total) as avg_tokens FROM (
                SELECT DATE(timestamp) as day, SUM(token_count) as daily_total
                FROM api_usage
                WHERE agent_name = ? AND DATE(timestamp) >= ?
                GROUP BY DATE(timestamp)
            )
        """, (agent_name, cutoff)).fetchone()
        return row["avg_tokens"] or 0


def get_total_daily_cost(date=None):
    date = date or _today()
    with _db() as conn:
        row = conn.execute(
            "SELECT SUM(cost_estimate) as total FROM api_usage WHERE DATE(timestamp)=?",
            (date,)
        ).fetchone()
        return row["total"] or 0.0


def get_total_monthly_cost(month=None):
    month = month or _month()
    with _db() as conn:
        row = conn.execute(
            "SELECT SUM(cost_estimate) as total FROM token_ledger WHERE month=?",
            (month,)
        ).fetchone()
        return row["total"] or 0.0


# ── ANOMALY DETECTION ─────────────────────────────────────────────────────────

def detect_anomalies():
    today = _today()
    anomalies = []
    for row in get_daily_usage(today):
        agent = row["agent_name"]
        tokens = row["total_tokens"] or 0
        cost = row["total_cost"] or 0.0
        avg = get_rolling_avg(agent)
        if avg > 0 and tokens > avg * SPIKE_MULTIPLIER:
            anomalies.append({
                "agent": agent,
                "today": tokens,
                "avg_7d": round(avg),
                "multiplier": round(tokens / avg, 1),
                "cost_today": round(cost, 4),
                "severity": "CRITICAL" if tokens > avg * 20 else "HIGH",
            })
    return anomalies


# ── REPORTING ─────────────────────────────────────────────────────────────────

def run_hourly():
    anomalies = detect_anomalies()
    daily_cost = get_total_daily_cost()

    result = {
        "mode": "hourly",
        "version": VERSION,
        "timestamp": _now().isoformat(),
        "daily_cost": round(daily_cost, 4),
        "anomalies": anomalies,
        "cost_alert": "CRITICAL" if daily_cost >= DAILY_COST_CRITICAL
                      else "WARN" if daily_cost >= DAILY_COST_WARN
                      else None,
    }

    for a in anomalies:
        log.warning(f"ANOMALY: {a['agent']} — {a['today']:,} tokens ({a['multiplier']}x avg)")

    REPORT_FILE.write_text(json.dumps(result, indent=2))
    log.info(f"Hourly: daily_cost=${daily_cost:.4f} anomalies={len(anomalies)}")
    return result


def run_daily():
    today = _today()
    month = _month()
    daily = get_daily_usage(today)
    monthly = get_monthly_usage(month)
    anomalies = detect_anomalies()

    daily_cost = sum(r["total_cost"] or 0 for r in daily)
    monthly_cost = sum(r["total_cost"] or 0 for r in monthly)
    day_of_month = max(_now().day, 1)

    result = {
        "mode": "daily",
        "version": VERSION,
        "timestamp": _now().isoformat(),
        "date": today,
        "month": month,
        "daily_cost": round(daily_cost, 4),
        "monthly_cost": round(monthly_cost, 4),
        "monthly_projection": round(monthly_cost / day_of_month * 30, 2),
        "daily_by_agent": daily,
        "monthly_by_agent": monthly,
        "anomalies": anomalies,
    }

    REPORT_FILE.write_text(json.dumps(result, indent=2))
    log.info(f"Daily: today=${daily_cost:.4f} month=${monthly_cost:.4f} "
             f"projected=${result['monthly_projection']:.2f}")
    return result


def run_monthly():
    prev = (_now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    monthly = get_monthly_usage(prev)
    total = sum(r["total_cost"] or 0 for r in monthly)

    result = {
        "mode": "monthly",
        "version": VERSION,
        "timestamp": _now().isoformat(),
        "month": prev,
        "total_cost": round(total, 4),
        "by_agent": monthly,
    }

    log.info(f"Monthly rollup {prev}: ${total:.4f}")
    if total > MONTHLY_COST_WARN:
        log.warning(f"Monthly cost ${total:.2f} exceeds ${MONTHLY_COST_WARN} threshold")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status():
    today = _today()
    month = _month()
    daily = get_daily_usage(today)
    monthly = get_monthly_usage(month)
    anomalies = detect_anomalies()

    daily_cost = sum(r["total_cost"] or 0 for r in daily)
    monthly_cost = sum(r["total_cost"] or 0 for r in monthly)
    projected = round(monthly_cost / max(_now().day, 1) * 30, 2)

    print(f"\n{'='*60}")
    print(f"FIDGET v{VERSION} — {today}")
    print(f"{'='*60}")
    print(f"  Today:      ${daily_cost:.4f}")
    print(f"  This month: ${monthly_cost:.4f}  (projected: ${projected:.2f})")
    if anomalies:
        print(f"  ANOMALIES:  {len(anomalies)}")
        for a in anomalies:
            print(f"    ! {a['agent']}: {a['multiplier']}x normal")
    else:
        print(f"  No anomalies")
    if daily:
        print(f"\n  BY AGENT (today):")
        for r in daily[:10]:
            tokens = r['total_tokens'] or 0
            cost = r['total_cost'] or 0
            print(f"    {r['agent_name']:20} {tokens:>8,} tokens  ${cost:.4f}")
    else:
        print(f"\n  No API calls recorded today")
    print(f"{'='*60}\n")


# ── SHUTDOWN ──────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    log.info(f"Fidget received signal {sig}")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fidget v2 — API Cost Monitor")
    parser.add_argument("--mode", choices=["hourly", "daily", "monthly"],
                        default="hourly")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.mode == "hourly":
        run_hourly()
    elif args.mode == "daily":
        run_daily()
    elif args.mode == "monthly":
        run_monthly()
