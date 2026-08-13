"""
Early Marks V2 — market observation store and evidence functions.

Non-response ("the market has not reacted yet") is a claim about HISTORY and
cannot be measured from a single scan. This module persists point-in-time
observations of the market universe in SQLite and derives the two tri-state
evidence signals the V2 candidate-generation contract requires:

    response_state:  observed_flat | moved | unknown
    attention_state: low | normal | high | unknown

Storage is optimized for meaningful results: an observation row is written
only when the market MATERIALLY CHANGED (price, book, volume, status) since
its last stored row; otherwise only `markets.last_seen` advances. A dormant
market — the exact thing early marks hunts — costs one row plus a sliding
"unchanged as of" timestamp, so storage grows with market activity, not
universe size. "Nothing changed for three days" is a queryable fact.

Missing data is `unknown`, never `flat` — one stale quote is an unobserved
market, not a lazy one. Timestamps are always injected by the caller so
evidence functions stay deterministic and testable with synthetic histories.
See docs/early-marks-v2-detection-spec.md.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from config.paths import RUNTIME_DATA_DIR

DB_PATH = RUNTIME_DATA_DIR / "early_marks" / "observations.db"

# Evidence thresholds. Deliberately simple and documented rather than tuned:
# they define what the store can CLAIM, not what is worth trading.
MIN_OBSERVATIONS = 2
MIN_WINDOW_HOURS = 6.0        # two prints minutes apart prove nothing
MOVED_PRICE_DELTA = 0.03      # ≥3¢ of mid/last movement counts as a response
HIGH_ATTENTION_VOLUME_24H = 500.0
LOW_ATTENTION_VOLUME_24H = 20.0

# A new observation row is stored only when one of these fields changed.
# volume moves only on trades and the book fields only on quote changes, so
# together they capture every market event; volume_24h/liquidity drift
# constantly and are stored as context on whichever row gets written.
# volume_24h is additionally material when its ATTENTION BUCKET (including
# missingness) changes: the synthetic trailing observation copies the newest
# stored row, so an unstored low→high/value→missing transition would keep
# feeding attention_state a stale claim about current flow.
# Top-of-book depth (yes_bid_size/yes_ask_size) is deliberately context-only:
# sizes flicker on every resting-order change and would erode the dedup schema
# if material. The cost is that depth shifts at unchanged prices go unstored
# and the trailing synthetic observation repeats stale depth — any future
# depth-evidence function must first add bucket-transition materiality here,
# mirroring volume_24h's _attention_bucket handling above.
_MATERIAL_FIELDS = ("status", "yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "open_interest")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    open_time TEXT,
    close_time TEXT,
    expected_expiration_time TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    ticker TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT,
    yes_bid REAL,
    yes_ask REAL,
    yes_bid_size REAL,
    yes_ask_size REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume REAL,
    volume_24h REAL,
    liquidity REAL,
    open_interest REAL,
    PRIMARY KEY (ticker, observed_at)
);
CREATE TABLE IF NOT EXISTS collection_runs (
    observed_at TEXT PRIMARY KEY,
    market_count INTEGER NOT NULL,
    new_market_count INTEGER NOT NULL,
    changed_count INTEGER NOT NULL,
    provenance TEXT
);
"""


def open_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    try:  # migrate pre-series databases in place
        conn.execute("ALTER TABLE markets ADD COLUMN series_ticker TEXT")
    except sqlite3.OperationalError:
        pass
    for column in ("yes_bid_size", "yes_ask_size"):  # migrate pre-depth databases in place
        try:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {column} REAL")
        except sqlite3.OperationalError:
            pass
    return conn


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attention_bucket(volume_24h: float | None) -> str:
    """Tri-state attention bucket; missing flow is `unknown`, never `low`."""
    if volume_24h is None:
        return "unknown"
    if volume_24h >= HIGH_ATTENTION_VOLUME_24H:
        return "high"
    if volume_24h > LOW_ATTENTION_VOLUME_24H:
        return "normal"
    return "low"


def _first_float(raw: dict[str, Any], *keys: str) -> float | None:
    """First parseable numeric among keys — 0.0 is data, absence is None."""
    for key in keys:
        value = _as_float(raw.get(key))
        if value is not None:
            return value
    return None


