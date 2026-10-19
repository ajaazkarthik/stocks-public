"""
Angel One GTT Manager — Stop-Loss / Target Automation for Angel One Holdings
=============================================================================
GTT = Good Till Triggered. Unlike normal orders (which expire same day),
GTT orders persist ACROSS sessions until triggered, expired, or cancelled.

Angel One SmartAPI supports single-trigger GTT rules (one price per rule).
Since OCO (One Cancels Other) is NOT supported, this script places TWO
separate rules per position:
  RULE 1 (stop-loss)  → SELL if price drops to stop level
  RULE 2 (target)     → SELL if price rises to target level

NOTE: If one triggers, the other remains active. You must cancel it manually
      or use --cleanup to cancel stale GTTs after a fill.

API: https://apiconnect.angelone.in/rest/secure/angelbroking/gtt/v1/
Docs: https://smartapi.angelbroking.com/docs/Gtt

Stop/Target formula (mirrors Zerodha GTT manager):
  stop   = ltp  - max(2×ATR14, 8% of avg_price)
  target = avg_price + max(3×ATR14, 12% of avg_price)
  ATR uses Angel One Historical API (daily candles). Falls back to % if unavailable.

Usage:
  python angelone_gtt_manager.py --audit          # show unprotected positions
  python angelone_gtt_manager.py --suggest        # show suggested stop/target levels
  python angelone_gtt_manager.py --place-all      # place stop+target GTTs for all unprotected
  python angelone_gtt_manager.py --place RELIANCE # place for a single symbol
  python angelone_gtt_manager.py --list           # list all active GTTs
  python angelone_gtt_manager.py --cancel 123456  # cancel a GTT rule by ID
  python angelone_gtt_manager.py --cleanup        # cancel GTTs for fully-exited positions

Requirements:
  pip install smartapi-python pyotp requests
  ENV: ANGELONE_API_KEY      (default: dkozr7EU)
  ENV: ANGELONE_CLIENT_CODE  (default: K51989642)
  ENV: ANGELONE_PIN          (default: 2468)
  ENV: ANGELONE_TOTP_SECRET  (default: 6XZS25XVVSWCICOYQR45VDQCME)
"""

from __future__ import annotations

import os
import sys
import json
import time
import math
import argparse
import traceback
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

# ─── Angel One credentials ────────────────────────────────────────────────────
AO_API_KEY     = os.environ.get("ANGELONE_API_KEY",     "")
AO_CLIENT_CODE = os.environ.get("ANGELONE_CLIENT_CODE", "")
AO_PIN         = os.environ.get("ANGELONE_PIN",         "")
AO_TOTP_SECRET = os.environ.get("ANGELONE_TOTP_SECRET", "")

BASE_URL = "https://apiconnect.angelone.in"

# ─────────────────────────────────────────────────────────────────────────────
# Session state (module-level singleton)
# ─────────────────────────────────────────────────────────────────────────────
_smart    = None
_jwt      = None
_last_auth: float = 0.0
SESSION_TTL = 1200  # seconds


def _auth() -> bool:
    """Authenticate / refresh Angel One SmartAPI session. Returns True on success."""
    global _smart, _jwt, _last_auth
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError:
        print("  pip install smartapi-python pyotp")
        return False
    import logging as _l; _l.getLogger("logzero_default").setLevel(_l.CRITICAL)
    try:
        totp  = pyotp.TOTP(AO_TOTP_SECRET).now()
        smart = SmartConnect(api_key=AO_API_KEY)
        resp  = smart.generateSession(AO_CLIENT_CODE, AO_PIN, totp)
        if resp.get("status") and resp.get("data", {}).get("jwtToken"):
            _smart     = smart
            _jwt       = resp["data"]["jwtToken"].removeprefix("Bearer ").strip()
            _last_auth = time.time()
            return True
        print(f"  Angel One auth failed: {resp.get('message', '')}")
        return False
    except Exception as exc:
        print(f"  Angel One auth error: {exc}")
        return False


