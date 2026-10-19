"""Regression tests for tradingview/backtest/outcome_study.py.

Each test pins one of the five defects found in the signal outcome study:

  1. A verdict could be declared "EDGE" from a handful of distinct symbols
     because only the alert count was gated, not the symbol count.
  2. A single-ticker yfinance batch returns MultiIndex columns, so the flat
     ``df["Close"]`` lookup raised KeyError and the ticker was dropped.
  3. A stale index bar was accepted as the benchmark, distorting excess return.
  4. The report advertised a "score >= 62.5" policy while the code used the
     engine's stored ``actionable`` flag (legacy rows can be actionable below
     62.5).
  5. (report_scheduler) failure notes appended after the 4096-char split pushed
     the first Telegram message over the limit.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradingview.backtest import outcome_study as os_

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)   # Monday 15:00 UTC
HORIZON = os_.HORIZON


# ─── helpers ─────────────────────────────────────────────────────────────────

def _alert(*, symbol="NASDAQ:AAPL", ticker="AAPL", decision=T0, score=70.0,
           actionable=True, version="test-1", benchmark="SPY"):
    return {
        "id": f"{symbol}-{decision.isoformat()}",
        "symbol": symbol, "ticker": ticker, "exchange": symbol.split(":")[0],
        "benchmark": benchmark, "fire_at": decision - timedelta(minutes=5),
        "decision_at": decision, "score": score, "actionable": actionable,
        "version": version,
    }


def _bars(points):
    """points: list of (datetime, price) -> ascending (bar_end, close) list."""
    return sorted(points, key=lambda p: p[0])


def _row(*, symbol, actionable, excess, score=70.0, ret=None, reason=None):
    return {
        "symbol": symbol, "actionable": actionable, "score": score,
        "excess_pct": excess, "return_pct": excess if ret is None else ret,
        "status": "observed_target_first", "reason": reason,
        "exit_offset_h": 0.0, "decision_at": T0, "fire_at": T0,
    }


# ─── defect 2: single-ticker MultiIndex ──────────────────────────────────────

def test_single_ticker_multiindex_batch_returns_prices(monkeypatch):
    """yfinance returns MultiIndex columns even for a 1-ticker batch."""
    idx = pd.DatetimeIndex(
        [datetime(2026, 1, 5, 14, tzinfo=UTC), datetime(2026, 1, 5, 15, tzinfo=UTC)]
    )
    cols = pd.MultiIndex.from_product(
        [["AAPL"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    frame = pd.DataFrame(
        [[1, 1, 1, 100.0, 1], [1, 1, 1, 101.0, 1]], index=idx, columns=cols
    )
    monkeypatch.setattr("yfinance.download", lambda *a, **k: frame)

    bars = os_.fetch_hourly_bars({"AAPL"}, T0 - timedelta(days=1), T0)

    assert "AAPL" in bars, "single-ticker MultiIndex batch was silently dropped"
    assert [round(c, 2) for _, c in bars["AAPL"]] == [100.0, 101.0]


def test_flat_single_column_frame_still_supported(monkeypatch):
    """A flat frame (no group_by) must keep working."""
    idx = pd.DatetimeIndex([datetime(2026, 1, 5, 15, tzinfo=UTC)])
    frame = pd.DataFrame({"Close": [100.0]}, index=idx)
    monkeypatch.setattr("yfinance.download", lambda *a, **k: frame)

    bars = os_.fetch_hourly_bars({"AAPL"}, T0 - timedelta(days=1), T0)

    assert bars["AAPL"][0][1] == 100.0


# ─── defect 3: stale benchmark ───────────────────────────────────────────────

def _stock_bars():
    return _bars([(T0, 100.0), (T0 + HORIZON, 110.0)])


def test_stale_benchmark_is_rejected():
    """An index bar 5 days before entry must not be used as the benchmark."""
    stale = _bars([(T0 - timedelta(days=5), 100.0), (T0 + HORIZON, 105.0)])
    result = os_.evaluate_alert(_alert(), _stock_bars(), stale, T0 + HORIZON + timedelta(hours=1))

    assert result["return_pct"] == pytest.approx(10.0)
    assert result["excess_pct"] is None
    assert result["reason"] == "no fresh benchmark at entry/exit"


def test_fresh_benchmark_is_accepted():
    fresh = _bars([(T0, 100.0), (T0 + HORIZON, 105.0)])
    result = os_.evaluate_alert(_alert(), _stock_bars(), fresh, T0 + HORIZON + timedelta(hours=1))

    assert result["excess_pct"] == pytest.approx(5.0)   # 10% stock − 5% index
    assert result["reason"] is None


def test_stale_benchmark_counted_in_funnel():
    stale = _bars([(T0 - timedelta(days=5), 100.0), (T0 + HORIZON, 105.0)])
    row = os_.evaluate_alert(_alert(), _stock_bars(), stale, T0 + HORIZON + timedelta(hours=1))
    stats = os_.analyse_version([row])

    assert stats["funnel"]["no_fresh_benchmark"] == 1
    assert stats["funnel"]["with_benchmark"] == 0


# ─── defect 1: verdict must gate on distinct symbols ─────────────────────────

def test_verdict_requires_minimum_distinct_symbols():
    """30 alerts on one symbol per arm is n=1, not n=30."""
    rows = ([_row(symbol="NASDAQ:AAA", actionable=True, excess=5.0)] * 30
            + [_row(symbol="NASDAQ:BBB", actionable=False, excess=-5.0)] * 30)
    stats = os_.analyse_version(rows)

    assert stats["live_rule"]["arms"] == {"actionable": 30, "rejected": 30}
    assert stats["live_rule"]["symbols"] == {"actionable": 1, "rejected": 1}
    assert stats["live_rule"]["verdict"] != "EDGE"
    assert stats["live_rule"]["verdict"] == "UNDERPOWERED"


def test_verdict_edge_when_both_gates_pass():
    rows = []
    for i in range(15):                       # 15 symbols x 3 alerts = 45 per arm
        for _ in range(3):
            rows.append(_row(symbol=f"NASDAQ:A{i}", actionable=True, excess=5.0))
            rows.append(_row(symbol=f"NASDAQ:B{i}", actionable=False, excess=-5.0))
    stats = os_.analyse_version(rows)

    assert stats["live_rule"]["symbols"]["actionable"] >= os_.MIN_SYMBOLS
    assert stats["live_rule"]["symbols"]["rejected"] >= os_.MIN_SYMBOLS
    assert stats["live_rule"]["verdict"] == "EDGE"


def test_verdict_insufficient_when_one_arm_empty():
    rows = [_row(symbol=f"NASDAQ:A{i}", actionable=True, excess=5.0) for i in range(40)]
    assert os_.analyse_version(rows)["live_rule"]["verdict"] == "INSUFFICIENT DATA"


def test_verdict_direct_gates():
    ci = (1.0, 2.0)
    assert os_._verdict(ci, (100, 100), (1, 1)) == "UNDERPOWERED"
    assert os_._verdict(ci, (5, 100), (20, 20)) == "UNDERPOWERED"
    assert os_._verdict(ci, (100, 100), (20, 20)) == "EDGE"
    assert os_._verdict((-2.0, -1.0), (100, 100), (20, 20)) == "INVERTED"
    assert os_._verdict((-1.0, 1.0), (100, 100), (20, 20)) == "NO DETECTABLE EDGE"
    assert os_._verdict(None, (100, 100), (20, 20)) == "INSUFFICIENT DATA"


# ─── defect 4: report must describe the stored flag, not a score cutoff ──────

def test_report_describes_stored_actionable_flag():
    rows = [_row(symbol=f"NASDAQ:A{i}", actionable=True, excess=5.0) for i in range(12)]
    rows += [_row(symbol=f"NASDAQ:B{i}", actionable=False, excess=-5.0) for i in range(12)]
    html = os_.build_html({"test-1": os_.analyse_version(rows)}, T0)

    assert "stored <code>actionable</code> flag" in html
    assert "score ≥ 62.5 and no veto" not in html
    assert "re-derived score cutoff" in html


def test_report_surfaces_min_actionable_score():
    rows = [_row(symbol=f"NASDAQ:A{i}", actionable=True, excess=5.0, score=50.0)
            for i in range(12)]
    rows += [_row(symbol=f"NASDAQ:B{i}", actionable=False, excess=-5.0, score=40.0)
             for i in range(12)]
    stats = os_.analyse_version(rows)

    assert stats["funnel"]["min_actionable_score"] == 50.0
    assert "Lowest actionable score" in os_.build_html({"test-1": stats}, T0)


# ─── report integrity ────────────────────────────────────────────────────────

def test_report_is_wellformed_and_has_viewport():
    rows = [_row(symbol=f"NASDAQ:A{i}", actionable=True, excess=5.0) for i in range(12)]
    html = os_.build_html({"test-1": os_.analyse_version(rows)}, T0)

    assert html.startswith("<!doctype html>")
    assert 'lang="en"' in html
    assert 'name="viewport"' in html
    assert html.rstrip().endswith("</html>")
