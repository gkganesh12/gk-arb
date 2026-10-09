"""
Kalshi API Client
=================

Client for interacting with Kalshi prediction market exchange.
Supports public market data endpoints (no authentication required).

API Documentation: https://docs.kalshi.com/getting_started/quick_start_market_data
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, AsyncIterator
import httpx

from kalshi_client.models import (
    KalshiMarket,
    KalshiOrderBook,
    KalshiEvent,
    KalshiSeries,
)
from polymarket_client.models import PriceLevel, OrderBook

logger = logging.getLogger(__name__)


def _to_float(value) -> Optional[float]:
    """Parse numbers that may arrive as strings ("0.4500") or be missing."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(data: dict, dollars_key: str, cents_key: str) -> float:
    """
    Read a price in dollars.
    
    The API now returns `*_dollars` strings; older responses used integer cents.
    """
    dollars = _to_float(data.get(dollars_key))
    if dollars is not None:
        return dollars
    cents = _to_float(data.get(cents_key))
    return cents / 100.0 if cents else 0.0


def _quantity(data: dict, fp_key: str, legacy_key: str) -> float:
    """Read a count that may be a fixed-point string (`*_fp`) or a legacy integer."""
    value = _to_float(data.get(fp_key))
    if value is None:
        value = _to_float(data.get(legacy_key))
    return value or 0.0


def _parse_levels(raw_levels: Optional[list], scale: float) -> list[PriceLevel]:
    """Parse [price, quantity] pairs, dividing prices by `scale` (100 for cents)."""
    levels = []
    for level in raw_levels or []:
        if len(level) < 2:
            continue
        price = _to_float(level[0])
        quantity = _to_float(level[1])
        if price is None or not quantity:
            continue
        levels.append(PriceLevel(price=price / scale, size=quantity))
    # Bids, best (highest) first
    levels.sort(key=lambda x: x.price, reverse=True)
    return levels