def _ensure_auth() -> bool:
    if time.time() - _last_auth > SESSION_TTL or _smart is None:
        return _auth()
    return True


def _headers() -> dict:
    return {
        "X-PrivateKey":     AO_API_KEY,
        "Accept":           "application/json",
        "X-SourceID":       "WEB",
        "X-ClientLocalIP":  "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress":     "AA:BB:CC:DD:EE:FF",
        "X-UserType":       "USER",
        "Authorization":    f"Bearer {_jwt}",
        "Content-Type":     "application/json",
    }


def _post(path: str, body: dict) -> dict:
    url  = BASE_URL + path
    data = json.dumps(body).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"status": False, "message": str(exc), "data": None}


def _post_with_reauth(path: str, body: dict) -> dict:
    """POST with one automatic re-auth retry on session expiry."""
    resp = _post(path, body)
    if not resp.get("status"):
        msg = str(resp.get("message", "")).lower()
        if any(k in msg for k in ("invalid token", "unauthorized", "session expired")):
            global _last_auth
            _last_auth = 0
            if _auth():
                resp = _post(path, body)
    return resp


# ─── Holdings ─────────────────────────────────────────────────────────────────

def get_holdings() -> list[dict]:
    """Return Angel One holdings (demat positions only, qty > 0)."""
    try:
        resp = _smart.holding()
        raw  = resp.get("data") or []
        holdings = []
        for h in raw:
            qty = int(h.get("quantity", 0) or 0)
            if qty > 0:
                holdings.append({
                    "symbol":        h.get("tradingsymbol", "").upper().replace("-EQ", ""),
                    "tradingsymbol": h.get("tradingsymbol", ""),
                    "symboltoken":   str(h.get("symboltoken", "")),
                    "exchange":      h.get("exchange", "NSE"),
                    "qty":           qty,
                    "avg":           float(h.get("averageprice", 0) or 0),
                    "ltp":           float(h.get("ltp", 0) or 0),
                    "pnl":           float(h.get("profitandloss", 0) or 0),
                })
        return holdings
    except Exception as exc:
        print(f"  Failed to fetch holdings: {exc}")
        return []


# ─── Symbol token resolution ──────────────────────────────────────────────────

def search_scrip(symbol: str, exchange: str = "NSE") -> Optional[str]:
    """
    Search Angel One for a symbol and return its symboltoken.
    Uses searchScrip API — does NOT require symboltoken ahead of time.
    """
    resp = _post_with_reauth(
        "/rest/secure/angelbroking/order/v1/searchScrip",
        {"exchange": exchange, "searchscrip": symbol},
    )
    for item in (resp.get("data") or []):
        ts = item.get("tradingsymbol", "")
        # prefer the EQ variant for equity
        if ts.upper() == f"{symbol.upper()}-EQ" or ts.upper() == symbol.upper():
            return str(item.get("symboltoken", ""))
    # fallback: first NSE result
    items = resp.get("data") or []
    if items:
        return str(items[0].get("symboltoken", ""))
    return None


def _resolve_token_and_sym(symbol: str, exchange: str = "NSE") -> tuple[Optional[str], str]:
    """
    Resolve (symboltoken, tradingsymbol) for a bare symbol like 'RELIANCE'.
    Checks holdings cache first, then searchScrip.
    Returns (None, '') on failure.
    """
    # try Angel One searchScrip
    tok = search_scrip(symbol, exchange)
    tsym = f"{symbol.upper()}-EQ" if exchange == "NSE" else symbol.upper()
    return tok, tsym


# ─── Historical data + ATR ────────────────────────────────────────────────────

