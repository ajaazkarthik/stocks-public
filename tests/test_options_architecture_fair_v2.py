from datetime import datetime,timedelta,timezone
from copy import deepcopy
import sqlite3
import pytest
from tradingview.options.architecture_fair_experiment import decision_snapshot,choose_studies,replay
from tradingview.options.architecture_research_integration import assemble,assemble_account
from tradingview.options import architecture_forecast_validation as f
from tests.test_options_architecture_integration import setup,KEY,NOW,TARGET
from tests.test_options_architecture_milestones import forecast_fixture,candidate,KEYS


import sys,types
@pytest.fixture(autouse=True)
def stub_frozen_contract_key(monkeypatch):
    paper=types.ModuleType('tradingview.options.paper_scanner')
    paper.contract_key=lambda q:f"{q['symbol']}|{q['expiry']}|{q['strike']:g}|{q['option_type']}"
    monkeypatch.setitem(sys.modules,'tradingview.options.paper_scanner',paper)

def test_same_day_spot_100_to_110_and_single_vintage():
    x=setup();x['observation']['quotes'][0]['spot']=110
    x['forecast']['start_spot']=110
    x['observation']['features']['close']=100
    d=assemble(**x)
    v=d['forecast_vintage']
    assert d['common_decision_snapshot']['spot']==110
    assert v['common_initial_underlying_spot']==110
    assert v['predictions'][KEY]['unchanged']['spot']==110
    assert v['predictions'][KEY]['momentum']['spot']!=100
    assert v['common_initial_option_ivs'][KEY]==pytest.approx(.22)


def test_start_spot_mismatch_rejected():
    x=setup();x['observation']['quotes'][0]['spot']=110
    x['forecast']['start_spot']=100
    with pytest.raises(ValueError,match='UNEQUAL_FORECAST_START_SPOT'):
        assemble(**x)


def test_asynchronous_quote_spots_rejected():
    x=setup();x['observation']['quotes'][0]['spot']=110
    x['observation']['quotes'].append({**x['observation']['quotes'][0], 'strike':765,'spot':90})
    with pytest.raises(ValueError,match='CONFLICTING_DECISION_SPOT_QUOTES'):
        decision_snapshot(x['observation'],{'start_spot':110})


def build_portfolio():
    base=candidate('BASE');base.update(symbol='AMEX:SPY',risk_per_spread=50.,kind='DEBIT',direction='BULL')
    selected=candidate('SELECT');selected.update(symbol='AMEX:SPY',risk_per_spread=50.,kind='DEBIT',direction='BULL')
    row=dict(candidate=selected,valuation=dict(conservative_mean_proxy_usd=20.,
             projection_within_frozen_risk_budget=True,max_contractual_loss_usd=50.,
             projected_max_liquidation_loss_usd=50.),
             price_limit=dict(status='MODEL_ONLY_PRICE_CEILING',limit_quote=1.2),
             reason='PROXY_EDGE_CLEARS_HURDLE')
    x=setup();x['comparison']['baseline']['candidate']=base
    x['comparison']['ranked']=[row]
    x['comparison']['selection']={'candidate':selected}
    x['forecast']['start_spot']=760
    return x


def test_allocator_actually_drives_full_policy_quantity():
    d=assemble(**build_portfolio())
    q=d['portfolio_allocation']['positions'][0]['qty']
    assert q>1
    assert d['full_policy']['challenger'][0]['qty']==q
    assert d['ranking_only']['challenger'][0]['qty']==1
    assert d['ranking_only']['baseline'][0]['qty']==1
    assert d['full_policy']['challenger'][0]['allocation_id']==d['portfolio_allocation']['positions'][0]['id']


def test_allocation_cash_abstention_is_preserved():
    x=build_portfolio();x['comparison']['ranked'][0]['valuation']['conservative_mean_proxy_usd']=-20
    d=assemble(**x)
    assert d['portfolio_allocation']['positions']==[]
    assert d['full_policy']['challenger']==[]
    assert d['ranking_only']['challenger'] # selection-only remains a real comparison


def test_existing_stress_unknown_not_claimed_as_zero():
    x=build_portfolio();d=assemble(**x,existing_reserved_risk=100)
    assert d['portfolio_allocation']['status']=='EXISTING_POSITION_STRESS_NOT_EVALUATED'
    assert d['full_policy']['challenger']==[]


def test_account_assembly_combines_symbol_universe_same_clock():
    d=assemble(**build_portfolio())
    account=assemble_account([d],cash_available=1000,shared_stress_budget=1000)
    assert account['full_policy']['challenger']
    assert account['allocation']['positions'][0]['qty']==account['full_policy']['challenger'][0]['qty']
    other=deepcopy(d);other['common_decision_snapshot']['symbol']='NASDAQ:QQQ'
    # Full pool cannot silently double a candidate identifier used in two feeds.
    with pytest.raises(ValueError,match='DUPLICATE_ACCOUNT_ALLOCATION_ID'):
        assemble_account([d,other],cash_available=1000,shared_stress_budget=1000)


