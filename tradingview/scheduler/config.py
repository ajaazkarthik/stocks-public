"""Environment configuration for the report scheduler.

Credentials are resolved **at startup**, never at import time, and never fall
back to a literal embedded in source. Missing credentials raise
:class:`MissingCredential` with an actionable, secret-free message.
"""

from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path

from tradingview.scheduler.timeutil import (
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_SCHEDULE_TIMEZONE,
    InvalidTimezone,
    load_timezone,
)

#: Environment variable holding the scheduler bot token.
BOT_TOKEN_ENV = "AJAAZ_ALERT_BOT_TOKEN"

#: Optional local secrets file sourced by the launch wrappers.
LOCAL_SECRETS_FILENAME = "scheduler_env.local.sh"

#: Telegram's hard message limit.
TELEGRAM_MESSAGE_LIMIT = 4096

#: Conservative per-part budget: leaves room for part labels and headings and
#: keeps multi-byte characters well inside the hard limit.
TELEGRAM_SAFE_LIMIT = 3500

#: Default freshness deadline for queued trade alerts.
DEFAULT_ALERT_TTL_HOURS = 12.0

#: Default retry policy for the notification outbox.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_CAP_SECONDS = 1800.0
DEFAULT_CLAIM_LEASE_SECONDS = 300.0

_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_]*)"
    r"\s*[:=]\s*\S+"
)


class MissingCredential(RuntimeError):
    """Raised when a required credential is absent from the environment."""


class InvalidConfiguration(ValueError):
    """Raised when a configuration value is present but unusable."""


def redact(text: object) -> str:
    """Return *text* with credential-shaped substrings replaced.

    Used on every log/exception path so a token can never reach a log file,
    a Telegram message, or a test assertion message.
    """
    if text is None:
        return ""
    cleaned = _TOKEN_PATTERN.sub("<redacted-token>", str(text))
    cleaned = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=<redacted>", cleaned)
    return cleaned


def _env_int(env: dict, name: str, default: int) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidConfiguration(f"{name} must be an integer") from exc


def _env_float(env: dict, name: str, default: float) -> float:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise InvalidConfiguration(f"{name} must be a number") from exc


def _env_bool(env: dict, name: str, default: bool) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise InvalidConfiguration(f"{name} must be true or false")


def parse_days(raw: str) -> tuple[int, ...]:
    """Parse ``SCHEDULER_DAYS`` (1=Mon … 7=Sun) into Python weekday numbers.

    Python's :meth:`datetime.weekday` uses 0=Mon … 6=Sun, which is what the
    interval gate compares against. Invalid tokens are rejected rather than
    silently dropped, so a typo cannot quietly disable the calendar.
    """
    days: list[int] = []
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            raise InvalidConfiguration(f"SCHEDULER_DAYS contains a non-numeric day: {token!r}")
        value = int(token)
        if not 1 <= value <= 7:
            raise InvalidConfiguration(f"SCHEDULER_DAYS day out of range 1-7: {value}")
        weekday = value - 1
        if weekday not in days:
            days.append(weekday)
    if not days:
        raise InvalidConfiguration("SCHEDULER_DAYS must list at least one day")
    return tuple(sorted(days))


