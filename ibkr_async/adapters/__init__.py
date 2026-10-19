"""
Adapters subpackage for ibkr_async.

Provides:
- TWSAdapter  : Real-time live data via ib_async socket connection to TWS/IB Gateway
- FlexAdapter : Batch T+1 data via IBKR Flex Query HTTPS Web Service

Both implement IBKRDataPort.  Only TWSAdapter implements IBKROrderPort.
"""

from .tws_adapter import TWSAdapter
from .flex_adapter import FlexAdapter

__all__ = ["TWSAdapter", "FlexAdapter"]
