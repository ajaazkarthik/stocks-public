"""Failing-before / passing-after evidence for the stock query defects.

Two pre-fix behaviours are reconstructed from the pre-fix Git revision and
demonstrated against a temporary database:

* **Issue 2** — ``created_at > datetime('now', '-24 hours')`` compares ISO ``T``
  timestamps lexically against SQLite's space-separated datetime, so rows older
  than 24 hours stay included.
* **Issue 1** — ``GROUP BY tv_symbol`` with ``MAX(composite_score)`` resurrects
  an old high-scoring eligible row even after a newer ineligible assessment.

The pre-fix source is read from Git and executed in an isolated namespace. The
user's working tree is never reset.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingview.scheduler import stock_results

ROOT = Path(__file__).resolve().parents[1]
# Commit be086921 is the known pre-fix source; keep this baseline stable as HEAD advances.
PREFIX_REVISION = "be08692174367c1a03d5eacd21ff9e3eeefaf681"
FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)


def _prefix_function_source(name: str) -> str:
    blob = subprocess.run(
        ["git", "show", f"{PREFIX_REVISION}:report_scheduler.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    ).stdout
    lines = blob.split("\n")
    start = next(
        index for index, line in enumerate(lines) if line.startswith(f"def {name}(")
    )
    end = start + 1
    while end < len(lines) and not lines[end].startswith(("def ", "async def ", "# ═")):
        end += 1
    return "\n".join(lines[start:end])


def _load_prefix_extract_stock_alerts(db_path: Path):
    """Execute the pre-fix ``_extract_stock_alerts`` in an isolated namespace."""
    import logging

    namespace: dict = {
        "sqlite3": SimpleNamespace(
            connect=_frozen_connect(db_path, "2026-10-13 06:42:19"), Row=sqlite3.Row
        ),
        "logger": logging.getLogger("prefix"),
        "_DB_PATH": db_path,
    }
    exec(
        compile(
            _prefix_function_source("_parse_exchange_symbol")
            + "\n\n"
            + _prefix_function_source("_extract_stock_alerts"),
            "prefix-report_scheduler",
            "exec",
        ),
        namespace,
    )
    return namespace["_extract_stock_alerts"]


def _alerts_db(tmp_path: Path, rows: list[tuple]) -> Path:
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE alerts (tv_symbol TEXT, composite_score REAL, signal TEXT, "
        "fire_time TEXT, resolution TEXT, created_at TEXT, actionable INTEGER)"
    )
    conn.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db_path


# ══════════════════════════════════════════════════════════════════════════════
# Issue 2 — the 24-hour boundary
# ══════════════════════════════════════════════════════════════════════════════

def test_prefix_query_includes_a_row_older_than_24_hours(tmp_path):
    """FAILING-BEFORE: the observed ~29h44m old row was still selected."""
    db_path = _alerts_db(tmp_path, [
        ("ASX:CDA", 80.0, "BUY", "2026-10-12T00:58:00Z", "60",
         "2026-10-12T00:58:00.786494+00:00", 1),
    ])
    prefix_extract = _load_prefix_extract_stock_alerts(db_path)
    alerts = prefix_extract(hours=24)
    assert [alert["symbol"] for alert in alerts] == ["CDA"], (
        "pre-fix query unexpectedly excluded the stale row"
    )


def test_new_query_excludes_the_same_stale_row(tmp_path):
    """PASSING-AFTER: the normalized comparison excludes it."""
    db_path = _alerts_db(tmp_path, [
        ("ASX:CDA", 80.0, "BUY", "2026-10-12T00:58:00Z", "60",
         "2026-10-12T00:58:00.786494+00:00", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.stocks == []


def _frozen_connect(db_path: Path, frozen_now: str):
    """Freeze SQLite's built-in ``datetime('now', ...)`` for prefix connections."""
    real_connect = sqlite3.connect

    def _connect(database, *args, **kwargs):
        conn = real_connect(database, *args, **kwargs)

        def _datetime(*values):
            # Evaluate through an unmodified SQLite connection so the built-in
            # datetime implementation handles modifiers exactly as usual.
            source = real_connect(":memory:")
            try:
                values = (frozen_now, *values[1:]) if values and values[0] == "now" else values
                placeholders = ", ".join("?" for _ in values)
                return source.execute(
                    f"SELECT datetime({placeholders})", values
                ).fetchone()[0]
            finally:
                source.close()

        conn.create_function("datetime", -1, _datetime)
        return conn

    return _connect


def test_frozen_prefix_datetime_applies_sqlite_now_modifiers(tmp_path):
    """The fixture freezes the built-in datetime expression, including modifiers."""
    connect = _frozen_connect(tmp_path / "unused.db", "2026-10-13 06:42:19")
    conn = connect(":memory:")
    try:
        now, cutoff = conn.execute(
            "SELECT datetime('now'), datetime('now', '-24 hours')"
        ).fetchone()
    finally:
        conn.close()

    assert now == "2026-10-13 06:42:19"
    assert cutoff == "2026-10-12 06:42:19"


# ══════════════════════════════════════════════════════════════════════════════
# Issue 1 — latest assessment vs historical maximum
# ══════════════════════════════════════════════════════════════════════════════

def test_prefix_query_resurrects_an_old_high_score(tmp_path):
    """FAILING-BEFORE: an old eligible row survives a newer ineligible one."""
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:GOOG", 95.0, "BUY", "t1", "60", "2026-10-13T01:00:00Z", 1),
        ("NASDAQ:GOOG", 40.0, "BUY", "t2", "60", "2026-10-13T05:00:00Z", 0),
    ])
    prefix_extract = _load_prefix_extract_stock_alerts(db_path)
    alerts = prefix_extract(hours=24)
    assert [alert["symbol"] for alert in alerts] == ["GOOG"], (
        "pre-fix query unexpectedly excluded the old high-scoring row"
    )
    assert alerts[0]["score"] == 95.0


def test_new_query_uses_the_latest_assessment(tmp_path):
    """PASSING-AFTER: the newer ineligible assessment wins."""
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:GOOG", 95.0, "BUY", "t1", "60", "2026-10-13T01:00:00Z", 1),
        ("NASDAQ:GOOG", 40.0, "BUY", "t2", "60", "2026-10-13T05:00:00Z", 0),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.stocks == []


def test_prefix_query_crowds_out_a_new_lower_scoring_fire(tmp_path):
    """FAILING-BEFORE: 20 historical high scores hide a new lower-scoring fire."""
    rows = [
        (f"NASDAQ:HIST{i}", 99.0 - i, "BUY", "t", "60", "2026-10-13T01:00:00Z", 1)
        for i in range(20)
    ]
    rows.append(("NASDAQ:NEW", 65.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1))
    db_path = _alerts_db(tmp_path, rows)

    prefix_extract = _load_prefix_extract_stock_alerts(db_path)
    alerts = prefix_extract(hours=24)
    assert "NEW" not in {alert["symbol"] for alert in alerts}, (
        "pre-fix query unexpectedly surfaced the new lower-scoring fire"
    )


def test_new_query_surfaces_the_new_lower_scoring_fire(tmp_path):
    """PASSING-AFTER: recency-first ordering keeps the new fire visible."""
    rows = [
        (f"NASDAQ:HIST{i}", 99.0 - i, "BUY", "t", "60", "2026-10-13T01:00:00Z", 1)
        for i in range(20)
    ]
    rows.append(("NASDAQ:NEW", 65.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1))
    db_path = _alerts_db(tmp_path, rows)

    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW, limit=20)
    assert "NEW" in {stock["symbol"] for stock in result.stocks}
    assert result.remainder == 1
