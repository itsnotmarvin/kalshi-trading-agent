"""Fetch structured sports context for supported Kalshi market analysis.

The engine reads ESPN's unofficial API for scores, standings, and injury data.
External availability and schemas are not guaranteed, and the returned context
does not by itself establish forecast accuracy.
"""
import re
import httpx
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field


# ── ESPN API (unofficial but stable) ─────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Map Kalshi categories/tickers to ESPN sport/league paths
SPORT_LEAGUE_MAP = {
    "basketball": "basketball/nba",
    "nba": "basketball/nba",
    "football": "football/nfl",
    "nfl": "football/nfl",
    "baseball": "baseball/mlb",
    "mlb": "baseball/mlb",
    "hockey": "hockey/nhl",
    "nhl": "hockey/nhl",
    "soccer": "soccer/usa.1",  # MLS
    "mls": "soccer/usa.1",
    "ncaab": "basketball/mens-college-basketball",
    "ncaaf": "football/college-football",
    "wnba": "basketball/wnba",
    "mma": "mma/ufc",
    "ufc": "mma/ufc",
}

# Common team name aliases for matching Kalshi market titles
TEAM_ALIASES: dict[str, list[str]] = {
    # NBA
    "BOS": ["celtics", "boston"],
    "LAL": ["lakers", "los angeles lakers", "la lakers"],
    "GSW": ["warriors", "golden state"],
    "MIL": ["bucks", "milwaukee"],
    "DEN": ["nuggets", "denver nuggets"],
    "PHI": ["76ers", "sixers", "philadelphia"],
    "NYK": ["knicks", "new york knicks"],
    "MIA": ["heat", "miami heat"],
    "DAL": ["mavericks", "mavs", "dallas mavericks"],
    "PHX": ["suns", "phoenix suns"],
    "LAC": ["clippers", "la clippers"],
    "MIN": ["timberwolves", "wolves", "minnesota"],
    "OKC": ["thunder", "oklahoma city"],
    "CLE": ["cavaliers", "cavs", "cleveland"],
    "SAC": ["kings", "sacramento"],
    "IND": ["pacers", "indiana"],
    "ORL": ["magic", "orlando"],
    "ATL": ["hawks", "atlanta hawks"],
    "BKN": ["nets", "brooklyn"],
    "CHI": ["bulls", "chicago bulls"],
    "TOR": ["raptors", "toronto"],
    "HOU": ["rockets", "houston rockets"],
    "MEM": ["grizzlies", "memphis"],
    "NOP": ["pelicans", "new orleans"],
    "POR": ["trail blazers", "blazers", "portland"],
    "SAS": ["spurs", "san antonio"],
    "CHA": ["hornets", "charlotte"],
    "DET": ["pistons", "detroit pistons"],
    "UTA": ["jazz", "utah"],
    "WAS": ["wizards", "washington wizards"],
    # MLB
    "NYY": ["yankees", "new york yankees"],
    "BOS_MLB": ["red sox", "boston red sox"],
    "LAD": ["dodgers", "los angeles dodgers"],
    "HOU_MLB": ["astros", "houston astros"],
    "ATL_MLB": ["braves", "atlanta braves"],
    "NYM": ["mets", "new york mets"],
    "PHI_MLB": ["phillies", "philadelphia phillies"],
    "SD": ["padres", "san diego padres"],
    "CHC": ["cubs", "chicago cubs"],
    "SF": ["giants", "san francisco giants"],
    "SEA": ["mariners", "seattle mariners"],
    "TEX": ["rangers", "texas rangers"],
    "BAL": ["orioles", "baltimore orioles"],
    "MIN_MLB": ["twins", "minnesota twins"],
    "TB": ["rays", "tampa bay rays"],
    "CLE_MLB": ["guardians", "cleveland guardians"],
    "MIL_MLB": ["brewers", "milwaukee brewers"],
    # NHL
    "EDM": ["oilers", "edmonton"],
    "FLA": ["panthers", "florida panthers"],
    "DAL_NHL": ["stars", "dallas stars"],
    "COL": ["avalanche", "colorado"],
    "VGK": ["golden knights", "vegas"],
    "CAR": ["hurricanes", "carolina"],
    "NYR": ["rangers", "new york rangers"],
    "TOR_NHL": ["maple leafs", "leafs", "toronto maple leafs"],
    "BOS_NHL": ["bruins", "boston bruins"],
    "WPG": ["jets", "winnipeg"],
    "WSH": ["capitals", "caps", "washington capitals"],
    # NFL
    "KC": ["chiefs", "kansas city"],
    "BUF": ["bills", "buffalo"],
    "SF_NFL": ["49ers", "niners", "san francisco 49ers"],
    "PHI_NFL": ["eagles", "philadelphia eagles"],
    "BAL_NFL": ["ravens", "baltimore ravens"],
    "DET_NFL": ["lions", "detroit lions"],
    "DAL_NFL": ["cowboys", "dallas cowboys"],
    "GB": ["packers", "green bay"],
}


