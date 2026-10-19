"""
sync_holdings_watchlist.py — Auto-sync HOLDINGS sell alert with actual positions

ARCHITECTURE:
  ALL_NEW (1000 symbols) ─── WIN alert 1H ──► BUY signals (discovery)
  HOLDINGS (~10-30 syms) ─── WIN/SELL 1H ──► SELL signals (exit timing)

WHY SEPARATE:
  - Enabling SELL on ALL_NEW → 1000 stocks firing sell = ~500 daily noise alerts
  - HOLDINGS only fires sell for stocks you actually own (10-30 signals max)
  - No more "sell alert on a stock you don't hold"

AUTO-SYNC FLOW:
  positions.json (TV-formatted) 
      ↓
  This script updates the SELL alert's symbol list via TradingView API
      ↓
  TV SELL alert automatically monitors only current holdings

TRIGGER POINTS (all call this script):
  1. After Moomoo BUY executes → webhook_server.py calls add_to_holdings(symbol)
  2. After Moomoo SELL executes → webhook_server.py calls remove_from_holdings(symbol)
  3. morning_screener.py startup → full_sync()
  4. CLI: python sync_holdings_watchlist.py

USAGE:
  python sync_holdings_watchlist.py                  # full sync from positions.json
  python sync_holdings_watchlist.py --add NYSE:AAPL  # add one symbol
  python sync_holdings_watchlist.py --remove NYSE:AAPL  # remove one
  python sync_holdings_watchlist.py --list           # show current holdings in alert
"""

import json
import re
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
BASE = Path(__file__).parent
POSITIONS_FILE = ROOT / "positions.json"
HOLDINGS_FILE  = BASE / "holdings.json"     # local cache of current HOLDINGS alert symbols
COOKIE_FILE    = ROOT / "cookie"

# ─── TradingView SELL Alert ID (from alerts_raw.json) ────────────────────────
# Looking at existing alerts: 2325083017 is "SELL" at 2H, 2325001918 is "Sell" at 1H
# For sell timing we want 1H (faster responds to sell signals)
# CHANGE THIS if you create a new dedicated HOLDINGS alert:
SELL_ALERT_ID = 2325001918   # "Sell" res=60 (1H) — currently on NSE:WAAREEENER
# Alternative:  2325083017   # "SELL" res=120 (2H)

# ─── TradingView API session ──────────────────────────────────────────────────
def _make_session():
    """Build a requests-like thin wrapper using urllib (no external deps)."""
    import urllib.request
    import urllib.parse
    
    cookie_raw = COOKIE_FILE.read_text(encoding="utf-8").strip() if COOKIE_FILE.exists() else ""
    # Parse sessionid and sessionid_sign from cookie string
    sessionid = ""
    sessionid_sign = ""
    for part in cookie_raw.split(";"):
        p = part.strip()
        if p.startswith("sessionid="):
            sessionid = p[len("sessionid="):]
        elif p.startswith("sessionid_sign="):
            sessionid_sign = p[len("sessionid_sign="):]
    return sessionid, sessionid_sign


def _get_jwt(sessionid: str, sessionid_sign: str) -> str | None:
    """Fetch JWT auth_token from TradingView chart page."""
    import urllib.request
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Cookie": f"sessionid={sessionid}; sessionid_sign={sessionid_sign}",
    }
    try:
        req = urllib.request.Request("https://www.tradingview.com/chart/", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', html)
        return m.group(1) if m else None
    except Exception as e:
        print(f"⚠️  Could not fetch JWT: {e}")
        return None


def _tv_post(endpoint: str, payload: dict, sessionid: str, sessionid_sign: str, jwt: str) -> dict:
    """POST to pricealerts.tradingview.com endpoint."""
    import urllib.request
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Authorization": f"Bearer {jwt}",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/chart/",
        "Cookie": f"sessionid={sessionid}; sessionid_sign={sessionid_sign}",
    }
    url = f"https://pricealerts.tradingview.com/{endpoint}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"s": "error", "err": str(e)}


