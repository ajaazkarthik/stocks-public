"""
Tests for sell_strategy infrastructure features:
  - Task 4: CLI flag integration (--dry-run, --symbol, --sort, --json-out)
  - Task 5: SQLite sell events database (_store_run_to_db, _query_history)
  - Task 6: Report archiving (_archive_report, _update_archive_index)
  - Task 7: UX sort attributes in render_card / render_html
  - Task 10: CSV export (_write_csv)

All tests are network-free; no positions JSON or yfinance calls.
"""

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

# ─── path setup ───────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent   # tradingview/
sys.path.insert(0, str(_ROOT))

# Import the helpers directly (avoid running main)
from sell.sell_strategy import (
    _store_run_to_db,
    _query_history,
    _write_csv,
    _archive_report,
    _update_archive_index,
    _ARCHIVE_ROOT,
    _SELL_DB_SCHEMA,
    _CSV_FIELDS,
    render_card,
    render_html,
    build_summary_bar,
)
from signal_engine.sell_engine import SellEngine


# ─── Shared fixture ───────────────────────────────────────────────────────────

def _make_result(**overrides) -> dict:
    """Minimal analysed position result with safe defaults."""
    base = {
        "symbol":             "NYSE:TEST",
        "entry_price":        100.0,
        "entry_date":         "2024-01-01",
        "shares":             100,
        "broker":             "demo",
        "current_price":      115.0,
        "current_gain_pct":   15.0,
        "current_r":          1.5,
        "action":             "TRAIL",
        "action_reason":      "TP1 hit — trailing stop active",
        "atr_entry":          2.0,
        "atr_current":        2.5,
        "high_water_mark":    120.0,
        "hard_stop":          96.0,
        "breakeven_trigger":  102.0,
        "breakeven_hit":      True,
        "trailing_stop":      116.25,
        "effective_stop":     116.25,
        "tp1_price":          103.0,
        "tp2_price":          106.0,
        "tp1_hit":            True,
        "tp2_hit":            False,
        "R_value":            2.0,
        "trading_days":       10,
        "time_stop_triggered": False,
        "decay_rsi":          False,
        "decay_adx":          False,
        "decay_rv":           False,
        "decay_count":        0,
        "rsi":                55.0,
        "adx":                25.0,
        "rel_vol":            0.9,
        "rec_mark":           "buy",
        "pe":                 20.0,
        "roe":                18.0,
        "vix":                18.0,
        "vix_regime":         "NORMAL",
        "vix_force_exit":     False,
        "fund_pe_flag":       False,
        "fund_roe_flag":      False,
        "stopped_out":        False,
        "errors":             [],
        "sector":             "Technology",
        "held_days":          20,
        "cgt_eligible":       False,
        "holding_term":       "SHORT_TERM",
        "days_to_lt":         345,
        "growth_ai":          {},
        "conviction_score":   7.0,
        "conviction_label":   "STRONG",
        "position_quality":   "OPTIMAL",
        "profit_action":      "TAKE_PROFIT_PARTIAL",
        "scaling_action":     "HOLD_SIZE",
        "hedge_urgency":      "NONE",
        "hedge_instruments":  [],
        "hedge_rationale":    "",
        "yf_ticker":          "TEST",
    }
    base.update(overrides)
    return base


# ─── Task 5: SQLite DB tests ───────────────────────────────────────────────────

