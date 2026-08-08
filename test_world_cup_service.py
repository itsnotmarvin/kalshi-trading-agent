from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from adapters.base import Market
from core.world_cup_service import (
    CALIBRATION_UNAVAILABLE,
    LIVE_UNAVAILABLE,
    MAPPING_UNAVAILABLE,
    MODEL_INPUTS_UNAVAILABLE,
    NO_MATCHES_TODAY,
    NO_SCHEDULE,
    PROVIDER_UNAVAILABLE,
    QUOTE_STALE,
    SLIP_STRATEGY_BIG_PAYOUT,
    SLIP_STRATEGY_PLAYER_PROPS,
    SCOPE_TODAY_ONLY,
    SCOPE_TODAY_OR_NEXT_MATCHDAY,
    TRUE_COMBO_UNAVAILABLE,
    Match,
    WorldCupService,
    redacted_environment_status,
)
from config.settings import settings


class FakeSchedule:
    def __init__(self, matches=None, error=None):
        self.matches = matches or []
        self.error = error

    async def get_matches_for_date(self, day: date, fetched_at=None):
        return self.matches, self.error


class DateSchedule:
    def __init__(self, matches_by_day=None, error=None):
        self.matches_by_day = matches_by_day or {}
        self.error = error
        self.requested_days = []

    async def get_matches_for_date(self, day: date, fetched_at=None):
        self.requested_days.append(day)
        return self.matches_by_day.get(day, []), self.error


class FakeAdapter:
    def __init__(self, markets=None, orderbook=None, fail=False):
        self.markets = markets or []
        self.orderbook = orderbook or {"yes": [["0.54", "10"]], "no": [["0.44", "10"]]}
        self.fail = fail

    async def get_world_cup_candidate_markets(self, limit=1000):
        if self.fail:
            raise RuntimeError("kalshi unavailable")
        return self.markets

    async def get_orderbook(self, market_id):
        if self.fail:
            raise RuntimeError("kalshi unavailable")
        if isinstance(self.orderbook, dict) and market_id in self.orderbook:
            return self.orderbook[market_id]
        return self.orderbook


class FakePlayerHistory:
    async def get_player_context(self, player_name, fetched_at=None):
        return {
            "status": "available",
            "requested_name": player_name,
            "player_name": player_name,
            "team": "Test FC",
            "national_team": "Brazil",
            "nationality": "Brazil",
            "position": "Forward",
            "status_text": "Active",
            "history_excerpt": f"{player_name} profile history from a source.",
            "performance_stats_status": "recent match logs unavailable from configured public source",
            "probability_use": "Profile/history is displayed for research context only.",
            "links": [{"label": "Stats", "url": "https://example.com/stats"}],
            "sources": [{
                "title": "Fake player source",
                "url": "https://example.com/player",
                "timestamp": fetched_at.isoformat() if fetched_at else None,
                "status": "available",
            }],
        }


def make_match():
    now = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)
    return Match(
        id="game-1",
        home_team="Brazil",
        away_team="Morocco",
        kickoff_utc=now,
        venue="MetLife Stadium",
        venue_city="East Rutherford",
        status="Scheduled",
        source_title="Source",
        source_url="https://example.com/schedule",
        source_fetched_at=now,
    )


def make_dated_match(day: date, home_team="Brazil", away_team="Morocco"):
    kickoff = datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc)
    return Match(
        id=f"game-{day.isoformat()}",
        home_team=home_team,
        away_team=away_team,
        kickoff_utc=kickoff,
        venue="MetLife Stadium",
        venue_city="East Rutherford",
        status="Scheduled",
        source_title="Source",
        source_url="https://example.com/schedule",
        source_fetched_at=kickoff,
    )


def make_market(title="Brazil to beat Morocco in the FIFA World Cup?", raw=None):
    return Market(
        id="KXWC-BRA-MAR-BRAZIL",
        question=title,
        category="Sports",
        yes_price=0.56,
        no_price=0.44,
        volume_24h=100,
        total_volume=1000,
        liquidity=500,
        end_date=None,
        resolution_source="https://kalshi.com/markets/KXWC-BRA-MAR-BRAZIL",
        is_active=True,
        platform="kalshi",
        raw=raw or {
            "yes_bid_dollars": "0.56",
            "no_bid_dollars": "0.44",
            "world_cup_model_inputs": {
                "model_yes_probability": 0.75,
                "source": "test-fixture-model",
            },
        },
    )