# ─── positions.json helpers ───────────────────────────────────────────────────
def load_positions() -> list[str]:
    """Read positions.json → return list of TV-format symbols e.g. ['NYSE:AAPL', ...]."""
    if not POSITIONS_FILE.exists():
        print(f"⚠️  {POSITIONS_FILE} not found — returning empty holdings")
        return []
    positions = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    symbols = []
    for p in positions:
        sym = p.get("symbol", "")
        if sym:
            symbols.append(sym)
    return symbols


def save_positions(positions: list[dict]) -> None:
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2), encoding="utf-8")


def add_position(tv_symbol: str, entry_price: float = 0.0, shares: int = 1) -> list[str]:
    """Add a symbol to positions.json (called after BUY executes)."""
    positions = json.loads(POSITIONS_FILE.read_text(encoding="utf-8")) if POSITIONS_FILE.exists() else []
    existing = [p["symbol"] for p in positions]
    if tv_symbol not in existing:
        positions.append({
            "symbol": tv_symbol,
            "entry_price": entry_price,
            "entry_date": datetime.now().strftime("%Y-%m-%d"),
            "shares": shares,
            "atr_at_entry": 0.0,
            "high_water_mark": entry_price,
        })
        save_positions(positions)
        print(f"  📋 Added {tv_symbol} to positions.json")
    return [p["symbol"] for p in positions]


def remove_position(tv_symbol: str) -> list[str]:
    """Remove a symbol from positions.json (called after SELL executes)."""
    if not POSITIONS_FILE.exists():
        return []
    positions = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    before = len(positions)
    positions = [p for p in positions if p.get("symbol") != tv_symbol]
    if len(positions) < before:
        save_positions(positions)
        print(f"  📋 Removed {tv_symbol} from positions.json")
    return [p["symbol"] for p in positions]


# ─── Local holdings cache ─────────────────────────────────────────────────────
def load_holdings_cache() -> list[str]:
    if HOLDINGS_FILE.exists():
        return json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
    return []


def save_holdings_cache(symbols: list[str]) -> None:
    HOLDINGS_FILE.write_text(json.dumps(sorted(set(symbols)), indent=2), encoding="utf-8")


# ─── TV Alert sync ────────────────────────────────────────────────────────────
def get_current_alert_symbols(alert_id: int, sessionid: str, sessionid_sign: str, jwt: str) -> list[str] | None:
    """Fetch the current symbol list from the SELL alert."""
    result = _tv_post("list_alerts", {"uid": _get_uid(sessionid, sessionid_sign, jwt)}, sessionid, sessionid_sign, jwt)
    if result.get("s") != "ok":
        return None
    for alert in result.get("r", []):
        if alert.get("alert_id") == alert_id:
            sd = alert.get("symbolset_data", {})
            if sd:
                return sd.get("symbols", [])
            # Single symbol alert
            sym = alert.get("pro_symbol", alert.get("symbol", ""))
            if sym and not sym.startswith("WATCHLIST"):
                return [sym]
    return None


