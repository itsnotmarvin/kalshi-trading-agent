"""
Tests for the weather forecast log (every analyzed market, traded or not)
and the model-vs-market skill scorer built on top of it.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.weather_engine import WeatherEngine
from core.weather_skill import latest_per_market, load_forecasts, score_forecasts


class DummyMarket:
    def __init__(self, market_id, question, yes_price=0.65, no_price=0.35, end_date=None):
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


# ---------------------------------------------------------------------------
# Slice 1: every analyzed market lands in the forecast log
# ---------------------------------------------------------------------------

def test_analyze_market_logs_every_forecast_even_untraded(monkeypatch, tmp_path):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
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
            "current": {"temperature_2m": 68.0},
            "hourly": {"temperature_2m": [75.0]},
        },
    )
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)

    result = engine.analyze_market(
        DummyMarket(
            "KXHIGHNY-26JUN02-T70",
            "Will the high temperature in NYC be above 70°F?",
            yes_price=0.97,  # market almost agrees with the model → no edge
            no_price=0.03,
            end_date=target_time,
        )
    )

    # The market is analyzed but not tradeable — it must STILL be logged;
    # scoring only traded markets selection-biases the calibration curve.
    assert result is not None
    assert result["should_trade"] is False

    records = load_forecasts(engine.forecast_log_path)
    assert len(records) == 1
    record = records[0]
    assert record["market_id"] == "KXHIGHNY-26JUN02-T70"
    assert record["city"] == "nyc"
    assert record["probability"] == pytest.approx(31.5 / 32)
    assert record["market_yes_price"] == 0.97
    assert record["should_trade"] is False
    assert record["hrrr_veto"] is False
    assert record["threshold"] == 70.0


def test_observation_override_path_also_logs(monkeypatch, tmp_path):
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
            "current": {"temperature_2m": 74.0},
            "hourly": {"temperature_2m": [75.0]},
        },
    )
    # Settlement-station observation on the market's climate day, clearing
    # the threshold by more than LOCK_MARGIN_F → 0.99 override.
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    monkeypatch.setattr(
        engine,
        "get_station_observation",
        lambda _station: {
            "temperature_f": 74.0,
            "observed_at": now_ny.isoformat(),
            "station_id": "KNYC",
            "_provenance": {"source_type": "station_observation", "station_id": "KNYC"},
        },
    )
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)

    ticker = f"KXHIGHNY-{now_ny.strftime('%y%b%d').upper()}-T70"
    result = engine.analyze_market(
        DummyMarket(
            ticker,
            "Will the high temperature in NYC be above 70°F?",
            yes_price=0.55,
            no_price=0.45,
            end_date=target_time,
        )
    )

    assert result is not None
    assert result["probability_method"] == "observed threshold override"
    records = load_forecasts(engine.forecast_log_path)
    assert len(records) == 1
    assert records[0]["probability"] == 0.99
    assert records[0]["probability_method"] == "observed threshold override"


def test_forecast_logging_failure_never_breaks_analysis(monkeypatch, tmp_path):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
    engine = WeatherEngine()
    # Unwritable path (a directory) — logging must swallow the error.
    engine.forecast_log_path = tmp_path
    monkeypatch.setattr(
        engine,
        "get_ensemble_forecast",
        lambda **_kwargs: make_ensemble_data([75.0] * 31, target_time),
    )
    monkeypatch.setattr(
        engine,
        "get_deterministic_forecast",
        lambda **_kwargs: {
            "current": {"temperature_2m": 68.0},
            "hourly": {"temperature_2m": [75.0]},
        },
    )
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)

    result = engine.analyze_market(
        DummyMarket(
            "KXHIGHNY-26JUN02-T70",
            "Will the high temperature in NYC be above 70°F?",
            end_date=target_time,
        )
    )
    assert result is not None


# ---------------------------------------------------------------------------
# Slice 2: model-vs-market skill scoring
# ---------------------------------------------------------------------------

def _record(market_id, prob, price, city="new york", variable="temperature_2m", ts="2026-08-08T00:00:00+00:00"):
    return {
        "ts": ts,
        "market_id": market_id,
        "city": city,
        "variable": variable,
        "probability": prob,
        "market_yes_price": price,
    }


def test_latest_per_market_keeps_most_recent_forecast():
    records = [
        _record("M1", 0.60, 0.50, ts="2026-08-08T00:00:00+00:00"),
        _record("M1", 0.80, 0.55, ts="2026-08-08T06:00:00+00:00"),
        _record("M2", 0.30, 0.40, ts="2026-08-08T01:00:00+00:00"),
    ]
    latest = latest_per_market(records)
    assert len(latest) == 2
    assert latest["M1"]["probability"] == 0.80


def test_score_forecasts_computes_brier_skill_vs_market():
    forecasts = {
        # Model confident and right; market lukewarm.
        "M1": _record("M1", 0.90, 0.60),
        # Model doubtful and right (outcome NO); market leaned YES.
        "M2": _record("M2", 0.20, 0.50, city="chicago"),
        # Unresolved — must be excluded.
        "M3": _record("M3", 0.70, 0.65),
    }
    outcomes = {"M1": "yes", "M2": "no"}

    report = score_forecasts(forecasts, outcomes)

    assert report["n_forecasts"] == 3
    assert report["n_resolved"] == 2
    overall = report["overall"]
    # Model Brier: ((0.9−1)² + (0.2−0)²) / 2 = (0.01 + 0.04) / 2 = 0.025
    assert overall["model_brier"] == pytest.approx(0.025)
    # Market Brier: ((0.6−1)² + (0.5−0)²) / 2 = (0.16 + 0.25) / 2 = 0.205
    assert overall["market_brier"] == pytest.approx(0.205)
    # BSS = 1 − 0.025/0.205 ≈ 0.878 → model beats the market
    assert overall["brier_skill_score"] == pytest.approx(1 - 0.025 / 0.205, abs=1e-6)
    assert set(report["by_city"]) == {"new york", "chicago"}
    assert report["by_city"]["new york"]["n"] == 1


def test_score_forecasts_negative_skill_when_model_is_worse():
    forecasts = {"M1": _record("M1", 0.20, 0.60)}  # model wrong side, market right side
    report = score_forecasts(forecasts, {"M1": "yes"})
    assert report["overall"]["brier_skill_score"] < 0


def test_score_forecasts_empty_inputs():
    report = score_forecasts({}, {})
    assert report["n_resolved"] == 0
    assert report["overall"] == {"n": 0}


def test_load_forecasts_skips_malformed_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps(_record("M1", 0.5, 0.5)) + "\nnot json\n\n")
    records = load_forecasts(path)
    assert len(records) == 1
    assert load_forecasts(tmp_path / "missing.jsonl") == []
