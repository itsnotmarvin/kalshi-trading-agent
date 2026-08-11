#!/usr/bin/env python3
"""
Render a Market Move Report markdown document from JSON.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def value(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict) or current.get(key) in (None, ""):
            return "not assessed"
        current = current[key]
    return current


def text(value_: Any) -> str:
    if value_ in (None, "", []):
        return "not assessed"
    if isinstance(value_, float):
        return f"{value_:.4f}"
    return str(value_)


def render_catalysts(catalysts: Any) -> str:
    if not isinstance(catalysts, list) or not catalysts:
        return "1. not assessed\n   Evidence: not assessed\n   Source: not assessed\n   Probability impact: not assessed"
    lines = []
    for index, catalyst in enumerate(catalysts, start=1):
        item = catalyst if isinstance(catalyst, dict) else {}
        lines.append(
            "\n".join(
                [
                    f"{index}. {text(item.get('name') or item.get('title'))}",
                    f"   Evidence: {text(item.get('evidence'))}",
                    f"   Source: {text(item.get('source'))}",
                    f"   Probability impact: {text(item.get('probability_impact'))}",
                ]
            )
        )
    return "\n".join(lines)


def render_report(payload: dict[str, Any]) -> str:
    fair = payload.get("fair_probability_check") if isinstance(payload.get("fair_probability_check"), dict) else {}
    plan = payload.get("hold_sell_watch_plan") if isinstance(payload.get("hold_sell_watch_plan"), dict) else {}
    quick = payload.get("quick_read") if isinstance(payload.get("quick_read"), dict) else {}
    return "\n".join(
        [
            "# Market Move Report",
            "",
            "## Quick Read",
            f"- Market: {text(quick.get('market') or payload.get('market'))}",
            f"- Side: {text(quick.get('side') or payload.get('side'))}",
            f"- Current price: {text(quick.get('current_price') or payload.get('current_price'))}",
            f"- Move type: {text(quick.get('move_type') or payload.get('move_type'))}",
            f"- Direction bias: {text(quick.get('direction_bias') or payload.get('direction_bias'))}",
            f"- Confidence: {text(quick.get('confidence') or payload.get('confidence'))}",
            "",
            "## What Changed",
            text(payload.get("what_changed")),
            "",
            "## Price History Signals",
            text(payload.get("price_history_signals")),
            "",
            "## Likely Catalysts",
            render_catalysts(payload.get("likely_catalysts")),
            "",
            "## Fair Probability Check",
            f"- Market implied probability: {text(fair.get('market_implied_probability'))}",
            f"- Pricing method: {text(fair.get('pricing_method'))}",
            f"- Executable price used for EV: {text(fair.get('executable_price_used_for_ev'))}",
            f"- Estimated fair probability: {text(fair.get('estimated_fair_probability'))}",
            f"- Edge: {text(fair.get('edge'))}",
            f"- Confidence: {text(fair.get('confidence'))}",
            "",
            "## Hold-Sell-Watch Plan",
            f"- Current stance: {text(plan.get('current_stance'))}",
            f"- Hold if: {text(plan.get('hold_if'))}",
            f"- Sell-trim if: {text(plan.get('sell_trim_if'))}",
            f"- Reassess if: {text(plan.get('reassess_if'))}",
            f"- Next catalyst-date: {text(plan.get('next_catalyst_date'))}",
            "",
            "## Risks",
            text(payload.get("risks")),
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a Market Move Report markdown file.")
    parser.add_argument("payload", help="Report payload JSON")
    parser.add_argument("--output", help="Output markdown path; defaults to stdout")
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.payload).read_text())
    markdown = render_report(payload)
    if args.output:
        Path(args.output).write_text(markdown)
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
