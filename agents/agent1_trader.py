#!/usr/bin/env python3
"""
agent1_trader.py — Agent 1 (ExecutionAgent)
14-gate deterministic trade execution spine.
All decisions are rule-based and fully traceable.
AGENT_VERSION = "1.0.0"
"""
import sys
import os
import json
import logging
import datetime
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent1_trader")

_paths = get_paths()
_db    = get_db_helpers()

AGENT_VERSION = "1.0.0"

# ============================================================
# CONFIG BLOCK
# ============================================================

TRADING_MODE = os.environ.get("TRADING_MODE", "PAPER")  # PAPER or LIVE

# Session windows (ET)
SESSION_OPEN_HOUR    = 9
SESSION_OPEN_MIN     = 30
SESSION_MIDDAY_HOUR  = 12
SESSION_MIDDAY_MIN   = 30
SESSION_CLOSE_HOUR   = 15
SESSION_CLOSE_MIN    = 30

# Gate 1 — Hard stops
MAX_DAILY_LOSS_PCT        = 0.02    # 2% of equity
MAX_DRAWDOWN_PCT          = 0.10    # 10% from high-water mark
MAX_DATA_LATENCY_MIN      = 5       # minutes
MAX_MISSING_DATA_RATIO    = 0.20    # 20%
MAX_API_FAILURES          = 3       # consecutive failures before halt

# Gate 2 — Benchmark
BENCHMARK_TICKER          = "SPY"
BENCHMARK_SMA_SHORT       = 20
BENCHMARK_SMA_LONG        = 50
BENCHMARK_DRAWDOWN_THRESH = 0.05    # 5% rolling drawdown → DEFENSIVE
BENCHMARK_ATR_NORMAL_MAX  = 0.015   # ATR/price < 1.5% → can be AGGRESSIVE

# Gate 3 — Regime
REGIME_BULL_SMA_SPREAD    = 0.005   # SMA20/SMA50 spread > 0.5% → BULL
REGIME_BEAR_SMA_SPREAD    = -0.005
REGIME_VOL_HIGH_THRESH    = 0.020   # ATR/price > 2% → HIGH vol
REGIME_VOL_LOW_THRESH     = 0.008

# Gate 4 — Eligibility
MIN_AVG_DAILY_VOLUME      = 500_000
MAX_SPREAD_PCT            = 0.005   # 0.5% of mid-price
CORRELATION_CAP           = 0.80    # max correlation to existing portfolio

# Gate 5 — Signal evaluation
SIG_MIN_CONFIDENCE        = 0.40    # minimum composite score to proceed
SIG_W_SOURCE_TIER         = 0.25
SIG_W_POLITICIAN_WEIGHT   = 0.20
SIG_W_STALENESS           = 0.15
SIG_W_INTERROGATION       = 0.15
SIG_W_SENTIMENT_CORR      = 0.15
SIG_W_BENCHMARK_REL       = 0.10
SIG_STALENESS_MAX_DAYS    = 3       # signals > 3 days old score 0 on staleness

# Gate 6 — Entry decision
MOMENTUM_ROC_THRESH       = 0.02    # 2% ROC for momentum entry
MEAN_REV_ZSCORE_THRESH    = 2.0     # Z-score threshold for mean-reversion
BREAKOUT_LOOKBACK         = 20      # N-period high for breakout
PULLBACK_RETRACE_PCT      = 0.382   # 38.2% Fibonacci pullback

# Gate 7 — Position sizing
RISK_PER_TRADE_PCT        = 0.01    # 1% of equity per trade
ATR_STOP_MULTIPLIER       = 2.0     # stop = entry - ATR_STOP_MULTIPLIER * ATR
DEFENSIVE_SIZE_FACTOR     = 0.50
AGGRESSIVE_SIZE_FACTOR    = 1.25
MAX_POSITION_PCT          = 0.10    # max 10% of equity in one position

# Gate 8 — Risk setup
REWARD_RISK_RATIO         = 2.0     # profit target = entry + RRR * stop_distance
TRAILING_STOP_ATR_MULT    = 2.0
OVERNIGHT_SIZE_FACTOR     = 0.75    # reduce size for overnight holds
GAP_RISK_STD_THRESH       = 0.015   # skip if historical gap std > 1.5%

# Gate 9 — Execution
MAX_SLIPPAGE_PCT          = 0.003   # 0.3% slippage tolerance
EXEC_WINDOW_START_MIN     = 5       # 5 min after open
EXEC_WINDOW_END_MIN       = 30      # 30 min before close

# Gate 10 — Active management
EXIT_CONF_FLOOR           = 0.25    # signal confidence below this → exit
MAX_HOLDING_DAYS          = 20      # exit after 20 trading days
BENCH_UNDERPERF_THRESH    = -0.03   # -3% relative to benchmark → exit

# Gate 11 — Portfolio controls
MAX_GROSS_EXPOSURE_PCT    = 0.90    # 90% of equity
MAX_SECTOR_EXPOSURE_PCT   = 0.25    # 25% in any one sector
MAX_PORT_CORRELATION      = 0.70    # mean pairwise correlation limit
MAX_LEVERAGE              = 1.0     # no leverage by default

# Gate 12 — Adaptive layer
MIN_SHARPE_ROLLING        = 0.50
PARAM_DRIFT_THRESH        = 0.20

# Gate 13 — Stress overrides
FLASH_CRASH_PCT           = 0.03    # 3% drop → flash crash
FLASH_CRASH_MIN           = 10      # within 10 minutes
SPREAD_EXPLOSION_MULT     = 5.0     # spread > 5x normal → liquidity collapse
BENCHMARK_CRASH_PCT       = 0.05    # 5% intraday drop → benchmark crash

# Gate 14 — Evaluation loop
SHARPE_KILL_MIN           = 0.0     # Sharpe < 0 AND drawdown breach → kill
DRAWDOWN_KILL_MAX         = 0.15    # 15% drawdown with low Sharpe → kill
SHARPE_ROLLING_WINDOW     = 20      # trading days


# ============================================================
# HELPERS
# ============================================================

def _mean(lst):
    return sum(lst) / len(lst) if lst else 0.0


# ============================================================
# TradeDecisionLog
# ============================================================

