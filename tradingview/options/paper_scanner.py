"""Canonical US options forward experiment. No orders, holdings or notifications.

All trades are simulated vertical spreads. The immutable policy and evidence
travel with each trade; reference observations never become executable quotes.
Run: python -m tradingview.options.paper_scanner [--report-only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
VERSION = 'us-vertical-paper-v1'
ROOT = Path(__file__).resolve().parents[2]
POLICY = dict(version=VERSION, universe=['AMEX:SPY', 'NASDAQ:QQQ', 'AMEX:IWM'],
              initial_capital=10000.0, trade_risk=250.0, total_risk=1000.0,
              min_dte=30, max_dte=60, exit_dte=7, max_hold_days=21,
              min_oi=100, min_volume=10, max_spread_fraction=.10,
              max_width=5.0, max_debit_fraction=.60, min_credit_fraction=.20,
              buy_iv_rv=1.10, sell_iv_rv=1.20, fee_per_leg=.65,
              slippage_per_share=.01, pending_minutes=30,
              quote_age_seconds=900, leg_skew_seconds=120,
              debit_target_fraction=.50, debit_stop_fraction=.40,
              credit_target_fraction=.50, credit_stop_fraction=1.0)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS option_paper_policies (version TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS option_paper_runs (
 id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS option_paper_observations (
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS option_paper_trades (
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, session_date TEXT NOT NULL,
 version TEXT NOT NULL, status TEXT NOT NULL, state_json TEXT NOT NULL,
 UNIQUE(symbol,session_date,version));
CREATE TABLE IF NOT EXISTS option_paper_equity (
 observed_at TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
'''


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def stamp(value):
    t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('timezone-aware timestamp required')
    return t.astimezone(timezone.utc)


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('boolean is not a number')
    n = float(value)
    if not math.isfinite(n) or (positive and n <= 0):
        raise ValueError('invalid finite number')
    return n


def contract_key(q):
    expiry = date.fromisoformat(q['expiry'])
    strike = number(q['strike'], positive=True)
    if q['option_type'] not in {'CALL', 'PUT'} or not q.get('symbol'):
        raise ValueError('invalid option identity')
    return f"{q['symbol']}|{expiry}|{strike:g}|{q['option_type']}"


def validate_quote(q, now, *, entry=False, policy=POLICY):
    contract_key(q)
    bid, ask = number(q['bid']), number(q['ask'], positive=True)
    if bid < 0 or ask < bid:
        raise ValueError('invalid bid/ask')
    if not q.get('provider') or q.get('currency') != 'USD' or q.get('multiplier') != 100:
        raise ValueError('unsupported contract units or provider')
    if q.get('deliverable') != 'REGULAR' or not q.get('contract_spec_source'):
        raise ValueError('unverified contract deliverable')
    basis = q.get('basis')
    if basis == 'reference':
        times = [stamp(q['updated_at'])]
    elif basis == 'verified':
        times = [stamp(q['bid_at']), stamp(q['ask_at'])]
    else:
        raise ValueError('unknown quote basis')
    from tradingview.scheduler.market_calendar import market_session_bounds
    opening, closing = market_session_bounds('US', now.astimezone(NY).date().isoformat())
    age_limit = 120 if basis == 'verified' else policy['quote_age_seconds']
    if any(t > now or (now-t).total_seconds() > age_limit or not opening <= t < closing for t in times):
        raise ValueError('stale, future or outside-session quote')
    if entry:
        if bid <= 0 or (ask-bid)/((ask+bid)/2) > policy['max_spread_fraction']:
            raise ValueError('entry spread too wide')
        for field, minimum in [('oi', policy['min_oi']), ('volume', policy['min_volume'])]:
            n = number(q[field])
            if not n.is_integer() or n < minimum:
                raise ValueError('entry liquidity too low')
        if not q.get('oi_source') or q.get('volume_session') != now.astimezone(NY).date().isoformat():
            raise ValueError('liquidity provenance missing')
    return min(times), max(times)


def validate_observation(obs, now):
    if obs['symbol'] not in POLICY['universe']:
        raise ValueError('symbol outside frozen universe')
    at = stamp(obs['observed_at'])
    if at > now or now-at > timedelta(minutes=2):
        raise ValueError('observation receipt stale or future')
    seen = set()
    for q in obs['quotes']:
        if q['symbol'] != obs['symbol'] or contract_key(q) in seen:
            raise ValueError('wrong underlying or duplicate contract')
        seen.add(contract_key(q))
    return at


def signal(obs, now, policy=POLICY):
    """Only completed daily features. No score-to-probability conversion."""
    f = obs['features']
    asof = stamp(f['asof'])
    if asof >= now or asof.astimezone(NY).date() >= now.astimezone(NY).date():
        raise ValueError('daily features include current/future session')
    from tradingview.scheduler.market_calendar import _calendar
    sched = _calendar('NYSE').schedule(start_date=(now-timedelta(days=10)).date(),
                                        end_date=now.astimezone(NY).date())
    completed = sched[sched['market_close'] < now]
    if completed.empty or asof.date() != completed.iloc[-1]['market_close'].date():
        raise ValueError('daily features are not from latest completed session')
    close, fast, slow, rv = [number(f[k], positive=True) for k in ('close', 'ema20', 'ema50', 'rv20')]
    direction = 'BULL' if close > fast > slow else 'BEAR' if close < fast < slow else 'FLAT'
    return direction, rv


def cash_flow(legs, quotes, *, opening, policy=POLICY):
    """USD per one spread. Buys pay ask+slip, sells receive bid-slip."""
    cash = 0.0
    for leg, q in zip(legs, quotes):
        side = leg['side'] if opening else -leg['side']
        price = q['ask']+policy['slippage_per_share'] if side == 1 else max(0, q['bid']-policy['slippage_per_share'])
        cash -= side*price*100
    return cash


def economics(candidate, quotes, policy=POLICY):
    cash = cash_flow(candidate['legs'], quotes, opening=True, policy=policy)
    width = candidate['width']*100
    fees = 4*policy['fee_per_leg']
    if candidate['kind'] == 'DEBIT':
        if not 0 < -cash <= width*policy['max_debit_fraction']:
            raise ValueError('debit exceeds frozen value bound')
        risk, reward = -cash+fees, width+cash-fees
    else:
        if not width*policy['min_credit_fraction'] <= cash < width:
            raise ValueError('credit below frozen value bound')
        risk, reward = width-cash+fees, cash-fees
    if min(risk, reward) <= 0:
        raise ValueError('costs consume payoff')
    return cash, risk, reward


def choose_candidate(obs, now, policy=POLICY):
    direction, rv = signal(obs, now, policy)
    if direction == 'FLAT':
        return None, 'NO_TREND'
    usable = []
    for q in obs['quotes']:
        try:
            validate_quote(q, now, entry=True, policy=policy)
            dte = (date.fromisoformat(q['expiry'])-now.astimezone(NY).date()).days
            if policy['min_dte'] <= dte <= policy['max_dte']:
                number(q['iv'], positive=True)
                delta = number(q['delta'])
                if (q['option_type'] == 'CALL' and 0 < delta < 1) or (q['option_type'] == 'PUT' and -1 < delta < 0):
                    usable.append(q)
        except (KeyError, ValueError, TypeError):
            continue
    candidates = []
    for anchor in usable:
        delta = abs(anchor['delta'])
        ratio = anchor['iv']/rv
        for kind in ('DEBIT', 'CREDIT'):
            opt = ('CALL' if direction == 'BULL' else 'PUT') if kind == 'DEBIT' else ('PUT' if direction == 'BULL' else 'CALL')
            if anchor['option_type'] != opt:
                continue
            if kind == 'DEBIT' and not (.45 <= delta <= .65 and ratio <= policy['buy_iv_rv']):
                continue
            if kind == 'CREDIT' and not (.20 <= delta <= .35 and ratio >= policy['sell_iv_rv']):
                continue
            for wing in usable:
                if wing['expiry'] != anchor['expiry'] or wing['option_type'] != opt or wing['basis'] != anchor['basis']:
                    continue
                # Debit anchor is long; credit anchor is short. Wing is farther OTM.
                diff = (wing['strike']-anchor['strike'])*(1 if opt == 'CALL' else -1)
                if not 0 < diff <= policy['max_width']:
                    continue
                times = [validate_quote(q, now, entry=True, policy=policy) for q in (anchor, wing)]
                if (max(x[1] for x in times)-min(x[0] for x in times)).total_seconds() > policy['leg_skew_seconds']:
                    continue
                c = dict(strategy=f'{direction}_{opt}_{kind}', kind=kind, direction=direction,
                         symbol=obs['symbol'], expiry=anchor['expiry'], width=diff,
                         basis=anchor['basis'], iv_rv=ratio,
                         legs=[dict(key=contract_key(anchor), side=1 if kind == 'DEBIT' else -1),
                               dict(key=contract_key(wing), side=-1 if kind == 'DEBIT' else 1)])
                try:
                    cash, risk, reward = economics(c, [anchor, wing], policy)
                    if risk <= policy['trade_risk']:
                        c.update(indicative_cash=cash, risk_per_spread=risk, max_profit_per_spread=reward)
                        # Deterministic execution-quality rank, not a win probability.
                        cost = sum(q['ask']-q['bid'] for q in (anchor, wing))/diff
                        candidates.append((cost, abs((date.fromisoformat(c['expiry'])-now.astimezone(NY).date()).days-45), encode(c['legs']), c))
                except ValueError:
                    continue
    return (min(candidates, key=lambda c:c[:3])[3], 'CANDIDATE') if candidates else (None, 'NO_ELIGIBLE_SPREAD')


def advance(state, obs, now, *, available_risk):
    """Advance one archived observation; never repair a missing price with theory."""
    s = dict(state)
    p = s['policy']
    if s['status'] in {'CLOSED', 'CANCELLED', 'UNRESOLVED'}:
        return s
    if s['status'] == 'PENDING' and now > stamp(s['decision_at'])+timedelta(minutes=p['pending_minutes']):
        s.update(status='CANCELLED', reason='ENTRY_WINDOW_EXPIRED')
        return s
    if now.astimezone(NY).date() >= date.fromisoformat(s['expiry']):
        s.update(status='UNRESOLVED' if s['status']=='OPEN' else 'CANCELLED', reason='EXPIRY_WITHOUT_EXIT_QUOTE', mark_status='MISSING')
        return s
    try:
        validate_observation(obs, now)
        if obs['symbol'] != s['symbol']:
            raise ValueError('wrong observation symbol')
        indexed = {contract_key(q): q for q in obs['quotes']}
        quotes = [indexed[leg['key']] for leg in s['legs']]
        times = [validate_quote(q, now, entry=s['status']=='PENDING', policy=p) for q in quotes]
        if any(q['basis'] != s['basis'] for q in quotes):
            raise ValueError('quote cohort changed')
        at = min(x[0] for x in times)
        if (max(x[1] for x in times)-at).total_seconds() > p['leg_skew_seconds']:
            raise ValueError('leg updates too far apart')
        if s.get('last_quote_at') and at <= stamp(s['last_quote_at']):
            return s
    except (KeyError, TypeError, ValueError) as exc:
        s.update(mark_status='MISSING', last_error=str(exc))
        return s
    if s['status'] == 'PENDING':
        if at <= stamp(s['decision_at']):
            s.update(mark_status='WAITING_FOR_POST_DECISION_OBSERVATION')
            return s
        try:
            direction, rv = signal(obs, now, p)
            if direction != s['direction']:
                raise ValueError('trend changed before entry')
            ratio = number(quotes[0]['iv'], positive=True)/rv
            if (s['kind']=='DEBIT' and ratio>p['buy_iv_rv']) or (s['kind']=='CREDIT' and ratio<p['sell_iv_rv']):
                raise ValueError('volatility regime changed before entry')
            for leg, q in zip(s['legs'], quotes):
                if leg['side'] == -1:
                    spot = number(q['spot'], positive=True)
                    if (q['option_type']=='CALL' and spot>=q['strike']) or (q['option_type']=='PUT' and spot<=q['strike']):
                        raise ValueError('short leg already in the money before entry')
            cash, risk, reward = economics(s, quotes, p)
            qty = math.floor(min(p['trade_risk'], available_risk)/risk)
            if qty < 1:
                raise ValueError('risk budget exhausted')
        except (KeyError, TypeError, ValueError) as exc:
            s.update(status='CANCELLED', reason=str(exc))
            return s
        s.update(status='OPEN', entry_at=obs['observed_at'], entry_quote_at=at.isoformat(),
                 entry_quotes=quotes, entry_cash=cash*qty, quantity=qty,
                 risk=risk*qty, max_profit=reward*qty, entry_fees=2*p['fee_per_leg']*qty,
                 last_quote_at=at.isoformat(), mark_status='CURRENT', last_error=None)
        # Entry spread costs are immediately reflected in liquidation value.
    close_cash = cash_flow(s['legs'], quotes, opening=False, policy=p)*s['quantity']
    net = s['entry_cash']+close_cash-s['entry_fees']-2*p['fee_per_leg']*s['quantity']
    s.update(mark_at=obs['observed_at'], last_quote_at=at.isoformat(), mark_net=net,
             mark_status='CURRENT', last_error=None, mark_quotes=quotes)
    if s['entry_at'] == obs['observed_at']:
        return s
    reason = None
    base = abs(s['entry_cash'])
    if net <= -base*p['debit_stop_fraction' if s['kind']=='DEBIT' else 'credit_stop_fraction']:
        reason = 'LOSS_LIMIT'
    elif net >= base*p['debit_target_fraction' if s['kind']=='DEBIT' else 'credit_target_fraction']:
        reason = 'PROFIT_TARGET'
    elif (date.fromisoformat(s['expiry'])-now.astimezone(NY).date()).days <= p['exit_dte']:
        reason = 'PRE_EXPIRY_EXIT'
    elif now-stamp(s['entry_at']) >= timedelta(days=p['max_hold_days']):
        reason = 'TIME_EXIT'
    # Close a short leg reaching the money; assignment is not simulated.
    for leg, q in zip(s['legs'], quotes):
        if leg['side'] == -1:
            try:
                spot = number(q['spot'], positive=True)
            except (KeyError, TypeError, ValueError):
                s['assignment_warning'] = 'Underlying mark unavailable; assignment proximity unknown'
                continue
            if (q['option_type']=='CALL' and spot>=q['strike']) or (q['option_type']=='PUT' and spot<=q['strike']):
                reason = reason or 'SHORT_LEG_ITM'
    try:
        direction, _ = signal(obs, now, p)
        if direction != s['direction']:
            reason = reason or 'TREND_EXIT'
    except (KeyError, TypeError, ValueError):
        # Missing trend must not prevent price/time risk exits.
        s['signal_warning'] = 'Current completed trend unavailable'
    if reason:
        s.update(status='CLOSED', exit_at=obs['observed_at'], exit_quotes=quotes,
                 exit_cash=close_cash, net_pnl=net, exit_reason=reason,
                 exit_fees=2*p['fee_per_leg']*s['quantity'])
    return s


def states(conn):
    return [json.loads(row[0]) for row in conn.execute('SELECT state_json FROM option_paper_trades ORDER BY session_date,symbol')]


def save_state(conn, s):
    conn.execute('INSERT INTO option_paper_trades VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,state_json=excluded.state_json',
                 (s['id'],s['symbol'],s['session_date'],s['version'],s['status'],encode(s)))


def summary(conn, now):
    trades = states(conn)
    closed = [t for t in trades if t['status']=='CLOSED']
    active = [t for t in trades if t['status'] in {'OPEN','UNRESOLVED'}]
    usable = [t for t in active if t['status']=='OPEN' and t.get('mark_status')=='CURRENT'
              and now-stamp(t['last_quote_at'])<=timedelta(seconds=120 if t['basis']=='verified' else t['policy']['quote_age_seconds'])]
    realized = sum(t['net_pnl'] for t in closed)
    unrealized = sum(t['mark_net'] for t in usable)
    complete = len(usable)==len(active)
    pnl = [t['net_pnl'] for t in closed]
    losses = -sum(x for x in pnl if x<0)
    wins = sum(x for x in pnl if x>0)
    groups = []
    for key in sorted({(t['strategy'],t['basis']) for t in trades}):
        cohort = [t for t in trades if (t['strategy'],t['basis'])==key]
        done = [t for t in cohort if t['status']=='CLOSED']
        groups.append(dict(strategy=key[0],basis=key[1],decisions=len(cohort),closed=len(done),
                           net_pnl=sum(t['net_pnl'] for t in done)))
    return dict(asof=now.isoformat(),version=VERSION, decisions=len(trades), closed=len(closed),
                pending=sum(t['status']=='PENDING' for t in trades), open=len(active),
                cancelled=sum(t['status']=='CANCELLED' for t in trades), missing_marks=len(active)-len(usable),
                realized_net=realized, marked_unrealized_net=unrealized,
                equity=POLICY['initial_capital']+realized+unrealized if complete else None,
                risk_reserved=sum(t['risk'] for t in active),
                win_rate=sum(x>0 for x in pnl)/len(pnl) if pnl else None,
                expectancy=realized/len(pnl) if pnl else None,
                profit_factor=wins/losses if losses else None, cohorts=groups,
                verdict='UNPROVEN — forward paper research; no execution or profitability claim')


def run_cycle(db_path, report_path, *, now=None, provider=None):
    import fcntl
    from tradingview.scheduler.market_calendar import market_session
    fixed_now = now
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('aware clock required')
    db_path, report_path = Path(db_path), Path(report_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'BUSY'}
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.executescript(SCHEMA)
            with conn:
                conn.execute('INSERT OR IGNORE INTO option_paper_policies VALUES (?,?)',(VERSION,encode(POLICY)))
                saved = conn.execute('SELECT policy_json FROM option_paper_policies WHERE version=?',(VERSION,)).fetchone()[0]
                if saved != encode(POLICY):
                    raise ValueError('Frozen policy changed without a new strategy version')
            active, session = market_session('US', now)
            detail = {'symbols':{}, 'session':session}
            if active:
                if provider is None:
                    from tradingview.options.paper_scanner_data import fetch_observation
                    provider = fetch_observation
                for symbol in POLICY['universe']:
                    prior = [s for s in states(conn) if s['symbol']==symbol and s['status'] in {'PENDING','OPEN','UNRESOLVED'}]
                    try:
                        obs = provider(symbol, prior, now)
                        moment = fixed_now or datetime.now(timezone.utc)
                        if not market_session('US', moment)[0]:
                            raise ValueError('session ended during fetch')
                        validate_observation(obs, moment)
                        if obs['symbol'] != symbol:
                            raise ValueError('provider returned a different underlying')
                        payload = encode(obs)
                        oid = hashlib.sha256(payload.encode()).hexdigest()
                        with conn:
                            conn.execute('INSERT OR IGNORE INTO option_paper_observations VALUES (?,?,?,?)',
                                         (oid,symbol,obs['observed_at'],payload))
                            for s in prior:
                                current = summary(conn,moment)
                                budget = max(0,min(POLICY['total_risk']-current['risk_reserved'],
                                                  POLICY['initial_capital']+current['realized_net']-current['risk_reserved']))
                                updated = advance(s,obs,moment,available_risk=budget)
                                updated['last_observation_id'] = oid
                                save_state(conn,updated)
                            symbol_trades = [s for s in states(conn) if s['symbol']==symbol]
                            if any(s['status'] in {'PENDING','OPEN','UNRESOLVED'} for s in symbol_trades):
                                detail['symbols'][symbol] = 'POSITION_MONITORED'
                            elif any(s['session_date']==session and s['version']==VERSION for s in symbol_trades):
                                detail['symbols'][symbol] = 'DAILY_DECISION_ALREADY_RECORDED'
                            else:
                                c, reason = choose_candidate(obs,moment)
                                detail['symbols'][symbol] = reason
                                if c:
                                    sid = hashlib.sha256(f'{VERSION}|{session}|{symbol}'.encode()).hexdigest()
                                    c.update(id=sid,status='PENDING',session_date=session,version=VERSION,
                                             policy=dict(POLICY),decision_at=moment.isoformat(),
                                             decision_observation_id=oid,decision_features=obs['features'],
                                             mark_status='WAITING_FOR_POST_DECISION_OBSERVATION')
                                    save_state(conn,c)
                    except Exception as exc:
                        # Exception class is safe to persist; vendor messages can contain URLs/tokens.
                        detail['symbols'][symbol] = f'DATA_UNAVAILABLE:{type(exc).__name__}'
                        with conn:
                            for s in prior:
                                updated = advance(s,{'quotes':[]},fixed_now or datetime.now(timezone.utc),available_risk=0)
                                updated.update(mark_status='MISSING',last_error=f'Provider unavailable: {type(exc).__name__}')
                                save_state(conn,updated)
            moment = fixed_now or datetime.now(timezone.utc)
            if not active:
                with conn:
                    for s in states(conn):
                        if (s['status']=='PENDING' and moment>stamp(s['decision_at'])+timedelta(minutes=s['policy']['pending_minutes'])) or (
                            s['status']=='OPEN' and moment.astimezone(NY).date()>=date.fromisoformat(s['expiry'])):
                            save_state(conn,advance(s,{'quotes':[]},moment,available_risk=0))
            status = ('PARTIAL' if any(str(x).startswith('DATA_UNAVAILABLE') for x in detail['symbols'].values())
                      else 'COMPLETE') if active else 'MARKET_CLOSED'
            with conn:
                conn.execute('INSERT INTO option_paper_runs(observed_at,status,detail_json) VALUES (?,?,?)',
                             (moment.isoformat(),status,encode(detail)))
                stats = summary(conn,moment)
                conn.execute('INSERT OR REPLACE INTO option_paper_equity VALUES (?,?)',(moment.isoformat(),encode(stats)))
            from tradingview.options.paper_scanner_report import write_report
            write_report(conn,report_path,moment)
            return dict(status=status,detail=detail,**stats)
        finally:
            conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',type=Path,default=ROOT/'database/options_paper.db')
    ap.add_argument('--report',type=Path,default=ROOT/'reports/options_paper.html')
    ap.add_argument('--report-only',action='store_true')
    args = ap.parse_args(argv)
    if args.report_only:
        from tradingview.options.paper_scanner_report import write_report
        with sqlite3.connect(f'file:{args.db}?mode=ro',uri=True) as conn:
            write_report(conn,args.report,datetime.now(timezone.utc))
        print(args.report)
    else:
        print(encode(run_cycle(args.db,args.report)))


if __name__=='__main__':
    main()
