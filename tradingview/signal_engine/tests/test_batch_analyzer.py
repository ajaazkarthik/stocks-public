"""
Unit tests for BatchAnalyzer — fire loading, deduplication, report saving.

External APIs (TV screener, yfinance) are mocked so no network access is needed.
Run with:  python -m pytest signal_engine/tests/ -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from signal_engine.analysis_engine import AnalysisEngine
from signal_engine.batch_analyzer  import BatchAnalyzer


# ─── helper builders ──────────────────────────────────────────────────────────

def _make_fire(sym: str, resolution: str = "60") -> dict:
    return {
        "pro_symbol": sym, "symbol": sym,
        "resolution": resolution,
        "message": f"BUY {sym.split(':')[-1]} price: 100.00, WinRate: 65.00%",
        "fire_time": "2026-04-06T12:00:00+00:00",
    }

def _make_fires_json(fires: list[dict]) -> dict:
    return {"all": fires, "win2h": [], "win1h": []}

def _make_result(sym: str, comp: float) -> dict:
    """Minimal analyse_signal result dict."""
    return {
        "tv_symbol": sym, "yf_ticker": sym, "fire_time": "2026-04-06T12:00:00+00:00",
        "resolution": "60", "meta": {}, "fundamentals": {},
        "sentiment_data": {}, "market_internals": {}, "l5_enrichment": None,
        "l6_enrichment": None, "yield_curve": {}, "in_pool": None,
        "pool_boost": 0.0, "is_etf": False, "etf_label": "",
        "composite": comp, "composite_raw": comp,
        "scores": {
            "l1": {"total": comp / 10},
            "l2": {"total": comp / 10},
            "l3": {"total": comp / 10},
            "l4": {"total": comp / 10},
        },
    }


class TestBatchAnalyzerLoadFires(unittest.TestCase):
    """BatchAnalyzer.load_fires — JSON reading, dedup, N-limit."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def _write_fires(self, tmp_dir: Path, fires: list[dict]) -> Path:
        fp = tmp_dir / "fires_raw.json"
        fp.write_text(json.dumps(_make_fires_json(fires)), encoding="utf-8")
        return fp

    def test_loads_n_files(self):
        with tempfile.TemporaryDirectory() as td:
            fires = [_make_fire(f"NSE:SYM{i}") for i in range(20)]
            fp    = self._write_fires(Path(td), fires)
            ba    = BatchAnalyzer(self.eng, fires_file=fp, n=10)
            loaded = ba.load_fires()
            self.assertEqual(len(loaded), 10)

    def test_deduplication_removes_repeat_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            fires = [_make_fire("NSE:TITAN")] * 5 + [_make_fire("NSE:MCX")] * 3
            fp    = self._write_fires(Path(td), fires)
            ba    = BatchAnalyzer(self.eng, fires_file=fp, n=50, deduplicate=True)
            loaded = ba.load_fires()
            syms = [f["pro_symbol"] for f in loaded]
            # Each symbol must appear at most once
            self.assertEqual(len(syms), len(set(syms)))

    def test_no_dedup_allows_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            fires = [_make_fire("NSE:TITAN")] * 3
            fp    = self._write_fires(Path(td), fires)
            ba    = BatchAnalyzer(self.eng, fires_file=fp, n=50, deduplicate=False)
            loaded = ba.load_fires()
            self.assertEqual(len(loaded), 3)

    def test_all_bucket_used(self):
        """load_fires must read the 'all' key, not win2h/win1h."""
        with tempfile.TemporaryDirectory() as td:
            fires_data = {
                "all":   [_make_fire("NSE:TITAN")],
                "win2h": [_make_fire("NSE:FAKE_NOT_USED")],
            }
            fp = Path(td) / "fires_raw.json"
            fp.write_text(json.dumps(fires_data), encoding="utf-8")
            ba = BatchAnalyzer(self.eng, fires_file=fp, n=50)
            loaded = ba.load_fires()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["pro_symbol"], "NSE:TITAN")


