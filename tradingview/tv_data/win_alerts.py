"""
Canonical WIN alert identifiers.

These IDs are the single source of truth shared by the downloader
(``tv_data/download_fires.py``) and the scorer (``signal_stack.py``). Keeping
them in one side-effect-free module prevents the two copies from drifting apart
(review 2026-10-13, Issue 3).

This module must stay import-safe: no network calls, no ``sys.exit()``, no
filesystem access at import time.
"""

from __future__ import annotations

# WIN strategy alerts on the TradingView account.
WIN_2H_ID = 2381963449   # 2-hour resolution
WIN_1H_ID = 2381961922   # 1-hour resolution

#: The set of alert IDs the default downloaded-WIN scoring mode accepts.
WIN_ALERT_IDS = frozenset({WIN_2H_ID, WIN_1H_ID})
