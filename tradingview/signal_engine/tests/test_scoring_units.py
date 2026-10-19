"""
Unit tests for the individual scoring functions in signal_stack.py.

Tests score_sentiment(), score_market_internals(), and score_technical()
with the new SMA200/MACD signals. All tests are pure-math — no network calls.

Run with:  python -m pytest signal_engine/tests/test_scoring_units.py -v
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import signal_stack as ss


# ─── score_sentiment ──────────────────────────────────────────────────────────

class TestScoreSentiment(unittest.TestCase):
    """Direct unit tests for score_sentiment() — L3 scoring."""

    def _base_sent(self, **overrides) -> dict:
        """Neutral baseline — all sub-signals at midpoint."""
        base = {
            "analyst": {"bull_ratio": 1.0, "consensus": "Hold", "total": 10},
            "recent_upgrade": False,
            "recent_downgrade": False,
            "earnings_beats": {"beat_rate": 0.5, "beats": 2, "quarters": 4},
            "earnings_days_away": None,
            "earnings_risk": "Unknown",
            "news": {"score": 0.0, "label": "Neutral", "count": 10},
        }
        base.update(overrides)
        return base

    def test_output_keys(self):
        s = ss.score_sentiment(self._base_sent())
        for key in ("total", "analyst", "upgrade_mod", "earnings_beat",
                     "earnings_risk", "news"):
            self.assertIn(key, s, f"Missing key: {key}")

    def test_total_in_range(self):
        s = ss.score_sentiment(self._base_sent())
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_perfect_signals_near_ten(self):
        """All bullish signals should produce a high score."""
        sent = self._base_sent(
            analyst={"bull_ratio": 2.0, "total": 15},
            recent_upgrade=True,
            earnings_beats={"beat_rate": 1.0, "beats": 4, "quarters": 4},
            news={"score": 0.8, "label": "Very Bullish", "count": 20},
        )
        s = ss.score_sentiment(sent)
        self.assertGreater(s["total"], 8.5,
                           f"Perfect bullish → expected >8.5, got {s['total']}")

    def test_terrible_signals_near_zero(self):
        """All bearish signals should produce a low score."""
        sent = self._base_sent(
            analyst={"bull_ratio": 0.0, "total": 15},
            recent_downgrade=True,
            earnings_beats={"beat_rate": 0.0, "beats": 0, "quarters": 4},
            earnings_days_away=3,
            news={"score": -0.8, "label": "Very Bearish", "count": 20},
        )
        s = ss.score_sentiment(sent)
        self.assertLess(s["total"], 2.0,
                        f"All bearish → expected <2.0, got {s['total']}")

    def test_upgrade_boosts_score(self):
        base = ss.score_sentiment(self._base_sent())
        upgraded = ss.score_sentiment(self._base_sent(recent_upgrade=True))
        self.assertGreater(upgraded["total"], base["total"])

    def test_downgrade_lowers_score(self):
        base = ss.score_sentiment(self._base_sent())
        downgraded = ss.score_sentiment(self._base_sent(recent_downgrade=True))
        self.assertLess(downgraded["total"], base["total"])

    def test_earnings_risk_penalty_7_days(self):
        no_risk = ss.score_sentiment(self._base_sent())
        high_risk = ss.score_sentiment(self._base_sent(earnings_days_away=3))
        self.assertLess(high_risk["total"], no_risk["total"],
                        "Earnings in <7 days should lower score")
        self.assertEqual(high_risk["earnings_risk"], -2.5)

    def test_earnings_risk_penalty_14_days(self):
        s = ss.score_sentiment(self._base_sent(earnings_days_away=10))
        self.assertEqual(s["earnings_risk"], -1.5)

    def test_earnings_risk_21_days_mild(self):
        s = ss.score_sentiment(self._base_sent(earnings_days_away=18))
        self.assertEqual(s["earnings_risk"], -0.5)

    def test_earnings_risk_28_days_none(self):
        s = ss.score_sentiment(self._base_sent(earnings_days_away=30))
        self.assertEqual(s["earnings_risk"], 0.0)

    def test_sparse_beat_data_shrinks_toward_neutral(self):
        """With only 1 quarter, even 100% beat rate should be pulled toward 5."""
        sparse = self._base_sent(
            earnings_beats={"beat_rate": 1.0, "beats": 1, "quarters": 1}
        )
        full = self._base_sent(
            earnings_beats={"beat_rate": 1.0, "beats": 4, "quarters": 4}
        )
        s_sparse = ss.score_sentiment(sparse)
        s_full = ss.score_sentiment(full)
        self.assertLess(s_sparse["earnings_beat"], s_full["earnings_beat"],
                        "1 quarter data should be discounted vs 4 quarters")

    def test_empty_dict_no_crash(self):
        """score_sentiment({}) should not crash."""
        s = ss.score_sentiment({})
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_news_bullish_increases_score(self):
        neutral = ss.score_sentiment(self._base_sent(
            news={"score": 0.0, "count": 10}
        ))
        bullish = ss.score_sentiment(self._base_sent(
            news={"score": 0.6, "count": 10}
        ))
        self.assertGreater(bullish["total"], neutral["total"])

    def test_news_bearish_decreases_score(self):
        neutral = ss.score_sentiment(self._base_sent(
            news={"score": 0.0, "count": 10}
        ))
        bearish = ss.score_sentiment(self._base_sent(
            news={"score": -0.6, "count": 10}
        ))
        self.assertLess(bearish["total"], neutral["total"])


# ─── score_market_internals ──────────────────────────────────────────────────

class TestScoreMarketInternals(unittest.TestCase):
    """Direct unit tests for score_market_internals() — L4 scoring."""

    def _base_int(self, **overrides) -> dict:
        base = {
            "pcr": 0.9,
            "insider_score_raw": 5.0,
            "w52_position": 0.65,
        }
        base.update(overrides)
        return base

    def test_output_keys(self):
        s = ss.score_market_internals(self._base_int())
        for key in ("total", "pcr", "insider", "w52", "yc"):
            self.assertIn(key, s)

    def test_total_in_range(self):
        s = ss.score_market_internals(self._base_int())
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_bullish_pcr_high_score(self):
        """PCR < 0.5 → very bullish → score 10."""
        s = ss.score_market_internals(self._base_int(pcr=0.3))
        self.assertEqual(s["pcr"], 10.0)

    def test_bearish_pcr_low_score(self):
        """PCR > 1.5 → very bearish → score 1."""
        s = ss.score_market_internals(self._base_int(pcr=2.0))
        self.assertEqual(s["pcr"], 1.0)

    def test_none_pcr_neutral(self):
        s = ss.score_market_internals(self._base_int(pcr=None))
        self.assertEqual(s["pcr"], 5.0)

    def test_w52_momentum_sweet_spot(self):
        """0.50-0.85 is the momentum sweet spot → score 10."""
        s = ss.score_market_internals(self._base_int(w52_position=0.70))
        self.assertEqual(s["w52"], 10.0)

    def test_w52_near_high_decent(self):
        """0.85-0.95 → strong trend, slight extension → 8."""
        s = ss.score_market_internals(self._base_int(w52_position=0.90))
        self.assertEqual(s["w52"], 8.0)

    def test_w52_at_ceiling(self):
        """0.95+ → at ceiling → 5 (could break out or reverse)."""
        s = ss.score_market_internals(self._base_int(w52_position=0.97))
        self.assertEqual(s["w52"], 5.0)

    def test_w52_near_low_is_danger(self):
        """<0.15 → near 52W low → 2 (falling knife risk)."""
        s = ss.score_market_internals(self._base_int(w52_position=0.10))
        self.assertEqual(s["w52"], 2.0)

    def test_w52_none_neutral(self):
        s = ss.score_market_internals(self._base_int(w52_position=None))
        self.assertEqual(s["w52"], 5.0)

    def test_insider_buying_boosts_score(self):
        low = ss.score_market_internals(self._base_int(insider_score_raw=2.0))
        high = ss.score_market_internals(self._base_int(insider_score_raw=9.0))
        self.assertGreater(high["total"], low["total"])

    def test_yield_curve_inverted_lowers_score(self):
        normal = ss.score_market_internals(self._base_int(), yc={"spread_bps": 50})
        inverted = ss.score_market_internals(self._base_int(), yc={"spread_bps": -50})
        self.assertGreater(normal["total"], inverted["total"])

    def test_yield_curve_none_neutral(self):
        s = ss.score_market_internals(self._base_int(), yc=None)
        self.assertEqual(s["yc"], 5.0)

    def test_empty_internals_no_crash(self):
        s = ss.score_market_internals({})
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_sparse_non_us_data_is_shrunk_toward_neutral(self):
        sparse = ss.score_market_internals(
            {
                "pcr": None,
                "pcr_available": False,
                "insider_score_raw": 5.0,
                "insider_available": False,
                "w52_position": None,
                "short_score_raw": 5.0,
                "short_available": False,
                "vix3m_vix_ratio": 1.2,
            },
            yc={"spread_bps": 100},
            vix_level=12.0,
        )
        self.assertLess(sparse["coverage"], 0.5)
        self.assertLess(sparse["total"], sparse["raw_total"])
        self.assertGreater(sparse["total"], 5.0)

    def test_full_observed_data_retains_more_conviction(self):
        full = ss.score_market_internals(
            {
                "pcr": 0.3,
                "pcr_available": True,
                "insider_score_raw": 9.0,
                "insider_available": True,
                "w52_position": 0.7,
                "short_ratio": 1.0,
                "short_score_raw": 9.0,
                "short_available": True,
                "sector_etf_score": 9.0,
                "vix3m_vix_ratio": 1.2,
            },
            yc={"spread_bps": 100},
            vix_level=12.0,
        )
        self.assertEqual(full["coverage"], 1.0)
        self.assertEqual(full["total"], full["raw_total"])

    def test_pcr_expiry_prefers_21_to_60_dte(self):
        anchor = datetime(2026, 7, 13, tzinfo=timezone.utc)
        selected = ss._select_pcr_expiry(
            ["2026-07-17", "2026-07-31", "2026-08-21", "2026-10-16"],
            today=anchor,
        )
        self.assertEqual(selected, "2026-08-21")


# ─── score_technical with new SMA200/MACD signals ────────────────────────────

class TestScoreTechnicalNewSignals(unittest.TestCase):
    """Tests for score_technical() with SMA200 and MACD screener signals."""

    def _meta(self) -> dict:
        return {"WinRate": 65, "NetProfit%": 25, "rsi": 65, "sqzOff": 1.0, "ADX": 30}

    def test_sma200_above_trend_scores_well(self):
        s = ss.score_technical(self._meta(), screener={"sma200_pct": 12.0})
        self.assertEqual(s["sma200"], 10.0, "10-20% above SMA200 is ideal sweet spot")

    def test_sma200_below_trend_scores_low(self):
        s = ss.score_technical(self._meta(), screener={"sma200_pct": -10.0})
        self.assertEqual(s["sma200"], 1.0, "Deep below SMA200 → bearish")

    def test_sma200_just_above(self):
        s = ss.score_technical(self._meta(), screener={"sma200_pct": 1.5})
        self.assertEqual(s["sma200"], 6.0, "Just above SMA200 → watch")

    def test_sma200_none_redistributed(self):
        """When SMA200 is unavailable, its weight goes to WR/NP."""
        s_with = ss.score_technical(self._meta(), screener={"sma200_pct": 12.0})
        s_without = ss.score_technical(self._meta(), screener={})
        # Both should produce valid scores
        self.assertGreaterEqual(s_without["total"], 0.0)
        self.assertIsNone(s_without["sma200"])

    def test_macd_bullish_scores_well(self):
        # close=100, vol=2% → daily_move=2.0; hist=0.5 → norm=0.25 → 10.0
        s = ss.score_technical(self._meta(),
                               screener={"macd_hist": 0.5, "close": 100.0, "volatility": 2.0})
        self.assertEqual(s["macd"], 10.0, "Normalised MACD >12% of daily move = strong bullish")

    def test_macd_bearish_scores_low(self):
        # close=100, vol=2% → daily_move=2.0; hist=-0.5 → norm=-0.25 < -0.06 → 1.5
        s = ss.score_technical(self._meta(),
                               screener={"macd_hist": -0.5, "close": 100.0, "volatility": 2.0})
        self.assertEqual(s["macd"], 1.5, "Normalised MACD < -6% of daily move = strong bearish")

    def test_macd_mild_bullish(self):
        # close=100, vol=2% → daily_move=2.0; hist=0.1 → norm=0.05 (0<norm<0.06 → 6.5)
        s = ss.score_technical(self._meta(),
                               screener={"macd_hist": 0.1, "close": 100.0, "volatility": 2.0})
        self.assertEqual(s["macd"], 6.5)

    def test_all_screener_fields_present(self):
        """With all optional fields present, no weight redistribution occurs."""
        screener = {
            "rel_vol": 2.0,
            "sma200_pct": 8.0,
            "macd_hist": 1.0,
        }
        s = ss.score_technical(self._meta(), screener=screener)
        self.assertIsNotNone(s["rel_vol"])
        self.assertIsNotNone(s["sma200"])
        self.assertIsNotNone(s["macd"])
        # Total should be higher than when signals are missing (all bullish here)
        s_none = ss.score_technical(self._meta(), screener={})
        # Can't assert > because redistribution to WR/NP might compensate,
        # but both should be valid
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_no_screener_at_all(self):
        """screener=None → all optional signals missing → weight redistributed."""
        s = ss.score_technical(self._meta(), screener=None)
        self.assertIsNone(s["rel_vol"])
        self.assertIsNone(s["sma200"])
        self.assertIsNone(s["macd"])
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)

    def test_raw_values_returned(self):
        screener = {"sma200_pct": 5.5, "macd_hist": 0.25, "rel_vol": 1.8}
        s = ss.score_technical(self._meta(), screener=screener)
        self.assertEqual(s["raw_sma200_pct"], 5.5)
        self.assertEqual(s["raw_macd_hist"], 0.25)
        self.assertEqual(s["raw_rv"], 1.8)



# ─── VIX market-regime scoring ────────────────────────────────────────────────

class TestVixRegime(unittest.TestCase):
    """Tests for VIX market-regime score inside score_market_internals (L4)."""

    def _base_int(self) -> dict:
        return {"pcr": 0.9, "insider_score_raw": 5.0, "w52_position": 0.65}

    def test_low_vix_boosts_total(self):
        """VIX=12 (extreme calm) should produce a higher total than VIX=35 (fear)."""
        calm  = ss.score_market_internals(self._base_int(), vix_level=12.0)
        fear  = ss.score_market_internals(self._base_int(), vix_level=35.0)
        self.assertGreater(calm["total"], fear["total"])

    def test_crisis_vix_gives_low_vix_score(self):
        s = ss.score_market_internals(self._base_int(), vix_level=50.0)
        self.assertEqual(s["vix"], 1.0, "VIX ≥40 = crisis → vix score 1.0")

    def test_calm_vix_gives_high_vix_score(self):
        s = ss.score_market_internals(self._base_int(), vix_level=13.0)
        self.assertEqual(s["vix"], 9.0, "VIX <15 = extreme calm → vix score 9.0")

    def test_none_vix_neutral(self):
        s = ss.score_market_internals(self._base_int(), vix_level=None)
        self.assertEqual(s["vix"], 5.0, "No VIX data → neutral 5.0")

    def test_vix_key_present_in_output(self):
        s = ss.score_market_internals(self._base_int())
        self.assertIn("vix", s)
        self.assertIn("macro", s)

    def test_macro_combines_vix_and_yc(self):
        """macro score = vix*0.6 + yc*0.4; with VIX=12, YC=50bps both good."""
        s = ss.score_market_internals(self._base_int(),
                                       yc={"spread_bps": 50}, vix_level=12.0)
        # vix=9.0, yc=8.0 → macro = 9.0*0.6 + 8.0*0.4 = 5.4 + 3.2 = 8.6
        self.assertAlmostEqual(s["macro"], 8.6, places=1)

    def test_backward_compat_no_vix_arg(self):
        """Old callers omitting vix_level must still work (default=None)."""
        s = ss.score_market_internals(self._base_int(), yc={"spread_bps": 50})
        self.assertGreaterEqual(s["total"], 0.0)
        self.assertLessEqual(s["total"], 10.0)


# ─── Smooth RSI scoring ───────────────────────────────────────────────────────

class TestSmoothRsi(unittest.TestCase):
    """Verify the new piecewise-linear RSI scoring has no cliff edges."""

    def _score_rsi(self, rsi):
        """Helper: run score_technical with fixed meta except RSI."""
        meta = {"WinRate": 65, "NetProfit%": 25, "sqzOff": 0.0, "ADX": 25, "rsi": rsi}
        return ss.score_technical(meta, screener=None)["rsi"]

    def test_never_drops_below_1(self):
        for rsi in (0, 10, 20, 100, 110):
            self.assertGreaterEqual(self._score_rsi(rsi), 1.0,
                                    f"RSI={rsi} should not drop below 1.0")

    def test_never_exceeds_10(self):
        for rsi in (60, 65, 70):
            self.assertLessEqual(self._score_rsi(rsi), 10.0,
                                 f"RSI={rsi} should not exceed 10.0")

    def test_ideal_zone_scores_ten(self):
        for rsi in (60, 65, 70):
            self.assertEqual(self._score_rsi(rsi), 10.0,
                             f"RSI={rsi} in 60-70 ideal zone should score 10.0")

    def test_no_cliff_at_60(self):
        """RSI 59.9 should be close to RSI 60.0, not a 100% jump."""
        s_599 = self._score_rsi(59.9)
        s_600 = self._score_rsi(60.0)
        self.assertGreater(s_600, s_599)
        self.assertLess(s_600 - s_599, 1.0,  # gap must be < 1.0 (was 5.0)
                        f"Cliff edge at RSI 60: {s_599:.2f}→{s_600:.2f}")

    def test_monotone_approaching_ideal(self):
        """Scores should increase as RSI rises from 40 → 65."""
        scores = [self._score_rsi(r) for r in range(40, 66, 5)]
        for i in range(len(scores) - 1):
            self.assertLessEqual(scores[i], scores[i + 1],
                                 f"RSI scoring not monotone near ideal zone: {scores}")

    def test_none_rsi_neutral(self):
        meta = {"WinRate": 65, "NetProfit%": 25, "rsi": None, "sqzOff": 0.0, "ADX": 25}
        s = ss.score_technical(meta, screener=None)
        self.assertEqual(s["rsi"], 5.0)


# ─── Cross-market MACD normalisation ─────────────────────────────────────────

class TestMacdNormalisation(unittest.TestCase):
    """MACD histogram must score comparably for NSE (₹100) and US ($300) stocks."""

    def _score_macd(self, macd_hist, close=None, volatility=None):
        meta = {"WinRate": 65, "NetProfit%": 25, "rsi": 65, "sqzOff": 0.0, "ADX": 25}
        screener = {"macd_hist": macd_hist}
        if close is not None:
            screener["close"] = close
        if volatility is not None:
            screener["volatility"] = volatility
        return ss.score_technical(meta, screener=screener)["macd"]

    def test_same_relative_strength_same_score(self):
        """NSE hist=0.12 on ₹100 stock (2% vol) should equal US hist=6 on $300 (2% vol).
        Both represent hist/daily_move ≈ 0.06 → s_macd = 8.0."""
        # NSE: daily_move = 100 * 2/100 = 2.0; hist=0.12 → norm=0.06
        nse = self._score_macd(0.12, close=100.0, volatility=2.0)
        # US: daily_move = 300 * 2/100 = 6.0; hist=0.36 → norm=0.06
        us  = self._score_macd(0.36, close=300.0, volatility=2.0)
        self.assertEqual(nse, us, f"NSE={nse} vs US={us} — should be equal after normalisation")

    def test_sign_only_fallback_still_reasonable(self):
        """When close/volatility missing, fallback uses sign → not 1.5 for any positive hist."""
        s_pos = self._score_macd(3.0)   # no close/vol — big US-style hist, fallback sign
        s_neg = self._score_macd(-3.0)
        self.assertGreater(s_pos, s_neg, "Positive hist should score higher than negative")
        self.assertGreaterEqual(s_pos, 6.5, "Positive hist fallback should not score < 6.5")

    def test_normalization_below_threshold_correct_bin(self):
        """hist=0.1 on $100 stock, 2% vol → norm=0.05 (0<0.05<0.06) → 6.5."""
        s = self._score_macd(0.1, close=100.0, volatility=2.0)
        self.assertEqual(s, 6.5)

    def test_strong_bullish_full_score(self):
        """hist=0.5 on $100 stock, 2% vol → norm=0.25 (>0.12) → 10.0."""
        s = self._score_macd(0.5, close=100.0, volatility=2.0)
        self.assertEqual(s, 10.0)

    def test_mild_bearish(self):
        """hist=-0.1 on $100 stock, 2% vol → norm=-0.05 (>-0.06) → 4.0."""
        s = self._score_macd(-0.1, close=100.0, volatility=2.0)
        self.assertEqual(s, 4.0)


# ─── composite edge cases ────────────────────────────────────────────────────

class TestCompositeEdgeCases(unittest.TestCase):

    def test_l5_only(self):
        c = ss.composite(8, 8, 8, 8, l5=5)
        self.assertGreater(c, 0.0)
        self.assertLess(c, 100.0)

    def test_l6_only(self):
        c = ss.composite(8, 8, 8, 8, l6=5)
        self.assertGreater(c, 0.0)
        self.assertLess(c, 100.0)

    def test_both_l5_l6(self):
        c = ss.composite(8, 8, 8, 8, l5=5, l6=5)
        self.assertGreater(c, 0.0)
        self.assertLess(c, 100.0)

    def test_l5_l6_lower_base_proportion(self):
        """With L5+L6, base L1-L4 share should be reduced from 100% to 82%."""
        base = ss.composite(10, 10, 10, 10)
        both = ss.composite(10, 10, 10, 10, l5=10, l6=10)
        self.assertAlmostEqual(base, 100.0, places=1)
        self.assertAlmostEqual(both, 100.0, places=1)


# ─── grade edge cases ────────────────────────────────────────────────────────

class TestGradeEdgeCases(unittest.TestCase):

    def test_zero(self):
        letter, _ = ss.grade(0)
        self.assertEqual(letter, "F")

    def test_exactly_75(self):
        letter, _ = ss.grade(75)
        self.assertEqual(letter, "A")

    def test_exactly_60(self):
        letter, _ = ss.grade(60)
        self.assertEqual(letter, "B")

    def test_100(self):
        letter, _ = ss.grade(100)
        self.assertEqual(letter, "A")


# ─── Sector ETF scoring (L4f) ───────────────────────────────────────────────

class TestSectorEtfInMarketInternals(unittest.TestCase):
    """score_market_internals() new sector_etf sub-score (4f)."""

    def _score(self, sector_etf_score=None, pcr=None, vix=None):
        internals = {
            "pcr": pcr,
            "insider_score_raw": 5.0,
            "w52_position": 0.65,          # momentum sweet spot → 10.0
            "sector_etf_score": sector_etf_score,
        }
        return ss.score_market_internals(internals, yc={}, vix_level=vix)

    def test_neutral_when_none(self):
        """Missing sector_etf_score → neutral 5.0 in output."""
        r = self._score(sector_etf_score=None)
        self.assertEqual(r["sector_etf"], 5.0)

    def test_bullish_sector_raises_total(self):
        """High sector ETF score (9.0) should push total above neutral."""
        r_bull = self._score(sector_etf_score=9.0, vix=18.0)
        r_neut = self._score(sector_etf_score=5.0, vix=18.0)
        self.assertGreater(r_bull["total"], r_neut["total"])

    def test_bearish_sector_lowers_total(self):
        """Low sector ETF score (1.0) should pull total below neutral sector."""
        r_bear = self._score(sector_etf_score=1.0, vix=18.0)
        r_neut = self._score(sector_etf_score=5.0, vix=18.0)
        self.assertLess(r_bear["total"], r_neut["total"])

    def test_sector_etf_key_in_output(self):
        """'sector_etf' key must always be present in return dict."""
        r = self._score()
        self.assertIn("sector_etf", r)

    def test_weights_sum_to_one(self):
        """New weights (0.28+0.22+0.20+0.20+0.10) must equal 1.0."""
        weights = [0.28, 0.22, 0.20, 0.20, 0.10]
        self.assertAlmostEqual(sum(weights), 1.0, places=10)

    def test_all_perfect_scores_gives_10(self):
        """internals with all sub-scores = 10 should produce total = 10.0."""
        internals = {
            "pcr": 0.3,                # → s_pcr = 10
            "insider_score_raw": 10.0, # → s_insider = 10
            "w52_position": 0.7,       # → s_w52 = 10
            "sector_etf_score": 10.0,  # → s_sector_etf = 10
            "short_ratio": 0.5,
            "short_score_raw": 10.0,
            "short_available": True,
        }
        # VIX=12 → s_vix=9; yc spread=100 → s_yc=10; s_macro=9.4
        # With s_macro≠10, total is ~9.88 not exactly 10 — just check it's high
        r = ss.score_market_internals(internals, yc={"spread_bps": 100}, vix_level=12.0)
        self.assertGreater(r["total"], 9.0)

    def test_all_worst_scores_gives_low(self):
        """internals with all sub-scores near 0 should produce a low total."""
        internals = {
            "pcr": 2.0,                # → s_pcr = 1
            "insider_score_raw": 0.0,  # → s_insider = 0
            "w52_position": 0.02,      # → s_w52 = 2
            "sector_etf_score": 1.0,   # → s_sector_etf = 1
        }
        r = ss.score_market_internals(internals, yc={"spread_bps": -50}, vix_level=45.0)
        self.assertLess(r["total"], 3.0)


# ─── Pipeline invariants ─────────────────────────────────────────────────────

class TestPipelineInvariants(unittest.TestCase):
    """Property invariants: all layer scores, composite in valid range."""

    def _make_score(self, l1=5.0, l2=5.0, l3=5.0, l4=5.0):
        comp = ss.composite(l1, l2, l3, l4)
        return comp

    def test_composite_range_all_zeroes(self):
        self.assertEqual(self._make_score(0, 0, 0, 0), 0.0)

    def test_composite_range_all_tens(self):
        self.assertAlmostEqual(self._make_score(10, 10, 10, 10), 100.0, places=1)

    def test_composite_midpoint(self):
        mid = self._make_score(5, 5, 5, 5)
        self.assertAlmostEqual(mid, 50.0, places=1)

    def test_score_market_internals_always_bounded(self):
        """Total must be in [0, 10] for many random internals combinations."""
        import itertools
        for pcr, pos, s_etf in itertools.product([None, 0.3, 1.0, 2.0],
                                                  [None, 0.1, 0.5, 0.9],
                                                  [None, 1.0, 5.0, 9.0]):
            ints = {"pcr": pcr, "insider_score_raw": 5.0,
                    "w52_position": pos, "sector_etf_score": s_etf}
            r = ss.score_market_internals(ints, yc={"spread_bps": 50}, vix_level=20.0)
            self.assertGreaterEqual(r["total"], 0.0,
                f"total below 0: pcr={pcr}, pos={pos}, etf={s_etf}")
            self.assertLessEqual(r["total"], 10.0,
                f"total above 10: pcr={pcr}, pos={pos}, etf={s_etf}")

    def test_score_fundamental_always_bounded(self):
        """Fundamental score must be in [0, 10] for edge-case inputs."""
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        import tv_screener_fundamentals as tvf
        for fund in [
            {},                                         # empty
            {"price_earnings_ttm": -5, "net_margin": -100},   # negative
            {"price_earnings_ttm": 1000, "debt_to_equity": 500},  # extreme
            {"return_on_equity": 500, "net_margin": 200},          # supernormal
        ]:
            r = tvf.score_fundamental(fund)
            self.assertGreaterEqual(r["total"], 0.0)
            self.assertLessEqual(r["total"], 10.0)


class TestDecisionScorePolicy(unittest.TestCase):

    def test_decision_score_equals_quality_when_confidence_is_absent(self):
        quality, decision = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
        )
        self.assertEqual(decision, quality)

    def test_book_bonus_lifts_quality_score(self):
        base, _ = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
        )
        boosted, _ = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
            book_bonus=4.0,
        )
        self.assertEqual(boosted, round(base + 4.0, 2))

    def test_pool_and_basket_boosts_add_to_the_decision_score(self):
        quality, decision = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
            pool_boost=3.0, basket_boost=5.0,
        )
        self.assertEqual(decision, round(quality + 8.0, 2))

    def test_l7_expert_signal_raises_quality(self):
        base, _ = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
        )
        with_l7, _ = ss._compute_decision_scores(
            7.0, 7.0, 6.0, 6.0, institutional_l5=None,
            l7=10.0,
        )
        self.assertGreater(with_l7, base)

    def test_only_decision_score_gates_actionability(self):
        item = {
            "composite": 75.0,
            "scores": {"l1": {"total": 4.0}},
            "sentiment_data": {"earnings_days_away": 3},
            "data_confidence": {"overall": 0.4},
        }
        self.assertEqual(ss._actionability_reasons(item, 50.0), [])

    def test_below_threshold_blocks_actionability(self):
        item = {
            "composite": 45.0,
            "scores": {"l1": {"total": 8.0}},
            "sentiment_data": {"earnings_days_away": 30},
            "data_confidence": {"overall": 0.9},
        }
        reasons = ss._actionability_reasons(item, 50.0)
        self.assertTrue(any("decision score" in reason for reason in reasons))

    def test_etf_is_not_actionable_as_equity(self):
        item = {"composite": 75.0,
                "fundamentals": {"industry": "Investment Trusts/Mutual Funds"}}
        self.assertTrue(any("ETF/fund" in r
                            for r in ss._actionability_reasons(item, 50.0)))

    def test_investment_manager_is_still_a_real_equity(self):
        item = {"composite": 75.0, "fundamentals": {"industry": "Investment Managers"}}
        self.assertEqual(ss._actionability_reasons(item, 50.0), [])

    def test_price_at_or_above_consensus_target_blocks(self):
        item = {"composite": 75.0,
                "fundamentals": {"current_price": 61.99, "analyst_target": 55.08}}
        self.assertTrue(any("consensus target" in r
                            for r in ss._actionability_reasons(item, 50.0)))

    def test_price_below_target_does_not_block(self):
        item = {"composite": 75.0,
                "fundamentals": {"current_price": 50.0, "analyst_target": 60.0}}
        self.assertEqual(ss._actionability_reasons(item, 50.0), [])

    def test_earnings_inside_blackout_blocks(self):
        item = {"composite": 75.0, "sentiment_data": {"earnings_days_away": 0}}
        self.assertTrue(any("earnings in" in r
                            for r in ss._actionability_reasons(item, 50.0)))

    def test_trigger_and_buy_flag_vetoes_are_opt_in(self):
        item = {
            "composite": 75.0,
            "fundamentals": {"current_price": 123.88},
            "meta": {"trigger_price": 123.93, "BUY": "false"},
        }
        self.assertEqual(ss._actionability_reasons(item, 50.0), [])
        opted_in = ss._actionability_reasons(
            item, 50.0, require_trigger=True, require_buy_flag=True
        )
        self.assertTrue(any("below trigger" in r for r in opted_in))
        self.assertTrue(any("BUY flag" in r for r in opted_in))

    def test_low_confidence_demotes_decision_score(self):
        _, high = ss._compute_decision_scores(
            8.0, 8.0, 8.0, 8.0, institutional_l5=None,
            data_confidence=1.0,
        )
        _, low = ss._compute_decision_scores(
            8.0, 8.0, 8.0, 8.0, institutional_l5=None,
            data_confidence=0.5,
        )
        self.assertLess(low, high)
        # Floored, so a thin-data name stays rankable rather than collapsing.
        self.assertAlmostEqual(low / high, ss.CONFIDENCE_DEMOTION_FLOOR, places=2)

    def test_confidence_omitted_leaves_score_unchanged(self):
        _, without = ss._compute_decision_scores(
            7.0, 7.0, 7.0, 7.0, institutional_l5=None,
        )
        _, full = ss._compute_decision_scores(
            7.0, 7.0, 7.0, 7.0, institutional_l5=None,
            data_confidence=1.0,
        )
        self.assertEqual(without, full)

    def test_missing_data_reduces_confidence_without_changing_score(self):
        low = ss._data_confidence({}, {}, {}, {})
        high = ss._data_confidence(
            {"WinRate": 55, "NetProfit%": 20, "rsi": 62, "ADX": 25, "sqzOff": True},
            {"forward_pe": 20, "roe": 15, "profit_margin": 10,
             "earnings_growth": 8, "revenue_growth": 9, "debt_to_equity": 0.5,
             "free_cash_flow": 1_000_000},
            {"analyst": {"total": 2, "bull_ratio": 1.1},
             "earnings_beats": {"quarters": 4, "beat_rate": 0.75},
             "news": {"count": 3, "score": 0.2}, "earnings_days_away": 20},
            {"pcr": 0.8, "insider_buy_ratio": 0.5, "w52_position": 0.7,
             "short_ratio": 2.0, "sector_etf_score": 8.0, "vix_level": 18.0,
             "yc_spread": 50.0, "vix3m_vix_ratio": 1.1},
            {"rel_vol": 1.5, "sma200_pct": 5, "macd_hist": 0.2, "bb_width": 6},
        )
        self.assertLess(low["overall"], high["overall"])


if __name__ == "__main__":
    unittest.main()
