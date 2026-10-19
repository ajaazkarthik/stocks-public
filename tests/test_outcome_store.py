import ast
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from tradingview.outcome_store import (ensure_schema, record_entry, record_point,
    evaluate_records, report, report_json, install_tracker_outcomes, persist_run,
    record_observation_attempt, record_resolution_mapping, observation_coverage)

@pytest.fixture
def conn():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE alerts (alert_uuid TEXT PRIMARY KEY, fire_time TEXT, created_at TEXT, expires_at TEXT)')
    db.execute("INSERT INTO alerts VALUES ('a','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','legacy-expiry')")
    db.commit()
    yield db
    db.close()


def payload():
    return dict(origin='fire', entry_price=100, entry_price_source='fixture',
        entry_price_at='2026-01-01T00:00:00+00:00',
        entry_available_at='2026-01-01T00:00:00+00:00', horizon_hours=24)


def row():
    return dict(alert_uuid='a',fire_time='2026-01-01T00:00:00+00:00',created_at='2026-01-02T00:00:00+00:00')


def test_persist_evaluate_render_and_expiry(conn):
    record_entry(conn, row(), payload())
    assert conn.execute('SELECT expires_at FROM alerts').fetchone()[0] == '2026-01-02T01:00:00+00:00'
    record_point(conn, 'a', 104, '2026-01-02T00:30:00+00:00', '2026-01-02T00:30:00+00:00', 'fixture')
    clock=datetime(2026,1,3,tzinfo=timezone.utc)
    values, summary, invalid = evaluate_records(conn, clock)
    assert values[0].measured and not values[0].observed
    assert summary.denominators.measured == 1 and not invalid
    page=report(conn,clock)
    assert 'Audited observational outcomes' in page and 'insufficient_observations' in page
    assert 'Offset from expiry' in page
    audit_payload = report_json(conn, clock)
    assert audit_payload['rows'][0]['signal_id'] == 'a'


def test_legacy_unknown_and_audit_idempotent(conn):
    for _ in range(2):
        values, summary, invalid=evaluate_records(conn,datetime(2026,1,3,tzinfo=timezone.utc))
        assert values[0].status.value == 'not_eligible' and not invalid
    assert conn.execute('SELECT COUNT(*) FROM outcome_history_audit').fetchone()[0]==1


def test_observation_coverage_separates_trusted_untrusted_and_missing(conn):
    record_observation_attempt(
        conn, 'a', 101, '2026-01-01T01:00:00+00:00',
        '2026-01-01T01:01:00+00:00', 'fixture', True,
    )
    record_observation_attempt(
        conn, 'a', 101, None, '2026-01-01T02:00:00+00:00', None, False,
        'provider bar timestamp is stale',
    )
    coverage = observation_coverage(conn)
    assert coverage == {
        'trusted': 1,
        'untrusted': 1,
        'untrusted_by_reason': {'provider bar timestamp is stale': 1},
        'no_observations': 0,
    }


def test_untrusted_snapshots_not_used_and_conflicts_rejected(conn):
    record_point(conn,'a',110,None,None,None)
    assert conn.execute('SELECT COUNT(*) FROM outcome_points').fetchone()[0]==0
    record_entry(conn,row(),payload())
    record_entry(conn,row(),payload())
    with pytest.raises(ValueError):
        record_entry(conn,row(),{**payload(),'entry_price':101})
    record_point(conn,'a',101,'2026-01-01T01:00:00+00:00','2026-01-01T02:00:00+00:00','fixture')
    with pytest.raises(ValueError):
        record_point(conn,'a',102,'2026-01-01T01:00:00+00:00','2026-01-01T02:00:00+00:00','fixture')


def test_tracker_hooks_use_actual_sqlite_store(conn):
    class Tracker:
        def _conn(self): return conn
        def record_alert(self,fire,result): return 'a'
        def get_stats(self): return {'old':1}
        def build_performance_report(self): return 'old report'
    install_tracker_outcomes(Tracker)
    tracker=Tracker()
    assert tracker.record_alert({}, {'outcome_measurement':payload()})=='a'
    assert tracker.get_stats()['observational_denominators']['registered']==1
    assert 'Audited observational outcomes' in tracker.build_outcome_audit_report()


def test_invalid_record_visible(conn):
    record_entry(conn,row(),{**payload(),'entry_price_at':'2026-01-01T01:00:00+00:00'})
    values,summary,invalid=evaluate_records(conn,datetime(2026,1,3,tzinfo=timezone.utc))
    assert not values and len(invalid)==1
    assert 'Invalid records' in report(conn)


def test_migrations_preserve_transaction(conn):
    conn.execute("UPDATE alerts SET expires_at='pending'")
    ensure_schema(conn)
    conn.rollback()
    assert conn.execute('SELECT expires_at FROM alerts').fetchone()[0]=='legacy-expiry'


def test_resolution_mapping_keeps_same_fire_time_distinct(conn):
    fire = {
        'pro_symbol': 'NASDAQ:A',
        'fire_time': '2026-01-01T00:00:00+00:00',
        '_source': 'download',
    }
    record_resolution_mapping(conn, 'a', {**fire, 'resolution': '60'})
    record_resolution_mapping(conn, 'a', {**fire, 'resolution': '120'})
    rows = conn.execute(
        'SELECT resolution FROM outcome_resolution_map ORDER BY resolution'
    ).fetchall()
    assert [row[0] for row in rows] == ['120', '60']


def test_pipeline_persists_all_dispositions_and_policy(conn, monkeypatch):
    import sys
    from types import ModuleType
    module=ModuleType('database.fires_ledger')
    module.fire_key=lambda f:(f['pro_symbol'],f['fire_time'],f['resolution'])
    monkeypatch.setitem(sys.modules,'database.fires_ledger',module)
    fires=[dict(pro_symbol=s,fire_time=t,resolution='60') for s,t in [('A','1'),('A','2'),('B','1'),('C','1'),('D','1')]]
    persist_run(
        conn,
        fires,
        [fires[0], fires[2], fires[3]],
        [fires[0]],
        {('B','1','60'):'timeout'},
        'run1',
        filtered={('C','1','60'):'below market-cap floor'},
    )
    rows=conn.execute('SELECT pro_symbol,fire_time,state FROM fire_attempt_evidence ORDER BY pro_symbol,fire_time').fetchall()
    assert rows==[('A','1','completed'),('A','2','deduplicated'),('B','1','failed'),('C','1','filtered'),('D','1','considered_not_attempted')]


def test_production_installs_hooks_and_disables_legacy_sync():
    root=Path(__file__).resolve().parents[1]
    tracker=(root/'tradingview/alert_tracker.py').read_text()
    assert 'def build_outcome_audit_report' in tracker
    assert 'record_point(conn, alert_uuid' in tracker
    tree=ast.parse((root/'database/fires_ledger.py').read_text())
    f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='sync_scored_from_alert_tracker')
    env={};exec(compile(ast.Module(body=[f],type_ignores=[]),'legacy','exec'),env)
    assert env['sync_scored_from_alert_tracker']()==0
