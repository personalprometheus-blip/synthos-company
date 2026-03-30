#!/usr/bin/env python3
"""
macro_regime_agent.py — Agent 8
Macro regime classification spine. 23-gate deterministic, rule-based spine.
Benchmark context: S&P 500 (SPX).

Gates 1-2   : System validation and input universe activation.
Gates 3-13  : Regime component gates (benchmark, inflation, growth, labor,
              policy, yield curve, credit, liquidity, FX/global, commodity, news).
Gates 14-15 : Component scoring and dynamic weight adjustment.
Gates 16-23 : Regime scoring, confidence, divergence warnings, classification,
              persistence, evaluation loop, output, and final signal.
"""

import sys
import os
import json
import logging
import argparse
import datetime
import hashlib
import statistics
import math
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macro_regime_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

# Gate 1 — System Gate
MIN_REQUIRED_MACRO_INPUTS  = 3
MAX_SNAPSHOT_AGE_MINUTES   = 60

# Gate 3 — Benchmark (SPX)
SPX_DRAWDOWN_THRESHOLD = 0.10    # 10% drawdown from rolling peak
SPX_VOL_THRESHOLD      = 0.20    # 20% annualized realized vol
VIX_THRESHOLD          = 25.0

# Gate 4 — Inflation
INFLATION_HIGH_THRESHOLD       = 0.040   # 4.0% CPI YoY
INFLATION_LOW_THRESHOLD        = 0.020   # 2.0%
INFLATION_ACCEL_THRESHOLD      = 0.005   # 0.5 pp acceleration
INFLATION_DECEL_THRESHOLD      = 0.005
SERVICES_STICKY_THRESHOLD      = 0.050   # 5.0% services inflation
GOODS_DISINFLATION_THRESHOLD   = 0.010   # -1.0 pp goods inflation change
INFLATION_SURPRISE_THRESHOLD   = 0.002   # 0.2 pp surprise

# Gate 5 — Growth
GROWTH_STRONG_THRESHOLD           = 2.5    # % annualized GDP
GROWTH_WEAK_THRESHOLD             = 1.0
GROWTH_ACCEL_THRESHOLD            = 0.5    # pp change in nowcast
GROWTH_DECEL_THRESHOLD            = 0.5
MANUFACTURING_CONTRACTION_THRESHOLD = 50.0  # PMI
SERVICES_EXPANSION_THRESHOLD      = 50.0
RECESSION_PROB_THRESHOLD          = 0.30   # 30%

# Gate 6 — Labor
UNEMPLOYMENT_LOW_THRESHOLD      = 0.040   # 4.0%
LABOR_TIGHT_THRESHOLD           = 1.20    # job openings ratio
LABOR_LOOSEN_THRESHOLD          = 0.002   # 0.2 pp unemployment rise
PAYROLL_STRONG_THRESHOLD        = 200_000
PAYROLL_WEAK_THRESHOLD          = 75_000
WAGE_GROWTH_HIGH_THRESHOLD      = 0.045   # 4.5% AHE YoY
WAGE_GROWTH_MODERATION_THRESHOLD= 0.035   # 3.5%
CLAIMS_RISING_THRESHOLD         = 250_000

# Gate 7 — Policy
RESTRICTIVE_REAL_RATE_THRESHOLD    = 0.015   # 1.5%
ACCOMMODATIVE_REAL_RATE_THRESHOLD  = 0.000   # 0%
HIKING_CYCLE_THRESHOLD             = 0.0025  # 25 bps over 6 months
EASING_CYCLE_THRESHOLD             = 0.0025
QT_THRESHOLD                       = 50e9    # $50B balance sheet reduction
QE_THRESHOLD                       = 50e9    # $50B expansion
HAWKISH_SURPRISE_THRESHOLD         = 0.0010  # 10 bps
DOVISH_SURPRISE_THRESHOLD          = 0.0010

# Gate 8 — Yield Curve
CURVE_INVERSION_THRESHOLD  = 0.000    # spread < 0 = inverted
STEEPENING_THRESHOLD       = 0.0010   # 10 bps per period
BULL_STEEPENING_THRESHOLD  = 0.0010
BEAR_STEEPENING_THRESHOLD  = 0.0010
FRONTEND_STRESS_THRESHOLD  = 0.0050   # 50 bps move in 2y
TERM_PREMIUM_THRESHOLD     = 0.0010   # 10 bps term premium change

# Gate 9 — Credit
IG_SPREAD_WIDEN_THRESHOLD    = 0.0010   # 10 bps
HY_SPREAD_WIDEN_THRESHOLD    = 0.0050   # 50 bps
SPREAD_TIGHTENING_THRESHOLD  = 0.0010   # 10 bps tightening
DEFAULT_RISK_THRESHOLD       = 0.040    # 4.0% default rate
FUNDING_STRESS_THRESHOLD     = 0.0050   # 50 bps funding spread

# Gate 10 — Liquidity
LIQUIDITY_IMPROVING_THRESHOLD     = 50e9    # +$50B
LIQUIDITY_DETERIORATING_THRESHOLD = 50e9    # -$50B
RESERVE_TIGHT_THRESHOLD           = 3.0e12  # $3.0T reserves
MONEY_MARKET_STRESS_THRESHOLD     = 0.0020  # 20 bps money market spread
ISSUANCE_PRESSURE_THRESHOLD       = 100e9   # $100B net Treasury supply

# Gate 11 — FX / Global
USD_STRENGTH_THRESHOLD        = 0.02   # 2% DXY return
USD_WEAKNESS_THRESHOLD        = 0.02
EM_STRESS_THRESHOLD           = 0.70   # EM FX stress index
GLOBAL_STABILIZATION_THRESHOLD= 0.50   # global PMI change

# Gate 12 — Commodity
OIL_SPIKE_THRESHOLD         = 0.10    # 10% WTI return
METALS_GROWTH_THRESHOLD     = 0.05    # 5% industrial metals return
COMMODITY_WEAKNESS_THRESHOLD= -0.05   # -5% broad commodity
FOOD_PRESSURE_THRESHOLD     = 0.05    # 5% food index change

# Gate 13 — Macro News
MACRO_NEWS_POSITIVE_THRESHOLD = 0.30
MACRO_NEWS_NEGATIVE_THRESHOLD = 0.30   # applies as -0.30
HAWKISH_COMM_THRESHOLD        = 0.15   # hawkish term density
DOVISH_COMM_THRESHOLD         = 0.15
MIN_CONFIRMATIONS             = 2

# Gate 16 — Regime score thresholds
REGIME_EXPANSION_THRESHOLD   = 0.30
REGIME_CONTRACTION_THRESHOLD = -0.30

# Gate 17 — Confidence
MIN_CONFIDENT_COMPONENT_COUNT = 5
AGREEMENT_THRESHOLD           = 0.30
DISAGREEMENT_THRESHOLD        = 0.40
DATA_QUALITY_THRESHOLD        = 0.70

# Gate 19 — Classification
HIGH_CONF_THRESHOLD       = 0.70
LOW_CONF_THRESHOLD        = 0.40
DIVERGENCE_CONF_THRESHOLD = 0.50

# Gate 20 — Persistence
PERSISTENCE_THRESHOLD  = 3
FLIP_THRESHOLD         = 3
IMPROVEMENT_THRESHOLD  = 0.05
DETERIORATION_THRESHOLD= 0.05

# Gate 23 — Final signal thresholds
FINAL_STRONG_EXPANSION_THRESHOLD  =  0.60
FINAL_EXPANSION_THRESHOLD         =  0.20
FINAL_NEUTRAL_LOW                 = -0.15
FINAL_STRONG_CONTRACTION_THRESHOLD= -0.50
FINAL_WARN_PENALTY                =  0.05  # per active warning

# Base component weights (must sum to 1.0)
BASE_WEIGHTS = {
    "growth":    0.25,
    "policy":    0.20,
    "inflation": 0.15,
    "curve":     0.10,
    "credit":    0.10,
    "labor":     0.08,
    "liquidity": 0.05,
    "fx_global": 0.04,
    "commodity": 0.02,
    "news":      0.01,
}

# Dynamic weight upshift multipliers
INFLATION_WEIGHT_UPSHIFT = 1.30
CREDIT_WEIGHT_UPSHIFT    = 1.40
POLICY_WEIGHT_UPSHIFT    = 1.25
LIQUIDITY_WEIGHT_UPSHIFT = 1.30
DRAWDOWN_CREDIT_UPSHIFT  = 1.20

# ============================================================
# COMPONENT SCORING TABLES
# Positive = expansionary / supportive for risk assets
# Negative = contractionary / restrictive
# ============================================================

# Inflation sub-state scores (weights: level 0.40, trend 0.30, quality 0.20, surprise 0.10)
INFLATION_STATE_SCORES    = {"high": -1.0, "low":  1.0}
INFLATION_TREND_SCORES    = {"accelerating": -1.0, "decelerating":  1.0}
INFLATION_QUALITY_SCORES  = {"sticky_services": -0.5, "goods_disinflation": 0.5}
INFLATION_SURPRISE_SCORES = {"upside": -0.5, "downside": 0.5}
INFLATION_WEIGHTS         = (0.40, 0.30, 0.20, 0.10)

# Growth sub-state scores (weights: level 0.35, trend 0.30, quality 0.20, risk 0.15)
GROWTH_STATE_SCORES    = {"strong": 1.0, "weak": -1.0}
GROWTH_TREND_SCORES    = {"accelerating": 1.0, "decelerating": -1.0}
GROWTH_QUALITY_SCORES  = {"services_strength": 0.5, "manufacturing_weakness": -0.5}
GROWTH_RISK_SCORES     = {"recession_risk": -1.0}
GROWTH_WEIGHTS         = (0.35, 0.30, 0.20, 0.15)

# Labor sub-state scores (weights: tightness 0.25, momentum 0.35, wages 0.25, risk 0.15)
LABOR_STATE_SCORES    = {"tight": 0.5, "loosening": -0.5}
LABOR_MOMENTUM_SCORES = {"strong": 1.0, "weak": -1.0}
WAGE_STATE_SCORES     = {"inflationary": -0.5, "easing": 0.5}
LABOR_RISK_SCORES     = {"weakening": -1.0}
LABOR_WEIGHTS         = (0.25, 0.35, 0.25, 0.15)

# Policy sub-state scores (weights: stance 0.35, cycle 0.30, balance sheet 0.25, surprise 0.10)
POLICY_STATE_SCORES          = {"accommodative": 1.0, "restrictive": -1.0}
POLICY_TREND_SCORES          = {"easing": 1.0, "tightening": -1.0}
LIQUIDITY_POLICY_STATE_SCORES= {"easing": 1.0, "tightening": -1.0}
POLICY_SURPRISE_SCORES       = {"dovish": 0.5, "hawkish": -0.5}
POLICY_WEIGHTS               = (0.35, 0.30, 0.25, 0.10)

# Curve sub-state scores (weights: level 0.30, trend 0.25, regime 0.30, stress 0.15)
CURVE_STATE_SCORES        = {"inverted": -1.0}
CURVE_TREND_SCORES        = {"steepening": 0.5}
CURVE_REGIME_SCORES       = {"bull_steepener": 1.0, "bear_steepener": -0.5}
RATE_STRESS_SCORES        = {"frontend_stress": -1.0}
CURVE_WEIGHTS             = (0.30, 0.25, 0.30, 0.15)

# Credit sub-state scores (weights: direction 0.35, HY 0.25, default 0.20, funding 0.20)
CREDIT_STATE_SCORES         = {"improving": 1.0, "deteriorating": -1.0}
CREDIT_RISK_SCORES          = {"high_yield_stress": -1.0}
CREDIT_DEFAULT_SCORES       = {"elevated": -1.0}
FUNDING_LIQUIDITY_SCORES    = {"funding_stress": -1.0}
CREDIT_WEIGHTS              = (0.35, 0.25, 0.20, 0.20)

