"""TradingView cash-market scanner cycle used by the Telegram scheduler.

Each market is scanned in its own signal-stack process and handoff directory.
Failures stay local to that market; the successful market results are combined
into the existing durable CycleResult/outbox pipeline.
"""

from __future__ import annotations

import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo
from tradingview.scheduler import stock_results
from tradingview.scheduler.config import SchedulerConfig
from tradingview.scheduler.cycle import (
    CYCLE_FAILED, CYCLE_NO_NEW, CYCLE_OK, CYCLE_PARTIAL,
    CycleLock, CycleResult, _options_result_from_snapshot,
)
from tradingview.scheduler.steps import StepResult
from tradingview.scheduler.timeutil import to_utc, utc_now_iso
from tradingview.scheduler.market_calendar import market_session, market_session_bounds
from tradingview.scheduler.freshness import BAR_INTERVAL_MINUTES, QUOTE_SOURCE

MARKETS = {
    "US": ("stocks_us", "America/New_York"),
    "AU": ("stocks_au", "Australia/Sydney"),
    "IN": ("stocks_in", "Asia/Kolkata"),
    "SG": ("stocks_sg", "Asia/Singapore"),
}


def _valid_freshness_contract(
    row: dict, market: str, session_date: str, observed_at: datetime,
    max_bar_age_minutes: int = 45,
    configured_provider_delay_minutes: int = 15,
) -> bool:
    """Require explicit independent bar provenance at the scheduler boundary."""
    if row.get("quote_source") != QUOTE_SOURCE:
        return False
    if row.get("freshness_assurance") != "independent_chart_crosscheck":
        return False
    if row.get("bar_interval_minutes") != BAR_INTERVAL_MINUTES:
        return False
    if row.get("configured_provider_delay_minutes") != configured_provider_delay_minutes:
        return False
    try:
        start = datetime.fromisoformat(str(row.get("bar_time") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(row.get("bar_end_time") or "").replace("Z", "+00:00"))
        if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
            return False
        observed = to_utc(observed_at)
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if end_utc - start_utc != timedelta(minutes=BAR_INTERVAL_MINUTES):
            return False
        if (start_utc > observed + timedelta(minutes=2)
                or observed - min(end_utc, observed) > timedelta(minutes=max_bar_age_minutes)):
            return False
        from zoneinfo import ZoneInfo
        if start.astimezone(ZoneInfo(MARKETS[market][1])).date().isoformat() != session_date:
            return False
        return True
    except (TypeError, ValueError, KeyError):
        return False


def produce_scanner_cycle(
    *,
    run_id: str,
    config: SchedulerConfig,
    project_root: Path | str,
    python_executable: str,
    runner: Callable[..., StepResult],
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    force: bool = False,
    lock: CycleLock | None = None,
) -> CycleResult:
    """Run enabled market scans sequentially and persist a recoverable cycle."""
    root = Path(project_root)
    run_root = config.run_root or (root / "reports" / "scheduler_runs")
    run_dir = run_root / f"scanner_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    clock_now = clock or (lambda: to_utc(now) if now is not None else datetime.now(timezone.utc))
    started = to_utc(now) if now is not None else to_utc(clock_now())
    result = CycleResult(
        run_id=run_id, started_at=started.isoformat(),
        display_timezone=config.display_timezone,
        paths={"run_dir": run_dir, "cycle_result": run_dir / "cycle_result.json"},
    )
    if lock is not None and not lock.acquire(f"scanner:{run_id}"):
        result.status = CYCLE_FAILED
        result.errors.append("another analysis cycle holds the shared execution lock")
        result.stock_result = stock_results.StockResult(
            status=stock_results.STATUS_UNAVAILABLE, source="scanner", run_id=run_id,
            errors=[result.errors[-1]],
        )
        result.completed_at = utc_now_iso()
        result.write(run_dir / "cycle_result.json")
        return result

    rows: list[dict] = []
    market_statuses: dict[str, dict] = {}
    try:
        for market in config.scanner_markets:
            screener_class, _zone = MARKETS[market]
            market_now = to_utc(clock_now())
            try:
                is_open, session_date = market_session(market, market_now, force=force)
            except Exception as exc:
                market_statuses[market] = {
                    "status": "failed",
                    "session_date": market_now.astimezone(ZoneInfo(MARKETS[market][1])).date().isoformat(),
                    "count": 0, "errors": [f"exchange calendar unavailable: {exc}"],
                }
                result.warnings.append(f"Scanner {market}: exchange calendar unavailable")
                continue
            if not is_open:
                market_statuses[market] = {"status": "off_hours", "session_date": session_date, "count": 0}
                result.warnings.append(f"Scanner {market}: off hours ({session_date})")
                continue

            market_dir = run_dir / market
            market_dir.mkdir(parents=True, exist_ok=True)
            market_run_id = f"{run_id}_{market.lower()}"
            payload_path = market_dir / "stock_results.json"
            options_session_open = not force
            if market == "US" and force:
                # Manual/forced stock scans may run off-hours. Scanner-origin
                # options still require the actual regular US session.
                try:
                    options_session_open = market_session("US", market_now)[0]
                except Exception:
                    options_session_open = False
            options_enabled = (
                market == "US" and config.scanner_us_options_enabled
                and config.scanner_scope == "production" and options_session_open
            )
            options_path = market_dir / "options_signals.json"
            env = dict(os.environ)
            env.update({
                "SIGNAL_RUN_ID": market_run_id,
                "SIGNAL_STOCK_PAYLOAD": str(payload_path),
                "SIGNAL_OPTIONS_SNAPSHOT": str(options_path),
                "SIGNAL_REPORT_DIR": str(market_dir),
                "SIGNAL_SCANNER_MARKET": market,
                "SIGNAL_SCANNER_SESSION_DATE": session_date,
                "SIGNAL_SCANNER_TIMESTAMP": market_now.isoformat(),
                "SIGNAL_SCANNER_FETCH_LIMIT": str(config.scanner_fetch_limit),
                "SIGNAL_SCANNER_PROFILE": config.scanner_profile,
                "SIGNAL_SCANNER_SCOPE": config.scanner_scope,
                "SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES": str(config.scanner_provider_delay_minutes),
                "SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES": str(config.scanner_max_bar_age_minutes),
                "SIGNAL_SCANNER_PRICE_TOLERANCE_PCT": str(config.scanner_price_tolerance_pct),
                # Pass the resolved scheduler policy explicitly. The child
                # must not derive scanner behavior from ambient shell values.
                "SCHEDULER_SCANNER_US_OPTIONS_ENABLED": "true" if options_enabled else "false",
                "SCHEDULER_SCANNER_US_OPTIONS_MAX_UNDERLYINGS": str(config.scanner_us_options_max_underlyings),
                "SCHEDULER_SCANNER_US_OPTIONS_MAX_CONTRACTS_PER_SYMBOL": str(config.scanner_us_options_max_contracts_per_symbol),
                "SCHEDULER_SCANNER_US_OPTIONS_MIN_DTE": str(config.scanner_us_options_min_dte),
                "SCHEDULER_SCANNER_US_OPTIONS_MAX_DTE": str(config.scanner_us_options_max_dte),
                "SCHEDULER_SCANNER_US_OPTIONS_MIN_OPEN_INTEREST": str(config.scanner_us_options_min_open_interest),
                "SCHEDULER_SCANNER_US_OPTIONS_MIN_VOLUME": str(config.scanner_us_options_min_volume),
                "SCHEDULER_SCANNER_US_OPTIONS_MAX_SPREAD_PCT": str(config.scanner_us_options_max_spread_pct),
                "SCHEDULER_SCANNER_US_OPTIONS_MAX_QUOTE_AGE_SECONDS": str(config.scanner_us_options_max_quote_age_seconds),
                "SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE": config.scanner_us_options_quote_mode,
                "SCHEDULER_SCORE_THRESHOLD": str(config.option_threshold),
                "SCHEDULER_STOCK_THRESHOLD": str(config.stock_threshold),
            })
            command = [python_executable, str(root / "tradingview" / "signal_stack.py"),
                       "--screener", screener_class, "--n", str(config.scanner_candidates),
                       "--threshold", f"{config.stock_threshold:g}", "--no-external"]
            if not options_enabled:
                command.append("--no-options")
            command.extend([
                "--no-moomoo", "--no-india",
                "--out", str(market_dir / "signal_stack_report.html"),
                "--actionable-out", str(market_dir / "signal_report_actionable.html"),
            ])
            step = runner(
                f"scanner_{market.lower()}",
                command,
                cwd=root / "tradingview", env=env, timeout=config.scanner_step_timeout,
            )
            result.steps.append(step)
            if not step.produced_fresh_result:
                # The producer commits the stock handoff before starting the
                # optional options stage. Preserve that independently durable
                # result if the bounded subprocess later times out or fails.
                loaded = stock_results.load_current_run_stocks(payload_path, market_run_id)
                if loaded.status not in stock_results.UNAVAILABLE_STATUSES:
                    status, market_rows = loaded.status, loaded.stocks
                    market_errors = [f"signal stack {step.status} after stock payload publication"]
                else:
                    status, market_rows = stock_results.STATUS_FAILED, []
                    market_errors = [f"signal stack {step.status}"]
            else:
                loaded = stock_results.load_current_run_stocks(payload_path, market_run_id)
                status, market_rows, market_errors = loaded.status, loaded.stocks, loaded.errors
                if loaded.status in stock_results.UNAVAILABLE_STATUSES:
                    status = stock_results.STATUS_FAILED

            # Do not manufacture source metadata here: the producer must emit
            # the explicit scanner contract used later by identity/recovery.
            for row in market_rows:
                direction = str(row.get("direction") or "").upper()
                try:
                    score = float(row.get("score", float("nan")))
                except (TypeError, ValueError, OverflowError):
                    score = float("nan")
                valid = (
                    row.get("source") == "scanner"
                    and str(row.get("market") or "").upper() == market
                    and row.get("session_date") == session_date
                    and row.get("actionable") is True
                    and direction in {"BUY", "SELL"}
                    and isinstance(row.get("timestamp"), str)
                    and bool(row.get("timestamp"))
                    and math.isfinite(score)
                    and _valid_freshness_contract(
                        row, market, session_date, market_now,
                        config.scanner_max_bar_age_minutes,
                        config.scanner_provider_delay_minutes,
                    )
                )
                if valid:
                    timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    valid = timestamp.tzinfo is not None and timestamp.astimezone(
                        ZoneInfo(MARKETS[market][1])
                    ).date().isoformat() == session_date
                if not valid:
                    status = stock_results.STATUS_FAILED
                    market_rows = []
                    market_errors = ["scanner payload omitted or invalid required provenance/actionability"]
                    break
            if market_errors and status not in stock_results.UNAVAILABLE_STATUSES:
                status = "partial" if market_rows or status == "partial" else stock_results.STATUS_FAILED

            for row in market_rows:
                rows.append(dict(row))
            stock_market_status = status
            options_result = {"options": [], "status": "disabled", "errors": []}
            options_errors: list[str] = []
            if options_enabled:
                if step.produced_fresh_result:
                    options_result = _options_result_from_snapshot(
                        options_path, market_run_id, config.option_threshold,
                        # Quote freshness and snapshot age are checked when
                        # the child has completed, not at subprocess start.
                        now=to_utc(clock_now()),
                    )
                else:
                    options_result = {
                        "options": [], "status": "unavailable",
                        "errors": ["US signal stack did not complete; options were not produced"],
                    }
                options_errors = list(options_result.get("errors") or [])
                if result.option_result is None:
                    result.option_result = options_result
                for option in options_result.get("options", []):
                    option["scanner_option_cap"] = config.scanner_us_options_max_contracts_per_symbol
                option_failed = options_result.get("status") in {"unavailable", "failed", "partial"}
                if option_failed:
                    if status in stock_results.UNAVAILABLE_STATUSES:
                        status = stock_results.STATUS_FAILED
                    else:
                        status = "partial"

            market_statuses[market] = {
                "status": status, "stock_status": stock_market_status,
                "session_date": session_date, "count": len(market_rows),
                "errors": market_errors, "options_status": options_result.get("status", "disabled"),
                "options_errors": options_errors,
            }
            note = f"Scanner {market}: {status} ({len(market_rows)} actionable)"
            if market_errors:
                note += " — " + "; ".join(market_errors)
            result.warnings.append(note)

        statuses = [item["status"] for item in market_statuses.values()]
        failures = [s for s in statuses if s in stock_results.UNAVAILABLE_STATUSES or s == "partial"]
        valid_markets = [s for s in statuses if s not in stock_results.UNAVAILABLE_STATUSES and s not in {"off_hours", "partial"}]
        if rows:
            result.status = CYCLE_PARTIAL if failures else CYCLE_OK
            stock_status = stock_results.STATUS_SUCCESS
        elif failures and valid_markets:
            result.status = CYCLE_PARTIAL
            stock_status = stock_results.STATUS_FAILED
        elif failures:
            result.status = CYCLE_FAILED
            stock_status = stock_results.STATUS_FAILED
        else:
            result.status = CYCLE_NO_NEW
            stock_status = stock_results.STATUS_NO_NEW
        result.stock_result = stock_results.StockResult(
            status=stock_status, stocks=rows, source="scanner", run_id=run_id,
            total_eligible=len(rows), errors=[
                f"{market}: {error}"
                for market, status in market_statuses.items()
                for error in status.get("errors", [])
            ],
        )
        if result.option_result is None:
            result.option_result = {"status": "disabled", "options": [], "errors": []}
        result.scanner_markets = market_statuses
        result.completed_at = utc_now_iso()
        result.write(run_dir / "cycle_result.json")
        return result
    finally:
        if lock is not None:
            lock.release()


__all__ = ["MARKETS", "market_session", "market_session_bounds", "produce_scanner_cycle"]
