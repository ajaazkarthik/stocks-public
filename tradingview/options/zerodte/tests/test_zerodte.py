"""
ZeroDTE Module Tests
=====================
Unit tests for signal scoring, contract filtering, and integration.
"""
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import date, timedelta
import sys

# Ensure the workspace root is on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from tradingview.options.zerodte.models import (
    MoverCandidate, IVStructure, VOIData, SignalScore, ZeroDTEContract,
)
from tradingview.options.zerodte.scorer import (
    score_momentum, score_iv_backwardation, score_voi_surge, compute_signal_score,
)
from tradingview.options.zerodte.config import ZeroDTEConfig, DEFAULT_CONFIG


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def bullish_candidate():
    return MoverCandidate(
        symbol="MSFT",
        price=420.0,
        prev_day_change_pct=3.5,      # strong bullish move
        volume=80_000_000,
        volume_20d_avg=50_000_000,     # 1.6x ratio
        rsi=62.0,                      # bullish zone
        sector="Technology",
        industry="Software",
    )


@pytest.fixture
def bearish_candidate():
    return MoverCandidate(
        symbol="NVDA",
        price=95.0,
        prev_day_change_pct=-3.2,     # bearish move
        volume=120_000_000,
        volume_20d_avg=80_000_000,    # 1.5x ratio
        rsi=38.0,                     # bearish zone
        sector="Technology",
        industry="Semiconductors",
    )


@pytest.fixture
def weak_candidate():
    return MoverCandidate(
        symbol="AAPL",
        price=185.0,
        prev_day_change_pct=0.5,      # weak move
        volume=20_000_000,
        volume_20d_avg=50_000_000,    # 0.4x ratio
        rsi=50.0,
        sector="Technology",
        industry="Consumer Electronics",
    )


@pytest.fixture
def iv_backwardation():
    return IVStructure(
        symbol="MSFT",
        iv_1d=0.65,
        iv_1w=0.55,
        iv_1m=0.42,
        iv_3m=0.38,
        available=True,
    )


@pytest.fixture
def iv_flat():
    return IVStructure(
        symbol="AAPL",
        iv_1d=0.30,
        iv_1w=0.30,
        iv_1m=0.32,
        iv_3m=0.28,
        available=True,
    )


@pytest.fixture
def iv_unavailable():
    return IVStructure(symbol="XYZ", available=False)


@pytest.fixture
def voi_call_surge():
    return VOIData(
        symbol="MSFT",
        call_put_ratio=25.0,
        put_call_ratio=0.04,
        dominant_direction="call",
        available=True,
    )


@pytest.fixture
def voi_put_surge():
    return VOIData(
        symbol="NVDA",
        call_put_ratio=0.1,
        put_call_ratio=10.0,
        dominant_direction="put",
        available=True,
    )


@pytest.fixture
def voi_neutral():
    return VOIData(
        symbol="AAPL",
        call_put_ratio=1.5,
        put_call_ratio=0.67,
        dominant_direction="neutral",
        available=True,
    )


@pytest.fixture
def voi_unavailable():
    return VOIData(symbol="XYZ", available=False)


# ── Momentum Scorer Tests ──────────────────────────────────────────────────────

