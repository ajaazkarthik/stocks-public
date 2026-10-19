"""
tests/test_buy_advisor_kelly.py
================================
Tests for buy_advisor.py Kelly Criterion position sizing logic.

Risk: CRITICAL — these functions drive real money allocation.
A sign error in _compute_half_kelly() silently returns 0% for all positions
because of the max(..., 0.0) floor; STRONG signals get no allocation.

Run:
    python -m pytest tests/test_buy_advisor_kelly.py -v
"""

import sys
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))


# ─── _compute_half_kelly ─────────────────────────────────────────────────────

class TestComputeHalfKelly:
    """_compute_half_kelly: half-Kelly = max(full_kelly / 2, 0.0)"""

    def _kelly(self, wr: float) -> float:
        from buy_advisor import _compute_half_kelly
        return _compute_half_kelly(wr)

    def test_known_values_match_manual_calculation(self):
        """WR=0.447, avg_W=0.3054, avg_L=0.1786 → full≈12.36%, half≈6.18%."""
        from buy_advisor import _KELLY_WR_DEFAULT
        result = self._kelly(_KELLY_WR_DEFAULT)
        # full_kelly = (0.447*0.3054 - 0.553*0.1786) / 0.3054 ≈ 0.1236
        # half_kelly = 0.0618
        assert abs(result - 0.0618) < 0.001, (
            f"Expected half-Kelly ≈ 6.18%, got {result*100:.2f}%"
        )

    def test_zero_win_rate_returns_zero(self):
        """WR=0 → full_kelly = negative → clamped to 0.0."""
        result = self._kelly(0.0)
        assert result == 0.0, "Zero WR must give 0.0 (max-floor applied)"

    def test_perfect_win_rate_is_capped(self):
        """WR=1.0 → full_kelly = 1.0, half ≤ 0.5."""
        result = self._kelly(1.0)
        assert 0.0 < result <= 0.5, f"WR=1.0 gave {result*100:.1f}%, expected ≤50%"

    def test_low_win_rate_clamped_to_zero(self):
        """WR=0.10: full_kelly is strongly negative → 0.0."""
        result = self._kelly(0.10)
        assert result == 0.0, "Low WR → negative full-Kelly must be clamped to 0.0"

    def test_result_is_fraction_not_percentage(self):
        """Returns a fraction [0,1], not a percentage [0,100]."""
        result = self._kelly(0.447)
        assert 0.0 <= result <= 1.0, (
            f"Expected fraction in [0,1], got {result}. "
            "If >1 the function is returning percent not fraction."
        )

    def test_result_non_negative_for_all_realistic_wr(self):
        """For WR in 0.30–0.70 (realistic range), result must always be >= 0."""
        for wr in [0.30, 0.35, 0.40, 0.447, 0.50, 0.55, 0.60, 0.70]:
            result = self._kelly(wr)
            assert result >= 0.0, f"Negative half-Kelly at WR={wr}: {result}"


# ─── _live_kelly_wr fallback ─────────────────────────────────────────────────

class TestLiveKellyWrFallback:
    """_live_kelly_wr withholds sizing when versioned evidence is absent or thin."""

    def test_returns_none_when_db_absent(self, tmp_path):
        import buy_advisor as ba
        result = ba._live_kelly_wr(tmp_path / "missing.db")
        assert result is None

    def test_returns_none_when_fewer_than_100_versioned_outcomes(self, tmp_path):
        import buy_advisor as ba

        db = tmp_path / "alert_tracker.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE signal_engine_outputs (id INTEGER PRIMARY KEY, timestamp TEXT, parameters_json TEXT)")
        conn.execute("CREATE TABLE price_evolution (output_id INTEGER, hours_elapsed REAL, return_pct REAL)")
        for _ in range(10):
            cursor = conn.execute(
                "INSERT INTO signal_engine_outputs(timestamp, parameters_json) VALUES (?, ?)",
                ("2026-07-13T00:00:00Z", '{"model_version":"test-v1"}'),
            )
            conn.execute(
                "INSERT INTO price_evolution VALUES (?, 168.0, 5.0)",
                (cursor.lastrowid,),
            )
        conn.commit()
        conn.close()

        assert ba._live_kelly_wr(db) is None

    def test_default_wr_is_sane(self):
        """_KELLY_WR_DEFAULT must be in a realistic range (30%–70%)."""
        from buy_advisor import _KELLY_WR_DEFAULT
        assert 0.30 <= _KELLY_WR_DEFAULT <= 0.70, (
            f"_KELLY_WR_DEFAULT={_KELLY_WR_DEFAULT} is outside realistic 30%-70% range"
        )


