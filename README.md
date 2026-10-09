# GK Arb

Arbitrage scanner for **Polymarket** and **Kalshi** prediction markets, with a live web dashboard.

**Author:** gkganesh12 · **License:** MIT

## Strategies

| Strategy | Trade | Profitable when |
|---|---|---|
| Bundle arb (Polymarket) | Buy YES + NO (or sell both) | `ask_yes + ask_no + fees < $1 - min_edge` |
| Cross-platform hedge | Buy YES on one venue + NO on the other | `yes + no + both venues' fees < $1 - min_edge` |
| Market making (off by default) | Quote inside wide spreads | spread ≥ `min_spread` |

A hedged pair pays exactly $1 whichever way the market resolves, so the edge is locked in at entry.

## Market model

- **Real fee curves.** Polymarket fees come from each market's own `feeSchedule` (`fee = shares × rate × p × (1 − p)`; many markets are fee-free). Kalshi uses `ceil(0.07 × C × P × (1 − P))`, rounded up to the cent per order.
- **Depth-aware sizing.** Both legs are walked level by level while each extra unit still clears `min_edge` after fees, within `slippage_tolerance` of the top of book. Limit prices are the deepest level touched.
- **Batched books.** Polymarket books are fetched with `POST /books` (20 markets per request) instead of two requests per market.
- **Outcome alignment.** For non-Yes/No Polymarket markets (e.g. `["Lakers", "Celtics"]`), the token that matches Kalshi's YES outcome is resolved explicitly, and pairs that can't be aligned are skipped.
- **Current Kalshi API.** Parses `*_dollars` prices and `orderbook_fp` books, and excludes multivariate parlay markets.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python run_with_dashboard.py      # bot + dashboard at http://localhost:8888
python main.py                    # bot only
pytest tests/ -q                  # tests
```

Defaults are `dry_run` mode with real market data. Set `data_mode: "simulation"` in `config.yaml` for demo data.

## Key settings (`config.yaml`)

| Key | Meaning | Default |
|---|---|---|
| `trading.min_edge` | Min net edge for bundle arb | 0.01 |
| `trading.cross_platform_min_edge` | Min net edge per $1 hedge | 0.02 |
| `trading.cross_platform_poll_seconds` | Re-price interval for matched pairs | 5 |
| `trading.cross_platform_max_contracts` | Max units per leg | 100 |
| `trading.taker_fee_bps` | Fallback fee when a market's schedule is unknown | 150 |
| `mode.min_match_similarity` | Market-matching threshold | 0.6 |
| `risk.max_global_exposure` | Max total exposure ($) | 50 |

Credentials can come from env vars: `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_PASSPHRASE`, `POLYMARKET_PRIVATE_KEY`.

## Limitations

- Cross-platform opportunities are **detected and reported only**. Kalshi order placement isn't implemented.
- Live Polymarket order signing isn't implemented either, so `live` mode can't actually trade yet.
- Markets are matched by text similarity. Check that both venues' resolution rules really match before trading a pair.

## Disclaimer

For educational use. Prediction-market trading carries risk of loss.

---

Originally derived from [polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage) by ImMike (MIT).
