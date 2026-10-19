"""
TQS v2 End-to-End Smoke Test
Runs the full pipeline against live TV API with a small stock batch.
"""
import sys
import json
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.options.tqs_v2.config import TQSv2Config
from tradingview.options.tqs_v2.market_data.tv_scanner import TVStockScanner
from tradingview.options.tqs_v2.options_data.tv_options import TVOptionsFetcher
from tradingview.options.tqs_v2.options_data.yfinance_oi import YFinanceOIEnricher
from tradingview.options.tqs_v2.options_data.iv_structure import (
    TVIVTermStructure, TVVoiLens, TVVolatilityChart
)
from tradingview.options.tqs_v2.filtering.pipeline import FilterPipeline
from tradingview.options.tqs_v2.scoring.engine import ScoringEngine
from tradingview.options.tqs_v2.ranking.engine import RankingEngine
from tradingview.options.tqs_v2.reporting.html_reporter import HTMLReporter
from tradingview.options.tqs_v2.reporting.json_reporter import JSONReporter

cfg = TQSv2Config()
OPTION_TYPE = "call"
N_STOCKS = 10  # limit for speed


def banner(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print('='*60)


# ── Stage 1: Stock universe ──────────────────────────────────────────────────
banner("Stage 1: Stock Universe Fetch")
scanner = TVStockScanner(cfg)
try:
    stocks = scanner.fetch_quality_stocks()
    stocks = stocks[:N_STOCKS]
    print(f"  Got {len(stocks)} stocks")
    for s in stocks:
        print(f"    {s.symbol:8s}  close={s.close:8.2f}  cap={s.market_cap/1e9:.1f}B  RSI={s.rsi:.0f}  rec={getattr(s,'recommendation_mark','?')}")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

# ── Stage 2: Options chain fetch ─────────────────────────────────────────────
banner(f"Stage 2: Options Chain Fetch ({OPTION_TYPE.upper()})")
fetcher = TVOptionsFetcher()
all_raw = []
for s in stocks:
    try:
        contracts = fetcher.fetch_contracts(s, option_type=OPTION_TYPE, config=cfg)
        all_raw.extend(contracts)
        print(f"  {s.symbol:8s}: {len(contracts):3d} contracts")
    except Exception as e:
        print(f"  {s.symbol:8s}: ERROR - {e}")

print(f"\n  Total raw: {len(all_raw)} contracts")
if not all_raw:
    print("  NO CONTRACTS — check TV cookie, DTE filter, moneyness band")
    sys.exit(1)

# Sample contract inspection
c0 = all_raw[0]
print(f"\n  Sample contract: {c0.opra_code}")
print(f"    strike={c0.strike}  expiration={c0.expiration}  dte={c0.dte}")
print(f"    bid={c0.bid}  ask={c0.ask}  mid={c0.mid:.2f}  spread_pct={c0.spread_pct:.1f}%")
print(f"    iv={c0.iv:.1%}  delta={c0.delta:.3f}  gamma={c0.gamma:.4f}")
print(f"    theta={c0.theta:.4f}  vega={c0.vega:.4f}  rho={c0.rho:.4f}")
print(f"    bid_iv={c0.bid_iv:.3f}  ask_iv={c0.ask_iv:.3f}  theo_price={c0.theo_price:.2f}")
print(f"    volume={c0.volume}  atm={c0.atm}  moneyness={c0.moneyness_pct:.1f}%")

# ── Stage 3: OI Enrichment ───────────────────────────────────────────────────
banner("Stage 3: OI Enrichment (yfinance)")
oi_enricher = YFinanceOIEnricher()
try:
    enriched_raw = oi_enricher.enrich(all_raw, config=cfg)
    oi_set = [c for c in enriched_raw if c.open_interest > 0]
    print(f"  {len(enriched_raw)} contracts, {len(oi_set)} with OI>0")
    if oi_set:
        sample_oi = oi_set[0]
        print(f"  Sample OI: {sample_oi.opra_code} OI={sample_oi.open_interest}")
except Exception as e:
    print(f"  OI enrichment FAILED: {e}")
    enriched_raw = all_raw

# ── Stage 4: IV Term Structure ───────────────────────────────────────────────
if cfg.enable_iv_term_structure and stocks:
    banner("Stage 4: IV Term Structure")
    iv_fetcher = TVIVTermStructure()
    for s in stocks[:3]:
        try:
            ts = iv_fetcher.enrich(s.symbol, s.exchange, cfg)
            print(f"  {s.symbol}: iv_1w={ts.get('iv_1w')} iv_1m={ts.get('iv_1m')} iv_3m={ts.get('iv_3m')} slope={ts.get('iv_term_slope')}")
        except Exception as e:
            print(f"  {s.symbol}: ERROR {e}")

# ── Stage 4b: Voi-Lens + Volatility Chart ────────────────────────────────────
if cfg.enable_vol_enrichment and stocks:
    banner("Stage 4b: Voi-Lens + Volatility Chart")
    voi = TVVoiLens()
    vc = TVVolatilityChart()
    for s in stocks[:3]:
        try:
            voi_data = voi.fetch(s.symbol, s.exchange, cfg)
            print(f"  {s.symbol} voi-lens: {len(voi_data)} expiries in response")
            if voi_data:
                first_exp = next(iter(voi_data))
                strikes_data = voi_data[first_exp]
                total_call_vol = sum(v['call_vol'] for v in strikes_data.values())
                total_put_vol = sum(v['put_vol'] for v in strikes_data.values())
                pcr = total_put_vol / max(1, total_call_vol)
                print(f"    exp={first_exp}: call_vol={total_call_vol} put_vol={total_put_vol} PCR={pcr:.2f}")
        except Exception as e:
            print(f"  {s.symbol} voi-lens ERROR: {e}")

        try:
            # Get first expiration from contracts
            exps = sorted(set(c.expiration for c in all_raw if c.root == s.symbol))
            if exps:
                smile = vc.fetch_smile(s.symbol, s.exchange, exps[0], cfg)
                print(f"    {s.symbol} smile exp={exps[0]}: skew={smile.get('skew')}")
        except Exception as e:
            print(f"  {s.symbol} vol-chart ERROR: {e}")

# ── Stage 5: Filtering ────────────────────────────────────────────────────────
banner("Stage 5: Contract Filtering")
fp = FilterPipeline(cfg)
filter_result = fp.apply(enriched_raw)
print(f"  Input:   {filter_result.total}")
print(f"  Passed:  {filter_result.passed}")
print(f"  Rejected: {filter_result.rejected}")
print(f"  Rejection reasons:")
for reason, count in sorted(filter_result.rejection_reasons.items(), key=lambda x: -x[1]):
    print(f"    {reason}: {count}")

if not filter_result.contracts:
    print("\n  NO CONTRACTS PASSED FILTERS — adjust thresholds")
    sys.exit(0)

# ── Stage 6: Scoring ──────────────────────────────────────────────────────────
banner("Stage 6: Scoring (7 Dimensions)")
scorer = ScoringEngine(cfg)
scored_contracts = scorer.score_all(filter_result.contracts, option_type=OPTION_TYPE)
above_threshold = [s for s in scored_contracts if s.breakdown.total >= cfg.min_score_for_report]
print(f"  Scored: {len(scored_contracts)} | Above threshold ({cfg.min_score_for_report}): {len(above_threshold)}")

# Distribution
buckets = {"<55": 0, "55-65": 0, "65-75": 0, "75-85": 0, "85+": 0}
for sc in scored_contracts:
    t = sc.breakdown.total
    if t < 55:
        buckets["<55"] += 1
    elif t < 65:
        buckets["55-65"] += 1
    elif t < 75:
        buckets["65-75"] += 1
    elif t < 85:
        buckets["75-85"] += 1
    else:
        buckets["85+"] += 1
for k, v in buckets.items():
    print(f"    {k}: {v}")

# ── Stage 7: Ranking ───────────────────────────────────────────────────────────
banner("Stage 7: Ranking")
ranker = RankingEngine(cfg)
ranked = ranker.rank(above_threshold)
print(f"  Ranked: {len(ranked)}")

print("\n  TOP 10 PICKS:")
for r in ranked[:10]:
    sc = r.scored
    c = sc.contract
    b = sc.breakdown
    print(f"  #{r.rank:2d} {c.underlying_symbol} {OPTION_TYPE.upper()} ${c.strike} exp={c.expiration} | Score={b.total:.1f} ({b.grade})")
    print(f"       bid=${c.bid:.2f} ask=${c.ask:.2f} iv={c.iv:.0%} delta={c.delta:.2f} dte={c.dte}d OI={c.open_interest}")
    print(f"       Liq={b.liquidity:.0f} IV={b.iv_analysis:.0f} Prob={b.probability:.0f} RR={b.risk_reward:.0f} WR={b.win_rate:.0f} Vol={b.volatility_conditions:.0f} PE={b.premium_efficiency:.0f}")
    print()

# ── Stage 8: Reports ──────────────────────────────────────────────────────────
banner("Stage 8: Generating Reports")
html_out = cfg.reports_dir / cfg.call_html_report
json_out = cfg.reports_dir / cfg.call_json_report
cfg.reports_dir.mkdir(parents=True, exist_ok=True)

try:
    html_reporter = HTMLReporter(cfg)
    html_reporter.write(ranked, option_type=OPTION_TYPE, path=html_out)
    print(f"  HTML: {html_out}")
except Exception as e:
    print(f"  HTML report FAILED: {e}")

try:
    json_reporter = JSONReporter(cfg)
    json_reporter.write(ranked, option_type=OPTION_TYPE, path=json_out)
    print(f"  JSON: {json_out}")
except Exception as e:
    print(f"  JSON report FAILED: {e}")

# ── Done ──────────────────────────────────────────────────────────────────────
banner("E2E Smoke Test COMPLETE")
print(f"  Stocks scanned:       {len(stocks)}")
print(f"  Raw contracts:        {len(all_raw)}")
print(f"  After OI enrichment:  {len(enriched_raw)}")
print(f"  After filtering:      {filter_result.passed}")
print(f"  Above threshold:      {len(above_threshold)}")
print(f"  In ranked report:     {len(ranked)}")
print()
print("  Results saved to:")
print(f"    {html_out}")
print(f"    {json_out}")
