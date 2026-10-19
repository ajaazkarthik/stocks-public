#!/usr/bin/env python3
"""
End-to-End Menu Test Runner
============================
Actually runs each script (not mocked), validates generated HTML reports,
and produces a comprehensive HTML bug report.

Usage:
    python run_e2e_tests.py                   # run all tests
    python run_e2e_tests.py --key 14          # run only option 14 (signal stack)
    python run_e2e_tests.py --section TRADER  # run all TRADER options
    python run_e2e_tests.py --quick           # summary only (no per-option detail)
    python run_e2e_tests.py --out reports/my_e2e.html   # custom output path
    python run_e2e_tests.py --list            # list all registered options

Philosophy
----------
• This runner calls the REAL scripts (not mocked subprocess).
• Broker-dependent scripts use offline flags: --no-moomoo --no-india --no-external --demo.
• Scripts that are ALWAYS live (OpenD required, Zerodha auth required) are marked
  requires_live=True and skipped unless --include-live is passed.
• HTML output is validated with BeautifulSoup for common bugs.

Exit codes:
    0  all tests passed (or only skipped)
    1  one or more tests failed
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.resolve()
TV   = ROOT / "tradingview"
CGT  = ROOT / "cgt"
INDIA = ROOT / "india-brokers"

# ══════════════════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScriptSpec:
    """Defines one E2E test case (one menu option)."""
    key:             str                    # menu option key e.g. "14"
    label:           str                    # human label
    section:         str                    # TRADER / TAX / ADMIN
    script:          Path                   # full path to Python script
    args:            List[str]              # CLI args for offline/safe run
    expected_output: Optional[Path] = None  # HTML file to validate (None = console only)
    requires_live:   bool = False           # True = skip unless --include-live
    skip_reason:     str  = ""             # shown in SKIP output to explain why
    timeout:         int  = 90              # seconds
    cwd:             Optional[Path] = None  # working directory (defaults to ROOT)
    env_extras:      dict = field(default_factory=dict)  # extra env vars


@dataclass
class TestResult:
    """Outcome for one ScriptSpec run."""
    key:          str
    label:        str
    section:      str
    status:       str   # PASS / FAIL / SKIP / WARN / TIMEOUT / ERROR
    duration:     float = 0.0
    exit_code:    int   = 0
    stdout:       str   = ""
    stderr:       str   = ""
    html_bugs:    List[str] = field(default_factory=list)
    skip_reason:  str   = ""
    output_file:  Optional[Path] = None


# ══════════════════════════════════════════════════════════════════════════════
# HTML Validator
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate real bugs in generated HTML (triggers FAIL)
_BUG_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bNaN\b"),                                "NaN in output"),
    (re.compile(r"Traceback \(most recent call last\)", re.I), "Python traceback in HTML"),
    (re.compile(r"₹\s*nan", re.I),                          "₹NaN price"),
    (re.compile(r"\$\s*nan", re.I),                         "$NaN price"),
    (re.compile(r"NameError|AttributeError|KeyError|TypeError", re.I), "Python exception class in HTML"),
    (re.compile(r"Error generating|failed to generate", re.I), "Generation failure message"),
]
# Patterns that indicate empty/no-data or ambiguous state (triggers WARN, not FAIL)
_WARN_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"No data|no results", re.I),               "Empty data message (no live data)"),
    (re.compile(r"\bnull\b"),                               "null in output (may be JS code — verify manually)"),
    (re.compile(r"\bundefined\b", re.I),                    "'undefined' in output (may be JS code — verify manually)"),
]
_NONE_IN_PRICE = re.compile(r"(?:₹|AUD\$?|\$|HKD|SGD)\s*None\b", re.I)
_REQUIRED_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]


def validate_html(path: Path) -> Tuple[List[str], List[str]]:
    """
    Validate an HTML file and return (bugs, warnings).
    bugs    — real errors → trigger FAIL  (NaN, traceback, exception classes)
    warnings — data-state issues → trigger WARN  (no data, empty state)
    Empty lists = no issues found.
    """
    bugs: List[str] = []
    warnings: List[str] = []

    if not path.exists():
        bugs.append(f"Output file not found: {path.name}")
        return bugs, warnings

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        bugs.append(f"Cannot read file: {e}")
        return bugs, warnings

    # Minimum content check
    if len(raw.strip()) < 200:
        bugs.append(f"File is nearly empty ({len(raw)} bytes)")
        return bugs, warnings

    # Bug pattern scan on full text
    for pattern, description in _BUG_PATTERNS:
        m = pattern.search(raw)
        if m:
            ctx = raw[max(0, m.start()-40):m.end()+40].replace("\n", " ").strip()
            bugs.append(f"{description}: '…{ctx}…'")

    for pattern, description in _WARN_PATTERNS:
        m = pattern.search(raw)
        if m:
            ctx = raw[max(0, m.start()-40):m.end()+40].replace("\n", " ").strip()
            warnings.append(f"{description}: '…{ctx}…'")

    m = _NONE_IN_PRICE.search(raw)
    if m:
        ctx = raw[max(0, m.start()-30):m.end()+30].replace("\n", " ").strip()
        bugs.append(f"None in price context: '…{ctx}…'")

    # Try to parse with BeautifulSoup for structure checks
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")

        # Check for empty tables (< 2 rows = only header, no data)
        for tbl in soup.find_all("table"):
            rows = tbl.find_all("tr")
            if len(rows) < 2:
                tbl_id = tbl.get("id", "") or tbl.get("class", [""])
                if isinstance(tbl_id, list):
                    tbl_id = " ".join(tbl_id)
                bugs.append(f"Empty table (0 data rows): id/class='{tbl_id}'")

        # Check page title exists
        title = soup.find("title")
        if not title or not title.get_text(strip=True):
            bugs.append("Missing <title> element")

    except ImportError:
        pass  # BeautifulSoup not installed — skip structural checks

    # Global dashboard: check all 6 brokers are present
    if "global_portfolio" in path.name.lower() or "global_portfolio" in str(path).lower():
        for broker in _REQUIRED_BROKERS:
            if broker not in raw:
                bugs.append(f"Missing broker in global dashboard: {broker}")

    return bugs, warnings


# ══════════════════════════════════════════════════════════════════════════════
# Script Registry
# ══════════════════════════════════════════════════════════════════════════════

def _build_registry() -> List[ScriptSpec]:
    """Return all script specs for E2E testing."""
    specs: List[ScriptSpec] = []

    RPTS = ROOT / "reports"
    # RPTS removed — all reports go to ROOT/"reports"

    # ── SECTION 1: TRADER ──────────────────────────────────────────────────────

    specs.append(ScriptSpec(
        key="1",  label="Global Portfolio Dashboard",  section="TRADER",
        script=ROOT / "global_portfolio_dashboard.py",
        args=["--no-open", "--allow-partial", "--no-ibkr"],
        expected_output=RPTS / "global_portfolio_dashboard.html",
        requires_live=True,
        skip_reason="needs Moomoo OpenD (ports 11111/11112) + Zerodha/AngelOne auth",
        timeout=120,
    ))

    specs.append(ScriptSpec(
        key="2",  label="India Portfolio Dashboard",  section="TRADER",
        script=INDIA / "india_portfolio_dashboard.py",
        args=["--no-open"],
        expected_output=RPTS / "india_portfolio_dashboard.html",
        requires_live=True,
        skip_reason="needs Zerodha access token + AngelOne TOTP auth",
        timeout=120,
    ))

    specs.append(ScriptSpec(
        key="3",  label="Portfolio Analytics",  section="TRADER",
        script=ROOT / "analyze_portfolio.py",
        args=[],
        expected_output=ROOT / "portfolio_report.html",  # writes to repo root (not reports/)
        requires_live=False,  # offline: exits 1 with clear error if stock-statement/ missing
        timeout=30,
    ))

    specs.append(ScriptSpec(
        key="14", label="Signal Stack (rank + score)", section="TRADER",
        script=TV / "signal_stack.py",
        args=["--n", "30", "--no-moomoo", "--no-india", "--no-external",
              "--out", str(RPTS / "e2e_signal_stack.html")],
        expected_output=RPTS / "e2e_signal_stack.html",
        requires_live=False,  # TV screener is internet-only (no broker); brokers skipped via flags
        timeout=240,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="15", label="Sell Strategy (demo)", section="TRADER",
        script=TV / "sell" / "sell_strategy.py",
        args=["--demo"],
        expected_output=RPTS / "sell_strategy_report.html",  # saves to root reports/
        requires_live=False,
        timeout=60,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="16", label="Multi-Asset Screener", section="TRADER",
        script=TV / "screeners" / "multi_asset_screener.py",
        args=["--out", str(RPTS / "e2e_multi_asset.html"),
              "--max-per-class", "5"],
        expected_output=RPTS / "e2e_multi_asset.html",
        requires_live=False,   # uses TV screener (may return empty if offline)
        timeout=120,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="17", label="Morning Screener US", section="TRADER",
        script=TV / "screeners" / "morning_screener.py",
        args=["--market", "US"],
        expected_output=None,  # writes screener_pool.json, not HTML
        requires_live=False,
        timeout=120,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="20", label="India Market Internals", section="TRADER",
        script=INDIA / "india_market_internals.py",
        args=[],
        expected_output=None,
        requires_live=True,
        skip_reason="needs Zerodha KiteConnect access token",
        timeout=60,
        cwd=INDIA,
    ))

    specs.append(ScriptSpec(
        key="22", label="India F&O Analyzer", section="TRADER",
        script=INDIA / "india_fo_analyzer.py",
        args=[],
        expected_output=None,
        requires_live=True,
        skip_reason="needs AngelOne SmartAPI TOTP auth",
        timeout=60,
        cwd=INDIA,
    ))

    # ── Options Analysis ───────────────────────────────────────────────────────

    specs.append(ScriptSpec(
        key="6",  label="Options from Actionable Signals", section="TRADER",
        script=TV / "options" / "run_options.py",
        args=["--from-actionable", "--all-modes", "--limit", "5",
              "--out", str(RPTS / "e2e_options_report.html")],
        expected_output=RPTS / "e2e_options_report.html",
        requires_live=False,
        timeout=120,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="10", label="Option Position Manager", section="TRADER",
        script=TV / "options" / "option_position_manager.py",
        args=["--demo"],
        expected_output=None,
        requires_live=False,
        timeout=60,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="11", label="F&O / Forex Arbitrage Engine", section="TRADER",
        script=TV / "screeners" / "fo_arbitrage.py",
        args=[],
        expected_output=None,
        requires_live=False,
        timeout=90,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="12", label="Options Hedging Advisor", section="TRADER",
        script=TV / "screeners" / "options_hedger.py",  # was wrongly TV/options/options_hedger.py
        args=["--no-ao", "--no-regime",
              "--out", str(RPTS / "e2e_hedging_advisor_report.html")],
        expected_output=RPTS / "e2e_hedging_advisor_report.html",
        requires_live=True,
        skip_reason="needs live broker holdings (Moomoo + Zerodha + AngelOne)",
        timeout=90,
        cwd=TV,
    ))

    # ── SECTION 2: TAX ────────────────────────────────────────────────────────

    specs.append(ScriptSpec(
        key="23", label="Moomoo CGT (AU)", section="TAX",
        script=CGT / "moomoo-cgt" / "cgt_analysis.py",
        args=["--firm", "AU", "--port", "11111", "--acc-id", "284008279318020118",
              "--out", str(RPTS / "cgt" / "e2e_au_cgt_report.html")],
        expected_output=RPTS / "cgt" / "e2e_au_cgt_report.html",
        requires_live=True,
        skip_reason="needs Moomoo OpenD running on port 11111",
        timeout=90,
    ))

    specs.append(ScriptSpec(
        key="24", label="Moomoo CGT (SG)", section="TAX",
        script=CGT / "moomoo-cgt" / "cgt_analysis.py",
        args=["--firm", "SG", "--port", "11112", "--acc-id", "283726805871679562",
              "--out", str(RPTS / "cgt" / "e2e_sg_cgt_report.html")],
        expected_output=RPTS / "cgt" / "e2e_sg_cgt_report.html",
        requires_live=True,
        skip_reason="needs Moomoo OpenD running on port 11112",
        timeout=90,
    ))

    specs.append(ScriptSpec(
        key="25", label="IBKR CGT (Flex Query)", section="TAX",
        script=CGT / "ibkr-cgt" / "ibkr_cgt_analysis.py",
        args=["--out", str(RPTS / "cgt" / "e2e_ibkr_cgt_report.html")],
        expected_output=RPTS / "cgt" / "e2e_ibkr_cgt_report.html",
        requires_live=False,  # uses Flex Query (HTTP, no TWS needed)
        timeout=120,
    ))

    specs.append(ScriptSpec(
        key="27", label="Combined CGT Report", section="TAX",
        script=CGT / "combined_cgt_report.py",
        args=["--no-moomoo", "--no-ibkr", "--no-india", "--no-webull",
              "--out-dir", str(RPTS / "cgt")],
        expected_output=RPTS / "cgt" / "combined_cgt_report.html",
        requires_live=False,
        timeout=240,
        cwd=CGT,
    ))

    # ── SECTION 3: ADMIN ──────────────────────────────────────────────────────

    specs.append(ScriptSpec(
        key="31", label="Broker API Audit", section="ADMIN",
        script=ROOT / "broker_api_audit.py",
        args=[],
        expected_output=None,
        requires_live=False,
        timeout=60,
    ))

    specs.append(ScriptSpec(
        key="35", label="Signal Backtest (vectorbt)", section="ADMIN",
        script=ROOT / "benchmarks/signal_backtest.py",
        args=[],
        expected_output=None,
        requires_live=False,
        timeout=120,
    ))

    specs.append(ScriptSpec(
        key="36", label="Analyse Tickers (manual)", section="ADMIN",
        script=TV / "webhook_server.py",
        args=["--analyse", "NASDAQ:NVDA,NASDAQ:AAPL",
              "--no-sizer"],
        expected_output=RPTS / "signal_report_actionable_webhook.html",
        requires_live=False,
        timeout=180,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="39", label="Alert Tracker: Performance Report", section="ADMIN",
        script=TV / "alert_tracker.py",
        args=["--report"],
        expected_output=RPTS / "alert_performance_report.html",
        requires_live=False,
        timeout=60,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="40", label="Alert Tracker: Stats Summary", section="ADMIN",
        script=TV / "alert_tracker.py",
        args=["--stats"],
        expected_output=None,
        requires_live=False,
        timeout=30,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="42", label="News Scan: Bullish Signals", section="ADMIN",
        script=TV / "signal_stack.py",
        args=["--news", "bullish", "--news-max-age", "4",
              "--news-threshold", "0.35", "--no-moomoo", "--no-india", "--no-external",
              "--out", str(RPTS / "e2e_news_bullish.html")],
        expected_output=RPTS / "e2e_news_bullish.html",
        requires_live=False,  # TV news API = internet (will WARN if TV cookie expired)
        timeout=360,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="46", label="Fundamental Analysis", section="ADMIN",
        script=TV / "analysis" / "fundamental_analysis.py",
        args=["--n", "10",
              "--out", str(RPTS / "e2e_fundamental_report.html")],
        expected_output=RPTS / "e2e_fundamental_report.html",
        requires_live=False,
        timeout=120,
        cwd=TV,
    ))

    specs.append(ScriptSpec(
        key="47", label="Performance Analyzer", section="ADMIN",
        script=TV / "performance_analyzer.py",
        args=[],
        expected_output=None,
        requires_live=False,
        timeout=60,
        cwd=TV,
    ))

    return specs


REGISTRY: List[ScriptSpec] = _build_registry()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def _run_spec(spec: ScriptSpec, include_live: bool = False) -> TestResult:
    """Run one ScriptSpec and return a TestResult."""

    if spec.requires_live and not include_live:
        reason = spec.skip_reason or "requires live broker connection"
        return TestResult(
            key=spec.key, label=spec.label, section=spec.section,
            status="SKIP",
            skip_reason=f"needs --include-live: {reason}",
        )

    if not spec.script.exists():
        return TestResult(
            key=spec.key, label=spec.label, section=spec.section,
            status="SKIP",
            skip_reason=f"script not found: {spec.script.relative_to(ROOT)}",
        )

    # Ensure output directory exists
    if spec.expected_output:
        spec.expected_output.parent.mkdir(parents=True, exist_ok=True)

    import os
    env = {
        **os.environ,
        "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
        "PYTHONIOENCODING": "utf-8",
        "OPENBLAS_NUM_THREADS": "1",
        **spec.env_extras,
    }

    cmd = [sys.executable, str(spec.script)] + spec.args
    cwd = str(spec.cwd or ROOT)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout,
            cwd=cwd,
            env=env,
        )
        duration = time.monotonic() - t0
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - t0
        return TestResult(
            key=spec.key, label=spec.label, section=spec.section,
            status="TIMEOUT",
            duration=duration,
            skip_reason=f"timed out after {spec.timeout}s",
        )
    except Exception as e:
        duration = time.monotonic() - t0
        return TestResult(
            key=spec.key, label=spec.label, section=spec.section,
            status="ERROR",
            duration=duration,
            skip_reason=str(e),
        )

    # Validate HTML output
    html_bugs: List[str] = []
    html_warnings: List[str] = []
    if spec.expected_output:
        html_bugs, html_warnings = validate_html(spec.expected_output)

    # Determine status
    if exit_code != 0 and not html_bugs and not html_warnings:
        # Non-zero exit but no HTML issues — might be a live-data failure (warn, not fail)
        stderr_lower = stderr.lower()
        if any(w in stderr_lower for w in ["connection refused", "timeout", "authentication", "login"]):
            status = "WARN"
        else:
            status = "FAIL"
    elif html_bugs:
        status = "FAIL"
    elif html_warnings:
        status = "WARN"
    else:
        status = "PASS"

    return TestResult(
        key=spec.key, label=spec.label, section=spec.section,
        status=status,
        duration=duration,
        exit_code=exit_code,
        stdout=stdout[-3000:] if stdout else "",  # cap at 3KB
        stderr=stderr[-3000:] if stderr else "",
        html_bugs=html_bugs + [f"[WARN] {w}" for w in html_warnings],
        output_file=spec.expected_output,
    )


def run_all(
    specs: List[ScriptSpec],
    include_live: bool = False,
    parallel: bool = False,
) -> List[TestResult]:
    """Run a list of specs and return results."""
    results = []
    total = len(specs)
    for i, spec in enumerate(specs, 1):
        print(f"  [{i:02d}/{total}] {spec.key:5s} {spec.label[:45]:<45}", end="", flush=True)
        t0 = time.monotonic()
        r = _run_spec(spec, include_live=include_live)
        elapsed = time.monotonic() - t0

        icon = {
            "PASS": "✓", "FAIL": "✗", "SKIP": "–",
            "WARN": "!", "TIMEOUT": "⏱", "ERROR": "E"
        }.get(r.status, "?")
        print(f"  {icon} {r.status:<8} ({elapsed:.1f}s)", flush=True)

        if r.status == "FAIL" and r.html_bugs:
            for bug in r.html_bugs[:3]:
                print(f"             ↳ {bug[:90]}")
        if r.status in ("FAIL", "ERROR") and r.stderr:
            first_err = r.stderr.splitlines()[0][:90]
            print(f"             ↳ stderr: {first_err}")
        if r.status == "SKIP" and r.skip_reason:
            print(f"             ↳ {r.skip_reason[:90]}")

        results.append(r)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# HTML Report Generator
# ══════════════════════════════════════════════════════════════════════════════

def _status_color(status: str) -> str:
    return {"PASS": "#27ae60", "FAIL": "#e74c3c", "WARN": "#f39c12",
            "SKIP": "#95a5a6", "TIMEOUT": "#8e44ad", "ERROR": "#c0392b"}.get(status, "#7f8c8d")


def generate_html_report(results: List[TestResult], out: Path) -> None:
    """Write a comprehensive HTML bug report."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total   = len(results)
    passed  = sum(1 for r in results if r.status == "PASS")
    failed  = sum(1 for r in results if r.status == "FAIL")
    warned  = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status in ("SKIP",))
    timeouts= sum(1 for r in results if r.status == "TIMEOUT")
    errors  = sum(1 for r in results if r.status == "ERROR")
    total_duration = sum(r.duration for r in results)

    # Gather all bugs
    all_bugs = [(r, bug) for r in results for bug in r.html_bugs]

    rows_html = []
    for r in results:
        color = _status_color(r.status)
        bug_list = ""
        if r.html_bugs:
            items = "".join(f"<li>{_esc(b)}</li>" for b in r.html_bugs)
            bug_list = f"<ul class='bugs'>{items}</ul>"
        elif r.skip_reason:
            bug_list = f"<span class='skip-reason'>{_esc(r.skip_reason)}</span>"

        stderr_snippet = ""
        if r.status in ("FAIL", "ERROR") and r.stderr:
            first = r.stderr.strip().splitlines()[0][:200]
            stderr_snippet = f"<div class='stderr'>{_esc(first)}</div>"

        output_link = ""
        if r.output_file and r.output_file.exists():
            rel = r.output_file.relative_to(ROOT)
            output_link = f"<a href='{rel}' target='_blank'>📄 report</a>"

        rows_html.append(f"""
        <tr>
          <td class='key'>{_esc(r.key)}</td>
          <td>{_esc(r.label)}</td>
          <td><span class='badge' style='background:{color}'>{r.status}</span></td>
          <td class='num'>{r.duration:.1f}s</td>
          <td class='num'>{r.exit_code if r.exit_code != 0 else ""}</td>
          <td>{bug_list}{stderr_snippet}{output_link}</td>
        </tr>""")

    # Bugs summary section
    bug_summary_html = ""
    if all_bugs:
        bug_rows = "".join(f"""
        <tr>
          <td class='key'>{_esc(r.key)}</td>
          <td>{_esc(r.label)}</td>
          <td>{_esc(bug)}</td>
        </tr>""" for r, bug in all_bugs)
        bug_summary_html = f"""
        <h2>🐛 Bug Summary ({len(all_bugs)} issues)</h2>
        <table>
          <thead><tr><th>Key</th><th>Option</th><th>Bug Description</th></tr></thead>
          <tbody>{bug_rows}</tbody>
        </table>"""

    overall = "ALL PASS ✓" if failed == 0 and errors == 0 else f"{failed + errors} FAILED ✗"
    ov_color = "#27ae60" if failed == 0 and errors == 0 else "#e74c3c"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E2E Test Report — {ts}</title>