@dataclass(frozen=True)
class SchedulerConfig:
    """Resolved scheduler configuration."""

    bot_token: str = ""
    allowed_users: tuple[int, ...] = ()
    every_hours: int | None = None
    days: tuple[int, ...] = (0, 1, 2, 3, 4)
    schedule_timezone: str = DEFAULT_SCHEDULE_TIMEZONE
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE
    stock_threshold: float = 62.5
    # Scanner is opt-in on directly constructed configs so older callers/tests
    # retain their behavior. load_config enables the documented production
    # defaults unless SCHEDULER_SCANNER_MARKETS is explicitly empty.
    scanner_markets: tuple[str, ...] = ()
    scanner_scope: str = "production"
    scanner_candidates: int = 20
    scanner_every_hours: int = 2
    scanner_fetch_limit: int = 600
    scanner_profile: str = "broad"
    scanner_step_timeout: int = 600
    scanner_provider_delay_minutes: int = 15
    scanner_max_bar_age_minutes: int = 45
    scanner_price_tolerance_pct: float = 2.0
    scanner_retry_base_seconds: int = 300
    scanner_retry_cap_seconds: int = 1800
    scanner_us_options_enabled: bool = False
    scanner_us_options_max_underlyings: int = 5
    scanner_us_options_max_contracts_per_symbol: int = 1
    scanner_us_options_min_dte: int = 14
    scanner_us_options_max_dte: int = 45
    scanner_us_options_min_open_interest: int = 100
    scanner_us_options_min_volume: int = 10
    scanner_us_options_max_spread_pct: float = 10.0
    scanner_us_options_max_quote_age_seconds: int = 120
    scanner_us_options_quote_mode: str = "strict"
    option_threshold: float = 70.0
    step_timeout: int = 900
    alert_ttl_hours: float = DEFAULT_ALERT_TTL_HOURS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS
    message_limit: int = TELEGRAM_SAFE_LIMIT
    outbox_path: Path | None = None
    run_root: Path | None = None
    extra: dict = field(default_factory=dict)

    def validate(self) -> None:
        """Raise for unusable values; called by :func:`load_config`."""
        load_timezone(self.schedule_timezone)
        load_timezone(self.display_timezone)
        if not math.isfinite(self.stock_threshold) or not 0 <= self.stock_threshold <= 100:
            raise InvalidConfiguration("SCHEDULER_STOCK_THRESHOLD must be finite and between 0 and 100")
        if self.every_hours is not None and self.every_hours <= 0:
            raise InvalidConfiguration("SCHEDULER_EVERY_HOURS must be positive")
        if self.step_timeout <= 0:
            raise InvalidConfiguration("SCHEDULER_STEP_TIMEOUT must be positive")
        if self.scanner_candidates <= 0:
            raise InvalidConfiguration("SCHEDULER_SCANNER_CANDIDATES must be positive")
        if self.scanner_scope not in {"production", "test"}:
            raise InvalidConfiguration("SIGNAL_SCANNER_SCOPE must be production or test")
        if self.scanner_every_hours <= 0:
            raise InvalidConfiguration("SCHEDULER_SCANNER_EVERY_HOURS must be positive")
        if not 1 <= self.scanner_fetch_limit <= 2000:
            raise InvalidConfiguration("SIGNAL_SCANNER_FETCH_LIMIT must be between 1 and 2000")
        if self.scanner_profile not in {"broad", "large_cap"}:
            raise InvalidConfiguration("SIGNAL_SCANNER_PROFILE must be broad or large_cap")
        if self.scanner_step_timeout <= 0:
            raise InvalidConfiguration("SCHEDULER_SCANNER_STEP_TIMEOUT must be positive")
        if not 0 <= self.scanner_provider_delay_minutes <= 120:
            raise InvalidConfiguration("SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES must be between 0 and 120")
        if self.scanner_max_bar_age_minutes < self.scanner_provider_delay_minutes + 15:
            raise InvalidConfiguration("SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES must include provider delay plus one bar")
        if not math.isfinite(self.scanner_price_tolerance_pct) or not 0 < self.scanner_price_tolerance_pct <= 10:
            raise InvalidConfiguration("SIGNAL_SCANNER_PRICE_TOLERANCE_PCT must be finite and between 0 and 10")
        if self.scanner_retry_base_seconds <= 0 or self.scanner_retry_cap_seconds < self.scanner_retry_base_seconds:
            raise InvalidConfiguration("scanner retry cap must be at least its positive retry base")
        if type(self.scanner_us_options_enabled) is not bool:
            raise InvalidConfiguration("SCHEDULER_SCANNER_US_OPTIONS_ENABLED must be boolean")
        from tradingview.scheduler.us_options_policy import USOptionsPolicy
        try:
            USOptionsPolicy(
                max_underlyings=self.scanner_us_options_max_underlyings,
                max_contracts_per_symbol=self.scanner_us_options_max_contracts_per_symbol,
                min_dte=self.scanner_us_options_min_dte,
                max_dte=self.scanner_us_options_max_dte,
                min_open_interest=self.scanner_us_options_min_open_interest,
                min_volume=self.scanner_us_options_min_volume,
                max_spread_pct=self.scanner_us_options_max_spread_pct,
                max_quote_age_seconds=self.scanner_us_options_max_quote_age_seconds,
                quote_mode=self.scanner_us_options_quote_mode,
            )
        except ValueError as exc:
            raise InvalidConfiguration(str(exc)) from exc
        if not math.isfinite(self.option_threshold) or not 0 <= self.option_threshold <= 100:
            raise InvalidConfiguration("SCHEDULER_SCORE_THRESHOLD must be finite and between 0 and 100")
        if any(market not in {"US", "AU", "IN", "SG"} for market in self.scanner_markets):
            raise InvalidConfiguration("SCHEDULER_SCANNER_MARKETS supports US, AU, IN, SG")
        if self.alert_ttl_hours <= 0:
            raise InvalidConfiguration("SCHEDULER_ALERT_TTL_HOURS must be positive")
        if self.max_attempts < 1:
            raise InvalidConfiguration("SCHEDULER_MAX_ATTEMPTS must be at least 1")
        if not 0 < self.message_limit <= TELEGRAM_MESSAGE_LIMIT:
            raise InvalidConfiguration(
                f"SCHEDULER_MESSAGE_LIMIT must be between 1 and {TELEGRAM_MESSAGE_LIMIT}"
            )


