# Kalshi 15-minute paper trading

This repository contains one active, read-only Kalshi paper simulator. It uses live Kalshi market data and simulated cash. Its HTTP client allows market, series, event, order-book and Kalshi-hosted reference-feed reads only. It has no real trading transport.

## Current deployment status

The implementation includes Decimal payout accounting, a single selection engine, atomic paper state, durable Git persistence, official-result settlement, deterministic simulations and the requested scheduling workflow. **Code/test success alone is not a readiness certificate.**

The deployment currently fails closed for new paper entries because `quote_policy.json` has no verified account balance precision or genuine SOL quote capture. The historical $5 → $5.69 → 1.14× arithmetic is covered by explicitly synthetic tests; it is not represented as an observed API response. Live monitoring, reporting and controlled simulations can run while that evidence is missing.

Dow's mapped US30 observation also remains incompatible until its instrument and units are established against the live contract. All 30 configured series remain visible in the universe report; a missing or incompatible series is never silently removed.

Live Kalshi series metadata exposed two compatibility corrections after the initial deployment: instrument-specific `app.pyth.com` explorer links are now recognized and their decoded feed IDs checked, and WTI uses the official `Commodities.Index.PYTHOIL/USD` identifier instead of the older WTI1M mapping. A live observation from the corrected identifier is still required before that market can qualify.

Readiness additionally requires live same-source deployment evidence, several hours of measured final-minute coverage, and a recorded deliberate chain break recovered by a scheduled run. `scripts/schedule_evidence.py` reports missing evidence rather than inferring it from green workflow jobs.

## Trading rules

- Exactly 30 mapped 15-minute series: 11 Kalshi-hosted CF Benchmarks references and 19 Kalshi-hosted Pyth references, defined once in `universe.py`. No direct external price provider is used.
- The five excluded families are TON, coffee, S&P 500, Nasdaq-100 and comparative/highest-return crypto. Their existing exact aliases are listed in `EXCLUDED_FAMILIES`.
- One $5 budget allocation, never split among markets. Actual cash debit includes fees and is positive and at most $5. Unused cash remains in the balance.
- The selected side's executable ask must be 0.80–0.90 inclusive. This ask is the probability field used; there is no independent probability model.
- The exact payout is the amount returned on a winning position. The exact multiplier is payout / actual debit. The displayed multiplier is rounded to two decimal places with `ROUND_HALF_UP` and must be 1.10–1.20 inclusive. Thus a raw 1.095× qualifies as displayed 1.10×.
- A payout of $5.69 on a $5 debit is exactly 1.138× and displays as 1.14×. Display rounding never changes the cash payout to $5.70.
- Eligible candidates rank by signed percentage distance, then exact multiplier, ticker and side. YES distance is 100 × (current − target) / target; NO distance is 100 × (target − current) / target. There is no positive or minimum distance gate.
- Monitor before the final minute. Entry is allowed only when `0 < close_time − now <= 60` seconds. Preserve the existing immediate in-window trigger, including entry 52.9 seconds before close.
- References must be no more than 15 seconds old and no more than 5 seconds in the future. CF cached observations are reused for at most 8 seconds; the same 15-second freshness gate applies at entry.
- Re-fetch the selected market, book, reference observation and fee metadata immediately before entry. If it becomes ineligible or expires, try the next ranked candidate while the window remains open.
- At most one position and one trade per shared 15-minute round across the whole universe. Persisted round buckets preserve the earlier `ceil(close_timestamp / 900)` identity; reports also include the ISO close time.
- Settle only from Kalshi's official final YES/NO result. Unknown, missing or provisional results remain pending across runs. An observed underlying price never settles a position.
- A win permits the next unused round. A loss persists `STOPPED`; no automatic reset exists. Verified official cancellation/void refunds the actual debit, records zero P&L and resumes scanning.
- No profit target, trade-count target, fixed session length or additional distance rule is imposed.

## Quote evidence and fee modeling

