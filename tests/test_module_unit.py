"""Unit tests for module logic extracted from test_html_reports.py."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestSectorHeatmap:
    """Tests for the TV sector heatmap (plotly.js treemap)."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def _sample_data(self):
        return [
            {"symbol": "AAPL", "name": "Apple Inc", "sector": "Technology",
             "industry": "Consumer Electronics", "market_cap": 3e12, "change": 1.5,
             "rsi": 55, "close": 190.0, "recommend": 0.4},
            {"symbol": "MSFT", "name": "Microsoft Corp", "sector": "Technology",
             "industry": "Software", "market_cap": 2.8e12, "change": -0.3,
             "rsi": 48, "close": 410.0, "recommend": 0.2},
            {"symbol": "JPM", "name": "JPMorgan", "sector": "Financial Services",
             "industry": "Banks", "market_cap": 500e9, "change": 2.1,
             "rsi": 60, "close": 200.0, "recommend": 0.5},
            {"symbol": "XOM", "name": "Exxon Mobil", "sector": "Energy",
             "industry": "Oil & Gas", "market_cap": 400e9, "change": -1.8,
             "rsi": 42, "close": 110.0, "recommend": -0.3},
        ]

    def test_build_html_contains_plotly(self):
        from analysis.tv_sector_heatmap import build_html
        html = build_html(self._sample_data(), "america")
        assert "plotly" in html.lower()
        assert "treemap" in html.lower()
        assert "Technology" in html
        assert "Financial Services" in html

    def test_build_html_empty_data(self):
        from analysis.tv_sector_heatmap import build_html
        html = build_html([], "america")
        assert "No data available" in html

    def test_sector_summary_weighted_average(self):
        from analysis.tv_sector_heatmap import _sector_summary
        data = self._sample_data()
        summary = _sector_summary(data)
        # Tech sector: AAPL(3T, +1.5%) + MSFT(2.8T, -0.3%)
        # weighted avg = (3e12*1.5 + 2.8e12*(-0.3)) / (3e12+2.8e12)
        tech = next(s for s in summary if s["sector"] == "Technology")
        expected = (3e12 * 1.5 + 2.8e12 * (-0.3)) / (3e12 + 2.8e12)
        assert abs(tech["avg_change"] - round(expected, 2)) < 0.01
        assert tech["count"] == 2

    def test_sector_summary_single_stock_sector(self):
        from analysis.tv_sector_heatmap import _sector_summary
        data = self._sample_data()
        summary = _sector_summary(data)
        energy = next(s for s in summary if s["sector"] == "Energy")
        assert energy["avg_change"] == -1.8
        assert energy["count"] == 1

    def test_build_html_has_color_scale(self):
        """Treemap should have red-to-green colour scale for % change."""
        from analysis.tv_sector_heatmap import build_html
        html = build_html(self._sample_data(), "america")
        assert "colorscale" in html
        assert "#dc2626" in html  # red
        assert "#16a34a" in html  # green


