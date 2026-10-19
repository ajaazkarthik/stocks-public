"""
Tests for options_contract_screener.py — US 300 Quality Options Scan.

Coverage:
  • passes_quality_gate() — all filtering rules
  • build_html()          — smoke-test HTML shape
  • scan_symbol()         — mocked fetcher, scored output
  • run_scan()            — full integration with mocked I/O
  • menu handler          — run_options_scan_us_quality() in main.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))

# ── Subject under test ────────────────────────────────────────────────────────

from tradingview.options.options_contract_screener import (
    passes_quality_gate,
    build_html,
    scan_symbol,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1 — passes_quality_gate()
# ══════════════════════════════════════════════════════════════════════════════

class TestPassesQualityGate:
    """Verify the quality gate filters correctly for all edge cases."""

    def _base(self, **overrides) -> dict:
        base = {
            "mid_price":  1.50,
            "spread_pct": 5.0,
            "oi":         500,
            "oi_source":  "moomoo",
        }
        base.update(overrides)
        return base

    def test_good_contract_passes(self):
        assert passes_quality_gate(self._base()) is True

    def test_penny_option_rejected(self):
        assert passes_quality_gate(self._base(mid_price=0.04)) is False

    def test_five_cent_option_passes_minimum_premium(self):
        # The implemented contract floor is $0.05; sub-dime premiums are valid.
        assert passes_quality_gate(self._base(mid_price=0.05)) is True

    def test_exact_min_premium_passes(self):
        assert passes_quality_gate(self._base(mid_price=0.05)) is True

    def test_below_min_premium_rejected(self):
        assert passes_quality_gate(self._base(mid_price=0.049)) is False

    def test_wide_spread_rejected(self):
        assert passes_quality_gate(self._base(spread_pct=20.1)) is False

    def test_max_spread_boundary_passes(self):
        assert passes_quality_gate(self._base(spread_pct=20.0)) is True

    def test_low_oi_moomoo_online_passes(self):
        """OI gate removed — low OI with moomoo source passes."""
        assert passes_quality_gate(self._base(oi=99, oi_source="moomoo")) is True

    def test_low_oi_moomoo_offline_passes(self):
        """When Moomoo is offline (oi_source=unavailable) OI gate is skipped."""
        assert passes_quality_gate(self._base(oi=0, oi_source="unavailable")) is True

    def test_mm_miss_oi_skips_gate(self):
        """When oi_source=mm-miss, OI gate is skipped (neutral scoring)."""
        assert passes_quality_gate(self._base(oi=0, oi_source="mm-miss")) is True

    def test_yfinance_oi_skips_gate(self):
        """When oi_source=yfinance, OI gate is skipped."""
        assert passes_quality_gate(self._base(oi=50, oi_source="yfinance")) is True

    def test_zero_oi_moomoo_online_passes(self):
        """OI gate removed — zero OI passes."""
        assert passes_quality_gate(self._base(oi=0, oi_source="moomoo")) is True

    def test_missing_oi_source_low_oi_passes(self):
        """Without oi_source we don't know if Moomoo was online — don't penalise."""
        assert passes_quality_gate(self._base(oi=0, oi_source="")) is True

    def test_missing_mid_price_rejected(self):
        assert passes_quality_gate(self._base(mid_price=None)) is False

    def test_missing_spread_rejected(self):
        assert passes_quality_gate(self._base(spread_pct=None)) is False


# ══════════════════════════════════════════════════════════════════════════════
# 2 — build_html()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildHtml:
    """Smoke-test the HTML report builder."""

    def _sample_rec(self, **overrides) -> dict:
        base = {
            "symbol":       "NASDAQ:AAPL",
            "strike":       200.0,
            "expiry":       "2026-06-20",
            "dte":          30,
            "mid_price":    3.50,
            "tqs_v2":       72.5,
            "wrs":          58.0,
            "oi":           1200,
            "volume":       800,
            "iv":           28.5,
            "delta":        0.45,
            "spread_pct":   4.2,
            "spot_price":   195.0,
            "moneyness_pct": 2.6,
            "tqs_v2_badge": "[Market Closed]",
            "oi_source":    "moomoo",
            "ws_source":    "closed",
        }
        base.update(overrides)
        return base

    def test_returns_string(self):
        html = build_html([], 0, 0, "2026-01-01 00:00 UTC")
        assert isinstance(html, str)

    def test_contains_doctype(self):
        html = build_html([], 0, 0, "2026-01-01 00:00 UTC")
        assert "<!DOCTYPE html>" in html

    def test_empty_no_contracts_message(self):
        html = build_html([], 0, 0, "2026-01-01 00:00 UTC")
        assert "quality gate" in html.lower() or "no contracts" in html.lower()

    def test_ticker_appears_in_output(self):
        html = build_html([self._sample_rec()], 5, 1, "2026-01-01 00:00 UTC")
        assert "AAPL" in html

    def test_strike_appears_in_output(self):
        html = build_html([self._sample_rec()], 5, 1, "2026-01-01 00:00 UTC")
        assert "200" in html

    def test_tqs_score_appears(self):
        html = build_html([self._sample_rec()], 5, 1, "2026-01-01 00:00 UTC")
        assert "72.5" in html

    def test_scanned_count_appears(self):
        html = build_html([self._sample_rec()], 42, 5, "2026-01-01 00:00 UTC")
        assert "42" in html

    def test_generated_at_appears(self):
        html = build_html([], 0, 0, "2026-05-01 12:34 UTC")
        assert "2026-05-01" in html

    def test_moomoo_badge_when_online(self):
        html = build_html([self._sample_rec(oi_source="moomoo")], 1, 1, "x")
        assert "OI:Moomoo" in html

    def test_offline_badge_when_unavailable(self):
        html = build_html([self._sample_rec(oi_source="unavailable")], 1, 1, "x")
        assert "OI:Offline" in html

    def test_itm_label_correct(self):
        html = build_html([self._sample_rec(moneyness_pct=-5.0)], 1, 1, "x")
        assert "ITM" in html

    def test_otm_label_correct(self):
        html = build_html([self._sample_rec(moneyness_pct=5.0)], 1, 1, "x")
        assert "OTM" in html

    def test_atm_label_correct(self):
        html = build_html([self._sample_rec(moneyness_pct=0.0)], 1, 1, "x")
        assert "ATM" in html

    def test_limits_to_100_cards(self):
        recs = [self._sample_rec(strike=float(k)) for k in range(150)]
        html = build_html(recs, 150, 150, "x")
        # At most 100 cards rendered (each has data-symbol attr)
        assert html.count('data-symbol=') <= 100

    def test_filter_script_present(self):
        html = build_html([], 0, 0, "x")
        assert "filterSymbol" in html

    def test_ws_live_badge(self):
        html = build_html([self._sample_rec(ws_source="live")], 1, 1, "x")
        assert "WS:Live" in html


# ══════════════════════════════════════════════════════════════════════════════
# 3 — scan_symbol()
# ══════════════════════════════════════════════════════════════════════════════

class TestScanSymbol:
    """Verify scan_symbol() scores and filters contracts correctly."""

    def _make_contract(self, **overrides) -> dict:
        base = {
            "code":        "OPRA:AAPL260620C200.0",
            "symbol":      "NASDAQ:AAPL",
            "strike":      200.0,
            "expiry":      "2026-06-20",
            "option_type": "CALL",
            "mid_price":   3.50,
            "bid":         3.40,
            "ask":         3.60,
            "spread_pct":  5.7,
            "oi":          800,
            "oi_source":   "moomoo",
            "volume":      500,
            "iv":          28.0,
            "delta":       0.45,
            "gamma":       0.02,
            "theta":      -0.08,
            "vega":        0.15,
            "dte":         30,
            "spot_price":  195.0,
            "moneyness_pct": 2.6,
            "source":      "hybrid",
            "iv_spread":   1.2,
            "pressure":    0.1,
            "ohlc_range_pct": 55.0,
            "oi_source":   "moomoo",
            "ws_source":   "live",
        }
        base.update(overrides)
        return base

    def _make_fetcher(self, contracts):
        fetcher = MagicMock()
        fetcher.get_calls.return_value = contracts
        return fetcher

    def _dummy_tqs(self, c, **kwargs):
        return 70.0

    def _dummy_wrs(self, c, sig=65.0):
        return 55.0

    def test_returns_list_for_good_contracts(self):
        contracts = [self._make_contract()]
        fetcher   = self._make_fetcher(contracts)
        result    = scan_symbol(
            "NASDAQ:AAPL", fetcher, self._dummy_tqs, self._dummy_wrs
        )
        assert len(result) == 1
        assert result[0]["tqs_v2"] == 70.0
        assert result[0]["wrs"]    == 55.0

    def test_sets_symbol_field(self):
        contracts = [self._make_contract()]
        result    = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            self._dummy_tqs,
            self._dummy_wrs,
        )
        assert result[0]["symbol"] == "NASDAQ:AAPL"

    def test_filters_penny_option(self):
        contracts = [self._make_contract(mid_price=0.02)]
        result    = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            self._dummy_tqs,
            self._dummy_wrs,
        )
        assert result == []

    def test_filters_wide_spread(self):
        contracts = [self._make_contract(spread_pct=25.0)]
        result    = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            self._dummy_tqs,
            self._dummy_wrs,
        )
        assert result == []

    def test_sorted_by_tqs_descending(self):
        scores = [30.0, 80.0, 55.0]

        def _varying_tqs(c, **kwargs):
            return scores.pop(0)

        contracts = [self._make_contract(strike=float(k)) for k in (1, 2, 3)]
        result    = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            _varying_tqs,
            self._dummy_wrs,
        )
        tqs_vals = [r["tqs_v2"] for r in result]
        assert tqs_vals == sorted(tqs_vals, reverse=True)

    def test_returns_empty_on_fetch_error(self):
        fetcher = MagicMock()
        fetcher.get_calls.side_effect = RuntimeError("connection refused")
        result  = scan_symbol(
            "NASDAQ:AAPL", fetcher, self._dummy_tqs, self._dummy_wrs
        )
        assert result == []

    def test_returns_empty_when_fetcher_returns_nothing(self):
        result = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher([]),
            self._dummy_tqs,
            self._dummy_wrs,
        )
        assert result == []

    def test_low_oi_moomoo_offline_passes_gate(self):
        contracts = [self._make_contract(oi=0, oi_source="unavailable")]
        result    = scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            self._dummy_tqs,
            self._dummy_wrs,
        )
        assert len(result) == 1

    def test_passes_iv_spread_and_pressure_to_tqs(self):
        called_with = {}

        def _capture_tqs(c, **kwargs):
            called_with.update(kwargs)
            return 65.0

        contracts = [self._make_contract(iv_spread=2.0, pressure=0.2)]
        scan_symbol(
            "NASDAQ:AAPL",
            self._make_fetcher(contracts),
            _capture_tqs,
            self._dummy_wrs,
        )
        assert "iv_spread" in called_with
        assert "pressure" in called_with


# ══════════════════════════════════════════════════════════════════════════════
# 4 — menu handler: run_options_scan_us_quality()
# ══════════════════════════════════════════════════════════════════════════════

class TestRunOptionsScanUsQuality:
    """Option 10d — US 300 Quality Options Scan handler."""

    def test_calls_screener_script(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "3", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        mock_subprocess_run.assert_called_once()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "options_contract_screener.py" in str(cmd)

    def test_refresh_flag_added_when_yes(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "y", "3", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "--refresh-universe" in cmd

    def test_no_refresh_flag_when_no(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "3", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "--refresh-universe" not in cmd

    def test_n_arg_passed(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["100", "n", "3", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "--n" in cmd
        n_idx = cmd.index("--n")
        assert cmd[n_idx + 1] == "100"

    def test_expiries_arg_passed(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "2", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "--expiries" in cmd
        idx = cmd.index("--expiries")
        assert cmd[idx + 1] == "3"  # handler intentionally scans three expiries

    def test_out_flag_points_to_reports(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "3", "", ""])
        import main as M
        M.run_options_scan_us_quality()
        cmd = mock_subprocess_run.call_args[0][0]
        assert "--out" in cmd
        out_idx = cmd.index("--out")
        assert "options_scan_us_300.html" in cmd[out_idx + 1]

    def test_menu_entry_10d_exists(self):
        import main as M
        keys = [entry[0] for entry in M.MENU]
        assert "10d" in keys

    def test_menu_handler_is_run_options_scan_us_quality(self):
        import main as M
        handler = next(
            (entry[2] for entry in M.MENU if entry[0] == "10d"), None
        )
        assert handler is M.run_options_scan_us_quality


# ── Regression test: Golden Rule #16 — ThreadPoolExecutor shutdown(wait=False) ──


class TestMoomooTimeoutDoesNotBlock:
    """
    Golden Rule #16 regression guard.

    _fetch_moomoo_all() MUST return within timeout_s + 5s overhead even when
    Moomoo OpenD is offline or get_market_snapshot() hangs indefinitely.

    Root cause of the original bug (2026-05-11): using `with ThreadPoolExecutor()`
    calls shutdown(wait=True) on __exit__ — which BLOCKS until the worker finishes
    EVEN AFTER TimeoutError.  Fixed by using shutdown(wait=False) in finally block.

    This test mocks get_market_snapshot to sleep for 60s and asserts that
    _fetch_moomoo_all returns in < 35s (well under the 60s sleep).
    """

    def test_moomoo_fetch_respects_timeout(self, mocker):
        """
        When Moomoo hangs (simulated by a 60s sleep), _fetch_moomoo_all must
        return ([], {}, 'unavailable') within 35 seconds — not 60 seconds.
        """
        import time
        import sys

        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "tradingview"))

        from tradingview.options.hybrid_options import HybridOptionsFetcher

        def _slow_worker(*args, **kwargs):
            """Simulate a hung Moomoo connection."""
            time.sleep(60)
            return ([], {}, "unavailable")

        # Patch USOptionsFetcher so no real OpenD connection is made
        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.is_available",
            return_value=True,
        )
        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.get_expiry_dates",
            return_value=["2026-05-15"],
        )
        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.get_option_chain_for_date",
            side_effect=_slow_worker,
        )

        fetcher = HybridOptionsFetcher(verbose=False)

        t0 = time.time()
        contracts, oi_map, source = fetcher._fetch_moomoo_all(
            "NASDAQ:NVDA",
            n_expiries=1,
            timeout_s=5.0,  # 5s timeout; worker sleeps 60s
        )
        elapsed = time.time() - t0

        # Must return within timeout + 5s overhead (not 60s)
        assert elapsed < 12.0, (
            f"_fetch_moomoo_all blocked for {elapsed:.1f}s — "
            f"shutdown(wait=True) regression re-introduced! "
            f"Expected < 12s with timeout_s=5."
        )
        assert contracts == []
        assert oi_map == {}
        assert source == "unavailable"

    def test_moomoo_fetch_succeeds_when_fast(self, mocker):
        """
        When Moomoo responds immediately, _fetch_moomoo_all returns real data.
        """
        import sys

        sys.path.insert(0, str(ROOT))

        from tradingview.options.hybrid_options import HybridOptionsFetcher

        fake_chain = [
            {
                "strike": 220.0,
                "expiry": "2026-05-15",
                "option_type": "CALL",
                "oi": 5000,
                "bid": 1.0,
                "ask": 1.05,
            }
        ]

        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.is_available",
            return_value=True,
        )
        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.get_expiry_dates",
            return_value=["2026-05-15"],
        )
        mocker.patch(
            "tradingview.options.us_options.USOptionsFetcher.get_option_chain_for_date",
            return_value=fake_chain,
        )

        fetcher = HybridOptionsFetcher(verbose=False)
        contracts, oi_map, source = fetcher._fetch_moomoo_all(
            "NASDAQ:NVDA",
            n_expiries=1,
            timeout_s=10.0,
        )

        assert source == "moomoo"
        assert len(oi_map) == 1
        assert (220.0, 20260515, "CALL") in oi_map
        assert oi_map[(220.0, 20260515, "CALL")] == 5000


# ══════════════════════════════════════════════════════════════════════════════
# 5 — inject_screener_subscores(): weekend spread_pct fix (Behaviour 1)
# ══════════════════════════════════════════════════════════════════════════════


class TestWeekendSpreadFix:
    """
    Behaviour 1: When bid=ask=0 (market closed) spread_pct=0.0 is meaningless.

    New rule (after fix):
      oi > 0  →  spd_s = 5.0 (neutral — OI proves the contract exists)
      oi == 0 →  spd_s = 0.0 (penalty — no proof of liquidity)

    Old (broken) rule: spread_pct=0.0 → 10*(1 − 0/10) = 10.0 → spd_s=10.0 for
    both cases, inflating liq_score on weekends.
    """

    def _weekend_contract(self, **overrides) -> dict:
        """A market-closed contract: bid=ask=0, spread_pct=0 (TV/Moomoo both zero)."""
        base = {
            "bid":        0,
            "ask":        0,
            "spread_pct": 0.0,
            "volume":     0,
            "oi_source":  "moomoo",
            "iv":         30.0,
            "delta":      0.45,
            "dte":        30,
        }
        base.update(overrides)
        return base

    def test_weekend_spread_neutral_when_oi_positive(self):
        """bid=ask=0, oi=1000 → spd_s=5.0 (neutral), OI not in scoring → liq_score from spread+vol."""
        from tradingview.options.hybrid_options import inject_screener_subscores

        c = self._weekend_contract(oi=1000)
        inject_screener_subscores(c)

        # spd_s=5.0, vol_s=0.0  (OI excluded from liq_score)
        # liq_score = 5.0*0.55 + 0.0*0.45 = 2.75
        assert c["liq_score"] == pytest.approx(2.75, abs=0.01)

    def test_weekend_spread_penalty_when_oi_zero(self):
        """bid=ask=0, oi=0, oi_source=moomoo → spd_s=0.0 (penalty), OI excluded → liq_score=0.0."""
        from tradingview.options.hybrid_options import inject_screener_subscores

        c = self._weekend_contract(oi=0, oi_source="moomoo")
        inject_screener_subscores(c)

        # spd_s=0.0, vol_s=0.0 (OI excluded from liq_score) → 0.0
        assert c["liq_score"] == pytest.approx(0.0, abs=0.01)

    @pytest.mark.parametrize(
        "oi, oi_source, regime",
        [
            (500, "moomoo",     "neutral"),    # oi>0, moomoo live  → spd_s=5.0
            (0,   "moomoo",     "penalty"),    # oi=0, moomoo live  → spd_s=0.0
            (0,   "unavailable","penalty"),    # oi=0, moomoo offline → spd_s=0.0 (OI no longer rescues)
        ],
    )
    def test_weekend_spread_regimes(self, oi, oi_source, regime):
        """Parametrize weekend spread regimes (OI excluded from scoring)."""
        from tradingview.options.hybrid_options import inject_screener_subscores

        c = self._weekend_contract(oi=oi, oi_source=oi_source, volume=0)
        inject_screener_subscores(c)

        assert "liq_score" in c
        assert 0.0 <= c["liq_score"] <= 10.0

        if regime == "penalty":
            # spd_s=0, vol_s=0, OI excluded → liq_score = 0.0
            assert c["liq_score"] == pytest.approx(0.0)
        else:
            # spd_s=5.0 (neutral) → liq_score = 5.0*0.55 = 2.75
            assert c["liq_score"] > 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 6 — HybridOptionsFetcher.get_calls(): yfinance OI fallback (Behaviour 2)
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# 7 — options_actionable_report: filter_actionable()
# ══════════════════════════════════════════════════════════════════════════════

from tradingview.options.options_actionable_report import (
    filter_actionable,
    enrich_with_signal_score,
    enrich_with_support,
    _actionable_card_html,
    build_actionable_html,
    run_actionable,
    _grade,
)


def _ok_contract(**kwargs) -> dict:
    """Minimal contract that passes all filters by default."""
    # filter_actionable defaults to TQS >= 65; keep unrelated filter tests
    # above that floor unless a test explicitly exercises the score threshold.
    base = {"be_sigma_ratio": 0.40, "oi": 200, "spread_pct": 2.0, "dte": 7, "tqs_v2": 70}
    base.update(kwargs)
    return base


class TestFilterActionable:
    """Verify multi-filter logic in filter_actionable()."""

    # ── BE/1σ ratio (filter 1+2) ──────────────────────────────────────────

    def test_passes_cheap(self):
        contracts = [_ok_contract(be_sigma_ratio=0.30, tqs_v2=80)]
        assert len(filter_actionable(contracts)) == 1

    def test_passes_fair(self):
        contracts = [_ok_contract(be_sigma_ratio=0.50, tqs_v2=70)]
        assert len(filter_actionable(contracts)) == 1

    def test_rejects_expensive(self):
        contracts = [_ok_contract(be_sigma_ratio=0.90, tqs_v2=80)]
        assert len(filter_actionable(contracts)) == 0

    def test_rejects_inf(self):
        contracts = [_ok_contract(be_sigma_ratio=float("inf"), tqs_v2=80)]
        assert len(filter_actionable(contracts)) == 0

    def test_missing_ratio_defaults_to_inf(self):
        contracts = [{"tqs_v2": 80, "oi": 200, "spread_pct": 2.0, "dte": 7}]  # no be_sigma_ratio key
        assert len(filter_actionable(contracts)) == 0

    def test_custom_threshold_accepts(self):
        contracts = [_ok_contract(be_sigma_ratio=0.60, tqs_v2=70)]
        assert len(filter_actionable(contracts, max_be_sigma=0.70)) == 1

    def test_custom_threshold_rejects(self):
        contracts = [_ok_contract(be_sigma_ratio=0.60)]
        assert len(filter_actionable(contracts, max_be_sigma=0.55)) == 0

    @pytest.mark.parametrize("score, expected", [(64.99, 0), (65.0, 1)])
    def test_tqs_floor_boundary(self, score, expected):
        assert len(filter_actionable([_ok_contract(tqs_v2=score)])) == expected

    def test_boundary_inclusive(self):
        """≤ not < — boundary value must pass."""
        contracts = [_ok_contract(be_sigma_ratio=0.55)]
        assert len(filter_actionable(contracts, max_be_sigma=0.55)) == 1

    def test_empty_input(self):
        assert len(filter_actionable([])) == 0

    def test_multiple_mixed(self):
        """Only contracts within threshold survive."""
        contracts = [
            _ok_contract(be_sigma_ratio=0.30),
            _ok_contract(be_sigma_ratio=0.55),
            _ok_contract(be_sigma_ratio=0.70),
            _ok_contract(be_sigma_ratio=0.10),
        ]
        result = filter_actionable(contracts, max_be_sigma=0.55)
        assert len(result) == 3  # 0.30, 0.55, 0.10

    def test_nan_rejected(self):
        contracts = [_ok_contract(be_sigma_ratio=float("nan"))]
        assert len(filter_actionable(contracts)) == 0

    def test_negative_ratio_passes(self):
        """ITM contracts can have negative BE/1σ — those should pass."""
        contracts = [_ok_contract(be_sigma_ratio=-0.10)]
        assert len(filter_actionable(contracts)) == 1

    # ── Spread filter (filter 3) ──────────────────────────────────────────

    def test_rejects_wide_spread(self):
        """Spread > 6% means can't exit profitably — reject."""
        contracts = [_ok_contract(spread_pct=7.5)]
        assert len(filter_actionable(contracts)) == 0

    def test_passes_tight_spread(self):
        contracts = [_ok_contract(spread_pct=3.0)]
        assert len(filter_actionable(contracts)) == 1

    def test_spread_boundary(self):
        """Exactly 5.0% is allowed by the current default threshold."""
        contracts = [_ok_contract(spread_pct=5.0)]
        assert len(filter_actionable(contracts)) == 1

    def test_missing_spread_treated_as_zero(self):
        """No spread key → treat as 0 → passes."""
        c = _ok_contract()
        del c["spread_pct"]
        assert len(filter_actionable([c])) == 1

    # ── OI filter REMOVED — OI is display-only ──────────────────────────

    def test_rejects_low_oi(self):
        """OI=50 — passes now (OI removed from filtering)."""
        contracts = [_ok_contract(oi=50, volume=0)]
        assert len(filter_actionable(contracts)) == 1

    def test_passes_sufficient_oi(self):
        contracts = [_ok_contract(oi=500)]
        assert len(filter_actionable(contracts)) == 1

    def test_oi_boundary(self):
        """OI=100 — passes (OI removed from filtering)."""
        contracts = [_ok_contract(oi=100)]
        assert len(filter_actionable(contracts)) == 1

    def test_missing_oi_treated_as_zero(self):
        """No OI key — passes (OI removed from filtering)."""
        c = _ok_contract()
        del c["oi"]
        c["volume"] = 0
        assert len(filter_actionable([c])) == 1

    def test_open_interest_field_alias(self):
        """open_interest field is also accepted as OI source."""
        c = _ok_contract()
        del c["oi"]
        c["open_interest"] = 200
        assert len(filter_actionable([c])) == 1

    # ── F4 volume bypass (T+1 OI settlement lag) ──────────────────────────
    # OI is published overnight by exchanges.  Contracts with high volume but
    # OI=0 are legitimately liquid — the OI just hasn’t settled yet.
    # Evidence: NVDA C240 14d OI=0 Vol=11,582; TSLA C440 3d OI=0 Vol=20,800.

    def test_f4_zero_oi_high_volume_passes(self):
        """Zero OI with ≥500 volume passes F4 (T+1 settlement lag bypass)."""
        c = _ok_contract(oi=0, volume=600)
        assert len(filter_actionable([c])) == 1

    def test_f4_zero_oi_boundary_volume_passes(self):
        """Exactly 500 volume is the inclusive boundary — must pass F4."""
        c = _ok_contract(oi=0, volume=500)
        assert len(filter_actionable([c])) == 1

    def test_f4_zero_oi_low_volume_passes(self):
        """Zero OI with <500 volume — passes (OI removed from filtering)."""
        c = _ok_contract(oi=0, volume=100)
        assert len(filter_actionable([c])) == 1

    def test_f4_zero_oi_zero_volume_passes(self):
        """Zero OI with zero volume — passes (OI removed from filtering)."""
        c = _ok_contract(oi=0, volume=0)
        assert len(filter_actionable([c])) == 1

    def test_f4_zero_oi_missing_volume_passes(self):
        """Zero OI with no volume key — passes (OI removed from filtering)."""
        c = _ok_contract(oi=0)
        # no 'volume' key at all
        assert len(filter_actionable([c])) == 1

    def test_f4_good_oi_zero_volume_passes(self):
        """Sufficient OI (300 ≥ 200) passes F4 even with volume=0 (regression guard)."""
        c = _ok_contract(oi=300, volume=0)
        assert len(filter_actionable([c])) == 1

    def test_f4_custom_min_volume(self):
        """Custom min_volume no longer affects filtering (OI+volume removed from filter)."""
        c = _ok_contract(oi=0, volume=600)
        assert len(filter_actionable([c], min_volume=1000)) == 1

    def test_f4_nvda_like_scenario(self):
        """NVDA-like: OI=0, volume=11582 (T+1 lag) → passes F4."""
        c = _ok_contract(oi=0, volume=11582)
        assert len(filter_actionable([c])) == 1

    def test_f4_no_oi_filter_label(self):
        """F4-liq label should never appear (OI filter removed)."""
        from tradingview.options.options_actionable_report import filter_actionable_tracked
        c = _ok_contract(oi=0, volume=0)
        passed, rejected = filter_actionable_tracked([c])
        reasons = [r.get("filter") for r in rejected]
        assert "F4-liq" not in reasons, f"F4-liq should not exist: {reasons}"

    def test_f4_volume_bypass_no_f4_label(self):
        """Contracts passing F4 via volume bypass must NOT carry F4-liq label."""
        from tradingview.options.options_actionable_report import filter_actionable_tracked
        c = _ok_contract(oi=0, volume=600)
        passed, rejected = filter_actionable_tracked([c])
        assert passed, "Expected contract to pass all filters"
        reasons = [r.get("filter") for r in rejected]
        assert "F4-liq" not in reasons

    # ── 1-DTE leverage trap (filter 5) ───────────────────────────────────

    def test_1dte_high_leverage_rejected(self):
        """1-DTE with >50× leverage is a binary bet — reject."""
        contracts = [_ok_contract(dte=1, leverage=80.0)]
        assert len(filter_actionable(contracts)) == 0

    def test_1dte_low_leverage_passes(self):
        contracts = [_ok_contract(dte=1, leverage=20.0)]
        assert len(filter_actionable(contracts, min_dte=0)) == 1

    def test_multiday_high_leverage_passes(self):
        """14-DTE leverage may exceed the 1-DTE cap but stays under the global 65x cap."""
        contracts = [_ok_contract(dte=14, leverage=60.0)]
        assert len(filter_actionable(contracts)) == 1

    # ── 1-DTE theta death (filter 6) ─────────────────────────────────────

    def test_1dte_theta_death_rejected(self):
        """1-DTE with >40% daily theta decay is effectively worthless by close."""
        contracts = [_ok_contract(dte=1, theta_day_pct=50.0)]
        assert len(filter_actionable(contracts)) == 0

    def test_1dte_low_theta_passes(self):
        contracts = [_ok_contract(dte=1, theta_day_pct=15.0)]
        assert len(filter_actionable(contracts, min_dte=0)) == 1

    def test_multiday_high_theta_passes(self):
        """High theta % is only a flag on 1-DTE."""
        contracts = [_ok_contract(dte=7, theta_day_pct=50.0)]
        assert len(filter_actionable(contracts)) == 1

    # ── SMA200 trend filter (filter 7) ───────────────────────────────────

    def test_below_sma200_rejected(self):
        """Buying CALL on stock below SMA200 fights primary trend."""
        contracts = [_ok_contract(above_sma200=False)]
        assert len(filter_actionable(contracts)) == 0

    def test_above_sma200_passes(self):
        contracts = [_ok_contract(above_sma200=True)]
        assert len(filter_actionable(contracts)) == 1

    def test_unknown_sma200_passes(self):
        """above_sma200=None means unknown — allow (don't penalise missing data)."""
        contracts = [_ok_contract(above_sma200=None)]
        assert len(filter_actionable(contracts)) == 1

    def test_missing_sma200_passes(self):
        """No above_sma200 key → unknown → allow."""
        c = _ok_contract()
        assert "above_sma200" not in c
        assert len(filter_actionable([c])) == 1

    def test_sma200_filter_can_be_disabled(self):
        contracts = [_ok_contract(above_sma200=False)]
        assert len(filter_actionable(contracts, require_above_sma200=False)) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 8 — options_actionable_report: enrich_with_signal_score()
