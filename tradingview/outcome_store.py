"""Additive audited outcome persistence. No network or implicit DB access.

Historical price_snapshots are NOT treated as market observations: their old
snapshot_time may only be retrieval time. Trusted observations require explicit
source, observed_at and available_at. Legacy alerts remain not_eligible until
real entry provenance is supplied. Reports describe point samples, not trades.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from tradingview.outcome_logic import (MeasurementOrigin, SignalMeasurement,
    PointObservation, evaluate_outcome, summarize_outcomes)

SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_entries (
 alert_uuid TEXT PRIMARY KEY, measurement_json TEXT NOT NULL, validation_error TEXT);
CREATE TABLE IF NOT EXISTS outcome_points (
 alert_uuid TEXT NOT NULL, observed_at TEXT NOT NULL, available_at TEXT NOT NULL,
 price REAL NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY (alert_uuid, observed_at));
CREATE TABLE IF NOT EXISTS outcome_observation_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, alert_uuid TEXT NOT NULL,
 attempted_at TEXT NOT NULL, price REAL, observed_at TEXT, available_at TEXT,
 source TEXT, trusted INTEGER NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS fire_attempt_evidence (
 attempt_id TEXT PRIMARY KEY, pro_symbol TEXT NOT NULL, fire_time TEXT NOT NULL,
 resolution TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, completion_ref TEXT,
 selection_dedup_key TEXT, deduplicated_against TEXT, considered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fire_attempt_resets (
 reset_id TEXT PRIMARY KEY, pro_symbol TEXT NOT NULL, fire_time TEXT NOT NULL,
 resolution TEXT NOT NULL, reason TEXT NOT NULL, reset_at TEXT NOT NULL,
 reset_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcome_history_audit (
 alert_uuid TEXT PRIMARY KEY, finding TEXT NOT NULL, reviewed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcome_resolution_map (
 alert_uuid TEXT NOT NULL, pro_symbol TEXT NOT NULL, fire_time TEXT NOT NULL,
 resolution TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY (alert_uuid, pro_symbol, fire_time, resolution, source));
CREATE TABLE IF NOT EXISTS scoring_results (
 pro_symbol TEXT NOT NULL, fire_time TEXT NOT NULL, resolution TEXT NOT NULL,
 run_id TEXT NOT NULL, scored_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY (pro_symbol, fire_time, resolution));
CREATE INDEX IF NOT EXISTS idx_scoring_results_run ON scoring_results (run_id);
CREATE INDEX IF NOT EXISTS idx_scoring_results_scored_at ON scoring_results (scored_at)
"""


def ensure_schema(conn):
    # execute, rather than executescript, preserves the caller's transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def dt(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return result.astimezone(timezone.utc)


def measurement(row, payload):
    # Missing historical provenance remains unknown; never infer decision time
    # from created_at, a prior close from entry_price, or availability from fire.
    origin = MeasurementOrigin(payload.get('origin', 'fire'))
    fire = dt(row['fire_time'])
    analysis = dt(payload['analysis_at']) if payload.get('analysis_at') else None
    return SignalMeasurement(
        signal_id=row['alert_uuid'], origin=origin, fire_at=fire,
        registered_at=dt(row['created_at']), analysis_at=analysis,
        entry_price=payload.get('entry_price'),
        entry_price_source=payload.get('entry_price_source'),
        entry_price_at=dt(payload['entry_price_at']) if payload.get('entry_price_at') else None,
        entry_available_at=dt(payload['entry_available_at']) if payload.get('entry_available_at') else None,
        target_return_pct=payload.get('target_return_pct', 10),
        stop_return_pct=payload.get('stop_return_pct', -5),
        horizon=timedelta(hours=payload.get('horizon_hours', 168)),
        horizon_tolerance=timedelta(hours=payload.get('horizon_tolerance_hours', 1)))


