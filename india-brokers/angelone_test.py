# coding: utf-8
"""
Angel One SmartAPI — Account Test Script
Mirrors moomoo_test.py pattern: connects to the broker API and dumps
account info, holdings, positions, orders, and trades.

Auth flow (SmartAPI):
  Fully programmatic — no browser needed.
  Uses client_code + trading_pin + live TOTP (from TOTP secret).
  Session valid until midnight.

ENV VARS (set in .env or OS environment):
  ANGELONE_API_KEY       — your SmartAPI app's api_key (required)
  ANGELONE_CLIENT_CODE   — your Angel One client code / user ID (required)
  ANGELONE_PIN           — your 4-digit trading PIN (required)
  ANGELONE_TOTP_SECRET   — the TOTP secret key / QR code value from Angel One (required)
                           Get it from: My Account → TOTP Settings → show QR secret

Usage:
  python angelone_test.py
"""

import sys
import os

# Fix Windows console encoding
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    try: sys.stderr.reconfigure(encoding="utf-8")
    except Exception: pass

try:
    from SmartApi import SmartConnect
except ImportError:
    print("[FAIL] smartapi-python not installed. Run: pip install smartapi-python pyotp logzero")
    sys.exit(1)

try:
    import pyotp
except ImportError:
    print("[FAIL] pyotp not installed. Run: pip install pyotp")
    sys.exit(1)

DIVIDER = "-" * 65

# ── Credentials from environment ────────────────────────────────────────────
API_KEY      = os.environ.get("ANGELONE_API_KEY", "").strip()
CLIENT_CODE  = os.environ.get("ANGELONE_CLIENT_CODE", "").strip()
PIN          = os.environ.get("ANGELONE_PIN", "").strip()
TOTP_SECRET  = os.environ.get("ANGELONE_TOTP_SECRET", "").strip()

print("=" * 65)
print("  Angel One SmartAPI Test  (smartapi-python)")
print("=" * 65)

missing = []
if not API_KEY:     missing.append("ANGELONE_API_KEY")
if not CLIENT_CODE: missing.append("ANGELONE_CLIENT_CODE")
if not PIN:         missing.append("ANGELONE_PIN")
if not TOTP_SECRET: missing.append("ANGELONE_TOTP_SECRET")

if missing:
    print("\n[ERROR] Missing required environment variables:")
    for m in missing:
        print(f"  {m}")
    print("\n  Set them as environment variables, for example:")
    print("  Windows:")
    print("    set ANGELONE_API_KEY=your_api_key")
    print("    set ANGELONE_CLIENT_CODE=your_client_code")
    print("    set ANGELONE_PIN=your_pin")
    print("    set ANGELONE_TOTP_SECRET=your_totp_secret")
    print("\n  Get TOTP secret from: My Account → TOTP Setup → reveal secret key")
    sys.exit(1)

# ── Authentication ────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  AUTHENTICATION")
print(DIVIDER)

try:
    totp = pyotp.TOTP(TOTP_SECRET).now()
except Exception as e:
    print(f"[FAIL] Invalid TOTP secret — cannot generate OTP: {e}")
    print("  Verify ANGELONE_TOTP_SECRET is the base32 secret, not the QR image URL.")
    sys.exit(1)

smart_api = SmartConnect(api_key=API_KEY)
try:
    session = smart_api.generateSession(CLIENT_CODE, PIN, totp)
except Exception as e:
    print(f"[FAIL] generateSession raised exception: {e}")
    sys.exit(1)

if not session or not session.get("status"):
    print(f"[FAIL] Login failed: {session.get('message', 'Unknown error')}")
    sys.exit(1)

print(f"[OK] Logged in as {CLIENT_CODE}")
jwt_token     = session["data"]["jwtToken"]
refresh_token = session["data"]["refreshToken"]
feed_token    = smart_api.getfeedToken()

# ── User Profile ─────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  USER PROFILE")
print(DIVIDER)
try:
    profile = smart_api.getProfile(refresh_token)
    if profile and profile.get("status"):
        d = profile.get("data", {})
        print(f"  Client Code : {d.get('clientcode', 'N/A')}")
        print(f"  Name        : {d.get('name', 'N/A')}")
        print(f"  Email       : {d.get('email', 'N/A')}")
        print(f"  Mobile      : {d.get('mobileno', 'N/A')}")
        print(f"  Exchanges   : {d.get('exchanges', 'N/A')}")
        print(f"  Products    : {d.get('products', 'N/A')}")
    else:
        print(f"  [WARN] {profile.get('message', 'No data')}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Funds & Margins ───────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  FUNDS & MARGINS (RMS)")
print(DIVIDER)
try:
    rms = smart_api.rmsLimit()
    if rms and rms.get("status"):
        d = rms.get("data", {})
        net      = float(d.get("net", 0) or 0)
        cash     = float(d.get("availablecash", 0) or 0)
        used     = float(d.get("utiliseddebits", 0) or 0)
        coll     = float(d.get("collateral", 0) or 0)
        print(f"  Net Balance      : {net:>14,.2f}")
        print(f"  Available Cash   : {cash:>14,.2f}")
        print(f"  Collateral       : {coll:>14,.2f}")
        print(f"  Utilised Debits  : {used:>14,.2f}")
    else:
        print(f"  [WARN] {rms.get('message', 'No data') if rms else 'No response'}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Holdings ──────────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  HOLDINGS  (long-term equity / DEMAT)")
