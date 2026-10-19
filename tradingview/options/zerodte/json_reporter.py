"""
ZeroDTE JSON Reporter
======================
Writes structured JSON companion file alongside every HTML report.
JSON is consumed by downstream agents and programmatic analysis.

Golden Rule #20: Every HTML report MUST have a companion JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import ZeroDTEContract, ZeroDTERunResult
from .archiver import archive_if_exists


def _contract_to_dict(c: ZeroDTEContract) -> dict:
    sig = c.signal
    return {
        "symbol": c.symbol,
        "option_type": c.option_type,
        "strike": c.strike,
        "expiry": c.expiry,
        "dte": c.dte,
        "contract_code": c.contract_code,
        "bid": c.bid,
        "ask": c.ask,
        "mid_price": c.mid_price,
        "last_price": c.last_price,
        "premium": c.ask,
        "iv": c.iv,
        "delta": c.delta,
        "gamma": c.gamma,
        "theta": c.theta,
        "open_interest": c.open_interest,
        "volume": c.volume,
        "spread_pct": c.spread_pct,
        "otm_pct": c.otm_pct,
        "spot_price": c.spot_price,
        "composite_score": c.composite_score,
        "momentum_score": c.momentum_score,
        "iv_back_score": c.iv_back_score,
        "voi_score": c.voi_score,
        "confidence": c.confidence,
        "signal": {
            "prev_day_change_pct": sig.prev_day_change_pct if sig else None,
            "volume_ratio": sig.volume_ratio if sig else None,
            "rsi": sig.rsi if sig else None,
            "iv_1w": sig.iv_1w if sig else None,
            "iv_1m": sig.iv_1m if sig else None,
            "iv_slope": sig.iv_back_slope if sig else None,
            "voi_ratio": sig.voi_ratio if sig else None,
            "iv_available": sig.iv_available if sig else False,
            "voi_available": sig.voi_available if sig else False,
            "sector": sig.sector if sig else "",
            "industry": sig.industry if sig else "",
        } if sig else None,
    }


def write_json_report(
    result: ZeroDTERunResult,
    direction: str,
    contracts: list[ZeroDTEContract],
    out_path: Path,
) -> None:
    """
    Write JSON companion file alongside HTML report.
    Archives any existing file first.
    """
    archive_if_exists(out_path)
    payload = {
        "run_at": result.run_at,
        "run_date": result.run_date,
        "direction": direction,
        "candidates_evaluated": result.candidates_evaluated,
        "signals_found": result.signals_found_calls if direction == "call" else result.signals_found_puts,
        "contracts_count": len(contracts),
        "contracts": [_contract_to_dict(c) for c in contracts],
        "errors": result.errors,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
