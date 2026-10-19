#!/usr/bin/env python3
from __future__ import annotations
"""
Stocks Trading System — Main Menu
==================================
Run this file to access all available functionality.
"""

import os
import sys
import json
import socket
import subprocess
import threading
import datetime as _dt
from pathlib import Path
from datetime import date

# ── Windows: force UTF-8 console so emoji/rupee/arrows render correctly ────────
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths (centralised in path_config.py) ──────────────────────────────────────
from path_config import ROOT, TV, CGT, INDIA, REPORTS, LOGS, ensure_paths

# ── Structured logging ────────────────────────────────────────────────────────
from log_config import get_logger, new_run_id
_log = get_logger("main")

# ── Per-option log file (set in main() before each handler call) ───────────────
_current_log_path: Path | None = None

# ── Environment (required for moomoo protobuf + numpy/OpenBLAS stability) ──────
BASE_ENV = {
    **os.environ,
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",   # prevent "Memory allocation still failed" on numpy load
}

from html_utils import BOLD, DIM, CYAN, GREEN, YELLOW, BLUE, MAGENTA, RED, RESET  # B-5

_PROJECT_PYTHON = ROOT / ".venv" / "bin" / "python"


def _python_executable() -> str:
    return str(_PROJECT_PYTHON) if _PROJECT_PYTHON.exists() else sys.executable

# ── Helpers ────────────────────────────────────────────────────────────────────
def header(text: str, color: str = CYAN) -> str:
    bar = "─" * (len(text) + 4)
    return f"\n{color}{BOLD}┌{bar}┐\n│  {text}  │\n└{bar}┘{RESET}"

