import json
from datetime import datetime, timezone, timedelta

from core.logger import TradeLogger


def write_jsonl(path, entries):
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_calibration_report_includes_execution_metrics(tmp_path):
    log_path = tmp_path / "trades.jsonl"
    entry_time = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    resolution_time = entry_time + timedelta(hours=6)
    later_resolution = entry_time + timedelta(hours=30)

    write_jsonl(
        log_path,
        [
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-high",
                "question": "Will NYC high temperature be above 80 degrees?",
                "category": "Weather",
                "direction": "BUY_YES",
                "price": 0.6,
                "size_usd": 10.0,
                "estimated_probability": 0.7,
                "edge": 0.1,
                "approved": True,
                "estimated_fee": 0.2,
            },
            {
                "type": "resolution",
                "timestamp": resolution_time.isoformat(),
                "market_id": "wx-high",
                "question": "Will NYC high temperature be above 80 degrees?",
                "outcome": "YES",
                "our_estimate": 0.7,
                "market_price_at_entry": 0.6,
                "pnl": 4.0,
            },
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-rain",
                "question": "Will it rain in Austin?",
                "category": "Weather",
                "direction": "BUY_NO",
                "price": 0.3,
                "size_usd": 20.0,
                "estimated_probability": 0.4,
                "edge": 0.2,
                "approved": True,
                "estimated_fee": 0.3,
            },
            {
                "type": "resolution",
                "timestamp": later_resolution.isoformat(),
                "market_id": "wx-rain",
                "question": "Will it rain in Austin?",
                "outcome": "NO",
                "our_estimate": 0.4,
                "market_price_at_entry": 0.7,
                "pnl": -2.0,
            },
        ],
    )

    report = TradeLogger(str(log_path)).get_calibration_report()

    assert "Total resolved: 2" in report
    assert "Brier score: 0.1250" in report
    assert "Recorded ROI: +6.7%" in report
    assert "not re-estimated here" in report
    assert "By market family" in report
    assert "high_temp" in report
    assert "rain" in report
    assert "By timing" in report
    assert "same_day" in report
    assert "tomorrow" in report


def test_weather_validation_report_gates_by_resolved_count(tmp_path):
    log_path = tmp_path / "weather_trades.jsonl"
    entry_time = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    entries = []

    for i in range(50):
        market_id = f"wx-{i}"
        entries.append({
            "type": "trade",
            "timestamp": entry_time.isoformat(),
            "market_id": market_id,
            "question": "Will NYC high temperature be above 80 degrees?",
            "category": "Weather",
            "direction": "BUY_YES",
            "price": 0.6,
            "size_usd": 10.0,
            "estimated_probability": 0.7,
            "edge": 0.1,
            "approved": True,
            "estimated_fee": 0.1,
            "model_source": "weather",
        })
        entries.append({
            "type": "resolution",
            "timestamp": (entry_time + timedelta(hours=8)).isoformat(),
            "market_id": market_id,
            "question": "Will NYC high temperature be above 80 degrees?",
            "outcome": "YES" if i % 2 == 0 else "NO",
            "our_estimate": 0.7,
            "market_price_at_entry": 0.6,
            "pnl": 4.0 if i % 2 == 0 else -6.0,
        })

    write_jsonl(log_path, entries[:-2])
    paper_only_report = TradeLogger(str(log_path)).get_weather_validation_report()

    assert "Resolved weather trades: 49" in paper_only_report
    assert "Readiness: PAPER_ONLY" in paper_only_report
    assert "Need for live review: 1 more resolved trades" in paper_only_report

    write_jsonl(log_path, entries)
    live_review_report = TradeLogger(str(log_path)).get_weather_validation_report()

    assert "Resolved weather trades: 50" in live_review_report
    assert "Readiness: LIVE_REVIEW" in live_review_report
    assert "Need for live review: 0 more resolved trades" in live_review_report
    assert "high_temp" in live_review_report
    assert "same_day" in live_review_report


