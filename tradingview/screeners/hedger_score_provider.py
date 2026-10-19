"""
HedgerScoreProvider — SOLID signal-score resolver for the options hedger.
===========================================================================
Produces a :class:`ScoreRecord` for any underlying symbol by querying data
sources in strict priority order:

  Priority 1 — DB composite (full L1+L2+L3+L4, stored when alert fired)
  Priority 2 — Live AnalysisEngine (full L1+L2+L3+L4 computed on demand)
  Priority 3 — TV screener L1-only estimate (last resort, *partial* flag set)

SOLID alignment
---------------
S — Single Responsibility: each class has one job (read DB / run engine / L1 fallback).
O — Open/Closed: add new score sources by implementing :class:`ScoreSource`; no
    changes to :class:`HedgerScoreProvider` needed.
L — Liskov Substitution: any :class:`ScoreSource` can be swapped without affecting
    the provider.
I — Interface Segregation: :class:`ScoreSource` has exactly one method.
D — Dependency Inversion: :class:`HedgerScoreProvider` depends on the
    :class:`ScoreSource` protocol, not concrete implementations.

Usage
-----
>>> provider = HedgerScoreProvider.default()           # production
>>> record   = provider.get(underlying="NVDA", tv_symbol="NASDAQ:NVDA")
>>> record.score       # full composite  (e.g. 82)
>>> record.source      # "db" | "live" | "partial"
>>> record.partial     # True only when L2/L3/L4 are NOT available
>>> record.age_h       # hours since score was computed (None for live)
"""

from __future__ import annotations

import sqlite3
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Locate repo root reliably wherever this file is called from ──────────────
_HERE       = Path(__file__).resolve()
_SCREENERS  = _HERE.parent                            # tradingview/screeners/
_TV         = _SCREENERS.parent                       # tradingview/
_REPO_ROOT  = _TV.parent                              # repo root
_DB_PATH    = _REPO_ROOT / "database" / "alert_tracker.db"

# Ensure tradingview/ is importable (for signal_engine, signal_stack, etc.)
if str(_TV) not in sys.path:
    sys.path.insert(0, str(_TV))

# ─────────────────────────────────────────────────────────────────────────────
# Value object
# ─────────────────────────────────────────────────────────────────────────────

