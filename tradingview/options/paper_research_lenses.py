"""Isolated, read-only research reuse of existing ZeroDTE TradingView lenses.

This is NOT a source of executable option-side quotes or a default strategy input.
It intentionally calls the *existing* ZeroDTE IV and volume fetchers rather than
copying their endpoint or formula implementation. Never persist auth headers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

from tradingview.options.zerodte.config import DEFAULT_CONFIG
from tradingview.options.zerodte.fetcher import (
    _charting_symbol, fetch_iv_structure, fetch_voi_data,
)


def _pct_if_valid(value, is_available):
    if not is_available:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v * 100.0, 5) if 0.0 < v <= 10.0 and math.isfinite(v) else None


def fetch_research_lenses(tv_symbol: str, headers: dict, *, config=DEFAULT_CONFIG):
    """Explicit opt-in only; outputs observation diagnostics, never trades.

    `headers` is caller-owned; credentials are not copied, returned, logged or
    saved. The original endpoints provide no independent bid/ask-side timestamp.
    """
    if not isinstance(tv_symbol, str) or ":" not in tv_symbol:
        raise ValueError('explicit exchange-qualified underlying required')
    plain = tv_symbol.split(':')[-1]
    _charting_symbol(plain, tv_symbol)  # early, strict identity check
    iv = fetch_iv_structure(plain, headers, config, tv_symbol=tv_symbol)
    voi = fetch_voi_data(plain, headers, config, tv_symbol=tv_symbol)
    iv_1w = _pct_if_valid(iv.iv_1w, iv.available)
    iv_1m = _pct_if_valid(iv.iv_1m, iv.available)
    iv_3m = _pct_if_valid(iv.iv_3m, iv.available)
    cp_ratio = None
    if voi.available:
        try:
            raw = float(voi.call_put_ratio)
            if math.isfinite(raw) and raw > 0:
                cp_ratio = round(raw, 6)
        except (TypeError, ValueError):
            pass
    return {
        'tv_symbol': tv_symbol, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'source': 'TradingView options-charting via existing zerodte.fetcher',
        'quote_basis': 'reference',
        'quote_side_timestamps': None,
        'iv_term': {
            'status': 'REFERENCE_ONLY' if iv_1w is not None and iv_1m is not None else 'UNAVAILABLE',
            'iv_1w_pct': iv_1w, 'iv_1m_pct': iv_1m, 'iv_3m_pct': iv_3m,
            'short_minus_month_pct_points': round(iv_1w-iv_1m,5) if iv_1w is not None and iv_1m is not None else None,
            'provider_asof': None, 'is_tenor_matched_forecast': False,
        },
        'volume_lens': {
            'status': 'REFERENCE_ONLY' if cp_ratio is not None else 'UNAVAILABLE',
            'aggregated_call_put_volume_ratio': cp_ratio,
            'expiry_scope': 'all expiries reported by the VOI endpoint',
            'directional_aggressor': 'UNKNOWN',
            'oi_or_signed_order_flow': False,
            'provider_asof': None,
        },
        'eligible_for_live_fills': False,
        'eligible_for_challenger_a_forecast': False,
    }


def with_research_lenses(observation: dict, lens: dict) -> dict:
    """Attach optional metadata to a *copy*; frozen scanner ignores this key."""
    enriched = dict(observation)
    enriched['research_lenses'] = lens
    return enriched