@dataclass
class TeamStats:
    """Stats for one team in a matchup."""
    name: str
    abbreviation: str
    record: str = ""          # e.g. "45-27"
    standing: str = ""        # e.g. "2nd in Eastern Conference"
    recent_form: str = ""     # e.g. "W-W-L-W-W (last 5)"
    home_away_record: str = ""
    score: int | None = None  # If game is live/completed
    injuries: list[str] = field(default_factory=list)
    key_players: list[str] = field(default_factory=list)


@dataclass
class MatchupContext:
    """Full context for a sports matchup."""
    sport: str
    league: str
    home_team: TeamStats
    away_team: TeamStats
    game_time: str = ""
    venue: str = ""
    status: str = ""          # "pre", "in", "post"
    spread: str = ""          # e.g. "HOU -3.5"
    over_under: str = ""
    headline: str = ""
    odds_summary: str = ""
    injuries_summary: str = ""

    def to_prompt_context(self) -> str:
        """Format as structured text for Claude's research prompt."""
        lines = [
            f"[SPORTS DATA ENGINE — GROUND TRUTH]",
            f"Sport: {self.sport.upper()} | League: {self.league.upper()}",
            f"Matchup: {self.away_team.name} @ {self.home_team.name}",
        ]
        if self.game_time:
            lines.append(f"Game Time: {self.game_time}")
        if self.venue:
            lines.append(f"Venue: {self.venue}")
        if self.status:
            lines.append(f"Status: {self.status}")

        # Home team
        lines.append(f"\n--- {self.home_team.name} (HOME) ---")
        if self.home_team.record:
            lines.append(f"Record: {self.home_team.record}")
        if self.home_team.standing:
            lines.append(f"Standing: {self.home_team.standing}")
        if self.home_team.recent_form:
            lines.append(f"Recent Form: {self.home_team.recent_form}")
        if self.home_team.home_away_record:
            lines.append(f"Home Record: {self.home_team.home_away_record}")
        if self.home_team.injuries:
            lines.append(f"Injuries: {', '.join(self.home_team.injuries[:5])}")

        # Away team
        lines.append(f"\n--- {self.away_team.name} (AWAY) ---")
        if self.away_team.record:
            lines.append(f"Record: {self.away_team.record}")
        if self.away_team.standing:
            lines.append(f"Standing: {self.away_team.standing}")
        if self.away_team.recent_form:
            lines.append(f"Recent Form: {self.away_team.recent_form}")
        if self.away_team.home_away_record:
            lines.append(f"Away Record: {self.away_team.home_away_record}")
        if self.away_team.injuries:
            lines.append(f"Injuries: {', '.join(self.away_team.injuries[:5])}")

        # Odds
        if self.spread or self.over_under:
            lines.append(f"\n--- Consensus Lines ---")
            if self.spread:
                lines.append(f"Spread: {self.spread}")
            if self.over_under:
                lines.append(f"Over/Under: {self.over_under}")
        if self.odds_summary:
            lines.append(f"Odds Context: {self.odds_summary}")

        lines.append(
            "\nINSTRUCTION: Use this structured data as your foundation. "
            "It is sourced from ESPN's live API. Web search for additional "
            "context (coaching changes, recent trades, momentum) but anchor "
            "your probability estimate on these hard numbers."
        )
        return "\n".join(lines)


