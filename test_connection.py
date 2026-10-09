#!/usr/bin/env python3
"""
Test Polymarket API Connection
===============================

Run this BEFORE going live to verify your credentials work.

Usage:
    python3 test_connection.py
    python3 test_connection.py --config config.live.yaml
"""

import asyncio
import argparse
import sys

from utils.config_loader import load_config
from utils.logging_utils import setup_logging
from polymarket_client import PolymarketClient


async def test_connection(config_path: str = "config.live.yaml"):
    """Test the API connection and credentials."""
    print("=" * 60)
    print("🔌 Polymarket API Connection Test")
    print("=" * 60)
    
    # Load config
    try:
        config = load_config(config_path)
        print(f"✅ Config loaded from {config_path}")
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False
    
    print(f"   Mode: {config.mode.trading_mode.upper()}")
    print(f"   API Key: {config.api.api_key[:8]}..." if config.api.api_key else "   API Key: NOT SET")
    
    # Check credentials
    if config.is_live:
        if not config.api.api_key or config.api.api_key == "YOUR_API_KEY_HERE":
            print("❌ API key not configured!")
            print("   Edit config.live.yaml and add your API key")
            return False
        
        if not config.api.private_key or config.api.private_key == "YOUR_WALLET_PRIVATE_KEY_HERE":
            print("❌ Private key not configured!")
            print("   Edit config.live.yaml and add your wallet private key")
            return False
    
    print()
    print("📡 Testing API connection...")
    
    # Create client
    client = PolymarketClient(
        rest_url=config.api.polymarket_rest_url,
        ws_url=config.api.polymarket_ws_url,
        gamma_url=config.api.gamma_api_url,
        api_key=config.api.api_key,
        api_secret=config.api.api_secret,
        private_key=config.api.private_key,
        timeout=config.api.timeout_seconds,
        dry_run=config.is_dry_run,
    )
    
    try:
        await client.connect()
        print("✅ HTTP client connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False
    
    # Test Gamma API (market data)
    print()
    print("📊 Testing Gamma API (market data)...")
    try:
        markets = await client.list_markets({"limit": 5, "closed": "false"})
        print(f"✅ Gamma API working - found {len(markets)} markets")
        
        if markets:
            print("   Sample markets:")
            for m in markets[:3]:
                print(f"   - {m.question[:50]}...")
                print(f"     Volume 24h: ${m.volume_24h:,.0f} | Liquidity: ${m.liquidity:,.0f}")
    except Exception as e:
        print(f"❌ Gamma API error: {e}")
        await client.disconnect()
        return False
    
    # Test positions (requires auth)
    if config.is_live:
        print()
        print("💼 Testing authenticated endpoints...")
        try:
            positions = await client.get_positions()
            print(f"✅ Auth working - {len(positions)} positions")
        except Exception as e:
            print(f"⚠️  Could not fetch positions: {e}")
            print("   This may be normal if you have no positions yet")
    
    await client.disconnect()
    
    print()
    print("=" * 60)
    print("✅ Connection test PASSED!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Review config.live.yaml settings")
    print("2. Start with: python3 run_with_dashboard.py -c config.live.yaml")
    print("3. Monitor closely on the dashboard")
    print()
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Test Polymarket API connection")
    parser.add_argument("-c", "--config", default="config.live.yaml", help="Config file")
    args = parser.parse_args()
    
    setup_logging(console_level="WARNING")
    
    success = asyncio.run(test_connection(args.config))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

