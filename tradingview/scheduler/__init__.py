"""Two-hour report-scheduler support package.

Focused, dependency-injected modules used by ``report_scheduler.py``:

* :mod:`tradingview.scheduler.identity`      — stable delivery identities
* :mod:`tradingview.scheduler.timeutil`      — timezone-aware time helpers
* :mod:`tradingview.scheduler.config`        — environment configuration
* :mod:`tradingview.scheduler.steps`         — subprocess execution with deadlines
* :mod:`tradingview.scheduler.stock_results` — typed stock data availability
* :mod:`tradingview.scheduler.outbox`        — durable notification outbox
* :mod:`tradingview.scheduler.formatting`    — safe Telegram message rendering
* :mod:`tradingview.scheduler.cycle`         — produce-once / deliver-many cycle

Nothing here imports the Telegram client or opens a production database at
import time, so tests can exercise every path offline.
"""

from __future__ import annotations

__all__ = [
    "config",
    "cycle",
    "formatting",
    "identity",
    "outbox",
    "steps",
    "stock_results",
    "timeutil",
]
