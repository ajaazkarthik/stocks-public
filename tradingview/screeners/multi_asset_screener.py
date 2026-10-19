"""
Multi-Asset Screener — WIN-Equivalent Technical Scanner
========================================================
Covers all asset classes accessible from TV without watchlist limits:
  • Stocks  : US, AU, IN, HK, SG, JP, KR
  • Forex   : 8 major pairs + XAUUSD / XAGUSD  (FX: canonical only)
  • Crypto  : Top coins on Binance  (BINANCE: only, deduped)
  • Futures : Continuous contracts only  (GC1! / CL1! / NG1! / NQ1! / ES1! …)
  • Bonds   : Benchmark yields  (TVC: only)

Technical filter (per class, configurable):
  Bullish  — RSI in [rsi_min, rsi_max]  +  ADX > adx_min  +  close > SMA200
  Crypto   — RSI in [rsi_min, rsi_max]  +  ADX > adx_min  (no SMA200 field)
  Bonds    — no technical filter; show TVC benchmark yields only

Results are saved to multi_asset_report.html and optionally appended to
fires_raw.json so signal_stack.py can score stocks through the full pipeline.

Usage:
  python multi_asset_screener.py
  python multi_asset_screener.py --rsi-min 45 --adx-min 15 --max-per-class 40
  python multi_asset_screener.py --class stocks_us crypto --out my_report.html
  python multi_asset_screener.py --append-fires   # also append to fires_raw.json
  python multi_asset_screener.py --bearish         # show RSI < 50 + close < SMA200
"""
import json
import argparse
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Output files ───────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent
OUTPUT_HTML  = ROOT.parent / "reports" / "multi_asset_report.html"
FIRES_FILE   = ROOT / "fires_raw.json"

# ── Per-asset column lists ─────────────────────────────────────────────────────
# Only include fields confirmed valid by probe testing.
STOCK_COLS   = ["name", "description", "close", "change", "volume",
                "RSI", "ADX", "SMA200", "relative_volume_10d_calc",
                "recommendation_mark", "type",
                "market_cap_basic",          # used for min-market-cap filter
                "average_volume_10d_calc",    # used for min-volume filter
                "is_primary", "currency"]
CRYPTO_COLS  = ["name", "description", "close", "change", "volume",
                "RSI", "ADX", "ATR", "Volatility.D", "type"]
FOREX_COLS   = ["name", "description", "close", "change",
                "RSI", "ADX", "ATR", "SMA200", "type"]
FUTURES_COLS = ["name", "description", "close", "change", "volume",
                "RSI", "ADX", "ATR", "SMA200", "subtype", "type"]
BOND_COLS    = ["name", "description", "close", "change", "type"]

# ── Scan configuration ─────────────────────────────────────────────────────────
# Each entry: (scan_key, label, url, columns, extra_static_filters, max_results)
_BASE = "https://scanner.tradingview.com"
# The tuple's final value is retained for report/UI defaults. Discovery fetch
# depth and the engine's scoring budget are configured independently below.
# For stocks: RelVol > 1.0 + strong recommendation mark first.
# For crypto/forex: pure ADX+RSI rank.  For futures: pinned key contracts only.
SCAN_DEFS = [
    ("stocks_us", "🇺🇸 US Stocks",
     f"{_BASE}/america/scan",    STOCK_COLS,   [], 10),
    ("stocks_au", "🇦🇺 AU Stocks",
     f"{_BASE}/australia/scan",  STOCK_COLS,   [], 8),
    ("stocks_in", "🇮🇳 India Stocks",
     f"{_BASE}/india/scan",      STOCK_COLS,   [], 8),
    ("stocks_hk", "🇭🇰 HK Stocks",
     f"{_BASE}/hongkong/scan",   STOCK_COLS,   [], 5),
    ("stocks_sg", "🇸🇬 SG Stocks",
     f"{_BASE}/singapore/scan",  STOCK_COLS,   [], 5),
    ("stocks_jp", "🇯🇵 Japan Stocks",
     f"{_BASE}/japan/scan",      STOCK_COLS,   [], 5),
    ("stocks_kr", "🇰🇷 Korea Stocks",
     f"{_BASE}/korea/scan",      STOCK_COLS,   [], 5),
    ("crypto",    "₿ Crypto (Binance)",
     f"{_BASE}/crypto/scan",     CRYPTO_COLS,
     [{"left": "exchange", "operation": "equal", "right": "BINANCE"}], 8),
    ("forex",     "💱 Forex (FX canonical)",
     f"{_BASE}/forex/scan",      FOREX_COLS,
     [{"left": "exchange", "operation": "equal", "right": "FX"}], 6),
    ("futures",   "🛢 Futures (continuous)",
     f"{_BASE}/futures/scan",    FUTURES_COLS,
     # volume > 0 removes zero-volume/stale contracts that pollute top-ADX ranks
     [{"left": "subtype",  "operation": "equal",   "right": "continuous"},
      {"left": "volume",   "operation": "greater", "right": 0}], 10),
    ("bonds",     "📊 Bond Yields (TVC benchmarks)",
     f"{_BASE}/bonds/scan",      BOND_COLS,
     [{"left": "exchange", "operation": "equal", "right": "TVC"}], 10),
]

# TradingView reports market_cap_basic in the listing's local currency. Keep
# thresholds explicit per market; these values are raw units, not USD-converted.
MARKET_CAP_CURRENCY = {
    "stocks_us": "USD", "stocks_au": "AUD", "stocks_in": "INR",
    "stocks_hk": "HKD", "stocks_sg": "SGD", "stocks_jp": "JPY",
    "stocks_kr": "KRW",
}
MARKET_CAP_FLOORS = {
    "broad": {
        "stocks_us": 50_000_000, "stocks_au": 75_000_000,
        "stocks_in": 4_000_000_000, "stocks_hk": 400_000_000,
        "stocks_sg": 70_000_000, "stocks_jp": 7_500_000_000,
        "stocks_kr": 70_000_000_000,
    },
    "large_cap": {
        "stocks_us": 1_000_000_000, "stocks_au": 1_500_000_000,
        "stocks_in": 50_000_000_000, "stocks_hk": 8_000_000_000,
        "stocks_sg": 1_000_000_000, "stocks_jp": 150_000_000_000,
        "stocks_kr": 1_000_000_000_000,
    },
}
DEFAULT_FETCH_LIMIT = 600
DEFAULT_SCORE_LIMIT = 20
MAX_FETCH_LIMIT = 2_000
PAGE_SIZE = 100