def _tee_stream(stream, log_fh, console_fh) -> None:
    """Read lines from stream and write to both log file and console."""
    for line in iter(stream.readline, ""):
        console_fh.write(line)
        console_fh.flush()
        log_fh.write(line)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> int:
    """Run a command, wait for it to finish, and return its exit code."""
    global _current_log_path
    cmd_str = " ".join(str(c) for c in cmd)
    print(f"\n{DIM}$ {cmd_str}{RESET}\n")
    try:
        if _current_log_path:
            LOGS.mkdir(exist_ok=True)
            ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            process = subprocess.Popen(
                cmd,
                env=env if env is not None else BASE_ENV,
                cwd=str(cwd or ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with open(_current_log_path, "a", encoding="utf-8", errors="replace") as lf:
                lf.write(f"\n[{ts}] CMD: {cmd_str}\n")
                lf.write("─" * 80 + "\n")
                t1 = threading.Thread(target=_tee_stream, args=(process.stdout, lf, sys.stdout))
                t2 = threading.Thread(target=_tee_stream, args=(process.stderr, lf, sys.stderr))
                t1.start(); t2.start()
                t1.join(); t2.join()
                rc = process.wait()
                lf.write(f"\n[exit code: {rc}]\n")
                return rc
        else:
            result = subprocess.run(
                cmd,
                env=env if env is not None else BASE_ENV,
                cwd=str(cwd or ROOT),
                check=False,
            )
            return result.returncode
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[interrupted]{RESET}")
        return 130
    except OSError as exc:
        print(f"\n{RED}[launch failed: {exc}]{RESET}")
        return 127

def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{YELLOW}{text}{suffix}: {RESET}").strip()
        result = value or default
        _log.debug("user_prompt", question=text, answer=result)
        return result
    except KeyboardInterrupt:
        return default

def pause() -> None:
    try:
        input(f"\n{DIM}Press Enter to return to the menu…{RESET}")
    except KeyboardInterrupt:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════════════════════════

def run_moomoo_au_cgt() -> None:
    print(header("Moomoo CGT  — AU account  (OpenD port 11111)", GREEN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • CGT SNAPSHOT block: matched sell events, warnings, open lots
  • Net taxable gains/losses by financial year and currency
  • Open FIFO buy lots still held
  • Full HTML report at {REPORTS / 'cgt' / 'au_cgt_report.html'}

{BOLD}Filters:{RESET}  Only SELL events (FY-based CGT). Dividends excluded.
{BOLD}Requires:{RESET} OpenD running on port 11111
""")
    from moomoo_config import AU_ACCOUNT_ID
    run([
        sys.executable, str(CGT / "moomoo-cgt" / "cgt_analysis.py"),
        "--firm", "AU",
        "--port", "11111",
        "--acc-id", str(AU_ACCOUNT_ID),
        "--out", str(REPORTS / "cgt" / "au_cgt_report.html"),
    ])


def run_moomoo_sg_cgt() -> None:
    print(header("Moomoo CGT  \u2014 SG account  (FUTUSG \u00b7 OpenD port 11112)", GREEN))
    from moomoo_config import SG_ACCOUNT_ID, is_opend_running, OPEND_SG_PORT

    # Pre-flight: verify OpenD is reachable
    if not is_opend_running(OPEND_SG_PORT):
        print(f"{RED}\u26a0  OpenD is NOT running on port 11112.{RESET}")
        print(f"{DIM}  Start it with:  .\\start_opend.ps1   then re-run this option.{RESET}")
        return

    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 CGT SNAPSHOT: matched sell events with warnings by market (SGX/TSE/NYSE/HKEX)
  \u2022 Net taxable gains/losses by FY in native currencies (SGD, JPY, USD, HKD)
  \u2022 Open FIFO lots still held (cost base retained)
  \u2022 Full HTML report at {CGT / 'moomoo-cgt' / 'sg_cgt_report.html'}

{BOLD}Account:{RESET}  Moomoo SG  (SecurityFirm.FUTUSG)  \u00b7  ID {SG_ACCOUNT_ID}
{BOLD}Markets:{RESET}  SGX \u2192 SGD  |  TSE \u2192 JPY  |  NYSE/NASDAQ \u2192 USD  |  HKEX \u2192 HKD
{BOLD}Output:{RESET}   reports/cgt/sg_cgt_report.html

{YELLOW}Note:{RESET} Foreign-currency gains (JPY, USD, HKD, SGD) are reported in their
  native currency. For Australian CGT, convert each amount to AUD using
  the RBA published exchange rate on each trade date.
  DLC positions (SG.NVIW, SG.ZOGW, etc.) report last_price=0 when market
  is closed \u2014 unrealised P&L uses the API fallback value in that case.
""")
    run([
        sys.executable, str(CGT / "moomoo-cgt" / "cgt_analysis.py"),
        "--firm", "SG",
        "--port", "11112",
        "--acc-id", str(SG_ACCOUNT_ID),
        "--out", str(REPORTS / "cgt" / "sg_cgt_report.html"),
    ])

def run_ibkr_cgt() -> None:
    print(header("IBKR CGT — Flex Query (MyFlexQuery)", GREEN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Statement date range and number of matched sell events
  • Net taxable gains/losses by financial year (AUD, USD)
  • Coverage warnings if prior-period or transferred-in positions exist
  • Full HTML report at {REPORTS / 'cgt' / 'ibkr_cgt_report.html'}

{BOLD}Data source:{RESET}  Flex Query auto-fetch (MyFlexQuery, id=1462304)
  Pass --statement <csv> to use a manual Activity Statement instead.
""")
    _ibkr_out = str(REPORTS / "cgt" / "ibkr_cgt_report.html")
    choice = prompt("Use Flex Query defaults? (y) or manual CSV? (csv)", "y").lower()
    if choice == "csv":
        csv_path = prompt("Path to Activity Statement CSV")
        if not csv_path:
            print(f"{RED}CSV path is required.{RESET}")
            return
        run([sys.executable, str(CGT / "ibkr-cgt" / "ibkr_cgt_analysis.py"), "--statement", csv_path, "--out", _ibkr_out])
    else:
        run([sys.executable, str(CGT / "ibkr-cgt" / "ibkr_cgt_analysis.py"), "--out", _ibkr_out])

def run_webull_cgt() -> None:
    print(header("Webull CGT — AU PDF Statements or CSV", GREEN))
    print(f"""
{BOLD}WEBULL CGT ANALYSIS{RESET}
  Analyses Webull AU trades for Australian Capital Gains Tax using FIFO matching.
  Supports two input sources:

  {CYAN}1{RESET}) PDF Statements {BOLD}(recommended){RESET}
     Auto-scans webull-statement/ folder for PDF files
     Includes: EOFY, Monthly, Trade Confirmation PDFs
     Shows live prices via yfinance, adjusted holdings, full CGT breakdown

  {CYAN}2{RESET}) CSV Export
     Webull app → Orders → History → Export → CSV
     Requires manual export from Webull desktop/web

{BOLD}Output:{RESET}  reports/cgt/webull_cgt_report.html
""")
    _wb_out = str(REPORTS / "cgt" / "webull_cgt_report.html")
    choice = prompt("Enter 1 (PDF) or 2 (CSV)", "1").strip()
    if choice == "2":
        csv_path = prompt("Path to Webull order-history CSV (or press Enter to cancel)").strip()
        if not csv_path:
            print(f"{YELLOW}No file provided — cancelled.{RESET}")
            return
        p = Path(csv_path)
        if not p.exists():
            print(f"{RED}File not found: {csv_path}{RESET}")
            return
        run([sys.executable,
             str(CGT / "webull-cgt" / "webull_cgt_analysis.py"),
             csv_path, "--out", _wb_out])
    else:
        # Default: PDF statements
        pdf_dir = str(ROOT / "webull-statement")
        if not Path(pdf_dir).is_dir():
            print(f"{RED}PDF folder not found: {pdf_dir}{RESET}")
            print(f"{DIM}Place Webull AU PDF statements in webull-statement/ and try again.{RESET}")
            return
        n_pdfs = len(list(Path(pdf_dir).glob("*.pdf")))
        print(f"  Found {GREEN}{n_pdfs} PDF files{RESET} in webull-statement/")
        run([sys.executable,
             str(CGT / "webull-cgt" / "webull_cgt_analysis.py"),
             "--pdf-dir", pdf_dir, "--out", _wb_out])

def run_combined_cgt() -> None:
    print(header("Combined CGT Report  (IBKR + Moomoo + India)", GREEN))
    # Live broker coverage check
    from moomoo_config import is_opend_running
    au_ok  = is_opend_running(11111)
    sg_ok  = is_opend_running(11112)
    z_at   = bool(os.environ.get("ZERODHA_ACCESS_TOKEN", "") or _load_cached_token())

    def _bstat(ok: bool, label: str) -> str:
        return f"{GREEN}● {label}{RESET}" if ok else f"{YELLOW}! {label} — skipped if not available{RESET}"

    print(f"""
{BOLD}BROKER COVERAGE MATRIX{RESET}
  {_bstat(True,    "IBKR       (Flex Query auto-fetch or manual CSV)")}
  {_bstat(au_ok,   "Moomoo AU  (OpenD port 11111)")}
  {_bstat(sg_ok,   "Moomoo SG  (OpenD port 11112)")}
  {_bstat(z_at,    "Zerodha    (ZERODHA_ACCESS_TOKEN)")}
  {GREEN}● Angel One  (auto-auth via TOTP){RESET}

{BOLD}WHAT YOU'LL SEE:{RESET}
  • Sell events matched per broker with FY and currency breakdown
  • Net taxable total by FY (AUD, USD, INR, SGD, JPY, HKD)
  • Open FIFO lots (remaining cost basis)
  • Coverage warnings for prior-period / transferred positions
  • HTML reports per broker + merged combined_cgt_report.html
  • Output directory: reports/cgt/

{BOLD}Configuration:{RESET}  Leave blank to use default / skip.
  OpenD must be running for Moomoo. ZERODHA_ACCESS_TOKEN for Zerodha.
""")
    ibkr_csv    = prompt("IBKR Activity Statement CSV  (blank = use Flex Query or skip IBKR)")
    no_moomoo   = prompt("Skip Moomoo? (y/n)", "n").lower() == "y"
    no_ibkr_val = prompt("Skip IBKR? (y/n)", "n").lower() == "y"
    no_india    = prompt("Skip India (Zerodha + Angel One)? (y/n)", "n").lower() == "y"

    script = str(CGT / "combined_cgt_report.py")
    cmd    = [sys.executable, script]

    if ibkr_csv:
        cmd += ["--ibkr-statement", ibkr_csv]
    if no_moomoo:
        cmd.append("--no-moomoo")
    if no_ibkr_val:
        cmd.append("--no-ibkr")
    if no_india:
        cmd.append("--no-india")
    cmd += ["--out-dir", str(REPORTS / "cgt")]

    # Pass Zerodha token to subprocess when India CGT is requested
    cgt_env = _india_env(require_zerodha=False) if not no_india else BASE_ENV.copy()
    run(cmd, cwd=CGT, env=cgt_env)


def run_tax_harvest_advisor() -> None:
    """Idea 6: Proactive Tax-Loss Harvesting Advisor — AU FY deadline aware."""
    print(header("Tax-Loss Harvesting Advisor  (AU / India / SG)", GREEN))
    from datetime import date as _date
    fy_end   = _date(2026, 6, 30)
    days_left = (fy_end - _date.today()).days
    urgency = f"{RED}!! URGENT{RESET}" if days_left < 60 else f"{YELLOW}! Note{RESET}"
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Tax-loss harvest candidates ranked by potential tax saving (AUD)
  • CGT discount cliff: positions held 340-365 days (50% discount in 9 days?)
  • Short-term gain positions: sell before FY end to defer tax to next year
  • Estimates tax liability by country: AU / India / Singapore

{urgency}: {days_left} days to AU FY end ({fy_end}).  Act before June 30 to harvest losses.

{BOLD}Data sources:{RESET}  IBKR + Moomoo AU/SG + India (Zerodha + Angel One)
{BOLD}Output:{RESET}       reports/cgt/tax_harvest_report.html
""")
    no_moomoo = prompt("Skip Moomoo? (y/n)", "n").lower() == "y"
    no_india  = prompt("Skip India? (y/n)", "n").lower() == "y"
    out       = str(REPORTS / "cgt" / "tax_harvest_report.html")
    cmd       = [sys.executable, str(CGT / "tax_harvest_advisor.py"), "--out", out]
    if no_moomoo: cmd.append("--no-moomoo")
    if no_india:  cmd.append("--no-india")
    cgt_env = _india_env(require_zerodha=False) if not no_india else BASE_ENV.copy()
    run(cmd, cwd=CGT, env=cgt_env)


def run_global_dashboard() -> None:
    print(header("Global Portfolio Dashboard  (all 6 brokers)", CYAN))
    print(f"""
{BOLD}Aggregates live positions from:{RESET}
  • Moomoo AU  (OpenD port 11111 — skipped if not running)
  • Moomoo SG  (OpenD port 11112 — skipped if not running)
  • IBKR       (Flex Query XML, cached or fresh)
  • Webull AU  (PDF statements)
  • Zerodha    (needs ZERODHA_ACCESS_TOKEN)
  • Angel One  (auto-auth via TOTP)

{BOLD}HARD STOP:{RESET}  If Zerodha is unavailable, report is NOT generated.
            India equity (~₹2-3L / ~A$40K) would otherwise be silently excluded.
            Use --allow-partial to override (not recommended).

{BOLD}Output:{RESET}  reports/global_portfolio_dashboard.html  (saved to reports/)
         reports/portfolio_history.db            (historical snapshots)
         reports/archive/YYYY-MM-DD/             (archived copy)
{BOLD}Shows:{RESET}   Broker coverage matrix → portfolio value by currency →
           market exposure by region → top/worst performers
""")
    # Resolve Zerodha token and capture env dict — pass explicitly to subprocess
    # so it receives the token even though BASE_ENV is a startup snapshot.
    env = _india_env()
    cmd = [_python_executable(), str(ROOT / "global_portfolio_dashboard.py")]
    run(cmd, cwd=ROOT, env=env)


def run_portfolio() -> None:
    print(header("Portfolio Analyser  (Moomoo AU — stock-statement/)", BLUE))
    print(f"{YELLOW}⚠ DEPRECATED: Option 3 reads manual PDF/XLSX exports from Moomoo AU only.\n"
          f"  For a live multi-broker view across all 6 brokers, use option {BOLD}1{RESET}{YELLOW} — Global Portfolio Dashboard.{RESET}\n")
    stmt_dir = ROOT / "stock-statement"
    files    = list(stmt_dir.glob("*.[pPxX][dDlL][fFsS]*")) if stmt_dir.exists() else []
    file_status = f"{GREEN}{len(files)} statement file(s) found{RESET}" if files else f"{RED}No statement files found in stock-statement/{RESET}"
    print(f"""
{BOLD}REQUIRES:{RESET}  Moomoo AU statement PDFs or Excel files
  Place them in:  {stmt_dir}
  Files found:    {file_status}
""")
    if not files:
        print(f"{DIM}  Download statements from Moomoo: Account → Reports → Statement → Export PDF/XLS{RESET}\n")
    run([sys.executable, str(ROOT / "analyze_portfolio.py")])


def run_portfolio_analytics() -> None:
    print(header("Portfolio Analytics  (historical value chart)", CYAN))
    print(f"""
{BOLD}Reads from:{RESET}  reports/portfolio_history.db  (populated by Global Portfolio Dashboard)
{BOLD}Output:{RESET}      reports/portfolio_analytics.html  (saved to reports/)
{BOLD}Shows:{RESET}
  • Line chart of daily AUD portfolio value
  • Today vs yesterday delta (red banner on drop)
  • 30-day / all-time high & low
  • Broker breakdown for today
  • 30-day history table with Zerodha status
""")
    run([sys.executable, str(ROOT / "reports" / "portfolio_analytics.py")], cwd=ROOT)


def run_report_analytics() -> None:
    print(header("Report Analytics  (cross-report trends + anomaly detection)", CYAN))
    print(f"""
{BOLD}Reads from:{RESET}  reports/report_analytics.db  (populated by each report on run)
{BOLD}Output:{RESET}      reports/report_analytics.html  (saved to reports/)
{BOLD}Shows:{RESET}
  • Run history: which reports ran, when, success / failure
  • India portfolio trend chart  (total INR over time)
  • Alert tracker win-rate trend (closed % over time)
  • Sell strategy action distribution  (SELL/REDUCE/HOLD per run)
  • Hedging advisor urgency heatmap    (HIGH urgency count over time)
  • CGT exposure table                 (net taxable by FY/currency, latest run)
  • Anomaly alerts: big portfolio drops, Zerodha offline, sell spikes

{BOLD}Populated by running:{RESET}
  • India Portfolio Dashboard (option 2)
  • Alert Tracker (option 20)
  • Sell Strategy (option 25)
  • Options Hedging Advisor (option 14)
  • Combined CGT Report (option CGT)
""")
    run([sys.executable, str(ROOT / "reports" / "report_analytics.py")], cwd=ROOT)


def run_pnl_attribution() -> None:
    print(header("P&L Attribution Report — top winners/losers by sector & broker", CYAN))
    print(f"""
{BOLD}Reads from:{RESET}  .cache_global_positions.json  (populated by Global Portfolio Dashboard)
{BOLD}Output:{RESET}      reports/pnl_attribution.html  (saved to reports/)
{BOLD}Shows:{RESET}
  • Top 5 Winners and Top 5 Losers across all brokers
  • Broker P&L breakdown
  • Sector P&L breakdown
""")
    out = str(REPORTS / "pnl_attribution.html")
    cmd = [sys.executable, str(TV / "analysis" / "pnl_attribution.py"), "--out", out]
    run(cmd, cwd=ROOT)


def run_holdings_analysis() -> None:
    """Menu 4c — Holdings Snapshot Analysis: position history, cost basis, P&L trend."""
    print(header("Holdings Snapshot Analysis — position history + cost basis + P&L trend", CYAN))
    out = str(REPORTS / "holdings_analysis.html")
    cmd = [sys.executable, str(TV / "analysis" / "holdings_analyzer.py"), "--out", out]
    run(cmd, cwd=ROOT)


def run_portfolio_risk_optimizer() -> None:
    """Menu 4d — Portfolio Risk Optimizer: HRP allocation weights via skfolio."""
    print(header("Portfolio Risk Optimizer — HRP allocation weights (skfolio)", CYAN))
    out = str(REPORTS / "portfolio_risk_optimizer.html")
    cmd = [sys.executable, str(TV / "analysis" / "portfolio_risk_optimizer.py"), "--out", out]
    run(cmd, cwd=ROOT)


def run_portfolio_correlation() -> None:
    """Idea 5: Portfolio Correlation Matrix — concentration risk via pairwise returns."""
    print(header("Portfolio Correlation Matrix  — concentration risk detection", CYAN))
    print(f"""
{BOLD}Reads from:{RESET}  .cache_global_positions.json  (populated by Global Portfolio Dashboard)
{BOLD}Output:{RESET}      reports/portfolio_correlation.html

{BOLD}WHAT YOU'LL SEE:{RESET}
  • Pearson correlation heatmap of all US positions (90-day daily returns)
  • Pairs with correlation >0.7 flagged as hidden concentration risk
  • Color scale: red=very high | orange=high | yellow=moderate | blue=negative (diversifying)
  • Runs once per day (download ~1-2s per 10 tickers; cache recommended)

{BOLD}Note:{RESET}  Only US-listed tickers (no .AX/.NS suffix).
       Run 'Global Portfolio Dashboard' first if cache is stale.
""")
    days = prompt("Lookback days", "90").strip() or "90"
    out  = str(ROOT / "reports" / "portfolio_correlation.html")
    cmd  = [
        sys.executable,
        str(TV / "analysis" / "portfolio_correlation.py"),
        "--days", days,
        "--out", out,
    ]
    run(cmd, cwd=ROOT)




# ══════════════════════════════════════════════════════════════════════════════
# India Broker Helpers
# ══════════════════════════════════════════════════════════════════════════════

# Token cache: stored as { "token": "...", "date": "YYYY-MM-DD", "auth_time": <epoch> }
# You only need to paste the token ONCE per trading day — it is saved here.
_TOKEN_CACHE = INDIA / ".zerodha_token_cache"


def _zerodha_helpers():
    """
    Return the canonical zerodha_auth module as the Single Source of Truth for
    all Zerodha token operations.

    Importing inline (rather than top-level) avoids circular imports and allows
    the module to be absent on machines without kiteconnect.  All of
    _load_cached_token / _save_cached_token / _has_stale_cache delegate here so
    there is exactly ONE token-cache implementation across the codebase.
    """
    ensure_paths(INDIA)
    import zerodha_auth as _za  # type: ignore
    return _za


def _load_cached_token() -> str:
    """Return today's cached Zerodha token via zerodha_auth SSOT."""
    try:
        return _zerodha_helpers().load_cache()
    except Exception as exc:
        _log.debug("zerodha_cache_load_failed", error=str(exc))
    return ""


def _save_cached_token(token: str) -> None:
    """Persist today's token via zerodha_auth SSOT."""
    try:
        _zerodha_helpers().save_cache(token)
    except Exception as exc:
        _log.debug("zerodha_cache_save_failed", error=str(exc))


def _has_stale_cache() -> bool:
    """Return True if a token cache file exists but the token is expired.

    Delegates to zerodha_auth.load_cache() (the SSOT) which uses auth_time
    for a 24.75-hour expiry window — correctly handling tokens cached the
    previous evening that are still valid (Zerodha tokens live until ~06:00 IST
    the next day, not midnight local time).

    Old behaviour (date != today) caused spurious re-authentication for valid
    tokens cached after midnight IST — a common nightly run scenario.
    """
    try:
        if not _TOKEN_CACHE.exists():
            return False  # no cache at all — not stale, just absent
        # Delegate to zerodha_auth SSOT for consistent auth_time-based expiry
        cached = _zerodha_helpers().load_cache()
        return not bool(cached)  # cache exists but load_cache returned "" → stale
    except Exception as exc:
        _log.debug("zerodha_stale_cache_check_failed", error=str(exc))
    return False

def _india_env(require_zerodha: bool = True) -> dict:
    """
    Return env dict with India broker tokens.

    Zerodha token resolution order (first non-empty wins):
      1. ZERODHA_ACCESS_TOKEN env var (already set in shell or from a prior
         _india_env() call in this session)
      2. Today's token cache  (india-brokers/.zerodha_token_cache)
      3. Auto-login via zerodha_login.run_login() — tries Playwright first,
         then semi-manual browser paste. Called only if require_zerodha=True.

    When require_zerodha=False: resolve silently (no login prompt), include
    token only if already available — never block the caller.
    """
    # Always start from the CURRENT os.environ (not the stale BASE_ENV snapshot)
    # so that tokens set by a previous _india_env() call in this session are included.
    env = {
        **BASE_ENV,
        **{k: v for k, v in os.environ.items()},   # live env — picks up dynamic tokens
    }

    if not require_zerodha:
        # Non-fatal path: include token if available without triggering login
        if not env.get("ZERODHA_ACCESS_TOKEN"):
            cached = _load_cached_token()
            if cached:
                env["ZERODHA_ACCESS_TOKEN"] = cached
                os.environ["ZERODHA_ACCESS_TOKEN"] = cached
        return env

    # 1. Shell env var — trust it if present; if it turns out to be expired
    #    KiteConnect will raise TokenException, which is caught downstream.
    #    Do NOT discard a valid env-var token just because the cache file is stale.
    at = os.environ.get("ZERODHA_ACCESS_TOKEN", "")

    # 2. Today's cache
    if not at:
        at = _load_cached_token()
        if at:
            print(f"{DIM}  (Using cached Zerodha token from today){RESET}")

    # 3. Token missing — auto-trigger login (no confirmation gate)
    if not at:
        stale = _has_stale_cache()
        if stale:
            print(f"\n{YELLOW}⚠  Zerodha token has expired. Re-authenticating…{RESET}")
        else:
            print(f"\n{YELLOW}⚠  Zerodha access token is not set. Starting login…{RESET}")
        try:
            ensure_paths(INDIA)
            from zerodha_login import run_login
            at = run_login()
            if at:
                _save_cached_token(at)
                os.environ["ZERODHA_ACCESS_TOKEN"] = at
                print(f"{DIM}  Token cached for today.{RESET}")
            else:
                print(f"{YELLOW}  Login skipped — Zerodha holdings will be missing.{RESET}")
        except Exception as exc:
            print(f"{YELLOW}  Login error: {exc}{RESET}")

    if at:
        env["ZERODHA_ACCESS_TOKEN"] = at
    return env


def _require_zerodha(env: dict) -> bool:
    """Return True if env has a Zerodha token, else print a message and return False."""
    if env.get("ZERODHA_ACCESS_TOKEN"):
        return True
    print(f"\n{YELLOW}Skipped — Zerodha access token is required for this command.{RESET}")
    print(f"  Tip: set it once in your shell:{DIM}")
    print(f"  $env:ZERODHA_ACCESS_TOKEN='<token>'  then re-run.{RESET}\n")
    return False


def run_india_dashboard() -> None:
    """
    Combined India Portfolio Dashboard
    ────────────────────────────────────
    Fetches live holdings from BOTH Zerodha (DFX693) and Angel One (K51989642),
    then generates a single HTML page showing:
      • Total equity, unrealised P&L, per-broker cash available
      • Risk-alert banner if any positions are missing GTT stop-losses
      • Holdings table sorted by P&L %, with weight bars
    Opens the HTML in your default browser when done.
    Last run produced: 15 Zerodha + 4 Angel One = 19 holdings, ₹14.5L.
    """
    print(header("India Portfolio Dashboard  (Zerodha + Angel One)", MAGENTA))
    env = _india_env()
    z_ok = bool(env.get("ZERODHA_ACCESS_TOKEN"))
    print(f"""
{BOLD}BROKER COVERAGE:{RESET}
  Zerodha   {'✅ token found' if z_ok else f'{YELLOW}⚠  no token — Zerodha holdings will be missing{RESET}'}
  Angel One  ✅ TOTP auto-auth

{BOLD}WHAT YOU'LL SEE:{RESET}
  • Total equity + unrealised P&L for each broker
  • Risk-alert banner if positions have no GTT stop-loss
  • Holdings table sorted by P&L %, with position weight bars
  • HTML report opens in browser automatically
""")
    if not z_ok:
        print(f"{DIM}  Tip: run zerodha_login.py to generate a fresh access token.{RESET}\n")
    run([sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
        cwd=INDIA, env=env)


def run_india_gtt_audit() -> None:
    """
    GTT Stop-Loss Audit
    ────────────────────
    Checks every Zerodha holding and reports which positions have NO GTT
    (Good Till Triggered) order protecting them. GTT is Zerodha's OCO
    (One-Cancels-Other) mechanism for persistent stop-losses — they survive
    across sessions, unlike regular SL orders.

    Finding: All 12 Zerodha positions are currently UNPROTECTED (no stops).
    This command lists them so you can decide which to protect.
    """
    print(header("GTT Stop-Loss Audit  — find unprotected Zerodha positions", MAGENTA))
    env = _india_env()
    if not _require_zerodha(env): return
    run([sys.executable, str(INDIA / "india_gtt_manager.py"), "--audit"],
        cwd=INDIA, env=env)


def run_india_gtt_suggest() -> None:
    """
    Suggest GTT Levels (dry-run)
    ──────────────────────────────
    For every unprotected holding, calculates recommended:
      stop-loss  = avg_price − max(2×ATR, 8%)
      take-profit = avg_price + max(3×ATR, 12%)
    Shows a preview table WITHOUT placing any orders.
    ATR comes from Zerodha historical data (falls back to % if unavailable).
    """
    print(header("GTT Level Suggestions  — dry-run preview", MAGENTA))
    env = _india_env()
    if not _require_zerodha(env): return
    run([sys.executable, str(INDIA / "india_gtt_manager.py"), "--suggest"],
        cwd=INDIA, env=env)


def run_india_gtt_place() -> None:
    """
    Place GTT Stop-Losses for ALL Unprotected Positions
    ──────────────────────────────────────────────────────
    Places Zerodha GTT TWO-LEG (OCO) orders for every holding that has no
    active stop. Each GTT has both a stop-loss trigger AND a take-profit target.
    Once placed, they persist indefinitely — no daily renewal needed.

    ⚠️ This places REAL orders. Confirm before proceeding.
    Currently 12 positions are unprotected including:
      IFBIND −35.1%, SWSOLAR −34%, CAPACITE −33.8%, GREAVESCOT −33.3%
    """
    print(header("Place GTT Stop-Losses for ALL Unprotected Positions", RED))
    print(f"""
{BOLD}ORDER PREVIEW (run GTT Audit first for exact counts){RESET}
  {YELLOW}Recommended workflow: 1 (Audit) → 2 (Suggest) → 3 (Place){RESET}

  {BOLD}What will be placed:{RESET}
    One TWO-LEG GTT per unprotected holding (stop-loss + take-profit)
    GTTs persist until triggered or manually deleted
  {BOLD}Stop formula:{RESET}   avg_price − max(2×ATR, 8%)
  {BOLD}Target formula:{RESET} avg_price + max(3×ATR, 12%)

{BOLD}This will place REAL Zerodha GTT orders (not a simulation).{RESET}
{RED}⚠  Action is IRREVERSIBLE without manually deleting the GTTs.{RESET}
""")
    confirm = prompt("Type 'yes' to confirm (run 4 + 5 first for preview)", "no")
    if confirm.lower() != "yes":
        print(f"{YELLOW}Cancelled — no orders placed.{RESET}")
        print(f"{DIM}  Tip: run 4 (Audit) and 5 (Suggest) first to preview impact.{RESET}")
        return
    env = _india_env()
    if not _require_zerodha(env): return
    run([sys.executable, str(INDIA / "india_gtt_manager.py"), "--place-all"],
        cwd=INDIA, env=env)


def run_india_gtt_list() -> None:
    """
    List Active GTT Orders
    ────────────────────────
    Shows all currently active GTT orders on your Zerodha account: symbol,
    type (SINGLE or TWO-LEG), trigger levels, status (active/triggered/expired).
    """
    print(header("List Active Zerodha GTT Orders", MAGENTA))
    print(f"{DIM}  A GTT count of 0 means all positions are unprotected.\n"
          f"  Compare with GTT Audit (4): 2 active GTTs per symbol = fully protected.{RESET}\n")
    env = _india_env()
    if not _require_zerodha(env): return
    run([sys.executable, str(INDIA / "india_gtt_manager.py"), "--list"],
        cwd=INDIA, env=env)


# ─── Angel One GTT handlers ───────────────────────────────────────────────────

def run_india_ao_gtt_audit() -> None:
    """
    Angel One GTT Audit
    ────────────────────
    Checks every Angel One holding and reports which positions have NO GTT
    (Good Till Triggered) rule protecting them. Angel One's SmartAPI supports
    single-trigger GTT rules on NSE/BSE for DELIVERY and MARGIN products.
    """
    print(header("GTT Audit  — find unprotected Angel One positions", MAGENTA))
    run([sys.executable, str(INDIA / "angelone_gtt_manager.py"), "--audit"],
        cwd=INDIA)


def run_india_ao_gtt_suggest() -> None:
    """
    Suggest Angel One GTT Levels (dry-run)
    ────────────────────────────────────────
    Calculates stop-loss + target levels for every unprotected Angel One
    holding. Uses ATR(14) from Angel One Historical API (falls back to 8%/12%
    if historical data unavailable). Preview only — no orders placed.
    """
    print(header("Angel One GTT Level Suggestions  — dry-run", MAGENTA))
    run([sys.executable, str(INDIA / "angelone_gtt_manager.py"), "--suggest"],
        cwd=INDIA)


def run_india_ao_gtt_place() -> None:
    """
    Place Angel One GTT Stop-Losses for ALL Unprotected Positions
    ──────────────────────────────────────────────────────────────
    Places two separate GTT rules per position (Angel One does NOT support OCO):
      RULE 1 (stop-loss)  — SELL if price drops below stop level
      RULE 2 (take-profit) — SELL if price rises above target level
    If one triggers, the other stays active until manually cancelled.

    ⚠️ This places REAL orders on Angel One.
    """
    print(header("Place GTT Stop-Losses for ALL Unprotected Angel One Positions", RED))
    print(f"""
{BOLD}ORDER PREVIEW (run AO GTT Audit+Suggest first for exact counts){RESET}
  {YELLOW}Recommended workflow: 5 (Audit) → 6 (Suggest) → 7 (Place){RESET}

  {BOLD}What will be placed:{RESET}
    2 separate GTT rules per unprotected position (Angel One has no OCO)
    RULE 1 = stop-loss trigger, RULE 2 = take-profit trigger
  {BOLD}Stop formula:{RESET}   avg_price − max(2×ATR, 8%)
  {BOLD}Target formula:{RESET} avg_price + max(3×ATR, 12%)
  {BOLD}Cleanup note:{RESET}  After a fill, use --cleanup to cancel the surviving orphan rule

{BOLD}This will place REAL Angel One GTT rules (not a simulation).{RESET}
{RED}⚠  Action is IRREVERSIBLE without manually cancelling the GTT rules.{RESET}
""")
    confirm = prompt("Type 'yes' to confirm (run 8 + 9 first for preview)", "no")
    if confirm.lower() != "yes":
        print(f"{YELLOW}Cancelled — no orders placed.{RESET}")
        print(f"{DIM}  Tip: run 8 (Audit) and 9 (Suggest) first to preview impact.{RESET}")
        return
    run([sys.executable, str(INDIA / "angelone_gtt_manager.py"), "--place-all"],
        cwd=INDIA)


def run_india_ao_gtt_list() -> None:
    """
    List Active Angel One GTT Rules
    ─────────────────────────────────
    Shows all NEW / ACTIVE / SENTTOEXCHANGE GTT rules on your Angel One
    account: symbol, trigger price, qty, direction (SELL), status.
    """
    print(header("List Active Angel One GTT Rules", MAGENTA))
    print(f"""{DIM}
  Interpretation guide:
    Active rules = 0  →  All positions are UNPROTECTED (run audit to confirm)
    Active rules > 0  →  Each rule covers one leg (stop OR target per symbol)
    2 rules per symbol = fully protected by both stop-loss and target
  Compare active count with the AO GTT Audit (option 8) to find gaps.{RESET}\n""")
    run([sys.executable, str(INDIA / "angelone_gtt_manager.py"), "--list"],
        cwd=INDIA)


def run_india_market_internals() -> None:
    """
    India Market Internals  (Angel One SmartAPI — FREE)
    ─────────────────────────────────────────────────────
    Fetches three types of institutional + market intelligence for NSE:

    1. PCR (Put-Call Ratio)  — Bull: PCR<0.8, Bear: PCR>1.2. Fetched for
       top F&O stocks. NSE-specific, equivalent to US options flow in L4.

    2. OI Buildup signals — Long Buildup / Short Buildup / Short Covering /
       Long Unwinding. Detects institutional positioning before price moves.
       This is the closest equivalent to IBKR dark-pool flow for Indian markets.

    3. FULL Market Data (batch, 50 symbols/call) — LTP, 52W high/low,
       buy/sell quantity ratio, volume, circuit limits.
       Used internally to upgrade GTT levels from % fallback to real ATR.

    All three APIs are FREE with Angel One SmartAPI (unlike Zerodha Quote).
    """
    print(header("India Market Internals  (PCR + OI Buildup + Market Data)", YELLOW))
    print(f"""\n{BOLD}WHAT YOU'LL SEE:{RESET}\n  [1] Live Market Data — LTP, OHLC, volume, 52W range, buy/sell ratio\n  [2] Put-Call Ratio  — bullish PCR<0.8, bearish PCR>1.2\n  [3] OI Buildup      — Long/Short Buildup, Short Covering, Long Unwinding\n  [4] Composite Score — weighted MI score for the symbol (0–10)\n""")
    symbol    = prompt("Symbol to check (NSE:SYMBOL)", "NSE:RELIANCE")
    expiry    = prompt("F&O expiry type for OI data (NEAR / NEXT / FAR)", "NEAR")
    show_all  = prompt("Run all analyses (PCR + OI + Score)? (y/n)", "y").lower() == "y"
    show_gain = prompt("Show top NSE F&O gainers/losers? (y/n)", "n").lower() == "y"
    cmd = [sys.executable, str(INDIA / "india_market_internals.py"),
           "--symbol", symbol, "--expiry", expiry]
    if show_all:
        cmd.append("--all")
    else:
        cmd.extend(["--pcr", "--oi-buildup"])
    if show_gain:
        cmd.extend(["--gainers", "--losers"])
    run(cmd, cwd=INDIA)


def run_india_order_test() -> None:
    """
    India Order Executor  — Dry-Run Test
    ──────────────────────────────────────
    Simulates a TradingView NSE BUY signal end-to-end:
      1. TV alert arrives → symbol parsed
      2. Position size calculated from available Zerodha margin
      3. MARKET BUY qty computed
      4. GTT TWO-LEG (stop-loss + target) calculated
    No real orders are placed. Shows exactly what WOULD happen for a live alert.
    """
    print(header("India Order Executor  — Dry-Run", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Simulated BUY signal flow: symbol parse → position sizing → GTT levels
  • No real orders are placed — all output is labelled [DRY-RUN]
  • Position size uses ₹ amount you enter ÷ entered price (no live margin fetch)

{BOLD}Requires:{RESET}  ZERODHA_ACCESS_TOKEN — used to verify the symbol is a valid holding.
  {DIM}If the token is missing you'll see an auth error. Set it via zerodha_login.py.{RESET}
""")
    symbol = prompt("TV symbol to simulate (e.g. NSE:INFY)", "NSE:INFY")
    price  = prompt("Current price (₹)", "")
    amount = prompt("Order amount (₹)", "15000")
    env = _india_env()
    if not _require_zerodha(env): return
    cmd = [sys.executable, str(INDIA / "india_order_executor.py"),
           "--symbol", symbol, "--dry-run"]
    if price:
        cmd += ["--price", price]
    if amount:
        cmd += ["--amount", amount]
    run(cmd, cwd=INDIA, env=env)


def run_india_tests() -> None:
    """
    Run India Broker Test Suite
    ────────────────────────────
    Runs all unit + integration tests for the India broker components.
    Unit tests use mocks (no broker connection needed).
    Integration tests require live credentials (ZERODHA_ACCESS_TOKEN set).
    """
    print(header("India Broker Test Suite", BLUE))
    print(f"""
{BOLD}Test options:{RESET}
  {CYAN}1{RESET}) Unit tests only    (no credentials required — uses mocks)
  {CYAN}2{RESET}) Integration tests  (requires ZERODHA_ACCESS_TOKEN + Angel One TOTP)
  {CYAN}3{RESET}) All tests
""")
    choice = prompt("Enter 1 / 2 / 3", "1")
    if choice == "2":
        env = _india_env()
        run([sys.executable, "-m", "pytest", "-v", "--tb=short",
             str(INDIA / "test_integration_india.py")], cwd=INDIA, env=env)
    elif choice == "3":
        env = _india_env()
        run([sys.executable, "-m", "pytest", "-v", "--tb=short",
             str(INDIA)], cwd=INDIA, env=env)
    else:
        run([sys.executable, "-m", "pytest", "-v", "--tb=short",
             str(INDIA / "test_india_enricher.py"),
             str(INDIA / "test_india_gtt.py"),
             str(INDIA / "test_india_order_executor.py"),
             str(INDIA / "test_india_market_internals.py")], cwd=INDIA)

def run_india_fo_analyzer() -> None:
    """
    India F&O Loss Analyzer  — surface hidden options P&L
    ───────────────────────────────────────────────────────
    Parses Zerodha + Angel One trade history to find option premium burn,
    expired contracts, and F&O cost drag hidden inside net equity P&L.
    Generates a dedicated HTML breakdown with per-contract loss forensics.
    """
    print(header("India F&O Loss Analyzer  — surface hidden options P&L", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • All option buy/sell trades extracted from Zerodha + Angel One statements
  • Expired (worthless) options clearly flagged — often the biggest hidden loss
  • F&O P&L broken down by underlying, expiry, contract type (CE/PE)
  • Net premium burn vs equity gains side-by-side
  • Full HTML report at reports/india_fo_analysis.html

{BOLD}Requires:{RESET}  Zerodha statement XLSX files in zerodha-statement/
  {DIM}Download from Zerodha: Console → Reports → TradeBook & P&L → Export.{RESET}
""")
    out = str(REPORTS / "india_fo_analysis.html")
    run([sys.executable, str(INDIA / "india_fo_analyzer.py"), "--html", "--out", out],
        cwd=ROOT)


def run_fo_arbitrage() -> None:
    """
    F&O / Forex Arbitrage Engine
    ───────────────────────────────
    Detects basis spread opportunities across:
      1. NSE Index Arb — compares yfinance spot vs TV Pro near-month futures
         for Nifty / BankNifty / FinNifty.  Signals: Rich / Cheap / Fair.
      2. G10 Forex Basis — TV spot vs CME FX futures (EUR, GBP, JPY, AUD, CAD, NZD).
      3. AngelOne OI Buildup crosscheck — Long Buildup / Short Covering signals.
    """
    print(header("F&O / Forex Arbitrage Engine", YELLOW))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • NSE Nifty / BankNifty / FinNifty basis vs fair-value (bps)
  • Signal: Rich (sell futures) | Cheap (buy futures) | Fair
  • G10 forex forward basis (pips): EUR, GBP, JPY, AUD, CAD, NZD
  • Top OI Buildup signals — Long Buildup + Short Covering
  • Full HTML report at reports/fo_arbitrage_report.html

{BOLD}Options:{RESET}
  --no-india   skip AngelOne OI Buildup calls
  --no-forex   skip G10 forex basis section
  --rfi <n>    India risk-free rate % (default 6.5, current RBI repo rate)
""")
    rfi     = prompt("India risk-free rate % (press Enter for 6.5)", "6.5")
    skip_ao = prompt("Skip Angel One OI Buildup? (y/n)", "n").lower() == "y"
    skip_fx = prompt("Skip G10 forex basis? (y/n)", "n").lower() == "y"
    cmd = [sys.executable, str(TV / "screeners" / "fo_arbitrage.py"),
           "--rfi", rfi,
           "--out", str(REPORTS / "fo_arbitrage_report.html")]
    if skip_ao:
        cmd.append("--no-india")
    if skip_fx:
        cmd.append("--no-forex")
    run(cmd, cwd=ROOT)


def run_tv_options_screener() -> None:
    """TV Pro Options Screener — reverse-engineered scan2 API."""
    print(header("TV Pro Options Screener  — full chain via scan2 API", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Full options chain for any symbol: strike, bid/ask, IV, Greeks (delta/gamma/theta/vega/rho)
  • Filters: call/put, min-IV, max-DTE, min-volume, delta range
  • Multi-symbol screener mode (--screener flag)
  • Dark-theme HTML report at reports/tv_options_screener.html

{BOLD}Options:{RESET}
  --type call|put    filter by option type
  --min-iv <0.3>     minimum implied volatility (e.g. 0.30 = 30%)
  --max-dte <45>     maximum days to expiration
  --min-volume <n>   minimum daily volume
  --limit <500>      max contracts per request (default: 500)

{YELLOW}Requires:{RESET}  tradingview/cookie file with sessionid + sessionid_sign
""")
    sym     = prompt("Symbol (e.g. NYSE:NEE, NASDAQ:AAPL)", "NYSE:NEE").strip()
    opt_type = prompt("Option type (call/put/both)", "both").strip().lower()
    min_iv  = prompt("Min IV (e.g. 0.30, blank=none)", "").strip()
    max_dte = prompt("Max DTE (e.g. 45, blank=none)", "").strip()
    limit   = prompt("Max contracts", "500").strip()
    out     = str(REPORTS / "tv_options_screener.html")
    cmd = [sys.executable, str(TV / "tv_options_screener.py"),
           "--symbol", sym, "--limit", limit, "--out", out, "--no-open"]
    if opt_type in ("call", "put"):
        cmd += ["--type", opt_type]
    if min_iv:
        cmd += ["--min-iv", min_iv]
    if max_dte:
        cmd += ["--max-dte", max_dte]
    run(cmd, cwd=ROOT)


