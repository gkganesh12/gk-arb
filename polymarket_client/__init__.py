"""
Polymarket Client Module
========================

Provides abstracted access to Polymarket's REST and WebSocket APIs.
"""

from polymarket_client.models import (
    Market,
    OrderBook,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Trade,
    PriceLevel,
)
from polymarket_client.api import PolymarketClient

__all__ = [
    "PolymarketClient",
    "Market",
    "OrderBook",
    "Order",
    "OrderSide",
    "OrderStatus",
    "Position",
    "Trade",
    "PriceLevel",
]

