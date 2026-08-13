"""
Advanced Self-Improving Trade Analyzer

Uses multiple randomized analysis lenses to examine past trades from different
perspectives, validates lessons against real evidence, deduplicates semantically,
and manages a probationary → active → retired lifecycle for all learned rules.
"""

import json
import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from anthropic import Anthropic
from config.settings import settings
from config.prompts import SYSTEM_PROMPT
from config.paths import TRADE_LOG_PATH
from core.memory_manager import MemoryManager

# ── Analysis Lenses ──────────────────────────────────────────
# Each lens examines the same trade history from a different angle.
# 2 of 5 are randomly selected per run to maximize perspective diversity.

LENS_CALIBRATION_AUDIT = """
ANALYSIS METHOD: Calibration Audit (Mathematical)

You have a log of past trade analyses with `estimated_probability` and `market_price`, plus resolutions showing actual outcomes.

{trades_context}

Perform a QUANTITATIVE calibration check:
1. Group predictions by confidence bucket (e.g. 0-20%, 20-40%, etc.)
2. Compare how often the agent's predicted probability matched reality
3. Identify systematic over-confidence or under-confidence patterns
4. Calculate something like a Brier score: mean of (predicted - actual)^2

Based on this mathematical analysis, extract 1-2 lessons about HOW the agent's probability estimation is miscalibrated.
Do NOT give vague lessons. Be specific: "The agent overestimates YES probability by ~15% on political markets" is good. "Be more careful" is bad.

{output_format}
"""

LENS_CATEGORY_AUTOPSY = """
ANALYSIS METHOD: Category Autopsy

You have a log of past trade analyses grouped by market category, plus resolutions.

{trades_context}

Analyze performance BY CATEGORY:
1. Which categories had the worst win rate or most negative P&L?
2. Which categories does the agent systematically misjudge?
3. Are there categories where the agent's reasoning style doesn't match the domain? (e.g., applying political analysis to sports)

Focus on categories where the flaw is SYSTEMATIC, not random bad luck. A category with 2 losses could be variance; a category with 8 losses is a pattern.

{output_format}
"""

LENS_REASONING_CRITIQUE = """
ANALYSIS METHOD: Reasoning Critique

You have a log of past trade analyses that include the agent's raw reasoning.

{trades_context}

Re-read the agent's reasoning for LOSING trades and identify logical fallacies or weak evidence patterns:
1. Did the agent rely on speculation instead of data?
2. Did it anchor too heavily on a single data point?
3. Did it ignore base rates?
4. Did it confuse "interesting edge" with "tradeable edge" (ignoring transaction costs)?
5. Did it attempt to trade markets it demonstrably cannot analyze (e.g., live sports without real-time data)?

Extract lessons about REASONING PROCESS flaws, not outcome-based complaints.

{output_format}
"""

LENS_COUNTERFACTUAL = """
ANALYSIS METHOD: Counterfactual Review

You have a log of past trade analyses and their outcomes.

{trades_context}

For each LOSING trade (or trade where the agent HELD but shouldn't have), answer:
1. What would the OPTIMAL action have been given information available at the time?
2. What specific signal did the agent miss or misweigh?
3. Is this a repeatable mistake, or was it genuinely unforeseeable?

Only extract lessons from situations where the agent COULD have known better. Ignore pure bad luck.

{output_format}
"""

LENS_EDGE_DECAY = """
ANALYSIS METHOD: Edge Decay Analysis

You have a log of past trade analyses showing reported `edge` values and actual P&L from resolutions.

{trades_context}

Analyze whether the agent's reported edges are real or illusory:
1. Compare claimed edge percentages to actual realized P&L
2. Are there market types where the agent claims large edges but consistently loses?
3. Did the user-confirmed Kalshi checkout totals materially differ from the quote recorded by the agent?
4. Is there evidence that the agent's edges shrink or vanish by settlement time?

Focus on the gap between CLAIMED edge and REALIZED edge. This is the most direct measure of overconfidence.

{output_format}
"""

ALL_LENSES = {
    "calibration_audit": LENS_CALIBRATION_AUDIT,
    "category_autopsy": LENS_CATEGORY_AUTOPSY,
    "reasoning_critique": LENS_REASONING_CRITIQUE,
    "counterfactual_review": LENS_COUNTERFACTUAL,
    "edge_decay_analysis": LENS_EDGE_DECAY,
}

