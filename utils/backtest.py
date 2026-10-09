"""
Backtesting Module
===================

Provides backtesting capabilities using historical or simulated data.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from polymarket_client.models import (
    Market,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for backtesting."""
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    time_step_seconds: float = 1.0
    
    # Simulation parameters
    initial_balance: float = 10000.0
    simulate_fills: bool = True
    fill_probability: float = 0.8
    partial_fill_probability: float = 0.3
    
    # Price dynamics
    price_volatility: float = 0.01
    spread_range: tuple[float, float] = (0.02, 0.10)
    mispricing_probability: float = 0.05
    mispricing_magnitude: float = 0.03
    
    # Liquidity
    base_liquidity: float = 1000.0
    liquidity_variance: float = 0.5


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    
    # Performance
    initial_balance: float
    final_balance: float
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    
    # Trading statistics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    
    # Opportunity statistics
    bundle_opportunities: int
    mm_opportunities: int
    opportunities_acted_on: int
    
    # Risk metrics
    max_drawdown: float
    max_exposure: float
    sharpe_ratio: Optional[float] = None
    
    def summary(self) -> str:
        """Get a formatted summary."""
        return f"""
=== Backtest Results ===
Duration: {self.duration_seconds:.1f} seconds
PnL: ${self.total_pnl:.2f} ({self.total_pnl / self.initial_balance * 100:.1f}%)
  Realized: ${self.realized_pnl:.2f}
  Unrealized: ${self.unrealized_pnl:.2f}

Trading:
  Total Trades: {self.total_trades}
  Win Rate: {self.win_rate:.1%}
  
Opportunities:
  Bundle Arb: {self.bundle_opportunities}
  Market Making: {self.mm_opportunities}
  Acted Upon: {self.opportunities_acted_on}

Risk:
  Max Drawdown: {self.max_drawdown:.1%}
  Max Exposure: ${self.max_exposure:.2f}
"""


class SimulatedOrderBook:
    """Generates simulated order book data."""
    
    def __init__(
        self,
        market_id: str,
        initial_yes_price: float = 0.5,
        volatility: float = 0.01,
        spread_range: tuple[float, float] = (0.02, 0.08),
        base_liquidity: float = 1000.0,
    ):
        self.market_id = market_id
        self.yes_price = initial_yes_price
        self.volatility = volatility
        self.spread_range = spread_range
        self.base_liquidity = base_liquidity
    
    def step(self, introduce_mispricing: bool = False, mispricing_mag: float = 0.02) -> OrderBook:
        """Generate the next order book state."""
        # Random walk for YES price
        self.yes_price += random.gauss(0, self.volatility)
        self.yes_price = max(0.05, min(0.95, self.yes_price))
        
        # NO price should be roughly complementary
        no_price = 1.0 - self.yes_price
        
        # Add some inefficiency
        if introduce_mispricing:
            # Create arbitrage opportunity
            adjustment = random.uniform(0.5, 1.0) * mispricing_mag
            if random.random() < 0.5:
                # Bundle underpriced
                self.yes_price -= adjustment / 2
                no_price -= adjustment / 2
            else:
                # Bundle overpriced
                self.yes_price += adjustment / 2
                no_price += adjustment / 2
        
        no_price = max(0.05, min(0.95, no_price))
        
        # Generate spreads
        yes_spread = random.uniform(*self.spread_range)
        no_spread = random.uniform(*self.spread_range)
        
        # Generate books
        yes_book = self._generate_token_book(self.yes_price, yes_spread, TokenType.YES)
        no_book = self._generate_token_book(no_price, no_spread, TokenType.NO)
        
        return OrderBook(
            market_id=self.market_id,
            yes=yes_book,
            no=no_book,
            timestamp=datetime.utcnow(),
        )
    
    def _generate_token_book(
        self,
        mid_price: float,
        spread: float,
        token_type: TokenType,
    ) -> TokenOrderBook:
        """Generate order book for a single token."""
        bids = []
        asks = []
        
        best_bid = mid_price - spread / 2
        best_ask = mid_price + spread / 2
        
        # Generate 5 levels each side
        for i in range(5):
            bid_price = max(0.01, best_bid - i * 0.01)
            ask_price = min(0.99, best_ask + i * 0.01)
            
            # Declining liquidity away from best price
            liquidity_factor = 1.0 / (1 + i * 0.3)
            bid_size = self.base_liquidity * liquidity_factor * random.uniform(0.5, 1.5)
            ask_size = self.base_liquidity * liquidity_factor * random.uniform(0.5, 1.5)
            
            bids.append(PriceLevel(price=round(bid_price, 2), size=round(bid_size, 2)))
            asks.append(PriceLevel(price=round(ask_price, 2), size=round(ask_size, 2)))
        
        return TokenOrderBook(
            token_type=token_type,
            bids=OrderBookSide(levels=bids),
            asks=OrderBookSide(levels=asks),
        )


