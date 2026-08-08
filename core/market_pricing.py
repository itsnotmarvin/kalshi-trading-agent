"""Kalshi quote and probability-comparison helpers.

Fee, payout, and checkout calculations are intentionally left to Kalshi.
"""
from __future__ import annotations

# Retained for offline historical probes that need an explicit simulation
# assumption. The live recommendation path does not calculate fees.
TAKER_FEE_RATE = 0.07


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
