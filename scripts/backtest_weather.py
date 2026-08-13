#!/usr/bin/env python3
"""Leakage-safe historical backtest for settled Kalshi KXHIGH markets.

The dataset preserves the ``market_ticker x horizon_days`` candidate grain.
Unavailable forecasts and quotes remain null; they are never filled from a
later model component or candle. Kalshi payloads are decoded with
``JSONDecoder(strict=False)`` because rules text can contain raw controls.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings  # noqa: E402
from core.forecast_scoring import (  # noqa: E402
    brier_score,
    calibration_buckets,
    log_loss,
    make_walk_forward_splits,
)
from core.market_pricing import kalshi_trading_fee  # noqa: E402
from core.settlement_stations import SETTLEMENT_STATIONS, SettlementStation  # noqa: E402

SCHEMA = "weather-backtest/v1"
HORIZONS = (1, 2, 3, 5, 7)
MAX_PRICE_AGE_MINUTES = 120.0
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
USER_AGENT = "kalshi-trading-agent weather-backtest/1"
DATASET_FIELDS = (
    "series_ticker",
    "station_id",
    "target_date",
    "market_ticker",
    "horizon_days",
    "forecast_cutoff_ts",
    "forecast_daily_high_f",
    "candle_end_ts",
    "price_age_minutes",
    "yes_bid_close",
    "yes_ask_close",
    "market_mid",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "forecast_implied_yes",
    "result_yes",
)
MARKET_FIELDS = (
    "event_ticker",
    "ticker",
    "result",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "open_time",
    "close_time",
    "expected_expiration_time",
)
MONTH_NUMBERS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
JSON_DECODER = json.JSONDecoder(strict=False)
EPSILON = 1e-12


class BacktestDataError(RuntimeError):
    """Raised when a real upstream response cannot support the backtest."""


class CandlestickAuthenticationRequired(BacktestDataError):
    """Raised when Kalshi rejects candle access and no usable key is present."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime:
    """Parse an API timestamp and normalize it to aware UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def decode_kalshi_json(text: str) -> Any:
    """Decode Kalshi JSON while accepting raw control characters in strings."""
    return JSON_DECODER.decode(text)


def unique_stations() -> dict[str, SettlementStation]:
    """Return the seven non-alias settlement stations keyed by series."""
    return {station.series_ticker: station for station in SETTLEMENT_STATIONS.values()}


def ticker_target_date(ticker: str) -> date | None:
    """Parse the ticker-local climate date, never a UTC close date."""
    import re

    match = re.search(
        r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?:-|$)",
        ticker.upper(),
    )
    if not match:
        return None
    try:
        return date(
            2000 + int(match.group(1)),
            MONTH_NUMBERS[match.group(2)],
            int(match.group(3)),
        )
    except ValueError:
        return None


def forecast_implied_yes(
    forecast_high_f: float,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
) -> bool:
    """Apply the repository's exact KXHIGH settlement boundaries."""
    if strike_type == "greater" and floor_strike is not None:
        return forecast_high_f > floor_strike
    if strike_type == "less" and cap_strike is not None:
        return forecast_high_f < cap_strike
    if strike_type == "between" and floor_strike is not None and cap_strike is not None:
        return floor_strike <= forecast_high_f <= cap_strike
    raise ValueError("unsupported or incomplete strike")


