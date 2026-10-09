"""
Order Book Depth
================

Sizes two-leg trades (buy YES + buy NO, sell YES + sell NO, or a hedged
cross-venue pair) by walking both books together, so size reflects how much
depth stays profitable rather than just the top level.
"""

from dataclasses import dataclass
from typing import Callable

from polymarket_client.models import PriceLevel


# (price_a, price_b) -> (gross edge per unit, fee on leg A per unit, fee on leg B per unit)
UnitEconomics = Callable[[float, float], tuple[float, float, float]]

_EPSILON = 1e-9


@dataclass
class TwoLegFill:
    """Result of walking two books in lock-step."""
    size: float = 0.0        # Units per leg
    notional_a: float = 0.0  # Sum of price * qty on leg A
    notional_b: float = 0.0
    gross_edge: float = 0.0  # Total gross edge across all units
    fees_a: float = 0.0      # Total estimated fees on leg A
    fees_b: float = 0.0
    limit_a: float = 0.0     # Worst price touched on leg A (use as the limit price)
    limit_b: float = 0.0

    @property
    def avg_price_a(self) -> float:
        return self.notional_a / self.size if self.size else 0.0

    @property
    def avg_price_b(self) -> float:
        return self.notional_b / self.size if self.size else 0.0

    @property
    def fees(self) -> float:
        return self.fees_a + self.fees_b

    @property
    def net_edge_per_unit(self) -> float:
        if not self.size:
            return 0.0
        return (self.gross_edge - self.fees) / self.size


def walk_two_legs(
    levels_a: list[PriceLevel],
    levels_b: list[PriceLevel],
    unit_economics: UnitEconomics,
    min_edge: float,
    max_size: float,
) -> TwoLegFill:
    """
    Consume both books level by level while the marginal unit still clears
    `min_edge` after fees, up to `max_size` units per leg.

    Levels must be sorted best-first (bids descending, asks ascending).
    """
    fill = TwoLegFill()
    if not levels_a or not levels_b or max_size <= 0:
        return fill

    i = j = 0
    remaining_a = levels_a[0].size
    remaining_b = levels_b[0].size

    while i < len(levels_a) and j < len(levels_b) and fill.size < max_size - _EPSILON:
        price_a = levels_a[i].price
        price_b = levels_b[j].price
        gross, fee_a, fee_b = unit_economics(price_a, price_b)
        if gross - fee_a - fee_b < min_edge:
            break

        qty = min(remaining_a, remaining_b, max_size - fill.size)
        if qty > 0:
            fill.size += qty
            fill.notional_a += price_a * qty
            fill.notional_b += price_b * qty
            fill.gross_edge += gross * qty
            fill.fees_a += fee_a * qty
            fill.fees_b += fee_b * qty
            fill.limit_a = price_a
            fill.limit_b = price_b

        remaining_a -= qty
        remaining_b -= qty
        if remaining_a <= _EPSILON:
            i += 1
            remaining_a = levels_a[i].size if i < len(levels_a) else 0.0
        if remaining_b <= _EPSILON:
            j += 1
            remaining_b = levels_b[j].size if j < len(levels_b) else 0.0

    return fill
