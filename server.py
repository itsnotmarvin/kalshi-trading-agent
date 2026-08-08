"""
Web Server — FastAPI backend for the trading dashboard.

Serves the React dashboard and provides REST API endpoints
for the frontend to communicate with the trading bot.

Usage:
    pip install "fastapi[standard]" uvicorn
    python server.py

Then open http://localhost:8000 in your browser.
"""
import asyncio
import json
import os
import re
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import httpx  # type: ignore
from fastapi import Body, Depends, FastAPI, Header, HTTPException  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # type: ignore

from adapters.base import TradeResult, Side, Order, OrderStatus  # type: ignore
from config.settings import settings  # type: ignore
from core.notifier import send_telegram_alert  # type: ignore
from core.agent import TradingAgent  # type: ignore
from core.risk_manager import RiskManager, TradeProposal  # type: ignore
from core.logger import TradeLogger  # type: ignore
from core.trade_utils import direction_to_side_and_price, is_executable_direction  # type: ignore
from core.postmortem_engine import PostmortemEngine  # type: ignore
from core.memory_manager import MemoryManager  # type: ignore
from core.world_cup_service import WorldCupService, redacted_environment_status  # type: ignore


# ── Global State ─────────────────────────────────────────────
class BotState:
    """Shared state between the background trading loop and the API."""
    def __init__(self):
        self.is_running = False
        self.is_paused = False
        self.mode = "paper"
        self.platform = settings.platform
        self.cycle_count = 0
        self.last_cycle_time: str | None = None
        self.next_cycle_time: str | None = None
        self.current_activity = "Idle"
        self.recent_logs: list[dict[str, str]] = []  # Last 100 events
        self.markets_scanned = 0
        self.trades_proposed = 0
        self.trades_approved = 0
        self.balance = 0.0
        self.portfolio_value = 0.0
        self.live_balance = 0.0
        self.live_portfolio_value = 0.0
        self.initial_balance = 0.0  # Captured at first connect
        self.total_deposited = 0.0  # Real sum from Kalshi transfers API
        self.total_withdrawn = 0.0  # Real sum from Kalshi transfers API
        self.total_profit = 0.0
        self.total_loss = 0.0
        self.positions: list[dict[str, Any]] = []
        self.proposals: list[dict[str, Any]] = []  # Recent trade proposals
        self.pending_entries: dict[str, dict[str, Any]] = {}
        self.processed_exits: set[str] = set()  # Tickers already scalped this session
        self.market_titles: dict[str, str] = {}  # Cache for market IDs to titles
        self.researched_markets: set[str] = set()
        self.last_research_clear = datetime.now(timezone.utc)
        self.research_active = False
        self.research_completed = 0
        self.research_total = 0
        self.research_steps_completed = 0
        self.research_steps_total = 0
        self.status_message = "Offline"
        self.current_research_targets: list[dict[str, Any]] = []

        self.scan_categories: list[str] = ["Weather", "Sports", "Politics", "Crypto", "Entertainment", "Science"]
        self.resolved_since_last_reflection = 0  # Auto-trigger reflection after N resolutions
        self.last_reflection_trade_count = 0
        self.active_directive: str | None = None  # Strategy provided by Market Intel Recon
        self.macro_lessons: list[dict[str, Any]] = []

    def add_log(self, level: str, message: str):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        self.recent_logs.insert(0, entry)
        self.recent_logs = self.recent_logs[:100]  # type: ignore

    def to_dict(self) -> dict:
        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "mode": self.mode,
            "platform": self.platform,
            "cycle_count": self.cycle_count,
            "last_cycle_time": self.last_cycle_time,
            "next_cycle_time": self.next_cycle_time,
            "current_activity": self.current_activity,
            "markets_scanned": self.markets_scanned,
            "trades_proposed": self.trades_proposed,
            "trades_approved": self.trades_approved,
            "balance": self.balance,
            "portfolio_value": self.portfolio_value,
            "live_balance": self.live_balance,
            "live_portfolio_value": self.live_portfolio_value,
            "initial_balance": self.initial_balance,
            "total_deposited": self.total_deposited,
            "total_withdrawn": self.total_withdrawn,
            "total_profit": self.total_profit,
            "total_loss": self.total_loss,
            "research_active": self.research_active,
            "research_completed": self.research_completed,
            "research_total": self.research_total,
            "research_steps_completed": self.research_steps_completed,
            "research_steps_total": self.research_steps_total,
            "status_message": self.status_message,
            "current_research_targets": self.current_research_targets,

            "active_directive": self.active_directive,
            "macro_lessons": self.macro_lessons,
        }


state = BotState()
risk_manager = RiskManager()
logger = TradeLogger()
memory_manager = MemoryManager()
postmortem_engine = PostmortemEngine(memory_manager=memory_manager, anthropic_api_key=settings.anthropic_api_key)
bot_task: asyncio.Task | None = None
API_TOKEN = settings.dashboard_api_token or secrets.token_urlsafe(32)
COUNTED_TRANSFER_STATUSES = {"applied", "approved", "completed", "settled", "success"}
PREOPEN_MARKET_STATUSES = {"initialized", "unopened", "preopen", "pending", "not_started", "unstarted"}
EARLY_MARK_IMAGE_CACHE: dict[str, str | None] = {}
EARLY_MARK_IMAGE_USER_AGENT = "KalshiEarlyMarks/0.1 (local research dashboard; contact: research@example.com)"
EARLY_MARK_PROBE_PATH = Path(__file__).parent / "data" / "early_marks_probe.json"
EARLY_MARK_RUNS_PATH = Path(__file__).parent / "data" / "early_marks_runs.json"
EARLY_MARK_STATUS_PATH = Path(__file__).parent / "data" / "early_marks_status.json"
EARLY_MARK_RUNNING: dict[str, Any] = {"running": False, "run_id": None}
EARLY_MARK_STATUS_DISPLAY = {
    "Watch only": "Watch only",
    "Catalyst setup": "Catalyst setup",
    "Pre-open watch": "Too early",
    "Too directionless": "Reject",
    "Market knows something": "Reject",
}
EARLY_MARK_TICKER_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9-]*[0-9]{2}[A-Z0-9-]*\b")
EARLY_MARK_STAGE_LABELS = {
    "connect": "Connecting to Kalshi",
    "health": "Checking service health",
    "pull_markets": "Pulling markets",
    "category_model": "Checking category model",
    "rank": "Ranking early marks",
    "enrich": "Enriching with research",
    "render": "Rendering cards",
}


def require_api_token(x_agent_token: str | None = Header(default=None)):
    if x_agent_token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid dashboard token")


_global_adapter = None

def get_adapter():
    global _global_adapter
    if _global_adapter is not None:
        return _global_adapter

    if settings.platform == "kalshi":
        from adapters.kalshi_adapter import KalshiAdapter  # type: ignore
        _global_adapter = KalshiAdapter()
    elif settings.platform == "manifold":
        from adapters.manifold_adapter import ManifoldAdapter  # type: ignore
        _global_adapter = ManifoldAdapter()
    elif settings.platform == "polymarket":
        from adapters.polymarket_adapter import PolymarketAdapter  # type: ignore
        _global_adapter = PolymarketAdapter()
    else:
        raise ValueError(f"Unknown platform: {settings.platform}")
        
    return _global_adapter

async def refresh_state_from_platform(adapter=None):
    """Refresh balance, portfolio value, and transfer totals from the platform."""
    try:
        if not adapter:
            adapter = get_adapter()
            await adapter.connect()

        # Always fetch actual platform values into live_balance / live_portfolio_value
        try:
            state.live_balance = await adapter.get_balance()
            state.live_portfolio_value = await adapter.get_portfolio_value()
        except Exception as e:
            print(f"⚠️  Failed to fetch live balance: {e}")

        # Fetch real deposit/withdrawal totals from Kalshi transfers API
        try:
            transfers = await adapter.get_transfers()
            total_dep = 0.0
            total_wth = 0.0
            for t in transfers:
                if t.status.lower() not in COUNTED_TRANSFER_STATUSES:
                    continue
                if t.type.lower() == "deposit":
                    total_dep += t.amount
                elif t.type.lower() == "withdrawal":
                    total_wth += t.amount
            state.total_deposited = total_dep
            state.total_withdrawn = total_wth
        except Exception as e:
            print(f"⚠️  Failed to fetch transfers: {e}")

        is_paper_mode = state.mode == "paper" or state.mode.endswith("_paper")
        if is_paper_mode:
            # Don't overwrite paper bankroll from the platform — initialize once.
            if state.initial_balance == 0.0:
                state.balance = settings.paper_balance
                state.initial_balance = settings.paper_balance
            if state.portfolio_value == 0.0:
                state.portfolio_value = state.balance
            return

        state.balance = state.live_balance
        state.portfolio_value = state.live_portfolio_value

        if state.initial_balance == 0.0 and state.balance > 0:
            state.initial_balance = state.balance

    except Exception as e:
        print(f"⚠️  Failed to refresh state: {e}")