# ══════════════════════════════════════════════════════════════════════════════


class _MockScorer:
    """Deterministic scorer for testing."""
    def score(self, ticker: str) -> float:
        scores = {"NVDA": 72.5, "TSLA": 45.0, "AAPL": 83.0}
        return scores.get(ticker, 50.0)


class TestEnrichWithSignalScore:
    """Verify signal engine enrichment with mock scorer."""

    def test_applies_score(self):
        contracts = [{"symbol": "NASDAQ:NVDA", "tqs_v2": 80}]
        result = enrich_with_signal_score(contracts, scorer=_MockScorer())
        assert result[0]["signal_composite"] == 72.5
        assert result[0]["signal_grade"] == "B"

    def test_multiple_tickers(self):
        contracts = [
            {"symbol": "NASDAQ:NVDA", "tqs_v2": 80},
            {"symbol": "NASDAQ:TSLA", "tqs_v2": 70},
            {"symbol": "NASDAQ:NVDA", "tqs_v2": 75},  # duplicate ticker
        ]
        result = enrich_with_signal_score(contracts, scorer=_MockScorer())
        assert result[0]["signal_composite"] == 72.5
        assert result[1]["signal_composite"] == 45.0
        assert result[2]["signal_composite"] == 72.5  # same as first NVDA

    def test_none_scorer_result(self):
        """When scorer returns None, signal_composite is None."""
        class NoneScorer:
            def score(self, ticker):
                return None
        contracts = [{"symbol": "NASDAQ:NVDA"}]
        result = enrich_with_signal_score(contracts, scorer=NoneScorer())
        assert result[0]["signal_composite"] is None

    def test_grade_A_for_high_score(self):
        class HighScorer:
            def score(self, ticker):
                return 83.0
        contracts = [{"symbol": "NASDAQ:AAPL"}]
        result = enrich_with_signal_score(contracts, scorer=HighScorer())
        assert result[0]["signal_grade"] == "A"

    def test_bare_ticker_without_exchange(self):
        """Symbol without colon should still work."""
        contracts = [{"symbol": "NVDA"}]
        result = enrich_with_signal_score(contracts, scorer=_MockScorer())
        assert result[0]["signal_composite"] == 72.5


