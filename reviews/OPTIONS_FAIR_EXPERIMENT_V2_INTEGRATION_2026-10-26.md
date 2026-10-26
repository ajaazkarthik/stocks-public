# Public options fair experiment v2 — research integration (26 October 2026)

**Review-only. Do not merge into live systems or treat the results as realized option returns.** This public branch has a sanitized copy of the same research implementation in private draft PR #36; no brokerage credentials, account records, local paths or proprietary vendor data are included.

## Implemented

1. **Common decision-time reference snapshot:** all forecast arms use the same spot, exact-contract IV, underlying timestamp and source provenance; the historical paths now expose their starting spot explicitly. Completed-day history is for features, not a cheaper baseline starting price.
2. **Allocator-driven experimental decisions:** ranking-only changes selection with frozen participation and sizing; full-policy uses model-limit-qualified allocator IDs, integer quantities and cash abstention. Same-decision multi-symbol research uses a common residual-budget constraint.
3. **Frozen reference lifecycle:** replay delegates to the original unmodified frozen paper scanner `advance`, including pending entry, stop/target/trend, short-leg checks, pre-expiry, 21-day maximum holding, slippage and fees. Arms receive the same chronological observations but can exit at different times. Missing evidence remains unresolved; reference marks are never broker-confirmed fills.
4. **Complete evidence accounting:** includes matured outcomes whether matched, delayed, invalid or missing, immutable research vintages, later append-only evidence, and distinguishes residual-budget assumptions from proven complete-account risk.
5. **Fair comparison:** separate, prospective ranking-only and full-policy reference portfolios with actual allocated quantities/cash decisions; report frequency, exposure, unresolved lifecycles and hypothetical paper net separately from real broker P/L (unobserved).

## Tests verified on the connected Mac

- Private isolated research worktree full scoped original scanner/options/edge/provider/TQS/0DTE/Telegram/scheduler regression: **588 passed, one pre-existing pytest `asyncio_mode` configuration warning**.
- The public review worktree: **142 relevant tests passed, same pre-existing warning**.
- All original source/fixture patch files were SHA-256 matched before the private commit. No live production code was changed, no task activated and no market-data purchase made.

## Astra's requested remit

Assess the **architecture and economic engine**, especially causal forecast validation, debit/credit payoffs, uncertainty/stress assumptions, correlated account allocation, quote provenance and whether forward **hypothetical** outcomes could support a credible after-cost comparison. **Not code-review/style feedback.** Neither the 588 nor 142 tests are evidence that the strategy beats the frozen scanner. There are no verified profitable completed options trades in this research deliverable.

**Status:** Draft research PR, unmerged, manual execution only.
