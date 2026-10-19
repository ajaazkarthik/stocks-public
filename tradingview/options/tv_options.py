"""
TV Options Fetcher — TradingView Pro scan2 API
===============================================
Primary US options fetcher using TradingView's reverse-engineered options/scan2 endpoint.

Key confirmed facts (probed 2026-05-09, Playwright-validated 2026-05-22):
  • IV returned as DECIMAL FRACTION (e.g. 0.2543 = 25.43%) — confirmed exact match vs UI
  • Volume only populated during US market hours (09:30–16:00 ET); None pre/post market
  • bid/ask/Greeks persist 24/7 from last traded quotes
  • theoPrice (theoretical price) available 24/7
  • bid_iv / ask_iv (bid-side / ask-side IV) available 24/7
  • Open Interest NOT available from scan2 — TV options chain does NOT show OI at all
  • 'time' field in response is ISO string (e.g. '2026-05-09T11:09Z'), NOT epoch int
  • Spot price: use global/scan2 (response: symbols[0].f[0])
  • filter2 (not filter) used for multi-expiration targeted loads
  • TV uses 'root' column (ticker only) + 'underlying_symbol' both valid
  • volatility-chart series format: {EXCHANGE}:{TICKER};{TICKER};{YYYYMMDD}

Discovered new endpoints via Playwright (2026-05-22):
  • /v1/strategies-chart  — POST payoff/PnL/Greeks for multi-leg strategies
  • /v1/volatility-chart  — GET IV smile by strike for a specific expiry
  • options-spread-explorer.tradingview.com  — DNS dead (future feature, not live)

Requirements:
  • TV Pro subscription + tradingview/cookie file with valid sessionid
  • pip install requests

Advantages over Moomoo:
  • No OpenD dependency (works when OpenD offline)
  • theoPrice, bid_iv, ask_iv available
  • All expirations scanned in a single API call
  • Real-time during market hours without any local software

Limitations vs Moomoo:
  • volume = None when market closed (Moomoo has yesterday's volume)
  • Open Interest never available (Moomoo has real OI)

Usage:
  from options.tv_options import TVOptionsFetcher
  fetcher = TVOptionsFetcher(verbose=True)
  if fetcher.is_available():
      contracts = fetcher.get_calls("NYSE:NEE", n_expiries=3)
"""

import sys
import re
import datetime
import traceback as _traceback
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

# tv_options.py lives in tradingview/options/ — cookie is in tradingview/
_TV_DIR      = Path(__file__).parent.parent   # tradingview/
_COOKIE_FILE = _TV_DIR / "cookie"
_SCAN2_URL           = "https://scanner.tradingview.com/options/scan2?label-product=options-builder"
_SPOT_URL            = "https://scanner.tradingview.com/global/scan2?label-product=symbols-options"  # validated by Playwright 2026-05-22 (replaces america/scan)
_CHARTING_URL        = "https://options-charting.tradingview.com/v1/in-time-iv"   # ATM IV term structure
_VOL_CHART_URL       = "https://options-charting.tradingview.com/v1/volatility-chart"  # IV smile: /{EXCHANGE}:{TICKER};{TICKER};{YYYYMMDD}?xaxis=strikes
_STRATEGIES_CHART_URL = "https://options-charting.tradingview.com/v1/strategies-chart"  # payoff/PnL/Greeks for strategy legs

# Confirmed valid columns for options/scan2 (exhaustive probe 2026-05-09 + rho confirmed 2026-05-12
# + live probe 2026-05-22: added underlying_symbol, update_time, atm)
# Fields NOT returned by scan2 (computed client-side by TV UI):
#   intrinsicValue, timeValue, toBreakEven, breakeven, annAsk, annBid, relDist, distance
#   These are computed in hybrid_options.py after fetching.
_COLUMNS = [
    "bid", "ask", "close", "theoPrice", "volume",
    "delta", "gamma", "theta", "vega", "rho",
    "iv", "bid_iv", "ask_iv",
    "expiration", "strike", "option-type",
    # New confirmed fields (2026-05-22 probe)
    "underlying_symbol",  # e.g. "NASDAQ:NVDA" — confirms contract's underlying
    "update_time",        # Unix timestamp of last data update
    "atm",               # 1 for the single ATM contract, null for all others
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val, default: float = 0.0) -> float:
    """NaN-safe float: float('nan') or 0 returns NaN (NaN is truthy)."""
    try:
        f = float(val)
        return default if f != f else f   # f != f is True only for NaN
    except (TypeError, ValueError):
        return default


