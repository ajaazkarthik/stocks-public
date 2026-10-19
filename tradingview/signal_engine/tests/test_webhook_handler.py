"""
Unit tests for WebhookServer — payload parsing and fire processing logic.

All external I/O is mocked; no network calls and no real HTTP server is started
during the lightweight tests.  The integration test (``TestWebhookHTTP``) spins
up and tears down a real server on a high loopback port — it is skipped
automatically if that port is already in use.

Run with:  python -m pytest signal_engine/tests/ -v
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from signal_engine.analysis_engine import AnalysisEngine
from signal_engine.webhook_handler  import WebhookServer


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_result(sym: str, comp: float) -> dict:
    return {
        "tv_symbol": sym, "yf_ticker": sym, "composite": comp,
        "composite_raw": comp,
        "fire_time": "2026-04-06T12:00:00+00:00",
        "resolution": "60", "meta": {},
        "fundamentals": {}, "sentiment_data": {},
        "market_internals": {}, "l5_enrichment": None,
        "l6_enrichment": None, "yield_curve": {}, "in_pool": None,
        "pool_boost": 0.0, "is_etf": False, "etf_label": "",
        "scores": {
            "l1": {"total": comp / 10},
            "l2": {"total": comp / 10},
            "l3": {"total": comp / 10},
            "l4": {"total": comp / 10},
        },
    }


# ─── Parse payload ────────────────────────────────────────────────────────────

class TestParsePayloadJson(unittest.TestCase):
    """WebhookServer.parse_payload — JSON body with well-formed data."""

    def test_json_symbol_and_resolution(self):
        body = json.dumps({
            "s": "NSE:TITAN", "r": "60",
            "msg": "BUY TITAN price: 3450.00, WinRate: 65.00%"
        }).encode()
        fire = WebhookServer.parse_payload(body, {})
        self.assertEqual(fire["pro_symbol"], "NSE:TITAN")
        self.assertEqual(fire["resolution"], "60")

    def test_json_symbol_uppercased(self):
        body = json.dumps({
            "s": "nse:titan", "r": "D",
            "msg": "BUY titan price: 3450.00, WinRate: 65%"
        }).encode()
        fire = WebhookServer.parse_payload(body, {})
        self.assertEqual(fire["pro_symbol"], "NSE:TITAN")

    def test_json_exchange_plus_ticker_fields(self):
        """{"exchange": "NSE", "ticker": "MCX"} → NSE:MCX"""
        body = json.dumps({
            "exchange": "NSE", "ticker": "MCX",
            "r": "60", "msg": "BUY MCX price: 6000, WinRate: 70%"
        }).encode()
        fire = WebhookServer.parse_payload(body, {})
        self.assertEqual(fire["pro_symbol"], "NSE:MCX")

    def test_close_price_captured(self):
        body = json.dumps({
            "s": "NSE:TITAN", "r": "60", "close": "3450.5",
            "msg": "BUY TITAN price: 3450.5, WinRate: 65%"
        }).encode()
        fire = WebhookServer.parse_payload(body, {})
        self.assertAlmostEqual(fire["close"], 3450.5)


class TestParsePayloadPineFallback(unittest.TestCase):
    """WebhookServer.parse_payload — Pine strategy body (no JSON, unresolved templates)."""

    def test_pine_body_extracts_symbol(self):
        """Pure text body from a Pine strategy alert (no JSON wrapper)."""
        body = b"BUY TITAN price: 3450.00, WinRate: 65.00%, NetProfit%: 120"
        # query has unresolved exchange — should be dropped
        query = {"exchange": ["{{exchange}}"], "s": ["{{exchange}}:{{ticker}}"]}
        fire  = WebhookServer.parse_payload(body, query)
        # Symbol should be parsed from body text; exchange may be empty or inferred
        self.assertTrue(fire["pro_symbol"].endswith("TITAN"),
                        f"Expected TITAN suffix, got {fire['pro_symbol']!r}")

    def test_inline_exchange_colon_ticker_in_body(self):
        """Body text directly contains EXCHANGE:TICKER."""
        body = b"NSE:KAYNES price: 2800.00, WinRate: 60.00%  action: BUY"
        fire = WebhookServer.parse_payload(body, {})
        self.assertIn("KAYNES", fire["pro_symbol"])


class TestParsePayloadValidation(unittest.TestCase):
    """WebhookServer.parse_payload — error cases."""

    def test_unresolved_template_raises(self):
        body = json.dumps({
            "s": "{{exchange}}:{{ticker}}", "r": "60",
            "msg": "BUY SOMETHING price: 100, WinRate: 60%"
        }).encode()
        with self.assertRaises(ValueError):
            WebhookServer.parse_payload(body, {})

    def test_empty_body_raises(self):
        with self.assertRaises(ValueError):
            WebhookServer.parse_payload(b"{}", {})

    def test_invalid_symbol_chars_raises(self):
        body = json.dumps({
            "s": "../../etc/passwd", "r": "60", "msg": "BUY x price: 1"
        }).encode()
        with self.assertRaises(ValueError):
            WebhookServer.parse_payload(body, {})


# ─── Fire processing ──────────────────────────────────────────────────────────

class TestProcessFire(unittest.TestCase):
    """WebhookServer._process_fire — threshold gating and side-effects."""

    def _make_server(self, tmp_dir: Path, threshold: int) -> WebhookServer:
        mock_eng = MagicMock(spec=AnalysisEngine)
        mock_eng.grade.return_value = ("B", "#16a34a")
        srv = WebhookServer(
            engine=mock_eng, port=9999, threshold=threshold,
            fires_file=tmp_dir / "fires_raw.json",
            actionable_log=tmp_dir / "actionable_log.json",
            actionable_report=tmp_dir / "actionable_report.html",
            stack_report=tmp_dir / "signal_stack_report.html",
            analysis_log=tmp_dir / "analysis.log",
        )
        return srv

    def test_below_threshold_not_added_to_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = self._make_server(tmp, threshold=55)
            # Analyse returns 45 — below threshold
            srv.engine.analyse_signal.return_value = _make_result("NSE:MCX", 45.0)
            srv.engine.build_html = MagicMock(return_value="<html></html>")

            fire = {"pro_symbol": "NSE:MCX", "symbol": "NSE:MCX",
                    "resolution": "60", "message": "BUY MCX price: 100",
                    "fire_time": "2026-04-06T12:00:00+00:00"}
            srv._process_fire(fire)

            self.assertEqual(len(srv._actionable), 0)

    def test_above_threshold_added_to_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = self._make_server(tmp, threshold=55)
            srv.engine.analyse_signal.return_value = _make_result("NSE:TITAN", 72.0)
            srv.engine.build_html = MagicMock(return_value="<html></html>")

            fire = {"pro_symbol": "NSE:TITAN", "symbol": "NSE:TITAN",
                    "resolution": "60", "message": "BUY TITAN price: 3450",
                    "fire_time": "2026-04-06T12:00:00+00:00"}
            srv._process_fire(fire)

            self.assertEqual(len(srv._actionable), 1)
            self.assertEqual(srv._actionable[0]["tv_symbol"], "NSE:TITAN")

    def test_above_threshold_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = self._make_server(tmp, threshold=55)
            srv.engine.analyse_signal.return_value = _make_result("NSE:TITAN", 72.0)
            srv.engine.build_html = MagicMock(return_value="<html>actionable report</html>")

            fire = {"pro_symbol": "NSE:TITAN", "symbol": "NSE:TITAN",
                    "resolution": "60", "message": "BUY TITAN price: 3450",
                    "fire_time": "2026-04-06T12:00:00+00:00"}
            srv._process_fire(fire)

            report = srv.actionable_report
            self.assertTrue(report.exists(), "Actionable HTML report not created")

    def test_analysis_log_always_written(self):
        """Every analysed fire should be appended to the analysis log."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = self._make_server(tmp, threshold=55)
            srv.engine.analyse_signal.return_value = _make_result("NSE:HDFC", 40.0)
            srv.engine.build_html = MagicMock(return_value="<html></html>")

            fire = {"pro_symbol": "NSE:HDFC", "symbol": "NSE:HDFC",
                    "resolution": "60", "message": "BUY HDFC price: 1650",
                    "fire_time": "2026-04-06T12:00:00+00:00"}
            srv._process_fire(fire)

            log_path = srv.analysis_log
            self.assertTrue(log_path.exists(), "Analysis log not created")
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("HDFC", content)
            self.assertIn("40", content)  # composite score in log

    def test_duplicate_processing_skipped(self):
        """test_duplicate_processing_skipped is an integration concern tested in
        TestWebhookHTTP.test_duplicate_debounce. The dedup guard lives in the
        HTTP handler, not in _process_fire, so this placeholder passes."""
        pass


