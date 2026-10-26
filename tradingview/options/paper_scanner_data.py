"""Read-only provider for the canonical US options paper experiment.

TradingView supplies reference bid/ask, IV and delta. Yahoo supplies completed
daily history and exact-expiry/type/strike OI and REGULAR contract specification.
Generic TV updates remain reference observations, never bid/ask-side timestamps.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from tradingview.options.paper_scanner import NY, POLICY, number


def daily_features(history, now):
    from tradingview.scheduler.market_calendar import _calendar
    if history.empty or history.index.tz is None:
        raise ValueError('timestamped daily history unavailable')
    history = history.sort_index()
    history = history[[d.date()<now.astimezone(NY).date() for d in history.index]]
    if history.index.has_duplicates or len(history)<60:
        raise ValueError('insufficient unique completed daily bars')
    close = history['Close'].astype(float)
    if not all(math.isfinite(v) and v>0 for v in close):
        raise ValueError('invalid daily closes')
    returns = close.pct_change().dropna().tail(20)
    rv = float(returns.std(ddof=1)*math.sqrt(252)*100)
    day = history.index[-1].date().isoformat()
    schedule = _calendar('NYSE').schedule(start_date=day,end_date=day)
    if schedule.empty:
        raise ValueError('daily bar is not from a trading session')
    return dict(close=float(close.iloc[-1]),ema20=float(close.ewm(span=20,adjust=False).mean().iloc[-1]),
                ema50=float(close.ewm(span=50,adjust=False).mean().iloc[-1]),rv20=rv,
                asof=schedule.iloc[-1]['market_close'].isoformat(),source='Yahoo completed adjusted daily closes')


def enrich_contracts(symbol, contracts, chains, observed_at):
    """Exact matches only. Absent metadata is rejected, never defaulted to 100."""
    def optional(value):
        try:
            return number(value)
        except (TypeError, ValueError):
            return None
    result = []
    for c in contracts:
        try:
            chain = chains[c['expiry']]
            table = chain.calls if c['option_type']=='CALL' else chain.puts
            matched = table[table['strike']==c['strike']]
            if len(matched)!=1:
                continue
            row = matched.iloc[0]
            if row.get('contractSize')!='REGULAR' or row.get('currency')!='USD':
                continue
            update = datetime.fromtimestamp(number(c['tv_update_time'],positive=True),timezone.utc)
            q = dict(symbol=symbol,expiry=c['expiry'],strike=c['strike'],option_type=c['option_type'],
                     bid=number(c['bid']),ask=number(c['ask']),iv=optional(c.get('iv')),delta=optional(c.get('delta')),
                     oi=optional(row.get('openInterest')),volume=optional(c.get('volume')) if c.get('volume_reported') is True else None,
                     spot=optional(c.get('spot_price')),
                     provider='TradingView scan2',basis='reference',updated_at=update.isoformat(),
                     currency='USD',multiplier=100,deliverable='REGULAR',
                     contract_spec_source='Yahoo exact expiry/type/strike contractSize=REGULAR',
                     oi_source='Yahoo exact expiry/type/strike; settlement-age unspecified',
                     volume_session=update.astimezone(NY).date().isoformat(),
                     volume_provenance='inferred_from_generic_TV_update',received_at=observed_at.isoformat())
            result.append(q)
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    return result


def fetch_observation(symbol, active, now):
    import yfinance as yf
    from tradingview.options.tv_options import TVOptionsFetcher
    ticker = yf.Ticker(symbol.split(':',1)[1])
    try:
        hist = ticker.history(period='6mo',interval='1d',auto_adjust=True,timeout=15,raise_errors=True)
        features = daily_features(hist,now)
    except Exception:
        # Missing directional research must not block liquidation monitoring.
        features = {'error':'completed daily history unavailable'}
    fetcher = TVOptionsFetcher()
    collected = {}
    errors = []
    ref = now.astimezone(NY).date()
    # Active lots have priority; fetch their exact expiry and strikes even when
    # they have moved outside the discovery ATM or DTE window.
    requests = []
    for s in active:
        if s['status'] in {'OPEN','PENDING'}:
            expiry = datetime.fromisoformat(s['expiry']).date()
            dte = (expiry-ref).days
            if dte>=0:
                opt = s['legs'][0]['key'].split('|')[-1]
                strikes = [float(leg['key'].split('|')[-2]) for leg in s['legs']]
                requests.append((opt,dict(n_expiries=1,min_dte=dte,max_dte=dte,strike_bounds=(min(strikes),max(strikes)))))
    if not active:
        requests.extend((opt,dict(n_expiries=3,min_dte=POLICY['min_dte'],max_dte=POLICY['max_dte'])) for opt in ('CALL','PUT'))
    for opt,kw in requests:
        fetch = fetcher.get_calls_outcome if opt=='CALL' else fetcher.get_puts_outcome
        outcome = fetch(symbol,reference_date=ref,**kw)
        if outcome.status=='error':
            errors.append(outcome.diagnostic_code or 'chain_error')
        for c in outcome.contracts:
            collected[(c['expiry'],c['strike'],c['option_type'])]=c
    if errors:
        raise ValueError('option provider failed')
    # TradingView can list expiries that Yahoo does not publish (or vice versa).
    # Reject those precise contracts, NOT the independently matched expiries.
    # Never substitute the nearest available date or a different strike.
    tv_expiries = {c['expiry'] for c in collected.values()}
    yahoo_expiries = set(ticker.options) if tv_expiries else set()
    skipped_expiries = sorted(tv_expiries - yahoo_expiries)
    admitted = [c for c in collected.values() if c['expiry'] in yahoo_expiries]
    chains = {expiry:ticker.option_chain(expiry) for expiry in sorted(tv_expiries & yahoo_expiries)}
    received = datetime.now(timezone.utc)
    quotes = enrich_contracts(symbol,admitted,chains,received)
    if collected and not quotes:
        raise ValueError('no independently matched exact options contracts')
    return dict(symbol=symbol,observed_at=received.isoformat(),features=features,quotes=quotes,
                unmatched_expiries=skipped_expiries,
                source_note='Reference prices only; TV-only expiries rejected; bid/ask freshness and multi-leg fills unverified')


def fetch_research_observation(symbol, active, now, headers, *, lens_config=None):
    """Opt-in research overlay; default paper provider/scheduler is unchanged.

    Underlying options/positions still use the SAME frozen `fetch_observation`;
    the old ZeroDTE IV / VOI endpoints add only independent labelled metadata.
    The endpoint receipt is not a provider as-of side timestamp or a forecast.
    """
    from tradingview.options.paper_research_lenses import (
        fetch_research_lenses, with_research_lenses,
    )
    observation = fetch_observation(symbol, active, now)
    kw = {'config':lens_config} if lens_config is not None else {}
    lens = fetch_research_lenses(symbol, headers, **kw)
    return with_research_lenses(observation, lens)


def fetch_full_research_observation(symbol, active, now, headers=None, *,
                                    enable_tv_lenses=False,enable_cboe=False,
                                    enable_smile=False,enable_moomoo=False,
                                    max_quotes=12):
    """Explicit opt-in, never used by frozen scheduler or entry logic.

    Native baseline quotes/features are unchanged, all supplemental providers
    appear ONLY under source_comparisons / research_lenses metadata.
    """
    from tradingview.options.paper_research_sources import compare_all_sources
    native=fetch_observation(symbol,active,now)
    if enable_tv_lenses:
        from tradingview.options.paper_research_lenses import fetch_research_lenses,with_research_lenses
        if headers is None:raise ValueError('explicit TradingView auth headers required')
        native=with_research_lenses(native,fetch_research_lenses(symbol,headers))
    return dict(native,source_comparisons=compare_all_sources(
        native,now,enable_cboe=enable_cboe,enable_smile=enable_smile,
        enable_moomoo=enable_moomoo,max_quotes=max_quotes))
