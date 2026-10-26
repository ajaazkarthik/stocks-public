"""Existing-sources-only options selection shadow lab (NO orders or alerts).

A fully usable research workflow even if sources are delayed or sparse.
An explicit REFERENCE_PROXY_UNVERIFIED joint spot/IV assumption is built from
prior daily underlying outcomes, anchored to CURRENT exact-leg option IV, and
tested as a MODEL HYPOTHESIS, NEVER as an independently calibrated forecast.
No scheduled invocation; source data and live trading state are read-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

import numpy as np

from tradingview.options.edge_engine import (
    EDGE_VERSION, compare_frozen_to_edge, enumerate_frozen_candidates,
    validate_forecast,
)
from tradingview.options.joint_realized_proxy import LOOKBACK,HORIZON,MIN_TRAIN,features,prepare,train_at
from tradingview.options.paper_scanner import NY,POLICY,contract_key,stamp
from tradingview.scheduler.market_calendar import _calendar,CalendarCoverageError

VERSION="existing-free-feeds-shadow-v1"
DEFAULT_SHADOW=Path.home()/".config/stocks-options-edge/shadow-comparisons.sqlite"
DEFAULT_SOURCE=Path(__file__).resolve().parents[2]/"database"
SCHEMA="""CREATE TABLE IF NOT EXISTS shadow_decisions (
  decision_id TEXT PRIMARY KEY,
  observed_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  baseline_status TEXT NOT NULL,
  challenger_status TEXT NOT NULL,
  forward_outcome TEXT NOT NULL CHECK(forward_outcome='NOT_MEASURED'),
  evidence_class TEXT NOT NULL CHECK(evidence_class='REFERENCE_ONLY'),
  payload_json TEXT NOT NULL
);
"""

def audit_existing_archives(folder=DEFAULT_SOURCE):
    """All database reads are read-only and only column/aggregate counts return."""
    root=Path(folder)
    spec={
      "legacy_options_scans":("alert_tracker.db","options_scan_results","created_at","code"),
      "tqs_v2_scans":("tqs_v2.db","tqs_v2_results","created_at","opra_code"),
      "options_recommendations":("alert_tracker.db","options_recommendations","run_timestamp","rec_uuid"),
      "zerodte_scans":("zerodte.db","zerodte_scans","scan_date","contract_code"),
      "frozen_paper_observations":("options_paper.db","option_paper_observations","observed_at","id"),
    }
    out={}
    for key,(file,table,clock,identity) in spec.items():
        path=root/file
        if not path.is_file():
            out[key]={"status":"UNAVAILABLE"};continue
        try:
            with sqlite3.connect("file:"+str(path.resolve())+"?mode=ro",uri=True) as c:
                if table not in {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                    out[key]={"status":"TABLE_MISSING"};continue
                count,days,distinct=c.execute(
                    f'SELECT COUNT(*),COUNT(DISTINCT substr("{clock}",1,10)),'
                    f'COUNT(DISTINCT "{identity}") FROM "{table}"').fetchone()
                columns={row[1] for row in c.execute(f'PRAGMA table_info("{table}")')}
                out[key]={
                    "status":"REFERENCE_HISTORY_PRESENT" if count else "EMPTY",
                    "rows":count,"distinct_receipt_dates":days,
                    "distinct_identifiers":distinct,
                    "has_bid_ask_fields":{"bid","ask"}.issubset(columns),
                    "has_iv_field":"iv" in columns,
                    "has_independent_side_event_fields":{"bid_at","ask_at"}.issubset(columns),
                    "clock_provenance":"application_row_time_not_exchange_bid_ask_event",
                }
        except sqlite3.Error:
            out[key]={"status":"READ_ERROR"}
    return out

def provider_matrix(source_comparisons=None):
    """Provider features are separate; never coalesce quote sides between vendors."""
    c=source_comparisons or {}
    def counts(item):
        data=c.get(item) or {}
        rows=data.get("rows",data.get("expiries",[]))
        return {"queried":bool(data),"rows":len(rows),
                "statuses":sorted({r.get("status","UNKNOWN") for r in rows}),
                "quote_event_times_certified":False,
                "eligible_for_synthetic_reference_research":True,
                "eligible_as_observed_option_fill":False}
    strict=c.get("strict_evidence") or {}
    return {"tradingview_scan2":"REFERENCE_ONLY_GENERIC_UPDATE",
            "yahoo":"EXACT_CHAIN_OI_BUT_TRADE_TIME_NOT_SIDE_EVENT",
            "cboe_free":counts("cboe_oi"),
            "moomoo":counts("moomoo"),
            "tv_smile":counts("iv_smile"),
            "tqs_scores":counts("tqs_scores"),
            "strict_side_gates":sorted({v.get("status","UNKNOWN") for v in strict.values()}),
            "combined_sources_do_not_prove_independent_side_event_clock":True}

def build_reference_forecast(obs, history, now, *,paths=8,iv_sensitivity=.25,
                             assumption_loss_usd=20.0):
    """Empirical forward SPOT pathways + bounded ASSUMED future option-IV shifts.

    Training labels are known by the decision time. No option future IV
    histories are invented. IV levels are current exact-leg reference IVs;
    scenario shifts proxy conditional historic realized-vol changes.
    """
    if not 2<=paths<=24 or not 0<=iv_sensitivity<=.5:
        raise ValueError("INVALID_SCENARIO_PARAMETERS")
    receipt=stamp(obs["observed_at"])
    now=stamp(now)
    if not receipt<=now or now-receipt>timedelta(minutes=10):
        raise ValueError("STALE_OBSERVATION")
    if obs["symbol"] not in POLICY["universe"]:
        raise ValueError("INVALID_SYMBOL")
    dates,closes,returns=prepare(history)
    if dates[-1].date()>=receipt.astimezone(NY).date():
        raise ValueError("UNFINISHED_BAR_OR_LOOKAHEAD")
    if obs.get("features",{}).get("asof"):
        asof=stamp(obs["features"]["asof"])
        if asof.astimezone(NY).date()!=dates[-1].date():
            raise ValueError("HISTORY_NOT_ALIGNED_TO_SIGNAL")
    # Exact-leg IV is the reference surface anchor, NOT a future observed IV.
    quotes=obs.get("quotes",[])
    if not quotes:
        raise ValueError("NO_CURRENT_OPTION_REFERENCE")
    iv={}
    for q in quotes:
        k=contract_key(q)
        v=float(q["iv"])
        # TVOptionsFetcher stores quoted IV as PERCENT (e.g. 25.43);
        # frozen synthetic fixtures use fractional IV (e.g. 0.20).
        if q.get("provider")=="TradingView scan2":
            v=v/100.0
        if not .01<=v<=5 or not math.isfinite(v):
            raise ValueError("MISSING_EXACT_LEG_IV")
        iv[k]=v
    last=len(returns)-1
    trained=train_at(returns,last)
    cutoff=last-HORIZON
    indices=np.arange(LOOKBACK,cutoff+1)
    xs=np.stack([features(returns,int(i)) for i in indices])
    target=features(returns,last)
    sd=np.maximum(np.std(xs,axis=0,ddof=1),1e-5)
    dist=np.sum(((xs-target)/sd)**2,axis=1)
    sorted_idx=indices[np.argsort(dist,kind="stable")]
    chosen=[]
    for i in sorted_idx:
        if all(abs(int(i)-j)>=HORIZON for j in chosen):
            chosen.append(int(i))
        if len(chosen)==paths:break
    if len(chosen)!=paths:
        raise ValueError("INSUFFICIENT_NONOVERLAPPING_REFERENCE_PATHS")
    # Use real NYSE future session closes (including holiday/early-close).
    schedule=_calendar("NYSE").schedule(
        start_date=receipt.astimezone(NY).date(),
        end_date=(receipt.astimezone(NY)+timedelta(days=65)).date())
    future=schedule[schedule["market_close"]>now]
    if len(future)<HORIZON:
        raise ValueError("INSUFFICIENT_FUTURE_CALENDAR")
    # Entry policy holds at most 21 CALENDAR days, not 21 trading days.
    # Stop at the first future NYSE close covering the calendar-day horizon;
    # consuming all 21 trading sessions could incorrectly run past a 30-DTE
    # option expiry and falsely reject valid frozen candidates.
    clocks=[]
    boundary=now+timedelta(days=POLICY["max_hold_days"])
    for x in future["market_close"].iloc[:HORIZON]:
        closing=x.to_pydatetime().astimezone(timezone.utc)
        clocks.append(closing)
        if closing>=boundary:
            break
    if not clocks or clocks[-1]<boundary:
        raise ValueError("SCENARIO_DOES_NOT_REACH_CALENDAR_HOLDING_HORIZON")
    # The same volatility ratio applies to all exact legs; historical realized
    # variance does NOT identify future option implied volatility/skew.
    spot=float(next((q["spot"] for q in quotes if q.get("spot") and float(q["spot"])>0),
                    obs["features"]["close"]))
    generated=[]
    for n,idx in enumerate(chosen):
        changes=returns[idx+1:idx+HORIZON+1]
        historical=float(np.std(returns[idx-19:idx+1],ddof=1))
        next_vol=float(np.std(changes,ddof=1))
        shift=np.clip(next_vol/max(historical,1e-5)-1,-1,1)*iv_sensitivity
        scaled={k:round(v*(1+float(shift)),8) for k,v in iv.items()}
        pts=[]
        curr=spot
        ema20=float(obs["features"]["ema20"])
        ema50=float(obs["features"]["ema50"])
        for t,dr in zip(clocks,changes):
            curr*=math.exp(float(dr))
            ema20+=(curr-ema20)*(2/21)
            ema50+=(curr-ema50)*(2/51)
            trend="BULL" if curr>ema20>ema50 else "BEAR" if curr<ema20<ema50 else "FLAT"
            pts.append({"at":t.isoformat(),"spot":round(curr,8),
                        "trend":trend,"ivs":scaled,
                        "rate":0.0,"dividend":0.0,
                        "funding_cost_usd":0.0,"assignment_cost_usd":0.0})
        generated.append({"scenario_id":f"historic_nonoverlap_{n}",
                          "probability":1/paths,"path":pts})
    sample=closes[max(0,last-252):last+1].astype(float)
    digest=hashlib.sha256(sample.tobytes()).hexdigest()
    result={
        "engine":EDGE_VERSION,"forecast_id":VERSION+"-"+digest[:12],
        "method":"predecision nearest historical joint spot-return paths, current-IV anchored SAME-STRIKE shift assumption; NOT a trained option-IV forecast",
        "training_source_sha256":digest,
        "training_cutoff":dates[-1].isoformat(),
        "created_at":now.isoformat(),
        "uncertainty_method":"user-unvalidated fixed sensitivity allowance, NOT a calibrated confidence bound",
        "forecast_status":"REFERENCE_PROXY_UNVERIFIED",
        "underlying":obs["symbol"],"mean_estimation_error_usd":assumption_loss_usd,
        "scenarios":generated,
        "source_caveats":["Yahoo downloaded-now bars are not historical vintages",
                          "future IV is an assumed regime shift of current contract IV",
                          "daily scenarios cannot resolve intraday stop timing",
                          "zero future funding/dividend/assignment costs are explicit assumptions",
                          "no independent bid/ask-side clocks or executed spreads"],
        "verified_option_iv_forecast":False,
        "historical_option_iv_outcomes":0,
    }
    validate_forecast(result,obs,now)
    return result


def safe_comparison(obs,forecast,now):
    """Compact two-arm source-clock aligned shadow selection (not executed P/L)."""
    decision=compare_frozen_to_edge(obs,now,forecast,max_candidates=12)
    best=decision.get("selection")
    limit=best.get("price_limit") if best else None
    value=best.get("valuation") if best else None
    return {
        "run_version":VERSION,
        "symbol":obs["symbol"],
        "observed_at":obs["observed_at"],
        "baseline_status":decision["baseline"]["status"],
        "baseline_reference_candidate":decision["baseline"]["candidate"],
        "edge_reference_candidate":best["candidate"] if best else None,
        "challenger_status":decision["challenger"],
        "eligible_frozen_count":decision["considered"],
        "candidate_rank_rows":len(decision["ranked"]),
        "model_only_price_limit":limit,
        "model_only_expected_net_usd":value["expected_net_usd"] if value else None,
        "model_only_conservative_proxy_usd":value["conservative_mean_proxy_usd"] if value else None,
        "model_only_max_contractual_loss_usd":value["max_contractual_loss_usd"] if value else None,
        "risk_factor_exposure":"NOT_OBSERVED",
        "forecast_status":forecast["forecast_status"] if forecast else "NO_FORECAST",
        "reference_quote_basis":decision["quote_basis"],
        "price_limit_tradable":False,
        "shadow_fill_status":"NOT_OBSERVED",
        "realized_net_pnl":"NOT_MEASURED",
        "funded_portfolio_return":"NOT_MEASURED",
        "notifier_eligible":False,
        "broker_orders":0,
        "cash_control":"0 dollar exposure when neither qualifies",
        "provider_matrix":provider_matrix(obs.get("source_comparisons")),
    }

def _private(path):
    p=Path(path).expanduser().resolve()
    if p.name in ("alert_tracker.db","options_paper.db","tqs_v2.db","zerodte.db"):
        raise ValueError("CANNOT_OVERWRITE_PRODUCTION_DATABASE")
    p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not p.exists():
        fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    if p.stat().st_mode&0o077:
        raise PermissionError("PRIVATE_RESEARCH_STATE_MUST_BE_0600")
    return p

def record_research(decision, path=DEFAULT_SHADOW):
    """Optional durable REFERENCE shadow decisions, never a filled position."""
    if decision.get("broker_orders")!=0 or decision.get("notifier_eligible") is not False:
        raise ValueError("ACTIONABLE_SIGNALS_FORBIDDEN")
    if decision.get("realized_net_pnl")!="NOT_MEASURED":
        raise ValueError("OBSERVED_PROFIT_NOT_SUPPORTED")
    short={k:v for k,v in decision.items() if k not in ("provider_matrix",)}
    short["provider_statuses"]=decision.get("provider_matrix",{})
    body=json.dumps(short,sort_keys=True,allow_nan=False)
    identity=hashlib.sha256((VERSION+"|"+decision["symbol"]+"|"+
                             decision["observed_at"]).encode()).hexdigest()
    p=_private(path)
    with sqlite3.connect(p) as c:
        c.executescript(SCHEMA)
        with c:
            c.execute(
                "INSERT OR IGNORE INTO shadow_decisions VALUES (?,?,?,?,?,?,?,?)",
                (identity,decision["observed_at"],decision["symbol"],
                 decision["baseline_status"],decision["challenger_status"],
                 "NOT_MEASURED","REFERENCE_ONLY",body))
        count=c.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]
    return {"status":"RECORDED_REFERENCE_RESEARCH","decision_sha256":identity,
            "total_shadow_decisions":count,"ledger_private":True}

def _history(symbol):
    import yfinance as yf
    from datetime import datetime, timezone
    today=datetime.now(timezone.utc).astimezone(NY).date()
    df=yf.Ticker(symbol.split(":")[-1]).history(period="5y",interval="1d",
                                                   auto_adjust=True,timeout=15)
    return df[df.index.date<today]

def run_symbol(symbol, *,include_cboe=True,include_moomoo=True,
               include_smile=True,include_tv_lenses=False,record=False,
               shadow_db=DEFAULT_SHADOW):
    """Run existing provider wrappers AS IS; no silent replacement for failures."""
    if symbol not in POLICY["universe"]:
        raise ValueError("UNSUPPORTED_SYMBOL")
    from tradingview.options.paper_scanner_data import fetch_full_research_observation
    from tradingview.options.zerodte.config import DEFAULT_CONFIG
    now=datetime.now(timezone.utc)
    headers=None
    if include_tv_lenses:
        from tradingview.options.zerodte.fetcher import _load_tv_cookies,_make_tv_headers
        cookies=_load_tv_cookies(DEFAULT_CONFIG.cookie_path)
        if not cookies.get("sessionid"):
            raise ValueError("TV_AUTH_REQUIRED_NO_GUEST_FALLBACK")
        headers=_make_tv_headers(cookies)
    obs=fetch_full_research_observation(
        symbol,[],now,headers,enable_tv_lenses=include_tv_lenses,
        enable_cboe=include_cboe,enable_moomoo=include_moomoo,
        enable_smile=include_smile,max_quotes=2)
    history=_history(symbol)
    after=datetime.now(timezone.utc)
    try:
        forecast=build_reference_forecast(obs,history,after)
        outcome=safe_comparison(obs,forecast,after)
    except (ValueError,KeyError,TypeError) as exc:
        outcome=safe_comparison(obs,None,after)
        outcome["research_model_status"]="PROXY_FAILED_CLOSED"
        outcome["research_model_error_class"]=type(exc).__name__
    else:
        outcome["research_model_status"]="UNVALIDATED_REFERENCE_PROXY_ASSUMPTIONS"
        outcome["historical_spot_training_bars"]=len(history)
        outcome["empirical_spot_paths"]=len(forecast["scenarios"])
        outcome["assumed_option_iv_shift"]=True
        outcome["forecast_heldout_option_iv_calibrated"]=False
    if record:
        from tradingview.options.shadow_repricing import reconcile
        outcome["reference_followup"]=reconcile(shadow_db,obs,after)
        outcome["recording"]=record_research(outcome,shadow_db)
    return outcome

def main(argv=None):
    p=argparse.ArgumentParser(description="Five-source FREE/EXISTING options shadow; off by default")
    p.add_argument("--audit-only",action="store_true",
                   help="read-only local historical coverage counts (DEFAULT)")
    p.add_argument("--scan",action="store_true",help="manual read-only provider + shadow valuation")
    p.add_argument("--scan-all",action="store_true",help="manual SPY+QQQ+IWM, preserve failures per symbol")
    p.add_argument("--factor-check",action="store_true",help="historical daily underlying co-movement, NOT option PNL")
    p.add_argument("--symbol",choices=POLICY["universe"],default="AMEX:SPY")
    p.add_argument("--record",action="store_true",help="store one PRIVATE non-trading research decision")
    p.add_argument("--shadow-db",type=Path,default=DEFAULT_SHADOW)
    p.add_argument("--source-dir",type=Path,default=DEFAULT_SOURCE)
    p.add_argument("--tv-lenses",action="store_true")
    args=p.parse_args(argv)
    if args.record and not (args.scan or args.scan_all):
        p.error("--record requires explicit --scan or --scan-all")
    from tradingview.options.legacy_iv_evidence import evaluate
    iv_diagnostics=evaluate(args.source_dir/"alert_tracker.db",
                  asof_date=datetime.now(timezone.utc).astimezone(NY).date().isoformat())
    data={"version":VERSION,"source_spending_usd":0,
          "archive":audit_existing_archives(args.source_dir),
          "legacy_option_iv_holdout":iv_diagnostics,
          "real_option_profit":"NOT_MEASURED","live_orders":0,"telegram_sends":0}
    if args.scan or args.scan_all:
        targets=POLICY["universe"] if args.scan_all else [args.symbol]
        observations={}
        for symbol in targets:
            try:
                observations[symbol]=run_symbol(symbol,record=args.record,
                                      include_tv_lenses=args.tv_lenses,
                                      shadow_db=args.shadow_db)
            except (KeyError,TypeError,ValueError,RuntimeError) as exc:
                observations[symbol]={"status":"MARKET_CLOSED_OR_CALENDAR_UNAVAILABLE" if isinstance(exc,CalendarCoverageError) else "SOURCE_OR_MODEL_UNAVAILABLE",
                                     "error_class":type(exc).__name__,
                                     "broker_orders":0,"profit":"NOT_MEASURED"}
        data["shadows"]=observations
    if args.factor_check:
        from tradingview.options.shadow_factor_risk import underlying_factor_reference
        try:
            histories={s:_history("AMEX:"+s) for s in ("SPY","QQQ","IWM")}
            data["factor_reference"]=underlying_factor_reference(histories)
        except (ValueError,KeyError,TypeError,RuntimeError) as exc:
            data["factor_reference"]={"status":"UNAVAILABLE","error_class":type(exc).__name__,
                                      "observed_option_trade_pnl":"NOT_MEASURED"}
    print(json.dumps(data,sort_keys=True,allow_nan=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