def test_weather_validation_scores_only_yes_no_weather_settlements(tmp_path):
    log_path = tmp_path / "weather_semantics.jsonl"
    entry_time = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    write_jsonl(
        log_path,
        [
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-low",
                "question": "Will Chicago low temperature be below 32 degrees?",
                "category": "Weather",
                "direction": "BUY_YES",
                "price": 0.3,
                "size_usd": 10.0,
                "estimated_probability": 0.25,
                "edge": 0.05,
                "approved": True,
                "estimated_fee": 0.1,
                "model_source": "weather",
            },
            {
                "type": "resolution",
                "timestamp": (entry_time + timedelta(hours=8)).isoformat(),
                "market_id": "wx-low",
                "question": "Will Chicago low temperature be below 32 degrees?",
                "outcome": "NO",
                "our_estimate": 0.25,
                "market_price_at_entry": 0.3,
                "pnl": -3.0,
            },
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-snow",
                "question": "Will snow in Salt Lake City exceed 1 inch?",
                "category": "Weather",
                "direction": "BUY_YES",
                "price": 0.4,
                "size_usd": 12.0,
                "estimated_probability": 0.8,
                "edge": 0.4,
                "approved": True,
                "estimated_fee": 0.2,
                "model_source": "weather",
            },
            {
                "type": "resolution",
                "timestamp": (entry_time + timedelta(hours=30)).isoformat(),
                "market_id": "wx-snow",
                "question": "Will snow in Salt Lake City exceed 1 inch?",
                "outcome": "YES",
                "our_estimate": 0.8,
                "market_price_at_entry": 0.4,
                "pnl": 6.0,
            },
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-wind",
                "question": "Will wind speed in Boston exceed 20 mph?",
                "category": "Weather",
                "direction": "BUY_YES",
                "price": 0.5,
                "size_usd": 8.0,
                "estimated_probability": 0.65,
                "edge": 0.15,
                "approved": True,
                "estimated_fee": 0.1,
                "model_source": "weather",
            },
            {
                "type": "resolution",
                "timestamp": (entry_time + timedelta(hours=60)).isoformat(),
                "market_id": "wx-wind",
                "question": "Will wind speed in Boston exceed 20 mph?",
                "outcome": "YES",
                "our_estimate": 0.65,
                "market_price_at_entry": 0.5,
                "pnl": 2.0,
            },
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "wx-sold",
                "question": "Will Miami high temperature be above 90 degrees?",
                "category": "Weather",
                "direction": "BUY_YES",
                "price": 0.6,
                "size_usd": 10.0,
                "estimated_probability": 0.7,
                "edge": 0.1,
                "approved": True,
                "estimated_fee": 0.1,
                "model_source": "weather",
            },
            {
                "type": "resolution",
                "timestamp": (entry_time + timedelta(hours=10)).isoformat(),
                "market_id": "wx-sold",
                "question": "Will Miami high temperature be above 90 degrees?",
                "outcome": "SOLD",
                "our_estimate": 0.7,
                "market_price_at_entry": 0.6,
                "pnl": 0.0,
            },
            {
                "type": "trade",
                "timestamp": entry_time.isoformat(),
                "market_id": "sports",
                "question": "Will Houston win by over 3.5 points?",
                "category": "Sports",
                "direction": "BUY_YES",
                "price": 0.5,
                "size_usd": 20.0,
                "estimated_probability": 0.55,
                "edge": 0.05,
                "approved": True,
                "estimated_fee": 0.2,
            },
            {
                "type": "resolution",
                "timestamp": (entry_time + timedelta(hours=6)).isoformat(),
                "market_id": "sports",
                "question": "Will Houston win by over 3.5 points?",
                "outcome": "YES",
                "our_estimate": 0.55,
                "market_price_at_entry": 0.5,
                "pnl": 2.0,
            },
        ],
    )

    summary = TradeLogger(str(log_path)).get_weather_validation_summary()
    report = TradeLogger(str(log_path)).get_weather_validation_report()

    assert summary["resolved_count"] == 3
    assert summary["family_stats"]["low_temp"]["count"] == 1
    assert summary["family_stats"]["snow"]["count"] == 1
    assert summary["family_stats"]["wind"]["count"] == 1
    assert "high_temp" not in summary["family_stats"]
    assert "same_day" in summary["timing_stats"]
    assert "tomorrow" in summary["timing_stats"]
    assert "over_24h" in summary["timing_stats"]
    assert "Resolved weather trades: 3" in report
    assert "low_temp" in report
    assert "snow" in report
    assert "wind" in report
