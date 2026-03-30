"""
social_rumor_agent.py — Social & Rumor Flow Agent
Synthos Company Pi | /home/<user>/synthos-company/agents/social_rumor_agent.py

Role:
  Monitor social feeds for market-relevant rumors. Classify every post through
  a 24-gate deterministic decision spine. Forward high-confidence signals to
  the signal queue for Agent 1 (ExecutionAgent) evaluation.

  This agent is accountable for:
    - Processing social posts through explicit, traceable classification logic
    - Detecting rumor source credibility, propagation patterns, and manipulation
    - Mapping claims to tradeable entities with benchmark-relative context
    - Producing a composite score and final signal classification
    - Writing a complete RumorDecisionLog for every post processed

  This agent does NOT trade. It does not issue buy or sell orders.
  All trade decisions are made by Agent 1 (ExecutionAgent).

  This agent does NOT send communications directly.
  All output routes through db_helpers.

  Human-readable logic specification:
    documentation/governance/AGENT4_SYSTEM_DESCRIPTION.md

Data sources:
  - StockTwits public API (free, market-focused)
  - Reddit API (free: r/wallstreetbets, r/investing, r/stocks)
  - Yahoo Finance RSS (supplementary, volume proxy)

Schedule:
  Pre-market:   Every 30 min, 7:00am–9:30am ET
  Market hours: Every 15 min, 9:30am–4:00pm ET
  Post-market:  Every 30 min, 4:00pm–8:00pm ET
  Overnight:    No scan

USAGE:
  python3 social_rumor_agent.py --mode scan     # single scan pass
  python3 social_rumor_agent.py --mode status   # show queue depth and last run
"""

import os
import sys
import json
import time
import logging
import argparse
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── PATH BOOTSTRAP ─────────────────────────────────────────────────────────────

import os.path as _osp
_AGENTS_DIR  = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in sys.path:
    sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import BASE_DIR, DATA_DIR, LOGS_DIR, ENV_PATH
from db_helpers import DB as _DB

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
except ImportError:
    pass

_db = _DB()

# ── LOGGING ────────────────────────────────────────────────────────────────────

LOG_FILE = LOGS_DIR / "social_rumor_agent.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s social_rumor_agent: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("social_rumor_agent")

AGENT_VERSION = "1.0.0"

# ── CONSTANTS — GATE 1: SYSTEM GATE ───────────────────────────────────────────

MAX_SOCIAL_AGE_HOURS          = 4       # posts older than this → stale
DUPLICATE_THRESHOLD           = 0.75    # Jaccard similarity → suppress duplicate
MIN_CHARACTER_COUNT           = 20      # posts shorter → insufficient content
SUPPORTED_LANGUAGES           = {"en"}  # reject all others

# ── CONSTANTS — GATE 2: SOURCE IDENTITY ───────────────────────────────────────

CREDIBILITY_THRESHOLD         = 60      # 0–100 composite source score
IDENTITY_THRESHOLD            = 50      # 0–100 identity confidence
SOURCE_ACCURACY_THRESHOLD     = 0.60    # historical claim accuracy floor

# ── CONSTANTS — GATE 3: ENGAGEMENT / PROPAGATION ──────────────────────────────

REPOST_VELOCITY_THRESHOLD     = 10      # reposts per minute → fast spread
ENGAGEMENT_VELOCITY_THRESHOLD = 20      # engagements per minute → high attention
BREADTH_THRESHOLD             = 25      # unique accounts sharing
CONCENTRATION_THRESHOLD       = 0.80    # top-k share of amplification
BOT_THRESHOLD                 = 0.70    # bot probability floor
DECAY_THRESHOLD_ENGAGE        = 0.25    # relative velocity drop fraction

# ── CONSTANTS — GATE 4: CONTENT CLASSIFICATION ────────────────────────────────

FACT_THRESHOLD                = 0.05    # factual claim density minimum
SATIRE_THRESHOLD              = 0.70    # satire probability floor
CONTENT_CONFIDENCE_THRESHOLD  = 0.55    # minimum type probability for classification
NOVELTY_THRESHOLD_CONTENT     = 0.60    # claim novelty for originating_claim
DETAIL_THRESHOLD              = 0.08    # detail density minimum for specificity

# ── CONSTANTS — GATE 5: CLAIM DETECTION ───────────────────────────────────────

CLAIM_THRESHOLD               = 0.50    # claim extraction confidence minimum

# ── CONSTANTS — GATE 7: RUMOR CONFIRMATION ────────────────────────────────────

MIN_CONFIRMATIONS             = 2       # independent confirmations for strong state
CONTRADICTION_THRESHOLD       = 0.40    # claim variance → contradictory
CONFIRMATION_TIMEOUT_HOURS    = 24      # elapsed hours → expired_unconfirmed

# ── CONSTANTS — GATE 8: SENTIMENT EXTRACTION ──────────────────────────────────

POSITIVE_THRESHOLD            = 0.08    # sentiment score → positive
NEGATIVE_THRESHOLD            = -0.08   # sentiment score → negative
MIXED_FLOOR                   = 0.03    # both polarities above this → mixed
SENTIMENT_CONFIDENCE_THRESHOLD = 0.40
EMOTION_THRESHOLD             = 0.15    # emotion term density → emotionally_charged
PANIC_THRESHOLD               = 0.10    # panic term density → panic_flag

# ── CONSTANTS — GATE 9: NOVELTY / SURPRISE ────────────────────────────────────

NOVELTY_THRESHOLD_SEMANTIC    = 0.65    # Jaccard distance → high novelty
MIN_INCREMENTAL_INFO          = 0.25    # new information score minimum
SURPRISE_THRESHOLD            = 0.20    # event delta → high surprise
PRICED_MOVE_THRESHOLD         = 0.005   # 0.5% pre/post-market move → already priced

# ── CONSTANTS — GATE 10: MANIPULATION / ABUSE ─────────────────────────────────

COORDINATION_THRESHOLD        = 0.85    # message similarity across accounts
FOLLOWER_FLOOR                = 100     # low-follower floor
IMPERSONATION_THRESHOLD       = 0.85    # identity similarity to known account
AUTHENTICITY_THRESHOLD        = 0.40    # media authenticity score minimum

# ── CONSTANTS — GATE 11: MEDIA / ATTACHMENT ───────────────────────────────────

EDIT_THRESHOLD                = 0.60    # forensic edit score → manipulated risk
FORMAT_THRESHOLD              = 0.75    # template similarity → plausible official

# ── CONSTANTS — GATE 12: MARKET IMPACT SCOPE ──────────────────────────────────

PEER_THRESHOLD                = 0.50    # peer transfer likelihood
SCOPE_THRESHOLD               = 0.55    # scope confidence minimum

# ── CONSTANTS — GATE 13: TIME HORIZON ─────────────────────────────────────────

IMMEDIATE_WINDOW_HOURS        = 4       # median decay ≤ this → intraday
SHORT_WINDOW_HOURS            = 48      # median decay ≤ this → multi_day
TRANSIENT_THRESHOLD           = 0.35    # persistence score → fast decay
PERSISTENT_THRESHOLD          = 0.65    # persistence score → slow decay

# ── CONSTANTS — GATE 14: BENCHMARK-RELATIVE ───────────────────────────────────

ALPHA_THRESHOLD               = 0.005   # predicted move minus SPX move → alpha
BETA_BAND                     = 0.003   # asset minus SPX ≤ this → beta

# ── CONSTANTS — GATE 15: CROWDING / SATURATION ────────────────────────────────

CLUSTER_VOLUME_THRESHOLD      = 20      # posts in claim cluster → crowded
ATTENTION_THRESHOLD           = 1000    # combined mentions → extreme_attention
LOW_DISPERSION_THRESHOLD      = 0.10    # sentiment dispersion → one_sided
EXHAUSTION_MOVE_THRESHOLD     = 0.02    # realized market move → exhausted
GROWTH_THRESHOLD              = 5       # cluster growth rate → active_development

# ── CONSTANTS — GATE 16: CONTRADICTION / AMBIGUITY ────────────────────────────

INTERNAL_CONFLICT_THRESHOLD   = 0.50    # claim conflict score
UNCERTAINTY_THRESHOLD_AMB     = 0.12    # uncertainty term density
SARCASM_THRESHOLD             = 0.60    # sarcasm score
CLUSTER_CONFLICT_THRESHOLD    = 0.40    # cross-post claim variance

# ── CONSTANTS — GATE 17: TIMING ───────────────────────────────────────────────

TRADEABLE_SOCIAL_WINDOW_HOURS = 2       # post older than this → expired
BURST_THRESHOLD               = 10      # posts/minute in cluster → active_flow
DECAY_FLOW_THRESHOLD          = 2       # posts/minute → fading

# ── CONSTANTS — GATE 18: IMPACT MAGNITUDE ─────────────────────────────────────

HIGH_IMPACT_THRESHOLD         = 0.030   # predicted absolute move → high
MEDIUM_IMPACT_THRESHOLD       = 0.010   # predicted absolute move → medium
SPX_LINK_THRESHOLD            = 0.60    # historical correlation → benchmark_linked

# ── CONSTANTS — GATE 20: RISK DISCOUNTS ───────────────────────────────────────

LOW_CONFIDENCE_DISCOUNT       = 0.75
ANONYMITY_DISCOUNT            = 0.60
HIGH_VOL_DISCOUNT             = 0.80
MANIPULATION_DISCOUNT         = 0.40
BOT_DISCOUNT                  = 0.50
CONFLICT_UPDATE_THRESHOLD     = 3       # new conflicting posts → freeze

# ── CONSTANTS — GATE 21: PERSISTENCE ──────────────────────────────────────────

STRUCTURAL_THRESHOLD          = 0.70

# ── CONSTANTS — GATE 22: EVALUATION LOOP ──────────────────────────────────────

PREDICTION_ERROR_THRESHOLD    = 0.015   # abs error → high prediction error
ERROR_PENALTY                 = 0.05
ACCURACY_REWARD               = 0.03
ROLLING_ERROR_THRESHOLD       = 0.020   # rolling mean error → downweight class
SOURCE_REWARD                 = 2.0     # added to source_score when accurate
SOURCE_PENALTY                = 2.0     # subtracted from source_score when inaccurate

# ── CONSTANTS — GATE 23: OUTPUT ───────────────────────────────────────────────

HIGH_CONFIDENCE_THRESHOLD     = 0.70
MEDIUM_CONFIDENCE_THRESHOLD   = 0.45

# ── CONSTANTS — GATE 24: FINAL COMPOSITE SCORE ────────────────────────────────

W1_CREDIBILITY                = 0.20
W2_RELEVANCE                  = 0.15
W3_NOVELTY                    = 0.15
W4_CONFIRMATION               = 0.15
W5_PROPAGATION_QUALITY        = 0.10
W6_IMPACT                     = 0.10
W7_AMBIGUITY_PENALTY          = 0.07
W8_MANIPULATION_PENALTY       = 0.05
W9_BOT_PENALTY                = 0.03
BULLISH_SCORE_THRESHOLD       = 0.60
BEARISH_SCORE_THRESHOLD       = 0.30

