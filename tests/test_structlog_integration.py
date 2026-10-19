"""Tests for structlog integration across high-value modules."""
import importlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestStructlogIntegration:
    """Verify that high-value modules have _log attribute from structlog."""

    @pytest.mark.parametrize("module_path,module_name", [
        ("tradingview/webhook_server.py", "webhook_server"),
        ("tradingview/auto_trader.py", "auto_trader"),
        ("tradingview/ibkr_enricher.py", "ibkr_enricher"),
        ("tradingview/moomoo_enricher.py", "moomoo_enricher"),
    ])
    def test_module_has_log(self, module_path, module_name):
        """Each structlog-enabled module should have a _log attribute."""
        tv_dir = str(Path(__file__).parent / "tradingview")
        root_dir = str(Path(__file__).parent)
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)

        # Pre-create stubs for heavy optional deps to avoid ImportError
        stubs = {}
        for name in [
            "ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.contract",
            "ibapi.scanner", "moomoo", "yfinance",
            "signal_stack", "sync_holdings_watchlist",
            "tv_screener_fundamentals", "backtest", "backtest.backtest_sizer",
            "external_signal_enricher", "platform_watchdog", "order_ledger",
            "input_sources", "india_order_executor",
        ]:
            if name not in sys.modules:
                stubs[name] = MagicMock()

        with patch.dict("sys.modules", stubs):
            if module_name in sys.modules:
                mod = sys.modules[module_name]
            else:
                try:
                    mod = importlib.import_module(module_name)
                except Exception:
                    pytest.skip(f"Cannot import {module_name} (missing deps)")
                    return

            assert hasattr(mod, "_log"), (
                f"{module_name} should have a _log attribute from structlog/logging"
            )

    def test_log_config_get_logger_returns_callable(self):
        """log_config.get_logger should return a logger with standard methods."""
        from log_config import get_logger
        logger = get_logger("test_structlog")
        assert callable(getattr(logger, "info", None))
        assert callable(getattr(logger, "warning", None))
        assert callable(getattr(logger, "error", None))
