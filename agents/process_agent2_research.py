#!/usr/bin/env python3
"""
agent2_research.py — Agent 2 (ResearchAgent)
22-gate deterministic news classification spine.
Processes political and legislative disclosures and news.
All decisions are rule-based and fully traceable.
AGENT_VERSION = "1.0.0"
"""

import sys
import os
import json
import logging
import argparse
import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

# --- Path bootstrap ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_helpers import get_db_helpers
from synthos_paths import get_paths

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent2_research")

# --- Path and DB resolution ---
_paths = get_paths()
_db    = get_db_helpers()

AGENT_VERSION = "1.0.0"

# ============================================================
# CONFIG BLOCK
# ============================================================

MAX_NEWS_AGE_HOURS          = 24
DUPLICATE_THRESHOLD         = 0.60
MIN_WORD_COUNT              = 8
TREND_NEUTRAL_BAND          = 0.002
SPX_VOL_THRESHOLD           = 0.018
SPX_DRAWDOWN_THRESH         = 0.05
SPX_ROC_POSITIVE_THRESH     = 0.001
SPX_ROC_NEGATIVE_THRESH     = -0.001
MIN_CREDIBILITY             = 0.35
MIN_RELEVANCE               = 0.20
FOLLOW_UP_SIMILARITY        = 0.50
POSITIVE_THRESHOLD          = 0.10
NEGATIVE_THRESHOLD          = -0.10
MIXED_MIN_THRESHOLD         = 0.05
UNCERTAINTY_DENSITY_MAX     = 0.12
EXAGGERATION_DELTA          = 0.15
SURPRISE_THRESHOLD          = 0.65
NOVELTY_THRESHOLD           = 0.40
MIN_INCREMENTAL_INFO        = 0.25
MIN_CONFIRMATIONS           = 2
TRADEABLE_WINDOW_HOURS      = 8
CLUSTER_VOL_THRESHOLD       = 8
EXTREME_ATTENTION_MULT      = 2.0
SENTIMENT_CONF_MIN          = 0.25
DISCOUNT_SENTIMENT_CONF     = 0.70
DISCOUNT_BENCHMARK_VOL      = 0.80
DISCOUNT_NOISY_EVENT        = 0.60
DISCOUNT_SOURCE_LOW         = 0.50
DISCOUNT_CONTRADICTION      = 0.50
COMPOSITE_QUALITY_THRESH    = 0.45
COMPOSITE_W1                = 0.20   # impact_score
COMPOSITE_W2                = 0.15   # credibility_score
COMPOSITE_W3                = 0.15   # novelty_score
COMPOSITE_W4                = 0.20   # sentiment_confidence
COMPOSITE_W5                = 0.15   # confirmation_score
COMPOSITE_W6                = 0.10   # (1 - crowding_discount)
COMPOSITE_W7                = 0.05   # (1 - ambiguity_score)
MEMBER_WEIGHT_MIN           = 0.5
MEMBER_WEIGHT_MAX           = 1.5
MEMBER_WEIGHT_MIN_TRADES    = 5

# ============================================================
# KEYWORD SETS
# ============================================================

MACRO_TERMS = {
    "federal reserve", "interest rate", "inflation", "gdp", "unemployment",
    "cpi", "fomc", "monetary policy", "fiscal", "stimulus", "deficit",
    "treasury", "debt ceiling",
}

EARNINGS_TERMS = {
    "earnings", "revenue", "eps", "guidance", "profit", "loss",
    "beat", "miss", "quarterly results",
}

GEO_TERMS = {
    "war", "conflict", "sanctions", "tariff", "trade war", "geopolitical",
    "military", "election", "coup",
}

REGULATORY_TERMS = {
    "regulation", "sec", "compliance", "antitrust", "merger", "acquisition",
    "investigation", "fine", "penalty", "legislation", "bill", "act",
    "congress", "senate", "house vote",
}

POSITIVE_WORDS = {
    "surges", "rises", "jumps", "beats", "exceeds", "strong", "growth",
    "gains", "record", "positive", "bullish", "upgrade",
}

NEGATIVE_WORDS = {
    "falls", "drops", "declines", "misses", "weak", "loss", "cut",
    "downgrade", "warning", "crisis", "crash", "risk", "concern",
}

UNCERTAINTY_WORDS = {
    "may", "might", "could", "uncertain", "unclear", "possible", "potential",
    "speculation", "rumor", "suggests", "reportedly",
}

OPINION_KEYWORDS = {
    "opinion", "editorial", "analysis", "commentary", "perspective",
    "view", "think", "believe",
}

PRIMARY_SOURCE_SIGNALS = {
    "said", "announced", "reported", "confirmed", "stated", "disclosed",
}

MARKET_STRUCTURE_TERMS = {
    "hft", "high frequency", "circuit breaker", "liquidity", "flash crash",
    "market maker", "spread", "volatility index",
}

SECTOR_NAMES = {
    "technology", "healthcare", "financials", "energy", "utilities",
    "industrials", "materials", "consumer", "real estate", "communication",
}


# ============================================================
# ARTICLE STATE
# ============================================================

@dataclass
class ArticleState:
    """Flat store of all gate outputs for a single article run."""

    # Gate 1
    system_status:          Optional[str] = None
    word_count:             int           = 0

    # Gate 2
    trend_state:            str           = "neutral"
    volatility_state:       str           = "normal"
    drawdown_state:         bool          = False
    momentum_state:         str           = "flat"
    benchmark_data_missing: bool          = False

    # Gate 3
    credibility_score:      float         = 0.0
    opinion_flag:           bool          = False
    relevance_score:        float         = 0.0
    relevance_ok:           bool          = False
    source_skipped:         bool          = False

    # Gate 4
    topic_state:            Optional[str] = None

    # Gate 5
    entity_state:           Optional[str] = None

    # Gate 6
    event_state:            Optional[str] = None
    uncertainty_density:    float         = 0.0

    # Gate 7
    sentiment_score:        float         = 0.0
    sentiment_state:        Optional[str] = None
    sentiment_confidence:   float         = 0.0
    headline_score:         float         = 0.0
    body_score:             float         = 0.0
    headline_exaggeration:  bool          = False
    pos_count:              int           = 0
    neg_count:              int           = 0

    # Gate 8
    novelty_score:          float         = 0.0
    novelty_state:          Optional[str] = None
    novelty_ok:             bool          = True

    # Gate 9
    scope_state:            Optional[str] = None
    benchmark_corr:         Optional[str] = None

    # Gate 10
    horizon_state:          Optional[str] = None
    decay_state:            Optional[str] = None

    # Gate 11
    benchmark_rel_state:    Optional[str] = None
    signal_type:            Optional[str] = None
    dominance_state:        Optional[str] = None

    # Gate 12
    confirmation_state:     Optional[str] = None
    confirmation_score:     float         = 0.0

    # Gate 13
    article_age_hours:      float         = 0.0
    timing_state:           Optional[str] = None
    timing_tradeable:       bool          = True

    # Gate 14
    crowding_state:         Optional[str] = None
    crowding_discount:      float         = 0.0

    # Gate 15
    ambiguity_state:        Optional[str] = None
    ambiguity_score:        float         = 0.0

    # Gate 16
    impact_magnitude:       Optional[str] = None
    impact_link_state:      Optional[str] = None
    base_impact_score:      float         = 0.0

    # Gate 17
    action_state:           Optional[str] = None

    # Gate 18
    impact_score:           float         = 0.0
    final_confidence:       Optional[str] = None

    # Gate 19
    persistence_state:      Optional[str] = None

    # Gate 20
    portfolio_relevant:     bool          = False

    # Gate 21
    output_mode:            Optional[str] = None
    output_priority:        Optional[str] = None
    output_action:          Optional[str] = None
    pre_routing:            Optional[str] = None

    # Gate 22
    composite_score:        float         = 0.0
    final_signal:           Optional[str] = None
    final_routing:          Optional[str] = None
    adjusted_score:         float         = 0.0