@dataclass
class TradeDecisionLog:
    session_ts: str
    session_type: str       # "open" / "midday" / "close"
    ticker: str = ""
    signal_id: str = ""
    halted: bool = False
    halt_reason: str = ""

    # Gate 1
    g1_market_hours_ok: bool = False
    g1_daily_loss_ok: bool = False
    g1_drawdown_ok: bool = False
    g1_data_ok: bool = False
    g1_api_ok: bool = False
    g1_result: str = ""

    # Gate 2
    benchmark_mode: str = ""
    benchmark_trend: str = ""
    benchmark_drawdown_pct: float = 0.0

    # Gate 3
    vol_regime: str = ""
    trend_regime: str = ""
    risk_posture: str = ""

    # Gate 4
    eligibility_result: str = ""
    eligibility_reason: str = ""

    # Gate 5
    signal_confidence: float = 0.0
    signal_result: str = ""

    # Gate 6
    entry_type: str = ""
    entry_result: str = ""

    # Gate 7
    position_size: float = 0.0
    size_calc_log: dict = field(default_factory=dict)

    # Gate 8
    stop_level: float = 0.0
    target_level: float = 0.0
    trailing_stop: float = 0.0

    # Gate 9
    order_status: str = ""
    fill_price: float = 0.0
    fill_qty: float = 0.0
    order_id: str = ""

    # Gate 10
    active_mgmt_result: str = ""
    exit_reason: str = ""

    # Gate 11
    portfolio_entry_allowed: bool = True
    portfolio_block_reason: str = ""

    # Gate 12
    adaptive_result: str = ""
    adaptive_flag: str = ""

    # Gate 13
    stress_state: str = ""
    stress_response: str = ""

    # Gate 14
    metrics_updated: bool = False
    kill_condition: bool = False

    final_decision: str = ""    # MIRROR / WATCH / SKIP / EXIT / HOLD
    decision_log: list = field(default_factory=list)

    def record(self, gate_num, gate_name, inputs, result, reason_code, ts=None):
        if ts is None:
            ts = datetime.datetime.utcnow().isoformat()
        self.decision_log.append({
            "gate_num": gate_num,
            "gate_name": gate_name,
            "inputs": inputs,
            "result": result,
            "reason_code": reason_code,
            "ts": ts,
        })

    def halt(self, reason):
        self.halted = True
        self.halt_reason = reason

    def to_dict(self):
        return {
            "session_ts": self.session_ts,
            "session_type": self.session_type,
            "ticker": self.ticker,
            "signal_id": self.signal_id,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "g1_market_hours_ok": self.g1_market_hours_ok,
            "g1_daily_loss_ok": self.g1_daily_loss_ok,
            "g1_drawdown_ok": self.g1_drawdown_ok,
            "g1_data_ok": self.g1_data_ok,
            "g1_api_ok": self.g1_api_ok,
            "g1_result": self.g1_result,
            "benchmark_mode": self.benchmark_mode,
            "benchmark_trend": self.benchmark_trend,
            "benchmark_drawdown_pct": self.benchmark_drawdown_pct,
            "vol_regime": self.vol_regime,
            "trend_regime": self.trend_regime,
            "risk_posture": self.risk_posture,
            "eligibility_result": self.eligibility_result,
            "eligibility_reason": self.eligibility_reason,
            "signal_confidence": self.signal_confidence,
            "signal_result": self.signal_result,
            "entry_type": self.entry_type,
            "entry_result": self.entry_result,
            "position_size": self.position_size,
            "size_calc_log": self.size_calc_log,
            "stop_level": self.stop_level,
            "target_level": self.target_level,
            "trailing_stop": self.trailing_stop,
            "order_status": self.order_status,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "order_id": self.order_id,
            "active_mgmt_result": self.active_mgmt_result,
            "exit_reason": self.exit_reason,
            "portfolio_entry_allowed": self.portfolio_entry_allowed,
            "portfolio_block_reason": self.portfolio_block_reason,
            "adaptive_result": self.adaptive_result,
            "adaptive_flag": self.adaptive_flag,
            "stress_state": self.stress_state,
            "stress_response": self.stress_response,
            "metrics_updated": self.metrics_updated,
            "kill_condition": self.kill_condition,
            "final_decision": self.final_decision,
            "decision_log": self.decision_log,
        }

    def to_human_readable(self):
        lines = [
            f"=== TradeDecisionLog ===",
            f"Session: {self.session_ts} ({self.session_type})",
            f"Ticker: {self.ticker}  Signal ID: {self.signal_id}",
            f"Halted: {self.halted}  Halt Reason: {self.halt_reason}",
            f"",
            f"--- Gate 1: System Check ---",
            f"  Market Hours OK : {self.g1_market_hours_ok}",
            f"  Daily Loss OK   : {self.g1_daily_loss_ok}",
            f"  Drawdown OK     : {self.g1_drawdown_ok}",
            f"  Data OK         : {self.g1_data_ok}",
            f"  API OK          : {self.g1_api_ok}",
            f"  Result          : {self.g1_result}",
            f"",
            f"--- Gate 2: Benchmark ---",
            f"  Mode     : {self.benchmark_mode}",
            f"  Trend    : {self.benchmark_trend}",
            f"  Drawdown : {self.benchmark_drawdown_pct:.4f}",
            f"",
            f"--- Gate 3: Regime ---",
            f"  Vol Regime   : {self.vol_regime}",
            f"  Trend Regime : {self.trend_regime}",
            f"  Risk Posture : {self.risk_posture}",
            f"",
            f"--- Gate 4: Eligibility ---",
            f"  Result : {self.eligibility_result}",
            f"  Reason : {self.eligibility_reason}",
            f"",
            f"--- Gate 5: Signal Evaluation ---",
            f"  Confidence : {self.signal_confidence:.4f}",
            f"  Result     : {self.signal_result}",
            f"",
            f"--- Gate 6: Entry Decision ---",
            f"  Entry Type   : {self.entry_type}",
            f"  Entry Result : {self.entry_result}",
            f"",
            f"--- Gate 7: Position Sizing ---",
            f"  Position Size  : {self.position_size}",
            f"  Size Calc Log  : {self.size_calc_log}",
            f"",
            f"--- Gate 8: Risk Setup ---",
            f"  Stop Level    : {self.stop_level}",
            f"  Target Level  : {self.target_level}",
            f"  Trailing Stop : {self.trailing_stop}",
            f"",
            f"--- Gate 9: Execution ---",
            f"  Order Status : {self.order_status}",
            f"  Fill Price   : {self.fill_price}",
            f"  Fill Qty     : {self.fill_qty}",
            f"  Order ID     : {self.order_id}",
            f"",
            f"--- Gate 10: Active Management ---",
            f"  Result      : {self.active_mgmt_result}",
            f"  Exit Reason : {self.exit_reason}",
            f"",
            f"--- Gate 11: Portfolio Controls ---",
            f"  Entry Allowed  : {self.portfolio_entry_allowed}",
            f"  Block Reason   : {self.portfolio_block_reason}",
            f"",
            f"--- Gate 12: Adaptive Layer ---",
            f"  Result : {self.adaptive_result}",
            f"  Flag   : {self.adaptive_flag}",
            f"",
            f"--- Gate 13: Stress Overrides ---",
            f"  Stress State    : {self.stress_state}",
            f"  Stress Response : {self.stress_response}",
            f"",
            f"--- Gate 14: Evaluation Loop ---",
            f"  Metrics Updated : {self.metrics_updated}",
            f"  Kill Condition  : {self.kill_condition}",
            f"",
            f"FINAL DECISION: {self.final_decision}",
            f"",
            f"--- Decision Log ({len(self.decision_log)} entries) ---",
        ]
        for entry in self.decision_log:
            lines.append(
                f"  [{entry['gate_num']}] {entry['gate_name']} → {entry['result']} ({entry['reason_code']}) @ {entry['ts']}"
            )
        return "\n".join(lines)