def compute_forecast_bundle(
    hourly: dict[str, Any],
    target_day: date,
    horizon_days: int,
    station_timezone: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build one daily forecast using only its 24 fixed-lead components.

    Each component's implied issue time is its valid UTC time minus H*24h.
    The bundle is unavailable unless the local climate day has exactly 24
    non-null hourly values.
    """
    times = hourly.get("time")
    values = hourly.get(f"temperature_2m_previous_day{horizon_days}")
    if not isinstance(times, list) or not isinstance(values, list):
        return None, "forecast_variable_missing"

    local_zone = ZoneInfo(station_timezone)
    components: list[tuple[datetime, float]] = []
    for index, raw_time in enumerate(times):
        if index >= len(values) or values[index] is None:
            continue
        valid_local = datetime.fromisoformat(str(raw_time))
        if valid_local.tzinfo is None:
            valid_local = valid_local.replace(tzinfo=local_zone)
        else:
            valid_local = valid_local.astimezone(local_zone)
        if valid_local.date() != target_day:
            continue
        valid_utc = valid_local.astimezone(timezone.utc)
        issue_utc = valid_utc - timedelta(days=horizon_days)
        components.append((issue_utc, float(values[index])))

    if len(components) != 24:
        return None, "forecast_incomplete_climate_day"
    cutoff = max(issue for issue, _ in components)
    if any(issue > cutoff for issue, _ in components):
        raise AssertionError("forecast component was issued after bundle cutoff")
    return {
        "forecast_cutoff_ts": iso_utc(cutoff),
        "forecast_daily_high_f": max(value for _, value in components),
    }, None


def _candle_close(side: Any) -> float | None:
    if not isinstance(side, dict):
        return None
    if side.get("close_dollars") is not None:
        try:
            value = float(side["close_dollars"])
        except (TypeError, ValueError):
            return None
        return value if 0.0 <= value <= 1.0 else None
    for key in ("close_cents", "close"):
        if side.get(key) is None:
            continue
        try:
            value = float(side[key])
        except (TypeError, ValueError):
            return None
        # Legacy live candles used integer cents in ``close``; current
        # historical responses can use an unsuffixed fixed-point dollar value.
        if value > 1.0:
            value /= 100.0
        return value if 0.0 <= value <= 1.0 else None
    return None


def select_candle(
    candles: Sequence[dict[str, Any]],
    forecast_cutoff_ts: str | datetime,
    max_age_minutes: float = MAX_PRICE_AGE_MINUTES,
) -> tuple[dict[str, Any] | None, str | None]:
    """Select the latest two-sided close at or before the forecast cutoff."""
    cutoff = parse_timestamp(forecast_cutoff_ts)
    eligible: list[tuple[datetime, dict[str, Any]]] = []
    saw_after_cutoff = False
    for candle in candles:
        try:
            end = datetime.fromtimestamp(int(candle["end_period_ts"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if end > cutoff:
            saw_after_cutoff = True
            continue
        eligible.append((end, candle))
    if not eligible:
        return None, "candle_after_cutoff_only" if saw_after_cutoff else "candle_missing"

    end, candle = max(eligible, key=lambda pair: pair[0])
    age_minutes = (cutoff - end).total_seconds() / 60.0
    if age_minutes > max_age_minutes:
        return None, "candle_stale"
    bid = _candle_close(candle.get("yes_bid"))
    ask = _candle_close(candle.get("yes_ask"))
    if bid is None or ask is None:
        return None, "candle_one_sided"
    if ask < bid:
        return None, "candle_crossed"
    return {
        "candle_end_ts": iso_utc(end),
        "price_age_minutes": age_minutes,
        "yes_bid_close": bid,
        "yes_ask_close": ask,
        "market_mid": (bid + ask) / 2.0,
    }, None


def empty_dataset_row(
    market: dict[str, Any],
    station: SettlementStation,
    target_day: date | None,
    horizon_days: int,
) -> dict[str, Any]:
    """Create an exact Section 4 row; absent upstream data stays null."""
    return {
        "series_ticker": station.series_ticker,
        "station_id": station.station_id,
        "target_date": target_day.isoformat() if target_day else None,
        "market_ticker": market.get("ticker"),
        "horizon_days": horizon_days,
        "forecast_cutoff_ts": None,
        "forecast_daily_high_f": None,
        "candle_end_ts": None,
        "price_age_minutes": None,
        "yes_bid_close": None,
        "yes_ask_close": None,
        "market_mid": None,
        "strike_type": market.get("strike_type"),
        "floor_strike": _optional_float(market.get("floor_strike")),
        "cap_strike": _optional_float(market.get("cap_strike")),
        "forecast_implied_yes": None,
        "result_yes": None,
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def assert_unique_grain(rows: Sequence[dict[str, Any]]) -> None:
    """Reject duplicate market ticker × horizon rows before writing."""
    keys = [(row.get("market_ticker"), row.get("horizon_days")) for row in rows]
    if len(keys) != len(set(keys)):
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        raise ValueError(f"duplicate dataset grain: {duplicates[:5]}")


def gaussian_yes_probability(
    forecast_high_f: float,
    sigma: float,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
) -> float:
    """Map a deterministic high to YES probability under N(forecast, sigma)."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    distribution = NormalDist(mu=float(forecast_high_f), sigma=float(sigma))
    if strike_type == "greater" and floor_strike is not None:
        probability = 1.0 - distribution.cdf(float(floor_strike))
    elif strike_type == "less" and cap_strike is not None:
        probability = distribution.cdf(float(cap_strike))
    elif strike_type == "between" and floor_strike is not None and cap_strike is not None:
        probability = distribution.cdf(float(cap_strike)) - distribution.cdf(float(floor_strike))
    else:
        raise ValueError("unsupported or incomplete strike")
    return min(1.0 - EPSILON, max(EPSILON, probability))


def _training_log_loss(rows: Sequence[dict[str, Any]], sigma: float) -> float:
    total = 0.0
    for row in rows:
        probability = gaussian_yes_probability(
            row["forecast_daily_high_f"],
            sigma,
            row["strike_type"],
            row.get("floor_strike"),
            row.get("cap_strike"),
        )
        outcome = int(row["result_yes"])
        total -= outcome * math.log(probability) + (1 - outcome) * math.log(1.0 - probability)
    return total / len(rows)


def fit_gaussian_sigma(rows: Sequence[dict[str, Any]]) -> float:
    """Fit sigma by training-only binary likelihood using golden-section search."""
    if not rows:
        raise ValueError("at least one training row is required")
    low = math.log(0.20)
    high = math.log(30.0)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = high - ratio * (high - low)
    right = low + ratio * (high - low)
    for _ in range(60):
        left_loss = _training_log_loss(rows, math.exp(left))
        right_loss = _training_log_loss(rows, math.exp(right))
        if left_loss <= right_loss:
            high = right
            right = left
            left = high - ratio * (high - low)
        else:
            low = left
            left = right
            right = low + ratio * (high - low)
    return math.exp((low + high) / 2.0)


def build_walk_forward_folds(
    rows: Sequence[dict[str, Any]],
    *,
    min_train_days: int | None = None,
    max_folds: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return sorted rows and expanding folds backed by the core split helper."""
    ordered = sorted(rows, key=lambda row: (row["target_date"], row["market_ticker"]))
    unique_days = sorted({row["target_date"] for row in ordered})
    if len(unique_days) < 3:
        return ordered, []
    train_days = min_train_days if min_train_days is not None else max(2, len(unique_days) // 3)
    if train_days >= len(unique_days):
        return ordered, []
    remaining = len(unique_days) - train_days
    chunk_days = max(1, math.ceil(remaining / max_folds))
    test_start_day_indexes = list(range(train_days, len(unique_days), chunk_days))
    cutoffs = [f"{unique_days[index - 1]}T23:59:59+00:00" for index in test_start_day_indexes]
    timestamps = [f"{row['target_date']}T12:00:00+00:00" for row in ordered]
    core_splits = make_walk_forward_splits(timestamps, cutoffs)
    folds: list[dict[str, Any]] = []
    for index, split in enumerate(core_splits):
        test_start = split["hidden"]["start"]
        test_end = (
            core_splits[index + 1]["hidden"]["start"] - 1
            if index + 1 < len(core_splits)
            else len(ordered) - 1
        )
        folds.append(
            {
                "cutoff": split["cutoff"],
                "train_start": split["visible"]["start"],
                "train_end": split["visible"]["end"],
                "test_start": test_start,
                "test_end": test_end,
            }
        )
    return ordered, folds


def walk_forward_predictions(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fit per-fold sigma on prior days and predict each later row once."""
    ordered, folds = build_walk_forward_folds(rows)
    predictions: list[dict[str, Any]] = []
    for fold_number, fold in enumerate(folds, start=1):
        training = ordered[fold["train_start"] : fold["train_end"] + 1]
        sigma = fit_gaussian_sigma(training)
        for row in ordered[fold["test_start"] : fold["test_end"] + 1]:
            predicted = dict(row)
            predicted["model_p"] = gaussian_yes_probability(
                row["forecast_daily_high_f"],
                sigma,
                row["strike_type"],
                row.get("floor_strike"),
                row.get("cap_strike"),
            )
            predicted["sigma"] = sigma
            predicted["fold"] = fold_number
            predicted["training_cutoff"] = fold["cutoff"]
            predictions.append(predicted)
    return predictions


def cluster_bootstrap_skill_ci(
    rows: Sequence[dict[str, Any]],
    *,
    resamples: int = 1000,
    seed: int = 20260813,
) -> tuple[float, float] | tuple[None, None]:
    """Percentile CI for midpoint Brier minus model Brier by station-day."""
    if not rows or resamples <= 0:
        return None, None
    clusters: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        clusters[(row["station_id"], row["target_date"])].append(row)
    keys = list(clusters)
    if not keys:
        return None, None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(resamples):
        sampled: list[dict[str, Any]] = []
        for key in rng.choices(keys, k=len(keys)):
            sampled.extend(clusters[key])
        outcomes = [int(row["result_yes"]) for row in sampled]
        market = [float(row["market_mid"]) for row in sampled]
        model = [float(row["model_p"]) for row in sampled]
        samples.append(brier_score(market, outcomes) - brier_score(model, outcomes))
    samples.sort()
    low_index = max(0, math.floor(0.025 * (len(samples) - 1)))
    high_index = min(len(samples) - 1, math.ceil(0.975 * (len(samples) - 1)))
    return samples[low_index], samples[high_index]


def paper_policy_pnl(rows: Sequence[dict[str, Any]], edge_threshold: float) -> dict[str, Any]:
    """One-contract taker P&L using the executable side and Kalshi fee math."""
    pnl = 0.0
    fees = 0.0
    trades = 0
    for row in rows:
        edge = float(row["model_p"]) - float(row["market_mid"])
        if abs(edge) <= edge_threshold:
            continue
        if edge > 0:
            cost = float(row["yes_ask_close"])
            payout = int(row["result_yes"])
        else:
            cost = 1.0 - float(row["yes_bid_close"])
            payout = 1 - int(row["result_yes"])
        fee = kalshi_trading_fee(1, cost, maker=False)
        pnl += payout - cost - fee
        fees += fee
        trades += 1
    return {"trades": trades, "fees": fees, "net_pnl": pnl}


def score_prediction_set(
    rows: Sequence[dict[str, Any]],
    *,
    bootstrap_resamples: int,
    edge_threshold: float,
    seed: int,
) -> dict[str, Any]:
    outcomes = [int(row["result_yes"]) for row in rows]
    model = [float(row["model_p"]) for row in rows]
    market = [float(row["market_mid"]) for row in rows]
    model_brier = brier_score(model, outcomes)
    market_brier = brier_score(market, outcomes)
    indicator_brier = brier_score(
        [float(bool(row["forecast_implied_yes"])) for row in rows], outcomes
    )
    ci_low, ci_high = cluster_bootstrap_skill_ci(rows, resamples=bootstrap_resamples, seed=seed)
    model_log_loss = log_loss(model, outcomes)
    market_log_loss = log_loss(market, outcomes)
    return {
        "n": len(rows),
        "clusters": len({(row["station_id"], row["target_date"]) for row in rows}),
        "model_brier": model_brier,
        "market_brier": market_brier,
        "indicator_brier": indicator_brier,
        "brier_skill": market_brier - model_brier,
        "brier_skill_ci_low": ci_low,
        "brier_skill_ci_high": ci_high,
        "model_log_loss": model_log_loss,
        "market_log_loss": market_log_loss,
        "calibration": calibration_buckets(model, outcomes),
        "paper": paper_policy_pnl(rows, edge_threshold),
    }


class RequestRecorder:
    """Thread-safe manifest request log without credentials or response bodies."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.entries.append(entry)


class RealDataClient:
    """HTTP client that tries public Kalshi access before optional signing."""

    def __init__(self, recorder: RequestRecorder, timeout_seconds: float = 30.0) -> None:
        self.recorder = recorder
        self.client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._signer: Any = None
        self._signer_checked = False
        self._signer_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def _auth_headers(self, method: str, url: str) -> dict[str, str] | None:
        with self._signer_lock:
            if not self._signer_checked:
                self._signer_checked = True
                key_path = (
                    Path(settings.kalshi_private_key_path).expanduser()
                    if settings.kalshi_private_key_path
                    else None
                )
                if settings.kalshi_api_key_id and key_path and key_path.is_file():
                    from adapters.kalshi_adapter import KalshiAdapter

                    signer = object.__new__(KalshiAdapter)
                    signer.api_key_id = settings.kalshi_api_key_id
                    signer.private_key = key_path.read_text()
                    self._signer = signer
        if self._signer is None:
            return None
        return self._signer._auth_headers(method, url)

    def get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        source: str,
        kalshi: bool = False,
        auth_fallback: bool = False,
    ) -> Any:
        attempts = 3
        for attempt in range(1, attempts + 1):
            started = utc_now()
            try:
                response = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                self.recorder.add(
                    {
                        "source": source,
                        "url": url,
                        "params": params,
                        "started_at": iso_utc(started),
                        "retrieved_at": iso_utc(utc_now()),
                        "attempt": attempt,
                        "authenticated": False,
                        "error": str(exc),
                    }
                )
                if attempt == attempts:
                    raise BacktestDataError(f"{source} request failed: {exc}") from exc
                time.sleep(0.5 * attempt)
                continue

            authenticated = False
            if response.status_code in (401, 403) and auth_fallback:
                headers = self._auth_headers("GET", str(response.request.url))
                if headers is None:
                    self.recorder.add(
                        {
                            "source": source,
                            "url": url,
                            "params": params,
                            "started_at": iso_utc(started),
                            "retrieved_at": iso_utc(utc_now()),
                            "status_code": response.status_code,
                            "authenticated": False,
                            "error": "authentication required and no usable Kalshi credentials configured",
                        }
                    )
                    raise CandlestickAuthenticationRequired(
                        "Kalshi candlestick endpoint returned "
                        f"{response.status_code}; configure KALSHI_API_KEY_ID and "
                        "KALSHI_PRIVATE_KEY_PATH. No candle data were fabricated."
                    )
                response = self.client.get(url, params=params, headers=headers)
                authenticated = True

            entry = {
                "source": source,
                "url": url,
                "params": params,
                "started_at": iso_utc(started),
                "retrieved_at": iso_utc(utc_now()),
                "attempt": attempt,
                "status_code": response.status_code,
                "authenticated": authenticated,
            }
            if response.status_code == 429 and attempt < attempts:
                entry["error"] = "rate_limited"
                self.recorder.add(entry)
                delay = min(5.0, float(response.headers.get("Retry-After", "1")))
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                entry["error"] = response.text[:500]
                self.recorder.add(entry)
                raise BacktestDataError(f"{source} returned HTTP {response.status_code}: {response.text[:300]}")
            try:
                payload = decode_kalshi_json(response.text) if kalshi else response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                entry["error"] = f"invalid JSON: {exc}"
                self.recorder.add(entry)
                raise BacktestDataError(f"{source} returned invalid JSON: {exc}") from exc
            self.recorder.add(entry)
            return payload
        raise AssertionError("request retry loop exhausted")


def fetch_settled_markets(
    client: RealDataClient,
    station: SettlementStation,
) -> list[dict[str, Any]]:
    """Paginate every settled market currently exposed for one KXHIGH series."""
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {
            "series_ticker": station.series_ticker,
            "status": "settled",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        payload = client.get_json(
            f"{KALSHI_BASE_URL}/markets",
            params,
            source=f"kalshi_markets:{station.series_ticker}",
            kalshi=True,
        )
        page = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise BacktestDataError(f"Kalshi markets response missing list for {station.series_ticker}")
        for raw in page:
            market = {field: raw.get(field) for field in MARKET_FIELDS}
            market["series_ticker"] = station.series_ticker
            markets.append(market)
        next_cursor = payload.get("cursor")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise BacktestDataError(f"Kalshi repeated pagination cursor for {station.series_ticker}")
        seen_cursors.add(next_cursor)
        cursor = str(next_cursor)
    deduplicated = {market["ticker"]: market for market in markets if market.get("ticker")}
    return list(deduplicated.values())


def fetch_station_forecast(
    client: RealDataClient,
    station: SettlementStation,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": ",".join(f"temperature_2m_previous_day{day}" for day in range(1, 8)),
        "models": "gfs_seamless",
        "temperature_unit": "fahrenheit",
        "timezone": station.timezone,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    payload = client.get_json(
        PREVIOUS_RUNS_URL,
        params,
        source=f"open_meteo_previous_runs:{station.station_id}",
    )
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        reason = payload.get("reason") if isinstance(payload, dict) else None
        raise BacktestDataError(f"Open-Meteo response missing hourly data for {station.station_id}: {reason}")
    return hourly


def fetch_market_candles(
    client: RealDataClient,
    station: SettlementStation,
    market_ticker: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    payload = client.get_json(
        (
            f"{KALSHI_BASE_URL}/series/{quote(station.series_ticker, safe='')}"
            f"/markets/{quote(market_ticker, safe='')}/candlesticks"
        ),
        {
            "period_interval": 60,
            "start_ts": int(start.timestamp()),
            "end_ts": int(end.timestamp()),
        },
        source=f"kalshi_candles:{market_ticker}",
        kalshi=True,
        auth_fallback=True,
    )
    candles = payload.get("candlesticks") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        raise BacktestDataError(f"Kalshi candlestick response missing list for {market_ticker}")
    return candles


def selected_stations(city_names: Sequence[str] | None) -> dict[str, SettlementStation]:
    stations = unique_stations()
    if not city_names:
        return dict(sorted(stations.items()))
    selected: dict[str, SettlementStation] = {}
    for city in city_names:
        station = SETTLEMENT_STATIONS.get(city.strip().lower())
        if station is None:
            valid = ", ".join(sorted({key for key in SETTLEMENT_STATIONS if key not in {"nyc", "la"}}))
            raise ValueError(f"unknown city {city!r}; choose from {valid}")
        selected[station.series_ticker] = station
    return dict(sorted(selected.items()))


def _valid_strike(row: dict[str, Any]) -> bool:
    strike_type = row["strike_type"]
    return (
        (strike_type == "greater" and row["floor_strike"] is not None)
        or (strike_type == "less" and row["cap_strike"] is not None)
        or (
            strike_type == "between"
            and row["floor_strike"] is not None
            and row["cap_strike"] is not None
        )
    )


def assemble_rows(
    markets_by_series: dict[str, list[dict[str, Any]]],
    forecasts: dict[tuple[str, date, int], tuple[dict[str, Any] | None, str | None]],
    candles: dict[str, list[dict[str, Any]]],
    stations: dict[str, SettlementStation],
    candle_request_errors: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, Counter[str]]]:
    """Leakage-safe join; settlement is attached only after forecast and price."""
    rows: list[dict[str, Any]] = []
    drops: dict[int, Counter[str]] = {horizon: Counter() for horizon in HORIZONS}
    request_errors = candle_request_errors or {}
    for series_ticker, station in stations.items():
        for market in markets_by_series.get(series_ticker, []):
            target_day = ticker_target_date(str(market.get("ticker") or ""))
            for horizon in HORIZONS:
                row = empty_dataset_row(market, station, target_day, horizon)
                reason: str | None = None
                if target_day is None:
                    reason = "invalid_target_date"
                elif str(market.get("result", "")).lower() not in {"yes", "no"}:
                    reason = "invalid_result"
                elif not _valid_strike(row):
                    reason = "invalid_strike"

                bundle: dict[str, Any] | None = None
                if reason is None and target_day is not None:
                    bundle, forecast_reason = forecasts.get(
                        (series_ticker, target_day, horizon),
                        (None, "forecast_missing"),
                    )
                    if bundle is None:
                        reason = forecast_reason or "forecast_missing"
                    else:
                        row.update(bundle)
                        row["forecast_implied_yes"] = forecast_implied_yes(
                            float(row["forecast_daily_high_f"]),
                            str(row["strike_type"]),
                            row["floor_strike"],
                            row["cap_strike"],
                        )
                        cutoff = parse_timestamp(bundle["forecast_cutoff_ts"])
                        try:
                            close_time = parse_timestamp(market["close_time"])
                        except (KeyError, TypeError, ValueError):
                            reason = "market_close_time_missing"
                        else:
                            if cutoff >= close_time:
                                reason = "forecast_cutoff_not_before_close"
                            elif market.get("expected_expiration_time"):
                                expiration = parse_timestamp(market["expected_expiration_time"])
                                if cutoff >= expiration:
                                    reason = "forecast_cutoff_not_before_resolution"

                if reason is None and bundle is not None:
                    ticker = str(market["ticker"])
                    if ticker in request_errors:
                        reason = "candle_request_error"
                    else:
                        quote_data, candle_reason = select_candle(
                            candles.get(ticker, []),
                            bundle["forecast_cutoff_ts"],
                        )
                        if quote_data is None:
                            reason = candle_reason or "candle_missing"
                        else:
                            if parse_timestamp(quote_data["candle_end_ts"]) > parse_timestamp(bundle["forecast_cutoff_ts"]):
                                raise AssertionError("selected candle ends after forecast cutoff")
                            row.update(quote_data)

                # Outcomes are deliberately attached last so they cannot affect joins.
                result = str(market.get("result", "")).lower()
                row["result_yes"] = 1 if result == "yes" else (0 if result == "no" else None)
                if reason is None:
                    drops[horizon]["scoreable"] += 1
                else:
                    drops[horizon][reason] += 1
                if tuple(row) != DATASET_FIELDS:
                    raise AssertionError("dataset schema drifted from Section 4")
                rows.append(row)
    assert_unique_grain(rows)
    return rows, drops


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=False) + "\n")


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None or isinstance(value, dict):
        return "n/a"
    return f"{float(value):.{digits}f}"


def render_results_markdown(
    manifest: dict[str, Any],
    scoring: dict[str, Any] | None,
    *,
    fatal_error: str | None = None,
) -> str:
    cities = ", ".join(manifest.get("cities", [])) or "none"
    lines = [
        "# Weather Backtest Results",
        "",
        f"Generated: `{manifest.get('completed_at') or manifest.get('started_at')}`",
        "",
        f"Series/cities: {cities}.",
        "",
        "## Methodology",
        "",
        "Settled KXHIGH markets were paginated by series. Target dates came from each ticker, "
        "and station-local GFS `gfs_seamless` fixed-lead hourly temperatures were reduced to "
        "daily maxima. A quote was usable only when its hourly candle ended at or before the "
        "forecast bundle cutoff, had both closes, and was no more than 120 minutes old. "
        "Market outcomes were attached only after forecast/price matching.",
        "",
        "Deterministic forecasts were converted to probabilities with a zero-bias Gaussian "
        "error model. Sigma was fitted separately per horizon by binary likelihood on expanding "
        "training folds created with `core.forecast_scoring.make_walk_forward_splits`; each row "
        "was scored only out of sample. Brier skill is midpoint Brier minus model Brier. "
            f"Confidence intervals use {manifest.get('bootstrap_resamples')} cluster resamples by "
            "`(station_id, target_date)`. Paper "
        "P&L buys one contract at the executable YES ask or implied NO ask only when absolute "
        f"edge exceeds `{manifest.get('edge_threshold')}`, and subtracts taker fees.",
        "",
    ]
    if fatal_error:
        lines.extend(
            [
                "## Run status",
                "",
                "The real-data run did not complete, so no scores are reported and no synthetic "
                "substitute was used.",
                "",
                f"Error: `{fatal_error}`",
                "",
            ]
        )
    coverage = manifest.get("coverage", {})
    lines.extend(
        [
            "## Coverage",
            "",
            "| Horizon | Candidates | Scoreable joins | Walk-forward scored | Warm-up | Dropped | Drop reasons |",
            "|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for horizon in HORIZONS:
        item = coverage.get(str(horizon), {})
        score_item = scoring.get(str(horizon), {}) if scoring else {}
        reasons = item.get("drop_reasons", {})
        reason_text = "; ".join(f"{key}: {value}" for key, value in sorted(reasons.items())) or "none"
        lines.append(
            f"| {horizon} | {item.get('candidates', 0)} | {item.get('scoreable', 0)} | "
            f"{score_item.get('n', 0)} | {score_item.get('walk_forward_warmup_n', 0)} | "
            f"{item.get('dropped', 0)} | {reason_text} |"
        )
    lines.extend(["", "## Scoring results", ""])
    if scoring:
        lines.extend(
            [
                "| Horizon | N | Indicator Brier | Gaussian Brier | Midpoint Brier | Skill (95% CI) | Gaussian log loss | Net P&L | Trades |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for label in [*(str(horizon) for horizon in HORIZONS), "pooled"]:
            item = scoring.get(label)
            if not item:
                continue
            interval = f"{_fmt(item['brier_skill'])} ({_fmt(item['brier_skill_ci_low'])}, {_fmt(item['brier_skill_ci_high'])})"
            lines.append(
                f"| {label} | {item['n']} | {_fmt(item['indicator_brier'])} | "
                f"{_fmt(item['model_brier'])} | {_fmt(item['market_brier'])} | "
                f"{interval} | {_fmt(item['model_log_loss'])} | ${_fmt(item['paper']['net_pnl'], 2)} | "
                f"{item['paper']['trades']} |"
            )
        lines.extend(["", "### Calibration buckets", ""])
        for label in [*(str(horizon) for horizon in HORIZONS), "pooled"]:
            item = scoring.get(label)
            if not item:
                continue
            lines.extend(
                [
                    f"#### {label}",
                    "",
                    "| Bucket | N | Mean prediction | Observed YES rate |",
                    "|---|---:|---:|---:|",
                ]
            )
            for bucket in item["calibration"]:
                lines.append(
                    f"| {bucket['range']} | {bucket['n']} | {_fmt(bucket['mean_pred'])} | "
                    f"{_fmt(bucket['observed_rate'])} |"
                )
            lines.append("")
    else:
        lines.extend(["No scoring results are available.", ""])
    lines.extend(
        [
            "## Caveats",
            "",
            "- Previous Runs is a rolling fixed-lead product, not one internally consistent model run; the 24 hourly components have different implied issue times.",
            "- Hourly model maxima can miss a brief intrahour peak measured by the continuous settlement sensor.",
            "- Open-Meteo does not expose exact public-release/ingestion timestamps for each fixed-lead component. The implied issue timestamp is therefore an availability assumption, without an added publication buffer.",
            "- Missing, stale, one-sided, crossed, or post-cutoff candles remain missing. No later quote is backfilled.",
            "- Multiple strikes on a station-day share a forecast and weather driver; the bootstrap clusters them together.",
            "",
        ]
    )
    return "\n".join(lines)


def coverage_payload(rows: Sequence[dict[str, Any]], drops: dict[int, Counter[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for horizon in HORIZONS:
        counts = drops[horizon]
        candidates = sum(1 for row in rows if row["horizon_days"] == horizon)
        scoreable = counts.get("scoreable", 0)
        payload[str(horizon)] = {
            "candidates": candidates,
            "scoreable": scoreable,
            "dropped": candidates - scoreable,
            "drop_reasons": {key: value for key, value in counts.items() if key != "scoreable"},
        }
    return payload


def score_dataset(
    rows: Sequence[dict[str, Any]],
    *,
    bootstrap_resamples: int,
    edge_threshold: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    complete = [
        row
        for row in rows
        if row["forecast_daily_high_f"] is not None
        and row["market_mid"] is not None
        and row["result_yes"] is not None
        and row["forecast_implied_yes"] is not None
    ]
    predictions: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    for horizon in HORIZONS:
        horizon_rows = [row for row in complete if row["horizon_days"] == horizon]
        scored = walk_forward_predictions(horizon_rows)
        predictions.extend(scored)
        if scored:
            results[str(horizon)] = score_prediction_set(
                scored,
                bootstrap_resamples=bootstrap_resamples,
                edge_threshold=edge_threshold,
                seed=20260813 + horizon,
            )
            results[str(horizon)]["assembly_scoreable_n"] = len(horizon_rows)
            results[str(horizon)]["walk_forward_warmup_n"] = len(horizon_rows) - len(scored)
    if predictions:
        results["pooled"] = score_prediction_set(
            predictions,
            bootstrap_resamples=bootstrap_resamples,
            edge_threshold=edge_threshold,
            seed=20260813,
        )
        results["pooled"]["assembly_scoreable_n"] = len(complete)
        results["pooled"]["walk_forward_warmup_n"] = len(complete) - len(predictions)
    return results, predictions


def run_backtest(args: argparse.Namespace) -> dict[str, Any]:
    stations = selected_stations(args.cities)
    output_dir = Path(args.output_dir)
    manifest_path = output_dir / "manifest.json"
    dataset_path = output_dir / "dataset.jsonl"
    scores_path = output_dir / "scores.json"
    results_doc = Path(args.results_doc)
    recorder = RequestRecorder()
    started = utc_now()
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": iso_utc(started),
        "completed_at": None,
        "cities": [station.name for station in stations.values()],
        "stations": [asdict(station) for station in stations.values()],
        "horizons": list(HORIZONS),
        "max_price_age_minutes": MAX_PRICE_AGE_MINUTES,
        "edge_threshold": settings.weather_min_edge_threshold,
        "bootstrap_resamples": args.bootstrap_resamples,
        "request_parameters": {
            "kalshi_markets": {"status": "settled", "limit": 100},
            "open_meteo": {
                "hourly": [f"temperature_2m_previous_day{day}" for day in range(1, 8)],
                "models": "gfs_seamless",
                "temperature_unit": "fahrenheit",
                "timezone": "per station",
            },
            "kalshi_candlesticks": {"period_interval": 60},
        },
        "requests": recorder.entries,
        "coverage": {},
    }
    client = RealDataClient(recorder, args.timeout)
    try:
        markets_by_series: dict[str, list[dict[str, Any]]] = {}
        for series_ticker, station in stations.items():
            markets_by_series[series_ticker] = fetch_settled_markets(client, station)
            if not markets_by_series[series_ticker]:
                raise BacktestDataError(
                    f"Kalshi returned no settled markets for required series {series_ticker}"
                )
            print(f"markets {series_ticker}: {len(markets_by_series[series_ticker])}", flush=True)

        forecasts: dict[tuple[str, date, int], tuple[dict[str, Any] | None, str | None]] = {}
        for series_ticker, station in stations.items():
            target_days = sorted(
                {
                    parsed
                    for market in markets_by_series[series_ticker]
                    if (parsed := ticker_target_date(str(market.get("ticker") or ""))) is not None
                }
            )
            if not target_days:
                continue
            hourly = fetch_station_forecast(client, station, target_days[0], target_days[-1])
            for target_day in target_days:
                for horizon in HORIZONS:
                    forecasts[(series_ticker, target_day, horizon)] = compute_forecast_bundle(
                        hourly, target_day, horizon, station.timezone
                    )
            print(
                f"forecasts {station.station_id}: {target_days[0]}..{target_days[-1]}",
                flush=True,
            )

        candle_jobs: dict[str, tuple[SettlementStation, datetime, datetime]] = {}
        for series_ticker, station in stations.items():
            for market in markets_by_series[series_ticker]:
                target_day = ticker_target_date(str(market.get("ticker") or ""))
                if target_day is None:
                    continue
                cutoffs = [
                    parse_timestamp(bundle[0]["forecast_cutoff_ts"])
                    for horizon in HORIZONS
                    if (bundle := forecasts.get((series_ticker, target_day, horizon)))
                    and bundle[0] is not None
                ]
                if not cutoffs:
                    continue
                candle_jobs[str(market["ticker"])] = (
                    station,
                    min(cutoffs) - timedelta(minutes=MAX_PRICE_AGE_MINUTES),
                    max(cutoffs),
                )

        candles: dict[str, list[dict[str, Any]]] = {}
        candle_request_errors: dict[str, str] = {}
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_market_candles, client, station, ticker, start, end): ticker
                for ticker, (station, start, end) in candle_jobs.items()
            }
            completed = 0
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    candles[ticker] = future.result()
                except Exception as exc:  # individual unavailable markets stay missing
                    candle_request_errors[ticker] = str(exc)
                    errors.append(exc)
                completed += 1
                if completed % 100 == 0 or completed == len(futures):
                    print(f"candles: {completed}/{len(futures)}", flush=True)
        if errors:
            auth_error = next((error for error in errors if isinstance(error, CandlestickAuthenticationRequired)), None)
            if auth_error is not None:
                raise auth_error
            if len(errors) == len(futures):
                raise errors[0]

        rows, drops = assemble_rows(
            markets_by_series,
            forecasts,
            candles,
            stations,
            candle_request_errors,
        )
        write_jsonl(dataset_path, rows)
        manifest["coverage"] = coverage_payload(rows, drops)
        scoring, predictions = score_dataset(
            rows,
            bootstrap_resamples=args.bootstrap_resamples,
            edge_threshold=settings.weather_min_edge_threshold,
        )
        write_json(scores_path, {"metrics": scoring, "walk_forward_predictions": predictions})
        manifest["completed_at"] = iso_utc(utc_now())
        manifest["dataset_path"] = str(dataset_path)
        manifest["scores_path"] = str(scores_path)
        manifest["dataset_rows"] = len(rows)
        manifest["walk_forward_scored_rows"] = len(predictions)
        write_json(manifest_path, manifest)
        results_doc.parent.mkdir(parents=True, exist_ok=True)
        results_doc.write_text(render_results_markdown(manifest, scoring))
        return {"manifest": manifest, "scoring": scoring}
    except Exception as exc:
        manifest["completed_at"] = iso_utc(utc_now())
        manifest["fatal_error"] = str(exc)
        write_json(manifest_path, manifest)
        results_doc.parent.mkdir(parents=True, exist_ok=True)
        results_doc.write_text(render_results_markdown(manifest, None, fatal_error=str(exc)))
        raise
    finally:
        client.close()


def print_summary(result: dict[str, Any]) -> None:
    manifest = result["manifest"]
    scoring = result["scoring"]
    print("\nWeather backtest summary")
    print(f"dataset rows: {manifest['dataset_rows']}")
    print(f"walk-forward scored rows: {manifest['walk_forward_scored_rows']}")
    print("horizon  n  model_brier  midpoint_brier  skill  net_pnl")
    for label in [*(str(horizon) for horizon in HORIZONS), "pooled"]:
        item = scoring.get(label)
        if not item:
            continue
        print(
            f"{label:>7}  {item['n']:>5}  {item['model_brier']:.4f}  "
            f"{item['market_brier']:.4f}  {item['brier_skill']:+.4f}  "
            f"${item['paper']['net_pnl']:+.2f}"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cities",
        nargs="+",
        help="Optional honest runtime subsample, e.g. --cities denver chicago (default: all seven)",
    )
    parser.add_argument("--output-dir", default="data/runtime/backtest")
    parser.add_argument("--results-doc", default="docs/backtest-results.md")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--bootstrap-resamples", type=int, default=1000)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.bootstrap_resamples < 1:
        parser.error("--bootstrap-resamples must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_backtest(args)
    except (BacktestDataError, httpx.HTTPError, ValueError) as exc:
        print(f"BACKTEST FAILED: {exc}", file=sys.stderr)
        return 2
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
