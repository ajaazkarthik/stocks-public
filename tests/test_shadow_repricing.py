"""Paired forward REFERENCE quote stress metrics are never real option fills."""
from datetime import timedelta
import sqlite3

from tradingview.options import existing_data_shadow as s
from tradingview.options.shadow_repricing import one_arm,reconcile
from tradingview.options.paper_scanner import choose_candidate
from tests.test_option_paper_scanner import NOW,observation
from tests.test_options_quote_forecast_evidence import synthetic_history
import pandas as pd


def aligned():
    h=synthetic_history()
    h.index=pd.date_range(end="2026-10-23",periods=len(h),freq="B",tz="America/New_York")
    obs=observation()
    return obs,h


def test_initial_run_and_future_reference_cross_no_real_pnl(tmp_path):
    obs,h=aligned()
    f=s.build_reference_forecast(obs,h,NOW)
    output=s.safe_comparison(obs,f,NOW)
    path=tmp_path/"research.sqlite"
    assert reconcile(path,obs,NOW)["status"]=="NO_PREVIOUS_SHADOW_DECISIONS"
    assert s.record_research(output,path)["total_shadow_decisions"]==1
    later=NOW+timedelta(minutes=1)
    next_obs=observation(at=later)
    r=reconcile(path,next_obs,later)
    assert r["status"]=="REFERENCE_ONLY_NOT_TRADING_PERFORMANCE"
    assert r["compared"]==1
    assert r["actual_broker_fills"]==0
    assert r["net_realized_return"]=="NOT_MEASURED"
    assert reconcile(path,next_obs,later)["stored"]==r["stored"]
    with sqlite3.connect(path) as c:
        rows=c.execute("SELECT baseline_reference_net_usd,edge_reference_net_usd,"
                       "observed_fill_status FROM shadow_reference_reprices").fetchall()
    assert len(rows)==1
    assert rows[0][0] is not None
    assert rows[0][2]=="NO_BROKER_FILL_EVIDENCE"


def test_no_exact_future_option_leg_never_fabricated(tmp_path):
    obs,h=aligned()
    out=s.safe_comparison(obs,s.build_reference_forecast(obs,h,NOW),NOW)
    path=tmp_path/"research.sqlite"
    s.record_research(out,path)
    later=NOW+timedelta(minutes=1)
    new=observation(at=later)
    new["quotes"].pop()
    r=reconcile(path,new,later)
    assert r["compared"]==1
    with sqlite3.connect(path) as c:
        row=c.execute("SELECT baseline_status,baseline_reference_net_usd "
                      "FROM shadow_reference_reprices").fetchone()
    assert row==("EXACT_FUTURE_QUOTE_UNAVAILABLE",None)


def test_cash_is_explicit_zero_not_invented_profit():
    x=observation()
    c,_=choose_candidate(x,NOW)
    a,b=one_arm(None,x,NOW)
    assert a=="CASH_ABSTAIN" and b==0.0
    a,b=one_arm(c,x,NOW)
    assert a=="UNVERIFIED_REFERENCE_MARK"
    assert b is not None and b<0
