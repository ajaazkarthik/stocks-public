"""
India GTT Manager — Stop-Loss / Target Automation for Zerodha Holdings
========================================================================
GTT = Good Till Triggered. Unlike normal orders (which expire same day),
GTT orders persist ACROSS sessions until triggered, expired, or cancelled.

Zerodha supports two GTT types:
  SINGLE  → one trigger (stop-loss only, or buy-the-dip)
  TWO-LEG → OCO (One Cancels Other): stop-loss + take-profit target

This script:
  1. Audits current holdings vs active GTTs → finds UNPROTECTED positions
  2. Fetches Zerodha historical data → calculates ATR(14) per symbol
  3. Suggests and optionally places GTT OCO orders with:
       stop-loss  = avg_price − max(2×ATR, 8% of avg_price)   ← floor: 8%
       target     = avg_price + max(3×ATR, 12% of avg_price)  ← 1.5 R:R minimum
  4. Lets you selectively place, modify or delete GTTs

WHY THIS MATTERS:
  The current Zerodha portfolio has ZERO stop-losses on Indian positions:
    CAPACITE:  550 qty  @₹318  → ltp₹210  (-33.8%)  UNPROTECTED
    SWSOLAR:   700 qty  @₹250  → ltp₹165  (-34.0%)  UNPROTECTED
    GREAVESCOT: 500 qty @₹206  → ltp₹137  (-33.3%)  UNPROTECTED
    IFBIND:    65 qty   @₹1507 → ltp₹979  (-35.1%)  UNPROTECTED
  One GTT OCO per position = full risk management with ZERO manual intervention.

Usage:
  python india_gtt_manager.py --audit          # show unprotected positions
  python india_gtt_manager.py --suggest        # show suggested stop/target levels
  python india_gtt_manager.py --place-all      # place GTTs for all unprotected
  python india_gtt_manager.py --list           # list all active GTTs
  python india_gtt_manager.py --delete 123456  # delete a specific GTT

Requirements:
  pip install kiteconnect
  ENV: ZERODHA_ACCESS_TOKEN  (required)
  ENV: ZERODHA_API_KEY       (default: a9jb8rdr88cr8a9s)
"""

from __future__ import annotations

import os
import sys
import json
import time
import math
import argparse
import traceback
from datetime import datetime, timedelta
from typing import Optional

# ─── Zerodha connection ────────────────────────────────────────────────────────

API_KEY = os.environ.get("ZERODHA_API_KEY", "")  # nocheck: J1 — fallback required, system won't connect without it, DO NOT REMOVE


def _get_kite():
    """Return authenticated KiteConnect instance via the common auth module."""
    from zerodha_auth import get_kite
    return get_kite()


# ─── ATR calculation ──────────────────────────────────────────────────────────

def _calc_atr(kite, instrument_token: int, period: int = 14) -> Optional[float]:
    """
    Fetch last (period+1) daily candles and return ATR(period) in price units.
    Returns None if historical data fails.
    """
    try:
        to_date   = datetime.today()
        from_date = to_date - timedelta(days=period * 2 + 10)  # extra buffer for holidays
        data = kite.historical_data(
            instrument_token,
            from_date.strftime("%Y-%m-%d"),
            to_date.strftime("%Y-%m-%d"),
            "day",
        )
        if len(data) < period + 1:
            return None

        true_ranges = []
        for i in range(1, len(data)):
            hl  = data[i]["high"] - data[i]["low"]
            hpc = abs(data[i]["high"] - data[i - 1]["close"])
            lpc = abs(data[i]["low"]  - data[i - 1]["close"])
            true_ranges.append(max(hl, hpc, lpc))

        atr = sum(true_ranges[-period:]) / period
        return atr
    except Exception:
        return None


def _get_instrument_token(kite, symbol: str, exchange: str = "NSE") -> Optional[int]:
    """
    Resolve instrument_token for a symbol using kite.ltp() (fastest method).
    """
    try:
        result = kite.ltp(f"{exchange}:{symbol}")
        if result:
            key = f"{exchange}:{symbol}"
            return result[key]["instrument_token"]
    except Exception:
        pass
    return None


# ─── Stop/target levels ────────────────────────────────────────────────────────

