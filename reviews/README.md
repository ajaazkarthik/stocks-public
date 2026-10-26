# Public options engine architecture review

**Astra: review the engine ARCHITECTURE, forecasting/economic assumptions, ranking objective and portfolio risk model — NOT line-by-line code.**

**[Start here: Astra architecture and engine brief](./ASTRA_ARCHITECTURE_ENGINE_BRIEF.md)**

[Detailed F-01/F-02 engineering fixes and architecture questions](./OPTIONS_ENGINE_F01_F02_FIX_AND_ARCHITECTURE_REVIEW_2026-10-26.md) · [No-purchase implementation report](./OPTIONS_NO_PURCHASE_EXISTING_DATA_SHADOW_2026-10-26.md) · [Historical pre-fix independent findings](./OPTIONS_INDEPENDENT_PRE_ASTRA_REVIEW_2026-10-26.md)

The branch contains the complete frozen paper scanner, expected-net edge engine, shadow comparison, research source-quality tools and offline tests. Real broker fills, credentials and licensed raw vendor datasets are not included.

Run from the repository root with Python 3.12+ and the existing test dependencies:

```bash
python -m pytest -q tests/test_options_pre_astra_independent_review.py tests/test_options_edge_engine.py tests/test_options_edge_ledger.py tests/test_options_quote_forecast_evidence.py tests/test_existing_data_shadow.py tests/test_legacy_iv_evidence.py tests/test_shadow_repricing.py tests/test_shadow_factor_risk.py
```

**F-01 and F-02 should be normal PASS tests, not XFAIL.** This work is model-only, with no live trade/Telegram action or production merge.


## Astra's actual economic-design feedback (26 October 2026)

**[Read the architecture findings and prioritized no-purchase research plan](./ASTRA_ARCHITECTURE_FINDINGS_RESPONSE_2026-10-26.md).** It summarizes the four user-provided photographs of Astra's critique, without claiming the full original review text was available. Astra recommends retaining full-spread/price-limit infrastructure but redesigning forecast calibration, joint allocation and matched portfolio performance evaluation. The report proposes three **OPEN** milestones (A forecast validation, B portfolio allocator, C paired forward evaluator), not completed code. F-01/F-02 implementation bugs are already fixed. Real option profitability remains unproven; Astra's task is architecture/economic review, not routine code review.

## Updated: integrated fair experiment v2, 26 October 2026

[View implementation and verified tests](./OPTIONS_FAIR_EXPERIMENT_V2_INTEGRATION_2026-10-26.md). The previous Astra five-point experiment critique now has corresponding **research-only code and tests**: one decision-price/IV snapshot, allocator-selected portfolio/quantities/cash, original frozen lifecycle replay on identical future observations, complete forecast outcome/missingness accounting, and prospective ranking-only/full-policy reference comparisons. Private scoped regression **588 passed**, public review **142 passed** (one pre-existing pytest warning each). No production changes or actual return outperformance are established. Astra should review the economics and architecture, not code style.
