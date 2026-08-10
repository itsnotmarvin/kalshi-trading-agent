#!/usr/bin/env python3
"""
Kalshi Forecasting Lab
======================
Experimental prediction-market research and paper-validation workflow.

Usage:
    python main.py --mode paper              # Paper trading (no real money)
    python main.py --mode paper --once       # Run one cycle and exit
    python main.py --mode live               # Live trading (REAL MONEY)
    python main.py --calibration             # Show calibration report
    python main.py --status                  # Show risk status

⚠️  WARNING: Live trading uses real money. Always paper trade first.
    Only use money you can afford to lose. This is not financial advice.
"""
import asyncio
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from config.settings import settings
from core.agent import TradingAgent
from core.risk_manager import RiskManager, TradeProposal
from core.logger import TradeLogger
from core.trade_utils import direction_to_side_and_price, is_executable_direction


def get_adapter():
    """Create the appropriate platform adapter based on settings."""
    if settings.platform == "kalshi":
        from adapters.kalshi_adapter import KalshiAdapter
        return KalshiAdapter()
    elif settings.platform == "manifold":
        from adapters.manifold_adapter import ManifoldAdapter
        return ManifoldAdapter(api_key="")  # Add your key
    elif settings.platform == "polymarket":
        from adapters.polymarket_adapter import PolymarketAdapter
        return PolymarketAdapter()
    else:
        raise ValueError(f"Unknown platform: {settings.platform}")


async def place_live_order_with_audit(
    adapter,
    logger: TradeLogger,
    market_id: str,
    side,
    price: float,
    contracts: float,
    proposal: TradeProposal,
):
    logger.log_order_attempt(
        client_order_id=proposal.client_order_id or "",
        market_id=market_id,
        side=side.value,
        price=price,
        quantity=contracts,
        status="attempted",
        extra={
            "post_only": proposal.post_only,
            "direction": proposal.direction,
            "size_usd": proposal.position_size_usd,
        },
    )
    result = await adapter.place_order(
        market_id=market_id,
        side=side,
        price=price,
        quantity=contracts,
        client_order_id=proposal.client_order_id,
        post_only=proposal.post_only,
    )
    logger.log_order_attempt(
        client_order_id=proposal.client_order_id or "",
        market_id=market_id,
        side=side.value,
        price=price,
        quantity=contracts,
        status="accepted" if result.success else "rejected",
        order_id=result.order.id if result.success and result.order else None,
        error=result.error,
        extra={
            "post_only": proposal.post_only,
            "direction": proposal.direction,
            "size_usd": proposal.position_size_usd,
        },
    )
    return result


