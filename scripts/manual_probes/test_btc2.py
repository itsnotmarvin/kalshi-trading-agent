import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Check BTC series
    resp = await ada._request("GET", "/events", {"series_ticker": "BTC", "status": "open", "with_nested_markets": True})
    print(f"BTC events: {len(resp.get('events', []))}")
    for event in resp.get('events', []):
        print(f" - {event.get('title')}")

if __name__ == "__main__":
    asyncio.run(main())
