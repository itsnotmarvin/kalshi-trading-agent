import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    b = await ada.get_balance()
    p = await ada.get_portfolio_value()
    pos = await ada.get_positions()
    print(f"Balance: ${b}")
    print(f"Portfolio Value: ${p}")
    print(f"Positions: {len(pos)}")
    for x in pos:
        print(f"{x.market_id}: {x.side.value} {x.quantity} shares | UnPnl: ${x.unrealized_pnl}")

if __name__ == "__main__":
    asyncio.run(main())
