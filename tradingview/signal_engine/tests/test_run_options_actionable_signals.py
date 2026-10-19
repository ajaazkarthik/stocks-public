"""
Tests for run_options.py's _load_actionable_signals() (C2 critical fix,
2026-07-06 option-14 deep-dive fix pass).

BEFORE the fix: this function only read actionable_log.json (written
exclusively by webhook_server.py, a separate live-trading process) and
never read reports/actionable_report.json (signal_stack.py's real, freshly
L1-L4-scored output). The auto-chain from main.py option 14
(signal_stack.py -> run_options.py --from-actionable) was silently
disconnected from all scoring that had just happened.

AFTER the fix: reports/actionable_report.json is read FIRST (real scores),
falling back to actionable_log.json only when no fresh report exists.

All tests are network-free; uses tmp_path fixtures for the JSON files so no
real report/log data is touched.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
OPTIONS_DIR = TV_DIR / "options"
for _p in (TV_DIR, OPTIONS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_options as ro


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect all path constants to a tmp_path sandbox so tests never touch
    real reports/actionable_report.json or actionable_log.json."""
    monkeypatch.setattr(ro, "ACTIONABLE_REPORT_JSON", tmp_path / "actionable_report.json")
    monkeypatch.setattr(ro, "ACTIONABLE_LOG", tmp_path / "actionable_log_tv.json")
    monkeypatch.setattr(ro, "ACTIONABLE_LOG_ALT", tmp_path / "actionable_log_reports.json")
    monkeypatch.setattr(ro, "FIRES_RAW", tmp_path / "fires_raw.json")
    yield tmp_path


def _write_actionable_report(path: Path, signals: list[dict], generated_at="2026-07-06T04:51:49Z"):
    path.write_text(json.dumps({
        "generated_at": generated_at,
        "source": "download",
        "n_total": len(signals),
        "n_actionable": len(signals),
        "is_watch": False,
        "threshold": 50.0,
        "actionable_signals": signals,
    }), encoding="utf-8")


def _write_strict_report(path: Path, run_id="run-1", signals=None, **overrides):
    signals = signals if signals is not None else [
        {"symbol": "NASDAQ:NVDA", "score": 78.5},
    ]
    payload = {
        "status": "complete",
        "generated_at": "2026-09-30T12:00:00Z",
        "run_id": run_id,
        "n_actionable": len(signals),
        "is_watch": False,
        "actionable_signals": signals,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestPrimarySourceIsActionableReportJson:

    def test_reads_real_composite_scores_from_actionable_report(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5, "l1": 7.0, "l2": 8.0, "l3": 7.5, "l4": 6.0},
            {"symbol": "NSE:TITAN", "score": 65.2, "l1": 6.0, "l2": 7.0, "l3": 6.5, "l4": 5.5},
        ])

        signals = ro._load_actionable_signals(limit=20)

        by_symbol = {s["tv_symbol"]: s for s in signals}
        assert by_symbol["NASDAQ:NVDA"]["composite"] == 78.5
        assert by_symbol["NSE:TITAN"]["composite"] == 65.2

    def test_does_not_use_hardcoded_55_default_when_report_exists(self, tmp_path):
        """Regression guard for the exact C2 bug: real scores must never be
        silently replaced by _augment_from_fires()'s composite=55.0 default."""
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 91.0, "l1": 9.0, "l2": 9.0, "l3": 9.0, "l4": 9.0},
        ])

        signals = ro._load_actionable_signals(limit=20)

        nvda = next(s for s in signals if s["tv_symbol"] == "NASDAQ:NVDA")
        assert nvda["composite"] == 91.0
        assert nvda["composite"] != 55.0

    def test_source_tag_marks_signal_stack_origin(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5, "l1": 7.0, "l2": 8.0, "l3": 7.5, "l4": 6.0},
        ])
        signals = ro._load_actionable_signals(limit=20)
        assert signals[0]["_source"] == "signal_stack"

    def test_l1_l2_l3_l4_carried_through(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5, "l1": 7.1, "l2": 8.2, "l3": 7.3, "l4": 6.4},
        ])
        signals = ro._load_actionable_signals(limit=20)
        s = signals[0]
        assert (s["l1"], s["l2"], s["l3"], s["l4"]) == (7.1, 8.2, 7.3, 6.4)

    def test_highest_scores_are_kept_when_limit_applies(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:LOW", "score": 51.0},
            {"symbol": "NASDAQ:HIGH", "score": 82.0},
            {"symbol": "NASDAQ:MID", "score": 65.0},
        ])

        signals = ro._load_actionable_signals(limit=2)

        assert [s["tv_symbol"] for s in signals] == ["NASDAQ:HIGH", "NASDAQ:MID"]

    def test_ineligible_nse_symbol_is_removed_before_limit(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NSE:NOTINFO", "score": 95.0},
            {"symbol": "NSE:RELIANCE", "score": 80.0},
            {"symbol": "NASDAQ:NVDA", "score": 75.0},
        ])

        signals = ro._load_actionable_signals(limit=2)

        assert [s["tv_symbol"] for s in signals] == ["NSE:RELIANCE", "NASDAQ:NVDA"]

    def test_fresh_raw_fires_augment_the_actionable_report(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5},
        ])
        ro.FIRES_RAW.write_text(json.dumps({"all": [{
            "pro_symbol": "NASDAQ:UNSCORED",
            "fire_time": "2099-01-01T00:00:00Z",
        }]}), encoding="utf-8")

        signals = ro._load_actionable_signals(limit=20)

        assert [s["tv_symbol"] for s in signals] == ["NASDAQ:NVDA", "NASDAQ:UNSCORED"]