def record_entry(conn, row, payload):
    ensure_schema(conn)
    # Explicit caller-supplied provenance only; retain validation failure visibly.
    error = None
    try:
        item = measurement(row, payload)
    except (ValueError, TypeError, OverflowError) as exc:
        error = str(exc)
        item = None
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    previous = conn.execute('SELECT measurement_json FROM outcome_entries WHERE alert_uuid=?', (row['alert_uuid'],)).fetchone()
    if previous is not None:
        if not payload:
            return  # re-registration without provenance never overwrites or conflicts
        if previous[0] != encoded:
            raise ValueError('conflicting entry provenance; explicit correction required')
        return
    if not payload:
        return  # legacy/unknown: stays implicit-unknown, no fabricated row
    conn.execute('INSERT INTO outcome_entries VALUES (?,?,?)', (row['alert_uuid'], encoded, error))
    if item is not None:
        tracking_until = item.expiry_at + item.horizon_tolerance
        conn.execute('UPDATE alerts SET expires_at=? WHERE alert_uuid=?', (tracking_until.isoformat(), row['alert_uuid']))


def record_resolution_mapping(conn, alert_uuid, fire, source=None):
    ensure_schema(conn)
    pro_symbol = str(fire.get('pro_symbol') or fire.get('symbol') or '').strip()
    fire_time = str(fire.get('fire_time') or '').strip()
    resolution = str(fire.get('resolution') or '').strip()
    source_name = str(source or fire.get('_source') or 'unknown').strip()
    if not pro_symbol or not fire_time or not resolution or not source_name:
        raise ValueError('resolution mapping requires symbol, fire_time, resolution, and source')
    conn.execute(
        'INSERT OR IGNORE INTO outcome_resolution_map VALUES (?,?,?,?,?)',
        (alert_uuid, pro_symbol, fire_time, resolution, source_name),
    )


def record_point(conn, uuid, price, observed_at, available_at, source):
    ensure_schema(conn)
    if observed_at is None:
        return  # provider timestamp unknown; legacy snapshot is not trusted
    if available_at is None or source is None:
        raise ValueError('trusted observation requires availability and source')
    if not isinstance(source, str) or not source.strip():
        raise ValueError('observation source required')
    point = PointObservation(dt(observed_at), price, dt(available_at))
    values = (uuid, point.observed_at.isoformat(), point.available_at.isoformat(), point.price, source)
    old = conn.execute('SELECT available_at,price,source FROM outcome_points WHERE alert_uuid=? AND observed_at=?', values[:2]).fetchone()
    if old is not None:
        if tuple(old) != values[2:]:
            raise ValueError('conflicting persisted observation')
        return
    conn.execute('INSERT INTO outcome_points VALUES (?,?,?,?,?)', values)


def record_observation_attempt(
    conn, uuid, price, observed_at, available_at, source, trusted, reason=None,
):
    """Record provider coverage separately from trusted outcome points."""
    ensure_schema(conn)
    conn.execute(
        'INSERT INTO outcome_observation_attempts '
        '(alert_uuid,attempted_at,price,observed_at,available_at,source,trusted,reason) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (
            uuid,
            datetime.now(timezone.utc).isoformat(),
            price,
            observed_at,
            available_at,
            source,
            int(bool(trusted)),
            reason,
        ),
    )


def evaluate_records(conn, as_of=None, audit=True):
    ensure_schema(conn)
    as_of = as_of or datetime.now(timezone.utc)
    evaluations, invalid = [], []
    for row in conn.execute('SELECT alert_uuid,fire_time,created_at FROM alerts ORDER BY alert_uuid').fetchall():
        row = dict(zip(('alert_uuid','fire_time','created_at'), row))
        stored = conn.execute('SELECT measurement_json FROM outcome_entries WHERE alert_uuid=?', (row['alert_uuid'],)).fetchone()
        payload = json.loads(stored[0]) if stored else {}
        try:
            item = measurement(row, payload)
            points = [PointObservation(dt(r[0]), r[2], dt(r[1])) for r in conn.execute('SELECT observed_at,available_at,price FROM outcome_points WHERE alert_uuid=?', (row['alert_uuid'],))]
            evaluations.append(evaluate_outcome(item, points, as_of=as_of))
        except (ValueError, TypeError, OverflowError) as exc:
            invalid.append({'signal_id': row['alert_uuid'], 'reason': str(exc)})
        if stored is None and audit:
            conn.execute('INSERT OR IGNORE INTO outcome_history_audit VALUES (?,?,?)', (row['alert_uuid'], 'legacy provenance unknown; no fabricated backfill', as_of.isoformat()))
    summary = summarize_outcomes(evaluations)
    return evaluations, summary, invalid