def make_service(monkeypatch, tmp_path, markets=None, orderbook=None, schedule=None, key=True, player_history_provider=None):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test" if key else "")
    monkeypatch.setattr(settings, "world_cup_claude_model", "claude-opus-5")
    cal_path = tmp_path / "calibration.json"
    cal_path.write_text('{"resolved_predictions": 30, "event_types": ["match_result", "totals"], "brier": 0.20, "market_brier": 0.24}')
    monkeypatch.setattr(settings, "world_cup_calibration_file", str(cal_path))
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(markets=markets or [], orderbook=orderbook)
    return WorldCupService(
        lambda: adapter,
        schedule_provider=schedule or FakeSchedule([make_match()]),
        player_history_provider=player_history_provider,
        now_fn=lambda: now,
    )


def test_provider_status_uses_claude_key_and_48(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, key=True)
    status = service.provider_status()

    assert status["active"] == "Claude"
    assert status["claude"]["api_key_configured"] is True
    assert status["claude"]["model"] == "claude-opus-5"
    assert "mock" not in str(status).lower()


def test_missing_claude_key_disables_recommendations(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, key=False)
    status = service.route_status()

    assert status["label"] == LIVE_UNAVAILABLE
    assert PROVIDER_UNAVAILABLE in status["blocked_reasons"]


async def test_schedule_unavailable_returns_no_fake_matches(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, schedule=FakeSchedule([], NO_SCHEDULE))
    payload = await service.get_matchday()

    assert payload["matches"] == []
    assert payload["candidates"] == []
    assert payload["status"]["label"] == LIVE_UNAVAILABLE
    assert NO_SCHEDULE in payload["message"]


async def test_today_only_scope_does_not_roll_forward(monkeypatch, tmp_path):
    tomorrow = date(2026, 6, 25)
    schedule = DateSchedule({tomorrow: [make_dated_match(tomorrow)]})
    service = make_service(monkeypatch, tmp_path, schedule=schedule)

    service.update_settings({"scope": SCOPE_TODAY_ONLY})
    payload = await service.get_matchday()

    assert payload["dashboard_day"] == "2026-06-24"
    assert payload["matchday_scope"] == SCOPE_TODAY_ONLY
    assert payload["matches"] == []
    assert payload["message"] == NO_MATCHES_TODAY
    assert schedule.requested_days == [date(2026, 6, 24)]


async def test_next_matchday_scope_rolls_forward(monkeypatch, tmp_path):
    tomorrow = date(2026, 6, 25)
    schedule = DateSchedule({tomorrow: [make_dated_match(tomorrow)]})
    service = make_service(monkeypatch, tmp_path, schedule=schedule)

    service.update_settings({"scope": SCOPE_TODAY_OR_NEXT_MATCHDAY})
    payload = await service.get_matchday()

    assert payload["dashboard_day"] == "2026-06-25"
    assert payload["matchday_scope"] == SCOPE_TODAY_OR_NEXT_MATCHDAY
    assert len(payload["matches"]) == 1
    assert schedule.requested_days[:2] == [date(2026, 6, 24), date(2026, 6, 25)]


async def test_unmapped_market_is_blocked(monkeypatch, tmp_path):
    service = make_service(
        monkeypatch,
        tmp_path,
        markets=[make_market("Brazil and Morocco FIFA World Cup weather delay?")],
    )
    payload = await service.get_matchday()

    assert payload["candidates"]
    assert payload["candidates"][0]["approval_eligible"] is False
    assert MAPPING_UNAVAILABLE in payload["candidates"][0]["blocked_reasons"]