def run_options_scan_us_quality() -> None:
    """US 300 Quality Options Scan — top CALL per quality stock (TQS v2 ranked)."""
    print(header("US 300 Quality Options Scan  — top CALL per quality stock", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Top CALL option per stock across up to 300 US quality stocks
  • Scored by TQS v2 (Trade Quality Score): spread, OI, IV, pressure
  • Win Rate Score (WRS): lognormal break-even probability
  • Source badges: OI:yfinance | WS:Live / WS:Closed
  • Searchable dark-theme HTML report at reports/options_scan_us_300.html

{BOLD}Stock quality gate:{RESET}
  Price > $10 | Market cap > $200M | Avg volume > 100K | No OTC

{BOLD}Contract quality gate:{RESET}
  Premium $0.10–$20.00 (default) | Spread ≤ 20% | OI display from yfinance

{YELLOW}Requires:{RESET}  tradingview/cookie file with sessionid + sessionid_sign
""")
    n         = prompt("Max stocks to scan", "300").strip() or "300"
    refresh   = prompt("Refresh stock universe? (y/n)", "n").strip().lower()
    max_dte   = prompt("Max DTE (days to expiry)", "28").strip() or "28"
    min_prem  = prompt("Min premium $ (default 0.10)", "0.10").strip() or "0.10"
    max_prem  = prompt("Max premium $ (default 20.00)", "20.00").strip() or "20.00"
    out       = str(REPORTS / "options_scan_us_300.html")
    cmd = [
        sys.executable,
        str(TV / "options" / "options_contract_screener.py"),
        "--n", n,
        "--expiries", "3",
        "--max-dte", max_dte,
        "--out", out,
        "--min-premium", min_prem,
        "--max-premium", max_prem,
    ]
    if refresh == "y":
        cmd.append("--refresh-universe")
    cmd.append("--verbose")
    run(cmd, cwd=ROOT)


def run_options_scan_us_quality_puts() -> None:
    """US 300 Quality Options Scan — top PUT per quality stock (TQS v2 ranked)."""
    print(header("US 300 Quality Options Scan  — top PUT per quality stock", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Top PUT option per stock across up to 300 US quality stocks
  • Scored by TQS v2 (Trade Quality Score): spread, OI, IV, pressure
  • Win Rate Score (WRS): lognormal break-even probability
  • PUT-aware support display: breaking below = ↓ good signal
  • Searchable dark-theme HTML report at reports/options_scan_us_300_puts.html

{BOLD}Stock quality gate:{RESET}
  Price > $10 | Market cap > $200M | Avg volume > 100K | No OTC

{BOLD}Contract quality gate:{RESET}
  Premium $0.10–$20.00 (default) | Spread ≤ 20% | OI display from yfinance

{YELLOW}Requires:{RESET}  tradingview/cookie file with sessionid + sessionid_sign
""")
    n         = prompt("Max stocks to scan", "300").strip() or "300"
    refresh   = prompt("Refresh stock universe? (y/n)", "n").strip().lower()
    max_dte   = prompt("Max DTE (days to expiry)", "28").strip() or "28"
    min_prem  = prompt("Min premium $ (default 0.10)", "0.10").strip() or "0.10"
    max_prem  = prompt("Max premium $ (default 20.00)", "20.00").strip() or "20.00"
    out       = str(REPORTS / "options_scan_us_300_puts.html")
    cmd = [
        sys.executable,
        str(TV / "options" / "options_contract_screener.py"),
        "--n", n,
        "--expiries", "3",
        "--max-dte", max_dte,
        "--out", out,
        "--min-premium", min_prem,
        "--max-premium", max_prem,
        "--option-type", "PUT",
    ]
    if refresh == "y":
        cmd.append("--refresh-universe")
    cmd.append("--verbose")
    run(cmd, cwd=ROOT)


def run_tqs_v2_calls() -> None:
    """TQS v2 US Quality CALL Scan — new 7-dimension scoring engine."""
    print(header("TQS v2 — US 300 Quality CALL Scan  (new scoring engine)", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Top CALL per stock across up to 300 quality US stocks ($1B+ market cap)
  • Scored on 7 dimensions: Liquidity · IV Analysis · Probability · Risk/Reward
    · Win Rate · Volatility Conditions · Premium Efficiency
  • Grades: A+ ≥85 · A ≥80 · B+ ≥75 · B ≥70 · C+ ≥65 · C ≥60
  • OI enriched from yfinance (real, delayed ~1 day)
  • HTML + JSON output: reports/tqs_v2_calls.html / reports/tqs_v2_calls.json

{BOLD}Stock quality gate:{RESET}
  Price > $10 | Market cap > $1B | NYSE/NASDAQ only | No preferred shares

{BOLD}Contract quality gate:{RESET}
  DTE 21–90 | Bid > $0.10 | Spread < 12% | Delta 0.20–0.55 | OI > 1500

{YELLOW}Requires:{RESET}  tradingview/cookie file with sessionid + sessionid_sign
""")
    ans = prompt("Run CALL scan now? (y/n)", "y").strip().lower()
    if ans != "y":
        print("Scan aborted.")
        return

    # ── Interactive parameters ──────────────────────────────────────────
    max_premium_raw = prompt(
        "Max premium / budget per contract in $ (default $4.00, use 10 for large-caps like NVDA/AAPL)", "4.00"
    ).strip()
    try:
        max_premium = float(max_premium_raw)
    except ValueError:
        max_premium = 4.00

    top_n_raw = prompt(
        "Number of stocks to scan (default 20 | choose 50 / 100 / 200 / 300 for broader universe)", "20"
    ).strip()
    try:
        top_n = max(1, min(300, int(top_n_raw)))
    except ValueError:
        top_n = 20

    sort_raw = prompt(
        "Sort stocks by: [1] Market Cap — default  [2] Relative Volume (momentum mode)", "1"
    ).strip()
    if sort_raw == "2":
        sort_by = "relative_volume_10d_calc"
        min_rv = "0.0"   # TV returns RV=0 on weekends — don't filter, just sort
    else:
        sort_by = "market_cap_basic"
        min_rv = "0.0"

    mode_raw = prompt(
        "Scan mode: [1] Standard (21-90 DTE, quality) — default  [2] Catalyst (7-21 DTE, near-earnings + flow momentum)", "1"
    ).strip()
    catalyst_mode = mode_raw == "2"

    cmd = [
        sys.executable, str(TV / "options" / "tqs_v2" / "workflow.py"),
        "--scan-type", "calls",
        "--max-premium", str(max_premium),
        "--top-n", str(top_n),
        "--sort-by", sort_by,
        "--min-rv", min_rv,
    ]
    if catalyst_mode:
        cmd.append("--catalyst")
    run(cmd, cwd=ROOT)


def run_tqs_v2_puts() -> None:
    """TQS v2 US Quality PUT Scan — new 7-dimension scoring engine."""
    print(header("TQS v2 — US 300 Quality PUT Scan  (new scoring engine)", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Top PUT per stock across up to 300 quality US stocks ($1B+ market cap)
  • Scored on 7 dimensions: Liquidity · IV Analysis · Probability · Risk/Reward
    · Win Rate · Volatility Conditions · Premium Efficiency
  • PUT-aware delta scoring (sweet zone 0.20–0.50 abs delta)
  • OI enriched from yfinance (real, delayed ~1 day)
  • HTML + JSON output: reports/tqs_v2_puts.html / reports/tqs_v2_puts.json

{BOLD}Stock quality gate:{RESET}
  Price > $10 | Market cap > $1B | NYSE/NASDAQ only | No preferred shares

{BOLD}Contract quality gate:{RESET}
  DTE 21–90 | Bid > $0.10 | Spread < 12% | Delta −0.55 to −0.20 | OI > 1500

{YELLOW}Requires:{RESET}  tradingview/cookie file with sessionid + sessionid_sign
""")
    ans = prompt("Run PUT scan now? (y/n)", "y").strip().lower()
    if ans != "y":
        print("Scan aborted.")
        return

    # ── Interactive parameters ──────────────────────────────────────────
    max_premium_raw = prompt(
        "Max premium / budget per contract in $ (default $4.00, use 10 for large-caps like NVDA/AAPL)", "4.00"
    ).strip()
    try:
        max_premium = float(max_premium_raw)
    except ValueError:
        max_premium = 4.00

    top_n_raw = prompt(
        "Number of stocks to scan (default 20 | choose 50 / 100 / 200 / 300 for broader universe)", "20"
    ).strip()
    try:
        top_n = max(1, min(300, int(top_n_raw)))
    except ValueError:
        top_n = 20

    sort_raw = prompt(
        "Sort stocks by: [1] Market Cap — default  [2] Relative Volume (momentum mode)", "1"
    ).strip()
    if sort_raw == "2":
        sort_by = "relative_volume_10d_calc"
        min_rv = "0.0"   # TV returns RV=0 on weekends — don't filter, just sort
    else:
        sort_by = "market_cap_basic"
        min_rv = "0.0"

    mode_raw = prompt(
        "Scan mode: [1] Standard (21-90 DTE, quality) — default  [2] Catalyst (7-21 DTE, near-earnings + flow momentum)", "1"
    ).strip()
    catalyst_mode = mode_raw == "2"

    cmd = [
        sys.executable, str(TV / "options" / "tqs_v2" / "workflow.py"),
        "--scan-type", "puts",
        "--max-premium", str(max_premium),
        "--top-n", str(top_n),
        "--sort-by", sort_by,
        "--min-rv", min_rv,
    ]
    if catalyst_mode:
        cmd.append("--catalyst")
    run(cmd, cwd=ROOT)


def run_tqs_v2_backtest() -> None:
    """TQS v2 Backtest Analyzer — fetch expiry outcomes and generate factor analysis report."""
    print(header("TQS v2 — Backtest Analyzer  (win/loss factor analysis)", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Fetches expiry stock prices for all expired TQS v2 contracts (via yfinance)
  • Computes intrinsic value, P&L %, and win/loss outcome per contract
  • Spearman rank correlations: which scoring factors predict wins?
  • Win rate breakdown by grade (A/B/C/D/F) and by type (CALL/PUT)
  • HTML + JSON output: reports/tqs_v2_backtest_TIMESTAMP.html

{YELLOW}Requires:{RESET}  Prior TQS v2 scans with expired contracts in the database
           Run options 61/62 first, then wait for contracts to expire.
""")

    out = str(REPORTS / "tqs_v2_backtest.html")
    cmd = [
        sys.executable, str(TV / "options" / "tqs_v2" / "backtest_main.py"),
        "--out", out,
    ]
    run(cmd, cwd=ROOT)


def run_zero_dte() -> None:
    """ZeroDTE 0-2DTE Pattern Scanner — CALL and PUT signals."""
    print(header("ZeroDTE 0-2DTE Pattern Scanner", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Stocks with IV backwardation (near-term event risk signal)
  • VOI surge — institutional call/put buying >10x C/P ratio
  • Strong momentum: prev day >=2% move, volume >=1.2x, RSI aligned
  • Matching 0-2 DTE contracts (CALL and PUT setups)
  • Composite score 0-100 (Momentum 40 + IV Backwardation 30 + VOI 30)
""")
    direction = prompt("Scan type (calls/puts/both)", "both").strip().lower()
    if direction not in ("calls", "puts", "both"):
        direction = "both"
    max_premium = prompt("Max premium per contract ($) [0-2DTE range]", "1.00").strip()
    min_score = prompt("Min composite score (0-100)", "60").strip()
    out_dir = str(REPORTS)
    cmd = [
        sys.executable,
        str(TV / "options" / "zerodte" / "workflow.py"),
        "--direction", direction,
        "--max-premium", max_premium,
        "--min-score", min_score,
        "--out-dir", out_dir,
    ]
    run(cmd, cwd=ROOT)


def run_zerodte_backtest() -> None:
    """ZeroDTE Backtest — fetch expiry outcomes and generate win/loss factor analysis."""
    print(header("ZeroDTE Backtest Analyzer  — win/loss factor analysis", CYAN))
    out_html = str(REPORTS / "zerodte_backtest.html")
    out_json = str(REPORTS / "zerodte_backtest.json")
    cmd = [
        sys.executable,
        str(TV / "options" / "zerodte" / "backtest_vectorbt.py"),
        "--out", out_html,
        "--json", out_json,
    ]
    run(cmd, cwd=ROOT)


def run_options_specialist_analysis() -> None:
    """Options Specialist Analysis — independent scoring of PUT/CALL contracts from JSON."""
    print(header("Options Specialist Analysis  — independent scoring from options theory", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Independent evaluation of all contracts in the options JSON reports
  • Uses ONLY options theory: IV, delta, DTE, OI, spread, break-even sigma, support
  • No access to system filters — completely fresh perspective
  • Compares specialist picks vs system actionable list (match rate target ≥ 80%)
  • Output: reports/options_specialist_puts.json or options_specialist_calls.json

{BOLD}Specialist criteria (weighted 0-100):{RESET}
  IV attractiveness 25wt  |  Delta sweet spot 20wt  |  DTE bucket 15wt
  OI liquidity 15wt       |  Spread quality 10wt    |  BE sigma ratio 10wt
  Support holding 5wt

{YELLOW}Requires:{RESET}  JSON companion files from a prior PUT or CALL scan (menus 10d / 10f)
""")
    opt_type_in = prompt("Evaluate PUT or CALL contracts? (put/call)", "put").strip().lower()
    if "put" in opt_type_in:
        act_default = "reports/options_actionable_puts.json"
        rej_default = "reports/options_rejected_puts.json"
        out_default = "reports/options_specialist_puts.json"
    else:
        act_default = "reports/options_actionable.json"
        rej_default = "reports/options_rejected.json"
        out_default = "reports/options_specialist_calls.json"

    act = prompt(f"Actionable JSON path", act_default).strip() or act_default
    rej = prompt(f"Rejected JSON path (or blank to auto-detect)", rej_default).strip() or rej_default
    out = prompt(f"Output JSON path", out_default).strip() or out_default

    cmd = [
        sys.executable,
        str(TV / "options" / "options_specialist.py"),
        "--actionable", act,
        "--rejected", rej,
        "--out", out,
    ]
    run(cmd, cwd=ROOT)


def run_options_hedger() -> None:
    """
    Options Hedging Advisor  — Global portfolio hedge recommendations
    ─────────────────────────────────────────────────────────────────────
    Analyses open Zerodha + Angel One holdings and recommends tailored
    hedging strategies per position based on P&L %, market regime, OI buildup,
    and TV Pro technical data (RSI / ADX / SMA200).

    Strategies: EXIT | PROTECTIVE PUT | COLLAR | COVERED CALL | HOLD
    """
    print(header("Options Hedging Advisor  — Global portfolio hedge recommendations", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Per-position hedge recommendation: EXIT / PROTECTIVE PUT / COLLAR / COVERED CALL / HOLD
  • Indicative strike, delta, and expiry for each recommended position
  • Cost estimate in ₹ and % of position value
  • Market regime context (bull / bear / sideways) from india_regime.py
  • Full HTML report at reports/hedging_advisor_report.html

{BOLD}Options:{RESET}
  --min-value <n>  minimum position market value in ₹ to analyse (default: 0)
  --no-ao          skip Angel One OI Buildup calls
  --no-regime      skip india_regime.py regime detection

{YELLOW}Requires:{RESET}  Zerodha holdings (ZERODHA_ACCESS_TOKEN) + Angel One TOTP
""")
    env      = _india_env(require_zerodha=False)
    min_val  = prompt("Minimum position size to analyse (₹, Enter for All)", "0")
    skip_ao  = prompt("Skip Angel One OI Buildup? (y/n)", "n").lower() == "y"
    skip_reg = prompt("Skip regime detection? (y/n)", "n").lower() == "y"
    cmd = [sys.executable, str(TV / "screeners" / "options_hedger.py"),
           "--min-value", min_val,
           "--out", str(REPORTS / "hedging_advisor_report.html")]
    if skip_ao:
        cmd.append("--no-ao")
    if skip_reg:
        cmd.append("--no-regime")
    run(cmd, cwd=ROOT, env=env)


def run_broker_api_audit() -> None:
    """
    Broker API Capabilities Audit
    ────────────────────────────────
    Runs live probes across all data sources and produces a capability matrix:
      TV Pro / Moomoo / Angel One / IBKR / Zerodha / yfinance
    Shows what is FULL / PARTIAL / EXTRA$ / HAS-CODE / NONE for each asset class.
    Helpful to know which source to use when adding new features.
    """
    print(header("Broker API Capabilities Audit", BLUE))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Capability matrix: all 5 brokers + yfinance vs all asset classes
    (Stocks, Options, Futures, Forex, Crypto, Gold, ETFs, Market Internals…)
  • Rating: FULL (green) | PARTIAL (lime) | EXTRA$ (amber) | HAS-CODE (blue) | NONE (grey)
  • Live probe results confirming which APIs are currently reachable
  • Full HTML report at reports/broker_api_audit.html

{BOLD}Options:{RESET}
  --no-probes     skip live probes (static matrix only, faster)
  --test-symbol   test options chain on a specific US symbol (e.g. AAPL)

{YELLOW}Note:{RESET} Live probes require OpenD on 11111 (Moomoo AU) and ANGEL_TOTP in env.
  IBKR TWS on port 7496 is optional; IBKR section shows HAS-CODE if not running.
""")
    script = ROOT / "broker_api_audit.py"
    if not script.is_file():
        print(f"{RED}Script not found: {script}{RESET}")
        print(f"{DIM}broker_api_audit.py is documented in WORKSPACE_SPEC.md but is not "
              f"present in the repository, so this option cannot run.{RESET}")
        return
    out       = str(REPORTS / "broker_api_audit.html")
    skip_prb  = prompt("Skip live probes? (y/n)", "n").lower() == "y"
    test_sym  = prompt("Test options chain on symbol (blank to skip)", "").strip()
    cmd       = [sys.executable, str(script), "--html", "--out", out]
    if skip_prb:
        cmd.append("--no-probes")
    if test_sym:
        cmd += ["--test-symbol", test_sym]
    run(cmd, cwd=ROOT)


def run_vectorbt_backtest() -> None:
    print(header("Signal Score Event Study — vectorbt Bucket Analysis", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Bucket stats (LOW/MED/HIGH/APEX) from matured alert history
  • Win rate and mean fixed 7-day forward return per bucket
  • 3 in-sample time windows to check period-to-period consistency
  • Score vs 7d-return scatter chart
  • Tests whether HIGH composite score (≥65) beats LOW (<55) signals
  • Data source: {CYAN}database/alert_tracker.db{RESET} (read-only, matured 7d outcomes)

{DIM}NOTE: in-sample event study — not out-of-sample walk-forward, and no
      cost/slippage/benchmark model. Equity and drawdown are hypothetical.{RESET}

{DIM}Output: reports/backtest_vectorbt_report.html{RESET}
""")
    out = str(REPORTS / "backtest_vectorbt_report.html")
    cmd = [
        sys.executable,
        str(TV / "backtest_vectorbt.py"),
        "--out", out,
    ]
    run(cmd, cwd=TV)
    if Path(out).exists():
        print(f"  Report: file:///{out}")


def run_signal_outcome_study() -> None:
    print(header("Signal Outcome Study — does the score predict forward returns?", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Engine eligibility: stored {CYAN}actionable{RESET} flag vs rejected — mean excess return
    (the flag as recorded at scoring time, NOT a re-derived score ≥ 62.5 cutoff)
  • Score bands (<55 / 55–62.5 / 62.5–70 / ≥70) with mean/median excess and hit rates
  • Spearman ρ of composite score vs 7-day excess return
  • 95% symbol-cluster bootstrap CI on each gap → EDGE / INVERTED / NO DETECTABLE EDGE
  • Each score-model version reported separately (never pooled)

{BOLD}HOW IT DIFFERS FROM OPTION 35:{RESET}
  • Decision time = {CYAN}alerts.created_at{RESET} (when the scored alert existed), so no
    score input post-dates the entry — the legacy study measured from fire_time.
  • Entry = last completed hourly close at decision time; exit = close nearest +7d (±24h).
  • Excess return is measured against the exchange index over the same bars.
  • Built on the audited outcome logic ({CYAN}tradingview/outcome_logic.py{RESET}).

{DIM}NOTE: observed point samples, not executed trades — no costs, slippage or fills.
      Verdicts need ≥30 benchmarked alerts AND ≥10 distinct symbols per arm, because
      repeat alerts on one symbol are not independent samples. Excess return is dropped
      when the index bar is >96h from the stock's entry or exit. Legacy rows were marked
      actionable under an older policy and can sit below 62.5 — the report prints each
      version's lowest actionable score. Database opened read-only.{RESET}

{DIM}Output: reports/signal_outcome_study.html  (+ .json companion){RESET}
""")
    out = str(REPORTS / "signal_outcome_study.html")
    cmd = [
        _python_executable(),
        str(TV / "backtest" / "outcome_study.py"),
        "--out", out,
    ]
    run(cmd, cwd=ROOT)
    if Path(out).exists():
        print(f"  Report: file:///{out}")


def run_options_rec_report() -> None:
    """Options Recommendations Backtest Report — analyse US options rec outcomes."""
    print(header("Options Recommendations Backtest Report  — US rec outcomes analysis", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • 3 separate backtest reports (all / actionable / rejected) + companion JSON files
  • KPI strip: win rate, avg P\u0026L%, signal/option scores across all US recs
  • Spearman correlation of 12 predictors vs win outcome (requires scipy)
  • Win/loss breakdowns by Mode, Option Type, DTE, Moneyness, Signal Score, Earnings Risk, BE/\u03c3
  • Monthly win rate trend + P\u0026L distribution histogram
  • Full sortable trades table (click header to expand / collapse)

{DIM}Output:
  reports/options_scan_us_300_backtest.html/json  \u2014 all US recs
  reports/options_actionable_backtest.html/json   \u2014 high-score recs (option_score \u2265 60)
  reports/options_rejected_backtest.html/json     \u2014 low-score recs  (option_score < 60){RESET}
""")
    skip_update = prompt("Skip current premium refresh? (y/n)", "n").strip().lower() == "y"
    out    = str(REPORTS / "options_scan_us_300_backtest.html")
    cmd    = [
        sys.executable,
        str(TV / "options" / "options_recommendations_report.py"),
        "--out", out,
    ]
    if skip_update:
        cmd.append("--no-update-premiums")
    run(cmd, cwd=TV)
    for fname in [
        "options_scan_us_300_backtest.html",
        "options_actionable_backtest.html",
        "options_rejected_backtest.html",
    ]:
        p = REPORTS / fname
        if p.exists():
            print(f"  Report: file:///{p}")


def run_live_dashboard() -> None:
    print(header("Live Portfolio Dashboard  — Streamlit interactive view", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Real-time P&L summary cards per currency (AUD / USD / HKD / SGD / INR)
  • Per-broker P&L bar chart (Moomoo-AU, Moomoo-SG, IBKR, Webull, Zerodha, AngelOne)
  • Filterable position table sorted by P&L
  • Auto-refreshes every 60 s — reads from {CYAN}.cache_global_positions.json{RESET}
  • Broker status panel showing last fetch result

{DIM}Tip: Run option 1 (Global Portfolio Dashboard) first to populate the cache.
Opens browser at http://localhost:8501{RESET}
""")
    port = prompt("Streamlit port", "8501").strip() or "8501"
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(TV / "live_dashboard.py"),
        "--server.port", port,
        "--server.headless", "false",
    ]
    # Launch in a NEW terminal window so this CLI menu keeps responding.
    # subprocess.run() would block until Streamlit exits — unacceptable for a
    # menu-driven tool.  CREATE_NEW_CONSOLE spawns a detached console on Windows.
    import subprocess as _sp
    popen_kwargs: dict = {"cwd": str(TV)}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = _sp.CREATE_NEW_CONSOLE
    _sp.Popen(cmd, **popen_kwargs)
    print(f"\n  {GREEN}{BOLD}✓  Live Dashboard launched in a new terminal window.{RESET}")
    print(f"  {CYAN}→  Open: http://localhost:{port}{RESET}")
    print(f"  {DIM}   Close the Streamlit terminal window to stop the server.{RESET}\n")


def run_moomoo_market_tools() -> None:
    print(header("Moomoo Market Tools  — 6 untapped APIs", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Market state — 🟢/🔴 trading hours for US / HK / SH / SZ / SG
  • SG futures  — CN50, Nikkei, MSCI Taiwan / India / SGX contracts
  • HK warrants — top warrants by volume for any HK stock
  • Basicinfo   — PE, EPS, lot_size for HK / SG symbols (TV screener fallback)
  • Intraday bars — K_1M / K_5M OHLCV → trend, vol spike, momentum score
  • Watchlist sync — push TradingView symbols into a Moomoo watchlist group
  • Benchmark   — latency test all 8 APIs (baseline comparison)

{DIM}Requires: OpenD running on 11111 (AU) and / or 11112 (SG).{RESET}
""")
    MODE_OPTS = {
        "state": "Market state for all markets",
        "sync":  "TV ↔ Moomoo watchlist sync (read current + diff)",
        "futures": "SG futures snapshot",
        "warrants": "HK warrants for a stock",
        "basicinfo": "Basicinfo for specific mm_codes",
        "bars":  "Intraday K_5M bars + momentum signals",
        "bench": "Latency benchmark all 8 APIs",
        "smoke": "Quick connectivity smoke test (all APIs)",
    }
    for k, v in MODE_OPTS.items():
        print(f"  {CYAN}{k:<12}{RESET} {v}")
    mode = prompt("\nAction", "state").strip().lower()

    cmd: list = [sys.executable, str(TV / "moomoo_market_tools.py")]
    if mode == "state":
        cmd.append("--market-state")
    elif mode == "sync":
        group = prompt("Moomoo watchlist group name", "My Favorites").strip()
        cmd += ["--smoke"]   # smoke test includes watchlist read
        print(f"\n  {DIM}Watchlist group: {group}{RESET}")
        print(f"  {DIM}Use sync_tv_to_moomoo() directly from Python for full sync.{RESET}")
    elif mode == "futures":
        cmd.append("--futures")
    elif mode == "warrants":
        hk_code = prompt("HK code (Moomoo format)", "HK.00700").strip()
        cmd += ["--warrants", hk_code]
    elif mode == "basicinfo":
        codes = prompt("Moomoo codes (comma-separated)", "US.AAPL,HK.00700").strip()
        cmd += ["--basicinfo", codes]
    elif mode == "bars":
        code   = prompt("Moomoo code", "US.AAPL").strip()
        btype  = prompt("Bar type (K_1M / K_5M / K_15M)", "K_5M").strip()
        num    = prompt("Number of bars", "60").strip()
        cmd += ["--bars", code, "--bars-type", btype, "--bars-num", num]
    elif mode == "bench":
        cmd.append("--bench")
    else:
        cmd.append("--smoke")

    run(cmd, cwd=TV)


def run_multi_source_test() -> None:
    """
    Multi-Source Data Integration Tests
    ──────────────────────────────────────
    Validates all data sources end-to-end with live probes:
      TV Pro scanner, Moomoo, Angel One, IBKR, Zerodha, yfinance, AngelOne PCR/OI.
    Shows PASS / FAIL per test with timing. Generates HTML report.
    """
    print(header("Multi-Source Data Integration Tests", BLUE))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Pass / Fail for each data source test with response time
  • TV Pro scanner endpoint reachability + data quality checks
  • Moomoo OpenD position fetch test
  • Angel One SmartAPI auth + PCR + OI Buildup probe
  • Zerodha + IBKR connectivity checks
  • yfinance spot / futures / options chain tests
  • HTML report at reports/multi_source_test.html

{DIM}Full test suite takes ~60 seconds due to live API calls.
Use --quick to skip slow sources (e.g. IBKR TWS probe).{RESET}
""")
    quick = prompt("Quick mode — skip slow probes? (y/n)", "y").lower() == "y"
    cmd   = [sys.executable, str(ROOT / "multi_source_test.py")]
    if quick:
        cmd.append("--quick")
    run(cmd, cwd=ROOT)


# ══════════════════════════════════════════════════════════════════════════════
# Performance Analyzer
# ══════════════════════════════════════════════════════════════════════════════

def run_performance_analyzer() -> None:
    print(header("Performance Analyzer  — data-driven signal improvement", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  \u2022 Queries alert_tracker.db for all scored signals with outcomes
  \u2022 Computes Spearman correlation: L1\u2013L4 / composite / VIX vs win rate
  \u2022 Identifies best/worst input sources (win rate, avg return, MAE/MFE)
  \u2022 Compares success rates across market regimes (bull/bear/neutral/vix-spike)
  \u2022 Detects failure patterns (e.g. low L2 + high VIX \u2192 poor outcomes)
  \u2022 Suggests threshold adjustments (e.g. "raise composite to 65")
  \u2022 Rolling 30-day win rate time-series chart

{BOLD}Price Coverage:{RESET}
  Signals from CBOT (futures), BMFBOVESPA, MYX, TASE, NSE/BSE etc. cannot be
  price-tracked via yfinance and will always show \u2014 for Entry $ / Return%.
  Use {CYAN}--tradable-only{RESET} mode to exclude them for a cleaner win-rate picture.

{BOLD}Output:{RESET}
  {CYAN}{REPORTS / "performance_analysis.html"}{RESET}
""")
    min_samples   = prompt("Minimum samples per bucket", "10").strip() or "10"
    since         = prompt("Since date YYYY-MM-DD (blank = all time)", "").strip() or ""
    tradable_only = prompt("Tradable-only? Exclude non-priceable exchanges (y/N)", "n").strip().lower()
    out = str(REPORTS / "performance_analysis.html")
    cmd = [
        sys.executable, str(TV / "performance_analyzer.py"),
        "--min-samples", min_samples,
        "--out", out,
    ]
    if since:
        cmd += ["--since", since]
    if tradable_only == "y":
        cmd += ["--tradable-only"]
    run(cmd, cwd=TV)


def run_adaptive_threshold() -> None:
    print(header("Adaptive Threshold  — analyse actionable threshold vs win rate", BLUE))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Analyses alert_tracker.db to find the optimal composite score threshold
  • Shows win rate, trade count, avg return at each threshold level
  • Identifies the best threshold for maximising risk-adjusted returns

{BOLD}⚠️  NOTE:{RESET}
  Uses conviction_score (sell urgency 0-10) as proxy for composite_score
  (buy quality 0-100). This is a known metric mismatch — interpret with care.
""")
    cmd = [sys.executable, str(TV / "adaptive_threshold.py")]
    run(cmd, cwd=TV)


def run_download_fires() -> None:
    print(header("Download Fired Alerts from TradingView", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 New alert count since last download (BUY / SELL split)
  \u2022 Total fires in the local cache and latest alert timestamp
  \u2022 Top-5 most-repeated symbols (high conviction = fired many times)

{BOLD}Next action:{RESET}
  Run {CYAN}Signal Stack{RESET} (option 17) to rank and score the downloaded alerts.
  Fires older than 48 h decay in weight \u2014 run regularly for freshest signals.
""")
    run([sys.executable, str(TV / "tv_data" / "download_fires.py")], cwd=TV)

def run_combined_signals() -> None:
    """One-shot: Download TV alerts + Screener + News → Signal Stack (all sources in sequence)."""
    print(header("Combined Signals  — Download + Screener + News → Signal Stack", MAGENTA))

    au_live   = _is_port_in_use(11111)
    sg_live   = _is_port_in_use(11112)
    z_cached  = bool(_load_cached_token() or os.environ.get("ZERODHA_ACCESS_TOKEN", ""))
    moomoo_ok = au_live or sg_live

    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  Runs all 3 signal sources in sequence, then scores everything together:

  1. {CYAN}Download TV alert fires{RESET}   — polls TradingView for recent fired alerts
  2. {CYAN}TV Screener scan{RESET}           — multi-asset screener appends to fires pipeline
  3. {CYAN}Signal Stack with News{RESET}     — fetches TV news + scores ALL combined signals

{BOLD}Broker status (auto-detected):{RESET}
  Moomoo  {"✓ running" if moomoo_ok else f"{YELLOW}✗ offline — will be skipped{RESET}"}
  India   {"✓ token cached" if z_cached else f"{YELLOW}✗ no token — India enrichment skipped{RESET}"}

{DIM}Total time estimate: ~5–8 min (screener scan is the slowest step).{RESET}
""")

    n_fires       = prompt("Number of fires to analyse", "100")
    threshold     = prompt("Min score threshold (0–100)", "62.5")
    news_dir      = prompt("News direction: bullish / bearish / all", "all")
    skip_screener = prompt("Skip screener scan? (faster start, y/n)", "n").lower() == "y"

    # ── Step 1: Download TV alert fires ──────────────────────────────────
    print(f"\n{CYAN}{BOLD}[1/3]  Downloading TV alert fires…{RESET}")
    run([sys.executable, str(TV / "tv_data" / "download_fires.py")], cwd=TV)

    # ── Step 2 (optional): TV Screener scan (appends to fires_raw.json) ──
    if not skip_screener:
        print(f"\n{CYAN}{BOLD}[2/3]  Running TV multi-asset screener (--append-fires)…{RESET}")
        run([sys.executable, str(TV / "screeners" / "multi_asset_screener.py"),
             "--append-fires"], cwd=TV)
    else:
        print(f"\n{DIM}[2/3]  Screener scan skipped.{RESET}")

    # ── Step 3: Signal Stack — score all fires + news ─────────────────────
    print(f"\n{CYAN}{BOLD}[3/3]  Signal Stack — scoring all fires + {news_dir} news signals…{RESET}")
    try:
        n_val = int(n_fires)
    except ValueError:
        n_val = 100
    ss_cmd = [sys.executable, str(TV / "signal_stack.py"),
              "--n", str(n_val), "--threshold", str(threshold),
        "--news",      news_dir,
        "--no-external",   # skip slow YouTube/Smallcase for combined speed
    ]
    if not moomoo_ok:
        ss_cmd.append("--no-moomoo")
    if not z_cached:
        ss_cmd.append("--no-india")
    signal_env = _india_env(require_zerodha=False) if z_cached else BASE_ENV.copy()
    run(ss_cmd, cwd=TV, env=signal_env)

    # ── Summary ───────────────────────────────────────────────────────────
    # signal_stack.py names the report based on the --news flag value:
    #   --news all   → signal_report_actionable_news_all.html
    #   --news fires → signal_report_actionable_news_fires.html
    _nd = news_dir.strip().lower()
    report = REPORTS / f"signal_report_actionable_news_{_nd}.html"
    if report.exists():
        import time as _time
        age = _time.time() - report.stat().st_mtime
        if age < 300:
            print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} Combined signals scored — all 3 sources merged")
            print(f"  {BOLD}REPORT:{RESET}  {report}")
            print(f"  {DIM}Open in browser to review ranked actionable signals{RESET}")
        else:
            print(f"\n{YELLOW}⚠  Report exists but may be stale (modified {int(age)}s ago){RESET}")
    else:
        print(f"\n{YELLOW}⚠  No actionable report generated — check errors above{RESET}")


def _signal_stack_win_rates(
    db_path: Path | None = None,
    min_samples: int = 100,
) -> str:
    """Return latest-model 7-day source rates, or a transparent pending state."""
    import sqlite3 as _sq
    from database.queries import WIN_THRESHOLD_PCT

    _DB = db_path or (ROOT / "database" / "alert_tracker.db")
    _PENDING = f"  {YELLOW}Current model: collecting validated 7-day outcomes (minimum n={min_samples}){RESET}"
    _SOURCE_LABELS = {
        "webhook":  "News alerts",
        "download": "Download alerts",
        "screener": "Screener alerts",
        "manual":   "Manual alerts",
    }
    if not _DB.exists():
        return _PENDING
    try:
        con = _sq.connect(str(_DB))
        model_row = con.execute(
            "SELECT json_extract(parameters_json, '$.model_version') "
            "FROM signal_engine_outputs "
            "WHERE json_extract(parameters_json, '$.model_version') IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not model_row or not model_row[0]:
            con.close()
            return _PENDING
        model_version = model_row[0]
        rows = con.execute(
            "WITH candidates AS ("
            " SELECT seo.id, seo.input_source AS source, pe.return_pct,"
            " ROW_NUMBER() OVER (PARTITION BY seo.id"
            " ORDER BY ABS(pe.hours_elapsed - 168.0)) AS rn"
            " FROM signal_engine_outputs seo"
            " JOIN price_evolution pe ON pe.output_id = seo.id"
            " WHERE json_extract(seo.parameters_json, '$.model_version') = ?"
            " AND pe.hours_elapsed BETWEEN 144.0 AND 192.0"
            " AND pe.return_pct IS NOT NULL)"
            " SELECT source, COUNT(*) AS total,"
            " SUM(CASE WHEN return_pct >= ? THEN 1 ELSE 0 END) AS hits"
            " FROM candidates WHERE rn = 1 GROUP BY source",
            (model_version, WIN_THRESHOLD_PCT),
        ).fetchall()
        con.close()
    except Exception:
        return _PENDING

    total_observations = sum(row[1] for row in rows)
    if total_observations < min_samples:
        return (
            f"  {YELLOW}{model_version}: collecting validated 7-day outcomes "
            f"(n={total_observations}/{min_samples}){RESET}"
        )

    # Build rate map  {source: (rate, total)}
    rate_map: dict[str, tuple[float, int]] = {}
    for source, total, hits in rows:
        if total:
            rate_map[source] = (round((hits or 0) / total * 100, 1), total)

    lines = []
    for src in ["webhook", "download", "screener"]:
        label = _SOURCE_LABELS.get(src, src)
        if src in rate_map:
            rate, total = rate_map[src]
            color = GREEN if rate >= 15 else (YELLOW if rate >= 5 else RED)
            lines.append(
                f"  {color}{label:<18} {rate:>5.1f}% win rate{RESET}  "
                f"(n={total}, 7d return ≥{WIN_THRESHOLD_PCT:.0f}%)"
            )
        else:
            lines.append(f"  {DIM}{label:<18} pending{RESET}")

    return "\n".join(lines)


def _count_new_fires() -> int:
    """How many fires in fires_raw.json have never been through the pipeline.

    Returns -1 when the ledger or the fires file is unavailable, so callers can
    treat "unknown" as "proceed" rather than silently skipping the run.
    """
    try:
        from database.fires_ledger import unseen_fires

        fires_file = TV / "fires_raw.json"
        if not fires_file.exists():
            return -1
        with open(fires_file, encoding="utf-8") as fh:
            fires = json.load(fh).get("all", [])
        return len(unseen_fires(fires))
    except Exception as exc:
        print(f"{DIM}  (could not check the fires ledger: {exc}){RESET}")
        return -1


def _actionable_signal_count(companion: Path | None = None) -> int:
    """Signals in the actionable report just written by signal_stack.

    Returns -1 when the companion JSON is missing or unreadable, so callers can
    treat "unknown" as "proceed" rather than silently skipping downstream work.
    """
    try:
        companion = companion or (REPORTS / "actionable_report.json")
        if not companion.exists():
            return -1
        with open(companion, encoding="utf-8") as fh:
            return len(json.load(fh).get("actionable_signals", []))
    except Exception:
        return -1


def _validate_actionable_report(path: Path, expected_run_id: str) -> tuple[bool, str, int]:
    """Validate the immutable report used by option 14's automatic handoff."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False, "top-level JSON value is not an object", 0
        if data.get("run_id") != expected_run_id:
            return False, "run_id does not match this option-14 invocation", 0
        generated_at = data.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.strip():
            return False, "generated_at is missing", 0
        timestamp = _dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != _dt.timezone.utc.utcoffset(timestamp):
            return False, "generated_at must be an explicit UTC timestamp", 0
        if data.get("status") != "complete":
            return False, "report status is not complete", 0
        if data.get("is_watch"):
            return False, "report is WATCH-only", 0
        signals = data.get("actionable_signals")
        if not isinstance(signals, list):
            return False, "actionable_signals is not a list", 0
        if data.get("n_actionable") != len(signals):
            return False, "n_actionable does not match actionable_signals", 0
        for signal in signals:
            if not isinstance(signal, dict) or not signal.get("symbol"):
                return False, "an actionable candidate has no symbol", 0
            if signal.get("decision_score", signal.get("score")) is None:
                return False, "an actionable candidate has no score", 0
        return True, "valid", len(signals)
    except FileNotFoundError:
        return False, "actionable report is missing", 0
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return False, f"actionable report is malformed: {exc}", 0
    except OSError as exc:
        return False, f"actionable report is unreadable: {exc}", 0


def run_signal_stack() -> None:
    print(header("Signal Stack  — rank + score fired alerts", MAGENTA))
    option14_run_id = new_run_id()
    actionable_html = REPORTS / f"actionable_report_{option14_run_id}.html"
    actionable_json = actionable_html.with_suffix(".json")
    signal_html = REPORTS / f"signal_stack_report_{option14_run_id}.html"

    # Auto-detect broker availability
    au_live = _is_port_in_use(11111)
    sg_live = _is_port_in_use(11112)
    z_cached = bool(_load_cached_token() or os.environ.get("ZERODHA_ACCESS_TOKEN", ""))
    moomoo_ok = au_live or sg_live

    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
    • All signals ranked by quality × data confidence (signal age is shown, not scored)
    • Quality-gated candidates require L1≥6, data coverage≥55%, and no earnings within 14 days
    • Portfolio, expert, book, pool, and IBKR data are labeled as context, not hidden score boosts
    • Reproducible HTML + JSON at {REPORTS / 'signal_stack_report.html'}

{BOLD}Broker status (auto-detected):{RESET}
  Moomoo  {"✓ running" if moomoo_ok else f"{YELLOW}✗ offline — will be skipped{RESET}"}
  India   {"✓ token cached" if z_cached else f"{YELLOW}✗ no token — India enrichment skipped{RESET}"}
  IBKR L6 {DIM}off by default (needs TWS on port 7496){RESET}

{BOLD}Current-model outcome evidence:{RESET}
{_signal_stack_win_rates()}
  Signals are labeled by source in the report.
""")

    quick = prompt("[Enter] quick run (defaults) or (c)ustomize?", "").lower()
    customize = quick.startswith("c")

    if not customize:
        # Quick run with sensible defaults
        new_only = prompt("Scope: (n)ew alerts only, or [Enter] for the 50 most recent?",
                          "").lower().startswith("n")
        n_fires  = "50"
        threshold = "62.5"
        no_moomoo  = not moomoo_ok
        no_india   = not z_cached
        no_external = True   # skip slow YouTube/Smallcase by default
        use_ibkr   = False
        use_ai     = False
        use_edgar  = False
        sentiment_choice = "vader"  # VADER fast — Golden Rule #13 (FinBERT never the default)
        _scope_label = "new alerts only" if new_only else "top 50 fires"
        print(f"{DIM}  Running: {_scope_label}, threshold 62.5, external skipped, sentiment=vader{RESET}")
    else:
        new_only = prompt("Scope: (n)ew alerts only, or (r)ecent fires?", "r").lower().startswith("n")
        n_fires     = "50" if new_only else prompt("Number of recent unique fires to analyse", "50")
        threshold   = prompt("Min confluence score to include (0–100)", "62.5")
        no_moomoo   = prompt("Skip Moomoo enrichment? (y/n)", "n" if moomoo_ok else "y").lower() == "y"
        no_india    = prompt("Skip India enrichment? (y/n)", "n" if z_cached else "y").lower() == "y"
        use_ibkr    = prompt("Enable IBKR L6 enrichment? (needs TWS on port 7496) (y/n)", "n").lower() == "y"
        no_external = prompt(
            "Skip optional external context? (YouTube/Smallcase/Twitter; display-only) (y/n)",
            "y",
        ).lower() == "y"
        use_ai      = prompt("Enable AI commentary? (adds ~30s) (y/n)", "n").lower() == "y"
        use_edgar   = prompt("Include SEC EDGAR insider buys? (US stocks only, adds ~20s) (y/n)", "n").lower() == "y"
        _s = prompt("Sentiment backend: (v)ader fast, (f)inbert accurate, (b)oth dual", "v").lower().strip()
        sentiment_choice = {"v": "vader", "f": "finbert", "b": "both"}.get(_s[:1] if _s else "v", "vader")

    # Always download fresh fires from TradingView before scoring
    print(f"\n{CYAN}Downloading fresh fires from TradingView…{RESET}")
    download_rc = run([sys.executable, str(TV / "tv_data" / "download_fires.py")], cwd=TV)
    if download_rc != 0:
        print(f"{RED}F&O handoff stopped: fire download failed with exit code {download_rc}.{RESET}")
        return

    # ── Settle outcomes for alerts fired on earlier runs ─────────────────────
    # alert_tracker only snapshots alerts whose expires_at is still in the
    # future, so any alert that expires between runs loses its outcome forever.
    # This used to run only in the morning run; it belongs on the path that
    # actually gets used, before the --new-only early return below.
    print(f"\n{CYAN}Settling outcomes for previously fired alerts…{RESET}")
    print(f"{DIM}  Keeps the forward-test record current. Skips the HTML report for speed.{RESET}")
    settlement_rc = run([sys.executable, str(TV / "alert_tracker.py"), "--update", "--compute"], cwd=TV)
    if settlement_rc != 0:
        print(f"{RED}F&O handoff stopped: outcome settlement failed with exit code {settlement_rc}.{RESET}")
        return

    if new_only and _count_new_fires() == 0:
        print(f"\n{YELLOW}No new alerts since the last run — nothing to score.{RESET}")
        print(f"{DIM}  Re-run and choose a recent-fire count to re-analyse existing alerts.{RESET}")
        return

    try:
        n_val = int(n_fires)
    except ValueError:
        n_val = 50
    cmd = [sys.executable, str(TV / "signal_stack.py"),
           "--n", str(n_val), "--threshold", str(threshold),
           "--out", str(signal_html), "--actionable-out", str(actionable_html)]
    if new_only:
        cmd.append("--new-only")
    if no_moomoo:
        cmd.append("--no-moomoo")
    if no_india:
        cmd.append("--no-india")
    if use_ibkr:
        cmd.append("--ibkr")
    if no_external:
        cmd.append("--no-external")
    if use_ai:
        cmd.append("--ai")
    if use_edgar:
        cmd.append("--edgar")
    cmd.extend(["--sentiment", sentiment_choice])
    # Pass Zerodha token to subprocess when India enrichment is enabled
    signal_env = _india_env(require_zerodha=False) if not no_india else BASE_ENV.copy()
    signal_env["SIGNAL_RUN_ID"] = option14_run_id
    scoring_rc = run(cmd, cwd=TV, env=signal_env)
    if scoring_rc != 0:
        print(f"{RED}F&O handoff stopped: Signal Stack failed with exit code {scoring_rc}.{RESET}")
        return

    report_ok, report_reason, actionable_count = _validate_actionable_report(
        actionable_json, option14_run_id
    )
    if not report_ok:
        print(f"{RED}F&O handoff stopped: {report_reason}.{RESET}")
        return

    # ── Auto-run F&O options analysis on actionable results ──────────────────
    if actionable_count == 0:
        print(f"\n{YELLOW}No actionable signals — skipping the F&O options analysis.{RESET}")
    else:
        print(f"\n{CYAN}Auto-running F&O options analysis on actionable signals…{RESET}")
        print(f"{DIM}  (top 20 eligible quality-gated signals, all modes){RESET}")
        options_cmd = [
            sys.executable, str(TV / "options" / "run_options.py"),
            "--from-actionable",
            "--actionable-file", str(actionable_json),
            "--expected-run-id", option14_run_id,
            "--strict-actionable",
            "--all-modes",
            "--limit", "20",
            "--output", str(REPORTS / f"options_report_{option14_run_id}.html"),
        ]
        options_rc = run(options_cmd, cwd=TV)
        options_report = REPORTS / f"options_report_{option14_run_id}.html"
        if options_rc != 0:
            print(f"{RED}F&O options analysis failed with exit code {options_rc}.{RESET}")
        elif not options_report.exists():
            print(f"{RED}F&O options analysis failed: current run report was not created.{RESET}")
        else:
            print(f"{GREEN}{BOLD}✓ DONE:{RESET} F&O Options Analysis complete")
            print(f"  {BOLD}REPORT:{RESET}  {options_report}")
            print(f"  {DIM}Review strategies: SAFE / BALANCED / AGGRESSIVE{RESET}")

    # ── CLI Summary ──
    print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} Signal Stack scored and ranked alerts")
    print(f"  {BOLD}ACTIONABLE REPORT:{RESET}  {actionable_html}")
    print(f"  {DIM}Open in browser to review scored signals{RESET}")

def run_holdings_signals() -> None:
    print(header("Holdings Signal Scan  — which holdings would I buy more of", MAGENTA))

    au_live = _is_port_in_use(11111)
    sg_live = _is_port_in_use(11112)
    z_cached = bool(_load_cached_token() or os.environ.get("ZERODHA_ACCESS_TOKEN", ""))
    moomoo_ok = au_live or sg_live

    holdings_report = REPORTS / "signal_report_actionable_holdings.html"
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
    • Every position you hold, scored by the same L1–L8 pipeline as option 14
    • Leveraged ETFs are scored through their underlying (NVDL → NVDA)
    • Index and sector funds are flagged — equity fundamentals do not apply to them
    • Anything that cannot be mapped is listed as excluded, never neutral-scored
    • Reproducible HTML + JSON at {holdings_report}

{BOLD}Broker status (auto-detected):{RESET}
  Moomoo  {"✓ running" if moomoo_ok else f"{YELLOW}✗ offline — will be skipped{RESET}"}
  India   {"✓ token cached" if z_cached else f"{YELLOW}✗ no token — India enrichment skipped{RESET}"}
""")

    if not _ensure_fresh_positions():
        return

    threshold = "62.5"
    cmd = [sys.executable, str(TV / "signal_stack.py"),
           "--holdings", "--threshold", threshold]
    if not moomoo_ok:
        cmd.append("--no-moomoo")
    if not z_cached:
        cmd.append("--no-india")
    cmd.append("--no-external")   # skip slow YouTube/Smallcase context
    cmd.extend(["--sentiment", "vader"])   # Golden Rule #13 — VADER is the default

    signal_env = _india_env(require_zerodha=False) if z_cached else BASE_ENV.copy()
    import log_config as _log_config
    signal_env["SIGNAL_RUN_ID"] = _log_config.RUN_ID
    run(cmd, cwd=TV, env=signal_env)

    # ── Auto-run options analysis, scoped to this run's actionable holdings ──
    holdings_json = REPORTS / "signal_report_actionable_holdings.json"
    if _actionable_signal_count(holdings_json) == 0:
        print(f"\n{YELLOW}No holdings cleared the gate — skipping the options analysis.{RESET}")
    else:
        print(f"\n{CYAN}Auto-running options analysis on holdings worth adding to…{RESET}")
        run([sys.executable, str(TV / "options" / "run_options.py"),
             "--from-actionable",
             "--actionable-file", str(holdings_json),
             "--all-modes",
             "--limit", "20"], cwd=TV)

    if holdings_report.exists():
        import time as _time
        if _time.time() - holdings_report.stat().st_mtime < 120:
            print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} Holdings scored and ranked")
            print(f"  {BOLD}REPORT:{RESET}  {holdings_report}")
        else:
            print(f"\n{YELLOW}⚠ Report exists but may be stale{RESET}")
    else:
        print(f"\n{YELLOW}⚠ No report generated — check errors above{RESET}")


def _ensure_fresh_positions() -> bool:
    """Refresh the positions cache when it is older than the dashboard's TTL.

    Returns False when positions could not be made available, so the caller can
    stop rather than score a stale or empty book.
    """
    try:
        ensure_paths(TV)
        from holdings_resolver import POSITIONS_CACHE, cache_age_seconds
        from global_portfolio_dashboard import _POSITIONS_CACHE_TTL as ttl
    except ImportError as exc:
        print(f"{YELLOW}Cannot check positions cache ({exc}){RESET}")
        return False

    age = cache_age_seconds()
    if age is not None and age <= ttl:
        print(f"{DIM}  Positions cache is {age / 60:.0f} min old — using it.{RESET}")
        return True

    why = "missing" if age is None else f"{age / 3600:.1f}h old"
    print(f"{CYAN}Positions cache is {why} — refreshing from all brokers first…{RESET}")
    print(f"{DIM}  This runs the Global Portfolio Dashboard and can take a few minutes.{RESET}")
    run([sys.executable, str(ROOT / "global_portfolio_dashboard.py"), "--force-refresh"],
        cwd=ROOT, env=_india_env())

    if cache_age_seconds() is None and not Path(POSITIONS_CACHE).exists():
        print(f"\n{YELLOW}Positions are still unavailable — cannot score holdings.{RESET}")
        return False
    return True


def _is_port_in_use(port: int) -> bool:
    """Return True if something is already listening on localhost:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_webhook_server() -> None:
    print(header("Webhook Server  (port 8765 — receives live TV alerts)", MAGENTA))
    if _is_port_in_use(8765):
        print(f"{YELLOW}⚠  Webhook server is already running on port 8765.{RESET}")
        print(f"{DIM}   If you need to restart it, close the existing console first.{RESET}")
        return
    no_sizer = prompt("Disable position sizer? (y/n, disable if no price data)", "n").lower() == "y"
    no_ext   = prompt("Disable external enrichers? (YouTube/Smallcase/Twitter) (y/n)", "n").lower() == "y"
    cmd = [sys.executable, str(TV / "webhook_server.py")]
    if no_sizer:
        cmd.append("--no-sizer")
    if no_ext:
        cmd.append("--no-external")
    print(f"{CYAN}Launching webhook server in a new console window\u2026{RESET}\n")
    try:
        subprocess.Popen(
            cmd,
            env=BASE_ENV,
            cwd=str(TV),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print(f"{GREEN}Webhook server started in a new window.{RESET}")
        print(f"""
{BOLD}WEBHOOK SERVER STATUS{RESET}
  {BOLD}Status:{RESET}     Running (new console window)
  {BOLD}Endpoint:{RESET}   http://localhost:8765/webhook
  {BOLD}Health:{RESET}     http://localhost:8765/health
  {BOLD}Log file:{RESET}   {TV / 'wh_start.txt'} / wh_err.txt
  {BOLD}Test command:{RESET}
    Invoke-RestMethod -Uri http://localhost:8765/webhook -Method POST \\
      -ContentType 'application/json' \\
      -Body '{{"symbol":"NASDAQ:NVDA","score":75,"action":"BUY"}}'
  {BOLD}To stop:{RESET}    Close the new console window
  {DIM}This menu stays active while the server runs.{RESET}
""")
    except Exception as exc:
        print(f"{RED}Failed to open new console: {exc}\nFalling back to foreground mode\u2026{RESET}")
        run(cmd, cwd=TV)


def _bot_is_running(script_name: str) -> tuple:
    """Check whether a background bot process is still alive.

    Uses a PID file at ``ROOT/.<stem>.pid`` (e.g. ``.telegram_bot.pid``).
    Returns ``(is_running: bool, pid: int)`` — pid is 0 when not running.
    Cleans up a stale PID file automatically.
    """
    pid_file = ROOT / f".{script_name.replace('.py', '')}.pid"
    if not pid_file.exists():
        return False, 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False, 0
    try:
        os.kill(pid, 0)   # signal 0: check process existence without killing it
        return True, pid
    except OSError:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False, 0


def run_telegram_bot() -> None:
    running, pid = _bot_is_running("telegram_bot.py")
    if running:
        print(f"{YELLOW}Telegram bot is already running (PID {pid}). Only one instance allowed.{RESET}")
        return
    print(header("Telegram Bot  (@Ajaaz_bot \u2014 interactive alerts)", MAGENTA))
    print(f"""
{BOLD}Launches the Telegram bot in a background process.{RESET}
  \u2022 Responds to /status, /top, /alerts, /report Telegram commands
  \u2022 Forwards fired signal alerts to your Telegram chat in real-time
  \u2022 Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment

{YELLOW}Note:{RESET}  The bot runs continuously until you close the console.
""")
    go = prompt("Launch Telegram bot? (y/N)", "N").strip().lower()
    if go != "y":
        print(f"{DIM}Cancelled.{RESET}")
        return
    subprocess.Popen(
        [sys.executable, str(ROOT / "telegram_bot.py")],
        env=BASE_ENV,
        cwd=str(ROOT),
    )
    print(f"{GREEN}Telegram bot started.{RESET}")


def run_report_scheduler() -> None:
    running, pid = _bot_is_running("report_scheduler.py")
    if running:
        print(f"{YELLOW}Report Scheduler is already running (PID {pid}). Only one instance allowed.{RESET}")
        return
    print(header("Report Scheduler  \u2014 auto-push reports via Telegram", MAGENTA))
    print(f"""
{BOLD}Launches the report scheduler in a background process.{RESET}
  \u2022 Automatically pushes daily reports to Telegram at scheduled times
  \u2022 Runs morning screener + dashboard + alert stats on a cron-like schedule
  \u2022 Requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and scheduler_env.ps1

{YELLOW}Note:{RESET}  The scheduler runs continuously \u2014 close the console to stop.
""")
    go = prompt("Launch Report Scheduler? (y/N)", "N").strip().lower()
    if go != "y":
        print(f"{DIM}Cancelled.{RESET}")
        return
    subprocess.Popen(
        [sys.executable, str(ROOT / "report_scheduler.py")],
        env=BASE_ENV,
        cwd=str(ROOT),
    )
    print(f"{GREEN}Report Scheduler started.{RESET}")


def run_test_enrichers() -> None:
    print(header("Test Enrichers  \u2014 YouTube / Smallcase / Twitter signals", MAGENTA))
    print(f"""
{BOLD}Tests each external enricher against a symbol and prints the raw score.{RESET}
Useful for verifying credentials, API keys, and cookie freshness.

  {CYAN}youtube{RESET}    \u2014 search Akshat Shrivastava channel (member posts scored higher)
  {CYAN}smallcase{RESET}  \u2014 check if symbol is in your subscribed green-energy / super-business baskets
  {CYAN}twitter{RESET}    \u2014 check Kiyosaki / Nishant Kumar cashtag mentions
  {CYAN}sizer{RESET}      \u2014 run the backtest position sizer and show size_units recommendation
  {CYAN}all{RESET}        \u2014 run all four
""")
    source = prompt("Which enricher to test", "all")
    symbol = prompt("Symbol to test (TradingView format)", "NSE:RELIANCE")
    valid  = ("all", "youtube", "smallcase", "twitter", "sizer")
    if source not in valid:
        print(f"{RED}Unknown enricher '{source}'. Choose from: {', '.join(valid)}{RESET}")
        return
    run([
        sys.executable, str(TV / "webhook_server.py"),
        "--test-enrichers", source,
        "--test-symbol", symbol,
    ], cwd=TV)


def run_strategy_backtest() -> None:
    print(header("Strategy Backtest  — validate buy_sell_v2 parameters", MAGENTA))
    print(f"""
{BOLD}Runs the buy_sell_v2 strategy backtest for one or all symbols and shows{RESET}
position-sizing recommendations  (0 / 1 / 2 / 3 units) based on:
  • Win rate   ≥55%  and  Sharpe ≥0.5  → {GREEN}3 units — HIGH CONVICTION{RESET}
  • Win rate   ≥45%   or  Sharpe ≥0.2  → {YELLOW}2 units — MODERATE{RESET}
  • Below both thresholds              → {DIM}1 unit  — LOW CONVICTION{RESET}
  • Win rate   <35%   or  P&L  < −10%  → {RED}0 units — AVOID{RESET}

Results are cached for 7 days (backtest/backtest_cache.json).
""")
    sym = prompt("Symbol to test (e.g. NASDAQ:NVDA) or press Enter for ALL", "all")
    try:
        ensure_paths(TV)
        from backtest.backtest_sizer import run_backtest_menu
        run_backtest_menu(sym)
    except ImportError as exc:
        print(f"{RED}Could not import backtest_sizer: {exc}{RESET}")
    except Exception as exc:
        print(f"{RED}Backtest error: {exc}{RESET}")

def run_sell_strategy() -> None:
    print(header("Sell Strategy  — position exit analysis  (Moomoo + IBKR + India)", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Per-position sell urgency score (0–100) with factors: momentum, ATR, P&L %, days held
  • Broker exposure summary (Moomoo AU/SG, IBKR, Zerodha, Angel One) by currency
  • Top 5 EXIT candidates and any hard-stop breaches
  • Full HTML report at {REPORTS / 'sell_strategy_report.html'}

{YELLOW}Data quality note:{RESET}  Live mode fetches from ALL connected brokers.
  If OpenD is not running, Moomoo positions will be missing.
  If ZERODHA_ACCESS_TOKEN is unset, India holdings will be omitted.
  Use option 2 (positions.json snapshot) for a consistent offline run.

{BOLD}Options:{RESET}
  {CYAN}1{RESET}) Live from all brokers   {BOLD}(Moomoo + IBKR + India, recommended — always up to date){RESET}
  {CYAN}2{RESET}) Run with positions.json  (static snapshot)
  {CYAN}3{RESET}) Run with custom positions file
  {CYAN}4{RESET}) Run with built-in demo positions
""")
    choice = prompt("Enter 1 / 2 / 3 / 4", "1")
    script = str(TV / "sell" / "sell_strategy.py")
    if choice == "3":
        pos_file = prompt("Path to positions JSON")
        if not pos_file:
            print(f"{RED}File path is required.{RESET}")
            return
        run([sys.executable, script, "--positions", pos_file], cwd=TV)
    elif choice == "4":
        run([sys.executable, script, "--demo"], cwd=TV)
    elif choice == "2":
        run([sys.executable, script], cwd=TV)
    else:
        # Live mode fetches India positions — pass Zerodha token to subprocess
        sell_env = _india_env(require_zerodha=False)
        run([sys.executable, script, "--live"], cwd=TV, env=sell_env)

    # ── CLI Summary ──
    report = REPORTS / "sell_strategy_report.html"
    if report.exists():
        import time as _time
        age = _time.time() - report.stat().st_mtime
        if age < 120:
            print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} Sell Strategy — exit urgency scored")
            print(f"  {BOLD}REPORT:{RESET}  {report}")
            print(f"  {DIM}Review top EXIT candidates and hard-stop breaches{RESET}")
        else:
            print(f"\n{YELLOW}⚠ Report exists but may be stale (last modified {int(age)}s ago){RESET}")
    else:
        print(f"\n{YELLOW}⚠ No report generated — check errors above{RESET}")

