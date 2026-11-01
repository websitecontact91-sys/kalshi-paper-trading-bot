# Review specification: 0.012% minimum absolute strike cushion

Status: **review-only reference; not wired into live execution**

## Intended live eligibility rule

Add one new mandatory eligibility gate:

```
strike_cushion_pct = abs(current_underlying - strike) / abs(strike) * 100
eligible only if strike_cushion_pct >= 0.012
```

The gate is strictly additive:

```
eligible = ALL_EXISTING_LIVE_RULES AND strike_cushion_pct >= 0.012
```

It is intended to be evaluated at every existing candidate validation point,
including initial candidate selection, the first locked refresh, and the final
locked refresh immediately before POST.

## Preserve all existing behavior unchanged

Do not remove, weaken, replace, reinterpret, bypass, or otherwise modify any
existing condition, including:

- 80-90 cent executable-price band
- 1.10-1.20 payout-multiple requirement
- selected-side confirmation relative to strike
- selected-side strike cushion may stay unchanged or increase but may not shrink
- both live quote refreshes
- final-60-second entry window
- no initiation with fewer than 10 seconds remaining
- quote/reference freshness requirements
- IOC execution semantics
- immediate cancellation of unfilled IOC remainder
- no chasing or retry of remainder
- partial IOC fill acceptance and actual-fill reconciliation
- zero-fill handling
- one attempted position per shared round
- no resting or overlapping exposure
- balance and insufficient-funds protections
- $5 base stake
- four consecutive settled $5-tier wins select the next $10-tier trade
- existing streak reset behavior
- persistence, reconciliation, restart, hard-halt, and unknown-state protections

## Required acceptance tests before any human applies live wiring

1. 0.01199% fails.
2. Exactly 0.01200% passes.
3. Above 0.012% passes if all other conditions pass.
4. Initial candidate below threshold is rejected.
5. First locked refresh below threshold is rejected.
6. Final locked refresh below threshold is NOT_SENT.
7. Existing toward-strike rule still rejects a shrinking cushion even above 0.012%.
8. Existing 80-90 cent gate remains authoritative.
9. Existing 1.10-1.20 payout gate remains authoritative.
10. Existing side-of-strike gate remains authoritative.
11. Existing <10-second entry block remains authoritative.
12. IOC full/partial/zero-fill tests remain unchanged and pass.
13. $5/$10 stake and streak tests remain unchanged and pass.
14. Full pre-existing test suite passes without weakening or deleting tests.

The accompanying `strike_cushion.py` and tests are a deterministic reference
implementation only. They intentionally do not modify `live_runner.py`,
`live_execution.py`, workflow activation, configuration, credentials, or any
real-money execution path.
