"""Existing-data-only reference modeling never promotes estimated trades."""
from datetime import datetime,timezone,timedelta
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from tradingview.options import existing_data_shadow as s
from tradingview.options.edge_engine import validate_forecast
from tests.test_option_paper_scanner import NOW,observation
from tests.test_options_quote_forecast_evidence import synthetic_history


def aligned():
    h=synthetic_history()
    h.index=pd.date_range(end="2026-10-23",periods=len(h),freq="B",
                          tz="America/New_York")
    obs=observation()
    return obs,h


def test_full_reference_proxy_paths_are_chronological_and_forecast_unverified():
    obs,h=aligned()
    f=s.build_reference_forecast(obs,h,NOW)
    assert f["forecast_status"]=="REFERENCE_PROXY_UNVERIFIED"
    assert f["verified_option_iv_forecast"] is False
    assert len(f["scenarios"])==8
    assert f["training_cutoff"]<f["created_at"]
    assert f["created_at"]==NOW.isoformat()
    # V1 exits after 21 CALENDAR days, not 21 trading sessions.
    assert 14<=len(f["scenarios"][0]["path"])<=19
    assert f["scenarios"][0]["path"][-1]["at"]>=(NOW+timedelta(days=21)).isoformat()
    validate_forecast(f,obs,NOW)
    last=h.index[-1].date()
    for scenario in f["scenarios"]:
        assert scenario["path"][0]["at"]>NOW.isoformat()
        assert all(p["funding_cost_usd"]==0 and p["assignment_cost_usd"]==0 for p in scenario["path"])
    assert f["mean_estimation_error_usd"]==20.0


def test_reproducible_scenarios_at_fixed_vintage():
    obs,h=aligned()
    a=s.build_reference_forecast(obs,h,NOW)
    b=s.build_reference_forecast(obs,h,NOW)
    assert a==b


def test_future_price_history_mutation_after_receipt_excluded():
    obs,h=aligned()
    newer=h.copy()
    newer.loc[pd.Timestamp("2026-10-26",tz="America/New_York")]=999999
    with pytest.raises(ValueError,match="UNFINISHED"):
        s.build_reference_forecast(obs,newer,NOW)


def test_forecast_clock_cannot_be_backdated_to_pretend_calibrated():
    obs,h=aligned()
    f=s.build_reference_forecast(obs,h,NOW)
    f["created_at"]=(NOW-timedelta(minutes=20)).isoformat()
    with pytest.raises(ValueError,match="FUTURE_INFORMATION"):
        validate_forecast(f,obs,NOW)


def test_invalid_or_nonstandard_iv_does_not_get_imputed():
    obs,h=aligned()
    obs["quotes"][0]["iv"]=None
    with pytest.raises((ValueError,TypeError)):
        s.build_reference_forecast(obs,h,NOW)


def test_tv_iv_percent_converts_to_fraction_only_for_real_tv_provider():
    obs,h=aligned()
    q=obs["quotes"][0]
    q["provider"]="TradingView scan2"
    q["iv"]=20.0
    f=s.build_reference_forecast(obs,h,NOW)
    from tradingview.options.paper_scanner import contract_key
    assert all(.05<=r["path"][0]["ivs"][contract_key(q)]<=.25
               for r in f["scenarios"])


def test_model_shadow_never_send_or_fabricated_profit():
    obs,h=aligned()
    f=s.build_reference_forecast(obs,h,NOW)
    out=s.safe_comparison(obs,f,NOW)
    assert out["baseline_status"]=="CANDIDATE"
    assert out["broker_orders"]==0
    assert out["notifier_eligible"] is False
    assert out["realized_net_pnl"]=="NOT_MEASURED"
    assert out["shadow_fill_status"]=="NOT_OBSERVED"
    assert out["price_limit_tradable"] is False
    assert out["forecast_status"]=="REFERENCE_PROXY_UNVERIFIED"


