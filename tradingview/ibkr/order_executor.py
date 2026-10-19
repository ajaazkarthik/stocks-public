"""
IBKR order executor using ib_insync.

Prerequisites:
  1. TWS or IB Gateway must be running on the same machine.
  2. In TWS: File → Global Configuration → API → Settings
       ☑ Enable ActiveX and Socket Clients
       ☑ Allow connections from localhost only
     Set Socket Port to match IB_PORT in your .env (default 7497 for paper).
"""
from __future__ import annotations

import logging
from typing import Optional

from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, Trade

from config import settings
from models import Alert

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self) -> None:
        self.ib = IB()

    # ── Connection lifecycle ───────────────────────────────────────────────────

    def connect(self) -> None:
        if self.ib.isConnected():
            return
        logger.info(
            "Connecting to IBKR at %s:%s (clientId=%s)",
            settings.IB_HOST,
            settings.IB_PORT,
            settings.IB_CLIENT_ID,
        )
        self.ib.connect(
            settings.IB_HOST,
            settings.IB_PORT,
            clientId=settings.IB_CLIENT_ID,
            readonly=False,
            timeout=10,
        )
        logger.info("Connected to IBKR — account: %s", self.ib.managedAccounts())

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IBKR")

    def _ensure_connected(self) -> None:
        if not self.ib.isConnected():
            logger.warning("IBKR not connected, attempting reconnect…")
            self.connect()

    # ── Contract helpers ───────────────────────────────────────────────────────

    def _build_contract(self, symbol: str) -> Stock:
        contract = Stock(symbol, settings.EXCHANGE, settings.CURRENCY)
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(
                f"Could not qualify contract for {symbol} on {settings.EXCHANGE}"
            )
        return qualified[0]

    def _get_open_position(self, symbol: str) -> float:
        """Return current net position size for *symbol* (0 if none)."""
        for pos in self.ib.positions():
            if pos.contract.symbol.upper() == symbol.upper():
                return pos.position
        return 0.0

    # ── Order building ─────────────────────────────────────────────────────────

    def _resolve_side_and_qty(self, alert: Alert) -> tuple[str, float]:
        """
        Determine IB action (BUY/SELL) and share quantity.
        CLOSE_LONG  → SELL existing long position
        CLOSE_SHORT → BUY  to cover existing short
        BUY / SELL  → use alert.quantity or DEFAULT_QUANTITY
        """
        if alert.action == "CLOSE_LONG":
            qty = abs(self._get_open_position(alert.symbol))
            if qty == 0:
                raise ValueError(f"No open long position to close for {alert.symbol}")
            return "SELL", qty

        if alert.action == "CLOSE_SHORT":
            qty = abs(self._get_open_position(alert.symbol))
            if qty == 0:
                raise ValueError(f"No open short position to close for {alert.symbol}")
            return "BUY", qty

        side = "BUY" if alert.action == "BUY" else "SELL"
        qty = alert.quantity if alert.quantity is not None else settings.DEFAULT_QUANTITY
        return side, qty

    def _build_order(self, side: str, qty: float, alert: Alert):
        if alert.order_type == "LMT":
            return LimitOrder(side, qty, alert.price)
        if alert.order_type == "STP":
            return StopOrder(side, qty, alert.price)
        return MarketOrder(side, qty)

    # ── Main execute entry point ───────────────────────────────────────────────

    def execute(self, alert: Alert) -> dict:
        """Place an order based on a validated Alert.  Returns order summary."""
        self._ensure_connected()

        contract = self._build_contract(alert.symbol)
        side, qty = self._resolve_side_and_qty(alert)
        order = self._build_order(side, qty, alert)

        logger.info(
            "Placing %s %s x%s (%s) — strategy=%s",
            side,
            alert.symbol,
            qty,
            alert.order_type,
            alert.strategy,
        )

        trade: Trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)  # allow one event loop tick for status update

        summary = {
            "order_id": trade.order.orderId,
            "symbol": alert.symbol,
            "exchange": settings.EXCHANGE,
            "side": side,
            "qty": qty,
            "order_type": alert.order_type,
            "limit_price": alert.price,
            "status": trade.orderStatus.status,
            "strategy": alert.strategy,
            "comment": alert.comment,
        }
        logger.info("Order result: %s", summary)
        return summary