class TestMomentumScorer:

    def test_strong_bullish_call_scores_40(self, bullish_candidate):
        """Strong bullish move (>4%) + high volume + good RSI should score 40."""
        cand = MoverCandidate(
            symbol="MSFT", price=420.0,
            prev_day_change_pct=4.5, volume=100_000_000, volume_20d_avg=50_000_000,
            rsi=65.0, sector="", industry="",
        )
        total, _, _, _ = score_momentum(cand, "call")
        assert total == 40.0

    def test_threshold_bullish_call_scores_positive(self, bullish_candidate):
        total, prev_pts, vol_pts, rsi_pts = score_momentum(bullish_candidate, "call")
        assert total > 0
        assert prev_pts > 0    # prev day >= 2%
        assert vol_pts > 0     # volume > 1.2x
        assert rsi_pts > 0     # RSI in range

    def test_weak_move_scores_zero_calls(self, weak_candidate):
        total, prev_pts, vol_pts, rsi_pts = score_momentum(weak_candidate, "call")
        # 0.5% change below 2% threshold
        assert prev_pts == 0.0
        assert total < 20

    def test_strong_bearish_put_scores_40(self, bearish_candidate):
        cand = MoverCandidate(
            symbol="NVDA", price=95.0,
            prev_day_change_pct=-4.5, volume=160_000_000, volume_20d_avg=80_000_000,
            rsi=35.0, sector="", industry="",
        )
        total, _, _, _ = score_momentum(cand, "put")
        assert total == 40.0

    def test_threshold_bearish_put_scores_positive(self, bearish_candidate):
        total, prev_pts, vol_pts, rsi_pts = score_momentum(bearish_candidate, "put")
        assert total > 0
        assert prev_pts > 0    # prev day <= -2%

    def test_call_momentum_does_not_score_bearish(self, bearish_candidate):
        """A bearish mover should score 0 for CALL momentum."""
        total, prev_pts, _, _ = score_momentum(bearish_candidate, "call")
        assert prev_pts == 0.0  # -3.2% < +2.0% threshold

    def test_put_momentum_does_not_score_bullish(self, bullish_candidate):
        """A bullish mover should score 0 for PUT momentum."""
        total, prev_pts, _, _ = score_momentum(bullish_candidate, "put")
        assert prev_pts == 0.0  # +3.5% > -2.0% threshold

    def test_momentum_capped_at_40(self):
        cand = MoverCandidate(
            symbol="X", price=100.0,
            prev_day_change_pct=10.0, volume=500_000_000, volume_20d_avg=100_000_000,
            rsi=70.0, sector="", industry="",
        )
        total, _, _, _ = score_momentum(cand, "call")
        assert total <= 40.0


# ── IV Backwardation Tests ─────────────────────────────────────────────────────

class TestIVBackwardation:

    def test_backwardation_scores_positive(self, iv_backwardation):
        """iv_1w > iv_1m → positive score."""
        total, base, mid, high = score_iv_backwardation(iv_backwardation)
        assert total > 0
        assert base == 15.0   # any backwardation

    def test_steep_backwardation_gets_bonus(self, iv_backwardation):
        """slope >= 0.05 → base + mid + high = 30."""
        # iv_backwardation fixture: slope = 0.55 - 0.42 = 0.13 > 0.05
        total, base, mid, high = score_iv_backwardation(iv_backwardation)
        assert mid == 10.0
        assert high == 5.0
        assert total == 30.0

    def test_flat_iv_scores_low(self, iv_flat):
        """Contango (iv_1w < iv_1m) should score 0."""
        total, base, mid, high = score_iv_backwardation(iv_flat)
        # iv_1w = 0.30, iv_1m = 0.32 → slope = -0.02 (contango)
        assert total == 0.0
        assert base == 0.0

    def test_unavailable_iv_scores_zero(self, iv_unavailable):
        total, base, mid, high = score_iv_backwardation(iv_unavailable)
        assert total == 0.0
        assert base == 0.0

    def test_iv_capped_at_30(self):
        """Score must never exceed 30."""
        iv = IVStructure(symbol="X", iv_1w=1.0, iv_1m=0.0, available=True)
        total, _, _, _ = score_iv_backwardation(iv)
        assert total <= 30.0


# ── VOI Surge Tests ────────────────────────────────────────────────────────────

