"""
Pure-function tests for the forecast archiver (scripts/archive_forecasts.py):
ticker → climate day, per-member daily maxima, and the Jeffreys-smoothed
YES probability for each Kalshi strike type.
"""
from datetime import date

import pytest

from scripts.archive_forecasts import (
    member_daily_maxes,
    probability_yes,
    ticker_target_date,
    unique_stations,
)


def test_ticker_target_date_parses_climate_day():
    assert ticker_target_date("KXHIGHNY-26AUG13-T92") == date(2026, 8, 13)
    assert ticker_target_date("KXHIGHDEN-26MAR23-B71.5") == date(2026, 3, 23)


def test_ticker_target_date_rejects_garbage():
    assert ticker_target_date("KXHIGHNY") is None
    assert ticker_target_date("KXHIGHNY-26FOO13-T92") is None
    assert ticker_target_date("KXHIGHNY-26FEB31-T92") is None  # invalid day


def test_member_daily_maxes_filters_to_target_day():
    hourly = {
        "time": ["2026-08-12T23:00", "2026-08-13T00:00", "2026-08-13T15:00"],
        "temperature_2m": [90.0, 70.0, 80.0],
        "temperature_2m_member01": [95.0, 71.0, 79.0],
    }
    maxes = member_daily_maxes(hourly, date(2026, 8, 13))
    # The 90/95°F readings belong to Aug 12 and must not leak in.
    assert sorted(maxes) == [79.0, 80.0]


def test_member_daily_maxes_empty_when_day_not_covered():
    hourly = {"time": ["2026-08-12T23:00"], "temperature_2m": [90.0]}
    assert member_daily_maxes(hourly, date(2026, 8, 14)) == []


def test_probability_yes_greater_uses_strict_inequality():
    # Rules say "greater than X": a member exactly at the strike is a NO.
    maxes = [92.0, 93.0, 94.0]
    prob = probability_yes(maxes, "greater", 92.0, None)
    assert prob == pytest.approx((2 + 0.5) / (3 + 1))


def test_probability_yes_less_and_between():
    maxes = [88.0, 91.0, 92.0, 95.0]
    assert probability_yes(maxes, "less", None, 92.0) == pytest.approx((2 + 0.5) / (4 + 1))
    assert probability_yes(maxes, "between", 91.0, 92.0) == pytest.approx((2 + 0.5) / (4 + 1))


def test_probability_yes_refuses_unknown_or_empty():
    assert probability_yes([], "greater", 92.0, None) is None
    assert probability_yes([90.0], "greater", None, None) is None
    assert probability_yes([90.0], "exotic", 92.0, 93.0) is None


def test_unique_stations_deduplicates_aliases():
    stations = unique_stations()
    assert len(stations) == 7  # nyc/new york and la/los angeles collapse
    assert stations["KAUS"].name == "Austin Bergstrom"
