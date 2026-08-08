"""
Probability and quote helpers for the World Cup Kalshi route.

Kalshi remains responsible for fee, payout, and final order-preview math.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


CONFIDENCE_MULTIPLIERS = {
    "LOW": 0.0,
    "MEDIUM": 0.50,
    "HIGH": 0.75,
    "VERY_HIGH": 1.0,
}

CONFIDENCE_ORDER = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "VERY_HIGH": 3,
}

EPSILON = 1e-9


@dataclass(frozen=True)
class QuoteSnapshot:
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    side: str
    executable_price: float | None
    side_spread: float | None
    side_depth_contracts: float
    side_depth_dollars: float
    depth_covers_quantity: bool


@dataclass(frozen=True)
class ProbabilitySignal:
    side: str
    model_yes_probability: float
    model_side_probability: float
    market_side_probability: float
    probability_gap: float
    confidence: str
    confidence_multiplier: float
    confidence_adjusted_probability_gap: float


def clamp_probability(value: float, epsilon: float = EPSILON) -> float:
    return max(epsilon, min(1.0 - epsilon, float(value)))


def normalize_midpoint_probabilities(midpoints: dict[str, float]) -> dict[str, float]:
    clean = {key: max(0.0, float(value)) for key, value in midpoints.items()}
    total = sum(clean.values())
    if total <= 0:
        return {key: 0.0 for key in clean}
    return {key: value / total for key, value in clean.items()}


def confidence_multiplier(confidence: str) -> float:
    return CONFIDENCE_MULTIPLIERS.get((confidence or "").upper(), 0.0)


def confidence_at_least(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_ORDER.get((confidence or "").upper(), -1) >= CONFIDENCE_ORDER.get(minimum.upper(), 999)


def parse_orderbook_levels(levels: Iterable | None) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        try:
            price = float(level[0])
            count = float(level[1])
        except (TypeError, ValueError):
            continue
        if price > 1.0:
            price = price / 100.0
        if 0.0 <= price <= 1.0 and count > 0:
            parsed.append((price, count))
    parsed.sort(key=lambda item: item[0])
    return parsed


def best_bid(levels: list[tuple[float, float]]) -> float | None:
    return levels[-1][0] if levels else None


def derive_quote_snapshot(orderbook: dict, side: str, quantity: float) -> QuoteSnapshot:
    side_upper = side.upper()
    yes_levels = parse_orderbook_levels(orderbook.get("yes") or orderbook.get("yes_dollars"))
    no_levels = parse_orderbook_levels(orderbook.get("no") or orderbook.get("no_dollars"))

    yes_bid = best_bid(yes_levels)
    no_bid = best_bid(no_levels)
    yes_ask = round(1.0 - no_bid, 10) if no_bid is not None else None
    no_ask = round(1.0 - yes_bid, 10) if yes_bid is not None else None

    if side_upper == "YES":
        executable_price = yes_ask
        spread = yes_ask - yes_bid if yes_ask is not None and yes_bid is not None else None
        depth = no_levels[-1][1] if no_levels else 0.0
    elif side_upper == "NO":
        executable_price = no_ask
        spread = no_ask - no_bid if no_ask is not None and no_bid is not None else None
        depth = yes_levels[-1][1] if yes_levels else 0.0
    else:
        executable_price = None
        spread = None
        depth = 0.0

    depth_dollars = depth * executable_price if executable_price is not None else 0.0
    covered = executable_price is not None and quantity > 0 and depth >= quantity

    return QuoteSnapshot(
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        side=side_upper,
        executable_price=executable_price,
        side_spread=spread,
        side_depth_contracts=depth,
        side_depth_dollars=depth_dollars,
        depth_covers_quantity=covered,
    )


def calculate_probability_signal(
    *,
    model_yes_probability: float,
    market_yes_probability: float,
    side: str,
    confidence: str,
) -> ProbabilitySignal:
    side_upper = side.upper()
    if side_upper not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    model_yes = clamp_probability(model_yes_probability)
    market_yes = clamp_probability(market_yes_probability)
    model_side = model_yes if side_upper == "YES" else 1.0 - model_yes
    market_side = market_yes if side_upper == "YES" else 1.0 - market_yes
    gap = model_side - market_side
    multiplier = confidence_multiplier(confidence)
    return ProbabilitySignal(
        side=side_upper,
        model_yes_probability=model_yes,
        model_side_probability=model_side,
        market_side_probability=market_side,
        probability_gap=gap,
        confidence=confidence.upper(),
        confidence_multiplier=multiplier,
        confidence_adjusted_probability_gap=gap * multiplier,
    )


def poisson_probability(lam: float, goals: int) -> float:
    if goals < 0:
        return 0.0
    lam = max(0.0, lam)
    return math.exp(-lam) * (lam ** goals) / math.factorial(goals)


def poisson_score_distribution(home_xg: float, away_xg: float, max_goals: int = 10) -> dict[tuple[int, int], float]:
    distribution: dict[tuple[int, int], float] = {}
    for home_goals in range(max_goals + 1):
        home_prob = poisson_probability(home_xg, home_goals)
        for away_goals in range(max_goals + 1):
            distribution[(home_goals, away_goals)] = home_prob * poisson_probability(away_xg, away_goals)
    total = sum(distribution.values())
    if total > 0:
        distribution = {score: probability / total for score, probability in distribution.items()}
    return distribution


def derived_soccer_probabilities(distribution: dict[tuple[int, int], float]) -> dict[str, float]:
    home_win = sum(prob for (home, away), prob in distribution.items() if home > away)
    draw = sum(prob for (home, away), prob in distribution.items() if home == away)
    away_win = sum(prob for (home, away), prob in distribution.items() if home < away)
    over_2_5 = sum(prob for (home, away), prob in distribution.items() if home + away >= 3)
    both_score = sum(prob for (home, away), prob in distribution.items() if home > 0 and away > 0)
    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "over_2_5": over_2_5,
        "both_teams_score": both_score,
    }