OUTPUT_FORMAT = """
Return ONLY a JSON array of objects. Each object must have:
- "lesson": A specific, actionable rule (string)
- "evidence_ids": An array of 2+ market_id strings from the trade history above that support this lesson
- "confidence": A float 0.0-1.0 representing how confident you are this is a real pattern vs. noise

Example:
[
  {
    "lesson": "Stop trading 3+ leg parlays; compound probability uncertainty and transaction costs erode all edges.",
    "evidence_ids": ["KXMVESPORTS-123", "KXMVECROSS-456"],
    "confidence": 0.85
  }
]

CRITICAL RULES:
- Every lesson MUST cite real market_id values from the history above. If you cannot find 2+ supporting examples, do NOT include that lesson.
- Do NOT fabricate market IDs. Only use IDs that appear in the trade data.
- If you cannot find any systematic patterns, return an empty array: []
"""

SEMANTIC_DEDUP_PROMPT = """
I have an existing set of learned lessons for a trading bot:

EXISTING LESSONS:
{existing_lessons}

A new candidate lesson has been proposed:
NEW LESSON: "{new_lesson}"

Determine if this new lesson is REDUNDANT with any existing lesson. Be AGGRESSIVE about marking lessons as redundant. Two lessons are redundant if ANY of the following are true:
1. They give the same core actionable advice (e.g. "avoid fees" and "fees destroy profits" are redundant)
2. They address the same topic/domain with the same conclusion (e.g. "sports parlays are unprofitable" and "stop trading multi-leg sports bets" are redundant)
3. The new lesson would NOT change the bot's behavior beyond what existing lessons already enforce

When in doubt, mark as redundant. We want a SMALL set of non-overlapping rules, not a bloated list of similar advice.

Respond with ONLY a JSON object with two keys:
- "redundant": true or false (boolean)
- "reason": a brief explanation string

Example response:
{"redundant": true, "reason": "Both lessons say to avoid low-volume markets."}
"""


