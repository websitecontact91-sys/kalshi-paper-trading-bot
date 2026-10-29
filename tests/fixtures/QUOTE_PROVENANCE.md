# Quote provenance and paper-model assumptions

The owner authorized API-based simulated quotes on 29 October 2026 without
requiring a historical SOL capture or UI match. The configured `documented_api`
policy permits paper entries based on observed Kalshi data and documented fees.
The provenance distinctions below remain important: permission to simulate does
not relabel a modeled quote as a captured or UI-verified quote.

`sol_5usd_screenshot_arithmetic.json` preserves the owner's screenshot arithmetic.
It is **not a captured Kalshi response** and does not prove live quote-adapter parity.
There is deliberately no invented `sol_5usd_quote.json` presented as an API capture.

The pure accounting contract is `$5.69 / $5 = 1.138`, displayed as `1.14` with the
rounding rule specified in the implementation instructions. Multiplying the rounded
display back by the budget would incorrectly manufacture `$5.70`.

The adapter tests use synthetic response field names to exercise digest validation
and field mapping. They do not establish a production endpoint or raw field schema.
The normalized schema `kalshi-paper.normalized-quote.v1` is internal to this bot.
Only an independently audited read-only capture adapter may attest its source mapping
as verified. It supplies the original JSON text, SHA256, timestamp, request method,
Kalshi source URL, and paths to the debit/payout/quantity/fees/principal values.
`build_quote` checks those values and accounting reconciliation. UI parity requires
additional validation linked to the same response digest.

`build_modeled_quote` uses the documented quadratic taker formula and fractional
quantities. It explicitly labels results `documented_fee_model` with
`ui_parity_verified=false`. The test case at a synthetic $0.87 ask with non-direct
member cent rounding produces a $5 debit and $5.69 payout. This shows the documented
formula is capable of that arithmetic; it does **not** establish that those were the
historical screenshot's actual inputs. No constant is adjusted to force the result.

Model assumptions are recorded in the ledger: one equivalent taker fill at one
observed price level, observed fee metadata, and explicitly supplied balance precision.
The configured paper account uses documented direct-member $0.0001 balance
alignment as an explicit simulation convention. It does not assert anything about
the owner's real account. An invalid precision setting or unknown fee metadata
still blocks the affected quote. The model does not claim the interface's
multi-fill/rounding behavior has been captured.

Official sources checked 29 October 2026:

- https://kalshi.com/docs/kalshi-fee-schedule.pdf — July 7, 2026 fee schedule.
- https://docs.kalshi.com/getting_started/fee_rounding — six-decimal model fee,
  direct-member $0.0001 and non-direct-member $0.01 balance alignment, and rebates.
- https://docs.kalshi.com/getting_started/fixed_point_migration — 0.01-contract
  quantities and market-specific price grids.
- https://docs.kalshi.com/api-reference/market/get-series — series fee metadata.
- https://docs.kalshi.com/api-reference/events/get-event — event overrides.
- https://docs.kalshi.com/getting_started/rfqs — RFQ quotes are a communications
  workflow, not a standalone retail preview endpoint. This bot does not create RFQs.

Optional strict capture mode still requires genuine corresponding inputs and UI
validation before it can claim parity. The deployed API-based paper mode does not
require that extra proof; an accounting unit test is never described as a capture.
