import asyncio
from datetime import datetime, timezone, timedelta
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Fetch markets
    markets = await ada.get_markets(limit=200, min_volume=100)
    
    now = datetime.now(timezone.utc)
    short_term = []
    
    for m in markets:
        if m.end_date:
            time_left = m.end_date - now
            if time_left < timedelta(hours=48):
                short_term.append((m, time_left))
                
    print(f"\n--- Found {len(short_term)} markets resolving within 48h ---")
    for m, tl in sorted(short_term, key=lambda x: x[1]):
        hours = tl.total_seconds() / 3600
        print(f"[{m.category}] (In {hours:.1f}h) {m.question} - Vol: ${m.volume_24h}")

if __name__ == "__main__":
    asyncio.run(main())
