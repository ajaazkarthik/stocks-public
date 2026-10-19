"""
options_hedger_data.py — Broker data fetchers for the Options Hedger.

Provides all _load_* functions that retrieve live holdings from:
  - Zerodha + AngelOne (India, via IndiaEnricher)
  - Moomoo-AU / Moomoo-SG (via OpenD)
  - IBKR (via Flex XML cache)
  - PCR + OI Buildup data (via AngelOne)
  - India VIX (via yfinance)
  - India market regime (via india_regime.py)
"""
from __future__ import annotations

import math
import socket
import sys
from pathlib import Path
from typing import Optional

import structlog

from .options_hedger_quotes import _parse_option_code  # used in _load_global_holdings

ROOT      = Path(__file__).parent.parent        # tradingview/
REPO_ROOT = ROOT.parent                         # stocks/

_log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data fetchers
# ─────────────────────────────────────────────────────────────────────────────

def _load_holdings(min_value_inr: float = 0.0) -> list[dict]:
    """
    Load India holdings from Zerodha + AngelOne (via IndiaEnricher).

    Returns list of holding dicts with: symbol, total_qty, combined_avg,
    combined_ltp, combined_pnl, combined_pnl_pct, combined_val, brokers.
    """
    try:
        sys.path.insert(0, str(ROOT.parent / "india-brokers"))
        from india_enricher import IndiaEnricher  # type: ignore
    except ImportError as e:
        print(f"  ⚠ Cannot import IndiaEnricher: {e}")
        return []

    enricher = IndiaEnricher()
    holdings = list(enricher._holdings.values())   # type: ignore[attr-defined]

    if min_value_inr > 0:
        holdings = [h for h in holdings if h.get("combined_val", 0) >= min_value_inr]

    holdings.sort(key=lambda h: h.get("combined_val", 0), reverse=True)
    return holdings


def _is_port_open(port: int) -> bool:
    """Check if OpenD (Moomoo) is running on the given port. B-6 shim."""
    try:
        from moomoo_config import is_opend_running
        return is_opend_running(port)
    except Exception:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0):
                return True
        except OSError:
            return False