# Liquidity sub-state scores (weights: direction 0.50, quality 0.30, supply 0.20)
SYSTEM_LIQUIDITY_SCORES  = {"improving": 1.0, "deteriorating": -1.0}
LIQUIDITY_QUALITY_SCORES = {"tight_reserves": -0.5, "stressed": -1.0}
LIQUIDITY_SUPPLY_SCORES  = {"supply_pressure": -0.5}
LIQUIDITY_WEIGHTS        = (0.50, 0.30, 0.20)

# FX/Global sub-state scores (weights: dollar 0.25, conditions 0.35, EM 0.20, global growth 0.20)
FX_STATE_SCORES           = {"weak_dollar": 0.5, "strong_dollar": -0.5}
GLOBAL_PRESSURE_SCORES    = {"tightening_financial_conditions": -1.0}
GLOBAL_RISK_SCORES        = {"EM_stress": -0.5}
GLOBAL_GROWTH_SCORES      = {"stabilizing": 0.5}
FX_WEIGHTS                = (0.25, 0.35, 0.20, 0.20)

# Commodity single-state scores
COMMODITY_STATE_SCORES = {
    "growth_confirmation":   1.0,
    "deflationary_signal":  -0.5,
    "energy_inflation_risk": -0.5,
    "food_inflation_risk":   -0.3,
}

# News sub-state scores (weights: sentiment 0.35, policy comm 0.30, conviction 0.35)
NEWS_STATE_SCORES       = {"supportive": 1.0, "unsupportive": -1.0}
POLICY_COMM_SCORES      = {"dovish": 0.5, "hawkish": -0.5}
NEWS_CONVICTION_SCORES  = {"strong_positive": 1.0, "strong_negative": -1.0}
NEWS_WEIGHTS            = (0.35, 0.30, 0.35)

# Macro input fields used for usable_macro_input_count in Gate 1
MACRO_INPUT_KEYS = [
    "headline_CPI_yoy", "GDP_nowcast", "unemployment_rate",
    "real_policy_rate", "yield_10y", "IG_spread_change",
    "net_liquidity_change", "DXY_return", "WTI_return",
    "macro_news_sentiment_score",
]

# ============================================================
# REGIME DECISION LOG
# ============================================================

class RegimeDecisionLog:
    """
    Accumulates all gate records and component states produced during a
    single process_snapshot() run. Serialises to JSON and human-readable text.
    """

    def __init__(self, snapshot_id: str):
        self.snapshot_id = snapshot_id
        self.timestamp   = datetime.datetime.utcnow().isoformat()
        self.records     = []

        # Gate 1
        self.halted                    = False
        self.halt_reason               = None
        self.benchmark_context_disabled= False

        # Gate 2 — input availability
        self.input_states = {}

        # Gate 3 — benchmark states
        self.benchmark_state     = None
        self.benchmark_risk_state= None
        self.benchmark_vol_state = None

        # Gate 4 — inflation states
        self.inflation_state         = None
        self.inflation_trend_state   = None
        self.inflation_quality_state = None
        self.inflation_surprise_state= None

        # Gate 5 — growth states
        self.growth_state        = None
        self.growth_trend_state  = None
        self.growth_quality_state= None
        self.growth_risk_state   = None

        # Gate 6 — labor states
        self.labor_state         = None
        self.labor_momentum_state= None
        self.wage_state          = None
        self.labor_risk_state    = None

        # Gate 7 — policy states
        self.policy_state          = None
        self.policy_trend_state    = None
        self.liquidity_policy_state= None
        self.policy_surprise_state = None

        # Gate 8 — curve states
        self.curve_state       = None
        self.curve_trend_state = None
        self.curve_regime_state= None
        self.rate_stress_state = None
        self.long_end_state    = None

        # Gate 9 — credit states
        self.credit_state          = None
        self.credit_risk_state     = None
        self.credit_default_state  = None
        self.funding_liquidity_state= None

        # Gate 10 — liquidity states (system-level)
        self.system_liquidity_state = None
        self.liquidity_quality_state= None
        self.liquidity_supply_state = None

        # Gate 11 — FX / global states
        self.fx_state            = None
        self.global_pressure_state= None
        self.global_risk_state   = None
        self.global_growth_state = None

        # Gate 12 — commodity state
        self.commodity_state = None

        # Gate 13 — news states
        self.news_state          = None
        self.policy_comm_state   = None
        self.news_conviction_state= None

        # Gate 14 — component scores
        self.component_scores = {}

        # Gate 15 — weights (initialised from BASE_WEIGHTS; adjusted in Gate 15)
        self.component_weights = dict(BASE_WEIGHTS)

        # Gate 16 — regime
        self.macro_regime_score = None
        self.macro_regime_state = None

        # Gate 17 — confidence
        self.confidence_input_state= None
        self.confidence_state      = None
        self.data_confidence_state = None
        self.macro_confidence      = None

        # Gate 18 — divergence warnings
        self.warning_states = []

        # Gate 19 — classification
        self.classification = None

        # Gate 20 — persistence / trend
        self.persistence_state = None
        self.macro_trend_state = None

        # Gate 21 — evaluation notes
        self.evaluation_notes = []

        # Gate 22 — output
        self.output_action = None

        # Gate 23 — final signal
        self.final_macro_signal = None
        self.final_macro_state  = None

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
            "snapshot_id":            self.snapshot_id,
            "timestamp":              self.timestamp,
            "halted":                 self.halted,
            "halt_reason":            self.halt_reason,
            "benchmark_context_disabled": self.benchmark_context_disabled,
            "input_states":           self.input_states,
            "benchmark_state":        self.benchmark_state,
            "benchmark_risk_state":   self.benchmark_risk_state,
            "benchmark_vol_state":    self.benchmark_vol_state,
            "inflation_state":        self.inflation_state,
            "inflation_trend_state":  self.inflation_trend_state,
            "inflation_quality_state":self.inflation_quality_state,
            "inflation_surprise_state":self.inflation_surprise_state,
            "growth_state":           self.growth_state,
            "growth_trend_state":     self.growth_trend_state,
            "growth_quality_state":   self.growth_quality_state,
            "growth_risk_state":      self.growth_risk_state,
            "labor_state":            self.labor_state,
            "labor_momentum_state":   self.labor_momentum_state,
            "wage_state":             self.wage_state,
            "labor_risk_state":       self.labor_risk_state,
            "policy_state":           self.policy_state,
            "policy_trend_state":     self.policy_trend_state,
            "liquidity_policy_state": self.liquidity_policy_state,
            "policy_surprise_state":  self.policy_surprise_state,
            "curve_state":            self.curve_state,
            "curve_trend_state":      self.curve_trend_state,
            "curve_regime_state":     self.curve_regime_state,
            "rate_stress_state":      self.rate_stress_state,
            "long_end_state":         self.long_end_state,
            "credit_state":           self.credit_state,
            "credit_risk_state":      self.credit_risk_state,
            "credit_default_state":   self.credit_default_state,
            "funding_liquidity_state":self.funding_liquidity_state,
            "system_liquidity_state": self.system_liquidity_state,
            "liquidity_quality_state":self.liquidity_quality_state,
            "liquidity_supply_state": self.liquidity_supply_state,
            "fx_state":               self.fx_state,
            "global_pressure_state":  self.global_pressure_state,
            "global_risk_state":      self.global_risk_state,
            "global_growth_state":    self.global_growth_state,
            "commodity_state":        self.commodity_state,
            "news_state":             self.news_state,
            "policy_comm_state":      self.policy_comm_state,
            "news_conviction_state":  self.news_conviction_state,
            "component_scores":       self.component_scores,
            "component_weights":      self.component_weights,
            "macro_regime_score":     self.macro_regime_score,
            "macro_regime_state":     self.macro_regime_state,
            "confidence_input_state": self.confidence_input_state,
            "confidence_state":       self.confidence_state,
            "data_confidence_state":  self.data_confidence_state,
            "macro_confidence":       self.macro_confidence,
            "warning_states":         self.warning_states,
            "classification":         self.classification,
            "persistence_state":      self.persistence_state,
            "macro_trend_state":      self.macro_trend_state,
            "evaluation_notes":       self.evaluation_notes,
            "output_action":          self.output_action,
            "final_macro_signal":     self.final_macro_signal,
            "final_macro_state":      self.final_macro_state,
            "decision_log":           self.records,
        }

    def to_human_readable(self) -> str:
        lines = [
            "=" * 72,
            "MACRO REGIME AGENT — DECISION LOG",
            f"Snapshot ID    : {self.snapshot_id}",
            f"Timestamp      : {self.timestamp}",
            "=" * 72,
            f"\nMacro Regime State  : {self.macro_regime_state}",
            f"Classification      : {self.classification}",
            f"Output Action       : {self.output_action}",
            f"Macro Regime Score  : {self.macro_regime_score}",
            f"Macro Confidence    : {self.macro_confidence}",
            f"Final Macro Signal  : {self.final_macro_signal}",
            f"Final Macro State   : {self.final_macro_state}",
            f"\nWarnings            : {self.warning_states}",
            f"Persistence State   : {self.persistence_state}",
            f"Macro Trend State   : {self.macro_trend_state}",
            "\nComponent Scores:",
        ]
        for comp, score in sorted(self.component_scores.items()):
            weight = self.component_weights.get(comp, 0.0)
            lines.append(f"  {comp:12s}  score={score:+.4f}  weight={weight:.4f}")
        lines.append("\nComponent States:")
        lines.append(f"  inflation   : {self.inflation_state} / {self.inflation_trend_state} / {self.inflation_quality_state}")
        lines.append(f"  growth      : {self.growth_state} / {self.growth_trend_state} / {self.growth_quality_state}")
        lines.append(f"  labor       : {self.labor_state} / {self.labor_momentum_state} / {self.wage_state}")
        lines.append(f"  policy      : {self.policy_state} / {self.policy_trend_state} / {self.liquidity_policy_state}")
        lines.append(f"  curve       : {self.curve_state} / {self.curve_trend_state} / {self.curve_regime_state}")
        lines.append(f"  credit      : {self.credit_state} / {self.credit_risk_state}")
        lines.append(f"  liquidity   : {self.system_liquidity_state} / {self.liquidity_quality_state}")
        lines.append(f"  fx/global   : {self.fx_state} / {self.global_pressure_state}")
        lines.append(f"  commodity   : {self.commodity_state}")
        lines.append(f"  news        : {self.news_state} / {self.policy_comm_state}")
        lines.append(f"\nBenchmark (SPX) : state={self.benchmark_state}  risk={self.benchmark_risk_state}  vol={self.benchmark_vol_state}")
        lines.append("=" * 72)
        return "\n".join(lines)


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: RegimeDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates snapshot before any regime processing begins.
    Returns True if the run should continue; False if halted.
    """
    payload = snapshot.get("snapshot_payload", {})

    # Check 1: Macro data feed
    macro_status = snapshot.get("macro_data_status")
    if macro_status != "online":
        dlog.halt(1, "halt_regime_calc",
                  {"macro_data_status": macro_status})
        return False

    # Check 2: Benchmark feed (non-halting)
    spx_status = snapshot.get("SPX_feed_status")
    if spx_status != "online":
        dlog.benchmark_context_disabled = True
        dlog.record(1, "system_benchmark",
                    {"SPX_feed_status": spx_status},
                    "benchmark_context_disabled",
                    "spx_feed_unavailable_benchmark_skipped")

    # Check 3: Minimum usable inputs
    usable_count = sum(1 for k in MACRO_INPUT_KEYS if payload.get(k) is not None)
    if usable_count < MIN_REQUIRED_MACRO_INPUTS:
        dlog.halt(1, "insufficient_inputs",
                  {"usable_macro_input_count": usable_count,
                   "min_required": MIN_REQUIRED_MACRO_INPUTS})
        return False

    # Check 4: Timestamp present
    timestamp_str = snapshot.get("timestamp")
    if timestamp_str is None:
        dlog.halt(1, "reject_snapshot", {"timestamp": None})
        return False

    # Check 5: Timestamp freshness
    try:
        snap_time = datetime.datetime.fromisoformat(timestamp_str)
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

    # Check 6: Duplicate detection
    processed_store = snapshot.get("processed_snapshot_store", [])
    snap_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    if snap_hash in processed_store:
        dlog.halt(1, "suppress_duplicate",
                  {"snapshot_hash": snap_hash[:16] + "..."})
        return False

    dlog.record(1, "system_gate",
                {"usable_macro_input_count": usable_count,
                 "age_minutes": round(age_minutes, 1),
                 "benchmark_context_disabled": dlog.benchmark_context_disabled},
                "pass", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — INPUT UNIVERSE CONTROLS
# ============================================================

def gate_2_input_universe(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 2 — INPUT UNIVERSE CONTROLS
    Activates each macro input component based on availability flags.
    """
    payload = snapshot.get("snapshot_payload", {})

    input_flags = {
        "inflation":  payload.get("inflation_series_available", False),
        "growth":     payload.get("growth_series_available",    False),
        "labor":      payload.get("labor_series_available",     False),
        "policy":     payload.get("policy_series_available",    False),
        "curve":      payload.get("curve_series_available",     False),
        "credit":     payload.get("credit_series_available",    False),
        "liquidity":  payload.get("liquidity_series_available", False),
        "fx":         payload.get("fx_series_available",        False),
        "commodity":  payload.get("commodity_series_available", False),
        "news":       payload.get("macro_news_available",       False),
    }

    for component, available in input_flags.items():
        state = "active" if available else "inactive"
        dlog.input_states[component] = state
        dlog.record(2, "input_universe",
                    {"component": component, "available": available},
                    state, f"input_{component}_{state}")