async def test_calibration_unavailable_blocks_paper_approval(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, markets=[make_market()])
    monkeypatch.setattr(settings, "world_cup_calibration_file", str(tmp_path / "missing.json"))
    payload = await service.get_matchday()

    assert payload["candidates"]
    assert payload["candidates"][0]["approval_eligible"] is False
    assert payload["candidates"][0]["trade"]["paper_approval_state"] == "disabled"
    assert payload["candidates"][0]["trade"]["live_approval_state"] == "disabled"
    assert CALIBRATION_UNAVAILABLE in payload["candidates"][0]["blocked_reasons"]
    assert "live approval disabled" not in str(payload).lower()


async def test_calibration_must_beat_market_baseline(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, markets=[make_market()])
    Path(settings.world_cup_calibration_file).write_text(
        '{"resolved_predictions": 100, "event_types": ["match_result", "totals"], "brier": 0.26, "market_brier": 0.24}'
    )

    payload = await service.get_matchday()

    assert payload["candidates"][0]["approval_eligible"] is False
    assert CALIBRATION_UNAVAILABLE in payload["candidates"][0]["blocked_reasons"]
    assert service.calibration_status()["beats_market_baseline"] is False


async def test_midpoint_only_model_inputs_block_approval(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, markets=[make_market(raw={"yes_bid_dollars": "0.56", "no_bid_dollars": "0.44"})])
    payload = await service.get_matchday()

    assert payload["candidates"]
    assert payload["candidates"][0]["approval_eligible"] is False
    assert payload["candidates"][0]["status"] == "blocked"
    assert MODEL_INPUTS_UNAVAILABLE in payload["candidates"][0]["blocked_reasons"]


async def test_trade_cards_show_one_contract_kalshi_quote(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path, markets=[make_market()])
    payload = await service.get_matchday()
    trade = payload["candidates"][0]["trade"]

    assert trade["cost"] == 0.56
    assert trade["contracts"] == 1.0
    assert trade["max_payout"] == 1.0
    assert trade["hit_probability"] > 0
    assert trade["probability_gap"] > 0
    assert "Confirm final fees" in trade["pricing_source"]
    assert trade["live_order_placement"] == "disabled"
    assert payload["basket"]["totals"]["cost"] == 0.56


async def test_low_price_trade_remains_one_contract_quote(monkeypatch, tmp_path):
    service = make_service(
        monkeypatch,
        tmp_path,
        markets=[make_market()],
        orderbook={"yes": [["0.10", "20"]], "no": [["0.87", "20"]]},
    )
    payload = await service.get_matchday()
    trade = payload["candidates"][0]["trade"]

    assert trade["price"] == 0.13
    assert trade["contracts"] == 1.0
    assert trade["cost"] == 0.13
    assert trade["max_payout"] == 1.0
    assert payload["basket"]["totals"]["cost"] == 0.13


async def test_research_slip_groups_build_multiple_one_contract_cards(monkeypatch, tmp_path):
    over_market = make_market("Will over 2.5 goals be scored in Brazil vs Morocco?")
    over_market.id = "KXWCTOTAL-BRA-MAR-OVER25"
    btts_market = make_market("Will both teams score in Brazil vs Morocco?")
    btts_market.id = "KXWCBTTS-BRA-MAR-YES"
    service = make_service(
        monkeypatch,
        tmp_path,
        markets=[over_market, btts_market],
        orderbook={"yes": [["0.54", "100"]], "no": [["0.44", "100"]]},
    )
    service.update_settings({"max_slips": 4})

    payload = await service.get_matchday()

    assert payload["candidate_count"] == 2
    assert payload["slip_count"] == 2
    assert payload["basket"]["slip_id"] == payload["slips"][0]["slip_id"]
    for slip in payload["slips"]:
        assert slip["totals"]["cost"] == 0.56
        assert slip["status"] == "paper-ready"
        assert slip["approval_eligible"] is True
        assert "all_legs_hit_probability" not in slip
        assert slip["full_payout_requires"] == "Every leg must win to receive the full gross payout shown."
        assert slip["live_order_placement"] == "disabled"
        assert len(slip["legs"]) == 1
        assert slip["legs"][0]["trade"]["cost"] == 0.56
        assert slip["legs"][0]["trade"]["contracts"] == 1


