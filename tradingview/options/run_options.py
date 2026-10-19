#!/usr/bin/env python3
"""
Options Analyzer — CLI Runner
==============================
Standalone script to analyze F&O options for actionable buy signals.

Reads the last N actionable signals from the signal_stack and runs
options analysis on each symbol.

Can also analyze individual symbols manually.

Usage:
  # Analyze actionable signals from last run:
  python run_options.py --from-actionable --mode BALANCED

  # Analyze a specific symbol:
  python run_options.py --symbol NASDAQ:AAPL --mode AGGRESSIVE --score 72

  # Analyze multiple specific symbols:
  python run_options.py --symbols "NASDAQ:AAPL,NSE:INFY,NYSE:JPM" --mode BALANCED

  # India symbols only:
  python run_options.py --from-actionable --india-only --mode SAFE

  # US symbols only:
  python run_options.py --from-actionable --us-only --mode AGGRESSIVE

  # All three modes side-by-side for one symbol:
  python run_options.py --symbol NASDAQ:NVDA --all-modes

Output:
  options_report.html (in tradingview/reports/)
  Also prints a text summary to console.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure Unicode output works on Windows PowerShell (cp1252 default)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timezone
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent.resolve()           # tradingview/options/
TV_DIR       = SCRIPT_DIR.parent.resolve()               # tradingview/
ROOT_DIR     = TV_DIR.parent.resolve()                   # project root
REPORTS_DIR  = ROOT_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Signal Stack (menu option 14) writes reports/actionable_report.json — this is
# the PRIMARY source: it has real L1-L4 composite scores for freshly-ranked
# signals. actionable_log.json is written ONLY by webhook_server.py (a
# separate live-trading process) and is used as a fallback for symbols that
# arrived via live webhook alerts rather than a signal_stack.py run.
# Fixed 2026-07-06: this file previously claimed signal_stack.py wrote
# actionable_log.json, which it never did — the auto-chain from main.py
# option 14 was silently falling through to _augment_from_fires()'s flat
# composite=55.0 default for every symbol, discarding all L1-L4 scoring.
ACTIONABLE_REPORT_JSON = REPORTS_DIR / "actionable_report.json"
ACTIONABLE_LOG = TV_DIR / "actionable_log.json"
ACTIONABLE_LOG_ALT = ROOT_DIR / "reports" / "actionable_log.json"
FIRES_RAW      = TV_DIR / "fires_raw.json"

# Valid exchange sets for options analysis
_US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "BATS", "CBOE", "ARCA"})
_IN_EXCHANGES = frozenset({"NSE", "BSE"})


class StrictActionableReportError(ValueError):
    """Raised when strict automatic loading cannot validate its report."""


def _read_strict_actionable_report(path: Path, expected_run_id: str) -> tuple[dict | None, str]:
    """Read and validate the report contract used by automatic option-14 runs."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "top-level JSON value is not an object"
        if data.get("run_id") != expected_run_id:
            return None, "run_id does not match the expected option-14 run"
        generated_at = data.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.strip():
            return None, "generated_at is missing"
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            return None, "generated_at must be an explicit UTC timestamp"
        if data.get("status") != "complete":
            return None, "report status is not complete"
        if data.get("is_watch"):
            return None, "report is WATCH-only"
        signals = data.get("actionable_signals")
        if not isinstance(signals, list):
            return None, "actionable_signals is not a list"
        if data.get("n_actionable") != len(signals):
            return None, "n_actionable does not match actionable_signals"
        for signal in signals:
            if not isinstance(signal, dict) or not signal.get("symbol"):
                return None, "an actionable candidate has no symbol"
            if signal.get("decision_score", signal.get("score")) is None:
                return None, "an actionable candidate has no score"
        return data, "valid"
    except FileNotFoundError:
        return None, "actionable report is missing"
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return None, f"actionable report is malformed: {exc}"
    except OSError as exc:
        return None, f"actionable report is unreadable: {exc}"