class KalshiClient:
    """
    Async client for Kalshi prediction market API.
    
    Note: Uses the elections subdomain which provides access to ALL markets,
    not just election-related ones.
    """
    
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    
    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        dry_run: bool = True,
        base_url: Optional[str] = None,
        max_concurrency: int = 4,
    ):
        """
        Initialize Kalshi client.
        
        Args:
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            dry_run: If True, don't place real orders (read-only mode)
            base_url: API base URL (defaults to BASE_URL)
            max_concurrency: Max in-flight requests (Kalshi resets connections when hammered)
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._markets_cache: dict[str, KalshiMarket] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)
    
    async def connect(self) -> None:
        """Open the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Accept": "application/json"}
            )
    
    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        
    async def __aenter__(self) -> "KalshiClient":
        """Async context manager entry."""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
    
    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        Make a GET request to the Kalshi API.
        
        Args:
            endpoint: API endpoint (without base URL)
            params: Query parameters
            
        Returns:
            JSON response as dictionary
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() or use async with.")
        
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(self.max_retries):
            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Rate limited
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                    await asyncio.sleep(wait_time)
                elif e.response.status_code == 404:
                    logger.debug(f"Not found: {endpoint}")
                    return {}
                else:
                    logger.error(f"HTTP error {e.response.status_code}: {e}")
                    raise
            except httpx.RequestError as e:
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise
        
        return {}
    
    # =========================================================================
    # SERIES ENDPOINTS
    # =========================================================================
    
    async def get_series(self, series_ticker: str) -> Optional[KalshiSeries]:
        """
        Get information about a series.
        
        Args:
            series_ticker: Series ticker (e.g., "KXHIGHNY")
            
        Returns:
            KalshiSeries object or None if not found
        """
        data = await self._get(f"/series/{series_ticker}")
        if not data or "series" not in data:
            return None
        
        s = data["series"]
        return KalshiSeries(
            ticker=s.get("ticker", series_ticker),
            title=s.get("title", ""),
            frequency=s.get("frequency", ""),
            category=s.get("category", ""),
        )
    
    # =========================================================================
    # EVENTS ENDPOINTS
    # =========================================================================
    
    async def get_event(self, event_ticker: str) -> Optional[KalshiEvent]:
        """
        Get information about an event.
        
        Args:
            event_ticker: Event ticker (e.g., "KXHIGHNY-25DEC08")
            
        Returns:
            KalshiEvent object or None if not found
        """
        data = await self._get(f"/events/{event_ticker}")
        if not data or "event" not in data:
            return None
        
        e = data["event"]
        return KalshiEvent(
            event_ticker=e.get("ticker", event_ticker),
            series_ticker=e.get("series_ticker", ""),
            title=e.get("title", ""),
            category=e.get("category", ""),
        )
    
    # =========================================================================
    # MARKETS ENDPOINTS
    # =========================================================================
    
    async def list_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """
        List markets with optional filters.
        
        Args:
            status: Market status filter (open, closed, settled)
            series_ticker: Filter by series
            event_ticker: Filter by event
            limit: Maximum markets to return (max 1000)
            cursor: Pagination cursor
            
        Returns:
            Tuple of (list of markets, next cursor or None)
        """
        # mve_filter=exclude drops multivariate "parlay" markets, which flood the
        # listing and can never match a single Polymarket question
        params = {"status": status, "limit": min(limit, 1000), "mve_filter": "exclude"}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        
        data = await self._get("/markets", params=params)
        if not data or "markets" not in data:
            return [], None
        
        markets = []
        for m in data["markets"]:
            market = self._parse_market(m)
            if market:
                markets.append(market)
                self._markets_cache[market.ticker] = market
        
        next_cursor = data.get("cursor")
        return markets, next_cursor
    
    async def list_all_markets(
        self,
        status: str = "open",
        max_markets: int = 10000,
        on_progress: callable = None,  # Callback for progress updates
    ) -> list[KalshiMarket]:
        """
        Fetch all markets with pagination.
        
        Args:
            status: Market status filter
            max_markets: Maximum total markets to fetch
            on_progress: Optional callback(loaded_count) for progress updates
            
        Returns:
            List of all markets
        """
        all_markets = []
        cursor = None
        
        while len(all_markets) < max_markets:
            markets, next_cursor = await self.list_markets(
                status=status,
                limit=1000,
                cursor=cursor,
            )
            
            if not markets:
                break
            
            all_markets.extend(markets)
            logger.info(f"Kalshi: {len(all_markets)} markets loaded...")
            
            # Report progress
            if on_progress:
                try:
                    on_progress(len(all_markets))
                except:
                    pass
            
            if not next_cursor:
                break
            cursor = next_cursor
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.2)
        
        logger.info(f"Kalshi: {len(all_markets)} total markets loaded ✓")
        return all_markets[:max_markets]
    
    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """
        Get a specific market by ticker.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiMarket object or None if not found
        """
        # Check cache first
        if ticker in self._markets_cache:
            return self._markets_cache[ticker]
        
        data = await self._get(f"/markets/{ticker}")
        if not data or "market" not in data:
            return None
        
        market = self._parse_market(data["market"])
        if market:
            self._markets_cache[ticker] = market
        return market
    
    def _parse_market(self, data: dict) -> Optional[KalshiMarket]:
        """Parse market data from API response."""
        try:
            if data.get("mve_collection_ticker"):
                return None  # Multivariate parlay leg bundle
            
            yes_bid = _price(data, "yes_bid_dollars", "yes_bid")
            yes_ask = _price(data, "yes_ask_dollars", "yes_ask")
            no_bid = _price(data, "no_bid_dollars", "no_bid")
            no_ask = _price(data, "no_ask_dollars", "no_ask")
            
            yes_price = _price(data, "last_price_dollars", "last_price")
            if not yes_price and yes_bid and yes_ask:
                yes_price = (yes_bid + yes_ask) / 2
            no_price = 1.0 - yes_price if yes_price > 0 else 0.0
            
            # Parse close time
            close_time = None
            if data.get("close_time"):
                try:
                    close_time = datetime.fromisoformat(data["close_time"].replace("Z", "+00:00"))
                except ValueError:
                    pass
            
            return KalshiMarket(
                ticker=data.get("ticker", ""),
                event_ticker=data.get("event_ticker", ""),
                series_ticker=data.get("series_ticker", ""),
                title=data.get("title", "") or "",
                subtitle=data.get("subtitle", "") or "",
                yes_sub_title=data.get("yes_sub_title", "") or "",
                yes_price=yes_price,
                no_price=no_price,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                status=data.get("status", ""),
                result=data.get("result") or None,
                volume=_quantity(data, "volume_fp", "volume"),
                volume_24h=_quantity(data, "volume_24h_fp", "volume_24h"),
                open_interest=_quantity(data, "open_interest_fp", "open_interest"),
                close_time=close_time,
                category=data.get("category", "") or "",
            )
        except Exception as e:
            logger.warning(f"Failed to parse Kalshi market: {e}")
            return None
    
    # =========================================================================
    # ORDERBOOK ENDPOINTS
    # =========================================================================
    
    async def get_orderbook(self, ticker: str) -> Optional[KalshiOrderBook]:
        """
        Get order book for a market.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiOrderBook object or None if not found
        """
        data = await self._get(f"/markets/{ticker}/orderbook")
        if not data:
            return None
        
        # Current API: {"orderbook_fp": {"yes_dollars": [["0.45", "100.00"], ...], "no_dollars": [...]}}
        # Legacy API:  {"orderbook": {"yes": [[45, 100], ...], "no": [...]}} (cents)
        book = data.get("orderbook_fp") or data.get("orderbook")
        if book is None:
            return None
        if "yes_dollars" in book or "no_dollars" in book:
            yes_bids = _parse_levels(book.get("yes_dollars"), scale=1.0)
            no_bids = _parse_levels(book.get("no_dollars"), scale=1.0)
        else:
            yes_bids = _parse_levels(book.get("yes"), scale=100.0)
            no_bids = _parse_levels(book.get("no"), scale=100.0)
        
        return KalshiOrderBook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            timestamp=datetime.utcnow(),
        )
    
    async def get_orderbook_unified(self, ticker: str) -> Optional[OrderBook]:
        """
        Get order book in unified format (compatible with Polymarket).
        
        Args:
            ticker: Market ticker
            
        Returns:
            OrderBook object or None if not found
        """
        kalshi_ob = await self.get_orderbook(ticker)
        if not kalshi_ob:
            return None
        return kalshi_ob.to_unified_orderbook()
    
    async def get_orderbooks_unified(self, tickers: list[str]) -> dict[str, OrderBook]:
        """
        Fetch unified order books for many markets.
        
        Requests run concurrently but are throttled by the client's semaphore.
        Markets whose book can't be fetched are left out of the result.
        """
        results = await asyncio.gather(
            *(self.get_orderbook_unified(ticker) for ticker in tickers),
            return_exceptions=True,
        )
        books = {}
        for ticker, result in zip(tickers, results):
            if isinstance(result, Exception):
                logger.debug(f"Failed to get Kalshi orderbook for {ticker}: {result}")
            elif result:
                books[ticker] = result
        return books
    
    # =========================================================================
    # STREAMING (Polling-based for public API)
    # =========================================================================
    
    async def stream_orderbooks(
        self,
        tickers: list[str],
        batch_size: int = 100,
        rotation_delay: float = 2.0,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        """
        Stream order books for multiple markets using polling.
        
        Args:
            tickers: List of market tickers to stream
            batch_size: Number of markets to fetch per batch
            rotation_delay: Delay between batches in seconds
            
        Yields:
            Tuple of (ticker, OrderBook) for each update
        """
        logger.info(f"Starting Kalshi orderbook stream for {len(tickers)} markets")
        
        while True:
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i:i + batch_size]
                logger.debug(f"Fetching Kalshi orderbooks {i+1}-{min(i+batch_size, len(tickers))} of {len(tickers)}")
                
                # Fetch orderbooks in parallel
                tasks = [self.get_orderbook_unified(ticker) for ticker in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for ticker, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.debug(f"Failed to get Kalshi orderbook for {ticker}: {result}")
                        continue
                    if result:
                        yield (ticker, result)
                
                await asyncio.sleep(rotation_delay)
    
    # =========================================================================
    # CATEGORY/SEARCH HELPERS
    # =========================================================================
    
    async def get_markets_by_category(self, category: str) -> list[KalshiMarket]:
        """
        Get all open markets in a category.
        
        Common categories: elections, economics, crypto, tech, entertainment
        """
        # Kalshi API doesn't have a direct category filter, so we fetch all
        # and filter client-side
        all_markets = await self.list_all_markets(status="open")
        return [m for m in all_markets if m.category.lower() == category.lower()]
    
    async def search_markets(self, query: str) -> list[KalshiMarket]:
        """
        Search markets by title.
        
        Args:
            query: Search query string
            
        Returns:
            List of matching markets
        """
        all_markets = await self.list_all_markets(status="open")
        query_lower = query.lower()
        return [
            m for m in all_markets 
            if query_lower in m.title.lower() or query_lower in m.subtitle.lower()
        ]

