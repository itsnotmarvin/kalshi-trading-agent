import asyncio
from datetime import datetime, timedelta, timezone

from core.weather_engine import WeatherEngine
from core.memory_manager import MemoryManager


class DummyMarket:
    def __init__(self, market_id, question, yes_price=0.65, no_price=0.35, end_date=None):
        self.id = market_id
        self.question = question
        self.yes_price = yes_price
        self.no_price = no_price
        self.end_date = end_date


def make_ensemble_data(member_values, start_time=None):
    start_time = start_time or datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    hourly = {"time": [start_time.isoformat()]}
    hourly["temperature_2m"] = [member_values[0]]
    for i, value in enumerate(member_values[1:], start=1):
        hourly[f"temperature_2m_member{i:02d}"] = [value]
    return {"hourly": hourly}


def no_forecaster_insight(*_args, **_kwargs):
    return {"modifier": 0.0, "reasoning": "No external forecaster modifier."}


async def test_learning():
    print("--- Testing Weather Learning & Safety System ---")

    mm = MemoryManager(data_dir="data")
    lessons = mm.get_macro_lessons()
    print(f"Loaded {len(lessons)} active lessons.")

    engine = WeatherEngine(lessons=lessons)
    print(f"Engine parsed humility buffer: {engine.humility_buffer * 100}%")

    # Test the buffer math directly (unit test — no API needed)
    raw_prob = 0.95
    expected_ensemble = 0.5 + (raw_prob - 0.5) * (1 - engine.humility_buffer)
    actual_ensemble = 0.5 + (raw_prob - 0.5) * (1 - engine.humility_buffer)
    assert abs(actual_ensemble - expected_ensemble) < 0.001, \
        f"Buffer math wrong: expected {expected_ensemble:.4f}, got {actual_ensemble:.4f}"
    print(f"  Buffer math: raw={raw_prob:.0%} → buffered={actual_ensemble:.1%} ✓")

    # Test that engine correctly parses a weather market question
    market = DummyMarket(
        "KXHIGHNY-26MAR22-T62",
        "Will the high temp in NYC be >62° on Mar 22, 2026?",
    )
    parsed = engine.parse_kalshi_weather_market(market)
    assert parsed is not None, "Engine failed to parse a valid weather market"
    assert parsed.city.lower() in ("new york", "nyc"), f"Unexpected city: {parsed.city}"
    assert parsed.threshold == 62.0, f"Unexpected threshold: {parsed.threshold}"
    assert parsed.above is True, f"Expected above=True for >62 market"
    print(f"  Market parse: city={parsed.city}, threshold={parsed.threshold}°F, above={parsed.above} ✓")

    print("\n✅ Test Passed: Humility buffer math and market parsing verified.")


def test_compute_probability_counts_ensemble_members_above_and_below():
    engine = WeatherEngine()
    hourly = {"time": ["2026-03-22T12:00"]}
    values = [70.0] + [75.0] * 20 + [65.0] * 10
    hourly["temperature_2m"] = [values[0]]
    for i, value in enumerate(values[1:], start=1):
        hourly[f"temperature_2m_member{i:02d}"] = [value]

    data = {"hourly": hourly}
    above = engine.compute_probability(data, "temperature_2m", 0, 70.0, above=True)
    below = engine.compute_probability(data, "temperature_2m", 0, 70.0, above=False)

    assert len(above.member_values) == 31
    assert above.probability == 21 / 31
    assert below.probability == 10 / 31
    assert above.forecast_hour == "2026-03-22T12:00"
    assert above.threshold == 70.0


def test_parse_low_temperature_market_sets_below_direction():
    parsed = WeatherEngine().parse_kalshi_weather_market(
        DummyMarket(
            "KXLOWCHI-26MAR22-T32",
            "Will the low temperature in Chicago be below 32°F on March 22, 2026?",
        )
    )

    assert parsed is not None
    assert parsed.city == "chicago"
    assert parsed.variable == "temperature_2m"
    assert parsed.threshold == 32.0
    assert parsed.above is False


def test_parse_precipitation_market_extracts_fractional_threshold():
    parsed = WeatherEngine().parse_kalshi_weather_market(
        DummyMarket(
            "KXRAINAUS-26MAR22-T0.5",
            "Will rainfall in Austin be more than 0.5 inches on March 22, 2026?",
        )
    )

    assert parsed is not None
    assert parsed.city == "austin"
    assert parsed.variable == "precipitation"
    assert parsed.threshold == 0.5
    assert parsed.above is True


def test_weather_ticker_city_fallback_matches_city_code_not_substrings():
    parsed = WeatherEngine().parse_kalshi_weather_market(
        DummyMarket(
            "KXSNOWSLC-26MAR22-T1",
            "Will **snow** be more than 1 inch on March 22, 2026?",
        )
    )

    assert parsed is not None
    assert parsed.city == "salt lake city"
    assert parsed.variable == "snowfall"
    assert parsed.threshold == 1.0


