"""Exchange-backed regular-session checks for scanner markets.

Holiday and special-session dates come from ``pandas_market_calendars``. The
calendar is packaged data, not a live exchange feed, so dates outside the
calendar's declared coverage and missing calendars fail closed.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

MARKET_CALENDARS = {
    "US": ("NYSE", "America/New_York"),
    "AU": ("XASX", "Australia/Sydney"),
    "IN": ("NSE", "Asia/Kolkata"),
    "SG": ("XSES", "Asia/Singapore"),
}
CALENDAR_DATA_START = "2000-01-01"
# Horizon is deliberately per market. XSES holiday rows are precomputed only
# through 2026-12-25 in the installed calendar package; NYSE/ASX use recurring
# rules and NSE's package includes holiday coverage through 2027.
CALENDAR_DATA_SUPPORTED_THROUGH = {
    "US": "2027-12-31",
    "AU": "2027-12-31",
    "IN": "2027-12-31",
    "SG": "2026-12-31",
}


class CalendarCoverageError(RuntimeError):
    """The packaged exchange schedule cannot safely answer this request."""


@lru_cache(maxsize=len(MARKET_CALENDARS))
def _calendar(name: str):
    try:
        return mcal.get_calendar(name)
    except Exception as exc:
        raise RuntimeError(f"exchange calendar unavailable: {name}") from exc


def market_session(market: str, now: datetime, *, force: bool = False) -> tuple[bool, str]:
    """Return whether *now* is inside a scheduled regular trading session.

    Calendar schedules account for exchange holidays, special/early sessions,
    and daylight-saving transitions. SGX's published intraday break is
    applied explicitly because the calendar currently models the outer
    session only.
    """
    key = str(market).upper()
    if key not in MARKET_CALENDARS:
        raise ValueError(f"unsupported scanner market: {market}")
    calendar_name, zone_name = MARKET_CALENDARS[key]
    moment = now
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("scanner clock must be timezone-aware")
    utc_now = moment.astimezone(timezone.utc)
    local = utc_now.astimezone(ZoneInfo(zone_name))
    session_date = local.date().isoformat()
    if force:
        return True, session_date

    supported_through = CALENDAR_DATA_SUPPORTED_THROUGH[key]
    if not CALENDAR_DATA_START <= session_date <= supported_through:
        raise CalendarCoverageError(
            f"{key} calendar package coverage is {CALENDAR_DATA_START} through "
            f"{supported_through}; refusing {session_date}"
        )

    calendar = _calendar(calendar_name)
    try:
        schedule = calendar.schedule(start_date=session_date, end_date=session_date)
    except Exception as exc:
        raise CalendarCoverageError(f"{key} calendar failed for {session_date}") from exc
    if schedule.empty:
        return False, session_date

    row = schedule.iloc[0]
    opened = row["market_open"].to_pydatetime()
    closed = row["market_close"].to_pydatetime()
    active = opened <= utc_now < closed
    if key == "SG" and dtime(12, 0) <= local.timetz().replace(tzinfo=None) < dtime(13, 0):
        active = False
    return active, session_date


def market_session_bounds(market: str, session_date: str) -> tuple[datetime, datetime]:
    """Return the scheduled regular-session open and close in UTC.

    The exchange calendar supplies DST, holiday, and early-close behavior. The
    requested date must be within packaged coverage and must be a session.
    """
    key = str(market).upper()
    if key not in MARKET_CALENDARS:
        raise ValueError(f"unsupported scanner market: {market}")
    try:
        requested = datetime.fromisoformat(str(session_date)).date()
    except ValueError as exc:
        raise ValueError("session_date must be an ISO calendar date") from exc
    canonical_date = requested.isoformat()
    if canonical_date != session_date:
        raise ValueError("session_date must be an ISO calendar date")
    if not CALENDAR_DATA_START <= canonical_date <= CALENDAR_DATA_SUPPORTED_THROUGH[key]:
        raise CalendarCoverageError(f"{key} calendar package does not cover {canonical_date}")
    calendar_name, _zone = MARKET_CALENDARS[key]
    calendar = _calendar(calendar_name)
    try:
        schedule = calendar.schedule(start_date=canonical_date, end_date=canonical_date)
    except Exception as exc:
        raise CalendarCoverageError(f"{key} calendar failed for {canonical_date}") from exc
    if schedule.empty:
        raise CalendarCoverageError(f"{key} is closed on {canonical_date}")
    row = schedule.iloc[0]
    opened = row["market_open"].to_pydatetime().astimezone(timezone.utc)
    closed = row["market_close"].to_pydatetime().astimezone(timezone.utc)
    return opened, closed


__all__ = [
    "CALENDAR_DATA_START", "CALENDAR_DATA_SUPPORTED_THROUGH",
    "CalendarCoverageError", "MARKET_CALENDARS", "market_session", "market_session_bounds",
]
