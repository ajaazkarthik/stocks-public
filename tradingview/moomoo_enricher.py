"""
Moomoo Enricher — Layer 5 "Institutional Flow" for Signal Stack
================================================================
Fetches real-time Moomoo data and produces an L5 score (0–10) per symbol.

What it measures (Ray Dalio / institutional lens):
  • Capital Distribution  — Super-large & large order net flow (smart money)
  • Capital Flow          — Intraday main-in flow direction
  • Market Snapshot       — Turnover rate, bid/ask pressure, pe/pb
  • Options PCR           — Put/call ratio from Moomoo's option chain (US only)

Score Breakdown (L5, 0–10):
  A) Smart Money Net    (40%): (super_lg_buy - super_lg_sell) / total_flow sign + magnitude
  B) Intraday Flow Dir  (25%): Latest capital_flow_item net direction
  C) Bid/Ask Pressure   (20%): bid_ask_ratio vs 1.0
  D) Turnover Activity  (15%): Turnover rate vs market average (>3% = active)

Supported markets (Moomoo OpenD):
  US, HK, SH, SZ
  ASX / NSE / SGX: returns None (unsupported) — signal_stack falls back gracefully

Requirements:
  pip install moomoo-api
  Moomoo OpenD must be running on 127.0.0.1:11111

Usage (standalone test):
  python moomoo_enricher.py --symbols NYSE:RCL NYSE:BX HK.00700
"""

import sys, os, json, time, traceback, argparse, threading
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Structured logging ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from path_config import ensure_paths, ROOT
ensure_paths(ROOT / "tradingview")

try:
    from log_config import get_logger as _get_logger
    _log = _get_logger(__name__)
except Exception:
    import logging as _logging
    _log = _logging.getLogger(__name__)

# ─── Moomoo symbol conversion ──────────────────────────────────────────────────
# TradingView exchange prefix → Moomoo market prefix
TV_TO_MM_MARKET = {
    "NYSE":   "US",
    "NASDAQ": "US",
    "AMEX":   "US",
    "BATS":   "US",
    "HKEX":   "HK",
    "SH":     "SH",
    "SZ":     "SZ",
}

# Markets NOT supported by Moomoo — return None gracefully
UNSUPPORTED = {"ASX", "ASX_DLY", "NSE", "BSE", "SGX", "LSE", "TSX"}


def tv_to_moomoo(pro_symbol: str) -> Optional[str]:
    """
    Convert TradingView pro_symbol to Moomoo ticker code.
    
    Examples:
      NYSE:RCL   → US.RCL
      HKEX:00700 → HK.00700
      ASX:MIN    → None  (unsupported)
    """
    if ":" not in pro_symbol:
        return None
    exchange, ticker = pro_symbol.split(":", 1)
    exchange = exchange.upper()
    
    if exchange in UNSUPPORTED:
        return None
    
    market = TV_TO_MM_MARKET.get(exchange)
    if not market:
        return None
    
    # HK: zero-pad to 5 digits
    if market == "HK" and ticker.isdigit():
        ticker = ticker.zfill(5)
    
    return f"{market}.{ticker}"


# ─── Moomoo data fetchers ──────────────────────────────────────────────────────

# Rights cache: tracks which markets have quote rights in this process session.
# First call per market probes the API; subsequent calls skip if no rights.
# Maps market prefix ("US", "HK", "SH", "SZ") → True (rights OK) | False (no rights)
_RIGHTS_CACHE: dict[str, bool] = {}


def _market_prefix(mm_code: str) -> str:
    return mm_code.split(".")[0] if "." in mm_code else "UNKNOWN"


def _has_rights(market: str) -> bool:
    """Return False if we already know this market has no quote rights."""
    return _RIGHTS_CACHE.get(market, True)   # assume OK until proven otherwise


def _record_rights(market: str, api_ret: int, api_msg: str) -> bool:
    """
    Inspect a Moomoo API return code/message and cache rights status.
    Returns True if the call succeeded, False if rights error.
    """
    if api_ret == 0:
        _RIGHTS_CACHE[market] = True
        return True
    msg = str(api_msg).lower()
    if "no right" in msg or "no quote right" in msg or "quote right" in msg:
        _RIGHTS_CACHE[market] = False
        return False
    return False   # other error — not a rights error, but still a failure


