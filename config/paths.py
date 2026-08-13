"""Canonical project paths used by application, scripts, and web routes."""
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "web"
REFERENCE_DATA_DIR = PROJECT_ROOT / "data" / "reference"


def resolve_project_path(raw: str | Path) -> Path:
    """Resolve a configured path consistently, independent of process cwd."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _configured_path(env_name: str, default: Path) -> Path:
    raw = os.getenv(env_name, "").strip()
    return resolve_project_path(raw) if raw else default


RUNTIME_DATA_DIR = _configured_path(
    "KALSHI_DATA_DIR", PROJECT_ROOT / "data" / "runtime"
)
TRADE_LOG_PATH = RUNTIME_DATA_DIR / "trades.jsonl"
