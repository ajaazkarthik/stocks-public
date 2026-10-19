"""
Unit tests for webull_au_api.py — Webull AU official OpenAPI client
(pure HTTP + manual HMAC-SHA256 signing, no third-party SDK).

All tests are network-free — `requests.get`/`requests.post` are mocked.
Only pure-logic / mapping / signing functions are exercised.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import webull_au_api as W


def _fake_response(status_code: int, json_data):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = "" if status_code == 200 else "error body"
    return resp


class TestIsConfigured(unittest.TestCase):
    def test_false_when_app_key_missing(self):
        with patch.object(W, "_APP_KEY", ""), patch.object(W, "_APP_SECRET", "secret"):
            self.assertFalse(W.is_configured())

    def test_false_when_app_secret_missing(self):
        with patch.object(W, "_APP_KEY", "key"), patch.object(W, "_APP_SECRET", ""):
            self.assertFalse(W.is_configured())

    def test_true_when_fully_configured(self):
        with patch.object(W, "_APP_KEY", "key"), patch.object(W, "_APP_SECRET", "secret"):
            self.assertTrue(W.is_configured())


class TestSignature(unittest.TestCase):
    """The signing algorithm itself — deterministic + sensitive to every input."""

    def _sign(self, **overrides):
        defaults = dict(
            path="/openapi/account/list",
            query_params={},
            body_string=None,
            timestamp="2026-01-01T00:00:00Z",
            nonce="fixed-nonce-for-test",
        )
        defaults.update(overrides)
        with patch.object(W, "_APP_KEY", "test-app-key"), \
             patch.object(W, "_APP_SECRET", "test-app-secret"), \
             patch.object(W, "_API_HOST", "api.webull.com.au"):
            return W._generate_signature(**defaults)

    def test_deterministic_for_same_inputs(self):
        self.assertEqual(self._sign(), self._sign())

    def test_changes_with_different_path(self):
        self.assertNotEqual(self._sign(path="/openapi/assets/positions"), self._sign())

    def test_changes_with_different_nonce(self):
        self.assertNotEqual(self._sign(nonce="different-nonce"), self._sign())

    def test_changes_with_different_body(self):
        self.assertNotEqual(
            self._sign(body_string='{"a":1}'),
            self._sign(body_string='{"a":2}'),
        )

    def test_produces_valid_base64_hmac_sha256_length(self):
        import base64
        sig = self._sign()
        decoded = base64.b64decode(sig)
        self.assertEqual(len(decoded), 32)  # HMAC-SHA256 digest is always 32 bytes

    def test_query_params_included_in_signature(self):
        self.assertNotEqual(
            self._sign(query_params={"account_id": "123"}),
            self._sign(query_params={"account_id": "456"}),
        )


class TestExchangeForCurrency(unittest.TestCase):
    def test_aud_maps_to_au(self):
        self.assertEqual(W._exchange_for_currency("AUD"), "AU")

    def test_hkd_maps_to_hk(self):
        self.assertEqual(W._exchange_for_currency("HKD"), "HK")

    def test_cnh_maps_to_hk(self):
        self.assertEqual(W._exchange_for_currency("CNH"), "HK")

    def test_sgd_maps_to_sg(self):
        self.assertEqual(W._exchange_for_currency("SGD"), "SG")

    def test_usd_maps_to_us(self):
        self.assertEqual(W._exchange_for_currency("USD"), "US")

    def test_unknown_defaults_to_us(self):
        self.assertEqual(W._exchange_for_currency(""), "US")


class TestGetHoldingsNotConfigured(unittest.TestCase):
    def test_returns_empty_with_status_when_not_configured(self):
        with patch.object(W, "_APP_KEY", ""), patch.object(W, "_APP_SECRET", ""):
            positions, status = W.get_holdings()
        self.assertEqual(positions, [])
        self.assertIn("not configured", status.lower())


class TestGetBalanceNotConfigured(unittest.TestCase):
    def test_returns_none_with_status_when_not_configured(self):
        with patch.object(W, "_APP_KEY", ""), patch.object(W, "_APP_SECRET", ""):
            balance, status = W.get_balance()
        self.assertIsNone(balance)
        self.assertIn("not configured", status.lower())


class TestGetHoldingsMapping(unittest.TestCase):
    """Verify the AssetsPositionResult schema is mapped to the normalized
    dashboard/sell_strategy shape (see developer.webull.com.au/apis/docs/
    reference/account-position). get_access_token / _resolve_account_id are
    mocked so no real HTTP or 2FA flow is exercised."""

    def _configured(self):
        return patch.multiple(W, _APP_KEY="key", _APP_SECRET="secret")

    def test_maps_official_schema_fields(self):
        raw = [{
            "position_id": "N4I4SIM8TJF38KN2TAA0QVVNE9",
            "currency": "USD",
            "quantity": "10",
            "symbol": "AAPL",
            "instrument_type": "EQUITY",
            "last_price": "150.0",
            "cost_price": "120.0",
            "unrealized_profit_loss": "300.0",
        }]
        with self._configured(), \
             patch.object(W, "get_access_token", return_value="tok"), \
             patch.object(W, "_resolve_account_id", return_value="acct123"), \
             patch.object(W, "_call", return_value=_fake_response(200, raw)) as mock_call:
            positions, status = W.get_holdings()

        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p["symbol"], "AAPL")
        self.assertEqual(p["shares"], 10)
        self.assertEqual(p["avg_cost"], 120.0)
        self.assertEqual(p["current_price"], 150.0)
        self.assertEqual(p["market_val"], 1500.0)
        self.assertEqual(p["pnl"], 300.0)
        self.assertEqual(p["currency"], "USD")
        self.assertEqual(p["broker"], "Webull AU")
        self.assertEqual(p["exchange"], "US")
        self.assertIn("Live API", status)
        # positions endpoint called with the resolved account_id
        mock_call.assert_called_once()
        self.assertEqual(mock_call.call_args[0][1], "/openapi/assets/positions")

    def test_hk_and_au_positions_map_correctly(self):
        """Regression: the API returns ALL markets (verified live), not just US."""
        raw = [
            {"symbol": "07200", "currency": "HKD", "quantity": "9400",
             "cost_price": "4.05", "last_price": "5.17", "unrealized_profit_loss": "10432.35"},
            {"symbol": "GEAR", "currency": "AUD", "quantity": "357",
             "cost_price": "37.73", "last_price": "35.38", "unrealized_profit_loss": "-837.53"},
        ]
        with self._configured(), \
             patch.object(W, "get_access_token", return_value="tok"), \
             patch.object(W, "_resolve_account_id", return_value="acct123"), \
             patch.object(W, "_call", return_value=_fake_response(200, raw)):
            positions, _status = W.get_holdings()

        self.assertEqual(len(positions), 2)
        hk, au = positions[0], positions[1]
        self.assertEqual(hk["exchange"], "HK")
        self.assertEqual(hk["currency"], "HKD")
        self.assertEqual(au["exchange"], "AU")
        self.assertEqual(au["currency"], "AUD")

    def test_skips_zero_quantity_positions(self):
        raw = [{
            "symbol": "AAPL", "currency": "USD", "quantity": "0",
            "cost_price": "120.0", "last_price": "150.0",
            "unrealized_profit_loss": "0.0",
        }]
        with self._configured(), \
             patch.object(W, "get_access_token", return_value="tok"), \
             patch.object(W, "_resolve_account_id", return_value="acct123"), \
             patch.object(W, "_call", return_value=_fake_response(200, raw)):
            positions, _status = W.get_holdings()

        self.assertEqual(positions, [])

    def test_http_error_returns_empty_with_status(self):
        with self._configured(), \
             patch.object(W, "get_access_token", return_value="tok"), \
             patch.object(W, "_resolve_account_id", return_value="acct123"), \
             patch.object(W, "_call", return_value=_fake_response(401, {"error_code": "UNAUTHORIZED"})):
            positions, status = W.get_holdings()

        self.assertEqual(positions, [])
        self.assertIn("401", status)

    def test_missing_account_id_returns_error_status(self):
        with self._configured(), \
             patch.object(W, "get_access_token", return_value="tok"), \
             patch.object(W, "_resolve_account_id", return_value=None):
            positions, status = W.get_holdings()

        self.assertEqual(positions, [])
        self.assertIn("account_id", status)

    def test_token_error_is_caught_and_returned_as_status(self):
        with self._configured(), \
             patch.object(W, "get_access_token", side_effect=RuntimeError("boom")):
            positions, status = W.get_holdings()

        self.assertEqual(positions, [])
        self.assertIn("boom", status)


class TestResolveAccountId(unittest.TestCase):
    def test_env_override_takes_priority(self):
        with patch.object(W, "_ACCOUNT_ID", "env-account-id"):
            self.assertEqual(W._resolve_account_id("tok"), "env-account-id")

    def test_falls_back_to_account_list(self):
        raw = [{"account_id": "resolved-123", "account_type": "MARGIN"}]
        with patch.object(W, "_ACCOUNT_ID", ""), \
             patch.object(W, "_account_id_cache", None), \
             patch.object(W, "_call", return_value=_fake_response(200, raw)):
            result = W._resolve_account_id("tok")
        self.assertEqual(result, "resolved-123")

    def test_returns_none_on_http_error(self):
        with patch.object(W, "_ACCOUNT_ID", ""), \
             patch.object(W, "_account_id_cache", None), \
             patch.object(W, "_call", return_value=_fake_response(500, {})):
            result = W._resolve_account_id("tok")
        self.assertIsNone(result)


class TestTokenCache(unittest.TestCase):
    def test_load_cached_token_returns_none_when_file_missing(self):
        with patch.object(W, "_TOKEN_FILE", Path("/nonexistent/token.json")):
            self.assertIsNone(W._load_cached_token())

    def test_load_cached_token_returns_expired_record_for_revalidation(self):
        fake_file = MagicMock()
        fake_file.exists.return_value = True
        expires = int((time.time() - 100) * 1000)
        fake_file.read_text.return_value = (
            '{"token": "abc", "expires": %d}' % expires
        )
        with patch.object(W, "_TOKEN_FILE", fake_file):
            self.assertEqual(W._load_cached_token(), ("abc", expires))

    def test_load_cached_token_returns_token_when_valid(self):
        fake_file = MagicMock()
        fake_file.exists.return_value = True
        expires = int((time.time() + 100_000) * 1000)
        fake_file.read_text.return_value = (
            '{"token": "abc123", "expires": %d}' % expires
        )
        with patch.object(W, "_TOKEN_FILE", fake_file):
            self.assertEqual(W._load_cached_token(), ("abc123", expires))

    def test_expired_cache_is_revalidated_without_creating_token(self):
        verified_expires = int((time.time() + 15 * 86_400) * 1000)
        with patch.object(W, "_load_cached_token", return_value=("cached", 1)), \
             patch.object(W, "_check_token", return_value=(
                 "cached", verified_expires, "NORMAL"
             )), \
             patch.object(W, "_create_token") as mock_create, \
             patch.object(W, "_save_token") as mock_save:
            token = W.get_access_token()

        self.assertEqual(token, "cached")
        mock_create.assert_not_called()
        mock_save.assert_called_once_with("cached", verified_expires)

    def test_validation_error_does_not_create_replacement_token(self):
        with patch.object(W, "_load_cached_token", return_value=("cached", 1)), \
             patch.object(W, "_check_token", side_effect=ConnectionError("offline")), \
             patch.object(W, "_create_token") as mock_create:
            with self.assertRaisesRegex(RuntimeError, "refusing to create"):
                W.get_access_token()

        mock_create.assert_not_called()


class TestGetAccessTokenPollingResilience(unittest.TestCase):
    """A transient network error during polling must NOT abort the whole wait
    (regression: a single read-timeout used to kill the entire 5 min window)."""

    def test_transient_check_token_error_does_not_abort_wait(self):
        verified_expires = 9999999999999
        with patch.object(W, "_load_cached_token", return_value=None), \
             patch.object(W, "_create_token", return_value=("tok", 9999999999999, "PENDING")), \
             patch.object(W, "_save_token") as mock_save, \
             patch.object(W, "time") as mock_time, \
             patch.object(W, "_check_token", side_effect=[
                 ConnectionError("transient"), ("tok", verified_expires, "NORMAL")
             ]) as mock_check:
            # Simulate time passing across the deadline-check loop
            mock_time.time.side_effect = [0, 1, 2, 3]
            mock_time.sleep = MagicMock()
            token = W.get_access_token(poll_interval=0, timeout=100)
        self.assertEqual(token, "tok")
        self.assertEqual(mock_check.call_count, 2)
        mock_save.assert_called_once_with("tok", verified_expires)


if __name__ == "__main__":
    unittest.main()