def report(conn, as_of=None, audit=True):
    as_of = as_of or datetime.now(timezone.utc)
    values, summary, invalid = evaluate_records(conn, as_of, audit=audit)
    den = summary.denominators
    counts = {status.value: count for status, count in summary.status_counts.items()}
    pending = max(0, den.eligible - den.mature)
    expired = counts.get('mature_no_observed_threshold_hit', 0)
    attempt_counts = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fire_attempt_evidence'").fetchone():
        attempt_counts = {
            row[0]: row[1]
            for row in conn.execute('SELECT state, COUNT(*) FROM fire_attempt_evidence GROUP BY state')
        }
    legacy_unverified = 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='downloaded_fires'").fetchone():
        legacy_unverified = conn.execute(
            "SELECT COUNT(*) FROM downloaded_fires WHERE scored_at IS NOT NULL"
        ).fetchone()[0]
    coverage = observation_coverage(conn)
    rows = ''.join('<tr><td>'+html.escape(v.signal_id)+'</td><td>'+v.status.value+'</td><td>'+html.escape(str(v.fixed_horizon_return_pct))+'</td><td>'+html.escape(str(v.horizon_offset))+'</td></tr>' for v in values)
    issues = ''.join('<li>'+html.escape(v['signal_id']+': '+v['reason'])+'</li>' for v in invalid)
    return ('<!doctype html><html><body><h1>Audited observational outcomes</h1>'
        '<p>Point observations, not execution P&amp;L or continuous first-passage coverage. '
        'Population: all tracker alerts; eligible does not mean actionable. '
        'Invalid records are listed separately and excluded from evaluated denominators.</p>'
        '<p>As of: '+html.escape((as_of or datetime.now(timezone.utc)).isoformat())+'</p>'
        '<p>Registered: '+str(den.registered)+'; eligible: '+str(den.eligible)+'; pending: '+str(pending)+'; expired: '+str(expired)+'; measured: '+str(den.measured)+'; invalid: '+str(len(invalid))+'</p>'
        '<p>Target touches: '+str(den.target_touched)+'; retry-exhausted: '+str(attempt_counts.get("filtered", 0) + attempt_counts.get("failed", 0))+'; legacy-unverified: '+str(legacy_unverified)+'</p>'
        '<p>Observation coverage: trusted: '+str(coverage['trusted'])+'; untrusted: '+str(coverage['untrusted'])+'; no observations: '+str(coverage['no_observations'])+'; untrusted reasons: '+html.escape(json.dumps(coverage['untrusted_by_reason'], sort_keys=True))+'</p>'
        '<pre>'+html.escape(json.dumps(vars(den), sort_keys=True))+'</pre>'
        '<pre>'+html.escape(json.dumps(counts, sort_keys=True))+'</pre>'
        '<table><tr><th>Signal</th><th>Observed status</th><th>Horizon return %</th><th>Offset from expiry</th></tr>'+rows+'</table>'
        '<h2>Invalid records</h2><ul>'+issues+'</ul></body></html>')


def report_json(conn, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)
    values, summary, invalid = evaluate_records(conn, as_of, audit=True)
    return {
        "metadata": {
            "kind": "observational_outcome_audit",
            "as_of": as_of.isoformat(),
            "statement": "point observations, not executed P&L or continuous first-passage evidence",
        },
        "denominators": vars(summary.denominators),
        "status_counts": {key.value: value for key, value in summary.status_counts.items()},
        "rows": [
            {
                "signal_id": value.signal_id,
                "status": value.status.value,
                "target_touched": value.target_touched,
                "max_observed_return_pct": value.max_observed_return_pct,
                "fixed_horizon_return_pct": value.fixed_horizon_return_pct,
                "horizon_offset": str(value.horizon_offset) if value.horizon_offset else None,
            }
            for value in values
        ],
        "invalid": invalid,
        "observation_coverage": observation_coverage(conn),
    }


