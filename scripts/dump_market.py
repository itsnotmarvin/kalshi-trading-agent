import asyncio
import json
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    res = await ada._request('GET', '/markets', {'limit': 1})
    if res and 'markets' in res:
        print(json.dumps(res['markets'][0], indent=2))
    else:
        print("No markets found or request failed.")

if __name__ == "__main__":
    asyncio.run(main())
