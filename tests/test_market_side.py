"""
Tests for market-side pricing: fees, book parsing, depth sizing, cross-platform hedges.
"""

import asyncio

from core.arb_engine import ArbConfig, ArbEngine
from core.cross_platform_arb import CrossPlatformArbEngine, MarketMatcher, MarketPair
from core.fees import kalshi_taker_fee, polymarket_taker_fee
from kalshi_client.api import KalshiClient
from kalshi_client.models import KalshiMarket, KalshiOrderBook
from polymarket_client.api import PolymarketClient, _token_book_from_payload
from polymarket_client.models import (
    Market, MarketState, OrderBook, OrderBookSide, PriceLevel, TokenOrderBook, TokenType,
)


def book(yes_asks, no_asks, yes_bids=((0.01, 10),), no_bids=((0.01, 10),), market_id="m"):
    side = lambda levels: OrderBookSide(levels=[PriceLevel(p, s) for p, s in levels])
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(TokenType.YES, bids=side(yes_bids), asks=side(yes_asks)),
        no=TokenOrderBook(TokenType.NO, bids=side(no_bids), asks=side(no_asks)),
    )


def test_fee_curves_match_published_examples():
    # Polymarket docs: 100 shares at $0.50, crypto rate 0.07 -> $1.75
    assert abs(polymarket_taker_fee(0.5, 100, 0.07) - 1.75) < 1e-9
    assert polymarket_taker_fee(0.5, 100, 0.0) == 0.0
    # Kalshi: ceil(0.07 * C * P * (1 - P)) to the cent
    assert kalshi_taker_fee(0.5, 100) == 1.75
    assert kalshi_taker_fee(0.5, 1) == 0.02


def test_polymarket_book_sorted_best_first():
    # Live CLOB order: bids ascending, asks descending (best price last)
    payload = {
        "bids": [{"price": "0.01", "size": "100"}, {"price": "0.13", "size": "50"}],
        "asks": [{"price": "0.99", "size": "100"}, {"price": "0.14", "size": "50"}],
    }
    token_book = _token_book_from_payload(payload, TokenType.YES)
    assert token_book.best_bid == 0.13
    assert token_book.best_ask == 0.14


def test_polymarket_market_fee_schedule_parsed():
    client = PolymarketClient()
    market = client._parse_market({
        "id": "1", "conditionId": "c", "question": "Q?",
        "clobTokenIds": '["a", "b"]', "outcomes": '["Yes", "No"]',
        "feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1},
        "acceptingOrders": True, "orderMinSize": 5,
    })
    assert market.fee_rate == 0.04 and market.min_order_size == 5
    assert market.has_binary_outcomes
    free = client._parse_market({"id": "2", "feesEnabled": False, "acceptingOrders": False})
    assert free.fee_rate == 0.0 and not free.accepting_orders


def test_kalshi_dollar_fields_and_orderbook_fp():
    client = KalshiClient()
    market = client._parse_market({
        "ticker": "T", "title": "Who wins?", "yes_sub_title": "Celtics", "status": "active",
        "yes_bid_dollars": "0.4500", "yes_ask_dollars": "0.4700", "volume_fp": "1200.00",
    })
    assert market.yes_bid == 0.45 and market.volume == 1200.0
    assert market.match_text == "Who wins? Celtics"
    assert client._parse_market({"ticker": "P", "mve_collection_ticker": "X"}) is None

    async def fake_get(endpoint, params=None):
        return {"orderbook_fp": {
            "yes_dollars": [["0.0500", "40.00"], ["0.0900", "1141.35"]],
            "no_dollars": [["0.8500", "1050.00"], ["0.8900", "200.27"]],
        }}
    client._get = fake_get
    ob = asyncio.run(client.get_orderbook("T"))
    assert ob.best_bid_yes == 0.09 and ob.best_bid_no == 0.89
    assert abs(ob.best_ask_yes - 0.11) < 1e-9


def test_bundle_uses_market_fee_curve_and_depth():
    engine = ArbEngine(ArbConfig(min_edge=0.01, mm_enabled=False, default_order_size=1000,
                                 min_order_size=1, gas_cost_per_order=0, slippage_tolerance=0.1))
    ob = book(yes_asks=[(0.45, 50), (0.47, 100)], no_asks=[(0.50, 200)])
    fee_free = Market("m", "m", "Q", fee_rate=0.0)
    signals = engine.analyze(MarketState(market=fee_free, order_book=ob))
    assert len(signals) == 1
    opp = signals[0].opportunity
    assert opp.suggested_size == 150  # walked past the top level
    assert [o["price"] for o in signals[0].orders] == [0.47, 0.50]

    # Same prices with a 1% edge, but unknown fee schedule -> 1.5% fallback kills it
    engine = ArbEngine(ArbConfig(min_edge=0.005, mm_enabled=False, taker_fee_bps=150, gas_cost_per_order=0))
    ob = book(yes_asks=[(0.49, 100)], no_asks=[(0.50, 100)])
    assert engine.analyze(MarketState(market=Market("m", "m", "Q"), order_book=ob)) == []
    assert engine.analyze(MarketState(market=Market("m", "m", "Q", fee_rate=0.0), order_book=ob))


def test_cross_platform_hedge_detected_after_fees():
    pair = MarketPair("p1", "K1", "Q", "Q", 0.9)
    engine = CrossPlatformArbEngine(min_edge=0.02, max_contracts=50)
    poly = book(yes_asks=[(0.40, 100)], no_asks=[(0.62, 100)])
    kalshi = KalshiOrderBook("K1", yes_bids=[PriceLevel(0.52, 100)], no_bids=[PriceLevel(0.30, 100)]).to_unified_orderbook()
    opp = engine.check_arbitrage(pair, poly, kalshi, polymarket_market=Market("p1", "p1", "Q", fee_rate=0.0))
    # YES on Polymarket @0.40 + NO on Kalshi @0.48 = 0.88 before Kalshi's fee
    assert opp.yes_platform == "polymarket" and opp.no_platform == "kalshi"
    assert opp.size == 50 and 0.09 < opp.net_edge < 0.12
    # Cooldown: same hedge isn't re-reported on the next poll
    assert engine.check_arbitrage(pair, poly, kalshi, polymarket_market=Market("p1", "p1", "Q", fee_rate=0.0)) is None

    fair = KalshiOrderBook("K1", yes_bids=[PriceLevel(0.39, 100)], no_bids=[PriceLevel(0.59, 100)]).to_unified_orderbook()
    assert CrossPlatformArbEngine().check_arbitrage(pair, poly, fair) is None


def test_outcome_alignment_and_dates():
    matcher = MarketMatcher()
    lakers_celtics = Market("p", "p", "Lakers vs Celtics", outcomes=["Lakers", "Celtics"])
    celtics = KalshiMarket("K", "E", "S", "Lakers vs Celtics winner?", yes_sub_title="Celtics")
    assert matcher.align_outcomes(lakers_celtics, celtics) is True
    unclear = KalshiMarket("K", "E", "S", "Total points?", yes_sub_title="Over 210.5")
    assert matcher.align_outcomes(lakers_celtics, unclear) is None
    assert matcher.dates_match(matcher.extract_date("Dec 8"), matcher.extract_date("Dec 8, 2026"))
    assert not matcher.dates_match(matcher.extract_date("Dec 8"), matcher.extract_date("Dec 9"))
