"""Tests for austsuper_screener.py DB path convention (BUG-DB-A)."""
import pytest
from pathlib import Path


def test_db_path_in_database_folder():
    """DB must be in the root database/ folder, not tradingview/."""
    from tradingview.screeners.austsuper_screener import DB_PATH
    # Must be inside the 'database' directory
    assert DB_PATH.parent.name == "database", (
        f"DB_PATH should be in 'database/' folder, got: {DB_PATH}"
    )


def test_db_path_not_in_tradingview():
    """DB must NOT be inside tradingview/ directory."""
    from tradingview.screeners.austsuper_screener import DB_PATH
    parts = DB_PATH.parts
    assert "tradingview" not in parts, (
        f"DB_PATH must not be inside tradingview/: {DB_PATH}"
    )


def test_append_to_fires_replaces_previous_austsuper_run(tmp_path, monkeypatch):
    """Each run re-screens the whole universe — old austsuper fires must not pile up."""
    import json
    from tradingview.screeners import austsuper_screener as aus

    fires = tmp_path / "fires_raw.json"
    fires.write_text(json.dumps({"all": [
        {"symbol": "ASX:PME", "source": "austsuper_screener"},
        {"symbol": "ASX:VAS", "source": "austsuper_screener"},   # ETF from the old universe
        {"symbol": "NASDAQ:NVDA", "source": "webhook"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(aus, "FIRES_FILE", fires)

    assert aus.append_to_fires([{"tv_symbol": "ASX:PME", "close": 178.86}]) == 1

    all_fires = json.loads(fires.read_text(encoding="utf-8"))["all"]
    austsuper = [f for f in all_fires if f.get("source") == "austsuper_screener"]
    assert [f["symbol"] for f in austsuper] == ["ASX:PME"]
    assert any(f["symbol"] == "NASDAQ:NVDA" for f in all_fires), "other sources must survive"


def test_download_asx300_uses_official_index_symbolset(monkeypatch):
    """Universe must be the real S&P/ASX 300 constituent set, not a market-cap proxy."""
    from tradingview.screeners import austsuper_screener as aus

    captured = {}

    def fake_scan(filters, sort_by, count, retries=2, symbolset=None):
        captured["filters"] = filters
        captured["symbolset"] = symbolset
        return [
            {"tv_symbol": "ASX:CBA",  "type": "stock", "subtype": "common"},
            {"tv_symbol": "ASX:NEM",  "type": "dr",    "subtype": ""},
            {"tv_symbol": "ASX:ARG",  "type": "fund",  "subtype": ""},
            {"tv_symbol": "ASX:VAS",  "type": "fund",  "subtype": "etf"},
        ]

    monkeypatch.setattr(aus, "_tv_scan", fake_scan)
    rows = aus.download_asx300(verbose=False)

    assert captured["symbolset"] == "SYML:ASX;XKO"
    assert captured["filters"] == [], "index membership defines the universe, not filters"
    assert [r["tv_symbol"] for r in rows] == ["ASX:CBA", "ASX:NEM"], \
        "funds must be excluded; CDIs are index members and must be kept"
    assert rows[0]["_universe"] == "asx300"


def test_tv_scan_omits_symbols_key_when_no_symbolset():
    """Plain filter scans must not send an empty symbols block."""
    import inspect
    from tradingview.screeners import austsuper_screener as aus

    src = inspect.getsource(aus._tv_scan)
    assert 'payload["symbols"] = {"symbolset": [symbolset]}' in src
    assert "if symbolset:" in src
