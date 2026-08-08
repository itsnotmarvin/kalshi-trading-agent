import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set
import anthropic  # type: ignore
from adapters.base import PlatformAdapter, Position  # type: ignore
from core.memory_manager import MemoryManager  # type: ignore
from config.settings import settings  # type: ignore
from config.prompts import POSTMORTEM_PROMPT  # type: ignore

class PostmortemEngine:
    """
    Analyzes settled trades to generate long-term 'MACRO-LESSONS'.
    This is the core of the bot's continuous learning system.
    """

    def __init__(self, memory_manager: MemoryManager, anthropic_api_key: str):
        self.memory = memory_manager
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.data_dir = Path("data/postmortems")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def run_analysis(self, adapter: PlatformAdapter):
        """Find recently settled trades and analyze them using trades.jsonl."""
        try:
            log_path = Path("trades.jsonl")
            if not log_path.exists():
                return
            
            # 1. Gather all analyses and their corresponding resolutions
            analyses = {}
            resolutions = []
            
            with open(log_path, "r") as f:
                for line in f.read().splitlines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("type") == "analysis":
                        analyses[data["market_id"]] = data
                    elif data.get("type") == "resolution":
                        resolutions.append(data)
            
            # 2. Identify resolutions that haven't been processed
            history_file = Path("data/postmortem_history.json")
            processed_ids: Set[str] = set()
            if history_file.exists():
                try:
                    with open(history_file, "r") as f:
                        processed_ids = set(json.load(f))
                except Exception:
                    pass

            newly_analyzed = []
            for res in resolutions:
                mid = res.get("market_id")
                if mid and mid not in processed_ids:
                    analysis = analyses.get(mid)
                    if analysis:
                        print(f"🧐 Running deep postmortem for {mid}: {analysis.get('question')[:50]}...")
                        # Enrich resolution with original analysis data
                        combined_data = {**analysis, "outcome": res.get("outcome", "Unknown"), "actual_pnl": res.get("pnl", 0.0)}
                        await self.analyze_trade(combined_data)
                        processed_ids.add(mid)
                        newly_analyzed.append(mid)
            
            if newly_analyzed:
                with open(history_file, "w") as f:
                    json.dump(list(processed_ids), f, indent=2)
                
        except Exception as e:
            print(f"⚠️ Postmortem run failed: {e}")

    async def analyze_trade(self, trade_data: dict):
        """Deep dive analysis on a single closed trade."""
        try:
            # In a real scenario, we'd use the web_search tool here.
            # Since this engine runs inside the server process, we'll give it a mini-agent capability.
            
            prompt = POSTMORTEM_PROMPT.format(
                question=trade_data.get("question", "Unknown"),
                category=trade_data.get("category", "General"),
                direction=trade_data.get("direction", "Unknown"),
                probability=trade_data.get("my_probability", 0.5),
                outcome=trade_data.get("outcome", "Unknown"),
                reasoning=trade_data.get("reasoning_summary", "No original reasoning found.")
            )
            
            # We don't have the full agent loop here, so we do a single-pass reflection for now.
            # In a 'Deep Research' phase, we'd allow Claude to search before this step.
            response = self.client.messages.create(
                model=settings.claude_model,
                max_tokens=1000,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}]
            )
            
            analysis_text = response.content[0].text
            
            # Extract lessons (Looking for "[CATEGORY/TOPIC]: ...")
            lessons = []
            for line in analysis_text.split("\n"):
                if "[" in line and "]:" in line:
                    lessons.append(line.strip())
            
            if lessons:
                print(f"📝 Learned {len(lessons)} new macro-lessons.")
                self.memory.add_macro_lessons(lessons)
            
            # Save the full postmortem report
            report_path = self.data_dir / f"{trade_data['market_id']}_{datetime.now().strftime('%Y%m%d')}.md"
            with open(report_path, "w") as f:
                f.write(f"# Postmortem: {trade_data['market_id']}\n\n")
                f.write(f"Outcome: {trade_data['outcome']}\n\n")
                f.write(f"## Analysis\n{analysis_text}")
                
        except Exception as e:
            print(f"⚠️ Failed to analyze trade {trade_data.get('market_id')}: {e}")
