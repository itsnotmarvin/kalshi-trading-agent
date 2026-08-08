#!/usr/bin/env python3
"""
Score logged weather forecasts against Kalshi settlements.

Usage (from kalshi-agent/):
    python3 scripts/score_weather.py

Reads data/weather_forecast_log.jsonl (written by WeatherEngine for every
analyzed market, traded or not), fetches settlement results — surviving the
404 that settled markets return — and reports model-vs-market Brier scores
and the Brier Skill Score. Writes data/weather_skill_report.json.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.kalshi_adapter import KalshiAdapter  # noqa: E402
from core.weather_skill import latest_per_market, load_forecasts, score_forecasts  # noqa: E402

FORECAST_LOG = Path("data/weather_forecast_log.jsonl")
REPORT_PATH = Path("data/weather_skill_report.json")


async def main() -> int:
    forecasts = latest_per_market(load_forecasts(FORECAST_LOG))
    if not forecasts:
        print(f"No forecasts logged yet in {FORECAST_LOG} — run the weather scanner first.")
        return 1

    adapter = KalshiAdapter()
    outcomes: dict[str, str] = {}
    for i, market_id in enumerate(sorted(forecasts), start=1):
        try:
            result = await adapter.get_market_result(market_id)
        except Exception as e:
            print(f"  ⚠️  {market_id}: result lookup failed ({e})")
            continue
        if result in ("yes", "no"):
            outcomes[market_id] = result
        if i % 25 == 0:
            print(f"  … checked {i}/{len(forecasts)} markets ({len(outcomes)} resolved)")

    report = score_forecasts(forecasts, outcomes)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    overall = report["overall"]
    print(f"\nWeather skill report ({report['n_resolved']} resolved of {report['n_forecasts']} forecasted markets)")
    if overall.get("n"):
        print(f"  Model  Brier: {overall['model_brier']:.4f}   log-loss: {overall['model_log_loss']:.4f}")
        print(f"  Market Brier: {overall['market_brier']:.4f}   log-loss: {overall['market_log_loss']:.4f}")
        bss = overall["brier_skill_score"]
        verdict = "model BEATS the market price" if bss and bss > 0 else "model does NOT beat the market price"
        print(f"  Brier Skill Score: {bss:+.4f} → {verdict}")
        for section in ("by_city", "by_variable"):
            rows = report[section]
            if rows:
                print(f"  {section}:")
                for name, stats in rows.items():
                    if stats.get("n"):
                        print(f"    {name:<16} n={stats['n']:<4} BSS={stats['brier_skill_score']:+.4f}")
    else:
        print("  No resolved markets yet — rerun after settlements.")
    print(f"\nFull report written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
