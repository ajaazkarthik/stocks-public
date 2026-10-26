"""Point-in-time options forecast research with no paid history, orders, or alerts.

Capture predictions BEFORE their targets. Three comparable arms:
  unchanged: flat stock/unchanged observed option IV
  momentum: 5-day log stock drift/unchanged option IV
  path_proxy: weighted historical-stock-path and assumed option-IV projection
The third arm is NOT a calibrated probability model or executable forecast.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "options_forecast_vintage_v1"
DDL = """CREATE TABLE IF NOT EXISTS forecast_vintage (
 id TEXT PRIMARY KEY, source_symbol TEXT NOT NULL, decision_at TEXT NOT NULL,
 target_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_outcome (
 id TEXT NOT NULL, observed_at TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(id,observed_at));"""


def aware(t):
    if not isinstance(t, str):
        raise ValueError("TIMESTAMP_STRING_REQUIRED")
    d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return d.astimezone(timezone.utc)


def number(x, positive=False):
    if isinstance(x, bool):
        raise ValueError("INVALID_BOOLEAN_NUMBER")
    val = float(x)
    if not math.isfinite(val) or (positive and val <= 0):
        raise ValueError("INVALID_NUMBER")
    return val


def normalize_iv(quote):
    v = number(quote["iv"], positive=True)
    if quote.get("provider") == "TradingView scan2":
        v /= 100.0
    if not .01 <= v <= 5:
        raise ValueError("IV_OUT_OF_RANGE")
    return v


def make_vintage(*, symbol, observed_at, created_at, target_at,
                 completed_bars, exact_quotes, path_predictions, observed_spot, future_sessions):
    """All historical bars must precede the decision's UTC date.

    exact_quotes: [{'key', 'iv', 'provider'}, ...]
    path_predictions: [{probability, spot, iv_by_key: {key: fraction}}, ...]
    Input paths must be calculated without future data at decision time.
    """
    decision, created, target = (aware(v) for v in (observed_at, created_at, target_at))
    if not decision <= created < target:
        raise ValueError("LOOKAHEAD_OR_TARGET_NOT_FUTURE")
    bars = [(aware(t), number(price, positive=True)) for t, price in completed_bars]
    if len(bars) < 21 or any(a[0] >= b[0] for a, b in zip(bars, bars[1:])):
        raise ValueError("INSUFFICIENT_OR_UNORDERED_BARS")
    if bars[-1][0].date() >= decision.date():
        raise ValueError("UNFINISHED_OR_FUTURE_BAR")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("MISSING_SYMBOL")
    names = [q["key"] for q in exact_quotes]
    if not names or len(names) != len(set(names)):
        raise ValueError("DUPLICATE_OR_MISSING_CONTRACTS")
    if len(path_predictions) < 2:
        raise ValueError("TOO_FEW_PATHS")
    weights = [number(p["probability"]) for p in path_predictions]
    if min(weights) < 0 or not math.isclose(sum(weights), 1, rel_tol=0, abs_tol=1e-9):
        raise ValueError("PATH_WEIGHTS_INVALID")
    current_spot = number(observed_spot, positive=True)
    if type(future_sessions) is not int or future_sessions < 1 or future_sessions > 60:
        raise ValueError("INVALID_FORECAST_SESSION_HORIZON")
    drift = math.log(bars[-1][1] / bars[-6][1]) / 5
    horizon_sessions = future_sessions
    options = {}
    for q in exact_quotes:
        key = q["key"]
        iv = normalize_iv(q)
        values = []
        for path, weight in zip(path_predictions, weights):
            predicted_iv = number(path["iv_by_key"][key], positive=True)
            if not .01 <= predicted_iv <= 5:
                raise ValueError("INVALID_PATH_IV")
            values.append((number(path["spot"], positive=True), predicted_iv, weight))
        options[key] = {
            "unchanged": {"spot": current_spot, "iv": iv},
            "momentum": {"spot": current_spot * math.exp(drift * horizon_sessions), "iv": iv},
            "path_proxy": {
                "spot": sum(spot * p for spot, vol, p in values),
                "iv": sum(vol * p for spot, vol, p in values),
                "iv_min": min(vol for spot, vol, p in values),
                "iv_max": max(vol for spot, vol, p in values),
                "interval_kind": "SCENARIO_ENVELOPE_NOT_80_PERCENT_CALIBRATED",
            },
        }
    v = {
        "schema": SCHEMA, "symbol": symbol,
        "decision_at": decision.isoformat(), "created_at": created.isoformat(),
        "target_at": target.isoformat(), "source_completed_bar": bars[-1][0].isoformat(),
        "predictions": options, "provenance": "REFERENCE_MODEL_UNVERIFIED",
        "option_iv_calibrated": False, "broker_fills": 0, "notifier_eligible": False,
    }
    v["id"] = hashlib.sha256(json.dumps(v, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return v


def _private(path):
    p = Path(path).expanduser().resolve()
    if "database" in p.parts or p.name in {"alert_tracker.db", "options_paper.db", "tqs_v2.db", "zerodte.db"}:
        raise ValueError("PRODUCTION_DB_FORBIDDEN")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not p.exists():
        os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    if p.stat().st_mode & 0o077:
        raise PermissionError("OWNER_ONLY_0600_REQUIRED")
    return p


def record(path, vintage):
    if vintage.get("schema") != SCHEMA or aware(vintage["target_at"]) <= aware(vintage["created_at"]):
        raise ValueError("INVALID_OR_RETROACTIVE_FORECAST")
    copy = dict(vintage)
    supplied_id = copy.pop("id", "")
    if hashlib.sha256(json.dumps(copy, sort_keys=True, allow_nan=False).encode()).hexdigest() != supplied_id:
        raise ValueError("VINTAGE_HASH_MISMATCH")
    p = _private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        body = json.dumps(vintage, sort_keys=True, allow_nan=False)
        original = db.execute("SELECT body FROM forecast_vintage WHERE id=?", (vintage["id"],)).fetchone()
        if original is not None and original[0] != body:
            raise ValueError("IMMUTABLE_VINTAGE_VIOLATION")
        db.execute("INSERT OR IGNORE INTO forecast_vintage VALUES (?,?,?,?,?)",
                   (vintage["id"], vintage["symbol"], vintage["decision_at"], vintage["target_at"], body))
    return vintage["id"]


def match_future(path, *, symbol, observed_at, exact_quotes, observed_spot, max_lag_days=3):
    """Later reference quote is an outcome *observation*, not a fill or side-event proof."""
    now = aware(observed_at)
    qmap = {q["key"]: q for q in exact_quotes}
    if len(qmap) != len(exact_quotes):
        raise ValueError("DUPLICATE_FUTURE_CONTRACT")
    p = _private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        historical = db.execute("SELECT body FROM forecast_vintage WHERE source_symbol=?", (symbol,)).fetchall()
        saved = 0
        for (body,) in historical:
            v = json.loads(body)
            lag = (now.date() - aware(v["target_at"]).date()).days
            if lag < 0 or lag > max_lag_days or now < aware(v["target_at"]):
                continue
            outcomes = {}
            for key in v["predictions"]:
                if key not in qmap:
                    outcomes[key] = {"status": "MISSING_EXACT_CONTRACT"}
                else:
                    try:
                        outcomes[key] = {"status": "LATER_REFERENCE_ONLY", "spot": number(observed_spot, True),
                                         "iv": normalize_iv(qmap[key]), "exchange_side_times_verified": False}
                    except (KeyError, TypeError, ValueError):
                        outcomes[key] = {"status": "INVALID_LATER_REFERENCE"}
            result = {"vintage_id": v["id"], "observed_at": now.isoformat(),
                      "evidence": "REFERENCE_NOT_BROKER_FILL", "contracts": outcomes, "actual_fills": 0}
            with db:
                cur = db.execute("INSERT OR IGNORE INTO forecast_outcome VALUES(?,?,?)",
                                 (v["id"], now.isoformat(), json.dumps(result, sort_keys=True, allow_nan=False)))
            saved += cur.rowcount
    return {"new_outcomes": saved, "actual_fills": 0}


def compare(path):
    p = _private(path)
    with sqlite3.connect(p) as db:
        db.executescript(DDL)
        rows = db.execute("SELECT v.body,o.body FROM forecast_vintage v "
                          "JOIN forecast_outcome o USING(id)").fetchall()
    dates = set()
    metrics = {}
    completed = set()
    seen = set()
    for vb, ob in sorted(rows, key=lambda pair: json.loads(pair[1])["observed_at"]):
        vintage, outcome = json.loads(vb), json.loads(ob)
        completed.add(vintage["id"])
        for key, value in outcome["contracts"].items():
            if value["status"] != "LATER_REFERENCE_ONLY":
                continue
            if (vintage["id"], key) in seen:
                continue
            seen.add((vintage["id"], key))
            dates.add(vintage["target_at"][:10])
            for name, pred in vintage["predictions"][key].items():
                item = metrics.setdefault(name, {"pairs": 0, "spot_abs": 0., "iv_abs": 0., "iv_envelope_hits": 0})
                item["pairs"] += 1
                item["spot_abs"] += abs(value["spot"] - pred["spot"])
                item["iv_abs"] += abs(value["iv"] - pred["iv"])
                if "iv_min" in pred and pred["iv_min"] <= value["iv"] <= pred["iv_max"]:
                    item["iv_envelope_hits"] += 1
    for v in metrics.values():
        count = v["pairs"]
        v["spot_mae"] = round(v.pop("spot_abs") / count, 7)
        v["iv_mae"] = round(v.pop("iv_abs") / count, 7)
        v["empirical_envelope_rate"] = round(v.pop("iv_envelope_hits") / count, 4)
    return {"matched_vintages": len(completed), "later_observation_rows": len(rows),
            "independent_target_dates": len(dates),
            "models": metrics, "nominal_80_percent_calibration_proven": False,
            "actual_trading_profit": "NOT_MEASURED"}
