#!/usr/bin/env python3
"""Signal outcome study — does the Signal Stack score or the live "actionable"
rule predict forward returns?

Built on the audited outcome logic (tradingview/outcome_logic.py) instead of
the legacy price_snapshots / performance_metrics.return_7d pipeline:

  • Decision time = alerts.created_at, the moment the scored alert existed.
    Every input to the score was observed before it, so measuring from here
    has no look-ahead (the legacy study measured from fire_time while the
    score used data fetched up to days later).
  • Entry = close of the last completed hourly bar at/before decision time.
  • Exit  = hourly bar close nearest decision + 7 days (±24h tolerance).
  • Excess return = stock return minus the exchange's index over the same
    entry and exit bars.
  • Observations come from historical bars fetched at report time because
    the live tracker has too few trusted points to measure a 7-day horizon.
  • Score model versions are reported separately, never pooled.
  • Uncertainty uses a symbol-cluster bootstrap: repeat alerts on the same
    symbol move together and are not independent samples. A verdict needs
    both a minimum alert count and a minimum distinct-symbol count per arm.
  • The comparison uses the engine's stored ``actionable`` flag, not a
    re-derived score cutoff — legacy rows were marked actionable under an
    older policy and can sit below the current threshold.

Limitations: point observations, not executed fills; no costs or slippage;
hourly bars are timestamped at bar start + 1h (conservative for entry).
The database is opened read-only.

Usage:
    python tradingview/backtest/outcome_study.py
    python tradingview/backtest/outcome_study.py --out reports/x.html
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingview.outcome_logic import (  # noqa: E402
    MeasurementOrigin,
    OutcomeStatus,
    PointObservation,
    SignalMeasurement,
    evaluate_outcome,
)

DEFAULT_DB = ROOT / "database" / "alert_tracker.db"
DEFAULT_OUT = ROOT / "reports" / "signal_outcome_study.html"

HORIZON = timedelta(days=7)
HORIZON_TOLERANCE = timedelta(hours=24)
MAX_ENTRY_STALENESS = timedelta(hours=96)   # covers a weekend plus a holiday
MAX_BENCHMARK_STALENESS = timedelta(hours=96)  # index bar must be as fresh as entry
BAR = timedelta(hours=1)
TARGET_PCT, STOP_PCT = 10.0, -5.0            # same as the audited tracker
LIVE_THRESHOLD = 62.5
SCORE_BANDS = [("<55", 0.0, 55.0), ("55–62.5", 55.0, 62.5),
               ("62.5–70", 62.5, 70.0), ("≥70", 70.0, 1000.0)]
MIN_ARM = 30
MIN_SYMBOLS = 10      # distinct symbols per arm — alerts on one symbol are not
                      # independent, so a large alert count can still be n=1
N_BOOT = 2000

_US = ("NASDAQ", "NYSE", "AMEX", "BATS", "CBOE", "ARCA", "NYSEARCA", "OTC")
BENCHMARKS = {**{ex: "SPY" for ex in _US},
              "ASX": "^AXJO", "NSE": "^NSEI", "BSE": "^BSESN", "HKEX": "^HSI",
              "SGX": "^STI", "KRX": "^KS11", "TSE": "^N225", "LSE": "^FTSE",
              "TSX": "^GSPTSE"}

Bars = list[tuple[datetime, float]]   # (bar end UTC, close), ascending


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def load_alerts(db: Path, as_of: datetime) -> list[dict]:
    """Scored alerts whose decision time is at least one horizon old."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT alert_uuid, tv_symbol, yf_ticker, fire_time, created_at,
                      composite_score, actionable,
                      COALESCE(json_extract(result_json, '$.score_model_version'),
                               'legacy (unversioned)')
               FROM alerts
               WHERE composite_score IS NOT NULL AND yf_ticker IS NOT NULL
                 AND yf_ticker != '' AND fire_time IS NOT NULL
                 AND (is_delisted IS NULL OR is_delisted = 0)"""
        ).fetchall()
    finally:
        conn.close()
    alerts = []
    for uuid, tv, yf, fire, created, score, act, version in rows:
        try:
            decision, fire_at = _utc(created), _utc(fire)
        except ValueError:
            continue
        if decision + HORIZON + HORIZON_TOLERANCE > as_of:
            continue
        exchange = tv.split(":", 1)[0].upper() if ":" in tv else "US"
        alerts.append({
            "id": uuid, "symbol": tv, "ticker": yf, "exchange": exchange,
            "benchmark": BENCHMARKS.get(exchange), "fire_at": fire_at,
            "decision_at": decision, "score": float(score),
            "actionable": bool(act), "version": version,
        })
    return alerts


def fetch_hourly_bars(tickers: set[str], start: datetime, end: datetime) -> dict[str, Bars]:
    """Hourly closes per ticker keyed by bar end (UTC)."""
    import yfinance as yf

    out: dict[str, Bars] = {}
    ordered = sorted(tickers)
    for i in range(0, len(ordered), 100):
        chunk = ordered[i:i + 100]
        df = yf.download(chunk, interval="60m", start=start.date().isoformat(),
                         end=(end + timedelta(days=1)).date().isoformat(),
                         group_by="ticker", auto_adjust=True, progress=False,
                         threads=True)
        if df is None or df.empty:
            continue
        for tk in chunk:
            try:
                # yfinance returns MultiIndex columns whenever group_by="ticker"
                # is set — even for a single-ticker batch. Select the ticker
                # level first, then the field, so a 1-ticker chunk is not
                # silently dropped by a KeyError on the flat "Close" label.
                if isinstance(df.columns, pd.MultiIndex):
                    closes = df[tk]["Close"].dropna()
                else:
                    closes = df["Close"].dropna()
            except KeyError:
                continue
            closes = closes[closes > 0]
            if len(closes):
                ends = closes.index.tz_convert("UTC").to_pydatetime() + BAR
                out[tk] = list(zip(ends, closes.astype(float).tolist()))
    return out


def _last_at_or_before(bars: Bars, t: datetime) -> tuple[datetime, float] | None:
    best = None
    for end, close in bars:
        if end > t:
            break
        best = (end, close)
    return best


def evaluate_alert(alert: dict, bars: Bars | None, bench: Bars | None,
                   as_of: datetime) -> dict:
    """Measure one alert from its decision time with the audited outcome logic."""
    result = {**alert, "status": None, "reason": None, "return_pct": None,
              "excess_pct": None, "exit_offset_h": None}
    entry = _last_at_or_before(bars or [], alert["decision_at"])
    if entry is None or alert["decision_at"] - entry[0] > MAX_ENTRY_STALENESS:
        result["reason"] = "no price at decision time"
        return result
    measurement = SignalMeasurement(
        signal_id=alert["id"], origin=MeasurementOrigin.ANALYSIS,
        fire_at=alert["fire_at"], registered_at=alert["decision_at"],
        analysis_at=alert["decision_at"], entry_price=entry[1],
        entry_price_source="yfinance.60m.close", entry_price_at=entry[0],
        entry_available_at=entry[0], target_return_pct=TARGET_PCT,
        stop_return_pct=STOP_PCT, horizon=HORIZON,
        horizon_tolerance=HORIZON_TOLERANCE)
    points = [PointObservation(end, close, end) for end, close in bars or []]
    ev = evaluate_outcome(measurement, points, as_of=as_of)
    result["status"] = ev.status.value
    if not ev.measured:
        result["reason"] = "no price near decision + 7d"
        return result
    result["return_pct"] = ev.fixed_horizon_return_pct
    result["exit_offset_h"] = ev.horizon_offset.total_seconds() / 3600
    b_entry = _last_at_or_before(bench or [], entry[0])
    b_exit = _last_at_or_before(bench or [], ev.horizon_observed_at)
    # A stale index bar would distort the excess return, so require the
    # benchmark to be as fresh as the stock entry/exit it is compared against.
    if (b_entry and b_exit and b_exit[0] > b_entry[0]
            and entry[0] - b_entry[0] <= MAX_BENCHMARK_STALENESS
            and ev.horizon_observed_at - b_exit[0] <= MAX_BENCHMARK_STALENESS):
        result["excess_pct"] = ev.fixed_horizon_return_pct - (b_exit[1] / b_entry[1] - 1) * 100
    else:
        result["reason"] = "no fresh benchmark at entry/exit"
    return result


# ─── Statistics ──────────────────────────────────────────────────────────────

def _cluster_bootstrap(rows: list[dict], stat, seed: int = 7) -> tuple[float, float] | None:
    """95% CI of stat(rows) resampling whole symbols with replacement."""
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)
    groups = list(by_symbol.values())
    if len(groups) < 2:
        return None
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(N_BOOT):
        sample = [r for i in rng.integers(0, len(groups), len(groups)) for r in groups[i]]
        value = stat(sample)
        if value is not None and np.isfinite(value):
            draws.append(value)
    if len(draws) < N_BOOT // 2:
        return None
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def _mean(rows: list[dict], key: str = "excess_pct") -> float | None:
    vals = [r[key] for r in rows if r[key] is not None]
    return float(np.mean(vals)) if vals else None


def _diff(rows: list[dict]) -> float | None:
    a = _mean([r for r in rows if r["actionable"]])
    b = _mean([r for r in rows if not r["actionable"]])
    return None if a is None or b is None else a - b


def _spearman(rows: list[dict]) -> float | None:
    from scipy.stats import spearmanr
    pairs = [(r["score"], r["excess_pct"]) for r in rows if r["excess_pct"] is not None]
    if len(pairs) < 5 or len({p[0] for p in pairs}) < 2:
        return None
    rho = spearmanr(*zip(*pairs)).statistic
    return float(rho) if np.isfinite(rho) else None


def _group_stats(rows: list[dict]) -> dict:
    ex = [r["excess_pct"] for r in rows if r["excess_pct"] is not None]
    raw = [r["return_pct"] for r in rows]
    return {
        "n": len(rows), "n_symbols": len({r["symbol"] for r in rows}),
        "n_excess": len(ex),
        "mean_return": float(np.mean(raw)) if raw else None,
        "mean_excess": float(np.mean(ex)) if ex else None,
        "median_excess": float(np.median(ex)) if ex else None,
        "beat_index_pct": 100 * float(np.mean([e > 0 for e in ex])) if ex else None,
        "target_first_pct": 100 * float(np.mean(
            [r["status"] == OutcomeStatus.OBSERVED_TARGET_FIRST.value for r in rows])) if rows else None,
        "stop_first_pct": 100 * float(np.mean(
            [r["status"] == OutcomeStatus.OBSERVED_STOP_FIRST.value for r in rows])) if rows else None,
    }


def _verdict(ci: tuple[float, float] | None, arms: tuple[int, int],
             symbols: tuple[int, int]) -> str:
    """Classify the actionable−rejected gap.

    ``arms`` are alert counts and ``symbols`` are distinct-symbol counts. Both
    gates are required: repeated alerts on one symbol are not independent
    samples, so a large alert count can still be a single observation.
    """
    if ci is None or min(arms) == 0 or min(symbols) == 0:
        return "INSUFFICIENT DATA"
    if min(arms) < MIN_ARM or min(symbols) < MIN_SYMBOLS:
        return "UNDERPOWERED"
    if ci[0] > 0:
        return "EDGE"
    if ci[1] < 0:
        return "INVERTED"
    return "NO DETECTABLE EDGE"


def analyse_version(rows: list[dict]) -> dict:
    measured = [r for r in rows if r["return_pct"] is not None]
    act = [r for r in measured if r["actionable"]]
    rej = [r for r in measured if not r["actionable"]]
    diff_ci = _cluster_bootstrap(measured, _diff)
    rho_ci = _cluster_bootstrap(measured, _spearman)
    arms = (sum(r["excess_pct"] is not None for r in act),
            sum(r["excess_pct"] is not None for r in rej))
    symbols = (len({r["symbol"] for r in act if r["excess_pct"] is not None}),
               len({r["symbol"] for r in rej if r["excess_pct"] is not None}))
    return {
        "funnel": {
            "matured_alerts": len(rows),
            "no_price_at_decision": sum(r["reason"] == "no price at decision time" for r in rows),
            "no_price_at_exit": sum(r["reason"] == "no price near decision + 7d" for r in rows),
            "no_fresh_benchmark": sum(r["reason"] == "no fresh benchmark at entry/exit" for r in rows),
            "measured": len(measured),
            "with_benchmark": sum(r["excess_pct"] is not None for r in measured),
            "min_actionable_score": min((r["score"] for r in act), default=None),
            "median_abs_exit_offset_h": float(np.median([abs(r["exit_offset_h"]) for r in measured])) if measured else None,
            "median_fire_to_decision_h": float(np.median(
                [(r["decision_at"] - r["fire_at"]).total_seconds() / 3600 for r in rows])) if rows else None,
        },
        "live_rule": {
            "actionable": _group_stats(act), "rejected": _group_stats(rej),
            "diff_mean_excess": _diff(measured), "diff_ci95": diff_ci,
            "arms": {"actionable": arms[0], "rejected": arms[1]},
            "symbols": {"actionable": symbols[0], "rejected": symbols[1]},
            "verdict": _verdict(diff_ci, arms, symbols),
        },
        "score": {
            "spearman_rho": _spearman(measured), "rho_ci95": rho_ci,
            "bands": {label: _group_stats([r for r in measured if lo <= r["score"] < hi])
                      for label, lo, hi in SCORE_BANDS},
        },
    }


# ─── Report ──────────────────────────────────────────────────────────────────

def _f(v, fmt="{:+.2f}%") -> str:
    return "—" if v is None else fmt.format(v)


def _ci(ci, fmt="{:+.2f}") -> str:
    return "—" if ci is None else f"[{fmt.format(ci[0])}, {fmt.format(ci[1])}]"


def _stats_row(label: str, s: dict) -> str:
    return (f"<tr><td>{html.escape(label)}</td><td>{s['n']}</td><td>{s['n_symbols']}</td>"
            f"<td>{_f(s['mean_return'])}</td><td>{_f(s['mean_excess'])}</td>"
            f"<td>{_f(s['median_excess'])}</td><td>{_f(s['beat_index_pct'], '{:.0f}%')}</td>"
            f"<td>{_f(s['target_first_pct'], '{:.0f}%')}</td><td>{_f(s['stop_first_pct'], '{:.0f}%')}</td></tr>")


def build_html(results: dict[str, dict], as_of: datetime) -> str:
    head = ("<tr><th>Group</th><th>Alerts</th><th>Symbols</th><th>Mean 7d return</th>"
            "<th>Mean excess vs index</th><th>Median excess</th><th>Beat index</th>"
            f"<th>+{TARGET_PCT:.0f}% first</th><th>{STOP_PCT:.0f}% first</th></tr>")
    sections = []
    for version, res in results.items():
        fn, lr, sc = res["funnel"], res["live_rule"], res["score"]
        bands = "".join(_stats_row(k, v) for k, v in sc["bands"].items())
        sections.append(f"""
