"""
Early Marks V2 — repricing labels and the premise check.

Implements docs/early-marks-evaluation-spec.md §1–2b: the tri-state label
(`repriced | not_repriced | unlabelable`, plus internal `pending` for label
windows that have not elapsed yet), and the whole-universe premise check —
do markets in the candidate state (observed_flat + low attention) go on to
reprice more often than matched controls?

Evaluation shares the store's definitions by construction: observed price,
attention buckets, and response state are imported from
core.early_marks_snapshots, never re-implemented. Timestamps (`as_of`) are
always injected so labeling is deterministic and testable with synthetic
histories.

Honesty notes baked into the implementation:
- Movement is measured in log-odds (prices clamped to [0.01, 0.99]); a
  repricing must also CROSS the book (later bid at/above the flag-time ask,
  or mirrored downward) — mid drift inside a wide spread never qualifies.
- Synthetic `confirmed` trailing observations prove coverage (the market was
  seen unchanged) but are never priced endpoints for a move (spec §3).
- Coverage uses the market's latest sighting; without a per-run presence
  table a mid-window disappearance that later reappears can overstate
  coverage. Known limitation, inherited from the store, stated here rather
  than hidden.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from core.early_marks_snapshots import (
    _attention_bucket,
    _observed_price,
    attention_state,
    response_state,
)

SPEC_VERSION = "early-marks-evaluation-spec 2026-08-12"

PRIMARY_DELTA_L = 0.5
PRIMARY_HORIZON_DAYS = 14.0
SETTLEMENT_EMBARGO_HOURS = 48.0
COVERAGE_FRACTION = 0.8
SENSITIVITY_DELTAS = (0.35, 0.7)
SENSITIVITY_HORIZONS = (7.0, 30.0)

# Statuses that indicate determination/settlement (label windows end an
# embargo before the first of these) vs. statuses eligible for bucketing.
SETTLEMENT_STATUSES = {"determined", "settled", "finalized"}
ACTIVE_STATUSES = {"active", "open", "initialized", "unopened"}

_PRICE_CLAMP = (0.01, 0.99)
# |log-odds| band edges: ~30–70%, ~10–30% / 70–90%, tails.
_BAND_EDGES = (0.85, 2.2)
_BAND_NAMES = ("mid", "lean", "tail")

DESIGNATED_RUN_HOUR_UTC = 12


def logit(p: float) -> float:
    p = min(max(p, _PRICE_CLAMP[0]), _PRICE_CLAMP[1])
    return math.log(p / (1.0 - p))


def price_band(p: float) -> str:
    magnitude = abs(logit(p))
    for edge, name in zip(_BAND_EDGES, _BAND_NAMES):
        if magnitude < edge:
            return name
    return _BAND_NAMES[-1]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _valid_book(row: dict[str, Any]) -> tuple[float, float] | None:
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        return bid, ask
    return None


def label_market(
    history: list[dict[str, Any]],
    t0: datetime,
    as_of: datetime,
    close_time: Any = None,
    horizon_days: float = PRIMARY_HORIZON_DAYS,
    delta_l: float = PRIMARY_DELTA_L,
    embargo_hours: float = SETTLEMENT_EMBARGO_HOURS,
) -> dict[str, Any]:
    """
    Label one market's forward repricing from flag time `t0`, judged with
    knowledge through `as_of`. `history` is the FULL observation history
    (stored rows oldest-first, optionally ending in a synthetic `confirmed`
    row); rows at or before t0 provide the reference state, rows after it
    the outcome.
    """
    stored = [r for r in history if not r.get("confirmed")]
    at_or_before = [r for r in stored if _parse_ts(r["observed_at"]) <= t0]
    if not at_or_before:
        return {"label": "unlabelable", "reason": "no observation at flag time"}
    t0_row = at_or_before[-1]
    p0 = _observed_price(t0_row)
    if p0 is None:
        return {"label": "unlabelable", "reason": "no priced observation at flag time"}
    book0 = _valid_book(t0_row)

    window_end = t0 + timedelta(days=horizon_days)
    embargo = timedelta(hours=embargo_hours)
    close_dt = _parse_ts(close_time)
    if close_dt is not None:
        window_end = min(window_end, close_dt - embargo)
    for row in stored:
        if str(row.get("status") or "").lower() in SETTLEMENT_STATUSES:
            settle_dt = _parse_ts(row["observed_at"])
            if settle_dt is not None:
                window_end = min(window_end, settle_dt - embargo)
            break
    if window_end <= t0:
        return {"label": "unlabelable", "reason": "label window empty (close/settlement embargo)"}

    l0 = logit(p0)
    for row in stored:
        ts = _parse_ts(row["observed_at"])
        if ts is None or ts <= t0 or ts > window_end:
            continue
        price = _observed_price(row)
        if price is None:
            continue
        delta = logit(price) - l0
        if abs(delta) < delta_l:
            continue
        book_t = _valid_book(row)
        if book0 is None or book_t is None:
            continue  # crossing cannot be certified without both books
        crossed = (delta > 0 and book_t[0] >= book0[1]) or (delta < 0 and book_t[1] <= book0[0])
        if crossed:
            return {
                "label": "repriced",
                "repriced_at": row["observed_at"],
                "delta_logodds": round(abs(delta), 4),
                "p0": round(p0, 4),
                "p1": round(price, 4),
            }

    if as_of < window_end:
        return {"label": "pending", "reason": f"window open until {window_end.isoformat()}"}

    coverage_deadline = t0 + (window_end - t0) * COVERAGE_FRACTION
    seen_through = max(
        (t for t in (_parse_ts(r["observed_at"]) for r in history) if t is not None),
        default=None,
    )
    if seen_through is None or seen_through < coverage_deadline:
        return {"label": "unlabelable", "reason": "coverage below 80% of label window"}
    return {"label": "not_repriced", "p0": round(p0, 4)}


def evidence_bucket(history: list[dict[str, Any]]) -> str:
    """Spec §2a buckets from the store's own evidence functions."""
    response = response_state(history)["state"]
    if response == "moved":
        return "moved"
    if response == "observed_flat":
        attention = attention_state(history[-1] if history else None)["state"]
        if attention == "low":
            return "candidate"
        if attention in ("normal", "high"):
            return "flat_watched"
    return "unknown"


