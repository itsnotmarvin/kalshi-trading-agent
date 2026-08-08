"""
Risk Manager - The safety layer between Claude's brain and your wallet.

This is the most important file in the entire project. Every trade
recommendation from Claude passes through here before execution.
If ANY check fails, the trade is blocked.
"""
import json
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from pathlib import Path

from adapters.base import Position, Market
from config.settings import settings


CONFIDENCE_EDGE_MULTIPLIERS = {
    "LOW": 0.0,
    "MEDIUM": 0.50,
    "HIGH": 0.75,
    "VERY_HIGH": 1.0,
}


@dataclass
class DailyStats:
    """Track daily trading statistics for circuit breakers."""
    date: str = ""
    trades_placed: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    total_pnl: float = 0.0
    peak_portfolio: float = 0.0
    current_portfolio: float = 0.0
    consecutive_losses: int = 0
    is_halted: bool = False
    halt_reason: str = ""


@dataclass
class TradeProposal:
    """A proposed trade from the Claude agent."""
    market_id: str
    market_question: str
    category: str
    direction: str         # BUY_YES, BUY_NO, HOLD
    estimated_prob: float
    market_price: float
    edge: float
    confidence: str        # LOW, MEDIUM, HIGH, VERY_HIGH
    position_size_usd: float
    reasoning: str
    target_entry_price: float = 0.0
    risk_factors: list[str] = field(default_factory=list)
    r_score: float = 0.0
    entry_direction: str | None = None
    is_deep_research: bool = False
    execution_metrics: dict = field(default_factory=dict)
    client_order_id: str | None = None
    post_only: bool = False