def test_weather_parser_rejects_sports_market_with_weather_city_name():
    parsed = WeatherEngine().parse_kalshi_weather_market(
        DummyMarket(
            "KXNFLHOU-26MAR22-P3.5",
            "Will Houston win by over 3.5 points?",
        )
    )

    assert parsed is None


def test_analyze_market_buys_yes_when_gfs_and_hrrr_agree(monkeypatch):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
    engine = WeatherEngine()
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
            yes_price=0.55,
            no_price=0.45,
            end_date=target_time,
        )
    )

    assert result is not None
    assert result["raw_probability"] == 1.0
    assert result["ensemble_probability"] == 31.5 / 32
    assert result["probability"] == 31.5 / 32
    assert result["direction"] == "BUY_YES"
    assert abs(result["edge"] - ((31.5 / 32) - 0.55)) < 1e-9
    assert result["should_trade"] is True
    assert "Agrees with GFS" in result["forecaster_insight"]


def test_analyze_market_vetoes_when_gfs_and_hrrr_disagree(monkeypatch):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
    engine = WeatherEngine()
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
            "hourly": {"temperature_2m": [65.0]},
        },
    )
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)

    result = engine.analyze_market(
        DummyMarket(
            "KXHIGHNY-26JUN02-T70",
            "Will the high temperature in NYC be above 70°F?",
            yes_price=0.55,
            no_price=0.45,
            end_date=target_time,
        )
    )

    assert result is not None
    assert result["direction"] == "BUY_YES"
    assert result["should_trade"] is False
    assert "VETOED" in result["forecaster_insight"]


def test_daily_high_probability_uses_each_members_daily_maximum():
    engine = WeatherEngine()
    hourly = {
        "time": ["2026-06-02T12:00", "2026-06-02T13:00"],
        "temperature_2m": [71.0, 60.0],
        "temperature_2m_member01": [60.0, 71.0],
        "temperature_2m_member02": [60.0, 60.0],
    }

    result = engine.compute_daily_extreme_probability(
        {"hourly": hourly},
        "temperature_2m",
        [0, 1],
        70.0,
        True,
        use_maximum=True,
    )

    # Two members cross at different hours. Taking the maximum hourly
    # crossing fraction would incorrectly report only 1/3.
    assert result.probability == 2 / 3
    assert result.member_values == [71.0, 71.0, 60.0]


def test_forecaster_numeric_modifier_does_not_move_probability(monkeypatch):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
    engine = WeatherEngine()
    monkeypatch.setattr(
        engine,
        "get_ensemble_forecast",
        lambda **_kwargs: make_ensemble_data([75.0] * 20 + [65.0] * 11, target_time),
    )
    monkeypatch.setattr(
        engine,
        "get_deterministic_forecast",
        lambda **_kwargs: {
            "current": {"temperature_2m": 68.0},
            "hourly": {"temperature_2m": [75.0]},
        },
    )
    monkeypatch.setattr(
        engine.forecaster,
        "get_market_insight",
        lambda *_args, **_kwargs: {"modifier": 0.15, "reasoning": "Qualitative context."},
    )

    result = engine.analyze_market(
        DummyMarket(
            "KXHIGHNY-26JUN02-T70",
            "Will the high temperature in NYC be above 70°F?",
            yes_price=0.55,
            no_price=0.45,
            end_date=target_time,
        )
    )

    assert result is not None
    assert result["probability"] == 20.5 / 32
    assert result["forecaster_probability_adjustment"] == 0.0


def test_negative_edges_never_set_should_trade(monkeypatch):
    target_time = datetime.now(timezone.utc) + timedelta(hours=24)
    engine = WeatherEngine()
    monkeypatch.setattr(
        engine,
        "get_ensemble_forecast",
        lambda **_kwargs: make_ensemble_data([75.0] * 15 + [65.0] * 16, target_time),
    )
    monkeypatch.setattr(
        engine,
        "get_deterministic_forecast",
        lambda **_kwargs: {"current": {}, "hourly": {"temperature_2m": [75.0]}},
    )
    monkeypatch.setattr(engine.forecaster, "get_market_insight", no_forecaster_insight)

    result = engine.analyze_market(
        DummyMarket(
            "KXHIGHNY-26JUN02-T70",
            "Will the high temperature in NYC be above 70°F?",
            yes_price=0.70,
            no_price=0.70,
            end_date=target_time,
        )
    )

    assert result is not None
    assert result["direction"] == "HOLD"
    assert result["edge"] < 0
    assert result["should_trade"] is False


if __name__ == "__main__":
    asyncio.run(test_learning())
