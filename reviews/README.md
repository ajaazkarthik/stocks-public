# Options edge-engine — public review

**Start here:** [Independent pre-Astra findings](./OPTIONS_INDEPENDENT_PRE_ASTRA_REVIEW_2026-10-26.md). The [research implementation report](./OPTIONS_NO_PURCHASE_EXISTING_DATA_SHADOW_2026-10-26.md) and [code-review guide](./OPTIONS_EDGE_ENGINE_REVIEW_2026-10-26.md) provide context.

The public branch now contains the frozen paper scanner, expected-net edge engine, existing-data shadow analysis, reference reprice implementation, supporting source-quality code and offline tests. No credentials, personal trading records or proprietary raw vendor market data are included.

From the public repository root, install Python 3.12+ and the test dependencies, then run:

```bash
python -m pytest -q tests/test_options_pre_astra_independent_review.py tests/test_options_edge_engine.py tests/test_options_edge_ledger.py tests/test_options_quote_forecast_evidence.py tests/test_existing_data_shadow.py tests/test_legacy_iv_evidence.py tests/test_shadow_repricing.py tests/test_shadow_factor_risk.py
```

**Verified on the public checkout: 86 passed, 2 xfailed.** The two expected failures reproduce known unresolved research issues: (F-01) post-horizon/expiry marks accepted as reference reprices; (F-02) ranking omitted when more than twelve verticals qualify. Do not mistake expected failures for fixed code or model P&L for real fills.

This is a public research review, not a live scanner update. Broker orders, Telegram and production jobs are not changed by this branch.