def _get_context():
    """Create and return an OpenQuoteContext. Caller must close it.

    Auto-starts OpenD via start_opend.ps1 if not running.
    """
    import sys as _sys, os as _os
    _root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), ".."))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from moomoo import OpenQuoteContext
    from moomoo_config import OPEND_HOST, quote_port, ensure_opend_running, is_opend_running
    try:
        port = quote_port()
    except RuntimeError:
        port = 11111   # fallback — will attempt auto-start below
    # Auto-start OpenD if not running (prevents "Moomoo offline" stuck state)
    if not is_opend_running(port):
        _log.warning("OpenD not running on port %s — attempting auto-start…", port)
        if ensure_opend_running(timeout=30):
            _log.info("OpenD started successfully on port %s", port)
            # Re-resolve port — ensure_opend_running may have started on different port
            try:
                port = quote_port()
            except RuntimeError:
                pass  # keep original port
        else:
            _log.error("OpenD failed to start on port %s — Moomoo enrichment will be skipped", port)
    return OpenQuoteContext(host=OPEND_HOST, port=port)


def fetch_capital_distribution(mm_code: str) -> Optional[dict]:
    """
    Returns capital distribution breakdown:
    {
      super_large_buy, super_large_sell, large_buy, large_sell,
      mid_buy, mid_sell, small_buy, small_sell,
      smart_money_net_pct:  (super+large net) / total as -1..+1
    }
    """
    market = _market_prefix(mm_code)
    if not _has_rights(market):
        return None   # fast-skip: already known no rights
    ctx = None
    try:
        ctx = _get_context()
        ret, data = ctx.get_capital_distribution(mm_code)
        if not _record_rights(market, ret, data if ret != 0 else ""):
            return None
        if data is None or (hasattr(data, 'empty') and data.empty):
            _RIGHTS_CACHE[market] = False   # empty result = silent quota restriction
            return None
        row = data.iloc[0]
        
        def safe(col): 
            return float(row[col]) if col in data.columns else 0.0
        
        slb = safe("super_large_buy")
        sls = safe("super_large_sell")
        lb  = safe("large_buy")
        ls  = safe("large_sell")
        mb  = safe("mid_buy")
        ms  = safe("mid_sell")
        sb  = safe("small_buy")
        ss  = safe("small_sell")
        
        total = slb + sls + lb + ls + mb + ms + sb + ss
        smart_net = (slb + lb) - (sls + ls)
        smart_net_pct = smart_net / total if total > 0 else 0.0
        
        return {
            "super_large_buy":   slb,  "super_large_sell": sls,
            "large_buy":         lb,   "large_sell":       ls,
            "mid_buy":           mb,   "mid_sell":         ms,
            "small_buy":         sb,   "small_sell":       ss,
            "smart_money_net_pct": smart_net_pct,
        }
    except Exception:
        return None
    finally:
        if ctx:
            try: ctx.close()
            except Exception:  pass  # cleanup


def fetch_capital_flow_direction(mm_code: str) -> Optional[float]:
    """
    Returns latest intraday capital flow as a normalised direction score (-1..+1).
    Uses the most recent bar's in/out ratio from get_capital_flow (intraday).
    """
    market = _market_prefix(mm_code)
    if not _has_rights(market):
        return None   # fast-skip
    ctx = None
    try:
        ctx = _get_context()
        from moomoo import PeriodType
        ret, data = ctx.get_capital_flow(mm_code, period_type=PeriodType.INTRADAY)
        if not _record_rights(market, ret, data if ret != 0 else ""):
            return None
        if data is None or (hasattr(data, 'empty') and data.empty):
            _RIGHTS_CACHE[market] = False   # empty result = silent quota restriction
            return None
        
        # Last row = most recent period
        row = data.iloc[-1]
        in_flow  = float(row["in_flow"])  if "in_flow"  in data.columns else 0.0
        out_flow = float(row["out_flow"]) if "out_flow" in data.columns else 0.0
        total    = abs(in_flow) + abs(out_flow)
        
        if total == 0:
            return 0.0
        return (in_flow - out_flow) / total   # range: -1..+1
    except Exception:
        return None
    finally:
        if ctx:
            try: ctx.close()
            except Exception:  pass  # cleanup


