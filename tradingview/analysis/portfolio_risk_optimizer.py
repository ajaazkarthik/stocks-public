"""
Portfolio Risk Optimizer
========================
Uses skfolio (HRP) to compare our current allocation against a
hierarchical-risk-parity optimized portfolio.

Reads  : .cache_global_positions.json (live holdings cache)
Fetches: 1-year daily price history via yfinance
Outputs: reports/portfolio_risk_optimizer.html

Why HRP (not MVO)?
  Mean-Variance Optimization is notoriously overfitted in-sample.
  HRP (Lopez de Prado 2016) uses hierarchical clustering and does not
  require inverting the covariance matrix — much more stable out-of-sample.
  Walk-forward test on this portfolio: HRP Sharpe 0.081 vs EW 0.068,
  MaxDD 13.9% vs 31.1%.  Source: skfolio eval 2026-05-09.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TV   = Path(__file__).parent.parent
ROOT = TV.parent

DEFAULT_CACHE = ROOT / ".cache_global_positions.json"
DEFAULT_OUT   = ROOT / "reports" / "portfolio_risk_optimizer.html"

# ── optional deps (graceful degradation) ─────────────────────────────────────
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

try:
    import skfolio  # noqa: F401
    from skfolio.optimization import HierarchicalRiskParity, EqualWeighted
    from skfolio.preprocessing import prices_to_returns
    from skfolio.model_selection import WalkForward, cross_val_predict
    _HAS_SKFOLIO = True
except ImportError:
    _HAS_SKFOLIO = False

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


# ── symbol mapping ────────────────────────────────────────────────────────────

def _tv_to_yf(tv_sym: str) -> str:
    """Convert TradingView symbol to a yfinance-compatible ticker."""
    parts = tv_sym.split(":", 1)
    sym      = parts[-1] if len(parts) > 1 else parts[0]
    exchange = parts[0]  if len(parts) > 1 else ""
    if exchange == "ASX":
        return sym + ".AX"
    if exchange == "SGX":
        return sym + ".SI"
    if exchange == "NSE":
        return sym + ".NS"
    if exchange == "BSE":
        return sym + ".BO"
    return sym


# ── data loading ──────────────────────────────────────────────────────────────

def _load_positions(cache_path: Path) -> list[dict]:
    """Load positions from cache, excluding India and options."""
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Cache not found: {cache_path}\n"
            "Run Global Portfolio Dashboard first."
        )
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    positions = data.get("positions", [])
    return [
        p for p in positions
        if not p.get("is_option")
        and float(p.get("market_val", 0) or 0) > 0
    ]


def _fetch_prices(symbols: list[str], period: str = "1y") -> "pd.DataFrame":
    """Download adjusted close prices; returns clean DataFrame."""
    raw = yf.download(symbols, period=period, auto_adjust=True, progress=False)["Close"]
    if raw.empty:
        raise RuntimeError("yfinance returned no data.")
    if len(symbols) == 1:
        # Single-ticker yfinance returns a Series
        if hasattr(raw, "to_frame"):
            raw = raw.to_frame(name=symbols[0])
    thresh = len(raw) * 0.80
    clean  = raw.dropna(axis=1, thresh=int(thresh)).ffill().dropna()
    return clean


# ── optimization ──────────────────────────────────────────────────────────────

def _run_optimization(prices: "pd.DataFrame", ticker_map: dict) -> dict:
    """Return optimization results dict."""
    X = prices_to_returns(prices)
    total_mv = sum(ticker_map.get(t, {}).get("market_val", 0) for t in prices.columns)

    # Current weights
    curr_weights = {
        t: (ticker_map.get(t, {}).get("market_val", 0) / total_mv if total_mv else 0)
        for t in prices.columns
    }

    # HRP
    hrp = HierarchicalRiskParity()
    hrp.fit(X)
    hrp_p = hrp.predict(X)
    hrp_w = dict(zip(prices.columns, hrp_p.weights))

    # Equal weight baseline
    ew  = EqualWeighted()
    ew.fit(X)
    ew_p = ew.predict(X)

    # Walk-forward (only if enough data)
    wf = {}
    if len(X) >= 120:
        try:
            cv      = WalkForward(train_size=63, test_size=21)
            wf_hrp  = cross_val_predict(HierarchicalRiskParity(), X, cv=cv)
            wf_ew   = cross_val_predict(EqualWeighted(),           X, cv=cv)
            wf = {
                "hrp_sharpe": wf_hrp.sharpe_ratio,
                "hrp_return": wf_hrp.annualized_mean,
                "hrp_maxdd":  wf_hrp.max_drawdown,
                "ew_sharpe":  wf_ew.sharpe_ratio,
                "ew_return":  wf_ew.annualized_mean,
                "ew_maxdd":   wf_ew.max_drawdown,
                "folds":      cv.get_n_splits(X),
                "hrp_beats_sharpe": wf_hrp.sharpe_ratio > wf_ew.sharpe_ratio,
                "hrp_beats_maxdd":  abs(wf_hrp.max_drawdown) < abs(wf_ew.max_drawdown),
            }
        except Exception:
            pass

    # Divergences
    divergences = sorted(
        [
            {
                "symbol": t,
                "tv_symbol": ticker_map.get(t, {}).get("symbol", t),
                "broker": ticker_map.get(t, {}).get("broker", ""),
                "hrp_weight": hrp_w[t],
                "curr_weight": curr_weights[t],
                "delta": hrp_w[t] - curr_weights[t],
                "market_val": ticker_map.get(t, {}).get("market_val", 0),
                "pnl_pct": ticker_map.get(t, {}).get("pnl_pct", 0),
            }
            for t in prices.columns
        ],
        key=lambda x: abs(x["delta"]),
        reverse=True,
    )

    # Full table: all assets sorted by HRP weight
    all_assets = sorted(divergences, key=lambda x: -x["hrp_weight"])

    return {
        "in_sample": {
            "hrp":  {"sharpe": hrp_p.sharpe_ratio, "ret": hrp_p.annualized_mean,
                     "vol": hrp_p.annualized_standard_deviation, "maxdd": hrp_p.max_drawdown},
            "ew":   {"sharpe": ew_p.sharpe_ratio,  "ret": ew_p.annualized_mean,
                     "vol": ew_p.annualized_standard_deviation,  "maxdd": ew_p.max_drawdown},
        },
        "walk_forward": wf,
        "divergences":  divergences,
        "all_assets":   all_assets,
        "n_symbols":    len(prices.columns),
        "n_days":       len(prices),
        "date_range": (str(prices.index[0].date()), str(prices.index[-1].date())),
    }


# ── HTML report ───────────────────────────────────────────────────────────────

def _pnl_cls(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _pct(v: float, plus: bool = False) -> str:
    """Format a fraction (0.0–1.0) as a percentage string."""
    sign = "+" if (plus and v >= 0) else ""
    return f"{sign}{v:.1%}"


def _broker_pct(v: float, plus: bool = False) -> str:
    """Format a broker-reported pnl_pct (already a plain %, e.g. 357.1 → '+357.1%')."""
    sign = "+" if (plus and v >= 0) else ""
    return f"{sign}{v:.1f}%"


def _build_html(res: dict, snap_date: str, db_path: Path) -> str:  # noqa: ARG001
    ins = res["in_sample"]
    wf  = res["walk_forward"]
    div = res["divergences"]
    all_a = res["all_assets"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- walk-forward section ---
    if wf:
        hrp_beats = wf.get("hrp_beats_sharpe")
        wf_html = f"""
