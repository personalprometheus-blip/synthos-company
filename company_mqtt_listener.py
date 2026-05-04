#!/usr/bin/env python3
"""
company_mqtt_listener.py — MQTT telemetry subscriber for the company node.

Created 2026-05-04 as Tier 4 of the distributed-trader migration.

Lives on the company node (pi4b). Subscribes to all telemetry topics
the process node publishes and records what it sees. This is ADDITIVE
to company_auditor.py (which still SSH-scans logs) — MQTT gives near-
real-time visibility, the SSH scanner gives forensic depth.

Topics subscribed:
    process/heartbeat/+/+   — every agent's liveness pulse
    process/regime          — current market regime (retained)
    process/prices/+        — live price updates per ticker

Outputs:
    1. Updates last_seen + last_payload in mqtt_observations table
       (created on first run if missing) in auditor.db
    2. Logs a one-line summary every minute showing topic counts +
       any agents that haven't pulsed in >2 minutes

Service shape:
    - systemd unit company-mqtt-listener.service (Type=simple, Restart=always)
    - Connects to MQTT_HOST/PORT/USER/PASS from environment
    - No CLI args — pure daemon

Failure modes:
    - Broker unreachable: paho auto-reconnects; we log and continue
    - DB write fails: log + continue (auditor.db can be rebuilt)
    - Receive bad payload: log warning + continue
"""

from __future__ import annotations
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

# ── PATH SETUP ────────────────────────────────────────────────────────────
# Make sure synthos_build/src is on sys.path so we can import mqtt_client.
# In production both repos live side-by-side under /home/<user>/.
_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "synthos" / "synthos_build" / "src",
                  _HERE / "agents",
                  _HERE):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# ── LOGGING ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mqtt_listener")

# ── ENVIRONMENT ───────────────────────────────────────────────────────────
MQTT_HOST = os.environ.get("MQTT_HOST", "10.0.0.11")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "synthos_broker")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# auditor.db is already maintained by company_auditor.py — we add a single
# new table to it (no schema collision).
AUDITOR_DB = os.environ.get(
    "AUDITOR_DB",
    str(_HERE / "data" / "auditor.db"),
)

SUMMARY_INTERVAL_S = 60   # log a summary line this often
HEARTBEAT_STALE_S  = 120  # warn if an agent hasn't pulsed in this long

# ── STATE ─────────────────────────────────────────────────────────────────
_state_lock = Lock()
_topic_counts: dict[str, int] = {}     # topic -> messages received this minute
_last_heartbeat: dict[str, float] = {} # agent_key -> epoch_ts
_stop_event = Event()


# ── DATABASE ──────────────────────────────────────────────────────────────

