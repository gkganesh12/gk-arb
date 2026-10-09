"""
Tests for the Portfolio Module
"""

import pytest
from datetime import datetime

from polymarket_client.models import OrderSide, TokenType, Trade
from core.portfolio import Portfolio, PortfolioPosition


@pytest.fixture
def portfolio() -> Portfolio:
    """Create portfolio for tests."""
    return Portfolio(initial_balance=10000.0)


def create_trade(
    market_id: str = "test_market",
    token_type: TokenType = TokenType.YES,
    side: OrderSide = OrderSide.BUY,
    price: float = 0.50,
    size: float = 100.0,
    fee: float = 0.0,
    trade_id: str = "trade_1",
    order_id: str = "order_1",
) -> Trade:
    """Helper to create test trades."""
    return Trade(
        trade_id=trade_id,
        order_id=order_id,
        market_id=market_id,
        token_type=token_type,
        side=side,
        price=price,
        size=size,
        fee=fee,
    )


class TestPositionTracking:
    """Tests for position tracking."""
    
    def test_initial_state(self, portfolio: Portfolio):
        """Test initial portfolio state."""
        assert portfolio.cash_balance == 10000.0
        assert portfolio.get_total_exposure() == 0.0
        assert portfolio.stats.total_trades == 0
    
    def test_buy_creates_position(self, portfolio: Portfolio):
        """Test buying creates a long position."""
        trade = create_trade(side=OrderSide.BUY, price=0.50, size=100.0)
        portfolio.update_from_fill(trade)
        
        position = portfolio.get_position("test_market", TokenType.YES)
        assert position is not None
        assert position.size == 100.0
        assert position.avg_entry_price == 0.50
    
    def test_sell_reduces_position(self, portfolio: Portfolio):
        """Test selling reduces position."""
        # Buy 100
        buy_trade = create_trade(side=OrderSide.BUY, price=0.50, size=100.0)
        portfolio.update_from_fill(buy_trade)
        
        # Sell 50
        sell_trade = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            side=OrderSide.SELL, 
            price=0.60, 
            size=50.0
        )
        portfolio.update_from_fill(sell_trade)
        
        position = portfolio.get_position("test_market", TokenType.YES)
        assert position.size == 50.0
    
    def test_average_price_calculation(self, portfolio: Portfolio):
        """Test average entry price calculation."""
        # Buy 100 @ 0.50
        trade1 = create_trade(side=OrderSide.BUY, price=0.50, size=100.0)
        portfolio.update_from_fill(trade1)
        
        # Buy 100 @ 0.60
        trade2 = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            side=OrderSide.BUY, 
            price=0.60, 
            size=100.0
        )
        portfolio.update_from_fill(trade2)
        
        position = portfolio.get_position("test_market", TokenType.YES)
        assert position.size == 200.0
        assert position.avg_entry_price == 0.55  # (100*0.50 + 100*0.60) / 200


class TestPnLCalculation:
    """Tests for PnL calculation."""
    
    def test_realized_pnl_on_profitable_trade(self, portfolio: Portfolio):
        """Test realized PnL on profitable trade."""
        # Buy 100 @ 0.50
        buy_trade = create_trade(side=OrderSide.BUY, price=0.50, size=100.0)
        portfolio.update_from_fill(buy_trade)
        
        # Sell 100 @ 0.60 -> $10 profit
        sell_trade = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            side=OrderSide.SELL, 
            price=0.60, 
            size=100.0
        )
        portfolio.update_from_fill(sell_trade)
        
        assert abs(portfolio.stats.total_realized_pnl - 10.0) < 0.01
        assert portfolio.stats.winning_trades == 1
    
    def test_realized_pnl_on_losing_trade(self, portfolio: Portfolio):
        """Test realized PnL on losing trade."""
        # Buy 100 @ 0.60
        buy_trade = create_trade(side=OrderSide.BUY, price=0.60, size=100.0)
        portfolio.update_from_fill(buy_trade)
        
        # Sell 100 @ 0.50 -> $10 loss
        sell_trade = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            side=OrderSide.SELL, 
            price=0.50, 
            size=100.0
        )
        portfolio.update_from_fill(sell_trade)
        
        assert abs(portfolio.stats.total_realized_pnl - (-10.0)) < 0.01
        assert portfolio.stats.losing_trades == 1
    
    def test_unrealized_pnl(self, portfolio: Portfolio):
        """Test unrealized PnL calculation."""
        # Buy 100 @ 0.50
        trade = create_trade(side=OrderSide.BUY, price=0.50, size=100.0)
        portfolio.update_from_fill(trade)
        
        # Update current prices
        portfolio.update_prices("test_market", yes_price=0.60, no_price=0.40)
        
        position = portfolio.get_position("test_market", TokenType.YES)
        assert abs(position.unrealized_pnl(0.60) - 10.0) < 0.01  # 100 * (0.60 - 0.50)
    
    def test_fee_tracking(self, portfolio: Portfolio):
        """Test fee tracking."""
        trade = create_trade(fee=0.50)
        portfolio.update_from_fill(trade)
        
        assert portfolio.stats.total_fees_paid == 0.50


