"""Kalshi quote, fee, and probability-comparison helpers.

Fees are charged by Kalshi on every trade execution (not on settlement):
fee = ceil_to_cent(rate × contracts × price × (1 − price)), with a lower
maker rate for resting post-only orders. Both the risk gate and realized
P&L must account for them — an "edge" smaller than the fee is not an edge.
"""
from __future__ import annotations

import math

TAKER_FEE_RATE = 0.07
MAKER_FEE_RATE = 0.0175


def kalshi_trading_fee(contracts: float, price_dollars: float, *, maker: bool = False) -> float:
    """
    Return the Kalshi trading fee in dollars for an order.

    Kalshi charges rate × C × P × (1 − P), rounded UP to the next cent,
    on execution. Settlement itself is fee-free.
    """
    if contracts <= 0 or not (0.0 < price_dollars < 1.0):
        return 0.0
    rate = MAKER_FEE_RATE if maker else TAKER_FEE_RATE
    raw = rate * contracts * price_dollars * (1.0 - price_dollars)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def fee_fraction_per_contract(price_dollars: float, *, maker: bool = False) -> float:
    """
    Return the expected fee per contract as a fraction of the $1 payout.

    This is the continuous (un-ceiled) form used by the decision gate, where
    probabilities and edges are already per-$1 fractions.
    """
    if not (0.0 < price_dollars < 1.0):
        return 0.0
    rate = MAKER_FEE_RATE if maker else TAKER_FEE_RATE
    return rate * price_dollars * (1.0 - price_dollars)


def _cents_to_dollars(cents: float) -> float:
    value = float(cents) / 100.0
    if value < 0.0 or value > 1.0:
        raise ValueError("price cents must be between 0 and 100")
    return value


def implied_prob_mid(yes_bid_cents: float, yes_ask_cents: float) -> float:
    """
    Return the YES midpoint as a market-belief estimate.

    The midpoint is a market-belief estimate. The ask is executable cost
    including spread; never call the ask the market probability.
    """
    bid = _cents_to_dollars(yes_bid_cents)
    ask = _cents_to_dollars(yes_ask_cents)
    if ask < bid:
        raise ValueError("yes ask must be greater than or equal to yes bid")
    return (bid + ask) / 2.0


def executable_yes_cost(yes_ask_cents: float) -> float:
    """
    Return executable YES cost in dollars from the YES ask.

    The ask is executable cost including spread; never call the ask the market
    probability. Use implied_prob_mid for a market-belief estimate.
    """
    return _cents_to_dollars(yes_ask_cents)


def executable_no_cost(yes_bid_cents: float) -> float:
    """
    Return executable NO cost in dollars from the YES bid.

    A NO buyer pays the implied NO ask, 1 - YES bid. This is executable cost
    including spread; never call the ask the market probability. Use the YES
    midpoint for a market-belief estimate.
    """
    return 1.0 - _cents_to_dollars(yes_bid_cents)


def edge(estimated_probability: float, implied_probability: float) -> float:
    """Return probability edge versus the market-belief midpoint."""
    return float(estimated_probability) - float(implied_probability)
