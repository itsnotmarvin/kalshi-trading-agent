import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    cursor = None
    total_events = 0
    total_markets = 0
    found_crypto = []
    
    # Loop to fetch all pages of open events
    for i in range(5):  # check first 5 pages mapping to up to 1000 events
        params = {"limit": 200, "status": "open", "with_nested_markets": True}
        if cursor:
            params["cursor"] = cursor
            
        resp = await ada._request("GET", "/events", params)
        if not resp or "events" not in resp:
            break
            
        events = resp["events"]
        total_events += len(events)
        
        for e in events:
            for m in e.get("markets", []):
                total_markets += 1
                txt = f"{m.get('title', '')} {m.get('subtitle', '')} {e.get('category', '')}".lower()
                if "btc" in txt or "bitcoin" in txt or "crypto" in txt or "eth" in txt or "ethereum" in txt:
                    found_crypto.append(f"{m.get('ticker')} | {m.get('title')} | Cat: {e.get('category')} | Vol: {m.get('volume_24h_fp')}")
        
        cursor = resp.get("cursor")
        if not cursor:
            break
            
    print(f"Scanned {total_events} events with {total_markets} total nested markets.")
    print(f"Found {len(found_crypto)} crypto markets.")
    for x in found_crypto[:10]:
        print("  -", x)

if __name__ == "__main__":
    asyncio.run(main())