class TestBatchAnalyzerSaveReport(unittest.TestCase):
    """BatchAnalyzer.save_report / save_actionable_report — file output."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_save_report_creates_html(self):
        with tempfile.TemporaryDirectory() as td:
            ranked = [_make_result("NSE:TITAN", 72.0), _make_result("NSE:MCX", 55.0)]
            # Mock engine.build_html so we don't need full signal_stack rendering
            self.eng.build_html = MagicMock(return_value="<html>mock</html>")
            ba = BatchAnalyzer(self.eng, fires_file=Path(td) / "f.json", n=50, threshold=50)
            out = Path(td) / "report.html"
            ba.save_report(ranked, output_path=out)
            self.assertTrue(out.exists())
            self.eng.build_html.assert_called_once()

    def test_save_actionable_filters_by_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            ranked = [
                _make_result("NSE:TITAN", 72.0),   # above 60
                _make_result("NSE:MCX",   45.0),   # below 60
                _make_result("NSE:HDFC",  65.0),   # above 60
            ]
            self.eng.build_html = MagicMock(return_value="<html>mock</html>")
            ba = BatchAnalyzer(self.eng, fires_file=Path(td) / "f.json",
                               n=50, threshold=60)
            out = Path(td) / "actionable.html"
            _, count = ba.save_actionable_report(ranked, output_path=out)
            self.assertEqual(count, 2,
                             "Expected 2 signals above threshold=60")
            # Verify build_html was called with only the 2 above-threshold results
            # call_args[0] is the positional-args tuple; [0][0] is the ranked list
            actionable_passed = self.eng.build_html.call_args[0][0]
            self.assertEqual(len(actionable_passed), 2)

    def test_run_and_save_with_mocked_engine(self):
        """Full run_and_save flow with all external calls mocked."""
        with tempfile.TemporaryDirectory() as td:
            fires = [_make_fire("NSE:TITAN"), _make_fire("NSE:MCX")]
            fp    = Path(td) / "fires_raw.json"
            fp.write_text(json.dumps(_make_fires_json(fires)), encoding="utf-8")

            mock_engine = MagicMock(spec=AnalysisEngine)
            mock_engine.fetch_tv_screener.return_value      = {}
            mock_engine.fetch_tv_fundamentals.return_value  = {}
            mock_engine.fetch_yield_curve.return_value      = {}
            mock_engine.analyse_signal.side_effect = lambda fire, **kw: _make_result(
                fire["pro_symbol"], 68.0
            )
            mock_engine.build_html.return_value = "<html>mock</html>"

            ba = BatchAnalyzer(mock_engine, fires_file=fp, n=50, threshold=50)
            ranked = ba.run_and_save(
                report_out=Path(td) / "report.html",
                actionable_out=Path(td) / "actionable.html",
            )
            self.assertEqual(len(ranked), 2)
            self.assertEqual(mock_engine.analyse_signal.call_count, 2)


# ─── Edge cases for load_fires ────────────────────────────────────────────────

class TestBatchAnalyzerEdgeCases(unittest.TestCase):
    """Edge cases: missing file, malformed JSON, empty fires list."""

    def _make_analyzer(self, fires_file: Path, n: int = 10) -> BatchAnalyzer:
        engine = MagicMock(spec=AnalysisEngine)
        return BatchAnalyzer(engine=engine, fires_file=fires_file, n=n)

    def test_load_fires_raises_for_missing_file(self):
        """FileNotFoundError when the fires file does not exist."""
        ba = self._make_analyzer(Path("/nonexistent/fires_raw.json"))
        with self.assertRaises(FileNotFoundError):
            ba.load_fires()

    def test_load_fires_raises_for_malformed_json(self):
        """json.JSONDecodeError for a file that is not valid JSON."""
        import json
        with tempfile.TemporaryDirectory() as td:
            bad_file = Path(td) / "fires_raw.json"
            bad_file.write_text("this is not json", encoding="utf-8")
            ba = self._make_analyzer(bad_file)
            with self.assertRaises(json.JSONDecodeError):
                ba.load_fires()

    def test_load_fires_empty_all_key(self):
        """File with empty 'all' array returns an empty list."""
        import json
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "fires_raw.json"
            f.write_text(json.dumps({"all": []}), encoding="utf-8")
            ba = self._make_analyzer(f)
            fires = ba.load_fires()
            self.assertEqual(fires, [])

    def test_load_fires_missing_all_key_returns_empty(self):
        """File with no 'all' key treats it as empty list."""
        import json
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "fires_raw.json"
            f.write_text(json.dumps({"win2h": []}), encoding="utf-8")
            ba = self._make_analyzer(f)
            fires = ba.load_fires()
            self.assertEqual(fires, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
