# Options edge-engine architecture milestones — isolated research patch

**Status:** Portable, locally tested research implementation; **NOT deployed or yet committed in full to either GitHub review PR**. No brokerage orders, Telegram notifications, credentials, vendor subscriptions, original paper-scanner database writes, or claims of actual options profitability.

This source-only package is intended to be applied to the existing private `stocks` research branch (and then carefully sanitized for the public review branch). It implements the three architectural priorities arising from Astra's economic-design review, not a new trade recommendation.

## Contents and order

| Milestone | Module | What is implemented | What is NOT proven |
| --- | --- | --- | --- |
| A — forward validation | `architecture_forecast_validation.py` | Immutable timestamped predictions, three matched model arms (unchanged spot/IV; 5-session momentum/unchanged IV; original spot/IV proxy), exact-contract later-reference append journal and errors/IV-envelope coverage by target date | Accurate future IV, calibrated forecast probabilities, option execution, broker P/L |
| B — portfolio allocation | `architecture_allocation.py` | Whole-contract quantities, cash abstention, modeled net USD objective, $250/trade + $1,000 portfolio risk caps, cash/collateral and correlated stress scenarios, existing risk reservations, deterministic tie handling | Account positions from broker, predictive alpha, true options tail stress; for >12 candidates uses fully labeled feasible heuristic, NOT an exact optimum |
| C — fair forward evaluation | `architecture_paired_evaluation.py`, `architecture_paired_store.py` | Frozen-vs-challenger cohort states, paired reference bid/ask crosses at exact contract keys, entry and exit timing, immutable event journal, ranking-only vs full-policy comparability, missing/unresolved evidence and cash | Historical intraday full exit replay, verified brokerage fills, actual closed-option profits |
| Glue | `architecture_research_integration.py` | Pure adapter accepting original observed option snapshot, original model scenarios, prior completed price bars and existing `edge_engine.compare_frozen_to_edge` result | It does NOT fetch data or automatically run in a production scheduler |

### Applying the patch

Copy the `tradingview/options/architecture_*.py` files and `tests/test_options_architecture_*.py`, `tests/test_options_paired_store.py` into the *isolated* options research worktree. **`architecture_allocation.py` replaces the earlier draft allocation module already created on the private research branch.** Keep all preexisting original files unchanged. Run focused tests from that repo root:

```bash
python -m pytest -q tests/test_options_architecture_milestones.py tests/test_options_architecture_integration.py tests/test_options_paired_store.py
```

The package itself was tested independently on Python 3.11: **33 passed**. This does NOT replace the repository's 541-test original/frozen/scheduler regression suite, which must be rerun after applying the package.

### Safe manual workflow

1. During a new research-only SPY/QQQ/IWM scan, obtain a **completed-bar** price history and existing-source exact option quotes. Run existing `build_reference_forecast(...)` and `compare_frozen_to_edge(...)` without changing frozen selection.
2. Call `architecture_research_integration.assemble(...)` with the *same decision timestamp*, original observation, forecast and comparison. It returns three separate artifacts with `broker_orders=0`, `telegram_messages=0` and `actual_profit=NOT_MEASURED`.
3. Use `architecture_forecast_validation.record(research_db_path, artifact['forecast_vintage'])` before any future target is observed. The owner-only SQLite database must be separate from all original trading/paper databases.
4. As later observations actually arrive, call `match_future(...)` with **exact keys and timestamps**. Missing option legs are marked missing, never filled with synthetic marks. Call `compare(...)` to see MAE versus unchanged-IV and momentum naive alternatives and actual number of distinct target dates. Empirical envelope coverage is **NOT** calibrated 80% interval coverage.
5. Explicitly write cohort decisions to `architecture_paired_store.save_cohort(...)`; only after later matching exact-leg reference prices arrive append `entry` and `exit` events via `append_reference(...)`. Compare **ranking-only** separately from **full-policy** outcomes with `report(...)`. These are hypothetical price crosses, not broker fills. If either arm lacks evidence, the comparison is unresolved.
6. Use allocation suggestions **for offline research only**; shared factor stress inputs must be aligned scenario-by-scenario and collateral must be supplied. Unknown stress is a refusal status, not a zero-risk default.

**Important limits:** The source-first inventory remains TradingView scan2, Yahoo, free Cboe, Moomoo, installed TQS/ZeroDTE and existing archived option SQLite, with IBKR optional. No paid market data is required to run the research code, but current delayed quote sides and limited future-IV records do not prove executable net profits. Never promote reference marks or assumption-based model price limits to actionable buy/sell alerts. Do not merge/deploy without separate approval.

Astra's next review should remain **architecture/financial-engine only**: objective, forecasting reliability, joint portfolio sizing, risk exposure and falsifiable comparison design—not routine code style.
