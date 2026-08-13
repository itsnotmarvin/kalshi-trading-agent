import asyncio
import os
import sys
from dotenv import load_dotenv
from collections import defaultdict

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
load_dotenv("/Users/marbin/kalshi/.env")

from adapters.kalshi_adapter import KalshiAdapter

async def analyze_fills_with_resolutions():
    adapter = KalshiAdapter()
    await adapter.connect()
    
    resp = await adapter._request("GET", "/portfolio/fills", params={"limit": 200})
    if not resp or "fills" not in resp:
        print("No fills found to analyze.")
        return
        
    fills = resp["fills"]
    market_trades = defaultdict(list)
    for f in fills:
        ticker = f.get("ticker")
        market_trades[ticker].append(f)
        
    print(f"Fetching current market status for {len(market_trades)} markets...")
    
    # Fetch market details in parallel
    tickers = list(market_trades.keys())
    market_details = {}
    
    # We do them in chunks to avoid hitting rate limits
    chunk_size = 10
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        results = await asyncio.gather(*(adapter.get_market(t) for t in chunk), return_exceptions=True)
        for ticker, market in zip(chunk, results):
            if isinstance(market, Exception) or market is None:
                market_details[ticker] = None
            else:
                market_details[ticker] = market
                
    # Group by category
    weather_pnl = 0.0
    sports_pnl = 0.0
    tech_pnl = 0.0
    other_pnl = 0.0
    
    all_summaries = []
    
    for ticker, t_list in market_trades.items():
        buys_yes = 0
        buys_no = 0
        sells_yes = 0
        sells_no = 0
        cost_basis = 0.0
        revenue = 0.0
        fees = 0.0
        
        sorted_trades = sorted(t_list, key=lambda x: x.get("created_time", ""))
        first_date = sorted_trades[0].get("created_time")[:10]
        
        for f in sorted_trades:
            action = f.get("action")
            side = f.get("side")
            qty = float(f.get("count_fp", 0.0))
            fee = float(f.get("fee_cost", 0.0))
            fees += fee
            
            yes_price = float(f.get("yes_price_dollars", 0.0))
            no_price = float(f.get("no_price_dollars", 0.0))
            price = yes_price if side == "yes" else no_price
            
            trade_value = qty * price
            
            if action == "buy":
                cost_basis += trade_value
                if side == "yes":
                    buys_yes += qty
                else:
                    buys_no += qty
            elif action == "sell":
                revenue += trade_value
                if side == "yes":
                    sells_yes += qty
                else:
                    sells_no += qty
                    
        net_yes = buys_yes - sells_yes
        net_no = buys_no - sells_no
        
        market = market_details.get(ticker)
        raw_market = market.raw if market else {}
        status = raw_market.get("status", "unknown").lower()
        result = raw_market.get("result", "").lower()
        
        realized_pnl = revenue - cost_basis - fees
        payout = 0.0
        
        if status in ("finalized", "resolved") or result in ("yes", "no"):
            # Market has resolved! Compute payoff
            status_str = f"RESOLVED {result.upper()}"
            if net_yes > 0:
                payout += net_yes * (1.0 if result == "yes" else 0.0)
            if net_no > 0:
                payout += net_no * (1.0 if result == "no" else 0.0)
                
            # If we shorted (negative net position, which is unusual in Kalshi unless closing)
            # but let's assume standard payouts
            realized_pnl += payout
        else:
            status_str = f"ACTIVE (Net Y:{net_yes:.0f} N:{net_no:.0f})"
            # Use current market mark-to-market as payout for open value
            if market:
                yes_price = market.yes_price
                no_price = market.no_price
                current_value = (net_yes * yes_price) + (net_no * no_price)
                realized_pnl += current_value
                status_str += f" | M2M Val: ${current_value:.2f}"
            else:
                status_str += " | No market data"
                
        # Classify category
        is_weather = any(prefix in ticker for prefix in ["KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXWIND"])
        is_sports = any(prefix in ticker for prefix in ["KXUFC", "KXEPL", "KXMVE", "KXSPORTS"])
        is_tech = any(prefix in ticker for prefix in ["KXTOPMODEL", "KXMUSKOAI", "KXLLM1"])
        
        cat = "other"
        if is_weather:
            cat = "weather"
            weather_pnl += realized_pnl
        elif is_sports:
            cat = "sports"
            sports_pnl += realized_pnl
        elif is_tech:
            cat = "tech"
            tech_pnl += realized_pnl
        else:
            other_pnl += realized_pnl
            
        all_summaries.append({
            "ticker": ticker,
            "category": cat,
            "first_date": first_date,
            "status": status_str,
            "pnl": realized_pnl,
            "cost": cost_basis,
            "rev": revenue,
            "fees": fees,
            "qty": buys_yes + buys_no
        })
        
    print("\n=======================================================")
    print("📈 DETAILED PERFORMANCE REPORT BY CATEGORY")
    print("=======================================================")
    
    for cat_name in ["weather", "tech", "sports", "other"]:
        cat_items = [item for item in all_summaries if item["category"] == cat_name]
        cat_pnl = sum(item["pnl"] for item in cat_items)
        print(f"\n📁 CATEGORY: {cat_name.upper()} (Total P&L: ${cat_pnl:+.2f})")
        print("-" * 100)
        for item in sorted(cat_items, key=lambda x: x["first_date"]):
            print(f"📅 {item['first_date']} | {item['ticker']:<40} | {item['status']:<35} | P&L: {item['pnl']:+7.2f} | Cost: ${item['cost']:.2f} | Fees: ${item['fees']:.2f}")

    total_all_pnl = weather_pnl + sports_pnl + tech_pnl + other_pnl
    print(f"\n=======================================================")
    print(f"🏆 OVERALL PORTFOLIO P&L SUMMARY: {total_all_pnl:+.2f}")
    print(f"   • Weather: {weather_pnl:+.2f}")
    print(f"   • Tech / AI: {tech_pnl:+.2f}")
    print(f"   • Sports / UFC: {sports_pnl:+.2f}")
    print(f"   • Other: {other_pnl:+.2f}")
    print("=======================================================")

if __name__ == "__main__":
    asyncio.run(analyze_fills_with_resolutions())