async def _maybe_auto_reflect():
    """Auto-trigger a deep reflection after every 10 resolved trades."""
    if state.resolved_since_last_reflection >= 10:
        state.resolved_since_last_reflection = 0
        state.add_log("info", "🧠 Auto-triggering Deep Reflection after 10 resolved trades...")
        try:
            from core.analyzer import TradeAnalyzer
            analyzer = TradeAnalyzer()
            result = await analyzer.run_deep_reflection()
            analyzer.check_and_retire_lessons()
            accepted = result.get("lessons_accepted", 0)
            lenses = result.get("lenses_used", [])
            state.add_log("success", f"Auto-Reflection complete via {', '.join(lenses)}. {accepted} new lessons accepted.")
        except Exception as e:
            state.add_log("error", f"Auto-Reflection failed: {e}")


# ── Background Trading Loop (extracted to core/trading_loop.py) ──
from core import trading_loop as _tl

def _init_trading_loop():
    """Wire shared state into the extracted trading loop module."""
    _tl.init(
        _state=state,
        _risk_manager=risk_manager,
        _logger=logger,
        _memory_manager=memory_manager,
        _get_adapter=get_adapter,
        _auto_reflect=_maybe_auto_reflect,
    )

# Re-export for backward compat (bot_task references this)
async def trading_loop():
    _init_trading_loop()
    await _tl.trading_loop()


# ── FastAPI App ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fetch live balance/portfolio on startup
    asyncio.create_task(refresh_state_from_platform())
    yield
    # Cleanup on shutdown
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()


app = FastAPI(title="Prediction Market Agent", lifespan=lifespan)
world_cup_service = WorldCupService(get_adapter)


# ── API Endpoints ────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _early_mark_status(decision: str, fallback: str) -> str:
    if decision in {"Catalyst setup", "Watch only", "Market knows something", "Too directionless", "Pre-open watch"}:
        return decision
    if fallback in {"Catalyst setup", "Watch only", "Too directionless", "Market knows something", "Pre-open watch"}:
        return fallback
    return "Watch only"


def _early_mark_status_display(status: Any) -> str:
    return EARLY_MARK_STATUS_DISPLAY.get(str(status or ""), str(status or "Watch only"))


def _visible_early_mark_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if EARLY_MARK_TICKER_RE.search(text):
        return fallback
    if re.fullmatch(r"[A-Z0-9]{2,12}", text):
        return fallback
    return text


def _strip_early_mark_tickers(text: str, replacement: str) -> str:
    cleaned = EARLY_MARK_TICKER_RE.sub(replacement, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .;-")
    return cleaned or replacement


def _early_mark_market_group(candidate: dict[str, Any], placement: str) -> str:
    display_title = str(candidate.get("display_title") or "").strip()
    title = str(candidate.get("title") or "").strip()
    suffix = f" - {placement}"
    if display_title and placement and display_title.endswith(suffix):
        return _visible_early_mark_label(display_title[: -len(suffix)], "Market")
    return _visible_early_mark_label(display_title or title, "Market")


def _early_mark_text(
    value: Any,
    ticker: str,
    placement: str,
    fallback: str,
    replacements: dict[str, str] | None = None,
) -> str:
    text = str(value or "").strip() or fallback
    local_replacements = [ticker]
    if "-" in ticker:
        local_replacements.append(ticker.rsplit("-", 1)[-1])
    for token, label in (replacements or {}).items():
        if token and label:
            text = re.sub(rf"\b{re.escape(token)}\b", label, text)
    for token in local_replacements:
        if not token:
            continue
        text = re.sub(rf"\b{re.escape(token)}\b", placement, text)
    return _strip_early_mark_tickers(text, placement)


def _early_mark_cents(value: Any) -> str:
    number = _safe_float(value, -1.0)
    if number < 0:
        return "an unavailable price"
    return f"{round(number * 100)}c"


def _early_mark_image_url(placement: str, category: str) -> str:
    query = urllib.parse.urlencode({"name": placement, "category": category})
    return f"/api/early-marks/image?{query}"


def _early_mark_initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not words:
        return "EM"
    if len(words) == 1:
        return words[0][:2].upper()
    return f"{words[0][0]}{words[-1][0]}".upper()


def _early_mark_fallback_svg(name: str, category: str) -> str:
    initials = _early_mark_initials(name)
    seed = sum(ord(char) for char in f"{name}:{category}")
    palettes = [
        ("#e8f1ff", "#2563eb"),
        ("#ecfdf3", "#16803b"),
        ("#fff7ed", "#c2410c"),
        ("#f5f3ff", "#6d28d9"),
        ("#f8fafc", "#334155"),
    ]
    bg, fg = palettes[seed % len(palettes)]
    safe_initials = initials.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        f'<rect width="128" height="128" rx="24" fill="{bg}"/>'
        f'<text x="64" y="74" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" '
        f'font-size="42" font-weight="800" fill="{fg}">{safe_initials}</text>'
        '</svg>'
    )


async def _lookup_wikipedia_thumbnail(name: str, category: str) -> str | None:
    key = f"{name.strip().lower()}::{category.strip().lower()}"
    if key in EARLY_MARK_IMAGE_CACHE:
        return EARLY_MARK_IMAGE_CACHE[key]
    if not name.strip() or len(name) > 80:
        EARLY_MARK_IMAGE_CACHE[key] = None
        return None
    slug = re.sub(r"\s+", "_", name.strip())
    try:
        async with httpx.AsyncClient(timeout=4.0, headers={"User-Agent": EARLY_MARK_IMAGE_USER_AGENT}) as client:
            summary = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(slug)}")
            if summary.status_code == 200:
                thumbnail = (summary.json().get("thumbnail") or {}).get("source")
                if thumbnail:
                    EARLY_MARK_IMAGE_CACHE[key] = str(thumbnail)
                    return str(thumbnail)
    except Exception:
        pass
    search_terms = name.strip()
    if "politics" in category.lower() or "election" in category.lower():
        search_terms = f"{search_terms} politician"
    elif "sports" in category.lower():
        search_terms = f"{search_terms} sports"
    try:
        async with httpx.AsyncClient(timeout=4.0, headers={"User-Agent": EARLY_MARK_IMAGE_USER_AGENT}) as client:
            response = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": search_terms,
                    "gsrlimit": 1,
                    "prop": "pageimages",
                    "pithumbsize": 160,
                    "redirects": 1,
                },
            )
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                thumbnail = page.get("thumbnail") if isinstance(page, dict) else None
                source = thumbnail.get("source") if isinstance(thumbnail, dict) else None
                if source:
                    EARLY_MARK_IMAGE_CACHE[key] = str(source)
                    return str(source)
    except Exception:
        pass
    EARLY_MARK_IMAGE_CACHE[key] = None
    return None


def _early_mark_domain_summary(domain_signals: dict[str, Any]) -> str:
    signals = domain_signals.get("signals") if isinstance(domain_signals.get("signals"), list) else []
    matched = [
        str(signal.get("name") or "").strip()
        for signal in signals
        if isinstance(signal, dict) and signal.get("matched_terms")
    ]
    if matched:
        return ", ".join(matched[:3])
    return "domain facts still need source enrichment"


def _early_mark_flow_summary(flow_signal: dict[str, Any]) -> str:
    inputs = flow_signal.get("inputs") if isinstance(flow_signal.get("inputs"), dict) else {}
    volume = _safe_float(inputs.get("volume"), 0.0)
    volume_24h = _safe_float(inputs.get("volume_24h"), 0.0)
    liquidity = _safe_float(inputs.get("liquidity"), 0.0)
    recent_move = _safe_float(inputs.get("recent_move_pct"), 0.0)
    parts = []
    if volume:
        parts.append(f"volume {volume:,.0f}")
    if volume_24h:
        parts.append(f"24h volume {volume_24h:,.0f}")
    if liquidity:
        parts.append(f"liquidity ${liquidity:,.0f}")
    if recent_move:
        parts.append(f"recent move {recent_move:.1f}%")
    return ", ".join(parts) or "thin public flow so far"


