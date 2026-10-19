"""Typed stock-data results: unavailable is not the same as empty.

The scheduler previously returned ``[]`` for a missing database, a lock
timeout, a schema error, a corrupt file, *and* a genuinely empty result. The
summary then claimed "no stock alerts" while the data was simply unavailable.

Every function here returns a :class:`StockResult` carrying an explicit
``status``, the rows, and sanitized ``errors``, so the caller can distinguish:

``success``      query ran, rows may be present
``empty``        query ran, zero actionable rows — a genuine zero-result run
``no_new``       current-run payload exists but contains no new fires
``unavailable``  the data source is missing (never created as a side effect)
``failed``       the source exists but could not be read
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from tradingview.scheduler.config import redact
from tradingview.scheduler.identity import stock_fire_key
from tradingview.scheduler.timeutil import to_utc, utc_now

STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_NO_NEW = "no_new"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

#: Statuses that mean "we do not know the market state".
UNAVAILABLE_STATUSES = frozenset({STATUS_UNAVAILABLE, STATUS_FAILED})

#: Statuses that legitimately mean "there is nothing to alert on".
EMPTY_STATUSES = frozenset({STATUS_EMPTY, STATUS_NO_NEW})

#: Default rolling window for the manual/latest watchlist.
DEFAULT_WINDOW_HOURS = 24.0

#: Default cap on rows returned by the historical watchlist query.
DEFAULT_HISTORICAL_LIMIT = 20

_SIGNAL_MAP = {"UNKNOWN": "SCR", "MANUAL": "SCR", "": "SCR"}


@dataclass
class StockResult:
    """Structured stock-data outcome for one cycle or one manual query."""

    status: str
    stocks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = "current_run"
    run_id: str = ""
    window_start: str = ""
    window_end: str = ""
    total_eligible: int = 0
    malformed_timestamps: int = 0

    @property
    def available(self) -> bool:
        return self.status not in UNAVAILABLE_STATUSES

    @property
    def is_empty(self) -> bool:
        return self.status in EMPTY_STATUSES

    @property
    def remainder(self) -> int:
        """Rows eligible but not included because of the display limit."""
        return max(0, self.total_eligible - len(self.stocks))

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "run_id": self.run_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_eligible": self.total_eligible,
            "malformed_timestamps": self.malformed_timestamps,
            "errors": [redact(error) for error in self.errors],
            "stocks": self.stocks,
        }


def _parse_exchange_symbol(tv_symbol: str) -> tuple[str, str]:
    """Extract ``(exchange, symbol)`` from ``'SGX:BS6'``; bare symbols → ``US``."""
    text = str(tv_symbol or "")
    if ":" in text:
        exchange, symbol = text.split(":", 1)
        return exchange.upper(), symbol.upper()
    return "US", text.upper()


def _row_to_stock(row: sqlite3.Row | tuple) -> dict:
    tv_symbol = row["tv_symbol"] if isinstance(row, sqlite3.Row) else row[0]
    score = row["composite_score"] if isinstance(row, sqlite3.Row) else row[1]
    signal = row["signal"] if isinstance(row, sqlite3.Row) else row[2]
    fire_time = row["fire_time"] if isinstance(row, sqlite3.Row) else row[3]
    resolution = row["resolution"] if isinstance(row, sqlite3.Row) else row[4]
    exchange, symbol = _parse_exchange_symbol(tv_symbol or "")
    raw_signal = str(signal or "").upper()
    return {
        "exchange": exchange,
        "symbol": symbol,
        "pro_symbol": str(tv_symbol or ""),
        "score": round(float(score or 0), 1),
        "signal": _SIGNAL_MAP.get(raw_signal, raw_signal[:4]),
        "fire_time": fire_time or "",
        "resolution": resolution or "",
    }


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """Open *db_path* read-only so a missing file is never created."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _classify_sqlite_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "stock database is locked"
    if "no such table" in text:
        return "stock database is missing the alerts table"
    if "malformed" in text or "not a database" in text or "corrupt" in text:
        return "stock database is malformed"
    if "unable to open" in text or "readonly" in text:
        return "stock database could not be opened read-only"
    return f"stock database query failed: {type(exc).__name__}"


