"""Durable scanner setup identity and append-only scoring observations.

Scanner observations repeat; a setup is the daily market opportunity.  This
store keeps those concepts separate and namespaces explicit test runs away
from production identities.  It performs no implicit database access.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanner_setups (
 setup_id TEXT PRIMARY KEY,
 scope TEXT NOT NULL,
 market TEXT NOT NULL,
 pro_symbol TEXT NOT NULL,
 direction TEXT NOT NULL,
 session_date TEXT NOT NULL,
 first_run_id TEXT NOT NULL,
 first_observed_at TEXT NOT NULL,
 first_result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(scope, market, pro_symbol, direction, session_date)
);
CREATE TABLE IF NOT EXISTS scanner_observations (
 observation_id TEXT PRIMARY KEY,
 setup_id TEXT NOT NULL,
 scope TEXT NOT NULL,
 market TEXT NOT NULL,
 pro_symbol TEXT NOT NULL,
 direction TEXT NOT NULL,
 session_date TEXT NOT NULL,
 run_id TEXT NOT NULL,
 observed_at TEXT,
 actionable INTEGER NOT NULL,
 result_json TEXT NOT NULL,
 recorded_at TEXT NOT NULL,
 UNIQUE(scope, market, pro_symbol, direction, session_date, run_id)
);
CREATE TABLE IF NOT EXISTS scanner_scoring_results (
 setup_id TEXT PRIMARY KEY,
 scope TEXT NOT NULL,
 run_id TEXT NOT NULL,
 scored_at TEXT NOT NULL,
 payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scanner_observations_setup
 ON scanner_observations(setup_id, recorded_at);
"""


def ensure_schema(conn) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def normalize_scope(value: Any) -> str:
    scope = _text(value).lower()
    if scope not in {"production", "test"}:
        raise ValueError("scanner scope must be explicitly production or test")
    return scope


def setup_identity(*, scope: str, market: str, pro_symbol: str,
                   direction: str, session_date: str) -> str:
    values = {
        "scope": normalize_scope(scope),
        "market": _text(market).upper(),
        "pro_symbol": _text(pro_symbol).upper(),
        "direction": _text(direction).upper(),
        "session_date": _text(session_date),
    }
    if not all(values.values()):
        raise ValueError("scanner setup identity requires scope, market, symbol, direction, and session date")
    raw = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_observation(conn, result: dict[str, Any], *, run_id: str,
                       scope: str = "production") -> tuple[str, bool, dict[str, Any]]:
    """Append one run observation; create setup/performance identity once.

    The first actionable result and its entry evidence are immutable. Repeated
    runs add observation rows and cannot overwrite that initial evidence.
    """
    ensure_schema(conn)
    scope = normalize_scope(scope)
    market = _text(result.get("market")).upper()
    pro_symbol = _text(result.get("tv_symbol") or result.get("pro_symbol")).upper()
    direction = _text(result.get("direction") or result.get("signal")).upper()
    session_date = _text(result.get("session_date"))
    setup_id = setup_identity(scope=scope, market=market, pro_symbol=pro_symbol,
                              direction=direction, session_date=session_date)
    run_id = _text(run_id)
    if not run_id:
        raise ValueError("scanner observation requires a run id")
    safe_result = _json_safe(result)
    encoded = json.dumps(safe_result, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    actionable = result.get("actionable") is True
    observed_at = _text(result.get("timestamp")) or None
    now = datetime.now(timezone.utc).isoformat()
    observation_id = hashlib.sha256(f"{setup_id}:{run_id}".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO scanner_observations "
        "(observation_id,setup_id,scope,market,pro_symbol,direction,session_date,run_id,observed_at,actionable,result_json,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, setup_id, scope, market, pro_symbol, direction, session_date,
         run_id, observed_at, int(actionable), encoded, now),
    )
    created = False
    if actionable:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO scanner_setups "
            "(setup_id,scope,market,pro_symbol,direction,session_date,first_run_id,first_observed_at,first_result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (setup_id, scope, market, pro_symbol, direction, session_date, run_id,
             observed_at or "", encoded, now),
        )
        created = cursor.rowcount == 1
        if created:
            payload = {
                "run_id": run_id,
                "setup_id": setup_id,
                "scope": scope,
                "scored_at": now,
                "pro_symbol": pro_symbol,
                "fire_time": safe_result.get("fire_time") or "",
                "resolution": safe_result.get("resolution") or "",
                "market": market,
                "session_date": session_date,
                "result": safe_result,
            }
            conn.execute(
                "INSERT OR IGNORE INTO scanner_scoring_results(setup_id,scope,run_id,scored_at,payload_json) "
                "VALUES (?,?,?,?,?)",
                (setup_id, scope, run_id, now, json.dumps(payload, sort_keys=True, default=str, allow_nan=False)),
            )
    row = conn.execute(
        "SELECT first_result_json FROM scanner_setups WHERE setup_id=?", (setup_id,)
    ).fetchone()
    if row is None:
        first_result = safe_result
    else:
        first_result = json.loads(row[0])
    return setup_id, created, first_result


def load_scoring_results(conn, *, scope: str = "production", limit: int | None = None) -> list[dict]:
    """Load first-actionable setup results for durable notification recovery."""
    ensure_schema(conn)
    scope = normalize_scope(scope)
    sql = "SELECT payload_json FROM scanner_scoring_results WHERE scope=? ORDER BY scored_at DESC"
    params: list[Any] = [scope]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    out = []
    for row in conn.execute(sql, params):
        try:
            out.append(json.loads(row[0]))
        except (TypeError, ValueError):
            continue
    return out
