import asyncio
from datetime import datetime, timezone, timedelta
from adapters.kalshi_adapter import KalshiAdapter

async def main():
    ada = KalshiAdapter()
    await ada.connect()
    
    # Fetch as many markets as possible (up to 500)
    markets = await ada.get_markets(limit=500, min_volume=0)
    
    now = datetime.now(timezone.utc)
    one_week = now + timedelta(days=7)
    
    groups = {
        "Short Term (< 48h)": [],
        "Crypto": [],
        "Politics": []
    }
    
    for m in markets:
        # Check if short term
        if m.end_date and m.end_date < now + timedelta(days=2):
            groups["Short Term (< 48h)"].append(m)
            
        # Check if crypto
        if "btc" in m.id.lower() or "bitcoin" in m.question.lower() or "crypto" in m.category.lower():
            groups["Crypto"].append(m)
            
        # Check if politics
        if m.category.lower() in ["politics", "elections"]:
            groups["Politics"].append(m)
            
    for name, list_ in groups.items():
        print(f"\n--- {name} (Top 10) ---")
        for m in sorted(list_, key=lambda x: x.volume_24h, reverse=True)[:10]:
            end_str = m.end_date.strftime("%Y-%m-%d") if m.end_date else "None"
            print(f"[{m.id}] (Ends {end_str}) {m.question} - Vol: ${m.volume_24h}")

if __name__ == "__main__":
    asyncio.run(main())
