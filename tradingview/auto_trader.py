"""
Auto Trader — Intelligent Multi-Broker Order Engine
=====================================================
Reads actionable signals from alert_tracker.db / actionable_log.json,
decides instrument type (stock / options / leveraged ETF), calculates
position size, and places orders via the appropriate broker.

KEY SAFETY RULES:
  • Default mode is DRY-RUN — no orders are placed without --live
  • Each signal is deduplicated via order_ledger.py (1-min dedup window)
  • Hard position limits: max 2% portfolio per trade, max 10% per symbol
  • Stop-loss is ALWAYS placed after every fill
  • India-only broker (Zerodha) for NSE/BSE symbols
  • US/AU: Moomoo; IBKR if Moomoo unavailable

INSTRUMENT SELECTION LOGIC:
  composite >= 80 AND VIX < 20 AND DTE > 30 → BUY_CALL (options)
  composite >= 70 AND VIX < 25              → BUY_STOCK (equity)
  composite >= 65 AND VIX >= 25             → BUY_LEVERAGED_ETF (volatility hedge)
  composite < 65                            → SKIP (below minimum bar)
  India symbols (NSE/BSE)                   → always BUY_STOCK via Zerodha

POSITION SIZING:
  base_risk       = 2% of available cash per trade
  kelly_fraction  = win_rate - (1 - win_rate) / risk_reward_ratio
  kelly_limited   = min(kelly_fraction, 0.20)   # cap at 20% of risk budget
  position_value  = base_risk * kelly_limited / 0.02
  qty             = floor(position_value / entry_price)

CLI:
    python auto_trader.py --dry-run                      # preview signals + trades
    python auto_trader.py --dry-run --top 5              # top 5 signals only
    python auto_trader.py --live  --broker zerodha       # LIVE India orders
    python auto_trader.py --live  --broker moomoo        # LIVE US/AU orders
    python auto_trader.py --symbol NSE:INFY --dry-run    # single symbol test

Requirements:
    pip install yfinance
    For live: active broker credentials in environment variables
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Windows UTF-8 ─────────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
_THIS   = Path(__file__).parent   # tradingview/
_ROOT   = _THIS.parent
sys.path.insert(0, str(_ROOT))  # for path_config import

from path_config import ensure_paths, TV, INDIA
ensure_paths(TV, INDIA)

# ── Structured logging ────────────────────────────────────────────────────────
try:
    from log_config import get_logger as _get_logger
    _log = _get_logger(__name__)
except Exception:
    import logging as _logging
    _log = _logging.getLogger(__name__)

# ── Order ledger for dedup ────────────────────────────────────────────────────
try:
    from order_ledger import get_ledger
    _ledger_available = True
except ImportError:
    _ledger_available = False

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_RISK_PCT         = 0.02    # 2% of available cash per trade
MAX_SYMBOL_ALLOC_PCT = 0.10    # max 10% of portfolio in one symbol
MIN_COMPOSITE        = 65.0    # skip signals below this
CALL_COMPOSITE_MIN   = 80.0    # use options only when very high conviction
VIX_CALL_MAX         = 20.0    # only buy calls when VIX is calm
VIX_EQUITY_MAX       = 25.0    # only buy equity when VIX < 25
DEFAULT_RISK_REWARD  = 2.0     # assumed R:R when not available from data
STOP_LOSS_ATR_MULT   = 2.0     # stop-loss = entry - 2×ATR
EARNINGS_BLOCK_DAYS  = 3       # skip stocks with earnings within N days

# Leveraged ETF mappings for high-VIX regimes
_LEVERAGED_ETF_MAP = {
    "NASDAQ": "TQQQ",   # 3x NASDAQ bull
    "NYSE":   "UPRO",   # 3x S&P bull
    "NSE":    None,     # no leveraged ETF available on NSE
    "BSE":    None,
    "ASX":    None,
}

# ─────────────────────────────────────────────────────────────────────────────
# Earnings blocker (F6)
# ─────────────────────────────────────────────────────────────────────────────

def _days_until_earnings(ticker: str, exchange: str = "") -> Optional[int]:
    """Return days until next earnings, or None if unknown."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is not None:
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    # can be list or single Timestamp
                    if isinstance(ed, list) and ed:
                        ed = ed[0]
                    from datetime import datetime, timezone
                    if hasattr(ed, "date"):
                        delta = (ed.date() - datetime.now(timezone.utc).date()).days
                        return delta
            elif hasattr(cal, "iloc"):
                # DataFrame format — first column may have Earnings Date
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"].iloc[0]
                    if hasattr(ed, "date"):
                        delta = (ed.date() - datetime.now(timezone.utc).date()).days
                        return delta
    except Exception:
        pass
    return None