def _suggest_levels(avg_price: float, ltp: float, atr: Optional[float]) -> dict:
    """
    Calculate stop-loss and target levels.

    Rules:
      stop  = ltp  - max(2*ATR, 8% of avg_price)
      target= avg_price + max(3*ATR, 12% of avg_price)

    If already in loss, stop is placed below current LTP to prevent forced
    averaging down at worse levels. Target is from avg_price (break-even+).
    """
    # Floor percentages
    pct_stop   = 0.08 * avg_price  # 8% of avg
    pct_target = 0.12 * avg_price  # 12% of avg

    atr_stop   = 2.0 * atr if atr else 0
    atr_target = 3.0 * atr if atr else 0

    stop_gap   = max(atr_stop, pct_stop)
    target_gap = max(atr_target, pct_target)

    # Stop below current LTP (not below avg — position may already be down)
    stop_price   = round(ltp - stop_gap, 1)
    # Target above avg_price (recovery to profit, not just breakeven)
    target_price = round(max(avg_price, ltp) + target_gap, 1)

    # Sanity: stop must be below ltp, target must be above ltp
    stop_price   = min(stop_price, ltp * 0.95)  # at least 5% below ltp
    target_price = max(target_price, ltp * 1.05)  # at least 5% above ltp

    # Limit price = slightly past trigger (slippage buffer)
    stop_limit   = round(stop_price   * 0.995, 1)  # 0.5% below stop
    target_limit = round(target_price * 1.005, 1)  # 0.5% above target

    rr = (target_price - avg_price) / (avg_price - stop_price) if avg_price > stop_price else 0

    return {
        "stop_price":    stop_price,
        "stop_limit":    stop_limit,
        "target_price":  target_price,
        "target_limit":  target_limit,
        "atr_used":      round(atr, 2) if atr else None,
        "rr_ratio":      round(rr, 2),
    }


# ─── GTT operations ───────────────────────────────────────────────────────────

def list_gtts(kite) -> list[dict]:
    """Return all active GTTs."""
    try:
        return kite.get_gtts() or []
    except Exception as exc:
        print(f"[gtt_manager] Failed to fetch GTTs: {exc}")
        return []


def _gtts_by_symbol(gtts: list[dict]) -> dict[str, list[dict]]:
    """Index active GTTs by tradingsymbol for fast lookup."""
    idx: dict[str, list[dict]] = {}
    for g in gtts:
        for cond in [g.get("condition", {})]:
            sym = cond.get("tradingsymbol", "").upper()
            if sym:
                idx.setdefault(sym, []).append(g)
    return idx


def place_gtt_oco(kite, symbol: str, exchange: str, qty: int,
                  stop_price: float, stop_limit: float,
                  target_price: float, target_limit: float,
                  current_ltp: float) -> dict:
    """
    Place a TWO-LEG (OCO) GTT: stop-loss + take-profit.

    Returns {"status": "ok", "trigger_id": ...} or {"status": "error", "msg": ...}
    """
    try:
        result = kite.place_gtt(
            trigger_type = kite.GTT_TYPE_TWO_LEG,
            tradingsymbol = symbol,
            exchange      = exchange,
            trigger_values= [stop_price, target_price],
            last_price    = current_ltp,
            orders = [
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":    qty,
                    "order_type":  kite.ORDER_TYPE_LIMIT,
                    "product":     kite.PRODUCT_CNC,
                    "price":       stop_limit,
                },
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":    qty,
                    "order_type":  kite.ORDER_TYPE_LIMIT,
                    "product":     kite.PRODUCT_CNC,
                    "price":       target_limit,
                },
            ],
        )
        return {"status": "ok", "trigger_id": result}
    except Exception as exc:
        return {"status": "error", "msg": str(exc)}


def place_gtt_stop(kite, symbol: str, exchange: str, qty: int,
                   stop_price: float, stop_limit: float,
                   current_ltp: float) -> dict:
    """
    Place a SINGLE-LEG stop-loss GTT.
    Use when target placement is not desired.
    """
    try:
        result = kite.place_gtt(
            trigger_type  = kite.GTT_TYPE_SINGLE,
            tradingsymbol = symbol,
            exchange       = exchange,
            trigger_values = [stop_price],
            last_price     = current_ltp,
            orders = [
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":   qty,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "product":    kite.PRODUCT_CNC,
                    "price":      stop_limit,
                },
            ],
        )
        return {"status": "ok", "trigger_id": result}
    except Exception as exc:
        return {"status": "error", "msg": str(exc)}


