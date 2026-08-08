import asyncio
import json
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    resp = await ada._request("GET", "/portfolio/positions")
    print(json.dumps(resp, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