<h2>Score model: {html.escape(version)}</h2>
<p class="funnel">Matured alerts {fn['matured_alerts']} → measured {fn['measured']}
 (no price at decision {fn['no_price_at_decision']}, no price at exit {fn['no_price_at_exit']})
 → with index benchmark {fn['with_benchmark']} (no fresh benchmark {fn['no_fresh_benchmark']}).
 Lowest actionable score in this version
 {_f(fn['min_actionable_score'], '{:.2f}')}. Median |exit offset| {_f(fn['median_abs_exit_offset_h'], '{:.1f}h')};
 median fire→decision lag {_f(fn['median_fire_to_decision_h'], '{:.1f}h')}.</p>
<h3>Engine eligibility: stored <code>actionable</code> flag vs rejected</h3>
<table>{head}{_stats_row('Actionable', lr['actionable'])}{_stats_row('Rejected', lr['rejected'])}</table>
<p class="verdict">Actionable − rejected mean excess: <b>{_f(lr['diff_mean_excess'])}</b>,
 95% symbol-cluster CI {_ci(lr['diff_ci95'])} → <b>{lr['verdict']}</b>
 (arms {lr['arms']['actionable']}/{lr['arms']['rejected']} alerts,
 {lr['symbols']['actionable']}/{lr['symbols']['rejected']} symbols;
 needs ≥{MIN_ARM} alerts and ≥{MIN_SYMBOLS} symbols per arm)</p>
