"""
Risk management checks applied before every order.
All checks must pass (return True) for the order to proceed.
"""
from __future__ import annotations

import logging
from models import Alert
from config import settings

logger = logging.getLogger(__name__)


class RiskManager:
    """Stateless per-alert risk checks."""

    def check(self, alert: Alert) -> tuple[bool, str]:
        """
        Returns (passed: bool, reason: str).
        reason is empty string when passed=True.
        """
        checks = [
            self._check_symbol_whitelist,
            self._check_quantity,
        ]
        for fn in checks:
            ok, reason = fn(alert)
            if not ok:
                logger.warning("Risk check FAILED [%s]: %s", fn.__name__, reason)
                return False, reason
        return True, ""

    # ── Individual checks ──────────────────────────────────────────────────────

    def _check_symbol_whitelist(self, alert: Alert) -> tuple[bool, str]:
        allowed = settings.allowed_symbols_set
        if allowed is None:
            return True, ""
        if alert.symbol not in allowed:
            return False, f"{alert.symbol} is not in the allowed-symbol whitelist"
        return True, ""

    def _check_quantity(self, alert: Alert) -> tuple[bool, str]:
        qty = alert.quantity if alert.quantity is not None else settings.DEFAULT_QUANTITY
        if qty <= 0:
            return False, f"Quantity must be > 0, got {qty}"
        if qty > settings.MAX_QUANTITY:
            return (
                False,
                f"Quantity {qty} exceeds MAX_QUANTITY {settings.MAX_QUANTITY}",
            )
        return True, ""
