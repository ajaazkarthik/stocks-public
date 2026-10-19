"""
ZeroDTE Contract Filter
========================
Selects the best 0-2DTE option contracts for a given stock signal.
Applies quality filters: DTE range, OTM %, liquidity, spread, premium.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from .config import ZeroDTEConfig, DEFAULT_CONFIG
from .models import ZeroDTEContract, SignalScore
from .fetcher import (
    fetch_option_chain_for_expiry,
    fetch_contracts_via_scan2,
    fetch_oi_from_yfinance,
    get_expiry_dates_0_to_2dte,
)

_log = logging.getLogger("zerodte.filter")

# TV scan2 options endpoint (used in GAP-3 Greek enrichment)
_TV_SCAN2_URL = "https://scanner.tradingview.com/options/scan2?label-product=options-builder"

# Default exchange prefixes for the ZeroDTE fixed watchlist + common movers
_EXCHANGE_PREFIX: dict[str, str] = {
    "SPY": "AMEX", "QQQ": "NASDAQ", "IWM": "AMEX",
    "NVDA": "NASDAQ", "AAPL": "NASDAQ", "MSFT": "NASDAQ",
    "META": "NASDAQ", "GOOGL": "NASDAQ", "AMZN": "NASDAQ",
    "TSLA": "NASDAQ", "AMD": "NASDAQ", "INTC": "NASDAQ", "QCOM": "NASDAQ",
    "MU": "NASDAQ", "AVGO": "NASDAQ", "ARM": "NASDAQ", "MRVL": "NASDAQ",
    "CRWD": "NASDAQ", "PANW": "NASDAQ", "SNOW": "NYSE", "CRM": "NYSE",
    "NFLX": "NASDAQ", "UBER": "NYSE", "COIN": "NASDAQ", "HOOD": "NASDAQ",
    "RBLX": "NYSE", "PLTR": "NYSE", "BABA": "NYSE", "JD": "NASDAQ",
    "NIO": "NYSE", "RIVN": "NASDAQ", "LCID": "NASDAQ", "GME": "NYSE",
    "AMC": "NYSE", "SHOP": "NYSE", "MELI": "NASDAQ", "SE": "NYSE",
    "SMCI": "NASDAQ", "SOFI": "NASDAQ", "DKNG": "NASDAQ", "LYFT": "NASDAQ",
    "BIDU": "NASDAQ",
}


def _resolve_pro_symbol(symbol: str) -> str:
    """Map bare ticker to 'EXCHANGE:TICKER' format required by TV scan2."""
    return f"{_EXCHANGE_PREFIX.get(symbol, 'NASDAQ')}:{symbol}"


def _enrich_greeks_from_tv_scan2(
    contracts: list["ZeroDTEContract"],
    cookie_path: Path,
) -> list["ZeroDTEContract"]:
    """
    GAP-3 fix (2026-05-31): Fetch actual delta/gamma/theta from TV scan2 and
    replace the placeholder 0.0 values on each ZeroDTEContract.

    Groups by (symbol, option_type, expiry) to minimise scan2 round-trips.
    Uses TVOptionsFetcher for cookie-based authentication.
    Safe for both frozen and non-frozen ZeroDTEContract via dataclasses.replace().
    """
    if not contracts:
        return contracts

    try:
        from ..tv_options import TVOptionsFetcher
        fetcher = TVOptionsFetcher(cookie_path=cookie_path, verbose=False)
        if not fetcher.is_available():
            _log.debug("[GAP-3] TV scan2 unavailable \u2014 Greeks remain 0.0")
            return contracts
    except Exception as exc:
        _log.debug("[GAP-3] Could not init TVOptionsFetcher: %s", exc)
        return contracts

    # Group contract indices by (symbol, option_type, expiry)
    by_group: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, c in enumerate(contracts):
        by_group[(c.symbol, c.option_type.lower(), c.expiry)].append(i)

    result: list[ZeroDTEContract] = list(contracts)

    for (sym, opt_type, expiry), idxs in by_group.items():
        exp_int = int(expiry.replace("-", ""))  # "2026-06-02" \u2192 20260602
        group = [contracts[i] for i in idxs]
        strikes = [c.strike for c in group]
        strike_lo = round(min(strikes) * 0.995, 2)
        strike_hi = round(max(strikes) * 1.005, 2)
        pro_sym = _resolve_pro_symbol(sym)

        try:
            sess = fetcher._session()
            payload = {
                "columns": ["strike", "expiration", "option-type", "delta", "gamma", "theta", "vega"],
                "ignore_unknown_fields": True,
                "index_filters": [{"name": "underlying_symbol", "values": [pro_sym]}],
                "filter2": {
                    "operator": "and",
                    "operands": [
                        {"expression": {"left": "type", "operation": "equal", "right": "option"}},
                        {"expression": {"left": "option-type", "operation": "equal", "right": opt_type}},
                        {"expression": {"left": "expiration", "operation": "equal", "right": exp_int}},
                        {"expression": {"left": "strike", "operation": "in_range",
                                        "right": [strike_lo, strike_hi]}},
                    ]
                },
                "sort": {"sortBy": "strike", "sortOrder": "asc"},
                "range": [0, 200],
            }
            r = sess.post(_TV_SCAN2_URL, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            fields = data.get("fields", [])
            syms_data = data.get("symbols", [])

            # Build lookup: round(strike, 2) \u2192 {delta, gamma, theta, vega}
            greeks_map: dict[float, dict[str, float]] = {}
            for sym_row in syms_data:
                row = dict(zip(fields, sym_row.get("f", [])))
                s = row.get("strike")
                if s is None:
                    continue
                greeks_map[round(float(s), 2)] = {
                    "delta": float(row.get("delta") or 0.0),
                    "gamma": float(row.get("gamma") or 0.0),
                    "theta": float(row.get("theta") or 0.0),
                    "vega":  float(row.get("vega") or 0.0),
                }

            # Replace placeholder Greeks on matched contracts
            enriched = 0
            for i in idxs:
                c = contracts[i]
                g = greeks_map.get(round(c.strike, 2))
                if g:
                    result[i] = dataclasses.replace(
                        c, delta=g["delta"], gamma=g["gamma"], theta=g["theta"]
                    )
                    enriched += 1

            _log.debug(
                "[GAP-3] Greeks enriched: %s %s %s \u2014 %d/%d contracts",
                sym, opt_type, expiry, enriched, len(idxs),
            )

        except Exception as exc:
            _log.debug(
                "[GAP-3] Greek enrichment failed for %s %s %s: %s",
                sym, opt_type, expiry, exc,
            )

    return result


def _dte(expiry_str: str) -> int:
    """Days to expiry from today. Returns -1 on parse error."""
    try:
        exp_date = date.fromisoformat(expiry_str)
        return (exp_date - date.today()).days
    except ValueError:
        return -1


def _compute_spread_pct(bid: float, ask: float) -> float:
    """Bid-ask spread as % of mid price."""
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 100.0
    return ((ask - bid) / mid) * 100.0


def _compute_otm_pct(strike: float, spot: float, option_type: str) -> float:
    """
    How far OTM is this strike relative to spot price?
    Always returns a positive value (distance from spot).
    Returns 0 if ITM.
    """
    if spot <= 0:
        return 0.0
    if option_type == "call":
        otm = ((strike - spot) / spot) * 100.0
    else:  # put
        otm = ((spot - strike) / spot) * 100.0
    return max(otm, 0.0)


def select_contracts_for_signal(
    signal: SignalScore,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> list[ZeroDTEContract]:
    """
    Given a scored signal (stock + direction), fetch option chains for
    0-2DTE expiries and select contracts matching quality filters.

    Data source priority:
      1. TV scan2 (primary): preserves last-session bid/ask even after 16:00 ET.
         OI is always 0 in scan2 (API limitation), so OI is fetched separately
         from yfinance and merged in.
      2. yfinance (fallback): used when TV cookies are unavailable.
         WARNING: returns bid=0.0/ask=0.0 after 16:00 ET which kills all contracts.

    Returns list of ZeroDTEContract sorted by composite_score desc then otm_pct asc.
    """
    symbol = signal.symbol
    direction = signal.direction  # "call" or "put"
    spot = signal.price

    # Skip preferred shares and other yfinance-incompatible tickers (e.g. ORCL/PD)
    if "/" in symbol:
        _log.info("Skipping preferred share / invalid ticker format: %s", symbol)
        return []

    if spot <= 0:
        _log.warning("Invalid spot price for %s, skipping contract selection", symbol)
        return []

    try:
        ticker = yf.Ticker(symbol)
        expiries = get_expiry_dates_0_to_2dte(ticker)
    except Exception as exc:
        _log.warning("Could not get expiries for %s: %s", symbol, exc)
        return []

    if not expiries:
        _log.debug("No 0-2DTE expiries available for %s", symbol)
        return []

    selected: list[ZeroDTEContract] = []

    for expiry in expiries:
        dte = _dte(expiry)
        if not (config.contract_min_dte <= dte <= config.contract_max_dte):
            continue

        # ── Step 1: Try TV scan2 first ────────────────────────────────────────
        # TV scan2 preserves last-session bid/ask prices after market close.
        # yfinance returns bid=0.0/ask=0.0 after 16:00 ET for all contracts,
        # causing every contract to fail the minimum-bid filter.
        scan2_df = fetch_contracts_via_scan2(symbol, direction, expiry, config)
        use_scan2 = not scan2_df.empty

        if use_scan2:
            # ── Step 2a: Merge OI from yfinance (scan2 OI is always 0) ────────
            oi_map = fetch_oi_from_yfinance(symbol, expiry, direction)
            # oi_filter_active: True only when yfinance returned at least one
            # non-zero OI value. After hours yfinance also returns oi=0, so we
            # relax the OI filter in that case to avoid rejecting all contracts.
            oi_filter_active = any(v > 0 for v in oi_map.values()) if oi_map else False

            for _, row in scan2_df.iterrows():
                strike    = float(row.get("strike")    or 0)
                bid       = float(row.get("bid")       or 0)
                ask       = float(row.get("ask")       or 0)
                iv        = float(row.get("iv")        or 0)  # decimal: 0.389 = 38.9%
                delta     = float(row.get("delta")     or 0)
                gamma     = float(row.get("gamma")     or 0)
                theta     = float(row.get("theta")     or 0)
                volume    = int(  row.get("volume")    or 0)
                theo      = float(row.get("theoPrice") or 0)

                oi = oi_map.get(round(strike, 2), 0)

                # When scan2 bid=0 (deep OTM, no market-maker quote), use theo price
                if bid <= 0 and theo > 0:
                    bid = round(theo * 0.85, 2)
                if ask <= 0 and theo > 0:
                    ask = round(theo * 1.05, 2)

                mid = (bid + ask) / 2.0
                if mid <= 0:
                    mid = theo if theo > 0 else 0.0

                # ── Quality filters ───────────────────────────────────────────
                if bid < config.contract_min_bid:
                    continue
                if ask > config.contract_max_premium:
                    continue
                if bid <= 0 or ask <= 0:
                    continue  # cannot compute valid spread
                spread_pct = _compute_spread_pct(bid, ask)
                if spread_pct > config.contract_max_spread_pct:
                    continue
                otm_pct = _compute_otm_pct(strike, spot, direction)
                if otm_pct < config.contract_otm_min_pct or otm_pct > config.contract_otm_max_pct:
                    continue
                # OI filter: skip only when yfinance OI data is available AND this
                # contract doesn't meet the minimum. If yfinance has no OI (after
                # hours), allow the contract through.
                if oi_filter_active and oi < config.contract_min_oi:
                    continue

                lottery_bonus = (
                    config.lottery_ticket_bonus_pts
                    if (ask <= config.lottery_ticket_max_premium
                        and otm_pct >= config.lottery_ticket_min_otm_pct)
                    else 0.0
                )
                dte_bonus = (
                    config.preferred_dte_bonus_pts
                    if dte == config.preferred_dte
                    else 0.0
                )

                contract_code = str(row.get("contract_code", "") or "")
                contract = ZeroDTEContract(
                    symbol=symbol,
                    option_type=direction,
                    strike=strike,
                    expiry=expiry,
                    dte=dte,
                    contract_code=contract_code,   # OPRA code from TV scan2 (e.g. OPRA:NVDA260606C150.0)
                    bid=bid,
                    ask=ask,
                    last_price=0.0,
                    mid_price=mid,
                    iv=iv,
                    delta=delta,        # real value from TV scan2 (not placeholder)
                    gamma=gamma,
                    theta=theta,
                    open_interest=oi,
                    volume=volume,
                    spread_pct=spread_pct,
                    otm_pct=otm_pct,
                    spot_price=spot,
                    signal=signal,
                    lottery_ticket_bonus=lottery_bonus,
                    dte_bonus=dte_bonus,
                )
                selected.append(contract)

        else:
            # ── Fallback: yfinance when scan2 unavailable (no cookies / error) ──
            calls_df, puts_df = fetch_option_chain_for_expiry(symbol, expiry, config)
            chain_df = calls_df if direction == "call" else puts_df

            if chain_df.empty:
                _log.debug("Empty chain for %s %s %s", symbol, direction, expiry)
                continue

            for _, row in chain_df.iterrows():
                _s = row.get("strike", 0); strike = 0.0 if pd.isna(_s) else float(_s or 0)
                _b = row.get("bid", 0); bid = 0.0 if pd.isna(_b) else float(_b or 0)
                _a = row.get("ask", 0); ask = 0.0 if pd.isna(_a) else float(_a or 0)
                _lp = row.get("lastPrice", 0); last = 0.0 if pd.isna(_lp) else float(_lp or 0)
                _oi_raw = row.get("openInterest", 0)
                oi = 0 if pd.isna(_oi_raw) else int(_oi_raw or 0)
                _vol_raw = row.get("volume", 0)
                volume = 0 if pd.isna(_vol_raw) else int(_vol_raw or 0)
                _iv_raw = row.get("impliedVolatility", 0)
                iv = 0.0 if pd.isna(_iv_raw) else float(_iv_raw or 0)
                contract_code = str(row.get("contractSymbol", "") or "")

                # Mid price
                mid = (bid + ask) / 2.0
                if mid <= 0:
                    mid = last if last > 0 else (ask * 0.95 if ask > 0 else 0)

                # ── Quality filters ───────────────────────────────────────────
                if bid < config.contract_min_bid:
                    continue
                premium = ask
                if premium > config.contract_max_premium:
                    continue
                spread_pct = _compute_spread_pct(bid, ask)
                if spread_pct > config.contract_max_spread_pct:
                    continue
                otm_pct = _compute_otm_pct(strike, spot, direction)
                if otm_pct < config.contract_otm_min_pct or otm_pct > config.contract_otm_max_pct:
                    continue
                if oi < config.contract_min_oi:
                    continue
                if volume < config.contract_min_volume:
                    pass  # allow but don't reject — 0DTE volume builds through the day

                lottery_bonus = (
                    config.lottery_ticket_bonus_pts
                    if (ask <= config.lottery_ticket_max_premium
                        and otm_pct >= config.lottery_ticket_min_otm_pct)
                    else 0.0
                )
                dte_bonus = (
                    config.preferred_dte_bonus_pts
                    if dte == config.preferred_dte
                    else 0.0
                )

                contract = ZeroDTEContract(
                    symbol=symbol,
                    option_type=direction,
                    strike=strike,
                    expiry=expiry,
                    dte=dte,
                    contract_code=contract_code,
                    bid=bid,
                    ask=ask,
                    last_price=last,
                    mid_price=mid,
                    iv=iv,
                    delta=0.0,    # yfinance doesn't return delta; placeholder
                    gamma=0.0,
                    theta=0.0,
                    open_interest=oi,
                    volume=volume,
                    spread_pct=spread_pct,
                    otm_pct=otm_pct,
                    spot_price=spot,
                    signal=signal,
                    lottery_ticket_bonus=lottery_bonus,
                    dte_bonus=dte_bonus,
                )
                selected.append(contract)

    # GAP-3 fix (2026-05-31): Enrich delta/gamma/theta from TV scan2 for any
    # yfinance-fallback contracts (delta=0.0/gamma=0.0 placeholders).
    # Skip enrichment for scan2-sourced contracts — Greeks are already populated.
    if any(c.delta == 0.0 and c.gamma == 0.0 for c in selected):
        selected = _enrich_greeks_from_tv_scan2(selected, config.cookie_path)

    # Sort by composite score desc, then OTM asc (prefer closer to ATM)
    selected.sort(
        key=lambda c: (-c.composite_score, c.otm_pct)
    )
    return selected
