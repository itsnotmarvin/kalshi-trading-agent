"""
Tests for the repricing label engine and premise check (evaluation spec §1–2b).

The load-bearing invariants: movement is judged in log-odds AND must cross
the book; missing coverage yields `unlabelable`, never `not_repriced`;
settlement drift is embargoed; and unelapsed windows stay `pending` instead
of quietly counting as negatives.
"""
from datetime import datetime, timedelta, timezone

from core.early_marks_labels import (
    designated_runs,
    evidence_bucket,
    label_market,
    logit,
    premise_check,
    price_band,
    render_premise_report,
)
from core.early_marks_snapshots import open_db, record_observations

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FAR_CLOSE = "2026-12-01T12:00:00+00:00"


def _row(at, bid=0.10, ask=0.12, status="active", **extra):
    row = {
        "observed_at": at.isoformat(),
        "status": status,
        "yes_bid": bid,
        "yes_ask": ask,
        "last_price": None,
        "volume": 10.0,
        "volume_24h": 5.0,
    }
    row.update(extra)
    return row


def test_price_band_matches_logodds_bands():
    assert price_band(0.50) == "mid"          # |L| = 0
    assert price_band(0.20) == "lean"         # |L| ≈ 1.39
    assert price_band(0.05) == "tail"         # |L| ≈ 2.94
    assert logit(0.001) == logit(0.01)        # clamped at the floor


def test_repriced_requires_logodds_move_and_book_crossing():
    # 0.11 → 0.26 mid is ~1.05 logits and the later bid (0.25) clears the
    # flag-time ask (0.12): a real, executable repricing.
    history = [
        _row(T0),
        _row(T0 + timedelta(days=5), bid=0.25, ask=0.27),
    ]
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=FAR_CLOSE)
    assert result["label"] == "repriced"
    assert result["delta_logodds"] >= 0.5


def test_mid_drift_inside_a_wide_spread_is_not_repricing():
    # The book widens (0.10/0.60): mid jumps ~1.5 logits but the bid never
    # reaches the flag-time ask — a quote artifact, not a repricing. With
    # coverage held through the window, that is a confident not_repriced.
    history = [
        _row(T0),
        _row(T0 + timedelta(days=12), bid=0.10, ask=0.60),
    ]
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=FAR_CLOSE)
    assert result["label"] == "not_repriced"


def test_unelapsed_window_is_pending_not_negative():
    history = [_row(T0)]
    result = label_market(history, T0, T0 + timedelta(days=3), close_time=FAR_CLOSE)
    assert result["label"] == "pending"


def test_missing_coverage_is_unlabelable_never_not_repriced():
    # Seen only at flag time, judged after the window: nobody watched, so
    # "it didn't reprice" is unknowable — the V1 missing-data trap again.
    history = [_row(T0)]
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=FAR_CLOSE)
    assert result["label"] == "unlabelable"
    assert "coverage" in result["reason"]


def test_settlement_embargo_empties_the_window():
    # Market closes 24h after flag time; the 48h embargo empties the label
    # window entirely — settlement drift must never count as repricing.
    history = [
        _row(T0),
        _row(T0 + timedelta(hours=12), bid=0.90, ask=0.92),
    ]
    close = (T0 + timedelta(days=1)).isoformat()
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=close)
    assert result["label"] == "unlabelable"
    assert "window empty" in result["reason"]


def test_unpriced_flag_time_is_unlabelable():
    history = [
        _row(T0, bid=None, ask=None),
        _row(T0 + timedelta(days=5), bid=0.25, ask=0.27),
    ]
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=FAR_CLOSE)
    assert result["label"] == "unlabelable"


