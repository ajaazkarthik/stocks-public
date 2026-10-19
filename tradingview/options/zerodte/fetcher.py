"""
ZeroDTE Market Data Fetcher
============================
All external API calls for the ZeroDTE scanner.

Sources:
  1. TradingView stock scanner (America) — top movers by relative volume
  2. TradingView scan2 batch API — DTE pre-filter (which symbols have 0-2DTE options)
  3. yfinance — OHLCV, RSI, volume history, option chains
  4. TradingView options-charting API — IV term structure (in-time-iv)
  5. TradingView options-charting API — VOI (volume open interest lens)
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
import yfinance as yf
import pandas as pd

from .config import ZeroDTEConfig, DEFAULT_CONFIG
from .models import MoverCandidate, IVStructure, VOIData

_log = logging.getLogger("zerodte.fetcher")

# ── TV scan2 endpoint (for DTE pre-filter) ────────────────────────────────────
_TV_SCAN2_URL = "https://scanner.tradingview.com/options/scan2?label-product=options-builder"
_SCAN2_BATCH_SIZE = 200   # symbols per scan2 request (keep below TV's hard limit)


# ── Cookie loading ────────────────────────────────────────────────────────────

def _load_tv_cookies(cookie_path: Path) -> dict:
    """Load TradingView session cookies from file.

    Supports three formats (same as TQS v2):
      1. Netscape cookie file: 7 tab-separated fields, col[5]=name, col[6]=value
      2. Multi-line key=value pairs (one cookie per line)
      3. Single-line browser cookie string: key=val; key2=val2; ...

    Returns empty dict if file missing, unreadable, or sessionid absent.
    """
    if not cookie_path.exists():
        return {}
    try:
        raw = cookie_path.read_text(encoding="utf-8", errors="replace").strip()
        cookies: dict[str, str] = {}

        def _ingest_pairs(text: str) -> None:
            """Parse semicolon-separated key=value cookie pairs into cookies."""
            for pair in text.split(";"):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                key, _, val = pair.partition("=")
                key = key.strip()
                # Skip JSON blobs and empty keys
                if not key or key.startswith("{"):
                    continue
                if key not in cookies:
                    cookies[key] = val.strip()

        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                # Netscape cookie format (e.g. from EditThisCookie / Cookie-Editor export)
                name, value = parts[5].strip(), parts[6].strip()
                if name and name not in cookies:
                    cookies[name] = value
            else:
                # Single-line browser string or multi-line key=value
                _ingest_pairs(line)

        if "sessionid" not in cookies:
            _log.warning(
                "Cookie file at %s is missing 'sessionid' — re-export cookies from your browser",
                cookie_path,
            )
            return {}
        _log.debug("TV cookie loaded (%d keys, has sessionid_sign=%s)", len(cookies), "sessionid_sign" in cookies)
        return cookies
    except Exception as exc:
        _log.warning("Could not load TV cookies from %s: %s", cookie_path, exc)
        return {}


def _make_tv_headers(cookies: dict) -> dict:
    """Build TV options-charting request headers."""
    sid = cookies.get("sessionid", "")
    ssign = cookies.get("sessionid_sign", "")
    return {
        "Cookie": f"sessionid={sid}; sessionid_sign={ssign}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }


# ── TV Stock scanner (universe) ───────────────────────────────────────────────

def fetch_tv_top_movers(config: ZeroDTEConfig = DEFAULT_CONFIG) -> list[str]:
    """
    Query TradingView America scanner for high relative-volume movers.
    Returns a list of ticker symbols (plain, no exchange prefix).

    Note: /america/scan is already US-scoped — no exchange filter needed.
    The 'exchange' field with 'in' and multiple values causes HTTP 400.
    """
    body = {
        "filter": [
            {"left": "close", "operation": "greater", "right": config.tv_scan_min_price},
            {"left": "market_cap_basic", "operation": "greater", "right": config.tv_scan_min_market_cap},
            {"left": "average_volume_10d_calc", "operation": "greater", "right": config.tv_scan_min_volume},
            {"left": "relative_volume_10d_calc", "operation": "greater", "right": 1.5},
        ],
        "columns": ["name", "close", "change", "volume", "market_cap_basic", "relative_volume_10d_calc", "RSI"],
        "range": [0, config.tv_scan_top_n],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
    }
    try:
        resp = requests.post(
            config.tv_stock_scan_url,
            json=body,
            timeout=config.http_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        tickers: list[str] = []
        for row in data.get("data", []):
            sym = row.get("s", "")          # "NASDAQ:NVDA" or "NYSE:AAPL"
            exchange = None
            if ":" in sym:
                exchange, sym = sym.split(":", 1)  # strip exchange prefix
            if sym and "/" not in sym:  # filter malformed symbols like "ORCL/PD" (preferred shares)
                if exchange:             # cache ONLY valid tickers (filter before cache)
                    _EXCHANGE_CACHE[sym.upper()] = exchange
                tickers.append(sym)
        _log.info("TV scanner returned %d top movers", len(tickers))
        return tickers
    except Exception as exc:
        _log.warning("TV stock scanner error: %s", exc)
        return []


# ── yfinance helpers ──────────────────────────────────────────────────────────

def _compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Compute RSI from a closing price series. Returns 50 on insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.rolling(window=period, min_periods=period).mean().iloc[-1]
    avg_loss = losses.rolling(window=period, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def fetch_stock_data(symbol: str, config: ZeroDTEConfig = DEFAULT_CONFIG) -> Optional[MoverCandidate]:
    """
    Fetch price momentum data for a single stock via yfinance.
    Returns None if data is unavailable or symbol is invalid.
    """
    if "/" in symbol:  # reject malformed symbols like "ORCL/PD" (preferred shares from TV scanner)
        _log.debug("Skipping malformed symbol: %s", symbol)
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="30d", interval="1d", auto_adjust=True, timeout=15)
        if hist.empty or len(hist) < 3:
            _log.debug("Insufficient history for %s", symbol)
            return None

        closes = hist["Close"]
        volumes = hist["Volume"]

        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        prev_day_change_pct = ((last_close - prev_close) / prev_close) * 100

        last_volume = float(volumes.iloc[-1])
        # Use the last 20 days excluding today for the average
        vol_avg_20d = float(volumes.iloc[:-1].tail(20).mean()) if len(volumes) > 1 else 0.0

        rsi = _compute_rsi(closes)

        # Sector / industry
        info = {}
        try:
            info = ticker.fast_info
        except Exception:
            pass

        sector = getattr(info, "sector", "") if hasattr(info, "sector") else ""
        industry = getattr(info, "industry", "") if hasattr(info, "industry") else ""

        time.sleep(config.yfinance_delay_sec)

        return MoverCandidate(
            symbol=symbol,
            price=last_close,
            prev_day_change_pct=prev_day_change_pct,
            volume=last_volume,
            volume_20d_avg=vol_avg_20d,
            rsi=rsi,
            sector=str(sector),
            industry=str(industry),
        )
    except Exception as exc:
        _log.warning("yfinance error for %s: %s", symbol, exc)
        return None


# ── TV IV term structure ──────────────────────────────────────────────────────

def _ticker_to_tv_symbol(ticker: str) -> str:
    """Convert plain ticker to TV format EXCHANGE:TICKER.

    Priority:
    1. _EXCHANGE_CACHE populated by fetch_tv_top_movers (TV scanner knows the exchange)
    2. Hardcoded NASDAQ set for fixed_watchlist tickers not in the scanner
    3. Fallback to NYSE
    """
    key = ticker.upper()
    if key in _EXCHANGE_CACHE:
        return f"{_EXCHANGE_CACHE[key]}:{key}"
    nasdaq_listed = {
        "NVDA", "MSFT", "AAPL", "META", "GOOGL", "AMZN", "TSLA", "AMD",
        "PLTR", "ARM", "SMCI", "MRVL", "CRWD", "AVGO", "NFLX", "CRM",
        "SNOW", "PANW", "COIN", "HOOD", "RBLX", "DKNG", "MU", "INTC",
        "QCOM", "SHOP", "MELI", "SE", "BIDU", "NIO", "SOFI", "DDOG",
        "NET", "ZS", "OKTA", "ASAN", "CFLT", "MDB", "MNDY", "GTLB",
        "QQQ", "MSTR",
    }
    exchange = "NASDAQ" if ticker.upper() in nasdaq_listed else "NYSE"
    return f"{exchange}:{ticker.upper()}"


# Module-level cache: plain ticker → TV exchange prefix (populated by fetch_tv_top_movers)
# Fixes the wrong-exchange-prefix → 404 bug: TV scanner returns "NASDAQ:REPL" but
# _ticker_to_tv_symbol previously defaulted unknown tickers to NYSE.
_EXCHANGE_CACHE: dict[str, str] = {}


_TV_IV_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/options/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def _safe_float(v: object) -> Optional[float]:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fetch_iv_structure(
    symbol: str,
    headers: dict,  # TV auth headers (may include Cookie for authenticated endpoints)
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> IVStructure:
    """
    Fetch IV term structure from TradingView options-charting API.

    URL format (confirmed from TQS v2 probe 2026-05):
      GET /v1/in-time-iv/{encoded_symbol}?label-product=product_page_overview
      where encoded_symbol = quote("NASDAQ:NVDA", safe="")  -> NASDAQ%3ANVDA

    Response structure:
      {
        "real-ivs":     [{"span": {"value": 0, "unit": "d"}, "value": 0.566}, ...],
        "standard-ivs": [{"span": {"value": 1, "unit": "w"}, "value": 0.39}, ...]
      }
    Note: span is a DICT {value, unit}, NOT a string like "1W".
    """
    tv_sym = _ticker_to_tv_symbol(symbol)
    # Encode the colon: NASDAQ:NVDA → NASDAQ%3ANVDA (path parameter)
    encoded_sym = quote(tv_sym, safe="")
    url = f"{config.tv_in_time_iv_base}/{encoded_sym}"
    params = {"label-product": "product_page_overview"}

    try:
        # Merge auth headers (includes Cookie if TV cookies are loaded)
        merged_iv_headers = {**_TV_IV_HEADERS, **headers}
        resp = requests.get(
            url,
            params=params,
            headers=merged_iv_headers,
            timeout=config.http_timeout,
        )
        if resp.status_code != 200:
            _log.debug("in-time-iv HTTP %d for %s", resp.status_code, tv_sym)
            time.sleep(config.tv_api_delay_sec)
            return IVStructure(symbol=symbol, available=False)

        data = resp.json()

        # Parse standard-ivs: span is a dict {"value": 1, "unit": "w"} → key "1w"
        span_map: dict[str, float] = {}
        for item in data.get("standard-ivs", []):
            span_raw = item.get("span", "")
            val = _safe_float(item.get("value"))
            if isinstance(span_raw, dict):
                key = f"{span_raw.get('value', '')}{span_raw.get('unit', '')}"  # e.g. "1w", "1m"
            else:
                key = str(span_raw).lower()
            if key and val is not None:
                span_map[key] = val

        # Current ATM IV from real-ivs[0]
        real_ivs = data.get("real-ivs", [])
        iv_1d = _safe_float(real_ivs[0].get("value")) if real_ivs else None

        iv_1w = span_map.get("1w")
        iv_1m = span_map.get("1m")
        iv_3m = span_map.get("3m")
        available = bool(iv_1w or iv_1m)

        time.sleep(config.tv_api_delay_sec)
        return IVStructure(
            symbol=symbol,
            iv_1d=iv_1d or 0.0,
            iv_1w=iv_1w or 0.0,
            iv_1m=iv_1m or 0.0,
            iv_3m=iv_3m or 0.0,
            available=available,
        )
    except Exception as exc:
        _log.debug("IV structure error for %s: %s", symbol, exc)
        time.sleep(config.tv_api_delay_sec)
        return IVStructure(symbol=symbol, available=False)


# ── TV VOI Lens ────────────────────────────────────────────────────────────────

def fetch_voi_data(
    symbol: str,
    headers: dict,  # TV auth headers (may include Cookie for authenticated endpoints)
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> VOIData:
    """
    Fetch VOI (volume by strike) from TradingView options-charting API.

    URL format (confirmed from TQS v2 iv_structure.py probe 2026-05):
      GET /v1/voi-lens/{symbol}?label-product=product_page_volume_tab
      where symbol colon is NOT encoded: NASDAQ:NVDA  (quote safe=":")

    Response structure:
      {"items": [{"exp": 20260522, "strikes": [{"s": 220.0, "c": {"v": 5}, "p": {"v": 2}}, ...]}, ...]}

    Aggregates call/put volume across all strikes and expiries to derive
    the dominant directional bias.
    """
    tv_sym = _ticker_to_tv_symbol(symbol)
    # Colon NOT encoded for voi-lens (confirmed: NASDAQ:NVDA works, NASDAQ%3ANVDA 404s)
    encoded_sym = quote(tv_sym, safe=":")
    url = f"{config.tv_voi_lens_base}/{encoded_sym}"
    params = {"label-product": "product_page_volume_tab"}
    voi_headers = {
        "Accept": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/options/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    }

    try:
        resp = requests.get(
            url,
            params=params,
            headers=voi_headers,
            timeout=config.http_timeout,
        )
        if resp.status_code != 200:
            _log.debug("voi-lens HTTP %d for %s", resp.status_code, tv_sym)
            time.sleep(config.tv_api_delay_sec)
            return VOIData(symbol=symbol, available=False)

        data = resp.json()

        # Aggregate call and put volume across all expiries and strikes
        # Response: {"items": [{"exp": YYYYMMDD, "strikes": [{"s": price, "c": {"v": N}, "p": {"v": N}}, ...]}, ...]}
        total_call_vol = 0.0
        total_put_vol = 0.0
        for expiry_item in data.get("items", []):
            for strike_entry in expiry_item.get("strikes", []):
                call_vol = _safe_float((strike_entry.get("c") or {}).get("v")) or 0.0
                put_vol = _safe_float((strike_entry.get("p") or {}).get("v")) or 0.0
                total_call_vol += call_vol
                total_put_vol += put_vol

        if total_call_vol > 0 and total_put_vol > 0:
            cp_ratio = total_call_vol / total_put_vol
            pc_ratio = total_put_vol / total_call_vol
            if cp_ratio >= 2.0:
                direction = "call"
            elif pc_ratio >= 2.0:
                direction = "put"
            else:
                direction = "neutral"
            time.sleep(config.tv_api_delay_sec)
            return VOIData(
                symbol=symbol,
                call_put_ratio=cp_ratio,
                put_call_ratio=pc_ratio,
                dominant_direction=direction,
                available=True,
            )

        time.sleep(config.tv_api_delay_sec)
        return VOIData(symbol=symbol, available=False)
    except Exception as exc:
        _log.debug("VOI fetch error for %s: %s", symbol, exc)
        time.sleep(config.tv_api_delay_sec)
        return VOIData(symbol=symbol, available=False)


# ── TV scan2 DTE pre-filter ────────────────────────────────────────────────────

def batch_filter_symbols_with_dte_options(
    symbols: list[str],
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> list[str]:
    """
    Use TV scan2 batch API to find which symbols have options expiring within
    0-N DTE (N = config.contract_max_dte, default 2).

    OLD approach: call yfinance.Ticker(sym).options for every symbol (serial, slow)
      333 symbols × 0.2 s delay = 66 s minimum, before any actual data is fetched.

    NEW approach: send up to 200 symbols per scan2 POST, filter by expiration date.
      Results in ~1-3 s for the full universe (182× speedup per live benchmark).

    Day-of-week effect:
      Sunday  : ~11 symbols (only ETFs have weekly expiries on a non-market day)
      Monday  : ~30-50 symbols (Monday/Tuesday expirations for weeklies)
      Thursday: ~100-200 symbols (Friday expirations — most liquid weekly cycle)

    Falls back to the full universe if TV cookies are unavailable — the pipeline
    continues to work correctly, just without the pre-filter optimisation.

    Args:
        symbols: plain tickers (no exchange prefix), e.g. ["NVDA", "MSFT", "SPY"]
        config:  ZeroDTEConfig — uses cookie_path, contract_max_dte, http_timeout

    Returns:
        Filtered list preserving input order — only symbols with ≥1 contract
        expiring in [today, today + contract_max_dte].
    """
    if not symbols:
        return []

    cookies = _load_tv_cookies(config.cookie_path)
    if not cookies:
        _log.warning(
            "DTE pre-filter: no TV cookies at %s — skipping optimisation, "
            "using full %d-symbol universe",
            config.cookie_path,
            len(symbols),
        )
        return list(symbols)

    # Date window: today → today + max_dte (inclusive), as YYYYMMDD integers
    today = date.today()
    cutoff = today + timedelta(days=config.contract_max_dte)
    today_int = int(today.strftime("%Y%m%d"))
    cutoff_int = int(cutoff.strftime("%Y%m%d"))

    # Authenticated session (same cookie format as TVOptionsFetcher)
    sess = requests.Session()
    sess.cookies.set("sessionid",      cookies.get("sessionid", ""),
                     domain=".tradingview.com")
    sess.cookies.set("sessionid_sign", cookies.get("sessionid_sign", ""),
                     domain=".tradingview.com")
    sess.headers.update({
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/options/",
        "Accept":       "application/json",
        "Content-Type": "text/plain;charset=UTF-8",
    })

    found: set[str] = set()   # uppercase plain symbols that have 0-N DTE contracts

    for batch_start in range(0, len(symbols), _SCAN2_BATCH_SIZE):
        batch_plain = symbols[batch_start : batch_start + _SCAN2_BATCH_SIZE]
        # _EXCHANGE_CACHE is populated by fetch_tv_top_movers (called first in orchestrator)
        batch_tv = [_ticker_to_tv_symbol(s) for s in batch_plain]

        payload: dict = {
            "columns": ["underlying_symbol", "expiration"],
            "ignore_unknown_fields": True,
            "index_filters": [{"name": "underlying_symbol", "values": batch_tv}],
            "filter2": {
                "operator": "and",
                "operands": [
                    {"expression": {"left": "type",       "operation": "equal",    "right": "option"}},
                    {"expression": {"left": "expiration", "operation": "in_range",
                                    "right": [today_int, cutoff_int]}},
                ],
            },
            "sort": {"sortBy": "underlying_symbol", "sortOrder": "asc"},
            "range": [0, 2000],
        }

        # Paginate until we've seen all contracts (edge case: heavy Thursday with many strikes)
        offset = 0
        PAGE_SIZE = 2000
        while True:
            payload["range"] = [offset, offset + PAGE_SIZE]
            try:
                resp = sess.post(
                    _TV_SCAN2_URL,
                    json=payload,
                    timeout=config.http_timeout,
                )
                resp.raise_for_status()
                data   = resp.json()
                fields = data.get("fields", [])
                rows   = data.get("symbols", [])
                total  = data.get("totalCount", 0)

                # Extract unique underlying symbols from this page
                if "underlying_symbol" in fields:
                    uidx = fields.index("underlying_symbol")
                    for sym_row in rows:
                        f = sym_row.get("f", [])
                        if uidx < len(f) and f[uidx]:
                            tv_sym = str(f[uidx])           # e.g. "NASDAQ:NVDA"
                            plain  = tv_sym.split(":", 1)[-1] if ":" in tv_sym else tv_sym
                            found.add(plain.upper())

                offset += PAGE_SIZE
                # Stop when we have all pages OR all batch symbols already identified
                if not rows or offset >= total:
                    break
                if all(s.upper() in found for s in batch_plain):
                    break   # early exit — every symbol in batch is accounted for

            except Exception as exc:
                _log.warning(
                    "DTE pre-filter: batch %d-%d scan2 error: %s "
                    "— including all %d symbols in this batch as fallback",
                    batch_start, batch_start + len(batch_plain), exc, len(batch_plain),
                )
                # Conservative fallback: treat the whole batch as having valid contracts
                found.update(s.upper() for s in batch_plain)
                break

        _log.debug(
            "DTE pre-filter: processed %d/%d symbols, %d with 0-%dDTE options so far",
            min(batch_start + _SCAN2_BATCH_SIZE, len(symbols)), len(symbols),
            len(found), config.contract_max_dte,
        )

    # Filter original list (preserving order) to only symbols with matching contracts
    result = [s for s in symbols if s.upper() in found]
    _log.info(
        "DTE pre-filter: %d → %d symbols have 0-%d DTE options "
        "(filtered out %d with no near-expiry contracts, window %d–%d)",
        len(symbols), len(result), config.contract_max_dte,
        len(symbols) - len(result), today_int, cutoff_int,
    )
    return result


# ── yfinance Options Chains ────────────────────────────────────────────────────

def get_expiry_dates_0_to_2dte(ticker_obj: yf.Ticker) -> list[str]:
    """
    Get all option expiry dates that are 0, 1, or 2 days away.
    Returns list of expiry date strings "YYYY-MM-DD".
    """
    today = date.today()
    cutoff = today + timedelta(days=2)
    try:
        exps = ticker_obj.options  # tuple of "YYYY-MM-DD" strings
        if not exps:
            return []
        valid = []
        for exp_str in exps:
            try:
                exp_date = date.fromisoformat(exp_str)
                if today <= exp_date <= cutoff:
                    valid.append(exp_str)
            except ValueError:
                continue
        return valid
    except Exception as exc:
        _log.debug("Could not get expiry dates: %s", exc)
        return []


def fetch_option_chain_for_expiry(
    symbol: str,
    expiry: str,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch call and put option chains for a given expiry from yfinance.
    Returns (calls_df, puts_df) — may be empty DataFrames.
    NOTE: yfinance returns bid=0.0/ask=0.0 after 16:00 ET for all contracts.
          Use fetch_contracts_via_scan2() for after-hours / EOD scanning instead.
    """
    try:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        return chain.calls, chain.puts
    except Exception as exc:
        _log.warning("Option chain fetch error for %s %s: %s", symbol, expiry, exc)
        return pd.DataFrame(), pd.DataFrame()