def _calc_atr(symboltoken: str, exchange: str = "NSE", period: int = 14) -> Optional[float]:
    """
    Fetch last (period×2 + 10) daily candles from Angel One Historical API
    and return ATR(period) in price units.
    Returns None on failure.

    Endpoint: POST /rest/secure/angelbroking/historical/v1/getCandleData
    """
    try:
        to_date   = datetime.today()
        from_date = to_date - timedelta(days=period * 2 + 10)
        body = {
            "exchange":    exchange,
            "symboltoken": symboltoken,
            "interval":    "ONE_DAY",
            "fromdate":    from_date.strftime("%Y-%m-%d 09:15"),
            "todate":      to_date.strftime("%Y-%m-%d 15:30"),
        }
        resp = _post_with_reauth(
            "/rest/secure/angelbroking/historical/v1/getCandleData", body
        )
        candles = resp.get("data") or []
        # Each candle: [timestamp, open, high, low, close, volume]
        if len(candles) < period + 1:
            return None

        true_ranges = []
        for i in range(1, len(candles)):
            h   = float(candles[i][2])
            lo  = float(candles[i][3])
            pc  = float(candles[i - 1][4])
            true_ranges.append(max(h - lo, abs(h - pc), abs(lo - pc)))

        atr = sum(true_ranges[-period:]) / period
        return atr
    except Exception:
        return None


# ─── Stop/target levels ────────────────────────────────────────────────────────

def _suggest_levels(avg_price: float, ltp: float, atr: Optional[float]) -> dict:
    """
    Same formula as Zerodha GTT manager.
      stop   = ltp - max(2×ATR, 8% of avg_price)
      target = max(avg, ltp) + max(3×ATR, 12% of avg_price)
    """
    pct_stop   = 0.08 * avg_price
    pct_target = 0.12 * avg_price
    atr_stop   = 2.0 * atr if atr else 0
    atr_target = 3.0 * atr if atr else 0

    stop_gap   = max(atr_stop, pct_stop)
    target_gap = max(atr_target, pct_target)

    stop_price   = round(ltp - stop_gap, 1)
    target_price = round(max(avg_price, ltp) + target_gap, 1)

    stop_price   = min(stop_price, ltp * 0.95)
    target_price = max(target_price, ltp * 1.05)

    stop_limit   = round(stop_price   * 0.995, 1)
    target_limit = round(target_price * 1.005, 1)

    rr = (target_price - avg_price) / (avg_price - stop_price) if avg_price > stop_price else 0

    return {
        "stop_price":    stop_price,
        "stop_limit":    stop_limit,
        "target_price":  target_price,
        "target_limit":  target_limit,
        "atr_used":      round(atr, 2) if atr else None,
        "rr_ratio":      round(rr, 2),
    }


# ─── GTT CRUD ─────────────────────────────────────────────────────────────────

def list_gtts(status_filter: Optional[list[str]] = None) -> list[dict]:
    """
    Return Angel One GTT rules.
    status_filter: list of statuses e.g. ["NEW","ACTIVE"] or None → ["FORALL"]
    """
    statuses = status_filter or ["FORALL"]
    resp = _post_with_reauth(
        "/rest/secure/angelbroking/gtt/v1/ruleList",
        {"status": statuses, "page": 1, "count": 100},
    )
    return resp.get("data") or []


def _gtts_by_symbol(gtts: list[dict]) -> dict[str, list[dict]]:
    """Index GTTs by base symbol (e.g. 'RELIANCE') for fast lookup."""
    idx: dict[str, list[dict]] = {}
    for g in gtts:
        raw = str(g.get("tradingsymbol", "")).upper().replace("-EQ", "")
        idx.setdefault(raw, []).append(g)
    return idx


def place_gtt_stop(tradingsymbol: str, symboltoken: str, exchange: str,
                   qty: int, stop_trigger: float, stop_limit: float,
                   current_ltp: float) -> dict:
    """
    Place a SELL stop-loss GTT rule.
    Triggers when price drops to stop_trigger; executes at stop_limit.
    """
    body = {
        "tradingsymbol":  tradingsymbol,
        "symboltoken":    symboltoken,
        "exchange":       exchange,
        "transactiontype": "SELL",
        "producttype":    "DELIVERY",
        "price":          str(stop_limit),
        "qty":            str(qty),
        "triggerprice":   str(stop_trigger),
        "disclosedqty":   "0",
    }
    resp = _post_with_reauth("/rest/secure/angelbroking/gtt/v1/createRule", body)
    if resp.get("status"):
        rule_id = (resp.get("data") or {}).get("id", "?")
        return {"status": "ok", "rule_id": rule_id, "type": "stop"}
    return {"status": "error", "msg": resp.get("message", str(resp))}


