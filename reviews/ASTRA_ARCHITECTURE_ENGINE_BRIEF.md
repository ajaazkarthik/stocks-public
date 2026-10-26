# Astra review — options ENGINE ARCHITECTURE and financial model

**Your assignment is to assess the architecture and economic validity of this options engine. Do not spend the review on routine source-code review, styling, refactoring or individual test implementation.** Code-level F-01 (invalid post-horizon reference marks) and F-02 (12-candidate hard cap) have already been fixed and tested.

Start with [Architecture / economics review brief and fix evidence](./OPTIONS_ENGINE_F01_F02_FIX_AND_ARCHITECTURE_REVIEW_2026-10-26.md). Supporting [research implementation methodology](./OPTIONS_NO_PURCHASE_EXISTING_DATA_SHADOW_2026-10-26.md) and [original independent bug discovery](./OPTIONS_INDEPENDENT_PRE_ASTRA_REVIEW_2026-10-26.md) are available.

## Architecture questions to answer

1. Is **conservative expected net profit** after bid/ask, uncertainty and slippage a sound option-selection objective? Does the model compare abstention (cash), defined-risk credit selling and directional debit buying fairly?
2. Is full **joint spot-price plus per-strike/per-expiry future implied-volatility** valuation with scenario-aware exit rules suitable? Where are the economic model and uncertainty assumptions weak?
3. Are the maximum debit / minimum credit limits and all existing frozen 21-calendar-day, 7-DTE, stop/target and assignment-related exit constraints consistent with the objective?
4. Does the portfolio engine control shared SPY/QQQ/IWM directional/correlation and tail exposure under the fixed per-spread and total risk budgets?
5. What minimal, no-purchase, point-in-time evaluation using TradingView scan2, Yahoo, free Cboe, Moomoo, TQS v2 and existing private research archives would credibly falsify the thesis? Please separate model predictions, observed reference quotes, hypothetical fills and actual closed trades.
6. What architecture should separate market-source validation, prediction, edge valuation, order execution and notifications so unverified scenarios can never trigger live actions?

**Deliverable:** prioritized architecture/engine findings, assumptions to challenge, proposed alternatives, acceptance criteria, and verdict on whether to retain or redesign the economic engine. No new paid market-data product is permitted.

## Engineering evidence already provided

- Full review source: `tradingview/options/edge_engine.py`, `existing_data_shadow.py`, `shadow_repricing.py`, `paper_scanner.py` and supporting research.
- F-01 and F-02 fixes committed in the public review branch. The 12-candidate parameter is now a **batch size**; expired/out-of-horizon reference marks are rejected.
- The public checkout is testable with the included offline tests. No broker data, credentials, historical licensed vendor files or actual fills are exported.
- **No observed options profitability or independently calibrated future IV surface is claimed.** This remains research and has not been merged into the running scanner.


## 2026-10-26: Three architecture research milestones integrated and tested

The isolated research implementation is now available in `tradingview/options/architecture_forecast_validation.py`, `architecture_paired_evaluation.py`, `architecture_paired_store.py`, `architecture_allocation.py` and `architecture_research_integration.py`, with corresponding tests. **Public review checkout: 128 focused tests PASS. Private scanner regression: 574 tests PASS.** One pre-existing pytest `asyncio_mode` configuration warning in each run.

- **A — Forecast:** Immutable, as-of timestamped projection snapshots and later exact-contract REFERENCE observations, comparing unchanged spot/IV, 5-day momentum/unchanged IV and the eight-path assumed-IV challenger. Empirical model errors and coverage remain reference-only, not proof of calibration.
- **C — Matched evaluation:** Frozen-versus-challenger ranking-only and full-policy cohorts with decision/entry/exit/abstention states and an append-only journal. Missed source evidence remains unresolved. Crossed bid/ask prices never become claimed brokerage fills or realized profit.
- **B — Joint portfolio allocation:** Whole-contract quantities with cash abstention, $250 per-position / $1,000 reserved-risk caps, collateral and shared stress scenarios. More than twelve candidates use a transparent NOT-PROVEN-OPTIMAL heuristic. No executable orders.

Astra's scope is **economic architecture, model forecasting and joint risk/evaluation assumptions**, NOT routine line-by-line code review. The implementation is complete as an isolated, manually invoked research package; it does **not** automatically collect live observations, prove profitable options, or modify the running scanner, broker, Telegram or production DBs. No paid market data purchased. See [implementation methodology](./OPTIONS_ARCHITECTURE_IMPLEMENTATION_2026-10-26.md).