def test_different_policy_exit_times_with_same_future_stream():
    base=candidate('BASE');base.update(symbol='AMEX:SPY',risk_per_spread=100)
    challenger=candidate('CHALLENGER');challenger.update(symbol='AMEX:SPY',risk_per_spread=100)
    studies=choose_studies(dict(baseline=dict(candidate=base),
               ranked=[dict(candidate=challenger,valuation={'conservative_mean_proxy_usd':10})]),
               dict(positions=[]),decision_at=NOW,cash=1000)
    start=datetime.fromisoformat(NOW)
    stream=[dict(observed_at=(start+timedelta(hours=i)).isoformat(),symbol='AMEX:SPY') for i in (1,2,3)]
    def frozen(s,obs,at,available_risk):
        s=deepcopy(s)
        if s['status']=='PENDING':
            s.update(status='OPEN',entry_at=obs['observed_at'],quantity=1)
        elif s['status']=='OPEN' and (s['id'] and (at-start).total_seconds()/3600>= (2 if s.get('direction')=='BULL' else 3)):
            s.update(status='CLOSED',exit_at=obs['observed_at'],net_pnl=8.,exit_reason='PROFIT_TARGET')
        return s
    base['direction']='BULL';challenger['direction']='BEAR'
    studies=choose_studies(dict(baseline=dict(candidate=base),
               ranked=[dict(candidate=challenger,valuation={'conservative_mean_proxy_usd':10})]),
               dict(positions=[]),decision_at=NOW,cash=1000)
    report=replay(studies['ranking_only'],stream,frozen_advance=frozen,policy={'version':'us-vertical-paper-v1'})
    assert report['both_arms_comparable']
    assert report['arms']['baseline']['exit_times']!=report['arms']['challenger']['exit_times']
    assert report['broker_fills']==0
    assert report['realized_pnl']=='NOT_MEASURED'


def test_missing_future_exit_is_unresolved_not_profit():
    base=candidate('BASE');base.update(symbol='AMEX:SPY',risk_per_spread=100)
    studies=choose_studies(dict(baseline=dict(candidate=base),ranked=[]),
                           dict(positions=[]),decision_at=NOW,cash=1000)
    ts=(datetime.fromisoformat(NOW)+timedelta(minutes=1)).isoformat()
    def frozen(s,obs,now,available_risk):
        c=deepcopy(s);c.update(status='OPEN',quantity=1,entry_at=ts)
        return c
    result=replay(studies['full_policy'],[dict(symbol='AMEX:SPY',observed_at=ts)],
                  frozen_advance=frozen,policy={'version':'us-vertical-paper-v1'})
    assert result['arms']['baseline']['hypothetical_policy_net_usd'] is None
    assert not result['both_arms_comparable']


def test_complete_matured_forecast_denominator_and_lag(tmp_path):
    kw=forecast_fixture();p=tmp_path/'vintages.sqlite';v=f.make_vintage(**kw)
    f.record(p,v)
    pending=f.coverage(p,as_of='2026-11-14T15:00:00+00:00')
    assert pending['statuses']['NOT_YET_MATURE']==2
    f.match_future(p,symbol='AMEX:SPY',observed_at='2026-11-17T21:00:00+00:00',
                   exact_quotes=[dict(key=KEYS[0],iv=.21,provider='Mock')],observed_spot=780)
    summary=f.coverage(p,as_of='2026-11-21T21:00:00+00:00')
    assert summary['matured_contract_predictions']==2
    assert summary['statuses']['MATCHED_DELAYED']==1
    assert summary['statuses']['MATURED_MISSING']==1
    assert summary['missing_fraction']==.5
    assert summary['independent_matured_target_dates']==1
    assert summary['broker_profit']=='NOT_MEASURED'


def test_full_policy_price_limit_determines_entry_not_original_selection():
    from tradingview.options.architecture_fair_experiment import replay
    x=build_portfolio()
    d=assemble(**x, cash_available=100.) # max two lots at $50 risk
    assert d['full_policy']['challenger']
    root=datetime.fromisoformat(NOW)
    book={
       k:dict(symbol='AMEX:SPY',expiry='2026-12-04',strike=760.+5*i,
              option_type='CALL',basis='reference',bid=2.+i,ask=2.1+i)
       for i,k in enumerate(KEYS)
    }
    stream=[dict(symbol='AMEX:SPY',observed_at=(root+timedelta(minutes=t)).isoformat(),
                 quotes=list(book.values())) for t in (1,31)]
    def frozen(s,observation,now,available_risk):
        s=dict(s)
        if s['status']=='PENDING':s.update(status='OPEN',quantity=1,entry_at=observation['observed_at'])
        return s
    out=replay(d['full_policy'],stream,frozen_advance=frozen,
               policy=dict(version='us-vertical-paper-v1',slippage_per_share=.01,pending_minutes=30))
    assert out['arms']['challenger']['statuses']=={'CANCELLED':2}
    assert out['arms']['challenger']['model_price_limit_wait_events']==4
    assert out['arms']['challenger']['hypothetical_policy_net_usd']==0
    assert out['arms']['baseline']['hypothetical_policy_net_usd'] is None