class TestFallbackToActionableLog:

    def test_falls_back_to_actionable_log_when_no_report(self, tmp_path):
        ro.ACTIONABLE_LOG.write_text(json.dumps([
            {"tv_symbol": "NYSE:UBER", "composite": 62.0},
        ]), encoding="utf-8")

        signals = ro._load_actionable_signals(limit=20)

        assert any(s.get("tv_symbol") == "NYSE:UBER" for s in signals)

    def test_report_takes_priority_over_log_when_both_exist(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5, "l1": 7.0, "l2": 8.0, "l3": 7.5, "l4": 6.0},
        ])
        ro.ACTIONABLE_LOG.write_text(json.dumps([
            {"tv_symbol": "NYSE:UBER", "composite": 62.0},
        ]), encoding="utf-8")

        signals = ro._load_actionable_signals(limit=20)

        symbols = {s.get("tv_symbol") for s in signals}
        assert "NASDAQ:NVDA" in symbols
        assert "NYSE:UBER" not in symbols   # log ignored once report exists

    def test_empty_report_signals_list_falls_back_to_log(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [])
        ro.ACTIONABLE_LOG.write_text(json.dumps([
            {"tv_symbol": "NYSE:UBER", "composite": 62.0},
        ]), encoding="utf-8")

        signals = ro._load_actionable_signals(limit=20)

        assert any(s.get("tv_symbol") == "NYSE:UBER" for s in signals)


class TestNoSourcesAvailable:

    def test_returns_empty_list_when_nothing_exists(self, tmp_path):
        signals = ro._load_actionable_signals(limit=20)
        assert signals == []

    def test_corrupt_report_json_does_not_crash(self, tmp_path):
        ro.ACTIONABLE_REPORT_JSON.write_text("{not valid json", encoding="utf-8")
        signals = ro._load_actionable_signals(limit=20)
        assert signals == []

    def test_watch_report_does_not_feed_options(self, tmp_path):
        ro.ACTIONABLE_REPORT_JSON.write_text(json.dumps({
            "is_watch": True,
            "n_actionable": 0,
            "actionable_signals": [],
            "watch_signals": [{"symbol": "NASDAQ:NVDA", "score": 70.0}],
        }), encoding="utf-8")

        signals = ro._load_actionable_signals(limit=20)

        assert signals == []


class TestScoreConsumptionCompatibility:
    """The existing main()/_run_analysis() code reads
    s.get('composite') or s.get('score') — verify our new dicts satisfy that."""

    def test_composite_key_readable_via_existing_fallback_chain(self, tmp_path):
        _write_actionable_report(ro.ACTIONABLE_REPORT_JSON, [
            {"symbol": "NASDAQ:NVDA", "score": 78.5, "l1": 7.0, "l2": 8.0, "l3": 7.5, "l4": 6.0},
        ])
        signals = ro._load_actionable_signals(limit=20)
        s = signals[0]
        score = float(s.get("composite") or s.get("score") or 0)
        assert score == 78.5