def _input_active(dlog: RegimeDecisionLog, component: str) -> bool:
    return dlog.input_states.get(component) == "active"


# ============================================================
# GATE 3 — BENCHMARK GATE
# ============================================================

def gate_3_benchmark(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 3 — BENCHMARK GATE
    Classifies S&P 500 trend, drawdown, and volatility.
    Skipped if benchmark_context_disabled was set in Gate 1.
    """
    if dlog.benchmark_context_disabled:
        dlog.record(3, "benchmark", {}, "skipped", "benchmark_context_disabled")
        return

    payload = snapshot.get("snapshot_payload", {})

    # Trend: short vs long MA
    ma_short = payload.get("MA_short_SPX")
    ma_long  = payload.get("MA_long_SPX")
    if ma_short is not None and ma_long is not None:
        if ma_short > ma_long:
            dlog.benchmark_state = "bullish"
            dlog.record(3, "benchmark_trend",
                        {"MA_short": ma_short, "MA_long": ma_long},
                        "bullish", "short_ma_above_long_ma")
        elif ma_short < ma_long:
            dlog.benchmark_state = "bearish"
            dlog.record(3, "benchmark_trend",
                        {"MA_short": ma_short, "MA_long": ma_long},
                        "bearish", "short_ma_below_long_ma")

    # Drawdown
    spx_current     = payload.get("SPX_current")
    rolling_peak    = payload.get("rolling_peak_SPX")
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
    realized_vol = payload.get("realized_vol_SPX")
    vix          = payload.get("VIX")
    if realized_vol is not None or vix is not None:
        vol_high = (realized_vol is not None and realized_vol > SPX_VOL_THRESHOLD) or \
                   (vix is not None and vix > VIX_THRESHOLD)
        if vol_high:
            dlog.benchmark_vol_state = "high"
            dlog.record(3, "benchmark_vol",
                        {"realized_vol": realized_vol, "VIX": vix,
                         "vol_threshold": SPX_VOL_THRESHOLD, "vix_threshold": VIX_THRESHOLD},
                        "high", "benchmark_volatility_high")
        else:
            dlog.benchmark_vol_state = "normal"
            dlog.record(3, "benchmark_vol",
                        {"realized_vol": realized_vol, "VIX": vix},
                        "normal", "benchmark_volatility_normal")


# ============================================================
# GATE 4 — INFLATION REGIME CONTROLS
# ============================================================

def gate_4_inflation(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 4 — INFLATION REGIME CONTROLS"""
    if not _input_active(dlog, "inflation"):
        return

    p = snapshot.get("snapshot_payload", {})

    # Level
    cpi_hl  = p.get("headline_CPI_yoy")
    cpi_core= p.get("core_CPI_yoy")
    if cpi_hl is not None or cpi_core is not None:
        hl   = cpi_hl   if cpi_hl   is not None else 0.0
        core = cpi_core if cpi_core is not None else 0.0
        if hl > INFLATION_HIGH_THRESHOLD or core > INFLATION_HIGH_THRESHOLD:
            dlog.inflation_state = "high"
            dlog.record(4, "inflation_level",
                        {"headline_CPI_yoy": cpi_hl, "core_CPI_yoy": cpi_core,
                         "threshold": INFLATION_HIGH_THRESHOLD},
                        "high", "cpi_above_high_threshold")
        elif hl < INFLATION_LOW_THRESHOLD and core < INFLATION_LOW_THRESHOLD:
            dlog.inflation_state = "low"
            dlog.record(4, "inflation_level",
                        {"headline_CPI_yoy": cpi_hl, "core_CPI_yoy": cpi_core},
                        "low", "both_cpi_below_low_threshold")
        else:
            dlog.record(4, "inflation_level",
                        {"headline_CPI_yoy": cpi_hl, "core_CPI_yoy": cpi_core},
                        "moderate", "cpi_between_thresholds")

    # Trend
    cpi_now  = p.get("CPI_yoy_t") or cpi_hl
    cpi_prev = p.get("CPI_yoy_prev")
    if cpi_now is not None and cpi_prev is not None:
        change = cpi_now - cpi_prev
        if change > INFLATION_ACCEL_THRESHOLD:
            dlog.inflation_trend_state = "accelerating"
            dlog.record(4, "inflation_trend",
                        {"CPI_change": change, "accel_threshold": INFLATION_ACCEL_THRESHOLD},
                        "accelerating", "inflation_accelerating")
        elif change < -INFLATION_DECEL_THRESHOLD:
            dlog.inflation_trend_state = "decelerating"
            dlog.record(4, "inflation_trend",
                        {"CPI_change": change},
                        "decelerating", "inflation_decelerating")
        else:
            dlog.record(4, "inflation_trend",
                        {"CPI_change": change},
                        "stable", "inflation_trend_stable")

    # Quality
    services_inf = p.get("services_inflation")
    goods_change = p.get("goods_inflation_change")
    if services_inf is not None and services_inf > SERVICES_STICKY_THRESHOLD:
        dlog.inflation_quality_state = "sticky_services"
        dlog.record(4, "inflation_quality",
                    {"services_inflation": services_inf,
                     "threshold": SERVICES_STICKY_THRESHOLD},
                    "sticky_services", "services_inflation_sticky")
    elif goods_change is not None and goods_change < -GOODS_DISINFLATION_THRESHOLD:
        dlog.inflation_quality_state = "goods_disinflation"
        dlog.record(4, "inflation_quality",
                    {"goods_inflation_change": goods_change},
                    "goods_disinflation", "goods_disinflation_dominant")

    # Surprise
    actual    = p.get("inflation_actual")
    consensus = p.get("inflation_consensus")
    if actual is not None and consensus is not None:
        surprise = actual - consensus
        if surprise > INFLATION_SURPRISE_THRESHOLD:
            dlog.inflation_surprise_state = "upside"
            dlog.record(4, "inflation_surprise",
                        {"surprise": surprise, "threshold": INFLATION_SURPRISE_THRESHOLD},
                        "upside", "inflation_surprise_upside")
        elif surprise < -INFLATION_SURPRISE_THRESHOLD:
            dlog.inflation_surprise_state = "downside"
            dlog.record(4, "inflation_surprise",
                        {"surprise": surprise},
                        "downside", "inflation_surprise_downside")
        else:
            dlog.record(4, "inflation_surprise",
                        {"surprise": surprise},
                        "in_line", "no_inflation_surprise")


# ============================================================
# GATE 5 — GROWTH REGIME CONTROLS
# ============================================================

def gate_5_growth(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 5 — GROWTH REGIME CONTROLS"""
    if not _input_active(dlog, "growth"):
        return

    p = snapshot.get("snapshot_payload", {})

    # Level
    gdp_now    = p.get("GDP_nowcast")
    growth_idx = p.get("composite_growth_index")
    if gdp_now is not None or growth_idx is not None:
        g1 = gdp_now    if gdp_now    is not None else 0.0
        g2 = growth_idx if growth_idx is not None else 0.0
        if g1 > GROWTH_STRONG_THRESHOLD or g2 > GROWTH_STRONG_THRESHOLD:
            dlog.growth_state = "strong"
            dlog.record(5, "growth_level",
                        {"GDP_nowcast": gdp_now, "composite_growth_index": growth_idx},
                        "strong", "growth_above_strong_threshold")
        elif g1 < GROWTH_WEAK_THRESHOLD or g2 < GROWTH_WEAK_THRESHOLD:
            dlog.growth_state = "weak"
            dlog.record(5, "growth_level",
                        {"GDP_nowcast": gdp_now, "composite_growth_index": growth_idx},
                        "weak", "growth_below_weak_threshold")
        else:
            dlog.record(5, "growth_level",
                        {"GDP_nowcast": gdp_now},
                        "moderate", "growth_between_thresholds")

    # Trend
    now_t  = p.get("GDP_nowcast") or p.get("growth_nowcast_t")
    now_tm1= p.get("growth_nowcast_prev")
    if now_t is not None and now_tm1 is not None:
        delta = now_t - now_tm1
        if delta > GROWTH_ACCEL_THRESHOLD:
            dlog.growth_trend_state = "accelerating"
            dlog.record(5, "growth_trend",
                        {"delta": delta, "threshold": GROWTH_ACCEL_THRESHOLD},
                        "accelerating", "growth_nowcast_rising")
        elif delta < -GROWTH_DECEL_THRESHOLD:
            dlog.growth_trend_state = "decelerating"
            dlog.record(5, "growth_trend",
                        {"delta": delta},
                        "decelerating", "growth_nowcast_falling")
        else:
            dlog.record(5, "growth_trend", {"delta": delta},
                        "stable", "growth_trend_stable")

    # Quality: services vs manufacturing
    pmi_mfg = p.get("PMI_manufacturing")
    pmi_svc = p.get("PMI_services")
    if pmi_svc is not None and pmi_svc > SERVICES_EXPANSION_THRESHOLD:
        dlog.growth_quality_state = "services_strength"
        dlog.record(5, "growth_quality",
                    {"PMI_services": pmi_svc, "threshold": SERVICES_EXPANSION_THRESHOLD},
                    "services_strength", "services_pmi_above_expansion")
    elif pmi_mfg is not None and pmi_mfg < MANUFACTURING_CONTRACTION_THRESHOLD:
        dlog.growth_quality_state = "manufacturing_weakness"
        dlog.record(5, "growth_quality",
                    {"PMI_manufacturing": pmi_mfg,
                     "threshold": MANUFACTURING_CONTRACTION_THRESHOLD},
                    "manufacturing_weakness", "manufacturing_pmi_below_contraction")

    # Recession risk
    rec_prob = p.get("recession_probability")
    if rec_prob is not None:
        if rec_prob > RECESSION_PROB_THRESHOLD:
            dlog.growth_risk_state = "recession_risk"
            dlog.record(5, "growth_risk",
                        {"recession_probability": rec_prob,
                         "threshold": RECESSION_PROB_THRESHOLD},
                        "recession_risk", "recession_probability_elevated")
        else:
            dlog.record(5, "growth_risk",
                        {"recession_probability": rec_prob},
                        "pass", "recession_probability_acceptable")


# ============================================================
# GATE 6 — LABOR REGIME CONTROLS
# ============================================================

def gate_6_labor(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 6 — LABOR REGIME CONTROLS"""
    if not _input_active(dlog, "labor"):
        return

    p = snapshot.get("snapshot_payload", {})

    # Tightness / loosening
    ur       = p.get("unemployment_rate")
    ur_prev  = p.get("unemployment_rate_prev")
    job_open = p.get("job_openings_ratio")
    if ur is not None:
        if ur < UNEMPLOYMENT_LOW_THRESHOLD and (job_open is None or job_open > LABOR_TIGHT_THRESHOLD):
            dlog.labor_state = "tight"
            dlog.record(6, "labor_tightness",
                        {"unemployment_rate": ur, "job_openings_ratio": job_open},
                        "tight", "low_unemployment_and_high_openings")
        elif ur_prev is not None and ur - ur_prev > LABOR_LOOSEN_THRESHOLD:
            dlog.labor_state = "loosening"
            dlog.record(6, "labor_tightness",
                        {"unemployment_rate": ur, "unemployment_rate_prev": ur_prev,
                         "delta": ur - ur_prev},
                        "loosening", "unemployment_rising")

    # Payroll momentum
    payrolls = p.get("nonfarm_payrolls_3m_avg")
    if payrolls is not None:
        if payrolls > PAYROLL_STRONG_THRESHOLD:
            dlog.labor_momentum_state = "strong"
            dlog.record(6, "labor_momentum",
                        {"nonfarm_payrolls_3m_avg": payrolls,
                         "threshold": PAYROLL_STRONG_THRESHOLD},
                        "strong", "payroll_growth_strong")
        elif payrolls < PAYROLL_WEAK_THRESHOLD:
            dlog.labor_momentum_state = "weak"
            dlog.record(6, "labor_momentum",
                        {"nonfarm_payrolls_3m_avg": payrolls,
                         "threshold": PAYROLL_WEAK_THRESHOLD},
                        "weak", "payroll_growth_weak")

    # Wages
    ahe = p.get("average_hourly_earnings_yoy")
    if ahe is not None:
        if ahe > WAGE_GROWTH_HIGH_THRESHOLD:
            dlog.wage_state = "inflationary"
            dlog.record(6, "wages",
                        {"AHE_yoy": ahe, "threshold": WAGE_GROWTH_HIGH_THRESHOLD},
                        "inflationary", "wage_growth_above_high_threshold")
        elif ahe < WAGE_GROWTH_MODERATION_THRESHOLD:
            dlog.wage_state = "easing"
            dlog.record(6, "wages",
                        {"AHE_yoy": ahe, "threshold": WAGE_GROWTH_MODERATION_THRESHOLD},
                        "easing", "wage_growth_moderating")

    # Layoff risk
    claims = p.get("initial_claims_4w_avg")
    if claims is not None:
        if claims > CLAIMS_RISING_THRESHOLD:
            dlog.labor_risk_state = "weakening"
            dlog.record(6, "labor_risk",
                        {"initial_claims_4w_avg": claims,
                         "threshold": CLAIMS_RISING_THRESHOLD},
                        "weakening", "layoffs_rising")
        else:
            dlog.record(6, "labor_risk",
                        {"initial_claims_4w_avg": claims},
                        "pass", "claims_within_range")


# ============================================================
# GATE 7 — POLICY REGIME CONTROLS
# ============================================================

def gate_7_policy(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 7 — POLICY REGIME CONTROLS"""
    if not _input_active(dlog, "policy"):
        return

    p = snapshot.get("snapshot_payload", {})

    # Policy stance
    real_rate = p.get("real_policy_rate")
    if real_rate is not None:
        if real_rate > RESTRICTIVE_REAL_RATE_THRESHOLD:
            dlog.policy_state = "restrictive"
            dlog.record(7, "policy_stance",
                        {"real_policy_rate": real_rate,
                         "threshold": RESTRICTIVE_REAL_RATE_THRESHOLD},
                        "restrictive", "real_rate_above_restrictive_threshold")
        elif real_rate < ACCOMMODATIVE_REAL_RATE_THRESHOLD:
            dlog.policy_state = "accommodative"
            dlog.record(7, "policy_stance",
                        {"real_policy_rate": real_rate},
                        "accommodative", "real_rate_below_accommodative_threshold")
        else:
            dlog.record(7, "policy_stance",
                        {"real_policy_rate": real_rate},
                        "neutral", "real_rate_between_thresholds")

    # Hiking / easing cycle
    rate_change_6m = p.get("policy_rate_change_6m")
    if rate_change_6m is not None:
        if rate_change_6m > HIKING_CYCLE_THRESHOLD:
            dlog.policy_trend_state = "tightening"
            dlog.record(7, "policy_cycle",
                        {"policy_rate_change_6m": rate_change_6m,
                         "threshold": HIKING_CYCLE_THRESHOLD},
                        "tightening", "hiking_cycle_active")
        elif rate_change_6m < -EASING_CYCLE_THRESHOLD:
            dlog.policy_trend_state = "easing"
            dlog.record(7, "policy_cycle",
                        {"policy_rate_change_6m": rate_change_6m},
                        "easing", "easing_cycle_active")

    # Balance sheet direction
    bs_change = p.get("central_bank_balance_sheet_change")
    if bs_change is not None:
        if bs_change < -QT_THRESHOLD:
            dlog.liquidity_policy_state = "tightening"
            dlog.record(7, "policy_balance_sheet",
                        {"balance_sheet_change": bs_change, "QT_threshold": -QT_THRESHOLD},
                        "tightening", "QT_active")
        elif bs_change > QE_THRESHOLD:
            dlog.liquidity_policy_state = "easing"
            dlog.record(7, "policy_balance_sheet",
                        {"balance_sheet_change": bs_change},
                        "easing", "QE_active")

    # Policy surprise
    implied_shift = p.get("policy_market_implied_shift")
    if implied_shift is not None:
        if implied_shift > HAWKISH_SURPRISE_THRESHOLD:
            dlog.policy_surprise_state = "hawkish"
            dlog.record(7, "policy_surprise",
                        {"policy_market_implied_shift": implied_shift,
                         "threshold": HAWKISH_SURPRISE_THRESHOLD},
                        "hawkish", "hawkish_policy_surprise")
        elif implied_shift < -DOVISH_SURPRISE_THRESHOLD:
            dlog.policy_surprise_state = "dovish"
            dlog.record(7, "policy_surprise",
                        {"policy_market_implied_shift": implied_shift},
                        "dovish", "dovish_policy_surprise")


# ============================================================
# GATE 8 — YIELD CURVE REGIME CONTROLS
# ============================================================

def gate_8_yield_curve(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 8 — YIELD CURVE REGIME CONTROLS"""
    if not _input_active(dlog, "curve"):
        return

    p = snapshot.get("snapshot_payload", {})

    y10 = p.get("yield_10y")
    y2  = p.get("yield_2y")

    # Inversion
    if y10 is not None and y2 is not None:
        spread = y10 - y2
        if spread < CURVE_INVERSION_THRESHOLD:
            dlog.curve_state = "inverted"
            dlog.record(8, "curve_level",
                        {"spread_10y_2y": spread,
                         "inversion_threshold": CURVE_INVERSION_THRESHOLD},
                        "inverted", "curve_inverted")
        else:
            dlog.record(8, "curve_level",
                        {"spread_10y_2y": spread},
                        "positive", "curve_not_inverted")

    # Steepening trend
    spread_prev = p.get("spread_10y_2y_prev")
    if y10 is not None and y2 is not None and spread_prev is not None:
        spread_now = y10 - y2
        if spread_now - spread_prev > STEEPENING_THRESHOLD:
            dlog.curve_trend_state = "steepening"
            dlog.record(8, "curve_trend",
                        {"spread_change": spread_now - spread_prev,
                         "threshold": STEEPENING_THRESHOLD},
                        "steepening", "curve_spread_widening")

    # Bull / bear steepener
    y10_chg = p.get("yield_10y_change")
    y2_chg  = p.get("yield_2y_change")
    if y10_chg is not None and y2_chg is not None:
        # Bear steepener: 10y rising faster than 2y
        if y10_chg - y2_chg > BEAR_STEEPENING_THRESHOLD:
            dlog.curve_regime_state = "bear_steepener"
            dlog.record(8, "curve_regime",
                        {"yield_10y_change": y10_chg, "yield_2y_change": y2_chg},
                        "bear_steepener", "long_end_rising_faster_than_short_end")
        # Bull steepener: 2y falling faster than 10y
        elif y2_chg - y10_chg < -BULL_STEEPENING_THRESHOLD:
            dlog.curve_regime_state = "bull_steepener"
            dlog.record(8, "curve_regime",
                        {"yield_10y_change": y10_chg, "yield_2y_change": y2_chg},
                        "bull_steepener", "short_end_falling_faster_than_long_end")

    # Front-end stress
    if y2_chg is not None and y2_chg > FRONTEND_STRESS_THRESHOLD:
        dlog.rate_stress_state = "frontend_stress"
        dlog.record(8, "rate_stress",
                    {"yield_2y_change": y2_chg, "threshold": FRONTEND_STRESS_THRESHOLD},
                    "frontend_stress", "front_end_rate_stress_high")

    # Long-end inflation premium
    term_premium_change = p.get("term_premium_change")
    if term_premium_change is not None and term_premium_change > TERM_PREMIUM_THRESHOLD:
        dlog.long_end_state = "inflation_premium_rise"
        dlog.record(8, "long_end",
                    {"term_premium_change": term_premium_change,
                     "threshold": TERM_PREMIUM_THRESHOLD},
                    "inflation_premium_rise", "term_premium_rising")


# ============================================================
# GATE 9 — CREDIT REGIME CONTROLS
# ============================================================

def gate_9_credit(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 9 — CREDIT REGIME CONTROLS"""
    if not _input_active(dlog, "credit"):
        return

    p = snapshot.get("snapshot_payload", {})

    ig_chg = p.get("IG_spread_change")
    hy_chg = p.get("HY_spread_change")

    # IG widening
    if ig_chg is not None and ig_chg > IG_SPREAD_WIDEN_THRESHOLD:
        dlog.credit_state = "deteriorating"
        dlog.record(9, "credit_ig",
                    {"IG_spread_change": ig_chg, "threshold": IG_SPREAD_WIDEN_THRESHOLD},
                    "deteriorating", "IG_spreads_widening")

    # HY stress
    if hy_chg is not None and hy_chg > HY_SPREAD_WIDEN_THRESHOLD:
        dlog.credit_risk_state = "high_yield_stress"
        dlog.record(9, "credit_hy",
                    {"HY_spread_change": hy_chg, "threshold": HY_SPREAD_WIDEN_THRESHOLD},
                    "high_yield_stress", "HY_spreads_widening_materially")

    # Tightening (both IG and HY tightening)
    if (hy_chg is not None and hy_chg < -SPREAD_TIGHTENING_THRESHOLD and
            ig_chg is not None and ig_chg < -SPREAD_TIGHTENING_THRESHOLD):
        dlog.credit_state = "improving"
        dlog.record(9, "credit_tightening",
                    {"IG_spread_change": ig_chg, "HY_spread_change": hy_chg},
                    "improving", "both_spreads_tightening")

    # Default risk
    default_rate = p.get("default_rate_nowcast")
    if default_rate is not None:
        if default_rate > DEFAULT_RISK_THRESHOLD:
            dlog.credit_default_state = "elevated"
            dlog.record(9, "credit_default",
                        {"default_rate_nowcast": default_rate,
                         "threshold": DEFAULT_RISK_THRESHOLD},
                        "elevated", "default_risk_elevated")
        else:
            dlog.record(9, "credit_default",
                        {"default_rate_nowcast": default_rate},
                        "pass", "default_risk_acceptable")

    # Funding stress
    funding_spread = p.get("funding_spread")
    if funding_spread is not None:
        if funding_spread > FUNDING_STRESS_THRESHOLD:
            dlog.funding_liquidity_state = "funding_stress"
            dlog.record(9, "credit_funding",
                        {"funding_spread": funding_spread,
                         "threshold": FUNDING_STRESS_THRESHOLD},
                        "funding_stress", "funding_spread_elevated")
        else:
            dlog.record(9, "credit_funding",
                        {"funding_spread": funding_spread},
                        "pass", "funding_spread_normal")


# ============================================================
# GATE 10 — LIQUIDITY REGIME CONTROLS
# ============================================================

def gate_10_liquidity(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 10 — LIQUIDITY REGIME CONTROLS"""
    if not _input_active(dlog, "liquidity"):
        return

    p = snapshot.get("snapshot_payload", {})

    # System liquidity direction
    net_liq = p.get("net_liquidity_change")
    if net_liq is not None:
        if net_liq > LIQUIDITY_IMPROVING_THRESHOLD:
            dlog.system_liquidity_state = "improving"
            dlog.record(10, "liquidity_direction",
                        {"net_liquidity_change": net_liq,
                         "threshold": LIQUIDITY_IMPROVING_THRESHOLD},
                        "improving", "net_liquidity_improving")
        elif net_liq < -LIQUIDITY_DETERIORATING_THRESHOLD:
            dlog.system_liquidity_state = "deteriorating"
            dlog.record(10, "liquidity_direction",
                        {"net_liquidity_change": net_liq,
                         "threshold": -LIQUIDITY_DETERIORATING_THRESHOLD},
                        "deteriorating", "net_liquidity_deteriorating")

    # Reserves and money market
    reserves = p.get("bank_reserves_metric")
    mm_metric= p.get("short_term_funding_metric")
    if mm_metric is not None and mm_metric > MONEY_MARKET_STRESS_THRESHOLD:
        dlog.liquidity_quality_state = "stressed"
        dlog.record(10, "liquidity_quality",
                    {"short_term_funding_metric": mm_metric,
                     "threshold": MONEY_MARKET_STRESS_THRESHOLD},
                    "stressed", "money_market_stress_elevated")
    elif reserves is not None and reserves < RESERVE_TIGHT_THRESHOLD:
        dlog.liquidity_quality_state = "tight_reserves"
        dlog.record(10, "liquidity_quality",
                    {"bank_reserves_metric": reserves,
                     "threshold": RESERVE_TIGHT_THRESHOLD},
                    "tight_reserves", "bank_reserves_low")

    # Fiscal issuance pressure
    net_supply = p.get("net_treasury_supply_change")
    if net_supply is not None and net_supply > ISSUANCE_PRESSURE_THRESHOLD:
        dlog.liquidity_supply_state = "supply_pressure"
        dlog.record(10, "liquidity_supply",
                    {"net_treasury_supply_change": net_supply,
                     "threshold": ISSUANCE_PRESSURE_THRESHOLD},
                    "supply_pressure", "fiscal_issuance_pressure_high")


# ============================================================
# GATE 11 — FX / GLOBAL TRANSMISSION CONTROLS
# ============================================================

def gate_11_fx_global(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 11 — FX / GLOBAL TRANSMISSION CONTROLS"""
    if not _input_active(dlog, "fx"):
        return

    p = snapshot.get("snapshot_payload", {})

    # USD direction
    dxy_ret = p.get("DXY_return")
    if dxy_ret is not None:
        if dxy_ret > USD_STRENGTH_THRESHOLD:
            dlog.fx_state = "strong_dollar"
            dlog.record(11, "fx_dollar",
                        {"DXY_return": dxy_ret, "threshold": USD_STRENGTH_THRESHOLD},
                        "strong_dollar", "USD_strengthening_materially")
        elif dxy_ret < -USD_WEAKNESS_THRESHOLD:
            dlog.fx_state = "weak_dollar"
            dlog.record(11, "fx_dollar",
                        {"DXY_return": dxy_ret},
                        "weak_dollar", "USD_weakening_materially")

    # Financial conditions (strong dollar + weak growth cross-component check)
    if dlog.fx_state == "strong_dollar" and dlog.growth_state == "weak":
        dlog.global_pressure_state = "tightening_financial_conditions"
        dlog.record(11, "fx_conditions",
                    {"fx_state": dlog.fx_state, "growth_state": dlog.growth_state},
                    "tightening_financial_conditions",
                    "strong_dollar_with_weak_growth")

    # EM stress
    em_stress = p.get("EM_fx_stress_index")
    if em_stress is not None and em_stress > EM_STRESS_THRESHOLD:
        dlog.global_risk_state = "EM_stress"
        dlog.record(11, "fx_em",
                    {"EM_fx_stress_index": em_stress, "threshold": EM_STRESS_THRESHOLD},
                    "EM_stress", "EM_stress_elevated")

    # Global growth stabilization
    global_pmi_chg = p.get("global_PMI_change")
    if global_pmi_chg is not None and global_pmi_chg > GLOBAL_STABILIZATION_THRESHOLD:
        dlog.global_growth_state = "stabilizing"
        dlog.record(11, "fx_global_growth",
                    {"global_PMI_change": global_pmi_chg,
                     "threshold": GLOBAL_STABILIZATION_THRESHOLD},
                    "stabilizing", "global_growth_stabilizing")


# ============================================================
# GATE 12 — COMMODITY REGIME CONTROLS
# ============================================================

def gate_12_commodity(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 12 — COMMODITY REGIME CONTROLS"""
    if not _input_active(dlog, "commodity"):
        return

    p = snapshot.get("snapshot_payload", {})

    wti_ret      = p.get("WTI_return")
    metals_ret   = p.get("industrial_metals_return")
    broad_ret    = p.get("broad_commodity_index_return")
    food_chg     = p.get("food_commodity_index_change")

    # Priority 1: Energy inflation risk
    if wti_ret is not None and wti_ret > OIL_SPIKE_THRESHOLD:
        dlog.commodity_state = "energy_inflation_risk"
        dlog.record(12, "commodity",
                    {"WTI_return": wti_ret, "threshold": OIL_SPIKE_THRESHOLD},
                    "energy_inflation_risk", "oil_rising_sharply")
        return

    # Priority 2: Food inflation risk
    if food_chg is not None and food_chg > FOOD_PRESSURE_THRESHOLD:
        dlog.commodity_state = "food_inflation_risk"
        dlog.record(12, "commodity",
                    {"food_commodity_index_change": food_chg,
                     "threshold": FOOD_PRESSURE_THRESHOLD},
                    "food_inflation_risk", "food_inflation_pressure_elevated")
        return

    # Priority 3: Growth confirmation
    if (metals_ret is not None and metals_ret > METALS_GROWTH_THRESHOLD and
            dlog.growth_state in ("strong",) or
            (metals_ret is not None and metals_ret > METALS_GROWTH_THRESHOLD and
             dlog.growth_trend_state == "accelerating")):
        dlog.commodity_state = "growth_confirmation"
        dlog.record(12, "commodity",
                    {"industrial_metals_return": metals_ret,
                     "growth_state": dlog.growth_state,
                     "growth_trend_state": dlog.growth_trend_state},
                    "growth_confirmation", "metals_rising_with_growth")
        return

    # Priority 4: Deflationary signal
    if broad_ret is not None and broad_ret < COMMODITY_WEAKNESS_THRESHOLD:
        dlog.commodity_state = "deflationary_signal"
        dlog.record(12, "commodity",
                    {"broad_commodity_index_return": broad_ret,
                     "threshold": COMMODITY_WEAKNESS_THRESHOLD},
                    "deflationary_signal", "commodities_falling_broadly")


# ============================================================
# GATE 13 — MACRO NEWS INTEGRATION CONTROLS
# ============================================================

def gate_13_macro_news(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 13 — MACRO NEWS INTEGRATION CONTROLS"""
    if not _input_active(dlog, "news"):
        return

    p = snapshot.get("snapshot_payload", {})

    # Sentiment
    sentiment = p.get("macro_news_sentiment_score")
    if sentiment is not None:
        if sentiment > MACRO_NEWS_POSITIVE_THRESHOLD:
            dlog.news_state = "supportive"
            dlog.record(13, "news_sentiment",
                        {"macro_news_sentiment_score": sentiment,
                         "threshold": MACRO_NEWS_POSITIVE_THRESHOLD},
                        "supportive", "macro_news_sentiment_positive")
        elif sentiment < -MACRO_NEWS_NEGATIVE_THRESHOLD:
            dlog.news_state = "unsupportive"
            dlog.record(13, "news_sentiment",
                        {"macro_news_sentiment_score": sentiment},
                        "unsupportive", "macro_news_sentiment_negative")

    # CB communication tone (hawkish checked first)
    hawkish_density = p.get("hawkish_term_density")
    dovish_density  = p.get("dovish_term_density")
    if hawkish_density is not None and hawkish_density > HAWKISH_COMM_THRESHOLD:
        dlog.policy_comm_state = "hawkish"
        dlog.record(13, "news_cb_comm",
                    {"hawkish_term_density": hawkish_density,
                     "threshold": HAWKISH_COMM_THRESHOLD},
                    "hawkish", "central_bank_communication_hawkish")
    elif dovish_density is not None and dovish_density > DOVISH_COMM_THRESHOLD:
        dlog.policy_comm_state = "dovish"
        dlog.record(13, "news_cb_comm",
                    {"dovish_term_density": dovish_density,
                     "threshold": DOVISH_COMM_THRESHOLD},
                    "dovish", "central_bank_communication_dovish")

    # Conviction
    neg_confirms = p.get("negative_macro_news_confirmations", 0)
    pos_confirms = p.get("positive_macro_news_confirmations", 0)
    if neg_confirms >= MIN_CONFIRMATIONS:
        dlog.news_conviction_state = "strong_negative"
        dlog.record(13, "news_conviction",
                    {"negative_confirmations": neg_confirms,
                     "min_confirmations": MIN_CONFIRMATIONS},
                    "strong_negative", "macro_downside_confirmed")
    elif pos_confirms >= MIN_CONFIRMATIONS:
        dlog.news_conviction_state = "strong_positive"
        dlog.record(13, "news_conviction",
                    {"positive_confirmations": pos_confirms,
                     "min_confirmations": MIN_CONFIRMATIONS},
                    "strong_positive", "macro_upside_confirmed")


# ============================================================
# GATE 14 — COMPOSITE REGIME CONSTRUCTION CONTROLS
# ============================================================

def _weighted_score(sub_scores: list, weights: tuple) -> float:
    """Compute weighted sum of sub-scores; skip None contributions. Clamp to [-1, +1]."""
    total_weight = 0.0
    score        = 0.0
    for s, w in zip(sub_scores, weights):
        if s is not None:
            score        += w * s
            total_weight += w
    if total_weight == 0:
        return 0.0
    # Normalise by actual weight used (handles missing sub-components gracefully)
    result = score / total_weight * sum(weights)
    return max(-1.0, min(1.0, result))


def gate_14_composite_construction(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 14 — COMPOSITE REGIME CONSTRUCTION CONTROLS
    Converts each active component's state variables into a component score
    in [-1.0, +1.0] using the sub-state lookup tables defined at module level.
    """

    # Inflation
    if _input_active(dlog, "inflation"):
        s = _weighted_score([
            INFLATION_STATE_SCORES.get(dlog.inflation_state),
            INFLATION_TREND_SCORES.get(dlog.inflation_trend_state),
            INFLATION_QUALITY_SCORES.get(dlog.inflation_quality_state),
            INFLATION_SURPRISE_SCORES.get(dlog.inflation_surprise_state),
        ], INFLATION_WEIGHTS)
        dlog.component_scores["inflation"] = round(s, 4)
        dlog.record(14, "score_inflation",
                    {"states": [dlog.inflation_state, dlog.inflation_trend_state,
                                dlog.inflation_quality_state, dlog.inflation_surprise_state]},
                    str(s), "inflation_component_scored")

    # Growth
    if _input_active(dlog, "growth"):
        s = _weighted_score([
            GROWTH_STATE_SCORES.get(dlog.growth_state),
            GROWTH_TREND_SCORES.get(dlog.growth_trend_state),
            GROWTH_QUALITY_SCORES.get(dlog.growth_quality_state),
            GROWTH_RISK_SCORES.get(dlog.growth_risk_state),
        ], GROWTH_WEIGHTS)
        dlog.component_scores["growth"] = round(s, 4)
        dlog.record(14, "score_growth",
                    {"states": [dlog.growth_state, dlog.growth_trend_state,
                                dlog.growth_quality_state, dlog.growth_risk_state]},
                    str(s), "growth_component_scored")

    # Labor
    if _input_active(dlog, "labor"):
        s = _weighted_score([
            LABOR_STATE_SCORES.get(dlog.labor_state),
            LABOR_MOMENTUM_SCORES.get(dlog.labor_momentum_state),
            WAGE_STATE_SCORES.get(dlog.wage_state),
            LABOR_RISK_SCORES.get(dlog.labor_risk_state),
        ], LABOR_WEIGHTS)
        dlog.component_scores["labor"] = round(s, 4)
        dlog.record(14, "score_labor",
                    {"states": [dlog.labor_state, dlog.labor_momentum_state,
                                dlog.wage_state, dlog.labor_risk_state]},
                    str(s), "labor_component_scored")

    # Policy
    if _input_active(dlog, "policy"):
        s = _weighted_score([
            POLICY_STATE_SCORES.get(dlog.policy_state),
            POLICY_TREND_SCORES.get(dlog.policy_trend_state),
            LIQUIDITY_POLICY_STATE_SCORES.get(dlog.liquidity_policy_state),
            POLICY_SURPRISE_SCORES.get(dlog.policy_surprise_state),
        ], POLICY_WEIGHTS)
        dlog.component_scores["policy"] = round(s, 4)
        dlog.record(14, "score_policy",
                    {"states": [dlog.policy_state, dlog.policy_trend_state,
                                dlog.liquidity_policy_state, dlog.policy_surprise_state]},
                    str(s), "policy_component_scored")

    # Curve
    if _input_active(dlog, "curve"):
        s = _weighted_score([
            CURVE_STATE_SCORES.get(dlog.curve_state),
            CURVE_TREND_SCORES.get(dlog.curve_trend_state),
            CURVE_REGIME_SCORES.get(dlog.curve_regime_state),
            RATE_STRESS_SCORES.get(dlog.rate_stress_state),
        ], CURVE_WEIGHTS)
        dlog.component_scores["curve"] = round(s, 4)
        dlog.record(14, "score_curve",
                    {"states": [dlog.curve_state, dlog.curve_trend_state,
                                dlog.curve_regime_state, dlog.rate_stress_state]},
                    str(s), "curve_component_scored")

    # Credit
    if _input_active(dlog, "credit"):
        s = _weighted_score([
            CREDIT_STATE_SCORES.get(dlog.credit_state),
            CREDIT_RISK_SCORES.get(dlog.credit_risk_state),
            CREDIT_DEFAULT_SCORES.get(dlog.credit_default_state),
            FUNDING_LIQUIDITY_SCORES.get(dlog.funding_liquidity_state),
        ], CREDIT_WEIGHTS)
        dlog.component_scores["credit"] = round(s, 4)
        dlog.record(14, "score_credit",
                    {"states": [dlog.credit_state, dlog.credit_risk_state,
                                dlog.credit_default_state, dlog.funding_liquidity_state]},
                    str(s), "credit_component_scored")

    # Liquidity
    if _input_active(dlog, "liquidity"):
        s = _weighted_score([
            SYSTEM_LIQUIDITY_SCORES.get(dlog.system_liquidity_state),
            LIQUIDITY_QUALITY_SCORES.get(dlog.liquidity_quality_state),
            LIQUIDITY_SUPPLY_SCORES.get(dlog.liquidity_supply_state),
        ], LIQUIDITY_WEIGHTS)
        dlog.component_scores["liquidity"] = round(s, 4)
        dlog.record(14, "score_liquidity",
                    {"states": [dlog.system_liquidity_state,
                                dlog.liquidity_quality_state, dlog.liquidity_supply_state]},
                    str(s), "liquidity_component_scored")

    # FX / Global
    if _input_active(dlog, "fx"):
        s = _weighted_score([
            FX_STATE_SCORES.get(dlog.fx_state),
            GLOBAL_PRESSURE_SCORES.get(dlog.global_pressure_state),
            GLOBAL_RISK_SCORES.get(dlog.global_risk_state),
            GLOBAL_GROWTH_SCORES.get(dlog.global_growth_state),
        ], FX_WEIGHTS)
        dlog.component_scores["fx_global"] = round(s, 4)
        dlog.record(14, "score_fx_global",
                    {"states": [dlog.fx_state, dlog.global_pressure_state,
                                dlog.global_risk_state, dlog.global_growth_state]},
                    str(s), "fx_global_component_scored")

    # Commodity
    if _input_active(dlog, "commodity"):
        s = COMMODITY_STATE_SCORES.get(dlog.commodity_state, 0.0)
        dlog.component_scores["commodity"] = round(s, 4)
        dlog.record(14, "score_commodity",
                    {"commodity_state": dlog.commodity_state},
                    str(s), "commodity_component_scored")

    # News
    if _input_active(dlog, "news"):
        s = _weighted_score([
            NEWS_STATE_SCORES.get(dlog.news_state),
            POLICY_COMM_SCORES.get(dlog.policy_comm_state),
            NEWS_CONVICTION_SCORES.get(dlog.news_conviction_state),
        ], NEWS_WEIGHTS)
        dlog.component_scores["news"] = round(s, 4)
        dlog.record(14, "score_news",
                    {"states": [dlog.news_state, dlog.policy_comm_state,
                                dlog.news_conviction_state]},
                    str(s), "news_component_scored")


# ============================================================
# GATE 15 — WEIGHTING CONTROLS
# ============================================================

def gate_15_weighting(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 15 — WEIGHTING CONTROLS
    Applies dynamic upshifts to base weights, then re-normalises to sum to 1.0.
    """
    w = dict(dlog.component_weights)  # start from current (base) weights
    applied = []

    # Inflation volatility
    if (dlog.inflation_trend_state == "accelerating" or
            dlog.inflation_surprise_state == "upside"):
        w["inflation"] *= INFLATION_WEIGHT_UPSHIFT
        applied.append("inflation_volatility_upshift")

    # Credit stress
    if dlog.credit_risk_state == "high_yield_stress" or \
            dlog.funding_liquidity_state == "funding_stress":
        w["credit"] *= CREDIT_WEIGHT_UPSHIFT
        applied.append("credit_stress_upshift")

    # Policy transition
    if (dlog.policy_trend_state in ("tightening", "easing") and
            dlog.policy_surprise_state is not None):
        w["policy"] *= POLICY_WEIGHT_UPSHIFT
        applied.append("policy_transition_upshift")

    # Liquidity deteriorating
    if dlog.system_liquidity_state == "deteriorating":
        w["liquidity"] *= LIQUIDITY_WEIGHT_UPSHIFT
        applied.append("liquidity_deteriorating_upshift")

    # Benchmark drawdown — additional credit upshift
    if dlog.benchmark_risk_state == "drawdown":
        w["credit"] *= DRAWDOWN_CREDIT_UPSHIFT
        applied.append("drawdown_credit_upshift")

    # Re-normalise
    total = sum(w.values())
    if total > 0:
        for k in w:
            w[k] = round(w[k] / total, 6)

    dlog.component_weights = w
    dlog.record(15, "weighting",
                {"adjustments_applied": applied,
                 "final_weights": w},
                "normalised", "weights_adjusted_and_normalised")


# ============================================================
# GATE 16 — COMPOSITE MACRO REGIME SCORE CONTROLS
# ============================================================

def gate_16_composite_regime_score(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 16 — COMPOSITE MACRO REGIME SCORE CONTROLS
    Computes weighted sum of component scores, then classifies the regime.
    Overlay labels (stagflation, reflation, disinflationary_slowdown) take priority.
    """
    score = 0.0
    for comp, comp_score in dlog.component_scores.items():
        weight = dlog.component_weights.get(comp, 0.0)
        score += weight * comp_score

    score = round(max(-1.0, min(1.0, score)), 4)
    dlog.macro_regime_score = score
    dlog.record(16, "regime_score",
                {"component_scores": dlog.component_scores,
                 "component_weights": dlog.component_weights,
                 "macro_regime_score": score},
                str(score), "macro_regime_score_calculated")

    # Overlay classification checks (priority order)
    if dlog.growth_state == "weak" and dlog.inflation_state == "high":
        dlog.macro_regime_state = "stagflation"
        dlog.record(16, "regime_classification",
                    {"growth_state": dlog.growth_state, "inflation_state": dlog.inflation_state},
                    "stagflation", "stagflation_overlay_weak_growth_high_inflation")

    elif ((dlog.growth_state == "strong" or dlog.growth_trend_state == "accelerating") and
          dlog.inflation_trend_state == "accelerating"):
        dlog.macro_regime_state = "reflation"
        dlog.record(16, "regime_classification",
                    {"growth_state": dlog.growth_state,
                     "growth_trend_state": dlog.growth_trend_state,
                     "inflation_trend_state": dlog.inflation_trend_state},
                    "reflation", "reflation_overlay_growth_and_inflation_accelerating")

    elif (dlog.growth_trend_state == "decelerating" and
          dlog.inflation_trend_state == "decelerating"):
        dlog.macro_regime_state = "disinflationary_slowdown"
        dlog.record(16, "regime_classification",
                    {"growth_trend_state": dlog.growth_trend_state,
                     "inflation_trend_state": dlog.inflation_trend_state},
                    "disinflationary_slowdown",
                    "disinflationary_slowdown_overlay_both_decelerating")

    # Score-based classification
    elif score >= REGIME_EXPANSION_THRESHOLD:
        dlog.macro_regime_state = "expansion"
        dlog.record(16, "regime_classification",
                    {"macro_regime_score": score,
                     "expansion_threshold": REGIME_EXPANSION_THRESHOLD},
                    "expansion", "score_above_expansion_threshold")

    elif score <= REGIME_CONTRACTION_THRESHOLD:
        dlog.macro_regime_state = "contraction"
        dlog.record(16, "regime_classification",
                    {"macro_regime_score": score,
                     "contraction_threshold": REGIME_CONTRACTION_THRESHOLD},
                    "contraction", "score_below_contraction_threshold")

    else:
        dlog.macro_regime_state = "slowdown"
        dlog.record(16, "regime_classification",
                    {"macro_regime_score": score},
                    "slowdown", "score_between_expansion_and_contraction_thresholds")


# ============================================================
# GATE 17 — CONFIDENCE CONTROLS
# ============================================================

def gate_17_confidence(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 17 — CONFIDENCE CONTROLS
    Computes macro_confidence from active component count, score dispersion,
    and input data quality.
    """
    p = snapshot.get("snapshot_payload", {})

    active_count = sum(1 for s in dlog.input_states.values() if s == "active")
    total_comps  = 10

    # Input sufficiency
    if active_count >= MIN_CONFIDENT_COMPONENT_COUNT:
        dlog.confidence_input_state = "sufficient"
    else:
        dlog.confidence_input_state = "insufficient"
    dlog.record(17, "confidence_inputs",
                {"active_count": active_count,
                 "min_confident": MIN_CONFIDENT_COMPONENT_COUNT},
                dlog.confidence_input_state, "input_count_assessed")

    # Score dispersion
    scores = list(dlog.component_scores.values())
    dispersion = 0.0
    if len(scores) >= 2:
        try:
            dispersion = statistics.stdev(scores)
        except statistics.StatisticsError:
            dispersion = 0.0
    elif len(scores) == 1:
        dispersion = 0.0

    if dispersion < AGREEMENT_THRESHOLD:
        dlog.confidence_state = "high_agreement"
    elif dispersion >= DISAGREEMENT_THRESHOLD:
        dlog.confidence_state = "conflicted"
    else:
        dlog.confidence_state = "moderate_agreement"
    dlog.record(17, "confidence_agreement",
                {"dispersion": round(dispersion, 4),
                 "agreement_threshold": AGREEMENT_THRESHOLD,
                 "disagreement_threshold": DISAGREEMENT_THRESHOLD},
                dlog.confidence_state, "component_dispersion_assessed")

    # Data quality
    quality_scores = p.get("input_quality_scores", [])
    if quality_scores:
        mean_quality = sum(quality_scores) / len(quality_scores)
    else:
        mean_quality = 0.5  # assume moderate quality if not provided
    if mean_quality >= DATA_QUALITY_THRESHOLD:
        dlog.data_confidence_state = "high"
    else:
        dlog.data_confidence_state = "low"
    dlog.record(17, "confidence_data_quality",
                {"mean_quality": round(mean_quality, 4),
                 "threshold": DATA_QUALITY_THRESHOLD},
                dlog.data_confidence_state, "data_quality_assessed")

    # Composite confidence score
    count_factor   = min(active_count / total_comps, 1.0)
    agreement_factor = max(0.0, 1.0 - dispersion)
    confidence = (0.40 * count_factor +
                  0.35 * agreement_factor +
                  0.25 * mean_quality)
    confidence = round(max(0.0, min(1.0, confidence)), 4)
    dlog.macro_confidence = confidence
    dlog.record(17, "confidence_score",
                {"count_factor": count_factor,
                 "agreement_factor": round(agreement_factor, 4),
                 "mean_quality": round(mean_quality, 4),
                 "macro_confidence": confidence},
                str(confidence), "macro_confidence_scored")


# ============================================================
# GATE 18 — DIVERGENCE WARNING CONTROLS
# ============================================================

def gate_18_divergence_warnings(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 18 — DIVERGENCE WARNING CONTROLS
    Detects cross-component inconsistencies. Multiple warnings may be active.
    """
    found = False

    # Growth strong but credit weak
    if dlog.growth_state == "strong" and dlog.credit_state == "deteriorating":
        dlog.warning_states.append("growth_credit_divergence")
        dlog.record(18, "divergence",
                    {"growth_state": dlog.growth_state, "credit_state": dlog.credit_state},
                    "growth_credit_divergence", "growth_strong_credit_weak")
        found = True

    # Inflation easing but policy still tightening
    if (dlog.inflation_trend_state == "decelerating" and
            dlog.policy_trend_state == "tightening"):
        dlog.warning_states.append("policy_lag_risk")
        dlog.record(18, "divergence",
                    {"inflation_trend_state": dlog.inflation_trend_state,
                     "policy_trend_state": dlog.policy_trend_state},
                    "policy_lag_risk", "inflation_easing_policy_still_tightening")
        found = True

    # Market bullish but macro contractionary
    if (dlog.benchmark_state == "bullish" and
            dlog.macro_regime_state == "contraction"):
        dlog.warning_states.append("market_macro_divergence")
        dlog.record(18, "divergence",
                    {"benchmark_state": dlog.benchmark_state,
                     "macro_regime_state": dlog.macro_regime_state},
                    "market_macro_divergence", "market_bullish_macro_contractionary")
        found = True

    # Curve steepening with growth weakness
    if (dlog.curve_trend_state == "steepening" and
            dlog.growth_state == "weak"):
        dlog.warning_states.append("recession_transition_risk")
        dlog.record(18, "divergence",
                    {"curve_trend_state": dlog.curve_trend_state,
                     "growth_state": dlog.growth_state},
                    "recession_transition_risk",
                    "curve_steepening_with_weak_growth")
        found = True

    # Strong dollar with widening credit spreads
    if (dlog.fx_state == "strong_dollar" and
            dlog.credit_state == "deteriorating"):
        dlog.warning_states.append("financial_conditions_tightening")
        dlog.record(18, "divergence",
                    {"fx_state": dlog.fx_state, "credit_state": dlog.credit_state},
                    "financial_conditions_tightening",
                    "strong_dollar_with_widening_spreads")
        found = True

    if not found:
        dlog.record(18, "divergence",
                    {"macro_regime_state": dlog.macro_regime_state},
                    "no_warnings", "no_divergence_conditions_met")


# ============================================================
# GATE 19 — ACTION CLASSIFICATION CONTROLS
# ============================================================

def gate_19_action_classification(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 19 — ACTION CLASSIFICATION CONTROLS
    Maps regime state and confidence to a named classification.
    Evaluated in priority order; first match wins.
    """
    mc = dlog.macro_confidence or 0.0
    rs = dlog.macro_regime_state

    if rs == "stagflation":
        dlog.classification = "stagflation_warning"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs},
                    "stagflation_warning", "stagflation_detected")

    elif rs == "expansion" and mc >= HIGH_CONF_THRESHOLD:
        dlog.classification = "pro_growth_regime"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs, "macro_confidence": mc},
                    "pro_growth_regime", "expansion_with_high_confidence")

    elif rs == "contraction" and mc >= HIGH_CONF_THRESHOLD:
        dlog.classification = "defensive_regime"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs, "macro_confidence": mc},
                    "defensive_regime", "contraction_with_high_confidence")

    elif rs == "reflation":
        dlog.classification = "reflation_signal"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs},
                    "reflation_signal", "reflation_detected")

    elif rs == "disinflationary_slowdown":
        dlog.classification = "disinflation_slowdown_signal"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs},
                    "disinflation_slowdown_signal", "disinflationary_slowdown_detected")

    elif dlog.warning_states and mc >= DIVERGENCE_CONF_THRESHOLD:
        dlog.classification = "divergence_watch"
        dlog.record(19, "classification",
                    {"warning_states": dlog.warning_states, "macro_confidence": mc},
                    "divergence_watch", "major_warning_active_with_sufficient_confidence")

    else:
        dlog.classification = "no_clear_macro_signal"
        dlog.record(19, "classification",
                    {"macro_regime_state": rs, "macro_confidence": mc,
                     "low_conf_threshold": LOW_CONF_THRESHOLD},
                    "no_clear_macro_signal",
                    "low_confidence_or_no_matching_regime_pattern")


# ============================================================
# GATE 20 — TEMPORAL PERSISTENCE CONTROLS
# ============================================================

def gate_20_temporal_persistence(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 20 — TEMPORAL PERSISTENCE CONTROLS
    Evaluates regime persistence and trend using caller-supplied history counts.
    (TODO:DATA_DEPENDENCY — persistence counters require historical state store.)
    """
    p = snapshot.get("snapshot_payload", {})

    consec_expansion   = p.get("consecutive_expansion_count", 0)
    consec_contraction = p.get("consecutive_contraction_count", 0)
    state_change_count = p.get("recent_state_change_count", 0)
    score_prev         = p.get("macro_regime_score_prev")

    # Persistence state
    if consec_contraction >= PERSISTENCE_THRESHOLD:
        dlog.persistence_state = "persistent_contraction"
        dlog.record(20, "persistence",
                    {"consecutive_contraction_count": consec_contraction,
                     "threshold": PERSISTENCE_THRESHOLD},
                    "persistent_contraction", "contraction_persists")
    elif consec_expansion >= PERSISTENCE_THRESHOLD:
        dlog.persistence_state = "persistent_expansion"
        dlog.record(20, "persistence",
                    {"consecutive_expansion_count": consec_expansion,
                     "threshold": PERSISTENCE_THRESHOLD},
                    "persistent_expansion", "expansion_persists")
    elif state_change_count > FLIP_THRESHOLD:
        dlog.persistence_state = "unstable_regime"
        dlog.record(20, "persistence",
                    {"state_change_count": state_change_count,
                     "flip_threshold": FLIP_THRESHOLD},
                    "unstable_regime", "regime_flipping_too_frequently")
    else:
        dlog.record(20, "persistence",
                    {"consec_expansion": consec_expansion,
                     "consec_contraction": consec_contraction},
                    "no_persistence_state", "insufficient_consecutive_windows")

    # Macro score trend
    if score_prev is not None and dlog.macro_regime_score is not None:
        delta = dlog.macro_regime_score - score_prev
        if delta > IMPROVEMENT_THRESHOLD:
            dlog.macro_trend_state = "improving"
            dlog.record(20, "macro_trend",
                        {"score_now": dlog.macro_regime_score, "score_prev": score_prev,
                         "delta": round(delta, 4)},
                        "improving", "macro_score_improving")
        elif delta < -DETERIORATION_THRESHOLD:
            dlog.macro_trend_state = "worsening"
            dlog.record(20, "macro_trend",
                        {"score_now": dlog.macro_regime_score, "score_prev": score_prev,
                         "delta": round(delta, 4)},
                        "worsening", "macro_score_worsening")
        else:
            dlog.record(20, "macro_trend",
                        {"delta": round(delta, 4)},
                        "stable", "macro_score_stable")


# ============================================================
# GATE 21 — EVALUATION LOOP CONTROLS
# ============================================================

def gate_21_evaluation_loop(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 21 — EVALUATION LOOP CONTROLS
    Records feedback events for model calibration.
    All threshold write-back operations are TODO:DATA_DEPENDENCY.
    """
    p = snapshot.get("snapshot_payload", {})

    # Snapshot retention
    if dlog.classification not in ("reject_snapshot", "stale_snapshot"):
        dlog.evaluation_notes.append("snapshot_retained")
        dlog.record(21, "eval_retention",
                    {"classification": dlog.classification},
                    "store_macro_state_record",
                    "snapshot_retained_for_evaluation")
        # TODO:DATA_DEPENDENCY — write macro_state_record to persistent evaluation store

    # Prediction accuracy
    regime_accuracy = p.get("regime_prediction_accuracy")
    if regime_accuracy is True:
        dlog.evaluation_notes.append("prediction_correct")
        dlog.record(21, "eval_accuracy",
                    {"regime_prediction_accuracy": True},
                    "reward", "predicted_regime_matched_realized_path")
        # TODO:DATA_DEPENDENCY — increment macro_model_score by reward value
    elif regime_accuracy is False:
        dlog.evaluation_notes.append("prediction_incorrect")
        dlog.record(21, "eval_accuracy",
                    {"regime_prediction_accuracy": False},
                    "penalty", "predicted_regime_mismatched_realized_path")
        # TODO:DATA_DEPENDENCY — decrement macro_model_score by penalty value

    # False positive correction — contraction
    contraction_fp_rate = p.get("contraction_false_positive_rate")
    contraction_fp_thresh= p.get("contraction_fp_threshold", 0.15)
    if (contraction_fp_rate is not None and
            contraction_fp_rate > contraction_fp_thresh):
        dlog.evaluation_notes.append("contraction_fp_threshold_breach")
        dlog.record(21, "eval_fp_contraction",
                    {"contraction_false_positive_rate": contraction_fp_rate,
                     "threshold": contraction_fp_thresh},
                    "adjust_contraction_thresholds",
                    "contraction_fp_rate_too_high")
        # TODO:DATA_DEPENDENCY — flag contraction threshold for human-supervised recalibration

    # False positive correction — expansion
    expansion_fp_rate = p.get("expansion_false_positive_rate")
    expansion_fp_thresh = p.get("expansion_fp_threshold", 0.15)
    if (expansion_fp_rate is not None and
            expansion_fp_rate > expansion_fp_thresh):
        dlog.evaluation_notes.append("expansion_fp_threshold_breach")
        dlog.record(21, "eval_fp_expansion",
                    {"expansion_false_positive_rate": expansion_fp_rate,
                     "threshold": expansion_fp_thresh},
                    "adjust_expansion_thresholds",
                    "expansion_fp_rate_too_high")
        # TODO:DATA_DEPENDENCY — flag expansion threshold for human-supervised recalibration


# ============================================================
# GATE 22 — OUTPUT CONTROLS
# ============================================================

def gate_22_output_controls(snapshot: dict, dlog: RegimeDecisionLog):
    """GATE 22 — OUTPUT CONTROLS: maps classification to output_action."""
    action_map = {
        "pro_growth_regime":          "emit_growth_regime",
        "defensive_regime":           "emit_defensive_regime",
        "stagflation_warning":        "emit_stagflation_alert",
        "reflation_signal":           "emit_reflation_signal",
        "disinflation_slowdown_signal": "emit_disinflation_slowdown_signal",
        "divergence_watch":           "emit_macro_divergence_warning",
        "no_clear_macro_signal":      "emit_uncertain_macro_state",
    }
    dlog.output_action = action_map.get(dlog.classification, "emit_uncertain_macro_state")
    dlog.record(22, "output_controls",
                {"classification": dlog.classification},
                dlog.output_action,
                f"output_action_from_{dlog.classification}")


# ============================================================
# GATE 23 — FINAL COMPOSITE MACRO SIGNAL
# ============================================================

def gate_23_final_macro_signal(snapshot: dict, dlog: RegimeDecisionLog):
    """
    GATE 23 — FINAL COMPOSITE MACRO SIGNAL
    Computes final_macro_signal and assigns final_macro_state.

    final_macro_signal = (macro_regime_score × macro_confidence)
                       - (WARN_PENALTY × len(warning_states))
                       + persistence_adjustment
    Clamped to [-1.0, +1.0].
    Stagflation override takes priority over score-based final state.
    """
    base = (dlog.macro_regime_score or 0.0) * (dlog.macro_confidence or 0.5)

    warn_penalty = FINAL_WARN_PENALTY * len(dlog.warning_states)

    persistence_adj = 0.0
    if dlog.persistence_state == "persistent_expansion":
        persistence_adj = 0.05
    elif dlog.persistence_state == "persistent_contraction":
        persistence_adj = -0.05
    elif dlog.persistence_state == "unstable_regime":
        persistence_adj = -0.10
    if dlog.macro_trend_state == "improving":
        persistence_adj += 0.03
    elif dlog.macro_trend_state == "worsening":
        persistence_adj -= 0.03

    signal = base - warn_penalty + persistence_adj
    signal = round(max(-1.0, min(1.0, signal)), 4)
    dlog.final_macro_signal = signal

    dlog.record(23, "final_signal",
                {"base": round(base, 4),
                 "warn_penalty": round(warn_penalty, 4),
                 "persistence_adj": round(persistence_adj, 4),
                 "final_macro_signal": signal},
                str(signal), "final_macro_signal_calculated")

    # Stagflation override
    if dlog.classification == "stagflation_warning":
        dlog.final_macro_state = "stagflation_override"
        dlog.record(23, "final_state",
                    {"classification": "stagflation_warning"},
                    "stagflation_override", "stagflation_override_active")
        return

    # Score-based final state
    if signal >= FINAL_STRONG_EXPANSION_THRESHOLD:
        dlog.final_macro_state = "strong_expansion"
        dlog.record(23, "final_state",
                    {"signal": signal,
                     "threshold": FINAL_STRONG_EXPANSION_THRESHOLD},
                    "strong_expansion", "signal_strongly_expansionary")
    elif signal >= FINAL_EXPANSION_THRESHOLD:
        dlog.final_macro_state = "mild_expansion"
        dlog.record(23, "final_state",
                    {"signal": signal,
                     "expansion_threshold": FINAL_EXPANSION_THRESHOLD,
                     "strong_expansion_threshold": FINAL_STRONG_EXPANSION_THRESHOLD},
                    "mild_expansion", "signal_mildly_expansionary")
    elif signal >= FINAL_NEUTRAL_LOW:
        dlog.final_macro_state = "neutral"
        dlog.record(23, "final_state",
                    {"signal": signal,
                     "neutral_low": FINAL_NEUTRAL_LOW,
                     "neutral_high": FINAL_EXPANSION_THRESHOLD},
                    "neutral", "signal_neutral")
    elif signal > FINAL_STRONG_CONTRACTION_THRESHOLD:
        dlog.final_macro_state = "mild_contraction"
        dlog.record(23, "final_state",
                    {"signal": signal,
                     "strong_contraction_threshold": FINAL_STRONG_CONTRACTION_THRESHOLD},
                    "mild_contraction", "signal_mildly_contractionary")
    else:
        dlog.final_macro_state = "strong_contraction"
        dlog.record(23, "final_state",
                    {"signal": signal,
                     "threshold": FINAL_STRONG_CONTRACTION_THRESHOLD},
                    "strong_contraction", "signal_strongly_contractionary")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def process_snapshot(snapshot: dict) -> dict:
    """
    Orchestrates the 23-gate macro regime classification pipeline.

    Args:
        snapshot: dict with keys:
            snapshot_id           — identifier for this snapshot
            timestamp             — ISO UTC timestamp of the snapshot
            macro_data_status     — "online" or other
            SPX_feed_status       — "online" or other
            snapshot_payload      — dict of all macro metric values
            processed_snapshot_store — list of previously processed SHA-256 hashes

    Returns:
        Complete decision log dict produced by RegimeDecisionLog.to_dict().
    """
    snapshot_id = snapshot.get("snapshot_id", "unknown")
    dlog        = RegimeDecisionLog(snapshot_id)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Input universe
    gate_2_input_universe(snapshot, dlog)

    # Gates 3-13: Component regime gates (all non-halting)
    gate_3_benchmark(snapshot, dlog)
    gate_4_inflation(snapshot, dlog)
    gate_5_growth(snapshot, dlog)
    gate_6_labor(snapshot, dlog)
    gate_7_policy(snapshot, dlog)
    gate_8_yield_curve(snapshot, dlog)
    gate_9_credit(snapshot, dlog)
    gate_10_liquidity(snapshot, dlog)
    gate_11_fx_global(snapshot, dlog)
    gate_12_commodity(snapshot, dlog)
    gate_13_macro_news(snapshot, dlog)

    # Gate 14: Component scoring
    gate_14_composite_construction(snapshot, dlog)

    # Gate 15: Dynamic weighting
    gate_15_weighting(snapshot, dlog)

    # Gate 16: Regime score and classification
    gate_16_composite_regime_score(snapshot, dlog)

    # Gate 17: Confidence
    gate_17_confidence(snapshot, dlog)

    # Gate 18: Divergence warnings
    gate_18_divergence_warnings(snapshot, dlog)

    # Gate 19: Action classification
    gate_19_action_classification(snapshot, dlog)

    # Gate 20: Temporal persistence
    gate_20_temporal_persistence(snapshot, dlog)

    # Gate 21: Evaluation loop
    gate_21_evaluation_loop(snapshot, dlog)

    # Gate 22: Output controls
    gate_22_output_controls(snapshot, dlog)

    # Gate 23: Final composite signal
    gate_23_final_macro_signal(snapshot, dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_regime_log(result: dict):
    """Writes the macro regime decision log to the database event log."""
    _db.log_event(
        event_type="macro_regime_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity regime classifications.
    """
    cls   = result.get("classification")
    fms   = result.get("final_macro_state")
    sid   = result.get("snapshot_id")

    if cls == "stagflation_warning":
        _db.post_suggestion(
            agent="macro_regime_agent",
            classification="stagflation_warning",
            risk_level="HIGH",
            payload=result,
            note="Stagflation regime detected — weak growth with high inflation. Immediate review required.",
        )
        log.warning("ESCALATE: stagflation_warning for snapshot %s", sid)

    elif cls == "defensive_regime":
        _db.post_suggestion(
            agent="macro_regime_agent",
            classification="defensive_regime",
            risk_level="HIGH",
            payload=result,
            note="Contraction regime confirmed with high confidence. Review risk controls.",
        )
        log.warning("ESCALATE: defensive_regime for snapshot %s", sid)

    elif cls == "divergence_watch":
        _db.post_suggestion(
            agent="macro_regime_agent",
            classification="divergence_watch",
            risk_level="MEDIUM",
            payload=result,
            note=f"Macro divergence warnings: {result.get('warning_states')}",
        )
        log.warning("ESCALATE: divergence_watch for snapshot %s", sid)

    elif fms == "strong_contraction":
        _db.post_suggestion(
            agent="macro_regime_agent",
            classification="strong_contraction",
            risk_level="HIGH",
            payload=result,
            note="Final macro signal strongly contractionary. Review exposure.",
        )
        log.warning("ESCALATE: strong_contraction final signal for snapshot %s", sid)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Macro Regime Agent — 23-gate macro classification spine"
    )
    parser.add_argument("--snapshot-id", default="cli-run",
                        help="Snapshot ID for this run")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Minimal test snapshot — macro data online, all series active, neutral data
    test_snapshot = {
        "snapshot_id":   args.snapshot_id,
        "timestamp":     datetime.datetime.utcnow().isoformat(),
        "macro_data_status": "online",
        "SPX_feed_status":   "online",
        "processed_snapshot_store": [],
        "snapshot_payload": {
            # Availability flags (all active for test)
            "inflation_series_available":  True,
            "growth_series_available":     True,
            "labor_series_available":      True,
            "policy_series_available":     True,
            "curve_series_available":      True,
            "credit_series_available":     True,
            "liquidity_series_available":  True,
            "fx_series_available":         True,
            "commodity_series_available":  True,
            "macro_news_available":        True,
            # Benchmark
            "MA_short_SPX":     4900.0,
            "MA_long_SPX":      4800.0,
            "SPX_current":      5000.0,
            "rolling_peak_SPX": 5100.0,
            "realized_vol_SPX": 0.15,
            "VIX":              18.0,
            # Inflation
            "headline_CPI_yoy": 0.030,
            "core_CPI_yoy":     0.032,
            "CPI_yoy_prev":     0.031,
            "inflation_actual": 0.030,
            "inflation_consensus": 0.031,
            # Growth
            "GDP_nowcast":          2.2,
            "composite_growth_index": 2.0,
            "growth_nowcast_prev":  2.1,
            "PMI_manufacturing":    51.5,
            "PMI_services":         54.0,
            "recession_probability": 0.12,
            # Labor
            "unemployment_rate":     0.038,
            "unemployment_rate_prev":0.038,
            "job_openings_ratio":    1.30,
            "nonfarm_payrolls_3m_avg": 180_000,
            "average_hourly_earnings_yoy": 0.040,
            "initial_claims_4w_avg": 215_000,
            # Policy
            "real_policy_rate":              0.018,
            "policy_rate_change_6m":         0.0,
            "central_bank_balance_sheet_change": 0.0,
            "policy_market_implied_shift":   0.0,
            # Curve
            "yield_10y":        0.043,
            "yield_2y":         0.047,
            "spread_10y_2y_prev": -0.003,
            "yield_10y_change": 0.0002,
            "yield_2y_change":  0.0001,
            "term_premium_change": 0.0,
            # Credit
            "IG_spread_change":    0.0,
            "HY_spread_change":    0.0,
            "default_rate_nowcast":0.025,
            "funding_spread":      0.002,
            # Liquidity
            "net_liquidity_change":         0.0,
            "bank_reserves_metric":         3.2e12,
            "short_term_funding_metric":    0.001,
            "net_treasury_supply_change":   50e9,
            # FX
            "DXY_return":          0.005,
            "EM_fx_stress_index":  0.45,
            "global_PMI_change":   0.3,
            # Commodities
            "WTI_return":                   0.02,
            "industrial_metals_return":     0.03,
            "broad_commodity_index_return": 0.01,
            "food_commodity_index_change":  0.02,
            # News
            "macro_news_sentiment_score":          0.10,
            "hawkish_term_density":                0.08,
            "dovish_term_density":                 0.06,
            "negative_macro_news_confirmations":   0,
            "positive_macro_news_confirmations":   1,
            # Quality and history
            "input_quality_scores":            [0.85] * 10,
            "consecutive_expansion_count":     1,
            "consecutive_contraction_count":   0,
            "recent_state_change_count":       1,
            "macro_regime_score_prev":         None,
        },
    }

    log.info("Running macro regime agent for snapshot: %s", args.snapshot_id)
    result = process_snapshot(test_snapshot)
    write_regime_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"MACRO REGIME AGENT — snapshot: {result.get('snapshot_id')}")
        print(f"{'='*72}")
        print(f"Macro Regime State   : {result.get('macro_regime_state')}")
        print(f"Classification       : {result.get('classification')}")
        print(f"Output Action        : {result.get('output_action')}")
        print(f"Macro Regime Score   : {result.get('macro_regime_score')}")
        print(f"Macro Confidence     : {result.get('macro_confidence')}")
        print(f"Final Macro Signal   : {result.get('final_macro_signal')}")
        print(f"Final Macro State    : {result.get('final_macro_state')}")
        print(f"Warning States       : {result.get('warning_states')}")
        print(f"\nComponent Scores:")
        for comp, score in sorted(result.get("component_scores", {}).items()):
            wt = result.get("component_weights", {}).get(comp, 0)
            print(f"  {comp:12s}  score={score:+.4f}  weight={wt:.4f}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
