import sqlite3
import pytest
from tradingview.options import architecture_paired_store as s
from tradingview.options import architecture_paired_evaluation as p

NOW='2026-10-26T15:00:00+00:00'
ENTRY='2026-10-26T15:01:00+00:00'
EXIT='2026-11-02T16:00:00+00:00'
K1='AMEX:SPY|2026-12-04|760|CALL'
K2='AMEX:SPY|2026-12-04|765|CALL'

def c(ident):
    return dict(id=ident,expiry='2026-12-04',legs=[dict(key=K1,side=1),dict(key=K2,side=-1)])

def book(t):
    return {K1:dict(basis='reference',bid=2.,ask=2.1,updated_at=t),
            K2:dict(basis='reference',bid=1.,ask=1.1,updated_at=t)}


def test_immutable_reference_lifecycle_only(tmp_path):
    path=tmp_path/'vintage'/'reference.sqlite'
    cohort=p.start(cohort_id='test',decision_at=NOW,mode='ranking_only',cash=1000,
                   baseline=c('one'),challenger=c('two'))
    assert s.save_cohort(path,cohort)==s.save_cohort(path,cohort)
    assert path.stat().st_mode&0o777==0o600
    assert not s.report(path,'test')['both_arms_comparable']
    for arm in ('baseline','challenger'):
        assert s.append_reference(path,cohort_id='test',arm=arm,phase='entry',observed_at=ENTRY,quotes=book(ENTRY))['saved']
    for arm in ('baseline','challenger'):
        assert s.append_reference(path,cohort_id='test',arm=arm,phase='exit',observed_at=EXIT,quotes=book(EXIT))['saved']
    x=s.report(path,'test')
    assert x['recorded_reference_events']==4
    assert x['both_arms_comparable']
    assert x['actual_broker_pnl']=='NOT_MEASURED'
    assert x['hypothetical_difference_usd']==0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM paired_event').fetchone()[0]==4


def test_rejected_missing_quotes_do_not_become_fill(tmp_path):
    path=tmp_path/'r.db'
    s.save_cohort(path,p.start(cohort_id='c',decision_at=NOW,mode='full_policy',cash=1000,
                               baseline=c('a'),challenger=None))
    x=s.append_reference(path,cohort_id='c',arm='baseline',phase='entry',observed_at=ENTRY,quotes={})
    assert not x['saved'] and x['reason']=='MISSING_REFERENCE'
    assert s.report(path,'c')['recorded_reference_events']==0


def test_reject_backdated_attempt_after_valid_reference(tmp_path):
    path=tmp_path/'r.db'
    s.save_cohort(path,p.start(cohort_id='c',decision_at=NOW,mode='full_policy',cash=1000,
                               baseline=c('a'),challenger=c('b')))
    s.append_reference(path,cohort_id='c',arm='baseline',phase='entry',observed_at=ENTRY,quotes=book(ENTRY))
    with pytest.raises(ValueError,match='BACKDATED'):
        s.append_reference(path,cohort_id='c',arm='challenger',phase='entry',
                           observed_at='2026-10-26T15:00:30+00:00',
                           quotes=book('2026-10-26T15:00:30+00:00'))
