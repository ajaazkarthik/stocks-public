"""
Tests for platform_watchdog.py
Run: pytest test_platform_watchdog.py -v
All network/broker calls are mocked — no real broker connections needed.
"""
import json
import os
import sys
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).parent))
import platform_watchdog as pw


class TestMoomoCheck(unittest.TestCase):
    """TCP socket check for Moomoo OpenD."""

    def test_healthy_when_port_open(self):
        """Returns ok=True when TCP connect succeeds."""
        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=None)
            mock_conn.return_value.__exit__  = MagicMock(return_value=False)
            result = pw._check_moomoo()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertIsNotNone(result["latency_ms"])

    def test_degraded_when_connection_refused(self):
        """Returns ok=False when TCP connect raises ConnectionRefusedError."""
        with patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
            result = pw._check_moomoo()
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["error"])

    def test_degraded_on_timeout(self):
        """Returns ok=False on socket.timeout."""
        with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            result = pw._check_moomoo()
        self.assertFalse(result["ok"])

    def test_returns_host_and_port(self):
        """Detail dict always includes host and port."""
        with patch("socket.create_connection", side_effect=ConnectionRefusedError("x")):
            result = pw._check_moomoo()
        self.assertEqual(result["host"], pw.MOOMOO_HOST)
        self.assertEqual(result["port"], pw.MOOMOO_PORT)


class TestZerodhaCheck(unittest.TestCase):
    """Zerodha session health check — cache-only, NO live kite.profile() calls."""

    def _mock_zerodha_auth(self, is_valid=True, get_kite_side_effect=None,
                           cache_age_hours_return=0.5):
        """Return a mock zerodha_auth module."""
        mock_za = MagicMock()
        mock_za.is_token_valid.return_value = is_valid
        mock_za.cache_age_hours.return_value = cache_age_hours_return
        if get_kite_side_effect is not None:
            mock_za.get_kite.side_effect = get_kite_side_effect
        return mock_za

    def test_degraded_when_no_token(self):
        """Returns ok=False when is_token_valid() is False (no cache, no env var)."""
        mock_za = self._mock_zerodha_auth(is_valid=False)
        with patch.dict(sys.modules, {"zerodha_auth": mock_za}):
            result = pw._check_zerodha()
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])
        # Must NOT call kite.profile() — that would be a live Zerodha API hit
        mock_za.get_kite.assert_not_called()

    def test_healthy_when_token_valid(self):
        """Returns ok=True when a valid today-token exists in cache — no API calls."""
        mock_za = self._mock_zerodha_auth(is_valid=True, cache_age_hours_return=1.5)
        with patch.dict(sys.modules, {"zerodha_auth": mock_za}):
            result = pw._check_zerodha()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["token_age_s"], round(1.5 * 3600))
        # profile() must never be called — core rule preventing spurious Zerodha API calls
        mock_za.get_kite.return_value.profile.assert_not_called()

    def test_degraded_when_get_kite_raises(self):
        """Returns ok=False when get_kite() raises (KiteConnect import fails etc.)."""
        mock_za = self._mock_zerodha_auth(
            is_valid=True,
            get_kite_side_effect=RuntimeError("kiteconnect not installed"),
        )
        with patch.dict(sys.modules, {"zerodha_auth": mock_za}):
            result = pw._check_zerodha()
        self.assertFalse(result["ok"])
        self.assertIn("kiteconnect not installed", result["error"])

    def test_degraded_when_kiteconnect_not_installed(self):
        """Returns ok=False when zerodha_auth module is unavailable."""
        saved = sys.modules.pop("zerodha_auth", None)
        try:
            with patch.dict(sys.modules, {"zerodha_auth": None}):
                result = pw._check_zerodha()
        finally:
            if saved is not None:
                sys.modules["zerodha_auth"] = saved
        self.assertFalse(result["ok"])

    def test_no_live_profile_call_ever(self):
        """Regression: profile() must NEVER be called from _check_zerodha().

        kite.profile() makes a live Zerodha API call.  Calling it every
        90 s from the watchdog (+ twice at startup due to the init thread
        that was removed) caused 5+ Zerodha auth events within 10 s of
        startup.  This test prevents that regression.
        """
        mock_za = self._mock_zerodha_auth(is_valid=True)
        mock_kite = MagicMock()
        mock_za.get_kite.return_value = mock_kite
        with patch.dict(sys.modules, {"zerodha_auth": mock_za}):
            pw._check_zerodha()
        mock_kite.profile.assert_not_called()


