"""
Support Levels — Multi-Source Price Floor Analysis
====================================================
Computes price support levels for any US equity using 4 independent data sources:

  Layer 1 — yfinance OHLCV:
    SMA200, SMA50, 20-day swing low, yesterday pivot (S1/S2), Bollinger lower band

  Layer 2 — Moomoo OpenD OI (PUT wall):
    Full PUT options chain → strike with highest open interest below spot.
    This is the "gamma dealer floor" — market makers who sold puts must buy the
    underlying as price falls toward these strikes (real hedging demand).

  Layer 3 — TradingView scan (america/scan):
    Server-side pre-computed SMA200, SMA50, RSI, ADX.
    Cross-validates Layer 1. Available instantly (no OHLCV download).

  Layer 4 — TradingView WebSocket (optional):
    Real-time last price (lp). Used to check whether price is currently
    holding above support levels. Falls back gracefully when market is closed.

Design principles:
  • SOLID — each layer is an independent fetcher class with a common interface
  • Dependency injection — fetchers are constructor-injectable (fully testable)
  • Graceful fallback — any individual source failure → continues with remainder
  • Thread-safe — Moomoo calls wrapped in ThreadPoolExecutor(wait=False)
  • No side effects — no file I/O, no DB writes, no global state
  • Works 24/7 — yfinance + Moomoo OI work even when markets are closed

Usage:
  from tradingview.options.support_levels import support_levels, SupportLevelsFetcher

  # Simple convenience call
  levels = support_levels("NVDA")
  if levels:
      print(f"NVDA support: {levels.key_support:.2f}")
      print(f"Hold score: {levels.hold_score}/10")
      print(f"Put wall: {levels.put_wall}")
      print(f"Is holding: {levels.is_holding}")

  # Injected (for testing or repeated calls on same ticker universe)
  fetcher = SupportLevelsFetcher(timeout_moomoo=25)
  levels = fetcher.fetch("NVDA")

Data quality indicator (SupportLevels.data_quality):
  "full"        — all 4 sources successful
  "partial"     — 2-3 sources successful (still highly actionable)
  "minimal"     — 1 source only (treat as directional indicator only)
  "unavailable" — all sources failed; SupportLevels still returned with None fields

Public API:
  support_levels(ticker, ...) → SupportLevels | None
  SupportLevelsFetcher.fetch(ticker) → SupportLevels
  SupportLevels (dataclass)
  SupportLevels.is_holding_above(level_name) → bool | None
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import ssl
import sys
import time
import traceback
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ─── UTF-8 fix (Windows) ─────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Path setup ──────────────────────────────────────────────────────────────
_OPTIONS_DIR = Path(__file__).parent           # tradingview/options/
_TV_DIR      = _OPTIONS_DIR.parent             # tradingview/
_ROOT        = _TV_DIR.parent                  # workspace root
_COOKIE_FILE = _TV_DIR / "cookie"

# Ensure workspace root is importable (for moomoo_config, cache_utils, etc.)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ─── Optional dependency guards ──────────────────────────────────────────────
try:
    import requests as _req
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    import numpy as _np
    import pandas as _pd
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False

try:
    import yfinance as _yf
    _YFINANCE_OK = True
except ImportError:
    _YFINANCE_OK = False


# ══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SupportLevels:
    """
    Price support level analysis for a single equity ticker.

    All price fields are in USD (the underlying's quote currency).
    None means the field could not be computed (source unavailable or insufficient data).
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    ticker: str
    spot_price: Optional[float]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ── Layer 1: yfinance technical levels ────────────────────────────────────
    sma200: Optional[float] = None          # 200-day simple moving average
    sma50: Optional[float] = None           # 50-day simple moving average
    swing_low_20d: Optional[float] = None   # lowest closing low in last 20 sessions
    pivot_s1: Optional[float] = None        # S1 = 2×PP − High(yest)
    pivot_s2: Optional[float] = None        # S2 = PP − (High−Low)(yest)
    bb_lower: Optional[float] = None        # Bollinger band lower (SMA20 − 2σ)

    # ── Layer 2: Moomoo PUT wall ───────────────────────────────────────────────
    put_wall: Optional[float] = None        # strike with highest OI below spot
    put_wall_oi: Optional[int] = None       # OI at that strike
    put_wall_confidence: Optional[str] = None  # "strong" / "moderate" / "weak"
    put_wall_dte: Optional[int] = None      # DTE of the expiry used for put wall

    # ── Layer 3: TradingView scan cross-validation ────────────────────────────
    sma200_tv: Optional[float] = None       # TV server-side SMA200 (cross-validates yfinance)
    sma50_tv: Optional[float] = None        # TV server-side SMA50
    rsi: Optional[float] = None             # RSI(14)
    adx: Optional[float] = None             # ADX(14) — trend strength

    # ── Layer 4: WebSocket real-time price ────────────────────────────────────
    ws_price: Optional[float] = None        # Real-time last price (market hours only)

    # ── Composite key support ─────────────────────────────────────────────────
    key_support: Optional[float] = None         # strongest confirmed support below spot
    key_support_sources: List[str] = field(default_factory=list)   # which sources agree
    key_support_confidence: str = "none"        # "high" / "medium" / "low" / "none"

    # ── Market internals ──────────────────────────────────────────────────────
    trend: Optional[str] = None             # "BULL" / "BEAR" / "NEUTRAL"
    is_above_sma200: Optional[bool] = None
    is_above_sma50: Optional[bool] = None

    # ── Hold assessment ───────────────────────────────────────────────────────
    is_holding: Optional[bool] = None       # True if spot > key_support
    hold_score: int = 0                     # 0–10 composite hold score
    hold_reasons: List[str] = field(default_factory=list)  # human-readable reasons
    breakdown: Dict[str, Any] = field(default_factory=dict)  # per-layer detail

    # ── Data quality metadata ─────────────────────────────────────────────────
    sources_used: List[str] = field(default_factory=list)
    sources_failed: List[str] = field(default_factory=list)
    data_quality: str = "unavailable"       # "full" / "partial" / "minimal" / "unavailable"

    # ──────────────────────────────────────────────────────────────────────────

    def is_holding_above(self, level_name: str) -> Optional[bool]:
        """
        Check if spot is currently above a named support level.

        level_name: one of "sma200", "sma50", "swing_low_20d", "pivot_s1",
                    "pivot_s2", "bb_lower", "put_wall", "key_support"
        """
        if self.spot_price is None:
            return None
        level = getattr(self, level_name, None)
        if level is None:
            return None
        return self.spot_price > level

    def all_levels(self) -> Dict[str, Optional[float]]:
        """Return all support levels as an ordered dict (highest → lowest)."""
        raw = {
            "sma200":       self.sma200,
            "sma200_tv":    self.sma200_tv,
            "sma50":        self.sma50,
            "sma50_tv":     self.sma50_tv,
            "swing_low_20d": self.swing_low_20d,
            "pivot_s1":     self.pivot_s1,
            "pivot_s2":     self.pivot_s2,
            "bb_lower":     self.bb_lower,
            "put_wall":     self.put_wall,
            "key_support":  self.key_support,
        }
        populated = {k: v for k, v in raw.items() if v is not None}
        return dict(sorted(populated.items(), key=lambda x: -x[1]))

    def summary(self) -> str:
        """Human-readable one-liner for logging/display."""
        spot   = f"${self.spot_price:.2f}" if self.spot_price else "?"
        sup    = f"${self.key_support:.2f}" if self.key_support else "?"
        oi_str = f"OI={self.put_wall_oi:,}" if self.put_wall_oi is not None else "OI=?"
        pw     = f"${self.put_wall:.0f}({oi_str})" if self.put_wall else "none"
        return (
            f"{self.ticker} | spot={spot} | key_support={sup} "
            f"({self.key_support_confidence}) | put_wall={pw} | "
            f"hold={self.hold_score}/10 | quality={self.data_quality}"
        )

    def get(self, key: str, default=None):
        """Dict-like attribute access — allows ``support_levels.get("key_support")``."""
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        """Subscript access — allows ``support_levels["sma200"]``."""
        return getattr(self, key)

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable dict (compatible with json.dumps)."""
        d = {}
        for f_name in self.__dataclass_fields__:  # type: ignore[attr-defined]
            val = getattr(self, f_name)
            if isinstance(val, datetime):
                d[f_name] = val.isoformat()
            else:
                d[f_name] = val
        return d


# ══════════════════════════════════════════════════════════════════════════════
#  ABSTRACT BASE: SupportSourceFetcher
# ══════════════════════════════════════════════════════════════════════════════

class SupportSourceFetcher:
    """
    Abstract interface for a single-source support level fetcher.

    Concrete implementations must override:
      • name         — str identifier ("yfinance", "moomoo", "tv_scan", "tv_ws")
      • is_available() → bool
      • fetch(ticker) → dict  (partial fields for SupportLevels)

    The dict returned by fetch() must contain ONLY fields that exist on
    SupportLevels. Unknown keys are silently dropped by the orchestrator.
    """
    name: str = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    def fetch(self, ticker: str) -> Dict[str, Any]:
        """
        Fetch support data for *ticker*.

        Returns a (possibly empty) dict of SupportLevels fields.
        On ANY failure, must return {} without raising.
        """
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 1: YFinanceFetcher
# ══════════════════════════════════════════════════════════════════════════════

class YFinanceFetcher(SupportSourceFetcher):
    """
    Computes technical support levels from yfinance OHLCV data.

    Levels computed:
      sma200      — 200-day simple moving average of Close
      sma50       — 50-day simple moving average of Close
      swing_low_20d — minimum Low over the past 20 trading sessions
      pivot_s1    — S1 = 2×PP − H(yesterday);  PP = (H+L+C)/3
      pivot_s2    — S2 = PP − (H−L)(yesterday)
      bb_lower    — SMA20 − 2 × StdDev20(Close)  (Bollinger lower band)
      spot_price  — previous close (most recent Close value)
    """
    name = "yfinance"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def is_available(self) -> bool:
        return _YFINANCE_OK and _NUMPY_OK

    def fetch(self, ticker: str) -> Dict[str, Any]:
        if not self.is_available():
            return {"sources_failed": ["yfinance: library not installed"]}
        try:
            clean = _clean_ticker(ticker)
            t = _yf.Ticker(clean)
            # Need 210 days to guarantee 200 valid SMA rows even with gaps
            df = t.history(period="1y", auto_adjust=True)
            if df is None or df.empty or len(df) < 10:
                return {"sources_failed": [f"yfinance: no history for {clean}"]}

            close  = df["Close"]
            high   = df["High"]
            low    = df["Low"]
            n      = len(close)

            result: Dict[str, Any] = {}

            # ── Spot price from most recent close ───────────────────────────
            result["spot_price"] = float(close.iloc[-1])

            # ── SMA200 ───────────────────────────────────────────────────────
            if n >= 200:
                result["sma200"] = float(close.rolling(200).mean().iloc[-1])

            # ── SMA50 ────────────────────────────────────────────────────────
            if n >= 50:
                result["sma50"] = float(close.rolling(50).mean().iloc[-1])

            # ── 20-day swing low ─────────────────────────────────────────────
            # Use Low prices (not Close) — more accurate floor
            if n >= 20:
                result["swing_low_20d"] = float(low.iloc[-20:].min())

            # ── Pivot point (yesterday) ──────────────────────────────────────
            if n >= 2:
                yest_h = float(high.iloc[-2])
                yest_l = float(low.iloc[-2])
                yest_c = float(close.iloc[-2])
                pp     = (yest_h + yest_l + yest_c) / 3.0
                result["pivot_s1"] = round(2 * pp - yest_h, 4)
                result["pivot_s2"] = round(pp - (yest_h - yest_l), 4)

            # ── Bollinger lower band (SMA20 − 2σ) ───────────────────────────
            if n >= 20:
                sma20     = close.rolling(20).mean()
                std20     = close.rolling(20).std(ddof=1)
                bb_lo     = sma20 - 2 * std20
                result["bb_lower"] = float(bb_lo.iloc[-1])

            # ── is_above computed fields ──────────────────────────────────────
            spot = result.get("spot_price")
            if spot:
                if result.get("sma200"):
                    result["is_above_sma200"] = spot > result["sma200"]
                if result.get("sma50"):
                    result["is_above_sma50"] = spot > result["sma50"]

            result["sources_used"] = ["yfinance"]
            return result

        except Exception as exc:
            if self.verbose:
                traceback.print_exc()
            return {"sources_failed": [f"yfinance: {exc}"]}


# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 2: MoomooPutWallFetcher
# ══════════════════════════════════════════════════════════════════════════════

class MoomooPutWallFetcher(SupportSourceFetcher):
    """
    Computes the "PUT wall" — the PUT strike with the highest open interest
    below the current spot price — using Moomoo OpenD's real-time OI data.

    The put wall represents a genuine price floor: market makers who sold
    puts at that strike must buy the underlying as price approaches it
    (gamma/delta hedging), creating real buying demand.

    Features:
      • Works 24/7 — Moomoo OI data is available even when markets are closed
      • Filters to near-term expiries (DTE ≤ 45) to avoid stale LEAPS
      • Thread-safe: wraps Moomoo API in ThreadPoolExecutor(wait=False)
        per Golden Rule #16 (prevents 7-minute hang on Moomoo disconnect)
    """
    name = "moomoo"

    _MAX_DTE = 45          # maximum DTE for put wall expiry consideration
    _MIN_OI  = 100         # minimum OI to qualify as a real put wall
    _NEAR_ATM_PCT = 0.25   # only scan puts within 25% of spot (avoids deep OTM noise)
    _BATCH   = 200         # max codes per get_market_snapshot() call
    _TIMEOUT = 25          # seconds before giving up

    def __init__(self, verbose: bool = False, timeout: int = _TIMEOUT):
        self.verbose  = verbose
        self.timeout  = timeout
        self._ctx     = None
        self._lock    = threading.Lock()
        self._avail   = None   # None = unchecked

    def is_available(self) -> bool:
        if self._avail is not None:
            return self._avail
        self._avail = self._try_connect()
        return self._avail

    def _try_connect(self) -> bool:
        try:
            self._ctx = self._make_ctx()
            return self._ctx is not None
        except Exception:
            return False

    def _make_ctx(self):
        """Open and return a Moomoo OpenQuoteContext, or None."""
        try:
            import importlib
            os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
            from moomoo import OpenQuoteContext
            from moomoo_config import quote_port, OPEND_HOST
            try:
                port = quote_port()
            except RuntimeError:
                if self.verbose:
                    print("[SupportLevels/Moomoo] No OpenD on 11111/11112")
                return None
            ctx = OpenQuoteContext(host=OPEND_HOST, port=port)
            return ctx
        except Exception as exc:
            if self.verbose:
                print(f"[SupportLevels/Moomoo] Connect error: {exc}")
            return None

    def _close(self) -> None:
        with self._lock:
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None

    def fetch(self, ticker: str) -> Dict[str, Any]:
        if not self.is_available():
            return {"sources_failed": ["moomoo: OpenD not reachable"]}

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._fetch_put_wall, ticker)
        try:
            return fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            return {"sources_failed": [f"moomoo: timeout after {self.timeout}s"]}
        except Exception as exc:
            return {"sources_failed": [f"moomoo: {exc}"]}
        finally:
            ex.shutdown(wait=False)  # Golden Rule #16: NEVER wait=True with timed futures

    def _fetch_put_wall(self, ticker: str) -> Dict[str, Any]:
        """Inner (run in worker thread). Returns partial SupportLevels dict."""
        try:
            mm_code = _tv_to_mm(ticker)
            if not mm_code:
                return {"sources_failed": [f"moomoo: cannot convert {ticker} to Moomoo code"]}

            # Ensure we have a context (thread may need its own)
            # Read ctx under lock to avoid TOCTOU race with _close()
            with self._lock:
                ctx = self._ctx
            if ctx is None:
                ctx = self._make_ctx()
                if ctx is None:
                    return {"sources_failed": ["moomoo: context unavailable in worker thread"]}

            from moomoo import RET_OK

            # ── Get spot price ─────────────────────────────────────────────
            ret, snap = ctx.get_market_snapshot([mm_code])
            if ret != RET_OK or snap is None or snap.empty:
                return {"sources_failed": [f"moomoo: snapshot failed for {mm_code}"]}
            spot = float(snap["last_price"].iloc[0])

            # ── Get expiry dates (near-term only, DTE ≤ 45) ────────────────
            ret, exp_df = ctx.get_option_expiration_date(code=mm_code)
            if ret != RET_OK or exp_df is None or exp_df.empty:
                return {"sources_failed": ["moomoo: no expiry dates"]}

            today = datetime.now(timezone.utc).date()
            valid_expiries: List[Tuple[str, int]] = []
            for exp_val in exp_df["time"].tolist():
                try:
                    exp_str = str(exp_val)[:10]   # YYYY-MM-DD
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    dte = (exp_date - today).days
                    if 3 <= dte <= self._MAX_DTE:
                        valid_expiries.append((exp_str, dte))
                except Exception:
                    continue

            if not valid_expiries:
                return {"sources_failed": ["moomoo: no near-term expiries found"]}

            # Use the first (nearest) valid expiry for the put wall
            exp_str, dte_used = valid_expiries[0]

            # ── Fetch PUT codes for that expiry ───────────────────────────
            start_str = today.strftime("%Y-%m-%d")
            ret, chain_df = ctx.get_option_chain_for_date(
                code=mm_code,
                start=exp_str,
                end=exp_str,
                option_type="put"
            )
            if ret != RET_OK or chain_df is None or chain_df.empty:
                return {"sources_failed": [f"moomoo: no put chain for {exp_str}"]}

            # ── Filter: PUT strikes within 25% below spot ─────────────────
            min_strike = spot * (1 - self._NEAR_ATM_PCT)
            codes_below: List[str] = []
            for _, row in chain_df.iterrows():
                try:
                    stk = float(row.get("strike_price", 0))
                    if min_strike <= stk <= spot:
                        code = row.get("code", "")
                        if code:
                            codes_below.append(code)
                except Exception:
                    continue

            if not codes_below:
                return {"sources_failed": ["moomoo: no eligible put strikes below spot"]}

            # ── Batch fetch snapshots ──────────────────────────────────────
            all_snaps = []
            for i in range(0, len(codes_below), self._BATCH):
                batch = codes_below[i: i + self._BATCH]
                ret, sdf = ctx.get_market_snapshot(batch)
                if ret == RET_OK and sdf is not None and not sdf.empty:
                    all_snaps.append(sdf)

            if not all_snaps:
                return {"sources_failed": ["moomoo: snapshot batch returned nothing"]}

            import pandas as pd
            snaps = pd.concat(all_snaps, ignore_index=True)

            # ── Find strike with max OI ────────────────────────────────────
            oi_col = "option_open_interest" if "option_open_interest" in snaps.columns else None
            if oi_col is None:
                return {"sources_failed": ["moomoo: OI column not found"]}

            snaps["_oi_num"] = pd.to_numeric(snaps[oi_col], errors="coerce").fillna(0)

            # Merge strike prices back from chain_df
            code_to_strike: Dict[str, float] = {}
            for _, row in chain_df.iterrows():
                code_to_strike[row.get("code", "")] = float(row.get("strike_price", 0))
            snaps["_strike"] = snaps["code"].map(code_to_strike).fillna(0)

            best = snaps.loc[snaps["_oi_num"].idxmax()]
            best_oi     = int(best["_oi_num"])
            best_strike = float(best["_strike"])

            if best_oi < self._MIN_OI or best_strike <= 0:
                return {"sources_failed": [f"moomoo: best OI={best_oi} below threshold {self._MIN_OI}"]}

            # ── Classify confidence ─────────────────────────────────────────
            if best_oi >= 5000:
                confidence = "strong"
            elif best_oi >= 1000:
                confidence = "moderate"
            else:
                confidence = "weak"

            return {
                "put_wall":            best_strike,
                "put_wall_oi":         best_oi,
                "put_wall_confidence": confidence,
                "put_wall_dte":        dte_used,
                "sources_used":        ["moomoo"],
            }

        except Exception as exc:
            if self.verbose:
                traceback.print_exc()
            return {"sources_failed": [f"moomoo: unexpected error: {exc}"]}


# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 3: TVScanFetcher
# ══════════════════════════════════════════════════════════════════════════════

class TVScanFetcher(SupportSourceFetcher):
    """
    Fetches pre-computed server-side support indicators from TradingView scan API.

    Fields returned: SMA200, SMA50, RSI, ADX, close (spot), SMA20 (for BB cross-check).

    Advantages over yfinance:
      • No OHLCV download (instant, single HTTP POST)
      • TV computes SMA200 server-side — no float precision issues
      • Cross-validates Layer 1 yfinance results

    Auth: TV cookie file at tradingview/cookie (sessionid + sessionid_sign).
    If cookie is missing/expired → falls back gracefully to empty dict.
    """
    name = "tv_scan"

    _SCAN_URL  = "https://scanner.tradingview.com/america/scan"
    _TIMEOUT   = 15   # seconds

    _COLUMNS = [
        "close",
        "SMA200",
        "SMA50",
        "RSI",
        "ADX",
        "BB.lower",   # Bollinger lower (cross-check)
    ]

    def __init__(self, verbose: bool = False):
        self.verbose  = verbose
        self._session = None
        self._avail   = None

    def is_available(self) -> bool:
        if self._avail is not None:
            return self._avail
        self._avail = _REQUESTS_OK and self._cookie_exists()
        return self._avail

    def _cookie_exists(self) -> bool:
        if not _COOKIE_FILE.exists():
            return False
        raw = _COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
        return "sessionid=" in raw and "sessionid_sign=" in raw

    def _headers(self) -> Dict[str, str]:
        """Build request headers from TV cookie file."""
        if not _COOKIE_FILE.exists():
            return {}
        raw = _COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
        sid  = re.search(r"sessionid=([^;\s]+)",      raw)
        sign = re.search(r"sessionid_sign=([^;\s]+)", raw)
        if not sid or not sign:
            return {}
        cookie = f"sessionid={sid.group(1)}; sessionid_sign={sign.group(1)}"
        return {
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "Cookie":       cookie,
            "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin":       "https://www.tradingview.com",
            "Referer":      "https://www.tradingview.com/",
        }

    def fetch(self, ticker: str) -> Dict[str, Any]:
        if not self.is_available():
            return {"sources_failed": ["tv_scan: requests library or cookie unavailable"]}
        try:
            symbol = _ensure_exchange_prefix(ticker)
            hdrs   = self._headers()
            if not hdrs:
                return {"sources_failed": ["tv_scan: cookie missing or invalid"]}

            payload = {
                "symbols": {"tickers": [symbol], "query": {"types": []}},
                "columns": self._COLUMNS,
            }
            resp = _req.post(
                self._SCAN_URL,
                json=payload,
                headers=hdrs,
                timeout=self._TIMEOUT,
            )
            if resp.status_code == 401:
                if self.verbose:
                    print("[SupportLevels/TVScan] Cookie expired (401). Refresh tradingview/cookie.")
                return {"sources_failed": ["tv_scan: cookie expired (401)"]}
            resp.raise_for_status()
            data = resp.json()

            rows = data.get("data", [])
            if not rows:
                return {"sources_failed": [f"tv_scan: no data returned for {symbol}"]}

            vals = rows[0].get("d", [])
            if len(vals) < len(self._COLUMNS):
                return {"sources_failed": ["tv_scan: incomplete column data"]}

            # Map column index → value
            col_map = dict(zip(self._COLUMNS, vals))

            result: Dict[str, Any] = {}

            def _f(key: str) -> Optional[float]:
                v = col_map.get(key)
                if v is None:
                    return None
                try:
                    f = float(v)
                    return None if f != f else f   # reject NaN
                except (TypeError, ValueError):
                    return None

            close = _f("close")
            if close is not None:
                result["spot_price"] = close   # use as spot_price cross-check

            sma200 = _f("SMA200")
            if sma200 is not None:
                result["sma200_tv"] = sma200

            sma50 = _f("SMA50")
            if sma50 is not None:
                result["sma50_tv"] = sma50

            rsi = _f("RSI")
            if rsi is not None:
                result["rsi"] = rsi

            adx = _f("ADX")
            if adx is not None:
                result["adx"] = adx

            if result:
                result["sources_used"] = ["tv_scan"]
            else:
                result["sources_failed"] = ["tv_scan: all fields were None/NaN"]

            return result

        except Exception as exc:
            if self.verbose:
                traceback.print_exc()
            return {"sources_failed": [f"tv_scan: {exc}"]}


# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 4: TVWebSocketFetcher
# ══════════════════════════════════════════════════════════════════════════════

class TVWebSocketFetcher(SupportSourceFetcher):
    """
    Fetches the real-time last price (lp) from TradingView WebSocket feed.

    This layer is OPTIONAL and supplementary:
      • Provides the most current price during US market hours (09:30–16:00 ET)
      • Outside market hours, returns last-session values (still useful for levels check)
      • Falls back gracefully when WebSocket is unavailable

    Uses the same WebSocket connection pattern as hybrid_options.py.
    Auth: TV cookie file at tradingview/cookie.
    """
    name = "tv_ws"

    _WS_URL  = "wss://data.tradingview.com/socket.io/websocket"
    _TIMEOUT = 12   # seconds to wait for ws_price

    def __init__(self, verbose: bool = False, timeout: int = _TIMEOUT):
        self.verbose = verbose
        self.timeout = timeout
        self._avail  = None

    def is_available(self) -> bool:
        if self._avail is not None:
            return self._avail
        try:
            import websocket as _ws  # noqa: F401
            self._avail = _REQUESTS_OK and _COOKIE_FILE.exists()
        except ImportError:
            self._avail = False
        return self._avail

    def fetch(self, ticker: str) -> Dict[str, Any]:
        if not self.is_available():
            return {"sources_failed": ["tv_ws: websocket-client not installed"]}
        try:
            price = self._fetch_ws_price(ticker)
            if price is not None:
                return {
                    "ws_price":     price,
                    "sources_used": ["tv_ws"],
                }
            return {"sources_failed": ["tv_ws: no price received"]}
        except Exception as exc:
            if self.verbose:
                traceback.print_exc()
            return {"sources_failed": [f"tv_ws: {exc}"]}

    def _fetch_ws_price(self, ticker: str) -> Optional[float]:
        """Open WS connection, send subscribe, read lp. Returns None on failure."""
        try:
            import websocket as _ws
        except ImportError:
            return None

        symbol = _ensure_exchange_prefix(ticker)
        cookie_hdr = self._build_cookie_header()
        if not cookie_hdr:
            return None

        result_holder: Dict[str, Optional[float]] = {"lp": None}
        done = threading.Event()

        def on_message(ws_app, message: str):
            try:
                # TV uses a custom framing: ~m~<len>~m~<json>
                for chunk in re.split(r"~m~\d+~m~", message):
                    if not chunk:
                        continue
                    # ping frame
                    if re.match(r"~h~\d+", chunk):
                        ws_app.send(f"~m~{len(chunk)+9}~m~" + f"~h~{chunk[3:]}")
                        continue
                    try:
                        obj = json.loads(chunk)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if obj.get("m") == "qsd":
                        p_data = obj.get("p", [])
                        for item in p_data:
                            if isinstance(item, dict) and "v" in item:
                                lp = item["v"].get("lp")
                                if lp is not None:
                                    result_holder["lp"] = float(lp)
                                    done.set()
                                    ws_app.close()
                                    return
            except Exception:
                pass

        def on_error(ws_app, error):
            done.set()

        def on_open(ws_app):
            try:
                # Auth + subscribe pattern identical to hybrid_options.py
                sess_id = _rand_session_id()
                chart_sess = _rand_session_id()
                _send(ws_app, "set_auth_token", [_get_tv_token()])
                _send(ws_app, "quote_create_session", [sess_id])
                _send(ws_app, "quote_set_fields", [sess_id, "lp", "volume", "ch", "chp"])
                _send(ws_app, "quote_add_symbols", [sess_id, symbol])
            except Exception:
                done.set()

        ws_app = _ws.WebSocketApp(
            self._WS_URL,
            header={"Cookie": cookie_hdr},
            on_message=on_message,
            on_error=on_error,
            on_open=on_open,
        )

        t = threading.Thread(
            target=lambda: ws_app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}),
            daemon=True,
        )
        t.start()
        done.wait(timeout=self.timeout)
        try:
            ws_app.close()
        except Exception:
            pass

        return result_holder["lp"]

    def batch_fetch_prices(self, tickers: List[str]) -> Dict[str, Optional[float]]:
        """Open ONE WebSocket connection and fetch lp for ALL tickers at once.

        Returns {ticker: price_or_None}.  Tickers are automatically prefixed
        with the correct exchange (e.g. "NASDAQ:NVDA").
        """
        if not tickers:
            return {}
        try:
            import websocket as _ws
        except ImportError:
            return {t: None for t in tickers}

        cookie_hdr = self._build_cookie_header()
        if not cookie_hdr:
            return {t: None for t in tickers}

        # Map exchange-prefixed symbol → bare ticker for result dict
        sym_to_ticker: Dict[str, str] = {
            _ensure_exchange_prefix(t): t for t in tickers
        }
        all_symbols  = list(sym_to_ticker.keys())
        result: Dict[str, Optional[float]] = {t: None for t in tickers}
        received: set = set()
        done = threading.Event()

        def on_message(ws_app, message: str):
            try:
                for chunk in re.split(r"~m~\d+~m~", message):
                    if not chunk:
                        continue
                    if re.match(r"~h~\d+", chunk):
                        ws_app.send(f"~m~{len(chunk)+9}~m~" + f"~h~{chunk[3:]}")
                        continue
                    try:
                        obj = json.loads(chunk)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if obj.get("m") == "qsd":
                        p_data = obj.get("p", [])
                        if len(p_data) >= 2 and isinstance(p_data[1], dict):
                            sym = p_data[1].get("n", "")
                            v   = p_data[1].get("v", {})
                            lp  = v.get("lp")
                            if lp is not None:
                                # Match by full prefixed symbol first; fall back to
                                # bare ticker to handle NYSE vs NASDAQ prefix mismatches.
                                ticker_key = sym_to_ticker.get(sym)
                                if ticker_key is None:
                                    bare = sym.split(":", 1)[-1]
                                    ticker_key = next(
                                        (t for t in tickers if t.upper() == bare), None
                                    )
                                if ticker_key is not None:
                                    result[ticker_key] = float(lp)
                                    received.add(sym)
                                    if len(received) >= len(all_symbols):
                                        done.set()
                                        ws_app.close()
            except Exception:
                pass

        def on_error(ws_app, error):
            done.set()

        def on_open(ws_app):
            try:
                sess_id = _rand_session_id()
                _send(ws_app, "set_auth_token", [_get_tv_token()])
                _send(ws_app, "quote_create_session", [sess_id])
                _send(ws_app, "quote_set_fields", [sess_id, "lp", "volume", "ch", "chp"])
                # Subscribe to ALL symbols in one call
                _send(ws_app, "quote_add_symbols", [sess_id] + all_symbols)
            except Exception:
                done.set()

        ws_app = _ws.WebSocketApp(
            self._WS_URL,
            header={"Cookie": cookie_hdr},
            on_message=on_message,
            on_error=on_error,
            on_open=on_open,
        )

        t = threading.Thread(
            target=lambda: ws_app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}),
            daemon=True,
        )
        t.start()
        # Wait up to self.timeout seconds for all prices (or partial if some tickers
        # don't respond — e.g. after-hours or bad symbols).
        done.wait(timeout=self.timeout)
        try:
            ws_app.close()
        except Exception:
            pass

        filled = sum(1 for v in result.values() if v is not None)
        if self.verbose:
            print(f"[TVWebSocketFetcher] batch: {filled}/{len(tickers)} prices received")
        if filled < len(tickers):
            missing = [t for t in tickers if result.get(t) is None]
            print(f"[TVWebSocket] batch timeout: {len(missing)} symbols returned no lp: {missing}")
        return result

    def _build_cookie_header(self) -> str:
        """Read cookie file and return the Cookie header string."""
        if not _COOKIE_FILE.exists():
            return ""
        raw = _COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
        sid  = re.search(r"sessionid=([^;\s]+)",      raw)
        sign = re.search(r"sessionid_sign=([^;\s]+)", raw)
        if not sid or not sign:
            return ""
        return f"sessionid={sid.group(1)}; sessionid_sign={sign.group(1)}"


# ── WebSocket helpers (same pattern as hybrid_options.py) ────────────────────

def _rand_session_id() -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase, k=12))


def _send(ws_app, func: str, params: list) -> None:
    """Send a TradingView WebSocket message."""
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    msg = f"~m~{len(payload)}~m~{payload}"
    ws_app.send(msg)


def _get_tv_token() -> str:
    """Return TV WebSocket auth token.

    Cookie-authenticated sessions use 'unauthorized_user_token' as the token;
    actual authentication happens via the Cookie header in the WS HTTP upgrade
    (set in ws_app header dict). If a JWT is present in the cookie file, use it.
    """
    if not _COOKIE_FILE.exists():
        return "unauthorized_user_token"
    raw = _COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
    # Look for embedded JWT (auth_token field)
    jwt_match = re.search(r'"auth_token"\s*:\s*"(eyJ[^"]+)"', raw)
    if jwt_match:
        return jwt_match.group(1)
    return "unauthorized_user_token"


# ══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR: SupportLevelsFetcher
# ══════════════════════════════════════════════════════════════════════════════

class SupportLevelsFetcher:
    """
    Orchestrates all 4 support level sources and combines them into a
    SupportLevels dataclass.

    Constructor injection allows replacing any source with a mock:

      class MockMoomoo(SupportSourceFetcher):
          name = "moomoo"
          def is_available(self): return True
          def fetch(self, ticker): return {"put_wall": 131.0, "put_wall_oi": 5000, ...}

      fetcher = SupportLevelsFetcher(moomoo=MockMoomoo())
      levels  = fetcher.fetch("NVDA")

    Fields are merged from all sources in priority order:
      yfinance → tv_scan → moomoo → tv_ws
    When fields conflict (e.g. spot_price from both yfinance and tv_scan),
    the earlier source wins unless the later value is more recent.
    """

    def __init__(
        self,
        yfinance: Optional[SupportSourceFetcher] = None,
        moomoo:   Optional[SupportSourceFetcher] = None,
        tv_scan:  Optional[SupportSourceFetcher] = None,
        tv_ws:    Optional[SupportSourceFetcher] = None,
        timeout_moomoo: int = 25,
        verbose: bool = False,
    ):
        self.verbose = verbose
        self._sources: List[SupportSourceFetcher] = [
            yfinance or YFinanceFetcher(verbose=verbose),
            tv_scan  or TVScanFetcher(verbose=verbose),
            moomoo   or MoomooPutWallFetcher(verbose=verbose, timeout=timeout_moomoo),
            tv_ws    or TVWebSocketFetcher(verbose=verbose),
        ]

    def fetch(self, ticker: str) -> SupportLevels:
        """
        Fetch support levels from all available sources and return a combined SupportLevels.

        Never raises. Always returns a SupportLevels instance; if all sources fail,
        most fields will be None and data_quality == "unavailable".
        """
        ticker = ticker.strip().upper()

        # ── Run all sources, collecting partial results ───────────────────
        combined: Dict[str, Any] = {}
        sources_used: List[str]   = []
        sources_failed: List[str] = []

        for src in self._sources:
            try:
                if not src.is_available():
                    sources_failed.append(f"{src.name}: not available")
                    continue
                partial = src.fetch(ticker)
                # Accumulate sources_used / sources_failed lists
                sources_used.extend(partial.pop("sources_used", []))
                sources_failed.extend(partial.pop("sources_failed", []))
                # Merge: first writer wins for scalar fields
                for k, v in partial.items():
                    if k not in combined or combined[k] is None:
                        combined[k] = v
            except Exception as exc:
                sources_failed.append(f"{src.name}: unexpected: {exc}")

        # ── Build SupportLevels instance ────────────────────────────────
        # Known scalar fields (avoid polluting dataclass with unknown keys)
        _KNOWN_FIELDS = {f for f in SupportLevels.__dataclass_fields__}

        init_kwargs: Dict[str, Any] = {"ticker": ticker}
        for k, v in combined.items():
            if k in _KNOWN_FIELDS and k not in ("ticker", "sources_used", "sources_failed"):
                init_kwargs[k] = v

        # Ensure spot_price is set
        if "spot_price" not in init_kwargs or init_kwargs.get("spot_price") is None:
            init_kwargs["spot_price"] = None

        levels = SupportLevels(**init_kwargs)
        levels.sources_used   = list(set(sources_used))
        levels.sources_failed = sources_failed

        # ── Compute derived fields ──────────────────────────────────────
        _compute_key_support(levels)
        _compute_trend(levels)
        _compute_hold_score(levels)
        _compute_data_quality(levels)

        if self.verbose:
            print(f"[SupportLevels] {levels.summary()}")

        return levels

    def batch_fetch(self, tickers: List[str]) -> Dict[str, "SupportLevels"]:
        """
        Fetch support levels for ALL tickers using exactly ONE WebSocket connection.

        Other sources (yfinance, tv_scan, moomoo) are still called per-ticker via
        their normal REST APIs (they're fast).  Only the WS connection is batched.

        Returns {ticker: SupportLevels}.  Never raises.
        """
        if not tickers:
            return {}

        # Step 1 — Batch WS price fetch (ONE connection for all tickers)
        ws_fetcher = next(
            (s for s in self._sources if isinstance(s, TVWebSocketFetcher)), None
        )
        ws_prices: Dict[str, Optional[float]] = {}
        if ws_fetcher is not None:
            ws_prices = ws_fetcher.batch_fetch_prices(list(tickers))

        # Step 2 — Per-ticker: run all non-WS sources
        non_ws_sources = [s for s in self._sources if not isinstance(s, TVWebSocketFetcher)]
        results: Dict[str, "SupportLevels"] = {}

        for ticker in tickers:
            ticker = ticker.strip().upper()
            combined: Dict[str, Any]      = {}
            sources_used: List[str]       = []
            sources_failed: List[str]     = []

            for src in non_ws_sources:
                try:
                    if not src.is_available():
                        sources_failed.append(f"{src.name}: not available")
                        continue
                    partial = src.fetch(ticker)
                    sources_used.extend(partial.pop("sources_used", []))
                    sources_failed.extend(partial.pop("sources_failed", []))
                    for k, v in partial.items():
                        if k not in combined or combined[k] is None:
                            combined[k] = v
                except Exception as exc:
                    sources_failed.append(f"{src.name}: unexpected: {exc}")

            # Inject WS price obtained from the single batch connection.
            # Always store as ws_price (for intraday freshness checks) and credit tv_ws.
            # Only use as spot_price fallback when REST sources didn't supply one.
            ws_price = ws_prices.get(ticker)
            if ws_price is not None:
                combined["ws_price"] = ws_price
                sources_used.append("tv_ws")
                if combined.get("spot_price") is None:
                    combined["spot_price"] = ws_price

            # Build SupportLevels instance
            _KNOWN_FIELDS = {f for f in SupportLevels.__dataclass_fields__}
            init_kwargs: Dict[str, Any] = {"ticker": ticker}
            for k, v in combined.items():
                if k in _KNOWN_FIELDS and k not in ("ticker", "sources_used", "sources_failed"):
                    init_kwargs[k] = v
            if "spot_price" not in init_kwargs or init_kwargs.get("spot_price") is None:
                init_kwargs["spot_price"] = None

            levels = SupportLevels(**init_kwargs)
            levels.sources_used   = list(set(sources_used))
            levels.sources_failed = sources_failed

            _compute_key_support(levels)
            _compute_trend(levels)
            _compute_hold_score(levels)
            _compute_data_quality(levels)

            if self.verbose:
                print(f"[SupportLevels] {levels.summary()}")

            results[ticker] = levels

        return results


# ══════════════════════════════════════════════════════════════════════════════
#  DERIVED FIELD COMPUTATIONS (pure functions — fully testable)
# ══════════════════════════════════════════════════════════════════════════════

def _compute_key_support(levels: SupportLevels) -> None:
    """
    Select the single strongest support level below spot.

    Algorithm:
    1. Collect all valid support levels < spot_price
    2. For each, count how many independent sources agree within ±1%
    3. Score each candidate: base_score + source_agreement_bonus + proximity_bonus
    4. Pick highest-scored candidate as key_support
    """
    spot = levels.spot_price
    if spot is None or spot <= 0:
        return

    # All candidate levels (source_name, price)
    # Guard: value must be not None, strictly positive (no negative pivots), and below spot
    candidates: List[Tuple[str, float]] = []
    if levels.sma200        is not None and 0 < levels.sma200        < spot: candidates.append(("sma200",        levels.sma200))
    if levels.sma200_tv     is not None and 0 < levels.sma200_tv     < spot: candidates.append(("sma200_tv",     levels.sma200_tv))
    if levels.sma50         is not None and 0 < levels.sma50         < spot: candidates.append(("sma50",         levels.sma50))
    if levels.sma50_tv      is not None and 0 < levels.sma50_tv      < spot: candidates.append(("sma50_tv",      levels.sma50_tv))
    if levels.swing_low_20d is not None and 0 < levels.swing_low_20d < spot: candidates.append(("swing_low_20d", levels.swing_low_20d))
    if levels.pivot_s1      is not None and 0 < levels.pivot_s1      < spot: candidates.append(("pivot_s1",      levels.pivot_s1))
    if levels.pivot_s2      is not None and 0 < levels.pivot_s2      < spot: candidates.append(("pivot_s2",      levels.pivot_s2))
    if levels.bb_lower      is not None and 0 < levels.bb_lower      < spot: candidates.append(("bb_lower",      levels.bb_lower))
    if levels.put_wall      is not None and 0 < levels.put_wall      < spot: candidates.append(("put_wall",      levels.put_wall))

    if not candidates:
        return

    # Source reliability weights (higher = more trustworthy)
    _RELIABILITY: Dict[str, float] = {
        "put_wall":       3.0,   # Real market data (Moomoo OI), 24/7
        "sma200":         2.5,   # Most widely watched technical level
        "sma200_tv":      2.5,   # TV cross-validates yfinance
        "sma50":          2.0,
        "sma50_tv":       2.0,
        "swing_low_20d":  1.8,   # Recent tested support
        "pivot_s1":       1.5,   # Yesterday's pivot — shorter time frame
        "pivot_s2":       1.2,
        "bb_lower":       1.0,   # Least reliable as a hard support
    }

    def _score(name: str, price: float) -> float:
        base = _RELIABILITY.get(name, 1.0)
        # Proximity bonus: prefer levels within 3% of spot (recently tested)
        pct_from_spot = (spot - price) / spot
        if 0 < pct_from_spot <= 0.03:
            proximity = 1.5
        elif 0 < pct_from_spot <= 0.08:
            proximity = 1.0
        else:
            proximity = 0.5
        # Source agreement: count how many OTHER sources agree within ±1%
        band = price * 0.01
        agreement = sum(
            1 for (n2, p2) in candidates
            if n2 != name and abs(p2 - price) <= band
        )
        return base * proximity + agreement * 0.5

    scored = [(n, p, _score(n, p)) for (n, p) in candidates]
    scored.sort(key=lambda x: -x[2])

    best_name, best_price, _ = scored[0]

    # Find all sources that agree within ±1% of the best
    agreement_band = best_price * 0.01
    agreeing = [
        n for (n, p, _) in scored
        if abs(p - best_price) <= agreement_band
    ]

    levels.key_support         = round(best_price, 2)
    levels.key_support_sources = agreeing

    n_agree = len(agreeing)
    if n_agree >= 3:
        levels.key_support_confidence = "high"
    elif n_agree == 2:
        levels.key_support_confidence = "medium"
    else:
        levels.key_support_confidence = "low"


def _compute_trend(levels: SupportLevels) -> None:
    """Determine macro trend from SMA position and RSI."""
    spot = levels.spot_price
    if spot is None:
        return

    sma200 = levels.sma200 or levels.sma200_tv
    sma50  = levels.sma50  or levels.sma50_tv

    above_200 = (sma200 is not None) and (spot > sma200)
    above_50  = (sma50  is not None) and (spot > sma50)

    levels.is_above_sma200 = above_200 if sma200 else None
    levels.is_above_sma50  = above_50  if sma50  else None

    if above_200 and above_50:
        levels.trend = "BULL"
    elif not above_200 and not above_50:
        levels.trend = "BEAR"
    elif sma200 or sma50:
        levels.trend = "NEUTRAL"


def _compute_hold_score(levels: SupportLevels) -> None:
    """
    Compute the 0–10 hold score: "Is price holding above its key support?"

    Scoring rationale (each component independently verified):
      +3.0  price > SMA200 (trend intact — most important)
      +2.0  price > SMA50  (medium-term support)
      +1.5  price > swing_low_20d (recent support held)
      +1.5  price > put_wall (dealer floor intact)
      +1.0  RSI > 50 (momentum confirms buyers in control)
      +0.5  key_support_confidence == "high" (multi-source agreement)
      +0.5  adx > 20 (trend has strength)

    Total max = 10.0 → clamped to int(0–10)
    """
    spot = levels.spot_price
    score  = 0.0
    reasons: List[str] = []
    bd: Dict[str, Any] = {}

    sma200 = levels.sma200 or levels.sma200_tv
    sma50  = levels.sma50  or levels.sma50_tv

    # ── SMA200 ──────────────────────────────────────────────────────────────
    if spot and sma200:
        if spot > sma200:
            score += 3.0
            reasons.append(f"Above SMA200 (${sma200:.2f}) — long-term trend intact")
            bd["sma200"] = {"value": sma200, "holding": True, "pts": 3.0}
        else:
            reasons.append(f"Below SMA200 (${sma200:.2f}) — bearish")
            bd["sma200"] = {"value": sma200, "holding": False, "pts": 0}

    # ── SMA50 ───────────────────────────────────────────────────────────────
    if spot and sma50:
        if spot > sma50:
            score += 2.0
            reasons.append(f"Above SMA50 (${sma50:.2f}) — medium trend intact")
            bd["sma50"] = {"value": sma50, "holding": True, "pts": 2.0}
        else:
            reasons.append(f"Below SMA50 (${sma50:.2f}) — medium-term bearish")
            bd["sma50"] = {"value": sma50, "holding": False, "pts": 0}

    # ── Swing low ────────────────────────────────────────────────────────────
    if spot and levels.swing_low_20d:
        if spot > levels.swing_low_20d:
            score += 1.5
            reasons.append(f"Above 20d swing low (${levels.swing_low_20d:.2f})")
            bd["swing_low"] = {"value": levels.swing_low_20d, "holding": True, "pts": 1.5}
        else:
            reasons.append(f"Below 20d swing low (${levels.swing_low_20d:.2f}) — breakdown")
            bd["swing_low"] = {"value": levels.swing_low_20d, "holding": False, "pts": 0}

    # ── Put wall ─────────────────────────────────────────────────────────────
    if spot and levels.put_wall:
        if spot > levels.put_wall:
            score += 1.5
            oi_str   = f"OI={levels.put_wall_oi:,}" if levels.put_wall_oi is not None else "OI=?"
            conf_str = levels.put_wall_confidence or "?"
            reasons.append(
                f"Above put wall (${levels.put_wall:.0f}, {oi_str}, {conf_str}) — dealer floor intact"
            )
            bd["put_wall"] = {"value": levels.put_wall, "holding": True, "pts": 1.5}
        else:
            reasons.append(f"Below put wall (${levels.put_wall:.0f}) — significant breakdown")
            bd["put_wall"] = {"value": levels.put_wall, "holding": False, "pts": 0}

    # ── RSI momentum ─────────────────────────────────────────────────────────
    if levels.rsi is not None:
        if levels.rsi > 50:
            score += 1.0
            reasons.append(f"RSI={levels.rsi:.1f} > 50 — buyers in control")
            bd["rsi"] = {"value": levels.rsi, "holding": True, "pts": 1.0}
        else:
            reasons.append(f"RSI={levels.rsi:.1f} ≤ 50 — sellers in control")
            bd["rsi"] = {"value": levels.rsi, "holding": False, "pts": 0}

    # ── Multi-source agreement bonus ─────────────────────────────────────────
    if levels.key_support_confidence == "high":
        score += 0.5
        reasons.append("Multi-source support agreement (high confidence)")

    # ── ADX trend strength ────────────────────────────────────────────────────
    if levels.adx is not None and levels.adx > 20:
        score += 0.5
        reasons.append(f"ADX={levels.adx:.1f} > 20 — trend has strength")

    # ── Final assembly ────────────────────────────────────────────────────────
    levels.hold_score  = max(0, min(10, int(round(score))))
    levels.hold_reasons = reasons
    levels.breakdown    = bd

    # Determine is_holding
    if levels.key_support and spot:
        levels.is_holding = spot > levels.key_support
    elif sma200 and spot:
        levels.is_holding = spot > sma200   # fallback to SMA200
    else:
        levels.is_holding = None


def _compute_data_quality(levels: SupportLevels) -> None:
    """Set data_quality based on how many sources contributed."""
    n = len(levels.sources_used)
    if n >= 3:
        levels.data_quality = "full"
    elif n == 2:
        levels.data_quality = "partial"
    elif n == 1:
        levels.data_quality = "minimal"
    else:
        levels.data_quality = "unavailable"


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

_TV_TO_MM_MARKET = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "BATS": "US", "CBOE": "US",
}

_EXCHANGE_MAP = {
    # Common US exchanges → TV prefix
    "NVDA": "NASDAQ", "AAPL": "NASDAQ", "TSLA": "NASDAQ", "META": "NASDAQ",
    "MSFT": "NASDAQ", "GOOGL": "NASDAQ", "AMZN": "NASDAQ", "NFLX": "NASDAQ",
}


def _clean_ticker(ticker: str) -> str:
    """Strip exchange prefix for yfinance (NYSE:AAPL → AAPL)."""
    if ":" in ticker:
        return ticker.split(":", 1)[1]
    return ticker.strip().upper()


def _ensure_exchange_prefix(ticker: str) -> str:
    """
    Ensure ticker has an exchange prefix for TV scan API.
    NYSE:AAPL → NYSE:AAPL (unchanged)
    AAPL      → NASDAQ:AAPL (best-effort)
    """
    ticker = ticker.strip().upper()
    if ":" in ticker:
        return ticker
    # Try known map first
    exch = _EXCHANGE_MAP.get(ticker)
    if exch:
        return f"{exch}:{ticker}"
    # Default to NASDAQ for unknown tickers
    return f"NASDAQ:{ticker}"


def _tv_to_mm(pro_symbol: str) -> Optional[str]:
    """Convert TradingView pro_symbol, bare ticker, or Moomoo code → Moomoo code."""
    pro_symbol = pro_symbol.strip().upper()
    # Already Moomoo format (e.g. US.NVDA, HK.00700) — return unchanged
    if re.match(r'^[A-Z]{2}\.[A-Z0-9\.]+$', pro_symbol) and '.' in pro_symbol:
        return pro_symbol
    if ":" in pro_symbol:
        exch, ticker = pro_symbol.split(":", 1)
        market = _TV_TO_MM_MARKET.get(exch)
        if not market:
            return None
        return f"{market}.{ticker}"
    # bare ticker — assume US
    return f"US.{pro_symbol}"


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC CONVENIENCE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def support_levels(
    ticker: str,
    *,
    yfinance_fetcher: Optional[SupportSourceFetcher] = None,
    moomoo_fetcher:   Optional[SupportSourceFetcher] = None,
    tv_scan_fetcher:  Optional[SupportSourceFetcher] = None,
    tv_ws_fetcher:    Optional[SupportSourceFetcher] = None,
    timeout_moomoo:   int  = 25,
    verbose:          bool = False,
) -> SupportLevels:
    """
    Compute multi-source support levels for *ticker*.

    This is the primary entry point. Returns a SupportLevels dataclass
    regardless of which data sources are available.

    Args:
        ticker:            Ticker symbol. Accepts bare (NVDA), TV-prefixed
                           (NASDAQ:NVDA), or Moomoo-prefixed (US.NVDA).
        yfinance_fetcher:  Injectable replacement for YFinanceFetcher.
        moomoo_fetcher:    Injectable replacement for MoomooPutWallFetcher.
        tv_scan_fetcher:   Injectable replacement for TVScanFetcher.
        tv_ws_fetcher:     Injectable replacement for TVWebSocketFetcher.
        timeout_moomoo:    Seconds before Moomoo call is abandoned (default 25).
        verbose:           Print diagnostic messages.

    Returns:
        SupportLevels — never None, never raises.

    Example:
        levels = support_levels("NVDA")
        if levels.data_quality != "unavailable":
            print(levels.summary())
            if levels.is_holding:
                print(f"✅ NVDA holding above ${levels.key_support:.2f}")
    """
    fetcher = SupportLevelsFetcher(
        yfinance        = yfinance_fetcher,
        moomoo          = moomoo_fetcher,
        tv_scan         = tv_scan_fetcher,
        tv_ws           = tv_ws_fetcher,
        timeout_moomoo  = timeout_moomoo,
        verbose         = verbose,
    )
    return fetcher.fetch(ticker)