def _get_uid(sessionid: str, sessionid_sign: str, jwt: str) -> int:
    """Extract user ID from JWT."""
    try:
        import base64
        payload_b64 = jwt.split(".")[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        return payload.get("uid", 57923804)
    except Exception:
        return 57923804  # fallback


def modify_alert_symbols(alert_id: int, new_symbols: list[str], sessionid: str, sessionid_sign: str, jwt: str) -> bool:
    """
    Update the SELL alert to watch only new_symbols.
    This modifies the symbolset_data (watchlist) of the alert.
    
    TradingView's modify_alert endpoint accepts the same payload as the alert object.
    """
    uid = _get_uid(sessionid, sessionid_sign, jwt)
    
    # First fetch current alert to preserve all other fields
    result = _tv_post("list_alerts", {"uid": uid}, sessionid, sessionid_sign, jwt)
    if result.get("s") != "ok":
        print(f"  ⚠️  Could not fetch alerts: {result}")
        return False
    
    target_alert = None
    for alert in result.get("r", []):
        if alert.get("alert_id") == alert_id:
            target_alert = alert
            break
    
    if not target_alert:
        print(f"  ⚠️  Alert ID {alert_id} not found in TV account")
        return False
    
    # Build the modified alert payload
    # We keep all other fields (condition, resolution, etc.) and only update symbols
    payload_alert = {k: v for k, v in target_alert.items()}
    
    # If alert currently watches a specific symbol → convert to watchlist format
    # OR if it already has symbolset_data → update the symbols
    if len(new_symbols) == 1:
        # Single holding: use direct symbol format
        payload_alert["pro_symbol"] = new_symbols[0]
        payload_alert["symbol"] = new_symbols[0]
        if "symbolset_data" in payload_alert:
            del payload_alert["symbolset_data"]
    else:
        # Multiple holdings: use symbolset_data (watchlist-style)
        payload_alert["pro_symbol"] = None
        payload_alert["symbol"] = None
        payload_alert["symbolset_data"] = {
            "symbols": new_symbols
        }
    
    # POST to modify_alert
    modify_result = _tv_post("modify_alert", {"uid": uid, "alert": payload_alert}, sessionid, sessionid_sign, jwt)
    
    if modify_result.get("s") == "ok":
        return True
    
    # Try alternative endpoint: edit_alert
    modify_result2 = _tv_post("edit_alert", {"uid": uid, "alert": payload_alert}, sessionid, sessionid_sign, jwt)
    if modify_result2.get("s") == "ok":
        return True
    
    # Try put_alert (another common TV endpoint name)
    modify_result3 = _tv_post("put_alert", {"uid": uid, "alert": payload_alert}, sessionid, sessionid_sign, jwt)
    if modify_result3.get("s") == "ok":
        return True
    
    print(f"  ⚠️  All modify endpoints failed:")
    print(f"      modify_alert: {modify_result}")
    print(f"      edit_alert:   {modify_result2}")
    print(f"      put_alert:    {modify_result3}")
    return False


# ─── export file (fallback when API fails) ────────────────────────────────────
def write_holdings_import(symbols: list[str]) -> Path:
    """Write holdings_import.txt for manual TV import."""
    out = BASE / "holdings_import.txt"
    out.write_text("\n".join(symbols), encoding="utf-8")
    return out


# ─── Main sync ────────────────────────────────────────────────────────────────
def full_sync(verbose: bool = True) -> bool:
    """
    Full sync: read positions.json → update TV SELL alert.
    Returns True on API success, False if manual import needed.
    """
    symbols = load_positions()
    if not symbols:
        if verbose:
            print("⚠️  No positions found in positions.json — SELL alert will be empty")
        symbols = []

    cache = load_holdings_cache()
    if sorted(symbols) == sorted(cache) and cache:
        if verbose:
            print(f"✅ HOLDINGS already in sync ({len(symbols)} symbols) — no update needed")
        return True

    if verbose:
        print(f"🔄 Syncing HOLDINGS sell alert with {len(symbols)} positions:")
        for s in sorted(symbols):
            marker = "  +" if s not in cache else "   "
            print(f"  {marker} {s}")
        removed = [s for s in cache if s not in symbols]
        for s in sorted(removed):
            print(f"  - {s}  (sold)")

    # Try TV API
    sessionid, sessionid_sign = _make_session()
    if not sessionid:
        if verbose:
            print("⚠️  No TV session cookie found — cannot update via API")
        out = write_holdings_import(symbols)
        if verbose:
            _print_manual_instructions(out, symbols)
        return False

    jwt = _get_jwt(sessionid, sessionid_sign)
    if not jwt:
        if verbose:
            print("⚠️  Could not get JWT token — offline mode")
        out = write_holdings_import(symbols)
        if verbose:
            _print_manual_instructions(out, symbols)
        return False

    success = modify_alert_symbols(SELL_ALERT_ID, symbols, sessionid, sessionid_sign, jwt)

    if success:
        save_holdings_cache(symbols)
        if verbose:
            print(f"✅ TV SELL alert ({SELL_ALERT_ID}) updated: {len(symbols)} holdings")
        return True
    else:
        out = write_holdings_import(symbols)
        if verbose:
            print(f"\n📄 Fallback: {out.name} written — see manual instructions below")
            _print_manual_instructions(out, symbols)
        # Still save cache so we track desired state
        save_holdings_cache(symbols)
        return False


def _print_manual_instructions(import_file: Path, symbols: list[str]) -> None:
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  MANUAL UPDATE REQUIRED (TV API endpoint not confirmed yet)         ║
╠══════════════════════════════════════════════════════════════════════╣
║  OPTION 1 — Create HOLDINGS watchlist in TradingView UI:            ║
║    1. Open TradingView → Watchlists → New list → "HOLDINGS"         ║
║    2. Import {import_file.name:<50}  ║
║       (contains {len(symbols):<3} symbols in TV format)                         ║
║    3. Create a new SELL alert → choose "HOLDINGS" watchlist         ║
║    4. Note the new alert ID and set SELL_ALERT_ID in this script     ║
║                                                                      ║
║  OPTION 2 — Update existing SELL alert manually:                    ║
║    1. Open existing SELL alert → change symbol to each holding       ║
║    ⚠️  TV only supports 1 symbol per non-watchlist alert              ║
║                                                                      ║
║  BEST PRACTICE: Use the HOLDINGS watchlist (Option 1)               ║
║  After creating, set SELL_ALERT_ID = the new alert ID               ║
╚══════════════════════════════════════════════════════════════════════╝

Current holdings ({len(symbols)} symbols):
""" + "\n".join(f"  {s}" for s in sorted(symbols)))


# ─── Convenience functions (called from webhook_server.py) ───────────────────
def on_buy_executed(tv_symbol: str, entry_price: float = 0.0, shares: int = 1) -> None:
    """Call this after a successful Moomoo BUY order. Adds to positions + syncs."""
    add_position(tv_symbol, entry_price, shares)
    full_sync(verbose=False)
    print(f"  📡 Holdings sync: +{tv_symbol} ({len(load_positions())} total)", flush=True)


def on_sell_executed(tv_symbol: str) -> None:
    """Call this after a successful Moomoo SELL order. Removes from positions + syncs."""
    remove_position(tv_symbol)
    full_sync(verbose=False)
    print(f"  📡 Holdings sync: -{tv_symbol} ({len(load_positions())} total)", flush=True)


# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync HOLDINGS sell alert with positions.json")
    parser.add_argument("--add",    metavar="SYM", help="Add symbol (e.g. NYSE:AAPL)")
    parser.add_argument("--remove", metavar="SYM", help="Remove symbol (e.g. NYSE:AAPL)")
    parser.add_argument("--list",   action="store_true", help="Show current holdings")
    parser.add_argument("--alert-id", type=int, default=SELL_ALERT_ID,
                        help=f"TV alert ID to update (default: {SELL_ALERT_ID})")
    args = parser.parse_args()
    
    SELL_ALERT_ID = args.alert_id  # allow override

    if args.list:
        symbols = load_positions()
        print(f"Current holdings in positions.json ({len(symbols)}):")
        for s in sorted(symbols): print(f"  {s}")
        cached = load_holdings_cache()
        if sorted(symbols) != sorted(cached):
            print(f"\n⚠️  TV SELL alert cache differs ({len(cached)} symbols) — run sync")
    elif args.add:
        on_buy_executed(args.add)
    elif args.remove:
        on_sell_executed(args.remove)
    else:
        # Full sync
        print(f"🏦 Holdings Sync — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"   SELL alert ID: {SELL_ALERT_ID}")
        print()
        success = full_sync(verbose=True)
        sys.exit(0 if success else 1)