class TestFOChainScreener:
    """Tests for F&O Chain Screener S1-S5 scoring."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def _sample_contract(self, **overrides):
        c = {
            "symbol": "AAPL", "underlying": "AAPL", "underlying_price": 190.0,
            "option_type": "call", "strike": 195.0, "expiry": "2026-07-18",
            "dte": 30, "bid": 3.50, "ask": 3.80, "last": 3.65,
            "volume": 500, "open_interest": 1200, "iv": 0.35,
            "delta": 0.42, "theta": -0.08, "market": "us",
            "broker_note": "IBKR (full options)",
        }
        c.update(overrides)
        return c

    def test_score_contract_returns_all_fields(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract()
        scored = score_contract(c)
        for field in ["total_score", "s1_iv_rank", "s2_direction",
                      "s3_liquidity", "s4_time", "s5_greeks"]:
            assert field in scored

    def test_score_range_0_100(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract()
        scored = score_contract(c)
        assert 0 <= scored["total_score"] <= 100

    def test_s1_high_iv_scores_higher(self):
        from options.fo_chain_screener import score_contract
        c_high = self._sample_contract(iv=0.55)
        c_low = self._sample_contract(iv=0.10)
        score_contract(c_high)
        score_contract(c_low)
        assert c_high["s1_iv_rank"] > c_low["s1_iv_rank"]

    def test_s2_aligned_call_bullish(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(option_type="call")
        scored = score_contract(c, underlying_score=75)
        assert scored["s2_direction"] == 25.0

    def test_s2_aligned_put_bearish(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(option_type="put")
        scored = score_contract(c, underlying_score=25)
        assert scored["s2_direction"] == 25.0

    def test_s3_high_oi_tight_spread(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(open_interest=600, bid=3.50, ask=3.55)
        score_contract(c)
        assert c["s3_liquidity"] >= 17.0  # OI>500=10 + spread<2%=10

    def test_s3_low_oi_scores_lower(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(open_interest=5, bid=3.00, ask=4.00)
        score_contract(c)
        assert c["s3_liquidity"] < 10

    def test_s4_optimal_dte(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(dte=30)
        score_contract(c)
        assert c["s4_time"] == 15.0

    def test_s4_short_dte(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(dte=3)
        score_contract(c)
        assert c["s4_time"] == 5.0

    def test_s5_delta_zone(self):
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(delta=0.40, theta=-0.05, bid=3.50, ask=3.80)
        score_contract(c)
        assert c["s5_greeks"] >= 5.0

    def test_build_html_has_table(self):
        from options.fo_chain_screener import build_html, score_contract
        c = self._sample_contract()
        score_contract(c)
        c["total_score"] = 75
        html = build_html([c], min_score=70)
        assert "<table" in html
        assert "AAPL" in html
        assert "F&O Chain Screener" in html

    def test_build_html_empty_below_threshold(self):
        from options.fo_chain_screener import build_html, score_contract
        c = self._sample_contract()
        score_contract(c)
        c["total_score"] = 50
        html = build_html([c], min_score=90)
        assert "No contracts scored above 90" in html

    # ── Group 1: score_contract / S1 IV floor ──────────────────────────────

    def test_iv_zero_gives_s1_neutral_floor(self):
        """iv=0 must produce s1=3.0 (the neutral sentinel), not 0.0 or 4.0."""
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(iv=0)
        score_contract(c)
        assert c["s1_iv_rank"] == 3.0

    def test_iv_zero_prints_api_failure_warning(self, capsys):
        """iv=0 must print a warning containing 'possible API/auth failure'."""
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(iv=0, symbol="NIFTY", option_type="call", strike=22000)
        score_contract(c)
        captured = capsys.readouterr()
        assert "possible API/auth failure" in captured.out

    def test_iv_very_low_gives_s1_4(self):
        """iv=0.05 (real but tiny) must score s1=4.0 — the low-IV live-data floor."""
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(iv=0.05)
        score_contract(c)
        assert c["s1_iv_rank"] == 4.0

    def test_iv_moderate_gives_s1_12(self):
        """iv=0.25 (0.20 < iv <= 0.30 tier) must produce s1=12.0."""
        from options.fo_chain_screener import score_contract
        c = self._sample_contract(iv=0.25)
        score_contract(c)
        assert c["s1_iv_rank"] == 12.0

    def test_real_iv_always_beats_neutral_floor(self):
        """KEY REGRESSION: any real IV (>0) must always score higher S1 than iv=0.
        4.0 (real, very low) > 3.0 (neutral sentinel) must always hold."""
        from options.fo_chain_screener import score_contract
        c_real = self._sample_contract(iv=0.05)
        c_zero = self._sample_contract(iv=0)
        score_contract(c_real)
        score_contract(c_zero)
        assert c_real["s1_iv_rank"] > c_zero["s1_iv_rank"], (
            f"Real-IV floor {c_real['s1_iv_rank']} must exceed neutral floor "
            f"{c_zero['s1_iv_rank']} — regression in S1 neutral/floor logic"
        )

    # ── Group 2: build_html / default min_score ────────────────────────────

    def test_build_html_default_min_score_is_55(self):
        """build_html() with no min_score arg must use 55 as default (not 70).
        A contract scored at 60 should appear in the output."""
        from options.fo_chain_screener import build_html
        c = self._sample_contract()
        c["total_score"] = 60  # above 55, below old default 70
        # populate required sub-score keys (build_html reads them)
        for k in ["s1_iv_rank", "s2_direction", "s3_liquidity", "s4_time", "s5_greeks"]:
            c[k] = 0.0
        html = build_html([c])  # no min_score → default 55
        assert "AAPL" in html, "Contract at score 60 must appear with default threshold 55"
        assert "No contracts scored above" not in html

    def test_build_html_old_threshold_70_excludes_score_60(self):
        """Regression: explicit min_score=70 must exclude a contract at score 60.
        Proves the threshold change from 70→55 is meaningful."""
        from options.fo_chain_screener import build_html
        c = self._sample_contract()
        c["total_score"] = 60
        for k in ["s1_iv_rank", "s2_direction", "s3_liquidity", "s4_time", "s5_greeks"]:
            c[k] = 0.0
        html_new = build_html([c])          # default 55 → included
        html_old = build_html([c], min_score=70)  # old default → excluded
        assert "AAPL" in html_new
        assert "No contracts scored above 70" in html_old

    # ── Group 3: _nse_fetch_chain / CookieJar + error handling ────────────

    def test_nse_fetch_chain_http_error_returns_empty_list(self, mocker):
        """HTTPError from NSE API must return [] without raising."""
        import urllib.error
        from options.fo_chain_screener import _nse_fetch_chain

        mock_opener = mocker.MagicMock()
        mock_opener.open.side_effect = urllib.error.HTTPError(
            url="https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            code=403, msg="Forbidden", hdrs=None, fp=None,
        )
        mocker.patch("urllib.request.build_opener", return_value=mock_opener)

        result = _nse_fetch_chain("NIFTY", True)
        assert result == []

    def test_nse_fetch_chain_uses_http_cookie_processor(self, mocker):
        """build_opener must be called with an HTTPCookieProcessor — CookieJar wiring."""
        import urllib.request
        from options.fo_chain_screener import _nse_fetch_chain

        captured_handlers = []
        mock_resp = mocker.MagicMock()
        mock_resp.read.return_value = b'{"records": {"data": [], "underlyingValue": 0}}'
        mock_opener = mocker.MagicMock()
        mock_opener.open.return_value = mock_resp

        def _fake_build_opener(*handlers):
            captured_handlers.extend(handlers)
            return mock_opener

        mocker.patch("urllib.request.build_opener", side_effect=_fake_build_opener)

        _nse_fetch_chain("NIFTY", True)

        assert any(
            isinstance(h, urllib.request.HTTPCookieProcessor)
            for h in captured_handlers
        ), "build_opener must receive an HTTPCookieProcessor for NSE cookie handling"

    # ── Group 4: split try block / report_db isolation ────────────────────

    def test_report_db_import_failure_is_graceful(self, mocker, tmp_path):
        """When report_db cannot be imported, main() must complete without exception
        and the HTML output file must still be written."""
        import argparse, sys
        from options import fo_chain_screener

        # Block report_db import by injecting a broken module sentinel
        mocker.patch.dict(sys.modules, {"report_db": None})

        mocker.patch.object(fo_chain_screener, "scan_us", return_value=[])
        mocker.patch.object(fo_chain_screener, "scan_india", return_value=[])

        out_path = tmp_path / "fo_report.html"
        args = argparse.Namespace(
            market="us", min_score=90, n_top=1, n_expiries=1,
            symbols="", out=str(out_path),
        )
        fo_chain_screener.main(args)  # must not raise
        assert out_path.exists(), "HTML file must be written even when report_db is missing"

    def test_abw_failure_does_not_skip_save_snapshot(self, mocker, tmp_path):
        """archive_before_write raising must NOT prevent save_fo_chain_snapshot from being called.
        This proves the split try-block pattern works: Block 2 exception does not affect Block 1 vars."""
        import argparse, sys
        from options import fo_chain_screener

        save_fn = mocker.MagicMock()
        mock_report_db = mocker.MagicMock()
        mock_report_db.archive_before_write = mocker.MagicMock(side_effect=RuntimeError("archive boom"))
        mock_report_db.save_fo_chain_snapshot = save_fn
        mock_report_db.archive_report = mocker.MagicMock()

        mocker.patch.dict(sys.modules, {"report_db": mock_report_db})

        mocker.patch.object(fo_chain_screener, "scan_us", return_value=[])
        mocker.patch.object(fo_chain_screener, "scan_india", return_value=[])

        out_path = tmp_path / "fo_report.html"
        args = argparse.Namespace(
            market="us", min_score=90, n_top=1, n_expiries=1,
            symbols="", out=str(out_path),
        )
        fo_chain_screener.main(args)

        save_fn.assert_called_once(), (
            "save_fo_chain_snapshot must be called even when archive_before_write raises"
        )


class TestEconomicCalendar:
    """Tests for the TradingView Economic Calendar module."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def _sample_events(self):
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        return [
            {"date": today, "time": "14:00 UTC", "title": "Fed Interest Rate Decision",
             "country": "US", "currency": "USD", "impact": 3, "impact_label": "High",
             "forecast": "5.25%", "previous": "5.25%", "actual": None,
             "days_away": 0, "is_central_bank": True, "is_high_volatility": False},
            {"date": tomorrow, "time": "08:30 UTC", "title": "Non-Farm Payrolls",
             "country": "US", "currency": "USD", "impact": 3, "impact_label": "High",
             "forecast": "180K", "previous": "175K", "actual": None,
             "days_away": 1, "is_central_bank": False, "is_high_volatility": True},
            {"date": tomorrow, "time": "10:00 UTC", "title": "ECB Rate Decision",
             "country": "EU", "currency": "EUR", "impact": 3, "impact_label": "High",
             "forecast": "4.25%", "previous": "4.25%", "actual": None,
             "days_away": 1, "is_central_bank": True, "is_high_volatility": False},
        ]

    def test_build_calendar_html_has_events(self):
        from tv_data.tv_economic_calendar import build_calendar_html_fragment
        events = self._sample_events()
        html = build_calendar_html_fragment(events)
        assert "Economic Calendar" in html
        assert "Fed Interest Rate Decision" in html
        assert "Non-Farm Payrolls" in html

    def test_build_calendar_html_empty(self):
        from tv_data.tv_economic_calendar import build_calendar_html_fragment
        html = build_calendar_html_fragment([])
        assert html == ""

    def test_build_calendar_html_low_impact_filtered(self):
        from tv_data.tv_economic_calendar import build_calendar_html_fragment
        # Events with impact < 3 should not appear
        events = [{"date": "2026-01-01", "time": "10:00 UTC", "title": "Minor Event",
                   "country": "US", "currency": "USD", "impact": 1, "impact_label": "Low",
                   "forecast": None, "previous": None, "actual": None,
                   "days_away": 2, "is_central_bank": False, "is_high_volatility": False}]
        html = build_calendar_html_fragment(events)
        assert html == ""  # no high-impact events

    def test_get_upcoming_warnings_format(self):
        from tv_data.tv_economic_calendar import get_upcoming_warnings
        # Mock fetch_economic_events to return our sample data
        import tv_data.tv_economic_calendar as ecal
        original = ecal.fetch_economic_events
        ecal.fetch_economic_events = lambda **kw: self._sample_events()
        try:
            warnings = get_upcoming_warnings(days=7)
            assert len(warnings) >= 2  # Fed + NFP + ECB
            assert any("Fed" in w for w in warnings)
            assert any("TODAY" in w or "TOMORROW" in w for w in warnings)
        finally:
            ecal.fetch_economic_events = original

    def test_parse_date_formats(self):
        from tv_data.tv_economic_calendar import _parse_date
        # ISO format
        dt = _parse_date("2026-05-30T14:00:00.000Z")
        assert dt is not None
        assert dt.year == 2026
        # Unix timestamp
        dt2 = _parse_date(1748620800)
        assert dt2 is not None
        # None input
        assert _parse_date(None) is None
        assert _parse_date("") is None


