from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import backtest_weather
from scripts.backtest_weather import (
    assert_unique_grain,
    build_walk_forward_folds,
    compute_forecast_bundle,
    gaussian_yes_probability,
    select_candle,
)


def test_cutoff_computation_uses_latest_component_issue_time():
    hourly = {
        "time": [f"2026-08-10T{hour:02d}:00" for hour in range(24)],
        "temperature_2m_previous_day1": [70.0 + hour / 10.0 for hour in range(24)],
    }

    bundle, reason = compute_forecast_bundle(
        hourly,
        date(2026, 8, 10),
        1,
        "America/New_York",
    )

    assert reason is None
    assert bundle["forecast_cutoff_ts"] == "2026-08-10T03:00:00Z"
    assert bundle["forecast_daily_high_f"] == 72.3


def test_leakage_guard_rejects_candle_after_cutoff():
    cutoff = datetime(2026, 8, 10, 3, tzinfo=timezone.utc)
    candle = {
        "end_period_ts": int((cutoff + timedelta(hours=1)).timestamp()),
        "yes_bid": {"close_dollars": "0.45"},
        "yes_ask": {"close_dollars": "0.51"},
    }

    selected, reason = select_candle([candle], cutoff)

    assert selected is None
    assert reason == "candle_after_cutoff_only"


def test_join_grain_uniqueness_rejects_duplicate_market_horizon():
    rows = [
        {"market_ticker": "KXHIGHNY-26AUG10-T80", "horizon_days": 1},
        {"market_ticker": "KXHIGHNY-26AUG10-T80", "horizon_days": 3},
    ]
    assert_unique_grain(rows)

    with pytest.raises(ValueError, match="duplicate dataset grain"):
        assert_unique_grain([rows[0], dict(rows[0])])


def test_gaussian_probability_is_monotone_in_forecast_for_greater_strike():
    probabilities = [
        gaussian_yes_probability(forecast, 4.0, "greater", 80.0, None)
        for forecast in (74.0, 80.0, 86.0)
    ]

    assert probabilities[0] < probabilities[1] < probabilities[2]
    assert probabilities[1] == pytest.approx(0.5)


def test_walk_forward_folds_use_core_helper_and_never_train_on_test(monkeypatch):
    called = False
    real_helper = backtest_weather.make_walk_forward_splits

    def recording_helper(timestamps, cutoffs):
        nonlocal called
        called = True
        return real_helper(timestamps, cutoffs)

    monkeypatch.setattr(backtest_weather, "make_walk_forward_splits", recording_helper)
    rows = [
        {
            "target_date": f"2026-08-{day:02d}",
            "market_ticker": f"M{day}",
        }
        for day in range(1, 10)
    ]

    ordered, folds = build_walk_forward_folds(rows, min_train_days=3, max_folds=3)

    assert called
    assert folds
    for fold in folds:
        training_dates = {
            ordered[index]["target_date"]
            for index in range(fold["train_start"], fold["train_end"] + 1)
        }
        test_dates = {
            ordered[index]["target_date"]
            for index in range(fold["test_start"], fold["test_end"] + 1)
        }
        assert max(training_dates) < min(test_dates)
        assert training_dates.isdisjoint(test_dates)
