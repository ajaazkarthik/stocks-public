"""
India CGT / Holdings Data
==========================
Provides live holdings from Zerodha (Kite Connect) and Angel One (SmartAPI).

Unlike IBKR/Moomoo, India broker APIs do not expose full trade history —
only current DEMAT holdings are available as a CGT data source.
Tax treatment: STCG (<1 year) @ 15%, LTCG (>=1 year) @ 10% above ₹1L.

Usage:
    from india_cgt import get_data
    data = get_data()   # returns dict with "positions" list

    # standalone
    python india_cgt.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, date

# CF-13 (audit 2026-04-29): UTF-8 reconfigure for Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Credentials must be set via environment variables — no hardcoded defaults here.


def _strip_eq(s: str) -> str:
    return s[:-3] if s.upper().endswith("-EQ") else s


def _zerodha_holdings() -> list[dict]:
    """Return normalised Zerodha DEMAT holdings. Returns [] on error.

    Auth is delegated to zerodha_auth.get_kite() — the single source of
    truth for token resolution (cache → env → auto-login → retry-on-expiry).
    """
    from zerodha_auth import get_kite, invalidate_token

    _retried = False
    while True:
        try:
            kite = get_kite()  # silent=False — triggers auth when token is expired/missing
            raw  = kite.holdings() or []
            break  # success
        except EnvironmentError:
            print("  [India] Zerodha login skipped — holdings will be empty.")
            return []
        except Exception as e:
            err      = str(e)
            err_low  = err.lower()
            err_type = type(e).__name__
            # Mirror the precise auth-error check in global_portfolio_dashboard.py.
            # "Incorrect" alone is too broad — it matches API errors like
            # "Incorrect exchange or instrument" that are NOT token errors.
            is_auth_err = (
                "TokenException" in err_type
                or "PermissionException" in err_type
                or (
                    "Incorrect" in err
                    and ("api_key" in err_low or "access_token" in err_low)
                )
                or "insufficient permission" in err_low
                or "token expired" in err_low
            )
            if not _retried and is_auth_err:
                _retried = True
                print("  [India] Zerodha token expired. Re-authenticating...")
                invalidate_token()   # clears singleton + cache
                continue             # retry — get_kite() (silent=False) triggers fresh login
            else:
                print(f"  [India] Zerodha fetch error: {e}")
                return []

    out = []
    for h in raw:
        qty = int(h.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        sym = _strip_eq(h.get("tradingsymbol", "")).upper()
        avg = float(h.get("average_price", 0) or 0)
        ltp = float(h.get("last_price", 0) or 0)
        pnl = float(h.get("pnl", 0) or 0)
        out.append({
            "broker":        "Zerodha",
            "symbol":        f"NSE:{sym}",
            "exchange":      h.get("exchange", "NSE"),
            "qty":           qty,
            "avg_cost":      avg,
            "last_price":    ltp,
            "unrealized_pnl": pnl,
            "currency":      "INR",
        })
    return out


def _angelone_holdings() -> list[dict]:
    """Return normalised Angel One DEMAT holdings. Returns [] on error."""
    try:
        import pyotp  # type: ignore
        from SmartApi import SmartConnect  # type: ignore
        import logging as _l; _l.getLogger("logzero_default").setLevel(_l.CRITICAL)
        totp   = pyotp.TOTP(os.environ["ANGELONE_TOTP_SECRET"]).now()
        smart  = SmartConnect(api_key=os.environ["ANGELONE_API_KEY"])
        sess   = smart.generateSession(
            os.environ["ANGELONE_CLIENT_CODE"],
            os.environ["ANGELONE_PIN"], totp)
        if not sess.get("status"):
            print(f"  [India] Angel One session failed: {sess}")
            return []
        raw = (smart.holding().get("data") or [])
    except Exception as e:
        print(f"  [India] Angel One fetch error: {e}")
        return []

    out = []
    for h in raw:
        qty = int(h.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        sym = _strip_eq(
            h.get("tradingsymbol") or h.get("symbolname") or ""
        ).upper()
        avg = float(h.get("averageprice", 0) or 0)
        ltp = float(h.get("ltp", 0) or 0)
        pnl = float(h.get("profitandloss", 0) or 0)
        out.append({
            "broker":        "AngelOne",
            "symbol":        f"NSE:{sym}",
            "exchange":      "NSE",
            "qty":           qty,
            "avg_cost":      avg,
            "last_price":    ltp,
            "unrealized_pnl": pnl,
            "currency":      "INR",
        })
    return out


def get_data() -> dict:
    """
    Fetch live holdings from Zerodha + Angel One.

    Returns:
        {
          "positions":    list of normalised holding dicts
          "zerodha_count":  int
          "angelone_count": int
          "total_value":    float (sum of last_price * qty, INR)
          "total_pnl":      float (sum of unrealized_pnl, INR)
          "warnings":       list of str
        }
    """
    warnings: list[str] = []

    print("  [India] Fetching Zerodha holdings ...")
    z_pos = _zerodha_holdings()
    if not z_pos:
        warnings.append("Zerodha holdings unavailable (no access token or connection error)")

    print("  [India] Fetching Angel One holdings ...")
    a_pos = _angelone_holdings()
    if not a_pos:
        warnings.append("Angel One holdings unavailable (connection error or invalid credentials)")

    all_pos = z_pos + a_pos
    total_value = sum(p["last_price"] * p["qty"] for p in all_pos)
    total_pnl   = sum(p["unrealized_pnl"] for p in all_pos)

    print(f"  [India] {len(z_pos)} Zerodha + {len(a_pos)} Angel One positions "
          f"= {len(all_pos)} total  | Value: ₹{total_value:,.0f}  PnL: ₹{total_pnl:+,.0f}")

    return {
        "positions":      all_pos,
        "zerodha_count":  len(z_pos),
        "angelone_count": len(a_pos),
        "total_value":    total_value,
        "total_pnl":      total_pnl,
        "warnings":       warnings,
    }


# ─── India CGT (FIFO via statement files) ─────────────────────────────────────
# BUG-NEW-1 fix (2026-05-01): india_cgt.py was holdings-only — zero CGT
# computation despite the module name.  Zerodha Tax P&L XLSX files already
# exist in zerodha-statement/ and zerodha_statement_reader.py can parse them
# (STCG, LTCG, intraday, per-trade details).  Wire that reader here so callers
# get real tax data from a single point of truth: india_cgt.get_cgt_data().


def _zr_stmt_dir() -> Path:
    """Return the path to the zerodha-statement folder (sibling of india-brokers/)."""
    return Path(__file__).resolve().parent.parent / "zerodha-statement"


def _ao_stmt_dir() -> Path:
    """Return the path to the angelone-statement folder."""
    return Path(__file__).resolve().parent.parent / "angelone-statement"


def get_cgt_data(
    zerodha_dir: "Path | None" = None,
    angelone_dir: "Path | None" = None,
) -> dict:
    """Return structured India CGT data from statement files.

    Zerodha Tax P&L XLSX files are parsed by ``zerodha_statement_reader``.
    AngelOne P&L XLSX/CSV files are parsed by ``angelone_statement_reader``
    when available.

    Returns:
        {
          "fy_rows":   list[dict]  — one row per FY per broker:
                       {fy, broker, net_inr, st_inr, lt_inr, intraday_inr,
                        trades_count, dividends_total, source}
          "trades":    list[dict]  — all per-trade CGT events across FYs
                       {symbol, isin, section, entry_date, exit_date, qty,
                        buy_val, sell_val, profit, taxable_profit, fy, broker}
          "warnings":  list[str]
          "zerodha_fy_count":  int
          "angelone_fy_count": int
        }

    Notes:
        - STCG rate: 15% (held < 12 months)
        - LTCG rate: 10% above ₹1L exemption (held >= 12 months)
        - Intraday (speculative): 30% slab rate
        - FY: April 1 – March 31  (Zerodha FY label: "FY2026" = Apr 2025–Mar 2026)
    """
    from pathlib import Path as _Path

    warnings_out: list[str] = []
    fy_rows:       list[dict] = []
    all_trades:    list[dict] = []

    z_dir = _Path(zerodha_dir) if zerodha_dir else _zr_stmt_dir()
    a_dir = _Path(angelone_dir) if angelone_dir else _ao_stmt_dir()

    # ── Zerodha ───────────────────────────────────────────────────────────────
    _zr_reader_path = z_dir / "zerodha_statement_reader.py"
    if _zr_reader_path.exists():
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("_zr_reader", str(_zr_reader_path))
            _mod  = _ilu.module_from_spec(_spec)          # type: ignore[arg-type]
            _spec.loader.exec_module(_mod)                 # type: ignore[union-attr]

            # FY summary rows
            _rows = _mod.build_summary_for_dashboard(z_dir)
            fy_rows.extend(_rows)

            # Per-trade details
            for _summary in _mod.get_all_zerodha_summaries(z_dir):
                _fy = _summary.get("fy", "")
                for _t in _summary.get("trades", []):
                    all_trades.append({**_t, "fy": _fy, "broker": "Zerodha"})

        except Exception as _e:
            warnings_out.append(f"Zerodha statement reader error: {_e}")
    else:
        warnings_out.append(
            f"Zerodha statement folder not found: {z_dir} — install Zerodha P&L XLSX "
            "into zerodha-statement/ to enable CGT computation."
        )

    _zerodha_fy_count = len(fy_rows)

    # ── AngelOne ─────────────────────────────────────────────────────────────
    _angelone_fy_count = 0
    _ao_reader_path = a_dir / "angelone_statement_reader.py"
    if _ao_reader_path.exists():
        try:
            import importlib.util as _ilu2
            _spec2 = _ilu2.spec_from_file_location("_ao_reader", str(_ao_reader_path))
            _mod2  = _ilu2.module_from_spec(_spec2)        # type: ignore[arg-type]
            _spec2.loader.exec_module(_mod2)               # type: ignore[union-attr]

            if hasattr(_mod2, "build_summary_for_dashboard"):
                _ao_rows = _mod2.build_summary_for_dashboard(a_dir)
                fy_rows.extend(_ao_rows)
                _angelone_fy_count = len(_ao_rows)
            if hasattr(_mod2, "get_all_summaries"):
                for _s2 in _mod2.get_all_summaries(a_dir):
                    _fy2 = _s2.get("fy", "")
                    for _t2 in _s2.get("trades", []):
                        all_trades.append({**_t2, "fy": _fy2, "broker": "AngelOne"})
        except Exception as _e2:
            warnings_out.append(f"AngelOne statement reader error: {_e2}")
    else:
        warnings_out.append(
            "AngelOne statement folder or reader not found — AngelOne CGT skipped."
        )

    return {
        "fy_rows":            fy_rows,
        "trades":             all_trades,
        "warnings":           warnings_out,
        "zerodha_fy_count":   _zerodha_fy_count,
        "angelone_fy_count":  _angelone_fy_count,
    }


def positions_for_sell_strategy() -> list[dict]:
    """
    Return positions in sell_strategy.py input format.
    Each dict has: symbol, entry_price, entry_date, shares, atr_at_entry,
                   high_water_mark, current_price.

    NOTE: entry_date, atr_at_entry, and high_water_mark are ALWAYS None here,
    not just "not yet implemented" -- Zerodha/AngelOne holdings APIs return
    only current qty + average cost, never the original buy date. The only
    local data source that has trade dates is zerodha-statement/, which
    contains a realized/closed-trades Tax P&L export (taxpnl-*.xlsx), not a
    Tradebook -- it has no rows for currently OPEN positions at all, so it
    cannot be used to backfill entry_date for held positions. As a result,
    for every India position: time_stop_triggered/min_hold_active are always
    None-safe no-ops (see sell_engine.trading_days_between), and Sell
    Strategy's "Time Stop" card row will show "Unknown" instead of a day
    count. Fixing this requires either a broker API that returns lot-level
    buy dates, or a maintained full Tradebook export -- confirmed unavailable
    as of 2026-07-06.
    """
    import math
    try:
        data = get_data()
    except Exception as exc:
        print(f"  [India] positions_for_sell_strategy error: {exc}")
        return []
    out  = []
    for p in data["positions"]:
        avg = p["avg_cost"]
        if avg <= 0 or not math.isfinite(avg):
            continue
        ltp = p.get("last_price")
        out.append({
            "symbol":          p["symbol"],
            "entry_price":     round(avg, 4),
            "entry_date":      None,   # not available from holdings API
            "shares":          int(p["qty"]),
            "atr_at_entry":    None,
            "high_water_mark": None,
            "current_price":   round(ltp, 4) if (ltp is not None and ltp > 0 and math.isfinite(ltp)) else None,
            "broker":          p.get("broker", "India"),
        })
    return out


if __name__ == "__main__":
    data = get_data()
    print("\n=== India Holdings ===")
    for p in data["positions"]:
        pct = ((p["last_price"] - p["avg_cost"]) / p["avg_cost"] * 100
               if p["avg_cost"] else 0)
        sign = "+" if pct >= 0 else ""
        print(f"  [{p['broker']:<9}] {p['symbol']:<25}  "
              f"qty={p['qty']:>4}  avg=₹{p['avg_cost']:>8.2f}  "
              f"ltp=₹{p['last_price']:>8.2f}  "
              f"pnl=₹{p['unrealized_pnl']:>8.0f}  ({sign}{pct:.1f}%)")
    print(f"\n  Total value: ₹{data['total_value']:,.0f}")
    print(f"  Total P&L:   ₹{data['total_pnl']:+,.0f}")
    if data["warnings"]:
        for w in data["warnings"]:
            print(f"  ⚠ {w}")
