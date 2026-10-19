"""
TQS v2 Market Data Models
==========================
Immutable domain objects for the stock universe.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class StockRecord:
    """Represents a quality stock from the TV screener."""

    symbol: str                    # e.g. "NVDA"
    exchange: str                  # e.g. "NASDAQ"
    tv_symbol: str                 # e.g. "NASDAQ:NVDA" (used in API calls)
    close: float                   # current price
    volume: float                  # daily volume
    market_cap: float              # USD
    rsi: Optional[float] = None
    sma200: Optional[float] = None
    change_pct: Optional[float] = None
    relative_volume: Optional[float] = None
    pe_ttm: Optional[float] = None
    roe: Optional[float] = None
    debt_to_equity: Optional[float] = None
    recommendation: Optional[float] = None    # 1=Strong Buy … 5=Strong Sell
    price_target: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    current_ratio: Optional[float] = None
    eps_ttm: Optional[float] = None
    eps_growth_ttm: Optional[float] = None
    # ── Technical momentum (TASK 5iii — confirmed stable 2026-05-23) ────────
    sma50: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    atr: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None   # Gap 1: DI+ for directional polarity check
    minus_di: Optional[float] = None  # Gap 1: DI- for directional polarity check
    perf_w: Optional[float] = None
    perf_1m: Optional[float] = None
    earnings_dte: Optional[float] = None   # days until next earnings (TV screener field)

    @classmethod
    def from_scan_row(
        cls,
        d: list,
        columns: list[str],
    ) -> "StockRecord":
        """
        Build a StockRecord from a TV scanner /scan response row.

        Args:
            d: the `d` array from a single data[i] record
            columns: the columns list sent in the scan request (same order as d)
        """
        def _get(col: str, default=None):
            try:
                idx = columns.index(col)
                val = d[idx]
                return val if val is not None else default
            except (ValueError, IndexError):
                return default

        symbol: str = _get("name") or ""
        close: float = float(_get("close") or 0.0)

        # Derive exchange from market data if not available
        exchange = "NASDAQ" if symbol else "NYSE"

        return cls(
            symbol=symbol,
            exchange=exchange,
            tv_symbol=f"{exchange}:{symbol}",
            close=close,
            volume=float(_get("volume") or 0.0),
            market_cap=float(_get("market_cap_basic") or 0.0),
            rsi=_safe_float(_get("RSI")),
            sma200=_safe_float(_get("SMA200")),
            change_pct=_safe_float(_get("change")),
            relative_volume=_safe_float(_get("relative_volume_10d_calc")),
            pe_ttm=_safe_float(_get("price_earnings_ttm")),
            roe=_safe_float(_get("return_on_equity")),
            debt_to_equity=_safe_float(_get("debt_to_equity")),
            recommendation=_safe_float(_get("recommendation_mark")),
            price_target=_safe_float(_get("price_target_mean_estimate")),
            sector=_get("sector"),
            industry=_get("industry"),
            gross_margin=_safe_float(_get("gross_margin")),
            operating_margin=_safe_float(_get("operating_margin")),
            current_ratio=_safe_float(_get("current_ratio")),
            eps_ttm=_safe_float(_get("earnings_per_share_diluted_ttm")),
            eps_growth_ttm=_safe_float(_get("earnings_per_share_diluted_yoy_growth_ttm")),
            sma50=_safe_float(_get("SMA50")),
            macd=_safe_float(_get("MACD.macd")),
            macd_signal=_safe_float(_get("MACD.signal")),
            atr=_safe_float(_get("ATR")),
            adx=_safe_float(_get("ADX")),
            plus_di=_safe_float(_get("ADX+DI")),
            minus_di=_safe_float(_get("ADX-DI")),
            perf_w=_safe_float(_get("Perf.W")),
            perf_1m=_safe_float(_get("Perf.1M")),
            earnings_dte=_unix_ts_to_dte(_safe_float(_get("earnings_release_next_date"))),
        )

    @property
    def is_above_sma200(self) -> bool:
        """True if price is above its 200-day moving average."""
        if self.sma200 is None or self.sma200 <= 0:
            return True  # assume OK if data unavailable
        return self.close > self.sma200


def _unix_ts_to_dte(ts: Optional[float]) -> Optional[float]:
    """Convert a Unix epoch timestamp (from TV screener) to days-until-event.

    TV's ``earnings_release_next_date`` is a Unix timestamp (e.g. 1777550400),
    NOT a DTE count.  Without this conversion the EarningsProximityFilter's
    ``>365`` sanity cap silently treats every stock as "no data" — making the
    entire filter a no-op.  (P1 bug found 2026-05-28 by Bug Detection Agent.)
    """
    if ts is None:
        return None
    import time

    try:
        now = time.time()
        diff_days = (ts - now) / 86400.0
        return round(diff_days, 1)
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_float(val) -> Optional[float]:
    """Return float or None, never raising."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