def _positive_finite(value) -> bool:
    """True only for numeric, finite values greater than zero."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _profile_name(value: str | None = None) -> str:
    profile = (value or os.getenv("SIGNAL_SCANNER_PROFILE", "broad")).strip().lower()
    if profile not in MARKET_CAP_FLOORS:
        raise ValueError(f"Unknown scanner profile {profile!r}; valid: {sorted(MARKET_CAP_FLOORS)}")
    return profile


def _fetch_limit(value: int | None = None) -> int:
    raw = value if value is not None else os.getenv("SIGNAL_SCANNER_FETCH_LIMIT", str(DEFAULT_FETCH_LIMIT))
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("scanner fetch limit must be an integer") from exc
    if not 1 <= limit <= MAX_FETCH_LIMIT:
        raise ValueError(f"scanner fetch limit must be between 1 and {MAX_FETCH_LIMIT}")
    return limit


# ── Quality ranker for stocks ─────────────────────────────────────────────────
RECOMMENDATION_RANK = {
    "Strong Buy": 5, "Buy": 4, "Neutral": 3, "Sell": 2, "Strong Sell": 1,
}

def _quality_sort_key(r: dict) -> float:
    """
    Score each signal for quality ranking.
    Higher = better. Used for stocks only.
    Combines: ADX (trend strength) + RelVol (interest) + recommendation.
    """
    adx     = r.get("ADX") or 0
    relvol  = r.get("relative_volume_10d_calc") or 1.0
    rec     = RECOMMENDATION_RANK.get(r.get("recommendation_mark") or "", 3)
    # Weighted score: heavy on ADX (trend), moderate on rec, small rel-vol bonus
    return adx * 0.5 + rec * 10 + min(relvol, 5) * 2


# ── Key commodity/index futures — always include regardless of batch position ─
# These are the most traded continuous contracts; they may be missed when
# obscure single-stock futures rank higher by ADX in the batch results.
PINNED_FUTURES = [
    "NYMEX:CL1!",    # WTI Crude Oil
    "ICEEUR:BRN1!",  # Brent Crude
    "COMEX:GC1!",    # Gold
    "COMEX:SI1!",    # Silver
    "NYMEX:NG1!",    # Natural Gas
    "CME:NQ1!",      # Nasdaq-100 Futures
    "CME:ES1!",      # S&P 500 Futures
    "CBOT:ZW1!",     # Wheat
    "CBOT:ZC1!",     # Corn
    "CME:HG1!",      # Copper
]

# ── Core screener fetch ─────────────────────────────────────────────────────────
def _tv_scan(url: str, columns: list, filters: list, sort_by: str,
             max_results: int, retries: int = 2, offset: int = 0) -> list[dict]:
    """
    POST to a TV Screener endpoint, return list of record dicts.
    Each record: { field: value, ... }  with an extra 'tv_symbol' key.
    """
    payload = {
        "columns": columns,
        "filter": filters,
        "sort": {"sortBy": sort_by, "sortOrder": "desc"},
        "range": [offset, offset + max_results],
    }
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0",
            "Origin":       "https://www.tradingview.com",
        }
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict):
                raise ValueError("malformed TradingView response: expected JSON object")
            if any(key in data for key in ("error", "errors", "error_code")) or data.get("success") is False:
                raise ValueError(f"TradingView returned an error payload: {str(data)[:300]}")
            if "data" not in data or not isinstance(data["data"], list):
                raise ValueError("malformed TradingView response: expected a data array")
            rows = []
            for item in data["data"]:
                if not isinstance(item, dict) or not isinstance(item.get("d"), list):
                    raise ValueError("malformed TradingView response: invalid data row")
                sym = item.get("s", "")
                d   = item.get("d", [])
                if not isinstance(sym, str) or not sym:
                    raise ValueError("malformed TradingView response: row has no symbol")
                if len(d) != len(columns):
                    raise ValueError("malformed TradingView response: row column count mismatch")
                rec = {"tv_symbol": sym}
                for i, col in enumerate(columns):
                    rec[col] = d[i] if i < len(d) else None
                # Preserve provider-supplied row metadata verbatim. In particular,
                # do not convert observation time into a quote timestamp.
                for key, value in item.items():
                    if key not in {"s", "d"}:
                        rec[f"_tv_{key}"] = value
                rows.append(rec)
            return rows
        except urllib.error.HTTPError as he:
            if he.code == 429 and attempt < retries:
                # Rate-limited — exponential backoff
                wait = 2 ** attempt
                print(f"  [screener] HTTP 429 rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            elif attempt < retries:
                time.sleep(1)
            else:
                return [{"_error": f"HTTP {he.code}: {he.reason}"}]
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                return [{"_error": f"{type(e).__name__}: {e}"}]
    return []


# ── Filter helpers ─────────────────────────────────────────────────────────────
# OTC/Pink-sheet exchange prefixes — rejected for ALL stock scans
_OTC_PREFIXES: frozenset[str] = frozenset({"OTC", "OTCMKTS", "OTCBB", "PINK"})


def _make_filters(rsi_min: float, rsi_max: float, adx_min: float,
                  static_filters: list, bearish: bool, has_sma200: bool,
                  min_close: float = 0.0,
                  min_market_cap: float = 0.0) -> list:
    """Build dynamic filter list for bullish or bearish mode.

    Args:
        min_close: If > 0, adds a server-side close price floor filter.
                   Pass 1.0 for stock scans to reject penny stocks at the API level.
        min_market_cap: If > 0, adds a server-side market-cap floor in listing currency.
                        Default 50 000 000 ($50 M) for stock scans.
    """
    f = list(static_filters)        # start with any static (exchange, subtype…)

    # Penny-stock guard: reject stocks below minimum price at the TV scanner API
    # level so sub-$1 micro-caps never reach the scoring pipeline.
    if min_close > 0:
        f.append({"left": "close", "operation": "greater", "right": min_close})

    # Market-cap guard: reject nano-cap / shell companies at the API level
    if min_market_cap > 0:
        f.append({"left": "market_cap_basic", "operation": "greater", "right": min_market_cap})

    if bearish:
        f.append({"left": "RSI",    "operation": "in_range",
                  "right": [max(0, 100 - rsi_max), max(0, 100 - rsi_min)]})
    else:
        f.append({"left": "RSI",    "operation": "in_range",
                  "right": [rsi_min, rsi_max]})

    f.append(    {"left": "ADX",    "operation": "greater",      "right": adx_min})
    return f


def _post_filter_sma200(rows: list, bearish: bool) -> list:
    """Client-side filter: keep rows where close is above (bullish) / below (bearish) SMA200."""
    out = []
    for r in rows:
        if "_error" in r:
            continue
        close  = r.get("close")
        sma200 = r.get("SMA200")
        if close is None or sma200 is None:
            out.append(r)   # Some non-stock asset classes do not expose SMA200.
            continue
        if bearish and close < sma200:
            out.append(r)
        elif (not bearish) and close > sma200:
            out.append(r)
    return out


def _fetch_pages(url: str, columns: list, filters: list, sort_by: str,
                 fetch_limit: int) -> tuple[list[dict], int]:
    """Fetch a bounded discovery universe in pages; keep adapter errors intact."""
    rows: list[dict] = []
    raw_count = 0
    seen: set[str] = set()
    for offset in range(0, fetch_limit, PAGE_SIZE):
        page_size = min(PAGE_SIZE, fetch_limit - offset)
        page = _tv_scan(url, columns, filters, sort_by, page_size, offset=offset)
        if page and page[0].get("_error"):
            return page, raw_count
        raw_count += len(page)
        for row in page:
            symbol = row.get("tv_symbol")
            if symbol and symbol in seen:
                continue
            if symbol:
                seen.add(symbol)
            rows.append(row)
        if len(page) < page_size:
            break
    return rows, raw_count


def _stock_row_is_valid(row: dict, scan_key: str, min_volume: float,
                        min_relvol: float, bearish: bool, rsi_min: float,
                        rsi_max: float, adx_min: float, min_close: float,
                        min_market_cap: float) -> bool:
    """Require an explicitly identified primary common-stock listing and usable data."""
    if str(row.get("type") or "").strip().lower() != "stock":
        return False
    primary = row.get("is_primary")
    if primary is not True and primary != 1:
        return False
    expected_ccy = MARKET_CAP_CURRENCY.get(scan_key)
    if not expected_ccy or str(row.get("currency") or "").upper() != expected_ccy:
        return False
    for field in ("close", "RSI", "ADX", "SMA200", "market_cap_basic",
                  "average_volume_10d_calc", "relative_volume_10d_calc"):
        if not _positive_finite(row.get(field)):
            return False
    try:
        rsi = float(row["RSI"])
        rsi_low, rsi_high = ((max(0, 100 - rsi_max), max(0, 100 - rsi_min))
                             if bearish else (rsi_min, rsi_max))
        if (not rsi_low <= rsi <= rsi_high or rsi > 100
                or float(row["ADX"]) <= adx_min
                or float(row["close"]) <= min_close
                or float(row["market_cap_basic"]) <= min_market_cap
                or float(row["average_volume_10d_calc"]) < min_volume):
            return False
        if not bearish and float(row["relative_volume_10d_calc"]) < min_relvol:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    if row.get("recommendation_mark") == "Strong Sell":
        return False
    sym = str(row.get("tv_symbol") or "")
    exch = sym.split(":", 1)[0].upper() if ":" in sym else ""
    if not exch or exch in _OTC_PREFIXES:
        return False
    if scan_key == "stocks_us" and exch not in {"NYSE", "NASDAQ", "AMEX", "CBOE", "BATS"}:
        return False
    close, sma = float(row["close"]), float(row["SMA200"])
    return close < sma if bearish else close > sma


# ── Main scan runner ───────────────────────────────────────────────────────────
def run_all_scans(args) -> dict[str, list]:
    """Run configured scans and attach raw/filter/selected coverage to args."""
    wanted = set(args.scan_class) if args.scan_class else None
    results = {}
    diagnostics = {}
    profile = _profile_name(getattr(args, "profile", None))
    fetch_limit = _fetch_limit(getattr(args, "fetch_limit", None))
    requested_score_limit = getattr(args, "max_per_class", None)

    for (scan_key, label, url, cols, static_f, max_r) in SCAN_DEFS:
        if wanted and scan_key not in wanted:
            continue

        # Bonds — no technical filter, just pull TVC yields
        if scan_key == "bonds":
            print(f"  {label} …", end=" ", flush=True)
            rows = _tv_scan(url, cols, static_f, "change", max_r)
            if rows and "_error" in rows[0]:
                print(f"ERROR: {rows[0]['_error']}")
            else:
                print(f"{len(rows)} yields")
            results[scan_key] = rows
            diagnostics[scan_key] = {"profile": profile, "fetch_limit": fetch_limit,
                                     "scoring_limit": len(rows), "raw": len(rows),
                                     "filtered": len(rows), "selected": len(rows),
                                     "currency": None, "market_cap_floor": None}
            continue

        # Crypto — no SMA200 field
        is_crypto = (scan_key == "crypto")

        # Enforce price, market-cap and primary-listing constraints server-side;
        # client validation below checks the returned metadata again.
        _min_close     = 1.0        if scan_key.startswith("stocks_") else 0.0
        _min_mcap = (getattr(args, "min_market_cap", None)
                     if getattr(args, "min_market_cap", None) is not None
                     else MARKET_CAP_FLOORS[profile].get(scan_key, 0.0)) \
                    if scan_key.startswith("stocks_") else 0.0
        scan_static_filters = list(static_f)
        if scan_key.startswith("stocks_"):
            scan_static_filters.append({"left": "is_primary", "operation": "equal", "right": True})
        filters = _make_filters(
            rsi_min=args.rsi_min, rsi_max=args.rsi_max, adx_min=args.adx_min,
            static_filters=scan_static_filters, bearish=args.bearish, has_sma200=(not is_crypto),
            min_close=_min_close, min_market_cap=_min_mcap,
        )

        print(f"  {label} …", end=" ", flush=True)
        is_stock = scan_key.startswith("stocks_")
        top_n = max(1, int(requested_score_limit or DEFAULT_SCORE_LIMIT)) if is_stock \
                else (requested_score_limit or max_r)
        # Deeper discovery applies to stock universes; preserve legacy scan
        # budgets for non-stock asset classes.
        batch = 500 if scan_key == "futures" else (fetch_limit if is_stock else top_n * 3)
        if batch > PAGE_SIZE:
            rows, raw_count = _fetch_pages(url, cols, filters, "ADX", batch)
        else:
            rows = _tv_scan(url, cols, filters, "ADX", batch)
            raw_count = len(rows)

        if rows and "_error" in rows[0]:
            print(f"ERROR: {rows[0]['_error']}")
            # Preserve the failure for strict consumers such as the durable
            # scheduler. Other legacy callers already skip rows carrying
            # _error and retain their existing behavior.
            results[scan_key] = rows
            diagnostics[scan_key] = {"profile": profile, "fetch_limit": batch,
                                     "scoring_limit": top_n, "raw": raw_count,
                                     "filtered": 0, "selected": 0,
                                     "currency": MARKET_CAP_CURRENCY.get(scan_key),
                                     "market_cap_floor": _min_mcap}
            continue

        # A valid empty `data` array is a successful no-result scan.
        if not rows:
            print("0 results")
            results[scan_key] = []
            diagnostics[scan_key] = {"profile": profile, "fetch_limit": batch,
                                     "scoring_limit": top_n, "raw": raw_count,
                                     "filtered": 0, "selected": 0,
                                     "currency": MARKET_CAP_CURRENCY.get(scan_key),
                                     "market_cap_floor": _min_mcap}
            continue

        # Client-side SMA200 filter (stocks, forex, futures)
        if not is_crypto and not scan_key.startswith("stocks_"):
            rows = _post_filter_sma200(rows, args.bearish)

        # Quality pre-filter for stocks: drop low-conviction signals
        # Keep only: relative volume >= 0.8 AND recommendation != Strong Sell
        # AND exclude non-tradeable OTC/pink-sheet exchanges for US
        if scan_key.startswith("stocks_"):
            min_relvol = 0.8 if not args.bearish else 0.0  # bearish: volume filter less relevant
            min_vol = getattr(args, "min_volume", 100_000)
            rows = [r for r in rows if _stock_row_is_valid(
                r, scan_key, min_vol, min_relvol, args.bearish,
                args.rsi_min, args.rsi_max, args.adx_min, _min_close, _min_mcap)]

        # For futures: always include pinned key commodities/indices if they
        # pass technical criteria (they may be buried beyond the top-ADX batch)
        if scan_key == "futures":
            existing_syms = {r.get("tv_symbol") for r in rows}
            missing_pins = [s for s in PINNED_FUTURES if s not in existing_syms]
            if missing_pins:
                pin_rows = _tv_scan(url, cols,
                    [{"left": "name", "operation": "in_range", "right": missing_pins}],
                    "ADX", len(missing_pins) + 5)
                for pr in pin_rows:
                    if "_error" in pr:
                        continue
                    rsi = pr.get("RSI")
                    adx = pr.get("ADX")
                    close = pr.get("close")
                    sma200 = pr.get("SMA200")
                    # Apply same criteria as main scan (without volume filter)
                    rsi_ok = rsi is not None and args.rsi_min <= rsi <= args.rsi_max
                    adx_ok = adx is not None and adx > args.adx_min
                    sma_ok = (close is None or sma200 is None or
                              (close > sma200 if not args.bearish else close < sma200))
                    if rsi_ok and adx_ok and sma_ok:
                        pr["_pinned"] = True
                        rows.append(pr)

        # Quality sort for stocks: rank by ADX + recommendation + relative volume
        # then truncate to actionable top_n.
        # For crypto / forex / futures: ADX-sorted from the API is fine.
        if scan_key.startswith("stocks_"):
            rows.sort(key=_quality_sort_key, reverse=True)

        filtered_count = len(rows)
        rows = rows[:top_n]
        print(f"{len(rows)} signals")
        results[scan_key] = rows
        diagnostics[scan_key] = {
            "profile": profile, "fetch_limit": batch, "scoring_limit": top_n,
            "raw": raw_count, "filtered": filtered_count, "selected": len(rows),
            "currency": MARKET_CAP_CURRENCY.get(scan_key),
            "market_cap_floor": _min_mcap,
        }

    args._scan_diagnostics = diagnostics
    return results


# ── Optional: append to fires_raw.json ────────────────────────────────────────
def append_to_fires(results: dict) -> int:
    """
    Append screener-originated signals to fires_raw.json so signal_stack.py
    can score them through the full pipeline (stocks only — those have
    fundamentals, news sentiment, etc.).
    Returns count appended.
    """
    stock_keys = {"stocks_us", "stocks_au", "stocks_in",
                  "stocks_hk", "stocks_sg", "stocks_jp", "stocks_kr"}
    now_iso = datetime.now(timezone.utc).isoformat()
    new_fires = []

    for key, rows in results.items():
        if key not in stock_keys:
            continue
        for r in rows:
            if "_error" in r:
                continue
            sym = r.get("tv_symbol") or r.get("name", "")
            if not sym or ":" not in sym:
                continue
            rsi = r.get("RSI")
            adx = r.get("ADX")
            msg = "SCREENER BUY:1"
            if rsi is not None:
                msg += f", rsi:{rsi:.1f}"
            if adx is not None:
                msg += f", ADX:{adx:.1f}"
            rel_vol = r.get("relative_volume_10d_calc")
            if rel_vol is not None:
                msg += f", relVol:{rel_vol:.2f}"

            new_fires.append({
                "fire_id":       int(time.time() * 1000),
                "alert_id":      0,
                "symbol":        sym,
                "pro_symbol":    sym,
                "resolution":    "D",
                "sound_file":    None,
                "sound_duration": 0,
                "popup":         False,
                "cross_interval": False,
                "message":       msg,
                "fire_time":     now_iso,
                "bar_time":      now_iso,
                "name":          f"screener_{sym}",
                "kinds":         ["screener"],
                "close":         r.get("close"),
                "_source":       "screener",
            })

    if not new_fires:
        return 0

    if FIRES_FILE.exists():
        with open(FIRES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"all": []}

    # Prepend — signal_stack.py reads newest first
    data["all"] = new_fires + data["all"]
    with open(FIRES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return len(new_fires)


# ── Leveraged ETF watchlist ────────────────────────────────────────────────────
# Known 2x/3x leveraged ETFs across US + AU + HK markets.
# Use yfinance to fetch last price, 1-day change, 1-month change (momentum proxy).

LEVERAGED_ETFS = [
    # US leveraged ETFs
    {"sym": "TQQQ",    "desc": "3x Nasdaq-100 (US)",          "yf": "TQQQ",      "market": "US"},
    {"sym": "SOXL",    "desc": "3x Semis (US)",               "yf": "SOXL",      "market": "US"},
    {"sym": "UDOW",    "desc": "3x Dow Jones (US)",           "yf": "UDOW",      "market": "US"},
    {"sym": "UPRO",    "desc": "3x S&P500 (US)",              "yf": "UPRO",      "market": "US"},
    {"sym": "LABU",    "desc": "3x Biotech (US)",             "yf": "LABU",      "market": "US"},
    {"sym": "FNGU",    "desc": "3x FANG+ (US)",               "yf": "FNGU",      "market": "US"},
    {"sym": "SQQQ",    "desc": "3x Inverse Nasdaq (US)",      "yf": "SQQQ",      "market": "US", "inverse": True},
    {"sym": "SPXS",    "desc": "3x Inverse S&P500 (US)",      "yf": "SPXS",      "market": "US", "inverse": True},
    # AU leveraged ETFs (ASX)
    {"sym": "LNAS.AX", "desc": "2x Nasdaq-100 (AU)",          "yf": "LNAS.AX",   "market": "AU"},
    {"sym": "BBOZ.AX", "desc": "2x Inverse ASX200 (AU Bear)", "yf": "BBOZ.AX",   "market": "AU", "inverse": True},
    {"sym": "GEAR.AX", "desc": "2x ASX200 (AU)",              "yf": "GEAR.AX",   "market": "AU"},
    # HK leveraged ETFs on HSI futures
    {"sym": "7200.HK", "desc": "2x HSI (HK)",                 "yf": "7200.HK",   "market": "HK"},
    {"sym": "7300.HK", "desc": "2x HSCEI (HK)",               "yf": "7300.HK",   "market": "HK"},
    {"sym": "7552.HK", "desc": "2x Short HSI (HK Bear)",      "yf": "7552.HK",   "market": "HK", "inverse": True},
]


def fetch_leveraged_etfs() -> list[dict]:
    """
    Fetch price data for the leveraged ETF watchlist via yfinance.
    Returns list of dicts with price, 1d change, 1m change, RSI-proxy.
    """
    try:
        import yfinance as yf
    except ImportError:
        return [{"_error": "yfinance not installed — pip install yfinance"}]

    results = []
    yf_syms = [e["yf"] for e in LEVERAGED_ETFS]
    try:
        data = yf.download(yf_syms, period="2mo", interval="1d", progress=False, auto_adjust=True)
    except Exception as exc:
        return [{"_error": f"yfinance download failed: {exc}"}]

    close_df = data.get("Close") if hasattr(data, "get") else None
    if close_df is None:
        # Single ticker returns different structure
        close_df = data["Close"] if "Close" in data.columns else None
    if close_df is None:
        return [{"_error": "yfinance returned unexpected structure"}]

    for etf in LEVERAGED_ETFS:
        yf_sym = etf["yf"]
        try:
            if yf_sym in close_df.columns:
                series = close_df[yf_sym].dropna()
            else:
                series = close_df.dropna()  # fallback single ticker

            if len(series) < 2:
                continue

            price_now  = float(series.iloc[-1])
            price_1d   = float(series.iloc[-2])
            price_1m   = float(series.iloc[-22]) if len(series) >= 22 else float(series.iloc[0])

            chg_1d = (price_now / price_1d - 1) * 100 if price_1d else None
            chg_1m = (price_now / price_1m - 1) * 100 if price_1m else None

            # Simple RSI(14) from close series
            rsi = None
            if len(series) >= 15:
                delta  = series.diff().dropna()
                gain   = delta.clip(lower=0).rolling(14).mean().iloc[-1]
                loss   = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
                if loss > 0:
                    rsi = round(100 - 100 / (1 + gain / loss), 1)

            # Momentum signal
            signal = "—"
            if rsi is not None and chg_1m is not None:
                if not etf.get("inverse"):
                    if rsi >= 60 and chg_1m >= 5:
                        signal = "STRONG"
                    elif rsi >= 50 and chg_1m >= 0:
                        signal = "BULL"
                    elif rsi < 40 or chg_1m < -10:
                        signal = "WEAK"
                else:
                    # For inverse ETFs, a bear market signal is "interesting"
                    if rsi >= 60 and chg_1m >= 5:
                        signal = "BEAR-ON"
                    elif rsi < 40:
                        signal = "BEAR-OFF"

            results.append({
                "sym":      etf["sym"],
                "desc":     etf["desc"],
                "market":   etf["market"],
                "inverse":  etf.get("inverse", False),
                "price":    round(price_now, 4),
                "chg_1d":   round(chg_1d, 2) if chg_1d is not None else None,
                "chg_1m":   round(chg_1m, 2) if chg_1m is not None else None,
                "rsi":      rsi,
                "signal":   signal,
                "tv_url":   f"https://www.tradingview.com/chart/?symbol={etf['sym'].replace('.AX', 'ASX:').replace('.HK', 'HKEX:')}",
            })
        except Exception:
            results.append({
                "sym":   etf["sym"],
                "desc":  etf["desc"],
                "market": etf["market"],
                "_error": "data unavailable",
            })
    return results


def _build_leveraged_etf_html(etf_rows: list, id_prefix: str = "") -> str:
    """Build HTML section for leveraged ETF watchlist."""
    sec_id = f"{id_prefix}leveraged_etfs"
    if not etf_rows:
        return ""
    if "_error" in (etf_rows[0] if etf_rows else {}):
        return f"""