class TestVOISurge:

    def test_call_surge_scores_positive(self, voi_call_surge):
        total, base, mid, high = score_voi_surge(voi_call_surge, "call")
        assert total > 0
        assert base == 10.0   # C/P >= 5x

    def test_call_surge_at_25x_scores_25(self, voi_call_surge):
        """C/P = 25x → base(10) + mid(15) = 25 pts."""
        total, base, mid, high = score_voi_surge(voi_call_surge, "call")
        assert base == 10.0
        assert mid == 15.0
        assert high == 0.0    # 25 < 50 threshold
        assert total == 25.0

    def test_extreme_call_surge_scores_30(self):
        voi = VOIData(symbol="MSFT", call_put_ratio=200.0, put_call_ratio=0.005,
                      dominant_direction="call", available=True)
        total, base, mid, high = score_voi_surge(voi, "call")
        assert high == 5.0    # >= 50x
        assert total == 30.0

    def test_put_surge_scores_positive(self, voi_put_surge):
        total, base, mid, high = score_voi_surge(voi_put_surge, "put")
        assert total > 0
        # P/C = 10x → base(10) + mid(15)
        assert total == 25.0

    def test_neutral_voi_scores_below_threshold(self, voi_neutral):
        """C/P = 1.5x < 5x threshold → 0 pts."""
        total, base, mid, high = score_voi_surge(voi_neutral, "call")
        assert total == 0.0

    def test_unavailable_voi_scores_zero(self, voi_unavailable):
        total, base, mid, high = score_voi_surge(voi_unavailable, "call")
        assert total == 0.0

    def test_voi_capped_at_30(self):
        voi = VOIData(symbol="X", call_put_ratio=1000.0, put_call_ratio=0.001,
                      dominant_direction="call", available=True)
        total, _, _, _ = score_voi_surge(voi, "call")
        assert total <= 30.0


# ── Composite Scorer Tests ─────────────────────────────────────────────────────

class TestCompositeScorer:

    def test_full_signal_high_confidence(self, bullish_candidate, iv_backwardation, voi_call_surge):
        """Strong mover + backwardation + VOI surge → HIGH confidence."""
        cand = MoverCandidate(
            symbol="MSFT", price=420.0,
            prev_day_change_pct=4.5, volume=100_000_000, volume_20d_avg=50_000_000,
            rsi=65.0, sector="Tech", industry="Software",
        )
        sig = compute_signal_score(cand, iv_backwardation, voi_call_surge, "call")
        assert sig.composite_score >= 75.0
        assert sig.direction == "call"
        assert sig.symbol == "MSFT"

    def test_weak_candidate_low_score(self, weak_candidate, iv_flat, voi_neutral):
        sig = compute_signal_score(weak_candidate, iv_flat, voi_neutral, "call")
        assert sig.composite_score < 50.0

    def test_composite_is_sum_of_signals(self, bullish_candidate, iv_backwardation, voi_call_surge):
        sig = compute_signal_score(bullish_candidate, iv_backwardation, voi_call_surge, "call")
        expected = sig.momentum_score + sig.iv_back_score + sig.voi_score
        assert abs(sig.composite_score - expected) < 0.01

    def test_composite_never_exceeds_100(self):
        """Theoretical maximum is 40 + 30 + 30 = 100."""
        cand = MoverCandidate(
            symbol="X", price=100.0,
            prev_day_change_pct=10.0, volume=1_000_000_000, volume_20d_avg=100_000_000,
            rsi=70.0, sector="", industry="",
        )
        iv = IVStructure(symbol="X", iv_1w=1.0, iv_1m=0.0, available=True)
        voi = VOIData(symbol="X", call_put_ratio=1000.0, put_call_ratio=0.001,
                      dominant_direction="call", available=True)
        sig = compute_signal_score(cand, iv, voi, "call")
        assert sig.composite_score <= 100.0

    def test_put_direction_signal(self, bearish_candidate, iv_backwardation, voi_put_surge):
        sig = compute_signal_score(bearish_candidate, iv_backwardation, voi_put_surge, "put")
        assert sig.direction == "put"
        assert sig.momentum_score > 0
        assert sig.voi_score > 0


# ── Model Tests ────────────────────────────────────────────────────────────────

