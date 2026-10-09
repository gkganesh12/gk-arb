"""
Data Models for Kalshi API Client
=================================

Kalshi-specific data structures that map to our unified trading models.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from polymarket_client.models import PriceLevel, OrderBookSide, TokenOrderBook, OrderBook, TokenType


@dataclass
class KalshiMarket:
    """Kalshi market information."""
    ticker: str                     # e.g., "KXHIGHNY-25DEC08-T50"
    event_ticker: str               # e.g., "KXHIGHNY-25DEC08"
    series_ticker: str              # e.g., "KXHIGHNY"
    title: str                      # Full question
    subtitle: str = ""              # Additional context
    yes_sub_title: str = ""         # Outcome this market's YES pays on (e.g. a team name)
    
    # Prices (in dollars)
    yes_price: float = 0.0          # Last YES price
    no_price: float = 0.0           # Last NO price
    yes_bid: float = 0.0            # Top of book snapshot from the listing
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    
    # Market state
    status: str = "open"            # open, closed, settled
    result: Optional[str] = None    # yes, no, or None
    
    # Volume and liquidity
    volume: float = 0               # Total volume traded
    volume_24h: float = 0
    open_interest: float = 0        # Open positions
    
    # Timestamps
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    
    # Category
    category: str = ""
    
    @property
    def is_active(self) -> bool:
        """Check if market is actively trading."""
        return self.status in ("open", "active")
    
    @property
    def match_text(self) -> str:
        """
        Text used for cross-platform matching.
        
        Multi-outcome events share one title across markets ("Who will win X?"),
        so the YES outcome sub-title is what tells the markets apart.
        """
        sub_title = self.yes_sub_title.strip()
        if sub_title and sub_title.lower() not in self.title.lower():
            return f"{self.title} {sub_title}"
        return self.title
    
    def to_unified_market_id(self) -> str:
        """Create a unified ID for cross-platform matching."""
        return f"kalshi:{self.ticker}"


@dataclass
class KalshiOrderBook:
    """
    Kalshi order book.
    
    Note: Kalshi only returns bids in their API. For a binary market:
    - YES bids are what people will pay to buy YES
    - NO bids are what people will pay to buy NO
    
    The ask price can be derived: ask_yes = 1.0 - best_bid_no
    """
    ticker: str
    yes_bids: list[PriceLevel] = field(default_factory=list)
    no_bids: list[PriceLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def best_bid_yes(self) -> Optional[float]:
        """Best bid for YES tokens."""
        return self.yes_bids[0].price if self.yes_bids else None
    
    @property
    def best_bid_no(self) -> Optional[float]:
        """Best bid for NO tokens."""
        return self.no_bids[0].price if self.no_bids else None
    
    @property
    def best_ask_yes(self) -> Optional[float]:
        """
        Derived ask for YES tokens.
        If someone bids X for NO, they're implicitly offering YES at (1.0 - X).
        """
        if not self.no_bids:
            return None
        return 1.0 - self.no_bids[0].price
    
    @property
    def best_ask_no(self) -> Optional[float]:
        """
        Derived ask for NO tokens.
        If someone bids X for YES, they're implicitly offering NO at (1.0 - X).
        """
        if not self.yes_bids:
            return None
        return 1.0 - self.yes_bids[0].price
    
    def to_unified_orderbook(self) -> OrderBook:
        """Convert to unified OrderBook format for cross-platform arbitrage."""
        yes_token_ob = TokenOrderBook(TokenType.YES)
        no_token_ob = TokenOrderBook(TokenType.NO)
        
        # Set YES side
        yes_token_ob.bids = OrderBookSide(levels=self.yes_bids.copy())
        # Derive asks from NO bids
        if self.no_bids:
            derived_yes_asks = [
                PriceLevel(price=1.0 - bid.price, size=bid.size)
                for bid in self.no_bids
            ]
            # Sort asks ascending (best/lowest first)
            derived_yes_asks.sort(key=lambda x: x.price)
            yes_token_ob.asks = OrderBookSide(levels=derived_yes_asks)
        
        # Set NO side
        no_token_ob.bids = OrderBookSide(levels=self.no_bids.copy())
        # Derive asks from YES bids
        if self.yes_bids:
            derived_no_asks = [
                PriceLevel(price=1.0 - bid.price, size=bid.size)
                for bid in self.yes_bids
            ]
            derived_no_asks.sort(key=lambda x: x.price)
            no_token_ob.asks = OrderBookSide(levels=derived_no_asks)
        
        return OrderBook(
            market_id=f"kalshi:{self.ticker}",
            yes=yes_token_ob,
            no=no_token_ob,
            timestamp=self.timestamp
        )


@dataclass
class KalshiEvent:
    """Kalshi event (contains multiple markets)."""
    event_ticker: str
    series_ticker: str
    title: str
    category: str
    markets: list[KalshiMarket] = field(default_factory=list)
    
    @property
    def market_count(self) -> int:
        return len(self.markets)


@dataclass
class KalshiSeries:
    """Kalshi series (recurring events)."""
    ticker: str
    title: str
    frequency: str  # daily, weekly, etc.
    category: str

