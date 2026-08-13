#!/usr/bin/env python3
"""
Forecast archiver — credential-free, point-in-time snapshots for the weather
route's forward test.

Each run captures, for every settlement station in
core/settlement_stations.py:

  - the raw GFS ensemble hourly members at the station coordinates,
  - the HRRR deterministic forecast and its "current" grid value,
  - the latest NWS observation from the settlement station itself,
  - the full top-of-book state of every open KXHIGH market on that station,
  - an ensemble probability per market, computed from the archived members
    (method-labeled so later model changes can be re-scored from raw inputs).

Snapshots are meant to be committed to the `forecast-archive` branch by the
GitHub Actions cron in .github/workflows/forecast-archive.yml. The lineage
proof is GitHub's server-side run history, not git timestamps (commit dates
are author-settable); every snapshot therefore embeds its GITHUB_RUN_ID and
run URL so any record can be checked against logs the author cannot edit.

Usage (from the repo root):
    python3 scripts/archive_forecasts.py --out archive/
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.settlement_stations import SETTLEMENT_STATIONS, SettlementStation  # noqa: E402

SCHEMA = "forecast-archive/v1"
PROBABILITY_METHOD = "ensemble_fraction_jeffreys_v1"
ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
DETERMINISTIC_API_URL = "https://api.open-meteo.com/v1/forecast"
NWS_OBSERVATION_URL = "https://api.weather.gov/stations/{station_id}/observations/latest"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
USER_AGENT = "kalshi-trading-agent forecast-archiver (github.com/itsnotmarvin/kalshi-trading-agent)"

MONTH_NUMBERS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Market fields worth archiving: book, flow, strikes, and timing. Rules text
# is static per series and lives in docs/settlement-stations.md.
MARKET_FIELDS = (
    "ticker", "event_ticker", "status", "strike_type", "floor_strike",
    "cap_strike", "subtitle", "close_time", "expected_expiration_time",
    "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars",
    "yes_bid_size_fp", "yes_ask_size_fp", "last_price_dollars",
    "previous_price_dollars", "volume_fp", "volume_24h_fp",
    "open_interest_fp", "liquidity_dollars",
)


def unique_stations() -> dict[str, SettlementStation]:
    """Settlement stations keyed by station id (the table aliases cities)."""
    return {st.station_id: st for st in SETTLEMENT_STATIONS.values()}


def ticker_target_date(ticker: str) -> date | None:
    """KXHIGHNY-26AUG13-T92 → date(2026, 8, 13)."""
    match = re.search(
        r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?:-|$)",
        ticker.upper(),
    )
    if not match:
        return None
    try:
        return date(
            2000 + int(match.group(1)),
            MONTH_NUMBERS[match.group(2)],
            int(match.group(3)),
        )
    except ValueError:
        return None


def member_daily_maxes(ensemble_hourly: dict, target_day: date) -> list[float]:
    """Per-member maxima over the target local calendar day.

    Open-Meteo returns local ISO times when called with timezone=auto, so the
    calendar-day prefix IS the station's climate day.
    """
    times = ensemble_hourly.get("time", [])
    day_prefix = target_day.isoformat()
    indexes = [i for i, t in enumerate(times) if str(t).startswith(day_prefix)]
    if not indexes:
        return []
    maxes: list[float] = []
    for key, values in ensemble_hourly.items():
        if key == "time" or not isinstance(values, list):
            continue
        day_values = [values[i] for i in indexes if i < len(values) and values[i] is not None]
        if day_values:
            maxes.append(max(day_values))
    return maxes


def probability_yes(
    maxes: list[float],
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
) -> float | None:
    """Jeffreys-smoothed fraction of members whose daily max resolves YES.

    Mirrors the engine's daily-extreme math; the method label lets a later
    fitted model re-score these same archived members.
    """
    if not maxes:
        return None
    if strike_type == "greater" and floor_strike is not None:
        hits = sum(1 for m in maxes if m > floor_strike)
    elif strike_type == "less" and cap_strike is not None:
        hits = sum(1 for m in maxes if m < cap_strike)
    elif strike_type == "between" and floor_strike is not None and cap_strike is not None:
        hits = sum(1 for m in maxes if floor_strike <= m <= cap_strike)
    else:
        return None
    return (hits + 0.5) / (len(maxes) + 1.0)


def fetch_station_payloads(client: httpx.Client, station: SettlementStation) -> dict:
    ensemble = client.get(
        ENSEMBLE_API_URL,
        params={
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "models": "gfs_seamless",
            "forecast_days": 3,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        },
    )
    ensemble.raise_for_status()
    hrrr = client.get(
        DETERMINISTIC_API_URL,
        params={
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "current": "temperature_2m",
            "models": "ncep_hrrr_conus",
            "forecast_days": 3,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        },
    )
    hrrr.raise_for_status()

    observation = None
    try:
        obs_resp = client.get(
            NWS_OBSERVATION_URL.format(station_id=station.station_id),
            headers={"Accept": "application/geo+json"},
        )
        obs_resp.raise_for_status()
        props = obs_resp.json().get("properties", {})
        temp_c = (props.get("temperature") or {}).get("value")
        if temp_c is not None and props.get("timestamp"):
            observation = {
                "temperature_f": temp_c * 9.0 / 5.0 + 32.0,
                "temperature_c": temp_c,
                "observed_at": props["timestamp"],
            }
    except httpx.HTTPError:
        observation = None  # a station outage must not sink the snapshot

    return {
        "station": {
            "station_id": station.station_id,
            "series_ticker": station.series_ticker,
            "name": station.name,
            "lat": station.lat,
            "lon": station.lon,
            "timezone": station.timezone,
        },
        "ensemble": ensemble.json(),
        "hrrr": hrrr.json(),
        "observation": observation,
    }


def fetch_series_markets(client: httpx.Client, series_ticker: str) -> list[dict]:
    resp = client.get(
        KALSHI_MARKETS_URL,
        params={"series_ticker": series_ticker, "status": "open", "limit": 100},
    )
    resp.raise_for_status()
    markets = resp.json().get("markets", [])
    return [{k: m.get(k) for k in MARKET_FIELDS} | {"series_ticker": series_ticker} for m in markets]


def run_metadata() -> dict:
    run_id = os.getenv("GITHUB_RUN_ID")
    if not run_id:
        return {"source": "local"}
    return {
        "source": "github-actions",
        "github_run_id": run_id,
        "github_run_url": (
            f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}"
        ),
        "github_sha": os.getenv("GITHUB_SHA"),
    }


def collect_snapshot() -> dict:
    generated_at = datetime.now(timezone.utc)
    stations = unique_stations()
    snapshot: dict = {
        "schema": SCHEMA,
        "generated_at": generated_at.isoformat(),
        "run": run_metadata(),
        "stations": {},
        "markets": [],
        "forecasts": [],
        "errors": [],
    }

    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        for station_id, station in sorted(stations.items()):
            try:
                snapshot["stations"][station_id] = fetch_station_payloads(client, station)
            except httpx.HTTPError as exc:
                snapshot["errors"].append({"station": station_id, "stage": "weather", "error": str(exc)})
                continue
            try:
                markets = fetch_series_markets(client, station.series_ticker)
            except httpx.HTTPError as exc:
                snapshot["errors"].append({"station": station_id, "stage": "markets", "error": str(exc)})
                continue
            snapshot["markets"].extend(markets)

            ensemble_hourly = snapshot["stations"][station_id]["ensemble"].get("hourly", {})
            for market in markets:
                target_day = ticker_target_date(market.get("ticker") or "")
                if target_day is None:
                    continue
                maxes = member_daily_maxes(ensemble_hourly, target_day)
                prob = probability_yes(
                    maxes,
                    market.get("strike_type") or "",
                    market.get("floor_strike"),
                    market.get("cap_strike"),
                )
                if prob is None:
                    continue
                snapshot["forecasts"].append({
                    "ticker": market["ticker"],
                    "station_id": station_id,
                    "target_date": target_day.isoformat(),
                    "probability_yes": round(prob, 6),
                    "member_count": len(maxes),
                    "model": "gfs_seamless",
                    "method": PROBABILITY_METHOD,
                    "generated_at": generated_at.isoformat(),
                })
    return snapshot


def write_snapshot(snapshot: dict, out_root: Path) -> Path:
    generated_at = datetime.fromisoformat(snapshot["generated_at"])
    out_dir = out_root / generated_at.strftime("%Y/%m/%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{generated_at.strftime('%H%M')}Z.json.gz"
    with gzip.open(out_path, "wt", encoding="utf-8") as handle:
        json.dump(snapshot, handle, separators=(",", ":"))
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="archive root directory")
    args = parser.parse_args()

    snapshot = collect_snapshot()
    out_path = write_snapshot(snapshot, Path(args.out))

    station_count = len(snapshot["stations"])
    print(
        f"archived {len(snapshot['forecasts'])} forecasts across "
        f"{station_count} stations, {len(snapshot['markets'])} markets "
        f"→ {out_path}"
    )
    for error in snapshot["errors"]:
        print(f"  warning: {error}", file=sys.stderr)
    # A snapshot with no station data at all is a failed run, not evidence.
    return 0 if station_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