def _early_mark_decision_drivers(
    candidate: dict[str, Any],
    next_catalyst: dict[str, Any],
    domain_signals: dict[str, Any],
    flow_signal: dict[str, Any],
) -> list[dict[str, str]]:
    days = candidate.get("days_to_resolution")
    days_text = f"{round(_safe_float(days))} days to resolution" if days is not None else "runway unknown"
    drivers = [
        {
            "label": "Catalyst path",
            "note": str(next_catalyst.get("label") or "Next public pricing event"),
        },
        {
            "label": "Why catalyst matters",
            "note": str(next_catalyst.get("why_it_matters") or "A public fact could change resale demand before resolution."),
        },
        {
            "label": "Domain signal",
            "note": _early_mark_domain_summary(domain_signals),
        },
        {
            "label": "Flow / exit",
            "note": f"{_early_mark_flow_summary(flow_signal)}; {days_text}",
        },
    ]
    public_profile = candidate.get("public_figure_profile") if isinstance(candidate.get("public_figure_profile"), dict) else {}
    checks = public_profile.get("what_to_check") if isinstance(public_profile.get("what_to_check"), list) else []
    if checks:
        drivers.insert(0, {
            "label": str(public_profile.get("name") or "Entity context"),
            "note": " ".join(str(check) for check in checks[:2]),
        })
    return drivers


def _early_mark_fallback_thesis(candidate: dict[str, Any], placement: str, next_catalyst: dict[str, Any]) -> str:
    entry = _early_mark_cents(candidate.get("yes_ask"))
    target = _early_mark_cents(candidate.get("expected_future_price") or candidate.get("entry_limit"))
    roi = candidate.get("expected_repricing_roi")
    rank = candidate.get("rank_score")
    flow = candidate.get("flow_signal") if isinstance(candidate.get("flow_signal"), dict) else {}
    domain = candidate.get("domain_signals") if isinstance(candidate.get("domain_signals"), dict) else {}
    catalyst = next_catalyst.get("label") or "the next public pricing catalyst"
    catalyst_reason = next_catalyst.get("why_it_matters") or "that kind of public fact can change resale demand before resolution"
    domain_text = _early_mark_domain_summary(domain)
    flow_text = _early_mark_flow_summary(flow)
    math_text = f"The math is secondary support: score {rank}, raw resale ROI around {roi}%." if roi is not None else f"The math is secondary support: score {rank}."
    return (
        f"I believe {placement} can reprice from {entry} toward {target} if {catalyst} starts making the story less theoretical, "
        f"because {catalyst_reason}. The domain signals I would watch are {domain_text}, and the current public flow picture is {flow_text}. "
        f"{math_text} The risk is that the market may already be pricing the obvious story, so a weak spread, fading volume, or no source-backed catalyst should keep it on watch."
    )


def _early_mark_brief_text(text: Any, fallback: str, max_chars: int = 150) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if not cleaned:
        return fallback
    sentence_match = re.search(r"(.+?[.!?])(?:\s|$)", cleaned)
    if sentence_match:
        cleaned = sentence_match.group(1).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return f"{cut}..."


def _build_early_mark_quick_take(
    *,
    placement: str,
    current_price: Any,
    target_price: Any,
    status_display: str,
    repricing_thesis: str,
    caution: str,
    market_knows_check: str,
    next_catalyst: dict[str, Any],
    soccer_path: dict[str, Any] | None,
) -> dict[str, str]:
    entry = _early_mark_cents(current_price)
    target = _early_mark_cents(target_price)
    catalyst_label = str(next_catalyst.get("label") or "next public catalyst")
    catalyst_why = str(next_catalyst.get("why_it_matters") or "")
    if soccer_path:
        path = (
            f"Path math: market {soccer_path.get('market_share_pct', '?')}% vs "
            f"path {soccer_path.get('path_implied_pct', '?')}%."
        )
    else:
        path = _early_mark_brief_text(catalyst_why, "Catalyst details still need source enrichment.", 130)
    return {
        "takeaway": (
            f"{status_display}: watch whether {placement} can move from {entry} toward {target}."
            if target_price is not None
            else f"{status_display}: watch {placement}; no clean target price yet."
        ),
        "catalyst": _early_mark_brief_text(catalyst_label, "Next catalyst unavailable.", 130),
        "why": path,
        "risk": _early_mark_brief_text(caution, "Stay research-only if spread, depth, or catalyst quality is weak.", 150),
        "check": _early_mark_brief_text(market_knows_check, "Check public explanation before acting.", 150),
        "full_thesis": _early_mark_brief_text(repricing_thesis, "Full thesis available in Research.", 170),
    }


