import sys
from pathlib import Path

# Ensure core is importable
sys.path.append(str(Path(__file__).parent))

from core.paper_sizer import get_exploratory_size
from core.risk_manager import RiskManager, TradeProposal
from adapters.base import Position, Side

def test_paper_sizer():
    print("🧪 Running Test: Paper Sizer...")
    sizes = [get_exploratory_size(5.0, 10000.0) for _ in range(1000)]
    assert all(5.0 <= s <= 10000.0 for s in sizes), "Sizer generated size out of bounds"
    
    # Check that we span multiple scales
    has_micro = any(s < 50 for s in sizes)
    has_small = any(50 <= s < 250 for s in sizes)
    has_med = any(250 <= s < 1000 for s in sizes)
    has_large = any(1000 <= s < 5000 for s in sizes)
    has_mega = any(5000 <= s for s in sizes)
    
    assert has_micro and has_small and has_med and has_large and has_mega, "Sizer failed to distribute across size scales"
    print("✅ Paper Sizer tests passed successfully!")

def test_risk_manager_bypass():
    print("🧪 Running Test: Risk Manager Bypass...")
    rm = RiskManager()
    
    # Inject fake settings values for max limits to make sure we hit them normally
    from config.settings import settings
    original_max = settings.max_single_trade
    settings.max_single_trade = 100.0  # limit size to $100
    
    try:
        # Create a proposal that exceeds standard limit
        proposal = TradeProposal(
            market_id="test_market",
            market_question="Is this a test?",
            category="Weather",  # Use weather so it passes blocked category checks
            direction="BUY_YES",
            estimated_prob=0.8,
            market_price=0.2,   # 20 cents, outside of fee kill zone (35-65)
            edge=0.6,
            confidence="HIGH",
            position_size_usd=5000.0,  # Exceeds $100!
            reasoning="Testing sizing engine",
            r_score=1.5,
            execution_metrics={
                "side_cost": 0.2,
                "side_spread": 0.02,
                "side_depth_usd": 10000.0,
            },
        )
        
        # Test 1: Standard live check - should reject due to size
        approved, reasons = rm.check_trade(
            proposal=proposal,
            positions=[],
            balance=10000.0,
            portfolio_value=0.0,
            paper_mode=False
        )
        assert not approved, "Live trade should have been rejected for exceeding max single trade limit"
        assert any("exceeds max" in r for r in reasons), f"Expected max limit rejection reason, got: {reasons}"
        print("  - Live mode rejection verified.")
        
        # Test 2: Paper mode check - should bypass size limit!
        approved_paper, reasons_paper = rm.check_trade(
            proposal=proposal,
            positions=[],
            balance=10000.0,
            portfolio_value=0.0,
            paper_mode=True
        )
        assert approved_paper, f"Paper trade should be approved (bypassing max size limit), failed with: {reasons_paper}"
        print("  - Paper mode size-limit bypass verified.")
        
        # Test 3: Paper mode balance bypass - balance is small but trade is large
        approved_bal, reasons_bal = rm.check_trade(
            proposal=proposal,
            positions=[],
            balance=50.0,  # much smaller than $5000.0 proposal size!
            portfolio_value=0.0,
            paper_mode=True
        )
        assert approved_bal, f"Paper trade should be approved (bypassing balance limit), failed with: {reasons_bal}"
        print("  - Paper mode balance limit bypass verified.")
        
    finally:
        settings.max_single_trade = original_max

    print("✅ Risk Manager Bypass tests passed successfully!")

if __name__ == "__main__":
    try:
        test_paper_sizer()
        test_risk_manager_bypass()
        print("\n🎉 ALL SIZING ENGINE TESTS PASSED SUCCESSFULLY! 🎉")
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