def fetch_snapshot_data(mm_code: str) -> Optional[dict]:
    """
    Returns lite snapshot fields relevant for scoring:
    {
      turnover_rate: float (%),
      bid_ask_ratio: float (buyers / sellers pressure),
      amplitude: float (% range today),
      pe_ttm: float,
      pb: float,
      change_pct: float  (today's % change)
    }
    Snapshot requires less rights than capital_distribution — HK works, US needs subscription.

    Push cache fast-path: if MOOMOO_USE_PUSH=1 and MoomooQuoteCache has data
    for this symbol, the dict read is near-zero latency vs the ~200–350 ms
    polling call. Falls back to polling if cache miss.
    """
    market = _market_prefix(mm_code)
    if not _has_rights(market):
        return None   # fast-skip

    # ── Push-cache fast-path (opt-in via MOOMOO_USE_PUSH=1) ──────────────────
    cache = MoomooQuoteCache.get_instance()
    if cache is not None:
        cached = cache.get(mm_code)
        if cached is not None:
            # Normalise row dict to snapshot schema
            def _f(col, default=None):
                try:
                    v = cached.get(col)
                    return float(v) if v not in (None, "nan", "N/A", "") else default
                except Exception:
                    return default
            lp = _f("last_price")
            pc = _f("prev_close_price")
            return {
                "turnover_rate": _f("turnover_rate"),
                "bid_ask_ratio": _f("bid_ask_ratio"),
                "amplitude":     _f("amplitude"),
                "pe_ttm":        _f("pe_ttm_ratio"),
                "pb":            _f("pb_ratio"),
                "change_pct":    round((lp - pc) / pc * 100, 3)
                                 if lp is not None and pc and pc > 0 else None,
                "source":        "push_cache",
            }
    # ── Polling fallback ──────────────────────────────────────────────────────
    ctx = None
    try:
        ctx = _get_context()
        ret, data = ctx.get_market_snapshot([mm_code])
        if not _record_rights(market, ret, data if ret != 0 else ""):
            return None
        if data is None or (hasattr(data, 'empty') and data.empty):
            _RIGHTS_CACHE[market] = False   # empty/no-rights response
            return None
        row = data.iloc[0]
        
        def safe(col, default=0.0):
            try:
                return float(row[col]) if col in data.columns else default
            except Exception:
                return default
        
        return {
            "turnover_rate": safe("turnover_rate"),
            "bid_ask_ratio": safe("bid_ask_ratio"),
            "amplitude":     safe("amplitude"),
            "pe_ttm":        safe("pe_ttm_ratio", default=None),
            "pb":            safe("pb_ratio", default=None),
            "change_pct":    safe("change_rate"),
        }
    except Exception:
        return None
    finally:
        if ctx:
            try: ctx.close()
            except Exception:  pass  # cleanup


# ─── L5 Score Computation ──────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


