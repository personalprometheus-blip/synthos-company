#!/usr/bin/env python3
"""
market_state_aggregator.py — Agent 9
Master market-state aggregator. 26-gate deterministic, rule-based top-level
integration layer. Accepts outputs from up to 7 upstream agents, maps each
into a common state vocabulary, scores and weights each component, computes
a composite market-state signal, and emits a final market-state classification
with a downstream route for the trade logic agent.

Benchmark context: S&P 500 (SPX).

Gates 1-2   : System validation and upstream availability.
Gates 3-9   : Benchmark anchor and input mapping from each upstream agent.
Gates 10-12 : Directional, informational, and benchmark consistency alignment.
Gates 13-15 : Composite component scoring, weighting, and aggregate score.
Gates 16-17 : Confidence and divergence warnings.
Gates 18-21 : Market regime classification, overrides, action classification,
              and risk discounts.
Gates 22-26 : Temporal persistence, evaluation loop, output controls,
              final signal, and downstream routing.
"""

import sys
import os
import json
import logging
import argparse
import datetime
import hashlib
import statistics
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("market_state_aggregator")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

# Gate 1 — System Gate
MIN_REQUIRED_AGENTS        = 2
MIN_CONFIDENT_AGENT_COUNT  = 3
MAX_SNAPSHOT_AGE_MINUTES   = 60

# Gate 3 — Benchmark (SPX)
TREND_NEUTRAL_BAND         = 0.005   # 0.5% absolute MA difference triggers neutral
SPX_DRAWDOWN_THRESHOLD     = 0.10    # 10% from rolling peak
SPX_VOL_THRESHOLD          = 0.20    # 20% annualized realized vol
VIX_THRESHOLD              = 25.0

# Gate 4 — Macro input confidence
MACRO_HIGH_CONF_THRESHOLD  = 0.70
MACRO_LOW_CONF_THRESHOLD   = 0.40

# Gate 5 — Sentiment confidence
SENTIMENT_HIGH_CONF_THRESHOLD = 0.70
SENTIMENT_LOW_CONF_THRESHOLD  = 0.40

# Gate 6 — Flow confidence
FLOW_HIGH_CONF_THRESHOLD   = 0.70
FLOW_LOW_CONF_THRESHOLD    = 0.40

# Gate 7 — News confidence
NEWS_HIGH_CONF_THRESHOLD   = 0.70
NEWS_LOW_CONF_THRESHOLD    = 0.40

# Gate 8 — Rumor confidence
RUMOR_HIGH_CONF_THRESHOLD  = 0.70
RUMOR_LOW_CONF_THRESHOLD   = 0.40

# Gate 15 — Aggregate state thresholds
STRONG_BULL_THRESHOLD      =  0.55
BULL_THRESHOLD             =  0.20
NEUTRAL_LOW                = -0.15
STRONG_BEAR_THRESHOLD      = -0.50

# Gate 16 — Confidence
AGREEMENT_THRESHOLD        = 0.30
DISAGREEMENT_THRESHOLD     = 0.45
DATA_QUALITY_THRESHOLD     = 0.70
HIGH_CONF_THRESHOLD        = 0.70
LOW_CONF_THRESHOLD         = 0.40

# Gate 20 — Action classification
ACTION_HIGH_CONF_THRESHOLD = 0.70
ACTION_LOW_CONF_THRESHOLD  = 0.40

# Gate 21 — Risk discounts
LOW_CONFIDENCE_DISCOUNT    = 0.80
WARNING_DISCOUNT           = 0.90
VALIDATION_CAUTION_DISCOUNT= 0.90
INFO_CONFLICT_DISCOUNT     = 0.85
HIGH_VOL_DISCOUNT          = 0.85
DRAWDOWN_DISCOUNT          = 0.80
WARNING_COUNT_THRESHOLD    = 2

# Gate 22 — Temporal persistence
PERSISTENCE_THRESHOLD      = 3
FLIP_THRESHOLD             = 3
IMPROVEMENT_THRESHOLD      = 0.05
DETERIORATION_THRESHOLD    = 0.05

# Gate 25 — Final signal thresholds
FINAL_STRONG_RISK_ON       =  0.55
FINAL_RISK_ON              =  0.15
FINAL_NEUTRAL_LOW          = -0.10
FINAL_STRONG_RISK_OFF      = -0.45
WARN_PENALTY               =  0.05

# Base component weights (must sum to 1.0)
BASE_WEIGHTS = {
    "macro":      0.30,
    "sentiment":  0.25,
    "flow":       0.20,
    "benchmark":  0.10,
    "news":       0.07,
    "rumor":      0.04,
    "validation": 0.04,
}

# Dynamic weight upshift multipliers (Gate 14)
MACRO_UPSHIFT       = 1.25
SENTIMENT_UPSHIFT   = 1.20
FLOW_UPSHIFT        = 1.20
NEWS_UPSHIFT        = 1.30   # applied when news_confidence=high AND rumor=low_trust
RUMOR_UPSHIFT       = 1.25   # applied when rumor=confirmed_transition
BENCHMARK_UPSHIFT   = 1.30   # applied when benchmark_risk_state=drawdown
FLOW_VOL_UPSHIFT    = 1.20   # additional flow upshift when benchmark_vol=high
VALIDATION_UPSHIFT  = 1.50   # applied for caution/failed/blocked/systemic/unresolved

# ============================================================
# COMPONENT SCORING TABLES
# Positive = risk-on / bullish. Negative = risk-off / bearish.
# ============================================================

# Macro component scoring
MACRO_STATE_SCORES = {
    "pro_growth":       1.0,
    "neutral":          0.0,
    "defensive_growth": -1.0,
    "stagflation":      -1.0,
}
MACRO_CONF_ADJUSTMENTS = {"high": 0.10, "low": -0.10}
MACRO_WARN_ADJUSTMENT  = -0.20

# Sentiment component scoring
SENTIMENT_STATE_SCORES = {
    "bullish":         1.0,
    "mildly_bullish":  0.5,
    "neutral":         0.0,
    "mildly_bearish": -0.5,
    "bearish":        -1.0,
    "panic":          -1.0,
    "euphoric":        0.7,
}
SENTIMENT_CONF_ADJUSTMENTS = {"high": 0.10, "low": -0.10}
SENTIMENT_WARN_ADJUSTMENT  = -0.20

# Flow component scoring
FLOW_STATE_SCORES = {
    "supportive":        1.0,
    "mildly_supportive": 0.5,
    "neutral":           0.0,
    "squeeze_prone":     0.0,
    "fragile":          -0.5,
    "destabilizing":    -1.0,
    "liquidation_prone":-1.0,
}
FLOW_CONF_ADJUSTMENTS = {"high": 0.10, "low": -0.10}
FLOW_WARN_ADJUSTMENT  = -0.20

# News component scoring
NEWS_STATE_SCORES = {
    "positive":                0.8,
    "macro_benchmark_relevant": 0.0,
    "uncertain":               0.0,
    "inactive":                0.0,
    "negative":               -0.8,
}
NEWS_CONF_ADJUSTMENTS  = {"high": 0.10, "low": -0.10}
NEWS_CONTRADICTION_ADJUSTMENT = -0.30

# Rumor component scoring
RUMOR_STATE_SCORES = {
    "confirmed_transition":      0.8,
    "positive":                  0.6,
    "macro_benchmark_relevant":  0.0,
    "low_trust":                 0.0,
    "inactive":                  0.0,
    "negative":                 -0.6,
}
RUMOR_CONF_ADJUSTMENTS  = {"high": 0.10, "low": -0.10}
RUMOR_CONTRADICTION_ADJUSTMENT = -0.30

# Benchmark component scoring
BENCHMARK_STATE_SCORES    = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
BENCHMARK_MOMENTUM_SCORES = {"positive": 0.3, "negative": -0.3}
BENCHMARK_RISK_SCORES     = {"drawdown": -0.5}
BENCHMARK_VOL_SCORES      = {"high": -0.2, "normal": 0.0}

# Benchmark component sub-weights
BENCHMARK_WEIGHTS = (0.45, 0.25, 0.20, 0.10)
# state, momentum, risk, vol

# Validation component scoring
VALIDATION_STATE_SCORES = {
    "clean":            0.2,
    "caution":          0.0,
    "unresolved_risk": -0.2,
    "failed":          -0.3,
    "blocked":         -1.0,
    "systemic_failure":-1.0,
}

# Component sub-weights (for 3-part weighted sum: state, confidence, warning)
MACRO_WEIGHTS      = (0.70, 0.15, 0.15)
SENTIMENT_WEIGHTS  = (0.70, 0.15, 0.15)
FLOW_WEIGHTS       = (0.70, 0.15, 0.15)
NEWS_WEIGHTS       = (0.75, 0.15, 0.10)
RUMOR_WEIGHTS      = (0.75, 0.15, 0.10)


# ============================================================
# AGGREGATOR DECISION LOG
# ============================================================

