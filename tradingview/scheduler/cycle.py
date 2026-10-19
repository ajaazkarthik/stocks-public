"""Produce-once / deliver-many cycle orchestration.

``produce_cycle`` runs the analysis pipeline exactly once and returns an
immutable :class:`CycleResult`. ``deliver_cycle`` then enqueues that same result
for every recipient, with independent receipts and retries.

Splitting the two is what fixes the multi-recipient defect: previously the whole
pipeline ran inside the recipient loop, so the first recipient consumed the new
fires and the second saw an empty or different result.

Dependency rules enforced by ``produce_cycle``:

* A failed **download** stops fresh scoring and fresh options production. The
  preserved cache is never presented as a fresh result.
* **Settlement** is maintenance: its failure is recorded as a warning and
  scoring continues.
* A failed **scorer** publishes no fresh stock or option results.
* Every step result — including launch failures and timeouts — is persisted so
  the cycle can still be reported and retried.
"""

from __future__ import annotations

import json
import hashlib
import os
import socket
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from tradingview.scheduler import formatting, identity, stock_results
from tradingview.scheduler.config import SchedulerConfig, redact
from tradingview.scheduler.steps import StepResult, run_step
from tradingview.scheduler.timeutil import (
    format_display,
    load_timezone,
    to_utc,
    utc_now,
    utc_now_iso,
)

SCHEMA_VERSION = 1

CYCLE_OK = "ok"
CYCLE_PARTIAL = "partial"
CYCLE_FAILED = "failed"
CYCLE_NO_NEW = "no_new"

STEP_DOWNLOAD = "download_fires"
STEP_SETTLE = "settle_outcomes"
STEP_SCORE = "signal_stack"


def new_run_id(now: datetime | None = None) -> str:
    """Return a fresh cycle run ID."""
    moment = to_utc(now) if now is not None else utc_now()
    return f"ss_{moment.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"


def should_run_analysis(
    now_utc: datetime,
    allowed_days: Sequence[int],
    schedule_timezone: str,
) -> bool:
    """Return True when *now_utc* falls on an allowed weekday.

    The weekday is evaluated in *schedule_timezone*, not on the host clock, so
    a UTC/local date boundary cannot silently shift the calendar. ``allowed_days``
    uses Python weekday numbering (0=Mon … 6=Sun).
    """
    zone = load_timezone(schedule_timezone)
    local = to_utc(now_utc).astimezone(zone)
    return local.weekday() in {int(day) for day in allowed_days}