class TestForexSignalEngine:
    """Tests for the Forex Signal Engine L1-L4 scoring."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def _sample_tv_data(self, **overrides):
        d = {
            "name": "FX:EURUSD", "close": 1.08500, "change": 0.15,
            "rsi": 52.0, "macd_macd": 0.0012, "macd_signal": 0.0008,
            "sma50": 1.08000, "sma200": 1.07500, "adx": 28.0,
            "atr": 0.0065, "volume": 150000,
            "recommend_all": 0.3, "recommend_ma": 0.2, "recommend_other": 0.4,
        }
        d.update(overrides)
        return d

    def test_score_pair_returns_all_fields(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        result = engine.score_pair("FX:EURUSD", self._sample_tv_data())
        required = {"symbol", "pair", "base", "quote", "close", "change",
                    "score", "direction", "recommendation",
                    "l1_score", "l2_score", "l3_score", "l4_score",
                    "l1_detail", "l2_detail", "l3_detail", "l4_detail",
                    "broker_note", "atr"}
        assert required.issubset(set(result.keys()))

    def test_score_range_0_100(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        result = engine.score_pair("FX:EURUSD", self._sample_tv_data())
        assert 0 <= result["score"] <= 100

    def test_buy_recommendation_above_65(self):
        from forex.forex_signal_engine import ForexSignalEngine
        # High carry + strong technicals → high score
        engine = ForexSignalEngine(cb_rates={"AUD": 12.0, "JPY": 0.1})
        data = self._sample_tv_data(
            rsi=42.0, macd_macd=0.05, macd_signal=0.01,
            adx=35.0, close=95.0, sma50=90.0, sma200=85.0,
            recommend_all=0.8, recommend_ma=0.7, recommend_other=0.9,
        )
        result = engine.score_pair("FX:AUDJPY", data)
        assert result["recommendation"] == "BUY"
        assert "BUY AUD" in result["direction"]

    def test_sell_recommendation_below_35(self):
        from forex.forex_signal_engine import ForexSignalEngine
        # Negative carry + weak technicals → low score
        engine = ForexSignalEngine(cb_rates={"JPY": 0.1, "AUD": 12.0})
        data = self._sample_tv_data(
            rsi=78.0, macd_macd=-0.03, macd_signal=0.01,
            adx=10.0, close=0.010, sma50=0.012, sma200=0.013,
            recommend_all=-0.7, recommend_ma=-0.6, recommend_other=-0.8,
        )
        result = engine.score_pair("FX:JPYAUD", data)
        assert result["recommendation"] == "SELL"

    def test_hold_recommendation_neutral(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        data = self._sample_tv_data(
            recommend_all=0.0, recommend_ma=0.0, recommend_other=0.0,
        )
        result = engine.score_pair("FX:EURUSD", data)
        assert result["recommendation"] == "HOLD"

    def test_carry_trade_positive_differential(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine(cb_rates={"NZD": 5.5, "JPY": 0.25})
        data = self._sample_tv_data()
        result = engine.score_pair("FX:NZDJPY", data)
        assert result["l2_detail"]["carry_diff"] > 0
        assert result["l2_detail"]["carry_pts"] > 7.5  # positive carry → above midpoint

    def test_carry_trade_negative_differential(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine(cb_rates={"JPY": 0.25, "NZD": 5.5})
        data = self._sample_tv_data()
        result = engine.score_pair("FX:JPYNZD", data)
        assert result["l2_detail"]["carry_diff"] < 0
        assert result["l2_detail"]["carry_pts"] < 7.5  # negative carry → below midpoint

    def test_parse_currencies(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        assert engine._parse_currencies("FX:EURUSD") == ("EUR", "USD")
        assert engine._parse_currencies("FX:USDJPY") == ("USD", "JPY")
        assert engine._parse_currencies("GBPAUD") == ("GBP", "AUD")

    def test_broker_note_inr(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        result = engine.score_pair("FX:USDINR", self._sample_tv_data())
        assert "Zerodha" in result["broker_note"] or "AngelOne" in result["broker_note"]

    def test_broker_note_sgd(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        result = engine.score_pair("FX:USDSGD", self._sample_tv_data())
        assert "Moomoo-SG" in result["broker_note"]

    def test_build_html_contains_table(self):
        from forex.forex_signal_engine import build_html
        results = [{
            "pair": "EUR/USD", "close": 1.085, "change": 0.15,
            "score": 62.5, "recommendation": "HOLD", "direction": "NEUTRAL",
            "l1_score": 18.0, "l2_score": 15.0, "l3_score": 17.5, "l4_score": 12.0,
            "broker_note": "IBKR (full forex)", "atr": 0.0065,
        }]
        html = build_html(results)
        assert "EUR/USD" in html
        assert "Forex Signal Report" in html
        assert "<table" in html
        assert "HOLD" in html

    def test_build_html_empty(self):
        from forex.forex_signal_engine import build_html
        html = build_html([])
        assert "No forex data" in html

    def test_l1_rsi_zones(self):
        from forex.forex_signal_engine import ForexSignalEngine
        engine = ForexSignalEngine()
        # Oversold bounce (30-45) should score higher RSI pts than overbought (>70)
        data_oversold = self._sample_tv_data(rsi=35.0)
        data_overbought = self._sample_tv_data(rsi=75.0)
        r1 = engine.score_pair("FX:EURUSD", data_oversold)
        r2 = engine.score_pair("FX:EURUSD", data_overbought)
        assert r1["l1_detail"]["rsi_pts"] > r2["l1_detail"]["rsi_pts"]


class TestLivePriceFetch:
    """Unit tests for fetch_live_prices(), tv_symbol_to_yf(), and live injection."""

    @pytest.fixture(autouse=True)
    def _import(self):
        import sys
        sys.path.insert(0, str(ROOT / "tradingview"))
        import backtest_vectorbt as bv
        self.bv = bv

    # ── Symbol conversion ──────────────────────────────────────────────────────

    def test_tv_symbol_to_yf_us_exchanges(self):
        """US exchange symbols map to bare tickers (no suffix)."""
        tv_to_yf = self.bv.tv_symbol_to_yf
        assert tv_to_yf("NYSE:DELL")   == "DELL"
        assert tv_to_yf("NASDAQ:CSCO") == "CSCO"
        assert tv_to_yf("AMEX:SPY")    == "SPY"
        assert tv_to_yf("OTC:INVMF")   == "INVMF"

    def test_tv_symbol_to_yf_asx_suffix(self):
        """ASX symbols must get .AX suffix."""
        assert self.bv.tv_symbol_to_yf("ASX:CBA")  == "CBA.AX"
        assert self.bv.tv_symbol_to_yf("ASX:BHP")  == "BHP.AX"

    def test_tv_symbol_to_yf_lse_suffix(self):
        """LSE symbols must get .L suffix."""
        assert self.bv.tv_symbol_to_yf("LSE:HSBA") == "HSBA.L"

    def test_tv_symbol_to_yf_nse_returns_none(self):
        """NSE exchange is not supported by yfinance — must return None."""
        assert self.bv.tv_symbol_to_yf("NSE:INDIAGLYCO") is None
        assert self.bv.tv_symbol_to_yf("BSE:500325")       is None

    def test_tv_symbol_to_yf_futures_returns_none(self):
        """Futures exchanges (CME, CBOT) are unsupported."""
        assert self.bv.tv_symbol_to_yf("CME:ES1!")  is None
        assert self.bv.tv_symbol_to_yf("CBOT:ZC1!") is None

    def test_tv_symbol_to_yf_no_colon(self):
        """Bare symbol without exchange prefix passes through unchanged."""
        assert self.bv.tv_symbol_to_yf("AAPL") == "AAPL"

    def test_tv_symbol_to_yf_dash_to_dot(self):
        """TV dash separator in tickers becomes dot for yfinance (BRK-B → BRK.B)."""
        assert self.bv.tv_symbol_to_yf("NYSE:BRK-B") == "BRK.B"

    # ── fetch_live_prices: yfinance path (mocked) ──────────────────────────────

    def test_fetch_live_prices_yfinance_single(self, monkeypatch):
        """fetch_live_prices returns the yfinance close for a US symbol."""
        import types, pandas as pd

        # Build a mock yfinance module
        mock_yf = types.ModuleType("yfinance")

        class _MockTicker:
            def __init__(self, symbol):
                self._sym = symbol

            def history(self, **kwargs):
                return pd.DataFrame({"Close": [190.0, 212.36]})

        mock_yf.Ticker = _MockTicker

        # Inject into sys.modules so `import yfinance as yf` inside the function picks it up
        import sys
        monkeypatch.setitem(sys.modules, "yfinance", mock_yf)

        result = self.bv.fetch_live_prices(["NYSE:DELL"])
        assert result["NYSE:DELL"] is not None
        assert abs(result["NYSE:DELL"] - 212.36) < 0.01, (
            f"Expected 212.36, got {result['NYSE:DELL']}"
        )

    def test_fetch_live_prices_yfinance_batch(self, monkeypatch):
        """
        fetch_live_prices resolves multiple US symbols via yfinance batch download.

        Specifically tests the per-column last-valid-observation path that fixes
        the NaN-last-row bug: when US markets haven't closed yet, the last row of
        a multi-ticker yfinance download has NaN for US tickers. The fix iterates
        per-column and uses .dropna().iloc[-1] so each ticker gets its own latest close.
        """
        import types, pandas as pd

        mock_yf = types.ModuleType("yfinance")

        def _mock_download(tickers, **kwargs):
            """
            Mimic yf.download: 5 rows, last row is NaN for all tickers
            (simulating in-progress trading day with no close yet).
            """
            import pandas as _pd
            dates      = _pd.date_range("2026-04-15", periods=5)
            close_data = {}
            for i, t in enumerate(tickers):
                # 4 valid closes, then NaN on "today"
                close_data[t] = [100.0 + i * 10.0] * 4 + [float("nan")]
            close_df = _pd.DataFrame(close_data, index=dates)

            class _FakeResult:
                empty = False
                def __getitem__(self, key):
                    if key == "Close":
                        return close_df
                    raise KeyError(key)

            return _FakeResult()

        mock_yf.download = _mock_download
        import sys
        monkeypatch.setitem(sys.modules, "yfinance", mock_yf)

        symbols = ["NYSE:DELL", "NASDAQ:CSCO", "ASX:CBA"]
        result  = self.bv.fetch_live_prices(symbols)

        # All three must resolve (using per-column last valid, not the NaN last row)
        assert result["NYSE:DELL"]   is not None, "DELL must resolve despite NaN last row"
        assert result["NASDAQ:CSCO"] is not None, "CSCO must resolve despite NaN last row"
        assert result["ASX:CBA"]     is not None, "CBA must resolve despite NaN last row"
        # Values should be from the 4th row (index 3)
        assert abs(result["NYSE:DELL"]   - 100.0) < 0.01
        assert abs(result["NASDAQ:CSCO"] - 110.0) < 0.01
        assert abs(result["ASX:CBA"]     - 120.0) < 0.01

    def test_fetch_live_prices_nse_uses_tv_scanner(self, monkeypatch):
        """NSE symbols are routed to TV scanner, not yfinance."""
        import urllib.request as _ur, json as _json

        # Patch urlopen to return a fake TV scanner response
        class _FakeResponse:
            def read(self):
                return _json.dumps({
                    "data": [
                        {"s": "NSE:INDIAGLYCO", "d": ["INDIAGLYCO", 974.1]}
                    ]
                }).encode()

        class _FakeOpener:
            def __enter__(self):
                return _FakeResponse()
            def __exit__(self, *a):
                pass

        def _mock_urlopen(req, timeout=15):
            return _FakeResponse()

        monkeypatch.setattr(_ur, "urlopen", _mock_urlopen)

        result = self.bv.fetch_live_prices(["NSE:INDIAGLYCO"])
        assert result["NSE:INDIAGLYCO"] is not None
        assert abs(result["NSE:INDIAGLYCO"] - 974.1) < 0.01

    def test_fetch_live_prices_unsupported_exchange_returns_none(self):
        """Futures exchanges not supported by either path must return None."""
        result = self.bv.fetch_live_prices(["CME:ES1!", "CBOT:ZC1!"])
        # Should not raise — both should be None
        assert result["CME:ES1!"]  is None
        assert result["CBOT:ZC1!"] is None

    def test_fetch_live_prices_yfinance_failure_returns_none(self, monkeypatch):
        """If yfinance raises, fetch_live_prices returns None (non-fatal)."""
        import types, sys

        mock_yf = types.ModuleType("yfinance")
        mock_yf.Ticker = lambda _: (_ for _ in ()).throw(RuntimeError("yf down"))
        mock_yf.download = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("yf down"))
        monkeypatch.setitem(sys.modules, "yfinance", mock_yf)

        result = self.bv.fetch_live_prices(["NYSE:DELL"])
        # Should not raise; price stays None
        assert result["NYSE:DELL"] is None

    # ── Live price injection into trades ──────────────────────────────────────

    def test_live_price_overrides_snapshot_in_trade(self, monkeypatch, tmp_path):
        """After fetch_live_prices, injected live price must override snapshot price."""
        import sqlite3, json as _json, sys, types
        sys.path.insert(0, str(ROOT / "tradingview"))
        from backtest_vectorbt import load_trades, _TV_SCANNER_BY_EXCHANGE

        # Create minimal DB
        db_path = str(tmp_path / "live_test.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE alerts (
                alert_uuid TEXT PRIMARY KEY, tv_symbol TEXT, fire_time TEXT,
                resolution TEXT,
                composite_score REAL, l1_score REAL, l2_score REAL,
                l3_score REAL, l4_score REAL,
                entry_price REAL, source TEXT, result_json TEXT
            );
            CREATE TABLE performance_metrics (
                alert_uuid TEXT PRIMARY KEY,
                return_7d REAL, return_3d REAL, return_1d REAL,
                max_return REAL, max_drawdown REAL, status TEXT
            );
            CREATE TABLE price_snapshots (
                id INTEGER PRIMARY KEY, alert_uuid TEXT, snapshot_time TEXT,
                price REAL, elapsed_hours REAL, return_pct REAL
            );
        """)
        uuid = "live-test-uuid"
        entry = 193.17
        snap  = 196.55  # stale snapshot (what it was 6 days ago)
        live  = 212.36  # today's actual price
        rj = _json.dumps({"fundamentals": {"current_price": entry, "company_name": "Dell"}})
        conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (uuid, "NYSE:DELL", "2026-04-16T09:00:00", "D", 65.6, 1.0, 1.0, 1.0, 1.0, entry, "download", rj))
        conn.execute("INSERT INTO performance_metrics VALUES (?,?,?,?,?,?,?)",
                     (uuid, 1.75, 1.0, 0.5, 3.0, -0.5, "open"))
        conn.execute("INSERT INTO price_snapshots VALUES (?,?,?,?,?,?)",
                     (1, uuid, "2026-04-16T10:00:00", snap, 24.0, 1.75))
        conn.commit(); conn.close()

        trades = load_trades(db_path, min_return_abs=0.0)
        assert len(trades) == 1
        # Before injection: current_price should be snapshot
        assert abs(trades[0]["current_price"] - snap) < 0.01
        assert trades[0]["current_price_src"] == "snapshot"

        # Mock yfinance to return the live price
        mock_yf = types.ModuleType("yfinance")
        import pandas as pd

        class _MockTicker:
            def __init__(self, sym): pass
            def history(self, **kw):
                return pd.DataFrame({"Close": [live]})

        mock_yf.Ticker = _MockTicker
        import sys as _sys
        monkeypatch.setitem(_sys.modules, "yfinance", mock_yf)

        # Simulate what main() does: fetch + inject
        from backtest_vectorbt import fetch_live_prices
        live_prices = fetch_live_prices(["NYSE:DELL"])
        for t in trades:
            lp = live_prices.get(t["symbol"])
            if lp is not None and lp > 0:
                t["current_price"] = lp
                exch = t["symbol"].split(":", 1)[0].upper()
                t["current_price_src"] = "live_tv" if exch in _TV_SCANNER_BY_EXCHANGE else "live_yf"

        assert abs(trades[0]["current_price"] - live) < 0.01, (
            f"Expected live price {live}, got {trades[0]['current_price']}"
        )
        assert trades[0]["current_price"] != snap, "Live price must differ from stale snapshot"
        assert trades[0]["current_price_src"] == "live_yf"

    def test_live_price_badge_appears_in_report_html(self, tmp_path, monkeypatch):
        """HTML report must show live price badge when current_price_src='live_yf'."""
        import sys
        sys.path.insert(0, str(ROOT / "tradingview"))
        from backtest_vectorbt import compute_bucket_stats, walk_forward, generate_html_report

        trades = [
            {
                "symbol": "NYSE:DELL", "fire_time": __import__("datetime").datetime(2026, 4, 16),
                "company_name": "Dell Technologies", "source": "download",
                "entry_price": 193.17, "current_price": 212.36, "current_price_src": "live_yf",
                "score": 65.6, "l1_score": 1.0, "l2_score": 1.0, "l3_score": 1.0, "l4_score": 1.0,
                "rsi": None, "adx": None, "squeeze": None, "rel_vol": None,
                "sma200_pct": None, "macd_hist": None,
                "l2_valuation": None, "l2_profit": None, "l2_growth": None, "l2_health": None,
                "l3_analyst": None, "l3_news": None, "sentiment_label": "", "sentiment_score": None,
                "sentiment_count": None, "l4_pcr": None, "l4_vix_score": None, "vix_at_fire": None,
                "insider_net": None, "pcr_raw": None, "us10y": None, "spread_bps": None,
                "return_7d": 1.75, "return_3d": 0.5, "return_1d": 0.2,
                "max_return": 3.0, "max_drawdown": -0.5, "status": "open",
                "best_return": 1.75,
                "actual_return": (212.36 - 193.17) / 193.17 * 100.0,
                "data_quality": "OK",
                "pe": None, "roe": None,
            }
        ]
        stats   = compute_bucket_stats(trades)
        windows = walk_forward(trades, n_windows=1)
        out     = str(tmp_path / "live_badge.html")
        generate_html_report(trades, stats, windows, out, "test.db")
        html    = (tmp_path / "live_badge.html").read_text(encoding="utf-8")

        # Green live badge colour must be present
        assert "#00c853" in html, "Live yfinance badge colour #00c853 must appear in report"
        # Price 212.36 must be shown (not the stale 196.55)
        assert "212.3600" in html, "Live price 212.36 must appear in report"