def _market_is_open(exchange: str, now_utc: datetime | None = None) -> bool:
    """Return whether the exchange's regular options session is open."""
    now = now_utc or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60.0
    exchange_upper = exchange.upper()
    if exchange_upper in _IN_EXCHANGES:
        return 3.75 <= hour <= 10.0
    if exchange_upper in _US_EXCHANGES:
        return 13.5 <= hour <= 21.0
    return False

sys.path.insert(0, str(ROOT_DIR))   # for html_utils (lives at project root)
sys.path.insert(0, str(TV_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

# ─── ANSI ────────────────────────────────────────────────────────────────────
from html_utils import BOLD, DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, RESET  # B-5
from options.nse_fo_list import is_fo_eligible


def _print_header(text: str) -> None:
    bar = "─" * (len(text) + 4)
    print(f"\n{CYAN}{BOLD}┌{bar}┐\n│  {text}  │\n└{bar}┘{RESET}\n")


def _augment_from_fires(signals: list[dict]) -> None:
    """Append recent fires (≤72 h) from fires_raw.json not already in signals."""
    if not FIRES_RAW.exists():
        return
    try:
        from datetime import timezone, timedelta
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=72)
        data  = json.loads(FIRES_RAW.read_text(encoding="utf-8"))
        fires = data.get("all", []) if isinstance(data, dict) else []
        existing: set[str] = {
            (e.get("tv_symbol") or e.get("symbol") or e.get("pro_symbol") or "").upper()
            for e in signals
        }
        for fire in fires:
            sym = fire.get("pro_symbol") or fire.get("symbol") or ""
            if not sym or sym.upper() in existing:
                continue
            ts_str = fire.get("fire_time") or fire.get("bar_time") or ""
            try:
                ts = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts < cutoff:
                break  # fires_raw is newest-first — stop when too old
            exch = sym.split(":")[0].upper() if ":" in sym else ""
            if exch not in (_US_EXCHANGES | _IN_EXCHANGES):
                continue
            signals.append({
                "tv_symbol": sym,
                "composite": 55.0,   # default — no L1-L4 done yet
                "fire_time": ts_str,
                "_from_fires": True,
            })
            existing.add(sym.upper())
    except Exception:
        pass


def _load_actionable_signals(limit: int = 20,
                             actionable_file: Path | None = None,
                             strict: bool = False,
                             expected_run_id: str | None = None) -> list[dict]:
    """Load the most recent actionable signals.

    PRIMARY: reports/actionable_report.json — signal_stack.py's real L1-L4
             scored output from the most recent option-14 run.
    FALLBACK: actionable_log.json / reports/actionable_log.json — webhook_server.py's
             live-trading signal log (used when no signal_stack.py report exists,
             or standalone webhook-driven usage).

    When *actionable_file* is given the caller is scoping the run to one report
    (e.g. holdings), so the webhook fallback and recent-fire augmentation are
    skipped — mixing in unrelated symbols would silently widen that scope.
    Strict automatic mode additionally validates the report contract and never
    consults fallback sources; manual mode retains the historical fallback.
    """
    signals: list[dict] = []
    scoped = actionable_file is not None or strict
    source_json = Path(actionable_file) if scoped else ACTIONABLE_REPORT_JSON

    if strict:
        if not expected_run_id:
            raise StrictActionableReportError("strict actionable mode requires an expected run ID")
        report_data, reason = _read_strict_actionable_report(source_json, expected_run_id)
        if report_data is None:
            raise StrictActionableReportError(reason)
        if not report_data["actionable_signals"]:
            print(f"{YELLOW}No eligible actionable option symbols found; options analysis skipped.{RESET}")
            return []
        for entry in report_data["actionable_signals"]:
            sym = entry.get("symbol")
            signals.append({
                **entry,
                "tv_symbol":    sym,
                "composite":    entry.get("decision_score", entry.get("score")),
                "l1":           entry.get("l1"),
                "l2":           entry.get("l2"),
                "l3":           entry.get("l3"),
                "l4":           entry.get("l4"),
                "generated_at": report_data.get("generated_at"),
                "_source":      "signal_stack",
            })
    elif source_json.exists():
        try:
            report_data = json.loads(source_json.read_text(encoding="utf-8"))
            for entry in report_data.get("actionable_signals", []):
                sym = entry.get("symbol")
                if not sym:
                    continue
                signals.append({
                    **entry,
                    "tv_symbol":    sym,
                    "composite":    entry.get("decision_score", entry.get("score")),
                    "l1":           entry.get("l1"),
                    "l2":           entry.get("l2"),
                    "l3":           entry.get("l3"),
                    "l4":           entry.get("l4"),
                    "generated_at": report_data.get("generated_at"),
                    "_source":      "signal_stack",
                })
        except Exception as exc:
            print(f"{YELLOW}Could not load {source_json.name}: {exc}{RESET}")
    elif scoped:
        print(f"{YELLOW}Actionable file not found: {source_json}{RESET}")
        return []

    # Use whichever log file is more recently written
    candidates = [p for p in (ACTIONABLE_LOG, ACTIONABLE_LOG_ALT) if p.exists()]
    if candidates:
        log_path = max(candidates, key=lambda p: p.stat().st_mtime)
    else:
        log_path = None

    if not signals and log_path and not scoped:
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                signals = data
            elif isinstance(data, dict):
                signals = data.get("signals", data.get("entries", []))
        except Exception as exc:
            print(f"{YELLOW}Could not load {log_path.name}: {exc}{RESET}")

    # Pull in any recent fires not yet in the log
    if not scoped:
        _augment_from_fires(signals)

    if not signals:
        print(
            f"{YELLOW}No eligible actionable option symbols found — signals may be "
            f"outside supported markets or NSE F&O eligibility.{RESET}"
        )
        return []

    # Rank by the actual signal score before limiting. reports/actionable_report.json
    # is already descending, but fallback logs are not guaranteed to be ordered.
    signals.sort(
        key=lambda item: float(item.get("composite") or item.get("score") or 0),
        reverse=True,
    )

    # Deduplicate by symbol and reject unsupported/permanently ineligible markets
    # before any options API call is made.
    seen: set[str] = set()
    unique: list[dict] = []
    for s in signals:
        sym = s.get("tv_symbol") or s.get("symbol") or s.get("s") or ""
        if not sym or sym.upper() == "UNKNOWN":
            continue
        exch = sym.split(":")[0].upper() if ":" in sym else ""
        if exch and exch not in (_US_EXCHANGES | _IN_EXCHANGES):
            continue   # skip ASX, SGX, SGX_DLY, TSX, etc.
        if exch in _IN_EXCHANGES and not is_fo_eligible(sym):
            continue
        if sym not in seen:
            seen.add(sym)
            unique.append(s)
        if len(unique) >= limit:
            break
    return unique


def _run_analysis(symbols_with_scores: list[tuple[str, float]],
                  mode_str: str,
                  access_token: str | None = None,
                  verbose: bool = False,
                  puts_mode: bool = False,
                  signal_meta: dict | None = None) -> list[dict]:
    """
    Run OptionsAnalyzer for each (symbol, score) pair.
    Returns list of result dicts enriched with signal context.

    Parameters
    ----------
    signal_meta : dict | None
        Mapping tv_symbol -> full signal dict from actionable_log.json.
        When provided, L1-L4 scores, news_sentiment, earnings, PCR are
        merged into each result for DB persistence and analytics.
    """
    from options.options_analyzer import OptionsAnalyzer, StrategyMode

    try:
        mode = StrategyMode(mode_str.upper())
    except ValueError:
        print(f"{RED}Invalid mode '{mode_str}'. Choose SAFE / BALANCED / AGGRESSIVE{RESET}")
        mode = StrategyMode.BALANCED

    analyzer = OptionsAnalyzer(verbose=verbose)
    results: list[dict] = []
    _meta = signal_meta or {}

    total = len(symbols_with_scores)
    for i, (sym, score) in enumerate(symbols_with_scores, 1):
        print(f"  [{i}/{total}] {CYAN}{sym}{RESET} (signal {score:.0f}) ...", end="", flush=True)
        try:
            result = analyzer.analyze(
                pro_symbol   = sym,
                signal_score = score,
                mode         = mode,
                access_token = access_token,
                puts_mode    = puts_mode,
            )
            # ── Merge signal-level metadata for DB persistence ────────────
            sig = _meta.get(sym) or _meta.get(sym.upper()) or {}
            if sig:
                _scores = sig.get("scores") or {}
                if not _scores:
                    _scores = {
                        "l1": {"total": sig.get("l1")},
                        "l2": {"total": sig.get("l2")},
                        "l3": {"total": sig.get("l3")},
                        "l4": {"total": sig.get("l4")},
                    }
                _sent   = sig.get("sentiment_data") or {}
                _news   = _sent.get("news") or {}
                _mi     = sig.get("market_internals") or {}
                result["_signal_meta"] = {
                    "run_id":                sig.get("run_id"),
                    "model_version":         sig.get("model_version"),
                    "quality_score":         sig.get("quality_score"),
                    "decision_score":        sig.get("decision_score", sig.get("score")),
                    "data_confidence":       sig.get("data_confidence"),
                    "l1_score":             (_scores.get("l1") or {}).get("total"),
                    "l2_score":             (_scores.get("l2") or {}).get("total"),
                    "l3_score":             (_scores.get("l3") or {}).get("total"),
                    "l4_score":             (_scores.get("l4") or {}).get("total"),
                    "news_sentiment_score": _news.get("score"),
                    "news_count":           _news.get("count"),
                    "earnings_days_away":   _sent.get("earnings_days_away"),
                    "earnings_risk":        _sent.get("earnings_risk"),
                    "pcr":                  _mi.get("pcr"),
                }
            results.append(result)
            avail = result.get("options_available", False)
            n_top = len(result.get("top_contracts", []))
            if avail and n_top:
                best = result["top_contracts"][0]
                opt_score = best.get("option_score", 0)
                market    = result.get("market", "US")
                currency  = "₹" if market == "NSE" else "$"
                strike    = best.get("strike", 0)
                mid       = best.get("mid_price", 0)
                print(f" {GREEN}✓ {n_top} contracts | Best {opt_score:.0f}/100 "
                      f"| CALL {currency}{strike:,.0f} @ {currency}{mid:.2f}{RESET}")
            elif avail:
                print(f" {YELLOW}Available but no contracts passed filters{RESET}")
            else:
                reason = result.get("no_options_reason", "unavailable")
                _perm  = "NOT in" in reason and ("eligible" in reason or "F&O" in reason)
                _temp  = any(kw in reason.lower() for kw in
                             ("market closed", "ab9019", "after hour", "outside trading",
                              "session", "holiday"))
                if _perm:
                    print(f" {RED}✗ PERMANENT — {reason[:50]}{RESET}")
                elif _temp:
                    print(f" {YELLOW}⏱ TEMP (market closed) — {reason[:40]}{RESET}")
                else:
                    print(f" {DIM}{reason[:60]}{RESET}")
        except Exception as exc:
            print(f" {RED}ERROR: {exc}{RESET}")
            if verbose:
                import traceback; traceback.print_exc()
            results.append({
                "symbol": sym, "market": "US", "mode": mode_str,
                "signal_score": score, "options_available": False,
                "no_options_reason": str(exc),
                "warnings": [], "top_contracts": [], "fo_eligible": True,
            })

    return results


def _fetch_vix() -> float | None:
    """Fetch current VIX from yfinance. Returns None on failure (non-blocking)."""
    try:
        import yfinance as _yf
        vix_data = _yf.Ticker("^VIX").fast_info
        v = float(vix_data.last_price)
        return v if v > 0 else None
    except Exception:
        return None


def _save_report(results: list[dict], mode: str, output_path: Path,
                 vix: float | None = None) -> None:
    """Save HTML report to file, persist recommendations to alert_tracker.db."""
    from options.options_report import build_options_full_report
    title = f"F&O Options Analysis — {mode} Mode"
    html  = build_options_full_report(results, title=title)
    # Import report_db utilities (independent of archiving — don't entangle)
    _abw = None; _arc = None; save_options_report_snapshot = None  # type: ignore
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent.parent / "reports"))
        from report_db import archive_before_write as _abw, archive_report as _arc, save_options_report_snapshot  # noqa: E501
    except Exception:
        pass

    # Archive previous version before overwriting (failure must not block write)
    if _abw:
        try:
            _abw(output_path)
            _json_path = output_path.with_suffix(".json")
            if _json_path.exists():
                _abw(_json_path)
        except Exception:
            pass

    _ro_ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _ro_agent = {
        "generated_at": _ro_ts,
        "mode":         mode,
        "vix":          round(float(vix or 0), 2),
        "n_symbols":    len(results),
        "n_contracts":  sum(len(r.get("top_contracts") or []) for r in results),
        "top_positions": [
            {
                "symbol":       r.get("symbol"),
                "signal_score": round(float(r.get("signal_score") or 0), 2),
                "mode":         r.get("mode", mode),
                "market":       r.get("market", "US"),
                "n_contracts":  len(r.get("top_contracts") or []),
                "top_contract": (
                    {k: r["top_contracts"][0].get(k)
                     for k in ("strike", "expiry", "dte", "option_score", "mid_price", "delta", "iv")}
                    if r.get("top_contracts") else None
                ),
            }
            for r in results[:30]
        ],
    }
    _ro_comment = f"<!-- AGENT-DATA: {json.dumps(_ro_agent, separators=(',', ':'))} -->\n"
    output_path.write_text(_ro_comment + html, encoding="utf-8")
    output_path.with_suffix(".json").write_text(json.dumps(_ro_agent, indent=2), encoding="utf-8")
    print(f"\n{GREEN}Report saved: {output_path}{RESET}")

    # ── Persist to report_analytics.db (summary snapshot) ─────────────────
    try:
        if save_options_report_snapshot:
            save_options_report_snapshot(results, mode)
        if _arc:
            _arc(output_path)
            _json_arc_path = output_path.with_suffix(".json")
            if _json_arc_path.exists():
                _arc(_json_arc_path)
    except Exception as _e:
        print(f"  [report_db] options_report snapshot skipped: {_e}")

    # ── Persist individual recommendations to alert_tracker.db ────────────
    try:
        _sys.path.insert(0, str(TV_DIR))
        from alert_tracker import AlertTracker
        _db_path = ROOT_DIR / "database" / "alert_tracker.db"
        tracker  = AlertTracker(str(_db_path))
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        run_ts   = datetime.now(timezone.utc).isoformat()
        saved_n  = 0
        for r in results:
            if not r.get("top_contracts"):
                continue
            sym  = r.get("symbol", "")
            meta = r.get("_signal_meta") or {}
            for rank, c in enumerate(r["top_contracts"], 1):
                # Build moneyness_pct — available on contract dict
                m_pct = c.get("moneyness_pct")
                # Determine CALL/PUT from option_type field
                opt_type = c.get("option_type", "CALL").upper()
                if opt_type not in ("CALL", "PUT"):
                    opt_type = "CALL"
                rec = {
                    "run_date":             run_date,
                    "run_timestamp":        run_ts,
                    "tv_symbol":            sym,
                    "signal_score":         r.get("signal_score"),
                    "l1_score":             meta.get("l1_score"),
                    "l2_score":             meta.get("l2_score"),
                    "l3_score":             meta.get("l3_score"),
                    "l4_score":             meta.get("l4_score"),
                    "news_sentiment_score": meta.get("news_sentiment_score"),
                    "news_count":           meta.get("news_count"),
                    "earnings_days_away":   meta.get("earnings_days_away"),
                    "earnings_risk":        meta.get("earnings_risk"),
                    "pcr":                  meta.get("pcr"),
                    "vix_at_rec":           vix,
                    "mode":                 r.get("mode", mode),
                    "rank":                 rank,
                    "option_type":          opt_type,
                    "strike":               c.get("strike"),
                    "expiry":               c.get("expiry"),
                    "dte_at_rec":           c.get("dte"),
                    "spot_price":           r.get("spot_price"),
                    "moneyness_pct":        m_pct,
                    "premium":              c.get("mid_price"),
                    "delta":                c.get("delta"),
                    "theta":                c.get("theta"),
                    "iv_pct":               c.get("iv"),
                    "volume":               c.get("volume"),
                    "open_interest":        c.get("oi"),
                    "spread_pct":           c.get("spread_pct"),
                    "option_score":         c.get("option_score"),
                    "liq_score":            c.get("liq_score"),
                    "iv_score":             c.get("iv_score"),
                    "strike_score":         c.get("strike_score"),
                    "dte_score":            c.get("dte_score"),
                    "moneyball_score":      c.get("moneyball_score"),
                    "final_score":          c.get("final_score"),
                    "market":               r.get("market", "US"),
                    "lot_size":             c.get("lot_size", 1),
                    "data_source":          r.get("data_source"),
                    "outcome":              "OPEN",
                }
                tracker.save_options_recommendation(rec)
                saved_n += 1
        print(f"  [options] persisted {saved_n} recommendations to alert_tracker.db")
    except Exception as _e:
        print(f"  [alert_tracker] options_recommendations save skipped: {_e}")


