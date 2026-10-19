"""
India Options Fetcher — Angel One SmartAPI (optionGreeks endpoint)
==================================================================
Fetches NSE F&O option chains via Angel One's native optionGreeks API.

WHY Angel One over Zerodha:
  • Angel One returns IV, Delta, Gamma, Theta, Vega natively per-strike
  • No 100,000-row NFO instrument CSV download required
  • No Black-Scholes computation needed (Angel One computes Greeks server-side)
  • Same credentials already in codebase (india_market_internals.py)
  • Free tier supports option chain queries without extra subscription
  • Zerodha requires a separate access-token refresh every trading day;
    Angel One's TOTP auto-auth is fully seamless

Angel One SmartAPI endpoint:
  GET /rest/secure/angelbroking/marketData/v1/optionGreeks
  ?symbol=INFY&expiry=10APR2026

  Returns per-strike: CE/PE LTP, IV, Delta, Gamma, Theta, Vega,
                      OI, Volume, bid/ask prices

Requirements:
  pip install smartapi-python pyotp
  ENV: ANGELONE_API_KEY       (default dkozr7EU)
  ENV: ANGELONE_CLIENT_CODE   (default K51989642)
  ENV: ANGELONE_PIN           (default 2468)
  ENV: ANGELONE_TOTP_SECRET   (default 6XZS25XVVSWCICOYQR45VDQCME)

Usage:
  from options.india_options import IndiaOptionsFetcher
  fetcher = IndiaOptionsFetcher()
  if fetcher.is_available():
      contracts = fetcher.get_calls("NSE:INFY")
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from typing import Optional

from .nse_fo_list import is_fo_eligible, get_lot_size

# ─── Angel One credentials ────────────────────────────────────────────────────
AO_API_KEY     = os.environ.get("ANGELONE_API_KEY",     "")
AO_CLIENT_CODE = os.environ.get("ANGELONE_CLIENT_CODE", "")
AO_PIN         = os.environ.get("ANGELONE_PIN",         "")
AO_TOTP_SECRET = os.environ.get("ANGELONE_TOTP_SECRET", "")

_SESSION_TTL = 1200  # seconds


# ─── Expiry date helpers ──────────────────────────────────────────────────────

import calendar as _calendar


def _last_tuesday_of_month(year: int, month: int) -> date:
    """Return the last Tuesday of a given month."""
    last_day = _calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != 1:   # 1 = Tuesday
        d = date(year, month, d.day - 1)
    return d


def _upcoming_monthly_tuesdays(from_date: date, n: int, min_dte: int = 3) -> list[date]:
    """
    Return the next n NSE monthly expiry dates (last Tuesday of each month).

    NSE changed all F&O (both index and stock) monthly expiries to the last
    Tuesday of the month (effective 2024).  Weekly expiries were also discontinued
    for most contracts so monthly-Tuesday is now the standard for all symbols.
    """
    result: list[date] = []
    year, month = from_date.year, from_date.month
    while len(result) < n:
        lt = _last_tuesday_of_month(year, month)
        if (lt - from_date).days >= min_dte:
            result.append(lt)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return result


def _expiry_str(d: date) -> str:
    """Format date as DDMMMYYYY (Angel One format), e.g. 28APR2026."""
    return d.strftime("%d%b%Y").upper()


def _get_expiries(base_symbol: str, n: int = 3) -> list[date]:
    """Return next n NSE F&O expiry dates for the given underlying symbol."""
    today = date.today()
    return _upcoming_monthly_tuesdays(today, n)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ─── Angel One REST session (module-level singleton) ─────────────────────────

class _AOSession:
    """Angel One session — uses SmartConnect SDK for all calls (avoids WAF)."""
    _token:     Optional[str] = None
    _smart:     object        = None   # SmartConnect instance
    _last_auth: float         = 0.0

    @classmethod
    def ensure_auth(cls) -> bool:
        if cls._token and (time.time() - cls._last_auth < _SESSION_TTL):
            return True
        return cls._auth()

    @classmethod
    def _auth(cls) -> bool:
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError:
            return False
        try:
            totp  = pyotp.TOTP(AO_TOTP_SECRET).now()
            smart = SmartConnect(api_key=AO_API_KEY)
            resp  = smart.generateSession(AO_CLIENT_CODE, AO_PIN, totp)
            token = (resp or {}).get("data", {}).get("jwtToken", "")
            if token:
                cls._token     = token.removeprefix("Bearer ").strip()
                cls._smart     = smart
                cls._last_auth = time.time()
                return True
            return False
        except Exception:
            return False

    @classmethod
    def invalidate(cls) -> None:
        cls._last_auth = 0.0

    @classmethod
    def call_option_greek(cls, base: str, expiry_str: str) -> dict:
        """Call optionGreek via SDK (POST /v1/optionGreek) — avoids WAF."""
        if cls._smart is None:
            return {"status": False, "message": "not authenticated", "data": None}
        try:
            return cls._smart.optionGreek({"name": base, "expirydate": expiry_str})
        except Exception as exc:
            return {"status": False, "message": str(exc), "data": None}


# ─── Response parser ──────────────────────────────────────────────────────────

def _parse_option_greeks_response(
    raw: dict,
    underlying_price: float,
    base_symbol: str,
    expiry_date: date,
    lot_size: int,
    option_filter: str = "CE",
) -> list[dict]:
    """
    Parse Angel One optionGreek response into our standard contract dict.

    Actual API response (POST /v1/optionGreek) returns a flat list of items,
    each with fields: name, expiry, strikePrice, optionType (CE/PE), delta,
    gamma, theta, vega, impliedVolatility, tradeVolume.
    No ltp/bid/ask are included in this endpoint.

    Parameters
    ----------
    option_filter : str  "CE" (calls) or "PE" (puts).
    """
    data = raw.get("data")

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Legacy nested format — extract spot price and rows
        underlying_price = _safe_float(
            data.get("underlyingValue") or data.get("spotPrice"), underlying_price
        )
        rows = data.get("fetched") or data.get("optionChain") or []
    else:
        rows = []

    contracts: list[dict] = []
    dte = (expiry_date - date.today()).days
    if dte <= 0:
        return []

    expiry_str = _expiry_str(expiry_date)

    for row in rows:
        if not isinstance(row, dict):
            continue

        opt_type = (row.get("optionType") or row.get("option_type") or "").upper()
        if opt_type != option_filter:
            continue  # skip rows that don't match our option type filter

        strike = _safe_float(
            row.get("strikePrice") or row.get("strike_price") or row.get("strike")
        )
        if strike <= 0:
            continue

        volume = int(_safe_float(
            row.get("tradeVolume") or row.get("volume") or
            row.get("totalTradedVolume") or row.get("tradedVolume")
        ))
        oi     = int(_safe_float(
            row.get("oi") or row.get("openInterest") or row.get("openinterest")
        ))
        # Price fields (not provided by optionGreek endpoint — default to 0)
        ltp    = _safe_float(row.get("ltp") or row.get("lastPrice"))
        bid    = _safe_float(row.get("bidPrice") or row.get("buyPrice") or row.get("bid"))
        ask    = _safe_float(row.get("askPrice") or row.get("sellPrice") or row.get("ask"))
        iv     = _safe_float(
            row.get("impliedVolatility") or row.get("IV") or row.get("iv")
        )
        delta  = _safe_float(row.get("delta"))
        gamma  = _safe_float(row.get("gamma"))
        theta  = _safe_float(row.get("theta"))
        vega   = _safe_float(row.get("vega"))

        # Accept contracts that have at least some pricing or Greek data
        if iv <= 0 and volume <= 0 and ltp <= 0:
            continue

        mid_price  = (bid + ask) / 2 if bid > 0 and ask > 0 else ltp
        spread_pct = ((ask - bid) / mid_price * 100) if mid_price > 0 and ask > bid else 0.0
        if option_filter == "PE":
            moneyness = ((underlying_price - strike) / underlying_price * 100) if underlying_price > 0 else 0.0
        else:
            moneyness = ((strike - underlying_price) / underlying_price * 100) if underlying_price > 0 else 0.0

        contracts.append({
            "tradingsymbol": (row.get("tradingSymbol") or row.get("tradingsymbol")
                              or f"{base_symbol}{expiry_str}{option_filter}{int(strike)}"),
            "strike":        strike,
            "expiry":        expiry_str,
            "option_type":   option_filter,
            "bid":           bid,
            "ask":           ask,
            "last_price":    ltp,
            "mid_price":     mid_price,
            "volume":        volume,
            "oi":            oi,
            "iv":            iv if iv > 0 else None,
            "delta":         delta,
            "gamma":         gamma,
            "theta":         theta,
            "vega":          vega,
            "dte":           dte,
            "spread_pct":    spread_pct,
            "moneyness_pct": moneyness,
            "spot_price":    underlying_price,
            "lot_size":      lot_size,
            "lot_cost_inr":  round(mid_price * lot_size, 2),
            "market":        "NSE",
        })

    return contracts


# ─── Main fetcher class ───────────────────────────────────────────────────────

class IndiaOptionsFetcher:
    """
    Fetches NSE F&O option chains via Angel One SmartAPI optionGreeks endpoint.

    Angel One provides IV, Delta, Gamma, Theta, Vega natively — no Black-Scholes
    needed. No 100k-row NFO instrument download. Same TOTP credentials as the
    rest of the india-brokers codebase.

    The `access_token` parameter is accepted for API compatibility but ignored;
    Angel One uses its own TOTP-based session managed by _AOSession.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def is_available(self) -> bool:
        """Return True if smartapi-python + pyotp are installed and auth works."""
        try:
            import pyotp        # noqa: F401
            from SmartApi import SmartConnect  # noqa: F401
        except ImportError:
            if self.verbose:
                print("[IndiaOptions] pip install smartapi-python pyotp")
            return False
        return _AOSession.ensure_auth()

    def _spot_price(self, base_symbol: str) -> float:
        """Get current spot LTP from Angel One full_quote."""
        try:
            import sys as _sys
            _india = _get_india_dir()
            if str(_india) not in _sys.path:
                _sys.path.insert(0, str(_india))
            from india_market_internals import IndiaMarketInternals
            mi     = IndiaMarketInternals(verbose=False)
            quotes = mi.full_quote([f"NSE:{base_symbol}"])
            ltp    = (quotes.get(f"NSE:{base_symbol}") or {}).get("ltp", 0.0)
            if ltp:
                return float(ltp)
        except Exception:
            pass
        return 0.0

    def get_calls(
        self,
        tv_symbol: str,
        access_token: Optional[str] = None,  # kept for API compat, not used
        n_expiries: int = 3,
        strikes_around_atm: int = 8,
    ) -> list[dict]:
        """
        Fetch NSE F&O CALL contracts for tv_symbol (e.g. 'NSE:INFY').

        Uses Angel One optionGreeks endpoint — returns IV + Greeks natively.
        No Zerodha token required. Angel One TOTP auth handled automatically.

        Returns list of contract dicts sorted by DTE asc then strike asc.
        """
        base = tv_symbol.split(":")[-1].upper()

        if not is_fo_eligible(tv_symbol):
            if self.verbose:
                print(f"[IndiaOptions] {base} not in NSE F&O list")
            return []

        if not _AOSession.ensure_auth():
            if self.verbose:
                print("[IndiaOptions] Angel One auth failed — check credentials")
            return []

        lot_size = get_lot_size(tv_symbol)
        expiries = _get_expiries(base, n_expiries)
        spot     = self._spot_price(base)

        all_contracts: list[dict] = []

        for expiry_date in expiries:
            expiry_str = _expiry_str(expiry_date)
            raw = _AOSession.call_option_greek(base, expiry_str)

            # Retry once on auth expiry
            msg = (raw.get("message") or "").lower()
            if not raw.get("status") and any(
                kw in msg for kw in ("invalid token", "unauthorized", "session expired")
            ):
                _AOSession.invalidate()
                if _AOSession.ensure_auth():
                    raw = _AOSession.call_option_greek(base, expiry_str)

            if not raw.get("status"):
                if self.verbose:
                    print(f"[IndiaOptions] {base} {expiry_str}: {raw.get('message', '?')}")
                continue

            # Resolve spot from response if not yet known
            raw_data = raw.get("data")
            if isinstance(raw_data, dict):
                resp_spot = _safe_float(
                    raw_data.get("underlyingValue") or raw_data.get("spotPrice"),
                    spot,
                )
                if resp_spot > 0:
                    spot = resp_spot

            contracts = _parse_option_greeks_response(
                raw, spot, base, expiry_date, lot_size
            )
            all_contracts.extend(contracts)

        # ── Tier 2/3 fallback when AngelOne returns nothing ──────────────────
        if not all_contracts:
            try:
                from .nse_bs_fallback import get_nse_fallback_contracts
                yf_ticker = f"{base}.NS"
                fallback = get_nse_fallback_contracts(
                    base_symbol       = base,
                    yf_ticker         = yf_ticker,
                    expiry_dates      = expiries,
                    lot_size          = lot_size,
                    strikes_around_atm= strikes_around_atm,
                    verbose           = self.verbose,
                )
                if self.verbose and fallback:
                    src = fallback[0].get("source", "?")
                    print(f"[IndiaOptions] {base}: AngelOne empty → {len(fallback)} contracts via {src}")
                return fallback
            except Exception as _fe:
                if self.verbose:
                    print(f"[IndiaOptions] {base}: fallback error: {_fe}")
                return []

        if spot <= 0:
            return all_contracts

        # ── Narrow to ±strikes_around_atm strikes from ATM ───────────────────
        strikes = sorted({c["strike"] for c in all_contracts})
        atm     = min(strikes, key=lambda s: abs(s - spot))
        try:
            idx = strikes.index(atm)
        except ValueError:
            return all_contracts
        lo, hi        = max(0, idx - strikes_around_atm), min(len(strikes) - 1, idx + strikes_around_atm)
        valid_strikes = set(strikes[lo: hi + 1])
        filtered      = [c for c in all_contracts if c["strike"] in valid_strikes]
        filtered.sort(key=lambda c: (c["dte"], c["strike"]))
        return filtered

    def get_puts(
        self,
        tv_symbol: str,
        access_token: Optional[str] = None,  # kept for API compat, not used
        n_expiries: int = 3,
        strikes_around_atm: int = 8,
    ) -> list[dict]:
        """
        Fetch NSE F&O PUT contracts for tv_symbol (e.g. 'NSE:INFY').

        Uses Angel One optionGreeks endpoint — returns IV + Greeks natively.
        No Zerodha token required. Angel One TOTP auth handled automatically.

        Returns list of contract dicts sorted by DTE asc then strike asc.
        """
        base = tv_symbol.split(":")[-1].upper()

        if not is_fo_eligible(tv_symbol):
            if self.verbose:
                print(f"[IndiaOptions] {base} not in NSE F&O list")
            return []

        if not _AOSession.ensure_auth():
            if self.verbose:
                print("[IndiaOptions] Angel One auth failed — check credentials")
            return []

        lot_size = get_lot_size(tv_symbol)
        expiries = _get_expiries(base, n_expiries)
        spot     = self._spot_price(base)

        all_contracts: list[dict] = []

        for expiry_date in expiries:
            expiry_str = _expiry_str(expiry_date)
            raw = _AOSession.call_option_greek(base, expiry_str)

            # Retry once on auth expiry
            msg = (raw.get("message") or "").lower()
            if not raw.get("status") and any(
                kw in msg for kw in ("invalid token", "unauthorized", "session expired")
            ):
                _AOSession.invalidate()
                if _AOSession.ensure_auth():
                    raw = _AOSession.call_option_greek(base, expiry_str)

            if not raw.get("status"):
                if self.verbose:
                    print(f"[IndiaOptions] {base} {expiry_str}: {raw.get('message', '?')}")
                continue

            # Resolve spot from response if not yet known
            raw_data = raw.get("data")
            if isinstance(raw_data, dict):
                resp_spot = _safe_float(
                    raw_data.get("underlyingValue") or raw_data.get("spotPrice"),
                    spot,
                )
                if resp_spot > 0:
                    spot = resp_spot

            contracts = _parse_option_greeks_response(
                raw, spot, base, expiry_date, lot_size, option_filter="PE",
            )
            all_contracts.extend(contracts)

        # ── No NSE fallback for puts (fallback only generates calls) ──────────
        if not all_contracts:
            if self.verbose:
                print(f"[IndiaOptions] {base}: no PUT contracts from AngelOne (no fallback for puts)")
            return []

        if spot <= 0:
            return all_contracts

        # ── Narrow to ±strikes_around_atm strikes from ATM ───────────────────
        strikes = sorted({c["strike"] for c in all_contracts})
        atm     = min(strikes, key=lambda s: abs(s - spot))
        try:
            idx = strikes.index(atm)
        except ValueError:
            return all_contracts
        lo, hi        = max(0, idx - strikes_around_atm), min(len(strikes) - 1, idx + strikes_around_atm)
        valid_strikes = set(strikes[lo: hi + 1])
        filtered      = [c for c in all_contracts if c["strike"] in valid_strikes]
        filtered.sort(key=lambda c: (c["dte"], c["strike"]))
        return filtered


# ─── Path helper ──────────────────────────────────────────────────────────────

def _get_india_dir():
    import pathlib
    return pathlib.Path(__file__).parent.parent.parent / "india-brokers"

