#!/usr/bin/env python3
"""
flow_positioning_agent.py — Agent 7.6
Positioning and Flow Agent. 10-gate deterministic, rule-based flow spine.

Classifies institutional positioning and flow conditions as supportive,
fragile, or destabilizing. Identifies squeeze and liquidation risk states
for the Master Market-State Aggregator.

Gates 1     : System validation, timestamp check.
Gates 2-3   : Squeeze detection, liquidation detection (override flags).
Gates 4-8   : Institutional flow, positioning, futures, options skew, ETF flows.
Gate 9      : Composite flow score construction.
Gate 10     : Classification — overrides first (squeeze/liquidation), then score-based.
"""

import sys
import os
import json
import logging
import argparse
import datetime
import math
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flow_positioning_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION = "1.0.0"

# Gate 1 — System
MAX_SNAPSHOT_AGE_MINUTES = 60

# Gate 2 — Squeeze detection
SQUEEZE_SHORT_INTEREST_THRESHOLD = 0.20    # short_interest_ratio > this = squeeze risk
SQUEEZE_POSITIONING_THRESHOLD    = -0.60   # positioning_score < this = crowded short

# Gate 3 — Liquidation detection
LIQUIDATION_MARGIN_THRESHOLD = 0.15        # margin_debt_change > this = forced selling
LIQUIDATION_FLOW_THRESHOLD   = -0.70       # net_institutional_flow < this = exit

# Gate 10 — Score classification thresholds
STRONG_SUPPORTIVE_THRESHOLD    =  0.60
MILD_SUPPORTIVE_THRESHOLD      =  0.20
MILD_FRAGILE_THRESHOLD         = -0.20
STRONG_DESTABILIZING_THRESHOLD = -0.60

# Gate 4 — Institutional flow scoring
NET_FLOW_HIGH_THRESHOLD = 0.50
NET_FLOW_LOW_THRESHOLD  = -0.50

# Gate 6 — Futures positioning scoring
FUTURES_BULLISH_THRESHOLD =  0.40
FUTURES_BEARISH_THRESHOLD = -0.40

# Gate 7 — Options skew scoring
OPTIONS_SKEW_BEARISH_THRESHOLD = 0.15     # high skew = bearish demand

# Confidence thresholds
HIGH_CONFIDENCE_THRESHOLD = 0.70
LOW_CONFIDENCE_THRESHOLD  = 0.35

# ============================================================
# COMPONENT WEIGHT TABLE
# Base weights for gates 4-8 composite (must sum to 1.0).
# ============================================================

BASE_COMPONENT_WEIGHTS = {
    "institutional_flow": 0.35,
    "positioning":        0.25,
    "futures":            0.20,
    "options_skew":       0.10,
    "etf_flows":          0.10,
}


# ============================================================
# FLOW DECISION LOG
# ============================================================

