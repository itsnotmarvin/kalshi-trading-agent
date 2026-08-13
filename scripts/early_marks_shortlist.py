#!/usr/bin/env python3
"""
Pull a rough Stage 1 shortlist from the observation store.

Usage (from kalshi-agent/):
    python3 scripts/early_marks_shortlist.py [--run ISO_TS] [--limit N] [--proxy]

Evaluates every market's tri-state evidence AS OF one collection run
(default: the latest) — observations and last_seen confirmations after that
run are excluded, so a shortlist regenerated later for the same run is
identical. Prints the full state distribution, then the candidates:
markets whose history shows `observed_flat` response and `low` attention.

--proxy additionally lists markets that are priced, low-attention, and flat
so far but BELOW the evidence threshold (window < 6h or < 2 observations).
Their response state is `unknown` — they are NOT Stage 1 candidates and are
printed only for workflow dry runs (verdicts against them must use a
`dryrun-*` shortlist_ref; evaluation excludes those).

Output is deliberately observational only: no direction, fair value,
expected price, ROI, entry/exit, sizing, or recommendation status (the
Stage 1 forbidden-outputs contract).
"""
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.early_marks_snapshots import (  # noqa: E402
    MOVED_PRICE_DELTA,
    _observed_price,
    attention_state,
    open_db,
    response_state,
)


def histories_as_of(conn, run_ts: str) -> dict[str, list[dict]]:
    """
    Observation histories cut to the chosen run, oldest first, with the
    synthetic trailing confirmation capped at min(last_seen, run_ts) —
    knowledge from after the run must not leak into its shortlist.
    """
    histories: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT * FROM observations WHERE observed_at <= ? ORDER BY ticker, observed_at",
        (run_ts,),
    ):
        histories[r["ticker"]].append(dict(r))
    for ticker, last_seen in conn.execute("SELECT ticker, last_seen FROM markets"):
        rows = histories.get(ticker)
        confirmed_at = min(last_seen, run_ts)
        if rows and confirmed_at > rows[-1]["observed_at"]:
            confirmed = dict(rows[-1])
            confirmed["observed_at"] = confirmed_at
            confirmed["confirmed"] = True
            rows.append(confirmed)
    return histories


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", help="collection run observed_at (default: latest)")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--proxy", action="store_true",
                        help="also list below-threshold dry-run proxies")
    args = parser.parse_args()

    conn = open_db()
    try:
        if args.run:
            row = conn.execute(
                "SELECT observed_at FROM collection_runs WHERE observed_at = ?",
                (args.run,),
            ).fetchone()
            if row is None:
                print(f"no collection run at {args.run!r}", file=sys.stderr)
                return 1
            run_ts = row["observed_at"]
        else:
            run_ts = conn.execute(
                "SELECT MAX(observed_at) AS ts FROM collection_runs"
            ).fetchone()["ts"]
            if run_ts is None:
                print("observation store has no collection runs", file=sys.stderr)
                return 1

        meta = {
            r["ticker"]: dict(r)
            for r in conn.execute("SELECT ticker, event_ticker, series_ticker FROM markets")
        }
        histories = histories_as_of(conn, run_ts)
    finally:
        conn.close()

    states = Counter()
    candidates, proxies = [], []
    for ticker, history in histories.items():
        latest = history[-1]
        if latest.get("status") not in (None, "active", "open", "initialized", "unopened"):
            states["inactive"] += 1
            continue
        response = response_state(history)
        attention = attention_state(latest)
        states[f"{response['state']}/{attention['state']}"] += 1
        if response["state"] == "observed_flat" and attention["state"] == "low":
            candidates.append((ticker, response, attention, latest))
        elif (
            args.proxy
            and response["state"] == "unknown"
            and attention["state"] == "low"
            and _observed_price(latest) is not None
        ):
            prices = [p for p in (_observed_price(r) for r in history) if p is not None]
            if len(prices) >= 2 and max(prices) - min(prices) < MOVED_PRICE_DELTA:
                proxies.append((ticker, response, attention, latest))

    print(f"shortlist as of run {run_ts}")
    print(f"markets evaluated: {sum(states.values())}")
    for state, count in states.most_common():
        print(f"  {state}: {count}")

    print(f"\ncandidates (observed_flat + low attention): {len(candidates)}")
    for ticker, response, attention, latest in candidates[: args.limit]:
        m = meta.get(ticker, {})
        print(
            f"  {ticker}  event={m.get('event_ticker')}  series={m.get('series_ticker')}"
            f"  price={_observed_price(latest)}  window={response.get('window_hours')}h"
            f"  obs={response.get('observations')}  vol24h={attention.get('volume_24h')}"
        )

    if args.proxy:
        print(
            f"\nDRY-RUN PROXIES — below evidence threshold, response=unknown,"
            f" NOT Stage 1 candidates: {len(proxies)}"
        )
        for ticker, response, attention, latest in proxies[: args.limit]:
            m = meta.get(ticker, {})
            print(
                f"  {ticker}  event={m.get('event_ticker')}  series={m.get('series_ticker')}"
                f"  price={_observed_price(latest)}  reason={response.get('reason')}"
                f"  vol24h={attention.get('volume_24h')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
