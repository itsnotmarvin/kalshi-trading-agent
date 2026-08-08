"""
Manifold Markets Adapter - PLAY MONEY (risk-free practice)

Manifold uses "mana" (play money), making it perfect for:
- Testing your bot logic with zero financial risk
- Validating Claude's probability estimates against outcomes
- Learning the agent architecture before committing real money

API docs: https://docs.manifold.markets/api
No authentication needed for reading, API key for betting.
Get your API key at: https://manifold.markets/profile → API tab
Age requirement: 13+ (play money only)
"""
import httpx
from datetime import datetime, timezone

from adapters.base import (
    PlatformAdapter, Market, Position, Order, TradeResult,
    Side, OrderStatus, Transfer
)

MANIFOLD_API = "https://api.manifold.markets/v0"


class ManifoldAdapter(PlatformAdapter):
    """
    Manifold Markets adapter for risk-free testing.

    SETUP:
    1. Create account at manifold.markets
    2. Go to Profile → API → copy your API key
    3. Set MANIFOLD_API_KEY in .env (optional, only needed for placing bets)

    You start with free mana. No real money involved.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def connect(self) -> bool:
        try:
            if getattr(self, "_connected", False):
                return True
                
            resp = await self.client.get(f"{MANIFOLD_API}/markets", params={"limit": 1})
            if resp.status_code == 200:
                print("✅ Connected to Manifold Markets (play money)")
                self._connected = True
                return True
            return False
        except Exception as e:
            print(f"❌ Manifold connection failed: {e}")
            return False

    async def get_markets(
        self,
        active_only: bool = True,
        min_volume: float = 0,
        category: str | None = None,
        limit: int = 50,
    ) -> list[Market]:
        """Fetch markets from Manifold."""
        params = {"limit": min(limit, 1000), "sort": "liquidity"}
        if category:
            params["topics"] = category

        resp = await self.client.get(f"{MANIFOLD_API}/markets", params=params)
        if resp.status_code != 200:
            return []

        markets = []
        for m in resp.json():
            if m.get("outcomeType") != "BINARY":
                continue
            if active_only and m.get("isResolved"):
                continue

            volume = m.get("volume", 0)
            if volume < min_volume:
                continue

            prob = m.get("probability", 0.5)
            end_date = None
            if m.get("closeTime"):
                end_date = datetime.fromtimestamp(m["closeTime"] / 1000, tz=timezone.utc)

            markets.append(Market(
                id=m["id"],
                question=m.get("question", ""),
                category=m.get("groupSlugs", ["unknown"])[0] if m.get("groupSlugs") else "unknown",
                yes_price=prob,
                no_price=1 - prob,
                volume_24h=volume * 0.1,  # Rough approximation
                total_volume=volume,
                liquidity=m.get("totalLiquidity", 0),
                end_date=end_date,
                resolution_source="Manifold community",
                is_active=not m.get("isResolved", False),
                platform="manifold",
                raw=m,
            ))

        markets.sort(key=lambda x: x.total_volume, reverse=True)
        return markets[:limit]

    async def get_market(self, market_id: str) -> Market | None:
        resp = await self.client.get(f"{MANIFOLD_API}/market/{market_id}")
        if resp.status_code != 200:
            return None
        m = resp.json()
        prob = m.get("probability", 0.5)
        return Market(
            id=m["id"],
            question=m.get("question", ""),
            category="unknown",
            yes_price=prob,
            no_price=1 - prob,
            volume_24h=m.get("volume", 0) * 0.1,
            total_volume=m.get("volume", 0),
            liquidity=m.get("totalLiquidity", 0),
            end_date=None,
            resolution_source="Manifold community",
            is_active=not m.get("isResolved", False),
            platform="manifold",
            raw=m,
        )

    async def get_orderbook(self, market_id: str) -> dict:
        # Manifold uses AMM, no traditional orderbook
        return {"bids": [], "asks": [], "type": "AMM"}

    async def place_order(
        self,
        market_id: str,
        side: Side,
        price: float,
        quantity: float,
        order_type: str = "limit",
        client_order_id: str | None = None,
        post_only: bool = False,
    ) -> TradeResult:
        """Place a bet on Manifold (uses mana, not real money)."""
        if not self.api_key:
            return TradeResult(False, None, "No API key — set MANIFOLD_API_KEY")

        data = {
            "contractId": market_id,
            "outcome": "YES" if side == Side.YES else "NO",
            "amount": int(quantity),  # Amount in mana
        }
        if order_type == "limit":
            data["limitProb"] = price

        headers = {"Authorization": f"Key {self.api_key}"}
        resp = await self.client.post(f"{MANIFOLD_API}/bet", json=data, headers=headers)

        if resp.status_code == 200:
            bet = resp.json()
            return TradeResult(
                success=True,
                order=Order(
                    id=bet.get("betId", ""),
                    market_id=market_id,
                    side=side,
                    price=price,
                    quantity=quantity,
                    status=OrderStatus.FILLED,
                    filled_quantity=quantity,
                    created_at=datetime.now(timezone.utc),
                    platform="manifold",
                ),
                error=None,
            )
        return TradeResult(False, None, f"Bet failed: {resp.text[:200]}")

    async def cancel_order(self, order_id: str) -> bool:
        if not self.api_key:
            return False
        headers = {"Authorization": f"Key {self.api_key}"}
        resp = await self.client.post(
            f"{MANIFOLD_API}/bet/cancel/{order_id}", headers=headers
        )
        return resp.status_code == 200

    async def get_positions(self) -> list[Position]:
        # Manifold doesn't have a simple positions endpoint
        return []

    async def get_balance(self) -> float:
        if not self.api_key:
            return 0.0
        headers = {"Authorization": f"Key {self.api_key}"}
        resp = await self.client.get(f"{MANIFOLD_API}/me", headers=headers)
        if resp.status_code == 200:
            return resp.json().get("balance", 0)
        return 0.0

    async def get_portfolio_value(self) -> float:
        return await self.get_balance()

    async def get_transfers(self) -> list[Transfer]:
        """Manifold transfers not yet implemented."""
        return []