# ─── Position sizing by conviction ───────────────────────────────────────────

class TestKellyPositionSizing:
    """kelly_pct scales correctly by conviction tier."""

    def _kelly_pct(self, conviction: str, wr: float = 0.447) -> float:
        from buy_advisor import _compute_half_kelly, _KELLY_SCALE
        half_k = _compute_half_kelly(wr)
        return round(half_k * _KELLY_SCALE.get(conviction, 0.0) * 100, 1)

    def test_skip_gives_zero(self):
        """SKIP conviction → 0% position size."""
        assert self._kelly_pct("SKIP") == 0.0, "SKIP must give 0% — no position entered"

    def test_strong_greater_than_good_greater_than_weak(self):
        """STRONG > GOOD > WEAK > 0."""
        strong = self._kelly_pct("STRONG")
        good   = self._kelly_pct("GOOD")
        weak   = self._kelly_pct("WEAK")
        assert strong > good > weak > 0.0, (
            f"Expected STRONG({strong}%) > GOOD({good}%) > WEAK({weak}%) > 0"
        )

    def test_scale_factors_are_canonical(self):
        """Scale factors must match documented 100/75/50/0 percent."""
        from buy_advisor import _KELLY_SCALE
        assert _KELLY_SCALE["STRONG"] == 1.00
        assert _KELLY_SCALE["GOOD"]   == 0.75
        assert _KELLY_SCALE["WEAK"]   == 0.50
        assert _KELLY_SCALE["SKIP"]   == 0.0

    def test_strong_position_under_15_pct_at_default_wr(self):
        """At default WR=44.7%, STRONG position must be < 15% (sanity cap)."""
        strong = self._kelly_pct("STRONG")
        assert strong < 15.0, (
            f"STRONG kelly_pct={strong}% at default WR — suspiciously large. "
            "Check that full_kelly is halved (half-Kelly, not full-Kelly)."
        )
        assert strong > 0.0, "STRONG must give non-zero position at default WR"

    def test_zero_wr_all_convictions_give_zero(self):
        """WR=0 → floor clamps to 0 → all tiers give 0%."""
        for conv in ("STRONG", "GOOD", "WEAK"):
            pct = self._kelly_pct(conv, wr=0.0)
            assert pct == 0.0, f"{conv} with WR=0 gave {pct}% — floor not applied"

    def test_strong_equals_2x_weak(self):
        """STRONG is exactly 2× WEAK (100% scale vs 50% scale)."""
        strong = self._kelly_pct("STRONG")
        weak   = self._kelly_pct("WEAK")
        if strong > 0:
            assert abs(strong / weak - 2.0) < 0.01, (
                f"STRONG({strong}%) should be 2× WEAK({weak}%)"
            )


class TestUnvalidatedAdviceGate:

    def test_unvalidated_model_never_suggests_leverage(self, monkeypatch):
        import buy_advisor as ba
        monkeypatch.setattr(ba, "_live_kelly_inputs", lambda: None)
        item = {
            "tv_symbol": "NASDAQ:NVDA",
            "composite": 90.0,
            "actionability_reasons": [],
            "near_earnings": False,
        }

        advice = ba.build_buy_advice(item, {})

        assert advice["conviction"] == "UNVALIDATED"
        assert advice["leverage_alts"] == []
        assert advice["kelly_pct"] == 0.0
        assert "no leverage" in advice["suggestion"]

    def test_quality_gate_failure_forces_skip(self, monkeypatch):
        import buy_advisor as ba
        monkeypatch.setattr(
            ba,
            "_live_kelly_inputs",
            lambda: (0.55, 0.10, 0.05, 100),
        )
        item = {
            "tv_symbol": "NASDAQ:NVDA",
            "composite": 90.0,
            "actionability_reasons": ["L1 technical 4.0 < 5.0"],
            "near_earnings": False,
        }

        advice = ba.build_buy_advice(item, {})

        assert advice["conviction"] == "SKIP"
        assert advice["leverage_alts"] == []
        assert advice["kelly_pct"] == 0.0
