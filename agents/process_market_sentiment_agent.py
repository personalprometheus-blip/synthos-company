#!/usr/bin/env python3
"""
market_sentiment_agent.py — Agent 7.4
Market Sentiment Agent. 10-gate deterministic, rule-based sentiment spine.

Classifies market sentiment from price, volatility, and flow data combined
with news and rumor context. Produces a directional sentiment state for the
Master Market-State Aggregator.

Gates 1-3  : System validation, panic detection, euphoria detection.
Gates 4-6  : Price momentum scoring, volatility regime, flow indicators.
Gates 7-8  : News context integration, rumor context integration (discounted).
Gate 9     : Composite sentiment score construction.
Gate 10    : Classification — overrides first (panic/euphoria), then score-based.
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
log = logging.getLogger("market_sentiment_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION = "1.0.0"

# Gate 1 — System
MAX_SNAPSHOT_AGE_MINUTES = 60

# Gate 2 — Panic detection
PANIC_VIX_THRESHOLD    = 35.0
PANIC_RETURN_THRESHOLD = -0.03      # -3% 1d return

# Gate 3 — Euphoria detection
EUPHORIA_VIX_THRESHOLD    = 12.0
EUPHORIA_RETURN_THRESHOLD = 0.025   # +2.5% 1d return

# Gate 10 — Score classification thresholds
STRONG_BULLISH_THRESHOLD = 0.60
MILD_BULLISH_THRESHOLD   = 0.20
MILD_BEARISH_THRESHOLD   = -0.20
STRONG_BEARISH_THRESHOLD = -0.60

# Gate 5 — Volatility regime
VOL_HIGH_THRESHOLD = 0.25
VOL_LOW_THRESHOLD  = 0.12

# Gate 6 — Flow indicators
PUT_CALL_BEARISH_THRESHOLD = 1.20
PUT_CALL_BULLISH_THRESHOLD = 0.75

# Gates 7-8 — Context weights (applied to composite before normalization)
NEWS_CONTEXT_WEIGHT  = 0.25
RUMOR_CONTEXT_WEIGHT = 0.15

# Confidence thresholds
HIGH_CONFIDENCE_THRESHOLD = 0.70
LOW_CONFIDENCE_THRESHOLD  = 0.35

# ============================================================
# COMPONENT WEIGHT TABLE
# Base weights for price momentum, vol, flow (must sum to 1.0 before context)
# Context layers (news, rumor) shift the composite additively after weighting.
# ============================================================

# Gate 4 — Price momentum sub-weights
MOMENTUM_1D_WEIGHT = 0.60
MOMENTUM_5D_WEIGHT = 0.40

# Gate 5 — Vol discount factor when high-vol regime is active
VOL_HIGH_DISCOUNT = 0.80    # multiplier applied to momentum component score

# Gate 6 — Flow sub-weights
FLOW_PUT_CALL_WEIGHT    = 0.55
FLOW_ADV_DECLINE_WEIGHT = 0.45

# Base component weights for gates 4-6 composite (used in gate 9)
BASE_COMPONENT_WEIGHTS = {
    "price_momentum": 0.50,
    "flow":           0.35,
    "vol_regime":     0.15,
}

# ============================================================
# SCORING LOOKUP TABLES
# Positive = bullish / risk-on; Negative = bearish / risk-off
# ============================================================

# Price return 1d breakpoints → partial scores
#   Each entry: (threshold, score_if_return >= threshold)
#   Evaluated in descending threshold order; first match wins.
RETURN_1D_SCORE_TABLE = [
    ( 0.020,  1.0),    # >= +2.0%  : strongly bullish
    ( 0.010,  0.6),    # >= +1.0%  : moderately bullish
    ( 0.003,  0.2),    # >= +0.3%  : mildly bullish
    (-0.003,  0.0),    # >= -0.3%  : neutral
    (-0.010, -0.2),    # >= -1.0%  : mildly bearish
    (-0.020, -0.6),    # >= -2.0%  : moderately bearish
    (  None, -1.0),    # < -2.0%   : strongly bearish
]

RETURN_5D_SCORE_TABLE = [
    ( 0.040,  1.0),
    ( 0.020,  0.6),
    ( 0.005,  0.2),
    (-0.005,  0.0),
    (-0.020, -0.2),
    (-0.040, -0.6),
    (  None, -1.0),
]

# Advance-decline ratio → directional score
ADV_DECLINE_SCORE_TABLE = [
    (1.50,  1.0),
    (1.20,  0.5),
    (0.95,  0.0),
    (0.80, -0.5),
    (None, -1.0),
]

# Volume ratio (vs average): > 1 = elevated volume (amplifies direction, not scored standalone)
# Used in confidence adjustment only — not a standalone scoring gate.


def _lookup_score(value: float, table: list) -> float:
    """
    Resolve a score from a descending-threshold breakpoint table.
    Table entries: (threshold, score). The last entry uses threshold=None
    as the catch-all fallback.
    """
    for threshold, score in table:
        if threshold is None:
            return score
        if value >= threshold:
            return score
    return 0.0


# ============================================================
# SENTIMENT DECISION LOG
# ============================================================

class SentimentDecisionLog:
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

        # Gate 2 — panic
        self.panic_triggered  = False
        self.panic_vix        = None
        self.panic_return_1d  = None

        # Gate 3 — euphoria
        self.euphoria_triggered  = False
        self.euphoria_vix        = None
        self.euphoria_return_1d  = None

        # Gate 4 — price momentum
        self.momentum_score_1d    = None
        self.momentum_score_5d    = None
        self.momentum_score       = None

        # Gate 5 — vol regime
        self.vol_regime           = None    # "high" | "normal" | "low"
        self.vol_discount_applied = False
        self.vol_score            = None    # directional vol contribution

        # Gate 6 — flow indicators
        self.put_call_score   = None
        self.adv_decline_score= None
        self.flow_score       = None

        # Gate 7 — news context
        self.news_contribution = None
        self.news_available    = False

        # Gate 8 — rumor context
        self.rumor_contribution = None
        self.rumor_available    = False

        # Gate 9 — composite
        self.component_scores  = {}
        self.composite_score   = None

        # Gate 10 — classification
        self.final_market_state    = None
        self.sentiment_confidence  = None
        self.warning_state         = []
        self.override_applied      = None

    # ----------------------------------------------------------

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
            "agent":               "market_sentiment_agent",
            "agent_version":       AGENT_VERSION,
            "run_id":              self.run_id,
            "timestamp":           self.timestamp,
            "halted":              self.halted,
            "halt_reason":         self.halt_reason,
            # Gate 2/3 override flags
            "panic_triggered":     self.panic_triggered,
            "panic_vix":           self.panic_vix,
            "panic_return_1d":     self.panic_return_1d,
            "euphoria_triggered":  self.euphoria_triggered,
            "euphoria_vix":        self.euphoria_vix,
            "euphoria_return_1d":  self.euphoria_return_1d,
            "override_applied":    self.override_applied,
            # Gate 4
            "momentum_score_1d":   self.momentum_score_1d,
            "momentum_score_5d":   self.momentum_score_5d,
            "momentum_score":      self.momentum_score,
            # Gate 5
            "vol_regime":          self.vol_regime,
            "vol_discount_applied":self.vol_discount_applied,
            "vol_score":           self.vol_score,
            # Gate 6
            "put_call_score":      self.put_call_score,
            "adv_decline_score":   self.adv_decline_score,
            "flow_score":          self.flow_score,
            # Gates 7-8
            "news_available":      self.news_available,
            "news_contribution":   self.news_contribution,
            "rumor_available":     self.rumor_available,
            "rumor_contribution":  self.rumor_contribution,
            # Gate 9
            "component_scores":    self.component_scores,
            "composite_score":     self.composite_score,
            # Gate 10 — primary outputs
            "final_market_state":  self.final_market_state,
            "sentiment_confidence":self.sentiment_confidence,
            "warning_state":       self.warning_state,
            "decision_log":        self.records,
        }


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: SentimentDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates required inputs and snapshot freshness.
    Halt codes: no_market_input, stale_snapshot.
    Returns True if the run should continue; False if halted.
    """
    normalized_market = snapshot.get("normalized_market")
    run_id            = snapshot.get("run_id")
    timestamp_str     = snapshot.get("timestamp")

    # Check 1: required top-level keys
    if not normalized_market or not isinstance(normalized_market, dict):
        dlog.halt(1, "no_market_input",
                  {"normalized_market": normalized_market})
        return False

    if not run_id:
        dlog.halt(1, "no_market_input",
                  {"run_id": run_id, "error": "missing_run_id"})
        return False

    # Check 2: required market fields present
    required_market_fields = [
        "price_return_1d", "price_return_5d", "realized_vol_20d",
        "vix_level", "put_call_ratio", "advance_decline_ratio", "volume_ratio",
    ]
    missing = [f for f in required_market_fields if normalized_market.get(f) is None]
    if missing:
        dlog.halt(1, "no_market_input",
                  {"missing_fields": missing})
        return False

    # Check 3: timestamp present
    if not timestamp_str:
        dlog.halt(1, "no_market_input",
                  {"timestamp": None, "error": "missing_timestamp"})
        return False

    # Check 4: timestamp freshness
    try:
        snap_time   = datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
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
        dlog.halt(1, "no_market_input",
                  {"timestamp": timestamp_str, "error": f"unparseable_timestamp: {exc}"})
        return False

    dlog.record(1, "system_gate",
                {"run_id":      run_id,
                 "age_minutes": round(age_minutes, 1),
                 "fields_ok":   len(required_market_fields)},
                "pass", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — PANIC DETECTION
# ============================================================

def gate_2_panic_detection(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 2 — PANIC DETECTION  [NON-HALTING OVERRIDE]
    Triggers panic_override when VIX is extreme AND 1d return is deeply negative.
    Sets dlog.panic_triggered; does not halt the pipeline.
    """
    market   = snapshot["normalized_market"]
    vix      = market["vix_level"]
    ret_1d   = market["price_return_1d"]

    dlog.panic_vix       = vix
    dlog.panic_return_1d = ret_1d

    if vix > PANIC_VIX_THRESHOLD and ret_1d < PANIC_RETURN_THRESHOLD:
        dlog.panic_triggered = True
        dlog.warning_state.append("panic_conditions_detected")
        dlog.record(2, "panic_detection",
                    {"vix_level":       vix,
                     "price_return_1d": ret_1d,
                     "panic_vix_threshold":    PANIC_VIX_THRESHOLD,
                     "panic_return_threshold": PANIC_RETURN_THRESHOLD},
                    "panic_override_set", "vix_and_return_breach")
    else:
        dlog.record(2, "panic_detection",
                    {"vix_level":       vix,
                     "price_return_1d": ret_1d},
                    "no_panic", "thresholds_not_breached")


# ============================================================
# GATE 3 — EUPHORIA DETECTION
# ============================================================

def gate_3_euphoria_detection(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 3 — EUPHORIA DETECTION  [NON-HALTING OVERRIDE]
    Triggers euphoric_warning_override when VIX is suppressed AND 1d return
    is strongly positive (complacency + momentum divergence risk).
    Sets dlog.euphoria_triggered; does not halt the pipeline.
    """
    market   = snapshot["normalized_market"]
    vix      = market["vix_level"]
    ret_1d   = market["price_return_1d"]

    dlog.euphoria_vix       = vix
    dlog.euphoria_return_1d = ret_1d

    if vix < EUPHORIA_VIX_THRESHOLD and ret_1d > EUPHORIA_RETURN_THRESHOLD:
        dlog.euphoria_triggered = True
        dlog.warning_state.append("euphoria_conditions_detected")
        dlog.record(3, "euphoria_detection",
                    {"vix_level":         vix,
                     "price_return_1d":   ret_1d,
                     "euphoria_vix_threshold":    EUPHORIA_VIX_THRESHOLD,
                     "euphoria_return_threshold": EUPHORIA_RETURN_THRESHOLD},
                    "euphoric_warning_override_set", "vix_suppressed_and_return_elevated")
    else:
        dlog.record(3, "euphoria_detection",
                    {"vix_level":       vix,
                     "price_return_1d": ret_1d},
                    "no_euphoria", "thresholds_not_breached")


# ============================================================
# GATE 4 — PRICE MOMENTUM
# ============================================================

def gate_4_price_momentum(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 4 — PRICE MOMENTUM
    Scores price_return_1d and price_return_5d into a combined directional
    momentum score on [-1.0, 1.0].
    """
    market = snapshot["normalized_market"]
    ret_1d = market["price_return_1d"]
    ret_5d = market["price_return_5d"]

    score_1d = _lookup_score(ret_1d, RETURN_1D_SCORE_TABLE)
    score_5d = _lookup_score(ret_5d, RETURN_5D_SCORE_TABLE)

    momentum_score = (score_1d * MOMENTUM_1D_WEIGHT) + (score_5d * MOMENTUM_5D_WEIGHT)
    momentum_score = max(-1.0, min(1.0, momentum_score))

    dlog.momentum_score_1d = round(score_1d, 4)
    dlog.momentum_score_5d = round(score_5d, 4)
    dlog.momentum_score    = round(momentum_score, 4)

    dlog.record(4, "price_momentum",
                {"price_return_1d": ret_1d,
                 "price_return_5d": ret_5d,
                 "score_1d":        score_1d,
                 "score_5d":        score_5d,
                 "momentum_score":  round(momentum_score, 4)},
                f"momentum_score={round(momentum_score, 4)}",
                "price_momentum_scored")


# ============================================================
# GATE 5 — VOLATILITY REGIME
# ============================================================

def gate_5_volatility_regime(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 5 — VOLATILITY REGIME
    Classifies volatility as high / normal / low using realized_vol_20d and
    vix_level. A high-vol regime applies a discount multiplier to the
    momentum component score (captured in dlog for gate 9 consumption).
    The vol regime also contributes a small directional score: low vol is
    mildly bullish (complacency / carry-friendly), high vol is mildly bearish.
    """
    market    = snapshot["normalized_market"]
    real_vol  = market["realized_vol_20d"]
    vix       = market["vix_level"]

    # Classify regime using the higher of realized vol or vix-implied vol
    # VIX is annualised in percentage points (e.g. 18 = 18%); normalise to decimal
    vix_decimal = vix / 100.0

    effective_vol = max(real_vol, vix_decimal)

    if effective_vol >= VOL_HIGH_THRESHOLD:
        vol_regime = "high"
        vol_score  = -0.50   # high vol = bearish tilt in flow/sentiment context
        dlog.vol_discount_applied = True
        # Apply discount to momentum score already set in gate 4
        if dlog.momentum_score is not None:
            dlog.momentum_score = round(dlog.momentum_score * VOL_HIGH_DISCOUNT, 4)
        reason = "high_vol_regime_discount_applied"
    elif effective_vol <= VOL_LOW_THRESHOLD:
        vol_regime = "low"
        vol_score  = 0.30    # suppressed vol = slight bullish / carry-friendly
        reason     = "low_vol_regime"
    else:
        vol_regime = "normal"
        vol_score  = 0.0
        reason     = "normal_vol_regime"

    dlog.vol_regime = vol_regime
    dlog.vol_score  = round(vol_score, 4)

    dlog.record(5, "volatility_regime",
                {"realized_vol_20d":   real_vol,
                 "vix_level":          vix,
                 "vix_decimal":        round(vix_decimal, 4),
                 "effective_vol":      round(effective_vol, 4),
                 "vol_high_threshold": VOL_HIGH_THRESHOLD,
                 "vol_low_threshold":  VOL_LOW_THRESHOLD},
                f"vol_regime={vol_regime}  vol_score={round(vol_score, 4)}",
                reason)


# ============================================================
# GATE 6 — FLOW INDICATORS
# ============================================================

def gate_6_flow_indicators(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 6 — FLOW INDICATORS
    Scores put_call_ratio (inverse — high P/C = bearish) and
    advance_decline_ratio (higher = broader participation = bullish).
    Combines into a single flow score on [-1.0, 1.0].
    """
    market   = snapshot["normalized_market"]
    put_call = market["put_call_ratio"]
    adv_dec  = market["advance_decline_ratio"]

    # Put/call: high ratio is bearish, low ratio is bullish
    if put_call >= PUT_CALL_BEARISH_THRESHOLD:
        pc_score = -1.0
        pc_state = "bearish_hedging"
    elif put_call <= PUT_CALL_BULLISH_THRESHOLD:
        pc_score =  1.0
        pc_state = "bullish_complacency"
    else:
        # Linear interpolation between the two thresholds
        spread = PUT_CALL_BEARISH_THRESHOLD - PUT_CALL_BULLISH_THRESHOLD
        pos    = (put_call - PUT_CALL_BULLISH_THRESHOLD) / spread
        pc_score = 1.0 - 2.0 * pos   # maps [bullish=1.0 ... bearish=-1.0]
        pc_state = "neutral_flow"

    pc_score = max(-1.0, min(1.0, pc_score))

    # Advance-decline ratio
    adv_score = _lookup_score(adv_dec, ADV_DECLINE_SCORE_TABLE)

    flow_score = (pc_score * FLOW_PUT_CALL_WEIGHT) + (adv_score * FLOW_ADV_DECLINE_WEIGHT)
    flow_score = max(-1.0, min(1.0, flow_score))

    dlog.put_call_score    = round(pc_score, 4)
    dlog.adv_decline_score = round(adv_score, 4)
    dlog.flow_score        = round(flow_score, 4)

    dlog.record(6, "flow_indicators",
                {"put_call_ratio":       put_call,
                 "advance_decline_ratio":adv_dec,
                 "pc_score":             round(pc_score, 4),
                 "pc_state":             pc_state,
                 "adv_score":            round(adv_score, 4),
                 "flow_score":           round(flow_score, 4)},
                f"flow_score={round(flow_score, 4)}",
                "flow_indicators_scored")


# ============================================================
# GATE 7 — NEWS CONTEXT
# ============================================================

def _extract_context_contribution(context_dict: dict, weight: float, label: str) -> float:
    """
    Shared helper for gates 7 and 8.
    Extracts a directional contribution from a context agent output dict.
    Expects keys: classification (str) and overall_confidence (float [0,1]).
    Returns a weighted score contribution on [-weight, +weight].
    """
    classification  = context_dict.get("classification", "neutral")
    confidence      = float(context_dict.get("overall_confidence", 0.5))
    confidence      = max(0.0, min(1.0, confidence))

    cls_lower = classification.lower()

    if "strong_bullish" in cls_lower or "very_positive" in cls_lower or "strongly_positive" in cls_lower:
        direction =  1.0
    elif "bullish" in cls_lower or "positive" in cls_lower:
        direction =  0.6
    elif "mild_bullish" in cls_lower or "mildly_positive" in cls_lower:
        direction =  0.3
    elif "strong_bearish" in cls_lower or "very_negative" in cls_lower or "strongly_negative" in cls_lower:
        direction = -1.0
    elif "bearish" in cls_lower or "negative" in cls_lower:
        direction = -0.6
    elif "mild_bearish" in cls_lower or "mildly_negative" in cls_lower:
        direction = -0.3
    else:
        direction =  0.0   # neutral / unknown

    contribution = direction * confidence * weight
    return round(contribution, 4)


def gate_7_news_context(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 7 — NEWS CONTEXT
    If news_output is present, extract directional sentiment contribution
    weighted by NEWS_CONTEXT_WEIGHT and the output's overall_confidence.
    Stores contribution in dlog.news_contribution.
    """
    news_output = snapshot.get("news_output")

    if not news_output or not isinstance(news_output, dict):
        dlog.news_available    = False
        dlog.news_contribution = 0.0
        dlog.record(7, "news_context",
                    {"news_output": None},
                    "skipped", "no_news_output_provided")
        return

    contribution = _extract_context_contribution(news_output, NEWS_CONTEXT_WEIGHT, "news")

    dlog.news_available    = True
    dlog.news_contribution = contribution

    if abs(contribution) >= NEWS_CONTEXT_WEIGHT * HIGH_CONFIDENCE_THRESHOLD:
        dlog.warning_state.append("news_context_high_conviction")

    dlog.record(7, "news_context",
                {"classification":    news_output.get("classification"),
                 "overall_confidence":news_output.get("overall_confidence"),
                 "news_weight":        NEWS_CONTEXT_WEIGHT,
                 "contribution":       contribution},
                f"contribution={contribution}",
                "news_context_integrated")


# ============================================================
# GATE 8 — RUMOR CONTEXT
# ============================================================

def gate_8_rumor_context(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 8 — RUMOR CONTEXT
    If rumor_output is present, extract directional contribution weighted by
    RUMOR_CONTEXT_WEIGHT (discounted relative to news) and overall_confidence.
    Stores contribution in dlog.rumor_contribution.
    """
    rumor_output = snapshot.get("rumor_output")

    if not rumor_output or not isinstance(rumor_output, dict):
        dlog.rumor_available    = False
        dlog.rumor_contribution = 0.0
        dlog.record(8, "rumor_context",
                    {"rumor_output": None},
                    "skipped", "no_rumor_output_provided")
        return

    contribution = _extract_context_contribution(rumor_output, RUMOR_CONTEXT_WEIGHT, "rumor")

    dlog.rumor_available    = True
    dlog.rumor_contribution = contribution

    if abs(contribution) >= RUMOR_CONTEXT_WEIGHT * HIGH_CONFIDENCE_THRESHOLD:
        dlog.warning_state.append("rumor_context_high_conviction")

    dlog.record(8, "rumor_context",
                {"classification":    rumor_output.get("classification"),
                 "overall_confidence":rumor_output.get("overall_confidence"),
                 "rumor_weight":       RUMOR_CONTEXT_WEIGHT,
                 "contribution":       contribution},
                f"contribution={contribution}",
                "rumor_context_integrated")


# ============================================================
# GATE 9 — COMPOSITE SENTIMENT SCORE
# ============================================================

def gate_9_composite_score(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 9 — COMPOSITE SENTIMENT SCORE
    Combines gates 4-8 into a single weighted composite sentiment score.

    Structure:
      - Core score  = weighted sum of price_momentum, flow, vol_regime
                      using BASE_COMPONENT_WEIGHTS (sum = 1.0).
      - Context     = news_contribution + rumor_contribution added directly
                      (these are pre-weighted absolute contributions).
      - Final score is clamped to [-1.0, 1.0].

    Volume ratio is used as an amplifier on the core score
    (elevated volume confirms directional signal; subdued volume discounts it).
    """
    market       = snapshot["normalized_market"]
    volume_ratio = market.get("volume_ratio", 1.0)

    # Gather component scores
    price_momentum = dlog.momentum_score  if dlog.momentum_score  is not None else 0.0
    flow           = dlog.flow_score      if dlog.flow_score       is not None else 0.0
    vol_component  = dlog.vol_score       if dlog.vol_score        is not None else 0.0

    # Weighted core
    w = BASE_COMPONENT_WEIGHTS
    core_score = (
        price_momentum * w["price_momentum"] +
        flow           * w["flow"]           +
        vol_component  * w["vol_regime"]
    )

    # Volume amplifier: > 1.0 amplifies by up to 10%; < 0.8 discounts by up to 10%
    vol_ratio_clamped = max(0.5, min(2.0, volume_ratio))
    if vol_ratio_clamped >= 1.0:
        vol_amp = 1.0 + 0.10 * min(1.0, (vol_ratio_clamped - 1.0))
    else:
        vol_amp = 1.0 - 0.10 * min(1.0, (1.0 - vol_ratio_clamped) / 0.5)

    core_score_amplified = core_score * vol_amp

    # Add context contributions (additive, pre-weighted)
    news_contribution  = dlog.news_contribution  if dlog.news_contribution  is not None else 0.0
    rumor_contribution = dlog.rumor_contribution if dlog.rumor_contribution is not None else 0.0

    composite = core_score_amplified + news_contribution + rumor_contribution
    composite = max(-1.0, min(1.0, composite))

    dlog.component_scores = {
        "price_momentum":   round(price_momentum, 4),
        "flow":             round(flow, 4),
        "vol_regime":       round(vol_component, 4),
        "news_contribution":round(news_contribution, 4),
        "rumor_contribution":round(rumor_contribution, 4),
    }
    dlog.composite_score = round(composite, 4)

    dlog.record(9, "composite_score",
                {"core_score":           round(core_score, 4),
                 "vol_amp":              round(vol_amp, 4),
                 "core_score_amplified": round(core_score_amplified, 4),
                 "news_contribution":    round(news_contribution, 4),
                 "rumor_contribution":   round(rumor_contribution, 4),
                 "composite_score":      round(composite, 4)},
                f"composite={round(composite, 4)}",
                "composite_score_constructed")


# ============================================================
# GATE 10 — CLASSIFICATION
# ============================================================

def _compute_confidence(dlog: SentimentDecisionLog, snapshot: dict) -> float:
    """
    Derive sentiment_confidence [0.0, 1.0] from:
      1. Distance of composite score from nearest threshold boundary.
      2. Agreement between price momentum and flow direction.
      3. Volume ratio confirmation.
      4. Context alignment (news/rumor direction vs composite direction).
    """
    composite    = dlog.composite_score or 0.0
    market       = snapshot["normalized_market"]
    volume_ratio = market.get("volume_ratio", 1.0)

    # 1. Score distance from nearest threshold boundary
    boundaries  = [STRONG_BEARISH_THRESHOLD, MILD_BEARISH_THRESHOLD,
                   MILD_BULLISH_THRESHOLD,   STRONG_BULLISH_THRESHOLD]
    min_dist    = min(abs(composite - b) for b in boundaries)
    dist_factor = min(1.0, min_dist / 0.20)   # full confidence at 0.20 away from boundary

    # 2. Directional agreement between price momentum and flow
    momentum = dlog.momentum_score or 0.0
    flow     = dlog.flow_score     or 0.0
    agree    = 1.0 if (momentum * flow > 0) else (0.5 if (momentum == 0 or flow == 0) else 0.0)

    # 3. Volume confirmation (ratio >= 1.0 confirms the directional move)
    vol_ratio_clamped = max(0.5, min(2.0, volume_ratio))
    if vol_ratio_clamped >= 1.0:
        vol_conf = min(1.0, 0.7 + 0.3 * min(1.0, vol_ratio_clamped - 1.0))
    else:
        vol_conf = max(0.4, vol_ratio_clamped)

    # 4. Context alignment
    context_alignment = 1.0
    if dlog.news_available and dlog.news_contribution is not None:
        same_dir = (composite * dlog.news_contribution > 0)
        context_alignment *= (1.05 if same_dir else 0.90)
    if dlog.rumor_available and dlog.rumor_contribution is not None:
        same_dir = (composite * dlog.rumor_contribution > 0)
        context_alignment *= (1.03 if same_dir else 0.95)
    context_alignment = min(1.10, context_alignment)   # cap upside

    # Override conditions simplify confidence: clear threshold breach
    if dlog.panic_triggered or dlog.euphoria_triggered:
        base = 0.90
    else:
        base = (dist_factor * 0.40 + agree * 0.30 + vol_conf * 0.30)

    confidence = base * context_alignment
    return max(0.0, min(1.0, round(confidence, 4)))


def gate_10_classification(snapshot: dict, dlog: SentimentDecisionLog):
    """
    GATE 10 — CLASSIFICATION
    Maps composite score to final_market_state.
    Override precedence: panic_override > euphoric_warning_override > score-based.

    States:
      panic_override            — gates 2 panic condition met
      euphoric_warning_override — gate 3 euphoria condition met
      strong_bullish            — composite >= STRONG_BULLISH_THRESHOLD
      mild_bullish              — composite >= MILD_BULLISH_THRESHOLD
      neutral                   — between MILD thresholds
      mild_bearish              — composite <= MILD_BEARISH_THRESHOLD
      strong_bearish            — composite <= STRONG_BEARISH_THRESHOLD
    """
    composite = dlog.composite_score or 0.0

    # --- Override check (panic takes precedence over euphoria) ---
    if dlog.panic_triggered:
        final_state     = "panic_override"
        override_reason = "panic_vix_and_return_breach"
        dlog.override_applied = "panic_override"
    elif dlog.euphoria_triggered:
        final_state     = "euphoric_warning_override"
        override_reason = "euphoria_vix_suppressed_return_elevated"
        dlog.override_applied = "euphoric_warning_override"
    else:
        override_reason = None
        # Score-based classification
        if composite >= STRONG_BULLISH_THRESHOLD:
            final_state = "strong_bullish"
        elif composite >= MILD_BULLISH_THRESHOLD:
            final_state = "mild_bullish"
        elif composite > MILD_BEARISH_THRESHOLD:
            final_state = "neutral"
        elif composite > STRONG_BEARISH_THRESHOLD:
            final_state = "mild_bearish"
        else:
            final_state = "strong_bearish"

    # Confidence calculation
    sentiment_confidence = _compute_confidence(dlog, snapshot)

    # Append low-confidence warning if applicable
    if sentiment_confidence < LOW_CONFIDENCE_THRESHOLD:
        dlog.warning_state.append("low_confidence_classification")

    dlog.final_market_state   = final_state
    dlog.sentiment_confidence = sentiment_confidence

    dlog.record(10, "classification",
                {"composite_score":      composite,
                 "panic_triggered":      dlog.panic_triggered,
                 "euphoria_triggered":   dlog.euphoria_triggered,
                 "override_applied":     dlog.override_applied,
                 "override_reason":      override_reason,
                 "final_market_state":   final_state,
                 "sentiment_confidence": sentiment_confidence},
                f"final_market_state={final_state}",
                override_reason or "score_based_classification")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    run_agent — Market Sentiment Agent pipeline orchestrator.

    Accepts snapshot dict with keys:
        normalized_market : dict, required — price_return_1d, price_return_5d,
                            realized_vol_20d, vix_level, put_call_ratio,
                            advance_decline_ratio, volume_ratio
        news_output       : dict, optional — News Agent output
        rumor_output      : dict, optional — Social Rumor Agent output
        run_id            : str, required
        timestamp         : str, required — ISO UTC

    Returns:
        Complete decision log dict produced by SentimentDecisionLog.to_dict(),
        always containing: final_market_state, sentiment_confidence,
        warning_state, decision_log.
    """
    run_id = snapshot.get("run_id", "unknown")
    dlog   = SentimentDecisionLog(run_id)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Panic detection (non-halting override flag)
    gate_2_panic_detection(snapshot, dlog)

    # Gate 3: Euphoria detection (non-halting override flag)
    gate_3_euphoria_detection(snapshot, dlog)

    # Gate 4: Price momentum scoring
    gate_4_price_momentum(snapshot, dlog)

    # Gate 5: Volatility regime — may discount momentum score from gate 4
    gate_5_volatility_regime(snapshot, dlog)

    # Gate 6: Flow indicators
    gate_6_flow_indicators(snapshot, dlog)

    # Gate 7: News context (optional)
    gate_7_news_context(snapshot, dlog)

    # Gate 8: Rumor context (optional, discounted)
    gate_8_rumor_context(snapshot, dlog)

    # Gate 9: Composite sentiment score
    gate_9_composite_score(snapshot, dlog)

    # Gate 10: Classification — overrides first, then score-based
    gate_10_classification(snapshot, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_sentiment_log(result: dict):
    """Writes the market sentiment decision log to the database event log."""
    _db.log_event(
        event_type="market_sentiment_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-priority sentiment states.
    """
    state  = result.get("final_market_state")
    run_id = result.get("run_id")

    if state == "panic_override":
        _db.post_suggestion(
            agent="market_sentiment_agent",
            classification="panic_override",
            risk_level="HIGH",
            payload=result,
            note="Panic conditions detected: VIX breach + deep negative 1d return. Immediate review required.",
        )
        log.warning("ESCALATE: panic_override for run_id %s", run_id)

    elif state == "strong_bearish":
        _db.post_suggestion(
            agent="market_sentiment_agent",
            classification="strong_bearish",
            risk_level="HIGH",
            payload=result,
            note=f"Strong bearish sentiment confirmed. Composite={result.get('composite_score')}.",
        )
        log.warning("ESCALATE: strong_bearish for run_id %s", run_id)

    elif state == "euphoric_warning_override":
        _db.post_suggestion(
            agent="market_sentiment_agent",
            classification="euphoric_warning_override",
            risk_level="MEDIUM",
            payload=result,
            note="Euphoric conditions detected: VIX suppressed with strong 1d return. Review complacency risk.",
        )
        log.warning("ESCALATE: euphoric_warning_override for run_id %s", run_id)

    warnings = result.get("warning_state", [])
    if "panic_conditions_detected" not in warnings and "low_confidence_classification" in warnings:
        _db.post_suggestion(
            agent="market_sentiment_agent",
            classification="low_confidence_sentiment",
            risk_level="LOW",
            payload=result,
            note=f"Sentiment classification below confidence threshold. Warnings: {warnings}",
        )
        log.info("NOTE: low_confidence_classification for run_id %s", run_id)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Market Sentiment Agent 7.4 — 10-gate sentiment classification spine"
    )
    parser.add_argument("--run-id", default="cli-run",
                        help="Run ID for this execution")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    parser.add_argument("--scenario",
                        choices=["neutral", "panic", "euphoria", "bullish", "bearish"],
                        default="neutral",
                        help="Pre-built test scenario")
    args = parser.parse_args()

    # ---- Test scenario snapshots ----
    base_market = {
        "price_return_1d":      0.003,
        "price_return_5d":      0.010,
        "realized_vol_20d":     0.14,
        "vix_level":            18.0,
        "put_call_ratio":       0.95,
        "advance_decline_ratio":1.10,
        "volume_ratio":         1.05,
    }

    scenarios = {
        "neutral": dict(base_market),
        "panic": {
            **base_market,
            "price_return_1d": -0.040,
            "vix_level":       38.0,
            "realized_vol_20d":0.32,
            "put_call_ratio":  1.45,
            "advance_decline_ratio": 0.60,
            "volume_ratio":    1.80,
        },
        "euphoria": {
            **base_market,
            "price_return_1d": 0.030,
            "vix_level":       10.5,
            "realized_vol_20d":0.09,
            "put_call_ratio":  0.65,
            "advance_decline_ratio": 1.70,
            "volume_ratio":    1.40,
        },
        "bullish": {
            **base_market,
            "price_return_1d": 0.015,
            "price_return_5d": 0.030,
            "vix_level":       16.0,
            "put_call_ratio":  0.80,
            "advance_decline_ratio": 1.45,
            "volume_ratio":    1.20,
        },
        "bearish": {
            **base_market,
            "price_return_1d": -0.018,
            "price_return_5d": -0.035,
            "vix_level":       28.0,
            "realized_vol_20d":0.26,
            "put_call_ratio":  1.25,
            "advance_decline_ratio": 0.75,
            "volume_ratio":    1.30,
        },
    }

    test_snapshot = {
        "run_id":           args.run_id,
        "timestamp":        datetime.datetime.utcnow().isoformat(),
        "normalized_market":scenarios[args.scenario],
        "news_output": {
            "classification":    "mild_bullish",
            "overall_confidence":0.65,
        },
        "rumor_output": {
            "classification":    "neutral",
            "overall_confidence":0.45,
        },
    }

    log.info("Running market sentiment agent  run_id=%s  scenario=%s",
             args.run_id, args.scenario)

    result = run_agent(test_snapshot)
    write_sentiment_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"MARKET SENTIMENT AGENT v{AGENT_VERSION} — run_id: {result.get('run_id')}")
        print(f"{'='*72}")
        print(f"Final Market State   : {result.get('final_market_state')}")
        print(f"Sentiment Confidence : {result.get('sentiment_confidence')}")
        print(f"Override Applied     : {result.get('override_applied')}")
        print(f"Composite Score      : {result.get('composite_score')}")
        print(f"Vol Regime           : {result.get('vol_regime')}")
        print(f"Vol Discount Applied : {result.get('vol_discount_applied')}")
        print(f"Warning State        : {result.get('warning_state')}")
        print(f"\nComponent Scores:")
        for comp, score in sorted(result.get("component_scores", {}).items()):
            print(f"  {comp:22s}  {score:+.4f}")
        print(f"\nMomentum 1d          : {result.get('momentum_score_1d')}  "
              f"5d: {result.get('momentum_score_5d')}  "
              f"combined: {result.get('momentum_score')}")
        print(f"Flow Score           : {result.get('flow_score')}  "
              f"(pc={result.get('put_call_score')}  ad={result.get('adv_decline_score')})")
        print(f"News Available       : {result.get('news_available')}  "
              f"contribution: {result.get('news_contribution')}")
        print(f"Rumor Available      : {result.get('rumor_available')}  "
              f"contribution: {result.get('rumor_contribution')}")
        print(f"Halted               : {result.get('halted')}  "
              f"({result.get('halt_reason')})")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
