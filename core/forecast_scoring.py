"""
Forecast scoring helpers for locked Kalshi prediction workflows.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any


DEFAULT_BUCKET_EDGES = tuple(i / 10 for i in range(11))
EPSILON = 1e-15


def _as_float_list(values: Iterable[float]) -> list[float]:
    return [float(value) for value in values]


def _as_binary_list(values: Iterable[int | bool | float]) -> list[int]:
    outcomes = []
    for value in values:
        numeric = int(value)
        if numeric not in (0, 1):
            raise ValueError("outcomes must be binary 0/1 values")
        outcomes.append(numeric)
    return outcomes


def _require_same_length(predictions: Sequence[Any], outcomes: Sequence[Any]) -> None:
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must have the same length")
    if not predictions:
        raise ValueError("at least one prediction is required")


def brier_score(predictions: Iterable[float], outcomes: Iterable[int | bool | float]) -> float:
    """Return mean Brier score for binary forecasts."""
    probs = _as_float_list(predictions)
    actuals = _as_binary_list(outcomes)
    _require_same_length(probs, actuals)
    return sum((p - outcome) ** 2 for p, outcome in zip(probs, actuals)) / len(probs)


def log_loss(
    predictions: Iterable[float],
    outcomes: Iterable[int | bool | float],
    min_samples: int = 30,
) -> float | dict[str, Any]:
    """Return mean log loss, gated until enough resolved predictions exist."""
    probs = _as_float_list(predictions)
    actuals = _as_binary_list(outcomes)
    _require_same_length(probs, actuals)
    if len(probs) < min_samples:
        return {
            "value": None,
            "reason": f"log loss requires at least {min_samples} resolved predictions",
            "n": len(probs),
            "min_samples": min_samples,
        }
    total = 0.0
    for p, outcome in zip(probs, actuals):
        clean = min(1.0 - EPSILON, max(EPSILON, p))
        total += -(outcome * math.log(clean) + (1 - outcome) * math.log(1.0 - clean))
    return total / len(probs)


def calibration_buckets(
    predictions: Iterable[float],
    outcomes: Iterable[int | bool | float],
    bucket_edges: Sequence[float] = DEFAULT_BUCKET_EDGES,
) -> list[dict[str, Any]]:
    """Return calibration buckets with counts, mean prediction, and observed rate."""
    probs = _as_float_list(predictions)
    actuals = _as_binary_list(outcomes)
    _require_same_length(probs, actuals)
    if len(bucket_edges) < 2:
        raise ValueError("bucket_edges must contain at least two values")

    buckets: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(bucket_edges, bucket_edges[1:])):
        in_bucket: list[tuple[float, int]] = []
        for p, outcome in zip(probs, actuals):
            if (index == len(bucket_edges) - 2 and start <= p <= end) or (start <= p < end):
                in_bucket.append((p, outcome))
        n = len(in_bucket)
        buckets.append(
            {
                "range": f"{start:.1f}-{end:.1f}",
                "n": n,
                "mean_pred": (sum(p for p, _ in in_bucket) / n) if n else None,
                "observed_rate": (sum(outcome for _, outcome in in_bucket) / n) if n else None,
            }
        )
    return buckets


def directional_accuracy(
    predictions: Iterable[float],
    market_probs: Iterable[float],
    outcomes: Iterable[int | bool | float],
) -> dict[str, Any]:
    """Score whether the model's side-vs-market call matched the outcome."""
    probs = _as_float_list(predictions)
    markets = _as_float_list(market_probs)
    actuals = _as_binary_list(outcomes)
    if not (len(probs) == len(markets) == len(actuals)):
        raise ValueError("predictions, market_probs, and outcomes must have the same length")
    if not probs:
        raise ValueError("at least one prediction is required")

    scoreable = 0
    correct = 0
    for p, market, outcome in zip(probs, markets, actuals):
        if p == market:
            continue
        side_yes = p > market
        correct += int((side_yes and outcome == 1) or ((not side_yes) and outcome == 0))
        scoreable += 1
    return {
        "n": scoreable,
        "correct": correct,
        "accuracy": (correct / scoreable) if scoreable else None,
    }


def baseline_comparison(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compare model Brier score against required probability baselines."""
    rows = list(records)
    if not rows:
        raise ValueError("at least one record is required")
    outcomes = _as_binary_list(row["outcome"] for row in rows)
    model_predictions = _as_float_list(row["model_p"] for row in rows)
    model_brier = brier_score(model_predictions, outcomes)

    baselines: dict[str, list[float]] = {
        "market_only": _as_float_list(row["market_mid_at_cutoff"] for row in rows),
        "naive_0_5": [0.5 for _ in rows],
        "closing_price": _as_float_list(row["closing_price"] for row in rows),
    }
    external = [row.get("external_p") for row in rows]
    if any(value is not None for value in external):
        present = [(float(row["model_p"]), float(row["external_p"]), int(row["outcome"])) for row in rows if row.get("external_p") is not None]
        external_model_brier = brier_score([item[0] for item in present], [item[2] for item in present])
        external_brier = brier_score([item[1] for item in present], [item[2] for item in present])
    else:
        external_model_brier = None
        external_brier = None

    table = []
    for name, predictions in baselines.items():
        baseline_brier = brier_score(predictions, outcomes)
        delta = model_brier - baseline_brier
        table.append(
            {
                "baseline": name,
                "model_brier": model_brier,
                "baseline_brier": baseline_brier,
                "delta": delta,
                "result": "beat" if delta < 0 else ("tied" if delta == 0 else "lost"),
            }
        )

    if external_brier is not None and external_model_brier is not None:
        delta = external_model_brier - external_brier
        table.append(
            {
                "baseline": "external",
                "model_brier": external_model_brier,
                "baseline_brier": external_brier,
                "delta": delta,
                "result": "beat" if delta < 0 else ("tied" if delta == 0 else "lost"),
            }
        )

    return {"model_brier": model_brier, "baselines": table}


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def make_walk_forward_splits(
    timestamps: Sequence[Any],
    cutoffs: Sequence[Any],
) -> list[dict[str, Any]]:
    """Return visible/hidden index ranges for chronological walk-forward scoring."""
    parsed_timestamps = [_parse_timestamp(value) for value in timestamps]
    parsed_cutoffs = [_parse_timestamp(value) for value in cutoffs]
    if parsed_timestamps != sorted(parsed_timestamps):
        raise ValueError("timestamps must be sorted chronologically")
    if any(current <= previous for previous, current in zip(parsed_cutoffs, parsed_cutoffs[1:])):
        raise ValueError("hidden-data leak request: cutoffs must be strictly increasing")

    splits = []
    previous_hidden_start = 0
    for index, cutoff in enumerate(parsed_cutoffs):
        visible = [i for i, timestamp in enumerate(parsed_timestamps) if timestamp <= cutoff]
        if not visible:
            raise ValueError(f"cutoff {cutoff.isoformat()} has no visible history")
        hidden = [i for i, timestamp in enumerate(parsed_timestamps) if timestamp > cutoff]
        if not hidden:
            raise ValueError(f"cutoff {cutoff.isoformat()} has no hidden future to score")
        hidden_start = hidden[0]
        if hidden_start < previous_hidden_start:
            raise ValueError("hidden-data leak request: cutoffs must move forward chronologically")
        previous_hidden_start = hidden_start
        splits.append(
            {
                "cutoff": cutoff.isoformat(),
                "visible": {"start": visible[0], "end": visible[-1]},
                "hidden": {"start": hidden[0], "end": hidden[-1]},
            }
        )
    return splits
