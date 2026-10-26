"""Pure offline bridge from existing options-edge outputs to three research layers.

Caller supplies the existing scanner observation, its previously computed proxy
forecast and the unchanged frozen-vs-edge comparison; no provider calls here.
Never sends Telegram, changes a live DB, submits orders or claims profit.
"""
from __future__ import annotations
import math

from .architecture_forecast_validation import make_vintage
from .architecture_allocation import allocate
from .architecture_paired_evaluation import start


def assemble(*, observation, forecast, completed_bars, comparison, created_at,
             existing_reserved_risk=0., cash_available=10000.,
             portfolio_stress_budget=1000., per_position_risk=250.,
             total_reserved_risk_budget=1000.):
    if comparison.get('paper_orders')!=0 or comparison.get('notifier_eligible') is not False:
        raise ValueError('ACTIONABLE_COMPARATOR_FORBIDDEN')
    if comparison.get('performance')!='NOT_MEASURED':
        raise ValueError('FALSE_OBSERVED_PERFORMANCE')
    if comparison['baseline'].get('candidate') is not None:
        assert comparison['baseline']['candidate']['symbol']==observation['symbol']
    quote_rows=[]
    for q in observation['quotes']:
        # Contract key from the FROZEN original module; import only on demand
        # so isolated research tests do not need broker/integration packages.
        from tradingview.options.paper_scanner import contract_key
        quote_rows.append(dict(key=contract_key(q),iv=q['iv'],provider=q['provider']))
    scenarios=[]
    for item in forecast['scenarios']:
        last=item['path'][-1]
        scenarios.append(dict(probability=item['probability'],spot=last['spot'],iv_by_key=last['ivs']))
    target=forecast['scenarios'][0]['path'][-1]['at']
    vintage=make_vintage(symbol=observation['symbol'],observed_at=observation['observed_at'],
                         created_at=created_at,target_at=target,completed_bars=completed_bars,
                         exact_quotes=quote_rows,path_predictions=scenarios,
                         observed_spot=observation['features']['close'],
                         future_sessions=len(forecast['scenarios'][0]['path']))
    candidates=[]
    for i,row in enumerate(comparison.get('ranked',[])):
        if row.get('reason')!='PROXY_EDGE_CLEARS_HURDLE':
            continue
        val=row['valuation'];can=row['candidate']
        if not val.get('projection_within_frozen_risk_budget'):
            continue
        # These are deliberately coarse MODELLED common-factor stress losses,
        # NOT calibrated cross-market shocks or real executable liquidation.
        contractual=val['max_contractual_loss_usd']
        liquidation=val['projected_max_liquidation_loss_usd']
        risk=max(contractual,liquidation)
        candidates.append(dict(id=str(i)+'|'+can['symbol']+'|'+can['expiry'],
                               conservative_net_usd=val['conservative_mean_proxy_usd'],
                               risk_usd=risk,collateral_usd=risk,max_qty=1,
                               stress_losses={'modelled_crash':risk,'modelled_vol_widening':liquidation}))
    allocation=allocate(candidates,available_cash=cash_available,
                        risk_budget=total_reserved_risk_budget,
                        existing_reserved_risk=existing_reserved_risk,
                        per_position_risk=per_position_risk,
                        shared_stress_budget=portfolio_stress_budget)
    baseline=comparison['baseline'].get('candidate')
    selected=(comparison.get('selection') or {}).get('candidate')
    base=start(cohort_id=vintage['id']+'|ranking',decision_at=observation['observed_at'],
               mode='ranking_only',cash=cash_available,
               baseline=baseline,challenger=selected)
    full=start(cohort_id=vintage['id']+'|policy',decision_at=observation['observed_at'],
               mode='full_policy',cash=cash_available,
               baseline=baseline,challenger=selected)
    return dict(schema='options-architecture-shadow-v1',forecast_vintage=vintage,
                portfolio_allocation=allocation,
                ranking_only=base,full_policy=full,
                broker_orders=0,telegram_messages=0,
                actual_profit='NOT_MEASURED',
                stress_method='COMMON_ASSUMED_WORST_PER_POSITION_NOT_CALIBRATED')
