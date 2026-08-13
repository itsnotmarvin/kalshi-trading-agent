"""Ensemble-based probability calculator for supported Kalshi weather markets.

The engine requests explicit GFS ensemble and HRRR model data through
Open-Meteo, derives an empirical distribution from available members, and logs
each analyzed forecast for later scoring. Model output is an input to the risk
process, not evidence by itself that the strategy has an edge.
"""
import json
import re
import httpx
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from core.forecaster import ForecasterInsight
from core.settlement_stations import station_for_city
from config.paths import RUNTIME_DATA_DIR
from config.settings import settings


# ── City Coordinates ────────────────────────────────────────
# Major US cities that frequently appear on Kalshi weather markets.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york":     (40.71, -74.01),
    "nyc":          (40.71, -74.01),
    "los angeles":  (34.05, -118.24),
    "la":           (34.05, -118.24),
    "chicago":      (41.88, -87.63),
    "miami":        (25.76, -80.19),
    "houston":      (29.76, -95.37),
    "phoenix":      (33.45, -112.07),
    "philadelphia": (39.95, -75.17),
    "san antonio":  (29.42, -98.49),
    "san diego":    (32.72, -117.16),
    "dallas":       (32.78, -96.80),
    "austin":       (30.27, -97.74),
    "denver":       (39.74, -104.99),
    "seattle":      (47.61, -122.33),
    "boston":        (42.36, -71.06),
    "atlanta":      (33.75, -84.39),
    "las vegas":    (36.17, -115.14),
    "san francisco":(37.77, -122.42),
    "sf":           (37.77, -122.42),
    "dc":           (38.91, -77.04),
    "washington":   (38.91, -77.04),
    "detroit":      (42.33, -83.05),
    "minneapolis":  (44.98, -93.27),
    "tampa":        (27.95, -82.46),
    "nashville":    (36.16, -86.78),
    "portland":     (45.52, -122.68),
    "charlotte":    (35.23, -80.84),
    "indianapolis": (39.77, -86.16),
    "columbus":     (39.96, -82.99),
    "memphis":      (35.15, -90.05),
    "baltimore":    (39.29, -76.61),
    "milwaukee":    (43.04, -87.91),
    "kansas city":  (39.10, -94.58),
    "st louis":     (38.63, -90.20),
    "pittsburgh":   (40.44, -79.99),
    "new orleans":  (29.95, -90.07),
    "salt lake city":(40.76, -111.89),
    "sacramento":   (38.58, -121.49),
    "oklahoma city":(35.47, -97.52),
    "raleigh":      (35.78, -78.64),
    "tucson":       (32.22, -110.93),
    "bakersfield":  (35.37, -119.02),
    "louisville":   (38.25, -85.76),
    "omaha":        (41.26, -95.94),
    "boise":        (43.62, -116.21),
    "el paso":      (31.76, -106.49),
    "anchorage":    (61.22, -149.90),
    "honolulu":     (21.31, -157.86),
}

# Map weather variable keywords → Open-Meteo API parameter
# IMPORTANT: These must be WEATHER-SPECIFIC terms, not generic words like
# "high" or "low" that appear in sports lines ("high score", "low seed").
VARIABLE_MAP = {
    "temperature":      "temperature_2m",
    "temp":             "temperature_2m",
    "high temperature": "temperature_2m",
    "low temperature":  "temperature_2m",
    "high temp":        "temperature_2m",
    "low temp":         "temperature_2m",
    "precipitation":    "precipitation",
    "rain":             "precipitation",
    "rainfall":         "precipitation",
    "snow":             "snowfall",
    "snowfall":         "snowfall",
    "wind":             "wind_speed_10m",
    "wind speed":       "wind_speed_10m",
    "humidity":         "relative_humidity_2m",
}

# Explicit weather keywords that MUST appear in the question for it to be a weather market.
# This prevents sports markets ("Houston wins by over 13.5 Points") from being matched.
WEATHER_REQUIRED_KEYWORDS = [
    "temperature", "temp", "°f", "°c", "°", "fahrenheit", "celsius",
    "weather", "forecast", "rain", "rainfall", "precipitation",
    "snow", "snowfall", "wind", "humidity", "inches of",
    "high temp", "low temp", "degrees",
    # Kalshi markdown formatting
    "**high temp", "**low temp", "**snow",
]

