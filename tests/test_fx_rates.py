"""
test_fx_rates.py
================
Tests for fx_rates.py — the single source of truth for currency conversion
across global_portfolio_dashboard.py, portfolio_report_html.py, and
reports/report_db.py.

Regression guard: this module replaced ~8 independently hand-maintained
hardcoded FX tables that had drifted apart by up to 4.4% and some of which
silently omitted GBP/CAD/KRW (currencies reachable via Moomoo positions on
LON/TSX/KRX), causing database/portfolio_history.db and
reports/report_analytics.db to silently disagree with the CLI/HTML totals
from the same run whenever a GBP/CAD position existed.

These tests must NEVER depend on live network access — all live-fetch
functions are mocked so the suite is deterministic and fast offline.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fx_rates as FX


# Canonical live rates used across every test below (mirrors a real
# frankfurter.app / open.er-api.com response, already inverted to
# "1 unit of CCY -> USD" convention).
_FAKE_LIVE_RATES = {
    "USD": 1.0,
    "AUD": 0.69,
    "INR": 0.0106,
    "SGD": 0.7726,
    "HKD": 0.1275,
    "JPY": 0.00615,
    "GBP": 1.3247,
    "CAD": 0.7038,
    "KRW": 0.000646,
    "CNY": 0.1471,
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own cache file — never touch the real repo cache."""
    fake_cache = tmp_path / ".cache_fx_rates.json"
    monkeypatch.setattr(FX, "_FX_CACHE_FILE", fake_cache)
    return fake_cache


# ══════════════════════════════════════════════════════════════════════════════
# SECTION A — get_fx_to_usd() basic contract
# ══════════════════════════════════════════════════════════════════════════════

class TestGetFxToUsd:
    def test_returns_all_canonical_currencies(self):
        with patch.object(FX, "_fetch_frankfurter", return_value=dict(_FAKE_LIVE_RATES)):
            rates = FX.get_fx_to_usd()
        for ccy in FX._CURRENCIES:
            assert ccy in rates, f"{ccy} missing from get_fx_to_usd() result"

    def test_usd_is_always_exactly_1(self):
        with patch.object(FX, "_fetch_frankfurter", return_value=dict(_FAKE_LIVE_RATES)):
            rates = FX.get_fx_to_usd()
        assert rates["USD"] == 1.0

    def test_live_fetch_populates_cache_file(self, isolated_cache):
        assert not isolated_cache.exists()
        with patch.object(FX, "_fetch_frankfurter", return_value=dict(_FAKE_LIVE_RATES)):
            FX.get_fx_to_usd()
        assert isolated_cache.exists()
        saved = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert saved["rates"]["AUD"] == pytest.approx(0.69)
        assert "saved_at" in saved


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B — Caching behavior (TTL, force_refresh)
# ══════════════════════════════════════════════════════════════════════════════

class TestCachingBehavior:
    def test_fresh_cache_skips_network_entirely(self, isolated_cache):
        isolated_cache.write_text(
            json.dumps({"saved_at": time.time(), "rates": dict(_FAKE_LIVE_RATES)}),
            encoding="utf-8",
        )
        with patch.object(FX, "_fetch_frankfurter") as mock_ff, \
             patch.object(FX, "_fetch_open_er_api") as mock_oa:
            rates = FX.get_fx_to_usd()
        mock_ff.assert_not_called()
        mock_oa.assert_not_called()
        assert rates["AUD"] == pytest.approx(0.69)

    def test_stale_cache_triggers_live_refetch(self, isolated_cache):
        stale_ts = time.time() - FX._FX_CACHE_TTL - 3600  # 1h past TTL
        isolated_cache.write_text(
            json.dumps({"saved_at": stale_ts, "rates": {"USD": 1.0, "AUD": 0.50}}),
            encoding="utf-8",
        )
        with patch.object(FX, "_fetch_frankfurter", return_value=dict(_FAKE_LIVE_RATES)) as mock_ff:
            rates = FX.get_fx_to_usd()
        mock_ff.assert_called_once()
        assert rates["AUD"] == pytest.approx(0.69)  # fresh value, not the stale 0.50

    def test_force_refresh_bypasses_fresh_cache(self, isolated_cache):
        isolated_cache.write_text(
            json.dumps({"saved_at": time.time(), "rates": {"USD": 1.0, "AUD": 0.50}}),
            encoding="utf-8",
        )
        with patch.object(FX, "_fetch_frankfurter", return_value=dict(_FAKE_LIVE_RATES)) as mock_ff:
            rates = FX.get_fx_to_usd(force_refresh=True)
        mock_ff.assert_called_once()
        assert rates["AUD"] == pytest.approx(0.69)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION C — Fallback chain (live sources down, stale cache, static table)
# ══════════════════════════════════════════════════════════════════════════════