<section id="{sec_id}">
  <h2>⚡ Leveraged ETFs</h2>
  <p style="color:#f44336">{etf_rows[0]['_error']}</p>
</section>"""

    ok_rows = [r for r in etf_rows if "_error" not in r]
    # Group by market
    by_market: dict[str, list] = {}
    for r in ok_rows:
        by_market.setdefault(r["market"], []).append(r)

    rows_html = ""
    for market, rows in sorted(by_market.items()):
        flag = {"US": "🇺🇸", "AU": "🇦🇺", "HK": "🇭🇰"}.get(market, market)
        for r in rows:
            sym   = r["sym"]
            desc  = r["desc"]
            price = r.get("price")
            c1d   = r.get("chg_1d")
            c1m   = r.get("chg_1m")
            rsi   = r.get("rsi")
            sig   = r.get("signal", "—")
            inv   = r.get("inverse", False)

            c1d_color = "#69f0ae" if (c1d or 0) >= 0 else "#ff7043"
            c1m_color = "#69f0ae" if (c1m or 0) >= 0 else "#ff7043"
            rsi_color = ("#ff7043" if (rsi or 0) >= 70 else
                         "#69f0ae" if (rsi or 0) >= 50 else
                         "#f44336" if (rsi or 0) <= 30 else "#ffeb3b")
            sig_map = {
                "STRONG":   ("#00e676", "⚡ STRONG"),
                "BULL":     ("#69f0ae", "↑ BULL"),
                "WEAK":     ("#ff7043", "↓ WEAK"),
                "BEAR-ON":  ("#ff7043", "🐻 BEAR-ON"),
                "BEAR-OFF": ("#69f0ae", "🐂 BEAR-OFF"),
                "—":        ("#888",    "—"),
            }
            sig_col, sig_lbl = sig_map.get(sig, ("#888", sig))
            inv_badge = ' <span title="inverse/short ETF" style="color:#ff9800;font-size:0.8em">⬇INV</span>' if inv else ""
            tv_url = r.get("tv_url", f"https://www.tradingview.com/chart/?symbol={sym}")

            rows_html += f"""
