"""
Connection management subpackage for ibkr_async.

Provides:
- ConnectionManager : Full lifecycle manager with retry and health monitoring
- is_tws_reachable  : Lightweight TCP probe (no ib_async required)
"""

from .manager import ConnectionManager
from .health import is_tws_reachable

__all__ = ["ConnectionManager", "is_tws_reachable"]
