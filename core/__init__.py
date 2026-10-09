"""
Core Trading Engine Module
===========================

Contains the main trading logic components:
- DataFeed: Real-time market data streaming
- ArbEngine: Arbitrage and spread opportunity detection
- ExecutionEngine: Order management and placement
- RiskManager: Position and loss limits
- Portfolio: Inventory and PnL tracking
"""

from core.data_feed import DataFeed
from core.arb_engine import ArbEngine
from core.execution import ExecutionEngine
from core.risk_manager import RiskManager
from core.portfolio import Portfolio

__all__ = [
    "DataFeed",
    "ArbEngine",
    "ExecutionEngine",
    "RiskManager",
    "Portfolio",
]

