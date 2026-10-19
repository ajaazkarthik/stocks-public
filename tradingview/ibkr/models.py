"""
Pydantic models for incoming TradingView alert payloads.

Expected JSON body from TradingView alert (customize in the alert message box):
{
    "symbol":     "BHP",
    "action":     "BUY",
    "quantity":   100,
    "order_type": "MKT",
    "price":      {{close}},
    "strategy":   "RSI_Breakout",
    "comment":    "Long entry signal"
}

Fields:
  symbol     - ASX ticker WITHOUT exchange suffix  (e.g. "BHP" not "BHP.AX")
  action     - BUY | SELL | CLOSE_LONG | CLOSE_SHORT
  quantity   - Number of shares (optional; falls back to DEFAULT_QUANTITY)
  order_type - MKT | LMT | STP  (default MKT)
  price      - Limit / stop price, required when order_type != MKT
  strategy   - Free-text label for logging
  comment    - Free-text comment for logging
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, field_validator, model_validator


class Alert(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL", "CLOSE_LONG", "CLOSE_SHORT"]
    quantity: Optional[float] = None
    order_type: Literal["MKT", "LMT", "STP"] = "MKT"
    price: Optional[float] = None
    strategy: Optional[str] = None
    comment: Optional[str] = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalise_symbol(cls, v: str) -> str:
        # Strip any accidental exchange suffix the user may have added
        return v.upper().strip().removesuffix(".AX").removesuffix("-ASX")

    @field_validator("action", mode="before")
    @classmethod
    def normalise_action(cls, v: str) -> str:
        return v.upper().strip()

    @model_validator(mode="after")
    def price_required_for_non_market(self) -> "Alert":
        if self.order_type in ("LMT", "STP") and self.price is None:
            raise ValueError(
                f"price is required when order_type is {self.order_type}"
            )
        return self


def parse_alert(data: dict) -> Alert:
    """Validate and parse a raw webhook payload dict into an Alert."""
    return Alert.model_validate(data)
