from core.world_cup_math import (
    calculate_probability_signal,
    derive_quote_snapshot,
    derived_soccer_probabilities,
    normalize_midpoint_probabilities,
    poisson_score_distribution,
)


def test_normalize_midpoint_probabilities():
    normalized = normalize_midpoint_probabilities({"a": 0.60, "b": 0.30, "c": 0.30})

    assert round(sum(normalized.values()), 10) == 1.0
    assert normalized["a"] == 0.5
    assert normalized["b"] == 0.25


def test_orderbook_bid_only_derives_executable_asks_and_depth():
    quote = derive_quote_snapshot(
        {"yes": [["0.42", "10"], ["0.46", "4"]], "no": [["0.48", "3"], ["0.55", "2"]]},
        "YES",
        quantity=2,
    )

    assert quote.yes_bid == 0.46
    assert quote.yes_ask == 0.45
    assert abs(quote.side_spread + 0.01) < 1e-9
    assert quote.depth_covers_quantity is True


def test_orderbook_quote_uses_top_level_depth_only():
    quote = derive_quote_snapshot(
        {"yes": [["0.20", "20"]], "no": [["0.55", "2"], ["0.50", "3"]]},
        "YES",
        quantity=5,
    )

    assert quote.yes_ask == 0.45
    assert quote.depth_covers_quantity is False
    assert quote.side_depth_contracts == 2


def test_yes_no_probability_signals():
    yes = calculate_probability_signal(
        model_yes_probability=0.60,
        market_yes_probability=0.50,
        side="YES",
        confidence="HIGH",
    )
    no = calculate_probability_signal(
        model_yes_probability=0.30,
        market_yes_probability=0.40,
        side="NO",
        confidence="HIGH",
    )

    assert abs(yes.probability_gap - 0.10) < 1e-9
    assert abs(yes.confidence_adjusted_probability_gap - 0.075) < 1e-9
    assert abs(no.probability_gap - 0.10) < 1e-9
    assert no.model_side_probability == 0.70


def test_poisson_soccer_distribution_derives_market_probabilities():
    distribution = poisson_score_distribution(1.4, 1.0, max_goals=8)
    probs = derived_soccer_probabilities(distribution)

    assert abs(sum(distribution.values()) - 1.0) < 1e-9
    assert 0 < probs["home_win"] < 1
    assert 0 < probs["over_2_5"] < 1