<tr>
  <td>{flag}
    <a href="{tv_url}" target="_blank" style="color:#90caf9;text-decoration:none">{sym}</a>{inv_badge}<br>
    <small style="color:#aaa">{desc}</small>
  </td>
  <td>{f'{price:.4f}' if price is not None else '—'}</td>
  <td style="color:{c1d_color}">{f'{c1d:+.2f}%' if c1d is not None else '—'}</td>
  <td style="color:{c1m_color}">{f'{c1m:+.2f}%' if c1m is not None else '—'}</td>
  <td style="color:{rsi_color};font-weight:600">{f'{rsi:.1f}' if rsi is not None else '—'}</td>
  <td style="color:{sig_col};font-weight:600">{sig_lbl}</td>
</tr>"""

    count = len(ok_rows)
    return f"""
<section id="{sec_id}">
  <h2>⚡ Leveraged ETFs <span style="background:#7b1fa2;border-radius:10px;padding:2px 10px;font-size:0.75em">{count}</span>
    <small style="font-size:0.65em;color:#b0bec5;font-weight:400"> via yfinance</small>
  </h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th scope='col'>Symbol</th><th scope='col'>Price</th><th scope='col'>1D Chg</th><th scope='col'>1M Chg</th>
      <th scope='col'>RSI(14)</th><th scope='col'>Signal</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