def snapshot_record(raw: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    """
    Compact one raw Kalshi market payload into an observation record.

    Flow and depth fields (volume, volume_24h, liquidity, open_interest,
    yes_bid_size, yes_ask_size) stay None when the payload omits them: a
    stored 0.0 would later read as evidence of low attention or an empty
    book, and the contract requires missing evidence to stay `unknown`.
    """
    return {
        "observed_at": observed_at.isoformat(),
        "ticker": str(raw.get("ticker") or ""),
        "event_ticker": str(raw.get("event_ticker") or ""),
        "series_ticker": str(raw.get("series_ticker") or "") or None,
        "status": str(raw.get("status") or "unknown").lower(),
        "yes_bid": _as_float(raw.get("yes_bid_dollars")),
        "yes_ask": _as_float(raw.get("yes_ask_dollars")),
        "yes_bid_size": _first_float(raw, "yes_bid_size_fp", "yes_bid_size"),
        "yes_ask_size": _first_float(raw, "yes_ask_size_fp", "yes_ask_size"),
        "no_bid": _as_float(raw.get("no_bid_dollars")),
        "no_ask": _as_float(raw.get("no_ask_dollars")),
        "last_price": _as_float(raw.get("last_price_dollars")),
        "volume": _first_float(raw, "volume_fp", "volume"),
        "volume_24h": _first_float(raw, "volume_24h_fp", "volume_24h"),
        "liquidity": _first_float(raw, "liquidity_dollars", "liquidity"),
        "open_interest": _first_float(raw, "open_interest_fp", "open_interest"),
        "open_time": raw.get("open_time"),
        "close_time": raw.get("close_time"),
        "expected_expiration_time": raw.get("expected_expiration_time"),
    }


def record_observations(
    conn: sqlite3.Connection,
    raw_markets: list[dict[str, Any]],
    observed_at: datetime,
    provenance: dict[str, Any] | None = None,
) -> dict[str, int]:
    """
    Record one collection run: write observation rows only for markets that
    materially changed, advance last_seen for everything observed, and log
    the run's coverage provenance so gaps are auditable.
    """
    ts = observed_at.isoformat()
    seen = new_markets = changed = 0

    cur = conn.cursor()
    for raw in raw_markets:
        record = snapshot_record(raw, observed_at)
        ticker = record["ticker"]
        if not ticker:
            continue
        seen += 1

        row = cur.execute(
            "SELECT last_seen FROM markets WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            new_markets += 1
            cur.execute(
                "INSERT INTO markets (ticker, event_ticker, series_ticker, open_time, close_time,"
                " expected_expiration_time, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                (ticker, record["event_ticker"], record["series_ticker"], record["open_time"],
                 record["close_time"], record["expected_expiration_time"], ts, ts),
            )
            material_changed = True
        else:
            # An overlapping slower run committing an older observed_at must
            # not move last_seen backward or overwrite newer metadata.
            cur.execute(
                "UPDATE markets SET last_seen = ?, event_ticker = ?, series_ticker = ?, open_time = ?,"
                " close_time = ?, expected_expiration_time = ? WHERE ticker = ? AND last_seen <= ?",
                (ts, record["event_ticker"], record["series_ticker"], record["open_time"],
                 record["close_time"], record["expected_expiration_time"], ticker, ts),
            )
            latest = cur.execute(
                "SELECT status, yes_bid, yes_ask, no_bid, no_ask, last_price, volume,"
                " open_interest, volume_24h FROM observations WHERE ticker = ?"
                " ORDER BY observed_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            material_changed = latest is None or any(
                latest[field] != record[field] for field in _MATERIAL_FIELDS
            ) or _attention_bucket(latest["volume_24h"]) != _attention_bucket(record["volume_24h"])

        if material_changed:
            changed += 1
            cur.execute(
                "INSERT OR REPLACE INTO observations (ticker, observed_at, status, yes_bid,"
                " yes_ask, yes_bid_size, yes_ask_size, no_bid, no_ask, last_price, volume,"
                " volume_24h, liquidity, open_interest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticker, ts, record["status"], record["yes_bid"], record["yes_ask"],
                 record["yes_bid_size"], record["yes_ask_size"],
                 record["no_bid"], record["no_ask"], record["last_price"], record["volume"],
                 record["volume_24h"], record["liquidity"], record["open_interest"]),
            )

    cur.execute(
        "INSERT OR REPLACE INTO collection_runs (observed_at, market_count,"
        " new_market_count, changed_count, provenance) VALUES (?,?,?,?,?)",
        (ts, seen, new_markets, changed, json.dumps(provenance or {})),
    )
    conn.commit()
    return {"seen": seen, "new_markets": new_markets, "changed": changed,
            "unchanged": seen - changed}


