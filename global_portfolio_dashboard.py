#!/usr/bin/env python3
"""
Global Portfolio Dashboard
===========================
Aggregates live positions from ALL 6 brokers:
  • Moomoo AU  (OpenD port 11111)
  • Moomoo SG  (OpenD port 11112)
  • IBKR       (Flex Query XML / cached)
  • Webull AU  (PDF statements)
  • Zerodha    (ZERODHA_ACCESS_TOKEN env var)
  • Angel One  (TOTP auto-auth)

Displays a consolidated CLI snapshot FIRST, then generates
global_portfolio_dashboard.html with full broker-by-broker breakdown.

HARD STOP: If Zerodha is unavailable, report is NOT generated (India equity
~₹2-3L would be silently excluded, making the total misleading).
Use --allow-partial to override (not recommended).

Usage:
    python global_portfolio_dashboard.py
    python global_portfolio_dashboard.py --no-open      # skip browser auto-open
    python global_portfolio_dashboard.py --no-ibkr      # skip IBKR (slow ~2 min)
    python global_portfolio_dashboard.py --allow-partial # override Zerodha hard stop
"""
from __future__ import annotations

import os, sys, math, json, re, socket, argparse, webbrowser, sqlite3, shutil, time, threading
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT        = Path(__file__).parent.resolve()
TV          = ROOT / "tradingview"
CGT         = ROOT / "cgt"
INDIA       = ROOT / "india-brokers"
REPORTS     = ROOT / "reports"
OUTPUT      = REPORTS / "global_portfolio_dashboard.html"
DB_PATH     = ROOT / "database" / "portfolio_history.db"
ARCHIVE_DIR = REPORTS / "archive"

# ─── Jinja2 template environment ──────────────────────────────────────────────
_TEMPLATE_DIR = ROOT / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)

try:
    from tradingview.analysis.portfolio_risk_scorer import (
        PortfolioRiskScorer as _RiskScorer,
        render_risk_html as _render_risk_html,
    )
    _RISK_SCORER_AVAILABLE = True