# ── MARKET HOURS (ET) ──────────────────────────────────────────────────────────

MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 30
MARKET_CLOSE_HOUR  = 16
MARKET_CLOSE_MIN   = 0

# ── KEYWORD TERM SETS ──────────────────────────────────────────────────────────
# All classification uses keyword matching — no ML/AI inference.

UNCONFIRMED_TERMS = {
    "rumor", "rumour", "allegedly", "allegedly", "supposedly", "unconfirmed",
    "sources say", "i heard", "word is", "word on the street", "may be",
    "could be", "might be", "speculating", "speculation", "chatter",
    "whispers", "murmurs", "circulating", "floating around",
}

SPECULATIVE_TERMS = {
    "if true", "if this is real", "take with a grain", "unverified",
    "not confirmed", "reportedly", "according to sources", "per sources",
    "my source", "anonymous source",
}

LEAK_TERMS = {
    "leak", "leaked", "leaking", "insider", "inside source",
    "exclusive", "breaking", "scoop", "tip", "tipster",
}

INSIDER_TERMS = {
    "insider", "inside information", "inside source", "source familiar",
    "people familiar with the matter", "person familiar",
}

SOURCE_SAYS_TERMS = {
    "sources say", "source says", "per source", "source tells",
    "told reuters", "told bloomberg", "told cnbc", "told wsj",
}

OPINION_MARKERS = {
    "i think", "i believe", "in my opinion", "imo", "imho",
    "my take", "my view", "seems like", "feels like", "gut feeling",
    "my guess", "personally", "i feel",
}

DENIAL_TERMS = {
    "denied", "denies", "deny", "false", "not true", "untrue",
    "no truth", "categorically false", "completely false", "refutes",
    "refuted", "debunked", "correction", "incorrect",
}

SATIRE_MARKERS = {
    "satire", "parody", "joke", "just kidding", "jk", "lol",
    "/s", "not financial advice", "nfa", "this is satire",
    "humor", "humour", "satirical",
}

PUMP_TERMS = {
    "to the moon", "moon soon", "buy now", "last chance to buy",
    "going to explode", "massive gains", "10x incoming", "100x",
    "load up", "back up the truck", "all in", "calls only",
    "never selling", "diamond hands", "apes together strong",
    "rocket", "mooning", "stonks only go up",
}

DUMP_TERMS = {
    "going to zero", "this is the end", "sell everything", "get out now",
    "company is done", "bankruptcy incoming", "collapse imminent",
    "short this", "puts only", "house of cards", "fraud exposed",
    "accounting fraud", "massive fraud", "ponzi",
}

UNCERTAINTY_TERMS = {
    "maybe", "perhaps", "possibly", "might", "could", "may",
    "uncertain", "unclear", "unknown", "not sure", "doubt",
    "questionable", "allegedly", "supposedly", "rumored",
}

PANIC_TERMS = {
    "panic", "crash", "collapse", "catastrophe", "disaster",
    "meltdown", "implosion", "bankrupt", "default", "crisis",
    "emergency", "urgent", "breaking", "halt", "circuit breaker",
}

EMOTION_TERMS = {
    "love", "hate", "excited", "thrilled", "terrified", "furious",
    "amazing", "incredible", "unbelievable", "shocked", "outraged",
    "euphoric", "devastated", "ecstatic", "disgusted",
}

POSITIVE_TERMS = {
    "buy", "bullish", "upside", "growth", "beat", "exceeded",
    "strong", "surge", "rally", "breakout", "positive", "upgrade",
    "opportunity", "profit", "gain", "up", "higher", "recover",
    "improve", "expand", "win", "success", "approval", "approved",
}

NEGATIVE_TERMS = {
    "sell", "bearish", "downside", "decline", "miss", "missed",
    "weak", "drop", "fall", "breakdown", "negative", "downgrade",
    "risk", "loss", "down", "lower", "deteriorate", "shrink",
    "lose", "fail", "rejected", "rejected", "concern", "worry",
}

CORPORATE_ACTION_TERMS = {
    "merger", "acquisition", "takeover", "buyout", "ipo", "spin-off",
    "spinoff", "divestiture", "restructuring", "bankruptcy", "delisting",
    "going private", "tender offer", "proxy", "shareholder vote",
}

EARNINGS_TERMS = {
    "earnings", "eps", "revenue", "guidance", "outlook", "forecast",
    "quarterly results", "annual results", "q1", "q2", "q3", "q4",
    "beat", "miss", "above expectations", "below expectations",
    "raised guidance", "lowered guidance",
}

REGULATION_TERMS = {
    "regulation", "regulatory", "sec", "ftc", "doj", "fda", "cftc",
    "investigation", "fine", "penalty", "lawsuit", "antitrust",
    "compliance", "enforcement", "subpoena", "settlement",
}

MACRO_TERMS = {
    "fed", "federal reserve", "interest rate", "inflation", "cpi",
    "gdp", "unemployment", "recession", "rate hike", "rate cut",
    "quantitative easing", "taper", "fiscal", "treasury", "debt ceiling",
}

GEOPOLITICAL_TERMS = {
    "sanctions", "tariffs", "trade war", "election", "geopolitical",
    "military", "war", "conflict", "coup", "embargo", "nato",
    "china", "russia", "middle east", "opec",
}

INDEX_TERMS = {
    "s&p 500", "spx", "spy", "nasdaq", "dow jones", "djia", "vix",
    "russell 2000", "iwm", "qqq", "broad market", "market-wide",
    "all sectors", "entire market",
}

MA_TERMS         = {"merger", "acquisition", "takeover", "buyout", "deal"}
FINANCING_TERMS  = {"debt", "bond", "credit", "financing", "raise capital", "dilution"}
MINOR_PRODUCT    = {"product update", "new feature", "minor release", "patch", "hotfix"}

# ── TEXT HELPERS ───────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set:
    """Lower-case word tokens, stripped of punctuation."""
    return set(re.findall(r"\b[a-z]{2,}\b", text.lower()))

def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two token sets. Returns 0.0 if both empty."""
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)

def _keyword_density(tokens: set, term_set: set) -> float:
    """Fraction of term_set members found in tokens, relative to token count."""
    if not tokens:
        return 0.0
    matches = len(tokens & term_set)
    return matches / len(tokens)

def _keyword_count(tokens: set, term_set: set) -> int:
    return len(tokens & term_set)

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _market_session(ts: datetime) -> str:
    """Classify a UTC timestamp into a market session label."""
    # Convert UTC to ET (approximation: UTC-4 during EDT, UTC-5 during EST)
    # Using fixed UTC-4 offset; DST handling is a known simplification.
    # TODO: DATA_DEPENDENCY — proper timezone library (pytz/zoneinfo) for DST
    et_offset = timedelta(hours=-4)
    et_time = ts + et_offset
    open_minutes  = MARKET_OPEN_HOUR  * 60 + MARKET_OPEN_MIN
    close_minutes = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    current_minutes = et_time.hour * 60 + et_time.minute
    if current_minutes < open_minutes:
        return "premarket"
    if current_minutes > close_minutes:
        return "postmarket"
    return "intraday"

# ── V1 SCHEMA DEFINITIONS ─────────────────────────────────────────────────────

INPUT_SCHEMA = {
    "normalized_social": {"type": "dict", "required": True,  "description": "Normalized social/rumor fields"},
    "run_id":            {"type": "str",  "required": True},
    "timestamp":         {"type": "str",  "required": True,  "description": "ISO UTC snapshot timestamp"},
}

OUTPUT_SCHEMA = {
    "classification":     {"type": "str",  "values": ["bullish_rumor_signal", "bearish_rumor_signal", "relative_alpha_signal", "manipulation_watch", "benchmark_regime_signal", "upgraded_to_confirmed_event", "ignore"]},
    "overall_confidence": {"type": "float", "range": "[0.0, 1.0]"},
    "confirmation_state": {"type": "str",  "values": ["confirmed", "provisional", "contradictory", "unresolved"]},
    "decision_log":       {"type": "list", "description": "Full gate trace"},
}

# ── RUMOR DECISION LOG ─────────────────────────────────────────────────────────

class RumorDecisionLog:
    """
    Accumulates all gate inputs, results, and reason codes for a single post.
    Serialised to JSON and written to the database after Gate 24.
    """

    def __init__(self, post_id: str, source_id: str, post_time: Optional[datetime],
                 post_text: str, run_ts: datetime):
        self.run_ts        = run_ts.isoformat()
        self.post_id       = post_id
        self.source_id     = source_id
        self.post_time     = post_time.isoformat() if post_time else None
        self.post_text     = post_text[:500]  # truncate for log storage
        self.gates         = {}
        self.halted_at     = None
        self.halt_reason   = None
        # Final outputs — populated by the end of the pipeline
        self.classification    = None
        self.final_signal      = None
        self.composite_score   = None
        self.output_mode       = None
        self.output_action     = None
        self.impact_score      = None

    def record(self, gate: int, name: str, inputs: dict, result: str, reason: str):
        self.gates[gate] = {
            "name":   name,
            "inputs": inputs,
            "result": result,
            "reason": reason,
        }

    def halt(self, gate: int, name: str, reason: str):
        self.halted_at  = gate
        self.halt_reason = reason
        self.record(gate, name, {}, "HALT", reason)

    def to_dict(self) -> dict:
        return {
            "run_ts":         self.run_ts,
            "post_id":        self.post_id,
            "source_id":      self.source_id,
            "post_time":      self.post_time,
            "halted_at":      self.halted_at,
            "halt_reason":    self.halt_reason,
            "classification": self.classification,
            "final_signal":   self.final_signal,
            "composite_score": self.composite_score,
            "output_mode":    self.output_mode,
            "output_action":  self.output_action,
            "impact_score":   self.impact_score,
            "gates":          self.gates,
        }

    def to_human_readable(self) -> str:
        lines = [
            f"=== RumorDecisionLog ===",
            f"Run:      {self.run_ts}",
            f"Post ID:  {self.post_id}",
            f"Source:   {self.source_id}",
            f"Posted:   {self.post_time}",
            f"Text:     {self.post_text}",
            f"",
        ]
        for gate_num in sorted(self.gates.keys()):
            g = self.gates[gate_num]
            lines.append(f"  Gate {gate_num:>2} — {g['name']}")
            for k, v in g["inputs"].items():
                lines.append(f"             {k}: {v}")
            lines.append(f"           → result: {g['result']}  ({g['reason']})")
        lines += [
            f"",
            f"Classification:  {self.classification}",
            f"Final signal:    {self.final_signal}",
            f"Composite score: {self.composite_score}",
            f"Output mode:     {self.output_mode}",
            f"Output action:   {self.output_action}",
            f"Impact score:    {self.impact_score}",
        ]
        if self.halted_at:
            lines.append(f"HALTED at gate {self.halted_at}: {self.halt_reason}")
        return "\n".join(lines)


# ── GATE IMPLEMENTATIONS ───────────────────────────────────────────────────────

def gate_1_system(post: dict, run_cache: list, benchmark_available: bool,
                  rdl: RumorDecisionLog) -> bool:
    """
    Gate 1 — System Gate
    Hard rejection checks before any analysis is performed.
    Returns True (PROCEED) or False (HALT).
    """
    now = _now_utc()

    # IF social feed unavailable → halt_ingestion
    if post.get("feed_status") == "error":
        rdl.halt(1, "System Gate", "halt_ingestion: feed unavailable")
        return False

    # IF post parse failure → reject_post
    text = post.get("text") or ""
    if not text.strip():
        rdl.halt(1, "System Gate", "reject_post: post text null or empty")
        return False

    # IF timestamp missing → reject_post
    post_time = post.get("post_time")
    if not post_time:
        rdl.halt(1, "System Gate", "reject_post: timestamp missing")
        return False

    # IF timestamp stale → stale_post
    age_hours = (now - post_time).total_seconds() / 3600
    if age_hours > MAX_SOCIAL_AGE_HOURS:
        rdl.halt(1, "System Gate",
                 f"stale_post: age {age_hours:.1f}h > max {MAX_SOCIAL_AGE_HOURS}h")
        return False

    # IF duplicate post detected → suppress_duplicate
    tokens = _tokenize(text)
    for cached in run_cache:
        sim = _jaccard(tokens, cached)
        if sim > DUPLICATE_THRESHOLD:
            rdl.halt(1, "System Gate",
                     f"suppress_duplicate: Jaccard {sim:.2f} > threshold {DUPLICATE_THRESHOLD}")
            return False

    # IF unsupported language → reject_post
    lang = post.get("language", "en")
    if lang not in SUPPORTED_LANGUAGES:
        rdl.halt(1, "System Gate",
                 f"reject_post: unsupported language '{lang}'")
        return False

    # IF post too short → insufficient_content
    if len(text) < MIN_CHARACTER_COUNT:
        rdl.halt(1, "System Gate",
                 f"insufficient_content: {len(text)} chars < min {MIN_CHARACTER_COUNT}")
        return False

    # IF benchmark data unavailable → benchmark_context_disabled (non-halting)
    benchmark_note = "benchmark_context_disabled" if not benchmark_available else "benchmark_available"

    rdl.record(1, "System Gate",
               {"age_hours": round(age_hours, 2),
                "char_count": len(text),
                "language": lang,
                "benchmark": benchmark_note},
               "PASS", "all system checks passed")
    return True


def gate_2_source_identity(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 2 — Source Identity Controls
    Classifies the source account's identity, credibility, and historical accuracy.
    Returns a dict of source state flags.
    """
    source_score      = post.get("source_score", 50)           # 0–100
    verified          = post.get("account_verification_flag", False)
    account_type      = post.get("account_type", "unknown")
    account_role      = post.get("account_role", "unknown")
    identity_conf     = post.get("account_identity_confidence", 50)
    claim_accuracy    = post.get("historical_claim_accuracy", 0.50)

    states = []

    # IF source is verified
    if verified:
        states.append("verified")
    else:
        states.append("unverified")

    # IF source credibility high / low
    if source_score >= CREDIBILITY_THRESHOLD:
        states.append("credible")
    else:
        states.append("low_credibility")

    # IF source is official company/government account
    if account_type in {"company_official", "regulator_official", "government_official"}:
        states.append("official")

    # IF source is journalist / analyst / domain specialist
    if account_role in {"journalist", "analyst", "researcher", "industry_specialist"}:
        states.append("professional")

    # IF source is anonymous
    if identity_conf < IDENTITY_THRESHOLD:
        states.append("anonymous")

    # IF source has prior rumor accuracy high / low
    if claim_accuracy >= SOURCE_ACCURACY_THRESHOLD:
        states.append("historically_reliable")
    else:
        states.append("historically_unreliable")

    rdl.record(2, "Source Identity",
               {"source_score": source_score,
                "verified": verified,
                "account_type": account_type,
                "account_role": account_role,
                "identity_confidence": identity_conf,
                "historical_accuracy": claim_accuracy},
               "PASS", f"source_states: {states}")
    return {"source_score": source_score, "states": states,
            "is_anonymous": "anonymous" in states}


