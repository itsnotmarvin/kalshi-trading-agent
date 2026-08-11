"""
Polymarket Platform Adapter

Uses py-clob-client to interact with Polymarket's orderbook and Gamma API for markets.
"""
import httpx
import json
from datetime import datetime

from adapters.base import (
    PlatformAdapter, Market, Position, Order, TradeResult,
    Side, OrderStatus, Transfer
)
from config.settings import settings

class PolymarketAdapter(PlatformAdapter):
    """
    Polymarket prediction market adapter.
    Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS in .env.
    """

    def __init__(self):
        self.private_key = settings.polymarket_private_key
        self.funder_address = settings.polymarket_funder
        self.client_clob = None
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.host = "https://clob.polymarket.com"
        self.chain_id = 137 # Polygon Mainnet
        
        self.http_client = httpx.AsyncClient(timeout=30.0)

    async def connect(self) -> bool:
        try:
            if getattr(self, "_connected", False):
                return True

            if settings.trading_mode == "live":
                print("❌ Polymarket live trading is disabled until order, balance, and position support are implemented.")
                return False

            resp = await self.http_client.get(f"{self.gamma_url}/events", params={"limit": 1})
            if resp.status_code == 200:
                print("✅ Connected to Polymarket (READ-ONLY PAPER)")
                print(f"   Paper balance: ${await self.get_balance():.2f}")
                self._connected = True
                return True
            return False
        except Exception as e:
            print(f"❌ Polymarket connection failed: {e}")
            return False

    async def get_markets(
        self,
        active_only: bool = True,
        min_volume: float = 0,
        category: str | None = None,
        limit: int = 50,
        target_category: str | None = None,
    ) -> list[Market]:
        """Fetch markets from the Gamma API."""
        markets = []
        try:
            params = {
                "active": "true" if active_only else "false",
                "closed": "false",
                "limit": limit * 2, # Fetch more to allow for filtering
            }
            if target_category:
                params["tag_slug"] = target_category.lower().replace(" ", "-")

            resp = await self.http_client.get(f"{self.gamma_url}/events", params=params)
            if resp.status_code == 200:
                events = resp.json()
                for event in events:
                    if not event.get("markets"):
                        continue
                    
                    # We usually trade the first/primary market in the event
                    for m_data in event["markets"]:
                        # Exclude weird markets
                        if m_data.get("closed", False):
                            continue
                            
                        volume = float(m_data.get("volume", 0))
                        if volume < min_volume:
                            continue

                        # Extract prices from tokens
                        tokens = m_data.get("tokens", [])
                        yes_price = 0.5
                        no_price = 0.5
                        
                        try:
                            # Usually yes is token_id "Yes" or index 0, no is index 1
                            if len(tokens) >= 2:
                                yes_price = float(tokens[0].get("price", 0.5))
                                no_price = float(tokens[1].get("price", 0.5))
                                # Only valid if summing to ~1 or valid numbers
                                if yes_price == 0 and "outcome" in tokens[0] and tokens[0]["outcome"].upper() == "YES":
                                    pass # Need to parse correctly
                        except:
                            pass

                        # If Gamma API doesn't have prices or it is weird, fallback
                        if "outcomePrices" in m_data:
                            try:
                                prices = json.loads(m_data["outcomePrices"])
                                if len(prices) >= 2:
                                    yes_price = float(prices[0])
                                    no_price = float(prices[1])
                            except:
                                pass
                        
                        end_date = None
                        if m_data.get("endDate"):
                            end_date = datetime.fromisoformat(m_data["endDate"].replace("Z", "+00:00"))

                        market = Market(
                            id=m_data.get("conditionId"),
                            question=m_data.get("question", event.get("title", "")),
                            category=event.get("tags", ["unknown"])[0].get("label", "unknown") if event.get("tags") else "unknown",
                            yes_price=yes_price,
                            no_price=no_price,
                            volume_24h=float(m_data.get("volume24hr", 0)),
                            total_volume=volume,
                            liquidity=float(m_data.get("liquidity", 0)),
                            end_date=end_date,
                            resolution_source=m_data.get("resolutionSource", "Polymarket"),
                            is_active=m_data.get("active", True),
                            platform="polymarket",
                            raw=m_data,
                        )
                        
                        # Set custom attribute token_id for order placing
                        market.yes_token_id = tokens[0].get("token_id") if len(tokens) > 0 else None
                        market.no_token_id = tokens[1].get("token_id") if len(tokens) > 1 else None

                        markets.append(market)
                        
                        if len(markets) >= limit:
                            break
                    if len(markets) >= limit:
                            break
        except Exception as e:
            print(f"⚠️ Polymarket get_markets error: {e}")

        # Sort by volume descending
        markets.sort(key=lambda m: m.total_volume, reverse=True)
        return markets[:limit]

    async def get_market(self, market_id: str) -> Market | None:
        """Fetch a single market by condition ID."""
        try:
            resp = await self.http_client.get(f"{self.gamma_url}/markets/{market_id}")
            if resp.status_code == 200:
                # Same parsing logic as get_markets
                m_data = resp.json()
                volume = float(m_data.get("volume", 0))
                yes_price = 0.5
                no_price = 0.5
                if "outcomePrices" in m_data:
                    import json
                    try:
                        prices = json.loads(m_data["outcomePrices"])
                        if len(prices) >= 2:
                            yes_price = float(prices[0])
                            no_price = float(prices[1])
                    except:
                        pass
                
                end_date = None
                if m_data.get("endDate"):
                    end_date = datetime.fromisoformat(m_data["endDate"].replace("Z", "+00:00"))

                return Market(
                    id=m_data.get("conditionId"),
                    question=m_data.get("question", ""),
                    category="unknown",
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_24h=float(m_data.get("volume24hr", 0)),
                    total_volume=volume,
                    liquidity=float(m_data.get("liquidity", 0)),
                    end_date=end_date,
                    resolution_source=m_data.get("resolutionSource", "Polymarket"),
                    is_active=m_data.get("active", True),
                    platform="polymarket",
                    raw=m_data,
                )
        except:
            pass
        return None

    async def get_orderbook(self, market_id: str) -> dict:
        """Fetch orderbook using py_clob_client"""
        if not self.client_clob: return {"bids": [], "asks": []}
        try:
            # We need the token_id to fetch the orderbook. We'll assume the market_id here is actually the token ID or condition ID.
            # In Polymarket CLOB, orderbooks are per-token.
            # This is a stub for now.
            return {"bids": [], "asks": []}
        except Exception:
            return {"bids": [], "asks": []}

    async def place_order(
        self,
        market_id: str,
        side: Side,
        price: float,
        quantity: float,
        action: str = "buy",
        order_type: str = "limit",
        client_order_id: str | None = None,
        post_only: bool = False,
    ) -> TradeResult:
        return TradeResult(
            success=False,
            order=None,
            error="Polymarket live order placement is not implemented. Use paper mode only.",
        )

    async def cancel_order(self, order_id: str) -> bool:
        return False

    async def get_positions(self) -> list[Position]:
        return []

    async def get_balance(self) -> float:
        if settings.trading_mode == "paper":
            return settings.max_portfolio_value
        return 0.0

    async def get_portfolio_value(self) -> float:
        return await self.get_balance()

    async def get_transfers(self) -> list[Transfer]:
        """Polymarket transfers (deposits/withdrawals) not yet implemented."""
        return []
