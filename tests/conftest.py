"""Shared pytest fixtures."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _redirect_weather_forecast_log(tmp_path, monkeypatch):
    """
    WeatherEngine logs every analyzed market to data/weather_forecast_log.jsonl
    by design. Tests exercise analyze_market freely and must not pollute the
    real forecast log, so point every engine constructed in a test at tmp_path.
    """
    from core.weather_engine import WeatherEngine

    original_init = WeatherEngine.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.forecast_log_path = tmp_path / "weather_forecast_log.jsonl"

    monkeypatch.setattr(WeatherEngine, "__init__", patched_init)


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