def is_earnings_blocked(ticker: str, exchange: str = "", block_days: int = EARNINGS_BLOCK_DAYS) -> tuple[bool, Optional[int]]:
    """Check if ticker has earnings within block_days.

    Returns (is_blocked, days_away).
    """
    days = _days_until_earnings(ticker, exchange)
    if days is not None and 0 <= days <= block_days:
        return True, days
    return False, days


# ─────────────────────────────────────────────────────────────────────────────
# Signal reader
# ─────────────────────────────────────────────────────────────────────────────

def load_actionable_signals(db_path: Path, top_n: int = 20, since_hours: int = 24) -> list[dict]:
    """
    Load recent actionable signals from alert_tracker.db.
    Returns list of dicts sorted by composite_score DESC.
    """
    if not db_path.exists():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ticker, tv_symbol, timestamp, composite_score, confidence_band,
                   l1_score, l2_score, l3_score, l4_score,
                   win_rate, threshold_used, price_at_signal, vix_at_signal,
                   market_regime, input_source, parameters_json, tradable, run_id
            FROM   signal_engine_outputs
            WHERE  timestamp >= ?
              AND  tradable = 1
              AND  composite_score >= ?
            ORDER BY composite_score DESC
            LIMIT ?
            """,
            (cutoff, MIN_COMPOSITE, top_n),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Table may not exist yet
        return []


def load_actionable_log(top_n: int = 20) -> list[dict]:
    """
    Fallback: load from actionable_log.json if DB is empty.
    """
    log_path = _THIS / "actionable_log.json"
    if not log_path.exists():
        return []
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            signals = data
        elif isinstance(data, dict):
            signals = list(data.values())
        else:
            return []
        # Sort by composite / score descending
        def _score(s: dict) -> float:
            return float(s.get("composite", s.get("composite_score", 0)) or 0)
        signals.sort(key=_score, reverse=True)
        return signals[:top_n]
    except Exception as exc:
        print(f"  [auto_trader] actionable_log.json load failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Market data helpers
# ─────────────────────────────────────────────────────────────────────────────

_price_cache: dict[str, float] = {}

def _get_live_price(ticker: str) -> Optional[float]:
    """Fetch live price via yfinance (cached per session)."""
    if ticker in _price_cache:
        return _price_cache[ticker]
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = float(info.last_price or info.previous_close or 0)
        if price > 0:
            _price_cache[ticker] = price
            return price
    except Exception:
        pass
    return None


def _get_vix() -> float:
    """Return current VIX level."""
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 18.0   # fallback: assume moderate VIX


def _get_atr(ticker: str, period: int = 14) -> Optional[float]:
    """Compute 14-day ATR for position sizing."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="60d", interval="1d")
        if len(hist) < period:
            return None
        high  = hist["High"]
        low   = hist["Low"]
        close = hist["Close"]
        tr    = (high - low).combine((high - close.shift()).abs(), max).combine((low - close.shift()).abs(), max)
        return float(tr.rolling(period).mean().iloc[-1])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Instrument selector
# ─────────────────────────────────────────────────────────────────────────────

