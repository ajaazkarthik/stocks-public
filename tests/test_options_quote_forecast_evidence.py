"""Fail-closed quote provenance and historical chronological joint RV proxy."""
from datetime import datetime,timedelta,timezone
import copy
import numpy as np
import pandas as pd
import pytest

from tradingview.options.quote_evidence_research import audit,classify_ibkr_snapshot
from tradingview.options.joint_realized_proxy import (
    prepare,features,future_labels,train_at,validate_walk_forward,HORIZON
)

NOW="2026-10-26T15:30:00+00:00"
def exact_quote(**overrides):
    q=dict(source="mock_verified_side_feed",root="SPY",
           expiry="2026-12-04",option_type="CALL",strike=760.0,
           currency="USD",multiplier=100,deliverable="REGULAR",trading_class="SPY",
           bid=17.84,ask=17.93,top_status="REALTIME",
           timestamp_kind="exchange_side_event",
           provenance_independently_verified=True,market_session_verified=True,
           bid_at="2026-10-26T15:29:55+00:00",ask_at="2026-10-26T15:29:56+00:00")
    q.update(overrides)
    return q

def test_independent_side_event_gate_is_technical_only_not_trade():
    x=audit(exact_quote(),decision_at=NOW)
    assert x["status"]=="TIMESTAMP_QUALIFIED_NOT_A_FILL"
    assert x["identity_verified"] is True
    assert x["side_times_verified"] is True
    assert x["executable"] is False

@pytest.mark.parametrize("change,reason",[
    ({"top_status":"FROZEN_DELAYED"},"NOT_REALTIME_EVIDENCE"),
    ({"timestamp_kind":"generic_receipt"},"SIDE_EVENT_PROVENANCE_UNVERIFIED"),
    ({"provenance_independently_verified":False},"SIDE_EVENT_PROVENANCE_UNVERIFIED"),
    ({"bid_at":None},"MISSING_TIMESTAMP"),
    ({"bid_at":"2026-10-26T15:28:55+00:00"},"SIDE_EVENT_NOT_FRESH"),
    ({"ask_at":"2026-10-26T15:29:48+00:00"},"SIDES_NOT_SYNCHRONIZED"),
    ({"ask":17.30},"CROSSED_MARKET"),
    ({"multiplier":10},"NONSTANDARD_OR_AMBIGUOUS_CONTRACT"),
    ({"trading_class":"2SPY"},"NONSTANDARD_OR_AMBIGUOUS_CONTRACT"),
    ({"market_session_verified":False},"MARKET_SESSION_UNVERIFIED"),
])
def test_unverified_price_blocks_even_if_other_fields_populated(change,reason):
    q=exact_quote(**change)
    r=audit(q,decision_at=NOW)
    assert r["status"]=="BLOCKED"
    assert r["reason"]==reason
    assert r["executable"] is False

def test_ibkr_frozen_option_quote_cannot_fake_last_trade_event():
    identity={k:exact_quote()[k] for k in
        ("root","expiry","option_type","strike","currency","multiplier","deliverable","trading_class")}
    raw={"top-status":{"status":"FROZEN_DELAYED"},"bid-ask":{"bid":17.84,"ask":17.93},
         "last":{"price":17.90,"ts":1792786458}}
    out=classify_ibkr_snapshot(identity,raw,at=NOW)
    assert out["reason"]=="NOT_REALTIME_EVIDENCE"
    raw["top-status"]["status"]="REALTIME"
    out=classify_ibkr_snapshot(identity,raw,at=NOW)
    assert out["reason"]=="SIDE_EVENT_PROVENANCE_UNVERIFIED"
    assert out["side_times_verified"] is False

def synthetic_history(n=640,seed=3):
    g=np.random.default_rng(seed)
    returns=g.normal(0.0002,.01,n)
    for j in range(70,n,200):returns[j:min(j+30,n)]*=1.5
    close=100*np.exp(np.cumsum(returns))
    index=pd.date_range("2022-01-03",periods=n,freq="B",tz="America/New_York")
    return pd.DataFrame({"Close":close},index=index)

def test_training_labels_embargo_and_no_future_revision():
    dates,price,ret=prepare(synthetic_history())
    p=train_at(ret,500)
    assert p["training_last_label_index"]<=500
    assert p["training_count"]>=252
    mutated=ret.copy()
    mutated[501:]=100
    new=train_at(mutated,500)
    assert np.allclose(new["forecast"],p["forecast"])
    assert np.allclose(new["lo"],p["lo"])
    assert np.allclose(new["hi"],p["hi"])

def test_calibration_reports_nonoverlapping_heldout_and_no_option_iv():
    result=validate_walk_forward(synthetic_history())
    assert result["walkforward_evaluation_count"]>=12
    assert result["evaluation_spacing_sessions"]==21
    assert result["leakage_embargo_sessions"]==21
    assert result["edge_engine_forecast_eligible"] is False
    assert result["calibrated_option_implied_vol_surface"] is False
    assert result["historical_vendor_vintage_certified"] is False
    assert result["profitability_measured"] is False

def test_missing_timezone_and_duplicate_bars_rejected():
    hist=synthetic_history()
    with pytest.raises(ValueError,match="TZ_AWARE"):
        prepare(hist.tz_localize(None))
    other=pd.concat([hist,hist.iloc[-1:]])
    with pytest.raises(ValueError,match="INSUFFICIENT"):
        prepare(other)

def test_yfinance_current_day_disallowed_in_training_results():
    df=synthetic_history()
    last=df.iloc[-1:]
    res=validate_walk_forward(df)
    assert res["date_last"]==str(last.index[-1].date())
    assert not res["edge_engine_forecast_eligible"]

def test_insufficient_training_fails_closed():
    h=synthetic_history(530)
    _,_,returns=prepare(h)
    with pytest.raises(ValueError,match="INSUFFICIENT_PURGED"):
        train_at(returns,250)


def test_deduped_provenance_store_never_retains_raw_quote_or_credentials(tmp_path):
    from tradingview.options.quote_evidence_store import archive
    path=tmp_path/"private"/"research.sqlite"
    q=exact_quote(top_status="FROZEN_DELAYED",source="IBKR",api_key="DO_NOT_SAVE")
    a=archive(path,q,decision_at=NOW)
    b=archive(path,q,decision_at=NOW)
    assert a["evidence_sha256"]==b["evidence_sha256"]
    assert a["local_evidence_records"]==b["local_evidence_records"]==1
    assert path.stat().st_mode&0o777==0o600
    raw=path.read_bytes()
    assert b"DO_NOT_SAVE" not in raw
    assert b"17.84" not in raw and b"17.93" not in raw
    assert b"FROZEN_DELAYED" in raw
    assert a["live_orders"]==0


def test_evidence_store_world_readable_and_production_db_rejected(tmp_path):
    from tradingview.options.quote_evidence_store import archive
    p=tmp_path/"research.sqlite"
    p.write_bytes(b"")
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        archive(p,exact_quote(),decision_at=NOW)
    with pytest.raises(ValueError):
        archive(tmp_path/"options_paper.db",exact_quote(),decision_at=NOW)


def test_model_is_not_accepted_as_options_joint_iv_forecast():
    from tradingview.options.edge_engine import validate_forecast
    from tests.test_option_paper_scanner import observation,NOW as FROZEN_NOW
    model=validate_walk_forward(synthetic_history())
    with pytest.raises(ValueError,match="FORECAST"):
        validate_forecast(model,observation(),FROZEN_NOW)
