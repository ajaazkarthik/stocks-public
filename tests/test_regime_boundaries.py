"""
tests/test_regime_boundaries.py
================================
Exact VIX threshold boundary tests for regime_detector.py.

Risk: CRITICAL
The existing test_regime_wiring.py only checks that VIX=10 differs from
VIX=40 (two very different values). It does NOT test the four exact threshold
crossings where the regime name changes. A single wrong comparison operator
(>= instead of >) silently mis-classifies every signal scored near a boundary.

Run:
    python -m pytest tests/test_regime_boundaries.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))


# ─── Reset the module cache between tests ────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_vix_cache():
    """Clear _last_vix before every test to prevent cross-test contamination."""
    import tradingview.regime_detector as rd
    rd._last_vix = None
    yield
    rd._last_vix = None


# ─── Exact boundary crossings ────────────────────────────────────────────────

class TestClassifyRegimeBoundaries:
    """
    Documented boundaries (from regime_detector.py):
        BULL_QUIET  : VIX < 15
        NORMAL      : 15 ≤ VIX < 25
        ELEVATED    : 25 ≤ VIX < 35
        CRISIS      : VIX ≥ 35
    """

    def _c(self, vix):
        from tradingview.regime_detector import classify_regime
        return classify_regime(vix)

    # ── BULL_QUIET / NORMAL boundary at 15.0 ─────────────────────────────────

    def test_vix_14_99_is_bull_quiet(self):
        assert self._c(14.99) == "BULL_QUIET"

    def test_vix_exactly_15_is_normal(self):
        """VIX=15.0 must be NORMAL, not BULL_QUIET (boundary is strict `< 15`)."""
        assert self._c(15.0) == "NORMAL", (
            "VIX=15.0 should be NORMAL. Boundary is `vix < 15.0`, "
            "so 15.0 itself is the first NORMAL value."
        )

    def test_vix_15_01_is_normal(self):
        assert self._c(15.01) == "NORMAL"

    # ── NORMAL / ELEVATED boundary at 25.0 ───────────────────────────────────

    def test_vix_24_99_is_normal(self):
        assert self._c(24.99) == "NORMAL"

    def test_vix_exactly_25_is_elevated(self):
        """VIX=25.0 must be ELEVATED, not NORMAL."""
        assert self._c(25.0) == "ELEVATED", (
            "VIX=25.0 should be ELEVATED. Boundary is `vix < 25.0`."
        )

    def test_vix_25_01_is_elevated(self):
        assert self._c(25.01) == "ELEVATED"

    # ── ELEVATED / CRISIS boundary at 35.0 ───────────────────────────────────

    def test_vix_34_99_is_elevated(self):
        assert self._c(34.99) == "ELEVATED"

    def test_vix_exactly_35_is_crisis(self):
        """VIX=35.0 must be CRISIS, not ELEVATED."""
        assert self._c(35.0) == "CRISIS", (
            "VIX=35.0 should be CRISIS. Boundary is `vix < 35.0`."
        )

    def test_vix_35_01_is_crisis(self):
        assert self._c(35.01) == "CRISIS"

    # ── Edge values ───────────────────────────────────────────────────────────

    def test_vix_zero_is_bull_quiet(self):
        assert self._c(0.0) == "BULL_QUIET"

    def test_vix_very_large_is_crisis(self):
        assert self._c(999.0) == "CRISIS"


# ─── Weight vector invariants ────────────────────────────────────────────────

class TestWeightVectorInvariants:
    """Weight vectors must be valid and have correct directional semantics."""

    @pytest.mark.parametrize("vix", [1.0, 14.99, 15.0, 20.0, 24.99, 25.0, 30.0, 34.99, 35.0, 55.0])
    def test_weights_sum_to_one(self, vix):
        """L1+L2+L3+L4 weights must always sum to exactly 1.0."""
        from tradingview.regime_detector import get_regime_weights
        w = get_regime_weights(vix)
        total = sum(w.values())
        assert abs(total - 1.0) < 1e-9, f"VIX={vix}: weights sum to {total}, not 1.0"

    @pytest.mark.parametrize("vix", [1.0, 15.0, 25.0, 35.0, 55.0])
    def test_weights_have_exactly_four_keys(self, vix):
        """Every regime must have exactly l1, l2, l3, l4."""
        from tradingview.regime_detector import get_regime_weights
        w = get_regime_weights(vix)
        assert set(w.keys()) == {"l1", "l2", "l3", "l4"}, (
            f"VIX={vix}: expected keys {{l1,l2,l3,l4}}, got {set(w.keys())}"
        )

    @pytest.mark.parametrize("vix", [1.0, 15.0, 25.0, 35.0, 55.0])
    def test_all_weights_positive(self, vix):
        """No weight may be ≤ 0 (every layer must have some contribution)."""
        from tradingview.regime_detector import get_regime_weights
        w = get_regime_weights(vix)
        for k, v in w.items():
            assert v > 0.0, f"VIX={vix}: {k}={v} — all weights must be > 0"

    def test_crisis_reduces_l1_vs_normal(self):
        """CRISIS must have LOWER L1 (technicals) weight than NORMAL."""
        from tradingview.regime_detector import get_regime_weights
        w_normal = get_regime_weights(20.0)
        w_crisis = get_regime_weights(40.0)
        assert w_crisis["l1"] < w_normal["l1"], (
            f"CRISIS l1={w_crisis['l1']} must be < NORMAL l1={w_normal['l1']}"
        )

    def test_crisis_increases_l4_vs_normal(self):
        """CRISIS must have HIGHER L4 (market internals) weight than NORMAL."""
        from tradingview.regime_detector import get_regime_weights
        w_normal = get_regime_weights(20.0)
        w_crisis = get_regime_weights(40.0)
        assert w_crisis["l4"] > w_normal["l4"], (
            f"CRISIS l4={w_crisis['l4']} must be > NORMAL l4={w_normal['l4']}"
        )

    def test_bull_quiet_has_highest_l1(self):
        """BULL_QUIET must have the highest L1 weight across all regimes."""
        from tradingview.regime_detector import get_regime_weights
        w_bull   = get_regime_weights(10.0)
        w_normal = get_regime_weights(20.0)
        w_elev   = get_regime_weights(30.0)
        w_crisis = get_regime_weights(40.0)
        assert w_bull["l1"] >= w_normal["l1"]
        assert w_bull["l1"] >= w_elev["l1"]
        assert w_bull["l1"] >= w_crisis["l1"]


# ─── None / cache behaviour ───────────────────────────────────────────────────

class TestNoneCacheBehaviour:
    """classify_regime(None) uses cached _last_vix or falls back to NORMAL."""

    def test_none_without_prior_call_returns_normal(self):
        """No prior call → _last_vix=None → classify_regime(None) = NORMAL."""
        import tradingview.regime_detector as rd
        assert rd._last_vix is None, "Fixture should have reset _last_vix"
        result = rd.classify_regime(None)
        assert result == "NORMAL", (
            "With no cached VIX, None must return NORMAL (safe default)"
        )

    def test_none_uses_cached_vix_crisis(self):
        """After seeding _last_vix=40 via a real call, None → CRISIS."""
        import tradingview.regime_detector as rd
        rd.classify_regime(40.0)   # seeds _last_vix = 40.0
        result = rd.classify_regime(None)
        assert result == "CRISIS"

    def test_none_uses_cached_vix_bull_quiet(self):
        """After seeding _last_vix=10, None → BULL_QUIET."""
        import tradingview.regime_detector as rd
        rd.classify_regime(10.0)
        result = rd.classify_regime(None)
        assert result == "BULL_QUIET"

    def test_real_call_after_none_updates_cache(self):
        """Passing a real VIX after None must update the cache."""
        import tradingview.regime_detector as rd
        rd.classify_regime(None)          # uses default (None cache)
        rd.classify_regime(40.0)          # updates cache to 40
        assert rd._last_vix == 40.0


# ─── detect_regime return shape ───────────────────────────────────────────────

class TestDetectRegimeReturnShape:

    @pytest.mark.parametrize("vix,expected", [
        (10.0, "BULL_QUIET"),
        (20.0, "NORMAL"),
        (30.0, "ELEVATED"),
        (40.0, "CRISIS"),
    ])
    def test_regime_string_matches_classify(self, vix, expected):
        import tradingview.regime_detector as rd
        result = rd.detect_regime(vix)
        assert result["regime"] == expected
        assert result["vix"] == vix
        assert set(result.keys()) == {"regime", "vix", "weights", "label"}

    def test_label_is_nonempty_string(self):
        import tradingview.regime_detector as rd
        for vix in [10.0, 20.0, 30.0, 40.0]:
            result = rd.detect_regime(vix)
            assert isinstance(result["label"], str) and result["label"], (
                f"VIX={vix}: label must be a non-empty string"
            )
