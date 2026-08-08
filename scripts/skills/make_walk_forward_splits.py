#!/usr/bin/env python3
"""
Create chronological walk-forward split skeletons from normalized history.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.forecast_scoring import make_walk_forward_splits


def parse_cutoffs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def auto_cutoffs(timestamps: list[str], count: int) -> list[str]:
    if count <= 0:
        raise ValueError("--auto-cutoffs must be positive")
    if len(timestamps) < count + 1:
        raise ValueError("not enough history points for requested auto cutoffs")
    step = max(1, len(timestamps) // (count + 1))
    indexes = [min(len(timestamps) - 2, step * (index + 1)) for index in range(count)]
    seen: set[int] = set()
    cutoffs = []
    for index in indexes:
        if index not in seen:
            cutoffs.append(timestamps[index])
            seen.add(index)
    return cutoffs


def build_payload(args: argparse.Namespace) -> dict:
    history = json.loads(Path(args.history).read_text())
    points = history.get("points") or []
    timestamps = [point["ts"] for point in points]
    cutoffs = parse_cutoffs(args.cutoffs)
    if args.auto_cutoffs is not None:
        cutoffs = auto_cutoffs(timestamps, args.auto_cutoffs)
    if not cutoffs:
        raise ValueError("provide --cutoffs or --auto-cutoffs")

    splits = make_walk_forward_splits(timestamps, cutoffs)
    ticker = args.ticker or history.get("ticker")
    for split in splits:
        cutoff = split["cutoff"]
        visible_end = split["visible"]["end"]
        split["prediction_lock"] = {
            "cutoff": cutoff,
            "ticker": ticker,
            "side": args.side,
            "predicted_probability": None,
            "confidence": None,
            "rationale": None,
            "predicted_catalysts": [],
            "locked": False,
            "locked_at": None,
            "market_mid_at_cutoff": points[visible_end]["yes_dollars"],
        }
    return {
        "ticker": ticker,
        "source_history": str(args.history),
        "anti_overfit_contract": {
            "chronological_splits_only": True,
            "hidden_future_excluded_at_cutoff": True,
            "locked_at_required_before_scoring": True,
            "no_retuning_after_outcomes": True,
        },
        "splits": splits,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create walk-forward prediction-lock skeletons.")
    parser.add_argument("history", help="Normalized history JSON from parse_kalshi_history.py")
    parser.add_argument("--cutoffs", help="Comma-separated ISO cutoffs")
    parser.add_argument("--auto-cutoffs", type=int, help="Create N evenly spaced cutoffs")
    parser.add_argument("--ticker", help="Override market ticker")
    parser.add_argument("--side", default="YES", choices=("YES", "NO"), help="Prediction side")
    parser.add_argument("--output", help="Output JSON path; defaults to stdout")
    args = parser.parse_args(argv)

    try:
        payload = build_payload(args)
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