async def run_trading_cycle(
    adapter,
    agent: TradingAgent,
    risk_manager: RiskManager,
    logger: TradeLogger,
    is_paper: bool = True,
    pending_entries: dict[str, TradeProposal] | None = None,
):
    """
    Run one complete trading cycle:
    1. Scan markets for opportunities
    2. Research promising markets
    3. Generate trade proposals
    4. Risk-check each proposal
    5. Execute approved trades (or log paper trades)
    """
    cycle_start = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"🔄 Trading Cycle — {cycle_start.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Mode: {'📝 PAPER' if is_paper else '💰 LIVE'}")
    print(f"   Platform: {settings.platform}")
    print(f"{'='*60}")

    # --- Step 1: Get current portfolio state ---
    try:
        positions = await adapter.get_positions()
        if is_paper:
            paper_positions = risk_manager.load_paper_positions()
            # Merge paper positions (prefer paper if both exist for same ID)
            existing_ids = {p.market_id for p in positions}
            for pp in paper_positions:
                if pp.market_id not in existing_ids:
                    positions.append(pp)
            # Paper balance = configured bankroll minus open paper exposure
            paper_exposure = sum(p.quantity * p.avg_price for p in positions)
            balance = max(0.0, settings.paper_balance - paper_exposure)
            portfolio_value = balance + sum(p.quantity * p.current_price for p in positions)
        else:
            balance = await adapter.get_balance()
            portfolio_value = await adapter.get_portfolio_value()
        print(f"\n💰 Balance: ${balance:.2f} | Positions: {len(positions)} | Portfolio: ${portfolio_value:.2f}")
    except Exception as e:
        logger.log_error("portfolio_fetch", str(e))
        print(f"❌ Could not fetch portfolio: {e}")
        return

    # Update risk manager's peak tracking
    risk_manager.daily_stats.current_portfolio = portfolio_value
    if portfolio_value > risk_manager.daily_stats.peak_portfolio:
        risk_manager.daily_stats.peak_portfolio = portfolio_value

    if risk_manager.daily_stats.is_halted:
        print(f"\n🚨 Trading is HALTED: {risk_manager.daily_stats.halt_reason}")
        print(risk_manager.get_status_report())
        return

    # --- Step 1.5: Evaluate Exits ---
    exits = risk_manager.evaluate_exits(positions)
    if exits:
        print(f"\n🎯 Found {len(exits)} exit opportunities...")
        for pos, reason in exits:
            print(f"   EXIT: {pos.market_question[:60]}... ({reason})")
            if is_paper:
                logger.log_resolution(
                    market_id=pos.market_id,
                    question=pos.market_question,
                    outcome="MANUAL_TP",
                    our_estimate=0,
                    market_price_at_entry=pos.avg_price,
                    pnl=pos.unrealized_pnl
                )
                # Update paper positions
                p_pos = risk_manager.load_paper_positions()
                p_pos = [p for p in p_pos if p.market_id != pos.market_id]
                risk_manager.save_paper_positions(p_pos)
            else:
                # Live sell order
                print(f"   💰 Placing live SELL order...")
                # For Kalshi, selling YES is Side.YES, action="sell"
                res = await adapter.place_order(
                    market_id=pos.market_id,
                    side=pos.side,
                    price=pos.current_price,
                    quantity=pos.quantity,
                    action="sell"
                )
                if res.success:
                    print(f"   ✅ Exit order placed")
                else:
                    print(f"   ❌ Exit failed: {res.error}")

    pending_entries = pending_entries if pending_entries is not None else {}
    if pending_entries:
        print(f"\n🎯 Checking {len(pending_entries)} pending entries...")
        executed_entries = []
        for market_id, proposal in list(pending_entries.items()):
            try:
                market = await adapter.get_market(market_id)
                if not market or not proposal.entry_direction:
                    continue

                side, current_price = direction_to_side_and_price(
                    proposal.entry_direction,
                    market.yes_price,
                )
                proposal.direction = proposal.entry_direction
                proposal.market_price = market.yes_price
                try:
                    risk_manager.attach_orderbook_metrics(
                        proposal,
                        await adapter.get_orderbook(market_id),
                    )
                    current_price = risk_manager.get_side_cost(proposal) or current_price
                except Exception as e:
                    logger.log_error("pending_entry_orderbook", str(e))
                    continue

                if current_price > 0.01 and current_price <= proposal.target_entry_price:
                    print(
                        f"   Triggered: {proposal.entry_direction} {market.question[:60]}... "
                        f"@ ${current_price:.2f} (target ${proposal.target_entry_price:.2f})"
                    )
                    approved, reasons = risk_manager.check_trade(
                        proposal,
                        positions,
                        balance,
                        portfolio_value,
                        paper_mode=is_paper,
                    )
                    if not approved:
                        print(f"   ❌ Pending entry rejected: {'; '.join(reasons[:2])}")
                        logger.log_trade(
                            market_id=proposal.market_id,
                            direction=proposal.direction,
                            price=current_price,
                            size_usd=proposal.position_size_usd,
                            estimated_prob=proposal.estimated_prob,
                            edge=proposal.edge,
                            approved=False,
                            rejection_reasons=reasons,
                            extra={"execution_metrics": proposal.execution_metrics},
                        )
                        continue

                    if is_paper:
                        risk_manager.record_trade_entry(proposal.position_size_usd)
                        risk_manager.record_paper_trade(proposal)
                        logger.log_trade(
                            market_id=proposal.market_id,
                            direction=proposal.direction,
                            price=current_price,
                            size_usd=proposal.position_size_usd,
                            estimated_prob=proposal.estimated_prob,
                            edge=proposal.edge,
                            approved=True,
                            extra={"execution_metrics": proposal.execution_metrics},
                        )
                    else:
                        contracts = proposal.position_size_usd / current_price if current_price > 0 else 0
                        if not proposal.client_order_id:
                            proposal.client_order_id = f"kalshi-{market_id}-{uuid.uuid4().hex[:16]}"
                        result = await place_live_order_with_audit(
                            adapter,
                            logger,
                            proposal.market_id,
                            side,
                            current_price,
                            contracts,
                            proposal,
                        )
                        if not result.success:
                            print(f"   ❌ Pending entry failed: {result.error}")
                            logger.log_error("pending_entry_execution", result.error)
                            continue

                    executed_entries.append(market_id)
            except Exception as e:
                logger.log_error("pending_entry_check", str(e))

        for market_id in executed_entries:
            pending_entries.pop(market_id, None)

    # --- Step 2: Scan markets ---
    print(f"\n🔍 Scanning markets (min volume: ${settings.min_market_volume:,.0f})...")
    try:
        markets = await adapter.get_markets(
            active_only=True,
            min_volume=settings.min_market_volume,
            limit=30,
        )
        print(f"   Found {len(markets)} markets above volume threshold")
        logger.log_scan(len(markets), 0)
    except Exception as e:
        logger.log_error("market_scan", str(e))
        print(f"❌ Market scan failed: {e}")
        return

    # Filter out markets already in portfolio
    initial_count = len(markets)
    markets = [m for m in markets if not any(p.market_id == m.id for p in positions)]
    if len(markets) < initial_count:
        print(f"   Filtered out {initial_count - len(markets)} markets already in portfolio")

    if not markets:
        print("   No markets found. Check your filters or try again later.")
        return

    # --- Step 3: Claude selects promising markets ---
    print(f"\n🧠 Claude scanning for opportunities...")
    selected_ids = agent.scan_markets(markets)
    print(f"   Selected {len(selected_ids)} markets for deep research")

    if not selected_ids:
        print("   No promising opportunities found this cycle. That's fine — patience pays.")
        return

    # --- Step 4: Research and analyze each selected market ---
    proposals = []
    for market_id in selected_ids:
        market = next((m for m in markets if m.id == market_id), None)
        if not market:
            continue

        print(f"\n📊 Researching: {market.question[:80]}...")
        print(f"   Current price: YES=${market.yes_price:.2f} | Volume: ${market.volume_24h:,.0f}")

        proposal = agent.research_and_analyze(market, positions, balance)
        if proposal:
            proposals.append(proposal)
            print(f"   Claude's estimate: {proposal.estimated_prob:.1%} "
                  f"(market: {proposal.market_price:.1%})")
            print(f"   Edge: {proposal.edge:.1%} | Direction: {proposal.direction} "
                  f"| Confidence: {proposal.confidence}")

            logger.log_analysis(
                market_id=proposal.market_id,
                question=proposal.market_question,
                market_price=proposal.market_price,
                estimated_prob=proposal.estimated_prob,
                edge=proposal.edge,
                direction=proposal.direction,
                confidence=proposal.confidence,
                reasoning=proposal.reasoning,
            )

    # --- Step 5: Risk-check and execute ---
    trades_executed = 0
    for proposal in proposals:
        if proposal.direction == "HOLD":
            print(f"\n⏸️  HOLD: {proposal.market_question[:60]}... (edge too small or low confidence)")
            continue

        print(f"\n🔒 Risk checking: {proposal.market_question[:60]}...")
        if is_executable_direction(proposal.direction):
            try:
                risk_manager.attach_orderbook_metrics(
                    proposal,
                    await adapter.get_orderbook(proposal.market_id),
                )
                if not proposal.client_order_id:
                    proposal.client_order_id = f"kalshi-{proposal.market_id}-{uuid.uuid4().hex[:16]}"
            except Exception as e:
                proposal.execution_metrics["orderbook_error"] = str(e)[:200]
                logger.log_error("proposal_orderbook", str(e))

        approved, reasons = risk_manager.check_trade(
            proposal, positions, balance, portfolio_value
        )

        if proposal.direction == "WAIT_FOR_ENTRY":
            if approved and proposal.target_entry_price > 0 and proposal.entry_direction:
                pending_entries[proposal.market_id] = proposal
                print(
                    f"   ⏳ WAITING FOR ENTRY: {proposal.entry_direction} at "
                    f"${proposal.target_entry_price:.2f}"
                )
            elif approved:
                print("   ⚠️  WAIT_FOR_ENTRY missing a valid target price or entry direction")
            else:
                print(f"   ❌ REJECTED:")
                for r in reasons:
                    print(f"      • {r}")
            continue

        if not approved:
            print(f"   ❌ REJECTED:")
            for r in reasons:
                print(f"      • {r}")
            logger.log_trade(
                market_id=proposal.market_id,
                direction=proposal.direction,
                price=risk_manager.get_side_cost(proposal),
                size_usd=proposal.position_size_usd,
                estimated_prob=proposal.estimated_prob,
                edge=proposal.edge,
                approved=False,
                rejection_reasons=reasons,
                extra={"execution_metrics": proposal.execution_metrics},
            )
            continue

        # Trade approved!
        print(f"   ✅ APPROVED: {proposal.direction} ${proposal.position_size_usd:.2f}")

        if not is_executable_direction(proposal.direction):
            print("   ⚠️  Non-executable direction skipped")
            continue

        if is_paper:
            print(f"   📝 PAPER TRADE (not executed)")
            logger.log_trade(
                market_id=proposal.market_id,
                direction=proposal.direction,
                price=risk_manager.get_side_cost(proposal),
                size_usd=proposal.position_size_usd,
                estimated_prob=proposal.estimated_prob,
                edge=proposal.edge,
                approved=True,
                extra={"execution_metrics": proposal.execution_metrics},
            )
            trades_executed += 1
            risk_manager.record_paper_trade(proposal)
        else:
            # LIVE EXECUTION
            print(f"   💰 EXECUTING LIVE TRADE...")
            side, price = direction_to_side_and_price(
                proposal.entry_direction or proposal.direction,
                proposal.market_price,
            )
            price = risk_manager.get_side_cost(proposal) or price

            # Convert USD to contracts
            contracts = proposal.position_size_usd / price if price > 0 else 0

            result = await place_live_order_with_audit(
                adapter,
                logger,
                proposal.market_id,
                side,
                price,
                contracts,
                proposal,
            )

            if result.success:
                print(f"   ✅ Order placed: {result.order.id}")
                trades_executed += 1
                risk_manager.daily_stats.trades_placed += 1
            else:
                print(f"   ❌ Order failed: {result.error}")
                logger.log_error("order_execution", result.error)

            logger.log_trade(
                market_id=proposal.market_id,
                direction=proposal.entry_direction or proposal.direction,
                price=price,
                size_usd=proposal.position_size_usd,
                estimated_prob=proposal.estimated_prob,
                edge=proposal.edge,
                approved=True,
                extra={"execution_metrics": proposal.execution_metrics},
            )

    # --- Summary ---
    elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
    print(f"\n{'─'*60}")
    print(f"✅ Cycle complete in {elapsed:.1f}s")
    print(f"   Markets scanned: {len(markets)} → Researched: {len(selected_ids)} → "
          f"Trades: {trades_executed}")
    print(risk_manager.get_status_report())