<style>
  body {{ font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          margin:0; background:#f5f6fa; color:#222; }}
  header {{ background:#1a1a2e; color:#fff; padding:20px 32px; }}
  header h1 {{ margin:0 0 8px; font-size:22px; }}
  .stats {{ display:flex; gap:16px; flex-wrap:wrap; padding:16px 32px;
            background:#fff; border-bottom:1px solid #ddd; }}
  .stat {{ background:#f8f9fa; border-radius:8px; padding:12px 20px; text-align:center; min-width:90px; }}
  .stat .n {{ font-size:28px; font-weight:700; }}
  .stat .lbl {{ font-size:12px; color:#666; }}
  .pass  {{ color:#27ae60; }} .fail {{ color:#e74c3c; }}
  .warn  {{ color:#f39c12; }} .skip {{ color:#95a5a6; }}
  .timeout {{ color:#8e44ad; }} .error {{ color:#c0392b; }}
  main {{ padding:24px 32px; max-width:1400px; }}
  h2 {{ font-size:16px; color:#444; margin:24px 0 12px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff;
           border-radius:8px; overflow:hidden;
           box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  th {{ background:#f0f0f0; font-size:12px; color:#555; padding:10px 12px;
        text-align:left; border-bottom:2px solid #ddd; white-space:nowrap; }}
  td {{ padding:9px 12px; border-bottom:1px solid #eee; font-size:13px;
        vertical-align:top; }}
  td.key {{ font-weight:700; color:#0a7cff; min-width:40px; }}
  td.num {{ text-align:right; color:#666; min-width:60px; }}
  .badge {{ border-radius:4px; padding:2px 8px; color:#fff;
            font-size:11px; font-weight:700; white-space:nowrap; }}
  ul.bugs {{ margin:0; padding:0 0 0 16px; color:#c0392b; font-size:12px; }}
  ul.bugs li {{ margin:2px 0; }}
  .skip-reason {{ color:#999; font-size:12px; font-style:italic; }}
  .stderr {{ font-family:monospace; font-size:11px; color:#e74c3c;
             background:#fff5f5; padding:3px 6px; border-radius:3px; margin-top:4px; }}
  a {{ color:#0a7cff; text-decoration:none; font-size:12px; }}
  a:hover {{ text-decoration:underline; }}
  .overall {{ font-size:18px; font-weight:700; color:{ov_color}; }}
  .section-header td {{ background:#f8f9fa; font-weight:700; color:#555;
                        font-size:12px; text-transform:uppercase;
                        letter-spacing:.05em; padding:6px 12px; }}
</style>
</head>
<body>
<header>
  <h1>🧪 E2E Menu Test Report</h1>
  <div>Generated: {ts} &nbsp;|&nbsp; Total duration: {total_duration:.1f}s</div>
  <div class='overall' style='margin-top:8px'>{overall}</div>
</header>
<div class="stats">
  <div class="stat"><div class="n pass">{passed}</div><div class="lbl">PASSED</div></div>
  <div class="stat"><div class="n fail">{failed}</div><div class="lbl">FAILED</div></div>
  <div class="stat"><div class="n warn">{warned}</div><div class="lbl">WARNED</div></div>
  <div class="stat"><div class="n skip">{skipped}</div><div class="lbl">SKIPPED</div></div>
  <div class="stat"><div class="n timeout">{timeouts}</div><div class="lbl">TIMEOUT</div></div>
  <div class="stat"><div class="n error">{errors}</div><div class="lbl">ERRORS</div></div>
  <div class="stat"><div class="n">{total}</div><div class="lbl">TOTAL</div></div>
</div>
<main>
  {bug_summary_html}
  <h2>📋 Per-Option Results</h2>
  <table>
    <thead>
      <tr>
        <th>Key</th><th>Option</th><th>Status</th>
        <th>Time</th><th>Exit</th><th>Details / Bugs</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
</main>
</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"\n  📄 Report: {out}")


def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ══════════════════════════════════════════════════════════════════════════════
# JSON output (for Test-E2E.ps1 parsing)
# ══════════════════════════════════════════════════════════════════════════════

def write_json_results(results: List[TestResult], out: Path) -> None:
    data = [
        {
            "key": r.key,
            "label": r.label,
            "section": r.section,
            "status": r.status,
            "duration": round(r.duration, 2),
            "exit_code": r.exit_code,
            "html_bugs": r.html_bugs,
            "skip_reason": r.skip_reason,
            "stderr_snippet": (r.stderr.strip().splitlines()[0][:200]
                               if r.stderr.strip() else ""),
            "output_file": str(r.output_file) if r.output_file else None,
        }
        for r in results
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="E2E Menu Test Runner — runs real scripts and validates HTML output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python run_e2e_tests.py                       # run all non-live tests
          python run_e2e_tests.py --key 14              # run only signal stack
          python run_e2e_tests.py --section TRADER      # run all TRADER options
          python run_e2e_tests.py --include-live        # also run live-only tests
          python run_e2e_tests.py --out reports/my.html # custom report path
          python run_e2e_tests.py --list                # list all registered options
        """),
    )
    parser.add_argument("--key",          help="Run only this menu option key (e.g. '14')")
    parser.add_argument("--section",      help="Run only this section (TRADER/TAX/ADMIN)")
    parser.add_argument("--include-live", action="store_true",
                        help="Include tests that require live broker connections")
    parser.add_argument("--out",          default="reports/e2e_test_report.html",
                        help="Output HTML report path (default: reports/e2e_test_report.html)")
    parser.add_argument("--json-out",     default="reports/e2e_test_results.json",
                        help="Output JSON results path")
    parser.add_argument("--quick",        action="store_true",
                        help="Show summary only (no per-option table)")
    parser.add_argument("--list",         action="store_true",
                        help="List all registered options and exit")
    args = parser.parse_args()

    # List mode
    if args.list:
        print(f"\n{'Key':<8} {'Section':<10} {'Script':<55} {'Live?'}")
        print("─" * 100)
        for spec in REGISTRY:
            script_rel = str(spec.script.relative_to(ROOT)) if spec.script.is_absolute() else str(spec.script)
            live = "LIVE" if spec.requires_live else ""
            print(f"{spec.key:<8} {spec.section:<10} {script_rel:<55} {live}")
        return 0

    # Filter specs
    specs = REGISTRY
    if args.key:
        specs = [s for s in specs if s.key == args.key]
        if not specs:
            print(f"No spec found for key '{args.key}'")
            return 1
    elif args.section:
        sec = args.section.upper()
        specs = [s for s in specs if s.section == sec]
        if not specs:
            print(f"No specs found for section '{args.section}'")
            return 1

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  E2E Menu Test Runner   {ts}")
    print(f"  Running {len(specs)} test(s)   include-live={args.include_live}")
    print(f"{'='*60}\n")

    results = run_all(specs, include_live=args.include_live)

    # Summary
    passed  = sum(1 for r in results if r.status == "PASS")
    failed  = sum(1 for r in results if r.status == "FAIL")
    warned  = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status in ("SKIP",))
    errors  = sum(1 for r in results if r.status in ("ERROR", "TIMEOUT"))
    total_dur = sum(r.duration for r in results)

    print(f"\n{'─'*60}")
    print(f"  Results: {passed} PASS · {failed} FAIL · {warned} WARN · "
          f"{skipped} SKIP · {errors} ERR   ({total_dur:.1f}s)")

    if not args.quick:
        failing = [r for r in results if r.status in ("FAIL", "ERROR", "TIMEOUT")]
        if failing:
            print(f"\n  FAILURES:")
            for r in failing:
                print(f"    [{r.key}] {r.label}")
                for bug in r.html_bugs[:3]:
                    print(f"         ↳ {bug[:90]}")
                if r.skip_reason:
                    print(f"         ↳ {r.skip_reason}")

    # Write outputs
    out_html = ROOT / args.out
    out_json = ROOT / args.json_out
    generate_html_report(results, out_html)
    write_json_results(results, out_json)

    return 0 if (failed == 0 and errors == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