def load_history(ticker: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """
    Return a market's observation history, oldest first.

    Because unchanged sightings only advance `markets.last_seen`, the history
    is extended with one synthetic trailing observation (same values,
    `confirmed: True`) when the market was seen unchanged after its newest
    stored row — "observed identical at last_seen" is real evidence and the
    whole point of the dedup schema.
    """
    own = conn is None
    if own:
        if not DB_PATH.exists():
            return []
        conn = open_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE ticker = ? ORDER BY observed_at", (ticker,)
        ).fetchall()]
        market = conn.execute(
            "SELECT last_seen FROM markets WHERE ticker = ?", (ticker,)
        ).fetchone()
        if rows and market and market["last_seen"] > rows[-1]["observed_at"]:
            confirmed = dict(rows[-1])
            confirmed["observed_at"] = market["last_seen"]
            confirmed["confirmed"] = True
            rows.append(confirmed)
        return rows
    finally:
        if own:
            conn.close()


def _observed_price(record: dict[str, Any]) -> float | None:
    """Best available price mark: mid of a two-sided book, else last trade."""
    yes_bid, yes_ask = record.get("yes_bid"), record.get("yes_ask")
    if yes_bid is not None and yes_ask is not None and 0 < yes_bid <= yes_ask < 1:
        return (yes_bid + yes_ask) / 2.0
    last = record.get("last_price")
    if last is not None and 0 < last < 1:
        return last
    return None


def _window_hours(history: list[dict[str, Any]]) -> float:
    try:
        first = datetime.fromisoformat(history[0]["observed_at"])
        last = datetime.fromisoformat(history[-1]["observed_at"])
        return (last - first).total_seconds() / 3600.0
    except (KeyError, ValueError, IndexError):
        return 0.0


