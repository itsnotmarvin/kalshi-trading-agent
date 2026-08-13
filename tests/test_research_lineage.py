import json

from core.agent import TradingAgent
from core.logger import TradeLogger


class DummyMarket:
    id = "KXTEST-26AUG08"
    question = "Will the test event occur?"
    category = "Politics"
    yes_price = 0.40


class DummyRiskManager:
    @staticmethod
    def calculate_r_score(**_kwargs):
        return 1.25


def make_agent():
    agent = TradingAgent.__new__(TradingAgent)
    agent.risk_manager = DummyRiskManager()
    return agent


def analysis_data(source_url):
    return {
        "market_id": DummyMarket.id,
        "market_question": DummyMarket.question,
        "my_probability": 0.63,
        "current_market_price": 0.40,
        "direction": "BUY_YES",
        "confidence": "HIGH",
        "reasoning_summary": "The cited primary-source evidence supports YES.",
        "base_rate": "Comparable events resolved YES in 6 of 10 cases.",
        "base_rate_source_urls": [source_url],
        "evidence_for": [{"claim": "The agency confirmed the event.", "source_urls": [source_url]}],
        "evidence_against": [{"claim": "Timing remains uncertain.", "source_urls": [source_url]}],
        "risk_factors": ["Timing uncertainty"],
    }


def collected_sources():
    sources = []
    TradingAgent._merge_search_sources(
        sources,
        {
            "query": "official agency event",
            "source": "tavily",
            "results": [{
                "title": "Official agency release",
                "url": "https://agency.gov/release?utm_source=search",
                "content": "Official confirmation.",
            }],
        },
    )
    return sources


def test_verified_search_result_citation_survives_into_actionable_proposal():
    proposal = make_agent()._proposal_from_analysis_data(
        analysis_data("https://agency.gov/release"),
        DummyMarket(),
        balance=100.0,
        research_sources=collected_sources(),
    )

    assert proposal.direction == "BUY_YES"
    assert proposal.source_validation == {
        "requested_direction": "BUY_YES",
        "actionable": True,
        "cited_source_count": 1,
        "unverified_url_count": 0,
    }
    assert proposal.evidence_for[0]["verified"] is True
    assert proposal.evidence_for[0]["source_ids"] == [proposal.research_sources[0]["source_id"]]
    assert proposal.research_sources[0]["cited"] is True
    # Tracking parameters do not break the match to the clean URL cited by the model.
    assert "utm_source=search" in proposal.research_sources[0]["url"]


def test_invented_citation_is_blocked_and_cannot_become_a_trade():
    proposal = make_agent()._proposal_from_analysis_data(
        analysis_data("https://invented.example/not-in-search-results"),
        DummyMarket(),
        balance=100.0,
        research_sources=collected_sources(),
    )

    assert proposal.direction == "HOLD"
    assert proposal.entry_direction is None
    assert proposal.position_size_usd == 0.0
    assert proposal.source_validation["actionable"] is False
    assert proposal.source_validation["unverified_url_count"] == 1
    assert any("source_lineage_gate" in factor for factor in proposal.risk_factors)


def test_legacy_string_evidence_is_retained_but_not_treated_as_sourced():
    data = analysis_data("https://agency.gov/release")
    data["evidence_for"] = ["An uncited legacy claim"]

    proposal = make_agent()._proposal_from_analysis_data(
        data,
        DummyMarket(),
        balance=100.0,
        research_sources=collected_sources(),
    )

    assert proposal.direction == "HOLD"
    assert proposal.evidence_for == [{
        "claim": "An uncited legacy claim",
        "source_ids": [],
        "verified": False,
    }]


def test_analysis_logger_persists_source_lineage(tmp_path):
    log_path = tmp_path / "trades.jsonl"
    logger = TradeLogger(str(log_path))
    proposal = make_agent()._proposal_from_analysis_data(
        analysis_data("https://agency.gov/release"),
        DummyMarket(),
        balance=100.0,
        research_sources=collected_sources(),
    )

    logger.log_analysis(
        market_id=proposal.market_id,
        question=proposal.market_question,
        market_price=proposal.market_price,
        estimated_prob=proposal.estimated_prob,
        edge=proposal.edge,
        direction=proposal.direction,
        confidence=proposal.confidence,
        reasoning=proposal.reasoning,
        base_rate=proposal.base_rate,
        base_rate_source_ids=proposal.base_rate_source_ids,
        evidence_for=proposal.evidence_for,
        evidence_against=proposal.evidence_against,
        research_sources=proposal.research_sources,
        source_validation=proposal.source_validation,
    )

    record = json.loads(log_path.read_text().strip())
    assert record["source_validation"]["actionable"] is True
    assert record["evidence_for"][0]["verified"] is True
    assert record["research_sources"][0]["url"].startswith("https://agency.gov/")
