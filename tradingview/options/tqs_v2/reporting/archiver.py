"""
TQS v2 Report Archiver
========================
Before any HTML or JSON report is overwritten, this module copies the
existing file to reports/archive/ with a timestamp suffix.

This ensures NO report is ever lost — every run's output is preserved.

Archive naming pattern:
  reports/archive/tqs_v2_calls_20260523_143022.html
  reports/archive/tqs_v2_calls_20260523_143022.json

Usage (called from html_reporter.py and json_reporter.py):
    from .archiver import archive_if_exists
    archive_if_exists(out_path)
    out_path.write_text(...)
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("tqs_v2.reporting.archiver")


def archive_if_exists(out_path: Path) -> Path | None:
    """
    If ``out_path`` exists, copy it to reports/archive/ with a timestamp suffix.

    Returns the archive path if a copy was made, otherwise None.

    Thread-safety note: uses a timestamp with seconds resolution.
    Two concurrent writes within the same second would overwrite each other in
    the archive, but that cannot happen in a sequential single-process pipeline.
    """
    if not out_path.exists():
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = out_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stem = out_path.stem          # e.g. "tqs_v2_calls"
    suffix = out_path.suffix      # e.g. ".html"
    archive_path = archive_dir / f"{stem}_{ts}{suffix}"

    shutil.copy2(str(out_path), str(archive_path))
    _log.info("Archived previous report: %s → %s", out_path.name, archive_path.name)
    return archive_path
