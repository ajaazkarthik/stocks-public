"""
OutcomeTracker — fetches expiry stock prices for pending TQS v2 contracts
and writes intrinsic_value_at_expiry, pnl_pct, is_winner back to DB.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# Resolve _apply_migrations regardless of how this module is imported
try:
    from ..persistence.db import _apply_migrations as _apply_db_migrations
except ImportError:
    from tradingview.options.tqs_v2.persistence.db import _apply_migrations as _apply_db_migrations

# Optional: yfinance for price fetching
try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False
    _log.warning("yfinance not installed — OutcomeTracker cannot fetch prices")


def _expiry_int_to_date(expiry_int: int) -> str:
    """Convert YYYYMMDD integer to 'YYYY-MM-DD' string."""
    s = str(expiry_int)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _fetch_close_on_date(symbol: str, date_str: str) -> float | None:
    """Return closing price for *symbol* on *date_str* ('YYYY-MM-DD') via yfinance.

    Returns None if data is unavailable (symbol not found, future date, weekend, etc.).
    """
    if not _YF_OK:
        return None
    try:
        ticker = yf.Ticker(symbol)
        # Download a small window around the expiry date to handle weekends/holidays
        hist = ticker.history(start=date_str, end=None, period="5d", auto_adjust=True)
        if hist.empty:
            return None
        # Use the closing price on or just after the expiry date
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        target = datetime.strptime(date_str, "%Y-%m-%d")
        future = hist[hist.index >= target]
        if future.empty:
            return None
        return float(future["Close"].iloc[0])
    except Exception as exc:
        _log.debug("yfinance error for %s on %s: %s", symbol, date_str, exc)
        return None


class OutcomeTracker:
    """Fills outcome columns for expired TQS v2 contracts.

    Usage::

        tracker = OutcomeTracker(db_path)
        updated = tracker.fetch_pending_outcomes()
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Ensure all new columns exist in pre-Task-2 databases (idempotent)
        _apply_db_migrations(self._conn)

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_pending_outcomes(self) -> int:
        """Fetch and store outcomes for all expired, unfilled contracts.

        A contract is *pending* when ``is_winner IS NULL`` and
        ``expiration < today_int`` (i.e. it has expired).

        Returns the number of rows updated.
        """
        today_int = int(datetime.now(timezone.utc).strftime("%Y%m%d"))

        rows = self._conn.execute(
            """SELECT id, symbol, option_type, expiration, strike, mid
               FROM tqs_v2_results
               WHERE (is_winner IS NULL OR outcome_fetched_at LIKE 'LIVE:%')
                 AND expiration < ?
               ORDER BY expiration ASC, symbol ASC""",
            (today_int,),
        ).fetchall()

        if not rows:
            _log.info("OutcomeTracker: no pending outcomes")
            return 0

        _log.info("OutcomeTracker: processing %d pending contracts", len(rows))

        # Group by (symbol, expiration) to minimise yfinance calls
        groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            date_str = _expiry_int_to_date(row["expiration"])
            groups[(row["symbol"], date_str)].append(row)

        updated = 0
        for (symbol, date_str), group_rows in groups.items():
            expiry_price = _fetch_close_on_date(symbol, date_str)
            fetched_at = datetime.utcnow().isoformat()

            for row in group_rows:
                intrinsic = self._intrinsic(
                    row["option_type"],
                    float(row["strike"] or 0),
                    expiry_price,
                )
                mid = float(row["mid"] or 0)
                pnl_pct = None
                is_winner = None
                if intrinsic is not None and mid and mid > 0:
                    pnl_pct = round((intrinsic - mid) / mid * 100, 2)
                    is_winner = 1 if intrinsic > mid else 0

                self._conn.execute(
                    """UPDATE tqs_v2_results
                       SET expiry_stock_price      = ?,
                           intrinsic_value_at_expiry = ?,
                           pnl_pct                 = ?,
                           outcome_fetched_at      = ?,
                           is_winner               = ?
                       WHERE id = ?""",
                    (expiry_price, intrinsic, pnl_pct, fetched_at, is_winner, row["id"]),
                )
                updated += 1

        self._conn.commit()
        _log.info("OutcomeTracker: updated %d rows", updated)
        return updated

    def fetch_live_outcomes(self) -> int:
        """Fetch current market premiums for unexpired contracts (mark-to-market).

        For each contract where ``is_winner IS NULL`` and ``expiration >= today``
        we fetch the current option chain from yfinance, compute the mark-to-market
        mid price, and store the unrealised P&L.

        ``outcome_fetched_at`` is prefixed with ``LIVE:`` so the reporter can
        distinguish unrealised outcomes from expiry-settled ones.

        Returns the number of rows updated.
        """
        if not _YF_OK:
            _log.warning("fetch_live_outcomes: yfinance not available, skipping")
            return 0

        today_int = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        rows = self._conn.execute(
            """SELECT id, symbol, option_type, expiration, strike, mid
               FROM tqs_v2_results
               WHERE is_winner IS NULL
                 AND expiration >= ?
               ORDER BY expiration ASC, symbol ASC""",
            (today_int,),
        ).fetchall()

        if not rows:
            _log.info("OutcomeTracker: no live unexpired contracts to update")
            return 0

        _log.info("OutcomeTracker: fetching live premiums for %d contracts", len(rows))

        # Group by (symbol, expiry_str) to batch yfinance option_chain calls
        groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            date_str = _expiry_int_to_date(row["expiration"])
            groups[(row["symbol"], date_str)].append(row)

        updated = 0
        for (symbol, expiry_str), group_rows in groups.items():
            try:
                ticker = yf.Ticker(symbol)
                available = ticker.options  # tuple of 'YYYY-MM-DD'
                if expiry_str not in available:
                    _log.debug(
                        "No chain for %s exp %s (not in available dates: %s)",
                        symbol, expiry_str, list(available)[:5],
                    )
                    continue
                chain = ticker.option_chain(expiry_str)
            except Exception as exc:
                _log.debug("yfinance option chain error for %s: %s", symbol, exc)
                continue

            fetched_at = f"LIVE:{datetime.utcnow().isoformat()}"

            for row in group_rows:
                ot = (row["option_type"] or "").upper()
                df = chain.calls if ot.startswith("C") else chain.puts
                strike = float(row["strike"] or 0)

                # Match by strike (round to 2 dp for float safety)
                match = df[df["strike"].round(2) == round(strike, 2)]
                if match.empty:
                    _log.debug(
                        "No strike match for %s %s %.2f exp %s",
                        symbol, ot, strike, expiry_str,
                    )
                    continue

                r = match.iloc[0]
                bid = float(r.get("bid", 0) or 0)
                ask = float(r.get("ask", 0) or 0)
                last = float(r.get("lastPrice", 0) or 0)
                if bid > 0 or ask > 0:
                    current_mid = round((bid + ask) / 2, 4)
                elif last > 0:
                    current_mid = round(last, 4)
                else:
                    continue

                entry_mid = float(row["mid"] or 0)
                if entry_mid <= 0:
                    continue

                pnl_pct = round((current_mid - entry_mid) / entry_mid * 100, 2)
                is_winner = 1 if current_mid > entry_mid else 0

                self._conn.execute(
                    """UPDATE tqs_v2_results
                       SET current_premium    = ?,
                           pnl_pct            = ?,
                           outcome_fetched_at = ?,
                           is_winner          = ?
                       WHERE id = ?""",
                    (current_mid, pnl_pct, fetched_at, is_winner, row["id"]),
                )
                updated += 1

        self._conn.commit()
        _log.info("OutcomeTracker: %d live contracts updated (unrealised)", updated)
        return updated

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _intrinsic(
        option_type: str,
        strike: float,
        expiry_price: float | None,
    ) -> float | None:
        """Compute intrinsic value at expiry.

        * CALL: max(0, expiry_price - strike)
        * PUT:  max(0, strike - expiry_price)
        """
        if expiry_price is None:
            return None
        ot = (option_type or "").upper()
        if ot.startswith("C"):
            return round(max(0.0, expiry_price - strike), 4)
        if ot.startswith("P"):
            return round(max(0.0, strike - expiry_price), 4)
        return None

    def close(self) -> None:
        """Close the DB connection."""
        self._conn.close()

    def __enter__(self) -> "OutcomeTracker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