def gate_3_propagation(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 3 — Engagement / Propagation Controls
    Characterises how the post is spreading.
    Returns propagation state flags.
    """
    reposts_per_min    = post.get("reposts_per_minute", 0)
    engagements_per_min = post.get("engagements_per_minute", 0)
    unique_accounts    = post.get("unique_accounts_sharing", 0)
    top_k_share        = post.get("top_k_accounts_share_of_total_amplification", 0.0)
    bot_prob           = post.get("bot_probability_amplifying", 0.0)
    prev_velocity      = post.get("engagement_velocity_prior_interval", 0)

    states = []

    # IF repost velocity high
    if reposts_per_min > REPOST_VELOCITY_THRESHOLD:
        states.append("fast_spread")

    # IF like/comment velocity high
    if engagements_per_min > ENGAGEMENT_VELOCITY_THRESHOLD:
        states.append("high_attention")

    # IF unique account propagation broad / narrow
    if unique_accounts > BREADTH_THRESHOLD:
        states.append("broad_distribution")
    else:
        states.append("narrow_distribution")

    # IF amplification concentrated
    if top_k_share > CONCENTRATION_THRESHOLD:
        states.append("concentrated_boost")

    # IF bot-like amplification detected
    if bot_prob > BOT_THRESHOLD:
        states.append("synthetic_amplification")

    # IF propagation decaying
    if prev_velocity > 0 and engagements_per_min < prev_velocity * (1 - DECAY_THRESHOLD_ENGAGE):
        states.append("decaying")

    # Propagation quality score: penalise bot and concentrated amplification
    quality = 1.0
    if "synthetic_amplification" in states:
        quality *= 0.40
    if "concentrated_boost" in states:
        quality *= 0.60
    if "broad_distribution" in states:
        quality *= 1.20
    quality = min(quality, 1.0)

    rdl.record(3, "Engagement / Propagation",
               {"reposts_per_min": reposts_per_min,
                "engagements_per_min": engagements_per_min,
                "unique_accounts": unique_accounts,
                "top_k_share": top_k_share,
                "bot_probability": bot_prob},
               "PASS", f"propagation_states: {states}")
    return {"states": states,
            "propagation_quality": quality,
            "synthetic": "synthetic_amplification" in states}


def gate_4_content_classification(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 4 — Content Classification
    Identifies the nature of the post content.
    Returns content_state and whether post should be discarded pre-signal.
    """
    text   = post.get("text", "").lower()
    tokens = _tokenize(text)

    refutation_count = _keyword_count(tokens, DENIAL_TERMS)
    satire_count     = _keyword_count(tokens, SATIRE_MARKERS)
    leak_count       = _keyword_count(tokens, LEAK_TERMS | INSIDER_TERMS | SOURCE_SAYS_TERMS)
    rumor_count      = _keyword_count(tokens, UNCONFIRMED_TERMS | SPECULATIVE_TERMS)
    opinion_count    = _keyword_count(tokens, OPINION_MARKERS)
    fact_density     = _keyword_density(tokens, {"announced", "confirmed", "filed",
                                                  "reported", "disclosed", "stated",
                                                  "according to filing", "per filing"})
    has_existing_ref = bool(post.get("references_existing_claim"))
    novel_score      = post.get("claim_novelty", 0.5)
    total            = len(tokens) or 1

    # Satire probability heuristic (keyword ratio)
    satire_prob = min(satire_count / max(total / 20, 1), 1.0)

    # Evaluate in priority order
    if refutation_count > 0:
        state = "refutation"
    elif satire_prob >= SATIRE_THRESHOLD:
        state = "satire"
    elif leak_count > 0:
        state = "leak"
    elif rumor_count > 0 and not post.get("official_source_present"):
        state = "rumor"
    elif novel_score > NOVELTY_THRESHOLD_CONTENT and not has_existing_ref:
        state = "originating_claim"
    elif has_existing_ref:
        state = "secondary_flow"
    elif opinion_count > 0 and fact_density < FACT_THRESHOLD:
        state = "opinion"
    else:
        state = "uncertain"

    # Satire and opinion: do not advance to action classification
    discard_pre_signal = state in {"satire", "uncertain"}

    rdl.record(4, "Content Classification",
               {"refutation_terms": refutation_count,
                "leak_terms": leak_count,
                "rumor_terms": rumor_count,
                "satire_probability": round(satire_prob, 3),
                "fact_density": round(fact_density, 3),
                "claim_novelty": novel_score},
               state, f"content_state: {state}")
    return {"content_state": state, "discard_pre_signal": discard_pre_signal}


def gate_5_claim_detection(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 5 — Claim Detection
    Extracts and characterises market-relevant claims.
    Returns claim_state, claim_scope, and specificity.
    """
    tokens = _tokenize(post.get("text", ""))

    corporate_hits  = _keyword_count(tokens, CORPORATE_ACTION_TERMS)
    earnings_hits   = _keyword_count(tokens, EARNINGS_TERMS)
    regulation_hits = _keyword_count(tokens, REGULATION_TERMS)
    macro_hits      = _keyword_count(tokens, MACRO_TERMS)
    geo_hits        = _keyword_count(tokens, GEOPOLITICAL_TERMS)
    index_hits      = _keyword_count(tokens, INDEX_TERMS)

    company_count   = post.get("recognized_company_count", 0)
    sector_count    = post.get("recognized_sector_count", 0)
    claim_count     = post.get("claim_count", 1)
    claim_conf      = post.get("claim_extraction_confidence", 0.0)
    has_entities    = company_count > 0 or sector_count > 0
    has_time_metric = bool(post.get("claim_has_time_or_metric_detail"))
    detail_density  = post.get("detail_density", 0.0)

    market_relevant = (corporate_hits + earnings_hits + regulation_hits +
                       macro_hits + geo_hits + index_hits) > 0

    # Claim state
    if claim_conf < CLAIM_THRESHOLD:
        claim_state = "non_actionable"
    elif claim_count > 1:
        claim_state = "multi_claim"
    elif market_relevant:
        claim_state = "market_relevant"
    else:
        claim_state = "non_actionable"

    # Claim scope
    if company_count >= 1:
        claim_scope = "company"
    elif sector_count >= 1:
        claim_scope = "sector"
    elif index_hits > 0 or macro_hits > 0:
        claim_scope = "marketwide"
    else:
        claim_scope = "none"

    # Claim specificity
    if has_entities and has_time_metric:
        specificity = "specific"
    elif not has_entities or detail_density < DETAIL_THRESHOLD:
        specificity = "vague"
    else:
        specificity = "moderate"

    rdl.record(5, "Claim Detection",
               {"claim_confidence": claim_conf,
                "market_relevant_terms": market_relevant,
                "company_count": company_count,
                "sector_count": sector_count,
                "claim_count": claim_count,
                "has_time_metric_detail": has_time_metric},
               claim_state, f"scope: {claim_scope}, specificity: {specificity}")
    return {"claim_state": claim_state, "claim_scope": claim_scope,
            "specificity": specificity}


def gate_6_entity_mapping(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 6 — Entity Mapping
    Maps the claim to tradeable entities and benchmark constituents.
    Returns entity_state and entity list.
    """
    company_count    = post.get("recognized_company_count", 0)
    sector_count     = post.get("recognized_sector_count", 0)
    spx_constituent  = post.get("company_in_spx_constituents", False)
    reg_entity_count = post.get("recognized_regulatory_or_government_entity_count", 0)
    tradeable_count  = post.get("tradeable_entity_count", 0)

    states = []

    if company_count >= 1:
        states.append("company_linked")
        if spx_constituent:
            states.append("benchmark_constituent")
        if company_count > 1:
            states.append("multi_company")

    if sector_count >= 1:
        states.append("sector_linked")

    if reg_entity_count >= 1:
        states.append("policy_linked")

    if tradeable_count == 0:
        states = ["non_actionable"]

    rdl.record(6, "Entity Mapping",
               {"company_count": company_count,
                "sector_count": sector_count,
                "spx_constituent": spx_constituent,
                "regulatory_entity_count": reg_entity_count,
                "tradeable_entity_count": tradeable_count},
               "non_actionable" if "non_actionable" in states else "mapped",
               f"entity_states: {states}")
    return {"entity_states": states,
            "is_non_actionable": "non_actionable" in states,
            "is_benchmark_constituent": "benchmark_constituent" in states}


def gate_7_confirmation(post: dict, run_ts: datetime, rdl: RumorDecisionLog) -> dict:
    """
    Gate 7 — Rumor Confirmation Controls
    Assesses the level of independent confirmation or denial.
    Returns confirmation_state.
    """
    independent_confs    = post.get("independent_confirmations", 0)
    primary_source_absent= not post.get("primary_source_present", False)
    official_filing      = post.get("official_filing_present", False)
    official_statement   = post.get("official_statement_present", False)
    official_denial      = post.get("official_denial_present", False)
    claim_variance       = post.get("claim_variance_across_sources", 0.0)
    first_claim_time     = post.get("first_claim_time")
    elapsed = 0.0
    if first_claim_time:
        elapsed = (run_ts - first_claim_time).total_seconds() / 3600

    # Evaluate in priority order
    if official_denial:
        state = "officially_denied"
    elif official_filing or official_statement:
        state = "primary_confirmed"
    elif claim_variance > CONTRADICTION_THRESHOLD:
        state = "contradictory"
    elif independent_confs >= MIN_CONFIRMATIONS:
        state = "strong"
    elif independent_confs == 1 and primary_source_absent:
        state = "weak"
    elif independent_confs == 0:
        if elapsed > CONFIRMATION_TIMEOUT_HOURS:
            state = "expired_unconfirmed"
        else:
            state = "unconfirmed"
    else:
        state = "unconfirmed"

    rdl.record(7, "Rumor Confirmation",
               {"independent_confirmations": independent_confs,
                "official_filing": official_filing,
                "official_denial": official_denial,
                "claim_variance": claim_variance,
                "elapsed_hours": round(elapsed, 1)},
               state, f"confirmation_state: {state}")
    return {"confirmation_state": state}


def gate_8_sentiment(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 8 — Sentiment Extraction
    Keyword-based directional sentiment scoring. No ML inference.
    Returns sentiment_state, score, and confidence.
    """
    tokens = _tokenize(post.get("text", ""))
    total  = len(tokens) or 1

    pos_count    = _keyword_count(tokens, POSITIVE_TERMS)
    neg_count    = _keyword_count(tokens, NEGATIVE_TERMS)
    emotion_count = _keyword_count(tokens, EMOTION_TERMS)
    panic_count  = _keyword_count(tokens, PANIC_TERMS)

    score        = (pos_count - neg_count) / total
    confidence   = min((pos_count + neg_count) / max(total / 15, 1), 1.0)
    emotion_dens = emotion_count / total
    panic_dens   = panic_count / total
    pos_dens     = pos_count / total
    neg_dens     = neg_count / total

    # Determine sentiment state
    if panic_dens > PANIC_THRESHOLD:
        state = "panic_flag"
    elif emotion_dens > EMOTION_THRESHOLD:
        state = "emotionally_charged"
    elif confidence < SENTIMENT_CONFIDENCE_THRESHOLD:
        state = "uncertain"
    elif pos_dens > MIXED_FLOOR and neg_dens > MIXED_FLOOR:
        state = "mixed"
    elif score > POSITIVE_THRESHOLD:
        state = "positive"
    elif score < NEGATIVE_THRESHOLD:
        state = "negative"
    else:
        state = "neutral"

    rdl.record(8, "Sentiment Extraction",
               {"sentiment_score": round(score, 4),
                "sentiment_confidence": round(confidence, 3),
                "positive_count": pos_count,
                "negative_count": neg_count,
                "emotion_density": round(emotion_dens, 4),
                "panic_density": round(panic_dens, 4)},
               state, f"sentiment_state: {state}")
    return {"sentiment_state": state,
            "sentiment_score": score,
            "sentiment_confidence": confidence,
            "low_confidence": confidence < SENTIMENT_CONFIDENCE_THRESHOLD}


def gate_9_novelty(post: dict, prior_cluster_tokens: list,
                   rdl: RumorDecisionLog) -> dict:
    """
    Gate 9 — Novelty / Surprise Controls
    Detects whether the post adds new information relative to the claim cluster.
    prior_cluster_tokens: list of token sets from recent posts in same cluster.
    Returns novelty_state, information_state, surprise_state.
    """
    tokens = _tokenize(post.get("text", ""))

    # Semantic novelty via Jaccard distance (1 - max similarity to prior cluster)
    max_sim = 0.0
    for prior in prior_cluster_tokens:
        sim = _jaccard(tokens, prior)
        if sim > max_sim:
            max_sim = sim
    novelty_score = 1.0 - max_sim

    # Novelty state
    if novelty_score > NOVELTY_THRESHOLD_SEMANTIC:
        novelty_state = "high"
    else:
        novelty_state = "low"

    # Incremental information
    new_info_score = novelty_score  # proxy: novelty = new information
    if new_info_score >= MIN_INCREMENTAL_INFO:
        information_state = "incremental"
    else:
        information_state = "repetitive"

    # Surprise level
    event_delta   = post.get("actual_or_alleged_event_delta", 0.0)
    if event_delta > SURPRISE_THRESHOLD:
        surprise_state = "high"
    else:
        surprise_state = "normal"

    # Already priced check
    pre_post_move = abs(post.get("pre_post_market_move", 0.0))
    pricing_state = "unknown"
    if pre_post_move > PRICED_MOVE_THRESHOLD and novelty_state == "low":
        pricing_state = "already_priced"

    rdl.record(9, "Novelty / Surprise",
               {"novelty_score": round(novelty_score, 3),
                "max_cluster_similarity": round(max_sim, 3),
                "new_information_score": round(new_info_score, 3),
                "event_delta": event_delta,
                "pre_post_market_move": pre_post_move},
               novelty_state,
               f"novelty: {novelty_state}, info: {information_state}, "
               f"surprise: {surprise_state}, pricing: {pricing_state}")
    return {"novelty_state": novelty_state,
            "information_state": information_state,
            "surprise_state": surprise_state,
            "pricing_state": pricing_state}


def gate_10_manipulation(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 10 — Manipulation / Abuse Controls
    Detects coordinated manipulation, automated amplification, and deceptive patterns.
    Returns abuse_states list. Empty list = no abuse detected.
    """
    tokens = _tokenize(post.get("text", ""))

    pump_hits    = _keyword_count(tokens, PUMP_TERMS)
    dump_hits    = _keyword_count(tokens, DUMP_TERMS)
    coord_sim    = post.get("message_similarity_across_accounts", 0.0)
    bot_prob     = post.get("bot_probability_source", 0.0)
    follower_cnt = post.get("follower_count", 9999)
    fast_spread  = post.get("propagation_state_fast_spread", False)
    verified     = post.get("account_verification_flag", False)
    imperson_sim = post.get("identity_similarity_to_known_account", 0.0)
    media_auth   = post.get("media_authenticity_score", 1.0)

    abuse_states = []

    # IF pump language detected
    if pump_hits > 0:
        abuse_states.append("pump_risk")

    # IF dump language detected
    if dump_hits > 0:
        abuse_states.append("dump_risk")

    # IF coordination pattern detected
    if coord_sim > COORDINATION_THRESHOLD:
        abuse_states.append("coordinated_campaign")

    # IF bot probability high
    if bot_prob > BOT_THRESHOLD:
        abuse_states.append("automation_risk")

    # IF low-follower source with abnormal reach
    if follower_cnt < FOLLOWER_FLOOR and fast_spread:
        abuse_states.append("suspicious_amplification")

    # IF impersonation risk detected
    if imperson_sim > IMPERSONATION_THRESHOLD and not verified:
        abuse_states.append("impersonation_risk")

    # IF fabricated evidence risk detected
    if media_auth < AUTHENTICITY_THRESHOLD:
        abuse_states.append("fabricated_media_risk")

    rdl.record(10, "Manipulation / Abuse",
               {"pump_terms": pump_hits,
                "dump_terms": dump_hits,
                "coordination_similarity": coord_sim,
                "bot_probability": bot_prob,
                "follower_count": follower_cnt,
                "media_authenticity": media_auth},
               "abuse_detected" if abuse_states else "clean",
               f"abuse_states: {abuse_states}")
    return {"abuse_states": abuse_states,
            "has_abuse": len(abuse_states) > 0}


def gate_11_media(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 11 — Media / Attachment Controls
    Classifies any attached evidence and flags authenticity risks.
    """
    attachment_count = post.get("attachment_count", 0)
    attachment_type  = post.get("attachment_type", None)
    parse_status     = post.get("attachment_parse_status", None)
    edit_score       = post.get("forensic_edit_score", 0.0)
    format_sim       = post.get("template_similarity_to_official", 0.0)
    provenance       = post.get("original_source_of_attachment", None)

    if attachment_count == 0:
        state = "text_only"
    elif parse_status and parse_status != "success":
        state = "unreadable_attachment"
    elif edit_score > EDIT_THRESHOLD:
        state = "manipulated_attachment_risk"
    elif format_sim > FORMAT_THRESHOLD:
        state = "plausible_official_format"
    elif provenance is None:
        state = "unknown_provenance"
    else:
        state = "attached_evidence"

    rdl.record(11, "Media / Attachment",
               {"attachment_count": attachment_count,
                "attachment_type": attachment_type,
                "parse_status": parse_status,
                "edit_score": edit_score,
                "format_similarity": format_sim},
               state, f"media_state: {state}")
    return {"media_state": state}


def gate_12_impact_scope(post: dict, claim_scope: str,
                         rdl: RumorDecisionLog) -> dict:
    """
    Gate 12 — Market Impact Scope
    Classifies the expected breadth of market impact.
    """
    company_count       = post.get("recognized_company_count", 0)
    peer_likelihood     = post.get("peer_transfer_likelihood", 0.0)
    policy_entity       = post.get("recognized_policy_entity", False)
    scope_confidence    = post.get("scope_confidence", 0.80)

    if scope_confidence < SCOPE_THRESHOLD:
        impact_scope = "unclear"
    elif claim_scope == "marketwide" or policy_entity:
        impact_scope = "marketwide"
    elif claim_scope == "sector":
        impact_scope = "sector_only"
    elif claim_scope == "company" and company_count == 1:
        if peer_likelihood > PEER_THRESHOLD:
            impact_scope = "peer_group"
        else:
            impact_scope = "single_name"
    elif claim_scope == "company" and company_count > 1:
        impact_scope = "peer_group"
    else:
        impact_scope = "unclear"

    rdl.record(12, "Market Impact Scope",
               {"claim_scope": claim_scope,
                "company_count": company_count,
                "peer_transfer_likelihood": peer_likelihood,
                "policy_entity": policy_entity,
                "scope_confidence": scope_confidence},
               impact_scope, f"impact_scope: {impact_scope}")
    return {"impact_scope": impact_scope}


def gate_13_time_horizon(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 13 — Time Horizon
    Classifies the expected duration and decay rate of the rumor's market impact.
    """
    median_decay_hours = post.get("historical_rumor_decay_median_hours",
                                  IMMEDIATE_WINDOW_HOURS)  # default: intraday
    persistence_score  = post.get("persistence_score", 0.40)

    # Horizon state
    if median_decay_hours <= IMMEDIATE_WINDOW_HOURS:
        horizon_state = "intraday"
    elif median_decay_hours <= SHORT_WINDOW_HOURS:
        horizon_state = "multi_day"
    else:
        horizon_state = "persistent"

    # Decay rate
    if persistence_score < TRANSIENT_THRESHOLD:
        decay_state = "fast_decay"
    elif persistence_score >= PERSISTENT_THRESHOLD:
        decay_state = "slow_decay"
    else:
        decay_state = "moderate_decay"

    rdl.record(13, "Time Horizon",
               {"median_decay_hours": median_decay_hours,
                "persistence_score": persistence_score},
               horizon_state,
               f"horizon: {horizon_state}, decay: {decay_state}")
    return {"horizon_state": horizon_state,
            "decay_state": decay_state,
            "persistence_score": persistence_score}


def gate_14_benchmark_relative(post: dict, sentiment_state: str,
                                benchmark: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 14 — Benchmark-Relative Interpretation
    Adjusts signal interpretation based on the current SPX regime.
    benchmark: dict with keys trend (UP/DOWN/NEUTRAL), vol (HIGH/NORMAL)
    """
    spx_trend      = benchmark.get("trend", "NEUTRAL")
    benchmark_state = "bullish" if spx_trend == "UP" else \
                      "bearish" if spx_trend == "DOWN" else "neutral"

    predicted_asset_move = post.get("predicted_asset_move", 0.0)
    predicted_spx_move   = post.get("predicted_spx_move", 0.0)
    diff                 = predicted_asset_move - predicted_spx_move

    # Relative state
    if sentiment_state == "positive" and benchmark_state == "bullish":
        relative_state = "aligned_positive"
    elif sentiment_state == "negative" and benchmark_state == "bearish":
        relative_state = "aligned_negative"
    elif sentiment_state == "positive" and benchmark_state == "bearish":
        relative_state = "countertrend_positive"
    elif sentiment_state == "negative" and benchmark_state == "bullish":
        relative_state = "countertrend_negative"
    else:
        relative_state = "neutral_context"

    # Signal type
    if abs(diff) > ALPHA_THRESHOLD:
        signal_type = "alpha"
    elif abs(diff) <= BETA_BAND:
        signal_type = "beta"
    else:
        signal_type = "mixed"

    # Dominance
    benchmark_weight     = post.get("benchmark_weight", 0.5)
    rumor_specific_weight = post.get("rumor_specific_weight", 0.5)
    if benchmark_weight > rumor_specific_weight:
        interpretation_state = "benchmark_dominant"
    else:
        interpretation_state = "idiosyncratic_dominant"

    rdl.record(14, "Benchmark-Relative Interpretation",
               {"spx_trend": spx_trend,
                "sentiment_state": sentiment_state,
                "predicted_asset_move": predicted_asset_move,
                "predicted_spx_move": predicted_spx_move,
                "diff": round(diff, 4),
                "benchmark_weight": benchmark_weight,
                "rumor_specific_weight": rumor_specific_weight},
               relative_state,
               f"signal_type: {signal_type}, interpretation: {interpretation_state}")
    return {"benchmark_state": benchmark_state,
            "relative_state": relative_state,
            "signal_type": signal_type,
            "interpretation_state": interpretation_state}


def gate_15_crowding(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 15 — Crowding / Saturation Controls
    Detects overcrowded clusters, extreme attention, and exhausted signals.
    """
    cluster_posts        = post.get("posts_in_claim_cluster", 0)
    mention_count        = post.get("combined_mention_count", 0)
    sentiment_dispersion = post.get("sentiment_dispersion", 1.0)
    realized_move        = abs(post.get("realized_market_move", 0.0))
    cluster_growth_rate  = post.get("cluster_growth_rate", 0)
    novelty_state        = post.get("_novelty_state", "high")  # passed from gate 9

    states = []

    if cluster_posts > CLUSTER_VOLUME_THRESHOLD:
        states.append("crowded")

    if mention_count > ATTENTION_THRESHOLD:
        states.append("extreme_attention")

    if sentiment_dispersion < LOW_DISPERSION_THRESHOLD:
        states.append("one_sided")

    if realized_move > EXHAUSTION_MOVE_THRESHOLD and novelty_state == "low":
        states.append("exhausted")

    if cluster_growth_rate > GROWTH_THRESHOLD:
        states.append("active_development")

    if not states:
        states.append("normal")

    rdl.record(15, "Crowding / Saturation",
               {"cluster_posts": cluster_posts,
                "mention_count": mention_count,
                "sentiment_dispersion": sentiment_dispersion,
                "realized_move": realized_move,
                "cluster_growth_rate": cluster_growth_rate},
               states[0], f"crowding_states: {states}")
    return {"crowding_states": states}


def gate_16_ambiguity(post: dict, rdl: RumorDecisionLog) -> dict:
    """
    Gate 16 — Contradiction / Ambiguity Controls
    Detects internally inconsistent, hedged, sarcastic, or cluster-conflicted posts.
    """
    tokens = _tokenize(post.get("text", ""))
    total  = len(tokens) or 1

    internal_conflict   = post.get("claim_conflict_score", 0.0)
    uncertainty_density = _keyword_count(tokens, UNCERTAINTY_TERMS) / total
    sarcasm_score       = post.get("sarcasm_score", 0.0)
    cluster_variance    = post.get("cross_post_claim_variance", 0.0)

    # Evaluate — most severe state wins
    if cluster_variance > CLUSTER_CONFLICT_THRESHOLD:
        state = "cluster_conflicted"
    elif internal_conflict > INTERNAL_CONFLICT_THRESHOLD:
        state = "internally_conflicted"
    elif sarcasm_score > SARCASM_THRESHOLD:
        state = "sarcasm_risk"
    elif uncertainty_density > UNCERTAINTY_THRESHOLD_AMB:
        state = "hedged_language"
    else:
        state = "clear"

    rdl.record(16, "Contradiction / Ambiguity",
               {"internal_conflict": internal_conflict,
                "uncertainty_density": round(uncertainty_density, 4),
                "sarcasm_score": sarcasm_score,
                "cluster_variance": cluster_variance},
               state, f"ambiguity_state: {state}")
    return {"ambiguity_state": state}


def gate_17_timing(post: dict, run_ts: datetime, rdl: RumorDecisionLog) -> dict:
    """
    Gate 17 — Timing Controls
    Classifies when the post was published and whether it is still tradeable.
    Returns timing_state and expired flag.
    """
    post_time           = post.get("post_time")
    cluster_posts_pm    = post.get("cluster_posts_per_minute", 0)

    age_hours = (run_ts - post_time).total_seconds() / 3600 if post_time else 99

    # Expired check first
    if age_hours > TRADEABLE_SOCIAL_WINDOW_HOURS:
        rdl.record(17, "Timing Controls",
                   {"post_age_hours": round(age_hours, 2),
                    "cluster_posts_per_minute": cluster_posts_pm},
                   "expired", "timing_state: expired — post too old to trade")
        return {"timing_state": "expired", "is_expired": True}

    # Session classification
    session = _market_session(post_time) if post_time else "unknown"

    # Flow state
    if cluster_posts_pm > BURST_THRESHOLD:
        flow_state = "active_flow"
    elif cluster_posts_pm < DECAY_FLOW_THRESHOLD:
        flow_state = "fading"
    else:
        flow_state = "normal_flow"

    rdl.record(17, "Timing Controls",
               {"post_age_hours": round(age_hours, 2),
                "session": session,
                "cluster_posts_per_minute": cluster_posts_pm},
               session, f"timing: {session}, flow: {flow_state}")
    return {"timing_state": session, "flow_state": flow_state, "is_expired": False}


def gate_18_impact_magnitude(post: dict, benchmark: dict,
                              rdl: RumorDecisionLog) -> dict:
    """
    Gate 18 — Impact Magnitude Estimation
    Estimates the expected absolute price impact and benchmark linkage.
    """
    predicted_move_abs  = abs(post.get("predicted_move", 0.0))
    historical_corr     = post.get("historical_corr_rumor_class_spx", 0.0)
    benchmark_vol       = benchmark.get("vol", "NORMAL")

    # Magnitude
    if predicted_move_abs > HIGH_IMPACT_THRESHOLD:
        impact_state = "high"
    elif predicted_move_abs > MEDIUM_IMPACT_THRESHOLD:
        impact_state = "medium"
    else:
        impact_state = "low"

    # Elevate if benchmark vol is high
    if benchmark_vol == "HIGH" and impact_state == "medium":
        impact_state = "high"

    # Benchmark linkage
    if historical_corr > SPX_LINK_THRESHOLD:
        impact_link_state = "benchmark_linked"
    else:
        impact_link_state = "benchmark_weak"

    rdl.record(18, "Impact Magnitude",
               {"predicted_move_abs": round(predicted_move_abs, 4),
                "historical_spx_corr": historical_corr,
                "benchmark_vol": benchmark_vol},
               impact_state,
               f"impact: {impact_state}, spx_linkage: {impact_link_state}")
    return {"impact_state": impact_state,
            "impact_link_state": impact_link_state,
            "predicted_move_abs": predicted_move_abs}


def gate_19_action_classification(source_score: float, novelty_state: str,
                                   claim_state: str, sentiment_state: str,
                                   confirmation_state: str, impact_scope: str,
                                   impact_link_state: str, benchmark_state: str,
                                   signal_type: str, abuse_states: list,
                                   ambiguity_state: str,
                                   rdl: RumorDecisionLog) -> dict:
    """
    Gate 19 — Action Classification
    Combines all upstream results into a single classification label.
    Priority order matches AGENT4_SYSTEM_DESCRIPTION.md Gate 19 table.
    """
    # Priority 1: Official denial
    if confirmation_state == "officially_denied":
        classification = "deny_and_discount"

    # Priority 2: Manipulation
    elif len(abuse_states) > 0:
        classification = "manipulation_watch"

    # Priority 3: Freeze conditions
    elif confirmation_state == "contradictory" or ambiguity_state == "cluster_conflicted":
        classification = "freeze"

    # Priority 4: Confirmed event
    elif confirmation_state == "primary_confirmed" and claim_state == "market_relevant":
        classification = "upgraded_to_confirmed_event"

    # Priority 5: Benchmark regime signal
    elif impact_scope == "marketwide" and impact_link_state == "benchmark_linked":
        classification = "benchmark_regime_signal"

    # Priority 6: Relative alpha signal
    elif (impact_scope == "single_name" and benchmark_state == "neutral"
          and signal_type == "alpha"):
        classification = "relative_alpha_signal"

    # Priority 7: Bullish rumor signal
    elif (source_score >= CREDIBILITY_THRESHOLD and novelty_state == "high"
          and claim_state == "market_relevant" and sentiment_state == "positive"):
        classification = "bullish_rumor_signal"

    # Priority 8: Bearish rumor signal
    elif (source_score >= CREDIBILITY_THRESHOLD and novelty_state == "high"
          and claim_state == "market_relevant" and sentiment_state == "negative"):
        classification = "bearish_rumor_signal"

    # Priority 9: Mixed sentiment and high ambiguity
    elif sentiment_state == "mixed" and ambiguity_state != "clear":
        classification = "watch_only"

    # Priority 10: Low credibility and no confirmation
    elif (source_score < CREDIBILITY_THRESHOLD
          and confirmation_state in {"unconfirmed", "weak"}):
        classification = "ignore"

    # Default
    else:
        classification = "watch_only"

    rdl.record(19, "Action Classification",
               {"source_score": source_score,
                "novelty_state": novelty_state,
                "claim_state": claim_state,
                "sentiment_state": sentiment_state,
                "confirmation_state": confirmation_state,
                "impact_scope": impact_scope,
                "impact_link_state": impact_link_state,
                "benchmark_state": benchmark_state,
                "signal_type": signal_type,
                "abuse_states": abuse_states,
                "ambiguity_state": ambiguity_state},
               classification, f"classification: {classification}")
    return {"classification": classification}


def gate_20_risk_discounts(classification: str, abuse_states: list,
                            sentiment_confidence: float, is_anonymous: bool,
                            propagation_states: list, benchmark_vol: str,
                            new_conflicting_posts: int,
                            base_impact_score: float,
                            rdl: RumorDecisionLog) -> dict:
    """
    Gate 20 — Risk Discounts
    Applies multiplicative discounts to the impact score.
    """
    score = base_impact_score
    applied = []

    # IF sentiment confidence low
    if sentiment_confidence < SENTIMENT_CONFIDENCE_THRESHOLD:
        score *= LOW_CONFIDENCE_DISCOUNT
        applied.append(f"low_confidence_discount×{LOW_CONFIDENCE_DISCOUNT}")

    # IF source anonymous
    if is_anonymous:
        score *= ANONYMITY_DISCOUNT
        applied.append(f"anonymity_discount×{ANONYMITY_DISCOUNT}")

    # IF benchmark volatility high
    if benchmark_vol == "HIGH":
        score *= HIGH_VOL_DISCOUNT
        applied.append(f"high_vol_discount×{HIGH_VOL_DISCOUNT}")

    # IF manipulation risk present
    if len(abuse_states) > 0:
        score *= MANIPULATION_DISCOUNT
        applied.append(f"manipulation_discount×{MANIPULATION_DISCOUNT}")

    # IF bot amplification high
    if "synthetic_amplification" in propagation_states:
        score *= BOT_DISCOUNT
        applied.append(f"bot_discount×{BOT_DISCOUNT}")

    # IF contradictory updates arriving → override to freeze
    final_classification = classification
    if new_conflicting_posts > CONFLICT_UPDATE_THRESHOLD:
        final_classification = "freeze"
        applied.append("freeze_override: conflicting_update_count exceeded")

    rdl.record(20, "Risk Discounts",
               {"base_impact_score": round(base_impact_score, 4),
                "discounted_score": round(score, 4),
                "new_conflicting_posts": new_conflicting_posts,
                "discounts_applied": applied},
               "discounted" if applied else "no_discount",
               f"final_impact_score: {round(score, 4)}, "
               f"final_classification: {final_classification}")
    return {"impact_score": score,
            "classification": final_classification}


def gate_21_persistence(post: dict, confirmation_state: str,
                         rdl: RumorDecisionLog) -> dict:
    """
    Gate 21 — Persistence Controls
    Classifies the expected decay rate of the rumor's market impact.
    """
    tokens            = _tokenize(post.get("text", ""))
    persistence_score = post.get("persistence_score", 0.40)

    has_ma          = bool(_keyword_count(tokens, MA_TERMS))
    has_financing   = bool(_keyword_count(tokens, FINANCING_TERMS))
    has_regulation  = bool(_keyword_count(tokens, REGULATION_TERMS))
    has_minor_prod  = bool(_keyword_count(tokens, MINOR_PRODUCT))
    no_confirmation = confirmation_state not in {"primary_confirmed", "strong"}

    # Evaluate in priority order
    if confirmation_state == "primary_confirmed":
        expected_decay = "recompute_persistence"
    elif has_ma or has_financing or has_regulation:
        expected_decay = "persistent_candidate"
    elif has_minor_prod and no_confirmation:
        expected_decay = "likely_fast"
    elif persistence_score >= STRUCTURAL_THRESHOLD:
        expected_decay = "slow"
    elif persistence_score < TRANSIENT_THRESHOLD:
        expected_decay = "rapid"
    else:
        expected_decay = "moderate"

    rdl.record(21, "Persistence Controls",
               {"persistence_score": persistence_score,
                "has_ma_or_financing_or_reg": has_ma or has_financing or has_regulation,
                "has_minor_product": has_minor_prod,
                "confirmation_state": confirmation_state},
               expected_decay, f"expected_decay: {expected_decay}")
    return {"expected_decay": expected_decay}


def gate_22_evaluation_loop(post: dict, classification: str,
                              rdl: RumorDecisionLog) -> dict:
    """
    Gate 22 — Evaluation Loop
    Updates model scores and source credibility weights based on prediction accuracy.
    Stores rumor state for later post-trade comparison.

    NOTE: Full post-trade outcome feedback requires Agent 1 integration.
    TODO: DATA_DEPENDENCY — tracked in AGENT4_SYSTEM_DESCRIPTION.md
    """
    predicted_move = post.get("predicted_move", None)
    realized_move  = post.get("realized_move_outcome", None)   # from prior evaluation
    source_id      = post.get("source_id", "unknown")
    rumor_class    = post.get("rumor_class", "general")

    source_score_delta = 0.0
    class_score_delta  = 0.0
    evaluation_note    = "no_prior_outcome_available"

    # IF prediction error high / low — only evaluated if realized move available
    if predicted_move is not None and realized_move is not None:
        error = abs(predicted_move - realized_move)
        if error > PREDICTION_ERROR_THRESHOLD:
            class_score_delta  = -ERROR_PENALTY
            source_score_delta = -SOURCE_PENALTY
            evaluation_note    = f"high_error: predicted {predicted_move:.4f}, realized {realized_move:.4f}"
        else:
            class_score_delta  = ACCURACY_REWARD
            source_score_delta = SOURCE_REWARD
            evaluation_note    = f"low_error: predicted {predicted_move:.4f}, realized {realized_move:.4f}"

    # Store rumor state for later comparison if not ignored
    store_for_evaluation = classification not in {"ignore", "reject_post"}

    rdl.record(22, "Evaluation Loop",
               {"predicted_move": predicted_move,
                "realized_move": realized_move,
                "source_score_delta": source_score_delta,
                "class_score_delta": class_score_delta,
                "store_for_evaluation": store_for_evaluation},
               "evaluated" if realized_move is not None else "pending_outcome",
               evaluation_note)
    return {"store_for_evaluation": store_for_evaluation,
            "source_score_delta": source_score_delta,
            "class_score_delta": class_score_delta}


def gate_23_output_controls(classification: str, overall_confidence: float,
                              interpretation_state: str,
                              rdl: RumorDecisionLog) -> dict:
    """
    Gate 23 — Output Controls
    Determines output mode (decisive/probabilistic/uncertain) and output action.
    """
    # Output mode by confidence
    if overall_confidence >= HIGH_CONFIDENCE_THRESHOLD:
        output_mode = "decisive"
    elif overall_confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
        output_mode = "probabilistic"
    else:
        output_mode = "uncertain"

    # Output priority by interpretation state
    if interpretation_state == "benchmark_dominant":
        output_priority = "benchmark_first"
    else:
        output_priority = "rumor_first"

    # Output action by classification
    ACTION_MAP = {
        "freeze":                    "wait_for_confirmation",
        "ignore":                    "no_signal",
        "deny_and_discount":         "no_signal",
        "bullish_rumor_signal":      "positive_signal",
        "bearish_rumor_signal":      "negative_signal",
        "benchmark_regime_signal":   "benchmark_context_signal",
        "relative_alpha_signal":     "idiosyncratic_alpha_signal",
        "manipulation_watch":        "monitor_for_abuse",
        "upgraded_to_confirmed_event": "transition_to_news_event_logic",
        "watch_only":                "monitor",
    }
    output_action = ACTION_MAP.get(classification, "monitor")

    rdl.record(23, "Output Controls",
               {"classification": classification,
                "overall_confidence": round(overall_confidence, 3),
                "interpretation_state": interpretation_state},
               output_mode,
               f"output_mode: {output_mode}, action: {output_action}, "
               f"priority: {output_priority}")
    return {"output_mode": output_mode,
            "output_action": output_action,
            "output_priority": output_priority}


def gate_24_composite_score(source_score: float, claim_state: str,
                              novelty_state: str, confirmation_state: str,
                              propagation_quality: float, impact_state: str,
                              ambiguity_state: str, abuse_states: list,
                              propagation_states: list, sentiment_state: str,
                              benchmark_risk_state: str,
                              impact_link_state: str, classification: str,
                              rdl: RumorDecisionLog) -> dict:
    """
    Gate 24 — Final Composite Score
    Computes the weighted composite score and final_signal.
    """
    # Normalise components to 0–1
    credibility_n       = min(source_score / 100.0, 1.0)
    relevance_n         = 1.0 if claim_state == "market_relevant" else 0.3
    novelty_n           = 1.0 if novelty_state == "high" else 0.2
    confirmation_map    = {"primary_confirmed": 1.0, "strong": 0.85, "weak": 0.45,
                           "unconfirmed": 0.20, "contradictory": 0.10,
                           "expired_unconfirmed": 0.05, "officially_denied": 0.0}
    confirmation_n      = confirmation_map.get(confirmation_state, 0.20)
    propagation_n       = propagation_quality
    impact_map          = {"high": 1.0, "medium": 0.55, "low": 0.20}
    impact_n            = impact_map.get(impact_state, 0.20)
    ambiguity_pen       = 0.0 if ambiguity_state == "clear" else 0.60
    manipulation_pen    = min(len(abuse_states) * 0.30, 1.0)
    bot_pen             = 1.0 if "synthetic_amplification" in propagation_states else 0.0

    composite = (
        W1_CREDIBILITY        * credibility_n
      + W2_RELEVANCE          * relevance_n
      + W3_NOVELTY            * novelty_n
      + W4_CONFIRMATION       * confirmation_n
      + W5_PROPAGATION_QUALITY * propagation_n
      + W6_IMPACT             * impact_n
      - W7_AMBIGUITY_PENALTY  * ambiguity_pen
      - W8_MANIPULATION_PENALTY * manipulation_pen
      - W9_BOT_PENALTY        * bot_pen
    )
    composite = max(0.0, min(composite, 1.0))

    # Final signal — priority order matches Gate 24 spec
    if confirmation_state == "officially_denied":
        final_signal = "denial_override"
    elif classification == "manipulation_watch" and len(abuse_states) > 0:
        final_signal = "suppress_or_discount"
    elif benchmark_risk_state == "drawdown" and impact_link_state == "benchmark_linked":
        final_signal = "benchmark_override"
    elif composite >= BULLISH_SCORE_THRESHOLD and sentiment_state == "positive":
        final_signal = "bullish"
    elif composite <= BEARISH_SCORE_THRESHOLD and sentiment_state == "negative":
        final_signal = "bearish"
    else:
        final_signal = "neutral_or_watch"

    components = {
        "credibility":        round(W1_CREDIBILITY * credibility_n, 4),
        "relevance":          round(W2_RELEVANCE * relevance_n, 4),
        "novelty":            round(W3_NOVELTY * novelty_n, 4),
        "confirmation":       round(W4_CONFIRMATION * confirmation_n, 4),
        "propagation_quality": round(W5_PROPAGATION_QUALITY * propagation_n, 4),
        "impact":             round(W6_IMPACT * impact_n, 4),
        "ambiguity_penalty":  round(W7_AMBIGUITY_PENALTY * ambiguity_pen, 4),
        "manipulation_penalty": round(W8_MANIPULATION_PENALTY * manipulation_pen, 4),
        "bot_penalty":        round(W9_BOT_PENALTY * bot_pen, 4),
    }

    rdl.record(24, "Final Composite Score",
               {"components": components,
                "composite_score": round(composite, 4),
                "sentiment_state": sentiment_state,
                "benchmark_risk_state": benchmark_risk_state},
               final_signal,
               f"composite: {round(composite, 4)}, final_signal: {final_signal}")
    return {"composite_score": composite, "final_signal": final_signal}


# ── PIPELINE ORCHESTRATOR ──────────────────────────────────────────────────────

def process_post(post: dict, run_ts: datetime, run_cache: list,
                 benchmark: dict) -> dict:
    """
    Run a single social post through all 24 gates.

    post:       dict with all post fields (see gate implementations for keys)
    run_ts:     UTC timestamp for this run
    run_cache:  list of token sets from posts processed in this run (for dedup)
    benchmark:  dict with keys: trend (UP/DOWN/NEUTRAL), vol (HIGH/NORMAL),
                drawdown_active (bool)

    Returns the complete RumorDecisionLog as a dict.
    """
    source_id = post.get("source_id", "unknown")
    post_id   = post.get("post_id", hashlib.md5(
        post.get("text", "").encode()).hexdigest()[:12])
    post_time = post.get("post_time")
    text      = post.get("text", "")

    rdl = RumorDecisionLog(post_id, source_id, post_time, text, run_ts)

    benchmark_available = bool(benchmark)

    # ── GATE 1: System Gate ────────────────────────────────────────────────────
    if not gate_1_system(post, run_cache, benchmark_available, rdl):
        return rdl.to_dict()

    # Add this post's tokens to run_cache for subsequent dedup
    run_cache.append(_tokenize(text))

    # ── GATE 2: Source Identity ────────────────────────────────────────────────
    g2 = gate_2_source_identity(post, rdl)
    source_score = g2["source_score"]
    is_anonymous = g2["is_anonymous"]

    # ── GATE 3: Engagement / Propagation ──────────────────────────────────────
    g3 = gate_3_propagation(post, rdl)
    propagation_states  = g3["states"]
    propagation_quality = g3["propagation_quality"]

    # ── GATE 4: Content Classification ────────────────────────────────────────
    g4 = gate_4_content_classification(post, rdl)
    content_state = g4["content_state"]
    # Satire/uncertain: log and discard, do not forward as signal
    if g4["discard_pre_signal"]:
        rdl.classification  = "discard_pre_signal"
        rdl.final_signal    = "no_signal"
        rdl.output_action   = "no_signal"
        rdl.composite_score = 0.0
        return rdl.to_dict()

    # ── GATE 5: Claim Detection ────────────────────────────────────────────────
    g5 = gate_5_claim_detection(post, rdl)
    claim_state = g5["claim_state"]
    claim_scope = g5["claim_scope"]

    # ── GATE 6: Entity Mapping ─────────────────────────────────────────────────
    g6 = gate_6_entity_mapping(post, rdl)
    # Non-actionable entities: log and discard
    if g6["is_non_actionable"]:
        rdl.classification  = "ignore"
        rdl.final_signal    = "no_signal"
        rdl.output_action   = "no_signal"
        rdl.composite_score = 0.0
        return rdl.to_dict()

    # ── GATE 7: Rumor Confirmation ─────────────────────────────────────────────
    g7 = gate_7_confirmation(post, run_ts, rdl)
    confirmation_state = g7["confirmation_state"]

    # ── GATE 8: Sentiment Extraction ──────────────────────────────────────────
    g8 = gate_8_sentiment(post, rdl)
    sentiment_state      = g8["sentiment_state"]
    sentiment_confidence = g8["sentiment_confidence"]

    # ── GATE 9: Novelty / Surprise ─────────────────────────────────────────────
    prior_cluster = post.get("_prior_cluster_tokens", [])
    g9 = gate_9_novelty(post, prior_cluster, rdl)
    novelty_state = g9["novelty_state"]
    post["_novelty_state"] = novelty_state  # pass to gate 15

    # ── GATE 10: Manipulation / Abuse ─────────────────────────────────────────
    g10 = gate_10_manipulation(post, rdl)
    abuse_states = g10["abuse_states"]

    # ── GATE 11: Media / Attachment ────────────────────────────────────────────
    gate_11_media(post, rdl)

    # ── GATE 12: Market Impact Scope ───────────────────────────────────────────
    g12 = gate_12_impact_scope(post, claim_scope, rdl)
    impact_scope = g12["impact_scope"]

    # ── GATE 13: Time Horizon ─────────────────────────────────────────────────
    g13 = gate_13_time_horizon(post, rdl)

    # ── GATE 14: Benchmark-Relative ────────────────────────────────────────────
    g14 = gate_14_benchmark_relative(post, sentiment_state, benchmark, rdl)
    benchmark_state      = g14["benchmark_state"]
    signal_type          = g14["signal_type"]
    interpretation_state = g14["interpretation_state"]

    # ── GATE 15: Crowding / Saturation ────────────────────────────────────────
    gate_15_crowding(post, rdl)

    # ── GATE 16: Contradiction / Ambiguity ────────────────────────────────────
    g16 = gate_16_ambiguity(post, rdl)
    ambiguity_state = g16["ambiguity_state"]

    # ── GATE 17: Timing Controls ──────────────────────────────────────────────
    g17 = gate_17_timing(post, run_ts, rdl)
    if g17["is_expired"]:
        rdl.classification  = "ignore"
        rdl.final_signal    = "no_signal"
        rdl.output_action   = "no_signal"
        rdl.composite_score = 0.0
        return rdl.to_dict()

    # ── GATE 18: Impact Magnitude ─────────────────────────────────────────────
    g18 = gate_18_impact_magnitude(post, benchmark, rdl)
    impact_state      = g18["impact_state"]
    impact_link_state = g18["impact_link_state"]

    # ── GATE 19: Action Classification ────────────────────────────────────────
    g19 = gate_19_action_classification(
        source_score, novelty_state, claim_state, sentiment_state,
        confirmation_state, impact_scope, impact_link_state,
        benchmark_state, signal_type, abuse_states, ambiguity_state, rdl)
    classification = g19["classification"]

    # ── GATE 20: Risk Discounts ────────────────────────────────────────────────
    benchmark_vol            = benchmark.get("vol", "NORMAL")
    new_conflicting_posts    = post.get("new_conflicting_posts_count", 0)
    base_impact_score        = g18["predicted_move_abs"]
    g20 = gate_20_risk_discounts(
        classification, abuse_states, sentiment_confidence,
        is_anonymous, propagation_states, benchmark_vol,
        new_conflicting_posts, base_impact_score, rdl)
    impact_score   = g20["impact_score"]
    classification = g20["classification"]  # may have been overridden to freeze

    # ── GATE 21: Persistence Controls ─────────────────────────────────────────
    gate_21_persistence(post, confirmation_state, rdl)

    # ── GATE 22: Evaluation Loop ───────────────────────────────────────────────
    gate_22_evaluation_loop(post, classification, rdl)

    # ── GATE 23: Output Controls ───────────────────────────────────────────────
    # Overall confidence: sentiment confidence tempered by source credibility
    overall_confidence = (sentiment_confidence * 0.5 +
                          min(source_score / 100.0, 1.0) * 0.5)
    g23 = gate_23_output_controls(classification, overall_confidence,
                                   interpretation_state, rdl)
    output_mode   = g23["output_mode"]
    output_action = g23["output_action"]

    # ── GATE 24: Final Composite Score ─────────────────────────────────────────
    benchmark_risk_state = "drawdown" if benchmark.get("drawdown_active") else "normal"
    g24 = gate_24_composite_score(
        source_score, claim_state, novelty_state, confirmation_state,
        propagation_quality, impact_state, ambiguity_state, abuse_states,
        propagation_states, sentiment_state, benchmark_risk_state,
        impact_link_state, classification, rdl)
    composite_score = g24["composite_score"]
    final_signal    = g24["final_signal"]

    # Populate summary fields on log
    rdl.classification  = classification
    rdl.final_signal    = final_signal
    rdl.composite_score = round(composite_score, 4)
    rdl.output_mode     = output_mode
    rdl.output_action   = output_action
    rdl.impact_score    = round(impact_score, 4)

    return rdl.to_dict()


# ── DATABASE WRITE ─────────────────────────────────────────────────────────────

def write_decision_log(decision: dict) -> None:
    """
    Write a completed RumorDecisionLog to the database.

    Currently writes to system_log via db_helpers.log_event().
    FLAG: A dedicated rumor_decisions table is recommended for regulatory export.
    TODO: tracked in AGENT4_SYSTEM_DESCRIPTION.md section 6.
    """
    summary = (
        f"[social_rumor_agent] post={decision['post_id']} "
        f"source={decision['source_id']} "
        f"classification={decision.get('classification')} "
        f"final_signal={decision.get('final_signal')} "
        f"score={decision.get('composite_score')}"
    )
    _db.log_event(
        agent="social_rumor_agent",
        event_type="rumor_decision",
        message=summary,
        payload=json.dumps(decision),
    )


def write_signal_to_queue(decision: dict) -> None:
    """
    Forward a high-confidence signal to the signal queue for Agent 1 evaluation.

    Only called when output_action is one of:
      positive_signal, negative_signal, benchmark_context_signal,
      idiosyncratic_alpha_signal, transition_to_news_event_logic

    TODO: DATA_DEPENDENCY — full Agent 1 signal queue integration requires
    schema alignment. Currently logs as a suggestion for review.
    Tracked in AGENT4_SYSTEM_DESCRIPTION.md section 7.
    """
    _db.post_suggestion(
        created_by="social_rumor_agent",
        target_files=[],
        description=(
            f"Social rumor signal: {decision.get('classification')} | "
            f"final_signal: {decision.get('final_signal')} | "
            f"composite_score: {decision.get('composite_score')} | "
            f"post: {decision.get('post_id')} source: {decision.get('source_id')}"
        ),
        risk_level="LOW",
        impact="social_signal",
        metadata=json.dumps(decision),
    )


# ── BENCHMARK CONTEXT ──────────────────────────────────────────────────────────

def fetch_benchmark_context() -> dict:
    """
    Retrieve current SPX regime from the database (written by Agent 1 or Agent 2).
    Falls back to a neutral default if unavailable.

    TODO: DATA_DEPENDENCY — direct SPX feed would reduce dependency on
    Agent 1/2 having run recently. Tracked in spec.
    """
    try:
        row = _db.get_latest_benchmark_regime()
        if row:
            return row
    except Exception:
        pass
    # Neutral fallback — no benchmark data
    return {"trend": "NEUTRAL", "vol": "NORMAL", "drawdown_active": False}


# ── DATA SOURCE FETCH (STUBS) ──────────────────────────────────────────────────
# These functions are integration stubs. Each returns a list of raw post dicts.
# Actual API calls are wired in during source integration.
# TODO: DATA_DEPENDENCY — wire live API calls when access is confirmed.

def fetch_stocktwits(tickers: list) -> list:
    """
    Fetch recent posts from StockTwits for the given ticker list.
    Returns list of raw post dicts with normalized fields.
    TODO: DATA_DEPENDENCY — StockTwits API integration.
    """
    log.info("fetch_stocktwits: stub — no live API wired")
    return []


def fetch_reddit(subreddits: list) -> list:
    """
    Fetch recent posts from specified Reddit subreddits.
    Returns list of raw post dicts with normalized fields.
    TODO: DATA_DEPENDENCY — Reddit API integration.
    """
    log.info("fetch_reddit: stub — no live API wired")
    return []


def fetch_yahoo_rss(tickers: list) -> list:
    """
    Fetch recent headlines from Yahoo Finance RSS as volume proxy.
    Returns list of raw post dicts with normalized fields.
    TODO: DATA_DEPENDENCY — RSS parser integration.
    """
    log.info("fetch_yahoo_rss: stub — no live API wired")
    return []


# ── MAIN SCAN ──────────────────────────────────────────────────────────────────

SIGNAL_FORWARD_ACTIONS = {
    "positive_signal",
    "negative_signal",
    "benchmark_context_signal",
    "idiosyncratic_alpha_signal",
    "transition_to_news_event_logic",
}

WATCH_TICKERS = []   # populated from config or db at runtime
WATCH_SUBREDDITS = ["wallstreetbets", "investing", "stocks"]


def run_scan() -> dict:
    """
    Execute one full scan pass:
      1. Fetch posts from all configured sources
      2. Process each post through the 24-gate pipeline
      3. Write every decision log to the database
      4. Forward qualifying signals to the signal queue
      5. Return a run summary
    """
    run_ts    = _now_utc()
    benchmark = fetch_benchmark_context()
    run_cache = []  # token sets for intra-run duplicate detection

    log.info(f"scan start — benchmark: trend={benchmark.get('trend')} "
             f"vol={benchmark.get('vol')} drawdown={benchmark.get('drawdown_active')}")

    # Fetch from all sources
    raw_posts = []
    raw_posts += fetch_stocktwits(WATCH_TICKERS)
    raw_posts += fetch_reddit(WATCH_SUBREDDITS)
    raw_posts += fetch_yahoo_rss(WATCH_TICKERS)

    summary = {
        "run_ts":            run_ts.isoformat(),
        "posts_fetched":     len(raw_posts),
        "posts_processed":   0,
        "halted":            0,
        "signals_forwarded": 0,
        "classifications":   {},
        "final_signals":     {},
    }

    for post in raw_posts:
        decision = process_post(post, run_ts, run_cache, benchmark)
        write_decision_log(decision)

        summary["posts_processed"] += 1

        if decision.get("halted_at"):
            summary["halted"] += 1
        else:
            cls = decision.get("classification", "unknown")
            sig = decision.get("final_signal", "unknown")
            summary["classifications"][cls] = summary["classifications"].get(cls, 0) + 1
            summary["final_signals"][sig]   = summary["final_signals"].get(sig, 0) + 1

            if decision.get("output_action") in SIGNAL_FORWARD_ACTIONS:
                write_signal_to_queue(decision)
                summary["signals_forwarded"] += 1
                log.info(f"signal forwarded — post={decision['post_id']} "
                         f"classification={cls} final_signal={sig} "
                         f"score={decision.get('composite_score')}")

    log.info(f"scan complete — processed={summary['posts_processed']} "
             f"halted={summary['halted']} forwarded={summary['signals_forwarded']}")
    return summary


# ── STATUS ─────────────────────────────────────────────────────────────────────

def show_status() -> None:
    """Print a brief status summary from the last run."""
    try:
        rows = _db.get_recent_events(agent="social_rumor_agent", limit=10)
        if not rows:
            print("No recent runs found.")
            return
        print(f"Last {len(rows)} events for social_rumor_agent:")
        for r in rows:
            print(f"  {r}")
    except Exception as exc:
        print(f"Status unavailable: {exc}")


# ── ENTRYPOINT ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Social & Rumor Flow Agent — processes social posts through a "
                    "24-gate deterministic classification spine."
    )
    parser.add_argument("--mode", choices=["scan", "status"], default="scan",
                        help="scan: run a full scan pass. status: show last run summary.")
    args = parser.parse_args()

    if args.mode == "scan":
        summary = run_scan()
        print(json.dumps(summary, indent=2))
    elif args.mode == "status":
        show_status()


# ── V1 ENTRY POINT ────────────────────────────────────────────────────────────

def run_agent(snapshot: dict) -> dict:
    """
    V1 standard entry point for the Social / Rumor Agent (7.3).

    Accepts a snapshot dict with normalized social data instead of making
    live API calls. The classification spine (process_post) runs on the
    normalized_social dict mapped to the expected post field format.

    snapshot keys:
        normalized_social  (required) — normalized social/rumor fields:
            text               — post text content
            source_id          — source platform identifier
            post_time          — ISO UTC post timestamp
            credibility_score  — source credibility 0-100
            language           — language code (default "en")
            repost_count       — repost/share count
            engagement_count   — total engagements
            bot_probability    — float 0-1
            confirmation_count — independent confirmations
            sentiment_score    — float [-1, 1]
            benchmark_context  — dict with trend/vol/drawdown_active (optional)
        run_id     (required) — unique run identifier
        timestamp  (required) — ISO UTC snapshot timestamp

    Returns:
        classification     — V1 rumor signal category
        overall_confidence — composite confidence [0.0, 1.0]
        confirmation_state — confirmed | provisional | contradictory | unresolved
        decision_log       — full gate trace list
    """
    normalized_social = snapshot.get("normalized_social")
    run_id            = snapshot.get("run_id", "unknown")
    timestamp         = snapshot.get("timestamp")

    if not normalized_social:
        return {
            "halted":             True,
            "halt_reason":        "no_social_input",
            "classification":     None,
            "overall_confidence": 0.0,
            "confirmation_state": "unresolved",
            "decision_log":       [],
        }

    # Resolve run timestamp from snapshot (deterministic — never datetime.now())
    try:
        if timestamp:
            run_ts = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc)
        else:
            run_ts = _now_utc()
    except (ValueError, AttributeError):
        run_ts = _now_utc()

    # Map normalized_social fields to the post dict format expected by process_post
    post = {
        "post_id":           run_id,
        "source_id":         normalized_social.get("source_id", "synthetic"),
        "post_time":         normalized_social.get("post_time", timestamp),
        "text":              normalized_social.get("text", ""),
        "language":          normalized_social.get("language", "en"),
        "character_count":   len(normalized_social.get("text", "")),
        "credibility_score": normalized_social.get("credibility_score", 70),
        "source_score":      normalized_social.get("credibility_score", 70) / 100.0,
        "repost_count":      normalized_social.get("repost_count", 0),
        "engagement_count":  normalized_social.get("engagement_count", 0),
        "bot_probability":   normalized_social.get("bot_probability", 0.0),
        "confirmation_count": normalized_social.get("confirmation_count", 0),
        "sentiment_score":   normalized_social.get("sentiment_score", 0.0),
        **{k: v for k, v in normalized_social.items()},
    }

    benchmark = normalized_social.get("benchmark_context", {})
    run_cache = []

    result = process_post(post, run_ts, run_cache, benchmark)

    # Map internal classification values to V1 output states
    internal_cls = result.get("classification", "ignore")
    _cls_map = {
        "long_bias":                   "bullish_rumor_signal",
        "short_bias":                  "bearish_rumor_signal",
        "relative_alpha":              "relative_alpha_signal",
        "relative_alpha_signal":       "relative_alpha_signal",
        "manipulation_watch":          "manipulation_watch",
        "benchmark_regime":            "benchmark_regime_signal",
        "benchmark_regime_signal":     "benchmark_regime_signal",
        "upgraded_to_confirmed_event": "upgraded_to_confirmed_event",
        "discard_pre_signal":          "ignore",
        "no_signal":                   "ignore",
        "ignore":                      "ignore",
    }
    v1_cls = _cls_map.get(internal_cls, "ignore")

    _conf_map = {
        "confirmed":           "confirmed",
        "provisional":         "provisional",
        "contradictory":       "contradictory",
        "unresolved":          "unresolved",
        "expired_unconfirmed": "unresolved",
    }
    raw_conf = result.get("confirmation_state", "unresolved")

    return {
        "classification":     v1_cls,
        "overall_confidence": float(result.get("composite_score", 0.0)),
        "confirmation_state": _conf_map.get(raw_conf, "unresolved"),
        "decision_log":       result.get("decision_log", []),
    }


if __name__ == "__main__":
    main()
