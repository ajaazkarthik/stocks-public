"""Read-only, source-first IV-change diagnostics using EXISTING local option scans.

Archived exact contracts give some 1–7 CALENDAR-day paired reference IV
changes, NOT reliable 21-trading-day option IV surfaces or executable returns.
No source-side event times exist; this never trains a certified live forecaster.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
from pathlib import Path
import sqlite3

import numpy as np

DEFAULT=Path(__file__).resolve().parents[2]/"database"/"alert_tracker.db"

def evaluate(path=DEFAULT, *,asof_date, min_pairs=40):
    cutoff=date.fromisoformat(str(asof_date))
    p=Path(path)
    if not p.is_file():
        return {"status":"ARCHIVE_UNAVAILABLE","usable_pairs":0,"option_iv_forecast_calibrated":False}
    with sqlite3.connect("file:"+str(p.resolve())+"?mode=ro",uri=True) as conn:
        raw=conn.execute(
            "SELECT tv_symbol,option_type,strike,expiry,created_at,iv,spot_price "
            "FROM options_scan_results WHERE created_at IS NOT NULL "
            "AND iv>0 AND spot_price>0 AND strike>0 "
            "ORDER BY tv_symbol,option_type,strike,expiry,created_at").fetchall()
    by_code=defaultdict(dict)
    for sym,opt,strike,expiry,created,iv,spot in raw:
        try:
            day=date.fromisoformat(str(created)[:10])
            if day>cutoff or day>date.fromisoformat(str(expiry)[:10]):
                continue
            key=(str(sym),str(opt),float(strike),str(expiry))
            iv=float(iv);spot=float(spot)
            if not (.01<iv<500 and 0<spot<1e7 and math.isfinite(iv) and math.isfinite(spot)):
                continue
            # Only first application receipt per exact contract per day, never
            # resample intra-day into independent exchange quote events.
            by_code[key].setdefault(day,(math.log(iv),math.log(spot)))
        except (ValueError,TypeError,OverflowError):
            continue
    pairs=[]
    for series in by_code.values():
        days=sorted(series)
        for a,b in zip(days,days[1:]):
            gap=(b-a).days
            if not 1<=gap<=7:
                continue
            iv1,spot1=series[a]
            iv2,spot2=series[b]
            pairs.append((b,iv2-iv1,spot2-spot1,gap))
    if len(pairs)<min_pairs:
        return {"status":"TOO_FEW_DISTINCT_CONTRACT_DAY_PAIRS",
                "usable_pairs":len(pairs),"exact_contracts":len(by_code),
                "option_iv_forecast_calibrated":False}
    pairs.sort(key=lambda x:x[0])
    n=len(pairs)
    # Hold out entire later application dates, not random contract rows.
    distinct_days=sorted({r[0] for r in pairs})
    if len(distinct_days)<5:
        return {"status":"TOO_FEW_INDEPENDENT_RECEIPT_DATES",
                "usable_pairs":n,"distinct_pair_end_dates":len(distinct_days),
                "option_iv_forecast_calibrated":False}
    cut=distinct_days[max(0,int(len(distinct_days)*.7)-1)]
    train=np.array([(r[1],r[2]) for r in pairs if r[0]<=cut])
    test=np.array([(r[1],r[2]) for r in pairs if r[0]>cut])
    if len(train)<30 or len(test)<10:
        return {"status":"INSUFFICIENT_CHRONOLOGICAL_HOLDOUT",
                "usable_pairs":n,"training_pairs":len(train),"heldout_pairs":len(test),
                "option_iv_forecast_calibrated":False}
    # One pooled short-horizon cross-stock shock/spot relationship; not a
    # valid SPY/QQQ/IWM-specific option strike term surface model.
    x=train[:,1];y=train[:,0]
    slope=float(np.cov(x,y)[0,1]/max(float(np.var(x,ddof=1)),1e-9))
    slope=float(np.clip(slope,-25,25))
    intercept=float(np.median(y-slope*x))
    model=intercept+slope*test[:,1]
    naive=np.zeros(len(test))
    errors=np.abs(test[:,0]-model)
    baseline=np.abs(test[:,0]-naive)
    residual=y-(intercept+slope*x)
    lo,hi=np.quantile(residual,[.1,.9])
    coverage=np.mean((test[:,0]-model>=lo)&(test[:,0]-model<=hi))
    out={
        "status":"REFERENCE_SHORT_HORIZON_IV_CHANGE_ONLY",
        "usable_pairs":n,
        "exact_contracts":len(by_code),
        "distinct_pair_end_dates":len(distinct_days),
        "training_pairs":len(train),"heldout_pairs":len(test),
        "minimum_observed_gap_days":min(x[3] for x in pairs),
        "maximum_observed_gap_days":max(x[3] for x in pairs),
        "training_last_receipt_date":str(cut),
        "heldout_first_receipt_date":str(next(r[0] for r in pairs if r[0]>cut)),
        "pooled_iv_log_change_vs_spot_slope":round(slope,6),
        "model_absolute_log_iv_error":round(float(np.mean(errors)),6),
        "unchanged_iv_baseline_error":round(float(np.mean(baseline)),6),
        "nominal_80pct_heldout_interval_coverage":round(float(coverage),5),
        "distinct_holdout_dates":len(set(r[0] for r in pairs if r[0]>cut)),
        "assumed_cross_stock_transfer_to_spy_qqq_iwm":False,
        "independent_option_side_event_times":False,
        "21_session_option_iv_surface_available":False,
        "option_iv_forecast_calibrated":False,
        "executable_option_returns":False,
        "source":"original SQLite mode=ro application receipts; no raw vendor rows exported",
    }
    return out
