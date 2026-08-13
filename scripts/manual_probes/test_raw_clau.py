import asyncio
import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
load_dotenv("/Users/marbin/kalshi/.env")

from adapters.kalshi_adapter import KalshiAdapter

async def print_raw_clau():
    adapter = KalshiAdapter()
    await adapter.connect()
    
    resp = await adapter._request("GET", "/portfolio/fills", params={"limit": 200})
    if not resp or "fills" not in resp:
        return
        
    for f in resp["fills"]:
        ticker = f.get("ticker", "")
        if "CLAU" in ticker:
            print(f"Ticker: {ticker} | Action: {f.get('action')} | Side: {f.get('side')} | Qty: {f.get('count_fp')} | YesPrice: {f.get('yes_price_dollars')} | NoPrice: {f.get('no_price_dollars')} | OutcomeSide: {f.get('outcome_side')}")

if __name__ == "__main__":
    asyncio.run(print_raw_clau())
