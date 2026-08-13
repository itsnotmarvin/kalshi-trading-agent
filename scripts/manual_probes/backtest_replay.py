"""
Point-in-time replay harness for a resolved Kalshi market.

Target use case:
    Who will Trump nominate as Fed Chair? -> Kevin Warsh
    KXFEDCHAIRNOM-29-KW

The public Kalshi API no longer serves this resolved market's history in this
environment, so this script reads the chart Download CSV instead. The expected
CSV shape is an event-level export:

    timestamp,<candidate 1>,<candidate 2>,...,Kevin Warsh,...

Values are YES prices in cents. The script converts them to dollars, forward
fills sparse dates, freezes the existing Early Marks scorer at each as-of date,
and grades the mechanical calls without using future bars.

Read-only: this script never places, cancels, or approves orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manual_probes import test_early_marks as early_marks


TARGET_TICKER = "KXFEDCHAIRNOM-29-KW"
TARGET_COLUMN = "Kevin Warsh"
MARKET_TITLE = "Who will Trump nominate as Fed Chair?"
EVENT_TITLE = "Fed Chair nomination"
CATEGORY = "Politics"
OPEN_DATE = date(2024, 12, 19)
CLOSE_DATE = date(2026, 3, 3)
CLOSE_TIME = datetime(2026, 3, 3, 23, 59, 59, tzinfo=timezone.utc)
RESOLUTION = "YES"


@dataclass(frozen=True)
class PriceBar:
    as_of: date
    price: float
    source_timestamp: str
    rival_prices: dict[str, float]


@dataclass(frozen=True)
class LoaderSummary:
    csv_path: str
    timestamp_column: str
    target_column: str
    raw_rows: int
    post_close_rows_dropped: int
    native_bars: int
    replay_bars: int
    date_start: str
    date_end: str
    price_min: float
    price_max: float
    price_open: float
    price_close: float
    rival_columns: list[str]


@dataclass(frozen=True)
class TradeSimulation:
    entered: bool
    entry_date: str | None
    entry_price: float | None
    entry_target: float | None
    exit_date: str | None
    exit_price: float | None
    exit_reason: str | None
    contracts: int
    gross_cost: float
    gross_proceeds: float
    fees: float
    spread_cost: float
    net_pnl: float
    roi: float | None


@dataclass(frozen=True)
class ResearchSource:
    title: str
    url: str
    content: str
    published_date: str | None
    date_source: str | None
    score: float | None
    admissible: bool
    rejection_reason: str | None


def normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_timestamp(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("empty timestamp")
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp: {value!r}")


def parse_source_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(str(value)).date()
    except ValueError:
        pass
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return date.fromisoformat(match.group(0))
    return None


def parse_price(value: Any) -> float | None:
    """Parse Kalshi chart export values, which are YES prices in cents."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    text = text.removesuffix("%").strip()
    text = text.removeprefix("$").strip()
    try:
        parsed = float(text)
    except ValueError:
        return None
    if math.isnan(parsed):
        return None
    parsed = parsed / 100.0
    return max(0.0, min(1.0, parsed))


def find_column(fieldnames: Iterable[str], desired: str, aliases: Iterable[str] = ()) -> str:
    fields = [field for field in fieldnames if field is not None]
    normalized = {normalize_label(field): field for field in fields}
    for candidate in (desired, *aliases):
        match = normalized.get(normalize_label(candidate))
        if match:
            return match

    desired_norm = normalize_label(desired)
    contains = [field for field in fields if desired_norm in normalize_label(field)]
    if len(contains) == 1:
        return contains[0]
    if contains:
        raise ValueError(f"ambiguous column for {desired!r}: {contains}")
    raise ValueError(f"missing column {desired!r}; available columns: {fields}")


def load_event_csv(
    csv_path: Path,
    *,
    target_column: str = TARGET_COLUMN,
    cadence: str = "daily",
) -> tuple[list[PriceBar], LoaderSummary]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{csv_path} has no header row")

        timestamp_column = find_column(
            reader.fieldnames,
            "timestamp",
            aliases=("time", "date", "datetime", "created_at"),
        )
        target = find_column(reader.fieldnames, target_column)
        rival_columns = [
            field
            for field in reader.fieldnames
            if field not in {timestamp_column, target} and field is not None and field.strip()
        ]

        raw_rows = 0
        post_close_rows_dropped = 0
        native_by_date: dict[date, PriceBar] = {}
        native_sparse: list[PriceBar] = []
        for row in reader:
            raw_rows += 1
            timestamp_text = str(row.get(timestamp_column) or "")
            timestamp = parse_timestamp(timestamp_text)
            if timestamp.date() > CLOSE_DATE:
                post_close_rows_dropped += 1
                continue
            price = parse_price(row.get(target))
            if price is None:
                continue

            rivals = {
                column: parsed
                for column in rival_columns
                if (parsed := parse_price(row.get(column))) is not None
            }
            bar = PriceBar(
                as_of=timestamp.date(),
                price=price,
                source_timestamp=timestamp_text,
                rival_prices=rivals,
            )
            native_sparse.append(bar)
            native_by_date[bar.as_of] = bar

    if not native_sparse:
        raise ValueError(f"{csv_path} did not contain any parseable {target_column!r} prices")

    native_sparse.sort(key=lambda bar: bar.as_of)
    if cadence == "native":
        bars = native_sparse
    elif cadence == "daily":
        bars = forward_fill_daily(native_by_date)
    else:
        raise ValueError(f"unsupported cadence {cadence!r}; use daily or native")

    prices = [bar.price for bar in bars]
    summary = LoaderSummary(
        csv_path=str(csv_path),
        timestamp_column=timestamp_column,
        target_column=target,
        raw_rows=raw_rows,
        post_close_rows_dropped=post_close_rows_dropped,
        native_bars=len(native_sparse),
        replay_bars=len(bars),
        date_start=bars[0].as_of.isoformat(),
        date_end=bars[-1].as_of.isoformat(),
        price_min=round(min(prices), 4),
        price_max=round(max(prices), 4),
        price_open=round(bars[0].price, 4),
        price_close=round(bars[-1].price, 4),
        rival_columns=rival_columns,
    )
    return bars, summary