async def test_big_payout_strategy_prioritizes_player_props_and_shows_context(monkeypatch, tmp_path):
    over_market = make_market("Will over 0.5 goals be scored in Brazil vs Morocco?")
    over_market.id = "KXWCTOTAL-BRA-MAR-OVER05"
    player_market = make_market(
        title="Neymar: 2+ goals",
        raw={
            "yes_bid_dollars": "0.01",
            "no_bid_dollars": "0.98",
            "ticker": "KXWCGOAL-26JUN24BRAMAR-NEYMAR-2",
            "event_ticker": "KXWCGOAL-26JUN24BRAMAR",
            "rules_primary": "If Neymar records at least 2 goals during the Brazil vs Morocco FIFA World Cup soccer game, then the market resolves to Yes.",
        },
    )
    player_market.id = "KXWCGOAL-26JUN24BRAMAR-NEYMAR-2"
    service = make_service(
        monkeypatch,
        tmp_path,
        markets=[over_market, player_market],
        orderbook={
            over_market.id: {"yes": [["0.94", "100"]], "no": [["0.04", "100"]]},
            player_market.id: {"yes": [["0.01", "1000"]], "no": [["0.98", "1000"]]},
        },
        player_history_provider=FakePlayerHistory(),
    )
    service.update_settings({
        "slip_strategy": SLIP_STRATEGY_BIG_PAYOUT,
        "max_legs_per_match": 1,
    })

    payload = await service.get_matchday()
    first_leg = payload["slips"][0]["legs"][0]

    assert payload["status"]["settings_summary"]["slip_strategy"] == SLIP_STRATEGY_BIG_PAYOUT
    assert first_leg["mapping"]["type"] == "player_prop"
    assert first_leg["mapping"]["player_name"] == "Neymar"
    assert first_leg["trade"]["price"] == 0.02
    assert "payout_multiple" not in first_leg["trade"]
    assert "payout_multiple" not in payload["slips"][0]["totals"]
    assert first_leg["trade"]["player_context"]["player_name"] == "Neymar"
    assert "recent match logs unavailable" in first_leg["trade"]["player_context"]["performance_stats_status"]
    assert "Player history" not in first_leg["blocked_reasons"]


async def test_player_prop_strategy_filters_to_player_markets(monkeypatch, tmp_path):
    exact_market = make_market("Will the final score be Brazil wins 3-2?")
    exact_market.id = "KXWCSCORE-BRA-MAR-3-2"
    player_market = make_market(
        title="Neymar: 1+ assists?",
        raw={
            "yes_bid_dollars": "0.05",
            "no_bid_dollars": "0.90",
            "ticker": "KXWCAST-26JUN24BRAMAR-NEYMAR-1",
            "event_ticker": "KXWCAST-26JUN24BRAMAR",
            "rules_primary": "If Neymar records at least 1 assist during the Brazil vs Morocco FIFA World Cup soccer game, then the market resolves to Yes.",
        },
    )
    player_market.id = "KXWCAST-26JUN24BRAMAR-NEYMAR-1"
    service = make_service(
        monkeypatch,
        tmp_path,
        markets=[exact_market, player_market],
        orderbook={
            exact_market.id: {"yes": [["0.01", "1000"]], "no": [["0.98", "1000"]]},
            player_market.id: {"yes": [["0.03", "1000"]], "no": [["0.94", "1000"]]},
        },
        player_history_provider=FakePlayerHistory(),
    )
    service.update_settings({"slip_strategy": SLIP_STRATEGY_PLAYER_PROPS})

    payload = await service.get_matchday()

    assert payload["slips"]
    assert all(leg["mapping"]["type"] == "player_prop" for slip in payload["slips"] for leg in slip["legs"])