def designated_runs(run_timestamps: list[str]) -> list[str]:
    """One run per UTC day: the run nearest 12:00 UTC (spec §2a)."""
    by_day: dict[Any, list[datetime]] = defaultdict(list)
    for ts in run_timestamps:
        parsed = _parse_ts(ts)
        if parsed is not None:
            by_day[parsed.date()].append(parsed)
    chosen = []
    for day, stamps in sorted(by_day.items()):
        noon = datetime(day.year, day.month, day.day, DESIGNATED_RUN_HOUR_UTC, tzinfo=timezone.utc)
        chosen.append(min(stamps, key=lambda s: abs(s - noon)).isoformat())
    return chosen


def _load_store(conn) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM observations ORDER BY ticker, observed_at"):
        histories[r["ticker"]].append(dict(r))
    markets = {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM markets")}
    return histories, markets


def _history_as_of(rows: list[dict[str, Any]], last_seen: str, run_ts: str) -> list[dict[str, Any]]:
    """Rows through run_ts plus the capped synthetic confirmation (leak-safe)."""
    cut = bisect_right([r["observed_at"] for r in rows], run_ts)
    slice_ = rows[:cut]
    if not slice_:
        return slice_
    confirmed_at = min(last_seen, run_ts)
    if confirmed_at > slice_[-1]["observed_at"]:
        tail = dict(slice_[-1])
        tail["observed_at"] = confirmed_at
        tail["confirmed"] = True
        slice_ = slice_ + [tail]
    return slice_


def premise_check(conn, as_of: datetime) -> dict[str, Any]:
    """
    Spec §2a–2b: bucket every market at each designated daily run, label its
    forward repricing, and compare the candidate bucket's reprice rate to
    matched controls. Returns a result dict for render_premise_report.
    """
    histories, markets = _load_store(conn)
    run_rows = [r["observed_at"] for r in conn.execute(
        "SELECT observed_at FROM collection_runs ORDER BY observed_at"
    )]
    runs = designated_runs(run_rows)

    # A series is "recurring" at t0 when it existed a full horizon earlier.
    series_first_seen: dict[str, str] = {}
    for m in markets.values():
        series = m.get("series_ticker")
        if series:
            first = m.get("first_seen") or ""
            if series not in series_first_seen or first < series_first_seen[series]:
                series_first_seen[series] = first

    variants = [("primary", PRIMARY_DELTA_L, PRIMARY_HORIZON_DAYS)]
    variants += [(f"dL={d}", d, PRIMARY_HORIZON_DAYS) for d in SENSITIVITY_DELTAS]
    variants += [(f"H={int(h)}d", PRIMARY_DELTA_L, h) for h in SENSITIVITY_HORIZONS]

    pairs: list[dict[str, Any]] = []
    counts = {"evaluated": 0, "pending": 0, "unlabelable": 0}
    sensitivity: dict[str, dict[str, int]] = {
        name: {"candidate_repriced": 0, "candidate_decided": 0} for name, _, _ in variants
    }

    # Outcome-side history: full rows plus the synthetic last_seen tail, so a
    # dormant market's "seen unchanged through last_seen" counts as coverage.
    full_histories = {
        ticker: _history_as_of(rows, markets.get(ticker, {}).get("last_seen")
                               or rows[-1]["observed_at"],
                               markets.get(ticker, {}).get("last_seen")
                               or rows[-1]["observed_at"])
        for ticker, rows in histories.items()
    }

    for run_ts in runs:
        t0 = _parse_ts(run_ts)
        for ticker, rows in histories.items():
            if rows[0]["observed_at"] > run_ts:
                continue
            market = markets.get(ticker, {})
            history = _history_as_of(rows, market.get("last_seen") or run_ts, run_ts)
            if not history:
                continue
            if str(history[-1].get("status") or "").lower() not in ACTIVE_STATUSES:
                continue
            counts["evaluated"] += 1
            primary = label_market(full_histories[ticker], t0, as_of, close_time=market.get("close_time"))
            if primary["label"] in ("pending", "unlabelable"):
                counts[primary["label"]] += 1
                continue

            bucket = evidence_bucket(history)
            p0 = primary.get("p0")
            series = market.get("series_ticker")
            recurring = bool(
                series
                and series_first_seen.get(series)
                and _parse_ts(series_first_seen[series]) is not None
                and _parse_ts(series_first_seen[series]) <= t0 - timedelta(days=PRIMARY_HORIZON_DAYS)
            )
            pairs.append({
                "ticker": ticker,
                "run": run_ts,
                "bucket": bucket,
                "band": price_band(p0) if p0 is not None else "mid",
                "attention": _attention_bucket(history[-1].get("volume_24h")),
                "repriced": primary["label"] == "repriced",
                "recurring": recurring,
            })
            if bucket == "candidate":
                for name, d, h in variants:
                    if name == "primary":
                        result = primary
                    else:
                        result = label_market(
                            full_histories[ticker], t0, as_of,
                            close_time=market.get("close_time"),
                            delta_l=d, horizon_days=h,
                        )
                    if result["label"] in ("repriced", "not_repriced"):
                        sensitivity[name]["candidate_decided"] += 1
                        sensitivity[name]["candidate_repriced"] += result["label"] == "repriced"

    def rate(subset: list[dict[str, Any]]) -> float | None:
        return (sum(p["repriced"] for p in subset) / len(subset)) if subset else None

    buckets: dict[str, dict[str, Any]] = {}
    for name in ("candidate", "flat_watched", "moved", "unknown"):
        subset = [p for p in pairs if p["bucket"] == name]
        buckets[name] = {"n": len(subset), "reprice_rate": rate(subset)}

    candidates = [p for p in pairs if p["bucket"] == "candidate"]

    def matched_rate(cohort: list[dict[str, Any]]) -> float | None:
        """Cohort reprice rate reweighted to the candidates' band mix (§2b)."""
        if not candidates or not cohort:
            return None
        total, weighted = 0, 0.0
        for band in _BAND_NAMES:
            weight = sum(1 for c in candidates if c["band"] == band)
            band_cohort = [p for p in cohort if p["band"] == band]
            if weight and band_cohort:
                weighted += weight * rate(band_cohort)
                total += weight
        return weighted / total if total else None

    matched_random = matched_rate([p for p in pairs if p["attention"] == "low"])
    flat_watched = matched_rate([p for p in pairs if p["bucket"] == "flat_watched"])
    candidate_rate = rate(candidates)

    repriced_pairs = [p for p in pairs if p["repriced"]]
    episodes = len({p["ticker"] for p in repriced_pairs})
    decided = len(pairs)

    def lift(base: float | None) -> float | None:
        if candidate_rate is None or not base:
            return None
        return candidate_rate / base

    return {
        "spec_version": SPEC_VERSION,
        "as_of": as_of.isoformat(),
        "designated_runs": runs,
        "counts": {**counts, "decided": decided},
        "coverage_fraction": (decided / (decided + counts["unlabelable"]))
        if decided + counts["unlabelable"] else None,
        "buckets": buckets,
        "candidate_rate": candidate_rate,
        "matched_random_rate": matched_random,
        "flat_watched_rate": flat_watched,
        "lift_vs_matched_random": lift(matched_random),
        "lift_vs_flat_watched": lift(flat_watched),
        "p_candidate_given_repriced": (
            sum(1 for p in repriced_pairs if p["bucket"] == "candidate") / len(repriced_pairs)
        ) if repriced_pairs else None,
        "repriced_episodes": episodes,
        "episode_floor_met": episodes >= 200,  # necessary for the Sep 15 checkpoint, not sufficient
        "recurring_candidate_rate": rate([p for p in candidates if p["recurring"]]),
        "unseen_candidate_rate": rate([p for p in candidates if not p["recurring"]]),
        "sensitivity": sensitivity,
    }


def render_premise_report(result: dict[str, Any]) -> str:
    """Markdown per spec §5: no bare precision without its matched lift."""
    def fmt(value: float | None, pct: bool = True) -> str:
        if value is None:
            return "n/a"
        return f"{value * 100:.2f}%" if pct else f"{value:.2f}×"

    counts = result["counts"]
    lines = [
        "# Early Marks premise check",
        "",
        f"- graded under: {result['spec_version']}",
        f"- as of: {result['as_of']}",
        f"- designated runs: {len(result['designated_runs'])}"
        + (f" ({result['designated_runs'][0][:10]} → {result['designated_runs'][-1][:10]})"
           if result["designated_runs"] else ""),
        f"- (run, market) pairs — decided: {counts['decided']}, pending: {counts['pending']},"
        f" unlabelable: {counts['unlabelable']}",
        f"- labelable coverage: {fmt(result['coverage_fraction'])}",
        f"- repriced episodes (distinct markets): {result['repriced_episodes']}"
        f" — ≥200 floor {'met' if result['episode_floor_met'] else 'NOT met'}"
        f" (checkpoint itself is date-gated: 2026-09-15)",
        "",
        "## Forward reprice rate by evidence bucket",
        "",
        "| bucket | pairs | reprice rate |",
        "|---|---:|---:|",
    ]
    for name, stats in result["buckets"].items():
        lines.append(f"| {name} | {stats['n']} | {fmt(stats['reprice_rate'])} |")
    lines += [
        "",
        "## Headline (candidate bucket, matched-control lifts)",
        "",
        f"- P(reprice | candidate): {fmt(result['candidate_rate'])}",
        f"- vs matched-random (band-matched, low attention): {fmt(result['matched_random_rate'])}"
        f" → lift {fmt(result['lift_vs_matched_random'], pct=False)}",
        f"- vs flat-but-watched (band-matched): {fmt(result['flat_watched_rate'])}"
        f" → lift {fmt(result['lift_vs_flat_watched'], pct=False)}",
        f"- P(candidate state | later repriced): {fmt(result['p_candidate_given_repriced'])}",
        f"- recurring-series candidates: {fmt(result['recurring_candidate_rate'])}"
        f" | unseen-event candidates: {fmt(result['unseen_candidate_rate'])}",
        "",
        "## Sensitivity (candidate reprice rate; report-only, never primary)",
        "",
        "| variant | decided | reprice rate |",
        "|---|---:|---:|",
    ]
    for name, s in result["sensitivity"].items():
        variant_rate = (s["candidate_repriced"] / s["candidate_decided"]) if s["candidate_decided"] else None
        lines.append(f"| {name} | {s['candidate_decided']} | {fmt(variant_rate)} |")
    lines += [
        "",
        "Kill criterion (spec §4): candidate lift < 2× vs matched-random AND no",
        "better vs flat-but-watched at the 2026-09-15 checkpoint → redesign Stage 1.",
        "",
    ]
    return "\n".join(lines)
