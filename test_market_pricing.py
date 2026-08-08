from core.market_pricing import (
    edge,
    executable_no_cost,
    executable_yes_cost,
    implied_prob_mid,
)


def test_probability_gap_known_value():
    assert abs(edge(0.58, 0.52) - 0.06) < 1e-9


def test_midpoint_is_separate_from_executable_ask_cost():
    assert implied_prob_mid(48, 54) == 0.51
    assert executable_yes_cost(54) == 0.54
    assert executable_no_cost(48) == 0.52
    assert implied_prob_mid(48, 54) != executable_yes_cost(54)
