# Independent pre-Astra review — existing-data-only options edge shadow

**Reviewed:** 26 October 2026 · original private source repository · published public branch [review/options-edge-engine-20261026](https://github.com/ajaazkarthik/stocks-public/tree/review/options-edge-engine-20261026), based on an unmerged private development draft, original implementation head `b884d91d773e35896e502e81e7ea3cc1809bdc41` · reviewed basis [OPTIONS_NO_PURCHASE_EXISTING_DATA_SHADOW_2026-10-26.md](./OPTIONS_NO_PURCHASE_EXISTING_DATA_SHADOW_2026-10-26.md).

**Reviewer identity / independence:** This is a second, source-level code and reproducibility review by ChatGPT, separate from and **NOT** an Astra invocation, external human expert, independent licensed market-data certification, or profitable-trade audit. Astra has **not** supplied feedback here. The review checked original source and code, tests, synthetic reproductions on the authorized connected Mac, and master research decisions. It is included in this PUBLIC review package so any reviewer can reproduce the independently identified findings.

## Executive decision

**Engineering proof-of-concept: functionally useful and appropriately marked RESEARCH_ONLY. Two reproducible correctness deficiencies mean PR #34 is suitable for review but NOT ready to be promoted to a dependable matched-selection outcome comparator.** Keep the original private research DRAFT, unmerged, no trading orders, no Telegram strategy alerts, no automatic collector and no paid data. Do NOT treat historical options data as a blocker for fixing either issue.

- **532/532 earlier relevant tests PASS** (frozen paper, edge engine, multiple sources, TQS, ZeroDTE, original paper notifier and scheduler). However the old tests did not cover the two conditions below.
- Added **2 deterministic, expected-to-fail regression characterizations** in `tests/test_options_pre_astra_independent_review.py`, marked `@pytest.mark.xfail(strict=True)`: **2 XFAIL, 0 unexpected passes** on Mac. They document CURRENT real failures and should become ordinary passing assertions after repairs.
- Master/source-first decisions are respected: no purchase, reuse TV scan2 + Yahoo + FREE Cboe + Moomoo + existing TQS/ZeroDTE/SQLite, keep quote-event times separate from receipts, no claimed option profitability.
- Verified original frozen `paper_scanner.py`, `paper_scanner_data.py`, scheduled strategy, live Telegram and production DB were not modified by PR #34. No live sent orders/paper trade profitability demonstrated.

## F-01 · HIGH — follow-up quotes can be accepted beyond original holding limit AND past expiry

**Code:** `tradingview/options/shadow_repricing.py::one_arm` and `reconcile`; compare `tradingview/options/paper_scanner.py::advance` and `POLICY[max_hold_days=21,exit_dte=7]`.

`one_arm` checks the exact two option keys, reference quote basis and freshness **only**, then calculates `candidate['indicative_cash'] + theoretical reference close cash - fees`. It does NOT enforce decision-to-entry state, 30-minute pending-entry deadline, the frozen 21-**calendar**-day max hold, 7-DTE exit, previously triggered stop/target/trend exit, or option expiry. `reconcile` admits any receipt later than the decision and has no upper time bound. `paper_scanner.advance` explicitly stops at expiry and follows real frozen position state; the new reprice module does not reuse that state machine.

**Independently reproduced** with deterministic original synthetic fixture (not vendor prices):
- Decision: **2026-10-26 15:00 UTC**, one eligible synthetic SPY BULL CALL DEBIT, option expiration **2026-12-04**.
- Follow-up at **2026-11-17 15:00 UTC** (**22 calendar days later**, already beyond 21-day max hold): `one_arm(...)` returned **`('UNVERIFIED_REFERENCE_MARK', -14.6)`**.
- Follow-up at **2026-12-07 15:00 UTC** (**42 days after entry, 3 days after expiry**): **same reference mark `-14.6`**. This is a real code defect despite its NONEXECUTABLE label; it can populate an economically invalid paired comparison row.

**Impact:** Research-only quote-to-quote P/L can be calculated when the frozen control would already have exited or become unresolved. This biases a baseline-vs-edge diagnostic. It does NOT trigger a broker trade and does NOT fabricate `realized_net_pnl` (still `NOT_MEASURED`), so production safety is preserved.

**Required before promotion:** Reject reference marks after max holding/mandatory 7DTE exit and after expiration. For true baseline-vs-edge exit comparability, replay the SAME original pending-entry/stop/target/exit state machine at each *available future same-clock exact-leg quote*; missing quotes imply MISSING/UNRESOLVED, not retroactive fills. Store `decision_at`, hypothetical entry, exit event, reason and explicit option non-fill evidence separately. Until then label all arbitrary later marks **snapshot-to-snapshot sensitivity only**, not matched strategy returns.

**Repro:** `python -m pytest -rxq tests/test_options_pre_astra_independent_review.py::test_hypothetical_reference_mark_not_allowed_after_expiry`.

## F-02 · HIGH for research completeness — silent 12-candidate ceiling prevents challenger comparison

**Code:** `tradingview/options/existing_data_shadow.py::safe_comparison` passes `max_candidates=12`; `tradingview/options/edge_engine.py::compare_frozen_to_edge` returns `CANDIDATE_SET_EXCEEDS_RESEARCH_LIMIT` and an EMPTY ranked list whenever `len(candidates)>12`, even though the frozen chooser can choose a candidate.

**Independently reproduced** with a deterministic synthetic chain of additional BULL CALL strikes:
- Unmodified frozen `choose_candidate`: **`CANDIDATE`**, **132** mirror-eligible vertical candidates.
- `safe_comparison(obs,None,NOW)`: **baseline `CANDIDATE`** but **challenger `CANDIDATE_SET_EXCEEDS_RESEARCH_LIMIT`**, `candidate_rank_rows=0`, no selected opportunity.
- This does NOT establish that 132 spreads existed on the real exchange or that any would be profitable. It demonstrates a deterministic processing shortfall under a larger, otherwise valid synthetic exact-strike universe.

**Impact:** Exactly when a chain has more available spreads, ranking vanishes instead of producing a bounded transparent comparison. The user asked to implement the challenger even with existing reference data, so the choice of an arbitrary 12 threshold should be explicit and not create a false absence of edge.

**Required before promotion:** Preserve the full eligible set and baseline control counts, then batch a deterministic feasible candidate list with an auditable limit, optional interval feasibility/cheap computation, and a clearly described `NOT_ALL_CANDIDATES_VALUED` state if CPU budget prevents full scan. Do NOT automatically truncate to the 12 cheapest, as that could change the underlying economic choice and introduce selection bias. Add tests for 13/50/100+ candidates.

**Repro:** `python -m pytest -rxq tests/test_options_pre_astra_independent_review.py::test_many_eligible_spreads_can_still_receive_complete_shadow_ranking`.

## F-03 · MEDIUM — “all existing sources integrated” overstates what is actually queried

**Code:** `existing_data_shadow.run_symbol` calls `paper_scanner_data.fetch_full_research_observation` which calls `paper_research_sources.compare_all_sources`.

**Verified present:** TradingView scan2 native price data, Yahoo exact contract/OI + underlying history, Cboe `CboeOIEnricher` exact OI/volume, Moomoo **`get_market_snapshot`** exact option, TQS liquidity/premium scores, TV expiry smile, optional TV ZeroDTE lens. Those already enable valuable source enrichment and satisfy the no-buy engineering objective.

**Not actually invoked in this runner:** The **FREE full Cboe delayed IV/Greeks/bid/ask fields** from `trading-api-mcp/server/src/tools/cboe_quote.ts`; the correct Moomoo `subscribe(SubType.ORDER_BOOK)` then `get_order_book` bid/ask **size/receipt-time** path; and the optional **TQS OPRA WebSocket** option quote-size/volume stream. The master source-first audit documents these APIs, but the runner's claim of complete source coverage is **only true of its existing bounded adapters**, not of every installed options feed and every useful field. No side-event timestamps can be promoted from these sources merely because the APIs are connected.

**Recommendation:** Either (a) add separate read-only opt-in adapter status rows for these existing feeds, with explicit `queried / available / as-of / matched / reference-only / no fill` state; or (b) narrow the report/CLI description to **“existing five-source comparison subset”** and document which local feeds remain unqueried. Never pay for data to resolve a naming/integration issue; no credentials or live bot changes needed.

## F-04 · MEDIUM — IV assumption and sparse scenario cadence limit comparability, not deployment safety

**Code:** `existing_data_shadow.build_reference_forecast`, `edge_engine._exit_path`, `shadow_factor_risk`.

Eight nearest **historical underlying** price-return paths over a 21-calendar-day holding horizon are a reproducible sensitivity mechanism. They are not independent complete future options-IV paths: future option IV is assigned a clipped uniform relative shift of current per-leg IV based on future historical *realized vol*, constant through each path, with zero assumed dividends/rates/funding/assignment cost; overnight/intraday quote changes and exercise probabilities are not observed. Existing edge engine marks the projection **COARSE_RESEARCH_ONLY**, `REFERENCE_PROXY_UNVERIFIED`, `live_executable=False`, and **NOT_MEASURED**—good safeguards. `shadow_factor_risk` measures correlated underlying returns (not correlated option-leg risk).

**Recommendation:** Keep the above caveats in every study output and require options-level, next-quote or full scenario sensitivity comparisons before promoting any `MODEL_ONLY_PRICE_CEILING` to an actionable alert. This is not a purchase/blocker recommendation. Do not use the 1,145 short-horizon IV archive pairs as a calibrated 21-session full smile (407 holdout pairs across only 3 dates, undercoverage and worse-than-unchanged-IV MAE); the existing report correctly avoids this claim.

## Positive controls checked

- The user-funded-source/no-purchase directive is honored; `scan2`/Yahoo/free Cboe/Moomoo and local history are investigated before external data.
- No modifications to frozen production policy or option entry/exit source. No new scheduler, account order, Telegram delivery or live service mutation.
- Separate owner-only research SQLite stores **REFERENCE_ONLY**, **NOT_MEASURED** and no broker-execution record; missing exact-leg future quote is not backfilled.
- Forecast generation enforces completed prior daily price data; versioned `REFERENCE_PROXY_UNVERIFIED` remains nonactionable. Tests for IV percent→fraction conversion, missing-data fail-closed and last-known daily dates are present.
- Correlation audit identifies correlated SPY/QQQ/IWM risk without claiming actual option-book P/L.
- Existing 532 tests passed before this review; this review adds two **XFAIL** demonstrations, not a claim that all correctness tests now pass.

## Astra handoff / exit criteria

**Ask Astra to review F-01 and F-02 first**, reproduce both with `tests/test_options_pre_astra_independent_review.py`, judge the proposed corrections and highlight anything this ChatGPT review missed. Then review F-03 adapter completeness/claim accuracy and F-04 IV-path economics. Do not mistake the XFAIL cases for accepted design: their markers should be removed and tests made normal PASS only after fixes.

**Do NOT merge or activate the research challenger before resolving F-01/F-02 and independent user approval.** Do not buy new data or treat missing historical OPRA options ticks as a code-development blocker. True after-cost strategy outperformance is **still NOT MEASURED**; preliminary futures session/closed-trade evidence remains a truthfulness issue, not a software task veto.

### Reproducibility

```bash
cd <local research checkout>
python -m pytest -rxq tests/test_options_pre_astra_independent_review.py
# Expected CURRENT result: 2 xfailed, 0 xpassed.
python -m tradingview.options.existing_data_shadow --audit-only
# Existing local DBs opened read-only; no changes to production scanners.
```

**Scope of independent review:** code and report inspection plus deterministic Python tests against the same branch, not an outside expert sign-off or real trading performance verification. No Astra session was started and no paid data/license was purchased.