</section>"""


# ── India NSE PCR + OI Buildup ─────────────────────────────────────────────────

def fetch_india_internals() -> dict:
    """
    Fetch NSE PCR and OI Buildup data via Angel One SmartAPI.
    Returns {"pcr": list[dict], "oi_long": list[dict], "oi_short": list[dict], "_error": str|None}
    """
    result: dict = {"pcr": [], "oi_long": [], "oi_short": [], "_error": None}
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, str(Path(__file__).parent.parent.parent / "india-brokers"))
        from india_market_internals import IndiaMarketInternals
        mi = IndiaMarketInternals()
        result["pcr"]      = mi.pcr()
        result["oi_long"]  = mi.oi_buildup("Long Built Up",  "NEAR")
        result["oi_short"] = mi.oi_buildup("Short Built Up", "NEAR")
    except ImportError:
        result["_error"] = "india_market_internals not available"
    except Exception as exc:
        result["_error"] = f"AngelOne error: {str(exc)[:80]}"
    return result


def _build_india_internals_html(data: dict, id_prefix: str = "") -> str:
    """Build HTML section for India NSE PCR + OI Buildup."""
    sec_id = f"{id_prefix}india_internals"
    error = data.get("_error")
    if error:
        return f"""
<section id="{sec_id}">
  <h2>🇮🇳 India NSE F&amp;O Internals (AngelOne)</h2>
  <p style="color:#f44336">{error}</p>
</section>"""

    pcr_list  = data.get("pcr", [])
    oi_long   = data.get("oi_long", [])
    oi_short  = data.get("oi_short", [])

    if not pcr_list and not oi_long and not oi_short:
        return f"""
<section id="{sec_id}">
  <h2>🇮🇳 India NSE F&amp;O Internals (AngelOne)</h2>
  <p style="color:#888">No data — AngelOne session may have expired.</p>
</section>"""

    # PCR table (top 20)
    pcr_html = ""
    for item in sorted(pcr_list, key=lambda x: x.get("pcr", 0)):
        sym   = item.get("symbol", "")
        pcr_v = item.get("pcr", 0)
        lbl   = item.get("label", "")
        col   = ("#00e676" if pcr_v < 0.5 else
                 "#69f0ae" if pcr_v < 0.8 else
                 "#ffeb3b" if pcr_v < 1.2 else
                 "#ff7043" if pcr_v < 1.5 else "#f44336")
        pcr_html += f"""<tr>
  <td><a href="https://www.tradingview.com/chart/?symbol=NSE:{sym}" target="_blank"
     style="color:#90caf9;text-decoration:none">{sym}</a></td>
  <td style="color:{col};font-weight:600">{pcr_v:.3f}</td>
  <td style="color:{col}">{lbl}</td>