class TestPerformanceAnalyzerReportHygiene:
    """Regression tests for performance-analysis report symbol hygiene."""

    @pytest.fixture(autouse=True)
    def _import_pa(self):
        import sys
        sys.path.insert(0, str(ROOT / "tradingview"))
        import performance_analyzer as pa
        self.pa = pa

    def test_default_db_path_uses_root_database(self):
        expected = ROOT / "database" / "alert_tracker.db"
        assert self.pa._DB_PATH == expected

    def test_wrapped_tv_symbol_is_cleaned_in_html(self):
        wrapped = '={"adjustment":"splits","currency-id":"INR","session":"regular","symbol":"NSE:WAAREEENER"}'
        rows = [{
            "ticker": wrapped,
            "tv_symbol": wrapped,
            "yf_ticker": '"adjustment":"splits","symbol":"NSE:WAAREEENER"}',
            "fire_time": "2026-04-22T10:00:00",
            "entry_price": 100.0,
            "return_7d": 6.0,
            "return_3d": 4.0,
            "return_1d": 1.0,
            "max_return": 6.0,
            "max_drawdown": -1.0,
            "outcome": "success",
            "source": "screener",
            "composite_score": 75.0,
            "l1_score": 80.0,
            "l2_score": 70.0,
            "l3_score": 60.0,
            "l4_score": 50.0,
            "win_rate_at_fire": 55.0,
            "vix_at_signal": 18.0,
            "market_regime": "neutral",
            "sector": "Energy",
            "signal_type": "actionable",
            "signal": "BUY",
            "resolution": "D",
            "current_price": 106.0,
            "best_return": 6.0,
            "actual_return": 6.0,
            "data_quality": "OK",
        }]

        html = self.pa.build_html(rows, min_samples=1, tradable_only=True)

        assert "NSE:WAAREEENER" in html
        assert "NSE%3AWAAREEENER" in html
        assert "adjustment&quot;" not in html
        assert "currency-id" not in html


