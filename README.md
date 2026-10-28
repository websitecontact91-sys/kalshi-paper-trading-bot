# Kalshi 15-minute prediction-market PAPER bot

**Paper simulation only.** This project has no authenticated trading client and cannot submit real orders. It scans Kalshi's public market endpoints, estimates probability using a *heuristic* normal-distribution model of spot-price changes, simulates one $100 order at a time, waits for the official Kalshi result, and stops after the first loss.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml to add real Kalshi series tickers and market mappings.
python bot.py --config config.yaml
python bot.py --db paper.sqlite3 --status
pytest -q
```

## Critical configuration / limits

1. **Market discovery:** Enter real 15-minute `series_tickers` after checking Kalshi's public series/markets documentation. This bot enumerates all open markets in the selected series. It does not assume every crypto/FX/commodity product has a 15-minute market.
2. **Strike mapping:** The bot **does not guess a strike from ticker text**. Map each real market ticker to its correct `target_price`, product and YES/NO threshold rule in `markets:`. New market tickers require mappings. This is intentionally fail-closed rather than risking incorrect simulated bets. For fully automatic rollover, build a carefully verified rule parser per series and test against the official Kalshi market rules.
3. **Settlement price:** Coinbase spot `BTC-USD` etc. is **not automatically the settlement source** Kalshi uses. `settlement_source_verified` defaults to `false`. The program refuses simulated entries unless explicitly changed to true after verifying exact reference, timing, and threshold semantics. **Do not set this true simply because the symbol matches.** A proper deployment should implement the *exact* settlement reference feed; the sample Coinbase adapter is illustrative and can exhibit basis risk.
4. **Volatility:** Fresh spot samples are accumulated in memory after launch; until `min_price_samples` observations exist, no trade is placed. Sigma is computed from observed log-return volatility and scaled to remaining seconds. The normal-CDF probability is **not empirically calibrated** and can materially overstate accuracy in jumps, fat tails, and market volatility changes. Before treating paper results as meaningful, backtest with official settlement reference data and evaluate probability calibration.
5. **Order-book fills:** Kalshi orderbooks expose YES and NO bids; the opposite bid implies the ask. This script uses the displayed ask and sufficient top-of-book opposite-side quantity as a conservative simplified fill assumption. It does **not** model quote changes, queue priority, execution latency, partial fills, slippage or orderbook snapshots between polls. A 5-second poll can miss a market's final entry window.
6. **Contract sizing:** The bot buys an integer number of contracts costing **at most $100 inclusive of configured per-contract fees**; it cannot guarantee spending exactly $100. The per-contract fee setting is a manual conservative assumption, not Kalshi's official fee schedule. Set it after verifying the current series fee schedule.
7. **One trade at a time:** After opening a simulated trade, no other market can be entered until its official YES/NO result is available. A win resumes scanning; a loss sets `STOPPED` persistently in SQLite. Restarting the process does not bypass the stop. Unresolved/void/other results remain pending for manual review, never count as wins.
8. **Balance:** The default starting paper balance is $1,000, not $100, so a loss does not make balance negative. Set `initial_balance: 100` if desired. It records total cash and each trade in `paper.sqlite3`. Stop-after-loss is a risk control, not a profitability guarantee.
9. **Data sources:** The program needs outbound internet to Kalshi public REST and Coinbase Exchange public ticker endpoints. It will not produce simulated entries until real tickers and verified mappings are configured. Do not use for live trading.

## Architecture

`SCANNING` -> qualifying paper entry -> `WAITING_FOR_SETTLEMENT` -> official `result=yes/no` -> `SCANNING` on win or `STOPPED` on loss.

Every evaluated mapped market logs a reason to `observations`. Paper trades, realized P/L, cash balance and state persist in SQLite. Run `python bot.py --status` to inspect the most recent trades.

## References

- Kalshi API: https://docs.kalshi.com/
- Kalshi markets: https://docs.kalshi.com/api-reference/market/get-markets
- Kalshi order book: https://docs.kalshi.com/api-reference/market/get-market-orderbook
- Coinbase Exchange public market data: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductticker

## Market ranking (updated)

When multiple markets satisfy *all* eligibility rules, choose the market with the
**largest volatility-normalized distance** (`z`) on its winning side of the
threshold. This is the main ranking criterion, not payout. For markets whose
`z` scores differ by at most `z_tie_tolerance` (default 0.05), use the
**higher gross payout** as the tiebreaker, followed by estimated net ROI.

The candidate records `underlying_price`, `target_price`, `signed_distance`
(in native units), and `distance_pct`. These are useful diagnostics but are
not compared raw across BTC, currencies, and commodities. A BTC $300 buffer
and a currency $0.03 buffer cannot be compared meaningfully without scaling
for their different remaining volatility.

The scan interval defaults to two seconds, but this is **not a latency
guarantee**: serial REST calls, API throttling, timeouts, and model warm-up
can make scans slower. The current program still requires manually verified
market-to-settlement mappings and is paper-only.

## Version 3: full-window monitoring and tied-market basket allocation

- Every scan discovers configured open markets and samples each underlying from the
  beginning of its 15-minute window, so the rolling volatility estimate is warmed
  before the final 60 seconds. No entry occurs before that final window.
- Within the last 60 seconds, orderbooks are fetched concurrently and every
  eligible market is re-evaluated with fresh quotes. A 2-second polling setting
  is **best effort**, not a guaranteed scan latency or execution time.
- **Only top-ranked safety-margin ties** are split. The ranking is the
  volatility-adjusted threshold distance (`z`); markets within the configured
  `z_tie_tolerance` of the largest value count as equivalent. This is not an
  exact equality test and does not split among every qualifying market.
- The **entire $100 budget** is split equally across those tied markets; contract
  quantities are integers, so some cash remains unspent. For three ties, the
  allocation is *up to* $33.33 per market. The simulator never exceeds $100
  across the basket. Payout is only a tie-breaker for display/ordering.
- All basket markets must settle officially. If **any** leg loses, the bot
  permanently switches to `STOPPED`; if every leg wins, it resumes scanning.
- Current market mappings still need manually verified tickers, threshold,
  interval, and settlement-reference compatibility. The public Coinbase price
  feed is not automatically the official Kalshi settlement price. The normal
  approximation is **not a calibrated probability model**.
- The code still has **no real-order submission or authentication**.

Run tests: `python -m pytest -q`.

## Cloud deployment checkpoint (October 2026)

**This release adds read-only, paginated discovery monitoring, not automatic
trading.** `monitor.py` searches public open markets for likely 15-minute
crypto/commodity/FX tickers. These are **candidates**, not guaranteed complete
coverage or verified settlement-compatible instruments. It does not place any
paper or real orders. Run `python monitor.py --once` to see one discovery pass.

`render.yaml` creates a **paid Starter background worker** (check Render's
current prices before deploying). Deploy from the GitHub repository using a
Render Blueprint or background worker with start command
`python monitor.py --interval 60`. This monitoring service does not need a
persistent database; a future trading service will require a persistent store
for positions and stop-after-loss state.

The earlier `bot.py` strategy is still a manually mapped, fail-closed paper
engine. Its example config now caps each basket at **$5**. Do not switch the
Render start command to `bot.py` until official settlement mappings, historical
price calibration, complete series discovery, fees and execution logic have
been validated. The current discovery pass polls every 60 seconds, NOT every
2 seconds, to limit API load. Do not assume a ticker suffix proves duration.

## October 28 single-market rule update (authoritative over older README sections)

- **Paper only**, no authenticated order placement.
- $5 **maximum total cost per round**, including configured per-contract fees.
- Contract ask must be **$0.80-$0.90**; gross payout must be **1.10x-1.20x**; estimated model probability must be **0.80-0.90**; minimum expected ROI still applies.
- Price-to-target distance is **NOT an eligibility filter**. Among qualified opportunities, select **one** with the greatest **signed percentage distance in the predicted winning direction**. This makes prices across asset types comparable. Exact ties are broken by payout and then deterministic ticker/side order.
- Never split the $5, never open more than one outstanding paper position. Wait for settlement. A win resumes scanning; a loss stops.
- Earlier README mentions of baskets, splits, z-distance tie thresholds, or a mandatory `min_z_buffer` are **obsolete**. `z` is retained for the probability model and audit logs, not an eligibility cutoff.
- **Important:** The model probability and minimum ROI remain separate filters from the market's 80-90 cent ask price. At these payout levels, some combinations may never qualify after fees. This is expected fail-closed behavior.
- **Not ready for live paper sessions across every 15-minute market:** discovery currently identifies potential series only; exact per-market verified settlement thresholds and underlying price feeds still require integration. The current bot requires `series_tickers` and exact `markets` mappings. It must not be described as fully automatic.
- Existing SQLite databases containing multi-position baskets must not be reused; start with a fresh `paper.sqlite3` database after verifying configuration.

## Discovery CLI fix (GitHub Actions)

`python discover.py` now **actually fetches** public Kalshi open-market pages and
prints a count and details of potential 15-minute crypto/commodity/FX markets.
A network/API error returns a nonzero exit code, so GitHub Actions marks the
step failed instead of silently succeeding. It also warns when its page limit
prevents a complete catalog scan. This is a **read-only discovery diagnostic**;
no paper positions or live orders are created by this command. It does **not**
verify exact 15-minute duration, underlying settlement oracle, target semantics,
or completeness of all available market families. Such verification is still
required before meaningful paper trading.

The workflow command remains `python discover.py` (no workflow edit needed).
