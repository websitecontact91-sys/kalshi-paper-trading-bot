# Kalshi 15-minute paper trading

The scheduled paper simulator uses live Kalshi market data and simulated cash. Its HTTP client allows market, series, event, order-book and Kalshi-hosted reference-feed reads only. A separate live-account implementation is included with real-order submission disabled by default. Both use a $1 total budget including fees for new entries.

## Current deployment status

The implementation includes Decimal payout accounting, a single selection engine, atomic paper state, durable Git persistence, official-result settlement, deterministic simulations and the requested scheduling workflow.

On 29 October 2026 the owner authorized paper entries using Kalshi's live API data and published fee rules without reproducing a historical retail UI quote. `quote_policy.json` selects `documented_api` mode. A genuine SOL screenshot/API capture and verification of a real account's rounding are not prerequisites for this simulated session. The model records its assumptions and never claims UI parity or an actual execution.

All 30 configured series are checked repeatedly. Markets can appear and disappear during the day. An unavailable, stale or incompatible market is skipped for that scan; it does not prevent another market from qualifying. A later discovery refresh can bring an available market back into consideration. No extra price provider is used: current reference values, contract targets, market prices and final results come from Kalshi, including its hosted CF Benchmarks and Pyth feeds.

Dow stays on the configured list. Its matching Kalshi-hosted US30 reference can be used when an available contract's Kalshi source metadata and target units agree. Missing or contradictory data skips Dow only; the bot does not invent a target, rescale an unrelated instrument, or consult an external feed.

The corrected WTI mapping uses Kalshi's `Commodities.Index.PYTHOIL/USD` identifier. Instrument-specific `app.pyth.com` links in Kalshi's metadata are inspected as identifiers; they are not fetched as external price sources.

Workflow reports record actual scans, closing-minute coverage, handoff gaps and source versions. `scripts/schedule_evidence.py` remains a diagnostic for sustained coverage and recovery; incomplete multi-hour evidence does not disable otherwise valid paper entries under the owner's updated operating rules. A green run alone does not guarantee uninterrupted coverage.

## Trading rules

- Exactly 30 mapped 15-minute series: 11 Kalshi-hosted CF Benchmarks references and 19 Kalshi-hosted Pyth references, defined once in `universe.py`. No direct external price provider is used.
- The five excluded families are TON, coffee, S&P 500, Nasdaq-100 and comparative/highest-return crypto. Their existing exact aliases are listed in `EXCLUDED_FAMILIES`.
- One $1 budget allocation, never split among markets. Actual cash debit includes fees and is positive and at most $1. Unused cash remains in the balance. `trade_config.TRADE_BUDGET` is the shared limit for all new entries; existing $5 history and pending positions retain their original recorded amounts.
- The selected side's executable ask must be 0.80–0.90 inclusive. This ask is the probability field used; there is no independent probability model.
- The exact payout is the amount returned on a winning position. The exact multiplier is payout / actual debit. The displayed multiplier is rounded to two decimal places with `ROUND_HALF_UP` and must be 1.10–1.20 inclusive. Thus a raw 1.095× qualifies as displayed 1.10×.
- For example, the $1 direct-member paper model at an 87-cent ask buys 1.13 contracts for $0.9921 including fees and pays exactly $1.13 if it wins. Its displayed multiplier is 1.14×; multiplying a rounded multiplier by the budget never determines the cash payout. The historical $5.69 payout regression remains in the tests.
- Eligible candidates rank by signed percentage distance, then exact multiplier, ticker and side. YES distance is 100 × (current − target) / target; NO distance is 100 × (target − current) / target. There is no positive or minimum distance gate.
- Monitor before the final minute. Entry is allowed only when `0 < close_time − now <= 60` seconds. Preserve the existing immediate in-window trigger, including entry 52.9 seconds before close.
- References must be no more than 15 seconds old and no more than 5 seconds in the future. CF cached observations are reused for at most 8 seconds; the same 15-second freshness gate applies at entry.
- Re-fetch the selected market, book, reference observation and fee metadata immediately before entry. If it becomes ineligible or expires, try the next ranked candidate while the window remains open.
- At most one position and one trade per shared 15-minute round across the whole universe. Persisted round buckets preserve the earlier `ceil(close_timestamp / 900)` identity; reports also include the ISO close time.
- Settle only from Kalshi's official final YES/NO result. Unknown, missing or provisional results remain pending across runs. An observed underlying price never settles a position.
- Both wins and losses permit scanning for the next unused round when available cash is at least $1.00. The normal strategy stop is available cash strictly below $1.00; exactly $1.00 is sufficient even if an individual quote could cost less. Verified official cancellation/void refunds the actual debit and records zero P&L, then applies the same balance rule.
- A pending position is always followed through official settlement even if its purchase leaves available cash below $1.00. Low cash blocks new positions; it never discards an unresolved position. Invalid state, uncertain executions and failed durability checks still prevent unsafe entries.
- No profit target, trade-count target, fixed session length or additional distance rule is imposed.

