"""
Read-only World Cup market inventory probe.

This script:
- fetches the real public matchday schedule,
- scans live Kalshi Sports markets,
- links markets to the matchday teams,
- classifies market/leg types such as exact score, totals, BTTS, winner,
  player props, and Kalshi combo/RFQ-style markets.

It does not place orders, create approvals, print secrets, or require live
trading to be enabled.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

from adapters.kalshi_adapter import KalshiAdapter  # noqa: E402
from config.settings import settings  # noqa: E402
from core.world_cup_service import (  # noqa: E402
    Match,
    WorldCupScheduleProvider,
    WorldCupService,
    _contains_team,
    utc_now,
)


MAX_EXAMPLES_PER_TYPE = 8


def classify_text(text: str, team_names: list[str] | None = None) -> str:
    value = text.lower()
    if "exact score" in value or re.search(r"\b\d+\s*[-:]\s*\d+\b", value) or re.search(r"\b\d+\s+to\s+\d+\b", value):
        return "exact_score"
    if "both teams" in value and ("score" in value or "to score" in value):
        return "both_teams_to_score"
    if re.search(r"\bover\s+\d+(?:\.\d+)?\s+goals?", value) or re.search(r"\bunder\s+\d+(?:\.\d+)?\s+goals?", value):
        return "total_goals"
    if "team total" in value:
        return "team_total"
    if "wins by more than" in value or "win by more than" in value:
        return "win_margin"
    if re.search(r":[ ]*\d+\+", text) or any(term in value for term in ("goalscorer", "shots", "assist", "score or assist", "player")):
        return "player_prop"
    normalized_leg = re.sub(r"^(yes|no)\s+", "", value).strip()
    if team_names and any(_contains_team(normalized_leg, team) for team in team_names):
        return "match_winner_or_result"
    if any(term in value for term in (" tie", "draw", "wins", "win", "beats", "defeat")):
        return "match_winner_or_result"
    return "other"


def split_legs(title: str) -> list[str]:
    legs = [part.strip() for part in title.split(",") if part.strip()]
    return legs or [title.strip()]


def linked_team_names(match: Match, market, service: WorldCupService) -> list[str]:
    names = []
    title = service._market_search_text(market)
    if _contains_team(title, match.home_team):
        names.append(match.home_team)
    if _contains_team(title, match.away_team):
        names.append(match.away_team)
    return names


def matchday_team_names(matches: list[Match]) -> list[str]:
    names = {match.home_team for match in matches}
    names.update(match.away_team for match in matches)
    return sorted(names)


def market_kind(title: str, ticker: str, team_names: list[str]) -> str:
    if "KXMVE" in ticker.upper() or "," in title:
        return "kalshi_combo_or_rfq"
    return classify_text(title, team_names)


async def find_dashboard_matches(provider: WorldCupScheduleProvider, days_ahead: int) -> tuple[str, list[Match]]:
    now = utc_now()
    user_tz = ZoneInfo(os.getenv("USER_TIMEZONE", "America/New_York"))
    start_day = now.astimezone(user_tz).date()
    for offset in range(days_ahead + 1):
        day = start_day + timedelta(days=offset)
        matches, error = await provider.get_matches_for_date(day, fetched_at=now)
        if error:
            print(f"Schedule fetch warning for {day}: {error}")
        if matches:
            return day.isoformat(), matches
    return start_day.isoformat(), []


async def main() -> int:
    adapter = KalshiAdapter()
    # Market discovery is public/read-only. Clearing auth avoids signing
    # warnings and avoids touching private account endpoints.
    adapter.api_key_id = ""
    adapter.private_key_path = ""
    adapter.private_key = None

    service = WorldCupService(lambda: adapter)
    provider = WorldCupScheduleProvider()
    dashboard_day, matches = await find_dashboard_matches(provider, settings.world_cup_schedule_days_ahead)

    print("World Cup market inventory probe")
    print("Kalshi environment: production")
    print(f"Dashboard day: {dashboard_day}")
    print(f"Schedule matches found: {len(matches)}")

    if not matches:
        print("No real schedule matches found. No market scan performed.")
        return 1

    for idx, match in enumerate(matches, start=1):
        print(f"  {idx}. {match.away_team} at {match.home_team} | kickoff_utc={match.kickoff_utc.isoformat()} | venue={match.venue}")

    markets, error = await service._discover_markets(matches)
    if error:
        print(f"Kalshi market discovery error: {error}")
        return 1

    linked = service._link_markets(matches, markets)
    team_names = matchday_team_names(matches)
    linked_total = sum(len(items) for items in linked.values())
    unique_linked_tickers = {market.id for items in linked.values() for market in items}
    type_counts: Counter[str] = Counter()
    leg_type_counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)

    print()
    print(f"Kalshi candidate markets discovered: {len(markets)}")
    print(f"Linked market rows across matches: {linked_total}")
    print(f"Unique linked market tickers: {len(unique_linked_tickers)}")
    print()

    for match in matches:
        markets_for_match = linked.get(match.id, [])
        print(f"{match.away_team} at {match.home_team}: {len(markets_for_match)} linked market rows")
        if not markets_for_match:
            print("  none linked")
            continue

        per_match_types: Counter[str] = Counter()
        for market in markets_for_match:
            title = market.question
            kind = market_kind(title, market.id, team_names)
            per_match_types[kind] += 1
            type_counts[kind] += 1

            leg_types = Counter(classify_text(leg, team_names) for leg in split_legs(title))
            leg_type_counts.update(leg_types)
            teams = ", ".join(linked_team_names(match, market, service)) or "team implied by ticker/rules"
            if len(examples[kind]) < MAX_EXAMPLES_PER_TYPE:
                examples[kind].append(f"{market.id} | teams={teams} | {title}")

        print("  market rows by type:")
        for kind, count in sorted(per_match_types.items()):
            print(f"    {kind}: {count}")
        print("  first examples:")
        for market in markets_for_match[:5]:
            leg_summary = Counter(classify_text(leg, team_names) for leg in split_legs(market.question))
            leg_summary_text = ", ".join(f"{key}={value}" for key, value in sorted(leg_summary.items()))
            print(f"    {market.id} | {market_kind(market.question, market.id, team_names)} | legs: {leg_summary_text}")
            print(f"      {market.question[:220]}")

    print()
    print("Overall linked market rows by market type:")
    for kind, count in sorted(type_counts.items()):
        print(f"  {kind}: {count}")

    print()
    print("Overall combo/single leg contents by type:")
    for kind, count in sorted(leg_type_counts.items()):
        print(f"  {kind}: {count}")

    print()
    print("Examples by market type:")
    for kind, rows in sorted(examples.items()):
        print(f"  {kind}:")
        for row in rows:
            print(f"    {row[:260]}")

    if leg_type_counts.get("exact_score", 0) == 0 and type_counts.get("exact_score", 0) == 0:
        print()
        print("Exact score note: no exact-score market wording was found in this live scan.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