# ── TV scan2 Contract Fetcher & OI Merge ─────────────────────────────────────
# After-hours fix (2026-06-03): yfinance returns bid=0.0/ask=0.0 for ALL option
# contracts after 16:00 ET, causing the ZeroDTE scanner to reject every contract.
# TV scan2 preserves last-session bid/ask prices and is the primary data source.
# OI is always 0 in TV scan2 (hardcoded API limitation) so we fetch it separately
# from yfinance via fetch_oi_from_yfinance().


def fetch_contracts_via_scan2(
    symbol: str,
    direction: str,      # "call" or "put"
    expiry: str,         # "YYYY-MM-DD"
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """
    Fetch option contracts from TradingView scan2 for a given symbol/direction/expiry.

    TV scan2 preserves last-session bid/ask prices even after US market close (16:00 ET).
    yfinance returns bid=0.0/ask=0.0 for ALL contracts after hours, causing the ZeroDTE
    scanner to reject every contract — this function is the fix.

    Columns in returned DataFrame:
        contract_code (OPRA format, e.g. "OPRA:NVDA260606C150.0"), strike,
        bid, ask, iv, delta, gamma, theta, volume, theoPrice

    NOTE: open_interest is NOT included — TV scan2 always returns 0 (hardcoded API
          limitation). Use fetch_oi_from_yfinance() to get real OI separately.

    Returns empty DataFrame if TV cookies are unavailable or the request fails.
    Caller should fall back to fetch_option_chain_for_expiry() in that case.
    """
    cookies = _load_tv_cookies(config.cookie_path)
    if not cookies:
        _log.debug(
            "[scan2-contracts] No TV cookies — cannot use scan2 for %s %s %s",
            symbol, direction, expiry,
        )
        return pd.DataFrame()

    tv_sym   = _ticker_to_tv_symbol(symbol)
    exp_int  = int(expiry.replace("-", ""))   # "2026-06-05" → 20260605

    # Safe columns only — NEVER include open_interest, dte, moneyness, iv_rank,
    # iv_percentile, pcr, vanna, charm, etc. (any invalid column = HTTP 400)
    columns = [
        "code", "strike", "expiration", "option-type",
        "bid", "ask", "iv",
        "delta", "gamma", "theta",
        "volume", "theoPrice",
    ]

    payload: dict = {
        "columns": columns,
        "ignore_unknown_fields": True,
        "index_filters": [{"name": "underlying_symbol", "values": [tv_sym]}],
        "filter2": {
            "operator": "and",
            "operands": [
                {"expression": {"left": "type",        "operation": "equal", "right": "option"}},
                {"expression": {"left": "option-type", "operation": "equal", "right": direction}},
                {"expression": {"left": "expiration",  "operation": "equal", "right": exp_int}},
            ],
        },
        "sort": {"sortBy": "strike", "sortOrder": "asc"},
        "range": [0, 500],
    }

    sess = requests.Session()
    sess.cookies.set("sessionid",      cookies.get("sessionid", ""),      domain=".tradingview.com")
    sess.cookies.set("sessionid_sign", cookies.get("sessionid_sign", ""), domain=".tradingview.com")
    sess.headers.update({
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/options/",
        "Accept":       "application/json",
        "Content-Type": "text/plain;charset=UTF-8",
    })

    try:
        resp = sess.post(_TV_SCAN2_URL, json=payload, timeout=config.http_timeout)
        if resp.status_code != 200:
            _log.debug(
                "[scan2-contracts] HTTP %d for %s %s %s",
                resp.status_code, symbol, direction, expiry,
            )
            return pd.DataFrame()

        data   = resp.json()
        fields = data.get("fields", [])
        rows   = data.get("symbols", [])

        if not rows:
            _log.debug(
                "[scan2-contracts] 0 rows from TV scan2: %s %s %s",
                symbol, direction, expiry,
            )
            return pd.DataFrame()

        # Pre-compute OCC code parts once (symbol and direction are constant per call)
        _sym_bare = symbol.split(":")[-1]           # "NASDAQ:AAPL" → "AAPL"
        _yymmdd   = str(exp_int)[2:]                # 20260605 → "260605"
        _opt_char = "C" if direction == "call" else "P"

        records = []
        for sym_row in rows:
            f     = sym_row.get("f", [])
            row_d = dict(zip(fields, f))

            # TV scan2 returns code=None for all options — derive OCC format from fields
            _code_tv = str(row_d.get("code") or "")
            if not _code_tv:
                _stk_raw    = row_d.get("strike") or 0
                _strike_enc = f"{int(round(float(_stk_raw) * 1000)):08d}"
                _code_tv    = f"{_sym_bare}{_yymmdd}{_opt_char}{_strike_enc}"
                # e.g. "AAPL260605C00315000"

            records.append({
                "contract_code": _code_tv,
                "strike":    float(row_d.get("strike")    or 0),
                "bid":       float(row_d.get("bid")       or 0),
                "ask":       float(row_d.get("ask")       or 0),
                "iv":        float(row_d.get("iv")        or 0),   # decimal: 0.389 = 38.9%
                "delta":     float(row_d.get("delta")     or 0),
                "gamma":     float(row_d.get("gamma")     or 0),
                "theta":     float(row_d.get("theta")     or 0),
                "volume":    int(  row_d.get("volume")    or 0),
                "theoPrice": float(row_d.get("theoPrice") or 0),
            })

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        _log.debug(
            "[scan2-contracts] Fetched %d contracts via TV scan2: %s %s %s",
            len(df), symbol, direction, expiry,
        )
        return df

    except Exception as exc:
        _log.warning(
            "[scan2-contracts] Error fetching %s %s %s: %s",
            symbol, direction, expiry, exc,
        )
        return pd.DataFrame()


def fetch_oi_from_yfinance(
    symbol: str,
    expiry: str,     # "YYYY-MM-DD"
    direction: str,  # "call" or "put"
) -> dict:
    """
    Fetch open interest by strike from yfinance for a given symbol/expiry/direction.

    TV scan2 always returns open_interest=0 (hardcoded API limitation).
    yfinance provides real (delayed ~1 trading day) OI from the exchange.
    Use this alongside fetch_contracts_via_scan2() to supplement real OI values.

    Returns:
        dict mapping round(strike, 2) → open_interest (int).
        Empty dict on any error or if yfinance returns no OI data
        (e.g. after 16:00 ET yfinance returns all zeros).
    """
    try:
        ticker = yf.Ticker(symbol)
        chain  = ticker.option_chain(expiry)
        df = chain.calls if direction == "call" else chain.puts
        if df.empty:
            return {}
        result: dict = {}
        for _, row in df.iterrows():
            s      = row.get("strike")
            oi_raw = row.get("openInterest")
            if s is None or (isinstance(s, float) and pd.isna(s)):
                continue
            oi = 0 if (oi_raw is None or
                       (isinstance(oi_raw, float) and pd.isna(oi_raw))) else int(oi_raw or 0)
            result[round(float(s), 2)] = oi
        return result
    except Exception as exc:
        _log.debug("[yf-oi] OI fetch error for %s %s %s: %s", symbol, expiry, direction, exc)
        return {}