def test_synthetic_confirmed_rows_prove_coverage_but_never_moves():
    # A dormant market seen unchanged through last_seen: the confirmed tail
    # supplies coverage (not_repriced, honestly), and even a price on it
    # could never certify a move because stored rows alone are endpoints.
    tail = _row(T0 + timedelta(days=13))
    tail["confirmed"] = True
    history = [_row(T0), tail]
    result = label_market(history, T0, T0 + timedelta(days=30), close_time=FAR_CLOSE)
    assert result["label"] == "not_repriced"


def test_designated_run_is_nearest_noon_utc():
    runs = [
        "2026-08-08T02:00:00+00:00",
        "2026-08-08T11:30:00+00:00",
        "2026-08-08T23:00:00+00:00",
        "2026-08-09T18:00:00+00:00",
    ]
    assert designated_runs(runs) == [
        "2026-08-08T11:30:00+00:00",
        "2026-08-09T18:00:00+00:00",
    ]


def _raw(ticker, bid, ask, volume, volume_24h, close_time=FAR_CLOSE):
    return {
        "ticker": ticker,
        "event_ticker": f"EV-{ticker}",
        "series_ticker": f"SER-{ticker}",
        "status": "active",
        "yes_bid_dollars": str(bid),
        "yes_ask_dollars": str(ask),
        "volume_fp": str(volume),
        "volume_24h_fp": str(volume_24h),
        "close_time": close_time,
    }


def test_premise_check_end_to_end(tmp_path):
    # Two markets observed twice before the designated noon run (so their
    # response is a legitimate observed_flat), then one of them — the
    # low-attention candidate — reprices on day 5 while the watched one
    # stays flat through last_seen. The premise check must bucket them
    # apart, label them from snapshots alone, and report a candidate rate
    # with matched-control context.
    conn = open_db(tmp_path / "observations.db")
    try:
        day0 = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
        noon0 = day0 + timedelta(hours=12)

        def both(bid_c, ask_c):
            return [
                _raw("CAND-1", bid_c, ask_c, volume=10, volume_24h=5),
                _raw("WATCH-1", 0.10, 0.12, volume=500, volume_24h=100),
            ]

        record_observations(conn, both(0.10, 0.12), day0)
        record_observations(conn, both(0.10, 0.12), noon0)
        # Day 5: the candidate repricies (bid clears the flag-time ask).
        record_observations(conn, [
            _raw("CAND-1", 0.25, 0.27, volume=60, volume_24h=40),
            _raw("WATCH-1", 0.10, 0.12, volume=500, volume_24h=100),
        ], day0 + timedelta(days=5, hours=12))
        # Day 20: everything seen again (advances last_seen → coverage).
        record_observations(conn, [
            _raw("CAND-1", 0.25, 0.27, volume=60, volume_24h=40),
            _raw("WATCH-1", 0.10, 0.12, volume=500, volume_24h=100),
        ], day0 + timedelta(days=20))

        result = premise_check(conn, as_of=day0 + timedelta(days=40))
    finally:
        conn.close()

    assert result["buckets"]["candidate"]["n"] >= 1
    assert result["buckets"]["candidate"]["reprice_rate"] == 1.0
    assert result["buckets"]["flat_watched"]["reprice_rate"] == 0.0
    assert result["repriced_episodes"] == 1
    assert result["episode_floor_met"] is False
    assert result["coverage_fraction"] is not None

    report = render_premise_report(result)
    assert "candidate" in report and "matched-random" in report
    assert "≥200 floor NOT met" in report


def test_evidence_bucket_distinguishes_watched_from_candidate():
    flat = [
        _row(T0, volume_24h=5.0),
        _row(T0 + timedelta(hours=12), volume_24h=5.0),
    ]
    watched = [dict(r, volume_24h=100.0) for r in flat]
    assert evidence_bucket(flat) == "candidate"
    assert evidence_bucket(watched) == "flat_watched"
    moved = [
        _row(T0),
        _row(T0 + timedelta(hours=12), bid=0.30, ask=0.32),
    ]
    assert evidence_bucket(moved) == "moved"
    assert evidence_bucket([_row(T0)]) == "unknown"
