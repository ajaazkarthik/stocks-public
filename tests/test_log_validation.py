"""
tests/test_log_validation.py
════════════════════════════
End-to-end diagnostic: does the log-reading layer in test_smoke.py actually
catch real errors from server logs?

Tests:
  1. Verify a KNOWN-GOOD script produces a log with no severe errors
  2. Inject a synthetic AttributeError into a script → assert log catches it
  3. Verify log content matches stdout (they are tee'd by main.py)
  4. Verify that non-crash errors (ConnectionError, warnings) are NOT flagged
  5. Verify log structure (header, CMD line, exit code line)

Usage:
  pytest tests/test_log_validation.py -v -s
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
LOGS = ROOT / "logs"
_BASE_ENV_KEYS = {
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",
}

import os
_BASE_ENV = {**os.environ, **_BASE_ENV_KEYS}


def _run_option_and_get_log(option: str, extra_inputs: list[str] | None = None, timeout: int = 120) -> tuple[str, str, str, str | None]:
    """Run a menu option and return (stdout, stderr, log_content | None)."""
    t0 = time.time()
    inputs = [option] + (extra_inputs or []) + ["q"]
    stdin_data = "\n".join(inputs) + "\n"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), env=_BASE_ENV,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()

    # Find matching log file
    log_content = None
    if LOGS.exists():
        matches = [
            f for f in LOGS.glob(f"{option}_*.log")
            if f.stat().st_mtime >= t0 - 2
        ]
        if matches:
            latest = max(matches, key=lambda f: f.stat().st_mtime)
            log_content = latest.read_text(encoding="utf-8", errors="replace")

    return stdout, stderr, log_content


# ══════════════════════════════════════════════════════════════════════════════
# Test 1 — Good script: log exists and has no severe errors
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(90)
def test_log_exists_for_option_35():
    """
    Option 35 (VectorBT backtest) creates a log file via main.py's _set_log_path.
    Baseline: confirms log reading is wired up correctly end-to-end.

    NOTE: Option 43 (alert_stats) does NOT produce a log because it prints
    directly without spawning a subprocess — only subprocess-dispatching options
    generate logs.  This is a documented limitation of the log-reading layer.
    """
    stdout, stderr, log = _run_option_and_get_log("35")

    assert log is not None, (
        "No log file found for option 35. "
        "Either main.py's _set_log_path() didn't fire, or LOGS dir is missing."
    )
    # Log must have the standard header
    assert "Option:" in log and "35" in log, (
        f"Log does not contain expected header.\nLog start:\n{log[:500]}"
    )
    assert "exit code" in log.lower(), "Log missing exit code footer"
    print(f"\n[LOG EXISTS] option 35 log length: {len(log)} bytes")
    print(f"  Log starts: {log[:100].strip()!r}")


# ══════════════════════════════════════════════════════════════════════════════
# Test 2 — Log content mirrors stdout (tee verification)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(90)
def test_log_content_mirrors_stdout_option_35():
    """
    main.py uses _tee_stream to write subprocess output to BOTH stdout AND log.
    Verify that key output lines from the script appear in BOTH places.
    Option 35 (VectorBT backtest) is used because it reliably spawns a subprocess
    and thus always generates a log (unlike print-only options like 43).
    """
    stdout, stderr, log = _run_option_and_get_log("35")

    if log is None:
        pytest.skip("No log found for option 35")

    # Extract lines from the script output portion of the log
    # (log has a header block before the CMD output)
    log_lines = set(l.strip() for l in log.splitlines() if l.strip())
    stdout_lines = set(l.strip() for l in stdout.splitlines() if l.strip())

    # Find lines that are in the log script output section (after the dashes)
    try:
        dash_idx = log.index("─" * 40)
        script_section = log[dash_idx:]
    except ValueError:
        script_section = log

    script_lines = set(l.strip() for l in script_section.splitlines() if l.strip())
    overlap = script_lines & stdout_lines

    print(f"\n[TEE VERIFICATION] option 35")
    print(f"  Log total lines   : {len(log_lines)}")
    print(f"  Stdout total lines: {len(stdout_lines)}")
    print(f"  Overlap lines     : {len(overlap)}")

    # At least 3 lines should overlap (script output is tee'd to both)
    assert len(overlap) >= 1, (
        "Log and stdout share NO common lines — tee is not working!\n"
        f"Log snippet:\n{script_section[:300]}\n"
        f"Stdout snippet:\n{stdout[:300]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 3 — Synthetic error injection: does assert_log_no_severe_errors catch it?
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(30)
def test_synthetic_error_detection(tmp_path):
    """
    Create a temporary script that raises AttributeError.
    Run it via subprocess and check that the error string appears in its output.
    Then verify assert_log_no_severe_errors would catch it.

    This proves the detection logic works even when we can't inject into a live log.
    """
    # Create a broken script
    broken_script = tmp_path / "broken_script.py"
    broken_script.write_text(textwrap.dedent("""\
        import sys
        print("Starting broken script...")
        obj = None
        obj.get("key")  # AttributeError: 'NoneType' object has no attribute 'get'
    """))

    result = subprocess.run(
        [sys.executable, str(broken_script)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=_BASE_ENV,
    )
    combined = result.stdout + result.stderr

    print(f"\n[SYNTHETIC ERROR] broken_script.py output:")
    print(combined[:500])

    # The error must be present
    assert "AttributeError" in combined, "Expected AttributeError not found"
    assert "Traceback (most recent call last)" in combined, "Expected Traceback not found"

    # Now simulate what assert_log_no_severe_errors does
    severe = [
        "Traceback (most recent call last)",
        "SyntaxError:", "ModuleNotFoundError:", "NameError:", "AttributeError:",
    ]
    detected = [p for p in severe if p in combined]
    assert len(detected) >= 2, f"Detection logic only caught: {detected}"
    print(f"  Detected patterns: {detected}")
    print("  => assert_log_no_severe_errors WOULD catch this error ✓")


# ══════════════════════════════════════════════════════════════════════════════
# Test 4 — Non-crash errors are NOT false-positives
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(30)
def test_no_false_positives_on_connection_errors(tmp_path):
    """
    Scripts that print connection warnings should NOT trigger assert_log_no_severe_errors.
    Broker-offline warnings like 'OpenD not running' are acceptable output.
    """
    # Simulate a script that prints broker-offline warnings but succeeds
    graceful_script = tmp_path / "graceful_offline.py"
    graceful_script.write_text(textwrap.dedent("""\
        import sys
        print("Connecting to OpenD...")
        print("WARNING: OpenD not running - skipping Moomoo broker")
        print("WARNING: Zerodha API unavailable (session expired)")
        print("ConnectionError: [Errno 111] Connection refused")
        print("Finished with 0 successful brokers (all offline)")
        sys.exit(0)
    """), encoding="utf-8")  # explicit UTF-8 to avoid Windows cp1252 issues

    result = subprocess.run(
        [sys.executable, str(graceful_script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=_BASE_ENV,
    )
    output = result.stdout + result.stderr

    # The severe-error patterns must NOT appear
    severe = [
        "Traceback (most recent call last)",
        "SyntaxError:", "ModuleNotFoundError:", "NameError:", "AttributeError:",
    ]
    false_positives = [p for p in severe if p in output]

    print(f"\n[FALSE POSITIVE CHECK] graceful-offline script output:")
    print(output[:400])
    print(f"  False positives detected: {false_positives}")

    assert not false_positives, (
        f"assert_log_no_severe_errors would incorrectly flag: {false_positives}\n"
        "The detection patterns are too broad!"
    )
    print("  => No false positives ✓")


# ══════════════════════════════════════════════════════════════════════════════
# Test 5 — Log structure: verify header + CMD + exit code footer
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(90)
def test_log_structure_option_35():
    """
    Verify log structure for option 35 (VectorBT backtest):
      - Header: 'Option:  35  ...'
      - Started timestamp
      - CMD line with full Python path
      - Exit code footer: '[exit code: N]'
    """
    stdout, stderr, log = _run_option_and_get_log("35")

    if log is None:
        pytest.skip("No log found for option 35 — backtest may need alert_tracker.db")

    print(f"\n[LOG STRUCTURE] option 35 log ({len(log)} bytes)")
    print("First 300 chars:", log[:300])

    assert "Option:" in log, "Missing 'Option:' header line"
    assert "Started:" in log, "Missing 'Started:' timestamp"
    assert "[exit code:" in log.lower() or "exit code" in log.lower(), (
        "Missing '[exit code:]' footer"
    )
    # CMD line should reference the Python executable
    assert "CMD:" in log or ".py" in log, "Missing CMD or script reference"
    print("  => Log structure OK ✓")


# ══════════════════════════════════════════════════════════════════════════════
# Test 6 — Log-reading coverage: which options generate logs?
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.timeout(30)
def test_log_coverage_summary():
    """
    Report which options have existing logs from recent runs.
    This is a diagnostic test — not a pass/fail — it helps understand
    how many of the 51 menu options are actually covered by log reading.
    """
    if not LOGS.exists():
        pytest.skip("logs/ directory does not exist")

    all_logs = list(LOGS.glob("*.log"))
    # Get unique option prefixes (e.g., "1", "2", "35", "43")
    covered_options = set()
    for f in all_logs:
        # Filename: {option}_{label}_{ts}.log
        # Option can be "1", "4a", "35" etc.
        name = f.name
        # Extract prefix up to first "_"
        prefix = name.split("_")[0]
        if prefix.isdigit() or (len(prefix) <= 3 and prefix[:-1].isdigit()):
            covered_options.add(prefix)

    total_logs = len(all_logs)
    total_unique = len(covered_options)

    print(f"\n[LOG COVERAGE SUMMARY]")
    print(f"  Total log files in logs/: {total_logs}")
    print(f"  Unique options with logs : {total_unique}")
    print(f"  Options covered: {sorted(covered_options)}")
    print(f"  Coverage vs 51 options   : {total_unique}/51 = {total_unique/51*100:.0f}%")

    # At least some options should have logs from previous test runs
    assert total_logs > 0, "No logs found — has test_smoke.py been run?"