# ============================================================
# GATE FUNCTIONS
# ============================================================

def gate_1_system_check(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    session_time = context.get("session_time", datetime.datetime.utcnow())

    # Market hours check: 9:30 <= time <= 16:00 ET
    hour = session_time.hour
    minute = session_time.minute
    if hour > 9 or (hour == 9 and minute >= 30):
        state.g1_market_hours_ok = (hour < 16)
    else:
        state.g1_market_hours_ok = False

    if not state.g1_market_hours_ok:
        state.halt("outside_market_hours")

    # Daily loss check
    daily_pnl = context.get("daily_pnl", 0)
    equity = context.get("equity", 1)
    if equity != 0 and (daily_pnl / equity) <= -MAX_DAILY_LOSS_PCT:
        state.g1_daily_loss_ok = False
        state.halt("daily_loss_limit")
    else:
        state.g1_daily_loss_ok = True

    # Drawdown check
    peak_equity = context.get("peak_equity", equity)
    if peak_equity != 0:
        dd = (peak_equity - equity) / peak_equity
    else:
        dd = 0.0
    if dd >= MAX_DRAWDOWN_PCT:
        state.g1_drawdown_ok = False
        state.halt("portfolio_drawdown")
    else:
        state.g1_drawdown_ok = True

    # Data integrity check
    data_age_min = context.get("data_age_min", 0)
    missing_data_ratio = context.get("missing_data_ratio", 0)
    if data_age_min > MAX_DATA_LATENCY_MIN or missing_data_ratio > MAX_MISSING_DATA_RATIO:
        state.g1_data_ok = False
        state.halt("data_integrity_failure")
    else:
        state.g1_data_ok = True

    # API health check
    api_failure_count = context.get("api_failure_count", 0)
    if api_failure_count >= MAX_API_FAILURES:
        state.g1_api_ok = False
        state.halt("api_health_failure")
    else:
        state.g1_api_ok = True

    if not state.halted:
        state.g1_result = "PROCEED"
    else:
        state.g1_result = f"HALTED:{state.halt_reason}"
        try:
            _db.post_suggestion("agent1_halt", {
                "halt_reason": state.halt_reason,
                "session_ts": state.session_ts,
                "ticker": state.ticker,
                "daily_pnl": daily_pnl,
                "equity": equity,
                "peak_equity": peak_equity,
                "data_age_min": data_age_min,
                "api_failure_count": api_failure_count,
            })
        except Exception as e:
            log.error(f"Gate 1 _db.post_suggestion failed: {e}")

    inputs = {
        "session_time": str(session_time),
        "daily_pnl": daily_pnl,
        "equity": equity,
        "peak_equity": peak_equity,
        "data_age_min": data_age_min,
        "missing_data_ratio": missing_data_ratio,
        "api_failure_count": api_failure_count,
    }
    state.record(1, "System Check", inputs, state.g1_result, state.halt_reason or "ok", ts)


def gate_2_benchmark(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    benchmark_bars = context.get("benchmark_bars", [])

    if not benchmark_bars or len(benchmark_bars) < BENCHMARK_SMA_LONG:
        state.benchmark_mode = "NEUTRAL"
        state.benchmark_trend = "unknown"
        state.benchmark_drawdown_pct = 0.0
        state.record(2, "Benchmark", {"bars_count": len(benchmark_bars)}, "NEUTRAL", "insufficient_data", ts)
        return

    closes = [b["close"] for b in benchmark_bars]

    sma_short = _mean(closes[-BENCHMARK_SMA_SHORT:])
    sma_long = _mean(closes[-BENCHMARK_SMA_LONG:])
    sma_ratio = (sma_short - sma_long) / sma_long if sma_long != 0 else 0.0

    if sma_ratio > 0.002:
        benchmark_trend = "bull"
    elif sma_ratio < -0.002:
        benchmark_trend = "bear"
    else:
        benchmark_trend = "neutral"

    recent_closes_20 = closes[-20:]
    period_high = max(recent_closes_20) if recent_closes_20 else closes[-1]
    benchmark_drawdown_pct = (period_high - closes[-1]) / period_high if period_high != 0 else 0.0

    # ATR proxy: mean of last 5 absolute daily moves / last close
    atr_moves = [abs(closes[i] - closes[i - 1]) for i in range(-5, 0) if len(closes) >= abs(i)]
    atr_pct = (_mean(atr_moves) / closes[-1]) if closes[-1] != 0 else 0.0

    if benchmark_drawdown_pct > BENCHMARK_DRAWDOWN_THRESH:
        benchmark_mode = "DEFENSIVE"
    elif benchmark_trend == "bull" and atr_pct <= BENCHMARK_ATR_NORMAL_MAX:
        benchmark_mode = "AGGRESSIVE"
    else:
        benchmark_mode = "NEUTRAL"

    state.benchmark_mode = benchmark_mode
    state.benchmark_trend = benchmark_trend
    state.benchmark_drawdown_pct = benchmark_drawdown_pct

    inputs = {
        "bars_count": len(closes),
        "sma_short": round(sma_short, 4),
        "sma_long": round(sma_long, 4),
        "sma_ratio": round(sma_ratio, 6),
        "benchmark_drawdown_pct": round(benchmark_drawdown_pct, 6),
        "atr_pct": round(atr_pct, 6),
    }
    state.record(2, "Benchmark", inputs, benchmark_mode, benchmark_trend, ts)


def gate_3_regime(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    benchmark_bars = context.get("benchmark_bars", [])

    if not benchmark_bars or len(benchmark_bars) < 20:
        state.vol_regime = "NORMAL"
        state.trend_regime = "SIDEWAYS"
        state.risk_posture = "NEUTRAL"
        state.record(3, "Regime", {"bars_count": len(benchmark_bars)}, "NEUTRAL", "insufficient_data", ts)
        return

    closes = [b["close"] for b in benchmark_bars]

    # ATR-based vol regime: mean of last 10 abs daily moves / last close
    atr_moves = [abs(closes[i] - closes[i - 1]) for i in range(-10, 0) if len(closes) >= abs(i)]
    atr = _mean(atr_moves) / closes[-1] if closes[-1] != 0 else 0.0

    if atr > REGIME_VOL_HIGH_THRESH:
        vol_regime = "HIGH"
    elif atr < REGIME_VOL_LOW_THRESH:
        vol_regime = "LOW"
    else:
        vol_regime = "NORMAL"

    # SMA-based trend regime
    sma20 = _mean(closes[-20:])
    if len(closes) >= 50:
        sma50 = _mean(closes[-50:])
    else:
        sma50 = _mean(closes)
    spread = (sma20 - sma50) / sma50 if sma50 != 0 else 0.0

    if spread > REGIME_BULL_SMA_SPREAD:
        trend_regime = "BULL"
    elif spread < REGIME_BEAR_SMA_SPREAD:
        trend_regime = "BEAR"
    else:
        trend_regime = "SIDEWAYS"

    # Risk posture: TODO:DATA_DEPENDENCY — TLT and credit spread data
    tlt_bars = context.get("tlt_bars", [])
    if tlt_bars and len(tlt_bars) >= 5:
        tlt_return = (tlt_bars[-1]["close"] - tlt_bars[-5]["close"]) / tlt_bars[-5]["close"] if tlt_bars[-5]["close"] != 0 else 0.0
        if trend_regime == "BEAR" and tlt_return > 0.01:
            risk_posture = "RISK_OFF"
        elif trend_regime == "BULL" and tlt_return < 0:
            risk_posture = "RISK_ON"
        else:
            risk_posture = "NEUTRAL"
    else:
        risk_posture = "NEUTRAL"  # TODO:DATA_DEPENDENCY

    state.vol_regime = vol_regime
    state.trend_regime = trend_regime
    state.risk_posture = risk_posture

    inputs = {
        "bars_count": len(closes),
        "atr_pct": round(atr, 6),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "spread": round(spread, 6),
        "tlt_bars_count": len(tlt_bars),
    }
    result = f"{trend_regime}/{vol_regime}/{risk_posture}"
    state.record(3, "Regime", inputs, result, risk_posture, ts)


def gate_4_eligibility(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    signal = context.get("signal", {})
    if not signal:
        state.eligibility_result = "SKIP"
        state.eligibility_reason = "no_signal"
        state.record(4, "Eligibility", {}, "SKIP", "no_signal", ts)
        return

    # Liquidity check
    avg_vol = context.get("avg_daily_volume", 0)
    if avg_vol < MIN_AVG_DAILY_VOLUME:
        state.eligibility_result = "SKIP"
        state.eligibility_reason = "low_liquidity"
        state.record(4, "Eligibility", {"avg_daily_volume": avg_vol}, "SKIP", "low_liquidity", ts)
        return

    # Spread check
    bid = context.get("bid", 0)
    ask = context.get("ask", 0)
    if bid > 0 and ask > 0:
        mid = (ask + bid) / 2
        spread_pct = (ask - bid) / mid if mid != 0 else 0.0
        if spread_pct > MAX_SPREAD_PCT:
            state.eligibility_result = "SKIP"
            state.eligibility_reason = "wide_spread"
            state.record(4, "Eligibility", {"spread_pct": round(spread_pct, 6)}, "SKIP", "wide_spread", ts)
            return
    else:
        spread_pct = 0.0

    # Event risk check — TODO:DATA_DEPENDENCY automated event calendar
    if context.get("event_exclusion", False):
        state.eligibility_result = "SKIP"
        state.eligibility_reason = "event_exclusion_window"
        state.record(4, "Eligibility", {"event_exclusion": True}, "SKIP", "event_exclusion_window", ts)
        return

    # Correlation check
    port_corr = context.get("portfolio_correlations", {}).get(signal.get("ticker", ""), 0.0)
    if port_corr > CORRELATION_CAP:
        state.eligibility_result = "SKIP"
        state.eligibility_reason = "correlated_exposure"
        state.record(4, "Eligibility", {"portfolio_correlation": port_corr}, "SKIP", "correlated_exposure", ts)
        return

    state.eligibility_result = "ELIGIBLE"
    state.eligibility_reason = "all_checks_passed"

    inputs = {
        "ticker": signal.get("ticker", ""),
        "avg_daily_volume": avg_vol,
        "spread_pct": round(spread_pct, 6),
        "port_corr": port_corr,
    }
    state.record(4, "Eligibility", inputs, "ELIGIBLE", "all_checks_passed", ts)


def gate_5_signal_evaluation(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    signal = context.get("signal", {})
    if not signal or state.eligibility_result == "SKIP":
        state.signal_result = "SKIP"
        state.signal_confidence = 0.0
        state.record(5, "Signal Evaluation", {}, "SKIP", "no_signal_or_ineligible", ts)
        return

    source_tier_score = min(1.0, signal.get("source_tier", 0.5))
    politician_weight_score = signal.get("politician_weight", 0.5)

    staleness_days = signal.get("staleness_days", 0)
    staleness_score = max(0.0, 1.0 - staleness_days / SIG_STALENESS_MAX_DAYS)

    interrogation_status = signal.get("interrogation_status", "unverified")
    if interrogation_status == "validated":
        interrogation_score = 1.0
    elif interrogation_status == "neutral":
        interrogation_score = 0.5
    elif interrogation_status == "challenged":
        interrogation_score = 0.0
    else:
        interrogation_score = 0.3

    sentiment_corr_tier = signal.get("sentiment_corroboration_tier", 4)
    if sentiment_corr_tier == 1:    # CRITICAL cascade scan — contrarian/negative for entry
        sentiment_score = 0.2
    elif sentiment_corr_tier == 2:
        sentiment_score = 0.4
    elif sentiment_corr_tier == 3:  # NEUTRAL
        sentiment_score = 0.6
    elif sentiment_corr_tier == 4:  # QUIET
        sentiment_score = 0.7
    else:
        sentiment_score = 0.5

    benchmark_rel = signal.get("benchmark_rel_strength", 0.5)

    signal_confidence = (
        SIG_W_SOURCE_TIER       * source_tier_score +
        SIG_W_POLITICIAN_WEIGHT * politician_weight_score +
        SIG_W_STALENESS         * staleness_score +
        SIG_W_INTERROGATION     * interrogation_score +
        SIG_W_SENTIMENT_CORR    * sentiment_score +
        SIG_W_BENCHMARK_REL     * benchmark_rel
    )
    signal_confidence = round(max(0.0, min(1.0, signal_confidence)), 4)

    signal_result = "PROCEED" if signal_confidence >= SIG_MIN_CONFIDENCE else "SKIP"

    state.signal_confidence = signal_confidence
    state.signal_result = signal_result

    inputs = {
        "source_tier_score": source_tier_score,
        "politician_weight_score": politician_weight_score,
        "staleness_days": staleness_days,
        "staleness_score": staleness_score,
        "interrogation_status": interrogation_status,
        "interrogation_score": interrogation_score,
        "sentiment_corr_tier": sentiment_corr_tier,
        "sentiment_score": sentiment_score,
        "benchmark_rel": benchmark_rel,
        "signal_confidence": signal_confidence,
    }
    state.record(5, "Signal Evaluation", inputs, signal_result, f"confidence_{signal_confidence}", ts)


def gate_6_entry_decision(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    if state.signal_result == "SKIP":
        state.entry_result = "NO_ENTRY"
        state.entry_type = "none"
        state.record(6, "Entry Decision", {}, "NO_ENTRY", "signal_skipped", ts)
        return

    price_bars = context.get("price_bars", [])
    closes = [b["close"] for b in price_bars]
    entry_price = context.get("entry_price", closes[-1] if closes else 0.0)
    regime = state.trend_regime

    candidates = []

    # Momentum
    if regime in ["BULL", "NEUTRAL"] and len(closes) >= 5:
        roc = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0.0
        sma20 = _mean(closes[-20:]) if len(closes) >= 20 else _mean(closes)
        if roc > MOMENTUM_ROC_THRESH and entry_price > sma20:
            candidates.append(("momentum", state.signal_confidence * 1.0))

    # Breakout
    if regime in ["BULL", "NEUTRAL"] and len(closes) >= BREAKOUT_LOOKBACK + 1:
        period_high = max(closes[-(BREAKOUT_LOOKBACK + 1):-1])
        if entry_price > period_high:
            candidates.append(("breakout", state.signal_confidence * 0.95))

    # Mean-reversion
    if regime == "SIDEWAYS" and len(closes) >= 20:
        sma = _mean(closes[-20:])
        std_dev = (sum((c - sma) ** 2 for c in closes[-20:]) / 20) ** 0.5
        zscore = (sma - entry_price) / std_dev if std_dev > 0 else 0.0
        if zscore >= MEAN_REV_ZSCORE_THRESH:
            candidates.append(("mean_reversion", state.signal_confidence * 0.90))

    # Pullback
    if regime == "BULL" and len(closes) >= 20:
        sma = _mean(closes[-20:])
        recent_high = max(closes[-10:])
        retrace = (recent_high - entry_price) / (recent_high - sma) if (recent_high - sma) > 0 else 0.0
        if PULLBACK_RETRACE_PCT * 0.8 <= retrace <= PULLBACK_RETRACE_PCT * 1.2:
            candidates.append(("pullback", state.signal_confidence * 0.85))

    if not candidates:
        state.entry_result = "NO_ENTRY"
        state.entry_type = "none"
        entry_result = "NO_ENTRY"
        reason = "no_pattern_match"
    else:
        best = max(candidates, key=lambda x: x[1])
        state.entry_type = best[0]
        state.entry_result = "ENTRY_APPROVED"
        entry_result = "ENTRY_APPROVED"
        reason = best[0]

    inputs = {
        "regime": regime,
        "entry_price": entry_price,
        "bars_count": len(closes),
        "candidates": [(c[0], round(c[1], 4)) for c in candidates],
    }
    state.record(6, "Entry Decision", inputs, entry_result, reason, ts)


def gate_7_position_sizing(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    if state.entry_result == "NO_ENTRY":
        state.position_size = 0.0
        state.size_calc_log = {}
        state.record(7, "Position Sizing", {}, "SIZE_ZERO", "no_entry", ts)
        return

    equity = context.get("equity", 100000.0)
    entry_price = context.get("entry_price", 100.0)
    atr = context.get("atr", entry_price * 0.02)  # default 2% if not provided

    risk_amount = equity * RISK_PER_TRADE_PCT
    stop_distance = ATR_STOP_MULTIPLIER * atr
    base_size = risk_amount / stop_distance if stop_distance > 0 else 0.0

    # Volatility adjustment
    vol_target_atr = entry_price * 0.015  # target ATR ~1.5% of price
    vol_adj = vol_target_atr / atr if atr > 0 else 1.0
    vol_adj = max(0.5, min(2.0, vol_adj))

    # Mode adjustment
    if state.benchmark_mode == "DEFENSIVE":
        mode_adj = DEFENSIVE_SIZE_FACTOR
    elif state.benchmark_mode == "AGGRESSIVE":
        mode_adj = AGGRESSIVE_SIZE_FACTOR
    else:
        mode_adj = 1.0

    # Drawdown scaling
    dd = state.benchmark_drawdown_pct
    drawdown_adj = max(0.5, 1.0 - (dd / MAX_DRAWDOWN_PCT))

    adjusted_size = base_size * vol_adj * mode_adj * drawdown_adj
    max_size = (equity * MAX_POSITION_PCT) / entry_price if entry_price > 0 else 0.0
    final_size = round(min(adjusted_size, max_size), 2)

    size_calc_log = {
        "base_size": round(base_size, 2),
        "vol_adj": round(vol_adj, 4),
        "mode_adj": mode_adj,
        "drawdown_adj": round(drawdown_adj, 4),
        "adjusted_size": round(adjusted_size, 2),
        "max_size_cap": round(max_size, 2),
        "final_size": final_size,
    }

    state.position_size = final_size
    state.size_calc_log = size_calc_log

    inputs = {
        "equity": equity,
        "entry_price": entry_price,
        "atr": round(atr, 4),
        "risk_amount": round(risk_amount, 2),
        "stop_distance": round(stop_distance, 4),
        "benchmark_mode": state.benchmark_mode,
        "benchmark_drawdown_pct": state.benchmark_drawdown_pct,
    }
    state.record(7, "Position Sizing", inputs, f"SIZE_{final_size}", "sized", ts)


def gate_8_risk_setup(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    if state.position_size == 0.0:
        state.stop_level = 0.0
        state.target_level = 0.0
        state.trailing_stop = 0.0
        state.record(8, "Risk Setup", {}, "RISK_SKIPPED", "no_position", ts)
        return

    entry_price = context.get("entry_price", 100.0)
    atr = context.get("atr", entry_price * 0.02)
    stop_distance = ATR_STOP_MULTIPLIER * atr
    stop_level = round(entry_price - stop_distance, 4)
    target_level = round(entry_price + REWARD_RISK_RATIO * stop_distance, 4)
    trailing_stop = stop_level  # initialized to stop; updated each session

    # Overnight risk
    if context.get("session_type") == "close" and not context.get("skip_overnight_check", False):
        if context.get("hold_overnight", True):
            state.position_size = round(state.position_size * OVERNIGHT_SIZE_FACTOR, 2)

    # Gap risk — TODO:DATA_DEPENDENCY
    gap_std = context.get("gap_risk_std", 0.0)  # TODO:DATA_DEPENDENCY
    if gap_std > GAP_RISK_STD_THRESH:
        stop_level = round(entry_price - (stop_distance * 1.5), 4)

    state.stop_level = stop_level
    state.target_level = target_level
    state.trailing_stop = trailing_stop

    inputs = {
        "entry_price": entry_price,
        "atr": round(atr, 4),
        "stop_distance": round(stop_distance, 4),
        "gap_std": gap_std,
        "session_type": context.get("session_type", ""),
    }
    state.record(8, "Risk Setup", inputs, f"STOP_{stop_level}_TARGET_{target_level}", "risk_set", ts)


def gate_9_execution(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    if state.position_size == 0.0:
        state.order_status = "NOT_PLACED"
        state.fill_price = 0.0
        state.fill_qty = 0.0
        state.order_id = ""
        state.record(9, "Execution", {}, "NOT_PLACED", "no_position", ts)
        return

    if TRADING_MODE == "PAPER":
        fill_price = context.get("entry_price", 100.0)
        fill_qty = state.position_size
        order_id = f"PAPER_{context.get('ticker', 'UNK')}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        order_status = "PAPER_FILLED"
    elif TRADING_MODE == "LIVE":
        # TODO:DATA_DEPENDENCY — live broker order placement
        order_status = "LIVE_PENDING"
        fill_price = 0.0
        fill_qty = 0.0
        order_id = "TODO_LIVE"
        log.warning("LIVE trading not yet wired — TODO:DATA_DEPENDENCY")
    else:
        order_status = "UNKNOWN_MODE"
        fill_price = 0.0
        fill_qty = 0.0
        order_id = ""

    # Slippage check (paper: always within tolerance)
    expected_price = context.get("entry_price", fill_price)
    slippage = abs(fill_price - expected_price) / expected_price if expected_price > 0 else 0.0
    if slippage > MAX_SLIPPAGE_PCT:
        order_status = "SLIPPAGE_EXCEEDED"
        fill_qty = 0.0

    state.order_status = order_status
    state.fill_price = fill_price
    state.fill_qty = fill_qty
    state.order_id = order_id

    inputs = {
        "trading_mode": TRADING_MODE,
        "expected_price": expected_price,
        "fill_price": fill_price,
        "slippage": round(slippage, 6),
        "position_size": state.position_size,
    }
    state.record(9, "Execution", inputs, order_status, order_id or "no_order", ts)


def gate_10_active_management(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    open_positions = context.get("open_positions", [])
    current_prices = context.get("current_prices", {})

    if not open_positions:
        state.active_mgmt_result = "NO_POSITIONS"
        state.exit_reason = "none"
        state.record(10, "Active Management", {}, "NO_POSITIONS", "no_open_positions", ts)
        return

    exit_decisions = []

    for position in open_positions:
        ticker = position.get("ticker")
        current_price = current_prices.get(ticker, position.get("entry_price", 0))
        stop = position.get("stop_level", 0)
        target = position.get("target_level", float("inf"))
        trailing = position.get("trailing_stop", 0)
        entry_date = position.get("entry_date")
        signal_confidence = position.get("current_confidence", 1.0)

        exit_reason = None

        if current_price <= stop:
            exit_reason = "stop_loss_hit"
        elif current_price >= target:
            exit_reason = "profit_target_hit"
        elif current_price <= trailing:
            exit_reason = "trailing_stop_hit"
        elif signal_confidence < EXIT_CONF_FLOOR:
            exit_reason = "signal_confidence_floor"
        elif entry_date:
            try:
                days_held = (datetime.datetime.utcnow().date() - datetime.date.fromisoformat(str(entry_date))).days
                if days_held > MAX_HOLDING_DAYS:
                    exit_reason = "max_holding_time"
            except Exception as e:
                log.error(f"Gate 10 entry_date parse error for {ticker}: {e}")

        # Benchmark underperformance: TODO:DATA_DEPENDENCY — need benchmark return

        # Protective exit from Agent 3
        scan_log = context.get("scan_log_entry", {})
        if scan_log.get("ticker") == ticker and scan_log.get("tier") == 1:
            exit_reason = "cascade_protective_exit"
            log.warning(f"Protective exit triggered for {ticker} — CRITICAL cascade signal from Agent 3")

        if exit_reason:
            exit_decisions.append({"ticker": ticker, "action": "EXIT", "reason": exit_reason})
        else:
            exit_decisions.append({"ticker": ticker, "action": "HOLD", "reason": "all_conditions_met"})

    state.active_mgmt_result = "PROCESSED"
    state.exit_reason = "; ".join(set(d["reason"] for d in exit_decisions if d["action"] == "EXIT")) or "none"

    for d in exit_decisions:
        if d["action"] == "EXIT":
            try:
                _db.log_event("POSITION_EXIT", d)
            except Exception as e:
                log.error(f"Gate 10 _db.log_event failed for {d.get('ticker')}: {e}")

    exits = [d for d in exit_decisions if d["action"] == "EXIT"]
    holds = [d for d in exit_decisions if d["action"] == "HOLD"]

    inputs = {
        "positions_count": len(open_positions),
        "exits": len(exits),
        "holds": len(holds),
    }
    state.record(10, "Active Management", inputs, f"EXITS_{len(exits)}_HOLDS_{len(holds)}", state.exit_reason, ts)


def gate_11_portfolio_controls(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    equity = context.get("equity", 100000.0)
    positions = context.get("portfolio_positions", [])

    gross_exposure = sum(p.get("market_value", 0) for p in positions)
    new_val = context.get(
        "new_position_value",
        state.position_size * context.get("entry_price", 100.0)
    )

    # Gross exposure check
    if equity > 0 and (gross_exposure + new_val) / equity > MAX_GROSS_EXPOSURE_PCT:
        state.portfolio_entry_allowed = False
        state.portfolio_block_reason = "gross_exposure_cap"
        inputs = {
            "gross_exposure": gross_exposure,
            "new_val": new_val,
            "equity": equity,
            "ratio": round((gross_exposure + new_val) / equity, 4),
        }
        state.record(11, "Portfolio Controls", inputs, "BLOCKED", "gross_exposure_cap", ts)
        state.final_decision = "SKIP"
        return

    # Sector exposure check
    sector = context.get("signal", {}).get("sector", "unknown")
    sector_exp = context.get("sector_exposures", {}).get(sector, 0.0)
    if equity > 0 and (sector_exp + new_val) / equity > MAX_SECTOR_EXPOSURE_PCT:
        state.portfolio_entry_allowed = False
        state.portfolio_block_reason = "sector_exposure_limit"
        inputs = {
            "sector": sector,
            "sector_exp": sector_exp,
            "new_val": new_val,
            "equity": equity,
            "ratio": round((sector_exp + new_val) / equity, 4),
        }
        state.record(11, "Portfolio Controls", inputs, "BLOCKED", "sector_exposure_limit", ts)
        state.final_decision = "SKIP"
        return

    # Correlation check — TODO:DATA_DEPENDENCY
    mean_corr = context.get("mean_portfolio_correlation", 0.0)  # TODO:DATA_DEPENDENCY
    if mean_corr > MAX_PORT_CORRELATION:
        state.portfolio_entry_allowed = False
        state.portfolio_block_reason = "correlation_spike"
        inputs = {"mean_corr": mean_corr}
        state.record(11, "Portfolio Controls", inputs, "BLOCKED", "correlation_spike", ts)
        state.final_decision = "SKIP"
        return

    # Leverage check
    leverage = (gross_exposure + new_val) / equity if equity > 0 else 0.0
    if leverage > MAX_LEVERAGE:
        state.portfolio_entry_allowed = False
        state.portfolio_block_reason = "leverage_limit"
        inputs = {"leverage": round(leverage, 4)}
        state.record(11, "Portfolio Controls", inputs, "BLOCKED", "leverage_limit", ts)
        state.final_decision = "SKIP"
        return

    state.portfolio_entry_allowed = True
    state.portfolio_block_reason = ""

    inputs = {
        "gross_exposure": gross_exposure,
        "new_val": new_val,
        "equity": equity,
        "leverage": round(leverage, 4),
        "sector": sector,
        "mean_corr": mean_corr,
    }
    state.record(11, "Portfolio Controls", inputs, "ALLOWED", "all_checks_passed", ts)


def gate_12_adaptive_layer(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    rolling_sharpe = context.get("rolling_sharpe", 1.0)  # TODO:DATA_DEPENDENCY — computed from trade history
    regime_changed = context.get("regime_changed", False)
    current_params = context.get("current_params", {})
    prior_params = context.get("prior_params", {})

    adaptive_flag = "none"
    adaptive_result = "no_change"

    if rolling_sharpe < MIN_SHARPE_ROLLING:
        adaptive_flag = "performance_below_threshold"
        adaptive_result = "risk_reduced"
        # In V1: log the flag; do not silently modify global constants
        log.warning(f"Rolling Sharpe {rolling_sharpe:.2f} below minimum {MIN_SHARPE_ROLLING} — flag for human review")

    if regime_changed:
        adaptive_flag = "regime_change" if adaptive_flag == "none" else adaptive_flag + "+regime_change"
        adaptive_result = "parameter_set_switched"

    # Parameter drift check
    if current_params and prior_params:
        drift_keys = [
            k for k in current_params
            if k in prior_params and
            abs(current_params[k] - prior_params.get(k, current_params[k])) /
            (abs(prior_params.get(k, 1)) + 1e-9) > PARAM_DRIFT_THRESH
        ]
        if drift_keys:
            adaptive_flag = "parameter_drift"
            adaptive_result = "flag_for_human_review"
            try:
                _db.post_suggestion("agent1_param_drift", {"drifted_params": drift_keys})
            except Exception as e:
                log.error(f"Gate 12 _db.post_suggestion failed: {e}")

    state.adaptive_result = adaptive_result
    state.adaptive_flag = adaptive_flag

    inputs = {
        "rolling_sharpe": rolling_sharpe,
        "regime_changed": regime_changed,
        "current_params_keys": list(current_params.keys()),
        "prior_params_keys": list(prior_params.keys()),
    }
    state.record(12, "Adaptive Layer", inputs, adaptive_result, adaptive_flag, ts)


def gate_13_stress_overrides(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    stress_state = "normal"
    stress_response = "none"

    intraday_drop = context.get("intraday_drop_pct", 0.0)
    drop_minutes = context.get("intraday_drop_min", 0)
    if intraday_drop >= FLASH_CRASH_PCT and drop_minutes <= FLASH_CRASH_MIN:
        stress_state = "flash_crash"
        stress_response = "force_exit_all"
        log.critical(f"Flash crash detected: {intraday_drop:.1%} in {drop_minutes}min")

    current_spread = context.get("current_spread_pct", 0.0)
    normal_spread = context.get("normal_spread_pct", 0.001)
    if stress_state == "normal" and normal_spread > 0 and current_spread / normal_spread >= SPREAD_EXPLOSION_MULT:
        stress_state = "liquidity_collapse"
        stress_response = "halt_new_entries"

    bench_drop = context.get("benchmark_intraday_drop", 0.0)
    if stress_state == "normal" and bench_drop >= BENCHMARK_CRASH_PCT:
        stress_state = "benchmark_crash"
        stress_response = "force_defensive_mode"
        # Override benchmark_mode
        state.benchmark_mode = "DEFENSIVE"

    # Forced de-risk: if stress_state is not normal with force_exit response
    if stress_response in ["force_exit_all"]:
        state.position_size = 0.0
        state.order_status = "STRESS_OVERRIDE_HALT"

    state.stress_state = stress_state
    state.stress_response = stress_response

    try:
        if stress_state != "normal":
            _db.post_suggestion("agent1_stress", {"state": stress_state, "response": stress_response})
    except Exception:
        pass

    inputs = {
        "intraday_drop_pct": intraday_drop,
        "intraday_drop_min": drop_minutes,
        "current_spread_pct": current_spread,
        "normal_spread_pct": normal_spread,
        "benchmark_intraday_drop": bench_drop,
    }
    state.record(13, "Stress Overrides", inputs, stress_state, stress_response, ts)


def gate_14_evaluation_loop(context: dict, state: TradeDecisionLog):
    ts = datetime.datetime.utcnow().isoformat()

    trade_history = context.get("trade_history", [])  # TODO:DATA_DEPENDENCY — from DB
    current_drawdown = context.get("current_drawdown", 0.0)

    metrics = {
        "sharpe_rolling": 0.0,
        "sortino_rolling": 0.0,
        "max_drawdown": current_drawdown,
        "win_rate": 0.0,
        "expectancy": 0.0,
        "benchmark_alpha": 0.0,
    }

    if trade_history and len(trade_history) >= 5:
        returns = [t.get("return_pct", 0.0) for t in trade_history[-SHARPE_ROLLING_WINDOW:]]
        mean_ret = _mean(returns)
        std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        metrics["sharpe_rolling"] = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0.0

        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        metrics["win_rate"] = len(wins) / len(returns) if returns else 0.0
        avg_win = _mean(wins)
        avg_loss = _mean(losses)
        metrics["expectancy"] = avg_win * metrics["win_rate"] + avg_loss * (1 - metrics["win_rate"])

    # Kill condition
    kill_condition = (metrics["sharpe_rolling"] < SHARPE_KILL_MIN and current_drawdown >= DRAWDOWN_KILL_MAX)
    if kill_condition:
        log.critical("KILL CONDITION: Sharpe below minimum AND drawdown exceeded — suspending new entries")
        try:
            _db.post_suggestion("agent1_kill", {"metrics": metrics, "drawdown": current_drawdown})
        except Exception:
            pass

    state.metrics_updated = True
    state.kill_condition = kill_condition

    try:
        _db.log_event("AGENT1_METRICS", {"agent_version": AGENT_VERSION, **metrics})
    except Exception as e:
        log.error(f"Gate 14 _db.log_event failed: {e}")

    inputs = {
        "trade_history_count": len(trade_history),
        "current_drawdown": current_drawdown,
        "sharpe_rolling": round(metrics["sharpe_rolling"], 4),
        "win_rate": round(metrics["win_rate"], 4),
        "expectancy": round(metrics["expectancy"], 6),
    }
    state.record(14, "Evaluation Loop", inputs, f"KILL_{kill_condition}", "metrics_updated", ts)


# ============================================================
# PIPELINE ORCHESTRATORS
# ============================================================

def run_signal_evaluation(signal_context: dict) -> dict:
    ts = datetime.datetime.utcnow().isoformat()
    session_type = signal_context.get("session_type", "open")
    ticker = signal_context.get("signal", {}).get("ticker", "")
    signal_id = signal_context.get("signal", {}).get("signal_id", "")

    state = TradeDecisionLog(session_ts=ts, session_type=session_type, ticker=ticker, signal_id=signal_id)

    gate_1_system_check(signal_context, state)
    if state.halted:
        state.final_decision = "HALT"
        return state.to_dict()

    gate_2_benchmark(signal_context, state)
    gate_3_regime(signal_context, state)
    gate_4_eligibility(signal_context, state)
    gate_5_signal_evaluation(signal_context, state)
    gate_6_entry_decision(signal_context, state)
    gate_7_position_sizing(signal_context, state)
    gate_8_risk_setup(signal_context, state)
    gate_13_stress_overrides(signal_context, state)
    gate_9_execution(signal_context, state)
    gate_11_portfolio_controls(signal_context, state)
    gate_12_adaptive_layer(signal_context, state)
    gate_14_evaluation_loop(signal_context, state)

    # Determine final decision
    if state.kill_condition:
        state.final_decision = "SUSPENDED"
    elif not state.portfolio_entry_allowed:
        state.final_decision = "SKIP"
    elif state.order_status in ["PAPER_FILLED", "LIVE_PENDING"]:
        state.final_decision = "MIRROR"
    elif state.entry_result == "NO_ENTRY" or state.signal_result == "SKIP":
        state.final_decision = "WATCH"
    else:
        state.final_decision = "SKIP"

    return state.to_dict()


def run_position_management(mgmt_context: dict) -> dict:
    ts = datetime.datetime.utcnow().isoformat()
    session_type = mgmt_context.get("session_type", "midday")
    state = TradeDecisionLog(session_ts=ts, session_type=session_type, ticker="PORTFOLIO")

    gate_1_system_check(mgmt_context, state)
    if state.halted:
        state.final_decision = "HALT"
        return state.to_dict()

    gate_2_benchmark(mgmt_context, state)
    gate_3_regime(mgmt_context, state)
    gate_10_active_management(mgmt_context, state)
    gate_11_portfolio_controls(mgmt_context, state)
    gate_12_adaptive_layer(mgmt_context, state)
    gate_13_stress_overrides(mgmt_context, state)
    gate_14_evaluation_loop(mgmt_context, state)

    if state.kill_condition:
        state.final_decision = "SUSPENDED"
    elif state.active_mgmt_result == "NO_POSITIONS":
        state.final_decision = "HOLD"
    else:
        state.final_decision = "HOLD"

    return state.to_dict()


# ============================================================
# MAIN / CLI
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent 1 — ExecutionAgent")
    parser.add_argument("--test", action="store_true", help="Run smoke test")
    args = parser.parse_args()

    if args.test:
        # Smoke test: minimal signal evaluation context
        import random
        closes = [500.0 + i * 0.3 + random.uniform(-1, 1) for i in range(60)]
        test_context = {
            "session_type": "open",
            "session_time": datetime.datetime.now(),
            "daily_pnl": 0.0,
            "equity": 100000.0,
            "peak_equity": 102000.0,
            "data_age_min": 1,
            "missing_data_ratio": 0.05,
            "api_failure_count": 0,
            "benchmark_bars": [{"close": c} for c in closes],
            "tlt_bars": [{"close": 95.0 + i * 0.1} for i in range(20)],
            "signal": {
                "ticker": "NVDA",
                "signal_id": "TEST001",
                "source_tier": 0.8,
                "politician_weight": 0.7,
                "staleness_days": 1,
                "interrogation_status": "validated",
                "sentiment_corroboration_tier": 4,
                "benchmark_rel_strength": 0.75,
                "sector": "technology",
            },
            "price_bars": [{"close": c} for c in closes],
            "entry_price": closes[-1],
            "avg_daily_volume": 2000000,
            "bid": closes[-1] - 0.10,
            "ask": closes[-1] + 0.10,
            "event_exclusion": False,
            "atr": closes[-1] * 0.018,
            "open_positions": [],
            "current_prices": {},
            "portfolio_positions": [],
            "sector_exposures": {"technology": 5000.0},
            "trade_history": [],
            "current_drawdown": 0.02,
            "intraday_drop_pct": 0.0,
            "intraday_drop_min": 0,
            "current_spread_pct": 0.001,
            "normal_spread_pct": 0.001,
            "benchmark_intraday_drop": 0.0,
            "rolling_sharpe": 1.2,
            "regime_changed": False,
        }
        result = run_signal_evaluation(test_context)
        print(json.dumps(result, indent=2, default=str))
        print(f"\nSmoke test results:")
        print(f"  halted: {result.get('halted')}")
        print(f"  final_decision: {result.get('final_decision')}")
        print(f"  benchmark_mode: {result.get('benchmark_mode')}")
        print(f"  signal_confidence: {result.get('signal_confidence')}")
        print(f"  entry_type: {result.get('entry_type')}")
        print(f"  position_size: {result.get('position_size')}")
        print(f"  order_status: {result.get('order_status')}")
        print(f"  stress_state: {result.get('stress_state')}")
        print(f"  kill_condition: {result.get('kill_condition')}")
