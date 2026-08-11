import asyncio
import os
from adapters.kalshi_adapter import KalshiAdapter
from config.settings import settings

async def main():
    print("Testing get_weather_markets...")
    adapter = KalshiAdapter()
    connected = await adapter.connect()
    if not connected:
        print("Failed to connect")
        return
    
    start_time = asyncio.get_event_loop().time()
    markets = await adapter.get_weather_markets(limit=10)
    end_time = asyncio.get_event_loop().time()
    
    print(f"Found {len(markets)} weather markets in {end_time - start_time:.2f} seconds")
    for m in markets:
        print(f" - {m.id}: {m.question} (${m.yes_price})")

if __name__ == "__main__":
    asyncio.run(main())
