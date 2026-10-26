"""Append-only, OWNER-ONLY fair-experiment decision and future reference journal.

Stores the actual allocated portfolios and original immutable decisions. Quotes
must arrive later and be replayed by frozen paper_scanner.advance; no fills.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from .architecture_forecast_validation import _private,aware
from .architecture_fair_experiment import replay

DDL='''CREATE TABLE IF NOT EXISTS experiment_decision (
 id TEXT PRIMARY KEY, decision_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_reference_event (
 id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 payload TEXT NOT NULL, UNIQUE(experiment_id,observed_at));'''

def _canon(value):
    return json.dumps(value,sort_keys=True,allow_nan=False)

def save(path, experiment):
    if experiment.get('broker_orders')!=0 or experiment.get('actual_broker_profit')!='NOT_MEASURED':
        raise ValueError('BROKER_PROFIT_CLAIM_FORBIDDEN')
    clock=experiment['decision_at']
    aware(clock)
    blob=_canon(experiment)
    ident=hashlib.sha256(blob.encode()).hexdigest()
    dbpath=_private(path)
    with sqlite3.connect(dbpath) as db:
        db.executescript(DDL)
        row=db.execute('SELECT payload FROM experiment_decision WHERE id=?',(ident,)).fetchone()
        if row is not None and row[0]!=blob:raise ValueError('DECISION_IMMUTABILITY_VIOLATION')
        db.execute('INSERT OR IGNORE INTO experiment_decision VALUES (?,?,?)',(ident,clock,blob))
    return ident

def append(path,ident,observation):
    clock=aware(observation['observed_at']);blob=_canon(observation)
    dbpath=_private(path)
    with sqlite3.connect(dbpath) as db:
        db.executescript(DDL)
        row=db.execute('SELECT decision_at FROM experiment_decision WHERE id=?',(ident,)).fetchone()
        if row is None:raise ValueError('UNKNOWN_EXPERIMENT')
        if clock<=aware(row[0]):raise ValueError('FUTURE_ONLY_EVENT_REQUIRED')
        earlier=db.execute('SELECT observed_at FROM experiment_reference_event WHERE experiment_id=? '
                           'ORDER BY observed_at DESC LIMIT 1',(ident,)).fetchone()
        if earlier and clock<aware(earlier[0]):raise ValueError('REFERENCE_EVENT_BACKDATE')
        oid=hashlib.sha256((ident+'|'+clock.isoformat()).encode()).hexdigest()
        present=db.execute('SELECT payload FROM experiment_reference_event WHERE id=?',(oid,)).fetchone()
        if present and present[0]!=blob:raise ValueError('EVENT_OVERWRITE_FORBIDDEN')
        with db:
            cur=db.execute('INSERT OR IGNORE INTO experiment_reference_event VALUES(?,?,?,?)',
                           (oid,ident,clock.isoformat(),blob))
        return cur.rowcount==1

def evaluate(path,ident,*,frozen_advance=None,policy=None):
    dbpath=_private(path)
    with sqlite3.connect(dbpath) as db:
        db.executescript(DDL)
        row=db.execute('SELECT payload FROM experiment_decision WHERE id=?',(ident,)).fetchone()
        if row is None:raise ValueError('UNKNOWN_EXPERIMENT')
        experiment=json.loads(row[0])
        events=[json.loads(r[0]) for r in db.execute(
               'SELECT payload FROM experiment_reference_event WHERE experiment_id=? '
               'ORDER BY observed_at',(ident,)).fetchall()]
    study={k:experiment[k] for k in ('mode','decision_at','cash_budget','baseline','challenger')}
    report=replay(study,events,frozen_advance=frozen_advance,policy=policy)
    report.update(saved_reference_events=len(events),decision_immutable=True,
                  observed_profit='NOT_MEASURED',evidence='REFERENCE_ONLY')
    return report