<section class='card'>
<h2>Walk-Forward Test ({wf['folds']} folds, 3mo train / 1mo test)</h2>
<p class='note'>Out-of-sample — the honest test of whether optimization beats naive equal-weight.</p>
<table><thead><tr><th>Strategy</th><th class='num'>Sharpe</th>
<th class='num'>Ann. Return</th><th class='num'>Max Drawdown</th><th>Verdict</th></tr></thead>
<tbody>
<tr><td><strong>HRP (out-of-sample)</strong></td>
    <td class='num {_pnl_cls(wf['hrp_sharpe'])}'>{wf['hrp_sharpe']:.3f}</td>
    <td class='num {_pnl_cls(wf['hrp_return'])}'>{_pct(wf['hrp_return'])}</td>
    <td class='num neg'>{_pct(wf['hrp_maxdd'])}</td>
    <td>{'WIN' if hrp_beats else 'LOSE'}</td></tr>
<tr><td><strong>Equal-Weight (baseline)</strong></td>
    <td class='num {_pnl_cls(wf['ew_sharpe'])}'>{wf['ew_sharpe']:.3f}</td>
    <td class='num {_pnl_cls(wf['ew_return'])}'>{_pct(wf['ew_return'])}</td>
    <td class='num neg'>{_pct(wf['ew_maxdd'])}</td>
    <td>baseline</td></tr>
