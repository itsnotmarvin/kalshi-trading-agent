#!/usr/bin/env python3
"""
Score locked Kalshi forecast JSON against resolved outcomes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.forecast_scoring import (
    baseline_comparison,
    brier_score,
    calibration_buckets,
    directional_accuracy,
    log_loss,
)
from core.market_pricing import edge


def parse_timestamp(value: Any) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prediction_key(prediction: dict[str, Any]) -> str:
    return str(prediction.get("id") or f"{prediction.get('ticker')}|{prediction.get('cutoff')}")


def load_json(path: str | None) -> dict[str, Any]:
    return json.loads(Path(path).read_text()) if path else {}


def extract_predictions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("predictions"), list):
        return [dict(item) for item in payload["predictions"]]
    predictions = []
    for split in payload.get("splits", []):
        if isinstance(split, dict) and isinstance(split.get("prediction_lock"), dict):
            item = dict(split["prediction_lock"])
            item.setdefault("visible", split.get("visible"))
            item.setdefault("hidden", split.get("hidden"))
            predictions.append(item)
    if predictions:
        return predictions
    if {"predicted_probability", "outcome"}.issubset(payload):
        return [dict(payload)]
    raise ValueError("input must contain predictions, splits[].prediction_lock, or one prediction object")


def outcome_lookup(outcomes_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = outcomes_payload.get("outcomes", outcomes_payload)
    if isinstance(raw, list):
        return {prediction_key(item): dict(item) for item in raw if isinstance(item, dict)}
    if isinstance(raw, dict):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
    return {}


def merge_outcomes(predictions: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    for prediction in predictions:
        item = dict(prediction)
        item.update(outcomes.get(prediction_key(prediction), {}))
        merged.append(item)
    return merged


def validate_prediction(prediction: dict[str, Any]) -> None:
    key = prediction_key(prediction)
    if prediction.get("locked") is not True:
        raise ValueError(f"refusing to score unlocked prediction {key}")
    if not prediction.get("locked_at"):
        raise ValueError(f"refusing to score prediction {key} without locked_at")
    if not prediction.get("outcome_known_at"):
        raise ValueError(f"prediction {key} is missing outcome_known_at")
    if parse_timestamp(prediction["locked_at"]) > parse_timestamp(prediction["outcome_known_at"]):
        raise ValueError(f"refusing to score prediction {key}: locked_at is after outcome_known_at")
    for field in ("predicted_probability", "outcome", "market_mid_at_cutoff", "closing_price"):
        if prediction.get(field) is None:
            raise ValueError(f"prediction {key} is missing {field}")


def false_positive_catalyst_rate(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    false_positive = 0
    for prediction in predictions:
        for catalyst in prediction.get("predicted_catalysts") or []:
            if not isinstance(catalyst, dict):
                continue
            if "realized" not in catalyst:
                continue
            total += 1
            false_positive += int(catalyst.get("realized") is False)
    return {
        "n": total,
        "false_positives": false_positive,
        "rate": (false_positive / total) if total else None,
    }


def score_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_json(args.predictions)
    outcomes = outcome_lookup(load_json(args.outcomes))
    predictions = merge_outcomes(extract_predictions(payload), outcomes)
    for prediction in predictions:
        validate_prediction(prediction)

    probs = [float(item["predicted_probability"]) for item in predictions]
    actuals = [int(item["outcome"]) for item in predictions]
    markets = [float(item["market_mid_at_cutoff"]) for item in predictions]
    records = [
        {
            "model_p": item["predicted_probability"],
            "market_mid_at_cutoff": item["market_mid_at_cutoff"],
            "closing_price": item["closing_price"],
            "external_p": item.get("external_p"),
            "outcome": item["outcome"],
        }
        for item in predictions
    ]

    per_prediction = []
    for item in predictions:
        model_p = float(item["predicted_probability"])
        outcome = int(item["outcome"])
        market_p = float(item["market_mid_at_cutoff"])
        per_prediction.append(
            {
                "id": prediction_key(item),
                "ticker": item.get("ticker"),
                "cutoff": item.get("cutoff"),
                "predicted_probability": model_p,
                "outcome": outcome,
                "brier": (model_p - outcome) ** 2,
                "edge_vs_market": edge(model_p, market_p),
            }
        )

    return {
        "n": len(predictions),
        "per_prediction": per_prediction,
        "aggregate": {
            "brier": brier_score(probs, actuals),
            "log_loss": log_loss(probs, actuals),
            "calibration_buckets": calibration_buckets(probs, actuals),
            "directional_accuracy": directional_accuracy(probs, markets, actuals),
            "avg_edge_vs_market": sum(edge(p, m) for p, m in zip(probs, markets)) / len(probs),
            "baseline_table": baseline_comparison(records)["baselines"],
            "false_positive_catalyst_rate": false_positive_catalyst_rate(predictions),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score locked Kalshi predictions.")
    parser.add_argument("predictions", help="Locked predictions JSON")
    parser.add_argument("--outcomes", help="Optional outcomes JSON to merge by id or ticker|cutoff")
    parser.add_argument("--output", help="Output JSON path; defaults to stdout")
    args = parser.parse_args(argv)

    try:
        payload = score_payload(args)
    except Exception as exc:
        parser.exit(2, f"error: {exc}\n")
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
