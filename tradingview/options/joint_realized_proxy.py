"""Chronological joint underlying RETURN / FUTURE REALIZED-VOL proxy research.

THIS IS NOT AN IMPLIED VOLATILITY FORECAST, not a per-strike volatility surface,
not true vintage point-in-time option data, and NEVER satisfies edge_engine's
forecast contract. Fetching adjusted/unadjusted Yahoo history today cannot prove
the same historical values were available exactly then.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
import numpy as np
import pandas as pd

HORIZON=21
LOOKBACK=20
MIN_TRAIN=252
NEIGHBORS=120
UNIVERSE=("SPY","QQQ","IWM")
SCHEMA="UNDERLYING_JOINT_REALIZED_VOL_PROXY_NOT_OPTION_IV_V0"

def prepare(history):
    if not isinstance(history,pd.DataFrame) or "Close" not in history or history.index.tz is None:
        raise ValueError("TZ_AWARE_COMPLETED_DAILY_BARS_REQUIRED")
    h=history.sort_index()
    if h.index.has_duplicates or len(h)<520:
        raise ValueError("INSUFFICIENT_HISTORY")
    if not all(np.isfinite(h["Close"].astype(float))) or not all(h["Close"]>0):
        raise ValueError("INVALID_CLOSE")
    dates=h.index
    close=np.asarray(h["Close"],dtype=float)
    ret=np.diff(np.log(close),prepend=np.nan)
    return dates,close,ret

def features(ret,i):
    window=ret[i-19:i+1]
    if not np.all(np.isfinite(window)):
        raise ValueError("MISSING_PAST_RETURNS")
    rv=float(np.std(window,ddof=1)*np.sqrt(252))
    return np.array([float(np.sum(ret[i-4:i+1])),float(np.sum(window)),math.log(max(rv,1e-4))])

def future_labels(ret,i,h=HORIZON):
    r=ret[i+1:i+h+1]
    if len(r)!=h or not np.all(np.isfinite(r)):
        raise ValueError("FUTURE_MISSING")
    return np.array([float(np.sum(r)),float(np.std(r,ddof=1)*np.sqrt(252))])

def train_at(ret, decision_index, *,h=HORIZON, min_train=MIN_TRAIN):
    """Each train label ends at or before decision_index, NEVER afterward."""
    historical_end=decision_index-h
    ids=np.arange(LOOKBACK, historical_end+1)
    if len(ids)<min_train:
        raise ValueError("INSUFFICIENT_PURGED_TRAINING")
    x=np.stack([features(ret,int(i)) for i in ids])
    y=np.stack([future_labels(ret,int(i),h) for i in ids])
    f=features(ret,decision_index)
    # Robust training-only scale; no test labels or means are involved.
    spread=np.maximum(np.std(x,axis=0,ddof=1),.00001)
    distance=np.sum(((x-f)/spread)**2,axis=1)
    nearest=np.argsort(distance,kind="stable")[:min(NEIGHBORS,len(ids))]
    chosen=y[nearest]
    if not np.all(np.isfinite(chosen)):
        raise ValueError("INVALID_DISTRIBUTION")
    return dict(training_last_label_index=int(ids[-1]+h),training_count=len(ids),
                nearest_count=len(nearest),forecast=np.mean(chosen,axis=0),
                lo=np.quantile(chosen,.1,axis=0),hi=np.quantile(chosen,.9,axis=0),
                joint_historical_outcomes=chosen)

def validate_walk_forward(history,*,step=HORIZON):
    dates,close,ret=prepare(history)
    start=LOOKBACK+MIN_TRAIN+HORIZON
    evaluated=[]
    for i in range(start,len(ret)-HORIZON,step):
        model=train_at(ret,i)
        if model["training_last_label_index"]>i:
            raise AssertionError("LEAKAGE: forecast uses future known label")
        actual=future_labels(ret,i)
        predicted=model["forecast"]
        inside=np.logical_and(actual>=model["lo"],actual<=model["hi"])
        evaluated.append(dict(decision_idx=i,predicted=predicted,actual=actual,
                              inside=inside,train_rows=model["training_count"]))
    if len(evaluated)<12:raise ValueError("INSUFFICIENT_NONOVERLAPPING_BACKTEST")
    # Model comparison with a naive zero-drift/RV-persistence forecast.
    eval_idx=[v["decision_idx"] for v in evaluated]
    actual=np.stack([v["actual"] for v in evaluated])
    pred=np.stack([v["predicted"] for v in evaluated])
    naive=np.array([[0.0,float(np.std(ret[i-19:i+1],ddof=1)*np.sqrt(252))]
                    for i in eval_idx])
    coverage=np.mean(np.stack([v["inside"] for v in evaluated]),axis=0)
    loss=np.mean(np.abs(pred-actual),axis=0)
    naive_loss=np.mean(np.abs(naive-actual),axis=0)
    final=train_at(ret,len(ret)-1)
    return {
        "schema":SCHEMA,
        "source":"Yahoo daily unadjusted Close, retrieved at analysis time, NOT historical vintages",
        "date_first":dates[0].date().isoformat(),
        "date_last":dates[-1].date().isoformat(),
        "bars":len(close),
        "horizon_trading_days":HORIZON,
        "walkforward_evaluation_count":len(evaluated),
        "evaluation_spacing_sessions":step,
        "leakage_embargo_sessions":HORIZON,
        "percentile_nominal_80pct_intervals":True,
        "return_interval_80pct_coverage":round(float(coverage[0]),4),
        "realized_vol_interval_80pct_coverage":round(float(coverage[1]),4),
        "return_mae_log":round(float(loss[0]),6),
        "naive_return_mae_log":round(float(naive_loss[0]),6),
        "future_realized_vol_mae_annual":round(float(loss[1]),6),
        "naive_realized_vol_mae_annual":round(float(naive_loss[1]),6),
        "latest_price_log_return_mean":round(float(final["forecast"][0]),6),
        "latest_price_log_return_10_90":[round(float(z),6) for z in (final["lo"][0],final["hi"][0])],
        "latest_forward_realized_vol_mean_annual":round(float(final["forecast"][1]),6),
        "latest_forward_realized_vol_10_90_annual":[round(float(z),6) for z in (final["lo"][1],final["hi"][1])],
        "joint_outcome_correlation":round(float(np.corrcoef(final["joint_historical_outcomes"].T)[0,1]),5),
        "calibrated_option_implied_vol_surface":False,
        "independent_option_bid_ask_side_times":False,
        "historical_vendor_vintage_certified":False,
        "edge_engine_forecast_eligible":False,
        "profitability_measured":False,
        "research_only":True
    }

def main(argv=None):
    ap=argparse.ArgumentParser(description="Read-only historical rolling validation: spot and future realized volatility NOT IV")
    ap.add_argument("--tickers",nargs="+",choices=UNIVERSE,default=list(UNIVERSE))
    args=ap.parse_args(argv)
    import yfinance as yf
    now=datetime.now(timezone.utc)
    output={}
    for symbol in args.tickers:
        data=yf.Ticker(symbol).history(period="5y",interval="1d",auto_adjust=False,timeout=14)
        # Current session is NOT usable until completed NYSE session.
        from zoneinfo import ZoneInfo
        ny_today=now.astimezone(ZoneInfo("America/New_York")).date()
        data=data.loc[data.index.date<ny_today]
        try:output[symbol]=validate_walk_forward(data)
        except (ValueError,TypeError,KeyError) as e:output[symbol]={"status":"BLOCKED","reason":str(e)}
    print(json.dumps(output,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
