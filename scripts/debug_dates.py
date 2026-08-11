import asyncio
from datetime import datetime, timezone
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Force 5 pages to see more types of markets
    markets = []
    cursor = None
    for i in range(5):
        params = {"limit": 100, "status": "open"}
        if cursor: params["cursor"] = cursor
        resp = await ada._request("GET", "/events", params)
        if not resp or "events" not in resp: break
        
        for event in resp["events"]:
            markets.extend(ada._parse_market(event))
        cursor = resp.get("cursor")
        if not cursor: break

    print(f"Total parsed: {len(markets)}")
    now = datetime.now(timezone.utc)
    
    for m in markets[:50]:
        if m.end_date:
            diff = m.end_date - now
            print(f"[{m.category}] {m.id} -> End: {m.end_date} (Diff: {diff})")
        else:
            print(f"[{m.category}] {m.id} -> NO END DATE")

if __name__ == "__main__":
    asyncio.run(main())
