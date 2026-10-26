from datetime import datetime,timedelta
from copy import deepcopy
import sqlite3
import pytest
from tradingview.options.architecture_fair_experiment import choose_studies
from tradingview.options.architecture_experiment_store import save,append,evaluate
from tests.test_options_architecture_milestones import candidate

NOW='2026-10-26T15:00:00+00:00'

def study():
    frozen=candidate('CONTROL');frozen.update(symbol='AMEX:SPY',risk_per_spread=100.,direction='BULL')
    s=choose_studies({'baseline':{'candidate':frozen},'ranked':[]},{'positions':[]},decision_at=NOW,cash=1000.)
    return {'mode':'full_policy','decision_at':NOW,'cash_budget':1000.,
            'baseline':s['full_policy']['baseline'],
            'challenger':s['full_policy']['challenger'],
            'broker_orders':0,'actual_broker_profit':'NOT_MEASURED'}

def test_record_and_replay_unresolved_with_only_one_observation(tmp_path):
    path=tmp_path/'study.db';p=study()
    ident=save(path,p)
    assert ident==save(path,p)
    assert path.stat().st_mode & 0o777 == 0o600
    t=(datetime.fromisoformat(NOW)+timedelta(minutes=1)).isoformat()
    assert append(path,ident,dict(observed_at=t,symbol='AMEX:SPY'))
    assert not append(path,ident,dict(observed_at=t,symbol='AMEX:SPY'))
    def frozen(s,obs,now,available_risk):
        s=dict(s);s.update(status='OPEN',quantity=1,entry_at=t)
        return s
    report=evaluate(path,ident,frozen_advance=frozen,policy={'version':'us-vertical-paper-v1'})
    assert report['saved_reference_events']==1
    assert report['arms']['baseline']['hypothetical_policy_net_usd'] is None
    assert report['broker_fills']==0
    assert not report['both_arms_comparable']


def test_journal_rejects_backdated_event_and_changed_same_timestamp(tmp_path):
    path=tmp_path/'study.db';ident=save(path,study())
    t=(datetime.fromisoformat(NOW)+timedelta(minutes=5)).isoformat()
    assert append(path,ident,dict(observed_at=t,symbol='AMEX:SPY'))
    with pytest.raises(ValueError,match='EVENT_OVERWRITE'):
        append(path,ident,dict(observed_at=t,symbol='NASDAQ:QQQ'))
    before=(datetime.fromisoformat(NOW)+timedelta(minutes=2)).isoformat()
    with pytest.raises(ValueError,match='BACKDATE'):
        append(path,ident,dict(observed_at=before,symbol='AMEX:SPY'))


def test_no_actionable_or_reported_broker_fills_saved(tmp_path):
    x=study();x['actual_broker_profit']=100
    with pytest.raises(ValueError,match='BROKER_PROFIT_CLAIM'):
        save(tmp_path/'x.db',x)
