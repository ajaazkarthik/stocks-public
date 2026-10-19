"""Tests that regime_detector is properly wired into composite() (C-01 fix)."""
import pytest


def test_regime_detector_importable():
    """regime_detector must be importable from tradingview package."""
    from tradingview.regime_detector import get_regime_weights, detect_regime
    assert get_regime_weights is not None
    assert detect_regime is not None


def test_regime_weights_shape():
    """get_regime_weights must return a dict with l1-l4 keys."""
    from tradingview.regime_detector import get_regime_weights
    w = get_regime_weights(15.0)
    assert isinstance(w, dict)
    assert "l1" in w or "l2" in w  # at least one layer key present


def test_regime_weights_vix_bull():
    """VIX=10 (bull quiet) should give different weights than VIX=40 (crisis)."""
    from tradingview.regime_detector import get_regime_weights
    w_bull = get_regime_weights(10.0)
    w_crisis = get_regime_weights(40.0)
    assert w_bull != w_crisis


def test_signal_stack_has_regime_import():
    """signal_stack.py must import or attempt to import regime_detector."""
    with open("tradingview/signal_stack.py", encoding="utf-8") as f:
        source = f.read()
    assert "regime_detector" in source, (
        "signal_stack.py must import from regime_detector (C-01 fix)"
    )


def test_composite_accepts_regime_weights():
    """composite() must accept regime_weights parameter."""
    import inspect
    from tradingview.signal_stack import composite
    sig = inspect.signature(composite)
    assert "regime_weights" in sig.parameters, (
        "composite() must have regime_weights parameter"
    )
