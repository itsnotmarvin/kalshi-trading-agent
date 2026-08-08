"""
Agent Brain - provider-aware reasoning engine.

This is where the configured LLM analyzes markets, researches events,
estimates probabilities, and makes trade recommendations.
The agent uses tool calling for web search and structured
output for trade decisions.
"""
import asyncio
import json
from typing import Any

import anthropic  # type: ignore

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore

from adapters.base import Market, Position  # type: ignore
from config.settings import settings  # type: ignore
from config.prompts import SYSTEM_PROMPT, RESEARCH_PROMPT_TEMPLATE, MARKET_SCAN_PROMPT  # type: ignore
from core.risk_manager import RiskManager, TradeProposal  # type: ignore
from core.memory_manager import MemoryManager  # type: ignore
from core.trade_utils import infer_entry_direction  # type: ignore
from core.weather_engine import WeatherEngine  # type: ignore
from core.climate_engine import ClimateEngine  # type: ignore
from core.sports_engine import SportsEngine  # type: ignore


# Tools the configured LLM can use during analysis
AGENT_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for current news, data, polls, expert analysis, "
            "and any information relevant to estimating the probability of an "
            "event. Use this to gather evidence during the research phase. "
            "Always search for multiple perspectives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and include relevant dates"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "submit_analysis",
        "description": (
            "Submit your final analysis and trade recommendation for a market. "
            "Only call this AFTER completing your research and probability estimation. "
            "Set direction to HOLD if edge is below 8% or confidence is LOW. "
            "Use WAIT_FOR_ENTRY if conviction is high but you want a better price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {"type": "string"},
                "market_question": {"type": "string"},
                "my_probability": {
                    "type": "number",
                    "description": "Your calibrated probability estimate (0.0 to 1.0)"
                },
                "current_market_price": {
                    "type": "number",
                    "description": "Current YES price on the market"
                },
                "direction": {
                    "type": "string",
                    "enum": ["BUY_YES", "BUY_NO", "HOLD", "WAIT_FOR_ENTRY"]
                },
                "target_entry_price": {
                    "type": "number",
                    "description": "If WAIT_FOR_ENTRY, the exact price to trigger the buy"
                },
                "confidence": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
                },
                "reasoning_summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of your analysis"
                },
                "base_rate": {"type": "string"},
                "evidence_for": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "evidence_against": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "risk_factors": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": [
                "market_id", "market_question", "my_probability",
                "current_market_price", "direction", "confidence",
                "reasoning_summary"
            ]
        }
    }
]


