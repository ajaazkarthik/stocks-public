"""
Input Sources — Unified Fire Factory for 4-Layer Scoring Pipeline
==================================================================
All four input sources produce identically structured fire dicts that
flow through the same analyse_fire / process_fire_background pipeline.

Sources:
    1. webhook    — TradingView POST /webhook    (handled by webhook_server.py)
    2. download   — Batch import from fires_raw.json (handled by signal_stack.py)
    3. screener   — TV Screener quality/momentum scan  (this module: SOURCE_SCREENER)
    4. manual     — User-supplied ticker list           (this module: SOURCE_MANUAL)

Usage (imported by webhook_server.py):
    from input_sources import manual_to_fires, tv_screener_to_fires

Usage (standalone):
    python input_sources.py --manual "NVDA, oracle, MSFT, NSE:MRF"
    python input_sources.py --screener stocks_us,stocks_in --rsi-min 50 --adx-min 20
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ── Source tag constants ──────────────────────────────────────────────────────
SOURCE_WEBHOOK  = "webhook"
SOURCE_DOWNLOAD = "download"
SOURCE_SCREENER = "screener"
SOURCE_MANUAL   = "manual"
SOURCE_NEWS     = "news"          # TradingView news-driven signals
SOURCE_HOLDINGS = "holdings"      # Live broker positions (option 1 cache)
SOURCE_MOOMOO_INSIGHTS = "moomoo_insights"  # Moomoo Insights official profile feed

# ── Directory refs ────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent

# ── TV News Screener import (optional — requires vaderSentiment + cookies) ────
try:
    from tv_news_screener import (
        news_to_fires  as news_to_fires,
        news_scan_meta as news_scan_meta,
    )
    _NEWS_SCREENER_OK = True
except Exception:
    _NEWS_SCREENER_OK = False

    def news_to_fires(*a, **kw):  # type: ignore[misc]
        """Stub — tv_news_screener not available."""
        return [], []

    def news_scan_meta(*a, **kw):  # type: ignore[misc]
        """Stub — tv_news_screener not available."""
        return {"error": "tv_news_screener not installed"}

# ── Moomoo Insights import (optional — requires playwright) ───────────────────
_ROOT_DIR = _THIS_DIR.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
try:
    from moomoo_community_intel import (
        insights_to_fires    as insights_to_fires,
        get_insights_headlines as get_insights_headlines,
        load_insights_cache  as load_insights_cache,
    )
    _INSIGHTS_OK = True
except Exception:
    _INSIGHTS_OK = False

    def insights_to_fires(*a, **kw):  # type: ignore[misc]
        """Stub — moomoo_community_intel not available."""
        return [], []

    def get_insights_headlines(*a, **kw):  # type: ignore[misc]
        """Stub — moomoo_community_intel not available."""
        return []

    def load_insights_cache(*a, **kw):  # type: ignore[misc]
        """Stub — moomoo_community_intel not available."""
        return []

# ── Multi-asset screener import (optional — falls back to raw TV scan) ─────────
try:
    _screeners_dir = _THIS_DIR / "screeners"
    if str(_screeners_dir) not in sys.path:
        sys.path.insert(0, str(_screeners_dir))
    from multi_asset_screener import (
        run_all_scans as _run_all_scans,
        SCAN_DEFS     as _SCAN_DEFS,
    )
    _SCREENER_OK = True
except Exception:
    _SCREENER_OK = False

# ─────────────────────────────────────────────────────────────────────────────
# Common name → canonical TV pro_symbol
# Add or extend as needed.  Lower-case keys; values must be EXCHANGE:TICKER.
# ─────────────────────────────────────────────────────────────────────────────
TICKER_ALIASES: dict[str, str] = {
    # US Tech
    "oracle":           "NYSE:ORCL",
    "nvidia":           "NASDAQ:NVDA",
    "microsoft":        "NASDAQ:MSFT",
    "apple":            "NASDAQ:AAPL",
    "google":           "NASDAQ:GOOGL",
    "alphabet":         "NASDAQ:GOOGL",
    "meta":             "NASDAQ:META",
    "facebook":         "NASDAQ:META",
    "amazon":           "NASDAQ:AMZN",
    "netflix":          "NASDAQ:NFLX",
    "tesla":            "NASDAQ:TSLA",
    "amd":              "NASDAQ:AMD",
    "intel":            "NASDAQ:INTC",
    "qualcomm":         "NASDAQ:QCOM",
    "broadcom":         "NASDAQ:AVGO",
    "salesforce":       "NYSE:CRM",
    "palantir":         "NYSE:PLTR",
    "snowflake":        "NYSE:SNOW",
    "shopify":          "NYSE:SHOP",
    "uber":             "NYSE:UBER",
    "airbnb":           "NASDAQ:ABNB",
    "spotify":          "NYSE:SPOT",
    "zoom":             "NASDAQ:ZM",
    "coinbase":         "NASDAQ:COIN",
    "robinhood":        "NASDAQ:HOOD",
    # US Finance
    "jpmorgan":         "NYSE:JPM",
    "jp morgan":        "NYSE:JPM",
    "bank of america":  "NYSE:BAC",
    "goldman sachs":    "NYSE:GS",
    "goldman":          "NYSE:GS",
    "morgan stanley":   "NYSE:MS",
    "berkshire":        "NYSE:BRK.B",
    "visa":             "NYSE:V",
    "mastercard":       "NYSE:MA",
    "american express": "NYSE:AXP",
    "amex":             "NYSE:AXP",
    # US Energy / Industrial
    "exxon":            "NYSE:XOM",
    "chevron":          "NYSE:CVX",
    # India (NSE)
    "reliance":         "NSE:RELIANCE",
    "tcs":              "NSE:TCS",
    "infosys":          "NSE:INFY",
    "wipro":            "NSE:WIPRO",
    "hdfc bank":        "NSE:HDFCBANK",
    "hdfc":             "NSE:HDFCBANK",
    "icici bank":       "NSE:ICICIBANK",
    "axis bank":        "NSE:AXISBANK",
    "mrf":              "NSE:MRF",
    "bajaj finserv":    "NSE:BAJAJFINSV",
    "bajaj finance":    "NSE:BAJFINANCE",
    "maruti":           "NSE:MARUTI",
    "tata motors":      "NSE:TATAMOTORS",
    "tata steel":       "NSE:TATASTEEL",
    "asian paints":     "NSE:ASIANPAINT",
    "sun pharma":       "NSE:SUNPHARMA",
    "dr reddy":         "NSE:DRREDDY",
    "cipla":            "NSE:CIPLA",
    "larsen":           "NSE:LT",
    "l&t":              "NSE:LT",
    "itc":              "NSE:ITC",
    "nestle india":     "NSE:NESTLEIND",
    "adani ports":      "NSE:ADANIPORTS",
    # AU (ASX)
    "bhp":              "ASX:BHP",
    "csl":              "ASX:CSL",
    "commonwealth bank": "ASX:CBA",
    "westpac":          "ASX:WBC",
    "anz":              "ASX:ANZ",
    "nab":              "ASX:NAB",
    "rio tinto":        "ASX:RIO",
    "fortescue":        "ASX:FMG",
    "macquarie":        "ASX:MQG",
}

# ── Known exchanges for bare-ticker lookup fallback ──────────────────────────
_SAFE_SYMBOL = re.compile(r'^[A-Z0-9:._\-!]{1,30}$', re.IGNORECASE)

#: TradingView's symbol-search API reports human-readable exchange names, but the
#: scanner only accepts its own prefixes.  Everything listed on NYSE Arca or NYSE
#: American is "AMEX" to the scanner, so an unmapped "NYSE ARCA:XLV" returns no
#: data at all — which silently becomes a neutral fallback score downstream.
_TV_EXCHANGE_DISPLAY_TO_PREFIX: dict[str, str] = {
    "NYSE ARCA":     "AMEX",
    "ARCA":          "AMEX",
    "NYSE AMERICAN": "AMEX",
}

#: Scanner prefixes that make up the US market.
US_TV_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "AMEX", "CBOE", "OTC"})


def _normalise_tv_exchange(exchange: str) -> str:
    """Map a TradingView display exchange name onto its scanner prefix."""
    name = exchange.strip().upper()
    return _TV_EXCHANGE_DISPLAY_TO_PREFIX.get(name, name)


# ─────────────────────────────────────────────────────────────────────────────
# Result type for validation
# ─────────────────────────────────────────────────────────────────────────────
class ValidationResult(NamedTuple):
    pro_symbol: str   # empty string on failure
    error: str        # empty string on success


# ─────────────────────────────────────────────────────────────────────────────
# Exchange resolver (standalone — no import from webhook_server to avoid circular)
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_exchange_via_tv_search(
    bare_tick: str,
    allowed_exchanges: frozenset[str] | set[str] | None = None,
) -> str:
    """
    Use TV public symbol-search API to find the primary exchange for a bare ticker.
    Returns a scanner exchange prefix (e.g. 'NYSE', 'AMEX') or '' if not found.
    No authentication required.

    ``allowed_exchanges`` restricts the answer to a known market.  The API ranks
    every global listing of a ticker together, so an unconstrained search can
    match a foreign company that merely shares the ticker (US "SPAL" vs Indian
    "NSE:SPAL").  Pass the set when the caller already knows the region.
    """
    try:
        params = urllib.parse.urlencode({
            "text":        bare_tick,
            "hl":          "false",
            "exchange":    "",
            "lang":        "en",
            "search_type": "undefined",
            "domain":      "production",
        })
        url = f"https://symbol-search.tradingview.com/symbol_search/v3/?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer":    "https://www.tradingview.com/",
            "Origin":     "https://www.tradingview.com",
            "Accept":     "application/json, text/javascript, */*; q=0.01",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            hits = json.loads(resp.read().decode("utf-8"))
        items = hits if isinstance(hits, list) else hits.get("symbols", [])
        for item in items:
            sym = item.get("symbol", "").upper()
            if sym != bare_tick.upper():
                continue
            exch = _normalise_tv_exchange(item.get("exchange", ""))
            if not exch:
                continue
            if allowed_exchanges and exch not in allowed_exchanges:
                continue
            return exch
    except Exception:
        pass
    return ""


def _resolve_exchange_via_fires_cache(bare_tick: str) -> str:
    """
    Look up a bare ticker in fires_raw.json to find a previously-resolved exchange.
    """
    fires_file = _THIS_DIR / "fires_raw.json"
    if not fires_file.exists():
        return ""
    try:
        data = json.loads(fires_file.read_text(encoding="utf-8"))
        for fire in data.get("all", []):
            ps = fire.get("pro_symbol", "")
            if ":" in ps:
                exch, tick = ps.split(":", 1)
                if tick.upper() == bare_tick.upper():
                    return _normalise_tv_exchange(exch)
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Ticker validation & normalisation
# ─────────────────────────────────────────────────────────────────────────────
def validate_and_normalise(
    raw: str,
    allowed_exchanges: frozenset[str] | set[str] | None = None,
) -> ValidationResult:
    """
    Validate and normalise a raw ticker string to a canonical EXCHANGE:TICKER.

    Accepts:
        "NVDA"             — bare ticker (will attempt exchange resolution)
        "NASDAQ:NVDA"      — already qualified (validated and returned)
        "oracle"           — company name (looked up in TICKER_ALIASES)
        "nvidia"           — company name
        "NSE:MRF"          — Indian stock (valid as-is)

    ``allowed_exchanges`` restricts bare-ticker resolution to a known market, so
    a US ticker cannot be answered with a same-named foreign listing.

    Returns ValidationResult(pro_symbol, error).
    On success: pro_symbol is set, error is empty.
    On failure: pro_symbol is empty, error describes the problem.
    """
    if not isinstance(raw, str):
        return ValidationResult("", f"expected string, got {type(raw).__name__}")

    cleaned = raw.strip()
    if not cleaned:
        return ValidationResult("", "empty ticker")

    # 1. Company name alias lookup (case-insensitive)
    alias_key = cleaned.lower()
    if alias_key in TICKER_ALIASES:
        pro_sym = TICKER_ALIASES[alias_key].upper()
        return ValidationResult(pro_sym, "")

    # 2. Already EXCHANGE:TICKER format
    if ":" in cleaned:
        candidate = cleaned.upper()
        if not _SAFE_SYMBOL.match(candidate):
            return ValidationResult("", f"invalid characters in symbol: {cleaned!r}")
        exch, tick = candidate.split(":", 1)
        if not exch or not tick:
            return ValidationResult("", f"malformed EXCHANGE:TICKER: {cleaned!r}")
        return ValidationResult(candidate, "")

    # 3. Bare ticker — attempt exchange resolution
    bare = cleaned.upper()
    if not re.match(r'^[A-Z0-9._\-!]{1,20}$', bare):
        return ValidationResult("", f"invalid ticker format: {cleaned!r}")

    # 3a. Try fires_raw.json cache first (instant, no network)
    exch = _resolve_exchange_via_fires_cache(bare)
    if exch and allowed_exchanges and exch not in allowed_exchanges:
        exch = ""

    # 3b. TV symbol-search API (network call ~0.5s)
    if not exch:
        exch = _resolve_exchange_via_tv_search(bare, allowed_exchanges)

    if not exch:
        return ValidationResult(
            "",
            f"could not resolve exchange for bare ticker {bare!r} — "
            "provide as EXCHANGE:TICKER (e.g. NASDAQ:NVDA) or add to TICKER_ALIASES",
        )
    return ValidationResult(f"{exch}:{bare}", "")


# ─────────────────────────────────────────────────────────────────────────────
# Fire dict factory
# ─────────────────────────────────────────────────────────────────────────────
def _make_fire(pro_symbol: str, source: str,
               message: str = "MANUAL BUY:1",
               resolution: str = "D",
               close: float | None = None) -> dict:
    """Build a fire dict compatible with analyse_fire / process_fire_background."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "fire_id":         int(time.time() * 1000),
        "alert_id":        0,
        "symbol":          pro_symbol,
        "pro_symbol":      pro_symbol,
        "resolution":      resolution,
        "sound_file":      None,
        "sound_duration":  0,
        "popup":           False,
        "cross_interval":  False,
        "message":         message,
        "fire_time":       now_iso,
        "bar_time":        now_iso,
        "name":            f"{source}_{pro_symbol}",
        "kinds":           [source],
        "close":           close,
        "_source":         source,       # consumed by process_fire_background
    }