def run_multi_asset_screener() -> None:
    print(header("Multi-Asset Screener  (US/AU/IN/HK/SG/JP/KR + crypto/forex/futures)", YELLOW))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Screener results across all asset classes with composite scores
  • Output: {REPORTS / 'multi_asset_report.html'}

{BOLD}Pipeline integration:{RESET}
  {CYAN}--append-fires{RESET}  Appends screener signals to fires_raw.json so signal_stack.py
                  can score them through the full 4-layer pipeline.
                  Use this to turn a screener run into a full analysis run.
""")
    feed = prompt("Feed results into signal pipeline? (appends to fires_raw.json)", "n").lower() == "y"
    cmd = [sys.executable, str(TV / "screeners" / "multi_asset_screener.py")]
    if feed:
        cmd.append("--append-fires")
    run(cmd, cwd=TV)


def run_sector_heatmap() -> None:
    print(header("TV Sector Heatmap  — Interactive Treemap", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Interactive plotly.js treemap of sectors/stocks
  • Tile size = Market Cap, Colour = Daily % Change
  • Output: {REPORTS / 'sector_heatmap.html'}
""")
    exchange = prompt("Exchange (america/australia/india/hongkong/singapore/japan/korea)", "america").strip().lower()
    n = prompt("Number of stocks to fetch", "300").strip()
    out = str(REPORTS / "sector_heatmap.html")
    cmd = [sys.executable, str(TV / "analysis" / "tv_sector_heatmap.py"),
           "--exchange", exchange, "--n", n, "--out", out]
    run(cmd, cwd=TV)

