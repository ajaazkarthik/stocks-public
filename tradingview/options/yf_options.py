"""
yfinance Options Fetcher (US Equities) — fallback when Moomoo OpenD is offline
==============================================================================
Returns option chain contracts in the same dict format as us_options.py so
either fetcher can be passed to OptionsAnalyzer.

Greeks (delta/gamma/theta/vega) are NOT provided by yfinance; they are
computed here from the Black-Scholes model using:
  • impliedVolatility from yfinance (already calibrated to market price)
  • T = DTE / 365 years
  • r = 4.5% risk-free rate (US T-bill 2025-2026)

Black-Scholes call delta:
  d1 = [ln(S/K) + (r + σ²/2)·T] / (σ·√T)
  delta = N(d1)   where N is the standard normal CDF

Usage:
  from options.yf_options import YFOptionsFetcher
  fetcher = YFOptionsFetcher()
  if fetcher.is_available():
      contracts = fetcher.get_calls("MU", n_expiries=2)
      # or with full TV symbol:
      contracts = fetcher.get_calls("NASDAQ:MU", n_expiries=2)
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

# Risk-free rate in decimal (US T-bill, approximate for 2025-2026)
_RISK_FREE_RATE = 0.045


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via erf. Accurate to 7 decimal places."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(spot: float, strike: float, dte_days: int, sigma: float,
              r: float = _RISK_FREE_RATE) -> float:
    """
    Black-Scholes call delta.

    Parameters
    ----------
    spot : float     Current underlying price.
    strike : float   Option strike price.
    dte_days : int   Days to expiry.
    sigma : float    Implied volatility as a decimal (0.65 = 65%).
    r : float        Risk-free rate as a decimal.

    Returns
    -------
    float : call delta in [0, 1]; returns 0.50 on degenerate inputs.
    """
    if dte_days <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.50
    T = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return round(_normal_cdf(d1), 4)
    except (ValueError, ZeroDivisionError):
        return 0.50


def _tv_to_ticker(pro_symbol: str) -> str:
    """
    Convert TradingView pro_symbol → plain ticker for yfinance.
    NASDAQ:AAPL → AAPL,  NYSE:GLD → GLD,  BATS:SPY → SPY, MU → MU.
    """
    if ":" in pro_symbol:
        return pro_symbol.split(":", 1)[1].strip()
    return pro_symbol.strip()


_TV_US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "BATS", "CBOE", "ARCA"}


def is_us_symbol(pro_symbol: str) -> bool:
    """Return True if the TradingView symbol is a US equity."""
    if ":" not in pro_symbol:
        return True   # bare ticker assumed US
    exch = pro_symbol.split(":", 1)[0].upper()
    return exch in _TV_US_EXCHANGES


class YFOptionsFetcher:
    """
    yfinance-based US option chain fetcher.

    Output contract format is identical to USOptionsFetcher so OptionsAnalyzer
    can use whichever source is available.

    Includes:
      - Black-Scholes delta computation
      - Moneyness % relative to spot
      - DTE, mid price, spread_pct
      - Source tag "yfinance" (not "moomoo") for badge display
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        """Return True if yfinance is importable."""
        if self._available is not None:
            return self._available
        try:
            import yfinance  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
        return self._available

    def get_spot_price(self, pro_symbol: str) -> Optional[float]:
        """Fetch last price for the underlying via yfinance fast_info."""
        if not self.is_available():
            return None
        ticker = _tv_to_ticker(pro_symbol)
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            val = t.fast_info.last_price
            return float(val) if val else None
        except Exception as exc:
            if self.verbose:
                print(f"[YFOptions] get_spot_price({ticker}): {exc}")
            return None

    def get_expiry_dates(self, pro_symbol: str) -> list[str]:
        """
        Return available expiry dates for the symbol, filtered to 5–90 days out.
        """
        if not self.is_available():
            return []
        ticker = _tv_to_ticker(pro_symbol)
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            today = datetime.today().date()
            result: list[str] = []
            for exp in t.options:
                try:
                    exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
                    diff = (exp_d - today).days
                    if 5 <= diff <= 90:
                        result.append(exp)
                except ValueError:
                    continue
            return sorted(result)
        except Exception as exc:
            if self.verbose:
                print(f"[YFOptions] get_expiry_dates({ticker}): {exc}")
            return []

    def get_option_chain_for_date(
        self,
        pro_symbol: str,
        expiry_date: str,
        spot_price: Optional[float] = None,
        option_type: str = "CALL",
    ) -> list[dict]:
        """
        Fetch option contracts for a single expiry date.

        Parameters
        ----------
        pro_symbol : str    TradingView symbol e.g. "NASDAQ:MU" or "MU".
        expiry_date : str   Expiry date string "YYYY-MM-DD".
        spot_price : float  Current underlying price (used for delta + moneyness).
        option_type : str   "CALL" or "PUT" (default "CALL").
        """
        option_type = option_type.upper()
        if not self.is_available():
            return []
        ticker = _tv_to_ticker(pro_symbol)
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            chain = t.option_chain(expiry_date)
            calls_df = chain.puts if option_type == "PUT" else chain.calls

            if calls_df is None or calls_df.empty:
                return []

            today = datetime.today().date()
            try:
                exp_d = datetime.strptime(expiry_date, "%Y-%m-%d").date()
                dte = max(0, (exp_d - today).days)
            except ValueError:
                dte = 30

            if spot_price is None or spot_price <= 0:
                try:
                    spot_price = float(t.fast_info.last_price) or 0
                except Exception:
                    spot_price = 0.0

            def _safe_float(v, default: float = 0.0) -> float:
                """Convert pandas cell to float, treating NaN/None as default."""
                if v is None:
                    return default
                try:
                    f = float(v)
                    return default if (math.isnan(f) or math.isinf(f)) else f
                except (ValueError, TypeError):
                    return default

            def _safe_int(v, default: int = 0) -> int:
                """Convert pandas cell to int, treating NaN/None as 0."""
                return int(_safe_float(v, float(default)))

            contracts: list[dict] = []
            for _, row in calls_df.iterrows():
                strike = _safe_float(row.get("strike", 0))
                bid    = _safe_float(row.get("bid", 0))
                ask    = _safe_float(row.get("ask", 0))
                last   = _safe_float(row.get("lastPrice", 0))
                vol    = _safe_int(row.get("volume", 0))
                oi     = _safe_int(row.get("openInterest", 0))
                # yfinance IV is already a decimal fraction (e.g. 0.69 = 69%)
                iv_raw = float(row.get("impliedVolatility", 0) or 0)
                # Convert to percentage (same as Moomoo format)
                iv_pct = round(iv_raw * 100, 1)

                if strike <= 0:
                    continue

                mid_price = round((bid + ask) / 2.0, 2) if ask > 0 else last
                spread_pct = (
                    round((ask - bid) / max(mid_price, 0.01) * 100, 2)
                    if mid_price > 0 else 999.0
                )
                if option_type == "PUT":
                    moneyness_pct = (
                        round((spot_price - strike) / spot_price * 100, 2)
                        if spot_price > 0 else 0.0
                    )
                else:
                    moneyness_pct = (
                        round((strike - spot_price) / spot_price * 100, 2)
                        if spot_price > 0 else 0.0
                    )

                # Black-Scholes delta using calibrated IV
                call_delta = _bs_delta(spot_price, strike, dte, iv_raw)
                delta = call_delta - 1.0 if option_type == "PUT" else call_delta

                _code_suffix = "P" if option_type == "PUT" else "C"
                contracts.append({
                    "code":        f"{ticker}{expiry_date}{strike:.0f}{_code_suffix}",
                    "strike":      strike,
                    "expiry":      expiry_date,
                    "option_type": option_type,
                    "bid":         bid,
                    "ask":         ask,
                    "last_price":  last,
                    "mid_price":   mid_price,
                    "volume":      vol,
                    "oi":          oi,
                    "iv":          iv_pct,          # in % (like Moomoo)
                    "delta":       delta,           # B-S computed
                    "gamma":       0.0,             # not provided by yfinance
                    "theta":       0.0,
                    "vega":        0.0,
                    "dte":         dte,
                    "spread_pct":  spread_pct,
                    "moneyness_pct": moneyness_pct,
                    "spot_price":  spot_price,
                    "market":      "US",
                    "source":      "yfinance",
                    "iv_source":   "yfinance",   # overwritten below if ATM-substituted
                })

            # ------------------------------------------------------------------ #
            # ATM IV substitution — fix the "zero-bid" / OTM stale data problem  #
            # yfinance computes IV from mid_price = (bid+ask)/2. For OTM calls    #
            # bid=0 is common, so mid = ask/2 and the BS back-calculation gives   #
            # garbage low IV (e.g. 3.8% for a stock with 40% real vol).           #
            #                                                                     #
            # Correction: use the median IV of the 3 nearest-ATM strikes as a     #
            # reference. Any contract whose IV < 50% of ATM IV has bad data and   #
            # is replaced with ATM IV (and delta recomputed).                     #
            # OTM options should have EQUAL or HIGHER IV than ATM (vol smile),    #
            # so OTM_IV < ATM_IV always means bad data, not real skew.            #
            # ------------------------------------------------------------------ #
            if spot_price > 0 and contracts:
                atm_sorted = sorted(contracts, key=lambda c: abs(c["strike"] - spot_price))
                # Use up to 3 contracts closest to ATM with non-garbage IV (>5%)
                atm_ivs = [c["iv"] for c in atm_sorted[:5] if c["iv"] > 5.0]
                if atm_ivs:
                    atm_iv_ref = sorted(atm_ivs)[len(atm_ivs) // 2]  # median
                    iv_floor = max(atm_iv_ref * 0.5, 15.0)  # at least 15% absolute floor
                    for c in contracts:
                        if c["iv"] < iv_floor:
                            c["iv"] = round(atm_iv_ref, 1)
                            c["iv_source"] = "yfinance_atm_sub"  # flagged: substituted, not direct
                            # Recompute delta with corrected IV
                            _cd = _bs_delta(
                                c["spot_price"], c["strike"],
                                c["dte"], atm_iv_ref / 100.0,
                            )
                            c["delta"] = _cd - 1.0 if option_type == "PUT" else _cd

            return contracts

        except Exception as exc:
            if self.verbose:
                print(f"[YFOptions] get_option_chain_for_date({ticker}, {expiry_date}): {exc}")
            return []

    def get_calls(
        self,
        pro_symbol: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
    ) -> list[dict]:
        """
        Fetch CALL contracts across the nearest *n_expiries* expiry dates.

        Parameters
        ----------
        pro_symbol : str          TradingView symbol or plain ticker.
        n_expiries : int          Number of expiry dates (default 3).
        spot_price : float        Current price (fetched if not provided).
        strikes_around_atm : int  Keep N strikes each side of ATM (default 8).

        Returns
        -------
        list[dict]  All call contracts passing basic sanity checks.
        """
        if not self.is_available():
            return []

        expiries = self.get_expiry_dates(pro_symbol)[:n_expiries]
        if not expiries:
            return []

        if spot_price is None or spot_price <= 0:
            spot_price = self.get_spot_price(pro_symbol) or 0.0

        all_calls: list[dict] = []
        for exp_date in expiries:
            contracts = self.get_option_chain_for_date(pro_symbol, exp_date, spot_price)
            calls = [c for c in contracts if c.get("option_type") == "CALL"]

            # Reject zero-IV entries (stale / unsupported contracts)
            calls = [c for c in calls if c.get("iv", 0) > 1.0]

            # Filter strikes around ATM
            if spot_price > 0 and calls:
                calls_sorted = sorted(calls, key=lambda c: abs(c["strike"] - spot_price))
                calls = calls_sorted[: strikes_around_atm * 2 + 1]

            all_calls.extend(calls)

        return all_calls

    def get_puts(
        self,
        pro_symbol: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
    ) -> list[dict]:
        """
        Fetch PUT contracts across the nearest *n_expiries* expiry dates.

        Parameters
        ----------
        pro_symbol : str          TradingView symbol or plain ticker.
        n_expiries : int          Number of expiry dates (default 3).
        spot_price : float        Current price (fetched if not provided).
        strikes_around_atm : int  Keep N strikes each side of ATM (default 8).

        Returns
        -------
        list[dict]  All put contracts passing basic sanity checks.
        """
        if not self.is_available():
            return []

        expiries = self.get_expiry_dates(pro_symbol)[:n_expiries]
        if not expiries:
            return []

        if spot_price is None or spot_price <= 0:
            spot_price = self.get_spot_price(pro_symbol) or 0.0

        all_puts: list[dict] = []
        for exp_date in expiries:
            contracts = self.get_option_chain_for_date(
                pro_symbol, exp_date, spot_price, option_type="PUT",
            )
            puts = [c for c in contracts if c.get("option_type") == "PUT"]

            # Reject zero-IV entries (stale / unsupported contracts)
            puts = [c for c in puts if c.get("iv", 0) > 1.0]

            # Filter strikes around ATM
            if spot_price > 0 and puts:
                puts_sorted = sorted(puts, key=lambda c: abs(c["strike"] - spot_price))
                puts = puts_sorted[: strikes_around_atm * 2 + 1]

            all_puts.extend(puts)

        return all_puts