class AggregatorDecisionLog:
    """
    Accumulates all gate records and state produced during a single
    aggregate_market_state() run. Serialises to JSON and human-readable text.
    """

    def __init__(self, snapshot_id: str):
        self.snapshot_id = snapshot_id
        self.timestamp   = datetime.datetime.utcnow().isoformat()
        self.records     = []

        # Gate 1
        self.halted                     = False
        self.halt_reason                = None
        self.benchmark_context_disabled = False

        # Gate 2 — upstream slot availability
        self.upstream_states = {}   # keyed by slot name → "active" or "inactive"
        self.available_upstream_agent_count = 0

        # Gate 3 — benchmark anchor
        self.benchmark_state     = None
        self.benchmark_risk_state= None
        self.benchmark_vol_state = None
        self.benchmark_momentum_state = None

        # Gate 4 — macro input mapping
        self.macro_state             = None
        self.macro_confidence_state  = None
        self.macro_warning_state     = None

        # Gate 5 — sentiment input mapping (TODO:DATA_DEPENDENCY)
        self.sentiment_state             = None
        self.sentiment_confidence_state  = None
        self.sentiment_warning_state     = None

        # Gate 6 — flow input mapping (TODO:DATA_DEPENDENCY)
        self.flow_state             = None
        self.flow_confidence_state  = None
        self.flow_warning_state     = None

        # Gate 7 — news input mapping
        self.news_state             = None
        self.news_confidence_state  = None
        self.news_warning_state     = None

        # Gate 8 — rumor input mapping
        self.rumor_state             = None
        self.rumor_confidence_state  = None
        self.rumor_warning_state     = None

        # Gate 9 — validator input
        self.validation_state = None

        # Gate 10 — directional alignment
        self.alignment_state = None

        # Gate 11 — information alignment
        self.info_state = None

        # Gate 12 — benchmark consistency
        self.benchmark_consistency_state = None

        # Gate 13 — component scores
        self.component_scores = {}

        # Gate 14 — weights (initialised from BASE_WEIGHTS; adjusted in Gate 14)
        self.component_weights = dict(BASE_WEIGHTS)

        # Gate 15 — aggregate score and state
        self.aggregate_market_score = None
        self.aggregate_state        = None

        # Gate 16 — confidence
        self.confidence_input_state = None
        self.confidence_state       = None
        self.data_confidence_state  = None
        self.aggregate_confidence   = None

        # Gate 17 — divergence warnings
        self.warning_states = []

        # Gate 18 — market regime
        self.market_regime_state = None

        # Gate 19 — overrides (recorded in market_regime_state)
        self.override_applied = None

        # Gate 20 — action classification
        self.classification = None

        # Gate 21 — risk discounts
        self.bullish_strength_multiplier = 1.0

        # Gate 22 — temporal persistence
        self.persistence_state    = None
        self.aggregate_trend_state= None

        # Gate 23 — evaluation loop
        self.evaluation_notes = []

        # Gate 24 — output action
        self.output_action = None

        # Gate 25 — final signal
        self.final_market_state_signal = None
        self.final_market_state        = None

        # Gate 26 — downstream routing
        self.downstream_route = None

    def record(self, gate: int, name: str, inputs: dict, result: str, reason_code: str):
        self.records.append({
            "gate":        gate,
            "name":        name,
            "inputs":      inputs,
            "result":      result,
            "reason_code": reason_code,
            "ts":          datetime.datetime.utcnow().isoformat(),
        })

    def halt(self, gate: int, reason_code: str, inputs: dict) -> dict:
        self.halted      = True
        self.halt_reason = reason_code
        self.record(gate, "HALT", inputs, "halted", reason_code)
        return {"halted": True, "reason": reason_code}

    def to_dict(self) -> dict:
        return {
            "snapshot_id":                   self.snapshot_id,
            "timestamp":                     self.timestamp,
            "halted":                        self.halted,
            "halt_reason":                   self.halt_reason,
            "benchmark_context_disabled":    self.benchmark_context_disabled,
            "upstream_states":               self.upstream_states,
            "available_upstream_agent_count":self.available_upstream_agent_count,
            "benchmark_state":               self.benchmark_state,
            "benchmark_risk_state":          self.benchmark_risk_state,
            "benchmark_vol_state":           self.benchmark_vol_state,
            "benchmark_momentum_state":      self.benchmark_momentum_state,
            "macro_state":                   self.macro_state,
            "macro_confidence_state":        self.macro_confidence_state,
            "macro_warning_state":           self.macro_warning_state,
            "sentiment_state":               self.sentiment_state,
            "sentiment_confidence_state":    self.sentiment_confidence_state,
            "sentiment_warning_state":       self.sentiment_warning_state,
            "flow_state":                    self.flow_state,
            "flow_confidence_state":         self.flow_confidence_state,
            "flow_warning_state":            self.flow_warning_state,
            "news_state":                    self.news_state,
            "news_confidence_state":         self.news_confidence_state,
            "news_warning_state":            self.news_warning_state,
            "rumor_state":                   self.rumor_state,
            "rumor_confidence_state":        self.rumor_confidence_state,
            "rumor_warning_state":           self.rumor_warning_state,
            "validation_state":              self.validation_state,
            "alignment_state":               self.alignment_state,
            "info_state":                    self.info_state,
            "benchmark_consistency_state":   self.benchmark_consistency_state,
            "component_scores":              self.component_scores,
            "component_weights":             self.component_weights,
            "aggregate_market_score":        self.aggregate_market_score,
            "aggregate_state":               self.aggregate_state,
            "confidence_input_state":        self.confidence_input_state,
            "confidence_state":              self.confidence_state,
            "data_confidence_state":         self.data_confidence_state,
            "aggregate_confidence":          self.aggregate_confidence,
            "warning_states":                self.warning_states,
            "market_regime_state":           self.market_regime_state,
            "override_applied":              self.override_applied,
            "classification":                self.classification,
            "bullish_strength_multiplier":   self.bullish_strength_multiplier,
            "persistence_state":             self.persistence_state,
            "aggregate_trend_state":         self.aggregate_trend_state,
            "evaluation_notes":              self.evaluation_notes,
            "output_action":                 self.output_action,
            "final_market_state_signal":     self.final_market_state_signal,
            "final_market_state":            self.final_market_state,
            "downstream_route":              self.downstream_route,
            "decision_log":                  self.records,
        }

    def to_human_readable(self) -> str:
        lines = [
            "=" * 72,
            "MASTER MARKET-STATE AGGREGATOR — DECISION LOG",
            f"Snapshot ID         : {self.snapshot_id}",
            f"Timestamp           : {self.timestamp}",
            "=" * 72,
            f"\nAggregate State     : {self.aggregate_state}",
            f"Market Regime       : {self.market_regime_state}",
            f"Override Applied    : {self.override_applied}",
            f"Classification      : {self.classification}",
            f"Output Action       : {self.output_action}",
            f"Aggregate Score     : {self.aggregate_market_score}",
            f"Aggregate Confidence: {self.aggregate_confidence}",
            f"Final Market Signal : {self.final_market_state_signal}",
            f"Final Market State  : {self.final_market_state}",
            f"Downstream Route    : {self.downstream_route}",
            f"\nWarnings            : {self.warning_states}",
            f"Persistence State   : {self.persistence_state}",
            f"Trend State         : {self.aggregate_trend_state}",
            "\nComponent Scores:",
        ]
        for comp, score in sorted(self.component_scores.items()):
            weight = self.component_weights.get(comp, 0.0)
            lines.append(f"  {comp:12s}  score={score:+.4f}  weight={weight:.4f}")
        lines.append("\nInput States:")
        lines.append(f"  macro      : state={self.macro_state}  conf={self.macro_confidence_state}  warn={self.macro_warning_state}")
        lines.append(f"  sentiment  : state={self.sentiment_state}  conf={self.sentiment_confidence_state}")
        lines.append(f"  flow       : state={self.flow_state}  conf={self.flow_confidence_state}")
        lines.append(f"  news       : state={self.news_state}  conf={self.news_confidence_state}  warn={self.news_warning_state}")
        lines.append(f"  rumor      : state={self.rumor_state}  conf={self.rumor_confidence_state}  warn={self.rumor_warning_state}")
        lines.append(f"  validation : state={self.validation_state}")
        lines.append(f"\nBenchmark (SPX)    : state={self.benchmark_state}  risk={self.benchmark_risk_state}  vol={self.benchmark_vol_state}  momentum={self.benchmark_momentum_state}")
        lines.append(f"Alignment          : directional={self.alignment_state}  info={self.info_state}  benchmark={self.benchmark_consistency_state}")
        lines.append("=" * 72)
        return "\n".join(lines)


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: AggregatorDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates aggregator input before any processing begins.
    Returns True if the run should continue; False if halted.
    """
    # Check 1: Payload present
    if snapshot is None:
        dlog.halt(1, "reject_snapshot", {"payload": None})
        return False

    # Check 2: Benchmark feed (non-halting)
    spx_status = snapshot.get("SPX_feed_status")
    if spx_status != "online":
        dlog.benchmark_context_disabled = True
        dlog.record(1, "system_benchmark",
                    {"SPX_feed_status": spx_status},
                    "benchmark_context_disabled",
                    "spx_feed_unavailable_benchmark_skipped")

    # Determine available upstream agent count by checking output fields
    upstream_fields = {
        "macro":     "macro_agent_output",
        "sentiment": "sentiment_agent_output",
        "flow":      "flow_agent_output",
        "news":      "news_agent_output",
        "rumor":     "rumor_agent_output",
        "benchmark": "benchmark_state_output",
        "validator": "validator_output",
    }
    available_count = sum(
        1 for field in upstream_fields.values()
        if snapshot.get(field) is not None
    )

    # Check 3: All agents unavailable
    if available_count == 0:
        dlog.halt(1, "halt_aggregation",
                  {"available_upstream_agent_count": 0})
        return False

    # Check 4: Insufficient agents
    if available_count < MIN_REQUIRED_AGENTS:
        dlog.halt(1, "insufficient_inputs",
                  {"available_upstream_agent_count": available_count,
                   "min_required": MIN_REQUIRED_AGENTS})
        return False

    # Check 5: Timestamp present
    timestamp_str = snapshot.get("timestamp")
    if timestamp_str is None:
        dlog.halt(1, "reject_snapshot", {"timestamp": None})
        return False

    # Check 6: Timestamp freshness
    try:
        snap_time   = datetime.datetime.fromisoformat(timestamp_str)
        age_minutes = (datetime.datetime.utcnow() - snap_time).total_seconds() / 60.0
        if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
            dlog.halt(1, "stale_snapshot",
                      {"age_minutes": round(age_minutes, 1),
                       "max_snapshot_age_minutes": MAX_SNAPSHOT_AGE_MINUTES})
            return False
    except (ValueError, TypeError):
        dlog.halt(1, "reject_snapshot",
                  {"timestamp": timestamp_str, "error": "unparseable_timestamp"})
        return False

    # Check 7: Duplicate detection
    processed_store = snapshot.get("processed_snapshot_store", [])
    # Hash over all upstream output fields for deduplication
    hash_payload = {
        field: snapshot.get(field)
        for field in upstream_fields.values()
    }
    snap_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    if snap_hash in processed_store:
        dlog.halt(1, "suppress_duplicate",
                  {"snapshot_hash": snap_hash[:16] + "..."})
        return False

    dlog.record(1, "system_gate",
                {"available_upstream_agent_count": available_count,
                 "age_minutes": round(age_minutes, 1),
                 "benchmark_context_disabled": dlog.benchmark_context_disabled},
                "pass", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — UPSTREAM AVAILABILITY CONTROLS
# ============================================================

def gate_2_upstream_availability(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 2 — UPSTREAM AVAILABILITY CONTROLS
    Sets each upstream slot to active or inactive based on output field presence.
    """
    slot_fields = {
        "macro":     "macro_agent_output",
        "sentiment": "sentiment_agent_output",
        "flow":      "flow_agent_output",
        "news":      "news_agent_output",
        "rumor":     "rumor_agent_output",
        "benchmark": "benchmark_state_output",
        "validator": "validator_output",
    }
    active_count = 0
    for slot, field in slot_fields.items():
        available = snapshot.get(field) is not None
        state     = "active" if available else "inactive"
        dlog.upstream_states[slot] = state
        if available:
            active_count += 1
        dlog.record(2, "upstream_availability",
                    {"slot": slot, "field": field, "present": available},
                    state, f"upstream_{slot}_{state}")

    dlog.available_upstream_agent_count = active_count
    dlog.record(2, "upstream_availability_summary",
                {"active_count": active_count},
                str(active_count), "upstream_slot_check_complete")


def _slot_active(dlog: AggregatorDecisionLog, slot: str) -> bool:
    return dlog.upstream_states.get(slot) == "active"


# ============================================================
# GATE 3 — BENCHMARK ANCHOR CONTROLS
# ============================================================