except ImportError:
    _RISK_SCORER_AVAILABLE = False

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("ANGELONE_API_KEY",     "")
os.environ.setdefault("ANGELONE_CLIENT_CODE", "")
os.environ.setdefault("ANGELONE_PIN",         "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

# Add paths (centralised via path_config)
from path_config import ensure_paths
ensure_paths(ROOT, TV, TV / "sell", INDIA, CGT / "ibkr-cgt")

# ── Structured logging ────────────────────────────────────────────────────────
from log_config import get_logger
_log = get_logger("global_portfolio_dashboard")

from broker_schemas import validate_positions

from html_utils import BOLD, DIM, CYAN, GREEN, YELLOW, BLUE, MAGENTA, RED, RESET  # B-5
from html_utils import system_footer_html  # C-12

# ── Extracted HTML report helpers ──────────────────────────────────────────────
from portfolio_report_html import (
    _compute_fd_value_at_month, _compute_capital_deployed_at_month,
    _generate_realised_pnl_html, _html_coverage_row,
)

# ── Live FX rates (single source of truth — see fx_rates.py) ───────────────────
from fx_rates import get_fx_to_usd, to_aud


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return True
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Positions cache — reject stale auth-error states so auth retry actually fires
# ═══════════════════════════════════════════════════════════════════════════════

_POSITIONS_CACHE_FILE = ROOT / ".cache_global_positions.json"
_POSITIONS_CACHE_TTL  = 3600  # seconds — 1 hour
_REALISED_PNL_CACHE_FILE = ROOT / ".cache_realised_pnl.json"
_REALISED_PNL_CACHE_TTL  = 43200  # 12 hours — PDF statements don't change intraday

# Data-source paths for all-or-none pre-flight broker availability checks
_IBKR_FLEX_CACHE  = ROOT / "cgt" / "ibkr-cgt" / "_last_flex.xml"
_IBKR_STMTS_DIR   = ROOT / "ibkr-statement"
_WEBULL_STMTS_DIR  = ROOT / "webull-statement"
_ZERODHA_TOKEN    = ROOT / "india-brokers" / ".zerodha_token_cache"

_AUTH_ERROR_SENTINELS = (
    "insufficient permission",
    "tokenexception",
    "permissionexception",
    "token expired",
    "incorrect access_token",
    "api_key",
    "login skipped",
    "holdings missing",
    "login failed",
    "no valid",
    "auth failed",
    "skipped (--no-ibkr)",   # guardrail: cache from an old --no-ibkr run must never be reused
)


def _status_has_auth_error(statuses: dict) -> bool:
    """Return True if any broker status string looks like an auth failure."""
    for v in (statuses or {}).values():
        low = str(v).lower()
        if any(s in low for s in _AUTH_ERROR_SENTINELS):
            return True
    return False


def _load_positions_cache() -> tuple[list | None, dict | None]:
    """
    Load cached positions + statuses from disk.
    Returns (None, None) if:
      • File does not exist or is malformed
      • Cache is older than 1 hour
      • Any status entry contains auth-error keywords (forces live re-fetch so auth retry fires)
    """
    try:
        if not _POSITIONS_CACHE_FILE.exists():
            return None, None
        raw = json.loads(_POSITIONS_CACHE_FILE.read_text(encoding="utf-8"))
        import time as _time
        if _time.time() - raw.get("saved_at", 0) > _POSITIONS_CACHE_TTL:
            return None, None
        statuses = raw.get("statuses", {})
        if _status_has_auth_error(statuses):
            print(f"{YELLOW}⚠ Cached positions had auth errors — forcing live re-fetch.{RESET}")
            return None, None
        return raw.get("positions"), statuses
    except Exception:
        return None, None


def _save_positions_cache(positions: list, statuses: dict) -> None:
    """Persist positions + statuses to disk. Skips save if auth errors are present."""
    if _status_has_auth_error(statuses):
        return  # never poison the cache with auth-error states
    try:
        import time as _time
        _POSITIONS_CACHE_FILE.write_text(
            json.dumps({"saved_at": _time.time(), "positions": positions, "statuses": statuses},
                       default=str),
            encoding="utf-8"
        )
    except Exception as exc:
        _log.warning("positions_cache_save_failed", error=str(exc))

def _load_realised_pnl_cache() -> dict | None:
    """Return cached realised P&L dict if it exists and is within TTL."""
    try:
        if not _REALISED_PNL_CACHE_FILE.exists():
            return None
        if time.time() - _REALISED_PNL_CACHE_FILE.stat().st_mtime > _REALISED_PNL_CACHE_TTL:
            return None
        return json.loads(_REALISED_PNL_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_realised_pnl_cache(data: dict) -> None:
    """Persist realised P&L data to JSON cache (12-hour TTL)."""
    try:
        _REALISED_PNL_CACHE_FILE.write_text(
            json.dumps(data, default=str, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

# Moomoo SDK is not fully thread-safe: two simultaneous OpenSecTradeContext
# connections (AU + SG) can interfere via shared protobuf/network state.
# This lock serialises Moomoo connections while other brokers still run in parallel.
_MOOMOO_LOCK = threading.Lock()


def _fetch_moomoo(label: str, port: int) -> tuple[list[dict], str]:
    """
    Returns (positions, status_msg).
    Each pos: {symbol, shares, avg_cost, current_price, market_val, pnl, broker}

    Spawns a daemon thread so OpenSecTradeContext() cannot block the main process
    indefinitely — the SDK has an internal `while True: _init_connect_sync()` retry
    loop with no external preemption when the protocol handshake fails (stale OpenD
    session, OpenD starting up, etc.).
    _MOOMOO_LOCK is acquired INSIDE the thread so the lock is always held and
    released by the same thread that does the SDK work, even if the outer caller
    times out and moves on.
    """
    _result: list = []

    def _worker() -> None:
        try:
            with _MOOMOO_LOCK:  # serialise AU/SG — SDK not fully thread-safe; lock inside thread
                data, msg = _fetch_moomoo_locked(label, port)
                _result.extend([data, msg])
        except Exception as _ex:
            _result.extend([[], f"Moomoo-{label}: worker error: {_ex}"])

    t = threading.Thread(target=_worker, daemon=True, name=f"moomoo-{label}")
    t.start()
    t.join(timeout=20)  # 20s = one full SDK _init_connect_sync() cycle
    if t.is_alive():
        return [], f"Moomoo-{label}: OpenD connect timeout (20s)"
    return (_result[0], _result[1]) if len(_result) >= 2 else ([], f"Moomoo-{label}: no result")


def _fetch_moomoo_locked(label: str, port: int) -> tuple[list[dict], str]:
    if not _is_port_open(port):
        return [], f"OpenD not running on port {port}"
    try:
        from moomoo import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
        from moomoo_config import ACCOUNTS  # [(label, port, firm, acc_id), ...]

        # Find the account for this label
        acct = next((a for a in ACCOUNTS if a[0] == label), None)
        if not acct:
            return [], f"No account config for {label}"
        _, _port, firm, acc_id = acct

        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE,
            host="127.0.0.1", port=port, security_firm=firm
        )
        try:
            ret, df = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
        finally:
            ctx.close()

        if ret != 0 or df is None or df.empty:
            return [], f"Empty response (ret={ret})"

        positions = []
        for _, row in df.iterrows():
            code      = str(row.get("code", ""))
            qty       = float(row.get("qty", 0))
            avg_cost  = float(row.get("average_cost", 0))
            market_val= float(row.get("market_val", 0))
            if qty <= 0 or avg_cost <= 0 or not math.isfinite(avg_cost):
                continue
            nom_price = float(row.get("nominal_price") or 0) or (market_val / qty if qty else 0)

            # Options: market_val already includes the 100× contract multiplier
            # but avg_cost is per-share (the premium).  Use Moomoo's own pl_val
            # which is already correct for both stocks and options.
            is_option = bool(re.search(r'\d{6}[CP]\d+', code))
            pl_val_valid = bool(row.get("pl_val_valid", True))
            if is_option and pl_val_valid:
                # Moomoo pl_val / pl_ratio_avg_cost are authoritative for options
                pnl        = float(row.get("pl_val", 0))
                pnl_pct    = float(row.get("pl_ratio_avg_cost", 0))
                cost_basis = market_val - pnl
                # avg_cost for display: multiply by contract size so it shows $150 not $1.50
                avg_cost_display = cost_basis / qty if qty else avg_cost
            else:
                cost_basis        = avg_cost * qty
                pnl               = market_val - cost_basis
                pnl_pct           = pnl / cost_basis * 100 if cost_basis else 0
                avg_cost_display  = avg_cost

            # Convert code prefix to exchange
            prefix = code.split(".")[0].upper() if "." in code else ""
            EXCH_MAP = {"AU":"ASX","HK":"HKEX","SG":"SGX","JP":"TYO",
                        "KR":"KRX","CA":"TSX","UK":"LON","US":""}
            exchange = EXCH_MAP.get(prefix, "")
            ticker   = code.split(".", 1)[1] if "." in code else code
            tv_sym   = f"{exchange}:{ticker}" if exchange else ticker
            positions.append({
                "symbol":        tv_sym,
                "shares":        int(qty),
                "avg_cost":      round(avg_cost_display, 4),
                "current_price": round(nom_price, 4),
                "market_val":    round(market_val, 2),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 1),
                "currency":      _currency_for_prefix(prefix),
                "broker":        f"Moomoo-{label}",
                "exchange":      exchange or "US",
                **({"is_option": True} if is_option else {}),
            })
        return positions, f"Live — {len(positions)} positions"
    except Exception as e:
        return [], f"Error: {e}"


def _currency_for_prefix(prefix: str) -> str:
    return {"AU":"AUD","HK":"HKD","SG":"SGD","JP":"JPY","KR":"KRW",
            "CA":"CAD","UK":"GBP","US":"USD"}.get(prefix, "USD")


def _fetch_webull_pdf(pdf_dir: str | None = None) -> tuple[list[dict], str]:
    """
    Read Webull AU PDF statements and return positions with live prices.
    Returns (positions, status_msg).
    Each pos: {symbol, shares, avg_cost, current_price, market_val, pnl, broker, currency, exchange}

    Results are cached in webull-statement/_webull_parse_cache.json keyed by
    PDF fingerprint (filename → mtime).  Re-parsing only happens when PDFs change.
    """
    if pdf_dir is None:
        pdf_dir = str(ROOT / "webull-statement")
    if not os.path.isdir(pdf_dir):
        return [], f"PDF folder not found: {pdf_dir}"

    _cache_path = Path(pdf_dir) / "_webull_parse_cache.json"

    # Build fingerprint: {filename: mtime} for all PDFs in folder
    pdf_files = sorted(Path(pdf_dir).glob("*.pdf"))
    fingerprint = {f.name: f.stat().st_mtime for f in pdf_files}

    # Try loading from cache if fingerprint matches
    _cached_data: dict | None = None
    if _cache_path.exists():
        try:
            _disk = json.loads(_cache_path.read_text(encoding="utf-8"))
            if _disk.get("fingerprint") == fingerprint:
                _cached_data = _disk.get("data")
        except Exception as exc:
            _log.debug("webull_cache_fingerprint_read_failed", error=str(exc))

    try:
        _webull_cgt_dir = str(CGT / "webull-cgt")
        ensure_paths(_webull_cgt_dir)
        from webull_statement_reader import parse_all_pdfs  # type: ignore
        from webull_cgt_analysis import fetch_live_prices   # type: ignore

        if _cached_data is not None:
            data = _cached_data
            _cache_source = "cache"
        else:
            data = parse_all_pdfs(pdf_dir)
            _cache_source = "parsed"
            # Save to cache
            try:
                _cache_path.write_text(
                    json.dumps({"fingerprint": fingerprint, "data": data}, indent=2,
                               default=str),
                    encoding="utf-8"
                )
            except Exception as exc:
                _log.debug("webull_cache_save_failed", error=str(exc))

        holdings = data.get("holdings", [])
        if not holdings:
            return [], f"No holdings in latest statement ({_cache_source})"

        # Fetch live prices
        live_prices = fetch_live_prices(holdings)

        positions = []
        for h in holdings:
            symbol    = h.get("symbol", "")
            qty       = float(h.get("qty", 0))
            avg_price = float(h.get("avg_price", 0) or 0)
            stmt_close = float(h.get("close_price", 0) or 0)
            currency  = h.get("currency", "USD")
            exchange  = h.get("exchange", "")

            if qty <= 0 or avg_price <= 0:
                continue

            # Use live price if available
            lp = live_prices.get(symbol, {})
            current_price = lp.get("price", 0.0) or stmt_close

            mval = current_price * qty if current_price else avg_price * qty
            cost = avg_price * qty
            pnl  = mval - cost
            pnl_pct = (pnl / cost * 100) if cost else 0.0

            # Determine exchange label
            if currency == "AUD" or exchange in ("ASX", "XASX"):
                ex_label = "AU"
            elif currency in ("HKD", "CNH") or exchange in ("HKEX", "SEHK"):
                ex_label = "HK"
            else:
                ex_label = "US"

            positions.append({
                "symbol":        symbol,
                "shares":        int(qty) if qty == int(qty) else qty,
                "avg_cost":      round(avg_price, 4),
                "current_price": round(current_price, 4),
                "market_val":    round(mval, 2),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 1),
                "currency":      currency,
                "broker":        "Webull AU",
                "exchange":      ex_label,
            })

        as_of = data.get("latest_date", "")
        has_live = any(live_prices.get(h.get("symbol", ""), {}).get("price", 0) > 0 for h in holdings)
        src = "live prices" if has_live else "statement prices"
        return positions, f"PDF statements — {len(positions)} positions ({src}, as of {as_of}, {_cache_source})"
    except Exception as e:
        return [], f"Error: {e}"


def _webull_api_configured() -> bool:
    """True if the official Webull AU OpenAPI credentials are configured.
    See cgt/webull-cgt/webull_au_api.py for setup instructions."""
    try:
        _webull_cgt_dir = str(CGT / "webull-cgt")
        ensure_paths(_webull_cgt_dir)
        from webull_au_api import is_configured  # type: ignore
        return is_configured()
    except Exception:
        return False


def _fetch_webull_api() -> tuple[list[dict], str]:
    """Fetch Webull AU positions via the official OpenAPI
    (https://developer.webull.com.au/apis/docs/) — replaces PDF parsing for
    CURRENT holdings. Historical CGT trade parsing still uses PDF statements
    (the API only exposes current positions/balance, not multi-year trade
    history needed for tax lodgement)"""
    try:
        _webull_cgt_dir = str(CGT / "webull-cgt")
        ensure_paths(_webull_cgt_dir)
        from webull_au_api import get_holdings as _wb_api_get_holdings  # type: ignore
        return _wb_api_get_holdings()
    except Exception as e:
        return [], f"API error: {e}"


def _fetch_webull(pdf_dir: str | None = None) -> tuple[list[dict], str]:
    """
    Fetch Webull AU positions — API-first, PDF is a FALLBACK ONLY (never
    merged in when the API succeeds):
      • Official OpenAPI (developer.webull.com.au) — verified live to return
        holdings across ALL markets (AU/HK/US), not just US as the docs'
        "Supported Markets and Products" table (about-open-api) suggests —
        that table appears to describe ORDER PLACEMENT support, not the
        read-only Account Positions query. When configured and it returns
        positions, it is the SOLE source of truth for current holdings.
      • PDF statement parsing (_fetch_webull_pdf) — used ONLY when the API
        is not configured, errors, or returns zero positions. Old PDF
        statements can contain positions that have since been closed, so
        merging them in alongside a working API would resurrect stale
        holdings — the API result must NOT be padded with PDF extras.
    """
    if _webull_api_configured():
        api_positions, api_status = _fetch_webull_api()
        if api_positions:
            return api_positions, api_status

    pdf_positions, pdf_status = _fetch_webull_pdf(pdf_dir)
    return pdf_positions, pdf_status


def _fetch_ibkr_live_tws() -> tuple[list[dict], str] | None:
    """Try ibkr_async TWSAdapter for real-time positions (~150ms).
    Returns None if TWS is unavailable so caller can fall back to Flex."""
    # Fast pre-check: skip the async dance if TWS port is obviously closed.
    _ibkr_port = int(os.environ.get("IBKR_PORT", "7496"))
    if not _is_port_open(_ibkr_port):
        _log.info("ibkr_tws_port_closed", port=_ibkr_port)
        return None
    try:
        import asyncio
        from ibkr_async import TWSAdapter, IBKRConfig
        from ibkr_async.exceptions import IBKRError

        async def _get():
            config = IBKRConfig.from_env()
            async with TWSAdapter(config) as adapter:
                return await adapter.fetch_positions()

        # 20 s wall-clock cap: prevents hanging if port is open but TWS is
        # not responding (asyncio.wait_for cancels the coroutine cleanly).
        positions_raw = asyncio.run(asyncio.wait_for(_get(), timeout=20.0))
        positions = [p.to_dict() for p in positions_raw]
        # Normalize field names to match dashboard expectation
        result = []
        for p in positions:
            result.append({
                "symbol":        p.get("symbol", ""),
                "shares":        int(p.get("qty", 0)),
                "avg_cost":      round(p.get("avg_cost", 0), 4),
                "current_price": round(p.get("last_price", 0), 4),
                "market_val":    round(p.get("market_value", 0), 2),
                "pnl":           round(p.get("unrealised_pnl", 0), 2),
                "pnl_pct":       round(p.get("pnl_pct", 0), 1),
                "currency":      p.get("currency", "USD"),
                "broker":        "IBKR",
                "exchange":      p.get("exchange", "US"),
            })
        return result, f"TWS Live — {len(result)} positions"
    except Exception as _tws_exc:
        _log.warning("ibkr_tws_fetch_failed",
                     error=str(_tws_exc),
                     exc_type=type(_tws_exc).__name__)
        return None  # Signal to fall back to Flex


def _fetch_ibkr_flex() -> tuple[list[dict], str]:
    """Fetch IBKR positions via Flex Query (existing method, 30-300s)."""
    _using_cache = False
    try:
        _ibkr_dir = CGT / "ibkr-cgt"
        ensure_paths(_ibkr_dir)
        import ibkr_cgt_analysis as ibkr  # type: ignore

        cache_path = _ibkr_dir / "_last_flex.xml"
        # Accept any cache younger than 48 h — IBKR Flex is T+1 anyway and
        # the live fetch hangs 30-300 s when IBKR servers are slow.
        _FLEX_CACHE_MAX_AGE_H = 48
        _cache_age_h: float = (
            (time.time() - cache_path.stat().st_mtime) / 3600
            if cache_path.exists() else float("inf")
        )
        _cache_fresh = _cache_age_h < _FLEX_CACHE_MAX_AGE_H

        if cache_path.exists() and _cache_fresh:
            _using_cache = True
            xml_str = cache_path.read_text(encoding="utf-8")
            _, stmt_pos, _, _ = ibkr.parse_flex_xml(xml_str, ibkr.FETCH_START)
            cache_age_label = f"{_cache_age_h:.0f}h"
            source_note = f"Cached ({cache_age_label} old)"
        else:
            # Live fetch — cap at 90 s; fall back to stale cache if it times out.
            if cache_path.exists():
                print(f"  ⚠️ Flex cache {_cache_age_h:.0f}h old (>{_FLEX_CACHE_MAX_AGE_H}h limit), refetching…")
            # Golden Rule #16: use daemon thread — NOT ThreadPoolExecutor — so
            # the interpreter exit never hangs waiting for a blocked Flex Query.
            _flex_exc: Exception | None = None
            _flex_result: list = []
            _flex_err: list = []
            def _do_flex() -> None:
                try:
                    _flex_result.append(ibkr.fetch_flex_statement(
                        ibkr.FLEX_TOKEN, ibkr.FLEX_QUERY_ID, ibkr.FETCH_START))
                except Exception as _fe:
                    _flex_err.append(_fe)
            _flex_t = threading.Thread(target=_do_flex, daemon=True, name="ibkr_flex")
            _flex_t.start()
            _flex_t.join(timeout=360)
            if _flex_t.is_alive():
                _flex_exc = TimeoutError("Flex Query timed out (360 s)")
                _log.warning("ibkr_flex_timeout", timeout_s=360)
            elif _flex_err:
                _flex_exc = _flex_err[0]
                _log.warning("ibkr_flex_live_failed", error=str(_flex_err[0]))
            else:
                _, stmt_pos, _, _ = _flex_result[0]
                source_note = "Live"
            if _flex_exc is not None:
                if cache_path.exists():
                    # Use stale cache rather than returning nothing.
                    # IMPORTANT: status must NOT contain offline markers
                    # ("timeout", "error", "failed", etc.) or the
                    # all-or-none broker rule will discard this cache on
                    # the next run, creating an infinite retry loop when
                    # IBKR Flex API is slow (1001) or in maintenance.
                    _using_cache = True
                    xml_str = cache_path.read_text(encoding="utf-8")
                    _, stmt_pos, _, _ = ibkr.parse_flex_xml(xml_str, ibkr.FETCH_START)
                    source_note = f"Flex cache ({_cache_age_h:.0f}h old)"
                    _log.info("ibkr_flex_using_stale_cache",
                              age_h=round(_cache_age_h, 1), reason=str(_flex_exc))
                else:
                    return [], f"Flex Error (no cache): {_flex_exc}"

        if stmt_pos is None or stmt_pos.empty:
            return [], "No open positions found"

        positions = []
        for _, row in stmt_pos.iterrows():
            qty      = float(row.get("qty", 0))
            avg_cost = float(row.get("avg_cost", 0))
            last_p   = float(row.get("last_price", 0) or 0)
            if qty <= 0 or avg_cost <= 0 or not math.isfinite(avg_cost):
                continue
            code     = str(row.get("code", ""))
            ticker   = code.split(".", 1)[1] if "." in code else code
            mval     = last_p * qty
            cost     = avg_cost * qty
            pnl      = mval - cost
            positions.append({
                "symbol":        ticker,
                "shares":        int(qty),
                "avg_cost":      round(avg_cost, 4),
                "current_price": round(last_p, 4),
                "market_val":    round(mval, 2),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl / cost * 100 if cost else 0, 1),
                "currency":      "USD",
                "broker":        "IBKR",
                "exchange":      "US",
            })

        # ---------------------------------------------------------------
        # Flex Query "last_price" is only as fresh as the report IBKR
        # generated (EOD snapshot, or up to 48h-old cache) -- it does NOT
        # reflect intraday moves. Overlay live prices via yfinance (same
        # helper already used for Webull AU) so P&L% matches reality, and
        # keep the Flex-embedded price only as a fallback per symbol.
        # ---------------------------------------------------------------
        if positions:
            try:
                _wb_cgt_dir = str(CGT / "webull-cgt")
                ensure_paths(_wb_cgt_dir)
                from webull_cgt_analysis import fetch_live_prices  # type: ignore
                _live = fetch_live_prices(positions)
                _n_live = 0
                for _p in positions:
                    _lp = _live.get(_p["symbol"], {})
                    _price = _lp.get("price", 0.0)
                    if _price:
                        _n_live += 1
                        _qty = _p["shares"]
                        _cost = _p["avg_cost"] * _qty
                        _mval = _price * _qty
                        _pnl = _mval - _cost
                        _p["current_price"] = round(_price, 4)
                        _p["market_val"]    = round(_mval, 2)
                        _p["pnl"]           = round(_pnl, 2)
                        _p["pnl_pct"]       = round(_pnl / _cost * 100 if _cost else 0, 1)
                if _n_live:
                    source_note += f", live-priced {_n_live}/{len(positions)}"
                    _log.info("ibkr_flex_live_price_overlay", n_live=_n_live, n_total=len(positions))
            except Exception as _lpe:
                _log.debug("ibkr_flex_live_price_overlay_failed", error=str(_lpe))

        return positions, f"Flex {source_note} — {len(positions)} positions"
    except Exception as e:
        if _using_cache:
            return [], f"Cached — parse error: {e}"
        return [], f"Error: {e}"