def observation_coverage(conn):
    """Return trusted, untrusted-reason, and no-observation denominators."""
    ensure_schema(conn)
    trusted, untrusted = conn.execute(
        'SELECT COALESCE(SUM(trusted),0), COALESCE(SUM(CASE WHEN trusted=0 THEN 1 ELSE 0 END),0) '
        'FROM outcome_observation_attempts'
    ).fetchone()
    reasons = {
        row[0] or 'unknown': row[1]
        for row in conn.execute(
            'SELECT reason, COUNT(*) FROM outcome_observation_attempts '
            'WHERE trusted=0 GROUP BY reason'
        )
    }
    no_observations = conn.execute(
        'SELECT COUNT(*) FROM alerts a LEFT JOIN outcome_observation_attempts o '
        'ON o.alert_uuid=a.alert_uuid WHERE o.alert_uuid IS NULL'
    ).fetchone()[0]
    return {
        'trusted': int(trusted or 0),
        'untrusted': int(untrusted or 0),
        'untrusted_by_reason': reasons,
        'no_observations': int(no_observations),
    }


def persist_attempts(conn, attempts):
    ensure_schema(conn)
    from tradingview.outcome_logic import classify_fire_batch
    for entry in classify_fire_batch(attempts):
        for a in entry.attempts:
            values = (a.attempt_id, a.fire.pro_symbol, a.fire.fire_time, a.fire.resolution, a.state.value, a.reason, a.completion_ref, a.selection_dedup_key,
                      json.dumps(vars(a.deduplicated_against), sort_keys=True) if a.deduplicated_against else None,
                      a.considered_at.isoformat() if a.considered_at else '')
            old = conn.execute('SELECT * FROM fire_attempt_evidence WHERE attempt_id=?', (a.attempt_id,)).fetchone()
            if old is not None:
                # considered_at is a wall-clock stamp; idempotency is on evidence.
                if tuple(old)[:9] != values[:9]:
                    raise ValueError('conflicting persisted attempt')
                continue
            conn.execute('INSERT INTO fire_attempt_evidence VALUES (?,?,?,?,?,?,?,?,?,?)', values)


def attempt_id_for(run_id, key):
    return hashlib.sha256((str(run_id)+repr(key)).encode()).hexdigest()


def persist_completed(conn, fire, run_id, result=None):
    """Durably record one completed scoring result as soon as it exists, so an
    interrupted run still leaves evidence for work already finished.

    The recoverable scoring payload and the completion evidence are written in
    the **same transaction** on the same connection. A crash therefore cannot
    leave completion evidence that suppresses a fire whose result was never
    retained — the two either both land or neither does.

    ``result`` is the scored result item (including its eligibility verdict and
    actionability reasons). It is optional so existing callers keep working,
    but the scheduler path always supplies it.
    """
    from tradingview.outcome_logic import FireIdentity, FireAttempt, FireDisposition
    from database.fires_ledger import fire_key
    key = fire_key(fire)
    if not all(key):
        return
    if result is not None:
        record_scoring_result(conn, fire, result, run_id)
    persist_attempts(conn, [FireAttempt(attempt_id_for(run_id, key), FireIdentity(*key),
        FireDisposition.COMPLETED, completion_ref=str(run_id)+':'+repr(key),
        considered_at=datetime.now(timezone.utc))])


def record_scoring_result(conn, fire, result, run_id):
    """Persist the recoverable scoring payload for one completed fire.

    Idempotent: re-recording the same fire replaces its payload rather than
    raising, so a retried run cannot fail on an already-recorded identity.
    """
    from database.fires_ledger import fire_key
    key = fire_key(fire)
    if not all(key):
        return False
    payload = {
        'run_id': str(run_id),
        'pro_symbol': key[0],
        'fire_time': key[1],
        'resolution': key[2],
        'scored_at': datetime.now(timezone.utc).isoformat(),
        'result': result,
    }
    conn.execute(
        'INSERT OR REPLACE INTO scoring_results '
        '(pro_symbol, fire_time, resolution, run_id, scored_at, payload_json) '
        'VALUES (?,?,?,?,?,?)',
        (key[0], key[1], key[2], str(run_id), payload['scored_at'],
         json.dumps(payload, default=str)),
    )
    return True


