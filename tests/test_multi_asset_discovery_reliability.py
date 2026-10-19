"""Focused regressions for bounded and validated TradingView discovery."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sys

ROOT = Path(__file__).parents[1]
SCREENERS = ROOT / "tradingview" / "screeners"
if str(SCREENERS) not in sys.path:
    sys.path.insert(0, str(SCREENERS))

import multi_asset_screener as mas


def _stock(symbol="NASDAQ:ABC", **overrides):
    row = {
        "tv_symbol": symbol, "name": "ABC", "description": "ABC Inc",
        "close": 10.0, "RSI": 55.0, "ADX": 25.0, "SMA200": 8.0,
        "relative_volume_10d_calc": 1.2, "average_volume_10d_calc": 200_000,
        "market_cap_basic": 5_000_000_000, "recommendation_mark": "Buy",
        "type": "stock", "is_primary": True, "currency": "USD",
    }
    row.update(overrides)
    return row


def _args(**kw):
    defaults = dict(scan_class=["stocks_us"], rsi_min=50, rsi_max=75,
                    adx_min=20, bearish=False, max_per_class=20,
                    fetch_limit=600, profile="broad", min_market_cap=None,
                    min_volume=100_000)
    defaults.update(kw)
    return Namespace(**defaults)


def test_successful_json_without_data_is_an_error(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"error":"session expired"}'
    monkeypatch.setattr(mas.urllib.request, "urlopen", lambda *a, **k: Response())
    monkeypatch.setattr(mas.time, "sleep", lambda *_: None)
    rows = mas._tv_scan("https://example.invalid", ["close"], [], "ADX", 10)
    assert rows and rows[0]["_error"].startswith("ValueError:")


def test_provider_row_metadata_is_preserved_without_timestamp_synthesis(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return b'{"data":[{"s":"NASDAQ:ABC","d":[10.0],"timestamp":1234567890}]}'
    monkeypatch.setattr(mas.urllib.request, "urlopen", lambda *a, **k: Response())
    rows = mas._tv_scan("https://example.invalid", ["close"], [], "ADX", 1, retries=0)
    assert rows == [{"tv_symbol": "NASDAQ:ABC", "close": 10.0,
                     "_tv_timestamp": 1234567890}]


def test_discovery_paginates_to_fetch_depth_and_scores_only_cap(monkeypatch):
    calls = []
    all_rows = [_stock(f"NASDAQ:S{i}") for i in range(225)]
    def fake_scan(url, cols, filters, sort_by, count, retries=2, offset=0):
        calls.append((offset, count))
        page = all_rows[offset:offset + count]
        return page
    monkeypatch.setattr(mas, "_tv_scan", fake_scan)
    result = mas.run_all_scans(_args(fetch_limit=225, max_per_class=7))
    assert calls == [(0, 100), (100, 100), (200, 25)]
    assert len(result["stocks_us"]) == 7
    assert mas.MARKET_CAP_CURRENCY["stocks_us"] == "USD"


def test_overlapping_page_rows_are_deduplicated_but_raw_coverage_counts_them(monkeypatch):
    pages = {
        0: [_stock("NASDAQ:A"), _stock("NASDAQ:B")],
        2: [_stock("NASDAQ:B"), _stock("NASDAQ:C")],
    }
    def fake_scan(url, cols, filters, sort_by, count, retries=2, offset=0):
        return pages.get(offset, [])
    monkeypatch.setattr(mas, "PAGE_SIZE", 2)
    monkeypatch.setattr(mas, "_tv_scan", fake_scan)
    args = _args(fetch_limit=4, max_per_class=20)
    result = mas.run_all_scans(args)
    assert [r["tv_symbol"] for r in result["stocks_us"]] == [
        "NASDAQ:A", "NASDAQ:B", "NASDAQ:C",
    ]
    assert args._scan_diagnostics["stocks_us"]["raw"] == 4


def test_nonstock_legacy_default_and_missing_sma_behavior_remain(monkeypatch):
    row = {"tv_symbol": "FX:EURUSD", "close": 1.1, "SMA200": None,
           "RSI": 55, "ADX": 25}
    monkeypatch.setattr(mas, "_tv_scan", lambda *a, **k: [row])
    args = _args(scan_class=["forex"], max_per_class=None, fetch_limit=600)
    result = mas.run_all_scans(args)
    assert len(result["forex"]) == 1
    assert args._scan_diagnostics["forex"]["fetch_limit"] == 18
    assert args._scan_diagnostics["forex"]["scoring_limit"] == 6


def test_stock_guards_reject_missing_zero_nonprimary_and_wrong_currency(monkeypatch):
    rows = [
        _stock("NASDAQ:GOOD"),
        _stock("NASDAQ:NOSMA", SMA200=None),
        _stock("NASDAQ:ZEROVOL", average_volume_10d_calc=0),
        _stock("NASDAQ:ZEROREL", relative_volume_10d_calc=0),
        _stock("NASDAQ:ADR", is_primary=False),
        _stock("NASDAQ:FUND", type="fund"),
        _stock("NASDAQ:BADCCY", currency="AUD"),
        _stock("OTC:OTC"),
    ]
    monkeypatch.setattr(mas, "_tv_scan", lambda *a, **k: rows)
    result = mas.run_all_scans(_args(fetch_limit=10))
    assert [r["tv_symbol"] for r in result["stocks_us"]] == ["NASDAQ:GOOD"]


def test_profiles_use_explicit_local_currency_market_cap_floors(monkeypatch):
    seen = {}
    def fake_scan(url, columns, filters, sort_by, count, retries=2, offset=0):
        seen["floor"] = next(f["right"] for f in filters if f["left"] == "market_cap_basic")
        return []
    monkeypatch.setattr(mas, "_tv_scan", fake_scan)
    mas.run_all_scans(_args(fetch_limit=10, profile="large_cap"))
    assert seen["floor"] == mas.MARKET_CAP_FLOORS["large_cap"]["stocks_us"]
    assert mas.MARKET_CAP_CURRENCY["stocks_us"] == "USD"
    assert mas.MARKET_CAP_CURRENCY["stocks_au"] == "AUD"
    assert mas.MARKET_CAP_CURRENCY["stocks_in"] == "INR"


def test_input_adapter_exposes_coverage_from_real_scan_path(monkeypatch):
    tradingview = ROOT / "tradingview"
    if str(tradingview) not in sys.path:
        sys.path.insert(0, str(tradingview))
    try:
        import input_sources
        monkeypatch.setattr(mas, "_tv_scan", lambda *a, **k: [_stock("NASDAQ:ABC")])
        fires, meta = input_sources.tv_screener_to_fires(
            classes=["stocks_us"], max_per_class=5, fetch_limit=20, profile="broad",
        )
    finally:
        if str(tradingview) in sys.path:
            sys.path.remove(str(tradingview))
    assert [f["pro_symbol"] for f in fires] == ["NASDAQ:ABC"]
    assert meta["coverage"]["stocks_us"] == {
        "profile": "broad", "fetch_limit": 20, "scoring_limit": 5,
        "raw": 1, "filtered": 1, "selected": 1,
        "currency": "USD", "market_cap_floor": mas.MARKET_CAP_FLOORS["broad"]["stocks_us"],
    }


def test_invalid_fetch_limit_and_profile_fail_loudly():
    for value in (0, mas.MAX_FETCH_LIMIT + 1):
        try:
            mas._fetch_limit(value)
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range fetch limit accepted")
    try:
        mas._profile_name("nifty200")
    except ValueError:
        pass
    else:
        raise AssertionError("unverified constituent profile accepted")


def test_environment_defaults_set_discovery_depth_and_profile(monkeypatch):
    monkeypatch.delenv("SIGNAL_SCANNER_FETCH_LIMIT", raising=False)
    monkeypatch.delenv("SIGNAL_SCANNER_PROFILE", raising=False)
    assert mas._fetch_limit() == 600
    assert mas._profile_name() == "broad"
    monkeypatch.setenv("SIGNAL_SCANNER_FETCH_LIMIT", "425")
    monkeypatch.setenv("SIGNAL_SCANNER_PROFILE", "large_cap")
    assert mas._fetch_limit() == 425
    assert mas._profile_name() == "large_cap"