def _fetch_ibkr_client_portal() -> tuple[list[dict], str]:
    """Fetch IBKR positions via Client Portal REST API.
    Tertiary fallback (after TWS and Flex both fail). Requires Client Portal Gateway
    running on localhost:5000 OR ibind installed for OAuth headless auth."""
    try:
        # ibkr_client_portal.py lives in tradingview/ — add to path if not already present.
        _tv_str = str(TV)
        if _tv_str not in sys.path:
            sys.path.insert(0, _tv_str)
        from ibkr_client_portal import IBKRClientPortal  # type: ignore
        cp = IBKRClientPortal()
        if not cp.is_available:
            return [], "Client Portal unavailable (gateway not running)"
        raw = cp.get_positions()
        positions = []
        for p in raw:
            qty        = float(p.get("qty", p.get("position", 0)))
            avg_cost   = float(p.get("avg_cost", 0))
            mkt_price  = float(p.get("market_price", 0))
            mkt_value  = float(p.get("market_value", 0))
            pnl        = float(p.get("unrealized_pnl", 0))
            cost       = avg_cost * qty if qty > 0 else 0
            pnl_pct    = pnl / cost * 100 if cost else 0
            sym        = str(p.get("symbol", ""))
            if not sym or qty <= 0:
                continue
            positions.append({
                "symbol":        sym,
                "shares":        int(qty),
                "avg_cost":      round(avg_cost, 4),
                "current_price": round(mkt_price, 4),
                "market_val":    round(mkt_value, 2),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 1),
                "currency":      p.get("currency", "USD"),
                "broker":        "IBKR",
                "exchange":      "US",
            })
        return positions, f"Client Portal — {len(positions)} positions"
    except Exception as e:
        return [], f"Client Portal Error: {e}"


def _fetch_ibkr(skip: bool = False) -> tuple[list[dict], str]:
    """Always returns (list, str) — never raises. Chain: TWS → Flex → Client Portal."""
    if skip:
        return [], "Skipped (--no-ibkr)"
    # Priority: TWS live (~150ms) → Flex Query (30-300s) → Client Portal REST
    try:
        result = _fetch_ibkr_live_tws()
    except Exception as _e:
        _log.warning("ibkr_tws_unexpected_exception", error=str(_e))
        result = None
    if result is not None:
        return result
    try:
        positions, msg = _fetch_ibkr_flex()
    except Exception as _e:
        _log.warning("ibkr_flex_unexpected_exception", error=str(_e))
        positions, msg = [], f"Flex Error: {_e}"
    if positions:
        return positions, msg
    # Both TWS and Flex failed — try Client Portal as final fallback
    try:
        cp_positions, cp_msg = _fetch_ibkr_client_portal()
    except Exception as _e:
        _log.warning("ibkr_cp_unexpected_exception", error=str(_e))
        cp_positions, cp_msg = [], f"Client Portal Error: {_e}"
    if cp_positions:
        return cp_positions, cp_msg
    # Return Flex error message (more descriptive) if Client Portal also failed
    return positions, msg


def _fetch_india() -> tuple[list[dict], list[dict], str, str]:
    """Returns (zerodha_positions, angelone_positions, z_status, a_status)."""
    z_pos, a_pos = [], []
    z_status, a_status = "Token not set", "Not attempted"

    ensure_paths(INDIA)
    from zerodha_auth import get_kite, invalidate_token

    # Zerodha
    _z_retried = False
    while True:
        try:
            kite     = get_kite()  # silent=False — triggers fresh login when token expired
            holdings = kite.holdings() or []
            try:
                margins = kite.margins() or {}
            except Exception:
                margins = {}   # margins unavailable outside trading hours — non-fatal
            z_cash   = float((margins.get("equity") or {}).get("available", {}).get("live_balance", 0) or 0)
            for h in holdings:
                qty  = int(h.get("quantity", 0) or 0)
                avg  = float(h.get("average_price", 0) or 0)
                ltp  = float(h.get("last_price", 0) or 0)
                if qty == 0:
                    continue
                cost = avg * qty
                mval = ltp * qty
                pnl  = float(h.get("pnl", 0) or 0)
                sym  = h.get("tradingsymbol", "").rstrip("-EQ").upper()
                z_pos.append({
                    "symbol":        f"NSE:{sym}",
                    "shares":        qty,
                    "avg_cost":      round(avg, 2),
                    "current_price": round(ltp, 2),
                    "market_val":    round(mval, 2),
                    "pnl":           round(pnl, 2),
                    "pnl_pct":       round(pnl / cost * 100 if cost else 0, 1),
                    "currency":      "INR",
                    "broker":        "Zerodha",
                    "exchange":      "NSE",
                })
            z_status = f"Live — {len(z_pos)} positions" + (f" | Cash ₹{z_cash:,.0f}" if z_cash else "")
            break  # success
        except EnvironmentError:
            z_status = "Login skipped — Zerodha holdings missing"
            break
        except Exception as e:
            err = str(e)
            err_low = err.lower()
            err_type = type(e).__name__
            # Broader auth-failure detection (restored 2026-04-21):
            # Kite raises TokenException / PermissionException / generic Exception
            # with strings like "Incorrect `api_key` or `access_token`" or
            # "Insufficient permission for that call." — all indicate a stale token.
            # Exact Kite API auth-error patterns only.
            # IMPORTANT: do NOT match "api_key" or "access_token" alone —
            # those strings appear in many non-auth messages and would cause
            # the guardrail to kill a perfectly valid token (Bug 2026-04-22).
            is_auth_err = (
                "TokenException" in err_type
                or "PermissionException" in err_type
                or (
                    "Incorrect" in err
                    and ("api_key" in err_low or "access_token" in err_low)
                )
                or "insufficient permission" in err_low
                or "token expired" in err_low
            )
            if not _z_retried and is_auth_err:
                # Auth failure — clear stale token and re-authenticate once
                _z_retried = True
                z_pos.clear()
                print(f"  \033[93m⚠  Zerodha token expired ({err_type}). Re-authenticating…\033[0m")
                invalidate_token()
                continue  # retry with fresh token
            else:
                z_status = f"Error: {e}"
                break

    # Angel One
    try:
        import pyotp
        from SmartApi import SmartConnect
        totp  = pyotp.TOTP(os.environ["ANGELONE_TOTP_SECRET"]).now()
        smart = SmartConnect(api_key=os.environ["ANGELONE_API_KEY"])
        sess  = smart.generateSession(
            os.environ["ANGELONE_CLIENT_CODE"],
            os.environ["ANGELONE_PIN"], totp
        )
        if sess.get("status"):
            holdings = (smart.holding().get("data") or [])
            rms      = (smart.rmsLimit().get("data") or {})
            a_cash   = float(rms.get("availablecash", 0) or 0)
            for h in holdings:
                qty = int(h.get("quantity", 0) or 0)
                avg = float(h.get("averageprice", 0) or 0)
                ltp = float(h.get("ltp", 0) or 0)
                if qty == 0:
                    continue
                cost = avg * qty
                mval = ltp * qty
                pnl  = float(h.get("profitandloss", 0) or 0)
                sym  = (h.get("tradingsymbol") or h.get("symbolname") or "").rstrip("-EQ").upper()
                a_pos.append({
                    "symbol":        f"NSE:{sym}",
                    "shares":        qty,
                    "avg_cost":      round(avg, 2),
                    "current_price": round(ltp, 2),
                    "market_val":    round(mval, 2),
                    "pnl":           round(pnl, 2),
                    "pnl_pct":       round(pnl / cost * 100 if cost else 0, 1),
                    "currency":      "INR",
                    "broker":        "AngelOne",
                    "exchange":      "NSE",
                })
            a_status = f"Live — {len(a_pos)} positions" + (f" | Cash ₹{a_cash:,.0f}" if a_cash else "")
        else:
            a_status = "Session failed"
    except Exception as e:
        a_status = f"Error: {e}"

    return z_pos, a_pos, z_status, a_status


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Summary
# ═══════════════════════════════════════════════════════════════════════════════

def _print_coverage_banner(statuses: dict[str, str]) -> None:
    """Print the 5-broker coverage matrix."""
    print(f"\n{BOLD}{CYAN}BROKER COVERAGE{RESET}")
    print(f"  {'─' * 52}")
    CYAN_CONST = "\033[36m"
    icons = {"Live": GREEN + "●" + RESET, "Cached": CYAN_CONST + "◌" + RESET,
             "Skipped": DIM + "○" + RESET, "Error": RED + "✗" + RESET,
             "Token": YELLOW + "!" + RESET, "OpenD": YELLOW + "!" + RESET}
    for broker, status in statuses.items():
        icon = icons["Live"] if status.startswith("Live") else \
               icons["Cached"] if status.startswith("Cached") or status.startswith("Flex") or status.startswith("PDF") else \
               icons["Skipped"] if status.startswith("Skipped") else \
               icons["Token"] if "Token" in status or "expired" in status else \
               icons["OpenD"] if "OpenD" in status else \
               icons["Error"]
        print(f"  {icon}  {BOLD}{broker:<14}{RESET}  {DIM}{status}{RESET}")
    print(f"  {'─' * 52}")


