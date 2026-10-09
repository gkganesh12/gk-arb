"""
Tests for the Arbitrage Engine
"""

import pytest
from datetime import datetime

from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
    OpportunityType,
)
from core.arb_engine import ArbEngine, ArbConfig


@pytest.fixture
def arb_config() -> ArbConfig:
    """Default arbitrage configuration for tests."""
    return ArbConfig(
        min_edge=0.01,
        bundle_arb_enabled=True,
        min_spread=0.05,
        mm_enabled=True,
        tick_size=0.01,
        default_order_size=50.0,
        # Set fees to 0 for testing (easier to verify edge calculations)
        maker_fee_bps=0,
        taker_fee_bps=0,
        gas_cost_per_order=0,
    )


@pytest.fixture
def arb_engine(arb_config: ArbConfig) -> ArbEngine:
    """Create arbitrage engine for tests."""
    return ArbEngine(arb_config)


def create_order_book(
    market_id: str,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
    size: float = 100.0,
) -> OrderBook:
    """Helper to create an order book with given prices."""
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=[PriceLevel(price=yes_bid, size=size)]),
            asks=OrderBookSide(levels=[PriceLevel(price=yes_ask, size=size)]),
        ),
        no=TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=[PriceLevel(price=no_bid, size=size)]),
            asks=OrderBookSide(levels=[PriceLevel(price=no_ask, size=size)]),
        ),
    )


def create_market_state(order_book: OrderBook) -> MarketState:
    """Helper to create a market state."""
    return MarketState(
        market=Market(
            market_id=order_book.market_id,
            condition_id=order_book.market_id,
            question="Test Market",
            active=True,
            volume_24h=50000.0,
        ),
        order_book=order_book,
    )


class TestBundleArbitrage:
    """Tests for bundle arbitrage detection."""
    
    def test_detect_bundle_long_opportunity(self, arb_engine: ArbEngine):
        """Test detection of bundle long (buy YES + NO for < $1)."""
        # YES ask = 0.45, NO ask = 0.50 -> total = 0.95 (5% edge)
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.43,
            yes_ask=0.45,
            no_bid=0.48,
            no_ask=0.50,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        assert len(signals) >= 1
        
        # Find bundle signal
        bundle_signals = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_signals) == 1
        
        signal = bundle_signals[0]
        assert signal.opportunity.opportunity_type == OpportunityType.BUNDLE_LONG
        assert signal.opportunity.edge >= 0.04  # At least 4% edge
        assert len(signal.orders) == 2  # Both YES and NO orders
    
    def test_detect_bundle_short_opportunity(self, arb_engine: ArbEngine):
        """Test detection of bundle short (sell YES + NO for > $1)."""
        # YES bid = 0.55, NO bid = 0.50 -> total = 1.05 (5% edge)
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.55,
            yes_ask=0.57,
            no_bid=0.50,
            no_ask=0.52,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        bundle_signals = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_signals) == 1
        
        signal = bundle_signals[0]
        assert signal.opportunity.opportunity_type == OpportunityType.BUNDLE_SHORT
        assert signal.opportunity.edge >= 0.04
    
    def test_no_opportunity_when_fair(self, arb_engine: ArbEngine):
        """Test no bundle opportunity when prices are fair."""
        # YES ask = 0.50, NO ask = 0.50 -> total = 1.00 (no edge)
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.48,
            yes_ask=0.50,
            no_bid=0.48,
            no_ask=0.50,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        bundle_signals = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_signals) == 0
    
    def test_edge_below_threshold(self, arb_engine: ArbEngine):
        """Test no opportunity when edge is below min_edge."""
        # Total ask = 0.995 -> only 0.5% edge, below 1% threshold
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.48,
            yes_ask=0.50,
            no_bid=0.48,
            no_ask=0.495,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        bundle_signals = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_signals) == 0


class TestMarketMaking:
    """Tests for market-making opportunity detection."""
    
    def test_detect_mm_opportunity_wide_spread(self, arb_engine: ArbEngine):
        """Test detection of MM opportunity with wide spread."""
        # YES spread = 0.10 (10%) - above min_spread of 5%
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.45,
            yes_ask=0.55,
            no_bid=0.40,
            no_ask=0.50,  # 10% spread on NO as well
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        mm_signals = [s for s in signals if s.opportunity and s.opportunity.is_market_making]
        assert len(mm_signals) >= 1
    
    def test_no_mm_opportunity_tight_spread(self, arb_engine: ArbEngine):
        """Test no MM opportunity with tight spread."""
        # YES spread = 0.02 (2%) - below min_spread of 5%
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.49,
            yes_ask=0.51,
            no_bid=0.48,
            no_ask=0.50,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        # Filter out any bundle opportunities (they might exist due to mispricing)
        mm_signals = [s for s in signals if s.opportunity and s.opportunity.is_market_making]
        assert len(mm_signals) == 0


class TestSignalGeneration:
    """Tests for signal generation."""
    
    def test_signal_contains_correct_orders(self, arb_engine: ArbEngine):
        """Test that signals contain properly structured orders."""
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.43,
            yes_ask=0.45,
            no_bid=0.48,
            no_ask=0.50,
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        for signal in signals:
            assert signal.signal_id is not None
            assert signal.action in ("place_orders", "cancel_orders")
            assert signal.market_id == "test_market"
            
            for order in signal.orders:
                assert "token_type" in order
                assert "side" in order
                assert "price" in order
                assert "size" in order
    
    def test_statistics_tracking(self, arb_engine: ArbEngine):
        """Test that engine tracks statistics correctly."""
        initial_stats = arb_engine.get_stats()
        assert initial_stats.bundle_opportunities_detected == 0
        
        # Generate an opportunity
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.43,
            yes_ask=0.45,
            no_bid=0.48,
            no_ask=0.50,
        )
        state = create_market_state(order_book)
        arb_engine.analyze(state)
        
        updated_stats = arb_engine.get_stats()
        assert updated_stats.bundle_opportunities_detected >= 1
        assert updated_stats.signals_generated >= 1


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_missing_prices(self, arb_engine: ArbEngine):
        """Test handling of missing prices in order book."""
        order_book = OrderBook(
            market_id="test_market",
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=[]),  # Empty
                asks=OrderBookSide(levels=[]),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=[]),
                asks=OrderBookSide(levels=[]),
            ),
        )
        
        state = create_market_state(order_book)
        signals = arb_engine.analyze(state)
        
        # Should handle gracefully without signals
        bundle_signals = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_signals) == 0
    
    def test_extreme_prices(self, arb_engine: ArbEngine):
        """Test handling of extreme price values."""
        order_book = create_order_book(
            market_id="test_market",
            yes_bid=0.01,
            yes_ask=0.02,
            no_bid=0.01,
            no_ask=0.02,
        )
        
        state = create_market_state(order_book)
        # Should not crash
        signals = arb_engine.analyze(state)
        assert isinstance(signals, list)