class TestAngelOneCheck(unittest.TestCase):
    """SmartAPI session validation for AngelOne."""

    def test_healthy_when_session_succeeds(self):
        """Returns ok=True when generateSession returns jwtToken."""
        mock_pyotp  = MagicMock()
        mock_totp   = MagicMock()
        mock_pyotp.TOTP.return_value = mock_totp
        mock_totp.now.return_value   = "123456"

        mock_smart_cls  = MagicMock()
        mock_smart_inst = mock_smart_cls.return_value
        mock_smart_inst.generateSession.return_value = {
            "status": True,
            "data":   {"jwtToken": "Bearer eyJ_test_token_abc"},
        }
        mock_smartapi = MagicMock(SmartConnect=mock_smart_cls)

        with patch.dict(sys.modules, {"pyotp": mock_pyotp, "SmartApi": mock_smartapi}):
            result = pw._check_angelone()
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["jwt_prefix"])
        self.assertIsNone(result["error"])

    def test_degraded_when_session_fails(self):
        """Returns ok=False when generateSession returns status=False."""
        mock_pyotp  = MagicMock()
        mock_pyotp.TOTP.return_value.now.return_value = "000000"

        mock_smart_cls = MagicMock()
        mock_smart_cls.return_value.generateSession.return_value = {
            "status": False, "message": "Invalid credentials",
        }
        with patch.dict(sys.modules, {
            "pyotp": mock_pyotp,
            "SmartApi": MagicMock(SmartConnect=mock_smart_cls),
        }):
            result = pw._check_angelone()
        self.assertFalse(result["ok"])
        self.assertIn("Invalid credentials", result["error"])

    def test_degraded_when_libraries_missing(self):
        """Returns ok=False when pyotp/SmartApi not installed."""
        with patch.dict(sys.modules, {"pyotp": None, "SmartApi": None}):
            with patch("builtins.__import__", side_effect=ImportError("no pyotp")):
                result = pw._check_angelone()
        self.assertFalse(result["ok"])


class TestIsHealthy(unittest.TestCase):
    """is_healthy() query logic."""

    def setUp(self):
        """Reset internal health state before each test."""
        with pw._lock:
            for broker in ("zerodha", "angelone", "moomoo"):
                pw._health[broker] = {
                    "status": "UNKNOWN", "last_check": None, "error": None, "detail": {}
                }

    def test_unknown_broker_returns_false(self):
        self.assertFalse(pw.is_healthy("zerodha"))
        self.assertFalse(pw.is_healthy("angelone"))
        self.assertFalse(pw.is_healthy("moomoo"))

    def test_healthy_broker_returns_true(self):
        from datetime import datetime, timezone
        with pw._lock:
            pw._health["moomoo"] = {
                "status":     "HEALTHY",
                "last_check": datetime.now(timezone.utc).isoformat(),
                "error":      None,
                "detail":     {},
            }
        self.assertTrue(pw.is_healthy("moomoo"))

    def test_degraded_broker_returns_false(self):
        from datetime import datetime, timezone
        with pw._lock:
            pw._health["zerodha"] = {
                "status":     "DEGRADED",
                "last_check": datetime.now(timezone.utc).isoformat(),
                "error":      "Invalid token",
                "detail":     {},
            }
        self.assertFalse(pw.is_healthy("zerodha"))

    def test_stale_result_returns_false(self):
        """Health that's older than STALE_THRESHOLD is considered unknown."""
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=pw.STALE_THRESHOLD + 1))
        with pw._lock:
            pw._health["moomoo"] = {
                "status":     "HEALTHY",
                "last_check": old_ts.isoformat(),
                "error":      None,
                "detail":     {},
            }
        self.assertFalse(pw.is_healthy("moomoo"))

    def test_is_any_healthy_with_one_healthy(self):
        from datetime import datetime, timezone
        with pw._lock:
            pw._health["moomoo"] = {
                "status": "HEALTHY",
                "last_check": datetime.now(timezone.utc).isoformat(),
                "error": None, "detail": {},
            }
        self.assertTrue(pw.is_any_healthy())

    def test_is_any_healthy_all_unknown(self):
        self.assertFalse(pw.is_any_healthy())


