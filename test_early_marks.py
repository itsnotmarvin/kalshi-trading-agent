from datetime import datetime, timezone
import json
from types import SimpleNamespace

import scripts.manual_probes.test_early_marks as early_marks

from scripts.manual_probes.test_early_marks import (
    EARLY_MARKS_RESPONSE_SCHEMA,
    _anthropic_message_kwargs,
    ask_claude,
    candidate_scan_matches,
    candidate_market_group_title,
    market_display_name,
    market_display_title,
    prioritize_soccer_path_candidates,
    ranking_roi_signal,
    score_raw_market,
    select_scan_shortlist,
    select_shortlist,
    validate_claude_results,
)


def presidential_market(**overrides):
    raw = {
        "ticker": "KXPRESPERSON-28-MRUB",
        "title": "Who will win the next presidential election?",
        "event_title": "2028 presidential election",
        "category": "Elections",
        "status": "active",
        "yes_sub_title": "Marco Rubio",
        "no_sub_title": "Marco Rubio",
        "subtitle": ":: Republican",
        "yes_ask_dollars": "0.1900",
        "yes_bid_dollars": "0.1800",
        "no_ask_dollars": "0.8200",
        "no_bid_dollars": "0.8100",
        "last_price_dollars": "0.1800",
        "previous_price_dollars": "0.1800",
        "volume_fp": "4261595.79",
        "volume_24h_fp": "10163.09",
        "liquidity_dollars": "0",
        "expected_expiration_time": "2029-01-21T15:00:00Z",
        "settlement_source_url": "Kalshi market metadata",
    }
    raw.update(overrides)
    return raw


def world_cup_market(**overrides):
    raw = presidential_market(
        ticker="KXWORLD-CUP-ARG",
        title="Who will win the FIFA World Cup?",
        event_title="World Cup winner",
        category="Sports",
        yes_sub_title="Argentina",
        no_sub_title="Argentina",
        subtitle=":: Soccer",
    )
    raw.update(overrides)
    return raw


def bitcoin_market(**overrides):
    raw = presidential_market(
        ticker="KXBTC-2028-100K",
        title="Will Bitcoin hit $100,000?",
        event_title="Bitcoin price milestone",
        category="Crypto",
        yes_sub_title="Above $100k",
        no_sub_title="Above $100k",
        subtitle=":: Bitcoin",
    )
    raw.update(overrides)
    return raw


def crossed_book_market(**overrides):
    # yes_ask + no_ask = 0.95 < 1 → a crossed/locked (stale/erroneous) book.
    raw = presidential_market(
        ticker="KXPRESPERSON-28-CROSS",
        yes_sub_title="Crossed Candidate",
        no_sub_title="Crossed Candidate",
        yes_ask_dollars="0.4000",
        no_ask_dollars="0.5500",
        yes_bid_dollars="0.3900",
        no_bid_dollars="0.5400",
    )
    raw.update(overrides)
    return raw


def liquid_market(**overrides):
    # Real CURRENT book: resting liquidity and a resale bid to exit into.
    raw = presidential_market(
        ticker="KXPRESPERSON-28-LIQ",
        yes_sub_title="Liquid Candidate",
        no_sub_title="Liquid Candidate",
        liquidity_dollars="5000",
        yes_bid_dollars="0.1800",
        no_bid_dollars="0.8100",
    )
    raw.update(overrides)
    return raw


def illiquid_market(**overrides):
    # Huge LIFETIME volume (inherited volume_fp) but no current liquidity and no
    # resting bid — nothing to sell into.
    raw = presidential_market(
        ticker="KXPRESPERSON-28-ILLIQ",
        yes_sub_title="Illiquid Candidate",
        no_sub_title="Illiquid Candidate",
        liquidity_dollars="0",
        yes_bid_dollars="",
        no_bid_dollars="",
    )
    raw.update(overrides)
    return raw


def test_market_display_title_combines_market_and_placement_name():
    raw = presidential_market()

    display_name = market_display_name(raw, raw["ticker"])
    display_title = market_display_title(raw["title"], display_name, raw["event_title"])

    assert display_name == "Marco Rubio"
    assert display_title == "2028 presidential election - Marco Rubio"
    assert "MRUB" not in display_title


