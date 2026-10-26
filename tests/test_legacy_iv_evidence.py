"""Only actual archived contract-to-contract date pairs; never implied real fills."""
import sqlite3
from datetime import date,timedelta

from tradingview.options.legacy_iv_evidence import evaluate


def build(path,*,future=False):
    d=date(2026,5,1)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE options_scan_results(tv_symbol TEXT,option_type TEXT,"
                  "strike REAL,expiry TEXT,created_at TEXT,iv REAL,spot_price REAL)")
        for symbol_num in range(10):
            for idx in range(11):
                day=(d+timedelta(days=idx)).isoformat()
                c.execute("INSERT INTO options_scan_results VALUES (?,?,?,?,?,?,?)",
                          ("NYSE:STOCK"+str(symbol_num),"CALL",100.0,
                           "2026-06-30",day+" 14:30:00",20+idx*.14+symbol_num*.01,
                           100+idx*.1+symbol_num*.1))
        if future:
            for symbol_num in range(10):
                c.execute("INSERT INTO options_scan_results VALUES (?,?,?,?,?,?,?)",
                          ("NYSE:STOCK"+str(symbol_num),"CALL",100.,
                           "2026-06-30","2026-05-25 13:00:00",300.,150.))
    return path


def test_option_iv_changes_from_existing_unmodified_sqlite(tmp_path):
    p=build(tmp_path/"existing.db")
    b=p.read_bytes()
    data=evaluate(p,asof_date="2026-05-13",min_pairs=10)
    assert data["status"]=="REFERENCE_SHORT_HORIZON_IV_CHANGE_ONLY"
    assert data["usable_pairs"]>=80
    assert data["heldout_pairs"]>10
    assert data["training_last_receipt_date"]<data["heldout_first_receipt_date"]
    assert data["option_iv_forecast_calibrated"] is False
    assert data["21_session_option_iv_surface_available"] is False
    assert data["independent_option_side_event_times"] is False
    assert p.read_bytes()==b


def test_future_archived_rows_not_included_in_past_audit(tmp_path):
    base=build(tmp_path/"a.db")
    extra=build(tmp_path/"b.db",future=True)
    a=evaluate(base,asof_date="2026-05-13",min_pairs=10)
    b=evaluate(extra,asof_date="2026-05-13",min_pairs=10)
    assert a==b


def test_missing_database_reports_blocked_not_fabricated(tmp_path):
    x=evaluate(tmp_path/"missing.db",asof_date="2026-10-26")
    assert x["status"]=="ARCHIVE_UNAVAILABLE"
    assert x["option_iv_forecast_calibrated"] is False