class TestSellEngineBugFixes:
    """
    Direct unit tests for the two confirmed sell_engine.py bugs fixed
    in the catastrophic-loss / penny-stock rounding patch.
    """

    @pytest.fixture(autouse=True)
    def _import_se(self):
        import sys
        sys.path.insert(0, str(ROOT / "tradingview" / "signal_engine"))
        import sell_engine as se
        self.se = se

    def _base_result(self, **kwargs):
        """Minimal result dict for apply_exit_rules()."""
        base = {
            "symbol":          "TEST",
            "entry_price":     100.0,
            "entry_date":      "2026-04-01",   # 7 trading days ago by default (patched in tests)
            "high_water_mark": 100.0,
            "current_price":   100.0,
            "atr_entry":       None,
            "atr_current":     None,
            "rsi":             None,
            "adx":             None,
            "rel_vol":         None,
            "pe":              None,
            "roe":             None,
            "days_to_lt":      None,
            "price_change_5d": None,
        }
        base.update(kwargs)
        return base

    # ── Bug 1: catastrophic loss bypasses MIN_HOLD guard ──────────────────────

    def test_catastrophic_loss_30pct_gets_sell_in_min_hold_window(self):
        """
        OTC:ONPH pattern: -30% loss within 7 trading days must produce SELL.
        Before fix: min_hold_active=True → REVIEW (wrong).
        After fix: catastrophic_loss=True → min_hold bypassed → SELL.
        """
        se = self.se
        from datetime import datetime
        # 7 trading days ago = use a recent date; fix to known date
        result = self._base_result(
            entry_price=0.10,
            current_price=0.07,       # -30%
            high_water_mark=0.10,
            entry_date="2026-04-22",  # will be ~7 trading days before test now
        )
        now = datetime(2026, 5, 1)   # ~7 business days after Apr 22
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        assert out["catastrophic_loss"] is True,  "Loss > 16% must flag catastrophic_loss"
        assert out["min_hold_active"] is False,   "min_hold must be bypassed for catastrophic loss"
        assert out["action"] == "SELL",           f"Expected SELL, got {out['action']}: {out['action_reason']}"

    def test_small_loss_5pct_still_gets_review_in_min_hold_window(self):
        """
        A small -5% dip within 7 days must still be protected by min_hold → REVIEW.
        This verifies the guard is intact for noise, only bypassed for thesis failures.
        """
        se = self.se
        from datetime import datetime
        result = self._base_result(
            entry_price=100.0,
            current_price=93.0,      # -7%: below hard_stop_floor at 92, triggers stop
            high_water_mark=100.0,
            entry_date="2026-04-22",
        )
        now = datetime(2026, 5, 1)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        # -7% < 16% → not catastrophic
        assert out["catastrophic_loss"] is False, "-7% must not flag catastrophic_loss"
        assert out["min_hold_active"] is True,    "min_hold must be active for small dip"
        assert out["action"] == "REVIEW",         f"Expected REVIEW, got {out['action']}"

    def test_catastrophic_loss_boundary_exactly_minus10pct(self):
        """
        Exactly at the -10% boundary (CATASTROPHIC_LOSS_MULT=1.25, HARD_STOP_FLOOR=0.08):
        -10% is NOT catastrophic (strict <, not <=), so min_hold still fires → REVIEW.
        """
        se = self.se
        from datetime import datetime
        entry = 100.0
        current = entry * 0.90   # exactly -10.0%
        result = self._base_result(
            entry_price=entry,
            current_price=current,
            high_water_mark=entry,
            entry_date="2026-04-22",
        )
        now = datetime(2026, 5, 1)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        # current_gain_pct = -10.0%, threshold is strictly < -10% (1.25 × 0.08)
        assert out["catastrophic_loss"] is False, "-10.0% is at boundary, not beyond — not catastrophic"
        assert out["min_hold_active"] is True,    "min_hold must still protect at exact boundary"
        assert out["action"] == "REVIEW"

    def test_catastrophic_loss_just_below_minus10pct(self):
        """
        -10.01% (just below the threshold) → catastrophic_loss=True → SELL even in min_hold.
        """
        se = self.se
        from datetime import datetime
        entry = 100.0
        current = entry * (1 - 0.1001)   # -10.01%
        result = self._base_result(
            entry_price=entry,
            current_price=current,
            high_water_mark=entry,
            entry_date="2026-04-22",
        )
        now = datetime(2026, 5, 1)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        assert out["catastrophic_loss"] is True,  "-10.01% must trigger catastrophic_loss"
        assert out["min_hold_active"] is False,   "min_hold must be bypassed"
        assert out["action"] == "SELL",           f"Expected SELL, got {out['action']}"

    def test_smlmah_pattern_minus11pct_gets_sell_in_min_hold(self):
        """
        NSE:SMLMAH -11.62% in 7 trading days (real observed case).
        After fix (CATASTROPHIC_LOSS_MULT=1.25, threshold=10%), must give SELL not REVIEW.
        """
        se = self.se
        from datetime import datetime
        entry   = 4299.5
        current = entry * (1 - 0.1162)  # -11.62%
        result = self._base_result(
            entry_price=entry,
            current_price=current,
            high_water_mark=entry,
            entry_date="2026-04-15",    # 7 trading days before 2026-04-24
        )
        now = datetime(2026, 4, 24)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        assert out["catastrophic_loss"] is True, "-11.62% must flag catastrophic_loss (>10% threshold)"
        assert out["min_hold_active"] is False,  "min_hold must be bypassed at -11.62%"
        assert out["action"] == "SELL",          f"SMLMAH pattern: expected SELL, got {out['action']}"

    def test_vea_pattern_minus10pt98_gets_sell_in_min_hold(self):
        """
        ASX:VEA -10.98% in 7 trading days (real observed case).
        After fix must give SELL not REVIEW.
        """
        se = self.se
        from datetime import datetime
        entry   = 2.5950
        current = entry * (1 - 0.1098)  # -10.98%
        result = self._base_result(
            entry_price=entry,
            current_price=current,
            high_water_mark=entry,
            entry_date="2026-04-15",
        )
        now = datetime(2026, 4, 24)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        assert out["catastrophic_loss"] is True, "-10.98% must flag catastrophic_loss (>10% threshold)"
        assert out["action"] == "SELL",          f"VEA pattern: expected SELL, got {out['action']}"

    def test_old_position_with_large_loss_gets_sell_regardless(self):
        """
        A 31-day-old position with -30% must also get SELL (base case, no min_hold).
        """
        se = self.se
        from datetime import datetime
        result = self._base_result(
            entry_price=0.10,
            current_price=0.07,    # -30%
            high_water_mark=0.10,
            entry_date="2026-03-10",  # > 14 trading days
        )
        now = datetime(2026, 5, 1)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        assert out["action"] == "SELL", f"Expected SELL for old position, got {out['action']}"

    # ── Bug 2: penny stock rounding collapse ──────────────────────────────────

    def test_penny_stock_rwax_pattern_no_spurious_trail(self):
        """
        OTC:RWAX pattern: entry=$0.0003, current=$0.0003 (0% return).
        Before fix: tp1_hit=True due to 4dp rounding collapse → TRAIL (wrong).
        After fix: R_significant=False → tp1_hit=False → action is HOLD/REVIEW/SELL, not TRAIL.
        """
        se = self.se
        from datetime import datetime
        entry = 0.0003
        result = self._base_result(
            entry_price=entry,
            current_price=entry,    # 0% return
            high_water_mark=entry,
            entry_date="2026-04-22",
        )
        now = datetime(2026, 5, 1)
        out = se.apply_exit_rules(result, vix=18.0, now=now)
        # R = 2 * (entry*0.02) = 0.000012 → not significant (< entry*0.001 = 0.0000003)
        # Wait: entry*0.001 = 0.0000003, R = 0.000012 > 0.0000003 — R IS significant!
        # But tp1_price_raw = entry + 1.5*0.000012 = 0.000318 > entry → need hwm >= tp1_price_raw
        # hwm = 0.0003 < 0.000318 → tp1_hit = False ✓
        assert out["tp1_hit"] is False, f"tp1_hit must be False at 0% return, got {out}"
        assert out["tp2_hit"] is False, f"tp2_hit must be False at 0% return, got {out}"
        assert out["action"] != "TRAIL", f"TRAIL must not fire at 0% return, got {out['action']}"

    def test_penny_stock_no_trail_at_exact_entry(self):
        """Price exactly at entry (HWM = entry) must never trigger TRAIL."""
        se = self.se
        from datetime import datetime
        for entry in (0.0003, 0.001, 0.005, 0.01):
            result = self._base_result(
                entry_price=entry,
                current_price=entry,
                high_water_mark=entry,
                entry_date="2025-01-01",  # old enough to skip min_hold
            )
            out = se.apply_exit_rules(result, vix=18.0, now=datetime(2026, 5, 1))
            assert out["tp1_hit"] is False, f"tp1_hit must be False at entry={entry}"
            assert out["action"] != "TRAIL", f"TRAIL must not fire at entry={entry} with zero gain"

    def test_price_dp_normal_stock(self):
        """_price_dp for normal-price stock returns 4."""
        se = self.se
        assert se._price_dp(1.0)   == 4
        assert se._price_dp(10.0)  == 4
        assert se._price_dp(100.0) == 4
        assert se._price_dp(0.1)   == 4   # magnitude=1 → max(4,1+3)=4

    def test_price_dp_sub_dollar_stock(self):
        """_price_dp for sub-dollar stocks returns at least 5."""
        se = self.se
        assert se._price_dp(0.05) == 5    # magnitude=2 → max(4,2+3)=5
        assert se._price_dp(0.01) == 5    # magnitude=2 → max(4,2+3)=5

    def test_price_dp_penny_stock(self):
        """_price_dp for ultra-penny stocks returns more than 4."""
        se = self.se
        dp = se._price_dp(0.0003)
        assert dp > 4, f"_price_dp(0.0003) must be > 4, got {dp}"
        dp2 = se._price_dp(0.00001)
        assert dp2 > dp, "Smaller price must give more decimal places"

    def test_catastrophic_loss_constant_accessible(self):
        """CATASTROPHIC_LOSS_MULT must be 1.25 and accessible from SellEngine facade."""
        se = self.se
        assert se.CATASTROPHIC_LOSS_MULT == 1.25
        assert se.SellEngine.CATASTROPHIC_LOSS_MULT == 1.25
        # Derived threshold: 1.25 * 0.08 = 0.10 (10%)
        assert se.CATASTROPHIC_LOSS_MULT * se.HARD_STOP_FLOOR == pytest.approx(0.10)


