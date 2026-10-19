"""
ZeroDTE Report Archiver
========================
Archives existing reports before overwriting — same pattern as TQS v2.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def archive_if_exists(out_path: Path) -> Path | None:
    """
    If the output file already exists, copy it to an archive/ subdirectory
    with a timestamp suffix before it is overwritten.

    Returns the archive path if archived, or None if file did not exist.
    """
    if not out_path.exists():
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = out_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stem = out_path.stem
    suffix = out_path.suffix
    archive_path = archive_dir / f"{stem}_{ts}{suffix}"

    shutil.copy2(str(out_path), str(archive_path))
    return archive_path
