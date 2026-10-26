# Options edge-engine — public review package (26 October 2026)

**Draft research code; not a profitable strategy, trade recommendation, live trading system, or approved Telegram notifier.** This package is a focused, sanitized review of the selection-only edge-engine from an isolated private research branch. No broker credentials, personal account data, transaction history, runtime configurations, bot tokens, vendor datasets, proprietary historical price feeds, or original private research ledger are included.

## Files

- [`tradingview/options/edge_engine.py`](../tradingview/options/edge_engine.py) — mirrors the frozen SPY/QQQ/IWM 30–60 DTE spread candidate set, values entire spreads over provided joint price/IV paths, applies the existing stops/targets and costs, and solves a model-only maximum debit/minimum credit at $0.01 per-share increments.
- [`tradingview/options/edge_research_ledger.py`](../tradingview/options/edge_research_ledger.py) — read-only paper-ledger auditor; optional *separate* local diagnostics DB storing only source hashes and summary statuses.
- [`tradingview/options/paper_scanner.py`](../tradingview/options/paper_scanner.py) — **complete frozen paper-only scanner reference**, including its offline state/lifecycle code; it has no broker order or Telegram delivery calls.
- [`tests/test_option_paper_scanner.py`](../tests/test_option_paper_scanner.py) — synthetic observation/contract fixture definitions only; not real market data.
- [`tests/test_options_edge_engine.py`](../tests/test_options_edge_engine.py) and [`tests/test_options_edge_ledger.py`](../tests/test_options_edge_ledger.py) — 44 deterministic synthetic/invariance/permission tests.

The core edge and ledger code are reproduced from private research commit `61e789ecddbb66f6bad2a0cd148c7b74d8b58982`. The previously incomplete policy stub has been replaced with the full offline frozen paper scanner. The code and review tests in this public branch can now be inspected and executed locally. This repository has an existing `tradingview.scheduler.market_calendar` module that the frozen quote validation references. The private repository, trading system, and production databases remain separate and unchanged.

## Reviewing and testing

Use a local Python 3.12+ environment with `pytest` and `pandas-market-calendars` installed. From this repository root:

```bash
python -m pytest -q tests/test_options_edge_engine.py tests/test_options_edge_ledger.py
```

The PUBLIC checkout has been verified independently: **86 relevant tests passed and two known review defects are reproduced as XFAIL**. The original private branch had a broader passing suite, but neither test result establishes trading profitability.

### Requested review topics

1. **Whole-spread economics:** bid/ask crossings, signed entry/exit cash flows, 100-share multiplier, commissions, assignment/funding assumptions, payoff parity between matched-strike bull call and bull put spreads.
2. **Path-dependent price limit:** does the cent-grid recompute stops/targets correctly, detect non-monotone admissible prices, and reject a price that consumes the modeled edge?
3. **Forecast integrity:** predecision timestamps, no future-label leakage, joint spot/IV correlation, volatility-surface arbitrage, missing input handling, forecast error uncertainty versus P&L tail risk.
4. **Risk and reproducibility:** $250 original spread risk, liquidation losses that can exceed expiration bounds, shared market-factor risk across SPY/QQQ/IWM, chronological out-of-sample validation.
5. **Fail-closed research:** zero actionable alerts on missing forecasts/quotes; no model-mark-to-observed-return promotion; ledger read-only mode and duplicate observation handling.

### Explicit limitations

- **No trained or independently calibrated probabilistic forecast artifact is included.** Synthetic fixture probabilities (e.g., 80% up / 20% down) are *invented test inputs*, not inferred odds.
- Projected future marks use Black–Scholes as a research approximation; ETF American exercise, discrete dividends, early assignment, joint IV surface consistency, sparse 15-minute stop paths, liquidity and actual multi-leg fills require separate validation. The error-adjusted expectation is a **model proxy**, not a defensible statistical lower confidence bound.
- The real private paper ledger had **53 runs, zero archived options observations, and zero trades** during the 26 Oct inspection. No empirical net expectancy, Sharpe, win rate, strategy outperformance or fill probability is established.
- No raw licensed option chains, user account records, browser sessions, original private DBs, broker connectors or Telegram delivery integrations are published.
- Keep the original frozen strategy as control. An actual edge claim needs point-in-time bid/ask-side quotes, a trained independent real-world joint forecast, next-observation costed entries/exits, matched shared-capital baselines and untouched forward confirmation.


## Current scope — architecture review, NOT routine code review

Astra is requested to review **engine architecture, economic selection objective, joint underlying/option-IV forecasts, spread valuation, price limits, portfolio concentration and falsifiable strategy experiments**, rather than style, individual patches or unit test internals. F-01 and F-02 have been fixed and expanded tests pass; the older results in this document predate that repair. Begin with [Architecture review brief](./ASTRA_ARCHITECTURE_ENGINE_BRIEF.md) and [fix/verification details](./OPTIONS_ENGINE_F01_F02_FIX_AND_ARCHITECTURE_REVIEW_2026-10-26.md). The current public branch remains review-only, not a real profitable or live trading strategy.