# ─────────────────────────────────────────────────────────────────────────────
# Manual input → fires
# ─────────────────────────────────────────────────────────────────────────────
def manual_to_fires(
    raw_tickers: list[str] | str,
    resolve_exchange: bool = True,
) -> tuple[list[dict], dict[str, str]]:
    """
    Convert a list of raw ticker strings (or a comma-separated string) to fire dicts.

    Parameters
    ----------
    raw_tickers : list[str] | str
        Ticker strings: bare ("NVDA"), qualified ("NASDAQ:NVDA"), or
        company names ("oracle", "nvidia").  A single comma/space/newline-
        separated string is also accepted.
    resolve_exchange : bool
        When True (default), attempt TV symbol-search for bare tickers.
        Set to False in unit tests or when network is unavailable.

    Returns
    -------
    (fires, errors)
        fires  — list of fire dicts ready for the pipeline (source='manual')
        errors — {raw_input: error_message} for all inputs that failed
    """
    # Normalise input to list
    if isinstance(raw_tickers, str):
        raw_tickers = [t.strip() for t in re.split(r'[,\n;|]+', raw_tickers) if t.strip()]

    if not raw_tickers:
        return [], {}

    fires:  list[dict]     = []
    errors: dict[str, str] = {}
    seen:   set[str]       = set()   # dedup by pro_symbol

    for raw in raw_tickers:
        raw = raw.strip()
        if not raw:
            continue

        vr = validate_and_normalise(raw)
        if vr.error:
            if not resolve_exchange and "bare ticker" in vr.error:
                # In no-network mode: try to treat bare-ticker as UNKNOWN:TICKER
                # so we can still report the error clearly
                errors[raw] = vr.error
            else:
                errors[raw] = vr.error
            continue

        pro_sym = vr.pro_symbol.upper()
        if pro_sym in seen:
            continue   # silent dedup
        seen.add(pro_sym)

        fires.append(_make_fire(
            pro_symbol=pro_sym,
            source=SOURCE_MANUAL,
            message=f"MANUAL BUY:1",  # no win-rate data for manual entries
        ))

    return fires, errors


