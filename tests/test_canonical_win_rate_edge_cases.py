"""
tests/test_canonical_win_rate_edge_cases.py
============================================
Edge-case tests for database/queries.py canonical_win_rate().

Risk: HIGH
The existing tests in test_audit_fixes.py cover the happy path and empty DB.
Missing coverage:
  - outcome='OPEN' must be excluded from total_closed
  - outcome IS NULL must be excluded from total_closed
  - return_7d=3.0 is a win (>= threshold); 2.99 is not
  - MAX(COUNT(*), 1) division guard prevents ZeroDivisionError

Run:
    python -m pytest tests/test_canonical_win_rate_edge_cases.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _conn(rows: list[tuple]) -> sqlite3.Connection:
    """Build an in-memory alerts table with (return_7d, outcome) rows."""
    c = sqlite3.connect(":memory:")
    c.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            return_7d REAL,
            outcome   TEXT
        )
    """)
    c.executemany("INSERT INTO alerts VALUES (NULL, ?, ?)", rows)
    c.commit()
    return c


# ─── Outcome filtering ────────────────────────────────────────────────────────

class TestOutcomeFiltering:

    def test_open_trades_excluded(self):
        """outcome='OPEN' must NOT count toward total_closed."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "WIN"), (5.0, "OPEN"), (-2.0, "LOSS")])
        r = canonical_win_rate(c)
        assert r["total_closed"] == 2, (
            f"OPEN trade must be excluded; expected 2 closed, got {r['total_closed']}"
        )

    def test_null_outcome_excluded(self):
        """outcome IS NULL must NOT count toward total_closed."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "WIN"), (5.0, None), (-2.0, "LOSS")])
        r = canonical_win_rate(c)
        assert r["total_closed"] == 2, (
            f"NULL outcome must be excluded; expected 2 closed, got {r['total_closed']}"
        )

    def test_mix_open_null_closed(self):
        """With OPEN, NULL, WIN, LOSS — only WIN+LOSS are closed."""
        from database.queries import canonical_win_rate
        c = _conn([
            (10.0, "WIN"), (10.0, "OPEN"), (10.0, None), (-2.0, "LOSS"), (-2.0, "OPEN"),
        ])
        r = canonical_win_rate(c)
        assert r["total_closed"] == 2   # WIN + LOSS only

    def test_all_open_returns_zero_closed(self):
        """If every trade is OPEN, total_closed=0."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "OPEN"), (8.0, "OPEN")])
        r = canonical_win_rate(c)
        assert r["total_closed"] == 0
        assert r["wins"] == 0
        assert r["win_rate_pct"] == 0.0


# ─── Win threshold boundary ───────────────────────────────────────────────────

class TestWinThresholdExact:

    def test_win_threshold_constant_is_3(self):
        """WIN_THRESHOLD_PCT must be exactly 3.0."""
        from database.queries import WIN_THRESHOLD_PCT
        assert WIN_THRESHOLD_PCT == 3.0, (
            f"WIN_THRESHOLD_PCT changed to {WIN_THRESHOLD_PCT}; "
            "update these tests and all callers if intentional"
        )

    def test_return_exactly_3_is_a_win(self):
        """return_7d=3.0 (== WIN_THRESHOLD_PCT) must be counted as a win."""
        from database.queries import canonical_win_rate
        c = _conn([(3.0, "CLOSED")])
        r = canonical_win_rate(c)
        assert r["wins"] == 1, "return_7d=3.0 should be a win (>= threshold)"

    def test_return_just_below_threshold_is_not_win(self):
        """return_7d=2.99 (< WIN_THRESHOLD_PCT) must NOT be a win."""
        from database.queries import canonical_win_rate
        c = _conn([(2.99, "CLOSED")])
        r = canonical_win_rate(c)
        assert r["wins"] == 0, "return_7d=2.99 should NOT be a win (< 3.0 threshold)"

    def test_zero_return_is_not_win(self):
        from database.queries import canonical_win_rate
        c = _conn([(0.0, "CLOSED")])
        r = canonical_win_rate(c)
        assert r["wins"] == 0

    def test_negative_return_is_not_win(self):
        from database.queries import canonical_win_rate
        c = _conn([(-5.0, "CLOSED"), (10.0, "CLOSED")])
        r = canonical_win_rate(c)
        assert r["wins"] == 1
        assert r["win_rate_pct"] == 50.0


# ─── Division guard ───────────────────────────────────────────────────────────

class TestDivisionGuard:
    """MAX(COUNT(*), 1) prevents ZeroDivisionError on empty/all-OPEN tables."""

    def test_empty_table_no_exception(self):
        """Empty table must return 0.0, not raise ZeroDivisionError."""
        from database.queries import canonical_win_rate
        c = sqlite3.connect(":memory:")
        c.execute("""
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY, return_7d REAL, outcome TEXT
            )
        """)
        c.commit()
        r = canonical_win_rate(c)
        assert r["win_rate_pct"] == 0.0
        assert r["total_closed"] == 0
        assert r["wins"] == 0

    def test_all_open_no_exception(self):
        """All OPEN trades: no closed → win_rate_pct=0.0 (no ZeroDivisionError)."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "OPEN"), (3.0, "OPEN")])
        r = canonical_win_rate(c)
        assert r["win_rate_pct"] == 0.0


# ─── Win rate accuracy ────────────────────────────────────────────────────────

class TestWinRateAccuracy:

    def test_100_percent(self):
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "W"), (7.0, "W"), (10.0, "W")])
        r = canonical_win_rate(c)
        assert r["win_rate_pct"] == 100.0
        assert r["wins"] == r["total_closed"] == 3

    def test_33_percent(self):
        """1 win, 2 losses → 33.3%."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "W"), (-2.0, "L"), (-3.0, "L")])
        r = canonical_win_rate(c)
        assert r["total_closed"] == 3
        assert r["wins"] == 1
        assert abs(r["win_rate_pct"] - 33.3) < 0.1

    def test_win_rate_rounded_to_one_decimal(self):
        """win_rate_pct is rounded to 1 decimal place."""
        from database.queries import canonical_win_rate
        c = _conn([(5.0, "W")] * 2 + [(-2.0, "L")] * 3)  # 2/5 = 40.0%
        r = canonical_win_rate(c)
        assert r["win_rate_pct"] == 40.0


# ─── canonical_pnl companion ─────────────────────────────────────────────────

class TestCanonicalPnl:

    def test_sums_closed_return_7d_only(self):
        """canonical_pnl must sum return_7d for closed trades, exclude OPEN/NULL."""
        from database.queries import canonical_pnl
        c = _conn([
            (5.0, "WIN"), (3.0, "LOSS"), (10.0, "OPEN"), (-2.0, None),
        ])
        result = canonical_pnl(c)
        assert abs(result - 8.0) < 0.01, (
            f"Expected 5.0+3.0=8.0 (OPEN/NULL excluded), got {result}"
        )

    def test_empty_table_returns_zero(self):
        from database.queries import canonical_pnl
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, return_7d REAL, outcome TEXT)")
        c.commit()
        assert canonical_pnl(c) == 0.0

    def test_all_open_returns_zero(self):
        from database.queries import canonical_pnl
        c = _conn([(5.0, "OPEN"), (8.0, "OPEN")])
        assert canonical_pnl(c) == 0.0
