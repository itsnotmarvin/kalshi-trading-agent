"""Kalshi weather-market settlement stations.

Kalshi temperature markets settle on the NWS Climatological Report (Daily)
for one specific observing station, not on city-center conditions. Forecasting
or observing anywhere else measures the wrong quantity: downtown Denver sits
~30 km from Denver International Airport, and Austin markets settle at
Bergstrom, not the better-known Camp Mabry.

Station identities were read from each market's `rules_primary` text on
2026-08-13; the quoted rules live in docs/settlement-stations.md. Denver's
rules name only "Denver, CO" — its daily climatological report is issued for
Denver International Airport.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettlementStation:
    series_ticker: str   # Kalshi daily-high series settling at this station
    station_id: str      # NWS observation station identifier (ICAO-style)
    name: str            # settlement location as named in the market rules
    lat: float           # station coordinates — forecast and observe HERE
    lon: float
    timezone: str        # IANA zone defining the local climate day


SETTLEMENT_STATIONS: dict[str, SettlementStation] = {
    "new york": SettlementStation(
        "KXHIGHNY", "KNYC", "Central Park, New York",
        40.779, -73.969, "America/New_York",
    ),
    "chicago": SettlementStation(
        "KXHIGHCHI", "KMDW", "Chicago Midway, IL",
        41.786, -87.752, "America/Chicago",
    ),
    "miami": SettlementStation(
        "KXHIGHMIA", "KMIA", "Miami International Airport",
        25.791, -80.316, "America/New_York",
    ),
    "austin": SettlementStation(
        "KXHIGHAUS", "KAUS", "Austin Bergstrom",
        30.195, -97.670, "America/Chicago",
    ),
    "denver": SettlementStation(
        "KXHIGHDEN", "KDEN", "Denver International Airport",
        39.847, -104.656, "America/Denver",
    ),
    "philadelphia": SettlementStation(
        "KXHIGHPHIL", "KPHL", "Philadelphia International Airport",
        39.868, -75.231, "America/New_York",
    ),
    "los angeles": SettlementStation(
        "KXHIGHLAX", "KLAX", "Los Angeles Airport, CA",
        33.938, -118.389, "America/Los_Angeles",
    ),
}

# City aliases used by the market parser.
SETTLEMENT_STATIONS["nyc"] = SETTLEMENT_STATIONS["new york"]
SETTLEMENT_STATIONS["la"] = SETTLEMENT_STATIONS["los angeles"]


def station_for_city(city: str | None) -> SettlementStation | None:
    """Settlement station for a parsed city name, or None if Kalshi has no
    verified station for it (in which case no observation lock may fire)."""
    if not city:
        return None
    return SETTLEMENT_STATIONS.get(city.strip().lower())