def run_prediction_engine() -> None:
    print(header("Prediction Engine — 7-day direction forecast", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 7-day direction forecast for specified symbols
  \u2022 Primary: Chronos-Bolt-Tiny (9M params, pretrained)
  \u2022 Fallback: Momentum ensemble (RSI + SMA + returns)
  \u2022 Output: {REPORTS / 'prediction_report.html'}
""")
    symbols = prompt("Symbols (comma-separated)", "AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,JNJ").strip()
    no_chronos = prompt("Skip Chronos model (momentum-only)? (y/n)", "n").strip().lower() == "y"
    out = str(REPORTS / "prediction_report.html")
    cmd = [sys.executable, str(TV / "analysis" / "prediction_engine.py"),
           "--symbols", symbols, "--out", out]
    if no_chronos:
        cmd.append("--no-chronos")
    run(cmd, cwd=TV)

def run_forex_screener() -> None:
    print(header("Forex Signal Report  — L1-L4 scored forex pairs", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 15 major forex pairs scored 0-100 (L1 Tech + L2 Macro/Carry + L3 Sentiment + L4 Consensus)
  \u2022 BUY \u2265 65  |  SELL \u2264 35  |  HOLD 35-65
  \u2022 Carry trade scoring via interest rate differentials
  \u2022 Output: {REPORTS / 'forex_signal_report.html'}
""")
    pairs_input = prompt("Custom pairs (comma-separated, e.g. EURUSD,GBPUSD) or blank for defaults", "").strip()
    out = str(REPORTS / "forex_signal_report.html")
    cmd = [sys.executable, str(TV / "forex" / "forex_signal_engine.py"), "--out", out]
    if pairs_input:
        cmd.extend(["--pairs", pairs_input])
    run(cmd, cwd=TV)


def run_morning_screener_us() -> None:
    print(header("Morning Screener  — US market", YELLOW))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 Top US (NASDAQ + NYSE) stocks by momentum score
  \u2022 Filters: RSI 50\u201375, ADX \u2265 20, price above 200 EMA
  \u2022 Results saved to {CYAN}screener_pool.json{RESET}

{BOLD}Pipeline integration:{RESET}
  Any symbol in the pool gets a {BOLD}+3 composite boost{RESET} + {CYAN}Pre-Screened \u2713{RESET} badge
  in Signal Stack (option 17) whenever a fire arrives for that symbol.
  Run this first thing each morning before checking option 17.
""")
    run([sys.executable,
         str(TV / "screeners" / "morning_screener.py"),
         "--market", "US"], cwd=TV)

    # ── CLI Summary ──
    pool = TV / "screener_pool.json"
    if pool.exists():
        import time as _time
        age = _time.time() - pool.stat().st_mtime
        if age < 120:
            import json as _json
            try:
                data = _json.loads(pool.read_text(encoding="utf-8"))
                n = len(data) if isinstance(data, list) else len(data.get("symbols", []))
            except Exception:
                n = "?"
            print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} Morning Screener — US market")
            print(f"  {BOLD}POOL:{RESET}    {pool}  ({n} symbols)")
            print(f"  {DIM}Run Signal Stack (option 17) to score these with +3 boost{RESET}")
        else:
            print(f"\n{YELLOW}⚠ Pool file stale (last modified {int(age)}s ago){RESET}")
    else:
        print(f"\n{YELLOW}⚠ No screener pool generated — check errors above{RESET}")

def run_morning_screener_hk() -> None:
    print(header("Morning Screener  — HK market", YELLOW))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 Top Hong Kong (HKEX) stocks by momentum score
  \u2022 Filters: RSI 50\u201375, ADX \u2265 20, price above 200 EMA
  \u2022 Results saved to {CYAN}screener_pool.json{RESET}

{BOLD}Pipeline integration:{RESET}
  Any symbol in the pool gets a {BOLD}+3 composite boost{RESET} + {CYAN}Pre-Screened \u2713{RESET} badge
  in Signal Stack (option 17) whenever a fire arrives for that symbol.
""")
    run([sys.executable,
         str(TV / "screeners" / "morning_screener.py"),
         "--market", "HK"], cwd=TV)

def run_morning_screener_custom() -> None:
    print(header("Morning Screener  — Custom market", YELLOW))
    print(f"""
{BOLD}Pipeline integration:{RESET}
  Writes {CYAN}screener_pool.json{RESET} → signal_stack reads it for {BOLD}+3 boost{RESET} + {CYAN}Pre-Screened \u2713{RESET} badge.
""")
    market = prompt("Market code (US / HK / SH / SZ)", "US")
    limit  = prompt("Max symbols to fetch", "100")
    cmd = [sys.executable,
           str(TV / "screeners" / "morning_screener.py"),
           "--market", market,
           "--limit", limit]
    run(cmd, cwd=TV)


def run_austsuper_screener() -> None:
    print(header("AustralianSuper Screener  — Member Direct ASX 300", YELLOW))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans the AustralianSuper Member Direct ASX 300 share universe
  • Applies technical signal filters (RSI, ADX, SMA200)
  • Optionally feeds BUY signals into the L1-L8 pipeline
""")
    direction = prompt("Direction (bullish / bearish)", "bullish").strip().lower() or "bullish"
    cmd = [sys.executable,
           str(TV / "screeners" / "austsuper_screener.py"),
           "--no-open"]
    if direction == "bearish":
        cmd.append("--bearish")
    run(cmd, cwd=TV)


def run_us_market_screener() -> None:
    print(header("US Market Screener  — S&P 500 universe", YELLOW))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans top ~500 US stocks by market cap (S&P 500 proxy)
  • Applies technical signal filters (RSI, ADX, SMA200)
  • Optionally feeds BUY signals into the L1-L8 pipeline
""")
    direction = prompt("Direction (bullish / bearish)", "bullish").strip().lower() or "bullish"
    cmd = [sys.executable,
           str(TV / "screeners" / "market_screener.py"),
           "--market", "us",
           "--no-open"]
    if direction == "bearish":
        cmd.append("--bearish")
    run(cmd, cwd=TV)


def run_india_market_screener() -> None:
    print(header("India Market Screener  — Nifty 200 universe", YELLOW))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans top ~200 Indian stocks by market cap (Nifty 200 proxy)
  • Applies technical signal filters (RSI, ADX, SMA200)
  • Optionally feeds BUY signals into the L1-L8 pipeline
""")
    direction = prompt("Direction (bullish / bearish)", "bullish").strip().lower() or "bullish"
    cmd = [sys.executable,
           str(TV / "screeners" / "market_screener.py"),
           "--market", "ind",
           "--no-open"]
    if direction == "bearish":
        cmd.append("--bearish")
    run(cmd, cwd=TV)


def run_sg_market_screener() -> None:
    print(header("Singapore Market Screener  — SGX universe", YELLOW))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans top SGX stocks by market cap
  • Applies technical signal filters (RSI, ADX, SMA200)
  • Optionally feeds BUY signals into the L1-L8 pipeline
""")
    direction = prompt("Direction (bullish / bearish)", "bullish").strip().lower() or "bullish"
    cmd = [sys.executable,
           str(TV / "screeners" / "market_screener.py"),
           "--market", "sg",
           "--no-open"]
    if direction == "bearish":
        cmd.append("--bearish")
    run(cmd, cwd=TV)


# ─── Manual Analysis & Alert Tracker handlers ─────────────────────────────────

