"""F-01/F-02 must pass. Synthetic reference data; not trade P&L."""
from datetime import datetime,timezone,timedelta
import sqlite3
import pytest

from tests.test_option_paper_scanner import NOW,observation,quote
from tradingview.options.paper_scanner import choose_candidate
from tradingview.options import edge_engine as e
from tradingview.options.existing_data_shadow import safe_comparison,record_research
from tradingview.options.shadow_repricing import one_arm,reconcile


def large_universe():
    obs=observation()
    for idx in range(1,20):
        if idx!=4:
            obs["quotes"].append(quote(100+idx*.5,"CALL",at=NOW,delta=.55,bid=2,ask=2.04))
    _,status=choose_candidate(obs,NOW)
    candidates,_=e.enumerate_frozen_candidates(obs,NOW)
    assert status=="CANDIDATE" and len(candidates)>100
    return obs,candidates


def test_hypothetical_reference_mark_not_allowed_after_expiry():
    obs=observation()
    candidate,status=choose_candidate(obs,NOW)
    assert status=="CANDIDATE"
    expired=datetime(2026,12,7,15,tzinfo=timezone.utc)
    assert one_arm(candidate,observation(at=expired),expired)==("EXPIRED_OR_PAST_HORIZON",None)


def test_after_21_calendar_days_from_decision_is_not_valid_reference_exit():
    candidate,_=choose_candidate(observation(),NOW)
    future=NOW+timedelta(days=22)
    assert one_arm(candidate,observation(at=future),future,decision_at=NOW)==(
        "EXPIRED_OR_PAST_HORIZON",None)


def test_past_pre_expiry_exit_window_is_not_valid_reference_exit():
    candidate,_=choose_candidate(observation(),NOW)
    late=datetime(2026,11,30,15,tzinfo=timezone.utc)
    assert one_arm(candidate,observation(at=late),late)==(
        "PAST_MANDATORY_PRE_EXPIRY_EXIT_WINDOW",None)


def test_reconciliation_uses_recorded_decision_clock(tmp_path):
    record=tmp_path/"shadow.db"
    record_research(safe_comparison(observation(),None,NOW),record)
    future=NOW+timedelta(days=22)
    result=reconcile(record,observation(at=future),future)
    assert result["compared"]==1 and result["with_both_reference_marks"]==0
    with sqlite3.connect(record) as c:
        row=c.execute("SELECT baseline_status,baseline_reference_net_usd,observed_fill_status FROM shadow_reference_reprices").fetchone()
    assert row==("EXPIRED_OR_PAST_HORIZON",None,"NO_BROKER_FILL_EVIDENCE")


def test_many_eligible_spreads_no_longer_stop_comparison():
    obs,candidates=large_universe()
    result=safe_comparison(obs,None,NOW)
    assert result["baseline_status"]=="CANDIDATE"
    assert result["eligible_frozen_count"]==len(candidates)
    assert result["challenger_status"]=="MISSING_POINT_IN_TIME_JOINT_FORECAST"
    assert result["broker_orders"]==0


@pytest.mark.parametrize("n",[13,50,132])
def test_all_candidates_ranked_in_bounded_batches(monkeypatch,n):
    obs,candidates=large_universe()
    subset=candidates[:n]
    monkeypatch.setattr(e,"enumerate_frozen_candidates",lambda *a,**k:(subset,{}))
    monkeypatch.setattr(e,"validate_forecast",lambda *a,**k:None)
    seen=[]
    def value(c,*a,**k):
        seen.append(e.encode(c["legs"]))
        return dict(edge_above_hurdle=True,conservative_mean_proxy_usd=50.,
                    expected_net_usd=55.)
    monkeypatch.setattr(e,"value_candidate",value)
    monkeypatch.setattr(e,"price_limit",lambda c,*a,**k:dict(
        status="MODEL_ONLY_PRICE_CEILING" if c["kind"]=="DEBIT" else "MODEL_ONLY_MINIMUM_CREDIT",
        limit_quote=5.0 if c["kind"]=="DEBIT" else .01,
        limit_usd=500 if c["kind"]=="DEBIT" else 1,complete_grid=True))
    result=e.compare_frozen_to_edge(obs,NOW,{},max_candidates=12)
    assert result["considered"]==n
    assert len(result["ranked"])==n and len(set(seen))==n
    assert result["evaluation_batches"]==(n+11)//12
    assert result["candidate_coverage"]=="ALL_ELIGIBLE_CANDIDATES_ACCOUNTED_FOR"
    assert result["selection"] is not None
    assert result["paper_orders"]==0 and result["notifier_eligible"] is False


def test_invalid_batch_size_rejected():
    with pytest.raises(ValueError,match="INVALID_EVALUATION_BATCH_SIZE"):
        e.compare_frozen_to_edge(observation(),NOW,max_candidates=0)