def query_latest_assessment_stocks(
    db_path: Path | str,
    *,
    now: datetime | None = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_HISTORICAL_LIMIT,
) -> StockResult:
    """Return the latest assessment per symbol inside ``(now - window, now]``.

    Two deliberate properties:

    * **Latest assessment wins.** The most recent row per symbol is selected
      *before* the actionability filter, so a newer ineligible assessment
      invalidates an older eligible one instead of the query resurrecting the
      old maximum-score row.
    * **Real instants, not text.** Comparisons use ``julianday()`` so ISO
      ``T``/offset/fractional timestamps and legacy space-separated SQLite
      timestamps are all compared as UTC instants. The lower bound is
      exclusive and future records are excluded.
    """
    path = Path(db_path)
    reference = to_utc(now) if now is not None else utc_now()
    cutoff = reference - timedelta(hours=float(window_hours))
    window_start = cutoff.isoformat()
    window_end = reference.isoformat()

    if not path.exists():
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=["stock database is missing"],
            source="historical",
            window_start=window_start,
            window_end=window_end,
        )

    try:
        conn = _read_only_connection(path)
    except sqlite3.Error as exc:
        return StockResult(
            status=STATUS_FAILED,
            errors=[_classify_sqlite_error(exc)],
            source="historical",
            window_start=window_start,
            window_end=window_end,
        )

    try:
        malformed = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE julianday(created_at) IS NULL"
        ).fetchone()[0]
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT tv_symbol, MAX(julianday(created_at)) AS jd
                FROM alerts
                WHERE julianday(created_at) IS NOT NULL
                GROUP BY tv_symbol
            )
            SELECT a.tv_symbol, a.composite_score, a.signal, a.fire_time,
                   a.resolution, a.created_at
            FROM alerts a
            JOIN latest l
              ON l.tv_symbol = a.tv_symbol
             AND julianday(a.created_at) = l.jd
            WHERE julianday(a.created_at) > julianday(?)
              AND julianday(a.created_at) <= julianday(?)
              AND a.actionable = 1
            ORDER BY julianday(a.created_at) DESC, a.composite_score DESC,
                     a.tv_symbol ASC
            """,
            (window_start, window_end),
        ).fetchall()
    except sqlite3.Error as exc:
        return StockResult(
            status=STATUS_FAILED,
            errors=[_classify_sqlite_error(exc)],
            source="historical",
            window_start=window_start,
            window_end=window_end,
        )
    finally:
        conn.close()

    stocks = [_row_to_stock(row) for row in rows]
    total = len(stocks)
    errors: list[str] = []
    if malformed:
        errors.append(f"{malformed} alert row(s) have an unparseable created_at and were skipped")

    return StockResult(
        status=STATUS_SUCCESS if stocks else STATUS_EMPTY,
        stocks=stocks[:limit],
        errors=errors,
        source="historical",
        window_start=window_start,
        window_end=window_end,
        total_eligible=total,
        malformed_timestamps=int(malformed or 0),
    )


def load_current_run_stocks(
    payload_path: Path | str,
    run_id: str,
) -> StockResult:
    """Read the current run's stock results from the durable cycle payload.

    Every eligible row is returned — the loader never truncates, because a
    truncated row would be silently lost from delivery (its fire has already
    completed scoring, so a later ``--new-only`` cycle will not recover it).
    Delivery chunking is the caller's responsibility.

    A missing file, an unreadable file, or a payload belonging to a different
    run is reported as ``unavailable`` — never as an empty market result, and
    never silently replaced by a historical query.
    """
    path = Path(payload_path)
    if not path.exists():
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=["current-run stock payload is missing"],
            source="current_run",
            run_id=run_id,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=[f"current-run stock payload is unreadable: {type(exc).__name__}"],
            source="current_run",
            run_id=run_id,
        )
    if not isinstance(payload, dict):
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=["current-run stock payload is not an object"],
            source="current_run",
            run_id=run_id,
        )
    payload_run_id = str(payload.get("run_id") or "")
    if run_id and payload_run_id != run_id:
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=["current-run stock payload belongs to a different run"],
            source="current_run",
            run_id=run_id,
        )

    raw_stocks = payload.get("stocks")
    if not isinstance(raw_stocks, list):
        return StockResult(
            status=STATUS_UNAVAILABLE,
            errors=["current-run stock payload has no stock list"],
            source="current_run",
            run_id=run_id,
        )

    stocks: list[dict] = []
    malformed = 0
    for entry in raw_stocks:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        pro_symbol, fire_time, resolution = stock_fire_key(entry)
        if not pro_symbol or not fire_time:
            malformed += 1
            continue
        try:
            score = float(entry.get("score"))
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not math.isfinite(score):
            malformed += 1
            continue
        # New producers must explicitly say the row passed the engine's
        # actionability gate. Legacy WIN handoffs predate this field, so only
        # reject an explicit false value.
        scanner_row = str(entry.get("source") or "").lower() == "scanner"
        if scanner_row:
            session_date = str(entry.get("session_date") or "")
            timestamp = str(entry.get("timestamp") or "")
            direction = str(entry.get("direction") or "").upper()
            try:
                valid_date = date.fromisoformat(session_date).isoformat() == session_date
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                valid_timestamp = parsed_timestamp.tzinfo is not None and parsed_timestamp.utcoffset() is not None
                from tradingview.scheduler.scanner import MARKETS
                market_key = str(entry.get("market") or "").upper()
                valid_timestamp = valid_timestamp and parsed_timestamp.astimezone(
                    ZoneInfo(MARKETS[market_key][1])
                ).date().isoformat() == session_date if market_key in MARKETS else False
            except (TypeError, ValueError):
                valid_date, valid_timestamp = False, False
            if (entry.get("actionable") is not True
                    or str(entry.get("market") or "").upper() not in {"US", "AU", "IN", "SG"}
                    or direction not in {"BUY", "SELL"}
                    or not valid_date or not valid_timestamp):
                malformed += 1
                continue
        if "actionable" in entry and entry.get("actionable") is not True:
            continue
        if scanner_row and not 0 <= score <= 100:
            malformed += 1
            continue
        exchange, symbol = _parse_exchange_symbol(pro_symbol)
        stock = {
            "exchange": entry.get("exchange") or exchange,
            "symbol": entry.get("symbol") or symbol,
            "pro_symbol": pro_symbol,
            "score": round(score, 1),
            "signal": str(entry.get("signal") or "SCR")[:4],
            "fire_time": fire_time,
            "resolution": resolution,
        }
        for key in ("source", "market", "session_date", "timestamp", "actionable", "direction",
                    "bar_time", "bar_end_time", "bar_interval_minutes", "quote_source",
                    "quote_symbol", "provider_close", "source_price_delta_pct",
                    "freshness_assurance", "configured_provider_delay_minutes"):
            if key in entry:
                stock[key] = entry[key]
        stocks.append(stock)

    errors: list[str] = []
    if malformed:
        errors.append(f"{malformed} current-run stock row(s) were malformed and skipped")

    # Preserve the producer's own status and errors. A producer that reported
    # `failed`/`unavailable` must not be silently downgraded to `empty`, and its
    # error text must survive into the cycle result.
    producer_status = str(payload.get("status") or "").strip()
    producer_errors = payload.get("errors")
    if isinstance(producer_errors, list):
        errors.extend(str(error) for error in producer_errors if str(error).strip())

    if producer_status in UNAVAILABLE_STATUSES:
        resolved_status = producer_status
    elif producer_status == "partial":
        resolved_status = "partial"
    elif producer_status == STATUS_NO_NEW:
        resolved_status = STATUS_NO_NEW
    elif stocks:
        resolved_status = STATUS_SUCCESS
    elif producer_status == STATUS_EMPTY:
        resolved_status = STATUS_EMPTY
    else:
        resolved_status = STATUS_EMPTY

    # Never truncate: every eligible current-run row must reach delivery.
    return StockResult(
        status=resolved_status,
        stocks=stocks,
        errors=errors,
        source="current_run",
        run_id=payload_run_id,
        window_start=str(payload.get("started_at") or ""),
        window_end=str(payload.get("completed_at") or ""),
        total_eligible=len(stocks),
        malformed_timestamps=malformed,
    )


def build_current_run_payload(
    run_id: str,
    stocks: Iterable[dict],
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    status: str | None = None,
) -> dict:
    """Serialize current-run stock results for durable handoff."""
    rows = [dict(stock) for stock in stocks]
    resolved_status = status or (STATUS_SUCCESS if rows else STATUS_NO_NEW)
    return {
        "schema_version": 1,
        "run_id": str(run_id),
        "status": resolved_status,
        "started_at": (to_utc(started_at) if started_at else utc_now()).isoformat(),
        "completed_at": (to_utc(completed_at) if completed_at else utc_now()).isoformat(),
        "stocks": rows,
    }


def write_current_run_payload(path: Path | str, payload: dict[str, Any]) -> None:
    """Atomically write the current-run stock payload."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(target)


__all__ = [
    "DEFAULT_HISTORICAL_LIMIT",
    "DEFAULT_WINDOW_HOURS",
    "EMPTY_STATUSES",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_NO_NEW",
    "STATUS_SUCCESS",
    "STATUS_UNAVAILABLE",
    "StockResult",
    "UNAVAILABLE_STATUSES",
    "build_current_run_payload",
    "load_current_run_stocks",
    "query_latest_assessment_stocks",
    "write_current_run_payload",
]
