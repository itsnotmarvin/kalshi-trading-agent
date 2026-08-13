#!/usr/bin/env python3
"""
Collect one point-in-time snapshot of the Kalshi market universe.

Usage (from kalshi-agent/):
    python3 scripts/collect_snapshot.py [max_pages] [page_limit]

Run this on a schedule (e.g. a launchd interval job) to build the observation
history that Early Marks V2 candidate generation requires — non-response is a
claim about history and cannot be measured from a single scan.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.kalshi_adapter import KalshiAdapter  # noqa: E402
from core.early_marks_snapshots import collect_snapshot  # noqa: E402


async def main() -> int:
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    page_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    observed_at = datetime.now(timezone.utc)
    summary = await collect_snapshot(
        KalshiAdapter(), observed_at, max_pages=max_pages, page_limit=page_limit
    )
    print(
        f"{observed_at.isoformat()} snapshot: {summary['seen']} markets seen,"
        f" {summary['new_markets']} new, {summary['changed']} changed,"
        f" {summary['unchanged']} unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