# Ticker prefixes that are NOT weather markets
NON_WEATHER_TICKER_PREFIXES = [
    "KXMVE",      # Sports multi-game / cross-category parlays
    "KXSPORTS",   # Sports
    "KXNBA",      # NBA
    "KXNFL",      # NFL
    "KXMLB",      # MLB
    "KXNHL",      # NHL
    "KXNCAA",     # NCAA
    "KXSOCCER",   # Soccer
    "KXCRYPTO",   # Crypto
    "KXBTC",      # Bitcoin
    "KXETH",      # Ethereum
    "KXFED",      # Fed rate
    "KXELECTION", # Elections
    "KXPOLITICS", # Politics
]


ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
DETERMINISTIC_API_URL = "https://api.open-meteo.com/v1/forecast"
NWS_OBSERVATION_URL = "https://api.weather.gov/stations/{station_id}/observations/latest"

# An observation lock must clear the threshold by this margin. The settlement
# number is the whole-°F maximum from a continuous sensor, while METAR-derived
# observations arrive in °C and round through conversion; a sub-degree
# clearance can still settle on the wrong side of a strict ">T" rule.
LOCK_MARGIN_F = 1.0
GFS_ENSEMBLE_MODEL = "gfs_seamless"
HRRR_CONUS_MODEL = "ncep_hrrr_conus"
NUM_MEMBERS = 31  # 1 control + 30 perturbed


@dataclass
class WeatherProbability:
    """Result of ensemble probability calculation."""
    probability: float          # 0.0 – 1.0
    confidence: float           # 0.0 – 1.0 (agreement among members)
    ensemble_mean: float        # Average across all members
    ensemble_spread: float      # Std deviation across members
    threshold: float            # The threshold we compared against
    above: bool                 # True = prob of going ABOVE threshold
    city: str
    variable: str
    forecast_hour: str          # Which hour we evaluated
    member_values: list[float]  # All 31 member values for debugging


@dataclass
class ParsedWeatherMarket:
    """Parsed metadata from a Kalshi weather market title/ticker."""
    city: str
    lat: float
    lon: float
    variable: str               # Open-Meteo parameter name
    threshold: float            # The numeric threshold in the question
    above: bool                 # True = "above X", False = "below X"
    target_date: datetime | None
    raw_question: str


