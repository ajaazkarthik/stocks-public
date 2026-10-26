from datetime import datetime,timedelta,timezone
import math
import sqlite3
import pytest

from tradingview.options import architecture_forecast_validation as f
from tradingview.options import architecture_allocation as a
from tradingview.options import architecture_paired_evaluation as p

NOW='2026-10-26T15:00:00+00:00'
TARGET='2026-11-16T21:00:00+00:00'
KEYS=('AMEX:SPY|2026-12-04|760|CALL','AMEX:SPY|2026-12-04|765|CALL')

def forecast_fixture():
    bars=[]
    begin=datetime(2026,9,1,tzinfo=timezone.utc)
    for i in range(40):
        bars.append(((begin+timedelta(days=i)).isoformat(),750.+i*.25))
    return dict(symbol='AMEX:SPY',observed_at=NOW,created_at='2026-10-26T15:00:01+00:00',
                target_at=TARGET,completed_bars=bars,observed_spot=759.75,future_sessions=15,
                exact_quotes=[dict(key=k,iv=.2+i*.01,provider='Mock') for i,k in enumerate(KEYS)],
                path_predictions=[dict(probability=.5,spot=s,iv_by_key={KEYS[0]:v,KEYS[1]:v+.01})
                                  for s,v in ((770.,.19),(790.,.23))])


def candidate(label):
    return {'expiry':'2026-12-04','qty':1,'legs':[{'key':k,'side':1 if i==0 else -1}
                                        for i,k in enumerate(KEYS)],'id':label}

def quotes(ts, price=2.):
    return {k:dict(basis='reference',bid=price+i,ask=price+i+.10,updated_at=ts)
            for i,k in enumerate(KEYS)}

def position(ident,net,risk,collateral,shock,qty=1):
    return {'id':ident,'conservative_net_usd':net,'risk_usd':risk,'collateral_usd':collateral,
            'max_qty':qty,'stress_losses':{'crash':shock,'iv_shock':shock*.6}}

# A — forecasts

def test_forecast_three_arms_and_before_outcomes():
    v=f.make_vintage(**forecast_fixture())
    assert v['option_iv_calibrated'] is False
    assert not v['notifier_eligible']
    assert set(v['predictions'][KEYS[0]])=={'unchanged','momentum','path_proxy'}
    assert v['predictions'][KEYS[0]]['path_proxy']['iv']==pytest.approx(.21)
    assert v['predictions'][KEYS[0]]['path_proxy']['spot']==780
    assert v['predictions'][KEYS[0]]['unchanged']['spot']==759.75


def test_forecast_rejects_newer_history():
    kw=forecast_fixture();kw['completed_bars'].append((NOW,777.))
    with pytest.raises(ValueError,match='UNFINISHED'):
        f.make_vintage(**kw)


def test_forecast_requires_unique_options():
    kw=forecast_fixture();kw['exact_quotes'].append(kw['exact_quotes'][0])
    with pytest.raises(ValueError,match='DUPLICATE'):
        f.make_vintage(**kw)


def test_forecast_rejects_invalid_scenario_probability():
    kw=forecast_fixture();kw['path_predictions'][0]['probability']=.99
    with pytest.raises(ValueError,match='PATH_WEIGHTS'):
        f.make_vintage(**kw)


def test_save_requires_owner_only_and_is_idempotent(tmp_path):
    file=tmp_path/'research'/'vintages.sqlite'
    v=f.make_vintage(**forecast_fixture())
    assert f.record(file,v)==f.record(file,v)
    assert file.stat().st_mode&0o777==0o600
    with sqlite3.connect(file) as db:
        assert db.execute('select count(*) from forecast_vintage').fetchone()[0]==1


def test_later_observations_are_exact_contract_and_never_fills(tmp_path):
    file=tmp_path/'forecasts.sqlite';f.record(file,f.make_vintage(**forecast_fixture()))
    early=f.match_future(file,symbol='AMEX:SPY',observed_at='2026-11-13T21:00:00+00:00',
                         exact_quotes=[],observed_spot=780)
    assert early['new_outcomes']==0
    later=f.match_future(file,symbol='AMEX:SPY',observed_at=TARGET,
                         exact_quotes=[dict(key=KEYS[0],iv=.22,provider='Mock')],observed_spot=779.)
    assert later=={'new_outcomes':1,'actual_fills':0}
    assert f.match_future(file,symbol='AMEX:SPY',observed_at=TARGET,
                          exact_quotes=[],observed_spot=100)['new_outcomes']==0
    stats=f.compare(file)
    assert stats['matched_vintages']==1 and stats['independent_target_dates']==1
    assert stats['models']['unchanged']['pairs']==1
    assert stats['nominal_80_percent_calibration_proven'] is False
    assert stats['actual_trading_profit']=='NOT_MEASURED'