<p class="note">This compares the engine's own stored <code>actionable</code> decision, not a
 re-derived score cutoff. Legacy rows were marked actionable under an older policy and can
 sit below {LIVE_THRESHOLD} — the current model's minimum actionable score is shown per
 version above. Read the gap as "did the engine's eligibility call separate outcomes",
 not as "does a {LIVE_THRESHOLD} cutoff work".</p>
<h3>Score bands</h3>
<table>{head}{bands}</table>
<p>Spearman ρ (score vs excess return): <b>{_f(sc['spearman_rho'], '{:+.3f}')}</b>,
 95% symbol-cluster CI {_ci(sc['rho_ci95'], '{:+.3f}')}</p>""")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Outcome Study</title><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;background:#0f1117;color:#e6e6e6;margin:24px;max-width:1100px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 4px}}
th,td{{border-bottom:1px solid #2a2e39;padding:6px 8px;text-align:right}}
th:first-child,td:first-child{{text-align:left}} th{{color:#9aa4b2;font-weight:600}}
h2{{border-top:1px solid #2a2e39;padding-top:16px}} .funnel,.note{{color:#9aa4b2}}
.verdict{{background:#1a1f2b;padding:8px 12px;border-radius:6px}}
</style></head><body>
<h1>Signal Outcome Study</h1>
<p class="note">As of {as_of:%Y-%m-%d %H:%M} UTC. Entry = last completed hourly close at the alert's
decision time (when the scored alert was stored), so no score input post-dates the entry.
Return = close nearest decision + 7 days (±24h). Excess = minus the exchange index over the same bars,
and is dropped when the index bar is more than {MAX_BENCHMARK_STALENESS.total_seconds() / 3600:.0f}h from the
stock's entry or exit. Observed point samples, not executed trades: no costs, slippage or fills.
Verdicts need ≥{MIN_ARM} benchmarked alerts and ≥{MIN_SYMBOLS} distinct symbols per arm, and a 95%
symbol-cluster bootstrap CI that excludes zero.</p>
{''.join(sections)}
</body></html>"""


