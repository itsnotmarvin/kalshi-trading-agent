"""Shared pytest fixtures."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_daily_breaker_state():
    """
    Circuit-breaker state persists across restarts by design
    (data/daily_stats_state.json). Tests construct fresh RiskManager
    instances and must not inherit halts or counters recorded by an
    earlier test, so clear the state file around every test.
    """
    state_file = Path("data/daily_stats_state.json")
    state_file.unlink(missing_ok=True)
    yield
    state_file.unlink(missing_ok=True)