`payout_quote.py` owns cash and multiplier arithmetic. It rejects binary floats for money. `quote_adapter.py` contains the only fee formula and quote-normalization implementation.

The documented model uses the current quadratic taker formula, effective series/event terms, market fee waivers, positive 0.01-contract increments, six-decimal model-fee rounding and explicitly configured account balance precision. It sizes against actual depth at one observed price level and never assumes cumulative liquidity at a different price. Modeled quotes identify their assumptions and do not claim to be captured retail UI quotes.

Primary documentation:

- [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Fixed-point prices and quantities](https://docs.kalshi.com/getting_started/fixed_point_migration)
- [Batch market order books](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)

No documented read-only retail $5 payout-preview endpoint was established during this change. RFQ functionality is a different mechanism and is not used. An authenticated response containing the required historical SOL quote was not supplied, so no `sol_5usd_quote.json` is fabricated.

To clear the evidence gate, supply an audited real capture using the internal `kalshi-paper.normalized-quote.v1` schema documented in `quote_adapter.build_quote`, preserving the raw JSON, source URL, GET method, timestamp, SHA256, and exact raw field paths. The quote's market ticker and side must be bound to raw fields. Include the model validation inputs from the observed market/book/fee metadata. The actual quote must match the model and the specified $5 debit / $5.69 payout / displayed 1.14× UI example. `quote_policy.json` must point to that evidence and state the verified account precision (`0.0001` or `0.01`). A boolean alone cannot unlock entries.

The fixture in `tests/fixtures/sol_5usd_screenshot_arithmetic.json` is an arithmetic regression only. The README in that directory explains its provenance limits. Synthetic parser fixtures created by tests are never installed as live evidence.

## State and history

`paper_state.json` is authoritative. Money is persisted as strings. Entry and its used-round marker are one atomic, fsynced JSON replacement. The CSV `paper_ledger.csv` is a derived view of pending and settled entries. Round diagnostics and no-trade reasons are also preserved in state at run completion and in workflow artifacts.

The pre-change snapshot was `SCANNING`, balance $995.71, with one win in the active session. Its starting balance is inferred as $995.20 from the recorded $0.51 win. The earlier Brent loss remains separately archived in `paper_sessions/session-001-2026-10-29.json`. Migration preserves both sessions and does not overwrite their history or automatically restart either session. The remote state may advance through subsequent authorized paper runs.

After each entry and settlement, persistence writes state and ledger, commits them, pulls with rebase, and pushes. It retries transient failures up to four times with bounded backoff, aborts failed rebases, rejects conflicting remote state and source changes, and exits nonzero if durability is not confirmed. A persistence failure is not logged as a successful paper entry.

A new session is an explicit operation:

```bash
python paper_runner.py --paper --new-session --confirm-reset --duration 600
```

It archives the exact old state under `ledger_archive/` before resetting. It refuses to discard a pending position. Do not use this command to resume a session; ordinary runs load existing state. New state is never created automatically because a file is missing.

## Scheduled execution

`.github/workflows/main.yml` retains cron `*/5 * * * *`, a 600-second scanning run, and a two-second target loop interval. Market discovery refreshes at most once per 60 seconds. Metadata/reference preparation happens before entry windows. Within the window, batch status/book reads cover the precomputed candidates plus all active mapped markets, so a recovered reference or changed eligibility cannot be hidden by a stale shortlist. Network calls, bounded timeouts and rate-limit backoff can lengthen an individual cycle; actual timing is measured.

The workflow serializes writers through `kalshi-paper-state`. It reads and validates saved state first, uses pip caching, then runs compile, all tests, all six simulations, the source boundary check and the live universe report on the checked-out code. Only successful checks dispatch a successor. The successor waits behind the current run. Cron remains enabled as recovery. A persisted loss stops scanning and chaining; future cron runs exit quickly.

GitHub scheduling is not an exact clock and can be delayed. Serial ten-minute jobs cannot have consecutive job start times under a minute apart. Coverage verification therefore measures **last scan → next first scan**, plus observed final-minute scans for every complete round. It keeps the requested 600-second duration. See [GitHub's schedule-event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

`PAPER_SOURCE_SHA` records the tested checkout identity independently of subsequent state-only commits. `PAPER_CODE_FINGERPRINT` identifies the tracked code, configuration, tests and documentation, excluding only the exact runtime state/ledger files and JSON session archives. Schedule verification requires a common fingerprint across the observation period and matching provenance for every interval; different state-only commit SHAs do not invalidate the same code version. Missing fingerprints or mixed code/configuration versions cannot pass verification. State persistence rejects source changes during a run. A companion validation workflow runs offline checks even when the paper session is stopped; main-branch runs also perform a read-only live inventory and 90-second monitor.

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

They inject a fake client and clock into the real `PaperEngine`, use temporary state, and assert balances, entry counts, settlement, restart behavior and loss stops. They do not access the deployed session or prove live quote parity.

`universe_report.csv` retains one row for each of the 30 configured series, with separate configured/available/fresh/compatible/evaluated/eligible flags, units, identifiers, observed target/current values, freshness, rejection reasons and entry-window status. A successful report-generation exit is not a declaration that all 30 series are ready.

Collect scheduling evidence using current workflow history and downloaded diagnostics:

```bash
gh run list --workflow main.yml --limit 100 --json databaseId,event,status,conclusion,createdAt,startedAt,updatedAt,headSha,url > workflow_runs.json
python scripts/schedule_evidence.py --runs workflow_runs.json --coverage downloaded-diagnostics --chain-break-at '<observed ISO timestamp>' --cancelled-run-id '<actual deliberately cancelled pending run ID>' --output schedule_evidence.json
```

The analyzer requires three hours by default, observed schedule events, bounded inter-scan gaps, final-minute coverage, and cancellation/recovery tied to the actual scheduled run. Missing evidence produces `NOT_VERIFIED` and exit 2. It does not certify quote accuracy.

## Rule ownership and tests

| Rule | Active implementation | Tests |
|---|---|---|
| Exact debit/payout and displayed eligibility | `payout_quote.PayoutQuote`, `payout_eligible` | `test_payout_quote.py` |
| Captured quote provenance / fee model | `quote_adapter.build_quote`, `build_modeled_quote`; `live_client.quote_policy` | `test_quote_adapter.py`, `test_live_client.py` |
| Eligibility and deterministic ranking | `selection.rank`, `distance_pct` | `test_selection.py`, `test_runner.py` |
| Last-minute timing and freshness | `timing.in_entry_window`, `observation_fresh` | `test_timing.py`, `test_runner.py` |
| One position/round and exact state | `state_store.record_entry`, `validate` | `test_state_store.py` |
| Official settlement / loss stop / void | `PaperEngine._settle`, `state_store.record_settlement`, `record_void` | `test_settlement.py`, `test_runner.py` |
| Atomic writes and durable remote state | `state_store.save`, `persistence.persist` | `test_state_store.py`, `test_persist.py` |
| Thirty mappings and live compatibility | `universe`, `validate_universe.build_report` | `test_universe.py` |
| Kalshi-only read boundary and batching | `kalshi_client.Kalshi`, `LiveKalshiClient` | `test_client.py`, `test_live_client.py`, `test_no_orders.py` |
| Actual runner scenarios | `simulate.run_scenario`, `PaperEngine` | `test_runner.py` and six CI simulations |
| Scheduling / stop gate / evidence | Workflows; `workflow_gate.py`, `schedule_evidence.py` | `test_workflow.py` |

All active tests are under `tests/`. Old SQLite/Coinbase engines, duplicate runners/configurations and their obsolete tests are quarantined as non-executable `.txt` files in `legacy/`. They are historical reference, excluded from imports, compilation, test collection and workflow execution. `discover.py` remains a standalone catalog diagnostic; it does not own strategy rules or replace the fixed 30-series universe.
