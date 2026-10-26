"""Research-only, causal, portfolio-led *frozen policy* experiment.

This intentionally delegates ALL entry/exit mechanics to original paper_scanner.advance.
No market access, broker fills, orders, Telegram or live database writes.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from collections import Counter, defaultdict
import hashlib
import json

from .architecture_forecast_validation import aware, number


def decision_snapshot(observation, forecast):
    """Select one snapshot; reject asynchronous or contradictory spot sources."""
    decision=aware(observation['observed_at'])
    spots=[number(q['spot'], positive=True) for q in observation['quotes'] if q.get('spot') is not None]
    if spots and max(spots)-min(spots)>max(.01, min(spots)*.0001):
        raise ValueError('CONFLICTING_DECISION_SPOT_QUOTES')
    if spots:
        spot=spots[0]; origin='OPTION_QUOTE_UNDERLYING_SPOT'
    else:
        # Only historical completed price is available; don't pretend live spot.
        spot=number(observation['features']['close'], positive=True)
        origin='COMPLETED_CLOSE_FALLBACK_UNVERIFIED'
    if 'start_spot' not in forecast:
        raise ValueError('FORECAST_MISSING_START_SPOT_PROVENANCE')
    if abs(number(forecast['start_spot'], positive=True)-spot)>max(.01,spot*.0001):
        raise ValueError('UNEQUAL_FORECAST_START_SPOT')
    by_key={}
    from tradingview.options.paper_scanner import contract_key
    from .architecture_forecast_validation import normalize_iv
    for q in observation['quotes']:
        key=contract_key(q)
        if key in by_key:raise ValueError('DUPLICATE_EXACT_OPTION')
        by_key[key]=dict(iv=normalize_iv(q),provider=q.get('provider'),
                         observed_at=q.get('updated_at'),basis=q.get('basis'),
                         spot=spot)
    return {'decision_at':decision.isoformat(),'symbol':observation['symbol'],
            'spot':spot,'spot_origin':origin,'exact_contracts':by_key,
            'reference_only':True,'exchange_side_verified':False}


def choose_studies(comparison, allocation, *,decision_at, cash, candidate_map=None):
    """Return immutable experiment descriptions; allocator drives FULL policy.

    Ranking-only has frozen participation, one contract and frozen exits; it uses
    a model ranking over *all* frozen-eligible ranked candidates, not the edge
    hurdle. No basis for ranking => unresolved rather than fabricated cash.
    """
    base=comparison['baseline'].get('candidate')
    candidate_map=candidate_map or {}
    ranked=[r for r in comparison.get('ranked',[]) if r.get('candidate') and r.get('valuation')]
    ranked=sorted(ranked,key=lambda r:(-float(r['valuation']['conservative_mean_proxy_usd']),
                                     json.dumps(r['candidate']['legs'],sort_keys=True)))
    ranking=ranked[0]['candidate'] if ranked else None
    frozen=[{'candidate':deepcopy(base),'qty':1}] if base else []
    rank=[{'candidate':deepcopy(ranking),'qty':1}] if ranking and base else []
    if base and not ranking:
        ranking_status='UNRESOLVED_NO_RANKING_EVIDENCE'
    else:ranking_status='FROZEN_PARTICIPATION_SAME_QUANTITY'
    full=[]
    for row in allocation.get('positions',[]):
        if row['id'] not in candidate_map:raise ValueError('ALLOCATED_ID_NOT_IN_CANDIDATE_SET')
        if type(row['qty']) is not int or row['qty']<1:raise ValueError('INVALID_ALLOCATOR_QUANTITY')
        proposal=candidate_map[row['id']]
        if not proposal.get('limit_verified_model_only'):
            raise ValueError('ALLOCATED_WITHOUT_PRICE_LIMIT_EVIDENCE')
        full.append({'candidate':deepcopy(proposal['candidate']),'qty':row['qty'],
                     'model_only_limit':proposal['limit'],'allocation_id':row['id']})
    return {
      'ranking_only':dict(mode='ranking_only',decision_at=decision_at,cash_budget=cash,
                          baseline=frozen,challenger=rank,ranking_status=ranking_status),
      'full_policy':dict(mode='full_policy',decision_at=decision_at,cash_budget=cash,
                         baseline=frozen,challenger=full,
                         allocation_status=allocation.get('status','UNASSESSED'),
                         abstained_cash=not bool(full)),
      'broker_orders':0,'realized_pnl':'NOT_MEASURED'
    }


def _entry_limit_satisfied(position, observation, policy):
    """MODEL_ONLY price condition before original frozen paper entry check.

    Reference quotes never prove an executable fill. If a quote is missing,
    allow frozen advance to log its normal missing-evidence state.
    """
    bound=position.get('model_only_limit')
    if not bound:return True
    from tradingview.options.paper_scanner import contract_key
    indexed={contract_key(q):q for q in observation.get('quotes',[])}
    cash=0.0
    for leg in position['candidate']['legs']:
        q=indexed.get(leg['key'])
        if not q or 'bid' not in q or 'ask' not in q:return None
        bid=float(q['bid']);ask=float(q['ask'])
        if bid<0 or ask<=0 or bid>ask:return None
        side=leg['side']
        price=ask+policy['slippage_per_share'] if side==1 else max(0.,bid-policy['slippage_per_share'])
        cash-=side*price
    limit=number(bound['limit_quote'],positive=True)
    kind=position['candidate']['kind']
    if kind=='DEBIT':return cash<0 and -cash<=limit+1e-9
    if kind=='CREDIT':return cash>0 and cash>=limit-1e-9
    raise ValueError('UNKNOWN_MODEL_SPREAD_KIND')


def replay(study, future_observations, *,frozen_advance=None, policy=None):
    """Run SAME chronological observations and SAME frozen advance for each arm.

    Quantity q produces q separate identical frozen policy paper states, so
    original trade_risk, entry tests and exit logic remain unchanged.
    """
    if frozen_advance is None:
        from tradingview.options.paper_scanner import advance as frozen_advance
    if policy is None:
        from tradingview.options.paper_scanner import POLICY as policy
    decision=aware(study['decision_at'])
    # All inputs must be chronological; never sort out a future-data error.
    stream=list(future_observations)
    times=[aware(x['observed_at']) for x in stream]
    if any(t<=decision for t in times) or any(a>=b for a,b in zip(times,times[1:])):
        raise ValueError('NONCAUSAL_OR_UNORDERED_FUTURE_STREAM')
    result={'mode':study['mode'],'decision_at':decision.isoformat(),
            'cash_budget':study['cash_budget'],'broker_fills':0,
            'realized_pnl':'NOT_MEASURED','arms':{},'same_stream_for_both':True,
            'exit_policy':'UNMODIFIED_US_VERTICAL_PAPER_V1'}
    for arm in ('baseline','challenger'):
        units=[]
        for index,position in enumerate(study[arm]):
            qty=position['qty']
            if type(qty) is not int or qty<1:raise ValueError('INVALID_PORTFOLIO_QUANTITY')
            for lot in range(qty):
                c=deepcopy(position['candidate'])
                if c.get('symbol') is None or len(c.get('legs',[]))!=2:
                    raise ValueError('INVALID_FROZEN_CANDIDATE')
                ident=hashlib.sha256((study['mode']+'|'+arm+'|'+str(index)+'|'+str(lot)).encode()).hexdigest()
                c.update(id=ident,status='PENDING',decision_at=decision.isoformat(),
                         session_date=decision.date().isoformat(),version=policy['version'],
                         policy=deepcopy(policy),mark_status='WAITING_FOR_POST_DECISION_OBSERVATION')
                c['_model_only_limit']=position.get('model_only_limit')
                c['_model_limit_wait_count']=0
                units.append(c)
        observed_events=0
        for event,now in zip(stream,times):
            for i,c in enumerate(units):
                if c['symbol']!=event['symbol']:continue
                # Available risk may only buy 1 lot in this *one-lot* state;
                # the account decision already imposed all external budgets.
                one_lot_risk=float(c['risk_per_spread'])+1e-5
                if not one_lot_risk>0:raise ValueError('UNKNOWN_POSITION_RISK')
                # Full-policy entries follow the allocator's selected price
                # limits. Ranking-only deliberately keeps frozen participation.
                if study['mode']=='full_policy' and arm=='challenger' and c['status']=='PENDING':
                    cond=_entry_limit_satisfied({'candidate':c,'model_only_limit':c.get('_model_only_limit')},event,policy)
                    if cond is False:
                        c['_model_limit_wait_count']+=1
                        from datetime import timedelta
                        if now>decision+timedelta(minutes=policy['pending_minutes']):
                            c.update(status='CANCELLED',reason='MODEL_PRICE_LIMIT_NOT_MET')
                        continue
                updated=frozen_advance(c,event,now,available_risk=one_lot_risk)
                if updated.get('quantity',1)!=1 and updated['status'] in ('OPEN','CLOSED'):
                    raise ValueError('UNEXPECTED_FROZEN_QUANTITY')
                units[i]=updated
                observed_events+=1
        # OPEN positions without mandatory exit evidence remain unresolved.
        status=[]
        for s in units:
            x=s['status']
            if x in ('OPEN','PENDING'):
                x='UNRESOLVED_FUTURE_EVIDENCE'
            status.append(x)
        closed=[x for x in units if x['status']=='CLOSED' and x.get('net_pnl') is not None]
        cash=not units
        complete=(cash or all(x['status'] in ('CLOSED','CANCELLED') for x in units))
        # Original advance includes frozen fees/slippage, but source quotes are
        # reference-only. Do NOT call their net_pnl broker-realized profit.
        result['arms'][arm]={'positions':len(study[arm]),'contracts':len(units),
                             'statuses':dict(Counter(status)),
                             'recorded_quote_events':observed_events,
                             'model_price_limit_wait_events':sum(x.get('_model_limit_wait_count',0) for x in units),
                             'policy_exit_reasons':dict(Counter(x.get('exit_reason') for x in closed)),
                             'hypothetical_policy_net_usd':round(sum(x['net_pnl'] for x in closed),4) if complete else None,
                             'cash_abstention':cash,'complete_reference_history':complete,
                             'entry_times':[x.get('entry_at') for x in units],
                             'exit_times':[x.get('exit_at') for x in units]}
    a,b=result['arms']['baseline'],result['arms']['challenger']
    result['both_arms_comparable']=(a['complete_reference_history'] and b['complete_reference_history'])
    result['hypothetical_net_difference_usd']=(round(b['hypothetical_policy_net_usd']-
              a['hypothetical_policy_net_usd'],4) if result['both_arms_comparable'] else None)
    result['cash_benchmark_usd']=0.
    result['trade_frequency']={arm:result['arms'][arm]['contracts'] for arm in ('baseline','challenger')}
    result['capital_exposure_note']='Account decisions enforced externally; reference price crossings are NOT actual fills.'
    return result
