# Astra architecture review round 2 — post-integration gaps (2026-10-26)

**Source provenance:** two photographs of Astra's approximately 38–40-line follow-up architecture assessment displayed in Notepad++, furnished by the user. The photographed review cites public branch revision `9cdc8ef` and assesses the *economic experiment*, not line-by-line code. This summary is grounded in visible screenshot text and direct source inspection of `stocks-public:review/options-edge-engine-20261026`, at `9cdc8effbc15fdb9016469be53d0424131880e6a`, files `architecture_research_integration.py`, `architecture_paired_evaluation.py`, `architecture_forecast_validation.py`, and `architecture_allocation.py`. Do not claim the screenshot contains text beyond the visible portion or that a new performance trial was run.

## Verdict

**“The architecture has improved, but the integrated experiment still does not reliably answer whether the engine beats the frozen scanner.”** Keep research modules and continue **without buying historical options data**, but prioritize experiment integration and parity over new forecasting features. Original F-01/F-02 code fixes remain completed, the PRs remain DRAFT/UNMERGED, and no profitable after-cost option outcome has been demonstrated. Prior 574 passing private and 128 passing public tests are engineering checks, not evidence of outperformance.

## Round-two findings, code verification, and required acceptance

### R2-01 / P0 — study selection not actually determined by portfolio allocation

**Astra:** `architecture_research_integration.assemble()` calculates `allocation=allocate(candidates,...)` but calls `start(...,baseline=comparison['baseline']['candidate'],challenger=comparison['selection']['candidate'])` for BOTH `ranking_only` and `full_policy`. The actual quantities and contracts in `allocation['positions']` are not passed to `start`. Hence the so-called full-policy study is still a single-spread selection and does NOT measure integer allocation, cash/abstention, quantities, or price-limit participation. Similarly “ranking only” inherits upstream pre-selection/edge filters; the experiment cannot separate *ranking* from *participation conditions*.

**Acceptance:** Define experiment arms as explicit portfolios of immutable `{contract,qty,decision_time,entry_limit/cost,capital}`. For **ranking-only**, hold frozen eligibility, participation, quantity/cash, costs, available information, and frozen exits fixed and change only the ranking choice. For **full-policy**, apply the new price-specific acceptance, CASH, and **actual returned allocator positions/quantities** before constructing the new arm; keep frozen baseline unchanged. Verify a test where allocator selects two contracts instead of the single `comparison['selection']`; recorded full-policy positions must actually equal allocator output. A cash abstention from the allocator must generate CASH, not a theoretical entry in a discarded candidate.

### R2-02 / P0 — reference exits do not replay frozen policy

**Astra:** `architecture_paired_evaluation.reference_cross(... phase='exit')` checks exact quotes, clocks, max 21-day hold, 7DTE and applies two-leg quote cross; it does **not** apply frozen stop, target, trend, short-leg or other original exit rules. It omits the frozen slippage assumption in its simple crossing. `summarize` treats matching exit timestamps as ranking-only comparability, but different spreads can correctly trigger the **same rule at different times**. Existing freeze-policy vs quote-cross experiment is not frozen-strategy P/L.

**Acceptance:** Replay the **same frozen policy lifecycle** separately for each arm over an identical **chronological future observation stream**, including pending entry confirmation, short-leg policy, stop/target/trend, max holding period, 7-DTE and expiry, fee/slippage assumptions, unresolved missing bars/leg quotes. Exit times can differ; exit **rules and available observation stream** must match. Preserve `REFERENCE_ONLY` vs simulated-fill vs actual-broker-fill evidence classes. Never infer that a stop was not reached from one daily close.

### R2-03 / P0 — unfair forecast information vintage

**Astra:** `architecture_forecast_validation.make_vintage()` gives the unchanged/momentum arms the last completed daily close, while `existing_data_shadow.build_reference_forecast()` initializes historical paths using a current underlying option/spot quote when present. If completed prior close is 100 and decision-time live quote is 110, one arm starts at 110 and the other at 100; the challenger is credited for a move *observed before forecast creation*.

**Source confirmation:** public `architecture_research_integration.assemble` passes `observed_spot=observation['features']['close']`; the original `existing_data_shadow` uses a quote-derived spot when present. `architecture_forecast_validation.make_vintage()` builds unchanged/momentum baselines from `observed_spot`. Compare initial values to underlying spot of projected path to ensure all are equal at the decision clock.

