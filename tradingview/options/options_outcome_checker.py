"""
Options Outcome Checker
=======================
Marks open options recommendations as ITM_WIN / OTM_LOSS / EXPIRED based on
the underlying spot price **at** the expiry date (not current price).

ITM_WIN  = underlying closed above strike on expiry date (CALL ITM)
           or below strike on expiry date (PUT ITM)
OTM_LOSS = opposite direction
EXPIRED  = option expired worthless (OTM) or no price data

Uses yfinance history(start=expiry_date, end=expiry_date+4 days) to get
the correct historical closing price — NOT fast_info.last_price which
returns today's price and gives wrong outcomes for past expiry.

Table: options_recommendations
  - Primary key: rec_uuid (TEXT)
  - Symbol:      tv_symbol (e.g. "AAPL", "NASDAQ:AAPL")
  - Strike:      strike (REAL)
  - Expiry:      expiry (TEXT ISO "2026-05-01")
  - Outcome:     outcome starts as 'OPEN'; updated to ITM_WIN/OTM_LOSS/PREMIUM_WIN/EXPIRED

Usage:
    python options_outcome_checker.py            # mark all eligible recs
    python options_outcome_checker.py --dry-run  # preview without writing
"""

import argparse
import sys
import math
from pathlib import Path
from datetime import date, timedelta

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
TV   = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))

try:
    from path_config import ensure_paths
    ensure_paths(str(TV))
except ImportError:
    pass

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bare_symbol(tv_symbol: str) -> str:
    """Strip EXCHANGE: prefix for yfinance (NASDAQ:AAPL → AAPL)."""
    if ":" in tv_symbol:
        return tv_symbol.split(":", 1)[1]
    return tv_symbol


def _get_spot_at_expiry(tv_symbol: str, expiry: date) -> float | None:
    """
    Return the closing price of the underlying on *expiry* date (or nearest
    prior trading day if expiry falls on a weekend/holiday).

    Uses yfinance.history(start=expiry, end=expiry+4 days) then takes the
    FIRST bar (closest to expiry) — this correctly handles weekends and
    market holidays where there is no bar exactly on expiry.

    Returns None on any failure (network error, delisted, no history).
    """
    try:
        import yfinance as yf
        symbol    = _bare_symbol(tv_symbol)
        start_str = expiry.strftime("%Y-%m-%d")
        end_str   = (expiry + timedelta(days=4)).strftime("%Y-%m-%d")
        ticker    = yf.Ticker(symbol)
        hist      = ticker.history(start=start_str, end=end_str)
        if hist.empty:
            return None
        close = float(hist["Close"].iloc[0])
        if not math.isfinite(close) or close <= 0:
            return None
        return close
    except Exception as exc:
        print(f"  {DIM}[yfinance] {tv_symbol} @ {expiry}: {exc}{RESET}")
        return None


def _determine_outcome(opt_type: str, strike: float, spot: float) -> str:
    """
    ITM_WIN  — option expired in-the-money (direction correct)
    OTM_LOSS — option expired out-of-the-money (direction wrong)

    Note: uses strike-only comparison (ITM/OTM), NOT premium break-even.
    Tells you if the direction was right; does not compute actual P&L.
    """
    if not opt_type or not math.isfinite(strike):
        return "EXPIRED"
    ot = opt_type.upper()
    if ot == "CALL":
        return "ITM_WIN" if spot > strike else "OTM_LOSS"
    if ot == "PUT":
        return "ITM_WIN" if spot < strike else "OTM_LOSS"
    return "EXPIRED"