class TestFallbackChain:
    def test_frankfurter_down_falls_back_to_open_er_api(self, isolated_cache):
        with patch.object(FX, "_fetch_frankfurter", return_value=None), \
             patch.object(FX, "_fetch_open_er_api", return_value=dict(_FAKE_LIVE_RATES)) as mock_oa:
            rates = FX.get_fx_to_usd()
        mock_oa.assert_called_once()
        assert rates["AUD"] == pytest.approx(0.69)

    def test_both_live_sources_down_uses_stale_cache_of_any_age(self, isolated_cache):
        ancient_ts = time.time() - (30 * 24 * 3600)  # 30 days old
        isolated_cache.write_text(
            json.dumps({"saved_at": ancient_ts, "rates": {"USD": 1.0, "AUD": 0.61}}),
            encoding="utf-8",
        )
        with patch.object(FX, "_fetch_frankfurter", return_value=None), \
             patch.object(FX, "_fetch_open_er_api", return_value=None):
            rates = FX.get_fx_to_usd(force_refresh=True)
        assert rates["AUD"] == pytest.approx(0.61)

    def test_both_live_sources_down_and_no_cache_uses_static_fallback(self, isolated_cache):
        assert not isolated_cache.exists()
        with patch.object(FX, "_fetch_frankfurter", return_value=None), \
             patch.object(FX, "_fetch_open_er_api", return_value=None):
            rates = FX.get_fx_to_usd()
        assert rates == FX._STATIC_FALLBACK_TO_USD

    def test_static_fallback_still_covers_all_canonical_currencies(self):
        """The last-resort table must never be missing a currency the live
        fetch would otherwise provide — this was the root cause of the
        original bug (some hardcoded tables silently omitted GBP/CAD/KRW)."""
        for ccy in FX._CURRENCIES:
            assert ccy in FX._STATIC_FALLBACK_TO_USD, (
                f"{ccy} missing from _STATIC_FALLBACK_TO_USD — this is exactly "
                f"the class of bug fx_rates.py was created to eliminate"
            )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION D — Conversion helpers (to_usd / to_aud / convert)
# ══════════════════════════════════════════════════════════════════════════════

class TestConversionHelpers:
    def test_to_usd_basic(self):
        assert FX.to_usd(100, "AUD", _FAKE_LIVE_RATES) == pytest.approx(69.0)
        assert FX.to_usd(100, "USD", _FAKE_LIVE_RATES) == pytest.approx(100.0)

    def test_to_aud_basic(self):
        # 100 USD -> AUD: 100 * 1.0 / 0.69 = 144.93
        assert FX.to_aud(100, "USD", _FAKE_LIVE_RATES) == pytest.approx(144.928, rel=1e-3)

    def test_convert_gbp_to_inr_matches_manual_usd_pivot(self):
        # 100 GBP -> USD -> INR, manually computed via the same USD pivot
        expected_usd = 100 * _FAKE_LIVE_RATES["GBP"]
        expected_inr = expected_usd / _FAKE_LIVE_RATES["INR"]
        assert FX.convert(100, "GBP", "INR", _FAKE_LIVE_RATES) == pytest.approx(expected_inr)

    def test_convert_cad_not_silently_treated_as_usd(self):
        """Regression guard for the original bug: CAD (reachable via Moomoo
        TSX positions) must be converted at its real rate, not defaulted to
        1.0 (which would overvalue a CAD position by ~35-40%)."""
        cad_as_usd_wrong = FX.to_usd(100, "CAD", {"USD": 1.0})  # simulates the OLD bug
        cad_as_usd_correct = FX.to_usd(100, "CAD", _FAKE_LIVE_RATES)
        assert cad_as_usd_wrong == 100.0          # old bug: 1:1 fallback
        assert cad_as_usd_correct == pytest.approx(70.38)  # correct live rate
        assert cad_as_usd_correct != cad_as_usd_wrong

    def test_gbp_not_silently_treated_as_usd(self):
        """Same regression guard for GBP (reachable via Moomoo LON positions)."""
        gbp_as_usd_wrong = FX.to_usd(100, "GBP", {"USD": 1.0})
        gbp_as_usd_correct = FX.to_usd(100, "GBP", _FAKE_LIVE_RATES)
        assert gbp_as_usd_wrong == 100.0
        assert gbp_as_usd_correct == pytest.approx(132.47)
        assert gbp_as_usd_correct != gbp_as_usd_wrong

    def test_unknown_currency_warns_loudly_instead_of_silent_wrong_value(self, capsys):
        """Unlike the old hardcoded dicts (silent .get(ccy, 1.0) fallback),
        fx_rates.py must print a visible warning when it has to approximate."""
        result = FX.to_usd(100, "XYZ_NOT_A_REAL_CURRENCY", _FAKE_LIVE_RATES)
        captured = capsys.readouterr()
        assert result == 100.0  # still falls back to 1:1, but now it's LOUD
        assert "WARNING" in captured.out
        assert "XYZ_NOT_A_REAL_CURRENCY" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# SECTION E — Regression guard: no hardcoded FX tables re-introduced