</tr>"""

    # OI Buildup — Long + Short combined
    oi_html = ""
    for item in (oi_long[:10] + oi_short[:10]):
        sym     = item.get("symbol", "")
        ltp     = item.get("ltp")
        pct_chg = item.get("pct_change")
        oi      = item.get("oi")
        oi_chg  = item.get("oi_change_pct")
        oi_type = item.get("dataType", "")
        col     = ("#69f0ae" if oi_type == "Long Built Up" else
                   "#ff7043" if oi_type == "Short Built Up" else
                   "#ffeb3b")
        badge   = ("🐂" if oi_type == "Long Built Up" else
                   "🐻" if oi_type == "Short Built Up" else "")
        oi_str  = f"{oi/1e6:.1f}M" if oi and oi >= 1e6 else (str(oi) if oi else "—")
        oi_html += f"""<tr>
  <td><a href="https://www.tradingview.com/chart/?symbol=NSE:{sym}" target="_blank"
      style="color:#90caf9;text-decoration:none">{sym}</a></td>
  <td>{f'{ltp:.2f}' if ltp else '—'}</td>
  <td style="color={'#69f0ae' if (pct_chg or 0) >= 0 else '#ff7043'}">{f'{pct_chg:+.2f}%' if pct_chg is not None else '—'}</td>
  <td>{oi_str}</td>
  <td style="color:{'#69f0ae' if (oi_chg or 0) >= 0 else '#ff7043'}">{f'{oi_chg:+.1f}%' if oi_chg is not None else '—'}</td>
  <td style="color:{col};font-weight:600">{badge} {oi_type}</td>
</tr>"""

    pcr_count = len(pcr_list)
    oi_count  = len(oi_long) + len(oi_short)
    return f"""
<section id="{sec_id}">
  <h2>🇮🇳 India NSE F&amp;O Internals
    <span style="background:#1565c0;border-radius:10px;padding:2px 10px;font-size:0.75em">{pcr_count} PCR</span>
    <span style="background:#2e7d32;border-radius:10px;padding:2px 10px;font-size:0.75em;margin-left:4px">{oi_count} OI</span>
    <small style="font-size:0.65em;color:#b0bec5;font-weight:400"> via AngelOne SmartAPI</small>
  </h2>
  <div style="display:flex;gap:20px;flex-wrap:wrap">
    <div style="flex:1;min-width:280px">
      <h3 style="font-size:0.95em;color:#b0bec5;margin-bottom:8px">Put-Call Ratio (NEAR expiry)</h3>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th scope='col'>Symbol</th><th scope='col'>PCR</th><th scope='col'>Sentiment</th></tr></thead>
        <tbody>{pcr_html if pcr_html else '<tr><td colspan="3" style="color:#888">No PCR data</td></tr>'}</tbody>
      </table>
      </div>
    </div>
    <div style="flex:1;min-width:320px">
      <h3 style="font-size:0.95em;color:#b0bec5;margin-bottom:8px">OI Buildup (Long + Short)</h3>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th scope='col'>Symbol</th><th scope='col'>LTP</th><th scope='col'>Chg%</th><th scope='col'>OI</th><th scope='col'>OI Chg%</th><th scope='col'>Type</th></tr></thead>
        <tbody>{oi_html if oi_html else '<tr><td colspan="6" style="color:#888">No OI data</td></tr>'}</tbody>
      </table>
      </div>
    </div>
  </div>
</section>"""


# ── HTML report builder ────────────────────────────────────────────────────────
_LABEL_MAP = {d[0]: d[1] for d in SCAN_DEFS}

_REC_LABELS = {
    None: ("—", "#888"),
    1:    ("STRONG BUY",  "#00e676"),
    2:    ("BUY",         "#69f0ae"),
    3:    ("NEUTRAL",     "#ffeb3b"),
    4:    ("SELL",        "#ff7043"),
    5:    ("STRONG SELL", "#f44336"),
}

def _rec_badge(val) -> str:
    if val is None:
        return '<span style="color:#888">—</span>'
    # TV rec_mark: 1=Strong Buy, 2=Buy, 3=Neutral, 4=Sell, 5=Strong Sell
    try:
        v = round(float(val))
    except (TypeError, ValueError):
        return f'<span style="color:#888">{val}</span>'
    label, colour = _REC_LABELS.get(v, ("?", "#888"))
    return f'<span style="color:{colour};font-weight:600">{label}</span>'

def _rsi_color(val) -> str:
    if val is None:
        return "#ccc"
    v = float(val)
    if v >= 70:
        return "#ff7043"   # overbought
    if v >= 50:
        return "#69f0ae"   # bullish
    if v <= 30:
        return "#f44336"   # oversold
    return "#ffeb3b"       # neutral

def _fmt(val, digits=2, suffix="") -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(val)

def _rows_html(rows: list, cols: list) -> str:
    if not rows:
        return ("<p style='color:#888;padding:12px'>No signals met the scoring threshold "
                "across all scanned markets. Try lowering <code>--threshold</code> or "
                "running during market hours for fresher data.</p>")

    header = "".join(f"<th scope='col'>{c}</th>" for c in cols)
    body = ""
    for r in rows:
        sym  = r.get("tv_symbol", r.get("name", ""))
        desc = r.get("description", "")
        close  = r.get("close")
        change = r.get("change")
        vol    = r.get("volume")
        rsi    = r.get("RSI")
        adx    = r.get("ADX")
        sma200 = r.get("SMA200")
        relvol = r.get("relative_volume_10d_calc")
        rec    = r.get("recommendation_mark")
        atr    = r.get("ATR")
        volat  = r.get("Volatility.D")
        subtyp = r.get("subtype", "")

        change_color = "#69f0ae" if (change or 0) >= 0 else "#ff7043"
        rsi_col  = _rsi_color(rsi)
        sma_flag = ""
        if close is not None and sma200 is not None:
            sma_flag = "▲" if close > sma200 else "▼"
            sma_flag_col = "#69f0ae" if close > sma200 else "#ff7043"
            sma_flag = f'<span style="color:{sma_flag_col}">{sma_flag}</span>'

        pinned_badge = ' <span title="Key commodity — always included" style="font-size:0.75em">📌</span>' if r.get("_pinned") else ""
        sym_link = f'<a href="https://www.tradingview.com/chart/?symbol={sym}" target="_blank" style="color:#90caf9;text-decoration:none">{sym}</a>{pinned_badge}'

        cells = [
            f"<td>{sym_link}<br><small style='color:#aaa'>{desc[:40]}</small></td>",
            f"<td>{_fmt(close, 4)}</td>",
            f"<td style='color:{change_color}'>{_fmt(change, 2)}%</td>",
        ]

        # RSI (if present)
        if rsi is not None:
            cells.append(f"<td style='color:{rsi_col};font-weight:600'>{_fmt(rsi, 1)}</td>")
        # ADX
        if adx is not None:
            adx_col = "#69f0ae" if float(adx) > 25 else "#ffeb3b"
            cells.append(f"<td style='color:{adx_col}'>{_fmt(adx, 1)}</td>")
        # SMA200
        if sma200 is not None:
            cells.append(f"<td>{_fmt(sma200, 2)} {sma_flag}</td>")
        # rel_vol
        if relvol is not None:
            rv_col = "#69f0ae" if float(relvol) >= 1.5 else "#ccc"
            cells.append(f"<td style='color:{rv_col}'>{_fmt(relvol, 2)}x</td>")
        # rec_mark
        if rec is not None:
            cells.append(f"<td>{_rec_badge(rec)}</td>")
        # ATR
        if atr is not None:
            cells.append(f"<td>{_fmt(atr, 3)}</td>")
        # Volatility.D (crypto)
        if volat is not None:
            cells.append(f"<td>{_fmt(volat, 2)}%</td>")
        # volume
        if vol is not None:
            try:
                v = float(vol)
                vol_str = f"{v/1e9:.2f}B" if v >= 1e9 else f"{v/1e6:.2f}M" if v >= 1e6 else f"{v/1e3:.1f}K" if v >= 1e3 else str(v)
            except (TypeError, ValueError):
                vol_str = "—"
            cells.append(f"<td>{vol_str}</td>")
        # subtype (futures)
        if subtyp:
            cells.append(f"<td style='color:#b0bec5'>{subtyp}</td>")

        body += f"<tr>{''.join(cells)}</tr>"

    return f"""
