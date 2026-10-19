"""
TQS v2 Options Data Models
============================
Immutable domain objects for option contracts at each pipeline stage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class RawContract:
    """
    Raw option contract as returned by TV scan2 — plus OI from yfinance.
    This is the pre-scoring representation.
    """

    # ── Identifiers ─────────────────────────────────────────────────────────
    opra_code: str           # e.g. "OPRA:NVDA260515C220.0"
    underlying_symbol: str   # TV format: "NASDAQ:NVDA"
    root: str                # bare ticker: "NVDA"
    exchange: str            # "NASDAQ"
    option_type: str         # "call" or "put"

    # ── Contract terms ───────────────────────────────────────────────────────
    strike: float
    expiration: int          # YYYYMMDD integer from scan2
    expiry_date: Optional[date] = None   # parsed from expiration

    # ── Pricing ──────────────────────────────────────────────────────────────
    bid: float = 0.0
    ask: float = 0.0
    theo_price: float = 0.0
    change: Optional[float] = None
    change_abs: Optional[float] = None

    # ── Greeks ───────────────────────────────────────────────────────────────
    iv: float = 0.0          # decimal fraction (0.35 = 35%)
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    bid_iv: float = 0.0
    ask_iv: float = 0.0

    # ── Liquidity ────────────────────────────────────────────────────────────
    volume: Optional[int] = None     # None when market closed
    open_interest: int | None = None  # from yfinance (enriched post-fetch); None = yfinance unavailable/refreshing

    # ── Market structure ─────────────────────────────────────────────────────
    atm: bool = False
    pricescale: int = 100
    currency: str = "USD"
    minmov: int = 1
    update_time: Optional[int] = None   # Unix timestamp

    # ── Enrichment from underlying ───────────────────────────────────────────
    spot_price: float = 0.0    # from stock screener (not from scan2)
    # ── Stock technical context (TASK 5iii — set by orchestrator) ───────────
    stock_rsi: Optional[float] = None
    stock_sma200: Optional[float] = None
    stock_sma50: Optional[float] = None
    stock_macd: Optional[float] = None
    stock_macd_signal: Optional[float] = None
    stock_atr: Optional[float] = None
    stock_adx: Optional[float] = None
    stock_plus_di: Optional[float] = None   # Gap 1: DI+ polarity
    stock_minus_di: Optional[float] = None  # Gap 1: DI- polarity
    stock_change_pct: Optional[float] = None  # Gap 5: momentum chasing guard
    # Days until next earnings event (from TV screener earnings_release_next_date).
    # None = not available. 0 = earnings today. Contracts spanning earnings are
    # filtered out in engine.py (30-50% IV crush risk at earnings).
    earnings_dte: Optional[float] = None

    # ── Catalyst signals (populated by CatalystEnricher, Stage 4e) ──────────
    iv_hump: float | None = None             # iv_2w / max(iv_1w, iv_1m) - 1.0
    iv_backwardation: float | None = None    # iv_1w / iv_1m - 1.0
    vol_oi_ratio: float | None = None        # daily_volume / open_interest
    call_vol_skew: float | None = None       # call_vol / total_vol at near-ATM strikes
    put_vol_skew: float | None = None        # put_vol / total_vol
    price_breakout_pct: float | None = None  # % change (from stock record)
    analyst_target_gap: float | None = None  # targetMeanPrice / current_price - 1.0
    gex: float | None = None                 # gamma exposure (stock-level, from cboe)
    # GAP-5 (2026-05-31): Post-earnings drift signal
    post_earnings_days: int | None = None    # calendar days since last earnings; None = unknown
    post_earnings_move_pct: float | None = None  # abs % move on earnings day; None = unknown

    # ── Computed fields (set after initialisation) ────────────────────────────
    dte: int = 0               # days to expiry
    moneyness_pct: float = 0.0 # (spot - strike) / strike × 100
    mid: float = 0.0           # (bid + ask) / 2
    spread_pct: float = 0.0    # (ask - bid) / mid × 100
    breakeven: float = 0.0     # strike + ask (CALL) / strike - ask (PUT)

    def __post_init__(self) -> None:
        self._compute_derived_fields()

    def _compute_derived_fields(self) -> None:
        """Compute all client-side derived fields."""
        # Parse expiry date
        if self.expiry_date is None and self.expiration:
            try:
                exp_str = str(self.expiration)
                self.expiry_date = date(
                    int(exp_str[:4]),
                    int(exp_str[4:6]),
                    int(exp_str[6:8]),
                )
            except (ValueError, IndexError):
                pass

        # DTE
        if self.expiry_date:
            self.dte = max(0, (self.expiry_date - date.today()).days)

        # Mid price
        if self.bid > 0 or self.ask > 0:
            self.mid = (self.bid + self.ask) / 2.0

        # Spread %
        if self.mid > 0:
            self.spread_pct = (self.ask - self.bid) / self.mid * 100.0

        # Moneyness
        if self.spot_price > 0 and self.strike > 0:
            self.moneyness_pct = (self.spot_price - self.strike) / self.strike * 100.0

        # Breakeven
        if self.option_type == "call":
            self.breakeven = self.strike + self.ask
        else:
            self.breakeven = self.strike - self.ask

    @classmethod
    def from_scan2_row(
        cls,
        opra_code: str,
        f: list,
        fields: list[str],
        stock_close: float,
        stock_exchange: str,
    ) -> "RawContract":
        """
        Build a RawContract from a TV scan2 symbols[] entry.

        TV scan2 response format:
          {fields:[...], symbols:[{s:"OPRA:...", f:[v1,v2,...]}]}

        Args:
            opra_code: e.g. "OPRA:NVDA260515C220.0"
            f: the f[] array values
            fields: the fields[] list from the scan2 response header
            stock_close: spot price of the underlying (from stock screener)
            stock_exchange: exchange of the underlying (NASDAQ / NYSE)
        """

        def _get(field_name: str, default=None):
            try:
                idx = fields.index(field_name)
                val = f[idx]
                return val if val is not None else default
            except (ValueError, IndexError):
                return default

        # Parse underlying_symbol → extract root + exchange
        underlying = _get("underlying_symbol") or ""
        root = _get("root") or ""
        if not root and underlying and ":" in underlying:
            root = underlying.split(":")[-1]

        # option-type from scan2 is "call" or "put"
        opt_type = (_get("option-type") or "call").lower()

        # expiration as YYYYMMDD integer
        expiration_raw = _get("expiration")
        expiration = int(expiration_raw) if expiration_raw is not None else 0

        # Bid/ask safety
        bid = float(_get("bid") or 0.0)
        ask = float(_get("ask") or 0.0)
        if ask < bid:
            ask = bid  # data anomaly guard

        contract = cls(
            opra_code=opra_code,
            underlying_symbol=underlying or f"{stock_exchange}:{root}",
            root=root,
            exchange=stock_exchange,
            option_type=opt_type,
            strike=float(_get("strike") or 0.0),
            expiration=expiration,
            bid=bid,
            ask=ask,
            theo_price=float(_get("theoPrice") or 0.0),
            change=_safe_float(_get("change")),
            change_abs=_safe_float(_get("change_abs")),
            iv=float(_get("iv") or 0.0),
            delta=float(_get("delta") or 0.0),
            gamma=float(_get("gamma") or 0.0),
            theta=float(_get("theta") or 0.0),
            vega=float(_get("vega") or 0.0),
            rho=float(_get("rho") or 0.0),
            bid_iv=float(_get("bid_iv") or 0.0),
            ask_iv=float(_get("ask_iv") or 0.0),
            volume=_safe_int(_get("volume")),
            open_interest=None,  # will be enriched by YFinanceOIEnricher
            atm=bool(_get("atm") or False),
            pricescale=int(_get("pricescale") or 100),
            currency=str(_get("currency") or "USD"),
            minmov=int(_get("minmov") or 1),
            update_time=_safe_int(_get("update_time")),
            spot_price=stock_close,
        )
        return contract


@dataclass
class OptionContract(RawContract):
    """
    Fully-enriched option contract ready for scoring.
    Adds IV structure data from additional TV endpoints.
    """

    # IV term structure from in-time-iv
    iv_term_slope: float = 0.0      # positive = contango (front < back)
    iv_1w: Optional[float] = None
    iv_1m: Optional[float] = None
    iv_3m: Optional[float] = None
    iv_current: Optional[float] = None   # real-ivs[0].value

    # Volume by strike from voi-lens
    strike_call_volume: Optional[int] = None
    strike_put_volume: Optional[int] = None
    put_call_volume_ratio: Optional[float] = None

    # IV smile skew from volatility-chart
    smile_skew: float = 0.0     # positive = put skew (normal), negative = call skew

    # WebSocket enrichment (optional, 15min delay)
    ws_bid: Optional[float] = None
    ws_ask: Optional[float] = None
    ws_bid_size: Optional[int] = None
    ws_ask_size: Optional[int] = None
    ws_volume: Optional[int] = None  # last-session volume from TV WebSocket (fills scan2 None when closed)

    @classmethod
    def from_raw(cls, raw: RawContract, **enrichments) -> "OptionContract":
        """Create an OptionContract from a RawContract + enrichment data."""
        # Copy all raw fields
        kwargs = {
            f.name: getattr(raw, f.name)
            for f in raw.__dataclass_fields__.values()
        }
        kwargs.update(enrichments)
        # Suppress __post_init__ recomputing to preserve computed fields
        obj = cls.__new__(cls)
        for k, v in kwargs.items():
            object.__setattr__(obj, k, v)
        # Restore computed fields that may have been set by enrichments
        for attr in ("dte", "moneyness_pct", "mid", "spread_pct", "breakeven"):
            if attr not in enrichments and hasattr(raw, attr):
                object.__setattr__(obj, attr, getattr(raw, attr))
        return obj


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
