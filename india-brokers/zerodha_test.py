# coding: utf-8
"""
Zerodha Kite Connect — Account Test Script
Mirrors moomoo_test.py pattern: connects to the broker API and dumps
account info, holdings, positions, orders, and trades.

Auth flow (Kite Connect v3):
  1. Browser-based OAuth — user visits login URL, gets redirected with request_token
  2. Script exchanges request_token → access_token (valid until 6 AM next day)

ENV VARS (set in .env or OS environment):
  ZERODHA_API_KEY        — your Kite Connect app's api_key (required)
  ZERODHA_API_SECRET     — your api_secret (required when generating a new session)
  ZERODHA_ACCESS_TOKEN   — cached access_token (skip login if set)

Usage:
  python zerodha_test.py               # full auth + data pull
  ZERODHA_ACCESS_TOKEN=xxx python zerodha_test.py   # skip login with cached token
"""

import sys
import os
import json

# Fix Windows console encoding
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    try: sys.stderr.reconfigure(encoding="utf-8")
    except Exception: pass

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("[FAIL] kiteconnect not installed. Run: pip install kiteconnect")
    sys.exit(1)

DIVIDER = "-" * 65

# ── Credentials from environment ────────────────────────────────────────────
API_KEY        = os.environ.get("ZERODHA_API_KEY", "").strip()
API_SECRET     = os.environ.get("ZERODHA_API_SECRET", "").strip()
ACCESS_TOKEN   = os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip()

print("=" * 65)
print("  Zerodha Kite Connect Test  (kiteconnect)")
print("=" * 65)

if not API_KEY:
    print("\n[ERROR] ZERODHA_API_KEY not set.")
    print("  Set it as an environment variable:")
    print("  Windows: set ZERODHA_API_KEY=your_api_key")
    print("  Linux/macOS: export ZERODHA_API_KEY=your_api_key")
    sys.exit(1)

kite = KiteConnect(api_key=API_KEY)

# ── Authentication ────────────────────────────────────────────────────────────
if ACCESS_TOKEN:
    print(f"\n[OK] Using cached access_token from env (ZERODHA_ACCESS_TOKEN)")
    kite.set_access_token(ACCESS_TOKEN)
else:
    if not API_SECRET:
        print("\n[ERROR] ZERODHA_API_SECRET not set — needed to generate a new session.")
        print("  Set ZERODHA_API_SECRET, or set ZERODHA_ACCESS_TOKEN to skip login.")
        sys.exit(1)

    print("\n" + DIVIDER)
    print("  AUTHENTICATION — Browser OAuth Flow")
    print(DIVIDER)
    login_url = kite.login_url()
    print(f"\nStep 1: Open this URL in your browser and log in:")
    print(f"  {login_url}")
    print(f"\nStep 2: After login, you'll be redirected to your redirect URL.")
    print(f"        Copy the 'request_token' value from the URL query params.")
    request_token = input("\nPaste request_token here: ").strip()

    if not request_token:
        print("[ERROR] No request_token provided. Exiting.")
        sys.exit(1)

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)
        print(f"\n[OK] Session generated. access_token: {ACCESS_TOKEN[:12]}...")
        print(f"\n[TIP] To skip login next time, set:")
        print(f"  set ZERODHA_ACCESS_TOKEN={ACCESS_TOKEN}")
    except Exception as e:
        print(f"[FAIL] Session generation failed: {e}")
        sys.exit(1)

# ── User Profile ─────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  USER PROFILE")
print(DIVIDER)
try:
    profile = kite.profile()
    print(f"  User ID   : {profile.get('user_id', 'N/A')}")
    print(f"  Name      : {profile.get('user_name', 'N/A')}")
    print(f"  Email     : {profile.get('email', 'N/A')}")
    print(f"  Broker    : {profile.get('broker', 'N/A')}")
    print(f"  Exchanges : {', '.join(profile.get('exchanges', []))}")
    print(f"  Products  : {', '.join(profile.get('products', []))}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Funds & Margins ───────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  FUNDS & MARGINS")
