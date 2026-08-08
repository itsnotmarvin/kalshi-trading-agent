import asyncio
from datetime import datetime, timezone
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Fetch 200 markets
    markets = await ada.get_markets(limit=200, min_volume=0)
    print(f"Fetched {len(markets)} markets")
    
    now = datetime.now(timezone.utc)
    
    for m in markets:
        if m.end_date:
            diff = m.end_date - now
            print(f"[{m.id}] {m.end_date} (Diff: {diff})")
        else:
            print(f"[{m.id}] NO END DATE")

if __name__ == "__main__":
    asyncio.run(main())
