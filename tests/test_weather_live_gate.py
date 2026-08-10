import server
from config.settings import settings


class FakeLogger:
    def __init__(self, summary):
        self.summary = summary

    def get_weather_validation_summary(self):
        return self.summary


def test_weather_live_gate_blocks_paper_only(monkeypatch):
    monkeypatch.setattr(settings, "allow_weather_live_before_validation", False)
    monkeypatch.setattr(
        server,
        "logger",
        FakeLogger({
            "readiness": "PAPER_ONLY",
            "resolved_count": 0,
            "live_remaining": 50,
        }),
    )

    error = server.weather_live_validation_error("weather_live")

    assert error is not None
    assert error["readiness"] == "PAPER_ONLY"
    assert error["resolved_weather_trades"] == 0
    assert "weather_live is blocked" in error["error"]


def test_weather_live_gate_allows_live_review(monkeypatch):
    monkeypatch.setattr(settings, "allow_weather_live_before_validation", False)
    monkeypatch.setattr(
        server,
        "logger",
        FakeLogger({
            "readiness": "LIVE_REVIEW",
            "resolved_count": 50,
            "live_remaining": 0,
        }),
    )

    assert server.weather_live_validation_error("weather_live") is None


def test_weather_live_gate_allows_explicit_override(monkeypatch):
    monkeypatch.setattr(settings, "allow_weather_live_before_validation", True)
    monkeypatch.setattr(
        server,
        "logger",
        FakeLogger({
            "readiness": "PAPER_ONLY",
            "resolved_count": 0,
            "live_remaining": 50,
        }),
    )

    assert server.weather_live_validation_error("weather_live") is None