class TestStrictActionableHandoff:
    def test_valid_current_report_loads_without_fallbacks(self, tmp_path, mocker):
        _write_strict_report(ro.ACTIONABLE_REPORT_JSON)
        fallback = mocker.patch.object(ro, "_augment_from_fires")

        signals = ro._load_actionable_signals(
            actionable_file=ro.ACTIONABLE_REPORT_JSON,
            strict=True,
            expected_run_id="run-1",
        )

        assert signals[0]["tv_symbol"] == "NASDAQ:NVDA"
        fallback.assert_not_called()

    @pytest.mark.parametrize("payload", [
        None,
        "{bad",
        {"status": "complete", "generated_at": "2026-09-30 12:00:00",
         "run_id": "wrong", "n_actionable": 1, "is_watch": False,
         "actionable_signals": [{"symbol": "NASDAQ:NVDA", "score": 78.5}]},
        {"status": "running", "generated_at": "2026-09-30 12:00:00",
         "run_id": "run-1", "n_actionable": 1, "is_watch": False,
         "actionable_signals": [{"symbol": "NASDAQ:NVDA", "score": 78.5}]},
        {"status": "complete", "generated_at": "2026-09-30 12:00:00",
         "run_id": "run-1", "n_actionable": 1, "is_watch": True,
         "actionable_signals": [{"symbol": "NASDAQ:NVDA", "score": 78.5}]},
    ])
    def test_invalid_strict_report_never_uses_fallbacks(self, tmp_path, mocker, payload):
        if payload is None:
            ro.ACTIONABLE_REPORT_JSON.write_text("", encoding="utf-8")
        elif isinstance(payload, str):
            ro.ACTIONABLE_REPORT_JSON.write_text(payload, encoding="utf-8")
        else:
            ro.ACTIONABLE_REPORT_JSON.write_text(json.dumps(payload), encoding="utf-8")
        ro.ACTIONABLE_LOG.write_text(
            json.dumps([{"tv_symbol": "NASDAQ:FALLBACK", "composite": 70}]),
            encoding="utf-8",
        )
        fallback = mocker.patch.object(ro, "_augment_from_fires")

        with pytest.raises(ro.StrictActionableReportError):
            ro._load_actionable_signals(
                actionable_file=ro.ACTIONABLE_REPORT_JSON,
                strict=True,
                expected_run_id="run-1",
            )
        fallback.assert_not_called()

    def test_valid_empty_report_is_successful_no_candidates(self):
        _write_strict_report(ro.ACTIONABLE_REPORT_JSON, signals=[])
        assert ro._load_actionable_signals(
            actionable_file=ro.ACTIONABLE_REPORT_JSON,
            strict=True,
            expected_run_id="run-1",
        ) == []

    def test_invalid_strict_loader_is_distinct_from_valid_empty(self):
        ro.ACTIONABLE_REPORT_JSON.write_text("{bad", encoding="utf-8")
        with pytest.raises(ro.StrictActionableReportError):
            ro._load_actionable_signals(
                actionable_file=ro.ACTIONABLE_REPORT_JSON,
                strict=True,
                expected_run_id="run-1",
            )

        _write_strict_report(ro.ACTIONABLE_REPORT_JSON, signals=[])
        assert ro._load_actionable_signals(
            actionable_file=ro.ACTIONABLE_REPORT_JSON,
            strict=True,
            expected_run_id="run-1",
        ) == []

    def test_strict_cli_returns_distinct_nonzero_for_invalid_report(self, monkeypatch):
        ro.ACTIONABLE_REPORT_JSON.write_text("{bad", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "run_options.py", "--from-actionable",
            "--actionable-file", str(ro.ACTIONABLE_REPORT_JSON),
            "--expected-run-id", "run-1", "--strict-actionable",
        ])

        assert ro.main() == 2

    def test_timestamp_policy_requires_explicit_utc(self):
        _write_strict_report(ro.ACTIONABLE_REPORT_JSON)
        report, reason = ro._read_strict_actionable_report(
            ro.ACTIONABLE_REPORT_JSON, "run-1"
        )
        assert report is not None and reason == "valid"

        _write_strict_report(
            ro.ACTIONABLE_REPORT_JSON,
            generated_at="2026-09-30 12:00:00",
        )
        report, reason = ro._read_strict_actionable_report(
            ro.ACTIONABLE_REPORT_JSON, "run-1"
        )
        assert report is None
        assert reason == "generated_at must be an explicit UTC timestamp"

    def test_report_replacement_is_rejected_on_child_revalidation(self):
        _write_strict_report(ro.ACTIONABLE_REPORT_JSON, run_id="run-1")
        report, reason = ro._read_strict_actionable_report(
            ro.ACTIONABLE_REPORT_JSON, "run-1"
        )
        assert report is not None and reason == "valid"

        _write_strict_report(ro.ACTIONABLE_REPORT_JSON, run_id="replacement")
        with pytest.raises(ro.StrictActionableReportError):
            ro._load_actionable_signals(
                actionable_file=ro.ACTIONABLE_REPORT_JSON,
                strict=True,
                expected_run_id="run-1",
            )

    def test_manual_mode_retains_log_fallback(self):
        ro.ACTIONABLE_LOG.write_text(
            json.dumps([{"tv_symbol": "NASDAQ:MANUAL", "composite": 70}]),
            encoding="utf-8",
        )
        signals = ro._load_actionable_signals(limit=20)
        assert signals[0]["tv_symbol"] == "NASDAQ:MANUAL"


class TestOptionsMarketHours:

    def test_nse_open_during_regular_session(self):
        now = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
        assert ro._market_is_open("NSE", now)
        assert not ro._market_is_open("NASDAQ", now)

    def test_us_open_during_regular_session(self):
        now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)
        assert ro._market_is_open("NASDAQ", now)
        assert not ro._market_is_open("NSE", now)

    def test_all_markets_closed_on_weekend(self):
        now = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)
        assert not ro._market_is_open("NASDAQ", now)
        assert not ro._market_is_open("NSE", now)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
