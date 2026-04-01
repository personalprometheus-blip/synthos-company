#!/usr/bin/env python3
"""
news_agent.py — Agent 7.2
News signal classification spine. 8-gate deterministic, rule-based spine.

Classifies structured news data into a signal category. Handles promoted
rumor context as an optional supplement. Feeds Market Sentiment Agent and
Master Market-State Aggregator.

Gates 1-2 : System validation and input merge.
Gates 3-5 : Source credibility, sentiment direction, confirmation state.
Gates 6-7 : Market relevance filtering and signal classification.
Gate  8   : Confidence scoring.
"""

import sys
import os
import json
import logging
import argparse
import datetime
from typing import Any

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("news_agent")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

# ============================================================
# CONSTANTS
# ============================================================

AGENT_VERSION = "1.0.0"

# Gate 1 — System Gate
MAX_SNAPSHOT_AGE_MINUTES = 60

# Gate 3 — Source Credibility
MIN_SOURCE_CREDIBILITY       = 0.30
HIGH_CREDIBILITY_THRESHOLD   = 0.70

# Gate 4 — Sentiment Direction
BULLISH_SENTIMENT_THRESHOLD  = 0.60
BEARISH_SENTIMENT_THRESHOLD  = -0.60
RELATIVE_ALPHA_THRESHOLD     = 0.40
BENCHMARK_REGIME_THRESHOLD   = 0.30

# Gate 5 — Confirmation State
CONFIRMATION_STRONG_THRESHOLD   = 3
CONTRADICTION_RATIO_THRESHOLD   = 0.40
FREEZE_CONTRADICTION_RATIO      = 0.60

# Gate 7 — Signal Classification
# (classification precedence: freeze > bearish > bullish > relative_alpha >
#  benchmark_regime > watch_only > provisional_watch > ignore)

# Gate 8 — Confidence Scoring
HIGH_CONFIDENCE_THRESHOLD    = 0.70
LOW_CONFIDENCE_THRESHOLD     = 0.35

# Input merge weights
PROMOTED_RUMOR_WEIGHT        = 0.80
NEWS_BASE_WEIGHT             = 1.0


# ============================================================
# NEWS DECISION LOG
# ============================================================

