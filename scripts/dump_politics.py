import asyncio
import json
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    # Find a Politics market
    res = await ada._request('GET', '/markets', {'limit': 50})
    if res and 'markets' in res:
        for m in res['markets']:
            if 'PRES' in m['ticker'] or 'CAB' in m['ticker']:
                print(json.dumps(m, indent=2))
                break
    else:
        print("No markets found or request failed.")

if __name__ == "__main__":
    asyncio.run(main())