## API-based paper quotes and fee modeling

`payout_quote.py` owns cash and multiplier arithmetic. It rejects binary floats for money. `quote_adapter.py` contains the only fee formula and quote-normalization implementation.

The documented model uses the current quadratic taker formula, effective series/event terms, market fee waivers, positive 0.01-contract increments and six-decimal model-fee rounding. The configured virtual account uses Kalshi's documented direct-member $0.0001 cash precision. This is an explicit simulation convention, not a claim that a particular real account has been inspected. Kalshi also documents a $0.01 convention for non-direct members; the policy supports an explicitly configured model for that case.

The simulator uses the executable ask derived from Kalshi's current order book and the actual quantity available at that same price level. The market-summary ask is retained only as a diagnostic: separate summary and order-book responses can disagree. A fresh book is fetched during final revalidation, and price eligibility, sizing and fees are recomputed from it. Missing book depth prevents a quote. No cumulative liquidity at a different price is assumed.

It records principal, fees, actual debit, quantity, unused cash, exact payout and the displayed multiplier. The ledger identifies the order-book source of the selected ask and separately retains the summary ask when available. Modeled quotes remain labeled `documented_fee_model` with `ui_parity_verified=false`. Operational permission is recorded separately as `quote_policy_ready` and `quote_policy_mode`.

Kalshi's live event responses omit unused fee-override fields. The adapter follows the official optional/nullable/omit-empty schema and inherits observed series fees only after binding the fetched event to its market and series. A captured public BTC/SOL/WTI metadata-and-book fixture verifies those actual response shapes. Missing fee data, invalid quantities, inadequate depth, or unsupported contract terms still make that quote unavailable.

Primary documentation:

