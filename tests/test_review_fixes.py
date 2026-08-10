from adapters.base import Position, Side
from config.settings import settings
from core.risk_manager import RiskManager, TradeProposal
from core.trade_utils import (
    direction_to_side_and_price,
    infer_entry_direction,
    is_executable_direction,
)


def make_proposal(**overrides) -> TradeProposal:
    base = {
        "market_id": "m-2",
        "market_question": "Will this resolve yes?",
        "category": "Science",
        "direction": "BUY_YES",
        "estimated_prob": 0.85,
        "market_price": 0.66,
        "edge": 0.19,
        "confidence": "HIGH",
        "position_size_usd": 10.0,
        "reasoning": "Test proposal",
        "target_entry_price": 0.0,
        "risk_factors": [],
        "r_score": 0.6,
        "entry_direction": "BUY_YES",
        "execution_metrics": {
            "side_spread": 0.02,
            "side_depth_usd": 100.0,
        },
    }
    base.update(overrides)
    return TradeProposal(**base)


def test_wait_for_entry_never_maps_to_direct_execution():
    assert not is_executable_direction("WAIT_FOR_ENTRY")
    assert infer_entry_direction("WAIT_FOR_ENTRY", 0.62, 0.48) == "BUY_YES"
    assert infer_entry_direction("WAIT_FOR_ENTRY", 0.38, 0.52) == "BUY_NO"
    assert direction_to_side_and_price("BUY_YES", 0.48)[0] == Side.YES
    assert direction_to_side_and_price("BUY_NO", 0.48)[0] == Side.NO