def test_tradingview_iv_percent_fraction_conversion():
    assert f.normalize_iv(dict(iv=20.,provider='TradingView scan2'))==pytest.approx(.2)
    with pytest.raises(ValueError):f.normalize_iv(dict(iv=20.,provider='Mock'))

# B — integer allocation

def test_account_net_profit_over_ratio_one_slot():
    # $30/140 higher ratio but $45/225 larger conservative net dollars.
    rows=[position('A',30,140,140,150),position('B',45,225,225,245)]
    result=a.allocate(rows,available_cash=300,risk_budget=1000,per_position_risk=250,
                      shared_stress_budget=300,max_positions=1)
    assert result['positions']==[{'id':'B','qty':1}]
    assert result['modelled_net_usd']==45
    assert result['broker_orders']==0


def test_cash_abstention_when_no_modelled_edge():
    result=a.allocate([position('A',-10,140,140,170)],available_cash=300,
                      shared_stress_budget=200)
    assert result['positions']==[] and result['modelled_net_usd']==0


def test_joint_risk_and_stress_prevent_overallocation():
    rows=[position('A',30,200,100,350),position('B',45,200,100,350)]
    out=a.allocate(rows,available_cash=1000,shared_stress_budget=500)
    assert len(out['positions'])==1
    assert out['worst_joint_stress_usd']<=500


def test_existing_reserved_risk_prevents_spend():
    out=a.allocate([position('A',30,200,100,50)],available_cash=500,
                   existing_reserved_risk=900,shared_stress_budget=200)
    assert out['positions']==[]


def test_stress_missing_is_not_silently_zero():
    result=a.allocate([position('A',100,200,200,100)],available_cash=1000)
    assert result['status']=='UNASSESSED_SHARED_STRESS'
    assert not result['positions']


def test_stress_scenario_alignment_is_required():
    items=[position('A',20,150,150,100),position('B',30,200,200,100)]
    items[1]['stress_losses']={'other':90,'iv_shock':40}
    with pytest.raises(ValueError,match='STRESS_SCENARIO_CLOCK'):
        a.allocate(items,available_cash=1000,shared_stress_budget=300)


def test_available_collateral_limits_position():
    out=a.allocate([position('A',80,200,500,70)],available_cash=100,
                   shared_stress_budget=500)
    assert out['positions']==[]

# C — paired reference lifecycle

def test_matched_reference_comparison_not_real_fills():
    s=p.start(cohort_id='1',decision_at=NOW,mode='ranking_only',cash=1000,
              baseline=candidate('FROZEN'),challenger=candidate('EDGE'))
    entry='2026-10-26T15:01:00+00:00';exit='2026-11-02T16:00:00+00:00'
    for arm in ('baseline','challenger'):
        assert p.reference_cross(s,arm,event_at=entry,quotes=quotes(entry),phase='entry')['status']=='REFERENCE_ENTRY'
        assert p.reference_cross(s,arm,event_at=exit,quotes=quotes(exit),phase='exit')['status']=='REFERENCE_CLOSED'
    report=p.summarize(s)
    assert report['both_arms_comparable']
    assert report['hypothetical_difference_usd']==0
    assert report['broker_fills']==0 and report['actual_broker_pnl']=='NOT_MEASURED'


def test_exit_without_entry_unresolved():
    s=p.start(cohort_id='2',decision_at=NOW,mode='ranking_only',cash=1000,
              baseline=candidate('a'),challenger=candidate('b'))
    ts='2026-10-26T15:03:00+00:00'
    r=p.reference_cross(s,'baseline',event_at=ts,quotes=quotes(ts),phase='exit')
    assert r['status']=='NO_ENTRY_REFERENCE'
    assert p.summarize(s)['arms']['baseline']['status']=='UNRESOLVED'


def test_post_expiry_and_max_hold_rejected():
    s=p.start(cohort_id='3',decision_at=NOW,mode='full_policy',cash=1000,
              baseline=candidate('a'),challenger=None)
    post='2026-12-07T15:03:00+00:00'
    out=p.reference_cross(s,'baseline',event_at=post,quotes=quotes(post),phase='entry')
    assert out['status']=='OUTSIDE_POLICY_TIME_WINDOW'