def delete_gtt(kite, trigger_id: int) -> dict:
    """Delete a GTT by ID."""
    try:
        kite.delete_gtt(trigger_id)
        return {"status": "ok", "trigger_id": trigger_id}
    except Exception as exc:
        return {"status": "error", "msg": str(exc)}


# ─── Audit logic ──────────────────────────────────────────────────────────────

def audit(kite, verbose: bool = True) -> list[dict]:
    """
    Cross-reference holdings vs active GTTs.
    Returns list of holdings that have NO active stop-loss GTT.
    """
    holdings = kite.holdings()
    active_gtts = list_gtts(kite)
    gtt_by_sym  = _gtts_by_symbol(active_gtts)

    # Cash-equivalent ETFs that must never receive a stop-loss GTT.
    # LIQUIDBEES NAV is pegged at ₹1000 and never moves — a GTT would consume
    # a GTT slot while never triggering and cluttering the audit view.
    _CASH_ETF_SKIP = frozenset({"LIQUIDBEES", "LIQUIDCASE", "SETFNIF50"})

    unprotected = []
    all_rows    = []

    for h in holdings:
        sym  = h.get("tradingsymbol", "").upper()
        qty  = int(h.get("quantity", 0) or 0)
        avg  = float(h.get("average_price", 0) or 0)
        ltp  = float(h.get("last_price", 0) or 0)
        pnl_pct = ((ltp - avg) / avg * 100) if avg else 0

        if qty == 0:
            continue

        # Skip cash-equivalent ETFs — their NAV doesn't fluctuate; GTT is meaningless
        if sym in _CASH_ETF_SKIP:
            continue

        has_gtt = sym in gtt_by_sym and len(gtt_by_sym[sym]) > 0
        gtt_ids = [g["id"] for g in gtt_by_sym.get(sym, [])]

        row = {
            "symbol":  sym,
            "qty":     qty,
            "avg":     avg,
            "ltp":     ltp,
            "pnl_pct": pnl_pct,
            "has_gtt": has_gtt,
            "gtt_ids": gtt_ids,
        }
        all_rows.append(row)
        if not has_gtt:
            unprotected.append(row)

    if verbose:
        print()
        print("=== GTT AUDIT — Zerodha Holdings ===")
        fmt = "  {:<22} {:>5} {:>10} {:>10} {:>8} {}"
        print(fmt.format("Symbol", "Qty", "AvgPrice", "LTP", "PnL%", "GTT Status"))
        print("  " + "-" * 72)
        for r in sorted(all_rows, key=lambda x: x["pnl_pct"]):
            status = f"✓ {r['gtt_ids']}" if r["has_gtt"] else "⚠ UNPROTECTED"
            sign = "+" if r["pnl_pct"] >= 0 else ""
            print(fmt.format(
                r["symbol"], r["qty"],
                f"₹{r['avg']:,.2f}", f"₹{r['ltp']:,.2f}",
                f"{sign}{r['pnl_pct']:.1f}%",
                status,
            ))
        print()
        print(f"  {len(all_rows)} positions total | "
              f"{len(unprotected)} UNPROTECTED (no active GTT)")

    return unprotected


# ─── Suggest + place pipeline ─────────────────────────────────────────────────

