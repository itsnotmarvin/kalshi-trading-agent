"""
Read-only Early Marks sweep.

Fetches Kalshi once, scores every candidate, then picks one decent-looking
category/status/horizon setup per available category. This is for auditing the
outputs quickly without turning the HTML page into the whole lab.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.kalshi_adapter import KalshiAdapter
from config.settings import settings
from scripts.manual_probes.test_early_marks import (
    Candidate,
    PREOPEN_MARKET_STATUSES,
    ask_claude,
    candidate_category_label,
    candidate_scan_matches,
    fetch_raw_markets,
    score_raw_market,
)


SWEEP_OUTPUT = PROJECT_ROOT / "data" / "runtime" / "early_marks_sweep.json"
STATUS_CHOICES = ("open", "preopen", "all")
HORIZON_CHOICES = ("short", "medium", "long")


def _candidate_sort_key(candidate: Candidate) -> tuple[float, float, float]:
    roi = candidate.expected_repricing_roi if candidate.expected_repricing_roi is not None else -999.0
    placement = candidate.recommended_placement_dollars or 0.0
    return candidate.rank_score, roi, placement


def _candidate_does_not_look_bad(candidate: Candidate, min_score: float) -> bool:
    if candidate.probe_status == "Too directionless" or candidate.rank_score < min_score:
        return False
    if candidate.status in PREOPEN_MARKET_STATUSES or candidate.yes_ask is None:
        return True
    if candidate.expected_repricing_roi is None:
        return False
    if candidate.expected_repricing_roi < 0:
        return False
    if candidate.probe_status == "Watch only" and candidate.expected_repricing_roi < 8:
        return False
    return True


def choose_category_candidate(
    category: str,
    candidates: list[Candidate],
    min_score: float,
) -> dict[str, Any] | None:
    best: tuple[tuple[float, float, float], str, str, Candidate] | None = None
    for status in STATUS_CHOICES:
        for horizon in HORIZON_CHOICES:
            matches = [
                candidate for candidate in candidates
                if _candidate_does_not_look_bad(candidate, min_score)
                and candidate_scan_matches(
                    candidate,
                    category_filter=category,
                    status_filter=status,
                    horizon_filter=horizon,
                )
            ]
            if not matches:
                continue
            candidate = max(matches, key=_candidate_sort_key)
            key = _candidate_sort_key(candidate)
            if best is None or key > best[0]:
                best = (key, status, horizon, candidate)

    if best is None:
        fallback = [
            candidate for candidate in candidates
            if candidate_scan_matches(candidate, category_filter=category)
            and candidate.probe_status != "Too directionless"
        ]
        if not fallback:
            return None
        candidate = max(fallback, key=_candidate_sort_key)
        return {
            "category": category,
            "chosen_status": "all",
            "chosen_horizon": "all",
            "clean_candidate": False,
            "candidate": candidate,
        }

    _, status, horizon, candidate = best
    return {
        "category": category,
        "chosen_status": status,
        "chosen_horizon": horizon,
        "clean_candidate": True,
        "candidate": candidate,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--page-limit", type=int, default=200)
    parser.add_argument("--min-score", type=float, default=45.0)
    parser.add_argument("--max-categories", type=int, default=0, help="0 means all categories from the fetch")
    parser.add_argument("--min-placement-dollars", type=float, default=20.0)
    parser.add_argument("--max-placement-dollars", type=float, default=100.0)
    parser.add_argument("--no-claude", action="store_true")
    args = parser.parse_args()

    adapter = KalshiAdapter()
    connected = await adapter.connect()
    print(f"kalshi_connected={connected}")
    print("kalshi_environment=production")
    print(f"claude_model={settings.claude_scan_model}")
    print(f"claude_key_present={bool(settings.anthropic_api_key)}")

    raw_markets, diagnostics = await fetch_raw_markets(adapter, pages=args.pages, page_limit=args.page_limit)
    candidates = [
        candidate for candidate in (
            score_raw_market(
                raw,
                min_placement_dollars=args.min_placement_dollars,
                max_placement_dollars=args.max_placement_dollars,
            )
            for raw in raw_markets
        )
        if candidate is not None
    ]
    candidates.sort(key=_candidate_sort_key, reverse=True)
    categories = sorted({candidate_category_label(candidate) for candidate in candidates})
    if args.max_categories > 0:
        categories = categories[: args.max_categories]

    chosen_rows = [
        row for row in (
            choose_category_candidate(category, candidates, min_score=args.min_score)
            for category in categories
        )
        if row is not None
    ]
    chosen_candidates = [row["candidate"] for row in chosen_rows]

    claude_results: list[dict[str, Any]] = []
    claude_raw_text = ""
    claude_parse_error: str | None = None
    if not args.no_claude and settings.anthropic_api_key and chosen_candidates:
        claude_results, claude_raw_text, claude_parse_error = await ask_claude(
            chosen_candidates,
            max_results=len(chosen_candidates),
        )
    claude_by_ticker = {
        str(item.get("ticker")): item
        for item in claude_results
        if isinstance(item, dict) and item.get("ticker")
    }

    category_results: list[dict[str, Any]] = []
    for row in chosen_rows:
        candidate: Candidate = row["candidate"]
        research = claude_by_ticker.get(candidate.ticker, {})
        category_results.append({
            "category": row["category"],
            "chosen_status": row["chosen_status"],
            "chosen_horizon": row["chosen_horizon"],
            "clean_candidate": row["clean_candidate"],
            "ticker": candidate.ticker,
            "market": candidate.title,
            "placement": candidate.display_name,
            "market_status": candidate.status,
            "probe_status": candidate.probe_status,
            "rank_score": candidate.rank_score,
            "recommended_placement_dollars": candidate.recommended_placement_dollars,
            "yes_ask_cents": round(candidate.yes_ask * 100, 1) if candidate.yes_ask is not None else None,
            "expected_repricing_roi": candidate.expected_repricing_roi,
            "days_to_resolution": candidate.days_to_resolution,
            "claude_decision": research.get("decision"),
            "claude_why": research.get("why_i_believe_this"),
            "market_knows_check": research.get("market_knows_check"),
            "candidate": asdict(candidate),
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "market_count": len(raw_markets),
        "candidate_count": len(candidates),
        "available_categories": categories,
        "available_statuses": sorted({candidate.status for candidate in candidates if candidate.status}),
        "tested_statuses": list(STATUS_CHOICES),
        "tested_horizons": list(HORIZON_CHOICES),
        "diagnostics": diagnostics,
        "category_results": category_results,
        "claude_results": claude_results,
        "claude_parse_error": claude_parse_error,
        "claude_raw_text": claude_raw_text,
    }
    SWEEP_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_OUTPUT.write_text(json.dumps(output, indent=2))

    print(f"raw_markets={len(raw_markets)} candidates={len(candidates)} categories={len(categories)}")
    print(f"chosen_category_outputs={len(category_results)}")
    print(f"wrote={SWEEP_OUTPUT}")
    if claude_parse_error:
        print(f"claude_parse_error={claude_parse_error}")
    print("\nCategory outputs:")
    for row in category_results:
        price = "pre-open" if row["yes_ask_cents"] is None else f"{row['yes_ask_cents']}c"
        stake = row["recommended_placement_dollars"]
        clean = "clean" if row["clean_candidate"] else "fallback"
        print(
            f"- {row['category']} | {row['chosen_status']} / {row['chosen_horizon']} | "
            f"{row['placement']} | {row['probe_status']} | score={row['rank_score']} | "
            f"{clean} | stake=${stake:.2f} | price={price} | roi={row['expected_repricing_roi']}"
        )
        if row.get("claude_decision"):
            print(f"  decision={row['claude_decision']}")
        if row.get("claude_why"):
            print(f"  why={row['claude_why']}")


if __name__ == "__main__":
    asyncio.run(main())