def load_scoring_results(conn, run_id=None, limit=None):
    """Return recoverable scoring payloads, newest first.

    Used by the scheduler to rebuild notification work after a crash without
    re-scoring fires that already completed.
    """
    ensure_schema(conn)
    sql = 'SELECT payload_json FROM scoring_results'
    params = []
    if run_id is not None:
        sql += ' WHERE run_id = ?'
        params.append(str(run_id))
    sql += ' ORDER BY scored_at DESC'
    if limit is not None:
        sql += ' LIMIT ?'
        params.append(int(limit))
    out = []
    for row in conn.execute(sql, params):
        try:
            out.append(json.loads(row[0]))
        except (TypeError, ValueError):
            continue
    return out


def persist_run(conn, batch, selected, completed, failures, run_id, filtered=None):
    from tradingview.outcome_logic import FireIdentity, FireAttempt, FireDisposition
    from database.fires_ledger import fire_key
    now = datetime.now(timezone.utc)
    chosen = {fire_key(f) for f in selected}
    done = {fire_key(f) for f in completed}
    selected_symbols = {fire_key(f)[0]: fire_key(f) for f in selected}
    filtered = filtered or {}
    records = []
    for fire in batch:
        key = fire_key(fire)
        if not all(key):
            continue
        identity = FireIdentity(*key)
        kwargs = {}
        if key in done:
            state = FireDisposition.COMPLETED
            kwargs['completion_ref'] = str(run_id)+':'+repr(key)
        elif key in failures:
            state = FireDisposition.FAILED
            kwargs['reason'] = failures[key]
        elif key in filtered:
            state = FireDisposition.FILTERED
            kwargs['reason'] = filtered[key]
        elif key[0] in selected_symbols:
            state = FireDisposition.DEDUPLICATED
            kwargs['selection_dedup_key'] = key[0]
            kwargs['deduplicated_against'] = FireIdentity(*selected_symbols[key[0]])
        else:
            state = FireDisposition.CONSIDERED_NOT_ATTEMPTED
        attempt_id = attempt_id_for(run_id, key)
        records.append(FireAttempt(attempt_id, identity, state, considered_at=now, **kwargs))
    persist_attempts(conn, records)


def install_tracker_outcomes(cls):
    """Additive tracker hooks. Never replaces an existing method's contract."""
    if getattr(cls, '_audited_outcomes_installed', False):
        return
    if getattr(cls, '_native_outcome_tracking', False):
        cls._audited_outcomes_installed = True
        return
    import logging
    log = logging.getLogger(__name__)
    original_record = cls.record_alert
    original_stats = cls.get_stats

    def provenance_row(conn, uuid):
        row = conn.execute('SELECT alert_uuid,fire_time,created_at FROM alerts WHERE alert_uuid=?', (uuid,)).fetchone()
        return dict(zip(('alert_uuid', 'fire_time', 'created_at'), row)) if row else None

    def record(self, fire, result):
        uuid = original_record(self, fire, result)
        payload = (result or {}).get('outcome_measurement') or {}
        if uuid and payload:
            # Alert is already committed; a provenance failure must neither hide
            # the alert nor abort the run. It is logged and repairable by a
            # later call (record_entry is idempotent for identical payloads).
            try:
                with self._conn() as conn:
                    row = provenance_row(conn, uuid)
                    if row:
                        record_entry(conn, row, payload)
            except Exception as exc:
                log.warning('outcome provenance not stored for %s: %s', uuid, exc)
        return uuid

    def stats(self):
        # Preserve every legacy top-level key; audited data is additive only.
        result = original_stats(self)
        try:
            with self._conn() as conn:
                _, summary, invalid = evaluate_records(conn)
            result['observational_denominators'] = vars(summary.denominators)
            result['observational_status_counts'] = {k.value: v for k, v in summary.status_counts.items()}
            result['observational_invalid_records'] = invalid
        except Exception as exc:
            log.warning('observational stats unavailable: %s', exc)
        return result

    def audit_report(self):
        with self._conn() as conn:
            return report(conn)

    cls.record_alert = record
    cls.get_stats = stats
    cls.build_outcome_audit_report = audit_report
    cls._audited_outcomes_installed = True
