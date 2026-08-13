"""
Tests for the Stage 2A verdict store.

The load-bearing asymmetry: `no_relevant_influence` — the verdict most
tempting to skip recording — must be the EASIEST to store, while verdicts
claiming an influence (`verified_influence`, `already_priced`) must carry
sources and a structured influence record or be rejected. Rows are
append-only so adjudication history stays auditable.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.early_marks_snapshots import open_db as open_snapshots_db
from core.early_marks_snapshots import record_observations
from core.early_marks_verdicts import (
    INFLUENCE_KEYS,
    list_verdicts,
    open_db,
    record_verdict,
    validate_verdict,
)

NOW = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc)


def _payload(**overrides):
    payload = {
        "ticker": "KXTEST-1",
        "shortlist_ref": "dryrun-2026-08-12",
        "verdict": "no_relevant_influence",
        "model": "test-model",
        "researched_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def _influence():
    return {k: f"test {k}" for k in INFLUENCE_KEYS}


def test_minimal_no_relevant_influence_is_recordable(tmp_path):
    # The schema's design goal: the "nothing here" verdict needs only
    # ticker, ref, verdict, model, and a timestamp.
    conn = open_db(tmp_path / "verdicts.db")
    try:
        verdict_id = record_verdict(conn, _payload(), recorded_at=NOW)
        rows = list_verdicts(conn=conn)
        assert rows[0]["verdict_id"] == verdict_id
        assert rows[0]["verdict"] == "no_relevant_influence"
        assert rows[0]["sources"] == []
        assert rows[0]["influence"] is None
    finally:
        conn.close()


def test_unknown_verdict_is_rejected():
    with pytest.raises(ValueError, match="verdict must be one of"):
        validate_verdict(_payload(verdict="actionable"))


def test_influence_claims_require_sources_and_structure():
    # verified_influence with no sources: rejected.
    with pytest.raises(ValueError, match="at least one source"):
        validate_verdict(_payload(verdict="verified_influence", influence=_influence()))
    # ...with sources but a hollow influence record: rejected, naming gaps.
    partial = {"influence": "something", "timing": "soon"}
    with pytest.raises(ValueError, match="causal_channel"):
        validate_verdict(_payload(
            verdict="verified_influence", sources=["https://example.com"],
            influence=partial,
        ))
    # already_priced holds the same evidentiary bar.
    with pytest.raises(ValueError, match="at least one source"):
        validate_verdict(_payload(verdict="already_priced", influence=_influence()))
    # Fully evidenced: accepted.
    record = validate_verdict(_payload(
        verdict="verified_influence", sources=["https://example.com"],
        influence=_influence(),
    ))
    assert json.loads(record["influence"])["timing"] == "test timing"


def test_insufficient_data_requires_a_reason():
    with pytest.raises(ValueError, match="reason in notes"):
        validate_verdict(_payload(verdict="insufficient_data"))
    validate_verdict(_payload(verdict="insufficient_data", notes="no sources found"))


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="naive"):
        validate_verdict(_payload(researched_at="2026-08-12T22:00:00"))


def test_real_shortlist_ref_must_match_a_collection_run(tmp_path):
    snapshots = open_snapshots_db(tmp_path / "observations.db")
    try:
        record_observations(snapshots, [{"ticker": "KXTEST-1"}], NOW)
        # A ref that is not a run and not dryrun-*: rejected.
        with pytest.raises(ValueError, match="matches no collection run"):
            validate_verdict(
                _payload(shortlist_ref="2020-01-01T00:00:00+00:00"),
                snapshots_conn=snapshots,
            )
        # The actual run timestamp: accepted.
        validate_verdict(
            _payload(shortlist_ref=NOW.isoformat()), snapshots_conn=snapshots
        )
    finally:
        snapshots.close()


def test_verdicts_are_append_only_and_dry_runs_filterable(tmp_path):
    conn = open_db(tmp_path / "verdicts.db")
    try:
        record_verdict(conn, _payload(), recorded_at=NOW)
        # A correction is a second row, not an overwrite.
        record_verdict(
            conn,
            _payload(verdict="insufficient_data", notes="revisited: sources conflict"),
            recorded_at=NOW,
        )
        assert len(list_verdicts(conn=conn, ticker="KXTEST-1")) == 2
        # Evaluation path: dryrun-* refs excluded.
        assert list_verdicts(conn=conn, include_dry_runs=False) == []
    finally:
        conn.close()


def test_ingest_cli_records_and_rejects(tmp_path, monkeypatch):
    # The paste path: valid JSON records and reports its id; garbage exits
    # nonzero with the reason on stderr and writes nothing.
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "record_verdict.py"

    ok = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(_payload()),
        capture_output=True, text=True,
    )
    assert ok.returncode == 0, ok.stderr
    assert "verdict_id=1" in ok.stdout

    bad = subprocess.run(
        [sys.executable, str(script)], input="not json",
        capture_output=True, text=True,
    )
    assert bad.returncode == 1
    assert "invalid JSON" in bad.stderr

    rejected = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(_payload(verdict="verified_influence")),
        capture_output=True, text=True,
    )
    assert rejected.returncode == 1
    assert "at least one source" in rejected.stderr
