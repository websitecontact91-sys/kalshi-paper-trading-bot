# Kalshi 15-minute market discovery and monitoring — read-only checkpoint

**No live orders and NO simulated trades in the included GitHub workflow.** The workflow tests code, performs targeted discovery, and continuously monitors market candidates for five minutes. It is a diagnostic milestone, not a fully working paper-trading strategy.

## GitHub instructions

Upload **all extracted files and the `.github/workflows/main.yml` file** to the repository, preserving the `.github/workflows/` folder path. The GitHub Actions workflow runs automatically when you click **Actions → Kalshi Bot - Discovery and Continuous Monitoring → Run workflow**. It runs about five minutes and shows recurring `SCAN` lines. The old workflow file should be replaced at `.github/workflows/main.yml`, not uploaded to the repository root.

## Behavior

- `python discover.py`: queries known 15-minute series directly, plus best-effort `/series` catalog enumeration; reports partial coverage and errors. A 0 count is not proof that no markets exist.
- `python monitor.py --interval 2 --duration 300`: refreshes the series catalog periodically and rechecks each series throughout a bounded session. **2 seconds is a target, not a guarantee**; multiple HTTP requests and API rate limits can make scans much slower. Catalog refresh defaults to 5 minutes.
- `bot.py`: the prior fail-closed paper strategy is retained unchanged. It is **not invoked by the workflow**. It requires verified per-market target mappings, exact settlement-reference data, and safe persistent state. The example configuration cannot enable trades.
- GitHub Actions runners are ephemeral: their local SQLite database is **not persistent**. Do not activate `bot.py` as a trading workflow until persistent storage and restart-safe stop-after-loss are implemented.

## Trading rules retained in `bot.py`

Paper only, maximum $5 per round, one market, final-60-second entry, $0.80–$0.90 contract ask, 1.10×–1.20× gross payout, price-to-target percentage distance used for ranking only, official settlement before next entry, and stop after loss. **Independent model-probability and expected-ROI filters also remain in the existing code**, which may prevent qualifying trades; confirm these requirements before enabling paper execution.

## Limitations

Candidate matching uses a conservative series ticker/asset heuristic and is not proof of a true 900-second market, completeness of eligible series, official strike semantics, or reference-price compatibility. Official Kalshi market rules and data feed integration still require validation. GitHub Actions cannot guarantee indefinite operation or two-second latency.