class TradeAnalyzer:
    """
    Advanced self-improving analyzer that uses multiple randomized lenses,
    validates lessons against evidence, prevents hallucination, and manages
    lesson lifecycle (probationary → active → retired).
    """

    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.memory = MemoryManager()

    def _load_trades(self) -> list[dict]:
        """Load and prepare trade history from the log file."""
        trades: list[dict] = []
        try:
            log_path = TRADE_LOG_PATH
            if log_path.exists():
                with open(log_path, "r") as f:
                    for line in f.read().splitlines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("type") in ["analysis", "resolution"]:
                            if "reasoning" in data:
                                r = data["reasoning"]
                                data["reasoning"] = r[:300] + "..." if len(r) > 300 else r
                            trades.append(data)
        except Exception as e:
            print(f"Error loading trades for reflection: {e}")
        return trades[-100:]

    def _get_valid_market_ids(self, trades: list[dict]) -> set[str]:
        """Extract all real market IDs from trade history for hallucination checking."""
        ids: set[str] = set()
        for t in trades:
            mid = t.get("market_id")
            if mid and mid != "unknown":
                ids.add(mid)
        return ids

    async def _call_claude(self, prompt: str) -> str:
        """Send a prompt to Claude and return the raw text response."""
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=settings.claude_model,
            max_tokens=2048,
            thinking={"type": "disabled"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    async def _run_lens(self, lens_name: str, lens_template: str, trades_context: str) -> list[dict]:
        """Run a single analysis lens and return validated lessons."""
        prompt = lens_template.replace("{trades_context}", trades_context).replace("{output_format}", OUTPUT_FORMAT)

        try:
            text = await self._call_claude(prompt)
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                raw_lessons = json.loads(text[start:end])
                if isinstance(raw_lessons, list):
                    # Tag each lesson with the lens that produced it
                    for lesson in raw_lessons:
                        if isinstance(lesson, dict):
                            lesson["lens"] = lens_name
                    return [l for l in raw_lessons if isinstance(l, dict)]
        except Exception as e:
            print(f"Lens '{lens_name}' failed: {e}")
        return []

    def _anti_hallucination_check(self, lesson: dict, valid_ids: set[str]) -> bool:
        """Verify a lesson is grounded in real evidence, not hallucinated."""
        # Must have a lesson string
        if not lesson.get("lesson") or not isinstance(lesson["lesson"], str):
            return False

        # Must have confidence >= 0.6
        confidence = lesson.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or confidence < 0.6:
            return False

        # Must cite at least 2 real market IDs from the actual data
        evidence_ids = lesson.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or len(evidence_ids) < 2:
            return False

        real_evidence = [eid for eid in evidence_ids if eid in valid_ids]
        if len(real_evidence) < 2:
            return False

        return True

    async def _semantic_dedup_check(self, new_lesson: str, existing_lessons: list[dict]) -> bool:
        """Returns True if the lesson is redundant with existing ones."""
        if not existing_lessons:
            return False

        existing_text = "\n".join([f"- {l.get('lesson', l) if isinstance(l, dict) else l}" for l in existing_lessons])
        prompt = SEMANTIC_DEDUP_PROMPT.replace("{existing_lessons}", existing_text).replace("{new_lesson}", new_lesson)

        try:
            text = await self._call_claude(prompt)
            # Try JSON parse first
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    result = json.loads(text[start:end])
                    return result.get("redundant", False)
                except json.JSONDecodeError:
                    pass
            # Fallback: look for "redundant": true or "redundant": false in raw text
            import re
            match = re.search(r'"?redundant"?\s*[:=]\s*(true|false)', text, re.IGNORECASE)
            if match:
                return match.group(1).lower() == "true"
        except Exception as e:
            print(f"Semantic dedup check failed: {e}")
        return False

    async def run_deep_reflection(self) -> dict:
        """
        Main entry point. Randomly selects 2 analysis lenses, runs them,
        validates results through anti-hallucination and dedup gates,
        then stores surviving lessons as probationary.

        Returns a dict with analysis results for the UI.
        """
        trades = self._load_trades()
        if not trades:
            return {
                "status": "no_data",
                "lenses_used": [],
                "lessons_generated": 0,
                "lessons_accepted": 0,
                "lessons": [],
                "message": "No trade history available yet.",
            }

        valid_ids = self._get_valid_market_ids(trades)
        trades_context = json.dumps(trades, indent=2)

        # Randomly select 2 of 5 lenses
        selected_lens_names = random.sample(list(ALL_LENSES.keys()), min(2, len(ALL_LENSES)))
        print(f"[Analyzer] Selected lenses: {selected_lens_names}")

        # Run selected lenses in parallel
        all_candidates: list[dict] = []
        tasks = [
            self._run_lens(name, ALL_LENSES[name], trades_context)
            for name in selected_lens_names
        ]
        results = await asyncio.gather(*tasks)
        for result in results:
            all_candidates.extend(result)

        print(f"[Analyzer] Raw candidates from Claude: {len(all_candidates)}")

        # Anti-hallucination gate
        grounded = [l for l in all_candidates if self._anti_hallucination_check(l, valid_ids)]
        hallucinated_count = len(all_candidates) - len(grounded)
        print(f"[Analyzer] Passed anti-hallucination: {len(grounded)} (blocked {hallucinated_count})")

        # Semantic dedup gate
        existing_lessons = self.memory.get_macro_lessons()
        accepted: list[dict] = []
        for candidate in grounded:
            is_redundant = await self._semantic_dedup_check(candidate["lesson"], existing_lessons + accepted)
            if not is_redundant:
                accepted.append(candidate)
            else:
                print(f"[Analyzer] Dedup blocked: {candidate['lesson'][:60]}...")

        print(f"[Analyzer] After dedup: {len(accepted)} lessons accepted")

        # Calculate PnL snapshot for lesson validation tracking
        current_pnl = self._calculate_total_pnl(trades)

        # Store as probationary lessons with metadata
        for lesson in accepted:
            self.memory.add_macro_lesson_with_metadata(
                lesson=lesson["lesson"],
                lens=lesson.get("lens", "unknown"),
                evidence_ids=lesson.get("evidence_ids", []),
                confidence=lesson.get("confidence", 0.0),
                pnl_at_creation=current_pnl,
            )

        return {
            "status": "success",
            "lenses_used": selected_lens_names,
            "lessons_generated": len(all_candidates),
            "hallucinated_blocked": hallucinated_count,
            "dedup_blocked": len(grounded) - len(accepted),
            "lessons_accepted": len(accepted),
            "lessons": [
                {
                    "lesson": l["lesson"],
                    "lens": l.get("lens", "unknown"),
                    "confidence": l.get("confidence", 0),
                    "evidence_ids": l.get("evidence_ids", []),
                }
                for l in accepted
            ],
            "message": f"Analyzed {len(trades)} trades using {', '.join(selected_lens_names)}. "
                       f"Generated {len(all_candidates)} candidates, accepted {len(accepted)} after validation.",
        }

    def _calculate_total_pnl(self, trades: list[dict]) -> float:
        """Sum up P&L from all resolution entries."""
        total = 0.0
        for t in trades:
            if t.get("type") == "resolution":
                total += t.get("pnl", 0.0)
        return total

    def check_and_retire_lessons(self):
        """
        Called periodically to check if probationary lessons have proven
        themselves. After 10 post-lesson trades, compare P&L and retire
        lessons that didn't improve performance.
        """
        trades = self._load_trades()
        resolutions = [t for t in trades if t.get("type") == "resolution"]
        current_pnl = self._calculate_total_pnl(trades)

        lessons = self.memory.get_macro_lessons_raw()
        changed = False

        for lesson in lessons:
            if lesson.get("status") != "probationary":
                continue

            added_at = lesson.get("added_at", "")
            # Count how many resolutions happened after this lesson was added
            trades_after = sum(1 for r in resolutions if r.get("timestamp", "") > added_at)

            if trades_after >= 10:
                pnl_at_creation = lesson.get("pnl_at_creation", 0.0)
                pnl_since = current_pnl - pnl_at_creation

                if pnl_since > 0:
                    lesson["status"] = "active"
                    lesson["trades_after_count"] = trades_after
                    lesson["trades_after_pnl"] = pnl_since
                    print(f"[Analyzer] PROMOTED lesson: {lesson['lesson'][:60]}... (PnL: +${pnl_since:.2f})")
                else:
                    lesson["status"] = "retired"
                    lesson["trades_after_count"] = trades_after
                    lesson["trades_after_pnl"] = pnl_since
                    lesson["retired_reason"] = f"No improvement after {trades_after} trades (PnL: ${pnl_since:.2f})"
                    print(f"[Analyzer] RETIRED lesson: {lesson['lesson'][:60]}... (PnL: ${pnl_since:.2f})")
                changed = True

        if changed:
            self.memory.save_macro_lessons_raw(lessons)


    async def generate_recommendations(self, state_data: dict) -> list[dict]:
        """
        Analyzes the current bot state, portfolio, and learned lessons to recommend
        what the user should do next (e.g., change mode, focus on specific markets).
        """
        prompt = f"""
You are the strategic advisor for a Kalshi prediction market trading bot.
Analyze the current state and provide 2-3 specific recommendations for what the user should do next to maximize profits and minimize risk.

CURRENT STATE:
Balance: ${state_data.get('balance', 0):.2f}
Current Bot Mode: {state_data.get('mode', 'unknown')}
Open Positions: {json.dumps(state_data.get('open_positions', []), indent=2)}
Recent Trade Performance: {json.dumps(state_data.get('recent_performance', {}), indent=2)}
Active Macro-Lessons (Rules the bot follows): {json.dumps(state_data.get('lessons', []), indent=2)}

Available Bot Modes to recommend:
- omni_sweep: Guarantees trades by sweeping all markets (high risk)
- smart_st: Standard AI-driven short term trading
- snipe_btc: Only trades Bitcoin price markets
- close_snipe: Only trades markets resolving within 5 minutes
- paper_24h: Paper trading simulating 24h market resolutions
- paper: Standard paper trading (no real money risk)
- live: Standard live trading

Based on this data, return a JSON array of 2-3 recommendations. At least one recommendation MUST be a bot mode change (even if it's keeping the current mode but explaining why).

Output EXACTLY this JSON format and nothing else:
[
  {{
    "type": "mode_change",
    "action_value": "smart_st", 
    "title": "Switch to Smart ST-BOT",
    "rationale": "Given the recent losses in parlays and the fact that we have $4.88, Smart ST is the safest way to find high-probability single markets."
  }},
  {{
    "type": "market_advice",
    "action_value": "none",
    "title": "Avoid Long-Term Sports",
    "rationale": "Per your recent macro-lessons, long-term sports markets lack an edge. Stick to short-term crypto or politics."
  }}
]
"""
        try:
            text = await self._call_claude(prompt)
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                recs = json.loads(text[start:end])
                return recs
        except Exception as e:
            print(f"[Analyzer] Failed to generate recommendations: {e}")
            
        # Fallback recommendation if API fails
        return [{
            "type": "mode_change",
            "action_value": "paper",
            "title": "Switch to Paper Trading",
            "rationale": "The recommendation engine encountered an error. Safe default is paper trading."
        }]

    async def generate_market_intel(self, markets_data: list[dict], balance: float) -> dict | None:
        """
        Takes raw market data from Kalshi, passes it to Claude, and returns 
        the definitive "Analyze NOW" intel report with best trading windows, 
        estimated returns, and explicit bot directives.
        """
        # Format the market data tightly to save tokens
        market_summaries = []
        for m in markets_data[:40]:  # Top 40 to avoid token limits
            yes_ask = m.get("yes_ask", 0)
            no_ask = m.get("no_ask", 0)
            vol = m.get("volume", 0)
            vol_24h = m.get("volume_24h", 0)
            liq = m.get("liquidity", 0)
            cat = m.get("category", "unknown")
            end = m.get("end_date", "N/A")
            market_summaries.append(
                f"[TICKER:{m.get('ticker')}] TITLE:\"{m.get('title')}\" | "
                f"YesAsk:{yes_ask} NoAsk:{no_ask} Vol:{vol} 24hVol:{vol_24h} "
                f"Liq:{liq} Cat:{cat} Ends:{end}"
            )
            
        markets_str = "\n".join(market_summaries) if market_summaries else "NO MARKETS AVAILABLE"

        prompt = f"""
You are the elite Market Intel Recon Engine for a Kalshi prediction bot. 
Your job is to scan the currently active markets and build a highly specific attack plan.
The user's current balance is ${balance:.2f}.

HERE ARE THE TOP ACTIVE MARKETS RIGHT NOW:
{markets_str}

Please generate an "Analyze NOW" intel report containing:
1. The best opportunity for a 5-minute scalp window.
2. The best opportunity for a 15-minute trend window.
3. The best opportunity for a 60-minute window.
4. The best opportunity for a 4-hour window.
5. A transparent independent-probability comparison for your strongest recommendation. Do not calculate fees, payout, expected profit, breakeven, or Kelly size; Kalshi owns those checkout numbers.
6. A definitive strategy recommendation: which exact bot mode should the user run, and what is the overarching directive?

IMPORTANT FILTERS (strictly enforced):
- For EVERY window, ONLY recommend markets where your estimated payout_likelihood is >= 20%.
- If no market meets the 20% threshold for a window, return the best available and note the low confidence in the rationale.
- Never recommend a trade where you have less than 20% confidence it will pay out.

Available bot modes: omni_sweep, smart_st, snipe_btc, close_snipe, paper, live, weather_st, paper_15m

Output MUST be strictly valid JSON with no other text, wrapping, or markdown:
{{
  "windows": {{
    "5m": {{"market": "FULL market question/title string here (NOT the ticker ID)", "rationale": "string"}},
    "15m": {{"market": "FULL market question/title string here (NOT the ticker ID)", "rationale": "string"}},
    "60m": {{"market": "FULL market question/title string here (NOT the ticker ID)", "rationale": "string"}},
    "4h": {{"market": "FULL market question/title string here (NOT the ticker ID)", "rationale": "string"}}
  }},
  "probability_signal": {{
    "target_market": "string",
    "model_probability": number (0-100),
    "market_probability": number (0-100, use the bid/ask midpoint when available),
    "probability_gap": number (model probability minus market probability, in percentage points),
    "explanation": "string describing sourced evidence and calibration",
    "kalshi_checkout_note": "Verify final fees, total cost, and payout in Kalshi before placing."
  }},
  "strategy": {{
    "recommended_bot_mode": "string (must be from the list above)",
    "directive": "string (strict instructions for the bot to follow when it starts, e.g. 'Only trade crypto markets under 30c ask')"
  }}
}}
"""
        try:
            text = await self._call_claude(prompt)
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                report = json.loads(text[start:end])
                return report
        except Exception as e:
            print(f"[Analyzer] Failed to generate market intel: {e}")
            
        return None
