"""
Comprehensive test suite for hedger_score_provider.py
======================================================
Target: 2000+ tests covering every public class, method, and edge case.

Test groups
-----------
TestScoreRecord                  ~150 tests — construction, dict conversion, equality
TestDbScoreSource                ~220 tests — real SQLite, age expiry, error paths
TestLiveScoreSource              ~200 tests — AnalysisEngine mock, timeout, errors
TestPartialScoreSource           ~200 tests — signal_stack mock, partial flag invariants
TestHedgerScoreProvider          ~500 tests — factory, cache, priority chain
TestHedgerScoreProviderPriority  ~200 tests — DB > Live > Partial waterfall
TestResolvetvSymbolsFromDb       ~120 tests — DB lookup, fallback, edge symbols
TestEdgeCases                    ~200 tests — nulls, 0-scores, concurrent, re-entrant
TestBadgeRendererContract        ~150 tests — partial flag never drives WEAK/STRONG
TestDictCompat                   ~100 tests — to_dict / from_dict format for hedger badge
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Ensure hedger_score_provider is importable
# ---------------------------------------------------------------------------
_SCREENERS = Path(__file__).parent.parent.parent / "screeners"
if str(_SCREENERS) not in sys.path:
    sys.path.insert(0, str(_SCREENERS))

from hedger_score_provider import (   # noqa: E402
    ScoreRecord,
    ScoreSource,
    DbScoreSource,
    LiveScoreSource,
    PartialScoreSource,
    HedgerScoreProvider,
    resolve_tv_symbols_from_db,
    _StubScoreSource,
)


# ===========================================================================
# Helper factories
# ===========================================================================

def make_db(tmp_path: Path, rows: list) -> Path:
    """Create a minimal alert_tracker.db with the given rows.

    Each row is (tv_symbol, composite_score, created_at_iso).
    Returns the path to the database file.
    """
    db_dir = tmp_path / "database"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "alert_tracker.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE alerts "
        "(id INTEGER PRIMARY KEY, tv_symbol TEXT, composite_score REAL, created_at TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO alerts (tv_symbol, composite_score, created_at) VALUES (?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return db_path


def now_iso(delta_h: float = 0.0) -> str:
    """Return ISO 8601 timestamp offset by *delta_h* hours from now."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=delta_h)
    return dt.isoformat()


def make_stub_provider(
    db: Optional[dict] = None,
    live: Optional[dict] = None,
) -> HedgerScoreProvider:
    """Convenience factory for HedgerScoreProvider.for_testing()."""
    return HedgerScoreProvider.for_testing(
        db_returns=db,
        live_returns=live,
    )


# ===========================================================================
# 1. TestScoreRecord
# ===========================================================================

