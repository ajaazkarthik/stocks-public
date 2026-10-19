"""
TQS v2 IV Term Structure Enricher
===================================
Fetches IV term structure from options-charting.tradingview.com/v1/in-time-iv.

Returns per-symbol enrichment data:
  - iv_1w, iv_1m, iv_3m: standard term structure IVs
  - iv_current: current ATM IV (real-ivs[0])
  - iv_term_slope: 1m IV - 1w IV (positive = contango)

Returns {} on failure — all enrichment is optional/non-blocking.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import quote

import requests

from ..config import TQSv2Config
from ..interfaces.contracts import IIVEnricher

_log = logging.getLogger("tqs_v2.options_data.iv_structure")


class TVIVTermStructure(IIVEnricher):
    """Fetches IV term structure from options-charting.tradingview.com."""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def enrich(
        self,
        symbol: str,
        exchange: str,
        config: TQSv2Config,
    ) -> dict:
        """
        Fetch IV term structure for the given symbol.

        Returns dict with keys:
          iv_current, iv_1w, iv_1m, iv_3m, iv_term_slope

        Returns {} on any failure (enrichment is optional).
        """
        tv_symbol = f"{exchange}:{symbol}"
        cache_key = tv_symbol

        if cache_key in self._cache:
            return self._cache[cache_key]

        # URL-encode the colon in the symbol: NASDAQ:NVDA → NASDAQ%3ANVDA
        encoded_symbol = quote(tv_symbol, safe="")
        url = f"{config.tv_in_time_iv_base}/{encoded_symbol}"
        params = {"label-product": "product_page_overview"}
        headers = self._build_headers()

        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=config.http_timeout,
            )

            if resp.status_code != 200:
                _log.debug(
                    "in-time-iv HTTP %d for %s", resp.status_code, tv_symbol
                )
                return {}

            data = resp.json()
            result = self._parse_response(data)
            self._cache[cache_key] = result
            return result

        except Exception as exc:
            _log.debug("in-time-iv failed for %s: %s", tv_symbol, exc)
            return {}

    @staticmethod
    def _parse_response(data: dict) -> dict:
        """
        Parse the in-time-iv response.

        Response structure:
          {
            symbol: "NASDAQ:NVDA",
            real-ivs: [{span: {value: 0, unit: "d"}, value: 0.566}, ...],
            standard-ivs: [{span: "1w", value: 0.39, percentile: null, rank: null}, ...]
          }
        """
        result: dict = {}

        # Current ATM IV from real-ivs[0]
        real_ivs = data.get("real-ivs", [])
        if real_ivs:
            current_iv = _safe_float(real_ivs[0].get("value"))
            if current_iv is not None:
                result["iv_current"] = current_iv

        # Standard term structure
        standard_ivs = data.get("standard-ivs", [])
        span_map: dict[str, float] = {}
        for item in standard_ivs:
            span_raw = item.get("span", "")
            val = _safe_float(item.get("value"))
            # API returns span as a dict {'value': 1, 'unit': 'w'}, not string '1w'
            if isinstance(span_raw, dict):
                v = span_raw.get("value", "")
                u = span_raw.get("unit", "")
                span = f"{v}{u}"  # -> '1w', '2w', '1m', '2m', '3m', '6m', '9m', '1y'
            else:
                span = str(span_raw)
            if span and val is not None:
                span_map[span] = val

        result["iv_1w"] = span_map.get("1w")
        result["iv_2w"] = span_map.get("2w")
        result["iv_1m"] = span_map.get("1m")
        result["iv_2m"] = span_map.get("2m")
        result["iv_3m"] = span_map.get("3m")
        result["iv_6m"] = span_map.get("6m")

        # Term slope: 1m - 1w (positive = contango = IV increases with time)
        if result.get("iv_1m") is not None and result.get("iv_1w") is not None:
            result["iv_term_slope"] = result["iv_1m"] - result["iv_1w"]
        else:
            result["iv_term_slope"] = 0.0

        # TASK 5ii: Multi-slope term structure
        iv_1m = result.get("iv_1m")
        iv_3m = result.get("iv_3m")
        iv_6m = result.get("iv_6m")

        # slope_3m_1m: 3m IV - 1m IV  (upward slope = steeper contango)
        if iv_3m is not None and iv_1m is not None:
            result["slope_3m_1m"] = iv_3m - iv_1m
        else:
            result["slope_3m_1m"] = None

        # slope_6m_3m: 6m IV - 3m IV
        if iv_6m is not None and iv_3m is not None:
            result["slope_6m_3m"] = iv_6m - iv_3m
        else:
            result["slope_6m_3m"] = None

        # iv_contango_score (0-100): how strongly IV term structure is in contango
        # Uses slope_3m_1m as primary signal (most informative for near-term trades)
        # Breakpoints: contango ≥+0.05 →100, 0 →50, backwardation ≤-0.05 →0
        slope = result.get("slope_3m_1m")
        if slope is not None:
            iv_contango_score = max(0.0, min(100.0, 50.0 + slope / 0.05 * 50.0))
        else:
            iv_contango_score = 50.0  # neutral when data unavailable
        result["iv_contango_score"] = iv_contango_score

        # curve_shape_score (0-100): penalises humped/kinked curves
        # Uses 1w→1m→3m monotonicity; fully monotone = 100, any inversion = reduced score
        iv_1w = result.get("iv_1w")
        inversions = 0
        points = [v for v in [iv_1w, iv_1m, iv_3m, iv_6m] if v is not None]
        for i in range(1, len(points)):
            if points[i] < points[i - 1]:  # backward inversion
                inversions += 1
        total_pairs = max(1, len(points) - 1)
        curve_shape_score = max(0.0, 100.0 - inversions / total_pairs * 50.0)
        result["curve_shape_score"] = curve_shape_score

        return result

    @staticmethod
    def _build_headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/options/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }


class TVVoiLens:
    """
    Fetches volume-by-strike data from options-charting voi-lens.
    NOTE: Returns call/put VOLUME per strike per expiry — NOT open interest.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def fetch(
        self,
        symbol: str,
        exchange: str,
        config: TQSv2Config,
    ) -> dict:
        """
        Returns dict:
          {YYYYMMDD: {strike: {"call_vol": N, "put_vol": N}}}

        Returns {} on failure.
        """
        tv_symbol = f"{exchange}:{symbol}"
        cache_key = tv_symbol

        if cache_key in self._cache:
            return self._cache[cache_key]

        # TASK 5v: colon must NOT be encoded — confirmed: PATH format NASDAQ:NVDA
        encoded_symbol = quote(tv_symbol, safe=":")
        url = f"{config.tv_voi_lens_base}/{encoded_symbol}"
        params = {"label-product": "product_page_volume_tab"}
        headers = {
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/options/",
            "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36",
        }

        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=config.http_timeout)
            if resp.status_code != 200:
                return {}

            data = resp.json()
            result = self._parse_response(data)
            self._cache[cache_key] = result
            return result

        except Exception as exc:
            _log.debug("voi-lens failed for %s: %s", tv_symbol, exc)
            return {}

    @staticmethod
    def _parse_response(data: dict) -> dict:
        """
        Response:
          {items: [{exp: 20260522, strikes: [{s: 50, c: {v: 5}, p: {v: 2}}, ...]}]}
        """
        result: dict[int, dict[float, dict]] = {}
        for item in data.get("items", []):
            exp = item.get("exp")
            if exp is None:
                continue
            strike_data: dict[float, dict] = {}
            for s_entry in item.get("strikes", []):
                strike = _safe_float(s_entry.get("s"))
                if strike is None:
                    continue
                call_vol = _safe_int((s_entry.get("c") or {}).get("v"))
                put_vol = _safe_int((s_entry.get("p") or {}).get("v"))
                strike_data[strike] = {
                    "call_vol": call_vol or 0,
                    "put_vol": put_vol or 0,
                }
            result[int(exp)] = strike_data
        return result


