"""Durable append-only reference lifecycle for frozen vs challenger, research-only.

All transitions are quoted hypothetical crossed-leg observations, not fills.
Never changes source scanner/paper/broker databases. Read-only consumers can
summarize matched cohorts without relabeling reference prices as broker profit.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .architecture_forecast_validation import _private, aware
from .architecture_paired_evaluation import reference_cross, summarize

DDL = """CREATE TABLE IF NOT EXISTS paired_cohort(
 id TEXT PRIMARY KEY, original_body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paired_event(
 id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, arm TEXT NOT NULL,
 phase TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(cohort_id,arm,phase,observed_at));"""


def save_cohort(path, cohort):
    if cohort.get('broker_realized_pnl')!='NOT_MEASURED' or cohort.get('actual_broker_fills')!=0:
        raise ValueError('BROKER_CLAIMS_NOT_SUPPORTED')
    ident=cohort['cohort_id']
    original=json.dumps(cohort,sort_keys=True,allow_nan=False)
    p=_private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        row=db.execute('SELECT original_body FROM paired_cohort WHERE id=?',(ident,)).fetchone()
        if row is not None and row[0]!=original:
            raise ValueError('COHORT_IMMUTABLE')
        db.execute('INSERT OR IGNORE INTO paired_cohort VALUES (?,?)',(ident,original))
    return ident


def _reload(db,cohort_id):
    row=db.execute('SELECT original_body FROM paired_cohort WHERE id=?',(cohort_id,)).fetchone()
    if row is None:raise ValueError('UNKNOWN_COHORT')
    state=json.loads(row[0])
    events=db.execute('SELECT arm,phase,observed_at,payload FROM paired_event WHERE cohort_id=? '
                      'ORDER BY observed_at,phase,arm',(cohort_id,)).fetchall()
    for arm,phase,at,raw in events:
        event=json.loads(raw)
        result=reference_cross(state,arm,event_at=at,quotes=event['quotes'],phase=phase)
        if result['status'] not in ('REFERENCE_ENTRY','REFERENCE_CLOSED'):
            raise ValueError('REPLAY_FAILED_OR_SOURCE_CHANGED')
    return state,events


def append_reference(path, *,cohort_id,arm,phase,observed_at,quotes):
    """Append only after recomputing the actual deterministic transition.

    Quote sources may be delayed; do not manufacture an available fill.
    Caller controls this manual research action; no brokerage or network.
    """
    moment=aware(observed_at)
    p=_private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        state,events=_reload(db,cohort_id)
        if events and moment<max(aware(item[2]) for item in events):
            raise ValueError('BACKDATED_REFERENCE_EVENT')
        result=reference_cross(state,arm,event_at=observed_at,quotes=quotes,phase=phase)
        if result['status'] not in ('REFERENCE_ENTRY','REFERENCE_CLOSED'):
            return dict(saved=False,reason=result['status'],actual_broker_fills=0)
        entry=dict(quotes=quotes,source='EXACT_LEG_REFERENCE_NOT_FILL')
        body=json.dumps(entry,sort_keys=True,allow_nan=False)
        eid=hashlib.sha256((cohort_id+'|'+arm+'|'+phase+'|'+moment.isoformat()).encode()).hexdigest()
        with db:
            db.execute('INSERT OR IGNORE INTO paired_event VALUES (?,?,?,?,?,?)',
                       (eid,cohort_id,arm,phase,moment.isoformat(),body))
    return dict(saved=True,event_id=eid,reference_status=result['status'],actual_broker_fills=0)


def report(path,cohort_id):
    p=_private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        state,events=_reload(db,cohort_id)
    result=summarize(state)
    result['recorded_reference_events']=len(events)
    return result