async def main():
    parser = argparse.ArgumentParser(description="Kalshi Forecasting Lab")
    parser.add_argument(
        "--mode", choices=["paper", "live"], default="paper",
        help="Trading mode (default: paper)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle and exit"
    )
    parser.add_argument(
        "--calibration", action="store_true",
        help="Show calibration report and exit"
    )
    parser.add_argument(
        "--weather-validation", action="store_true",
        help="Show Kalshi weather-only validation report and exit"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show risk status and exit"
    )
    parser.add_argument(
        "--platform", choices=["kalshi", "polymarket", "manifold"],
        help="Override platform from .env"
    )
    parser.add_argument(
        "--paper-balance", type=float, default=None,
        help="Starting paper trading bankroll in USD (default: $1000 or PAPER_BALANCE env)"
    )
    args = parser.parse_args()

    # Override settings from CLI
    if args.platform:
        settings.platform = args.platform
    if args.mode:
        settings.trading_mode = args.mode
    if args.paper_balance is not None:
        if args.paper_balance <= 0:
            print("❌ --paper-balance must be greater than 0")
            sys.exit(1)
        settings.paper_balance = args.paper_balance

    # Initialize components
    logger = TradeLogger()
    risk_manager = RiskManager()

    # Handle info-only commands
    if args.calibration:
        print(logger.get_calibration_report())
        return
    if args.weather_validation:
        print(logger.get_weather_validation_report())
        return

    if args.status:
        print(risk_manager.get_status_report())
        return

    # Validate configuration
    errors = settings.validate()
    if errors:
        print("❌ Configuration errors:")
        for e in errors:
            print(f"   • {e}")
        print("\nCopy .env.example to .env and fill in your API keys.")
        sys.exit(1)

    # Safety warning for live mode
    is_paper = args.mode == "paper"
    if not is_paper:
        print("\n" + "⚠️ " * 20)
        print("  YOU ARE ABOUT TO TRADE WITH REAL MONEY")
        print("  This is not financial advice. You can lose money.")
        print("⚠️ " * 20)
        confirm = input("\nType 'I UNDERSTAND THE RISKS' to continue: ")
        if confirm != "I UNDERSTAND THE RISKS":
            print("Aborted. Use --mode paper for paper trading.")
            sys.exit(0)

    # Connect to platform
    adapter = get_adapter()
    agent = TradingAgent(risk_manager)

    print(f"\n🚀 Starting Kalshi Forecasting Lab")
    print(f"   Platform: {settings.platform}")
    print(f"   Mode: {'📝 Paper' if is_paper else '💰 LIVE'}")
    if is_paper:
        print(f"   Paper bankroll: ${settings.paper_balance:.2f}")
    print(f"   Cycle interval: {settings.cycle_interval_minutes} minutes")
    print(f"   Max daily loss: ${settings.max_daily_loss:.2f}")
    print(f"   Min edge: {settings.min_edge_threshold:.0%}")

    connected = await adapter.connect()
    if not connected:
        print("❌ Failed to connect to platform. Check your credentials.")
        sys.exit(1)

    # Main loop
    pending_entries: dict[str, TradeProposal] = {}
    if args.once:
        await run_trading_cycle(
            adapter, agent, risk_manager, logger, is_paper, pending_entries
        )
    else:
        print(f"\n🔁 Running continuously (Ctrl+C to stop)...")
        while True:
            try:
                await run_trading_cycle(
                    adapter, agent, risk_manager, logger, is_paper, pending_entries
                )
                print(f"\n⏳ Next cycle in {settings.cycle_interval_minutes} minutes...")
                await asyncio.sleep(settings.cycle_interval_minutes * 60)
            except KeyboardInterrupt:
                print("\n\n👋 Shutting down gracefully...")
                break
            except Exception as e:
                logger.log_error("main_loop", str(e))
                print(f"\n⚠️  Error in cycle: {e}")
                print(f"   Retrying in 60 seconds...")
                await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