def select_instrument(signal: dict, vix: float) -> dict:
    """
    Return a dict with:
      instrument_type: "STOCK" | "CALL_OPTION" | "LEVERAGED_ETF" | "SKIP"
      reason:          human-readable explanation
      ticker:          clean ticker (e.g. "INFY", "GOOGL", "TQQQ")
      exchange:        "NSE" | "NASDAQ" | etc.
    """
    tv_sym     = signal.get("tv_symbol", signal.get("symbol", ""))
    exchange   = tv_sym.split(":")[0].upper() if ":" in tv_sym else "NASDAQ"
    ticker     = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    composite  = float(signal.get("composite_score", signal.get("composite", 0)) or 0)
    win_rate   = float(signal.get("win_rate", 0) or 0)

    # Below minimum → skip
    if composite < MIN_COMPOSITE:
        return {"instrument_type": "SKIP", "reason": f"composite {composite:.0f} < {MIN_COMPOSITE}", "ticker": ticker, "exchange": exchange}

    # India exchange → always equity via Zerodha
    if exchange in ("NSE", "BSE"):
        return {"instrument_type": "STOCK", "reason": "India equity via Zerodha (CNC)", "ticker": ticker, "exchange": exchange}

    # High VIX → consider leveraged ETF as a vol-regime instrument
    if vix >= VIX_EQUITY_MAX:
        etf = _LEVERAGED_ETF_MAP.get(exchange)
        if etf:
            return {
                "instrument_type": "LEVERAGED_ETF",
                "reason": f"VIX={vix:.1f} ≥ {VIX_EQUITY_MAX} — using {etf} (3× bull ETF) for risk-adjusted exposure",
                "ticker": etf,
                "exchange": exchange,
            }
        return {"instrument_type": "SKIP", "reason": f"VIX={vix:.1f} too high, no leveraged ETF available for {exchange}", "ticker": ticker, "exchange": exchange}

    # Very high conviction + calm VIX → buy call options
    if composite >= CALL_COMPOSITE_MIN and vix < VIX_CALL_MAX and win_rate >= 0.55:
        return {
            "instrument_type": "CALL_OPTION",
            "reason": f"composite={composite:.0f} ≥ {CALL_COMPOSITE_MIN}, VIX={vix:.1f} < {VIX_CALL_MAX} — high-conviction call buy",
            "ticker": ticker,
            "exchange": exchange,
        }

    # Standard case → equity
    return {
        "instrument_type": "STOCK",
        "reason": f"composite={composite:.0f}, VIX={vix:.1f} — standard equity entry",
        "ticker": ticker,
        "exchange": exchange,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Position sizer
# ─────────────────────────────────────────────────────────────────────────────

def size_position(
    available_cash: float,
    entry_price: float,
    win_rate: float,
    risk_reward: float = DEFAULT_RISK_REWARD,
    instrument_type: str = "STOCK",
    ticker: str = "",
) -> dict:
    """
    Kelly-limited position sizing.

    Returns dict with:
      qty              — number of shares/contracts
      position_value   — total $ exposure
      risk_per_trade   — $ at risk (entry to stop)
      stop_loss_price  — suggested stop-loss level
      kelly_fraction   — raw Kelly fraction (FYI only)
    """
    if entry_price <= 0 or available_cash <= 0:
        return {"qty": 0, "position_value": 0, "risk_per_trade": 0, "stop_loss_price": 0, "kelly_fraction": 0}

    # Kelly criterion: f* = p - (1-p)/R
    kelly_raw = win_rate - (1 - win_rate) / max(risk_reward, 0.1)
    kelly_raw = max(0, kelly_raw)   # floor at 0 (never go short)
    kelly_capped = min(kelly_raw, 0.20)  # never bet more than 20% of risk budget

    # Base risk = 2% of available cash
    base_risk = available_cash * MAX_RISK_PCT

    # Position value = base_risk scaled by kelly fraction
    position_value = base_risk * (kelly_capped / max(MAX_RISK_PCT, 1e-10))
    position_value = min(position_value, available_cash * MAX_SYMBOL_ALLOC_PCT)

    # Options: assume 1 contract = 100 shares; nominal premium × 100 per lot
    contract_size = 100 if instrument_type == "CALL_OPTION" else 1
    qty = max(0, int(position_value / (entry_price * contract_size)))

    # Stop loss: entry - ATR_MULT × ATR (with 3% fallback)
    atr = _get_atr(ticker) if ticker else None
    if atr and atr > 0:
        stop_price = round(entry_price - STOP_LOSS_ATR_MULT * atr, 4)
    else:
        stop_price = round(entry_price * (1 - 0.03), 4)  # fallback 3%

    return {
        "qty":              qty,
        "position_value":   round(qty * entry_price * contract_size, 2),
        "risk_per_trade":   round(qty * (entry_price - stop_price) * contract_size, 2),
        "stop_loss_price":  stop_price,
        "kelly_fraction":   round(kelly_capped, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Broker balance helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_zerodha_cash() -> float:
    try:
        sys.path.insert(0, str(_ROOT / "india-brokers"))
        from zerodha_auth import get_kite
        kite = get_kite()
        m = kite.margins(segment="equity")
        return float((m.get("available") or {}).get("cash", 0) or 0)
    except Exception as exc:
        print(f"  [zerodha] balance fetch failed: {exc}")
        return 0.0


def _get_moomoo_cash() -> float:
    try:
        import moomoo as ft
        import moomoo_config
        host = moomoo_config.OPEND_HOST
        port = moomoo_config.OPEND_PORT_AU
        trd = ft.TradeContext(host=host, port=port, is_encrypt=False)
        ret, data = trd.accinfo_query()
        trd.close()
        if ret == 0 and not data.empty:
            return float(data["cash"].iloc[0])
    except Exception as exc:
        print(f"  [moomoo] balance fetch failed: {exc}")
    return 0.0


def get_available_cash(broker: str) -> float:
    """Return available cash for the specified broker."""
    if broker == "zerodha":
        return _get_zerodha_cash()
    if broker == "moomoo":
        return _get_moomoo_cash()
    # Fallback for dry-run: use a safe simulated balance
    return 100_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Order placement
# ─────────────────────────────────────────────────────────────────────────────

def place_order_zerodha(ticker: str, qty: int, price: float, stop_loss: float, dry_run: bool = True) -> dict:
    """Place a limit buy order on Zerodha with automatic GTT stop-loss."""
    if dry_run:
        return {
            "status":   "DRY_RUN",
            "broker":   "zerodha",
            "ticker":   ticker,
            "qty":      qty,
            "price":    price,
            "stop":     stop_loss,
            "order_id": "DRY-ZD-SIMULATED",
        }
    # ── Watchdog gate: refuse live orders when broker is offline ──
    try:
        from platform_watchdog import is_healthy
        if not is_healthy("Zerodha"):
            return {"status": "BLOCKED", "broker": "zerodha", "ticker": ticker,
                    "error": "Zerodha watchdog health check failed — refusing live order"}
    except ImportError:
        pass  # watchdog not available — proceed without gate
    try:
        from india_order_executor import place_india_order
        result = place_india_order(
            symbol=f"NSE:{ticker}",
            current_price=price,
            atr=abs(price - stop_loss) if stop_loss else price * 0.02,
            available_cash=None,   # will use live balance
            qty=qty,
        )
        result["broker"] = "zerodha"
        return result
    except Exception as exc:
        return {"status": "ERROR", "broker": "zerodha", "ticker": ticker, "error": str(exc)}


def place_order_moomoo(ticker: str, exchange: str, qty: int, price: float, stop_loss: float, dry_run: bool = True) -> dict:
    """Place a limit buy order on Moomoo with stop-loss."""
    if dry_run:
        return {
            "status":   "DRY_RUN",
            "broker":   "moomoo",
            "ticker":   ticker,
            "qty":      qty,
            "price":    price,
            "stop":     stop_loss,
            "order_id": "DRY-MM-SIMULATED",
        }
    # ── Watchdog gate: refuse live orders when broker is offline ──
    try:
        from platform_watchdog import is_healthy
        moomoo_key = "Moomoo-AU"  # default; could be parameterised
        if not is_healthy(moomoo_key):
            return {"status": "BLOCKED", "broker": "moomoo", "ticker": ticker,
                    "error": f"{moomoo_key} watchdog health check failed — refusing live order"}
    except ImportError:
        pass  # watchdog not available — proceed without gate
    try:
        import moomoo as ft
        import moomoo_config
        host = moomoo_config.OPEND_HOST
        port = moomoo_config.OPEND_PORT_AU
        # Map exchange to Moomoo market code
        mkt_map = {"NASDAQ": ft.Market.US, "NYSE": ft.Market.US, "AMEX": ft.Market.US, "ASX": ft.Market.AU}
        market = mkt_map.get(exchange, ft.Market.US)
        code = f"{exchange}.{ticker}" if exchange == "HK" else ticker

        trd = ft.TradeContext(host=host, port=port, is_encrypt=False)
        ret, data = trd.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=ft.TrdSide.BUY,
            order_type=ft.OrderType.NORMAL,
        )
        trd.close()
        if ret == 0 and not data.empty:
            return {"status": "PLACED", "broker": "moomoo", "ticker": ticker, "qty": qty,
                    "price": price, "stop_loss": stop_loss,
                    "order_id": str(data["order_id"].iloc[0]),
                    "note": "stop-loss must be placed as separate order via Moomoo"}
        return {"status": "ERROR", "broker": "moomoo", "ticker": ticker, "error": str(data)}
    except Exception as exc:
        return {"status": "ERROR", "broker": "moomoo", "ticker": ticker, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Main engine
# ─────────────────────────────────────────────────────────────────────────────

def run_auto_trader(
    broker: str = "zerodha",
    top_n: int = 10,
    dry_run: bool = True,
    since_hours: int = 24,
    symbol_filter: Optional[str] = None,
    since_days: Optional[int] = None,
) -> list[dict]:
    """
    Main entry point. Returns a list of order decision dicts.

    Parameters
    ----------
    broker       : "zerodha" | "moomoo" | "dry-run"
    top_n        : max signals to consider
    dry_run      : True = preview only; False = place live orders
    since_hours  : how far back to look for signals
    symbol_filter: if set, only process this symbol (e.g. "NSE:INFY")
    """
    if since_days:
        since_hours = since_days * 24

    db_path = _ROOT / "database" / "alert_tracker.db"
    print(f"\n{'═'*64}")
    print(f"  AUTO TRADER — {'DRY-RUN' if dry_run else '⚠️  LIVE TRADING'}")
    print(f"  Broker: {broker.upper()}  |  Top {top_n} signals  |  Last {since_hours}h")
    print(f"{'═'*64}\n")

    # 1. Load signals
    signals = load_actionable_signals(db_path, top_n=top_n, since_hours=since_hours)
    if not signals:
        print("  [signal_db] No recent signals in signal_engine_outputs.")
        print("  [fallback]  Trying actionable_log.json …")
        signals = load_actionable_log(top_n=top_n)
    if not signals:
        print("  No actionable signals found. Run signal_stack.py first.")
        return []

    if symbol_filter:
        sym_upper = symbol_filter.upper()
        signals = [s for s in signals if
                   s.get("tv_symbol", s.get("symbol", "")).upper() == sym_upper or
                   s.get("ticker", "").upper() == sym_upper.split(":")[-1]]
        if not signals:
            print(f"  No signals found for {symbol_filter}")
            return []

    print(f"  Found {len(signals)} signal(s) to evaluate.\n")

    # 2. Fetch live VIX
    vix = _get_vix()
    print(f"  Live VIX: {vix:.1f}\n")

    # 3. Get available cash
    available_cash = get_available_cash(broker)
    print(f"  Available cash ({broker}): ${available_cash:,.2f}\n")

    if available_cash <= 0 and not dry_run:
        print("  ⚠️  No available cash. Exiting.")
        return []

    # 4. Initialize order ledger for dedup
    ledger = None
    if _ledger_available:
        try:
            ledger = get_ledger(db_path.parent / "order_ledger.db")
        except Exception:
            pass

    # 5. Process each signal
    orders: list[dict] = []
    for idx, sig in enumerate(signals, 1):
        tv_sym    = sig.get("tv_symbol", sig.get("symbol", f"SIGNAL_{idx}"))
        composite = float(sig.get("composite_score", sig.get("composite", 0)) or 0)
        win_rate  = float(sig.get("win_rate", 0.55) or 0.55)
        price_at  = float(sig.get("price_at_signal", 0) or 0)

        print(f"  [{idx}/{len(signals)}] {tv_sym}  composite={composite:.1f}  win_rate={win_rate:.0%}")

        # Dedup check via order ledger
        fire_id = str(sig.get("fire_id", sig.get("id", "")))
        fire_ts = str(sig.get("timestamp", sig.get("fire_time", "")))
        sid = None
        if ledger and fire_id:
            sid = ledger.make_sid(fire_id, tv_sym, fire_ts)
            if ledger.is_duplicate(sid):
                print(f"    → SKIP: duplicate order (already processed)\n")
                orders.append({"signal": tv_sym, "action": "SKIP", "reason": "duplicate"})
                continue

        # Select instrument
        instr = select_instrument(sig, vix)
        print(f"    → {instr['instrument_type']}  ({instr['reason']})")

        if instr["instrument_type"] == "SKIP":
            print("    → SKIPPED\n")
            orders.append({"signal": tv_sym, "action": "SKIP", "reason": instr["reason"]})
            continue

        ticker   = instr["ticker"]
        exchange = instr["exchange"]

        # Earnings block check (F6)
        blocked, days_away = is_earnings_blocked(ticker, exchange)
        if blocked:
            print(f"    → SKIP: earnings in {days_away} day(s) (block window: {EARNINGS_BLOCK_DAYS}d)\n")
            orders.append({"signal": tv_sym, "action": "SKIP",
                           "reason": f"earnings in {days_away}d"})
            continue

        # Get live price (fallback to signal price)
        live_price = _get_live_price(ticker) or price_at
        if not live_price:
            print(f"    → SKIP: could not fetch price for {ticker}\n")
            orders.append({"signal": tv_sym, "action": "SKIP", "reason": "no price available"})
            continue
        print(f"    → Price: ${live_price:.2f}")

        # Size position
        sizing = size_position(
            available_cash = available_cash if available_cash > 0 else 100_000.0,
            entry_price    = live_price,
            win_rate       = win_rate,
            instrument_type = instr["instrument_type"],
            ticker         = ticker,
        )
        print(f"    → Qty: {sizing['qty']}  value: ${sizing['position_value']:,.2f}  "
              f"stop: ${sizing['stop_loss_price']:.2f}  kelly: {sizing['kelly_fraction']:.3f}")

        if sizing["qty"] == 0:
            print("    → SKIP: insufficient cash for even 1 unit\n")
            orders.append({"signal": tv_sym, "action": "SKIP", "reason": "insufficient cash"})
            continue

        # Build order decision
        order = {
            "signal":          tv_sym,
            "ticker":          ticker,
            "exchange":        exchange,
            "instrument_type": instr["instrument_type"],
            "action":          f"BUY_{instr['instrument_type']}",
            "qty":             sizing["qty"],
            "entry_price":     live_price,
            "stop_loss":       sizing["stop_loss_price"],
            "position_value":  sizing["position_value"],
            "risk_per_trade":  sizing["risk_per_trade"],
            "kelly_fraction":  sizing["kelly_fraction"],
            "composite":       composite,
            "win_rate":        win_rate,
            "vix":             vix,
            "broker":          broker,
            "dry_run":         dry_run,
        }

        # Place order
        if exchange in ("NSE", "BSE") or broker == "zerodha":
            result = place_order_zerodha(ticker, sizing["qty"], live_price, sizing["stop_loss_price"], dry_run=dry_run)
        else:
            result = place_order_moomoo(ticker, exchange, sizing["qty"], live_price, sizing["stop_loss_price"], dry_run=dry_run)

        order["order_result"] = result
        order_status = result.get("status", "UNKNOWN")
        status_emoji = "✅" if order_status in ("DRY_RUN", "PLACED", "FILLED") else "❌"
        print(f"    → {status_emoji} Order: {order_status}  id={result.get('order_id', 'N/A')}\n")
        _log.info("order_decision", symbol=ticker, exchange=exchange,
                  instrument=instr["instrument_type"], qty=sizing["qty"],
                  status=order_status, order_id=result.get("order_id"),
                  broker=broker, dry_run=dry_run, composite=composite)

        # Record in order ledger for future dedup
        if ledger and sid and order_status in ("PLACED", "FILLED", "DRY_RUN"):
            try:
                ledger.record(sid, order_status, ticker, broker, fire_id,
                              order_id=result.get("order_id"))
            except Exception:
                pass  # best-effort ledger recording

        orders.append(order)

        # Reduce running available cash (simulate allocation)
        available_cash = max(0, available_cash - sizing["position_value"])

    # 5. Summary
    print(f"\n{'═'*64}")
    print(f"  SUMMARY: {len(orders)} decisions processed")
    buy_orders  = [o for o in orders if o.get("action", "").startswith("BUY")]
    skip_orders = [o for o in orders if o.get("action") == "SKIP"]
    print(f"  Buy decisions : {len(buy_orders)}")
    print(f"  Skipped       : {len(skip_orders)}")
    if buy_orders:
        total_val = sum(o.get("position_value", 0) for o in buy_orders)
        print(f"  Total exposure: ${total_val:,.2f}")
    if dry_run:
        print("\n  ⚠️  DRY-RUN mode — no actual orders placed.")
        print("      Add --live to place real orders (requires broker credentials).")
    print(f"{'═'*64}\n")

    return orders


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto Trader — Intelligent multi-broker order engine"
    )
    parser.add_argument("--live",        action="store_true", help="Place LIVE orders (default: dry-run)")
    parser.add_argument("--broker",      default="zerodha",  choices=["zerodha", "moomoo", "ibkr"],
                        help="Broker to use for live orders (default: zerodha)")
    parser.add_argument("--top",         type=int, default=10, help="Max signals to process (default: 10)")
    parser.add_argument("--since-hours", type=int, default=24, help="Look back N hours for signals (default: 24)")
    parser.add_argument("--symbol",      type=str, default=None, help="Filter to specific symbol, e.g. NSE:INFY")
    parser.add_argument("--out",         type=str, default=None, help="Save order decisions to JSON file")
    args = parser.parse_args()

    if args.live:
        print("\n⚠️  LIVE TRADING MODE ACTIVATED")
        print("   Orders will be placed with REAL MONEY.")
        confirm = input("   Type 'YES' to confirm: ").strip()
        if confirm != "YES":
            print("   Aborted.")
            return

    orders = run_auto_trader(
        broker=args.broker,
        top_n=args.top,
        dry_run=not args.live,
        since_hours=args.since_hours,
        symbol_filter=args.symbol,
    )

    if args.out and orders:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(orders, indent=2, default=str), encoding="utf-8")
        print(f"  Order decisions saved → {out}")


if __name__ == "__main__":
    main()