def response_state(history: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Tri-state response evidence from a market's observation history.

    `observed_flat` and `moved` both require at least MIN_OBSERVATIONS priced
    observations spanning MIN_WINDOW_HOURS. Anything less is `unknown` —
    absence of data is never evidence of a lazy mark.
    """
    priced = [(r, _observed_price(r)) for r in history]
    priced = [(r, p) for r, p in priced if p is not None]
    if len(priced) < MIN_OBSERVATIONS:
        return {"state": "unknown", "reason": f"{len(priced)} priced observations (<{MIN_OBSERVATIONS})"}

    window = _window_hours([r for r, _ in priced])
    prices = [p for _, p in priced]
    price_range = max(prices) - min(prices)
    first_volume = priced[0][0].get("volume")
    last_volume = priced[-1][0].get("volume")
    volume_delta = (
        last_volume - first_volume
        if first_volume is not None and last_volume is not None
        else None  # a delta against an unobserved baseline is fabricated data
    )

    if window < MIN_WINDOW_HOURS:
        return {
            "state": "unknown",
            "reason": f"window {window:.1f}h < {MIN_WINDOW_HOURS:.0f}h",
            "price_range": round(price_range, 4),
        }

    state = "moved" if price_range >= MOVED_PRICE_DELTA else "observed_flat"
    return {
        "state": state,
        "observations": len(priced),
        "window_hours": round(window, 1),
        "price_range": round(price_range, 4),
        "first_price": round(prices[0], 4),
        "last_price": round(prices[-1], 4),
        "volume_delta": round(volume_delta, 2) if volume_delta is not None else None,
    }


def attention_state(latest: dict[str, Any] | None) -> dict[str, Any]:
    """
    Tri-state attention evidence from the most recent observation's flow.

    Honesty note: public flow cannot reveal watchers or participant identity;
    this is observed trading attention only. Missing flow data is `unknown`.
    """
    if not latest:
        return {"state": "unknown", "reason": "no observations"}
    volume_24h = latest.get("volume_24h")
    if volume_24h is None:
        return {"state": "unknown", "reason": "no 24h volume data"}
    return {"state": _attention_bucket(volume_24h), "volume_24h": volume_24h}


def list_collection_runs(conn: sqlite3.Connection | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent collection runs with coverage stats, newest first."""
    own = conn is None
    if own:
        if not DB_PATH.exists():
            return []
        conn = open_db()
    try:
        rows = []
        for r in conn.execute(
            "SELECT observed_at, market_count, new_market_count, changed_count, provenance"
            " FROM collection_runs ORDER BY observed_at DESC LIMIT ?", (limit,)
        ).fetchall():
            row = dict(r)
            try:  # coverage-audit data (truncation warnings, fetch errors, sweep counts)
                row["provenance"] = json.loads(row.get("provenance") or "{}")
            except (TypeError, ValueError):
                row["provenance"] = {}
            rows.append(row)
        return rows
    finally:
        if own:
            conn.close()


async def fetch_universe(
    adapter,
    page_limit: int = 200,
    max_pages: int = 120,
    statuses: tuple[str, ...] = ("open", "unopened"),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Fetch the tradable market universe by walking cursor-paginated /events
    with nested markets until the cursor is exhausted.

    The bulk /markets firehose is dominated by auto-generated provisional
    multivariate (parlay) combo tickers — empty books created by the tens of
    thousands daily — which drowned the real universe and churned pagination
    (the August 2026 store held 126k tickers with one observation each).
    Walking events avoids the firehose; any parlay leg that still appears is
    skipped and counted in diagnostics so the exclusion stays auditable.
    Events also carry series_ticker, which markets need for peer grouping.
    """
    page_limit = max(1, min(int(page_limit), 200))  # /events rejects limit > 200
    markets: list[dict[str, Any]] = []
    seen: set[str] = set()
    mve_skipped = 0
    batches: list[dict[str, Any]] = []
    for status in statuses:
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_limit, "with_nested_markets": True, "status": status}
            if cursor:
                params["cursor"] = cursor
            resp = await adapter._request("GET", "/events", params=params, silent=True)
            if not isinstance(resp, dict) or "events" not in resp:
                batches.append({"status": status, "events": 0, "error": str(resp)[:200]})
                break
            events = resp.get("events") or []
            added = 0
            for event in events:
                event_ticker = event.get("event_ticker")
                series_ticker = event.get("series_ticker")
                for raw in event.get("markets") or []:
                    ticker = str(raw.get("ticker") or "")
                    if not ticker or ticker in seen:
                        continue
                    if raw.get("mve_collection_ticker"):
                        mve_skipped += 1
                        continue
                    seen.add(ticker)
                    raw["event_ticker"] = raw.get("event_ticker") or event_ticker
                    raw["series_ticker"] = raw.get("series_ticker") or series_ticker
                    markets.append(raw)
                    added += 1
            batches.append({"status": status, "events": len(events), "added": added})
            cursor = resp.get("cursor")
            if not cursor:
                break
        else:
            batches.append({"status": status, "warning": f"page cap {max_pages} hit before cursor exhausted"})
    return markets, {"batches": batches, "mve_skipped": mve_skipped}


async def collect_snapshot(
    adapter,
    observed_at: datetime,
    max_pages: int = 120,
    page_limit: int = 200,
    db_path: Path = DB_PATH,
    sweeps: int = 2,
) -> dict[str, int]:
    """
    Fetch the market universe (category-blind) and record one collection run.

    Kalshi's cursor pagination can be unstable between sweeps — consecutive
    identical fetches may differ — so a run unions multiple sweeps by ticker
    and records per-sweep counts in the run's provenance. Coverage gaps must
    be auditable, not silent.
    """
    merged: dict[str, dict[str, Any]] = {}
    sweep_counts: list[int] = []
    batches: list[Any] = []
    mve_skipped = 0
    for _ in range(max(1, sweeps)):
        raw_markets, diagnostics = await fetch_universe(adapter, page_limit=page_limit, max_pages=max_pages)
        sweep_counts.append(len(raw_markets))
        batches.append(diagnostics.get("batches", []))
        mve_skipped += int(diagnostics.get("mve_skipped") or 0)
        for raw in raw_markets:
            ticker = str(raw.get("ticker") or "")
            if ticker and ticker not in merged:
                merged[ticker] = raw

    if not merged:
        # A zero-market run recorded as success would silently poison the
        # coverage history; fail loudly so launchd/the API surface it.
        raise RuntimeError(f"collection fetched 0 markets; fetch batches: {str(batches)[:500]}")

    provenance = {
        "max_pages": max_pages,
        "page_limit": page_limit,
        "sweeps": len(sweep_counts),
        "sweep_market_counts": sweep_counts,
        "union_market_count": len(merged),
        "mve_skipped": mve_skipped,
        "fetch_batches": batches,
    }
    conn = open_db(db_path)
    try:
        return record_observations(conn, list(merged.values()), observed_at, provenance=provenance)
    finally:
        conn.close()