def resolve_bot_token(environ: dict | None = None) -> str:
    """Return the configured bot token or raise :class:`MissingCredential`.

    There is intentionally no embedded fallback: a committed literal token is
    both a leak and a silent-misconfiguration hazard.
    """
    env = os.environ if environ is None else environ
    token = str(env.get(BOT_TOKEN_ENV, "") or "").strip()
    if not token:
        raise MissingCredential(
            f"{BOT_TOKEN_ENV} is not set. Export it in your shell, or create an "
            f"ignored local secrets file ({LOCAL_SECRETS_FILENAME}) and source it "
            "before starting the scheduler. No credential value is printed here."
        )
    return token


def load_config(environ: dict | None = None, *, require_token: bool = False) -> SchedulerConfig:
    """Build a :class:`SchedulerConfig` from the environment.

    ``require_token=False`` (the default) lets tests and imports run without
    credentials; the entry point calls it with ``require_token=True``.
    """
    env = os.environ if environ is None else environ

    def _get(name: str, default: str = "") -> str:
        return str(env.get(name, default) or "").strip()

    every_raw = _get("SCHEDULER_EVERY_HOURS")
    every_hours: int | None = None
    if every_raw:
        if not every_raw.isdigit() or int(every_raw) <= 0:
            raise InvalidConfiguration("SCHEDULER_EVERY_HOURS must be a positive integer")
        every_hours = int(every_raw)

    days = parse_days(_get("SCHEDULER_DAYS", "1,2,3,4,5"))

    from tradingview.scheduler.identity import normalize_recipients

    config = SchedulerConfig(
        bot_token=resolve_bot_token(env) if require_token else _get(BOT_TOKEN_ENV),
        allowed_users=tuple(normalize_recipients(_get("TELEGRAM_ALLOWED_USERS").split(","))),
        every_hours=every_hours,
        days=days,
        schedule_timezone=_get("SCHEDULER_TIMEZONE", DEFAULT_SCHEDULE_TIMEZONE),
        display_timezone=_get("SCHEDULER_DISPLAY_TIMEZONE", DEFAULT_DISPLAY_TIMEZONE),
        stock_threshold=_env_float(env, "SCHEDULER_STOCK_THRESHOLD", 62.5),
        scanner_markets=tuple(dict.fromkeys(
            market.strip().upper()
            for market in _get("SCHEDULER_SCANNER_MARKETS", "US,AU,IN,SG").split(",")
            if market.strip()
        )),
        scanner_scope=_get("SIGNAL_SCANNER_SCOPE", "production").strip().lower(),
        scanner_candidates=_env_int(env, "SCHEDULER_SCANNER_CANDIDATES", 20),
        scanner_every_hours=_env_int(env, "SCHEDULER_SCANNER_EVERY_HOURS", 2),
        scanner_fetch_limit=_env_int(env, "SIGNAL_SCANNER_FETCH_LIMIT", 600),
        scanner_profile=_get("SIGNAL_SCANNER_PROFILE", "broad").lower(),
        scanner_step_timeout=_env_int(env, "SCHEDULER_SCANNER_STEP_TIMEOUT", 600),
        scanner_provider_delay_minutes=_env_int(env, "SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES", 15),
        scanner_max_bar_age_minutes=_env_int(env, "SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES", 45),
        scanner_price_tolerance_pct=_env_float(env, "SIGNAL_SCANNER_PRICE_TOLERANCE_PCT", 2.0),
        scanner_retry_base_seconds=_env_int(env, "SCHEDULER_SCANNER_RETRY_BASE_SECONDS", 300),
        scanner_retry_cap_seconds=_env_int(env, "SCHEDULER_SCANNER_RETRY_CAP_SECONDS", 1800),
        scanner_us_options_enabled=_env_bool(env, "SCHEDULER_SCANNER_US_OPTIONS_ENABLED", False),
        scanner_us_options_max_underlyings=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MAX_UNDERLYINGS", 5),
        scanner_us_options_max_contracts_per_symbol=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MAX_CONTRACTS_PER_SYMBOL", 1),
        scanner_us_options_min_dte=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MIN_DTE", 14),
        scanner_us_options_max_dte=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MAX_DTE", 45),
        scanner_us_options_min_open_interest=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MIN_OPEN_INTEREST", 100),
        scanner_us_options_min_volume=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MIN_VOLUME", 10),
        scanner_us_options_max_spread_pct=_env_float(env, "SCHEDULER_SCANNER_US_OPTIONS_MAX_SPREAD_PCT", 10.0),
        scanner_us_options_max_quote_age_seconds=_env_int(env, "SCHEDULER_SCANNER_US_OPTIONS_MAX_QUOTE_AGE_SECONDS", 120),
        scanner_us_options_quote_mode=_get("SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE", "strict").strip().lower(),
        option_threshold=_env_float(env, "SCHEDULER_SCORE_THRESHOLD", 70.0),
        step_timeout=_env_int(env, "SCHEDULER_STEP_TIMEOUT", 900),
        alert_ttl_hours=_env_float(env, "SCHEDULER_ALERT_TTL_HOURS", DEFAULT_ALERT_TTL_HOURS),
        max_attempts=_env_int(env, "SCHEDULER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        backoff_base_seconds=_env_float(
            env, "SCHEDULER_BACKOFF_BASE_SECONDS", DEFAULT_BACKOFF_BASE_SECONDS
        ),
        backoff_cap_seconds=_env_float(
            env, "SCHEDULER_BACKOFF_CAP_SECONDS", DEFAULT_BACKOFF_CAP_SECONDS
        ),
        claim_lease_seconds=_env_float(
            env, "SCHEDULER_CLAIM_LEASE_SECONDS", DEFAULT_CLAIM_LEASE_SECONDS
        ),
        message_limit=_env_int(env, "SCHEDULER_MESSAGE_LIMIT", TELEGRAM_SAFE_LIMIT),
        outbox_path=Path(_get("SCHEDULER_OUTBOX_PATH")) if _get("SCHEDULER_OUTBOX_PATH") else None,
        run_root=Path(_get("SCHEDULER_RUN_ROOT")) if _get("SCHEDULER_RUN_ROOT") else None,
    )
    config.validate()
    return config


__all__ = [
    "BOT_TOKEN_ENV",
    "DEFAULT_ALERT_TTL_HOURS",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_DISPLAY_TIMEZONE",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_SCHEDULE_TIMEZONE",
    "InvalidConfiguration",
    "InvalidTimezone",
    "LOCAL_SECRETS_FILENAME",
    "MissingCredential",
    "SchedulerConfig",
    "TELEGRAM_MESSAGE_LIMIT",
    "TELEGRAM_SAFE_LIMIT",
    "load_config",
    "parse_days",
    "redact",
    "resolve_bot_token",
]