class FlowDecisionLog:
    """
    Accumulates all gate records and intermediate states produced during a
    single run_agent() execution. Serialises to a complete result dict.
    """

    def __init__(self, run_id: str):
        self.run_id    = run_id
        self.timestamp = datetime.datetime.utcnow().isoformat()
        self.records   = []

        # Gate 1 — system
        self.halted      = False
        self.halt_reason = None

        # Gate 2 — squeeze detection
        self.squeeze_override       = False
        self.squeeze_short_interest = None
        self.squeeze_positioning    = None

        # Gate 3 — liquidation detection
        self.liquidation_override      = False
        self.liquidation_margin_change = None
        self.liquidation_net_flow      = None

        # Gate 4 — institutional flow score
        self.institutional_flow_score = None

        # Gate 5 — positioning score
        self.positioning_score_raw      = None
        self.positioning_score_adjusted = None
        self.short_interest_discount    = None

        # Gate 6 — futures positioning score
        self.futures_score = None

        # Gate 7 — options skew score
        self.options_skew_score = None

        # Gate 8 — ETF flow score
        self.etf_flow_score           = None
        self.market_context_available = False

        # Gate 9 — composite
        self.component_scores = {}
        self.composite_score  = None

        # Gate 10 — classification
        self.final_flow_state  = None
        self.flow_confidence   = None
        self.warning_state     = []
        self.override_applied  = None

    # ----------------------------------------------------------

    def add_record(self, gate: int, name: str, inputs: dict, result: str, reason_code: str):
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
        self.add_record(gate, "HALT", inputs, "halted", reason_code)
        return {"halted": True, "reason": reason_code}

    def to_dict(self) -> dict:
        return {
            "agent":               "flow_positioning_agent",
            "agent_version":       AGENT_VERSION,
            "run_id":              self.run_id,
            "timestamp":           self.timestamp,
            "halted":              self.halted,
            "halt_reason":         self.halt_reason,
            # Gate 2/3 override flags
            "squeeze_override":              self.squeeze_override,
            "squeeze_short_interest":        self.squeeze_short_interest,
            "squeeze_positioning":           self.squeeze_positioning,
            "liquidation_override":          self.liquidation_override,
            "liquidation_margin_change":     self.liquidation_margin_change,
            "liquidation_net_flow":          self.liquidation_net_flow,
            "override_applied":              self.override_applied,
            # Gate 4
            "institutional_flow_score":      self.institutional_flow_score,
            # Gate 5
            "positioning_score_raw":         self.positioning_score_raw,
            "short_interest_discount":       self.short_interest_discount,
            "positioning_score_adjusted":    self.positioning_score_adjusted,
            # Gate 6
            "futures_score":                 self.futures_score,
            # Gate 7
            "options_skew_score":            self.options_skew_score,
            # Gate 8
            "etf_flow_score":                self.etf_flow_score,
            "market_context_available":      self.market_context_available,
            # Gate 9
            "component_scores":              self.component_scores,
            "composite_score":               self.composite_score,
            # Gate 10 — primary outputs
            "final_flow_state":              self.final_flow_state,
            "flow_confidence":               self.flow_confidence,
            "warning_state":                 self.warning_state,
            "decision_log":                  self.records,
        }


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: FlowDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates required inputs and snapshot freshness.
    Halt codes: no_flow_input, stale_snapshot.
    Returns True if the run should continue; False if halted.
    """
    normalized_flow = snapshot.get("normalized_flow")
    run_id          = snapshot.get("run_id")
    timestamp_str   = snapshot.get("timestamp")

    # Check 1: required top-level keys
    if not normalized_flow or not isinstance(normalized_flow, dict):
        dlog.halt(1, "no_flow_input",
                  {"normalized_flow": normalized_flow})
        return False

    if not run_id:
        dlog.halt(1, "no_flow_input",
                  {"run_id": run_id, "error": "missing_run_id"})
        return False

    # Check 2: required flow fields present
    required_flow_fields = [
        "net_institutional_flow", "positioning_score", "short_interest_ratio",
        "margin_debt_change", "etf_flow_ratio", "futures_positioning_net",
        "options_skew",
    ]
    missing = [f for f in required_flow_fields if normalized_flow.get(f) is None]
    if missing:
        dlog.halt(1, "no_flow_input",
                  {"missing_fields": missing})
        return False

    # Check 3: timestamp present
    if not timestamp_str:
        dlog.halt(1, "no_flow_input",
                  {"timestamp": None, "error": "missing_timestamp"})
        return False

    # Check 4: timestamp freshness
    try:
        snap_time = datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        # Normalise to naive UTC for comparison
        if snap_time.tzinfo is not None:
            snap_time = snap_time.replace(tzinfo=None) + datetime.timedelta(
                seconds=snap_time.utcoffset().total_seconds()
            )
        age_minutes = (datetime.datetime.utcnow() - snap_time).total_seconds() / 60.0
        if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
            dlog.halt(1, "stale_snapshot",
                      {"age_minutes":              round(age_minutes, 1),
                       "max_snapshot_age_minutes": MAX_SNAPSHOT_AGE_MINUTES})
            return False
    except (ValueError, TypeError, AttributeError) as exc:
        dlog.halt(1, "no_flow_input",
                  {"timestamp": timestamp_str, "error": f"unparseable_timestamp: {exc}"})
        return False

    dlog.add_record(1, "system_gate",
                    {"run_id":      run_id,
                     "age_minutes": round(age_minutes, 1),
                     "fields_ok":   len(required_flow_fields)},
                    "pass", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — SQUEEZE DETECTION
# ============================================================

def gate_2_squeeze_detection(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 2 — SQUEEZE DETECTION  [NON-HALTING OVERRIDE]
    Triggers squeeze_override when short_interest_ratio exceeds the squeeze
    threshold AND positioning_score is below the crowded-short threshold.
    Sets dlog.squeeze_override; does not halt the pipeline.
    Warning states appended: squeeze_risk, high_short_interest.
    """
    flow    = snapshot["normalized_flow"]
    sir     = flow["short_interest_ratio"]
    pos     = flow["positioning_score"]

    dlog.squeeze_short_interest = sir
    dlog.squeeze_positioning    = pos

    sir_breach = sir > SQUEEZE_SHORT_INTEREST_THRESHOLD
    pos_breach = pos < SQUEEZE_POSITIONING_THRESHOLD

    if sir_breach and pos_breach:
        dlog.squeeze_override = True
        dlog.warning_state.append("squeeze_risk")
        dlog.warning_state.append("high_short_interest")
        dlog.add_record(2, "squeeze_detection",
                        {"short_interest_ratio":          sir,
                         "positioning_score":             pos,
                         "squeeze_sir_threshold":         SQUEEZE_SHORT_INTEREST_THRESHOLD,
                         "squeeze_positioning_threshold": SQUEEZE_POSITIONING_THRESHOLD},
                        "squeeze_override_set",
                        "short_interest_and_positioning_breach")
    else:
        if sir_breach:
            dlog.warning_state.append("high_short_interest")
        dlog.add_record(2, "squeeze_detection",
                        {"short_interest_ratio": sir,
                         "positioning_score":    pos,
                         "sir_breach":           sir_breach,
                         "pos_breach":           pos_breach},
                        "no_squeeze",
                        "thresholds_not_simultaneously_breached")


