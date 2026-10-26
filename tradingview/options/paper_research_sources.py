"""Bounded READ-ONLY source parity and scorer diagnostics for frozen US paper options.

No live scheduler import, order, trade, DB, alert, or quote-basis promotion.
Vendor snapshots / Cboe OI / TV smile and Greeks are NON-EXECUTABLE unless
separately proven by independent per-side market timestamps.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import math
from types import SimpleNamespace

from tradingview.options.paper_scanner import NY, POLICY, contract_key, validate_quote, stamp, choose_candidate
from tradingview.options.tqs_v2.options_data.cboe_oi import CboeOIEnricher
from tradingview.options.tqs_v2.options_data.iv_structure import TVVolatilityChart, TVVoiLens
from tradingview.options.tqs_v2.config import DEFAULT_CONFIG as TQS_CONFIG
from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer
from tradingview.options.tqs_v2.scoring.metrics.liquidity import LiquidityScorer
from tradingview.scheduler.us_options_policy import _aware_timestamp, USOptionsPolicy
from tradingview.scheduler.market_calendar import CalendarCoverageError


def positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if 0 < v < float('inf') else None
    except (ValueError, TypeError, OverflowError):
        return None


def nonnegative_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return int(v) if math.isfinite(v) and v >= 0 and v.is_integer() else None
    except (ValueError, TypeError, OverflowError):
        return None


def exact_identity(q, symbol):
    """No adjusted roots, nearest strike/expiry, nonstandard deliverables."""
    if not isinstance(symbol, str) or symbol.count(':') != 1:
        raise ValueError('exchange-qualified symbol required')
    root = symbol.split(':')[1]
    if q.get('symbol') != symbol or not root.isascii() or not root.isalpha():
        raise ValueError('underlying symbol mismatch')
    if q.get('currency') != 'USD' or q.get('multiplier') != 100 or q.get('deliverable') != 'REGULAR':
        raise ValueError('unverified standard contract')
    if not str(q.get('contract_spec_source') or '').startswith('Yahoo exact expiry/type/strike'):
        raise ValueError('no independent contract size evidence')
    key = contract_key(q)
    if not (positive(q.get('strike')) and q.get('option_type') in ('CALL','PUT')):
        raise ValueError('invalid contract')
    # Reject adjusted TV root when supplied; Yahoo symbol metadata alone cannot
    # independently certify a different Cboe trading class/adjusted deliverable.
    if q.get('trading_class') not in (None, root) or q.get('root') not in (None, root):
        raise ValueError('nonstandard trading class')
    return key, root


def strict_execution_gate(q, now, *, policy=POLICY):
    """Use original frozen entry validation PLUS existing US strict side-age rule.

    Unlike CALL-only USOptionsPolicy.validate_scanner_call, this may inspect
    either leg of a spread. A generic update_time/receipt never proves sides.
    """
    if q.get('basis') != 'verified' or q.get('side_quote_timestamps_verified') is not True:
        return {'status':'BLOCKED_UNVERIFIED_SIDES','executable':False}
    if not q.get('bid_quote_source') or not q.get('ask_quote_source'):
        return {'status':'BLOCKED_SIDE_SOURCE','executable':False}
    try:
        limit = USOptionsPolicy(quote_mode='strict').max_quote_age_seconds
        _aware_timestamp(q.get('bid_at'), 'bid_at', now, limit)
        _aware_timestamp(q.get('ask_at'), 'ask_at', now, limit)
        validate_quote(q, now, entry=True, policy=policy)
    except (ValueError, KeyError, TypeError, CalendarCoverageError) as exc:
        return {'status':'BLOCKED_STRICT_VALIDATION','executable':False,'reason':type(exc).__name__}
    return {'status':'TIMESTAMP_GATES_PASS_EXECUTION_NOT_PROVEN','executable':False,
            'note':'Sides could pass freshness controls; no actual fill or synchronized multi-leg execution evidence'}


def cboe_contract_comparison(observation, *, fetcher=None, max_quotes=24):
    """Cboe OCC-coded OI comparison; NO value substituted into baseline.

    The Cboe source metadata does not independently certify adjusted-contract
    deliverables or the OI as-of session; this is an exact *candidate* match.
    """
    symbol = observation['symbol']
    if not symbol.count(':') == 1: raise ValueError('invalid underlying')
    root = symbol.split(':')[1]
    if not root.isalpha(): raise ValueError('invalid root')
    quotes = list(observation.get('quotes') or [])[:max_quotes]
    api = fetcher if fetcher is not None else CboeOIEnricher()._fetch_symbol
    try:
        source = api(root,5)
        if not isinstance(source, dict): source = {}
        fetch_status = 'REFERENCE_ONLY' if source else 'UNAVAILABLE'
    except Exception:
        source={};fetch_status='UNAVAILABLE'
    matches=[]
    for q in quotes:
        try:
            k, qr = exact_identity(q,symbol)
            d=date.fromisoformat(q['expiry'])
            lookup=(qr,int(d.strftime('%Y%m%d')),q['option_type'].lower(),float(q['strike']))
            result=source.get(lookup)
            oi=nonnegative_int(result[0]) if isinstance(result,(list,tuple)) and len(result)>0 else None
            vol=nonnegative_int(result[1]) if isinstance(result,(list,tuple)) and len(result)>1 else None
            yahoo_oi=nonnegative_int(q.get('oi'))
            matches.append({'contract':k,'status':'REFERENCE_ONLY' if oi is not None else 'MISSING_CBOE_OI',
                            'cboe_open_interest':oi,'cboe_volume':vol,'yahoo_open_interest':yahoo_oi,
                            'oi_difference':oi-yahoo_oi if oi is not None and yahoo_oi is not None else None,
                            'source_asof':None,'source_session':None,
                            'provenance':'Cboe CDN OCC root/expiry/type/strike; deliverable inferred only from independent Yahoo contract metadata'})
        except (KeyError,ValueError,TypeError,OverflowError):
            matches.append({'contract':'REJECTED_IDENTITY','status':'REJECTED_IDENTITY','source_asof':None})
    return {'source':'TQS v2 CboeOIEnricher._fetch_symbol','fetch_status':fetch_status,
            'quote_basis':'reference','provider_asof':None,'rows':matches,
            'matched':sum(r['status']=='REFERENCE_ONLY' for r in matches),
            'not_authorized_as_liquidity_fallback':True}


def expiry_smile_comparison(observation, *, config=TQS_CONFIG, chart=None, lens=None,
                            max_expiries=3):
    """Call ORIGINAL TQS per-expiry IV smile / strike-volume parsers."""
    symbol=observation['symbol']
    if symbol.count(':')!=1:raise ValueError('exact symbol required')
    exchange,root=symbol.split(':')
    dates=sorted(set(q['expiry'] for q in observation.get('quotes',[]) if q.get('symbol')==symbol))[:max_expiries]
    chart=chart if chart is not None else TVVolatilityChart()
    lens=lens if lens is not None else TVVoiLens()
    try: per_expiry=lens.fetch(root,exchange,config)
    except Exception: per_expiry={}
    spots=[positive(q.get('spot')) for q in observation.get('quotes',[])]
    spots=[x for x in spots if x is not None]
    spot=spots[0] if spots else None
    out=[]
    for expiry in dates:
        try:
            e=int(date.fromisoformat(expiry).strftime('%Y%m%d'))
            raw=chart.fetch_smile(root,exchange,e,config,spot_price=spot or 0.0) if spot is not None else {}
            strikes=raw.get('strikes',[]) if isinstance(raw,dict) else []
            ivs=raw.get('ivs',[]) if isinstance(raw,dict) else []
            valid=(spot is not None and len(strikes)>=3 and len(strikes)==len(ivs) and
                   all(positive(k) is not None and positive(v) is not None and positive(v)<10 for k,v in zip(strikes,ivs)) and
                   min(strikes)<=.9*spot and max(strikes)>=1.1*spot)
            skew=float(raw.get('skew')) if valid else None
            if skew is not None and not math.isfinite(skew):skew=None
            wing=per_expiry.get(e,{}) if isinstance(per_expiry,dict) else {}
            exact={str(float(q['strike'])):wing.get(float(q['strike'])) for q in observation.get('quotes',[])
                   if q.get('expiry')==expiry and q.get('symbol')==symbol and float(q['strike']) in wing}
            out.append({'expiry':expiry,'status':'REFERENCE_ONLY' if skew is not None else 'UNAVAILABLE_OR_UNCOVERED_WINGS',
                        'put_minus_call_10pct_wing_iv_points':round(skew*100,5) if skew is not None else None,
                        'spot_for_skew':spot,'strike_volume':exact,
                        'volume_status':'REFERENCE_ONLY' if exact else 'UNAVAILABLE',
                        'provider_asof':None,'quote_basis':'reference','signed_flow':'UNKNOWN'})
        except (ValueError,KeyError,TypeError):
            out.append({'expiry':expiry,'status':'UNAVAILABLE','provider_asof':None})
    return {'source':'TQS v2 TVVolatilityChart + TVVoiLens','expiries':out,'quote_basis':'reference',
            'provider_asof':None,'not_a_volatility_forecast':True}


def tqs_score_comparison(observation, now, *, max_quotes=24):
    """Existing TQS formula only, no inferred missing data or strategy changes."""
    liq,prem=LiquidityScorer(),PremiumEfficiencyScorer()
    rows=[]
    for q in list(observation.get('quotes') or [])[:max_quotes]:
        try:
            key,root=exact_identity(q,observation['symbol'])
            bid=positive(q.get('bid'));ask=positive(q.get('ask'))
            spot=positive(q.get('spot'));iv=positive(q.get('iv'))
            delta=q.get('delta');oi=nonnegative_int(q.get('oi'))
            volume=nonnegative_int(q.get('volume'))
            delta=float(delta) if delta is not None and not isinstance(delta,bool) else None
            if not all(x is not None for x in (bid,ask,spot,iv,delta,oi,volume)) or ask<bid or not math.isfinite(delta) or abs(delta)>1:
                rows.append({'contract':key,'status':'INSUFFICIENT_INPUTS'});continue
            dte=(date.fromisoformat(q['expiry'])-now.astimezone(NY).date()).days
            if dte<=0:rows.append({'contract':key,'status':'OUT_OF_DATE'});continue
            c=SimpleNamespace(bid=bid,ask=ask,spot_price=spot,strike=float(q['strike']),
                option_type=q['option_type'].lower(),iv=iv/100 if iv>1 else iv,
                dte=dte,delta=delta,open_interest=oi,volume=volume,ws_volume=None,
                spread_pct=(ask-bid)/((ask+bid)/2)*100)
            rows.append({'contract':key,'status':'REFERENCE_DIAGNOSTIC_ONLY',
                'tqs_liquidity_score':round(liq.score(c),4),
                'tqs_premium_efficiency_score':round(prem.score(c),4),
                'pricing_basis':q.get('basis'),'is_calibrated_probability':False,
                'eligible_as_strategy_gate':False})
        except (ValueError,KeyError,TypeError,OverflowError):
            rows.append({'status':'UNAVAILABLE_OR_INVALID_IDENTITY'})
    return {'source':'TQS v2 LiquidityScorer and PremiumEfficiencyScorer','rows':rows,
            'entry_strategy_unchanged':True,'performance':'NOT_MEASURED'}


def moomoo_comparison(observation, *, fetcher=None, max_contracts=4):
    """Bounded exact contract and Greeks from Moomoo OpenD, READ-ONLY.

    Reuses the existing USOptionsFetcher connector and SDK but does not call its
    wide near-ATM batch routine: resolve *exact* codes from the option chain,
    then fetch only the requested <=4 contracts through get_market_snapshot.
    Never equate provider update_time with independently stamped bid/ask sides.
    """
    from tradingview.options.us_options import USOptionsFetcher
    symbol=observation['symbol'];root=symbol.split(':')[-1]
    client=fetcher if fetcher is not None else USOptionsFetcher(verbose=False)
    rows=[]
    try:
        if not client._connect():
            return {'source':'Moomoo OpenD read-only','rows':[],
                    'status':'UNAVAILABLE','eligible_for_live_fills':False}
        import pandas as pd
        from moomoo import RET_OK
        for q in list(observation.get('quotes') or [])[:max_contracts]:
            try:
                key,_=exact_identity(q,symbol)
                expiry=q['expiry']
                rc,chain=client._ctx.get_option_chain(code='US.'+root,start=expiry,end=expiry)
                if rc!=RET_OK or not isinstance(chain,pd.DataFrame) or chain.empty:
                    rows.append({'contract':key,'status':'CHAIN_UNAVAILABLE'});continue
                strike_col=next((c for c in ('option_strike_price','strike_price','strike') if c in chain),None)
                if not strike_col or 'option_type' not in chain or 'code' not in chain:
                    rows.append({'contract':key,'status':'CHAIN_IDENTITY_UNAVAILABLE'});continue
                eligible=chain[(chain['option_type'].astype(str).str.upper()==q['option_type']) &
                               (pd.to_numeric(chain[strike_col],errors='coerce')==float(q['strike']))]
                # Avoid ambiguity from adjusted contracts / multiple series at one strike.
                if len(eligible)!=1:
                    rows.append({'contract':key,'status':'NO_UNAMBIGUOUS_EXACT_MATCH'});continue
                code=str(eligible.iloc[0]['code'])
                expected_code=('US.'+root+date.fromisoformat(expiry).strftime('%y%m%d')+
                               ('C' if q['option_type']=='CALL' else 'P')+
                               str(int(round(float(q['strike'])*1000))))
                # An adjusted OCC root/settlement may share the same displayed strike.
                # Both owner and trading class are mandatory for exact identity.
                if code!=expected_code or str(eligible.iloc[0].get('stock_owner',''))!='US.'+root or \
                   nonnegative_int(eligible.iloc[0].get('lot_size'))!=100:
                    rows.append({'contract':key,'status':'ADJUSTED_OR_UNVERIFIED_CLASS'});continue
                # US SDK chain expiry may be the settlement rather than exact expiry;
                # if explicit strike_time metadata exists, NEVER tolerate a mismatch.
                if 'strike_time' in eligible and str(eligible.iloc[0]['strike_time'])[:10]!=expiry:
                    rows.append({'contract':key,'status':'EXPIRY_MISMATCH'});continue
                rs,snapshot=client._ctx.get_market_snapshot([code])
                if rs!=RET_OK or not isinstance(snapshot,pd.DataFrame) or len(snapshot)!=1:
                    rows.append({'contract':key,'status':'SNAPSHOT_UNAVAILABLE'});continue
                m=snapshot.iloc[0]
                if (str(m.get('code',''))!=code or str(m.get('option_type','')).upper()!=q['option_type']
                    or str(m.get('strike_time',''))[:10]!=expiry or
                    positive(m.get('option_strike_price'))!=float(q['strike'])):
                    rows.append({'contract':key,'status':'SNAPSHOT_IDENTITY_MISMATCH'});continue
                delta=m.get('option_delta')
                try:
                    delta=float(delta)
                    if not math.isfinite(delta) or abs(delta)>1:delta=None
                except (TypeError,ValueError,OverflowError):delta=None
                update=str(m.get('update_time') or '')
                try:
                    update=stamp(update).isoformat() if update else None
                except (ValueError,TypeError):update=None
                rows.append({'contract':key,'status':'REFERENCE_ONLY',
                             'moomoo_bid':positive(m.get('bid_price')),
                             'moomoo_ask':positive(m.get('ask_price')),
                             'moomoo_iv_pct':positive(m.get('option_implied_volatility')),
                             'moomoo_delta':delta,
                             'moomoo_oi':nonnegative_int(m.get('option_open_interest')),
                             'bid_at':None,'ask_at':None,'provider_update_time':update,
                             'code_matched':True,'source':'Moomoo OpenD get_market_snapshot',
                             'quote_basis':'reference','not_executable':True})
            except (ValueError,KeyError,TypeError,OverflowError):
                rows.append({'status':'ERROR_OR_UNSUPPORTED_CONTRACT'})
    finally:
        if fetcher is None and hasattr(client,'_close'):
            client._close()
    return {'source':'Moomoo OpenD exact-code market snapshots','rows':rows,
            'bid_ask_side_timestamp_verified':False,'eligible_for_live_fills':False}


def sample_research_quotes(observation, now, limit):
    """Bounded ATM-stratified CALL/PUT samples, never modify original cohort.

    Avoid source-order first-N bias towards deep OTM strikes. Deterministic,
    price-independent ranking; not a signal, strategy or profitability filter.
    """
    if not 1 <= limit <= 24:raise ValueError('research sample limit outside 1..24')
    def rank(q):
        try:
            strike=positive(q.get('strike'));spot=positive(q.get('spot'))
            dte=(date.fromisoformat(q['expiry'])-now.astimezone(NY).date()).days
            return (abs(strike-spot)/spot if strike and spot else float('inf'),
                    abs(dte-45),q.get('expiry',''),q.get('option_type',''),strike or 0)
        except (KeyError,TypeError,ValueError):
            return (float('inf'),float('inf'),'','','')
    ordered=sorted(observation.get('quotes') or [],key=rank)
    first=[]
    if limit >= 2:
        for side in ('CALL','PUT'):
            if next((q for q in ordered if q.get('option_type')==side),None) is not None:
                first.append(next(q for q in ordered if q.get('option_type')==side))
    taken={id(q) for q in first}
    return (first+[q for q in ordered if id(q) not in taken])[:limit]


def compare_all_sources(observation, now, *, enable_cboe=False, enable_smile=False,
                        enable_moomoo=False, cboe_fetcher=None, chart=None, lens=None,
                        moomoo_fetcher=None, max_quotes=12):
    """Explicit opt-in feature flag collection; zero changes to primary quotes."""
    quotes=sample_research_quotes(observation,now,max_quotes)
    subset=dict(observation,quotes=quotes)
    strict={}
    for q in quotes:
        try:strict[contract_key(q)]=strict_execution_gate(q,now)
        except (ValueError,KeyError,TypeError):strict['REJECTED_IDENTITY_'+str(len(strict))]={'status':'BLOCKED_IDENTITY','executable':False}
    result={'status':'RESEARCH_ONLY','source_quote_basis':'reference',
            'source_side_timestamps_verified':False,'strategy_frozen':True,
            'strict_evidence':strict,
            'tqs_scores':tqs_score_comparison(subset,now),
            'performance':'NOT_MEASURED', 'paper_orders':0}
    # Compare baseline decision to each original TQS ranking with no threshold
    # optimization and no change in execution, risk or side-price validity.
    try:
        candidate,decision=choose_candidate(observation,now)
    except CalendarCoverageError:
        candidate,decision=None,'MARKET_CLOSED'
    except (KeyError,ValueError,TypeError):
        candidate,decision=None,'MISSING_BASELINE_FEATURES'
    scored=[row for row in result['tqs_scores']['rows'] if row.get('status')=='REFERENCE_DIAGNOSTIC_ONLY']
    ranked=sorted(scored,key=lambda row:(-row['tqs_liquidity_score'],-row['tqs_premium_efficiency_score'],row['contract']))
    baseline_legs=({leg['key'] for leg in candidate['legs']} if candidate else set())
    result['baseline_vs_tqs']={'baseline_status':decision,
       'baseline_legs':sorted(baseline_legs),
       'top_liquidity_contract':ranked[0]['contract'] if ranked else None,
       'top_tqs_same_as_baseline_leg':ranked[0]['contract'] in baseline_legs if ranked else None,
       'eligible_for_automatic_entry':False,'profit_difference':'NOT_MEASURED',
       'scorer_contract_sample':len(quotes),'source_quotes_total':len(observation.get('quotes') or [])}
    if enable_cboe:result['cboe_oi']=cboe_contract_comparison(subset,fetcher=cboe_fetcher,max_quotes=max_quotes)
    if enable_smile:result['iv_smile']=expiry_smile_comparison(subset,chart=chart,lens=lens)
    if enable_moomoo:result['moomoo']=moomoo_comparison(subset,fetcher=moomoo_fetcher,max_contracts=min(4,max_quotes))
    return result