def compute_l5_score(mm_code: str, verbose: bool = False) -> dict:
    """
    Fetch all Moomoo data layers and return an L5 enrichment dict:
    {
      l5_score:   float (0–10),
      l5_grade:   str   (A/B/C/D/F),
      l5_detail:  dict  (component breakdown),
      l5_data:    dict  (raw fetched values),
      l5_error:   str or None
    }
    Returns l5_score=None for unsupported markets (ASX etc.).
    """
    result = {
        "l5_score":  None,
        "l5_grade":  "N/A",
        "l5_detail": {},
        "l5_data":   {},
        "l5_error":  None
    }
    
    if mm_code is None:
        result["l5_error"] = "Market not supported by Moomoo"
        return result
    
    market = _market_prefix(mm_code)   # "US", "HK", "SH", "SZ" etc.

    # ── Fetch ────────────────────────────────────────────────────────────────
    cap_dist = fetch_capital_distribution(mm_code)
    flow_dir = fetch_capital_flow_direction(mm_code)
    snapshot = fetch_snapshot_data(mm_code)
    
    if verbose:
        print(f"\n[L5] {mm_code}")
        print(f"  cap_dist:  {cap_dist}")
        print(f"  flow_dir:  {flow_dir}")
        print(f"  snapshot:  {snapshot}")
    
    result["l5_data"] = {
        "capital_distribution": cap_dist,
        "flow_direction":       flow_dir,
        "snapshot":             snapshot,
    }

    # If ALL sources returned None — either no rights (empty df) or API failure —
    # return l5_score=None so signal_stack uses clean 4-layer weights instead of
    # a fake neutral 5.0 that provides no information and distorts the composite.
    # Note: Moomoo returns ret=0 + empty DataFrame (not an error code) for
    # markets with insufficient quote rights, so the rights cache may not be set.
    if cap_dist is None and flow_dir is None and snapshot is None:
        no_rights_hint = _RIGHTS_CACHE.get(market) is False
        result["l5_error"] = (
            f"No quote rights for {market} market (requires Moomoo subscription)"
            if no_rights_hint else
            f"No L5 data available for {mm_code} (empty or no quote access)"
        )
        return result   # l5_score stays None
    
    # ── Score A: Smart Money Net (40%) ───────────────────────────────────────
    # smart_money_net_pct range: -1..+1
    # Map: +1 → 10, 0 → 5, -1 → 0
    if cap_dist and cap_dist.get("smart_money_net_pct") is not None:
        sn = cap_dist["smart_money_net_pct"]
        score_a = _clamp((sn + 1) / 2 * 10)          # -1→0, 0→5, +1→10
    else:
        score_a = 5.0   # neutral if no data
    
    # ── Score B: Intraday Flow Direction (25%) ──────────────────────────────
    if flow_dir is not None:
        score_b = _clamp((flow_dir + 1) / 2 * 10)    # -1→0, 0→5, +1→10
    else:
        score_b = 5.0
    
    # ── Score C: Bid/Ask Pressure (20%) ────────────────────────────────────
    # bid_ask_ratio: >1 means more buyers (bullish), <1 more sellers (bearish)
    # typical range 0.3..3.0.  Map 1.0 → 5, ratio↑ → score↑, clamp at extremes
    if snapshot and snapshot.get("bid_ask_ratio"):
        bar_raw = snapshot["bid_ask_ratio"]
        if bar_raw > 0:
            import math
            # log scale: log(ratio)/log(3) maps 3→1, 1→0, 0.33→-1  then × 5 + 5
            score_c = _clamp(math.log(bar_raw, 3) * 5 + 5)
        else:
            score_c = 5.0
    else:
        score_c = 5.0
    
    # ── Score D: Turnover Activity (15%) ───────────────────────────────────
    # Turnover rate > 3% = active = good conviction in move
    # >5% = very active (10), 2–3% = avg (5), <1% = low conviction (2)
    if snapshot and snapshot.get("turnover_rate") is not None:
        tr = snapshot["turnover_rate"]
        if tr >= 5.0:
            score_d = 10.0
        elif tr >= 3.0:
            score_d = _clamp(5.0 + (tr - 3.0) / 2.0 * 5.0)
        elif tr >= 1.0:
            score_d = _clamp(2.0 + (tr - 1.0) / 2.0 * 3.0)
        else:
            score_d = _clamp(tr / 1.0 * 2.0)
    else:
        score_d = 5.0

    # ── Score E: Intraday Momentum (bonus, 7% weight) ─────────────────
    # Only attempted when snapshot data is present (market open / quote rights held).
    # When snapshot is None (market closed, pre-market, no rights) the API call is
    # skipped entirely — it would return empty data anyway, saving ~3ms per signal.
    score_e = 5.0   # neutral default
    if snapshot is not None:   # guard: only call bars API when market is reachable
        try:
            from moomoo_market_tools import fetch_intraday_bars as _mm_bars, compute_intraday_signals as _mm_isigs
            _bars = _mm_bars(mm_code, ktype="K_5M", num=12)
            if _bars and len(_bars) >= 3:
                _isigs = _mm_isigs(_bars)
                score_e = _isigs.get("momentum_score", 5.0)
                result["l5_data"]["intraday_signals"] = _isigs
        except Exception:
            pass   # intraday bars are non-fatal (pre-market / no rights)

    # ── Composite ───────────────────────────────────────────────────────────
    # Original weights restored: A 40% | B 25% | C 20% | D 15%
    # Score E (intraday_momentum) stored in l5_data for reference but
    # excluded from composite — defaults to neutral 5.0 except during live market hours.
    l5 = _clamp(score_a * 0.40 + score_b * 0.25 + score_c * 0.20 + score_d * 0.15)
    
    if   l5 >= 8.0: grade = "A"
    elif l5 >= 6.5: grade = "B"
    elif l5 >= 5.0: grade = "C"
    elif l5 >= 3.5: grade = "D"
    else:           grade = "F"
    
    # ── Institutional Flow Score (0-5) — convenience for external consumers ──
    # Combines smart money net + flow direction into a single 0-5 score
    institutional_flow = _clamp(score_a * 0.60 + score_b * 0.40, 0.0, 10.0) / 2.0

    result.update({
        "l5_score": round(l5, 2),
        "l5_grade": grade,
        "l5_detail": {
            "smart_money_net":        round(score_a, 2),
            "intraday_flow":          round(score_b, 2),
            "bid_ask_pressure":       round(score_c, 2),
            "turnover_activity":      round(score_d, 2),
            "intraday_momentum":      round(score_e, 2),
            "institutional_flow_score": round(institutional_flow, 2),
        }
    })
    
    return result