def run(db: Path, out: Path, as_of: datetime | None = None, fetch=fetch_hourly_bars) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    alerts = load_alerts(db, as_of)
    if not alerts:
        results: dict[str, dict] = {}
    else:
        tickers = {a["ticker"] for a in alerts} | {a["benchmark"] for a in alerts if a["benchmark"]}
        start = min(a["decision_at"] for a in alerts) - MAX_ENTRY_STALENESS
        bars = fetch(tickers, start, as_of)
        evaluated = [evaluate_alert(a, bars.get(a["ticker"]), bars.get(a["benchmark"]), as_of)
                     for a in alerts]
        by_version: dict[str, list[dict]] = {}
        for r in evaluated:
            by_version.setdefault(r["version"], []).append(r)
        # Newest model first; the unversioned legacy model last.
        order = sorted(by_version, key=lambda v: (v.startswith("legacy"), v), reverse=False)
        order = [v for v in order if not v.startswith("legacy")][::-1] + [v for v in order if v.startswith("legacy")]
        results = {v: analyse_version(by_version[v]) for v in order}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(results, as_of), encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "versions": results}, indent=2, default=str),
        encoding="utf-8")
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    results = run(args.db, args.out)
    for version, res in results.items():
        lr, fn = res["live_rule"], res["funnel"]
        print(f"{version}: measured {fn['measured']}/{fn['matured_alerts']}  "
              f"actionable−rejected excess {_f(lr['diff_mean_excess'])} "
              f"CI {_ci(lr['diff_ci95'])} → {lr['verdict']}  "
              f"(arms {lr['arms']['actionable']}/{lr['arms']['rejected']}, "
              f"symbols {lr['symbols']['actionable']}/{lr['symbols']['rejected']})  "
              f"ρ={_f(res['score']['spearman_rho'], '{:+.3f}')}")
    print(f"Report: {args.out}")


if __name__ == "__main__":
    main()
