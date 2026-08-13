"""
Configuration - loads from .env and provides typed settings.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Prefer exported environment variables, then the project-local .env, then
# the parent repository's legacy .env location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

from config.paths import TRADE_LOG_PATH, resolve_project_path


@dataclass
class Settings:
    # LLM Provider
    # Options:
    # - anthropic: existing Claude-powered behavior
    # - openai: ChatGPT/OpenAI-powered behavior when OPENAI_API_KEY is set
    # - mock: no paid LLM calls; returns safe HOLD proposals for UI/dev testing
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic").lower())

    # Claude API
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5"))
    claude_scan_model: str = field(default_factory=lambda: os.getenv("CLAUDE_SCAN_MODEL", "claude-opus-5"))
    thinking_budget: int = 10000  # Legacy setting; Opus 5 uses adaptive thinking + effort.
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # Market scanning
    # 0 means no cap. The World Cup workflow intentionally evaluates every
    # available market one by one instead of shortcutting to only a top few.
    market_scan_limit: int = field(default_factory=lambda: int(os.getenv("MARKET_SCAN_LIMIT", "0")))

    # OpenAI / ChatGPT API
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    openai_scan_model: str = field(default_factory=lambda: os.getenv("OPENAI_SCAN_MODEL", "gpt-5.4-mini"))

    # World Cup assistant route - real-data only.
    # This route has its own provider gates so generic development behavior
    # cannot accidentally leak placeholder recommendations into World Cup workflows.
    world_cup_claude_model: str = field(default_factory=lambda: os.getenv("WORLD_CUP_CLAUDE_MODEL", "claude-opus-5"))
    world_cup_claude_fallback_model: str = field(default_factory=lambda: os.getenv("WORLD_CUP_CLAUDE_FALLBACK_MODEL", ""))
    world_cup_openai_enabled: bool = field(default_factory=lambda: os.getenv("WORLD_CUP_OPENAI_ENABLED", "false").lower() == "true")
    world_cup_both_mode_enabled: bool = field(default_factory=lambda: os.getenv("WORLD_CUP_BOTH_MODE_ENABLED", "false").lower() == "true")
    world_cup_both_combination_rule: str = field(default_factory=lambda: os.getenv("WORLD_CUP_BOTH_COMBINATION_RULE", ""))
    world_cup_schedule_days_ahead: int = field(default_factory=lambda: int(os.getenv("WORLD_CUP_SCHEDULE_DAYS_AHEAD", "45")))
    world_cup_quote_fresh_seconds: int = field(default_factory=lambda: int(os.getenv("WORLD_CUP_QUOTE_FRESH_SECONDS", "30")))
    world_cup_combo_quote_fresh_seconds: int = field(default_factory=lambda: int(os.getenv("WORLD_CUP_COMBO_QUOTE_FRESH_SECONDS", "10")))
    world_cup_calibration_file: str = field(default_factory=lambda: str(
        resolve_project_path(os.getenv(
            "WORLD_CUP_CALIBRATION_FILE",
            str(TRADE_LOG_PATH.parent / "world_cup_calibration.json"),
        ))
    ))

    # Platform selection
    platform: str = field(default_factory=lambda: os.getenv("PLATFORM", "kalshi"))
    trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "paper"))

    # Paper Trading
    # Starting bankroll used for paper-mode position sizing and risk checks.
    # Override via PAPER_BALANCE env, --paper-balance CLI flag, or /api/start payload.
    paper_balance: float = field(default_factory=lambda: float(os.getenv("PAPER_BALANCE", "1000")))

    # Kalshi — Production credentials
    kalshi_api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))
    kalshi_private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", ""))

    # Polymarket
    polymarket_private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    polymarket_funder: str = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER_ADDRESS", ""))

    # Risk Management
    max_portfolio_value: float = field(default_factory=lambda: float(os.getenv("MAX_PORTFOLIO_VALUE", "500")))
    max_single_trade: float = field(default_factory=lambda: float(os.getenv("MAX_SINGLE_TRADE", "25")))
    max_daily_loss: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS", "50")))
    min_edge_threshold: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE_THRESHOLD", "0.08")))
    # Optional floor on the probability of the purchased side paying out.
    # Defaults to 0 so long-odds trades are not blocked solely for being
    # unlikely; calibration and the probability-gap gate control eligibility.
    min_payout_likelihood: float = field(default_factory=lambda: float(os.getenv("MIN_PAYOUT_LIKELIHOOD", "0")) / 100.0)
    take_profit_threshold: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_THRESHOLD", "0.20")))
    min_market_volume: float = field(default_factory=lambda: float(os.getenv("MIN_MARKET_VOLUME", "50000")))
    max_positions: int = 12  # Max concurrent positions for diversification
    max_correlation_overlap: int = 3  # Max positions in same category
    max_category_exposure_pct: float = field(default_factory=lambda: float(os.getenv("MAX_CATEGORY_EXPOSURE_PCT", "0.35")))

    # Execution and fixed-stake policies
    min_position_size: float = field(default_factory=lambda: float(os.getenv("MIN_POSITION_SIZE", "3.0")))
    use_maker_orders: bool = field(default_factory=lambda: os.getenv("USE_MAKER_ORDERS", "false").lower() == "true")
    max_orderbook_spread: float = field(default_factory=lambda: float(os.getenv("MAX_ORDERBOOK_SPREAD", "0.08")))
    min_orderbook_depth_usd: float = field(default_factory=lambda: float(os.getenv("MIN_ORDERBOOK_DEPTH_USD", "25")))
    blocked_categories: list[str] = field(default_factory=lambda: [c.strip() for c in os.getenv("BLOCKED_CATEGORIES", "Economics").split(",") if c.strip()])

    # Agent Loop
    cycle_interval_minutes: int = field(default_factory=lambda: int(os.getenv("CYCLE_INTERVAL_MINUTES", "30")))
    log_file: str = field(default_factory=lambda: str(
        resolve_project_path(os.getenv("LOG_FILE", str(TRADE_LOG_PATH)))
    ))
    allow_weather_live_before_validation: bool = field(default_factory=lambda: os.getenv("ALLOW_WEATHER_LIVE_BEFORE_VALIDATION", "false").lower() == "true")

    # Telegram Notifications
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    dashboard_api_token: str = field(default_factory=lambda: os.getenv("DASHBOARD_API_TOKEN", ""))

    def validate(self) -> list[str]:
        """Return list of configuration errors."""
        errors = []
        if self.llm_provider not in {"anthropic", "openai", "mock"}:
            errors.append("LLM_PROVIDER must be one of: anthropic, openai, mock")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.llm_provider == "openai" and not self.openai_api_key:
            errors.append("OPENAI_API_KEY is required when LLM_PROVIDER=openai; use LLM_PROVIDER=mock for free local testing")
        if self.platform == "kalshi" and not self.kalshi_api_key_id:
            errors.append("KALSHI_API_KEY_ID required when platform=kalshi")
        if self.platform == "polymarket" and self.trading_mode == "live":
            errors.append("Polymarket live trading is not implemented; use paper mode only")
        return errors


# Singleton
settings = Settings()
