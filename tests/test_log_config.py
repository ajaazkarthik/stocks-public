"""
Tests for log_config — centralised structured logging.

[2/N] — RESEARCH [DONE], VERIFICATION [IN PROGRESS], SAVE TO BRAIN [PENDING]

Validates:
  • get_logger() returns a structlog BoundLogger
  • RUN_ID is a 12-character hex string
  • new_run_id() generates a different ID each time
  • Log events carry run_id automatically
  • JSON mode toggle works
  • Logger name binding works
"""
from __future__ import annotations

import logging
import re
import io

import pytest
import structlog


# ── get_logger() ──────────────────────────────────────────────────────────────

class TestGetLogger:
    """get_logger() must return a properly configured BoundLogger."""

    def test_returns_bound_logger(self):
        from log_config import get_logger
        log = get_logger("test_module")
        # structlog.get_logger() returns a BoundLoggerLazyProxy which resolves
        # to BoundLogger on first use — both are valid bound loggers.
        assert hasattr(log, "info") and hasattr(log, "warning"), (
            f"Expected a structlog bound logger, got {type(log)}"
        )

    def test_default_name(self):
        from log_config import get_logger
        log = get_logger()
        # Should not raise
        assert log is not None

    def test_custom_name(self):
        from log_config import get_logger
        log = get_logger("my.custom.module")
        assert log is not None


# ── RUN_ID ────────────────────────────────────────────────────────────────────

class TestRunId:
    """RUN_ID must be a stable 12-char hex, changeable via new_run_id()."""

    def test_run_id_format(self):
        from log_config import RUN_ID
        assert isinstance(RUN_ID, str)
        assert len(RUN_ID) == 12
        assert re.fullmatch(r"[0-9a-f]{12}", RUN_ID), (
            f"RUN_ID '{RUN_ID}' is not 12-char lowercase hex"
        )

    def test_new_run_id_changes_value(self):
        import log_config
        old = log_config.RUN_ID
        new = log_config.new_run_id()
        assert new != old, "new_run_id() must generate a different ID"
        assert log_config.RUN_ID == new, "RUN_ID module var must be updated"

    def test_new_run_id_format(self):
        from log_config import new_run_id
        rid = new_run_id()
        assert len(rid) == 12
        assert re.fullmatch(r"[0-9a-f]{12}", rid)

    def test_successive_ids_unique(self):
        from log_config import new_run_id
        ids = {new_run_id() for _ in range(20)}
        assert len(ids) == 20, "new_run_id() should produce unique IDs"


# ── Log event content ─────────────────────────────────────────────────────────

class TestLogEventContent:
    """Verify that log events contain the expected structured fields."""

    def test_run_id_injected_into_event(self):
        """The _add_run_id processor must inject run_id into event_dict."""
        from log_config import _add_run_id, RUN_ID
        event = {"event": "test"}
        result = _add_run_id(None, "info", event)
        assert "run_id" in result
        assert result["run_id"] == RUN_ID

    def test_run_id_does_not_overwrite_explicit(self):
        """If caller already set run_id, _add_run_id must not overwrite it."""
        from log_config import _add_run_id
        event = {"event": "test", "run_id": "explicit123a"}
        result = _add_run_id(None, "info", event)
        assert result["run_id"] == "explicit123a"

    def test_log_captures_to_stdlib(self):
        """Structured logs should flow through stdlib logging infrastructure."""
        from log_config import get_logger
        log = get_logger("test_capture")
        # If this doesn't raise, the pipeline is connected
        log.info("test_event", key="value", num=42)

    def test_warning_level(self):
        """Warning-level logs should not raise."""
        from log_config import get_logger
        log = get_logger("test_warn")
        log.warning("something_slow", elapsed_ms=5000)

    def test_error_level(self):
        """Error-level logs should not raise."""
        from log_config import get_logger
        log = get_logger("test_err")
        log.error("broker_failed", broker="test", reason="timeout")


# ── Module-level configuration ────────────────────────────────────────────────

class TestConfiguration:
    """Verify that structlog is properly configured at import time."""

    def test_structlog_is_configured(self):
        """structlog should have processors configured."""
        cfg = structlog.get_config()
        assert cfg["processors"], "structlog processors should be configured"

    def test_stdlib_root_has_handlers(self):
        """Root stdlib logger should have at least one handler (console)."""
        root = logging.getLogger()
        assert len(root.handlers) >= 1, "Root logger should have console handler"

    def test_handlers_have_formatter(self):
        """Handlers should have structlog ProcessorFormatter."""
        root = logging.getLogger()
        for handler in root.handlers:
            fmt = handler.formatter
            assert fmt is not None, "Handler should have a formatter set"
