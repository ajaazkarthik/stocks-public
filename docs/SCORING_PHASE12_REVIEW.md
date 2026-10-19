# Signal Stack scoring — Phase 1 and Phase 2 review

**Scope:** Only `tradingview/signal_stack.py` scoring correctness and missing-data
semantics; a focused existing assertion, isolated regression tests, and CI.
**Target:** the public review branch. The private `stocks` repository is
untouched. **This is NOT a validated trading-performance improvement.**

## Phase 1 — correctness

- Reject `NaN`, `Infinity`, nonnumeric values, and implausible percentage
  ranges before technical/fundamental/sentiment/internals scoring. Invalid
  WinRate can no longer propagate to the `min(10, nan)` clamp and become 10.
- Zero debt-to-equity is valid and scores as low leverage; negative valuation
  multiples still follow the prior valuation policy.
- Current ratio 0 is a measured weak liquidity outcome, not missing.
- Standardize invalid third-party observations in L4 rather than letting
  nonfinite values corrupt PCR, yield-curve, VIX, sector or other subscores.

## Phase 2 — unavailable is not bad

- L1: missing ADX or squeeze returns 5, not an invented bearish observation.
  Valid false squeeze and zero ADX remain weak. WinRate and NetProfit retain
  the existing missing-option-weight redistribution. L1 now reports observed
  weighted coverage without applying an extra shrink to the score.
- L2: the six *simplified* Piotroski tests now track 0–6 observed criteria.
  With missing data: `5 + (10*passed - 5*observed)/6`. This yields 5 with no
  observations, retains old scoring when all six are present, and shrinks
  sparse positive/negative evidence toward neutral. Emits `piotroski_observed`
  and `piotroski_coverage`. This remains a **nonstandard six-factor
  approximation**, not the academic nine-factor Piotroski F-score.
- L3: absent analyst coverage is neutral 5; a genuine aggregate analyst
  rating from TV remains observed when `source="TV"` but count is unknown.
  `Recommend.All` is a technical-rating field, **not** a proxy for analyst
  consensus and is removed from L3 analyst fallback. Vendor numeric values
  are checked before weights and modifiers. L3 now reports weighted coverage.
  Existing earnings/news bonus values and caps are unchanged.
- Overall `_data_confidence`: counts real TV RSI/ADX fallback observations,
  accepts valid aggregate TV analyst evidence, and excludes nonfinite input.
- L4: its existing conservative extra coverage shrink is deliberately
  **unchanged** in this patch; whether to remove it is a calibration question
  requiring a controlled backtest, not an arithmetic fix. Only validation and
  source-data sanitation were changed.

## Deliberately unchanged

Default and regime-dependent L1–L4 weights, L5–L7 modifiers, sentiment
bonus values, the 1.5x score stretch, and the 0.85 confidence floor.
Model version advances to `signal-stack-2026.10.19.phase12` so comparisons
can distinguish old and corrected score distributions.

## Testing

- `python3 -m unittest discover -s tests -p 'test_signal_scoring_phase12.py' -v`
  executes actual scoring function definitions extracted from the production
  module AST; requires no broker keys, network access or extra dependencies.
- `python3 -m py_compile tradingview/signal_stack.py` validates syntax.
- Existing broader regression suites should be run in the normal development
  environment before production promotion.
- Required further validation: chronological old-vs-new signal replay with
  frozen inputs, actionability threshold movement, results by market,
  market regimes and observed-data coverage, followed by shadow mode.

**Release rule:** Review this public PR and its test results before considering
a *separately reviewed* cherry-pick of the scoring changes into private `stocks`.
Never merge the public repository history into the private original.
