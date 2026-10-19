"""
Regression tests for exchange-aware symbol mapping (review 2026-10-13, Issue 4).

BEFORE the fix: ``fetch_tv_fundamentals`` keyed its reverse map by bare ticker
(``sym_map[ticker] = ps``) and fell back to the raw response symbol. Two
different exchanges sharing a ticker (NYSE:ALL vs ASX:ALL) collided, so one
symbol's price/fundamentals overwrote the other's, and unsolicited scanner rows
were assigned to whatever symbol happened to share the ticker.

AFTER: exact exchange-qualified matches win; a bare-ticker fallback is allowed
only when the ticker is unambiguous AND belongs to the same scanner; ambiguous
or unsolicited responses are skipped.

All tests are network-free (urllib is monkeypatched).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import tv_screener_fundamentals as tsf


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload


def _install_scanner(monkeypatch, responses_by_url):
    """responses_by_url: {url: [(raw_symbol, {col: value}), ...]}"""
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        rows = responses_by_url.get(url, [])
        data = []
        for raw_sym, cols in rows:
            d = [cols.get(c) for c in tsf.FUND_FIELDS]
            data.append({"s": raw_sym, "d": [cols.get("name")] + d})
        return _FakeResponse({"data": data})

    monkeypatch.setattr(tsf.urllib.request, "urlopen", fake_urlopen)
    return calls


AMERICA = tsf._TV_SCANNER["NYSE"]
AUSTRALIA = tsf._TV_SCANNER["ASX"]


class TestExchangeCollision:

    def test_same_ticker_two_exchanges_keep_own_data(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [("NYSE:ALL", {"name": "Allstate", "close": 200.0, "sector": "Finance"})],
            AUSTRALIA: [("ASX:ALL", {"name": "Aristocrat", "close": 70.0, "sector": "Technology Services"})],
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL", "ASX:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0
        assert out["NYSE:ALL"]["name"] == "Allstate"
        assert out["ASX:ALL"]["close"] == 70.0
        assert out["ASX:ALL"]["name"] == "Aristocrat"

    def test_reversing_input_order_gives_same_associations(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [("NYSE:ALL", {"name": "Allstate", "close": 200.0})],
            AUSTRALIA: [("ASX:ALL", {"name": "Aristocrat", "close": 70.0})],
        })
        out = tsf.fetch_tv_fundamentals(["ASX:ALL", "NYSE:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0
        assert out["ASX:ALL"]["close"] == 70.0

    def test_reversing_response_order_gives_same_associations(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [
                ("NYSE:OTHER", {"name": "Other", "close": 5.0}),
                ("NYSE:ALL", {"name": "Allstate", "close": 200.0}),
            ],
            AUSTRALIA: [("ASX:ALL", {"name": "Aristocrat", "close": 70.0})],
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL", "ASX:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0
        assert out["ASX:ALL"]["close"] == 70.0

    def test_unsolicited_symbol_cannot_overwrite(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [
                ("NYSE:ALL", {"name": "Allstate", "close": 200.0}),
                ("NYSE:ZZZ", {"name": "Unsolicited", "close": 999.0}),
            ],
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0
        assert "NYSE:ZZZ" not in out

    def test_missing_response_leaves_only_that_symbol_unavailable(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [("NYSE:ALL", {"name": "Allstate", "close": 200.0})],
            AUSTRALIA: [],   # ASX:ALL not returned
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL", "ASX:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0
        assert "ASX:ALL" not in out


class TestAliasesAndFallback:

    def test_unambiguous_bare_ticker_fallback_still_works(self, monkeypatch):
        # Scanner returns a bare ticker (no exchange prefix) for a single request.
        _install_scanner(monkeypatch, {
            AMERICA: [("TEST", {"name": "Test Co", "close": 42.0})],
        })
        out = tsf.fetch_tv_fundamentals(["NASDAQ:TEST"])
        assert out["NASDAQ:TEST"]["close"] == 42.0

    def test_ambiguous_bare_ticker_is_not_assigned(self, monkeypatch):
        # Same bare ticker requested on two exchanges; a bare response is ambiguous.
        _install_scanner(monkeypatch, {
            AMERICA: [("ALL", {"name": "Ambiguous", "close": 1.0})],
            AUSTRALIA: [("ALL", {"name": "Ambiguous", "close": 2.0})],
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL", "ASX:ALL"])
        assert out == {}

    def test_exact_match_wins_over_bare_fallback(self, monkeypatch):
        _install_scanner(monkeypatch, {
            AMERICA: [
                ("NYSE:ALL", {"name": "Exact", "close": 200.0}),
                ("ALL", {"name": "Bare", "close": 1.0}),
            ],
        })
        out = tsf.fetch_tv_fundamentals(["NYSE:ALL"])
        assert out["NYSE:ALL"]["close"] == 200.0

    def test_unknown_exchange_returns_empty(self, monkeypatch):
        _install_scanner(monkeypatch, {})
        assert tsf.fetch_tv_fundamentals(["LSE:VOD"]) == {}


INDIA = tsf._TV_SCANNER["NSE"]


class TestCrossExchangeRejection:
    """Issue 3 follow-up: an exchange-qualified response must not be
    reassigned to a different exchange that shares the same scanner.

    NSE and BSE both use the india scanner, so a ``BSE:ABC`` response was
    previously accepted for a request of ``NSE:ABC``.
    """

    def test_bse_response_not_assigned_to_nse_request(self, monkeypatch):
        _install_scanner(monkeypatch, {
            INDIA: [("BSE:ABC", {"name": "BSE Co", "close": 10.0})],
        })
        assert tsf.fetch_tv_fundamentals(["NSE:ABC"]) == {}

    def test_nse_response_not_assigned_to_bse_request(self, monkeypatch):
        _install_scanner(monkeypatch, {
            INDIA: [("NSE:ABC", {"name": "NSE Co", "close": 20.0})],
        })
        assert tsf.fetch_tv_fundamentals(["BSE:ABC"]) == {}

    def test_exact_india_match_still_works(self, monkeypatch):
        _install_scanner(monkeypatch, {
            INDIA: [("NSE:ABC", {"name": "NSE Co", "close": 20.0})],
        })
        out = tsf.fetch_tv_fundamentals(["NSE:ABC"])
        assert out["NSE:ABC"]["close"] == 20.0

    def test_bare_ticker_fallback_still_allowed(self, monkeypatch):
        _install_scanner(monkeypatch, {
            INDIA: [("ABC", {"name": "Bare", "close": 5.0})],
        })
        out = tsf.fetch_tv_fundamentals(["NSE:ABC"])
        assert out["NSE:ABC"]["close"] == 5.0

    def test_both_india_exchanges_requested_keep_own_data(self, monkeypatch):
        _install_scanner(monkeypatch, {
            INDIA: [
                ("NSE:ABC", {"name": "NSE Co", "close": 20.0}),
                ("BSE:ABC", {"name": "BSE Co", "close": 10.0}),
            ],
        })
        out = tsf.fetch_tv_fundamentals(["NSE:ABC", "BSE:ABC"])
        assert out["NSE:ABC"]["close"] == 20.0
        assert out["BSE:ABC"]["close"] == 10.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