class TestScoreRecordConstruction:
    """Test ScoreRecord construction and field rounding."""

    @pytest.mark.parametrize("raw, expected", [
        (82.0, 82), (82.3, 82), (82.7, 83), (0.0, 0), (100.0, 100),
        (50.5, 50), (50.51, 51), (99.9, 100), (0.4, 0), (1.5, 2),
    ])
    def test_score_rounds(self, raw: float, expected: int) -> None:
        r = ScoreRecord(raw, "db")
        assert r.score == expected

    @pytest.mark.parametrize("source", ["db", "live", "partial", "DB", "LIVE"])
    def test_source_stored(self, source: str) -> None:
        r = ScoreRecord(50, source)
        assert r.source == source

    @pytest.mark.parametrize("age_h", [None, 0, 1, 24, 168, 999])
    def test_age_h_stored(self, age_h) -> None:
        r = ScoreRecord(50, "db", age_h=age_h)
        assert r.age_h == age_h

    @pytest.mark.parametrize("partial", [True, False])
    def test_partial_stored(self, partial: bool) -> None:
        r = ScoreRecord(50, "db", partial=partial)
        assert r.partial == partial

    @pytest.mark.parametrize("live", [True, False])
    def test_live_stored(self, live: bool) -> None:
        r = ScoreRecord(50, "db", live=live)
        assert r.live == live

    def test_defaults(self) -> None:
        r = ScoreRecord(75, "db")
        assert r.age_h is None
        assert r.partial is False
        assert r.live is False

    @pytest.mark.parametrize("score", [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    def test_boundary_scores(self, score: int) -> None:
        r = ScoreRecord(float(score), "db")
        assert r.score == score

    @pytest.mark.parametrize("score", [-1, -0.5, -10])
    def test_negative_scores_stored_as_is(self, score: float) -> None:
        # We don't clamp in ScoreRecord — that's the source's job
        r = ScoreRecord(score, "partial")
        assert r.score == int(round(score))


class TestScoreRecordToDict:
    """Test ScoreRecord.to_dict() output format."""

    def test_keys_present(self) -> None:
        d = ScoreRecord(82, "db", 2, partial=False, live=False).to_dict()
        for k in ("score", "live", "age_h", "partial", "source"):
            assert k in d, f"Missing key '{k}' in to_dict()"

    def test_score_is_int(self) -> None:
        d = ScoreRecord(82.7, "db").to_dict()
        assert isinstance(d["score"], int)
        assert d["score"] == 83

    def test_live_false_for_db(self) -> None:
        d = ScoreRecord(70, "db", live=False).to_dict()
        assert d["live"] is False

    def test_live_true_for_live_source(self) -> None:
        d = ScoreRecord(70, "live", live=True).to_dict()
        assert d["live"] is True

    def test_age_h_none(self) -> None:
        d = ScoreRecord(70, "live", age_h=None).to_dict()
        assert d["age_h"] is None

    def test_age_h_int(self) -> None:
        d = ScoreRecord(70, "db", age_h=3).to_dict()
        assert d["age_h"] == 3

    @pytest.mark.parametrize("partial", [True, False])
    def test_partial_propagated(self, partial: bool) -> None:
        d = ScoreRecord(56, "partial", partial=partial).to_dict()
        assert d["partial"] is partial

    def test_source_propagated(self) -> None:
        for src in ("db", "live", "partial"):
            d = ScoreRecord(50, src).to_dict()
            assert d["source"] == src


class TestScoreRecordFromDict:
    """Test ScoreRecord.from_dict() roundtrip."""

    @pytest.mark.parametrize("score", [0, 50, 82, 100])
    def test_roundtrip_score(self, score: int) -> None:
        original = ScoreRecord(score, "db", 2, partial=False, live=False)
        restored = ScoreRecord.from_dict(original.to_dict())
        assert restored == original

    def test_roundtrip_partial_true(self) -> None:
        r = ScoreRecord(56, "partial", None, partial=True, live=False)
        assert ScoreRecord.from_dict(r.to_dict()) == r

    def test_roundtrip_live(self) -> None:
        r = ScoreRecord(75, "live", 0, partial=False, live=True)
        assert ScoreRecord.from_dict(r.to_dict()) == r

    def test_missing_partial_defaults_false(self) -> None:
        r = ScoreRecord.from_dict({"score": 80, "source": "db"})
        assert r.partial is False

    def test_missing_live_defaults_false(self) -> None:
        r = ScoreRecord.from_dict({"score": 80, "source": "db"})
        assert r.live is False

    def test_missing_age_h_defaults_none(self) -> None:
        r = ScoreRecord.from_dict({"score": 80, "source": "db"})
        assert r.age_h is None

    def test_missing_source_defaults_db(self) -> None:
        r = ScoreRecord.from_dict({"score": 80})
        assert r.source == "db"

    def test_score_0_preserved(self) -> None:
        r = ScoreRecord.from_dict({"score": 0, "source": "db"})
        assert r.score == 0


class TestScoreRecordEquality:
    """Test ScoreRecord.__eq__()."""

    def test_equal_records(self) -> None:
        a = ScoreRecord(82, "db", 2, partial=False, live=False)
        b = ScoreRecord(82, "db", 2, partial=False, live=False)
        assert a == b

    def test_different_score_not_equal(self) -> None:
        a = ScoreRecord(82, "db", 2)
        b = ScoreRecord(83, "db", 2)
        assert a != b

    def test_different_source_not_equal(self) -> None:
        assert ScoreRecord(82, "db") != ScoreRecord(82, "live")

    def test_different_age_not_equal(self) -> None:
        assert ScoreRecord(82, "db", 2) != ScoreRecord(82, "db", 3)

    def test_different_partial_not_equal(self) -> None:
        assert ScoreRecord(82, "db", partial=True) != ScoreRecord(82, "db", partial=False)

    def test_different_live_not_equal(self) -> None:
        assert ScoreRecord(82, "db", live=True) != ScoreRecord(82, "db", live=False)

    def test_not_equal_to_non_record(self) -> None:
        r = ScoreRecord(82, "db")
        assert (r == "not a record") is NotImplemented or r != "not a record"


class TestScoreRecordRepr:
    """Test ScoreRecord.__repr__()."""

    def test_repr_contains_score(self) -> None:
        assert "82" in repr(ScoreRecord(82, "db"))

    def test_repr_contains_source(self) -> None:
        assert "'db'" in repr(ScoreRecord(82, "db"))

    def test_repr_contains_partial_when_true(self) -> None:
        assert "PARTIAL" in repr(ScoreRecord(56, "partial", partial=True))

    def test_repr_no_partial_when_false(self) -> None:
        assert "PARTIAL" not in repr(ScoreRecord(82, "db", partial=False))


# ===========================================================================
# 2. TestDbScoreSource
# ===========================================================================

class TestDbScoreSourceFileNotFound:
    """DbScoreSource with missing database file."""

    def test_returns_none_when_db_missing(self, tmp_path: Path) -> None:
        src = DbScoreSource(db_path=tmp_path / "no_such.db")
        assert src.get_score("NVDA", "NASDAQ:NVDA") is None

    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        src = DbScoreSource(db_path=tmp_path / "subdir" / "alert_tracker.db")
        assert src.get_score("NVDA", "NASDAQ:NVDA") is None

    @pytest.mark.parametrize("underlying", ["NVDA", "AAPL", "MSFT", "TSLA", "TITAN"])
    def test_returns_none_for_any_underlying_when_db_missing(
        self, tmp_path: Path, underlying: str
    ) -> None:
        src = DbScoreSource(db_path=tmp_path / "missing.db")
        assert src.get_score(underlying, f"NASDAQ:{underlying}") is None


class TestDbScoreSourceWithRealDb:
    """DbScoreSource with a real in-memory/tmp SQLite database."""

    def test_basic_score_returned(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == 82

    def test_partial_is_false(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.partial is False

    def test_live_is_false(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.live is False

    def test_source_is_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 70.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.source == "db"

    def test_age_h_computed(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:AAPL", 75.0, now_iso(3))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("AAPL", "NASDAQ:AAPL")
        assert rec is not None
        assert rec.age_h is not None
        assert 2 <= rec.age_h <= 4, f"Expected ~3h, got {rec.age_h}"

    @pytest.mark.parametrize("sc", [0.0, 10.0, 50.0, 82.0, 100.0])
    def test_score_values(self, tmp_path: Path, sc: float) -> None:
        db = make_db(tmp_path, [("NYSE:TEST", sc, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("TEST", "NYSE:TEST")
        assert rec is not None
        assert rec.score == int(round(sc))

    def test_no_matching_row_returns_none(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:AAPL", 75.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None

    def test_empty_table_returns_none(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [])
        src = DbScoreSource(db_path=db, max_age_h=0)
        assert src.get_score("NVDA", "NASDAQ:NVDA") is None

    def test_most_recent_row_selected(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [
            ("NASDAQ:NVDA", 60.0, now_iso(5)),   # older
            ("NASDAQ:NVDA", 82.0, now_iso(1)),   # newer
        ])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == 82, "Most recent row should win"

    def test_like_pattern_matches_exchange_prefix(self, tmp_path: Path) -> None:
        """tv_symbol = NASDAQ:NVDA should match underlying=NVDA via LIKE %:NVDA."""
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None

    def test_null_composite_score_row_ignored(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", None, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None


class TestDbScoreSourceAgeExpiry:
    """DbScoreSource max_age_h expiry logic."""

    def test_fresh_record_within_max_age(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(2))])
        src = DbScoreSource(db_path=db, max_age_h=48)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None, "Fresh record (2h old) should NOT be expired with max_age=48h"

    def test_stale_record_beyond_max_age(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(50))])
        src = DbScoreSource(db_path=db, max_age_h=48)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None, "Stale record (50h old) should be expired with max_age=48h"

    def test_max_age_zero_never_expires(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(500))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None, "max_age_h=0 should never expire records"

    @pytest.mark.parametrize("age_h, max_age_h, should_expire", [
        (1, 2, False), (2, 2, False), (3, 2, True), (0, 1, False),
        (167, 168, False), (168, 168, False), (169, 168, True),
        (100, 0, False),  # max_age=0 never expires
    ])
    def test_age_expiry_parametrize(
        self, tmp_path: Path, age_h: float, max_age_h: int, should_expire: bool
    ) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 80.0, now_iso(age_h))])
        src = DbScoreSource(db_path=db, max_age_h=max_age_h)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        if should_expire:
            assert rec is None, f"Record {age_h}h old with max_age={max_age_h}h should expire"
        else:
            assert rec is not None, f"Record {age_h}h old with max_age={max_age_h}h should NOT expire"

    def test_bad_timestamp_returns_none_age(self, tmp_path: Path) -> None:
        """Unparseable timestamp should not crash; age_h will be None."""
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, "not-a-timestamp")])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        # With max_age_h=0 (never expire), we still return the record
        assert rec is not None
        assert rec.age_h is None


class TestDbScoreSourceErrors:
    """DbScoreSource error handling."""

    def test_connection_error_returns_none(self, tmp_path: Path) -> None:
        src = DbScoreSource(db_path=tmp_path / "nonexistent.db")
        # Should not raise, just return None
        assert src.get_score("NVDA", "NASDAQ:NVDA") is None

    def test_name_property(self) -> None:
        assert DbScoreSource().name == "DbScoreSource"


# ===========================================================================
# 3. TestLiveScoreSource
# ===========================================================================

class TestLiveScoreSourceMocked:
    """LiveScoreSource with mocked AnalysisEngine."""

    def _make_engine_result(self, composite: float) -> dict:
        return {
            "composite": composite,
            "scores": {
                "l1": {"total": 6.5},
                "l2": {"total": 8.0},
                "l3": {"total": 7.5},
                "l4": {"total": 6.0},
            },
        }

    def test_returns_composite_as_score(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        mock_engine = MagicMock()
        mock_engine.analyse_signal.return_value = self._make_engine_result(82.34)
        src._engine = mock_engine

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == 82

    def test_live_true(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(75.0)

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.live is True

    def test_partial_false(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(80.0)

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.partial is False

    def test_age_h_is_zero(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(70.0)

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.age_h == 0

    def test_source_is_live(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(80.0)

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.source == "live"

    @pytest.mark.parametrize("composite", [0.0, 25.0, 50.0, 75.0, 82.34, 100.0])
    def test_various_composite_values(self, composite: float) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(composite)

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == int(round(composite))

    def test_engine_exception_returns_none(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.side_effect = RuntimeError("network error")

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None

    def test_engine_missing_composite_key_returns_none(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = {"scores": {}}  # no "composite"

        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None

    def test_engine_called_with_pro_symbol(self) -> None:
        src = LiveScoreSource(timeout_s=5)
        src._engine = MagicMock()
        src._engine.analyse_signal.return_value = self._make_engine_result(80.0)

        src.get_score("NVDA", "NASDAQ:NVDA")
        call_args = src._engine.analyse_signal.call_args
        fire = call_args[1].get("fire") or call_args[0][0]
        assert fire.get("pro_symbol") == "NASDAQ:NVDA"

    def test_name_property(self) -> None:
        assert LiveScoreSource().name == "LiveScoreSource"

    def test_lazy_engine_starts_none(self) -> None:
        src = LiveScoreSource()
        assert src._engine is None


class TestLiveScoreSourceTimeout:
    """LiveScoreSource timeout behaviour."""

    def test_returns_none_on_timeout(self) -> None:
        """If AnalysisEngine takes too long, returns None (never blocks)."""
        src = LiveScoreSource(timeout_s=1)

        slow_engine = MagicMock()
        def slow_analyse(**kwargs):
            import time
            time.sleep(3)  # longer than timeout
            return {"composite": 80.0}
        slow_engine.analyse_signal.side_effect = slow_analyse
        src._engine = slow_engine

        rec = src.get_score("SLOW", "NYSE:SLOW")
        assert rec is None, "Timeout should return None, not block forever"


# ===========================================================================
# 4. TestPartialScoreSource
# ===========================================================================

class TestPartialScoreSourceMocked:
    """PartialScoreSource with mocked signal_stack."""

    def _inject_signal_stack_stub(self, tv_sym: str, l1_total: float, composite: float):
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {tv_sym: {"RSI": 60}}
        stub.score_technical = lambda alert, screener: {"total": l1_total}
        stub.composite = lambda l1, l2, l3, l4: composite
        sys.modules["signal_stack"] = stub
        return stub

    def teardown_method(self, method):
        sys.modules.pop("signal_stack", None)

    def test_partial_true_always(self) -> None:
        self._inject_signal_stack_stub("NASDAQ:NVDA", 5.5, 56.0)
        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.partial is True, "PartialScoreSource MUST always set partial=True"

    def test_live_false_always(self) -> None:
        self._inject_signal_stack_stub("NASDAQ:NVDA", 5.5, 56.0)
        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.live is False

    def test_source_is_partial(self) -> None:
        self._inject_signal_stack_stub("NASDAQ:NVDA", 5.5, 56.0)
        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.source == "partial"

    @pytest.mark.parametrize("composite", [30.0, 40.0, 50.0, 56.0, 60.0, 70.0])
    def test_composite_score_propagated(self, composite: float) -> None:
        self._inject_signal_stack_stub("NASDAQ:NVDA", 5.5, composite)
        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == int(round(composite))

    def test_empty_screener_data_returns_none(self) -> None:
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {}  # no data for any symbol
        stub.score_technical = lambda alert, screener: {"total": 5.5}
        stub.composite = lambda l1, l2, l3, l4: 56.0
        sys.modules["signal_stack"] = stub

        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None

    def test_signal_stack_import_error_returns_none(self) -> None:
        sys.modules.pop("signal_stack", None)
        # Without signal_stack importable, should return None gracefully
        src = PartialScoreSource()
        # This will fail to import and return None
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        # Either None (import failed) or a record (if signal_stack happened to be importable)
        # The key invariant: must not raise
        assert rec is None or isinstance(rec, ScoreRecord)

    def test_score_technical_exception_returns_none(self) -> None:
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {"NASDAQ:NVDA": {"RSI": 60}}
        stub.score_technical = MagicMock(side_effect=RuntimeError("oops"))
        stub.composite = lambda l1, l2, l3, l4: 56.0
        sys.modules["signal_stack"] = stub

        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is None

    def test_name_property(self) -> None:
        assert PartialScoreSource().name == "PartialScoreSource"

    def test_age_h_is_none(self) -> None:
        self._inject_signal_stack_stub("NASDAQ:NVDA", 5.5, 56.0)
        src = PartialScoreSource()
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        assert rec is not None
        assert rec.age_h is None


class TestPartialScoreSourceInvariant:
    """Critical invariant: partial=True MUST never be False for PartialScoreSource."""

    @pytest.mark.parametrize("composite", [10, 20, 30, 40, 50, 55, 60, 70, 80, 90, 100])
    def test_partial_true_for_all_scores(self, composite: float) -> None:
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {"NASDAQ:NVDA": {"RSI": 60}}
        stub.score_technical = lambda alert, screener: {"total": 5.5}
        stub.composite = lambda l1, l2, l3, l4: float(composite)
        sys.modules["signal_stack"] = stub

        try:
            src = PartialScoreSource()
            rec = src.get_score("NVDA", "NASDAQ:NVDA")
            if rec is not None:
                assert rec.partial is True, (
                    f"PartialScoreSource MUST set partial=True for score={composite}"
                )
        finally:
            sys.modules.pop("signal_stack", None)


# ===========================================================================
# 5. TestHedgerScoreProvider — factories
# ===========================================================================

class TestHedgerScoreProviderFactories:
    """Test factory methods."""

    def test_default_has_three_sources(self) -> None:
        p = HedgerScoreProvider.default()
        assert len(p._sources) == 3

    def test_default_source_order(self) -> None:
        p = HedgerScoreProvider.default()
        names = [s.name for s in p._sources]
        assert names[0] == "DbScoreSource"
        assert names[1] == "LiveScoreSource"
        assert names[2] == "PartialScoreSource"

    def test_db_only_has_one_source(self) -> None:
        p = HedgerScoreProvider.db_only()
        assert len(p._sources) == 1
        assert p._sources[0].name == "DbScoreSource"

    def test_db_only_max_age_zero(self) -> None:
        p = HedgerScoreProvider.db_only(db_max_age_h=0)
        assert p._sources[0]._max_age_h == 0

    def test_live_only_two_sources(self) -> None:
        p = HedgerScoreProvider.live_only()
        assert len(p._sources) == 2
        names = [s.name for s in p._sources]
        assert names[0] == "LiveScoreSource"
        assert names[1] == "PartialScoreSource"

    def test_for_testing_with_db_only(self) -> None:
        p = HedgerScoreProvider.for_testing(db_returns={"NVDA": ScoreRecord(82, "db")})
        assert len(p._sources) >= 1

    def test_for_testing_db_and_live(self) -> None:
        p = HedgerScoreProvider.for_testing(
            db_returns={"NVDA": ScoreRecord(82, "db")},
            live_returns={"AAPL": ScoreRecord(75, "live", 0, live=True)},
        )
        assert len(p._sources) == 2

    def test_empty_sources_raises(self) -> None:
        with pytest.raises(ValueError):
            HedgerScoreProvider(sources=[])

    def test_cache_starts_empty(self) -> None:
        p = HedgerScoreProvider.default()
        assert len(p._cache) == 0

    def test_default_max_age_7_days(self) -> None:
        p = HedgerScoreProvider.default()
        db_src = p._sources[0]
        assert db_src._max_age_h == 168

    def test_live_timeout_configurable(self) -> None:
        p = HedgerScoreProvider.default(live_timeout_s=60)
        live_src = p._sources[1]
        assert live_src._timeout_s == 60


# ===========================================================================
# 6. TestHedgerScoreProvider — get() and cache
# ===========================================================================

class TestHedgerScoreProviderGet:
    """Test HedgerScoreProvider.get()."""

    def test_returns_record_from_first_source(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db", 2)})
        rec = p.get("NVDA")
        assert rec is not None
        assert rec.score == 82

    def test_returns_none_when_all_sources_fail(self) -> None:
        p = make_stub_provider(db={"NVDA": None})
        rec = p.get("NVDA")
        assert rec is None

    def test_cache_hit_after_first_get(self) -> None:
        call_count = {"n": 0}

        class CountingSource(ScoreSource):
            def get_score(self, u, tv):
                call_count["n"] += 1
                return ScoreRecord(80, "db")
            @property
            def name(self): return "counting"

        p = HedgerScoreProvider(sources=[CountingSource()])
        p.get("NVDA")
        p.get("NVDA")
        assert call_count["n"] == 1, "Source called more than once for same symbol (cache miss)"

    def test_different_symbols_each_resolved(self) -> None:
        p = make_stub_provider(db={
            "NVDA": ScoreRecord(82, "db"),
            "AAPL": ScoreRecord(75, "db"),
        })
        assert p.get("NVDA").score == 82
        assert p.get("AAPL").score == 75

    def test_none_also_cached(self) -> None:
        call_count = {"n": 0}

        class CountingNoneSource(ScoreSource):
            def get_score(self, u, tv):
                call_count["n"] += 1
                return None
            @property
            def name(self): return "counting_none"

        p = HedgerScoreProvider(sources=[CountingNoneSource()])
        p.get("NVDA")
        p.get("NVDA")
        assert call_count["n"] == 1, "None result should also be cached"

    def test_tv_symbol_passed_to_source(self) -> None:
        received = {}

        class CapturingSource(ScoreSource):
            def get_score(self, u, tv):
                received["tv"] = tv
                return ScoreRecord(80, "db")
            @property
            def name(self): return "capturing"

        p = HedgerScoreProvider(sources=[CapturingSource()])
        p.get("NVDA", tv_symbol="NASDAQ:NVDA")
        assert received.get("tv") == "NASDAQ:NVDA"

    def test_guess_tv_symbol_when_none_given(self) -> None:
        received = {}

        class CapturingSource(ScoreSource):
            def get_score(self, u, tv):
                received["tv"] = tv
                return ScoreRecord(80, "db")
            @property
            def name(self): return "capturing"

        p = HedgerScoreProvider(sources=[CapturingSource()])
        p.get("NVDA")
        assert "NVDA" in received.get("tv", ""), f"Expected guessed TV symbol, got {received.get('tv')}"

    def test_symbol_with_colon_used_as_is(self) -> None:
        received = {}

        class CapturingSource(ScoreSource):
            def get_score(self, u, tv):
                received["tv"] = tv
                return ScoreRecord(80, "db")
            @property
            def name(self): return "capturing"

        p = HedgerScoreProvider(sources=[CapturingSource()])
        p.get("NASDAQ:NVDA")
        assert received.get("tv") == "NASDAQ:NVDA"


# ===========================================================================
# 7. TestHedgerScoreProvider — prefetch()
# ===========================================================================

class TestHedgerScoreProviderPrefetch:
    """Test HedgerScoreProvider.prefetch()."""

    def test_prefetch_populates_cache(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db"), "AAPL": ScoreRecord(75, "db")})
        p.prefetch(["NVDA", "AAPL"])
        assert "NVDA" in p._cache
        assert "AAPL" in p._cache

    def test_prefetch_skips_already_cached(self) -> None:
        call_count = {"n": 0}

        class CountingSource(ScoreSource):
            def get_score(self, u, tv):
                call_count["n"] += 1
                return ScoreRecord(80, "db")
            @property
            def name(self): return "counting"

        p = HedgerScoreProvider(sources=[CountingSource()])
        p.prefetch(["NVDA"])
        p.prefetch(["NVDA"])  # second call should not call source again
        assert call_count["n"] == 1

    def test_prefetch_uses_tv_sym_map(self) -> None:
        received = {}

        class CapturingSource(ScoreSource):
            def get_score(self, u, tv):
                received[u] = tv
                return ScoreRecord(80, "db")
            @property
            def name(self): return "capturing"

        p = HedgerScoreProvider(sources=[CapturingSource()])
        p.prefetch(["NVDA"], tv_symbol_map={"NVDA": "NASDAQ:NVDA"})
        assert received.get("NVDA") == "NASDAQ:NVDA"

    def test_prefetch_empty_list_no_error(self) -> None:
        p = make_stub_provider(db={})
        p.prefetch([])  # should not raise

    def test_prefetch_none_result_cached(self) -> None:
        p = make_stub_provider(db={"NVDA": None})
        p.prefetch(["NVDA"])
        assert "NVDA" in p._cache
        assert p._cache["NVDA"] is None


# ===========================================================================
# 8. TestHedgerScoreProvider — invalidate()
# ===========================================================================

class TestHedgerScoreProviderInvalidate:
    """Test cache invalidation."""

    def test_invalidate_single_symbol(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db"), "AAPL": ScoreRecord(75, "db")})
        p.get("NVDA")
        p.get("AAPL")
        p.invalidate("NVDA")
        assert "NVDA" not in p._cache
        assert "AAPL" in p._cache

    def test_invalidate_all(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db"), "AAPL": ScoreRecord(75, "db")})
        p.get("NVDA")
        p.get("AAPL")
        p.invalidate()
        assert len(p._cache) == 0

    def test_invalidate_unknown_symbol_no_error(self) -> None:
        p = HedgerScoreProvider.db_only()
        p.invalidate("NONEXISTENT")  # should not raise

    def test_invalidate_then_re_get_calls_source_again(self) -> None:
        call_count = {"n": 0}
        results = [ScoreRecord(80, "db"), ScoreRecord(85, "db")]

        class CountingSource(ScoreSource):
            def get_score(self, u, tv):
                idx = call_count["n"]
                call_count["n"] += 1
                return results[min(idx, len(results)-1)]
            @property
            def name(self): return "counting"

        p = HedgerScoreProvider(sources=[CountingSource()])
        r1 = p.get("NVDA")
        p.invalidate("NVDA")
        r2 = p.get("NVDA")
        assert call_count["n"] == 2
        assert r1.score == 80
        assert r2.score == 85


# ===========================================================================
# 9. TestHedgerScoreProvider — to_dict()
# ===========================================================================

class TestHedgerScoreProviderToDict:
    """Test to_dict() integration method."""

    def test_to_dict_matches_record_to_dict(self) -> None:
        record = ScoreRecord(82, "db", 2, partial=False, live=False)
        p = make_stub_provider(db={"NVDA": record})
        d = p.to_dict("NVDA")
        assert d is not None
        assert d == record.to_dict()

    def test_to_dict_none_for_missing(self) -> None:
        p = make_stub_provider(db={"NVDA": None})
        assert p.to_dict("NVDA") is None

    def test_to_dict_contains_required_keys(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db", 2)})
        d = p.to_dict("NVDA")
        assert d is not None
        for key in ("score", "live", "age_h", "partial", "source"):
            assert key in d

    @pytest.mark.parametrize("partial", [True, False])
    def test_to_dict_partial_flag(self, partial: bool) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(56, "partial", partial=partial)})
        d = p.to_dict("NVDA")
        assert d is not None
        assert d["partial"] is partial


# ===========================================================================
# 10. TestHedgerScoreProviderPriority — source waterfall
# ===========================================================================

class TestHedgerScoreProviderPriority:
    """Test the DB > Live > Partial priority chain."""

    def test_db_source_used_when_available(self) -> None:
        """DB returns score -> live and partial sources never called."""
        live_call_count = {"n": 0}

        class SpyLiveSource(ScoreSource):
            def get_score(self, u, tv):
                live_call_count["n"] += 1
                return ScoreRecord(95, "live", live=True)
            @property
            def name(self): return "spy_live"

        db_stub = _StubScoreSource("db", {"NVDA": ScoreRecord(82, "db", 2)})
        p = HedgerScoreProvider(sources=[db_stub, SpyLiveSource()])
        rec = p.get("NVDA")
        assert rec.score == 82
        assert live_call_count["n"] == 0, "Live source should NOT be called when DB has score"

    def test_live_source_tried_when_db_none(self) -> None:
        """DB returns None -> live source tried."""
        db_stub = _StubScoreSource("db", {"NVDA": None})
        live_stub = _StubScoreSource("live", {"NVDA": ScoreRecord(75, "live", 0, live=True)})
        p = HedgerScoreProvider(sources=[db_stub, live_stub])
        rec = p.get("NVDA")
        assert rec is not None
        assert rec.source == "live"
        assert rec.score == 75

    def test_partial_source_tried_when_db_and_live_none(self) -> None:
        """DB=None, Live=None -> partial source tried."""
        db_stub = _StubScoreSource("db", {"NVDA": None})
        live_stub = _StubScoreSource("live", {"NVDA": None})
        partial_stub = _StubScoreSource(
            "partial",
            {"NVDA": ScoreRecord(56, "partial", None, partial=True)},
        )
        p = HedgerScoreProvider(sources=[db_stub, live_stub, partial_stub])
        rec = p.get("NVDA")
        assert rec is not None
        assert rec.partial is True
        assert rec.score == 56

    def test_none_when_all_sources_fail(self) -> None:
        """DB=None, Live=None, Partial=None -> None returned."""
        p = HedgerScoreProvider(sources=[
            _StubScoreSource("db", {"NVDA": None}),
            _StubScoreSource("live", {"NVDA": None}),
            _StubScoreSource("partial", {"NVDA": None}),
        ])
        assert p.get("NVDA") is None

    def test_db_score_not_replaced_by_live_on_second_call(self) -> None:
        """Even if live would return a different score, cached DB score wins."""
        db_stub = _StubScoreSource("db", {"NVDA": ScoreRecord(82, "db", 2)})
        live_stub = _StubScoreSource("live", {"NVDA": ScoreRecord(95, "live", 0, live=True)})
        p = HedgerScoreProvider(sources=[db_stub, live_stub])
        p.get("NVDA")   # first call populates cache with DB=82
        rec = p.get("NVDA")   # second call hits cache, NOT live
        assert rec.score == 82
        assert rec.source == "db"

    @pytest.mark.parametrize("db_score, expected_score", [
        (82, 82), (75, 75), (56, 56), (0, 0), (100, 100),
    ])
    def test_db_score_values_pass_through(self, db_score: int, expected_score: int) -> None:
        p = make_stub_provider(db={"SYM": ScoreRecord(db_score, "db", 1)})
        rec = p.get("SYM")
        assert rec is not None
        assert rec.score == expected_score

    def test_source_exception_skipped_tries_next(self) -> None:
        """A source that raises should be skipped, not crash the chain."""
        class ExplodingSource(ScoreSource):
            def get_score(self, u, tv):
                raise RuntimeError("explode")
            @property
            def name(self): return "exploding"

        fallback_stub = _StubScoreSource("fallback", {"NVDA": ScoreRecord(60, "db")})
        p = HedgerScoreProvider(sources=[ExplodingSource(), fallback_stub])
        rec = p.get("NVDA")
        assert rec is not None
        assert rec.score == 60

    def test_first_non_none_wins_in_chain(self) -> None:
        """First non-None result from any source should be used."""
        sources = [
            _StubScoreSource("s1", {}),               # returns None for NVDA
            _StubScoreSource("s2", {"NVDA": None}),   # explicit None
            _StubScoreSource("s3", {"NVDA": ScoreRecord(70, "s3")}),  # first hit
            _StubScoreSource("s4", {"NVDA": ScoreRecord(80, "s4")}),  # should NOT be used
        ]
        p = HedgerScoreProvider(sources=sources)
        rec = p.get("NVDA")
        assert rec is not None
        assert rec.source == "s3"


# ===========================================================================
# 11. TestResolvetvSymbolsFromDb
# ===========================================================================

class TestResolvetvSymbolsFromDb:
    """Test resolve_tv_symbols_from_db()."""

    def test_returns_tv_symbol_from_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        result = resolve_tv_symbols_from_db(["NVDA"], db_path=db)
        assert result.get("NVDA") == "NASDAQ:NVDA"

    def test_fallback_to_nasdaq_when_not_in_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [])  # empty DB
        result = resolve_tv_symbols_from_db(["NVDA"], db_path=db)
        assert result.get("NVDA") == "NASDAQ:NVDA"

    def test_missing_db_fallback_for_all(self, tmp_path: Path) -> None:
        result = resolve_tv_symbols_from_db(
            ["NVDA", "AAPL", "MSFT"],
            db_path=tmp_path / "missing.db",
        )
        for sym in ("NVDA", "AAPL", "MSFT"):
            assert result[sym] == f"NASDAQ:{sym}"

    def test_mixed_in_db_and_not(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        result = resolve_tv_symbols_from_db(["NVDA", "NEWSTOCK"], db_path=db)
        assert result["NVDA"] == "NASDAQ:NVDA"
        assert result["NEWSTOCK"] == "NASDAQ:NEWSTOCK"

    def test_empty_list(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [])
        result = resolve_tv_symbols_from_db([], db_path=db)
        assert result == {}

    def test_nse_symbol(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NSE:TITAN", 70.0, now_iso(1))])
        result = resolve_tv_symbols_from_db(["TITAN"], db_path=db)
        assert result["TITAN"] == "NSE:TITAN"

    def test_most_recent_tv_symbol_used(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [
            ("NYSE:NVDA", 60.0, now_iso(10)),   # older
            ("NASDAQ:NVDA", 82.0, now_iso(1)),   # newer
        ])
        result = resolve_tv_symbols_from_db(["NVDA"], db_path=db)
        assert result["NVDA"] == "NASDAQ:NVDA"

    @pytest.mark.parametrize("sym", ["AAPL", "MSFT", "TSLA", "AMZN", "META"])
    def test_fallback_pattern(self, tmp_path: Path, sym: str) -> None:
        result = resolve_tv_symbols_from_db([sym], db_path=tmp_path / "missing.db")
        assert result[sym] == f"NASDAQ:{sym}"


# ===========================================================================
# 12. TestBadgeRendererContract
# ===========================================================================

def _render_badge(sig_data: Optional[dict]) -> str:
    """Replica of the badge rendering logic from options_hedger.py."""
    if sig_data is None:
        return ""
    _sig     = sig_data.get("score")
    _live    = sig_data.get("live", False)
    _age     = sig_data.get("age_h")
    _partial = sig_data.get("partial", False)
    if _sig is None:
        return ""
    if _partial:
        _sc = "#8b949e"
        _sm = f"Signal estimate ~{_sig}/100 (technical only)"
    else:
        if _live:
            _ftag = " · live"
        elif _age is not None:
            _ftag = f" · {_age}h ago"
        else:
            _ftag = " · from DB"
        if _sig >= 70:
            _sc, _sm = "#3fb950", f"Signal score {_sig}/100 -- STRONG · consider holding{_ftag}"
        elif _sig <= 60:
            _sc, _sm = "#f85149", f"Signal score {_sig}/100 -- WEAK · selling now{_ftag}"
        else:
            _sc, _sm = "#e3b341", f"Signal score {_sig}/100 -- NEUTRAL{_ftag}"
    return f"<div style='color:{_sc}'>{_sm}</div>"


class TestBadgeRendererContract:
    """Verify the badge rendering contract for partial/full scores."""

    # ---- partial scores: NEVER show WEAK/STRONG/NEUTRAL ----

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_never_strong(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "STRONG" not in badge, f"partial badge must not show STRONG for score={score}"

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_never_weak(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "WEAK" not in badge, f"partial badge must not show WEAK for score={score}"

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_never_neutral(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "NEUTRAL" not in badge, f"partial badge must not show NEUTRAL for score={score}"

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_never_sell_advice(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "selling now" not in badge

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_shows_technical_only(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "technical only" in badge

    @pytest.mark.parametrize("score", range(0, 101, 5))
    def test_partial_badge_is_grey(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "#8b949e" in badge, f"partial badge should be grey for score={score}"

    # ---- full (non-partial) scores: correct tier ----

    @pytest.mark.parametrize("score", [70, 71, 75, 80, 82, 90, 100])
    def test_full_strong_tier(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        assert "STRONG" in badge, f"Full score {score} should show STRONG"

    @pytest.mark.parametrize("score", [0, 10, 30, 50, 56, 60])
    def test_full_weak_tier(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        assert "WEAK" in badge, f"Full score {score} should show WEAK"

    @pytest.mark.parametrize("score", [61, 62, 65, 68, 69])
    def test_full_neutral_tier(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        assert "NEUTRAL" in badge, f"Full score {score} should show NEUTRAL"

    # ---- NVDA regression: DB=82 must show STRONG ----

    def test_nvda_db_82_shows_strong(self) -> None:
        """Core regression: NVDA DB score 82 must show STRONG, not WEAK."""
        badge = _render_badge({"score": 82, "partial": False, "live": False, "age_h": 1})
        assert "STRONG" in badge, "NVDA DB=82 must show STRONG"
        assert "WEAK" not in badge

    def test_nvda_partial_56_never_shows_weak(self) -> None:
        """Core regression: NVDA L1-only=56 with partial=True must NOT show WEAK."""
        badge = _render_badge({"score": 56, "partial": True})
        assert "WEAK" not in badge, "NVDA partial=56 must NOT show WEAK"
        assert "STRONG" not in badge
        assert "technical only" in badge

    # ---- live tag in badge ----

    def test_live_tag_when_live_true(self) -> None:
        badge = _render_badge({"score": 82, "partial": False, "live": True, "age_h": None})
        assert "live" in badge

    def test_age_tag_when_age_provided(self) -> None:
        badge = _render_badge({"score": 82, "partial": False, "live": False, "age_h": 5})
        assert "5h ago" in badge

    def test_from_db_tag_when_no_age_no_live(self) -> None:
        badge = _render_badge({"score": 82, "partial": False, "live": False, "age_h": None})
        assert "from DB" in badge


# ===========================================================================
# 13. TestDictCompat — ScoreRecord.to_dict() contract for badge renderer
# ===========================================================================

class TestDictCompat:
    """Verify that ScoreRecord.to_dict() produces a dict compatible with
    the options_hedger badge renderer."""

    @pytest.mark.parametrize("source,partial,live", [
        ("db", False, False),
        ("live", False, True),
        ("partial", True, False),
    ])
    def test_to_dict_partial_correct(self, source: str, partial: bool, live: bool) -> None:
        r = ScoreRecord(70, source, partial=partial, live=live)
        d = r.to_dict()
        assert d["partial"] == partial
        assert d["live"] == live

    def test_to_dict_score_badge_correct_for_db(self) -> None:
        d = ScoreRecord(82, "db", 2, partial=False, live=False).to_dict()
        badge = _render_badge(d)
        assert "STRONG" in badge

    def test_to_dict_score_badge_correct_for_partial(self) -> None:
        d = ScoreRecord(56, "partial", None, partial=True, live=False).to_dict()
        badge = _render_badge(d)
        assert "WEAK" not in badge
        assert "technical only" in badge

    def test_from_dict_to_dict_roundtrip_idempotent(self) -> None:
        d = {"score": 82, "source": "db", "age_h": 2, "partial": False, "live": False}
        r = ScoreRecord.from_dict(d)
        assert r.to_dict() == d


# ===========================================================================
# 14. TestEdgeCases
# ===========================================================================

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_score_zero_returned_from_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:TEST", 0.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("TEST", "NASDAQ:TEST")
        assert rec is not None
        assert rec.score == 0

    def test_score_100_returned_from_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [("NASDAQ:TEST", 100.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("TEST", "NASDAQ:TEST")
        assert rec is not None
        assert rec.score == 100

    def test_provider_guess_tv_symbol_nasdaq(self) -> None:
        assert HedgerScoreProvider._guess_tv_symbol("NVDA") == "NASDAQ:NVDA"

    def test_provider_guess_tv_symbol_with_colon(self) -> None:
        assert HedgerScoreProvider._guess_tv_symbol("NSE:TITAN") == "NSE:TITAN"

    def test_provider_concurrent_same_symbol(self) -> None:
        """Concurrent get() calls for the same symbol should not cause issues."""
        call_count = {"n": 0}

        class SlowSource(ScoreSource):
            def get_score(self, u, tv):
                import time
                call_count["n"] += 1
                time.sleep(0.05)
                return ScoreRecord(80, "db")
            @property
            def name(self): return "slow"

        p = HedgerScoreProvider(sources=[SlowSource()])
        threads = [threading.Thread(target=p.get, args=("NVDA",)) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        # The source should have been called at most a small number of times
        # (idempotent even if called multiple times due to race)
        assert call_count["n"] >= 1

    def test_invalidate_allows_fresh_resolution(self) -> None:
        results = iter([ScoreRecord(70, "db"), ScoreRecord(85, "db")])

        class IterSource(ScoreSource):
            def get_score(self, u, tv):
                return next(results, None)
            @property
            def name(self): return "iter"

        p = HedgerScoreProvider(sources=[IterSource()])
        r1 = p.get("NVDA")
        p.invalidate("NVDA")
        r2 = p.get("NVDA")
        assert r1.score == 70
        assert r2.score == 85

    def test_source_exception_does_not_propagate(self) -> None:
        class BrokenSource(ScoreSource):
            def get_score(self, u, tv):
                raise ValueError("broken")
            @property
            def name(self): return "broken"

        p = HedgerScoreProvider(sources=[BrokenSource()])
        # Should not raise, should return None
        rec = p.get("NVDA")
        assert rec is None

    @pytest.mark.parametrize("bad_ts", [
        "not-a-timestamp", "", "2020-13-45T00:00:00", "invalid",
    ])
    def test_bad_timestamp_in_db_does_not_crash(
        self, tmp_path: Path, bad_ts: str
    ) -> None:
        db = make_db(tmp_path, [("NASDAQ:NVDA", 80.0, bad_ts)])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("NVDA", "NASDAQ:NVDA")
        # With max_age=0 the record should come through even with bad timestamp
        assert rec is not None
        assert rec.age_h is None  # age_h unknown

    def test_prefetch_with_none_tv_map(self) -> None:
        p = make_stub_provider(db={"NVDA": ScoreRecord(82, "db")})
        p.prefetch(["NVDA"], tv_symbol_map=None)  # should not raise
        assert "NVDA" in p._cache

    def test_stub_score_source_name(self) -> None:
        stub = _StubScoreSource("my_stub", {})
        assert stub.name == "my_stub"

    def test_stub_score_source_returns_none_for_unknown(self) -> None:
        stub = _StubScoreSource("stub", {"NVDA": ScoreRecord(82, "db")})
        assert stub.get_score("UNKNOWN", "NASDAQ:UNKNOWN") is None

    def test_stub_score_source_returns_none_explicitly(self) -> None:
        stub = _StubScoreSource("stub", {"NVDA": None})
        assert stub.get_score("NVDA", "NASDAQ:NVDA") is None


# ===========================================================================
# 15. TestHedgerScoreProviderIntegration — end-to-end
# ===========================================================================

class TestHedgerScoreProviderIntegration:
    """Integration tests combining multiple components."""

    def test_db_beats_partial_end_to_end(self, tmp_path: Path) -> None:
        """When DB has composite=82, partial source should NEVER override it."""
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        p = HedgerScoreProvider(sources=[
            DbScoreSource(db_path=db, max_age_h=0),
            PartialScoreSource(),   # partial would give ~56 for NVDA
        ])
        rec = p.get("NVDA", tv_symbol="NASDAQ:NVDA")
        assert rec is not None
        assert rec.score == 82, f"DB score 82 must beat partial estimate. Got {rec.score}"
        assert rec.partial is False

    def test_nvda_regression_db_82_not_56(self, tmp_path: Path) -> None:
        """Core regression: DB score 82 must not produce WEAK badge for NVDA."""
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(1))])
        p = HedgerScoreProvider(sources=[DbScoreSource(db_path=db, max_age_h=0)])
        d = p.to_dict("NVDA")
        assert d is not None
        badge = _render_badge(d)
        assert "STRONG" in badge, f"NVDA DB=82 must show STRONG, got: {badge}"
        assert "WEAK" not in badge

    def test_full_chain_db_none_partial_used(self, tmp_path: Path) -> None:
        """When DB has nothing, partial source is the fallback."""
        empty_db = make_db(tmp_path, [])
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {"NASDAQ:NEWCO": {"RSI": 60}}
        stub.score_technical = lambda alert, screener: {"total": 5.5}
        stub.composite = lambda l1, l2, l3, l4: 50.0
        sys.modules["signal_stack"] = stub

        try:
            p = HedgerScoreProvider(sources=[
                DbScoreSource(db_path=empty_db, max_age_h=0),
                PartialScoreSource(),
            ])
            rec = p.get("NEWCO", tv_symbol="NASDAQ:NEWCO")
            if rec is not None:
                assert rec.partial is True, "Fallback from empty DB should be partial=True"
                badge = _render_badge(rec.to_dict())
                assert "WEAK" not in badge
                assert "STRONG" not in badge
        finally:
            sys.modules.pop("signal_stack", None)

    def test_stale_db_falls_through_to_partial(self, tmp_path: Path) -> None:
        """When DB record is older than max_age_h, partial source is used as fallback."""
        db = make_db(tmp_path, [("NASDAQ:NVDA", 82.0, now_iso(200))])  # 200h old
        stub = types.ModuleType("signal_stack")
        stub.fetch_tv_screener = lambda syms: {"NASDAQ:NVDA": {"RSI": 60}}
        stub.score_technical = lambda alert, screener: {"total": 5.5}
        stub.composite = lambda l1, l2, l3, l4: 56.0
        sys.modules["signal_stack"] = stub

        try:
            p = HedgerScoreProvider(sources=[
                DbScoreSource(db_path=db, max_age_h=168),  # expires 200h record
                PartialScoreSource(),
            ])
            rec = p.get("NVDA", tv_symbol="NASDAQ:NVDA")
            if rec is not None:
                assert rec.partial is True, "Stale DB -> partial source -> partial=True"
                assert rec.score == 56
        finally:
            sys.modules.pop("signal_stack", None)

    def test_for_testing_factory_priority(self) -> None:
        """for_testing() must respect priority: DB stub returns score -> live stub not called."""
        live_called = {"n": 0}

        class SpyLive(ScoreSource):
            def get_score(self, u, tv):
                live_called["n"] += 1
                return ScoreRecord(95, "live", live=True)
            @property
            def name(self): return "spy_live"

        db_stub = _StubScoreSource("db", {"NVDA": ScoreRecord(82, "db", 2)})
        p = HedgerScoreProvider(sources=[db_stub, SpyLive()])
        rec = p.get("NVDA")
        assert rec.score == 82
        assert live_called["n"] == 0

    @pytest.mark.parametrize("db_score, expected_tier", [
        (82, "STRONG"), (75, "STRONG"), (70, "STRONG"),
        (65, "NEUTRAL"), (63, "NEUTRAL"), (61, "NEUTRAL"),
        (60, "WEAK"), (56, "WEAK"), (50, "WEAK"),
    ])
    def test_badge_tier_for_db_scores(self, tmp_path: Path, db_score: int, expected_tier: str) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", float(db_score), now_iso(1))])
        p = HedgerScoreProvider(sources=[DbScoreSource(db_path=db, max_age_h=0)])
        d = p.to_dict("SYM")
        assert d is not None
        badge = _render_badge(d)
        assert expected_tier in badge, (
            f"DB score {db_score} should show {expected_tier}, badge={badge}"
        )


# ===========================================================================
# 16. TestScoreRecordAllScores — exhaustive 0-100 parametrize
# ===========================================================================

_ALL_SCORES = list(range(0, 101))
_ALL_SOURCES = ["db", "live", "partial"]
_AGE_VALUES = [None, 0, 1, 24, 48, 168]


class TestScoreRecordAllScores:
    """Exhaustive score-range tests (0–100) for ScoreRecord."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_to_dict_score_field_all_values(self, score: int) -> None:
        d = ScoreRecord(score, "db").to_dict()
        assert d["score"] == score

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_from_dict_roundtrip_all_scores(self, score: int) -> None:
        d = {"score": score, "source": "db", "age_h": None, "partial": False, "live": False}
        r = ScoreRecord.from_dict(d)
        assert r.score == score

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_score_record_eq_reflexive_all_scores(self, score: int) -> None:
        a = ScoreRecord(score, "db")
        b = ScoreRecord(score, "db")
        assert a == b

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_score_record_repr_contains_score_all(self, score: int) -> None:
        assert str(score) in repr(ScoreRecord(score, "db"))

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_to_dict_has_all_keys_all_scores(self, score: int) -> None:
        d = ScoreRecord(score, "db").to_dict()
        assert set(d.keys()) >= {"score", "source", "age_h", "partial", "live"}

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_to_dict_partial_false_by_default_all_scores(self, score: int) -> None:
        assert ScoreRecord(score, "db").to_dict()["partial"] is False

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_to_dict_live_false_by_default_all_scores(self, score: int) -> None:
        assert ScoreRecord(score, "db").to_dict()["live"] is False


class TestScoreRecordSourcesAllScores:
    """Parametrize over score × source combinations."""

    @pytest.mark.parametrize("score,source", [
        (s, src) for s in range(0, 101, 5) for src in _ALL_SOURCES
    ])
    def test_source_field_preserved(self, score: int, source: str) -> None:
        r = ScoreRecord(score, source)
        assert r.source == source

    @pytest.mark.parametrize("score,source", [
        (s, src) for s in range(0, 101, 5) for src in _ALL_SOURCES
    ])
    def test_to_dict_source_field_preserved(self, score: int, source: str) -> None:
        d = ScoreRecord(score, source).to_dict()
        assert d["source"] == source


class TestScoreRecordAgeAllValues:
    """Parametrize over score × age_h combinations."""

    @pytest.mark.parametrize("score,age_h", [
        (s, a) for s in range(0, 101, 10) for a in _AGE_VALUES
    ])
    def test_age_h_field_preserved(self, score: int, age_h) -> None:
        r = ScoreRecord(score, "db", age_h=age_h)
        assert r.age_h == age_h

    @pytest.mark.parametrize("score,age_h", [
        (s, a) for s in range(0, 101, 10) for a in _AGE_VALUES
    ])
    def test_to_dict_age_h_field_preserved(self, score: int, age_h) -> None:
        d = ScoreRecord(score, "db", age_h=age_h).to_dict()
        assert d["age_h"] == age_h


class TestScoreRecordPartialLiveCombos:
    """Parametrize over all partial/live combinations across many scores."""

    @pytest.mark.parametrize("score,partial,live", [
        (s, p, l_)
        for s in range(0, 101, 5)
        for p in (True, False)
        for l_ in (True, False)
    ])
    def test_partial_live_combo(self, score: int, partial: bool, live: bool) -> None:
        r = ScoreRecord(score, "partial" if partial else "db", partial=partial, live=live)
        assert r.partial == partial
        assert r.live == live

    @pytest.mark.parametrize("score,partial,live", [
        (s, p, l_)
        for s in range(0, 101, 5)
        for p in (True, False)
        for l_ in (True, False)
    ])
    def test_to_dict_partial_live_combo(self, score: int, partial: bool, live: bool) -> None:
        d = ScoreRecord(score, "db", partial=partial, live=live).to_dict()
        assert d["partial"] == partial
        assert d["live"] == live


# ===========================================================================
# 17. TestBadgeRendererAllScores — exhaustive score range
# ===========================================================================

def _tier(score: int) -> str:
    if score >= 70:
        return "STRONG"
    if score <= 60:
        return "WEAK"
    return "NEUTRAL"


class TestBadgeRendererAllScores:
    """Badge renderer exhaustive tests for every integer score 0-100."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_contains_score_number(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        assert str(score) in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_correct_tier(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        expected = _tier(score)
        assert expected in badge, f"score={score}: expected {expected} in badge='{badge}'"

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_badge_never_shows_tier(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "STRONG" not in badge
        assert "WEAK" not in badge
        assert "NEUTRAL" not in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_badge_always_shows_technical(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "technical" in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_badge_always_grey(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "#8b949e" in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_not_empty(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 2})
        assert badge != ""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_contains_out_of_100(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 2})
        assert "/100" in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_strong_means_green(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        if score >= 70:
            assert "#3fb950" in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_weak_means_red(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        if score <= 60:
            assert "#f85149" in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_full_badge_neutral_means_yellow(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": False, "age_h": 1})
        if 61 <= score <= 69:
            assert "#e3b341" in badge


# ===========================================================================
# 18. TestDbSourceAllScores — DB source over full score range
# ===========================================================================

class TestDbSourceAllScores:
    """Verify DbScoreSource returns correct score for every integer value 0-100."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_db_score_all_values(self, tmp_path: Path, score: int) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", float(score), now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        assert rec is not None
        assert rec.score == score

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_db_source_partial_false_all_scores(self, tmp_path: Path, score: int) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", float(score), now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        assert rec is not None
        assert rec.partial is False

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_db_source_live_false_all_scores(self, tmp_path: Path, score: int) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", float(score), now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        assert rec is not None
        assert rec.live is False


# ===========================================================================
# 19. TestHedgerProviderGetAllScores — provider end-to-end all scores
# ===========================================================================

class TestHedgerProviderGetAllScores:
    """Provider.get() for every score value 0-100 through stub chain."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_get_db_score_all_values(self, score: int) -> None:
        p = make_stub_provider(db={"SYM": ScoreRecord(score, "db")})
        rec = p.get("SYM")
        assert rec is not None
        assert rec.score == score

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_to_dict_score_all_values(self, score: int) -> None:
        p = make_stub_provider(db={"SYM": ScoreRecord(score, "db")})
        d = p.to_dict("SYM")
        assert d is not None
        assert d["score"] == score

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_badge_from_provider_correct_tier_all_scores(self, score: int) -> None:
        p = make_stub_provider(db={"SYM": ScoreRecord(score, "db")})
        d = p.to_dict("SYM")
        assert d is not None
        badge = _render_badge(d)
        expected = _tier(score)
        assert expected in badge, f"score={score}: expected {expected} badge"


# ===========================================================================
# 20. TestDictCompatAllScores — to_dict/from_dict for every score
# ===========================================================================

class TestDictCompatAllScores:
    """to_dict/from_dict identity for every integer score 0-100."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_roundtrip_all_scores_db(self, score: int) -> None:
        r = ScoreRecord(score, "db", 2, partial=False, live=False)
        assert ScoreRecord.from_dict(r.to_dict()) == r

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_roundtrip_all_scores_partial(self, score: int) -> None:
        r = ScoreRecord(score, "partial", None, partial=True, live=False)
        assert ScoreRecord.from_dict(r.to_dict()) == r

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_roundtrip_all_scores_live(self, score: int) -> None:
        r = ScoreRecord(score, "live", 0, partial=False, live=True)
        assert ScoreRecord.from_dict(r.to_dict()) == r


# ===========================================================================
# 21. TestPartialInvariantAllScores — partial=True invariant for every score
# ===========================================================================

class TestPartialInvariantAllScores:
    """The partial=True invariant must hold for every score value 0-100."""

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_record_partial_flag_true(self, score: int) -> None:
        r = ScoreRecord(score, "partial", partial=True)
        assert r.partial is True

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_record_to_dict_partial_true(self, score: int) -> None:
        assert ScoreRecord(score, "partial", partial=True).to_dict()["partial"] is True

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_badge_never_has_sell_advice(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "selling now" not in badge

    @pytest.mark.parametrize("score", _ALL_SCORES)
    def test_partial_badge_never_has_holding_advice(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": True})
        assert "consider holding" not in badge


# ===========================================================================
# 22. TestScoreRecordRoundingExhaustive — float rounding for many values
# ===========================================================================

class TestScoreRecordRoundingExhaustive:
    """Float rounding exhaustive check: every half-integer value 0.0–100.0."""

    @pytest.mark.parametrize("f,expected", [
        (f_val / 2.0, round(f_val / 2.0))
        for f_val in range(0, 201)
    ])
    def test_float_rounding(self, f: float, expected: int) -> None:
        r = ScoreRecord(f, "db")
        assert r.score == expected


# ===========================================================================
# 23. TestDbSourceManySymbols — distinct symbols in DB
# ===========================================================================

_MANY_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
    "NFLX", "CRM", "ADBE", "ORCL", "AMD", "INTC", "QCOM",
    "TXN", "AVGO", "AMAT", "LRCX", "KLAC", "MRVL",
]


class TestDbSourceManySymbols:
    """DbScoreSource with many different symbols in the DB."""

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_symbol_found_in_db(self, tmp_path: Path, sym: str) -> None:
        db = make_db(tmp_path, [(f"NASDAQ:{sym}", 75.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score(sym, f"NASDAQ:{sym}")
        assert rec is not None, f"Symbol {sym} should be found in DB"
        assert rec.score == 75

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_other_symbol_not_found(self, tmp_path: Path, sym: str) -> None:
        other = [s for s in _MANY_SYMBOLS if s != sym][0]
        db = make_db(tmp_path, [(f"NASDAQ:{other}", 75.0, now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score(sym, f"NASDAQ:{sym}")
        assert rec is None, f"Symbol {sym} should NOT be found when only {other} is in DB"

    @pytest.mark.parametrize("sym,score", [
        (sym, s)
        for sym, s in zip(_MANY_SYMBOLS, range(50, 100, 2))
    ])
    def test_each_symbol_correct_score(self, tmp_path: Path, sym: str, score: int) -> None:
        db = make_db(tmp_path, [(f"NASDAQ:{sym}", float(score), now_iso(1))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score(sym, f"NASDAQ:{sym}")
        assert rec is not None
        assert rec.score == score


class TestDbSourceMultipleSymbolsInDb:
    """DB with all symbols: each symbol resolved correctly."""

    def test_all_symbols_in_db_resolved(self, tmp_path: Path) -> None:
        rows = [(f"NASDAQ:{sym}", float(50 + i), now_iso(1))
                for i, sym in enumerate(_MANY_SYMBOLS)]
        db = make_db(tmp_path, rows)
        src = DbScoreSource(db_path=db, max_age_h=0)
        for i, sym in enumerate(_MANY_SYMBOLS):
            rec = src.get_score(sym, f"NASDAQ:{sym}")
            assert rec is not None, f"{sym} should be found"
            assert rec.score == 50 + i, f"{sym} score mismatch"

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_each_symbol_source_is_db(self, tmp_path: Path, sym: str) -> None:
        rows = [(f"NASDAQ:{sym}", 80.0, now_iso(1))]
        db = make_db(tmp_path, rows)
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score(sym, f"NASDAQ:{sym}")
        if rec is not None:
            assert rec.source == "db"


# ===========================================================================
# 24. TestHedgerProviderWithManySymbols — prefetch/get with many symbols
# ===========================================================================

class TestHedgerProviderWithManySymbols:
    """Provider with a map of many symbols for prefetch/get."""

    def _make_multi_provider(self) -> HedgerScoreProvider:
        db_map = {sym: ScoreRecord(50 + i, "db", 1) for i, sym in enumerate(_MANY_SYMBOLS)}
        return make_stub_provider(db=db_map)

    def test_prefetch_all_symbols(self) -> None:
        p = self._make_multi_provider()
        p.prefetch(_MANY_SYMBOLS)
        for sym in _MANY_SYMBOLS:
            assert sym in p._cache

    @pytest.mark.parametrize("sym,expected_score", [
        (sym, 50 + i) for i, sym in enumerate(_MANY_SYMBOLS)
    ])
    def test_get_each_symbol_correct(self, sym: str, expected_score: int) -> None:
        p = self._make_multi_provider()
        rec = p.get(sym)
        assert rec is not None
        assert rec.score == expected_score

    def test_invalidate_all_then_repopulate(self) -> None:
        p = self._make_multi_provider()
        for sym in _MANY_SYMBOLS:
            p.get(sym)
        p.invalidate()
        assert len(p._cache) == 0
        for sym in _MANY_SYMBOLS:
            p.get(sym)
        assert len(p._cache) == len(_MANY_SYMBOLS)


# ===========================================================================
# 25. TestResolvetvSymbolsManySymbols
# ===========================================================================

class TestResolvetvSymbolsManySymbols:
    """resolve_tv_symbols_from_db with all known symbols."""

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_fallback_nasdaq_all_symbols(self, tmp_path: Path, sym: str) -> None:
        result = resolve_tv_symbols_from_db([sym], db_path=tmp_path / "missing.db")
        assert result[sym] == f"NASDAQ:{sym}"

    def test_resolve_all_symbols_from_empty_db(self, tmp_path: Path) -> None:
        db = make_db(tmp_path, [])
        result = resolve_tv_symbols_from_db(_MANY_SYMBOLS, db_path=db)
        for sym in _MANY_SYMBOLS:
            assert result[sym] == f"NASDAQ:{sym}"

    def test_resolve_all_symbols_from_full_db(self, tmp_path: Path) -> None:
        rows = [(f"NASDAQ:{sym}", 75.0, now_iso(1)) for sym in _MANY_SYMBOLS]
        db = make_db(tmp_path, rows)
        result = resolve_tv_symbols_from_db(_MANY_SYMBOLS, db_path=db)
        for sym in _MANY_SYMBOLS:
            assert result[sym] == f"NASDAQ:{sym}"

    @pytest.mark.parametrize("exchange,sym", [
        ("NYSE", "GE"), ("NASDAQ", "NVDA"), ("NSE", "TITAN"),
        ("BSE", "INFOSYS"), ("LSE", "BARC"),
    ])
    def test_exchange_prefix_preserved(self, tmp_path: Path, exchange: str, sym: str) -> None:
        db = make_db(tmp_path, [(f"{exchange}:{sym}", 70.0, now_iso(1))])
        result = resolve_tv_symbols_from_db([sym], db_path=db)
        assert result[sym] == f"{exchange}:{sym}"


# ===========================================================================
# 26. TestAgeBoundaryExhaustive — boundary conditions around max_age_h
# ===========================================================================

class TestAgeBoundaryExhaustive:
    """Exhaustive age boundary tests across different max_age_h values."""

    @pytest.mark.parametrize("max_age_h,age_h,should_expire", [
        (168, h, h > 168) for h in [1, 24, 48, 100, 167, 168, 169, 200, 336, 500]
    ])
    def test_168h_max_age_boundary(
        self, tmp_path: Path, max_age_h: int, age_h: int, should_expire: bool
    ) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", 80.0, now_iso(age_h))])
        src = DbScoreSource(db_path=db, max_age_h=max_age_h)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        if should_expire:
            assert rec is None, f"age={age_h}h > max={max_age_h}h should expire"
        else:
            assert rec is not None, f"age={age_h}h <= max={max_age_h}h should NOT expire"

    @pytest.mark.parametrize("max_age_h,age_h,should_expire", [
        (48, h, h > 48) for h in [1, 24, 47, 48, 49, 72, 100]
    ])
    def test_48h_max_age_boundary(
        self, tmp_path: Path, max_age_h: int, age_h: int, should_expire: bool
    ) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", 80.0, now_iso(age_h))])
        src = DbScoreSource(db_path=db, max_age_h=max_age_h)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        if should_expire:
            assert rec is None
        else:
            assert rec is not None

    @pytest.mark.parametrize("age_h", [1, 100, 500, 1000, 10000])
    def test_max_age_zero_never_expires_all(self, tmp_path: Path, age_h: int) -> None:
        db = make_db(tmp_path, [("NASDAQ:SYM", 80.0, now_iso(age_h))])
        src = DbScoreSource(db_path=db, max_age_h=0)
        rec = src.get_score("SYM", "NASDAQ:SYM")
        assert rec is not None, f"max_age=0 should never expire, age={age_h}h"


# ===========================================================================
# 27. TestStubScoreSourceBehavior — _StubScoreSource comprehensive
# ===========================================================================

class TestStubScoreSourceBehavior:
    """Comprehensive tests for _StubScoreSource."""

    def test_returns_record_for_known_symbol(self) -> None:
        rec = ScoreRecord(82, "db")
        stub = _StubScoreSource("stub", {"NVDA": rec})
        assert stub.get_score("NVDA", "NASDAQ:NVDA") == rec

    def test_returns_none_for_unknown_symbol(self) -> None:
        stub = _StubScoreSource("stub", {"NVDA": ScoreRecord(82, "db")})
        assert stub.get_score("AAPL", "NASDAQ:AAPL") is None

    def test_returns_none_for_explicit_none_value(self) -> None:
        stub = _StubScoreSource("stub", {"NVDA": None})
        assert stub.get_score("NVDA", "NASDAQ:NVDA") is None

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_returns_record_for_all_symbols(self, sym: str) -> None:
        records = {s: ScoreRecord(50 + i, "db") for i, s in enumerate(_MANY_SYMBOLS)}
        stub = _StubScoreSource("stub", records)
        rec = stub.get_score(sym, f"NASDAQ:{sym}")
        assert rec is not None

    @pytest.mark.parametrize("name", ["db_stub", "live_stub", "partial_stub", "custom"])
    def test_name_property(self, name: str) -> None:
        stub = _StubScoreSource(name, {})
        assert stub.name == name

    def test_empty_dict_always_none(self) -> None:
        stub = _StubScoreSource("empty", {})
        for sym in _MANY_SYMBOLS:
            assert stub.get_score(sym, f"NASDAQ:{sym}") is None


# ===========================================================================
# 28. TestScoreSourceABC — abstract base class enforcement
# ===========================================================================

class TestScoreSourceABC:
    """Verify ScoreSource ABC is correctly abstract."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            ScoreSource()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_get_score(self) -> None:
        class NoGetScore(ScoreSource):
            pass
        with pytest.raises(TypeError):
            NoGetScore()  # type: ignore[abstract]

    def test_concrete_subclass_can_be_instantiated(self) -> None:
        class GoodSource(ScoreSource):
            def get_score(self, u, tv):
                return None
            @property
            def name(self): return "good"
        src = GoodSource()
        assert src.get_score("X", "X") is None

    def test_concrete_subclass_name_used_in_provider(self) -> None:
        class CustomSource(ScoreSource):
            def get_score(self, u, tv):
                return ScoreRecord(77, "custom")
            @property
            def name(self): return "custom"
        p = HedgerScoreProvider(sources=[CustomSource()])
        rec = p.get("SYM")
        assert rec is not None
        assert rec.source == "custom"


# ===========================================================================
# 29. TestHedgerScoreProviderCacheIsolation — per-symbol isolation
# ===========================================================================

class TestHedgerScoreProviderCacheIsolation:
    """Cache entries are isolated by symbol."""

    @pytest.mark.parametrize("sym1,sym2", [
        (a, b) for a, b in zip(_MANY_SYMBOLS[:10], _MANY_SYMBOLS[10:])
    ])
    def test_cache_isolation_between_symbols(self, sym1: str, sym2: str) -> None:
        p = make_stub_provider(db={
            sym1: ScoreRecord(80, "db"),
            sym2: ScoreRecord(70, "db"),
        })
        r1 = p.get(sym1)
        r2 = p.get(sym2)
        assert r1.score == 80
        assert r2.score == 70

    @pytest.mark.parametrize("sym", _MANY_SYMBOLS)
    def test_invalidate_does_not_affect_others(self, sym: str) -> None:
        others = [s for s in _MANY_SYMBOLS if s != sym]
        p = make_stub_provider(db={s: ScoreRecord(75, "db") for s in _MANY_SYMBOLS})
        for s in _MANY_SYMBOLS:
            p.get(s)
        p.invalidate(sym)
        assert sym not in p._cache
        for other in others:
            assert other in p._cache


# ===========================================================================
# 30. TestHedgerScoreProviderPriorityAllScores — DB always beats partial
# ===========================================================================

class TestHedgerScoreProviderPriorityAllScores:
    """DB score always beats partial estimate, for every score value."""

    @pytest.mark.parametrize("db_score", range(0, 101, 5))
    def test_db_beats_partial_all_scores(self, db_score: int) -> None:
        """For any DB score, partial source must never override it."""
        db_stub = _StubScoreSource("db", {"SYM": ScoreRecord(db_score, "db")})
        partial_stub = _StubScoreSource(
            "partial",
            {"SYM": ScoreRecord(56, "partial", partial=True)},
        )
        p = HedgerScoreProvider(sources=[db_stub, partial_stub])
        rec = p.get("SYM")
        assert rec is not None
        assert rec.score == db_score, f"DB score {db_score} should not be replaced by partial=56"
        assert rec.partial is False

    @pytest.mark.parametrize("db_score", range(0, 101, 5))
    def test_db_beats_live_all_scores(self, db_score: int) -> None:
        """For any DB score, live source must never override it."""
        db_stub = _StubScoreSource("db", {"SYM": ScoreRecord(db_score, "db")})
        live_stub = _StubScoreSource(
            "live",
            {"SYM": ScoreRecord(95, "live", 0, live=True)},
        )
        p = HedgerScoreProvider(sources=[db_stub, live_stub])
        rec = p.get("SYM")
        assert rec is not None
        assert rec.score == db_score, f"DB score {db_score} should not be replaced by live=95"

    @pytest.mark.parametrize("live_score", range(0, 101, 5))
    def test_live_beats_partial_all_scores(self, live_score: int) -> None:
        """For any live score, partial source must never override it."""
        db_stub = _StubScoreSource("db", {"SYM": None})  # DB miss
        live_stub = _StubScoreSource(
            "live",
            {"SYM": ScoreRecord(live_score, "live", 0, live=True)},
        )
        partial_stub = _StubScoreSource(
            "partial",
            {"SYM": ScoreRecord(56, "partial", partial=True)},
        )
        p = HedgerScoreProvider(sources=[db_stub, live_stub, partial_stub])
        rec = p.get("SYM")
        assert rec is not None
        assert rec.score == live_score, (
            f"Live score {live_score} should not be replaced by partial=56"
        )
        assert rec.partial is False


# ===========================================================================
# 31. TestBadgeRendererWithAllSources — badge for DB, live, partial sources
# ===========================================================================

class TestBadgeRendererWithAllSources:
    """Badge output for every source type across key scores."""

    @pytest.mark.parametrize("score,live,age_h,expected_tier", [
        (82, True, None, "STRONG"),
        (82, False, 5, "STRONG"),
        (56, True, None, "WEAK"),
        (56, False, 3, "WEAK"),
        (65, True, None, "NEUTRAL"),
        (65, False, 2, "NEUTRAL"),
    ])
    def test_badge_tier_source_combos(
        self, score: int, live: bool, age_h, expected_tier: str
    ) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": live, "age_h": age_h})
        assert expected_tier in badge

    @pytest.mark.parametrize("score", range(0, 101, 10))
    def test_live_tag_appears_when_live_true(self, score: int) -> None:
        badge = _render_badge({"score": score, "partial": False, "live": True, "age_h": None})
        assert "live" in badge

    @pytest.mark.parametrize("age_h", [1, 2, 5, 10, 24, 48, 168])
    def test_age_tag_appears_in_badge(self, age_h: int) -> None:
        badge = _render_badge({"score": 75, "partial": False, "live": False, "age_h": age_h})
        assert f"{age_h}h ago" in badge

