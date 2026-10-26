"""Ledger is separate, source-read-only, and never calls notifier/broker."""
import json
from pathlib import Path
import sqlite3

import pytest

from tradingview.options import edge_research_ledger as ledger
from tradingview.options import paper_scanner as frozen
from tests.test_option_paper_scanner import observation,NOW


def sample_source(path,*,with_observation=True):
    with sqlite3.connect(path) as c:
        c.executescript(frozen.SCHEMA)
        c.execute("INSERT INTO option_paper_runs (observed_at,status,detail_json) "
                  "VALUES (?,?,?)",(NOW.isoformat(),"COMPLETE","{}"))
        if with_observation:
            obs=observation()
            c.execute("INSERT INTO option_paper_observations (id,symbol,observed_at,payload_json) "
                      "VALUES (?,?,?,?)",("obs1",obs["symbol"],
                                         obs["observed_at"],json.dumps(obs)))
    return path


def test_empty_archive_fails_closed_no_profitable_claim(tmp_path):
    source=sample_source(tmp_path/"source.db",with_observation=False)
    result=ledger.audit(source)
    assert result["source_run_count"]==1
    assert result["source_trade_records"]==0
    assert result["archive_observations"]==0
    assert result["diagnostic_decisions"]==0
    assert result["forward_qualified_sessions"]==0
    assert result["funded_portfolio_pnl"]=="NOT_MEASURED"
    assert result["records"]==[]


def test_unchanged_frozen_candidate_and_separate_research_cursor(tmp_path):
    source=sample_source(tmp_path/"source.db")
    before=source.read_bytes()
    result=ledger.audit(source)
    assert result["diagnostic_decisions"]==1
    row=result["records"][0]
    assert row["baseline_status"]=="CANDIDATE"
    assert row["challenger_status"]=="MISSING_POINT_IN_TIME_JOINT_FORECAST"
    assert row["frozen_candidates"]==1
    assert row["conservative_model_only"]==0
    assert row["observation_class"]=="ARCHIVE_DIAGNOSTIC"
    assert row["performance"]=="NOT_MEASURED"
    assert row["side_quote_verification"]=="UNVERIFIED"
    assert "quotes" not in row["decision_json"]
    target=tmp_path/"separate"/"research.db"
    wrote=ledger.store_private(target,result,source=source)
    assert wrote["research_rows_total"]==1
    assert target.stat().st_mode&0o777==0o600
    assert source.read_bytes()==before
    # Same observation, same version, deterministic idempotent record.
    again=ledger.store_private(target,result,source=source)
    assert again["research_rows_total"]==1


def test_original_paper_db_cannot_be_reused_as_output(tmp_path):
    src=sample_source(tmp_path/"source.db")
    report=ledger.audit(src)
    with pytest.raises(ValueError):
        ledger.store_private(src,report,source=src)
    assert src.exists()


def test_archive_duplicate_quote_does_not_become_prospective(tmp_path):
    src=sample_source(tmp_path/"source.db")
    with sqlite3.connect(src) as c:
        obs=observation()
        c.execute("INSERT INTO option_paper_observations (id,symbol,observed_at,payload_json) "
                  "VALUES (?,?,?,?)",("obs2",obs["symbol"],obs["observed_at"],json.dumps(obs)))
    rows=ledger.audit(src)
    assert rows["diagnostic_decisions"]==2
    assert rows["forward_qualified_sessions"]==0
    out=tmp_path/"research.db"
    stored=ledger.store_private(out,rows,source=src)
    # Same payload hashes, so the duplicate source cannot inflate results.
    assert stored["research_rows_total"]==1


def test_malformed_identity_is_counted_not_silently_repaired(tmp_path):
    src=sample_source(tmp_path/"source.db",with_observation=False)
    with sqlite3.connect(src) as c:
        c.execute("INSERT INTO option_paper_observations (id,symbol,observed_at,payload_json) "
                  "VALUES (?,?,?,?)",("wrong","AMEX:SPY",NOW.isoformat(),
                                         json.dumps(dict(symbol="NASDAQ:QQQ",observed_at=NOW.isoformat()))))
    report=ledger.audit(src)
    assert report["diagnostic_decisions"]==0
    assert report["archive_errors"]=={"ValueError":1}


def test_owner_only_requirement(tmp_path):
    src=sample_source(tmp_path/"source.db")
    dst=tmp_path/"world-readable.db"
    dst.write_bytes(b"")
    dst.chmod(0o644)
    with pytest.raises(PermissionError):
        ledger.store_private(dst,ledger.audit(src),source=src)
