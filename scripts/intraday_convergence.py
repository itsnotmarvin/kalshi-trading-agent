#!/usr/bin/env python3
"""Study hourly KXHIGH price convergence after a physical YES lock.

The live command fetches settled Kalshi markets/candles and IEM ASOS
observations, then overwrites ``docs/intraday-convergence.md`` with an honest
coverage and results report.  Network access is confined to ``run_study``;
the event-study functions are intentionally pure and tested offline.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.market_pricing import kalshi_trading_fee  # noqa: E402
from core.settlement_stations import SettlementStation  # noqa: E402
from scripts.backtest_weather import (  # noqa: E402
    BacktestDataError,
    RealDataClient,
    RequestRecorder,
    _candle_close,
    _optional_float,
    fetch_market_candles,
    fetch_settled_markets,
    iso_utc,
    parse_timestamp,
    selected_stations,
    ticker_target_date,
    utc_now,
)

SCHEMA = "intraday-convergence/v1"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "kalshi-trading-agent intraday-convergence/1"
LOCK_MARGIN_F = 1.0
CONVERGENCE_MIDPOINT = 0.95
SNAPSHOT_OFFSETS = (0, 1, 2)


@dataclass(frozen=True)
class Observation:
    """One station observation, normalized to aware UTC."""

    valid: datetime
    temp_f: float


@dataclass(frozen=True)
class Quote:
    """Usable two-sided close from one hourly Kalshi candle."""

    end: datetime
    yes_bid: float
    yes_ask: float

    @property
    def midpoint(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0


# IEM station identifiers are the settlement ICAO identifiers without the
# leading K.  In particular, KNYC maps to IEM station NYC in NY_ASOS.  KNYC is
# Central Park and is not an airport ASOS; this mapping should be revalidated
# if IEM changes its network inventory or Kalshi changes its settlement rules.
IEM_STATIONS: dict[str, dict[str, str]] = {
    "KXHIGHNY": {"iem_id": "NYC", "network": "NY_ASOS", "note": "Central Park (KNYC caveat)"},
    "KXHIGHCHI": {"iem_id": "MDW", "network": "IL_ASOS", "note": "Chicago Midway"},
    "KXHIGHMIA": {"iem_id": "MIA", "network": "FL_ASOS", "note": "Miami International"},
    "KXHIGHAUS": {"iem_id": "AUS", "network": "TX_ASOS", "note": "Austin Bergstrom"},
    "KXHIGHDEN": {"iem_id": "DEN", "network": "CO_ASOS", "note": "Denver International"},
    "KXHIGHPHIL": {"iem_id": "PHL", "network": "PA_ASOS", "note": "Philadelphia International"},
    "KXHIGHLAX": {"iem_id": "LAX", "network": "CA_ASOS", "note": "Los Angeles International"},
}


def ceil_hour(value: datetime) -> datetime:
    """Return the UTC candle-end hour at or immediately after ``value``."""
    value = parse_timestamp(value)
    floor = value.replace(minute=0, second=0, microsecond=0)
    return floor if value == floor else floor + timedelta(hours=1)


def compute_lock_time(
    observations: Sequence[Observation],
    target_day: date,
    station_timezone: str,
    strike: float,
    *,
    margin_f: float = LOCK_MARGIN_F,
) -> datetime | None:
    """First observation whose same-climate-day running max locks YES.

    A greater-than-X market is conservatively locked only at an observed
    temperature of at least X + 1.0 F.  Observations outside the station-local
    calendar day never leak into its running maximum.
    """
    if margin_f < 0:
        raise ValueError("lock margin must be non-negative")
    local_zone = ZoneInfo(station_timezone)
    running_max = -math.inf
    for observation in sorted(observations, key=lambda item: item.valid):
        valid = parse_timestamp(observation.valid)
        if valid.astimezone(local_zone).date() != target_day:
            continue
        temperature = float(observation.temp_f)
        if not math.isfinite(temperature):
            continue
        running_max = max(running_max, temperature)
        if running_max >= float(strike) + margin_f:
            return valid
    return None


def has_climate_day_observation(
    observations: Sequence[Observation], target_day: date, station_timezone: str
) -> bool:
    """Whether at least one finite observation exists in the local climate day."""
    local_zone = ZoneInfo(station_timezone)
    return any(
        parse_timestamp(observation.valid).astimezone(local_zone).date() == target_day
        and math.isfinite(float(observation.temp_f))
        for observation in observations
    )


def parse_iem_csv(text: str) -> list[Observation]:
    """Parse IEM ``format=onlycomma`` output without third-party packages."""
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    observations: list[Observation] = []
    for row in reader:
        raw_valid = row.get("valid")
        raw_temp = row.get("tmpf")
        if not raw_valid or raw_temp in (None, "", "M", "null"):
            continue
        try:
            valid = datetime.fromisoformat(raw_valid.strip().replace("Z", "+00:00"))
            if valid.tzinfo is None:
                valid = valid.replace(tzinfo=timezone.utc)
            temperature = float(raw_temp)
        except (TypeError, ValueError):
            continue
        if math.isfinite(temperature):
            observations.append(Observation(valid.astimezone(timezone.utc), temperature))
    return sorted(observations, key=lambda item: item.valid)


def candle_quote(candle: dict[str, Any]) -> tuple[Quote | None, str | None]:
    """Decode a two-sided candle close using the backtest's price conventions."""
    try:
        end = datetime.fromtimestamp(int(candle["end_period_ts"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None, "candle_timestamp_invalid"
    bid = _candle_close(candle.get("yes_bid"))
    ask = _candle_close(candle.get("yes_ask"))
    if bid is None or ask is None:
        return None, "candle_one_sided"
    if ask < bid:
        return None, "candle_crossed"
    return Quote(end, bid, ask), None


def _candles_by_end(candles: Sequence[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
    indexed: dict[datetime, dict[str, Any]] = {}
    for candle in candles:
        try:
            end = datetime.fromtimestamp(int(candle["end_period_ts"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        indexed[end] = candle
    return indexed


def measure_convergence(
    candles: Sequence[dict[str, Any]],
    lock_time: datetime,
    *,
    threshold: float = CONVERGENCE_MIDPOINT,
) -> tuple[int | None, str | None]:
    """Return aligned whole hours until midpoint first reaches ``threshold``.

    The lock is aligned to the end of its containing hourly candle.  A hit in
    that candle is hour zero; later hits are the integer candle-end offset.
    Missing and one-sided candles are skipped rather than imputed.
    """
    anchor = ceil_hour(lock_time)
    usable: list[Quote] = []
    for candle in candles:
        quote, _ = candle_quote(candle)
        if quote is not None and quote.end >= anchor:
            usable.append(quote)
    if not usable:
        return None, "no_usable_post_lock_candles"
    for quote in sorted(usable, key=lambda item: item.end):
        if quote.midpoint >= threshold:
            hours = int((quote.end - anchor).total_seconds() // 3600)
            return hours, None
    return None, "convergence_not_observed"


def analyze_market_event(
    market: dict[str, Any],
    station: SettlementStation,
    observations: Sequence[Observation],
    candles: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Analyze one settled greater-strike market and return one auditable row."""
    ticker = str(market.get("ticker") or "")
    target_day = ticker_target_date(ticker)
    strike = _optional_float(market.get("floor_strike"))
    row: dict[str, Any] = {
        "series_ticker": station.series_ticker,
        "station_id": station.station_id,
        "market_ticker": ticker,
        "target_date": target_day.isoformat() if target_day else None,
        "strike_type": market.get("strike_type"),
        "floor_strike": strike,
        "result": str(market.get("result") or "").lower(),
        "lock_time": None,
        "aligned_lock_candle_end": None,
        "hours_to_convergence": None,
        "convergence_reason": None,
        "snapshots": {},
        "drop_reason": None,
    }
    if market.get("strike_type") != "greater":
        row["drop_reason"] = "not_greater_strike"
        return row
    if target_day is None:
        row["drop_reason"] = "invalid_target_date"
        return row
    if strike is None:
        row["drop_reason"] = "floor_strike_missing"
        return row
    if row["result"] not in {"yes", "no"}:
        row["drop_reason"] = "invalid_settlement_result"
        return row

    if not has_climate_day_observation(observations, target_day, station.timezone):
        row["drop_reason"] = "observations_missing_climate_day"
        return row
    lock_time = compute_lock_time(observations, target_day, station.timezone, strike)
    if lock_time is None:
        row["drop_reason"] = "no_physical_yes_lock"
        return row
    row["lock_time"] = iso_utc(lock_time)
    anchor = ceil_hour(lock_time)
    row["aligned_lock_candle_end"] = iso_utc(anchor)
    if row["result"] != "yes":
        row["drop_reason"] = "physical_lock_conflicts_with_settlement"
        return row

    by_end = _candles_by_end(candles)
    if not by_end:
        row["drop_reason"] = "candles_missing"
        return row
    if min(by_end) > anchor:
        row["drop_reason"] = "lock_before_market_had_quotes"
        return row
    lock_candle = by_end.get(anchor)
    if lock_candle is None:
        row["drop_reason"] = "lock_hour_candle_missing"
        return row
    lock_quote, lock_reason = candle_quote(lock_candle)
    if lock_quote is None:
        row["drop_reason"] = f"lock_hour_{lock_reason}"
        return row

    hours, convergence_reason = measure_convergence(candles, lock_time)
    row["hours_to_convergence"] = hours
    row["convergence_reason"] = convergence_reason
    for offset in SNAPSHOT_OFFSETS:
        end = anchor + timedelta(hours=offset)
        candle = by_end.get(end)
        snapshot: dict[str, Any] = {
            "candle_end": iso_utc(end),
            "yes_bid": None,
            "yes_ask": None,
            "midpoint": None,
            "gap_below_0_95": None,
            "taker_fee": None,
            "net_pnl": None,
            "reason": None,
        }
        if candle is None:
            snapshot["reason"] = "candle_missing"
        else:
            quote, reason = candle_quote(candle)
            if quote is None:
                snapshot["reason"] = reason
            else:
                fee = kalshi_trading_fee(1, quote.yes_ask, maker=False)
                snapshot.update(
                    {
                        "yes_bid": quote.yes_bid,
                        "yes_ask": quote.yes_ask,
                        "midpoint": quote.midpoint,
                        "gap_below_0_95": max(0.0, CONVERGENCE_MIDPOINT - quote.yes_ask),
                        "taker_fee": fee,
                        "net_pnl": 1.0 - quote.yes_ask - fee,
                    }
                )
        row["snapshots"][str(offset)] = snapshot
    return row


def count_drop_reasons(rows: Sequence[dict[str, Any]]) -> Counter[str]:
    """Count every candidate exactly once as analyzed or by primary drop reason."""
    counts: Counter[str] = Counter()
    for row in rows:
        counts[str(row.get("drop_reason") or "analyzed")] += 1
    if sum(counts.values()) != len(rows):
        raise AssertionError("drop accounting does not reconcile to candidate count")
    return counts


def summarize_events(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate convergence, executable gaps, P&L, and secondary missingness."""
    analyzed = [row for row in rows if row.get("drop_reason") is None]
    reached = [int(row["hours_to_convergence"]) for row in analyzed if row["hours_to_convergence"] is not None]
    distribution = Counter()
    for value in reached:
        if value == 0:
            distribution["0h"] += 1
        elif value == 1:
            distribution["1h"] += 1
        elif value == 2:
            distribution["2h"] += 1
        elif value <= 5:
            distribution["3-5h"] += 1
        else:
            distribution["6h+"] += 1
    distribution["not_observed"] = len(analyzed) - len(reached)

    snapshots: dict[str, Any] = {}
    snapshot_missing = Counter()
    for offset in SNAPSHOT_OFFSETS:
        items = [row["snapshots"].get(str(offset), {}) for row in analyzed]
        usable = [item for item in items if item.get("yes_ask") is not None]
        tradeable = [item for item in usable if float(item["yes_ask"]) < CONVERGENCE_MIDPOINT]
        for item in items:
            if item.get("reason"):
                snapshot_missing[f"lock+{offset}h:{item['reason']}"] += 1
        snapshots[str(offset)] = {
            "events": len(items),
            "usable_quotes": len(usable),
            "ask_below_0_95": len(tradeable),
            "average_ask": mean(float(item["yes_ask"]) for item in usable) if usable else None,
            "average_tradeable_gap": (
                mean(float(item["gap_below_0_95"]) for item in tradeable) if tradeable else None
            ),
            "total_taker_fees": sum(float(item["taker_fee"]) for item in usable),
            "total_net_pnl": sum(float(item["net_pnl"]) for item in usable),
            "average_net_pnl": mean(float(item["net_pnl"]) for item in usable) if usable else None,
        }
    ordered = sorted(reached)
    by_station: dict[str, Any] = {}
    for station_id in sorted({str(row["station_id"]) for row in rows}):
        station_rows = [row for row in rows if row["station_id"] == station_id]
        station_analyzed = [row for row in station_rows if row.get("drop_reason") is None]
        by_station[station_id] = {
            "candidates": len(station_rows),
            "analyzed_events": len(station_analyzed),
            "converged_events": sum(
                row.get("hours_to_convergence") is not None for row in station_analyzed
            ),
            "no_lock": sum(
                row.get("drop_reason") == "no_physical_yes_lock" for row in station_rows
            ),
        }
    return {
        "candidate_markets": len(rows),
        "drop_reasons": dict(sorted(count_drop_reasons(rows).items())),
        "analyzed_events": len(analyzed),
        "converged_events": len(reached),
        "convergence_distribution": dict(distribution),
        "median_hours_to_convergence": median(reached) if reached else None,
        "p75_hours_to_convergence": _nearest_rank(ordered, 0.75),
        "p90_hours_to_convergence": _nearest_rank(ordered, 0.90),
        "convergence_null_reasons": dict(
            sorted(
                Counter(
                    str(row["convergence_reason"])
                    for row in analyzed
                    if row.get("convergence_reason")
                ).items()
            )
        ),
        "by_station": by_station,
        "snapshots": snapshots,
        "snapshot_missing_reasons": dict(sorted(snapshot_missing.items())),
    }


def _nearest_rank(values: Sequence[int], probability: float) -> int | None:
    if not values:
        return None
    index = max(0, math.ceil(probability * len(values)) - 1)
    return values[index]


def iem_request_params(iem_id: str, start: date, end: date) -> dict[str, Any]:
    """Return the documented IEM ASOS CGI query parameters.

    IEM's form uses separate ``year1/month1/day1`` and
    ``year2/month2/day2`` fields (not ISO ``start``/``end`` fields), repeated
    ``data=tmpf`` in its HTML form, ``format=onlycomma``, and ``tz=Etc/UTC``.
    httpx serializes this single requested data field equivalently.  The
    station is the ICAO identifier without its leading K.
    """
    return {
        "station": iem_id,
        "data": "tmpf",
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "year2": end.year,
        "month2": end.month,
        "day2": end.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        # 3 = routine METAR reports. report_type=1 (5-minute MADIS feed)
        # returns all-missing tmpf for these stations — verified live.
        "report_type": "3",
    }


def fetch_iem_observations(
    client: RealDataClient,
    station: SettlementStation,
    start: date,
    end: date,
) -> list[Observation]:
    """Fetch IEM observations, including UTC dates bracketing local days."""
    mapping = IEM_STATIONS[station.series_ticker]
    zone = ZoneInfo(station.timezone)
    first_local = datetime.combine(start, datetime.min.time(), tzinfo=zone)
    last_local = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    query_start = first_local.astimezone(timezone.utc).date()
    query_end = last_local.astimezone(timezone.utc).date()
    params = iem_request_params(mapping["iem_id"], query_start, query_end)
    attempts = 3
    for attempt in range(1, attempts + 1):
        started = utc_now()
        try:
            response = client.client.get(IEM_ASOS_URL, params=params)
        except httpx.HTTPError as exc:
            client.recorder.add(
                {
                    "source": f"iem_asos:{station.station_id}",
                    "url": IEM_ASOS_URL,
                    "params": params,
                    "started_at": iso_utc(started),
                    "retrieved_at": iso_utc(utc_now()),
                    "attempt": attempt,
                    "error": str(exc),
                }
            )
            if attempt == attempts:
                raise BacktestDataError(f"IEM request failed for {station.station_id}: {exc}") from exc
            time.sleep(0.5 * attempt)
            continue
        entry = {
            "source": f"iem_asos:{station.station_id}",
            "url": IEM_ASOS_URL,
            "params": params,
            "started_at": iso_utc(started),
            "retrieved_at": iso_utc(utc_now()),
            "attempt": attempt,
            "status_code": response.status_code,
        }
        if response.status_code == 429 and attempt < attempts:
            entry["error"] = "rate_limited"
            client.recorder.add(entry)
            time.sleep(min(5.0, float(response.headers.get("Retry-After", "1"))))
            continue
        if response.status_code >= 400:
            entry["error"] = response.text[:500]
            client.recorder.add(entry)
            raise BacktestDataError(
                f"IEM returned HTTP {response.status_code} for {station.station_id}: {response.text[:300]}"
            )
        observations = parse_iem_csv(response.text)
        entry["observation_rows"] = len(observations)
        client.recorder.add(entry)
        if not observations:
            raise BacktestDataError(f"IEM returned no usable observations for {station.station_id}")
        return observations
    raise AssertionError("IEM retry loop exhausted")


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def render_report(manifest: dict[str, Any], summary: dict[str, Any] | None, error: str | None = None) -> str:
    """Render the checked-in study report or an honest failed-run report."""
    lines = [
        "# Intraday KXHIGH convergence after a physical temperature lock",
        "",
        f"Generated: `{manifest.get('completed_at') or manifest.get('started_at')}`",
        "",
        "## Methodology",
        "",
        "For each settled Kalshi KXHIGH market in the selected lookback window, this study "
        "keeps only `strike_type=greater`. The market ticker supplies the station-local climate "
        "date. IEM's ASOS archive supplies hourly/METAR `tmpf` observations for the exact "
        "settlement station. A physical YES lock occurs at the first observation where that "
        "climate day's running observed maximum is at least the floor strike plus 1.0°F.",
        "",
        "The lock timestamp is aligned to the end of its containing 60-minute Kalshi candle. "
        "Hour zero is that candle; midpoint convergence is the first usable two-sided candle "
        "with `(YES bid + YES ask) / 2 >= 0.95`. Missing quotes are never forward-filled. "
        "Snapshot P&L buys one YES contract at the candle's executable ask and subtracts the "
        "one-contract taker fee from `core.market_pricing.kalshi_trading_fee`; settlement pays $1.",
        "",
    ]
    if error:
        lines.extend(
            [
                "## Run status",
                "",
                "The live study did not complete. No synthetic observations, prices, counts, or results were substituted.",
                "",
                f"Error: `{error}`",
                "",
            ]
        )
    elif summary is None:
        lines.extend(
            [
                "## Run status",
                "",
                "Not run in this checkout. Run the command at the end of this document to populate real event counts and results.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Coverage and drop accounting",
                "",
                f"Study window: **{manifest.get('window_start')} through {manifest.get('window_end')}**, "
                f"using the latest settled target date returned by Kalshi as the endpoint. "
                f"Excluded non-greater markets by strike type: `{manifest.get('excluded_non_greater_by_type', {})}`.",
                "",
                f"Candidate greater-strike markets in window: **{summary['candidate_markets']}**. "
                f"Fully analyzed physical-lock events: **{summary['analyzed_events']}**.",
                "",
                "| Outcome / primary drop reason | Markets |",
                "|---|---:|",
            ]
        )
        for reason, count in summary["drop_reasons"].items():
            lines.append(f"| `{reason}` | {count} |")
        lines.extend(
            [
                "",
                "### Events by settlement station",
                "",
                "| Station | Greater-strike candidates | Analyzed locks | Converged | No physical lock |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for station_id, item in summary["by_station"].items():
            lines.append(
                f"| `{station_id}` | {item['candidates']} | {item['analyzed_events']} | "
                f"{item['converged_events']} | {item['no_lock']} |"
            )
        lines.extend(
            [
                "",
                "## Convergence time",
                "",
                f"Convergence was observed in **{summary['converged_events']} / {summary['analyzed_events']}** analyzed events. "
                f"Median: **{_fmt(summary['median_hours_to_convergence'], 1)}h**; "
                f"p75: **{_fmt(summary['p75_hours_to_convergence'], 1)}h**; "
                f"p90: **{_fmt(summary['p90_hours_to_convergence'], 1)}h**.",
                "",
                "| Aligned time to first midpoint >= 0.95 | Events |",
                "|---|---:|",
            ]
        )
        for bucket in ("0h", "1h", "2h", "3-5h", "6h+", "not_observed"):
            lines.append(f"| {bucket} | {summary['convergence_distribution'].get(bucket, 0)} |")
        if summary["convergence_null_reasons"]:
            lines.extend(["", "Convergence null reasons:", ""])
            for reason, count in summary["convergence_null_reasons"].items():
                lines.append(f"- `{reason}`: {count}")
        lines.extend(
            [
                "",
                "## Executable gaps and hypothetical after-fee P&L",
                "",
                "P&L is one YES contract per usable snapshot, including snapshots whose ask was already at or above 0.95. "
                "The average tradeable gap is conditional on `YES ask < 0.95` and equals `0.95 - ask`.",
                "",
                "| Snapshot | Events | Usable asks | Ask < 0.95 | Avg ask | Avg tradeable gap | Fees | Net P&L | Avg P&L |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for offset in SNAPSHOT_OFFSETS:
            item = summary["snapshots"][str(offset)]
            lines.append(
                f"| lock+{offset}h | {item['events']} | {item['usable_quotes']} | {item['ask_below_0_95']} | "
                f"{_fmt(item['average_ask'], 3)} | {_fmt(item['average_tradeable_gap'], 3)} | "
                f"${_fmt(item['total_taker_fees'])} | ${_fmt(item['total_net_pnl'])} | "
                f"${_fmt(item['average_net_pnl'], 3)} |"
            )
        lines.extend(["", "### Secondary snapshot missingness", ""])
        if summary["snapshot_missing_reasons"]:
            for reason, count in summary["snapshot_missing_reasons"].items():
                lines.append(f"- `{reason}`: {count}")
        else:
            lines.append("No missing snapshot quotes.")
        lines.append("")
    lines.extend(
        [
            "## Interpretation and caveats",
            "",
            "- **Hourly resolution makes every measured duration an upper bound on capturable edge duration.** A candle first showing convergence at hour N only establishes that convergence happened by that candle end; it can happen much earlier inside the interval.",
            "- If most events converge in the aligned lock candle, the defensible verdict is **not measurable at this granularity, likely arbitraged**. This study cannot establish a minutes-scale trading window.",
            "- Observation timestamps and candle end timestamps are different objects. Aligning a METAR timestamp to its containing candle end can include up to nearly one hour of market reaction before the recorded hour-zero close.",
            "- IEM observations are hourly METAR reports. The settlement climatological maximum comes from a continuous sensor, so the observed lock is conservative and may occur later than the true physical lock. The 1.0°F clearance margin also protects against METAR conversion/rounding differences.",
            "- Central Park is IEM `NYC` in `NY_ASOS` (Kalshi station `KNYC`), an important non-airport identifier caveat. Other mappings are MDW, MIA, AUS, DEN, PHL, and LAX.",
            "- Hourly candlesticks expose candle-close bid/ask summaries, not guaranteed fills, queue depth, latency, or the intrahour quote path. The P&L is hypothetical and ignores slippage beyond the displayed ask.",
            "- A missing, one-sided, crossed, or not-yet-open lock-hour candle is counted explicitly and never imputed. A physical lock that conflicts with settlement is dropped and surfaced as a data-integrity warning.",
            "",
            "## Reproduce",
            "",
            "```bash",
            "python3 scripts/intraday_convergence.py",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _market_window(
    markets_by_series: dict[str, list[dict[str, Any]]], lookback_days: int
) -> tuple[date, date]:
    dates = [
        parsed
        for markets in markets_by_series.values()
        for market in markets
        if market.get("strike_type") == "greater"
        if (parsed := ticker_target_date(str(market.get("ticker") or ""))) is not None
    ]
    if not dates:
        raise BacktestDataError("no dated settled greater-strike KXHIGH markets were returned")
    end = max(dates)
    return end - timedelta(days=lookback_days - 1), end


def run_study(args: argparse.Namespace) -> dict[str, Any]:
    stations = selected_stations(args.cities)
    output_dir = Path(args.output_dir)
    report_path = Path(args.results_doc)
    recorder = RequestRecorder()
    started = utc_now()
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": iso_utc(started),
        "completed_at": None,
        "lookback_days": args.lookback_days,
        "stations": [asdict(station) | IEM_STATIONS[station.series_ticker] for station in stations.values()],
        "request_parameters": {
            "kalshi_markets": {"status": "settled", "limit": 100},
            "kalshi_candlesticks": {"period_interval": 60},
            "iem_asos": {"data": "tmpf", "format": "onlycomma", "tz": "Etc/UTC"},
        },
        "requests": recorder.entries,
    }
    client = RealDataClient(recorder, args.timeout)
    client.client.headers["User-Agent"] = USER_AGENT
    try:
        markets_by_series: dict[str, list[dict[str, Any]]] = {}
        for series, station in stations.items():
            markets_by_series[series] = fetch_settled_markets(client, station)
            print(f"markets {series}: {len(markets_by_series[series])}", flush=True)
        window_start, window_end = _market_window(markets_by_series, args.lookback_days)
        manifest["window_start"] = window_start.isoformat()
        manifest["window_end"] = window_end.isoformat()

        observations: dict[str, list[Observation]] = {}
        for series, station in stations.items():
            observations[series] = fetch_iem_observations(client, station, window_start, window_end)
            print(f"observations {station.station_id}: {len(observations[series])}", flush=True)

        candidates: list[tuple[SettlementStation, dict[str, Any]]] = []
        excluded_strike_types = Counter()
        invalid_target_dates = 0
        for series, station in stations.items():
            for market in markets_by_series[series]:
                target_day = ticker_target_date(str(market.get("ticker") or ""))
                if target_day is None:
                    invalid_target_dates += 1
                    continue
                if not (window_start <= target_day <= window_end):
                    continue
                if market.get("strike_type") != "greater":
                    excluded_strike_types[str(market.get("strike_type") or "missing")] += 1
                    continue
                candidates.append((station, market))
        manifest["excluded_non_greater_by_type"] = dict(sorted(excluded_strike_types.items()))
        manifest["settled_markets_with_invalid_target_date"] = invalid_target_dates

        candles_by_ticker: dict[str, list[dict[str, Any]]] = {}
        candle_errors: dict[str, str] = {}
        jobs: dict[str, tuple[SettlementStation, datetime, datetime]] = {}
        for station, market in candidates:
            target_day = ticker_target_date(str(market["ticker"]))
            assert target_day is not None
            zone = ZoneInfo(station.timezone)
            local_start = datetime.combine(target_day, datetime.min.time(), tzinfo=zone)
            local_end = local_start + timedelta(days=1)
            request_end = local_end.astimezone(timezone.utc) + timedelta(hours=1)
            try:
                close_time = parse_timestamp(market.get("close_time"))
            except (TypeError, ValueError):
                close_time = request_end
            jobs[str(market["ticker"])] = (
                station,
                local_start.astimezone(timezone.utc) - timedelta(hours=1),
                max(request_end, close_time),
            )
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_market_candles, client, station, ticker, start, end): ticker
                for ticker, (station, start, end) in jobs.items()
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                ticker = futures[future]
                try:
                    candles_by_ticker[ticker] = future.result()
                except Exception as exc:
                    candle_errors[ticker] = str(exc)
                    candles_by_ticker[ticker] = []
                if completed % 100 == 0 or completed == len(futures):
                    print(f"candles: {completed}/{len(futures)}", flush=True)
        if jobs and len(candle_errors) == len(jobs):
            raise BacktestDataError(
                "every Kalshi candlestick request failed; refusing to report an all-missing study"
            )

        rows = [
            analyze_market_event(
                market,
                station,
                observations[station.series_ticker],
                candles_by_ticker.get(str(market.get("ticker") or ""), []),
            )
            for station, market in candidates
        ]
        for row in rows:
            ticker = str(row["market_ticker"])
            if ticker in candle_errors and row.get("drop_reason") in {"candles_missing", "lock_before_market_had_quotes"}:
                row["drop_reason"] = "candle_request_error"
                row["candle_request_error"] = candle_errors[ticker]
        summary = summarize_events(rows)
        manifest["completed_at"] = iso_utc(utc_now())
        manifest["candidate_markets"] = len(rows)
        manifest["summary"] = summary
        manifest["candle_request_errors"] = candle_errors
        _write_jsonl(output_dir / "events.jsonl", rows)
        _write_json(output_dir / "summary.json", summary)
        _write_json(output_dir / "manifest.json", manifest)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(manifest, summary))
        return {"manifest": manifest, "summary": summary}
    except Exception as exc:
        manifest["completed_at"] = iso_utc(utc_now())
        manifest["fatal_error"] = str(exc)
        _write_json(output_dir / "manifest.json", manifest)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(manifest, None, error=str(exc)))
        raise
    finally:
        client.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", help="Optional city subset; default is all seven stations")
    parser.add_argument("--lookback-days", type=int, default=62)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", default="data/runtime/intraday_convergence")
    parser.add_argument("--results-doc", default="docs/intraday-convergence.md")
    args = parser.parse_args(argv)
    if args.lookback_days < 1:
        parser.error("--lookback-days must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_study(args)
    except (BacktestDataError, httpx.HTTPError, ValueError) as exc:
        print(f"INTRADAY STUDY FAILED: {exc}", file=sys.stderr)
        return 2
    summary = result["summary"]
    print("\nIntraday convergence summary")
    print(f"candidates: {summary['candidate_markets']}")
    print(f"analyzed physical-lock events: {summary['analyzed_events']}")
    print(f"convergence observed: {summary['converged_events']}")
    print(f"median aligned hours: {_fmt(summary['median_hours_to_convergence'], 1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
