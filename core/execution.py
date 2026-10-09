"""
Execution Engine Module
========================

Handles order placement, cancellation, and management.
Consumes signals from the ArbEngine and interfaces with the API.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from polymarket_client.api import PolymarketClient
from polymarket_client.models import (
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    Signal,
    TokenType,
    Trade,
)
from core.risk_manager import RiskManager
from core.portfolio import Portfolio


logger = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    """Configuration for the execution engine."""
    slippage_tolerance: float = 0.02  # Max allowed price slippage
    order_timeout_seconds: float = 60.0  # Cancel unfilled orders after this time
    max_retries: int = 3
    retry_delay: float = 0.5
    enable_slippage_check: bool = True
    dry_run: bool = True


@dataclass
class ExecutionStats:
    """Statistics for the execution engine."""
    orders_placed: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    total_notional: float = 0.0
    signals_processed: int = 0
    signals_rejected: int = 0
    slippage_rejections: int = 0


class ExecutionEngine:
    """
    Order execution engine.
    
    Consumes trading signals and places/manages orders through the
    Polymarket API. Enforces risk limits and handles slippage checks.
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        portfolio: Portfolio,
        config: ExecutionConfig,
    ):
        self.client = client
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.config = config
        self.stats = ExecutionStats()
        
        # Track open orders
        self._open_orders: dict[str, Order] = {}
        self._order_timestamps: dict[str, datetime] = {}
        
        # Order tracking by market and strategy
        self._orders_by_market: dict[str, list[str]] = {}
        self._orders_by_strategy: dict[str, list[str]] = {}
        
        # Signal queue
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue()
        self._processing_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info(f"ExecutionEngine initialized (dry_run={config.dry_run})")
    
    async def start(self) -> None:
        """Start the execution engine."""
        if self._running:
            return
        
        self._running = True
        self._processing_task = asyncio.create_task(
            self._process_signals(),
            name="signal_processor"
        )
        
        # Start order timeout monitor
        asyncio.create_task(self._monitor_order_timeouts(), name="order_timeout_monitor")
        
        logger.info("ExecutionEngine started")
    
    async def stop(self) -> None:
        """Stop the execution engine."""
        self._running = False
        
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all open orders
        await self.cancel_all_orders()
        
        logger.info("ExecutionEngine stopped")
    
    async def submit_signal(self, signal: Signal) -> None:
        """Submit a signal for processing."""
        await self._signal_queue.put(signal)
        logger.debug(f"Signal queued: {signal.signal_id}")
    
    async def _process_signals(self) -> None:
        """Main signal processing loop."""
        while self._running:
            try:
                # Get next signal with timeout
                try:
                    signal = await asyncio.wait_for(
                        self._signal_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                await self._execute_signal(signal)
                self.stats.signals_processed += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Signal processing error: {e}")
    
    async def _execute_signal(self, signal: Signal) -> None:
        """Execute a single trading signal."""
        logger.info(f"Executing signal: {signal.signal_id} ({signal.action})")
        
        if signal.is_place:
            await self._handle_place_orders(signal)
        elif signal.is_cancel:
            await self._handle_cancel_orders(signal)
        else:
            logger.warning(f"Unknown signal action: {signal.action}")
    
    async def _handle_place_orders(self, signal: Signal) -> None:
        """Handle a place_orders signal."""
        for order_spec in signal.orders:
            try:
                # Extract order parameters
                token_type = order_spec["token_type"]
                side = order_spec["side"]
                price = order_spec["price"]
                size = order_spec["size"]
                strategy_tag = order_spec.get("strategy_tag", "")
                
                # Check slippage if enabled
                if self.config.enable_slippage_check and signal.opportunity:
                    if not self._check_slippage(signal.opportunity, order_spec):
                        self.stats.slippage_rejections += 1
                        logger.warning(f"Order rejected due to slippage: {order_spec}")
                        continue
                
                # Check risk limits
                proposed_order = Order(
                    order_id="temp",
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                if not self.risk_manager.check_order(proposed_order):
                    self.stats.signals_rejected += 1
                    logger.warning(f"Order rejected by risk manager: {order_spec}")
                    continue
                
                # Place the order
                order = await self._place_order(
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                if order:
                    self._track_order(order)
                    self.stats.orders_placed += 1
                    self.stats.total_notional += order.notional
                    
            except Exception as e:
                logger.error(f"Failed to place order: {e}")
                self.stats.orders_rejected += 1
    
    async def _handle_cancel_orders(self, signal: Signal) -> None:
        """Handle a cancel_orders signal."""
        for order_id in signal.cancel_order_ids:
            try:
                await self.cancel_order(order_id)
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
    
    def _check_slippage(self, opportunity, order_spec: dict) -> bool:
        """
        Check if current prices have slipped too far from signal generation.
        
        Returns True if within tolerance, False if slippage exceeded.
        """
        # Compare intended price vs opportunity snapshot
        intended_price = order_spec["price"]
        side = order_spec["side"]
        token_type = order_spec["token_type"]
        
        if token_type == TokenType.YES:
            snapshot_bid = opportunity.best_bid_yes
            snapshot_ask = opportunity.best_ask_yes
        else:
            snapshot_bid = opportunity.best_bid_no
            snapshot_ask = opportunity.best_ask_no
        
        if snapshot_bid is None or snapshot_ask is None:
            return True  # Can't check, allow
        
        if side == OrderSide.BUY:
            # For buys, check if ask hasn't moved up too much
            slippage = (intended_price - snapshot_ask) / snapshot_ask if snapshot_ask > 0 else 0
        else:
            # For sells, check if bid hasn't moved down too much
            slippage = (snapshot_bid - intended_price) / snapshot_bid if snapshot_bid > 0 else 0
        
        return abs(slippage) <= self.config.slippage_tolerance
    
    async def _place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
    ) -> Optional[Order]:
        """Place an order through the API with retry logic."""
        last_error = None
        
        for attempt in range(self.config.max_retries):
            try:
                order = await self.client.place_order(
                    market_id=market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                logger.info(
                    f"Order placed: {order.order_id} | "
                    f"{side.value} {size:.2f} {token_type.value} @ {price:.4f}"
                )
                
                return order
                
            except Exception as e:
                last_error = e
                logger.warning(f"Order placement attempt {attempt + 1} failed: {e}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay)
        
        logger.error(f"Order placement failed after {self.config.max_retries} attempts: {last_error}")
        return None
    
    def _track_order(self, order: Order) -> None:
        """Add order to tracking structures."""
        self._open_orders[order.order_id] = order
        self._order_timestamps[order.order_id] = datetime.utcnow()
        
        # Track by market
        if order.market_id not in self._orders_by_market:
            self._orders_by_market[order.market_id] = []
        self._orders_by_market[order.market_id].append(order.order_id)
        
        # Track by strategy
        if order.strategy_tag:
            if order.strategy_tag not in self._orders_by_strategy:
                self._orders_by_strategy[order.strategy_tag] = []
            self._orders_by_strategy[order.strategy_tag].append(order.order_id)
    
    def _untrack_order(self, order_id: str) -> None:
        """Remove order from tracking structures."""
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            del self._open_orders[order_id]
            
            if order_id in self._order_timestamps:
                del self._order_timestamps[order_id]
            
            # Remove from market tracking
            if order.market_id in self._orders_by_market:
                if order_id in self._orders_by_market[order.market_id]:
                    self._orders_by_market[order.market_id].remove(order_id)
            
            # Remove from strategy tracking
            if order.strategy_tag and order.strategy_tag in self._orders_by_strategy:
                if order_id in self._orders_by_strategy[order.strategy_tag]:
                    self._orders_by_strategy[order.strategy_tag].remove(order_id)
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        try:
            await self.client.cancel_order(order_id)
            self._untrack_order(order_id)
            self.stats.orders_cancelled += 1
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific market."""
        if market_id:
            order_ids = list(self._orders_by_market.get(market_id, []))
        else:
            order_ids = list(self._open_orders.keys())
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        logger.info(f"Cancelled {cancelled} orders")
        return cancelled
    
    async def cancel_orders_by_strategy(self, strategy_tag: str) -> int:
        """Cancel all orders for a specific strategy."""
        order_ids = list(self._orders_by_strategy.get(strategy_tag, []))
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        return cancelled
    
    async def _monitor_order_timeouts(self) -> None:
        """Monitor and cancel orders that have timed out."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                now = datetime.utcnow()
                timeout_delta = timedelta(seconds=self.config.order_timeout_seconds)
                
                timed_out = [
                    order_id for order_id, timestamp in self._order_timestamps.items()
                    if now - timestamp > timeout_delta
                ]
                
                for order_id in timed_out:
                    logger.info(f"Order timed out: {order_id}")
                    await self.cancel_order(order_id)
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Order timeout monitor error: {e}")
    
    def handle_fill(self, trade: Trade) -> None:
        """Handle a trade fill notification."""
        order_id = trade.order_id
        
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            order.filled_size += trade.size
            order.updated_at = datetime.utcnow()
            
            if order.remaining_size <= 0:
                order.status = OrderStatus.FILLED
                self._untrack_order(order_id)
                self.stats.orders_filled += 1
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
        
        # Update portfolio
        self.portfolio.update_from_fill(trade)
        
        # Update risk manager
        self.risk_manager.update_from_fill(trade)
        
        logger.info(
            f"Fill: {trade.trade_id} | "
            f"{trade.side.value} {trade.size:.2f} {trade.token_type.value} @ {trade.price:.4f}"
        )
    
    def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders, optionally filtered by market."""
        if market_id:
            order_ids = self._orders_by_market.get(market_id, [])
            return [self._open_orders[oid] for oid in order_ids if oid in self._open_orders]
        return list(self._open_orders.values())
    
    def get_stats(self) -> ExecutionStats:
        """Get execution statistics."""
        return self.stats
    
    @property
    def open_order_count(self) -> int:
        """Get number of open orders."""
        return len(self._open_orders)