<div style="overflow-x:auto">
<table>
<thead><tr>{header}</tr></thead>
<tbody>{body}</tbody>
</table>
</div>"""

def _section_html(scan_key: str, rows: list, id_prefix: str = "") -> str:
    label = _LABEL_MAP.get(scan_key, scan_key)
    count = len([r for r in rows if "_error" not in r])

    # Determine which columns actually have data
    present = set()
    for r in rows[:5]:
        present.update(k for k, v in r.items() if v is not None and k not in ("tv_symbol","_error"))

    # Build ordered column labels for header
    hdr = ["Symbol"]
    disp_fields = ["close", "change"]
    if "RSI" in present:         hdr.append("RSI");       disp_fields.append("RSI")
    if "ADX" in present:         hdr.append("ADX");       disp_fields.append("ADX")
    if "SMA200" in present:      hdr.append("SMA200");    disp_fields.append("SMA200")
    if "relative_volume_10d_calc" in present: hdr.append("RelVol"); disp_fields.append("rel_vol")
    if "recommendation_mark" in present: hdr.append("Analyst"); disp_fields.append("rec")
    if "ATR" in present:         hdr.append("ATR");       disp_fields.append("ATR")
    if "Volatility.D" in present: hdr.append("Volatility"); disp_fields.append("volat")
    if "volume" in present:      hdr.append("Volume");    disp_fields.append("volume")
    if "subtype" in present:     hdr.append("Subtype");   disp_fields.append("subtype")
    hdr += ["Price", "Chg%"]
    # Re-order: symbol, price, chg first then tech
    hdr_final = ["Symbol", "Price", "Chg%"] + hdr[1:-2]

    badge_color = "#1e88e5" if count > 0 else "#555"
    section_id = f"{id_prefix}{scan_key}"
    return f"""
<section id="{section_id}">
  <h2>{label} <span style="background:{badge_color};border-radius:10px;padding:2px 10px;font-size:0.75em">{count}</span></h2>
  {_rows_html(rows, hdr_final)}