def _print_text_summary(results: list[dict]) -> None:
    """Print a compact text summary table."""
    print(f"\n{BOLD}{'Symbol':<25} {'Market':<6} {'Available':<10} "
          f"{'Top Contract':<35} {'Score'}{RESET}")
    print("─" * 90)

    for r in results:
        sym    = r.get("symbol", "?")[:24]
        market = r.get("market", "US")
        avail  = "✅ YES" if r.get("options_available") else "❌ NO"
        currency = "₹" if market == "NSE" else "$"

        if r.get("top_contracts"):
            c = r["top_contracts"][0]
            contract_str = (f"CALL {currency}{c.get('strike',0):,.0f} "
                            f"exp {c.get('expiry','?')} "
                            f"@ {currency}{c.get('mid_price',0):.2f} "
                            f"(Δ {c.get('delta',0):.2f})")
            score_str = f"{c.get('option_score', 0):.0f}/100"
        else:
            _reason = r.get("no_options_reason", "")
            _perm   = "NOT in" in _reason and ("eligible" in _reason or "F&O" in _reason)
            _temp   = any(kw in _reason.lower() for kw in
                          ("market closed", "ab9019", "after hour", "outside trading",
                           "session", "holiday"))
            if _perm:
                contract_str = f"[PERM] {_reason[:28]}"
            elif _temp:
                contract_str = f"[TEMP closed] {_reason[:22]}"
            else:
                contract_str = _reason[:35]
            score_str    = "—"

        warns = len(r.get("warnings", []))
        warn_str = f" {YELLOW}⚠{warns}{RESET}" if warns else ""

        print(f"  {CYAN}{sym:<23}{RESET} {market:<6} {avail:<10} "
              f"{contract_str:<35} {score_str}{warn_str}")

    print()


