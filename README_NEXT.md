# Kalshi consolidated paper monitor

## Install
Upload `paper_runner.py`, `test_paper_runner.py`, and `.github/workflows/main.yml` to your GitHub repository. **Do not delete your existing `bot.py`, `discover.py`, or `requirements.txt`.** This package also includes copies of the existing files for reference; the three listed files are the only required uploads.

GitHub Actions > Kalshi Bot - Consolidated Paper Monitor > Run workflow.

## Current mode: safe read-only
The workflow deliberately invokes `paper_runner.py` without `--paper`. It fetches active Kalshi markets and, in the last 60 seconds, attempts to find the exact Pyth Hermes feeds for Gold, Silver, Brent, EUR/USD, and GBP/USD. It checks the latest Pyth observation, market strike, quote, and displayed orderbook depth, then prints a single selected opportunity. It does **not** open simulated trades.

**Important:** Pyth Hermes latest observations are not guaranteed to be identical to the one-minute candlestick closing prices specified by Kalshi. CF Benchmarks RTI (BTC/ETH/SOL/HYPE) is **not connected**; those markets are skipped. The 9-series list is not yet fully automatic discovery of every possible eligible Kalshi series. The public Pyth feed identifiers and endpoint have not been verified against the live service from this build environment. The workflow may show `FEED UNAVAILABLE` and fail closed.

## Paper mode (not enabled by default)
The engine has a `--paper --persist-to-github` opt-in, which writes `paper_state.json` and commits/pushes it to GitHub whenever a paper trade is opened or officially settled. The state machine allows one open position, waits for Kalshi's `result`, and permanently stops after a simulated loss. The workflow is intentionally not configured to enable this until the data sources and GitHub persistence have been validated. A GitHub Actions schedule can be delayed and does not guarantee continuous 24/7 monitoring or 2-second sampling. GitHub Actions can also terminate unexpectedly between state transitions.

The $5 maximum includes a **conservative, provisional $0.10/share fee reserve**, not a verified Kalshi fee quote. The payout filter is gross `1/ask`, before fees. Distance is a ranking criterion only, not an eligibility threshold. The monitor requires available depth and an affordable whole-contract quantity.

No authenticated Kalshi endpoints, API keys, or real order placement are included.
