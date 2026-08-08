"""
Kalshi Platform Adapter

Kalshi is CFTC-regulated, accepts USD deposits via ACH/wire/Apple Pay,
available in 42+ US states, and requires only 18+ age verification.

API docs: https://docs.kalshi.com
Python SDK: pip install kalshi-python
"""
import httpx
import asyncio
import json
import time
import hashlib
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from adapters.base import (
    PlatformAdapter, Market, Position, Order, TradeResult,
    Side, OrderStatus, Transfer
)
from config.settings import settings


# Kalshi API base URLs
KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAdapter(PlatformAdapter):
    """
    Kalshi prediction market adapter.

    SETUP INSTRUCTIONS:
    1. Create account at kalshi.com (18+, US address required)
    2. Go to Settings → API → Create new API key
    3. Download your private key (.pem file)
    4. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env

    Paper/shadow mode still uses production Kalshi market data; it only skips
    live order placement.
    """

    def __init__(self):
        self.base_url = KALSHI_PROD_URL
        self.api_key_id = settings.kalshi_api_key_id
        self.private_key_path = self._resolve_private_key_path(settings.kalshi_private_key_path)
        self.private_key: str | None = None
        self.client = httpx.AsyncClient(timeout=30.0)
        self._member_id = None

    def _resolve_private_key_path(self, configured_path: str) -> str:
        """
        Resolve the configured private key path with a local dev fallback.

        The fallback file is gitignored and only used when the configured path is
        missing, which keeps server startup working after local folder renames.
        """
        if configured_path and Path(configured_path).expanduser().exists():
            return configured_path

        local_key_path = Path(__file__).resolve().parents[1] / "yay.txt"
        if local_key_path.exists():
            return str(local_key_path)

        return configured_path

    async def connect(self) -> bool:
        """Load private key and verify connection."""
        if getattr(self, "_connected", False):
            return True

        try:
            if self.private_key_path and Path(self.private_key_path).expanduser().exists():
                self.private_key = Path(self.private_key_path).expanduser().read_text()

            # Test connection by fetching exchange status
            resp = await self._request("GET", "/exchange/status")
            if resp and isinstance(resp, dict) and resp.get("exchange_active"):
                print("✅ Connected to Kalshi (PRODUCTION MARKET DATA)")
                # Get member ID for authenticated requests
                if self.api_key_id:
                    me = await self._request("GET", "/portfolio/balance")
                    if me and isinstance(me, dict):
                        print(f"   Balance: ${me.get('balance', 0) / 100:.2f}")
                self._connected = True
                return True
            return False
        except Exception as e:
            print(f"❌ Kalshi connection failed: {e}")
            return False

    def _sign_request(self, method: str, path: str, timestamp_ms: int) -> str:
        """
        Sign a request using the private key (supports RSA and Ed25519).
        Kalshi uses the message format: timestamp + method + path
        """
        if not self.private_key:
            print("⚠️  No private key loaded — cannot sign requests")
            return ""
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa, padding, utils
            from cryptography.hazmat.primitives import hashes
            
            pk = self.private_key
            if not pk:
                return ""
            
            key = load_pem_private_key(pk.encode(), password=None)
            message = f"{timestamp_ms}{method}{path}".encode()

            if isinstance(key, rsa.RSAPrivateKey):
                signature = key.sign(
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
            elif isinstance(key, ec.EllipticCurvePrivateKey):
                signature = key.sign(
                    message,
                    ec.ECDSA(hashes.SHA256()),
                )
            elif isinstance(key, ed25519.Ed25519PrivateKey):
                signature = key.sign(message)
            else:
                print(f"⚠️  Unsupported key type: {type(key).__name__}")
                return ""

            return base64.b64encode(signature).decode()
        except Exception as e:
            print(f"⚠️  Signing failed: {e}")
            return ""

    def _auth_headers(self, method: str, url: str) -> dict:
        """Generate authentication headers for Kalshi API."""
        timestamp_ms = int(time.time() * 1000)
        from urllib.parse import urlparse
        path = urlparse(url).path
        signature = self._sign_request(method, path, timestamp_ms)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, params: dict | None = None, data: dict | None = None, silent: bool = False) -> Any:
        """Make an authenticated request to Kalshi API."""
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(method, url) if self.api_key_id else {}

        try:
            if method == "GET":
                resp = await self.client.get(url, headers=headers, params=params)
            elif method == "POST":
                resp = await self.client.post(url, headers=headers, json=data)
            elif method == "DELETE":
                resp = await self.client.delete(url, headers=headers)
            else:
                return None

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                if not silent: print(f"⚠️  Rate limited, waiting {retry_after}s...")
                await asyncio.sleep(retry_after)
                return await self._request(method, path, params, data, silent=silent)

            if resp.status_code >= 400:
                error_detail = resp.text[:500]
                try:
                    error_json = resp.json()
                    if "error" in error_json:
                        error_detail = error_json["error"]
                    elif "message" in error_json:
                        error_detail = error_json["message"]
                except:
                    pass
                if not silent: print(f"⚠️  Kalshi API error {resp.status_code}: {error_detail}")
                return {"error": error_detail, "status_code": resp.status_code}

            return resp.json()
        except Exception as e:
            if not silent: print(f"⚠️  Kalshi request failed: {e}")
            return None

    async def get_markets(
        self,
        active_only: bool = True,
        min_volume: float = 0,
        category: str | None = None,
        limit: int = 50,
        target_category: str | None = None,
        series_ticker: str | None = None,
        mve_filter: str | None = None,
    ) -> list[Market]:
        """Fetch active markets from Kalshi."""
        markets: list[Market] = []
        cursor: str | None = None
        max_pages = 20 
        cat_filter = target_category or category
        
        for _ in range(max_pages):
            params = {"limit": min(limit, 100)}
            if active_only:
                params["status"] = "open"
            if cat_filter:
                params["category"] = cat_filter
            if series_ticker:
                params["series_ticker"] = series_ticker
            if mve_filter:
                params["mve_filter"] = mve_filter
            if cursor:
                params["cursor"] = cursor

            resp = await self._request("GET", "/markets", params=params)
            if not resp or not isinstance(resp, dict) or "markets" not in resp:
                break

            for market_data in resp["markets"]:
                try:
                    if market_data.get("is_provisional", False):
                        continue
                    
                    # Double check status if active_only
                    status = market_data.get("status", "").lower()
                    if active_only and status not in ["active", "open"]:
                        continue

                    yes_ask = float(market_data.get("yes_ask_dollars", 0))
                    if not (0.01 < yes_ask < 0.99):
                        continue
                        
                    if cat_filter and not market_data.get("category"):
                        market_data["category"] = cat_filter

                    market = self._parse_market(market_data)
                    if market:
                        # Use total_volume — volume_24h is almost always 0 on Kalshi
                        if market.total_volume >= min_volume:
                            if target_category:
                                if market.category.lower() == target_category.lower():
                                    markets.append(market)
                            else:
                                markets.append(market)
                except Exception:
                    continue
            
            cursor = resp.get("cursor")
            if not cursor or len(markets) >= limit:
                break

        markets.sort(key=lambda m: m.total_volume, reverse=True)
        return markets[:limit]

    async def get_market_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        period_interval_minutes: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch real Kalshi candlestick data for a market."""
        params: dict[str, Any] = {"period_interval": period_interval_minutes}
        if start_ts is not None:
            params["start_ts"] = start_ts
        if end_ts is not None:
            params["end_ts"] = end_ts
        resp = await self._request(
            "GET",
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params=params,
            silent=True,
        )
        if not isinstance(resp, dict):
            return []
        return resp.get("candlesticks", []) if isinstance(resp.get("candlesticks"), list) else []

    async def get_world_cup_candidate_markets(self, limit: int = 1000) -> list[Market]:
        """Fetch open World Cup singles plus Kalshi combo candidates."""
        markets: list[Market] = []
        seen: set[str] = set()

        async def add_batch(batch: list[Market]) -> None:
            for market in batch:
                if market.id in seen:
                    continue
                seen.add(market.id)
                markets.append(market)

        # Kalshi's broad Sports response currently emphasizes MVE combo markets.
        # Read known World Cup single-market series directly so exact score,
        # totals, BTTS, goals, assists, and score/assist singles are not missed.
        world_cup_single_series = (
            "KXWCSCORE",
            "KXWCTOTAL",
            "KXWCBTTS",
            "KXWCGOAL",
            "KXWCAST",
            "KXWCSOA",
        )
        per_series_limit = max(100, min(limit, 500))
        for series_ticker in world_cup_single_series:
            await add_batch(
                await self.get_markets(
                    active_only=True,
                    series_ticker=series_ticker,
                    mve_filter="exclude",
                    limit=per_series_limit,
                )
            )

        # Keep combo candidates too, but the World Cup service filters out mixed
        # sports and non-current-matchday legs before showing cards.
        await add_batch(
            await self.get_markets(
                active_only=True,
                category="Sports",
                mve_filter="include",
                limit=min(limit, 300),
            )
        )

        markets.sort(key=lambda m: m.total_volume, reverse=True)
        return markets[:limit]

    async def get_weather_markets(self, limit: int = 200) -> list[Market]:
        """Fetch weather markets from Kalshi with throttled concurrency."""
        WEATHER_PREFIXES = ["KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXWIND"]
        CITY_CODES = [
            "NY", "LA", "CHI", "MIA", "HOU", "PHX", "PHI", "DAL", "DEN",
            "SEA", "BOS", "ATL", "SF", "DC", "DET", "MIN", "TPA", "POR",
            "AUS", "LV", "NO", "SLC", "SAC", "OKC",
        ]

        # Cap at 5 concurrent requests to avoid triggering Kalshi's rate limiter
        sem = asyncio.Semaphore(5)

        async def fetch_series(series: str) -> list[Market]:
            async with sem:
                await asyncio.sleep(0.1)  # small jitter between slots
                params = {"limit": 100, "series_ticker": series, "status": "open"}
                resp = await self._request("GET", "/markets", params=params, silent=True)
                if not resp or not isinstance(resp, dict) or "markets" not in resp:
                    return []
                results: list[Market] = []
                for market_data in resp["markets"]:
                    try:
                        if market_data.get("is_provisional", False):
                            continue
                        if market_data.get("status", "").lower() not in ["active", "open"]:
                            continue
                        yes_ask = float(market_data.get("yes_ask_dollars", 0))
                        if not (0.01 < yes_ask < 0.99):
                            continue
                        market = self._parse_market(market_data)
                        if market:
                            market.category = "Climate and Weather"
                            results.append(market)
                    except Exception:
                        continue
                return results

        all_series = [f"{prefix}{code}" for prefix in WEATHER_PREFIXES for code in CITY_CODES]
        results_nested = await asyncio.gather(*(fetch_series(s) for s in all_series), return_exceptions=True)

        markets: list[Market] = []
        for result in results_nested:
            if isinstance(result, list):
                markets.extend(result)
            if len(markets) >= limit:
                break

        return markets[:limit]

    def _parse_market(self, market_data: dict[str, Any]) -> Market | None:
        """Convert Kalshi API response to normalized Market object."""
        try:
            yes_price = float(market_data.get("yes_ask_dollars", 0.5))
            no_price = float(market_data.get("no_ask_dollars", 0.5))

            expiration = market_data.get("expected_expiration_time")
            close_time = market_data.get("close_time")
            
            dates: list[datetime] = []
            if expiration:
                dates.append(datetime.fromisoformat(expiration.replace("Z", "+00:00")))
            if close_time:
                dates.append(datetime.fromisoformat(close_time.replace("Z", "+00:00")))
            
            end_date = min(dates) if dates else None
            volume_24h = float(market_data.get("volume_24h_fp", 0))
            total_vol = float(market_data.get("volume_fp", 0))
            liquidity = float(market_data.get("liquidity_dollars", 0))
            settlement_source = market_data.get("settlement_source_url", "Kalshi")

            return Market(
                id=market_data.get("ticker", ""),
                question=market_data.get("title", ""),
                category=market_data.get("category", "unknown"),
                yes_price=yes_price,
                no_price=no_price,
                volume_24h=volume_24h,
                total_volume=total_vol,
                liquidity=liquidity,
                end_date=end_date,
                resolution_source=settlement_source,
                is_active=market_data.get("status") in ["active", "open"],
                platform="kalshi",
                raw=market_data,
            )
        except Exception:
            return None

    async def get_market(self, market_id: str) -> Market | None:
        """Fetch a single market by ticker."""
        # silent=True: expired/settled markets legitimately return 404
        resp = await self._request("GET", f"/markets/{market_id}", silent=True)
        if not resp or not isinstance(resp, dict) or "market" not in resp:
            return None
        return self._parse_market(resp["market"])

    async def get_orderbook(self, market_id: str) -> dict:
        """Fetch order book for a Kalshi market."""
        resp = await self._request("GET", f"/markets/{market_id}/orderbook")
        if not isinstance(resp, dict):
            return {"yes": [], "no": [], "bids": [], "asks": []}

        ob = resp.get("orderbook_fp") or resp.get("orderbook") or {}
        yes_levels = ob.get("yes_dollars") or ob.get("yes") or []
        no_levels = ob.get("no_dollars") or ob.get("no") or []
        return {
            "yes": yes_levels,
            "no": no_levels,
            "bids": yes_levels,
            "asks": no_levels,
        }

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
        """Place an order on Kalshi."""
        price_cents = max(1, min(99, round(price * 100)))
        count = max(1, int(quantity))

        order_data = {
            "ticker": market_id,
            "action": action,
            "side": side.value,
            "type": order_type,
            "count": count,
            "post_only": post_only,
        }
        if client_order_id:
            order_data["client_order_id"] = client_order_id
        if order_type == "limit":
            order_data["yes_price" if side == Side.YES else "no_price"] = price_cents

        resp = await self._request("POST", "/portfolio/orders", data=order_data)

        if not resp or not isinstance(resp, dict) or "order" not in resp:
            if (
                isinstance(resp, dict)
                and resp.get("status_code") == 409
                and client_order_id
            ):
                existing_order = await self.find_order_by_client_id(market_id, client_order_id)
                if existing_order:
                    return TradeResult(success=True, order=existing_order, error=None)

            error_msg = resp.get("error", "Unknown rejection") if isinstance(resp, dict) else "No response"
            status_code = resp.get("status_code") if isinstance(resp, dict) else None
            status_suffix = f" (status {status_code})" if status_code else ""
            return TradeResult(success=False, order=None, error=f"Order rejected{status_suffix}: {error_msg}")

        order_resp = resp["order"]
        order = self._parse_order(order_resp, market_id, side, price, count)
        if order is None:
            return TradeResult(success=False, order=None, error="Order rejected: malformed order response")
        return TradeResult(
            success=True,
            order=order,
            error=None,
        )

    async def find_order_by_client_id(
        self,
        market_id: str,
        client_order_id: str,
    ) -> Order | None:
        """Find an existing Kalshi order by client_order_id for idempotent retries."""
        if not client_order_id:
            return None

        resp = await self._request(
            "GET",
            "/portfolio/orders",
            params={"ticker": market_id, "limit": 1000},
            silent=True,
        )
        orders = resp.get("orders", []) if isinstance(resp, dict) else []
        for order_resp in orders:
            if order_resp.get("client_order_id") != client_order_id:
                continue
            side_value = order_resp.get("side")
            try:
                side = Side(side_value) if side_value else Side.YES
            except ValueError:
                side = Side.YES
            price = self._order_price(order_resp, side)
            count = self._order_count(order_resp)
            return self._parse_order(order_resp, market_id, side, price, count)
        return None

    def _parse_order(
        self,
        order_resp: dict,
        market_id: str,
        side: Side,
        price: float,
        quantity: float,
    ) -> Order | None:
        if not isinstance(order_resp, dict):
            return None

        status_raw = str(order_resp.get("status", "pending")).lower()
        status_map = {
            "resting": OrderStatus.PENDING,
            "pending": OrderStatus.PENDING,
            "executed": OrderStatus.FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        filled = (
            order_resp.get("filled_count")
            or order_resp.get("fill_count")
            or order_resp.get("fill_count_fp")
            or 0
        )
        try:
            filled_quantity = float(filled)
        except (TypeError, ValueError):
            filled_quantity = 0.0

        return Order(
            id=order_resp.get("order_id", ""),
            market_id=order_resp.get("ticker", market_id),
            side=side,
            price=price,
            quantity=quantity,
            status=status_map.get(status_raw, OrderStatus.PENDING),
            filled_quantity=filled_quantity,
            created_at=datetime.now(timezone.utc),
            platform="kalshi",
        )

    def _order_price(self, order_resp: dict, side: Side) -> float:
        keys = (
            ("yes_price_dollars", "yes_price")
            if side == Side.YES
            else ("no_price_dollars", "no_price")
        )
        for key in keys:
            value = order_resp.get(key)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            return price / 100.0 if price > 1 else price
        return 0.0

    def _order_count(self, order_resp: dict) -> float:
        for key in ("initial_count", "initial_count_fp", "count", "count_fp"):
            value = order_resp.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    async def cancel_order(self, order_id: str) -> bool:
        resp = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return resp is not None

    async def get_positions(self) -> list[Position]:
        """Get all open positions."""
        resp = await self._request("GET", "/portfolio/positions")
        portfolio_data = resp.get("market_positions", []) if isinstance(resp, dict) else []
        positions: list[Position] = []
        for pos in portfolio_data:
            pos_qty = float(pos.get("position", pos.get("position_fp", 0)))
            qty = abs(pos_qty)
            
            if qty >= 1:
                avg_price = 0.5
                if "average_price" in pos and pos["average_price"]:
                    avg_price = float(pos["average_price"]) / 100
                else:
                    traded = float(pos.get("total_traded_dollars", pos.get("total_traded", 0)))
                    if traded > 0:
                        avg_price = traded / qty
                
                market_price = None
                if "market_price" in pos and pos["market_price"]:
                    market_price = float(pos["market_price"]) / 100

                # Calculate P&L ourselves — Kalshi's unrealized_pnl field
                # often returns 0, which broke our entire learning system.
                kalshi_pnl = float(pos.get("unrealized_pnl", 0)) / 100
                cur_price = market_price if market_price is not None else avg_price
                if kalshi_pnl == 0 and market_price is not None and avg_price > 0:
                    if pos_qty > 0:  # YES side
                        calculated_pnl = (cur_price - avg_price) * qty
                    else:  # NO side
                        calculated_pnl = ((1 - cur_price) - (1 - avg_price)) * qty
                    final_pnl = round(calculated_pnl, 4)
                else:
                    final_pnl = kalshi_pnl

                positions.append(Position(
                    market_id=pos.get("ticker", ""),
                    market_question=pos.get("market_title", pos.get("ticker", "")),
                    side=Side.YES if pos_qty > 0 else Side.NO,
                    quantity=qty,
                    avg_price=avg_price,
                    current_price=cur_price,
                    unrealized_pnl=final_pnl,
                    platform="kalshi",
                    category=pos.get("category", pos.get("market_category", "")),
                ))

        if positions:
            looked_up = await asyncio.gather(
                *(self.get_market(p.market_id) for p in positions),
                return_exceptions=True,
            )
            for position, market in zip(positions, looked_up):
                if isinstance(market, Market):
                    if market.category and not position.category:
                        position.category = market.category
                    position.end_date = market.end_date

        return positions

    async def get_balance(self) -> float:
        """Get available balance in USD."""
        resp = await self._request("GET", "/portfolio/balance")
        if not resp or not isinstance(resp, dict):
            return 0.0
        return resp.get("balance", 0) / 100

    async def get_portfolio_value(self) -> float:
        """Get total portfolio value."""
        resp = await self._request("GET", "/portfolio/balance")
        if resp and isinstance(resp, dict):
            balance = resp.get("balance", 0) / 100
            positions_value = resp.get("portfolio_value", 0) / 100
            return balance + positions_value
        return 0.0

    @staticmethod
    def _transfer_datetime(value: Any) -> datetime:
        """Parse Kalshi transfer timestamps, accepting seconds, ms, or ISO strings."""
        if value is None:
            return datetime.now(timezone.utc)
        try:
            if isinstance(value, (int, float)):
                timestamp = float(value)
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
                return datetime.fromtimestamp(timestamp, timezone.utc)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.replace(".", "", 1).isdigit():
                    timestamp = float(stripped)
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    return datetime.fromtimestamp(timestamp, timezone.utc)
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except Exception:
            pass
        return datetime.now(timezone.utc)

    @classmethod
    def _parse_transfer(cls, raw: dict, transfer_type: str) -> Transfer | None:
        """Normalize Kalshi deposit/withdrawal rows into the shared Transfer model."""
        try:
            amount_cents = raw.get("amount_cents")
            if amount_cents is None:
                amount_cents = raw.get("amount")
            amount = float(amount_cents or 0) / 100
            return Transfer(
                id=str(raw.get("id") or raw.get("transfer_id") or ""),
                type=transfer_type,
                amount=amount,
                status=str(raw.get("status") or "unknown"),
                created_at=cls._transfer_datetime(raw.get("created_ts") or raw.get("created_at")),
                platform="kalshi",
            )
        except Exception:
            return None

    async def get_transfers(self) -> list[Transfer]:
        """Get history of deposits and withdrawals from Kalshi."""
        transfers: list[Transfer] = []

        async def fetch_pages(path: str, collection_key: str, transfer_type: str):
            cursor = None
            for _ in range(20):
                params = {"limit": 500}
                if cursor:
                    params["cursor"] = cursor
                resp = await self._request("GET", path, params=params, silent=True)
                if not resp or not isinstance(resp, dict) or resp.get("error"):
                    break

                for raw in resp.get(collection_key, []) or []:
                    if isinstance(raw, dict):
                        transfer = self._parse_transfer(raw, transfer_type)
                        if transfer:
                            transfers.append(transfer)

                cursor = resp.get("cursor")
                if not cursor:
                    break
        
        try:
            await fetch_pages("/portfolio/deposits", "deposits", "deposit")
        except Exception as e:
            print(f"⚠️  Failed to fetch deposits: {e}")

        try:
            await fetch_pages("/portfolio/withdrawals", "withdrawals", "withdrawal")
        except Exception as e:
            print(f"⚠️  Failed to fetch withdrawals: {e}")

        return transfers
