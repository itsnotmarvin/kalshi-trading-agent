#!/usr/bin/env python3
"""
Generate the weekly Early Marks premise-check report.

Usage (from kalshi-agent/):
    python3 scripts/early_marks_premise_report.py [--as-of ISO_TS] [--no-notify]

Runs the spec §2a premise check against the observation store, writes a
markdown report to data/runtime/early_marks/reports/, prints the headline,
and (on macOS) posts a notification so the schedule announces itself instead
of being polled. Run by the com.marbin.kalshi-early-marks-report launchd job
weekly; safe to run by hand at any time — labeling is deterministic for a
given --as-of.
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.paths import RUNTIME_DATA_DIR  # noqa: E402
from core.early_marks_labels import premise_check, render_premise_report  # noqa: E402
from core.early_marks_snapshots import DB_PATH, open_db  # noqa: E402

REPORTS_DIR = RUNTIME_DATA_DIR / "early_marks" / "reports"


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification; never fails the report."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="evaluation timestamp (default: now UTC)")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    as_of = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of else datetime.now(timezone.utc)
    )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    if not DB_PATH.exists():
        print("observation store does not exist yet", file=sys.stderr)
        return 1

    conn = open_db()
    try:
        result = premise_check(conn, as_of)
    finally:
        conn.close()

    report = render_premise_report(result)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"premise-{as_of.date().isoformat()}.md"
    out_path.write_text(report)

    lift = result["lift_vs_matched_random"]
    episodes = result["repriced_episodes"]
    headline = (
        f"{episodes} repriced episodes; candidate lift "
        f"{f'{lift:.2f}x' if lift is not None else 'n/a'}; "
        f"episode floor {'met' if result['episode_floor_met'] else 'not met (needs 200)'};"
        f" checkpoint 2026-09-15"
    )
    print(f"report written: {out_path}")
    print(headline)
    if not args.no_notify:
        notify("Early Marks premise check", headline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
