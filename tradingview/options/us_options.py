"""
US Options Fetcher — Moomoo OpenD API
======================================
Fetches US equity option chains from Moomoo OpenD using a two-step API:

  Step 1: get_option_chain(code, start, end)
    → Returns ONLY static contract metadata (code, option_type, strike_price,
      strike_time). Does NOT return bid/ask/Greeks/IV/OI.

  Step 2: get_market_snapshot(code_list)
    → Returns live market data with option_* prefixed columns:
        bid_price, ask_price, last_price, volume, after_volume,
        option_implied_volatility (PERCENTAGE, e.g. 43.171),
        option_delta, option_gamma, option_theta, option_vega, option_open_interest,
        update_time (last update timestamp for staleness detection)

Requirements:
  • Moomoo OpenD running on 127.0.0.1:11111 (AU) or 11112 (SG)
  • pip install moomoo-api
  • Data is live during US market hours; snapshot persists last-close values
    after hours (use update_time for staleness detection)

Usage:
  from options.us_options import USOptionsFetcher
  fetcher = USOptionsFetcher()
  if fetcher.is_available():
      contracts = fetcher.get_calls("US.AAPL", n_expiries=2)
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

# ─── Moomoo TV symbol → Moomoo code ──────────────────────────────────────────
TV_TO_MM_MARKET = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "BATS": "US", "CBOE": "US",
}


def tv_to_mm(pro_symbol: str) -> Optional[str]:
    """Convert TradingView pro_symbol (NYSE:AAPL) → Moomoo code (US.AAPL)."""
    if "." not in pro_symbol and ":" not in pro_symbol:
        return f"US.{pro_symbol}"   # bare ticker assumed US
    if ":" in pro_symbol:
        exch, ticker = pro_symbol.split(":", 1)
        market = TV_TO_MM_MARKET.get(exch.upper())
        if not market:
            return None             # non-US market → no Moomoo options
        return f"{market}.{ticker}"
    return None


class USOptionsFetcher:
    """
    Wraps Moomoo OpenD option chain API for US equities.

    All methods return empty lists / None on failure — the caller must handle
    unavailability gracefully (OpenD not running, rights not available, etc.).
    """

    # Resolved lazily via moomoo_config.quote_port() so both AU and SG
    # instances are naturally handled — whichever is running serves quotes.
    OPEND_HOST = "127.0.0.1"
    OPEND_PORT = None   # None → resolved at connect time

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._ctx    = None
        self._available: Optional[bool] = None

    # ─── Connection ────────────────────────────────────────────────────────────

    def _connect(self) -> bool:
        """Open Moomoo quote context. Returns True on success (max 10s)."""
        if self._ctx is not None:
            return True
        try:
            import sys as _sys, os as _os
            _root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from moomoo_config import is_opend_running, OPEND_HOST, OPEND_AU_PORT, OPEND_SG_PORT
            # Quick pre-check: bail out fast if no OpenD is reachable (2s TCP check)
            port = None
            for p in (OPEND_AU_PORT, OPEND_SG_PORT):
                if is_opend_running(p):
                    port = p
                    break
            if port is None:
                if self.verbose:
                    print("[USOptions] No OpenD instance reachable on 11111/11112. Run: .\\start_opend.ps1")
                return False
            from moomoo import OpenQuoteContext
            self._ctx = OpenQuoteContext(host=OPEND_HOST, port=port)
            return True
        except Exception as exc:
            if self.verbose:
                print(f"[USOptions] Cannot connect to OpenD: {exc}")
            return False

    def _close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    def is_available(self) -> bool:
        """Return True if Moomoo OpenD is reachable."""
        if self._available is not None:
            return self._available
        self._available = self._connect()
        if not self._available:
            self._close()
        return self._available

    # ─── Spot price ─────────────────────────────────────────────────────────────

    def get_spot_price(self, mm_code: str) -> Optional[float]:
        """Fetch current last-traded price for an underlying stock."""
        if not self._connect():
            return None
        try:
            from moomoo import RET_OK
            ret, df = self._ctx.get_market_snapshot([mm_code])
            if ret == RET_OK and hasattr(df, "empty") and not df.empty:
                val = df["last_price"].iloc[0]
                return float(val) if val else None
        except Exception as exc:
            if self.verbose:
                print(f"[USOptions] get_spot_price({mm_code}): {exc}")
        return None

    # ─── Expiry dates ───────────────────────────────────────────────────────────

    def get_expiry_dates(self, mm_code: str) -> list[str]:
        """
        Return sorted list of available option expiry dates for *mm_code*.
        Filters to dates between 5 days and 90 days out (optimal trading window).
        """
        if not self._connect():
            return []
        try:
            from moomoo import RET_OK
            ret, df = self._ctx.get_option_expiration_date(code=mm_code)
            if ret != RET_OK or not hasattr(df, "empty") or df.empty:
                return []
            today     = datetime.today().date()
            min_days  = 5
            max_days  = 90
            dates = []
            for _, row in df.iterrows():
                date_str = str(row.get("strike_time") or row.get("expiry_date") or "")
                if not date_str:
                    continue
                try:
                    exp_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                    diff = (exp_date - today).days
                    if min_days <= diff <= max_days:
                        dates.append(date_str[:10])
                except ValueError:
                    continue
            return sorted(set(dates))
        except Exception as exc:
            if self.verbose:
                print(f"[USOptions] get_expiry_dates({mm_code}): {exc}")
            return []

    # ─── Option chain ───────────────────────────────────────────────────────────

    def get_option_chain_for_date(
        self,
        mm_code: str,
        expiry_date: str,
        option_type: str = "CALL",
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 13,
    ) -> list[dict]:
        """
        Fetch option chain for a single expiry date using two-step API:
        1. get_option_chain()    → option codes + static metadata (strike, type, expiry)
        2. get_market_snapshot() → live market data (bid/ask/IV/Greeks/OI/volume)

        Performance: filters to CALL-only and near-ATM strikes before calling
        get_market_snapshot() (max 400 codes per call).  Without this filter,
        NVDA has 400+ codes per expiry and takes 7+ minutes.  With the filter,
        it returns in < 5 seconds.

        Parameters
        ----------
        option_type        : "CALL" or "PUT" (default: "CALL") — fetch only this type
        spot_price         : underlying price; if provided, filter to ±strikes_around_atm%
        strikes_around_atm : ATM filter half-width in % (default 13 → ±13% + 5% buffer)

        Snapshot column names (confirmed via probe):
          option_strike_price     ← strike (NOT strike_price)
          option_type             ← 'CALL' or 'PUT'
          strike_time             ← expiry date e.g. '2026-08-21'
          bid_price, ask_price    ← bid/ask
          option_implied_volatility ← PERCENTAGE (e.g. 43.171, NOT decimal 0.43)
          option_delta, option_gamma, option_theta, option_vega, option_rho
          option_open_interest    ← open interest (real data!)
          volume                  ← intraday volume (from snapshot)
          update_time             ← last update timestamp for staleness detection
        """
        if not self._connect():
            return []
        try:
            from moomoo import RET_OK

            # Step 1: Get option codes from chain (static metadata only)
            ret, df_chain = self._ctx.get_option_chain(
                code=mm_code,
                start=expiry_date,
                end=expiry_date,
            )
            if ret != RET_OK or not hasattr(df_chain, "empty") or df_chain.empty:
                return []

            # ── Filter 1: CALL-only (halves snapshot load vs fetching call+put) ──
            opt_type_upper = option_type.strip().upper()
            if "option_type" in df_chain.columns:
                df_chain = df_chain[
                    df_chain["option_type"].str.upper() == opt_type_upper
                ]

            # ── Filter 2: Near-ATM strike range (10× reduction for NVDA-scale chains) ──
            # Without this, NVDA has 200+ CALL codes from $50 to $700;
            # with ±13%+5% buffer we keep only the tradeable strikes (~30-50 codes).
            if spot_price and spot_price > 0:
                pct = max(strikes_around_atm, 5) / 100.0
                buf = 0.05
                lo = spot_price * (1.0 - pct - buf)
                hi = spot_price * (1.0 + pct + buf)
                strike_col = next(
                    (c for c in ("option_strike_price", "strike_price", "strike")
                     if c in df_chain.columns),
                    None,
                )
                if strike_col:
                    df_chain = df_chain[
                        df_chain[strike_col].between(lo, hi, inclusive="both")
                    ]

            codes = df_chain["code"].dropna().tolist()
            if not codes:
                if self.verbose:
                    print(f"[USOptions] No codes after filters for {mm_code} {expiry_date} {opt_type_upper}")
                return []

            # Step 2: Get real market data from snapshot in batches of 200
            # (API limit is 400 but smaller batches avoid rate-limit delays).
            # The snapshot already includes option_type, strike_time, and
            # option_strike_price — no merge with chain df is needed.
            _BATCH = 200
            all_rows = []
            for i in range(0, len(codes), _BATCH):
                batch = codes[i : i + _BATCH]
                ret2, df_snap_batch = self._ctx.get_market_snapshot(batch)
                if ret2 != RET_OK or not hasattr(df_snap_batch, "empty") or df_snap_batch.empty:
                    if self.verbose:
                        print(
                            f"[USOptions] get_market_snapshot batch {i//200+1} failed "
                            f"({len(batch)} codes): ret={ret2}"
                        )
                    continue
                all_rows.append(df_snap_batch)

            if not all_rows:
                if self.verbose:
                    print(f"[USOptions] No snapshot data for {mm_code} {expiry_date}")
                return []

            import pandas as _pd
            df_snap = _pd.concat(all_rows, ignore_index=True)

            def _safe_float(val, default: float = 0.0) -> float:
                """NaN-safe float: `float('nan') or 0` returns NaN (NaN is truthy)."""
                try:
                    f = float(val)
                    return default if f != f else f   # f != f is True only for NaN
                except (TypeError, ValueError):
                    return default

            contracts = []
            for _, row in df_snap.iterrows():
                opt_type = str(row.get("option_type", "")).upper()
                if opt_type not in ("CALL", "PUT"):
                    continue

                # Snapshot uses option_strike_price (NOT strike_price)
                strike_raw = row.get("option_strike_price") or row.get("strike_price", 0)
                expiry_str = str(row.get("strike_time", expiry_date))[:10]

                contracts.append({
                    "code":        str(row.get("code", "")),
                    "strike":      _safe_float(strike_raw),
                    "expiry":      expiry_str,
                    "option_type": opt_type,
                    "bid":         _safe_float(row.get("bid_price")),
                    "ask":         _safe_float(row.get("ask_price")),
                    "last_price":  _safe_float(row.get("last_price")),
                    "volume":      int(_safe_float(row.get("volume"))),
                    "oi":          int(_safe_float(row.get("option_open_interest"))),
                    # option_implied_volatility is PERCENTAGE (e.g. 43.171), not decimal
                    "iv":          _safe_float(row.get("option_implied_volatility")),
                    "delta":       _safe_float(row.get("option_delta")),
                    "gamma":       _safe_float(row.get("option_gamma")),
                    "theta":       _safe_float(row.get("option_theta")),
                    "vega":        _safe_float(row.get("option_vega")),
                    "market":      "US",
                    "source":      "moomoo",
                })
            return contracts

        except Exception as exc:
            if self.verbose:
                print(f"[USOptions] get_option_chain_for_date({mm_code}, {expiry_date}): {exc}")
            traceback.print_exc()
            return []

    def get_calls(
        self,
        mm_code: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
    ) -> list[dict]:
        """
        Fetch CALL contracts across the nearest *n_expiries* expiry dates.

        Parameters
        ----------
        mm_code : str
            Moomoo stock code, e.g. "US.AAPL".
        n_expiries : int
            Number of expiry dates to fetch (default 3 = weekly + monthly + next monthly).
        spot_price : float, optional
            Current underlying price. Used to filter strikes around ATM.
            Fetched automatically if not provided.
        strikes_around_atm : int
            Keep only *n* strikes above and below ATM (default 8 = 17 total).

        Returns
        -------
        list[dict]
            All call contracts with market metadata attached.
        """
        expiries = self.get_expiry_dates(mm_code)[:n_expiries]
        if not expiries:
            return []

        if spot_price is None:
            spot_price = self.get_spot_price(mm_code)

        all_calls: list[dict] = []
        for exp_date in expiries:
            chain = self.get_option_chain_for_date(mm_code, exp_date)
            calls = [c for c in chain if c.get("option_type", "").upper() == "CALL"]

            # Filter strikes around ATM if spot_price is available
            if spot_price and spot_price > 0 and calls:
                sorted_calls = sorted(calls, key=lambda c: abs(c["strike"] - spot_price))
                calls = sorted_calls[:strikes_around_atm * 2 + 1]

            # Add DTE and midpoint price
            today = datetime.today().date()
            for c in calls:
                try:
                    exp_d = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
                    c["dte"] = (exp_d - today).days
                except (ValueError, KeyError):
                    c["dte"] = 30
                # Mid-price for premium estimate
                b, a = c.get("bid", 0), c.get("ask", 0)
                c["mid_price"] = round((b + a) / 2, 2) if (a > 0) else c.get("last_price", 0)
                # Spread as % of mid (liquidity proxy)
                c["spread_pct"] = round((a - b) / max(c["mid_price"], 0.01) * 100, 2) if c["mid_price"] > 0 else 999.0
                # Moneyness: (strike - spot) / spot * 100 (positive = OTM, negative = ITM)
                if spot_price:
                    c["moneyness_pct"] = round((c["strike"] - spot_price) / spot_price * 100, 2)
                else:
                    c["moneyness_pct"] = 0.0
                c["spot_price"] = spot_price or 0

            all_calls.extend(calls)

        self._close()
        return all_calls

    def close(self) -> None:
        """Explicitly close the OpenD connection."""
        self._close()


# ── Module-level shared OpenD connection (prevents 2-5s reconnect per stock) ─
# Using a singleton means the first stock in a scan pays the connection cost;
# all subsequent stocks reuse the same TCP socket to OpenD port 11111.

import threading as _threading

_SHARED_FETCHER: "USOptionsFetcher | None" = None
_SHARED_FETCHER_LOCK = _threading.Lock()


def get_shared_fetcher(verbose: bool = False) -> "USOptionsFetcher":
    """Return a module-level shared :class:`USOptionsFetcher` that reuses the OpenD TCP connection.

    Thread-safe: protected by ``_SHARED_FETCHER_LOCK``.
    On success the same instance is returned for every call in the process lifetime.
    On timeout (see :func:`invalidate_shared_fetcher`) a fresh instance is created next call.
    """
    global _SHARED_FETCHER
    with _SHARED_FETCHER_LOCK:
        if _SHARED_FETCHER is None:
            _SHARED_FETCHER = USOptionsFetcher(verbose=verbose)
        return _SHARED_FETCHER


def invalidate_shared_fetcher() -> None:
    """Close and discard the shared fetcher.

    Call this whenever a Moomoo worker times out — the background thread may
    still be holding a reference to the shared ``OpenQuoteContext``, so we
    swap it out under the lock and close the old one off-thread.
    """
    global _SHARED_FETCHER
    with _SHARED_FETCHER_LOCK:
        f, _SHARED_FETCHER = _SHARED_FETCHER, None
    if f is not None:
        try:
            f.close()
        except Exception:
            pass


def close_shared_fetcher() -> None:
    """Alias for :func:`invalidate_shared_fetcher` — call after a full scan completes."""
    invalidate_shared_fetcher()
