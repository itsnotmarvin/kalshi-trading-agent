#!/usr/bin/env python3
"""Offline market-residual analysis and weather proxy v2 backtest."""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_weather import (
    brier_score,
    build_walk_forward_folds,
    score_prediction_set,
    walk_forward_predictions,
)
from scripts.backtest_weather_v2 import walk_forward_v2_predictions

POOLED_REFERENCE_BRIER = 0.0986
NOTABLE_EXCESS = 0.02


def price_bucket(price: float) -> str:
    if not 0.0 <= float(price) <= 1.0:
        raise ValueError("price must be between zero and one")
    if price < 0.10:
        return "longshot 0-10c"
    if price < 0.40:
        return "mid 10-40c"
    if price < 0.60:
        return "40-60c"
    return "favorite 60c+"


def price_age_bucket(minutes: float) -> str:
    value = float(minutes)
    if value < 0:
        raise ValueError("price age cannot be negative")
    if value == 0:
        return "0m"
    if value <= 30:
        return "(0,30]m"
    if value <= 60:
        return "(30,60]m"
    return "(60,120]m"


def strike_distance(row: dict[str, Any]) -> float:
    forecast = float(row["forecast_daily_high_f"])
    boundaries = [
        float(value)
        for value in (row.get("floor_strike"), row.get("cap_strike"))
        if value is not None
    ]
    if not boundaries:
        raise ValueError("row has no strike boundary")
    return min(abs(boundary - forecast) for boundary in boundaries)


def strike_distance_bucket(distance: float) -> str:
    value = float(distance)
    if value < 0:
        raise ValueError("strike distance cannot be negative")
    if value < 1:
        return "[0,1)F"
    if value < 3:
        return "[1,3)F"
    if value < 5:
        return "[3,5)F"
    if value < 10:
        return "[5,10)F"
    return "10F+"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def horizon_one_complete(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("horizon_days") == 1
        and row.get("forecast_daily_high_f") is not None
        and row.get("market_mid") is not None
        and row.get("result_yes") is not None
        and row.get("forecast_implied_yes") is not None
    ]