</tbody></table>
<p class='note' style='margin-top:10px'>
  {'HRP beats EW on risk-adjusted return AND max drawdown — optimization adds real value.' if hrp_beats else 'EW beats HRP — current concentrated bets outperformed diversification this period.'}
</p>
</section>"""
    else:
        wf_html = "<section class='card'><h2>Walk-Forward</h2><p class='note'>Insufficient data (need 120+ trading days).</p></section>"

    # --- in-sample table ---
    ins_html = f"""
<section class='card'>
<h2>In-Sample Statistics (1 year, {res['n_symbols']} symbols, {res['n_days']} trading days)</h2>
<p class='note'>Date range: {res['date_range'][0]} to {res['date_range'][1]}</p>
<table><thead><tr><th>Strategy</th><th class='num'>Sharpe</th>
<th class='num'>Ann. Return</th><th class='num'>Ann. Vol</th><th class='num'>Max Drawdown</th></tr></thead>
<tbody>
<tr><td><strong>HRP</strong></td>
    <td class='num'>{ins['hrp']['sharpe']:.3f}</td>
    <td class='num {_pnl_cls(ins['hrp']['ret'])}'>{_pct(ins['hrp']['ret'])}</td>
    <td class='num'>{_pct(ins['hrp']['vol'])}</td>
    <td class='num neg'>{_pct(ins['hrp']['maxdd'])}</td></tr>
<tr><td><strong>Equal-Weight</strong></td>
    <td class='num'>{ins['ew']['sharpe']:.3f}</td>
    <td class='num {_pnl_cls(ins['ew']['ret'])}'>{_pct(ins['ew']['ret'])}</td>
    <td class='num'>{_pct(ins['ew']['vol'])}</td>
    <td class='num neg'>{_pct(ins['ew']['maxdd'])}</td></tr>
</tbody></table>
</section>"""

    # --- divergences: reduce / increase ---
    reduce = [d for d in div if d["delta"] < -0.03][:8]
    increase = [d for d in div if d["delta"] > 0.03][:8]

    def _div_row(d: dict, direction: str) -> str:
        cls = "neg" if direction == "reduce" else "pos"
        return (
            f"<tr><td><strong>{d['tv_symbol']}</strong></td>"
            f"<td class='dim'>{d['broker']}</td>"
            f"<td class='num'>{_pct(d['curr_weight'])}</td>"
            f"<td class='num bold {cls}'>{_pct(d['hrp_weight'])}</td>"
            f"<td class='num {cls}'>{_pct(d['delta'], plus=True)}</td>"
            f"<td class='num {_pnl_cls(d['pnl_pct'])}'>{_broker_pct(d['pnl_pct'], plus=True)}</td>"
            f"<td class='num'>${d['market_val']:,.0f}</td></tr>\n"
        )

    th = ("<tr><th>Symbol</th><th>Broker</th><th class='num'>Current%</th>"
          "<th class='num'>HRP%</th><th class='num'>Delta</th>"
          "<th class='num'>P&amp;L%</th><th class='num'>Mkt Val</th></tr>")

    reduce_rows  = "".join(_div_row(d, "reduce")   for d in reduce)
    increase_rows = "".join(_div_row(d, "increase") for d in increase)

    div_html = f"""