def test_cash_compared_fairly_only_full_policy():
    s=p.start(cohort_id='4',decision_at=NOW,mode='full_policy',cash=1000,
              baseline=None,challenger=None)
    r=p.summarize(s)
    assert r['both_arms_comparable']
    assert r['hypothetical_difference_usd']==0
    assert r['arms']['baseline']['status']=='CASH'


def test_unpaired_participation_is_not_ranking_only_advantage():
    s=p.start(cohort_id='5',decision_at=NOW,mode='ranking_only',cash=1000,
              baseline=candidate('a'),challenger=None)
    assert not p.summarize(s)['both_arms_comparable']
    assert p.summarize(s)['hypothetical_difference_usd'] is None


def test_only_reference_quote_basis_accepted():
    s=p.start(cohort_id='6',decision_at=NOW,mode='ranking_only',cash=1000,
              baseline=candidate('a'),challenger=candidate('b'))
    ts='2026-10-26T15:01:00+00:00';book=quotes(ts)
    book[KEYS[0]]['basis']='verified'
    assert p.reference_cross(s,'baseline',event_at=ts,quotes=book,phase='entry')['status']=='MISSING_REFERENCE'


def test_entry_late_is_not_assumed_to_fill():
    s=p.start(cohort_id='7',decision_at=NOW,mode='ranking_only',cash=1000,
              baseline=candidate('a'),challenger=candidate('b'))
    ts='2026-10-26T15:45:00+00:00'
    assert p.reference_cross(s,'baseline',event_at=ts,quotes=quotes(ts),phase='entry')['status']=='ENTRY_WINDOW_ELAPSED'


def test_no_pre_decision_fills():
    s=p.start(cohort_id='8',decision_at=NOW,mode='full_policy',cash=1000,
              baseline=candidate('a'),challenger=None)
    with pytest.raises(ValueError,match='NO_FUTURE_EVIDENCE'):
        p.reference_cross(s,'baseline',event_at=NOW,quotes=quotes(NOW),phase='entry')


def test_target_day_morning_cannot_stand_in_for_later_close(tmp_path):
    path=tmp_path/'forecasts.db';f.record(path,f.make_vintage(**forecast_fixture()))
    result=f.match_future(path,symbol='AMEX:SPY',
        observed_at='2026-11-16T15:00:00+00:00',
        exact_quotes=[dict(key=KEYS[0],iv=.21,provider='Mock')],observed_spot=775.)
    assert result['new_outcomes']==0

def test_exit_cannot_precede_entry_time():
    state=p.start(cohort_id='later',decision_at=NOW,mode='full_policy',cash=1000,
                  baseline=candidate('b'),challenger=None)
    entry='2026-10-26T15:05:00+00:00'
    assert p.reference_cross(state,'baseline',event_at=entry,quotes=quotes(entry),phase='entry')['status']=='REFERENCE_ENTRY'
    before='2026-10-26T15:04:00+00:00'
    assert p.reference_cross(state,'baseline',event_at=before,quotes=quotes(before),phase='exit')['status']=='EXIT_PRE_DATES_ENTRY'
    assert state['baseline']['exit'] is None


def test_over_twelve_candidates_are_considered_without_false_optimality():
    rows=[position(f"C{i:03d}",25+i,200,200,60) for i in range(132)]
    result=a.allocate(rows,available_cash=800,shared_stress_budget=500,max_positions=5)
    assert result['status']=='MODEL_ONLY_HEURISTIC_NOT_PROVEN_OPTIMAL'
    assert result['candidate_count']==132
    assert result['all_candidates_considered']
    assert result['broker_orders']==0
    assert len(result['positions'])>0


def test_missing_reference_then_later_exact_quote_is_not_lost(tmp_path):
    path=tmp_path/'archive.sqlite'
    f.record(path,f.make_vintage(**forecast_fixture()))
    r=f.match_future(path,symbol='AMEX:SPY',observed_at=TARGET,exact_quotes=[],observed_spot=780)
    assert r['new_outcomes']==1
    later='2026-11-17T21:00:00+00:00'
    r=f.match_future(path,symbol='AMEX:SPY',observed_at=later,
        exact_quotes=[dict(key=KEYS[0],iv=.21,provider='Mock')],observed_spot=780)
    assert r['new_outcomes']==1
    stats=f.compare(path)
    assert stats['matched_vintages']==1 and stats['later_observation_rows']==2
    assert stats['models']['unchanged']['pairs']==1

def test_vintage_hash_tampering_is_rejected(tmp_path):
    original=f.make_vintage(**forecast_fixture())
    original['predictions'][KEYS[0]]['unchanged']['iv']=4.0
    with pytest.raises(ValueError,match='HASH_MISMATCH'):
        f.record(tmp_path/'research.sqlite',original)
