import math

import pytest

from scripts.analyze_market_residuals import (
    price_age_bucket,
    price_bucket,
    strike_distance,
    strike_distance_bucket,
)
from scripts.backtest_weather_v2 import (
    apply_platt,
    fit_platt_calibration,
    fit_station_bias_gaussian,
    station_bias_log_loss,
)


def _strike_row(station, forecast, strike, outcome):
    return {
        "station_id": station,
        "forecast_daily_high_f": forecast,
        "strike_type": "greater",
        "floor_strike": strike,
        "cap_strike": None,
        "result_yes": outcome,
    }


def test_price_and_age_bucket_boundaries():
    assert price_bucket(0.0) == "longshot 0-10c"
    assert price_bucket(0.0999) == "longshot 0-10c"
    assert price_bucket(0.10) == "mid 10-40c"
    assert price_bucket(0.40) == "40-60c"
    assert price_bucket(0.60) == "favorite 60c+"
    assert price_age_bucket(0) == "0m"
    assert price_age_bucket(30) == "(0,30]m"
    assert price_age_bucket(60) == "(30,60]m"
    assert price_age_bucket(120) == "(60,120]m"


def test_strike_distance_uses_nearest_boundary_and_buckets():
    row = {
        "forecast_daily_high_f": 82.0,
        "floor_strike": 79.0,
        "cap_strike": 83.5,
    }
    assert strike_distance(row) == pytest.approx(1.5)
    assert strike_distance_bucket(0.999) == "[0,1)F"
    assert strike_distance_bucket(1.0) == "[1,3)F"
    assert strike_distance_bucket(3.0) == "[3,5)F"
    assert strike_distance_bucket(5.0) == "[5,10)F"
    assert strike_distance_bucket(10.0) == "10F+"


def test_station_bias_binary_likelihood_recovers_positive_forecast_bias():
    rows = []
    # Forecast is consistently two degrees high; strikes bracket actual=78F.
    for _ in range(20):
        rows.extend(
            [
                _strike_row("TEST", 80.0, 76.0, 1),
                _strike_row("TEST", 80.0, 77.0, 1),
                _strike_row("TEST", 80.0, 79.0, 0),
                _strike_row("TEST", 80.0, 80.0, 0),
            ]
        )

    biases, sigma = fit_station_bias_gaussian(rows)

    fitted_loss = station_bias_log_loss(rows, sigma, biases)
    zero_bias_loss = station_bias_log_loss(rows, sigma, {"TEST": 0.0})
    assert biases["TEST"] > 1.0
    assert fitted_loss < zero_bias_loss


def test_platt_calibration_reduces_synthetic_log_loss():
    raw = [0.05] * 20 + [0.25] * 20 + [0.75] * 20 + [0.95] * 20
    outcomes = (
        [1] * 4 + [0] * 16
        + [1] * 7 + [0] * 13
        + [1] * 13 + [0] * 7
        + [1] * 16 + [0] * 4
    )
    slope, intercept = fit_platt_calibration(raw, outcomes)
    calibrated = [apply_platt(value, slope, intercept) for value in raw]

    def loss(probabilities):
        return -sum(
            y * math.log(p) + (1 - y) * math.log(1 - p)
            for p, y in zip(probabilities, outcomes)
        ) / len(outcomes)

    assert 0.0 < slope < 1.0
    assert loss(calibrated) < loss(raw)
