"""Small atomic persistence helpers for scanner cadence and retry state."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile


def load_cadence(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version") == 1 and isinstance(payload.get("markets"), dict):
            result = {}
            for key, value in payload["markets"].items():
                if not isinstance(value, dict):
                    continue
                safe = {}
                for field in ("last_success", "last_attempt", "retry_at"):
                    try:
                        number = float(value.get(field, 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        number = 0.0
                    safe[field] = number if math.isfinite(number) and number >= 0 else 0.0
                try:
                    failures = int(value.get("failure_count", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    failures = 0
                safe["failure_count"] = max(0, min(failures, 1000))
                result[str(key)] = safe
            return result
    except (OSError, ValueError, TypeError):
        pass
    return {}


def save_cadence(path: Path, markets: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "markets": markets}, sort_keys=True, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


__all__ = ["load_cadence", "save_cadence"]