def forward_fill_daily(native_by_date: dict[date, PriceBar]) -> list[PriceBar]:
    dates = sorted(native_by_date)
    start = dates[0]
    end = dates[-1]
    current = native_by_date[start]
    bars: list[PriceBar] = []
    day = start
    while day <= end:
        if day in native_by_date:
            current = native_by_date[day]
        else:
            current = PriceBar(
                as_of=day,
                price=current.price,
                source_timestamp=current.source_timestamp,
                rival_prices=current.rival_prices,
            )
        bars.append(current if current.as_of == day else PriceBar(day, current.price, current.source_timestamp, current.rival_prices))
        day += timedelta(days=1)
    return bars


def as_of_datetime(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)


@contextmanager
def frozen_scorer_clock(as_of: datetime):
    original_datetime = early_marks.datetime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return as_of.replace(tzinfo=None)
            return as_of.astimezone(tz)

    early_marks.datetime = FrozenDateTime
    try:
        yield
    finally:
        early_marks.datetime = original_datetime


def build_snapshot(bars: list[PriceBar], index: int) -> dict[str, Any]:
    bar = bars[index]
    previous_price = bars[index - 1].price if index > 0 else bar.price
    price = bar.price
    no_price = 1.0 - price
    return {
        "ticker": TARGET_TICKER,
        "title": MARKET_TITLE,
        "event_title": EVENT_TITLE,
        "category": CATEGORY,
        "status": "active",
        "yes_sub_title": TARGET_COLUMN,
        "no_sub_title": TARGET_COLUMN,
        "subtitle": TARGET_COLUMN,
        "yes_ask_dollars": f"{price:.4f}",
        "yes_bid_dollars": f"{price:.4f}",
        "no_ask_dollars": f"{no_price:.4f}",
        "no_bid_dollars": f"{no_price:.4f}",
        "last_price_dollars": f"{price:.4f}",
        "previous_price_dollars": f"{previous_price:.4f}",
        "volume_fp": "0",
        "volume_24h_fp": "0",
        "liquidity_dollars": "0",
        "open_time": datetime.combine(OPEN_DATE, time.min, tzinfo=timezone.utc).isoformat(),
        "expected_expiration_time": CLOSE_TIME.isoformat(),
        "close_time": CLOSE_TIME.isoformat(),
        "settlement_source_url": "Kalshi market metadata",
    }


def score_as_of(bars: list[PriceBar], index: int) -> early_marks.Candidate:
    snapshot = build_snapshot(bars, index)
    with frozen_scorer_clock(as_of_datetime(bars[index].as_of)):
        candidate = early_marks.score_raw_market(snapshot)
    if candidate is None:
        raise RuntimeError(f"scorer rejected snapshot at index={index} date={bars[index].as_of}")
    return candidate


def rank_rivals(bar: PriceBar) -> dict[str, Any]:
    field = [(TARGET_COLUMN, bar.price), *bar.rival_prices.items()]
    ranked = sorted(field, key=lambda item: item[1], reverse=True)
    rank = next((idx + 1 for idx, (name, _) in enumerate(ranked) if name == TARGET_COLUMN), None)
    top = [
        {"name": name, "price": round(price, 4)}
        for name, price in ranked[:5]
    ]
    return {
        "warsh_rank": rank,
        "field_count": len(ranked),
        "top_prices": top,
    }


def lean_from_candidate(candidate: early_marks.Candidate, price: float) -> str:
    target = candidate.expected_future_price
    if candidate.probe_status == "Too directionless" or target is None:
        return "WATCH"
    if candidate.probe_status == "Catalyst setup" and target > price:
        return "YES"
    if target < price * 0.98:
        return "NO"
    return "WATCH"


def model_probability(candidate: early_marks.Candidate, price: float) -> float:
    """
    Convert the scorer output into a settlement probability for calibration.

    The Early Marks scorer's bull/base/bear weights are explicitly resale-scenario
    weights, not final settlement probabilities. For this replay, the least
    magical reproducible mapping is the scorer's expected future price when
    available, otherwise the market price. That keeps the calibration audit honest:
    it is mostly measuring market-implied probability plus the scorer's timing tilt.
    """
    if candidate.expected_future_price is None:
        return price
    return max(0.0, min(1.0, float(candidate.expected_future_price)))


def kalshi_fee(contracts: int, price: float, *, maker: bool = False) -> float:
    multiplier = 0.0175 if maker else 0.07
    fee = multiplier * contracts * price * (1.0 - price)
    return math.ceil(fee * 100.0) / 100.0


def round_trip_cost(
    contracts: int,
    entry_price: float,
    exit_price: float,
    *,
    spread_assumption: float,
    maker: bool = False,
) -> tuple[float, float]:
    entry_fee = kalshi_fee(contracts, entry_price, maker=maker)
    exit_fee = kalshi_fee(contracts, exit_price, maker=maker)
    spread_cost = contracts * spread_assumption
    return entry_fee + exit_fee, spread_cost