# ══════════════════════════════════════════════════════════════════════════════
# 9 — options_actionable_report: enrich_with_support()
# ══════════════════════════════════════════════════════════════════════════════


class _MockSupportFetcher:
    """Deterministic support fetcher for testing."""
    def fetch(self, ticker: str) -> dict:
        return {
            "key_support": 130.0, "hold_score": 8,
            "is_holding": True, "put_wall": 125.0,
            "put_wall_oi": 15000, "data_quality": "full",
            "trend": "bullish", "sma200": 120.0, "sma50": 128.0,
        }


class TestEnrichWithSupport:
    """Verify support level enrichment with mock fetcher."""

    def test_applies_support_data(self):
        contracts = [{"symbol": "NASDAQ:NVDA"}]
        result = enrich_with_support(contracts, fetcher=_MockSupportFetcher())
        assert result[0]["support_key"] == 130.0
        assert result[0]["support_hold_score"] == 8
        assert result[0]["support_is_holding"] is True
        assert result[0]["support_put_wall"] == 125.0

    def test_caches_per_ticker(self):
        """Same ticker should only be fetched once."""
        calls = []

        class CountingFetcher:
            def fetch(self, ticker):
                calls.append(ticker)
                return {
                    "key_support": 100, "hold_score": 5, "is_holding": True,
                    "put_wall": None, "put_wall_oi": None, "data_quality": "partial",
                    "trend": None, "sma200": None, "sma50": None,
                }

        contracts = [
            {"symbol": "NASDAQ:NVDA"},
            {"symbol": "NASDAQ:NVDA"},  # same ticker
        ]
        enrich_with_support(contracts, fetcher=CountingFetcher())
        assert len(calls) == 1  # called only once for NVDA

    def test_different_tickers_both_fetched(self):
        calls = []

        class CountingFetcher:
            def fetch(self, ticker):
                calls.append(ticker)
                return {
                    "key_support": 100, "hold_score": 5, "is_holding": True,
                    "put_wall": None, "put_wall_oi": None, "data_quality": "partial",
                    "trend": None, "sma200": None, "sma50": None,
                }

        contracts = [
            {"symbol": "NASDAQ:NVDA"},
            {"symbol": "NASDAQ:TSLA"},
        ]
        enrich_with_support(contracts, fetcher=CountingFetcher())
        assert len(calls) == 2

    def test_support_quality_field_mapped(self):
        contracts = [{"symbol": "NASDAQ:NVDA"}]
        result = enrich_with_support(contracts, fetcher=_MockSupportFetcher())
        assert result[0]["support_quality"] == "full"

    def test_support_sma_fields_mapped(self):
        contracts = [{"symbol": "NASDAQ:NVDA"}]
        result = enrich_with_support(contracts, fetcher=_MockSupportFetcher())
        assert result[0]["support_sma200"] == 120.0
        assert result[0]["support_sma50"] == 128.0