def place_gtt_target(tradingsymbol: str, symboltoken: str, exchange: str,
                     qty: int, target_trigger: float, target_limit: float,
                     current_ltp: float) -> dict:
    """
    Place a SELL target (take-profit) GTT rule.
    Triggers when price rises to target_trigger; executes at target_limit.
    """
    body = {
        "tradingsymbol":  tradingsymbol,
        "symboltoken":    symboltoken,
        "exchange":       exchange,
        "transactiontype": "SELL",
        "producttype":    "DELIVERY",
        "price":          str(target_limit),
        "qty":            str(qty),
        "triggerprice":   str(target_trigger),
        "disclosedqty":   "0",
    }
    resp = _post_with_reauth("/rest/secure/angelbroking/gtt/v1/createRule", body)
    if resp.get("status"):
        rule_id = (resp.get("data") or {}).get("id", "?")
        return {"status": "ok", "rule_id": rule_id, "type": "target"}
    return {"status": "error", "msg": resp.get("message", str(resp))}


def cancel_gtt(rule_id: str, symboltoken: str, exchange: str) -> dict:
    """Cancel a GTT rule by ID."""
    body = {"id": str(rule_id), "symboltoken": symboltoken, "exchange": exchange}
    resp = _post_with_reauth("/rest/secure/angelbroking/gtt/v1/cancelRule", body)
    if resp.get("status"):
        return {"status": "ok", "rule_id": rule_id}
    return {"status": "error", "msg": resp.get("message", str(resp))}


# ─── Audit ────────────────────────────────────────────────────────────────────

def audit(verbose: bool = True) -> list[dict]:
    """
    Cross-reference Angel One holdings vs active GTT rules.
    Returns list of holdings with NO active stop-loss GTT.
    """
    holdings    = get_holdings()
    active_gtts = list_gtts(["NEW", "ACTIVE"])
    gtt_by_sym  = _gtts_by_symbol(active_gtts)

    all_rows    = []
    unprotected = []

    for h in holdings:
        sym     = h["symbol"]
        has_gtt = sym in gtt_by_sym
        gtt_ids = [g.get("id") for g in gtt_by_sym.get(sym, [])]
        pnl_pct = ((h["ltp"] - h["avg"]) / h["avg"] * 100) if h["avg"] else 0

        row = {**h, "has_gtt": has_gtt, "gtt_ids": gtt_ids, "pnl_pct": pnl_pct}
        all_rows.append(row)
        if not has_gtt:
            unprotected.append(row)

    if verbose:
        print()
        print("=== GTT AUDIT — Angel One Holdings ===")
        fmt = "  {:<22} {:>5} {:>10} {:>10} {:>8} {}"
        print(fmt.format("Symbol", "Qty", "AvgPrice", "LTP", "PnL%", "GTT Status"))
        print("  " + "-" * 72)
        for r in sorted(all_rows, key=lambda x: x["pnl_pct"]):
            status = f"✓ ids={r['gtt_ids']}" if r["has_gtt"] else "⚠ UNPROTECTED"
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
        if unprotected:
            total_exposure = sum(r["ltp"] * r["qty"] for r in unprotected)
            print(f"  Exposure at risk: \u20b9{total_exposure:,.0f}")
            if len(unprotected) == len(all_rows):
                print()
                print(f"  \U0001f534 CRITICAL: ALL {len(all_rows)} positions are UNPROTECTED!")
                print(f"     Run --suggest then --place-all to add stop-losses immediately.")
            else:
                print(f"  \u26a0\ufe0f  Run --suggest then --place-all to protect unprotected positions.")
    return unprotected


