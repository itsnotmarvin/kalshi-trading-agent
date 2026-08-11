import asyncio
from datetime import datetime, timezone, timedelta
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Fetch markets with low volume
    markets = await ada.get_markets(limit=200, min_volume=0)
    
    now = datetime.now(timezone.utc)
    print(f"Current time: {now}")
    
    found = 0
    for m in markets:
        if m.end_date:
            diff = m.end_date - now
            if diff < timedelta(hours=48) and diff > timedelta(0):
                print(f"MATCH: [{m.id}] Ends in {diff.total_seconds()/3600:.1f}h - {m.question}")
                found += 1
            elif diff < timedelta(days=7) and diff > timedelta(0):
                # print(f"Within 7d: [{m.id}] {diff}")
                pass
        else:
            # print(f"No end_date for {m.id}")
            pass
            
    print(f"\nTotal ST markets found: {found}")

if __name__ == "__main__":
    asyncio.run(main())