def test_provider_matrix_preserves_separate_sources_and_denies_execution():
    evidence={"cboe_oi":{"rows":[{"status":"REFERENCE_ONLY"}]},
      "moomoo":{"rows":[{"status":"REFERENCE_ONLY"}]},
      "iv_smile":{"expiries":[{"status":"REFERENCE_ONLY"}]},
      "tqs_scores":{"rows":[{"status":"INSUFFICIENT_INPUTS"}]},
      "strict_evidence":{"CALL":{"status":"BLOCKED_UNVERIFIED_SIDES"}}}
    m=s.provider_matrix(evidence)
    assert m["cboe_free"]["rows"]==1
    assert m["moomoo"]["quote_event_times_certified"] is False
    assert m["strict_side_gates"]==["BLOCKED_UNVERIFIED_SIDES"]
    assert m["combined_sources_do_not_prove_independent_side_event_clock"] is True


def test_readonly_archives_aggregate_counts_and_absent_db(tmp_path):
    with sqlite3.connect(tmp_path/"alert_tracker.db") as c:
        c.execute("CREATE TABLE options_scan_results(created_at TEXT, code TEXT,bid REAL,ask REAL,iv REAL)")
        c.execute("INSERT INTO options_scan_results VALUES ('2026-05-11','CODE_A',1,2,.3)")
    output=s.audit_existing_archives(tmp_path)
    assert output["legacy_options_scans"]["rows"]==1
    assert output["legacy_options_scans"]["has_bid_ask_fields"]
    assert output["legacy_options_scans"]["has_independent_side_event_fields"] is False
    assert output["tqs_v2_scans"]["status"]=="UNAVAILABLE"


def test_private_shadow_journal_idempotent_no_fills(tmp_path):
    obs,h=aligned()
    report=s.safe_comparison(obs,s.build_reference_forecast(obs,h,NOW),NOW)
    db=tmp_path/"research"/"shadow.db"
    a=s.record_research(report,db)
    b=s.record_research(report,db)
    assert a["total_shadow_decisions"]==b["total_shadow_decisions"]==1
    assert db.stat().st_mode&0o777==0o600
    with sqlite3.connect(db) as c:
        fields=c.execute("SELECT evidence_class,forward_outcome FROM shadow_decisions").fetchone()
    assert fields==("REFERENCE_ONLY","NOT_MEASURED")


def test_no_profit_insertion_allowed(tmp_path):
    obs,h=aligned()
    output=s.safe_comparison(obs,s.build_reference_forecast(obs,h,NOW),NOW)
    output["realized_net_pnl"]=1234
    with pytest.raises(ValueError,match="OBSERVED_PROFIT"):
        s.record_research(output,tmp_path/"x.db")


def test_no_running_orders_or_notification_without_forecast():
    obs,_=aligned()
    x=s.safe_comparison(obs,None,NOW)
    assert x["challenger_status"]=="MISSING_POINT_IN_TIME_JOINT_FORECAST"
    assert x["notifier_eligible"] is False
    assert x["broker_orders"]==0
    assert x["model_only_price_limit"] is None


def test_us_closed_one_symbol_does_not_abort_other_five_source_research(monkeypatch,tmp_path,capsys):
    from tradingview.scheduler.market_calendar import CalendarCoverageError
    from tradingview.options import legacy_iv_evidence
    monkeypatch.setattr(s,"audit_existing_archives",lambda folder:{})
    monkeypatch.setattr(legacy_iv_evidence,"evaluate",lambda *a,**kw:{"status":"REFERENCE_ONLY"})
    def trial(symbol,**kw):
        if symbol=="AMEX:IWM":
            raise CalendarCoverageError("US is closed")
        return {"symbol":symbol,"broker_orders":0,"realized_net_pnl":"NOT_MEASURED"}
    monkeypatch.setattr(s,"run_symbol",trial)
    assert s.main(["--scan-all","--source-dir",str(tmp_path)])==0
    x=json.loads(capsys.readouterr().out)
    assert x["shadows"]["AMEX:IWM"]["status"]=="MARKET_CLOSED_OR_CALENDAR_UNAVAILABLE"
    assert x["shadows"]["AMEX:SPY"]["broker_orders"]==0
    assert x["shadows"]["NASDAQ:QQQ"]["realized_net_pnl"]=="NOT_MEASURED"