def walk_forward_cohort(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select precisely the rows scored by the existing expanding-fold protocol."""
    ordered, folds = build_walk_forward_folds(horizon_one_complete(rows))
    return [
        ordered[index]
        for fold in folds
        for index in range(fold["test_start"], fold["test_end"] + 1)
    ]


def _cluster_ci_for_cell(
    rows: Sequence[dict[str, Any]],
    cell_rows: Sequence[dict[str, Any]],
    *,
    resamples: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """Paired cluster CI for cell midpoint Brier minus pooled midpoint Brier."""
    if not rows or not cell_rows or resamples <= 0:
        return None, None
    membership = {id(row) for row in cell_rows}
    clusters: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"all_sum": 0.0, "all_n": 0.0, "cell_sum": 0.0, "cell_n": 0.0}
    )
    for row in rows:
        key = (str(row["station_id"]), str(row["target_date"]))
        squared_error = (float(row["market_mid"]) - int(row["result_yes"])) ** 2
        clusters[key]["all_sum"] += squared_error
        clusters[key]["all_n"] += 1
        if id(row) in membership:
            clusters[key]["cell_sum"] += squared_error
            clusters[key]["cell_n"] += 1
    keys = list(clusters)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(resamples):
        totals = {"all_sum": 0.0, "all_n": 0.0, "cell_sum": 0.0, "cell_n": 0.0}
        for key in rng.choices(keys, k=len(keys)):
            for name, value in clusters[key].items():
                totals[name] += value
        if totals["cell_n"]:
            samples.append(
                totals["cell_sum"] / totals["cell_n"]
                - totals["all_sum"] / totals["all_n"]
            )
    if not samples:
        return None, None
    samples.sort()
    low = samples[max(0, math.floor(0.025 * (len(samples) - 1)))]
    high = samples[min(len(samples) - 1, math.ceil(0.975 * (len(samples) - 1)))]
    return low, high


def summarize_dimension(
    rows: Sequence[dict[str, Any]],
    dimension: str,
    classifier: Callable[[dict[str, Any]], str],
    *,
    bootstrap_resamples: int,
    seed: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[classifier(row)].append(row)
    output = []
    for index, (cell, cell_rows) in enumerate(sorted(grouped.items())):
        errors = [float(row["market_mid"]) - int(row["result_yes"]) for row in cell_rows]
        midpoint_brier = sum(error**2 for error in errors) / len(errors)
        notable = len(cell_rows) >= 50 and midpoint_brier >= POOLED_REFERENCE_BRIER + NOTABLE_EXCESS
        ci_low, ci_high = (None, None)
        if notable:
            ci_low, ci_high = _cluster_ci_for_cell(
                rows,
                cell_rows,
                resamples=bootstrap_resamples,
                seed=seed + index,
            )
        output.append(
            {
                "dimension": dimension,
                "cell": cell,
                "n": len(cell_rows),
                "mean_midpoint": sum(float(row["market_mid"]) for row in cell_rows) / len(cell_rows),
                "observed_yes": sum(int(row["result_yes"]) for row in cell_rows) / len(cell_rows),
                "signed_error": sum(errors) / len(errors),
                "brier": midpoint_brier,
                "notable": notable,
                "excess_ci_low": ci_low,
                "excess_ci_high": ci_high,
            }
        )
    return output


def residual_tables(
    rows: Sequence[dict[str, Any]], *, bootstrap_resamples: int
) -> dict[str, list[dict[str, Any]]]:
    classifiers: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("Station", lambda row: str(row["station_id"])),
        ("Month", lambda row: date.fromisoformat(str(row["target_date"])).strftime("%Y-%m")),
        ("Strike distance", lambda row: strike_distance_bucket(strike_distance(row))),
        ("Price bucket", lambda row: price_bucket(float(row["market_mid"]))),
        ("Price age", lambda row: price_age_bucket(float(row["price_age_minutes"]))),
        ("Day of week", lambda row: date.fromisoformat(str(row["target_date"])).strftime("%A")),
    ]
    return {
        name: summarize_dimension(
            rows,
            name,
            classifier,
            bootstrap_resamples=bootstrap_resamples,
            seed=20260813 + index * 1000,
        )
        for index, (name, classifier) in enumerate(classifiers)
    }


def _fmt(value: Any, digits: int = 4) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _residual_table_lines(cells: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in cells:
        flag = "notable" if item["notable"] else ""
        lines.append(
            f"| {item['cell']} | {item['n']} | {_fmt(item['mean_midpoint'])} | "
            f"{_fmt(item['observed_yes'])} | {_fmt(item['signed_error'])} | "
            f"{_fmt(item['brier'])} | {flag} |"
        )
    return lines


def render_markdown(
    cohort: Sequence[dict[str, Any]],
    tables: dict[str, list[dict[str, Any]]],
    scoring: dict[str, dict[str, Any]],
    *,
    bootstrap_resamples: int,
) -> str:
    outcomes = [int(row["result_yes"]) for row in cohort]
    market = [float(row["market_mid"]) for row in cohort]
    pooled_brier = brier_score(market, outcomes)
    pooled_signed = sum(p - y for p, y in zip(market, outcomes)) / len(cohort)
    lines = [
        "# Market Residuals and Weather Proxy v2",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
        "",
        "## 1. Market midpoint residual analysis",
        "",
        f"This uses the same `{len(cohort)}` horizon-1 rows scored out of sample by the "
        "existing walk-forward protocol. The market midpoint is treated as the forecaster. "
        "Signed error is `market_mid - result_yes`, so positive values mean YES was "
        "overpredicted. For between strikes, distance is to the nearer boundary.",
        "",
        f"Pooled midpoint Brier is **{pooled_brier:.4f}** and pooled signed error is "
        f"**{pooled_signed:+.4f}**. A cell is flagged only when `N >= 50` and its point "
        f"Brier is at least `{NOTABLE_EXCESS:.2f}` worse than the pre-existing pooled "
        f"reference `{POOLED_REFERENCE_BRIER:.4f}`.",
        "",
    ]
    for dimension, cells in tables.items():
        lines.extend([f"### {dimension}", "", *_residual_table_lines(cells), ""])

    highlighted = [item for cells in tables.values() for item in cells if item["notable"]]
    highlighted.sort(key=lambda item: item["brier"], reverse=True)
    lines.extend(
        [
            "### Highlighted cells and cluster uncertainty",
            "",
            f"The intervals below use `{bootstrap_resamples}` paired resamples of "
            "`(station_id, target_date)` clusters and estimate **cell Brier minus pooled "
            "Brier**. They account for shared outcomes across strikes within a station-day.",
            "",
            "| Dimension | Cell | N | Brier | Excess vs pooled | 95% cluster CI |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in highlighted:
        excess = item["brier"] - pooled_brier
        interval = f"({_fmt(item['excess_ci_low'])}, {_fmt(item['excess_ci_high'])})"
        lines.append(
            f"| {item['dimension']} | {item['cell']} | {item['n']} | "
            f"{item['brier']:.4f} | {excess:+.4f} | {interval} |"
        )
    if not highlighted:
        lines.append("| none | none | 0 | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "Raw Brier scores are mechanically higher for contracts near 50c and strikes "
            "near the forecast because those outcomes are intrinsically less certain. Thus, "
            "the middle-price and near-strike flags locate forecast difficulty; they do not "
            "by themselves diagnose market miscalibration or an exploitable residual.",
        ]
    )

    price_cells = tables["Price bucket"]
    lines.extend(
        [
            "",
            "### Favorite-longshot diagnostic",
            "",
            "| Price bucket | N | Mean midpoint | Observed YES | Gap (midpoint - observed) |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    price_order = {"longshot 0-10c": 0, "mid 10-40c": 1, "40-60c": 2, "favorite 60c+": 3}
    for item in sorted(price_cells, key=lambda item: price_order[item["cell"]]):
        lines.append(
            f"| {item['cell']} | {item['n']} | {item['mean_midpoint']:.4f} | "
            f"{item['observed_yes']:.4f} | {item['signed_error']:+.4f} |"
        )
    lines.extend(
        [
            "",
            "The direction is consistent with the classic favorite-longshot pattern: "
            "longshot YES contracts resolved less often than their mean price, while 60c+ "
            "favorites resolved more often. The favorite cell has only 35 observations, "
            "however, and this was not a preregistered test.",
            "",
            "This is exploratory hypothesis generation, not evidence of a tradeable edge. "
            "The analysis examines dozens of overlapping cells, so some extreme point "
            "estimates are expected by chance. The CIs are not adjusted for multiple "
            "comparisons, and any pattern needs preregistration and confirmation on new data.",
            "",
            "## 2. Leakage-safe proxy iteration",
            "",
            "All variants use the identical expanding folds and 1,584-row OOS cohort. The "
            "station-bias model fits a shared sigma plus one station parameter defined as "
            "`E[forecast high - actual high]` by binary likelihood on each training fold. "
            "It does not reconstruct actual temperatures from labels. The Platt layer fits "
            "`sigmoid(a * logit(p) + b)` on training-fold predictions only, then applies it "
            "to that fold's held-out rows. Skill is midpoint Brier minus model Brier; intervals "
            f"use `{bootstrap_resamples}` station-day cluster resamples. P&L uses the existing "
            "one-contract taker policy at a strict `0.12` edge threshold.",
            "",
            "| Model | N | Model Brier | Midpoint Brier | Skill (95% cluster CI) | Trades | Net P&L |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label in ("Original zero-bias Gaussian", "Station-bias Gaussian", "Station-bias + Platt"):
        item = scoring[label]
        interval = f"{item['brier_skill']:+.4f} ({item['brier_skill_ci_low']:+.4f}, {item['brier_skill_ci_high']:+.4f})"
        lines.append(
            f"| {label} | {item['n']} | {item['model_brier']:.4f} | "
            f"{item['market_brier']:.4f} | {interval} | {item['paper']['trades']} | "
            f"${item['paper']['net_pnl']:+.2f} |"
        )
    best_label = max(scoring, key=lambda label: scoring[label]["brier_skill"])
    best = scoring[best_label]
    conclusion = "still trails" if best["brier_skill"] < 0 else "beats"
    lines.extend(
        [
            "",
            f"The best tested proxy is **{best_label}** and it {conclusion} the midpoint "
            f"on Brier skill ({best['brier_skill']:+.4f}). These are proxy-model results, "
            "not a verdict on the production ensemble route.",
            "",
            "## Limitations",
            "",
            "- The same rolling fixed-lead GFS and hourly-maximum limitations documented in the original backtest apply.",
            "- Platt calibration is fitted on in-fold training predictions from the already fitted Gaussian parameters; held-out evaluation remains OOS, but a nested calibration fit would be more conservative.",
            "- Sparse stale-price cells and overlapping slices are especially unstable.",
            "- Transaction costs and executable-side pricing are included, but this remains a historical paper policy.",
            "",
        ]
    )
    return "\n".join(lines)


def run(dataset_path: Path, output_path: Path, bootstrap_resamples: int) -> dict[str, Any]:
    rows = load_dataset(dataset_path)
    complete = horizon_one_complete(rows)
    cohort = walk_forward_cohort(rows)
    if len(cohort) != 1584:
        raise ValueError(f"expected 1,584 walk-forward rows, found {len(cohort):,}")
    tables = residual_tables(cohort, bootstrap_resamples=bootstrap_resamples)

    original = walk_forward_predictions(complete)
    bias, calibrated = walk_forward_v2_predictions(complete)
    variants = {
        "Original zero-bias Gaussian": original,
        "Station-bias Gaussian": bias,
        "Station-bias + Platt": calibrated,
    }
    scoring = {
        label: score_prediction_set(
            predicted,
            bootstrap_resamples=bootstrap_resamples,
            edge_threshold=0.12,
            seed=20260813 + index,
        )
        for index, (label, predicted) in enumerate(variants.items())
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(
            cohort,
            tables,
            scoring,
            bootstrap_resamples=bootstrap_resamples,
        )
    )
    return {"cohort": cohort, "tables": tables, "scoring": scoring}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/runtime/backtest/dataset.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("docs/market-residuals.md"))
    parser.add_argument("--bootstrap-resamples", type=int, default=1000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args.dataset, args.output, args.bootstrap_resamples)
    print(f"walk-forward cohort: {len(result['cohort'])}")
    for label, item in result["scoring"].items():
        print(
            f"{label}: Brier={item['model_brier']:.4f}, "
            f"skill={item['brier_skill']:+.4f}, trades={item['paper']['trades']}, "
            f"P&L=${item['paper']['net_pnl']:+.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
