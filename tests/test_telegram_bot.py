"""Tests for F9: telegram_bot.py — push notification and auth logic."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Stub the telegram library before importing telegram_bot ───────────────

def _stub_telegram():
    """Create minimal stubs for python-telegram-bot so we can import telegram_bot."""
    # telegram package
    tg = types.ModuleType("telegram")
    tg.Update = MagicMock
    tg.InlineKeyboardButton = MagicMock
    tg.InlineKeyboardMarkup = MagicMock
    tg.KeyboardButton = MagicMock
    tg.ReplyKeyboardMarkup = MagicMock
    tg.ReplyKeyboardRemove = MagicMock
    sys.modules["telegram"] = tg

    # telegram.constants
    tc = types.ModuleType("telegram.constants")
    tc.ParseMode = type("ParseMode", (), {"MARKDOWN": "Markdown", "MARKDOWN_V2": "MarkdownV2", "HTML": "HTML"})()
    sys.modules["telegram.constants"] = tc

    # telegram.ext
    te = types.ModuleType("telegram.ext")
    te.ApplicationBuilder = MagicMock
    te.CallbackQueryHandler = MagicMock
    te.CommandHandler = MagicMock
    te.ContextTypes = MagicMock
    te.MessageHandler = MagicMock
    te.filters = MagicMock()
    sys.modules["telegram.ext"] = te

    return tg


_stub_telegram()
import telegram_bot as tb


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Auth — _allowed_users and _is_allowed
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    """Security: user whitelist enforcement."""

    def test_allowed_users_single(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "123456"}):
            result = tb._allowed_users()
        assert result == {123456}

    def test_allowed_users_multiple(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111,222,333"}):
            result = tb._allowed_users()
        assert result == {111, 222, 333}

    def test_allowed_users_empty(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}):
            result = tb._allowed_users()
        assert result == set()

    def test_allowed_users_with_spaces(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": " 111 , 222 "}):
            result = tb._allowed_users()
        assert result == {111, 222}

    def test_allowed_users_ignores_non_numeric(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111,abc,222"}):
            result = tb._allowed_users()
        assert result == {111, 222}

    def test_is_allowed_when_whitelisted(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "123456"}):
            assert tb._is_allowed(123456) is True

    def test_is_allowed_when_not_whitelisted(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "123456"}):
            assert tb._is_allowed(999999) is False

    def test_is_allowed_open_when_no_whitelist(self):
        """No whitelist configured → allow all (dev mode)."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}):
            assert tb._is_allowed(999999) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Constants and paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestConstants:
    """Verify key constants and paths are set."""

    def test_root_path(self):
        assert tb.ROOT.exists()
        assert (tb.ROOT / "telegram_bot.py").exists()

    def test_tv_path(self):
        assert tb.TV == tb.ROOT / "tradingview"

    def test_india_path(self):
        assert tb.INDIA == tb.ROOT / "india-brokers"

    def test_reports_path(self):
        assert tb.REPORTS == tb.ROOT / "reports"

    def test_base_env_has_protobuf(self):
        assert tb.BASE_ENV.get("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION") == "python"

    def test_step_timeout(self):
        assert tb._STEP_TIMEOUT == 900

    def test_main_keyboard_exists(self):
        assert tb._MAIN_KEYBOARD is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Inline keyboard
# ═══════════════════════════════════════════════════════════════════════════════

class TestInlineKeyboard:
    def test_main_inline_kb_callable(self):
        """_main_inline_kb is a callable that builds an InlineKeyboardMarkup."""
        assert callable(tb._main_inline_kb)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Command handler existence
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandlersExist:
    """Verify that all documented command handlers exist as functions."""

    @pytest.mark.parametrize("handler_name", [
        "cmd_start", "cmd_help", "cmd_pnl", "cmd_cancel", "cmd_unblock",
    ])
    def test_handler_exists(self, handler_name):
        assert hasattr(tb, handler_name), f"Missing handler: {handler_name}"
        assert callable(getattr(tb, handler_name))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _run_cmd subprocess helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunCmd:
    """_run_cmd() subprocess wrapper tests."""

    def test_successful_command(self):
        """Simple echo command should succeed."""
        rc, output = tb._run_cmd(
            [sys.executable, "-c", "print('hello')"],
            cwd=ROOT,
            user_id=99999,
        )
        assert rc == 0
        assert "hello" in output

    def test_failing_command(self):
        """Command that exits non-zero."""
        rc, output = tb._run_cmd(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            cwd=ROOT,
            user_id=99998,
        )
        assert rc == 1

    def test_output_truncated(self):
        """Long output should be truncated to last N chars."""
        rc, output = tb._run_cmd(
            [sys.executable, "-c", "print('x' * 10000)"],
            cwd=ROOT,
            user_id=99997,
        )
        assert rc == 0
        assert len(output) <= 5000  # should be capped


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Push notification helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestPushHelpers:
    """Tests for proactive push notification logic."""

    def test_fires_raw_path(self):
        """fires_raw.json path should be constructable."""
        fires_path = tb.TV / "fires_raw.json"
        # Just verify the path is a Path object — file may not exist
        assert isinstance(fires_path, Path)

    def test_cache_positions_path(self):
        """Cached positions path should point to project root."""
        cache = tb.ROOT / ".cache_global_positions.json"
        assert isinstance(cache, Path)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
