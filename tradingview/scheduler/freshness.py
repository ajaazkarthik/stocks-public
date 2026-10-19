"""Independent chart-bar freshness checks for scanner candidates.

TradingView's scanner rows do not currently carry a verified quote timestamp.
This module cross-checks each candidate against Yahoo Finance's 15-minute
chart history using yfinance. It records that provider and timestamp explicitly;
the scheduler observation time is never substituted for the source bar time.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo
import yfinance as yf

from tradingview.scheduler.market_calendar import MARKET_CALENDARS

BAR_INTERVAL_MINUTES = 15
MAX_BAR_AGE = timedelta(minutes=45)
MAX_FUTURE_SKEW = timedelta(minutes=2)
DEFAULT_PRICE_TOLERANCE_PCT = 2.0
QUOTE_SOURCE = "Yahoo Finance 15m chart"


def yahoo_symbol(tv_symbol: str, market: str) -> str:
    """Map supported primary-listing market symbols to Yahoo chart tickers."""
    key = str(market).upper()
    if key not in MARKET_CALENDARS:
        raise ValueError(f"unsupported scanner market: {market}")
    exchange, separator, raw = str(tv_symbol or "").partition(":")
    if not separator:
        raise ValueError("TradingView symbol must include its exchange")
    exchange = exchange.strip().upper()
    symbol = raw.strip().upper()
    if not symbol or any(ch.isspace() for ch in symbol):
        raise ValueError("missing or malformed TradingView symbol")
    exchange_groups = {
        "US": {"NYSE", "NASDAQ", "NYSEARCA", "AMEX", "CBOE", "BATS", "NASDAQGS", "NASDAQGM", "NASDAQCM"},
        "AU": {"ASX"},
        "IN": {"NSE", "BSE"},
        "SG": {"SGX"},
    }
    if exchange not in exchange_groups[key]:
        raise ValueError(f"exchange {exchange or '<missing>'} does not match {key} market")
    if key == "US":
        return symbol.replace(".", "-")
    if "." in symbol:
        raise ValueError("unexpected suffix in non-US TradingView symbol")
    suffix = {"AU": ".AX", "IN": ".BO" if exchange == "BSE" else ".NS", "SG": ".SI"}[key]
    return symbol + suffix


class FreshnessRejections(dict):
    """Candidate diagnostics plus separate operational provider failures."""

    def __init__(self):
        super().__init__()
        self.provider_errors: dict[str, str] = {}


class FreshnessProviderError(RuntimeError):
    pass


def _fetch_yahoo_history(symbol: str):
    ticker = yf.Ticker(symbol)
    kwargs = dict(interval="15m", prepost=False, actions=False,
                  auto_adjust=False, timeout=8, raise_errors=True)
    try:
        history = ticker.history(period="1d", **kwargs)
        if history is not None and not history.empty:
            return history
    except yf.exceptions.YFPricesMissingError:
        pass
    # A non-trading/illiquid symbol may have no bars today. Fetch older evidence
    # once to distinguish that from provider failure; session/age gates below
    # still reject old bars. Never substitute the fetch time for a quote time.
    return ticker.history(period="5d", **kwargs)


def _finite_price(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _latest_bar(history) -> tuple[datetime, float]:
    if history is None or getattr(history, "empty", True):
        raise ValueError("Yahoo Finance returned no 15-minute chart bars")
    if "Close" not in history.columns:
        raise ValueError("Yahoo Finance chart is missing Close")
    bar_index = history.index[-1]
    bar_time = bar_index.to_pydatetime() if hasattr(bar_index, "to_pydatetime") else bar_index
    if not isinstance(bar_time, datetime) or bar_time.tzinfo is None or bar_time.utcoffset() is None:
        raise ValueError("Yahoo Finance chart bar timestamp is missing timezone information")
    close = _finite_price(history["Close"].iloc[-1])
    if close is None:
        raise ValueError("Yahoo Finance chart close is missing or invalid")
    return bar_time.astimezone(timezone.utc), close


def validate_scanner_fires(
    fires: list[dict],
    *,
    market: str,
    session_date: str,
    observed_at: datetime,
    provider_fetcher: Callable[[str], object] | None = None,
    price_tolerance_pct: float | None = None,
    max_bar_age: timedelta | None = None,
    provider_delay_minutes: int | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Enrich candidates with an independently verified fresh chart bar.

    A candidate is rejected if its mapping, provider history, provider time,
    latest-bar age, or price cross-check is missing/ambiguous. ``observed_at``
    must be the scheduler's aware observation time. The returned ``bar_time``
    is Yahoo's bar-start timestamp and ``bar_end_time`` applies the documented
    15-minute interval to that start time.
    """
    key = str(market).upper()
    if key not in MARKET_CALENDARS:
        raise ValueError(f"unsupported scanner market: {market}")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("scanner observation time must be timezone-aware")
    observed_utc = observed_at.astimezone(timezone.utc)
    exchange_zone = ZoneInfo(MARKET_CALENDARS[key][1])
    if price_tolerance_pct is None:
        try:
            price_tolerance_pct = float(os.environ.get("SIGNAL_SCANNER_PRICE_TOLERANCE_PCT", DEFAULT_PRICE_TOLERANCE_PCT))
        except (TypeError, ValueError) as exc:
            raise ValueError("SIGNAL_SCANNER_PRICE_TOLERANCE_PCT must be numeric") from exc
    if max_bar_age is None:
        try:
            max_bar_age = timedelta(minutes=int(os.environ.get("SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES", "45")))
        except (TypeError, ValueError) as exc:
            raise ValueError("SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES must be an integer") from exc
    if provider_delay_minutes is None:
        try:
            provider_delay_minutes = int(os.environ.get("SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES", "15"))
        except (TypeError, ValueError) as exc:
            raise ValueError("SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES must be an integer") from exc
    if provider_delay_minutes < 0 or provider_delay_minutes > 120:
        raise ValueError("SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES must be between 0 and 120")
    if max_bar_age < timedelta(minutes=provider_delay_minutes + BAR_INTERVAL_MINUTES):
        raise ValueError("max_bar_age must include configured provider delay plus one bar interval")
    if not math.isfinite(float(price_tolerance_pct)) or not 0 < float(price_tolerance_pct) <= 10:
        raise ValueError("price_tolerance_pct must be finite and between 0 and 10")
    if max_bar_age < timedelta(minutes=BAR_INTERVAL_MINUTES):
        raise ValueError("max_bar_age must be at least one provider bar interval")
    fetcher = provider_fetcher or _fetch_yahoo_history

    prepared: list[tuple[dict, str, str]] = []
    rejected = FreshnessRejections()
    for fire in fires:
        symbol = str(fire.get("tv_symbol") or fire.get("pro_symbol") or fire.get("symbol") or "").strip()
        try:
            yahoo = yahoo_symbol(symbol, key)
        except Exception as exc:
            rejected[(symbol.partition(":")[2] or symbol) or "<missing>"] = str(exc)
            continue
        prepared.append((fire, symbol, yahoo))

    def fetch(item):
        fire, symbol, yahoo = item
        try:
            history = fetcher(yahoo)
            if history is None or getattr(history, "empty", True):
                raise ValueError("no chart history available")
        except Exception as exc:
            raise FreshnessProviderError(f"Yahoo chart provider unavailable: {type(exc).__name__}") from exc
        bar_start, yahoo_close = _latest_bar(history)
        bar_end = bar_start + timedelta(minutes=BAR_INTERVAL_MINUTES)
        if bar_start.astimezone(exchange_zone).date().isoformat() != session_date:
            raise ValueError("Yahoo Finance chart bar does not belong to the scanner session date")
        age = observed_utc - min(bar_end, observed_utc)
        if age > max_bar_age:
            raise ValueError(f"Yahoo Finance bar is stale ({age.total_seconds() / 60:.1f} minutes old)")
        if bar_start > observed_utc + MAX_FUTURE_SKEW:
            raise ValueError("Yahoo Finance bar start is later than the scanner observation time")
        scanner_price = _finite_price(
            fire.get("close") if fire.get("close") is not None else
            fire.get("price") if fire.get("price") is not None else
            fire.get("last_price")
        )
        if scanner_price is None:
            raise ValueError("TradingView row has no finite positive source price to cross-check")
        delta_pct = abs(scanner_price - yahoo_close) / yahoo_close * 100
        if delta_pct > float(price_tolerance_pct):
            raise ValueError(
                f"TradingView/Yahoo chart price differs by {delta_pct:.2f}% "
                f"(limit {float(price_tolerance_pct):.2f}%)"
            )
        enriched = dict(fire)
        enriched.update({
            "quote_source": QUOTE_SOURCE,
            "quote_symbol": yahoo,
            "bar_time": bar_start.isoformat(),
            "bar_end_time": bar_end.isoformat(),
            "bar_interval_minutes": BAR_INTERVAL_MINUTES,
            "configured_provider_delay_minutes": provider_delay_minutes,
            "provider_close": yahoo_close,
            "source_price_delta_pct": round(delta_pct, 4),
            "freshness_assurance": "independent_chart_crosscheck",
        })
        return symbol, enriched

    accepted: list[tuple[str, dict]] = []
    if prepared:
        with ThreadPoolExecutor(max_workers=min(5, len(prepared))) as pool:
            future_items = {pool.submit(fetch, item): item for item in prepared}
            for future in as_completed(future_items):
                fire, symbol, _yahoo = future_items[future]
                try:
                    accepted_symbol, row = future.result()
                    # Retain the scanner's session date as source metadata, but
                    # reject malformed/mismatched values here as an input error.
                    if fire.get("session_date") not in (None, "", session_date):
                        raise ValueError("candidate session_date does not match scanner session")
                    row.setdefault("session_date", session_date)
                    # Convert supplied display price to a normalized float for
                    # downstream recovery consistency.
                    accepted.append((accepted_symbol, row))
                except Exception as exc:
                    name = symbol.partition(":")[2] or symbol
                    rejected[name] = str(exc)
                    if isinstance(exc, FreshnessProviderError):
                        rejected.provider_errors[name] = str(exc)
    accepted.sort(key=lambda pair: pair[0])
    return [row for _symbol, row in accepted], rejected


__all__ = [
    "BAR_INTERVAL_MINUTES", "DEFAULT_PRICE_TOLERANCE_PCT", "MAX_BAR_AGE",
    "QUOTE_SOURCE", "validate_scanner_fires", "yahoo_symbol",
]
