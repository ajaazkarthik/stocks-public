"""
Broker Position Schema — Pydantic v2 validation at broker boundary.
===================================================================
Every broker adapter normalizes API responses into a flat dict.
This module validates those dicts so field-rename regressions, None values,
and type mismatches are caught at ingestion time (not when the HTML report
looks wrong).

Usage:
    from broker_schemas import validate_positions
    raw = [{"symbol": "AAPL", "shares": 10, ...}, ...]
    validated = validate_positions(raw, broker="Moomoo-AU")
"""
from __future__ import annotations

import math
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator


class BrokerPosition(BaseModel):
    """Unified position schema — all broker adapters must produce this shape."""

    symbol: str
    shares: int | float
    avg_cost: float
    current_price: float
    market_val: float
    pnl: float
    pnl_pct: float
    currency: str = "USD"
    broker: str = ""
    exchange: str = ""
    is_option: bool = False

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("symbol")
    @classmethod
    def symbol_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must not be empty")
        return v

    @field_validator("shares")
    @classmethod
    def shares_positive(cls, v: int | float) -> int | float:
        if v <= 0:
            raise ValueError(f"shares must be positive, got {v}")
        return v

    @field_validator("avg_cost", "current_price", "market_val")
    @classmethod
    def finite_number(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"must be finite, got {v}")
        return v

    @field_validator("currency")
    @classmethod
    def currency_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("currency must not be empty")
        return v


def validate_positions(
    raw: list[dict],
    broker: str = "",
    *,
    logger=None,
) -> list[dict]:
    """
    Validate a list of raw position dicts against BrokerPosition schema.

    Returns only valid positions. Invalid ones are logged and dropped.
    This is a non-breaking filter — callers get back a (possibly shorter) list.

    Parameters
    ----------
    raw : list[dict]
        Position dicts from any broker adapter.
    broker : str
        Broker label for log context.
    logger : optional
        structlog logger; if None, uses print for warnings.

    Returns
    -------
    list[dict]
        Validated position dicts (same shape as input, not model instances).
    """
    valid = []
    for i, pos in enumerate(raw):
        # Pre-filter: 0-share rows are closed positions returned by broker API.
        # They are meaningless in the portfolio view — skip silently, no warning.
        shares = pos.get("shares", 1)
        if isinstance(shares, (int, float)) and shares == 0:
            continue
        try:
            BrokerPosition(**pos)
            valid.append(pos)
        except Exception as exc:
            msg = f"[{broker}] Position #{i} dropped: {exc}"
            if logger:
                logger.warning("position_validation_failed",
                               broker=broker, index=i, error=str(exc),
                               symbol=pos.get("symbol", "?"))
            else:
                print(f"  ⚠ {msg}")
    return valid