def test_market_display_name_uses_custom_strike_when_subtitle_missing():
    raw = presidential_market(
        yes_sub_title="",
        no_sub_title="",
        subtitle=":: Republican",
        custom_strike={"Pres candidate": "Gavin Newsom"},
    )

    assert market_display_name(raw, raw["ticker"]) == "Gavin Newsom"


def test_score_and_roi_move_with_contract_specific_market_inputs():
    base = score_raw_market(presidential_market())
    stronger = score_raw_market(
        presidential_market(
            ticker="KXPRESPERSON-28-TEST",
            yes_sub_title="Test Candidate",
            no_sub_title="Test Candidate",
            yes_ask_dollars="0.0800",
            yes_bid_dollars="0.0790",
            no_ask_dollars="0.9210",
            no_bid_dollars="0.9200",
            last_price_dollars="0.0950",
            previous_price_dollars="0.0800",
            volume_fp="9000000",
            volume_24h_fp="500000",
        )
    )

    assert base is not None
    assert stronger is not None
    assert stronger.rank_score != base.rank_score
    assert stronger.expected_repricing_roi != base.expected_repricing_roi
    assert stronger.bull_probability != base.bull_probability


def test_sports_world_cup_uses_deeper_model_profile_than_politics():
    politics = score_raw_market(presidential_market())
    world_cup = score_raw_market(world_cup_market())

    assert politics is not None
    assert world_cup is not None
    assert politics.model_key == "politics_election"
    assert world_cup.model_key == "sports_soccer_worldcup"
    assert world_cup.category_path == ["sports", "soccer", "world cup"]
    assert world_cup.next_catalyst["label"].startswith("Draw/bracket")
    assert world_cup.expected_future_price != politics.expected_future_price


def test_flow_signal_is_explicit_about_missing_holder_identity():
    candidate = score_raw_market(world_cup_market(last_price_dollars="0.2500", previous_price_dollars="0.1900"))

    assert candidate is not None
    assert candidate.flow_signal["holder_identity_available"] is False
    assert candidate.flow_signal["holding_duration_available"] is False
    assert "cannot prove who bought or sold" in candidate.flow_signal["what_it_cannot_tell_us"]


def test_expected_roi_is_reported_as_raw_uncapped_learning_signal():
    candidate = score_raw_market(world_cup_market(yes_ask_dollars="0.0300", yes_bid_dollars="0.0290"))

    assert candidate is not None
    assert candidate.expected_repricing_roi is not None
    assert candidate.price_scenario_basis["roi_policy"].startswith("Expected ROI is raw and uncapped")


def test_roi_influence_on_ranking_is_capped_but_raw_roi_stays_visible():
    assert ranking_roi_signal(5.0) == 1.0

    tiny_price = score_raw_market(
        presidential_market(
            ticker="KXPRESPERSON-28-TINY",
            yes_sub_title="Tiny Price",
            no_sub_title="Tiny Price",
            yes_ask_dollars="0.0100",
            yes_bid_dollars="0.0090",
        )
    )
    normal_price = score_raw_market(
        presidential_market(
            ticker="KXPRESPERSON-28-NORMAL",
            yes_sub_title="Normal Price",
            no_sub_title="Normal Price",
            yes_ask_dollars="0.2000",
            yes_bid_dollars="0.1900",
        )
    )

    assert tiny_price is not None
    assert normal_price is not None
    assert tiny_price.expected_repricing_roi is not None
    assert tiny_price.expected_repricing_roi > normal_price.expected_repricing_roi
    assert tiny_price.rank_score < 90


def test_recommended_placement_size_is_user_dollar_amount_not_contract_price():
    candidate = score_raw_market(presidential_market(yes_ask_dollars="0.3800", yes_bid_dollars="0.3700"))

    assert candidate is not None
    assert 20 <= candidate.recommended_placement_dollars <= 100
    assert candidate.estimated_position_cost is not None
    assert 20 <= candidate.estimated_position_cost <= 100
    assert candidate.placement_sizing_basis["contract_math"].startswith("estimated_contracts")