def _parse_expiry(s: str) -> "date | None":
    """
    Parse both ISO '2026-05-08' and CME-style '26MAY2026' / '30JUN2026' formats.

    SQLite string comparison fails for CME dates because '26MAY2026' sorts
    lexicographically after '2026-05-06' (second char '6' > '0'), so any
    WHERE expiry <= '<ISO date>' clause silently skips all CME-format rows.
    This helper is used to normalise expiry dates in Python after fetching
    ALL open recs, bypassing the SQLite lexicographic comparison bug.
    """
    from datetime import datetime as _dt
    if not s:
        return None
    s = s.strip().upper()
    # ISO format: YYYY-MM-DD
    try:
        return _dt.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    # CME full-year format: DDMMMYYYY  e.g. '26MAY2026', '30JUN2026'
    try:
        return _dt.strptime(s, "%d%b%Y").date()
    except ValueError:
        pass
    # CME 2-digit year: DDMMMYY  e.g. '26MAY26'
    try:
        return _dt.strptime(s, "%d%b%y").date()
    except ValueError:
        pass
    return None

# ── Premium fetcher (for OPEN recs) ─────────────────────────────────────────

def fetch_current_premium(
    tv_symbol: str, option_type: str, strike: float, expiry_str: str,
    _chain_cache: "dict | None" = None,
) -> "float | None":
    """
    Fetch the current option mid-price from yfinance option chain.
    Returns (bid+ask)/2 if both are available, else lastPrice, else None.

    Pass _chain_cache (a dict) to reuse already-fetched chains across calls.
    Key format: (bare_symbol, iso_expiry_str) → {"calls": df, "puts": df} or None.
    This reduces API calls from N-per-record to 1-per-(symbol, expiry) pair.
    """
    try:
        import yfinance as yf
        symbol   = _bare_symbol(tv_symbol)
        exp_date = _parse_expiry(expiry_str)
        if exp_date is None:
            return None
        iso_exp   = exp_date.strftime("%Y-%m-%d")
        cache_key = (symbol, iso_exp)

        if _chain_cache is not None and cache_key in _chain_cache:
            chain_data = _chain_cache[cache_key]
        else:
            ticker    = yf.Ticker(symbol)
            available = ticker.options
            if not available or iso_exp not in available:
                if _chain_cache is not None:
                    _chain_cache[cache_key] = None  # mark unavailable
                return None
            raw        = ticker.option_chain(iso_exp)
            chain_data = {"calls": raw.calls, "puts": raw.puts}
            if _chain_cache is not None:
                _chain_cache[cache_key] = chain_data

        if chain_data is None:
            return None

        df = chain_data["calls"] if (option_type or "").upper() == "CALL" else chain_data["puts"]
        if df is None or df.empty:
            return None
        df = df.copy()
        df["_dist"] = (df["strike"] - strike).abs()
        row = df.nsmallest(1, "_dist").iloc[0]
        bid  = float(row.get("bid", 0)  or 0)
        ask  = float(row.get("ask", 0)  or 0)
        last = float(row.get("lastPrice", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        if last > 0:
            return last
        return None
    except Exception as exc:
        print(f"  {DIM}[yfinance opt-chain] {tv_symbol} {option_type} K={strike}: {exc}{RESET}")
        return None


# Multiplier threshold for early PREMIUM_WIN detection (3× entry premium)
_PREMIUM_WIN_MULT: float = 3.0


def run_premium_update(dry_run: bool = False) -> dict:
    """
    Fetch and store the current mid-price for every OPEN option that has
    not yet reached its expiry date.  Updates current_premium and
    current_premium_at in the DB so the backtest report can show live P&L.

    Also detects PREMIUM_WIN early: if current_premium >= entry_premium * 3.0,
    the record is closed immediately with outcome='PREMIUM_WIN'.

    India/NSE options are skipped — yfinance F&O chains are not available
    for NSE/BSE instruments.

    Returns dict: {updated: int, premium_wins: int, skipped_india: int}
    """
    from alert_tracker import AlertTracker, DB_PATH
    import sqlite3

    today_date = date.today()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT rec_uuid, tv_symbol, option_type, strike, expiry, premium
            FROM   options_recommendations
            WHERE  outcome = 'OPEN'
              AND  expiry IS NOT NULL
            ORDER BY expiry
            """,
        ).fetchall()
    finally:
        conn.close()

    # Only update premiums for options not yet expired
    active = [
        row for row in rows
        if (exp := _parse_expiry(row["expiry"])) is not None and exp > today_date
    ]

    print(f"\n{BOLD}Premium Update — {len(active)} active (not-yet-expired) OPEN recs{RESET}")

    # Count unique (symbol, expiry) pairs for progress reporting
    import yfinance as yf
    _unique_pairs = len({(_bare_symbol(r["tv_symbol"]), r["expiry"])
                         for r in active
                         if r["tv_symbol"] and ":" in r["tv_symbol"]
                         and r["tv_symbol"].split(":", 1)[0].upper() not in ("NSE", "BSE")})
    print(f"{DIM}Will fetch ~{_unique_pairs} option chains for {len(active)} records "
          f"(cached per symbol+expiry){RESET}\n")

    tracker = AlertTracker(DB_PATH)
    updated = 0
    premium_wins = 0
    skipped_india = 0
    # Chain cache: (bare_symbol, iso_expiry) → {"calls": df, "puts": df} or None.
    # Reusing fetched chains reduces API calls from N-per-record to 1-per-(symbol,expiry).
    _chain_cache: dict = {}

    for row in active:
        symbol = row["tv_symbol"] or ""
        market = symbol.split(":", 1)[0].upper() if ":" in symbol else ""

        # India/NSE/BSE options: yfinance F&O chains are not available
        if market in ("NSE", "BSE"):
            print(
                f"  {DIM}[SKIP] {symbol:12s} {(row['option_type'] or ''):4s} "
                f"K={row['strike']}  India/NSE — use outcome_tracker.py{RESET}"
            )
            skipped_india += 1
            continue

        curr = fetch_current_premium(
            symbol,
            row["option_type"] or "CALL",
            float(row["strike"] or 0),
            row["expiry"],
            _chain_cache=_chain_cache,
        )
        entry = float(row["premium"] or 0)
        if curr is not None:
            pnl_pct = (curr - entry) / entry * 100 if entry else 0.0
            mult    = curr / entry if entry > 0 else 0.0
            target  = entry * _PREMIUM_WIN_MULT
            colour  = GREEN if pnl_pct >= 0 else RED
            arrow   = "\u25b2" if pnl_pct >= 0 else "\u25bc"
            pw_flag = f"  {GREEN}{BOLD}\U0001f3c6 PREMIUM_WIN{RESET}" if (entry > 0 and curr >= target) else ""
            print(
                f"  {colour}{arrow} {pnl_pct:+.1f}%{RESET}  "
                f"{symbol:12s} {(row['option_type'] or ''):4s} "
                f"K={row['strike']}  entry=${entry:.2f}  curr=${curr:.2f}  "
                f"({mult:.2f}\u00d7 / target {_PREMIUM_WIN_MULT:.1f}\u00d7){pw_flag}"
            )
            if not dry_run:
                try:
                    tracker.update_options_rec_current_premium(row["rec_uuid"], curr)
                    updated += 1
                    # PREMIUM_WIN early detection: close rec if curr >= 3× entry
                    if entry > 0 and curr >= target:
                        tracker.update_options_rec_outcome(
                            row["rec_uuid"],
                            "PREMIUM_WIN",
                            close_spot=None,
                            close_date=today_date.isoformat(),
                        )
                        premium_wins += 1
                except Exception as exc:
                    print(f"  {YELLOW}[WARN] {row['rec_uuid'][:8]}: {exc}{RESET}")
        else:
            print(
                f"  {DIM}{symbol:12s} {(row['option_type'] or ''):4s} "
                f"K={row['strike']}  no data in yfinance chain{RESET}"
            )

    print()
    if dry_run:
        print(f"{YELLOW}Dry-run — no changes written to DB.{RESET}")
    else:
        us_active = len(active) - skipped_india
        print(f"{GREEN}Updated current_premium for {updated}/{us_active} US records.{RESET}")
        if skipped_india:
            print(f"{DIM}Skipped {skipped_india} India/NSE records (use outcome_tracker.py).{RESET}")
        if premium_wins:
            print(f"{GREEN}{BOLD}PREMIUM_WIN detected: {premium_wins} record(s) closed!{RESET}")
    return {"updated": updated, "premium_wins": premium_wins, "skipped_india": skipped_india}

# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> int:
    """
    Scan options_recommendations for OPEN recs with expiry <= today.
    Fetch spot price at expiry via yfinance and mark WIN/LOSS/EXPIRED.
    Returns count of records updated.
    """
    from alert_tracker import AlertTracker, DB_PATH

    tracker = AlertTracker(DB_PATH)
    today   = date.today().isoformat()

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Fetch ALL open recs with a non-null expiry regardless of format.
        # We do NOT use "expiry <= ?" in SQL because SQLite lexicographic
        # comparison fails for CME-style dates ('26MAY2026' sorts AFTER
        # '2026-05-06' even though May 26 has not yet passed at that point).
        # _parse_expiry() normalises both ISO and CME formats in Python.
        rows = conn.execute(
            """
            SELECT rec_uuid, tv_symbol, option_type, strike, expiry
            FROM   options_recommendations
            WHERE  outcome = 'OPEN'
              AND  expiry IS NOT NULL
            ORDER BY expiry
            """,
        ).fetchall()
    finally:
        conn.close()

    today_date = date.today()

    # Filter to only recs whose expiry has passed (normalised in Python)
    eligible = []
    for row in rows:
        exp = _parse_expiry(row["expiry"])
        if exp is not None and exp <= today_date:
            eligible.append((row, exp))

    if not eligible:
        print(f"{DIM}No open options recommendations with past expiry dates found.{RESET}")
        return 0

    rows_to_process = eligible

    print(f"\n{BOLD}Options Outcome Checker{RESET}")
    print(f"Checking {len(eligible)} open recommendation(s) with expiry ≤ {today_date}\n")
    print(f"  {DIM}Note: WIN/LOSS = direction accuracy (ITM/OTM). Does not reflect premium P&L.{RESET}\n")

    updated = 0
    for row, expiry in rows_to_process:
        rec_uuid  = row["rec_uuid"]
        tv_symbol = row["tv_symbol"]
        opt_type  = row["option_type"] or "?"
        strike    = row["strike"]

        spot = _get_spot_at_expiry(tv_symbol, expiry)

        if spot is None:
            outcome    = "EXPIRED"
            close_spot = None
        else:
            outcome    = _determine_outcome(opt_type, strike or 0, spot)
            close_spot = spot

        colour = GREEN if outcome == "ITM_WIN" else (RED if outcome == "OTM_LOSS" else DIM)
        spot_s = f"{spot:.2f}" if spot else "N/A"
        print(f"  {colour}{outcome:7s}{RESET}  {tv_symbol:10s} {opt_type:4s} K={strike}  "
              f"exp:{expiry}  spot@exp={spot_s}")

        if not dry_run:
            try:
                tracker.update_options_rec_outcome(
                    rec_uuid,
                    outcome,
                    close_spot=close_spot,
                    close_date=expiry.isoformat(),
                )
                updated += 1
            except Exception as exc:
                print(f"  {YELLOW}  [WARN] Failed to update {rec_uuid[:8]}…: {exc}{RESET}")

    print()
    if dry_run:
        print(f"{YELLOW}Dry-run — no changes written to DB.{RESET}")
    else:
        print(f"{GREEN}Updated {updated}/{len(rows_to_process)} records.{RESET}")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mark expired options recommendations ITM_WIN / OTM_LOSS / EXPIRED; "
                    "optionally refresh current premiums for open recs"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview outcomes without writing to DB",
    )
    parser.add_argument(
        "--update-premiums", action="store_true",
        help="Fetch and store current option premiums for all OPEN (not yet expired) recs",
    )
    args = parser.parse_args()
    if args.update_premiums:
        run_premium_update(dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