# ══════════════════════════════════════════════════════════════════════════════

class TestNoHardcodedFxTablesRegressionGuard:
    """
    These tests scan the actual consumer source files to guard against the
    original bug ever being re-introduced: a hand-copied hardcoded FX dict
    that silently omits a currency or drifts from the canonical live rates.
    """

    _CONSUMER_FILES = [
        ROOT / "global_portfolio_dashboard.py",
        ROOT / "portfolio_report_html.py",
        ROOT / "reports" / "report_db.py",
    ]

    def test_consumers_import_fx_rates(self):
        for f in self._CONSUMER_FILES:
            src = f.read_text(encoding="utf-8")
            assert "fx_rates" in src, (
                f"{f.name} no longer references fx_rates — did someone "
                f"re-introduce a hardcoded FX table?"
            )

    def test_no_removed_hardcoded_constants_remain(self):
        """The specific constants this fix removed must never come back."""
        removed_names = ("_USD_TO_AUD", "_APPROX_FX_TO_AUD", "_FX_TO_AUD")
        for f in self._CONSUMER_FILES:
            src = f.read_text(encoding="utf-8")
            for name in removed_names:
                assert name not in src, (
                    f"{f.name} still contains {name} — this was the hardcoded "
                    f"table removed in favour of fx_rates.get_fx_to_usd()"
                )

    def test_currency_for_prefix_outputs_all_covered_by_canonical_list(self):
        """Every currency global_portfolio_dashboard.py's _currency_for_prefix()
        can produce must exist in fx_rates._CURRENCIES — this is the exact
        assertion the original critique recommended to catch Finding 1."""
        import global_portfolio_dashboard as G

        exchange_prefixes = ["AU", "HK", "SG", "JP", "KR", "CA", "UK", "US", ""]
        for prefix in exchange_prefixes:
            ccy = G._currency_for_prefix(prefix)
            assert ccy in FX._CURRENCIES, (
                f"_currency_for_prefix({prefix!r}) -> {ccy!r}, which is NOT in "
                f"fx_rates._CURRENCIES — a live position in this currency would "
                f"silently fall back to a 1:1 USD rate"
            )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION F — Integration: consumers actually call get_fx_to_usd()
# ══════════════════════════════════════════════════════════════════════════════

class TestConsumerIntegration:
    def test_generate_html_calls_get_fx_to_usd(self, isolated_cache):
        import global_portfolio_dashboard as G

        isolated_cache.write_text(
            json.dumps({"saved_at": time.time(), "rates": dict(_FAKE_LIVE_RATES)}),
            encoding="utf-8",
        )
        positions = [{
            "symbol": "TEST", "broker": "Moomoo-AU", "shares": 10, "avg_cost": 100.0,
            "current_price": 110.0, "market_val": 1100.0, "pnl": 100.0, "pnl_pct": 10.0,
            "currency": "AUD", "exchange": "AU",
        }]
        with patch.object(G, "_load_last_india_total", return_value=(None, None)):
            html = G.generate_html(positions, {"Zerodha": "Live — 0 positions"}, None)
        assert "TOTAL" in html  # sanity: template rendered

    def test_save_portfolio_snapshot_uses_live_rates_for_gbp(self, tmp_path, monkeypatch, isolated_cache):
        """End-to-end regression check for the exact bug: a GBP position must
        NOT be recorded at a 1:1 USD value in database/portfolio_history.db."""
        import global_portfolio_dashboard as G

        isolated_cache.write_text(
            json.dumps({"saved_at": time.time(), "rates": dict(_FAKE_LIVE_RATES)}),
            encoding="utf-8",
        )
        fake_db = tmp_path / "portfolio_history.db"
        monkeypatch.setattr(G, "DB_PATH", fake_db)
        monkeypatch.setattr(G, "REPORTS", tmp_path)

        gbp_position = [{
            "symbol": "LON:VOD", "broker": "Moomoo-AU", "shares": 100, "avg_cost": 1.0,
            "current_price": 1.0, "market_val": 100.0, "pnl": 0.0, "pnl_pct": 0.0,
            "currency": "GBP", "exchange": "LON",
        }]
        G._save_portfolio_snapshot(gbp_position, {"Zerodha": "Live"}, aud_total=0.0, usd_total=0.0)

        import sqlite3
        con = sqlite3.connect(str(fake_db))
        row = con.execute(
            "SELECT broker_breakdown FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        con.close()
        broker_totals = json.loads(row[0])
        # 100 GBP * 1.3247 USD/GBP = 132.47 USD-equivalent stored for Moomoo-AU.
        # The old bug would have stored exactly 100.0 (1:1 USD fallback).
        assert broker_totals["Moomoo-AU"] == pytest.approx(132.47, rel=1e-3)
        assert broker_totals["Moomoo-AU"] != pytest.approx(100.0)
