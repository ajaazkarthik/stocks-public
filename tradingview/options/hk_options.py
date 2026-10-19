"""
HK Options Fetcher — Moomoo OpenD API
======================================
Fetches real-time Hong Kong equity option chains from Moomoo OpenD.

HKEX stock options are American-style, physically settled.
~69 stocks are eligible; contract sizes vary (100–2000 shares).
Expiries: monthly (3rd Friday) + weekly on select names (700, 5, 388, 941).

Data per contract:
  strike, expiry, bid, ask, last_price, volume, open_interest,
  implied_volatility, delta, gamma, theta, vega

Requirements:
  • Moomoo OpenD running on 127.0.0.1 (AU or SG instance)
  • pip install moomoo-api
  • HK market hours for live data; delayed otherwise

Moomoo code format for HK:
  HKEX:700   → HK.00700
  HKEX:5     → HK.00005
  HKEX:9988  → HK.09988

Usage:
  from options.hk_options import HKOptionsFetcher, tv_to_mm_hk
  fetcher = HKOptionsFetcher()
  if fetcher.is_available():
      contracts = fetcher.get_calls("HK.00700", n_expiries=2)
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Optional

# ─── HK equity stocks eligible for options trading on HKEX ──────────────────
# Source: HKEX Stock Options — approximately 69 names (~July 2024)
HK_OPTIONABLE_STOCKS: frozenset[str] = frozenset({
    "00001", "00002", "00003", "00005", "00006", "00011", "00012",
    "00016", "00017", "00019", "00023", "00027", "00066", "00083",
    "00101", "00135", "00175", "00177", "00267", "00268", "00270",
    "00285", "00293", "00303", "00388", "00489", "00552", "00656",
    "00688", "00700", "00762", "00823", "00836", "00857", "00883",
    "00916", "00939", "00941", "00960", "01038", "01044", "01093",
    "01109", "01113", "01177", "01199", "01211", "01299", "01398",
    "01810", "01928", "01988", "02007", "02018", "02020", "02238",
    "02318", "02382", "02388", "02628", "03328", "03690", "03968",
    "03988", "06098", "06862", "09618", "09888", "09988", "09999",
})

# ─── TradingView exchange → Moomoo market prefix ────────────────────────────
HK_TV_EXCHANGES = {"HKEX", "HKEX_DLY"}


def tv_to_mm_hk(pro_symbol: str) -> Optional[str]:
    """
    Convert TradingView pro_symbol → Moomoo HK code.

    Examples
    --------
    tv_to_mm_hk("HKEX:700")     → "HK.00700"
    tv_to_mm_hk("HKEX_DLY:700") → "HK.00700"
    tv_to_mm_hk("HKEX:9988")    → "HK.09988"
    tv_to_mm_hk("HKEX:5")       → "HK.00005"
    tv_to_mm_hk("NYSE:AAPL")    → None        (not HK)
    """
    if ":" not in pro_symbol:
        return None
    exch, ticker = pro_symbol.split(":", 1)
    if exch.upper() not in HK_TV_EXCHANGES:
        return None
    # Zero-pad ticker to 5 digits (HK stock codes are 1–5 digit integers)
    try:
        code = str(int(ticker)).zfill(5)
    except ValueError:
        return None
    return f"HK.{code}"


def is_hk_optionable(pro_symbol: str) -> bool:
    """Return True if the HK symbol has options available on HKEX."""
    mm_code = tv_to_mm_hk(pro_symbol)
    if not mm_code:
        return False
    stock_num = mm_code.split(".", 1)[-1]   # e.g. "00700"
    return stock_num in HK_OPTIONABLE_STOCKS


class HKOptionsFetcher:
    """
    Wraps Moomoo OpenD option chain API for HK equities.

    All methods return empty lists / None on failure — the caller must handle
    unavailability gracefully (OpenD not running, rights not available, etc.).

    The Moomoo option chain API is identical for HK and US; only the code
    prefix differs (HK.XXXXX vs US.AAPL).
    """

    OPEND_HOST = "127.0.0.1"
    OPEND_PORT = None   # resolved at connect time via moomoo_config

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._ctx:            object | None = None
        self._available:      Optional[bool] = None

    # ─── Connection ──────────────────────────────────────────────────────────

    def _connect(self) -> bool:
        """Open Moomoo quote context. Returns True on success."""
        if self._ctx is not None:
            return True
        try:
            import sys as _sys, os as _os
            _root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from moomoo import OpenQuoteContext
            from moomoo_config import quote_port, OPEND_HOST
            try:
                port = quote_port()
            except RuntimeError:
                port = 11111
            self._ctx = OpenQuoteContext(host=OPEND_HOST, port=port)
            return True
        except Exception as exc:
            if self.verbose:
                print(f"[HKOptions] Cannot connect to OpenD: {exc}")
            return False

    def _close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()   # type: ignore[union-attr]
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

    # ─── Spot price ──────────────────────────────────────────────────────────

    def get_spot_price(self, mm_code: str) -> Optional[float]:
        """Fetch current last-traded price for an underlying HK stock."""
        if not self._connect():
            return None
        try:
            from moomoo import RET_OK
            ret, df = self._ctx.get_market_snapshot([mm_code])  # type: ignore[union-attr]
            if ret == RET_OK and df is not None and not df.empty:
                val = df["last_price"].iloc[0]
                return float(val) if val else None
        except Exception as exc:
            if self.verbose:
                print(f"[HKOptions] get_spot_price({mm_code}): {exc}")
        return None

    # ─── Expiry dates ────────────────────────────────────────────────────────

    def get_expiry_dates(self, mm_code: str) -> list[str]:
        """
        Return sorted list of available option expiry dates for *mm_code*.
        Filters to dates between 5 days and 90 days out.
        """
        if not self._connect():
            return []
        try:
            from moomoo import RET_OK
            ret, df = self._ctx.get_option_expiration_date(code=mm_code)  # type: ignore[union-attr]
            if ret != RET_OK or df is None or df.empty:
                return []
            today    = datetime.today().date()
            min_days = 5
            max_days = 90
            dates: list[str] = []
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
                print(f"[HKOptions] get_expiry_dates({mm_code}): {exc}")
            return []

    # ─── Option chain ────────────────────────────────────────────────────────

    def get_option_chain_for_date(self, mm_code: str, expiry_date: str) -> list[dict]:
        """
        Fetch option chain for a single expiry date.
        Returns raw list of contract dicts (both calls and puts).
        """
        if not self._connect():
            return []
        try:
            from moomoo import RET_OK
            ret, df = self._ctx.get_option_chain(          # type: ignore[union-attr]
                code=mm_code,
                start=expiry_date,
                end=expiry_date,
            )
            if ret != RET_OK or df is None or df.empty:
                return []

            contracts: list[dict] = []
            col_map = {c.lower(): c for c in df.columns}

            def _get(row, *keys):
                for k in keys:
                    v = row.get(col_map.get(k.lower(), k))
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return v
                return None

            for _, row in df.iterrows():
                opt_type = str(_get(row, "option_type") or "").upper()
                if opt_type not in ("CALL", "PUT"):
                    call_code = _get(row, "call_option_code", "call_code")
                    if call_code:
                        opt_type = "CALL"
                    else:
                        continue

                contracts.append({
                    "code":        str(row.get(col_map.get("stock_code", "stock_code"), "")),
                    "strike":      float(_get(row, "strike_price") or 0),
                    "expiry":      expiry_date,
                    "option_type": opt_type,
                    "bid":         float(_get(row, "bid_price") or 0),
                    "ask":         float(_get(row, "ask_price") or 0),
                    "last_price":  float(_get(row, "last_price", "cur_price") or 0),
                    "volume":      int(_get(row, "volume") or 0),
                    "oi":          int(_get(row, "open_interest") or 0),
                    "iv":          float(_get(row, "implied_volatility") or 0),
                    "delta":       float(_get(row, "delta") or 0),
                    "gamma":       float(_get(row, "gamma") or 0),
                    "theta":       float(_get(row, "theta") or 0),
                    "vega":        float(_get(row, "vega") or 0),
                    "market":      "HK",
                })
            return contracts

        except Exception as exc:
            if self.verbose:
                print(f"[HKOptions] get_option_chain_for_date({mm_code}, {expiry_date}): {exc}")
            traceback.print_exc()
            return []

    def get_calls(
        self,
        mm_code: str,
        n_expiries: int = 2,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 6,
    ) -> list[dict]:
        """
        Fetch CALL contracts across the nearest *n_expiries* expiry dates.

        Parameters
        ----------
        mm_code : str
            Moomoo HK stock code, e.g. "HK.00700".
        n_expiries : int
            Number of expiry dates to fetch (default 2).
        spot_price : float, optional
            Current underlying price. Fetched automatically if not provided.
        strikes_around_atm : int
            Keep only *n* strikes above and below ATM (default 6).

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

            if spot_price and spot_price > 0 and calls:
                sorted_calls = sorted(calls, key=lambda c: abs(c["strike"] - spot_price))
                calls = sorted_calls[:strikes_around_atm * 2 + 1]

            today = datetime.today().date()
            for c in calls:
                try:
                    exp_d = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
                    c["dte"] = (exp_d - today).days
                except (ValueError, KeyError):
                    c["dte"] = 30
                b, a = c.get("bid", 0), c.get("ask", 0)
                c["mid_price"] = round((b + a) / 2, 2) if (a > 0) else c.get("last_price", 0)
                c["spread_pct"] = (
                    round((a - b) / max(c["mid_price"], 0.01) * 100, 2)
                    if c["mid_price"] > 0 else 999.0
                )
                if spot_price:
                    c["moneyness_pct"] = round((c["strike"] - spot_price) / spot_price * 100, 2)
                else:
                    c["moneyness_pct"] = 0.0
                c["spot_price"] = spot_price or 0

            all_calls.extend(calls)

        self._close()
        return all_calls

    def get_puts(
        self,
        mm_code: str,
        n_expiries: int = 2,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 6,
    ) -> list[dict]:
        """
        Fetch PUT contracts across the nearest *n_expiries* expiry dates.

        Parameters
        ----------
        mm_code : str
            Moomoo HK stock code, e.g. "HK.00700".
        n_expiries : int
            Number of expiry dates to fetch (default 2).
        spot_price : float, optional
            Current underlying price. Fetched automatically if not provided.
        strikes_around_atm : int
            Keep only *n* strikes above and below ATM (default 6).

        Returns
        -------
        list[dict]
            All put contracts with market metadata attached.
        """
        expiries = self.get_expiry_dates(mm_code)[:n_expiries]
        if not expiries:
            return []

        if spot_price is None:
            spot_price = self.get_spot_price(mm_code)

        all_puts: list[dict] = []
        for exp_date in expiries:
            chain = self.get_option_chain_for_date(mm_code, exp_date)
            puts = [c for c in chain if c.get("option_type", "").upper() == "PUT"]

            if spot_price and spot_price > 0 and puts:
                sorted_puts = sorted(puts, key=lambda c: abs(c["strike"] - spot_price))
                puts = sorted_puts[:strikes_around_atm * 2 + 1]

            today = datetime.today().date()
            for c in puts:
                try:
                    exp_d = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
                    c["dte"] = (exp_d - today).days
                except (ValueError, KeyError):
                    c["dte"] = 30
                b, a = c.get("bid", 0), c.get("ask", 0)
                c["mid_price"] = round((b + a) / 2, 2) if (a > 0) else c.get("last_price", 0)
                c["spread_pct"] = (
                    round((a - b) / max(c["mid_price"], 0.01) * 100, 2)
                    if c["mid_price"] > 0 else 999.0
                )
                if spot_price:
                    # For puts: OTM = positive moneyness (spot > strike)
                    c["moneyness_pct"] = round((spot_price - c["strike"]) / spot_price * 100, 2)
                else:
                    c["moneyness_pct"] = 0.0
                c["spot_price"] = spot_price or 0

            all_puts.extend(puts)

        self._close()
        return all_puts

    def close(self) -> None:
        """Explicitly close the OpenD connection."""
        self._close()
