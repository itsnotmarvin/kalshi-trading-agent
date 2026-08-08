"""
Read-only Early Marks probe.

This script fetches Kalshi markets, builds a first-pass repricing shortlist,
and asks Claude to turn the shortlist into thesis-style research notes.

It does not place, cancel, or approve orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import re

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from anthropic import Anthropic

from adapters.kalshi_adapter import KalshiAdapter
from config.settings import settings
from core.market_pricing import TAKER_FEE_RATE
from core.soccer_path import cross_section_report

RISE_FLOOR_DOLLARS = 0.09
SOCCER_BRACKET_PATH = Path(__file__).resolve().parents[2] / "data" / "wc2026_bracket.json"

EARLY_MARKS_DECISIONS = (
    "Catalyst setup",
    "Watch only",
    "Market knows something",
    "Too directionless",
    "Pre-open watch",
)
EARLY_MARKS_SIDES = ("YES", "NO", "Watch")
EARLY_MARKS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "decision": {"type": "string", "enum": list(EARLY_MARKS_DECISIONS)},
                    "side": {"type": "string", "enum": list(EARLY_MARKS_SIDES)},
                    "scenario_weights": {
                        "type": "object",
                        "properties": {
                            "bull": {"type": "number"},
                            "base": {"type": "number"},
                            "bear": {"type": "number"},
                        },
                        "required": ["bull", "base", "bear"],
                        "additionalProperties": False,
                    },
                    "scenario_weight_basis": {"type": "string"},
                    "expected_future_price_cents": {"type": ["number", "null"]},
                    "entry_limit_cents": {"type": ["number", "null"]},
                    "next_catalyst": {"type": "string"},
                    "catalyst_date_estimate": {"type": ["string", "null"]},
                    "fair_probability_range": {
                        "type": "object",
                        "properties": {
                            "low_pct": {"type": "number"},
                            "high_pct": {"type": "number"},
                            "basis": {"type": "string"},
                        },
                        "required": ["low_pct", "high_pct", "basis"],
                        "additionalProperties": False,
                    },
                    "why_i_believe_this": {"type": "string"},
                    "market_knows_check": {"type": "string"},
                    "no_direction_check": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "url_or_reference": {"type": "string"},
                                "published_date": {"type": "string"},
                                "why_it_matters": {"type": "string"},
                            },
                            "required": ["label", "url_or_reference", "published_date", "why_it_matters"],
                            "additionalProperties": False,
                        },
                    },
                    "exit_monitor": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "trigger": {"type": "string"},
                                "action": {"type": "string"},
                            },
                            "required": ["trigger", "action"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "ticker",
                    "decision",
                    "side",
                    "scenario_weights",
                    "scenario_weight_basis",
                    "expected_future_price_cents",
                    "entry_limit_cents",
                    "next_catalyst",
                    "catalyst_date_estimate",
                    "fair_probability_range",
                    "why_i_believe_this",
                    "market_knows_check",
                    "no_direction_check",
                    "sources",
                    "exit_monitor",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


PROBE_OUTPUT = Path("data/early_marks_probe.json")
STATUS_OUTPUT = Path("data/early_marks_status.json")
STATUS_STAGES = ("connect", "health", "pull_markets", "category_model", "rank", "enrich", "render")
MARKET_STATUSES_TO_TRY = (None, "open", "unopened", "active")
# Live tournament series fetched directly when a matching category scan runs;
# cursor-ordered bulk pagination cannot be trusted to reach a specific series.
LIVE_SERIES_BY_CATEGORY = {
    "soccer": ("KXMENWORLDCUP",),
    "sports": ("KXMENWORLDCUP",),
}
ACTIVE_MARKET_STATUSES = {"open", "active"}
PREOPEN_MARKET_STATUSES = {"initialized", "unopened", "preopen", "pending", "not_started", "unstarted"}
TERMINAL_MARKET_STATUSES = {"finalized", "settled", "closed", "expired", "inactive", "canceled", "cancelled", "resolved"}
MARKET_STATUS_ALIASES = {
    "all": set(),
    "open": ACTIVE_MARKET_STATUSES,
    "active": ACTIVE_MARKET_STATUSES,
    "preopen": PREOPEN_MARKET_STATUSES,
    "unopened": PREOPEN_MARKET_STATUSES,
}
FUTURE_CATALYST_TERMS = (
    "winner",
    "win ",
    "champion",
    "nominee",
    "nomination",
    "election",
    "control",
    "playoff",
    "final",
    "record",
    "rate",
    "cut",
    "hike",
    "decision",
    "approved",
    "launch",
    "named",
    "award",
    "season",
    "world cup",
)
HIGH_ATTENTION_CATEGORIES = (
    "sports",
    "politics",
    "economics",
    "financials",
    "crypto",
    "entertainment",
)
MIN_EARLY_RUNWAY_DAYS = 30
DEFAULT_MIN_PLACEMENT_DOLLARS = 20.0
DEFAULT_MAX_PLACEMENT_DOLLARS = 100.0
UUIDISH_CHARS = set("0123456789abcdefABCDEF-")


def write_stage_status(run_id: str, stage: str, done_through: bool = False) -> None:
    """Best-effort status emission for the Early Marks page loader."""
    try:
        done_index = STATUS_STAGES.index(stage) + (1 if done_through else 0)
        payload = {
            "run_id": run_id,
            "stage": stage,
            "stages_done": list(STATUS_STAGES[:done_index]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        STATUS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temp_path = STATUS_OUTPUT.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, STATUS_OUTPUT)
    except Exception:
        pass

DEFAULT_RANK_WEIGHTS = {
    "catalyst": 0.18,
    "runway": 0.14,
    "attention": 0.12,
    "liquidity": 0.10,
    "source": 0.09,
    "spread": 0.08,
    "anchor": 0.07,
    "momentum": 0.05,
    "domain": 0.10,
    "flow": 0.04,
    "roi_raw": 0.08,
    "directionless": 0.24,
}

ENTITY_RESEARCH_LENSES: dict[str, dict[str, Any]] = {
    "politics_election": {
        "name": "Political candidate / party-path lens",
        "what_to_check": [
            "What office, party role, public platform, or coalition gives this person a path to the market outcome?",
            "What is coming next: reelection, filing, debate, endorsement, fundraising, polling, ballot access, legal deadline, party meeting, or public campaign travel?",
            "What past moves matter: prior election margins, statewide wins, shortlist history, legislative wins, fundraising network, scandals, backlash, or coalition strength?",
            "What would make the party want this person later, and what would make the party move away from them?",
            "Is the price move reacting to public facts, or does volume/spread/momentum suggest the market may know something before the public story is clear?",
        ],
        "bull_examples": [
            "Strong polling, fundraising, endorsements, debate performance, ballot access, or a visible governing/campaign win.",
            "A party lane opening where this person fits the moment better than the current favorites.",
        ],
        "bear_examples": [
            "Party lane blocked by a stronger candidate, ideological backlash, scandal, weak operation, no clear catalyst, or obvious biography already priced.",
        ],
    },
    "sports_soccer_worldcup": {
        "name": "Team path / tournament catalyst lens",
        "what_to_check": [
            "Qualification status, draw/bracket path, group difficulty, seeding, travel, and fixture timing.",
            "Roster quality, injury risk, manager stability, chemistry, recent form, and player availability.",
            "Whether future public events can make the path easier or harder before final resolution.",
        ],
        "bull_examples": ["Favorable draw, key player return, easier path, strong qualifying form, or improved seeding."],
        "bear_examples": ["Hard bracket path, injuries, weak form, poor chemistry, or no clean exit liquidity."],
    },
    "sports_general": {
        "name": "Sports path / roster catalyst lens",
        "what_to_check": [
            "Schedule, standings, bracket, roster, injury, trades, coaching, matchup path, and attention timing.",
            "Whether a known future sports event can concentrate attention before resolution.",
        ],
        "bull_examples": ["Improving seeding, easier schedule, roster upgrade, injury return, or playoff-path clarity."],
        "bear_examples": ["Thin liquidity, no upcoming games that matter, injuries, matchup problems, or attention already priced."],
    },
    "entertainment_awards": {
        "name": "Attention / nomination catalyst lens",
        "what_to_check": [
            "Nomination calendar, shortlist timing, critics lists, voting windows, reviews, box office/streaming visibility, and campaign push.",
            "Whether the person/title has a real public path or is only a name with no current campaign.",
        ],
        "bull_examples": ["Shortlist inclusion, strong reviews, awards-campaign momentum, or sudden media attention."],
        "bear_examples": ["No current campaign path, low liquidity, stale rumor, or market based on one weak leak."],
    },
    "macro_financial": {
        "name": "Scheduled release / policy catalyst lens",
        "what_to_check": [
            "Known dates: data releases, rate decisions, earnings, regulatory deadlines, court decisions, or product launches.",
            "Whether the market has a measurable public input or is just a vague longshot.",
        ],
        "bull_examples": ["A scheduled public release can change expectations before settlement."],
        "bear_examples": ["No date-specific catalyst, stale narrative, thin liquidity, or impossible path hidden behind high ROI."],
    },
    "general": {
        "name": "General entity / future-fact lens",
        "what_to_check": [
            "What public fact could make this placement less theoretical?",
            "What has this person, team, company, or topic done before that makes the move plausible?",
            "What future action, announcement, deadline, or attention wave could move resale value?",
            "What obvious reason makes the placement look bad even if the price math looks good?",
        ],
        "bull_examples": ["Clear future catalyst, rising attention, public proof point, and enough liquidity to exit."],
        "bear_examples": ["No concrete path, meme attention only, stale rumor, or market already pricing the story."],
    },
}

PUBLIC_FIGURE_PROFILES: dict[str, dict[str, Any]] = {
    "politics_election": ENTITY_RESEARCH_LENSES["politics_election"],
    "entertainment_awards": ENTITY_RESEARCH_LENSES["entertainment_awards"],
    "general": ENTITY_RESEARCH_LENSES["general"],
}

MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "sports_soccer_worldcup": {
        "name": "Sports / Soccer / World Cup",
        "path": ["sports", "soccer", "world cup"],
        "min_runway_days": 5,
        "next_catalyst_label": "Draw/bracket path, qualification, seeding, roster, or injury update",
        "why_catalyst_matters": "World Cup futures can reprice when the path to later rounds becomes clearer before the tournament is decided.",
        "terms": ("world cup", "fifa", "draw", "bracket", "group", "seed", "seeding", "qualify", "qualification", "roster", "squad", "injury", "lineup"),
        "domain_signals": {
            "draw_bracket_path": ("draw", "bracket", "group", "path"),
            "qualification_seeding": ("qualify", "qualification", "seed", "seeding"),
            "roster_injury": ("roster", "squad", "injury", "lineup"),
            "team_quality_context": ("team", "winner", "champion", "final"),
            "attention_timing": ("world cup", "fifa", "season"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "domain": 0.16, "runway": 0.12, "source": 0.08, "roi_raw": 0.09},
        "scenario": {"domain_bull": 0.12, "domain_bear": 0.07, "catalyst": 0.18, "runway": 0.08},
        "price": {"domain_bull": 0.030, "domain_base": 0.006, "domain_bear": 0.025, "catalyst": 0.040, "runway": 0.020},
    },
    "sports_soccer": {
        "name": "Sports / Soccer",
        "path": ["sports", "soccer"],
        "next_catalyst_label": "Fixture draw, qualification, roster, injury, or form update",
        "why_catalyst_matters": "Soccer futures can reprice as schedule path, player availability, and team form become easier to judge.",
        "terms": ("soccer", "football", "fifa", "uefa", "mls", "premier league", "champions league", "draw", "fixture", "qualify", "injury", "roster"),
        "domain_signals": {
            "fixture_path": ("draw", "fixture", "schedule", "path"),
            "qualification_seeding": ("qualify", "qualification", "seed", "table"),
            "roster_injury": ("roster", "squad", "injury", "lineup"),
            "team_quality_context": ("team", "winner", "champion", "final"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "domain": 0.14, "runway": 0.12, "roi_raw": 0.09},
        "scenario": {"domain_bull": 0.10, "domain_bear": 0.07, "catalyst": 0.17, "runway": 0.08},
        "price": {"domain_bull": 0.025, "domain_base": 0.005, "domain_bear": 0.023, "catalyst": 0.037, "runway": 0.020},
    },
    "sports_basketball": {
        "name": "Sports / Basketball",
        "path": ["sports", "basketball"],
        "next_catalyst_label": "Schedule, seeding, injury, trade, roster, or playoff-path update",
        "why_catalyst_matters": "Basketball futures can reprice when rotations, injuries, trades, seeding, or matchup path change the resale story.",
        "terms": ("basketball", "nba", "wnba", "ncaa", "march madness", "playoff", "seed", "seeding", "injury", "roster", "trade", "draft"),
        "domain_signals": {
            "seeding_path": ("seed", "seeding", "playoff", "bracket", "matchup"),
            "roster_injury": ("injury", "roster", "lineup", "rotation", "trade"),
            "player_quality": ("player", "mvp", "points", "assists", "rebounds"),
            "team_quality_context": ("team", "winner", "champion", "final"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "domain": 0.15, "momentum": 0.07, "roi_raw": 0.09},
        "scenario": {"domain_bull": 0.11, "domain_bear": 0.08, "catalyst": 0.16, "runway": 0.07},
        "price": {"domain_bull": 0.026, "domain_base": 0.005, "domain_bear": 0.026, "catalyst": 0.035, "runway": 0.018},
    },
    "sports_general": {
        "name": "Sports / General",
        "path": ["sports"],
        "next_catalyst_label": "Schedule, bracket, roster, injury, or standings update",
        "why_catalyst_matters": "Sports futures need a concrete path-change event before early attention becomes useful.",
        "terms": ("sports", "playoff", "final", "champion", "winner", "season", "injury", "roster", "schedule", "bracket"),
        "domain_signals": {
            "path_structure": ("schedule", "bracket", "playoff", "final"),
            "roster_injury": ("injury", "roster", "lineup"),
            "team_quality_context": ("team", "winner", "champion", "season"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "domain": 0.12, "roi_raw": 0.08},
        "scenario": {"domain_bull": 0.09, "domain_bear": 0.08, "catalyst": 0.16, "runway": 0.08},
        "price": {"domain_bull": 0.022, "domain_base": 0.004, "domain_bear": 0.024, "catalyst": 0.034, "runway": 0.018},
    },
    "politics_election": {
        "name": "Politics / Elections",
        "path": ["politics", "elections"],
        "next_catalyst_label": "Polling, filing, ballot-access, debate, endorsement, or fundraising update",
        "why_catalyst_matters": "Election futures reprice when candidate viability becomes less theoretical and public signals narrow the field.",
        "terms": ("election", "president", "senate", "house", "governor", "nominee", "nomination", "primary", "debate", "poll", "endorsement", "fundraising", "ballot"),
        "domain_signals": {
            "candidate_viability": ("nominee", "nomination", "primary", "candidate", "president"),
            "public_signal_path": ("poll", "debate", "endorsement", "fundraising"),
            "legal_ballot_access": ("ballot", "filing", "eligible", "court"),
            "field_narrowing": ("winner", "election", "control", "party"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "domain": 0.15, "source": 0.11, "directionless": 0.28, "roi_raw": 0.07},
        "scenario": {"domain_bull": 0.10, "domain_bear": 0.10, "catalyst": 0.15, "runway": 0.10},
        "price": {"domain_bull": 0.020, "domain_base": 0.004, "domain_bear": 0.030, "catalyst": 0.030, "runway": 0.024},
    },
    "macro_financial": {
        "name": "Macro / Financial",
        "path": ["macro", "financial"],
        "next_catalyst_label": "Scheduled data release, rate decision, earnings, ETF, or regulatory update",
        "why_catalyst_matters": "Macro and financial contracts need date-specific public releases because surprises get priced quickly.",
        "terms": ("rate", "fed", "cpi", "inflation", "jobs", "earnings", "bitcoin", "crypto", "etf", "approved", "launch", "decision"),
        "domain_signals": {
            "scheduled_release": ("rate", "fed", "cpi", "inflation", "jobs", "earnings"),
            "regulatory_path": ("approved", "approval", "decision", "launch", "etf"),
            "market_attention": ("bitcoin", "crypto", "stock", "financial"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "source": 0.13, "spread": 0.10, "domain": 0.12, "directionless": 0.30, "roi_raw": 0.06},
        "scenario": {"domain_bull": 0.08, "domain_bear": 0.11, "catalyst": 0.14, "runway": 0.05},
        "price": {"domain_bull": 0.018, "domain_base": 0.003, "domain_bear": 0.032, "catalyst": 0.026, "runway": 0.012},
    },
    "entertainment_awards": {
        "name": "Entertainment / Awards",
        "path": ["entertainment", "awards"],
        "next_catalyst_label": "Nomination shortlist, voting window, award ceremony, or public ranking update",
        "why_catalyst_matters": "Awards markets can reprice as nominations and consensus lists make attention less scattered.",
        "terms": ("award", "oscar", "grammy", "emmy", "nominee", "nomination", "winner", "named"),
        "domain_signals": {
            "nomination_path": ("nominee", "nomination", "shortlist", "named"),
            "ceremony_timing": ("award", "oscar", "grammy", "emmy"),
            "attention_concentration": ("winner", "favorite", "final"),
        },
        "rank_weights": {**DEFAULT_RANK_WEIGHTS, "attention": 0.16, "domain": 0.12, "liquidity": 0.08, "roi_raw": 0.08},
        "scenario": {"domain_bull": 0.09, "domain_bear": 0.08, "catalyst": 0.16, "runway": 0.07},
        "price": {"domain_bull": 0.022, "domain_base": 0.004, "domain_bear": 0.024, "catalyst": 0.034, "runway": 0.017},
    },
    "general_event": {
        "name": "General Event",
        "path": ["general"],
        "next_catalyst_label": "Next public fact, market open, or settlement-source update",
        "why_catalyst_matters": "Generic markets need a source-backed event before an early setup becomes actionable.",
        "terms": FUTURE_CATALYST_TERMS,
        "domain_signals": {
            "future_fact_path": FUTURE_CATALYST_TERMS,
        },
        "rank_weights": DEFAULT_RANK_WEIGHTS,
        "scenario": {"domain_bull": 0.07, "domain_bear": 0.08, "catalyst": 0.16, "runway": 0.10},
        "price": {"domain_bull": 0.018, "domain_base": 0.003, "domain_bear": 0.024, "catalyst": 0.035, "runway": 0.025},
    },
}


@dataclass
class Candidate:
    ticker: str
    title: str
    display_name: str
    display_title: str
    category: str
    category_path: list[str]
    model_key: str
    model_name: str
    status: str
    yes_ask: float | None
    no_ask: float | None
    yes_bid: float | None
    no_bid: float | None
    last_price: float | None
    previous_price: float | None
    volume: float
    volume_24h: float
    liquidity: float
    spread: float | None
    crossed_book: bool
    end_date: str | None
    days_to_resolution: float | None
    settlement_source: str
    catalyst_score: float
    liquidity_score: float
    exitability_score: float
    runway_score: float
    attention_score: float
    source_score: float
    spread_score: float
    momentum_score: float
    price_anchor_score: float
    domain_signal_score: float
    flow_signal_score: float
    directionless_penalty: float
    rank_score: float
    bull_probability: float
    base_probability: float
    bear_probability: float
    bull_price: float | None
    base_price: float | None
    bear_price: float | None
    expected_future_price: float | None
    expected_repricing_roi: float | None
    entry_limit: float | None
    recommended_placement_dollars: float | None
    estimated_contracts: int | None
    estimated_position_cost: float | None
    estimated_target_value: float | None
    estimated_target_profit: float | None
    placement_sizing_basis: dict[str, Any]
    next_catalyst: dict[str, Any]
    domain_signals: dict[str, Any]
    flow_signal: dict[str, Any]
    public_figure_profile: dict[str, Any] | None
    scenario_weight_basis: dict[str, Any]
    price_scenario_basis: dict[str, Any]
    probe_status: str
    soccer_path: dict[str, Any] | None = None


def as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_public_figure_profile(display_name: str, title_l: str, model_key: str) -> dict[str, Any] | None:
    personish_market = any(term in title_l for term in ("election", "president", "nominee", "nomination", "award", "named"))
    if not personish_market:
        return None
    profile = PUBLIC_FIGURE_PROFILES.get(model_key) or PUBLIC_FIGURE_PROFILES["general"]
    if not profile:
        return None
    return {
        **profile,
        "subject": display_name,
        "research_policy": (
            "Use this as a checklist for person/entity context. Do not treat it as proof. "
            "If a fact is not in sources or candidate data, describe it as a question to verify, not as known."
        ),
    }


def parse_date(raw: dict[str, Any]) -> datetime | None:
    dates: list[datetime] = []
    for key in ("expected_expiration_time", "close_time", "open_time"):
        value = raw.get(key)
        if not value:
            continue
        try:
            dates.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            continue
    future_dates = [d for d in dates if d > datetime.now(timezone.utc)]
    return min(future_dates) if future_dates else (min(dates) if dates else None)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clean_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.removeprefix("::").strip()
    if not text:
        return ""
    if len(text) >= 24 and all(char in UUIDISH_CHARS for char in text):
        return ""
    return text


def market_display_name(raw: dict[str, Any], ticker: str) -> str:
    """Return the human contract label Kalshi provides instead of ticker codes."""
    for key in ("yes_sub_title", "no_sub_title"):
        label = _clean_label(raw.get(key))
        if label:
            return label

    custom_strike = raw.get("custom_strike")
    if isinstance(custom_strike, dict):
        for value in custom_strike.values():
            label = _clean_label(value)
            if label:
                return label

    label = _clean_label(raw.get("subtitle"))
    if label:
        return label

    # Last resort: use the ticker suffix, but mark it as a fallback by keeping
    # the full code visible in contract metadata rather than pretending it is a name.
    return ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker


def market_display_title(title: str, display_name: str, event_title: str = "") -> str:
    base_title = event_title or title
    if not display_name:
        return base_title
    if re.search(rf"\b{re.escape(display_name.lower())}\b", base_title.lower()):
        return base_title
    return f"{base_title} - {display_name}"


def candidate_market_group_title(candidate: "Candidate") -> str:
    suffix = f" - {candidate.display_name}"
    if candidate.display_title.endswith(suffix):
        return candidate.display_title[: -len(suffix)]
    return candidate.title


def candidate_category_label(candidate: "Candidate") -> str:
    return " / ".join(str(part) for part in candidate.category_path if part) or candidate.category


def candidate_status_matches(candidate: "Candidate", status_filter: str) -> bool:
    normalized = (status_filter or "all").strip().lower()
    if normalized in {"", "all"}:
        return True
    status_set = MARKET_STATUS_ALIASES.get(normalized)
    if status_set is not None:
        return not status_set or candidate.status in status_set
    return candidate.status == normalized


def candidate_horizon_matches(candidate: "Candidate", horizon_filter: str) -> bool:
    normalized = (horizon_filter or "all").strip().lower()
    if normalized in {"", "all"}:
        return True
    days = candidate.days_to_resolution
    if days is None:
        return normalized == "long"
    if normalized == "short":
        return 0 <= days <= 30
    if normalized == "medium":
        return 30 < days <= 180
    if normalized == "long":
        return days > 180
    return True


def candidate_scan_matches(
    candidate: "Candidate",
    category_filter: str = "all",
    status_filter: str = "all",
    horizon_filter: str = "all",
) -> bool:
    category = (category_filter or "all").strip().lower()
    category_label = candidate_category_label(candidate).lower()
    raw_category = candidate.category.lower()
    category_parts = {str(part).lower() for part in candidate.category_path if part}
    category_ok = (
        category in {"", "all"}
        or category == category_label
        or category == raw_category
        or category in category_parts
    )
    return (
        category_ok
        and candidate_status_matches(candidate, status_filter)
        and candidate_horizon_matches(candidate, horizon_filter)
    )


def select_shortlist(
    candidates: list["Candidate"],
    shortlist_size: int,
    max_per_market: int = 3,
    unreleased_slots: int = 3,
) -> list["Candidate"]:
    """Diversify by parent market so one cheap multi-placement market cannot dominate."""
    if shortlist_size <= 0:
        return []

    selected: list[Candidate] = []
    selected_tickers: set[str] = set()
    group_counts: Counter[str] = Counter()

    def add(candidate: Candidate, cap: int) -> bool:
        if candidate.ticker in selected_tickers:
            return False
        market_group = candidate_market_group_title(candidate)
        if group_counts[market_group] >= cap:
            return False
        selected.append(candidate)
        selected_tickers.add(candidate.ticker)
        group_counts[market_group] += 1
        return True

    pre_open = [
        c for c in candidates
        if c.probe_status == "Pre-open watch" or c.status in PREOPEN_MARKET_STATUSES
    ]
    reserved_preopen = min(unreleased_slots, len(pre_open), max(1, shortlist_size // 4))
    main_slots = max(0, shortlist_size - reserved_preopen)

    for candidate in candidates:
        if len(selected) >= main_slots:
            break
        add(candidate, max_per_market)

    for candidate in pre_open:
        if len(selected) >= shortlist_size:
            break
        add(candidate, max_per_market)

    cap = max_per_market
    while len(selected) < shortlist_size and cap < shortlist_size:
        cap += 1
        added_this_round = False
        for candidate in candidates:
            if len(selected) >= shortlist_size:
                break
            added_this_round = add(candidate, cap) or added_this_round
        if not added_this_round:
            break

    return selected


def scan_filters_are_restricted(
    category_filter: str = "all",
    status_filter: str = "all",
    horizon_filter: str = "all",
) -> bool:
    return any(
        str(value or "all").strip().lower() not in {"", "all"}
        for value in (category_filter, status_filter, horizon_filter)
    )


def select_scan_shortlist(
    candidates: list["Candidate"],
    *,
    shortlist_size: int,
    max_per_market: int = 3,
    unreleased_slots: int = 3,
    category_filter: str = "all",
    status_filter: str = "all",
    horizon_filter: str = "all",
) -> tuple[list["Candidate"], list["Candidate"]]:
    eligible_candidates = [
        c for c in candidates
        if c.probe_status != "Too directionless"
        and c.rank_score > 0
        and candidate_scan_matches(
            c,
            category_filter=category_filter,
            status_filter=status_filter,
            horizon_filter=horizon_filter,
        )
    ]
    if eligible_candidates:
        pool = eligible_candidates
    elif scan_filters_are_restricted(category_filter, status_filter, horizon_filter):
        pool = []
    else:
        pool = candidates
    return (
        select_shortlist(
            pool,
            shortlist_size=shortlist_size,
            max_per_market=max_per_market,
            unreleased_slots=unreleased_slots,
        ),
        eligible_candidates,
    )


def prioritize_soccer_path_candidates(candidates: list["Candidate"]) -> list["Candidate"]:
    return sorted(candidates, key=lambda c: (c.soccer_path is None, -c.rank_score))


def ranking_roi_signal(expected_repricing_roi: float | None) -> float:
    """Cap ROI influence on ranking while keeping raw ROI visible for learning."""
    if expected_repricing_roi is None:
        return 0.0
    return clamp(expected_repricing_roi / 0.50, -0.50, 1.0)


def build_placement_sizing(
    *,
    open_price: float | None,
    target_price: float | None,
    rank_score: float,
    expected_repricing_roi: float | None,
    liquidity_score: float,
    spread_score: float,
    directionless_penalty: float,
    min_dollars: float = DEFAULT_MIN_PLACEMENT_DOLLARS,
    max_dollars: float = DEFAULT_MAX_PLACEMENT_DOLLARS,
) -> dict[str, Any]:
    """Build a research-only stake size between the user's chosen dollar bounds."""
    min_stake = max(0.0, float(min_dollars))
    max_stake = max(min_stake, float(max_dollars))
    roi_component = max(0.0, ranking_roi_signal(expected_repricing_roi))
    sizing_score = clamp(
        0.42 * clamp(rank_score / 100, 0.0, 1.0)
        + 0.18 * roi_component
        + 0.16 * liquidity_score
        + 0.12 * spread_score
        + 0.12 * (1 - directionless_penalty),
        0.0,
        1.0,
    )
    recommended = round(min_stake + (max_stake - min_stake) * sizing_score, 2)

    if open_price is None or open_price <= 0:
        return {
            "recommended_placement_dollars": recommended,
            "estimated_contracts": None,
            "estimated_position_cost": None,
            "estimated_target_value": None,
            "estimated_target_profit": None,
            "basis": {
                "method": "Research-only watch budget because this placement has no executable ask yet.",
                "min_placement_dollars": min_stake,
                "max_placement_dollars": max_stake,
                "sizing_score": round(sizing_score * 100, 1),
                "roi_influence": round(roi_component * 100, 1),
            },
        }

    contracts = max(1, int(recommended // open_price))
    position_cost = round(contracts * open_price, 2)
    resale_value = round(contracts * target_price, 2) if target_price is not None else None
    resale_profit = round(resale_value - position_cost, 2) if resale_value is not None else None
    return {
        "recommended_placement_dollars": recommended,
        "estimated_contracts": contracts,
        "estimated_position_cost": position_cost,
        "estimated_target_value": resale_value,
        "estimated_target_profit": resale_profit,
        "basis": {
            "method": "Research-only dollar placement size, not an order. Size is clamped to the user's placement range.",
            "formula": "min_dollars + (max_dollars - min_dollars) * sizing_score",
            "sizing_score_formula": "42% rank + 18% capped ROI + 16% liquidity + 12% spread + 12% low-directionless-risk",
            "min_placement_dollars": min_stake,
            "max_placement_dollars": max_stake,
            "sizing_score": round(sizing_score * 100, 1),
            "raw_roi_still_reported_uncapped": True,
            "roi_influence": round(roi_component * 100, 1),
            "contract_math": "estimated_contracts = floor(recommended_placement_dollars / yes_ask)",
        },
    }


def price_anchor_score(price: float | None) -> float:
    """Favor tradable asymmetric contracts without blindly rewarding dust prices."""
    if price is None:
        return 0.45
    if price < 0.03:
        return 0.15
    if price <= 0.35:
        return 1.0
    if price <= 0.65:
        return 0.75
    if price <= 0.85:
        return 0.35
    return 0.15


def _has_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    return any(term in text for term in terms)


def classify_market(category: str, title_l: str) -> tuple[str, dict[str, Any]]:
    text = f"{category} {title_l}".lower()
    if _has_any(text, ("world cup", "fifa world cup")):
        key = "sports_soccer_worldcup"
    elif _has_any(text, ("basketball", "nba", "wnba", "march madness")):
        key = "sports_basketball"
    elif _has_any(text, ("soccer", "fifa", "uefa", "mls", "premier league", "champions league")):
        key = "sports_soccer"
    elif _has_any(text, ("sports", "playoff", "champion", "final", "season")):
        key = "sports_general"
    elif _has_any(text, ("politic", "election", "president", "senate", "house", "governor", "nominee", "primary")):
        key = "politics_election"
    elif _has_any(text, ("economics", "financial", "crypto", "bitcoin", "fed", "rate", "cpi", "inflation", "earnings", "etf")):
        key = "macro_financial"
    elif _has_any(text, ("entertainment", "award", "oscar", "grammy", "emmy")):
        key = "entertainment_awards"
    else:
        key = "general_event"
    return key, MODEL_PROFILES[key]


def _score_term_group(title_l: str, terms: tuple[str, ...] | list[str], baseline: float = 0.20) -> float:
    hits = sum(1 for term in terms if term in title_l)
    return clamp(baseline + hits * 0.18, 0.0, 1.0)


def build_domain_signals(profile: dict[str, Any], title_l: str) -> tuple[float, dict[str, Any]]:
    signal_rows: list[dict[str, Any]] = []
    total = 0.0
    groups = profile.get("domain_signals") or {}
    if not isinstance(groups, dict) or not groups:
        return 0.25, {"score": 25.0, "signals": [], "note": "No domain model available yet."}

    for name, terms in groups.items():
        term_tuple = tuple(str(term) for term in terms)
        matched = [term for term in term_tuple if term in title_l]
        score = _score_term_group(title_l, term_tuple)
        total += score
        signal_rows.append({
            "name": str(name).replace("_", " "),
            "score": round(score * 100, 1),
            "matched_terms": matched,
            "note": "Matched in market metadata." if matched else "Needs external source enrichment; using a low baseline.",
        })

    domain_score = total / max(len(signal_rows), 1)
    return domain_score, {
        "score": round(domain_score * 100, 1),
        "signals": signal_rows,
        "note": "Domain score is model-specific; the same surface setup can mean different resale math in different categories.",
    }


def build_next_catalyst(profile: dict[str, Any], title_l: str, status: str, days_to_resolution: float | None) -> dict[str, Any]:
    terms = tuple(str(term) for term in profile.get("terms", ()))
    matched = [term for term in terms if term in title_l][:8]
    label = str(profile.get("next_catalyst_label") or "Next public pricing event")
    if status not in {"open", "active"}:
        label = "Market open / first live order book"
    return {
        "label": label,
        "date_status": "needs source enrichment",
        "days_proxy": days_to_resolution,
        "matched_terms": matched,
        "why_it_matters": profile.get("why_catalyst_matters") or "This is the next type of public fact that could change resale demand.",
    }


def build_flow_signal(
    volume: float,
    volume_24h: float,
    liquidity: float,
    liquidity_score: float,
    spread_score: float,
    momentum_score: float,
    last_price: float | None,
    previous_price: float | None,
) -> tuple[float, float, dict[str, Any]]:
    recent_move = 0.0
    if last_price is not None and previous_price and previous_price > 0:
        recent_move = (last_price - previous_price) / previous_price

    volume_velocity = clamp(math.log10(max(volume_24h, 1.0)) / 5.0, 0.0, 1.0)
    lifetime_depth = clamp(math.log10(max(volume + liquidity, 1.0)) / 6.0, 0.0, 1.0)
    unexplained_move = clamp(abs(recent_move) / 0.25, 0.0, 1.0)
    flow_score = clamp(
        0.28 * lifetime_depth
        + 0.24 * volume_velocity
        + 0.18 * spread_score
        + 0.18 * unexplained_move
        + 0.12 * liquidity_score,
        0.0,
        1.0,
    )
    warning_score = clamp(
        0.55 * unexplained_move
        + 0.30 * (1 - spread_score)
        + 0.15 * volume_velocity,
        0.0,
        1.0,
    )
    return flow_score, warning_score, {
        "score": round(flow_score * 100, 1),
        "warning_score": round(warning_score * 100, 1),
        "public_data_level": "quote, spread, volume, and liquidity only",
        "holder_identity_available": False,
        "holding_duration_available": False,
        "what_it_can_tell_us": "It can flag unusual price/volume behavior that deserves a market-knows check.",
        "what_it_cannot_tell_us": "It cannot prove who bought or sold, whether they were insiders, or how long a public trader held.",
        "inputs": {
            "volume": round(volume, 2),
            "volume_24h": round(volume_24h, 2),
            "liquidity": round(liquidity, 2),
            "recent_move_pct": round(recent_move * 100, 2),
        },
    }


def _profile_number(profile: dict[str, Any], group: str, key: str, default: float) -> float:
    values = profile.get(group)
    if isinstance(values, dict) and key in values:
        try:
            return float(values[key])
        except (TypeError, ValueError):
            return default
    return default


def score_raw_market(
    raw: dict[str, Any],
    min_placement_dollars: float = DEFAULT_MIN_PLACEMENT_DOLLARS,
    max_placement_dollars: float = DEFAULT_MAX_PLACEMENT_DOLLARS,
) -> Candidate | None:
    ticker = str(raw.get("ticker") or "")
    title = str(raw.get("title") or raw.get("question") or "").strip()
    if not ticker or not title:
        return None

    status = str(raw.get("status") or "unknown").lower()
    if status in TERMINAL_MARKET_STATUSES:
        return None
    category = str(raw.get("category") or "unknown")
    event_title = str(raw.get("event_title") or "")
    display_name = market_display_name(raw, ticker)
    display_title = market_display_title(title, display_name, event_title)
    title_l = f"{title} {display_name} {event_title}".lower()
    model_key, profile = classify_market(category, title_l)
    public_figure_profile = build_public_figure_profile(display_name, title_l, model_key)

    yes_ask = as_float(raw.get("yes_ask_dollars"))
    no_ask = as_float(raw.get("no_ask_dollars"))
    yes_bid = as_float(raw.get("yes_bid_dollars"))
    no_bid = as_float(raw.get("no_bid_dollars"))
    last_price = as_float(raw.get("last_price_dollars"))
    previous_price = as_float(raw.get("previous_price_dollars"))
    volume_24h = as_float(raw.get("volume_24h_fp")) or 0.0
    volume = as_float(raw.get("volume_fp")) or as_float(raw.get("volume")) or 0.0
    liquidity = as_float(raw.get("liquidity_dollars")) or 0.0
    crossed_book = False
    spread = None
    if yes_ask is not None and no_ask is not None:
        crossed_book = yes_ask + no_ask < 1.0 - 1e-9
        spread = max(0.0, yes_ask + no_ask - 1.0)
    end_dt = parse_date(raw)
    days_to_resolution = None
    if end_dt:
        days_to_resolution = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400

    profile_terms = tuple(str(term) for term in profile.get("terms", ()))
    term_hits = sum(1 for term in FUTURE_CATALYST_TERMS if term in title_l)
    profile_term_hits = sum(1 for term in profile_terms if term in title_l)
    catalyst_score = clamp(0.16 + term_hits * 0.08 + profile_term_hits * 0.12, 0.0, 1.0)
    if status not in {"open", "active"}:
        catalyst_score = clamp(catalyst_score + 0.12, 0.0, 1.0)

    if days_to_resolution is None:
        runway_score = 0.35
    else:
        runway_score = clamp(days_to_resolution / 365, 0.0, 1.0)

    liquidity_score = clamp(math.log10(max(volume + liquidity, volume_24h, 1.0)) / 6, 0.0, 1.0)
    bid_factor = 1.0 if (yes_bid or no_bid) else 0.0
    exitability = clamp(0.6 * clamp(math.log10(max(liquidity, 1.0)) / 4.0, 0.0, 1.0) + 0.4 * bid_factor, 0.0, 1.0)
    spread_score = 0.10 if crossed_book else 0.45 if spread is None else clamp(1.0 - (spread / 0.08), 0.0, 1.0)
    if last_price is not None and previous_price and previous_price > 0:
        recent_move = (last_price - previous_price) / previous_price
        momentum_score = clamp(0.5 + recent_move, 0.0, 1.0)
    else:
        momentum_score = 0.5
    anchor_score = price_anchor_score(yes_ask)
    attention_text = f"{category} {' '.join(profile.get('path', []))}".lower()
    attention_score = 0.18 + 0.28 * any(cat in attention_text for cat in HIGH_ATTENTION_CATEGORIES)
    attention_score += 0.22 if term_hits else 0
    attention_score += 0.18 if runway_score > 0.35 else 0
    attention_score = clamp(attention_score, 0.0, 1.0)

    settlement_source = str(raw.get("settlement_source_url") or raw.get("settlement_sources") or "Kalshi market metadata")
    source_score = 0.75 if settlement_source and settlement_source != "Kalshi market metadata" else 0.45
    domain_signal_score, domain_signals = build_domain_signals(profile, title_l)
    next_catalyst = build_next_catalyst(profile, title_l, status, days_to_resolution)
    flow_signal_score, flow_warning_score, flow_signal = build_flow_signal(
        volume=volume,
        volume_24h=volume_24h,
        liquidity=liquidity,
        liquidity_score=liquidity_score,
        spread_score=spread_score,
        momentum_score=momentum_score,
        last_price=last_price,
        previous_price=previous_price,
    )

    # Live-tournament profiles run on scheduled-match catalysts, so they carry
    # a lower runway floor than slow-drift categories.
    min_runway_days = float(profile.get("min_runway_days", MIN_EARLY_RUNWAY_DAYS))
    too_short_for_early_mark = days_to_resolution is not None and days_to_resolution < min_runway_days
    directionless_penalty = 0.0
    if term_hits == 0 and profile_term_hits == 0:
        directionless_penalty += 0.35
    if domain_signal_score < 0.28:
        directionless_penalty += 0.12
    if too_short_for_early_mark:
        directionless_penalty += 0.55
    elif runway_score < 0.08:
        directionless_penalty += 0.25
    if exitability < 0.12:
        directionless_penalty += 0.12
    directionless_penalty = clamp(directionless_penalty, 0.0, 1.0)

    pre_open = status in PREOPEN_MARKET_STATUSES
    open_price = yes_ask if yes_ask and 0.01 <= yes_ask <= 0.99 else None

    bull_basis = {
        "base": 0.10,
        "next_catalyst": _profile_number(profile, "scenario", "catalyst", 0.16) * catalyst_score,
        "attention_growth": 0.11 * attention_score,
        "time_runway": _profile_number(profile, "scenario", "runway", 0.10) * runway_score,
        "source_backing": 0.06 * source_score,
        "liquidity_exit": 0.05 * exitability,
        "spread_quality": 0.04 * spread_score,
        "price_anchor": 0.06 * anchor_score,
        "domain_model_fit": _profile_number(profile, "scenario", "domain_bull", 0.08) * domain_signal_score,
        "flow_support": 0.04 * flow_signal_score,
        "momentum": 0.06 * max(momentum_score - 0.5, 0.0),
        "pre_open_structure": 0.04 if pre_open else 0.0,
        "directionless_or_short_runway_penalty": -0.12 * directionless_penalty,
    }
    bear_basis = {
        "base": 0.16,
        "directionless_or_short_runway_penalty": 0.18 * directionless_penalty,
        "thin_liquidity": 0.10 * (1 - exitability),
        "wide_spread": 0.08 * (1 - spread_score),
        "negative_momentum": 0.06 * max(0.5 - momentum_score, 0.0),
        "missing_domain_facts": _profile_number(profile, "scenario", "domain_bear", 0.08) * (1 - domain_signal_score),
        "flow_warning": 0.05 * flow_warning_score,
        "source_backing_offset": -0.05 * source_score,
    }
    raw_bull_probability = sum(bull_basis.values())
    raw_bear_probability = sum(bear_basis.values())
    bull_probability = clamp(raw_bull_probability, 0.05, 0.62)
    bear_probability = clamp(raw_bear_probability, 0.08, 0.55)
    base_probability = clamp(1.0 - bull_probability - bear_probability, 0.10, 0.80)
    total = bull_probability + base_probability + bear_probability
    bull_probability /= total
    base_probability /= total
    bear_probability /= total
    scenario_weight_basis = {
        "method": "Heuristic scenario weighting for price appreciation, not calibrated final-outcome probability.",
        "raw_bull_probability": round(raw_bull_probability * 100, 2),
        "raw_bear_probability": round(raw_bear_probability * 100, 2),
        "normalization": "Bull and bear are clamped to conservative ranges, base is the remaining probability, then all three are normalized to 100%.",
        "model_profile": profile.get("name"),
        "category_path": profile.get("path"),
        "bull_components_pct": {name: round(value * 100, 2) for name, value in bull_basis.items()},
        "bear_components_pct": {name: round(value * 100, 2) for name, value in bear_basis.items()},
    }

    bull_price = base_price = bear_price = expected_future_price = expected_repricing_roi = entry_limit = None
    price_scenario_basis: dict[str, Any] = {
        "method": "No tradable ask yet, so expected future price is not calculated.",
        "entry_price": open_price,
    }
    if open_price is not None:
        momentum_advantage = max(momentum_score - 0.5, 0.0)
        negative_momentum = max(0.5 - momentum_score, 0.0)
        bull_delta = (
            0.015
            + _profile_number(profile, "price", "catalyst", 0.035) * catalyst_score
            + 0.025 * attention_score
            + _profile_number(profile, "price", "runway", 0.025) * runway_score
            + 0.020 * liquidity_score
            + 0.020 * momentum_advantage
            + 0.015 * anchor_score
            + _profile_number(profile, "price", "domain_bull", 0.020) * domain_signal_score
            + 0.010 * flow_signal_score
        ) * (1 - open_price)
        base_delta = (
            0.002
            + 0.010 * runway_score
            + 0.006 * attention_score
            + _profile_number(profile, "price", "domain_base", 0.003) * domain_signal_score
            + 0.004 * momentum_advantage
            - 0.003 * (1 - spread_score)
        ) * (1 - open_price)
        bear_delta = -(
            0.025
            + 0.040 * (1 - source_score)
            + 0.035 * directionless_penalty
            + 0.025 * negative_momentum
            + 0.025 * (1 - liquidity_score)
            + 0.015 * (1 - spread_score)
            + _profile_number(profile, "price", "domain_bear", 0.024) * (1 - domain_signal_score)
            + 0.015 * flow_warning_score
        ) * open_price
        bull_price = clamp(open_price + bull_delta, 0.01, 0.95)
        base_price = clamp(open_price + base_delta, 0.01, 0.95)
        bear_price = clamp(open_price + bear_delta, 0.01, 0.95)
        expected_future_price = (
            bull_probability * bull_price
            + base_probability * base_price
            + bear_probability * bear_price
        )
        # Round-trip friction per contract: taker fee on entry, taker fee on
        # the estimated exit price, and half the spread to cross back to the
        # bid. Fee rate comes from core.market_pricing (canonical); the
        # per-order ceil-to-cent is negligible at realistic lot sizes.
        entry_fee = TAKER_FEE_RATE * open_price * (1 - open_price)
        exit_fee = TAKER_FEE_RATE * expected_future_price * (1 - expected_future_price)
        exit_half_spread = (spread or 0.01) / 2
        friction = clamp(entry_fee + exit_fee + exit_half_spread, 0.005, 0.08)
        expected_repricing_roi = (expected_future_price - open_price - friction) / open_price
        entry_limit = clamp(expected_future_price - friction, 0.01, 0.95)
        price_scenario_basis = {
            "method": "Expected future resale price = bull_probability * bull_price + base_probability * base_price + bear_probability * bear_price.",
            "entry_price": round(open_price, 4),
            "friction": round(friction, 4),
            "friction_breakdown": {
                "entry_fee": round(entry_fee, 4),
                "exit_fee_at_expected_price": round(exit_fee, 4),
                "exit_half_spread": round(exit_half_spread, 4),
                "fee_rate": TAKER_FEE_RATE,
            },
            "bull_price_formula": "entry + additive bull_delta scaled by remaining upside room",
            "base_price_formula": "entry + small additive base_delta scaled by remaining upside room",
            "bear_price_formula": "entry - downside_delta scaled by entry price",
            "expected_roi_formula": "(expected_future_price - entry_price - friction) / entry_price",
            "friction_formula": "taker fee at entry + taker fee at expected exit + exit half-spread",
            "roi_policy": "Expected ROI is raw and uncapped for study, but ranking uses a capped ROI signal so tiny-price contracts cannot dominate only because the percentage looks huge.",
            "model_profile": profile.get("name"),
        }

    rank_weights = profile.get("rank_weights") if isinstance(profile.get("rank_weights"), dict) else DEFAULT_RANK_WEIGHTS
    capped_roi_signal = ranking_roi_signal(expected_repricing_roi)
    rank_score = (
        100
        * (
            float(rank_weights.get("catalyst", 0.18)) * catalyst_score
            + float(rank_weights.get("runway", 0.14)) * runway_score
            + float(rank_weights.get("attention", 0.12)) * attention_score
            + float(rank_weights.get("liquidity", 0.10)) * exitability
            + float(rank_weights.get("source", 0.09)) * source_score
            + float(rank_weights.get("spread", 0.08)) * spread_score
            + float(rank_weights.get("anchor", 0.07)) * anchor_score
            + float(rank_weights.get("momentum", 0.05)) * momentum_score
            + float(rank_weights.get("domain", 0.10)) * domain_signal_score
            + float(rank_weights.get("flow", 0.04)) * flow_signal_score
            + float(rank_weights.get("roi_raw", 0.08)) * capped_roi_signal
            - float(rank_weights.get("directionless", 0.24)) * directionless_penalty
        )
    )
    if too_short_for_early_mark:
        rank_score -= 35

    placement_sizing = build_placement_sizing(
        open_price=open_price,
        target_price=expected_future_price,
        rank_score=rank_score,
        expected_repricing_roi=expected_repricing_roi,
        liquidity_score=exitability,
        spread_score=spread_score,
        directionless_penalty=directionless_penalty,
        min_dollars=min_placement_dollars,
        max_dollars=max_placement_dollars,
    )

    if crossed_book:
        probe_status = "Watch only"
    elif too_short_for_early_mark:
        probe_status = "Too directionless"
    elif pre_open:
        probe_status = "Pre-open watch"
    elif directionless_penalty >= 0.55:
        probe_status = "Too directionless"
    elif (
        expected_repricing_roi is not None
        and expected_repricing_roi > 0.08
        and rank_score >= 48
        and expected_future_price is not None
        and open_price is not None
        and (expected_future_price - open_price) >= RISE_FLOOR_DOLLARS
    ):
        probe_status = "Catalyst setup"
    else:
        probe_status = "Watch only"

    if isinstance(price_scenario_basis, dict) and open_price is not None and expected_future_price is not None:
        price_scenario_basis["rise_floor_cents"] = round(RISE_FLOOR_DOLLARS * 100)
        price_scenario_basis["expected_rise_cents"] = round((expected_future_price - open_price) * 100, 1)
        price_scenario_basis["rise_floor_pass"] = (expected_future_price - open_price) >= RISE_FLOOR_DOLLARS

    return Candidate(
        ticker=ticker,
        title=title,
        display_name=display_name,
        display_title=display_title,
        category=category,
        category_path=list(profile.get("path", [category])),
        model_key=model_key,
        model_name=str(profile.get("name") or model_key),
        status=status,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=yes_bid,
        no_bid=no_bid,
        last_price=last_price,
        previous_price=previous_price,
        volume=volume,
        volume_24h=volume_24h,
        liquidity=liquidity,
        spread=round(spread, 4) if spread is not None else None,
        crossed_book=crossed_book,
        end_date=end_dt.isoformat() if end_dt else None,
        days_to_resolution=days_to_resolution,
        settlement_source=settlement_source,
        catalyst_score=round(catalyst_score * 100, 1),
        liquidity_score=round(liquidity_score * 100, 1),
        exitability_score=round(exitability * 100, 1),
        runway_score=round(runway_score * 100, 1),
        attention_score=round(attention_score * 100, 1),
        source_score=round(source_score * 100, 1),
        spread_score=round(spread_score * 100, 1),
        momentum_score=round(momentum_score * 100, 1),
        price_anchor_score=round(anchor_score * 100, 1),
        domain_signal_score=round(domain_signal_score * 100, 1),
        flow_signal_score=round(flow_signal_score * 100, 1),
        directionless_penalty=round(directionless_penalty * 100, 1),
        rank_score=round(rank_score, 1),
        bull_probability=round(bull_probability * 100, 1),
        base_probability=round(base_probability * 100, 1),
        bear_probability=round(bear_probability * 100, 1),
        bull_price=round(bull_price, 4) if bull_price is not None else None,
        base_price=round(base_price, 4) if base_price is not None else None,
        bear_price=round(bear_price, 4) if bear_price is not None else None,
        expected_future_price=round(expected_future_price, 4) if expected_future_price is not None else None,
        expected_repricing_roi=round(expected_repricing_roi * 100, 1) if expected_repricing_roi is not None else None,
        entry_limit=round(entry_limit, 4) if entry_limit is not None else None,
        recommended_placement_dollars=placement_sizing["recommended_placement_dollars"],
        estimated_contracts=placement_sizing["estimated_contracts"],
        estimated_position_cost=placement_sizing["estimated_position_cost"],
        estimated_target_value=placement_sizing["estimated_target_value"],
        estimated_target_profit=placement_sizing["estimated_target_profit"],
        placement_sizing_basis=placement_sizing["basis"],
        next_catalyst=next_catalyst,
        domain_signals=domain_signals,
        flow_signal=flow_signal,
        public_figure_profile=public_figure_profile,
        scenario_weight_basis=scenario_weight_basis,
        price_scenario_basis=price_scenario_basis,
        probe_status=probe_status,
    )


async def fetch_raw_markets(adapter: KalshiAdapter, pages: int, page_limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen: set[str] = set()
    markets: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {"batches": []}

    for status in MARKET_STATUSES_TO_TRY:
        cursor = None
        for _ in range(pages):
            params: dict[str, Any] = {"limit": page_limit}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            resp = await adapter._request("GET", "/markets", params=params, silent=True)
            if not isinstance(resp, dict) or "markets" not in resp:
                diagnostics["batches"].append({"status": status or "default", "count": 0, "error": resp})
                break
            batch = resp.get("markets") or []
            added = 0
            for raw in batch:
                ticker = str(raw.get("ticker") or "")
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    markets.append(raw)
                    added += 1
            diagnostics["batches"].append({"status": status or "default", "count": len(batch), "added": added})
            cursor = resp.get("cursor")
            if not cursor:
                break

    for status in ("open", "unopened"):
        cursor = None
        for _ in range(pages):
            params: dict[str, Any] = {"limit": page_limit, "with_nested_markets": True}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            resp = await adapter._request("GET", "/events", params=params, silent=True)
            if not isinstance(resp, dict) or "events" not in resp:
                diagnostics["batches"].append({"endpoint": "events", "status": status, "count": 0, "error": resp})
                break
            events = resp.get("events") or []
            added = 0
            nested_count = 0
            for event in events:
                event_category = event.get("category") or "unknown"
                event_title = event.get("title") or ""
                for raw in event.get("markets") or []:
                    nested_count += 1
                    ticker = str(raw.get("ticker") or "")
                    if ticker and ticker not in seen:
                        seen.add(ticker)
                        raw["category"] = raw.get("category") or event_category
                        raw["event_title"] = event_title
                        raw["event_ticker"] = event.get("event_ticker")
                        markets.append(raw)
                        added += 1
            diagnostics["batches"].append({
                "endpoint": "events",
                "status": status,
                "events": len(events),
                "nested_markets": nested_count,
                "added": added,
            })
            cursor = resp.get("cursor")
            if not cursor:
                break

    return markets, diagnostics


def augment_soccer_path_features(
    shortlist: list[Candidate], all_candidates: list[Candidate]
) -> list[Candidate]:
    """Attach bracket path math to live World Cup winner candidates.

    Uses the whole winner-market family (not just the shortlist) as the price
    cross-section, the curated bracket file, and mechanical share
    renormalization for elimination scenarios. A candidate whose best
    elimination scenario clears the rise floor is promoted to Catalyst setup;
    the generic additive-delta model cannot see these event-driven rises.
    """
    if not SOCCER_BRACKET_PATH.exists():
        return shortlist
    family = [
        c for c in all_candidates
        if c.ticker.startswith("KXMENWORLDCUP-") and c.yes_bid is not None and c.yes_ask is not None
    ]
    if len(family) < 4:
        return shortlist
    try:
        bracket = json.loads(SOCCER_BRACKET_PATH.read_text())
    except (OSError, ValueError):
        return shortlist
    mids = {c.display_name: (c.yes_bid + c.yes_ask) / 2 for c in family if (c.yes_bid + c.yes_ask) > 0}
    report = cross_section_report(bracket, mids)
    rows = {row["team"]: row for row in report["teams"]}
    for candidate in shortlist:
        row = rows.get(candidate.display_name)
        if not candidate.ticker.startswith("KXMENWORLDCUP-") or row is None:
            continue
        mid = mids.get(candidate.display_name, 0.0)
        scenarios = []
        for beta in row["top_elimination_betas"]:
            rival_mid = mids.get(beta["rival"], 0.0)
            if rival_mid <= 0 or rival_mid >= 1:
                continue
            mechanical_rise = (mid / (1.0 - rival_mid)) - mid
            scenarios.append({
                "rival_eliminated": beta["rival"],
                "expected_rise_cents": round(mechanical_rise * 100, 1),
                "model_beta_pts": round(beta["beta"] * 100, 1),
                "basis": "mechanical share renormalization if the rival exits before meeting this team; model beta adds same-half path relief",
            })
        best_rise = max((s["expected_rise_cents"] for s in scenarios), default=0.0)
        candidate.soccer_path = {
            "market_share_pct": round(row["market_share"] * 100, 1),
            "path_implied_pct": round(row["path_implied"] * 100, 1),
            "consistency_gap_pct": round(row["consistency_gap"] * 100, 1),
            "elimination_scenarios": scenarios,
            "bracket_verified_as_of": bracket.get("verified_as_of"),
            "method": report["method"],
            "caveat": "Bradley-Terry on de-vigged winner shares is partly circular for favorites; treat gaps as internal-tension flags, not external truth.",
        }
        if (
            candidate.probe_status == "Watch only"
            and best_rise >= RISE_FLOOR_DOLLARS * 100
            and candidate.rank_score >= 35
        ):
            candidate.probe_status = "Catalyst setup"
            candidate.soccer_path["rise_floor_pass_via"] = "elimination scenario"
    return shortlist


def build_claude_prompt(
    candidates: list[Candidate],
    max_results: int = 5,
    external_research: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    shortlist = json.dumps([asdict(c) for c in candidates], indent=2)
    research_block = json.dumps(external_research or {}, indent=2)
    return f"""
You are the research analyst for Early Marks, a Kalshi repricing watchlist.

Goal:
- Find markets that may appreciate before resolution.
- Do not optimize for holding to final settlement.
- Do not claim secret information or guaranteed moves.
- Treat unexplained price movement as possible information from the market.
- This is research only. Do not place or recommend immediate live orders.

Use the candidate data below plus the external_research block: recent dated
news snippets fetched per ticker. When external research exists for a
candidate, your sources MUST cite those URLs with their published dates, and
your thesis must be consistent with them. Never cite a URL that is not in the
external_research block. If a candidate has no external research, say plainly
that public source depth is thin and cap the decision at Watch only.
The scenario_weight_basis fields explain exactly where
the bull/base/bear weights came from; do not invent a different basis.
Use next_catalyst, domain_signals, and flow_signal when explaining why the
category-specific model matters. If flow_signal says holder identity or holding
duration is unavailable, say that plainly instead of implying insider knowledge.
Use display_name / placement names in prose. Do not write ticker codes or ticker
suffixes like JSHA, TCAR, or DTRU inside why_i_believe_this.
If public_figure_profile is present, use it as a research checklist. The WHY
must include more than score/ROI math: explain what is coming for the person,
team, party, company, or topic; what past public moves make the path plausible
or weak; what future moves would change attention; and what public facts would
break the thesis. Do not invent specific facts not present in candidate data;
frame missing facts as questions to verify.
When a candidate has a soccer_path block, ground your numbers in it:
path_implied_pct vs market_share_pct is the calculated internal tension;
elimination_scenarios carry expected_rise_cents with the rival named — use the
external research and fixture schedule to date those scenarios, and set
catalyst_date_estimate to the specific match (team vs team, date) that
triggers them. Respect its caveat field. A suggestion-grade candidate
(decision Catalyst setup) must have at least one credible path to a rise of
9 cents or more; smaller expected moves are Watch only by policy.

Return exactly {min(max(1, max_results), len(candidates))} candidates in the
schema-constrained `results` list. Scenario weights are percentages: each must
be between 0 and 100, and bull + base + bear must sum to 100. Price and fair
probability fields are also percentages/cents between 0 and 100. Every result
must use a unique ticker copied exactly from Candidate data.

Candidate data:
{shortlist}

external_research (dated news per ticker; cite these URLs):
{research_block}
"""


def extract_json_array(text: str) -> list[dict[str, Any]]:
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        raise ValueError("Claude did not return a JSON array")
    parsed = json.loads(text[start:end])
    if not isinstance(parsed, list):
        raise ValueError("Claude JSON was not a list")
    return parsed


def _anthropic_message_kwargs(
    *,
    max_tokens: int,
    system: str,
    messages: list[dict[str, str]],
    temperature: float | None,
    output_schema: dict[str, Any] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": settings.claude_scan_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    # Opus 4.8+ rejects legacy sampling controls. Opus 5 is the active
    # Early Marks model, so let its effort/thinking controls drive generation.
    if temperature is not None and not (
        "4-8" in settings.claude_scan_model or settings.claude_scan_model.endswith("-5")
    ):
        kwargs["temperature"] = temperature
    if output_schema is not None or effort is not None:
        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        kwargs["output_config"] = output_config
    return kwargs


def _tavily_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _tavily_search_sync(query: str, api_key: str, max_results: int = 3, days: int = 45) -> list[dict[str, Any]]:
    """One Tavily news search; returns [{title,url,published_date,snippet}] or [] on any failure."""
    end = datetime.now(timezone.utc).date()
    payload = {
        "api_key": api_key,
        "query": query,
        "topic": "news",
        "search_depth": "basic",
        "max_results": max(1, min(max_results, 5)),
        "include_answer": False,
        "include_raw_content": False,
        "start_date": (end - timedelta(days=days)).isoformat(),
        "end_date": end.isoformat(),
    }
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_tavily_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return []
    results = []
    for row in data.get("results", []) or []:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        results.append({
            "title": str(row.get("title") or "")[:160],
            "url": str(row.get("url")),
            "published_date": str(row.get("published_date") or row.get("published_at") or "unknown"),
            "snippet": str(row.get("content") or "")[:400],
        })
    return results


async def gather_external_research(
    candidates: list[Candidate], research_slots: int
) -> dict[str, list[dict[str, Any]]]:
    """Fetch dated external news per top candidate so enrichment can cite real sources."""
    api_key = getattr(settings, "tavily_api_key", "") or os.getenv("TAVILY_API_KEY", "")
    if not api_key or research_slots <= 0:
        return {}
    research: dict[str, list[dict[str, Any]]] = {}
    targets = candidates[: research_slots]

    async def fetch(candidate: Candidate) -> None:
        group = candidate_market_group_title(candidate)
        query = f"{candidate.display_name} {group}".strip()[:180]
        rows = await asyncio.to_thread(_tavily_search_sync, query, api_key)
        if rows:
            research[candidate.ticker] = rows

    await asyncio.gather(*(fetch(c) for c in targets), return_exceptions=True)
    return research


def _anthropic_response_text(response: Any) -> str:
    """Return only final text blocks; Opus 5 may put thinking blocks first."""
    return "".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def validate_claude_results(
    results: Any,
    candidates: list[Candidate],
    max_results: int,
    external_research: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Validate business rules that JSON Schema alone cannot express."""
    if not isinstance(results, list):
        raise ValueError("Claude structured output did not contain a results list")

    expected_count = min(max(1, max_results), len(candidates))
    if len(results) != expected_count:
        raise ValueError(f"Claude returned {len(results)} results; expected {expected_count}")

    candidate_by_ticker = {candidate.ticker: candidate for candidate in candidates}
    allowed_urls = {
        ticker: {
            str(row.get("url"))
            for row in rows
            if isinstance(row, dict) and row.get("url")
        }
        for ticker, rows in external_research.items()
    }
    seen: set[str] = set()

    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ValueError(f"Claude result {index + 1} is not an object")

        ticker = str(item.get("ticker") or "")
        if ticker not in candidate_by_ticker:
            raise ValueError(f"Claude returned unknown ticker {ticker!r}")
        if ticker in seen:
            raise ValueError(f"Claude returned duplicate ticker {ticker}")
        seen.add(ticker)

        weights = item.get("scenario_weights")
        if not isinstance(weights, dict):
            raise ValueError(f"{ticker}: scenario_weights is missing")
        try:
            weight_values = [float(weights[name]) for name in ("bull", "base", "bear")]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{ticker}: scenario weights must be numeric") from exc
        if any(value < 0 or value > 100 for value in weight_values):
            raise ValueError(f"{ticker}: scenario weights must stay between 0 and 100")
        if abs(sum(weight_values) - 100.0) > 0.25:
            raise ValueError(f"{ticker}: scenario weights sum to {sum(weight_values):.2f}, not 100")

        for field in ("expected_future_price_cents", "entry_limit_cents"):
            value = item.get(field)
            if value is not None and not 0 <= float(value) <= 100:
                raise ValueError(f"{ticker}: {field} must stay between 0 and 100")

        fair_range = item.get("fair_probability_range")
        if not isinstance(fair_range, dict):
            raise ValueError(f"{ticker}: fair_probability_range is missing")
        low = float(fair_range.get("low_pct"))
        high = float(fair_range.get("high_pct"))
        if not (0 <= low <= high <= 100):
            raise ValueError(f"{ticker}: fair probability range is invalid")

        decision = str(item.get("decision") or "")
        if decision == "Catalyst setup":
            if item.get("side") == "Watch":
                raise ValueError(f"{ticker}: Catalyst setup cannot use side=Watch")
            if not external_research.get(ticker):
                raise ValueError(f"{ticker}: Catalyst setup has no external research")

            candidate = candidate_by_ticker[ticker]
            target = item.get("expected_future_price_cents")
            direct_rise = (
                float(target) - float(candidate.yes_ask) * 100
                if target is not None and candidate.yes_ask is not None
                else 0.0
            )
            soccer_rise = max(
                (
                    float(row.get("expected_rise_cents") or 0)
                    for row in (candidate.soccer_path or {}).get("elimination_scenarios", [])
                    if isinstance(row, dict)
                ),
                default=0.0,
            )
            if max(direct_rise, soccer_rise) < RISE_FLOOR_DOLLARS * 100:
                raise ValueError(f"{ticker}: Catalyst setup has no supported 9-cent repricing path")

        sources = item.get("sources")
        if not isinstance(sources, list):
            raise ValueError(f"{ticker}: sources must be a list")
        cited_urls = {
            str(source.get("url_or_reference"))
            for source in sources
            if isinstance(source, dict) and str(source.get("url_or_reference") or "").startswith(("http://", "https://"))
        }
        unexpected_urls = cited_urls - allowed_urls.get(ticker, set())
        if unexpected_urls:
            raise ValueError(f"{ticker}: Claude cited URL(s) outside supplied research")
        if external_research.get(ticker) and not cited_urls:
            raise ValueError(f"{ticker}: external research was supplied but no URL was cited")

    return results


async def ask_claude(candidates: list[Candidate], max_results: int = 5) -> tuple[list[dict[str, Any]], str, str | None]:
    external_research = await gather_external_research(
        candidates, research_slots=min(len(candidates), max(5, max_results + 2))
    )
    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = await asyncio.to_thread(
            client.messages.create,
            **_anthropic_message_kwargs(
                max_tokens=16000,
                temperature=None,
                effort="medium",
                output_schema=EARLY_MARKS_RESPONSE_SCHEMA,
                system=(
                    "You produce careful, research-only prediction-market analysis. "
                    "You never claim certainty. You make probability reasoning transparent."
                ),
                messages=[{
                    "role": "user",
                    "content": build_claude_prompt(
                        candidates, max_results=max_results, external_research=external_research
                    ),
                }],
            ),
        )
    except Exception as exc:
        return [], "", f"Claude request failed: {type(exc).__name__}: {exc}"

    raw_text = _anthropic_response_text(response)
    stop_reason = str(getattr(response, "stop_reason", ""))
    if stop_reason != "end_turn":
        return [], raw_text, f"Claude stopped with {stop_reason or 'unknown stop reason'}"
    if not raw_text:
        return [], raw_text, "Claude returned no final text block"

    try:
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("Claude structured output was not an object")
        results = validate_claude_results(
            payload.get("results"), candidates, max_results, external_research
        )
        return results, raw_text, None
    except Exception as exc:
        return [], raw_text, f"Claude structured output failed validation: {exc}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--shortlist", type=int, default=18)
    parser.add_argument("--max-per-market", type=int, default=3)
    parser.add_argument("--unreleased-slots", type=int, default=4)
    parser.add_argument("--min-placement-dollars", type=float, default=DEFAULT_MIN_PLACEMENT_DOLLARS)
    parser.add_argument("--max-placement-dollars", type=float, default=DEFAULT_MAX_PLACEMENT_DOLLARS)
    parser.add_argument("--category", default="all")
    parser.add_argument("--market-status", default="all", choices=("all", "open", "active", "preopen", "unopened"))
    parser.add_argument("--horizon", default="all", choices=("all", "short", "medium", "long"))
    parser.add_argument("--no-claude", action="store_true")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    run_id = args.run_id or f"manual-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"

    write_stage_status(run_id, "connect")
    adapter = KalshiAdapter()
    connected = await adapter.connect()
    print(f"kalshi_connected={connected}")
    print("kalshi_environment=production")
    print(f"claude_key_present={bool(settings.anthropic_api_key)}")

    write_stage_status(run_id, "health")
    write_stage_status(run_id, "pull_markets")
    raw_markets, diagnostics = await fetch_raw_markets(adapter, pages=args.pages, page_limit=args.page_limit)
    for series in LIVE_SERIES_BY_CATEGORY.get((args.category or "").strip().lower(), ()):
        try:
            resp = await adapter._request(
                "GET", "/markets", params={"series_ticker": series, "limit": 200}, silent=True
            )
        except Exception:
            resp = None
        series_markets = resp.get("markets") if isinstance(resp, dict) else None
        if not series_markets:
            continue
        known = {str(raw.get("ticker") or "") for raw in raw_markets}
        added = 0
        for raw in series_markets:
            ticker = str(raw.get("ticker") or "")
            if ticker and ticker not in known and str(raw.get("status") or "").lower() in ACTIVE_MARKET_STATUSES:
                raw_markets.append(raw)
                added += 1
        diagnostics.setdefault("live_series_topup", []).append({"series": series, "added": added})
    write_stage_status(run_id, "category_model")
    candidates = [
        c for c in (
            score_raw_market(
                raw,
                min_placement_dollars=args.min_placement_dollars,
                max_placement_dollars=args.max_placement_dollars,
            )
            for raw in raw_markets
        )
        if c is not None
    ]
    candidates.sort(key=lambda c: c.rank_score, reverse=True)
    write_stage_status(run_id, "rank")
    top, eligible_candidates = select_scan_shortlist(
        candidates,
        shortlist_size=args.shortlist,
        max_per_market=max(1, args.max_per_market),
        unreleased_slots=max(0, args.unreleased_slots),
        category_filter=args.category,
        status_filter=args.market_status,
        horizon_filter=args.horizon,
    )
    top = augment_soccer_path_features(top, candidates)
    if (args.category or "").strip().lower() in LIVE_SERIES_BY_CATEGORY:
        top = prioritize_soccer_path_candidates(top)

    claude_results: list[dict[str, Any]] = []
    claude_raw_text = ""
    claude_parse_error: str | None = None
    claude_status = "skipped"
    claude_requested = bool(not args.no_claude and settings.anthropic_api_key and top)
    write_stage_status(run_id, "enrich")
    if claude_requested:
        claude_results, claude_raw_text, claude_parse_error = await ask_claude(top)
        claude_status = "failed" if claude_parse_error or not claude_results else "succeeded"
    elif args.no_claude:
        claude_status = "disabled"
    elif not settings.anthropic_api_key:
        claude_status = "not_configured"
    elif not top:
        claude_status = "no_candidates"
    top_by_ticker = {c.ticker: c for c in top}

    write_stage_status(run_id, "render")
    output = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "diagnostics": diagnostics,
        "market_count": len(raw_markets),
        "candidate_count": len(candidates),
        "eligible_candidate_count": len(eligible_candidates),
        "available_categories": sorted({candidate_category_label(c) for c in candidates}),
        "available_statuses": sorted({c.status for c in candidates if c.status}),
        "available_horizons": ["short", "medium", "long"],
        "selection_policy": {
            "shortlist": args.shortlist,
            "max_per_market": max(1, args.max_per_market),
            "unreleased_slots": max(0, args.unreleased_slots),
            "min_placement_dollars": max(0.0, args.min_placement_dollars),
            "max_placement_dollars": max(args.min_placement_dollars, args.max_placement_dollars),
            "category": args.category,
            "market_status": args.market_status,
            "horizon": args.horizon,
            "market_groups_selected": len({candidate_market_group_title(c) for c in top}),
            "pre_open_selected": sum(1 for c in top if c.probe_status == "Pre-open watch" or c.status in PREOPEN_MARKET_STATUSES),
        },
        "top_candidates": [asdict(c) for c in top],
        "claude_model": settings.claude_scan_model,
        "claude_status": claude_status,
        "claude_results": claude_results,
        "claude_raw_text": claude_raw_text,
        "claude_parse_error": claude_parse_error,
    }
    PROBE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    PROBE_OUTPUT.write_text(json.dumps(output, indent=2))
    write_stage_status(run_id, "render", done_through=True)

    print(f"raw_markets={len(raw_markets)} candidates={len(candidates)} eligible={len(eligible_candidates)}")
    print(f"wrote={PROBE_OUTPUT}")
    print(
        "selection="
        f"groups={len({candidate_market_group_title(c) for c in top})} "
        f"pre_open={sum(1 for c in top if c.probe_status == 'Pre-open watch' or c.status in PREOPEN_MARKET_STATUSES)} "
        f"max_per_market={max(1, args.max_per_market)} "
        f"placement_size=${max(0.0, args.min_placement_dollars):.0f}-${max(args.min_placement_dollars, args.max_placement_dollars):.0f} "
        f"category={args.category} status={args.market_status} horizon={args.horizon}"
    )
    print("\nTop heuristic candidates:")
    for c in top[:5]:
        price = "pre-open/no ask" if c.yes_ask is None else f"{round(c.yes_ask * 100)}c"
        roi = "n/a" if c.expected_repricing_roi is None else f"{c.expected_repricing_roi}%"
        stake = "n/a" if c.recommended_placement_dollars is None else f"${c.recommended_placement_dollars:.0f}"
        print(f"- {c.display_name} | {c.probe_status} | score={c.rank_score} | stake={stake} | price={price} | repricing_roi={roi}")
        print(f"  {c.display_title[:160]}")

    if claude_results:
        print("\nClaude research shortlist:")
        for item in claude_results[:5]:
            ticker = str(item.get("ticker") or "")
            candidate = top_by_ticker.get(ticker)
            label = candidate.display_name if candidate else ticker
            why = str(item.get("why_i_believe_this", ""))
            if candidate:
                for token in (ticker, ticker.rsplit("-", 1)[-1] if "-" in ticker else ""):
                    if token:
                        why = re.sub(rf"\b{re.escape(token)}\b", candidate.display_name, why)
            print(f"- {label} | {item.get('decision')} | entry_limit={item.get('entry_limit_cents')}c")
            print(f"  {why[:260]}")
    elif claude_parse_error:
        print("\nClaude enrichment failed. Raw output was saved for inspection.")
        print(f"claude_error={claude_parse_error}")

    if claude_requested and (claude_parse_error or not claude_results):
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
