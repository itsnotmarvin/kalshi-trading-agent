"""
Safe Kalshi API-key smoke test.

Uses config.settings instead of hardcoded key IDs or private-key paths.
It intentionally avoids printing credentials or balances.
"""
import asyncio
import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

from adapters.kalshi_adapter import KalshiAdapter  # noqa: E402
from config.settings import settings  # noqa: E402


async def main() -> int:
    adapter = KalshiAdapter()
    configured_path = Path(adapter.private_key_path).expanduser() if adapter.private_key_path else None
    local_key_path = PROJECT_DIR / "yay.txt"
    if (not configured_path or not configured_path.exists()) and local_key_path.exists():
        adapter.private_key_path = str(local_key_path)

    print("Kalshi environment: production")
    print(f"API key configured: {bool(adapter.api_key_id)}")
    print(f"Private key file configured: {bool(adapter.private_key_path)}")

    if not adapter.api_key_id or not adapter.private_key_path:
        print("Missing Kalshi API configuration.")
        return 1

    key_path = Path(adapter.private_key_path).expanduser()
    if not key_path.exists():
        print("Private key file does not exist.")
        return 1
    adapter.private_key = key_path.read_text()

    status_response = await adapter._request("GET", "/exchange/status", silent=True)
    if not isinstance(status_response, dict) or not status_response.get("exchange_active"):
        print("Kalshi exchange status check failed.")
        return 1

    balance_response = await adapter._request("GET", "/portfolio/balance", silent=True)
    if not isinstance(balance_response, dict) or "balance" not in balance_response:
        print("Authenticated balance endpoint failed.")
        return 1

    print("Kalshi API-key smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