class MoomooQuoteCache:
    """
    Optional push-mode quote cache for Moomoo.

    Replaces per-symbol polling in fetch_snapshot_data() with a persistent
    OpenQuoteContext that receives real-time quote pushes.

    Opt-in: set environment variable MOOMOO_USE_PUSH=1 before importing.
    Default: disabled (existing polling behaviour unchanged).

    Quota: each subscribed symbol × type = 1 unit (100–2000 per account level).
    Each subscription must remain active ≥ 1 minute before unsubscribing.

    Benchmark:
      Polling  (before): context create + API call + close  ≈ 200–350 ms / symbol
      Push cache (after): dict read after first push        ≈ <1 ms / symbol

    Thread safety: the StockQuoteHandlerBase callback fires on the Moomoo
    SDK thread; a threading.Lock guards all writes to the internal cache.

    Usage:
        cache = MoomooQuoteCache.get_instance()
        cache.subscribe(['US.AAPL', 'US.MSFT'])
        time.sleep(2)                      # wait for first push
        data = cache.get('US.AAPL')        # {'last_price': ..., 'volume': ..., ...}
        cache.shutdown()                   # unsubscribe + close context
    """

    _instance: "Optional[MoomooQuoteCache]" = None
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "Optional[MoomooQuoteCache]":
        """Return singleton instance, or None if MOOMOO_USE_PUSH != '1'."""
        if os.environ.get("MOOMOO_USE_PUSH", "0") != "1":
            return None
        with cls._lock:
            if cls._instance is None:
                try:
                    cls._instance = cls()
                except Exception:
                    return None
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Teardown singleton (tests / pipeline end)."""
        with cls._lock:
            if cls._instance:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
                cls._instance = None

    class _QuoteHandler:
        """Bridges StockQuoteHandlerBase callbacks into the shared cache dict."""
        def __init__(self, cache_ref: dict, lock: threading.Lock):
            self._cache = cache_ref
            self._lock  = lock
            from moomoo import StockQuoteHandlerBase, RET_OK
            self._RET_OK = RET_OK
            outer = self
            class _Handler(StockQuoteHandlerBase):
                def on_recv_rsp(self_h, rsp_pb):
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == outer._RET_OK and data is not None:
                        outer._update(data)
                    return ret, data
            self._handler = _Handler()

        def _update(self, data) -> None:
            try:
                with self._lock:
                    for _, row in data.iterrows():
                        code = str(row.get("code", ""))
                        if code:
                            self._cache[code] = dict(row)
            except Exception:
                pass

        def get_handler(self):
            return self._handler

    def __init__(self):
        from moomoo import OpenQuoteContext
        from moomoo_config import OPEND_HOST, OPEND_AU_PORT
        self._data: dict = {}
        self._data_lock = threading.Lock()
        self._subscribed: set = set()
        self._ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_AU_PORT)
        h = MoomooQuoteCache._QuoteHandler(self._data, self._data_lock)
        self._ctx.set_handler(h.get_handler())

    def subscribe(self, mm_codes: list) -> None:
        """Subscribe to QUOTE push for the given Moomoo codes."""
        from moomoo import SubType
        new_codes = [c for c in mm_codes if c not in self._subscribed]
        if not new_codes:
            return
        for i in range(0, len(new_codes), 50):
            batch = new_codes[i:i + 50]
            ret, msg = self._ctx.subscribe(
                batch, [SubType.QUOTE],
                is_first_push=True,   # push last cached data immediately
                subscribe_push=True,
            )
            if ret == 0:
                self._subscribed.update(batch)

    def unsubscribe(self, mm_codes: list) -> None:
        """Unsubscribe from QUOTE push (must be subscribed ≥ 1 min first)."""
        from moomoo import SubType
        for i in range(0, len(mm_codes), 50):
            batch = mm_codes[i:i + 50]
            self._ctx.unsubscribe(batch, [SubType.QUOTE])
            self._subscribed -= set(batch)

    def get(self, mm_code: str) -> Optional[dict]:
        """Return latest cached snapshot dict for mm_code, or None."""
        with self._data_lock:
            return dict(self._data[mm_code]) if mm_code in self._data else None

    def warm_up(self, mm_codes: list, timeout: float = 3.0) -> int:
        """
        Subscribe and wait up to timeout seconds for first-push data.
        Returns number of symbols that have data in cache after the wait.
        """
        self.subscribe(mm_codes)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._data_lock:
                coverage = sum(1 for c in mm_codes if c in self._data)
            if coverage == len(mm_codes):
                break
            time.sleep(0.1)
        with self._data_lock:
            return sum(1 for c in mm_codes if c in self._data)

    def shutdown(self) -> None:
        """Unsubscribe all symbols and close the context."""
        try:
            if self._subscribed:
                from moomoo import SubType
                codes = list(self._subscribed)
                for i in range(0, len(codes), 50):
                    self._ctx.unsubscribe(codes[i:i + 50], [SubType.QUOTE])
                self._subscribed.clear()
        except Exception:
            pass
        try:
            self._ctx.close()
        except Exception:
            pass


def fetch_screener_snapshot(pro_symbols: list) -> dict:
    """
    Moomoo fallback screener — provides snapshot data when TV screener is unavailable
    (401, timeout, block). Returns data in fetch_tv_screener() schema.

    Fields provided from Moomoo get_market_snapshot():
      close       — last_price
      rel_vol     — volume_ratio (10-day relative volume, direct from Moomoo)
      avg_vol     — computed as volume / volume_ratio
      volatility  — amplitude (today's daily range %)
      moomoo_pe   — pe_ttm_ratio  (extra, not in TV schema but passed through)
      moomoo_pb   — pb_ratio      (extra)
      moomoo_change_pct — (last_price - prev_close) / prev_close * 100
      moomoo_bid_ask    — bid_ask_ratio
      moomoo_turnover   — turnover_rate

    Fields NOT available from snapshot (remain None):
      rec_mark, sma200_pct, tv_adx, tv_rsi, macd_hist, beta

    Only covers US/HK/SH/SZ symbols reachable via OpenD.
    ASX/NSE/SGX symbols are silently skipped (returns empty for them).

    Rate limit: same 10 req/30sec rule — this batches in groups of 50.
    """
    if not pro_symbols:
        return {}

    # Group by Moomoo market (only supported ones)
    mm_map: dict[str, str] = {}   # mm_code → original pro_symbol
    for ps in pro_symbols:
        mm = tv_to_moomoo(ps)
        if mm:
            mm_map[mm] = ps

    if not mm_map:
        return {}

    results: dict = {}
    mm_codes = list(mm_map.keys())
    ctx = None
    try:
        ctx = _get_context()
        # Batch in groups of 50 (Moomoo snapshot limit per call)
        for i in range(0, len(mm_codes), 50):
            batch = mm_codes[i:i + 50]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != 0 or data is None or (hasattr(data, "empty") and data.empty):
                continue
            for _, row in data.iterrows():
                mm_code = str(row.get("code", ""))
                ps = mm_map.get(mm_code)
                if not ps:
                    continue

                def _f(col, default=None):
                    try:
                        v = row.get(col)
                        return float(v) if v is not None and str(v) not in ("nan", "N/A", "") else default
                    except Exception:
                        return default

                last_price  = _f("last_price")
                prev_close  = _f("prev_close_price")
                volume      = _f("volume")
                vol_ratio   = _f("volume_ratio")   # 10-day relative volume — direct from Moomoo

                # Derived fields
                avg_vol_calc = None
                if volume is not None and vol_ratio and vol_ratio > 0:
                    avg_vol_calc = volume / vol_ratio

                change_pct_calc = None
                if last_price is not None and prev_close and prev_close > 0:
                    change_pct_calc = round((last_price - prev_close) / prev_close * 100, 3)

                results[ps] = {
                    # --- TV screener schema (partial) ---
                    "rel_vol":    vol_ratio,
                    "rec_mark":   None,          # not available from snapshot
                    "sma200_pct": None,          # needs kline history
                    "close":      last_price,
                    "tv_adx":     None,          # needs kline history
                    "tv_rsi":     None,          # needs kline history
                    "macd_hist":  None,          # needs kline history
                    "avg_vol":    avg_vol_calc,
                    "volatility": _f("amplitude"),
                    "beta":       None,          # not in snapshot
                    # --- Moomoo-only extras (passed through to signal_stack) ---
                    "moomoo_pe":          _f("pe_ttm_ratio"),
                    "moomoo_pb":          _f("pb_ratio"),
                    "moomoo_change_pct":  change_pct_calc,
                    "moomoo_bid_ask":     _f("bid_ask_ratio"),
                    "moomoo_turnover":    _f("turnover_rate"),
                    "source":             "moomoo_fallback",
                }
    except Exception:
        pass
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass

    return results


def enrich_signals(symbols: list[str], delay: float = 0.5) -> dict[str, dict]:
    """
    Given a list of TradingView pro_symbols, return a dict mapping each
    symbol to its L5 enrichment result.
    
    Args:
        symbols:  e.g. ["NYSE:RCL", "NASDAQ:BX", "ASX:MIN"]
        delay:    seconds between requests (rate limit: 30 req / 30 sec)
    
    Returns:
        { "NYSE:RCL": { l5_score, l5_grade, l5_detail, l5_data, l5_error }, ... }
    """
    results = {}
    for sym in symbols:
        mm_code = tv_to_moomoo(sym)
        enrichment = compute_l5_score(mm_code)
        enrichment["mm_code"] = mm_code
        results[sym] = enrichment
        if mm_code:   # only sleep when we actually hit the API
            time.sleep(delay)
    return results


# ─── HTML fragment renderer ────────────────────────────────────────────────────

def l5_html_badge(enrichment: dict) -> str:
    """Return a small HTML snippet for embedding in signal_stack report cards.

    Three distinct None states:
      1. Empty dict (Moomoo disabled/not run)          → return '' (silent)
      2. l5_error == "Market not supported by Moomoo"  → dim "N/A (market unsupported)"
      3. l5_error is another message (API/rights issue) → amber "N/A (no Moomoo data)"
         with a tooltip showing the real reason so the user can diagnose it.
    """
    score = enrichment.get("l5_score")
    if score is None:
        err = enrichment.get("l5_error") or ""
        if not err:
            # Empty enrichment dict — L5 was never attempted (Moomoo disabled)
            return ""
        if "not supported" in err.lower():
            # Genuine unsupported market (NSE, BSE, ASX, SGX …)
            return '<span style="color:#555;font-size:11px">L5: N/A (market unsupported)</span>'
        # Supported market but data unavailable (OpenD not running, no quote rights, etc.)
        safe_err = err.replace('"', "&quot;")
        return (
            f'<span title="{safe_err}" '
            f'style="color:#78716c;font-size:11px;cursor:help">'
            f'L5: N/A (no Moomoo data ⚠)</span>'
        )
    
    grade = enrichment.get("l5_grade", "?")
    detail = enrichment.get("l5_detail", {})
    colour_map = {"A": "#00ff88", "B": "#7fff7f", "C": "#ffcc00", "D": "#ff9900", "F": "#ff4444"}
    clr = colour_map.get(grade, "#aaa")
    
    tip = (
        f"Smart Money: {detail.get('smart_money_net', 'N/A')} | "
        f"Flow Dir: {detail.get('intraday_flow', 'N/A')} | "
        f"Bid/Ask: {detail.get('bid_ask_pressure', 'N/A')} | "
        f"Turnover: {detail.get('turnover_activity', 'N/A')}"
    )
    
    mm = enrichment.get("mm_code", "")
    data = enrichment.get("l5_data", {})
    snap = data.get("snapshot") or {}
    cap  = data.get("capital_distribution") or {}
    
    extra = []
    if snap.get("turnover_rate") is not None:
        extra.append(f"Turnover {snap['turnover_rate']:.1f}%")
    if cap.get("smart_money_net_pct") is not None:
        pct = cap["smart_money_net_pct"] * 100
        sign = "+" if pct >= 0 else ""
        extra.append(f"Smart$ {sign}{pct:.1f}%")
    extra_str = " · ".join(extra)
    
    return (
        f'<div style="margin-top:6px;padding:4px 8px;background:#1a1a2e;border-radius:4px;border-left:3px solid {clr}">'
        f'<span style="color:{clr};font-weight:bold;font-size:12px">L5 Moomoo: {score:.1f}/10 [{grade}]</span>'
        f'<span style="color:#888;font-size:11px;margin-left:8px">{mm} · {extra_str}</span>'
        f'</div>'
    )


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moomoo Enricher — L5 Institutional Flow")
    parser.add_argument("--symbols", nargs="+", default=["NYSE:RCL", "NASDAQ:BX", "NYSE:GD"],
                        help="TradingView pro_symbols to enrich")
    parser.add_argument("--json",   action="store_true", help="Output raw JSON")
    parser.add_argument("--verbose", action="store_true", help="Show raw API values")
    args = parser.parse_args()
    
    print(f"Enriching {len(args.symbols)} symbols via Moomoo...")
    results = {}
    for sym in args.symbols:
        mm = tv_to_moomoo(sym)
        print(f"\n{'─'*50}")
        print(f"  {sym:20}  →  Moomoo: {mm or '(unsupported)'}")
        enrichment = compute_l5_score(mm, verbose=args.verbose)
        results[sym] = enrichment
        if enrichment["l5_score"] is not None:
            d = enrichment["l5_detail"]
            print(f"  L5 Score : {enrichment['l5_score']:.1f} / 10  [{enrichment['l5_grade']}]")
            print(f"  Smart $  : {d.get('smart_money_net', 'N/A')}")
            print(f"  Flow Dir : {d.get('intraday_flow', 'N/A')}")
            print(f"  Bid/Ask  : {d.get('bid_ask_pressure', 'N/A')}")
            print(f"  Turnover : {d.get('turnover_activity', 'N/A')}")
        else:
            print(f"  {enrichment['l5_error']}")
        if mm:
            time.sleep(0.5)
    
    if args.json:
        print(json.dumps(results, indent=2))