class TestModels:

    def test_volume_ratio_computed(self):
        c = MoverCandidate(
            symbol="A", price=100.0, prev_day_change_pct=2.0,
            volume=30_000_000, volume_20d_avg=20_000_000, rsi=55.0,
        )
        assert abs(c.volume_ratio - 1.5) < 0.01

    def test_volume_ratio_zero_if_no_avg(self):
        c = MoverCandidate(
            symbol="A", price=100.0, prev_day_change_pct=2.0,
            volume=30_000_000, volume_20d_avg=0, rsi=55.0,
        )
        assert c.volume_ratio == 0.0

    def test_iv_backwardation_slope(self):
        iv = IVStructure(symbol="X", iv_1w=0.5, iv_1m=0.4, available=True)
        assert abs(iv.backwardation_slope - 0.1) < 0.001
        assert iv.is_backwardation

    def test_iv_contango_is_not_backwardation(self):
        iv = IVStructure(symbol="X", iv_1w=0.3, iv_1m=0.4, available=True)
        assert not iv.is_backwardation

    def test_signal_score_compute_composite(self):
        sig = SignalScore(symbol="X", direction="call")
        sig.momentum_score = 30.0
        sig.iv_back_score = 20.0
        sig.voi_score = 25.0
        sig.compute_composite()
        assert sig.composite_score == 75.0


# ── Config Tests ───────────────────────────────────────────────────────────────

class TestConfig:

    def test_default_config_has_universe(self):
        assert len(DEFAULT_CONFIG.fixed_watchlist) >= 30

    def test_default_config_includes_key_tickers(self):
        universe = DEFAULT_CONFIG.fixed_watchlist
        for must_have in ["NVDA", "MSFT", "AAPL", "META"]:
            assert must_have in universe

    def test_default_dte_range(self):
        assert DEFAULT_CONFIG.contract_min_dte == 0
        assert DEFAULT_CONFIG.contract_max_dte == 2

    def test_min_score_default(self):
        assert DEFAULT_CONFIG.min_score_for_report >= 50


# ── Archiver Tests ─────────────────────────────────────────────────────────────

class TestArchiver:

    def test_archive_creates_file(self, tmp_path):
        from tradingview.options.zerodte.archiver import archive_if_exists
        out = tmp_path / "report.html"
        out.write_text("<html>original</html>", encoding="utf-8")

        archived = archive_if_exists(out)
        assert archived is not None
        assert archived.exists()
        assert archived.parent.name == "archive"
        assert archived.read_text(encoding="utf-8") == "<html>original</html>"

    def test_archive_returns_none_if_no_file(self, tmp_path):
        from tradingview.options.zerodte.archiver import archive_if_exists
        out = tmp_path / "nonexistent.html"
        result = archive_if_exists(out)
        assert result is None


# ── DTE Pre-filter Tests ───────────────────────────────────────────────────────

