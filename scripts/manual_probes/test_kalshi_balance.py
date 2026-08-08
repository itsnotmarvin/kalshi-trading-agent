import asyncio
import os
from dotenv import load_dotenv

load_dotenv(".env")
from adapters.kalshi_adapter import KalshiAdapter

async def run_test_balance():
    adapter = KalshiAdapter()
    await adapter.connect()
    resp = await adapter._request("GET", "/portfolio/balance")
    print("BALANCE API RESPONSE:", resp)

if __name__ == "__main__":
    asyncio.run(run_test_balance())