# ─── HTTP integration test ────────────────────────────────────────────────────

class TestWebhookHTTP(unittest.TestCase):
    """
    Spin up a real WebhookServer on localhost:19876, send HTTP requests,
    verify responses — all analysis calls are mocked.
    """

    _PORT = 19876

    @classmethod
    def setUpClass(cls):
        try:
            import socket
            s = socket.socket()
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", cls._PORT))
            s.close()
            if result == 0:
                raise unittest.SkipTest(f"Port {cls._PORT} already in use")
        except OSError:
            pass

        mock_eng = MagicMock(spec=AnalysisEngine)
        mock_eng.analyse_signal.return_value = _make_result("NSE:TITAN", 40.0)
        mock_eng.grade.return_value = ("C", "#ca8a04")
        mock_eng.build_html = MagicMock(return_value="<html></html>")

        with tempfile.TemporaryDirectory() as td:
            cls._tmp_dir = Path(td)

        cls._tmp_dir.mkdir(parents=True, exist_ok=True)

        cls._server = WebhookServer(
            engine=mock_eng, port=cls._PORT,
            fires_file=cls._tmp_dir / "fires_raw.json",
            actionable_log=cls._tmp_dir / "actionable_log.json",
            actionable_report=cls._tmp_dir / "actionable_report.html",
            analysis_log=cls._tmp_dir / "analysis.log",
        )
        cls._thread = threading.Thread(target=cls._server.start, daemon=True)
        cls._thread.start()
        time.sleep(0.3)   # give server time to bind

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_server"):
            cls._server.stop()

    def test_health_endpoint_returns_200(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._PORT, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        conn.close()

    def test_webhook_post_returns_200(self):
        payload = json.dumps({
            "s": "NSE:TITAN", "r": "60",
            "msg": "BUY TITAN price: 3450.00, WinRate: 65.00%"
        }).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self._PORT, timeout=3)
        conn.request("POST", "/webhook", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertIn(resp.status, (200, 202))
        conn.close()

    def test_webhook_bad_payload_returns_400(self):
        payload = json.dumps({
            "s": "{{exchange}}:{{ticker}}", "r": "60",
            "msg": "BUY x price: 100"
        }).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self._PORT, timeout=3)
        conn.request("POST", "/webhook", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_health_json_has_actionable_count(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._PORT, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        # Health response uses 'actionable_count' key
        self.assertIn("actionable_count", body)
        conn.close()

    def test_duplicate_debounce(self):
        """Sending the same symbol twice in quick succession → second gets 202 debounced."""
        payload = json.dumps({
            "s": "NSE:RELAUTO", "r": "60",
            "msg": "BUY RELAUTO price: 1200.00, WinRate: 65.00%"
        }).encode()
        # Inject sym into _processing to simulate first request in-flight
        with self._server._lock:
            self._server._processing.add("NSE:RELAUTO")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self._PORT, timeout=3)
            conn.request("POST", "/webhook", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            self.assertEqual(resp.status, 202)
            self.assertEqual(body.get("status"), "debounced")
            conn.close()
        finally:
            with self._server._lock:
                self._server._processing.discard("NSE:RELAUTO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
