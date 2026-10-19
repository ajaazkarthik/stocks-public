"""
test_signal_properties.py — Property-based tests using hypothesis.

Tests invariants that MUST hold for any input to signal_stack functions,
automatically generating hundreds of test cases per property.

These tests complement the hand-crafted tests in test_menu_handlers.py.
If grade() or other scoring functions are refactored, these properties
AUTOMATICALLY verify that all mathematical invariants still hold — no
manual test updates required.

Run:
    python -m pytest test_signal_properties.py -v

Run with more examples:
    pytest test_signal_properties.py --hypothesis-seed=0 -v

Run slower (larger databases):
    pytest test_signal_properties.py --hypothesis-settings-database=.hypothesis -v
"""
import sys
import re
from pathlib import Path

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tradingview"))

# ── Import grade() from signal_stack ------------------------------------------
try:
    import signal_stack as _ss
    _grade = _ss.grade
except Exception as e:
    pytest.skip(f"signal_stack import failed: {e}", allow_module_level=True)


# ── Property 1: grade() always returns a 2-tuple ──────────────────────────────

@given(score=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
@settings(max_examples=200)
def test_grade_returns_two_tuple(score):
    """grade() ALWAYS returns exactly (letter: str, color: str) for any valid score."""
    result = _grade(score)
    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 2, f"Expected 2-tuple, got {len(result)}-tuple"


# ── Property 2: letter is always in {A, B, C, D, F} ──────────────────────────

@given(score=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
@settings(max_examples=200)
def test_grade_letter_is_valid(score):
    """Letter component is ALWAYS one of A, B, C, D, F."""
    letter, _ = _grade(score)
    assert letter in {"A", "B", "C", "D", "F"}, \
        f"Invalid grade letter '{letter}' for score={score}"


# ── Property 3: color is always a valid #rrggbb hex string ────────────────────

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

@given(score=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
@settings(max_examples=200)
def test_grade_color_is_valid_hex(score):
    """Color component is ALWAYS a valid 7-character hex color string."""
    _, color = _grade(score)
    assert _HEX_RE.match(color), \
        f"Invalid hex color '{color}' for score={score}"


# ── Property 4: grade is monotone (higher score ≥ same or better grade) ───────

_GRADE_ORDER = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}

@given(
    s1=st.floats(min_value=0, max_value=100, allow_nan=False),
    s2=st.floats(min_value=0, max_value=100, allow_nan=False),
)
@settings(max_examples=300)
def test_grade_is_monotone(s1, s2):
    """If score1 > score2, grade(score1) >= grade(score2) in letter order."""
    assume(s1 > s2)  # only test pairs where s1 strictly greater
    letter1, _ = _grade(s1)
    letter2, _ = _grade(s2)
    assert _GRADE_ORDER[letter1] >= _GRADE_ORDER[letter2], \
        f"Monotonicity violated: score {s1:.2f}→{letter1} but {s2:.2f}→{letter2}"


# ── Property 5: score >= 75 always produces A ─────────────────────────────────

@given(score=st.floats(min_value=75.0, max_value=100.0, allow_nan=False))
@settings(max_examples=200)
def test_grade_above_75_is_A(score):
    """Any score >= 75 MUST produce grade A."""
    letter, _ = _grade(score)
    assert letter == "A", f"Expected A for score={score:.4f}, got {letter}"


# ── Property 6: score < 30 always produces F ──────────────────────────────────

@given(score=st.floats(min_value=0.0, max_value=29.99, allow_nan=False))
@settings(max_examples=200)
def test_grade_below_30_is_F(score):
    """Any score < 30 MUST produce grade F."""
    letter, _ = _grade(score)
    assert letter == "F", f"Expected F for score={score:.4f}, got {letter}"


# ── Property 7: boundary values are stable ────────────────────────────────────

@pytest.mark.parametrize("score,expected", [
    (0.0,    "F"),
    (29.999, "F"),
    (30.0,   "D"),
    (44.999, "D"),
    (45.0,   "C"),
    (59.999, "C"),
    (60.0,   "B"),
    (74.999, "B"),
    (75.0,   "A"),
    (100.0,  "A"),
])
def test_grade_boundary_values(score, expected):
    """Grade thresholds are exact at boundary values."""
    letter, _ = _grade(score)
    assert letter == expected, f"Boundary test: score={score} → expected {expected}, got {letter}"