class TestCheckAll(unittest.TestCase):
    """check_all() updates all brokers and writes health file."""

    def test_check_all_populates_all_brokers(self):
        """After check_all, all three brokers have a non-UNKNOWN status."""
        with patch("platform_watchdog._check_zerodha",  return_value={"ok": True,  "error": None}):
            with patch("platform_watchdog._check_angelone", return_value={"ok": False, "error": "bad"}):
                with patch("platform_watchdog._check_moomoo",   return_value={"ok": True,  "error": None}):
                    with tempfile.TemporaryDirectory() as td:
                        # Redirect health file to temp dir
                        with patch.object(pw, "HEALTH_FILE", Path(td) / "test_health.json"):
                            report = pw.check_all()

        self.assertIn("zerodha",  report)
        self.assertIn("angelone", report)
        self.assertIn("moomoo",   report)
        self.assertEqual(report["zerodha"]["status"],  "HEALTHY")
        self.assertEqual(report["angelone"]["status"], "DEGRADED")
        self.assertEqual(report["moomoo"]["status"],   "HEALTHY")

    def test_check_all_writes_health_file(self):
        """check_all writes watchdog_health.json to disk."""
        with patch("platform_watchdog._check_zerodha",  return_value={"ok": True,  "error": None}):
            with patch("platform_watchdog._check_angelone", return_value={"ok": True,  "error": None}):
                with patch("platform_watchdog._check_moomoo",   return_value={"ok": True,  "error": None}):
                    with tempfile.TemporaryDirectory() as td:
                        health_path = Path(td) / "test_health.json"
                        with patch.object(pw, "HEALTH_FILE", health_path):
                            pw.check_all()
                        self.assertTrue(health_path.exists())
                        data = json.loads(health_path.read_text(encoding="utf-8"))
                        self.assertIn("brokers", data)
                        self.assertIn("zerodha", data["brokers"])

    def test_exception_in_check_marks_degraded(self):
        """If a check function raises, that broker is marked DEGRADED."""
        with patch("platform_watchdog._check_zerodha", side_effect=RuntimeError("crash")):
            with patch("platform_watchdog._check_angelone", return_value={"ok": True, "error": None}):
                with patch("platform_watchdog._check_moomoo",  return_value={"ok": True, "error": None}):
                    with tempfile.TemporaryDirectory() as td:
                        with patch.object(pw, "HEALTH_FILE", Path(td) / "h.json"):
                            report = pw.check_all()
        self.assertEqual(report["zerodha"]["status"], "DEGRADED")


class TestGetStatusText(unittest.TestCase):
    """get_status_text() human-readable summary."""

    def test_contains_all_broker_names(self):
        with patch("platform_watchdog._check_zerodha",  return_value={"ok": True,  "error": None}):
            with patch("platform_watchdog._check_angelone", return_value={"ok": False, "error": "err"}):
                with patch("platform_watchdog._check_moomoo",   return_value={"ok": True,  "error": None}):
                    with tempfile.TemporaryDirectory() as td:
                        with patch.object(pw, "HEALTH_FILE", Path(td) / "h.json"):
                            pw.check_all()
        text = pw.get_status_text()
        self.assertIn("zerodha",  text)
        self.assertIn("angelone", text)
        self.assertIn("moomoo",   text)


class TestTokenAge(unittest.TestCase):
    """_get_token_age() sidecar file tracking."""

    def test_first_seen_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(pw, "_TOKEN_AGE_FILE", Path(td) / "ages.json"):
                pw._token_age_cache = {}
                age = pw._get_token_age("zerodha", "my_test_token_value_xyz")
        self.assertEqual(age, 0.0)

    def test_same_token_returns_increasing_age(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(pw, "_TOKEN_AGE_FILE", Path(td) / "ages.json"):
                pw._token_age_cache = {}
                pw._get_token_age("zerodha", "token_for_age_test")
                time.sleep(0.05)
                age2 = pw._get_token_age("zerodha", "token_for_age_test")
        self.assertGreater(age2, 0.0)

    def test_different_token_resets_age(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(pw, "_TOKEN_AGE_FILE", Path(td) / "ages.json"):
                pw._token_age_cache = {}
                pw._get_token_age("zerodha", "old_token")
                time.sleep(0.02)
                age_new = pw._get_token_age("zerodha", "completely_different_token")
        self.assertEqual(age_new, 0.0)


if __name__ == "__main__":
    unittest.main()
