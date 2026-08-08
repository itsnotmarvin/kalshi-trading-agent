import asyncio
import os
from adapters.kalshi_adapter import KalshiAdapter
app = KalshiAdapter()
async def run_test():
    await app.connect()
    markets = await app._request("GET", "/markets", params={"status": "open", "category": "Climate and Weather", "limit": 100})
    if markets and "markets" in markets:
        print("Found", len(markets["markets"]), "markets in Climate and Weather")
    
    markets2 = await app._request("GET", "/markets", params={"status": "open", "category": "Climate", "limit": 100})
    if markets2 and "markets" in markets2:
        print("Found", len(markets2["markets"]), "markets in Climate")
        
asyncio.run(run_test())