def _openai_tools() -> list[dict[str, Any]]:
    """Convert Anthropic-style tool declarations to OpenAI chat tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in AGENT_TOOLS
    ]


class TradingAgent:
    """
    The provider-aware brain of the trading system.

    Flow:
    1. scan_markets() — identify promising markets
    2. research_and_analyze() — deep dive on each market
    3. Returns TradeProposal objects for the risk manager to approve/reject
    """

    def __init__(self, risk_manager: RiskManager, active_directive: str | None = None):
        self.llm_provider = self._resolve_llm_provider()
        self.client = None
        self.openai_client = None
        if self.llm_provider == "anthropic":
            self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        elif self.llm_provider == "openai":
            if OpenAI is None:
                print("⚠️  openai package is not installed; falling back to mock LLM mode.")
                self.llm_provider = "mock"
            else:
                self.openai_client = OpenAI(api_key=settings.openai_api_key)
        self.risk_manager = risk_manager
        self.memory = MemoryManager()
        self.weather_engine = WeatherEngine()
        self.climate_engine = ClimateEngine()
        self.sports_engine = SportsEngine()
        self.active_directive = active_directive
        self._system_prompt_cached = None

    def _resolve_llm_provider(self) -> str:
        """Choose a provider without accidentally making paid API calls."""
        provider = getattr(settings, "llm_provider", "anthropic").lower()
        if provider == "openai" and not settings.openai_api_key:
            print("⚠️  OPENAI_API_KEY is not set; using mock LLM mode.")
            return "mock"
        if provider == "anthropic" and not settings.anthropic_api_key:
            print("⚠️  ANTHROPIC_API_KEY is not set; using mock LLM mode.")
            return "mock"
        if provider not in {"anthropic", "openai", "mock"}:
            print(f"⚠️  Unknown LLM_PROVIDER={provider!r}; using mock LLM mode.")
            return "mock"
        return provider

    def _get_system_prompt(self) -> str:
        base = self.memory.inject_lessons_into_prompt(SYSTEM_PROMPT)
        if self.active_directive:
            base += f"\n\n[MARKET INTEL RECON DIRECTIVE]\nYou are currently operating under a strict tactical directive from the Recon Engine. Prioritize these instructions immediately:\n{self.active_directive}\n"
        return base

    def scan_markets(self, markets: list[Market], mode: str = "live") -> list[str]:
        """
        Quick scan to identify which markets are worth researching.
        Uses a cheaper/faster call — no extended thinking needed.

        Returns list of market IDs to research deeper.
        """
        if not markets:
            return []

        # Format markets for Claude
        markets_data = []
        if markets:
            m_count = len(markets)
            configured_limit = getattr(settings, "market_scan_limit", 0)
            m_limit = m_count if configured_limit <= 0 else min(configured_limit, m_count)
            for i in range(m_limit):
                m = markets[i]
                y_p = float(getattr(m, "yes_price", 0.5))
                # Kalshi's volume_24h is almost always 0 — use total_volume instead
                vol = float(getattr(m, "total_volume", 0) or getattr(m, "volume_24h", 0))
                markets_data.append({
                    "id": getattr(m, "id", "unknown"),
                    "question": getattr(m, "question", "unknown"),
                    "category": getattr(m, "category", "unknown"),
                    "yes_price": float(f"{y_p:.3f}"),
                    "volume": round(vol),
                    "end_date": m.end_date.isoformat() if hasattr(m, "end_date") and m.end_date else "unknown",
                })

        if mode == "snipe_btc":
            from config.prompts import CRYPTO_SNIPE_PROMPT  # type: ignore
            prompt_template = CRYPTO_SNIPE_PROMPT
        elif mode == "snipe_sports":
            from config.prompts import SPORTS_SNIPE_PROMPT  # type: ignore
            prompt_template = SPORTS_SNIPE_PROMPT
        else:
            prompt_template = MARKET_SCAN_PROMPT
            
        prompt = prompt_template.format(
            markets_json=json.dumps(markets_data, indent=2)
        )

        if self.llm_provider == "mock":
            return self._mock_scan_markets(markets)
        if self.llm_provider == "openai":
            return self._scan_markets_openai(prompt)

        try:
            if self.client is None:
                return self._mock_scan_markets(markets)
            response = self.client.messages.create(
                model=settings.claude_scan_model,  # Use cheaper model for scanning
                max_tokens=1024,
                thinking={"type": "disabled"},
                system=self._get_system_prompt(),
                messages=[{"role": "user", "content": prompt}],
            )

            # Parse the response
            text = response.content[0].text
            # Extract JSON from response
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                selections = json.loads(text[start:end])
                return [s["market_id"] for s in selections if "market_id" in s]
        except Exception as e:
            print(f"⚠️  Scan failed: {e}")

        return []

    def _mock_scan_markets(self, markets: list[Market]) -> list[str]:
        """Return every market ID for UI/dev flow without making LLM calls."""
        return [getattr(m, "id", "") for m in markets if getattr(m, "id", "")]

    def _scan_markets_openai(self, prompt: str) -> list[str]:
        """Use ChatGPT/OpenAI for the cheap market scan path."""
        if self.openai_client is None:
            return []

        try:
            response = self.openai_client.chat.completions.create(
                model=settings.openai_scan_model,
                max_completion_tokens=1024,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
            )
            text = response.choices[0].message.content or ""
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                selections = json.loads(text[start:end])
                return [s["market_id"] for s in selections if "market_id" in s]
        except Exception as e:
            print(f"⚠️  OpenAI scan failed: {e}")

        return []

    def research_and_analyze(
        self,
        market: Market,
        positions: list[Position],
        balance: float,
        mode: str = "live",
        depth: str = "fast",
        log_callback = None,
    ) -> TradeProposal | None:
        """
        Deep research and analysis of a single market.
        Uses extended thinking for careful probability estimation.
        Uses web search tool for real-time information gathering.

        Returns a TradeProposal or None if analysis fails.
        """
        # Get memory context
        recent_lessons = self.memory.get_lessons(limit=10)
        memory_text = "\n".join([f"- {m['lesson']} (Outcome: {m['outcome']})" for m in recent_lessons]) if recent_lessons else "No past lessons recorded yet."

        macro_lessons = self.memory.get_active_lessons()
        probationary_lessons = self.memory.get_probationary_lessons()
        macro_parts: list[str] = []
        if macro_lessons:
            macro_parts.append("PERMANENT RULES (validated, NEVER violate):\n" + "\n".join([f"- {m}" for m in macro_lessons]))
        if probationary_lessons:
            macro_parts.append("EMERGING PATTERNS (consider but not binding):\n" + "\n".join([f"- {m}" for m in probationary_lessons]))
        macro_text = "\n\n".join(macro_parts) if macro_parts else "No permanent macro-lessons established yet."

        # WEATHER SUPERPOWER INTEGRATION
        weather_data_context = ""
        if market.category == "Weather":
            weather_analysis = self.weather_engine.analyze_market(market)
            if weather_analysis:
                weather_data_context = f"""
