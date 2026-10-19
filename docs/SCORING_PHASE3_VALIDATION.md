# Phase 3: independent score arithmetic verification (public review)

**Status:** implementation and automated checks on the existing draft PR.
No modifications to the private `stocks` repository or public `main`.

## Files
- `tests/scoring_reference_oracle.py`: independent, dependency-free formula
  specification. It never imports the production signal engine and contains
  explicit documented policy weights for L1–L7.
- `tests/test_signal_scoring_phase3.py`: executes the real production
  scorer function definitions extracted from `signal_stack.py` via AST,
  without importing side-effectful broker integrations, then independently
  recomputes and checks their component arithmetic.
- `.github/workflows/scoring-phase12.yml`: runs both Phase 1–2 and Phase 3
  suites, and compiles the changed Python modules on each review-branch push.

## Checks performed
1. Technical L1: core weights, the 32 optional-field availability masks,
   optional weights returning to WinRate and NetProfit, a hand-calculated
   9.88/10 all-positive case, and missing-data coverage.
2. Fundamental L2: all emitted subscores, FCF-yield present/missing branches,
   empty, favorable, unfavorable and mixed raw inputs.
3. Sentiment L3: independent reconstruction of analyst/news/beat weights,
   article-velocity shifts, positive and negative bonuses, earnings penalties,
   clipping, and absent coverage.
4. Market internals L4: both VIX term-structure weight configurations, raw
   weighted sum and coverage shrinkage, full and partial coverage.
5. Composite L1–L7: 4 VIX regimes × L5 present/absent/zero × L6
   present/absent/zero × L7 absent/zero/5/7.5/10, plus 280 seeded
   pseudo-random scenarios.
6. Confidence (0–1, including floor), extra boosts, the 1.5× stretch,
   upper/lower clamps, layer-contribution labels and the final report
   explanation's last numeric step.
7. Monotonic composite response to one L1–L4 improvement at a time when
   weights are positive and other factors are unchanged.

## Independent rounding precision

The first independent check found that algebraically identical floating-point
evaluation orders may land on opposite sides of half-cent boundaries.
Example: a NORMAL-regime combination with zero-valued but *present* L5/L6
can produce 58.02 in production versus 58.03 via the independent formula
after the 1.5× stretch. This is **not** evidence of a weight mismatch.

Therefore **all intermediate weighted and bonus arithmetic is compared
exactly**, while independently computed *display-rounded final scores* allow
a maximum **0.01** difference. The production quality, decision and report
breakdown must still reconcile **exactly** to one another; differences in
the actual published breakdown are not tolerated.

Potential improvement (separate proposal): use a documented decimal
quantization policy before the stretch to eliminate half-cent ambiguity.
This PR does not alter existing published scoring numbers merely to satisfy
the independent model.

## Not demonstrated by these tests
- Independent validation of every individual threshold-to-subscore mapping.
  The reference oracle reconstructs layer totals from emitted subscores;
  the focused Phase 1–2 suite tests key raw boundaries and invalid inputs.
- Data-provider field quality, network fetches, production-side effects,
  report HTML rendering, or a complete system integration run.
- Whether layer weights, bonuses, stretch, regime switching or confidence
  improve trade returns out of sample.

**Release rule:** Do not merge into private `stocks` until PR review, normal
full-suite testing, and Phase 4 historical/shadow evaluation are completed.
