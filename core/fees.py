"""
Fee Models
==========

Taker fee curves for the venues GK Arb scans. Both venues charge fees
proportional to p * (1 - p), so fees peak at 50c and shrink toward 0c/100c.

- Polymarket: fee = C * rate * (p * (1 - p)) ** exponent, in USDC, takers only.
  `rate` and `exponent` come from each market's Gamma `feeSchedule`
  (many markets have fees disabled entirely).
- Kalshi: fee = round_up_to_cent(0.07 * multiplier * C * P * (1 - P)) per order.
"""

import math


KALSHI_TAKER_RATE = 0.07


def polymarket_fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket taker fee for one share at `price`."""
    if rate <= 0 or not 0.0 < price < 1.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def polymarket_taker_fee(price: float, size: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket taker fee for `size` shares at `price`."""
    return size * polymarket_fee_per_share(price, rate, exponent)


def kalshi_fee_per_contract(price: float, multiplier: float = 1.0) -> float:
    """Unrounded Kalshi taker fee for one contract (use for marginal edge math)."""
    if not 0.0 < price < 1.0:
        return 0.0
    return KALSHI_TAKER_RATE * multiplier * price * (1.0 - price)


def kalshi_taker_fee(price: float, contracts: float, multiplier: float = 1.0) -> float:
    """Kalshi taker fee for one order, rounded up to the next cent."""
    raw_cents = contracts * kalshi_fee_per_contract(price, multiplier) * 100
    # round() first so float noise like 7.0000000001 doesn't bump a whole cent
    return math.ceil(round(raw_cents, 6)) / 100
