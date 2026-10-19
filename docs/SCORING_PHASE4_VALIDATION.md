# Phase 4 — point-in-time historical replay and no-trade shadow comparison

**Status: framework implemented on public draft PR #1.**
**Historical return validation: NOT YET PERFORMED.** The public repository is
source-only; it deliberately contains no personally sensitive stock holdings,
broker records, or sufficiently complete original decision-time feature snapshots.

This work does NOT change the scoring formulas, live alerts, watchlists,
broker orders, schedulers, or private \`stocks\` repository. Never merge into
production solely because these tests are green.

## Why a new runner was necessary

\`benchmarks/score_validation_backtest.py\` simulates a momentum/volume *proxy*,
not the actual four-layer score. \`tradingview/backtest/outcome_study.py\`
evaluates previously stored outcomes by model version but does not rescore
the **same decision-time observations** under both model implementations.
Neither establishes that Phase 1–3 improved the signal rankings.

\`benchmarks/phase4_scoring_replay.py\` now loads the original public baseline
Git commit \`f3925b3\` (scoring blob \`d02df819\`) and the reviewed scoring
branch **as AST-extracted functions** (not side-effectful production imports).
It evaluates the same archived features for both versions and reports
individual scores, a top-25% selection comparison, score-band calibration,
data-coverage stratification, rank correlation, absolute and benchmark-excess
underlying-stock returns, score-gate crossings and symbol-cluster bootstrap
intervals. Each US/AU/IN market, 5/10/20 calendar-day horizon, and
chronological train/holdout split is reported separately.

An evaluation group requires at least **30 matured benchmarked observations
and 10 distinct symbols**; otherwise performance is withheld as
\`INSUFFICIENT_DATA\`. Confidence bands require the same floor for gap
calculations. A report with no valid historical data must not claim an edge.

## Only two modes

**Historical**: read-only score-and-outcome replay; **never** market orders.
**Shadow**: compare old/new scores without outcomes and emit JSONL differences;
it does **not** send alerts or modify a live scheduler. Running the CLI from
your own Mac is necessary to use archived private observations.

### Snapshot contract (one JSON object per line)

\`\`\`json
{
  "event_id": "unique-id-123",
  "market": "AU",
  "symbol": "ASX:EXAMPLE",
  "signal_at": "2026-07-01T09:00:00+10:00",
  "captured_at": "2026-07-01T09:06:00+10:00",
  "decision_at": "2026-07-01T09:07:00+10:00",
  "source_asof": {
    "meta": "2026-07-01T09:06:00+10:00",
    "screener": "2026-07-01T09:06:00+10:00"
  },
  "meta": {"WinRate": 65, "NetProfit%": 23},
  "screener": {"tv_rsi": 62, "tv_adx": 25},
  "fundamental": {},
  "sentiment": {},
  "internals": {},
  "yc": {},
  "vix": null
}
\`\`\`

The above is a *schema example only*, not a historical observation.
\`signal_at <= captured_at <= decision_at\`; each nonempty feature bundle
must have a separately recorded \`source_asof\` timestamp no later than
\`captured_at\`. Optional \`l5\`, \`l6\`, \`l7\`, \`regime_weights\`, or nonzero
\`book_bonus\`, \`pool_boost\`, \`basket_boost\` require matching timestamps.
No backwards-inferred old fundamentals, retrospectively recomputed sentiment,
or today's quoteSummary fields may be substituted for those original values.

A missing nonempty source timestamp, a future vendor observation, a timestamp
without timezone or an invalid numeric outcome is fatal. No silent lookahead.

### Outcome contract (one JSON object per event and horizon per line)

\`\`\`json
{
  "event_id": "unique-id-123",
  "horizon_days": 5,
  "entry_at": "2026-07-01T09:10:00+10:00",
  "exit_at": "2026-07-06T10:00:00+10:00",
  "available_at": "2026-07-06T10:30:00+10:00",
  "entry_price": 100.0,
  "exit_price": 103.0,
  "benchmark_entry": 100.0,
  "benchmark_exit": 101.0
}
\`\`\`

These are synthetic example numbers. Historical entry must occur strictly
AFTER decision. Outcomes must mature at least the requested number of
**calendar** days after decision, must be available no later than \`--as-of\`,
and for the train split the outcome must have been available no later than
the chosen cutoff. Records falling in the embargo window are excluded.
Market benchmarks must be from contemporaneous, comparably timed price bars;
any unavailable matched index result is recorded as missing, never assumed zero.

Round-trip costs are optionally deducted from stock returns using \`--cost-bps\`.
This is *not* an options-premium, greeks, leverage, 0DTE, or fills simulation.
Results are descriptive and do not account for capacity, slippage under
stress, survivorship bias, dividends or execution constraints unless the
provided prices and cost assumptions explicitly include those effects.

### Run locally (do not commit raw archived data)

\`\`\`bash
git clone https://github.com/ajaazkarthik/stocks-public.git
cd stocks-public
git fetch origin review/phase1-phase2-scoring-correctness
git switch -c review/phase1-phase2-scoring-correctness --track origin/review/phase1-phase2-scoring-correctness

# Old scorer pinned and integrity checked by the CLI itself.
git show f3925b3cc7bd4f74f61f6f5e8edb0e3d7fbbfe9f:tradingview/signal_stack.py > /tmp/stocks-original-scoring.py

# First run without any outcomes, to check your archived snapshot schema.
python3 benchmarks/phase4_scoring_replay.py \
  --mode shadow --baseline-source /tmp/stocks-original-scoring.py \
  --candidate-source tradingview/signal_stack.py \
  --snapshots /path/to/PRIVATE/point_in_time_signals.jsonl \
  --as-of 2026-10-19T17:00:00+11:00 \
  --out /path/to/PRIVATE/phase4_shadow

# Only once genuine recorded inputs and matured outcomes exist:
python3 benchmarks/phase4_scoring_replay.py \
  --mode historical --baseline-source /tmp/stocks-original-scoring.py \
  --candidate-source tradingview/signal_stack.py \
  --snapshots /path/to/PRIVATE/point_in_time_signals.jsonl \
  --outcomes /path/to/PRIVATE/matured_prices.jsonl \
  --cutoff 2026-08-01T00:00:00+10:00 \
  --as-of 2026-10-19T17:00:00+11:00 \
  --cost-bps 20 --out /path/to/PRIVATE/phase4_results
\`\`\`

Inputs, outputs and reports **must stay local/private**, not be committed to
\`stocks-public\`. Only synthetic fixture tests run in GitHub Actions.

### Reporting and interpretation

Outputs: \`phase4_summary.json\`, \`phase4_scores.jsonl\`,
\`phase4_summary.md\`.

- **Score correction:** distribution shifts, 62.5 threshold migrations, and
  rank correlation old vs new.
- **Discrimination:** direction and magnitude of rank-vs-excess-return
  association, band monotonicity, and top-quartile net excess returns.
- **Coverage fairness:** compare sparse, partial and well-observed sources.
- **Markets/time:** independent results by US/AU/IN and 5/10/20-day horizon
  in training versus untouched holdout (with a time embargo).
- **Uncertainty:** symbol-cluster bootstrap, min observations and min symbols.
  Interval excluding zero indicates an *association in this sample*, **not**
  a promise of future return improvement.
- **Selection bias warning:** use all point-in-time ranked candidates including
  rejected ones, not only historically successful trades. Stock-price histories
  must include delisted names; index changes must not introduce survivorship.

Do not tune weights based on the holdout set. Pre-register sample windows,
evaluation thresholds, broker cost assumptions and actionable criteria;
if the holdout is inspected and rules are changed, reserve an entirely later
untouched period for subsequent testing.

## Remaining work before declaring Phase 4 *empirically complete*

1. Locate or capture genuine **archived original scoring input dictionaries**
   for BOTH winning and rejected candidates; verify source timestamps. An
   archived score alone is insufficient to rescore L1–L4.
2. Privately supply contemporaneous matured prices + matching index prices.
3. Run the same frozen cohort through the CLI and review group sample sizes,
   embargo exclusions, score migrations and uncertainty intervals.
4. Schedule local shadow captures on future signals (opt-in only), and after
   enough matured observations, repeat the empirical evaluation.
5. Conduct standard full application integration tests, peer code review and
   publication security review before selectively cherry-picking into private
   \`stocks\`. No automatic private merge.

**CI:** \`python3 -m unittest discover -s tests -p 'test_phase4_scoring_replay.py' -v\`
uses **synthetic** fixtures plus the *actual pinned old and candidate scorer*
in a no-broker, no-network test. It verifies leak rejection, cutoff embargo,
actual source loading, decision-score comparison and deliberately underpowered
historical output. Passing CI establishes code behavior, not historical alpha.
