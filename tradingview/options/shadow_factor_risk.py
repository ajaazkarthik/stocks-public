"""SPY/QQQ/IWM historical UNDERLYING co-movement proxy, not option P/L.

One source-local daily history study (no additional subscription or licensed
options history). It cannot prove future joint option payoffs/filled spreads.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

SYMBOLS=("SPY","QQQ","IWM")

def underlying_factor_reference(histories):
    if set(histories)!=set(SYMBOLS):
        raise ValueError("ALL_THREE_MARKETS_REQUIRED")
    data={}
    for symbol in SYMBOLS:
        h=histories[symbol]
        if not isinstance(h,pd.DataFrame) or h.index.tz is None:
            raise ValueError("TIMESTAMPED_DAILY_PRICE_HISTORY_REQUIRED")
        h=h.sort_index()
        if len(h)<300 or h.index.has_duplicates:
            raise ValueError("INSUFFICIENT_DAILY_HISTORY")
        col=h["Close"].astype(float)
        if not np.isfinite(col.values).all() or not (col.values>0).all():
            raise ValueError("INVALID_DAILY_PRICES")
        data[symbol]=pd.Series(col.values,index=pd.to_datetime(h.index.date),name=symbol)
    merged=pd.concat([data[s] for s in SYMBOLS],axis=1,join="inner").dropna()
    if len(merged)<250:
        raise ValueError("INSUFFICIENT_PAIRED_DATES")
    ret=np.log(merged).diff().dropna()
    corr=ret.corr()
    avg=float((corr.to_numpy().sum()-3)/6)
    monthly=np.log(merged).diff(21).dropna()
    equal_log=monthly.mean(axis=1)
    return dict(status="HISTORICAL_UNDERLYING_FACTOR_PROXY_ONLY",
                source="completed Yahoo underlying closes; not OPRA option fills",
                paired_completed_days=len(merged),
                last_paired_date=str(merged.index[-1].date()),
                daily_log_return_correlation={
                    s:{t:round(float(corr.loc[s,t]),5) for t in SYMBOLS}
                    for s in SYMBOLS},
                average_off_diagonal_correlation=round(avg,5),
                equal_weight_underlying_worst_21_session_log_return=round(float(equal_log.min()),6),
                same_market_concentration_warning=avg>.5,
                observed_option_trade_pnl="NOT_MEASURED",
                future_option_iv_cross_correlation_calibrated=False,
                broker_positions_placed=0)
