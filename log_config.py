"""
Centralised structured-logging configuration for the stocks trading system.
============================================================================

**Problem solved**: The codebase relied on 7,800+ ``print()`` calls with zero
structured context.  Diagnosing missed alerts, broker outages, or slow enrichers
was nearly impossible because there were no correlation IDs, no severity levels,
and no machine-parseable output.

**Solution**: This module provides a thin, project-wide logging facade built on
*structlog*.  Every log entry automatically carries:

* ``run_id``   — a per-invocation UUID that ties together all events within a
  single menu-handler, webhook, or dashboard run.
* ``ts``       — ISO-8601 timestamp.
* ``module``   — the name of the calling module.
* ``level``    — standard severity (debug / info / warning / error / critical).

Usage
-----
::

    from log_config import get_logger

    log = get_logger(__name__)          # bind module name automatically
    log.info("portfolio_fetched", broker="moomoo", positions=42)
    log.warning("broker_timeout", broker="zerodha", elapsed_ms=3200)

The logger is configured once at import time.  Subsequent ``get_logger()`` calls
return bound loggers that share the same ``run_id``.

Console output
--------------
During interactive CLI use (the normal case), logs are rendered as coloured
key-value lines to *stderr* — print() output to *stdout* is untouched so that
menu prompts and user-facing output remain readable.

JSON output
-----------
Set the environment variable ``STOCKS_LOG_JSON=1`` to switch to one-JSON-object-
per-line output.  This is useful for piping into log aggregators or files.

Log files
---------
When ``LOG_DIR`` exists (``<repo>/logs/``), a rotating JSON log file is written
there automatically (``stocks.log``, max 5 MB, 3 backups).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import uuid
from pathlib import Path

import structlog

# ── Run ID: generated once per process invocation ──────────────────────────────
RUN_ID: str = uuid.uuid4().hex[:12]

# ── Paths ──────────────────────────────────────────────────────────────────────
_LOG_DIR = Path(__file__).parent.resolve() / "logs"

# ── Configuration ──────────────────────────────────────────────────────────────
_JSON_MODE = os.environ.get("STOCKS_LOG_JSON", "").strip() in ("1", "true", "yes")
_LOG_LEVEL = os.environ.get("STOCKS_LOG_LEVEL", "INFO").upper()

# ── Processor that injects run_id into every event ─────────────────────────────
def _add_run_id(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict,
) -> dict:
    """Inject the process-wide ``run_id`` into every log event."""
    event_dict.setdefault("run_id", RUN_ID)
    return event_dict


class _WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that silently skips rotation on Windows PermissionError.

    On Windows, ``os.rename`` fails with ``PermissionError [WinError 32]`` when any
    other thread (e.g. a background WebSocket thread) still holds the log file open.
    Rather than printing a noisy traceback at shutdown, we catch the error and
    continue writing to the existing file — the rotation will succeed next time.
    """

    def doRollover(self) -> None:  # type: ignore[override]
        try:
            super().doRollover()
        except PermissionError:
            pass  # Another thread has the file open; skip this rotation cycle


def _setup_stdlib_logging() -> None:
    """Configure stdlib root logger so third-party libraries route through structlog."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

    # Remove existing handlers to avoid double-logging
    root.handlers.clear()

    # Console handler → stderr (so print() on stdout is undisturbed)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
    root.addHandler(console)

    # File handler (JSON, rotating) if logs/ exists
    if _LOG_DIR.is_dir():
        try:
            fh = _WindowsSafeRotatingFileHandler(
                str(_LOG_DIR / "stocks.log"),
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
                encoding="utf-8",
                delay=True,
            )
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)
        except Exception:
            pass  # non-critical — if we can't log to file, console is fine

    # Silence websocket-client internal logger to prevent log noise
    logging.getLogger("websocket").setLevel(logging.ERROR)
    logging.getLogger("websocket").propagate = False


def _configure_structlog() -> None:
    """Wire structlog processors and renderers."""
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        _add_run_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if _JSON_MODE:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Also set the formatter on stdlib handlers so structlog renders them
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


# ── Initialise on import ──────────────────────────────────────────────────────
_setup_stdlib_logging()
_configure_structlog()


# ── Public API ─────────────────────────────────────────────────────────────────
def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound with the given module *name*.

    Parameters
    ----------
    name : str, optional
        Typically ``__name__``.  If omitted, the logger is unnamed.

    Returns
    -------
    structlog.stdlib.BoundLogger
        A logger instance that carries ``run_id`` automatically.
    """
    return structlog.get_logger(name or "stocks")


def new_run_id() -> str:
    """Generate and set a fresh ``run_id``.

    Call this at the start of a new logical operation (e.g. a menu-handler
    invocation) so that all subsequent log events within that handler share a
    unique correlation ID.

    Returns
    -------
    str
        The new 12-character hex run ID.
    """
    global RUN_ID
    RUN_ID = uuid.uuid4().hex[:12]
    return RUN_ID