def build_decision_log(bars: list[PriceBar]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        candidate = score_as_of(bars, index)
        probability = model_probability(candidate, bar.price)
        lean = lean_from_candidate(candidate, bar.price)
        decisions.append({
            "index": index,
            "as_of": bar.as_of.isoformat(),
            "ticker": TARGET_TICKER,
            "inputs": {
                "price": round(bar.price, 4),
                "previous_price": round(bars[index - 1].price if index > 0 else bar.price, 4),
                "yes_bid": round(bar.price, 4),
                "yes_ask": round(bar.price, 4),
                "volume": 0,
                "volume_24h": 0,
                "rival_context": rank_rivals(bar),
            },
            "scorer": {
                "rank_score": candidate.rank_score,
                "probe_status": candidate.probe_status,
                "bull_probability": candidate.bull_probability,
                "base_probability": candidate.base_probability,
                "bear_probability": candidate.bear_probability,
                "bull_price": candidate.bull_price,
                "base_price": candidate.base_price,
                "bear_price": candidate.bear_price,
                "expected_future_price": candidate.expected_future_price,
                "expected_repricing_roi": candidate.expected_repricing_roi,
                "entry_limit": candidate.entry_limit,
                "catalyst_score": candidate.catalyst_score,
                "runway_score": candidate.runway_score,
                "momentum_score": candidate.momentum_score,
                "liquidity_score": candidate.liquidity_score,
                "flow_signal_score": candidate.flow_signal_score,
                "directionless_penalty": candidate.directionless_penalty,
                "days_to_resolution": candidate.days_to_resolution,
            },
            "decision": {
                "lean": lean,
                "model_probability": round(probability, 4),
                "reason": decision_reason(candidate, bar.price),
            },
        })
    return decisions


def checkpoint_indexes(bars: list[PriceBar], every_days: int) -> list[int]:
    if not bars:
        return []
    if every_days <= 0:
        return [0, len(bars) - 1]

    indexes = {0, len(bars) - 1}
    start = bars[0].as_of
    target = start + timedelta(days=every_days)
    while target < bars[-1].as_of:
        index = max(
            idx for idx, bar in enumerate(bars)
            if bar.as_of <= target
        )
        indexes.add(index)
        target += timedelta(days=every_days)
    return sorted(indexes)


def research_cutoff_for_checkpoint(index: int, checkpoint_date: date) -> date:
    if index == 0:
        return checkpoint_date - timedelta(days=1)
    return checkpoint_date


def research_start_for_checkpoint(cutoff: date, lookback_days: int) -> date:
    if lookback_days <= 0:
        return OPEN_DATE
    return cutoff - timedelta(days=lookback_days)


def build_research_query(cutoff: date) -> str:
    return (
        f"Kevin Warsh Trump Fed Chair nomination Federal Reserve chair "
        f"public news before {cutoff.isoformat()}"
    )


def tavily_search(
    *,
    query: str,
    start_date: date,
    end_date: date,
    max_results: int,
    api_key: str,
    topic: str = "news",
) -> dict[str, Any]:
    payload = {
        "api_key": api_key,
        "query": query,
        "topic": topic,
        "search_depth": "advanced",
        "max_results": max(0, min(max_results, 20)),
        "chunks_per_source": 3,
        "include_answer": False,
        "include_raw_content": False,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tavily search failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Tavily search failed: {exc}") from exc


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class PageDateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.time_values: list[str] = []
        self.json_ld_blocks: list[str] = []
        self._in_json_ld = False
        self._json_ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {str(key).lower(): value for key, value in attrs if value is not None}
        tag_l = tag.lower()
        if tag_l == "meta":
            key = attr.get("property") or attr.get("name") or attr.get("itemprop")
            content = attr.get("content")
            if key and content:
                self.meta[key.strip().lower()] = content.strip()
        elif tag_l == "time" and attr.get("datetime"):
            self.time_values.append(str(attr["datetime"]).strip())
        elif tag_l == "script" and "ld+json" in str(attr.get("type") or "").lower():
            self._in_json_ld = True
            self._json_ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_json_ld:
            block = "".join(self._json_ld_chunks).strip()
            if block:
                self.json_ld_blocks.append(block)
            self._in_json_ld = False
            self._json_ld_chunks = []


def iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_json_objects(nested)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)


def json_ld_published_date(blocks: list[str]) -> tuple[date | None, str | None]:
    for block in blocks:
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        for obj in iter_json_objects(parsed):
            for key in ("datePublished", "dateCreated"):
                published = parse_source_date(obj.get(key))
                if published is not None:
                    return published, f"json_ld.{key}"
    return None, None


def metadata_published_date(html: str) -> tuple[date | None, str | None]:
    parser = PageDateParser()
    parser.feed(html)

    published, source = json_ld_published_date(parser.json_ld_blocks)
    if published is not None:
        return published, source

    meta_keys = (
        "article:published_time",
        "og:published_time",
        "datepublished",
        "date_published",
        "publishdate",
        "pubdate",
        "date",
        "dc.date",
        "dc.date.issued",
        "dcterms.created",
        "dcterms.issued",
        "sailthru.date",
        "parsely-pub-date",
    )
    for key in meta_keys:
        published = parse_source_date(parser.meta.get(key))
        if published is not None:
            return published, f"meta.{key}"

    for value in parser.time_values:
        published = parse_source_date(value)
        if published is not None:
            return published, "time.datetime"
    return None, None


def fetch_page_published_date(url: str) -> tuple[date | None, str | None, str | None]:
    if not url:
        return None, None, "missing url"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KalshiBacktestResearch/0.1 (point-in-time source date validation)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=ssl_context()) as response:
            content_type = str(response.headers.get("content-type") or "")
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(750_000).decode(charset, errors="replace")
    except Exception as exc:
        return None, None, f"page fetch failed: {type(exc).__name__}: {exc}"
    if "html" not in content_type.lower() and "<html" not in body[:5000].lower():
        return None, None, f"page fetch was not html: {content_type or 'unknown content-type'}"
    published, source = metadata_published_date(body)
    if published is None:
        return None, None, "page metadata missing published date"
    return published, source, None


def validate_research_sources(
    raw_results: list[dict[str, Any]],
    *,
    start_date: date,
    cutoff: date,
    allow_undated_sources: bool,
    infer_published_from_page: bool,
) -> list[ResearchSource]:
    sources: list[ResearchSource] = []
    for result in raw_results:
        published_raw = result.get("published_date") or result.get("date")
        published = parse_source_date(published_raw)
        date_source = "search_result.published_date" if published is not None else None
        fetch_error = None
        if published is None and infer_published_from_page:
            published, date_source, fetch_error = fetch_page_published_date(str(result.get("url") or ""))
        rejection = None
        if published is None and not allow_undated_sources:
            rejection = f"missing published_date ({fetch_error})" if fetch_error else "missing published_date"
        elif published is not None and published > cutoff:
            rejection = f"published after cutoff {cutoff.isoformat()}"
        elif published is not None and published < start_date:
            rejection = f"published before search window {start_date.isoformat()}"

        score = None
        try:
            score = float(result.get("score")) if result.get("score") is not None else None
        except (TypeError, ValueError):
            score = None

        sources.append(ResearchSource(
            title=str(result.get("title") or ""),
            url=str(result.get("url") or ""),
            content=str(result.get("content") or "")[:1200],
            published_date=published.isoformat() if published else None,
            date_source=date_source,
            score=score,
            admissible=rejection is None,
            rejection_reason=rejection,
        ))
    return sources


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("LLM did not return a JSON object")
    parsed = json.loads(text[start:end])
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON was not an object")
    return parsed