def gate_3_benchmark_anchor(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 3 — BENCHMARK ANCHOR CONTROLS
    Classifies SPX trend, drawdown, volatility, and momentum.
    Skipped if benchmark_context_disabled was set in Gate 1.
    """
    if dlog.benchmark_context_disabled:
        dlog.record(3, "benchmark_anchor", {}, "skipped", "benchmark_context_disabled")
        return

    p = snapshot.get("benchmark_state_output") or snapshot

    # Trend: MA comparison
    ma_short = p.get("MA_short_SPX")
    ma_long  = p.get("MA_long_SPX")
    if ma_short is not None and ma_long is not None and ma_long > 0:
        diff_pct = abs(ma_short - ma_long) / ma_long
        if diff_pct <= TREND_NEUTRAL_BAND:
            dlog.benchmark_state = "neutral"
            dlog.record(3, "benchmark_trend",
                        {"MA_short": ma_short, "MA_long": ma_long,
                         "diff_pct": round(diff_pct, 5)},
                        "neutral", "ma_difference_within_neutral_band")
        elif ma_short > ma_long:
            dlog.benchmark_state = "bullish"
            dlog.record(3, "benchmark_trend",
                        {"MA_short": ma_short, "MA_long": ma_long},
                        "bullish", "short_ma_above_long_ma")
        else:
            dlog.benchmark_state = "bearish"
            dlog.record(3, "benchmark_trend",
                        {"MA_short": ma_short, "MA_long": ma_long},
                        "bearish", "short_ma_below_long_ma")

    # Drawdown
    spx_current  = p.get("SPX_current")
    rolling_peak = p.get("rolling_peak_SPX")
    if spx_current is not None and rolling_peak is not None and rolling_peak > 0:
        drawdown = (spx_current - rolling_peak) / rolling_peak
        if drawdown <= -SPX_DRAWDOWN_THRESHOLD:
            dlog.benchmark_risk_state = "drawdown"
            dlog.record(3, "benchmark_drawdown",
                        {"drawdown": round(drawdown, 4),
                         "threshold": -SPX_DRAWDOWN_THRESHOLD},
                        "drawdown", "spx_drawdown_active")
        else:
            dlog.record(3, "benchmark_drawdown",
                        {"drawdown": round(drawdown, 4)},
                        "pass", "no_drawdown")

    # Volatility
    realized_vol = p.get("realized_vol_SPX")
    vix          = p.get("VIX")
    if realized_vol is not None or vix is not None:
        vol_high = ((realized_vol is not None and realized_vol > SPX_VOL_THRESHOLD) or
                    (vix is not None and vix > VIX_THRESHOLD))
        if vol_high:
            dlog.benchmark_vol_state = "high"
            dlog.record(3, "benchmark_vol",
                        {"realized_vol": realized_vol, "VIX": vix,
                         "vol_threshold": SPX_VOL_THRESHOLD,
                         "vix_threshold": VIX_THRESHOLD},
                        "high", "benchmark_volatility_high")
        else:
            dlog.benchmark_vol_state = "normal"
            dlog.record(3, "benchmark_vol",
                        {"realized_vol": realized_vol, "VIX": vix},
                        "normal", "benchmark_volatility_normal")

    # Momentum
    roc = p.get("ROC_SPX")
    if roc is not None:
        if roc > 0:
            dlog.benchmark_momentum_state = "positive"
            dlog.record(3, "benchmark_momentum",
                        {"ROC_SPX": roc},
                        "positive", "spx_roc_positive")
        elif roc < 0:
            dlog.benchmark_momentum_state = "negative"
            dlog.record(3, "benchmark_momentum",
                        {"ROC_SPX": roc},
                        "negative", "spx_roc_negative")


# ============================================================
# GATE 4 — MACRO INPUT MAPPING CONTROLS
# ============================================================

def gate_4_macro_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 4 — MACRO INPUT MAPPING CONTROLS
    Translates Agent 8 final_macro_state into aggregator vocabulary.
    Skipped if macro slot is inactive.
    """
    if not _slot_active(dlog, "macro"):
        dlog.record(4, "macro_mapping", {}, "skipped", "macro_slot_inactive")
        return

    output = snapshot.get("macro_agent_output", {})
    raw_state = output.get("final_macro_state")

    state_map = {
        "strong_expansion":    "pro_growth",
        "mild_expansion":      "pro_growth",
        "strong_contraction":  "defensive_growth",
        "mild_contraction":    "defensive_growth",
        "neutral":             "neutral",
        "stagflation_override":"stagflation",
    }
    dlog.macro_state = state_map.get(raw_state, "neutral")
    dlog.record(4, "macro_state_mapping",
                {"final_macro_state": raw_state},
                dlog.macro_state,
                f"macro_mapped_from_{raw_state}")

    # Confidence
    macro_conf = output.get("macro_confidence")
    if macro_conf is not None:
        if macro_conf >= MACRO_HIGH_CONF_THRESHOLD:
            dlog.macro_confidence_state = "high"
        elif macro_conf < MACRO_LOW_CONF_THRESHOLD:
            dlog.macro_confidence_state = "low"
        dlog.record(4, "macro_confidence",
                    {"macro_confidence": macro_conf,
                     "high_threshold": MACRO_HIGH_CONF_THRESHOLD,
                     "low_threshold": MACRO_LOW_CONF_THRESHOLD},
                    str(dlog.macro_confidence_state),
                    "macro_confidence_assessed")

    # Warning
    macro_warnings = output.get("warning_states", [])
    if macro_warnings:
        dlog.macro_warning_state = "active"
        dlog.record(4, "macro_warnings",
                    {"warning_states": macro_warnings},
                    "active", "macro_upstream_warnings_present")


# ============================================================
# GATE 5 — SENTIMENT INPUT MAPPING CONTROLS
# ============================================================

def gate_5_sentiment_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 5 — SENTIMENT INPUT MAPPING CONTROLS
    Translates Sentiment Agent final_market_state into aggregator vocabulary.
    Skipped if sentiment slot is inactive.
    TODO:DATA_DEPENDENCY — Sentiment Agent not yet built.
    """
    if not _slot_active(dlog, "sentiment"):
        dlog.record(5, "sentiment_mapping", {}, "skipped",
                    "sentiment_slot_inactive_TODO_DATA_DEPENDENCY")
        return

    output = snapshot.get("sentiment_agent_output", {})
    raw_state = output.get("final_market_state")

    state_map = {
        "strong_bullish":          "bullish",
        "mild_bullish":            "mildly_bullish",
        "neutral":                 "neutral",
        "mild_bearish":            "mildly_bearish",
        "strong_bearish":          "bearish",
        "panic_override":          "panic",
        "euphoric_warning_override":"euphoric",
    }
    dlog.sentiment_state = state_map.get(raw_state, "neutral")
    dlog.record(5, "sentiment_state_mapping",
                {"final_market_state": raw_state},
                dlog.sentiment_state,
                f"sentiment_mapped_from_{raw_state}")

    # Confidence
    sent_conf = output.get("sentiment_confidence")
    if sent_conf is not None:
        if sent_conf >= SENTIMENT_HIGH_CONF_THRESHOLD:
            dlog.sentiment_confidence_state = "high"
        elif sent_conf < SENTIMENT_LOW_CONF_THRESHOLD:
            dlog.sentiment_confidence_state = "low"
        dlog.record(5, "sentiment_confidence",
                    {"sentiment_confidence": sent_conf},
                    str(dlog.sentiment_confidence_state),
                    "sentiment_confidence_assessed")

    # Warning
    sent_warnings = output.get("warning_state")
    if sent_warnings:
        dlog.sentiment_warning_state = "active"
        dlog.record(5, "sentiment_warnings",
                    {"warning_state": sent_warnings},
                    "active", "sentiment_upstream_warnings_present")


# ============================================================
# GATE 6 — POSITIONING/FLOW INPUT MAPPING CONTROLS
# ============================================================

def gate_6_flow_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 6 — POSITIONING/FLOW INPUT MAPPING CONTROLS
    Translates Flow Agent final_flow_state into aggregator vocabulary.
    Skipped if flow slot is inactive.
    TODO:DATA_DEPENDENCY — Positioning/Flow Agent not yet built.
    """
    if not _slot_active(dlog, "flow"):
        dlog.record(6, "flow_mapping", {}, "skipped",
                    "flow_slot_inactive_TODO_DATA_DEPENDENCY")
        return

    output = snapshot.get("flow_agent_output", {})
    raw_state = output.get("final_flow_state")

    state_map = {
        "strong_supportive":   "supportive",
        "mild_supportive":     "mildly_supportive",
        "neutral":             "neutral",
        "mild_fragile":        "fragile",
        "strong_destabilizing":"destabilizing",
        "squeeze_override":    "squeeze_prone",
        "liquidation_override":"liquidation_prone",
    }
    dlog.flow_state = state_map.get(raw_state, "neutral")
    dlog.record(6, "flow_state_mapping",
                {"final_flow_state": raw_state},
                dlog.flow_state,
                f"flow_mapped_from_{raw_state}")

    # Confidence
    flow_conf = output.get("flow_confidence")
    if flow_conf is not None:
        if flow_conf >= FLOW_HIGH_CONF_THRESHOLD:
            dlog.flow_confidence_state = "high"
        elif flow_conf < FLOW_LOW_CONF_THRESHOLD:
            dlog.flow_confidence_state = "low"
        dlog.record(6, "flow_confidence",
                    {"flow_confidence": flow_conf},
                    str(dlog.flow_confidence_state),
                    "flow_confidence_assessed")

    # Warning
    flow_warnings = output.get("warning_state")
    if flow_warnings:
        dlog.flow_warning_state = "active"
        dlog.record(6, "flow_warnings",
                    {"warning_state": flow_warnings},
                    "active", "flow_upstream_warnings_present")


# ============================================================
# GATE 7 — NEWS INPUT MAPPING CONTROLS
# ============================================================

def gate_7_news_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 7 — NEWS INPUT MAPPING CONTROLS
    Translates Agent 2 classification into aggregator vocabulary.
    Skipped if news slot is inactive.
    """
    if not _slot_active(dlog, "news"):
        dlog.record(7, "news_mapping", {}, "skipped", "news_slot_inactive")
        return

    output = snapshot.get("news_agent_output", {})
    raw_cls = output.get("classification")

    state_map = {
        "bullish_signal":            "positive",
        "relative_alpha_signal":     "positive",
        "bearish_signal":            "negative",
        "benchmark_regime_signal":   "macro_benchmark_relevant",
        "watch_only":                "uncertain",
        "provisional_watch":         "uncertain",
        "ignore":                    "inactive",
    }
    dlog.news_state = state_map.get(raw_cls, "uncertain")
    dlog.record(7, "news_state_mapping",
                {"classification": raw_cls},
                dlog.news_state,
                f"news_mapped_from_{raw_cls}")

    # Confidence
    news_conf = output.get("overall_confidence")
    if news_conf is not None:
        if news_conf >= NEWS_HIGH_CONF_THRESHOLD:
            dlog.news_confidence_state = "high"
        elif news_conf < NEWS_LOW_CONF_THRESHOLD:
            dlog.news_confidence_state = "low"
        dlog.record(7, "news_confidence",
                    {"overall_confidence": news_conf},
                    str(dlog.news_confidence_state),
                    "news_confidence_assessed")

    # Contradiction warning
    confirmation = output.get("confirmation_state")
    if raw_cls == "freeze" or confirmation == "contradictory":
        dlog.news_warning_state = "contradiction"
        dlog.record(7, "news_contradiction",
                    {"classification": raw_cls, "confirmation_state": confirmation},
                    "contradiction", "news_contradiction_detected")


# ============================================================
# GATE 8 — SOCIAL/RUMOR INPUT MAPPING CONTROLS
# ============================================================

def gate_8_rumor_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 8 — SOCIAL/RUMOR INPUT MAPPING CONTROLS
    Translates Agent 4 classification into aggregator vocabulary.
    Skipped if rumor slot is inactive.
    """
    if not _slot_active(dlog, "rumor"):
        dlog.record(8, "rumor_mapping", {}, "skipped", "rumor_slot_inactive")
        return

    output = snapshot.get("rumor_agent_output", {})
    raw_cls = output.get("classification")

    state_map = {
        "bullish_rumor_signal":       "positive",
        "relative_alpha_signal":      "positive",
        "bearish_rumor_signal":        "negative",
        "manipulation_watch":          "low_trust",
        "benchmark_regime_signal":     "macro_benchmark_relevant",
        "upgraded_to_confirmed_event": "confirmed_transition",
        "ignore":                      "inactive",
    }
    dlog.rumor_state = state_map.get(raw_cls, "inactive")
    dlog.record(8, "rumor_state_mapping",
                {"classification": raw_cls},
                dlog.rumor_state,
                f"rumor_mapped_from_{raw_cls}")

    # Confidence
    rumor_conf = output.get("overall_confidence")
    if rumor_conf is not None:
        if rumor_conf >= RUMOR_HIGH_CONF_THRESHOLD:
            dlog.rumor_confidence_state = "high"
        elif rumor_conf < RUMOR_LOW_CONF_THRESHOLD:
            dlog.rumor_confidence_state = "low"
        dlog.record(8, "rumor_confidence",
                    {"overall_confidence": rumor_conf},
                    str(dlog.rumor_confidence_state),
                    "rumor_confidence_assessed")

    # Contradiction warning
    confirmation = output.get("confirmation_state")
    if raw_cls == "freeze" or confirmation == "contradictory":
        dlog.rumor_warning_state = "contradiction"
        dlog.record(8, "rumor_contradiction",
                    {"classification": raw_cls, "confirmation_state": confirmation},
                    "contradiction", "rumor_contradiction_detected")


# ============================================================
# GATE 9 — VALIDATOR INPUT CONTROLS
# ============================================================

def gate_9_validator_mapping(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 9 — VALIDATOR INPUT CONTROLS
    Translates Agent 7 master_classification into aggregator validation state.
    Skipped if validator slot is inactive.
    """
    if not _slot_active(dlog, "validator"):
        dlog.record(9, "validator_mapping", {}, "skipped", "validator_slot_inactive")
        return

    output = snapshot.get("validator_output", {})
    raw_cls = output.get("master_classification")

    state_map = {
        "pass":                    "clean",
        "review_recommended":      "caution",
        "fail":                    "failed",
        "block_output":            "blocked",
        "escalate_systemic_issue": "systemic_failure",
        "unresolved":              "unresolved_risk",
    }
    dlog.validation_state = state_map.get(raw_cls, "caution")
    dlog.record(9, "validator_state_mapping",
                {"master_classification": raw_cls},
                dlog.validation_state,
                f"validator_mapped_from_{raw_cls}")


# ============================================================
# GATE 10 — DIRECTIONAL ALIGNMENT CONTROLS
# ============================================================

def gate_10_directional_alignment(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 10 — DIRECTIONAL ALIGNMENT CONTROLS
    Evaluates directional consistency of macro, sentiment, and flow inputs.
    Checked in priority order; first match wins.
    """
    ms   = dlog.macro_state
    sent = dlog.sentiment_state
    flow = dlog.flow_state

    # Priority 1
    if (ms == "pro_growth" and
            sent in {"bullish", "mildly_bullish"} and
            flow in {"supportive", "mildly_supportive"}):
        dlog.alignment_state = "bullish_alignment"
        dlog.record(10, "directional_alignment",
                    {"macro_state": ms, "sentiment_state": sent, "flow_state": flow},
                    "bullish_alignment", "all_three_directionally_bullish")
        return

    # Priority 2
    if (ms == "defensive_growth" and
            sent in {"bearish", "mildly_bearish", "panic"} and
            flow in {"fragile", "destabilizing", "liquidation_prone"}):
        dlog.alignment_state = "bearish_alignment"
        dlog.record(10, "directional_alignment",
                    {"macro_state": ms, "sentiment_state": sent, "flow_state": flow},
                    "bearish_alignment", "all_three_directionally_bearish")
        return

    # Priority 3
    if ms == "pro_growth" and sent in {"bearish", "mildly_bearish", "panic"}:
        dlog.alignment_state = "macro_sentiment_conflict"
        dlog.record(10, "directional_alignment",
                    {"macro_state": ms, "sentiment_state": sent},
                    "macro_sentiment_conflict",
                    "pro_growth_macro_with_bearish_sentiment")
        return

    # Priority 4
    if ms == "defensive_growth" and sent in {"bullish", "mildly_bullish", "euphoric"}:
        dlog.alignment_state = "market_macro_conflict"
        dlog.record(10, "directional_alignment",
                    {"macro_state": ms, "sentiment_state": sent},
                    "market_macro_conflict",
                    "defensive_macro_with_bullish_sentiment")
        return

    # Priority 5
    if (sent in {"bullish", "mildly_bullish"} and
            flow in {"destabilizing", "liquidation_prone"}):
        dlog.alignment_state = "sentiment_flow_conflict"
        dlog.record(10, "directional_alignment",
                    {"sentiment_state": sent, "flow_state": flow},
                    "sentiment_flow_conflict",
                    "bullish_sentiment_with_destabilizing_flow")
        return

    # Priority 6
    if (sent in {"bearish", "mildly_bearish"} and
            flow in {"supportive", "mildly_supportive", "squeeze_prone"}):
        dlog.alignment_state = "downside_squeeze_conflict"
        dlog.record(10, "directional_alignment",
                    {"sentiment_state": sent, "flow_state": flow},
                    "downside_squeeze_conflict",
                    "bearish_sentiment_with_supportive_flow")
        return

    # Default
    dlog.alignment_state = "mixed"
    dlog.record(10, "directional_alignment",
                {"macro_state": ms, "sentiment_state": sent, "flow_state": flow},
                "mixed", "no_strong_directional_alignment")


# ============================================================
# GATE 11 — INFORMATION ALIGNMENT CONTROLS
# ============================================================

def gate_11_information_alignment(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 11 — INFORMATION ALIGNMENT CONTROLS
    Compares news and rumor states. Checked in priority order; first match wins.
    """
    ns = dlog.news_state
    rs = dlog.rumor_state

    # Priority 1
    if rs == "low_trust":
        dlog.info_state = "rumor_discounted"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "rumor_discounted", "rumor_low_trust_discounted")
        return

    # Priority 2
    if rs == "confirmed_transition":
        dlog.info_state = "rumor_upgraded_to_news_path"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "rumor_upgraded_to_news_path", "rumor_confirmed_upgraded")
        return

    # Priority 3
    if ns == "positive" and rs in {"positive", "confirmed_transition"}:
        dlog.info_state = "positive_confirmation"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "positive_confirmation", "both_positive")
        return

    # Priority 4
    if ns == "negative" and rs == "negative":
        dlog.info_state = "negative_confirmation"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "negative_confirmation", "both_negative")
        return

    # Priority 5 & 6
    if (ns == "positive" and rs == "negative") or (ns == "negative" and rs == "positive"):
        dlog.info_state = "information_conflict"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "information_conflict", "news_rumor_directionally_opposed")
        return

    # Priority 7
    if ns == "inactive" and rs == "inactive":
        dlog.info_state = "low_information_flow"
        dlog.record(11, "info_alignment",
                    {"news_state": ns, "rumor_state": rs},
                    "low_information_flow", "both_inactive")
        return

    # Default
    dlog.info_state = "mixed_information"
    dlog.record(11, "info_alignment",
                {"news_state": ns, "rumor_state": rs},
                "mixed_information", "no_strong_informational_alignment")


# ============================================================
# GATE 12 — BENCHMARK CONSISTENCY CONTROLS
# ============================================================

def gate_12_benchmark_consistency(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 12 — BENCHMARK CONSISTENCY CONTROLS
    Evaluates whether the S&P 500 trend confirms or contradicts aggregate inputs.
    Checked in priority order; first match wins.
    """
    bs  = dlog.benchmark_state
    aln = dlog.alignment_state

    # Priority 1
    if bs == "bullish" and aln == "bullish_alignment":
        dlog.benchmark_consistency_state = "confirmed_uptrend"
        dlog.record(12, "benchmark_consistency",
                    {"benchmark_state": bs, "alignment_state": aln},
                    "confirmed_uptrend", "benchmark_and_inputs_both_bullish")
        return

    # Priority 2
    if bs == "bearish" and aln == "bearish_alignment":
        dlog.benchmark_consistency_state = "confirmed_downtrend"
        dlog.record(12, "benchmark_consistency",
                    {"benchmark_state": bs, "alignment_state": aln},
                    "confirmed_downtrend", "benchmark_and_inputs_both_bearish")
        return

    # Priority 3
    if bs == "bullish" and aln in {"macro_sentiment_conflict",
                                    "sentiment_flow_conflict",
                                    "market_macro_conflict"}:
        dlog.benchmark_consistency_state = "fragile_uptrend"
        dlog.record(12, "benchmark_consistency",
                    {"benchmark_state": bs, "alignment_state": aln},
                    "fragile_uptrend", "bullish_benchmark_with_conflicted_inputs")
        return

    # Priority 4
    if bs == "bearish" and aln in {"macro_sentiment_conflict",
                                    "downside_squeeze_conflict",
                                    "market_macro_conflict"}:
        dlog.benchmark_consistency_state = "unstable_downtrend"
        dlog.record(12, "benchmark_consistency",
                    {"benchmark_state": bs, "alignment_state": aln},
                    "unstable_downtrend", "bearish_benchmark_with_conflicted_inputs")
        return

    # Priority 5
    if bs == "neutral" and aln not in {"bullish_alignment", "bearish_alignment"}:
        dlog.benchmark_consistency_state = "indecisive_market"
        dlog.record(12, "benchmark_consistency",
                    {"benchmark_state": bs, "alignment_state": aln},
                    "indecisive_market", "neutral_benchmark_without_clear_alignment")
        return

    # Default
    dlog.benchmark_consistency_state = "unresolved_consistency"
    dlog.record(12, "benchmark_consistency",
                {"benchmark_state": bs, "alignment_state": aln},
                "unresolved_consistency", "no_benchmark_consistency_pattern_matched")


# ============================================================
# GATE 13 — COMPOSITE COMPONENT SCORING CONTROLS
# ============================================================

def _score_macro(dlog: AggregatorDecisionLog) -> float:
    state_score = MACRO_STATE_SCORES.get(dlog.macro_state, 0.0)
    conf_adj    = MACRO_CONF_ADJUSTMENTS.get(dlog.macro_confidence_state, 0.0)
    warn_adj    = MACRO_WARN_ADJUSTMENT if dlog.macro_warning_state == "active" else 0.0
    w = MACRO_WEIGHTS
    raw = w[0] * state_score + w[1] * conf_adj + w[2] * warn_adj
    return max(-1.0, min(1.0, raw))


def _score_sentiment(dlog: AggregatorDecisionLog) -> float:
    state_score = SENTIMENT_STATE_SCORES.get(dlog.sentiment_state, 0.0)
    conf_adj    = SENTIMENT_CONF_ADJUSTMENTS.get(dlog.sentiment_confidence_state, 0.0)
    warn_adj    = SENTIMENT_WARN_ADJUSTMENT if dlog.sentiment_warning_state == "active" else 0.0
    w = SENTIMENT_WEIGHTS
    raw = w[0] * state_score + w[1] * conf_adj + w[2] * warn_adj
    return max(-1.0, min(1.0, raw))


def _score_flow(dlog: AggregatorDecisionLog) -> float:
    state_score = FLOW_STATE_SCORES.get(dlog.flow_state, 0.0)
    conf_adj    = FLOW_CONF_ADJUSTMENTS.get(dlog.flow_confidence_state, 0.0)
    warn_adj    = FLOW_WARN_ADJUSTMENT if dlog.flow_warning_state == "active" else 0.0
    w = FLOW_WEIGHTS
    raw = w[0] * state_score + w[1] * conf_adj + w[2] * warn_adj
    return max(-1.0, min(1.0, raw))


def _score_news(dlog: AggregatorDecisionLog) -> float:
    state_score  = NEWS_STATE_SCORES.get(dlog.news_state, 0.0)
    conf_adj     = NEWS_CONF_ADJUSTMENTS.get(dlog.news_confidence_state, 0.0)
    contra_adj   = NEWS_CONTRADICTION_ADJUSTMENT if dlog.news_warning_state == "contradiction" else 0.0
    w = NEWS_WEIGHTS
    raw = w[0] * state_score + w[1] * conf_adj + w[2] * contra_adj
    return max(-1.0, min(1.0, raw))


def _score_rumor(dlog: AggregatorDecisionLog) -> float:
    state_score  = RUMOR_STATE_SCORES.get(dlog.rumor_state, 0.0)
    conf_adj     = RUMOR_CONF_ADJUSTMENTS.get(dlog.rumor_confidence_state, 0.0)
    contra_adj   = RUMOR_CONTRADICTION_ADJUSTMENT if dlog.rumor_warning_state == "contradiction" else 0.0
    w = RUMOR_WEIGHTS
    raw = w[0] * state_score + w[1] * conf_adj + w[2] * contra_adj
    return max(-1.0, min(1.0, raw))


def _score_benchmark(dlog: AggregatorDecisionLog) -> float:
    state_score    = BENCHMARK_STATE_SCORES.get(dlog.benchmark_state, 0.0)
    momentum_score = BENCHMARK_MOMENTUM_SCORES.get(dlog.benchmark_momentum_state, 0.0)
    risk_score     = BENCHMARK_RISK_SCORES.get(dlog.benchmark_risk_state, 0.0)
    vol_score      = BENCHMARK_VOL_SCORES.get(dlog.benchmark_vol_state, 0.0)
    w = BENCHMARK_WEIGHTS
    raw = (w[0] * state_score + w[1] * momentum_score +
           w[2] * risk_score  + w[3] * vol_score)
    return max(-1.0, min(1.0, raw))


def _score_validation(dlog: AggregatorDecisionLog) -> float:
    return VALIDATION_STATE_SCORES.get(dlog.validation_state, 0.0)


def gate_13_composite_scoring(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 13 — COMPOSITE COMPONENT SCORING CONTROLS
    Converts each active upstream component to a numeric score in [-1.0, +1.0].
    Inactive components are not added to component_scores (contribute 0.0 via weight).
    """
    score_funcs = {
        "macro":      (_slot_active(dlog, "macro"),      _score_macro),
        "sentiment":  (_slot_active(dlog, "sentiment"),  _score_sentiment),
        "flow":       (_slot_active(dlog, "flow"),        _score_flow),
        "news":       (_slot_active(dlog, "news"),        _score_news),
        "rumor":      (_slot_active(dlog, "rumor"),       _score_rumor),
        "benchmark":  (not dlog.benchmark_context_disabled and
                       _slot_active(dlog, "benchmark"),  _score_benchmark),
        "validation": (_slot_active(dlog, "validator"),  _score_validation),
    }

    for comp, (active, fn) in score_funcs.items():
        if active:
            score = round(fn(dlog), 4)
            dlog.component_scores[comp] = score
            dlog.record(13, "component_score",
                        {"component": comp, "active": True},
                        str(score), f"component_{comp}_scored")
        else:
            dlog.record(13, "component_score",
                        {"component": comp, "active": False},
                        "0.0", f"component_{comp}_inactive_score_zero")


# ============================================================
# GATE 14 — WEIGHTING CONTROLS
# ============================================================

def gate_14_weighting(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 14 — WEIGHTING CONTROLS
    Applies dynamic upshifts to base weights, then re-normalises to sum to 1.0.
    """
    w = dict(BASE_WEIGHTS)
    applied = []

    # Macro confidence high
    if dlog.macro_confidence_state == "high":
        w["macro"] *= MACRO_UPSHIFT
        applied.append("macro_confidence_high_upshift")

    # Sentiment confidence high
    if dlog.sentiment_confidence_state == "high":
        w["sentiment"] *= SENTIMENT_UPSHIFT
        applied.append("sentiment_confidence_high_upshift")

    # Flow confidence high
    if dlog.flow_confidence_state == "high":
        w["flow"] *= FLOW_UPSHIFT
        applied.append("flow_confidence_high_upshift")

    # News confidence high AND rumor = low_trust
    if dlog.news_confidence_state == "high" and dlog.rumor_state == "low_trust":
        w["news"] *= NEWS_UPSHIFT
        applied.append("news_high_conf_rumor_low_trust_upshift")

    # Rumor = confirmed_transition
    if dlog.rumor_state == "confirmed_transition":
        w["rumor"] *= RUMOR_UPSHIFT
        applied.append("rumor_confirmed_transition_upshift")

    # Benchmark risk = drawdown
    if dlog.benchmark_risk_state == "drawdown":
        w["benchmark"] *= BENCHMARK_UPSHIFT
        applied.append("benchmark_drawdown_upshift")

    # Benchmark vol = high (additional flow upshift)
    if dlog.benchmark_vol_state == "high":
        w["flow"] *= FLOW_VOL_UPSHIFT
        applied.append("benchmark_high_vol_flow_upshift")

    # Validation state in caution/failed/blocked/systemic_failure/unresolved_risk
    if dlog.validation_state in {"caution", "failed", "blocked",
                                  "systemic_failure", "unresolved_risk"}:
        w["validation"] *= VALIDATION_UPSHIFT
        applied.append("validation_degraded_upshift")

    # Re-normalise
    total = sum(w.values())
    if total > 0:
        for k in w:
            w[k] = round(w[k] / total, 6)

    dlog.component_weights = w
    dlog.record(14, "weighting",
                {"adjustments_applied": applied,
                 "final_weights": w},
                "normalised", "weights_adjusted_and_normalised")


# ============================================================
# GATE 15 — COMPOSITE MARKET-STATE SCORE CONTROLS
# ============================================================

def gate_15_composite_score(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 15 — COMPOSITE MARKET-STATE SCORE CONTROLS
    Computes weighted sum of active component scores and classifies aggregate state.
    """
    score = 0.0
    for comp, comp_score in dlog.component_scores.items():
        weight = dlog.component_weights.get(comp, 0.0)
        score += weight * comp_score

    score = round(max(-1.0, min(1.0, score)), 4)
    dlog.aggregate_market_score = score
    dlog.record(15, "aggregate_score",
                {"component_scores": dlog.component_scores,
                 "component_weights": dlog.component_weights,
                 "aggregate_market_score": score},
                str(score), "aggregate_market_score_calculated")

    # Classify aggregate state
    if score >= STRONG_BULL_THRESHOLD:
        dlog.aggregate_state = "strong_bullish"
    elif score >= BULL_THRESHOLD:
        dlog.aggregate_state = "mild_bullish"
    elif score >= NEUTRAL_LOW:
        dlog.aggregate_state = "neutral"
    elif score > STRONG_BEAR_THRESHOLD:
        dlog.aggregate_state = "mild_bearish"
    else:
        dlog.aggregate_state = "strong_bearish"

    dlog.record(15, "aggregate_state",
                {"aggregate_market_score": score,
                 "strong_bull_threshold": STRONG_BULL_THRESHOLD,
                 "bull_threshold": BULL_THRESHOLD,
                 "neutral_low": NEUTRAL_LOW,
                 "strong_bear_threshold": STRONG_BEAR_THRESHOLD},
                dlog.aggregate_state, f"aggregate_state_{dlog.aggregate_state}")


# ============================================================
# GATE 16 — CONFIDENCE CONTROLS
# ============================================================

def gate_16_confidence(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 16 — CONFIDENCE CONTROLS
    Computes aggregate_confidence from input sufficiency, score dispersion,
    data quality, and upstream confidence scores.
    """
    active_count = sum(1 for s in dlog.upstream_states.values() if s == "active")

    # Input sufficiency
    if active_count >= MIN_CONFIDENT_AGENT_COUNT:
        dlog.confidence_input_state = "sufficient"
    else:
        dlog.confidence_input_state = "weak"
    dlog.record(16, "confidence_inputs",
                {"active_count": active_count,
                 "min_confident": MIN_CONFIDENT_AGENT_COUNT},
                dlog.confidence_input_state, "input_count_assessed")

    # Score dispersion
    scores = list(dlog.component_scores.values())
    dispersion = 0.0
    if len(scores) >= 2:
        try:
            dispersion = statistics.stdev(scores)
        except statistics.StatisticsError:
            dispersion = 0.0

    if dispersion < AGREEMENT_THRESHOLD:
        dlog.confidence_state = "high_agreement"
    elif dispersion >= DISAGREEMENT_THRESHOLD:
        dlog.confidence_state = "conflicted"
    else:
        dlog.confidence_state = "moderate_agreement"
    dlog.record(16, "confidence_agreement",
                {"dispersion": round(dispersion, 4),
                 "agreement_threshold": AGREEMENT_THRESHOLD,
                 "disagreement_threshold": DISAGREEMENT_THRESHOLD},
                dlog.confidence_state, "confidence_agreement_assessed")

    # Data quality
    upstream_quality_scores = snapshot.get("upstream_quality_scores", [])
    # TODO:DATA_DEPENDENCY — upstream quality scores supplied by caller
    mean_quality = 0.70   # fallback default
    if upstream_quality_scores:
        try:
            mean_quality = sum(upstream_quality_scores) / len(upstream_quality_scores)
        except (TypeError, ZeroDivisionError):
            mean_quality = 0.70

    if mean_quality >= DATA_QUALITY_THRESHOLD:
        dlog.data_confidence_state = "high"
    else:
        dlog.data_confidence_state = "low"
    dlog.record(16, "data_quality",
                {"mean_quality": round(mean_quality, 4),
                 "quality_threshold": DATA_QUALITY_THRESHOLD},
                dlog.data_confidence_state, "data_quality_assessed")

    # Upstream confidence scores
    upstream_conf_scores = snapshot.get("upstream_confidence_scores", [])
    # TODO:DATA_DEPENDENCY — upstream confidence scores supplied by caller
    mean_upstream_conf = 0.60   # fallback default
    if upstream_conf_scores:
        try:
            mean_upstream_conf = sum(upstream_conf_scores) / len(upstream_conf_scores)
        except (TypeError, ZeroDivisionError):
            mean_upstream_conf = 0.60

    # Aggregate confidence formula
    agg_conf = (0.35 * (active_count / 7.0) +
                0.30 * (1.0 - min(dispersion, 1.0)) +
                0.20 * mean_quality +
                0.15 * mean_upstream_conf)
    agg_conf = round(max(0.0, min(1.0, agg_conf)), 4)
    dlog.aggregate_confidence = agg_conf
    dlog.record(16, "aggregate_confidence",
                {"active_count": active_count,
                 "dispersion": round(dispersion, 4),
                 "mean_quality": round(mean_quality, 4),
                 "mean_upstream_conf": round(mean_upstream_conf, 4),
                 "aggregate_confidence": agg_conf},
                str(agg_conf), "aggregate_confidence_calculated")


# ============================================================
# GATE 17 — DIVERGENCE WARNING CONTROLS
# ============================================================

def gate_17_divergence_warnings(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 17 — DIVERGENCE WARNING CONTROLS
    Detects cross-component inconsistencies. Multiple warnings may be active.
    """
    ms   = dlog.macro_state
    sent = dlog.sentiment_state
    flow = dlog.flow_state
    bcs  = dlog.benchmark_consistency_state
    bs   = dlog.benchmark_state
    brisk= dlog.benchmark_risk_state
    info = dlog.info_state
    valid= dlog.validation_state

    if ms == "pro_growth" and bcs == "fragile_uptrend":
        dlog.warning_states.append("macro_not_confirmed_by_market")
        dlog.record(17, "divergence_warning",
                    {"macro_state": ms, "benchmark_consistency": bcs},
                    "macro_not_confirmed_by_market",
                    "pro_growth_macro_with_fragile_benchmark")

    if sent in {"bullish", "mildly_bullish"} and valid in {"caution", "failed", "unresolved_risk"}:
        dlog.warning_states.append("signal_quality_risk")
        dlog.record(17, "divergence_warning",
                    {"sentiment_state": sent, "validation_state": valid},
                    "signal_quality_risk",
                    "bullish_sentiment_with_degraded_validator")

    if flow in {"supportive", "mildly_supportive"} and ms in {"defensive_growth", "stagflation"}:
        dlog.warning_states.append("positioning_outrunning_macro")
        dlog.record(17, "divergence_warning",
                    {"flow_state": flow, "macro_state": ms},
                    "positioning_outrunning_macro",
                    "supportive_flow_with_defensive_macro")

    if info == "information_conflict":
        dlog.warning_states.append("information_conflict")
        dlog.record(17, "divergence_warning",
                    {"info_state": info},
                    "information_conflict",
                    "news_and_rumor_directionally_opposed")

    if bs == "bearish" and sent == "euphoric":
        dlog.warning_states.append("euphoric_mispricing")
        dlog.record(17, "divergence_warning",
                    {"benchmark_state": bs, "sentiment_state": sent},
                    "euphoric_mispricing",
                    "bearish_benchmark_with_euphoric_sentiment")

    if brisk == "drawdown" and flow == "liquidation_prone":
        dlog.warning_states.append("cascade_risk")
        dlog.record(17, "divergence_warning",
                    {"benchmark_risk_state": brisk, "flow_state": flow},
                    "cascade_risk",
                    "drawdown_with_liquidation_prone_flow")

    if sent == "panic" and flow in {"supportive", "squeeze_prone"}:
        dlog.warning_states.append("panic_vs_positioning_conflict")
        dlog.record(17, "divergence_warning",
                    {"sentiment_state": sent, "flow_state": flow},
                    "panic_vs_positioning_conflict",
                    "panic_sentiment_with_supportive_or_squeeze_flow")

    if not dlog.warning_states:
        dlog.record(17, "divergence_warnings",
                    {}, "none", "no_divergence_warnings_detected")


# ============================================================
# GATE 18 — MARKET REGIME CLASSIFICATION CONTROLS
# ============================================================

def gate_18_market_regime(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 18 — MARKET REGIME CLASSIFICATION CONTROLS
    Maps aggregate state and alignment into a named market regime.
    Checked in priority order; first match wins.
    """
    ms    = dlog.macro_state
    sent  = dlog.sentiment_state
    agg   = dlog.aggregate_state
    aln   = dlog.alignment_state
    bcs   = dlog.benchmark_consistency_state
    flow  = dlog.flow_state
    conf  = dlog.confidence_state

    # Priority 1
    if ms == "stagflation" and sent in {"bearish", "mildly_bearish", "panic"}:
        dlog.market_regime_state = "stagflationary_stress"
        dlog.record(18, "market_regime",
                    {"macro_state": ms, "sentiment_state": sent},
                    "stagflationary_stress", "stagflation_with_bearish_sentiment")
        return

    # Priority 2
    if agg in {"strong_bullish", "mild_bullish"} and aln == "bullish_alignment":
        dlog.market_regime_state = "coordinated_risk_on"
        dlog.record(18, "market_regime",
                    {"aggregate_state": agg, "alignment_state": aln},
                    "coordinated_risk_on", "bullish_aggregate_with_bullish_alignment")
        return

    # Priority 3
    if agg in {"strong_bearish", "mild_bearish"} and aln == "bearish_alignment":
        dlog.market_regime_state = "coordinated_risk_off"
        dlog.record(18, "market_regime",
                    {"aggregate_state": agg, "alignment_state": aln},
                    "coordinated_risk_off", "bearish_aggregate_with_bearish_alignment")
        return

    # Priority 4
    if agg in {"strong_bullish", "mild_bullish"} and bcs == "fragile_uptrend":
        dlog.market_regime_state = "fragile_risk_on"
        dlog.record(18, "market_regime",
                    {"aggregate_state": agg, "benchmark_consistency": bcs},
                    "fragile_risk_on", "bullish_aggregate_with_fragile_benchmark")
        return

    # Priority 5
    if agg in {"strong_bearish", "mild_bearish"} and flow == "squeeze_prone":
        dlog.market_regime_state = "unstable_risk_off"
        dlog.record(18, "market_regime",
                    {"aggregate_state": agg, "flow_state": flow},
                    "unstable_risk_off", "bearish_aggregate_with_squeeze_prone_flow")
        return

    # Priority 6
    if agg == "neutral" and conf == "conflicted":
        dlog.market_regime_state = "indecisive_transition"
        dlog.record(18, "market_regime",
                    {"aggregate_state": agg, "confidence_state": conf},
                    "indecisive_transition", "neutral_with_conflicted_confidence")
        return

    # Priority 7
    if sent == "euphoric" and bcs == "fragile_uptrend":
        dlog.market_regime_state = "unstable_euphoria"
        dlog.record(18, "market_regime",
                    {"sentiment_state": sent, "benchmark_consistency": bcs},
                    "unstable_euphoria", "euphoric_sentiment_with_fragile_benchmark")
        return

    # Default
    dlog.market_regime_state = "indecisive_transition"
    dlog.record(18, "market_regime",
                {"aggregate_state": agg, "alignment_state": aln},
                "indecisive_transition", "no_regime_pattern_matched_default")


# ============================================================
# GATE 19 — OVERRIDE CONTROLS
# ============================================================

def gate_19_overrides(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 19 — OVERRIDE CONTROLS
    May replace market_regime_state set in Gate 18.
    Evaluated in priority order; highest priority override wins.
    """
    valid = dlog.validation_state
    brisk = dlog.benchmark_risk_state
    sent  = dlog.sentiment_state
    flow  = dlog.flow_state
    bvol  = dlog.benchmark_vol_state
    rs    = dlog.rumor_state
    ns    = dlog.news_state
    info  = dlog.info_state

    # Priority 1
    if valid == "blocked":
        dlog.market_regime_state = "blocked_output_override"
        dlog.override_applied = "validation_blocked"
        dlog.record(19, "override",
                    {"validation_state": valid},
                    "blocked_output_override", "validator_blocked_output")
        return

    # Priority 2
    if valid == "systemic_failure":
        dlog.market_regime_state = "systemic_risk_override"
        dlog.override_applied = "systemic_failure"
        dlog.record(19, "override",
                    {"validation_state": valid},
                    "systemic_risk_override", "validator_systemic_failure")
        return

    # Priority 3
    if brisk == "drawdown" and sent == "panic":
        dlog.market_regime_state = "panic_override"
        dlog.override_applied = "drawdown_panic"
        dlog.record(19, "override",
                    {"benchmark_risk_state": brisk, "sentiment_state": sent},
                    "panic_override", "drawdown_with_panic_sentiment")
        return

    # Priority 4
    if flow == "liquidation_prone" and bvol == "high":
        dlog.market_regime_state = "forced_deleveraging_override"
        dlog.override_applied = "liquidation_high_vol"
        dlog.record(19, "override",
                    {"flow_state": flow, "benchmark_vol_state": bvol},
                    "forced_deleveraging_override", "liquidation_prone_with_high_vol")
        return

    # Priority 5
    if (rs == "low_trust" and ns == "inactive" and
            info != "positive_confirmation"):
        dlog.market_regime_state = "low_trust_information_override"
        dlog.override_applied = "low_trust_information"
        dlog.record(19, "override",
                    {"rumor_state": rs, "news_state": ns, "info_state": info},
                    "low_trust_information_override",
                    "low_trust_rumor_no_news_no_positive_confirmation")
        return

    dlog.record(19, "override",
                {"market_regime_state": dlog.market_regime_state},
                "no_override", "no_override_conditions_met")


# ============================================================
# GATE 20 — ACTION CLASSIFICATION CONTROLS
# ============================================================

def gate_20_action_classification(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 20 — ACTION CLASSIFICATION CONTROLS
    Maps market_regime_state and aggregate_confidence to a classification.
    Evaluated in priority order.
    """
    mr   = dlog.market_regime_state
    conf = dlog.aggregate_confidence or 0.0

    # Priority 1
    if mr in {"blocked_output_override", "systemic_risk_override"}:
        dlog.classification = "suppress_or_escalate"
    # Priority 2
    elif mr == "panic_override":
        dlog.classification = "panic_alert"
    # Priority 3
    elif mr == "forced_deleveraging_override":
        dlog.classification = "deleveraging_alert"
    # Priority 4
    elif mr == "coordinated_risk_on" and conf >= ACTION_HIGH_CONF_THRESHOLD:
        dlog.classification = "strong_risk_on_signal"
    # Priority 5
    elif mr == "coordinated_risk_off" and conf >= ACTION_HIGH_CONF_THRESHOLD:
        dlog.classification = "strong_risk_off_signal"
    # Priority 6
    elif mr == "fragile_risk_on":
        dlog.classification = "cautious_risk_on_signal"
    # Priority 7
    elif mr == "unstable_risk_off":
        dlog.classification = "unstable_risk_off_signal"
    # Priority 8
    elif mr == "indecisive_transition":
        dlog.classification = "transition_state"
    # Priority 9
    elif mr == "stagflationary_stress":
        dlog.classification = "stagflation_stress_signal"
    # Priority 10
    elif mr == "unstable_euphoria":
        dlog.classification = "euphoria_warning_signal"
    # Priority 11 & 12
    elif conf < ACTION_LOW_CONF_THRESHOLD:
        dlog.classification = "no_clear_market_state"
    else:
        dlog.classification = "no_clear_market_state"

    dlog.record(20, "action_classification",
                {"market_regime_state": mr,
                 "aggregate_confidence": conf},
                dlog.classification,
                f"classification_{dlog.classification}")


# ============================================================
# GATE 21 — RISK DISCOUNT CONTROLS
# ============================================================

def gate_21_risk_discounts(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 21 — RISK DISCOUNT CONTROLS
    Applies multiplicative discounts to aggregate_market_score,
    aggregate_confidence, and bullish_strength_multiplier.
    Multiple discounts on the same target are multiplicative.
    """
    score  = dlog.aggregate_market_score or 0.0
    conf   = dlog.aggregate_confidence   or 0.0
    bsm    = dlog.bullish_strength_multiplier
    applied = []

    # Low confidence discount on score
    if conf < LOW_CONF_THRESHOLD:
        score *= LOW_CONFIDENCE_DISCOUNT
        applied.append("low_confidence_score_discount")

    # Warning count discount on confidence
    if len(dlog.warning_states) > WARNING_COUNT_THRESHOLD:
        conf *= WARNING_DISCOUNT
        applied.append("warning_count_confidence_discount")

    # Validation caution discount on confidence
    if dlog.validation_state == "caution":
        conf *= VALIDATION_CAUTION_DISCOUNT
        applied.append("validation_caution_confidence_discount")

    # Information conflict discount on confidence
    if dlog.info_state == "information_conflict":
        conf *= INFO_CONFLICT_DISCOUNT
        applied.append("info_conflict_confidence_discount")

    # High vol discount on bullish strength multiplier
    if dlog.benchmark_vol_state == "high":
        bsm *= HIGH_VOL_DISCOUNT
        applied.append("high_vol_bullish_strength_discount")

    # Drawdown discount on bullish strength multiplier
    if dlog.benchmark_risk_state == "drawdown":
        bsm *= DRAWDOWN_DISCOUNT
        applied.append("drawdown_bullish_strength_discount")

    dlog.aggregate_market_score      = round(max(-1.0, min(1.0, score)), 4)
    dlog.aggregate_confidence        = round(max(0.0, min(1.0, conf)), 4)
    dlog.bullish_strength_multiplier = round(bsm, 4)

    dlog.record(21, "risk_discounts",
                {"discounts_applied": applied,
                 "post_discount_score": dlog.aggregate_market_score,
                 "post_discount_confidence": dlog.aggregate_confidence,
                 "bullish_strength_multiplier": dlog.bullish_strength_multiplier},
                "applied", "risk_discounts_calculated")


# ============================================================
# GATE 22 — TEMPORAL PERSISTENCE CONTROLS
# ============================================================

def gate_22_temporal_persistence(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 22 — TEMPORAL PERSISTENCE CONTROLS
    Evaluates regime history from caller-supplied counts in the snapshot.
    """
    # Persistence
    risk_on_consecutive   = snapshot.get("coordinated_risk_on_consecutive_windows", 0) or 0
    risk_off_consecutive  = snapshot.get("coordinated_risk_off_consecutive_windows", 0) or 0
    transition_consecutive= snapshot.get("indecisive_transition_consecutive_windows", 0) or 0
    state_change_count    = snapshot.get("state_change_count", 0) or 0

    if risk_on_consecutive >= PERSISTENCE_THRESHOLD:
        dlog.persistence_state = "persistent_risk_on"
        dlog.record(22, "persistence",
                    {"risk_on_consecutive": risk_on_consecutive,
                     "threshold": PERSISTENCE_THRESHOLD},
                    "persistent_risk_on", "risk_on_streak_above_threshold")

    elif risk_off_consecutive >= PERSISTENCE_THRESHOLD:
        dlog.persistence_state = "persistent_risk_off"
        dlog.record(22, "persistence",
                    {"risk_off_consecutive": risk_off_consecutive,
                     "threshold": PERSISTENCE_THRESHOLD},
                    "persistent_risk_off", "risk_off_streak_above_threshold")

    elif transition_consecutive >= PERSISTENCE_THRESHOLD:
        dlog.persistence_state = "persistent_transition"
        dlog.record(22, "persistence",
                    {"transition_consecutive": transition_consecutive,
                     "threshold": PERSISTENCE_THRESHOLD},
                    "persistent_transition", "transition_streak_above_threshold")

    # Instability overrides persistence
    if state_change_count > FLIP_THRESHOLD:
        dlog.persistence_state = "unstable_regime"
        dlog.record(22, "instability",
                    {"state_change_count": state_change_count,
                     "flip_threshold": FLIP_THRESHOLD},
                    "unstable_regime", "excessive_regime_flipping")

    if dlog.persistence_state:
        dlog.record(22, "persistence_summary",
                    {"persistence_state": dlog.persistence_state},
                    dlog.persistence_state, "persistence_state_set")
    else:
        dlog.record(22, "persistence_summary",
                    {}, "none", "no_persistence_condition_met")

    # Score trend
    prev_score = snapshot.get("aggregate_market_score_prev")
    if prev_score is not None and dlog.aggregate_market_score is not None:
        delta = dlog.aggregate_market_score - prev_score
        if delta > IMPROVEMENT_THRESHOLD:
            dlog.aggregate_trend_state = "improving"
            dlog.record(22, "score_trend",
                        {"current_score": dlog.aggregate_market_score,
                         "prev_score": prev_score,
                         "delta": round(delta, 4)},
                        "improving", "aggregate_score_improving")
        elif delta < -DETERIORATION_THRESHOLD:
            dlog.aggregate_trend_state = "worsening"
            dlog.record(22, "score_trend",
                        {"current_score": dlog.aggregate_market_score,
                         "prev_score": prev_score,
                         "delta": round(delta, 4)},
                        "worsening", "aggregate_score_worsening")
        else:
            dlog.record(22, "score_trend",
                        {"current_score": dlog.aggregate_market_score,
                         "prev_score": prev_score,
                         "delta": round(delta, 4)},
                        "stable", "aggregate_score_stable")


# ============================================================
# GATE 23 — EVALUATION LOOP CONTROLS
# ============================================================

def gate_23_evaluation_loop(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 23 — EVALUATION LOOP CONTROLS
    Records feedback events for aggregator calibration.
    All write-back operations are TODO:DATA_DEPENDENCY.
    """
    cls = dlog.classification

    # Retention
    if cls not in {"suppress_or_escalate"}:
        dlog.evaluation_notes.append("state_eligible_for_retention")
        dlog.record(23, "eval_retention",
                    {"classification": cls},
                    "eligible", "aggregate_state_eligible_for_retention")
        # TODO:DATA_DEPENDENCY — store aggregate state record for model calibration

    # Prediction accuracy
    accuracy = snapshot.get("aggregate_prediction_accuracy")
    if accuracy is True:
        dlog.evaluation_notes.append("prediction_correct")
        dlog.record(23, "eval_accuracy",
                    {"aggregate_prediction_accuracy": True},
                    "reward", "predicted_aggregate_state_matched_realized_path")
        # TODO:DATA_DEPENDENCY — increment aggregator_model_score by reward value
    elif accuracy is False:
        dlog.evaluation_notes.append("prediction_incorrect")
        dlog.record(23, "eval_accuracy",
                    {"aggregate_prediction_accuracy": False},
                    "penalty", "predicted_aggregate_state_mismatched_realized_path")
        # TODO:DATA_DEPENDENCY — decrement aggregator_model_score by penalty value

    # False positive correction — risk-on
    risk_on_fp_rate = snapshot.get("risk_on_false_positive_rate")
    risk_on_fp_thresh = snapshot.get("risk_on_fp_threshold", 0.15)
    if risk_on_fp_rate is not None and risk_on_fp_rate > risk_on_fp_thresh:
        dlog.evaluation_notes.append("risk_on_fp_threshold_breach")
        dlog.record(23, "eval_fp_risk_on",
                    {"risk_on_false_positive_rate": risk_on_fp_rate,
                     "threshold": risk_on_fp_thresh},
                    "adjust_risk_on_thresholds",
                    "risk_on_fp_rate_too_high")
        # TODO:DATA_DEPENDENCY — flag bullish threshold for human-supervised recalibration

    # False positive correction — risk-off
    risk_off_fp_rate = snapshot.get("risk_off_false_positive_rate")
    risk_off_fp_thresh = snapshot.get("risk_off_fp_threshold", 0.15)
    if risk_off_fp_rate is not None and risk_off_fp_rate > risk_off_fp_thresh:
        dlog.evaluation_notes.append("risk_off_fp_threshold_breach")
        dlog.record(23, "eval_fp_risk_off",
                    {"risk_off_false_positive_rate": risk_off_fp_rate,
                     "threshold": risk_off_fp_thresh},
                    "adjust_risk_off_thresholds",
                    "risk_off_fp_rate_too_high")
        # TODO:DATA_DEPENDENCY — flag bearish threshold for human-supervised recalibration

    # Warning precision
    warn_precision = snapshot.get("warning_precision")
    warn_threshold = snapshot.get("warning_precision_threshold", 0.60)
    if warn_precision is not None:
        if warn_precision > warn_threshold:
            dlog.evaluation_notes.append("warning_precision_high")
            dlog.record(23, "eval_warning_precision",
                        {"warning_precision": warn_precision},
                        "increase_warning_weight_flag",
                        "warning_precision_above_threshold")
            # TODO:DATA_DEPENDENCY — flag warning weights for increase
        else:
            dlog.evaluation_notes.append("warning_precision_low")
            dlog.record(23, "eval_warning_precision",
                        {"warning_precision": warn_precision},
                        "decrease_warning_weight_flag",
                        "warning_precision_below_threshold")
            # TODO:DATA_DEPENDENCY — flag warning weights for decrease


# ============================================================
# GATE 24 — OUTPUT CONTROLS
# ============================================================

def gate_24_output_controls(snapshot: dict, dlog: AggregatorDecisionLog):
    """GATE 24 — OUTPUT CONTROLS: maps classification to output_action."""
    action_map = {
        "strong_risk_on_signal":     "emit_strong_risk_on_state",
        "strong_risk_off_signal":    "emit_strong_risk_off_state",
        "cautious_risk_on_signal":   "emit_cautious_risk_on_state",
        "unstable_risk_off_signal":  "emit_unstable_risk_off_state",
        "transition_state":          "emit_transition_state",
        "stagflation_stress_signal": "emit_stagflation_stress_state",
        "euphoria_warning_signal":   "emit_euphoria_warning",
        "deleveraging_alert":        "emit_deleveraging_alert",
        "panic_alert":               "emit_panic_alert",
        "suppress_or_escalate":      "suppress_release_or_escalate",
        "no_clear_market_state":     "emit_uncertain_market_state",
    }
    dlog.output_action = action_map.get(dlog.classification, "emit_uncertain_market_state")
    dlog.record(24, "output_controls",
                {"classification": dlog.classification},
                dlog.output_action,
                f"output_action_from_{dlog.classification}")


# ============================================================
# GATE 25 — FINAL COMPOSITE MARKET-STATE SIGNAL
# ============================================================

def gate_25_final_signal(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 25 — FINAL COMPOSITE MARKET-STATE SIGNAL
    Computes final_market_state_signal and assigns final_market_state.

    base = aggregate_market_score × aggregate_confidence
    If aggregate_market_score > 0, apply bullish_strength_multiplier to base.
    final = base - warn_penalty + persistence_adj + trend_adj + validation_adj
    Clamped to [-1.0, +1.0].
    Override classification checked first.
    """
    # Override classification takes priority
    if dlog.classification == "panic_alert":
        dlog.final_market_state = "panic_override"
        dlog.record(25, "final_state_override",
                    {"classification": dlog.classification},
                    "panic_override", "panic_alert_override_active")
        dlog.final_market_state_signal = -1.0
        dlog.record(25, "final_signal",
                    {"final_market_state_signal": -1.0},
                    "-1.0", "panic_override_signal_clamp")
        return

    if dlog.classification == "deleveraging_alert":
        dlog.final_market_state = "deleveraging_override"
        dlog.record(25, "final_state_override",
                    {"classification": dlog.classification},
                    "deleveraging_override", "deleveraging_alert_override_active")
        dlog.final_market_state_signal = -1.0
        dlog.record(25, "final_signal",
                    {"final_market_state_signal": -1.0},
                    "-1.0", "deleveraging_override_signal_clamp")
        return

    if dlog.classification == "suppress_or_escalate":
        dlog.final_market_state = "blocked_override"
        dlog.record(25, "final_state_override",
                    {"classification": dlog.classification},
                    "blocked_override", "suppress_or_escalate_override_active")
        dlog.final_market_state_signal = 0.0
        dlog.record(25, "final_signal",
                    {"final_market_state_signal": 0.0},
                    "0.0", "blocked_override_signal_zero")
        return

    # Compute base signal
    score = dlog.aggregate_market_score or 0.0
    conf  = dlog.aggregate_confidence   or 0.5
    base  = score * conf
    if score > 0:
        base = base * dlog.bullish_strength_multiplier

    warn_penalty = WARN_PENALTY * len(dlog.warning_states)

    # Persistence adjustment
    persistence_adj = 0.0
    ps = dlog.persistence_state
    if ps == "persistent_risk_on":
        persistence_adj = 0.05
    elif ps == "persistent_risk_off":
        persistence_adj = -0.05
    elif ps == "unstable_regime":
        persistence_adj = -0.08

    # Trend adjustment
    trend_adj = 0.0
    ts = dlog.aggregate_trend_state
    if ts == "improving":
        trend_adj = 0.03
    elif ts == "worsening":
        trend_adj = -0.03

    # Validation adjustment
    validation_adj = 0.0
    vs = dlog.validation_state
    if vs in {"blocked", "systemic_failure"}:
        validation_adj = -0.25
    elif vs == "failed":
        validation_adj = -0.10
    elif vs == "caution":
        validation_adj = -0.05

    signal = base - warn_penalty + persistence_adj + trend_adj + validation_adj
    signal = round(max(-1.0, min(1.0, signal)), 4)
    dlog.final_market_state_signal = signal

    dlog.record(25, "final_signal",
                {"base": round(base, 4),
                 "warn_penalty": round(warn_penalty, 4),
                 "persistence_adj": persistence_adj,
                 "trend_adj": trend_adj,
                 "validation_adj": validation_adj,
                 "bullish_strength_multiplier": dlog.bullish_strength_multiplier,
                 "final_market_state_signal": signal},
                str(signal), "final_market_state_signal_calculated")

    # Score-based final state
    if signal >= FINAL_STRONG_RISK_ON:
        dlog.final_market_state = "strong_risk_on"
        dlog.record(25, "final_state",
                    {"signal": signal, "threshold": FINAL_STRONG_RISK_ON},
                    "strong_risk_on", "signal_strongly_risk_on")
    elif signal >= FINAL_RISK_ON:
        dlog.final_market_state = "mild_risk_on"
        dlog.record(25, "final_state",
                    {"signal": signal,
                     "risk_on_threshold": FINAL_RISK_ON,
                     "strong_risk_on_threshold": FINAL_STRONG_RISK_ON},
                    "mild_risk_on", "signal_mildly_risk_on")
    elif signal >= FINAL_NEUTRAL_LOW:
        dlog.final_market_state = "neutral"
        dlog.record(25, "final_state",
                    {"signal": signal,
                     "neutral_low": FINAL_NEUTRAL_LOW,
                     "neutral_high": FINAL_RISK_ON},
                    "neutral", "signal_neutral")
    elif signal > FINAL_STRONG_RISK_OFF:
        dlog.final_market_state = "mild_risk_off"
        dlog.record(25, "final_state",
                    {"signal": signal,
                     "strong_risk_off_threshold": FINAL_STRONG_RISK_OFF},
                    "mild_risk_off", "signal_mildly_risk_off")
    else:
        dlog.final_market_state = "strong_risk_off"
        dlog.record(25, "final_state",
                    {"signal": signal,
                     "threshold": FINAL_STRONG_RISK_OFF},
                    "strong_risk_off", "signal_strongly_risk_off")


# ============================================================
# GATE 26 — DOWNSTREAM ROUTING CONTROLS
# ============================================================

def gate_26_downstream_routing(snapshot: dict, dlog: AggregatorDecisionLog):
    """
    GATE 26 — DOWNSTREAM ROUTING CONTROLS
    Maps final_market_state to a downstream route and bias instruction.
    """
    route_map = {
        "strong_risk_on":       "trade_logic_agent: pro_risk_bias",
        "mild_risk_on":         "trade_logic_agent: cautious_pro_risk_bias",
        "neutral":              "trade_logic_agent: neutral_bias",
        "mild_risk_off":        "trade_logic_agent: defensive_bias",
        "strong_risk_off":      "trade_logic_agent: high_defensive_bias",
        "panic_override":       "trade_logic_agent: crisis_protocol",
        "deleveraging_override":"trade_logic_agent: forced_deleveraging_protocol",
        "blocked_override":     "validator_stack_agent OR suppress",
    }
    dlog.downstream_route = route_map.get(
        dlog.final_market_state,
        "trade_logic_agent: neutral_bias"
    )
    dlog.record(26, "downstream_routing",
                {"final_market_state": dlog.final_market_state},
                dlog.downstream_route,
                f"route_from_{dlog.final_market_state}")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def aggregate_market_state(snapshot: dict) -> dict:
    """
    Orchestrates the 26-gate master market-state aggregation pipeline.

    Args:
        snapshot: dict with keys:
            snapshot_id                  — identifier for this snapshot
            timestamp                    — ISO UTC timestamp
            SPX_feed_status              — "online" or other
            macro_agent_output           — dict from Agent 8 (or None)
            sentiment_agent_output       — dict from Sentiment Agent (or None; TODO)
            flow_agent_output            — dict from Flow Agent (or None; TODO)
            news_agent_output            — dict from Agent 2 (or None)
            rumor_agent_output           — dict from Agent 4 (or None)
            benchmark_state_output       — dict with SPX metrics (or None; TODO)
            validator_output             — dict from Agent 7 (or None)
            processed_snapshot_store     — list of previously seen SHA-256 hashes
            upstream_quality_scores      — list of floats (optional; TODO)
            upstream_confidence_scores   — list of floats (optional; TODO)
            coordinated_risk_on_consecutive_windows  — int (optional; TODO)
            coordinated_risk_off_consecutive_windows — int (optional; TODO)
            indecisive_transition_consecutive_windows— int (optional; TODO)
            state_change_count           — int (optional; TODO)
            aggregate_market_score_prev  — float (optional; TODO)
            aggregate_prediction_accuracy— bool (optional; TODO)

    Returns:
        Complete decision log dict from AggregatorDecisionLog.to_dict().
    """
    snapshot_id = (snapshot or {}).get("snapshot_id", "unknown")
    dlog        = AggregatorDecisionLog(snapshot_id)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Upstream availability
    gate_2_upstream_availability(snapshot, dlog)

    # Gate 3: Benchmark anchor
    gate_3_benchmark_anchor(snapshot, dlog)

    # Gates 4-9: Input mapping
    gate_4_macro_mapping(snapshot, dlog)
    gate_5_sentiment_mapping(snapshot, dlog)
    gate_6_flow_mapping(snapshot, dlog)
    gate_7_news_mapping(snapshot, dlog)
    gate_8_rumor_mapping(snapshot, dlog)
    gate_9_validator_mapping(snapshot, dlog)

    # Gates 10-12: Alignment controls
    gate_10_directional_alignment(snapshot, dlog)
    gate_11_information_alignment(snapshot, dlog)
    gate_12_benchmark_consistency(snapshot, dlog)

    # Gate 13: Composite component scoring
    gate_13_composite_scoring(snapshot, dlog)

    # Gate 14: Dynamic weighting
    gate_14_weighting(snapshot, dlog)

    # Gate 15: Composite market-state score
    gate_15_composite_score(snapshot, dlog)

    # Gate 16: Confidence
    gate_16_confidence(snapshot, dlog)

    # Gate 17: Divergence warnings
    gate_17_divergence_warnings(snapshot, dlog)

    # Gate 18: Market regime classification
    gate_18_market_regime(snapshot, dlog)

    # Gate 19: Override controls
    gate_19_overrides(snapshot, dlog)

    # Gate 20: Action classification
    gate_20_action_classification(snapshot, dlog)

    # Gate 21: Risk discounts
    gate_21_risk_discounts(snapshot, dlog)

    # Gate 22: Temporal persistence
    gate_22_temporal_persistence(snapshot, dlog)

    # Gate 23: Evaluation loop
    gate_23_evaluation_loop(snapshot, dlog)

    # Gate 24: Output controls
    gate_24_output_controls(snapshot, dlog)

    # Gate 25: Final composite signal
    gate_25_final_signal(snapshot, dlog)

    # Gate 26: Downstream routing
    gate_26_downstream_routing(snapshot, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_aggregator_log(result: dict):
    """Writes the aggregator decision log to the database event log."""
    _db.log_event(
        event_type="market_state_aggregator_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity market-state classifications.
    """
    cls = result.get("classification")
    sid = result.get("snapshot_id")

    if cls == "panic_alert":
        _db.post_suggestion(
            agent="market_state_aggregator",
            classification="panic_alert",
            risk_level="HIGH",
            payload=result,
            note="Drawdown active with panic sentiment. Immediate risk review required.",
        )
        log.warning("ESCALATE: panic_alert for snapshot %s", sid)

    elif cls == "suppress_or_escalate":
        _db.post_suggestion(
            agent="market_state_aggregator",
            classification="suppress_or_escalate",
            risk_level="HIGH",
            payload=result,
            note="Validator blocked or systemic failure. Suppress output and review.",
        )
        log.warning("ESCALATE: suppress_or_escalate for snapshot %s", sid)

    elif cls == "deleveraging_alert":
        _db.post_suggestion(
            agent="market_state_aggregator",
            classification="deleveraging_alert",
            risk_level="HIGH",
            payload=result,
            note="Forced deleveraging pattern detected. Review exposure immediately.",
        )
        log.warning("ESCALATE: deleveraging_alert for snapshot %s", sid)

    elif cls == "stagflation_stress_signal":
        _db.post_suggestion(
            agent="market_state_aggregator",
            classification="stagflation_stress_signal",
            risk_level="HIGH",
            payload=result,
            note="Stagflationary regime with bearish sentiment. Review risk controls.",
        )
        log.warning("ESCALATE: stagflation_stress_signal for snapshot %s", sid)

    elif cls == "euphoria_warning_signal":
        _db.post_suggestion(
            agent="market_state_aggregator",
            classification="euphoria_warning_signal",
            risk_level="MEDIUM",
            payload=result,
            note=f"Euphoric market regime with fragile benchmark underpinning. "
                 f"Warnings: {result.get('warning_states')}",
        )
        log.warning("ESCALATE: euphoria_warning_signal for snapshot %s", sid)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Master Market-State Aggregator — 26-gate top-level integration spine"
    )
    parser.add_argument("--snapshot-id", default="cli-run",
                        help="Snapshot ID for this run")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Minimal test snapshot — macro + news + rumor active, benchmark online
    test_snapshot = {
        "snapshot_id": args.snapshot_id,
        "timestamp":   datetime.datetime.utcnow().isoformat(),
        "SPX_feed_status": "online",
        "processed_snapshot_store": [],
        # Benchmark (embedded in top level for test; Gate 3 checks benchmark_state_output first)
        "benchmark_state_output": {
            "MA_short_SPX":     5050.0,
            "MA_long_SPX":      4900.0,
            "SPX_current":      5100.0,
            "rolling_peak_SPX": 5200.0,
            "realized_vol_SPX": 0.14,
            "VIX":              17.0,
            "ROC_SPX":          0.02,
        },
        # Agent 8 output (macro)
        "macro_agent_output": {
            "final_macro_state": "mild_expansion",
            "macro_confidence":  0.72,
            "warning_states":    [],
        },
        # Agent 2 output (news)
        "news_agent_output": {
            "classification":    "bullish_signal",
            "overall_confidence": 0.65,
            "confirmation_state": "confirmed",
        },
        # Agent 4 output (rumor)
        "rumor_agent_output": {
            "classification":    "bullish_rumor_signal",
            "overall_confidence": 0.55,
            "confirmation_state": "provisional",
        },
        # Agent 7 output (validator)
        "validator_output": {
            "master_classification": "pass",
        },
        # Sentinel / flow agents not yet built
        "sentiment_agent_output": None,
        "flow_agent_output":      None,
    }

    result = aggregate_market_state(test_snapshot)

    if args.output == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        dlog = AggregatorDecisionLog(result["snapshot_id"])
        # Reload state from result dict for human-readable output
        for k, v in result.items():
            if hasattr(dlog, k):
                setattr(dlog, k, v)
        print(dlog.to_human_readable())


if __name__ == "__main__":
    main()