def _build_early_marks_payload() -> dict[str, Any]:
    """Serve the latest read-only Early Marks probe in dashboard shape."""
    probe_path = EARLY_MARK_PROBE_PATH
    if not probe_path.exists():
        return {
            "mode_label": "Research only",
            "summary": {"marks_reviewed": 0, "entry_candidates": 0, "watch_only": 0, "caution_flags": 0},
            "category_radar": [],
            "available_categories": [],
            "available_statuses": ["all", "open", "preopen"],
            "available_horizons": ["all", "short", "medium", "long"],
            "marks": [],
            "message": "Run scripts/manual_probes/test_early_marks.py to generate a read-only market scan.",
        }

    try:
        raw_payload = json.loads(probe_path.read_text())
    except Exception as exc:
        return {
            "mode_label": "Research unavailable",
            "summary": {"marks_reviewed": 0, "entry_candidates": 0, "watch_only": 0, "caution_flags": 1},
            "category_radar": [],
            "available_categories": [],
            "available_statuses": ["all", "open", "preopen"],
            "available_horizons": ["all", "short", "medium", "long"],
            "marks": [],
            "message": f"Could not parse Early Marks probe: {exc}",
        }

    candidates = raw_payload.get("top_candidates", [])
    raw_claude_results = raw_payload.get("claude_results", [])
    if not isinstance(raw_claude_results, list):
        raw_claude_results = []
    claude_status = str(
        raw_payload.get("claude_status")
        or ("succeeded" if raw_claude_results else "legacy_unknown")
    )
    claude_error = str(raw_payload.get("claude_parse_error") or "") or None
    claude_by_ticker = {
        item.get("ticker"): item
        for item in raw_claude_results
        if isinstance(item, dict) and item.get("ticker")
    }

    marks: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    text_replacements: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_ticker = str(candidate.get("ticker") or "")
        candidate_placement = _visible_early_mark_label(candidate.get("display_name"), "")
        if not candidate_ticker or not candidate_placement:
            continue
        text_replacements[candidate_ticker] = candidate_placement
        if "-" in candidate_ticker:
            text_replacements[candidate_ticker.rsplit("-", 1)[-1]] = candidate_placement

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        ticker = str(candidate.get("ticker") or f"mark-{index}")
        research = claude_by_ticker.get(ticker, {})
        raw_category = str(candidate.get("category") or "unknown")
        category_path = candidate.get("category_path") if isinstance(candidate.get("category_path"), list) else [raw_category]
        category = " / ".join(str(part) for part in category_path if part) or raw_category
        category_counts[category] = category_counts.get(category, 0) + 1
        placement = _visible_early_mark_label(candidate.get("display_name"), "Unnamed placement")
        market_group = _early_mark_market_group(candidate, placement)
        current_price = candidate.get("yes_ask")
        expected_future_price = candidate.get("expected_future_price")
        entry_limit = candidate.get("entry_limit")
        next_catalyst = candidate.get("next_catalyst") if isinstance(candidate.get("next_catalyst"), dict) else {}
        domain_signals = candidate.get("domain_signals") if isinstance(candidate.get("domain_signals"), dict) else {}
        flow_signal = candidate.get("flow_signal") if isinstance(candidate.get("flow_signal"), dict) else {}
        status_internal = _early_mark_status(str(research.get("decision") or ""), str(candidate.get("probe_status") or ""))
        status_display = _early_mark_status_display(status_internal)
        rank_score = _safe_float(candidate.get("rank_score"))
        implied = _safe_float(current_price) * 100 if current_price is not None else None
        reference = _safe_float(expected_future_price) * 100 if expected_future_price is not None else None
        belief = max(5.0, min(95.0, _safe_float(candidate.get("bull_probability"))))
        placement_dollars = _safe_float(candidate.get("recommended_placement_dollars"), 0.0) or None
        estimated_contracts = candidate.get("estimated_contracts")
        if estimated_contracts is not None:
            try:
                estimated_contracts = int(estimated_contracts)
            except (TypeError, ValueError):
                estimated_contracts = None
        estimated_position_cost = _safe_float(candidate.get("estimated_position_cost"), 0.0) or None
        estimated_target_value = _safe_float(candidate.get("estimated_target_value"), 0.0) or None
        estimated_target_profit = _safe_float(candidate.get("estimated_target_profit"), 0.0) if candidate.get("estimated_target_profit") is not None else None
        trade_status = "Pre-open watch" if str(candidate.get("status") or "").lower() in PREOPEN_MARKET_STATUSES else "Research only"
        side = str(research.get("side") or ("Watch" if status_display != "Catalyst setup" else "YES"))
        target_price = expected_future_price if expected_future_price is not None else entry_limit
        fallback_thesis = _early_mark_fallback_thesis(candidate, placement, next_catalyst)
        decision_drivers = _early_mark_decision_drivers(candidate, next_catalyst, domain_signals, flow_signal)
        repricing_thesis = _early_mark_text(
            research.get("why_i_believe_this"), ticker, placement, fallback_thesis, text_replacements
        )
        caution = _early_mark_text(
            research.get("no_direction_check"),
            ticker,
            placement,
            "If the market opens with a bad spread, no depth, or no clear catalyst, keep it on watch only.",
            text_replacements,
        )
        market_knows_check = _early_mark_text(
            research.get("market_knows_check"),
            ticker,
            placement,
            "If price moves sharply before a source-backed explanation appears, treat the move as information and downgrade the thesis.",
            text_replacements,
        )
        soccer_path = candidate.get("soccer_path") if isinstance(candidate.get("soccer_path"), dict) else None
        quick_take = _build_early_mark_quick_take(
            placement=placement,
            current_price=current_price,
            target_price=target_price,
            status_display=status_display,
            repricing_thesis=repricing_thesis,
            caution=caution,
            market_knows_check=market_knows_check,
            next_catalyst=next_catalyst,
            soccer_path=soccer_path,
        )

        sources = research.get("sources") if isinstance(research.get("sources"), list) else []
        if not sources:
            sources = [{
                "label": "Kalshi market metadata",
                "url_or_reference": candidate.get("settlement_source") or "Kalshi",
                "why_it_matters": "This dry run used market metadata and settlement-source fields only.",
            }]
        has_external_sources = any(
            isinstance(source, dict)
            and str(source.get("url_or_reference") or "").startswith("http")
            for source in sources
        )

        marks.append({
            "id": ticker,
            "ticker": ticker,
            "market": market_group,
            "market_group": market_group,
            "display_title": f"{market_group} - {placement}",
            "contract": f"{placement} · {side} contract",
            "title": market_group,
            "placement": placement,
            "image_url": _early_mark_image_url(placement, category),
            "category": category,
            "raw_category": raw_category,
            "category_path": category_path,
            "model_key": candidate.get("model_key"),
            "model_name": candidate.get("model_name") or category,
            "status": status_display,
            "status_internal": status_internal,
            "status_display": status_display,
            "side": side,
            "side_or_placement": side if side != "Watch" else placement,
            "placement_dollars": round(placement_dollars, 2) if placement_dollars is not None else None,
            "estimated_contracts": estimated_contracts,
            "cost_basis": round(estimated_position_cost, 2) if estimated_position_cost is not None else None,
            "current_price": current_price,
            "yes_ask": candidate.get("yes_ask"),
            "yes_bid": candidate.get("yes_bid"),
            "last_price": candidate.get("last_price"),
            "spread": candidate.get("spread"),
            "liquidity_note": _early_mark_flow_summary(flow_signal),
            "target_price": target_price,
            "target_value": round(estimated_target_value, 2) if estimated_target_value is not None else None,
            "target_profit": round(estimated_target_profit, 2) if estimated_target_profit is not None else None,
            "resolution_payout": float(estimated_contracts) if estimated_contracts is not None else None,
            "trade_status": trade_status,
            "rank_score": rank_score,
            "score": rank_score,
            "expected_roi": candidate.get("expected_repricing_roi"),
            "days_to_resolution": candidate.get("days_to_resolution"),
            "days_to_catalyst": next_catalyst.get("days_proxy"),
            "next_catalyst": next_catalyst,
            "catalyst": research.get("next_catalyst") or next_catalyst.get("label") or "Next major pricing event or market open; exact catalyst needs source enrichment in the next scan.",
            "catalyst_date_estimate": _early_mark_text(
                research.get("catalyst_date_estimate"), ticker, placement, "", text_replacements
            ) or None,
            "fair_probability_range": research.get("fair_probability_range")
            if isinstance(research.get("fair_probability_range"), dict)
            else None,
            "reason": repricing_thesis,
            "repricing_thesis": repricing_thesis,
            "quick_take": quick_take,
            "decision_drivers": decision_drivers,
            "public_figure_profile": candidate.get("public_figure_profile"),
            "middle_ground": "This is not a claim that the market missed secret information. The thesis is about whether attention, path clarity, or opening structure can move price before resolution.",
            "caution": caution,
            "market_knows_check": market_knows_check,
            "domain_signals": domain_signals,
            "flow_signal": flow_signal,
            "soccer_path": soccer_path,
            "scenario_weight_basis": candidate.get("scenario_weight_basis") or research.get("scenario_weight_basis"),
            "scenario_weights": {
                "bull": candidate.get("bull_probability"),
                "base": candidate.get("base_probability"),
                "bear": candidate.get("bear_probability"),
            },
            "scenario_prices": {
                "bull": candidate.get("bull_price"),
                "base": candidate.get("base_price"),
                "bear": candidate.get("bear_price"),
                "expected_future": candidate.get("expected_future_price"),
                "entry_limit": candidate.get("entry_limit"),
                "expected_repricing_roi": candidate.get("expected_repricing_roi"),
            },
            "price_scenario_basis": candidate.get("price_scenario_basis"),
            "placement_sizing_basis": candidate.get("placement_sizing_basis"),
            "implied_probability": implied,
            "reference_probability": reference,
            "reprice_confidence": belief,
            "price_series": [
                max(0.01, (_safe_float(current_price, 0.05) * 0.92)),
                _safe_float(current_price, 0.05),
                _safe_float(target_price, _safe_float(current_price, 0.05) * 1.08),
            ],
            "price_action": {
                "label": "Dry-run price view",
                "note": "This first page test uses latest quote fields from the probe, not a full candlestick history yet.",
            },
            "factor_scores": [
                {"name": "Catalyst clarity", "score": candidate.get("catalyst_score"), "note": "Does the market have a recognizable future event that can change perception?"},
                {"name": "Runway", "score": candidate.get("runway_score"), "note": "More time before resolution gives repricing more room to happen."},
                {"name": "Liquidity/exit", "score": candidate.get("liquidity_score"), "note": "Higher volume and liquidity improve the chance of selling after a move."},
                {"name": "Attention growth", "score": candidate.get("attention_score"), "note": "Categories likely to attract later attention rank higher."},
                {"name": "Domain model fit", "score": candidate.get("domain_signal_score"), "note": "Does this market match the category-specific model instead of only generic Early Marks inputs?"},
                {"name": "Flow evidence", "score": candidate.get("flow_signal_score"), "note": "Public quote/volume/liquidity signal. It does not identify holders or insiders."},
                {"name": "Directionless penalty", "score": candidate.get("directionless_penalty"), "note": "High penalty means the setup may be early but not actionable."},
            ],
            "has_external_sources": has_external_sources,
            "sources": [
                {
                    "label": _early_mark_text(source.get("label"), ticker, placement, "Source", text_replacements),
                    "date": str(source.get("published_date") or "") or None,
                    "note": _early_mark_text(
                        source.get("why_it_matters"), ticker, placement, "Probe source", text_replacements
                    ),
                    "url": str(source.get("url_or_reference"))
                    if str(source.get("url_or_reference") or "").startswith("http")
                    else None,
                    "status": "External"
                    if str(source.get("url_or_reference") or "").startswith("http")
                    else "Internal",
                    "status_class": "research"
                    if str(source.get("url_or_reference") or "").startswith("http")
                    else "watch",
                }
                for source in sources
                if isinstance(source, dict)
            ],
            "exit_monitor": research.get("exit_monitor") if isinstance(research.get("exit_monitor"), list) else [
                {"trigger": "Price reaches target mark", "action": "Review sell or hedge plan.", "level": "Target"},
                {"trigger": "Unexplained sharp move", "action": "Pause entry and investigate why the market moved.", "level": "Caution"},
            ],
        })

    entry_candidates = sum(1 for mark in marks if mark["status_display"] == "Catalyst setup")
    caution_flags = sum(1 for mark in marks if mark["status_internal"] in {"Market knows something", "Too directionless"})
    ticker_prefixes = [str(mark.get("ticker") or "").split("-", 1)[0] for mark in marks if mark.get("ticker")]
    prefix_counts = {prefix: ticker_prefixes.count(prefix) for prefix in set(ticker_prefixes)}
    largest_prefix_count = max(prefix_counts.values(), default=0)
    concentrated = bool(ticker_prefixes) and largest_prefix_count / len(ticker_prefixes) >= 0.60
    payload_warnings = ["Concentrated: mostly one market family"] if concentrated else []
    if claude_status == "failed":
        payload_warnings.append("Claude enrichment failed; heuristic candidates are not enriched research.")
    return {
        "mode_label": "Research degraded" if claude_status == "failed" else "Research only",
        "run_id": raw_payload.get("run_id"),
        "generated_at": raw_payload.get("generated_at"),
        "enrichment": {
            "status": claude_status,
            "model": raw_payload.get("claude_model"),
            "result_count": len(raw_claude_results),
            "error": claude_error,
        },
        "summary": {
            "marks_reviewed": raw_payload.get("candidate_count", len(marks)),
            "entry_candidates": entry_candidates,
            "watch_only": sum(1 for mark in marks if mark["status"] == "Watch only"),
            "caution_flags": caution_flags,
        },
        "category_radar": [
            {"category": category, "count": count, "signal": "Shortlisted by the current read-only probe."}
            for category, count in sorted(category_counts.items(), key=lambda item: item[1], reverse=True)
        ],
        "available_categories": raw_payload.get("available_categories") or sorted(category_counts),
        "available_statuses": raw_payload.get("available_statuses") or ["active", "initialized", "open"],
        "available_horizons": raw_payload.get("available_horizons") or ["short", "medium", "long"],
        "selection_policy": raw_payload.get("selection_policy") or {},
        "warnings": payload_warnings,
        "concentration": {
            "concentrated": concentrated,
            "largest_share": round(largest_prefix_count / len(ticker_prefixes), 3) if ticker_prefixes else 0,
        },
        "marks": marks,
    }