class TestReportDbUniversalArchive:
    """Unit tests for every save_* function in reports/report_db.py."""

    @pytest.fixture(autouse=True)
    def _patch_db(self, tmp_path, monkeypatch):
        """Redirect the DB path to a temp file so tests don't touch production DB."""
        import sys
        sys.path.insert(0, str(ROOT / "reports"))
        import report_db
        monkeypatch.setattr(report_db, "DB_PATH",
                            str(tmp_path / "test_report_analytics.db"))
        report_db.init_db()
        self.db  = report_db
        self.tmp = tmp_path

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_count(self, table: str) -> int:
        import sqlite3
        con = sqlite3.connect(self.db.DB_PATH)
        n   = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.close()
        return n

    def _last_row(self, table: str) -> dict:
        import sqlite3
        con  = sqlite3.connect(self.db.DB_PATH)
        cur  = con.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1")
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
        con.close()
        return dict(zip(cols, row)) if row else {}

    # ── India portfolio ────────────────────────────────────────────────────────

    def test_save_india_snapshot(self):
        """save_india_snapshot must create one row in india_snapshots."""
        z_holdings = [
            {"tradingsymbol": "RELIANCE", "last_price": 2500.0, "quantity": 10,
             "average_price": 2300.0, "pnl": 2000.0},
            {"tradingsymbol": "TCS",      "last_price": 3800.0, "quantity": 5,
             "average_price": 3500.0, "pnl": 1500.0},
        ]
        a_holdings = [
            {"symboltoken": "INFY", "ltp": 1600.0, "quantity": 8,
             "averageprice": 1500.0, "profitandloss": 800.0},
        ]
        z_margins = {"equity": {"net": 15000.0}}
        a_rms     = {"net": 8000.0}
        self.db.save_india_snapshot(z_holdings, z_margins, a_holdings, a_rms, zerodha_ok=True)
        assert self._row_count("india_snapshots") == 1
        row = self._last_row("india_snapshots")
        assert row["positions_count"] == 3   # 2 Zerodha + 1 AngelOne
        assert row["zerodha_ok"] == 1

    # ── Alert tracker ─────────────────────────────────────────────────────────

    def test_save_alert_tracker_snapshot(self):
        """save_alert_tracker_snapshot must create one row in alert_tracker_snapshots."""
        stats = {
            "total_alerts": 5,
            "performance":  {"open": 2, "success": 2, "failed": 1},
            "source_perf":  [{"source": "screener", "total": 3, "success_rate_pct": 66.7}],
            "resolution_perf": [
                {"resolution": "success", "total": 2, "win_rate": 100.0},
                {"resolution": "failure", "total": 1, "win_rate": 0.0},
            ],
        }
        self.db.save_alert_tracker_snapshot(stats)
        assert self._row_count("alert_tracker_snapshots") == 1
        row = self._last_row("alert_tracker_snapshots")
        assert row["total_alerts"] == 5
        assert row["open_alerts"] == 2
        assert row["closed_success"] == 2
        assert row["closed_failed"] == 1

    # ── Sell strategy ─────────────────────────────────────────────────────────

    def test_save_sell_strategy_snapshot(self):
        """save_sell_strategy_snapshot must create one row in sell_strategy_snapshots."""
        positions = [
            {"symbol": "A", "action": "SELL",  "vix": 16.5},
            {"symbol": "B", "action": "HOLD",  "vix": 16.5},
            {"symbol": "C", "action": "TRAIL", "vix": 16.5},
        ]
        self.db.save_sell_strategy_snapshot(
            results=positions,
            vix=16.5,
            vix_regime="NORMAL",
        )
        assert self._row_count("sell_strategy_snapshots") == 1
        row = self._last_row("sell_strategy_snapshots")
        assert row["total_positions"] == 3
        assert row["sell_count"] == 1
        assert row["hold_count"] == 1
        assert row["trail_count"] == 1
        assert abs(row["vix"] - 16.5) < 0.01

    # ── Hedging advisor ───────────────────────────────────────────────────────

    def test_save_hedging_snapshot(self):
        """save_hedging_snapshot must create one row in hedging_snapshots."""
        recs = [
            {"symbol": "A", "rec": {"strategy": "EXIT",         "urgency": "HIGH"}, "pnl_pct": -15.0},
            {"symbol": "B", "rec": {"strategy": "COVERED_CALL", "urgency": "MEDIUM"}, "pnl_pct": 5.0},
            {"symbol": "C", "rec": {"strategy": "PROTECTIVE_PUT","urgency": "HIGH"}, "pnl_pct": -8.0},
        ]
        self.db.save_hedging_snapshot(recs, "BULL", india_vix=12.3)
        assert self._row_count("hedging_snapshots") == 1
        row = self._last_row("hedging_snapshots")
        assert row["total_positions"] == 3
        assert row["exit_count"] == 1
        assert abs(row["india_vix"] - 12.3) < 0.01
        assert row["regime"] == "BULL"

    # ── CGT snapshots ─────────────────────────────────────────────────────────

    def test_save_cgt_snapshot(self):
        """save_cgt_snapshot must insert one row per FY/currency combination."""
        import pandas as pd
        fy_df = pd.DataFrame([
            {"FY": "FY2024", "Currency": "AUD", "ST Gains": 1000.0,
             "ST Losses": -200.0, "LT Gross Gains": 2000.0, "LT Losses": -100.0,
             "CGT Discount": 500.0, "Net Taxable": 2200.0, "# Trades": 10},
            {"FY": "FY2024", "Currency": "USD", "ST Gains": 500.0,
             "ST Losses": -50.0, "LT Gross Gains": 0.0, "LT Losses": 0.0,
             "CGT Discount": 0.0, "Net Taxable": 450.0, "# Trades": 5},
        ])
        self.db.save_cgt_snapshot(fy_df, "TestBroker")
        assert self._row_count("cgt_snapshots") == 2
        rows = sorted(
            [self._last_row("cgt_snapshots")],
            key=lambda r: r.get("currency", ""),
        )
        # Check at least one AUD row
        import sqlite3
        con  = sqlite3.connect(self.db.DB_PATH)
        rows = con.execute(
            "SELECT * FROM cgt_snapshots WHERE broker='TestBroker' ORDER BY currency"
        ).fetchall()
        con.close()
        assert len(rows) == 2

    # ── Global portfolio ──────────────────────────────────────────────────────

    def test_save_global_portfolio_snapshot(self):
        """save_global_portfolio_snapshot must create one row in global_portfolio_snapshots."""
        positions = [
            {"currency": "USD", "market_val": 10000.0, "broker": "IBKR"},
            {"currency": "AUD", "market_val": 20000.0, "broker": "Moomoo-AU"},
        ]
        statuses = {"IBKR": "Live", "Moomoo-AU": "Cached"}
        self.db.save_global_portfolio_snapshot(
            all_positions=positions,
            statuses=statuses,
            aud_total=30000.0,
            usd_total=10000.0,
        )
        assert self._row_count("global_portfolio_snapshots") == 1
        row = self._last_row("global_portfolio_snapshots")
        assert row["positions_count"] == 2
        assert abs(row["usd_total"] - 10000.0) < 1.0
        assert abs(row["aud_total"] - 30000.0) < 1.0

    # ── Signal stack ─────────────────────────────────────────────────────────

    def test_save_signal_stack_snapshot(self):
        """save_signal_stack_snapshot must create one row in signal_stack_snapshots."""
        ranked     = [{"tv_symbol": f"SYM{i}", "composite": float(70 - i)} for i in range(20)]
        actionable = ranked[:5]
        self.db.save_signal_stack_snapshot(ranked, actionable, "moomoo", 45.0)
        assert self._row_count("signal_stack_snapshots") == 1
        row = self._last_row("signal_stack_snapshots")
        assert row["total_signals"] == 20
        assert row["actionable_count"] == 5
        assert row["source"] == "moomoo"
        assert abs(row["threshold"] - 45.0) < 0.01

    # ── F&O arbitrage ─────────────────────────────────────────────────────────

    def test_save_fo_arbitrage_snapshot(self):
        """save_fo_arbitrage_snapshot must create one row in fo_arbitrage_snapshots."""
        nse_rows   = [{"symbol": "NIFTY", "spread_pct": 0.25},
                      {"symbol": "BANKNIFTY", "spread_pct": 0.18}]
        forex_rows = [{"pair": "USDINR", "spread_pct": 0.05}]
        global_rows = [{"symbol": "SPX", "spread_pct": 0.12}]
        self.db.save_fo_arbitrage_snapshot(nse_rows, forex_rows, global_rows)
        assert self._row_count("fo_arbitrage_snapshots") == 1
        row = self._last_row("fo_arbitrage_snapshots")
        assert row["nse_count"] == 2
        assert row["forex_count"] == 1
        assert row["global_count"] == 1
        assert abs(row["top_nse_spread"] - 0.25) < 0.001

    # ── Performance analyzer ──────────────────────────────────────────────────

    def test_save_performance_analyzer_snapshot(self):
        """save_performance_analyzer_snapshot must create one row."""
        rows = [
            {"outcome": "success",  "composite": 65.0},
            {"outcome": "failure",  "composite": 40.0},
            {"outcome": "open",     "composite": 55.0},
        ]
        self.db.save_performance_analyzer_snapshot(rows)
        assert self._row_count("performance_analyzer_snapshots") == 1
        row = self._last_row("performance_analyzer_snapshots")
        assert row["total_signals"] == 3
        assert row["closed_signals"] == 2
        assert abs(row["win_rate_pct"] - 50.0) < 0.1

    # ── Trade history ─────────────────────────────────────────────────────────

    def test_save_trade_history_snapshot(self):
        """save_trade_history_snapshot must create one row in trade_history_snapshots."""
        stats = {
            "n": 25, "wins": 18, "losses": 7, "win_rate": 72.0,
            "total_gain": 8500.0, "profit_factor": 2.8, "sharpe": 1.65,
            "avg_hold_days": 14.2,
            "best_trade":  {"tv_symbol": "NYSE:NVDA", "pnl_pct": 45.0},
            "worst_trade": {"tv_symbol": "NSE:ADANI",  "pnl_pct": -12.0},
        }
        self.db.save_trade_history_snapshot(stats)
        assert self._row_count("trade_history_snapshots") == 1
        row = self._last_row("trade_history_snapshots")
        assert row["total_trades"] == 25
        assert row["wins"] == 18
        assert abs(row["win_rate_pct"] - 72.0) < 0.1
        assert abs(row["total_gain"] - 8500.0) < 1.0
        assert abs(row["profit_factor"] - 2.8) < 0.01

    # ── Fundamental analysis ──────────────────────────────────────────────────

    def test_save_fundamental_analysis_snapshot(self):
        """save_fundamental_analysis_snapshot must create one row."""
        ranked = [
            {"tv_symbol": "NYSE:MSFT", "composite": 85.0},
            {"tv_symbol": "NASDAQ:AAPL", "composite": 78.0},
            {"tv_symbol": "NYSE:GOOGL", "composite": 71.0},
        ]
        self.db.save_fundamental_analysis_snapshot(ranked)
        assert self._row_count("fundamental_analysis_snapshots") == 1
        row = self._last_row("fundamental_analysis_snapshots")
        assert row["symbols_count"] == 3
        assert row["top_symbol"] == "NYSE:MSFT"
        assert abs(row["top_composite"] - 85.0) < 0.1
        assert abs(row["avg_composite"] - 78.0) < 0.1

    # ── Options report ────────────────────────────────────────────────────────

    def test_save_options_report_snapshot(self):
        """save_options_report_snapshot must create one row."""
        results = [
            {"symbol": "NYSE:AAPL", "options_available": True,
             "top_contracts": [{"option_score": 0.72}]},
            {"symbol": "NYSE:MSFT", "options_available": False, "top_contracts": []},
        ]
        self.db.save_options_report_snapshot(results, "auto")
        assert self._row_count("options_report_snapshots") == 1
        row = self._last_row("options_report_snapshots")
        assert row["symbols_count"] == 2
        assert row["available_count"] == 1
        assert row["modes"] == "auto"

    # ── News screener ─────────────────────────────────────────────────────────

    def test_save_news_screener_snapshot(self):
        """save_news_screener_snapshot must create one row."""
        bull = [{"pro_symbol": "AAPL", "_news_compound": 0.8},
                {"pro_symbol": "MSFT", "_news_compound": 0.6}]
        bear = [{"pro_symbol": "META", "_news_compound": -0.7}]
        meta = {"providers_used": ["CNBC", "Bloomberg"], "max_age_hours": 24.0}
        self.db.save_news_screener_snapshot(bull, bear, meta)
        assert self._row_count("news_screener_snapshots") == 1
        row = self._last_row("news_screener_snapshots")
        assert row["bullish_count"] == 2
        assert row["bearish_count"] == 1
        assert row["total_signals"] == 3
        assert abs(row["max_age_hours"] - 24.0) < 0.01

    # ── Tax harvest ───────────────────────────────────────────────────────────

    def test_save_tax_harvest_snapshot(self):
        """save_tax_harvest_snapshot must create one row."""
        rows = [
            {"Symbol": "NSE:INFY", "Tax Saving": 3000.0, "Priority": "HIGH"},
            {"Symbol": "NSE:TCS",  "Tax Saving": 1500.0, "Priority": "MEDIUM"},
        ]
        self.db.save_tax_harvest_snapshot(rows, days_to_fy_end=90)
        assert self._row_count("tax_harvest_snapshots") == 1
        row = self._last_row("tax_harvest_snapshots")
        assert row["harvest_count"] == 2
        assert abs(row["total_tax_saving"] - 4500.0) < 1.0
        assert row["days_to_fy_end"] == 90
        assert row["high_priority_count"] == 1

    # ── Lending rates ─────────────────────────────────────────────────────────

    def test_save_lending_rates_snapshot(self):
        """save_lending_rates_snapshot must create one row."""
        lending = {
            "Moomoo-AU": {
                "markets": {
                    "ASX": {"rate_pct": 6.8},
                    "NASDAQ": {"rate_pct": 5.5},
                }
            },
            "IBKR": {
                "markets": {
                    "NYSE": {"rate_pct": 4.9},
                }
            },
        }
        self.db.save_lending_rates_snapshot(lending)
        assert self._row_count("lending_rates_snapshots") == 1
        row = self._last_row("lending_rates_snapshots")
        assert row["broker_count"] == 2
        assert row["market_count"] == 3
        assert abs(row["min_rate_pct"] - 4.9) < 0.01
        assert abs(row["max_rate_pct"] - 6.8) < 0.01

    # ── Broker API audit ──────────────────────────────────────────────────────

    def test_save_broker_api_audit_snapshot(self):
        """save_broker_api_audit_snapshot must create one row."""
        sources = ["moomoo", "ibkr", "zerodha", "angelone"]
        self.db.save_broker_api_audit_snapshot(sources, capabilities_count=28)
        assert self._row_count("broker_api_audit_snapshots") == 1
        row = self._last_row("broker_api_audit_snapshots")
        assert row["sources_count"] == 4
        assert row["capabilities_count"] == 28

    # ── report_runs log ───────────────────────────────────────────────────────

    def test_log_report_run_records_each_save(self):
        """Every save_* call must append one row to report_runs."""
        before = self._row_count("report_runs")
        self.db.save_broker_api_audit_snapshot(["moomoo"], 5)
        after  = self._row_count("report_runs")
        assert after == before + 1

    def test_all_tables_exist_after_init_db(self):
        """init_db must create every expected table."""
        import sqlite3
        con    = sqlite3.connect(self.db.DB_PATH)
        tables = {r[0] for r in
                  con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        con.close()
        expected = {
            "report_runs",
            "india_snapshots",
            "alert_tracker_snapshots",
            "sell_strategy_snapshots",
            "hedging_snapshots",
            "cgt_snapshots",
            "global_portfolio_snapshots",
            "signal_stack_snapshots",
            "fo_arbitrage_snapshots",
            "performance_analyzer_snapshots",
            "trade_history_snapshots",
            "fundamental_analysis_snapshots",
            "options_report_snapshots",
            "news_screener_snapshots",
            "tax_harvest_snapshots",
            "lending_rates_snapshots",
            "broker_api_audit_snapshots",
            "fo_chain_snapshots",
        }
        missing = expected - tables
        assert not missing, f"Tables missing from DB after init_db: {missing}"


class TestSignificanceTesting:
    """Bootstrap CI + permutation test for score-bucket return gaps."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        bt_dir = str(ROOT / "tradingview" / "backtest")
        if bt_dir not in sys.path:
            sys.path.insert(0, bt_dir)

    def _mod(self):
        import significance
        return significance

    def test_null_effect_ci_contains_zero(self):
        """Two identical distributions must not produce a significant gap."""
        import numpy as np
        m = self._mod()
        rng = np.random.default_rng(0)
        a = rng.normal(0, 1, 200)
        b = rng.normal(0, 1, 200)

        ci = m.bootstrap_mean_diff_ci(a, b)
        perm = m.permutation_test_mean_diff(a, b)

        assert not ci["excludes_zero"], "CI must contain 0 under the null"
        assert ci["ci_low"] < 0 < ci["ci_high"]
        assert perm["p_value"] > 0.05, "Null data must not be significant"

    def test_large_real_effect_is_detected(self):
        """A +5 sigma separation must be flagged as a confirmed edge."""
        import numpy as np
        m = self._mod()
        rng = np.random.default_rng(0)
        a = rng.normal(5, 1, 200)
        b = rng.normal(0, 1, 200)

        ci = m.bootstrap_mean_diff_ci(a, b)
        perm = m.permutation_test_mean_diff(a, b)

        assert ci["excludes_zero"]
        assert ci["ci_low"] > 0
        assert perm["p_value"] < 0.01
        assert m._verdict(ci, perm) == m.VERDICT_POSITIVE

    def test_ci_brackets_the_observed_difference(self):
        m = self._mod()
        ci = m.bootstrap_mean_diff_ci([3.0, 4.0, 5.0, 6.0] * 10, [1.0, 2.0] * 20)
        assert ci["ci_low"] <= ci["observed_diff"] <= ci["ci_high"]

    def test_small_sample_flagged_underpowered(self):
        """A 5-trade arm must be flagged so it is never traded on."""
        m = self._mod()
        ci = m.bootstrap_mean_diff_ci([10.0, 9.0, 8.0, 11.0, 12.0], [0.0] * 200)
        assert ci["n_treatment"] == 5
        assert ci["underpowered"] is True

    def test_underpowered_forces_non_confirmed_verdict_in_report(self):
        """Even a clean separation stays flagged when one arm is tiny."""
        m = self._mod()
        res = m.compare_score_buckets(
            [{"bucket": "APEX", "actual_return": v} for v in (10, 9, 8, 11, 12)]
            + [{"bucket": "LOW", "actual_return": 0.0} for _ in range(200)],
            n_resamples=2000,
        )
        assert res["bootstrap"]["underpowered"] is True

    def test_deterministic_for_fixed_seed(self):
        """Same input + same seed must reproduce identical numbers."""
        m = self._mod()
        a = [1.0, 5.0, -2.0, 3.0] * 15
        b = [0.5, -1.0, 2.0] * 20
        assert m.bootstrap_mean_diff_ci(a, b, seed=7) == m.bootstrap_mean_diff_ci(a, b, seed=7)
        assert m.permutation_test_mean_diff(a, b, seed=7) == m.permutation_test_mean_diff(a, b, seed=7)

    def test_empty_arm_returns_insufficient_data(self):
        m = self._mod()
        ci = m.bootstrap_mean_diff_ci([], [1.0, 2.0])
        perm = m.permutation_test_mean_diff([], [1.0, 2.0])
        assert m._verdict(ci, perm) == m.VERDICT_NO_DATA
        assert perm["p_value"] == 1.0

    def test_p_value_is_never_zero(self):
        """The observed split is one arrangement, so p must stay > 0."""
        m = self._mod()
        perm = m.permutation_test_mean_diff([100.0] * 50, [-100.0] * 50, n_resamples=500)
        assert perm["p_value"] > 0.0

    def test_compare_score_buckets_reads_ret_key_fallback(self):
        """JSON sidecar rows use 'ret' rather than 'actual_return'."""
        m = self._mod()
        trades = ([{"bucket": "APEX", "ret": 5.0}] * 40
                  + [{"bucket": "LOW", "ret": 0.0}] * 40)
        res = m.compare_score_buckets(trades, return_key="ret", n_resamples=1000)
        assert res["bootstrap"]["n_treatment"] == 40
        assert res["treatment_mean"] == 5.0

    def test_suite_returns_all_default_comparisons(self):
        m = self._mod()
        trades = ([{"bucket": "APEX", "actual_return": 5.0}] * 10
                  + [{"bucket": "HIGH", "actual_return": 2.0}] * 10
                  + [{"bucket": "MED", "actual_return": 1.0}] * 10
                  + [{"bucket": "LOW", "actual_return": 0.0}] * 10)
        results = m.run_significance_suite(trades, n_resamples=500)
        assert len(results) == len(m.DEFAULT_COMPARISONS)
        assert all("verdict" in r for r in results)


class TestSignificanceReportIntegration:
    """backtest_vectorbt must surface the significance result, not hide it."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def test_compute_significance_returns_results(self):
        import backtest_vectorbt as bv
        trades = ([{"bucket": "APEX", "actual_return": 5.0}] * 10
                  + [{"bucket": "LOW", "actual_return": 0.0}] * 10)
        results = bv.compute_significance(trades, n_resamples=500)
        assert results, "compute_significance returned nothing"
        assert "verdict" in results[0]

    def test_derives_bucket_from_raw_score(self):
        """Real trade dicts carry 'score', not 'bucket'.

        Regression: deriving the bucket was missing, so every comparison
        silently reported n=0 vs 0 instead of failing loudly.
        """
        import backtest_vectorbt as bv
        trades = ([{"score": 80.0, "actual_return": 5.0}] * 10
                  + [{"score": 20.0, "actual_return": 0.0}] * 10)
        results = bv.compute_significance(trades, n_resamples=500)
        apex_vs_low = results[0]
        assert apex_vs_low["bootstrap"]["n_treatment"] == 10, \
            "APEX arm empty — bucket was not derived from score"
        assert apex_vs_low["bootstrap"]["n_control"] == 10, \
            "LOW arm empty — bucket was not derived from score"

    def test_skips_trades_with_no_matured_return(self):
        import backtest_vectorbt as bv
        assert bv.compute_significance([{"score": 80.0, "actual_return": None}]) == []

    def test_significance_html_renders_verdict_and_ci(self):
        import backtest_vectorbt as bv
        trades = ([{"bucket": "APEX", "actual_return": 5.0}] * 10
                  + [{"bucket": "LOW", "actual_return": 0.0}] * 10)
        html = bv._significance_html(bv.compute_significance(trades, n_resamples=500))
        assert "Statistical Significance" in html
        assert "Bootstrap CI" in html
        assert "APEX vs LOW" in html
        assert "Permutation p" in html

    def test_significance_html_empty_when_no_results(self):
        import backtest_vectorbt as bv
        assert bv._significance_html([]) == ""


class TestSinceEntryHistoricalSection:
    """Historical since-entry returns stay visible but stay out of the stats."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def _trades(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        out = []
        for i in range(3):
            out.append({"symbol": "NASDAQ:OLD", "data_quality": "OK",
                        "live_return": 90.0, "actual_return": 1.0,
                        "fire_time": now - timedelta(days=200)})
        for i in range(3):
            out.append({"symbol": "ASX:NEW", "data_quality": "OK",
                        "live_return": 5.0, "actual_return": 1.0,
                        "fire_time": now - timedelta(days=5)})
        return out

    def test_section_renders_both_symbols(self):
        import backtest_vectorbt as bv
        html = bv._since_entry_html(self._trades())
        assert "Since-Entry Performance" in html
        assert "NASDAQ:OLD" in html
        assert "ASX:NEW" in html

    def test_section_shows_holding_period(self):
        """The holding period must be visible so long/short holds aren't confused."""
        import backtest_vectorbt as bv
        html = bv._since_entry_html(self._trades())
        assert "200d" in html, "long holding period not shown"
        assert "5d" in html, "short holding period not shown"
        assert "Median Held" in html

    def test_section_warns_it_is_not_comparable(self):
        import backtest_vectorbt as bv
        html = bv._since_entry_html(self._trades())
        assert "never mixed" in html.lower() or "not comparable" in html.lower()

    def test_live_return_excluded_from_significance(self):
        """A huge since-entry return must not move the fixed-horizon verdict."""
        import backtest_vectorbt as bv
        trades = [{"score": 80.0, "actual_return": 1.0, "live_return": 500.0}] * 10 \
            + [{"score": 20.0, "actual_return": 1.0, "live_return": -500.0}] * 10
        res = bv.compute_significance(trades, n_resamples=500)[0]
        assert res["bootstrap"]["observed_diff"] == 0.0, \
            "live_return leaked into the significance statistic"

    def test_empty_when_no_live_returns(self):
        import backtest_vectorbt as bv
        assert bv._since_entry_html(
            [{"symbol": "X", "data_quality": "OK", "live_return": None}] * 5
        ) == ""

    def test_ignores_symbols_below_min_sample(self):
        import backtest_vectorbt as bv
        from datetime import datetime, timezone
        html = bv._since_entry_html([
            {"symbol": "ASX:ONE", "data_quality": "OK", "live_return": 10.0,
             "fire_time": datetime.now(timezone.utc)}
        ])
        assert html == "", "symbol with n<3 should not appear"

    def test_loader_includes_alerts_with_no_matured_outcome(self, tmp_path):
        """Open alerts must appear here even with no return_7d.

        Regression: the section reused the fixed-horizon trade list, so symbols
        whose 7-day outcome had never been backfilled (ASX:TEA) vanished
        entirely despite being large movers.
        """
        import sqlite3
        import backtest_vectorbt as bv

        db = tmp_path / "t.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE alerts (alert_uuid TEXT, tv_symbol TEXT, "
                    "entry_price REAL, fire_time TEXT, composite_score REAL)")
        con.execute("CREATE TABLE price_snapshots (id INTEGER PRIMARY KEY, "
                    "alert_uuid TEXT, price REAL, snapshot_time TEXT)")
        con.execute("CREATE TABLE performance_metrics (alert_uuid TEXT, return_7d REAL)")
        for i in range(3):
            con.execute("INSERT INTO alerts VALUES (?,?,?,?,?)",
                        (f"u{i}", "ASX:OPEN", 5.0, "2026-05-01T00:00:00", 70.0))
            con.execute("INSERT INTO price_snapshots (alert_uuid, price, snapshot_time) "
                        "VALUES (?,?,?)", (f"u{i}", 8.0, "2026-05-02T00:00:00"))
        con.commit()
        con.close()

        rows = bv.load_since_entry_rows(str(db), use_live_prices=False)
        assert len(rows) == 3, "open alerts were dropped by the outcome filter"
        assert all(r["symbol"] == "ASX:OPEN" for r in rows)
        assert abs(rows[0]["live_return"] - 60.0) < 0.01

    def test_loader_marks_price_source(self, tmp_path):
        """Stale snapshot prices must be labelled, not silently presented as live."""
        import sqlite3
        import backtest_vectorbt as bv

        db = tmp_path / "t.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE alerts (alert_uuid TEXT, tv_symbol TEXT, "
                    "entry_price REAL, fire_time TEXT, composite_score REAL)")
        con.execute("CREATE TABLE price_snapshots (id INTEGER PRIMARY KEY, "
                    "alert_uuid TEXT, price REAL, snapshot_time TEXT)")
        con.execute("INSERT INTO alerts VALUES ('u1','ASX:X',5.0,'2026-05-01T00:00:00',70.0)")
        con.execute("INSERT INTO price_snapshots (alert_uuid, price, snapshot_time) "
                    "VALUES ('u1', 6.0, '2026-05-02T00:00:00')")
        con.commit()
        con.close()

        rows = bv.load_since_entry_rows(str(db), use_live_prices=False)
        assert rows[0]["price_src"] == "snapshot"