print(DIVIDER)
try:
    margins = kite.margins()
    for segment, data in margins.items():
        net   = data.get("net", 0)
        cash  = data.get("available", {}).get("cash", 0)
        used  = data.get("utilised", {}).get("debits", 0)
        print(f"  [{segment.upper():10}]  net={net:>14,.2f}  "
              f"cash={cash:>14,.2f}  used={used:>14,.2f}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Holdings ──────────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  HOLDINGS  (long-term equity / DEMAT)")
print(DIVIDER)
try:
    holdings = kite.holdings()
    if not holdings:
        print("  No holdings.")
    else:
        fmt = "  {:<20} {:>6} {:>12} {:>12} {:>10}"
        print(fmt.format("Symbol", "Qty", "Avg Price", "LTP", "P&L"))
        print("  " + "-" * 62)
        total_pnl = 0
        for h in holdings:
            sym   = h.get("tradingsymbol", "?")
            qty   = h.get("quantity", 0)
            avg   = h.get("average_price", 0)
            ltp   = h.get("last_price", 0)
            pnl   = h.get("pnl", 0)
            total_pnl += pnl
            print(fmt.format(sym[:20], qty, f"{avg:,.2f}", f"{ltp:,.2f}", f"{pnl:,.2f}"))
        print("  " + "-" * 62)
        print(fmt.format("TOTAL P&L", "", "", "", f"{total_pnl:,.2f}"))
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Positions ─────────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  POSITIONS  (intraday/F&O — net)")
print(DIVIDER)
try:
    positions = kite.positions()
    net = positions.get("net", [])
    if not net:
        print("  No open positions.")
    else:
        fmt = "  {:<20} {:<6} {:>6} {:>12} {:>12} {:>10}"
        print(fmt.format("Symbol", "Exch", "Qty", "Avg Price", "LTP", "P&L"))
        print("  " + "-" * 68)
        for p in net:
            sym   = p.get("tradingsymbol", "?")
            exch  = p.get("exchange", "?")
            qty   = p.get("quantity", 0)
            avg   = p.get("average_price", 0)
            ltp   = p.get("last_price", 0)
            pnl   = p.get("pnl", 0)
            print(fmt.format(sym[:20], exch[:6], qty, f"{avg:,.2f}", f"{ltp:,.2f}", f"{pnl:,.2f}"))
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Open Orders ───────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  OPEN ORDERS (today)")
print(DIVIDER)
try:
    orders = kite.orders()
    open_orders = [o for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")]
    if not open_orders:
        print("  No open orders.")
    else:
        fmt = "  {:<16} {:<20} {:<5} {:>6} {:>12} {:<10}"
        print(fmt.format("Order ID", "Symbol", "Side", "Qty", "Price", "Status"))
        print("  " + "-" * 72)
        for o in open_orders:
            print(fmt.format(
                str(o.get("order_id", "?"))[:16],
                o.get("tradingsymbol", "?")[:20],
                o.get("transaction_type", "?")[:5],
                o.get("quantity", 0),
                f"{o.get('price', 0):,.2f}",
                o.get("status", "?")[:10]
            ))
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Today's Trades ────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  TODAY'S TRADES (executed)")
print(DIVIDER)
try:
    trades = kite.trades()
    if not trades:
        print("  No trades today.")
    else:
        fmt = "  {:<16} {:<20} {:<5} {:>6} {:>12} {:<22}"
        print(fmt.format("Trade ID", "Symbol", "Side", "Qty", "Price", "Fill Time"))
        print("  " + "-" * 82)
        for t in trades:
            print(fmt.format(
                str(t.get("trade_id", "?"))[:16],
                t.get("tradingsymbol", "?")[:20],
                t.get("transaction_type", "?")[:5],
                t.get("filled", 0),
                f"{t.get('average_price', 0):,.2f}",
                str(t.get("fill_timestamp", "?"))[:22]
            ))
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print("  Done. All Zerodha Kite Connect data pulled successfully.")
print("=" * 65)
