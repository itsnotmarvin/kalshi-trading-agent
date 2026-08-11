"""
Weather forecast skill scoring.

Answers the one question that decides whether the weather engine deserves
capital: does the model beat the market it is trading against?

Joins the selection-bias-free forecast log (every analyzed market, traded or
not — see WeatherEngine._log_forecast) against Kalshi settlement results and
computes the Brier score of the model versus the Brier score of the market
midpoint, summarized as a Brier Skill Score:

    BSS = 1 − Brier(model) / Brier(market)

BSS > 0 means the model beat the midpoint baseline on the scored sample. That
is evidence about forecast quality, not proof of executable trading profit.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def load_forecasts(path: Path) -> list[dict]:
    """Load forecast records from the JSONL log, skipping malformed lines."""
    records = []
    if not path.exists():
        return records
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def latest_per_market(records: list[dict]) -> dict[str, dict]:
    """
    Keep the most recent forecast per market.

    The engine re-analyzes markets every cycle. Callers must exclude records at
    or after resolution before using this helper; it cannot infer settlement
    cutoffs from a forecast row alone.
    """
    latest: dict[str, dict] = {}
    for record in records:
        market_id = record.get("market_id")
        if not market_id:
            continue
        prior = latest.get(market_id)
        if prior is None or (record.get("ts") or "") >= (prior.get("ts") or ""):
            latest[market_id] = record
    return latest


def _brier(prob: float, outcome: int) -> float:
    return (prob - outcome) ** 2


def _log_loss(prob: float, outcome: int) -> float:
    clipped = min(max(prob, 1e-6), 1.0 - 1e-6)
    return -math.log(clipped if outcome == 1 else 1.0 - clipped)


def score_forecasts(forecasts: dict[str, dict], outcomes: dict[str, str]) -> dict:
    """
    Score model probabilities against market midpoints on resolved markets.

    forecasts: market_id → forecast record (from latest_per_market)
    outcomes:  market_id → "yes" / "no" settlement result

    Returns a report dict with overall and per-city / per-variable breakdowns.
    """
    scored: list[dict] = []
    for market_id, record in forecasts.items():
        result = (outcomes.get(market_id) or "").lower()
        if result not in ("yes", "no"):
            continue
        prob = record.get("probability")
        price = record.get("market_yes_midpoint")
        if price is None:
            yes_ask = record.get("market_yes_price")
            no_ask = record.get("market_no_price")
            if yes_ask is not None and no_ask is not None:
                price = (float(yes_ask) + (1.0 - float(no_ask))) / 2.0
        if prob is None or price is None:
            continue
        outcome = 1 if result == "yes" else 0
        scored.append({
            "market_id": market_id,
            "city": record.get("city") or "unknown",
            "variable": record.get("variable") or "unknown",
            "outcome": outcome,
            "model_brier": _brier(float(prob), outcome),
            "market_brier": _brier(float(price), outcome),
            "model_log_loss": _log_loss(float(prob), outcome),
            "market_log_loss": _log_loss(float(price), outcome),
        })

    def _summarize(rows: list[dict]) -> dict:
        n = len(rows)
        if n == 0:
            return {"n": 0}
        model_brier = sum(r["model_brier"] for r in rows) / n
        market_brier = sum(r["market_brier"] for r in rows) / n
        return {
            "n": n,
            "model_brier": round(model_brier, 6),
            "market_brier": round(market_brier, 6),
            "brier_skill_score": (
                round(1.0 - model_brier / market_brier, 6) if market_brier > 0 else None
            ),
            "model_log_loss": round(sum(r["model_log_loss"] for r in rows) / n, 6),
            "market_log_loss": round(sum(r["market_log_loss"] for r in rows) / n, 6),
        }

    by_city: dict[str, dict] = {}
    by_variable: dict[str, dict] = {}
    for key, selector in (("city", by_city), ("variable", by_variable)):
        groups: dict[str, list[dict]] = {}
        for row in scored:
            groups.setdefault(row[key], []).append(row)
        for name, rows in sorted(groups.items()):
            selector[name] = _summarize(rows)

    return {
        "n_forecasts": len(forecasts),
        "n_resolved": len(scored),
        "overall": _summarize(scored),
        "by_city": by_city,
        "by_variable": by_variable,
    }