async def test_budget_slips_do_not_use_kalshi_combo_markets_as_legs(monkeypatch, tmp_path):
    combo_market = make_market(
        title="yes Morocco,yes Brazil,yes Over 2.5 goals scored",
        raw={
            "yes_bid_dollars": "0.22",
            "no_bid_dollars": "0.72",
            "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026TEST",
        },
    )
    combo_market.id = "KXMVESPORTSMULTIGAMEEXTENDED-S2026TEST-ABC"
    single_market = make_market("Will over 2.5 goals be scored in Brazil vs Morocco?")
    single_market.id = "KXWCTOTAL-BRA-MAR-OVER25"
    service = make_service(monkeypatch, tmp_path, markets=[combo_market, single_market])

    payload = await service.get_matchday()

    assert payload["candidate_count"] == 2
    assert payload["slip_count"] == 1
    slip_market_ids = [leg["market_id"] for leg in payload["slips"][0]["legs"]]
    assert combo_market.id not in slip_market_ids
    assert single_market.id in slip_market_ids


def test_removed_slip_budget_settings_are_ignored(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path)

    result = service.update_settings({"slip_budget_min": 20, "slip_budget_max": 10, "max_slips": 99})

    assert result["status"] == "updated"
    assert "slip_budget_min" not in result["settings_summary"]
    assert "slip_budget_max" not in result["settings_summary"]
    assert result["settings_summary"]["max_slips"] == 12


async def test_team_only_kalshi_combo_market_is_discovered_and_blocked(monkeypatch, tmp_path):
    combo_market = make_market(
        title="yes Morocco,yes Brazil,yes Over 2.5 goals scored",
        raw={
            "yes_bid_dollars": "0.22",
            "no_bid_dollars": "0.72",
            "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026TEST",
        },
    )
    combo_market.id = "KXMVESPORTSMULTIGAMEEXTENDED-S2026TEST-ABC"
    service = make_service(monkeypatch, tmp_path, markets=[combo_market])
    payload = await service.get_matchday()

    assert payload["markets"]
    assert payload["matches"][0]["linked_market_count"] == 1
    assert payload["candidates"][0]["approval_eligible"] is False
    assert TRUE_COMBO_UNAVAILABLE in payload["candidates"][0]["blocked_reasons"]


async def test_mixed_sport_combo_market_is_not_discovered(monkeypatch, tmp_path):
    combo_market = make_market(
        title="yes Jack Draper,yes Boston,yes Los Angeles D,yes Morocco,yes Brazil,yes Karolina Muchova",
        raw={
            "yes_bid_dollars": "0.12",
            "no_bid_dollars": "0.82",
            "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026MIXED",
        },
    )
    combo_market.id = "KXMVESPORTSMULTIGAMEEXTENDED-S2026MIXED-ABC"
    service = make_service(monkeypatch, tmp_path, markets=[combo_market])
    payload = await service.get_matchday()

    assert payload["markets"] == []
    assert payload["candidates"] == []
    assert payload["matches"][0]["linked_market_count"] == 0


async def test_non_matchday_world_cup_team_combo_is_not_discovered(monkeypatch, tmp_path):
    combo_market = make_market(
        title="yes Morocco,yes Brazil,yes Netherlands",
        raw={
            "yes_bid_dollars": "0.12",
            "no_bid_dollars": "0.82",
            "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026FUTURE",
        },
    )
    combo_market.id = "KXMVESPORTSMULTIGAMEEXTENDED-S2026FUTURE-ABC"
    service = make_service(monkeypatch, tmp_path, markets=[combo_market])
    payload = await service.get_matchday()

    assert payload["markets"] == []
    assert payload["candidates"] == []
    assert payload["matches"][0]["linked_market_count"] == 0


async def test_player_goal_single_from_rules_is_discovered_with_lineup_warning(monkeypatch, tmp_path):
    player_market = make_market(
        title="Neymar: 1+ goals",
        raw={
            "yes_bid_dollars": "0.33",
            "no_bid_dollars": "0.62",
            "ticker": "KXWCGOAL-26JUN24BRAMAR-BRANEYM10-1",
            "event_ticker": "KXWCGOAL-26JUN24BRAMAR",
            "rules_primary": "If Neymar records at least 1 goal during the Brazil vs Morocco professional FIFA World Cup soccer game originally scheduled for Jun 24, 2026, then the market resolves to Yes.",
        },
    )
    player_market.id = "KXWCGOAL-26JUN24BRAMAR-BRANEYM10-1"
    service = make_service(monkeypatch, tmp_path, markets=[player_market])
    payload = await service.get_matchday()

    assert payload["markets"]
    assert payload["matches"][0]["linked_market_count"] == 1
    assert payload["candidates"][0]["mapping"]["type"] == "player_prop"
    assert payload["candidates"][0]["approval_eligible"] is False
    assert payload["candidates"][0]["trade"]["live_order_placement"] == "disabled"
    assert any(
        "Lineup data unavailable/provisional" in reason
        for reason in payload["candidates"][0]["blocked_reasons"]
    )


