import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    adapter = KalshiAdapter()
    await adapter.connect()
    resp = await adapter._request("GET", "/markets", {"limit": 200, "status": "open"})
    markets = resp.get("markets", [])
    print(f"Total returned: {len(markets)}")
    
    # Sort by volume_24h_fp or volume
    markets.sort(key=lambda m: m.get("volume_24h_fp", 0), reverse=True)
    for m in markets[:5]:
        print(m.get("ticker"), "| vol24h_fp:", m.get("volume_24h_fp"), "| vol_fp:", m.get("volume_fp"), "| liquidity:", m.get("liquidity_dollars"))
        
if __name__ == "__main__":
    asyncio.run(main())
