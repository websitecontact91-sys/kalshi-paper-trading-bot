# Pre-edit verification record

Source checkout: `e66973ec79a92c0745afc938dbf606b08dd37b38`.

Observed before implementation:

```text
git log -1 --oneline
e66973e Save simulated Kalshi trading state

python3 -m compileall -q .
[exit 0; no output]

/tmp/kalshi-paper-fix-venv/bin/python -m pytest -q
[collection error: test_kalshi_prices.py accesses KALSHI_API_KEY_ID at import]

/tmp/kalshi-paper-fix-venv/bin/python -m pytest -q --ignore=test_kalshi_prices.py
58 passed in 0.69s

rg -n 'create_order|portfolio/orders|place_order' -g '*.py' .
[exit 1; no matches]
```

The requested reserve/reciprocal search found fee-reserve field names and the old reserve in legacy engine paths. The active runner used a floating-point published fee estimate, reconstructed debit as a fixed $5, filtered on the raw multiplier, and discarded nonpositive signed distance. The attached document's suggested $5.70 failing assertion was a hypothesis; the original active repository did not contain that payout test. This change adds the exact $5.69 regression without changing its expectation.

The source was downloaded through the authenticated GitHub connector and reconstructed as the exact Git tree/commit because anonymous local clone could not authenticate. Original state and archived session bytes were preserved while code changed.

## Before-change ownership

| File | Used by main workflow | Payout | Sizing | Selection | State | Settlement |
|---|---|---|---|---|---|---|
| paper_runner.py | Yes, entrypoint | Yes | Yes | Yes | Yes | Yes |
| bot.py | Indirectly imported for client/book helpers | Yes, obsolete engine | Yes, obsolete | Yes, obsolete | SQLite, obsolete | Yes, obsolete |
| monitor.py | No | No | No | Legacy monitoring | Legacy database | No |
| kalshi_pyth.py | Yes, imported | No | No | Reference prices only | No | No |
| discover.py | Yes, imported before change | No | No | Market discovery only | No | No |
| market_coverage.py | Yes, imported before change | No | No | Classification only | No | No |
| test_bot.py | Test suite, not live workflow entrypoint | Old engine tests | Old engine tests | Old engine tests | Old engine tests | Old engine tests |

After-change ownership is the current README's rule table. The old engine/tests/configurations are retained under `legacy/` as text, so their rules cannot be imported accidentally.
