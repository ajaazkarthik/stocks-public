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
