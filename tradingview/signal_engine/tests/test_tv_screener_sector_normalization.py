"""
Regression tests for sector-name normalization (review 2026-10-13, Issue 5).

BEFORE the fix: ``SECTOR_ETF_MAP`` was keyed by canonical SPDR names
("Technology", "Financial Services", "Healthcare") but TradingView's ``sector``
field returns its own taxonomy ("Technology Services", "Electronic Technology",
"Finance", "Health Technology"). Every lookup missed, so all 94 rows had
``sector_etf_score=None`` and the sector ETF contribution was silently dropped.

AFTER: a single ``normalize_sector()`` maps TV + legacy names to canonical
sectors. The score lookup and the displayed ETF detail both use it, so they
always agree. Unknown sectors resolve to None (no arbitrary ETF).

All tests are network-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import tv_screener_fundamentals as tsf


class TestNormalizeSector:

    @pytest.mark.parametrize("raw,expected", [
        ("Technology Services",     "Technology"),
        ("Electronic Technology",   "Technology"),
        ("Finance",                 "Financial Services"),
        ("Health Technology",       "Healthcare"),
        ("Health Services",         "Healthcare"),
        ("Energy Minerals",         "Energy"),
        ("Non-Energy Minerals",     "Basic Materials"),
        ("Process Industries",      "Basic Materials"),
        ("Producer Manufacturing",  "Industrials"),
        ("Industrial Services",     "Industrials"),
        ("Commercial Services",     "Industrials"),
        ("Transportation",          "Industrials"),
        ("Consumer Durables",       "Consumer Cyclical"),
        ("Consumer Services",       "Consumer Cyclical"),
        ("Retail Trade",            "Consumer Cyclical"),
        ("Distribution Services",   "Consumer Cyclical"),
        ("Consumer Non-Durables",   "Consumer Defensive"),
        ("Communications",          "Communication Services"),
    ])
    def test_tradingview_names_map(self, raw, expected):
        assert tsf.normalize_sector(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("Technology",             "Technology"),
        ("Financial Services",     "Financial Services"),
        ("Healthcare",             "Healthcare"),
        ("Energy",                 "Energy"),
        ("Basic Materials",        "Basic Materials"),
        ("Industrials",            "Industrials"),
        ("Consumer Cyclical",      "Consumer Cyclical"),
        ("Consumer Defensive",     "Consumer Defensive"),
        ("Utilities",              "Utilities"),
        ("Real Estate",            "Real Estate"),
        ("Communication Services", "Communication Services"),
    ])
    def test_legacy_names_still_resolve(self, raw, expected):
        assert tsf.normalize_sector(raw) == expected

    def test_case_and_whitespace_insensitive(self):
        assert tsf.normalize_sector("  technology services ") == "Technology"
        assert tsf.normalize_sector("FINANCE") == "Financial Services"

    @pytest.mark.parametrize("raw", [None, "", "   ", "N/A", "Unknown Sector", 123])
    def test_unknown_or_missing_returns_none(self, raw):
        assert tsf.normalize_sector(raw) is None

    def test_unknown_sector_gets_no_arbitrary_etf(self):
        assert tsf.sector_etf_ticker("Quantum Widgets") is None
        assert tsf.sector_etf_ticker(None) is None

    @pytest.mark.parametrize("raw,expected", [
        ("Technology Services", "XLK"),
        ("Electronic Technology", "XLK"),
        ("Finance", "XLF"),
        ("Health Technology", "XLV"),
        ("Energy Minerals", "XLE"),
        ("Non-Energy Minerals", "XLB"),
    ])
    def test_required_mappings(self, raw, expected):
        assert tsf.sector_etf_ticker(raw) == expected


class TestScoreAndDetailAgree:

    def test_detail_lookup_normalizes_raw_sector(self, monkeypatch):
        tsf._SECTOR_ETF_DETAIL.clear()
        tsf._SECTOR_ETF_DETAIL["Technology"] = {"ticker": "XLK", "pct": 4.2, "score": 7.5}
        # Raw TV name must resolve to the same canonical entry.
        assert tsf.get_sector_etf_detail("Technology Services")["ticker"] == "XLK"
        assert tsf.get_sector_etf_detail("Electronic Technology")["ticker"] == "XLK"

    def test_unknown_sector_detail_is_empty(self):
        assert tsf.get_sector_etf_detail("Quantum Widgets") == {}

    def test_score_lookup_normalizes_raw_sector(self):
        tsf._SECTOR_ETF_CACHE["data"] = {"Technology": 7.5, "Financial Services": 6.0}
        tsf._SECTOR_ETF_CACHE["ts"] = 1.0
        assert tsf.get_sector_etf_score("Technology Services") == 7.5
        assert tsf.get_sector_etf_score("Finance") == 6.0
        assert tsf.get_sector_etf_score("Quantum Widgets") is None

    def test_displayed_etf_matches_scoring_etf(self):
        tsf._SECTOR_ETF_DETAIL.clear()
        tsf._SECTOR_ETF_DETAIL["Healthcare"] = {"ticker": "XLV", "pct": 1.0, "score": 6.0}
        tsf._SECTOR_ETF_CACHE["data"] = {"Healthcare": 6.0}
        tsf._SECTOR_ETF_CACHE["ts"] = 1.0
        raw = "Health Technology"
        assert tsf.get_sector_etf_score(raw) == 6.0
        assert tsf.get_sector_etf_detail(raw)["ticker"] == "XLV"


class TestMomentumFetchCoverage:

    def test_fetch_scores_canonical_sectors(self, monkeypatch):
        import json as _json

        tsf._SECTOR_ETF_CACHE.update({"data": None, "ts": 0.0})
        tsf._SECTOR_ETF_DETAIL.clear()

        def fake_urlopen(req, timeout=None):
            rows = []
            for etf in tsf.SECTOR_ETF_MAP.values():
                rows.append({"s": f"AMEX:{etf}", "d": [etf, 110.0, 100.0, 108.0]})
            return type("R", (), {"read": lambda self: _json.dumps({"data": rows}).encode()})()

        monkeypatch.setattr(tsf.urllib.request, "urlopen", fake_urlopen)
        scores = tsf.fetch_sector_etf_momentum()
        assert len(scores) == 11
        assert scores["Technology"] > 5.0
        # Detail is keyed by canonical sector so raw TV names resolve.
        assert tsf.get_sector_etf_detail("Technology Services")["ticker"] == "XLK"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
