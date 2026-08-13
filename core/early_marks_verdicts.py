"""
Early Marks V2 — Stage 2A verdict store.

Research adjudications need a durable, queryable home or the evaluation
funnel (surfaced → researched → verified influence → repriced) can never be
measured. This module persists verdicts append-only in SQLite, separate from
observations.db so paste-time writes never contend with the 30-minute
collector.

The validation encodes the Stage 2A contract asymmetrically on purpose:
`no_relevant_influence` — the verdict the pipeline most needs recorded and
the one most tempting to skip — has the smallest required payload, while
`verified_influence` and `already_priced` must carry at least one source and
a structured influence record (what, when, causal channel, settlement-rule
relevance, pricing assessment). An adjudication that cannot fill those
fields is `insufficient_data`, which requires only a reason.

Rows are append-only: corrections are new rows, never UPDATEs, so the
adjudication history stays auditable. Timestamps are injected and must be
timezone-aware. `shortlist_ref` ties a verdict to the collection run whose
shortlist surfaced the ticker; `dryrun-*` refs mark workflow rehearsals that
evaluation must exclude. See docs/early-marks-evaluation-spec.md §2e.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config.paths import RUNTIME_DATA_DIR
from core import early_marks_snapshots

DB_PATH = RUNTIME_DATA_DIR / "early_marks" / "verdicts.db"

VERDICTS = (
    "verified_influence",
    "no_relevant_influence",
    "already_priced",
    "insufficient_data",
)
# Verdicts that claim an influence exists must describe it well enough to be
# audited later: these keys mirror the Stage 2A research questions in the V2
# detection spec.
INFLUENCE_KEYS = (
    "influence",
    "timing",
    "causal_channel",
    "settlement_relevance",
    "pricing_assessment",
)
DRY_RUN_PREFIX = "dryrun-"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    shortlist_ref TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN (
        'verified_influence', 'no_relevant_influence',
        'already_priced', 'insufficient_data')),
    influence TEXT,
    sources TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL,
    template_version TEXT,
    retrieval TEXT,
    notes TEXT,
    researched_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_ticker ON verdicts (ticker);
CREATE INDEX IF NOT EXISTS idx_verdicts_ref ON verdicts (shortlist_ref);
CREATE INDEX IF NOT EXISTS idx_verdicts_verdict ON verdicts (verdict);
CREATE INDEX IF NOT EXISTS idx_verdicts_researched ON verdicts (researched_at);
"""


def open_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def _aware_iso(value: Any, field: str) -> str:
    """Parse a timestamp and insist on timezone-awareness."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}: not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}: naive timestamps are ambiguous; include an offset")
    return parsed.isoformat()


def _require_text(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def validate_verdict(
    payload: dict[str, Any],
    snapshots_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """
    Normalize and validate one verdict payload; raise ValueError on any
    violation. Returns the record ready for insertion (influence/sources as
    JSON strings).
    """
    if not isinstance(payload, dict):
        raise ValueError("verdict payload must be a JSON object")

    verdict = _require_text(payload, "verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    record = {
        "ticker": _require_text(payload, "ticker"),
        "shortlist_ref": _require_text(payload, "shortlist_ref"),
        "verdict": verdict,
        "model": _require_text(payload, "model"),
        "template_version": str(payload.get("template_version") or "") or None,
        "retrieval": str(payload.get("retrieval") or "") or None,
        "notes": str(payload.get("notes") or "") or None,
        "researched_at": _aware_iso(payload.get("researched_at"), "researched_at"),
    }

    sources = payload.get("sources") or []
    if not isinstance(sources, list) or any(
        not isinstance(s, str) or not s.strip() for s in sources
    ):
        raise ValueError("sources must be a list of non-empty strings")
    record["sources"] = json.dumps(sources)

    influence = payload.get("influence")
    if influence is not None and not isinstance(influence, dict):
        raise ValueError("influence must be a JSON object when present")
    if verdict in ("verified_influence", "already_priced"):
        if not sources:
            raise ValueError(f"{verdict} requires at least one source")
        missing = [
            k for k in INFLUENCE_KEYS
            if not str((influence or {}).get(k) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"{verdict} requires a structured influence record;"
                f" missing: {', '.join(missing)}"
            )
    if verdict == "insufficient_data" and not record["notes"]:
        raise ValueError("insufficient_data requires a reason in notes")
    record["influence"] = json.dumps(influence) if influence is not None else None

    ref = record["shortlist_ref"]
    if not ref.startswith(DRY_RUN_PREFIX):
        own_snapshots = snapshots_conn is None
        if own_snapshots:
            if not early_marks_snapshots.DB_PATH.exists():
                raise ValueError(
                    f"shortlist_ref {ref!r} cannot be verified: no observation store"
                )
            snapshots_conn = early_marks_snapshots.open_db()
        try:
            run = snapshots_conn.execute(
                "SELECT 1 FROM collection_runs WHERE observed_at = ?", (ref,)
            ).fetchone()
        finally:
            if own_snapshots:
                snapshots_conn.close()
        if run is None:
            raise ValueError(
                f"shortlist_ref {ref!r} matches no collection run;"
                f" use a run observed_at or a '{DRY_RUN_PREFIX}' ref"
            )
    return record


def record_verdict(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    recorded_at: datetime,
    snapshots_conn: sqlite3.Connection | None = None,
) -> int:
    """Validate and append one verdict; returns its verdict_id."""
    record = validate_verdict(payload, snapshots_conn=snapshots_conn)
    record["recorded_at"] = _aware_iso(recorded_at, "recorded_at")
    cur = conn.execute(
        "INSERT INTO verdicts (ticker, shortlist_ref, verdict, influence, sources,"
        " model, template_version, retrieval, notes, researched_at, recorded_at)"
        " VALUES (:ticker, :shortlist_ref, :verdict, :influence, :sources, :model,"
        " :template_version, :retrieval, :notes, :researched_at, :recorded_at)",
        record,
    )
    conn.commit()
    return int(cur.lastrowid)


def list_verdicts(
    conn: sqlite3.Connection | None = None,
    ticker: str | None = None,
    shortlist_ref: str | None = None,
    include_dry_runs: bool = True,
) -> list[dict[str, Any]]:
    """Verdicts oldest-first, with sources/influence parsed back to objects."""
    own = conn is None
    if own:
        if not DB_PATH.exists():
            return []
        conn = open_db()
    try:
        query = "SELECT * FROM verdicts WHERE 1=1"
        params: list[Any] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if shortlist_ref:
            query += " AND shortlist_ref = ?"
            params.append(shortlist_ref)
        if not include_dry_runs:
            query += " AND shortlist_ref NOT LIKE ?"
            params.append(f"{DRY_RUN_PREFIX}%")
        rows = []
        for r in conn.execute(query + " ORDER BY verdict_id", params).fetchall():
            row = dict(r)
            row["sources"] = json.loads(row.get("sources") or "[]")
            row["influence"] = (
                json.loads(row["influence"]) if row.get("influence") else None
            )
            rows.append(row)
        return rows
    finally:
        if own:
            conn.close()