- [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Fixed-point prices and quantities](https://docs.kalshi.com/getting_started/fixed_point_migration)
- [Batch market order books](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)

No documented read-only retail $5 payout-preview endpoint was established during this change. The selected paper mode uses observed executable prices and published fees instead. RFQ functionality is a different mechanism and is not used. No `sol_5usd_quote.json` is fabricated.

An optional strict capture-validation mode remains available for later investigation of retail UI parity. `quote_adapter.build_quote` verifies an audited internal `kalshi-paper.normalized-quote.v1` capture against preserved raw JSON, a Kalshi GET source URL, timestamp, SHA256, and exact field paths. That optional validation is separate from the configured API-based paper mode. Setting a purported verification boolean does not turn synthetic data into a genuine capture.

The fixture in `tests/fixtures/sol_5usd_screenshot_arithmetic.json` is an arithmetic regression only. The README in that directory explains its provenance limits. Synthetic parser fixtures created by tests are never installed as live evidence.

## Live-account implementation: disabled by default

`live_config.json` ships with `enabled: false`. The paper workflow still invokes only `paper_runner.py --paper`; it cannot import the live broker through its local dependency graph. `scripts/audit_paper_boundary.py` checks that graph and the market-only HTTP transport. Adding optional live code does not change the paper workflow into a real-money workflow.

`live_broker.py` implements authenticated account reads and the current V2 order interface. A YES purchase maps to a bid at its YES price; a NO purchase maps to an ask at `1 - NO price` on V2's YES-price scale. Orders use fixed-point quantities/prices, a deterministic client order ID, and fill-or-kill. Writes are disabled by default in both the broker and execution engine. No order is sent by account preflight, initialization, state inspection, or reconciliation-only mode.

`live_runner.py` uses the same thirty-series universe, Kalshi-hosted references, eligibility rules, signed-distance ranking, final-minute window and fresh-quote revalidation as the paper engine. It independently resizes the selected quote using a conservative live cost bound. The default command is a read-only account preflight:

```bash
python live_runner.py --preflight
```

The preflight verifies available account cash, exchange status, resting-order reads and position reads. Its report explicitly says that real-order execution has not been tested. Successfully reading a balance does not prove permission to submit an order, an actual fill, or a completed real-money settlement.

### Actual fills, fees and the $1 limit

The V2 order schema does not document a server-enforced total-cost field that includes fees. The live implementation therefore sizes with a conservative bound under the currently observed quadratic fee terms: every minimum 0.01-contract increment is treated as a separately rounded fill, possible rebates are ignored, and cent rounding conservatively covers either documented account type. The bound must be at most $1 before submission. The expected fee-inclusive payout must still display within 1.10–1.20, and the selected ask must still be 80–90 cents.

This bound can produce a smaller live position than the paper model. For example, at an 87-cent ask the paper model buys 1.13 contracts for $0.9921; the conservative live model can choose 1.00 contract with an expected $0.88 debit. Both use a maximum $1 budget, and unused money remains available. A fee-policy change during matching is not covered by a nonexistent server-side cost cap. Actual fills and fees remain authoritative; an accounting discrepancy or rule breach halts further entries.

The live journal records actual attributed fills, their IDs, timestamps, quantities, prices, principal, fees and debit. Order aggregates must agree with those fills. An acknowledged order is not recorded as a filled trade merely because its POST succeeded. Unknown or partial outcomes remain under reconciliation; a remaining/resting order or inconsistent response halts new entries for review. The implementation never guesses a fill, retries an uncertain submission, or cancels unrelated account orders.

Official market results and a matching account settlement are both required to recognize P/L. Aggregate settlement quantity, principal and fees must match the bot's own position; existing exposure in the selected ticker blocks entry. Deposits, withdrawals and unrelated positions do not become bot profit. Both wins and losses allow the next unused round when current available account cash is at least $1.00. Confirmed cash below $1.00 persists `STOPPED` with `stop_reason=insufficient_funds`; execution-integrity violations persist `HALTED`. A verified void needs evidence of the actual full refund. Missing, conflicting or insufficiently precise settlement evidence remains pending or halted.

The live runner checks available cash before scanning, periodically during the run, immediately before submitting a candidate, and after terminal order outcomes. A failed balance read cannot authorize a new entry from a stale balance. Pending orders continue reconciliation while funds are low. The read-only account check in `--inspect-state` refreshes the separate durable journal's balance and funding mode without placing an order. A later scheduled check can resume an ordinary funds stop if available cash is again at least $1.00; it does not clear an execution-integrity halt.

### Durable intent and recovery

`live_state.py` maintains an independent `kalshi-live.v1` journal bound to environment, subaccount and a hash of the API-key identity. A completed paper session is never imported as real cash or positions. The intent and used-round marker are saved durably before the first order request. The entry window and reference freshness are checked again after account reads and after saving the intent.

`live_persistence.py` uses an atomic fsynced local journal plus a separate GitHub `live-state` branch containing only `live_state.json`. It confirms the exact remote state before permitting submission, refuses competing writers, and detects a successful push whose acknowledgement was lost. It never rewrites the source branch or the paper ledger. The workflow uses its own single-writer concurrency group; production execution requires this remote journal and GitHub Actions. Local files support offline tests and read-only work, not production execution.

A restart reconciles the saved client/order identity first. A request timeout or an intent interrupted before its outcome is known never causes an automatic second POST. If no order can yet be found, that intent remains unresolved for review; an empty lookup is not proof that a delayed request failed. Previously observed fills cannot silently change or disappear. State validation independently checks fill totals, accounting, funding stops and execution-integrity halts. A legacy stop caused only by the former loss rule can resume after a fresh sufficient-balance check; its trades and realized P/L are retained.

### Owner-controlled eventual activation

`.github/workflows/live-readiness.yml` runs offline lifecycle tests and an authenticated GET-only preflight. It cannot place orders. `.github/workflows/live-trading.yml` is prepared separately with the same five-minute schedule and a 600-second scan, but scheduled live jobs are gated by the unset/false repository variable `KALSHI_LIVE_SCHEDULE_ENABLED` and the disabled versioned configuration.

The live workflow's manual default is `preflight`. Its `initialize` operation creates a new separate journal only when one does not exist; it cannot reset an existing or stopped session. `reconcile` refreshes existing order/settlement records without allowing a new order. `run` requires `live_config.json` to be explicitly enabled and the owner's exact `I_ACCEPT_REAL_MONEY_TRADES` confirmation. Recurring scans additionally require the owner to enable `KALSHI_LIVE_SCHEDULE_ENABLED`. The state-and-funds gate runs before a successor is queued. Insufficient funds or an execution-integrity halt without pending exposure stops chaining; cron remains available to recheck funding. An ordinary settled loss no longer stops scanning or chaining.

Those production activation controls are intentionally left off in this implementation. The repository contains no automatic switch from paper to real money. Offline tests use mocked orders and account responses; no real order placement or real-money end-to-end settlement is claimed. The owner must initiate and supervise any eventual exchange execution test separately.

Primary API references for the separate live implementation:

- [V2 order creation](https://docs.kalshi.com/api-reference/orders/create-order-v2)
- [Order direction and price conventions](https://docs.kalshi.com/getting_started/order_direction)
- [Account balance](https://docs.kalshi.com/api-reference/portfolio/get-balance)
- [Order retrieval](https://docs.kalshi.com/api-reference/orders/get-order)
- [Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)
- [Account settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements)

## State and history

`paper_state.json` is authoritative. Money is persisted as strings. Entry and its used-round marker are one atomic, fsynced JSON replacement. The CSV `paper_ledger.csv` is a derived view of pending and settled entries. Round diagnostics and no-trade reasons are also preserved in state at run completion and in workflow artifacts.

Before the insufficient-funds policy update, the active session was `STOPPED` under the former loss rule, with $997.8913, seven completed trades and no pending position. Its starting balance remains $995.20. The earlier Brent loss remains separately archived in `paper_sessions/session-001-2026-10-29.json`. The owner's new rule explicitly resumes the funded active session without resetting it. Migration adds `stop_policy=insufficient_funds_v1` and changes only the current policy/mode; session identity, balances, trade history, historical `state_after` values, used rounds and archived files remain intact. The remote state can advance through subsequent authorized paper runs.

After each entry and settlement, persistence writes state and ledger and confirms them on GitHub. It retries transient failures up to four times with bounded backoff, rejects conflicting remote state, and exits nonzero if durability is not confirmed. A persistence failure is not logged as a successful paper entry.

When code is updated during a scan, persistence saves only the current state and ledger on top of the newer remote code without replacing the running checkout's source. Only a confirmed save permits a clean `SOURCE_UPDATE_HANDOFF`: the current process stops, and the already queued successor loads and tests the newer code. A pending entry remains pending. A settled loss is retained and the next scan follows the current available-balance policy. Coverage artifacts record the handoff. Genuine save failures and competing state writes still fail the workflow; they are never hidden by the update handling.

A new session is an explicit operation:

```bash
python paper_runner.py --paper --new-session --confirm-reset --duration 600
```

It archives the exact old state under `ledger_archive/` before resetting. It refuses to discard a pending position. Do not use this command to resume a session; ordinary runs load existing state. New state is never created automatically because a file is missing.

## Scheduled execution

`.github/workflows/main.yml` retains cron `*/5 * * * *`, a 600-second scanning run, and a two-second target loop interval. Market discovery refreshes at most once per 60 seconds. Metadata/reference preparation happens before entry windows. Within the window, batch status/book reads cover the precomputed candidates plus all active mapped markets, so a recovered reference or changed eligibility cannot be hidden by a stale shortlist. Network calls, bounded timeouts and rate-limit backoff can lengthen an individual cycle; actual timing is measured.

The workflow serializes writers through `kalshi-paper-state`. It reads and validates saved state using the same balance-policy migration as the runner, uses pip caching, then runs compile, all tests, all six simulations, the source boundary check and the live universe report on the checked-out code. Only successful checks dispatch a successor. The successor waits behind the current run. Cron remains enabled as recovery. An available balance below $1.00 with no pending position stops scanning and chaining; future cron runs exit quickly while the balance remains insufficient. Ordinary losses do not stop the session. The runner durably saves a legacy policy migration before any new paper scanning.

GitHub scheduling is not an exact clock and can be delayed. Serial ten-minute jobs cannot have consecutive job start times under a minute apart. Coverage verification therefore measures **last scan → next first scan**, plus observed final-minute scans for every complete round. It keeps the requested 600-second duration. See [GitHub's schedule-event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

`PAPER_SOURCE_SHA` records the tested checkout identity independently of subsequent state-only commits. `PAPER_CODE_FINGERPRINT` identifies the tracked code, configuration, tests and documentation, excluding only the exact runtime state/ledger files and JSON session archives. Schedule verification requires a common fingerprint across the observation period and matching provenance for every interval; different state-only commit SHAs do not invalidate the same code version. Missing fingerprints or mixed code/configuration versions cannot pass verification. A source update ends the old scan after its current state transition is durably saved; the old process never continues with untested replacement code. A companion validation workflow runs offline checks even when the paper session is stopped; main-branch runs also perform a read-only live inventory and 90-second monitor.

## Validation commands

Use Python 3.12 and the existing Kalshi environment credentials for authenticated live references. Do not paste credentials into source files or reports.

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python -m pytest -q
python simulate.py --scenario all
python validate_universe.py --output universe_report.csv --json-output universe_report.json --wait 10
python paper_runner.py --duration 90 --interval 2 --report-dir runtime_reports
```

The last command monitors without writing trading state. The deployed writer adds `--paper --persist-to-github` in GitHub Actions and runs for 600 seconds. A STOPPED paper run exits before creating a live client.

Individual deterministic simulations:

```bash
python simulate.py --scenario win
python simulate.py --scenario loss
python simulate.py --scenario tie
python simulate.py --scenario no_candidate
python simulate.py --scenario late_expiry
python simulate.py --scenario delayed_result
```

They inject a fake client and clock into the real `PaperEngine`, use temporary state, and assert balances, entry counts, settlement, restart behavior and continuation after a loss. Additional boundary tests cover cash just below $1.00, exactly $1.00, pending settlement with low available cash, and legacy loss-stop migration. Integration tests run the real live-client adapter with controlled API-shaped inputs through paper entry, persistence, restart and official-result settlement. These tests do not access the deployed session or claim retail UI parity.

`universe_report.csv` retains one row for each of the 30 configured series, with separate configured/available/fresh/compatible/evaluated/eligible flags, units, identifiers, observed target/current values, freshness, rejection reasons and entry-window status. Missing markets are per-series skips. The report distinguishes policy/configuration blockers from those temporary skips; it never requires all 30 series to be open or eligible simultaneously.

Collect scheduling evidence using current workflow history and downloaded diagnostics:

```bash
gh run list --workflow main.yml --limit 100 --json databaseId,event,status,conclusion,createdAt,startedAt,updatedAt,headSha,url > workflow_runs.json
python scripts/schedule_evidence.py --runs workflow_runs.json --coverage downloaded-diagnostics --chain-break-at '<observed ISO timestamp>' --cancelled-run-id '<actual deliberately cancelled pending run ID>' --output schedule_evidence.json
```

The diagnostic analyzer requires three hours by default, observed schedule events, bounded inter-scan gaps, final-minute coverage, and cancellation/recovery tied to the actual scheduled run. Missing evidence produces `NOT_VERIFIED` and exit 2. That result describes the supplied reliability evidence; it is not a runtime paper-entry gate and does not certify quote accuracy.

## Rule ownership and tests

| Rule | Active implementation | Tests |
|---|---|---|
| Exact debit/payout and displayed eligibility | `payout_quote.PayoutQuote`, `payout_eligible` | `test_payout_quote.py` |
| $1 budget for new entries; historical accounting retained | `trade_config`, `selection`, `state_store` | Quote, selection, runner and historical-settlement tests |
| Captured quote provenance / fee model | `quote_adapter.build_quote`, `build_modeled_quote`; `live_client.quote_policy` | `test_quote_adapter.py`, `test_live_client.py` |
| Eligibility and deterministic ranking | `selection.rank`, `distance_pct` | `test_selection.py`, `test_runner.py` |
| Last-minute timing and freshness | `timing.in_entry_window`, `observation_fresh` | `test_timing.py`, `test_runner.py` |
| One position/round and exact state | `state_store.record_entry`, `validate` | `test_state_store.py` |
| Official settlement / continue after loss / void | `PaperEngine._settle`, `state_store.record_settlement`, `record_void` | `test_settlement.py`, `test_runner.py` |
| Available cash below $1.00 stops new scanning; pending positions still reconcile | `state_store`, `workflow_gate`, `live_execution`, `live_runner` | State, runner, workflow and live-execution boundary tests |
| Atomic writes and durable remote state | `state_store.save`, `persistence.persist` | `test_state_store.py`, `test_persist.py` |
| Thirty mappings and live compatibility | `universe`, `validate_universe.build_report` | `test_universe.py` |
| Kalshi-only read boundary and batching | `kalshi_client.Kalshi`, `LiveKalshiClient` | `test_client.py`, `test_live_client.py`, `test_no_orders.py` |
| Actual runner scenarios | `simulate.run_scenario`, `PaperEngine` | `test_runner.py` and six CI simulations |
| Scheduling / stop gate / evidence | Workflows; `workflow_gate.py`, `schedule_evidence.py` | `test_workflow.py` |
| Disabled-by-default live V2 transport and cost bound | `live_broker` | `test_live_broker.py` |
| Durable live intent, actual fills, settlement and persistent halt | `live_execution`, `live_state` | `test_live_execution.py` |
| Independent live journal durability | `live_persistence` | `test_live_persistence.py` |
| Shared strategy, live sizing and read-only preflight | `live_runner` | `test_live_runner.py` |

All active tests are under `tests/`. Old SQLite/Coinbase engines, duplicate runners/configurations and their obsolete tests are quarantined as non-executable `.txt` files in `legacy/`. They are historical reference, excluded from imports, compilation, test collection and workflow execution. `discover.py` remains a standalone catalog diagnostic; it does not own strategy rules or replace the fixed 30-series universe.