# ============================================================
# GATE 3 — LIQUIDATION DETECTION
# ============================================================

def gate_3_liquidation_detection(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 3 — LIQUIDATION DETECTION  [NON-HALTING OVERRIDE]
    Triggers liquidation_override when margin_debt_change exceeds the forced-
    selling threshold AND net_institutional_flow is below the exit threshold.
    Sets dlog.liquidation_override; does not halt the pipeline.
    Warning states appended: liquidation_risk, margin_expansion.
    """
    flow           = snapshot["normalized_flow"]
    margin_change  = flow["margin_debt_change"]
    net_inst_flow  = flow["net_institutional_flow"]

    dlog.liquidation_margin_change = margin_change
    dlog.liquidation_net_flow      = net_inst_flow

    margin_breach = margin_change > LIQUIDATION_MARGIN_THRESHOLD
    flow_breach   = net_inst_flow < LIQUIDATION_FLOW_THRESHOLD

    if margin_breach and flow_breach:
        dlog.liquidation_override = True
        dlog.warning_state.append("liquidation_risk")
        dlog.warning_state.append("margin_expansion")
        dlog.add_record(3, "liquidation_detection",
                        {"margin_debt_change":          margin_change,
                         "net_institutional_flow":      net_inst_flow,
                         "liquidation_margin_threshold":LIQUIDATION_MARGIN_THRESHOLD,
                         "liquidation_flow_threshold":  LIQUIDATION_FLOW_THRESHOLD},
                        "liquidation_override_set",
                        "margin_and_flow_breach")
    else:
        if margin_breach:
            dlog.warning_state.append("margin_expansion")
        dlog.add_record(3, "liquidation_detection",
                        {"margin_debt_change":     margin_change,
                         "net_institutional_flow": net_inst_flow,
                         "margin_breach":          margin_breach,
                         "flow_breach":            flow_breach},
                        "no_liquidation",
                        "thresholds_not_simultaneously_breached")


# ============================================================
# GATE 4 — INSTITUTIONAL FLOW SCORE
# ============================================================

def gate_4_institutional_flow_score(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 4 — INSTITUTIONAL FLOW SCORE
    Scores net_institutional_flow (assumed normalised to [-1, 1]) into a
    directional flow score on [-1.0, 1.0] using NET_FLOW thresholds.
    Values above NET_FLOW_HIGH_THRESHOLD signal strong inflows (bullish);
    below NET_FLOW_LOW_THRESHOLD signal strong outflows (bearish); linear
    interpolation in between.
    """
    flow     = snapshot["normalized_flow"]
    net_flow = flow["net_institutional_flow"]

    if net_flow >= NET_FLOW_HIGH_THRESHOLD:
        inst_score = 1.0
        flow_state = "strong_inflow"
    elif net_flow <= NET_FLOW_LOW_THRESHOLD:
        inst_score = -1.0
        flow_state = "strong_outflow"
    else:
        # Linear interpolation across the full band
        spread     = NET_FLOW_HIGH_THRESHOLD - NET_FLOW_LOW_THRESHOLD
        pos        = (net_flow - NET_FLOW_LOW_THRESHOLD) / spread
        inst_score = -1.0 + 2.0 * pos
        flow_state = "moderate_flow"

    inst_score = max(-1.0, min(1.0, inst_score))
    dlog.institutional_flow_score = round(inst_score, 4)

    dlog.add_record(4, "institutional_flow_score",
                    {"net_institutional_flow":  net_flow,
                     "net_flow_high_threshold": NET_FLOW_HIGH_THRESHOLD,
                     "net_flow_low_threshold":  NET_FLOW_LOW_THRESHOLD,
                     "flow_state":              flow_state,
                     "institutional_flow_score":round(inst_score, 4)},
                    f"institutional_flow_score={round(inst_score, 4)}",
                    "institutional_flow_scored")


# ============================================================
# GATE 5 — POSITIONING SCORE
# ============================================================

def gate_5_positioning_score(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 5 — POSITIONING SCORE
    Normalises positioning_score (already in [-1, 1]) and applies a short-
    interest discount: elevated short_interest_ratio > SQUEEZE threshold shrinks
    the positioning signal toward zero by 20%, reflecting crowded-positioning
    uncertainty.  Appended warning: crowded_positioning if positioning_score < -0.40.
    """
    flow     = snapshot["normalized_flow"]
    pos_raw  = flow["positioning_score"]
    sir      = flow["short_interest_ratio"]

    pos_raw = max(-1.0, min(1.0, float(pos_raw)))

    # Short-interest discount
    if sir > SQUEEZE_SHORT_INTEREST_THRESHOLD:
        discount = 0.80   # 20% reduction in signal strength — crowded positioning uncertainty
        pos_adj  = pos_raw * discount
        discount_applied = True
    else:
        discount = 1.0
        pos_adj  = pos_raw
        discount_applied = False

    pos_adj = max(-1.0, min(1.0, pos_adj))

    if pos_raw < -0.40:
        dlog.warning_state.append("crowded_positioning")

    dlog.positioning_score_raw      = round(pos_raw, 4)
    dlog.short_interest_discount    = round(discount, 4)
    dlog.positioning_score_adjusted = round(pos_adj, 4)

    dlog.add_record(5, "positioning_score",
                    {"positioning_score_raw":          round(pos_raw, 4),
                     "short_interest_ratio":           sir,
                     "squeeze_sir_threshold":          SQUEEZE_SHORT_INTEREST_THRESHOLD,
                     "discount_applied":               discount_applied,
                     "short_interest_discount":        round(discount, 4),
                     "positioning_score_adjusted":     round(pos_adj, 4)},
                    f"positioning_score_adjusted={round(pos_adj, 4)}",
                    "positioning_scored_with_sir_discount" if discount_applied else "positioning_scored")


# ============================================================
# GATE 6 — FUTURES POSITIONING
# ============================================================

def gate_6_futures_positioning(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 6 — FUTURES POSITIONING
    Scores futures_positioning_net (assumed normalised to [-1, 1]) into a
    directional score on [-1.0, 1.0] using FUTURES thresholds.
    Above FUTURES_BULLISH_THRESHOLD = bullish commitment;
    below FUTURES_BEARISH_THRESHOLD = bearish commitment;
    linear interpolation in between.
    """
    flow          = snapshot["normalized_flow"]
    futures_net   = flow["futures_positioning_net"]

    if futures_net >= FUTURES_BULLISH_THRESHOLD:
        fut_score   = 1.0
        futures_state = "net_long"
    elif futures_net <= FUTURES_BEARISH_THRESHOLD:
        fut_score   = -1.0
        futures_state = "net_short"
    else:
        spread      = FUTURES_BULLISH_THRESHOLD - FUTURES_BEARISH_THRESHOLD
        pos         = (futures_net - FUTURES_BEARISH_THRESHOLD) / spread
        fut_score   = -1.0 + 2.0 * pos
        futures_state = "balanced"

    fut_score = max(-1.0, min(1.0, fut_score))
    dlog.futures_score = round(fut_score, 4)

    dlog.add_record(6, "futures_positioning",
                    {"futures_positioning_net":   futures_net,
                     "futures_bullish_threshold": FUTURES_BULLISH_THRESHOLD,
                     "futures_bearish_threshold": FUTURES_BEARISH_THRESHOLD,
                     "futures_state":             futures_state,
                     "futures_score":             round(fut_score, 4)},
                    f"futures_score={round(fut_score, 4)}",
                    "futures_positioning_scored")


# ============================================================
# GATE 7 — OPTIONS SKEW
# ============================================================

def gate_7_options_skew(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 7 — OPTIONS SKEW
    Scores options_skew directionally: high positive skew reflects elevated
    demand for downside puts (bearish signal).
    - skew > OPTIONS_SKEW_BEARISH_THRESHOLD : bearish, score ∝ -skew
    - skew near 0                           : neutral
    - skew < 0 (call skew)                  : mild bullish/complacency
    Score is clamped to [-1.0, 1.0].
    """
    flow  = snapshot["normalized_flow"]
    skew  = flow["options_skew"]

    if skew > OPTIONS_SKEW_BEARISH_THRESHOLD:
        # Normalise above the threshold; at threshold = -0.5, grows toward -1.0
        excess     = skew - OPTIONS_SKEW_BEARISH_THRESHOLD
        skew_score = -(0.5 + min(0.5, excess / OPTIONS_SKEW_BEARISH_THRESHOLD))
        skew_state = "bearish_put_demand"
    elif skew < 0:
        # Call skew — mild bullish / complacency signal, capped at +0.5
        skew_score = min(0.5, abs(skew) / OPTIONS_SKEW_BEARISH_THRESHOLD * 0.5)
        skew_state = "call_skew_complacency"
    else:
        # Between 0 and threshold: linear mild-bearish ramp
        skew_score = -(skew / OPTIONS_SKEW_BEARISH_THRESHOLD) * 0.5
        skew_state = "mild_put_bias"

    skew_score = max(-1.0, min(1.0, skew_score))
    dlog.options_skew_score = round(skew_score, 4)

    dlog.add_record(7, "options_skew",
                    {"options_skew":                 skew,
                     "options_skew_bearish_threshold":OPTIONS_SKEW_BEARISH_THRESHOLD,
                     "skew_state":                   skew_state,
                     "options_skew_score":           round(skew_score, 4)},
                    f"options_skew_score={round(skew_score, 4)}",
                    "options_skew_scored")


# ============================================================
# GATE 8 — ETF FLOWS
# ============================================================

def gate_8_etf_flows(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 8 — ETF FLOWS
    Scores etf_flow_ratio (positive = net inflows, negative = net outflows)
    into a directional score on [-1.0, 1.0].  If normalized_market context is
    available (vix_level, price_return_5d) the score is amplified when ETF
    flows confirm the market trend, or discounted when they diverge.
    """
    flow          = snapshot["normalized_flow"]
    etf_ratio     = flow["etf_flow_ratio"]
    market_ctx    = snapshot.get("normalized_market")

    # Base score: treat etf_flow_ratio as already normalised to [-1, 1]
    etf_score = max(-1.0, min(1.0, float(etf_ratio)))

    context_factor = 1.0
    context_note   = "no_market_context"
    market_used    = False

    if market_ctx and isinstance(market_ctx, dict):
        vix       = market_ctx.get("vix_level")
        ret_5d    = market_ctx.get("price_return_5d")
        if vix is not None and ret_5d is not None:
            market_used = True
            # Determine market directional tilt from context
            if ret_5d > 0 and vix < 25:
                market_direction =  1.0   # risk-on trending
            elif ret_5d < 0 and vix > 25:
                market_direction = -1.0   # risk-off trending
            else:
                market_direction =  0.0   # ambiguous

            if market_direction != 0.0:
                # Confirming flow: amplify by up to 15%
                # Diverging flow: discount by up to 15%
                same_dir = (etf_score * market_direction > 0)
                context_factor = 1.15 if same_dir else 0.85
                context_note   = "context_amplified" if same_dir else "context_discounted"
            else:
                context_note = "market_direction_ambiguous"

    etf_score_final = max(-1.0, min(1.0, etf_score * context_factor))
    dlog.etf_flow_score           = round(etf_score_final, 4)
    dlog.market_context_available = market_used

    dlog.add_record(8, "etf_flows",
                    {"etf_flow_ratio":     etf_ratio,
                     "base_etf_score":     round(etf_score, 4),
                     "market_context":     market_used,
                     "context_factor":     round(context_factor, 4),
                     "context_note":       context_note,
                     "etf_flow_score":     round(etf_score_final, 4)},
                    f"etf_flow_score={round(etf_score_final, 4)}",
                    "etf_flows_scored")


# ============================================================
# GATE 9 — COMPOSITE FLOW SCORE
# ============================================================

def gate_9_composite_score(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 9 — COMPOSITE FLOW SCORE
    Combines gate 4-8 component scores into a single weighted composite on
    [-1.0, 1.0] using BASE_COMPONENT_WEIGHTS (sum = 1.0).

    Components:
      institutional_flow  — gate 4  (weight 0.35)
      positioning         — gate 5  (weight 0.25, uses adjusted score)
      futures             — gate 6  (weight 0.20)
      options_skew        — gate 7  (weight 0.10)
      etf_flows           — gate 8  (weight 0.10)
    """
    inst_flow  = dlog.institutional_flow_score      if dlog.institutional_flow_score      is not None else 0.0
    positioning = dlog.positioning_score_adjusted   if dlog.positioning_score_adjusted    is not None else 0.0
    futures    = dlog.futures_score                 if dlog.futures_score                 is not None else 0.0
    opt_skew   = dlog.options_skew_score            if dlog.options_skew_score            is not None else 0.0
    etf_flows  = dlog.etf_flow_score                if dlog.etf_flow_score                is not None else 0.0

    w = BASE_COMPONENT_WEIGHTS
    composite = (
        inst_flow   * w["institutional_flow"] +
        positioning * w["positioning"]        +
        futures     * w["futures"]            +
        opt_skew    * w["options_skew"]       +
        etf_flows   * w["etf_flows"]
    )
    composite = max(-1.0, min(1.0, composite))

    dlog.component_scores = {
        "institutional_flow": round(inst_flow,   4),
        "positioning":        round(positioning, 4),
        "futures":            round(futures,     4),
        "options_skew":       round(opt_skew,    4),
        "etf_flows":          round(etf_flows,   4),
    }
    dlog.composite_score = round(composite, 4)

    dlog.add_record(9, "composite_score",
                    {"component_scores": dlog.component_scores,
                     "weights":          w,
                     "composite_score":  round(composite, 4)},
                    f"composite={round(composite, 4)}",
                    "composite_score_constructed")


# ============================================================
# GATE 10 — CLASSIFICATION
# ============================================================

def _compute_flow_confidence(dlog: FlowDecisionLog) -> float:
    """
    Derive flow_confidence [0.0, 1.0] from:
      1. Distance of composite score from nearest threshold boundary.
      2. Directional agreement between institutional flow and positioning scores.
      3. Futures confirmation of composite direction.
      4. Override presence (overrides carry high inherent confidence).
    """
    composite = dlog.composite_score or 0.0

    # 1. Score distance from nearest threshold boundary
    boundaries = [
        STRONG_DESTABILIZING_THRESHOLD,
        MILD_FRAGILE_THRESHOLD,
        MILD_SUPPORTIVE_THRESHOLD,
        STRONG_SUPPORTIVE_THRESHOLD,
    ]
    min_dist    = min(abs(composite - b) for b in boundaries)
    dist_factor = min(1.0, min_dist / 0.20)   # full confidence at 0.20 away from boundary

    # 2. Institutional flow vs adjusted positioning agreement
    inst  = dlog.institutional_flow_score      or 0.0
    pos   = dlog.positioning_score_adjusted    or 0.0
    agree = 1.0 if (inst * pos > 0) else (0.5 if (inst == 0 or pos == 0) else 0.0)

    # 3. Futures confirmation
    fut = dlog.futures_score or 0.0
    if fut * composite > 0:
        fut_confirm = 1.0
    elif fut == 0:
        fut_confirm = 0.6
    else:
        fut_confirm = 0.3

    # 4. Override presence signals a high-conviction regime
    if dlog.squeeze_override or dlog.liquidation_override:
        base = 0.90
    else:
        base = (dist_factor * 0.40 + agree * 0.35 + fut_confirm * 0.25)

    return max(0.0, min(1.0, round(base, 4)))


def gate_10_classification(snapshot: dict, dlog: FlowDecisionLog):
    """
    GATE 10 — CLASSIFICATION
    Maps composite score to final_flow_state.
    Override precedence: liquidation_override > squeeze_override > score-based.

    States:
      liquidation_override  — gate 3 conditions met: margin + outflow breach
      squeeze_override      — gate 2 conditions met: high short interest + crowded short
      strong_supportive     — composite >= STRONG_SUPPORTIVE_THRESHOLD
      mild_supportive       — composite >= MILD_SUPPORTIVE_THRESHOLD
      neutral               — between MILD thresholds
      mild_fragile          — composite <= MILD_FRAGILE_THRESHOLD
      strong_destabilizing  — composite <= STRONG_DESTABILIZING_THRESHOLD
    """
    composite = dlog.composite_score or 0.0

    # --- Override check: liquidation takes precedence over squeeze ---
    if dlog.liquidation_override:
        final_state     = "liquidation_override"
        override_reason = "margin_debt_and_institutional_flow_breach"
        dlog.override_applied = "liquidation_override"
    elif dlog.squeeze_override:
        final_state     = "squeeze_override"
        override_reason = "short_interest_and_crowded_positioning_breach"
        dlog.override_applied = "squeeze_override"
    else:
        override_reason = None
        # Score-based classification
        if composite >= STRONG_SUPPORTIVE_THRESHOLD:
            final_state = "strong_supportive"
        elif composite >= MILD_SUPPORTIVE_THRESHOLD:
            final_state = "mild_supportive"
        elif composite > MILD_FRAGILE_THRESHOLD:
            final_state = "neutral"
        elif composite > STRONG_DESTABILIZING_THRESHOLD:
            final_state = "mild_fragile"
        else:
            final_state = "strong_destabilizing"

    # Confidence calculation
    flow_confidence = _compute_flow_confidence(dlog)

    # Append low-confidence warning if applicable
    if flow_confidence < LOW_CONFIDENCE_THRESHOLD:
        dlog.warning_state.append("low_confidence_classification")

    dlog.final_flow_state = final_state
    dlog.flow_confidence  = flow_confidence

    dlog.add_record(10, "classification",
                    {"composite_score":       composite,
                     "squeeze_override":      dlog.squeeze_override,
                     "liquidation_override":  dlog.liquidation_override,
                     "override_applied":      dlog.override_applied,
                     "override_reason":       override_reason,
                     "final_flow_state":      final_state,
                     "flow_confidence":       flow_confidence},
                    f"final_flow_state={final_state}",
                    override_reason or "score_based_classification")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    run_agent — Flow Positioning Agent pipeline orchestrator.

    Accepts snapshot dict with keys:
        normalized_flow   : dict, required — net_institutional_flow,
                            positioning_score, short_interest_ratio,
                            margin_debt_change, etf_flow_ratio,
                            futures_positioning_net, options_skew
        normalized_market : dict, optional — market reference
                            (vix_level, price_return_5d)
        run_id            : str, required
        timestamp         : str, required — ISO UTC

    Returns:
        Complete decision log dict produced by FlowDecisionLog.to_dict(),
        always containing: final_flow_state, flow_confidence,
        warning_state, decision_log.
    """
    run_id = snapshot.get("run_id", "unknown")
    dlog   = FlowDecisionLog(run_id)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Squeeze detection (non-halting override flag)
    gate_2_squeeze_detection(snapshot, dlog)

    # Gate 3: Liquidation detection (non-halting override flag)
    gate_3_liquidation_detection(snapshot, dlog)

    # Gate 4: Institutional flow scoring
    gate_4_institutional_flow_score(snapshot, dlog)

    # Gate 5: Positioning score — normalise and apply short-interest discount
    gate_5_positioning_score(snapshot, dlog)

    # Gate 6: Futures positioning scoring
    gate_6_futures_positioning(snapshot, dlog)

    # Gate 7: Options skew scoring
    gate_7_options_skew(snapshot, dlog)

    # Gate 8: ETF flow scoring — incorporates market context if available
    gate_8_etf_flows(snapshot, dlog)

    # Gate 9: Composite flow score
    gate_9_composite_score(snapshot, dlog)

    # Gate 10: Classification — overrides first, then score-based
    gate_10_classification(snapshot, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_flow_log(result: dict):
    """Writes the flow positioning decision log to the database event log."""
    _db.log_event(
        event_type="flow_positioning_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-priority flow states.
    """
    state  = result.get("final_flow_state")
    run_id = result.get("run_id")

    if state == "liquidation_override":
        _db.post_suggestion(
            agent="flow_positioning_agent",
            classification="liquidation_override",
            risk_level="HIGH",
            payload=result,
            note="Liquidation conditions detected: margin expansion + institutional exit. Immediate review required.",
        )
        log.warning("ESCALATE: liquidation_override for run_id %s", run_id)

    elif state == "squeeze_override":
        _db.post_suggestion(
            agent="flow_positioning_agent",
            classification="squeeze_override",
            risk_level="HIGH",
            payload=result,
            note="Squeeze conditions detected: high short interest + crowded short positioning. Review squeeze risk.",
        )
        log.warning("ESCALATE: squeeze_override for run_id %s", run_id)

    elif state == "strong_destabilizing":
        _db.post_suggestion(
            agent="flow_positioning_agent",
            classification="strong_destabilizing",
            risk_level="HIGH",
            payload=result,
            note=f"Strong destabilizing flow confirmed. Composite={result.get('composite_score')}.",
        )
        log.warning("ESCALATE: strong_destabilizing for run_id %s", run_id)

    warnings = result.get("warning_state", [])
    if "liquidation_override" not in (result.get("override_applied") or "") and \
       "low_confidence_classification" in warnings:
        _db.post_suggestion(
            agent="flow_positioning_agent",
            classification="low_confidence_flow",
            risk_level="LOW",
            payload=result,
            note=f"Flow classification below confidence threshold. Warnings: {warnings}",
        )
        log.info("NOTE: low_confidence_classification for run_id %s", run_id)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Flow Positioning Agent 7.6 — 10-gate flow/positioning classification spine"
    )
    parser.add_argument("--run-id", default="cli-run",
                        help="Run ID for this execution")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    parser.add_argument("--scenario",
                        choices=["neutral", "squeeze", "liquidation", "supportive", "destabilizing"],
                        default="neutral",
                        help="Pre-built test scenario")
    args = parser.parse_args()

    # ---- Test scenario snapshots ----
    base_flow = {
        "net_institutional_flow":  0.10,
        "positioning_score":       0.05,
        "short_interest_ratio":    0.08,
        "margin_debt_change":      0.02,
        "etf_flow_ratio":          0.15,
        "futures_positioning_net": 0.10,
        "options_skew":            0.05,
    }

    scenarios = {
        "neutral": dict(base_flow),
        "squeeze": {
            **base_flow,
            "short_interest_ratio":    0.28,   # > SQUEEZE_SHORT_INTEREST_THRESHOLD
            "positioning_score":      -0.72,   # < SQUEEZE_POSITIONING_THRESHOLD
            "net_institutional_flow":  0.05,
            "futures_positioning_net":-0.50,
            "options_skew":            0.20,
        },
        "liquidation": {
            **base_flow,
            "margin_debt_change":      0.22,   # > LIQUIDATION_MARGIN_THRESHOLD
            "net_institutional_flow": -0.80,   # < LIQUIDATION_FLOW_THRESHOLD
            "positioning_score":      -0.50,
            "futures_positioning_net":-0.60,
            "etf_flow_ratio":         -0.70,
            "options_skew":            0.30,
        },
        "supportive": {
            **base_flow,
            "net_institutional_flow":  0.75,
            "positioning_score":       0.55,
            "futures_positioning_net": 0.60,
            "etf_flow_ratio":          0.65,
            "options_skew":           -0.05,
        },
        "destabilizing": {
            **base_flow,
            "net_institutional_flow": -0.65,
            "positioning_score":      -0.50,
            "futures_positioning_net":-0.55,
            "etf_flow_ratio":         -0.60,
            "options_skew":            0.25,
            "margin_debt_change":      0.08,
        },
    }

    flow_data = scenarios[args.scenario]

    snapshot = {
        "normalized_flow":   flow_data,
        "normalized_market": {
            "vix_level":        18.0,
            "price_return_5d":  0.012,
        },
        "run_id":    args.run_id,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }

    result = run_agent(snapshot)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Flow Positioning Agent 7.6  |  run_id: {result.get('run_id')}")
        print(f"  Scenario: {args.scenario}")
        print(f"{'='*60}")
        print(f"  final_flow_state : {result.get('final_flow_state')}")
        print(f"  flow_confidence  : {result.get('flow_confidence')}")
        print(f"  composite_score  : {result.get('composite_score')}")
        print(f"  override_applied : {result.get('override_applied')}")
        print(f"  warning_state    : {result.get('warning_state')}")
        print(f"  halted           : {result.get('halted')}")
        if result.get("halted"):
            print(f"  halt_reason      : {result.get('halt_reason')}")
        print(f"\n  Component Scores:")
        for k, v in (result.get("component_scores") or {}).items():
            print(f"    {k:<22}: {v:>7.4f}")
        print(f"\n  Gates run: {len(result.get('decision_log', []))}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
