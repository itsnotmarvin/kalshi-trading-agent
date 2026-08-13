"""
Tests for the Early Marks V2 snapshot store and tri-state evidence functions.

The load-bearing invariant: missing data is `unknown`, never `flat`.
Non-response requires actual observed history (≥2 priced observations over a
minimum window); absence of data must never count as evidence of a lazy mark.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.early_marks_snapshots import (
    MOVED_PRICE_DELTA,
    attention_state,
    load_history,
    open_db,
    record_observations,
    response_state,
    snapshot_record,
)

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _raw(ticker="KXTEST-1", yes_bid="0.18", yes_ask="0.20", **overrides):
    raw = {
        "ticker": ticker,
        "event_ticker": "KXTEST",
        "status": "active",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "yes_bid_size_fp": "150.00",
        "yes_ask_size_fp": "80.50",
        "no_bid_dollars": "0.80",
        "no_ask_dollars": "0.82",
        "last_price_dollars": "0.19",
        "volume_fp": "120",
        "volume_24h_fp": "12",
        "liquidity_dollars": "300",
        "open_time": "2026-08-01T12:00:00Z",
        "close_time": "2026-09-01T12:00:00Z",
    }
    raw.update(overrides)
    return raw


def _obs(observed_at, yes_bid=0.18, yes_ask=0.20, last_price=None, volume=120.0, volume_24h=12.0):
    return {
        "observed_at": observed_at.isoformat(),
        "ticker": "KXTEST-1",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "last_price": last_price,
        "volume": volume,
        "volume_24h": volume_24h,
    }


# ---------------------------------------------------------------------------
# Snapshot store round-trip
# ---------------------------------------------------------------------------

def test_snapshot_write_and_history_roundtrip(tmp_path):
    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(
            conn,
            [_raw(), _raw(ticker="KXOTHER-1")],
            T0,
            provenance={"pages": 2},
        )
        record_observations(
            conn,
            [_raw(yes_bid="0.30", yes_ask="0.32")],
            T0 + timedelta(hours=12),
        )

        history = load_history("KXTEST-1", conn=conn)
        assert len(history) == 2
        assert history[0]["observed_at"] < history[1]["observed_at"]
        assert history[0]["yes_bid"] == 0.18
        assert history[1]["yes_bid"] == 0.30
        assert all(r["ticker"] == "KXTEST-1" for r in history)
    finally:
        conn.close()


def test_snapshot_record_treats_empty_strings_as_missing():
    record = snapshot_record(_raw(yes_bid="", yes_ask="", last_price_dollars=""), T0)
    assert record["yes_bid"] is None
    assert record["yes_ask"] is None
    assert record["last_price"] is None
    assert record["volume"] == 120.0


def test_missing_flow_fields_stay_none_never_zero():
    # A payload without flow fields must store None, not 0.0 — a stored zero
    # later reads as evidence of LOW attention, which fabricates the exact
    # "unwatched" candidate condition from missing data. (This defect made
    # every market in the August 2026 store read attention=low.)
    raw = _raw()
    for key in ("volume_fp", "volume_24h_fp", "liquidity_dollars"):
        raw.pop(key, None)
    record = snapshot_record(raw, T0)
    assert record["volume"] is None
    assert record["volume_24h"] is None
    assert record["liquidity"] is None
    assert attention_state({"volume_24h": record["volume_24h"]})["state"] == "unknown"

    # And a genuine zero is data, not absence.
    zero = snapshot_record(_raw(volume_24h_fp="0.00"), T0)
    assert zero["volume_24h"] == 0.0
    assert attention_state({"volume_24h": zero["volume_24h"]})["state"] == "low"


def test_book_depth_missing_stays_none_and_zero_is_data(tmp_path):
    # Kalshi's liquidity_dollars is dead (always 0); real depth lives in
    # yes_bid_size_fp/yes_ask_size_fp. Same missing-data contract as flow:
    # an absent size must not read as an empty book.
    raw = _raw()
    raw.pop("yes_bid_size_fp")
    raw.pop("yes_ask_size_fp")
    record = snapshot_record(raw, T0)
    assert record["yes_bid_size"] is None
    assert record["yes_ask_size"] is None

    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(conn, [raw], T0)
        history = load_history("KXTEST-1", conn=conn)
        assert history[0]["yes_bid_size"] is None
        assert history[0]["yes_ask_size"] is None
    finally:
        conn.close()

    zero = snapshot_record(_raw(yes_bid_size_fp="0.00", yes_ask_size_fp="0.00"), T0)
    assert zero["yes_bid_size"] == 0.0
    assert zero["yes_ask_size"] == 0.0


def test_book_depth_roundtrips_through_store(tmp_path):
    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(conn, [_raw()], T0)
        history = load_history("KXTEST-1", conn=conn)
        assert history[0]["yes_bid_size"] == 150.0
        assert history[0]["yes_ask_size"] == 80.5
    finally:
        conn.close()


def test_open_db_migrates_pre_depth_databases(tmp_path):
    # The live store predates the depth columns; open_db must ALTER them in
    # without disturbing existing rows, which read back as depth-unknown.
    db = tmp_path / "observations.db"
    conn = open_db(db)
    record_observations(conn, [_raw()], T0)
    conn.execute("ALTER TABLE observations DROP COLUMN yes_bid_size")
    conn.execute("ALTER TABLE observations DROP COLUMN yes_ask_size")
    conn.commit()
    conn.close()

    conn = open_db(db)
    try:
        record_observations(conn, [_raw(yes_bid="0.30", yes_ask="0.32")], T0 + timedelta(hours=12))
        history = load_history("KXTEST-1", conn=conn)
        assert history[0]["yes_bid_size"] is None  # pre-migration row: unknown
        assert history[1]["yes_bid_size"] == 150.0
        assert history[1]["yes_ask_size"] == 80.5
    finally:
        conn.close()


def test_attention_bucket_transitions_are_material(tmp_path):
    # volume_24h drifts constantly and is deliberately not a material field,
    # BUT its attention bucket (including value→missing) must be: the
    # synthetic trailing observation copies the newest stored row, so an
    # unstored transition would keep reporting stale attention as current.
    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(conn, [_raw(volume_24h_fp="12")], T0)

        # Same bucket (low), same prices: drift alone must NOT store a row.
        record_observations(conn, [_raw(volume_24h_fp="15")], T0 + timedelta(hours=1))
        assert len(load_history("KXTEST-1", conn=conn)) == 2  # row + confirmed tail

        # Flow field disappears (low → unknown): must store a row, and
        # attention must read unknown, not the stale stored zero/low value.
        gone = _raw()
        gone.pop("volume_24h_fp")
        record_observations(conn, [gone], T0 + timedelta(hours=2))
        history = load_history("KXTEST-1", conn=conn)
        assert history[-1]["volume_24h"] is None
        assert attention_state(history[-1])["state"] == "unknown"

        # Bucket crossing (unknown → high) is material again.
        record_observations(conn, [_raw(volume_24h_fp="600")], T0 + timedelta(hours=3))
        history = load_history("KXTEST-1", conn=conn)
        assert attention_state(history[-1])["state"] == "high"
    finally:
        conn.close()


def test_last_seen_never_moves_backward(tmp_path):
    # An overlapping slower collection committing an older observed_at after
    # a newer run must not rewind last_seen or overwrite newer metadata.
    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(conn, [_raw()], T0 + timedelta(hours=2))
        record_observations(conn, [_raw()], T0)
        row = conn.execute("SELECT last_seen FROM markets WHERE ticker = 'KXTEST-1'").fetchone()
        assert row["last_seen"] == (T0 + timedelta(hours=2)).isoformat()
    finally:
        conn.close()


async def test_collect_snapshot_refuses_to_record_empty_run(tmp_path, monkeypatch):
    # A network failure yields zero markets; recording that as a successful
    # run would silently poison the coverage history.
    import pytest

    import core.early_marks_snapshots as snapshots
    from core.early_marks_snapshots import collect_snapshot

    async def fake_fetch(adapter, page_limit, max_pages):
        return [], {"batches": [{"status": "open", "events": 0, "error": "boom"}], "mve_skipped": 0}

    monkeypatch.setattr(snapshots, "fetch_universe", fake_fetch)

    db_path = tmp_path / "observations.db"
    with pytest.raises(RuntimeError):
        await collect_snapshot(None, T0, db_path=db_path)
    assert not db_path.exists()


def test_volume_delta_requires_observed_endpoint_volumes():
    # A delta computed against a missing baseline is fabricated data.
    history = [
        _obs(T0, volume=None),
        _obs(T0 + timedelta(hours=24), volume=100.0),
    ]
    result = response_state(history)
    assert result["state"] == "observed_flat"
    assert result["volume_delta"] is None


def test_series_ticker_is_persisted(tmp_path):
    conn = open_db(tmp_path / "observations.db")
    try:
        record_observations(conn, [_raw(series_ticker="KXTESTSERIES")], T0)
        row = conn.execute(
            "SELECT series_ticker FROM markets WHERE ticker = 'KXTEST-1'"
        ).fetchone()
        assert row["series_ticker"] == "KXTESTSERIES"
    finally:
        conn.close()


def test_snapshot_skips_tickerless_rows(tmp_path):
    conn = open_db(tmp_path / "observations.db")
    try:
        summary = record_observations(conn, [_raw(), {"title": "no ticker"}], T0)
        assert summary == {"seen": 1, "new_markets": 1, "changed": 1, "unchanged": 0}
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Response evidence: missing data is unknown, never flat
# ---------------------------------------------------------------------------

def test_single_observation_is_unknown_not_flat():
    result = response_state([_obs(T0)])
    assert result["state"] == "unknown"


def test_no_priced_data_is_unknown_not_flat():
    # An untraded market with an empty book: V1 scored this as maximally
    # "lazy" — it must be UNKNOWN, because nothing was ever observed.
    history = [
        _obs(T0, yes_bid=None, yes_ask=None, last_price=None),
        _obs(T0 + timedelta(hours=24), yes_bid=None, yes_ask=None, last_price=None),
    ]
    assert response_state(history)["state"] == "unknown"


def test_short_window_is_unknown_even_with_two_prints():
    history = [_obs(T0), _obs(T0 + timedelta(minutes=30))]
    result = response_state(history)
    assert result["state"] == "unknown"
    assert "window" in result["reason"]


def test_flat_history_over_real_window_is_observed_flat():
    history = [_obs(T0), _obs(T0 + timedelta(hours=12)), _obs(T0 + timedelta(hours=24))]
    result = response_state(history)
    assert result["state"] == "observed_flat"
    assert result["observations"] == 3
    assert result["price_range"] < MOVED_PRICE_DELTA


def test_price_move_is_detected_as_responded():
    history = [
        _obs(T0),
        _obs(T0 + timedelta(hours=24), yes_bid=0.28, yes_ask=0.30),
    ]
    result = response_state(history)
    assert result["state"] == "moved"
    assert result["price_range"] >= MOVED_PRICE_DELTA


def test_last_trade_price_is_used_when_book_is_one_sided():
    history = [
        _obs(T0, yes_bid=None, yes_ask=None, last_price=0.20),
        _obs(T0 + timedelta(hours=24), yes_bid=None, yes_ask=None, last_price=0.21),
    ]
    result = response_state(history)
    assert result["state"] == "observed_flat"


# ---------------------------------------------------------------------------
# Attention evidence
# ---------------------------------------------------------------------------

def test_attention_states_from_observed_flow():
    assert attention_state(None)["state"] == "unknown"
    assert attention_state(_obs(T0, volume_24h=None))["state"] == "unknown"
    assert attention_state(_obs(T0, volume_24h=3.0))["state"] == "low"
    assert attention_state(_obs(T0, volume_24h=80.0))["state"] == "normal"
    assert attention_state(_obs(T0, volume_24h=5000.0))["state"] == "high"


# ---------------------------------------------------------------------------
# Category-blindness: evidence functions never read category or title
# ---------------------------------------------------------------------------

async def test_collect_snapshot_unions_unstable_sweeps(tmp_path, monkeypatch):
    # Kalshi cursor pagination is unstable: consecutive sweeps see different
    # subsets. A collection run must union sweeps and record per-sweep counts
    # so coverage gaps are auditable instead of silent.
    import core.early_marks_snapshots as snapshots
    from core.early_marks_snapshots import collect_snapshot

    sweep_results = [
        [_raw(ticker="KXA-1"), _raw(ticker="KXB-1")],
        [_raw(ticker="KXB-1"), _raw(ticker="KXC-1")],
    ]
    calls = {"n": 0}

    async def fake_fetch(adapter, page_limit, max_pages):
        result = sweep_results[min(calls["n"], len(sweep_results) - 1)]
        calls["n"] += 1
        return result, {"batches": [{"added": len(result)}], "mve_skipped": 0}

    monkeypatch.setattr(snapshots, "fetch_universe", fake_fetch)

    db_path = tmp_path / "observations.db"
    summary = await collect_snapshot(None, T0, db_path=db_path, sweeps=2)

    assert summary == {"seen": 3, "new_markets": 3, "changed": 3, "unchanged": 0}
    conn = open_db(db_path)
    try:
        tickers = {
            row[0] for row in conn.execute("SELECT ticker FROM observations").fetchall()
        }
        provenance = json.loads(
            conn.execute("SELECT provenance FROM collection_runs").fetchone()[0]
        )
        assert provenance["sweeps"] == 2
        assert provenance["sweep_market_counts"] == [2, 2]
        assert provenance["union_market_count"] == 3
        assert tickers == {"KXA-1", "KXB-1", "KXC-1"}
    finally:
        conn.close()


async def test_fetch_universe_walks_events_and_skips_parlay_combos():
    # The /markets firehose is dominated by auto-generated multivariate parlay
    # combos (empty books, created by the tens of thousands daily). Collection
    # walks /events instead, stamps series_ticker from the event, and skips
    # any parlay leg — counted in diagnostics so the exclusion is auditable.
    from core.early_marks_snapshots import fetch_universe

    pages = {
        None: {
            "events": [{
                "event_ticker": "KXTEST",
                "series_ticker": "KXTESTSERIES",
                "markets": [
                    _raw(ticker="KXTEST-1"),
                    dict(_raw(ticker="KXMVE-1"), mve_collection_ticker="KXMVECROSSCATEGORY-R"),
                ],
            }],
            "cursor": "page2",
        },
        "page2": {
            "events": [{
                "event_ticker": "KXOTHER",
                "series_ticker": "KXOTHERSERIES",
                "markets": [_raw(ticker="KXOTHER-1")],
            }],
            "cursor": None,
        },
    }

    class FakeAdapter:
        async def _request(self, method, path, params=None, data=None, silent=False):
            assert path == "/events"
            assert params["with_nested_markets"] is True
            return pages[params.get("cursor")]

    markets, diagnostics = await fetch_universe(FakeAdapter(), statuses=("open",))

    assert [m["ticker"] for m in markets] == ["KXTEST-1", "KXOTHER-1"]
    assert markets[0]["series_ticker"] == "KXTESTSERIES"
    assert diagnostics["mve_skipped"] == 1
    assert len(diagnostics["batches"]) == 2


def test_evidence_is_category_blind_by_construction():
    weather = [_obs(T0), _obs(T0 + timedelta(hours=24))]
    politics = [dict(r, ticker="KXPRESPERSON-28") for r in weather]
    assert response_state(weather) == response_state(politics)
    # snapshot_record carries no category or title field at all.
    record = snapshot_record(_raw(category="Politics", title="Election market"), T0)
    assert "category" not in record
    assert "title" not in record