def build_checkpoint_llm_prompt(
    *,
    checkpoint: dict[str, Any],
    admissible_sources: list[ResearchSource],
) -> str:
    source_payload = [
        {
            "id": idx + 1,
            "title": source.title,
            "url": source.url,
            "published_date": source.published_date,
            "content": source.content,
        }
        for idx, source in enumerate(admissible_sources)
    ]
    return f"""
You are replaying a resolved prediction market, but you must behave as if your
current date is exactly {checkpoint['research_cutoff']}. The true resolution is
hidden from you. You must not use any knowledge, article, fact, or implication
after {checkpoint['research_cutoff']}.

Market:
- Ticker: {TARGET_TICKER}
- Question: {MARKET_TITLE}
- Contract: {TARGET_COLUMN}
- As-of market state: {json.dumps(checkpoint['market_state'], sort_keys=True)}
- Mechanical scorer state: {json.dumps(checkpoint['mechanical_scorer'], sort_keys=True)}

Admissible public sources. These were pre-filtered so published_date is on or
before the cutoff. If the sources are thin, say so; do not fill gaps with later
knowledge.
{json.dumps(source_payload, indent=2, sort_keys=True)}

Return one strictly valid JSON object:
{{
  "as_of": "{checkpoint['as_of']}",
  "research_cutoff": "{checkpoint['research_cutoff']}",
  "probability_yes": number,
  "final_outcome_prediction": "YES | NO | UNCERTAIN",
  "trade_action": "BUY_YES | HOLD | AVOID",
  "rationale": "Short first-person explanation using only market state and admitted sources.",
  "source_ids_used": [number],
  "leakage_check": "One sentence confirming no post-cutoff facts were used."
}}
"""


def ask_checkpoint_llm(
    *,
    checkpoint: dict[str, Any],
    admissible_sources: list[ResearchSource],
) -> dict[str, Any]:
    if not early_marks.settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for --checkpoint-llm claude")
    client = early_marks.Anthropic(api_key=early_marks.settings.anthropic_api_key)
    request_kwargs = early_marks._anthropic_message_kwargs(
        max_tokens=1800,
        temperature=0,
        system=(
            "You are a point-in-time prediction-market analyst. "
            "You obey source cutoffs strictly and never use hindsight."
        ),
        messages=[{
            "role": "user",
            "content": build_checkpoint_llm_prompt(
                checkpoint=checkpoint,
                admissible_sources=admissible_sources,
            ),
        }],
    )
    request_kwargs["thinking"] = {"type": "disabled"}
    response = client.messages.create(**request_kwargs)
    raw_text = response.content[0].text
    try:
        parsed = extract_json_object(raw_text)
        parsed["raw_text"] = raw_text
        parsed["parse_error"] = None
        return parsed
    except Exception as exc:
        return {
            "raw_text": raw_text,
            "parse_error": str(exc),
            "final_outcome_prediction": None,
            "probability_yes": None,
            "trade_action": None,
            "rationale": None,
        }


def load_research_pack(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text())
    if isinstance(payload, dict) and isinstance(payload.get("checkpoints"), list):
        return {
            str(item.get("as_of") or item.get("cutoff") or ""): list(item.get("sources") or [])
            for item in payload["checkpoints"]
            if isinstance(item, dict)
        }
    if isinstance(payload, dict):
        return {
            str(key): list(value)
            for key, value in payload.items()
            if isinstance(value, list)
        }
    raise ValueError("research pack must be a dict keyed by checkpoint date or {'checkpoints': [...]}")


def sources_for_checkpoint(
    *,
    checkpoint_date: date,
    cutoff: date,
    start_date: date,
    provider: str,
    query: str,
    max_results: int,
    allow_undated_sources: bool,
    tavily_api_key: str,
    research_pack: dict[str, list[dict[str, Any]]] | None,
) -> tuple[list[ResearchSource], dict[str, Any]]:
    raw_results: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "provider": provider,
        "query": query,
        "search_start_date": start_date.isoformat(),
        "search_end_date": cutoff.isoformat(),
        "no_future_policy": "Sources are admitted by published_date only; later edit dates are ignored by user-selected policy.",
    }
    if provider == "none":
        meta["status"] = "skipped"
    elif provider == "pack":
        if research_pack is None:
            raise ValueError("--research-provider pack requires --research-json")
        raw_results = research_pack.get(checkpoint_date.isoformat()) or research_pack.get(cutoff.isoformat()) or []
        meta["status"] = "loaded_from_pack"
    elif provider == "tavily":
        if not tavily_api_key:
            raise ValueError("--research-provider tavily requires TAVILY_API_KEY or --tavily-api-key")
        response = tavily_search(
            query=query,
            start_date=start_date,
            end_date=cutoff,
            max_results=max_results,
            api_key=tavily_api_key,
        )
        raw_results = list(response.get("results") or [])
        meta["status"] = "searched"
        meta["request_id"] = response.get("request_id")
        meta["total_results"] = response.get("total_results")
    else:
        raise ValueError(f"unknown research provider {provider!r}")

    sources = validate_research_sources(
        raw_results,
        start_date=start_date,
        cutoff=cutoff,
        allow_undated_sources=allow_undated_sources,
        infer_published_from_page=(provider == "tavily"),
    )
    meta["admissible_sources"] = sum(1 for source in sources if source.admissible)
    meta["rejected_sources"] = sum(1 for source in sources if not source.admissible)
    return sources, meta


