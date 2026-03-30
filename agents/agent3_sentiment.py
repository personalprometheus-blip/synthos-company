#!/usr/bin/env python3
"""
agent3_sentiment.py — Agent 3 (SentimentAgent)
27-gate deterministic market sentiment spine + Phase 2 cascade scan + Phase 3 pre-trade check.
All decisions are rule-based and fully traceable.
AGENT_VERSION = "1.0.0"
"""
import sys, os, json, logging, hashlib, datetime
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent3_sentiment")

_paths = get_paths()
_db    = get_db_helpers()

AGENT_VERSION = "1.0.0"

# ============================================================
# CONFIG BLOCK
# ============================================================

# System / Gate 1-2
MAX_SNAPSHOT_AGE_MIN = 60
MIN_REQUIRED_INPUTS  = 3

# Gate 3 — Benchmark
TREND_NEUTRAL_BAND      = 0.002
DRAWDOWN_THRESHOLD      = 0.05
SPX_VOL_THRESHOLD       = 0.015
SPY_SMA_SHORT           = 20
SPY_SMA_LONG            = 50
SPY_ROC_BARS            = 5

# Gate 7 — Volatility
VIX_ELEVATED_THRESHOLD  = 20.0
VIX_ROC_SPIKE_THRESHOLD = 0.10

# Gate 17 — Component weights (must sum to 1.0)
WEIGHT_PRICE       = 0.20
WEIGHT_BREADTH     = 0.15
WEIGHT_VOLATILITY  = 0.15
WEIGHT_VOLUME      = 0.10
WEIGHT_OPTIONS     = 0.10
WEIGHT_CROSS_ASSET = 0.10
WEIGHT_CREDIT      = 0.10
WEIGHT_MACRO       = 0.05
WEIGHT_NEWS        = 0.03
WEIGHT_SOCIAL      = 0.02

# Gate 18 — Composite thresholds
COMPOSITE_BULLISH_THRESHOLD    = 0.20
COMPOSITE_BEARISH_THRESHOLD    = -0.20
COMPOSITE_EUPHORIA_THRESHOLD   = 0.60
COMPOSITE_PANIC_THRESHOLD      = -0.60

# Gate 19 — Confidence
MIN_CONFIDENT_COMPONENTS = 4
AGREEMENT_THRESHOLD      = 0.20
CONFIDENCE_HIGH          = 0.70
CONFIDENCE_LOW           = 0.40

# Gate 22 — Action
WARNING_COUNT_THRESHOLD  = 2

# Gate 23 — Discounts
DISCOUNT_LOW_TRUST_SOCIAL  = 0.90
DISCOUNT_FEW_INPUTS        = 0.80
DISCOUNT_DIVERGENCE        = 0.85
DISCOUNT_DRAWDOWN          = 0.80
DISCOUNT_VOL_INSTABILITY   = 0.90

# Gate 24 — Persistence
PERSISTENCE_THRESHOLD = 3
FLIP_THRESHOLD        = 3

# Gate 25 — Evaluation loop
IMPROVEMENT_THRESHOLD    = 0.05
DETERIORATION_THRESHOLD  = 0.05

# Gate 27 — Final thresholds
FINAL_STRONG_BULLISH  = 0.50
FINAL_MILD_BULLISH    = 0.20
FINAL_STRONG_BEARISH  = -0.50
FINAL_MILD_BEARISH    = -0.20

# Phase 2/3 — Cascade detection
PC_CRITICAL_MULT       = 1.10   # put/call > 110% of 30d avg = critical
PC_ELEVATED_MULT       = 1.20   # put/call > 120% = elevated
INSIDER_SELL_CRITICAL  = 4      # >= 4 sells, 0 buys
INSIDER_SELL_EDGE      = 2      # sells exceed buys by > 2
VOL_CASCADE_PCT        = 2.50   # > 250% avg volume
VOL_SELLER_DOM         = 0.70   # seller dominance > 70%
VOL_ELEVATED_PCT       = 0.80   # volume > 80% above avg
INSIDER_LOOKBACK_DAYS_PHASE2 = 30
INSIDER_LOOKBACK_DAYS_PHASE3 = 7
NEWS_LOOKBACK_HOURS    = 4


# ============================================================
# GATE/HELPER FUNCTIONS — Data structures
# ============================================================

@dataclass
class SentimentDecisionLog:
    snapshot_hash: str
    session_ts: str
    halted: bool = False
    halt_reason: str = ""
    system_status: str = ""
    active_input_count: int = 0
    input_channels: dict = field(default_factory=dict)

    # Gate 3 — Benchmark
    benchmark_state: str = ""
    benchmark_momentum_state: str = ""
    benchmark_risk_state: str = ""
    benchmark_vol_state: str = ""

    # Gate 4 — Price action
    price_score: float = 0.0
    price_sentiment_state: str = ""
    price_structure_state: str = ""
    price_momentum_state: str = ""
    price_dispersion_state: str = ""

    # Gate 5 — Breadth
    breadth_score: float = 0.0
    breadth_state: str = ""
    breadth_momentum_state: str = ""
    breadth_quality_state: str = ""

    # Gate 6 — Volume
    volume_score: float = 0.0
    volume_sentiment_state: str = ""
    volume_pattern_state: str = ""
    volume_confirmation_state: str = ""

    # Gate 7 — Volatility
    vol_score: float = 0.0
    vol_sentiment_state: str = ""
    vol_structure_state: str = ""
    realized_vol_state: str = ""
    vol_instability_state: str = ""

    # Gate 8 — Options
    options_score: float = 0.0
    options_sentiment_state: str = ""
    options_tail_risk_state: str = ""
    options_speculation_state: str = ""
    options_flow_state: str = ""
    options_corr_state: str = ""

    # Gate 9 — Safe haven / Cross-asset
    cross_asset_score: float = 0.0
    cross_asset_state: str = ""
    rotation_state: str = ""
    risk_appetite_state: str = ""

    # Gate 10 — Credit
    credit_score: float = 0.0
    credit_state: str = ""
    credit_risk_state: str = ""
    credit_stress_state: str = ""
    liquidity_state: str = ""

    # Gate 11 — Sector rotation
    sector_score: float = 0.0
    sector_leadership_state: str = ""
    sector_rotation_state: str = ""
    sector_confirmation_state: str = ""

    # Gate 12 — Macro
    macro_score: float = 0.0
    macro_state: str = ""
    macro_policy_state: str = ""
    macro_sentiment_state: str = ""

    # Gate 13 — News sentiment
    news_score: float = 0.0
    news_sentiment_state: str = ""
    news_driver_state: str = ""
    news_conviction_state: str = ""

    # Gate 14 — Social
    social_score: float = 0.0
    social_sentiment_state: str = ""
    social_attention_state: str = ""
    social_quality_state: str = ""

    # Gate 15 — Divergence
    divergence_state: str = ""

    # Gate 16 — Component scores
    component_scores: dict = field(default_factory=dict)

    # Gate 17 — Effective weights
    effective_weights: dict = field(default_factory=dict)

    # Gate 18 — Composite score
    raw_sentiment_score: float = 0.0
    market_sentiment_state: str = ""

    # Gate 19 — Confidence
    sentiment_confidence: float = 0.0
    confidence_state: str = ""

    # Gate 20 — Regime
    regime_state: str = ""

    # Gate 21 — Divergence warnings
    active_warnings: list = field(default_factory=list)
    active_warning_count: int = 0
    warning_state: str = "none"

    # Gate 22 — Action classification
    classification: str = ""

    # Gate 23 — Risk discounts
    discounts_applied: list = field(default_factory=list)
    discounted_sentiment_score: float = 0.0
    discounted_confidence: float = 0.0

    # Gate 24 — Temporal persistence
    persistence_state: str = ""
    sentiment_trend_state: str = ""

    # Gate 25 — Evaluation loop
    snapshot_retained: bool = True

    # Gate 26 — Output controls
    output_action: str = ""
    output_priority: str = "article_first"

    # Gate 27 — Final signal
    final_market_state: str = ""

    # Decision log
    decision_log: list = field(default_factory=list)

    def record(self, gate_num, gate_name, inputs, result, reason_code, ts=None):
        if ts is None:
            ts = datetime.datetime.utcnow().isoformat()
        self.decision_log.append({
            "gate": gate_num,
            "name": gate_name,
            "inputs": inputs,
            "result": result,
            "reason_code": reason_code,
            "ts": ts,
        })

    def halt(self, reason):
        self.halted = True
        self.halt_reason = reason

    def to_dict(self):
        d = {}
        for f_name in self.__dataclass_fields__:
            d[f_name] = getattr(self, f_name)
        return d

    def to_human_readable(self):
        lines = [
            f"SentimentDecisionLog [{self.snapshot_hash}] @ {self.session_ts}",
            f"  halted: {self.halted}  halt_reason: {self.halt_reason}",
            f"  system_status: {self.system_status}",
            f"  active_input_count: {self.active_input_count}",
            f"  input_channels: {self.input_channels}",
            "",
            "  --- Phase 1 Gate Outputs ---",
            f"  Gate 3:  benchmark={self.benchmark_state}, momentum={self.benchmark_momentum_state}, risk={self.benchmark_risk_state}, vol={self.benchmark_vol_state}",
            f"  Gate 4:  price_score={self.price_score:.4f}, sentiment={self.price_sentiment_state}, structure={self.price_structure_state}, momentum={self.price_momentum_state}, dispersion={self.price_dispersion_state}",
            f"  Gate 5:  breadth_score={self.breadth_score:.4f}, breadth={self.breadth_state}, momentum={self.breadth_momentum_state}, quality={self.breadth_quality_state}",
            f"  Gate 6:  volume_score={self.volume_score:.4f}, sentiment={self.volume_sentiment_state}, pattern={self.volume_pattern_state}, confirm={self.volume_confirmation_state}",
            f"  Gate 7:  vol_score={self.vol_score:.4f}, sentiment={self.vol_sentiment_state}, structure={self.vol_structure_state}, realized={self.realized_vol_state}, instability={self.vol_instability_state}",
            f"  Gate 8:  options_score={self.options_score:.4f}, sentiment={self.options_sentiment_state}, tail_risk={self.options_tail_risk_state}, speculation={self.options_speculation_state}",
            f"  Gate 9:  cross_asset_score={self.cross_asset_score:.4f}, state={self.cross_asset_state}, rotation={self.rotation_state}, appetite={self.risk_appetite_state}",
            f"  Gate 10: credit_score={self.credit_score:.4f}, state={self.credit_state}, risk={self.credit_risk_state}, stress={self.credit_stress_state}, liquidity={self.liquidity_state}",
            f"  Gate 11: sector_score={self.sector_score:.4f}, leadership={self.sector_leadership_state}, rotation={self.sector_rotation_state}, confirm={self.sector_confirmation_state}",
            f"  Gate 12: macro_score={self.macro_score:.4f}, state={self.macro_state}, policy={self.macro_policy_state}, sentiment={self.macro_sentiment_state}",
            f"  Gate 13: news_score={self.news_score:.4f}, sentiment={self.news_sentiment_state}, driver={self.news_driver_state}, conviction={self.news_conviction_state}",
            f"  Gate 14: social_score={self.social_score:.4f}, sentiment={self.social_sentiment_state}, attention={self.social_attention_state}, quality={self.social_quality_state}",
            f"  Gate 15: divergence={self.divergence_state}",
            f"  Gate 16: component_scores={self.component_scores}",
            f"  Gate 17: effective_weights={self.effective_weights}",
            f"  Gate 18: raw_sentiment_score={self.raw_sentiment_score:.4f}, market_sentiment_state={self.market_sentiment_state}",
            f"  Gate 19: sentiment_confidence={self.sentiment_confidence:.4f}, confidence_state={self.confidence_state}",
            f"  Gate 20: regime_state={self.regime_state}",
            f"  Gate 21: active_warnings={self.active_warnings}, count={self.active_warning_count}, warning_state={self.warning_state}",
            f"  Gate 22: classification={self.classification}",
            f"  Gate 23: discounts_applied={self.discounts_applied}, discounted_score={self.discounted_sentiment_score:.4f}, discounted_conf={self.discounted_confidence:.4f}",
            f"  Gate 24: persistence={self.persistence_state}, trend={self.sentiment_trend_state}",
            f"  Gate 25: snapshot_retained={self.snapshot_retained}",
            f"  Gate 26: output_action={self.output_action}, output_priority={self.output_priority}",
            f"  Gate 27: final_market_state={self.final_market_state}",
            "",
            f"  Decision log entries: {len(self.decision_log)}",
        ]
        for entry in self.decision_log:
            lines.append(f"    [{entry['gate']:>2}] {entry['name']:40s} result={entry['result']}  reason={entry['reason_code']}")
        return "\n".join(lines)


