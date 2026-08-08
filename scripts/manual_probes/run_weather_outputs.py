#!/usr/bin/env python3
"""
Read-only weather model runner.

Fetches Kalshi weather markets, runs WeatherEngine.analyze_market(), and prints
model outputs only. This script does not place orders, create paper positions,
write trade logs, or call RiskManager execution helpers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.kalshi_adapter import KalshiAdapter
from core.weather_engine import WeatherEngine


@dataclass
class WeatherOutput:
    market_id: str
    question: str
    end_date: str | None
    days_out: int | None
    yes_price: float
    no_price: float
    city: str
    variable: str
    threshold: float
    direction_threshold: str
    forecast_hour: str
    raw_probability: float
    ensemble_probability: float
    final_probability: float
    confidence: float
    edge: float
    model_direction: str
    output_signal: str
    should_trade_model: bool
    should_trade_after_timing_rules: bool
    ensemble_mean: float
    ensemble_spread: float
    member_count: int
    forecaster_insight: str
    timing_note: str | None
    sources: list[str]


class NoopForecaster:
    """Keeps this read-only runner on statistical model output by default."""

    def get_market_insight(
        self,
        market_id: str,
        variable: str,
        threshold: float,
        above: bool,
    ) -> dict[str, float | str]:
        return {
            "modifier": 0.0,
            "reasoning": "Forecaster insight skipped by output-only runner.",
        }


def load_macro_lessons(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []

    lessons: list[str] = []
    for item in data:
        if isinstance(item, str):
            lessons.append(item)
        elif isinstance(item, dict) and item.get("status") in ("active", "probationary"):
            lesson = item.get("lesson")
            if isinstance(lesson, str):
                lessons.append(lesson)
    return lessons


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Kalshi weather model outputs without trade automation."
    )
    parser.add_argument("--limit", type=int, default=25, help="Max Kalshi weather markets to fetch")
    parser.add_argument(
        "--days-out",
        type=int,
        nargs="*",
        default=[0, 1],
        help="Target days from today in America/New_York to analyze; default: 0 1",
    )
    parser.add_argument("--min-price", type=float, default=0.10)
    parser.add_argument("--min-confidence", type=float, default=0.50)
    parser.add_argument(
        "--include-forecaster-insight",
        action="store_true",
        help="Also call NWS + Claude forecaster-insight layer.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional path to write the full output JSON.",
    )
    return parser.parse_args()


def timing_guard(result: dict[str, Any], days_out: int | None) -> tuple[bool, str | None]:
    should_trade = bool(result.get("should_trade"))
    if days_out != 1:
        return should_trade, None

    confidence = float(result.get("confidence", 0.0))
    edge = abs(float(result.get("edge", 0.0)))
    if confidence < 0.80 or edge < 0.12:
        return (
            False,
            f"Tomorrow timing rule: needs confidence >= 0.80 and edge >= 0.12; got {confidence:.2f}, {edge:.2f}.",
        )
    return should_trade, "Tomorrow timing rule passed."


def build_output(market, parsed, result: dict[str, Any], days_out: int | None) -> WeatherOutput:
    should_trade_after_timing, timing_note = timing_guard(result, days_out)
    output_signal = result.get("direction", "HOLD") if should_trade_after_timing else "HOLD"
    return WeatherOutput(
        market_id=market.id,
        question=market.question,
        end_date=market.end_date.isoformat() if market.end_date else None,
        days_out=days_out,
        yes_price=round(float(market.yes_price), 4),
        no_price=round(float(market.no_price), 4),
        city=str(result.get("city") or parsed.city),
        variable=str(result.get("variable") or parsed.variable),
        threshold=float(result.get("threshold", parsed.threshold)),
        direction_threshold="above" if bool(result.get("above", parsed.above)) else "below",
        forecast_hour=str(result.get("forecast_hour", "")),
        raw_probability=round(float(result.get("raw_probability", 0.0)), 4),
        ensemble_probability=round(float(result.get("ensemble_probability", 0.0)), 4),
        final_probability=round(float(result.get("probability", 0.0)), 4),
        confidence=round(float(result.get("confidence", 0.0)), 4),
        edge=round(float(result.get("edge", 0.0)), 4),
        model_direction=str(result.get("direction", "HOLD")),
        output_signal=output_signal,
        should_trade_model=bool(result.get("should_trade")),
        should_trade_after_timing_rules=should_trade_after_timing,
        ensemble_mean=round(float(result.get("ensemble_mean", 0.0)), 4),
        ensemble_spread=round(float(result.get("ensemble_spread", 0.0)), 4),
        member_count=int(result.get("member_count", 0)),
        forecaster_insight=str(result.get("forecaster_insight", "")),
        timing_note=timing_note,
        sources=[
            "Kalshi market API",
            "Open-Meteo GFS ensemble API",
            "Open-Meteo deterministic forecast API",
        ],
    )


def print_table(outputs: list[WeatherOutput]) -> None:
    if not outputs:
        print("No weather model outputs produced for the requested filters.")
        return

    header = (
        f"{'Signal':<8} {'Prob':>6} {'Edge':>7} {'Conf':>6} "
        f"{'Mean':>7} {'Thresh':>9} {'Price':>7}  Market"
    )
    print(header)
    print("-" * len(header))
    for output in outputs:
        threshold = f"{output.direction_threshold[:1]}{output.threshold:g}"
        print(
            f"{output.output_signal:<8} "
            f"{output.final_probability:>6.1%} "
            f"{output.edge:>7.1%} "
            f"{output.confidence:>6.1%} "
            f"{output.ensemble_mean:>7.1f} "
            f"{threshold:>9} "
            f"{output.yes_price:>7.2f}  "
            f"{output.market_id}: {output.question[:88]}"
        )


async def run() -> int:
    args = parse_args()

    lessons = load_macro_lessons(Path("data/macro_lessons.json"))
    engine = WeatherEngine(
        min_price=args.min_price,
        min_confidence=args.min_confidence,
        lessons=lessons,
    )
    if not args.include_forecaster_insight:
        engine.forecaster = NoopForecaster()

    adapter = KalshiAdapter()
    # Public market reads do not need account auth; disabling signing here avoids
    # noisy key warnings and keeps this runner clearly detached from portfolio APIs.
    adapter.api_key_id = ""
    from zoneinfo import ZoneInfo

    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    days_filter = set(args.days_out)
    outputs: list[WeatherOutput] = []

    try:
        markets = await adapter.get_weather_markets(limit=args.limit)
        print(f"Fetched {len(markets)} Kalshi weather markets.")
        print(
            "Execution disabled: this runner only emits model outputs; no orders, paper positions, or trade logs."
        )
        if not args.include_forecaster_insight:
            print("Forecaster insight disabled; use --include-forecaster-insight for NWS + Claude enrichment.")
        print()

        for market in markets:
            parsed = engine.parse_kalshi_weather_market(market)
            if not parsed:
                continue

            days_out = None
            if parsed.target_date:
                target_ny = parsed.target_date.astimezone(ZoneInfo("America/New_York")).date()
                days_out = (target_ny - today_ny).days
                if days_filter and days_out not in days_filter:
                    continue

            try:
                result = await asyncio.to_thread(engine.analyze_market, market)
            except Exception as exc:
                print(f"Analysis failed for {market.id}: {exc}")
                continue
            if not result:
                continue

            outputs.append(build_output(market, parsed, result, days_out))

    finally:
        await adapter.client.aclose()
        engine.client.close()

    outputs.sort(
        key=lambda item: (
            item.output_signal == "HOLD",
            -(abs(item.edge)),
            item.days_out if item.days_out is not None else 99,
            item.market_id,
        )
    )

    print_table(outputs)

    if args.json_out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "forecaster_insight_included": bool(args.include_forecaster_insight),
            "count": len(outputs),
            "outputs": [asdict(output) for output in outputs],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote JSON output to {args.json_out}")

    actionable = [output for output in outputs if output.output_signal != "HOLD"]
    print(f"\nOutput signals: {len(actionable)} non-HOLD, {len(outputs) - len(actionable)} HOLD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
