#!/usr/bin/env python3
"""Offline, leakage-safe proxy iterations for the weather backtest.

The v2 model treats ``forecast - actual`` as a station-specific Gaussian
bias.  Biases and a shared sigma are fitted by binary likelihood using only
the training portion of each expanding fold.  An optional Platt layer is then
fitted on those same training rows and applied only to the held-out fold.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_weather import (
    EPSILON,
    build_walk_forward_folds,
    gaussian_yes_probability,
)


def _bounded_golden_search(function, low: float, high: float, iterations: int = 55) -> float:
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = high - ratio * (high - low)
    right = low + ratio * (high - low)
    left_value = function(left)
    right_value = function(right)
    for _ in range(iterations):
        if left_value <= right_value:
            high, right, right_value = right, left, left_value
            left = high - ratio * (high - low)
            left_value = function(left)
        else:
            low, left, left_value = left, right, right_value
            right = low + ratio * (high - low)
            right_value = function(right)
    return (low + high) / 2.0


def station_bias_probability(row: dict[str, Any], sigma: float, bias_f: float) -> float:
    """YES probability when station bias is E[forecast high - actual high]."""
    return gaussian_yes_probability(
        float(row["forecast_daily_high_f"]) - float(bias_f),
        sigma,
        str(row["strike_type"]),
        row.get("floor_strike"),
        row.get("cap_strike"),
    )


def station_bias_log_loss(
    rows: Sequence[dict[str, Any]],
    sigma: float,
    station_biases: dict[str, float],
) -> float:
    if not rows:
        raise ValueError("at least one training row is required")
    total = 0.0
    for row in rows:
        probability = station_bias_probability(
            row, sigma, station_biases.get(str(row["station_id"]), 0.0)
        )
        outcome = int(row["result_yes"])
        total -= outcome * math.log(probability) + (1 - outcome) * math.log(1.0 - probability)
    return total / len(rows)


def fit_station_bias_gaussian(
    rows: Sequence[dict[str, Any]],
    *,
    max_rounds: int = 12,
) -> tuple[dict[str, float], float]:
    """Fit per-station bias and shared sigma by coordinate likelihood search."""
    if not rows:
        raise ValueError("at least one training row is required")
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_station[str(row["station_id"])].append(row)
    biases = {station: 0.0 for station in by_station}
    sigma = 4.0

    for _ in range(max_rounds):
        previous = (dict(biases), sigma)
        sigma = math.exp(
            _bounded_golden_search(
                lambda log_sigma: station_bias_log_loss(rows, math.exp(log_sigma), biases),
                math.log(0.20),
                math.log(30.0),
            )
        )
        for station, station_rows in sorted(by_station.items()):
            other_biases = dict(biases)

            def loss(candidate: float) -> float:
                other_biases[station] = candidate
                return station_bias_log_loss(station_rows, sigma, other_biases)

            biases[station] = _bounded_golden_search(loss, -15.0, 15.0)
        largest_change = max(
            [abs(sigma - previous[1])]
            + [abs(biases[key] - previous[0][key]) for key in biases]
        )
        if largest_change < 1e-5:
            break
    # Finish with sigma conditional on the final station biases.
    sigma = math.exp(
        _bounded_golden_search(
            lambda log_sigma: station_bias_log_loss(rows, math.exp(log_sigma), biases),
            math.log(0.20),
            math.log(30.0),
        )
    )
    return biases, sigma


def _logit(probability: float) -> float:
    clean = min(1.0 - 1e-6, max(1e-6, float(probability)))
    return math.log(clean / (1.0 - clean))


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def apply_platt(probability: float, slope: float, intercept: float) -> float:
    return min(1.0 - EPSILON, max(EPSILON, _sigmoid(slope * _logit(probability) + intercept)))


def _platt_objective(
    raw_probabilities: Sequence[float],
    outcomes: Sequence[int],
    slope: float,
    intercept: float,
    ridge: float,
) -> float:
    total = 0.5 * ridge * ((slope - 1.0) ** 2 + intercept**2)
    for probability, outcome in zip(raw_probabilities, outcomes):
        calibrated = apply_platt(probability, slope, intercept)
        total -= outcome * math.log(calibrated) + (1 - outcome) * math.log(1 - calibrated)
    return total


def fit_platt_calibration(
    raw_probabilities: Sequence[float],
    outcomes: Sequence[int],
    *,
    ridge: float = 1e-3,
) -> tuple[float, float]:
    """Fit ``sigmoid(slope * logit(p) + intercept)`` with damped Newton steps."""
    if len(raw_probabilities) != len(outcomes) or not raw_probabilities:
        raise ValueError("probabilities and outcomes must have the same non-zero length")
    if any(int(outcome) not in (0, 1) for outcome in outcomes):
        raise ValueError("outcomes must be binary")
    slope, intercept = 1.0, 0.0
    current = _platt_objective(raw_probabilities, outcomes, slope, intercept, ridge)
    for _ in range(60):
        gradient_slope = ridge * (slope - 1.0)
        gradient_intercept = ridge * intercept
        h_ss = ridge
        h_si = 0.0
        h_ii = ridge
        for probability, outcome in zip(raw_probabilities, outcomes):
            feature = _logit(probability)
            calibrated = apply_platt(probability, slope, intercept)
            difference = calibrated - int(outcome)
            weight = calibrated * (1.0 - calibrated)
            gradient_slope += difference * feature
            gradient_intercept += difference
            h_ss += weight * feature * feature
            h_si += weight * feature
            h_ii += weight
        determinant = h_ss * h_ii - h_si * h_si
        if determinant <= 1e-18:
            break
        delta_slope = (h_ii * gradient_slope - h_si * gradient_intercept) / determinant
        delta_intercept = (-h_si * gradient_slope + h_ss * gradient_intercept) / determinant
        if max(abs(delta_slope), abs(delta_intercept)) < 1e-9:
            break
        step = 1.0
        accepted = False
        while step >= 1e-6:
            candidate_slope = slope - step * delta_slope
            candidate_intercept = intercept - step * delta_intercept
            candidate = _platt_objective(
                raw_probabilities,
                outcomes,
                candidate_slope,
                candidate_intercept,
                ridge,
            )
            if candidate <= current:
                slope, intercept, current = candidate_slope, candidate_intercept, candidate
                accepted = True
                break
            step /= 2.0
        if not accepted:
            break
    return slope, intercept


def walk_forward_v2_predictions(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return OOS station-bias and station-bias-plus-Platt predictions."""
    ordered, folds = build_walk_forward_folds(rows)
    bias_predictions: list[dict[str, Any]] = []
    calibrated_predictions: list[dict[str, Any]] = []
    for fold_number, fold in enumerate(folds, start=1):
        training = ordered[fold["train_start"] : fold["train_end"] + 1]
        biases, sigma = fit_station_bias_gaussian(training)
        training_raw = [
            station_bias_probability(
                row, sigma, biases.get(str(row["station_id"]), 0.0)
            )
            for row in training
        ]
        slope, intercept = fit_platt_calibration(
            training_raw, [int(row["result_yes"]) for row in training]
        )
        for row in ordered[fold["test_start"] : fold["test_end"] + 1]:
            raw_probability = station_bias_probability(
                row, sigma, biases.get(str(row["station_id"]), 0.0)
            )
            metadata = {
                "sigma": sigma,
                "station_bias_f": biases.get(str(row["station_id"]), 0.0),
                "fold": fold_number,
                "training_cutoff": fold["cutoff"],
            }
            bias_row = dict(row, model_p=raw_probability, **metadata)
            calibrated_row = dict(
                row,
                model_p=apply_platt(raw_probability, slope, intercept),
                platt_slope=slope,
                platt_intercept=intercept,
                **metadata,
            )
            bias_predictions.append(bias_row)
            calibrated_predictions.append(calibrated_row)
    return bias_predictions, calibrated_predictions