def _print_portfolio_snapshot(all_positions: list[dict],
                               statuses: dict[str, str]) -> None:
    """Print the global portfolio summary to the terminal."""
    _print_coverage_banner(statuses)

    if not all_positions:
        print(f"\n{YELLOW}  No positions retrieved from any broker.{RESET}\n")
        return

    # Group by broker
    brokers_seen = {}
    for pos in all_positions:
        b = pos["broker"]
        brokers_seen.setdefault(b, {"count": 0, "mval_inr": 0.0, "mval_usd": 0.0, "mval_aud": 0.0})
        brokers_seen[b]["count"] += 1

    # Regional grouping
    region_counts: dict[str, int] = {}
    total_positions = len(all_positions)
    for pos in all_positions:
        ex = pos.get("exchange", "US")
        region_counts[ex] = region_counts.get(ex, 0) + 1

    # P&L in native currencies
    pnl_by_currency: dict[str, float] = {}
    val_by_currency: dict[str, float] = {}
    for pos in all_positions:
        cur = pos.get("currency", "USD")
        pnl_by_currency[cur] = pnl_by_currency.get(cur, 0) + pos.get("pnl", 0)
        val_by_currency[cur] = val_by_currency.get(cur, 0) + pos.get("market_val", 0)

    # Top winners / losers
    ranked = sorted(all_positions, key=lambda p: p.get("pnl_pct", 0), reverse=True)
    top3   = ranked[:3]
    bot3   = ranked[-3:][::-1]

    # Pre-compute AUD totals using live FX rates (cached 6h — see fx_rates.py)
    _approx_usd = get_fx_to_usd()
    _aud_rate  = 1.0 / _approx_usd["AUD"]
    usd_total  = sum(val_by_currency.get(c, 0) * _approx_usd.get(c, 1.0) for c in val_by_currency)
    usd_pnl    = sum(pnl_by_currency.get(c, 0) * _approx_usd.get(c, 1.0) for c in pnl_by_currency)
    aud_total  = usd_total * _aud_rate
    aud_pnl    = usd_pnl   * _aud_rate
    _aud_cost  = aud_total - aud_pnl
    _aud_pct   = (aud_pnl / _aud_cost * 100) if _aud_cost else 0
    _tot_col   = GREEN if aud_pnl >= 0 else RED
    _tot_line  = (f"  {BOLD}TOTAL:  A${aud_total:>,.0f}   \u2502   "
                  f"PnL: {_tot_col}A${aud_pnl:>+,.0f}  ({_aud_pct:+.1f}%){RESET}")

    print(f"\n{BOLD}{CYAN}GLOBAL PORTFOLIO SNAPSHOT{RESET}  {DIM}as of {datetime.now().strftime('%Y-%m-%d %H:%M')}{RESET}")
    print(f"  {'─' * 52}")
    print(_tot_line)
    print(f"  {'─' * 52}")
    print(f"  {BOLD}Total open positions:{RESET}  {total_positions}")

    # Per-broker count
    for broker, info in brokers_seen.items():
        print(f"    {DIM}{broker:<18}  {info['count']} positions{RESET}")

    print(f"\n  {BOLD}Portfolio value (by currency):{RESET}")
    sym_map = {"AUD": "A$", "USD": "$", "INR": "₹", "HKD": "HK$",
               "SGD": "S$", "JPY": "¥", "KRW": "₩", "GBP": "£", "CAD": "C$"}
    for cur, val in sorted(val_by_currency.items()):
        sym = sym_map.get(cur, cur)
        pnl = pnl_by_currency.get(cur, 0)
        pnl_col = GREEN if pnl >= 0 else RED
        print(f"    {cur:<5}  {sym}{val:>14,.2f}    PnL: {pnl_col}{sym}{pnl:>+12,.2f}{RESET}")
    pnl_col   = GREEN if usd_pnl >= 0 else RED
    print(f"    {DIM}{'─'*44}{RESET}")
    print(f"    {'TOTAL':5}  ${usd_total:>14,.0f}    PnL: {pnl_col}${usd_pnl:>+12,.0f}{RESET}  {DIM}(\u2248USD){RESET}")
    print(f"    {'':5}  A${aud_total:>13,.0f}    PnL: {pnl_col}A${aud_pnl:>+11,.0f}{RESET}  {DIM}(\u2248AUD){RESET}")

    print(f"\n  {BOLD}Market exposure:{RESET}")
    for ex, cnt in sorted(region_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(cnt, 20)
        print(f"    {ex:<8}  {bar}  {cnt}")

    print(f"\n  {BOLD}Top performers:{RESET}")
    for pos in top3:
        pnl_col = GREEN if pos.get("pnl_pct", 0) >= 0 else RED
        print(f"    {GREEN}▲{RESET}  {pos['symbol']:<22}  "
              f"{pnl_col}{pos.get('pnl_pct',0):>+6.1f}%{RESET}  "
              f"({pos['broker']})")

    print(f"\n  {BOLD}Biggest risks:{RESET}")
    for pos in bot3:
        pnl_col = RED if pos.get("pnl_pct", 0) < 0 else GREEN
        print(f"    {RED}▼{RESET}  {pos['symbol']:<22}  "
              f"{pnl_col}{pos.get('pnl_pct',0):>+6.1f}%{RESET}  "
              f"({pos['broker']})")

    # Missing broker warnings
    _skip_keywords = ("Skipped", "Token not set", "expired", "not set", "OpenD")
    missing_brokers = [(b, s) for b, s in statuses.items()
                       if any(kw in s for kw in _skip_keywords)]
    if missing_brokers:
        print(f"\n  {YELLOW}⚠  MISSING BROKERS (data incomplete):{RESET}")
        for _b, _s in missing_brokers:
            print(f"    {YELLOW}• {_b}: {_s}{RESET}")

    print(f"\n  {BOLD}Next actions:{RESET}")
    protected = sum(1 for p in all_positions if p.get("broker") in ("Zerodha", "AngelOne"))
    india_count = sum(1 for p in all_positions if p.get("currency") == "INR")
    if india_count > 0:
        print(f"    • Check India GTT stop-losses ({india_count} India positions)")
    print(f"    • Run Sell Strategy for exit recommendations")
    print(f"    • Open full HTML report for position-level detail")
    print(f"  {'─' * 52}")
    print(_tot_line)
    print(f"  {'─' * 52}")
    print(f"\n  Report: {OUTPUT}")
    print(f"  {'─' * 52}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Realised P&L History
# ═══════════════════════════════════════════════════════════════════════════════



def _fetch_realised_pnl() -> dict:
    """
    Gather historical realised P&L data from all available sources.

    Returns:
        {
          "ibkr": [{"fy":"FY2024","period":"...","realized_usd":4566,"deposits_aud":40601,...}],
          "moomoo": [{"fy":"FY2024","currency":"USD","net_taxable":...},...],  # if available
          "india":  [],   # always empty – API limitation
          "webull": [],   # empty unless CSV available
          "status": {...}
        }
    """
    # ── Cache check — PDF parsing takes 30-40s; reuse if < 12h old ──────────
    _pnl_cached = _load_realised_pnl_cache()
    if _pnl_cached is not None:
        print(f"{DIM}  (Realised P&L from cache — < {_REALISED_PNL_CACHE_TTL // 3600}h old){RESET}")
        return _pnl_cached

    _fx_rates = get_fx_to_usd()  # live rates (cached 6h) — see fx_rates.py
    result: dict = {"ibkr": [], "moomoo": [], "india": [], "webull": [], "monthly_trades": {}, "status": {}}

    # ── IBKR: Parse annual PDF statements ────────────────────────────────────
    ibkr_stmt_dir = ROOT / "ibkr-statement"
    try:
        import importlib.util as _ul
        _spec = _ul.spec_from_file_location(
            "ibkr_statement_reader",
            str(ibkr_stmt_dir / "ibkr_statement_reader.py")
        )
        _mod = _ul.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        ibkr_data = _mod.build_realised_summary_for_dashboard(ibkr_stmt_dir)
        result["ibkr"] = ibkr_data.get("ibkr", [])
        result["status"]["IBKR PDFs"] = ibkr_data.get("status", "OK")
    except Exception as e:
        result["status"]["IBKR PDFs"] = f"Error: {e}"

    # ── Moomoo: Try CGT analysis (requires OpenD); cache results ─────────────
    _moomoo_cache_path = CGT / "moomoo-cgt" / "_moomoo_cgt_cache.json"
    try:
        if _is_port_open(11111):
            cgt_dir = str(CGT / "moomoo-cgt")
            ensure_paths(cgt_dir)
            import cgt_analysis as _cgt_au
            _trades_df = _cgt_au.fetch_deals(_cgt_au.FETCH_START)
            _ledger = _cgt_au.FifoLedger()
            # Sort chronologically for correct FIFO matching (same as cgt_analysis.py main)
            _trades_df = _trades_df.copy()
            _trades_df.columns = [c.lower() for c in _trades_df.columns]
            _trades_df = _trades_df.sort_values("create_time").reset_index(drop=True)
            from datetime import datetime as _dt_cls
            for _, row in _trades_df.iterrows():
                _code  = str(row.get("code", "") or "")
                _cur   = _cgt_au.infer_currency(_code)
                _qty   = float(row.get("qty", 0) or 0)
                _price = float(row.get("price", 0) or 0)
                _dt    = str(row.get("create_time", "") or "")
                # Determine side from trd_side (qty is always positive in Moomoo API)
                _side  = str(row.get("trd_side", "")).upper().split(".")[-1]
                try:
                    _d = _dt_cls.strptime(_dt[:10], "%Y-%m-%d").date()
                except Exception:
                    continue
                if _side == "BUY":
                    _ledger.buy(_code, _d, _price, _qty, _cur)
                elif _side == "SELL":
                    _ledger.sell(_code, _d, _price, _qty, _cur)
            _fy_df = _cgt_au.fy_summary(_ledger.matched)
            if not _fy_df.empty:
                for _, row in _fy_df.iterrows():
                    result["moomoo"].append({
                        "fy":          row["FY"],
                        "broker":      "Moomoo-AU",
                        "currency":    row["Currency"],
                        "st_gains":    float(row["ST Gains"]),
                        "st_losses":   float(row["ST Losses"]),
                        "lt_gross":    float(row["LT Gross Gains"]),
                        "lt_losses":   float(row["LT Losses"]),
                        "net_taxable": float(row["Net Taxable"]),
                        "n_trades":    int(row["# Trades"]),
                    })
            # Save to cache for use when OpenD is offline
            try:
                from datetime import date as _date_cls
                _moomoo_cache_path.parent.mkdir(parents=True, exist_ok=True)
                _moomoo_cache_path.write_text(
                    json.dumps({"fetched_at": str(_date_cls.today()), "data": result["moomoo"]},
                               indent=2),
                    encoding="utf-8"
                )
            except Exception as exc:
                _log.debug("moomoo_cache_save_failed", error=str(exc))
            # ── Extract monthly trade P&L for month-level cumulative chart ──
            for _mt in _ledger.matched:
                _sd = _mt.get("sell_date")
                if _sd:
                    _mk = str(_sd)[:7]
                    _gain_aud = to_aud(float(_mt.get("taxable_gain", 0.0)), str(_mt.get("currency", "AUD")), _fx_rates)
                    result["monthly_trades"][_mk] = result["monthly_trades"].get(_mk, 0.0) + _gain_aud
            result["status"]["Moomoo CGT"] = f"Live — {len(result['moomoo'])} FY rows"
        else:
            # OpenD offline — try cache
            if _moomoo_cache_path.exists():
                try:
                    _cached = json.loads(_moomoo_cache_path.read_text(encoding="utf-8"))
                    result["moomoo"] = _cached.get("data", [])
                    # Backfill broker tag for legacy cache rows that predate the broker field
                    for _r in result["moomoo"]:
                        _r.setdefault("broker", "Moomoo-AU")
                    _fetched_at = _cached.get("fetched_at", "unknown date")
                    result["status"]["Moomoo CGT"] = (
                        f"Cached ({_fetched_at}) — {len(result['moomoo'])} FY rows "
                        f"[OpenD offline]"
                    )
                except Exception as _ce:
                    result["status"]["Moomoo CGT"] = f"OpenD offline; cache read error: {_ce}"
            else:
                result["status"]["Moomoo CGT"] = "OpenD not running — no cache yet"
    except Exception as e:
        result["status"]["Moomoo CGT"] = f"Error: {e}"

    # ── Moomoo SG CGT: separate fetch via port 11112 ──────────────────────────
    # CRITICAL: SG rows MUST be kept separate from AU rows.  Moomoo-SG trades
    # Japanese stocks (JPY-denominated), HKD, SGD and USD.  Mixing them with AU
    # rows without a broker tag caused JPY amounts to be summed at wrong FX →
    # A$+270,119 nonsense (bug fixed 2026-04-21).
    _moomoo_sg_cache_path = CGT / "moomoo-cgt" / "_moomoo_sg_cgt_cache.json"
    try:
        if _is_port_open(11112):
            cgt_sg_dir = str(CGT / "moomoo-cgt")
            ensure_paths(cgt_sg_dir)
            import importlib, cgt_analysis as _cgt_sg_base  # type: ignore
            # Reload to get SG-specific config if module was already imported for AU
            _cgt_sg = importlib.import_module("cgt_analysis")
            # Override module-level PORT/FIRM/ACCOUNT_ID for SG account
            # (fetch_deals() uses module globals; passing port= as kwarg is not supported)
            from moomoo_config import OPEND_SG_PORT as _SG_PORT, SG_FIRM as _SG_FIRM, SG_ACCOUNT_ID as _SG_ACCT
            _cgt_sg.PORT       = _SG_PORT
            _cgt_sg.FIRM       = _SG_FIRM
            _cgt_sg.ACCOUNT_ID = _SG_ACCT
            _sg_trades_df = _cgt_sg.fetch_deals(_cgt_sg.FETCH_START)
            _sg_ledger = _cgt_sg.FifoLedger()
            _sg_trades_df = _sg_trades_df.copy()
            _sg_trades_df.columns = [c.lower() for c in _sg_trades_df.columns]
            _sg_trades_df = _sg_trades_df.sort_values("create_time").reset_index(drop=True)
            from datetime import datetime as _dt_cls2
            for _, row in _sg_trades_df.iterrows():
                _code  = str(row.get("code", "") or "")
                _cur   = _cgt_sg.infer_currency(_code)
                _qty   = float(row.get("qty", 0) or 0)
                _price = float(row.get("price", 0) or 0)
                _dt2   = str(row.get("create_time", "") or "")
                _side  = str(row.get("trd_side", "")).upper().split(".")[-1]
                try:
                    _d2 = _dt_cls2.strptime(_dt2[:10], "%Y-%m-%d").date()
                except Exception:
                    continue
                if _side == "BUY":
                    _sg_ledger.buy(_code, _d2, _price, _qty, _cur)
                elif _side == "SELL":
                    _sg_ledger.sell(_code, _d2, _price, _qty, _cur)
            _sg_fy_df = _cgt_sg.fy_summary(_sg_ledger.matched)
            _sg_rows: list[dict] = []
            if not _sg_fy_df.empty:
                for _, row in _sg_fy_df.iterrows():
                    _sg_rows.append({
                        "fy":          row["FY"],
                        "broker":      "Moomoo-SG",
                        "currency":    row["Currency"],
                        "st_gains":    float(row["ST Gains"]),
                        "st_losses":   float(row["ST Losses"]),
                        "lt_gross":    float(row["LT Gross Gains"]),
                        "lt_losses":   float(row["LT Losses"]),
                        "net_taxable": float(row["Net Taxable"]),
                        "n_trades":    int(row["# Trades"]),
                    })
            result["moomoo"].extend(_sg_rows)
            # Save SG cache
            try:
                from datetime import date as _date_sg
                _moomoo_sg_cache_path.parent.mkdir(parents=True, exist_ok=True)
                _moomoo_sg_cache_path.write_text(
                    json.dumps({"fetched_at": str(_date_sg.today()), "data": _sg_rows}, indent=2,
                               default=str),
                    encoding="utf-8"
                )
            except Exception as exc:
                _log.debug("moomoo_sg_cache_save_failed", error=str(exc))
            result["status"]["Moomoo SG CGT"] = f"Live — {len(_sg_rows)} FY rows"
        else:
            # OpenD SG offline — try cache
            if _moomoo_sg_cache_path.exists():
                try:
                    _sg_cached = json.loads(_moomoo_sg_cache_path.read_text(encoding="utf-8"))
                    _sg_cached_rows = _sg_cached.get("data", [])
                    # Backfill broker tag on legacy rows
                    for _r in _sg_cached_rows:
                        _r.setdefault("broker", "Moomoo-SG")
                    result["moomoo"].extend(_sg_cached_rows)
                    _sg_fetched_at = _sg_cached.get("fetched_at", "unknown date")
                    result["status"]["Moomoo SG CGT"] = (
                        f"Cached ({_sg_fetched_at}) — {len(_sg_cached_rows)} FY rows "
                        f"[OpenD SG offline]"
                    )
                except Exception as _sg_ce:
                    result["status"]["Moomoo SG CGT"] = f"OpenD SG offline; cache error: {_sg_ce}"
            else:
                result["status"]["Moomoo SG CGT"] = "OpenD SG not running — no cache yet"
    except Exception as _sg_e:
        result["status"]["Moomoo SG CGT"] = f"Error: {_sg_e}"

    # ── India: Parse Zerodha XLSX + AngelOne PDF statements ──────────────────
    _INR_TO_AUD = 0.018
    try:
        import importlib.util as _iul
        rows_z: list = []
        _zpath = ROOT / "zerodha-statement" / "zerodha_statement_reader.py"
        if _zpath.exists():
            _spec = _iul.spec_from_file_location("zerodha_statement_reader", str(_zpath))
            _mod = _iul.module_from_spec(_spec); _spec.loader.exec_module(_mod)
            rows_z = _mod.build_summary_for_dashboard(folder=ROOT / "zerodha-statement")
        rows_ao: list = []
        _aopath = ROOT / "angelone-statement" / "angelone_statement_reader.py"
        if _aopath.exists():
            _spec = _iul.spec_from_file_location("angelone_statement_reader", str(_aopath))
            _mod = _iul.module_from_spec(_spec); _spec.loader.exec_module(_mod)
            rows_ao = _mod.build_summary_for_dashboard(folder=ROOT / "angelone-statement")
        for r in rows_z + rows_ao:
            net_inr = r.get("net_inr", 0.0)
            result["india"].append({
                "fy":            r["fy"],
                "broker":        r["broker"],
                "net_inr":       net_inr,
                "net_aud":       round(net_inr * _INR_TO_AUD, 2),
                "period":        r.get("period", ""),
                "st_inr":        r.get("st_inr", 0.0),
                "lt_inr":        r.get("lt_inr", 0.0),
                "intraday_inr":  r.get("intraday_inr", 0.0),
                "options_inr":   r.get("options_inr", 0.0),
                "mf_ltcg_inr":   r.get("mf_ltcg_inr", 0.0),
                "dividends_inr": r.get("dividends_inr", 0.0),
            })
        n = len(result["india"])
        result["status"]["India Stmts"] = f"{n} rows ({len(rows_z)} Zerodha + {len(rows_ao)} AngelOne)"
    except Exception as _ie:
        result["status"]["India Stmts"] = f"Error: {_ie}"

    # ── Webull CGT: read from PDF statements ──────────────────────────────────
    _webull_stmt_dir = str(ROOT / "webull-statement")
    try:
        _wb_cgt_dir = str(CGT / "webull-cgt")
        ensure_paths(_wb_cgt_dir)
        import webull_cgt_analysis as _wb_cgt  # type: ignore
        _wb_data = _wb_cgt.get_data(_webull_stmt_dir)
        _wb_fy_df = _wb_data.get("fy_sum_df")
        if _wb_fy_df is not None and not _wb_fy_df.empty:
            for _, _row in _wb_fy_df.iterrows():
                result["webull"].append({
                    "fy":          _row["FY"],
                    "currency":    _row["Currency"],
                    "st_gains":    float(_row["ST Gains"]),
                    "st_losses":   float(_row["ST Losses"]),
                    "lt_gross":    float(_row["LT Gross Gains"]),
                    "lt_losses":   float(_row["LT Losses"]),
                    "net_taxable": float(_row["Net Taxable"]),
                    "n_trades":    int(_row["# Trades"]),
                })
        # ── Extract monthly trade P&L for month-level cumulative chart ──
        for _wt in _wb_data.get("matched", []):
            _sd2 = _wt.get("sell_date")
            if _sd2:
                _mk2 = str(_sd2)[:7]
                _gain_aud2 = to_aud(float(_wt.get("taxable_gain", 0.0)), str(_wt.get("currency", "AUD")), _fx_rates)
                result["monthly_trades"][_mk2] = result["monthly_trades"].get(_mk2, 0.0) + _gain_aud2
        n_wb = len(result["webull"])
        result["status"]["Webull CGT"] = f"PDF statements — {n_wb} FY rows"
    except Exception as _wbe:
        result["status"]["Webull CGT"] = f"Error reading PDFs: {_wbe}"

    # ── Extract individual deposit/withdrawal events with dates ──────────────
    result["deposit_events"] = _extract_all_deposit_events()
    _save_realised_pnl_cache(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Deposit / Withdrawal Event Extraction
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_all_deposit_events() -> list[dict]:
    """
    Parse ALL broker statements to extract individual deposit/withdrawal events.
    Returns list of {"date": "YYYY-MM-DD", "amount": float, "broker": str, "currency": "AUD"}.
    Positive = deposit, Negative = withdrawal.  All amounts normalised to AUD.
    """
    events: list[dict] = []
    events.extend(_extract_ibkr_deposits())
    events.extend(_extract_webull_deposits())
    events.extend(_estimate_moomoo_deposits())
    events.extend(_estimate_zerodha_deposits())
    events.extend(_estimate_angelone_deposits())
    events.sort(key=lambda e: e["date"])
    return events


def _extract_ibkr_deposits() -> list[dict]:
    """Parse individual EFT lines from IBKR annual statement PDFs."""
    import re
    events = []
    ibkr_dir = ROOT / "ibkr-statement"
    if not ibkr_dir.exists():
        return events
    try:
        import pdfplumber
    except ImportError:
        return events

    for pdf_path in sorted(ibkr_dir.glob("*.pdf")):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            # Find "Electronic Fund Transfer" lines with date + amount
            for m in re.finditer(
                r'(\d{4}-\d{2}-\d{2})\s+Electronic Fund Transfer\s+([\d,]+\.\d{2})',
                text
            ):
                dt = m.group(1)
                amt = float(m.group(2).replace(",", ""))
                if amt > 0:
                    events.append({"date": dt, "amount": amt, "broker": "IBKR", "currency": "AUD"})
        except Exception as exc:
            _log.debug("ibkr_deposit_extract_failed", error=str(exc))
    return events


def _extract_webull_deposits() -> list[dict]:
    """Parse deposit/withdrawal events from Webull EOFY + monthly statement PDFs."""
    import re
    events = []
    wb_dir = ROOT / "webull-statement"
    if not wb_dir.exists():
        return events
    try:
        import pdfplumber
    except ImportError:
        return events

    _eofy_months_covered: set[str] = set()  # months covered by monthly statements

    # ── Pass 1: Monthly statements (higher granularity — exact dates) ────────
    for pdf_path in sorted(wb_dir.glob("Webull_Monthly*.pdf")):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            lines = text.split("\n")
            in_deposit_section = False
            is_deposit = True

            for line in lines:
                stripped = line.strip()
                if "DEPOSIT & WITHDRAWAL RECORDS" in stripped:
                    in_deposit_section = True
                    continue
                if not in_deposit_section:
                    continue
                if any(marker in stripped for marker in ("DIVIDENDS", "TRADE RECORDS",
                       "WITHHOLDING TAX", "ACCRUED DIVIDENDS", "PORTFOLIO VALUATION")):
                    in_deposit_section = False
                    continue
                if stripped == "Deposits":
                    is_deposit = True
                    continue
                if stripped == "Withdrawals":
                    is_deposit = False
                    continue

                # Parse DD/MM/YYYY ... amount lines
                m = re.match(r'(\d{2}/\d{2}/\d{4})\s+.*?(-?[\d,]+\.\d{2})\s*$', stripped)
                if m:
                    dt_raw = m.group(1)
                    amt = float(m.group(2).replace(",", ""))
                    # Convert DD/MM/YYYY → YYYY-MM-DD
                    d, mo, y = dt_raw.split("/")
                    dt = f"{y}-{mo}-{d}"
                    if not is_deposit:
                        amt = -abs(amt)
                    events.append({"date": dt, "amount": amt, "broker": "Webull", "currency": "AUD"})
                    _eofy_months_covered.add(dt[:7])

                # Also handle simpler format: DD/MM/YYYY CUR amount
                m2 = re.match(r'(\d{2}/\d{2}/\d{4})\s+[A-Z]{3}\s+(-?[\d,]+\.\d{2})\s*$', stripped)
                if m2 and not m:
                    dt_raw = m2.group(1)
                    amt = float(m2.group(2).replace(",", ""))
                    d, mo, y = dt_raw.split("/")
                    dt = f"{y}-{mo}-{d}"
                    if not is_deposit:
                        amt = -abs(amt)
                    events.append({"date": dt, "amount": amt, "broker": "Webull", "currency": "AUD"})
                    _eofy_months_covered.add(dt[:7])
        except Exception as exc:
            _log.debug("webull_monthly_deposit_extract_failed", error=str(exc))
    for pdf_path in sorted(wb_dir.glob("Webull_EOFY*.pdf")):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            # Extract total deposits from EOFY
            dep_match = re.search(r'Deposits\s+[\d,.]+\s+(-?[\d,]+\.\d{2})\s+[\d,.]+\s+[\d,.]+\s+([\d,]+\.\d{2})', text)
            if not dep_match:
                dep_match = re.search(r'Deposits\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+[\d,.]+\s+[\d,.]+\s+([\d,]+\.\d{2})', text)

            # Extract FY period from filename
            fy_match = re.search(r'(\d{4})_(\d{4})', pdf_path.name)
            if fy_match:
                start_yr = int(fy_match.group(1))
                # EOFY covers July start_yr to June end_yr
                # Place unaccounted deposit at mid-FY (Jan of end year)
                mid_date = f"{start_yr + 1}-01-15"

                # Get total AUD deposits from the line
                for m in re.finditer(
                    r'Deposits\s+([\d,.]+)\s+([\d,]+\.\d{2})\s+([\d,.]+)\s+([\d,.]+)\s+([\d,]+\.\d{2})',
                    text
                ):
                    total_aud = float(m.group(5).replace(",", ""))
                    # Subtract deposits already covered by monthly statements
                    monthly_covered = sum(
                        e["amount"] for e in events
                        if e["broker"] == "Webull" and e["amount"] > 0
                        and f"{start_yr}-07" <= e["date"][:7] <= f"{start_yr+1}-06"
                    )
                    remaining = total_aud - monthly_covered
                    if remaining > 50:  # only add if meaningful
                        events.append({
                            "date": mid_date, "amount": remaining,
                            "broker": "Webull", "currency": "AUD"
                        })
                    break
        except Exception as exc:
            _log.debug("webull_eofy_deposit_extract_failed", error=str(exc))

    return events


def _estimate_moomoo_deposits() -> list[dict]:
    """
    Estimate Moomoo deposits from position cost basis (best available proxy).
    Moomoo doesn't provide deposit history via API.
    """
    events = []
    # Use current positions cost basis as proxy for total capital deployed
    # The first trade was in FY2025 (Jul 2024+), estimate initial deposit
    _cache = CGT / "moomoo-cgt" / "_moomoo_cgt_cache.json"
    if not _cache.exists():
        return events

    try:
        import json as _j
        data = _j.loads(_cache.read_text(encoding="utf-8"))
        fys = sorted(set(r["fy"] for r in data.get("data", [])))
        if fys:
            first_fy = fys[0]  # e.g. "FY2025"
            if first_fy.startswith("FY"):
                yr = int(first_fy[2:])
                # Estimate total capital from gross volume of gains + losses
                # This is a rough proxy — actual deposits were likely at start of trading
                total_gross = sum(
                    abs(r.get("st_gains", 0)) + abs(r.get("lt_gross", 0))
                    + abs(r.get("st_losses", 0)) + abs(r.get("lt_losses", 0))
                    for r in data.get("data", [])
                )
                # Can't determine exact deposits without API — skip Moomoo deposits
                # (avoids double-counting; chart annotation will note this)
    except Exception as exc:
        _log.debug("moomoo_deposit_estimate_failed", error=str(exc))
    return events


def _estimate_zerodha_deposits() -> list[dict]:
    """
    Zerodha has no deposit history API — returns empty list.
    Deposits are not trackable via KiteConnect; chart annotation notes this.
    """
    return []


def _estimate_angelone_deposits() -> list[dict]:
    """
    AngelOne has no deposit history API — returns empty list.
    Deposits are not trackable via SmartAPI; chart annotation notes this.
    """
    return []











# ═══════════════════════════════════════════════════════════════════════════════
# FD Benchmark — All 6 Brokers
# ═══════════════════════════════════════════════════════════════════════════════

# Earliest confirmed investment date per broker (updated from PDF statements and CGT reports).
# IBKR + Webull: from actual EFT deposit lines in PDF statements.
# Moomoo-AU / Moomoo-SG: first trade date in Moomoo CGT report (2024-02-08).
# Zerodha / AngelOne: FY2024 start (April 2024 = start of Indian FY24-25).
_BROKER_FIRST_INVEST_DATE: dict[str, str] = {
    "IBKR":      "2024-02-12",
    "Webull AU": "2024-01-15",
    "Moomoo-AU": "2024-02-08",
    "Moomoo-SG": "2024-02-08",
    "Zerodha":   "2024-04-01",
    "AngelOne":  "2024-04-01",
}

# NOTE: FX rates are fetched live via fx_rates.get_fx_to_usd() inside
# _build_fd_comparison_section() below — see fx_rates.py for source/cache/fallback.


def _build_fd_comparison_section(all_positions: list[dict]) -> str:
    """
    Per-broker FD vs Portfolio benchmark chart.

    For each of the 6 brokers shows 3 grouped bars (AUD):
      • Cost Basis       — total capital invested (shares × avg_cost → AUD)
      • FD @ 8%          — what that capital would be worth today at 8% p.a. compound
      • Portfolio Value  — current market value (AUD)

    Start dates come from _BROKER_FIRST_INVEST_DATE (PDF statements / CGT reports).
    Falls back to 2024-01-01 for any unknown broker.
    """
    import json as _json
    from datetime import date as _date, datetime as _dt

    today = _date.today()
    _fx_rates = get_fx_to_usd()

    cost_by_broker: dict[str, float] = {}
    val_by_broker:  dict[str, float] = {}

    for pos in all_positions:
        broker = pos.get("broker", "Unknown")
        ccy    = pos.get("currency", "USD")
        shares = pos.get("shares", 0) or 0
        avg_cost = pos.get("avg_cost", 0) or 0
        mval   = pos.get("market_val", 0) or 0
        cost_by_broker[broker] = cost_by_broker.get(broker, 0.0) + to_aud(shares * avg_cost, ccy, _fx_rates)
        val_by_broker[broker]  = val_by_broker.get(broker, 0.0)  + to_aud(mval, ccy, _fx_rates)

    if not cost_by_broker:
        return ""

    fd_by_broker:    dict[str, float] = {}
    years_by_broker: dict[str, float] = {}

    for broker, cost_aud in cost_by_broker.items():
        date_str = _BROKER_FIRST_INVEST_DATE.get(broker, "2024-01-01")
        try:
            first = _date.fromisoformat(date_str)
        except ValueError:
            first = _date(2024, 1, 1)
        years = max((today - first).days / 365.25, 0.01)
        fd_by_broker[broker]    = cost_aud * (1.08 ** years)
        years_by_broker[broker] = years

    brokers    = sorted(cost_by_broker.keys())
    cost_data  = [round(cost_by_broker.get(b, 0.0), 0) for b in brokers]
    fd_data    = [round(fd_by_broker.get(b, 0.0), 0)   for b in brokers]
    port_data  = [round(val_by_broker.get(b, 0.0), 0)  for b in brokers]

    # Colour: green if portfolio beats FD, red if below
    port_colors = []
    for b in brokers:
        port_colors.append("#4ade80" if val_by_broker.get(b, 0) >= fd_by_broker.get(b, 0) else "#f87171")

    port_bg = [c.replace("#4ade80", "rgba(74,222,128,0.65)").replace("#f87171", "rgba(248,113,113,0.65)")
               for c in port_colors]

    # Summary table rows
    rows = ""
    for b in brokers:
        cost = cost_by_broker.get(b, 0.0)
        port = val_by_broker.get(b, 0.0)
        fd   = fd_by_broker.get(b, 0.0)
        yrs  = years_by_broker.get(b, 0.0)
        ret_pct = ((port - cost) / cost * 100) if cost else 0.0
        fd_pct  = ((fd   - cost) / cost * 100) if cost else 0.0
        cls     = "pos" if port >= fd else "neg"
        verdict = "&#x2705; Beating FD" if port >= fd else "&#x26A0;&#xFE0F; Below FD"
        start_dt = _BROKER_FIRST_INVEST_DATE.get(b, "2024-01-01")
        rows += (
            f"<tr>"
            f"<td><strong>{b}</strong></td>"
            f"<td class='num dim'>{start_dt}</td>"
            f"<td class='num'>A${cost:,.0f}</td>"
            f"<td class='num'>A${fd:,.0f} <span class='dim'>({fd_pct:+.1f}%)</span></td>"
            f"<td class='num {cls}'>A${port:,.0f} <span class='dim'>({ret_pct:+.1f}%)</span></td>"
            f"<td class='num dim'>{yrs:.1f}y</td>"
            f"<td class='{cls}'>{verdict}</td>"
            f"</tr>\n"
        )

    return f"""
<h2>&#x1F4CA; FD vs Portfolio Benchmark &#8212; All 6 Brokers</h2>
<p class='dim' style='margin-top:-8px;margin-bottom:16px'>
  Compares current AUD portfolio value against 8&thinsp;% p.a. compound fixed deposit.
  Capital&nbsp;=&nbsp;cost basis (shares&times;avg&nbsp;cost). Start&nbsp;date = earliest
  deposit or first confirmed trade per broker.
</p>
<div style="position:relative;height:340px;margin-bottom:24px">
<canvas id="fdBrokerChart"></canvas>
</div>
<table class='t'>
<thead>
  <tr>
    <th scope='col'>Broker</th><th scope='col'>Since</th><th scope='col'>Cost Basis</th>
    <th scope='col'>FD @ 8%</th><th scope='col'>Portfolio Value</th><th scope='col'>Held</th><th scope='col'>Verdict</th>
  </tr>
</thead>
<tbody>{rows}</tbody>
</table>
<script>
(function(){{
  var ctx = document.getElementById('fdBrokerChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {_json.dumps(brokers)},
      datasets: [
        {{
          label: 'Cost Basis (A$)',
          data: {_json.dumps(cost_data)},
          backgroundColor: 'rgba(148,163,184,0.5)',
          borderColor: '#94a3b8',
          borderWidth: 1
        }},
        {{
          label: 'FD @ 8% p.a.',
          data: {_json.dumps(fd_data)},
          backgroundColor: 'rgba(251,191,36,0.55)',
          borderColor: '#fbbf24',
          borderWidth: 1
        }},
        {{
          label: 'Portfolio Value (A$)',
          data: {_json.dumps(port_data)},
          backgroundColor: {_json.dumps(port_bg)},
          borderColor: {_json.dumps(port_colors)},
          borderWidth: 2
        }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'top', labels: {{ color: '#e2e8f0' }} }},
        tooltip: {{
          callbacks: {{
            label: function(ctx) {{
              return ctx.dataset.label + ': A$' + ctx.parsed.y.toLocaleString();
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
        y: {{
          ticks: {{
            color: '#94a3b8',
            callback: function(v) {{ return 'A$' + (v/1000).toFixed(0) + 'k'; }}
          }},
          grid: {{ color: '#334155' }}
        }}
      }}
    }}
  }});
}})();
</script>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# HTML Report
# ═══════════════════════════════════════════════════════════════════════════════



def generate_html(
    all_positions: list[dict],
    statuses: dict[str, str],
    realised_data: dict | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Coverage table
    cov_rows = "\n".join(_html_coverage_row(b, s) for b, s in statuses.items())

    # Currency valuation table
    pnl_by_cur: dict[str, float] = {}
    val_by_cur: dict[str, float] = {}
    cnt_by_cur: dict[str, int]   = {}
    for pos in all_positions:
        cur = pos.get("currency", "USD")
        pnl_by_cur[cur] = pnl_by_cur.get(cur, 0) + pos.get("pnl", 0)
        val_by_cur[cur] = val_by_cur.get(cur, 0) + pos.get("market_val", 0)
        cnt_by_cur[cur] = cnt_by_cur.get(cur, 0) + 1

    sym_map = {"AUD":"A$","USD":"$","INR":"₹","HKD":"HK$","SGD":"S$",
               "JPY":"¥","KRW":"₩","GBP":"£","CAD":"C$"}

    val_rows = ""
    for cur, val in sorted(val_by_cur.items()):
        s   = sym_map.get(cur, cur)
        pnl = pnl_by_cur.get(cur, 0)
        cnt = cnt_by_cur.get(cur, 0)
        cls = "pos" if pnl >= 0 else "neg"
        val_rows += (f"<tr><td><strong>{cur}</strong></td>"
                     f"<td class='num'>{s}{val:,.2f}</td>"
                     f"<td class='num {cls}'>{s}{pnl:+,.2f}</td>"
                     f"<td class='num'>{cnt}</td></tr>\n")
    # Live-FX totals (cached 6h — see fx_rates.py)
    _approx_fx = get_fx_to_usd()
    _usd_tot = sum(val_by_cur.get(c, 0) * _approx_fx.get(c, 1.0) for c in val_by_cur)
    _usd_pnl = sum(pnl_by_cur.get(c, 0) * _approx_fx.get(c, 1.0) for c in pnl_by_cur)
    _aud_tot = _usd_tot / _approx_fx["AUD"]
    _aud_pnl = _usd_pnl / _approx_fx["AUD"]
    _tot_cls = "pos" if _usd_pnl >= 0 else "neg"
    val_rows += (f"<tr style='border-top:2px solid #30363d;background:#161b22'>"
                 f"<td><strong>TOTAL ≈USD</strong></td>"
                 f"<td class='num'><strong>${_usd_tot:,.0f}</strong></td>"
                 f"<td class='num {_tot_cls}'><strong>${_usd_pnl:+,.0f}</strong></td>"
                 f"<td class='num dim'>all</td></tr>\n")
    val_rows += (f"<tr style='background:#161b22'>"
                 f"<td><strong>TOTAL ≈AUD</strong></td>"
                 f"<td class='num'><strong>A${_aud_tot:,.0f}</strong></td>"
                 f"<td class='num {_tot_cls}'><strong>A${_aud_pnl:+,.0f}</strong></td>"
                 f"<td class='num dim'>indicative</td></tr>\n")

    # Profit/loss counts and overall PnL%
    profit_count = sum(1 for p in all_positions if p.get("pnl", 0) > 0)
    loss_count   = sum(1 for p in all_positions if p.get("pnl", 0) < 0)
    flat_count   = sum(1 for p in all_positions if p.get("pnl", 0) == 0)
    _usd_total_cost = sum(
        (p.get("avg_cost", 0) * p.get("shares", 0)) * _approx_fx.get(p.get("currency", "USD"), 1.0)
        for p in all_positions
    )
    overall_pnl_pct = (_usd_pnl / _usd_total_cost * 100) if _usd_total_cost else 0.0
    ovr_cls = "pos" if overall_pnl_pct >= 0 else "neg"

    # Per-broker PnL — pre-seed all 6 known brokers so offline ones still appear
    broker_stats: dict = {b: {"count": 0, "pnl_usd": 0.0, "cost_usd": 0.0} for b in _KNOWN_BROKERS}
    for pos in all_positions:
        b   = pos.get("broker", "Unknown")
        cur = pos.get("currency", "USD")
        _fx = _approx_fx.get(cur, 1.0)
        if b not in broker_stats:
            broker_stats[b] = {"count": 0, "pnl_usd": 0.0, "cost_usd": 0.0}
        broker_stats[b]["count"]    += 1
        broker_stats[b]["pnl_usd"]  += pos.get("pnl", 0) * _fx
        broker_stats[b]["cost_usd"] += pos.get("avg_cost", 0) * pos.get("shares", 0) * _fx

    # Render all known brokers first, then any unknowns; offline (count==0) shown dimmed
    _all_broker_keys = list(_KNOWN_BROKERS) + sorted(k for k in broker_stats if k not in _KNOWN_BROKERS)
    broker_rows = ""
    for b in _all_broker_keys:
        st = broker_stats[b]
        if st["count"] == 0:
            broker_rows += (
                f"<tr style='opacity:.45'>"
                f"<td><strong>{b}</strong></td>"
                f"<td class='num dim'>\u2014</td>"
                f"<td class='num dim'>\u2014</td>"
                f"<td class='num dim'>offline</td>"
                f"</tr>\n"
            )
        else:
            pnl_usd = st["pnl_usd"]
            pnl_pct = (pnl_usd / st["cost_usd"] * 100) if st["cost_usd"] else 0.0
            _bcls   = "pos" if pnl_usd >= 0 else "neg"
            broker_rows += (
                f"<tr>"
                f"<td><strong>{b}</strong></td>"
                f"<td class='num'>{st['count']}</td>"
                f"<td class='num {_bcls}'>${pnl_usd:+,.0f}</td>"
                f"<td class='num {_bcls}'>{pnl_pct:+.1f}%</td>"
                f"</tr>\n"
            )

    # Positions table
    pos_rows = ""
    sorted_pos = sorted(all_positions, key=lambda p: p.get("pnl_pct", 0), reverse=True)
    for pos in sorted_pos:
        pct = pos.get("pnl_pct", 0)
        cls = "pos" if pct >= 0 else "neg"
        bar_w = min(abs(pct) * 2, 100)
        bar_col = "#2ecc71" if pct >= 0 else "#e74c3c"
        s = sym_map.get(pos.get("currency", "USD"), "")
        pos_rows += (
            f"<tr>"
            f"<td><strong>{pos['symbol']}</strong></td>"
            f"<td class='dim'>{pos['broker']}</td>"
            f"<td class='num'>{pos['shares']:,}</td>"
            f"<td class='num'>{s}{pos['avg_cost']:,.4f}</td>"
            f"<td class='num'>{s}{pos['current_price']:,.4f}</td>"
            f"<td class='num'>{s}{pos['market_val']:,.2f}</td>"
            f"<td class='num {cls}'>"
            f"<div style='display:flex;align-items:center;gap:6px'>"
            f"<span>{s}{pos['pnl']:+,.2f}</span>"
            f"<div style='width:{bar_w:.0f}px;height:6px;background:{bar_col};border-radius:3px'></div>"
            f"</div></td>"
            f"<td class='num {cls}'>{pct:+.1f}%</td>"
            f"</tr>\n"
        )

    # Zerodha warning banner — enhanced with estimated total if DB has last known data
    z_status_val = statuses.get("Zerodha", "")
    zerodha_warn = ""
    if not z_status_val.startswith("Live"):
        FX_Z = get_fx_to_usd()
        live_usd_z = sum((pos.get("market_val", 0) or 0) * FX_Z.get(pos.get("currency", "USD"), 1.0)
                         for pos in all_positions)
        live_aud_z = live_usd_z / FX_Z["AUD"]
        last_india_aud, last_india_date = _load_last_india_total()
        if last_india_aud:
            estimated_total = live_aud_z + last_india_aud
            zerodha_warn = (
                '<div style="background:#3d2e00;border:1px solid #f0b84b;border-radius:8px;'
                'padding:14px 16px;margin-bottom:16px">'
                '<strong>&#9888; COVERAGE WARNING — Zerodha offline</strong><br>'
                '<table style="margin-top:8px;font-size:13px;border:none">'
                f'<tr><td style="padding:2px 8px">Live data today:</td><td><strong>A${live_aud_z:,.0f}</strong></td></tr>'
                f'<tr><td style="padding:2px 8px">Zerodha (last known {last_india_date}):</td><td style="color:#f0b84b">A${last_india_aud:,.0f} <em>(stale)</em></td></tr>'
                f'<tr><td style="padding:2px 8px;border-top:1px solid #f0b84b"><strong>Estimated total:</strong></td>'
                f'<td style="border-top:1px solid #f0b84b"><strong>A${estimated_total:,.0f}</strong> &#8203;<em style="color:#94a3b8">(India data is stale — not counted in live total)</em></td></tr>'
                '</table>'
                '<div style="margin-top:8px;font-size:12px;color:#d4a017">Set <code>ZERODHA_ACCESS_TOKEN</code> or run Zerodha Login to include live India holdings.</div>'
                '</div>'
            )
        else:
            zerodha_warn = (
                '<div style="background:#3d2e00;border:1px solid #f0b84b;border-radius:8px;'
                'padding:12px 16px;margin-bottom:16px">'
                '&#9888; <strong>Zerodha data unavailable</strong> — '
                'Indian equity positions (NSE/BSE) are excluded from this report. '
                'Set <code>ZERODHA_ACCESS_TOKEN</code> or run the Zerodha login to include holdings.</div>'
            )

    total_pos = len(all_positions)
    if not pos_rows:
        pos_rows = "<tr><td colspan='8' class='dim' style='text-align:center'>No positions available</td></tr>"

    # General missing-broker banner (excludes Zerodha which has its own detailed banner)
    _live_statuses = ("Live", "Cached", "PDF", "Flex")
    _offline_brokers = [
        b for b, s in statuses.items()
        if b != "Zerodha" and not any(s.startswith(pref) for pref in _live_statuses)
    ]
    missing_warn = ""
    if _offline_brokers:
        _broker_list = ", ".join(f"<strong>{b}</strong>" for b in _offline_brokers)
        missing_warn = (
            '<div style="background:#2d1a1a;border:1px solid #e74c3c;border-radius:8px;'
            'padding:12px 16px;margin-bottom:16px">'
            f'&#9888; <strong>PARTIAL DATA</strong> — '
            f'The following broker(s) are offline: {_broker_list}. '
            f'Portfolio totals below <em>exclude</em> positions from these brokers '
            f'and may understate your true portfolio value.</div>'
        )

    # Realised P&L section HTML
    realised_section_html = _generate_realised_pnl_html(realised_data) if realised_data else ""
    # FD vs Portfolio benchmark — always shown (uses all_positions cost basis)
    realised_section_html += _build_fd_comparison_section(all_positions)

    # Portfolio risk section
    if _RISK_SCORER_AVAILABLE:
        _risk_result = _RiskScorer().score(all_positions)
        risk_section_html = _render_risk_html(_risk_result)
    else:
        risk_section_html = ""

    tmpl = _jinja_env.get_template("portfolio_dashboard.html")
    return tmpl.render(
        now=now,
        total_pos=total_pos,
        zerodha_warn=zerodha_warn,
        missing_warn=missing_warn,
        profit_count=profit_count,
        loss_count=loss_count,
        flat_count=flat_count,
        overall_pnl_pct=f"{overall_pnl_pct:+.1f}%",
        ovr_cls=ovr_cls,
        broker_rows=broker_rows,
        cov_rows=cov_rows,
        val_rows=val_rows,
        realised_section_html=realised_section_html,
        risk_section_html=risk_section_html,
        pos_rows=pos_rows,
        system_footer=system_footer_html(),
    )


def _load_last_india_total() -> tuple[float | None, str | None]:
    """Return (last_aud_from_zerodha, date) from DB if available, else (None, None)."""
    if not DB_PATH.exists():
        return None, None
    try:
        con = sqlite3.connect(str(DB_PATH))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        row = con.execute(
            "SELECT broker_breakdown, date FROM portfolio_snapshots WHERE zerodha_ok=1 ORDER BY date DESC LIMIT 1"
        ).fetchone()
        con.close()
        if not row:
            return None, None
        breakdown = json.loads(row[0] or "{}")
        # broker_breakdown stores USD values; find Zerodha entry
        zerodha_usd = 0.0
        for key, val in breakdown.items():
            if "zerodha" in key.lower():
                zerodha_usd += float(val or 0)
        return zerodha_usd / 0.65, row[1]
    except Exception:
        return None, None




def _save_portfolio_snapshot(
    all_positions: list[dict],
    statuses: dict[str, str],
    aud_total: float,
    usd_total: float,
) -> None:
    """Persist today's portfolio snapshot to SQLite for historical analytics."""
    REPORTS.mkdir(exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            date             TEXT PRIMARY KEY,
            generated_at     TEXT,
            aud_total        REAL,
            usd_total        REAL,
            positions_count  INTEGER,
            currency_breakdown TEXT,
            broker_breakdown   TEXT,
            brokers_online     TEXT,
            brokers_offline    TEXT,
            zerodha_ok         INTEGER DEFAULT 0,
            notes              TEXT
        )
    """)

    # currency breakdown: {USD: val, AUD: val, …} — live FX rates (see fx_rates.py)
    FX = get_fx_to_usd()
    currency_totals: dict[str, float] = {}
    broker_totals: dict[str, float] = {}
    for pos in all_positions:
        ccy  = pos.get("currency", "USD")
        mval = pos.get("market_val", 0) or 0
        broker = pos.get("broker", "Unknown")
        currency_totals[ccy] = currency_totals.get(ccy, 0.0) + mval
        rate = FX.get(ccy, 1.0)
        broker_totals[broker] = broker_totals.get(broker, 0.0) + mval * rate

    online  = [b for b, s in statuses.items() if s.startswith("Live") or "position" in s.lower() or "cached" in s.lower()]
    offline = [b for b, s in statuses.items() if b not in online]
    zerodha_ok = 1 if statuses.get("Zerodha", "").startswith("Live") else 0

    con.execute("""
        INSERT OR REPLACE INTO portfolio_snapshots
            (date, generated_at, aud_total, usd_total, positions_count,
             currency_breakdown, broker_breakdown, brokers_online, brokers_offline,
             zerodha_ok, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().strftime("%Y-%m-%d"),
        datetime.now().isoformat(timespec="seconds"),
        round(aud_total, 2),
        round(usd_total, 2),
        len(all_positions),
        json.dumps({k: round(v, 2) for k, v in currency_totals.items()}),
        json.dumps({k: round(v, 2) for k, v in broker_totals.items()}),
        json.dumps(online),
        json.dumps(offline),
        zerodha_ok,
        None,
    ))
    con.commit()
    con.close()
    print(f"{GREEN}  Portfolio snapshot saved to DB:{RESET} {DB_PATH}")


# ═══════════════════════════════════════════════════════════════════════════════
# Archive
# ═══════════════════════════════════════════════════════════════════════════════

def _archive_dashboard_report() -> None:
    """Copy the just-written dashboard to reports/archive/YYYY-MM-DD/global_portfolio_HHMMSS.html."""
    if not OUTPUT.exists():
        return
    try:
        ensure_paths(REPORTS)
        from report_db import archive_report as _arc
        dest = _arc(OUTPUT, "global_portfolio")
        if dest:
            print(f"{GREEN}  Archived report:{RESET} {dest}")
    except Exception:
        # Fallback: direct copy so archiving is never silently skipped
        today     = datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.now().strftime("%H%M%S")
        dest_dir  = ARCHIVE_DIR / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"global_portfolio_{timestamp}.html"
        shutil.copy2(str(OUTPUT), str(dest))
        print(f"{GREEN}  Archived report:{RESET} {dest}")
    _rebuild_archive_index()


def _rebuild_archive_index() -> None:
    """Regenerate reports/archive/index.html — a browsable table of all archived reports."""
    rows: list[tuple[str, str, str]] = []   # (date, time, relative_path)
    for date_dir in sorted(ARCHIVE_DIR.glob("????-??-??")):
        for f in sorted(date_dir.glob("global_portfolio_*.html")):
            ts = f.stem.replace("global_portfolio_", "")
            time_str = f"{ts[:2]}:{ts[2:4]}:{ts[4:]}" if len(ts) == 6 else ts
            rel = f.relative_to(ARCHIVE_DIR)
            rows.append((date_dir.name, time_str, str(rel).replace("\\", "/")))

    rows_html = "\n".join(
        f'    <tr><td>{d}</td><td>{t}</td>'
        f'<td><a href="{p}">global_portfolio_{t.replace(":", "")}.html</a></td></tr>'
        for d, t, p in reversed(rows)
    )
    index_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Portfolio Archive</title>
<style>
  body{{font-family:sans-serif;background:#0d1117;color:#c9d1d9;padding:2rem}}
  h1{{color:#58a6ff}}
  table{{border-collapse:collapse;width:100%;max-width:800px}}
  th,td{{padding:.5rem 1rem;text-align:left;border-bottom:1px solid #30363d}}
  th{{background:#161b22;color:#8b949e}}
  a{{color:#58a6ff;text-decoration:none}}
  a:hover{{text-decoration:underline}}
</style>
</head><body>
<h1>Global Portfolio Archive</h1>
<p style="color:#8b949e">{len(rows)} archived reports</p>
<table>
  <tr><th scope='col'>Date</th><th scope='col'>Time</th><th scope='col'>Report</th></tr>
{rows_html}
</table>
</body></html>"""
    idx = ARCHIVE_DIR / "index.html"
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    idx.write_text(index_html, encoding="utf-8")


def _preflight_check_all_brokers(no_ibkr: bool = False) -> tuple[bool, list[str]]:
    """
    ALL-OR-NONE pre-flight: every broker must have a reachable data source.
    Returns (all_ok, list[str] of human-readable failure messages).
    Golden Rule #18: if ANY broker is down, the dashboard MUST abort.
    """
    failed: list[str] = []

    # 1. Moomoo-AU — must be live (OpenD on port 11111)
    if not _is_port_open(11111):
        failed.append("Moomoo-AU  — OpenD not running on port 11111 "
                      "(start C:\\moomoo.opend\\au\\OpenD.exe)")

    # 2. Moomoo-SG — must be live (OpenD on port 11112)
    if not _is_port_open(11112):
        failed.append("Moomoo-SG  — OpenD not running on port 11112 "
                      "(start C:\\moomoo.opend\\sg\\OpenD.exe)")

    # 3. IBKR — TWS live OR Flex cache XML present OR annual PDFs exist
    #    (skip check when --no-ibkr explicitly passed by caller)
    if not no_ibkr:
        _ibkr_ok = (
            _is_port_open(7496)                              # TWS live
            or _IBKR_FLEX_CACHE.exists()                     # Flex XML cache
            or bool(list(_IBKR_STMTS_DIR.glob("*.pdf")))    # Annual statements
        )
        if not _ibkr_ok:
            failed.append("IBKR       — no data source: TWS not on port 7496, "
                          "no Flex cache (_last_flex.xml), no PDFs in ibkr-statement/")

    # 4. Webull AU — official OpenAPI credentials OR PDF statements must exist
    _wb_api_ok = _webull_api_configured()
    _wb_pdf_ok = bool(list(_WEBULL_STMTS_DIR.glob("*.pdf")))
    if not (_wb_api_ok or _wb_pdf_ok):
        failed.append("Webull AU  — no API credentials (WEBULL_AU_APP_KEY/APP_SECRET) "
                      "and no PDF statements found in webull-statement/")

    # 5. Zerodha — valid token cached for today; if expired/missing, attempt re-auth inline
    import datetime as _dt
    import time as _pf_time
    _z_needs_auth = True
    if _ZERODHA_TOKEN.exists():
        try:
            # Use auth_time-based expiry (same logic as zerodha_auth.load_cache) so
            # cross-midnight tokens that are still within Zerodha's 24h window are accepted.
            _zdata = json.loads(_ZERODHA_TOKEN.read_text(encoding="utf-8"))
            _auth_ts = _zdata.get("auth_time")
            if _auth_ts:
                _age_h = (_pf_time.time() - float(_auth_ts)) / 3600.0
                if _age_h < 24.75:
                    _z_needs_auth = False   # token is within valid window
            elif _zdata.get("date", "") == _dt.date.today().isoformat():
                _z_needs_auth = False   # fallback: date-only check for old cache format
        except Exception:
            pass  # unreadable cache — will attempt auth below

    if _z_needs_auth:
        # Token expired or absent — attempt Zerodha re-authentication inline
        # (opens browser URL or uses TOTP auto-login via zerodha_auth._resolve_token)
        try:
            _z_dir = str(Path(__file__).parent / "india-brokers")
            if _z_dir not in sys.path:
                sys.path.insert(0, _z_dir)
            from zerodha_auth import _resolve_token as _z_resolve  # type: ignore
            print("\n  [Zerodha] Token expired — launching Zerodha login…")
            _z_token = _z_resolve(silent=False)
            if not _z_token:
                failed.append("Zerodha    — login cancelled or failed (run option [6] Zerodha Login first)")
        except Exception as _z_exc:
            failed.append(f"Zerodha    — re-auth failed: {_z_exc}")

    # 6. AngelOne — required env vars must be set
    _ao_keys = ["ANGELONE_API_KEY", "ANGELONE_CLIENT_CODE",
                "ANGELONE_PIN", "ANGELONE_TOTP_SECRET"]
    _ao_missing = [k for k in _ao_keys if not os.environ.get(k)]
    if _ao_missing:
        failed.append(f"AngelOne   — env vars not set: {', '.join(_ao_missing)}")

    return len(failed) == 0, failed


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main(no_open: bool = False, no_ibkr: bool = False, allow_partial: bool = False,
         force_refresh: bool = False) -> None:
    print(f"\n{BOLD}{CYAN}┌─────────────────────────────────────────────────┐")
    print(f"│       GLOBAL PORTFOLIO DASHBOARD                │")
    print(f"│       Aggregating all brokers…                  │")
    print(f"└─────────────────────────────────────────────────┘{RESET}\n")

    all_positions: list[dict] = []
    statuses: dict[str, str] = {}
    _from_cache = False

    # ── ALL-OR-NONE pre-flight: every broker must be reachable (Golden Rule #18) ──
    _all_brokers_ok, _broker_failures = _preflight_check_all_brokers(no_ibkr=no_ibkr)
    if not _all_brokers_ok:
        print(f"\n{RED}{BOLD}╔{'\u2550'*62}╗{RESET}")
        print(f"{RED}{BOLD}║  ⛔  DASHBOARD ABORTED — BROKER(S) UNAVAILABLE{' '*18}║{RESET}")
        print(f"{RED}{BOLD}╚{'\u2550'*62}╝{RESET}")
        print(f"\n{BOLD}  ALL 6 brokers must be reachable. Failed:{RESET}\n")
        for _bf in _broker_failures:
            print(f"  {RED}✗{RESET}  {_bf}")
        print(f"\n{YELLOW}  Fix the above and re-run.{RESET}")
        print(f"{YELLOW}  No report will be generated with partial data.{RESET}")
        print(f"{YELLOW}  BLACK OR WHITE — ALL OR NONE.{RESET}\n")
        sys.exit(1)

    # ── Try positions cache (skip if force_refresh requested) ─────────────────
    if not force_refresh:
        cached_pos, cached_st = _load_positions_cache()
        if cached_pos is not None and cached_st is not None:
            # All-or-none: discard cache if ANY broker was offline at creation time
            # (e.g. Moomoo-AU was down when cache was built — data is incomplete)
            _offline_markers = ("not running", "timeout", "error",
                                "unavailable", "offline", "failed")
            _cache_has_offline = any(
                any(m in (cached_st.get(b, "")).lower() for m in _offline_markers)
                for b in _KNOWN_BROKERS
            )
            if _cache_has_offline:
                _offline_brokers = [
                    b for b in _KNOWN_BROKERS
                    if any(m in cached_st.get(b, "").lower() for m in _offline_markers)
                ]
                print(f"{YELLOW}  Cache was created with broker(s) offline "
                      f"({', '.join(_offline_brokers)}) — forcing fresh fetch.{RESET}")
            else:
                all_positions = cached_pos
                statuses = cached_st
                _from_cache = True
                print(f"{DIM}  Using cached positions (< {_POSITIONS_CACHE_TTL // 60} min old).{RESET}")

    if not _from_cache:
        # ── Broker fetch: Moomoo SEQUENTIAL, others PARALLEL ─────────────────
        # Moomoo SDK is NOT thread-safe: running AU + SG in concurrent threads
        # corrupts shared protobuf/socket state → SG always returns
        # "OpenD not running" even when OpenD IS live.
        # FIX: fetch AU then SG sequentially (takes ~1-2s total, negligible).
        # IBKR, Webull, India still run in parallel (they use different SDKs).
        print(f"{DIM}  Fetching Moomoo AU → SG sequentially, others in parallel…{RESET}")
        _t_fetch_start = time.perf_counter()

        # Step 1: Moomoo AU then SG — sequential, no threads
        print(f"{DIM}    Moomoo AU…{RESET}", end="", flush=True)
        au_pos, au_status = _fetch_moomoo("AU", 11111)
        print(f" {au_status}")
        print(f"{DIM}    Moomoo SG…{RESET}", end="", flush=True)
        sg_pos, sg_status = _fetch_moomoo("SG", 11112)
        print(f" {sg_status}")

        # Step 2: IBKR + Webull + India in parallel
        # NOTE: Do NOT use `with ThreadPoolExecutor` here — __exit__ calls
        # shutdown(wait=True) which blocks indefinitely if any broker thread hangs
        # (Future.cancel() cannot stop a thread already in progress).
        _pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="broker")
        _fut_ibkr = _pool.submit(_fetch_ibkr, no_ibkr)
        _fut_wb   = _pool.submit(_fetch_webull)
        _fut_ind  = _pool.submit(_fetch_india)

        _all_futs = [_fut_ibkr, _fut_wb, _fut_ind]
        _done, _not_done = _futures_wait(_all_futs, timeout=90)
        for _f in _not_done:
            _f.cancel()
        # shutdown(wait=False) — never block on hung broker threads past the 90s cap.
        # cancel_futures=True cancels any not-yet-started tasks (Python 3.9+).
        _pool.shutdown(wait=False, cancel_futures=True)

        _fetch_elapsed = time.perf_counter() - _t_fetch_start

        # ── Collect results (failures return empty lists, never raise) ─────────
        try:
            ib_pos, ib_status = _fut_ibkr.result(timeout=0)
        except Exception as _e:
            ib_pos, ib_status = [], f"Timeout/Error: {_e}"

        try:
            wb_pos, wb_status = _fut_wb.result(timeout=0)
        except Exception as _e:
            wb_pos, wb_status = [], f"Timeout/Error: {_e}"

        try:
            z_pos, a_pos, z_status, a_status = _fut_ind.result(timeout=0)
        except Exception as _e:
            z_pos, a_pos, z_status, a_status = [], [], f"Timeout/Error: {_e}", "Not attempted"

        # ── Assemble positions ─────────────────────────────────────────────────
        all_positions.extend(au_pos)
        all_positions.extend(sg_pos)
        all_positions.extend(ib_pos)
        all_positions.extend(wb_pos)
        all_positions.extend(z_pos)
        all_positions.extend(a_pos)

        statuses["Moomoo-AU"] = au_status
        statuses["Moomoo-SG"] = sg_status
        statuses["IBKR"]      = ib_status
        statuses["Webull AU"] = wb_status
        statuses["Zerodha"]   = z_status
        statuses["AngelOne"]  = a_status

        print(f"    Moomoo AU:  {au_status}")
        print(f"    Moomoo SG:  {sg_status}")
        print(f"    IBKR:       {ib_status}")
        print(f"    Webull AU:  {wb_status}")
        print(f"    Zerodha:    {z_status}")
        print(f"    AngelOne:   {a_status}")
        print(f"{GREEN}  ✓ Parallel fetch done in {_fetch_elapsed:.1f}s{RESET}")

        # ── Validate positions at broker boundary (Pydantic v2) ────────────
        pre_count = len(all_positions)
        _pre_symbols = {(p.get("symbol","?"), p.get("broker","?")) for p in all_positions}
        all_positions = validate_positions(all_positions, broker="all", logger=_log)
        if len(all_positions) < pre_count:
            dropped = pre_count - len(all_positions)
            _post_symbols = {(p.get("symbol","?"), p.get("broker","?")) for p in all_positions}
            _gone = _pre_symbols - _post_symbols
            _gone_str = ", ".join(f"{s}({b})" for s, b in sorted(_gone)) or "unknown"
            _log.warning("positions_validation_dropped",
                         dropped=dropped, total=pre_count, symbols=_gone_str)
            print(f"{YELLOW}  ⚠ {dropped}/{pre_count} positions dropped by schema validation: {_gone_str}{RESET}")

        # Persist for next call
        _save_positions_cache(all_positions, statuses)

    else:
        # Reconstruct broker-local names from cache for hard-stop logic
        z_status = statuses.get("Zerodha",  "cache")
        a_status = statuses.get("AngelOne", "cache")

    # ── HARD STOP if Zerodha is missing ───────────────────────────────────────
    if not z_status.startswith("Live") and not allow_partial:
        print(f"\n{RED}{BOLD}╔═══════════════════════════════════════════════════════╗{RESET}")
        print(f"{RED}{BOLD}║  ✗  HARD STOP — Zerodha data is MISSING               ║{RESET}")
        print(f"{RED}{BOLD}╚═══════════════════════════════════════════════════════╝{RESET}")
        print(f"{RED}  Status: {z_status}{RESET}")
        print(f"{YELLOW}  India equity (~₹2-3L / ~A$40K) would be excluded.{RESET}")
        print(f"{YELLOW}  The report would be INCOMPLETE and MISLEADING.{RESET}")
        print(f"\n{BOLD}  Options:{RESET}")
        print(f"  1. Fix auth: run option [6] Zerodha Login, then retry")
        print(f"  2. Override (NOT recommended): add --allow-partial flag")
        print(f"\n{RED}  Report NOT generated. Exiting.{RESET}\n")
        sys.exit(1)

    # ── If partial mode with Zerodha missing, show estimated total from last snapshot ──
    _partial_india_aud: float | None = None
    if allow_partial and not z_status.startswith("Live"):
        last_india_aud, last_india_date = _load_last_india_total()
        _partial_india_aud = last_india_aud
        print(f"\n{YELLOW}{BOLD}⚠  PARTIAL MODE — Zerodha missing{RESET}")
        if last_india_aud:
            FX_RATES_P = get_fx_to_usd()
            live_usd = sum((pos.get("market_val", 0) or 0) * FX_RATES_P.get(pos.get("currency", "USD"), 1.0)
                           for pos in all_positions)
            live_aud = live_usd / FX_RATES_P["AUD"]
            estimated_aud = live_aud + last_india_aud
            print(f"{YELLOW}  Live data today:           A${live_aud:>,.0f}{RESET}")
            print(f"{YELLOW}  Zerodha (last known {last_india_date}): A${last_india_aud:>,.0f}{RESET}")
            print(f"{YELLOW}  ─────────────────────────────────────────{RESET}")
            print(f"{YELLOW}  ESTIMATED TOTAL:           A${estimated_aud:>,.0f}  (⚠ India data is stale){RESET}\n")

    # ── CLI summary ───────────────────────────────────────────────────────────
    _print_portfolio_snapshot(all_positions, statuses)

    # ── Realised P&L history ─────────────────────────────────────────────────
    print(f"{DIM}  Loading realised P&L history from statements…{RESET}")
    realised_data = _fetch_realised_pnl()
    # Attach live India positions so the P&L section can display a current-value card.
    # When using cache, reconstruct india_positions from the cached all_positions.
    if _from_cache:
        india_brokers_ = {"Zerodha", "AngelOne"}
        realised_data["india_positions"] = [
            p for p in all_positions if p.get("broker") in india_brokers_
        ]
    else:
        realised_data["india_positions"] = z_pos + a_pos
    for src, st in realised_data.get("status", {}).items():
        print(f"    {src}: {st}")

    # ── HTML report ───────────────────────────────────────────────────────────
    html = generate_html(all_positions, statuses, realised_data)
    REPORTS.mkdir(exist_ok=True)
    try:
        from report_db import archive_before_write as _abw_global_dashboard
        _abw_global_dashboard(OUTPUT)
    except Exception:
        pass
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"{GREEN}  HTML report written:{RESET} {OUTPUT}")

    # ── Save to portfolio history DB ──────────────────────────────────────────
    FX_RATES = get_fx_to_usd()
    usd_total = sum(
        (pos.get("market_val", 0) or 0) * FX_RATES.get(pos.get("currency", "USD"), 1.0)
        for pos in all_positions
    )
    aud_total = usd_total / FX_RATES["AUD"]
    _save_portfolio_snapshot(all_positions, statuses, aud_total, usd_total)

    # ── Archive the report ────────────────────────────────────────────────────
    _archive_dashboard_report()

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        ensure_paths(REPORTS)
        from report_db import save_global_portfolio_snapshot
        save_global_portfolio_snapshot(all_positions, statuses, aud_total, usd_total)
    except Exception as _e:
        print(f"{DIM}  [report_db] global_portfolio snapshot skipped: {_e}{RESET}")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Global Portfolio Dashboard")
    parser.add_argument("--no-open",       action="store_true", help="(deprecated — browser no longer auto-opens)")
    parser.add_argument("--no-ibkr",       action="store_true", help="Skip IBKR fetch")
    parser.add_argument("--allow-partial", action="store_true", help="Allow report even if Zerodha is unavailable (NOT recommended)")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass positions cache and fetch live data")
    parser.add_argument("--morning",       action="store_true",
                        help="Non-interactive morning mode: force-refresh, no-open. Zero prompts.")
    args = parser.parse_args()
    # --morning overrides
    if args.morning:
        args.force_refresh = True
        args.no_open = True
    main(no_open=args.no_open, no_ibkr=args.no_ibkr, allow_partial=args.allow_partial,
         force_refresh=args.force_refresh)
