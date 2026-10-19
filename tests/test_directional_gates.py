"""
Tests for directional bias gates in options_actionable_report.py.

Covers:
  F15b-sma50-bearish / F15b-sma50-bullish  — Step 3b2 SMA50 alignment gate
  F16-signal-low / F16-signal-high         — Step 3d signal composite gate
  _dir_score()                             — direction quality ranking bonus
  final_rank                               — composite ranking
  skip_directional=True                    — backward compatibility

Design: contracts are pre-seeded with directional fields (signal_composite,
above_sma50, above_sma200). skip_signal=True + skip_support=True bypass
enrichment so pre-set values are preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))

from tradingview.options.options_actionable_report import run_actionable  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _good_call(**overrides) -> dict:
    """Minimal CALL contract that passes all F1-F14 quality gates."""
    base = {
        "symbol": "AAPL",
        "option_type": "CALL",
        "strike": 205.0,
        "expiry": "2026-06-20",
        "dte": 30,
        "spot_price": 195.0,
        "iv": 30.0,           # percent (30%)
        "iv_rank": 50.0,
        "delta": 0.45,
        "gamma": 0.02,
        "theta": -0.05,
        "vega": 0.15,
        "mid_price": 3.00,
        "bid": 2.90,
        "ask": 3.10,
        "spread_pct": 5.0,
        "oi": 500,
        "oi_source": "tv",
        "volume": 100,
        "tqs_v2": 75.0,
        "wrs": 0.70,
        "moneyness_pct": 5.0,
        "be_sigma_ratio": 0.40,
        # directional fields (pre-set — skipped enrichment preserves these)
        "signal_composite": 65.0,   # bullish — good for CALL
        "above_sma50": True,        # above SMA50 — good for CALL
        # Note: above_sma200 NOT pre-set (avoid triggering F14 in filter_actionable)
        "support_hold_score": 7.0,
        "support_sma200": 180.0,
        "support_sma50": 185.0,
        "support_trend": "bullish",
    }
    base.update(overrides)
    return base


def _good_put(**overrides) -> dict:
    """Minimal PUT contract that passes all F1-F14 quality gates."""
    base = {
        "symbol": "TSLA",
        "option_type": "PUT",
        "strike": 185.0,
        "expiry": "2026-06-20",
        "dte": 30,
        "spot_price": 195.0,
        "iv": 35.0,
        "iv_rank": 50.0,
        "delta": -0.40,
        "gamma": 0.02,
        "theta": -0.05,
        "vega": 0.15,
        "mid_price": 3.50,
        "bid": 3.40,
        "ask": 3.60,
        "spread_pct": 2.9,
        "oi": 500,
        "oi_source": "tv",
        "volume": 100,
        "tqs_v2": 72.0,
        "wrs": 0.65,
        "moneyness_pct": -5.0,
        "be_sigma_ratio": 0.42,
        # directional fields (pre-set)
        "signal_composite": 40.0,   # bearish — good for PUT
        "above_sma50": False,       # below SMA50 — good for PUT
        # Note: above_sma200 NOT pre-set (avoid triggering F14 in filter_actionable)
        "support_hold_score": 3.0,
        "support_sma200": 210.0,
        "support_sma50": 205.0,
        "support_trend": "bearish",
    }
    base.update(overrides)
    return base


def _run(contracts: List[dict], tmp_path: Path, **kwargs) -> tuple[List[dict], Path]:
    """Call run_actionable with test defaults (fast — no live enrichment)."""
    out = tmp_path / "test_out.html"
    result = run_actionable(
        contracts,
        out_path=out,
        skip_signal=True,    # skip signal enrichment → pre-set values preserved
        skip_support=True,   # skip support enrichment → pre-set values preserved
        **kwargs,
    )
    return result, out


# ─────────────────────────────────────────────────────────────────────────────
# F16 — Signal composite gate (Step 3d)
# ─────────────────────────────────────────────────────────────────────────────

class TestF16SignalGate:
    """F16: signal_composite directional gate."""

    def test_call_low_signal_rejected(self, tmp_path):
        """CALL with signal=35 (< min_call_signal=45) must be rejected."""
        contracts = [_good_call(signal_composite=35.0)]
        result, _ = _run(contracts, tmp_path, min_call_signal=45.0)
        assert result == [], "CALL with low signal should be rejected"

    def test_call_signal_at_threshold_passes(self, tmp_path):
        """CALL with signal=45 (== min_call_signal) must pass."""
        contracts = [_good_call(signal_composite=45.0)]
        result, _ = _run(contracts, tmp_path, min_call_signal=45.0)
        assert len(result) == 1

    def test_call_high_signal_passes(self, tmp_path):
        """CALL with signal=70 must pass."""
        contracts = [_good_call(signal_composite=70.0)]
        result, _ = _run(contracts, tmp_path, min_call_signal=45.0)
        assert len(result) == 1

    def test_put_high_signal_rejected(self, tmp_path):
        """PUT with signal=65 (> max_put_signal=55) must be rejected."""
        contracts = [_good_put(signal_composite=65.0)]
        result, _ = _run(contracts, tmp_path, max_put_signal=55.0)
        assert result == [], "PUT with high signal should be rejected"

    def test_put_signal_at_threshold_passes(self, tmp_path):
        """PUT with signal=55 (== max_put_signal) must pass (boundary inclusive)."""
        contracts = [_good_put(signal_composite=55.0)]
        result, _ = _run(contracts, tmp_path, max_put_signal=55.0)
        assert len(result) == 1

    def test_none_signal_passes_through(self, tmp_path):
        """None signal_composite must NOT be rejected (pass through)."""
        call_no_sig = _good_call()
        call_no_sig["signal_composite"] = None
        put_no_sig = _good_put()
        put_no_sig["signal_composite"] = None
        result, _ = _run([call_no_sig, put_no_sig], tmp_path)
        assert len(result) == 2, "None signal must pass through both CALL and PUT"


# ─────────────────────────────────────────────────────────────────────────────
# F15b — SMA50 alignment gate (Step 3b2)
# ─────────────────────────────────────────────────────────────────────────────

class TestF15bSMA50Gate:
    """F15b: SMA50 alignment directional gate."""

    def test_call_below_sma50_rejected(self, tmp_path):
        """CALL with above_sma50=False (spot below SMA50) must be rejected."""
        contracts = [_good_call(above_sma50=False)]
        result, _ = _run(contracts, tmp_path, require_sma50_confirm=True)
        assert result == [], "CALL below SMA50 should be rejected"

    def test_put_above_sma50_rejected(self, tmp_path):
        """PUT with above_sma50=True (spot above SMA50) must be rejected."""
        contracts = [_good_put(above_sma50=True)]
        result, _ = _run(contracts, tmp_path, require_sma50_confirm=True)
        assert result == [], "PUT above SMA50 should be rejected"

    def test_call_above_sma50_passes(self, tmp_path):
        """CALL with above_sma50=True must pass."""
        contracts = [_good_call(above_sma50=True)]
        result, _ = _run(contracts, tmp_path, require_sma50_confirm=True)
        assert len(result) == 1

    def test_put_below_sma50_passes(self, tmp_path):
        """PUT with above_sma50=False must pass."""
        contracts = [_good_put(above_sma50=False)]
        result, _ = _run(contracts, tmp_path, require_sma50_confirm=True)
        assert len(result) == 1

    def test_none_sma50_passes_through(self, tmp_path):
        """above_sma50=None (no data) must NOT reject either direction."""
        call_no_sma = _good_call(above_sma50=None)
        put_no_sma = _good_put(above_sma50=None)
        result, _ = _run([call_no_sma, put_no_sma], tmp_path, require_sma50_confirm=True)
        assert len(result) == 2, "None above_sma50 must pass through both CALL and PUT"

    def test_sma50_gate_disabled_by_require_false(self, tmp_path):
        """require_sma50_confirm=False must disable the SMA50 gate entirely."""
        call_below = _good_call(above_sma50=False)
        put_above = _good_put(above_sma50=True)
        result, _ = _run([call_below, put_above], tmp_path, require_sma50_confirm=False)
        assert len(result) == 2, "require_sma50_confirm=False must disable SMA50 gate"


# ─────────────────────────────────────────────────────────────────────────────
# skip_directional — backward compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestSkipDirectional:
    """skip_directional=True must bypass ALL new directional gates."""

    def test_skip_directional_bypasses_f16_call(self, tmp_path):
        """CALL with signal=20 passes when skip_directional=True."""
        contracts = [_good_call(signal_composite=20.0)]
        result, _ = _run(contracts, tmp_path, skip_directional=True, min_call_signal=45.0)
        assert len(result) == 1

    def test_skip_directional_bypasses_f16_put(self, tmp_path):
        """PUT with signal=90 passes when skip_directional=True."""
        contracts = [_good_put(signal_composite=90.0)]
        result, _ = _run(contracts, tmp_path, skip_directional=True, max_put_signal=55.0)
        assert len(result) == 1

    def test_skip_directional_bypasses_sma50_call(self, tmp_path):
        """CALL below SMA50 passes when skip_directional=True."""
        contracts = [_good_call(above_sma50=False)]
        result, _ = _run(contracts, tmp_path, skip_directional=True)
        assert len(result) == 1

    def test_skip_directional_bypasses_sma50_put(self, tmp_path):
        """PUT above SMA50 passes when skip_directional=True."""
        contracts = [_good_put(above_sma50=True)]
        result, _ = _run(contracts, tmp_path, skip_directional=True)
        assert len(result) == 1


# ─────────────────────────────────────────────────────────────────────────────
# dir_score and final_rank ranking
# ─────────────────────────────────────────────────────────────────────────────

class TestDirScoreRanking:
    """dir_score and final_rank computed and stored correctly on actionable contracts."""

    def test_dir_score_stored_on_call(self, tmp_path):
        """dir_score and final_rank must be set on each actionable CALL."""
        contracts = [_good_call()]
        result, _ = _run(contracts, tmp_path)
        assert len(result) == 1
        c = result[0]
        assert "dir_score" in c, "dir_score must be set"
        assert "final_rank" in c, "final_rank must be set"
        assert c["dir_score"] >= 0.0
        assert c["final_rank"] >= 0.0

    def test_dir_score_stored_on_put(self, tmp_path):
        """dir_score and final_rank must be set on each actionable PUT."""
        contracts = [_good_put()]
        result, _ = _run(contracts, tmp_path)
        assert len(result) == 1
        c = result[0]
        assert "dir_score" in c
        assert "final_rank" in c

    def test_call_higher_signal_ranks_first(self, tmp_path):
        """CALL with signal=80 must rank above CALL with signal=55 (same tqs_v2)."""
        call_strong = _good_call(signal_composite=80.0, tqs_v2=70.0)
        call_weak = _good_call(signal_composite=55.0, tqs_v2=70.0)
        result, _ = _run([call_weak, call_strong], tmp_path)
        assert len(result) == 2
        assert result[0]["signal_composite"] == 80.0, "Stronger signal CALL should rank first"

    def test_put_lower_signal_ranks_first(self, tmp_path):
        """PUT with signal=25 (more bearish) must rank above PUT with signal=50 (same tqs_v2)."""
        put_strong = _good_put(signal_composite=25.0, tqs_v2=70.0)
        put_weak = _good_put(signal_composite=50.0, tqs_v2=70.0)
        result, _ = _run([put_weak, put_strong], tmp_path)
        assert len(result) == 2
        assert result[0]["signal_composite"] == 25.0, "More bearish PUT should rank first"

    def test_final_rank_equals_half_tqs_plus_half_dir(self, tmp_path):
        """final_rank = tqs_v2 * 0.5 + dir_score * 0.5."""
        contracts = [_good_call()]
        result, _ = _run(contracts, tmp_path)
        c = result[0]
        expected = c["tqs_v2"] * 0.5 + c["dir_score"] * 0.5
        assert abs(c["final_rank"] - expected) < 1e-9

    def test_dir_score_cap_at_100(self, tmp_path):
        """dir_score must be capped at 100 regardless of extreme inputs."""
        # Max possible: signal=100, hold=10, huge SMA200 distance
        extreme_call = _good_call(
            signal_composite=100.0,
            support_hold_score=10.0,
            spot_price=400.0,
            support_sma200=100.0,  # 300% above SMA200
        )
        result, _ = _run([extreme_call], tmp_path)
        assert len(result) == 1
        assert result[0]["dir_score"] <= 100.0, "dir_score must be capped at 100"