print(DIVIDER)
try:
    result = smart_api.holding()
    holdings = []
    if result and result.get("status"):
        data = result.get("data", [])
        # getAllHolding returns {"holdings": [...], "totalholding": {...}}
        if isinstance(data, dict):
            holdings = data.get("holdings", [])
            th = data.get("totalholding", {})
            if th:
                print(f"  Total Value  : {float(th.get('totalholdingvalue', 0) or 0):>14,.2f}")
                print(f"  Total Inv    : {float(th.get('totalinvvalue', 0) or 0):>14,.2f}")
                print(f"  Total P&L    : {float(th.get('totalprofitandloss', 0) or 0):>14,.2f}  "
                      f"({float(th.get('totalpnlpercentage', 0) or 0):+.2f}%)")
                print()
        elif isinstance(data, list):
            holdings = data

    if not holdings:
        print("  No holdings.")
    else:
        fmt = "  {:<20} {:>6} {:>12} {:>12} {:>10} {:>8}"
        print(fmt.format("Symbol", "Qty", "Avg Price", "LTP", "P&L", "P&L%"))
        print("  " + "-" * 72)
        for h in holdings:
            sym  = h.get("tradingsymbol", "?")
            qty  = h.get("quantity", 0)
            avg  = float(h.get("averageprice", 0) or 0)
            ltp  = float(h.get("ltp", 0) or 0)
            pnl  = float(h.get("profitandloss", 0) or 0)
            pct  = float(h.get("pnlpercentage", 0) or 0)
            print(fmt.format(
                sym[:20], qty, f"{avg:,.2f}", f"{ltp:,.2f}", f"{pnl:,.2f}", f"{pct:+.2f}%"
            ))
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Positions ─────────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  POSITIONS  (intraday / F&O)")
print(DIVIDER)
try:
    result = smart_api.position()
    if result and result.get("status"):
        positions = result.get("data", []) or []
        # Filter to positions with non-zero net quantity
        net_positions = [p for p in positions if int(p.get("netqty", 0) or 0) != 0
                         or int(p.get("buyqty", 0) or 0) != 0]
        if not net_positions:
            print("  No open positions.")
        else:
            fmt = "  {:<20} {:<6} {:>6} {:>12} {:>12}"
            print(fmt.format("Symbol", "Exch", "Net Qty", "Avg Price", "LTP"))
            print("  " + "-" * 60)
            for p in net_positions:
                sym    = p.get("tradingsymbol", "?")
                exch   = p.get("exchange", "?")
                netqty = p.get("netqty", p.get("buyqty", 0))
                avg    = float(p.get("avgnetprice", p.get("buyavgprice", 0)) or 0)
                ltp    = float(p.get("ltp", 0) or 0)
                print(fmt.format(sym[:20], exch[:6], netqty, f"{avg:,.2f}", f"{ltp:,.2f}"))
    else:
        msg = result.get("message", "No data") if result else "No response"
        if "No data" in msg or "errorcode" in str(result):
            print("  No open positions.")
        else:
            print(f"  [WARN] {msg}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Open Orders ───────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  OPEN ORDERS (today)")
print(DIVIDER)
try:
    result = smart_api.orderBook()
    if result and result.get("status"):
        orders = result.get("data", []) or []
        open_orders = [o for o in orders
                       if o.get("status", "").upper() in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")]
        if not open_orders:
            print("  No open orders.")
        else:
            fmt = "  {:<16} {:<20} {:<5} {:>6} {:>12} {:<12}"
            print(fmt.format("Order ID", "Symbol", "Side", "Qty", "Price", "Status"))
            print("  " + "-" * 74)
            for o in open_orders:
                print(fmt.format(
                    str(o.get("orderid", "?"))[:16],
                    o.get("tradingsymbol", "?")[:20],
                    o.get("transactiontype", "?")[:5],
                    o.get("quantity", 0),
                    f"{float(o.get('price', 0) or 0):,.2f}",
                    o.get("status", "?")[:12]
                ))
    else:
        msg = result.get("message", "No data") if result else "No response"
        print(f"  No open orders." if "No data" in msg else f"  [WARN] {msg}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Today's Trades ────────────────────────────────────────────────────────────
print("\n" + DIVIDER)
print("  TODAY'S TRADES (executed)")
print(DIVIDER)
try:
    result = smart_api.tradeBook()
    if result and result.get("status"):
        trades = result.get("data", []) or []
        if not trades:
            print("  No trades today.")
        else:
            fmt = "  {:<16} {:<20} {:<5} {:>6} {:>12} {:<22}"
            print(fmt.format("Trade ID", "Symbol", "Side", "Qty", "Price", "Fill Time"))
            print("  " + "-" * 84)
            for t in trades:
                print(fmt.format(
                    str(t.get("tradeid", "?"))[:16],
                    t.get("tradingsymbol", "?")[:20],
                    t.get("transactiontype", "?")[:5],
                    t.get("fillsize", t.get("quantity", 0)),
                    f"{float(t.get('fillprice', t.get('price', 0)) or 0):,.2f}",
                    str(t.get("filltime", t.get("exchtime", "?")))[:22]
                ))
    else:
        msg = result.get("message", "No data") if result else "No response"
        print(f"  No trades today." if "No data" in msg else f"  [WARN] {msg}")
except Exception as e:
    print(f"  [ERROR] {e}")

# ── Logout ────────────────────────────────────────────────────────────────────
try:
    smart_api.terminateSession(CLIENT_CODE)
    print(f"\n[OK] Session terminated (logout).")
except Exception:
    pass  # Non-critical: session expires at midnight anyway

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print("  Done. All Angel One SmartAPI data pulled successfully.")
print("=" * 65)
