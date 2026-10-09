"""
Risk Manager Module
====================

Enforces position limits, loss limits, and other risk constraints.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Set

from polymarket_client.models import Order, OrderSide, TokenType, Trade


logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """Configuration for risk management."""
    # Position limits
    max_position_per_market: float = 200.0  # Max notional per market
    max_global_exposure: float = 5000.0  # Max total exposure
    
    # Loss limits
    max_daily_loss: float = 500.0
    max_drawdown_pct: float = 0.10  # 10% max drawdown from peak
    
    # Market filters
    trade_only_high_volume: bool = True
    min_24h_volume: float = 10000.0
    
    # Whitelist/blacklist
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    
    # Kill switch
    kill_switch_enabled: bool = True
    auto_unwind_on_breach: bool = False


@dataclass
class RiskState:
    """Current risk state."""
    daily_pnl: float = 0.0
    peak_pnl: float = 0.0
    current_drawdown: float = 0.0
    global_exposure: float = 0.0
    kill_switch_triggered: bool = False
    kill_switch_reason: str = ""
    last_check: datetime = field(default_factory=datetime.utcnow)


class RiskManager:
    """
    Risk management system.
    
    Validates orders against risk limits and monitors overall exposure.
    Can trigger a kill switch to stop all trading.
    """
    
    def __init__(self, config: RiskConfig):
        self.config = config
        self.state = RiskState()
        
        # Per-market exposure tracking
        self._market_exposure: dict[str, float] = {}
        
        # Volume cache
        self._market_volumes: dict[str, float] = {}
        
        # Trading session tracking
        self._session_start = datetime.utcnow()
        self._session_trades: list[Trade] = []
        
        logger.info(
            f"RiskManager initialized | "
            f"max_per_market={config.max_position_per_market} | "
            f"max_global={config.max_global_exposure} | "
            f"max_daily_loss={config.max_daily_loss}"
        )
    
    def check_order(self, order: Order) -> bool:
        """
        Check if an order passes all risk checks.
        
        Returns True if the order is allowed, False otherwise.
        """
        # Kill switch check
        if self.state.kill_switch_triggered:
            logger.warning(f"Order rejected: kill switch triggered ({self.state.kill_switch_reason})")
            return False
        
        # Market blacklist check
        if order.market_id in self.config.blacklist:
            logger.warning(f"Order rejected: market {order.market_id} is blacklisted")
            return False
        
        # Whitelist check (if whitelist is non-empty)
        if self.config.whitelist and order.market_id not in self.config.whitelist:
            logger.warning(f"Order rejected: market {order.market_id} not in whitelist")
            return False
        
        # Volume check
        if self.config.trade_only_high_volume:
            market_volume = self._market_volumes.get(order.market_id, 0)
            if market_volume < self.config.min_24h_volume:
                logger.warning(
                    f"Order rejected: market {order.market_id} volume "
                    f"({market_volume:.0f}) below minimum ({self.config.min_24h_volume})"
                )
                return False
        
        # Per-market exposure check
        current_market_exposure = self._market_exposure.get(order.market_id, 0)
        new_exposure = order.notional if order.side == OrderSide.BUY else -order.notional
        projected_exposure = abs(current_market_exposure + new_exposure)
        
        if projected_exposure > self.config.max_position_per_market:
            logger.warning(
                f"Order rejected: would exceed market limit | "
                f"current={current_market_exposure:.2f} + order={new_exposure:.2f} = "
                f"{projected_exposure:.2f} > {self.config.max_position_per_market}"
            )
            return False
        
        # Global exposure check
        projected_global = self.state.global_exposure + abs(new_exposure)
        if projected_global > self.config.max_global_exposure:
            logger.warning(
                f"Order rejected: would exceed global limit | "
                f"current={self.state.global_exposure:.2f} + order={abs(new_exposure):.2f} = "
                f"{projected_global:.2f} > {self.config.max_global_exposure}"
            )
            return False
        
        # Daily loss check
        if self.state.daily_pnl < -self.config.max_daily_loss:
            logger.warning(
                f"Order rejected: daily loss limit exceeded | "
                f"daily_pnl={self.state.daily_pnl:.2f} < -{self.config.max_daily_loss}"
            )
            if self.config.kill_switch_enabled:
                self._trigger_kill_switch("Daily loss limit exceeded")
            return False
        
        # Drawdown check
        if self.state.current_drawdown > self.config.max_drawdown_pct:
            logger.warning(
                f"Order rejected: drawdown limit exceeded | "
                f"drawdown={self.state.current_drawdown:.2%} > {self.config.max_drawdown_pct:.2%}"
            )
            if self.config.kill_switch_enabled:
                self._trigger_kill_switch("Drawdown limit exceeded")
            return False
        
        return True
    
    def update_position(
        self,
        market_id: str,
        token_type: TokenType,
        size_delta: float,
        price: float
    ) -> None:
        """Update position tracking after a trade."""
        notional_change = abs(size_delta * price)
        
        if market_id not in self._market_exposure:
            self._market_exposure[market_id] = 0.0
        
        if size_delta > 0:
            self._market_exposure[market_id] += notional_change
            self.state.global_exposure += notional_change
        else:
            self._market_exposure[market_id] -= notional_change
            self.state.global_exposure -= notional_change
        
        # Ensure non-negative
        self._market_exposure[market_id] = max(0, self._market_exposure[market_id])
        self.state.global_exposure = max(0, self.state.global_exposure)
        
        self.state.last_check = datetime.utcnow()
    
    def update_from_fill(self, trade: Trade) -> None:
        """Update risk state from a trade fill."""
        size_delta = trade.size if trade.side == OrderSide.BUY else -trade.size
        self.update_position(trade.market_id, trade.token_type, size_delta, trade.price)
        self._session_trades.append(trade)
    
    def update_pnl(self, realized_pnl: float, unrealized_pnl: float) -> None:
        """Update PnL tracking."""
        total_pnl = realized_pnl + unrealized_pnl
        self.state.daily_pnl = total_pnl
        
        # Update peak and drawdown
        if total_pnl > self.state.peak_pnl:
            self.state.peak_pnl = total_pnl
        
        if self.state.peak_pnl > 0:
            self.state.current_drawdown = (self.state.peak_pnl - total_pnl) / self.state.peak_pnl
        else:
            self.state.current_drawdown = 0.0
        
        # Check for limit breaches
        if total_pnl < -self.config.max_daily_loss:
            if self.config.kill_switch_enabled and not self.state.kill_switch_triggered:
                self._trigger_kill_switch("Daily loss limit exceeded")
        
        if self.state.current_drawdown > self.config.max_drawdown_pct:
            if self.config.kill_switch_enabled and not self.state.kill_switch_triggered:
                self._trigger_kill_switch("Drawdown limit exceeded")
    
    def update_market_volume(self, market_id: str, volume_24h: float) -> None:
        """Update cached 24h volume for a market."""
        self._market_volumes[market_id] = volume_24h
    
    def set_market_volumes(self, volumes: dict[str, float]) -> None:
        """Bulk update market volumes."""
        self._market_volumes.update(volumes)
    
    def _trigger_kill_switch(self, reason: str) -> None:
        """Trigger the kill switch to stop trading."""
        self.state.kill_switch_triggered = True
        self.state.kill_switch_reason = reason
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")
    
    def reset_kill_switch(self) -> None:
        """Reset the kill switch (use with caution)."""
        self.state.kill_switch_triggered = False
        self.state.kill_switch_reason = ""
        logger.warning("Kill switch reset")
    
    def within_global_limits(self) -> bool:
        """Check if currently within global risk limits."""
        if self.state.kill_switch_triggered:
            return False
        if self.state.daily_pnl < -self.config.max_daily_loss:
            return False
        if self.state.current_drawdown > self.config.max_drawdown_pct:
            return False
        if self.state.global_exposure > self.config.max_global_exposure:
            return False
        return True
    
    def get_market_exposure(self, market_id: str) -> float:
        """Get current exposure for a market."""
        return self._market_exposure.get(market_id, 0.0)
    
    def get_available_exposure(self, market_id: str) -> float:
        """Get remaining available exposure for a market."""
        current = self._market_exposure.get(market_id, 0.0)
        return max(0, self.config.max_position_per_market - current)
    
    def get_global_available(self) -> float:
        """Get remaining global exposure capacity."""
        return max(0, self.config.max_global_exposure - self.state.global_exposure)
    
    def reset_daily_stats(self) -> None:
        """Reset daily statistics (call at start of trading day)."""
        self.state.daily_pnl = 0.0
        self.state.peak_pnl = 0.0
        self.state.current_drawdown = 0.0
        self._session_start = datetime.utcnow()
        self._session_trades = []
        logger.info("Daily stats reset")
    
    def get_summary(self) -> dict:
        """Get a summary of current risk state."""
        return {
            "global_exposure": self.state.global_exposure,
            "max_global_exposure": self.config.max_global_exposure,
            "utilization_pct": (self.state.global_exposure / self.config.max_global_exposure * 100
                               if self.config.max_global_exposure > 0 else 0),
            "daily_pnl": self.state.daily_pnl,
            "max_daily_loss": self.config.max_daily_loss,
            "peak_pnl": self.state.peak_pnl,
            "current_drawdown_pct": self.state.current_drawdown * 100,
            "max_drawdown_pct": self.config.max_drawdown_pct * 100,
            "kill_switch_triggered": self.state.kill_switch_triggered,
            "kill_switch_reason": self.state.kill_switch_reason,
            "markets_with_exposure": len([m for m, e in self._market_exposure.items() if e > 0]),
            "session_trade_count": len(self._session_trades),
            "within_limits": self.within_global_limits(),
        }
    
    def add_to_blacklist(self, market_id: str) -> None:
        """Add a market to the blacklist."""
        if market_id not in self.config.blacklist:
            self.config.blacklist.append(market_id)
            logger.info(f"Market {market_id} added to blacklist")
    
    def remove_from_blacklist(self, market_id: str) -> None:
        """Remove a market from the blacklist."""
        if market_id in self.config.blacklist:
            self.config.blacklist.remove(market_id)
            logger.info(f"Market {market_id} removed from blacklist")

