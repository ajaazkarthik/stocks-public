"""
Structured logging setup for the ibkr_async module.

Uses structlog if installed (JSON output, structured key-value pairs).
Gracefully falls back to stdlib logging so the module is fully usable
without structlog as a hard dependency.

Usage
-----
::

    from ibkr_async.logger import get_logger

    log = get_logger(__name__)
    log.info("positions_fetched", count=5, adapter="tws", elapsed_ms=142.3)
    log.error("connection_failed", host="127.0.0.1", port=7496, reason="refused")

Structlog output (JSON)::

    {"event": "positions_fetched", "count": 5, "adapter": "tws", "elapsed_ms": 142.3,
     "level": "info", "timestamp": "2026-04-22T10:30:00.000000Z", "logger": "ibkr_async.adapters.tws_adapter"}

Stdlib fallback output::

    2026-04-22T10:30:00 ibkr_async.adapters.tws_adapter INFO positions_fetched count=5 adapter='tws'
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# ── Check for structlog availability ─────────────────────────────────────────
_STRUCTLOG_AVAILABLE = False
try:
    import structlog  # type: ignore
    _STRUCTLOG_AVAILABLE = True
except ImportError:
    pass

_configured = False


def _configure_structlog() -> None:
    """One-time structlog configuration for JSON production output."""
    if not _STRUCTLOG_AVAILABLE:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """
    Return a structured logger for the given module name.

    If structlog is installed, returns a structlog BoundLogger with JSON output.
    Otherwise, returns a lightweight stdlib wrapper that accepts the same
    keyword-argument call signature as structlog.

    Both interfaces support::

        log.debug("event", key=value, ...)
        log.info("event", key=value, ...)
        log.warning("event", key=value, ...)
        log.error("event", key=value, ...)
        log.exception("event", key=value, ...)
    """
    global _configured

    if _STRUCTLOG_AVAILABLE:
        if not _configured:
            _configure_structlog()
            _configured = True
        return structlog.get_logger(name)

    # Stdlib fallback
    return _StdlibStructLogger(name)


class _StdlibStructLogger:
    """
    Minimal structlog-compatible wrapper around stdlib logging.

    Formats keyword arguments as ``key=repr(value)`` pairs appended to
    the event string, matching structlog's human-readable rendering.
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            self._log.addHandler(handler)
            self._log.setLevel(logging.DEBUG)

    def _fmt(self, event: str, **kw: Any) -> str:
        if not kw:
            return event
        pairs = " ".join(f"{k}={v!r}" for k, v in kw.items())
        return f"{event} {pairs}"

    def debug(self, event: str, **kw: Any) -> None:
        self._log.debug(self._fmt(event, **kw))

    def info(self, event: str, **kw: Any) -> None:
        self._log.info(self._fmt(event, **kw))

    def warning(self, event: str, **kw: Any) -> None:
        self._log.warning(self._fmt(event, **kw))

    def error(self, event: str, **kw: Any) -> None:
        self._log.error(self._fmt(event, **kw))

    def exception(self, event: str, **kw: Any) -> None:
        self._log.exception(self._fmt(event, **kw))

    def bind(self, **kw: Any) -> "_StdlibStructLogger":
        """Context binding (no-op in stdlib fallback; returns self)."""
        return self
