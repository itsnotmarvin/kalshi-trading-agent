#!/usr/bin/env python3
"""
Record one Stage 2A research verdict from a pasted JSON object.

Usage (from kalshi-agent/):
    pbpaste | python3 scripts/record_verdict.py
    python3 scripts/record_verdict.py '{"ticker": ..., "verdict": ...}'

The payload is the JSON block a Stage 2A research session ends with (see
docs/stage2a-research-template.md). `researched_at` defaults to now so the
paste stays minimal; validation rules live in core/early_marks_verdicts.py.
Exits nonzero with the reason on stderr when the payload is invalid —
nothing is written on failure.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.early_marks_verdicts import open_db, record_verdict  # noqa: E402


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    if isinstance(payload, dict):
        payload.setdefault("researched_at", now.isoformat())
    conn = open_db()
    try:
        verdict_id = record_verdict(conn, payload, recorded_at=now)
    except ValueError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(
        f"recorded verdict_id={verdict_id}"
        f" ticker={payload.get('ticker')} verdict={payload.get('verdict')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
