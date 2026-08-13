"""
Settlement-station alignment: forecasts and observation locks must use the
station Kalshi actually settles on, on the market's own climate day.

Regression targets:
  - grid "current" temperature must never fire a 99%-certainty lock
  - an observation from the wrong calendar day must never lock a market
  - locks must clear the threshold by LOCK_MARGIN_F, not by rounding luck
  - forecasts are requested at settlement-station coordinates
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.settlement_stations import SETTLEMENT_STATIONS, station_for_city
from core.weather_engine import LOCK_MARGIN_F, WeatherEngine


class DummyMarket:
    def __init__(self, market_id, question, yes_price=0.55, no_price=0.45, end_date=None):
        self.id = market_id
        self.question = question
        self.yes_price = yes_price
        self.no_price = no_price
        self.end_date = end_date


def make_ensemble_data(member_values, start_time):
    hourly = {"time": [start_time.isoformat()]}
    hourly["temperature_2m"] = [member_values[0]]
    for i, value in enumerate(member_values[1:], start=1):
        hourly[f"temperature_2m_member{i:02d}"] = [value]
    return {"hourly": hourly}


def no_forecaster_insight(*_args, **_kwargs):
    return {"modifier": 0.0, "reasoning": "No external forecaster modifier."}


def build_engine(monkeypatch, tmp_path, station_obs):
    """Engine with network calls stubbed; station_obs is what the settlement
    station reports (None = observation unavailable)."""
    target_time = datetime.now(timezone.utc) + timedelta(hours=6)
    engine = WeatherEngine()
    engine.forecast_log_path = tmp_path / "weather_forecast_log.jsonl"
    monkeypatch.setattr(
        engine,
        "get_ensemble_forecast",
        lambda **_kwargs: make_ensemble_data([75.0] * 31, target_time),
    )
    monkeypatch.setattr(
        engine,
        "get_deterministic_forecast",
        lambda **_kwargs: {
            # Grid current temp far above threshold — must NOT cause a lock.
            "current": {"temperature_2m": 100.0},
            "hourly": {"temperature_2m": [75.0]},
        },
    )
    monkeypatch.setattr(engine, "get_station_observation", lambda _station: station_obs)
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)
    return engine


def ny_market(day: datetime, threshold: int = 70):
    ticker = f"KXHIGHNY-{day.strftime('%y%b%d').upper()}-T{threshold}"
    return DummyMarket(
        ticker,
        f"Will the high temperature in NYC be above {threshold}°F?",
        end_date=datetime.now(timezone.utc) + timedelta(hours=6),
    )


def station_obs(temp_f: float, observed_at: datetime) -> dict:
    return {
        "temperature_f": temp_f,
        "observed_at": observed_at.isoformat(),
        "station_id": "KNYC",
        "_provenance": {"source_type": "station_observation", "station_id": "KNYC"},
    }


# ---------------------------------------------------------------------------
# Parser: settlement-station coordinates
# ---------------------------------------------------------------------------

def test_parser_uses_settlement_station_coordinates(monkeypatch, tmp_path):
    engine = build_engine(monkeypatch, tmp_path, station_obs=None)
    parsed = engine.parse_kalshi_weather_market(
        ny_market(datetime.now(ZoneInfo("America/New_York")))
    )
    knyc = SETTLEMENT_STATIONS["new york"]
    assert parsed is not None
    assert (parsed.lat, parsed.lon) == (knyc.lat, knyc.lon)


def test_denver_maps_to_dia_not_downtown():
    kden = station_for_city("denver")
    assert kden is not None
    assert kden.station_id == "KDEN"
    # Downtown Denver is ~(39.74, -104.99); DIA is ~30 km northeast.
    assert kden.lon > -104.8


def test_parser_resolves_lax_and_phil_ticker_codes(monkeypatch, tmp_path):
    engine = build_engine(monkeypatch, tmp_path, station_obs=None)
    for ticker, city in (("KXHIGHLAX-26AUG13-T77", "los angeles"),
                         ("KXHIGHPHIL-26AUG13-T93", "philadelphia")):
        parsed = engine.parse_kalshi_weather_market(
            DummyMarket(ticker, "Will the high temperature be above the cap?")
        )
        assert parsed is not None, ticker
        assert parsed.city == city
        assert parsed.target_date is not None
        assert parsed.target_date.date() == datetime(2026, 8, 13).date()


# ---------------------------------------------------------------------------
# Override: only settlement-station observations may lock
# ---------------------------------------------------------------------------

def test_grid_current_temp_never_locks_without_station_obs(monkeypatch, tmp_path):
    # Grid says 100°F, but the settlement station has no observation:
    # the engine must fall through to the ensemble, not fake certainty.
    engine = build_engine(monkeypatch, tmp_path, station_obs=None)
    result = engine.analyze_market(ny_market(datetime.now(ZoneInfo("America/New_York"))))
    assert result is not None
    assert result["probability_method"] != "observed threshold override"


def test_wrong_day_observation_never_locks(monkeypatch, tmp_path):
    # Yesterday hit 74°F at KNYC, but the market is about today: no lock.
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    engine = build_engine(
        monkeypatch, tmp_path, station_obs=station_obs(74.0, now_ny - timedelta(days=1))
    )
    result = engine.analyze_market(ny_market(now_ny))
    assert result is not None
    assert result["probability_method"] != "observed threshold override"


def test_tomorrows_market_never_locks_on_todays_heat(monkeypatch, tmp_path):
    # Today's 74°F observation must not lock tomorrow's market.
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    engine = build_engine(monkeypatch, tmp_path, station_obs=station_obs(74.0, now_ny))
    result = engine.analyze_market(ny_market(now_ny + timedelta(days=1)))
    assert result is not None
    assert result["probability_method"] != "observed threshold override"


def test_sub_margin_clearance_does_not_lock(monkeypatch, tmp_path):
    # 70.5°F against a 70° threshold is inside METAR/CLI rounding noise.
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    assert 0.5 < LOCK_MARGIN_F
    engine = build_engine(monkeypatch, tmp_path, station_obs=station_obs(70.5, now_ny))
    result = engine.analyze_market(ny_market(now_ny))
    assert result is not None
    assert result["probability_method"] != "observed threshold override"


def test_same_day_station_obs_clearing_margin_locks_yes(monkeypatch, tmp_path):
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    engine = build_engine(
        monkeypatch, tmp_path, station_obs=station_obs(70.0 + LOCK_MARGIN_F, now_ny)
    )
    result = engine.analyze_market(ny_market(now_ny))
    assert result is not None
    assert result["probability_method"] == "observed threshold override"
    assert result["probability"] == 0.99
    assert result["direction"] == "BUY_YES"


def test_station_obs_breaking_below_bound_locks_no(monkeypatch, tmp_path):
    # "High temp below 70" market: an observed 71°F+ max breaks it → NO.
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    engine = build_engine(
        monkeypatch, tmp_path, station_obs=station_obs(70.0 + LOCK_MARGIN_F, now_ny)
    )
    ticker = f"KXHIGHNY-{now_ny.strftime('%y%b%d').upper()}-T70"
    result = engine.analyze_market(
        DummyMarket(
            ticker,
            "Will the high temperature in NYC be below 70°F?",
            end_date=datetime.now(timezone.utc) + timedelta(hours=6),
        )
    )
    assert result is not None
    assert result["probability_method"] == "observed threshold override"
    assert result["probability"] == 0.01
    assert result["direction"] == "BUY_NO"
