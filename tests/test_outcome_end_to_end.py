import ast
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingview.alert_tracker import AlertTracker
from tradingview.outcome_store import evaluate_records

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tradingview"))


def _fire(source="webhook"):
    fire_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {
        "fire_id": 99,
        "pro_symbol": "NASDAQ:ACCEPTANCE",
        "symbol": "NASDAQ:ACCEPTANCE",
        "_source": source,
        "fire_time": fire_time,
        "bar_time": fire_time,
        "resolution": "60",
        "message": "BUY price: 100",
    }


def _result(measurement):
    return {
        "composite": 88.0,
        "actionable_at": datetime.now(timezone.utc).isoformat(),
        "fundamentals": {"current_price": 100.0},
        "scores": {"l1": {"total": 80}, "l2": {"total": 80}, "l3": {"total": 80}, "l4": {"total": 80}},
        "outcome_measurement": {
            **measurement,
            "horizon_hours": 2,
            "horizon_tolerance_hours": 0.5,
        },
    }


def _measurement(fire):
    # Load only the production helper by AST: importing signal_stack pulls in
    # the moomoo SDK, which opens a log file under the user's home directory.
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "tradingview/signal_stack.py").read_text())
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_fire_outcome_measurement"
    )
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "signal_stack", "exec"), namespace)
    return namespace["_fire_outcome_measurement"](
        fire, datetime.now(timezone.utc).isoformat()
    )


def test_real_tracker_end_to_end_with_trusted_observation(tmp_path, monkeypatch):
    tracker_module = importlib.import_module("tradingview.alert_tracker")
    tracker = AlertTracker(tmp_path / "tracker.db")
    fire = _fire()
    measurement = _measurement(fire)
    assert measurement["provenance_status"] == "valid"
    assert tracker.record_alert(fire, _result(measurement))

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(tracker_module, "_YF_OK", True)
    monkeypatch.setattr(tracker_module, "_fetch_observations", lambda tickers: {
        ticker: {
            "price": 110.0,
            "observed_at": now,
            "available_at": now,
            "price_source": "fixture",
            "trusted": True,
        }
        for ticker in tickers
    })
    assert tracker.update_prices()["updated"] == 1

    with tracker._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcome_entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outcome_points").fetchone()[0] == 1
    stats = tracker.get_stats()
    assert stats["observational_denominators"]["observed"] == 1
    with tracker._conn() as conn:
        values, _, invalid = evaluate_records(
            conn, as_of=now + timedelta(hours=2)
        )
    assert values[0].status.value == "observed_target_first" and not invalid
    assert "Audited observational outcomes" in tracker.build_outcome_audit_report()


def test_real_tracker_untrusted_timestamp_is_missing_coverage(tmp_path, monkeypatch):
    tracker_module = importlib.import_module("tradingview.alert_tracker")
    tracker = AlertTracker(tmp_path / "tracker.db")
    fire = _fire()
    measurement = _measurement(fire)
    assert tracker.record_alert(fire, _result(measurement))

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(tracker_module, "_YF_OK", True)
    monkeypatch.setattr(tracker_module, "_fetch_observations", lambda tickers: {
        ticker: {
            "price": 110.0,
            "observed_at": None,
            "available_at": now,
            "price_source": None,
            "trusted": False,
            "reason": "fixture has no provider timestamp",
        }
        for ticker in tickers
    })
    assert tracker.update_prices()["updated"] == 1
    with tracker._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcome_points").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
    stats = tracker.get_stats()
    assert stats["observational_denominators"]["observed"] == 0
    assert stats["observational_denominators"]["measured"] == 0