class RiskManager:
    """
    Enforces all risk rules. This is your guardrail against ruin.

    Philosophy: It's ALWAYS better to miss a trade than to lose money
    on a bad one. These checks are intentionally conservative.
    """

    def __init__(self):
        self.daily_stats = DailyStats(date=self._today())
        self.max_daily_trades = 20
        self.max_consecutive_losses = 5
        self.max_drawdown_pct = 0.15  # 15% max drawdown from peak

        # Category Discipline System
        self.category_stats_file = Path("data/category_stats.json")
        self.category_stats_file.parent.mkdir(parents=True, exist_ok=True)
        self.category_stats = self._load_category_stats()
        self.paper_positions_file = Path("data/paper_positions.json")

    def _load_category_stats(self) -> dict:
        # Baseline from verified fills history (May 27, 2026) — only used if no file exists yet
        default_stats = {
            "Weather": {"trades": 12, "won": 4, "lost": 8, "pnl": -20.49},
            "Sports": {"trades": 7, "won": 2, "lost": 5, "pnl": -78.34},
            "Tech/AI": {"trades": 4, "won": 2, "lost": 2, "pnl": 199.65},
            "Politics": {"trades": 0, "won": 0, "lost": 0, "pnl": 0.0},
            "Crypto": {"trades": 0, "won": 0, "lost": 0, "pnl": 0.0},
            "Economics": {"trades": 0, "won": 0, "lost": 0, "pnl": 0.0}
        }
        if self.category_stats_file.exists():
            try:
                with open(self.category_stats_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        self.category_stats = default_stats
        self._save_category_stats()
        return default_stats

    def _save_category_stats(self):
        try:
            with open(self.category_stats_file, "w") as f:
                json.dump(self.category_stats, f, indent=2)
        except Exception:
            pass

    def load_paper_positions(self) -> list[Position]:
        """Load simulated positions for paper trading mode."""
        from adapters.base import Position, Side
        if not self.paper_positions_file.exists():
            return []
        try:
            with open(self.paper_positions_file, "r") as f:
                data = json.load(f)
                positions = []
                for p in data:
                    positions.append(Position(
                        market_id=p["market_id"],
                        market_question=p.get("market_question", ""),
                        side=Side(p["side"]),
                        quantity=p["quantity"],
                        avg_price=p["avg_price"],
                        current_price=p.get("current_price", p["avg_price"]),
                        unrealized_pnl=p.get("unrealized_pnl", 0.0),
                        platform=p.get("platform", "kalshi"),
                        category=p.get("category", ""),
                        entry_prob=p.get("entry_prob", 0.0),
                    ))
                return positions
        except Exception as e:
            print(f"⚠️  Failed to load paper positions: {e}")
            return []

    def save_paper_positions(self, positions: list[Position]):
        """Save paper positions to disk."""
        try:
            data = []
            for p in positions:
                data.append({
                    "market_id": p.market_id,
                    "market_question": p.market_question,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "current_price": p.current_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "platform": p.platform,
                    "category": p.category,
                    "entry_prob": p.entry_prob,
                })
            with open(self.paper_positions_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️  Failed to save paper positions: {e}")

    def record_paper_trade(self, proposal: TradeProposal):
        """Add a paper trade to the persistent storage."""
        from adapters.base import Position, Side
        positions = self.load_paper_positions()
        
        # Check if already exists (should be caught by check_trade, but double check)
        for p in positions:
            if p.market_id == proposal.market_id:
                return

        executable_direction = proposal.direction
        if executable_direction not in ("BUY_YES", "BUY_NO"):
            executable_direction = proposal.entry_direction or ""
        if executable_direction not in ("BUY_YES", "BUY_NO"):
            return

        side = Side.YES if executable_direction == "BUY_YES" else Side.NO

        # proposal.market_price is the YES price. The cost basis of a NO
        # contract is (1 - yes_price) — store the price of the side we hold
        # so settlement / take-profit math is correct for both sides.
        entry_price = self.get_side_cost(proposal)
        if entry_price <= 0 or entry_price >= 1:
            return  # Degenerate price — refuse to record the position.
        contracts = float(
            proposal.execution_metrics.get("contracts")
            or proposal.execution_metrics.get("requested_contracts")
            or int(proposal.position_size_usd / entry_price)
        )
        if contracts < 1:
            return
        new_pos = Position(
            market_id=proposal.market_id,
            market_question=proposal.market_question,
            side=side,
            quantity=contracts,
            avg_price=entry_price,
            current_price=entry_price,
            unrealized_pnl=0.0,
            platform=settings.platform,
            category=proposal.category,
            entry_prob=proposal.estimated_prob,
        )
        positions.append(new_pos)
        self.save_paper_positions(positions)

    def record_category_result(self, category: str, pnl: float):
        """Update historical win rate for a category after a trade closes."""
        if not category:
            return
            
        if category not in self.category_stats:
            self.category_stats[category] = {"trades": 0, "won": 0, "lost": 0, "pnl": 0.0}
            
        stats = self.category_stats[category]
        stats["trades"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["won"] += 1
        else:
            stats["lost"] += 1
            
        self._save_category_stats()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def reset_daily_if_needed(self):
        """Reset daily stats at midnight UTC."""
        today = self._today()
        if self.daily_stats.date != today:
            # Save yesterday's stats for review
            self._save_daily_stats()
            self.daily_stats = DailyStats(date=today)

    def check_trade(
        self,
        proposal: TradeProposal,
        positions: list[Position],
        balance: float,
        portfolio_value: float,
        paper_mode: bool = False,
    ) -> tuple[bool, list[str]]:
        """
        Run ALL risk checks on a proposed trade.
        Returns (approved: bool, reasons: list[str])

        A trade must pass EVERY check to be approved.
        """
        self.reset_daily_if_needed()
        failures = []

        # === CHECK 1: Is trading halted? ===
        if self.daily_stats.is_halted:
            failures.append(f"🚨 TRADING HALTED: {self.daily_stats.halt_reason}")
            return False, failures

        # === CHECK 2: Direction must not be HOLD ===
        if proposal.direction == "HOLD":
            failures.append("Agent recommends HOLD — no trade")
            return False, failures

        # === CHECK 4: Minimum confidence ===
        if proposal.confidence == "LOW":
            failures.append("Confidence too low (LOW) — requires at least MEDIUM")

        # This is the app's unique signal: independent probability versus the
        # executable Kalshi price. Kalshi handles fee/payout/order-preview math.
        confidence_adjusted_gap = self.calculate_confidence_adjusted_probability_gap(proposal)
        required_edge = 0.12 if proposal.category.lower() == "weather" else settings.min_edge_threshold
        proposal.execution_metrics["required_probability_gap"] = required_edge
        if confidence_adjusted_gap < required_edge:
            failures.append(
                f"Confidence-adjusted probability gap {confidence_adjusted_gap:.1%} "
                f"is below the required {required_edge:.1%}."
            )

        # === CHECK 5: Position size limits ===
        if not paper_mode and proposal.position_size_usd > settings.max_single_trade:
            failures.append(
                f"Position ${proposal.position_size_usd:.2f} exceeds max "
                f"${settings.max_single_trade:.2f}"
            )

        if proposal.position_size_usd <= 0:
            failures.append("Position size must be positive")

        # === CHECK 6: Portfolio exposure limit ===
        total_exposure = sum(p.quantity * p.avg_price for p in positions)
        if not paper_mode and total_exposure + proposal.position_size_usd > settings.max_portfolio_value:
            failures.append(
                f"Would exceed max portfolio exposure "
                f"(${total_exposure + proposal.position_size_usd:.2f} > "
                f"${settings.max_portfolio_value:.2f})"
            )

        # === CHECK 7: Sufficient balance ===
        if not paper_mode and proposal.position_size_usd > balance * 0.95:  # Keep 5% buffer
            failures.append(
                f"Insufficient balance: need ${proposal.position_size_usd:.2f}, "
                f"have ${balance:.2f}"
            )

        # === CHECK 8: Daily loss circuit breaker ===
        if self.daily_stats.total_pnl < -settings.max_daily_loss:
            self.daily_stats.is_halted = True
            self.daily_stats.halt_reason = (
                f"Daily loss ${abs(self.daily_stats.total_pnl):.2f} "
                f"exceeds limit ${settings.max_daily_loss:.2f}"
            )
            failures.append(f"🚨 CIRCUIT BREAKER: {self.daily_stats.halt_reason}")

        # === CHECK 9: Daily trade count ===
        if self.daily_stats.trades_placed >= self.max_daily_trades:
            failures.append(
                f"Daily trade limit reached ({self.max_daily_trades})"
            )

        # === CHECK 10: Consecutive losses ===
        if self.daily_stats.consecutive_losses >= self.max_consecutive_losses:
            self.daily_stats.is_halted = True
            self.daily_stats.halt_reason = (
                f"{self.max_consecutive_losses} consecutive losses"
            )
            failures.append(f"🚨 CIRCUIT BREAKER: {self.daily_stats.halt_reason}")

        # === CHECK 11: Duplicate position ===
        for pos in positions:
            if pos.market_id == proposal.market_id:
                failures.append(
                    f"Already have position in {proposal.market_id}"
                )
                break

        # === CHECK 12: Category Discipline & Concentration ===
        # Part A: Category Discipline (Hard-block bad categories)
        if proposal.category in self.category_stats:
            stats = self.category_stats[proposal.category]
            if stats["trades"] >= 5:
                win_rate = stats["won"] / stats["trades"]
                if win_rate < 0.30:  # <30% WR is totally unacceptable
                    failures.append(
                        f"Category Discipline: '{proposal.category}' is historically unprofitable "
                        f"({win_rate:.0%} WR over {stats['trades']} trades). "
                        f"Blocking to protect capital."
                    )
        
        # Part B: Max Positions
        if len(positions) >= settings.max_positions:
            failures.append(
                f"Max positions reached ({settings.max_positions})"
            )

        # === CHECK 13: Probability bounds ===
        if not (0.03 < proposal.estimated_prob < 0.97):
            failures.append(
                f"Probability {proposal.estimated_prob:.2f} too extreme — "
                f"likely overconfident"
            )

        # === CHECK 13.5: Optional purchased-side payout likelihood floor ===
        # Defaults to 0. This is an optional policy floor, separate from the
        # model-versus-market probability-gap check above.
        min_likelihood = getattr(settings, "min_payout_likelihood", 0.0)
        side_prob = self.get_side_probability(proposal)
        if min_likelihood > 0 and side_prob < min_likelihood:
            failures.append(
                f"Payout likelihood {side_prob:.0%} is below the "
                f"minimum floor of {min_likelihood:.0%}. Skipping — odds too unfavorable."
            )

        # === CLAUDE'S BOT CHECK 18: Minimum Position Size ===
        min_pos = getattr(settings, "min_position_size", 3.0)
        if 0 < proposal.position_size_usd < min_pos:
            failures.append(
                f"Position ${proposal.position_size_usd:.2f} is below the configured "
                f"minimum-position policy of ${min_pos:.2f}."
            )

        # === CLAUDE'S BOT CHECK 19: Blocked Categories ===
        blocked = getattr(settings, "blocked_categories", [])
        if proposal.category in blocked:
            failures.append(
                f"🚫 BLOCKED CATEGORY: '{proposal.category}' is permanently blocked "
                f"due to historically unprofitable performance."
            )

        # === CHECK 14: Spread check ===
        spread = proposal.execution_metrics.get("side_spread")
        if spread is not None:
            max_spread = getattr(settings, "max_orderbook_spread", 0.08)
            if spread > max_spread:
                failures.append(
                    f"Orderbook spread {spread:.1%} exceeds max {max_spread:.1%}"
                )

            depth_usd = proposal.execution_metrics.get("side_depth_usd", 0.0)
            min_depth = getattr(settings, "min_orderbook_depth_usd", 25.0)
            if depth_usd < min_depth:
                failures.append(
                    f"Orderbook depth ${depth_usd:.2f} below minimum ${min_depth:.2f}"
                )
            if proposal.execution_metrics.get("top_level_covers_quantity") is False:
                failures.append("Proposed quantity crosses multiple price levels; live multi-level execution is disabled")
        elif proposal.direction in ("BUY_YES", "BUY_NO"):
            failures.append("Missing orderbook metrics for executable trade")

        # === CHECK 15: Drawdown check ===
        if self.daily_stats.peak_portfolio > 0:
            drawdown = (
                (self.daily_stats.peak_portfolio - portfolio_value)
                / self.daily_stats.peak_portfolio
            )
            if drawdown > self.max_drawdown_pct:
                self.daily_stats.is_halted = True
                self.daily_stats.halt_reason = (
                    f"Drawdown {drawdown:.1%} exceeds max {self.max_drawdown_pct:.1%}"
                )
                failures.append(f"🚨 CIRCUIT BREAKER: {self.daily_stats.halt_reason}")

        # === CHECK 21: Block multi-leg / parlay markets ===
        ticker_upper = proposal.market_id.upper()
        question_lower = proposal.market_question.lower()
        is_parlay = (
            ticker_upper.startswith("KXMVE")
            or "parlay" in question_lower
            or "multi-game" in question_lower
            or "cross-category" in question_lower
            or "combination bet" in question_lower
        )
        if is_parlay:
            failures.append(
                f"🚫 PARLAY BLOCK: Market '{proposal.market_id}' is a high-risk multi-leg/parlay bet. "
                f"These are manually blocked to protect the portfolio from high fee/variance drag."
            )

        # === CHECK 22: Category exposure cap ===
        if not paper_mode:
            category_exposure = sum(
                p.quantity * p.avg_price
                for p in positions
                if getattr(p, "category", "").lower() == proposal.category.lower()
            )
            max_cat_usd = portfolio_value * settings.max_category_exposure_pct
            if max_cat_usd > 0 and (category_exposure + proposal.position_size_usd) > max_cat_usd:
                failures.append(
                    f"Category cap: '{proposal.category}' exposure "
                    f"(${category_exposure + proposal.position_size_usd:.2f}) "
                    f"would exceed {settings.max_category_exposure_pct:.0%} limit "
                    f"(${max_cat_usd:.2f})"
                )

        approved = len(failures) == 0
        return approved, failures

    def build_trade_risk_snapshot(
        self,
        proposal: TradeProposal,
        positions: list[Position],
        balance: float,
        portfolio_value: float,
        approved: bool,
        reasons: list[str],
        paper_mode: bool = False,
        executed: bool = False,
        execution_blockers: list[str] | None = None,
    ) -> dict:
        """
        Build the dashboard-facing risk snapshot from the same inputs used by
        check_trade(). This keeps the UI tied to the trade's actual size,
        side price, orderbook metrics, and circuit-breaker state.
        """
        self.reset_daily_if_needed()
        execution_blockers = execution_blockers or []
        all_reasons = list(reasons or []) + execution_blockers

        direction = proposal.direction
        executable_direction = direction
        if executable_direction not in ("BUY_YES", "BUY_NO"):
            executable_direction = proposal.entry_direction or executable_direction
        executable = executable_direction in ("BUY_YES", "BUY_NO")

        if direction == "HOLD":
            status = "hold"
            label = "No Trade"
        elif direction == "WAIT_FOR_ENTRY" and approved and not execution_blockers:
            status = "watching"
            label = "Watching Entry"
        elif approved and not execution_blockers and executable:
            status = "safe" if executed else "approved"
            label = "Paper Safe" if paper_mode and executed else "Safe"
            if not executed:
                label = "Risk Approved"
        else:
            status = "blocked"
            label = "Blocked"

        size = float(proposal.position_size_usd or 0.0)
        side_cost = self.get_side_cost(proposal) if executable else 0.0
        contracts = proposal.execution_metrics.get("contracts")
        if contracts is None and executable and side_cost > 0:
            contracts = size / side_cost

        total_exposure = sum(p.quantity * p.avg_price for p in positions)
        category_exposure = sum(
            p.quantity * p.avg_price
            for p in positions
            if getattr(p, "category", "").lower() == proposal.category.lower()
        )
        category_cap = portfolio_value * settings.max_category_exposure_pct

        stats = self.category_stats.get(proposal.category, {})
        category_trades = int(stats.get("trades", 0)) if isinstance(stats, dict) else 0
        category_wins = int(stats.get("won", 0)) if isinstance(stats, dict) else 0
        category_win_rate = (category_wins / category_trades) if category_trades else None

        max_loss = settings.max_daily_loss
        daily_loss_used = max(0.0, -self.daily_stats.total_pnl)
        balance_limit = balance if paper_mode else balance * 0.95
        balance_failed = any("Insufficient" in reason for reason in all_reasons)
        daily_loss_failed = any(
            "Daily loss" in reason or "CIRCUIT BREAKER" in reason
            for reason in all_reasons
        )
        trade_count_failed = any("Daily trade limit" in reason for reason in all_reasons)
        loss_streak_failed = any("consecutive losses" in reason.lower() for reason in all_reasons)

        checks = [
            {
                "name": "Balance after",
                "value": balance - size if executable else balance,
                "limit": 0.0,
                "status": "fail" if balance_failed or (executable and size > balance_limit) else "pass",
                "unit": "currency",
                "enforced": True,
            },
            {
                "name": "Position size",
                "value": size,
                "limit": settings.max_single_trade,
                "status": "skipped" if paper_mode else ("pass" if size <= settings.max_single_trade else "fail"),
                "unit": "currency",
                "enforced": not paper_mode,
            },
            {
                "name": "Daily loss",
                "value": daily_loss_used,
                "limit": max_loss,
                "status": "fail" if daily_loss_failed or self.daily_stats.total_pnl < -max_loss else "pass",
                "unit": "currency",
                "enforced": True,
            },
            {
                "name": "Trades today",
                "value": self.daily_stats.trades_placed,
                "limit": self.max_daily_trades,
                "status": "fail" if trade_count_failed or self.daily_stats.trades_placed > self.max_daily_trades else "pass",
                "unit": "count",
                "enforced": True,
            },
            {
                "name": "Loss streak",
                "value": self.daily_stats.consecutive_losses,
                "limit": self.max_consecutive_losses,
                "status": "fail" if loss_streak_failed or self.daily_stats.consecutive_losses >= self.max_consecutive_losses else "pass",
                "unit": "count",
                "enforced": True,
            },
        ]

        probability_gap = proposal.execution_metrics.get("confidence_adjusted_probability_gap")
        if probability_gap is not None:
            checks.append({
                "name": "Probability gap",
                "value": probability_gap,
                "limit": proposal.execution_metrics.get("required_probability_gap", settings.min_edge_threshold),
                "status": "pass" if probability_gap >= proposal.execution_metrics.get("required_probability_gap", settings.min_edge_threshold) else "fail",
                "unit": "percent",
                "enforced": True,
            })

        spread = proposal.execution_metrics.get("side_spread")
        if spread is not None:
            max_spread = getattr(settings, "max_orderbook_spread", 0.08)
            checks.append({
                "name": "Spread",
                "value": spread,
                "limit": max_spread,
                "status": "pass" if spread <= max_spread else "fail",
                "unit": "percent",
                "enforced": True,
            })

        depth_usd = proposal.execution_metrics.get("side_depth_usd")
        if depth_usd is not None:
            min_depth = getattr(settings, "min_orderbook_depth_usd", 25.0)
            checks.append({
                "name": "Depth",
                "value": depth_usd,
                "limit": min_depth,
                "status": "pass" if depth_usd >= min_depth else "fail",
                "unit": "currency",
                "enforced": True,
            })

        return {
            "status": status,
            "label": label,
            "approved": approved and not execution_blockers,
            "risk_approved": approved,
            "executed": executed,
            "paper_mode": paper_mode,
            "direction": executable_direction,
            "size_usd": size,
            "side_cost": side_cost,
            "contracts": float(contracts or 0.0),
            "reported_probability_gap": proposal.edge,
            "confidence_adjusted_probability_gap": probability_gap,
            "spread": spread,
            "depth_usd": depth_usd,
            "balance_before": balance,
            "balance_after": balance - size if executable else balance,
            "portfolio_value": portfolio_value,
            "exposure_before": total_exposure,
            "exposure_after": total_exposure + size if executable else total_exposure,
            "max_portfolio_value": settings.max_portfolio_value,
            "category": proposal.category,
            "category_exposure_before": category_exposure,
            "category_exposure_after": category_exposure + size if executable else category_exposure,
            "category_exposure_limit": category_cap,
            "category_win_rate": category_win_rate,
            "category_trades": category_trades,
            "daily_loss_used": daily_loss_used,
            "max_daily_loss": max_loss,
            "trades_today": self.daily_stats.trades_placed,
            "max_daily_trades": self.max_daily_trades,
            "consecutive_losses": self.daily_stats.consecutive_losses,
            "max_consecutive_losses": self.max_consecutive_losses,
            "failures": all_reasons,
            "checks": checks,
        }

    def evaluate_exits(self, positions: list[Position]) -> list[tuple[Position, str]]:
        """
        Evaluate all open positions for take-profit thresholds.
        Returns a list of tuples: (Position to sell, reason string).
        """
        exits = []
        for p in positions:
            if p.avg_price <= 0 or p.quantity < 1:  # Ignore dust
                continue
                
            roi = (p.current_price - p.avg_price) / p.avg_price
            if roi >= settings.take_profit_threshold:
                # Extra safety: If ROI is absurdly high (e.g. 1000%+), 
                # cross-check that it's not a default price error.
                if roi > 10.0 and p.current_price == 0.5:
                    continue

                reason = f"Take profit triggered! Value surged +{roi:.1%} (Bought @ ${p.avg_price:.2f}, Selling @ ${p.current_price:.2f})"
                exits.append((p, reason))

        return exits

    def realized_pnl_on_settlement(self, position: Position, result: str) -> tuple[float, bool]:
        """
        Compute realized P&L for a position once its market has RESOLVED.

        Every contract of the winning side pays out $1; the losing side pays $0.
        Returns (pnl_usd, won).
        """
        won = position.side.value == (result or "").lower()
        payout = position.quantity * (1.0 if won else 0.0)
        cost_basis = position.quantity * position.avg_price
        return round(payout - cost_basis, 4), won

    def calculate_r_score(
        self,
        estimated_prob: float,
        market_price: float,
        direction: str
    ) -> float:
        """
        Calculate a variance-normalized edge display metric.
        R-Score = (p - y) / sqrt(p * (1 - p))

        This is not a statistical Z-score because it has no sample-size term,
        and it is not used as an execution eligibility gate.
        """
        if direction not in {"BUY_YES", "BUY_NO"}:
            return 0.0

        p = estimated_prob if direction == "BUY_YES" else (1.0 - estimated_prob)
        y = market_price if direction == "BUY_YES" else (1.0 - market_price)
        
        # Variance of a Bernoulli distribution
        variance = p * (1.0 - p)
        if variance <= 0:
            return 0.0
            
        return (p - y) / math.sqrt(variance)

    def get_confidence_multiplier(self, confidence: str) -> float:
        """Shrink raw edge toward zero based on model confidence."""
        return CONFIDENCE_EDGE_MULTIPLIERS.get((confidence or "").upper(), 0.0)

    def get_side_probability(self, proposal: TradeProposal) -> float:
        """Return the model probability for the side being purchased."""
        direction = proposal.direction
        if direction not in ("BUY_YES", "BUY_NO"):
            direction = proposal.entry_direction or ""
        if direction == "BUY_NO":
            return max(0.0, min(1.0, 1.0 - proposal.estimated_prob))
        if direction == "BUY_YES":
            return max(0.0, min(1.0, proposal.estimated_prob))
        return 0.0

    def calculate_probability_gap(self, proposal: TradeProposal) -> float:
        """Return model side probability minus market side probability."""
        if proposal.direction not in ("BUY_YES", "BUY_NO") and proposal.entry_direction not in ("BUY_YES", "BUY_NO"):
            return 0.0
        side_prob = self.get_side_probability(proposal)
        market_side_prob = self.get_market_side_probability(proposal)
        gap = side_prob - market_side_prob
        proposal.execution_metrics.update({
            "side_probability": side_prob,
            "market_side_probability": market_side_prob,
            "probability_gap": gap,
        })
        return gap

    def calculate_confidence_adjusted_probability_gap(self, proposal: TradeProposal) -> float:
        """
        Apply the configured confidence haircut to the independent probability
        gap. Kalshi remains responsible for trade calculations.
        """
        raw_gap = self.calculate_probability_gap(proposal)
        multiplier = self.get_confidence_multiplier(proposal.confidence)
        adjusted = raw_gap * multiplier
        proposal.execution_metrics.update({
            "confidence_multiplier": multiplier,
            "confidence_adjusted_probability_gap": adjusted,
        })
        return adjusted

    def get_market_side_probability(self, proposal: TradeProposal) -> float:
        """Return the market midpoint probability for the side being considered."""
        metric_probability = proposal.execution_metrics.get("market_side_probability")
        if metric_probability is not None:
            try:
                return max(0.0, min(1.0, float(metric_probability)))
            except (TypeError, ValueError):
                pass
        direction = proposal.direction if proposal.direction in ("BUY_YES", "BUY_NO") else proposal.entry_direction
        if direction == "BUY_NO":
            return max(0.0, min(1.0, 1.0 - proposal.market_price))
        return max(0.0, min(1.0, proposal.market_price))

    def get_side_cost(self, proposal: TradeProposal) -> float:
        """Return the per-contract cost for the side being bought."""
        metric_cost = proposal.execution_metrics.get("side_cost")
        if metric_cost is not None:
            try:
                return max(0.0, min(1.0, float(metric_cost)))
            except (TypeError, ValueError):
                pass
        direction = proposal.direction
        if direction not in ("BUY_YES", "BUY_NO"):
            direction = proposal.entry_direction or ""
        if direction == "BUY_NO":
            return max(0.0, min(1.0, 1.0 - proposal.market_price))
        return max(0.0, min(1.0, proposal.market_price))

    def attach_orderbook_metrics(
        self,
        proposal: TradeProposal,
        orderbook: dict,
    ) -> dict:
        """
        Normalize Kalshi-style YES/NO bid books into side-specific execution
        metrics. A bid on one side implies the ask on the opposite side.
        """
        yes_levels = orderbook.get("yes") or orderbook.get("bids") or []
        no_levels = orderbook.get("no") or orderbook.get("asks") or []

        yes_bid, yes_bid_qty = self._best_level(yes_levels)
        no_bid, no_bid_qty = self._best_level(no_levels)
        yes_ask = (1.0 - no_bid) if no_bid is not None else None
        no_ask = (1.0 - yes_bid) if yes_bid is not None else None
        market_yes_probability = (
            (yes_bid + yes_ask) / 2.0
            if yes_bid is not None and yes_ask is not None
            else None
        )

        yes_spread = (
            max(0.0, yes_ask - yes_bid)
            if yes_bid is not None and yes_ask is not None
            else None
        )
        no_spread = (
            max(0.0, no_ask - no_bid)
            if no_bid is not None and no_ask is not None
            else None
        )

        if proposal.direction == "BUY_NO":
            top_of_book_cost = no_ask if no_ask is not None else self.get_side_cost(proposal)
            side_spread = no_spread
            side_depth_contracts = yes_bid_qty
            side_depth_usd = side_depth_contracts * top_of_book_cost
            market_side_probability = 1.0 - market_yes_probability if market_yes_probability is not None else None
        else:
            top_of_book_cost = yes_ask if yes_ask is not None else self.get_side_cost(proposal)
            side_spread = yes_spread
            side_depth_contracts = no_bid_qty
            side_depth_usd = side_depth_contracts * top_of_book_cost
            market_side_probability = market_yes_probability

        requested_contracts = max(1, int(proposal.position_size_usd / top_of_book_cost)) if top_of_book_cost > 0 and proposal.position_size_usd > 0 else 1
        side_cost = top_of_book_cost
        estimated_total_cost = requested_contracts * side_cost

        metrics = {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_spread": yes_spread,
            "no_spread": no_spread,
            "top_of_book_side_cost": top_of_book_cost,
            "side_cost": side_cost,
            "side_spread": side_spread,
            "side_depth_contracts": side_depth_contracts,
            "side_depth_usd": side_depth_usd,
            "requested_contracts": requested_contracts,
            "top_level_covers_quantity": requested_contracts <= side_depth_contracts,
            "estimated_total_cost": estimated_total_cost,
            "market_side_probability": market_side_probability,
        }
        proposal.execution_metrics.update(metrics)
        return metrics

    def _best_level(self, levels: list) -> tuple[float | None, float]:
        best_price = None
        best_qty = 0.0
        for level in levels:
            if isinstance(level, dict):
                raw_price = level.get("price") or level.get("price_dollars")
                raw_qty = level.get("quantity") or level.get("count") or level.get("count_fp") or 0
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                raw_price, raw_qty = level[0], level[1]
            else:
                continue

            try:
                price = float(raw_price)
                qty = float(raw_qty)
            except (TypeError, ValueError):
                continue
            if price > 1:
                price = price / 100.0
            if best_price is None or price > best_price:
                best_price = price
                best_qty = qty

        return best_price, best_qty

    def record_trade_entry(self, size_usd: float):
        """Record that we entered a trade today to enforce the daily limit."""
        self.daily_stats.trades_placed += 1

    def record_trade_result(self, pnl: float):
        """Record a trade result for circuit breaker tracking."""
        self.daily_stats.total_pnl += pnl
        if pnl > 0:
            self.daily_stats.trades_won += 1
            self.daily_stats.consecutive_losses = 0
        else:
            self.daily_stats.trades_lost += 1
            self.daily_stats.consecutive_losses += 1

    def _save_daily_stats(self):
        """Persist daily stats to log file."""
        try:
            log_path = Path(settings.log_file).parent / "daily_stats.jsonl"
            with open(log_path, "a") as f:
                f.write(json.dumps({
                    "date": self.daily_stats.date,
                    "trades": self.daily_stats.trades_placed,
                    "won": self.daily_stats.trades_won,
                    "lost": self.daily_stats.trades_lost,
                    "pnl": self.daily_stats.total_pnl,
                    "halted": self.daily_stats.is_halted,
                }) + "\n")
        except Exception:
            pass

    def get_status_report(self) -> str:
        """Human-readable risk status."""
        s = self.daily_stats
        return (
            f"📊 Risk Status [{s.date}]\n"
            f"   Trades: {s.trades_placed}/{self.max_daily_trades} "
            f"(W:{s.trades_won} L:{s.trades_lost})\n"
            f"   Daily P&L: ${s.total_pnl:+.2f} "
            f"(limit: ${settings.max_daily_loss:.2f})\n"
            f"   Consecutive losses: {s.consecutive_losses}/{self.max_consecutive_losses}\n"
            f"   Status: {'🚨 HALTED — ' + s.halt_reason if s.is_halted else '✅ Active'}"
        )