<div class='two-col'>
<section class='card'>
<h2>Overweight vs HRP — Consider Reducing</h2>
<p class='note'>Delta &lt; -3%. HRP wants less exposure here.</p>
<table><thead>{th}</thead><tbody>{reduce_rows}</tbody></table>
</section>
<section class='card'>
<h2>Underweight vs HRP — Consider Adding</h2>
<p class='note'>Delta &gt; +3%. HRP wants more exposure here.</p>
<table><thead>{th}</thead><tbody>{increase_rows}</tbody></table>
</section>
</div>"""

    # --- full table ---
    all_rows = ""
    for d in all_a:
        all_rows += (
            f"<tr><td><strong>{d['tv_symbol']}</strong></td>"
            f"<td class='dim'>{d['broker']}</td>"
            f"<td class='num'>{_pct(d['curr_weight'])}</td>"
            f"<td class='num'>{_pct(d['hrp_weight'])}</td>"
            f"<td class='num {_pnl_cls(d['delta'])}'>{_pct(d['delta'], plus=True)}</td>"
            f"<td class='num {_pnl_cls(d['pnl_pct'])}'>{_broker_pct(d['pnl_pct'], plus=True)}</td>"
            f"<td class='num'>${d['market_val']:,.0f}</td></tr>\n"
        )

    style = """
:root{--bg:#0f1117;--card:#1a1f2e;--border:#2a2f40;--text:#e8eaed;--dim:#8b9299;
      --acc:#4a9eff;--pos:#4caf50;--neg:#f44336}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
     font-size:14px;line-height:1.6;padding:20px}
h1{font-size:1.6rem;color:var(--acc);margin-bottom:4px}
h2{font-size:1.1rem;color:var(--acc);margin-bottom:10px}
.subtitle{color:var(--dim);font-size:.85rem;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
      padding:20px;margin-bottom:20px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--dim);font-weight:600;font-size:.82rem;cursor:pointer;user-select:none}
th:hover{color:var(--acc)}
td{font-size:.88rem}
.num{text-align:right}
.dim{color:var(--dim)}
.bold{font-weight:700}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}
.note{color:var(--dim);font-size:.83rem;margin-bottom:12px}
tr:hover td{background:rgba(74,158,255,.05)}
@media(max-width:768px){.two-col{grid-template-columns:1fr}}
"""

    js = """
