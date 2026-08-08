"""
Regression tests for the scoreboard-integrity fixes.

The paper-validation gate is only as honest as the numbers feeding it:

1. Kalshi trading fees are in the decision gate and every P&L path.
2. Resolved markets that 404 still settle via the event-endpoint fallback.
3. An accepted-but-unfilled live order is not counted as a trade.
4. client_order_id is stable across retries of the same trade thesis.
5. Circuit-breaker state survives a restart.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from adapters.base import Order, OrderStatus, Position, Side
from adapters.kalshi_adapter import KalshiAdapter
from core.market_pricing import fee_fraction_per_contract, kalshi_trading_fee
from core.risk_manager import RiskManager, TradeProposal


# ---------------------------------------------------------------------------
# 1. Fee math and fee-aware decision gate
# ---------------------------------------------------------------------------

def test_kalshi_trading_fee_matches_published_formula():
    # 0.07 × C × P × (1 − P), rounded up to the next cent
    assert kalshi_trading_fee(100, 0.50) == 1.75
    assert kalshi_trading_fee(10, 0.30) == 0.15   # 14.7¢ → 15¢
    assert kalshi_trading_fee(1, 0.50) == 0.02    # 1.75¢ → 2¢
    assert kalshi_trading_fee(100, 0.50, maker=True) == 0.44  # 43.75¢ → 44¢
    assert kalshi_trading_fee(0, 0.50) == 0.0
    assert kalshi_trading_fee(10, 0.0) == 0.0
    assert kalshi_trading_fee(10, 1.0) == 0.0


def test_fee_fraction_is_continuous_form_of_fee():
    assert fee_fraction_per_contract(0.50) == pytest.approx(0.0175)
    assert fee_fraction_per_contract(0.10) == pytest.approx(0.0063)
    assert fee_fraction_per_contract(0.50, maker=True) == pytest.approx(0.004375)
    assert fee_fraction_per_contract(0.0) == 0.0


def _weather_proposal(estimated_prob: float) -> TradeProposal:
    return TradeProposal(
        market_id="KXHIGHNY-TEST-B90",
        market_question="Will the NYC high temperature be above 90?",
        category="Weather",
        direction="BUY_YES",
        estimated_prob=estimated_prob,
        market_price=0.50,
        edge=estimated_prob - 0.50,
        confidence="VERY_HIGH",
        position_size_usd=5.0,
        reasoning="test",
        execution_metrics={
            "side_cost": 0.50,
            "market_side_probability": 0.50,
            "side_spread": 0.01,
            "side_depth_usd": 100.0,
            "side_depth_contracts": 200,
            "top_level_covers_quantity": True,
        },
    )


def test_edge_gate_subtracts_entry_fee_from_probability_gap():
    rm = RiskManager()

    # Raw gap 13% clears the 12% weather bar, but not after the 1.75%
    # entry fee at a 50¢ side cost — the gate must reject.
    marginal = _weather_proposal(estimated_prob=0.63)
    _, reasons = rm.check_trade(marginal, [], balance=100.0, portfolio_value=100.0, paper_mode=True)
    assert any("Fee-adjusted probability gap" in r for r in reasons)
    assert marginal.execution_metrics["entry_fee_fraction"] == pytest.approx(0.0175)

    # Raw gap 15% still clears 12% after fees — no fee-gap failure.
    healthy = _weather_proposal(estimated_prob=0.65)
    _, reasons = rm.check_trade(healthy, [], balance=100.0, portfolio_value=100.0, paper_mode=True)
    assert not any("Fee-adjusted probability gap" in r for r in reasons)


# ---------------------------------------------------------------------------
# Fee-aware P&L paths
# ---------------------------------------------------------------------------

def _position(side: Side, quantity: float, avg_price: float, current_price: float | None = None) -> Position:
    return Position(
        market_id="KXHIGHNY-TEST-B90",
        market_question="Will the NYC high temperature be above 90?",
        side=side,
        quantity=quantity,
        avg_price=avg_price,
        current_price=current_price if current_price is not None else avg_price,
        unrealized_pnl=0.0,
        platform="kalshi",
        category="Weather",
        entry_prob=0.60,
    )


def test_settlement_pnl_deducts_entry_fee():
    rm = RiskManager()
    pos = _position(Side.YES, quantity=10, avg_price=0.30)

    win_pnl, won = rm.realized_pnl_on_settlement(pos, "yes")
    loss_pnl, lost = rm.realized_pnl_on_settlement(pos, "no")

    # Payout 10 − cost 3 − entry fee 0.15
    assert won is True
    assert win_pnl == pytest.approx(6.85)
    # Loss also carries the entry fee: −3 − 0.15
    assert lost is False
    assert loss_pnl == pytest.approx(-3.15)


def test_take_profit_exit_requires_gross_gain_to_clear_round_trip_fees():
    rm = RiskManager()

    # +20% ROI on a tiny position: 2¢ gross gain, 2¢ of round-trip fees —
    # a "take profit" that nets zero must not fire.
    fee_eaten = _position(Side.YES, quantity=1, avg_price=0.10, current_price=0.12)
    assert rm.evaluate_exits([fee_eaten]) == []

    # +30% ROI on real size clears fees comfortably and still exits.
    profitable = _position(Side.YES, quantity=100, avg_price=0.50, current_price=0.65)
    exits = rm.evaluate_exits([profitable])
    assert len(exits) == 1
    assert "after" in exits[0][1] and "fees" in exits[0][1]


# ---------------------------------------------------------------------------
# 2. Settlement survives the 404 on expired/settled markets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_market_result_falls_back_to_event_endpoint_on_404(monkeypatch):
    adapter = KalshiAdapter()
    calls = []

    async def fake_request(method, path, silent=False, **kwargs):
        calls.append(path)
        if path.startswith("/markets/"):
            return {"error": "market not found", "status_code": 404}
        if path == "/events/KXHIGHNY-25AUG08":
            return {"markets": [
                {"ticker": "KXHIGHNY-25AUG08-B90", "result": "yes"},
                {"ticker": "KXHIGHNY-25AUG08-B95", "result": "no"},
            ]}
        return {}

    monkeypatch.setattr(adapter, "_request", fake_request)
    result = await adapter.get_market_result("KXHIGHNY-25AUG08-B90")
    assert result == "yes"
    assert calls == ["/markets/KXHIGHNY-25AUG08-B90", "/events/KXHIGHNY-25AUG08"]


@pytest.mark.asyncio
async def test_get_market_result_treats_transient_errors_as_unknown(monkeypatch):
    adapter = KalshiAdapter()
    calls = []

    async def fake_request(method, path, silent=False, **kwargs):
        calls.append(path)
        return {"error": "rate limited", "status_code": 429}

    monkeypatch.setattr(adapter, "_request", fake_request)
    # A transient failure must NOT be mistaken for "market gone" — no event
    # fallback, no result.
    assert await adapter.get_market_result("KXHIGHNY-25AUG08-B90") is None
    assert calls == ["/markets/KXHIGHNY-25AUG08-B90"]


@pytest.mark.asyncio
async def test_paper_settlement_books_pnl_for_404_resolved_market(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import core.trading_loop as tl

    rm = RiskManager()
    rm.save_paper_positions([_position(Side.YES, quantity=10, avg_price=0.30)])

    logs: list = []
    resolutions: list = []
    state = SimpleNamespace(
        balance=100.0,
        resolved_since_last_reflection=0,
        add_log=lambda level, msg: logs.append((level, msg)),
    )
    logger = SimpleNamespace(log_resolution=lambda **kw: resolutions.append(kw))

    async def noop(*args, **kwargs):
        return None

    tl.init(state, rm, logger, None, lambda: None, noop)
    monkeypatch.setattr(tl, "send_telegram_alert", noop)

    adapter = SimpleNamespace(
        get_market=noop,                 # market 404s → None
        get_market_result=lambda market_id: _resolved("yes"),
    )
    agent = SimpleNamespace(generate_lesson=noop)

    still_open = await tl._refresh_and_settle_paper_positions(adapter, agent)
    await asyncio.sleep(0)  # let fire-and-forget alert/lesson tasks run

    assert still_open == []
    assert rm.load_paper_positions() == []
    assert len(resolutions) == 1
    assert resolutions[0]["outcome"] == "YES"
    assert resolutions[0]["pnl"] == pytest.approx(6.85)
    # Balance credits payout minus the entry fee: 100 + 10 − 0.15
    assert state.balance == pytest.approx(109.85)
    assert rm.daily_stats.total_pnl == pytest.approx(6.85)


async def _resolved(result: str) -> str:
    return result


# ---------------------------------------------------------------------------
# 3. Only actual fills count as trades
# ---------------------------------------------------------------------------

def _order(status: OrderStatus, filled_quantity: float) -> Order:
    return Order(
        id="ord-1",
        market_id="KXHIGHNY-TEST-B90",
        side=Side.YES,
        price=0.50,
        quantity=10,
        status=status,
        filled_quantity=filled_quantity,
        created_at=datetime.now(timezone.utc),
        platform="kalshi",
    )


def test_effective_filled_counts_only_real_fills():
    import core.trading_loop as tl

    # A resting (accepted, unfilled) order is not a trade.
    assert tl._effective_filled(_order(OrderStatus.PENDING, 0), 10) == 0.0
    # Partial fills count for exactly the filled contracts.
    assert tl._effective_filled(_order(OrderStatus.PARTIALLY_FILLED, 3), 10) == 3.0
    # A definitive FILLED status without a count means fully filled.
    assert tl._effective_filled(_order(OrderStatus.FILLED, 0), 10) == 10.0
    # The fill can never exceed what was requested, and no order means no fill.
    assert tl._effective_filled(_order(OrderStatus.FILLED, 25), 10) == 10.0
    assert tl._effective_filled(None, 10) == 0.0


# ---------------------------------------------------------------------------
# 4. Idempotency key is stable across retries of the same thesis
# ---------------------------------------------------------------------------

def test_client_order_id_is_stable_for_same_thesis_and_distinct_otherwise():
    import core.trading_loop as tl

    first = tl._stable_client_order_id("KXHIGHNY-TEST-B90", "BUY_YES", 0.50)
    retry = tl._stable_client_order_id("KXHIGHNY-TEST-B90", "BUY_YES", 0.50)
    assert first == retry  # a retried thesis re-derives the SAME id → 409 dedup works

    assert first != tl._stable_client_order_id("KXHIGHNY-TEST-B90", "BUY_NO", 0.50)
    assert first != tl._stable_client_order_id("KXHIGHNY-TEST-B95", "BUY_YES", 0.50)
    assert first != tl._stable_client_order_id("KXHIGHNY-TEST-B90", "BUY_YES", 0.51)


# ---------------------------------------------------------------------------
# 5. Circuit breakers survive restarts
# ---------------------------------------------------------------------------

def test_circuit_breaker_state_survives_restart(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    rm = RiskManager()
    rm.record_trade_result(-4.0)
    rm.record_trade_result(-3.0)
    rm.daily_stats.is_halted = True
    rm.daily_stats.halt_reason = "5 consecutive losses"
    rm._persist_daily_state()

    # A restart (fresh instance) must reload today's halt and counters.
    reborn = RiskManager()
    assert reborn.daily_stats.is_halted is True
    assert reborn.daily_stats.halt_reason == "5 consecutive losses"
    assert reborn.daily_stats.total_pnl == pytest.approx(-7.0)
    assert reborn.daily_stats.consecutive_losses == 2


def test_stale_daily_state_is_discarded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    rm = RiskManager()
    rm.daily_stats.date = "2000-01-01"
    rm.daily_stats.is_halted = True
    rm.daily_stats.halt_reason = "ancient history"
    rm._persist_daily_state()

    fresh = RiskManager()
    assert fresh.daily_stats.is_halted is False
    assert fresh.daily_stats.date != "2000-01-01"