def _early_mark_history_candidate(mark: dict[str, Any]) -> dict[str, Any]:
    next_catalyst = mark.get("next_catalyst") if isinstance(mark.get("next_catalyst"), dict) else {}
    scenario_prices = mark.get("scenario_prices") if isinstance(mark.get("scenario_prices"), dict) else {}
    return {
        "ticker": mark.get("ticker") or mark.get("id"),
        "side_or_placement": mark.get("side_or_placement") or mark.get("placement") or mark.get("side"),
        "title": _strip_early_mark_tickers(str(mark.get("title") or mark.get("market_group") or mark.get("display_title") or ""), "Market"),
        "image_url": mark.get("image_url"),
        "category": mark.get("category"),
        "status_internal": mark.get("status_internal") or mark.get("status"),
        "status_display": _early_mark_status_display(mark.get("status_internal") or mark.get("status_display") or mark.get("status")),
        "yes_ask": mark.get("yes_ask"),
        "yes_bid": mark.get("yes_bid"),
        "last_price": mark.get("last_price"),
        "score": mark.get("score") if mark.get("score") is not None else mark.get("rank_score"),
        "expected_roi": mark.get("expected_roi") if mark.get("expected_roi") is not None else scenario_prices.get("expected_repricing_roi"),
        "next_catalyst": _strip_early_mark_tickers(str(next_catalyst.get("label") or mark.get("catalyst") or ""), "Next catalyst unavailable"),
        "days_to_resolution": mark.get("days_to_resolution"),
        "spread": mark.get("spread"),
        "liquidity_note": _strip_early_mark_tickers(str(mark.get("liquidity_note") or ""), "Liquidity unavailable"),
        "reason": _strip_early_mark_tickers(str(mark.get("reason") or mark.get("repricing_thesis") or ""), "Reason unavailable"),
    }


def _load_early_mark_runs() -> list[dict[str, Any]]:
    if not EARLY_MARK_RUNS_PATH.exists():
        return []
    try:
        raw = json.loads(EARLY_MARK_RUNS_PATH.read_text())
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _write_early_mark_runs(runs: list[dict[str, Any]]) -> None:
    EARLY_MARK_RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = EARLY_MARK_RUNS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(runs[-20:], indent=2, sort_keys=True) + "\n")
    temp_path.replace(EARLY_MARK_RUNS_PATH)


def _append_early_mark_run(payload: dict[str, Any], filters_used: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    generated_at = str(payload.get("generated_at") or datetime.now(timezone.utc).isoformat())
    history_run_id = run_id or f"early-marks-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    run = {
        "run_id": history_run_id,
        "generated_at": generated_at,
        "filters_used": filters_used,
        "candidates": [
            _early_mark_history_candidate(mark)
            for mark in payload.get("marks", [])
            if isinstance(mark, dict)
        ],
    }
    runs = _load_early_mark_runs()
    runs.append(run)
    _write_early_mark_runs(runs)
    return run


def _annotate_early_mark_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    annotated: list[dict[str, Any]] = []
    for run in runs:
        current = dict(run)
        counts = {"new": 0, "updated": 0, "previously_seen": 0}
        candidates = []
        for candidate in run.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            identity = f"{item.get('ticker') or ''}::{item.get('side_or_placement') or ''}"
            previous = seen.get(identity)
            if previous is None:
                annotation = "new"
                prior_values = None
            else:
                current_price = _safe_float(item.get("yes_ask"), -1.0)
                prior_price = _safe_float(previous.get("yes_ask"), -1.0)
                price_moved = current_price >= 0 and prior_price >= 0 and abs(current_price - prior_price) >= 0.02
                status_changed = item.get("status_display") != previous.get("status_display")
                annotation = "updated" if price_moved or status_changed else "previously_seen"
                prior_values = {
                    "run_id": previous.get("_run_id"),
                    "generated_at": previous.get("_generated_at"),
                    "status_display": previous.get("status_display"),
                    "yes_ask": previous.get("yes_ask"),
                    "yes_bid": previous.get("yes_bid"),
                    "last_price": previous.get("last_price"),
                    "price_delta": round(current_price - prior_price, 4) if current_price >= 0 and prior_price >= 0 else None,
                }
            item["dedupe"] = annotation
            if prior_values:
                item["prior_values"] = prior_values
            counts[annotation] += 1
            seen[identity] = {
                **item,
                "_run_id": run.get("run_id"),
                "_generated_at": run.get("generated_at"),
            }
            candidates.append(item)
        current["candidates"] = candidates
        current["counts"] = counts
        current["suppressed_duplicates"] = counts["updated"] + counts["previously_seen"]
        annotated.append(current)
    return list(reversed(annotated))


def _early_marks_cache_state() -> str:
    candidates = [path for path in (EARLY_MARK_PROBE_PATH, EARLY_MARK_RUNS_PATH) if path.exists()]
    if not candidates:
        return "empty"
    newest_mtime = max(path.stat().st_mtime for path in candidates)
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(newest_mtime, timezone.utc)
    return "fresh" if age <= timedelta(hours=24) else "stale"


def _early_marks_claude_state() -> str:
    """Distinguish a configured key from a successfully verified enrichment."""
    if not settings.anthropic_api_key:
        return "not_configured"
    if not EARLY_MARK_PROBE_PATH.exists():
        return "configured"
    try:
        raw = json.loads(EARLY_MARK_PROBE_PATH.read_text())
    except Exception:
        return "configured"
    if not isinstance(raw, dict):
        return "configured"
    status = str(raw.get("claude_status") or "")
    if status == "succeeded" and raw.get("claude_results"):
        return "verified"
    if status == "failed":
        return "error"
    return "configured"


def _early_marks_category_quality() -> str:
    """Summarize per-category track record honestly; scans are category-blind."""
    stats_path = Path(__file__).parent / "data" / "category_stats.json"
    try:
        stats = json.loads(stats_path.read_text())
        parts = []
        if isinstance(stats, dict):
            for name, row in sorted(stats.items()):
                trades = int(row.get("trades") or 0)
                if trades <= 0:
                    continue
                won = int(row.get("won") or 0)
                parts.append(f"{name} {round((won / trades) * 100)}% ({won}/{trades})")
        if parts:
            return f"Track record by category (trading-agent stats, small samples): {', '.join(parts)}"
        return "No category track record yet — ranking is by market structure, not topic"
    except Exception:
        return "Category stats unavailable — ranking is by market structure, not topic"


async def _early_marks_kalshi_state() -> str:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get("https://api.elections.kalshi.com/trade-api/v2/exchange/status")
        if response.status_code >= 500:
            return "offline"
        if response.status_code >= 400:
            return "error"
        return "online"
    except Exception:
        return "offline"


