import asyncio
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    positions = await ada.get_positions()
    print(f"Total positions: {len(positions)}")
    for p in positions:
        print(f" - {p.market_id}: {p.side.value} {p.quantity} shares @ {p.avg_price:.2f} | PnL: ${p.unrealized_pnl:.2f}")

if __name__ == "__main__":
    asyncio.run(main())