class BacktestEngine:
    """
    Backtesting engine for the trading bot.
    
    Simulates market data and order execution to test strategies.
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        
        # Simulated markets
        self._markets: dict[str, Market] = {}
        self._order_books: dict[str, SimulatedOrderBook] = {}
        
        # State
        self._current_time = config.start_time
        self._running = False
        
        # Results tracking
        self._pnl_history: list[tuple[datetime, float]] = []
        self._exposure_history: list[tuple[datetime, float]] = []
        self._opportunity_count = {"bundle": 0, "mm": 0}
        self._trade_count = 0
        
        logger.info("BacktestEngine initialized")
    
    def add_market(
        self,
        market_id: str,
        question: str = "",
        initial_yes_price: float = 0.5,
    ) -> None:
        """Add a market to the simulation."""
        self._markets[market_id] = Market(
            market_id=market_id,
            condition_id=market_id,
            question=question or f"Simulated Market {market_id}",
            active=True,
            volume_24h=random.uniform(10000, 100000),
        )
        
        self._order_books[market_id] = SimulatedOrderBook(
            market_id=market_id,
            initial_yes_price=initial_yes_price,
            volatility=self.config.price_volatility,
            spread_range=self.config.spread_range,
            base_liquidity=self.config.base_liquidity,
        )
        
        logger.info(f"Added simulated market: {market_id}")
    
    async def stream_orderbooks(self) -> AsyncIterator[tuple[str, OrderBook]]:
        """Stream simulated order book updates."""
        self._running = True
        
        while self._running:
            for market_id, sim_book in self._order_books.items():
                # Decide if we should introduce mispricing
                introduce_mispricing = random.random() < self.config.mispricing_probability
                
                order_book = sim_book.step(
                    introduce_mispricing=introduce_mispricing,
                    mispricing_mag=self.config.mispricing_magnitude,
                )
                
                yield (market_id, order_book)
            
            # Advance time
            self._current_time += timedelta(seconds=self.config.time_step_seconds)
            
            # Check end condition
            if self.config.end_time and self._current_time >= self.config.end_time:
                self._running = False
                break
            
            # Simulate real-time delay (can be reduced for faster backtests)
            await asyncio.sleep(0.01)  # Minimal delay for fast iteration
    
    def get_markets(self) -> list[Market]:
        """Get all simulated markets."""
        return list(self._markets.values())
    
    def simulate_fill(
        self,
        side: str,
        price: float,
        size: float,
    ) -> tuple[bool, float]:
        """
        Simulate order fill.
        
        Returns (filled, fill_size).
        """
        if not self.config.simulate_fills:
            return (False, 0.0)
        
        if random.random() > self.config.fill_probability:
            return (False, 0.0)
        
        # Determine fill size
        if random.random() < self.config.partial_fill_probability:
            fill_size = size * random.uniform(0.3, 0.9)
        else:
            fill_size = size
        
        self._trade_count += 1
        return (True, fill_size)
    
    def record_opportunity(self, opportunity_type: str) -> None:
        """Record a detected opportunity."""
        if opportunity_type in ("bundle_long", "bundle_short"):
            self._opportunity_count["bundle"] += 1
        else:
            self._opportunity_count["mm"] += 1
    
    def record_pnl(self, pnl: float) -> None:
        """Record current PnL."""
        self._pnl_history.append((self._current_time, pnl))
    
    def record_exposure(self, exposure: float) -> None:
        """Record current exposure."""
        self._exposure_history.append((self._current_time, exposure))
    
    def stop(self) -> None:
        """Stop the backtest."""
        self._running = False
    
    def get_result(
        self,
        final_balance: float,
        realized_pnl: float,
        unrealized_pnl: float,
        winning_trades: int,
        losing_trades: int,
    ) -> BacktestResult:
        """Generate backtest results."""
        duration = (self._current_time - self.config.start_time).total_seconds()
        
        # Calculate max drawdown
        max_drawdown = 0.0
        peak = self.config.initial_balance
        for _, pnl in self._pnl_history:
            equity = self.config.initial_balance + pnl
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak if peak > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
        
        # Calculate max exposure
        max_exposure = max((e for _, e in self._exposure_history), default=0)
        
        total_trades = winning_trades + losing_trades
        
        return BacktestResult(
            start_time=self.config.start_time,
            end_time=self._current_time,
            duration_seconds=duration,
            initial_balance=self.config.initial_balance,
            final_balance=final_balance,
            total_pnl=realized_pnl + unrealized_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=winning_trades / total_trades if total_trades > 0 else 0,
            bundle_opportunities=self._opportunity_count["bundle"],
            mm_opportunities=self._opportunity_count["mm"],
            opportunities_acted_on=self._trade_count,
            max_drawdown=max_drawdown,
            max_exposure=max_exposure,
        )


async def run_backtest(
    config: BacktestConfig,
    market_ids: list[str],
    arb_engine,
    execution_engine,
    risk_manager,
    portfolio,
    duration_seconds: float = 60.0,
) -> BacktestResult:
    """
    Run a backtest with the given components.
    
    This is a high-level function that sets up and runs a complete backtest.
    """
    logger.info(f"Starting backtest for {duration_seconds} simulated seconds")
    
    # Set end time
    config.end_time = config.start_time + timedelta(seconds=duration_seconds)
    
    # Create backtest engine
    engine = BacktestEngine(config)
    
    # Add markets
    for market_id in market_ids:
        initial_price = random.uniform(0.3, 0.7)
        engine.add_market(market_id, initial_yes_price=initial_price)
    
    # Run simulation
    update_count = 0
    async for market_id, order_book in engine.stream_orderbooks():
        update_count += 1
        
        # Create market state
        from polymarket_client.models import MarketState
        market = engine._markets[market_id]
        state = MarketState(
            market=market,
            order_book=order_book,
        )
        
        # Analyze for opportunities
        signals = arb_engine.analyze(state)
        
        for signal in signals:
            if signal.opportunity:
                engine.record_opportunity(signal.opportunity.opportunity_type.value)
            
            # Submit signal to execution
            await execution_engine.submit_signal(signal)
        
        # Record metrics
        engine.record_pnl(portfolio.stats.total_pnl)
        engine.record_exposure(portfolio.get_total_exposure())
        
        # Progress logging
        if update_count % 100 == 0:
            logger.info(f"Backtest progress: {update_count} updates processed")
    
    # Generate results
    result = engine.get_result(
        final_balance=config.initial_balance + portfolio.stats.total_pnl,
        realized_pnl=portfolio.stats.total_realized_pnl,
        unrealized_pnl=portfolio.stats.total_unrealized_pnl,
        winning_trades=portfolio.stats.winning_trades,
        losing_trades=portfolio.stats.losing_trades,
    )
    
    logger.info("Backtest completed")
    print(result.summary())
    
    return result