def _read_early_mark_status_file() -> dict[str, Any]:
    if not EARLY_MARK_STATUS_PATH.exists():
        return {}
    try:
        raw = json.loads(EARLY_MARK_STATUS_PATH.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


@app.get("/static/desk.css")
async def serve_desk_css():
    """Serve the shared desk stylesheet."""
    css_path = Path(__file__).parent / "static" / "desk.css"
    if not css_path.exists():
        raise HTTPException(status_code=404, detail="desk.css not found")
    return Response(css_path.read_text(), media_type="text/css; charset=utf-8")


@app.get("/api/status")
async def get_status():
    """Get current bot status and stats."""
    return {
        **state.to_dict(),
        "kalshi_environment": "production",
        "risk": {
            "daily_pnl": risk_manager.daily_stats.total_pnl,
            "trades_today": risk_manager.daily_stats.trades_placed,
            "wins": risk_manager.daily_stats.trades_won,
            "losses": risk_manager.daily_stats.trades_lost,
            "consecutive_losses": risk_manager.daily_stats.consecutive_losses,
            "is_halted": risk_manager.daily_stats.is_halted,
            "halt_reason": risk_manager.daily_stats.halt_reason,
            "category_stats": risk_manager.category_stats,
        },
        "config": {
            "max_daily_loss": settings.max_daily_loss,
            "min_edge": settings.min_edge_threshold,
            "max_single_trade": settings.max_single_trade,
            "cycle_minutes": settings.cycle_interval_minutes,
        }
    }


@app.get("/api/logs")
async def get_logs():
    """Get recent activity logs."""
    return {"logs": state.recent_logs}


@app.get("/api/proposals")
async def get_proposals():
    """Get recent trade proposals from Claude."""
    return {"proposals": state.proposals}


@app.get("/api/positions")
async def get_positions():
    """Get current positions."""
    return {"positions": state.positions}


@app.get("/api/history")
async def get_history():
    """Get recent finalized trades from log."""
    history = []
    log_path = Path(settings.log_file)
    if log_path.exists():
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
                # Read backwards
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "resolution":
                            history.append(entry)
                        if len(history) >= 50:
                            break
                    except Exception:
                        continue
        except Exception:
            pass

    # Fetch missing titles dynamically
    if history:
        adapter = None
        for h in history:
            mid = h.get("market_id")
            if mid:
                if mid not in state.market_titles:
                    # First check if the question was already in the log entry
                    if h.get("question"):
                        state.market_titles[mid] = h["question"]
                    else:
                        if not adapter:
                            try:
                                adapter = get_adapter()
                                await adapter.connect()
                                await refresh_state_from_platform(adapter)
                            except Exception:
                                pass
                        if adapter:
                            try:
                                m = await adapter.get_market(mid)  # type: ignore
                                if m and hasattr(m, 'question'):
                                    state.market_titles[mid] = m.question  # type: ignore
                                else:
                                    state.market_titles[mid] = mid
                            except Exception:
                                state.market_titles[mid] = mid
                
                h["market_question"] = state.market_titles.get(mid, mid)

    return {"history": history}


@app.get("/api/transfers")
async def get_transfers():
    """Get history of deposits and withdrawals."""
    try:
        adapter = get_adapter()
        await adapter.connect()
        await refresh_state_from_platform(adapter)
        transfers = await adapter.get_transfers()
        # Sort by date descending
        transfers.sort(key=lambda x: x.created_at, reverse=True)
        return {"transfers": [
            {
                "id": t.id,
                "type": t.type,
                "amount": t.amount,
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "platform": t.platform
            } for t in transfers
        ]}
    except Exception as e:
        return {"transfers": [], "error": str(e)}


@app.get("/api/calibration")
async def get_calibration():
    """Get calibration report."""
    return {"report": logger.get_calibration_report()}


@app.get("/api/weather-validation")
async def get_weather_validation():
    """Get Kalshi weather-only paper validation report."""
    return {"report": logger.get_weather_validation_report()}


@app.get("/api/early-marks")
async def get_early_marks():
    """Get the latest read-only Early Marks repricing watchlist payload."""
    return _build_early_marks_payload()


@app.get("/api/early-marks/runs")
async def get_early_mark_runs():
    """Get Early Marks run history with point-in-time dedupe annotations."""
    return _annotate_early_mark_runs(_load_early_mark_runs())


@app.get("/api/early-marks/health")
async def get_early_marks_health():
    """Get readiness state for the read-only Early Marks workflow."""
    services = [
        {"id": "kalshi", "label": "Kalshi API", "state": await _early_marks_kalshi_state()},
        {"id": "llm", "label": "Claude API", "state": _early_marks_claude_state()},
        {"id": "research", "label": "Research API", "state": "configured" if settings.tavily_api_key else "not_configured"},
        {"id": "server", "label": "Local server", "state": "online"},
        {"id": "cache", "label": "Data cache", "state": _early_marks_cache_state()},
    ]
    if settings.openai_api_key:
        services.insert(2, {"id": "openai", "label": "OpenAI API", "state": "configured", "optional": True})
    return {
        "services": services,
        "category_quality": _early_marks_category_quality(),
        "category_quality_soccer": "Sports/Soccer — path-model research mode, no Early Marks track record yet.",
    }


@app.get("/api/early-marks/run/status")
async def get_early_marks_run_status():
    """Get best-effort status for a currently running Early Marks probe."""
    status_payload = _read_early_mark_status_file()
    stage = str(status_payload.get("stage") or "connect")
    return {
        "running": bool(EARLY_MARK_RUNNING.get("running")),
        "server_run_id": EARLY_MARK_RUNNING.get("run_id"),
        "run_id": status_payload.get("run_id"),
        "stage": stage,
        "stage_label": EARLY_MARK_STAGE_LABELS.get(stage, stage),
        "stages_done": status_payload.get("stages_done") if isinstance(status_payload.get("stages_done"), list) else [],
        "updated_at": status_payload.get("updated_at"),
    }


@app.get("/api/early-marks/image")
async def get_early_mark_image(name: str, category: str = ""):
    """Return a public thumbnail for a placement, or an initials fallback."""
    thumbnail = await _lookup_wikipedia_thumbnail(name, category)
    if thumbnail:
        return RedirectResponse(thumbnail)
    return Response(_early_mark_fallback_svg(name, category), media_type="image/svg+xml")


@app.post("/api/early-marks/run")
async def run_early_marks(payload: dict[str, Any] = Body(default_factory=dict)):
    """Run the read-only Early Marks probe manually from the page."""
    if EARLY_MARK_RUNNING.get("running"):
        return JSONResponse({"status": "error", "message": "Early Marks probe is already running."}, status_code=409)
    # Early marks are defined by lifecycle structure, not topic. Category is
    # an optional display filter — the default scans everything and lets the
    # structural ranking decide.
    requested_category = str(payload.get("category") or "all").strip().lower()
    if not re.fullmatch(r"[a-z0-9 _/&-]{1,40}", requested_category):
        return JSONResponse(
            {"status": "error", "message": "Invalid category filter."}, status_code=400
        )
    category = requested_category
    market_status = str(payload.get("market_status") or "all").lower()
    horizon = str(payload.get("horizon") or "all").lower()
    if market_status not in {"all", "open", "active", "preopen", "unopened"}:
        market_status = "all"
    if horizon not in {"all", "short", "medium", "long"}:
        horizon = "all"

    pages = max(1, min(int(_safe_float(payload.get("pages"), 2)), 5))
    page_limit = max(50, min(int(_safe_float(payload.get("page_limit"), 100)), 250))
    shortlist = max(5, min(int(_safe_float(payload.get("shortlist"), 18)), 30))
    min_placement = max(1.0, _safe_float(payload.get("min_placement_dollars"), 20.0))
    max_placement = max(min_placement, _safe_float(payload.get("max_placement_dollars"), 100.0))
    no_claude = bool(payload.get("no_claude"))
    run_id = f"early-marks-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"

    script = Path(__file__).parent / "scripts" / "manual_probes" / "test_early_marks.py"
    cmd = [
        sys.executable,
        str(script),
        "--pages",
        str(pages),
        "--page-limit",
        str(page_limit),
        "--shortlist",
        str(shortlist),
        "--category",
        category,
        "--market-status",
        market_status,
        "--horizon",
        horizon,
        "--min-placement-dollars",
        str(min_placement),
        "--max-placement-dollars",
        str(max_placement),
        "--run-id",
        run_id,
    ]
    if no_claude:
        cmd.append("--no-claude")

    EARLY_MARK_RUNNING["running"] = True
    EARLY_MARK_RUNNING["run_id"] = run_id
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(Path(__file__).parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        try:
            proc.kill()  # type: ignore[name-defined]
        except Exception:
            pass
        return JSONResponse({"status": "error", "message": "Early Marks probe timed out."}, status_code=504)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)
    finally:
        EARLY_MARK_RUNNING["running"] = False

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    if proc.returncode != 0:
        print("Early Marks probe failed.")
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        failure_detail = None
        failure_payload = None
        try:
            raw_failure = json.loads(EARLY_MARK_PROBE_PATH.read_text())
            if isinstance(raw_failure, dict) and raw_failure.get("run_id") == run_id:
                failure_detail = str(raw_failure.get("claude_parse_error") or "") or None
                failure_payload = _build_early_marks_payload()
        except Exception:
            pass
        return JSONResponse(
            {
                "status": "error",
                "ui_state": "failed",
                "run_id": run_id,
                "message": "Early Marks Claude enrichment failed." if failure_detail else "Early Marks probe failed.",
                "detail": failure_detail,
                "payload": failure_payload,
            },
            status_code=502 if failure_detail else 500,
        )

    built_payload = _build_early_marks_payload()
    enrichment = built_payload.get("enrichment") if isinstance(built_payload.get("enrichment"), dict) else {}
    if (
        not no_claude
        and settings.anthropic_api_key
        and enrichment.get("status") == "failed"
    ):
        return JSONResponse(
            {
                "status": "error",
                "ui_state": "failed",
                "run_id": run_id,
                "message": "Early Marks Claude enrichment failed validation.",
                "detail": enrichment.get("error"),
                "payload": built_payload,
            },
            status_code=502,
        )
    filters_used = {
        "category": category,
        "market_status": market_status,
        "horizon": horizon,
        "pages": pages,
        "page_limit": page_limit,
        "shortlist": shortlist,
        "min_placement_dollars": min_placement,
        "max_placement_dollars": max_placement,
        "no_claude": no_claude,
    }
    _append_early_mark_run(built_payload, filters_used, run_id=run_id)
    annotated_runs = _annotate_early_mark_runs(_load_early_mark_runs())
    current_run = annotated_runs[0] if annotated_runs else None
    limited_services = []
    if no_claude or not settings.anthropic_api_key:
        limited_services.append("Claude API")
    if not settings.tavily_api_key:
        limited_services.append("Research API")

    return {
        "status": "success",
        "run_id": run_id,
        "ui_state": "partial" if limited_services else "complete",
        "limited_services": limited_services,
        "run": current_run,
        "payload": built_payload,
    }


@app.get("/api/world-cup/status")
async def get_world_cup_status():
    """Get real-data status and provider gates for the World Cup route."""
    return {
        **world_cup_service.route_status(),
        "environment": redacted_environment_status(),
    }


@app.get("/api/world-cup/matchday")
async def get_world_cup_matchday(user_timezone: str = "America/New_York"):
    """Get the World Cup matchday dashboard payload."""
    return await world_cup_service.get_matchday(user_timezone=user_timezone)


@app.post("/api/world-cup/evaluate")
async def evaluate_world_cup(payload: dict | None = None, _: None = Depends(require_api_token)):
    """Evaluate current World Cup matchday markets without placing orders."""
    return await world_cup_service.evaluate(payload or {})


@app.post("/api/world-cup/settings")
async def update_world_cup_settings(updates: dict, _: None = Depends(require_api_token)):
    """Update route-local World Cup settings."""
    return world_cup_service.update_settings(updates)


@app.post("/api/world-cup/approval-lock")
async def lock_world_cup_approval(payload: dict, _: None = Depends(require_api_token)):
    """Create an immutable approval-card lock. Does not place orders."""
    card = payload.get("card") if isinstance(payload, dict) else None
    if not isinstance(card, dict):
        return JSONResponse({"status": "error", "error": "card is required"}, status_code=400)
    return world_cup_service.approval_lock(card)


@app.post("/api/world-cup/paper-approve")
async def paper_approve_world_cup(payload: dict, _: None = Depends(require_api_token)):
    """Approve only the exact locked card for paper simulation."""
    return world_cup_service.paper_approve(payload)


@app.post("/api/analyze")
async def run_deep_analyze(_: None = Depends(require_api_token)):
    """Run a deep reflection over past trades to extract macro-lessons without overfitting."""
    from core.analyzer import TradeAnalyzer
    state.add_log("info", "🧠 Starting Deep Reflection Analysis...")
    analyzer = TradeAnalyzer()
    try:
        result = await analyzer.run_deep_reflection()
        analyzer.check_and_retire_lessons()
        accepted = result.get("lessons_accepted", 0)
        blocked = result.get("hallucinated_blocked", 0)
        lenses = result.get("lenses_used", [])
        state.add_log("success", f"Deep Reflection complete via {', '.join(lenses)}. Accepted {accepted} lessons, blocked {blocked} hallucinated.")
        return result
    except Exception as e:
        state.add_log("error", f"Deep Reflection failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/lessons")
async def get_lessons():
    """Get all macro-lessons with their status and metadata."""
    from core.memory_manager import MemoryManager
    mm = MemoryManager()
    raw = mm.get_macro_lessons_raw()
    return {"lessons": raw}


@app.get("/api/paper/analytics")
async def get_paper_analytics():
    """
    Process trades.jsonl to aggregate paper/shadow trade performance metrics
    grouped by size buckets (Micro, Small, Medium, Large, Mega).
    """
    buckets = {
        "micro": {"name": "Micro (<$50)", "count": 0, "won": 0, "pnl": 0.0, "total_size": 0.0},
        "small": {"name": "Small ($50-$250)", "count": 0, "won": 0, "pnl": 0.0, "total_size": 0.0},
        "medium": {"name": "Medium ($250-$1,000)", "count": 0, "won": 0, "pnl": 0.0, "total_size": 0.0},
        "large": {"name": "Large ($1,000-$5,000)", "count": 0, "won": 0, "pnl": 0.0, "total_size": 0.0},
        "mega": {"name": "Mega (>=$5,000)", "count": 0, "won": 0, "pnl": 0.0, "total_size": 0.0},
    }
    
    trade_sizes = {}
    log_path = Path(settings.log_file)
    if log_path.exists():
        try:
            with open(log_path, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        t_type = entry.get("type")
                        if t_type == "trade" and entry.get("approved"):
                            trade_sizes[entry["market_id"]] = entry["size_usd"]
                        elif t_type == "resolution":
                            market_id = entry.get("market_id")
                            pnl = entry.get("pnl", 0.0)
                            price = entry.get("market_price_at_entry", 0.5)
                            
                            # Determine size
                            size = trade_sizes.get(market_id)
                            if size is None:
                                if pnl < 0:
                                    size = abs(pnl)
                                else:
                                    if price < 1.0:
                                        size = pnl * price / (1.0 - price)
                                    else:
                                        size = pnl
                            
                            if size <= 0:
                                continue
                                
                            # Sort into buckets
                            if size < 50.0:
                                b_key = "micro"
                            elif size < 250.0:
                                b_key = "small"
                            elif size < 1000.0:
                                b_key = "medium"
                            elif size < 5000.0:
                                b_key = "large"
                            else:
                                b_key = "mega"
                                
                            buckets[b_key]["count"] += 1
                            buckets[b_key]["pnl"] += pnl
                            buckets[b_key]["total_size"] += size
                            if pnl > 0:
                                buckets[b_key]["won"] += 1
                    except Exception:
                        continue
        except Exception:
            pass
            
    # Calculate averages and win rates
    formatted = []
    for key, data in buckets.items():
        count = data["count"]
        pnl = data["pnl"]
        won = data["won"]
        total_size = data["total_size"]
        
        win_rate = (won / count * 100.0) if count > 0 else 0.0
        avg_size = (total_size / count) if count > 0 else 0.0
        roi = (pnl / total_size * 100.0) if total_size > 0 else 0.0
        
        formatted.append({
            "key": key,
            "name": data["name"],
            "count": count,
            "win_rate": round(win_rate, 1),
            "pnl": round(pnl, 2),
            "avg_size": round(avg_size, 2),
            "roi": round(roi, 1),
        })
        
    return {"analytics": formatted}


@app.post("/api/recommendations")
async def get_recommendations(_: None = Depends(require_api_token)):
    """Generate recommendations on what to do next based on bot state."""
    from core.analyzer import TradeAnalyzer  # type: ignore
    from core.memory_manager import MemoryManager  # type: ignore
    
    mm = MemoryManager()
    analyzer = TradeAnalyzer()
    
    active_lessons = [l for l in mm.get_macro_lessons_raw() if l.get("status") in ("active", "probationary")]
    
    try:
        # Manual slicing to satisfy IDE
        all_positions = list(state.positions) if state.positions else []
        recent_positions = []
        if all_positions:
            p_len = len(all_positions)
            p_start = p_len - 5 if p_len > 5 else 0
            for i in range(p_start, p_len):
                recent_positions.append(all_positions[i])
        
        raw_lessons = list(active_lessons) if active_lessons else []
        recent_lessons = []
        if raw_lessons:
            l_len = len(raw_lessons)
            l_start = l_len - 5 if l_len > 5 else 0
            for i in range(l_start, l_len):
                recent_lessons.append(raw_lessons[i])
        
        open_positions = []
        if recent_positions:
            for p in recent_positions:
                if isinstance(p, dict):
                    open_positions.append({"ticker": str(p.get("ticker", "")), "size": str(p.get("size", ""))})

        lessons_list = []
        if recent_lessons:
            for l in recent_lessons:
                if isinstance(l, dict):
                    lessons_list.append(str(l.get("lesson", "")))

        state_data = {
            "balance": state.balance,
            "mode": state.mode,
            "open_positions": open_positions,
            "lessons": lessons_list
        }
    except Exception:
        state_data = {"balance": state.balance, "mode": state.mode, "open_positions": [], "lessons": []}
    
    recs = await analyzer.generate_recommendations(state_data)
    return {"status": "success", "recommendations": recs}


def weather_live_validation_error(mode: str) -> dict | None:
    """Return an error payload when weather_live is blocked by evidence gates."""
    if mode != "weather_live" or settings.allow_weather_live_before_validation:
        return None

    summary = logger.get_weather_validation_summary()
    if summary.get("readiness") in ("LIVE_REVIEW", "SCALE_REVIEW"):
        return None

    count = summary.get("resolved_count", 0)
    remaining = summary.get("live_remaining", 50)
    return {
        "error": (
            "weather_live is blocked until Kalshi weather validation reaches LIVE_REVIEW. "
            f"Resolved weather trades: {count}; need {remaining} more. "
            "Run weather_paper and review /api/weather-validation."
        ),
        "readiness": summary.get("readiness", "PAPER_ONLY"),
        "resolved_weather_trades": count,
        "needed_for_live_review": remaining,
    }


@app.post("/api/start")
async def start_bot(
    mode: str = "paper",
    paper_balance: float | None = None,
    _: None = Depends(require_api_token),
):
    """Start the trading bot.

    Optional `paper_balance` query param sets the starting paper bankroll
    when mode is paper. Ignored for live mode.
    """
    global bot_task
    if state.is_running:
        return {"error": "Bot is already running"}

    is_paper_mode = mode == "paper" or mode.endswith("_paper")
    validation_error = weather_live_validation_error(mode)
    if validation_error:
        return validation_error

    was_paper_mode = state.mode == "paper" or state.mode.endswith("_paper")
    if is_paper_mode:
        if paper_balance is not None:
            if paper_balance <= 0:
                return {"error": "paper_balance must be greater than 0"}
            settings.paper_balance = paper_balance
            state.balance = paper_balance
            state.initial_balance = paper_balance
        else:
            # If not transitioning from a paper mode, or balance is currently 0, reset to settings default
            if not was_paper_mode or state.balance == 0.0:
                state.balance = settings.paper_balance
                state.initial_balance = settings.paper_balance
    elif paper_balance is not None:
        return {"error": "paper_balance is only valid for paper modes"}

    state.is_running = True
    state.is_paused = False
    state.mode = mode
    state.active_directive = None  # Clear any old directive
    state.add_log("info", f"🟢 Starting bot in {mode.upper()} mode")
    if is_paper_mode:
        state.add_log("info", f"Paper bankroll: ${settings.paper_balance:.2f}")

    state.is_paused = False
    state.mode = mode
    settings.trading_mode = mode
    bot_task = asyncio.create_task(trading_loop())
    state.add_log("info", f"Bot started in {mode} mode")
    return {"status": "started", "mode": mode, "paper_balance": settings.paper_balance if is_paper_mode else None}


@app.post("/api/stop")
async def stop_bot(_: None = Depends(require_api_token)):
    """Stop the trading bot."""
    global bot_task
    state.is_running = False
    if bot_task and not bot_task.done():
        bot_task.cancel()
    state.add_log("info", "Bot stopped by user")
    return {"status": "stopped"}


@app.post("/api/pause")
async def pause_bot(_: None = Depends(require_api_token)):
    """Pause/resume the trading bot."""
    state.is_paused = not state.is_paused
    action = "paused" if state.is_paused else "resumed"
    state.add_log("info", f"Bot {action} by user")
    return {"status": action, "is_paused": state.is_paused}


@app.post("/api/settings")
async def update_settings(updates: dict, _: None = Depends(require_api_token)):
    """Update bot settings on the fly."""
    allowed = {"min_edge_threshold", "max_single_trade", "max_daily_loss",
               "cycle_interval_minutes", "min_market_volume",
               "paper_balance"}
    is_paper_mode = state.mode == "paper" or state.mode.endswith("_paper")
    for key, value in updates.items():
        if key in allowed and hasattr(settings, key):
            if key == "paper_balance":
                try:
                    new_balance = float(value)
                except (TypeError, ValueError):
                    continue
                if new_balance <= 0:
                    continue
                settings.paper_balance = new_balance
                # If we're currently in paper mode and the bot hasn't traded yet,
                # reflect the new bankroll in live state so the dashboard updates.
                if is_paper_mode:
                    state.balance = new_balance
                    state.initial_balance = new_balance
                state.add_log("info", f"Paper bankroll updated to ${new_balance:.2f}")
                continue
            setattr(settings, key, float(value) if isinstance(value, (int, float)) else value)
            state.add_log("info", f"Setting updated: {key} = {value}")
    return {"status": "updated"}


@app.post("/api/intel/scan")
async def scan_market_intel(_: None = Depends(require_api_token)):
    """Analyze the current market and return a strategy intel report."""
    from core.analyzer import TradeAnalyzer
    
    try:
        adapter = get_adapter()
        # Always connect fresh to ensure we have auth for signing
        await adapter.connect()
            
        # Handle paper mode vs live mode balance check
        is_paper_mode = state.mode == "paper" or state.mode.endswith("_paper")
        if is_paper_mode:
            # In paper mode, use current paper balance or default to settings.paper_balance
            if state.balance == 0.0:
                state.balance = settings.paper_balance
                state.initial_balance = settings.paper_balance
            live_balance = state.balance
        else:
            # Fetch live balance directly — state.balance may be stale/zero
            live_balance = await adapter.get_balance()
            state.balance = live_balance  # update state too
        
        markets_obj = await adapter.get_markets(limit=250)
        if not markets_obj:
            return {"status": "error", "message": "Failed to fetch active markets from platform"}
            
        # Send ALL markets to Claude — let the AI decide what's interesting.
        # Kalshi often reports 0 for volume_24h, so filtering is counterproductive.
        markets_data = []
        for m in markets_obj:
            markets_data.append({
                "ticker": m.id,
                "title": m.question,
                "yes_ask": round(m.yes_price, 2),
                "no_ask": round(m.no_price, 2),
                "volume": m.total_volume,
                "volume_24h": m.volume_24h,
                "liquidity": m.liquidity,
                "end_date": m.end_date.isoformat() if m.end_date else "N/A",
                "category": m.category,
            })
        
        # Sort by total volume descending
        markets_data.sort(key=lambda x: x["volume"], reverse=True)
            
        analyzer = TradeAnalyzer()
        report = await analyzer.generate_market_intel(markets_data, live_balance)
        
        if report:
            state.add_log("success", "🧠 Market Intel Scan complete.")
            return {"status": "success", "report": report}
        else:
            return {"status": "error", "message": "Intel Recon Engine failed to generate a report."}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

@app.post("/api/start-with-directive")
async def start_with_directive(data: dict, _: None = Depends(require_api_token)):
    """Start the trading bot with a specific strategic directive from Intel Recon."""
    global bot_task
    if state.is_running:
        return {"error": "Bot is already running. Please stop it first."}

    mode = data.get("mode", "paper")
    directive = data.get("directive", "")
    
    state.is_running = True
    state.is_paused = False
    state.mode = mode
    state.active_directive = directive

    state.add_log("info", f"🚀 Starting bot in {mode.upper()} mode via Market Intel Directive.")
    if directive:
        state.add_log("info", f"🎯 Directive Active: {directive[:50]}...")
        
    bot_task = asyncio.create_task(trading_loop())
    return {"status": "started", "mode": mode, "directive": directive}



# ── Serve Dashboard ──────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    """Serve the React dashboard."""
    dashboard_path = Path(__file__).parent / "dashboard.html"
    if dashboard_path.exists():
        html = dashboard_path.read_text()
        html = html.replace("__API_TOKEN__", API_TOKEN)
        return HTMLResponse(html)
    return HTMLResponse("<h1>Dashboard not found</h1>")


@app.get("/paper")
async def serve_paper():
    """Serve the paper dashboard."""
    paper_path = Path(__file__).parent / "paper.html"
    if paper_path.exists():
        html = paper_path.read_text()
        html = html.replace("__API_TOKEN__", API_TOKEN)
        return HTMLResponse(html)
    return HTMLResponse("<h1>Paper dashboard not found</h1>")


@app.get("/world-cup")
async def serve_world_cup():
    """Serve the real-data-only World Cup assistant route."""
    page_path = Path(__file__).parent / "world_cup.html"
    if page_path.exists():
        html = page_path.read_text()
        html = html.replace("__API_TOKEN__", API_TOKEN)
        return HTMLResponse(html)
    return HTMLResponse("<h1>World Cup dashboard not found</h1>")


@app.get("/early-marks")
async def serve_early_marks():
    """Serve the Early Marks repricing watchlist route."""
    page_path = Path(__file__).parent / "early_marks.html"
    if page_path.exists():
        html = page_path.read_text()
        html = html.replace("__API_TOKEN__", API_TOKEN)
        return HTMLResponse(html)
    return HTMLResponse("<h1>Early Marks dashboard not found</h1>")

if __name__ == "__main__":
    import uvicorn  # type: ignore
    print("\n🌐 Starting dashboard at http://127.0.0.1:8000")
    print("   Open this URL in your browser\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