# ============================================================
# NEWS DECISION LOG
# ============================================================

class NewsDecisionLog:
    """
    Accumulates all gate records produced during a single classify_article() run.
    Serialises to JSON and human-readable text.
    """

    def __init__(self, article_id: str):
        self.article_id = article_id
        self.timestamp  = datetime.datetime.utcnow().isoformat()
        self.records    = []
        self.halted     = False
        self.halt_reason: Optional[str] = None

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
            "article_id":           self.article_id,
            "timestamp":            self.timestamp,
            "agent_version":        AGENT_VERSION,
            "halted":               self.halted,
            "halt_reason":          self.halt_reason,
            "decision_log":         self.records,
        }

    def to_human_readable(self) -> str:
        lines = [
            "=" * 72,
            "RESEARCH AGENT (Agent 2) — DECISION LOG",
            f"Article ID     : {self.article_id}",
            f"Timestamp      : {self.timestamp}",
            f"Agent Version  : {AGENT_VERSION}",
            "=" * 72,
        ]
        if self.halted:
            lines.append(f"\n*** HALTED at gate — reason: {self.halt_reason} ***")
        lines.append(f"\nGate records ({len(self.records)} total):")
        for r in self.records:
            lines.append(
                f"  [{r['gate']:>2}] {r['name']:<34}  result={r['result']:<28}  code={r['reason_code']}"
            )
        lines.append("=" * 72)
        return "\n".join(lines)


# ============================================================
# HELPERS
# ============================================================

def _jaccard(text_a: str, candidates: list) -> float:
    """Return the maximum Jaccard similarity between text_a and any candidate string."""
    tokens_a = set(text_a.lower().split())
    if not tokens_a:
        return 0.0
    best = 0.0
    for cand in candidates:
        tokens_b = set(cand.lower().split())
        if not tokens_b:
            continue
        inter = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        if union > 0:
            best = max(best, inter / union)
    return best


def _word_count(text: str) -> int:
    return len(text.split()) if text and text.strip() else 0


def _count_keywords(text_lower: str, keyword_set: set) -> int:
    """Count how many keywords from the set appear in text_lower (phrase-aware)."""
    return sum(1 for kw in keyword_set if kw in text_lower)


# ============================================================
# GATE 1 — SYSTEM GATE
# ============================================================

def gate_1_system_gate(article: dict, state: ArticleState, dlog: NewsDecisionLog) -> bool:
    """
    GATE 1 — SYSTEM GATE  [HALTING]
    Validates headline, timestamp, age, duplicates, and body length.
    Returns True if the run should continue; False if halted.
    """
    headline     = article.get("headline")
    disc_date_str= article.get("disc_date")
    body         = article.get("body", "") or ""
    seen         = article.get("seen_headlines", [])

    # Check 1: Headline present and long enough
    if headline is None or len(headline.strip()) < 5:
        dlog.halt(1, "parse_failure", {"headline": headline})
        state.system_status = "parse_failure"
        return False

    # Check 2: disc_date present
    if disc_date_str is None:
        dlog.halt(1, "timestamp_rejected", {"disc_date": None})
        state.system_status = "timestamp_rejected"
        return False

    # Check 3: Parse and age check
    try:
        disc_date = datetime.datetime.fromisoformat(disc_date_str.replace("Z", "+00:00"))
        # Make disc_date offset-naive UTC for comparison if needed
        if disc_date.tzinfo is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
        else:
            now = datetime.datetime.utcnow()
        age_hours = (now - disc_date).total_seconds() / 3600.0
    except (ValueError, TypeError):
        dlog.halt(1, "timestamp_rejected", {"disc_date": disc_date_str, "error": "unparseable"})
        state.system_status = "timestamp_rejected"
        return False

    if age_hours > MAX_NEWS_AGE_HOURS:
        dlog.halt(1, "timestamp_rejected",
                  {"age_hours": round(age_hours, 2), "max_age_hours": MAX_NEWS_AGE_HOURS})
        state.system_status = "timestamp_rejected"
        return False

    # Check 4: Duplicate detection via Jaccard on headline vs seen_headlines
    if seen:
        sim = _jaccard(headline, seen)
        if sim > DUPLICATE_THRESHOLD:
            dlog.halt(1, "duplicate",
                      {"headline": headline, "max_jaccard": round(sim, 4),
                       "threshold": DUPLICATE_THRESHOLD})
            state.system_status = "duplicate"
            return False

    # Check 5: Body word count
    wc = _word_count(body)
    state.word_count = wc
    if wc < MIN_WORD_COUNT:
        dlog.halt(1, "body_too_short",
                  {"word_count": wc, "min_word_count": MIN_WORD_COUNT})
        state.system_status = "body_too_short"
        return False

    state.system_status = "system_ok"
    dlog.record(1, "system_gate",
                {"headline_len": len(headline.strip()), "age_hours": round(age_hours, 2),
                 "word_count": wc, "seen_headlines_checked": len(seen)},
                "system_ok", "all_system_checks_passed")
    return True


# ============================================================
# GATE 2 — BENCHMARK GATE (SPX Context)
# ============================================================

