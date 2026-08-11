"""
Real-data-only World Cup Kalshi assistant service.

This module deliberately does not call the generic TradingAgent. The World Cup
route must fail visibly when required live inputs are unavailable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx  # type: ignore

from adapters.base import Market
from config.settings import settings
from core.world_cup_math import (
    ProbabilitySignal,
    calculate_probability_signal,
    confidence_at_least,
    derive_quote_snapshot,
    normalize_midpoint_probabilities,
)


LIVE_CONNECTED = "LIVE DATA CONNECTED"
LIVE_DEGRADED = "LIVE DATA DEGRADED"
LIVE_UNAVAILABLE = "LIVE DATA UNAVAILABLE"

KALSHI_UNAVAILABLE = "Kalshi data unavailable — recommendations disabled."
QUOTE_STALE = "Quote stale — re-quote required."
MAPPING_UNAVAILABLE = "Market mapping unavailable — recommendation disabled."
LINEUP_UNAVAILABLE = "Lineup data unavailable/provisional — player-prop approval disabled."
SOURCES_UNAVAILABLE = "Sources unavailable — approval disabled."
CALIBRATION_UNAVAILABLE = "Calibration unavailable — probabilities are uncalibrated."
PROVIDER_UNAVAILABLE = "Model provider unavailable — recommendations disabled."
MODEL_INPUTS_UNAVAILABLE = "Sourced statistical model inputs unavailable — using market-implied probability only."
TRUE_COMBO_UNAVAILABLE = "True combo/RFQ quote handling unavailable — recommendation disabled."
ORDERBOOK_DEPTH_INSUFFICIENT = "Order-book depth does not cover the proposed quantity — approval disabled."
NO_WORLD_CUP_MARKETS = "No current-matchday-only Kalshi World Cup markets available."
NO_SCHEDULE = "Schedule data unavailable — recommendations disabled."
NO_MATCHES_TODAY = "No World Cup matches today for selected timezone."
NO_MATCHES_IN_LOOKAHEAD = "No World Cup matches found in configured lookahead window."

SCOPE_TODAY_ONLY = "today-only"
SCOPE_TODAY_OR_NEXT_MATCHDAY = "today-or-next-matchday"
SCOPE_LABELS = {
    SCOPE_TODAY_ONLY: "today only",
    SCOPE_TODAY_OR_NEXT_MATCHDAY: "today or next matchday",
}

SLIP_STRATEGY_BALANCED = "balanced"
SLIP_STRATEGY_BIG_PAYOUT = "big-payout"
SLIP_STRATEGY_PLAYER_PROPS = "player-props"
SLIP_STRATEGY_LABELS = {
    SLIP_STRATEGY_BALANCED: "Balanced",
    SLIP_STRATEGY_BIG_PAYOUT: "Big payout",
    SLIP_STRATEGY_PLAYER_PROPS: "Player props",
}

DEFAULT_MAX_SLIPS = 6
ORDERBOOK_EVALUATION_CONCURRENCY = 12

TEAM_ALIASES = {
    "usa": ["usa", "united states", "usmnt", "u.s."],
    "korea republic": ["south korea", "korea republic", "korea"],
    "czechia": ["czechia", "czech republic"],
}

HOST_TIMEZONES = {
    "atlanta": "America/New_York",
    "boston": "America/New_York",
    "east rutherford": "America/New_York",
    "new york": "America/New_York",
    "philadelphia": "America/New_York",
    "miami": "America/New_York",
    "toronto": "America/Toronto",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "kansas city": "America/Chicago",
    "mexico city": "America/Mexico_City",
    "monterrey": "America/Monterrey",
    "guadalajara": "America/Mexico_City",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "santa clara": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "vancouver": "America/Vancouver",
}


@dataclass(frozen=True)
class Match:
    id: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    venue: str
    venue_city: str
    status: str
    source_title: str
    source_url: str
    source_fetched_at: datetime


@dataclass(frozen=True)
class RuntimeSettings:
    recommendation_style: str = "Conservative"
    scope: str = SCOPE_TODAY_OR_NEXT_MATCHDAY
    market_categories_enabled: tuple[str, ...] = ("match winner/result", "over/under goals", "both teams to score", "team totals", "player props")
    max_legs: int = 4
    max_legs_per_match: int = 1
    minimum_confidence: str = "HIGH"
    show_blocked_trades: bool = True
    basket_placement: str = "manual-per-leg"
    slip_strategy: str = SLIP_STRATEGY_BIG_PAYOUT
    max_slips: int = DEFAULT_MAX_SLIPS
    no_unresolved_same_match_correlation: bool = True
    no_stale_quotes: bool = True
    no_unsourced_critical_factors: bool = True
    paper_auto_approval: bool = False


@dataclass
class ApprovalLock:
    lock_id: str
    card_hash: str
    card: dict[str, Any]
    created_at: str
    status: str = "locked"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def stable_hash(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _team_tokens(name: str) -> set[str]:
    base = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    tokens = {part for part in base.split() if len(part) > 1}
    aliases = TEAM_ALIASES.get(name.lower(), [])
    for alias in aliases:
        tokens.update(part for part in re.sub(r"[^a-z0-9 ]+", " ", alias.lower()).split() if len(part) > 1)
    return tokens


def _contains_team(title: str, team: str) -> bool:
    title_lower = title.lower()
    for token in _team_tokens(team):
        if re.search(rf"\b{re.escape(token)}\b", title_lower):
            return True
    return False


def _parse_espn_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class WorldCupScheduleProvider:
    """Fetch source-backed World Cup schedule data from ESPN's public API."""

    source_url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

    async def get_matches_for_date(self, day: date, fetched_at: datetime | None = None) -> tuple[list[Match], str | None]:
        fetched = fetched_at or utc_now()
        params = {"dates": day.strftime("%Y%m%d")}
        try:
            async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "KalshiWorldCupAssistant/1.0"}) as client:
                resp = await client.get(self.source_url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            return [], f"{NO_SCHEDULE} ({exc})"

        matches: list[Match] = []
        for event in payload.get("events", []):
            match = self._parse_event(event, fetched)
            if match:
                matches.append(match)
        matches.sort(key=lambda match: match.kickoff_utc)
        return matches, None

    def _parse_event(self, event: dict[str, Any], fetched_at: datetime) -> Match | None:
        competitions = event.get("competitions") or []
        competition = competitions[0] if competitions else {}
        competitors = competition.get("competitors") or []
        home = next((item for item in competitors if item.get("homeAway") == "home"), None)
        away = next((item for item in competitors if item.get("homeAway") == "away"), None)
        if not home or not away:
            return None

        venue = competition.get("venue") or {}
        status = (event.get("status") or {}).get("type", {}).get("description") or (event.get("status") or {}).get("type", {}).get("name", "")
        return Match(
            id=str(event.get("id") or event.get("uid") or event.get("name")),
            home_team=(home.get("team") or {}).get("displayName", ""),
            away_team=(away.get("team") or {}).get("displayName", ""),
            kickoff_utc=_parse_espn_datetime(event.get("date", "")),
            venue=venue.get("fullName", ""),
            venue_city=venue.get("address", {}).get("city", "") or venue.get("city", ""),
            status=status,
            source_title="ESPN FIFA World Cup scoreboard API",
            source_url=self.source_url,
            source_fetched_at=fetched_at,
        )


class PlayerHistoryProvider:
    """Fetch source-backed player profile/history context for player prop cards."""

    base_url = "https://www.thesportsdb.com/api/v1/json/3"

    def __init__(self):
        self._cache: dict[str, dict[str, Any]] = {}

    async def get_player_context(self, player_name: str, fetched_at: datetime | None = None) -> dict[str, Any]:
        clean_name = " ".join(str(player_name or "").split())
        if not clean_name:
            return {
                "status": "unavailable",
                "player_name": None,
                "reason": "Player name could not be parsed from the market title.",
                "sources": [],
            }
        cache_key = clean_name.lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        fetched = fetched_at or utc_now()
        search_url = f"{self.base_url}/searchplayers.php"
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "KalshiWorldCupAssistant/1.0"}) as client:
                search_resp = await client.get(search_url, params={"p": clean_name})
                search_resp.raise_for_status()
                search_payload = search_resp.json()
                players = search_payload.get("player") or search_payload.get("players") or []
                selected = self._choose_player(clean_name, players)
                if not selected:
                    context = {
                        "status": "not_found",
                        "player_name": clean_name,
                        "reason": "No matching player found in TheSportsDB search.",
                        "performance_stats_status": "unavailable",
                        "sources": [{
                            "title": "TheSportsDB searchplayers API",
                            "url": f"{search_url}?p={clean_name.replace(' ', '%20')}",
                            "timestamp": isoformat(fetched),
                            "status": "queried",
                        }],
                    }
                    self._cache[cache_key] = context
                    return context

                details = selected
                player_id = selected.get("idPlayer")
                if player_id:
                    lookup_url = f"{self.base_url}/lookupplayer.php"
                    lookup_resp = await client.get(lookup_url, params={"id": player_id})
                    lookup_resp.raise_for_status()
                    lookup_payload = lookup_resp.json()
                    lookup_players = lookup_payload.get("players") or lookup_payload.get("player") or []
                    if lookup_players:
                        details = lookup_players[0]
        except Exception as exc:
            context = {
                "status": "unavailable",
                "player_name": clean_name,
                "reason": f"Player history source unavailable: {exc}",
                "performance_stats_status": "unavailable",
                "sources": [{
                    "title": "TheSportsDB searchplayers API",
                    "url": f"{search_url}?p={clean_name.replace(' ', '%20')}",
                    "timestamp": isoformat(fetched),
                    "status": "error",
                }],
            }
            self._cache[cache_key] = context
            return context

        context = self._context_from_sportsdb(clean_name, details, fetched)
        self._cache[cache_key] = context
        return context

    def _choose_player(self, player_name: str, players: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not players:
            return None
        target = player_name.lower()
        for player in players:
            if str(player.get("strPlayer", "")).lower() == target:
                return player
        return players[0]

    def _context_from_sportsdb(self, requested_name: str, player: dict[str, Any], fetched_at: datetime) -> dict[str, Any]:
        player_id = player.get("idPlayer")
        espn_id = player.get("idESPN")
        description = " ".join(str(player.get("strDescriptionEN") or "").split())
        if len(description) > 520:
            description = f"{description[:520].rstrip()}..."
        sources = [
            {
                "title": "TheSportsDB player lookup API",
                "url": f"{self.base_url}/lookupplayer.php?id={player_id}" if player_id else f"{self.base_url}/searchplayers.php?p={requested_name.replace(' ', '%20')}",
                "timestamp": isoformat(fetched_at),
                "status": "available",
            }
        ]
        links = []
        if espn_id:
            links.extend([
                {"label": "ESPN player page", "url": f"https://www.espn.com/soccer/player/_/id/{espn_id}"},
                {"label": "ESPN stats", "url": f"https://www.espn.com/soccer/player/stats/_/id/{espn_id}"},
                {"label": "ESPN matches", "url": f"https://www.espn.com/soccer/player/matches/_/id/{espn_id}"},
            ])
        return {
            "status": "available",
            "requested_name": requested_name,
            "player_name": player.get("strPlayer") or requested_name,
            "team": player.get("strTeam"),
            "national_team": player.get("strTeam2") or player.get("strTeamNational"),
            "nationality": player.get("strNationality"),
            "position": player.get("strPosition"),
            "status_text": player.get("strStatus"),
            "date_born": player.get("dateBorn"),
            "profile_image": player.get("strThumb") or player.get("strCutout"),
            "history_excerpt": description or "No biography/history text returned by the source.",
            "performance_stats_status": "recent match logs unavailable from configured public source",
            "probability_use": "Profile/history is displayed for research context only; probability remains market-implied until sourced recent-performance stats are available.",
            "links": links,
            "sources": sources,
        }


class WorldCupService:
    def __init__(
        self,
        adapter_factory: Callable[[], Any],
        schedule_provider: WorldCupScheduleProvider | None = None,
        player_history_provider: PlayerHistoryProvider | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ):
        self.adapter_factory = adapter_factory
        self.schedule_provider = schedule_provider or WorldCupScheduleProvider()
        self.player_history_provider = player_history_provider or PlayerHistoryProvider()
        self.now_fn = now_fn
        self.runtime_settings = RuntimeSettings()
        self.approval_locks: dict[str, ApprovalLock] = {}

    def provider_status(self) -> dict[str, Any]:
        claude_key_configured = bool(settings.anthropic_api_key)
        claude_model = settings.world_cup_claude_model or "claude-opus-5"
        openai_selectable = bool(settings.world_cup_openai_enabled and settings.openai_api_key)
        both_selectable = bool(
            settings.world_cup_both_mode_enabled
            and claude_key_configured
            and openai_selectable
            and settings.world_cup_both_combination_rule
        )
        return {
            "active": "Claude" if claude_key_configured else None,
            "claude": {
                "label": "Claude",
                "active": claude_key_configured,
                "selectable": claude_key_configured,
                "api_key_configured": claude_key_configured,
                "model": claude_model,
                "disabled_reason": None if claude_key_configured else PROVIDER_UNAVAILABLE,
            },
            "openai": {
                "label": "OpenAI",
                "active": False,
                "selectable": openai_selectable,
                "api_key_configured": bool(settings.openai_api_key),
                "model": settings.openai_model,
                "disabled_reason": None if openai_selectable else "OpenAI — disabled until configured.",
            },
            "both": {
                "label": "Both / Sync Mode",
                "active": False,
                "selectable": both_selectable,
                "combination_rule": settings.world_cup_both_combination_rule,
                "disabled_reason": None if both_selectable else "Both / Sync Mode — planned.",
            },
            "blocked_reason": None if claude_key_configured else PROVIDER_UNAVAILABLE,
        }

    def calibration_status(self) -> dict[str, Any]:
        path = Path(settings.world_cup_calibration_file)
        payload: dict[str, Any] = {}
        if path.exists() and path.is_file():
            try:
                loaded = json.loads(path.read_text())
                payload = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                payload = {}
        aggregate = payload.get("aggregate") if isinstance(payload.get("aggregate"), dict) else {}
        resolved_predictions = int(
            payload.get("resolved_predictions")
            or payload.get("n")
            or aggregate.get("n")
            or 0
        )
        event_types = payload.get("event_types") or aggregate.get("event_types") or []
        event_type_count = len(event_types) if isinstance(event_types, (list, tuple, set)) else int(event_types or 0)
        brier = payload.get("brier") if payload.get("brier") is not None else aggregate.get("brier")
        market_brier = (
            payload.get("market_brier")
            if payload.get("market_brier") is not None
            else aggregate.get("market_brier")
        )
        available = (
            resolved_predictions >= 30
            and event_type_count >= 2
            and isinstance(brier, (int, float))
            and isinstance(market_brier, (int, float))
            and float(brier) < float(market_brier)
        )
        return {
            "available": available,
            "path": str(path),
            "status": "available" if available else "unavailable",
            "resolved_predictions": resolved_predictions,
            "event_type_count": event_type_count,
            "brier": brier,
            "market_brier": market_brier,
            "beats_market_baseline": (
                float(brier) < float(market_brier)
                if isinstance(brier, (int, float)) and isinstance(market_brier, (int, float))
                else False
            ),
            "blocked_reason": None if available else CALIBRATION_UNAVAILABLE,
        }

    def route_status(self, blocked_reasons: list[str] | None = None) -> dict[str, Any]:
        reasons = [reason for reason in (blocked_reasons or []) if reason]
        provider = self.provider_status()
        calibration = self.calibration_status()
        if provider["blocked_reason"]:
            reasons.append(provider["blocked_reason"])
        if calibration["blocked_reason"]:
            reasons.append(calibration["blocked_reason"])

        unique_reasons = list(dict.fromkeys(reasons))
        if any(reason in unique_reasons for reason in (KALSHI_UNAVAILABLE, NO_SCHEDULE, NO_MATCHES_TODAY, NO_MATCHES_IN_LOOKAHEAD, PROVIDER_UNAVAILABLE, NO_WORLD_CUP_MARKETS)):
            label = LIVE_UNAVAILABLE
        elif unique_reasons:
            label = LIVE_DEGRADED
        else:
            label = LIVE_CONNECTED

        return {
            "label": label,
            "blocked_reasons": unique_reasons,
            "provider": provider,
            "calibration": calibration,
            "settings_summary": self.settings_summary(label, unique_reasons),
        }

    def settings_summary(self, data_status: str, blocked_reasons: list[str] | None = None) -> dict[str, Any]:
        rs = self.runtime_settings
        return {
            "data_status": data_status,
            "blocked_reason": (blocked_reasons or [None])[0],
            "provider": "Claude" if self.provider_status()["claude"]["active"] else "Unavailable",
            "recommendation_style": rs.recommendation_style,
            "scope": SCOPE_LABELS.get(rs.scope, rs.scope),
            "scope_value": rs.scope,
            "market_categories_enabled": list(rs.market_categories_enabled),
            "max_legs": rs.max_legs,
            "max_legs_per_match": rs.max_legs_per_match,
            "minimum_confidence": rs.minimum_confidence,
            "blocked_trades": "shown" if rs.show_blocked_trades else "hidden",
            "basket_placement": rs.basket_placement,
            "slip_strategy": rs.slip_strategy,
            "slip_strategy_label": SLIP_STRATEGY_LABELS.get(rs.slip_strategy, rs.slip_strategy),
            "max_slips": rs.max_slips,
        }

    async def get_matchday(self, user_timezone: str = "America/New_York") -> dict[str, Any]:
        tz = ZoneInfo(user_timezone)
        local_today = self.now_fn().astimezone(tz).date()
        schedule_errors: list[str] = []
        selected_matches: list[Match] = []
        selected_day = local_today

        days_to_check = 1 if self.runtime_settings.scope == SCOPE_TODAY_ONLY else max(1, settings.world_cup_schedule_days_ahead + 1)
        for offset in range(days_to_check):
            day = local_today + timedelta(days=offset)
            matches, error = await self.schedule_provider.get_matches_for_date(day, fetched_at=self.now_fn())
            if error:
                schedule_errors.append(error)
            if matches:
                selected_day = day
                selected_matches = matches
                break

        if not selected_matches:
            empty_reason = NO_MATCHES_TODAY if self.runtime_settings.scope == SCOPE_TODAY_ONLY else NO_MATCHES_IN_LOOKAHEAD
            if schedule_errors:
                empty_reason = schedule_errors[0]
            status = self.route_status([empty_reason])
            return {
                "status": status,
                "dashboard_day": selected_day.isoformat(),
                "matchday_scope": self.runtime_settings.scope,
                "matches": [],
                "markets": [],
                "candidates": [],
                "basket": self._empty_basket([]),
                "slips": [],
                "candidate_count": 0,
                "slip_count": 0,
                "message": empty_reason,
            }

        markets, market_error = await self._discover_markets(selected_matches)
        linked = self._link_markets(selected_matches, markets)
        candidates = await self._evaluate_linked_markets(linked)
        slips = self._build_research_slips(candidates)
        await self._enrich_player_contexts(candidates, slips)
        basket = slips[0] if slips else self._empty_basket([])
        blocked = []
        if market_error:
            blocked.append(market_error)
        elif not markets:
            blocked.append(NO_WORLD_CUP_MARKETS)

        status = self.route_status(blocked)
        return {
            "status": status,
            "dashboard_day": selected_day.isoformat(),
            "matchday_scope": self.runtime_settings.scope,
            "matches": [self._match_to_api(match, linked.get(match.id, []), user_timezone) for match in selected_matches],
            "markets": [self._market_to_api(market) for market in markets],
            "candidates": candidates,
            "basket": basket,
            "slips": slips,
            "candidate_count": len(candidates),
            "slip_count": len(slips),
            "message": status["blocked_reasons"][0] if status["blocked_reasons"] else None,
        }

    async def evaluate(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        user_timezone = (payload or {}).get("user_timezone", "America/New_York")
        return await self.get_matchday(user_timezone=user_timezone)

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        blocked: list[str] = []
        current = self.runtime_settings

        if "recommendation_style" in updates and updates["recommendation_style"] != "Conservative":
            blocked.append("Balanced and Aggressive are disabled until exact thresholds, sizing caps, and risk rules are defined and tested.")

        max_legs = int(updates.get("max_legs", current.max_legs))
        max_legs_per_match = int(updates.get("max_legs_per_match", current.max_legs_per_match))
        show_blocked = bool(updates.get("show_blocked_trades", current.show_blocked_trades))
        slip_strategy = str(updates.get("slip_strategy", current.slip_strategy))
        if slip_strategy not in SLIP_STRATEGY_LABELS:
            blocked.append("Slip strategy must be balanced, big-payout, or player-props.")
            slip_strategy = current.slip_strategy
        max_slips = int(updates.get("max_slips", current.max_slips))
        scope = str(updates.get("scope", current.scope))
        if scope not in SCOPE_LABELS:
            blocked.append("Matchday scope must be today-only or today-or-next-matchday.")
            scope = current.scope
        basket_placement = str(updates.get("basket_placement", current.basket_placement))
        if basket_placement != "manual-per-leg":
            blocked.append("Sequential basket placement is disabled until quote-refresh, partial-fill, and re-approval rules are tested.")
            basket_placement = "manual-per-leg"

        self.runtime_settings = RuntimeSettings(
            recommendation_style="Conservative",
            scope=scope,
            market_categories_enabled=current.market_categories_enabled,
            max_legs=max(1, min(max_legs, 8)),
            max_legs_per_match=max(1, min(max_legs_per_match, 3)),
            minimum_confidence=current.minimum_confidence,
            show_blocked_trades=show_blocked,
            basket_placement=basket_placement,
            slip_strategy=slip_strategy,
            max_slips=max(1, min(max_slips, 12)),
            no_unresolved_same_match_correlation=current.no_unresolved_same_match_correlation,
            no_stale_quotes=current.no_stale_quotes,
            no_unsourced_critical_factors=current.no_unsourced_critical_factors,
            paper_auto_approval=current.paper_auto_approval,
        )
        status = self.route_status(blocked)
        return {"status": "updated" if not blocked else "updated_with_warnings", "warnings": blocked, "settings_summary": status["settings_summary"]}

    def approval_lock(self, card: dict[str, Any]) -> dict[str, Any]:
        immutable = self._lockable_card(card)
        card_hash = stable_hash(immutable)
        lock_id = f"wc-lock-{card_hash[:16]}"
        lock = ApprovalLock(
            lock_id=lock_id,
            card_hash=card_hash,
            card=immutable,
            created_at=isoformat(self.now_fn()) or "",
        )
        self.approval_locks[lock_id] = lock
        return {"status": "locked", "lock": asdict(lock)}

    def paper_approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        lock_id = str(payload.get("lock_id", ""))
        card = payload.get("card")
        lock = self.approval_locks.get(lock_id)
        if not lock:
            return {"status": "error", "error": "Approval lock not found."}
        if card is not None and stable_hash(self._lockable_card(card)) != lock.card_hash:
            return {"status": "error", "error": "Approval card changed — re-approval required."}
        stale_reason = self._quote_stale_reason(lock.card)
        if stale_reason:
            return {"status": "blocked", "error": stale_reason}
        if not lock.card.get("approval_eligible", False):
            return {"status": "blocked", "error": lock.card.get("disabled_reason") or "Approval disabled."}
        if lock.card.get("execution_mode") == "live":
            return {"status": "blocked", "error": "Live order placement is disabled for this route."}
        lock.status = "paper_approved"
        return {
            "status": "paper_approved",
            "lock_id": lock.lock_id,
            "card_hash": lock.card_hash,
            "simulated_order": {
                "mode": "paper",
                "type": lock.card.get("type", "single"),
                "side": lock.card.get("side"),
                "market": lock.card.get("market"),
                "legs": len(lock.card.get("legs") or []),
                "total_cost": (lock.card.get("totals") or {}).get("cost"),
                "contracts": lock.card.get("contracts"),
                "price": lock.card.get("price"),
                "live_order_placed": False,
            },
        }

    async def _discover_markets(self, matches: list[Match]) -> tuple[list[Market], str | None]:
        try:
            adapter = self.adapter_factory()
            if hasattr(adapter, "get_world_cup_candidate_markets"):
                raw_markets = await adapter.get_world_cup_candidate_markets(limit=1000)
            else:
                raw_markets = await adapter.get_markets(active_only=True, category="Sports", limit=1000)
        except Exception:
            return [], KALSHI_UNAVAILABLE

        markets = [market for market in raw_markets if self._looks_like_world_cup_market(market, matches)]
        return markets, None

    def _looks_like_world_cup_market(self, market: Market, matches: list[Match]) -> bool:
        if self._is_combo_market(market):
            return self._combo_market_is_current_world_cup_only(market, matches)

        text = self._market_search_text(market)
        if any(term in text for term in ("tournament winner", "group winner", "golden boot", "champion", "win the world cup")):
            return False
        if any(term in text for term in ("world cup", "fifa", "soccer", "football", "kxwc")):
            return any(self._market_mentions_match(market, match) for match in matches)
        for match in matches:
            if self._market_mentions_match(market, match):
                return True
        return False

    def _market_search_text(self, market: Market) -> str:
        raw = market.raw or {}
        parts = [
            market.question,
            market.id,
            str(raw.get("ticker", "")),
            str(raw.get("event_ticker", "")),
            str(raw.get("series_ticker", "")),
            str(raw.get("title", "")),
            str(raw.get("subtitle", "")),
            str(raw.get("yes_sub_title", "")),
            str(raw.get("no_sub_title", "")),
            str(raw.get("rules_primary", "")),
            str(raw.get("rules_secondary", "")),
        ]
        return " ".join(part for part in parts if part).lower()

    def _market_mentions_match(self, market: Market, match: Match) -> bool:
        text = self._market_search_text(market)
        return _contains_team(text, match.home_team) and _contains_team(text, match.away_team)

    def _is_combo_market(self, market: Market) -> bool:
        haystack = f"{market.id} {(market.raw or {}).get('event_ticker', '')} {market.question}".upper()
        return "KXMVE" in haystack or "," in market.question

    def _combo_legs(self, market: Market) -> list[str]:
        return [part.strip() for part in market.question.split(",") if part.strip()]

    def _combo_market_is_current_world_cup_only(self, market: Market, matches: list[Match]) -> bool:
        legs = self._combo_legs(market)
        if len(legs) < 2:
            return False
        if not any(self._leg_mentions_matchday_team(leg, matches) for leg in legs):
            return False
        return all(self._combo_leg_is_current_world_cup_related(leg, matches) for leg in legs)

    def _leg_mentions_matchday_team(self, leg: str, matches: list[Match]) -> bool:
        return any(_contains_team(leg, match.home_team) or _contains_team(leg, match.away_team) for match in matches)

    def _combo_leg_is_current_world_cup_related(self, leg: str, matches: list[Match]) -> bool:
        normalized = re.sub(r"^(yes|no)\s+", "", leg.lower()).strip()
        if self._leg_mentions_matchday_team(normalized, matches):
            return True
        if "both teams to score" in normalized:
            return True
        if re.search(r"\b(over|under)\s+\d+(?:\.\d+)?\s+goals?\s+scored\b", normalized):
            return True
        if normalized in {"tie", "draw"}:
            return True
        return False

    def _link_markets(self, matches: list[Match], markets: list[Market]) -> dict[str, list[Market]]:
        linked: dict[str, list[Market]] = {match.id: [] for match in matches}
        for match in matches:
            for market in markets:
                if self._market_mentions_match(market, match):
                    linked[match.id].append(market)
                elif self._is_combo_market(market) and (
                    title := market.question
                ) and (
                    _contains_team(title, match.home_team) or _contains_team(title, match.away_team)
                ):
                    linked[match.id].append(market)
        return linked

    async def _evaluate_linked_markets(self, linked: dict[str, list[Market]]) -> list[dict[str, Any]]:
        jobs = []
        semaphore = asyncio.Semaphore(ORDERBOOK_EVALUATION_CONCURRENCY)

        async def evaluate_with_limit(match_id: str, market: Market, normalized_midpoint: float | None) -> dict[str, Any]:
            async with semaphore:
                return await self._evaluate_market(match_id, market, normalized_midpoint)

        for match_id, markets in linked.items():
            normalized = self._normalized_collection_priors(markets)
            for market in markets:
                jobs.append(evaluate_with_limit(match_id, market, normalized.get(market.id) or self._midpoint_from_market(market)))

        candidates = await asyncio.gather(*jobs) if jobs else []
        candidates.sort(
            key=lambda item: (
                not item["approval_eligible"],
                -float(item["trade"].get("confidence_adjusted_probability_gap") or 0),
            )
        )
        if not self.runtime_settings.show_blocked_trades:
            candidates = [item for item in candidates if item["approval_eligible"]]
        return candidates

    def _normalized_collection_priors(self, markets: list[Market]) -> dict[str, float]:
        grouped: dict[str, dict[str, float]] = {}
        for market in markets:
            collection_id = (market.raw or {}).get("mutually_exclusive_collection")
            midpoint = self._midpoint_from_market(market)
            if collection_id and midpoint is not None:
                grouped.setdefault(str(collection_id), {})[market.id] = midpoint

        normalized: dict[str, float] = {}
        for collection in grouped.values():
            normalized.update(normalize_midpoint_probabilities(collection))
        return normalized

    async def _evaluate_market(self, match_id: str, market: Market, normalized_midpoint: float | None) -> dict[str, Any]:
        quote_timestamp = self.now_fn()
        mapping = self._map_market(market)
        side = "YES"
        blockers: list[str] = []
        warnings: list[str] = []
        if mapping is None:
            blockers.append(MAPPING_UNAVAILABLE)
        if self._is_combo_market(market):
            blockers.append(TRUE_COMBO_UNAVAILABLE)

        orderbook = {}
        try:
            adapter = self.adapter_factory()
            orderbook = await adapter.get_orderbook(market.id)
        except Exception:
            blockers.append(KALSHI_UNAVAILABLE)

        contracts = 1.0
        quote = derive_quote_snapshot(orderbook, side, contracts)
        executable_price = quote.executable_price or 0.5
        if quote.executable_price is None:
            blockers.append(KALSHI_UNAVAILABLE)
        if not quote.depth_covers_quantity:
            blockers.append(ORDERBOOK_DEPTH_INSUFFICIENT)
        if quote.side_spread is not None and (
            quote.side_spread < 0
            or quote.side_spread > getattr(settings, "max_orderbook_spread", 0.08)
        ):
            blockers.append("Order-book spread is invalid or exceeds the configured maximum.")

        market_probability = normalized_midpoint if normalized_midpoint is not None else self._midpoint_from_market(market)
        model_probability = self._sourced_model_probability(market)
        if model_probability is None:
            blockers.append(MODEL_INPUTS_UNAVAILABLE)
            model_probability = market_probability
        if model_probability is None:
            blockers.append(MAPPING_UNAVAILABLE)
            model_probability = 0.0

        confidence = "HIGH" if not blockers else "LOW"
        signal = calculate_probability_signal(
            model_yes_probability=model_probability,
            market_yes_probability=market_probability if market_probability is not None else executable_price,
            side=side,
            confidence=confidence,
        )

        if self.provider_status()["blocked_reason"]:
            blockers.append(PROVIDER_UNAVAILABLE)
        if self.calibration_status()["blocked_reason"]:
            blockers.append(CALIBRATION_UNAVAILABLE)
        if mapping and mapping.get("lineup_required"):
            if self.runtime_settings.no_unsourced_critical_factors:
                blockers.append(LINEUP_UNAVAILABLE)
            else:
                warnings.append(LINEUP_UNAVAILABLE)
        if mapping and mapping.get("source_required") and not market.resolution_source:
            if self.runtime_settings.no_unsourced_critical_factors:
                blockers.append(SOURCES_UNAVAILABLE)
            else:
                warnings.append(SOURCES_UNAVAILABLE)
        if signal.confidence_adjusted_probability_gap <= settings.min_edge_threshold:
            blockers.append(
                f"Independent probability gap does not exceed the configured {settings.min_edge_threshold:.0%} threshold."
            )
        if not confidence_at_least(confidence, self.runtime_settings.minimum_confidence):
            blockers.append(f"Confidence below {self.runtime_settings.minimum_confidence}.")

        blockers = list(dict.fromkeys(blockers))
        warnings = list(dict.fromkeys(warnings))
        approval_eligible = not blockers
        disabled_reason = None if approval_eligible else blockers[0]
        trade = self._trade_card(
            market=market,
            signal=signal,
            quote=quote,
            quote_timestamp=quote_timestamp,
            approval_eligible=approval_eligible,
            disabled_reason=disabled_reason,
            warnings=warnings,
        )
        return {
            "id": f"{match_id}:{market.id}:YES",
            "match_id": match_id,
            "market_id": market.id,
            "status": "paper-ready" if approval_eligible else "blocked",
            "approval_eligible": approval_eligible,
            "paper_approval_eligible": approval_eligible,
            "live_order_placement": "disabled",
            "disabled_reason": disabled_reason,
            "blocked_reasons": blockers,
            "warnings": warnings,
            "mapping": mapping or {"status": "unmapped", "description": MAPPING_UNAVAILABLE},
            "trade": trade,
            "research": self._research_trace(market, mapping, signal, quote, blockers, warnings),
        }

    def _map_market(self, market: Market) -> dict[str, Any] | None:
        title = market.question.lower()
        if self._is_combo_market(market):
            return {
                "status": "mapped_combo_blocked",
                "type": "kalshi_combo_or_rfq",
                "description": "Kalshi multi-leg combo/RFQ-style market. Legs must be shown separately and require exact RFQ quote/correlation handling before approval.",
                "lineup_required": False,
                "source_required": False,
                "combo": True,
            }
        exact_score = re.search(r"\b(\d+)\s*[-:]\s*(\d+)\b", title) or re.search(r"\b(\d+)\s+to\s+(\d+)\b", title)
        if "exact score" in title or exact_score:
            score = exact_score.group(0) if exact_score else "declared exact score"
            return {
                "status": "mapped",
                "type": "exact_score",
                "description": f"YES maps to the simulated final score equaling {score}.",
                "lineup_required": False,
                "source_required": False,
            }
        if "player" in title or "goalscorer" in title or "shots" in title or "score or assist" in title or "assists" in title or re.search(r":\s*\d+\+", title):
            player_name = self._extract_player_name(market)
            return {
                "status": "mapped_provisional",
                "type": "player_prop",
                "description": "Player prop requires sourced lineup/player model inputs.",
                "lineup_required": True,
                "source_required": True,
                "player_name": player_name,
            }
        over_match = re.search(r"over\s+(\d+(?:\.\d+)?)\s+goals?", title)
        if over_match:
            threshold = float(over_match.group(1))
            return {
                "status": "mapped",
                "type": "over_goals",
                "description": f"YES maps to total simulated goals > {threshold}.",
                "lineup_required": False,
                "source_required": False,
            }
        if "both teams" in title and ("score" in title or "to score" in title):
            return {
                "status": "mapped",
                "type": "both_teams_score",
                "description": "YES maps to both teams scoring at least one goal.",
                "lineup_required": False,
                "source_required": False,
            }
        if "team total" in title or re.search(r"\bover\s+\d+(?:\.\d+)?\s+.* goals?", title):
            return {
                "status": "mapped",
                "type": "team_total",
                "description": "YES maps to a team-specific simulated goal total threshold.",
                "lineup_required": False,
                "source_required": False,
            }
        if "wins by more than" in title or "win by more than" in title:
            return {
                "status": "mapped",
                "type": "win_margin",
                "description": "YES maps to the named team winning by more than the listed goal margin.",
                "lineup_required": False,
                "source_required": False,
            }
        if any(term in title for term in ("win", "winner", "beat", "beats", "defeat")):
            return {
                "status": "mapped",
                "type": "match_winner",
                "description": "YES maps to the named team scoring more goals than its opponent.",
                "lineup_required": False,
                "source_required": False,
            }
        return None

    def _extract_player_name(self, market: Market) -> str | None:
        title = re.sub(r"^(will|yes|no)\s+", "", market.question.strip(), flags=re.IGNORECASE)
        if ":" in title:
            candidate = title.split(":", 1)[0].strip()
            return candidate or None
        match = re.match(
            r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+){0,4})\s+(?:to\s+)?(?:score|record|have|make|get|assist|shots?|goals?)\b",
            title,
        )
        if match:
            return match.group(1).strip()
        rules = str((market.raw or {}).get("rules_primary") or "")
        rules_match = re.match(
            r"If\s+([A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+){0,4})\s+",
            rules,
        )
        return rules_match.group(1).strip() if rules_match else None

    def _midpoint_from_market(self, market: Market) -> float | None:
        raw = market.raw or {}
        yes_bid = self._price(raw.get("yes_bid_dollars") or raw.get("yes_bid"))
        no_bid = self._price(raw.get("no_bid_dollars") or raw.get("no_bid"))
        yes_ask = self._price(raw.get("yes_ask_dollars") or raw.get("yes_ask"))
        if yes_bid is not None and no_bid is not None:
            return (yes_bid + (1.0 - no_bid)) / 2.0
        if yes_bid is not None and yes_ask is not None:
            return (yes_bid + yes_ask) / 2.0
        return market.yes_price if 0 < market.yes_price < 1 else None

    def _price(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price / 100.0 if price > 1 else price

    def _sourced_model_probability(self, market: Market) -> float | None:
        inputs = (market.raw or {}).get("world_cup_model_inputs")
        if not isinstance(inputs, dict):
            return None
        sources = inputs.get("sources") or inputs.get("source")
        if not sources:
            return None
        for key in ("model_yes_probability", "yes_probability", "probability"):
            value = inputs.get(key)
            if value is None:
                continue
            try:
                probability = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 < probability < 1.0:
                return probability
        return None

    def _trade_card(
        self,
        *,
        market: Market,
        signal: ProbabilitySignal,
        quote: Any,
        quote_timestamp: datetime,
        approval_eligible: bool,
        disabled_reason: str | None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        price = quote.executable_price
        return {
            "title": market.question,
            "market": market.id,
            "side": signal.side,
            "price": price,
            "hit_probability": signal.model_side_probability,
            "market_implied_probability": signal.market_side_probability,
            "probability_gap": signal.probability_gap,
            "confidence_adjusted_probability_gap": signal.confidence_adjusted_probability_gap,
            "cost": price,
            "contracts": 1.0,
            "odds": price,
            "max_payout": 1.0,
            "pricing_source": "One-contract Kalshi quote. Confirm final fees, total cost, and payout in Kalshi before placing.",
            "status": "paper-ready" if approval_eligible else "blocked",
            "approval_state": "paper-enabled" if approval_eligible else "disabled",
            "paper_approval_state": "enabled" if approval_eligible else "disabled",
            "live_approval_state": "disabled",
            "approval_eligible": approval_eligible,
            "disabled_reason": disabled_reason,
            "warnings": warnings or [],
            "quote_timestamp": isoformat(quote_timestamp),
            "quote_fresh_seconds": settings.world_cup_quote_fresh_seconds,
            "execution_mode": "paper",
            "live_order_placement": "disabled",
        }

    def _research_trace(
        self,
        market: Market,
        mapping: dict[str, Any] | None,
        signal: ProbabilitySignal,
        quote: Any,
        blockers: list[str],
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        source_url = market.resolution_source or None
        warnings = warnings or []
        return {
            "summary": "Real-data evaluation produced no paper-eligible card." if blockers else "Paper-eligible real-data card. Live order placement remains disabled.",
            "inputs": {
                "model_probability_source": (
                    "missing"
                    if MODEL_INPUTS_UNAVAILABLE in blockers
                    else "sourced world_cup_model_inputs"
                ),
                "executable_price_source": "Kalshi orderbook implied ask",
                "quote": asdict(quote),
            },
            "probability_calculation": {
                "model_yes_probability": signal.model_yes_probability,
                "model_side_probability": signal.model_side_probability,
                "market_side_probability": signal.market_side_probability,
                "probability_gap": signal.probability_gap,
                "confidence_adjusted_probability_gap": signal.confidence_adjusted_probability_gap,
                "confidence": signal.confidence,
                "confidence_multiplier": signal.confidence_multiplier,
                "mapping": mapping,
            },
            "market_price_calculation": {
                "executable_price": quote.executable_price,
                "source": "Kalshi orderbook; final trade math stays in Kalshi",
            },
            "calibration": self.calibration_status(),
            "sources": [
                {
                    "title": "Kalshi market response",
                    "url": source_url or "Kalshi API",
                    "timestamp": isoformat(self.now_fn()),
                    "status": "available" if source_url else "source_url_unavailable",
                }
            ],
            "risk_notes": blockers,
            "why_not_approved": blockers,
            "warnings": warnings,
            "what_would_change_this": [
                "A fresh Kalshi quote or a materially different independent probability estimate.",
                "Available calibration storage would upgrade the warning state.",
                "Explicit market-to-outcome mapping and sourced critical factors improve confidence.",
            ],
        }

    def _build_research_slips(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pool = self._slip_candidate_pool(candidates)
        slips: list[dict[str, Any]] = []
        used_markets: set[str] = set()
        for slip_index in range(self.runtime_settings.max_slips):
            legs: list[dict[str, Any]] = []
            match_counts: dict[str, int] = {}
            for candidate in pool:
                market_id = str(candidate.get("market_id", ""))
                match_id = str(candidate.get("match_id", ""))
                if not market_id or market_id in used_markets:
                    continue
                if match_counts.get(match_id, 0) >= self.runtime_settings.max_legs_per_match:
                    continue
                legs.append(candidate)
                match_counts[match_id] = match_counts.get(match_id, 0) + 1
                if len(legs) >= self.runtime_settings.max_legs:
                    break
            if not legs:
                break
            slip = self._research_slip_from_legs(legs, slip_index + 1)
            slips.append(slip)
            used_markets.update(str(leg.get("market_id", "")) for leg in legs)
        return slips

    def _slip_candidate_pool(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        seen_markets: set[str] = set()
        for candidate in candidates:
            if (candidate.get("mapping") or {}).get("combo"):
                continue
            market_id = str(candidate.get("market_id", ""))
            if not market_id or market_id in seen_markets:
                continue
            seen_markets.add(market_id)
            pool.append(candidate)
        strategy = self.runtime_settings.slip_strategy
        if strategy == SLIP_STRATEGY_PLAYER_PROPS:
            player_pool = [candidate for candidate in pool if (candidate.get("mapping") or {}).get("type") == "player_prop"]
            if player_pool:
                pool = player_pool
            pool.sort(key=self._longshot_candidate_key)
        elif strategy == SLIP_STRATEGY_BIG_PAYOUT:
            pool.sort(key=self._longshot_candidate_key)
        return pool

    def _longshot_candidate_key(self, candidate: dict[str, Any]) -> tuple[float, float]:
        trade = candidate.get("trade") or {}
        mapping_type = (candidate.get("mapping") or {}).get("type")
        category_rank = {
            "player_prop": 0.0,
            "exact_score": 1.0,
            "win_margin": 2.0,
            "team_total": 3.0,
            "both_teams_score": 4.0,
            "over_goals": 5.0,
            "match_winner": 6.0,
        }.get(str(mapping_type), 9.0)
        price = float(trade.get("price") or 1.0)
        return (category_rank, price)

    def _research_slip_from_legs(self, legs: list[dict[str, Any]], slip_number: int) -> dict[str, Any]:
        return self._build_basket(legs, slip_number=slip_number)

    def _build_basket(self, legs: list[dict[str, Any]], slip_number: int | None = None) -> dict[str, Any]:
        legs = list(legs)
        seen_markets: set[str] = set()
        deduped_legs = []
        for leg in legs:
            market_id = str(leg.get("market_id", ""))
            if market_id and market_id not in seen_markets:
                seen_markets.add(market_id)
                deduped_legs.append(leg)
        legs = deduped_legs
        totals = {
            "cost": sum(float(leg["trade"]["cost"]) for leg in legs),
            "max_payout": sum(float(leg["trade"]["max_payout"]) for leg in legs),
        }
        warnings = list(dict.fromkeys(
            warning
            for leg in legs
            for warning in (leg.get("warnings") or (leg.get("trade") or {}).get("warnings") or [])
        ))
        blockers = []
        if len({leg["match_id"] for leg in legs}) < len(legs):
            correlation_message = "Same-match correlation is unresolved — grouped approval is disabled."
            if self.runtime_settings.no_unresolved_same_match_correlation:
                blockers.append(correlation_message)
            else:
                warnings.append(correlation_message)
        for leg in legs:
            if not leg["approval_eligible"]:
                blockers.append(f"{leg['market_id']}: {leg['disabled_reason']}")
        blockers = list(dict.fromkeys(blockers))
        slip_title = f"Kalshi-Style Slip #{slip_number}" if slip_number else "Kalshi-Style Slip"
        slip_hash = stable_hash({
            "legs": [f"{leg.get('market_id')}:{leg.get('trade', {}).get('contracts')}" for leg in legs],
            "slip_number": slip_number,
        })[:12]
        return {
            "type": "basket_of_singles",
            "title": slip_title,
            "slip_id": f"wc-slip-{slip_hash}",
            "status": "paper-ready" if legs and not blockers else "blocked",
            "approval_label": "Paper Approve Slip",
            "placement_mode": self.runtime_settings.basket_placement,
            "slip_strategy": self.runtime_settings.slip_strategy,
            "slip_strategy_label": SLIP_STRATEGY_LABELS.get(self.runtime_settings.slip_strategy, self.runtime_settings.slip_strategy),
            "execution_mode": "paper",
            "live_order_placement": "disabled",
            "live_approval_state": "disabled",
            "paper_approval_state": "enabled" if legs and not blockers else "disabled",
            "quote_timestamp": legs[0]["trade"].get("quote_timestamp") if legs else None,
            "quote_fresh_seconds": settings.world_cup_quote_fresh_seconds,
            "legs": legs,
            "totals": totals,
            "full_payout_requires": "Every leg must win to receive the full gross payout shown.",
            "pricing_source": "One-contract Kalshi quotes only. Confirm final fees, totals, and payout in Kalshi.",
            "partial_fill_risk": "Research grouping only. Open each leg in Kalshi for a fresh quote and manual approval.",
            "correlation_notes": blockers or ["Cross-match legs treated independently only when no shared condition is detected."],
            "warnings": warnings,
            "approval_eligible": bool(legs and not blockers),
            "disabled_reason": blockers[0] if blockers else None,
        }

    async def _enrich_player_contexts(self, candidates: list[dict[str, Any]], slips: list[dict[str, Any]]) -> None:
        player_cards: list[dict[str, Any]] = []
        for slip in slips:
            for leg in slip.get("legs") or []:
                if (leg.get("mapping") or {}).get("type") == "player_prop":
                    player_cards.append(leg)
        if not player_cards:
            return

        unique_names = {
            (card.get("mapping") or {}).get("player_name")
            for card in player_cards
            if (card.get("mapping") or {}).get("player_name")
        }
        semaphore = asyncio.Semaphore(4)

        async def fetch_context(name: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                return name, await self.player_history_provider.get_player_context(name, fetched_at=self.now_fn())

        contexts = dict(await asyncio.gather(*(fetch_context(name) for name in unique_names))) if unique_names else {}
        for card in player_cards:
            player_name = (card.get("mapping") or {}).get("player_name")
            if player_name and player_name in contexts:
                self._apply_player_context(card, contexts[player_name])

        for slip in slips:
            slip["warnings"] = list(dict.fromkeys(
                warning
                for leg in (slip.get("legs") or [])
                for warning in (leg.get("warnings") or (leg.get("trade") or {}).get("warnings") or [])
            ))

        context_by_market = {
            card.get("market_id"): (card.get("trade") or {}).get("player_context")
            for card in player_cards
            if (card.get("trade") or {}).get("player_context")
        }
        for candidate in candidates:
            context = context_by_market.get(candidate.get("market_id"))
            if context:
                self._apply_player_context(candidate, context)

    def _apply_player_context(self, card: dict[str, Any], context: dict[str, Any]) -> None:
        trade = card.setdefault("trade", {})
        trade["player_context"] = context
        warnings = list(dict.fromkeys(list(card.get("warnings") or []) + self._player_context_warnings(context)))
        card["warnings"] = warnings
        trade["warnings"] = list(dict.fromkeys(list(trade.get("warnings") or []) + self._player_context_warnings(context)))
        research = card.setdefault("research", {})
        research["player_history"] = context
        research["warnings"] = list(dict.fromkeys(list(research.get("warnings") or []) + self._player_context_warnings(context)))
        sources = list(research.get("sources") or [])
        for source in context.get("sources") or []:
            if source not in sources:
                sources.append(source)
        research["sources"] = sources

    def _player_context_warnings(self, context: dict[str, Any]) -> list[str]:
        if context.get("status") != "available":
            return [f"Player history unavailable — {context.get('reason') or 'source did not return a match'}."]
        if context.get("performance_stats_status") != "available":
            return ["Recent player performance stats unavailable — player context is profile/history only."]
        return []

    def _empty_basket(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return self._build_basket(candidates)

    def _match_to_api(self, match: Match, linked_markets: list[Market], user_timezone: str) -> dict[str, Any]:
        venue_tz_name = self._venue_timezone(match)
        user_tz = ZoneInfo(user_timezone)
        venue_tz = ZoneInfo(venue_tz_name)
        return {
            "id": match.id,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "kickoff_utc": isoformat(match.kickoff_utc),
            "venue_local_time": match.kickoff_utc.astimezone(venue_tz).isoformat(),
            "user_local_time": match.kickoff_utc.astimezone(user_tz).isoformat(),
            "venue": match.venue,
            "venue_city": match.venue_city,
            "match_status": match.status,
            "lineup_status": "unavailable/provisional",
            "linked_market_count": len(linked_markets),
            "linked_kalshi_markets": [self._market_to_api(market) for market in linked_markets],
            "evaluation_status": "evaluated" if linked_markets else "skipped",
            "source": {
                "title": match.source_title,
                "url": match.source_url,
                "timestamp": isoformat(match.source_fetched_at),
            },
        }

    def _market_to_api(self, market: Market) -> dict[str, Any]:
        return {
            "ticker": market.id,
            "title": market.question,
            "category": market.category,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "midpoint": self._midpoint_from_market(market),
            "volume_24h": market.volume_24h,
            "total_volume": market.total_volume,
            "liquidity": market.liquidity,
            "end_date": isoformat(market.end_date),
        }

    def _venue_timezone(self, match: Match) -> str:
        haystack = f"{match.venue_city} {match.venue}".lower()
        for city, tz in HOST_TIMEZONES.items():
            if city in haystack:
                return tz
        return "UTC"

    def _immutable_card(self, card: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "type", "title", "slip_id", "market", "side", "price", "cost", "contracts", "odds",
            "hit_probability", "market_implied_probability", "probability_gap",
            "confidence_adjusted_probability_gap", "max_payout", "pricing_source",
            "status", "approval_state", "approval_eligible", "disabled_reason",
            "paper_approval_state", "paper_approval_eligible", "live_approval_state",
            "quote_timestamp", "quote_fresh_seconds", "execution_mode", "placement_mode",
            "slip_strategy", "slip_strategy_label", "warnings", "player_context", "full_payout_requires",
            "legs", "totals", "partial_fill_risk", "correlation_notes",
            "approval_label", "live_order_placement",
        }
        return {key: card.get(key) for key in sorted(allowed) if key in card}

    def _lockable_card(self, card: dict[str, Any]) -> dict[str, Any]:
        immutable = self._immutable_card(card)
        stale_reason = self._quote_stale_reason(immutable)
        if stale_reason:
            immutable["approval_eligible"] = False
            immutable["approval_state"] = "disabled"
            immutable["disabled_reason"] = stale_reason
        return immutable

    def _quote_stale_reason(self, card: dict[str, Any]) -> str | None:
        timestamp = card.get("quote_timestamp")
        if not timestamp:
            return QUOTE_STALE
        try:
            quote_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return QUOTE_STALE
        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=timezone.utc)
        threshold = int(card.get("quote_fresh_seconds") or settings.world_cup_quote_fresh_seconds)
        age = (self.now_fn() - quote_time.astimezone(timezone.utc)).total_seconds()
        return QUOTE_STALE if age > threshold else None


def redacted_environment_status() -> dict[str, bool]:
    return {
        "ANTHROPIC_API_KEY": bool(os.getenv("ANTHROPIC_API_KEY")),
        "OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "KALSHI_API_KEY_ID": bool(os.getenv("KALSHI_API_KEY_ID")),
    }