def run_manual_analyse() -> None:
    print(header("Analyse Tickers Now  — manual symbol list  (one-shot)", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • One-shot analysis for the symbols you enter — no server needed
  • Runs the full signal pipeline: scoring, enrichment, HTML output
  • Result written to {REPORTS / 'actionable_report.html'}

{BOLD}Format:{RESET}  Comma-separated TradingView symbols, e.g.
  NASDAQ:NVDA, NYSE:AAPL, NSE:INFY, oracle, TSLA
  Short names (oracle, TSLA) are resolved via the built-in TICKER_ALIASES.
""")
    tickers = prompt("Tickers to analyse (comma-separated)").strip()
    if not tickers:
        print(f"{YELLOW}No tickers entered — cancelled.{RESET}")
        return
    no_sizer = prompt("Disable position sizer? (y/n)", "n").lower() == "y"
    no_ext   = prompt("Disable external enrichers? (y/n)", "n").lower() == "y"
    cmd = [sys.executable, str(TV / "webhook_server.py"), "--analyse", tickers]
    if no_sizer:
        cmd.append("--no-sizer")
    if no_ext:
        cmd.append("--no-external")
    run(cmd, cwd=TV)


def run_screener_run() -> None:
    print(header("Run TV Screener Scan  — fetch screener signals  (one-shot)", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Signals fetched from the TradingView screener you specify
  • Each result runs through the full scoring + enrichment pipeline
  • Result written to {REPORTS / 'signal_stack_report_screener.html'}

{BOLD}Available screener IDs:{RESET}
  {CYAN}stocks_us{RESET}   — US large-cap equities  (NASDAQ + NYSE)
  {CYAN}stocks_in{RESET}   — India equities  (NSE + BSE)
  {CYAN}stocks_au{RESET}   — Australia equities (ASX)
  {CYAN}forex{RESET}       — Major FX pairs
  Multiple IDs can be comma-separated, e.g. stocks_us,stocks_in
""")
    screener_id = prompt("Screener ID(s)", "stocks_us").strip()
    if not screener_id:
        print(f"{YELLOW}No screener ID entered — cancelled.{RESET}")
        return
    print(f"{DIM}  Using predefined BULLISH filters: RSI 50–75, ADX ≥ 20{RESET}")
    cmd = [sys.executable, str(TV / "signal_stack.py"), "--screener", screener_id, "--n", "200"]
    run(cmd, cwd=TV)


def run_alert_tracker_update() -> None:
    print(header("Alert Tracker  — Update Live Prices", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Fetches the current price for every open alert in the tracker DB
  • Computes unrealised P&L for all open positions
  • Updates {ROOT / 'database' / 'alert_tracker.db'}  (does NOT regenerate the HTML report)

{BOLD}Run this periodically{RESET} during market hours to keep P&L figures fresh.
After updating, run {CYAN}36) Alert Performance Report{RESET} to refresh the HTML output.
""")
    run([sys.executable, str(TV / "alert_tracker.py"), "--update"], cwd=TV)


def run_alert_tracker_report() -> None:
    print(header("Alert Tracker  — Performance Report", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Win rate, average gain/loss, Sharpe-like ratio per signal source
  • Open/closed alert breakdown with P&L % and holding period
  • Top performers and worst performers
  • Full HTML report at {REPORTS / 'alert_performance_report.html'}

{BOLD}Tip:{RESET}  Run {CYAN}35) Update Prices{RESET} first to ensure figures are current.
""")
    run([sys.executable, str(TV / "alert_tracker.py"), "--report"], cwd=TV)


def run_alert_tracker_stats() -> None:
    print(header("Alert Tracker  — Stats Summary", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Compact table: total alerts, open / closed count, overall win rate
  • Best-performing source (webhook / screener / manual)
  • Average P&L and largest single win/loss
  Printed directly to console — no HTML generated.
""")
    run([sys.executable, str(TV / "alert_tracker.py"), "--stats"], cwd=TV)


def run_alert_tracker_all() -> None:
    print(header("Alert Tracker  — Full Update + Report", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES  (equivalent to update → compute → report in one step):{RESET}
  1. Fetches live prices for all open alerts  (--update)
  2. Recomputes all performance metrics        (--compute)
  3. Generates the HTML performance report    (--report)
  Output: {REPORTS / 'alert_performance_report.html'}
""")
    run([sys.executable, str(TV / "alert_tracker.py"), "--all"], cwd=TV)


def run_signal_quality_monitor() -> None:
    """Idea 7: Rolling Signal Quality Monitor — 30d vs 90d win rate regime detection."""
    print(header("Signal Quality Monitor  — rolling win rate regime check", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • 30-day vs 90-day win rates per score bucket (APEX / HIGH / MED / LOW)
  • Delta column: negative delta ≥ 10 pts → DEGRADED warning
  • DEGRADED = signal model needs recalibration (regime shift detected)
  Pure SQL + Python read on alert_tracker.db — runs in < 10 ms.
""")
    db = str(ROOT / "database" / "alert_tracker.db")
    run([sys.executable, str(ROOT / "database" / "signal_quality_monitor.py"), "--db", db], cwd=ROOT)




def run_news_scan_bullish() -> None:
    print(header("TV News Scan  — Bullish Signals (BUY candidates)", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans TradingView news across quartr, Reuters, Dow Jones, MarketWatch, Invezz
  • Scores each article with sentiment analysis (VADER + FinBERT when available)
  • Extracts symbols with compound score ≥ +0.35 (bullish threshold)
  • Runs every candidate through the {BOLD}full 4-layer scoring pipeline{RESET}
    (L1 Technical + L2 Fundamental + L3 Sentiment + L4 Market Internals)
  • Output: {CYAN}signal_stack_report_news_bullish.html{RESET}  (dedicated file — does not overwrite other sources)
  Research basis: Tetlock 2007 — media tone predicts next-day stock returns.
""")
    print(f"{DIM}  Report: {REPORTS / 'signal_stack_report_news_bullish.html'}{RESET}")
    max_age   = prompt("Max article age (hours)", "24").strip() or "24"
    threshold = prompt("Sentiment compound threshold (bullish)", "0.35").strip() or "0.35"
    _sb = prompt("Sentiment backend: (v)ader fast, (f)inbert accurate, (b)oth dual", "v").lower().strip()
    sent_choice = {"f": "finbert", "b": "both"}.get(_sb[:1] if _sb else "v", "vader")
    cmd = [
        sys.executable, str(TV / "signal_stack.py"),
        "--news", "bullish",
        "--news-max-age", max_age,
        "--news-threshold", threshold,
        "--sentiment", sent_choice,
    ]
    run(cmd, cwd=TV)


def run_news_scan_bearish() -> None:
    print(header("TV News Scan  — Bearish Signals (PUT/short candidates)", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Scans TradingView news across quartr, Reuters, Dow Jones, MarketWatch, Invezz
  • Scores each article with sentiment analysis (VADER + FinBERT when available)
  • Extracts symbols with compound score ≤ -0.35 (bearish threshold)
  • Runs every candidate through the {BOLD}full 4-layer scoring pipeline{RESET}
    (L1 Technical + L2 Fundamental + L3 Sentiment + L4 Market Internals)
  • Output: {CYAN}signal_stack_report_news_bearish.html{RESET}  (dedicated file — does not overwrite other sources)
  Research basis: PEAD (Ball & Brown 1968) — negative news surprises predict
                  multi-day downside drift.
""")
    print(f"{DIM}  Report: {REPORTS / 'signal_stack_report_news_bearish.html'}{RESET}")
    max_age   = prompt("Max article age (hours)", "24").strip() or "24"
    threshold = prompt("Sentiment compound threshold (bearish magnitude)", "0.35").strip() or "0.35"
    _sb = prompt("Sentiment backend: (v)ader fast, (f)inbert accurate, (b)oth dual", "v").lower().strip()
    sent_choice = {"f": "finbert", "b": "both"}.get(_sb[:1] if _sb else "v", "vader")
    cmd = [
        sys.executable, str(TV / "signal_stack.py"),
        "--news", "bearish",
        "--news-max-age", max_age,
        "--news-threshold", threshold,
        "--sentiment", sent_choice,
    ]
    run(cmd, cwd=TV)


# ── Tests ──────────────────────────────────────────────────────────────────────
def _pytest(args: list[str], cwd: Path, suite_name: str = "") -> None:
    """Run pytest and show a compact ADMIN TEST SUMMARY at the end."""
    import re, io
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"] + args
    print(f"\n{DIM}$ {' '.join(str(c) for c in cmd)}{RESET}\n")
    try:
        result = subprocess.run(
            cmd, env=BASE_ENV, cwd=str(cwd),
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        output = result.stdout + result.stderr
        # Tee to log file if active
        if _current_log_path:
            LOGS.mkdir(exist_ok=True)
            with open(_current_log_path, "a", encoding="utf-8", errors="replace") as lf:
                ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lf.write(f"\n[{ts}] CMD: {' '.join(str(c) for c in cmd)}\n")
                lf.write("─" * 80 + "\n")
                lf.write(output)
                lf.write(f"\n[exit code: {result.returncode}]\n")
        # Show failures detail only (not the full verbose stream)
        lines = output.splitlines()
        in_failure = False
        failure_lines: list[str] = []
        for line in lines:
            if line.startswith("FAILED") or line.startswith("ERROR"):
                failure_lines.append(line)
            if "_ FAILED" in line or line.startswith("___"):
                in_failure = True
            if in_failure:
                failure_lines.append(line)
            if in_failure and line.strip() == "":
                in_failure = False

        # Parse summary line (last non-empty line)
        summary_line = ""
        for line in reversed(lines):
            if re.search(r"\d+ passed", line) or re.search(r"\d+ failed", line) or re.search(r"error", line, re.I):
                summary_line = line.strip()
                break

        passed  = int(m.group(1)) if (m := re.search(r"(\d+) passed",  summary_line)) else 0
        failed  = int(m.group(1)) if (m := re.search(r"(\d+) failed",  summary_line)) else 0
        skipped = int(m.group(1)) if (m := re.search(r"(\d+) skipped", summary_line)) else 0
        warns   = int(m.group(1)) if (m := re.search(r"(\d+) warning", summary_line)) else 0
        dur_m   = re.search(r"in ([\d.]+)s", summary_line)
        duration = dur_m.group(1) + "s" if dur_m else "?"

        overall = "PASS" if failed == 0 and result.returncode == 0 else "FAIL"
        col     = GREEN if overall == "PASS" else RED

        if failure_lines:
            print(f"{DIM}{'─' * 60}{RESET}")
            for fl in failure_lines[:40]:
                print(fl)
            print(f"{DIM}{'─' * 60}{RESET}\n")

        name = suite_name or " ".join(str(a) for a in args)[:50]
        print(f"""
{BOLD}{BLUE}ADMIN TEST SUMMARY{RESET}
  Suite:    {name}
  Result:   {col}{BOLD}{overall}{RESET}
  Passed:   {GREEN}{passed}{RESET}
  Failed:   {RED if failed else DIM}{failed}{RESET}
  Skipped:  {DIM}{skipped}{RESET}
  Warnings: {YELLOW if warns else DIM}{warns}{RESET}
  Duration: {duration}
""")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[interrupted]{RESET}")


def run_tests_signal_engine() -> None:
    print(header("Admin: Unit Tests  — signal_engine", BLUE))
    _pytest([str(TV / "signal_engine" / "tests")], cwd=TV, suite_name="Signal Engine")

def run_tests_cgt_moomoo() -> None:
    print(header("Admin: Unit Tests  — Moomoo CGT", BLUE))
    _pytest([str(CGT / "moomoo-cgt" / "test_cgt_analysis.py")], cwd=CGT, suite_name="Moomoo CGT")

def run_tests_integration_moomoo() -> None:
    print(header("Admin: Integration Tests  — Moomoo OpenD (server must be running)", BLUE))
    _pytest([str(CGT / "moomoo-cgt" / "test_integration_moomoo.py")], cwd=CGT, suite_name="Moomoo OpenD Integration")

def run_tests_integration_tv() -> None:
    print(header("Admin: Integration Tests  — TradingView", BLUE))
    _pytest([str(TV / "test_integration_tv.py")], cwd=TV, suite_name="TradingView API")

def run_tests_integration_external() -> None:
    print(header("Admin: Integration Tests  \u2014 External Enrichers", BLUE))
    print(f"{DIM}YouTube and Twitter tests make real network calls and are auto-skipped\n"
          f"when credentials / connectivity are unavailable.\n"
          f"Smallcase tests run locally from sc_baskets.json (no network needed).{RESET}\n")
    _pytest([str(TV / "test_integration_external.py")], cwd=TV, suite_name="External Enrichers")

def run_tests_all() -> None:
    print(header("Admin: All Tests", BLUE))
    _pytest([
        str(TV / "signal_engine" / "tests"),
        str(CGT / "moomoo-cgt" / "test_cgt_analysis.py"),
    ], cwd=ROOT, suite_name="All unit tests")

def run_tests_input_and_tracker() -> None:
    print(header("Admin: Unit Tests  — input_sources + alert_tracker", BLUE))
    _pytest([
        str(TV / "test_input_sources.py"),
        str(TV / "test_alert_tracker.py"),
    ], cwd=TV, suite_name="Input Sources + Alert Tracker")


def run_tests_news_screener() -> None:
    print(header("Admin: Unit Tests  — TV News Screener", BLUE))
    _pytest([
        str(TV / "test_tv_news_screener.py"),
    ], cwd=TV, suite_name="TV News Screener")

# ══════════════════════════════════════════════════════════════════════════════
# F&O Options Handlers
# ══════════════════════════════════════════════════════════════════════════════

def run_news_browser() -> None:
    print(header("TV News Scan  — All Directions (BUY + PUT candidates from TV news)", MAGENTA))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  \u2022 Scans TradingView news across quartr, Reuters, Dow Jones, MarketWatch, Invezz
  \u2022 Scores each article with sentiment analysis (VADER + FinBERT when available)
  \u2022 Extracts symbols above bullish OR below bearish threshold
  \u2022 Runs every candidate through the {BOLD}full 4-layer scoring pipeline{RESET}
    (L1 Technical + L2 Fundamental + L3 Sentiment + L4 Market Internals)
  \u2022 Output: dedicated file per direction — does not overwrite other sources
    {DIM}signal_stack_report_news_all.html / _news_bullish.html / _news_bearish.html{RESET}

{BOLD}Direction options:{RESET}
  {CYAN}1{RESET}) Both bullish + bearish  (BUY and PUT candidates combined)
  {CYAN}2{RESET}) Bullish only            (BUY candidates — same as menu 39)
  {CYAN}3{RESET}) Bearish only            (PUT/short candidates — same as menu 40)
""")
    direction_choice = prompt("Choose direction", "1")
    direction_map = {"1": "all", "2": "bullish", "3": "bearish"}
    direction = direction_map.get(direction_choice, "all")
    print(f"{DIM}  Report: {REPORTS / f'signal_stack_report_news_{direction}.html'}{RESET}")
    max_age   = prompt("Max article age (hours)", "24").strip() or "24"
    threshold = prompt("Sentiment compound threshold (magnitude)", "0.35").strip() or "0.35"
    _sb = prompt("Sentiment backend: (v)ader fast, (f)inbert accurate, (b)oth dual", "v").lower().strip()
    sent_choice = {"f": "finbert", "b": "both"}.get(_sb[:1] if _sb else "v", "vader")
    cmd = [
        sys.executable, str(TV / "signal_stack.py"),
        "--news", direction,
        "--news-max-age", max_age,
        "--news-threshold", threshold,
        "--sentiment", sent_choice,
    ]
    run(cmd, cwd=TV)


def run_moomoo_insights_scan() -> None:
    print(header("Moomoo Insights Scan  \u2014 community signals from Moomoo news feed", MAGENTA))
    print(f"\n{YELLOW}\u26a0  Moomoo Insights Scan is not yet implemented.{RESET}")
    print(f"   The --insights argument has not been added to signal_stack.py.")
    print(f"   This option is reserved for a future release.\n")


def run_trade_history_analysis() -> None:
    print(header("Trade History Deep Analysis  — FIFO P&L + Win Rate", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • All closed trades fetched from Moomoo OpenD (FIFO matched)
  • Per-trade win/loss breakdown with buy/sell dates and prices
  • Portfolio stats: Win Rate, Avg P&L %, Profit Factor, Sharpe-like Ratio
  • Top 5 winners and top 5 losers
  • Open-lot exposure summary
  • Full HTML report at {REPORTS / 'trade_history_report.html'}

{YELLOW}Requires:{RESET}  Moomoo OpenD running on port 11111 (AU) with trading permissions.
""")
    start     = prompt("Start date for history (YYYY-MM-DD)", "2020-01-01").strip()
    out       = str(REPORTS / "trade_history_report.html")
    cmd = [
        sys.executable,
        str(TV / "analysis" / "trade_history_analysis.py"),
        "--start", start,
        "--out", out,
    ]
    run(cmd, cwd=TV)


def run_fundamental_analysis() -> None:
    print(header("Fundamental Analysis  — PE/ROE/Growth/Health scores for fired alerts", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Last N unique symbols fetched from fires_raw.json (TradingView fired alerts)
  • Fundamental scoring: Valuation (P/E, PEG, P/S, P/B), Profitability (ROE, margins),
    Growth (revenue + EPS), Health (debt ratio, current ratio, free cashflow)
  • Composite rank: 60% fundamentals + 40% signal backtest quality
  • Full HTML report at {REPORTS / 'fundamental_report.html'}

{YELLOW}Requires:{RESET}  fires_raw.json in tradingview/ (run option 6/7 first to download fires)
          yfinance (pip install yfinance)
""")
    n_sym = prompt("Number of recent unique symbols to analyse", "20").strip()
    out   = str(REPORTS / "fundamental_report.html")
    cmd = [
        sys.executable,
        str(TV / "analysis" / "fundamental_analysis.py"),
        "--n", n_sym,
        "--out", out,
    ]
    run(cmd, cwd=TV)


def _options_preflight(require_opend: bool = False) -> bool:
    """
    Print pre-flight status for options analysis.
    Returns True if conditions are good enough to proceed, False if critical blocker.
    """
    import datetime as _dt

    opend_au = _is_port_in_use(11111)
    opend_sg = _is_port_in_use(11112)

    # ── Auto-start OpenD if offline ───────────────────────────────────────────
    if not (opend_au or opend_sg):
        ans = prompt("Moomoo OpenD is offline — auto-start it now? (y/n)", "y").lower()
        if ans == "y":
            try:
                _sys_path = str(ROOT)
                import sys as _sys_mod
                if _sys_path not in _sys_mod.path:
                    _sys_mod.path.insert(0, _sys_path)
                from moomoo_config import ensure_opend_running
                print(f"  {YELLOW}Starting OpenD — waiting up to 30s (complete any captcha in the new window)...{RESET}")
                ensure_opend_running(timeout=30)
                opend_au = _is_port_in_use(11111)
                opend_sg = _is_port_in_use(11112)
                if not (opend_au or opend_sg):
                    print(f"  {YELLOW}OpenD did not come up in 30s — continuing with yfinance fallback.{RESET}")
            except Exception as _exc:
                print(f"  {YELLOW}Auto-start error: {_exc}{RESET}")

    z_ok     = bool(_load_cached_token() or os.environ.get("ZERODHA_ACCESS_TOKEN", ""))

    # US market hours: Mon-Fri 09:30–16:00 ET = 23:30–06:00 UTC next day approx
    # Simple check using UTC; NYSE opens 14:30 UTC, closes 21:00 UTC (no DST adjust)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    us_open = (weekday < 5) and (14 <= now_utc.hour < 21)
    # India NSE: Mon-Fri 03:45–10:00 UTC
    nse_open = (weekday < 5) and (3 <= now_utc.hour < 10)

    print(f"""
{BOLD}Pre-flight check:{RESET}
  Moomoo OpenD (US options)  {"✓ running (port 11111)" if opend_au else f"{YELLOW}✗ offline — US Greeks will be stale (AB9019){RESET}"}
  Angel One (India options)  {"✓ token cached" if z_ok else f"{YELLOW}✗ no token — NSE F&O may fail{RESET}"}
  US market hours            {"✓ OPEN — live Greeks available" if us_open else f"{YELLOW}✗ CLOSED — after-hours Greeks may be stale{RESET}"}
  NSE market hours           {"✓ OPEN — live Greeks available" if nse_open else f"{YELLOW}✗ CLOSED — after-hours Greeks may be stale{RESET}"}
""")
    if require_opend and not opend_au:
        choice = prompt("OpenD is offline — US Greeks will be stale. Continue anyway? (y/n)", "n").lower()
        if choice != "y":
            print(f"{YELLOW}Cancelled.{RESET}")
            return False
    if not us_open and not nse_open:
        print(f"{YELLOW}  Tip: run during market hours (US: 09:30-16:00 ET, NSE: 09:15-15:30 IST) for live Greeks.{RESET}")
    return True


def run_options_from_actionable() -> None:
    print(header("F&O Options Analysis — from Actionable Signals", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Symbols pulled from actionable_log.json (last signal_stack or webhook run)
  • All 3 modes auto-analyse (SAFE / BALANCED / AGGRESSIVE) side-by-side
  • Blocked reasons shown if no tradable contract found (see below)

{YELLOW}Common blocked states:{RESET}
  AB9019   — option data unavailable (market closed or after-hours Greeks missing)
  AB9018   — symbol not eligible for F&O on this exchange
  LIQUIDITY — open interest / volume below filter threshold (widen with SAFE mode)
""")
    if not _options_preflight(require_opend=False):
        return
    limit = prompt("Max signals to analyse", "20")
    cmd   = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--from-actionable",
        "--all-modes",
        "--limit", limit,
    ]
    run(cmd, cwd=TV)

    # ── CLI Summary ──
    report = REPORTS / "options_report.html"
    if report.exists():
        import time as _time
        age = _time.time() - report.stat().st_mtime
        if age < 120:
            print(f"\n{GREEN}{BOLD}✓ DONE:{RESET} F&O Options Analysis complete")
            print(f"  {BOLD}REPORT:{RESET}  {report}")
            print(f"  {DIM}Review strategies: SAFE / BALANCED / AGGRESSIVE{RESET}")
        else:
            print(f"\n{YELLOW}⚠ Report exists but may be stale (last modified {int(age)}s ago){RESET}")
    else:
        print(f"\n{YELLOW}⚠ No options report generated — check errors above{RESET}")


def run_options_manual() -> None:
    print(header("F&O Options Analysis \u2014 Manual Symbol", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  \u2022 Option chain for the entered symbol at target score threshold
  \u2022 Recommended strategy: covered call / protective put / spread depending on mode
  \u2022 Max profit, breakeven, and probability-of-profit estimate

{YELLOW}If no contracts found:{RESET}  try SAFE mode, or confirm the symbol has listed options
  (e.g. NSE cash-only stocks like INFY do NOT have liquid weekly options)
""")
    sym = prompt("Symbol (e.g. NASDAQ:AAPL or NSE:INFY)")
    if not sym:
        print(f"{RED}Symbol required.{RESET}")
        return
    score     = prompt("Signal score (0-100)", "65")
    mode      = prompt("Strategy mode (SAFE / BALANCED / AGGRESSIVE)", "BALANCED")
    all_modes = prompt("Run all 3 modes side-by-side? (y/n)", "n").lower() == "y"
    env       = _india_env(require_zerodha=False)
    cmd       = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--symbol", sym,
        "--score", score,
        "--mode", mode.upper(),
    ]
    if all_modes:
        cmd.append("--all-modes")
    run(cmd, cwd=TV)


def run_put_options_from_actionable() -> None:
    print(header("PUT Options — from Actionable Bearish Signals", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • PUT contracts for bearish/overweight signals from the actionable report
  • Ranks by delta, IV and DTE with mode-appropriate filters
  • Use SAFE for protective hedges, AGGRESSIVE for directional bearish bets
""")
    if not _options_preflight(require_opend=False):
        return
    mode  = prompt("Strategy mode (SAFE / BALANCED / AGGRESSIVE)", "BALANCED")
    limit = prompt("Max signals to analyse", "20")
    cmd = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--from-actionable", "--puts",
        "--mode", mode.upper(),
        "--limit", limit,
    ]
    run(cmd, cwd=TV)


def run_put_options_from_bearish_screen() -> None:
    print(header("PUT Options — from Bearish Screen Candidates", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • PUT contracts for candidates from the multi-asset screener bearish list
  • Requires the multi-asset screener to have been run first
  • Suitable for systematic bearish/short-term hedge strategies
""")
    if not _options_preflight(require_opend=False):
        return
    mode  = prompt("Strategy mode (SAFE / BALANCED / AGGRESSIVE)", "BALANCED")
    limit = prompt("Max candidates to analyse", "20")
    cmd = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--from-bearish-screen", "--puts",
        "--mode", mode.upper(),
        "--limit", limit,
    ]
    run(cmd, cwd=TV)


def run_options_india() -> None:
    print(header("India NSE F&O Options — from Actionable Signals", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • NSE-only symbols from actionable report with F&O contracts available
  • Angel One TOTP auth is automatic — no manual token entry needed
  • Recommended strategy per symbol, premium, and risk/reward

{YELLOW}Note:{RESET}  NSE cash-segment stocks without F&O visibility will be skipped.
  Futures-only symbols (no weekly options) show futures-based strategy instead.
""")
    if not _options_preflight(require_opend=False):
        return
    mode = prompt("Strategy mode (SAFE / BALANCED / AGGRESSIVE)", "BALANCED")
    cmd = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--from-actionable",
        "--india-only",
        "--mode", mode.upper(),
    ]
    run(cmd, cwd=TV)


def _load_held_option_positions() -> list[dict]:
    """Load open option positions from alert_tracker.db options_recommendations table."""
    import sqlite3 as _sl
    db = ROOT / "database" / "alert_tracker.db"
    if not db.exists():
        return []
    try:
        conn = _sl.connect(db)
        conn.row_factory = _sl.Row
        cur = conn.execute(
            "SELECT * FROM options_recommendations WHERE outcome = 'OPEN' ORDER BY entry_date DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def run_option_position_manager() -> None:
    print(header("Option Position Manager — Strategy Advisor for Held Option Positions", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Strategy evaluation for an existing option position you are holding
  • Evaluates: HOLD, SELL, PARTIAL_CLOSE, PROTECTIVE_PUT, COLLAR,
    COVERED_CALL, ROLL_OUT, ROLL_UP, CASH_SECURED_PUT
  • Scenario payoffs (UP / FLAT / DOWN), timing guidance, ranked recommendations
  • Black-Scholes Greeks (delta, theta) solved from your premium data
  • No live broker connection required — pure analytical engine

{BOLD}USE CASE:{RESET}
  You hold GOOGL CALL 335 @ entry $1.50, current $8.00, underlying $338.
  The engine evaluates all strategies and tells you: best action, alternative,
  when to act, and what the payoff looks like under each scenario.
""")
    # ── Auto-load from alert_tracker.db ──────────────────────────────────────
    _pre: dict = {}
    held = _load_held_option_positions()
    if held:
        print(f"\n{BOLD}Auto-loaded {len(held)} open option position(s) from DB:{RESET}")
        for i, p in enumerate(held, 1):
            sym_p = p.get("tv_symbol") or p.get("symbol", "?")
            ot_p  = p.get("option_type", "?")
            st_p  = p.get("strike", "?")
            ex_p  = p.get("expiry_date") or p.get("expiry", "?")
            en_p  = p.get("entry_premium", "?")
            cu_p  = p.get("current_premium") or "–"
            print(f"  [{i}] {sym_p}  {ot_p}  strike={st_p}  exp={ex_p}  entry={en_p}  current={cu_p}")
        sel = prompt(f"Select position [1-{len(held)}] or press Enter to type manually", "").strip()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(held):
                _pre = held[idx]

    _d = _pre.get
    sym   = prompt("Symbol (e.g. NASDAQ:GOOGL)", _d("tv_symbol") or _d("symbol") or "NASDAQ:GOOGL").strip()
    otype = prompt("Option type (CALL / PUT)", _d("option_type") or "CALL").strip().upper()
    strike      = prompt("Strike price",             str(_d("strike")         or "335")).strip()
    expiry      = prompt("Expiry date (YYYY-MM-DD)", _d("expiry_date") or _d("expiry") or "2025-05-16").strip()
    entry_prem  = prompt("Entry premium (what you paid)",          str(_d("entry_premium")   or "1.5")).strip()
    curr_prem   = prompt("Current premium (current market price)", str(_d("current_premium") or "8.0")).strip()
    und_price   = prompt("Current underlying price", "338").strip()
    qty         = prompt("Quantity (contracts)",      str(_d("quantity")       or "1")).strip()
    cs          = prompt("Contract size (multiplier)", str(_d("contract_size") or "100")).strip()
    rsi_val     = prompt("RSI of underlying (blank = unknown)", "").strip()
    adx_val     = prompt("ADX of underlying (blank = unknown)", "").strip()
    vix_val     = prompt("VIX level (blank = unknown)", "").strip()

    pos = {
        "symbol":           sym,
        "option_type":      otype,
        "strike":           float(strike),
        "expiry":           expiry,
        "entry_premium":    float(entry_prem),
        "current_premium":  float(curr_prem),
        "underlying_price": float(und_price),
        "quantity":         int(qty) if qty.isdigit() else 1,
        "contract_size":    int(cs) if cs.isdigit() else 100,
    }
    if rsi_val:
        pos["rsi"] = float(rsi_val)
    if adx_val:
        pos["adx"] = float(adx_val)
    if vix_val:
        pos["vix"] = float(vix_val)

    cmd = [
        sys.executable,
        str(TV / "options" / "option_position_manager.py"),
        "--position", __import__("json").dumps(pos),
    ]
    run(cmd, cwd=TV)


def run_fo_chain_screener() -> None:
    print(header("F&O Chain Screener — options-first scanner with S1-S5 scoring", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Scans option chains across US (yfinance) and/or India (NSE) markets
  • Scores each contract 0-100: S1 IV Rank + S2 Direction + S3 Liquidity + S4 Time + S5 Greeks
  • Reports top contracts above threshold
  • Output: {REPORTS / 'fo_chain_report.html'}
""")
    market = prompt("Market (us / india / all)", "us").strip().lower()
    if market not in ("us", "india", "all"):
        market = "us"
    min_score = prompt("Minimum score threshold", "70").strip()
    n_top = prompt("Number of top US symbols to scan", "50").strip()
    symbols = prompt("Custom symbols (comma-separated, blank for auto)", "").strip()
    out = str(REPORTS / "fo_chain_report.html")
    cmd = [
        sys.executable, str(TV / "options" / "fo_chain_screener.py"),
        "--market", market, "--min-score", min_score,
        "--n-top", n_top, "--out", out,
    ]
    if symbols:
        cmd.extend(["--symbols", symbols])
    run(cmd, cwd=TV)


def run_options_outcome_checker() -> None:
    print(header("Options Outcome Checker — mark expired recs WIN / LOSS / EXPIRED", CYAN))
    print(f"""
{BOLD}WHAT THIS DOES:{RESET}
  • Finds all open options recommendations with expiry_date ≤ today
  • Fetches the underlying spot price {BOLD}at expiry{RESET} via yfinance history
  • Marks each rec: WIN (ITM) / LOSS (OTM) / EXPIRED (no price data)
  • WIN = underlying closed above strike (CALL) or below strike (PUT) on expiry

{BOLD}NOTE:{RESET} WIN/LOSS reflects direction accuracy, not P&L (does not account for premium paid).
""")
    dry = prompt("Dry run first? (y/n)", "y").strip().lower() == "y"
    cmd = [sys.executable, str(TV / "options" / "options_outcome_checker.py")]
    if dry:
        cmd.append("--dry-run")
    run(cmd, cwd=TV)


def run_options_us() -> None:
    print(header("US Options — from Actionable Signals  (Moomoo OpenD required)", CYAN))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • NASDAQ/NYSE symbols from actionable report with US option chains
  • Moomoo OpenD (AU, port 11111) provides live US option Greeks
  • Recommended strategy, expiry, strike and net debit/credit
""")
    if not _options_preflight(require_opend=True):
        return
    mode = prompt("Strategy mode (SAFE / BALANCED / AGGRESSIVE)", "BALANCED")
    cmd  = [
        sys.executable, str(TV / "options" / "run_options.py"),
        "--from-actionable",
        "--us-only",
        "--mode", mode.upper(),
    ]
    run(cmd, cwd=TV)


# ══════════════════════════════════════════════════════════════════════════════
# Morning Run — ALL reports in one shot
# ══════════════════════════════════════════════════════════════════════════════

def run_morning_all() -> None:
    """
    Morning Run — Generate ALL Reports  (one-shot full pipeline)
    ─────────────────────────────────────────────────────────────
    Authenticates Zerodha ONCE, then runs every reporting module in sequence:
      Portfolio → Signals → Sell Analysis → Alert Tracker → [Screener] → [CGT]
    No menus, no "Press Enter" prompts between steps.
    Interactive / destructive tools (GTT placement, order execution) are skipped.
    """
    print(header("☀  Morning Run  — Generate All Reports  (one-shot)", CYAN))

    # ── Pre-flight: broker status check ───────────────────────────────────
    from moomoo_config import is_opend_running
    au_live  = is_opend_running(11111)
    sg_live  = is_opend_running(11112)
    z_cached = bool(_load_cached_token() or os.environ.get("ZERODHA_ACCESS_TOKEN", ""))

    _bs = lambda ok, lbl: (
        f"{GREEN}● {lbl}{RESET}" if ok else f"{YELLOW}○ {lbl}  (offline — will be skipped){RESET}"
    )

    print(f"""
{BOLD}BROKER STATUS:{RESET}
  {_bs(au_live,   "Moomoo AU  (OpenD port 11111)")}
  {_bs(sg_live,   "Moomoo SG  (OpenD port 11112)")}
  {GREEN}● IBKR       (Flex Query — always available){RESET}
  {_bs(z_cached,  "Zerodha    (cached token check)")}
  {GREEN}● Angel One  (TOTP auto-auth){RESET}

{BOLD}REPORTS THAT WILL BE GENERATED:{RESET}
  {CYAN}Portfolio{RESET}
    • global_portfolio_dashboard.html   all brokers unified view           → reports/
    • pnl_attribution.html             top winners/losers by sector       → reports/
    • india_portfolio_dashboard.html    Zerodha + Angel One                → reports/
    • portfolio_score_history.html      daily L1-L4 score tracking         → reports/
    • portfolio_analytics.html          historical value chart             → reports/
    • portfolio_correlation.html        concentration risk matrix          → reports/
  {MAGENTA}Signals{RESET}
    • signal_stack_report.html          scored + ranked buy signals
    • actionable_report.html            buy signals above threshold
    • sell_strategy_report.html         exit urgency per open position
    • alert_performance_report.html     alert P&L analytics
  {CYAN}Performance & Analytics{RESET}
    • performance_analysis.html         signal quality analysis
    • backtest_vectorbt_report.html     score backtest validation
    • fundamental_report.html           fundamental scoring (top 20 fires)
  {CYAN}Intelligence{RESET}
    • prediction_report.html            7-day direction forecast
    • forex_signal_report.html          forex pair scoring
    • sector_heatmap.html               sector performance heatmap
    • economic_calendar.html            upcoming macro events
  {YELLOW}Screeners  (optional){RESET}
    • multi_asset_report.html           US/AU/IN + crypto/forex scan
  {GREEN}CGT  (optional){RESET}
    • combined_cgt_report.html          all-broker tax summary             → reports/cgt/
  {DIM}Meta{RESET}
    • report_analytics.html             cross-report freshness dashboard   → reports/

{DIM}GTT placement, order execution, and interactive tools are skipped.
TradingView fires are downloaded automatically before scoring.{RESET}
""")

    confirm = prompt("Start morning run? (y/n)", "y").lower()
    if confirm != "y":
        print(f"{YELLOW}Cancelled.{RESET}")
        return

    # ── Load or configure profile ─────────────────────────────────────────
    _profile_path = ROOT / "morning_run_profile.json"
    _saved_profile: dict | None = None
    if _profile_path.exists():
        try:
            _saved_profile = json.loads(_profile_path.read_text(encoding="utf-8"))
        except Exception:
            _saved_profile = None

    if _saved_profile:
        _scrn_s  = "y" if _saved_profile.get("include_screener", True) else "n"
        _cgt_s   = "y" if _saved_profile.get("include_cgt", True) else "n"
        _opts_s  = "y" if _saved_profile.get("include_options", False) else "n"
        _eta     = _saved_profile.get("est_minutes", 8)
        print(f"\n{BOLD}Saved profile:{RESET}  Screener={_scrn_s.upper()}  CGT={_cgt_s.upper()}  Options={_opts_s.upper()}  (~{_eta} min)")
        use_saved = prompt("[Enter] use saved profile, or (c)ustomize", "").lower()
        if not use_saved.startswith("c"):
            include_screener = _saved_profile.get("include_screener", True)
            include_cgt      = _saved_profile.get("include_cgt", True)
            include_options  = _saved_profile.get("include_options", False)
        else:
            include_screener = prompt("Include multi-asset screener? (~1 min, y/n)", "y").lower() == "y"
            include_cgt      = prompt("Include CGT reports? (~2 min, y/n)", "y").lower() == "y"
            include_options  = prompt("Include options analysis? (from actionable + hedger, ~3 min, y/n)", "n").lower() == "y"
    else:
        include_screener = prompt("Include multi-asset screener? (~1 min, y/n)", "y").lower() == "y"
        include_cgt      = prompt("Include CGT reports? (~2 min, y/n)", "y").lower() == "y"
        include_options  = prompt("Include options analysis? (from actionable + hedger, ~3 min, y/n)", "n").lower() == "y"

    # Save profile for next time
    _est = 11 + (1 if include_screener else 0) + (2 if include_cgt else 0) + (3 if include_options else 0)
    try:
        _profile_path.write_text(json.dumps({
            "include_screener": include_screener,
            "include_cgt":      include_cgt,
            "include_options":  include_options,
            "est_minutes":      _est,
        }, indent=2), encoding="utf-8")
    except Exception as exc:
        _log.debug("morning_profile_save_failed", error=str(exc))

    # ── Step counter ──────────────────────────────────────────────────────
    # 18 required steps + optional screener/CGT/options
    total_steps = 19 + (2 if include_screener else 0) + (1 if include_cgt else 0) + (2 if include_options else 0)
    step_num = [0]

    def _step(label: str) -> None:
        step_num[0] += 1
        bar = "─" * max(0, 58 - len(label))
        print(f"\n{CYAN}{BOLD}▶ [{step_num[0]}/{total_steps}]  {label}  {DIM}{bar}{RESET}")

    reports: list[tuple[str, Path]] = []   # (display-label, html path)

    # ── Auth: Zerodha once, reuse token for all following steps ───────────
    print(f"\n{CYAN}{BOLD}▶ [Auth]  Authenticating Zerodha…{RESET}")
    env = _india_env()
    z_ok = bool(env.get("ZERODHA_ACCESS_TOKEN"))
    if z_ok:
        # Verify the cached token is actually live (not just present in cache).
        # load_cache() only checks the age — it does NOT call the Zerodha API.
        # A token can be "from today" but already expired at the Zerodha server
        # (e.g. if authenticated before 06:05 IST and now it's past that time).
        # Calling validate_token() here prevents all subprocesses from hitting
        # TokenException and silently missing Zerodha data.
        _zh = _zerodha_helpers()
        if _zh.validate_token(silent=True):
            print(f"{GREEN}  Zerodha authenticated ✓{RESET}")
        else:
            print(f"{YELLOW}  Cached token stale — re-authenticating…{RESET}")
            _zh.invalidate_token()
            # Clear from our local env dict and os.environ so _india_env()
            # sees no token and triggers a fresh login.
            env.pop("ZERODHA_ACCESS_TOKEN", None)
            os.environ.pop("ZERODHA_ACCESS_TOKEN", None)
            env = _india_env()          # triggers run_login() → fresh token
            z_ok = bool(env.get("ZERODHA_ACCESS_TOKEN"))
            if z_ok:
                print(f"{GREEN}  Zerodha re-authenticated ✓{RESET}")
            else:
                print(f"{YELLOW}  Re-authentication failed — India data will be missing.{RESET}")
    else:
        print(f"{YELLOW}  No Zerodha token — India-specific data will be missing from some reports.{RESET}")

    # ── Step 1: Download fresh TradingView fires ──────────────────────────
    _step("Download fresh TradingView fires")
    run([sys.executable, str(TV / "tv_data" / "download_fires.py")], cwd=TV)

    # ── Step 2: Global Portfolio Dashboard ───────────────────────────────
    _step("Global Portfolio Dashboard  (all brokers)")
    gpd_cmd = [sys.executable, str(ROOT / "global_portfolio_dashboard.py")]
    run(gpd_cmd, cwd=ROOT, env=env)
    reports.append(("Global Portfolio", REPORTS / "global_portfolio_dashboard.html"))

    # ── Step 2b: P&L Attribution ─────────────────────────────────────────
    _step("P&L Attribution  (top winners/losers by sector & broker)")
    run([sys.executable, str(TV / "analysis" / "pnl_attribution.py"),
         "--out", str(REPORTS / "pnl_attribution.html")], cwd=ROOT)
    reports.append(("P&L Attribution", REPORTS / "pnl_attribution.html"))

    # ── Step 3: India Portfolio Dashboard ────────────────────────────────
    _step("India Portfolio Dashboard  (Zerodha + Angel One)")
    run([sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
        cwd=INDIA, env=env)
    reports.append(("India Portfolio", REPORTS / "india_portfolio_dashboard.html"))

    # ── Step 3a: Portfolio Score Tracker  (capture daily L1-L4 scores) ────
    _step("Portfolio Score Tracker  (capture + daily trend report)")
    run([sys.executable, str(TV / "portfolio_score_tracker.py"),
         "--capture",
         "--out", str(REPORTS / "portfolio_score_history.html")], cwd=TV, env=env)
    reports.append(("Score History", REPORTS / "portfolio_score_history.html"))

    # ── Step 3b: Portfolio Analytics  (historical value chart) ───────────
    _step("Portfolio Analytics  (historical value chart)")
    run([sys.executable, str(ROOT / "reports" / "portfolio_analytics.py"),
         "--no-open"], cwd=ROOT)
    reports.append(("Portfolio Analytics", REPORTS / "portfolio_analytics.html"))

    # ── Step 4: Signal Stack ──────────────────────────────────────────────
    _step("Signal Stack  (top 100 fires, threshold 62.5)")
    ss_cmd = [
        sys.executable, str(TV / "signal_stack.py"),
        "--n", "100", "--threshold", "62.5",
        "--no-external",    # skip YouTube/Smallcase/Twitter for morning speed
    ]
    if not au_live:
        ss_cmd.append("--no-moomoo")
    if not z_ok:
        ss_cmd.append("--no-india")
    run(ss_cmd, cwd=TV, env=env)
    reports.append(("Signal Stack",    REPORTS / "signal_stack_report.html"))
    reports.append(("Actionable",      REPORTS / "actionable_report.html"))

    # ── Step 5: Sell Strategy ─────────────────────────────────────────────
    _step("Sell Strategy  (live positions  — exit urgency scoring)")
    run([sys.executable, str(TV / "sell" / "sell_strategy.py"), "--live"],
        cwd=TV, env=env)
    reports.append(("Sell Strategy",   REPORTS / "sell_strategy_report.html"))

    # ── Step 6: Alert Tracker  (update live prices + generate HTML report)
    _step("Alert Tracker  (update prices + full performance report)")
    run([sys.executable, str(TV / "alert_tracker.py"), "--all"], cwd=TV)
    reports.append(("Alert Performance", REPORTS / "alert_performance_report.html"))

    # ── Step 6a: Performance Analyzer  (signal quality analysis) ────────
    _step("Performance Analyzer  (signal quality → data-driven improvements)")
    run([sys.executable, str(TV / "performance_analyzer.py"),
         "--out", str(REPORTS / "performance_analysis.html")], cwd=TV)
    reports.append(("Performance Analysis", REPORTS / "performance_analysis.html"))

    # ── Step 6b: Signal Quality Monitor  (rolling win-rate regime check) ─
    _step("Signal Quality Monitor  (30d vs 90d win-rate regime detection)")
    run([sys.executable, str(ROOT / "database" / "signal_quality_monitor.py"),
         "--db", str(ROOT / "database" / "alert_tracker.db"),
         "--out", str(REPORTS / "signal_quality_report.html")], cwd=ROOT)
    reports.append(("Signal Quality", REPORTS / "signal_quality_report.html"))

    # ── Step 6c: Backtest Vectorbt  (score backtest validation) ─────────
    _step("Signal Score Backtest  (vectorbt walk-forward validation)")
    run([sys.executable, str(TV / "backtest_vectorbt.py"),
         "--out", str(REPORTS / "backtest_vectorbt_report.html")], cwd=TV)
    reports.append(("Backtest Vectorbt", REPORTS / "backtest_vectorbt_report.html"))

    # ── Step 6c: Portfolio Correlation  (concentration risk matrix) ──────
    # NOTE: Runs AFTER signal/backtest steps to avoid blocking the critical trading output path.
    _step("Portfolio Correlation  (concentration risk detection)")
    run([sys.executable, str(TV / "analysis" / "portfolio_correlation.py"),
         "--out", str(REPORTS / "portfolio_correlation.html")], cwd=ROOT)
    reports.append(("Portfolio Correlation", REPORTS / "portfolio_correlation.html"))

    # ── Step 7: Sector Heatmap (market context) ──────────────────────────
    _step("Sector Heatmap  (all exchanges — market context)")
    run([sys.executable, str(TV / "analysis" / "tv_sector_heatmap.py"),
         "--exchange", "all",
         "--out", str(REPORTS / "sector_heatmap.html")], cwd=TV)
    reports.append(("Sector Heatmap", REPORTS / "sector_heatmap.html"))

    # ── Step 8: Economic Calendar (upcoming events) ──────────────────────
    _step("Economic Calendar  (upcoming macro events)")
    run([sys.executable, str(TV / "tv_data" / "tv_economic_calendar.py"),
         "--out", str(REPORTS / "economic_calendar.html")], cwd=TV)
    reports.append(("Economic Calendar", REPORTS / "economic_calendar.html"))

    # ── Step 8a: Fundamental Analysis  (top 20 fired symbols) ────────────
    _step("Fundamental Analysis  (PE/ROE/Growth scoring from fired alerts)")
    run([sys.executable, str(TV / "analysis" / "fundamental_analysis.py"),
         "--n", "20",
         "--out", str(REPORTS / "fundamental_report.html")], cwd=TV)
    reports.append(("Fundamentals", REPORTS / "fundamental_report.html"))

    # ── Step 8b: Prediction Engine  (7-day direction forecast) ───────────
    # --no-chronos: skip the 90-120s Chronos cold-start; use momentum fallback (~15s).
    # To run full Chronos model, use the dedicated Prediction Engine menu option.
    _step("Prediction Engine  (7-day direction forecast — momentum fallback, fast)")
    run([sys.executable, str(TV / "analysis" / "prediction_engine.py"),
         "--no-chronos",
         "--out", str(REPORTS / "prediction_report.html")], cwd=TV)
    reports.append(("Prediction", REPORTS / "prediction_report.html"))

    # ── Step 8c: Forex Signal Engine  (major pairs scoring) ──────────────
    _step("Forex Signal Engine  (15 major forex pairs L1-L4 scoring)")
    run([sys.executable, str(TV / "forex" / "forex_signal_engine.py"),
         "--out", str(REPORTS / "forex_signal_report.html")], cwd=TV)
    reports.append(("Forex Signals", REPORTS / "forex_signal_report.html"))

    # ── Step 9 [optional]: Multi-Asset Screener ──────────────────────────
    if include_screener:
        _step("Multi-Asset Screener  (US/AU/IN + crypto/forex/futures)")
        run([sys.executable, str(TV / "screeners" / "multi_asset_screener.py")],
            cwd=TV)
        reports.append(("Multi-Asset Screener", REPORTS / "multi_asset_report.html"))

    # ── Step 9b [optional]: Australian Super Screener ────────────────────
    if include_screener:
        _step("Australian Super Screener  (ASX quality stocks)")
        run([sys.executable, str(TV / "screeners" / "austsuper_screener.py")], cwd=TV)
        reports.append(("AU Super Screener", REPORTS / "austsuper_report.html"))

    # ── Step 10 [optional]: Combined CGT Report ───────────────────────────
    if include_cgt:
        _step("Combined CGT Report  (IBKR + Moomoo + India)")
        cgt_cmd = [sys.executable, str(CGT / "combined_cgt_report.py")]
        if not au_live and not sg_live:
            cgt_cmd.append("--no-moomoo")
        run(cgt_cmd, cwd=CGT, env=env)
        reports.append(("Combined CGT",  REPORTS / "cgt" / "combined_cgt_report.html"))

    # ── Steps 11+12+13 [optional]: Options Analysis ───────────────────────────
    if include_options:
        _step("Options from Actionable Signals  (F&O chain analysis)")
        run([sys.executable, str(TV / "options" / "run_options.py"),
             "--from-actionable"], cwd=TV, env=env)
        reports.append(("Options (actionable)", REPORTS / "options_report.html"))

        _step("Options Hedging Advisor  (global portfolio hedge recommendations)")
        run([sys.executable, str(TV / "options" / "options_hedger.py")],
            cwd=TV, env=env)
        reports.append(("Options Hedger", REPORTS / "options_hedger_report.html"))

        _step("Options Outcome Checker  (mark expired recs WIN/LOSS/EXPIRED)")
        run([sys.executable, str(TV / "options" / "options_outcome_checker.py")],
            cwd=TV, env=env)

    # ── Final: Report Analytics  (meta-dashboard — runs LAST) ────────────
    _step("Report Analytics  (cross-report freshness + trend dashboard)")
    run([sys.executable, str(ROOT / "reports" / "report_analytics.py"),
         "--no-open"], cwd=ROOT)
    reports.append(("Report Analytics", REPORTS / "report_analytics.html"))

    # ── Final Summary ─────────────────────────────────────────────────────
    bar = "═" * 62
    print(f"\n{GREEN}{BOLD}{bar}{RESET}")
    print(f"{GREEN}{BOLD}  ✅  Morning Run Complete!{RESET}")
    print(f"{GREEN}{BOLD}{bar}{RESET}\n")
    print(f"{BOLD}Reports generated  ({len(reports)} total):{RESET}\n")
    for label, path in reports:
        exists  = path.exists()
        icon    = f"{GREEN}✅{RESET}" if exists else f"{YELLOW}⚠ {RESET}"
        note    = "" if exists else f"  {DIM}(not found — that step may have failed){RESET}"
        print(f"  {icon}  {BOLD}{label:<24}{RESET}  {DIM}{path}{RESET}{note}")
    print()

    # ── Post-run: Telegram morning summary — staged, not sent immediately ──
    # staging=True queues locally; run `python tradingview/telegram_queue.py --flush`
    # to dispatch to Telegram after agent review.
    try:
        import sys as _sys
        _tq_dir = str(TV)
        if _tq_dir not in _sys.path:
            _sys.path.insert(0, _tq_dir)
        from telegram_alerts import send_morning_summary  # type: ignore
        _completed = [str(p) for _lbl, p in reports if p.exists()]
        _failed    = [str(p) for _lbl, p in reports if not p.exists()]
        send_morning_summary(reports=_completed, failures=_failed, staging=True)
    except Exception:
        pass  # Telegram not configured or module unavailable — skip silently

    # ── Show staged queue count ───────────────────────────────────────────
    try:
        import sys as _sys2
        _tq_dir2 = str(TV)
        if _tq_dir2 not in _sys2.path:
            _sys2.path.insert(0, _tq_dir2)
        from telegram_queue import count_pending  # type: ignore
        _n_staged = count_pending()
        if _n_staged > 0:
            print(f"\n{CYAN}{BOLD}📬  {_n_staged} message(s) staged in local Telegram queue{RESET}")
            print(f"{DIM}   Review:  python tradingview/telegram_queue.py --list{RESET}")
            print(f"{DIM}   Send:    python tradingview/telegram_queue.py --flush{RESET}")
            print(f"{DIM}   Discard: python tradingview/telegram_queue.py --clear{RESET}\n")
    except Exception:
        pass


def run_auto_trader_handler() -> None:
    print(header("Auto Trader — Intelligent Multi-Broker Order Engine", MAGENTA))
    print(f"""
{BOLD}WHAT YOU'LL SEE:{RESET}
  • Actionable signals loaded from actionable_log.json (last signal_stack run)
  • Position sizing: 2% risk per trade, ATR-based stops
  • Dry-run by default — no REAL orders placed unless --live is confirmed
  • Supported brokers: Zerodha (India), Moomoo (AU/SG/HK/US), IBKR

{YELLOW}⚠  LIVE mode places REAL orders with REAL money.  Use dry-run first.{RESET}
""")
    broker    = prompt("Broker (zerodha / moomoo / ibkr)", "zerodha").strip().lower()
    top       = prompt("Max signals to process", "10").strip()
    since_hrs = prompt("Look back N hours for signals", "24").strip()
    live_ans  = prompt("Live trading? REAL orders will be placed (y/n)", "n").strip().lower()
    out       = str(REPORTS / "auto_trader_orders.json")
    cmd = [
        sys.executable, str(TV / "auto_trader.py"),
        "--broker", broker,
        "--top", top,
        "--since-hours", since_hrs,
        "--out", out,
    ]
    if live_ans == "y":
        cmd.append("--live")
    run(cmd, cwd=TV)


# ══════════════════════════════════════════════════════════════════════════════
# Menu definition  (number, label, handler)
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Submenu helpers
# ══════════════════════════════════════════════════════════════════════════════

def _run_submenu(title: str, items: list, color: str = CYAN) -> None:
    """Display a numbered submenu and dispatch to the selected handler.
    items: list of (key, label, handler) tuples.
    """
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        bar = "─" * (len(title) + 4)
        print(f"\n{color}{BOLD}┌{bar}┐\n│  {title}  │\n└{bar}┘{RESET}\n")
        for key, label, _ in items:
            key_str = f"{key})".ljust(5)
            print(f"    {CYAN}{BOLD}{key_str}{RESET}  {label}")
        print(f"\n    {DIM}b)     Back to main menu{RESET}\n")
        try:
            choice = input(f"  {BOLD}Select: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            break
        if choice in ("b", "back", "q", ""):
            break
        handler = next((h for k, _l, h in items if k == choice), None)
        if handler:
            _set_log_path(choice, title.replace(" ", "_"))
            handler()
            pause()
        else:
            print(f"\n  {RED}Invalid option '{choice}'.{RESET}")
            import time; time.sleep(1)


def run_gtt_submenu() -> None:
    _run_submenu(
        "India GTT Risk Management  (Zerodha + Angel One)",
        [
            ("1", "GTT Audit               — find unprotected Zerodha positions",        run_india_gtt_audit),
            ("2", "GTT Suggest Levels      — Zerodha dry-run stop/target preview",       run_india_gtt_suggest),
            ("3", "GTT Place Stop-Losses   — protect unprotected Zerodha holdings \u26a0", run_india_gtt_place),
            ("4", "GTT List Active Orders  — show all Zerodha GTTs",                     run_india_gtt_list),
            ("5", "AO GTT Audit            — find unprotected Angel One positions",      run_india_ao_gtt_audit),
            ("6", "AO GTT Suggest Levels   — Angel One dry-run stop/target preview",     run_india_ao_gtt_suggest),
            ("7", "AO GTT Place Stop-Losses— protect unprotected Angel One holdings \u26a0", run_india_ao_gtt_place),
            ("8", "AO GTT List Active Rules— show all Angel One GTTs",                  run_india_ao_gtt_list),
        ],
        color=MAGENTA,
    )


def run_tests_submenu() -> None:
    _run_submenu(
        "Tests  (developer suite)",
        [
            ("t1",  "Unit tests        — signal_engine",                       run_tests_signal_engine),
            ("t2",  "Unit tests        — Moomoo CGT",                          run_tests_cgt_moomoo),
            ("t3",  "Integration tests — Moomoo OpenD",                        run_tests_integration_moomoo),
            ("t4",  "Integration tests — TradingView API",                     run_tests_integration_tv),
            ("t5",  "Integration tests — External enrichers",                  run_tests_integration_external),
            ("t6",  "All unit tests",                                           run_tests_all),
            ("t7",  "India Broker Tests — unit + integration test suite",      run_india_tests),
            ("t8",  "Unit tests — input_sources + alert_tracker",              run_tests_input_and_tracker),
            ("t9",  "Unit tests — TV news screener",                           run_tests_news_screener),
            ("t10", "Multi-Source Tests — all data source integration tests",  run_multi_source_test),
        ],
        color=BLUE,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Menu definition  (number, label, handler)
# ══════════════════════════════════════════════════════════════════════════════

MENU = [
    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 1: TRADER — Daily decision-making tools
    # ═══════════════════════════════════════════════════════════════════════
    ("section", "── TRADER  ──────────────────────────────────────────", CYAN),

    # ─── Morning shortcut ──────────────────────────────────────────────────
    ("section", "☀  Morning Shortcut  (run everything at once)", GREEN),
    ("0",  "Morning Run ★  — generate ALL reports in one shot  (portfolio + signals + CGT)", run_morning_all),

    # ─── Portfolio ─────────────────────────────────────────────────────────
    ("section", "Portfolio", CYAN),
    ("1",  "Global Portfolio Dashboard — all 6 brokers unified view",              run_global_dashboard),
    ("2",  "India Portfolio Dashboard  — Zerodha + Angel One",                     run_india_dashboard),
    ("3",  "Portfolio Analytics        — historical value chart + daily delta",    run_portfolio_analytics),
    ("4",  "Report Analytics           — cross-report trends + anomaly alerts",    run_report_analytics),
    ("4a", "P&L Attribution Report     — top winners/losers by sector & broker",   run_pnl_attribution),
    ("4b", "Portfolio Correlation Matrix — concentration risk heatmap (US positions, 90d returns)", run_portfolio_correlation),
    ("4c", "Holdings Snapshot Analysis  — position history + cost basis + P&L trend",              run_holdings_analysis),
    ("4d", "Portfolio Risk Optimizer    — HRP allocation weights (skfolio)",                       run_portfolio_risk_optimizer),

    # ─── India GTT Risk (submenu) ──────────────────────────────────────────
    ("section", "India GTT Risk Management  (Zerodha + Angel One)", MAGENTA),
    ("5",  "India GTT Risk Management  — Zerodha + Angel One  [submenu →]",        run_gtt_submenu),

    # ─── F&O Options ───────────────────────────────────────────────────────
    ("section", "F&O Options Analysis  (US + India NSE)", CYAN),
    ("6",   "Options from Actionable Signals  — all 3 modes auto-analyse",         run_options_from_actionable),
    ("6a",  "PUT Options from Actionable Signals  — bearish/overweight exits",     run_put_options_from_actionable),
    ("6b",  "PUT Options from Bearish Screen      — multi-asset screener cands",   run_put_options_from_bearish_screen),
    ("7",   "Options for single symbol        — choose mode + symbol",             run_options_manual),
    ("8",   "India NSE F&O only              — all actionable NSE stocks",         run_options_india),
    ("9",   "US Options only                 — all actionable US stocks",          run_options_us),
    ("10",  "Option Position Manager         — strategy advisor for held positions", run_option_position_manager),
    ("10a", "F&O Chain Screener             — options-first scanner (S1-S5 scoring)", run_fo_chain_screener),
    ("10b", "Options Outcome Checker        — mark expired recs WIN/LOSS/EXPIRED", run_options_outcome_checker),
    ("10c", "TV Pro Options Screener        — full chain via scan2 API (Greeks, IV, DTE)", run_tv_options_screener),
    ("10d", "US 300 Quality Options Scan   — top CALL per stock (legacy, fast scan)",  run_options_scan_us_quality),
    ("61",  "US 300 Quality Options Scan   — top CALL (TQS v2, 8-dim scoring)",        run_tqs_v2_calls),
    ("10e", "Options Rec Backtest Report   — win/loss analysis of US options recs",    run_options_rec_report),
    ("10f", "US 300 Quality Options Scan   — top PUT per stock (legacy, fast scan)",   run_options_scan_us_quality_puts),
    ("62",  "US 300 Quality Options Scan   — top PUT  (TQS v2, 8-dim scoring)",        run_tqs_v2_puts),
    ("63",  "TQS v2 Backtest Analyzer     — win/loss factor analysis",                  run_tqs_v2_backtest),
    ("64",  "ZeroDTE 0-2DTE Pattern Scanner — CALL + PUT signals (momentum + IV + VOI)", run_zero_dte),
    ("65",  "ZeroDTE Backtest             — win/loss analysis via vectorbt",              run_zerodte_backtest),
    ("10g", "Options Specialist Analysis   — independent theory-based scoring (PUT/CALL)", run_options_specialist_analysis),

    # ─── Portfolio Hedging & Arbitrage ─────────────────────────────────────
    ("section", "Portfolio Hedging & Arbitrage", CYAN),
    ("11",  "F&O / Forex Arbitrage Engine  — basis spread signals",                run_fo_arbitrage),
    ("12",  "Options Hedging Advisor        — global portfolio hedge recommendations", run_options_hedger),

    # ─── Buy Signals ───────────────────────────────────────────────────────
    ("section", "Buy Signals  (TradingView fired alerts)", MAGENTA),
    ("13", "Download fired alerts from TradingView",                               run_download_fires),
    ("14", "Signal Stack      — rank + score fired alerts",                        run_signal_stack),
    ("14a","Combined Signals  — download + screener + news → signal engine (one-shot)", run_combined_signals),
    ("14b","Holdings Signals  — score what I already own (buy-more candidates)",   run_holdings_signals),

    # ─── Sell Signals ──────────────────────────────────────────────────────
    ("section", "Sell Signals  (exit analysis)", MAGENTA),
    ("15", "Sell Strategy     — position exit analysis  (Moomoo + IBKR + India)",  run_sell_strategy),

    # ─── Screeners ─────────────────────────────────────────────────────────
    ("section", "Screeners", YELLOW),
    ("16", "Multi-Asset Screener (US/AU/IN/HK/SG/JP/KR + crypto/forex/futures)",   run_multi_asset_screener),
    ("17", "Morning Screener  — US market",                                        run_morning_screener_us),
    ("18", "Morning Screener  — HK market",                                        run_morning_screener_hk),
    ("19", "Morning Screener  — custom market",                                    run_morning_screener_custom),
    ("53", "AustralianSuper Screener — Member Direct ASX 300 scan",                 run_austsuper_screener),
    ("54", "US Market Screener    — S&P 500 universe scan",                        run_us_market_screener),
    ("55", "India Market Screener — Nifty 200 universe scan",                      run_india_market_screener),
    ("56", "SG Market Screener    — SGX universe scan",                            run_sg_market_screener),

    # ─── India Market Intelligence ─────────────────────────────────────────
    ("section", "India Market Intelligence", YELLOW),
    ("20", "India Market Internals  — PCR + OI Buildup + Market Data",             run_india_market_internals),
    ("21", "India Order Executor    — dry-run TV→Zerodha BUY test",                run_india_order_test),
    ("22", "India F&O Analyzer      — surface hidden options P&L",                 run_india_fo_analyzer),

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 2: TAX — CGT calculations and reports
    # ═══════════════════════════════════════════════════════════════════════
    ("section", "── TAX  ──────────────────────────────────────────────", GREEN),

    ("section", "CGT Analysis", GREEN),
    ("23", "Moomoo CGT (AU)   — live data via OpenD port 11111",                   run_moomoo_au_cgt),
    ("24", "Moomoo CGT (SG)   — live data via OpenD port 11112",                   run_moomoo_sg_cgt),
    ("25", "IBKR CGT          — Flex Query (MyFlexQuery) or manual CSV",           run_ibkr_cgt),
    ("26", "Webull CGT        — AU PDF statements or CSV (with live prices)",      run_webull_cgt),
    ("27", "Combined CGT      — IBKR + Moomoo + India merged report",              run_combined_cgt),
    ("27a","Tax-Loss Harvesting Advisor — harvest candidates + CGT discount cliff (AU FY deadline aware)", run_tax_harvest_advisor),

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 3: ADMIN — Tests, diagnostics, developer tools
    # ═══════════════════════════════════════════════════════════════════════
    ("section", "── ADMIN  ────────────────────────────────────────────", BLUE),

    ("section", "Diagnostics & Tools", BLUE),
    ("28", "Test Enrichers    — YouTube / Smallcase / Twitter",                    run_test_enrichers),
    ("29", "Strategy Backtest — validate buy_sell_v2 + position sizing",           run_strategy_backtest),
    ("30", "Webhook Server    — receive live TV alerts (port 8765)",               run_webhook_server),
    ("30a","Telegram Bot      — interactive alerts via @Ajaaz_bot",                run_telegram_bot),
    ("30b","Report Scheduler  — auto-push reports to Telegram on schedule",        run_report_scheduler),
    ("31", "Broker API Audit  — capability matrix + live probes",                  run_broker_api_audit),
    ("32", "Moomoo Market Tools — market state / SG futures / HK warrants / watchlist sync / intraday bars", run_moomoo_market_tools),
    ("33", "Auto Trader       — intelligent multi-broker order engine (dry-run / live)", run_auto_trader_handler),
    ("34", "Live Dashboard    — Streamlit real-time P&L (auto-refresh, filters)",  run_live_dashboard),
    ("35", "Signal Backtest   — vectorbt in-sample score event study",             run_vectorbt_backtest),
    ("35a","Signal Outcome Study — score vs forward return (audited outcome logic)", run_signal_outcome_study),
    ("36", "Sector Heatmap    — interactive treemap (sector × market cap × change)", run_sector_heatmap),
    ("37", "Forex Signal Report — L1-L4 scored forex pairs (carry trade + technicals)", run_forex_screener),
    ("38", "Prediction Engine  — 7-day direction forecast (Chronos + momentum)", run_prediction_engine),

    # ─── Signal Analysis & Alert Tracking ─────────────────────────────────
    ("section", "Signal Analysis  (manual tickers + alert performance)", MAGENTA),
    ("39", "Analyse tickers now        — manual symbol list  (one-shot)",          run_manual_analyse),
    ("40", "Run TV screener scan       — fetch screener signals  (one-shot)",      run_screener_run),
    ("41", "Alert tracker: update prices — refresh live P&L for open alerts",      run_alert_tracker_update),
    ("42", "Alert tracker: performance report — HTML analytics",                   run_alert_tracker_report),
    ("43", "Alert tracker: stats summary — compact P&L summary to console",        run_alert_tracker_stats),
    ("44", "Alert tracker: full run    — update + compute + report",               run_alert_tracker_all),
    ("44a","Signal Quality Monitor     — rolling 30d vs 90d win rate regime check", run_signal_quality_monitor),
    ("45", "News scan: bullish signals  — BUY candidates from TV news",            run_news_scan_bullish),
    ("46", "News scan: bearish signals  — PUT/short candidates from TV news",      run_news_scan_bearish),
    ("47", "News scan: all directions  — BUY + PUT candidates (both signals)",     run_news_browser),
    ("47b","Moomoo Insights Scan       — community signals from Moomoo news feed",  run_moomoo_insights_scan),
    ("48", "Trade History Analysis     — FIFO P&L, Win Rate, Best/Worst trades (Moomoo)", run_trade_history_analysis),
    ("49", "Fundamental Analysis       — PE/ROE/Growth scores for last N fired alerts",   run_fundamental_analysis),
    ("50", "Performance Analyzer      — data-driven signal improvement (L1-L4 vs win rate)", run_performance_analyzer),
    ("52", "Adaptive Threshold        — analyse actionable threshold vs win rate",         run_adaptive_threshold),

    # ─── Tests (submenu) ───────────────────────────────────────────────────
    ("section", "Tests  (developer)", BLUE),
    ("51", "Tests  — developer test suite  [submenu →]",                           run_tests_submenu),
]

# ══════════════════════════════════════════════════════════════════════════════
# Render menu
# ══════════════════════════════════════════════════════════════════════════════

def render_menu() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════════╗
║      Stocks Trading System — Main Menu               ║
║      [ TRADER · TAX · ADMIN ]                        ║
╚══════════════════════════════════════════════════════════╝{RESET}
""")
    for row in MENU:
        if row[0] == "section":
            _, title, color = row
            if title.startswith("──"):
                print(f"\n{color}{BOLD}{title}{RESET}")
            else:
                pad = " " * max(0, 54 - len(title))
                print(f"\n  {color}{BOLD}{title}{RESET}{DIM}{pad}{RESET}")
                print(f"  {DIM}{'─' * 54}{RESET}")
        else:
            num, label, _ = row
            num_str = f"{num})".ljust(5)
            print(f"    {CYAN}{BOLD}{num_str}{RESET}  {label}")

    print(f"\n  {DIM}{'─' * 54}{RESET}")
    print(f"    {RED}{BOLD}q){RESET}    Quit\n")


def build_dispatch() -> dict[str, callable]:
    return {row[0]: row[2] for row in MENU if row[0] != "section"}


def _set_log_path(choice: str, label: str = "") -> None:
    """Set the global log path for the current option run."""
    global _current_log_path
    LOGS.mkdir(exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = label.replace(" ", "_").replace("/", "_").replace("—", "").strip("_")[:40]
    _current_log_path = LOGS / f"{choice}_{safe}_{ts}.log"
    # Write header
    with open(_current_log_path, "w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"Option:  {choice}  {label}\n")
        lf.write(f"Started: {_dt.datetime.now().isoformat()}\n")
        lf.write("=" * 80 + "\n")


def main() -> None:
    global _current_log_path
    _log.info("app_startup", python=sys.version.split()[0], pid=os.getpid())
    dispatch = build_dispatch()
    while True:
        render_menu()
        try:
            choice = input(f"  {BOLD}Select option: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if choice in ("q", "quit", "exit"):
            break

        handler = dispatch.get(choice)
        if handler is None:
            print(f"\n  {RED}Invalid option '{choice}'. Try again.{RESET}")
            import time; time.sleep(1)
            continue

        # Find label for log file name
        label = next((row[1] for row in MENU if row[0] == choice), choice)
        _set_log_path(choice, label)

        # ── Correlation ID for this handler invocation ───────────
        rid = new_run_id()
        _log.info("menu_handler_start", option=choice, label=label, run_id=rid)

        handler()
        _log.info("menu_handler_done", option=choice, label=label, run_id=rid)
        print(f"\n{DIM}  📝 Log: {_current_log_path}{RESET}")
        _current_log_path = None
        pause()

    _log.info("app_shutdown")
    print(f"\n{CYAN}Goodbye!{RESET}\n")


if __name__ == "__main__":
    main()

