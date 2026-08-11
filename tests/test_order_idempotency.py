import json
from datetime import datetime, timezone

import pytest

import server
from adapters.base import Side, Transfer
from adapters.kalshi_adapter import KalshiAdapter
from config.settings import settings
from core.logger import TradeLogger


@pytest.mark.asyncio
async def test_kalshi_duplicate_client_order_id_reconciles_existing_order(monkeypatch):
    adapter = KalshiAdapter()
    calls = []

    async def fake_request(method, path, params=None, data=None, silent=False):
        calls.append((method, path, params, data, silent))
        if method == "POST":
            return {"error": "duplicate client_order_id", "status_code": 409}
        if method == "GET" and path == "/portfolio/orders":
            return {
                "orders": [
                    {
                        "order_id": "existing-order",
                        "client_order_id": "cid-123",
                        "ticker": "KXTEST",
                        "side": "yes",
                        "yes_price": 42,
                        "initial_count": 7,
                        "status": "resting",
                    }
                ]
            }
        return None

    monkeypatch.setattr(adapter, "_request", fake_request)

    result = await adapter.place_order(
        "KXTEST",
        Side.YES,
        0.42,
        7,
        client_order_id="cid-123",
    )

    assert result.success is True
    assert result.order is not None
    assert result.order.id == "existing-order"
    assert result.order.price == pytest.approx(0.42)
    assert result.order.quantity == 7
    assert calls[1][2] == {"ticker": "KXTEST", "limit": 1000}

    await adapter.client.aclose()


def test_log_order_attempt_records_idempotency_fields(tmp_path):
    log_path = tmp_path / "trades.jsonl"
    logger = TradeLogger(str(log_path))

    logger.log_order_attempt(
        client_order_id="cid-123",
        market_id="KXTEST",
        side="yes",
        price=0.42,
        quantity=7,
        status="accepted",
        order_id="order-1",
        extra={"post_only": False, "direction": "BUY_YES"},
    )

    row = json.loads(log_path.read_text().strip())

    assert row["type"] == "order_attempt"
    assert row["client_order_id"] == "cid-123"
    assert row["market_id"] == "KXTEST"
    assert row["status"] == "accepted"
    assert row["order_id"] == "order-1"
    assert row["post_only"] is False
    assert row["direction"] == "BUY_YES"


@pytest.mark.asyncio
async def test_kalshi_get_transfers_reads_paginated_deposits_and_withdrawals(monkeypatch):
    adapter = KalshiAdapter()
    calls = []

    async def fake_request(method, path, params=None, data=None, silent=False):
        calls.append((method, path, params, data, silent))
        if path == "/portfolio/deposits" and not params.get("cursor"):
            return {
                "deposits": [
                    {"id": "dep-1", "amount_cents": 12500, "status": "applied", "created_ts": 1_710_000_000}
                ],
                "cursor": "next-page",
            }
        if path == "/portfolio/deposits" and params.get("cursor") == "next-page":
            return {
                "deposits": [
                    {"id": "dep-2", "amount_cents": 5000, "status": "failed", "created_ts": 1_710_000_100}
                ]
            }
        if path == "/portfolio/withdrawals":
            return {
                "withdrawals": [
                    {"id": "wth-1", "amount_cents": 2500, "status": "applied", "created_ts": 1_710_000_200}
                ]
            }
        return None

    monkeypatch.setattr(adapter, "_request", fake_request)

    transfers = await adapter.get_transfers()

    assert [(t.id, t.type, t.amount, t.status) for t in transfers] == [
        ("dep-1", "deposit", 125.0, "applied"),
        ("dep-2", "deposit", 50.0, "failed"),
        ("wth-1", "withdrawal", 25.0, "applied"),
    ]
    assert calls[0][2] == {"limit": 500}
    assert calls[1][2] == {"limit": 500, "cursor": "next-page"}
    assert all(call[4] is True for call in calls)

    await adapter.client.aclose()


@pytest.mark.asyncio
async def test_refresh_state_counts_transfers_without_paper_bankroll(monkeypatch):
    fresh_state = server.BotState()
    fresh_state.mode = "weather_paper"
    monkeypatch.setattr(server, "state", fresh_state)
    monkeypatch.setattr(settings, "paper_balance", 1000.0)

    class FakeAdapter:
        async def get_balance(self):
            return 432.10

        async def get_portfolio_value(self):
            return 500.00

        async def get_transfers(self):
            created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            return [
                Transfer("dep-1", "deposit", 100.0, "applied", created_at, "kalshi"),
                Transfer("dep-2", "deposit", 50.0, "failed", created_at, "kalshi"),
                Transfer("wth-1", "withdrawal", 25.0, "applied", created_at, "kalshi"),
                Transfer("wth-2", "withdrawal", 7.0, "returned", created_at, "kalshi"),
            ]

    await server.refresh_state_from_platform(FakeAdapter())

    assert fresh_state.live_balance == 432.10
    assert fresh_state.live_portfolio_value == 500.00
    assert fresh_state.balance == 1000.0
    assert fresh_state.portfolio_value == 1000.0
    assert fresh_state.total_deposited == 100.0
    assert fresh_state.total_withdrawn == 25.0


@pytest.mark.asyncio
async def test_paper_bankroll_changes_do_not_overwrite_transfer_totals(monkeypatch):
    fresh_state = server.BotState()
    fresh_state.mode = "weather_paper"
    fresh_state.total_deposited = 321.0
    fresh_state.total_withdrawn = 45.0
    monkeypatch.setattr(server, "state", fresh_state)
    monkeypatch.setattr(server, "bot_task", None)
    monkeypatch.setattr(settings, "paper_balance", 1000.0)

    async def fake_trading_loop():
        return None

    monkeypatch.setattr(server, "trading_loop", fake_trading_loop)

    await server.update_settings({"paper_balance": 2000.0}, _=None)
    start_response = await server.start_bot(mode="weather_paper", paper_balance=1500.0, _=None)
    if server.bot_task:
        await server.bot_task

    assert start_response["status"] == "started"
    assert fresh_state.balance == 1500.0
    assert fresh_state.initial_balance == 1500.0
    assert fresh_state.total_deposited == 321.0
    assert fresh_state.total_withdrawn == 45.0