def _init_db() -> None:
    """Create the mqtt_observations table if it doesn't exist."""
    conn = sqlite3.connect(AUDITOR_DB)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mqtt_observations (
                topic         TEXT PRIMARY KEY,
                last_seen_ts  REAL NOT NULL,
                last_payload  TEXT,
                msg_count     INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mqtt_obs_last_seen
            ON mqtt_observations(last_seen_ts)
        """)
        conn.commit()
    finally:
        conn.close()


def _record_observation(topic: str, payload: bytes) -> None:
    """Upsert observation for a topic. Called from the MQTT callback thread —
    keep it fast, swallow errors so we never block the listener."""
    try:
        # Bound payload size in DB — long price streams + retained messages
        # can balloon if we store every byte.
        text = payload.decode("utf-8", errors="replace")[:2048]
        conn = sqlite3.connect(AUDITOR_DB, timeout=5.0)
        try:
            conn.execute("""
                INSERT INTO mqtt_observations (topic, last_seen_ts, last_payload, msg_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(topic) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    last_payload = excluded.last_payload,
                    msg_count    = mqtt_observations.msg_count + 1
            """, (topic, time.time(), text))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"DB record failed for {topic}: {e}")


# ── MESSAGE HANDLERS ──────────────────────────────────────────────────────

def _on_message(topic: str, payload: bytes) -> None:
    """Single dispatch for every subscribed topic. Categorize, count,
    persist, and (for heartbeats) update the in-memory liveness map."""
    with _state_lock:
        _topic_counts[topic] = _topic_counts.get(topic, 0) + 1
        # Heartbeats: process/heartbeat/<node>/<agent>
        parts = topic.split("/")
        if len(parts) == 4 and parts[0] == "process" and parts[1] == "heartbeat":
            agent_key = f"{parts[2]}/{parts[3]}"
            _last_heartbeat[agent_key] = time.time()
    _record_observation(topic, payload)


# ── PERIODIC SUMMARY ──────────────────────────────────────────────────────

def _summary_loop() -> None:
    """Background thread: every SUMMARY_INTERVAL_S, log a one-line summary
    of message counts + any stale heartbeats."""
    while not _stop_event.is_set():
        if _stop_event.wait(SUMMARY_INTERVAL_S):
            return
        with _state_lock:
            counts = dict(_topic_counts)
            _topic_counts.clear()
            now = time.time()
            stale = sorted(
                f"{k}({int(now - ts)}s)"
                for k, ts in _last_heartbeat.items()
                if now - ts > HEARTBEAT_STALE_S
            )
        # Aggregate counts by topic prefix for compact log
        agg = {"heartbeat": 0, "regime": 0, "prices": 0, "other": 0}
        for topic, n in counts.items():
            if topic.startswith("process/heartbeat/"):
                agg["heartbeat"] += n
            elif topic == "process/regime":
                agg["regime"] += n
            elif topic.startswith("process/prices/"):
                agg["prices"] += n
            else:
                agg["other"] += n
        msg = (
            f"summary {SUMMARY_INTERVAL_S}s window: "
            f"heartbeats={agg['heartbeat']} regime={agg['regime']} "
            f"prices={agg['prices']} other={agg['other']}"
        )
        if stale:
            msg += f" | STALE: {', '.join(stale)}"
        log.info(msg)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────

def _install_signal_handlers() -> None:
    def _shutdown(signum, frame):
        log.info(f"received signal {signum} — shutting down")
        _stop_event.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


def main() -> int:
    log.info(
        f"company_mqtt_listener starting — broker={MQTT_HOST}:{MQTT_PORT} "
        f"user={MQTT_USER} db={AUDITOR_DB}"
    )
    Path(AUDITOR_DB).parent.mkdir(parents=True, exist_ok=True)
    _init_db()
    _install_signal_handlers()

    try:
        from mqtt_client import MqttClient
    except ImportError as e:
        log.error(f"cannot import mqtt_client (synthos repo not on path): {e}")
        return 2

    mqtt = MqttClient(
        host=MQTT_HOST, port=MQTT_PORT,
        username=MQTT_USER, password=MQTT_PASS,
        client_id=f"company_mqtt_listener-{os.getpid()}",
        last_will_topic="process/heartbeat/company/mqtt_listener",
        last_will_payload="offline",
    )
    if not mqtt.connect():
        log.error("MQTT connect failed — exiting (systemd will restart us)")
        return 1

    # Subscribe to wildcard topics. Order: heartbeats first (most useful),
    # regime (small / retained), prices (volume).
    for topic in (
        "process/heartbeat/+/+",
        "process/regime",
        "process/prices/+",
    ):
        if mqtt.subscribe(topic, _on_message, qos=0):
            log.info(f"subscribed: {topic}")
        else:
            log.warning(f"subscribe failed: {topic}")

    # Start summary thread
    import threading
    summary_thread = threading.Thread(
        target=_summary_loop, name="summary", daemon=True,
    )
    summary_thread.start()

    # Park until shutdown signal
    log.info("listener active — Ctrl-C or SIGTERM to stop")
    while not _stop_event.wait(1.0):
        pass

    log.info("disconnecting MQTT")
    mqtt.disconnect()
    return 0


if __name__ == "__main__":
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("mqtt_listener", long_running=True)
    except Exception as _hb_e:
        pass
    sys.exit(main())