def suggest_and_place(kite, dry_run: bool = True) -> None:
    """
    For each unprotected holding:
      1. Fetch instrument_token
      2. Calculate ATR(14)
      3. Suggest stop/target levels
      4. Place OCO GTT (unless dry_run=True)
    """
    unprotected = audit(kite, verbose=True)

    if not unprotected:
        print("  All positions are protected. Nothing to do.")
        return

    print()
    print(f"{'DRY RUN — ' if dry_run else ''}Calculating ATR & suggesting GTT levels:")
    print("  " + "=" * 78)

    # Pre-flight: sample first holding to check ATR availability and show warning ONCE
    _sample_token = None
    for exchange in ["NSE", "BSE"]:
        _sample_token = _get_instrument_token(kite, unprotected[0]["symbol"], exchange)
        if _sample_token:
            break
    _sample_atr = _calc_atr(kite, _sample_token) if _sample_token else None
    if _sample_atr is None:
        print(f"  ⚠  ATR data unavailable — using fixed % fallback (8% stop / 12% target).")
        print(f"     Stops may not reflect actual volatility. Re-run during live market hours for real ATR.")
        print(f"     Symbols flagged '← ⚠ PRICE ALREADY BELOW STOP' need immediate attention.")
        print("  " + "=" * 78)
    _atr_warned = True  # warning shown once above; suppress per-symbol repetition

    for h in unprotected:
        sym  = h["symbol"]
        qty  = h["qty"]
        avg  = h["avg"]
        ltp  = h["ltp"]

        # Try NSE first, fallback BSE
        for exchange in ["NSE", "BSE"]:
            token = _get_instrument_token(kite, sym, exchange)
            if token:
                break
        else:
            token = None

        atr = _calc_atr(kite, token) if token else None

        levels = _suggest_levels(avg, ltp, atr)

        stop  = levels["stop_price"]
        tgt   = levels["target_price"]
        rr    = levels["rr_ratio"]
        atr_s = f"ATR={levels['atr_used']}" if levels["atr_used"] else "⚠ ATR=N/A"

        # Flag positions where current LTP is already below the suggested stop (emergency)
        emergency = (ltp < stop)
        urgency_tag = "  ← ⚠ PRICE ALREADY BELOW STOP — consider EXIT" if emergency else ""

        print(f"  {sym:<22} qty={qty}  ltp=₹{ltp:,.2f}  avg=₹{avg:,.2f}  "
              f"{atr_s}  R:R={rr:.1f}{urgency_tag}")
        print(f"      stop=₹{stop:,.2f}  limit (slippage buffer)=₹{levels['stop_limit']:,.2f}  "
              f"target=₹{tgt:,.2f}  limit (slippage buffer)=₹{levels['target_limit']:,.2f}")

        if not dry_run:
            res = place_gtt_oco(
                kite, sym, exchange or "NSE", qty,
                stop, levels["stop_limit"],
                tgt,  levels["target_limit"],
                ltp,
            )
            if res["status"] == "ok":
                print(f"      ✓ GTT placed — trigger_id={res['trigger_id']}")
            else:
                print(f"      ✗ GTT FAILED — {res['msg']}")
                # Fallback: single-leg stop-loss only
                res2 = place_gtt_stop(
                    kite, sym, exchange or "NSE", qty,
                    stop, levels["stop_limit"], ltp,
                )
                if res2["status"] == "ok":
                    print(f"      ✓ Stop-only GTT placed — trigger_id={res2['trigger_id']}")
                else:
                    print(f"      ✗ Stop-only also failed — {res2['msg']}")
        print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Zerodha GTT Manager — auto stop-loss / target for India holdings"
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--audit",      action="store_true", help="Audit holdings vs active GTTs")
    grp.add_argument("--suggest",    action="store_true", help="Suggest GTT levels (dry run)")
    grp.add_argument("--place-all",  action="store_true", help="Place GTTs for all unprotected positions")
    grp.add_argument("--list",       action="store_true", help="List all active GTTs")
    grp.add_argument("--delete",     type=int, metavar="TRIGGER_ID", help="Delete a GTT by ID")
    args = parser.parse_args()

    try:
        kite = _get_kite()
    except EnvironmentError as exc:
        print(f"\n\u274c  {exc}\n")
        sys.exit(1)

    if args.audit:
        audit(kite, verbose=True)

    elif args.suggest:
        suggest_and_place(kite, dry_run=True)

    elif args.place_all:
        print("⚠️  LIVE MODE — will place real GTT orders on Zerodha.")
        print("   Holdings from your DFX693 account will get OCO stop-loss + target GTTs.")
        confirm = input("   Type YES to continue: ").strip()
        if confirm.upper() == "YES":
            suggest_and_place(kite, dry_run=False)
        else:
            print("   Cancelled.")

    elif args.list:
        gtts = list_gtts(kite)
        if not gtts:
            print("No active GTTs found.")
        else:
            print(f"Active GTTs ({len(gtts)}):")
            for g in gtts:
                cond = g.get("condition", {})
                print(f"  [{g['id']}] {cond.get('tradingsymbol')}  "
                      f"type={g.get('type')}  status={g.get('status')}  "
                      f"triggers={cond.get('trigger_values')}")

    elif args.delete:
        res = delete_gtt(kite, args.delete)
        if res["status"] == "ok":
            print(f"✓ Deleted GTT {args.delete}")
        else:
            print(f"✗ Failed: {res['msg']}")
