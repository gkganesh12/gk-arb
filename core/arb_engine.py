"""
Arbitrage Engine Module
========================

Detects trading opportunities including:
1. Bundle mispricing (YES + NO != 1.0)
2. Market-making spread capture
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from core.depth import walk_two_legs
from core.fees import polymarket_fee_per_share
from polymarket_client.models import (
    Market,
    MarketState,
    Opportunity,
    OpportunityType,
    OrderBook,
    OrderSide,
    Signal,
    TokenType,
)


logger = logging.getLogger(__name__)


@dataclass
class ArbConfig:
    """Configuration for the arbitrage engine."""
    # Bundle arbitrage
    min_edge: float = 0.01  # Minimum edge required (1%)
    bundle_arb_enabled: bool = True
    
    # Market-making
    min_spread: float = 0.05  # Minimum spread to MM (5c)
    mm_enabled: bool = True
    tick_size: float = 0.01
    
    # Sizing
    default_order_size: float = 50.0
    min_order_size: float = 5.0
    max_order_size: float = 200.0
    
    # Signal expiry
    signal_expiry_seconds: float = 5.0
    
    # Depth sizing only walks levels within this distance of the top of book,
    # matching the execution engine's slippage check
    slippage_tolerance: float = 0.02
    
    # Fees (in basis points - 100 bps = 1%)
    # Markets with a known Gamma fee schedule use that curve instead;
    # these flat rates only apply when a market's schedule is unknown.
    maker_fee_bps: float = 0        # Limit orders adding liquidity
    taker_fee_bps: float = 150      # Taking liquidity (1.5%)
    gas_cost_per_order: float = 0.02


@dataclass
class OpportunityTiming:
    """Tracks timing of a specific opportunity."""
    opportunity_id: str
    market_id: str
    opportunity_type: str
    detected_at: datetime
    edge: float
    expired_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    was_executed: bool = False
    
    def mark_expired(self, executed: bool = False) -> None:
        """Mark this opportunity as expired."""
        self.expired_at = datetime.utcnow()
        self.duration_ms = (self.expired_at - self.detected_at).total_seconds() * 1000
        self.was_executed = executed


@dataclass
class ArbStats:
    """Statistics for the arbitrage engine."""
    bundle_opportunities_detected: int = 0
    mm_opportunities_detected: int = 0
    signals_generated: int = 0
    last_opportunity_time: Optional[datetime] = None
    
    # Opportunity duration tracking
    total_opportunities_tracked: int = 0
    avg_opportunity_duration_ms: float = 0.0
    min_opportunity_duration_ms: float = float('inf')
    max_opportunity_duration_ms: float = 0.0
    opportunities_under_100ms: int = 0
    opportunities_under_500ms: int = 0
    opportunities_under_1s: int = 0
    opportunities_over_1s: int = 0


class ArbEngine:
    """
    Arbitrage and market-making opportunity detection engine.
    
    Analyzes market states from the DataFeed and generates trading
    signals for the ExecutionEngine.
    """
    
    def __init__(self, config: ArbConfig):
        self.config = config
        self.stats = ArbStats()
        
        # Track recent opportunities to avoid duplicates
        self._recent_opportunities: dict[str, Opportunity] = {}
        self._opportunity_cooldown: dict[str, datetime] = {}
        
        # Track active opportunities for duration measurement
        self._active_opportunities: dict[str, OpportunityTiming] = {}
        self._opportunity_history: list[OpportunityTiming] = []
        
        logger.info(f"ArbEngine initialized with min_edge={config.min_edge}, min_spread={config.min_spread}")
    
    def analyze(self, market_state: MarketState) -> list[Signal]:
        """
        Analyze a market state and generate trading signals.
        
        Returns a list of signals (may be empty if no opportunities).
        """
        signals: list[Signal] = []
        
        order_book = market_state.order_book
        market_id = market_state.market.market_id
        
        # Check if previously tracked opportunities have expired
        self._check_expired_opportunities(market_id, order_book)
        
        # Check for bundle arbitrage
        if self.config.bundle_arb_enabled:
            bundle_signal = self._check_bundle_arbitrage(market_id, order_book, market_state.market)
            if bundle_signal:
                signals.append(bundle_signal)
        
        # Check for market-making opportunities
        if self.config.mm_enabled:
            mm_signals = self._check_market_making(market_id, order_book)
            signals.extend(mm_signals)
        
        return signals
    
    def _check_expired_opportunities(self, market_id: str, order_book: OrderBook) -> None:
        """Check if any tracked opportunities have expired (prices moved away)."""
        now = datetime.utcnow()
        expired_keys = []
        
        for key, timing in self._active_opportunities.items():
            if timing.market_id != market_id:
                continue
            
            # Check if opportunity still exists
            still_valid = False
            
            if "bundle_long" in timing.opportunity_type:
                # Check if total ask is still < 1 - min_edge
                if order_book.best_ask_yes and order_book.best_ask_no:
                    total_ask = order_book.best_ask_yes + order_book.best_ask_no
                    if 1.0 - total_ask >= self.config.min_edge * 0.5:  # Use lower threshold
                        still_valid = True
                        
            elif "bundle_short" in timing.opportunity_type:
                # Check if total bid is still > 1 + min_edge
                if order_book.best_bid_yes and order_book.best_bid_no:
                    total_bid = order_book.best_bid_yes + order_book.best_bid_no
                    if total_bid - 1.0 >= self.config.min_edge * 0.5:
                        still_valid = True
            
            # Also expire if too old (10 seconds max)
            age_seconds = (now - timing.detected_at).total_seconds()
            if age_seconds > 10:
                still_valid = False
            
            if not still_valid:
                timing.mark_expired(executed=False)
                self._record_opportunity_duration(timing)
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._active_opportunities[key]
    
    def _record_opportunity_duration(self, timing: OpportunityTiming) -> None:
        """Record the duration of an expired opportunity and update stats."""
        if timing.duration_ms is None:
            return
        
        self._opportunity_history.append(timing)
        
        # Keep only last 1000 opportunities
        if len(self._opportunity_history) > 1000:
            self._opportunity_history = self._opportunity_history[-500:]
        
        # Update stats
        self.stats.total_opportunities_tracked += 1
        
        # Update min/max
        if timing.duration_ms < self.stats.min_opportunity_duration_ms:
            self.stats.min_opportunity_duration_ms = timing.duration_ms
        if timing.duration_ms > self.stats.max_opportunity_duration_ms:
            self.stats.max_opportunity_duration_ms = timing.duration_ms
        
        # Update running average
        n = self.stats.total_opportunities_tracked
        old_avg = self.stats.avg_opportunity_duration_ms
        self.stats.avg_opportunity_duration_ms = old_avg + (timing.duration_ms - old_avg) / n
        
        # Update duration buckets
        if timing.duration_ms < 100:
            self.stats.opportunities_under_100ms += 1
        elif timing.duration_ms < 500:
            self.stats.opportunities_under_500ms += 1
        elif timing.duration_ms < 1000:
            self.stats.opportunities_under_1s += 1
        else:
            self.stats.opportunities_over_1s += 1
        
        logger.info(
            f"Opportunity EXPIRED: {timing.opportunity_type} | "
            f"duration={timing.duration_ms:.0f}ms | edge={timing.edge:.4f} | "
            f"market={timing.market_id}"
        )
    
    def _start_tracking_opportunity(self, opportunity: Opportunity) -> None:
        """Start tracking an opportunity for duration measurement."""
        key = f"{opportunity.market_id}_{opportunity.opportunity_type.value}"
        
        # Don't double-track
        if key in self._active_opportunities:
            return
        
        timing = OpportunityTiming(
            opportunity_id=opportunity.opportunity_id,
            market_id=opportunity.market_id,
            opportunity_type=opportunity.opportunity_type.value,
            detected_at=datetime.utcnow(),
            edge=opportunity.edge,
        )
        self._active_opportunities[key] = timing
    
    def mark_opportunity_executed(self, market_id: str, opportunity_type: str) -> None:
        """Mark an opportunity as executed (for accurate tracking)."""
        key = f"{market_id}_{opportunity_type}"
        if key in self._active_opportunities:
            timing = self._active_opportunities[key]
            timing.mark_expired(executed=True)
            self._record_opportunity_duration(timing)
            del self._active_opportunities[key]
    
    def get_timing_stats(self) -> dict:
        """Get opportunity timing statistics for dashboard."""
        recent_history = self._opportunity_history[-100:] if self._opportunity_history else []
        
        return {
            "total_tracked": self.stats.total_opportunities_tracked,
            "avg_duration_ms": round(self.stats.avg_opportunity_duration_ms, 1),
            "min_duration_ms": round(self.stats.min_opportunity_duration_ms, 1) if self.stats.min_opportunity_duration_ms != float('inf') else None,
            "max_duration_ms": round(self.stats.max_opportunity_duration_ms, 1),
            "under_100ms": self.stats.opportunities_under_100ms,
            "under_500ms": self.stats.opportunities_under_500ms,
            "under_1s": self.stats.opportunities_under_1s,
            "over_1s": self.stats.opportunities_over_1s,
            "active_opportunities": len(self._active_opportunities),
            "recent_durations": [
                {
                    "type": t.opportunity_type,
                    "duration_ms": round(t.duration_ms, 1) if t.duration_ms else 0,
                    "edge": round(t.edge, 4),
                    "executed": t.was_executed,
                    "time": t.detected_at.isoformat(),
                }
                for t in recent_history[-20:]
            ]
        }
    
    def _taker_fee_per_share(self, market: Optional[Market], price: float) -> float:
        """Taker fee for one share: the market's fee curve if known, else the flat config rate."""
        if market is not None and market.fee_rate is not None:
            return polymarket_fee_per_share(price, market.fee_rate, market.fee_exponent)
        return price * self.config.taker_fee_bps / 10000
    
    def _check_bundle_arbitrage(
        self,
        market_id: str,
        order_book: OrderBook,
        market: Optional[Market] = None,
    ) -> Optional[Signal]:
        """
        Check for bundle mispricing opportunities.
        
        Bundle Long: Buy YES + NO when total_ask + fees < 1 - min_edge
        Bundle Short: Sell YES + NO when total_bid - fees > 1 + min_edge
        
        Size comes from walking both books together while each extra share
        stays profitable after fees, capped by the per-order budget. Limit
        prices are the deepest level touched on each leg.
        """
        best_ask_yes = order_book.best_ask_yes
        best_ask_no = order_book.best_ask_no
        best_bid_yes = order_book.best_bid_yes
        best_bid_no = order_book.best_bid_no
        
        # Need all prices to evaluate
        if None in (best_ask_yes, best_ask_no, best_bid_yes, best_bid_no):
            return None
        
        def fee(price: float) -> float:
            return self._taker_fee_per_share(market, price)
        
        tolerance = self.config.slippage_tolerance
        
        def near_top(levels, buying: bool):
            if not levels:
                return levels
            best = levels[0].price
            if buying:
                return [level for level in levels if level.price <= best * (1 + tolerance)]
            return [level for level in levels if level.price >= best * (1 - tolerance)]
        
        gas_cost = self.config.gas_cost_per_order * 2  # 2 orders
        min_size = max(self.config.min_order_size, market.min_order_size if market else 0.0)
        
        candidates = [
            (
                OpportunityType.BUNDLE_LONG,
                near_top(order_book.yes.asks.levels, buying=True),
                near_top(order_book.no.asks.levels, buying=True),
                lambda yes_price, no_price: (1.0 - yes_price - no_price, fee(yes_price), fee(no_price)),
                max(best_ask_yes, best_ask_no),
            ),
            (
                OpportunityType.BUNDLE_SHORT,
                near_top(order_book.yes.bids.levels, buying=False),
                near_top(order_book.no.bids.levels, buying=False),
                lambda yes_price, no_price: (yes_price + no_price - 1.0, fee(yes_price), fee(no_price)),
                max(best_bid_yes, best_bid_no),
            ),
        ]
        
        opportunity: Optional[Opportunity] = None
        limit_prices: tuple[float, float] = (0.0, 0.0)
        
        for opportunity_type, yes_levels, no_levels, unit_economics, top_price in candidates:
            budget_size = self.config.default_order_size / top_price if top_price > 0 else 0.0
            fill = walk_two_legs(yes_levels, no_levels, unit_economics, self.config.min_edge, budget_size)
            if fill.size <= 0 or fill.size < min_size:
                continue
            
            # Gas is a fixed cost per order, so it's charged once against the whole fill
            edge = (fill.gross_edge - fill.fees - gas_cost) / fill.size
            if edge < self.config.min_edge:
                continue
            
            label = "LONG" if opportunity_type == OpportunityType.BUNDLE_LONG else "SHORT"
            opportunity = Opportunity(
                opportunity_id=f"{opportunity_type.value}_{uuid.uuid4().hex[:8]}",
                opportunity_type=opportunity_type,
                market_id=market_id,
                edge=edge,
                best_bid_yes=best_bid_yes,
                best_ask_yes=best_ask_yes,
                best_bid_no=best_bid_no,
                best_ask_no=best_ask_no,
                suggested_size=fill.size,
                max_size=fill.size,
                expires_at=datetime.utcnow() + timedelta(seconds=self.config.signal_expiry_seconds),
            )
            limit_prices = (fill.limit_a, fill.limit_b)
            
            self.stats.bundle_opportunities_detected += 1
            logger.info(
                f"Bundle {label} opportunity: {market_id} | "
                f"avg YES={fill.avg_price_a:.4f} avg NO={fill.avg_price_b:.4f} | "
                f"gross={fill.gross_edge / fill.size:.4f} | fees={fill.fees / fill.size:.4f} | "
                f"NET edge={edge:.4f} | size={fill.size:.2f}"
            )
            break
        
        if not opportunity:
            return None
        
        # Check cooldown to avoid spam
        cooldown_key = f"{market_id}_{opportunity.opportunity_type.value}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None
        
        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)
        self._recent_opportunities[opportunity.opportunity_id] = opportunity
        self.stats.last_opportunity_time = datetime.utcnow()
        
        # Start tracking for duration measurement
        self._start_tracking_opportunity(opportunity)
        
        # Generate signal
        return self._create_bundle_signal(opportunity, *limit_prices)
    
    def _create_bundle_signal(self, opportunity: Opportunity, yes_limit: float, no_limit: float) -> Signal:
        """Create a trading signal for a bundle arbitrage opportunity."""
        orders = []
        
        if opportunity.opportunity_type == OpportunityType.BUNDLE_LONG:
            # Buy both YES and NO, limit at the deepest ask level walked
            orders = [
                {
                    "token_type": TokenType.YES,
                    "side": OrderSide.BUY,
                    "price": yes_limit,
                    "size": opportunity.suggested_size,
                    "strategy_tag": "bundle_arb",
                },
                {
                    "token_type": TokenType.NO,
                    "side": OrderSide.BUY,
                    "price": no_limit,
                    "size": opportunity.suggested_size,
                    "strategy_tag": "bundle_arb",
                },
            ]
        else:
            # Sell both YES and NO, limit at the deepest bid level walked
            orders = [
                {
                    "token_type": TokenType.YES,
                    "side": OrderSide.SELL,
                    "price": yes_limit,
                    "size": opportunity.suggested_size,
                    "strategy_tag": "bundle_arb",
                },
                {
                    "token_type": TokenType.NO,
                    "side": OrderSide.SELL,
                    "price": no_limit,
                    "size": opportunity.suggested_size,
                    "strategy_tag": "bundle_arb",
                },
            ]
        
        signal = Signal(
            signal_id=f"sig_{uuid.uuid4().hex[:12]}",
            action="place_orders",
            market_id=opportunity.market_id,
            opportunity=opportunity,
            orders=orders,
            priority=10,  # High priority for arb
        )
        
        self.stats.signals_generated += 1
        return signal
    
    def _check_market_making(self, market_id: str, order_book: OrderBook) -> list[Signal]:
        """
        Check for market-making opportunities on YES and NO tokens.
        
        Place limit orders inside the spread to capture the bid-ask spread.
        """
        signals = []
        
        # Check YES token
        yes_signal = self._check_mm_token(market_id, order_book.yes, TokenType.YES)
        if yes_signal:
            signals.append(yes_signal)
        
        # Check NO token
        no_signal = self._check_mm_token(market_id, order_book.no, TokenType.NO)
        if no_signal:
            signals.append(no_signal)
        
        return signals
    
    def _check_mm_token(
        self,
        market_id: str,
        token_book,
        token_type: TokenType
    ) -> Optional[Signal]:
        """Check market-making opportunity for a single token."""
        best_bid = token_book.best_bid
        best_ask = token_book.best_ask
        spread = token_book.spread
        
        if spread is None or best_bid is None or best_ask is None:
            return None
        
        # Check if spread is wide enough
        if spread < self.config.min_spread:
            return None
        
        # Check cooldown
        cooldown_key = f"mm_{market_id}_{token_type.value}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None
        
        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=5)
        
        # Calculate our prices (inside the spread)
        our_bid = best_bid + self.config.tick_size
        our_ask = best_ask - self.config.tick_size
        
        # Make sure we still have positive edge
        if our_ask <= our_bid:
            return None
        
        our_spread = our_ask - our_bid
        if our_spread < self.config.tick_size * 2:
            return None
        
        # Calculate size
        order_size = self.config.default_order_size / ((our_bid + our_ask) / 2)
        order_size = min(order_size, self.config.max_order_size)
        order_size = max(order_size, self.config.min_order_size)
        
        # Create opportunity for logging
        opportunity = Opportunity(
            opportunity_id=f"mm_{token_type.value}_{uuid.uuid4().hex[:8]}",
            opportunity_type=OpportunityType.MM_BID if token_type == TokenType.YES else OpportunityType.MM_ASK,
            market_id=market_id,
            edge=our_spread / 2,  # Expected edge per side
            suggested_size=order_size,
            max_size=order_size * 2,
        )
        
        self.stats.mm_opportunities_detected += 1
        self.stats.last_opportunity_time = datetime.utcnow()
        
        logger.info(
            f"MM opportunity: {market_id}/{token_type.value} | "
            f"spread={spread:.4f} | our_spread={our_spread:.4f} | size={order_size:.2f}"
        )
        
        # Generate signal with both bid and ask orders
        orders = [
            {
                "token_type": token_type,
                "side": OrderSide.BUY,
                "price": our_bid,
                "size": order_size,
                "strategy_tag": "market_making",
            },
            {
                "token_type": token_type,
                "side": OrderSide.SELL,
                "price": our_ask,
                "size": order_size,
                "strategy_tag": "market_making",
            },
        ]
        
        signal = Signal(
            signal_id=f"sig_{uuid.uuid4().hex[:12]}",
            action="place_orders",
            market_id=market_id,
            opportunity=opportunity,
            orders=orders,
            priority=5,  # Lower priority than arb
        )
        
        self.stats.signals_generated += 1
        return signal
    
    def get_recent_opportunities(self, max_age_seconds: float = 60.0) -> list[Opportunity]:
        """Get recently detected opportunities."""
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        return [
            opp for opp in self._recent_opportunities.values()
            if opp.detected_at > cutoff
        ]
    
    def clear_expired_opportunities(self) -> int:
        """Remove expired opportunities from cache."""
        now = datetime.utcnow()
        expired = [
            opp_id for opp_id, opp in self._recent_opportunities.items()
            if opp.expires_at and opp.expires_at < now
        ]
        for opp_id in expired:
            del self._recent_opportunities[opp_id]
        return len(expired)
    
    def get_stats(self) -> ArbStats:
        """Get engine statistics."""
        return self.stats

