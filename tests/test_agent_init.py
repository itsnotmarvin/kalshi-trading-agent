import sys
import os
sys.path.append(os.getcwd())
from core.agent import TradingAgent
from core.risk_manager import RiskManager

def test_init():
    print("Initializing RiskManager...")
    rm = RiskManager()
    print("Initializing TradingAgent...")
    # This calls WeatherEngine -> ForecasterInsight
    agent = TradingAgent(rm)
    print("Success!")

if __name__ == "__main__":
    test_init()