# ══════════════════════════════════════════════════════════════════════════════
# 10 — options_actionable_report: _grade()
# ══════════════════════════════════════════════════════════════════════════════


class TestGrade:
    """Verify the grading function thresholds."""

    def test_grade_A(self):
        assert _grade(80)[0] == "A"

    def test_grade_A_boundary(self):
        assert _grade(75)[0] == "A"

    def test_grade_B(self):
        assert _grade(65)[0] == "B"

    def test_grade_B_boundary(self):
        assert _grade(60)[0] == "B"

    def test_grade_C(self):
        assert _grade(50)[0] == "C"

    def test_grade_C_boundary(self):
        assert _grade(45)[0] == "C"

    def test_grade_D(self):
        assert _grade(35)[0] == "D"

    def test_grade_D_boundary(self):
        assert _grade(30)[0] == "D"

    def test_grade_F(self):
        assert _grade(20)[0] == "F"

    def test_grade_F_zero(self):
        assert _grade(0)[0] == "F"

    def test_grade_None(self):
        assert _grade(None)[0] == "—"

    def test_grade_returns_color(self):
        """Each grade must return a hex color string."""
        for score in [80, 65, 50, 35, 20, None]:
            _, color = _grade(score)
            assert color.startswith("#")


# ══════════════════════════════════════════════════════════════════════════════
# 11 — options_actionable_report: _actionable_card_html()
# ══════════════════════════════════════════════════════════════════════════════