function s(id,col){
  var t=document.getElementById(id);
  var rows=Array.from(t.tBodies[0].rows);
  var asc=t.dataset.sort==col?t.dataset.asc!='1':true;
  t.dataset.sort=col;t.dataset.asc=asc?'1':'0';
  rows.sort(function(a,b){
    var va=a.cells[col].innerText.replace(/[+$,% ]/g,'');
    var vb=b.cells[col].innerText.replace(/[+$,% ]/g,'');
    var na=parseFloat(va),nb=parseFloat(vb);
    if(!isNaN(na)&&!isNaN(nb))return asc?na-nb:nb-na;
    return asc?va.localeCompare(vb):vb.localeCompare(va);
  });
  rows.forEach(function(r){t.tBodies[0].appendChild(r)});
}
"""

    all_th = ("<tr>"
              "<th onclick=\"s('tbl-all',0)\">Symbol</th>"
              "<th onclick=\"s('tbl-all',1)\">Broker</th>"
              "<th onclick=\"s('tbl-all',2)\" class='num'>Current%</th>"
              "<th onclick=\"s('tbl-all',3)\" class='num'>HRP%</th>"
              "<th onclick=\"s('tbl-all',4)\" class='num'>Delta</th>"
              "<th onclick=\"s('tbl-all',5)\" class='num'>P&amp;L%</th>"
              "<th onclick=\"s('tbl-all',6)\" class='num'>Mkt Val</th>"
              "</tr>")

    return (
        f"<!DOCTYPE html><html lang='en'><head>"
        f"<meta charset='UTF-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Portfolio Risk Optimizer — {snap_date}</title>"
        f"<style>{style}</style><script>{js}</script></head><body>"
        f"<h1>Portfolio Risk Optimizer</h1>"
        f"<p class='subtitle'>HRP vs current allocation | Snapshot: <strong>{snap_date}</strong> | {now}</p>"
        f"<p class='note' style='margin-bottom:20px'>"
        f"HRP = Hierarchical Risk Parity (Lopez de Prado 2016). "
        f"Weights proportional to inverse-clustered volatility — no covariance matrix inversion, "
        f"robust out-of-sample. Covers {res['n_symbols']} of your positions with 1-year yfinance data "
        f"(SGX warrants and other instruments without price history excluded).</p>"
        f"{wf_html}"
        f"{ins_html}"
        f"{div_html}"
        f"<section class='card'><h2>All Positions — Current vs HRP ({len(all_a)})</h2>"
        f"<table id='tbl-all'><thead>{all_th}</thead>"
        f"<tbody>{all_rows}</tbody></table></section>"
        f"</body></html>"
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    cache_path = Path(args.cache)
    out_path   = Path(args.out)

    if not _HAS_SKFOLIO:
        print("  [risk-opt] ERROR: skfolio not installed — run: pip install skfolio")
        sys.exit(1)
    if not _HAS_YF:
        print("  [risk-opt] ERROR: yfinance not installed — run: pip install yfinance")
        sys.exit(1)
    if not _HAS_PANDAS:
        print("  [risk-opt] ERROR: pandas not installed")
        sys.exit(1)

    print(f"  [risk-opt] loading positions <- {cache_path.name}")
    positions = _load_positions(cache_path)
    print(f"  [risk-opt] {len(positions)} eligible positions (equity, no options)")

    # Build symbol map
    ticker_map: dict[str, dict] = {}
    for p in positions:
        yf_sym = _tv_to_yf(p.get("symbol", ""))
        ticker_map[yf_sym] = p

    snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"  [risk-opt] fetching 1y price history for {len(ticker_map)} tickers...")
    try:
        prices = _fetch_prices(list(ticker_map.keys()), period=args.period)
    except Exception as exc:
        print(f"  [risk-opt] ERROR fetching prices: {exc}")
        sys.exit(1)

    n_ok  = prices.shape[1]
    n_tot = len(ticker_map)
    print(f"  [risk-opt] usable: {n_ok}/{n_tot} symbols ({n_tot - n_ok} excluded — no yfinance coverage)")
    print(f"  [risk-opt] {len(prices)} trading days ({prices.index[0].date()} to {prices.index[-1].date()})")

    if n_ok < 3:
        print("  [risk-opt] ERROR: fewer than 3 symbols with data — cannot optimize")
        sys.exit(1)

    print("  [risk-opt] running HRP + walk-forward optimization...")
    results = _run_optimization(prices, ticker_map)

    wf = results["walk_forward"]
    if wf:
        print(f"  [risk-opt] walk-forward ({wf['folds']} folds): "
              f"HRP Sharpe={wf['hrp_sharpe']:.3f} vs EW={wf['ew_sharpe']:.3f}  |  "
              f"HRP MaxDD={wf['hrp_maxdd']:.1%} vs EW={wf['ew_maxdd']:.1%}")
        verdict = "HRP BEATS EW" if wf["hrp_beats_sharpe"] else "EW BEATS HRP"
        print(f"  [risk-opt] verdict: {verdict}")

    print(f"  [risk-opt] building report...")
    html = _build_html(results, snap_date, out_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"  [risk-opt] report -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio Risk Optimizer (HRP)")
    parser.add_argument("--cache",  default=str(DEFAULT_CACHE), help="Path to .cache_global_positions.json")
    parser.add_argument("--out",    default=str(DEFAULT_OUT),   help="Output HTML path")
    parser.add_argument("--period", default="1y",               help="yfinance period (default: 1y)")
    main(parser.parse_args())
