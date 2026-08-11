"""
Probability math helpers for transparent prediction workflows.
"""
import math
from collections.abc import Iterable


EPSILON = 1e-9


def clamp_probability(probability: float, epsilon: float = EPSILON) -> float:
    """Clamp probability away from exact 0/1 for logit math."""
    return max(epsilon, min(1.0 - epsilon, float(probability)))


def logit(probability: float) -> float:
    """Convert probability to log-odds."""
    p = clamp_probability(probability)
    return math.log(p / (1.0 - p))


def inverse_logit(value: float) -> float:
    """Convert log-odds to probability."""
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def apply_logit_factors(base_probability: float, factor_weights: Iterable[float]) -> float:
    """Apply sourced factor weights in log-odds space."""
    return inverse_logit(logit(base_probability) + sum(float(w) for w in factor_weights))


def probability_impact(base_probability: float, changed_probability: float) -> float:
    """Return the measured impact of a model perturbation in probability points."""
    return float(changed_probability) - float(base_probability)


def independent_combo_probability(probabilities: Iterable[float]) -> float:
    """Multiply independent leg probabilities."""
    probability = 1.0
    for leg_probability in probabilities:
        probability *= clamp_probability(leg_probability)
    return probability