# ============================================================
# GATE/HELPER FUNCTIONS — Phase 1 Gates
# ============================================================

def _mean(values):
    """Compute mean of a list of numbers."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values):
    """Compute population standard deviation of a list of numbers."""
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    variance = sum((x - m) ** 2 for x in values) / len(values)
    return variance ** 0.5


def _clamp(value, lo=-1.0, hi=1.0):
    """Clamp a float to [lo, hi]."""
    return max(lo, min(hi, value))


def gate_1_system_check(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    # Check market data feed available
    if snapshot is None or snapshot == {}:
        state.halt("no_market_data")
        state.record(1, "system_check", {"snapshot_present": False}, "halted", "no_market_data", ts)
        return state

    # Compute snapshot hash
    snap_hash = hashlib.md5(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]

    # Check for duplicate snapshot in recent system log
    # TODO:DATA_DEPENDENCY — _db.get_recent_events not yet wired; duplicate check skipped
    try:
        recent_events = _db.get_recent_events("MARKET_SENTIMENT_CLASSIFIED", limit=10)
        if recent_events:
            for evt in recent_events:
                prior_hash = evt.get("snapshot_hash", "") if isinstance(evt, dict) else ""
                if prior_hash and prior_hash == snap_hash:
                    state.halt("duplicate_skip")
                    state.record(1, "system_check", {"snapshot_hash": snap_hash}, "halted", "duplicate_skip", ts)
                    return state
    except Exception as e:
        log.warning(f"gate_1: DB duplicate check failed (non-halting): {e}")

    # Check snapshot age
    snap_ts_raw = snapshot.get("ts")
    if snap_ts_raw is None:
        state.halt("stale_skip")
        state.record(1, "system_check", {"snapshot_ts": None}, "halted", "stale_skip", ts)
        return state

    try:
        snap_dt = datetime.datetime.fromisoformat(str(snap_ts_raw).replace("Z", "+00:00"))
        now_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) if snap_dt.tzinfo else datetime.datetime.utcnow()
        age_min = (now_dt - snap_dt).total_seconds() / 60.0
        if age_min > MAX_SNAPSHOT_AGE_MIN:
            state.halt("stale_skip")
            state.record(1, "system_check", {"snapshot_ts": snap_ts_raw, "age_min": round(age_min, 2)}, "halted", "stale_skip", ts)
            return state
    except Exception as e:
        log.warning(f"gate_1: snapshot age parse failed: {e}")
        # If we cannot parse the ts, treat as stale
        state.halt("stale_skip")
        state.record(1, "system_check", {"snapshot_ts": snap_ts_raw, "parse_error": str(e)}, "halted", "stale_skip", ts)
        return state

    state.system_status = "operational"
    state.record(1, "system_check", {"snapshot_hash": snap_hash, "age_min": round(age_min, 2)}, "operational", "ok", ts)
    return state


def gate_2_input_universe(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    channels = {
        "price":       "spy_bars" in snapshot and snapshot["spy_bars"] is not None,
        "volume":      "spy_bars" in snapshot and snapshot["spy_bars"] is not None,
        "breadth":     "sector_bars" in snapshot and snapshot["sector_bars"] is not None,
        "volatility":  "vix_level" in snapshot and snapshot["vix_level"] is not None,
        "options":     "put_call_ratio" in snapshot and snapshot["put_call_ratio"] is not None,
        "safe_haven":  "safe_haven_bars" in snapshot and snapshot["safe_haven_bars"] is not None,
        "credit":      "credit_bars" in snapshot and snapshot["credit_bars"] is not None,
        "macro":       "macro_news" in snapshot and snapshot["macro_news"] is not None,
        "news":        "news_scores" in snapshot and snapshot["news_scores"] is not None,
        "social":      "social_feed" in snapshot and snapshot["social_feed"] is not None,
    }

    for ch, is_active in channels.items():
        state.input_channels[ch] = "active" if is_active else "inactive"

    state.active_input_count = sum(1 for v in state.input_channels.values() if v == "active")

    if state.active_input_count < MIN_REQUIRED_INPUTS:
        state.halt("too_few_inputs")
        state.record(2, "input_universe", {"active_count": state.active_input_count, "channels": state.input_channels}, "halted", "too_few_inputs", ts)
        return state

    state.record(2, "input_universe", {"active_count": state.active_input_count, "channels": state.input_channels}, "universe_mapped", "ok", ts)
    return state


def gate_3_benchmark(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    spy_bars = snapshot.get("spy_bars", [])

    if not spy_bars or len(spy_bars) < SPY_SMA_LONG:
        state.benchmark_state = "insufficient_data"
        state.benchmark_momentum_state = "insufficient_data"
        state.benchmark_risk_state = "insufficient_data"
        state.benchmark_vol_state = "insufficient_data"
        state.record(3, "benchmark", {"spy_bars_len": len(spy_bars) if spy_bars else 0}, "insufficient_data", "not_enough_bars", ts)
        return state

    closes = [b["close"] for b in spy_bars]

    sma20 = _mean(closes[-SPY_SMA_SHORT:])
    sma50 = _mean(closes[-SPY_SMA_LONG:])
    sma_diff = (sma20 - sma50) / sma50 if sma50 != 0 else 0.0

    if sma_diff > TREND_NEUTRAL_BAND:
        benchmark_state = "bullish"
    elif sma_diff < -TREND_NEUTRAL_BAND:
        benchmark_state = "bearish"
    else:
        benchmark_state = "neutral"

    if len(closes) >= SPY_ROC_BARS + 1:
        roc = (closes[-1] - closes[-(SPY_ROC_BARS + 1)]) / closes[-(SPY_ROC_BARS + 1)] if closes[-(SPY_ROC_BARS + 1)] != 0 else 0.0
    else:
        roc = 0.0
    benchmark_momentum_state = "rising" if roc > 0 else ("falling" if roc < 0 else "flat")

    last_20 = closes[-20:]
    max_20 = max(last_20)
    dd = (max_20 - closes[-1]) / max_20 if max_20 != 0 else 0.0
    benchmark_risk_state = "drawdown" if dd > DRAWDOWN_THRESHOLD else "normal"

    if len(closes) >= 2:
        intraday_vol = abs(closes[-1] - closes[-2]) / closes[-2] if closes[-2] != 0 else 0.0
    else:
        intraday_vol = 0.0
    benchmark_vol_state = "elevated" if intraday_vol > SPX_VOL_THRESHOLD else "normal"

    state.benchmark_state = benchmark_state
    state.benchmark_momentum_state = benchmark_momentum_state
    state.benchmark_risk_state = benchmark_risk_state
    state.benchmark_vol_state = benchmark_vol_state

    state.record(3, "benchmark", {
        "sma20": round(sma20, 4), "sma50": round(sma50, 4),
        "sma_diff": round(sma_diff, 6), "roc": round(roc, 6),
        "drawdown": round(dd, 6), "intraday_vol": round(intraday_vol, 6),
    }, {
        "benchmark_state": benchmark_state,
        "benchmark_momentum_state": benchmark_momentum_state,
        "benchmark_risk_state": benchmark_risk_state,
        "benchmark_vol_state": benchmark_vol_state,
    }, "ok", ts)
    return state


def gate_4_price_action(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — actual sector ETF bar data not yet wired
    sector_bars = snapshot.get("sector_bars", {})

    if not sector_bars or len(sector_bars) < 3:
        state.price_score = 0.0
        state.price_sentiment_state = "insufficient_data"
        state.price_structure_state = "insufficient_data"
        state.price_momentum_state = "insufficient_data"
        state.price_dispersion_state = "insufficient_data"
        state.record(4, "price_action", {"sector_count": len(sector_bars)}, "insufficient_data", "not_enough_sectors", ts)
        return state

    # 1-day return for each sector
    sector_returns = {}
    for sector, bars in sector_bars.items():
        if isinstance(bars, list) and len(bars) >= 2:
            ret = (bars[-1] - bars[-2]) / bars[-2] if bars[-2] != 0 else 0.0
            sector_returns[sector] = ret
        elif isinstance(bars, list) and len(bars) == 1:
            sector_returns[sector] = 0.0

    total = len(sector_returns)
    pct_up = sum(1 for r in sector_returns.values() if r > 0) / total if total > 0 else 0.5

    if pct_up > 0.60:
        price_sentiment_state = "bullish"
    elif pct_up < 0.40:
        price_sentiment_state = "bearish"
    else:
        price_sentiment_state = "neutral"

    # ROC acceleration using SPY bars
    spy_bars = snapshot.get("spy_bars", [])
    price_structure_state = "neutral"
    if spy_bars and len(spy_bars) >= 7:
        closes = [b["close"] for b in spy_bars]
        recent_roc = (closes[-1] - closes[-4]) / closes[-4] if closes[-4] != 0 else 0.0
        prior_roc = (closes[-4] - closes[-7]) / closes[-7] if closes[-7] != 0 else 0.0
        acceleration = recent_roc - prior_roc
        if acceleration > 0.001:
            price_structure_state = "accelerating"
        elif acceleration < -0.001:
            price_structure_state = "decelerating"
        else:
            price_structure_state = "neutral"

    # Momentum from benchmark
    if state.benchmark_momentum_state == "rising":
        price_momentum_state = "strong"
    elif state.benchmark_momentum_state == "falling":
        price_momentum_state = "weak"
    else:
        price_momentum_state = "neutral"

    # Cross-sector dispersion
    ret_values = list(sector_returns.values())
    disp_std = _std(ret_values)
    price_dispersion_state = "high" if disp_std > 0.01 else "low"

    # Price score
    if price_sentiment_state == "bullish":
        price_score = 0.5
    elif price_sentiment_state == "bearish":
        price_score = -0.5
    else:
        price_score = 0.0

    if price_structure_state == "accelerating":
        price_score += 0.2
    elif price_structure_state == "decelerating":
        price_score -= 0.2

    price_score = _clamp(price_score)

    state.price_score = price_score
    state.price_sentiment_state = price_sentiment_state
    state.price_structure_state = price_structure_state
    state.price_momentum_state = price_momentum_state
    state.price_dispersion_state = price_dispersion_state

    state.record(4, "price_action", {
        "sector_count": total, "pct_up": round(pct_up, 4),
        "disp_std": round(disp_std, 6),
    }, {
        "price_score": price_score,
        "price_sentiment_state": price_sentiment_state,
        "price_structure_state": price_structure_state,
        "price_momentum_state": price_momentum_state,
        "price_dispersion_state": price_dispersion_state,
    }, "ok", ts)
    return state


def gate_5_breadth(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — advance/decline line requires exchange data
    sector_bars = snapshot.get("sector_bars", {})

    if not sector_bars:
        state.breadth_score = 0.0
        state.breadth_state = "insufficient_data"
        state.breadth_momentum_state = "insufficient_data"
        state.breadth_quality_state = "insufficient_data"
        state.record(5, "breadth", {"sector_count": 0}, "insufficient_data", "no_sector_bars", ts)
        return state

    # Today's positive fraction
    today_positive = 0
    today_total = 0
    yesterday_positive = 0
    yesterday_total = 0

    for sector, bars in sector_bars.items():
        if isinstance(bars, list) and len(bars) >= 2:
            ret_today = (bars[-1] - bars[-2]) / bars[-2] if bars[-2] != 0 else 0.0
            if ret_today > 0:
                today_positive += 1
            today_total += 1
            if len(bars) >= 3:
                ret_yest = (bars[-2] - bars[-3]) / bars[-3] if bars[-3] != 0 else 0.0
                if ret_yest > 0:
                    yesterday_positive += 1
                yesterday_total += 1
        elif isinstance(bars, list) and len(bars) == 1:
            today_total += 1

    pct_positive = today_positive / today_total if today_total > 0 else 0.5

    if pct_positive > 0.60:
        breadth_state = "broad"
    elif pct_positive < 0.40:
        breadth_state = "narrow"
    else:
        breadth_state = "neutral"

    # Breadth momentum
    if yesterday_total > 0:
        pct_yest = yesterday_positive / yesterday_total
        if pct_positive > pct_yest:
            breadth_momentum_state = "improving"
        elif pct_positive < pct_yest:
            breadth_momentum_state = "deteriorating"
        else:
            breadth_momentum_state = "neutral"
    else:
        breadth_momentum_state = "neutral"

    # TODO:DATA_DEPENDENCY — RSP vs SPY spread required for quality check
    breadth_quality_state = "unconfirmed"  # TODO:DATA_DEPENDENCY

    if breadth_state == "broad":
        breadth_score = 0.5
    elif breadth_state == "narrow":
        breadth_score = -0.5
    else:
        breadth_score = 0.0
    breadth_score = _clamp(breadth_score)

    state.breadth_score = breadth_score
    state.breadth_state = breadth_state
    state.breadth_momentum_state = breadth_momentum_state
    state.breadth_quality_state = breadth_quality_state

    state.record(5, "breadth", {
        "pct_positive": round(pct_positive, 4),
        "today_total": today_total,
    }, {
        "breadth_score": breadth_score,
        "breadth_state": breadth_state,
        "breadth_momentum_state": breadth_momentum_state,
        "breadth_quality_state": breadth_quality_state,
    }, "ok", ts)
    return state


def gate_6_volume(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    spy_bars = snapshot.get("spy_bars", [])

    if not spy_bars or len(spy_bars) < 5:
        state.volume_score = 0.0
        state.volume_sentiment_state = "insufficient_data"
        state.volume_pattern_state = "insufficient_data"
        state.volume_confirmation_state = "insufficient_data"
        state.record(6, "volume", {"spy_bars_len": len(spy_bars) if spy_bars else 0}, "insufficient_data", "not_enough_bars", ts)
        return state

    closes = [b["close"] for b in spy_bars]
    volumes = [b["volume"] for b in spy_bars]

    lookback = min(20, len(volumes))
    avg_vol = _mean(volumes[-lookback:])
    recent_vol = volumes[-1]
    vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    price_dir = "up" if closes[-1] > closes[-2] else "down"

    if vol_ratio > 1.2 and price_dir == "up":
        volume_sentiment_state = "buying_pressure"
    elif vol_ratio > 1.2 and price_dir == "down":
        volume_sentiment_state = "selling_pressure"
    else:
        volume_sentiment_state = "neutral"

    if volume_sentiment_state == "buying_pressure" and state.benchmark_state == "bullish":
        volume_pattern_state = "trend_confirmed"
    elif volume_sentiment_state == "selling_pressure" and vol_ratio > 1.5:
        volume_pattern_state = "distribution"
    elif volume_sentiment_state == "buying_pressure" and vol_ratio > 1.5:
        volume_pattern_state = "accumulation"
    else:
        volume_pattern_state = "neutral"

    if volume_pattern_state == "trend_confirmed":
        volume_confirmation_state = "confirmed"
    elif volume_pattern_state == "distribution":
        volume_confirmation_state = "unconfirmed"
    else:
        volume_confirmation_state = "neutral"

    volume_score = 0.0
    if volume_sentiment_state == "buying_pressure":
        volume_score += 0.4
    elif volume_sentiment_state == "selling_pressure":
        volume_score -= 0.4

    if volume_pattern_state == "trend_confirmed":
        volume_score += 0.3
    elif volume_pattern_state == "distribution":
        volume_score -= 0.3

    volume_score = _clamp(volume_score)

    state.volume_score = volume_score
    state.volume_sentiment_state = volume_sentiment_state
    state.volume_pattern_state = volume_pattern_state
    state.volume_confirmation_state = volume_confirmation_state

    state.record(6, "volume", {
        "vol_ratio": round(vol_ratio, 4),
        "price_dir": price_dir,
        "avg_vol": round(avg_vol, 2),
        "recent_vol": recent_vol,
    }, {
        "volume_score": volume_score,
        "volume_sentiment_state": volume_sentiment_state,
        "volume_pattern_state": volume_pattern_state,
        "volume_confirmation_state": volume_confirmation_state,
    }, "ok", ts)
    return state


def gate_7_volatility(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — real-time VIX feed not yet wired
    vix = snapshot.get("vix_level")
    vix_hist = snapshot.get("vix_history", [])
    spy_bars = snapshot.get("spy_bars", [])

    # Realized vol from spy_bars
    realized_vol_state = "neutral"
    vol_instability_state = "normal"
    if spy_bars and len(spy_bars) >= 10:
        closes = [b["close"] for b in spy_bars]
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, min(11, len(closes)))]
        realized_std = _std(returns)
        if realized_std > 0.015:
            realized_vol_state = "expanding"
        elif realized_std < 0.007:
            realized_vol_state = "contracting"
        else:
            realized_vol_state = "neutral"
    else:
        realized_std = 0.0

    if vix is None:
        state.vol_score = 0.0
        state.vol_sentiment_state = "unknown"
        state.vol_structure_state = "unknown"
        state.realized_vol_state = realized_vol_state
        state.vol_instability_state = "unstable" if realized_vol_state == "expanding" else "normal"
        state.record(7, "volatility", {"vix": None, "realized_vol_state": realized_vol_state}, "vix_unavailable", "no_vix_feed", ts)
        return state

    if vix >= VIX_ELEVATED_THRESHOLD:
        vol_sentiment_state = "risk_off"
    elif vix < 15.0:
        vol_sentiment_state = "risk_on"
    else:
        vol_sentiment_state = "neutral"

    if vix_hist and len(vix_hist) >= 2:
        prev_vix = vix_hist[-2]
        roc_vix = (vix - prev_vix) / prev_vix if prev_vix > 0 else 0.0
        if roc_vix > VIX_ROC_SPIKE_THRESHOLD:
            vol_structure_state = "spiking"
        elif vix >= VIX_ELEVATED_THRESHOLD:
            vol_structure_state = "elevated"
        else:
            vol_structure_state = "calm"
    else:
        vol_structure_state = "elevated" if vix >= VIX_ELEVATED_THRESHOLD else "calm"

    vol_instability_state = "unstable" if (vol_structure_state == "spiking" or realized_vol_state == "expanding") else "normal"

    vol_score = 0.0
    if vol_sentiment_state == "risk_off":
        vol_score = -0.5
    elif vol_sentiment_state == "risk_on":
        vol_score = 0.4

    if vol_structure_state == "spiking":
        vol_score -= 0.3
    if realized_vol_state == "contracting":
        vol_score += 0.1

    vol_score = _clamp(vol_score)

    state.vol_score = vol_score
    state.vol_sentiment_state = vol_sentiment_state
    state.vol_structure_state = vol_structure_state
    state.realized_vol_state = realized_vol_state
    state.vol_instability_state = vol_instability_state

    state.record(7, "volatility", {
        "vix": vix, "vol_sentiment_state": vol_sentiment_state,
        "vol_structure_state": vol_structure_state,
        "realized_vol_state": realized_vol_state,
    }, {
        "vol_score": vol_score,
        "vol_instability_state": vol_instability_state,
    }, "ok", ts)
    return state


def gate_8_options(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — per-ticker put/call requires paid options data
    pc_ratio = snapshot.get("put_call_ratio")

    if pc_ratio is None:
        state.options_score = 0.0
        state.options_sentiment_state = "unavailable"
        state.options_tail_risk_state = "unavailable"
        state.options_speculation_state = "unavailable"
        state.options_flow_state = "unavailable"
        state.options_corr_state = "unavailable"
        state.record(8, "options", {"put_call_ratio": None}, "unavailable", "no_options_data", ts)
        return state

    pc_hist = snapshot.get("pc_ratio_history", [])

    if pc_ratio > 1.2:
        options_sentiment_state = "fearful"
    elif pc_ratio < 0.7:
        options_sentiment_state = "complacent"
    else:
        options_sentiment_state = "neutral"

    options_tail_risk_state = "elevated" if pc_ratio > 1.3 else "normal"
    options_speculation_state = "call_speculation" if pc_ratio < 0.6 else "neutral"

    # TODO:DATA_DEPENDENCY — smart money vs retail flow
    options_flow_state = "neutral"
    # TODO:DATA_DEPENDENCY — options correlation state
    options_corr_state = "normal"

    if options_sentiment_state == "fearful":
        options_score = 0.4
    elif options_sentiment_state == "complacent":
        options_score = -0.4
    else:
        options_score = 0.0

    if options_tail_risk_state == "elevated":
        options_score += 0.1

    options_score = _clamp(options_score)

    state.options_score = options_score
    state.options_sentiment_state = options_sentiment_state
    state.options_tail_risk_state = options_tail_risk_state
    state.options_speculation_state = options_speculation_state
    state.options_flow_state = options_flow_state
    state.options_corr_state = options_corr_state

    state.record(8, "options", {"put_call_ratio": pc_ratio}, {
        "options_score": options_score,
        "options_sentiment_state": options_sentiment_state,
        "options_tail_risk_state": options_tail_risk_state,
        "options_speculation_state": options_speculation_state,
    }, "ok", ts)
    return state


def gate_9_safe_haven(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — safe haven ETF bars not yet wired
    safe_bars = snapshot.get("safe_haven_bars", {})

    safe_havens = ["GLD", "TLT", "UUP"]

    if not safe_bars or "SPY" not in safe_bars or all(k not in safe_bars for k in safe_havens):
        state.cross_asset_score = 0.0
        state.cross_asset_state = "insufficient_data"
        state.rotation_state = "insufficient_data"
        state.risk_appetite_state = "insufficient_data"
        state.record(9, "safe_haven", {"keys": list(safe_bars.keys()) if safe_bars else []}, "insufficient_data", "missing_keys", ts)
        return state

    def five_day_return(bars):
        if isinstance(bars, list) and len(bars) >= 6:
            return (bars[-1] - bars[-6]) / bars[-6] if bars[-6] != 0 else 0.0
        elif isinstance(bars, list) and len(bars) >= 2:
            return (bars[-1] - bars[0]) / bars[0] if bars[0] != 0 else 0.0
        return 0.0

    risk_off_count = 0
    risk_on_count = 0

    for etf in safe_havens:
        if etf in safe_bars:
            ret = five_day_return(safe_bars[etf])
            if ret > 0:
                risk_off_count += 1

    spy_return = five_day_return(safe_bars["SPY"])
    if spy_return > 0:
        risk_on_count += 1
    elif spy_return < 0:
        risk_off_count += 1

    if risk_off_count >= 2 and spy_return < 0:
        cross_asset_state = "risk_off_confirmed"
    elif risk_on_count > 0 and risk_off_count == 0:
        cross_asset_state = "risk_on_confirmed"
    elif risk_off_count >= 1 and risk_on_count >= 1:
        cross_asset_state = "mixed"
    else:
        cross_asset_state = "neutral"

    safe_haven_up = sum(1 for etf in safe_havens if etf in safe_bars and five_day_return(safe_bars[etf]) > 0)
    if safe_haven_up >= 2 and spy_return < 0:
        rotation_state = "into_safety"
    elif spy_return > 0 and safe_haven_up == 0:
        rotation_state = "into_risk"
    else:
        rotation_state = "neutral"

    if cross_asset_state == "risk_on_confirmed":
        risk_appetite_state = "high"
    elif cross_asset_state == "risk_off_confirmed":
        risk_appetite_state = "low"
    else:
        risk_appetite_state = "neutral"

    if cross_asset_state == "risk_on_confirmed":
        cross_asset_score = 0.5
    elif cross_asset_state == "risk_off_confirmed":
        cross_asset_score = -0.5
    elif cross_asset_state == "mixed":
        cross_asset_score = -0.2
    else:
        cross_asset_score = 0.0
    cross_asset_score = _clamp(cross_asset_score)

    state.cross_asset_score = cross_asset_score
    state.cross_asset_state = cross_asset_state
    state.rotation_state = rotation_state
    state.risk_appetite_state = risk_appetite_state

    state.record(9, "safe_haven", {
        "spy_return": round(spy_return, 6),
        "risk_off_count": risk_off_count,
        "risk_on_count": risk_on_count,
    }, {
        "cross_asset_score": cross_asset_score,
        "cross_asset_state": cross_asset_state,
        "rotation_state": rotation_state,
        "risk_appetite_state": risk_appetite_state,
    }, "ok", ts)
    return state


def gate_10_credit(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — CDS/CDX index and funding spreads require paid data
    credit_bars = snapshot.get("credit_bars", {})

    if not credit_bars or "HYG" not in credit_bars or "LQD" not in credit_bars:
        state.credit_score = 0.0
        state.credit_state = "insufficient_data"
        state.credit_risk_state = "insufficient_data"
        state.credit_stress_state = "insufficient_data"
        state.liquidity_state = "insufficient_data"
        state.record(10, "credit", {"keys": list(credit_bars.keys()) if credit_bars else []}, "insufficient_data", "missing_hyg_lqd", ts)
        return state

    def five_day_return(bars):
        if isinstance(bars, list) and len(bars) >= 6:
            return (bars[-1] - bars[-6]) / bars[-6] if bars[-6] != 0 else 0.0
        elif isinstance(bars, list) and len(bars) >= 2:
            return (bars[-1] - bars[0]) / bars[0] if bars[0] != 0 else 0.0
        return 0.0

    hyg_ret = five_day_return(credit_bars["HYG"])
    lqd_ret = five_day_return(credit_bars["LQD"])
    spread_proxy = lqd_ret - hyg_ret

    if hyg_ret > 0.002 and spread_proxy < 0:
        credit_state = "tightening"
    elif hyg_ret < -0.002 or spread_proxy > 0.003:
        credit_state = "widening"
    else:
        credit_state = "neutral"

    credit_risk_state = "high" if credit_state == "widening" else ("low" if credit_state == "tightening" else "neutral")
    credit_stress_state = "stressed" if spread_proxy > 0.005 else "normal"

    # TODO:DATA_DEPENDENCY — funding spread data for liquidity state
    liquidity_state = "normal"

    if credit_state == "tightening":
        credit_score = 0.4
    elif credit_state == "widening":
        credit_score = -0.4
    else:
        credit_score = 0.0

    if credit_stress_state == "stressed":
        credit_score -= 0.3

    credit_score = _clamp(credit_score)

    state.credit_score = credit_score
    state.credit_state = credit_state
    state.credit_risk_state = credit_risk_state
    state.credit_stress_state = credit_stress_state
    state.liquidity_state = liquidity_state

    state.record(10, "credit", {
        "hyg_ret": round(hyg_ret, 6),
        "lqd_ret": round(lqd_ret, 6),
        "spread_proxy": round(spread_proxy, 6),
    }, {
        "credit_score": credit_score,
        "credit_state": credit_state,
        "credit_risk_state": credit_risk_state,
        "credit_stress_state": credit_stress_state,
    }, "ok", ts)
    return state


def gate_11_sector_rotation(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — full sector ETF set (XLK, XLF, XLE, XLV, etc.)
    sector_bars = snapshot.get("sector_bars", {})

    if not sector_bars or len(sector_bars) < 3:
        state.sector_score = 0.0
        state.sector_leadership_state = "insufficient_data"
        state.sector_rotation_state = "insufficient_data"
        state.sector_confirmation_state = "insufficient_data"
        state.record(11, "sector_rotation", {"sector_count": len(sector_bars)}, "insufficient_data", "not_enough_sectors", ts)
        return state

    defensive_sectors = {"XLV", "XLU", "XLP", "XLRE"}
    cyclical_sectors  = {"XLK", "XLF", "XLE", "XLI", "XLB", "XLY"}

    def five_day_return(bars):
        if isinstance(bars, list) and len(bars) >= 6:
            return (bars[-1] - bars[-6]) / bars[-6] if bars[-6] != 0 else 0.0
        elif isinstance(bars, list) and len(bars) >= 2:
            return (bars[-1] - bars[0]) / bars[0] if bars[0] != 0 else 0.0
        return 0.0

    cyclical_rets = [five_day_return(bars) for sector, bars in sector_bars.items() if sector in cyclical_sectors]
    defensive_rets = [five_day_return(bars) for sector, bars in sector_bars.items() if sector in defensive_sectors]

    if cyclical_rets and defensive_rets:
        cyclical_avg = _mean(cyclical_rets)
        defensive_avg = _mean(defensive_rets)
        if cyclical_avg > defensive_avg + 0.003:
            sector_leadership_state = "cyclical"
        elif defensive_avg > cyclical_avg + 0.003:
            sector_leadership_state = "defensive"
        else:
            sector_leadership_state = "mixed"
    elif cyclical_rets:
        cyclical_avg = _mean(cyclical_rets)
        defensive_avg = 0.0
        sector_leadership_state = "cyclical" if cyclical_avg > 0 else "mixed"
    elif defensive_rets:
        cyclical_avg = 0.0
        defensive_avg = _mean(defensive_rets)
        sector_leadership_state = "defensive" if defensive_avg > 0 else "mixed"
    else:
        cyclical_avg = 0.0
        defensive_avg = 0.0
        sector_leadership_state = "neutral"

    if sector_leadership_state == "cyclical":
        sector_rotation_state = "risk_on"
    elif sector_leadership_state == "defensive":
        sector_rotation_state = "risk_off"
    else:
        sector_rotation_state = "neutral"

    bench = state.benchmark_state
    if (sector_rotation_state == "risk_on" and bench == "bullish") or \
       (sector_rotation_state == "risk_off" and bench == "bearish"):
        sector_confirmation_state = "confirmed"
    elif sector_rotation_state in ["risk_on", "risk_off"]:
        sector_confirmation_state = "unconfirmed"
    else:
        sector_confirmation_state = "unconfirmed"

    if sector_leadership_state == "cyclical":
        sector_score = 0.4
    elif sector_leadership_state == "defensive":
        sector_score = -0.4
    else:
        sector_score = 0.0

    if sector_confirmation_state == "confirmed":
        sector_score += 0.1

    sector_score = _clamp(sector_score)

    state.sector_score = sector_score
    state.sector_leadership_state = sector_leadership_state
    state.sector_rotation_state = sector_rotation_state
    state.sector_confirmation_state = sector_confirmation_state

    state.record(11, "sector_rotation", {
        "cyclical_avg": round(cyclical_avg, 6),
        "defensive_avg": round(defensive_avg, 6),
        "sector_count": len(sector_bars),
    }, {
        "sector_score": sector_score,
        "sector_leadership_state": sector_leadership_state,
        "sector_rotation_state": sector_rotation_state,
        "sector_confirmation_state": sector_confirmation_state,
    }, "ok", ts)
    return state


def gate_12_macro(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — economic surprise indices require FRED/Bloomberg
    macro_news = snapshot.get("macro_news", [])

    if not macro_news:
        state.macro_score = 0.0
        state.macro_state = "no_macro_news"
        state.macro_policy_state = "neutral"
        state.macro_sentiment_state = "neutral"
        state.record(12, "macro", {"macro_news_count": 0}, "no_macro_news", "no_macro_data", ts)
        return state

    bull_count = len([a for a in macro_news if isinstance(a, dict) and
                      a.get("final_signal") in ["bullish_signal", "benchmark_override"] and
                      a.get("action_state") != "ignore"])
    bear_count = len([a for a in macro_news if isinstance(a, dict) and
                      a.get("final_signal") in ["bearish_signal"] and
                      a.get("action_state") != "ignore"])

    # Check for inflationary keywords
    inflation_keywords = ["inflation", "cpi", "pce", "tariff", "price pressure"]
    has_inflation = any(
        any(kw in str(a.get("final_routing", "")).lower() or kw in str(a.get("headline", "")).lower()
            for kw in inflation_keywords)
        for a in macro_news if isinstance(a, dict)
    )

    if bear_count > bull_count + 1:
        macro_state = "growth_fear"
    elif bull_count > bear_count + 1:
        macro_state = "growth_expected"
    elif has_inflation:
        macro_state = "inflationary_fear"
    else:
        macro_state = "neutral"

    # Policy keywords
    easing_keywords = ["easing", "cut", "dovish", "stimulus"]
    tightening_keywords = ["tightening", "hike", "hawkish", "restrictive"]
    has_easing = any(
        any(kw in str(a.get("final_routing", "")).lower() for kw in easing_keywords)
        for a in macro_news if isinstance(a, dict)
    )
    has_tightening = any(
        any(kw in str(a.get("final_routing", "")).lower() for kw in tightening_keywords)
        for a in macro_news if isinstance(a, dict)
    )

    if has_easing:
        macro_policy_state = "easing_expected"
    elif has_tightening:
        macro_policy_state = "tightening_expected"
    else:
        macro_policy_state = "neutral"

    net_macro = bull_count - bear_count
    if net_macro > 0:
        macro_sentiment_state = "dovish"
    elif net_macro < 0:
        macro_sentiment_state = "hawkish"
    else:
        macro_sentiment_state = "neutral"

    if macro_state == "growth_expected":
        macro_score = 0.3
    elif macro_state == "growth_fear":
        macro_score = -0.3
    elif macro_state == "inflationary_fear":
        macro_score = -0.4
    else:
        macro_score = 0.0

    macro_score = _clamp(macro_score)

    state.macro_score = macro_score
    state.macro_state = macro_state
    state.macro_policy_state = macro_policy_state
    state.macro_sentiment_state = macro_sentiment_state

    state.record(12, "macro", {
        "bull_count": bull_count, "bear_count": bear_count,
        "has_inflation": has_inflation,
    }, {
        "macro_score": macro_score,
        "macro_state": macro_state,
        "macro_policy_state": macro_policy_state,
        "macro_sentiment_state": macro_sentiment_state,
    }, "ok", ts)
    return state


def gate_13_news_sentiment(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    news_scores = snapshot.get("news_scores", [])

    if not news_scores:
        state.news_score = 0.0
        state.news_sentiment_state = "no_recent_news"
        state.news_driver_state = "no_recent_news"
        state.news_conviction_state = "no_recent_news"
        state.record(13, "news_sentiment", {"news_count": 0}, "no_recent_news", "no_news_data", ts)
        return state

    # Filter to last NEWS_LOOKBACK_HOURS hours
    now_dt = datetime.datetime.utcnow()
    cutoff = now_dt - datetime.timedelta(hours=NEWS_LOOKBACK_HOURS)

    filtered = []
    for a in news_scores:
        if not isinstance(a, dict):
            continue
        art_ts = a.get("ts") or a.get("published_at") or a.get("session_ts", "")
        try:
            art_dt = datetime.datetime.fromisoformat(str(art_ts).replace("Z", "+00:00"))
            # Compare without timezone if needed
            if art_dt.tzinfo:
                art_dt = art_dt.replace(tzinfo=None)
            if art_dt >= cutoff:
                filtered.append(a)
        except Exception:
            filtered.append(a)

    if not filtered:
        filtered = news_scores

    total = len(filtered)
    positive = len([a for a in filtered if a.get("final_signal") in ["bullish_signal", "benchmark_override"]])
    negative = len([a for a in filtered if a.get("final_signal") == "bearish_signal"])

    pct_positive = positive / total if total > 0 else 0.5

    if pct_positive > 0.60:
        news_sentiment_state = "positive"
    elif pct_positive < 0.40:
        news_sentiment_state = "negative"
    else:
        news_sentiment_state = "neutral"

    # Driver analysis
    composite_scores = [float(a.get("composite_score", 0.0)) for a in filtered if "composite_score" in a]
    if composite_scores:
        mean_cs = _mean(composite_scores)
        max_cs = max(composite_scores)
        if max_cs > mean_cs * 1.5 and mean_cs > 0:
            news_driver_state = "single_driver"
        elif total >= 3:
            news_driver_state = "multi_driver"
        else:
            news_driver_state = "neutral"

        # Conviction from top 3
        top3 = sorted(composite_scores, reverse=True)[:3]
        mean_top3 = _mean(top3)
        news_conviction_state = "high" if mean_top3 > 0.60 else "low"
    else:
        news_driver_state = "neutral"
        news_conviction_state = "low"
        mean_cs = 0.0

    if news_sentiment_state == "positive":
        news_score = 0.3
    elif news_sentiment_state == "negative":
        news_score = -0.3
    else:
        news_score = 0.0

    if news_conviction_state == "high":
        if news_sentiment_state == "positive":
            news_score += 0.1
        elif news_sentiment_state == "negative":
            news_score -= 0.1

    news_score = _clamp(news_score)

    state.news_score = news_score
    state.news_sentiment_state = news_sentiment_state
    state.news_driver_state = news_driver_state
    state.news_conviction_state = news_conviction_state

    state.record(13, "news_sentiment", {
        "total": total, "positive": positive, "negative": negative,
        "pct_positive": round(pct_positive, 4),
    }, {
        "news_score": news_score,
        "news_sentiment_state": news_sentiment_state,
        "news_driver_state": news_driver_state,
        "news_conviction_state": news_conviction_state,
    }, "ok", ts)
    return state


def gate_14_social(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — StockTwits/Twitter X API not yet wired
    social_feed = snapshot.get("social_feed")

    if social_feed is None:
        state.social_score = 0.0
        state.social_quality_state = "low_trust"
        state.social_sentiment_state = "unavailable"
        state.social_attention_state = "unknown"
        state.record(14, "social", {"social_feed": None}, "unavailable", "no_social_feed", ts)
        return state

    # Social quality is always low_trust per governance; score locked to 0.0
    state.social_score = 0.0
    state.social_quality_state = "low_trust"
    state.social_sentiment_state = "present_but_locked"
    state.social_attention_state = "unknown"

    state.record(14, "social", {"social_feed_present": True}, {
        "social_score": 0.0,
        "social_quality_state": "low_trust",
    }, "quality_gate_applied", ts)
    return state


def gate_15_breadth_price_divergence(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    if state.price_sentiment_state == "bullish" and state.breadth_state == "narrow":
        divergence_state = "price_ahead_of_breadth"
    elif state.price_sentiment_state == "bearish" and state.breadth_state == "broad":
        divergence_state = "price_lagging_breadth"
    else:
        divergence_state = "none"

    state.divergence_state = divergence_state

    state.record(15, "breadth_price_divergence", {
        "price_sentiment_state": state.price_sentiment_state,
        "breadth_state": state.breadth_state,
    }, {"divergence_state": divergence_state}, "ok", ts)
    return state


def gate_16_component_score_collection(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    def active(ch):
        return state.input_channels.get(ch) == "active"

    component_scores = {
        "price":       state.price_score       if active("price")       else 0.0,
        "breadth":     state.breadth_score      if active("breadth")     else 0.0,
        "volatility":  state.vol_score          if active("volatility")  else 0.0,
        "volume":      state.volume_score       if active("volume")      else 0.0,
        "options":     state.options_score      if active("options")     else 0.0,
        "cross_asset": state.cross_asset_score  if active("safe_haven")  else 0.0,
        "credit":      state.credit_score       if active("credit")      else 0.0,
        "macro":       state.macro_score        if active("macro")       else 0.0,
        "news":        state.news_score         if active("news")        else 0.0,
        "social":      state.social_score       if active("social")      else 0.0,
    }

    state.component_scores = component_scores

    state.record(16, "component_score_collection", {
        "channels_active": [k for k, v in state.input_channels.items() if v == "active"],
    }, {"component_scores": component_scores}, "ok", ts)
    return state


def gate_17_weighting(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    channel_weights = {
        "price":       WEIGHT_PRICE,
        "breadth":     WEIGHT_BREADTH,
        "volatility":  WEIGHT_VOLATILITY,
        "volume":      WEIGHT_VOLUME,
        "options":     WEIGHT_OPTIONS,
        "cross_asset": WEIGHT_CROSS_ASSET,
        "credit":      WEIGHT_CREDIT,
        "macro":       WEIGHT_MACRO,
        "news":        WEIGHT_NEWS,
        "social":      WEIGHT_SOCIAL,
    }

    # Map input_channels keys to component keys for active check
    # input_channels uses: price, volume, breadth, volatility, options, safe_haven, credit, macro, news, social
    # component_scores uses: price, breadth, volatility, volume, options, cross_asset, credit, macro, news, social
    channel_to_input = {
        "price":       "price",
        "breadth":     "breadth",
        "volatility":  "volatility",
        "volume":      "volume",
        "options":     "options",
        "cross_asset": "safe_haven",
        "credit":      "credit",
        "macro":       "macro",
        "news":        "news",
        "social":      "social",
    }

    active_weights = {
        ch: w for ch, w in channel_weights.items()
        if state.input_channels.get(channel_to_input.get(ch, ch)) == "active"
    }
    total_active_weight = sum(active_weights.values())

    if total_active_weight > 0:
        effective_weights = {ch: w / total_active_weight for ch, w in active_weights.items()}
    else:
        effective_weights = {ch: 0.0 for ch in channel_weights}

    state.effective_weights = effective_weights

    state.record(17, "weighting", {
        "active_channels": list(active_weights.keys()),
        "total_active_weight": round(total_active_weight, 6),
    }, {"effective_weights": {k: round(v, 6) for k, v in effective_weights.items()}}, "ok", ts)
    return state


def gate_18_composite_score(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    raw_sentiment_score = sum(
        state.effective_weights.get(ch, 0.0) * state.component_scores.get(ch, 0.0)
        for ch in state.effective_weights
    )
    raw_sentiment_score = _clamp(raw_sentiment_score)

    if raw_sentiment_score >= COMPOSITE_EUPHORIA_THRESHOLD:
        market_sentiment_state = "euphoric"
    elif raw_sentiment_score <= COMPOSITE_PANIC_THRESHOLD:
        market_sentiment_state = "panic"
    elif raw_sentiment_score >= COMPOSITE_BULLISH_THRESHOLD:
        market_sentiment_state = "bullish"
    elif raw_sentiment_score <= COMPOSITE_BEARISH_THRESHOLD:
        market_sentiment_state = "bearish"
    else:
        market_sentiment_state = "neutral"

    state.raw_sentiment_score = raw_sentiment_score
    state.market_sentiment_state = market_sentiment_state

    state.record(18, "composite_score", {
        "component_scores": state.component_scores,
        "effective_weights": {k: round(v, 6) for k, v in state.effective_weights.items()},
    }, {
        "raw_sentiment_score": round(raw_sentiment_score, 6),
        "market_sentiment_state": market_sentiment_state,
    }, "ok", ts)
    return state


def gate_19_confidence(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    active_count = state.active_input_count
    input_quality = min(1.0, active_count / (MIN_CONFIDENT_COMPONENTS * 1.5))

    scores_list = list(state.component_scores.values())
    score_std = _std(scores_list) if len(scores_list) > 1 else 0.0
    signal_agreement_score = max(0.0, 1.0 - (score_std / (AGREEMENT_THRESHOLD * 3)))

    active_nonzero = len([
        v for k, v in state.component_scores.items()
        if state.input_channels.get(
            {"cross_asset": "safe_haven"}.get(k, k)
        ) == "active" and abs(v) > 0
    ])
    data_quality_score = active_nonzero / active_count if active_count > 0 else 0.0

    sentiment_confidence = (0.4 * input_quality) + (0.3 * signal_agreement_score) + (0.3 * data_quality_score)
    sentiment_confidence = round(max(0.0, min(1.0, sentiment_confidence)), 4)

    if sentiment_confidence >= CONFIDENCE_HIGH:
        confidence_state = "high"
    elif sentiment_confidence <= CONFIDENCE_LOW:
        confidence_state = "low"
    else:
        confidence_state = "neutral"

    state.sentiment_confidence = sentiment_confidence
    state.confidence_state = confidence_state

    state.record(19, "confidence", {
        "active_count": active_count,
        "input_quality": round(input_quality, 4),
        "score_std": round(score_std, 6),
        "signal_agreement_score": round(signal_agreement_score, 4),
        "data_quality_score": round(data_quality_score, 4),
    }, {
        "sentiment_confidence": sentiment_confidence,
        "confidence_state": confidence_state,
    }, "ok", ts)
    return state


def gate_20_regime(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    bs = state.benchmark_state
    brs = state.breadth_state
    vss = state.vol_structure_state
    brisk = state.benchmark_risk_state

    if bs == "bullish" and brs == "broad" and vss not in ["spiking", "elevated"]:
        regime_state = "trending_bull"
    elif bs == "bearish" and brs == "narrow" and vss in ["elevated", "spiking"]:
        regime_state = "trending_bear"
    elif bs == "bullish" and brs == "narrow":
        regime_state = "choppy_bull"
    elif bs == "bearish" and brs == "broad":
        regime_state = "choppy_bear"
    elif brisk == "drawdown" and vss == "spiking":
        regime_state = "risk_off"
    else:
        regime_state = "indecisive"

    state.regime_state = regime_state

    state.record(20, "regime", {
        "benchmark_state": bs,
        "breadth_state": brs,
        "vol_structure_state": vss,
        "benchmark_risk_state": brisk,
    }, {"regime_state": regime_state}, "ok", ts)
    return state


def gate_21_divergence_warnings(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    warnings_found = []

    # 1. VIX spike divergence
    if state.vol_structure_state == "spiking" and state.price_sentiment_state != "bearish":
        warnings_found.append("vix_spike_divergence")

    # 2. Breadth collapse
    if state.breadth_state == "narrow" and state.price_sentiment_state == "bullish":
        warnings_found.append("breadth_collapse")

    # 3. Extreme fear
    if state.market_sentiment_state == "panic" and state.options_sentiment_state == "fearful":
        warnings_found.append("extreme_fear")

    # 4. Extreme greed
    if state.market_sentiment_state == "euphoric" and state.options_sentiment_state == "complacent":
        warnings_found.append("extreme_greed")

    active_warning_count = len(warnings_found)

    if active_warning_count == 1:
        warning_state = warnings_found[0]
    elif active_warning_count > 1:
        warning_state = "multiple_warnings"
    else:
        warning_state = "none"

    state.active_warnings = warnings_found
    state.active_warning_count = active_warning_count
    state.warning_state = warning_state

    state.record(21, "divergence_warnings", {
        "vol_structure_state": state.vol_structure_state,
        "price_sentiment_state": state.price_sentiment_state,
        "breadth_state": state.breadth_state,
        "market_sentiment_state": state.market_sentiment_state,
        "options_sentiment_state": state.options_sentiment_state,
    }, {
        "active_warnings": warnings_found,
        "active_warning_count": active_warning_count,
        "warning_state": warning_state,
    }, "ok", ts)
    return state


def gate_22_action_classification(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    if state.active_warning_count >= WARNING_COUNT_THRESHOLD:
        classification = "warning_signal"
    elif state.market_sentiment_state == "euphoric":
        classification = "euphoria_warning"
    elif state.market_sentiment_state == "panic":
        classification = "panic_signal"
    elif state.market_sentiment_state == "bullish":
        classification = "bullish_signal"
    elif state.market_sentiment_state == "bearish":
        classification = "bearish_signal"
    else:
        classification = "no_clear_signal"

    state.classification = classification

    state.record(22, "action_classification", {
        "market_sentiment_state": state.market_sentiment_state,
        "active_warning_count": state.active_warning_count,
    }, {"classification": classification}, "ok", ts)
    return state


def gate_23_risk_discounts(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    score = state.raw_sentiment_score
    conf = state.sentiment_confidence
    discounts = []

    # 1. Low trust social
    if state.social_quality_state == "low_trust":
        score *= DISCOUNT_LOW_TRUST_SOCIAL
        conf *= DISCOUNT_LOW_TRUST_SOCIAL
        discounts.append("low_trust_social")

    # 2. Few inputs
    if state.active_input_count < MIN_CONFIDENT_COMPONENTS:
        score *= DISCOUNT_FEW_INPUTS
        conf *= DISCOUNT_FEW_INPUTS
        discounts.append("few_inputs")

    # 3. Active warnings
    if state.active_warning_count > 0:
        score *= DISCOUNT_DIVERGENCE
        conf *= DISCOUNT_DIVERGENCE
        discounts.append("divergence_warning")

    # 4. Drawdown
    if state.benchmark_risk_state == "drawdown":
        score *= DISCOUNT_DRAWDOWN
        conf *= DISCOUNT_DRAWDOWN
        discounts.append("drawdown")

    # 5. Vol instability
    if state.vol_instability_state == "unstable":
        score *= DISCOUNT_VOL_INSTABILITY
        conf *= DISCOUNT_VOL_INSTABILITY
        discounts.append("vol_instability")

    discounted_sentiment_score = round(_clamp(score), 4)
    discounted_confidence = round(max(0.0, min(1.0, conf)), 4)

    state.discounts_applied = discounts
    state.discounted_sentiment_score = discounted_sentiment_score
    state.discounted_confidence = discounted_confidence

    state.record(23, "risk_discounts", {
        "raw_score": round(state.raw_sentiment_score, 6),
        "raw_conf": state.sentiment_confidence,
        "discounts": discounts,
    }, {
        "discounted_sentiment_score": discounted_sentiment_score,
        "discounted_confidence": discounted_confidence,
    }, "ok", ts)
    return state


def gate_24_temporal_persistence(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()
    # TODO:DATA_DEPENDENCY — queries system_log for prior MARKET_SENTIMENT_CLASSIFIED events

    prior_records = []
    try:
        prior_records = _db.get_recent_events("MARKET_SENTIMENT_CLASSIFIED", limit=PERSISTENCE_THRESHOLD + 2) or []
    except Exception as e:
        log.warning(f"gate_24: DB query failed (non-halting): {e}")

    if not prior_records:
        state.persistence_state = "unknown"
        state.sentiment_trend_state = "stable"
        state.record(24, "temporal_persistence", {"prior_records": 0}, {
            "persistence_state": "unknown",
            "sentiment_trend_state": "stable",
        }, "no_history", ts)
        return state

    current_score = state.discounted_sentiment_score
    current_dir = "positive" if current_score > 0 else ("negative" if current_score < 0 else "neutral")

    same_direction_count = 0
    opposite_direction_count = 0

    for rec in prior_records[:PERSISTENCE_THRESHOLD]:
        if not isinstance(rec, dict):
            continue
        prior_score = rec.get("discounted_sentiment_score", rec.get("final_score", 0.0))
        try:
            prior_score = float(prior_score)
        except (TypeError, ValueError):
            continue
        prior_dir = "positive" if prior_score > 0 else ("negative" if prior_score < 0 else "neutral")
        if prior_dir == current_dir:
            same_direction_count += 1
        else:
            opposite_direction_count += 1

    if same_direction_count >= PERSISTENCE_THRESHOLD:
        persistence_state = "persistent"
    elif opposite_direction_count >= FLIP_THRESHOLD:
        persistence_state = "reversal"
    else:
        persistence_state = "unknown"

    if current_dir == "positive" and persistence_state == "persistent":
        sentiment_trend_state = "improving"
    elif current_dir == "negative" and persistence_state == "persistent":
        sentiment_trend_state = "deteriorating"
    elif persistence_state == "reversal":
        sentiment_trend_state = "reversing"
    else:
        sentiment_trend_state = "stable"

    state.persistence_state = persistence_state
    state.sentiment_trend_state = sentiment_trend_state

    state.record(24, "temporal_persistence", {
        "prior_records_count": len(prior_records),
        "same_direction_count": same_direction_count,
        "opposite_direction_count": opposite_direction_count,
        "current_dir": current_dir,
    }, {
        "persistence_state": persistence_state,
        "sentiment_trend_state": sentiment_trend_state,
    }, "ok", ts)
    return state


def gate_25_evaluation_loop(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    prior_score = snapshot.get("prior_sentiment_score", 0.0)
    try:
        prior_score = float(prior_score)
    except (TypeError, ValueError):
        prior_score = 0.0

    score_diff = abs(state.discounted_sentiment_score - prior_score)

    if state.persistence_state == "persistent" and score_diff < IMPROVEMENT_THRESHOLD:
        snapshot_retained = False
        reason = "persistent_no_improvement"
    elif score_diff > DETERIORATION_THRESHOLD:
        snapshot_retained = True
        reason = "significant_change"
    else:
        snapshot_retained = True
        reason = "normal_update"

    state.snapshot_retained = snapshot_retained

    state.record(25, "evaluation_loop", {
        "prior_score": prior_score,
        "current_score": state.discounted_sentiment_score,
        "score_diff": round(score_diff, 6),
        "persistence_state": state.persistence_state,
    }, {"snapshot_retained": snapshot_retained}, reason, ts)
    return state


def gate_26_output_controls(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    if state.classification == "panic_signal":
        output_action = "emit_panic_override"
        output_priority = "market_first"
    elif state.classification == "euphoria_warning":
        output_action = "emit_euphoria_warning"
        output_priority = "market_first"
    elif state.active_warning_count >= WARNING_COUNT_THRESHOLD:
        output_action = "emit_warning_state"
        output_priority = "market_first"
    elif state.confidence_state == "low" or state.classification == "no_clear_signal":
        output_action = "emit_neutral_state"
        output_priority = "article_first"
    else:
        output_action = "emit_sentiment_state"
        output_priority = "article_first"

    state.output_action = output_action
    state.output_priority = output_priority

    state.record(26, "output_controls", {
        "classification": state.classification,
        "active_warning_count": state.active_warning_count,
        "confidence_state": state.confidence_state,
    }, {
        "output_action": output_action,
        "output_priority": output_priority,
    }, "ok", ts)
    return state


def gate_27_final_signal_write(snapshot, state):
    ts = datetime.datetime.utcnow().isoformat()

    # Classification overrides first
    if state.classification == "panic_signal":
        final_market_state = "panic_override"
    elif state.classification == "euphoria_warning":
        final_market_state = "euphoric_warning_override"
    else:
        s = state.discounted_sentiment_score
        if s >= FINAL_STRONG_BULLISH:
            final_market_state = "strong_bullish"
        elif s >= FINAL_MILD_BULLISH:
            final_market_state = "mild_bullish"
        elif s <= FINAL_STRONG_BEARISH:
            final_market_state = "strong_bearish"
        elif s <= FINAL_MILD_BEARISH:
            final_market_state = "mild_bearish"
        else:
            final_market_state = "neutral"

    state.final_market_state = final_market_state

    # Write to scan_log if snapshot retained
    if state.snapshot_retained:
        try:
            _db.write_scan_log(
                ticker="MARKET",
                agent="agent3_sentiment",
                final_state=final_market_state,
                confidence=state.discounted_confidence,
                classification=state.classification,
                ts=ts,
            )
        except AttributeError:
            log.warning("gate_27: _db.write_scan_log not available — TODO:DATA_DEPENDENCY")
        except Exception as e:
            log.warning(f"gate_27: write_scan_log failed (non-halting): {e}")

    # Log to system_log
    try:
        _db.log_event("MARKET_SENTIMENT_CLASSIFIED", {
            "snapshot_hash": state.snapshot_hash,
            "final_market_state": final_market_state,
            "raw_sentiment_score": state.raw_sentiment_score,
            "discounted_sentiment_score": state.discounted_sentiment_score,
            "discounted_confidence": state.discounted_confidence,
            "classification": state.classification,
            "regime_state": state.regime_state,
            "market_sentiment_state": state.market_sentiment_state,
            "ts": ts,
        })
    except AttributeError:
        log.warning("gate_27: _db.log_event not available — TODO:DATA_DEPENDENCY")
    except Exception as e:
        log.warning(f"gate_27: log_event failed (non-halting): {e}")

    state.record(27, "final_signal_write", {
        "discounted_sentiment_score": state.discounted_sentiment_score,
        "classification": state.classification,
        "snapshot_retained": state.snapshot_retained,
    }, {"final_market_state": final_market_state}, "ok", ts)
    return state


# ============================================================
# PIPELINE ORCHESTRATOR — Phase 1
# ============================================================

def run_sentiment_spine(snapshot: dict) -> dict:
    """
    Phase 1: 27-gate deterministic market sentiment spine.
    """
    ts = datetime.datetime.utcnow().isoformat()
    snapshot_hash = hashlib.md5(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    state = SentimentDecisionLog(snapshot_hash=snapshot_hash, session_ts=ts)

    gate_1_system_check(snapshot, state)
    if state.halted:
        return state.to_dict()

    gate_2_input_universe(snapshot, state)
    if state.halted:
        return state.to_dict()

    # All remaining gates accumulate state (non-halting)
    gate_3_benchmark(snapshot, state)
    gate_4_price_action(snapshot, state)
    gate_5_breadth(snapshot, state)
    gate_6_volume(snapshot, state)
    gate_7_volatility(snapshot, state)
    gate_8_options(snapshot, state)
    gate_9_safe_haven(snapshot, state)
    gate_10_credit(snapshot, state)
    gate_11_sector_rotation(snapshot, state)
    gate_12_macro(snapshot, state)
    gate_13_news_sentiment(snapshot, state)
    gate_14_social(snapshot, state)
    gate_15_breadth_price_divergence(snapshot, state)
    gate_16_component_score_collection(snapshot, state)
    gate_17_weighting(snapshot, state)
    gate_18_composite_score(snapshot, state)
    gate_19_confidence(snapshot, state)
    gate_20_regime(snapshot, state)
    gate_21_divergence_warnings(snapshot, state)
    gate_22_action_classification(snapshot, state)
    gate_23_risk_discounts(snapshot, state)
    gate_24_temporal_persistence(snapshot, state)
    gate_25_evaluation_loop(snapshot, state)
    gate_26_output_controls(snapshot, state)
    gate_27_final_signal_write(snapshot, state)

    return state.to_dict()


# ============================================================
# PHASE 2 — Per-Position Cascade Scan
# ============================================================

@dataclass
class ScanDecisionLog:
    ticker: str
    scan_ts: str
    pc_ratio: Optional[float] = None
    pc_ratio_avg: Optional[float] = None
    pc_critical: bool = False
    pc_elevated: bool = False
    insider_sell_count: int = 0
    insider_buy_count: int = 0
    insider_critical: bool = False
    insider_elevated: bool = False
    vol_ratio: float = 0.0
    seller_dominance: float = 0.0
    vol_critical: bool = False
    vol_elevated: bool = False
    critical_count: int = 0
    elevated_count: int = 0
    tier: int = 4
    tier_label: str = "QUIET"
    actor_assessment: str = ""
    summary: str = ""
    decision_log: list = field(default_factory=list)

    def record(self, stage, name, inputs, result, reason_code, ts=None):
        if ts is None:
            ts = datetime.datetime.utcnow().isoformat()
        self.decision_log.append({
            "stage": stage,
            "name": name,
            "inputs": inputs,
            "result": result,
            "reason_code": reason_code,
            "ts": ts,
        })

    def to_dict(self):
        d = {}
        for f_name in self.__dataclass_fields__:
            d[f_name] = getattr(self, f_name)
        return d


def cascade_detect(pc_ratio, pc_avg, insider_sells, insider_buys, vol_ratio, seller_dom):
    """
    Pure function. Returns (critical_count, elevated_count, signals).
    """
    critical_count = 0
    elevated_count = 0
    signals = []

    # Put/call analysis
    if pc_avg and pc_avg > 0:
        pc_mult = pc_ratio / pc_avg if pc_ratio is not None else 1.0
        if pc_ratio is not None and pc_mult > PC_ELEVATED_MULT:
            critical_count += 1
            signals.append({"signal": "pc_elevated_critical", "pc_mult": round(pc_mult, 4), "level": "critical"})
        elif pc_ratio is not None and pc_mult > PC_CRITICAL_MULT:
            elevated_count += 1
            signals.append({"signal": "pc_elevated", "pc_mult": round(pc_mult, 4), "level": "elevated"})

    # Insider selling analysis
    if insider_sells >= INSIDER_SELL_CRITICAL and insider_buys == 0:
        critical_count += 1
        signals.append({"signal": "insider_sell_critical", "sells": insider_sells, "buys": insider_buys, "level": "critical"})
    elif insider_sells - insider_buys > INSIDER_SELL_EDGE:
        elevated_count += 1
        signals.append({"signal": "insider_sell_elevated", "sells": insider_sells, "buys": insider_buys, "level": "elevated"})

    # Volume analysis
    if vol_ratio > VOL_CASCADE_PCT and seller_dom > VOL_SELLER_DOM:
        critical_count += 1
        signals.append({"signal": "vol_cascade_critical", "vol_ratio": round(vol_ratio, 4), "seller_dom": round(seller_dom, 4), "level": "critical"})
    elif vol_ratio > (1.0 + VOL_ELEVATED_PCT):
        elevated_count += 1
        signals.append({"signal": "vol_elevated", "vol_ratio": round(vol_ratio, 4), "level": "elevated"})

    return critical_count, elevated_count, signals


def scan_position(ticker, position_data):
    """
    Phase 2: scan a single position for cascade signals.
    """
    ts = datetime.datetime.utcnow().isoformat()
    scan = ScanDecisionLog(ticker=ticker, scan_ts=ts)

    # Stage 1: Fetch data
    # TODO:DATA_DEPENDENCY — market-wide put/call from snapshot or None
    pc_ratio = position_data.get("put_call_ratio")
    pc_avg = position_data.get("pc_ratio_avg_30d")

    # TODO:DATA_DEPENDENCY — insider data from SEC EDGAR; default to 0 sells/buys
    insider_sells = int(position_data.get("insider_sells", 0))
    insider_buys = int(position_data.get("insider_buys", 0))

    # TODO:DATA_DEPENDENCY — vol data from Finviz; default vol_ratio=1.0, seller_dom=0.5
    vol_ratio = float(position_data.get("vol_ratio", 1.0))
    seller_dom = float(position_data.get("seller_dominance", 0.5))

    scan.pc_ratio = pc_ratio
    scan.pc_ratio_avg = pc_avg
    scan.insider_sell_count = insider_sells
    scan.insider_buy_count = insider_buys
    scan.vol_ratio = vol_ratio
    scan.seller_dominance = seller_dom

    scan.record("stage1", "data_fetch", {
        "ticker": ticker,
        "pc_ratio": pc_ratio,
        "pc_avg": pc_avg,
        "insider_sells": insider_sells,
        "insider_buys": insider_buys,
        "vol_ratio": vol_ratio,
        "seller_dom": seller_dom,
    }, "fetched", "ok", ts)

    # Stage 2: Cascade detect
    critical_count, elevated_count, signals = cascade_detect(
        pc_ratio=pc_ratio,
        pc_avg=pc_avg,
        insider_sells=insider_sells,
        insider_buys=insider_buys,
        vol_ratio=vol_ratio,
        seller_dom=seller_dom,
    )

    scan.critical_count = critical_count
    scan.elevated_count = elevated_count
    scan.pc_critical = any(s["signal"] in ["pc_elevated_critical"] for s in signals)
    scan.pc_elevated = any(s["signal"] == "pc_elevated" for s in signals)
    scan.insider_critical = any(s["signal"] == "insider_sell_critical" for s in signals)
    scan.insider_elevated = any(s["signal"] == "insider_sell_elevated" for s in signals)
    scan.vol_critical = any(s["signal"] == "vol_cascade_critical" for s in signals)
    scan.vol_elevated = any(s["signal"] == "vol_elevated" for s in signals)

    scan.record("stage2", "cascade_detect", {
        "signals": signals,
    }, {
        "critical_count": critical_count,
        "elevated_count": elevated_count,
    }, "ok", ts)

    # Stage 3: Tier classification
    if critical_count >= 2:
        tier = 1
        tier_label = "CRITICAL"
    elif critical_count == 1 or elevated_count >= 2:
        tier = 2
        tier_label = "ELEVATED"
    elif critical_count == 0 and elevated_count == 1:
        tier = 3
        tier_label = "NEUTRAL"
    else:
        tier = 4
        tier_label = "QUIET"

    scan.tier = tier
    scan.tier_label = tier_label

    if tier_label == "CRITICAL":
        actor_assessment = "cascade_likely"
    elif tier_label == "ELEVATED":
        actor_assessment = "elevated_risk"
    else:
        actor_assessment = "isolated"

    scan.actor_assessment = actor_assessment
    scan.summary = (
        f"{ticker}: tier={tier} ({tier_label}), "
        f"critical={critical_count}, elevated={elevated_count}, "
        f"assessment={actor_assessment}"
    )

    scan.record("stage3", "tier_classification", {
        "critical_count": critical_count,
        "elevated_count": elevated_count,
    }, {
        "tier": tier,
        "tier_label": tier_label,
        "actor_assessment": actor_assessment,
    }, "ok", ts)

    # Stage 4: Write to scan_log (Tier 1 and 2 only — caller filters)
    if tier <= 2:
        try:
            _db.write_scan_log(
                ticker=ticker,
                agent="agent3_sentiment_cascade",
                final_state=tier_label,
                confidence=None,
                classification=actor_assessment,
                ts=ts,
            )
        except AttributeError:
            log.warning(f"scan_position [{ticker}]: _db.write_scan_log not available — TODO:DATA_DEPENDENCY")
        except Exception as e:
            log.warning(f"scan_position [{ticker}]: write_scan_log failed (non-halting): {e}")

    scan.record("stage4", "scan_log_write", {
        "ticker": ticker,
        "tier": tier,
    }, {"written": tier <= 2}, "ok", ts)

    return scan.to_dict()


def run_cascade_scan(open_positions) -> list:
    """
    Phase 2: Scan all open positions for cascade signals.
    Only writes Tier 1 and Tier 2 to scan_log (handled within scan_position).
    """
    results = []
    for position in open_positions:
        ticker = position.get("ticker", "UNKNOWN") if isinstance(position, dict) else str(position)
        position_data = position if isinstance(position, dict) else {}
        try:
            result = scan_position(ticker, position_data)
            results.append(result)
        except Exception as e:
            log.error(f"run_cascade_scan: scan_position failed for {ticker}: {e}")
            results.append({"ticker": ticker, "error": str(e)})
    return results


# ============================================================
# PHASE 3 — Pre-Trade Queue Check
# ============================================================

def check_signal_pulse(signal_record) -> dict:
    """
    Phase 3: Pre-trade check. Same cascade logic as Phase 2 but
    insider lookback = INSIDER_LOOKBACK_DAYS_PHASE3 (7 days).
    """
    ts = datetime.datetime.utcnow().isoformat()
    ticker = signal_record.get("ticker", "UNKNOWN") if isinstance(signal_record, dict) else str(signal_record)
    signal_id = signal_record.get("signal_id") if isinstance(signal_record, dict) else None

    # Use signal_record as position data but with phase 3 lookback
    position_data = dict(signal_record) if isinstance(signal_record, dict) else {}
    position_data["_insider_lookback_days"] = INSIDER_LOOKBACK_DAYS_PHASE3

    scan_result = scan_position(ticker, position_data)

    tier = scan_result.get("tier", 4)
    tier_label = scan_result.get("tier_label", "QUIET")
    summary = scan_result.get("summary", "")

    # Annotate signal pulse in DB
    if signal_id is not None:
        try:
            _db.annotate_signal_pulse(signal_id, tier, summary)
        except AttributeError:
            log.warning(f"check_signal_pulse [{ticker}]: _db.annotate_signal_pulse not available — TODO:DATA_DEPENDENCY")
        except Exception as e:
            log.warning(f"check_signal_pulse [{ticker}]: annotate_signal_pulse failed (non-halting): {e}")

    return {
        "ticker": ticker,
        "signal_id": signal_id,
        "tier": tier,
        "tier_label": tier_label,
        "summary": summary,
        "scan_detail": scan_result,
        "ts": ts,
    }


def run_queue_check(queued_signals) -> list:
    """
    Phase 3: Run pre-trade queue check for all queued signals.
    """
    results = []
    for signal in queued_signals:
        try:
            result = check_signal_pulse(signal)
            results.append(result)
        except Exception as e:
            ticker = signal.get("ticker", "UNKNOWN") if isinstance(signal, dict) else str(signal)
            log.error(f"run_queue_check: check_signal_pulse failed for {ticker}: {e}")
            results.append({"ticker": ticker, "error": str(e)})
    return results


# ============================================================
# TOP-LEVEL RUN FUNCTION
# ============================================================

def run_agent3(snapshot: dict, open_positions: list = None, queued_signals: list = None) -> dict:
    """
    Main entry point for Agent 3.
    Phase 1: Market sentiment spine (always runs if snapshot provided)
    Phase 2: Per-position cascade scan (runs if open_positions provided)
    Phase 3: Pre-trade queue check (runs if queued_signals provided)
    """
    result = {
        "agent": "agent3_sentiment",
        "agent_version": AGENT_VERSION,
        "ts": datetime.datetime.utcnow().isoformat(),
        "phase1": None,
        "phase2": [],
        "phase3": [],
    }

    if snapshot is not None:
        result["phase1"] = run_sentiment_spine(snapshot)

    if open_positions:
        result["phase2"] = run_cascade_scan(open_positions)

    if queued_signals:
        result["phase3"] = run_queue_check(queued_signals)

    return result


# ============================================================
# CLI ENTRY POINT
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent 3 — SentimentAgent")
    parser.add_argument("--test", action="store_true", help="Run smoke test")
    args = parser.parse_args()

    if args.test:
        # Smoke test: minimal valid snapshot
        test_snapshot = {
            "ts": datetime.datetime.utcnow().isoformat(),
            "spy_bars": [{"close": 500.0 + i * 0.5, "volume": 50000000} for i in range(55)],
            "sector_bars": {
                "XLK":  [500.0 + i       for i in range(10)],
                "XLF":  [40.0  + i * 0.1 for i in range(10)],
                "XLE":  [80.0  - i * 0.2 for i in range(10)],
                "XLV":  [130.0 + i * 0.3 for i in range(10)],
            },
            "vix_level": 18.0,
            "put_call_ratio": 0.85,
            "macro_news": [],
            "news_scores": [],
        }
        result = run_agent3(test_snapshot, open_positions=[], queued_signals=[])
        print(json.dumps(result["phase1"], indent=2, default=str))
        phase1 = result["phase1"]
        print(f"\nSmoke test results:")
        print(f"  halted: {phase1.get('halted')}")
        print(f"  final_market_state: {phase1.get('final_market_state')}")
        print(f"  market_sentiment_state: {phase1.get('market_sentiment_state')}")
        print(f"  regime_state: {phase1.get('regime_state')}")
        print(f"  classification: {phase1.get('classification')}")
        print(f"  discounted_sentiment_score: {phase1.get('discounted_sentiment_score')}")
        print(f"  discounted_confidence: {phase1.get('discounted_confidence')}")