def _exp_int_to_str(exp_int) -> str:
    """Convert 20260515 → '2026-05-15'."""
    s = str(int(exp_int))
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _exp_str_to_int(exp_str: str) -> int:
    """Convert '2026-05-15' → 20260515."""
    return int(exp_str.replace("-", ""))


# ── Main class ────────────────────────────────────────────────────────────────

class TVOptionsFetcher:
    """
    Primary US options fetcher using TradingView Pro scan2 API.

    Contract output schema (compatible with options_analyzer):
      code, strike, expiry, option_type, bid, ask, last_price, mid_price,
      volume, oi (always 0), iv (%), bid_iv (%), ask_iv (%),
      delta, gamma, theta, vega, rho, theo_price, dte, spread_pct,
      moneyness_pct, spot_price, market, source,
      underlying_symbol, atm, tv_update_time,
      intrinsic_value, time_value, breakeven, to_breakeven_pct,
      ann_ask_pct, ann_bid_pct, rel_dist_pct, distance, iv_spread
    """

    def __init__(self, verbose: bool = False):
        self.verbose   = verbose
        self._sess: Optional[object] = None
        self._available: Optional[bool] = None

    # ── Session / auth ────────────────────────────────────────────────────────

    def _make_session(self):
        if not _REQUESTS_OK:
            raise ImportError("pip install requests")
        if not _COOKIE_FILE.exists():
            raise FileNotFoundError(f"TV cookie not found: {_COOKIE_FILE}")
        text = _COOKIE_FILE.read_text("utf-8")
        m1 = re.search(r"sessionid=([^;\s]+)", text)
        m2 = re.search(r"sessionid_sign=([^;\s]+)", text)
        if not m1:
            raise ValueError("sessionid not found in cookie file")
        s = _requests.Session()
        s.cookies.set("sessionid",      m1.group(1).strip(), domain=".tradingview.com")
        s.cookies.set("sessionid_sign", m2.group(1).strip() if m2 else "", domain=".tradingview.com")
        s.headers.update({
            "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer":      "https://www.tradingview.com/",
            "Origin":       "https://www.tradingview.com",
            "Accept":       "application/json",
            "Content-Type": "text/plain;charset=UTF-8",
        })
        return s

    def _session(self):
        if self._sess is None:
            self._sess = self._make_session()
        return self._sess

    def is_available(self) -> bool:
        """Return True if TV cookie is present and parseable."""
        if self._available is not None:
            return self._available
        try:
            self._session()   # raises if cookie missing or malformed
            self._available = True
        except Exception as exc:
            if self.verbose:
                print(f"[TVOptions] not available: {exc}")
            self._available = False
        return self._available

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def get_spot_price(self, pro_symbol: str) -> Optional[float]:
        """
        Fetch current/last price using global/scan2 (Playwright-validated 2026-05-22).
        pro_symbol: e.g. 'NYSE:NEE', 'NASDAQ:NVDA'
        Returns None on failure.

        global/scan2 payload: {"symbols": {"tickers": [...]}, "columns": ["close", ...]}
        Response: {"totalCount":N, "fields":[...], "symbols":[{"s":"NASDAQ:NVDA","f":[price,...]}]}
        """
        try:
            r = self._session().post(
                _SPOT_URL,
                json={
                    "symbols": {"tickers": [pro_symbol]},
                    "columns": ["close", "pricescale", "currency", "change", "change_abs"],
                    "ignore_unknown_fields": False,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            fields = data.get("fields", [])
            symbols = data.get("symbols", [])
            if symbols:
                vals = symbols[0].get("f", [])
                close_idx = fields.index("close") if "close" in fields else 0
                if close_idx < len(vals) and vals[close_idx] is not None:
                    return float(vals[close_idx])
        except Exception as exc:
            if self.verbose:
                print(f"[TVOptions] get_spot_price({pro_symbol}): {exc}")
        return None

    def get_available_expirations(
        self, pro_symbol: str, *, min_dte: int | None = None,
        max_dte: int | None = None, reference_date: datetime.date | str | None = None,
    ) -> list:
        """
        Fetch all available expiration dates for a symbol.
        Returns sorted list of YYYYMMDD integers (e.g. [20260516, 20260620, 20260821]).
        """
        try:
            expiry_filter = [
                {"expression": {"left": "type", "operation": "equal", "right": "option"}},
                {"expression": {"left": "option-type", "operation": "equal", "right": "call"}},
            ]
            if min_dte is not None or max_dte is not None:
                ref_date = self._reference_date(reference_date)
                low = ref_date + datetime.timedelta(days=min_dte or 0)
                high = ref_date + datetime.timedelta(days=max_dte if max_dte is not None else 365)
                expiry_filter.append({"expression": {
                    "left": "expiration", "operation": "in_range",
                    "right": [int(low.strftime("%Y%m%d")), int(high.strftime("%Y%m%d"))],
                }})
            payload = {
                "columns":               ["expiration"],
                "ignore_unknown_fields": True,
                "index_filters":         [{"name": "underlying_symbol", "values": [pro_symbol]}],
                "filter2": {
                    "operator": "and",
                    "operands": expiry_filter
                },
                "sort":  {"sortBy": "expiration", "sortOrder": "asc"},
                "range": [0, 1000],
            }
            r = self._session().post(_SCAN2_URL, json=payload, timeout=20)
            r.raise_for_status()
            data   = r.json()
            fields = data.get("fields", [])
            exps   = set()
            if "expiration" in fields:
                idx = fields.index("expiration")
                for sym in data.get("symbols", []):
                    v = sym["f"][idx] if idx < len(sym["f"]) else None
                    if v is not None:
                        exps.add(int(v))
            return sorted(exps)
        except Exception as exc:
            if self.verbose:
                print(f"[TVOptions] get_available_expirations({pro_symbol}): {exc}")
            return []

    def get_puts(
        self,
        pro_symbol: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
    ) -> list:
        """Fetch PUT option contracts. Thin wrapper around _get_options."""
        return self._get_options(pro_symbol, n_expiries, spot_price,
                                 strikes_around_atm, option_type="put")

    def get_calls(
        self,
        pro_symbol: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
        *,
        min_dte: int | None = None,
        max_dte: int | None = None,
        reference_date: datetime.date | str | None = None,
    ) -> list:
        """Fetch CALL option contracts. Thin wrapper around _get_options."""
        return self._get_options(pro_symbol, n_expiries, spot_price,
                                 strikes_around_atm, option_type="call",
                                 min_dte=min_dte, max_dte=max_dte,
                                 reference_date=reference_date)

    @staticmethod
    def _reference_date(value: datetime.date | str | None) -> datetime.date:
        if value is None:
            return datetime.date.today()
        if isinstance(value, datetime.datetime):
            raise ValueError("reference_date must be a date, not datetime")
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            return datetime.date.fromisoformat(value)
        raise ValueError("reference_date must be an ISO date")

    def _get_options(
        self,
        pro_symbol: str,
        n_expiries: int = 3,
        spot_price: Optional[float] = None,
        strikes_around_atm: int = 8,
        option_type: str = "call",
        min_dte: int | None = None,
        max_dte: int | None = None,
        reference_date: datetime.date | str | None = None,
    ) -> list:
        """
        Fetch option contracts for the nearest n_expiries.

        Args:
            pro_symbol:        TV symbol e.g. 'NASDAQ:NVDA', 'NYSE:NEE'
            n_expiries:        number of nearest expiration dates to include
            min_dte/max_dte:   optional inclusive scanner expiry window
            reference_date:    date used for DTE (NY session date for scanner)
            spot_price:        underlying price (fetched automatically if None)
            strikes_around_atm: percentage range around ATM to filter strikes
                                (default 8 → ±8% from spot price)
            option_type:       'call' or 'put'

        Returns:
            List of contract dicts. Empty list on failure. Each dict has:
              code, strike, expiry, option_type='CALL'|'PUT', bid, ask, last_price,
              mid_price, volume (0 when market closed), oi=0 (not available in scan2),
              iv (%), bid_iv (%), ask_iv (%), delta, gamma, theta, vega,
              theo_price, dte, spread_pct, moneyness_pct, spot_price, market, source='tv'
        """
        if not self.is_available():
            return []
        try:
            # Step 1: Get spot price for ATM filter and moneyness computation
            if spot_price is None:
                spot_price = self.get_spot_price(pro_symbol)
            if spot_price is None or spot_price <= 0:
                if self.verbose:
                    print(f"[TVOptions] Could not get spot price for {pro_symbol}")
                return []

            ref_date = self._reference_date(reference_date)
            if (min_dte is not None and (isinstance(min_dte, bool) or min_dte < 0)
                    or max_dte is not None and (isinstance(max_dte, bool) or max_dte < 0)
                    or min_dte is not None and max_dte is not None and min_dte > max_dte):
                raise ValueError("invalid DTE expiry window")
            # Step 2: Ask scan2 only for expiries in the scanner window, then
            # select the nearest bounded number of dates from that result.
            expirations = self.get_available_expirations(
                pro_symbol, min_dte=min_dte, max_dte=max_dte,
                reference_date=ref_date if min_dte is not None or max_dte is not None else None,
            )
            if not expirations:
                if self.verbose:
                    print(f"[TVOptions] No expirations found for {pro_symbol}")
                return []
            # Filter out already-expired contracts (today or earlier)
            ref_int = int(ref_date.strftime("%Y%m%d"))
            expirations = [e for e in expirations if e >= ref_int]
            if min_dte is not None or max_dte is not None:
                low = min_dte if min_dte is not None else 0
                high = max_dte if max_dte is not None else 365
                expirations = [e for e in expirations
                               if low <= (datetime.date(int(str(e)[:4]), int(str(e)[4:6]), int(str(e)[6:])) - ref_date).days <= high]
            if not expirations:
                if self.verbose:
                    print(f"[TVOptions] All expirations in past for {pro_symbol}")
                return []
            target_exps = set(expirations[:n_expiries])
            min_exp     = min(target_exps)
            max_exp     = max(target_exps)

            # Step 3: Strike range filter (percentage-based around ATM)
            # strikes_around_atm=8 → ±8% from spot, with +5% buffer to avoid edge misses
            pct     = max(strikes_around_atm, 5) / 100.0
            buf     = 0.05
            strike_lo = round(spot_price * (1.0 - pct - buf), 2)
            strike_hi = round(spot_price * (1.0 + pct + buf), 2)

            # Step 4: Fetch options from scan2
            payload = {
                "columns":               _COLUMNS,
                "ignore_unknown_fields": True,
                "index_filters":         [{"name": "underlying_symbol", "values": [pro_symbol]}],
                "filter2": {
                    "operator": "and",
                    "operands": [
                        {"expression": {"left": "type",        "operation": "equal",    "right": "option"}},
                        {"expression": {"left": "option-type", "operation": "equal",    "right": option_type}},
                        {"expression": {"left": "expiration",  "operation": "in_range", "right": [min_exp, max_exp]}},
                        {"expression": {"left": "strike",      "operation": "in_range", "right": [strike_lo, strike_hi]}},
                    ]
                },
                "sort":  {"sortBy": "strike", "sortOrder": "asc"},
                "range": [0, 500],
            }
            r = self._session().post(_SCAN2_URL, json=payload, timeout=25)
            r.raise_for_status()
            data   = r.json()
            fields = data.get("fields", [])
            syms   = data.get("symbols", [])

            if not syms:
                if self.verbose:
                    print(
                        f"[TVOptions] No contracts for {pro_symbol} "
                        f"(exps={list(target_exps)}, strike {strike_lo:.2f}–{strike_hi:.2f})"
                    )
                return []

            # Step 5: Normalize each contract
            today_dt    = ref_date
            contracts   = []
            seen_codes  = set()

            for sym in syms:
                row = dict(zip(fields, sym.get("f", [])))

                # Contract identity
                exp_int = row.get("expiration")
                if exp_int is None:
                    continue
                exp_int = int(exp_int)
                if exp_int not in target_exps:
                    continue   # outside our target expirations

                strike = _safe_float(row.get("strike"))
                if strike <= 0:
                    continue

                tv_code = sym.get("s", "")   # e.g. "OPRA:NVDA260821C230.0"
                if tv_code in seen_codes:
                    continue
                seen_codes.add(tv_code)

                expiry = _exp_int_to_str(exp_int)   # "2026-08-21"
                try:
                    exp_date = datetime.date(
                        int(str(exp_int)[:4]),
                        int(str(exp_int)[4:6]),
                        int(str(exp_int)[6:]),
                    )
                    dte = (exp_date - today_dt).days
                except Exception:
                    dte = 30

                # Pricing
                bid  = _safe_float(row.get("bid"))
                ask  = _safe_float(row.get("ask"))
                theo = _safe_float(row.get("theoPrice"))

                # mid: prefer bid/ask; fall back to theoPrice when both are 0
                if ask > 0:
                    mid = round((bid + ask) / 2.0, 2)
                elif theo > 0:
                    mid = round(theo, 2)
                else:
                    mid = 0.0

                # Spread is only meaningful when both bid and ask are live quotes.
                # When market is closed TV scan2 returns bid=0, ask=0 but mid is set from
                # theoPrice above → (0-0)/theo = 0.0 which is misleading. Use None instead.
                if bid > 0 and ask > 0:
                    spread_pct = round((ask - bid) / max(mid, 0.01) * 100.0, 2)
                else:
                    spread_pct = None  # market closed / no live quotes → display as N/A

                # IV: scan2 returns DECIMAL FRACTION → × 100 → percentage
                # Confirmed: 0.2543 × 100 = 25.43% matches TV UI exactly
                iv_raw   = row.get("iv")
                iv_pct   = round(_safe_float(iv_raw) * 100.0, 2) if iv_raw is not None else 0.0
                bid_iv_p = round(_safe_float(row.get("bid_iv")) * 100.0, 2) if row.get("bid_iv") is not None else 0.0
                ask_iv_p = round(_safe_float(row.get("ask_iv")) * 100.0, 2) if row.get("ask_iv") is not None else 0.0

                # Greeks (already in correct sign: delta+ for calls, theta- for options)
                delta = _safe_float(row.get("delta"))
                gamma = _safe_float(row.get("gamma"))
                theta = _safe_float(row.get("theta"))
                vega  = _safe_float(row.get("vega"))

                # Moneyness: positive = OTM for both calls and puts
                # CALL: OTM when strike > spot → (strike - spot) > 0
                # PUT:  OTM when strike < spot → (spot - strike) > 0
                if option_type.lower() == "put":
                    moneyness_pct = round((spot_price - strike) / spot_price * 100.0, 2)
                else:
                    moneyness_pct = round((strike - spot_price) / spot_price * 100.0, 2)

                # Volume: None when market closed → 0
                volume = int(_safe_float(row.get("volume")))

                # ── Computed fields (TV UI columns, derived client-side) ──────
                # Intrinsic value: amount option is ITM
                if option_type.lower() == "call":
                    intr_val = max(0.0, spot_price - strike)
                else:
                    intr_val = max(0.0, strike - spot_price)

                # Time value: extrinsic / premium above intrinsic
                time_val = round(max(0.0, mid - intr_val), 4)
                intr_val = round(intr_val, 4)

                # Breakeven price at expiry
                if option_type.lower() == "call":
                    be = round(strike + ask, 4) if ask > 0 else round(strike + mid, 4)
                    to_be_pct = round((be - spot_price) / spot_price * 100.0, 2) if spot_price > 0 else 0.0
                else:
                    be = round(strike - bid, 4) if bid > 0 else round(strike - mid, 4)
                    to_be_pct = round((spot_price - be) / spot_price * 100.0, 2) if spot_price > 0 else 0.0

                # Annualised ask%  = (ask / spot) * (365 / dte) * 100
                # Annualised bid%  = (bid / spot) * (365 / dte) * 100
                eff_dte = max(dte, 1)
                ann_ask = round((ask / spot_price) * (365.0 / eff_dte) * 100.0, 2) if spot_price > 0 and ask > 0 else 0.0
                ann_bid = round((bid / spot_price) * (365.0 / eff_dte) * 100.0, 2) if spot_price > 0 and bid > 0 else 0.0

                # Relative distance %: (strike - spot) / spot × 100
                #   +ve = OTM for calls, ITM for puts
                rel_dist  = round((strike - spot_price) / spot_price * 100.0, 2) if spot_price > 0 else 0.0
                distance  = round(abs(strike - spot_price), 4)

                # IV spread: ask_iv − bid_iv (both already in %, 0 when market closed)
                iv_spread = round(ask_iv_p - bid_iv_p, 2)

                contracts.append({
                    "code":              tv_code,
                    "strike":            strike,
                    "expiry":            expiry,
                    "option_type":       option_type.upper(),
                    "bid":               bid,
                    "ask":               ask,
                    "last_price":        _safe_float(row.get("close")),  # yesterday's close
                    "mid_price":         mid,
                    "volume":            volume,
                    "oi":                0,        # scan2 has no OI → filled by hybrid_options yfinance step
                    "iv":                iv_pct,   # percentage (e.g. 25.43)
                    "bid_iv":            bid_iv_p,
                    "ask_iv":            ask_iv_p,
                    "iv_spread":         iv_spread,  # ask_iv − bid_iv (%)
                    "delta":             delta,
                    "gamma":             gamma,
                    "theta":             theta,
                    "vega":              vega,
                    "rho":               _safe_float(row.get("rho")),
                    "theo_price":        theo,
                    "dte":               dte,
                    "spread_pct":        spread_pct,
                    "moneyness_pct":     moneyness_pct,
                    "spot_price":        spot_price,
                    "market":            "US",
                    "source":            "tv",
                    # New fields from 2026-05-22 probe
                    "underlying_symbol": row.get("underlying_symbol", ""),  # e.g. "NASDAQ:NVDA"
                    "atm":               bool(row.get("atm")),              # True only for ATM contract
                    "tv_update_time":    row.get("update_time"),            # Unix timestamp
                    # Computed fields matching TV options chain UI columns
                    "intrinsic_value":   intr_val,   # TV: "Intr value"
                    "time_value":        time_val,   # TV: "Time value"
                    "breakeven":         be,         # TV: "BE"
                    "to_breakeven_pct":  to_be_pct,  # TV: "To BE%"
                    "ann_ask_pct":       ann_ask,    # TV: "Ann ask%"
                    "ann_bid_pct":       ann_bid,    # TV: "Ann bid%"
                    "rel_dist_pct":      rel_dist,   # TV: "Rel dist"
                    "distance":          distance,   # TV: "Distance"
                })

            if self.verbose:
                print(
                    f"[TVOptions] {pro_symbol}: {len(contracts)} {option_type}s "
                    f"({len(target_exps)} expiries, spot={spot_price:.2f})"
                )
            return contracts

        except Exception as exc:
            if self.verbose:
                print(f"[TVOptions] get_calls({pro_symbol}): {exc}")
            _traceback.print_exc()
            return []

    def get_iv_rank(self, pro_symbol: str) -> dict:
        """
        Fetch historical IV data from TradingView's in-time-iv charting endpoint.

        Discovered 2026-05-22: options-charting.tradingview.com/v1/in-time-iv
        is a live, undocumented endpoint that returns 25+ historical IV data
        points at various time spans (3d, 4d, 6d, ..., 52w).

        Args:
            pro_symbol: TV symbol e.g. 'NASDAQ:NVDA', 'NYSE:NEE'

        Returns dict with keys:
          iv_rank      float  0–100+ (where current IV sits in historical range;
                               >100 means current IV is above all historical)
          iv_current   float  current IV as percent (e.g. 56.6 means 56.6%)
          iv_52w_low   float  lowest historical IV in the dataset (%)
          iv_52w_high  float  highest historical IV in the dataset (%)
        Returns empty dict on failure (endpoint unavailable / symbol not supported).

        Note: SPY and other broad ETFs return HTTP 404 — only single stocks supported.
        """
        if not self.is_available():
            return {}
        try:
            encoded = pro_symbol.replace(":", "%3A")
            url = (
                f"{_CHARTING_URL}/{encoded}"
                "?label-product=details_widget_in_time_iv_chart"
            )
            r = self._session().get(url, timeout=10)
            if r.status_code != 200:
                return {}
            data     = r.json()
            real_ivs = data.get("real-ivs", [])
            if not real_ivs:
                return {}

            # First point (span value=0) = current IV
            current_raw = real_ivs[0].get("value")
            if current_raw is None:
                return {}
            current_iv = round(float(current_raw) * 100.0, 2)   # decimal → %

            # All other points are historical
            hist_vals = [
                float(pt["value"]) * 100.0
                for pt in real_ivs[1:]
                if pt.get("value") is not None
            ]
            if not hist_vals:
                return {"iv_current": current_iv, "iv_rank": None,
                        "iv_52w_low": current_iv, "iv_52w_high": current_iv}

            iv_min = min(hist_vals)
            iv_max = max(hist_vals)

            if iv_max > iv_min:
                iv_rank = round((current_iv - iv_min) / (iv_max - iv_min) * 100.0, 1)
            elif current_iv >= iv_max:
                iv_rank = 100.0
            else:
                iv_rank = 0.0

            return {
                "iv_rank":    iv_rank,
                "iv_current": current_iv,
                "iv_52w_low":  round(iv_min, 2),
                "iv_52w_high": round(iv_max, 2),
            }
        except Exception as exc:
            if self.verbose:
                print(f"[TVOptions] get_iv_rank({pro_symbol}): {exc}")
            return {}