async def test_exact_score_single_from_rules_is_discovered(monkeypatch, tmp_path):
    exact_score_market = make_market(
        title="Will the final score be Brazil wins 2-1?",
        raw={
            "yes_bid_dollars": "0.08",
            "no_bid_dollars": "0.88",
            "ticker": "KXWCSCORE-26JUN24BRAMAR-BRA2MAR1",
            "event_ticker": "KXWCSCORE-26JUN24BRAMAR",
            "rules_primary": "This market resolves based on the final score of the Brazil vs Morocco professional FIFA World Cup soccer game originally scheduled for Jun 24, 2026.",
        },
    )
    exact_score_market.id = "KXWCSCORE-26JUN24BRAMAR-BRA2MAR1"
    service = make_service(monkeypatch, tmp_path, markets=[exact_score_market])
    payload = await service.get_matchday()

    assert payload["markets"]
    assert payload["matches"][0]["linked_market_count"] == 1
    assert payload["candidates"][0]["mapping"]["type"] == "exact_score"


def test_exact_score_market_mapping(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path)
    mapping = service._map_market(make_market("Brazil to win by exact score 2-1 against Morocco"))

    assert mapping["type"] == "exact_score"
    assert "2-1" in mapping["description"]
    assert mapping["lineup_required"] is False


def test_quote_staleness_blocks_paper_approval(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path)
    old = (datetime(2026, 6, 24, 11, 0, tzinfo=timezone.utc)).isoformat()
    card = {
        "title": "Brazil to beat Morocco",
        "market": "KXWC-BRA-MAR-BRAZIL",
        "side": "YES",
        "price": 0.56,
        "cost": 0.56,
        "contracts": 1,
        "odds": 0.56,
        "max_payout": 1,
        "probability_gap": 0.05,
        "confidence_adjusted_probability_gap": 0.04,
        "status": "approval-ready",
        "approval_state": "enabled",
        "approval_eligible": True,
        "quote_timestamp": old,
        "quote_fresh_seconds": 30,
        "execution_mode": "paper",
    }

    locked = service.approval_lock(card)
    result = service.paper_approve({"lock_id": locked["lock"]["lock_id"]})

    assert locked["lock"]["card"]["approval_eligible"] is False
    assert locked["lock"]["card"]["disabled_reason"] == QUOTE_STALE
    assert result["status"] == "blocked"
    assert result["error"] == QUOTE_STALE


def test_approval_lock_rejects_changed_card(monkeypatch, tmp_path):
    service = make_service(monkeypatch, tmp_path)
    now = service.now_fn().isoformat()
    card = {
        "title": "Brazil to beat Morocco",
        "market": "KXWC-BRA-MAR-BRAZIL",
        "side": "YES",
        "price": 0.56,
        "cost": 0.56,
        "contracts": 1,
        "odds": 0.56,
        "max_payout": 1,
        "probability_gap": 0.05,
        "confidence_adjusted_probability_gap": 0.04,
        "status": "approval-ready",
        "approval_state": "enabled",
        "approval_eligible": True,
        "quote_timestamp": now,
        "quote_fresh_seconds": 30,
        "execution_mode": "paper",
    }
    locked = service.approval_lock(card)
    changed = {**card, "price": 0.57}

    result = service.paper_approve({"lock_id": locked["lock"]["lock_id"], "card": changed})

    assert result["status"] == "error"
    assert "changed" in result["error"]


def test_environment_status_is_redacted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")

    status = redacted_environment_status()

    assert status["ANTHROPIC_API_KEY"] is True
    assert "secret-value" not in str(status)