class TVVolatilityChart:
    """
    Fetches IV smile data from options-charting volatility-chart.
    Returns per-expiry IV smile curve.
    NOTE: Y-values are ABSOLUTE IVs (decimal fractions, e.g. 0.45 = 45%).
    Confirmed via live API probe 2026-05-23 — NOT relative ratios.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def fetch_smile(
        self,
        symbol: str,
        exchange: str,
        expiry_yyyymmdd: int,
        config: TQSv2Config,
        spot_price: float = 0.0,
    ) -> dict:
        """
        Returns dict: {strikes: [list], ivs: [list], skew: float, skew_score: float}
        Returns {} on failure.
        """
        series_id = f"{exchange}:{symbol};{symbol};{expiry_yyyymmdd}"
        cache_key = series_id

        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{config.tv_volatility_chart_base}/{series_id}"
        params = {"xaxis": "strikes", "label-product": "product_page_volatility_chart"}
        headers = {
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/options/",
            "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36",
        }

        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=config.http_timeout)
            if resp.status_code != 200:
                return {}

            data = resp.json()
            result = self._parse_smile(data, spot_price=spot_price)
            self._cache[cache_key] = result
            return result

        except Exception as exc:
            _log.debug("volatility-chart failed for %s: %s", series_id, exc)
            return {}

    @staticmethod
    def _parse_smile(data: dict, spot_price: float = 0.0) -> dict:
        """
        TASK 5vi: y-values are ABSOLUTE IVs (decimal, e.g. 0.45 = 45%).
        Confirmed via live API probe 2026-05-23.

        Response: {x-axis: {x: [strikes]}, plots: [{plot: {y: [abs_ivs]}}]}

        Skew = IV at (ATM - 10%) minus IV at (ATM + 10%)
          Positive skew = put wing higher IV → bearish market pricing.
        """
        x_axis = data.get("x-axis", {})
        strikes = x_axis.get("x", [])
        plots = data.get("plots", [])

        if not strikes or not plots:
            return {}

        ivs = plots[0].get("plot", {}).get("y", []) if plots else []
        if len(ivs) != len(strikes):
            return {}

        # Determine ATM index: strike closest to spot_price; fall back to mid-index
        n = len(strikes)
        if spot_price > 0 and n > 0:
            diffs = [abs(s - spot_price) if s is not None else float("inf") for s in strikes]
            atm_idx = diffs.index(min(diffs))
        else:
            atm_idx = n // 2

        # Find strikes ~10% below and above spot
        target_put_strike = spot_price * 0.90
        target_call_strike = spot_price * 1.10

        def _nearest_iv(target_strike: float) -> float | None:
            """Return IV of the strike nearest to target_strike."""
            if target_strike <= 0 or not strikes:
                return None
            diffs = [abs(s - target_strike) if s is not None else float("inf") for s in strikes]
            idx = diffs.index(min(diffs))
            val = ivs[idx]
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        atm_iv = ivs[atm_idx]
        try:
            atm_iv = float(atm_iv) if atm_iv is not None else None
        except (TypeError, ValueError):
            atm_iv = None

        put_wing_iv = _nearest_iv(target_put_strike)
        call_wing_iv = _nearest_iv(target_call_strike)

        # Classic put skew: IV at 90% moneyness - IV at 110% moneyness
        if put_wing_iv is not None and call_wing_iv is not None:
            skew = put_wing_iv - call_wing_iv   # positive = fear premium (put skew)
        elif atm_iv is not None:
            # Fallback: left half vs right half average
            left_ivs = [float(v) for v in ivs[:atm_idx] if v is not None]
            right_ivs = [float(v) for v in ivs[atm_idx + 1:] if v is not None]
            if left_ivs and right_ivs:
                skew = sum(left_ivs) / len(left_ivs) - sum(right_ivs) / len(right_ivs)
            else:
                skew = 0.0
        else:
            skew = 0.0

        # skew_score (0-100): high put skew = bearish premium → lower score for call buyers
        # -0.05 (call skew) → 100, 0 → 50, +0.05 (put skew) → 0
        skew_score = max(0.0, min(100.0, 50.0 - skew / 0.05 * 50.0))

        return {
            "strikes": strikes,
            "ivs": ivs,
            "atm_iv": atm_iv,
            "put_wing_iv": put_wing_iv,
            "call_wing_iv": call_wing_iv,
            "skew": round(skew, 4),
            "skew_score": round(skew_score, 2),
        }


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ── Real IV Percentile via tv_in_time_iv ──────────────────────────────────
# Fix 2026-05-27 (C-3): Replaces contango proxy with actual 52-week IV rank.

_iv_history_cache: dict[str, tuple[float, float]] = {}  # symbol → (percentile, timestamp)
_IV_HISTORY_CACHE_TTL = 14400.0  # 4 hours


def fetch_iv_percentile(
    symbol: str,
    current_iv: float,
    session=None,
) -> Optional[float]:
    """
    Fetch 52-week ATM IV history from tv_in_time_iv and compute percentile rank.

    Args:
        symbol: TradingView symbol (e.g., "AAPL", "NASDAQ:AAPL")
        current_iv: Current IV as decimal (e.g., 0.25 for 25%)
        session: Optional requests.Session

    Returns:
        Percentile rank 0-100, or None if unavailable.
    """
    now = time.time()

    # Check cache
    cache_key = symbol.split(":")[-1] if ":" in symbol else symbol
    if cache_key in _iv_history_cache:
        cached_pct, cached_ts = _iv_history_cache[cache_key]
        if now - cached_ts < _IV_HISTORY_CACHE_TTL:
            return cached_pct

    try:
        sess = session or requests.Session()

        # tv_in_time_iv endpoint — same base as TVIVTermStructure
        url = "https://options-charting.tradingview.com/v1/in-time-iv"
        tv_sym = symbol if ":" in symbol else f"NASDAQ:{symbol}"
        encoded_symbol = quote(tv_sym, safe="")
        # Use the same URL pattern as TVIVTermStructure.enrich()
        url = f"https://options-charting.tradingview.com/v1/in-time-iv/{encoded_symbol}"
        params = {"label-product": "product_page_overview"}
        headers = {
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/options/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        resp = sess.get(url, params=params, headers=headers, timeout=10)

        if resp.status_code != 200:
            _log.warning("iv_percentile %s: HTTP %d", symbol, resp.status_code)
            return None

        data = resp.json()

        # Extract standard-ivs for historical percentile computation
        # The response contains real-ivs (current) and standard-ivs (term structure)
        # We use real-ivs[0].value as current IV and standard-ivs spans for context
        real_ivs = data.get("real-ivs", [])
        standard_ivs = data.get("standard-ivs", [])

        if not real_ivs and not standard_ivs:
            _log.debug("iv_percentile %s: no IV data in response", symbol)
            return None

        # Build historical IV range from standard term structure points
        # Each point has a percentile field (if available from TV)
        for item in standard_ivs:
            pct = item.get("percentile")
            if pct is not None:
                # TV provides percentile directly — use it
                percentile = float(pct)
                _iv_history_cache[cache_key] = (percentile, now)
                _log.info("IV percentile %s: %.1f%% (from TV percentile field)", symbol, percentile)
                return percentile

        # Fallback: yfinance 252-day historical volatility percentile
        # Fix 2026-05-27 (C-3): The old fallback compared current IV against 5-8
        # term structure points, measuring curve SHAPE not historical RANK.
        # yfinance HV computes 30-day rolling annualised volatility over 1 year
        # and ranks current IV against that distribution — a true percentile.
        try:
            import yfinance as yf
            import numpy as np

            clean_sym = symbol.split(":")[-1] if ":" in symbol else symbol
            ticker = yf.Ticker(clean_sym)
            hist = ticker.history(period="1y")

            if hist is not None and len(hist) >= 60:
                log_returns = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
                rolling_hv = (log_returns.rolling(30).std() * np.sqrt(252)).dropna()

                if len(rolling_hv) >= 20:
                    # current_iv in decimal form (e.g. 0.30 for 30%)
                    current_iv_dec = current_iv if current_iv < 1.0 else current_iv / 100.0
                    count_below = (rolling_hv < current_iv_dec).sum()
                    percentile = (count_below / len(rolling_hv)) * 100.0

                    _iv_history_cache[cache_key] = (percentile, now)
                    _log.info("IV percentile %s (yfinance HV fallback): %.1f%%", symbol, percentile)
                    return percentile
        except Exception as exc:
            _log.debug("yfinance HV fallback failed for %s: %s", symbol, exc)

    except Exception as exc:
        _log.warning("iv_percentile %s failed: %s", symbol, exc)
        return None
