from __future__ import annotations

from datetime import date

from tradingview.options.tv_options import TVOptionsFetcher, _SCAN2_URL, _SPOT_URL


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, expiries):
        self.expiries = expiries
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if url == _SPOT_URL:
            return _Response({"fields": ["close"], "symbols": [{"f": [100.0]}]})
        if len(json.get("columns", [])) == 1 and json["columns"] == ["expiration"]:
            return _Response({
                "fields": ["expiration"],
                "symbols": [{"f": [expiry]} for expiry in self.expiries],
            })
        return _Response({"fields": [], "symbols": []})


def test_scanner_expiry_window_filters_before_bounded_chain_request(monkeypatch):
    fetcher = TVOptionsFetcher()
    expiries = [20261019, 20261021, 20261023, 20261106, 20261113,
                20261120, 20261204]
    session = _Session(expiries)
    monkeypatch.setattr(fetcher, "is_available", lambda: True)
    monkeypatch.setattr(fetcher, "_session", lambda: session)

    assert fetcher.get_calls(
        "NASDAQ:AAPL", n_expiries=3, min_dte=14, max_dte=45,
        reference_date=date(2026, 10, 19),
    ) == []

    expiration_request = next(payload for url, payload, _ in session.calls
                              if payload.get("columns") == ["expiration"])
    expiration_right = expiration_request["filter2"]["operands"][-1]["expression"]["right"]
    assert expiration_right == [20261102, 20261203]
    chain_requests = [(url, payload) for url, payload, _ in session.calls
                      if url == _SCAN2_URL and payload.get("columns") != ["expiration"]]
    assert len(chain_requests) == 1
    requested = chain_requests[0][1]["filter2"]["operands"][2]["expression"]["right"]
    # The near expiries are excluded before selecting the three dates fetched.
    assert requested == [20261106, 20261120]


def test_legacy_nearest_expiry_call_keeps_default_window_behavior(monkeypatch):
    fetcher = TVOptionsFetcher()
    session = _Session([20261019, 20261021, 20261023, 20261106])
    monkeypatch.setattr(fetcher, "is_available", lambda: True)
    monkeypatch.setattr(fetcher, "_session", lambda: session)
    monkeypatch.setattr(fetcher, "_reference_date", lambda _value: date(2026, 10, 18))

    assert fetcher.get_calls("NASDAQ:AAPL", n_expiries=3) == []
    expiration_request = next(payload for _, payload, _ in session.calls
                              if payload.get("columns") == ["expiration"])
    assert all(operand["expression"]["left"] != "expiration"
               for operand in expiration_request["filter2"]["operands"])