@dataclass
class CyclePaths:
    """Run-scoped output paths.

    Automated runs get their own directory so a manual run cannot replace the
    scheduler's in-flight input (and vice versa).
    """

    root: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.root / self.run_id

    @property
    def stock_payload(self) -> Path:
        return self.run_dir / "stock_results.json"

    @property
    def options_snapshot(self) -> Path:
        return self.run_dir / "options_signals.json"

    @property
    def cycle_result(self) -> Path:
        return self.run_dir / "cycle_result.json"

    def ensure(self) -> "CyclePaths":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class CycleResult:
    """Immutable outcome of one analysis cycle."""

    run_id: str
    started_at: str
    completed_at: str = ""
    status: str = CYCLE_OK
    steps: list[StepResult] = field(default_factory=list)
    stock_result: stock_results.StockResult | None = None
    option_result: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scanner_markets: dict[str, dict] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    display_timezone: str = ""
    paths: dict = field(default_factory=dict)

    @property
    def stocks(self) -> list[dict]:
        return list(self.stock_result.stocks) if self.stock_result else []

    @property
    def options(self) -> list[dict]:
        result = self.option_result or {}
        return list(result.get("options") or [])

    @property
    def options_status(self) -> str:
        return str((self.option_result or {}).get("status") or "unavailable")

    @property
    def options_errors(self) -> list[str]:
        return list((self.option_result or {}).get("errors") or [])

    @property
    def stock_status(self) -> str:
        return self.stock_result.status if self.stock_result else stock_results.STATUS_UNAVAILABLE

    @property
    def has_new_actionable(self) -> bool:
        return bool(self.stocks or self.options)

    def step(self, label: str) -> StepResult | None:
        for result in self.steps:
            if result.label == label:
                return result
        return None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "display_timezone": self.display_timezone,
            "steps": [step.to_dict() for step in self.steps],
            "stock_result": self.stock_result.to_dict() if self.stock_result else None,
            "option_result": {
                "status": self.options_status,
                "errors": [redact(error) for error in self.options_errors],
                "options": self.options,
            },
            "errors": [redact(error) for error in self.errors],
            "warnings": [redact(warning) for warning in self.warnings],
            "scanner_markets": self.scanner_markets,
            "paths": {key: str(value) for key, value in self.paths.items()},
        }

    def write(self, path: Path | str) -> None:
        """Persist the cycle result atomically."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(target)


class CycleLock:
    """Kernel-backed cross-process guard, with ownership metadata for inspection.

    The persistent ``.guard`` inode is never unlinked: all contenders lock the
    same file even while the readable ownership file is being initialized or
    removed. Kernel locks disappear on process exit, so reclamation needs no
    read/unlink race. Legacy ownership files are checked under the kernel lock.
    """

    def __init__(self, path: Path | str, *, stale_seconds: float = 3600.0) -> None:
        self.path = Path(path)
        self.stale_seconds = float(stale_seconds)
        self._owner: str | None = None
        self._fd: int | None = None
        self._pid: int | None = None

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _owner_alive(self, data: dict) -> bool:
        pid = data.get("pid")
        host = str(data.get("host") or "")
        if host and host != socket.gethostname():
            return False
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _is_stale(self, data: dict) -> bool:
        from tradingview.scheduler.timeutil import try_parse_iso

        if str(data.get("host") or "") == socket.gethostname():
            return not self._owner_alive(data)
        parsed = try_parse_iso(str(data.get("started_at") or ""))
        return parsed is None or utc_now() - parsed > timedelta(seconds=self.stale_seconds)

    @staticmethod
    def _kernel_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _kernel_unlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def acquire(self, owner: str) -> bool:
        if self._fd is not None:
            return self._pid == os.getpid() and self._owner == str(owner)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard = self.path.with_name(self.path.name + ".guard")
        try:
            fd = os.open(str(guard), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        locked = False
        try:
            self._kernel_lock(fd)
            locked = True
            data = self._read()
            # A pre-upgrade live process does not hold the kernel guard yet.
            if data and data.get("lock_protocol") != "kernel-v1" and not self._is_stale(data):
                return False
            payload = json.dumps({
                "owner": str(owner), "started_at": utc_now_iso(),
                "pid": os.getpid(), "host": socket.gethostname(),
                "lock_protocol": "kernel-v1",
            }).encode("utf-8")
            metadata_fd = os.open(str(self.path), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    count = os.write(metadata_fd, remaining)
                    if count <= 0:
                        raise OSError("could not publish cycle lock owner")
                    remaining = remaining[count:]
                os.fsync(metadata_fd)
            finally:
                os.close(metadata_fd)
            self._fd, self._owner, self._pid = fd, str(owner), os.getpid()
            return True
        except OSError:
            return False
        finally:
            if self._fd != fd:
                if locked:
                    self._kernel_unlock(fd)
                os.close(fd)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            if self._pid == os.getpid():
                if str(self._read().get("owner") or "") == self._owner:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                self._kernel_unlock(fd)
        finally:
            os.close(fd)
            self._fd, self._owner, self._pid = None, None, None

    @property
    def acquired(self) -> bool:
        return self._fd is not None and self._pid == os.getpid()

    def __enter__(self) -> "CycleLock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _options_result_from_snapshot(
    snapshot_path: Path,
    run_id: str,
    threshold: float,
    *,
    max_age_hours: float = 6.0,
    now: datetime | None = None,
) -> dict:
    """Read and validate the current run's options snapshot.

    Reuses the existing snapshot contract (schema, run ID, freshness, atomic
    publication) rather than inventing a parallel format.
    """
    import math

    from tradingview.options.options_snapshot import read_snapshot, option_quote_fields

    snapshot, validation_errors = read_snapshot(
        snapshot_path, expected_run_id=run_id, max_age_hours=max_age_hours, now=now,
    )
    if validation_errors or snapshot is None:
        return {
            "options": [],
            "status": "unavailable",
            "errors": list(validation_errors or ["options snapshot unavailable"]),
        }

    status = snapshot.get("status", "unavailable")
    if status in {"disabled", "failed", "unavailable"}:
        return {
            "options": [],
            "status": status,
            "errors": _error_texts(snapshot.get("errors") or [f"options status: {status}"]),
        }

    options: list[dict] = []
    for contract in snapshot.get("contracts", []):
        try:
            score = float(contract["option_score"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(score) or score < threshold:
            continue
        options.append({
            "exchange": contract["exchange"],
            "symbol": contract["symbol"],
            "opt_type": contract["option_type"],
            "strike": contract["strike"],
            "expiry": contract["expiry"],
            "dte": contract["dte"],
            "score": round(score, 1),
            "parent_score": contract["parent_signal_score"],
            "option_score": score,
            "parent_signal_score": contract["parent_signal_score"],
            "strategy_modes": contract.get("strategy_modes", []),
            **option_quote_fields(contract),
        })
    return {
        "options": options,
        "status": "empty" if not options and status == "success" else status,
        "errors": _error_texts(snapshot.get("errors") or []),
    }


def _error_texts(errors: Iterable) -> list[str]:
    return [
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors
    ]


def produce_cycle(
    *,
    run_id: str,
    paths: CyclePaths,
    config: SchedulerConfig,
    python_executable: str | None = None,
    project_root: Path | str | None = None,
    env: dict | None = None,
    runner: Callable[..., StepResult] = run_step,
    now: datetime | None = None,
    cancelled: Callable[[], bool] | None = None,
    lock: "CycleLock | None" = None,
) -> CycleResult:
    """Run the analysis pipeline once and return its durable result.

    All external effects are injected: ``runner`` executes subprocesses,
    ``paths`` isolates outputs, and ``config`` supplies thresholds and timeouts.

    When ``lock`` is supplied it guards the shared-input analysis stage so a
    manual run and a scheduled run cannot overlap on the same fires cache.
    """
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    tv = root / "tradingview"
    executable = python_executable or sys.executable
    started = to_utc(now) if now is not None else utc_now()
    paths.ensure()

    if lock is not None and not lock.acquire(f"cycle:{run_id}"):
        result = CycleResult(
            run_id=run_id,
            started_at=started.isoformat(),
            completed_at=utc_now_iso(),
            status=CYCLE_FAILED,
            display_timezone=config.display_timezone,
            errors=["another analysis cycle holds the shared execution lock"],
            paths={"run_dir": paths.run_dir, "cycle_result": paths.cycle_result},
        )
        result.write(paths.cycle_result)
        return result

    result = CycleResult(
        run_id=run_id,
        started_at=started.isoformat(),
        display_timezone=config.display_timezone,
        paths={
            "run_dir": paths.run_dir,
            "stock_payload": paths.stock_payload,
            "options_snapshot": paths.options_snapshot,
            "cycle_result": paths.cycle_result,
        },
    )

    step_env = dict(env or os.environ)
    step_env["SIGNAL_RUN_ID"] = run_id
    step_env["SIGNAL_STOCK_PAYLOAD"] = str(paths.stock_payload)
    step_env["SIGNAL_OPTIONS_SNAPSHOT"] = str(paths.options_snapshot)
    # Run-scoped report outputs so a manual run cannot replace the scheduler's
    # in-flight artifacts (and vice versa).
    step_env["SIGNAL_REPORT_DIR"] = str(paths.run_dir)

    def _run(label: str, cmd: Sequence[str], cwd: Path) -> StepResult:
        return runner(
            label,
            list(cmd),
            cwd=cwd,
            env=step_env,
            timeout=config.step_timeout,
            cancelled=cancelled,
        )

    try:
        return _produce_cycle_body(
            result=result, paths=paths, config=config, tv=tv, executable=executable,
            step_env=step_env, run_id=run_id, _run=_run,
        )
    finally:
        if lock is not None:
            lock.release()


def _produce_cycle_body(
    *,
    result: "CycleResult",
    paths: "CyclePaths",
    config: SchedulerConfig,
    tv: Path,
    executable: str,
    step_env: dict,
    run_id: str,
    _run: Callable[[str, Sequence[str], Path], StepResult],
) -> "CycleResult":
    """Inner body of :func:`produce_cycle` (lock already held)."""

    # ── 1. Download fires (required) ──────────────────────────────────────
    download = _run(
        STEP_DOWNLOAD,
        [executable, str(tv / "tv_data" / "download_fires.py")],
        tv,
    )
    result.steps.append(download)

    if not download.produced_fresh_result:
        result.status = CYCLE_FAILED
        result.errors.append(
            f"fresh fire download did not complete ({download.status}); "
            "scoring and options production were skipped"
        )
        result.stock_result = stock_results.StockResult(
            status=stock_results.STATUS_UNAVAILABLE,
            errors=["fresh fire download failed — stock data unavailable for this cycle"],
            source="current_run",
            run_id=run_id,
        )
        result.option_result = {
            "options": [],
            "status": "unavailable",
            "errors": ["fresh fire download failed — options not produced for this cycle"],
        }
        result.completed_at = utc_now_iso()
        result.write(paths.cycle_result)
        return result

    # ── 2. Settle outcomes (maintenance; failure is a warning) ────────────
    settle = _run(
        STEP_SETTLE,
        [executable, str(tv / "alert_tracker.py"), "--update", "--compute"],
        tv,
    )
    result.steps.append(settle)
    if not settle.produced_fresh_result:
        result.warnings.append(
            f"outcome settlement failed ({settle.status}); scoring continued"
        )

    # ── 3. Score new fires (required for fresh results) ───────────────────
    score = _run(
        STEP_SCORE,
        [
            executable, str(tv / "signal_stack.py"),
            "--new-only",
            "--threshold", f"{config.stock_threshold:g}",
            "--no-external",
        ],
        tv,
    )
    result.steps.append(score)

    if not score.produced_fresh_result:
        result.status = CYCLE_FAILED
        result.errors.append(
            f"scoring did not complete ({score.status}); no fresh stock or option "
            "results are published for this cycle"
        )
        result.stock_result = stock_results.StockResult(
            status=stock_results.STATUS_UNAVAILABLE,
            errors=["scoring failed — stock results unavailable for this cycle"],
            source="current_run",
            run_id=run_id,
        )
        result.option_result = {
            "options": [],
            "status": "unavailable",
            "errors": ["scoring failed — options not produced for this cycle"],
        }
        result.completed_at = utc_now_iso()
        result.write(paths.cycle_result)
        return result

    # ── 4. Consume the current run's durable results ──────────────────────
    result.stock_result = stock_results.load_current_run_stocks(paths.stock_payload, run_id)
    result.option_result = _options_result_from_snapshot(
        paths.options_snapshot, run_id, config.option_threshold
    )

    if result.stock_result.status in stock_results.UNAVAILABLE_STATUSES:
        result.warnings.append(
            "current-run stock payload unavailable: "
            + "; ".join(result.stock_result.errors or ["unknown reason"])
        )
    if result.options_status in {"unavailable", "failed"}:
        result.warnings.append(
            "options unavailable: " + "; ".join(result.options_errors or ["unknown reason"])
        )

    # Status is derived from availability/failure BEFORE row count. A cycle
    # whose required data sources are unavailable is not a healthy no-new run.
    stocks_unavailable = result.stock_status in stock_results.UNAVAILABLE_STATUSES
    options_unavailable = result.options_status in {"unavailable", "failed"}

    if result.has_new_actionable:
        result.status = CYCLE_OK if not result.warnings else CYCLE_PARTIAL
    elif stocks_unavailable and options_unavailable:
        result.status = CYCLE_FAILED
        result.errors.append(
            "no fresh stock or option results are available for this cycle"
        )
    elif stocks_unavailable or options_unavailable:
        result.status = CYCLE_PARTIAL
    else:
        result.status = CYCLE_NO_NEW

    result.completed_at = utc_now_iso()
    result.write(paths.cycle_result)
    return result


def _summary_content(
    result: CycleResult,
    *,
    limit: int,
    analysis_time: datetime | None = None,
) -> tuple[str, list[str], list[str], list[str]]:
    """Build the source blocks before bounded rendering."""
    moment = analysis_time or utc_now()
    stamp = format_display(moment, result.display_timezone)
    header = f"Recommendations\n{stamp.split(' UTC')[0]}"
    stock_lines = (["STOCKS"] + [formatting.stock_card(row) for row in result.stocks]
                   if result.stocks else [])
    option_lines = (["OPTIONS"] + [formatting.option_card(row) for row in result.options]
                    if result.options else [])
    notes = [step.summary() for step in result.steps if not step.ok]
    notes.extend(result.errors)
    # Scanner success summaries are diagnostics, not failures.
    notes.extend(note for note in result.warnings if not any(
        note.startswith(f"Scanner {market}: {status}")
        for market in ("US", "AU", "IN", "SG") for status in ("success", "no_new")
    ))
    if result.stock_status in stock_results.UNAVAILABLE_STATUSES:
        notes.extend(result.stock_result.errors if result.stock_result and result.stock_result.errors
                     else ["Stock data unavailable for this cycle."])
    if result.options_status in {"unavailable", "failed", "partial"}:
        notes.extend(result.options_errors or ["Options data unavailable for this cycle."])
    if result.status in {CYCLE_FAILED, CYCLE_PARTIAL} and not notes:
        notes.append("Scan did not complete successfully; see the scheduler log.")
    notes = list(dict.fromkeys(formatting.compact_error_detail(note) for note in notes))
    if not stock_lines and not option_lines:
        header = f"Scan error\n{stamp.split(' UTC')[0]}"

    return header, stock_lines, option_lines, notes


def build_summary_parts(
    result: CycleResult,
    *,
    limit: int,
    analysis_time: datetime | None = None,
) -> list[formatting.MessagePart]:
    """Render the cycle summary into safe, bounded message parts."""
    header, stocks, options, notes = _summary_content(
        result, limit=limit, analysis_time=analysis_time,
    )
    return formatting.render_summary(
        header=header, stock_lines=stocks, option_lines=options, notes=notes, limit=limit,
    )


def _summary_parts_with_events(
    result: CycleResult, recipient: int, *, limit: int,
    analysis_time: datetime | None = None,
) -> list[tuple[formatting.MessagePart, list[dict]]]:
    """Associate rendered fragments with exact source-row character ranges.

    The formatter preserves the source's plain characters in order, inserting
    labels and dropping only block separators at part boundaries. Tracking
    offsets distinguishes identical-looking rows belonging to different fires.
    An oversized row can intersect several parts; every carrier then names the
    event, and the outbox acknowledges it only after all carriers succeed.
    """
    header, stocks, options, notes = _summary_content(
        result, limit=limit, analysis_time=analysis_time,
    )
    parts = formatting.render_summary(
        header=header, stock_lines=stocks, option_lines=options, notes=notes, limit=limit,
    )
    if not parts:
        return []
    events = _event_identities(result, recipient)
    source = header
    ranges: list[tuple[int, int, dict]] = []
    for lines, rows, row_events in (
        (stocks, result.stocks, events[:len(result.stocks)]),
        (options, result.options, events[len(result.stocks):]),
    ):
        if not lines:
            continue
        source += "\n"
        for index, line in enumerate(lines):
            if index:
                source += "\n"
            start = len(source)
            source += line
            if rows and 1 <= index < 1 + len(rows):
                ranges.append((start, len(source), row_events[index - 1]))
    if notes:
        source += "\n" + "\n".join(formatting.render_error_note("note", note) for note in notes)

    annotated = []
    cursor = 0
    for part in parts:
        body = part.plain_text
        if part.label:
            prefix = part.label + "\n\n"
            if not body.startswith(prefix):
                raise ValueError("summary part label differs from its plain body")
            body = body[len(prefix):]
        if not source.startswith(body, cursor):
            if source[cursor:cursor + 1] == "\n" and source.startswith(body, cursor + 1):
                cursor += 1
            else:
                raise ValueError("summary renderer changed source order; cannot reserve events")
        end = cursor + len(body)
        members = [event for start, stop, event in ranges if start < end and stop > cursor]
        annotated.append((part, members))
        cursor = end
    if cursor != len(source):
        raise ValueError("summary renderer omitted source content")
    return annotated


def _event_identities(result: CycleResult, recipient: int) -> list[dict]:
    """Return the per-fire/contract event identities carried by *result*.

    These are the identities that make deduplication real: a later cycle
    containing the same fire or contract must not send it again.
    """
    events: list[dict] = []
    for stock in result.stocks:
        events.append({
            "delivery_key": identity.stock_delivery_key(recipient, stock),
            "recipient": recipient,
            "kind": identity.KIND_STOCK,
            "run_id": result.run_id,
        })
    for option in result.options:
        event = {
            "delivery_key": identity.option_delivery_key(recipient, option),
            "recipient": recipient,
            "kind": identity.KIND_OPTION,
            "run_id": result.run_id,
        }
        if option.get("source") == "scanner" and option.get("market") == "US":
            event["scanner_option_reservation"] = {
                "market": "US", "symbol": option.get("symbol"),
                "session_date": option.get("session_date"),
                "cap": option.get("scanner_option_cap"),
                "expires_at": option.get("recommendation_expires_at"),
                "recommendation": dict(option),
            }
        events.append(event)
    return events


def _recipient_result(result: CycleResult, recipient: int, events: list[dict]) -> CycleResult:
    keys = {event["delivery_key"] for event in events}
    seen: set[str] = set()

    def selected(rows: list[dict], key_for: Callable) -> list[dict]:
        selected_rows = []
        for row in rows:
            key = key_for(recipient, row)
            if key in keys and key not in seen:
                selected_rows.append(row)
                seen.add(key)
        return selected_rows

    stocks = selected(result.stocks, identity.stock_delivery_key)
    options = selected(result.options, identity.option_delivery_key)
    stock_result = replace(result.stock_result, stocks=stocks, total_eligible=len(stocks)) if result.stock_result else None
    return replace(
        result, stock_result=stock_result,
        option_result={**(result.option_result or {}), "options": options},
    )


def enqueue_cycle(
    outbox,
    result: CycleResult,
    recipients: Sequence[int],
    *,
    limit: int,
    analysis_time: datetime | None = None,
    scanner_option_cap: int | None = None,
) -> int:
    """Reserve undelivered recipient events and their exact message parts atomically."""
    from tradingview.scheduler.timeutil import try_parse_iso

    original_time = try_parse_iso(result.started_at)
    if original_time is None:
        return 0  # Missing provenance must not create a fresh deadline on recovery.
    expires_at = original_time + timedelta(hours=outbox.alert_ttl_hours) if outbox.alert_ttl_hours > 0 else None
    queued = 0
    for recipient in identity.normalize_recipients(recipients):
        def build_items(events: list[dict]) -> list[dict]:
            groups: list[list[dict]]
            scanner_events = [event for event in events if event.get("scanner_option_reservation")]
            if scanner_events:
                ordinary = [event for event in events if not event.get("scanner_option_reservation")]
                groups = ([ordinary] if ordinary else []) + [[event] for event in scanner_events]
            else:
                groups = [events]
            items = []
            if not groups:
                # Failure-only retry after all recommendation identities were
                # already reserved or delivered.
                groups = [[]]
            part_number = 0
            for group_number, group_events in enumerate(groups):
                filtered = _recipient_result(result, recipient, group_events) if (
                    result.has_new_actionable
                ) else result
                group_run_id = result.run_id if group_number == 0 else f"{result.run_id}:scanner-option:{group_number}"
                for part, members in _summary_parts_with_events(
                    filtered, recipient, limit=limit, analysis_time=analysis_time,
                ):
                    delivery_run_id = group_run_id
                    if result.scanner_markets and not filtered.has_new_actionable:
                        # Failure-only scanner notifications are once per market
                        # session and normalized cause, including after restart.
                        # Recommendation identities and retries stay independent.
                        _, _, _, failure_notes = _summary_content(
                            filtered, limit=limit, analysis_time=analysis_time,
                        )
                        scope = sorted((market, state.get("session_date"))
                                       for market, state in result.scanner_markets.items())
                        digest = hashlib.sha256(json.dumps(
                            [scope, sorted(failure_notes)], sort_keys=True,
                        ).encode()).hexdigest()
                        delivery_run_id = f"scanner-error:{digest}"
                    items.append({
                        "delivery_key": identity.summary_delivery_key(recipient, delivery_run_id, part_number),
                        "recipient": recipient,
                        "kind": identity.KIND_SUMMARY,
                        "payload": {
                            "run_id": result.run_id, "part": part_number,
                            "status": result.status, "stock_status": result.stock_status,
                            "options_status": result.options_status, "events": members,
                            "plain_text": part.plain_text, "label": part.label,
                        },
                        "message_text": part.html_text, "run_id": result.run_id,
                        "parse_mode": formatting.PARSE_MODE_HTML,
                    })
                    part_number += 1
            return items

        if result.has_new_actionable:
            added = outbox.enqueue_event_parts(
                recipient=recipient, events=_event_identities(result, recipient),
                build_items=build_items, expires_at=expires_at,
                **({"scanner_option_cap": scanner_option_cap}
                   if scanner_option_cap is not None else {}),
            )
            queued += added
            if not added:
                # Recommendations may already be reserved or delivered, but a
                # new failure in this cycle must still reach this recipient.
                for item in build_items([]):
                    queued += bool(outbox.enqueue(**item, expires_at=expires_at))
        else:
            for item in build_items([]):
                queued += bool(outbox.enqueue(**item, expires_at=expires_at))
    return queued


def _plain_body(body: str) -> str:
    """Plain fallback for legacy persisted rows without a plain-text body."""
    import html
    import re

    return html.unescape(re.sub(r"</?(?:pre|b)>", "", body))


async def deliver_cycle(
    outbox,
    result: CycleResult,
    recipients: Sequence[int],
    *,
    bot: Any = None,
    limit: int,
    send: Callable[..., Any] | None = None,
    analysis_time: datetime | None = None,
    owner: str | None = None,
) -> dict[int, list[formatting.DeliveryReceipt]]:
    """Deliver *result* to every recipient with independent receipts.

    A failure for one recipient never blocks another, and a part already
    acknowledged is not re-sent.
    """
    from tradingview.scheduler.outbox import new_owner_id

    owner = owner or new_owner_id("deliver")
    receipts: dict[int, list[formatting.DeliveryReceipt]] = {}
    for recipient in identity.normalize_recipients(recipients):
        persisted = outbox.items_for_run(result.run_id, recipient=recipient, kind=identity.KIND_SUMMARY)
        persisted.sort(key=lambda item: (item.payload.get("part", 0), item.id))
        recipient_receipts = []
        for persisted_item in persisted:
            # Claim just before sending so earlier slow parts do not consume
            # the leases of later parts in the same cycle.
            claimed = outbox.claim_by_keys(owner, [persisted_item.delivery_key])
            if not claimed:
                recipient_receipts.append(formatting.DeliveryReceipt(
                    label=str(persisted_item.payload.get("label") or ""), status="skipped",
                ))
                continue
            item = claimed[0]
            # The durable body is authoritative even if delivery's input or
            # limit differs from the original per-recipient rendering.
            part = formatting.MessagePart(
                html_text=item.message_text,
                plain_text=str(item.payload.get("plain_text") or _plain_body(item.message_text)),
                label=str(item.payload.get("label") or ""),
            )
            fence = {"owner": owner, "claim_token": item.claim_token}
            lost_claim = False

            async def send_owned(**kwargs):
                nonlocal lost_claim
                # Recheck immediately before every external attempt, including
                # an HTML-to-plain fallback after a slow formatting rejection.
                if not outbox.renew_claim(item.id, **fence):
                    lost_claim = True
                    raise RuntimeError("delivery claim no longer belongs to this worker")
                response = send(**kwargs) if send is not None else bot.send_message(**kwargs)
                return await response if hasattr(response, "__await__") else response

            receipt = (await formatting.send_parts(bot, recipient, [part], send=send_owned))[0]
            if lost_claim:
                receipt.status = "skipped"
                recipient_receipts.append(receipt)
                continue
            if receipt.delivered:
                accepted = outbox.mark_sent(item.id, receipt.message_id, **fence)
            elif receipt.ambiguous:
                accepted = outbox.mark_ambiguous(item.id, receipt.error or "no acknowledgement", **fence)
            else:
                accepted = outbox.mark_failed(
                    item.id, receipt.error or "send failed", retry_after=receipt.retry_after,
                    terminal=receipt.error_class == formatting.ERROR_PERMISSION, **fence,
                )
            if not accepted:
                receipt.status = "skipped"
                receipt.error = "delivery claim no longer belongs to this worker"
            recipient_receipts.append(receipt)
        receipts[recipient] = recipient_receipts
    return receipts


__all__ = [
    "CYCLE_FAILED",
    "CYCLE_NO_NEW",
    "CYCLE_OK",
    "CYCLE_PARTIAL",
    "CycleLock",
    "CyclePaths",
    "CycleResult",
    "SCHEMA_VERSION",
    "STEP_DOWNLOAD",
    "STEP_SCORE",
    "STEP_SETTLE",
    "build_summary_parts",
    "deliver_cycle",
    "enqueue_cycle",
    "new_run_id",
    "produce_cycle",
    "should_run_analysis",
]
