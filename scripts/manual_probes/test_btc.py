import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    resp = await ada._request("GET", "/events", {"limit": 200, "status": "open", "with_nested_markets": True})
    
    btc_markets = []
    for event in resp.get("events", []):
        for m in event.get("markets", []):
            title = m.get("title", "").lower()
            subtitle = m.get("subtitle", "").lower()
            if "bitcoin" in title or "btc" in title or "bitcoin" in subtitle or "btc" in subtitle:
                btc_markets.append(m)
                
    print(f"Found {len(btc_markets)} BTC markets")
    for m in btc_markets[:10]:
        print(f"{m.get('ticker')} | {m.get('title')} | {m.get('subtitle')}")

if __name__ == "__main__":
    asyncio.run(main())
