"""
Centralised path configuration for the stocks trading system.
================================================================

**Problem solved**: The codebase had ~240+ scattered ``sys.path.insert`` calls,
each adding different directories to make cross-module imports work.  This makes
import behaviour dependent on call order, working directory, and which scripts
have already run — causing fragile tests and production-only bugs.

**Solution**: This module defines every importable directory **once** and
provides ``ensure_paths()`` to add them to ``sys.path`` safely.  Callers replace
scattered ``sys.path.insert(0, ...)`` blocks with::

    from path_config import ensure_paths, INDIA, TV
    ensure_paths(INDIA, TV)

Duplicate entries are never added.  The function is idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Canonical directory constants ──────────────────────────────────────────────
ROOT: Path = Path(__file__).parent.resolve()

# Major subsystems
TV: Path       = ROOT / "tradingview"
CGT: Path      = ROOT / "cgt"
INDIA: Path    = ROOT / "india-brokers"
REPORTS: Path  = ROOT / "reports"
LOGS: Path     = ROOT / "logs"
DATABASE: Path = ROOT / "database"

# Report sub-directories
REPORTS_ARCHIVE: Path      = REPORTS / "archive"

# ── Canonical database file paths ──────────────────────────────────────────────
DB_ALERT_TRACKER: Path       = DATABASE / "alert_tracker.db"
DB_PORTFOLIO_HISTORY: Path   = DATABASE / "portfolio_history.db"
DB_PORTFOLIO_SCORE: Path     = DATABASE / "portfolio_score_history.db"
DB_SELL_HISTORY: Path        = DATABASE / "sell_history.db"
DB_ORDER_LEDGER: Path        = DATABASE / "order_ledger.db"
DB_REPORT_ANALYTICS: Path    = DATABASE / "report_analytics.db"

# TradingView sub-packages
TV_SELL: Path       = TV / "sell"
TV_SCREENERS: Path  = TV / "screeners"
TV_BACKTEST: Path   = TV / "backtest"
TV_OPTIONS: Path    = TV / "options"
TV_DATA: Path       = TV / "tv_data"
TV_SIGNAL_ENGINE: Path = TV / "signal_engine"

# CGT sub-directories (hyphenated — cannot be Python packages)
CGT_MOOMOO: Path  = CGT / "moomoo-cgt"
CGT_IBKR: Path    = CGT / "ibkr-cgt"
CGT_WEBULL: Path  = CGT / "webull-cgt"

# Other subsystems
IBKR_ASYNC: Path    = ROOT / "ibkr_async"
MOOMOO_SCRIPTS: Path = ROOT / "moomoo-scripts"
BENCHMARKS: Path    = ROOT / "benchmarks"


def ensure_data_dirs() -> None:
    """Create all canonical data directories if they don't exist."""
    for d in (DATABASE, REPORTS, REPORTS_ARCHIVE, LOGS):
        d.mkdir(parents=True, exist_ok=True)


def ensure_paths(*dirs: Path | str) -> None:
    """Add directories to ``sys.path`` if they are not already present.

    This is the **single approved way** to extend ``sys.path`` in this project.
    It prevents duplicate entries and makes the import-surface explicit.

    Parameters
    ----------
    *dirs : Path or str
        One or more directory paths to add.  Each is resolved to an absolute
        string before comparison so that ``Path`` objects and raw strings both
        work correctly.

    Example
    -------
    >>> from path_config import ensure_paths, INDIA, TV
    >>> ensure_paths(INDIA, TV)
    """
    for d in dirs:
        s = str(Path(d).resolve()) if not isinstance(d, str) else str(Path(d).resolve())
        if s not in sys.path:
            sys.path.insert(0, s)