# ─── Suggest + place pipeline ─────────────────────────────────────────────────

def _process_symbol(h: dict, dry_run: bool = True) -> None:
    """Suggest levels + optionally place both stop and target GTT rules."""
    sym      = h["symbol"]
    tsym     = h.get("tradingsymbol") or f"{sym}-EQ"
    tok      = h.get("symboltoken", "")
    exchange = h.get("exchange", "NSE")
    qty      = h["qty"]
    avg      = h["avg"]
    ltp      = h["ltp"]

    # Ensure we have a token
    if not tok:
        tok, tsym = _resolve_token_and_sym(sym, exchange)
        tok = tok or ""

    atr = _calc_atr(tok, exchange) if tok else None
    levels = _suggest_levels(avg, ltp, atr)

    stop = levels["stop_price"]
    tgt  = levels["target_price"]
    rr   = levels["rr_ratio"]
    atr_s = f"ATR={levels['atr_used']}" if levels["atr_used"] else "⚠ ATR=N/A"

    print(f"  {sym:<22} qty={qty}  ltp=₹{ltp:,.2f}  avg=₹{avg:,.2f}  "
          f"{atr_s}  R:R={rr:.1f}")
    print(f"      stop=₹{stop:,.2f}  limit (slippage buffer)=₹{levels['stop_limit']:,.2f}  "
          f"target=₹{tgt:,.2f}  limit (slippage buffer)=₹{levels['target_limit']:,.2f}")
    if not levels["atr_used"]:
        print(f"      ⚠  ATR data unavailable — using fixed % rules (8% stop / 12% target).")
        print(f"         Stops may not reflect actual volatility. Re-run during live market hours for real ATR.")

    if not dry_run:
        if not tok:
            print(f"      ✗ Cannot place — symboltoken unknown for {sym}")
            return
        # Place stop-loss rule
        res = place_gtt_stop(tsym, tok, exchange, qty,
                             stop, levels["stop_limit"], ltp)
        if res["status"] == "ok":
            print(f"      ✓ Stop-loss GTT placed — rule_id={res['rule_id']}")
        else:
            print(f"      ✗ Stop-loss GTT FAILED — {res['msg']}")

        # Place target rule
        res2 = place_gtt_target(tsym, tok, exchange, qty,
                                tgt, levels["target_limit"], ltp)
        if res2["status"] == "ok":
            print(f"      ✓ Target GTT placed   — rule_id={res2['rule_id']}")
        else:
            print(f"      ✗ Target GTT FAILED   — {res2['msg']}")
    print()


