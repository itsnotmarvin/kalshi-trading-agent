"""
Trade Logger - Records every decision for review and calibration.

This is critical for learning whether Claude's probability estimates
are actually calibrated. After a few weeks, you can check:
- Did events Claude said were 70% likely happen ~70% of the time?
- Which categories does Claude estimate best/worst?
- What's the actual edge vs. estimated edge?
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings  # type: ignore


class TradeLogger:
    def __init__(self, log_path: str = ""):
        self.log_path = Path(log_path if log_path else settings.log_file)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_scan(self, markets_found: int, markets_researched: int):
        """Log a market scanning cycle."""
        self._write({
            "type": "scan",
            "timestamp": self._now(),
            "markets_found": markets_found,
            "markets_researched": markets_researched,
        })

    def log_analysis(
        self,
        market_id: str,
        question: str,
        market_price: float,
        estimated_prob: float,
        edge: float,
        direction: str,
        confidence: str,
        reasoning: str,
    ):
        """Log Claude's analysis of a market."""
        self._write({
            "type": "analysis",
            "timestamp": self._now(),
            "market_id": market_id,
            "question": question,
            "market_price": market_price,
            "estimated_probability": estimated_prob,
            "edge": edge,
            "direction": direction,
            "confidence": confidence,
            "reasoning": str(reasoning)[:500],  # type: ignore
        })

    def log_trade(
        self,
        market_id: str,
        direction: str,
        price: float,
        size_usd: float,
        estimated_prob: float,
        edge: float,
        approved: bool,
        rejection_reasons: list[str] = [],
        extra: dict | None = None,
    ):
        """Log a trade decision (whether approved or rejected)."""
        entry = {
            "type": "trade",
            "timestamp": self._now(),
            "market_id": market_id,
            "direction": direction,
            "price": price,
            "size_usd": size_usd,
            "estimated_probability": estimated_prob,
            "edge": edge,
            "approved": approved,
            "rejection_reasons": rejection_reasons or [],
        }
        if extra:
            entry.update(extra)
        self._write(entry)

    def log_order_attempt(
        self,
        client_order_id: str,
        market_id: str,
        side: str,
        price: float,
        quantity: float,
        status: str,
        order_id: str | None = None,
        error: str | None = None,
        extra: dict | None = None,
    ):
        """Log a live order attempt for idempotency and retry auditing."""
        entry = {
            "type": "order_attempt",
            "timestamp": self._now(),
            "client_order_id": client_order_id,
            "market_id": market_id,
            "side": side,
            "price": price,
            "quantity": quantity,
            "status": status,
            "order_id": order_id,
            "error": error,
        }
        if extra:
            entry.update(extra)
        self._write(entry)

    def log_resolution(
        self,
        market_id: str,
        question: str,
        outcome: str,  # "YES" or "NO"
        our_estimate: float,
        market_price_at_entry: float,
        pnl: float,
    ):
        """Log a market resolution for calibration tracking."""
        self._write({
            "type": "resolution",
            "timestamp": self._now(),
            "market_id": market_id,
            "question": question,
            "outcome": outcome,
            "our_estimate": our_estimate,
            "market_price_at_entry": market_price_at_entry,
            "pnl": pnl,
        })

    def log_error(self, context: str, error: str):
        """Log an error."""
        self._write({
            "type": "error",
            "timestamp": self._now(),
            "context": context,
            "error": str(error)[:500],  # type: ignore
        })

    def get_calibration_report(self) -> str:
        """
        Analyze logged predictions vs outcomes.
        This tells you if Claude is actually calibrated.
        """
        resolutions = []
        analyses = {}
        approved_trades = {}

        try:
            with open(self.log_path) as f:
                for line in f:
                    entry = json.loads(line)
                    if entry["type"] == "resolution":
                        resolutions.append(entry)
                    elif entry["type"] == "analysis":
                        analyses[entry["market_id"]] = entry
                    elif entry["type"] == "trade" and entry.get("approved"):
                        approved_trades[entry["market_id"]] = entry
        except FileNotFoundError:
            return "No data yet — run the agent first!"

        scored_resolutions = [
            res for res in resolutions
            if str(res.get("outcome", "")).upper() in {"YES", "NO"}
        ]
        if not scored_resolutions:
            return "No resolved markets yet — check back after some markets settle."

        # Bucket predictions by decile
        buckets = {i: {"predicted": [], "actual": []} for i in range(10)}
        total_pnl = 0
        total_staked = 0.0
        brier_terms = []
        family_stats = {}
        timing_stats = {}

        for res in scored_resolutions:
            prob = float(res["our_estimate"])
            outcome = 1.0 if str(res["outcome"]).upper() == "YES" else 0.0
            trade = approved_trades.get(res["market_id"], {})
            bucket = min(9, int(prob * 10))
            buckets[bucket]["predicted"].append(prob)
            buckets[bucket]["actual"].append(outcome)
            pnl = float(res.get("pnl", 0) or 0)
            stake = float(trade.get("size_usd", 0) or 0)
            total_pnl += pnl
            total_staked += stake
            brier_terms.append((prob - outcome) ** 2)

            family = self._market_family(
                trade.get("question") or res.get("question", ""),
                trade.get("category", ""),
            )
            self._accumulate_group(family_stats, family, prob, outcome, pnl, stake)

            timing = self._timing_bucket(trade.get("timestamp"), res.get("timestamp"))
            self._accumulate_group(timing_stats, timing, prob, outcome, pnl, stake)

        report = "📈 Calibration Report\n" + "=" * 40 + "\n"
        brier = sum(brier_terms) / len(brier_terms) if brier_terms else 0.0
        recorded_roi = total_pnl / total_staked if total_staked > 0 else 0.0
        report += f"Total resolved: {len(scored_resolutions)}\n"
        report += f"Total P&L: ${total_pnl:+.2f}\n\n"
        report += f"Brier score: {brier:.4f}\n"
        report += f"Recorded ROI: {recorded_roi:+.1%}\n"
        report += "Final fees and checkout totals are sourced from Kalshi, not re-estimated here.\n\n"
        report += f"{'Bucket':<12} {'Count':<8} {'Avg Pred':<10} {'Actual %':<10} {'Gap':<8}\n"
        report += "-" * 48 + "\n"

        for i, data in buckets.items():
            if data["predicted"]:
                avg_pred = sum(data["predicted"]) / len(data["predicted"])
                actual_pct = sum(data["actual"]) / len(data["actual"])
                gap = actual_pct - avg_pred
                report = report + (  # type: ignore
                    f"{i*10}-{(i+1)*10}%    "
                    f"{len(data['predicted']):<8} "
                    f"{avg_pred:<10.1%} "
                    f"{actual_pct:<10.1%} "
                    f"{gap:+.1%}\n"
                )

        report += "\nBy market family\n"
        report += f"{'Family':<14} {'Count':<7} {'Brier':<8} {'Net P&L':<10} {'ROI':<8}\n"
        report += "-" * 52 + "\n"
        for family, stats in sorted(family_stats.items()):
            report += self._format_group_row(family, stats)

        report += "\nBy timing\n"
        report += f"{'Timing':<14} {'Count':<7} {'Brier':<8} {'Net P&L':<10} {'ROI':<8}\n"
        report += "-" * 52 + "\n"
        for timing, stats in sorted(timing_stats.items()):
            report += self._format_group_row(timing, stats)

        return report

    def get_weather_validation_report(
        self,
        min_live_ready: int = 50,
        min_scale_ready: int = 100,
    ) -> str:
        """
        Report whether the Kalshi weather paper strategy has enough resolved
        evidence to consider live testing or larger size. This is advisory only.
        """
        summary = self.get_weather_validation_summary(min_live_ready, min_scale_ready)
        if summary["status"] == "no_data":
            return "No data yet — run weather_paper first."
        if summary["resolved_count"] == 0:
            return "No resolved Kalshi weather paper trades yet."

        report = "🌤️ Kalshi Weather Validation Report\n" + "=" * 44 + "\n"
        report += f"Resolved weather trades: {summary['resolved_count']}\n"
        report += f"Readiness: {summary['readiness']}\n"
        report += f"Need for live review: {summary['live_remaining']} more resolved trades\n"
        report += f"Need for scale review: {summary['scale_remaining']} more resolved trades\n\n"
        report += f"Brier score: {summary['brier']:.4f}\n"
        report += f"Recorded P&L: ${summary['pnl']:+.2f}\n"
        report += f"Recorded ROI: {summary['roi']:+.1%}\n"
        report += "Final fees and checkout totals are sourced from Kalshi, not re-estimated here.\n\n"

        report += "By weather family\n"
        report += f"{'Family':<14} {'Count':<7} {'Brier':<8} {'Net P&L':<10} {'ROI':<8}\n"
        report += "-" * 52 + "\n"
        for family, stats in sorted(summary["family_stats"].items()):
            report += self._format_group_row(family, stats)

        report += "\nBy timing\n"
        report += f"{'Timing':<14} {'Count':<7} {'Brier':<8} {'Net P&L':<10} {'ROI':<8}\n"
        report += "-" * 52 + "\n"
        for timing, stats in sorted(summary["timing_stats"].items()):
            report += self._format_group_row(timing, stats)

        report += "\nGate guidance\n"
        report += "- Stay paper-only until at least 50 resolved weather trades.\n"
        report += "- Do not materially increase size until at least 100 resolved weather trades.\n"
        report += "- Review family/timing buckets before changing strategy scope.\n"
        return report

    def get_weather_validation_summary(
        self,
        min_live_ready: int = 50,
        min_scale_ready: int = 100,
    ) -> dict:
        """Return structured Kalshi weather validation metrics for gating."""
        try:
            trades, resolutions = self._load_trade_and_resolution_entries()
        except FileNotFoundError:
            return {"status": "no_data", "resolved_count": 0, "readiness": "PAPER_ONLY"}

        weather_rows = []
        for market_id, res in resolutions.items():
            outcome_text = str(res.get("outcome", "")).upper()
            if outcome_text not in {"YES", "NO"}:
                continue

            trade = trades.get(market_id, {})
            category = str(trade.get("category") or "").lower()
            question = str(trade.get("question") or res.get("question") or "")
            model_source = str(trade.get("model_source") or "").lower()
            family = self._market_family(question, category)
            if (
                category != "weather"
                and "weather" not in model_source
                and not self._is_weather_family(family)
            ):
                continue

            prob = float(res.get("our_estimate", 0) or 0)
            actual = 1.0 if outcome_text == "YES" else 0.0
            pnl = float(res.get("pnl", 0) or 0)
            stake = float(trade.get("size_usd", 0) or 0)
            weather_rows.append({
                "prob": prob,
                "actual": actual,
                "pnl": pnl,
                "stake": stake,
                "family": family,
                "timing": self._timing_bucket(trade.get("timestamp"), res.get("timestamp")),
            })

        family_stats = {}
        timing_stats = {}
        for row in weather_rows:
            self._accumulate_group(
                family_stats,
                row["family"],
                row["prob"],
                row["actual"],
                row["pnl"],
                row["stake"],
            )
            self._accumulate_group(
                timing_stats,
                row["timing"],
                row["prob"],
                row["actual"],
                row["pnl"],
                row["stake"],
            )

        count = len(weather_rows)
        brier = sum((row["prob"] - row["actual"]) ** 2 for row in weather_rows) / count if count else 0.0
        recorded_pnl = sum(row["pnl"] for row in weather_rows)
        stake = sum(row["stake"] for row in weather_rows)
        roi = recorded_pnl / stake if stake > 0 else 0.0

        if count >= min_scale_ready:
            readiness = "SCALE_REVIEW"
        elif count >= min_live_ready:
            readiness = "LIVE_REVIEW"
        else:
            readiness = "PAPER_ONLY"

        return {
            "status": "ok",
            "resolved_count": count,
            "readiness": readiness,
            "live_remaining": max(0, min_live_ready - count),
            "scale_remaining": max(0, min_scale_ready - count),
            "brier": brier,
            "pnl": recorded_pnl,
            "roi": roi,
            "family_stats": family_stats,
            "timing_stats": timing_stats,
        }

    def _load_trade_and_resolution_entries(self) -> tuple[dict, dict]:
        trades = {}
        resolutions = {}
        with open(self.log_path) as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("type") == "trade" and entry.get("approved"):
                    trades[entry["market_id"]] = entry
                elif entry.get("type") == "resolution":
                    resolutions[entry["market_id"]] = entry
        return trades, resolutions

    def _accumulate_group(
        self,
        groups: dict,
        key: str,
        prob: float,
        outcome: float,
        pnl: float,
        stake: float,
    ):
        stats = groups.setdefault(
            key,
            {"count": 0, "brier_terms": [], "pnl": 0.0, "stake": 0.0},
        )
        stats["count"] += 1
        stats["brier_terms"].append((prob - outcome) ** 2)
        stats["pnl"] += pnl
        stats["stake"] += stake

    def _format_group_row(self, label: str, stats: dict) -> str:
        brier = sum(stats["brier_terms"]) / len(stats["brier_terms"])
        roi = stats["pnl"] / stats["stake"] if stats["stake"] > 0 else 0.0
        return (
            f"{label:<14} "
            f"{stats['count']:<7} "
            f"{brier:<8.4f} "
            f"${stats['pnl']:<+9.2f} "
            f"{roi:+.1%}\n"
        )

    def _market_family(self, question: str, category: str = "") -> str:
        text = f"{category} {question}".lower()
        if any(word in text for word in ("rain", "precip", "shower")):
            return "rain"
        if "snow" in text:
            return "snow"
        if "wind" in text:
            return "wind"
        if any(word in text for word in ("low temp", "low temperature", "below", "colder")):
            return "low_temp"
        if any(word in text for word in ("high temp", "high temperature", "above", "hotter")):
            return "high_temp"
        if "weather" in text or "temperature" in text or "degrees" in text:
            return "weather_other"
        return category.lower() if category else "other"

    def _is_weather_family(self, family: str) -> bool:
        return family in {"rain", "snow", "wind", "low_temp", "high_temp", "weather_other"}

    def _timing_bucket(self, entry_ts: str | None, resolution_ts: str | None) -> str:
        entry = self._parse_timestamp(entry_ts)
        resolution = self._parse_timestamp(resolution_ts)
        if not entry or not resolution:
            return "unknown"

        hours = (resolution - entry).total_seconds() / 3600
        if hours <= 24:
            return "same_day"
        if hours <= 48:
            return "tomorrow"
        return "over_24h"

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _write(self, entry: dict):
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
