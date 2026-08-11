"""
Abstract base class for prediction market platform adapters.
Every platform (Kalshi, Polymarket, Manifold) implements this interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(Enum):
    YES = "yes"
    NO = "no"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Market:
    """Normalized market representation across platforms."""
    id: str
    question: str
    category: str
    yes_price: float          # 0.0 to 1.0
    no_price: float           # 0.0 to 1.0
    volume_24h: float         # in USD
    total_volume: float       # lifetime volume in USD
    liquidity: float          # available liquidity in USD
    end_date: datetime | None
    resolution_source: str
    is_active: bool
    platform: str
    raw: dict                 # Original platform response

    @property
    def spread(self) -> float:
        """Bid-ask spread as fraction."""
        return abs(1.0 - self.yes_price - self.no_price)

    @property
    def midpoint(self) -> float:
        # yes_price/no_price are executable asks. The YES bid is the
        # complement of the NO ask, so midpoint must not alias the YES ask.
        yes_bid = 1.0 - self.no_price
        return (yes_bid + self.yes_price) / 2.0


@dataclass
class Position:
    """A position held in a market."""
    market_id: str
    market_question: str
    side: Side
    quantity: float           # Number of contracts/shares
    avg_price: float          # Average entry price
    current_price: float      # Current market price for this side
    unrealized_pnl: float     # Current P&L
    platform: str
    category: str = ""
    end_date: datetime | None = None
    entry_prob: float = 0.0   # Claude's YES-probability estimate at entry (for calibration)


@dataclass
class Order:
    """An order placed on a platform."""
    id: str
    market_id: str
    side: Side
    price: float
    quantity: float
    status: OrderStatus
    filled_quantity: float
    created_at: datetime
    platform: str


@dataclass
class TradeResult:
    """Result of attempting to place a trade."""
    success: bool
    order: Order | None
    error: str | None


@dataclass
class Transfer:
    """A deposit or withdrawal."""
    id: str
    type: str  # 'deposit' or 'withdrawal'
    amount: float
    status: str
    created_at: datetime
    platform: str


class PlatformAdapter(ABC):
    """
    Abstract interface for prediction market platforms.
    Implement this for each platform you want to trade on.
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Initialize connection and authenticate. Returns True on success."""
        ...

    @abstractmethod
    async def get_markets(
        self,
        active_only: bool = True,
        min_volume: float = 0,
        category: str | None = None,
        limit: int = 50,
    ) -> list[Market]:
        """Fetch available markets with optional filters."""
        ...

    @abstractmethod
    async def get_market(self, market_id: str) -> Market | None:
        """Fetch a single market by ID."""
        ...

    @abstractmethod
    async def get_orderbook(self, market_id: str) -> dict:
        """Fetch the order book for a market. Returns {bids: [...], asks: [...]}."""
        ...

    @abstractmethod
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
        """Place an order. Returns TradeResult with success/failure info."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        ...

    @abstractmethod
    async def get_balance(self) -> float:
        """Get available balance in USD."""
        ...

    @abstractmethod
    async def get_portfolio_value(self) -> float:
        """Get total portfolio value (balance + positions)."""
        ...

    @abstractmethod
    async def get_transfers(self) -> list[Transfer]:
        """Get history of deposits and withdrawals."""
        ...
