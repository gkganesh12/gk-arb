"""
Logging Utilities
==================

Configures structured logging for the trading bot.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# Custom log levels
TRADE = 25  # Between INFO and WARNING
OPPORTUNITY = 26


def setup_logging(
    log_dir: str = "logs",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    main_log_file: str = "bot.log",
    trades_log_file: str = "trades.log",
    opportunities_log_file: str = "opportunities.log",
    max_size_mb: int = 50,
    backup_count: int = 5,
) -> None:
    """
    Set up logging for the trading bot.
    
    Creates:
    - Console handler for key events
    - Main log file for all events
    - Trades log file for trade-specific events
    - Opportunities log file for detected opportunities
    """
    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Register custom levels
    logging.addLevelName(TRADE, "TRADE")
    logging.addLevelName(OPPORTUNITY, "OPPORTUNITY")
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler with UTF-8 encoding for Windows
    import io
    # Wrap stdout with UTF-8 encoding to handle international characters
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level.upper()))
    # Use ASCII pipe character for Windows compatibility
    console_handler.setFormatter(ColoredFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S"
    ))
    root_logger.addHandler(console_handler)
    
    # Main log file handler
    main_handler = RotatingFileHandler(
        log_path / main_log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    main_handler.setLevel(getattr(logging, file_level.upper()))
    main_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(funcName)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(main_handler)
    
    # Trades log file handler
    trades_logger = logging.getLogger("trades")
    trades_handler = RotatingFileHandler(
        log_path / trades_log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    trades_handler.setLevel(logging.DEBUG)
    trades_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S.%f"
    ))
    trades_logger.addHandler(trades_handler)
    trades_logger.propagate = True
    
    # Opportunities log file handler
    opps_logger = logging.getLogger("opportunities")
    opps_handler = RotatingFileHandler(
        log_path / opportunities_log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    opps_handler.setLevel(logging.DEBUG)
    opps_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S.%f"
    ))
    opps_logger.addHandler(opps_handler)
    opps_logger.propagate = True
    
    # Reduce noise from external libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    
    logging.info(f"Logging initialized | console={console_level} | file={file_level} | dir={log_dir}")


class ColoredFormatter(logging.Formatter):
    """Formatter that adds colors to console output."""
    
    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
        "TRADE": "\033[34m",     # Blue
        "OPPORTUNITY": "\033[96m",  # Light cyan
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        # Add color to level name
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        
        # Sanitize message - replace non-ASCII chars to avoid Windows encoding issues
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            record.msg = record.msg.encode('ascii', 'replace').decode('ascii')
        
        return super().format(record)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name."""
    return logging.getLogger(name)


class TradeLogger:
    """Specialized logger for trade events."""
    
    def __init__(self):
        self.logger = logging.getLogger("trades")
    
    def log_order_placed(
        self,
        order_id: str,
        market_id: str,
        side: str,
        token: str,
        price: float,
        size: float,
        strategy: str = "",
    ) -> None:
        """Log an order placement."""
        self.logger.log(
            TRADE,
            f"ORDER_PLACED | id={order_id} | market={market_id} | "
            f"{side} {size:.4f} {token} @ {price:.4f} | strategy={strategy}"
        )
    
    def log_order_filled(
        self,
        trade_id: str,
        order_id: str,
        market_id: str,
        side: str,
        token: str,
        price: float,
        size: float,
        fee: float = 0.0,
    ) -> None:
        """Log an order fill."""
        self.logger.log(
            TRADE,
            f"ORDER_FILLED | trade={trade_id} | order={order_id} | market={market_id} | "
            f"{side} {size:.4f} {token} @ {price:.4f} | fee={fee:.4f}"
        )
    
    def log_order_cancelled(self, order_id: str, reason: str = "") -> None:
        """Log an order cancellation."""
        self.logger.log(
            TRADE,
            f"ORDER_CANCELLED | id={order_id} | reason={reason}"
        )


class OpportunityLogger:
    """Specialized logger for opportunity events."""
    
    def __init__(self):
        self.logger = logging.getLogger("opportunities")
    
    def log_bundle_opportunity(
        self,
        opportunity_id: str,
        market_id: str,
        opportunity_type: str,
        edge: float,
        total_price: float,
        suggested_size: float,
    ) -> None:
        """Log a bundle arbitrage opportunity."""
        self.logger.log(
            OPPORTUNITY,
            f"BUNDLE_ARB | id={opportunity_id} | market={market_id} | "
            f"type={opportunity_type} | edge={edge:.4f} | total={total_price:.4f} | "
            f"size={suggested_size:.2f}"
        )
    
    def log_mm_opportunity(
        self,
        opportunity_id: str,
        market_id: str,
        token: str,
        spread: float,
        bid: float,
        ask: float,
        suggested_size: float,
    ) -> None:
        """Log a market-making opportunity."""
        self.logger.log(
            OPPORTUNITY,
            f"MM_SPREAD | id={opportunity_id} | market={market_id} | token={token} | "
            f"spread={spread:.4f} | bid={bid:.4f} | ask={ask:.4f} | size={suggested_size:.2f}"
        )


class PerformanceLogger:
    """Logger for performance metrics."""
    
    def __init__(self):
        self.logger = logging.getLogger("performance")
    
    def log_snapshot(
        self,
        pnl: dict,
        exposure: float,
        positions: int,
        open_orders: int,
    ) -> None:
        """Log a portfolio snapshot."""
        self.logger.info(
            f"SNAPSHOT | realized={pnl.get('realized_pnl', 0):.2f} | "
            f"unrealized={pnl.get('unrealized_pnl', 0):.2f} | "
            f"total={pnl.get('total_pnl', 0):.2f} | "
            f"exposure={exposure:.2f} | positions={positions} | orders={open_orders}"
        )
    
    def log_latency(self, operation: str, latency_ms: float) -> None:
        """Log operation latency."""
        self.logger.debug(f"LATENCY | {operation} | {latency_ms:.2f}ms")


# Global instances
trade_logger = TradeLogger()
opportunity_logger = OpportunityLogger()
performance_logger = PerformanceLogger()