def gate_2_benchmark(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 2 — BENCHMARK GATE
    Classifies SPX trend, volatility, drawdown, and momentum.
    Uses benchmark_data from article dict; defaults to neutral if absent.
    """
    bdata = article.get("benchmark_data") or {}

    if not bdata:
        state.trend_state      = "neutral"
        state.volatility_state = "normal"
        state.drawdown_state   = False
        state.momentum_state   = "flat"
        state.benchmark_data_missing = True
        dlog.record(2, "benchmark",
                    {"benchmark_data": None},
                    "defaults_applied", "TODO:DATA_DEPENDENCY")
        return

    sma_short    = bdata.get("SMA_short")
    sma_long     = bdata.get("SMA_long")
    atr          = bdata.get("ATR")
    price        = bdata.get("price")
    rolling_peak = bdata.get("rolling_peak")
    roc_5        = bdata.get("ROC_5")

    # Trend state
    if sma_short is not None and sma_long is not None and sma_long != 0:
        if sma_short > sma_long * (1 + TREND_NEUTRAL_BAND):
            state.trend_state = "bullish"
        elif sma_short < sma_long * (1 - TREND_NEUTRAL_BAND):
            state.trend_state = "bearish"
        else:
            state.trend_state = "neutral"
    else:
        state.trend_state = "neutral"

    # Volatility state
    if atr is not None and price is not None and price != 0:
        if (atr / price) > SPX_VOL_THRESHOLD:
            state.volatility_state = "high_vol"
        else:
            state.volatility_state = "normal"
    else:
        state.volatility_state = "normal"

    # Drawdown state
    if price is not None and rolling_peak is not None and rolling_peak != 0:
        drawdown = (price - rolling_peak) / rolling_peak
        state.drawdown_state = drawdown <= -SPX_DRAWDOWN_THRESH
    else:
        state.drawdown_state = False

    # Momentum state
    if roc_5 is not None:
        if roc_5 > SPX_ROC_POSITIVE_THRESH:
            state.momentum_state = "positive"
        elif roc_5 < SPX_ROC_NEGATIVE_THRESH:
            state.momentum_state = "negative"
        else:
            state.momentum_state = "flat"
    else:
        state.momentum_state = "flat"

    dlog.record(2, "benchmark",
                {"trend_state": state.trend_state,
                 "volatility_state": state.volatility_state,
                 "drawdown_state": state.drawdown_state,
                 "momentum_state": state.momentum_state},
                "benchmark_classified", "spx_context_applied")


# ============================================================
# GATE 3 — SOURCE & RELEVANCE FILTER
# ============================================================

def gate_3_source_relevance(article: dict, state: ArticleState, dlog: NewsDecisionLog) -> bool:
    """
    GATE 3 — SOURCE & RELEVANCE FILTER  [soft skip]
    Scores credibility by source tier and relevance by keyword density.
    Returns False if article should be skipped (not a hard halt).
    """
    source_tier  = article.get("source_tier", 3)
    headline     = article.get("headline", "") or ""
    body         = article.get("body", "") or ""
    full_text    = (headline + " " + body).lower()
    headline_low = headline.lower()

    # Credibility by tier
    if source_tier == 1:
        cred = 1.0
        opinion = False
    elif source_tier == 2:
        cred = 0.7
        opinion = False
    elif source_tier == 3:
        cred = 0.4
        opinion = False
    else:
        cred = 0.1
        opinion = True
        state.credibility_score = cred
        state.opinion_flag      = opinion
        state.source_skipped    = True
        dlog.record(3, "source_relevance",
                    {"source_tier": source_tier, "credibility_score": cred},
                    "skipped", "source_tier_4_or_above_opinion_flag")
        return False

    # Primary source signal boost
    if any(sig in full_text for sig in PRIMARY_SOURCE_SIGNALS):
        cred = min(cred + 0.1, 1.0)

    # Opinion keyword penalty
    if any(kw in headline_low for kw in OPINION_KEYWORDS):
        opinion = True
        cred -= 0.1

    state.opinion_flag = opinion

    # Min credibility check
    if cred < MIN_CREDIBILITY:
        state.credibility_score = cred
        state.source_skipped    = True
        dlog.record(3, "source_relevance",
                    {"credibility_score": round(cred, 4), "min_credibility": MIN_CREDIBILITY},
                    "skipped", "credibility_below_minimum")
        return False

    state.credibility_score = cred

    # Relevance score: keyword hits across all topic sets / 3.0, capped at 1.0
    all_topic_sets = [MACRO_TERMS, EARNINGS_TERMS, GEO_TERMS, REGULATORY_TERMS, MARKET_STRUCTURE_TERMS]
    total_hits = sum(_count_keywords(full_text, kset) for kset in all_topic_sets)
    relevance  = min(total_hits / 3.0, 1.0)
    state.relevance_score = round(relevance, 4)
    state.relevance_ok    = relevance >= MIN_RELEVANCE

    dlog.record(3, "source_relevance",
                {"source_tier": source_tier,
                 "credibility_score": round(state.credibility_score, 4),
                 "opinion_flag": state.opinion_flag,
                 "relevance_score": state.relevance_score,
                 "relevance_ok": state.relevance_ok},
                "passed", "source_relevance_evaluated")
    return True


# ============================================================
# GATE 4 — TOPIC CLASSIFICATION
# ============================================================

def gate_4_topic_classification(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 4 — TOPIC CLASSIFICATION
    Assigns topic_state using priority order (first match wins).
    """
    ticker       = article.get("ticker")
    known_tickers= article.get("known_tickers", []) or []
    headline     = article.get("headline", "") or ""
    body         = article.get("body", "") or ""
    text_lower   = (headline + " " + body).lower()

    topic = "uncertain"

    # 1. Company: ticker in text and known tickers
    if ticker and ticker in known_tickers and ticker.lower() in text_lower:
        topic = "company"

    # 2. Sector
    elif any(s in text_lower for s in SECTOR_NAMES):
        topic = "sector"

    # 3. Regulatory
    elif any(t in text_lower for t in REGULATORY_TERMS):
        topic = "regulatory"

    # 4. Earnings
    elif any(t in text_lower for t in EARNINGS_TERMS):
        topic = "earnings"

    # 5. Geopolitical
    elif any(t in text_lower for t in GEO_TERMS):
        topic = "geopolitical"

    # 6. Macro
    elif any(t in text_lower for t in MACRO_TERMS):
        topic = "macro"

    # 7. Market structure
    elif any(t in text_lower for t in MARKET_STRUCTURE_TERMS):
        topic = "market_structure"

    state.topic_state = topic
    dlog.record(4, "topic_classification",
                {"topic_state": topic, "ticker": ticker},
                topic, f"topic_classified_{topic}")


# ============================================================
# GATE 5 — ENTITY MAPPING
# ============================================================

def gate_5_entity_mapping(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 5 — ENTITY MAPPING
    Maps article to entity type for downstream routing.
    """
    ticker        = article.get("ticker")
    known_tickers = article.get("known_tickers", []) or []
    headline      = article.get("headline", "") or ""
    body          = article.get("body", "") or ""
    full_text     = (headline + " " + body).lower()
    topic         = state.topic_state

    # Count known tickers found in full text
    found_tickers = [t for t in known_tickers if t.lower() in full_text]
    multi_ticker  = len(found_tickers) >= 2

    if ticker and ticker in known_tickers and ticker.lower() in full_text:
        if multi_ticker:
            entity = "multi_company"
        else:
            entity = "company_linked"
    elif multi_ticker:
        entity = "multi_company"
    elif any(s in full_text for s in SECTOR_NAMES) and not ticker:
        entity = "sector_linked"
    elif topic in {"macro", "geopolitical"}:
        entity = "benchmark_relevant"
    else:
        entity = "non_actionable"

    state.entity_state = entity
    dlog.record(5, "entity_mapping",
                {"entity_state": entity, "found_tickers_count": len(found_tickers)},
                entity, f"entity_mapped_{entity}")


# ============================================================
# GATE 6 — EVENT DETECTION
# ============================================================

def gate_6_event_detection(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 6 — EVENT DETECTION
    Classifies the event type: breaking, follow_up, rumor, official, unscheduled.
    """
    source_tier      = article.get("source_tier", 3)
    recent_headlines = article.get("recent_headlines", []) or []
    headline         = article.get("headline", "") or ""
    body             = article.get("body", "") or ""
    full_text        = (headline + " " + body).lower()
    wc               = state.word_count

    # Uncertainty density
    uncertainty_count = sum(1 for uw in UNCERTAINTY_WORDS if uw in full_text.split())
    uncertainty_density = uncertainty_count / max(wc, 1)
    state.uncertainty_density = uncertainty_density

    event = "unscheduled"

    if source_tier <= 2 and wc < 50:
        event = "breaking"
    elif recent_headlines and _jaccard(headline, recent_headlines) > FOLLOW_UP_SIMILARITY:
        event = "follow_up"
    elif source_tier == 3 and uncertainty_density > 0.10:
        event = "rumor"
    elif source_tier == 1 and event == "unscheduled":
        event = "official"

    state.event_state = event
    dlog.record(6, "event_detection",
                {"event_state": event,
                 "uncertainty_density": round(uncertainty_density, 4),
                 "source_tier": source_tier, "word_count": wc},
                event, f"event_classified_{event}")


# ============================================================
# GATE 7 — SENTIMENT EXTRACTION
# ============================================================

def gate_7_sentiment_extraction(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 7 — SENTIMENT EXTRACTION
    Extracts sentiment score, state, confidence, and headline exaggeration flag.
    """
    headline = article.get("headline", "") or ""
    body     = article.get("body", "") or ""
    wc       = state.word_count

    full_text_lower = (headline + " " + body).lower()
    headline_lower  = headline.lower()
    body_lower      = body.lower()

    # Full text sentiment
    pos_count = sum(1 for w in POSITIVE_WORDS if w in full_text_lower)
    neg_count = sum(1 for w in NEGATIVE_WORDS if w in full_text_lower)
    total_tokens = pos_count + neg_count

    sentiment_score = (pos_count - neg_count) / max(total_tokens, 1)
    state.pos_count  = pos_count
    state.neg_count  = neg_count

    # Derive sentiment_state
    if sentiment_score > POSITIVE_THRESHOLD:
        sentiment_state = "positive"
    elif sentiment_score < NEGATIVE_THRESHOLD:
        sentiment_state = "negative"
    elif (pos_count > MIXED_MIN_THRESHOLD * wc and neg_count > MIXED_MIN_THRESHOLD * wc):
        sentiment_state = "mixed"
    elif state.uncertainty_density > UNCERTAINTY_DENSITY_MAX:
        # Would be neutral without this check
        sentiment_state = "uncertain"
    else:
        sentiment_state = "neutral"

    # Headline vs body exaggeration
    h_pos = sum(1 for w in POSITIVE_WORDS if w in headline_lower)
    h_neg = sum(1 for w in NEGATIVE_WORDS if w in headline_lower)
    h_tokens = h_pos + h_neg
    headline_score = (h_pos - h_neg) / max(h_tokens, 1)

    body_pos = sum(1 for w in POSITIVE_WORDS if w in body_lower)
    body_neg = sum(1 for w in NEGATIVE_WORDS if w in body_lower)
    body_tokens = body_pos + body_neg
    body_score = (body_pos - body_neg) / max(body_tokens, 1)

    headline_exaggeration = abs(headline_score - body_score) > EXAGGERATION_DELTA

    sentiment_confidence = min(total_tokens / max(wc, 1), 1.0)

    state.sentiment_score        = round(sentiment_score, 4)
    state.sentiment_state        = sentiment_state
    state.sentiment_confidence   = round(sentiment_confidence, 4)
    state.headline_score         = round(headline_score, 4)
    state.body_score             = round(body_score, 4)
    state.headline_exaggeration  = headline_exaggeration

    dlog.record(7, "sentiment_extraction",
                {"sentiment_state": sentiment_state,
                 "sentiment_score": round(sentiment_score, 4),
                 "sentiment_confidence": round(sentiment_confidence, 4),
                 "pos_count": pos_count, "neg_count": neg_count,
                 "headline_exaggeration": headline_exaggeration},
                sentiment_state, f"sentiment_{sentiment_state}")


# ============================================================
# GATE 8 — SURPRISE / NOVELTY
# ============================================================

def gate_8_surprise_novelty(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 8 — SURPRISE / NOVELTY
    Assigns novelty score and novelty_state.
    """
    novelty_score = article.get("novelty_score")

    if novelty_score is None:
        if state.event_state == "breaking":
            novelty_score = 0.75
        elif state.event_state == "follow_up":
            novelty_score = 0.20
        elif state.event_state == "official":
            novelty_score = 0.55
        else:
            novelty_score = 0.40

    sentiment = state.sentiment_state

    if novelty_score > SURPRISE_THRESHOLD and sentiment == "positive":
        novelty_state = "positive_surprise"
    elif novelty_score > SURPRISE_THRESHOLD and sentiment == "negative":
        novelty_state = "negative_surprise"
    elif novelty_score > NOVELTY_THRESHOLD:
        novelty_state = "novelty_high"
    elif novelty_score < MIN_INCREMENTAL_INFO:
        novelty_state = "repetitive"
    else:
        novelty_state = "incremental_update"

    novelty_ok = novelty_state not in {"repetitive"}

    state.novelty_score = round(novelty_score, 4)
    state.novelty_state = novelty_state
    state.novelty_ok    = novelty_ok

    dlog.record(8, "surprise_novelty",
                {"novelty_score": state.novelty_score, "novelty_state": novelty_state,
                 "novelty_ok": novelty_ok, "event_state": state.event_state},
                novelty_state, f"novelty_{novelty_state}")


# ============================================================
# GATE 9 — SCOPE OF IMPACT
# ============================================================

def gate_9_scope_of_impact(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 9 — SCOPE OF IMPACT
    Determines market scope and benchmark correlation level.
    """
    topic  = state.topic_state
    entity = state.entity_state

    if topic in {"macro", "geopolitical"} or entity == "benchmark_relevant":
        scope         = "marketwide"
        benchmark_corr= "HIGH"
    elif topic == "sector" or entity == "sector_linked":
        scope         = "sector_only"
        benchmark_corr= "MEDIUM"
    elif topic == "company" and entity == "company_linked":
        scope         = "single_name"
        benchmark_corr= "LOW"
    elif entity == "multi_company":
        scope         = "peer_group"
        benchmark_corr= "MEDIUM"
    else:
        scope         = "unclear"
        benchmark_corr= "MEDIUM"

    state.scope_state    = scope
    state.benchmark_corr = benchmark_corr

    dlog.record(9, "scope_of_impact",
                {"scope_state": scope, "benchmark_corr": benchmark_corr,
                 "topic_state": topic, "entity_state": entity},
                scope, f"scope_{scope}")


# ============================================================
# GATE 10 — TIME HORIZON
# ============================================================

def gate_10_time_horizon(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 10 — TIME HORIZON
    Classifies information decay horizon.
    """
    topic = state.topic_state
    event = state.event_state

    if topic in {"regulatory", "macro"}:
        horizon = "structural"
        decay   = "persistent"
    elif topic == "earnings":
        horizon = "multi_day"
        decay   = "medium_decay"
    elif topic == "geopolitical":
        horizon = "multi_day"
        decay   = "medium_decay"
    elif event == "breaking":
        horizon = "intraday"
        decay   = "fast_decay"
    else:
        horizon = "multi_day"
        decay   = "medium_decay"

    state.horizon_state = horizon
    state.decay_state   = decay

    dlog.record(10, "time_horizon",
                {"horizon_state": horizon, "decay_state": decay,
                 "topic_state": topic, "event_state": event},
                horizon, f"horizon_{horizon}")


# ============================================================
# GATE 11 — BENCHMARK-RELATIVE INTERPRETATION
# ============================================================

def gate_11_benchmark_relative(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 11 — BENCHMARK-RELATIVE INTERPRETATION
    Determines alignment with market trend and signal type.
    """
    sentiment = state.sentiment_state
    trend     = state.trend_state
    scope     = state.scope_state
    bench_corr= state.benchmark_corr

    if sentiment == "positive" and trend == "bullish":
        brel = "aligned_positive"
    elif sentiment == "negative" and trend == "bearish":
        brel = "aligned_negative"
    elif sentiment == "positive" and trend == "bearish":
        brel = "countertrend_positive"
    elif sentiment == "negative" and trend == "bullish":
        brel = "countertrend_negative"
    else:
        brel = "neutral"

    if scope == "single_name" and bench_corr == "LOW":
        signal_type     = "alpha"
        dominance_state = "idiosyncratic_dominant"
    else:
        signal_type     = "beta"
        dominance_state = "benchmark_dominant"

    state.benchmark_rel_state = brel
    state.signal_type         = signal_type
    state.dominance_state     = dominance_state

    dlog.record(11, "benchmark_relative",
                {"benchmark_rel_state": brel, "signal_type": signal_type,
                 "dominance_state": dominance_state},
                brel, f"benchmark_rel_{brel}")


# ============================================================
# GATE 12 — CONFIRMATION CONTROLS
# ============================================================

def gate_12_confirmation(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 12 — CONFIRMATION CONTROLS
    Evaluates source confirmation quality.
    """
    source_tier  = article.get("source_tier", 3)
    source_count = article.get("source_count", 1)
    headline     = article.get("headline", "") or ""
    body         = article.get("body", "") or ""
    full_text    = (headline + " " + body).lower()
    disc_date_str= article.get("disc_date")

    has_primary = (source_tier == 1 or
                   any(sig in full_text for sig in PRIMARY_SOURCE_SIGNALS))

    # Soft staleness: > 4 hours
    stale = False
    if disc_date_str:
        try:
            disc_date = datetime.datetime.fromisoformat(disc_date_str.replace("Z", "+00:00"))
            if disc_date.tzinfo is not None:
                now = datetime.datetime.now(datetime.timezone.utc)
            else:
                now = datetime.datetime.utcnow()
            article_age = (now - disc_date).total_seconds() / 3600.0
            stale = article_age > 4
        except (ValueError, TypeError):
            stale = False

    if source_tier == 1:
        conf_state = "primary_confirmed"
        conf_score = 1.0
    elif source_count >= MIN_CONFIRMATIONS and has_primary:
        conf_state = "strong"
        conf_score = 0.7
    elif (source_tier == 3 and not has_primary and
          state.uncertainty_density > 0.08):
        conf_state = "high_misinformation_risk"
        conf_score = 0.0
    elif source_count < MIN_CONFIRMATIONS and stale:
        conf_state = "expired_unconfirmed"
        conf_score = 0.1
    else:
        conf_state = "weak"
        conf_score = 0.4

    state.confirmation_state = conf_state
    state.confirmation_score = conf_score

    dlog.record(12, "confirmation",
                {"confirmation_state": conf_state,
                 "confirmation_score": conf_score,
                 "source_tier": source_tier,
                 "source_count": source_count,
                 "has_primary": has_primary,
                 "stale": stale},
                conf_state, f"confirmation_{conf_state}")


# ============================================================
# GATE 13 — TIMING CONTROLS
# ============================================================

def gate_13_timing(article: dict, state: ArticleState, dlog: NewsDecisionLog) -> bool:
    """
    GATE 13 — TIMING CONTROLS  [HALTING if expired]
    Returns False if article is outside tradeable window.
    """
    disc_date_str= article.get("disc_date")
    source_tier  = article.get("source_tier", 3)

    try:
        disc_date = datetime.datetime.fromisoformat(disc_date_str.replace("Z", "+00:00"))
        if disc_date.tzinfo is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
        else:
            now = datetime.datetime.utcnow()
            disc_date = disc_date  # naive, assume UTC/ET
        article_age_hours = (now - disc_date).total_seconds() / 3600.0
    except (ValueError, TypeError):
        article_age_hours = 0.0

    state.article_age_hours = round(article_age_hours, 2)

    # Expired check (hard halt)
    if article_age_hours > TRADEABLE_WINDOW_HOURS:
        state.timing_state     = "expired"
        state.timing_tradeable = False
        dlog.halt(13, "timing_expired",
                  {"article_age_hours": round(article_age_hours, 2),
                   "tradeable_window_hours": TRADEABLE_WINDOW_HOURS})
        return False

    # Publication hour in ET (no conversion; assume disc_date is already ET-equivalent)
    try:
        pub_hour   = disc_date.hour
        pub_minute = disc_date.minute
    except AttributeError:
        pub_hour   = 10
        pub_minute = 0

    if pub_hour < 9 or (pub_hour == 9 and pub_minute < 30):
        timing_state = "premarket"
    elif pub_hour >= 16:
        timing_state = "postmarket"
    elif source_tier == 3:
        timing_state = "delayed_distribution"
    else:
        timing_state = "active_flow"

    state.timing_state     = timing_state
    state.timing_tradeable = True

    dlog.record(13, "timing",
                {"timing_state": timing_state,
                 "article_age_hours": round(article_age_hours, 2),
                 "publication_hour": pub_hour},
                timing_state, f"timing_{timing_state}")
    return True


# ============================================================
# GATE 14 — CROWDING / SATURATION
# ============================================================

def gate_14_crowding(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 14 — CROWDING / SATURATION
    Evaluates cluster volume to detect crowded or exhausted narrative.
    """
    cluster_volume = article.get("cluster_volume", 0) or 0

    exhaustion_threshold = CLUSTER_VOL_THRESHOLD * EXTREME_ATTENTION_MULT

    if cluster_volume >= exhaustion_threshold:
        crowding_state    = "exhausted"
        crowding_discount = 0.9
    elif cluster_volume >= CLUSTER_VOL_THRESHOLD:
        crowding_state    = "crowded"
        crowding_discount = 0.5
    else:
        crowding_state    = "still_open"
        crowding_discount = 0.0

    state.crowding_state    = crowding_state
    state.crowding_discount = crowding_discount

    dlog.record(14, "crowding",
                {"crowding_state": crowding_state,
                 "crowding_discount": crowding_discount,
                 "cluster_volume": cluster_volume,
                 "threshold": CLUSTER_VOL_THRESHOLD},
                crowding_state, f"crowding_{crowding_state}")


# ============================================================
# GATE 15 — CONTRADICTION / AMBIGUITY
# ============================================================

def gate_15_contradiction_ambiguity(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 15 — CONTRADICTION / AMBIGUITY
    Flags internally conflicted or mismatched signals.
    """
    pos_count = state.pos_count
    neg_count = state.neg_count
    headline  = article.get("headline", "") or ""
    body      = article.get("body", "") or ""

    # Determine headline and body sentiments separately
    h_low   = headline.lower()
    b_low   = body.lower()
    h_pos   = sum(1 for w in POSITIVE_WORDS if w in h_low)
    h_neg   = sum(1 for w in NEGATIVE_WORDS if w in h_low)
    b_pos   = sum(1 for w in POSITIVE_WORDS if w in b_low)
    b_neg   = sum(1 for w in NEGATIVE_WORDS if w in b_low)

    # Headline sentiment direction
    def _direction(p, n):
        if p > n:
            return "positive"
        elif n > p:
            return "negative"
        return "neutral"

    h_dir = _direction(h_pos, h_neg)
    b_dir = _direction(b_pos, b_neg)

    headline_body_mismatch = (
        (h_dir != b_dir) and
        (h_pos + h_neg) > 0 and
        (b_pos + b_neg) > 0 and
        h_dir in {"positive", "negative"} and
        b_dir in {"positive", "negative"}
    )

    if pos_count >= 2 and neg_count >= 2:
        ambiguity_state = "internally_conflicted"
        ambiguity_score = 1.0
    elif headline_body_mismatch:
        ambiguity_state = "headline_body_mismatch"
        ambiguity_score = 0.8
    elif state.uncertainty_density > UNCERTAINTY_DENSITY_MAX:
        ambiguity_state = "uncertain_language"
        ambiguity_score = 0.5
    else:
        ambiguity_state = "clear"
        ambiguity_score = 0.0

    state.ambiguity_state = ambiguity_state
    state.ambiguity_score = ambiguity_score

    dlog.record(15, "contradiction_ambiguity",
                {"ambiguity_state": ambiguity_state,
                 "ambiguity_score": ambiguity_score,
                 "pos_count": pos_count, "neg_count": neg_count,
                 "headline_body_mismatch": headline_body_mismatch},
                ambiguity_state, f"ambiguity_{ambiguity_state}")


# ============================================================
# GATE 16 — IMPACT MAGNITUDE
# ============================================================

def gate_16_impact_magnitude(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 16 — IMPACT MAGNITUDE
    Determines base impact score and magnitude with volatility boost logic.
    """
    scope   = state.scope_state
    topic   = state.topic_state
    vol_st  = state.volatility_state
    drawdown= state.drawdown_state

    if scope == "marketwide" and topic in {"macro", "geopolitical"}:
        magnitude        = "high"
        impact_link      = "benchmark_linked"
        base_impact      = 1.0
    elif scope in {"sector_only", "peer_group"}:
        magnitude        = "medium"
        impact_link      = "benchmark_weak"
        base_impact      = 0.5
    elif scope == "single_name" and topic == "earnings":
        magnitude        = "medium"
        impact_link      = "benchmark_weak"
        base_impact      = 0.5
    elif scope == "single_name":
        magnitude        = "low"
        impact_link      = "benchmark_weak"
        base_impact      = 0.2
    else:
        magnitude        = "low"
        impact_link      = "benchmark_weak"
        base_impact      = 0.2

    # Boost logic
    if vol_st == "high_vol" or drawdown:
        if magnitude == "low":
            magnitude   = "medium"
            base_impact = 0.5
        elif magnitude == "medium":
            magnitude   = "high"
            base_impact = 1.0

    state.impact_magnitude  = magnitude
    state.impact_link_state = impact_link
    state.base_impact_score = base_impact

    dlog.record(16, "impact_magnitude",
                {"impact_magnitude": magnitude,
                 "impact_link_state": impact_link,
                 "base_impact_score": base_impact,
                 "volatility_boost": (vol_st == "high_vol" or drawdown)},
                magnitude, f"impact_{magnitude}")


# ============================================================
# GATE 17 — ACTION CLASSIFICATION
# ============================================================

def gate_17_action_classification(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 17 — ACTION CLASSIFICATION
    Applies priority-ordered rules to assign action_state.
    """
    cred      = state.credibility_score
    novelty_s = state.novelty_state
    conf_s    = state.confirmation_state
    ambig_s   = state.ambiguity_state
    event_s   = state.event_state
    sentiment = state.sentiment_state
    sent_conf = state.sentiment_confidence
    scope     = state.scope_state
    bench_c   = state.benchmark_corr
    sig_type  = state.signal_type
    novelty_ok= state.novelty_ok
    novelty_v = state.novelty_score

    action = "watch_only"   # default

    if novelty_s == "repetitive":
        action = "ignore"
    elif conf_s == "high_misinformation_risk":
        action = "ignore"
    elif cred < 0.35 and novelty_v < MIN_INCREMENTAL_INFO:
        action = "ignore"
    elif ambig_s == "internally_conflicted" and conf_s in {"weak", "contradictory"}:
        action = "freeze"
    elif event_s == "rumor" and conf_s == "weak":
        action = "provisional_watch"
    elif ambig_s in {"headline_body_mismatch", "uncertain_language"}:
        action = "watch_only"
    elif sentiment in {"neutral", "mixed", "uncertain"} or sent_conf < SENTIMENT_CONF_MIN:
        action = "watch_only"
    elif (scope == "marketwide" and bench_c == "HIGH" and
          sent_conf > SENTIMENT_CONF_MIN):
        action = "benchmark_regime_signal"
    elif (scope == "single_name" and sig_type == "alpha" and
          novelty_ok and cred > 0.5):
        action = "relative_alpha_signal"
    elif (sentiment == "positive" and cred > 0.5 and
          novelty_ok and ambig_s == "clear"):
        action = "bullish_signal"
    elif (sentiment == "negative" and cred > 0.5 and
          novelty_ok and ambig_s == "clear"):
        action = "bearish_signal"

    state.action_state = action

    dlog.record(17, "action_classification",
                {"action_state": action,
                 "sentiment_state": sentiment,
                 "ambiguity_state": ambig_s,
                 "confirmation_state": conf_s,
                 "credibility_score": round(cred, 4)},
                action, f"action_{action}")


# ============================================================
# GATE 18 — RISK DISCOUNTS
# ============================================================

def gate_18_risk_discounts(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 18 — RISK DISCOUNTS (multiplicative)
    Applies discount factors to produce final impact_score and confidence label.
    """
    impact = state.base_impact_score
    discounts_applied = []

    if state.sentiment_confidence < SENTIMENT_CONF_MIN:
        impact *= DISCOUNT_SENTIMENT_CONF
        discounts_applied.append("sentiment_conf")

    if state.volatility_state == "high_vol":
        impact *= DISCOUNT_BENCHMARK_VOL
        discounts_applied.append("benchmark_vol")

    if state.event_state == "rumor":
        impact *= DISCOUNT_NOISY_EVENT
        discounts_applied.append("noisy_event")

    if state.credibility_score < 0.5:
        impact *= DISCOUNT_SOURCE_LOW
        discounts_applied.append("source_low")

    if state.ambiguity_state != "clear":
        impact *= DISCOUNT_CONTRADICTION
        discounts_applied.append("contradiction")

    impact = round(impact, 4)

    if impact > 0.70:
        final_confidence = "HIGH"
    elif impact > 0.30:
        final_confidence = "MEDIUM"
    else:
        final_confidence = "LOW"

    state.impact_score     = impact
    state.final_confidence = final_confidence

    dlog.record(18, "risk_discounts",
                {"impact_score": impact,
                 "final_confidence": final_confidence,
                 "discounts_applied": discounts_applied,
                 "base_impact_score": state.base_impact_score},
                final_confidence, f"confidence_{final_confidence}")


# ============================================================
# GATE 19 — PERSISTENCE CONTROLS
# ============================================================

def gate_19_persistence(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 19 — PERSISTENCE CONTROLS
    Classifies how long the signal is expected to persist.
    """
    topic = state.topic_state
    event = state.event_state

    if topic in {"regulatory", "macro"}:
        persistence = "structural"
    elif topic == "geopolitical":
        persistence = "dynamically_updated"
    elif topic == "earnings":
        persistence = "medium_or_fast"
    elif event == "breaking":
        persistence = "rapid"
    else:
        persistence = "slow"

    state.persistence_state = persistence

    dlog.record(19, "persistence",
                {"persistence_state": persistence,
                 "topic_state": topic, "event_state": event},
                persistence, f"persistence_{persistence}")


# ============================================================
# GATE 20 — EVALUATION LOOP
# ============================================================

def gate_20_evaluation_loop(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 20 — EVALUATION LOOP
    Checks portfolio relevance. Post-trade outcome comparison is a future dependency.
    """
    active_ticker       = article.get("ticker_in_active_signals", False)
    portfolio_relevant  = bool(active_ticker)
    state.portfolio_relevant = portfolio_relevant

    dlog.record(20, "evaluation_loop",
                {"portfolio_relevant": portfolio_relevant},
                "recorded", "evaluation_noted")
    # TODO:DATA_DEPENDENCY — post-trade outcome comparison requires realized move tracking


# ============================================================
# GATE 21 — OUTPUT CONTROLS
# ============================================================

def gate_21_output_controls(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 21 — OUTPUT CONTROLS
    Maps action_state to output mode, priority, action label, and pre-routing.
    """
    impact       = state.impact_score
    bench_corr   = state.benchmark_corr
    action       = state.action_state

    if impact > 0.70:
        output_mode = "decisive"
    elif impact > 0.30:
        output_mode = "probabilistic"
    else:
        output_mode = "uncertain"

    if bench_corr == "HIGH":
        output_priority = "benchmark_first"
    else:
        output_priority = "article_first"

    output_action_map = {
        "freeze":                    "wait_for_confirmation",
        "ignore":                    "no_signal",
        "benchmark_regime_signal":   "benchmark_context_signal",
        "relative_alpha_signal":     "idiosyncratic_alpha_signal",
        "bullish_signal":            "positive_signal",
        "bearish_signal":            "negative_signal",
    }
    output_action = output_action_map.get(action, "watch_signal")

    if action in {"bullish_signal", "relative_alpha_signal"}:
        pre_routing = "QUEUE"
    elif action == "ignore":
        pre_routing = "DISCARD"
    else:
        pre_routing = "WATCH"

    state.output_mode     = output_mode
    state.output_priority = output_priority
    state.output_action   = output_action
    state.pre_routing     = pre_routing

    dlog.record(21, "output_controls",
                {"output_mode": output_mode,
                 "output_priority": output_priority,
                 "output_action": output_action,
                 "pre_routing": pre_routing},
                output_action, f"output_{output_mode}")


# ============================================================
# GATE 22 — FINAL COMPOSITE SCORE
# ============================================================

def gate_22_composite_score(article: dict, state: ArticleState, dlog: NewsDecisionLog):
    """
    GATE 22 — FINAL COMPOSITE SCORE
    Computes composite_score, final_signal, final_routing, and adjusted_score.
    """
    composite = (
        COMPOSITE_W1 * state.impact_score +
        COMPOSITE_W2 * state.credibility_score +
        COMPOSITE_W3 * state.novelty_score +
        COMPOSITE_W4 * state.sentiment_confidence +
        COMPOSITE_W5 * state.confirmation_score +
        COMPOSITE_W6 * (1.0 - state.crowding_discount) +
        COMPOSITE_W7 * (1.0 - state.ambiguity_score)
    )
    composite = round(max(0.0, min(1.0, composite)), 4)

    action      = state.action_state
    pre_routing = state.pre_routing

    # Final signal and routing
    if action == "ignore":
        final_signal  = "no_signal"
        final_routing = "DISCARD"
    elif composite >= COMPOSITE_QUALITY_THRESH and action == "bullish_signal":
        final_signal  = "bullish_signal"
        final_routing = "QUEUE"
    elif composite >= COMPOSITE_QUALITY_THRESH and action == "bearish_signal":
        final_signal  = "bearish_signal"
        final_routing = "WATCH"
    elif composite < COMPOSITE_QUALITY_THRESH and pre_routing == "QUEUE":
        final_signal  = "watch_only"
        final_routing = "WATCH"
    elif action in {"freeze", "provisional_watch"}:
        final_signal  = "neutral_or_watch"
        final_routing = "WATCH"
    else:
        final_signal  = action
        final_routing = pre_routing

    # Benchmark override
    if state.drawdown_state and state.impact_link_state == "benchmark_linked":
        final_signal  = "benchmark_override"
        final_routing = "WATCH"

    # Member weight application
    member_weight = article.get("member_weight", 1.0)
    if member_weight is None:
        member_weight = 1.0
    member_weight = max(MEMBER_WEIGHT_MIN, min(MEMBER_WEIGHT_MAX, member_weight))
    adjusted_score = round(composite * member_weight, 4)

    state.composite_score = composite
    state.final_signal    = final_signal
    state.final_routing   = final_routing
    state.adjusted_score  = adjusted_score

    dlog.record(22, "composite_score",
                {"composite_score": composite,
                 "final_signal": final_signal,
                 "final_routing": final_routing,
                 "adjusted_score": adjusted_score,
                 "member_weight": member_weight},
                final_signal, f"final_{final_signal}")


# ============================================================
# PIPELINE ORCHESTRATOR
# ============================================================

def classify_article(article: dict) -> dict:
    """
    Orchestrates the 22-gate news classification pipeline for a single article.

    article dict keys:
        headline               : str
        body                   : str
        disc_date              : str (ISO datetime)
        source_tier            : int (1-4)
        ticker                 : str | None
        known_tickers          : list[str]
        seen_headlines         : list[str]   (for dedup)
        recent_headlines       : list[str]   (for follow-up detection)
        cluster_volume         : int         (similar articles in last 4h)
        source_count           : int
        member_weight          : float       (default 1.0)
        ticker_in_active_signals: bool
        novelty_score          : float | None (pre-computed if available)
        benchmark_data         : dict | None  (SPX context)

    Returns complete decision log dict.
    """
    import uuid
    article_id = article.get("article_id") or str(uuid.uuid4())[:8]
    state = ArticleState()
    dlog  = NewsDecisionLog(article_id)

    # Gate 1: System gate (halting)
    if not gate_1_system_gate(article, state, dlog):
        result = dlog.to_dict()
        result.update(_state_summary(state))
        return result

    # Gate 2: Benchmark (SPX context)
    gate_2_benchmark(article, state, dlog)

    # Gate 3: Source & relevance (soft skip)
    if not gate_3_source_relevance(article, state, dlog):
        result = dlog.to_dict()
        result.update(_state_summary(state))
        return result

    # Gate 4: Topic classification
    gate_4_topic_classification(article, state, dlog)

    # Gate 5: Entity mapping
    gate_5_entity_mapping(article, state, dlog)

    # Gate 6: Event detection
    gate_6_event_detection(article, state, dlog)

    # Gate 7: Sentiment extraction
    gate_7_sentiment_extraction(article, state, dlog)

    # Gate 8: Surprise / novelty
    gate_8_surprise_novelty(article, state, dlog)

    # Gate 9: Scope of impact
    gate_9_scope_of_impact(article, state, dlog)

    # Gate 10: Time horizon
    gate_10_time_horizon(article, state, dlog)

    # Gate 11: Benchmark-relative interpretation
    gate_11_benchmark_relative(article, state, dlog)

    # Gate 12: Confirmation controls
    gate_12_confirmation(article, state, dlog)

    # Gate 13: Timing controls (halting if expired)
    if not gate_13_timing(article, state, dlog):
        result = dlog.to_dict()
        result.update(_state_summary(state))
        return result

    # Gate 14: Crowding / saturation
    gate_14_crowding(article, state, dlog)

    # Gate 15: Contradiction / ambiguity
    gate_15_contradiction_ambiguity(article, state, dlog)

    # Gate 16: Impact magnitude
    gate_16_impact_magnitude(article, state, dlog)

    # Gate 17: Action classification
    gate_17_action_classification(article, state, dlog)

    # Gate 18: Risk discounts
    gate_18_risk_discounts(article, state, dlog)

    # Gate 19: Persistence controls
    gate_19_persistence(article, state, dlog)

    # Gate 20: Evaluation loop
    gate_20_evaluation_loop(article, state, dlog)

    # Gate 21: Output controls
    gate_21_output_controls(article, state, dlog)

    # Gate 22: Final composite score
    gate_22_composite_score(article, state, dlog)

    result = dlog.to_dict()
    result.update(_state_summary(state))
    return result


def _state_summary(state: ArticleState) -> dict:
    """Merges all ArticleState fields into the result dict for a flat output."""
    return {
        # Gate 1
        "system_status":          state.system_status,
        "word_count":             state.word_count,
        # Gate 2
        "trend_state":            state.trend_state,
        "volatility_state":       state.volatility_state,
        "drawdown_state":         state.drawdown_state,
        "momentum_state":         state.momentum_state,
        "benchmark_data_missing": state.benchmark_data_missing,
        # Gate 3
        "credibility_score":      state.credibility_score,
        "opinion_flag":           state.opinion_flag,
        "relevance_score":        state.relevance_score,
        "relevance_ok":           state.relevance_ok,
        # Gate 4
        "topic_state":            state.topic_state,
        # Gate 5
        "entity_state":           state.entity_state,
        # Gate 6
        "event_state":            state.event_state,
        "uncertainty_density":    state.uncertainty_density,
        # Gate 7
        "sentiment_score":        state.sentiment_score,
        "sentiment_state":        state.sentiment_state,
        "sentiment_confidence":   state.sentiment_confidence,
        "headline_score":         state.headline_score,
        "body_score":             state.body_score,
        "headline_exaggeration":  state.headline_exaggeration,
        "pos_count":              state.pos_count,
        "neg_count":              state.neg_count,
        # Gate 8
        "novelty_score":          state.novelty_score,
        "novelty_state":          state.novelty_state,
        "novelty_ok":             state.novelty_ok,
        # Gate 9
        "scope_state":            state.scope_state,
        "benchmark_corr":         state.benchmark_corr,
        # Gate 10
        "horizon_state":          state.horizon_state,
        "decay_state":            state.decay_state,
        # Gate 11
        "benchmark_rel_state":    state.benchmark_rel_state,
        "signal_type":            state.signal_type,
        "dominance_state":        state.dominance_state,
        # Gate 12
        "confirmation_state":     state.confirmation_state,
        "confirmation_score":     state.confirmation_score,
        # Gate 13
        "article_age_hours":      state.article_age_hours,
        "timing_state":           state.timing_state,
        "timing_tradeable":       state.timing_tradeable,
        # Gate 14
        "crowding_state":         state.crowding_state,
        "crowding_discount":      state.crowding_discount,
        # Gate 15
        "ambiguity_state":        state.ambiguity_state,
        "ambiguity_score":        state.ambiguity_score,
        # Gate 16
        "impact_magnitude":       state.impact_magnitude,
        "impact_link_state":      state.impact_link_state,
        "base_impact_score":      state.base_impact_score,
        # Gate 17
        "action_state":           state.action_state,
        # Gate 18
        "impact_score":           state.impact_score,
        "final_confidence":       state.final_confidence,
        # Gate 19
        "persistence_state":      state.persistence_state,
        # Gate 20
        "portfolio_relevant":     state.portfolio_relevant,
        # Gate 21
        "output_mode":            state.output_mode,
        "output_priority":        state.output_priority,
        "output_action":          state.output_action,
        "pre_routing":            state.pre_routing,
        # Gate 22
        "composite_score":        state.composite_score,
        "final_signal":           state.final_signal,
        "final_routing":          state.final_routing,
        "adjusted_score":         state.adjusted_score,
    }


# ============================================================
# DATABASE WRITE AND ESCALATION
# ============================================================

def write_article_log(result: dict):
    """Writes the article classification decision log to the database event log."""
    _db.log_event(
        event_type="news_classification",
        payload=result,
    )


def escalate_if_needed(result: dict):
    """
    Posts to the suggestion queue for high-severity news classifications.
    Escalation criteria:
      - final_signal == "bullish_signal" AND final_confidence == "HIGH"  → MEDIUM
      - drawdown_state AND final_signal == "benchmark_override"          → HIGH
      - final_signal == "bearish_signal" AND final_confidence == "HIGH"  → MEDIUM
    """
    final_signal  = result.get("final_signal")
    final_conf    = result.get("final_confidence")
    drawdown      = result.get("drawdown_state", False)
    article_id    = result.get("article_id")

    if final_signal == "bullish_signal" and final_conf == "HIGH":
        _db.post_suggestion(
            agent="agent2_research",
            category="news_signal",
            title=f"Bullish HIGH-confidence signal — article {article_id}",
            description=(
                f"Agent 2 classified article {article_id} as bullish_signal with HIGH confidence. "
                f"composite_score={result.get('composite_score')}  "
                f"topic={result.get('topic_state')}  scope={result.get('scope_state')}."
            ),
            risk_level="MEDIUM",
        )
        log.warning("ESCALATE: bullish_signal HIGH for article %s", article_id)

    elif drawdown and final_signal == "benchmark_override":
        _db.post_suggestion(
            agent="agent2_research",
            category="news_signal",
            title=f"Benchmark override during drawdown — article {article_id}",
            description=(
                f"Agent 2 issued benchmark_override for article {article_id} while SPX is in drawdown. "
                f"Immediate regime review required. composite_score={result.get('composite_score')}."
            ),
            risk_level="HIGH",
        )
        log.warning("ESCALATE: benchmark_override during drawdown for article %s", article_id)

    elif final_signal == "bearish_signal" and final_conf == "HIGH":
        _db.post_suggestion(
            agent="agent2_research",
            category="news_signal",
            title=f"Bearish HIGH-confidence signal — article {article_id}",
            description=(
                f"Agent 2 classified article {article_id} as bearish_signal with HIGH confidence. "
                f"composite_score={result.get('composite_score')}  "
                f"topic={result.get('topic_state')}  scope={result.get('scope_state')}."
            ),
            risk_level="MEDIUM",
        )
        log.warning("ESCALATE: bearish_signal HIGH for article %s", article_id)


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Research Agent (Agent 2) — 22-gate news classification spine"
    )
    parser.add_argument("--output", choices=["json", "human"], default="human",
                        help="Output format for decision log")
    args = parser.parse_args()

    # Synthetic test article: congressional trade disclosure, regulatory + earnings content
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    test_article = {
        "article_id":   "cli-test-001",
        "headline":     "Senator Smith discloses purchase of semiconductor stocks",
        "body": (
            "Senator Jane Smith filed a financial disclosure on Monday revealing the purchase "
            "of shares in two major semiconductor companies, according to congressional records. "
            "The transactions, disclosed under the STOCK Act, occurred during a period when "
            "the Senate was considering legislation on domestic chip manufacturing subsidies. "
            "Critics raised concerns about potential conflicts of interest, while the senator's "
            "office confirmed compliance with all regulatory requirements. The acquired companies "
            "reported strong quarterly earnings and revenue beats in their most recent filings, "
            "adding scrutiny to the timing of the purchase. The SEC has not announced any "
            "investigation into the matter at this time."
        ),
        "disc_date":    now_iso,
        "source_tier":  1,
        "ticker":       None,
        "known_tickers":["NVDA", "INTC", "AMD"],
        "seen_headlines": [],
        "recent_headlines": [],
        "cluster_volume": 2,
        "source_count": 1,
        "member_weight": 1.0,
        "ticker_in_active_signals": False,
        "novelty_score": None,
        "benchmark_data": {
            "SMA_short":    4900.0,
            "SMA_long":     4850.0,
            "ATR":          45.0,
            "price":        5000.0,
            "rolling_peak": 5100.0,
            "ROC_5":        0.002,
        },
    }

    log.info("Running research agent classification for article: %s",
             test_article["article_id"])

    result = classify_article(test_article)
    write_article_log(result)
    escalate_if_needed(result)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*72}")
        print(f"RESEARCH AGENT (Agent 2) — article: {result.get('article_id')}")
        print(f"{'='*72}")
        print(f"System Status        : {result.get('system_status')}")
        print(f"Halted               : {result.get('halted')}")
        if result.get("halted"):
            print(f"Halt Reason          : {result.get('halt_reason')}")
        print(f"Topic State          : {result.get('topic_state')}")
        print(f"Entity State         : {result.get('entity_state')}")
        print(f"Event State          : {result.get('event_state')}")
        print(f"Sentiment State      : {result.get('sentiment_state')}")
        print(f"Sentiment Confidence : {result.get('sentiment_confidence')}")
        print(f"Novelty State        : {result.get('novelty_state')}")
        print(f"Scope State          : {result.get('scope_state')}")
        print(f"Action State         : {result.get('action_state')}")
        print(f"Impact Score         : {result.get('impact_score')}")
        print(f"Final Confidence     : {result.get('final_confidence')}")
        print(f"Composite Score      : {result.get('composite_score')}")
        print(f"Adjusted Score       : {result.get('adjusted_score')}")
        print(f"Final Signal         : {result.get('final_signal')}")
        print(f"Final Routing        : {result.get('final_routing')}")
        print(f"Output Action        : {result.get('output_action')}")
        print(f"Confirmation State   : {result.get('confirmation_state')}")
        print(f"Ambiguity State      : {result.get('ambiguity_state')}")
        print(f"Timing State         : {result.get('timing_state')}")
        print(f"Persistence State    : {result.get('persistence_state')}")
        print(f"Benchmark Rel State  : {result.get('benchmark_rel_state')}")
        print(f"Drawdown State       : {result.get('drawdown_state')}")
        print(f"Trend State          : {result.get('trend_state')}")
        print(f"\nGate records ({len(result.get('decision_log', []))} total):")
        for r in result.get("decision_log", []):
            print(f"  [{r['gate']:>2}] {r['name']:<34}  {r['result']:<28}  {r['reason_code']}")
        print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