def test_shortlist_diversifies_across_parent_markets():
    raws = [
        presidential_market(ticker=f"KXPRESPERSON-28-P{i}", yes_sub_title=f"Candidate {i}", no_sub_title=f"Candidate {i}", yes_ask_dollars="0.0100")
        for i in range(5)
    ]
    raws += [
        world_cup_market(ticker=f"KXWORLD-CUP-T{i}", yes_sub_title=f"Team {i}", no_sub_title=f"Team {i}", yes_ask_dollars="0.0200")
        for i in range(4)
    ]
    raws += [
        bitcoin_market(ticker=f"KXBTC-2028-B{i}", yes_sub_title=f"Bitcoin {i}", no_sub_title=f"Bitcoin {i}", yes_ask_dollars="0.0400")
        for i in range(3)
    ]
    candidates = [score_raw_market(raw) for raw in raws]
    scored = sorted([candidate for candidate in candidates if candidate is not None], key=lambda c: c.rank_score, reverse=True)

    selected = select_shortlist(scored, shortlist_size=6, max_per_market=2, unreleased_slots=0)
    group_counts = {}
    for candidate in selected:
        group = candidate_market_group_title(candidate)
        group_counts[group] = group_counts.get(group, 0) + 1

    assert len(selected) == 6
    assert max(group_counts.values()) <= 2
    assert len(group_counts) > 1


def test_shortlist_reserves_unopened_markets_when_available():
    open_candidates = [
        score_raw_market(presidential_market(ticker=f"KXPRESPERSON-28-O{i}", yes_sub_title=f"Open {i}", no_sub_title=f"Open {i}"))
        for i in range(5)
    ]
    unopened = score_raw_market(
        world_cup_market(
            ticker="KXWORLD-CUP-UNOPENED",
            yes_sub_title="Unopened Team",
            no_sub_title="Unopened Team",
            status="unopened",
            yes_ask_dollars="",
            no_ask_dollars="",
        )
    )
    scored = sorted([candidate for candidate in [*open_candidates, unopened] if candidate is not None], key=lambda c: c.rank_score, reverse=True)

    selected = select_shortlist(scored, shortlist_size=4, max_per_market=2, unreleased_slots=1)

    assert any(candidate.probe_status == "Pre-open watch" for candidate in selected)


def test_finalized_markets_are_not_early_mark_candidates():
    candidate = score_raw_market(presidential_market(status="finalized"))

    assert candidate is None


def test_scan_filters_match_category_status_and_horizon():
    candidate = score_raw_market(presidential_market())
    world_cup = score_raw_market(world_cup_market())

    assert candidate is not None
    assert world_cup is not None
    assert candidate_scan_matches(candidate, category_filter="politics", status_filter="open", horizon_filter="long")
    assert candidate_scan_matches(world_cup, category_filter="soccer", status_filter="open", horizon_filter="long")
    assert not candidate_scan_matches(candidate, category_filter="sports", status_filter="open", horizon_filter="long")
    assert not candidate_scan_matches(candidate, category_filter="politics", status_filter="preopen", horizon_filter="long")


def test_filtered_shortlist_does_not_fallback_to_other_categories():
    politics = score_raw_market(presidential_market())

    assert politics is not None
    selected, eligible = select_scan_shortlist(
        [politics],
        shortlist_size=5,
        category_filter="soccer",
        status_filter="all",
        horizon_filter="all",
    )

    assert eligible == []
    assert selected == []


def test_soccer_path_candidates_sort_before_host_markets():
    host = score_raw_market(
        world_cup_market(
            ticker="KXWCHOST-2038-CAM",
            title="Who will host the 2038 FIFA World Cup?",
            event_title="2038 World Cup host",
            yes_sub_title="Cambodia",
            no_sub_title="Cambodia",
        )
    )
    winner = score_raw_market(world_cup_market(ticker="KXMENWORLDCUP-26-BR", yes_sub_title="Brazil", no_sub_title="Brazil"))

    assert host is not None
    assert winner is not None
    host.rank_score = 99.0
    winner.rank_score = 10.0
    winner.soccer_path = {"method": "test"}

    assert prioritize_soccer_path_candidates([host, winner])[0] is winner


def test_crossed_book_is_flagged_not_rewarded_as_tight_spread():
    # Invariant I5: a crossed/locked book (yes_ask + no_ask < 1) is a data-quality
    # warning, not the tightest possible market. It must never earn the best spread
    # score and must not be promoted to an actionable catalyst setup.
    normal = score_raw_market(presidential_market())
    crossed = score_raw_market(crossed_book_market())

    assert normal is not None and crossed is not None
    assert crossed.crossed_book is True
    assert normal.crossed_book is False
    assert crossed.spread_score < normal.spread_score
    assert crossed.spread_score <= 50.0
    assert crossed.probe_status != "Catalyst setup"


