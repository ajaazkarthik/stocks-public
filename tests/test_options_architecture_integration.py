from datetime import datetime,timedelta,timezone
import sys,types
import pytest
from tradingview.options.architecture_research_integration import assemble

NOW='2026-10-26T15:00:00+00:00'
TARGET='2026-11-16T21:00:00+00:00'
KEY='AMEX:SPY|2026-12-04|760|CALL'

@pytest.fixture(autouse=True)
def stub_frozen_contract_key(monkeypatch):
    # Only this contract-key serializer is supplied by the frozen scanner.
    paper=types.ModuleType('tradingview.options.paper_scanner')
    paper.contract_key=lambda q:f"{q['symbol']}|{q['expiry']}|{q['strike']:g}|{q['option_type']}"
    for name in ('tradingview','tradingview.options'):
        sys.modules.setdefault(name,types.ModuleType(name))
    monkeypatch.setitem(sys.modules,'tradingview.options.paper_scanner',paper)


def setup():
    start=datetime(2026,9,1,tzinfo=timezone.utc)
    prices=[((start+timedelta(days=i)).isoformat(),750+i*.2) for i in range(40)]
    quote={'symbol':'AMEX:SPY','expiry':'2026-12-04','strike':760.,
           'option_type':'CALL','provider':'Mock','iv':.22}
    observation=dict(symbol='AMEX:SPY',observed_at=NOW,features={'close':760.},quotes=[quote])
    forecast={'start_spot':760.,'scenarios':[{'probability':.5,'path':[{'at':TARGET,'spot':765.,'ivs':{KEY:.20}}]},
                           {'probability':.5,'path':[{'at':TARGET,'spot':790.,'ivs':{KEY:.25}}]}]}
    comparison=dict(baseline=dict(candidate=None,status='NO_TREND'),selection=None,
                    ranked=[],paper_orders=0,notifier_eligible=False,performance='NOT_MEASURED')
    return dict(observation=observation,forecast=forecast,completed_bars=prices,
                comparison=comparison,created_at='2026-10-26T15:02:00+00:00')


def test_research_integrates_three_layers_without_execution():
    data=assemble(**setup())
    assert data['forecast_vintage']['predictions'][KEY]['unchanged']['iv']==.22
    assert data['portfolio_allocation']['positions']==[]
    assert data['ranking_only']['baseline']==[]
    assert data['full_policy']['challenger']==[]
    assert data['broker_orders']==data['telegram_messages']==0
    assert data['actual_profit']=='NOT_MEASURED'


def test_rejects_unsafe_comparator():
    x=setup();x['comparison']['paper_orders']=1
    with pytest.raises(ValueError,match='ACTIONABLE'):
        assemble(**x)


def test_rejects_future_price_leakage():
    x=setup();x['completed_bars'].append((TARGET,1000))
    with pytest.raises(ValueError,match='UNFINISHED'):
        assemble(**x)