class TestActionableCardHtml:
    """Verify the HTML card rendering for actionable contracts."""

    def _base_rec(self, **overrides) -> dict:
        base = {
            "symbol": "NASDAQ:NVDA", "strike": 140, "expiry": "2026-06-20",
            "dte": 39, "mid_price": 5.0, "tqs_v2": 80, "wrs": 65, "premium_win": 45,
            "oi": 5000, "volume": 1200, "iv": 35.0, "delta": 0.55,
            "spread_pct": 1.2, "spot_price": 135.0, "moneyness_pct": 3.7,
            "signal_composite": 72, "signal_grade": "B", "signal_grade_color": "#84cc16",
            "support_key": 130.0, "support_hold_score": 8, "support_is_holding": True,
            "support_put_wall": 125.0, "support_put_wall_oi": 15000,
            "support_quality": "full", "support_trend": "bullish",
            "support_sma200": 120.0, "support_sma50": 128.0,
        }
        base.update(overrides)
        return base

    def test_contains_be_sigma(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "BE/1σ" in html

    def test_contains_signal_engine(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "Signal Engine" in html

    def test_contains_support_floor(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "Support Floor" in html

    def test_holding_above_support(self):
        html = _actionable_card_html(1, self._base_rec(support_is_holding=True))
        assert "Holding above support" in html

    def test_broken_below_support(self):
        rec = self._base_rec(
            symbol="NASDAQ:TSLA", strike=200, mid_price=8.0, tqs_v2=60,
            wrs=40, premium_win=30, oi=3000, volume=800, iv=55.0, delta=0.45,
            spread_pct=2.0, spot_price=195.0, moneyness_pct=2.6,
            signal_composite=45, signal_grade="C", signal_grade_color="#eab308",
            support_key=198.0, support_hold_score=3, support_is_holding=False,
            support_put_wall=None, support_put_wall_oi=None,
            support_quality="partial", support_trend=None,
            support_sma200=None, support_sma50=None,
        )
        html = _actionable_card_html(2, rec)
        assert "Broken below support" in html

    def test_unavailable_signal(self):
        rec = self._base_rec(
            signal_composite=None,
            support_key=None, support_hold_score=0, support_is_holding=None,
            support_put_wall=None, support_put_wall_oi=None,
            support_quality="unavailable", support_trend=None,
            support_sma200=None, support_sma50=None,
        )
        html = _actionable_card_html(3, rec)
        assert "Unavailable" in html
        assert "Support data unavailable" in html

    def test_ticker_appears_in_card(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "NVDA" in html

    def test_strike_appears_in_card(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "C140" in html

    def test_data_symbol_attribute(self):
        html = _actionable_card_html(1, self._base_rec())
        assert 'data-symbol="NVDA"' in html

    def test_put_wall_oi_formatted(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "OI:15,000" in html

    def test_sma200_shown(self):
        html = _actionable_card_html(1, self._base_rec())
        assert "SMA200" in html
        assert "120.00" in html

    def test_no_support_data_message(self):
        rec = self._base_rec(
            support_key=None, support_sma200=None, support_sma50=None,
            support_put_wall=None, support_put_wall_oi=None,
        )
        html = _actionable_card_html(1, rec)
        assert "No support data" in html


# ══════════════════════════════════════════════════════════════════════════════
# 12 — options_actionable_report: build_actionable_html()
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildActionableHtml:
    """Verify the full actionable report HTML builder."""

    def test_empty_report(self):
        html = build_actionable_html([], 50, 0, "2026-05-12 10:00 UTC")
        assert "No contracts passed" in html

    def test_contains_doctype(self):
        html = build_actionable_html([], 0, 0, "2026-05-12 10:00 UTC")
        assert "<!DOCTYPE html>" in html

    def test_with_contracts(self):
        contracts = [{
            "symbol": "NASDAQ:NVDA", "strike": 140, "expiry": "2026-06-20",
            "dte": 39, "mid_price": 5.0, "tqs_v2": 80, "wrs": 65, "premium_win": 45,
            "oi": 5000, "volume": 1200, "iv": 35.0, "delta": 0.55,
            "spread_pct": 1.2, "spot_price": 135.0, "moneyness_pct": 3.7,
            "be_sigma_ratio": 0.35,
            "signal_composite": 72, "signal_grade": "B", "signal_grade_color": "#84cc16",
            "support_key": 130.0, "support_hold_score": 8, "support_is_holding": True,
            "support_put_wall": 125.0, "support_put_wall_oi": 15000,
            "support_quality": "full", "support_trend": "bullish",
            "support_sma200": 120.0, "support_sma50": 128.0,
        }]
        html = build_actionable_html(contracts, 50, 100, "2026-05-12 10:00 UTC")
        assert "Actionable: <b>1</b>" in html
        assert "NVDA" in html
        assert "Signal Engine" in html

    def test_scanned_count_in_report(self):
        html = build_actionable_html([], 42, 100, "2026-05-12")
        assert "42" in html

    def test_generated_at_in_report(self):
        html = build_actionable_html([], 0, 0, "2026-05-12 10:00 UTC")
        assert "2026-05-12" in html

    def test_max_be_sigma_shown(self):
        html = build_actionable_html([], 0, 0, "x", max_be_sigma=0.60)
        assert "0.60" in html

    def test_holding_count(self):
        contracts = [
            {
                "symbol": "NASDAQ:NVDA", "strike": 140, "expiry": "2026-06-20",
                "dte": 39, "mid_price": 5.0, "tqs_v2": 80, "wrs": 65, "premium_win": 45,
                "oi": 5000, "volume": 1200, "iv": 35.0, "delta": 0.55,
                "spread_pct": 1.2, "spot_price": 135.0, "moneyness_pct": 3.7,
                "be_sigma_ratio": 0.35, "support_is_holding": True,
                "signal_composite": 72, "signal_grade": "B", "signal_grade_color": "#84cc16",
                "support_key": 130.0, "support_hold_score": 8,
                "support_put_wall": 125.0, "support_put_wall_oi": 15000,
                "support_quality": "full", "support_trend": "bullish",
                "support_sma200": 120.0, "support_sma50": 128.0,
            },
        ]
        html = build_actionable_html(contracts, 10, 50, "x")
        assert "Holding Support: <b>1/1</b>" in html


# ══════════════════════════════════════════════════════════════════════════════
# 13 — options_actionable_report: run_actionable() full pipeline
# ══════════════════════════════════════════════════════════════════════════════


class _FakeScorer:
    def score(self, t):
        return 70.0


class _FakeFetcher:
    def fetch(self, t):
        return {
            "key_support": 130.0, "hold_score": 7, "is_holding": True,
            "put_wall": 125.0, "put_wall_oi": 10000, "data_quality": "full",
            "trend": "bullish", "sma200": 120.0, "sma50": 128.0,
        }


class TestRunActionable:
    """Verify the full run_actionable() pipeline with mocked dependencies."""

    def _make_contract(self, symbol="NASDAQ:NVDA", be_sigma=0.35, **kw):
        base = {
            "symbol": symbol, "strike": 140, "expiry": "2026-06-20",
            "dte": 39, "mid_price": 5.0, "tqs_v2": 80, "wrs": 65, "premium_win": 45,
            "oi": 5000, "volume": 1200, "iv": 35.0, "delta": 0.55,
            "spread_pct": 1.2, "spot_price": 135.0, "moneyness_pct": 3.7,
            "be_sigma_ratio": be_sigma,
        }
        base.update(kw)
        return base

    def test_full_pipeline(self, tmp_path):
        contracts = [
            self._make_contract("NASDAQ:NVDA", 0.35),
            self._make_contract("NASDAQ:TSLA", 0.70, strike=200, mid_price=8.0,
                                tqs_v2=60, wrs=40, premium_win=30, spot_price=195.0),
        ]
        out = tmp_path / "test_actionable.html"
        result = run_actionable(
            contracts, out,
            signal_scorer=_FakeScorer(), support_fetcher=_FakeFetcher(),
            scanned_count=50, found_count=100,
        )
        assert len(result) == 1  # only NVDA passes (0.35 ≤ 0.55)
        assert result[0]["signal_composite"] == 70.0
        assert result[0]["support_key"] == 130.0
        assert out.exists()
        html = out.read_text(encoding="utf-8")
        assert "NVDA" in html
        assert "TSLA" not in html

    def test_skip_enrichment(self, tmp_path):
        contracts = [self._make_contract()]
        out = tmp_path / "test_skip.html"
        result = run_actionable(contracts, out, skip_signal=True, skip_support=True)
        assert len(result) == 1
        assert out.exists()

    def test_empty_input(self, tmp_path):
        out = tmp_path / "test_empty.html"
        result = run_actionable([], out, skip_signal=True, skip_support=True)
        assert len(result) == 0
        assert out.exists()  # empty report still written

    def test_all_filtered_out(self, tmp_path):
        """When all contracts exceed BE/1σ, result is empty but report is written."""
        contracts = [
            self._make_contract(be_sigma=0.85),
            self._make_contract(be_sigma=0.95),
        ]
        out = tmp_path / "test_all_out.html"
        result = run_actionable(contracts, out, skip_signal=True, skip_support=True)
        assert len(result) == 0
        assert out.exists()

    def test_custom_max_be_sigma(self, tmp_path):
        contracts = [
            self._make_contract(be_sigma=0.60),
            self._make_contract(be_sigma=0.65),
        ]
        out = tmp_path / "test_custom.html"
        result = run_actionable(
            contracts, out,
            max_be_sigma=0.62,
            skip_signal=True, skip_support=True,
        )
        assert len(result) == 1  # only 0.60 passes

    def test_sorted_by_tqs_descending(self, tmp_path):
        contracts = [
            self._make_contract(be_sigma=0.30, tqs_v2=60),
            self._make_contract(be_sigma=0.30, tqs_v2=90),
            self._make_contract(be_sigma=0.30, tqs_v2=75),
        ]
        out = tmp_path / "test_sorted.html"
        result = run_actionable(
            contracts, out, skip_signal=True, skip_support=True,
        )
        tqs_vals = [r["tqs_v2"] for r in result]
        assert tqs_vals == [90, 75]  # score 60 is below filter_actionable's 65 floor

    def test_computes_be_sigma_if_missing(self, tmp_path):
        """run_actionable should compute be_sigma_ratio from component fields."""
        contract = {
            "symbol": "NASDAQ:NVDA", "strike": 140, "expiry": "2026-06-20",
            "dte": 39, "mid_price": 5.0, "tqs_v2": 80, "wrs": 65, "premium_win": 45,
            "oi": 5000, "volume": 1200, "iv": 35.0, "delta": 0.55,
            "spread_pct": 1.2, "spot_price": 135.0, "moneyness_pct": 3.7,
            # no be_sigma_ratio key — should be computed
        }
        out = tmp_path / "test_compute.html"
        result = run_actionable(
            [contract], out, skip_signal=True, skip_support=True,
        )
        # With these params: BE% = (140+5)/135 - 1 = 7.41%
        # σ_T = 0.35 * sqrt(39/365) * 100 = 11.44%
        # ratio = 7.41 / 11.44 ≈ 0.648 → passes at 0.80 default
        assert len(result) == 1

    def test_creates_output_directory(self, tmp_path):
        """run_actionable should create parent dirs if they don't exist."""
        out = tmp_path / "sub" / "dir" / "report.html"
        run_actionable([], out, skip_signal=True, skip_support=True)
        assert out.exists()


class TestYfinanceOIFallback:
    """
    Behaviour 2: When Moomoo returns oi_source="unavailable", get_calls() falls
    back to yfinance.Ticker.option_chain() to populate OI, volume, and mid_price.
    """

    def test_yfinance_oi_fallback_when_moomoo_offline(self, mocker):
        """OI=66000, volume=1500 from yfinance should appear in the returned contracts."""
        import pandas as pd
        from tradingview.options.hybrid_options import HybridOptionsFetcher

        # ── Fake TV contract (NVDA CALL at 200.0, expiry 2026-05-15) ──────
        fake_tv_contract = {
            "code":          "OPRA:NVDA260515C200.0",
            "strike":        200.0,
            "expiry":        "2026-05-15",
            "option_type":   "CALL",
            "mid_price":     0.0,
            "bid":           0.0,
            "ask":           0.0,
            "spread_pct":    0.0,
            "oi":            0,
            "volume":        0,
            "iv":            35.0,
            "delta":         0.40,
            "gamma":         0.02,
            "theta":        -0.05,
            "vega":          0.10,
            "dte":           3,
            "spot_price":    195.0,
            "moneyness_pct": 2.6,
            "bid_iv":        0.0,
            "ask_iv":        0.0,
        }

        # ── Mock the three data-source methods ───────────────────────────
        mocker.patch.object(
            HybridOptionsFetcher, "_fetch_tv",
            return_value=[fake_tv_contract],
        )
        # Moomoo offline: no contracts, no OI map, source="unavailable"
        mocker.patch.object(
            HybridOptionsFetcher, "_fetch_moomoo_all",
            return_value=([], {}, "unavailable"),
        )
        # WebSocket offline: no live data
        mocker.patch.object(
            HybridOptionsFetcher, "_fetch_ws",
            return_value=({}, "last-session"),
        )

        # yfinance OI fallback was removed — OI is now display-only.
        # When Moomoo is offline, contracts keep oi=0, oi_source="unavailable".
        fetcher = HybridOptionsFetcher(verbose=False)
        contracts = fetcher.get_calls("NASDAQ:NVDA", n_expiries=1)

        assert contracts, "Expected at least one contract from TV scan2"
        c = contracts[0]

        # OI stays 0 — no Moomoo, no yfinance enrichment
        assert c["oi"] == 0, f"Expected oi=0 (unavailable), got {c['oi']}"
        assert c["oi_source"] == "unavailable", (
            f"Expected oi_source='unavailable', got {c['oi_source']!r}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 7 — run_scan(): error handling in the scan loop (Behaviour 3)
# ══════════════════════════════════════════════════════════════════════════════


class TestRunScanErrorHandling:
    """
    Behaviour 3: scan_symbol() errors are caught and skipped (try/except in
    the run_scan loop) instead of crashing the entire scan.
    """

    def test_scan_symbol_error_does_not_crash_scan(self, mocker, tmp_path):
        """ValueError from scan_symbol() must be caught; run_scan() completes normally."""
        from tradingview.options.options_contract_screener import run_scan

        # ── Mock the fetcher class ────────────────────────────────────────
        mock_fetcher_instance = MagicMock()
        mock_fetcher_instance.is_available.return_value = True
        mock_fetcher_cls = MagicMock(return_value=mock_fetcher_instance)

        mocker.patch(
            "tradingview.options.options_contract_screener._get_fetcher_fns",
            return_value=(mock_fetcher_cls, MagicMock(), MagicMock(), MagicMock(), MagicMock()),
        )

        # ── Universe: one stock so the loop executes exactly once ─────────
        one_stock = [{"symbol": "NASDAQ:AAPL"}]
        mocker.patch(
            "tradingview.options.options_contract_screener._get_universe_fns",
            return_value=(
                lambda _path: one_stock,             # load_watchlist
                MagicMock(return_value=one_stock),   # build_watchlist
            ),
        )

        # ── scan_symbol always raises ValueError ──────────────────────────
        mocker.patch(
            "tradingview.options.options_contract_screener.scan_symbol",
            side_effect=ValueError("intentional test error"),
        )

        # ── Prevent real filesystem side-effects ──────────────────────────
        mocker.patch("tradingview.options.options_contract_screener._archive_report")
        mocker.patch("tradingview.options.options_contract_screener._save_to_db")
        mocker.patch("time.sleep")

        out_html = tmp_path / "test_scan_error.html"

        # Must not raise — the ValueError is caught inside run_scan()
        result = run_scan(n=1, out_path=out_html)

        # Even when every symbol errors, run_scan returns a list (empty) and
        # still writes the HTML report so the user sees a result page.
        assert isinstance(result, list)
        assert out_html.exists(), "HTML report must be written even when all symbols error"