class NewsDecisionLog:
    """
    Accumulates all gate records produced during a single run_agent() run.
    Serialises to JSON-compatible dict via to_dict().
    """

    def __init__(self, run_id: str, timestamp: str):
        self.run_id    = run_id
        self.timestamp = timestamp
        self.records: list = []

        # Halt state
        self.halted      = False
        self.halt_reason = None

        # Intermediate states set by gates
        self.source_credibility_state  = None   # high | low | discounted | absent
        self.sentiment_state           = None   # bullish | bearish | neutral
        self.confirmation_state        = None   # confirmed | provisional | contradictory | unresolved
        self.market_relevance_state    = None   # high | low | absent

        # Merged input (produced by gate_2)
        self._merged: dict = {}

        # Output fields
        self.classification     = None   # see 8 possible values
        self.overall_confidence = None   # float [0.0, 1.0]

    # ----------------------------------------------------------
    # Record helpers
    # ----------------------------------------------------------

    def add_record(self, gate: int, name: str, inputs: dict,
                   result: str, reason_code: str):
        self.records.append({
            "gate":        gate,
            "name":        name,
            "inputs":      inputs,
            "result":      result,
            "reason_code": reason_code,
            "ts":          datetime.datetime.utcnow().isoformat(),
        })

    # ----------------------------------------------------------
    # Halt helper — returns to_dict() immediately
    # ----------------------------------------------------------

    def _halt(self, gate: int, reason_code: str, inputs: dict) -> dict:
        self.halted      = True
        self.halt_reason = reason_code
        self.add_record(gate, "HALT", inputs, "halted", reason_code)
        return self.to_dict()

    # ----------------------------------------------------------
    # Serialisation
    # ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "run_id":                   self.run_id,
            "timestamp":                self.timestamp,
            "agent_version":            AGENT_VERSION,
            "halted":                   self.halted,
            "halt_reason":              self.halt_reason,
            "source_credibility_state": self.source_credibility_state,
            "sentiment_state":          self.sentiment_state,
            "confirmation_state":       self.confirmation_state,
            "market_relevance_state":   self.market_relevance_state,
            "classification":           self.classification,
            "overall_confidence":       self.overall_confidence,
            "decision_log":             self.records,
        }


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system(snapshot: dict, dlog: NewsDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates timestamp, checks snapshot age, and confirms at least one
    input (normalized_news or promoted_rumor_context) is present.

    Halts with:
      - no_news_input      : both inputs are absent/null
      - stale_snapshot     : timestamp older than MAX_SNAPSHOT_AGE_MINUTES
      - reject_snapshot    : timestamp missing or unparseable
    Returns True if run should continue, False if halted.
    """
    timestamp_str       = snapshot.get("timestamp")
    normalized_news     = snapshot.get("normalized_news")
    promoted_rumor      = snapshot.get("promoted_rumor_context")

    # Check 1: At least one input present
    if not normalized_news and not promoted_rumor:
        dlog._halt(1, "no_news_input",
                   {"normalized_news": None, "promoted_rumor_context": None})
        return False

    # Check 2: Timestamp present
    if timestamp_str is None:
        dlog._halt(1, "reject_snapshot", {"timestamp": None})
        return False

    # Check 3: Timestamp parseable and fresh
    try:
        snap_time   = datetime.datetime.fromisoformat(timestamp_str)
        now_utc     = datetime.datetime.utcnow()
        # Handle timezone-aware timestamps gracefully
        if snap_time.tzinfo is not None:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
        age_minutes = (now_utc - snap_time).total_seconds() / 60.0
        if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
            dlog._halt(1, "stale_snapshot",
                       {"age_minutes": round(age_minutes, 1),
                        "max_snapshot_age_minutes": MAX_SNAPSHOT_AGE_MINUTES})
            return False
    except (ValueError, TypeError):
        dlog._halt(1, "reject_snapshot",
                   {"timestamp": timestamp_str, "error": "unparseable_timestamp"})
        return False

    has_news   = normalized_news is not None
    has_rumor  = promoted_rumor is not None

    dlog.add_record(1, "system_gate",
                    {"has_normalized_news": has_news,
                     "has_promoted_rumor": has_rumor,
                     "age_minutes": round(age_minutes, 1)},
                    "pass", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — INPUT MERGE
# ============================================================

def gate_2_input_merge(snapshot: dict, dlog: NewsDecisionLog):
    """
    GATE 2 — INPUT MERGE
    If both inputs present: merge, with promoted_rumor boosting confidence
    field values proportionally.
    If only promoted_rumor: treat as news input with PROMOTED_RUMOR_WEIGHT
    scaling applied to credibility and relevance.
    If only normalized_news: pass through unchanged (NEWS_BASE_WEIGHT = 1.0).
    Stores merged result in dlog._merged.
    """
    normalized_news  = snapshot.get("normalized_news") or {}
    promoted_rumor   = snapshot.get("promoted_rumor_context") or {}

    has_news  = bool(snapshot.get("normalized_news"))
    has_rumor = bool(snapshot.get("promoted_rumor_context"))

    if has_news and has_rumor:
        # Both present — merge with rumor boost
        merged = dict(normalized_news)

        # Boost headline_sentiment toward the rumor's implied direction if available
        rumor_sentiment = promoted_rumor.get("headline_sentiment")
        base_sentiment  = normalized_news.get("headline_sentiment", 0.0)
        if rumor_sentiment is not None:
            merged["headline_sentiment"] = (
                NEWS_BASE_WEIGHT * base_sentiment
                + PROMOTED_RUMOR_WEIGHT * rumor_sentiment
            ) / (NEWS_BASE_WEIGHT + PROMOTED_RUMOR_WEIGHT)

        # Boost source_credibility if rumor carries one
        rumor_cred = promoted_rumor.get("source_credibility")
        base_cred  = normalized_news.get("source_credibility", 0.0)
        if rumor_cred is not None:
            merged["source_credibility"] = max(base_cred,
                                               PROMOTED_RUMOR_WEIGHT * rumor_cred)

        # Carry forward any confirmation count from rumor
        base_conf  = normalized_news.get("confirmation_count", 0)
        rumor_conf = promoted_rumor.get("confirmation_count", 0)
        merged["confirmation_count"] = base_conf + rumor_conf

        # Merge market_relevance (take higher of the two)
        rumor_rel = promoted_rumor.get("market_relevance")
        base_rel  = normalized_news.get("market_relevance", 0.0)
        if rumor_rel is not None:
            merged["market_relevance"] = max(base_rel, rumor_rel)

        dlog._merged = merged
        dlog.add_record(2, "input_merge",
                        {"has_normalized_news": True, "has_promoted_rumor": True,
                         "promoted_rumor_weight": PROMOTED_RUMOR_WEIGHT},
                        "merged_with_boost", "both_inputs_merged_rumor_boosted")

    elif has_rumor and not has_news:
        # Only promoted rumor — treat as news with PROMOTED_RUMOR_WEIGHT applied
        merged = {
            "headline_sentiment":  promoted_rumor.get("headline_sentiment", 0.0),
            "article_count":       promoted_rumor.get("article_count", 1),
            "source_credibility":  PROMOTED_RUMOR_WEIGHT * promoted_rumor.get("source_credibility", 0.0),
            "market_relevance":    PROMOTED_RUMOR_WEIGHT * promoted_rumor.get("market_relevance", 0.0),
            "confirmation_count":  promoted_rumor.get("confirmation_count", 0),
            "contradiction_count": promoted_rumor.get("contradiction_count", 0),
        }
        dlog._merged = merged
        dlog.add_record(2, "input_merge",
                        {"has_normalized_news": False, "has_promoted_rumor": True,
                         "promoted_rumor_weight": PROMOTED_RUMOR_WEIGHT},
                        "rumor_as_news", "only_promoted_rumor_weight_applied")

    else:
        # Only normalized_news
        dlog._merged = dict(normalized_news)
        dlog.add_record(2, "input_merge",
                        {"has_normalized_news": True, "has_promoted_rumor": False,
                         "news_base_weight": NEWS_BASE_WEIGHT},
                        "news_only", "normalized_news_passed_through")


# ============================================================
# GATE 3 — SOURCE CREDIBILITY
# ============================================================

def gate_3_source_credibility(dlog: NewsDecisionLog):
    """
    GATE 3 — SOURCE CREDIBILITY
    Evaluates source_credibility from the merged input.
    Sets dlog.source_credibility_state:
      - high       : >= HIGH_CREDIBILITY_THRESHOLD
      - discounted : >= MIN_SOURCE_CREDIBILITY but < HIGH_CREDIBILITY_THRESHOLD
      - low        : < MIN_SOURCE_CREDIBILITY (credibility penalised in gate 8)
      - absent     : field not present
    """
    credibility = dlog._merged.get("source_credibility")

    if credibility is None:
        dlog.source_credibility_state = "absent"
        dlog.add_record(3, "source_credibility",
                        {"source_credibility": None},
                        "absent", "source_credibility_field_missing")
        return

    credibility = float(credibility)

    if credibility >= HIGH_CREDIBILITY_THRESHOLD:
        dlog.source_credibility_state = "high"
        dlog.add_record(3, "source_credibility",
                        {"source_credibility": credibility,
                         "high_threshold": HIGH_CREDIBILITY_THRESHOLD},
                        "high", "credibility_above_high_threshold")

    elif credibility >= MIN_SOURCE_CREDIBILITY:
        dlog.source_credibility_state = "discounted"
        dlog.add_record(3, "source_credibility",
                        {"source_credibility": credibility,
                         "min_threshold": MIN_SOURCE_CREDIBILITY,
                         "high_threshold": HIGH_CREDIBILITY_THRESHOLD},
                        "discounted", "credibility_below_high_threshold")

    else:
        dlog.source_credibility_state = "low"
        dlog.add_record(3, "source_credibility",
                        {"source_credibility": credibility,
                         "min_threshold": MIN_SOURCE_CREDIBILITY},
                        "low", "credibility_below_minimum_threshold")


# ============================================================
# GATE 4 — SENTIMENT DIRECTION
# ============================================================

def gate_4_sentiment_direction(dlog: NewsDecisionLog):
    """
    GATE 4 — SENTIMENT DIRECTION
    Classifies headline_sentiment into a directional bucket.
    Sets dlog.sentiment_state:
      - bullish  : >= BULLISH_SENTIMENT_THRESHOLD
      - bearish  : <= BEARISH_SENTIMENT_THRESHOLD
      - neutral  : between the two thresholds
      - absent   : field not present
    """
    sentiment = dlog._merged.get("headline_sentiment")

    if sentiment is None:
        dlog.sentiment_state = "absent"
        dlog.add_record(4, "sentiment_direction",
                        {"headline_sentiment": None},
                        "absent", "headline_sentiment_field_missing")
        return

    sentiment = float(sentiment)

    if sentiment >= BULLISH_SENTIMENT_THRESHOLD:
        dlog.sentiment_state = "bullish"
        dlog.add_record(4, "sentiment_direction",
                        {"headline_sentiment": sentiment,
                         "threshold": BULLISH_SENTIMENT_THRESHOLD},
                        "bullish", "sentiment_above_bullish_threshold")

    elif sentiment <= BEARISH_SENTIMENT_THRESHOLD:
        dlog.sentiment_state = "bearish"
        dlog.add_record(4, "sentiment_direction",
                        {"headline_sentiment": sentiment,
                         "threshold": BEARISH_SENTIMENT_THRESHOLD},
                        "bearish", "sentiment_below_bearish_threshold")

    else:
        dlog.sentiment_state = "neutral"
        dlog.add_record(4, "sentiment_direction",
                        {"headline_sentiment": sentiment,
                         "bullish_threshold": BULLISH_SENTIMENT_THRESHOLD,
                         "bearish_threshold": BEARISH_SENTIMENT_THRESHOLD},
                        "neutral", "sentiment_within_neutral_band")


# ============================================================
# GATE 5 — CONFIRMATION STATE
# ============================================================

def gate_5_confirmation_state(dlog: NewsDecisionLog):
    """
    GATE 5 — CONFIRMATION STATE
    Computes confirmation_state from confirmation_count vs contradiction_count.

    Rules (in precedence order):
      - contradictory  : contradiction ratio >= FREEZE_CONTRADICTION_RATIO
      - contradictory  : contradiction ratio >= CONTRADICTION_RATIO_THRESHOLD
                         (and < FREEZE — freeze is handled in gate 7 via ratio)
      - confirmed      : confirmation_count >= CONFIRMATION_STRONG_THRESHOLD
                         and contradiction ratio < CONTRADICTION_RATIO_THRESHOLD
      - provisional    : confirmation_count > 0
                         and contradiction ratio < CONTRADICTION_RATIO_THRESHOLD
      - unresolved     : no confirmations and no contradictions (or fields absent)

    Note: the raw contradiction ratio is stored on dlog._merged for gate_7 use.
    """
    conf_count  = int(dlog._merged.get("confirmation_count",  0) or 0)
    contra_count = int(dlog._merged.get("contradiction_count", 0) or 0)

    total = conf_count + contra_count
    contradiction_ratio = (contra_count / total) if total > 0 else 0.0

    # Stash ratio for gate_7 access
    dlog._merged["_contradiction_ratio"] = contradiction_ratio

    if contradiction_ratio >= FREEZE_CONTRADICTION_RATIO:
        dlog.confirmation_state = "contradictory"
        dlog.add_record(5, "confirmation_state",
                        {"confirmation_count": conf_count,
                         "contradiction_count": contra_count,
                         "contradiction_ratio": round(contradiction_ratio, 4),
                         "freeze_threshold": FREEZE_CONTRADICTION_RATIO},
                        "contradictory", "contradiction_ratio_at_freeze_level")

    elif contradiction_ratio >= CONTRADICTION_RATIO_THRESHOLD:
        dlog.confirmation_state = "contradictory"
        dlog.add_record(5, "confirmation_state",
                        {"confirmation_count": conf_count,
                         "contradiction_count": contra_count,
                         "contradiction_ratio": round(contradiction_ratio, 4),
                         "threshold": CONTRADICTION_RATIO_THRESHOLD},
                        "contradictory", "contradiction_ratio_above_threshold")

    elif conf_count >= CONFIRMATION_STRONG_THRESHOLD:
        dlog.confirmation_state = "confirmed"
        dlog.add_record(5, "confirmation_state",
                        {"confirmation_count": conf_count,
                         "contradiction_count": contra_count,
                         "contradiction_ratio": round(contradiction_ratio, 4),
                         "strong_threshold": CONFIRMATION_STRONG_THRESHOLD},
                        "confirmed", "strong_confirmation_count_met")

    elif conf_count > 0:
        dlog.confirmation_state = "provisional"
        dlog.add_record(5, "confirmation_state",
                        {"confirmation_count": conf_count,
                         "contradiction_count": contra_count,
                         "contradiction_ratio": round(contradiction_ratio, 4)},
                        "provisional", "partial_confirmation_present")

    else:
        dlog.confirmation_state = "unresolved"
        dlog.add_record(5, "confirmation_state",
                        {"confirmation_count": conf_count,
                         "contradiction_count": contra_count},
                        "unresolved", "no_confirmations_present")


# ============================================================
# GATE 6 — MARKET RELEVANCE
# ============================================================

def gate_6_market_relevance(dlog: NewsDecisionLog):
    """
    GATE 6 — MARKET RELEVANCE
    Checks market_relevance field from merged input.
    Sets dlog.market_relevance_state:
      - high   : market_relevance >= BENCHMARK_REGIME_THRESHOLD
      - low    : market_relevance present but below threshold
      - absent : field not present

    A low or absent relevance score will constrain signal classification in
    gate_7 toward watch_only / provisional_watch.
    """
    relevance = dlog._merged.get("market_relevance")

    if relevance is None:
        dlog.market_relevance_state = "absent"
        dlog.add_record(6, "market_relevance",
                        {"market_relevance": None},
                        "absent", "market_relevance_field_missing")
        return

    relevance = float(relevance)

    if relevance >= BENCHMARK_REGIME_THRESHOLD:
        dlog.market_relevance_state = "high"
        dlog.add_record(6, "market_relevance",
                        {"market_relevance": relevance,
                         "threshold": BENCHMARK_REGIME_THRESHOLD},
                        "high", "market_relevance_above_threshold")
    else:
        dlog.market_relevance_state = "low"
        dlog.add_record(6, "market_relevance",
                        {"market_relevance": relevance,
                         "threshold": BENCHMARK_REGIME_THRESHOLD},
                        "low", "market_relevance_below_threshold")


# ============================================================
# GATE 7 — SIGNAL CLASSIFICATION
# ============================================================

def gate_7_signal_classification(dlog: NewsDecisionLog):
    """
    GATE 7 — SIGNAL CLASSIFICATION
    Applies precedence rules to produce the final classification.

    Precedence (highest to lowest):
      freeze            > bearish_signal     > bullish_signal
      relative_alpha_signal > benchmark_regime_signal
      watch_only        > provisional_watch  > ignore

    Rules:
      freeze                : contradictory state AND contradiction_ratio >= FREEZE_CONTRADICTION_RATIO
      bearish_signal        : sentiment=bearish, relevance=high, confirmation != unresolved, cred != low
      bullish_signal        : sentiment=bullish, relevance=high, confirmation=confirmed|provisional, cred != low
      relative_alpha_signal : neutral sentiment with abs(headline_sentiment) >= RELATIVE_ALPHA_THRESHOLD
                              AND relevance=high, confirmation != unresolved
      benchmark_regime_signal: neutral sentiment with abs(headline_sentiment) >= BENCHMARK_REGIME_THRESHOLD
                              AND relevance=high
      watch_only            : relevance=low and confirmation != unresolved
      provisional_watch     : relevance=absent OR confirmation=unresolved OR credibility=low
      ignore                : fallthrough
    """
    sentiment      = dlog.sentiment_state
    conf_state     = dlog.confirmation_state
    relevance      = dlog.market_relevance_state
    cred_state     = dlog.source_credibility_state
    contra_ratio   = dlog._merged.get("_contradiction_ratio", 0.0)
    hs             = float(dlog._merged.get("headline_sentiment", 0.0) or 0.0)

    # --- freeze ---
    if conf_state == "contradictory" and contra_ratio >= FREEZE_CONTRADICTION_RATIO:
        dlog.classification = "freeze"
        dlog.add_record(7, "signal_classification",
                        {"sentiment_state": sentiment,
                         "confirmation_state": conf_state,
                         "contradiction_ratio": round(contra_ratio, 4),
                         "freeze_threshold": FREEZE_CONTRADICTION_RATIO},
                        "freeze", "freeze_contradiction_ratio_met")
        return

    # --- bearish_signal ---
    if (sentiment == "bearish"
            and relevance == "high"
            and conf_state != "unresolved"
            and cred_state != "low"):
        dlog.classification = "bearish_signal"
        dlog.add_record(7, "signal_classification",
                        {"sentiment_state": sentiment, "relevance": relevance,
                         "confirmation_state": conf_state, "credibility_state": cred_state},
                        "bearish_signal", "bearish_conditions_met")
        return

    # --- bullish_signal ---
    if (sentiment == "bullish"
            and relevance == "high"
            and conf_state in ("confirmed", "provisional")
            and cred_state != "low"):
        dlog.classification = "bullish_signal"
        dlog.add_record(7, "signal_classification",
                        {"sentiment_state": sentiment, "relevance": relevance,
                         "confirmation_state": conf_state, "credibility_state": cred_state},
                        "bullish_signal", "bullish_conditions_met")
        return

    # --- relative_alpha_signal ---
    if (sentiment == "neutral"
            and abs(hs) >= RELATIVE_ALPHA_THRESHOLD
            and relevance == "high"
            and conf_state != "unresolved"):
        dlog.classification = "relative_alpha_signal"
        dlog.add_record(7, "signal_classification",
                        {"sentiment_state": sentiment,
                         "headline_sentiment": hs,
                         "relative_alpha_threshold": RELATIVE_ALPHA_THRESHOLD,
                         "relevance": relevance,
                         "confirmation_state": conf_state},
                        "relative_alpha_signal", "relative_alpha_threshold_met")
        return

    # --- benchmark_regime_signal ---
    if (sentiment == "neutral"
            and abs(hs) >= BENCHMARK_REGIME_THRESHOLD
            and relevance == "high"):
        dlog.classification = "benchmark_regime_signal"
        dlog.add_record(7, "signal_classification",
                        {"sentiment_state": sentiment,
                         "headline_sentiment": hs,
                         "benchmark_regime_threshold": BENCHMARK_REGIME_THRESHOLD,
                         "relevance": relevance},
                        "benchmark_regime_signal", "benchmark_regime_threshold_met")
        return

    # --- watch_only ---
    if relevance == "low" and conf_state != "unresolved":
        dlog.classification = "watch_only"
        dlog.add_record(7, "signal_classification",
                        {"relevance": relevance, "confirmation_state": conf_state},
                        "watch_only", "low_relevance_with_partial_confirmation")
        return

    # --- provisional_watch ---
    if (relevance in ("absent", "low")
            or conf_state == "unresolved"
            or cred_state == "low"):
        dlog.classification = "provisional_watch"
        dlog.add_record(7, "signal_classification",
                        {"relevance": relevance,
                         "confirmation_state": conf_state,
                         "credibility_state": cred_state},
                        "provisional_watch", "insufficient_quality_for_actionable_signal")
        return

    # --- ignore (fallthrough) ---
    dlog.classification = "ignore"
    dlog.add_record(7, "signal_classification",
                    {"sentiment_state": sentiment, "relevance": relevance,
                     "confirmation_state": conf_state, "credibility_state": cred_state},
                    "ignore", "no_classification_criteria_met")


# ============================================================
# GATE 8 — CONFIDENCE SCORING
# ============================================================

def gate_8_confidence_scoring(dlog: NewsDecisionLog):
    """
    GATE 8 — CONFIDENCE SCORING
    Computes overall_confidence = credibility_factor * relevance_factor
                                  * confirmation_factor.

    Factor mappings:
      credibility_factor:
        high       -> 1.0
        discounted -> 0.65
        low        -> 0.30
        absent     -> 0.50 (neutral assumption)

      relevance_factor  (raw market_relevance clamped [0,1] if available):
        high       -> market_relevance value (or 1.0 if field absent but state=high)
        low        -> market_relevance value (or 0.20 if field absent but state=low)
        absent     -> 0.50 (neutral assumption)

      confirmation_factor:
        confirmed      -> 1.0
        provisional    -> 0.65
        contradictory  -> 0.20
        unresolved     -> 0.40

    Result clamped to [0.0, 1.0].
    """
    cred_map = {
        "high":       1.0,
        "discounted": 0.65,
        "low":        0.30,
        "absent":     0.50,
    }
    conf_map = {
        "confirmed":     1.0,
        "provisional":   0.65,
        "contradictory": 0.20,
        "unresolved":    0.40,
    }

    credibility_factor  = cred_map.get(dlog.source_credibility_state, 0.50)
    confirmation_factor = conf_map.get(dlog.confirmation_state, 0.40)

    # Relevance factor: use raw value when available, otherwise use state-based default
    raw_relevance = dlog._merged.get("market_relevance")
    if raw_relevance is not None:
        relevance_factor = float(max(0.0, min(1.0, raw_relevance)))
    elif dlog.market_relevance_state == "high":
        relevance_factor = 1.0
    elif dlog.market_relevance_state == "low":
        relevance_factor = 0.20
    else:
        relevance_factor = 0.50

    raw_confidence = credibility_factor * relevance_factor * confirmation_factor
    overall_confidence = round(max(0.0, min(1.0, raw_confidence)), 4)

    dlog.overall_confidence = overall_confidence
    dlog.add_record(8, "confidence_scoring",
                    {"credibility_factor":   credibility_factor,
                     "relevance_factor":     relevance_factor,
                     "confirmation_factor":  confirmation_factor,
                     "raw_confidence":       round(raw_confidence, 4),
                     "high_conf_threshold":  HIGH_CONFIDENCE_THRESHOLD,
                     "low_conf_threshold":   LOW_CONFIDENCE_THRESHOLD},
                    str(overall_confidence),
                    "confidence_computed")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def run_agent(snapshot: dict) -> dict:
    """
    Orchestrates the 8-gate news signal classification pipeline.

    Args:
        snapshot: dict with keys:
            run_id                  — identifier for this run (required)
            timestamp               — ISO UTC timestamp (required)
            normalized_news         — dict of normalized news fields (optional)
            promoted_rumor_context  — dict of promoted rumor fields (optional)
            (at least one of normalized_news / promoted_rumor_context must be present)

    Returns:
        Complete decision log dict produced by NewsDecisionLog.to_dict().
    """
    run_id    = snapshot.get("run_id", "unknown")
    timestamp = snapshot.get("timestamp", "")
    dlog      = NewsDecisionLog(run_id=run_id, timestamp=timestamp)

    # Gate 1: System gate (halting)
    if not gate_1_system(snapshot, dlog):
        return dlog.to_dict()

    # Gate 2: Input merge
    gate_2_input_merge(snapshot, dlog)

    # Gate 3: Source credibility
    gate_3_source_credibility(dlog)

    # Gate 4: Sentiment direction
    gate_4_sentiment_direction(dlog)

    # Gate 5: Confirmation state
    gate_5_confirmation_state(dlog)

    # Gate 6: Market relevance
    gate_6_market_relevance(dlog)

    # Gate 7: Signal classification
    gate_7_signal_classification(dlog)

    # Gate 8: Confidence scoring
    gate_8_confidence_scoring(dlog)

    return dlog.to_dict()


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_news_log(result: dict):
    """Writes the news agent decision log to the database event log."""
    _db.log_event(
        event_type="news_agent_snapshot",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity news classifications.
    """
    cls    = result.get("classification")
    run_id = result.get("run_id")
    conf   = result.get("overall_confidence", 0.0) or 0.0

    if cls == "freeze":
        _db.post_suggestion(
            agent="news_agent",
            category="signal_quality",
            title=f"News freeze: high contradiction ratio — run {run_id}",
            description=(
                "News agent detected a freeze-level contradiction ratio. "
                "Strongly contradictory news signals present. "
                "Downstream agents should treat this input as unreliable."
            ),
            risk_level="HIGH",
            affected_component="news_agent",
            complexity="SIMPLE",
            root_cause="contradiction_ratio_at_freeze_threshold",
            solution_approach="Review incoming news sources for conflicting signals before re-processing.",
        )
        log.warning("ESCALATE: freeze classification for run %s", run_id)

    elif cls == "bearish_signal" and conf >= HIGH_CONFIDENCE_THRESHOLD:
        _db.post_suggestion(
            agent="news_agent",
            category="signal_quality",
            title=f"High-confidence bearish news signal — run {run_id}",
            description=(
                f"News agent classified a high-confidence bearish signal "
                f"(confidence={conf:.2f}). Review risk exposure."
            ),
            risk_level="MEDIUM",
            affected_component="news_agent",
            complexity="SIMPLE",
            root_cause="bearish_signal_high_confidence",
            solution_approach="Cross-reference with macro regime and market state agents.",
        )
        log.warning("ESCALATE: high-confidence bearish_signal for run %s", run_id)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="News Agent 7.2 — 8-gate news signal classification spine"
    )
    parser.add_argument("--run-id", default="cli-run",
                        help="Run ID for this agent invocation")
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Minimal test snapshot — bullish news with high credibility and relevance
    test_snapshot = {
        "run_id":    args.run_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "normalized_news": {
            "headline_sentiment":  0.65,
            "article_count":       12,
            "source_credibility":  0.80,
            "market_relevance":    0.75,
            "confirmation_count":  4,
            "contradiction_count": 1,
        },
        "promoted_rumor_context": None,
    }

    log.info("Running news agent for run_id: %s", args.run_id)
    result = run_agent(test_snapshot)
    write_news_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"NEWS AGENT 7.2 — run_id: {result.get('run_id')}")
        print(f"Agent Version        : {result.get('agent_version')}")
        print(f"{'='*72}")
        print(f"Classification       : {result.get('classification')}")
        print(f"Overall Confidence   : {result.get('overall_confidence')}")
        print(f"Confirmation State   : {result.get('confirmation_state')}")
        print(f"Sentiment State      : {result.get('sentiment_state')}")
        print(f"Credibility State    : {result.get('source_credibility_state')}")
        print(f"Relevance State      : {result.get('market_relevance_state')}")
        if result.get("halted"):
            print(f"\n*** HALTED: {result.get('halt_reason')} ***")
        print(f"\nDecision Log ({len(result.get('decision_log', []))} records):")
        for rec in result.get("decision_log", []):
            print(f"  Gate {rec['gate']:1d} | {rec['name']:30s} | {rec['result']:30s} | {rec['reason_code']}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
