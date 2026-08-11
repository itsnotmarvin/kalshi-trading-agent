import asyncio
from unittest.mock import MagicMock, AsyncMock
from core.risk_manager import RiskManager, TradeProposal
from adapters.base import Position, Market, Side
from config.settings import settings

async def test_filtering():
    print("🧪 Testing Duplicate Trade Filter...")
    
    # 1. Setup RiskManager and mock positions
    rm = RiskManager()
    
    # Create some mock positions
    pos1 = Position(market_id="MKT-1", market_question="Q1", side=Side.YES, quantity=10, avg_price=0.5, current_price=0.5, unrealized_pnl=0, platform="kalshi", category="Test")
    pos2 = Position(market_id="MKT-2", market_question="Q2", side=Side.NO, quantity=5, avg_price=0.3, current_price=0.3, unrealized_pnl=0, platform="kalshi", category="Test")
    
    positions = [pos1, pos2]
    
    # Create some mock markets (some in positions, some not)
    m1 = Market(id="MKT-1", question="Q1", category="Test", yes_price=0.5, no_price=0.5, volume_24h=100, total_volume=1000, liquidity=500, end_date=None, resolution_source="", is_active=True, platform="kalshi", raw={})
    m2 = Market(id="MKT-2", question="Q2", category="Test", yes_price=0.3, no_price=0.7, volume_24h=200, total_volume=2000, liquidity=600, end_date=None, resolution_source="", is_active=True, platform="kalshi", raw={})
    m3 = Market(id="MKT-3", question="Q3", category="Test", yes_price=0.8, no_price=0.2, volume_24h=300, total_volume=3000, liquidity=700, end_date=None, resolution_source="", is_active=True, platform="kalshi", raw={})
    
    markets = [m1, m2, m3]
    
    # 2. Run the filtering logic (extracted from main.py)
    print(f"   Initial markets: {[m.id for m in markets]}")
    print(f"   Current positions: {[p.market_id for p in positions]}")
    
    initial_count = len(markets)
    filtered_markets = [m for m in markets if not any(p.market_id == m.id for p in positions)]
    
    print(f"   Filtered markets: {[m.id for m in filtered_markets]}")
    
    # 3. Assertions
    assert len(filtered_markets) == 1
    assert filtered_markets[0].id == "MKT-3"
    print("✅ Logic test passed: Only MKT-3 remains.")

    # 4. Test paper position recording
    print("\n🧪 Testing Paper Position Recording...")
    proposal = TradeProposal(
        market_id="MKT-PAPER-1",
        market_question="Paper Q1",
        category="Test",
        direction="BUY_YES",
        estimated_prob=0.9,
        market_price=0.5,
        edge=0.4,
        confidence="HIGH",
        position_size_usd=10.0,
        reasoning="Testing paper positions"
    )
    
    # Ensure file is clean
    if rm.paper_positions_file.exists():
        rm.paper_positions_file.unlink()
        
    rm.record_paper_trade(proposal)
    
    # Load back
    saved_positions = rm.load_paper_positions()
    assert len(saved_positions) == 1
    assert saved_positions[0].market_id == "MKT-PAPER-1"
    assert saved_positions[0].avg_price == 0.5
    print("✅ Paper trade recording passed.")
    
    # Cleanup
    if rm.paper_positions_file.exists():
        rm.paper_positions_file.unlink()

if __name__ == "__main__":
    asyncio.run(test_filtering())