def test_profitable_day_does_not_trip_daily_loss_breaker(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_loss", 50.0)
    monkeypatch.setattr(settings, "max_single_trade", 25.0)
    rm = RiskManager()
    rm.daily_stats.total_pnl = 75.0

    approved, reasons = rm.check_trade(
        make_proposal(position_size_usd=5.0),
        [],
        100.0,
        100.0,
    )

    assert approved is True
    assert not any("Daily loss" in reason for reason in reasons)


def test_r_score_handles_yes_no_and_hold():
    rm = RiskManager()

    assert abs(rm.calculate_r_score(0.8, 0.7, "BUY_YES") - 0.25) < 1e-6
    assert abs(rm.calculate_r_score(0.2, 0.3, "BUY_NO") - 0.25) < 1e-6
    assert rm.calculate_r_score(0.8, 0.7, "HOLD") == 0.0


def test_variance_normalized_edge_is_not_mislabeled_as_execution_z_score():
    rm = RiskManager()
    proposal = make_proposal(r_score=0.01)

    approved, reasons = rm.check_trade(proposal, [], 100.0, 100.0, paper_mode=True)

    assert approved is True, reasons
    assert not any("Z-Score" in reason for reason in reasons)


def test_side_cost_for_buy_no_uses_no_contract_cost():
    rm = RiskManager()

    assert rm.get_side_cost(make_proposal(direction="BUY_YES", market_price=0.22)) == 0.22
    assert rm.get_side_cost(make_proposal(direction="BUY_NO", market_price=0.22)) == 0.78
    assert rm.get_side_cost(make_proposal(execution_metrics={"side_cost": 0.31})) == 0.31


def test_probability_gap_uses_market_probability_and_confidence_multiplier():
    rm = RiskManager()
    proposal = make_proposal(
        direction="BUY_YES",
        estimated_prob=0.60,
        market_price=0.50,
        confidence="MEDIUM",
        execution_metrics={"side_cost": 0.52, "market_side_probability": 0.52},
    )

    assert abs(rm.calculate_probability_gap(proposal) - 0.08) < 1e-9
    assert abs(rm.calculate_confidence_adjusted_probability_gap(proposal) - 0.04) < 1e-9
    assert proposal.execution_metrics["confidence_multiplier"] == 0.5


def test_probability_gap_for_buy_no_uses_no_and_market_probabilities():
    rm = RiskManager()
    proposal = make_proposal(
        direction="BUY_NO",
        estimated_prob=0.30,
        market_price=0.36,
        confidence="HIGH",
        execution_metrics={"side_cost": 0.62, "market_side_probability": 0.62},
    )

    assert abs(rm.get_side_probability(proposal) - 0.70) < 1e-9
    assert abs(rm.calculate_probability_gap(proposal) - 0.08) < 1e-9


def test_longshot_is_not_blocked_by_default_probability_floor(monkeypatch):
    monkeypatch.setattr(settings, "min_payout_likelihood", 0.0)
    monkeypatch.setattr(settings, "min_edge_threshold", 0.03)
    rm = RiskManager()
    proposal = make_proposal(
        estimated_prob=0.13,
        market_price=0.08,
        edge=0.05,
        confidence="VERY_HIGH",
        position_size_usd=5.0,
        r_score=0.5,
        post_only=True,
        execution_metrics={"side_cost": 0.08, "side_spread": 0.01, "side_depth_usd": 100.0},
    )

    approved, reasons = rm.check_trade(
        proposal,
        [],
        balance=100.0,
        portfolio_value=100.0,
        paper_mode=True,
    )

    assert approved is True
    assert not any("Payout likelihood" in reason for reason in reasons)


def test_orderbook_metrics_derive_side_spread_and_depth():
    rm = RiskManager()
    proposal = make_proposal(direction="BUY_YES", market_price=0.66)

    metrics = rm.attach_orderbook_metrics(
        proposal,
        {
            "yes": [[62, 80]],
            "no": [[28, 100]],
        },
    )

    assert metrics["yes_bid"] == 0.62
    assert metrics["yes_ask"] == 0.72
    assert abs(metrics["side_spread"] - 0.10) < 1e-6
    assert abs(metrics["side_depth_usd"] - 72.0) < 1e-6


def test_orderbook_metrics_use_top_quote_and_flag_missing_depth():
    rm = RiskManager()
    proposal = make_proposal(
        direction="BUY_YES",
        market_price=0.50,
        position_size_usd=10.0,
    )

    metrics = rm.attach_orderbook_metrics(
        proposal,
        {
            "yes": [[45, 100]],
            "no": [[50, 5], [40, 5]],
        },
    )

    assert metrics["top_of_book_side_cost"] == 0.50
    assert metrics["requested_contracts"] == 20
    assert metrics["top_level_covers_quantity"] is False
    assert metrics["side_cost"] == 0.50
    assert metrics["estimated_total_cost"] == 10.0
    assert metrics["estimated_total_cost"] <= proposal.position_size_usd


def test_probability_gap_gate_does_not_add_fee_math(monkeypatch):
    monkeypatch.setattr(settings, "min_edge_threshold", 0.08)
    rm = RiskManager()
    proposal = make_proposal(
        estimated_prob=0.70,
        market_price=0.50,
        confidence="VERY_HIGH",
        position_size_usd=10.0,
        execution_metrics={
            "side_cost": 0.50,
            "side_spread": 0.01,
            "side_depth_usd": 100.0,
            "requested_contracts": 20,
            "top_level_covers_quantity": True,
        },
    )

    approved, reasons = rm.check_trade(proposal, [], 100.0, 100.0, paper_mode=True)

    assert approved is True, reasons
    assert proposal.execution_metrics["required_probability_gap"] == 0.08
    assert "estimated_fee" not in proposal.execution_metrics
    assert "fee_adjusted_edge" not in proposal.execution_metrics


def test_executable_trade_requires_orderbook_metrics():
    rm = RiskManager()

    approved, reasons = rm.check_trade(
        make_proposal(execution_metrics={}),
        [],
        100.0,
        100.0,
    )

    assert approved is False
    assert any("Missing orderbook metrics" in reason for reason in reasons)


def test_wait_for_entry_paper_trade_records_entry_side(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rm = RiskManager()
    proposal = make_proposal(
        direction="WAIT_FOR_ENTRY",
        entry_direction="BUY_NO",
        market_price=0.2,
        position_size_usd=8.0,
    )

    rm.record_paper_trade(proposal)
    positions = rm.load_paper_positions()

    assert len(positions) == 1
    assert positions[0].side == Side.NO
    assert positions[0].avg_price == 0.8
    assert positions[0].quantity == 10.0


def test_settlement_pnl_scores_yes_and_no_positions_correctly():
    rm = RiskManager()
    yes_position = Position(
        market_id="wx-yes",
        market_question="Will NYC high temp be above 80?",
        side=Side.YES,
        quantity=10,
        avg_price=0.30,
        current_price=0.30,
        unrealized_pnl=0.0,
        platform="kalshi",
        category="Weather",
    )
    no_position = Position(
        market_id="wx-no",
        market_question="Will Chicago low temp be below 32?",
        side=Side.NO,
        quantity=10,
        avg_price=0.70,
        current_price=0.70,
        unrealized_pnl=0.0,
        platform="kalshi",
        category="Weather",
    )

    yes_pnl, yes_won = rm.realized_pnl_on_settlement(yes_position, "YES")
    yes_loss, yes_lost = rm.realized_pnl_on_settlement(yes_position, "NO")
    no_pnl, no_won = rm.realized_pnl_on_settlement(no_position, "NO")
    no_loss, no_lost = rm.realized_pnl_on_settlement(no_position, "YES")

    # Realized P&L is net of the entry trading fee:
    # ceil(0.07 × 10 × 0.30 × 0.70) = ceil(0.07 × 10 × 0.70 × 0.30) = $0.15
    assert yes_won is True
    assert yes_pnl == 6.85
    assert yes_lost is False
    assert yes_loss == -3.15
    assert no_won is True
    assert no_pnl == 2.85
    assert no_lost is False
    assert no_loss == -7.15


def test_category_exposure_cap_uses_position_categories(monkeypatch):
    monkeypatch.setattr(settings, "max_category_exposure_pct", 0.35)
    rm = RiskManager()
    positions = [
        Position(
            market_id="m-1",
            market_question="Existing sports trade",
            side=Side.YES,
            quantity=100,
            avg_price=0.5,
            current_price=0.55,
            unrealized_pnl=5.0,
            platform="kalshi",
            category="Sports",
        )
    ]

    approved, reasons = rm.check_trade(
        make_proposal(category="Sports"),
        positions,
        100.0,
        100.0,
    )

    assert approved is False
    assert any("Category cap" in reason for reason in reasons)


def test_trade_risk_snapshot_uses_checked_trade_numbers(monkeypatch):
    monkeypatch.setattr(settings, "max_single_trade", 25.0)
    rm = RiskManager()
    proposal = make_proposal(
        position_size_usd=12.0,
        market_price=0.66,
        execution_metrics={
            "side_cost": 0.25,
            "side_spread": 0.02,
            "side_depth_usd": 100.0,
        },
    )

    approved, reasons = rm.check_trade(
        proposal,
        [],
        balance=100.0,
        portfolio_value=100.0,
        paper_mode=True,
    )
    rm.record_trade_entry(proposal.position_size_usd)

    snapshot = rm.build_trade_risk_snapshot(
        proposal,
        [],
        balance=100.0,
        portfolio_value=100.0,
        approved=approved,
        reasons=reasons,
        paper_mode=True,
        executed=True,
    )

    assert snapshot["label"] == "Paper Safe"
    assert snapshot["approved"] is True
    assert snapshot["balance_after"] == 88.0
    assert snapshot["side_cost"] == 0.25
    assert snapshot["contracts"] == 48.0
    assert snapshot["trades_today"] == 1
    assert any(check["name"] == "Probability gap" for check in snapshot["checks"])


def test_trade_risk_snapshot_blocks_execution_guard_failures():
    rm = RiskManager()
    proposal = make_proposal(position_size_usd=500.0)

    snapshot = rm.build_trade_risk_snapshot(
        proposal,
        [],
        balance=50.0,
        portfolio_value=50.0,
        approved=True,
        reasons=[],
        paper_mode=True,
        executed=False,
        execution_blockers=["Insufficient paper balance for Weather trade ($500.00 > $50.00)."],
    )

    assert snapshot["status"] == "blocked"
    assert snapshot["approved"] is False
    assert "Insufficient paper balance" in snapshot["failures"][0]


def test_polymarket_live_mode_fails_validation(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "platform", "polymarket")
    monkeypatch.setattr(settings, "trading_mode", "live")

    errors = settings.validate()

    assert any("Polymarket live trading is not implemented" in error for error in errors)
