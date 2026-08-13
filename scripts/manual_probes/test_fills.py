import asyncio
import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
load_dotenv("/Users/marbin/kalshi/.env")

from adapters.kalshi_adapter import KalshiAdapter

async def get_and_format_fills():
    adapter = KalshiAdapter()
    await adapter.connect()
    
    # Fetch fills
    resp = await adapter._request("GET", "/portfolio/fills", params={"limit": 200})
    if not resp or "fills" not in resp:
        print("Failed to fetch fills or no fills found.")
        return
        
    fills = resp["fills"]
    print(f"\nSuccessfully fetched {len(fills)} fills from Kalshi.\n")
    
    print(f"{'Date/Time':<25} | {'Ticker':<45} | {'Action':<6} | {'Side':<4} | {'Price':<6} | {'Qty':<5} | {'Fee':<6} | {'Taker?':<6}")
    print("-" * 120)
    
    for f in sorted(fills, key=lambda x: x.get("created_time", ""), reverse=True):
        dt = f.get("created_time", "")[:19].replace("T", " ")
        ticker = f.get("ticker", "")
        action = f.get("action", "").upper()
        side = f.get("side", "").upper()
        
        # Determine actual price paid for the chosen side
        yes_price = float(f.get("yes_price_dollars", 0.0))
        no_price = float(f.get("no_price_dollars", 0.0))
        price = yes_price if side == "YES" else no_price
        
        qty = float(f.get("count_fp", 0.0))
        fee = float(f.get("fee_cost", 0.0))
        is_taker = "Yes" if f.get("is_taker") else "No"
        
        print(f"{dt:<25} | {ticker:<45} | {action:<6} | {side:<4} | ${price:<5.2f} | {qty:<5.0f} | ${fee:<5.2f} | {is_taker:<6}")

if __name__ == "__main__":
    asyncio.run(get_and_format_fills())