class SportsEngine:
    """
    Fetches structured sports data from ESPN's API.
    
    The ESPN site API is unofficial but widely used and stable.
    No API key needed. Rate-limit friendly at normal bot speeds.
    """

    def __init__(self):
        self.client = httpx.Client(
            timeout=15.0,
            headers={"User-Agent": "KalshiSportsBot/1.0"},
        )

    def analyze_market(self, market) -> MatchupContext | None:
        """
        Analyze a Kalshi sports market by:
        1. Detecting the sport and teams from the market title/ticker
        2. Fetching live ESPN data for the matchup
        3. Returning structured context for Claude
        """
        question = getattr(market, "question", "")
        ticker = getattr(market, "id", "")
        category = getattr(market, "category", "").lower()

        # Detect sport
        league = self._detect_league(question, ticker, category)
        if not league:
            return None

        espn_path = SPORT_LEAGUE_MAP.get(league)
        if not espn_path:
            return None

        # Try to find the specific game
        teams = self._extract_teams(question, ticker)
        if not teams:
            return None

        matchup = self._fetch_matchup(espn_path, teams, question)
        return matchup

    def _detect_league(self, question: str, ticker: str, category: str) -> str | None:
        """Detect which league this market is about."""
        q_lower = question.lower()
        t_upper = ticker.upper()

        # Check ticker prefixes (Kalshi uses these)
        for prefix in ["NBA", "NFL", "MLB", "NHL", "WNBA", "NCAAB", "NCAAF", "MLS", "UFC"]:
            if prefix in t_upper:
                return prefix.lower()

        # Check question text
        league_keywords = {
            "nba": ["nba", "basketball"],
            "nfl": ["nfl", "football", "touchdown"],
            "mlb": ["mlb", "baseball", "home run"],
            "nhl": ["nhl", "hockey", "puck"],
            "mls": ["mls", "soccer"],
            "ufc": ["ufc", "mma", "fight"],
        }
        for league, keywords in league_keywords.items():
            if any(kw in q_lower for kw in keywords):
                return league

        # Check category
        if "sport" in category:
            # Try to infer from team names
            for abbr, aliases in TEAM_ALIASES.items():
                if any(alias in q_lower for alias in aliases):
                    # Guess league from abbreviation suffix
                    if abbr.endswith("_MLB"):
                        return "mlb"
                    elif abbr.endswith("_NHL"):
                        return "nhl"
                    elif abbr.endswith("_NFL"):
                        return "nfl"
                    return "nba"  # Default to NBA if ambiguous

        return None

    def _extract_teams(self, question: str, ticker: str) -> list[str]:
        """Extract team names from question or ticker."""
        q_lower = question.lower()
        found = []

        for abbr, aliases in TEAM_ALIASES.items():
            for alias in aliases:
                if alias in q_lower:
                    found.append(alias)
                    break

        # Also try to extract from ticker (e.g., "NBA-CELWIN" → celtics)
        ticker_parts = ticker.upper().split("-")
        for part in ticker_parts:
            # Remove common suffixes
            clean = re.sub(r"(WIN|LOSE|OVER|UNDER|PTS|REB|AST|SPREAD)\d*", "", part)
            if clean and len(clean) >= 2:
                found.append(clean.lower())

        return list(set(found))[:2]

    def _fetch_matchup(
        self, espn_path: str, team_hints: list[str], question: str
    ) -> MatchupContext | None:
        """Fetch matchup data from ESPN scoreboard."""
        try:
            # Get today's scoreboard
            url = f"{ESPN_BASE}/{espn_path}/scoreboard"
            resp = self.client.get(url)
            if resp.status_code != 200:
                return None

            data = resp.json()
            events = data.get("events", [])

            # Find the matching event
            best_event = None
            best_score = 0

            for event in events:
                event_name = event.get("name", "").lower()
                short_name = event.get("shortName", "").lower()
                # Score based on how many team hints match
                score = 0
                for hint in team_hints:
                    if hint in event_name or hint in short_name:
                        score += 1
                    # Also check competitor names
                    for comp in event.get("competitions", [{}])[0].get("competitors", []):
                        team_name = comp.get("team", {}).get("displayName", "").lower()
                        team_abbr = comp.get("team", {}).get("abbreviation", "").lower()
                        if hint in team_name or hint == team_abbr:
                            score += 1
                if score > best_score:
                    best_score = score
                    best_event = event

            if not best_event or best_score == 0:
                # No matching event — try returning league-level context
                return self._build_league_context(espn_path, question)

            return self._parse_event(best_event, espn_path)

        except Exception as e:
            print(f"⚠️  ESPN fetch failed: {e}")
            return None

    def _parse_event(self, event: dict, espn_path: str) -> MatchupContext | None:
        """Parse an ESPN event into a MatchupContext."""
        try:
            competition = event.get("competitions", [{}])[0]
            competitors = competition.get("competitors", [])

            if len(competitors) < 2:
                return None

            # ESPN lists home team first usually, but check homeAway field
            home_comp = None
            away_comp = None
            for comp in competitors:
                if comp.get("homeAway") == "home":
                    home_comp = comp
                elif comp.get("homeAway") == "away":
                    away_comp = comp

            if not home_comp or not away_comp:
                home_comp, away_comp = competitors[0], competitors[1]

            home_team = self._parse_competitor(home_comp)
            away_team = self._parse_competitor(away_comp)

            # Game info
            status_obj = event.get("status", {})
            status_type = status_obj.get("type", {}).get("name", "")
            status_detail = status_obj.get("type", {}).get("detail", "")

            if status_type == "STATUS_SCHEDULED":
                status = "pre"
            elif status_type == "STATUS_IN_PROGRESS":
                status = "in"
            elif status_type == "STATUS_FINAL":
                status = "post"
            else:
                status = status_detail

            venue = competition.get("venue", {}).get("fullName", "")
            game_date = event.get("date", "")

            # Odds if available
            spread = ""
            over_under = ""
            odds = competition.get("odds", [])
            if odds:
                primary = odds[0]
                spread = primary.get("details", "")
                over_under = f"{primary.get('overUnder', '')}"

            sport = espn_path.split("/")[0]
            league = espn_path.split("/")[1] if "/" in espn_path else espn_path

            return MatchupContext(
                sport=sport,
                league=league,
                home_team=home_team,
                away_team=away_team,
                game_time=game_date,
                venue=venue,
                status=status,
                spread=spread,
                over_under=over_under,
                headline=event.get("name", ""),
            )

        except Exception as e:
            print(f"⚠️  Event parse failed: {e}")
            return None

    def _parse_competitor(self, comp: dict) -> TeamStats:
        """Parse an ESPN competitor into TeamStats."""
        team = comp.get("team", {})
        records = comp.get("records", [])

        record = ""
        home_away_record = ""
        for rec in records:
            if rec.get("name") == "overall" or rec.get("type") == "total":
                record = rec.get("summary", "")
            elif rec.get("name") in ("Home", "Away") or rec.get("type") in ("home", "road"):
                home_away_record = f"{rec.get('name', '')}: {rec.get('summary', '')}"

        # Recent form from last events if available
        form = ""
        last_events = comp.get("form", "")  # Not always present

        score = None
        if comp.get("score"):
            try:
                score = int(comp["score"])
            except (ValueError, TypeError):
                pass

        return TeamStats(
            name=team.get("displayName", team.get("name", "Unknown")),
            abbreviation=team.get("abbreviation", ""),
            record=record,
            home_away_record=home_away_record,
            recent_form=form,
            score=score,
        )

    def _build_league_context(self, espn_path: str, question: str) -> MatchupContext | None:
        """If we can't find a specific game, return general league context."""
        # Could fetch standings, but for now return None to signal no data
        return None

    def get_injuries(self, espn_path: str, team_abbr: str) -> list[str]:
        """Fetch injury report for a team."""
        try:
            url = f"{ESPN_BASE}/{espn_path}/teams/{team_abbr}/injuries"
            resp = self.client.get(url)
            if resp.status_code != 200:
                return []

            data = resp.json()
            injuries = []
            for item in data.get("injuries", []):
                for athlete in item.get("injuries", []):
                    name = athlete.get("athlete", {}).get("displayName", "")
                    status = athlete.get("status", "")
                    desc = athlete.get("details", {}).get("detail", "")
                    if name:
                        injuries.append(f"{name} ({status}: {desc})")

            return injuries[:8]  # Cap at 8 most relevant

        except Exception:
            return []