# ─────────────────────────────────────────────────────────────────────────────
# Live holdings → fires
# ─────────────────────────────────────────────────────────────────────────────
def holdings_to_fires(
    positions_file: str | Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Convert live broker holdings into fire dicts for the scoring pipeline.

    Reads the positions cache written by ``global_portfolio_dashboard.py``
    (menu option 1) and maps every broker symbol onto a TradingView symbol.
    Leveraged wrappers are scored via their underlying equity; index and sector
    funds keep their own symbol.  Anything that cannot be mapped is returned as
    an exclusion rather than being scored on neutral fallback data.

    Returns
    -------
    (fires, excluded)
        fires    — fire dicts ready for the pipeline (source='holdings')
        excluded — [{symbol, broker, reason}] for positions that were skipped
    """
    from holdings_resolver import Outcome, load_positions, resolve_holdings

    positions = load_positions(positions_file)
    if not positions:
        return [], []

    fires:    list[dict] = []
    excluded: list[dict] = []

    for res in resolve_holdings(positions):
        if not res.scoreable:
            excluded.append({
                "symbol": res.source_symbol,
                "broker": res.broker,
                "reason": res.detail or "unresolved",
            })
            continue
        fire = _make_fire(
            pro_symbol=res.pro_symbol,
            source=SOURCE_HOLDINGS,
            message="HOLDING BUY:1",   # no strategy trigger — this is a held position
        )
        fire["_holding_source_symbol"] = res.source_symbol
        fire["_holding_broker"]        = res.broker
        fire["_holding_outcome"]       = res.outcome.value
        if res.outcome is Outcome.UNDERLYING:
            fire["_holding_detail"] = res.detail
        fires.append(fire)

    return fires, excluded


# ─────────────────────────────────────────────────────────────────────────────
# TV Screener → fires
# ─────────────────────────────────────────────────────────────────────────────
def tv_screener_to_fires(
    classes:       list[str] | None = None,
    rsi_min:       float = 50.0,
    rsi_max:       float = 75.0,
    adx_min:       float = 20.0,
    max_per_class: int | None = None,
    bearish:       bool = False,
    fetch_limit:   int | None = None,
    profile:       str | None = None,
) -> tuple[list[dict], dict]:
    """
    Run the TradingView screener and return qualifying stocks as fire dicts.

    Parameters
    ----------
    classes : list[str] | None
        Asset classes to scan (e.g. ["stocks_us", "stocks_in"]).
        None → all classes defined in multi_asset_screener.SCAN_DEFS.
    rsi_min, rsi_max : float
        RSI range for the technical filter.
    adx_min : float
        Minimum ADX (trend strength).
    max_per_class : int | None
        Override max results per class.
    bearish : bool
        Flip to bearish mode (RSI < 50, close < SMA200).
    fetch_limit : int | None
        Maximum discovery rows per class, independent from max_per_class. Defaults
        to SIGNAL_SCANNER_FETCH_LIMIT (600).
    profile : str | None
        Discovery universe profile (broad or large_cap); defaults to
        SIGNAL_SCANNER_PROFILE (broad).

    Returns
    -------
    (fires, meta)
        fires — list of fire dicts (source='screener')
        meta  — {"classes_scanned": [...], "total_signals": N, "per_class": {...}}
    """
    if not _SCREENER_OK:
        raise RuntimeError(
            "multi_asset_screener not importable — ensure tradingview/screeners/ is on sys.path"
        )

    # Build a minimal args namespace for run_all_scans
    import argparse
    valid_classes = {d[0] for d in _SCAN_DEFS}

    if classes:
        invalid = [c for c in classes if c not in valid_classes]
        if invalid:
            raise ValueError(
                f"Unknown screener class(es): {invalid}. "
                f"Valid: {sorted(valid_classes)}"
            )

    args = argparse.Namespace(
        rsi_min       = float(rsi_min),
        rsi_max       = float(rsi_max),
        adx_min       = float(adx_min),
        max_per_class = max_per_class,
        bearish       = bearish,
        scan_class    = classes,  # None → all
        fetch_limit   = fetch_limit,
        profile       = profile,
        min_market_cap = None,
        min_volume    = 100_000,
    )

    raw_results = _run_all_scans(args)
    scan_errors = {
        key: str(rows[0].get("_error") or "scan failed")
        for key, rows in raw_results.items()
        if rows and isinstance(rows[0], dict) and rows[0].get("_error")
    }
    if scan_errors:
        raise RuntimeError("TradingView screener failed: " + "; ".join(
            f"{key}: {message}" for key, message in sorted(scan_errors.items())
        ))

    # Stock asset classes that support the full signal-stack (all 4 layers)
    FULL_STACK_CLASSES = {
        "stocks_us", "stocks_au", "stocks_in",
        "stocks_hk", "stocks_sg", "stocks_jp", "stocks_kr",
    }
    # Non-stock classes get a lighter message (no fundamental data available)
    OTHER_CLASSES = {"crypto", "forex", "futures", "bonds"}

    fires: list[dict] = []
    seen:  set[str]   = set()
    per_class: dict[str, int] = {}

    for key, rows in raw_results.items():
        count = 0
        is_stock = key in FULL_STACK_CLASSES
        for r in rows:
            if "_error" in r:
                continue
            sym = r.get("tv_symbol") or r.get("name", "")
            if not sym or ":" not in sym:
                continue
            sym = sym.upper()
            if sym in seen:
                continue
            seen.add(sym)

            rsi     = r.get("RSI")
            adx_val = r.get("ADX")
            rel_vol = r.get("relative_volume_10d_calc")
            close   = r.get("close")
            rec     = r.get("recommendation_mark")

            # Build a parse_fire_meta-compatible message
            # so L1 technical scoring gets correct inputs
            if is_stock:
                parts = ["SCREENER BUY:1"]
            else:
                parts = [f"SCREENER_{key.upper().replace('_', '-')} BUY:1"]

            if rsi is not None:
                parts.append(f"rsi:{rsi:.1f}")
            if adx_val is not None:
                parts.append(f"ADX:{adx_val:.1f}")
            if rel_vol is not None:
                parts.append(f"relVol:{rel_vol:.2f}")
            if rec is not None:
                try:
                    parts.append(f"rec:{float(rec):.2f}")
                except (TypeError, ValueError):
                    parts.append(f"recommendation:{rec}")

            msg = ", ".join(parts)

            fire = _make_fire(
                pro_symbol=sym,
                source=SOURCE_SCREENER,
                message=msg,
                resolution="D",
                close=close,
            )
            # Carry raw screener fields so analyse_fire can use them directly
            fire["_screener_data"] = {k: r.get(k) for k in r if k != "tv_symbol"}
            fire["_screener_class"] = key
            fires.append(fire)
            count += 1

        per_class[key] = count

    meta = {
        "classes_scanned": list(raw_results.keys()),
        "total_signals":   len(fires),
        "per_class":       per_class,
        "rsi_range":       [rsi_min, rsi_max],
        "adx_min":         adx_min,
        "bearish":         bearish,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "coverage":        getattr(args, "_scan_diagnostics", {}),
    }
    return fires, meta


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point (for quick standalone testing)
# ─────────────────────────────────────────────────────────────────────────────
def _cli() -> None:  # noqa: C901
    import argparse

    parser = argparse.ArgumentParser(
        description="Input Sources — test manual and screener fire factories"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--manual", "-m",
        metavar="TICKERS",
        help='Comma-separated tickers to validate, e.g. "NVDA, oracle, NSE:MRF"',
    )
    g.add_argument(
        "--screener", "-s",
        metavar="CLASSES",
        help='Comma-separated screener classes, e.g. "stocks_us,stocks_in"',
    )
    parser.add_argument("--rsi-min",       type=float, default=50.0)
    parser.add_argument("--rsi-max",       type=float, default=75.0)
    parser.add_argument("--adx-min",       type=float, default=20.0)
    parser.add_argument("--max-per-class", type=int,   default=None)
    parser.add_argument("--bearish",       action="store_true")
    parser.add_argument("--no-resolve",    action="store_true",
                        help="Skip TV symbol-search for bare tickers")
    args = parser.parse_args()

    if args.manual:
        print(f"\n=== Manual Input: {args.manual!r} ===\n")
        fires, errors = manual_to_fires(
            args.manual, resolve_exchange=not args.no_resolve
        )
        for f in fires:
            print(f"  OK   {f['pro_symbol']!s:30s}  source={f['_source']}  msg={f['message']}")
        for raw, err in errors.items():
            print(f"  ERR  {raw!r:30s}  → {err}")
        print(f"\n  {len(fires)} fire(s) OK, {len(errors)} error(s)")
    else:
        classes_list = [c.strip() for c in args.screener.split(",") if c.strip()] or None
        print(f"\n=== TV Screener: {classes_list or 'all'} ===\n")
        fires, meta = tv_screener_to_fires(
            classes       = classes_list,
            rsi_min       = args.rsi_min,
            rsi_max       = args.rsi_max,
            adx_min       = args.adx_min,
            max_per_class = args.max_per_class,
            bearish       = args.bearish,
        )
        for f in fires:
            close_str = f"close={f['close']:.2f}" if f.get("close") else ""
            print(f"  {f['pro_symbol']!s:30s}  cls={f['_screener_class']!s:15s}  {close_str}")
        print(f"\nMeta: {meta}")
        print(f"\n  {len(fires)} fire(s) ready for pipeline")


if __name__ == "__main__":
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    _cli()
