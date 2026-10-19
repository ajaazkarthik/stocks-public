"""Tests for F5: Short Squeeze badge in signal_stack.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TV = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))

try:
    from signal_stack import detect_short_squeeze, _short_squeeze_html_badge, SHORT_SQUEEZE_CSS
except ImportError:
    detect_short_squeeze = None
    _short_squeeze_html_badge = None
    SHORT_SQUEEZE_CSS = ""
    pytestmark = pytest.mark.skip(reason="short_squeeze feature not yet implemented in signal_stack.py")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. detect_short_squeeze
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectShortSqueeze:
    """detect_short_squeeze() should detect squeeze conditions."""

    def test_high_short_ratio_and_volume_and_rsi(self):
        """All criteria met → active squeeze."""
        result = detect_short_squeeze(
            {"short_ratio": 10.0, "short_pct_float": 15.0},
            {"relative_volume": 2.5, "rsi": 60},
        )
        assert result["is_squeeze"] is True
        assert result["confidence"] >= 50
        assert len(result["triggers"]) >= 2
        assert "SQUEEZE" in result["label"].upper() or "Squeeze" in result["label"]

    def test_no_short_data_returns_no_squeeze(self):
        """No short interest data → no squeeze."""
        result = detect_short_squeeze({}, {"relative_volume": 3.0, "rsi": 65})
        assert result["is_squeeze"] is False
        assert result["confidence"] == 0
        assert result["triggers"] == []

    def test_low_short_ratio_no_squeeze(self):
        """Low short ratio → no squeeze triggers."""
        result = detect_short_squeeze(
            {"short_ratio": 2.0, "short_pct_float": 3.0},
            {"relative_volume": 1.5, "rsi": 50},
        )
        assert result["is_squeeze"] is False

    def test_high_short_ratio_alone(self):
        """High short ratio but no volume surge → may not be active squeeze."""
        result = detect_short_squeeze(
            {"short_ratio": 10.0},
            {"relative_volume": 0.8, "rsi": 50},
        )
        # Should have triggers from short ratio but confidence may not reach 50
        assert result["confidence"] > 0
        assert len(result["triggers"]) >= 1

    def test_high_pct_float_triggers(self):
        """High short % float should trigger."""
        result = detect_short_squeeze(
            {"short_pct_float": 12.0},
            {"relative_volume": 2.0, "rsi": 55},
        )
        assert result["confidence"] >= 30
        assert any("float" in t.lower() for t in result["triggers"])

    def test_extreme_short_float_bonus(self):
        """Short float ≥20% gets extra confidence."""
        result = detect_short_squeeze(
            {"short_ratio": 15.0, "short_pct_float": 25.0},
            {"relative_volume": 3.0, "rsi": 60},
        )
        assert result["confidence"] >= 70
        assert result["is_squeeze"] is True

    def test_confidence_capped_at_100(self):
        """Confidence should never exceed 100."""
        result = detect_short_squeeze(
            {"short_ratio": 20.0, "short_pct_float": 30.0},
            {"relative_volume": 5.0, "rsi": 60},
        )
        assert result["confidence"] <= 100

    def test_rsi_overbought_reduces_confidence(self):
        """RSI > 80 gives less confidence than 40-80 zone."""
        normal = detect_short_squeeze(
            {"short_ratio": 10.0}, {"relative_volume": 2.0, "rsi": 60},
        )
        overbought = detect_short_squeeze(
            {"short_ratio": 10.0}, {"relative_volume": 2.0, "rsi": 85},
        )
        assert normal["confidence"] >= overbought["confidence"]

    def test_none_short_ratio_with_pct_float(self):
        """Only pct_float available should still detect."""
        result = detect_short_squeeze(
            {"short_ratio": None, "short_pct_float": 15.0},
            {"relative_volume": 2.0, "rsi": 55},
        )
        assert result["confidence"] > 0

    def test_returns_required_keys(self):
        """Result dict must have all expected keys."""
        result = detect_short_squeeze({}, {})
        for key in ("is_squeeze", "confidence", "triggers", "label"):
            assert key in result

    def test_elevated_short_ratio_not_high(self):
        """Short ratio 5-7 → elevated but not high threshold."""
        result = detect_short_squeeze(
            {"short_ratio": 6.0},
            {"relative_volume": 2.0, "rsi": 60},
        )
        assert result["confidence"] > 0
        assert any("elevated" in t.lower() for t in result["triggers"])

    def test_label_alert_vs_potential(self):
        """High confidence = ALERT, medium = Potential."""
        high = detect_short_squeeze(
            {"short_ratio": 15.0, "short_pct_float": 22.0},
            {"relative_volume": 3.0, "rsi": 60},
        )
        assert "ALERT" in high["label"] or high["confidence"] < 70

    def test_relative_volume_key_variant(self):
        """Should work with relative_volume_10d_calc key too."""
        result = detect_short_squeeze(
            {"short_ratio": 10.0},
            {"relative_volume_10d_calc": 2.5, "rsi": 55},
        )
        # The function checks relative_volume first, then relative_volume_10d_calc
        assert result["confidence"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _short_squeeze_html_badge
# ═══════════════════════════════════════════════════════════════════════════════

class TestShortSqueezeHtmlBadge:
    """_short_squeeze_html_badge() rendering tests."""

    def test_active_squeeze_renders(self):
        data = {"is_squeeze": True, "confidence": 75, "label": "⚡ SHORT SQUEEZE ALERT",
                "triggers": ["Short ratio 10d", "Volume 2.5×"]}
        html = _short_squeeze_html_badge(data)
        assert "squeeze-badge" in html
        assert "SHORT SQUEEZE" in html
        assert "75%" in html

    def test_no_squeeze_empty(self):
        html = _short_squeeze_html_badge({})
        assert html == ""

    def test_none_input_empty(self):
        html = _short_squeeze_html_badge(None)
        assert html == ""

    def test_warning_level_renders(self):
        data = {"is_squeeze": False, "confidence": 35, "label": "🔍 High Short Interest",
                "triggers": ["Short ratio 8d"]}
        html = _short_squeeze_html_badge(data)
        assert "squeeze-badge" in html
        assert "High Short Interest" in html

    def test_no_label_empty(self):
        data = {"is_squeeze": False, "confidence": 0, "label": "", "triggers": []}
        html = _short_squeeze_html_badge(data)
        assert html == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CSS constant
# ═══════════════════════════════════════════════════════════════════════════════

class TestShortSqueezeCSS:
    def test_css_defined(self):
        assert SHORT_SQUEEZE_CSS
        assert "squeeze-badge" in SHORT_SQUEEZE_CSS

    def test_css_has_gradient(self):
        assert "linear-gradient" in SHORT_SQUEEZE_CSS


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
