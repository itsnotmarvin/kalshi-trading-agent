import asyncio
import json
from adapters.kalshi_adapter import KalshiAdapter
from config.settings import settings

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Fetch a large sample of markets
    markets = await ada.get_markets(limit=100, min_volume=0)
    
    categories = {}
    for m in markets:
        cat = m.category.lower()
        categories[cat] = categories.get(cat, 0) + 1
        
    print("\n--- Available Categories ---")
    for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
        print(f"{cat}: {count} markets")
        
    print("\n--- Sample of Market Titles ---")
    for m in markets[:10]:
        print(f"[{m.category}] {m.question} (Vol: ${m.volume_24h})")

if __name__ == "__main__":
    asyncio.run(main())
