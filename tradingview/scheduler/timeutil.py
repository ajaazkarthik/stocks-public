"""Timezone-aware time helpers for the report scheduler.

Two rules this module exists to enforce:

1. **Persisted timestamps are UTC instants.** Everything written to a database
   or a payload goes through :func:`utc_now` / :func:`to_utc`.
2. **Displayed timestamps carry a real label.** The scheduler previously
   formatted host-local time and appended the literal ``IST``, which was wrong
   on any host that is not in India. :func:`format_display` renders the actual
   zone abbreviation and UTC offset for an explicit display timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Default user-facing display timezone (this user is in Sydney).
DEFAULT_DISPLAY_TIMEZONE = "Australia/Sydney"

#: Timezone used by the documented morning/evening schedules.
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Kolkata"


class InvalidTimezone(ValueError):
    """Raised when a configured timezone name cannot be resolved."""


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC instant."""
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Convert *value* to UTC, rejecting naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def utc_now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string."""
    return utc_now().isoformat()


def load_timezone(name: str | None, *, default: str = DEFAULT_DISPLAY_TIMEZONE) -> ZoneInfo:
    """Resolve *name* to a :class:`ZoneInfo`, falling back to *default*.

    An empty/``None`` name selects the default. An unknown name raises
    :class:`InvalidTimezone` so misconfiguration is visible instead of being
    silently rendered in the wrong zone.
    """
    candidate = (name or "").strip() or default
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezone(f"unknown timezone: {candidate!r}") from exc


def offset_label(moment: datetime) -> str:
    """Return a compact ``UTC+HH:MM`` label for *moment*."""
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError("datetime must be timezone-aware")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def zone_label(moment: datetime) -> str:
    """Return the zone abbreviation when available, else the offset label."""
    name = moment.tzname()
    if name and name.upper() not in {"UTC", "UNKNOWN"}:
        return name
    return offset_label(moment)


def format_display(
    moment: datetime,
    timezone_name: str | None = None,
    *,
    fmt: str = "%d %b %Y %H:%M",
    with_offset: bool = True,
) -> str:
    """Render *moment* in *timezone_name* with an explicit zone label.

    The result always names the zone actually used, so a Sydney host can never
    be mislabelled as IST.
    """
    zone = load_timezone(timezone_name)
    local = to_utc(moment).astimezone(zone)
    label = zone_label(local)
    if with_offset:
        label = f"{label} {offset_label(local)}"
    return f"{local.strftime(fmt)} {label}"


def format_utc(moment: datetime, *, fmt: str = "%d %b %Y %H:%M") -> str:
    """Render *moment* as an explicit UTC instant."""
    return f"{to_utc(moment).strftime(fmt)} UTC"


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting a trailing ``Z``.

    Naive input is rejected: an unlabelled timestamp cannot be compared to a
    UTC instant without guessing.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def try_parse_iso(value: str) -> datetime | None:
    """Return the parsed UTC instant, or ``None`` when *value* is unusable."""
    try:
        return parse_iso(value)
    except (TypeError, ValueError):
        return None
