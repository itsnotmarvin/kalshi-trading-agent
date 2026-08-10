from core.probability_math import (
    apply_logit_factors,
    independent_combo_probability,
    inverse_logit,
    logit,
    probability_impact,
)


def test_logit_round_trip():
    for probability in (0.04, 0.13, 0.50, 0.87):
        assert abs(inverse_logit(logit(probability)) - probability) < 1e-9


def test_logit_factor_update_stays_bounded():
    low_base = apply_logit_factors(0.04, [0.75, 0.50])
    high_base = apply_logit_factors(0.96, [0.75, 0.50])

    assert 0.04 < low_base < 1.0
    assert 0.96 < high_base < 1.0


def test_probability_impact_is_model_perturbation_difference():
    assert abs(probability_impact(0.42, 0.38) + 0.04) < 1e-9


def test_independent_combo_probability_multiplies_legs():
    assert abs(independent_combo_probability([0.5, 0.4, 0.25]) - 0.05) < 1e-9