**Acceptance:** Capture one shared exact decision `{spot,underlying_asof,option_contracts,IV_snapshot,quote_provenance}` for ALL forecast arms; use the last completed close only as a **historical feature**, not as a different baseline's starting price. Add a 100→110 counterexample test and ensure all models start at 110 before making divergent future projections. No cross-vendor asynchronous spot substitution without explicit proof of alignment. Do not allow late lookup data to retroactively change prior forecasts.

### R2-04 / P1 — risk budget approximation does not cover existing positions' stress

**Astra:** `architecture_allocation.allocate` computes a whole-contract/cash/risk constrained allocation but `assemble` supplies one underlying universe at a time, caps candidates `max_qty=1`, and uses `stress_losses={'modelled_crash':risk,'modelled_vol_widening':liquidation}` per candidate, often assumed worst losses rather than **common shared market outcomes**. `existing_reserved_risk` reduces available risk, but does not supply actual incumbent positions' **stress scenario loss vectors**. The large-candidate branch identifies itself as **HEURISTIC_NOT_PROVEN_OPTIMAL**, not true optimization.

**Acceptance:** Either explicitly name the component **allocation of newly proposed positions within externally computed remaining portfolio budgets** and do NOT claim account-level optimized performance, OR supply the complete account book, consistent SPY/QQQ/IWM common price/IV shock paths and existing-position stress/collateral exposures. For research full policy, allocations must use all selected eligible SPY/QQQ/IWM opportunities from the same decision window, not independent per-symbol budgets. Document quantity and broker-availability limits; preserve 'heuristic' vs 'exact' status.

### R2-05 / P1 — missing matured forecast outcomes hidden by matched-only score

**Astra:** `architecture_forecast_validation.match_future` accepts reference observations up to 3 days after target; `compare` summarizes matched pairs and dates but does NOT report a complete **matured forecast denominator**, missing/invalid exact-contract outcomes, observation delay, and date-block uncertainty. Returning a mean spot/IV error on available matches can silently drop hard-market or illiquid dates.

**Acceptance:** For each forecast vintage with target time at/before a documented evaluation cutoff, include complete list and counts by target date and contract: `MATURED_MATCHED, MATURED_MISSING, MATURED_INVALID, TOO_LATE, NOT_YET_MATURE`, with observed latency. Explicitly separate target-time observation from 1–3 day delayed observation; never quietly score them as exact-horizon identical. Report errors with date-block aggregation, matched count, missing fraction and naive baseline. Abs spot/IV forecast error alone is NOT proof of scenario-calibrated or stop-dependent option-spread profitability.

## Required research experiment order

1. **Experiment identity and common starting information (P0)** — define same observation clock, shared spot/IV snapshot, candidate/entry/quantity plan *before* any future evidence.
2. **Actual allocator-led decisions (P0)** — independent frozen-ranking-only and true full-policy portfolios, including cash and integer quantity.
3. **Frozen policy replay (P0)** — same future chronological reference stream and **identical rules**; allow arm-specific exit timestamps; unresolved outcomes preserved.
4. **Forecast coverage and joint stress controls (P1)** — record all matured vintages, delays/missing outcomes, actual account incumbent stress or explicitly name external-budget-only approximation.
5. **Only then compare outcomes** against unchanged scanner under matched capital/risk/cost, with naive/cash controls. No claim of calibrated alpha or real fills based on proxy outcomes.

## What is and is not completed

- **Done before this review:** modules for model forecast vintages, a reference quote-cross evaluator, research allocator and integrator exist; architecture is improved.
- **Not done / not validated by this review:** common initial forecast information; allocated portfolio actually drives both studies; frozen state-machine replay; incumbent portfolio stress; comprehensive matured outcome denominator; empirically positive trading results.
- **No paid data:** reuse TradingView scan2, Yahoo, free Cboe, Moomoo, TQS, existing options flow and archive. Missing reference data means unresolved outcomes, NOT software-development stop.
- **Operational boundary:** No production scanner/scheduler/Telegram/broker changes, no live trading, no merge until separate authorization. Astra is asked to assess the **economic experiment** and architecture, not perform ordinary code review.

**Source:** user-provided two Astra-review screenshots dated 26 October 2026 and current public review code. This file documents review findings/acceptance criteria; it does **not** implement the five changes or claim the new experiment has been run.