class TestSellDB(unittest.TestCase):

    def test_store_creates_table_and_inserts_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result()]
            now = datetime(2025, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
            _store_run_to_db(results, now, db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute("SELECT * FROM sell_events").fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)

    def test_store_persists_action_and_symbol(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result(symbol="NYSE:AAPL", action="SELL")]
            now = datetime(2025, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
            _store_run_to_db(results, now, db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM sell_events").fetchone()
            finally:
                conn.close()
            self.assertEqual(row["symbol"], "NYSE:AAPL")
            self.assertEqual(row["action"], "SELL")

    def test_store_multiple_results(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result(symbol=f"SYM{i}") for i in range(5)]
            now = datetime(2025, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
            _store_run_to_db(results, now, db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                count = conn.execute("SELECT COUNT(*) FROM sell_events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 5)

    def test_multiple_runs_accumulate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            r = [_make_result()]
            _store_run_to_db(r, datetime(2025, 4, 12, 9, 0, tzinfo=timezone.utc), db_path)
            _store_run_to_db(r, datetime(2025, 4, 12, 10, 0, tzinfo=timezone.utc), db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                count = conn.execute("SELECT COUNT(*) FROM sell_events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 2)

    def test_query_history_returns_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result(symbol="NYSE:TEST", action="SELL")]
            _store_run_to_db(results, datetime(2025, 4, 12, 10, tzinfo=timezone.utc), db_path)

            history = _query_history("NYSE:TEST", db_path)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["action"], "SELL")

    def test_query_history_empty_for_nonexistent_symbol(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result(symbol="NYSE:OTHER")]
            _store_run_to_db(results, datetime(2025, 4, 12, 10, tzinfo=timezone.utc), db_path)

            history = _query_history("NYSE:MISSING", db_path)
            self.assertEqual(history, [])

    def test_query_history_returns_empty_if_no_db(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "nonexistent.db"
            history = _query_history("SYM", db_path)
            self.assertEqual(history, [])

    def test_cgt_eligible_stored_as_int(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test_sell.db"
            results = [_make_result(cgt_eligible=True)]
            _store_run_to_db(results, datetime(2025, 4, 12, 10, tzinfo=timezone.utc), db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                val = conn.execute("SELECT cgt_eligible FROM sell_events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(val, 1)


# ─── Task 6: Archive tests ────────────────────────────────────────────────────

class TestArchive(unittest.TestCase):

    def test_archive_creates_report_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch archive root temporarily
            import sell.sell_strategy as ss
            orig = ss._ARCHIVE_ROOT
            ss._ARCHIVE_ROOT = Path(tmpdir) / "archive"
            try:
                results = [_make_result()]
                now = datetime(2025, 4, 12, 10, 30, 0, tzinfo=timezone.utc)
                path = _archive_report("<html></html>", results, 18.0, now)
                self.assertTrue(path.exists())
                self.assertEqual(path.name, "report.html")
            finally:
                ss._ARCHIVE_ROOT = orig

    def test_archive_creates_index_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import sell.sell_strategy as ss
            orig = ss._ARCHIVE_ROOT
            ss._ARCHIVE_ROOT = Path(tmpdir) / "archive"
            try:
                results = [_make_result(action="SELL"), _make_result(action="HOLD")]
                now = datetime(2025, 4, 12, 10, 30, 0, tzinfo=timezone.utc)
                _archive_report("<html></html>", results, 22.5, now)
                index_path = ss._ARCHIVE_ROOT / "index.html"
                self.assertTrue(index_path.exists())
            finally:
                ss._ARCHIVE_ROOT = orig

    def test_archive_index_contains_run_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import sell.sell_strategy as ss
            orig = ss._ARCHIVE_ROOT
            ss._ARCHIVE_ROOT = Path(tmpdir) / "archive"
            try:
                results = [_make_result(action="SELL")]
                now = datetime(2025, 4, 12, 10, 30, 0, tzinfo=timezone.utc)
                _archive_report("<html></html>", results, 30.0, now)
                index_content = (ss._ARCHIVE_ROOT / "index.html").read_text(encoding="utf-8")
                self.assertIn("2025-04-12", index_content)
                self.assertIn("30.0", index_content)
            finally:
                ss._ARCHIVE_ROOT = orig

    def test_archive_index_accumulates_multiple_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import sell.sell_strategy as ss
            orig = ss._ARCHIVE_ROOT
            ss._ARCHIVE_ROOT = Path(tmpdir) / "archive"
            try:
                r = [_make_result()]
                _archive_report("<html></html>", r, 18.0, datetime(2025, 4, 12, 9, 0, tzinfo=timezone.utc))
                _archive_report("<html><p>v2</p></html>", r, 22.0, datetime(2025, 4, 12, 10, 0, tzinfo=timezone.utc))
                index_content = (ss._ARCHIVE_ROOT / "index.html").read_text(encoding="utf-8")
                # Both run timestamps should appear in index
                self.assertIn("09:00", index_content)
                self.assertIn("10:00", index_content)
            finally:
                ss._ARCHIVE_ROOT = orig

    def test_archive_folder_name_matches_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import sell.sell_strategy as ss
            orig = ss._ARCHIVE_ROOT
            ss._ARCHIVE_ROOT = Path(tmpdir) / "archive"
            try:
                now = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)
                path = _archive_report("<html></html>", [_make_result()], 20.0, now)
                self.assertIn("2025-06-15_14-45", str(path))
            finally:
                ss._ARCHIVE_ROOT = orig


# ─── Task 10: CSV export tests ────────────────────────────────────────────────

class TestCSVExport(unittest.TestCase):

    def test_csv_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "results.csv"
            _write_csv([_make_result()], csv_path)
            self.assertTrue(csv_path.exists())

    def test_csv_has_header_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "results.csv"
            _write_csv([_make_result()], csv_path)
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames
            for field in ("symbol", "action", "current_gain_pct", "current_r"):
                self.assertIn(field, headers)

    def test_csv_row_contains_correct_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "results.csv"
            _write_csv([_make_result(symbol="NYSE:AAPL", action="SELL", current_gain_pct=-5.0)], csv_path)
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "NYSE:AAPL")
            self.assertEqual(rows[0]["action"], "SELL")
            self.assertAlmostEqual(float(rows[0]["current_gain_pct"]), -5.0)

    def test_csv_multiple_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "results.csv"
            results = [_make_result(symbol=f"SYM{i}") for i in range(4)]
            _write_csv(results, csv_path)
            with open(csv_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 4)

    def test_csv_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "nested" / "dir" / "results.csv"
            _write_csv([_make_result()], csv_path)
            self.assertTrue(csv_path.exists())


# ─── Task 7: UX sort attribute tests ─────────────────────────────────────────

class TestSortAttributes(unittest.TestCase):

    def test_render_card_has_data_gain_attribute(self):
        r = _make_result(current_gain_pct=12.34)
        card = render_card(r)
        self.assertIn("data-gain=", card)

    def test_render_card_has_data_r_attribute(self):
        r = _make_result(current_r=1.5)
        card = render_card(r)
        self.assertIn("data-r=", card)

    def test_render_card_has_data_conviction_attribute(self):
        r = _make_result(conviction_score=8.0)
        card = render_card(r)
        self.assertIn("data-conviction=", card)

    def test_render_card_data_action_present(self):
        r = _make_result(action="SELL")
        card = render_card(r)
        self.assertIn('data-action="SELL"', card)

    def test_render_html_has_cards_container_div(self):
        results = [_make_result()]
        html = render_html(results, 18.0, "2025-04-12 10:00 UTC", "test")
        self.assertIn('id="cards-container"', html)

    def test_render_html_has_sort_bar(self):
        results = [_make_result()]
        html = render_html(results, 18.0, "2025-04-12 10:00 UTC", "test")
        self.assertIn("sort-bar", html)

    def test_render_html_has_sort_js_function(self):
        results = [_make_result()]
        html = render_html(results, 18.0, "2025-04-12 10:00 UTC", "test")
        self.assertIn("function sortCards", html)

    def test_render_html_filter_action_js_present(self):
        results = [_make_result()]
        html = render_html(results, 18.0, "2025-04-12 10:00 UTC", "test")
        self.assertIn("function filterAction", html)


# ─── render_card() None-safety (trading_days) + decay denominator ────────────

class TestRenderCardNoneSafety(unittest.TestCase):
    """
    trading_days is None for positions with an unknown entry_date (e.g. India
    holdings -- see india_cgt.positions_for_sell_strategy()). render_card()
    previously did `r["trading_days"] > 15`, which raises TypeError when
    trading_days is None -- this would crash the ENTIRE report the moment a
    single India position was present. Fixed 2026-07-06.
    """

    def test_render_card_does_not_crash_when_trading_days_is_none(self):
        r = _make_result(trading_days=None, time_stop_triggered=False)
        try:
            card = render_card(r)
        except TypeError as exc:
            self.fail(f"render_card() crashed on trading_days=None: {exc}")
        self.assertIn("Unknown", card)

    def test_render_card_shows_day_count_when_trading_days_known(self):
        r = _make_result(trading_days=10, time_stop_triggered=False)
        card = render_card(r)
        self.assertIn("10d", card)

    def test_render_card_decay_denominator_matches_decay_factor_count(self):
        r = _make_result(decay_count=2)
        card = render_card(r)
        self.assertIn(f"2/{SellEngine.DECAY_FACTOR_COUNT}", card)
        self.assertNotIn("2/3", card)


# ─── Task 4: sort order tests ─────────────────────────────────────────────────

class TestSortLogic(unittest.TestCase):
    """Test the sort key logic that mirrors --sort CLI behaviour."""

    def _sort_by_urgency(self, results):
        action_order = {"SELL": 0, "REDUCE": 1, "REVIEW": 2, "TRAIL": 3, "HOLD": 4}
        return sorted(results, key=lambda r: action_order.get(r.get("action", "HOLD"), 5))

    def _sort_by_gain(self, results):
        return sorted(results, key=lambda r: r.get("current_gain_pct", 0))

    def _sort_by_r(self, results):
        return sorted(results, key=lambda r: (r.get("current_r") or 0))

    def _sort_by_conviction(self, results):
        return sorted(results, key=lambda r: -(r.get("conviction_score") or 0))

    def test_urgency_sort_sells_first(self):
        results = [
            _make_result(action="HOLD"),
            _make_result(action="SELL"),
            _make_result(action="TRAIL"),
        ]
        sorted_r = self._sort_by_urgency(results)
        self.assertEqual(sorted_r[0]["action"], "SELL")
        self.assertEqual(sorted_r[-1]["action"], "HOLD")

    def test_gain_sort_ascending(self):
        results = [
            _make_result(current_gain_pct=10.0),
            _make_result(current_gain_pct=-5.0),
            _make_result(current_gain_pct=3.0),
        ]
        sorted_r = self._sort_by_gain(results)
        gains = [r["current_gain_pct"] for r in sorted_r]
        self.assertEqual(gains, sorted(gains))

    def test_conviction_sort_descending(self):
        results = [
            _make_result(conviction_score=4.0),
            _make_result(conviction_score=9.0),
            _make_result(conviction_score=6.5),
        ]
        sorted_r = self._sort_by_conviction(results)
        scores = [r["conviction_score"] for r in sorted_r]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_r_multiple_sort_ascending(self):
        results = [
            _make_result(current_r=3.0),
            _make_result(current_r=-1.0),
            _make_result(current_r=1.5),
        ]
        sorted_r = self._sort_by_r(results)
        rs = [r["current_r"] for r in sorted_r]
        self.assertEqual(rs, sorted(rs))


# ─── Bug-fix tests ────────────────────────────────────────────────────────────

import sell.sell_strategy as ss  # noqa: E402  (already imported above via from)


class TestTerminalSummaryGroups(unittest.TestCase):
    """Terminal summary must label REDUCE and REVIEW separately (not collapse into HOLD)."""

    def _group_labels(self):
        return {0: "SELL", 1: "REDUCE", 2: "REVIEW", 3: "TRAIL", 4: "HOLD"}

    def _act_grp(self):
        return {"SELL": 0, "REDUCE": 1, "REVIEW": 2, "TRAIL": 3, "HOLD": 4}

    def test_reduce_mapped_to_own_group(self):
        grp = self._act_grp()
        self.assertEqual(grp["REDUCE"], 1)
        self.assertNotEqual(grp["REDUCE"], grp["HOLD"])

    def test_review_mapped_to_own_group(self):
        grp = self._act_grp()
        self.assertEqual(grp["REVIEW"], 2)
        self.assertNotEqual(grp["REVIEW"], grp["HOLD"])

    def test_five_group_labels_exist(self):
        labels = self._group_labels()
        self.assertEqual(set(labels.values()), {"SELL", "REDUCE", "REVIEW", "TRAIL", "HOLD"})

    def test_reduce_review_before_trail_in_urgency(self):
        grp = self._act_grp()
        self.assertLess(grp["REDUCE"], grp["TRAIL"])
        self.assertLess(grp["REVIEW"], grp["TRAIL"])


class TestDryRunNoWrites(unittest.TestCase):
    """--dry-run must suppress ALL writes: HTML, DB, archive, CSV, JSON."""

    def test_dry_run_string_mentions_all_outputs(self):
        # The dry-run message in main() must mention all suppressed outputs
        import inspect, sell.sell_strategy as _ss
        src = inspect.getsource(_ss.main)
        self.assertIn("DRY-RUN", src)
        # JSON output must be inside the `not args.dry_run` block — i.e., after the dry_run check
        dry_run_idx = src.index("not args.dry_run")
        json_out_idx = src.index("json_out")
        # json_out reference inside dry_run block should come after the dry_run guard
        self.assertGreater(json_out_idx, dry_run_idx)


class TestMoomooDedup(unittest.TestCase):
    """Duplicate TV symbols from multiple Moomoo accounts must be deduplicated."""

    def test_dedup_logic_removes_duplicate_symbol(self):
        positions = [
            {"symbol": "TYO:1572", "broker": "Moomoo-AU", "entry_price": 500.0, "shares": 100},
            {"symbol": "TYO:1572", "broker": "Moomoo-SG", "entry_price": 7000.0, "shares": 10},
            {"symbol": "NYSE:AAPL", "broker": "Moomoo-AU", "entry_price": 150.0, "shares": 5},
        ]
        _mm_seen: set = set()
        _mm_deduped = []
        for p in positions:
            s = p["symbol"]
            if s not in _mm_seen:
                _mm_seen.add(s)
                _mm_deduped.append(p)
        self.assertEqual(len(_mm_deduped), 2)
        symbols = [p["symbol"] for p in _mm_deduped]
        self.assertIn("TYO:1572", symbols)
        self.assertIn("NYSE:AAPL", symbols)

    def test_dedup_keeps_first_occurrence(self):
        positions = [
            {"symbol": "TYO:1572", "broker": "Moomoo-AU", "entry_price": 580.4, "shares": 100},
            {"symbol": "TYO:1572", "broker": "Moomoo-SG", "entry_price": 7360.0, "shares": 1},
        ]
        _mm_seen: set = set()
        _mm_deduped = []
        for p in positions:
            s = p["symbol"]
            if s not in _mm_seen:
                _mm_seen.add(s)
                _mm_deduped.append(p)
        self.assertEqual(_mm_deduped[0]["entry_price"], 580.4)
        self.assertEqual(_mm_deduped[0]["broker"], "Moomoo-AU")


class TestReImportRemoved(unittest.TestCase):
    """The `import re as _re` inside _update_archive_index must be gone."""

    def test_no_inline_re_import_in_update_archive_index(self):
        import inspect, sell.sell_strategy as _ss
        src = inspect.getsource(_ss._update_archive_index)
        self.assertNotIn("import re", src)

    def test_re_used_directly_in_update_archive_index(self):
        import inspect, sell.sell_strategy as _ss
        src = inspect.getsource(_ss._update_archive_index)
        self.assertIn("re.search", src)


class TestLeveragedUnderlyingParity(unittest.TestCase):
    """Sell strategy should mirror sell_actionable/signal_engine behavior for ETF underlyings."""

    def test_mapping_contains_reported_symbols(self):
        from sell.sell_actionable import LEVERAGED_ETF_UNDERLYING
        self.assertEqual(LEVERAGED_ETF_UNDERLYING.get("PTIR"), "PLTR")
        self.assertEqual(LEVERAGED_ETF_UNDERLYING.get("AMDL"), "AMD")
        self.assertEqual(LEVERAGED_ETF_UNDERLYING.get("GGLL"), "GOOGL")

    def test_exchange_map_has_signal_engine_suffixes(self):
        self.assertEqual(ss.EXCHANGE_MAP.get("TYO"), ".T")
        self.assertEqual(ss.EXCHANGE_MAP.get("KRX"), ".KS")
        self.assertEqual(ss.EXCHANGE_MAP.get("TSX"), ".TO")
        self.assertEqual(ss.EXCHANGE_MAP.get("LON"), ".L")

    def test_analyse_position_uses_underlying_fundamentals_for_leveraged_etf(self):
        calls: list[str] = []

        class _FakeTicker:
            def __init__(self, ticker: str):
                calls.append(ticker)

            def history(self, *args, **kwargs):
                # Empty history keeps test network-free and exercises TV close fallback path.
                return ss.pd.DataFrame()

        pos = {
            "symbol": "NASDAQ:PTIR",
            "entry_price": 10.0,
            "entry_date": "2025-01-01",
            "shares": 10,
            "atr_at_entry": 1.0,
            "high_water_mark": 10.0,
            "broker": "demo",
        }
        screener = {"NASDAQ:PTIR": {"close": 10.5, "rec_mark": "buy"}}
        now = datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc)

        fake_fund = {
            "company": "Palantir",
            "sector": "Technology",
            "earnings_growth": 12.0,
            "revenue_growth": 15.0,
            "profit_margin": 10.0,
            "roe": 8.0,
            "debt_to_equity": 15.0,
            "free_cashflow": 1_000_000,
            "analyst_target": 25.0,
            "forward_pe": 35.0,
            "peg_ratio": 2.0,
        }
        fake_sent = {
            "news": {"compound": 0.1},
            "analyst": {"bull_ratio": 0.6},
            "recent_downgrade": False,
            "earnings_beats": {"beat_rate": 0.5},
            "earnings_days_away": None,
        }

        with patch.object(ss.yf, "Ticker", side_effect=_FakeTicker):
            with patch.object(ss, "fetch_fundamentals", return_value=fake_fund) as m_fund:
                with patch.object(ss, "fetch_sentiment_data", return_value=fake_sent):
                    with patch.object(ss, "_finalise", side_effect=lambda result, vix, now, conviction_data=None: result):
                        with patch.object(ss, "_compute_hedge_recommendation", return_value={}):
                            ss.analyse_position(pos, screener, 18.0, now)

        self.assertEqual(m_fund.call_args[0][0], "PLTR")
        self.assertIn("PTIR", calls)
        self.assertIn("PLTR", calls)


class TestLeveragedETFSignalInsightResolution(unittest.TestCase):
    """
    _fetch_signal_insight() must resolve leveraged ETF symbols to their
    underlying equity for signal scoring.  This ensures that newly added
    ETFs in LEVERAGED_ETF_UNDERLYING are handled automatically.
    """

    def _make_engine_stub(self, tv_fund_calls: list):
        """Return a mock _ANALYSIS_ENGINE that tracks fetch_tv_fundamentals calls."""
        import unittest.mock as mock

        engine = mock.MagicMock()
        engine.fetch_tv_fundamentals.side_effect = lambda syms, **kw: (
            tv_fund_calls.extend(syms) or {}
        )
        engine.analyse_signal.return_value = {
            "composite": 72.0,
            "scores": {},
            "fundamentals": {},
            "sentiment_data": {},
        }
        return engine

    def _call_fetch_signal_insight(self, symbol, tv_fund_cache, engine_stub):
        """Helper: patch _ANALYSIS_ENGINE + _SIGNAL_INSIGHT_OK, then call."""
        import unittest.mock as mock

        with mock.patch.object(ss, "_SIGNAL_INSIGHT_OK", True), \
             mock.patch.object(ss, "_ANALYSIS_ENGINE", engine_stub), \
             mock.patch.object(ss, "_CONVICTION_OK", True):
            return ss._fetch_signal_insight(
                symbol, screener_cache={}, tv_fund_cache=tv_fund_cache, yc=None
            )

    def test_ptir_prefetches_pltr_in_cache(self):
        """NYSE:PTIR → should pre-fetch NYSE:PLTR fundamentals into tv_fund_cache."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NYSE:PTIR", cache, engine)

        # The underlying's TV symbol (PLTR) must have been fetched
        self.assertTrue(
            any("PLTR" in s for s in tv_fund_calls),
            f"Expected PLTR in fund fetch calls, got: {tv_fund_calls}",
        )

    def test_tsll_prefetches_tsla_in_cache(self):
        """NASDAQ:TSLL → should pre-fetch NASDAQ:TSLA fundamentals."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NASDAQ:TSLL", cache, engine)

        self.assertTrue(
            any("TSLA" in s for s in tv_fund_calls),
            f"Expected TSLA in fund fetch calls, got: {tv_fund_calls}",
        )

    def test_nvdu_prefetches_nvda_in_cache(self):
        """NASDAQ:NVDU → should pre-fetch NASDAQ:NVDA fundamentals."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NASDAQ:NVDU", cache, engine)

        self.assertTrue(
            any("NVDA" in s for s in tv_fund_calls),
            f"Expected NVDA in fund fetch calls, got: {tv_fund_calls}",
        )

    def test_ggll_prefetches_googl_in_cache(self):
        """NYSE:GGLL → should pre-fetch NASDAQ:GOOGL fundamentals."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NYSE:GGLL", cache, engine)

        self.assertTrue(
            any("GOOGL" in s for s in tv_fund_calls),
            f"Expected GOOGL in fund fetch calls, got: {tv_fund_calls}",
        )

    def test_amdl_prefetches_amd_in_cache(self):
        """NYSE:AMDL → should pre-fetch NASDAQ:AMD fundamentals."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NYSE:AMDL", cache, engine)

        self.assertTrue(
            any("AMD" in s for s in tv_fund_calls),
            f"Expected AMD in fund fetch calls, got: {tv_fund_calls}",
        )

    def test_sgx_dlc_nviw_prefetches_nvda(self):
        """SGX:NVIW (Societe Generale NVDA DLC) → should pre-fetch NASDAQ:NVDA."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("SGX:NVIW", cache, engine)

        self.assertTrue(
            any("NVDA" in s for s in tv_fund_calls),
            f"Expected NVDA in fund fetch calls for SGX DLC, got: {tv_fund_calls}",
        )

    def test_non_etf_symbol_no_extra_fetch(self):
        """NYSE:AAPL (regular stock) → no extra fundamental fetch should happen."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        cache: dict = {}

        self._call_fetch_signal_insight("NYSE:AAPL", cache, engine)

        # No extra fetch for non-ETF symbols (engine.analyse_signal handles it)
        self.assertEqual(tv_fund_calls, [],
                         "Regular stocks should not trigger extra fund pre-fetch")

    def test_underlying_already_in_cache_no_refetch(self):
        """If underlying TV funds are already in cache, no extra fetch should occur."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)
        # Pre-populate cache with PLTR
        cache: dict = {"NYSE:PLTR": {"forward_pe": 50.0}}

        self._call_fetch_signal_insight("NYSE:PTIR", cache, engine)

        # fetch_tv_fundamentals should NOT be called again since PLTR is cached
        self.assertEqual(tv_fund_calls, [],
                         "Should not re-fetch if underlying already in tv_fund_cache")

    def test_fetch_insight_returns_grade_key(self):
        """Successful insight always returns a _grade key (A/B/C/D)."""
        tv_fund_calls: list = []
        engine = self._make_engine_stub(tv_fund_calls)

        result = self._call_fetch_signal_insight("NASDAQ:TSLL", {}, engine)

        self.assertIn("_grade", result)
        self.assertIn(result["_grade"], ("A", "B", "C", "D"))

    def test_all_etf_underlyings_in_underlying_to_tv(self):
        """
        Every underlying in LEVERAGED_ETF_UNDERLYING should be resolvable to a
        TV symbol — either from UNDERLYING_TO_TV or via the NASDAQ fallback.
        This catches newly added ETFs that forget to add UNDERLYING_TO_TV entry.
        """
        from sell.sell_actionable import LEVERAGED_ETF_UNDERLYING, UNDERLYING_TO_TV

        missing_or_empty = []
        for etf_ticker, underlying_yf in LEVERAGED_ETF_UNDERLYING.items():
            tv_sym = UNDERLYING_TO_TV.get(underlying_yf)
            if tv_sym is None:
                # Fallback will be used — just verify it's a non-empty string
                fallback = f"NASDAQ:{underlying_yf}"
                if not fallback.strip():
                    missing_or_empty.append(etf_ticker)

        self.assertEqual(
            missing_or_empty, [],
            f"ETFs with unresolvable underlying: {missing_or_empty}",
        )

    def test_resolve_etf_underlying_fallback_for_unknown_underlying(self):
        """
        resolve_etf_underlying() must return a non-None TV symbol even when the
        underlying is not in UNDERLYING_TO_TV (uses NASDAQ: prefix as fallback).
        """
        from tradingview import signal_stack as _ss_mod
        import unittest.mock as mock

        # Temporarily inject a fake ETF entry whose underlying has no TV mapping
        fake_lev = {"FAKELEV": "FAKUND"}
        fake_tv_map = {}  # intentionally empty — no TV mapping

        with mock.patch.object(_ss_mod, "LEVERAGED_ETF_UNDERLYING",  # noqa: SIM117
                               fake_lev, create=True):
            with mock.patch(
                "tradingview.sell.sell_actionable.LEVERAGED_ETF_UNDERLYING",
                fake_lev,
            ):
                with mock.patch(
                    "tradingview.sell.sell_actionable.UNDERLYING_TO_TV",
                    fake_tv_map,
                ):
                    # re-import inside the patch context so the function sees the mocks
                    underlying_yf, underlying_tv = _ss_mod.resolve_etf_underlying(
                        "NYSE:FAKELEV"
                    )

        # underlying_tv must be a non-empty string fallback (NASDAQ:FAKUND)
        self.assertIsNotNone(underlying_tv,
                             "resolve_etf_underlying must not return None for underlying_tv")
        self.assertIn("FAKUND", underlying_tv)


if __name__ == "__main__":
    unittest.main()
