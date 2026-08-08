import asyncio
from adapters.polymarket_adapter import PolymarketAdapter
from config.settings import settings

async def main():
    print(f"Testing Polymarket Integration...")
    
    # Temporarily override settings if needed
    settings.platform = "polymarket"
    
    adapter = PolymarketAdapter()
    
    print("\n1. Connecting to Polymarket...")
    connected = await adapter.connect()
    
    if not connected:
        print("❌ Failed to connect. Did you set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS in .env?")
        return

    print("\n2. Fetching active markets...")
    markets = await adapter.get_markets(limit=5)
    
    if not markets:
        print("⚠️ No markets found or request failed.")
    else:
        for i, m in enumerate(markets):
            print(f"[{i+1}] {m.question}")
            print(f"    Yes: {m.yes_price:.2f} | No: {m.no_price:.2f} | Vol: ${m.total_volume:.2f}")

    print("\n✅ Test complete.")

if __name__ == "__main__":
    asyncio.run(main())