def _load_global_holdings(min_value_inr: float = 0.0) -> tuple[list[dict], list[dict]]:
    """
    Load global holdings from Moomoo-AU (port 11111), Moomoo-SG (port 11112),
    and IBKR (Flex Query cache).  Each position is normalised to the same
    schema used by IndiaEnricher so that the same hedge rules apply.

    Returns (global_positions, long_option_positions):
      global_positions  – stocks/ETFs
      long_option_positions – long calls/puts (e.g. GOOGL260424C335000)
    """
    _EXCH_MAP = {"AU": "ASX", "HK": "HKEX", "SG": "SGX", "JP": "TYO",
                 "KR": "KRX", "CA": "TSX", "UK": "LON", "US": ""}
    _CURR_MAP = {"AU": "AUD", "HK": "HKD", "SG": "SGD", "JP": "JPY",
                 "KR": "KRW", "CA": "CAD", "UK": "GBP", "US": "USD"}

    global_positions: list[dict] = []
    long_option_positions: list[dict] = []

    # ── Moomoo AU + SG ────────────────────────────────────────────────────────
    for label, port in [("AU", 11111), ("SG", 11112)]:
        if not _is_port_open(port):
            print(f"    ⚠ Moomoo-{label}: OpenD not running on port {port} — skipping")
            continue

        try:
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))

            from moomoo import OpenSecTradeContext, TrdMarket, TrdEnv  # type: ignore
            from moomoo_config import ACCOUNTS  # type: ignore

            acct = next((a for a in ACCOUNTS if a[0] == label), None)
            if not acct:
                print(f"    ⚠ Moomoo-{label}: no account config found")
                continue

            _, _port, firm, acc_id = acct
            ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.NONE,
                host="127.0.0.1", port=port, security_firm=firm,
            )
            try:
                ret, df = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
            finally:
                ctx.close()

            if ret != 0 or df is None or df.empty:
                print(f"    ⚠ Moomoo-{label}: empty response (ret={ret})")
                continue

            count_before = len(global_positions)
            for _, row in df.iterrows():
                code   = str(row.get("code", ""))
                qty    = float(row.get("qty", 0))
                avg    = float(row.get("average_cost", 0))
                mval   = float(row.get("market_val", 0))

                if qty <= 0 or avg <= 0 or not math.isfinite(avg):
                    continue

                opt_info = _parse_option_code(code)
                if opt_info is not None:
                    nom_price     = float(row.get("nominal_price") or 0) or (mval / qty / 100 if qty else 0)
                    contract_cost = avg * 100
                    cost          = contract_cost * qty
                    pnl           = mval - cost
                    pnl_pct       = pnl / cost * 100 if cost else 0.0
                    prefix        = code.split(".")[0].upper() if "." in code else ""

                    long_option_positions.append({
                        "symbol":           opt_info["underlying"],
                        "total_qty":        int(qty),
                        "combined_avg":     round(contract_cost, 2),
                        "combined_ltp":     round(nom_price, 4),
                        "combined_pnl":     round(pnl, 2),
                        "combined_pnl_pct": round(pnl_pct, 1),
                        "combined_val":     round(mval, 2),
                        "currency":         _CURR_MAP.get(prefix, "USD"),
                        "broker":           f"Moomoo-{label}",
                        "exchange":         "US",
                        "brokers":          [f"Moomoo-{label}"],
                        "opt_info":         opt_info,
                        "position_type":    "LONG_CALL" if opt_info["option_type"] == "C" else "LONG_PUT",
                    })
                    continue

                nom_price = float(row.get("nominal_price") or 0) or (mval / qty if qty else 0)
                cost      = avg * qty
                pnl       = mval - cost
                pnl_pct   = pnl / cost * 100 if cost else 0.0
                prefix    = code.split(".")[0].upper() if "." in code else ""
                exchange  = _EXCH_MAP.get(prefix, "")
                ticker    = code.split(".", 1)[1] if "." in code else code
                tv_sym    = f"{exchange}:{ticker}" if exchange else ticker

                global_positions.append({
                    "symbol":           tv_sym,
                    "total_qty":        int(qty),
                    "combined_avg":     round(avg, 4),
                    "combined_ltp":     round(nom_price, 4),
                    "combined_pnl":     round(pnl, 2),
                    "combined_pnl_pct": round(pnl_pct, 1),
                    "combined_val":     round(mval, 2),
                    "currency":         _CURR_MAP.get(prefix, "USD"),
                    "broker":           f"Moomoo-{label}",
                    "exchange":         exchange or "US",
                    "brokers":          [f"Moomoo-{label}"],
                })

            added = len(global_positions) - count_before
            print(f"    Moomoo-{label}: {added} positions")

        except Exception as e:
            print(f"    ⚠ Moomoo-{label} error: {e}")

    # ── IBKR ──────────────────────────────────────────────────────────────────
    try:
        _ibkr_dir = REPO_ROOT / "cgt" / "ibkr-cgt"
        if str(_ibkr_dir) not in sys.path:
            sys.path.insert(0, str(_ibkr_dir))

        import ibkr_cgt_analysis as ibkr  # type: ignore

        cache_path = _ibkr_dir / "_last_flex.xml"
        if cache_path.exists():
            xml_str = cache_path.read_text(encoding="utf-8")
            _, stmt_pos, _, _ = ibkr.parse_flex_xml(xml_str, ibkr.FETCH_START)
        else:
            _, stmt_pos, _, _ = ibkr.fetch_flex_statement(
                ibkr.FLEX_TOKEN, ibkr.FLEX_QUERY_ID, ibkr.FETCH_START
            )

        if stmt_pos is not None and not stmt_pos.empty:
            count_before = len(global_positions)
            for _, row in stmt_pos.iterrows():
                qty      = float(row.get("qty", 0))
                avg_cost = float(row.get("avg_cost", 0))
                last_p   = float(row.get("last_price", 0) or 0)

                if qty <= 0 or avg_cost <= 0 or not math.isfinite(avg_cost):
                    continue

                code   = str(row.get("code", ""))
                ticker = code.split(".", 1)[1] if "." in code else code
                mval   = last_p * qty
                cost   = avg_cost * qty
                pnl    = mval - cost

                global_positions.append({
                    "symbol":           ticker,
                    "total_qty":        int(qty),
                    "combined_avg":     round(avg_cost, 4),
                    "combined_ltp":     round(last_p, 4),
                    "combined_pnl":     round(pnl, 2),
                    "combined_pnl_pct": round(pnl / cost * 100 if cost else 0, 1),
                    "combined_val":     round(mval, 2),
                    "currency":         "USD",
                    "broker":           "IBKR",
                    "exchange":         "US",
                    "brokers":          ["IBKR"],
                })

            print(f"    IBKR: {len(global_positions) - count_before} positions")
        else:
            print("    IBKR: no open positions")

    except Exception as e:
        print(f"    ⚠ IBKR error: {e}")

    # ── Filter + sort ─────────────────────────────────────────────────────────
    if min_value_inr > 0:
        _FX = {"INR": 1, "USD": 84, "AUD": 55, "HKD": 11, "SGD": 63,
               "JPY": 0.56, "KRW": 0.063, "GBP": 107}
        global_positions = [
            p for p in global_positions
            if p["combined_val"] * _FX.get(p["currency"], 84) >= min_value_inr
        ]

    global_positions.sort(key=lambda h: h.get("combined_val", 0), reverse=True)
    return global_positions, long_option_positions


def _load_pcr_map() -> dict[str, float]:
    """Load PCR for all NSE F&O symbols via AngelOne."""
    try:
        sys.path.insert(0, str(ROOT.parent / "india-brokers"))
        from india_market_internals import IndiaMarketInternals  # type: ignore
    except ImportError:
        return {}

    mi = IndiaMarketInternals()
    pcr_list = mi.pcr()
    return {item["symbol"].upper(): item["pcr"] for item in pcr_list}


def _load_oi_map() -> dict[str, str]:
    """Load OI buildup type for near-month expiry via AngelOne."""
    try:
        sys.path.insert(0, str(ROOT.parent / "india-brokers"))
        from india_market_internals import IndiaMarketInternals  # type: ignore
    except ImportError:
        return {}

    mi = IndiaMarketInternals()
    oi_map: dict[str, str] = {}
    for btype in ("Long Built Up", "Short Built Up", "Short Covering", "Long Unwinding"):
        for row in mi.oi_buildup(btype, "NEAR"):
            sym = row["symbol"].upper()
            if sym not in oi_map:
                oi_map[sym] = btype
    return oi_map


def _load_regime() -> dict:
    """Load India market regime (Bull / Bear / Sideways) via india_regime.py."""
    try:
        sys.path.insert(0, str(ROOT.parent / "india-brokers"))
        from india_regime import get_regime  # type: ignore
        return get_regime()
    except Exception as exc:
        _log.warning("_load_india_regime failed", exc_info=exc)
        return {}


def _load_india_vix() -> Optional[float]:
    """Fetch India VIX (^INDIAVIX) from yfinance. Returns None on failure."""
    try:
        import yfinance as yf
        t = yf.Ticker("^INDIAVIX")
        h = t.history(period="2d", auto_adjust=True)
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception as exc:
        _log.warning("_load_india_vix: yfinance ^INDIAVIX fetch failed", exc_info=exc)
    return None