class WeatherEngine:
    """
    Fetches GFS ensemble data from Open-Meteo and computes probability
    distributions for Kalshi weather markets.
    """

    def __init__(self, min_price: float = 0.10, min_confidence: float = 0.65, lessons: list[str] | None = None):
        self.min_price = min_price
        self.min_confidence = min_confidence
        self.client = httpx.Client(timeout=15.0)
        self.lessons = lessons or []
        self.forecaster = ForecasterInsight()

        # Every analyzed market gets logged here, traded or not. Scoring only
        # traded markets selection-biases the calibration curve; the engine
        # produces ~20× more forecasts than trades, and they're all evidence.
        self.forecast_log_path = RUNTIME_DATA_DIR / "weather_forecast_log.jsonl"
        
        # Kept for dashboard compatibility. Probability is no longer moved by
        # an arbitrary lesson-derived percentage; calibration must come from
        # resolved forecasts.
        self.humility_buffer = 0.0

    # ── API ──────────────────────────────────────────────────

    def get_ensemble_forecast(
        self,
        lat: float,
        lon: float,
        variable: str = "temperature_2m",
        forecast_days: int = 3,
    ) -> dict:
        """
        Fetch 31-member GFS ensemble forecast from Open-Meteo.
        
        Returns the raw JSON with hourly data per member.
        """
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": variable,
            "models": GFS_ENSEMBLE_MODEL,
            "forecast_days": forecast_days,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        }
        resp = self.client.get(ENSEMBLE_API_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
        payload["_provenance"] = {
            "source_type": "forecast_model",
            "provider": "Open-Meteo",
            "model": GFS_ENSEMBLE_MODEL,
            "member_count_expected": NUM_MEMBERS,
            "url": str(resp.request.url),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        return payload

    def get_deterministic_forecast(
        self,
        lat: float,
        lon: float,
        variable: str = "temperature_2m",
        forecast_days: int = 3,
    ) -> dict:
        """
        Fetch an explicitly selected HRRR deterministic forecast and observations.
        """
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": variable,
            "current": variable,
            "models": HRRR_CONUS_MODEL,
            "forecast_days": forecast_days,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        }
        resp = self.client.get(DETERMINISTIC_API_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
        payload["_provenance"] = {
            "source_type": "forecast_model",
            "provider": "Open-Meteo",
            "model": HRRR_CONUS_MODEL,
            "url": str(resp.request.url),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        return payload

    def get_station_observation(self, station) -> dict | None:
        """Latest observation from the settlement station itself.

        Settlement-quality data or nothing: on any error or missing
        temperature this returns None, and the caller must fall back to model
        probabilities rather than substitute a grid estimate.
        """
        url = NWS_OBSERVATION_URL.format(station_id=station.station_id)
        try:
            resp = self.client.get(
                url,
                headers={
                    "User-Agent": "kalshi-trading-agent (github.com/itsnotmarvin/kalshi-trading-agent)",
                    "Accept": "application/geo+json",
                },
            )
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            temp_c = (props.get("temperature") or {}).get("value")
            observed_at = props.get("timestamp")
            if temp_c is None or not observed_at:
                return None
            return {
                "temperature_f": temp_c * 9.0 / 5.0 + 32.0,
                "observed_at": observed_at,
                "station_id": station.station_id,
                "_provenance": {
                    "source_type": "station_observation",
                    "provider": "NWS",
                    "station_id": station.station_id,
                    "station_name": station.name,
                    "url": url,
                    "observed_at": observed_at,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        except Exception:
            return None

    # ── Probability ──────────────────────────────────────────

    def compute_probability(
        self,
        ensemble_data: dict,
        variable: str,
        target_hour_index: int,
        threshold: float,
        above: bool = True,
    ) -> WeatherProbability:
        """
        Given ensemble data and a threshold, compute what fraction of
        the 31 members predict values above (or below) the threshold.

        Args:
            ensemble_data: Raw JSON from Open-Meteo ensemble API.
            variable: Base variable name (e.g., "temperature_2m").
            target_hour_index: Index into the hourly arrays for the target time.
            threshold: Numeric threshold to compare against.
            above: If True, compute P(value >= threshold). If False, P(value < threshold).

        Returns:
            WeatherProbability with probability, confidence, and diagnostics.
        """
        hourly = ensemble_data.get("hourly", {})
        times = hourly.get("time", [])

        if target_hour_index >= len(times):
            target_hour_index = len(times) - 1

        # Collect all 31 GFS member values for this hour
        member_values = []

        # Control member (no suffix)
        if variable in hourly and target_hour_index < len(hourly[variable]):
            val = hourly[variable][target_hour_index]
            if val is not None: member_values.append(val)
            
        # Perturbed members (member01 .. member30)
        for i in range(1, 31):
            key = f"{variable}_member{i:02d}"
            if key in hourly and target_hour_index < len(hourly[key]):
                val = hourly[key][target_hour_index]
                if val is not None: member_values.append(val)

        if not member_values:
            return WeatherProbability(
                probability=0.5, confidence=0.0, ensemble_mean=0.0,
                ensemble_spread=0.0, threshold=threshold, above=above,
                city="", variable=variable,
                forecast_hour=times[target_hour_index] if times else "",
                member_values=[],
            )

        return self._probability_from_member_values(
            member_values,
            threshold=threshold,
            above=above,
            variable=variable,
            forecast_hour=times[target_hour_index] if target_hour_index < len(times) else "",
        )

    def compute_daily_extreme_probability(
        self,
        ensemble_data: dict,
        variable: str,
        hour_indexes: list[int],
        threshold: float,
        above: bool,
        *,
        use_maximum: bool,
    ) -> WeatherProbability:
        """Compute P(daily max/min crossing) member-by-member."""
        hourly = ensemble_data.get("hourly", {})
        times = hourly.get("time", [])
        member_values: list[float] = []
        keys = [variable] + [f"{variable}_member{i:02d}" for i in range(1, 31)]
        for key in keys:
            series = hourly.get(key)
            if not isinstance(series, list):
                continue
            values = [series[i] for i in hour_indexes if i < len(series) and series[i] is not None]
            if values:
                member_values.append(max(values) if use_maximum else min(values))

        if hour_indexes and times:
            first = times[min(hour_indexes)]
            last = times[min(max(hour_indexes), len(times) - 1)]
            forecast_hour = f"{first}..{last}"
        else:
            forecast_hour = ""
        return self._probability_from_member_values(
            member_values,
            threshold=threshold,
            above=above,
            variable=variable,
            forecast_hour=forecast_hour,
        )

    def _probability_from_member_values(
        self,
        member_values: list[float],
        *,
        threshold: float,
        above: bool,
        variable: str,
        forecast_hour: str,
    ) -> WeatherProbability:
        if not member_values:
            return WeatherProbability(
                probability=0.5, confidence=0.0, ensemble_mean=0.0,
                ensemble_spread=0.0, threshold=threshold, above=above,
                city="", variable=variable, forecast_hour=forecast_hour,
                member_values=[],
            )

        if above:
            count = sum(1 for v in member_values if v >= threshold)
        else:
            count = sum(1 for v in member_values if v < threshold)

        probability = count / len(member_values)

        # Compute spread as a confidence measure
        mean_val = sum(member_values) / len(member_values)
        variance = sum((v - mean_val) ** 2 for v in member_values) / len(member_values)
        std_dev = variance ** 0.5

        # Confidence = 1.0 when all members agree, drops as spread increases.
        # Normalize: if std_dev < 1°F/0.5mm → high confidence; > 5°F/3mm → low.
        # Using a simple sigmoid-like mapping.
        spread_norm = std_dev / 5.0  # 5°F spread → ~0 confidence
        confidence = max(0.0, min(1.0, 1.0 - spread_norm))

        return WeatherProbability(
            probability=probability,
            confidence=confidence,
            ensemble_mean=mean_val,
            ensemble_spread=std_dev,
            threshold=threshold,
            above=above,
            city="",
            variable=variable,
            forecast_hour=forecast_hour,
            member_values=member_values,
        )

    # ── Market Parsing ───────────────────────────────────────

    def parse_kalshi_weather_market(self, market) -> ParsedWeatherMarket | None:
        """
        Parse a Kalshi weather market to extract city, variable, threshold,
        and direction from the market title/ticker.

        Examples of Kalshi weather market titles:
          "Will the high temperature in NYC be above 75°F on March 22?"
          "Will it rain more than 0.5 inches in Chicago on March 23?"
          "Will the low temperature in Miami drop below 60°F?"
          Tickers like: KXHIGHNY-26MAR22-T75, KXLOWCHI-26MAR22-T32
        """
        question = market.question.lower()
        ticker = market.id.upper()

        # ── GUARD: Reject non-weather tickers immediately ──
        for prefix in NON_WEATHER_TICKER_PREFIXES:
            if ticker.startswith(prefix):
                return None

        # ── GUARD: Require at least one explicit weather keyword ──
        # This is the primary defense against sports/politics/crypto markets
        # that happen to mention city names.
        has_weather_keyword = any(kw in question for kw in WEATHER_REQUIRED_KEYWORDS)
        has_weather_ticker = ticker.startswith("KXHIGH") or ticker.startswith("KXLOW") or ticker.startswith("KXRAIN") or ticker.startswith("KXSNOW") or ticker.startswith("KXWIND")
        
        if not has_weather_keyword and not has_weather_ticker:
            return None

        # ── GUARD: Reject sports-like questions ──
        sports_reject = ["wins by", "points scored", "wins by over", "rebounds",
                         "assists", "touchdowns", "goals", "nba", "nfl", "mlb",
                         "nhl", "ncaa", "march madness", "playoff", "parlay",
                         "slam dunk", "three-pointer", "soccer", "premier league"]
        if any(kw in question for kw in sports_reject):
            return None

        # Try to extract city
        city = None
        lat, lon = None, None
        for city_name, (clat, clon) in CITY_COORDS.items():
            if city_name in question:
                city = city_name
                lat, lon = clat, clon
                break

        # Fallback: try to parse city from WEATHER ticker (e.g., KXHIGHNY → NY)
        if not city:
            ticker_city_map = {
                "NY": "new york", "LA": "los angeles", "CHI": "chicago",
                "MIA": "miami", "HOU": "houston", "PHX": "phoenix",
                "PHI": "philadelphia", "PHIL": "philadelphia",
                "DAL": "dallas", "DEN": "denver", "LAX": "los angeles",
                "SEA": "seattle", "BOS": "boston", "ATL": "atlanta",
                "SF": "san francisco", "DC": "washington", "DET": "detroit",
                "MIN": "minneapolis", "TPA": "tampa", "POR": "portland",
                "AUS": "austin", "LV": "las vegas", "NO": "new orleans",
                "SLC": "salt lake city", "SAC": "sacramento", "OKC": "oklahoma city",
                "ANC": "anchorage", "HNL": "honolulu",
            }
            # Only match ticker city codes if this IS a weather ticker. Match
            # the code after the weather prefix exactly; substring matching can
            # misread KXSNOWSLC as "NO" because "NO" appears inside SNOW.
            if has_weather_ticker:
                ticker_match = re.match(r"^KX(?:HIGH|LOW|RAIN|SNOW|WIND)([A-Z]+?)(?:-|$)", ticker)
                ticker_city_code = ticker_match.group(1) if ticker_match else None
                if ticker_city_code in ticker_city_map:
                    city = ticker_city_map[ticker_city_code]
                    lat, lon = CITY_COORDS[city]

        if not city or lat is None:
            return None

        # Kalshi temperature markets settle at one specific NWS station, not
        # city center (docs/settlement-stations.md). Forecast at the station
        # when its identity is verified; DIA vs downtown Denver is ~30 km.
        station = station_for_city(city)
        if station is not None:
            lat, lon = station.lat, station.lon

        # Extract variable
        variable = "temperature_2m"  # default
        for keyword, var_name in VARIABLE_MAP.items():
            if keyword in question:
                variable = var_name
                break

        # Extract threshold number
        # Kalshi formats: ">69°", "<62°", "75°F", "above 80", "more than 0.5 inches"
        threshold = None
        threshold_patterns = [
            r'[><]\s*(\d+\.?\d*)\s*°',               # >69°, <62°, > 75°
            r'(\d+\.?\d*)\s*°?\s*[fF]',               # 75°F, 75 F, 75f
            r'above\s+(\d+\.?\d*)',                    # above 75
            r'below\s+(\d+\.?\d*)',                    # below 32
            r'over\s+(\d+\.?\d*)',                     # over 75
            r'under\s+(\d+\.?\d*)',                    # under 32
            r'more\s+than\s+(\d+\.?\d*)',              # more than 0.5
            r'less\s+than\s+(\d+\.?\d*)',              # less than 0.5
            r'exceed\s+(\d+\.?\d*)',                   # exceed 80
            r'at\s+least\s+(\d+\.?\d*)',               # at least 70
            r'T(\d+\.?\d*)',                           # Ticker: T75
        ]
        for pattern in threshold_patterns:
            match = re.search(pattern, question)
            if not match:
                match = re.search(pattern, ticker)
            if match:
                threshold = float(match.group(1))
                break

        if threshold is None:
            return None

        # Determine direction (above/below)
        above = True
        below_keywords = ["below", "under", "less than", "drop below"]
        for kw in below_keywords:
            if kw in question:
                above = False
                break
        # Check for > and < signs
        if re.search(r'<\s*\d', question):
            above = False
        if re.search(r'>\s*\d', question):
            above = True
        # Ticker hints
        if "LOW" in ticker:
            above = False

        # Try to extract target date. The ticker encodes the settlement
        # (climate) day exactly — KXHIGHNY-26AUG13-T92 → 2026-08-13 — so it
        # outranks end_date, whose UTC calendar date can fall on the wrong
        # local day.
        target_date = None
        ticker_date = re.search(
            r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?:-|$)",
            ticker,
        )
        if ticker_date:
            month_numbers = {
                "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
            }
            try:
                target_date = datetime(
                    2000 + int(ticker_date.group(1)),
                    month_numbers[ticker_date.group(2)],
                    int(ticker_date.group(3)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                target_date = None
        if target_date is not None:
            pass
        elif "tomorrow" in question:
            target_date = datetime.now(timezone.utc) + timedelta(days=1)
        elif "today" in question:
            target_date = datetime.now(timezone.utc)
        else:
            # Common explicit patterns: "on March 22", "Mar 22, 2026".
            # Keep this month-specific so threshold phrases like "above 70"
            # are not accidentally parsed as dates.
            month = (
                r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
                r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|sept|oct(?:ober)?|"
                r"nov(?:ember)?|dec(?:ember)?"
            )
            match = re.search(
                rf"(?:on\s+)?(({month})\s+\d{{1,2}}(?:,?\s*\d{{4}})?)",
                question,
            )
            if match:
                try:
                    from dateutil import parser as date_parser
                    target_date = date_parser.parse(match.group(1), fuzzy=True)
                    if target_date.tzinfo is None:
                        target_date = target_date.replace(tzinfo=timezone.utc)
                except Exception:
                    pass

        # Fallback: use the market's end_date
        if target_date is None and market.end_date:
            target_date = market.end_date

        return ParsedWeatherMarket(
            city=city,
            lat=lat,
            lon=lon,
            variable=variable,
            threshold=threshold,
            above=above,
            target_date=target_date,
            raw_question=market.question,
        )

    # ── Full Analysis ────────────────────────────────────────

    def analyze_market(self, market) -> dict | None:
        """
        Full pipeline: parse market → fetch ensemble → compute probability → return edge.

        Returns a dict with keys:
            probability, confidence, edge, direction, ensemble_mean, ensemble_spread,
            threshold, city, should_trade
        Or None if the market can't be parsed or doesn't qualify.
        """
        parsed = self.parse_kalshi_weather_market(market)
        if not parsed:
            return None

        # Price filter: skip penny contracts
        market_price = market.yes_price
        if parsed.above:
            # YES side represents "above threshold"
            market_price = market.yes_price
        else:
            # YES side represents "below threshold"
            market_price = market.yes_price

        # Fetch ensemble data and deterministic data
        try:
            data = self.get_ensemble_forecast(
                lat=parsed.lat,
                lon=parsed.lon,
                variable=parsed.variable,
                forecast_days=3,
            )
            det_data = self.get_deterministic_forecast(
                lat=parsed.lat,
                lon=parsed.lon,
                variable=parsed.variable,
                forecast_days=3,
            )
        except Exception:
            return None

        ensemble_provenance = data.get("_provenance", {})
        deterministic_provenance = det_data.get("_provenance", {})
        deterministic_model = deterministic_provenance.get("model", HRRR_CONUS_MODEL)
        forecast_provenance = [
            source for source in (ensemble_provenance, deterministic_provenance) if source
        ]

        # 1. Real-Time Observation Override.
        # A "locked outcome" claim may only come from the settlement station's
        # own observations, on the market's own climate day. The grid-model
        # "current" temperature is never consulted here: a 1-3°F grid/station
        # gap, or a hot day before the target date, both manufacture false
        # 99%-certainty signals. Only one-sided locks are valid — an
        # observation proves the daily max is at least obs_temp, never that
        # the temperature will stay below anything.
        station = station_for_city(parsed.city)
        obs = self.get_station_observation(station) if station is not None else None
        if obs is not None and parsed.target_date is not None:
            try:
                observed_local = datetime.fromisoformat(obs["observed_at"]).astimezone(
                    ZoneInfo(station.timezone)
                )
            except Exception:
                observed_local = None
            if observed_local is not None and observed_local.date() == parsed.target_date.date():
                obs_temp = obs["temperature_f"]
                obs_provenance = forecast_provenance + [obs["_provenance"]]
                obs_note = f"{station.station_id} observed {obs_temp:.1f}°F at {obs['observed_at']}"
                if "high" in parsed.raw_question.lower() or "HIGH" in market.id:
                    if parsed.above and obs_temp >= parsed.threshold + LOCK_MARGIN_F:
                        # Daily max already cleared the threshold at the settlement station.
                        return self._build_override_result(parsed, market, 0.99, "BUY_YES", f"{obs_note}, clearing threshold {parsed.threshold}°F", obs_provenance)
                    if not parsed.above and obs_temp >= parsed.threshold + LOCK_MARGIN_F:
                        # Daily max already broke the "below" bound → resolves NO.
                        return self._build_override_result(parsed, market, 0.01, "BUY_NO", f"{obs_note}, breaking 'below {parsed.threshold}°F'", obs_provenance)
                elif "low" in parsed.raw_question.lower() or "LOW" in market.id:
                    if not parsed.above and obs_temp <= parsed.threshold - LOCK_MARGIN_F:
                        # Daily min is at most obs_temp → already under the threshold.
                        return self._build_override_result(parsed, market, 0.99, "BUY_YES", f"{obs_note}, already under threshold {parsed.threshold}°F", obs_provenance)

        # Find the target hour index
        hourly_times = data.get("hourly", {}).get("time", [])
        target_idx = 0

        hours_to_target = 48.0
        if parsed.target_date:
            hours_to_target = (parsed.target_date - datetime.now(timezone.utc)).total_seconds() / 3600.0
            # Find closest hour to the target
            for i, t_str in enumerate(hourly_times):
                try:
                    t_dt = datetime.fromisoformat(t_str)
                    target_dt = parsed.target_date
                    if t_dt.tzinfo is None:
                        target_dt = target_dt.replace(tzinfo=None)
                    elif target_dt.tzinfo is not None:
                        target_dt = target_dt.astimezone(t_dt.tzinfo)
                    if t_dt >= target_dt:
                        target_idx = i
                        break
                except Exception:
                    continue

        day_indexes: list[int] = []
        if parsed.target_date:
            target_day = parsed.target_date.date()
            for i, t_str in enumerate(hourly_times):
                try:
                    if datetime.fromisoformat(t_str).date() == target_day:
                        day_indexes.append(i)
                except ValueError:
                    continue
        if not day_indexes and hourly_times:
            day_start = (target_idx // 24) * 24
            day_indexes = list(range(day_start, min(day_start + 24, len(hourly_times))))

        is_daily_high = "high" in parsed.raw_question.lower() or "HIGH" in market.id
        is_daily_low = "low" in parsed.raw_question.lower() or "LOW" in market.id
        if (is_daily_high or is_daily_low) and day_indexes:
            prob_result = self.compute_daily_extreme_probability(
                data,
                parsed.variable,
                day_indexes,
                parsed.threshold,
                parsed.above,
                use_maximum=is_daily_high,
            )
        else:
            prob_result = self.compute_probability(
                data, parsed.variable, target_idx, parsed.threshold, parsed.above
            )
        prob_result.city = parsed.city

        # 2. HRRR Deterministic Check (If < 48h to expiry)
        hrrr_temp = None
        if hours_to_target <= 48.0:
            hrrr_array = det_data.get("hourly", {}).get(parsed.variable, [])
            daily_values = [hrrr_array[i] for i in day_indexes if i < len(hrrr_array) and hrrr_array[i] is not None]
            if is_daily_high and daily_values:
                hrrr_temp = max(daily_values)
            elif is_daily_low and daily_values:
                hrrr_temp = min(daily_values)
            elif target_idx < len(hrrr_array):
                hrrr_temp = hrrr_array[target_idx]

        # Base probability from the GFS ensemble crossing fraction.
        raw_prob = prob_result.probability
        base_prob = raw_prob
        
        # 3. HRRR Consensus Veto Gating (If < 48h to expiry and HRRR available)
        hrrr_veto = False
        hrrr_msg = ""
        if hrrr_temp is not None and deterministic_model == HRRR_CONUS_MODEL:
            gfs_cross = raw_prob >= 0.5
            hrrr_cross = (hrrr_temp >= parsed.threshold) == parsed.above
            if gfs_cross != hrrr_cross:
                # GFS and HRRR disagree on whether the threshold will be crossed!
                hrrr_veto = True
                hrrr_msg = f"VETOED: GFS Ensemble and HRRR disagree. GFS projects {raw_prob:.0%} chance of crossing {parsed.threshold}°F, but HRRR deterministic run projects {hrrr_temp:.1f}°F."
            else:
                hrrr_msg = f"HRRR projects {hrrr_temp:.1f}°F (Agrees with GFS)."

        # Jeffreys smoothing prevents a finite 31-member ensemble from claiming
        # literal 0% or 100%: posterior mean = (hits + 0.5) / (n + 1).
        member_count = len(prob_result.member_values)
        ensemble_prob = (
            ((base_prob * member_count) + 0.5) / (member_count + 1)
            if member_count > 0
            else 0.5
        )
            
        # Get Human Insight
        insight = self.forecaster.get_market_insight(
            market.id, parsed.variable, parsed.threshold, parsed.above
        )
        
        forecaster_reasoning = insight["reasoning"] if insight else None
        # Forecast discussion is qualitative context only until its numeric
        # effect is calibrated on resolved markets.
        final_prob = max(0.01, min(0.99, ensemble_prob))

        yes_edge = final_prob - market.yes_price
        no_edge = (1.0 - final_prob) - market.no_price
        if yes_edge >= no_edge and yes_edge > 0:
            direction = "BUY_YES"
            edge = yes_edge
        elif no_edge > 0:
            direction = "BUY_NO"
            edge = no_edge
        else:
            direction = "HOLD"
            edge = max(yes_edge, no_edge)

        executable_price = market.yes_price if direction == "BUY_YES" else market.no_price if direction == "BUY_NO" else None
        should_trade = (
            direction in {"BUY_YES", "BUY_NO"}
            and edge >= settings.weather_min_edge_threshold
            and not hrrr_veto
            and prob_result.confidence >= self.min_confidence
            and executable_price is not None
            and self.min_price <= executable_price < 0.99
        )

        reasoning = forecaster_reasoning or "No insight."
        if hrrr_msg:
            reasoning = f"({hrrr_msg}). " + reasoning

        result = {
            "probability": final_prob,
            "raw_probability": raw_prob,
            "ensemble_probability": ensemble_prob,
            "confidence": prob_result.confidence,
            "edge": edge,
            "direction": direction,
            "ensemble_mean": prob_result.ensemble_mean,
            "ensemble_spread": prob_result.ensemble_spread,
            "threshold": parsed.threshold,
            "above": parsed.above,
            "city": parsed.city,
            "variable": parsed.variable,
            "forecast_hour": prob_result.forecast_hour,
            "should_trade": should_trade,
            "market_price": market_price,
            "executable_price": executable_price,
            "member_count": member_count,
            "hrrr_veto": hrrr_veto,
            "ensemble_model": ensemble_provenance.get("model", GFS_ENSEMBLE_MODEL),
            "deterministic_model": deterministic_model,
            "model_provenance": [
                source
                for source in (
                    ensemble_provenance,
                    deterministic_provenance,
                    insight.get("source") if insight else None,
                )
                if source
            ],
            "hours_to_target": round(hours_to_target, 1),
            "probability_method": "member-wise daily extreme with Jeffreys smoothing" if (is_daily_high or is_daily_low) else "ensemble fraction with Jeffreys smoothing",
            "forecaster_probability_adjustment": 0.0,
            "forecaster_insight": reasoning,
        }
        self._log_forecast(market, parsed, result)
        return result

    def _log_forecast(self, market, parsed: ParsedWeatherMarket, result: dict):
        """
        Append this forecast to the forecast log, whether or not it trades.

        One JSONL line per analysis; the scorer joins these against Kalshi
        settlement results to measure calibration and skill versus the market
        without selection bias.
        """
        try:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": market.id,
                "question": getattr(market, "question", ""),
                "city": parsed.city,
                "variable": parsed.variable,
                "threshold": parsed.threshold,
                "above": parsed.above,
                "target_date": parsed.target_date.isoformat() if parsed.target_date else None,
                "probability": result.get("probability"),
                "raw_probability": result.get("raw_probability"),
                "confidence": result.get("confidence"),
                "market_yes_price": market.yes_price,
                "market_no_price": market.no_price,
                "direction": result.get("direction"),
                "edge": result.get("edge"),
                "should_trade": result.get("should_trade"),
                "hrrr_veto": result.get("hrrr_veto", False),
                "ensemble_model": result.get("ensemble_model"),
                "deterministic_model": result.get("deterministic_model"),
                "model_provenance": result.get("model_provenance", []),
                "hours_to_target": result.get("hours_to_target"),
                "member_count": result.get("member_count"),
                "probability_method": result.get("probability_method"),
            }
            self.forecast_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.forecast_log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            # Logging must never break the analysis path.
            pass

    def _build_override_result(
        self,
        parsed: ParsedWeatherMarket,
        market,
        final_prob: float,
        direction: str,
        logic: str,
        model_provenance: list[dict] | None = None,
    ):
        market_price = market.yes_price
        edge = final_prob - market.yes_price if direction == "BUY_YES" else (1 - final_prob) - market.no_price
        executable_price = market.yes_price if direction == "BUY_YES" else market.no_price
        result = {
            "probability": final_prob,
            "raw_probability": final_prob,
            "ensemble_probability": final_prob,
            "confidence": 1.0,
            "edge": edge,
            "direction": direction,
            "ensemble_mean": 0.0,
            "ensemble_spread": 0.0,
            "threshold": parsed.threshold,
            "above": parsed.above,
            "city": parsed.city,
            "variable": parsed.variable,
            "forecast_hour": "Override",
            "should_trade": (
                edge >= settings.weather_min_edge_threshold
                and self.min_price <= executable_price < 0.99
            ),
            "market_price": market_price,
            "executable_price": executable_price,
            "member_count": 0,
            "hrrr_veto": False,
            "ensemble_model": next(
                (source.get("model") for source in (model_provenance or []) if source.get("model") == GFS_ENSEMBLE_MODEL),
                GFS_ENSEMBLE_MODEL,
            ),
            "deterministic_model": next(
                (source.get("model") for source in (model_provenance or []) if source.get("model") == HRRR_CONUS_MODEL),
                HRRR_CONUS_MODEL,
            ),
            "model_provenance": model_provenance or [],
            "hours_to_target": 0.0,
            "probability_method": "observed threshold override",
            "forecaster_probability_adjustment": 0.0,
            "forecaster_insight": logic,
        }
        self._log_forecast(market, parsed, result)
        return result
