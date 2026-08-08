"""
Background Trading Loop — extracted from server.py for maintainability.

Contains the main trading_loop() coroutine that runs in the background,
scanning markets, generating proposals, executing trades, and managing
the bot lifecycle. Also contains mode-specific logic (weather, sports,
crypto, climate) and the _wait_for_next_cycle helper.

This module receives shared state objects (BotState, RiskManager, etc.)
from the server module — it does NOT own them.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from adapters.base import TradeResult, Side, Order, OrderStatus, Market  # type: ignore
from config.settings import settings  # type: ignore
from core.notifier import send_telegram_alert  # type: ignore
from core.agent import TradingAgent  # type: ignore
from core.risk_manager import RiskManager, TradeProposal  # type: ignore
from core.logger import TradeLogger  # type: ignore
from core.trade_utils import direction_to_side_and_price, is_executable_direction  # type: ignore
from core.postmortem_engine import PostmortemEngine  # type: ignore
from core.memory_manager import MemoryManager  # type: ignore


# These will be set by server.py before starting the loop
state = None          # BotState instance
risk_manager = None   # RiskManager instance
logger = None         # TradeLogger instance
memory_manager = None # MemoryManager instance
get_adapter = None    # Callable that returns the platform adapter
_maybe_auto_reflect = None  # Callable for auto-reflection


def init(
    _state,
    _risk_manager,
    _logger,
    _memory_manager,
    _get_adapter,
    _auto_reflect,
):
    """Initialize module-level references to shared state. Call before trading_loop()."""
    global state, risk_manager, logger, memory_manager, get_adapter, _maybe_auto_reflect
    state = _state
    risk_manager = _risk_manager
    logger = _logger
    memory_manager = _memory_manager
    get_adapter = _get_adapter
    _maybe_auto_reflect = _auto_reflect


def _log_trade_decision(proposal, market, approved: bool, reasons: list[str], model_source: str):
    """Record a trade decision with the execution facts used by risk checks."""
    if not logger:
        return

    side_cost = risk_manager.get_side_cost(proposal)
    estimated_contracts = (
        proposal.execution_metrics.get("contracts")
        or (proposal.position_size_usd / side_cost if side_cost > 0 else 0)
    )
    logger.log_trade(
        market_id=market.id,
        direction=proposal.direction,
        price=side_cost,
        size_usd=proposal.position_size_usd,
        estimated_prob=proposal.estimated_prob,
        edge=proposal.edge,
        approved=approved,
        rejection_reasons=reasons,
        extra={
            "question": market.question,
            "category": proposal.category,
            "yes_price": proposal.market_price,
            "no_price": 1.0 - proposal.market_price,
            "side_cost": side_cost,
            "contract_count": estimated_contracts,
            "top_of_book_spread": proposal.execution_metrics.get("side_spread"),
            "liquidity_depth_usd": proposal.execution_metrics.get("side_depth_usd"),
            "probability_gap": proposal.execution_metrics.get("probability_gap", proposal.edge),
            "model_source": model_source,
            "client_order_id": proposal.client_order_id,
            "post_only": proposal.post_only,
            "execution_metrics": proposal.execution_metrics,
        },
    )


async def _place_live_order_with_audit(
    adapter,
    market_id: str,
    side,
    price: float,
    contracts: float,
    proposal,
):
    """Place a Kalshi live order and persist an idempotency audit trail."""
    if logger:
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

    res = await adapter.place_order(
        market_id,
        side,
        price,
        contracts,
        client_order_id=proposal.client_order_id,
        post_only=proposal.post_only,
    )

    if logger:
        logger.log_order_attempt(
            client_order_id=proposal.client_order_id or "",
            market_id=market_id,
            side=side.value,
            price=price,
            quantity=contracts,
            status="accepted" if res.success else "rejected",
            order_id=res.order.id if res.success and res.order else None,
            error=res.error,
            extra={
                "post_only": proposal.post_only,
                "direction": proposal.direction,
                "size_usd": proposal.position_size_usd,
            },
        )

    return res


async def _refresh_and_settle_paper_positions(adapter, agent):
    """
    Paper-mode position maintenance, run once per cycle.

      • RESOLVED markets  → book realized P&L into category stats, the
        circuit-breaker stats, the resolution log (calibration) and agent
        memory, then drop the position from the paper book.
      • Still-open markets → refresh the live mark-to-market price so
        take-profit exits and the dashboard reflect reality.

    Returns the list of still-open paper positions.
    """
    positions = risk_manager.load_paper_positions()
    if not positions:
        return []

    still_open: list = []
    settled = 0

    for pos in positions:
        market = None
        try:
            market = await adapter.get_market(pos.market_id)
        except Exception as e:
            state.add_log("warn", f"⚠️ Settlement: could not fetch {pos.market_id}: {e}")

        if market is None:
            # Can't determine status this cycle — leave the position untouched.
            still_open.append(pos)
            continue

        result = str((market.raw or {}).get("result", "")).lower()

        if result in ("yes", "no"):
            # === Market RESOLVED — book the realized P&L ===
            pnl, won = risk_manager.realized_pnl_on_settlement(pos, result)
            risk_manager.record_category_result(pos.category, pnl)
            risk_manager.record_trade_result(pnl)
            payout = pos.quantity if won else 0.0
            state.balance += payout
            state.resolved_since_last_reflection += 1
            settled += 1

            outcome = result.upper()
            verb = "WON" if won else "LOST"
            emoji = "✅" if won else "❌"

            logger.log_resolution(
                market_id=pos.market_id,
                question=pos.market_question,
                outcome=outcome,
                our_estimate=pos.entry_prob if pos.entry_prob > 0 else pos.avg_price,
                market_price_at_entry=pos.avg_price,
                pnl=pnl,
            )
            state.add_log(
                "success" if won else "warn",
                f"{emoji} SETTLED {pos.market_id}: resolved {outcome} — "
                f"{pos.side.name} position {verb} (P&L ${pnl:+.2f})",
            )
            asyncio.create_task(send_telegram_alert(
                f"{emoji} <b>MARKET SETTLED</b>\n\n"
                f"<b>Market:</b> {pos.market_question}\n"
                f"<b>Resolved:</b> {outcome} — our {pos.side.name} bet {verb}\n"
                f"<b>Realized P&L:</b> ${pnl:+.2f}"
            ))
            asyncio.create_task(agent.generate_lesson(
                market_id=pos.market_id,
                question=pos.market_question,
                reasoning=(
                    f"Held {pos.quantity:.0f}x {pos.side.name} @ ${pos.avg_price:.2f} "
                    f"until the market resolved {outcome}."
                ),
                outcome_pnl=pnl,
                outcome_msg=f"Market resolved {outcome}. Position {verb}.",
            ))
            continue

        # === Still open — refresh mark-to-market for this side ===
        side_price = (1.0 - market.no_price) if pos.side == Side.YES else (1.0 - market.yes_price)
        if 0.0 < side_price < 1.0:
            pos.current_price = side_price
            pos.unrealized_pnl = round(
                (pos.current_price - pos.avg_price) * pos.quantity,
                4,
            )
        still_open.append(pos)

    risk_manager.save_paper_positions(still_open)
    if settled:
        await _maybe_auto_reflect()
    return still_open


async def _process_trade_proposal(
    adapter, proposal, market, positions, is_live: bool,
    mode_emoji: str, mode_name: str, telegram_title: str,
):
    """
    Unified execution helper for all trade proposals.

    Builds the proposal_dict, runs risk_manager.check_trade, executes the
    trade (live or paper), logs, and fires Telegram alerts.

    Returns (executed, approved, reasons) so callers can react
    (e.g. set phase_1_executed for weather scanning).
    """
    if is_executable_direction(proposal.direction):
        try:
            orderbook = await adapter.get_orderbook(market.id)
            risk_manager.attach_orderbook_metrics(proposal, orderbook)
            proposal.position_size_usd = float(
                proposal.execution_metrics.get("estimated_total_cost")
                or proposal.position_size_usd
            )
        except Exception as e:
            proposal.execution_metrics["orderbook_error"] = str(e)[:200]
            state.add_log("warn", f"{mode_name} orderbook fetch failed for {market.id}: {e}")

    if not proposal.client_order_id and is_executable_direction(proposal.direction):
        proposal.client_order_id = f"kalshi-{market.id}-{uuid.uuid4().hex[:16]}"

    proposal_dict = {
        "market_id": market.id,
        "question": market.question,
        "category": proposal.category,
        "direction": proposal.direction,
        "estimated_prob": proposal.estimated_prob,
        "market_price": proposal.market_price,
        "edge": proposal.edge,
        "r_score": getattr(proposal, "r_score", 0.0),
        "confidence": proposal.confidence,
        "size_usd": proposal.position_size_usd,
        "reasoning": proposal.reasoning,
        "risk_factors": proposal.risk_factors,
        "entry_direction": proposal.entry_direction,
        "target_entry_price": proposal.target_entry_price,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "approved": None,
        "rejection_reasons": [],
        "client_order_id": proposal.client_order_id,
        "post_only": proposal.post_only,
        "execution_metrics": proposal.execution_metrics,
    }

    balance_before = state.balance
    portfolio_value_before = state.portfolio_value
    execution_blockers: list[str] = []

    approved, reasons = risk_manager.check_trade(
        proposal, positions, balance_before, portfolio_value_before,
        paper_mode=not is_live
    )
    proposal_dict["execution_metrics"] = proposal.execution_metrics

    _log_trade_decision(proposal, market, approved, reasons, mode_name.lower())

    executed = False

    if proposal.direction == "HOLD":
        state.add_log("info", f"{mode_name} trade passed: {market.question[:50]} (no edge)")
    elif proposal.direction == "WAIT_FOR_ENTRY":
        if approved and proposal.target_entry_price > 0 and proposal.entry_direction:
            state.pending_entries[proposal.market_id] = {
                "proposal": proposal,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            state.add_log(
                "info",
                f"\u23f3 {mode_name} PENDING SNIPE: {market.question[:50]} "
                f"({proposal.entry_direction} at ${proposal.target_entry_price:.2f})",
            )
        else:
            state.add_log("warn", f"REJECTED: {market.question[:50]} \u2014 {'; '.join(reasons[:2])}")
    elif approved and is_executable_direction(proposal.direction):
        executed = True
        state.trades_proposed += 1
        state.trades_approved += 1
        side, price = direction_to_side_and_price(
            proposal.entry_direction or proposal.direction, proposal.market_price,
        )
        side_cost = risk_manager.get_side_cost(proposal)
        price = side_cost if side_cost > 0 else price
        contracts = float(
            proposal.execution_metrics.get("contracts")
            or proposal.execution_metrics.get("requested_contracts")
            or (proposal.position_size_usd / price if price > 0 else 0)
        )

        if is_live:
            if proposal.position_size_usd <= state.balance:
                res = await _place_live_order_with_audit(
                    adapter, market.id, side, price, contracts, proposal
                )
                if res.success:
                    state.add_log("success", f"\U0001f4b0 {mode_name} LIVE TRADE: {market.id}")
                    risk_manager.record_trade_entry(proposal.position_size_usd)
                    state.balance -= proposal.position_size_usd
                    msg = (
                        f"{mode_emoji} <b>{telegram_title}</b> {mode_emoji}\n\n"
                        f"<b>Market:</b> {market.question}\n"
                        f"<b>Action:</b> {proposal.direction} @ ${price:.2f}\n"
                        f"<b>Size:</b> ${proposal.position_size_usd:.2f}\n\n"
                        f"<b>Logic:</b>\n<i>{proposal.reasoning}</i>"
                    )
                    asyncio.create_task(send_telegram_alert(msg))
                else:
                    failure = f"{mode_name} order failed: {res.error}"
                    execution_blockers.append(failure)
                    state.add_log("error", f"{mode_name} trade failed: {res.error}")
                    executed = False
            else:
                failure = f"Insufficient live balance for {mode_name} trade."
                execution_blockers.append(failure)
                state.add_log("warn", failure)
                executed = False
        else:
            if proposal.position_size_usd > state.balance:
                failure = (
                    f"Insufficient paper balance for {mode_name} trade "
                    f"(${proposal.position_size_usd:.2f} > ${state.balance:.2f})."
                )
                execution_blockers.append(failure)
                state.add_log("warn", failure)
                executed = False
            else:
                state.add_log(
                    "success",
                    f"\U0001f4dd PAPER {mode_name} TRADE: {proposal.direction} "
                    f"${proposal.position_size_usd:.2f} on {market.id}",
                )
                risk_manager.record_trade_entry(proposal.position_size_usd)
                risk_manager.record_paper_trade(proposal)
                state.balance -= proposal.position_size_usd
    else:
        state.add_log("warn", f"{mode_name} trade rejected: {'; '.join(reasons[:2])}")

    if execution_blockers:
        proposal_dict["approved"] = False
        proposal_dict["rejection_reasons"] = reasons + execution_blockers
    else:
        proposal_dict["approved"] = approved
        proposal_dict["rejection_reasons"] = reasons
    proposal_dict["executed"] = executed
    proposal_dict["risk_check"] = risk_manager.build_trade_risk_snapshot(
        proposal,
        positions,
        balance_before,
        portfolio_value_before,
        approved,
        reasons,
        paper_mode=not is_live,
        executed=executed,
        execution_blockers=execution_blockers,
    )
    state.proposals.insert(0, proposal_dict)
    state.proposals = state.proposals[:150]

    return executed, approved, reasons


async def trading_loop():
    """The main trading loop that runs in the background."""
    from adapters.base import Side  # type: ignore
    from core.agent import TradingAgent

    try:
        adapter = get_adapter()
        agent = TradingAgent(risk_manager, active_directive=state.active_directive)
        # Ensure memory is consistent
        agent.memory = memory_manager
    
        state.add_log("info", f"Connecting to {settings.platform}...")
        connected = await adapter.connect()
    except Exception as e:
        import traceback
        state.add_log("error", f"Bot Init Error: {e} | {traceback.format_exc().splitlines()[-1]}")
        state.is_running = False
        return

    if not connected:
        state.add_log("error", "Failed to connect to platform")
        state.is_running = False
        return

    state.add_log("success", f"Connected to {settings.platform}")

    while state.is_running:
        if state.is_paused:
            state.current_activity = "Paused"
            await asyncio.sleep(5)
            continue

        try:
            cycle_start = datetime.now(timezone.utc)
            state.cycle_count += 1
            state.last_cycle_time = cycle_start.isoformat()
            state.current_activity = "Fetching portfolio..."
            state.add_log("info", f"Starting cycle #{state.cycle_count}")

            # Fetch portfolio
            try:
                is_paper_mode = state.mode == "paper" or state.mode.endswith("_paper")

                if is_paper_mode:
                    # Paper mode: positions live in paper_positions.json, NOT the
                    # real Kalshi account. Refresh prices + settle resolved markets.
                    positions = await _refresh_and_settle_paper_positions(adapter, agent)
                else:
                    positions = await adapter.get_positions()

                if is_paper_mode:
                    # Initialize paper bankroll on first cycle, then track via decrements
                    if state.initial_balance == 0.0:
                        state.balance = settings.paper_balance
                        state.initial_balance = settings.paper_balance
                    state.portfolio_value = state.balance + sum(
                        p.quantity * p.current_price for p in positions
                    )
                else:
                    state.balance = await adapter.get_balance()
                    state.portfolio_value = await adapter.get_portfolio_value()
                    # Capture initial balance on first cycle
                    if state.initial_balance == 0.0 and state.balance > 0:
                        state.initial_balance = state.balance

                # Compute total profit / total loss from category stats
                cat_stats = risk_manager.category_stats if hasattr(risk_manager, 'category_stats') else {}
                total_p: float = 0.0
                total_l: float = 0.0
                for cat_data in cat_stats.values():  # type: ignore
                    # Explicitly check for dict/getattr to help the analyzer
                    pnl = 0.0
                    if isinstance(cat_data, dict):
                        pnl = float(cat_data.get("pnl", 0.0))
                    else:
                        pnl = float(getattr(cat_data, "pnl", 0.0))
                    if pnl > 0:
                        total_p = total_p + pnl  # type: ignore
                    elif pnl < 0:
                        total_l = total_l + abs(pnl)  # type: ignore
                state.total_profit = total_p  # type: ignore
                state.total_loss = total_l    # type: ignore

                # Track peak portfolio for the Drawdown circuit breaker
                if state.portfolio_value > risk_manager.daily_stats.peak_portfolio:
                    risk_manager.daily_stats.peak_portfolio = state.portfolio_value
                    
                state.positions = [
                    {
                        "market_id": p.market_id,
                        "market_question": p.market_question,
                        "side": p.side.value,
                        "quantity": p.quantity,
                        "avg_price": p.avg_price,
                        "current_price": p.current_price,
                        "pnl": p.unrealized_pnl,
                        "end_date": p.end_date.isoformat() if p.end_date else None,
                    }
                    for p in positions
                ]
            except Exception as e:
                state.add_log("error", f"Portfolio fetch failed: {e}")
                positions = None # Use None to signal failure

            # === SCALPER ENGINE (AUTO-EXITS) ===
            state.current_activity = "Checking scalping opportunities..."
            if hasattr(risk_manager, "evaluate_exits"):
                exits = risk_manager.evaluate_exits(positions)
                for pos, reason in exits:
                    if pos.market_id in state.processed_exits:
                        continue

                    state.add_log("info", f"🔪 SCALP TRIGGERED: {pos.market_id} - {reason}")
                    if not state.mode.endswith("_paper"):
                        res = await adapter.place_order(
                            market_id=pos.market_id,
                            side=pos.side,
                            price=pos.current_price,
                            quantity=pos.quantity,
                            action="sell"
                        )
                        if res.success:
                            state.processed_exits.add(pos.market_id)
                            state.add_log("success", f"💸 SCALPED PROPHIT: Sold {pos.quantity} shares of {pos.market_id}")
                            
                            # Log category discipline result
                            try:
                                m = await adapter.get_market(pos.market_id)
                                if m is not None:
                                    # Cast m to Market to help the analyzer
                                    from adapters.base import Market  # type: ignore
                                    market_obj: Market = m
                                    risk_manager.record_category_result(market_obj.category, pos.unrealized_pnl)
                                logger.log_resolution(
                                    market_id=pos.market_id,
                                    question=pos.market_question,
                                    outcome="SOLD",
                                    our_estimate=pos.current_price,
                                    market_price_at_entry=pos.avg_price,
                                    pnl=pos.unrealized_pnl
                                )
                            except Exception:
                                pass
                                
                            risk_manager.record_trade_result(pos.unrealized_pnl)
                            state.resolved_since_last_reflection += 1
                            await _maybe_auto_reflect()
                                
                            msg = f"💸 <b>AUTO-SCALP COMPLETE</b> 💸\n\n"
                            msg += f"<b>Market:</b> {pos.market_question}\n"
                            msg += f"<b>Action:</b> SOLD {pos.quantity} {pos.side.name} @ ${pos.current_price:.2f}\n"
                            msg += f"<b>Reason:</b> <i>{reason}</i>"
                            asyncio.create_task(send_telegram_alert(msg))
                            
                            # Generate a lesson for the bot's memory
                            asyncio.create_task(
                                agent.generate_lesson(
                                    market_id=pos.market_id,
                                    question=pos.market_question,
                                    reasoning=f"Exited position because: {reason}",
                                    outcome_pnl=pos.unrealized_pnl,
                                    outcome_msg=f"Sold {pos.quantity} shares at ${pos.current_price:.2f}"
                                )
                            )
                        else:
                            # If it failed because of insufficient contracts, mark it as processed
                            # to avoid spamming the same error.
                            if "insufficient" in str(res.error).lower() or "not enough" in str(res.error).lower():
                                state.processed_exits.add(pos.market_id)
                            state.add_log("error", f"Failed to scalp: {res.error}")
                    else:
                        state.processed_exits.add(pos.market_id)
                        state.add_log("success", f"Paper Scalp executed for {pos.market_id}.")

                        # Log category discipline result (Paper)
                        try:
                            m = await adapter.get_market(pos.market_id)
                            if m:
                                risk_manager.record_category_result(m.category, pos.unrealized_pnl)
                        except Exception:
                            pass

                        risk_manager.record_trade_result(pos.unrealized_pnl)
                        state.balance += pos.quantity * pos.current_price
                        state.resolved_since_last_reflection += 1

                        # Drop the sold position from the paper book.
                        remaining = [
                            pp for pp in risk_manager.load_paper_positions()
                            if pp.market_id != pos.market_id
                        ]
                        risk_manager.save_paper_positions(remaining)

                        await _maybe_auto_reflect()
                        
                        # Generate a lesson for the bot's memory (Paper)
                        asyncio.create_task(
                            agent.generate_lesson(
                                market_id=pos.market_id,
                                question=pos.market_question,
                                reasoning=f"Exited position because: {reason}",
                                outcome_pnl=pos.unrealized_pnl,
                                outcome_msg=f"Paper Sold {pos.quantity} shares at ${pos.current_price:.2f}"
                            )
                        )

                # Clean up processed_exits for markets no longer in positions
                # Only if positions were successfully fetched
                if isinstance(positions, list):
                    current_pos_ids = {p.market_id for p in positions}
                    state.processed_exits = {m_id for m_id in state.processed_exits if m_id in current_pos_ids}
                else:
                    # Keep existing positions if fetch failed to avoid logic errors
                    positions = []

            # === PENDING SNIPES (SMART ENTRY) ===
            state.current_activity = "Checking pending snipes..."
            if state.pending_entries:
                executed_snipes = []
                for m_id, entry_data in list(state.pending_entries.items()):
                    proposal = entry_data["proposal"]
                    market = await adapter.get_market(m_id)
                    if not market: continue
                    
                    if not proposal.entry_direction:
                        state.add_log("error", f"Pending entry {m_id} is missing an executable direction")
                        executed_snipes.append(m_id)
                        continue

                    proposal.direction = proposal.entry_direction
                    proposal.market_price = market.yes_price

                    try:
                        orderbook = await adapter.get_orderbook(m_id)
                        risk_manager.attach_orderbook_metrics(proposal, orderbook)
                    except Exception as e:
                        proposal.execution_metrics["orderbook_error"] = str(e)[:200]
                        state.add_log("warn", f"Pending snipe orderbook fetch failed for {m_id}: {e}")
                        continue

                    side, fallback_price = direction_to_side_and_price(proposal.direction, market.yes_price)
                    current_price = risk_manager.get_side_cost(proposal) or fallback_price
                    
                    # For a BUY, we want current_price <= target_entry_price
                    if current_price <= proposal.target_entry_price and current_price > 0.01:
                        state.add_log("success", f"🎯 SNIPE TRIGGERED: {m_id} at ${current_price:.2f}")
                        approved, reasons = risk_manager.check_trade(
                            proposal,
                            positions,
                            state.balance,
                            state.portfolio_value,
                            paper_mode=state.mode.endswith("_paper"),
                        )
                        _log_trade_decision(proposal, market, approved, reasons, "pending_snipe")

                        if not approved:
                            state.add_log("warn", f"Pending snipe rejected: {'; '.join(reasons[:2])}")
                            continue
                        
                        if state.mode.endswith("_paper"):
                            if proposal.position_size_usd > state.balance:
                                state.add_log("warn", f"Insufficient paper balance for pending snipe (${proposal.position_size_usd:.2f} > ${state.balance:.2f}).")
                                continue

                            state.add_log("success", f"📝 PAPER SNIPE: {proposal.direction} on {m_id}")
                            risk_manager.record_trade_entry(proposal.position_size_usd)
                            risk_manager.record_paper_trade(proposal)
                            state.balance -= proposal.position_size_usd
                            executed_snipes.append(m_id)
                        else:
                            contracts = proposal.position_size_usd / current_price if current_price > 0 else 0
                            
                            if proposal.position_size_usd <= state.balance:
                                if not proposal.client_order_id:
                                    proposal.client_order_id = f"kalshi-{m_id}-{uuid.uuid4().hex[:16]}"
                                res = await _place_live_order_with_audit(
                                    adapter, m_id, side, current_price, contracts, proposal
                                )
                                if res.success:
                                    state.add_log("success", f"💰 LIVE SNIPE: {m_id}")
                                    risk_manager.record_trade_entry(proposal.position_size_usd)
                                    state.balance -= proposal.position_size_usd
                                    executed_snipes.append(m_id)
                                    
                                    msg = f"🎯 <b>SMART ENTRY SNIPE</b> 🎯\n\n"
                                    msg += f"<b>Market:</b> {market.question}\n"
                                    msg += f"<b>Action:</b> {proposal.entry_direction or proposal.direction} @ ${current_price:.2f}\n"
                                    msg += f"<b>Target Was:</b> ${proposal.target_entry_price:.2f}\n"
                                    msg += f"<b>Size:</b> ${proposal.position_size_usd:.2f}\n"
                                    asyncio.create_task(send_telegram_alert(msg))
                                else:
                                    state.add_log("error", f"Snipe failed: {res.error}")
                            else:
                                state.add_log("warn", f"Skipping snipe {m_id} (insufficient balance)")
                                
                for m_id in executed_snipes:
                    state.pending_entries.pop(m_id, None)




            # === WEATHER MODE (Paper + Live) ===
            if getattr(state, "mode", "") in ("weather_paper", "weather_live"):
                is_live = state.mode == "weather_live"
                from core.weather_engine import WeatherEngine  # type: ignore
                from core.memory_manager import MemoryManager
                
                # Clear researched_markets every cycle — weather contracts change daily
                # and we want to re-analyze every scan (unlike stock/crypto modes).
                state.researched_markets.clear()

                # Load macro lessons to enable "Humility Buffer" and other learned behaviors
                wx_memory = MemoryManager()
                active_lessons = wx_memory.get_macro_lessons()
                weather_engine = WeatherEngine(min_price=0.10, min_confidence=0.50, lessons=active_lessons)
                
                state.current_activity = "Weather ST: scanning weather markets..."

                # Use dedicated weather market fetcher (queries by series ticker)
                if hasattr(adapter, 'get_weather_markets'):
                    weather_markets = await adapter.get_weather_markets(limit=50)
                else:
                    # Fallback for non-Kalshi adapters
                    wx_markets = await adapter.get_markets(
                        active_only=True, min_volume=0, limit=1000
                    )
                    weather_keywords = ["temperature", "temp", "weather", "rain", "snow",
                                        "precipitation", "wind", "degrees", "°f", "°c",
                                        "fahrenheit", "celsius", "forecast", "inches of",
                                        "humidity", "snowfall", "rainfall"]
                    weather_markets = [
                        m for m in wx_markets
                        if (any(kw in m.question.lower() for kw in weather_keywords)
                            or m.category.lower() in ["weather", "climate"])
                        and not m.id.upper().startswith("KXMVE")
                    ]

                state.markets_scanned = len(weather_markets)
                state.add_log("info", f"Weather ST: found {len(weather_markets)} weather markets")
                
                buffer_pct = int(weather_engine.humility_buffer * 100)
                state.add_log("info", f"Thinking differently: Applying {buffer_pct}% safety buffer to all probabilities based on past lessons.")

                if not weather_markets:
                    state.add_log("info", "Weather ST: no weather markets found this scan")
                    await _wait_for_next_cycle()
                    continue

                state.proposals = []
                trades_this_cycle: int = 0

                # ── Phase-Based Scanning Logic ──
                # Phase 1: Only check markets resolving Today
                # Phase 2: If Phase 1 yields no actionable trades, check Tomorrow
                from zoneinfo import ZoneInfo
                ny_tz = ZoneInfo("America/New_York")
                today_ny = datetime.now(ny_tz).date()

                parsed_markets = []
                for market in weather_markets:
                    if market.id in state.researched_markets:
                        continue
                    parsed_market = weather_engine.parse_kalshi_weather_market(market)
                    if parsed_market and parsed_market.target_date:
                        target_ny = parsed_market.target_date.astimezone(ny_tz).date()
                        days_out = (target_ny - today_ny).days
                        if 0 <= days_out <= 1:
                            parsed_markets.append({"market": market, "parsed": parsed_market, "days_out": days_out})
                
                # Phase 1: Today (days_out == 0)
                phase_1_markets = [m for m in parsed_markets if m["days_out"] == 0]
                state.add_log("info", f"Weather Phase 1: Analyzing {len(phase_1_markets)} markets for TODAY.")
                
                phase_1_executed = False
                for m_dict in phase_1_markets:
                    market = m_dict["market"]
                    try:
                        result = await asyncio.to_thread(weather_engine.analyze_market, market)
                    except Exception as e:
                        state.add_log("warn", f"Weather analysis failed for {market.id}: {e}")
                        continue
                    if not result:
                        continue

                    state.researched_markets.add(market.id)
                    
                    # ── Sanity-check log: date + model mean vs threshold ──────
                    hours_left = (
                        (market.end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                        if market.end_date else 999
                    )
                    resolve_str = market.end_date.strftime("%b %d") if market.end_date else "unknown date"
                    mean_temp = result.get("ensemble_mean", 0.0)
                    threshold = result.get("threshold", 0.0)
                    direction_word = "above" if result.get("above", True) else "below"
                    state.add_log("info",
                        f"📊 {market.id}: resolves {resolve_str} ({hours_left:.0f}h). "
                        f"Model mean={mean_temp:.1f}°F vs threshold {direction_word} {threshold}°F. "
                        f"Prob={result.get('probability', 0):.0%}")

                    # Warn if model mean is far from threshold on a same-day market
                    gap = abs(mean_temp - threshold)
                    if hours_left < 24 and gap >= 6:
                        state.add_log("warn",
                            f"⚠️ ACCURACY FLAG {market.id}: market resolves in {hours_left:.0f}h but GFS "
                            f"mean ({mean_temp:.1f}°F) is {gap:.1f}°F from threshold ({threshold}°F). "
                            f"Local forecasts may differ — verify before trading.")

                    proposal = TradeProposal(
                        market_id=market.id,
                        market_question=market.question,
                        category="Weather",
                        direction=result["direction"] if result["should_trade"] else "HOLD",
                        estimated_prob=result["probability"],
                        market_price=result.get("market_price", 0.0),
                        edge=result["edge"],
                        confidence="HIGH" if result["confidence"] > 0.7 else "MEDIUM" if result["confidence"] > 0.4 else "LOW",
                            position_size_usd=settings.min_position_size, # Use configured minimum
                        reasoning=(
                            f"Ensemble: {result.get('member_count', 0)} members \u2192 "
                            f"{result.get('raw_probability', 0):.0%} raw \u2192 "
                            f"Final Prob={result.get('probability', 0):.0%}. "
                            f"({result.get('city', 'Unknown').title()}: {result.get('ensemble_mean', 0):.1f}\u00b0F vs {result.get('threshold', 0)}\u00b0F)"
                        ),
                        risk_factors=[],
                        entry_direction=result.get("direction", "HOLD")
                    )

                    executed, _, _ = await _process_trade_proposal(
                        adapter, proposal, market, positions, is_live,
                        "⛅", "WEATHER", "WEATHER SNIPE FIRED",
                    )
                    if executed:
                        phase_1_executed = True

                # Phase 2: Tomorrow (days_out == 1)
                # Only run if we did not execute a trade today, or if we want to build a bigger portfolio
                if not phase_1_executed:
                    phase_2_markets = [m for m in parsed_markets if m["days_out"] == 1]
                    state.add_log("info", f"Weather Phase 2: No trades today. Analyzing {len(phase_2_markets)} markets for TOMORROW.")
                    for m_dict in phase_2_markets:
                        market = m_dict["market"]
                        try:
                            result = await asyncio.to_thread(weather_engine.analyze_market, market)
                        except Exception as e:
                            state.add_log("warn", f"Weather analysis failed for {market.id}: {e}")
                            continue
                        if not result:
                            continue
                            
                        # Stricter Tomorrow Rules
                        if result["confidence"] < 0.80 or abs(result["edge"]) < 0.12:
                            if result["should_trade"]: 
                                state.add_log("info", f"Skipping {market.id}: Resolves tomorrow. Conf={result['confidence']:.2f}, Edge={result['edge']:.2f} (Needs Conf>=0.80, Edge>=0.12)")
                            result["should_trade"] = False
                            result["direction"] = "HOLD"

                        state.researched_markets.add(market.id)

                        hours_left = (
                            (market.end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                            if market.end_date else 999
                        )
                        resolve_str = market.end_date.strftime("%b %d") if market.end_date else "unknown date"
                        mean_temp = result.get("ensemble_mean", 0.0)
                        threshold = result.get("threshold", 0.0)
                        direction_word = "above" if result.get("above", True) else "below"
                        state.add_log("info",
                            f"📊 {market.id}: resolves {resolve_str} ({hours_left:.0f}h). "
                            f"Model mean={mean_temp:.1f}°F vs threshold {direction_word} {threshold}°F. "
                            f"Prob={result.get('probability', 0):.0%}")

                        proposal = TradeProposal(
                            market_id=market.id,
                            market_question=market.question,
                            category="Weather",
                            direction=result["direction"] if result["should_trade"] else "HOLD",
                            estimated_prob=result["probability"],
                            market_price=result.get("market_price", 0.0),
                            edge=result["edge"],
                            confidence="HIGH" if result["confidence"] > 0.7 else "MEDIUM" if result["confidence"] > 0.4 else "LOW",
                                position_size_usd=settings.min_position_size, # Use configured minimum
                            reasoning=(
                                f"Ensemble: {result.get('member_count', 0)} members \u2192 "
                                f"{result.get('raw_probability', 0):.0%} raw \u2192 "
                                f"Final Prob={result.get('probability', 0):.0%}. "
                                f"({result.get('city', 'Unknown').title()}: {result.get('ensemble_mean', 0):.1f}\u00b0F vs {result.get('threshold', 0)}\u00b0F)"
                            ),
                            risk_factors=["Tomorrow Market (Stricter requirements applied)"],
                            entry_direction=result.get("direction", "HOLD")
                        )

                        executed, _, _ = await _process_trade_proposal(
                            adapter, proposal, market, positions, is_live,
                            "⛅", "WEATHER", "WEATHER SNIPE FIRED (TOMORROW)",
                        )
                        if executed:
                            phase_1_executed = True

                state.current_activity = "Weather ST: waiting for next tick..."
                await _wait_for_next_cycle()
                continue
            # === CLIMATE MODE (Paper + Live) ===
            elif getattr(state, "mode", "") in ("climate_paper", "climate_live"):
                is_climate_live = state.mode == "climate_live"
                agent = TradingAgent(risk_manager)
                
                state.current_activity = "Climate: fetching long-term climate markets..."
                raw_markets = await adapter.get_markets(
                    active_only=True, min_volume=0, category="Climate and Weather", limit=500
                )
                
                # Filter out the daily weather markets (KXHIGH, KXLOW, etc.) and unpredictable stuff
                climate_markets = []
                banned_prefixes = ("KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXWIND", "KXMVE")
                banned_words = ("earthquake", "volcano", "magnitude")
                
                for m in raw_markets:
                    question_lower = m.question.lower()
                    if any(m.id.upper().startswith(p) for p in banned_prefixes):
                        continue
                    if any(w in question_lower for w in banned_words):
                        continue
                    climate_markets.append(m)
                    
                state.markets_scanned = len(climate_markets)
                state.add_log("info", f"Climate ST: found {len(climate_markets)} general climate markets.")
                
                if not climate_markets:
                    await _wait_for_next_cycle()
                    continue
                    
                state.current_activity = "Climate: Agent scanning markets..."
                active_research_ids = await asyncio.to_thread(agent.scan_markets, climate_markets, state.mode)
                
                if active_research_ids:
                    state.research_active = True
                    state.current_research_targets = active_research_ids
                    state.add_log("info", f"Climate ST: Deep researching {len(active_research_ids)} markets...")
                    
                    async def run_climate_analysis(m_id):
                        m = next((mx for mx in climate_markets if mx.id == m_id), None)
                        if not m: return None
                        # Pass mode down to trigger climate data injection
                        return await asyncio.to_thread(
                            agent.research_and_analyze, m, positions, state.balance, mode=state.mode, depth="deep"
                        )
                        
                    analysis_results = await asyncio.gather(*(run_climate_analysis(mid) for mid in active_research_ids))
                    
                    state.research_active = False
                    state.current_research_targets = []
                    
                    for proposal in filter(None, analysis_results):
                        if not proposal: continue
                        market = next((mx for mx in climate_markets if mx.id == proposal.market_id), None)
                        if not market: continue
                        
                        await _process_trade_proposal(
                            adapter, proposal, market, positions, is_climate_live,
                            "🌍", "CLIMATE", "CLIMATE SNIPE FIRED",
                        )

                state.current_activity = "Climate ST: waiting for next tick..."
                await _wait_for_next_cycle()
                continue


            # === BITCOIN SNIPE MODE (Paper + Live) ===
            # (Weather modes `continue` above — only BTC modes reach here)
            is_btc_live = state.mode == "btc_live"
            state.current_activity = "Snipe BTC: scanning crypto markets..."

            # Fetch BTC markets from all known series
            markets = []
            btc_series = ["KXBTCD", "KXBTC", "KXBTCMAXW", "KXBTCMAXD", "BTCMINMAXY", "KXBTC15M", "KXBTCH"]
            for series in btc_series:
                series_markets = await adapter.get_markets(
                    active_only=True, min_volume=0, limit=50, series_ticker=series
                )
                markets.extend(series_markets)

            # Deduplicate by ID
            seen_ids = set()
            deduped = []
            for m in markets:
                if m.id not in seen_ids:
                    deduped.append(m)
                    seen_ids.add(m.id)
            markets = deduped

            # Filter to BTC-related only
            original_len = len(markets)
            markets = [m for m in markets if "btc" in m.id.lower() or "bitcoin" in m.question.lower() or "crypto" in m.category.lower()]
            state.markets_scanned = len(markets)
            state.add_log("info", f"Snipe BTC: Found {len(markets)} active BTC contracts across {len(btc_series)} series.")

            if not markets:
                state.add_log("info", "No BTC markets found. Waiting...")
                await _wait_for_next_cycle()
                continue

            # Periodically clear researched markets (every 4 hours)
            now = datetime.now(timezone.utc)
            if (now - state.last_research_clear) > timedelta(hours=4):
                state.researched_markets.clear()
                state.last_research_clear = now
                state.add_log("info", "Refreshing research memory for new cycle")

            # Filter out already-researched markets
            unresearched_markets = [m for m in markets if m.id not in state.researched_markets]
            if not unresearched_markets:
                state.add_log("info", "All BTC markets already researched. Waiting for fresh data...")
                await _wait_for_next_cycle()
                continue

            # Claude selects best opportunities
            state.current_activity = "Claude selecting BTC opportunities..."
            selected_ids = await asyncio.to_thread(agent.scan_markets, unresearched_markets, state.mode)
            state.add_log("info", f"Claude selected {len(selected_ids)} BTC markets to research")

            # Log skipped markets
            for m in unresearched_markets:
                if m.id not in selected_ids:
                    state.researched_markets.add(m.id)
                    skip_dict = {
                        "market_id": m.id, "question": m.question, "category": m.category,
                        "direction": "SKIPPED", "estimated_prob": 0.0, "market_price": m.yes_price,
                        "edge": 0.0, "r_score": 0.0, "confidence": "NONE", "size_usd": 0.0,
                        "reasoning": "Rejected by Claude quick scan: No obvious mispricing.",
                        "risk_factors": [], "entry_direction": None, "target_entry_price": 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "approved": False, "rejection_reasons": ["Filtered by Quick Scan"]
                    }
                    state.proposals.insert(0, skip_dict)
            state.proposals = state.proposals[:150]

            # Phase-based scanning: prioritize Today (<=16h) before Tomorrow (>16h)
            phase_1_ids, phase_2_ids = [], []
            now = datetime.now(timezone.utc)
            for mid in selected_ids:
                m = next((mx for mx in unresearched_markets if mx.id == mid), None)
                if m and m.end_date:
                    hours_out = (m.end_date - now).total_seconds() / 3600
                    if hours_out <= 16:
                        phase_1_ids.append(mid)
                    else:
                        phase_2_ids.append(mid)

            if phase_1_ids:
                state.add_log("info", f"BTC Phase 1: Prioritizing {len(phase_1_ids)} markets resolving TODAY.")
                active_research_ids = phase_1_ids
            else:
                state.add_log("info", f"BTC Phase 1 empty. Phase 2: {len(phase_2_ids)} markets resolving TOMORROW.")
                active_research_ids = phase_2_ids

            if not active_research_ids:
                state.add_log("info", "No BTC markets in active phase queue.")
                await _wait_for_next_cycle()
                continue

            # Research selected markets in parallel
            state.proposals = []
            state.current_activity = f"Deep researching {len(active_research_ids)} BTC markets..."
            state.research_active = True
            state.research_total = len(active_research_ids)
            state.research_completed = 0
            state.research_steps_total = len(active_research_ids) * 10
            state.research_steps_completed = 0
            state.status_message = "Analyzing BTC Edges..."

            state.current_research_targets = []
            for mid in active_research_ids:
                m = next((m for m in markets if m.id == mid), None)
                if m:
                    state.current_research_targets.append({"id": m.id, "question": m.question, "category": m.category})

            async def run_btc_analysis(m_id):
                try:
                    market = next((m for m in markets if m.id == m_id), None)
                    if not market: return None
                    state.add_log("info", f"Researching: {market.question[:80]}")
                    state.researched_markets.add(m_id)

                    def log_cb(msg):
                        state.add_log("info", msg)
                        if state.research_steps_completed < state.research_steps_total * 0.95:
                            state.research_steps_completed += 1

                    return await asyncio.to_thread(
                        agent.research_and_analyze, market, positions, state.balance,
                        state.mode, "deep", log_cb
                    )
                finally:
                    state.research_completed += 1
                    state.research_steps_completed += 10

            analysis_results = await asyncio.gather(*(run_btc_analysis(mid) for mid in active_research_ids))

            state.research_active = False
            state.current_research_targets = []
            state.status_message = "Online"

            # Process proposals and execute trades
            for proposal in filter(None, analysis_results):
                if not state.is_running:
                    state.add_log("warn", "Bot stopped during research. Aborting.")
                    break

                market = next((m for m in markets if m.id == proposal.market_id), None)
                if not market: continue

                await _process_trade_proposal(
                    adapter, proposal, market, positions, is_btc_live,
                    "🎯", "BTC", "BTC SNIPE FIRED",
                )

                logger.log_analysis(
                    market_id=proposal.market_id, question=proposal.market_question,
                    market_price=proposal.market_price, estimated_prob=proposal.estimated_prob,
                    edge=proposal.edge, direction=proposal.direction, confidence=proposal.confidence,
                    reasoning=proposal.reasoning,
                    is_deep_research=getattr(proposal, "is_deep_research", False),
                )

            # Run Postmortem Analysis every cycle
            state.current_activity = "Running trade postmortems..."
            await postmortem_engine.run_analysis(adapter)

            # Refresh macro-lessons for the UI
            state.macro_lessons = memory_manager.get_macro_lessons_raw()

            state.add_log("info", "Bot loop cycle complete")
            state.current_activity = "Cycle complete — waiting"
            await _wait_for_next_cycle()

        except asyncio.CancelledError:
            state.add_log("info", "Bot stopped")
            break
        except Exception as e:
            state.add_log("error", f"Cycle error: {str(e)[:200]}")  # type: ignore
            state.current_activity = f"Error — retrying in 60s"
            await asyncio.sleep(60)

    state.is_running = False
    state.current_activity = "Stopped"


async def _wait_for_next_cycle():
    """Wait for the next cycle, checking for stop/pause every second."""
    wait_seconds = int(settings.cycle_interval_minutes * 60)
    if getattr(state, "mode", "") in ("btc_paper", "btc_live"):
        wait_seconds = 60  # 1 minute for BTC sniping
    elif getattr(state, "mode", "") in ("weather_paper", "weather_live"):
        wait_seconds = 180  # 3 minutes for weather
    elif getattr(state, "mode", "") in ("climate_paper", "climate_live"):
        wait_seconds = 1800 # 30 minutes for general climate
        
    next_time = datetime.now(timezone.utc).timestamp() + wait_seconds
    state.next_cycle_time = datetime.fromtimestamp(next_time, tz=timezone.utc).isoformat()

    for _ in range(wait_seconds):
        if not state.is_running:
            break
        await asyncio.sleep(1)