[WEATHER ENGINE GROUND TRUTH DATA]
The high-precision weather engine has analyzed this market using GFS Ensembles, HRRR (High-Res) models, and real-time METAR observations.
- DETERMINISTIC PROBABILITY: {weather_analysis['probability']*100:.1f}%
- FORECASTER INSIGHT: {weather_analysis.get('forecaster_insight', 'No specific insight.')}
- RAW ENSEMBLE MEAN: {weather_analysis.get('ensemble_mean', 'N/A')}
- THRESHOLD: {weather_analysis.get('threshold', 'N/A')}
- CITY: {weather_analysis.get('city', 'Unknown')}

INSTRUCTION: You MUST incorporate this quantitative grounding into your analysis. 
The DETERMINISTIC PROBABILITY is a statistically rigorous calculation based on 31 ensemble members and current short-term observations. 
Weight this highly unless your web research shows a specific extreme anomaly the model might miss (e.g. record-breaking hurricane landfall just outside the grid).
"""
        
        if mode in ("climate_paper", "climate_live"):
            climate_context = self.climate_engine.get_climate_context()
            weather_data_context = f"""
[OFFICIAL NOAA/NHC CLIMATE GROUND TRUTH]
The following reports were automatically retrieved directly from the National Weather Service and National Hurricane Center. You MUST read them carefully to determine active tropical cyclones, long-term temperature outlooks, and precipitation mapping. Use this as your foundation before doing web search.

{climate_context}