# ─── CLI entry point ─────────────────────────────────────────────────────────

def _load_bearish_candidates(limit: int = 20) -> list[dict]:
    """Load bearish/overweight candidates from the multi-asset screener output.

    Falls back to the last actionable_log.json entries tagged as SELL/REDUCE
    if the screener bearish list is unavailable.
    """
    # Primary: look for screener bearish output
    for fname in ["multi_asset_bearish.json", "bearish_signals.json"]:
        p = TV_DIR / fname
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[:limit]
            except Exception:
                pass
    # Fallback: actionable_log with bearish indicators (short signals)
    alog = TV_DIR / "actionable_log.json"
    if alog.exists():
        try:
            data = json.loads(alog.read_text(encoding="utf-8"))
            candidates = [
                s for s in (data if isinstance(data, list) else [])
                if s.get("signal_type", "").upper() in ("SHORT", "SELL", "BEARISH")
                   or float(s.get("composite") or s.get("score") or 0) < 40
            ]
            return candidates[:limit]
        except Exception:
            pass
    return []


def main() -> int:
    # ── Moomoo daemon threads: prevent SDK threads from blocking process exit ──
    try:
        from moomoo import SysConfig
        SysConfig.set_all_thread_daemon(True)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="F&O Options Analyzer — analyze buy signals for options opportunities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from-actionable", action="store_true",
                        help="Load symbols from the actionable report or manual fallbacks")
    parser.add_argument("--actionable-file", type=str, default="",
                        help="Read --from-actionable symbols from this JSON instead of "
                             "reports/actionable_report.json (e.g. a holdings run)")
    parser.add_argument("--expected-run-id", type=str, default="",
                        help="Expected run ID for strict automatic actionable loading")
    parser.add_argument("--strict-actionable", action="store_true",
                        help="Require a complete matching report and disable fallbacks")
    parser.add_argument("--symbol",  type=str, default="",
                        help="Single symbol to analyze, e.g. NASDAQ:AAPL")
    parser.add_argument("--symbols", type=str, default="",
                        help="Comma-separated list of symbols")
    parser.add_argument("--score",   type=float, default=65.0,
                        help="Signal score (0-100, default 65)")
    parser.add_argument("--mode",    type=str, default="BALANCED",
                        choices=["SAFE", "BALANCED", "AGGRESSIVE"],
                        help="Strategy mode (default BALANCED)")
    parser.add_argument("--all-modes", action="store_true",
                        help="Run all three modes for the given symbol(s)")
    parser.add_argument("--india-only", action="store_true",
                        help="Only analyze NSE/BSE symbols")
    parser.add_argument("--us-only", action="store_true",
                        help="Only analyze US symbols")
    parser.add_argument("--limit",  type=int, default=20,
                        help="Max symbols from actionable_log (default 20)")
    parser.add_argument("--output",  type=str, default="",
                        help="Output HTML path (default: reports/options_report.html)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--puts", action="store_true",
                        help="Analyse PUT options for bearish/overweight positions instead of CALLs")
    parser.add_argument("--from-bearish-screen", action="store_true",
                        help="Load bearish candidates from the multi-asset screener bearish list")
    args = parser.parse_args()

    # ── Market-hours guard ─────────────────────────────────────────────────
    _now_utc = datetime.now(timezone.utc)
    _h_utc   = _now_utc.hour + _now_utc.minute / 60
    if args.india_only:
        _open_h, _close_h, _mkt_name = 3.75, 10.0, "NSE/BSE"
    else:
        _open_h, _close_h, _mkt_name = 13.5, 21.0, "US"
    if _now_utc.weekday() >= 5 or not (_open_h <= _h_utc <= _close_h):
        _why = "weekend" if _now_utc.weekday() >= 5 else "outside regular trading hours"
        print(f"\n\u26a0\ufe0f  {_mkt_name} options market is currently {_why} — contract data may be unavailable.")
        print(f"   UTC now: {_now_utc.strftime('%H:%M')}  |  "
              f"Market window (UTC): {int(_open_h):02d}:{int((_open_h%1)*60):02d}\u2013{int(_close_h):02d}:{int((_close_h%1)*60):02d}")
        print(f"   Run during market hours for live contract pricing and IV data.\n")

    # ── Collect symbols ──────────────────────────────────────────────────────
    symbols_with_scores: list[tuple[str, float]] = []

    if args.from_actionable:
        if args.strict_actionable:
            if not args.actionable_file or not args.expected_run_id:
                print(f"{YELLOW}Strict actionable mode requires --actionable-file and --expected-run-id.{RESET}")
                return 2
            report, reason = _read_strict_actionable_report(
                Path(args.actionable_file), args.expected_run_id
            )
            if report is None:
                print(f"{YELLOW}Strict actionable report unavailable: {reason}.{RESET}")
                return 2
        try:
            signals = _load_actionable_signals(
                limit=args.limit,
                actionable_file=Path(args.actionable_file) if args.actionable_file else None,
                strict=args.strict_actionable,
                expected_run_id=args.expected_run_id,
            )
        except StrictActionableReportError as exc:
            print(f"{YELLOW}Strict actionable report unavailable: {exc}.{RESET}")
            return 2
        for s in signals:
            sym   = s.get("tv_symbol") or s.get("symbol") or s.get("s") or ""
            score = float(s.get("composite") or s.get("score") or args.score)
            if not sym:
                continue
            exch = sym.split(":")[0].upper() if ":" in sym else "NYSE"
            if args.india_only and exch not in ("NSE", "BSE"):
                continue
            if args.us_only and exch in ("NSE", "BSE"):
                continue
            symbols_with_scores.append((sym, score))
        if not symbols_with_scores:
            print(f"{YELLOW}No eligible actionable option symbols found; options analysis skipped.{RESET}")
            return

    elif getattr(args, "from_bearish_screen", False):
        candidates = _load_bearish_candidates(limit=args.limit)
        for s in candidates:
            sym   = s.get("tv_symbol") or s.get("symbol") or s.get("s") or ""
            score = float(s.get("composite") or s.get("score") or args.score)
            if not sym:
                continue
            exch = sym.split(":")[0].upper() if ":" in sym else "NYSE"
            if args.india_only and exch not in ("NSE", "BSE"):
                continue
            if args.us_only and exch in ("NSE", "BSE"):
                continue
            symbols_with_scores.append((sym, score))
        if not symbols_with_scores:
            print(f"{YELLOW}No bearish candidates found. Run multi_asset_screener.py first.{RESET}")
            return

    elif args.symbol:
        symbols_with_scores.append((args.symbol.strip(), args.score))

    elif args.symbols:
        for sym in args.symbols.split(","):
            sym = sym.strip()
            if sym:
                symbols_with_scores.append((sym, args.score))

    else:
        parser.print_help()
        print(f"\n{YELLOW}Provide --from-actionable, --symbol, or --symbols{RESET}")
        return

    if not symbols_with_scores:
        print(f"{YELLOW}No symbols to analyze.{RESET}")
        return

    if args.from_actionable:
        _closed = [
            symbol for symbol, _ in symbols_with_scores
            if not _market_is_open(
                symbol.split(":", 1)[0] if ":" in symbol else "NYSE", _now_utc
            )
        ]
        if _closed:
            print(f"{DIM}  {len(_closed)} symbol(s) on a closed exchange — "
                  f"using last-session contract data.{RESET}")

    # ── Build signal metadata map (tv_symbol → full signal dict) ─────────────
    # Used to persist L1-L4, news, earnings, PCR alongside each recommendation.
    _signal_meta: dict[str, dict] = {}
    if args.from_actionable:
        try:
            _all_sigs = _load_actionable_signals(
                limit=200,   # wider to catch all symbols
                actionable_file=Path(args.actionable_file) if args.actionable_file else None,
            )
            for _s in _all_sigs:
                _sym = (_s.get("tv_symbol") or _s.get("symbol") or "").upper()
                if _sym:
                    _signal_meta[_sym] = _s
        except Exception:
            pass

    # ── Fetch VIX once for the whole run ─────────────────────────────────────
    _vix = _fetch_vix()
    if _vix:
        print(f"  {DIM}VIX = {_vix:.2f}{RESET}")

    # ── Run analysis ──────────────────────────────────────────────────────────
    modes = ["SAFE", "BALANCED", "AGGRESSIVE"] if args.all_modes else [args.mode]
    all_results: list[dict] = []

    for mode in modes:
        _print_header(f"Options Analysis — {mode} Mode ({len(symbols_with_scores)} symbols)")
        results = _run_analysis(
            symbols_with_scores,
            mode,
            access_token = None,
            verbose      = args.verbose,
            puts_mode    = getattr(args, "puts", False),
            signal_meta  = _signal_meta,
        )
        all_results.extend(results)

    # ── Sort: available contracts first, then by best option_score descending ──
    def _sort_key(r: dict) -> tuple:
        avail = 1 if r.get("options_available") else 0
        best_score = max((c.get("option_score", 0) for c in r.get("top_contracts", [])), default=0)
        return (avail, best_score)

    all_results.sort(key=_sort_key, reverse=True)

    # ── Output report ─────────────────────────────────────────────────────────
    _print_text_summary(all_results)

    out_path = Path(args.output) if args.output else REPORTS_DIR / "options_report.html"
    _save_report(all_results, " + ".join(modes), out_path, vix=_vix)

    # Force-exit to kill any Moomoo SDK background threads that prevent clean exit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