class TestExposure:
    """Tests for exposure calculation."""
    
    def test_market_exposure(self, portfolio: Portfolio):
        """Test per-market exposure calculation."""
        # Buy YES
        trade1 = create_trade(
            token_type=TokenType.YES,
            side=OrderSide.BUY, 
            price=0.50, 
            size=100.0
        )
        portfolio.update_from_fill(trade1)
        
        # Buy NO
        trade2 = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            token_type=TokenType.NO,
            side=OrderSide.BUY, 
            price=0.40, 
            size=100.0
        )
        portfolio.update_from_fill(trade2)
        
        exposure = portfolio.get_exposure("test_market")
        assert exposure["yes_size"] == 100.0
        assert exposure["no_size"] == 100.0
        assert exposure["total_notional"] == 90.0  # 50 + 40
    
    def test_total_exposure(self, portfolio: Portfolio):
        """Test total exposure across markets."""
        # Position in market 1
        trade1 = create_trade(market_id="market_1", price=0.50, size=100.0)
        portfolio.update_from_fill(trade1)
        
        # Position in market 2
        trade2 = create_trade(
            trade_id="trade_2",
            order_id="order_2",
            market_id="market_2", 
            price=0.40, 
            size=100.0
        )
        portfolio.update_from_fill(trade2)
        
        assert portfolio.get_total_exposure() == 90.0  # 50 + 40


class TestWinRate:
    """Tests for win rate calculation."""
    
    def test_win_rate_calculation(self, portfolio: Portfolio):
        """Test win rate calculation."""
        # Winning trade
        portfolio.update_from_fill(create_trade(side=OrderSide.BUY, price=0.50, size=100.0))
        portfolio.update_from_fill(create_trade(
            trade_id="t2", order_id="o2",
            side=OrderSide.SELL, price=0.60, size=100.0
        ))
        
        # Losing trade
        portfolio.update_from_fill(create_trade(
            trade_id="t3", order_id="o3",
            side=OrderSide.BUY, price=0.60, size=100.0
        ))
        portfolio.update_from_fill(create_trade(
            trade_id="t4", order_id="o4",
            side=OrderSide.SELL, price=0.50, size=100.0
        ))
        
        assert portfolio.stats.winning_trades == 1
        assert portfolio.stats.losing_trades == 1
        assert portfolio.stats.win_rate == 0.5


class TestPortfolioSummary:
    """Tests for portfolio summary."""
    
    def test_summary_structure(self, portfolio: Portfolio):
        """Test summary contains expected fields."""
        summary = portfolio.get_summary()
        
        expected_keys = [
            "initial_balance",
            "cash_balance",
            "total_exposure",
            "pnl",
            "total_trades",
            "win_rate",
        ]
        
        for key in expected_keys:
            assert key in summary
    
    def test_reset(self, portfolio: Portfolio):
        """Test portfolio reset."""
        trade = create_trade()
        portfolio.update_from_fill(trade)
        
        portfolio.reset()
        
        assert portfolio.stats.total_trades == 0
        assert portfolio.get_total_exposure() == 0.0
        assert portfolio.cash_balance == 10000.0