def suggest_and_place(target_symbol: Optional[str] = None, dry_run: bool = True) -> None:
    """
    For each unprotected holding (or a specific symbol), calculate levels and optionally place GTTs.
    """
    all_unprotected = audit(verbose=True)

    if target_symbol:
        sym_upper = target_symbol.upper().replace("-EQ", "")
        rows = [r for r in all_unprotected if r["symbol"] == sym_upper]
        if not rows:
            # Check if it's already protected
            holdings = get_holdings()
            match = [h for h in holdings if h["symbol"] == sym_upper]
            if match:
                print(f"\n  {sym_upper} already has GTT protection — use --list to review.")
            else:
                print(f"\n  {sym_upper} not found in Angel One holdings.")
            return
    else:
        rows = all_unprotected

    if not rows:
        print("\n  All positions are protected. Nothing to do.")
        return

    print()
    print(f"{'DRY RUN — ' if dry_run else ''}Calculating ATR & suggesting GTT levels:")
    print("  " + "=" * 78)

    for h in rows:
        _process_symbol(h, dry_run=dry_run)


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup_stale_gtts() -> None:
    """Cancel GTT rules for symbols no longer held (position fully exited)."""
    holdings    = get_holdings()
    held_syms   = {h["symbol"] for h in holdings}
    active_gtts = list_gtts(["NEW", "ACTIVE"])

    stale = [g for g in active_gtts
             if str(g.get("tradingsymbol", "")).upper().replace("-EQ", "") not in held_syms]

    if not stale:
        print("  No stale GTTs found.")
        return

    print(f"  Found {len(stale)} stale GTT rule(s) for exited positions:")
    for g in stale:
        print(f"    rule_id={g.get('id')}  sym={g.get('tradingsymbol')}  "
              f"trigger=₹{g.get('triggerprice')}")

    confirm = input("\n  Cancel all stale GTTs? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    for g in stale:
        res = cancel_gtt(str(g.get("id", "")), str(g.get("symboltoken", "")),
                         g.get("exchange", "NSE"))
        sym = str(g.get("tradingsymbol", ""))
        if res["status"] == "ok":
            print(f"  ✓ Cancelled rule {g.get('id')} ({sym})")
        else:
            print(f"  ✗ Failed to cancel {g.get('id')} ({sym}): {res['msg']}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Angel One GTT Manager — auto stop-loss / target for Angel One holdings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--audit",     action="store_true", help="Audit holdings vs active GTTs")
    grp.add_argument("--suggest",   action="store_true", help="Suggest GTT levels (dry run)")
    grp.add_argument("--place-all", action="store_true", help="Place GTTs for ALL unprotected positions")
    grp.add_argument("--place",     metavar="SYMBOL",    help="Place GTTs for one symbol (e.g. RELIANCE)")
    grp.add_argument("--list",      action="store_true", help="List all active/new GTT rules")
    grp.add_argument("--cancel",    metavar="RULE_ID",   help="Cancel a GTT rule by ID")
    grp.add_argument("--cleanup",   action="store_true", help="Cancel GTTs for exited positions")
    args = parser.parse_args()

    if not _ensure_auth():
        print("\n✗ Angel One authentication failed. Check credentials.\n")
        sys.exit(1)

    if args.audit:
        audit(verbose=True)

    elif args.suggest:
        suggest_and_place(dry_run=True)

    elif args.place_all:
        print("⚠️  LIVE MODE — will place real GTT orders on Angel One.")
        print(f"   Account: {AO_CLIENT_CODE}")
        confirm = input("   Type YES to continue: ").strip()
        if confirm.upper() == "YES":
            suggest_and_place(dry_run=False)
        else:
            print("   Cancelled.")

    elif args.place:
        print(f"⚠️  LIVE MODE — placing GTT for {args.place.upper()} on Angel One.")
        confirm = input("   Type YES to continue: ").strip()
        if confirm.upper() == "YES":
            suggest_and_place(target_symbol=args.place, dry_run=False)
        else:
            print("   Cancelled.")

    elif args.list:
        gtts = list_gtts(["NEW", "ACTIVE", "SENTTOEXCHANGE"])
        if not gtts:
            print("No active GTTs found.")
        else:
            print(f"Active GTT rules ({len(gtts)}):")
            for g in gtts:
                sym = str(g.get("tradingsymbol", "")).replace("-EQ", "")
                print(f"  [{g.get('id')}] {sym:<20}  "
                      f"trigger=₹{g.get('triggerprice'):<10}  "
                      f"qty={g.get('qty')}  "
                      f"status={g.get('status')}  "
                      f"side={g.get('transactiontype')}")

    elif args.cancel:
        # Need symboltoken + exchange to cancel — look up from list
        all_gtts = list_gtts()
        match = next((g for g in all_gtts if str(g.get("id")) == str(args.cancel)), None)
        if not match:
            print(f"✗ Rule ID {args.cancel} not found in GTT list.")
            sys.exit(1)
        res = cancel_gtt(args.cancel,
                         str(match.get("symboltoken", "")),
                         match.get("exchange", "NSE"))
        if res["status"] == "ok":
            print(f"✓ Cancelled GTT rule {args.cancel}")
        else:
            print(f"✗ Failed: {res['msg']}")

    elif args.cleanup:
        cleanup_stale_gtts()