</section>"""


def build_html(results: dict, args,
               etf_rows: list | None = None,
               india_data: dict | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "🐻 BEARISH" if args.bearish else "🐂 BULLISH"
    filters_desc = (f"RSI {args.rsi_min}–{args.rsi_max} | ADX &gt; {args.adx_min} | "
                    f"Close {'&lt;' if args.bearish else '&gt;'} SMA200")

    tab_btns = ""
    tab_contents = ""
    all_keys = list(results.keys())

    # "All" tab content = all sections stacked + extras at top
    all_sections = ""
    # India internals + leveraged ETFs appear at top of "All" tab
    if india_data is not None:
        all_sections += _build_india_internals_html(india_data, id_prefix="all-")
    if etf_rows is not None:
        all_sections += _build_leveraged_etf_html(etf_rows, id_prefix="all-")
    for key in all_keys:
        all_sections += _section_html(key, results[key], id_prefix="all-")

    tab_btns += '<button type="button" class="tab-btn active" onclick="showTab(\'all\', this)">All</button>\n'
    tab_contents += f'<div class="tab-pane active" id="tab-all">{all_sections}</div>\n'

    # Extra tabs for leveraged ETFs and India internals
    if etf_rows is not None:
        tab_btns += '<button type="button" class="tab-btn" onclick="showTab(\'leveraged_etfs\', this)">⚡ Lev ETFs</button>\n'
        tab_contents += f'<div class="tab-pane" id="tab-leveraged_etfs">{_build_leveraged_etf_html(etf_rows)}</div>\n'
    if india_data is not None:
        tab_btns += '<button type="button" class="tab-btn" onclick="showTab(\'india_internals\', this)">🇮🇳 NSE F&amp;O</button>\n'
        tab_contents += f'<div class="tab-pane" id="tab-india_internals">{_build_india_internals_html(india_data)}</div>\n'

    for key in all_keys:
        label = _LABEL_MAP.get(key, key).split(" ", 1)[-1]   # strip emoji prefix
        tab_btns += f'<button type="button" class="tab-btn" onclick="showTab(\'{key}\', this)">{label}</button>\n'
        section = _section_html(key, results[key])
        tab_contents += f'<div class="tab-pane" id="tab-{key}">{section}</div>\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Asset Screener — {now}</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --sub: #8b949e; --accent: #1f6feb;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; padding: 20px; }}
  h1   {{ font-size: 1.4em; margin-bottom: 6px; color: #58a6ff; }}
  .meta {{ color: var(--sub); font-size: 0.85em; margin-bottom: 20px; }}
  .filter-bar {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px;
                  padding: 10px 16px; margin-bottom: 18px; font-size: 0.85em; color: var(--sub); }}
  .filter-bar span {{ color: var(--text); font-weight: 600; }}

  /* Tabs */
  .tab-nav {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 18px; }}
  .tab-btn {{ background: var(--card); border: 1px solid var(--border); color: var(--sub);
               padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 0.85em;
               transition: all 0.15s; }}
  .tab-btn:hover  {{ background: #21262d; color: var(--text); }}
  .tab-btn.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}

  /* Sections */
  section {{ background: var(--card); border: 1px solid var(--border);
              border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
  h2 {{ font-size: 1.1em; margin-bottom: 14px; }}

  /* Tables */
  table  {{ width: 100%; border-collapse: collapse; font-size: 0.83em; white-space: nowrap; }}
  th     {{ text-align: left; padding: 8px 10px; color: var(--sub); border-bottom: 1px solid var(--border);
             font-weight: 500; position: sticky; top: 0; background: var(--card); }}
  td     {{ padding: 7px 10px; border-bottom: 1px solid #21262d; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,0.03); }}
  small  {{ font-size: 0.85em; }}
  a      {{ color: #58a6ff; }}
</style>
</head>
<body>
<header>
<h1>Multi-Asset Screener&nbsp;&nbsp;<small style="font-size:0.6em;color:var(--sub)">{mode}</small></h1>
<p class="meta">Generated: {now}</p>
</header>
<main>
<div class="filter-bar">
  Filters: <span>{filters_desc}</span>
</div>

<nav class="tab-nav">
{tab_btns}
</nav>

{tab_contents}

<script>
function showTab(key, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + key).classList.add('active');
  btn.classList.add('active');
}}
</script>
</main>
</body>
</html>"""


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Multi-asset TV Screener — WIN-equivalent technical scanner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rsi-min",       type=float, default=50.0,
                        help="Minimum RSI for bullish filter")
    parser.add_argument("--rsi-max",       type=float, default=75.0,
                        help="Maximum RSI for bullish filter")
    parser.add_argument("--adx-min",       type=float, default=20.0,
                        help="Minimum ADX (trend strength)")
    parser.add_argument("--max-per-class", type=int,   default=None,
                        help="Maximum candidates selected per class for scoring (default: 20)")
    parser.add_argument("--fetch-limit", type=int, default=None,
                        help="Discovery rows per class, independent of scoring limit; defaults to SIGNAL_SCANNER_FETCH_LIMIT or 600")
    parser.add_argument("--profile", choices=sorted(MARKET_CAP_FLOORS), default=None,
                        help="Universe profile; defaults to SIGNAL_SCANNER_PROFILE or broad")
    parser.add_argument("--bearish",       action="store_true",
                        help="Flip filter: RSI < 50, close < SMA200 (bearish mode)")
    parser.add_argument("--min-market-cap", dest="min_market_cap", type=float,
                        default=None,
                        help="Override the profile's market-cap floor in the listing's local currency.")
    parser.add_argument("--min-volume",    dest="min_volume", type=float,
                        default=100_000,
                        help="Minimum average daily volume for stock scans. "
                             "Default 100 000 — filters out illiquid/thinly traded stocks.")
    parser.add_argument("--class",         dest="scan_class", nargs="*",
                        choices=[d[0] for d in SCAN_DEFS],
                        help="Run only these asset classes (default: all)")
    parser.add_argument("--append-fires",  action="store_true",
                        help="Append stock screener signals to fires_raw.json")
    parser.add_argument("--leveraged-etfs", dest="leveraged_etfs", action="store_true",
                        help="Include leveraged ETF watchlist section (uses yfinance)")
    parser.add_argument("--india-internals", dest="india_internals", action="store_true",
                        help="Include India PCR + OI Buildup section (uses AngelOne SmartAPI)")
    parser.add_argument("--out",           type=str, default=str(OUTPUT_HTML),
                        help="Output HTML report path")

    args = parser.parse_args()

    mode_str = "BEARISH (RSI inverted, close < SMA200)" if args.bearish else "BULLISH"
    active_profile = _profile_name(args.profile)
    active_fetch_limit = _fetch_limit(args.fetch_limit)
    cap_label = ", ".join(
        f"{key.removeprefix('stocks_').upper()} {MARKET_CAP_CURRENCY[key]} {value:g}"
        for key, value in MARKET_CAP_FLOORS[active_profile].items()
    )
    print(f"\n{'='*60}")
    print(f"  Multi-Asset Screener  |  Mode: {mode_str}")
    print(f"  RSI [{args.rsi_min}–{args.rsi_max}]  ADX > {args.adx_min}")
    print(f"  Profile: {active_profile} | Fetch limit: {active_fetch_limit} | Score cap: {args.max_per_class or DEFAULT_SCORE_LIMIT}")
    print(f"  Market-cap floors (local currency): {cap_label} | Min avg vol: {args.min_volume:,.0f}")
    print(f"{'='*60}\n")

    t0 = time.time()
    results = run_all_scans(args)

    total = sum(len(v) for v in results.values())
    print(f"\n✅  Total signals: {total}  ({time.time()-t0:.1f}s)")

    # ── Top-5 stock signals ────────────────────────────────────────────────────
    _STOCK_KEYS = {"stocks_us", "stocks_au", "stocks_in", "stocks_hk", "stocks_sg", "stocks_jp", "stocks_kr"}
    _MARKET_LABELS = {
        "stocks_us": "US", "stocks_au": "AU", "stocks_in": "IN",
        "stocks_hk": "HK", "stocks_sg": "SG", "stocks_jp": "JP", "stocks_kr": "KR",
    }
    _all_stock_rows = []
    for _k, _rows in results.items():
        if _k not in _STOCK_KEYS:
            continue
        for _r in _rows:
            if "_error" in _r:
                continue
            _r["_market_label"] = _MARKET_LABELS.get(_k, _k.upper())
            _all_stock_rows.append(_r)
    if _all_stock_rows:
        # Exclude liquid/money-market ETFs and flat instruments (no real signal)
        # Both NSE: and BSE: prefixes are included to catch all TV symbol variants
        _LIQUID_BLACKLIST = {
            "NSE:LIQUIDSBI",  "BSE:LIQUIDSBI",
            "NSE:ABSLLIQUID", "BSE:ABSLLIQUID",
            "NSE:LIQUIDBEES", "BSE:LIQUIDBEES",
            "NSE:LIQUIDCASE", "BSE:LIQUIDCASE",
            "NSE:LIQUIDIETF", "BSE:LIQUIDIETF",
            "NSE:NIFTYBEES",  "BSE:NIFTYBEES",
        }
        _all_stock_rows = [
            r for r in _all_stock_rows
            if r.get("tv_symbol") not in _LIQUID_BLACKLIST
            and abs(r.get("change") or 0) >= 0.05
        ]
        _all_stock_rows.sort(key=lambda r: (r.get("ADX") or 0), reverse=True)
        _top5 = _all_stock_rows[:5]
        print(f"\n  TOP 5 STOCK SIGNALS (by ADX):")
        print(f"  {'#':<3}  {'Symbol':<28}  {'ADX':>7}  {'RSI':>7}  {'Chg%':>7}  Market")
        print(f"  {'-'*68}")
        for _i, _r in enumerate(_top5, 1):
            _sym  = _r.get("tv_symbol", "")
            _adx  = _r.get("ADX")
            _rsi  = _r.get("RSI")
            _chg  = _r.get("change")
            _mkt  = _r.get("_market_label", "")
            _adx_s = f"{_adx:>7.1f}" if _adx is not None else f"{'—':>7}"
            _rsi_s = f"{_rsi:>7.1f}" if _rsi is not None else f"{'—':>7}"
            _chg_s = f"{_chg:>+7.1f}%" if _chg is not None else f"{'—':>8}"
            print(f"  {_i:<3}  {_sym:<28}  {_adx_s}  {_rsi_s}  {_chg_s}  {_mkt}")

    # ── Optional: Leveraged ETFs + India Internals ────────────────────────────
    etf_rows  = None
    india_dat = None
    if args.leveraged_etfs:
        print("  📊 Leveraged ETFs (yfinance) …", end=" ", flush=True)
        etf_rows = fetch_leveraged_etfs()
        print(f"{len(etf_rows)} ETFs")
    if args.india_internals:
        print("  🇮🇳 India PCR/OI (AngelOne) …", end=" ", flush=True)
        india_dat = fetch_india_internals()
        pcr_n = len((india_dat or {}).get("pcr", []))
        oi_n  = len((india_dat or {}).get("oi_buildup", []))
        print(f"{pcr_n} PCR + {oi_n} OI Buildup")

    # Build and save HTML
    html = build_html(results, args, etf_rows=etf_rows, india_data=india_dat)
    out_path = Path(args.out)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    print(f"📄 Report saved → {out_path}")

    # Optionally append to fires_raw.json
    if args.append_fires:
        n = append_to_fires(results)
        print(f"🔥 Appended {n} stock signals to {FIRES_FILE}")

    print()


if __name__ == "__main__":
    main()