class ScoreRecord:
    """
    Immutable result from any :class:`ScoreSource`.

    Attributes
    ----------
    score   : int   — rounded composite score 0–100
    source  : str   — "db" | "live" | "partial"
    age_h   : int | None — hours since the score was originally computed
    partial : bool  — True when L2/L3/L4 are NOT available (L1 only)
                      The badge renderer MUST NOT show WEAK/STRONG for partial.
    live    : bool  — True when freshly computed (not from DB / cache)
    """

    __slots__ = ("score", "source", "age_h", "partial", "live")

    def __init__(
        self,
        score: float,
        source: str,
        age_h: Optional[int] = None,
        partial: bool = False,
        live: bool = False,
    ) -> None:
        self.score   = int(round(score))
        self.source  = source
        self.age_h   = age_h
        self.partial = partial
        self.live    = live

    # ------------------------------------------------------------------
    # Interop helpers — options_hedger uses dicts, so provide conversion
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Convert to the legacy dict format used by options_hedger.py."""
        return {
            "score":   self.score,
            "live":    self.live,
            "age_h":   self.age_h,
            "partial": self.partial,
            "source":  self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreRecord":
        """Reconstruct from a legacy hedger dict (backward compat)."""
        return cls(
            score   = float(d.get("score", 0)),
            source  = d.get("source", "db"),
            age_h   = d.get("age_h"),
            partial = bool(d.get("partial", False)),
            live    = bool(d.get("live", False)),
        )

    def __repr__(self) -> str:
        partial_tag = " PARTIAL" if self.partial else ""
        return (
            f"ScoreRecord(score={self.score}, source={self.source!r}"
            f", age_h={self.age_h}{partial_tag})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ScoreRecord):
            return NotImplemented
        return (
            self.score   == other.score
            and self.source  == other.source
            and self.age_h   == other.age_h
            and self.partial == other.partial
            and self.live    == other.live
        )


# ─────────────────────────────────────────────────────────────────────────────
# ScoreSource protocol / ABC  (Interface Segregation)
# ─────────────────────────────────────────────────────────────────────────────

class ScoreSource(ABC):
    """
    Abstract data source that resolves a signal score for one underlying.

    Implementations must be **stateless** — all state lives in
    :class:`HedgerScoreProvider`.
    """

    @abstractmethod
    def get_score(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        """
        Return a :class:`ScoreRecord` for *underlying* or ``None`` if this
        source cannot provide a score for this symbol.

        Parameters
        ----------
        underlying : str  — bare ticker (e.g. "NVDA", "TITAN")
        tv_symbol  : str  — fully-qualified TV symbol (e.g. "NASDAQ:NVDA")
        """

    @property
    def name(self) -> str:
        """Human-readable name for logging / diagnostics."""
        return type(self).__name__


# ─────────────────────────────────────────────────────────────────────────────
# Priority 1 — DB composite score
# ─────────────────────────────────────────────────────────────────────────────

class DbScoreSource(ScoreSource):
    """
    Reads the most recent ``composite_score`` for *underlying* from
    ``alert_tracker.db``.

    This is the **authoritative** score — it was produced by the complete
    L1+L2+L3+L4 signal pipeline when the TradingView alert fired.

    Parameters
    ----------
    db_path : Path  — path to alert_tracker.db (default: auto-detected)
    max_age_h : int — ignore DB records older than this (default: 168h = 7 days)
                      Use 0 to never expire.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        max_age_h: int = 168,
    ) -> None:
        self._db_path   = db_path or _DB_PATH
        self._max_age_h = max_age_h

    def get_score(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        if not self._db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            row = conn.execute(
                "SELECT composite_score, created_at FROM alerts "
                "WHERE (tv_symbol = ? OR tv_symbol LIKE ? OR tv_symbol LIKE ?) "
                "AND composite_score IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (underlying, f"%:{underlying}", f"{underlying}:%"),
            ).fetchone()
            conn.close()
        except Exception:
            return None

        if not row:
            return None

        sc = float(row[0])
        age_h: Optional[int] = None
        try:
            dt_str = row[1]
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_secs = (datetime.now(tz=timezone.utc) - dt).total_seconds()
            age_h = max(0, int(age_secs / 3600))
        except Exception:
            pass

        # Respect max_age_h — if too stale, let a fresher source try
        if self._max_age_h > 0 and age_h is not None and age_h > self._max_age_h:
            return None

        return ScoreRecord(
            score   = sc,
            source  = "db",
            age_h   = age_h,
            partial = False,
            live    = False,
        )

    @property
    def name(self) -> str:
        return "DbScoreSource"


# ─────────────────────────────────────────────────────────────────────────────
# Priority 2 — Live AnalysisEngine (full L1+L2+L3+L4)
# ─────────────────────────────────────────────────────────────────────────────

class LiveScoreSource(ScoreSource):
    """
    Runs the full L1+L2+L3+L4 pipeline via :class:`AnalysisEngine` for
    symbols that have no (or stale) DB record.

    Takes ~5-15 s per symbol because it fetches TV screener data, TV
    fundamentals, yfinance sentiment, and market internals.

    Parameters
    ----------
    timeout_s : int  — per-symbol timeout in seconds (default 30)
    use_auth  : bool — pass the TV session cookie for richer data
    """

    def __init__(self, timeout_s: int = 30, use_auth: bool = False) -> None:
        self._timeout_s = timeout_s
        self._use_auth  = use_auth
        self._engine    = None   # lazy-loaded

    def _get_engine(self):
        """Lazy-load AnalysisEngine (avoids heavy imports at module load time)."""
        if self._engine is None:
            from signal_engine.analysis_engine import AnalysisEngine  # type: ignore
            self._engine = AnalysisEngine()
        return self._engine

    def get_score(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        """
        Run analyse_signal() with a synthetic fire dict containing only the
        TV symbol.  The analysis engine handles empty ``message`` gracefully —
        L1 still gets RSI/ADX/rel_vol from TV screener; L2/L3/L4 are fully
        computed from TV fundamentals, yfinance sentiment, and market internals.
        """
        try:
            import signal as _signal_mod  # noqa: F401  (we only need threading)
            import threading

            engine = self._get_engine()
            result: dict = {}
            exc_holder: list = []

            def _run() -> None:
                try:
                    result.update(
                        engine.analyse_signal(
                            fire={"pro_symbol": tv_symbol},
                            use_auth=self._use_auth,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    exc_holder.append(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=self._timeout_s)

            if t.is_alive() or exc_holder or "composite" not in result:
                # Timed out or errored — let the next source handle it
                return None

            scores = result.get("scores", {})
            return ScoreRecord(
                score   = float(result["composite"]),
                source  = "live",
                age_h   = 0,       # freshly computed right now
                partial = False,   # all 4 layers computed
                live    = True,
            )
        except Exception:  # noqa: BLE001
            return None

    @property
    def name(self) -> str:
        return "LiveScoreSource"


# ─────────────────────────────────────────────────────────────────────────────
# Priority 3 — TV screener L1-only estimate (last resort)
# ─────────────────────────────────────────────────────────────────────────────

class PartialScoreSource(ScoreSource):
    """
    Computes a **partial** score using only the TV screener (L1 technical).
    L2/L3/L4 use a neutral placeholder (5.0/10) and are NOT measured.

    ⚠️  CRITICAL: scores from this source MUST be marked ``partial=True``.
    The badge renderer in options_hedger MUST NOT show WEAK/STRONG advice
    for partial scores — a partial score of 56 for NVDA does NOT mean
    "selling is the right call" (the real score with L2/L3/L4 is 82+).

    This source should only fire for symbols that:
      - Have never appeared in alert_tracker.db (very new discovery)
      - AND where the AnalysisEngine timed out or errored
    """

    def get_score(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        try:
            if str(_TV) not in sys.path:
                sys.path.insert(0, str(_TV))
            import signal_stack as ss  # type: ignore

            screener_data = ss.fetch_tv_screener([tv_symbol])
            sc_raw = screener_data.get(tv_symbol, {})
            if not sc_raw:
                return None

            l1_result = ss.score_technical({}, screener=sc_raw)
            l1 = l1_result.get("total", 5.0)
            # Use neutral 5.0 for L2/L3/L4 — these are NOT available here.
            # This will underestimate quality stocks by ~20-30 pts.
            composite = ss.composite(l1, 5.0, 5.0, 5.0)
            return ScoreRecord(
                score   = composite,
                source  = "partial",
                age_h   = None,
                partial = True,    # MUST be True — L2/L3/L4 not available
                live    = False,
            )
        except Exception:  # noqa: BLE001
            return None

    @property
    def name(self) -> str:
        return "PartialScoreSource"


# ─────────────────────────────────────────────────────────────────────────────
# HedgerScoreProvider — orchestrator (Dependency Inversion)
# ─────────────────────────────────────────────────────────────────────────────

class HedgerScoreProvider:
    """
    Resolves signal scores for option underlying symbols using a prioritised
    chain of :class:`ScoreSource` implementations.

    Design
    ------
    * Stateful cache (``_cache``) so each underlying is resolved only once per
      ``HedgerScoreProvider`` instance.
    * Thread-safe prefetch via :meth:`prefetch` — call it once before the
      options-position loop so all symbols are resolved up front.
    * Dependency-injection constructor — swap sources for testing or custom
      configurations without changing this class.

    Default priority chain
    ----------------------
    1. :class:`DbScoreSource`      — full composite, stored in alert_tracker.db
    2. :class:`LiveScoreSource`    — full composite, freshly computed
    3. :class:`PartialScoreSource` — L1-only estimate, partial=True (last resort)

    Parameters
    ----------
    sources : list[ScoreSource]  — ordered list (first match wins)
    """

    def __init__(self, sources: list) -> None:
        if not sources:
            raise ValueError("At least one ScoreSource is required")
        self._sources: list = sources
        self._cache: dict[str, Optional[ScoreRecord]] = {}

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def default(
        cls,
        db_max_age_h: int = 168,
        live_timeout_s: int = 30,
        use_auth: bool = False,
    ) -> "HedgerScoreProvider":
        """
        Production instance with DB → live → partial priority chain.

        Parameters
        ----------
        db_max_age_h  : DB records older than this are considered stale
                        (0 = never expire).  Default 7 days.
        live_timeout_s: per-symbol timeout for live AnalysisEngine calls.
        use_auth      : whether to use TV cookie for richer data.
        """
        return cls(
            sources=[
                DbScoreSource(max_age_h=db_max_age_h),
                LiveScoreSource(timeout_s=live_timeout_s, use_auth=use_auth),
                PartialScoreSource(),
            ]
        )

    @classmethod
    def db_only(cls, db_max_age_h: int = 0) -> "HedgerScoreProvider":
        """
        DB-only instance — useful in tests or offline mode.
        Never makes network calls.
        """
        return cls(sources=[DbScoreSource(max_age_h=db_max_age_h)])

    @classmethod
    def live_only(
        cls,
        timeout_s: int = 30,
        use_auth: bool = False,
    ) -> "HedgerScoreProvider":
        """
        Live-only instance — always computes fresh scores, never uses DB.
        Useful for one-off diagnostic runs.
        """
        return cls(
            sources=[
                LiveScoreSource(timeout_s=timeout_s, use_auth=use_auth),
                PartialScoreSource(),
            ]
        )

    @classmethod
    def for_testing(
        cls,
        db_returns: Optional[dict] = None,
        live_returns: Optional[dict] = None,
    ) -> "HedgerScoreProvider":
        """
        Test-only factory that injects stub sources.

        Parameters
        ----------
        db_returns   : {underlying: ScoreRecord | None} — DB source mock
        live_returns : {underlying: ScoreRecord | None} — live source mock
        """
        sources: list = []
        if db_returns is not None:
            sources.append(_StubScoreSource("db_stub", db_returns))
        if live_returns is not None:
            sources.append(_StubScoreSource("live_stub", live_returns))
        if not sources:
            sources.append(PartialScoreSource())
        return cls(sources=sources)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def get(
        self,
        underlying: str,
        tv_symbol: Optional[str] = None,
    ) -> Optional[ScoreRecord]:
        """
        Return a :class:`ScoreRecord` for *underlying*, or ``None`` if all
        sources fail.

        Results are cached — repeated calls for the same underlying are free.

        Parameters
        ----------
        underlying : str   — bare ticker (e.g. "NVDA")
        tv_symbol  : str   — optional fully-qualified TV symbol used by
                             live/partial sources.  If omitted, guessed as
                             ``NASDAQ:<underlying>`` for US symbols.
        """
        if underlying in self._cache:
            return self._cache[underlying]

        resolved_tv = tv_symbol or self._guess_tv_symbol(underlying)
        record = self._resolve(underlying, resolved_tv)
        self._cache[underlying] = record
        return record

    def prefetch(
        self,
        underlyings: list,
        tv_symbol_map: Optional[dict] = None,
    ) -> None:
        """
        Batch-resolve scores for all *underlyings* that are not yet cached.

        Currently resolves serially (one at a time).  The :class:`LiveScoreSource`
        already uses a background thread internally, so this blocks for at most
        ``live_timeout_s`` per uncached symbol.

        Parameters
        ----------
        underlyings   : list[str]      — bare tickers to resolve
        tv_symbol_map : dict[str, str] — optional {underlying: tv_symbol} mapping
        """
        needed = [u for u in underlyings if u not in self._cache]
        for u in needed:
            tv_sym = (tv_symbol_map or {}).get(u) or self._guess_tv_symbol(u)
            record = self._resolve(u, tv_sym)
            self._cache[u] = record

    def invalidate(self, underlying: Optional[str] = None) -> None:
        """
        Clear the cache.

        Parameters
        ----------
        underlying : str | None — clear only this symbol; None clears all.
        """
        if underlying is None:
            self._cache.clear()
        else:
            self._cache.pop(underlying, None)

    def to_dict(self, underlying: str) -> Optional[dict]:
        """
        Return the legacy ``{score, live, age_h, partial}`` dict that
        options_hedger's badge renderer expects, or ``None``.
        """
        record = self.get(underlying)
        return record.to_dict() if record is not None else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        """Try each source in priority order; return first non-None result."""
        for source in self._sources:
            try:
                record = source.get_score(underlying, tv_symbol)
            except Exception:  # noqa: BLE001
                record = None
            if record is not None:
                return record
        return None

    @staticmethod
    def _guess_tv_symbol(underlying: str) -> str:
        """
        Best-effort TV symbol when none is provided.

        Simple heuristic:
        - If underlying already contains ":" — use as-is
        - Otherwise default to NASDAQ (covers most US large-caps in our system)
        """
        if ":" in underlying:
            return underlying
        return f"NASDAQ:{underlying}"


# ─────────────────────────────────────────────────────────────────────────────
# Test-only stub (not exported in __all__)
# ─────────────────────────────────────────────────────────────────────────────

class _StubScoreSource(ScoreSource):
    """Injectable stub for unit tests — returns pre-set ScoreRecords."""

    def __init__(self, name: str, returns: dict) -> None:
        self._name    = name
        self._returns = returns   # {underlying: ScoreRecord | None}

    def get_score(
        self,
        underlying: str,
        tv_symbol: str,
    ) -> Optional[ScoreRecord]:
        return self._returns.get(underlying)

    @property
    def name(self) -> str:
        return self._name


# ─────────────────────────────────────────────────────────────────────────────
# TV-symbol resolution helper (extracted from options_hedger for reuse)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_tv_symbols_from_db(
    underlyings: list,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Query alert_tracker.db to find the TV symbol for each underlying.

    Returns {underlying: tv_symbol}.  Symbols not found in DB default to
    ``NASDAQ:<underlying>``.

    This function is used by :meth:`HedgerScoreProvider.prefetch` and
    can also be called standalone.
    """
    path = db_path or _DB_PATH
    tv_sym_map: dict = {}

    if path.exists():
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            for u in underlyings:
                row = conn.execute(
                    "SELECT tv_symbol FROM alerts "
                    "WHERE tv_symbol LIKE ? AND tv_symbol LIKE '%:%' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (f"%:{u}",),
                ).fetchone()
                if row:
                    tv_sym_map[u] = row[0]
            conn.close()
        except Exception:
            pass

    # Default for symbols not found in DB
    for u in underlyings:
        if u not in tv_sym_map:
            tv_sym_map[u] = f"NASDAQ:{u}"

    return tv_sym_map


__all__ = [
    "ScoreRecord",
    "ScoreSource",
    "DbScoreSource",
    "LiveScoreSource",
    "PartialScoreSource",
    "HedgerScoreProvider",
    "resolve_tv_symbols_from_db",
]