INSTRUCTION: 
1. If this market requires predicting a random, inherently unpredictable event (e.g. Earthquakes, Solar Flares, localized non-weather disasters) with zero evidence, you MUST output a direction of "HOLD" and state your reasoning is "Unpredictable market."
2. If the data provides clear probabilities for the market (e.g. Hurricanes, month-long temperatures), leverage it deeply.
"""

        if mode == "snipe_sports":
            from config.prompts import SPORTS_RESEARCH_PROMPT  # type: ignore
            prompt = SPORTS_RESEARCH_PROMPT.format(
                question=market.question,
                category=market.category,
                yes_price=market.yes_price,
                no_price=market.no_price,
                volume=market.volume_24h,
                end_date=market.end_date.isoformat() if market.end_date else "unknown",
                resolution_source=market.resolution_source,
                available_capital=balance,
                current_positions=len(positions),
                daily_pnl=self.risk_manager.daily_stats.total_pnl,  # Tracked from risk manager
                max_trade=settings.max_single_trade,
                memory_context=memory_text,
                macro_lessons_context=macro_text,
            )
            # SPORTS ENGINE: inject structured ESPN data
            sports_matchup = self.sports_engine.analyze_market(market)
            if sports_matchup:
                prompt += "\n" + sports_matchup.to_prompt_context()
        else:
            from config.prompts import RESEARCH_PROMPT_TEMPLATE  # type: ignore
            prompt = RESEARCH_PROMPT_TEMPLATE.format(
                question=market.question,
                category=market.category,
                yes_price=market.yes_price,
                no_price=market.no_price,
                volume=market.volume_24h,
                end_date=market.end_date.isoformat() if market.end_date else "unknown",
                resolution_source=market.resolution_source,
                available_capital=balance,
                current_positions=len(positions),
                daily_pnl=self.risk_manager.daily_stats.total_pnl,  # Tracked from risk manager
                max_trade=settings.max_single_trade,
                memory_context=memory_text,
                macro_lessons_context=macro_text,
            )
            if weather_data_context:
                prompt += weather_data_context
            # SPORTS ENGINE: inject structured data for sports-category markets
            elif market.category.lower() in ("sports", "sport"):
                sports_matchup = self.sports_engine.analyze_market(market)
                if sports_matchup:
                    prompt += "\n" + sports_matchup.to_prompt_context()

        if depth == "deep":
            from config.prompts import DEVILS_ADVOCATE_PROMPT  # type: ignore
            prompt += DEVILS_ADVOCATE_PROMPT

        if self.llm_provider == "mock":
            return self._mock_trade_proposal(market)
        if self.llm_provider == "openai":
            return self._research_and_analyze_openai(
                prompt=prompt,
                market=market,
                balance=balance,
                depth=depth,
                log_callback=log_callback,
            )

        messages = [{"role": "user", "content": prompt}]
        
        max_iterations = 15 if depth == "deep" else 8

        try:
            # Agentic loop — Claude may call tools multiple times
            for iteration in range(max_iterations):
                if self.client is None:
                    return self._mock_trade_proposal(market)
                response = self.client.messages.create(
                    model=settings.claude_model,
                    max_tokens=12000,
                    system=[{
                        "type": "text",
                        "text": self._get_system_prompt() + (
                            "\n\n<deep_research_mode>ACTIVE: Perform multiple searches. If initial results are vague, try more specific keywords. "
                            "Look for primary sources and non-US perspectives for political/corporate markets.</deep_research_mode>"
                            if depth == "deep" else ""
                        ),
                        "cache_control": {"type": "ephemeral"}  # Cache for cost savings
                    }],
                    tools=AGENT_TOOLS,
                    messages=messages,
                )

                # Collect assistant response
                messages.append({"role": "assistant", "content": response.content})

                # Check if Claude is done (no more tool calls)
                if response.stop_reason == "end_turn":
                    break

                # Process blocks and tool calls
                tool_results = []
                for block in response.content:
                    b_type = getattr(block, "type", None)
                    if b_type == "text":
                        b_text = getattr(block, "text", "")
                        if b_text.strip():
                            if log_callback:
                                import re
                                b_text_clean = str(b_text).strip()
                                first_sentence = str(re.split(r'(?<=[.!?])\s+', b_text_clean)[0])
                                snippet = str(first_sentence)[0:90] + ("..." if len(first_sentence) > 90 else "")
                                log_callback(f"💭 {snippet}")

                    elif b_type == "tool_use":
                        b_name = getattr(block, "name", "")
                        b_input = getattr(block, "input", {})
                        b_id = getattr(block, "id", "")
                        
                        if log_callback:
                            if b_name == "web_search":
                                query = b_input.get("query", "")
                                log_callback(f"🧠 Claude is searching: '{query}'")
                            elif b_name == "submit_analysis":
                                log_callback("🧠 Claude is finalizing mathematical calculations...")
                                
                        result = self._execute_tool(b_name, b_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": b_id,
                            "content": json.dumps(result),
                        })

                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                else:
                    break

            # Extract the trade proposal from the conversation
            proposal = self._extract_proposal(messages, market, balance)
            if proposal and depth == "deep":
                proposal.is_deep_research = True
            return proposal

        except Exception as e:
            print(f"⚠️  Analysis failed for {market.id}: {e}")
            return None

    def _research_and_analyze_openai(
        self,
        prompt: str,
        market: Market,
        balance: float,
        depth: str = "fast",
        log_callback = None,
    ) -> TradeProposal | None:
        """Deep research path using ChatGPT/OpenAI tool calling."""
        if self.openai_client is None:
            return self._mock_trade_proposal(market)

        system_prompt = self._get_system_prompt() + (
            "\n\n<deep_research_mode>ACTIVE: Perform multiple searches. If initial results are vague, try more specific keywords. "
            "Look for primary sources and non-US perspectives for political/corporate markets.</deep_research_mode>"
            if depth == "deep" else ""
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        max_iterations = 15 if depth == "deep" else 8
        submitted_analysis: dict[str, Any] | None = None
        total_text = ""

        try:
            for _ in range(max_iterations):
                response = self.openai_client.chat.completions.create(
                    model=settings.openai_model,
                    max_completion_tokens=4096,
                    tools=_openai_tools(),
                    messages=messages,
                )
                message = response.choices[0].message
                message_dict = message.model_dump(exclude_none=True)
                messages.append(message_dict)

                if message.content:
                    total_text += message.content
                    if log_callback:
                        import re
                        text_clean = str(message.content).strip()
                        first_sentence = str(re.split(r'(?<=[.!?])\s+', text_clean)[0])
                        snippet = first_sentence[0:90] + ("..." if len(first_sentence) > 90 else "")
                        log_callback(f"💭 ChatGPT: {snippet}")

                tool_calls = message.tool_calls or []
                if not tool_calls:
                    break

                for tool_call in tool_calls:
                    name = tool_call.function.name
                    try:
                        input_data = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        input_data = {}

                    if log_callback:
                        if name == "web_search":
                            log_callback(f"🧠 ChatGPT is searching: '{input_data.get('query', '')}'")
                        elif name == "submit_analysis":
                            log_callback("🧠 ChatGPT is finalizing mathematical calculations...")

                    if name == "submit_analysis":
                        submitted_analysis = input_data

                    result = self._execute_tool(name, input_data)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })

                if submitted_analysis is not None:
                    break

            if submitted_analysis is not None:
                proposal = self._proposal_from_analysis_data(submitted_analysis, market, balance)
                if proposal and depth == "deep":
                    proposal.is_deep_research = True
                return proposal

            return self._extract_text_proposal(total_text, market, balance)
        except Exception as e:
            print(f"⚠️  OpenAI analysis failed for {market.id}: {e}")
            return None

    def _mock_trade_proposal(self, market: Market) -> TradeProposal:
        """Return a safe non-trading proposal when no paid LLM is configured."""
        return TradeProposal(
            market_id=market.id,
            market_question=market.question,
            category=market.category,
            direction="HOLD",
            estimated_prob=market.yes_price,
            market_price=market.yes_price,
            edge=0.0,
            r_score=0.0,
            confidence="LOW",
            position_size_usd=0.0,
            reasoning=(
                "Mock LLM mode is active, so no paid model was called. "
                "Set OPENAI_API_KEY with LLM_PROVIDER=openai or ANTHROPIC_API_KEY "
                "with LLM_PROVIDER=anthropic to enable real analysis."
            ),
            risk_factors=["mock_llm_mode_no_trade"],
        )

    def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call from Claude."""

        if name == "web_search":
            query = input_data.get("query", "")
            # Encouragement for deep search if the query is too generic
            return self._web_search(query)

        elif name == "submit_analysis":
            # This is captured in _extract_proposal
            return {"status": "analysis_recorded"}

        return {"error": f"Unknown tool: {name}"}

    def _web_search(self, query: str) -> dict:
        """
        Perform a real web search using Tavily if configured,
        otherwise fallback to DuckDuckGo search.
        """
        tavily_key = getattr(settings, "tavily_api_key", None)
        if not tavily_key:
            try:
                from ddgs import DDGS  # type: ignore
                results = []
                with DDGS() as ddgs:
                    for r in ddgs.text(query, max_results=5):
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "content": r.get("body", "")[:500],
                            "score": 1.0
                        })
                return {
                    "query": query,
                    "results": results,
                    "source": "duckduckgo",
                }
            except Exception as e:
                return {
                    "query": query,
                    "error": "web_search_unavailable",
                    "source": "none",
                    "note": f"Tavily not configured, and DuckDuckGo fallback failed: {e}",
                }

        try:
            import httpx  # type: ignore

            with httpx.Client(timeout=20.0) as client:
                resp = client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": query,
                        "search_depth": "advanced",
                        "max_results": 5,
                        "include_answer": False,
                    },
                )
                resp.raise_for_status()

            payload = resp.json()
            results = []
            for item in payload.get("results", []):
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "")[:500],
                    "score": item.get("score"),
                })

            return {
                "query": query,
                "results": results,
                "source": "tavily",
            }
        except Exception as e:
            return {"query": query, "error": str(e)}

    def _extract_proposal(
        self,
        messages: list,
        market: Market,
        balance: float,
    ) -> TradeProposal | None:
        """Extract the trade proposal from Claude's conversation."""
        # Look for submit_analysis tool calls
        for msg in messages:
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    b_type = getattr(block, "type", None)
                    if b_type == "tool_use":
                        b_name = getattr(block, "name", "")
                        if b_name == "submit_analysis":
                            data = getattr(block, "input", {})
                            return self._proposal_from_analysis_data(data, market, balance)

        # If no tool call found, try to parse from text
        import re
        total_text = ""
        for msg in messages:
            if isinstance(msg.get("content"), str):
                total_text += msg["content"]
            elif isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if hasattr(block, "type") and block.type == "text":
                        total_text += block.text
                    elif isinstance(block, dict) and block.get("type") == "text":
                        total_text += block.get("text", "")

        # Look for JSON block
        json_match = re.search(r'\{.*"my_probability".*\}', total_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return self._proposal_from_analysis_data(data, market, balance)
            except Exception as e:
                print(f"⚠️  JSON extraction failed: {e}")

        return TradeProposal(
            market_id=market.id,
            market_question=market.question,
            category=market.category,
            direction="HOLD",
            estimated_prob=market.yes_price,
            market_price=market.yes_price,
            edge=0.0,
            r_score=0.0,
            confidence="LOW",
            position_size_usd=0.0,
            reasoning="Could not extract structured analysis — defaulting to HOLD",
            risk_factors=["analysis_extraction_failed"],
        )

    def _extract_text_proposal(self, text: str, market: Market, balance: float) -> TradeProposal | None:
        """Parse a JSON proposal from plain model text."""
        import re

        json_match = re.search(r'\{.*"my_probability".*\}', text, re.DOTALL)
        if not json_match:
            return self._mock_trade_proposal(market)
        try:
            data = json.loads(json_match.group(0))
            return self._proposal_from_analysis_data(data, market, balance)
        except Exception as e:
            print(f"⚠️  OpenAI JSON extraction failed: {e}")
            return self._mock_trade_proposal(market)

    def _proposal_from_analysis_data(
        self,
        data: dict[str, Any],
        market: Market,
        balance: float,
    ) -> TradeProposal:
        """Build a TradeProposal from either Claude or ChatGPT structured output."""
        model_probability = float(data.get("my_probability", 0.5))
        executable_yes_ask = market.yes_price
        edge = abs(model_probability - executable_yes_ask)

        direction = data.get("direction", "HOLD")
        entry_direction = infer_entry_direction(
            direction=direction,
            estimated_prob=model_probability,
            market_price=executable_yes_ask,
        )

        size = 0.0
        r_score_val = 0.0
        if entry_direction:
            r_score_val = self.risk_manager.calculate_r_score(
                estimated_prob=model_probability,
                market_price=executable_yes_ask,
                direction=entry_direction
            )
            # Kalshi owns fee, payout, and final order-preview math. The app
            # requests only a small configured paper stake for review.
            size = min(settings.min_position_size, max(0.0, balance))

        return TradeProposal(
            market_id=data.get("market_id", market.id),
            market_question=data.get("market_question", market.question),
            category=market.category,
            direction=direction,
            estimated_prob=model_probability,
            market_price=executable_yes_ask,
            edge=edge,
            r_score=r_score_val,
            confidence=data.get("confidence", "LOW"),
            position_size_usd=size,
            reasoning=data.get("reasoning_summary", data.get("reasoning", "")),
            target_entry_price=data.get("target_entry_price", 0.0),
            risk_factors=data.get("risk_factors", []),
            entry_direction=entry_direction,
        )

    async def generate_lesson(self, market_id: str, question: str, reasoning: str, outcome_pnl: float, outcome_msg: str) -> str:
        """
        Ask Claude to extract a 1-sentence lesson from a closed trade.
        """
        prompt = f"""
        We just closed a trade. Please formulate a single-sentence lesson learned from this experience to improve our future trading.

        Market: {question}
        Our Original Reasoning: {reasoning}
        Trade Outcome: {outcome_msg} (P&L: ${outcome_pnl:.2f})

        What is the key takeaway? Keep it to ONE concise sentence. Do not include introductory text like "The lesson is...".
        """
        if self.llm_provider == "mock":
            return ""

        try:
            if self.llm_provider == "openai" and self.openai_client is not None:
                response = await asyncio.to_thread(
                    self.openai_client.chat.completions.create,
                    model=settings.openai_scan_model,
                    max_completion_tokens=100,
                    messages=[{"role": "user", "content": prompt}],
                )
                lesson = (response.choices[0].message.content or "").strip()
            elif self.client is not None:
                response = await asyncio.to_thread(
                    self.client.messages.create,
                    model=settings.claude_model,
                    max_tokens=100,
                    thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": prompt}],
                )
                lesson = response.content[0].text.strip()
            else:
                return ""
            self.memory.add_lesson(market_id, question, outcome_msg, lesson)
            return lesson
        except Exception as e:
            print(f"⚠️  Failed to generate lesson: {e}")
            return ""
