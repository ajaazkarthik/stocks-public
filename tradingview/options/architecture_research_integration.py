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
             total_reserved_risk_budget=1000., existing_stress_losses=None):
    """Construct actual allocator-led FULL policy and ranking-only control.

    One symbol only: this is NEW-POSITIONS-IN-EXTERNAL-REMAINING-BUDGETS,
    not complete account optimisation across SPY/QQQ/IWM. No future observations
    used in selecting a portfolio. No portfolio return is asserted.
    """
    from .architecture_fair_experiment import decision_snapshot,choose_studies
    if comparison.get('paper_orders')!=0 or comparison.get('notifier_eligible') is not False:
        raise ValueError('ACTIONABLE_COMPARATOR_FORBIDDEN')
    if comparison.get('performance')!='NOT_MEASURED':
        raise ValueError('FALSE_OBSERVED_PERFORMANCE')
    snap=decision_snapshot(observation, forecast)
    # This is the SAME underlying quote used by the path forecast and by BOTH
    # benchmark arms. Prior close remains available only as a historical input.
    quote_rows=[dict(key=k,iv=v['iv'],provider='fractional_normalized') for k,v in snap['exact_contracts'].items()]
    scenarios=[]
    for item in forecast['scenarios']:
        last=item['path'][-1]
        scenarios.append(dict(probability=item['probability'],spot=last['spot'],iv_by_key=last['ivs']))
    target=forecast['scenarios'][0]['path'][-1]['at']
    vintage=make_vintage(symbol=observation['symbol'],observed_at=observation['observed_at'],
                         created_at=created_at,target_at=target,completed_bars=completed_bars,
                         exact_quotes=quote_rows,path_predictions=scenarios,
                         observed_spot=snap['spot'],
                         future_sessions=len(forecast['scenarios'][0]['path']))
    candidates=[];candidate_map={}
    for i,row in enumerate(comparison.get('ranked',[])):
        if row.get('reason')!='PROXY_EDGE_CLEARS_HURDLE':continue
        val=row['valuation'];can=row['candidate']
        if not val.get('projection_within_frozen_risk_budget'):continue
        limit=row.get('price_limit') or {}
        if limit.get('status') not in ('MODEL_ONLY_PRICE_CEILING','MODEL_ONLY_MINIMUM_CREDIT'):
            continue
        if limit.get('limit_quote') is None:continue
        contractual=float(val['max_contractual_loss_usd'])
        liquidation=float(val['projected_max_liquidation_loss_usd'])
        risk=max(contractual,liquidation)
        ident=str(i)+'|'+can['symbol']+'|'+can['expiry']
        candidates.append(dict(id=ident,conservative_net_usd=val['conservative_mean_proxy_usd'],
                               risk_usd=risk,collateral_usd=risk,
                               max_qty=max(1,int(per_position_risk//risk)),
                               stress_losses={'modelled_crash':risk,'modelled_vol_widening':liquidation}))
        candidate_map[ident]={'candidate':can,'limit':limit,'limit_verified_model_only':True}
    allocation=allocate(candidates,available_cash=cash_available,
                        risk_budget=total_reserved_risk_budget,
                        existing_reserved_risk=existing_reserved_risk,
                        per_position_risk=per_position_risk,
                        shared_stress_budget=portfolio_stress_budget,
                        existing_stress_losses=existing_stress_losses)
    studies=choose_studies(comparison,allocation,
                           decision_at=observation['observed_at'],cash=cash_available,
                           candidate_map=candidate_map)
    return dict(schema='options-architecture-fair-shadow-v2',forecast_vintage=vintage,
                common_decision_snapshot=snap,
                portfolio_allocation=allocation,allocation_candidates=candidates,
                allocation_candidate_map=candidate_map,ranking_only=studies['ranking_only'],
                full_policy=studies['full_policy'],
                broker_orders=0,telegram_messages=0,actual_profit='NOT_MEASURED',
                risk_scope='NEW_POSITIONS_WITH_EXTERNAL_ACCOUNT_BUDGETS',
                frozen_replay_function='architecture_fair_experiment.replay',
                full_policy_positions_are_allocation_outputs=True)


def assemble_account(packages, *,cash_available,shared_stress_budget,
                     existing_reserved_risk=0.,existing_stress_losses=None):
    """One account decision over SPY/QQQ/IWM opportunities at ONE clock.

    Packages are as-of-complete per-symbol assemble results. Ignore individual
    per-symbol allocations and optimize once over the COMBINED new-position
    universe, with explicit externally provided incumbent-account constraints.
    """
    from .architecture_fair_experiment import choose_studies
    if not packages:raise ValueError('EMPTY_ACCOUNT_UNIVERSE')
    clocks={p['common_decision_snapshot']['decision_at'] for p in packages}
    symbols=[p['common_decision_snapshot']['symbol'] for p in packages]
    if len(clocks)!=1 or len(set(symbols))!=len(symbols):
        raise ValueError('ACCOUNT_DECISION_CLOCK_OR_DUPLICATE_SYMBOL')
    all_candidates=[];maps={};frozen=[];ranked=[]
    for p in packages:
        if p.get('broker_orders')!=0 or p.get('actual_profit')!='NOT_MEASURED':
            raise ValueError('ACTIONABLE_PACKAGE_FORBIDDEN')
        all_candidates+=p['allocation_candidates']
        for key,val in p['allocation_candidate_map'].items():
            if key in maps:raise ValueError('DUPLICATE_ACCOUNT_ALLOCATION_ID')
            maps[key]=val
        frozen+=p['full_policy']['baseline']
        ranked+=p['ranking_only']['challenger']
    allocation=allocate(all_candidates,available_cash=cash_available,
                        risk_budget=1000.,per_position_risk=250.,
                        existing_reserved_risk=existing_reserved_risk,
                        existing_stress_losses=existing_stress_losses,
                        shared_stress_budget=shared_stress_budget)
    by_id={k:v for k,v in maps.items()}
    full=[]
    for row in allocation.get('positions',[]):
        if row['id'] not in by_id:raise ValueError('ACCOUNT_ALLOCATION_UNRECOGNIZED')
        full.append({'candidate':by_id[row['id']]['candidate'],'qty':row['qty'],
                     'allocation_id':row['id'],
                     'model_only_limit':by_id[row['id']]['limit']})
    return dict(schema='account-wide-shadow-v1',decision_at=next(iter(clocks)),
                symbols=sorted(symbols),allocation=allocation,
                ranking_only=dict(mode='ranking_only',decision_at=next(iter(clocks)),
                                  cash_budget=cash_available,baseline=frozen,
                                  challenger=ranked),
                full_policy=dict(mode='full_policy',decision_at=next(iter(clocks)),
                                 cash_budget=cash_available,baseline=frozen,
                                 challenger=full),
                account_risk_scope='NEW_POSITIONS_WITH_EXTERNAL_INCUMBENT_STRESS',
                broker_orders=0,actual_broker_profit='NOT_MEASURED')


def run_account_comparison(account,future_observations,*,frozen_advance=None,policy=None):
    """Evaluate two distinct policies against one fixed future observation stream.

    THIS IS REFERENCE-ONLY REPLAY, NOT executed broker P/L. Both studies can
    show conditional differences only when underlying original frozen rules
    closed/cancelled every relevant position with available future evidence.
    """
    from .architecture_fair_experiment import replay
    if account.get('broker_orders')!=0 or account.get('actual_broker_profit')!='NOT_MEASURED':
        raise ValueError('UNSAFE_EXPERIMENT')
    events=list(future_observations)
    ranking=replay(account['ranking_only'],events,frozen_advance=frozen_advance,policy=policy)
    full=replay(account['full_policy'],events,frozen_advance=frozen_advance,policy=policy)
    return dict(schema='reference-only-fair-comparison-v1',ranking_only=ranking,
                full_policy=full,all_arms_same_evidence_stream=True,
                selection_only_changes_rank=account['ranking_only']['mode']=='ranking_only',
                full_policy_uses_allocator=account['full_policy']['mode']=='full_policy',
                confidence='NONE_UNTIL_PROSPECTIVE_MATCHED_TRADES',
                broker_fills=0,actual_after_cost_profits='NOT_MEASURED')
