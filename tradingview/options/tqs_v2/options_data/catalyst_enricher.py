"""
Catalyst Enricher (Stage 4e)
=============================

Populates catalyst sub-signals on each RawContract using data already
fetched in earlier stages plus a yfinance call for analyst targets.

  Signal          | Source
  ───────────────────────────────────────────────────────
  iv_hump         | IV term structure (Stage 4 iv_snapshot_map)
  iv_backwardation| IV term structure (Stage 4 iv_snapshot_map)
  vol_oi_ratio    | VOI lens data    (Stage 4 voi_data_map)
  call_vol_skew   | VOI lens data    (Stage 4 voi_data_map)
  put_vol_skew    | VOI lens data    (Stage 4 voi_data_map)
  price_breakout  | Stock record     (Stage 1 stock_record_map)
  analyst_gap     | yfinance         (new per-symbol call, cached)
  gex             | contract fields  (computed from contract gamma/oi/spot)

Only runs when config.catalyst_mode == True.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..market_data.models import StockRecord
    from ..options_data.models import RawContract

_log = logging.getLogger("tqs_v2.options_data.catalyst_enricher")


@dataclass
class CatalystSignals:
    """All catalyst sub-signals for one stock ticker."""
    symbol: str

    # S1 — IV Hump + Backwardation (from iv_snapshot, Stage 4)
    iv_hump: float | None = None           # iv_2w / max(iv_1w, iv_1m) - 1.0
    iv_backwardation: float | None = None  # iv_1w / iv_1m - 1.0

    # S2 — Volume/OI ratio (from voi_data)
    vol_oi_ratio: float | None = None      # daily volume / open_interest

    # S3 — Call/Put Volume Skew (from voi_data)
    call_vol_skew: float | None = None     # call_vol / total_vol
    put_vol_skew: float | None = None      # put_vol / total_vol

    # S4 — Price Breakout (from stock_record)
    price_breakout_pct: float | None = None  # % change (1-day)
    relative_volume: float | None = None

    # S5 — Analyst Target Gap (yfinance)
    analyst_target_gap: float | None = None  # targetMeanPrice / current_price - 1.0

    # GAP-5 (2026-05-31): Post-earnings drift signal
    post_earnings_days: int | None = None       # calendar days since last earnings date
    post_earnings_move_pct: float | None = None # abs % move on earnings day (from yf history)

    # GEX — computed from contract gamma/oi if available
    gex: float | None = None


class CatalystEnricher:
    """
    Enrich RawContracts with catalyst signals.
    Called from orchestrator.py Stage 4e when config.catalyst_mode == True.
    """

    def __init__(self, yfinance_delay_sec: float = 0.5) -> None:
        self._yf_delay = yfinance_delay_sec
        self._signal_cache: dict[str, CatalystSignals] = {}

    def enrich(
        self,
        contracts: list[RawContract],
        iv_snapshots: dict,       # {symbol: SimpleNamespace(iv_1w, iv_2w, iv_1m)}
        voi_data: dict,           # {symbol: SimpleNamespace(call_volume, put_volume)}
        stock_records: dict,      # {symbol: StockRecord} — keyed by bare ticker
    ) -> list[RawContract]:
        """
        Populate catalyst fields on all contracts in-place.
        Returns the same list with catalyst fields populated.
        """
        # Build set of unique bare tickers from contracts
        symbols = {c.root for c in contracts}
        _log.info("CatalystEnricher: enriching %d symbols", len(symbols))

        for symbol in symbols:
            signals = self._compute_signals(
                symbol,
                iv_snapshot=iv_snapshots.get(symbol),
                voi=voi_data.get(symbol),
                stock_record=stock_records.get(symbol),
            )
            self._signal_cache[symbol] = signals

        enriched = 0
        for c in contracts:
            sig = self._signal_cache.get(c.root)
            if sig:
                c.iv_hump = sig.iv_hump
                c.iv_backwardation = sig.iv_backwardation
                c.vol_oi_ratio = sig.vol_oi_ratio
                c.call_vol_skew = sig.call_vol_skew
                c.put_vol_skew = sig.put_vol_skew
                c.price_breakout_pct = sig.price_breakout_pct
                c.analyst_target_gap = sig.analyst_target_gap
                # GAP-5 fields
                c.post_earnings_days = sig.post_earnings_days
                c.post_earnings_move_pct = sig.post_earnings_move_pct
                c.gex = sig.gex
                enriched += 1

        _log.info("CatalystEnricher: enriched %d contracts", enriched)
        return contracts

    def _compute_signals(
        self,
        symbol: str,
        iv_snapshot,
        voi,
        stock_record,
    ) -> CatalystSignals:
        sig = CatalystSignals(symbol=symbol)

        # S1: IV Hump + Backwardation (from iv_snapshot built in Stage 4e)
        if iv_snapshot is not None:
            try:
                iv_1w = getattr(iv_snapshot, "iv_1w", None)
                iv_2w = getattr(iv_snapshot, "iv_2w", None)
                iv_1m = getattr(iv_snapshot, "iv_1m", None)
                if iv_1w and iv_1m and iv_1w > 0 and iv_1m > 0:
                    sig.iv_backwardation = iv_1w / iv_1m - 1.0
                if iv_2w and iv_1w and iv_1m and iv_2w > 0:
                    sig.iv_hump = iv_2w / max(iv_1w, iv_1m) - 1.0
            except Exception as e:
                _log.debug("IV hump/backwardation failed for %s: %s", symbol, e)

        # S3: Call/Put Volume Skew (from voi_data)
        if voi is not None:
            try:
                call_vol = getattr(voi, "call_volume", None) or 0
                put_vol = getattr(voi, "put_volume", None) or 0
                total = call_vol + put_vol
                if total > 0:
                    sig.call_vol_skew = call_vol / total
                    sig.put_vol_skew = put_vol / total
            except Exception as e:
                _log.debug("Vol skew failed for %s: %s", symbol, e)

        # S4: Price Breakout (from stock_record — change_pct is the 1-day % change)
        if stock_record is not None:
            try:
                # StockRecord uses change_pct (percentage, e.g. 2.5 = 2.5% up)
                # Convert to decimal fraction for consistency with catalyst thresholds
                chg = getattr(stock_record, "change_pct", None)
                if chg is not None:
                    sig.price_breakout_pct = chg / 100.0
                sig.relative_volume = getattr(stock_record, "relative_volume", None)
            except Exception as e:
                _log.debug("Price breakout failed for %s: %s", symbol, e)

        # S5: Analyst Target Gap (yfinance — one call per symbol)
        sig.analyst_target_gap = self._fetch_analyst_gap(symbol)
        # GAP-5 (2026-05-31): Post-earnings drift signal
        sig.post_earnings_days, sig.post_earnings_move_pct = self._fetch_post_earnings(symbol)
        return sig

    def _fetch_analyst_gap(self, symbol: str) -> float | None:
        """Fetch analyst mean target price via yfinance and compute gap vs current price."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            # fast_info for current price (avoids full info dict download)
            info_dict = ticker.info or {}
            target = info_dict.get("targetMeanPrice")
            current = ticker.fast_info.last_price
            if target and current and current > 0:
                time.sleep(self._yf_delay)
                return float(target) / float(current) - 1.0
        except Exception as e:
            _log.debug("Analyst gap fetch failed for %s: %s", symbol, e)
        return None

    def _fetch_post_earnings(self, symbol: str) -> tuple[int | None, float | None]:
        """
        GAP-5 (2026-05-31): Fetch days-since-earnings and earnings-day move from yfinance.

        Returns (post_earnings_days, post_earnings_move_pct).
        Both are None when data unavailable (graceful degradation).
        """
        try:
            import yfinance as yf
            from datetime import date as _date, datetime as _dt

            ticker = yf.Ticker(symbol)

            # Use earnings_dates from calendar if available
            cal = ticker.calendar
            earnings_date: _date | None = None
            if cal is not None and not cal.empty:
                # calendar index may be date objects or strings
                idx = cal.columns.tolist() if hasattr(cal, "columns") else []
                if "Earnings Date" in idx:
                    raw_date = cal.loc["Earnings Date"].iloc[0] if hasattr(cal, "loc") else None
                    if raw_date is not None:
                        try:
                            earnings_date = _dt.fromisoformat(str(raw_date)[:10]).date()
                        except Exception:
                            pass

            # Fall back to historical earnings dates
            if earnings_date is None:
                hist_dates = ticker.earnings_dates
                if hist_dates is not None and not hist_dates.empty:
                    past = [
                        d.date() if hasattr(d, "date") else _dt.fromisoformat(str(d)[:10]).date()
                        for d in hist_dates.index
                        if (d.date() if hasattr(d, "date") else _dt.fromisoformat(str(d)[:10]).date())
                           <= _date.today()
                    ]
                    if past:
                        earnings_date = max(past)

            if earnings_date is None:
                return None, None

            days_since = (_date.today() - earnings_date).days

            # Compute the earnings-day move from OHLCV history (open→close that day)
            hist = ticker.history(start=earnings_date, end=earnings_date + 
                                  __import__("datetime").timedelta(days=3), interval="1d")
            if hist is None or hist.empty:
                return days_since, None

            # Use the row closest to earnings_date
            hist.index = hist.index.tz_localize(None) if hist.index.tzinfo is not None else hist.index
            row = hist.iloc[0]
            if row["Open"] and row["Close"] and row["Open"] > 0:
                move_pct = abs((row["Close"] / row["Open"]) - 1.0) * 100.0  # absolute %
                return days_since, move_pct

            return days_since, None

        except Exception as e:
            _log.debug("Post-earnings fetch failed for %s: %s", symbol, e)
            return None, None