def checkpoint_final_call(decision: dict[str, Any]) -> str:
    probability = float(decision["decision"]["model_probability"])
    lean = decision["decision"]["lean"]
    if lean == "YES":
        return "Predict YES"
    if lean == "NO":
        return "Predict NO"
    if probability >= 0.60:
        return "Lean YES"
    if probability <= 0.40:
        return "Lean NO"
    return "No directional prediction"


def checkpoint_research_note(
    decision: dict[str, Any],
    sources: list[ResearchSource],
    meta: dict[str, Any],
) -> str:
    admitted = [source for source in sources if source.admissible]
    if meta["provider"] == "none":
        return "No external research run; checkpoint uses market state only."
    if not admitted:
        return "Search ran, but no dated sources passed the as-of cutoff guard."
    titles = "; ".join(source.title for source in admitted[:3] if source.title)
    return (
        f"Admitted {len(admitted)} source(s) dated no later than {meta['search_end_date']}. "
        f"Top admitted titles: {titles or 'untitled sources'}."
    )


def build_checkpoints(
    decisions: list[dict[str, Any]],
    bars: list[PriceBar],
    *,
    every_days: int,
    research_provider: str,
    research_lookback_days: int,
    research_max_results: int,
    allow_undated_sources: bool,
    tavily_api_key: str,
    research_pack: dict[str, list[dict[str, Any]]] | None,
    checkpoint_llm: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in checkpoint_indexes(bars, every_days):
        bar = bars[index]
        decision = decisions[index]
        cutoff = research_cutoff_for_checkpoint(index, bar.as_of)
        start_date = research_start_for_checkpoint(cutoff, research_lookback_days)
        query = build_research_query(cutoff)
        sources, meta = sources_for_checkpoint(
            checkpoint_date=bar.as_of,
            cutoff=cutoff,
            start_date=start_date,
            provider=research_provider,
            query=query,
            max_results=research_max_results,
            allow_undated_sources=allow_undated_sources,
            tavily_api_key=tavily_api_key,
            research_pack=research_pack,
        )
        checkpoint = {
            "index": index,
            "as_of": bar.as_of.isoformat(),
            "research_cutoff": cutoff.isoformat(),
            "market_state": decision["inputs"],
            "mechanical_scorer": decision["scorer"],
            "mechanical_call": decision["decision"],
            "final_outcome_prediction": checkpoint_final_call(decision),
            "research": {
                **meta,
                "sources": [asdict(source) for source in sources],
                "note": checkpoint_research_note(decision, sources, meta),
            },
            "llm_research_call": None,
        }
        if checkpoint_llm == "claude":
            admitted = [source for source in sources if source.admissible]
            checkpoint["llm_research_call"] = ask_checkpoint_llm(
                checkpoint=checkpoint,
                admissible_sources=admitted,
            )
        elif checkpoint_llm != "none":
            raise ValueError(f"unknown checkpoint LLM {checkpoint_llm!r}")
        rows.append(checkpoint)
    return rows


def decision_reason(candidate: early_marks.Candidate, price: float) -> str:
    if candidate.expected_future_price is None:
        return f"{candidate.probe_status}; no target price from scorer."
    direction = "above" if candidate.expected_future_price >= price else "below"
    return (
        f"{candidate.probe_status}; target {candidate.expected_future_price:.2%} is "
        f"{direction} as-of price {price:.2%}; rank={candidate.rank_score:.1f}, "
        f"momentum={candidate.momentum_score:.1f}, runway={candidate.runway_score:.1f}."
    )


def first_entry_index(
    decisions: list[dict[str, Any]],
    *,
    rank_threshold: float,
) -> int | None:
    for decision in decisions:
        if (
            decision["scorer"]["probe_status"] == "Catalyst setup"
            and decision["scorer"]["rank_score"] >= rank_threshold
            and decision["decision"]["lean"] == "YES"
        ):
            return int(decision["index"])
    return None


def find_exit_index(
    decisions: list[dict[str, Any]],
    bars: list[PriceBar],
    entry_index: int,
) -> tuple[int, str]:
    entry_target = decisions[entry_index]["scorer"]["expected_future_price"]
    target = float(entry_target) if entry_target is not None else None
    for index in range(entry_index + 1, len(decisions)):
        decision = decisions[index]
        price = bars[index].price
        if target is not None and price >= target:
            return index, "take_profit_target"
        if decision["decision"]["lean"] == "NO":
            return index, "flip_to_bear"
    return len(decisions) - 1, "resolution"


def simulate_model_trade(
    decisions: list[dict[str, Any]],
    bars: list[PriceBar],
    *,
    stake_dollars: float,
    rank_threshold: float,
    spread_assumption: float,
    maker: bool = False,
) -> TradeSimulation:
    entry_index = first_entry_index(decisions, rank_threshold=rank_threshold)
    if entry_index is None:
        return TradeSimulation(
            entered=False,
            entry_date=None,
            entry_price=None,
            entry_target=None,
            exit_date=None,
            exit_price=None,
            exit_reason=None,
            contracts=0,
            gross_cost=0.0,
            gross_proceeds=0.0,
            fees=0.0,
            spread_cost=0.0,
            net_pnl=0.0,
            roi=None,
        )

    entry_price = bars[entry_index].price
    contracts = max(1, int(stake_dollars // entry_price))
    exit_index, exit_reason = find_exit_index(decisions, bars, entry_index)
    exit_price = 1.0 if exit_reason == "resolution" and RESOLUTION == "YES" else bars[exit_index].price
    fees, spread_cost = round_trip_cost(
        contracts,
        entry_price,
        exit_price,
        spread_assumption=spread_assumption,
        maker=maker,
    )
    gross_cost = contracts * entry_price
    gross_proceeds = contracts * exit_price
    net_pnl = gross_proceeds - gross_cost - fees - spread_cost
    return TradeSimulation(
        entered=True,
        entry_date=bars[entry_index].as_of.isoformat(),
        entry_price=round(entry_price, 4),
        entry_target=decisions[entry_index]["scorer"]["expected_future_price"],
        exit_date=bars[exit_index].as_of.isoformat(),
        exit_price=round(exit_price, 4),
        exit_reason=exit_reason,
        contracts=contracts,
        gross_cost=round(gross_cost, 2),
        gross_proceeds=round(gross_proceeds, 2),
        fees=round(fees, 2),
        spread_cost=round(spread_cost, 2),
        net_pnl=round(net_pnl, 2),
        roi=round(net_pnl / gross_cost, 4) if gross_cost else None,
    )


def simulate_buy_hold(
    bars: list[PriceBar],
    *,
    stake_dollars: float,
    entry_index: int,
    spread_assumption: float,
    maker: bool = False,
) -> TradeSimulation:
    entry_price = bars[entry_index].price
    contracts = max(1, int(stake_dollars // entry_price))
    exit_price = 1.0 if RESOLUTION == "YES" else 0.0
    fees, spread_cost = round_trip_cost(
        contracts,
        entry_price,
        exit_price,
        spread_assumption=spread_assumption,
        maker=maker,
    )
    gross_cost = contracts * entry_price
    gross_proceeds = contracts * exit_price
    net_pnl = gross_proceeds - gross_cost - fees - spread_cost
    return TradeSimulation(
        entered=True,
        entry_date=bars[entry_index].as_of.isoformat(),
        entry_price=round(entry_price, 4),
        entry_target=None,
        exit_date=bars[-1].as_of.isoformat(),
        exit_price=round(exit_price, 4),
        exit_reason="resolution",
        contracts=contracts,
        gross_cost=round(gross_cost, 2),
        gross_proceeds=round(gross_proceeds, 2),
        fees=round(fees, 2),
        spread_cost=round(spread_cost, 2),
        net_pnl=round(net_pnl, 2),
        roi=round(net_pnl / gross_cost, 4) if gross_cost else None,
    )


def brier_score(decisions: list[dict[str, Any]]) -> float:
    outcome = 1.0 if RESOLUTION == "YES" else 0.0
    terms = [(float(row["decision"]["model_probability"]) - outcome) ** 2 for row in decisions]
    return sum(terms) / len(terms) if terms else 0.0


def outcome_accuracy(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    directional = [
        row for row in decisions
        if row["decision"]["lean"] in {"YES", "NO"}
    ]
    final_freeze_lean = decisions[-1]["decision"]["lean"] if decisions else None
    if not directional:
        return {
            "resolution": RESOLUTION,
            "final_freeze_lean": final_freeze_lean,
            "last_directional_lean": None,
            "last_directional_date": None,
            "match": None,
            "note": "No YES/NO directional lean was emitted; final-outcome accuracy is not scoreable.",
        }
    last = directional[-1]
    last_lean = last["decision"]["lean"]
    return {
        "resolution": RESOLUTION,
        "final_freeze_lean": final_freeze_lean,
        "last_directional_lean": last_lean,
        "last_directional_date": last["as_of"],
        "match": last_lean == RESOLUTION,
        "note": "Accuracy is based on the most recent YES/NO lean before or at resolution; WATCH is not counted as a directional call.",
    }


def calibration_buckets(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcome = 1.0 if RESOLUTION == "YES" else 0.0
    buckets: dict[int, list[float]] = {idx: [] for idx in range(10)}
    for row in decisions:
        prob = float(row["decision"]["model_probability"])
        bucket = min(9, int(prob * 10))
        buckets[bucket].append(prob)
    result = []
    for bucket, probs in buckets.items():
        if not probs:
            continue
        avg_prob = sum(probs) / len(probs)
        result.append({
            "bucket": f"{bucket * 10}-{(bucket + 1) * 10}%",
            "count": len(probs),
            "avg_predicted": round(avg_prob, 4),
            "actual": outcome,
            "gap": round(outcome - avg_prob, 4),
        })
    return result


def assert_no_lookahead(bars: list[PriceBar], sample_indexes: Iterable[int] | None = None) -> None:
    if sample_indexes is None:
        if len(bars) <= 12:
            sample_indexes = range(len(bars))
        else:
            sample_indexes = sorted({0, 1, len(bars) // 4, len(bars) // 2, (len(bars) * 3) // 4, len(bars) - 2, len(bars) - 1})

    for index in sample_indexes:
        full = score_as_of(bars, index)
        truncated = score_as_of(bars[: index + 1], index)
        fields = (
            "rank_score",
            "probe_status",
            "bull_probability",
            "base_probability",
            "bear_probability",
            "expected_future_price",
            "entry_limit",
        )
        for field in fields:
            if getattr(full, field) != getattr(truncated, field):
                raise AssertionError(
                    f"lookahead check failed at index {index} field {field}: "
                    f"{getattr(full, field)!r} != {getattr(truncated, field)!r}"
                )


def choose_timeline_points(decisions: list[dict[str, Any]], bars: list[PriceBar], max_points: int) -> list[int]:
    selected = {0, len(bars) - 1}
    entry = first_entry_index(decisions, rank_threshold=48.0)
    if entry is not None:
        selected.add(entry)
    selected.add(min(range(len(bars)), key=lambda idx: bars[idx].price))
    selected.add(max(range(len(bars)), key=lambda idx: bars[idx].price))

    for index in range(1, len(bars)):
        previous = bars[index - 1].price
        current = bars[index].price
        if previous > 0 and abs(current - previous) / previous >= 0.20:
            selected.add(index)

    if len(selected) < max_points:
        stride = max(1, len(bars) // max_points)
        for index in range(0, len(bars), stride):
            selected.add(index)
            if len(selected) >= max_points:
                break

    return sorted(selected)[:max_points]


def narrated_timeline(decisions: list[dict[str, Any]], bars: list[PriceBar], max_points: int) -> list[dict[str, Any]]:
    points = choose_timeline_points(decisions, bars, max_points)
    rows = []
    for index in points:
        row = decisions[index]
        next_index = min(index + 1, len(bars) - 1)
        field = row["inputs"]["rival_context"]
        rows.append({
            "as_of": row["as_of"],
            "price_cents": round(bars[index].price * 100, 2),
            "warsh_rank": field["warsh_rank"],
            "field_count": field["field_count"],
            "top_field": field["top_prices"],
            "call": row["decision"]["lean"],
            "probe_status": row["scorer"]["probe_status"],
            "model_probability": row["decision"]["model_probability"],
            "target_cents": (
                round(float(row["scorer"]["expected_future_price"]) * 100, 2)
                if row["scorer"]["expected_future_price"] is not None
                else None
            ),
            "why": row["decision"]["reason"],
            "next_move": {
                "to_date": bars[next_index].as_of.isoformat(),
                "price_cents": round(bars[next_index].price * 100, 2),
                "change_cents": round((bars[next_index].price - bars[index].price) * 100, 2),
            },
        })
    return rows


def build_report(
    *,
    summary: LoaderSummary,
    decisions: list[dict[str, Any]],
    model_trade: TradeSimulation,
    buy_from_model_entry: TradeSimulation | None,
    buy_at_open: TradeSimulation,
    timeline: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    spread_assumption: float,
    rank_threshold: float,
) -> str:
    lines = [
        "Point-in-Time Backtest: Fed Chair / Kevin Warsh",
        "=" * 54,
        "",
        "Loader dry run",
        f"- CSV: {summary.csv_path}",
        f"- Parsed rows: raw={summary.raw_rows}, native_bars={summary.native_bars}, replay_bars={summary.replay_bars}",
        f"- Post-close rows dropped: {summary.post_close_rows_dropped}",
        f"- Date range: {summary.date_start} to {summary.date_end}",
        f"- Warsh price range: {summary.price_min:.2%} to {summary.price_max:.2%}",
        f"- Warsh open/close: {summary.price_open:.2%} -> {summary.price_close:.2%}",
        f"- Rival columns kept for narration: {len(summary.rival_columns)}",
        "",
        "Scoring setup",
        "- Brain: existing score_raw_market() mechanical scorer, frozen at each as-of date.",
        "- Inputs: price-only chart export. last=bid=ask=price, volume=0, liquidity=0.",
        "- Rival columns are narration context only; they are not fed to the scorer.",
        f"- Entry rule: first Catalyst setup with rank_score >= {rank_threshold:.1f}.",
        f"- Exit cost assumption: official Kalshi fees plus {spread_assumption * 100:.1f} cents/contract round-trip spread haircut.",
        "",
        "Grades",
        format_outcome_accuracy(outcome_accuracy(decisions)),
        f"- Brier score across freeze points: {brier_score(decisions):.4f}",
        "",
        "P&L",
        format_trade("Model timing", model_trade),
        format_trade("No-model buy at open", buy_at_open),
    ]
    if buy_from_model_entry is not None:
        lines.append(format_trade("Buy/hold from model entry date", buy_from_model_entry))
    lines.extend([
        "",
        "120-Day Checkpoints",
    ])
    for checkpoint in checkpoints:
        state = checkpoint["market_state"]
        scorer = checkpoint["mechanical_scorer"]
        research = checkpoint["research"]
        lines.extend([
            f"- {checkpoint['as_of']} (research cutoff {checkpoint['research_cutoff']}): "
            f"price={state['price']:.2%}, rank={state['rival_context']['warsh_rank']}/"
            f"{state['rival_context']['field_count']}, "
            f"call={checkpoint['final_outcome_prediction']}, p={checkpoint['mechanical_call']['model_probability']:.2%}, "
            f"status={scorer['probe_status']}, target="
            f"{format_optional_cents(scorer['expected_future_price'])}",
            f"  Research: {research['note']}",
        ])
        llm_call = checkpoint.get("llm_research_call")
        if isinstance(llm_call, dict):
            lines.append(
                f"  LLM as-of call: {llm_call.get('final_outcome_prediction')} "
                f"p={format_optional_percent(llm_call.get('probability_yes'))}, "
                f"action={llm_call.get('trade_action')}; {llm_call.get('rationale') or llm_call.get('parse_error')}"
            )
    lines.extend([
        "",
        "Narrated timeline",
    ])
    for row in timeline:
        top = ", ".join(f"{item['name']} {item['price']:.0%}" for item in row["top_field"][:3])
        target = f"{row['target_cents']:.2f}c" if row["target_cents"] is not None else "n/a"
        lines.extend([
            f"- {row['as_of']}: Warsh {row['price_cents']:.2f}c, rank {row['warsh_rank']}/{row['field_count']} "
            f"(top: {top}). Call={row['call']} / {row['probe_status']}, "
            f"p={row['model_probability']:.2%}, target={target}",
            f"  Why: {row['why']}",
            f"  Next move: {row['next_move']['to_date']} at {row['next_move']['price_cents']:.2f}c "
            f"({row['next_move']['change_cents']:+.2f}c).",
        ])
    lines.extend([
        "",
        "Honest limitations",
        "- Historical order-book depth is unavailable in the CSV, so exitability is approximated by fees plus a spread haircut. This is optimistic versus live depth-gated execution.",
        "- The model probability is mostly a transform of market price plus the scorer's target tilt, so calibration partly measures the market predicting itself.",
        "- One resolved market is a worked example, not validation. Real evidence needs a batch of resolved replays.",
    ])
    return "\n".join(lines)


def format_trade(label: str, trade: TradeSimulation) -> str:
    if not trade.entered:
        return f"- {label}: no entry signal; P&L=$0.00"
    roi = "n/a" if trade.roi is None else f"{trade.roi:+.1%}"
    return (
        f"- {label}: {trade.contracts} contracts, entry {trade.entry_date} @ {trade.entry_price:.2%}, "
        f"exit {trade.exit_date} @ {trade.exit_price:.2%} ({trade.exit_reason}), "
        f"gross_cost=${trade.gross_cost:.2f}, fees=${trade.fees:.2f}, spread=${trade.spread_cost:.2f}, "
        f"net_pnl=${trade.net_pnl:+.2f}, ROI={roi}"
    )


def format_optional_cents(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.2f}c"


def format_optional_percent(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "n/a"


def format_outcome_accuracy(accuracy: dict[str, Any]) -> str:
    match = accuracy["match"]
    match_text = "n/a" if match is None else str(match)
    return (
        f"- Final outcome: {accuracy['resolution']}; final freeze lean: {accuracy['final_freeze_lean']}; "
        f"last directional lean: {accuracy['last_directional_lean']} on {accuracy['last_directional_date']}; "
        f"match={match_text}"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a resolved Kalshi chart CSV through the Early Marks scorer.")
    parser.add_argument("csv_path", type=Path, help="Kalshi chart Download CSV path")
    parser.add_argument("--target-column", default=TARGET_COLUMN)
    parser.add_argument("--cadence", choices=("daily", "native"), default="daily")
    parser.add_argument("--dry-run", action="store_true", help="Only parse and summarize the CSV")
    parser.add_argument("--assert-no-lookahead", action="store_true", default=True)
    parser.add_argument("--no-assert-no-lookahead", dest="assert_no_lookahead", action="store_false")
    parser.add_argument("--stake-dollars", type=float, default=100.0)
    parser.add_argument("--rank-threshold", type=float, default=48.0)
    parser.add_argument("--spread-cents", type=float, default=2.0)
    parser.add_argument("--maker-fees", action="store_true")
    parser.add_argument("--timeline-points", type=int, default=12)
    parser.add_argument("--checkpoint-days", type=int, default=120)
    parser.add_argument("--research-provider", choices=("none", "pack", "tavily"), default="none")
    parser.add_argument("--research-json", type=Path, help="Dated research source pack for --research-provider pack")
    parser.add_argument("--research-lookback-days", type=int, default=365)
    parser.add_argument("--research-max-results", type=int, default=8)
    parser.add_argument("--allow-undated-sources", action="store_true")
    parser.add_argument("--tavily-api-key", default=os.getenv("TAVILY_API_KEY", ""))
    parser.add_argument("--checkpoint-llm", choices=("none", "claude"), default="none")
    parser.add_argument("--json-out", type=Path, default=Path("data/runtime/backtests/backtest_replay_kxfedchairnom_kw.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bars, summary = load_event_csv(args.csv_path, target_column=args.target_column, cadence=args.cadence)
    print(
        "loader_ok "
        f"bars={summary.replay_bars} native={summary.native_bars} "
        f"range={summary.date_start}..{summary.date_end} "
        f"price={summary.price_min:.2%}..{summary.price_max:.2%} "
        f"target_column={summary.target_column!r}"
    )
    if args.dry_run:
        return

    decisions = build_decision_log(bars)
    if args.assert_no_lookahead:
        assert_no_lookahead(bars)

    spread_assumption = max(0.0, args.spread_cents / 100.0)
    model_trade = simulate_model_trade(
        decisions,
        bars,
        stake_dollars=args.stake_dollars,
        rank_threshold=args.rank_threshold,
        spread_assumption=spread_assumption,
        maker=args.maker_fees,
    )
    model_entry_index = (
        next((idx for idx, bar in enumerate(bars) if bar.as_of.isoformat() == model_trade.entry_date), None)
        if model_trade.entered
        else None
    )
    buy_from_model_entry = (
        simulate_buy_hold(
            bars,
            stake_dollars=args.stake_dollars,
            entry_index=model_entry_index,
            spread_assumption=spread_assumption,
            maker=args.maker_fees,
        )
        if model_entry_index is not None
        else None
    )
    buy_at_open = simulate_buy_hold(
        bars,
        stake_dollars=args.stake_dollars,
        entry_index=0,
        spread_assumption=spread_assumption,
        maker=args.maker_fees,
    )
    timeline = narrated_timeline(decisions, bars, args.timeline_points)
    research_pack = load_research_pack(args.research_json) if args.research_json else None
    checkpoints = build_checkpoints(
        decisions,
        bars,
        every_days=args.checkpoint_days,
        research_provider=args.research_provider,
        research_lookback_days=args.research_lookback_days,
        research_max_results=args.research_max_results,
        allow_undated_sources=args.allow_undated_sources,
        tavily_api_key=args.tavily_api_key,
        research_pack=research_pack,
        checkpoint_llm=args.checkpoint_llm,
    )
    payload = {
        "market": {
            "ticker": TARGET_TICKER,
            "title": MARKET_TITLE,
            "category": CATEGORY,
            "sub_title": TARGET_COLUMN,
            "open_date": OPEN_DATE.isoformat(),
            "close_date": CLOSE_DATE.isoformat(),
            "resolution": RESOLUTION,
        },
        "loader": asdict(summary),
        "policy": {
            "cadence": args.cadence,
            "stake_dollars": args.stake_dollars,
            "rank_threshold": args.rank_threshold,
            "spread_cents_round_trip": args.spread_cents,
            "fees": "maker" if args.maker_fees else "taker",
            "checkpoint_days": args.checkpoint_days,
            "research_provider": args.research_provider,
            "research_lookback_days": args.research_lookback_days,
            "allow_undated_sources": args.allow_undated_sources,
            "checkpoint_llm": args.checkpoint_llm,
        },
        "grades": {
            "outcome_accuracy": outcome_accuracy(decisions),
            "brier_score": round(brier_score(decisions), 6),
            "calibration_buckets": calibration_buckets(decisions),
        },
        "pnl": {
            "model_timing": asdict(model_trade),
            "buy_hold_from_model_entry": asdict(buy_from_model_entry) if buy_from_model_entry else None,
            "buy_at_open_hold_to_resolution": asdict(buy_at_open),
        },
        "checkpoints": checkpoints,
        "timeline": timeline,
        "decisions": decisions,
        "limitations": [
            "Historical order-book depth is unavailable in the CSV; exitability is approximated by fees plus a spread haircut.",
            "The scorer's probability is largely market-price-derived, so calibration partly measures the market itself.",
            "One market is an anecdote, not strategy validation.",
        ],
    }
    write_json(args.json_out, payload)
    print(build_report(
        summary=summary,
        decisions=decisions,
        model_trade=model_trade,
        buy_from_model_entry=buy_from_model_entry,
        buy_at_open=buy_at_open,
        timeline=timeline,
        checkpoints=checkpoints,
        spread_assumption=spread_assumption,
        rank_threshold=args.rank_threshold,
    ))
    print(f"\nwrote_json={args.json_out}")


if __name__ == "__main__":
    main()