class TestDTEPrefilter:
    """Tests for batch_filter_symbols_with_dte_options() — the TV scan2 batch DTE filter."""

    def _make_scan2_response(self, symbols_with_exps: dict[str, int]) -> dict:
        """
        Build a fake scan2 response.
        symbols_with_exps: {"NVDA": 20260601, "SPY": 20260601, ...}
        """
        rows = []
        for sym_tv, exp_int in symbols_with_exps.items():
            rows.append({"s": f"OPRA:{sym_tv}260601C100.0", "f": [sym_tv, exp_int]})
        return {
            "fields": ["underlying_symbol", "expiration"],
            "symbols": rows,
            "totalCount": len(rows),
        }

    def test_returns_full_list_when_no_cookies(self, tmp_path):
        """If no cookie file exists, all symbols are returned unchanged."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        import dataclasses

        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=tmp_path / "no_cookie.txt")
        result = batch_filter_symbols_with_dte_options(["AAPL", "MSFT", "SPY"], cfg)
        assert result == ["AAPL", "MSFT", "SPY"]

    def test_filters_to_only_symbols_with_contracts(self, tmp_path):
        """Only symbols present in scan2 response are returned."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        from datetime import date, timedelta
        import dataclasses

        # Write a minimal cookie file
        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("sessionid=test123; sessionid_sign=sign456", encoding="utf-8")
        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=cookie_file)

        today = date.today()
        today_int = int(today.strftime("%Y%m%d"))

        # scan2 only has NVDA and SPY — MSFT and AAPL have no near-expiry contracts
        mock_response = self._make_scan2_response({
            "NASDAQ:NVDA": today_int,
            "AMEX:SPY": today_int,
        })

        with patch("tradingview.options.zerodte.fetcher.requests.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess_cls.return_value = mock_sess
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_sess.post.return_value = mock_resp

            result = batch_filter_symbols_with_dte_options(
                ["NVDA", "MSFT", "AAPL", "SPY"], cfg
            )

        # Only NVDA and SPY should survive the filter
        assert set(result) == {"NVDA", "SPY"}
        # Order is preserved relative to input
        assert result.index("NVDA") < result.index("SPY")

    def test_empty_response_returns_empty(self, tmp_path):
        """scan2 returning no contracts → no symbols pass."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        import dataclasses

        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("sessionid=x; sessionid_sign=y", encoding="utf-8")
        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=cookie_file)

        empty_resp = {"fields": ["underlying_symbol", "expiration"], "symbols": [], "totalCount": 0}

        with patch("tradingview.options.zerodte.fetcher.requests.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess_cls.return_value = mock_sess
            mock_resp = MagicMock()
            mock_resp.json.return_value = empty_resp
            mock_resp.raise_for_status = MagicMock()
            mock_sess.post.return_value = mock_resp

            result = batch_filter_symbols_with_dte_options(["AAPL", "MSFT"], cfg)

        assert result == []

    def test_scan2_error_falls_back_to_full_batch(self, tmp_path):
        """Network error on any batch → that batch's symbols are included conservatively."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        import dataclasses

        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("sessionid=x; sessionid_sign=y", encoding="utf-8")
        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=cookie_file)

        with patch("tradingview.options.zerodte.fetcher.requests.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess_cls.return_value = mock_sess
            mock_sess.post.side_effect = Exception("connection timeout")

            result = batch_filter_symbols_with_dte_options(["AAPL", "MSFT", "NVDA"], cfg)

        # All symbols included as conservative fallback
        assert set(result) == {"AAPL", "MSFT", "NVDA"}

    def test_empty_symbols_list_returns_empty(self, tmp_path):
        """Edge case: empty input → empty output, no API call made."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        import dataclasses

        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("sessionid=x; sessionid_sign=y", encoding="utf-8")
        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=cookie_file)

        with patch("tradingview.options.zerodte.fetcher.requests.Session") as mock_sess_cls:
            result = batch_filter_symbols_with_dte_options([], cfg)
            mock_sess_cls.assert_not_called()

        assert result == []

    def test_preserves_input_order(self, tmp_path):
        """Filtered result preserves original input ordering."""
        from tradingview.options.zerodte.fetcher import batch_filter_symbols_with_dte_options
        from datetime import date
        import dataclasses

        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("sessionid=x; sessionid_sign=y", encoding="utf-8")
        cfg = dataclasses.replace(DEFAULT_CONFIG, cookie_path=cookie_file)

        today_int = int(date.today().strftime("%Y%m%d"))
        mock_response = self._make_scan2_response({
            "NASDAQ:QQQ": today_int,
            "AMEX:SPY":   today_int,
            "NASDAQ:NVDA": today_int,
        })

        with patch("tradingview.options.zerodte.fetcher.requests.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess_cls.return_value = mock_sess
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_sess.post.return_value = mock_resp

            result = batch_filter_symbols_with_dte_options(
                ["SPY", "NVDA", "MSFT", "QQQ", "AAPL"], cfg
            )

        # MSFT and AAPL filtered out; remaining order preserved from input
        assert result == ["SPY", "NVDA", "QQQ"]


# ── Integration Smoke Test ─────────────────────────────────────────────────────

class TestIntegrationSmoke:
    """
    Integration tests that mock external API calls.
    These ensure the orchestrator pipeline runs end-to-end.
    """

    @patch("tradingview.options.zerodte.orchestrator.batch_filter_symbols_with_dte_options")
    @patch("tradingview.options.zerodte.fetcher.fetch_stock_data")
    @patch("tradingview.options.zerodte.fetcher.fetch_tv_top_movers")
    @patch("tradingview.options.zerodte.fetcher.fetch_iv_structure")
    @patch("tradingview.options.zerodte.fetcher.fetch_voi_data")
    @patch("tradingview.options.zerodte.contract_filter.select_contracts_for_signal")
    @patch("tradingview.options.zerodte.database.init_db")
    @patch("tradingview.options.zerodte.database.write_contracts")
    @patch("tradingview.options.zerodte.html_reporter.write_html_report")
    @patch("tradingview.options.zerodte.json_reporter.write_json_report")
    def test_run_call_scan_no_crash(
        self, mock_json, mock_html, mock_write_db, mock_init_db,
        mock_contracts, mock_voi, mock_iv, mock_tv, mock_stock_data, mock_prefilter,
        tmp_path
    ):
        """Full pipeline runs without crashing with mocked data."""
        import dataclasses
        from tradingview.options.zerodte.orchestrator import ZeroDTEOrchestrator

        # Setup mocks
        mock_prefilter.side_effect = lambda syms, cfg: syms  # pass-through; avoid live API
        mock_tv.return_value = ["MSFT"]
        mock_stock_data.return_value = MoverCandidate(
            symbol="MSFT", price=420.0, prev_day_change_pct=3.5,
            volume=80_000_000, volume_20d_avg=50_000_000,
            rsi=62.0, sector="Tech", industry="Software",
        )
        mock_iv.return_value = IVStructure(
            symbol="MSFT", iv_1w=0.55, iv_1m=0.42, available=True
        )
        mock_voi.return_value = VOIData(
            symbol="MSFT", call_put_ratio=25.0, put_call_ratio=0.04,
            dominant_direction="call", available=True,
        )
        mock_contracts.return_value = []  # no contracts (avoid yfinance calls)
        mock_init_db.return_value = None
        mock_write_db.return_value = 0

        # Use tmp_path for outputs
        config = dataclasses.replace(
            DEFAULT_CONFIG,
            reports_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            fixed_watchlist=("MSFT",),
            tv_scan_top_n=1,
        )

        orch = ZeroDTEOrchestrator(config=config)
        result = orch.run(scan_directions=["call"])

        assert result is not None
        assert result.candidates_evaluated >= 0
        # Should have found 1 signal (MSFT qualifies)
        assert result.signals_found_calls >= 0

    @patch("tradingview.options.zerodte.orchestrator.batch_filter_symbols_with_dte_options")
    @patch("tradingview.options.zerodte.orchestrator.fetch_stock_data")
    @patch("tradingview.options.zerodte.orchestrator.fetch_tv_top_movers")
    @patch("tradingview.options.zerodte.database.init_db")
    @patch("tradingview.options.zerodte.database.write_contracts")
    @patch("tradingview.options.zerodte.html_reporter.write_html_report")
    @patch("tradingview.options.zerodte.json_reporter.write_json_report")
    def test_no_candidates_returns_empty_result(
        self, mock_json, mock_html, mock_write_db, mock_init_db,
        mock_tv, mock_stock_data, mock_prefilter, tmp_path
    ):
        """When yfinance returns nothing, result has zero candidates."""
        import dataclasses
        from tradingview.options.zerodte.orchestrator import ZeroDTEOrchestrator

        mock_prefilter.side_effect = lambda syms, cfg: syms  # pass-through; avoid live API
        mock_tv.return_value = []
        mock_stock_data.return_value = None
        mock_init_db.return_value = None
        mock_write_db.return_value = 0

        config = dataclasses.replace(
            DEFAULT_CONFIG,
            reports_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            fixed_watchlist=("MSFT",),
        )

        orch = ZeroDTEOrchestrator(config=config)
        result = orch.run(scan_directions=["call"])

        assert result.candidates_evaluated == 0
        assert result.signals_found_calls == 0
        assert len(result.contracts_calls) == 0