def test_lifetime_volume_does_not_fake_current_exitability():
    # Invariant: exitability must reflect the CURRENT book (resting liquidity +
    # a resale bid), not lifetime volume. A market with huge lifetime volume but
    # no current liquidity and no bid cannot be scored as exitable.
    liquid = score_raw_market(liquid_market())
    illiquid = score_raw_market(illiquid_market())

    assert liquid is not None and illiquid is not None
    # Lifetime interest stays high for the illiquid market (huge volume_fp)...
    assert illiquid.liquidity_score >= 90.0
    # ...but exitability does not inherit that illusion.
    assert illiquid.exitability_score <= 30.0
    assert illiquid.exitability_score < liquid.exitability_score
    # Exitability must actually move the rank: the genuinely exitable market
    # outranks the illiquid one, all else equal.
    assert liquid.rank_score > illiquid.rank_score


def _valid_claude_result(ticker: str, source_url: str) -> dict:
    return {
        "ticker": ticker,
        "decision": "Watch only",
        "side": "Watch",
        "scenario_weights": {"bull": 35.0, "base": 40.0, "bear": 25.0},
        "scenario_weight_basis": "Current price, public catalyst timing, and exitability.",
        "expected_future_price_cents": 24.0,
        "entry_limit_cents": 18.0,
        "next_catalyst": "Next scheduled public event",
        "catalyst_date_estimate": None,
        "fair_probability_range": {
            "low_pct": 12.0,
            "high_pct": 20.0,
            "basis": "Research-only range anchored to the supplied market data.",
        },
        "why_i_believe_this": "I would watch for a sourced public catalyst before considering any action.",
        "market_knows_check": "Investigate any sharp move before a public explanation.",
        "no_direction_check": "The catalyst path is plausible but not tradeable yet.",
        "sources": [{
            "label": "Supplied report",
            "url_or_reference": source_url,
            "published_date": "2026-08-01",
            "why_it_matters": "It dates the public catalyst context.",
        }],
        "exit_monitor": [{"trigger": "Price moves", "action": "Recheck public evidence."}],
    }


def test_opus_5_structured_kwargs_omit_legacy_temperature(monkeypatch):
    monkeypatch.setattr(early_marks.settings, "claude_scan_model", "claude-opus-5")

    kwargs = _anthropic_message_kwargs(
        max_tokens=16000,
        system="system",
        messages=[{"role": "user", "content": "prompt"}],
        temperature=0.2,
        output_schema=EARLY_MARKS_RESPONSE_SCHEMA,
        effort="medium",
    )

    assert kwargs["model"] == "claude-opus-5"
    assert "temperature" not in kwargs
    assert kwargs["output_config"]["effort"] == "medium"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


async def test_ask_claude_reads_final_text_after_opus_thinking_block(monkeypatch):
    candidate = score_raw_market(presidential_market())
    assert candidate is not None
    source_url = "https://example.com/supplied-report"
    result = _valid_claude_result(candidate.ticker, source_url)
    captured = {}

    async def fake_research(candidates, research_slots):
        return {candidate.ticker: [{"url": source_url}]}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(type="thinking", thinking=""),
                    SimpleNamespace(type="text", text=json.dumps({"results": [result]})),
                ],
            )

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr(early_marks, "gather_external_research", fake_research)
    monkeypatch.setattr(early_marks, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(early_marks.settings, "claude_scan_model", "claude-opus-5")

    results, raw_text, error = await ask_claude([candidate], max_results=1)

    assert error is None
    assert results == [result]
    assert json.loads(raw_text) == {"results": [result]}
    assert captured["model"] == "claude-opus-5"
    assert captured["output_config"]["format"]["type"] == "json_schema"


def test_claude_validation_rejects_unsupplied_source_url():
    candidate = score_raw_market(presidential_market())
    assert candidate is not None
    supplied_url = "https://example.com/supplied-report"
    result = _valid_claude_result(candidate.ticker, "https://example.com/invented")

    try:
        validate_claude_results(
            [result],
            [candidate],
            max_results=1,
            external_research={candidate.ticker: [{"url": supplied_url}]},
        )
    except ValueError as exc:
        assert "outside supplied research" in str(exc)
    else:
        raise AssertionError("Invented source URL should fail semantic validation")
