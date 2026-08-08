#!/usr/bin/env python3
"""
Normalize Kalshi chart CSV or candlestick JSON history.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def parse_timestamp(value: Any) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            numeric = float(text)
            parsed = datetime.fromtimestamp(numeric / 1000 if numeric > 10_000_000_000 else numeric, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "").removeprefix("$").removesuffix("%").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if math.isnan(parsed):
        return None
    if parsed > 1.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def find_timestamp_column(fieldnames: list[str]) -> str:
    normalized = {field.lower().strip(): field for field in fieldnames}
    for name in ("timestamp", "time", "date", "datetime", "created_at"):
        if name in normalized:
            return normalized[name]
    raise ValueError(f"missing timestamp column; available columns: {fieldnames}")


def load_csv(path: Path, column: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        timestamp_column = find_timestamp_column([field for field in reader.fieldnames if field])
        price_column = column
        if price_column is None:
            candidates = [field for field in reader.fieldnames if field and field != timestamp_column]
            if len(candidates) != 1:
                raise ValueError("--column is required when the CSV has multiple price columns")
            price_column = candidates[0]
        if price_column not in reader.fieldnames:
            raise ValueError(f"missing column {price_column!r}; available columns: {reader.fieldnames}")

        points = []
        rows = 0
        for row in reader:
            rows += 1
            price = parse_price(row.get(price_column))
            if price is None:
                continue
            points.append({"ts": parse_timestamp(row[timestamp_column]).isoformat(), "yes_dollars": price})
    return points, {"input_type": "csv", "csv_path": str(path), "timestamp_column": timestamp_column, "price_column": price_column, "raw_rows": rows}


def pick_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def load_candlesticks(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text())
    candles = payload.get("candlesticks", payload) if isinstance(payload, dict) else payload
    if not isinstance(candles, list):
        raise ValueError("candlesticks JSON must be a list or an object with candlesticks")

    points = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        ts = pick_first(candle, ("ts", "timestamp", "time", "end_period_ts", "start_period_ts", "period_start_ts"))
        price = pick_first(candle, ("yes_dollars", "yes_price", "yes", "price", "close", "yes_close"))
        if price is None and isinstance(candle.get("yes_bid"), dict):
            price = candle["yes_bid"].get("close")
        if isinstance(price, dict):
            # Adapter candlesticks nest OHLC dicts, e.g. price.close_dollars.
            price = pick_first(price, ("close_dollars", "close", "mean_dollars", "mean", "open_dollars", "open"))
        parsed_price = parse_price(price)
        if ts is not None and parsed_price is not None:
            points.append({"ts": parse_timestamp(ts).isoformat(), "yes_dollars": parsed_price})
    return points, {"input_type": "candlesticks", "json_path": str(path), "raw_rows": len(candles)}


def value_at_or_before(points: list[dict[str, Any]], target: datetime) -> float | None:
    eligible = [point for point in points if parse_timestamp(point["ts"]) <= target]
    return eligible[-1]["yes_dollars"] if eligible else None


def summarize(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        raise ValueError("no parseable price points")
    points = sorted(points, key=lambda point: point["ts"])
    current = points[-1]["yes_dollars"]
    current_ts = parse_timestamp(points[-1]["ts"])
    prices = [point["yes_dollars"] for point in points]
    deltas = [prices[index] - prices[index - 1] for index in range(1, len(prices))]

    def change(days: int) -> float | None:
        previous = value_at_or_before(points, current_ts - timedelta(days=days))
        return round(current - previous, 4) if previous is not None else None

    inflections = []
    previous_sign = 0
    for index, delta in enumerate(deltas, start=1):
        sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if sign and previous_sign and sign != previous_sign:
            inflections.append({"ts": points[index]["ts"], "yes_dollars": prices[index], "direction": "up" if sign > 0 else "down"})
        if sign:
            previous_sign = sign

    net = prices[-1] - prices[0]
    if abs(net) < 0.01:
        trend = "flat"
    elif net > 0:
        trend = "up"
    else:
        trend = "down"

    return {
        "current": round(current, 4),
        "change_24h": change(1),
        "change_7d": change(7),
        "change_30d": change(30),
        "largest_spike": round(max(deltas), 4) if deltas else 0.0,
        "largest_drop": round(min(deltas), 4) if deltas else 0.0,
        "realized_range": round(max(prices) - min(prices), 4),
        "trend_label": trend,
        "inflection_points": inflections,
    }


def normalize_history(args: argparse.Namespace) -> dict[str, Any]:
    if args.candlesticks:
        points, source = load_candlesticks(Path(args.candlesticks))
    elif args.input:
        points, source = load_csv(Path(args.input), args.column)
    else:
        raise ValueError("provide a CSV input path or --candlesticks JSON")
    points = sorted(points, key=lambda point: point["ts"])
    return {
        "ticker": args.ticker,
        "points": points,
        "source": source,
        "explanatory_only": {"summary": summarize(points)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Kalshi chart history into skill JSON.")
    parser.add_argument("input", nargs="?", help="Kalshi chart CSV path")
    parser.add_argument("--column", help="CSV YES cents column to parse")
    parser.add_argument("--candlesticks", help="Candlesticks JSON path from get_market_candlesticks")
    parser.add_argument("--ticker", default=None, help="Market ticker to copy into output")
    parser.add_argument("--output", help="Output JSON path; defaults to stdout")
    args = parser.parse_args(argv)

    try:
        payload = normalize_history(args)
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
