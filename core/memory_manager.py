import json
from datetime import datetime, timezone
from pathlib import Path
from config.paths import RUNTIME_DATA_DIR


class MemoryManager:
    """Manages the bot's long-term memory of past trades and lessons learned."""
    
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else RUNTIME_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.data_dir / "memory.json"
        self.macro_file = self.data_dir / "macro_lessons.json"
        
        if not self.memory_file.exists():
            with open(self.memory_file, "w") as f:
                json.dump([], f)

    # ── Micro-Lessons (per-trade, short-term) ────────────────

    def add_lesson(self, market_id: str, question: str, outcome: str, lesson: str):
        """Adds a new lesson learned from a closed trade."""
        memories = self._load()
        memories.append({
            "market_id": market_id,
            "question": question,
            "outcome": outcome,
            "lesson": lesson
        })
        memories = memories[-50:]
        self._save(memories)

    def get_lessons(self, limit: int = 10) -> list[dict]:
        """Retrieves the most recent lessons to inject into the analyst prompt."""
        memories = self._load()
        return memories[-limit:]
        
    def _load(self) -> list[dict]:
        try:
            with open(self.memory_file, "r") as f:
                return json.load(f)
        except Exception:
            return []
            
    def _save(self, memories: list[dict]):
        with open(self.memory_file, "w") as f:
            json.dump(memories, f, indent=2)

    # ── Macro-Lessons (permanent, lifecycle-managed) ─────────

    def add_macro_lesson_with_metadata(
        self,
        lesson: str,
        lens: str,
        evidence_ids: list[str],
        confidence: float,
        pnl_at_creation: float,
    ):
        """Store a validated lesson with full metadata for lifecycle tracking."""
        lessons = self._load_macro_raw()
        lessons.append({
            "lesson": lesson,
            "lens": lens,
            "evidence_ids": evidence_ids,
            "confidence": confidence,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "pnl_at_creation": pnl_at_creation,
            "trades_after_count": 0,
            "trades_after_pnl": 0.0,
            "status": "probationary",  # probationary → active → retired
        })
        # Hard cap at 30 total lessons (including retired, for history)
        lessons = lessons[-30:]
        self._save_macro_raw(lessons)

    def add_macro_lessons(self, lessons: list[str]):
        """Legacy-compatible: store simple string lessons as probationary."""
        for lesson in lessons:
            self.add_macro_lesson_with_metadata(
                lesson=lesson,
                lens="legacy",
                evidence_ids=[],
                confidence=0.7,
                pnl_at_creation=0.0,
            )

    def get_macro_lessons(self) -> list[str]:
        """Get active + probationary lesson strings for prompt injection."""
        raw = self._load_macro_raw()
        return [
            l["lesson"] for l in raw
            if isinstance(l, dict) and l.get("status") in ("active", "probationary")
        ]

    def get_active_lessons(self) -> list[str]:
        """Get only fully validated active lessons (for strict prompt injection)."""
        raw = self._load_macro_raw()
        return [
            l["lesson"] for l in raw
            if isinstance(l, dict) and l.get("status") == "active"
        ]

    def get_probationary_lessons(self) -> list[str]:
        """Get only probationary lessons (for softer prompt injection)."""
        raw = self._load_macro_raw()
        return [
            l["lesson"] for l in raw
            if isinstance(l, dict) and l.get("status") == "probationary"
        ]

    def get_macro_lessons_raw(self) -> list[dict]:
        """Get all lessons with full metadata (for lifecycle management)."""
        return self._load_macro_raw()

    def save_macro_lessons_raw(self, lessons: list[dict]):
        """Save the full lessons list (used by analyzer for bulk updates)."""
        self._save_macro_raw(lessons)

    def get_resolved_trade_count(self) -> int:
        """Count resolved trades from trades.jsonl for auto-trigger logic."""
        count = 0
        from config.paths import TRADE_LOG_PATH
        log_path = TRADE_LOG_PATH
        if log_path.exists():
            try:
                with open(log_path, "r") as f:
                    for line in f.read().splitlines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("type") == "resolution":
                            count += 1
            except Exception:
                pass
        return count

    def _load_macro_raw(self) -> list[dict]:
        if self.macro_file.exists():
            try:
                with open(self.macro_file, "r") as f:
                    data = json.load(f)
                    # Handle legacy format (list of strings)
                    if data and isinstance(data[0], str):
                        return [
                            {"lesson": s, "status": "active", "lens": "legacy",
                             "confidence": 1.0, "evidence_ids": [],
                             "added_at": "", "pnl_at_creation": 0.0,
                             "trades_after_count": 0, "trades_after_pnl": 0.0}
                            for s in data
                        ]
                    return data
            except Exception:
                return []
        return []

    def _save_macro_raw(self, lessons: list[dict]):
        with open(self.macro_file, "w") as f:
            json.dump(lessons, f, indent=2)

    def inject_lessons_into_prompt(self, base_prompt: str) -> str:
        """Injects active macro-lessons into the system prompt."""
        lessons = self.get_macro_lessons()
        if not lessons:
            return base_prompt
            
        lessons_text = "\n".join([f"- {l}" for l in lessons])
        memory_section = f"""
<analyst_memory_macro_lessons>
You MUST strictly adhere to these validated lessons learned from your own trading history:
{lessons_text}
</analyst_memory_macro_lessons>"""
        return base_prompt + memory_section
