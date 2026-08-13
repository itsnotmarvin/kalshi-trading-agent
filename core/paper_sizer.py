"""Deterministic size ladder for explicitly indexed paper experiments.

This module is not wired into the normal paper-trading path (which uses the
configured minimum stakes). Callers must supply a persisted observation index
so an experiment can be reproduced exactly; randomized sizing would make
experimental paper P&L unrepeatable.
"""

EXPLORATORY_SIZE_LADDER_USD = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0,
    500.0, 1000.0, 2500.0, 5000.0, 10000.0,
)


def get_exploratory_size(
    min_usd: float = 5.0,
    max_usd: float = 10000.0,
    *,
    observation_index: int = 0,
) -> float:
    """Return the indexed ladder value within the requested inclusive bounds."""
    if min_usd <= 0 or max_usd < min_usd:
        raise ValueError("size bounds must satisfy 0 < min_usd <= max_usd")
    ladder = tuple(
        size for size in EXPLORATORY_SIZE_LADDER_USD
        if min_usd <= size <= max_usd
    )
    if not ladder:
        raise ValueError("no configured ladder size falls within the requested bounds")
    return ladder[observation_index % len(ladder)]
