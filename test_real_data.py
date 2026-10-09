#!/usr/bin/env python3
"""Quick test to verify we can fetch REAL Polymarket data."""

import asyncio
from polymarket_client import PolymarketClient


async def test():
    print("=" * 60)
    print("Testing REAL Polymarket Data Fetch")
    print("=" * 60)
    
    client = PolymarketClient(dry_run=True)
    await client.connect()
    
    print("\n1. Fetching markets from Gamma API...")
    try:
        markets = await client.list_markets({"limit": 5, "closed": "false"})
        print(f"   [OK] Found {len(markets)} markets\n")
        
        for i, m in enumerate(markets[:3], 1):
            print(f"   Market {i}: {m.question[:55]}...")
            print(f"      ID: {m.market_id}")
            print(f"      YES Token: {m.yes_token_id[:40]}..." if m.yes_token_id else "      YES Token: Not available")
            print(f"      Volume 24h: ${m.volume_24h:,.0f}")
            print()
            
    except Exception as e:
        print(f"   [ERROR] {e}")
    
    # Try to get real order book
    if markets and markets[0].yes_token_id:
        print("2. Fetching REAL order book from CLOB API...")
        try:
            market = markets[0]
            orderbook = await client.get_orderbook(market.market_id)
            
            print(f"   Market: {market.question[:50]}...")
            print(f"   YES Best Bid: {orderbook.best_bid_yes}")
            print(f"   YES Best Ask: {orderbook.best_ask_yes}")
            print(f"   NO Best Bid: {orderbook.best_bid_no}")
            print(f"   NO Best Ask: {orderbook.best_ask_no}")
            
            if orderbook.total_ask:
                print(f"\n   Bundle Ask (YES+NO): ${orderbook.total_ask:.4f}")
                print(f"   Bundle Bid (YES+NO): ${orderbook.total_bid:.4f}")
                edge = 1.0 - orderbook.total_ask if orderbook.total_ask else 0
                print(f"   Potential Edge: {edge*100:.2f}%")
        except Exception as e:
            print(f"   [WARN] Order book error: {e}")
            print("   (Order book API may require different token format)")
    
    await client.disconnect()
    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(test())

