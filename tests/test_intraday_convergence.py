from datetime import date, datetime, timedelta, timezone

import pytest

from core.settlement_stations import SETTLEMENT_STATIONS
from scripts.intraday_convergence import (
    Observation,
    analyze_market_event,
    compute_lock_time,
    count_drop_reasons,
    measure_convergence,
    summarize_events,
)


def _observation(iso: str, temp: float) -> Observation:
    return Observation(datetime.fromisoformat(iso.replace("Z", "+00:00")), temp)


def _candle(end: datetime, bid: float | None, ask: float | None) -> dict:
    return {
        "end_period_ts": int(end.timestamp()),
        "yes_bid": {} if bid is None else {"close_dollars": str(bid)},
        "yes_ask": {} if ask is None else {"close_dollars": str(ask)},
    }


def test_lock_time_requires_full_one_degree_margin():
    observations = [
        _observation("2026-08-10T15:00:00Z", 80.9),
        _observation("2026-08-10T16:00:00Z", 81.0),
    ]

    lock = compute_lock_time(observations, date(2026, 8, 10), "America/New_York", 80.0)

    assert lock == datetime(2026, 8, 10, 16, tzinfo=timezone.utc)


def test_lock_time_does_not_leak_across_station_climate_day_boundary():
    # At New York in August, 03:30Z is still the previous local day.
    observations = [
        _observation("2026-08-10T03:30:00Z", 90.0),
        _observation("2026-08-10T04:30:00Z", 80.9),
        _observation("2026-08-10T14:30:00Z", 81.0),
    ]

    lock = compute_lock_time(observations, date(2026, 8, 10), "America/New_York", 80.0)

    assert lock == datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)


def test_convergence_uses_containing_candle_as_hour_zero_and_skips_one_sided():
    lock = datetime(2026, 8, 10, 15, 35, tzinfo=timezone.utc)
    candles = [
        _candle(datetime(2026, 8, 10, 16, tzinfo=timezone.utc), 0.80, 0.90),
        _candle(datetime(2026, 8, 10, 17, tzinfo=timezone.utc), 0.96, None),
        _candle(datetime(2026, 8, 10, 18, tzinfo=timezone.utc), 0.94, 0.96),
    ]

    hours, reason = measure_convergence(candles, lock)

    assert reason is None
    assert hours == 2


def test_convergence_reports_when_threshold_never_observed():
    lock = datetime(2026, 8, 10, 16, tzinfo=timezone.utc)
    candles = [_candle(lock, 0.90, 0.94)]

    hours, reason = measure_convergence(candles, lock)

    assert hours is None
    assert reason == "convergence_not_observed"


def test_event_analysis_accounts_for_missing_lock_hour_and_one_sided_snapshots():
    station = SETTLEMENT_STATIONS["new york"]
    market = {
        "ticker": "KXHIGHNY-26AUG10-T80",
        "strike_type": "greater",
        "floor_strike": 80,
        "result": "yes",
    }
    lock_observation = _observation("2026-08-10T15:35:00Z", 81.0)
    prior = _candle(datetime(2026, 8, 10, 15, tzinfo=timezone.utc), 0.50, 0.55)

    missing = analyze_market_event(market, station, [lock_observation], [prior])

    assert missing["drop_reason"] == "lock_hour_candle_missing"

    lock_candle = _candle(datetime(2026, 8, 10, 16, tzinfo=timezone.utc), 0.82, 0.88)
    one_sided = _candle(datetime(2026, 8, 10, 17, tzinfo=timezone.utc), 0.90, None)
    later = _candle(datetime(2026, 8, 10, 18, tzinfo=timezone.utc), 0.95, 0.97)
    analyzed = analyze_market_event(
        market, station, [lock_observation], [prior, lock_candle, one_sided, later]
    )

    assert analyzed["drop_reason"] is None
    assert analyzed["hours_to_convergence"] == 2
    assert analyzed["snapshots"]["0"]["gap_below_0_95"] == pytest.approx(0.07)
    assert analyzed["snapshots"]["1"]["reason"] == "candle_one_sided"
    # A 97-cent taker buy pays a one-cent rounded-up fee.
    assert analyzed["snapshots"]["2"]["net_pnl"] == pytest.approx(0.02)


def test_event_analysis_distinguishes_missing_observations_from_no_lock():
    station = SETTLEMENT_STATIONS["new york"]
    market = {
        "ticker": "KXHIGHNY-26AUG10-T80",
        "strike_type": "greater",
        "floor_strike": 80,
        "result": "yes",
    }

    missing = analyze_market_event(market, station, [], [])
    below_strike = analyze_market_event(
        market, station, [_observation("2026-08-10T15:00:00Z", 79.0)], []
    )

    assert missing["drop_reason"] == "observations_missing_climate_day"
    assert below_strike["drop_reason"] == "no_physical_yes_lock"


def test_drop_reason_accounting_counts_every_candidate_once():
    rows = [
        {"drop_reason": None},
        {"drop_reason": "no_physical_yes_lock"},
        {"drop_reason": "no_physical_yes_lock"},
        {"drop_reason": "lock_before_market_had_quotes"},
    ]

    counts = count_drop_reasons(rows)

    assert counts == {
        "analyzed": 1,
        "no_physical_yes_lock": 2,
        "lock_before_market_had_quotes": 1,
    }
    assert sum(counts.values()) == len(rows)


def test_summary_uses_core_fee_math_for_one_contract_pnl():
    row = {
        "station_id": "KNYC",
        "drop_reason": None,
        "hours_to_convergence": 0,
        "snapshots": {
            "0": {"yes_ask": 0.80, "gap_below_0_95": 0.15, "taker_fee": 0.02, "net_pnl": 0.18, "reason": None},
            "1": {"yes_ask": None, "reason": "candle_missing"},
            "2": {"yes_ask": None, "reason": "candle_one_sided"},
        },
    }

    summary = summarize_events([row])

    assert summary["convergence_distribution"]["0h"] == 1
    assert summary["snapshots"]["0"]["average_tradeable_gap"] == pytest.approx(0.15)
    assert summary["snapshots"]["0"]["total_net_pnl"] == pytest.approx(0.18)
    assert summary["snapshot_missing_reasons"] == {
        "lock+1h:candle_missing": 1,
        "lock+2h:candle_one_sided": 1,
    }
