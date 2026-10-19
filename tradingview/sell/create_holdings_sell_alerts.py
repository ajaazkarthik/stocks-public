"""
create_holdings_sell_alerts.py

Creates/manages individual per-symbol SELL alerts for current holdings.
Uses TV technical alert quota (400 slots), NOT the 2-slot watchlist quota.

Architecture:
  - Each holding gets its own SELL alert using the Pine SELL strategy script
  - condition.type="strategy", strategy_mode="alerts" → uses alertcondition()
  - Quota: technical alerts (400), vs watchlist alerts (2) used by WIN alerts
  - Fires mobile push notification to user on bearish breakout

Usage:
  python create_holdings_sell_alerts.py --sync      # sync alerts to match positions.json
  python create_holdings_sell_alerts.py --list      # list current SELL alerts
  python create_holdings_sell_alerts.py --delete-all  # remove all SELL alerts (cleanup)
"""

import json, re, sys, argparse
from pathlib import Path
import requests

ROOT = Path(__file__).parent.parent
BASE = Path(__file__).parent
POSITIONS_FILE = ROOT / 'positions.json'
COOKIE_FILE = ROOT / 'cookie'

# Pine script IDs
SELL_PINE_ID = 'USER;34152e1cd8524db59808220de501a9aa'
WIN_PINE_ID  = 'USER;91cbee5a4fdd4f2f9b1f2dd32c51ce11'

UID = 57923804  # ajaazkarthik1

# SELL alert resolution: 60 = 1H
SELL_RESOLUTION = '60'

# ── Standard SELL script Pine inputs (from existing alerts_raw.json) ──────────
SELL_INPUTS = {
    "__log_level": 0, "__profile": False,
    "in_0": "Chart", "in_1": 15, "in_2": 2, "in_3": "rgba(8,153,129,0.47)",
    "in_4": "rgba(223,243,239,0)", "in_5": "rgba(242,54,69,0.47)",
    "in_6": "rgba(241,206,208,0)", "in_7": True, "in_8": True, "in_9": 1.3,
    "in_10": "rgba(41,98,255,0.27)", "in_11": "rgba(255,152,0,0.27)",
    "in_12": True,
    "in_13": "rgba(8,153,129,0.67)", "in_14": "rgba(242,54,69,0.67)",
    "in_15": "Tiny", "in_16": "rgba(41,98,255,0.67)",
    "in_17": "rgba(224,64,251,0.67)", "in_18": "Tiny",
    "in_19": "rgba(8,153,129,0.67)", "in_20": "rgba(242,54,69,0.67)",
    "in_21": "Tiny", "in_22": "rgba(8,153,129,0.67)", "in_23": "rgba(242,54,69,0.67)",
    "in_24": "Tiny", "in_25": "None", "in_26": "rgba(242,54,69,0.67)",
    "in_27": "rgba(8,153,129,0.67)",
    "in_28": 20, "in_29": 2, "in_30": 20, "in_31": 1.5, "in_32": True,
    "in_33": 14, "in_34": "close", "in_35": 20, "in_36": "Wicks",
    "in_37": 1.1, "in_38": "Close", "in_39": False, "in_40": "Dashed",
    "in_41": "rgba(2,133,69,1)", "in_42": "rgba(246,7,7,1)",
    "in_43": True, "in_44": "D", "in_45": 20, "in_46": 7, "in_47": 1,
    "in_48": False, "in_49": True, "in_50": 2,
    "in_51": False, "in_52": False, "in_53": 50, "in_54": 7, "in_55": 1,
    "in_56": True, "in_57": False, "in_58": False, "in_59": False,
    "in_60": "percent_of_equity", "in_61": 100, "in_62": "percent",
    "in_63": 0.1, "in_64": 3, "in_65": 1,
    "in_66": False, "in_67": False, "in_68": 0, "in_69": 1000000,
    "in_70": "NONE", "in_71": False, "in_72": "FIFO",
    "in_73": 0, "in_74": 0, "in_75": "BACKTEST", "in_76": "",
    "in_77": "order_fills", "in_78": "", "in_79": 2,
    "in_80": False, "in_81": False, "in_82": False,
    "pineFeatures": '{"strategy":1,"plot":1,"str":1,"array":1,"ta":1,"math":1,"alert":1,"alertcondition":1,"line":1,"box":1,"label":1,"table":1,"request.security":1,"type":1,"builtin_methods":1,"log":1}'
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def build_session() -> tuple[requests.Session, str, str]:
    """Return (requests.Session, jwt_token, uid_str).
    Raises RuntimeError if cookie is expired or JWT not found."""
    cookie_raw = COOKIE_FILE.read_text(encoding='utf-8').strip()

    s = requests.Session()
    for field in cookie_raw.split(';'):
        k, _, v = field.strip().partition('=')
        k = k.strip()
        if k in ('sessionid', 'sessionid_sign'):
            s.cookies.set(k, v.strip(), domain='.tradingview.com')

    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    })

    # extract JWT from chart page
    resp = s.get('https://www.tradingview.com/chart/', timeout=20)
    if 'is-not-authenticated' in resp.text:
        raise RuntimeError(
            'TV session expired. Refresh cookies:\n'
            '  1. Log into tradingview.com in browser\n'
            '  2. F12 → Network → click chart/ request → Request Headers → copy Cookie\n'
            '  3. Paste into tradingview/cookie file'
        )

    m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
    if not m:
        raise RuntimeError('auth_token JWT not found in page. Session may be invalid.')
    jwt = m.group(1)

    # Configure PA session
    pa = requests.Session()
    pa.cookies.set('sessionid', s.cookies.get('sessionid'), domain='.tradingview.com')
    pa.cookies.set('sessionid_sign', s.cookies.get('sessionid_sign'), domain='.tradingview.com')
    pa.headers.update({
        'User-Agent': s.headers['User-Agent'],
        'Authorization': f'Bearer {jwt}',
        'Content-Type': 'application/json',
        'Origin': 'https://www.tradingview.com',
        'Referer': 'https://www.tradingview.com/chart/',
    })
    return pa, jwt

def pa_post(pa: requests.Session, endpoint: str, payload: dict) -> dict:
    r = pa.post(f'https://pricealerts.tradingview.com/{endpoint}', json=payload, timeout=20)
    try:
        return r.json()
    except Exception:
        return {'s': 'parse_error', 'raw': r.text[:200], 'code': r.status_code}

# ── Symbol encoding ────────────────────────────────────────────────────────────

def encode_symbol(sym: str) -> str:
    """Encode a TV symbol (e.g. 'NYSE:XLV') to the pro_symbol JSON string."""
    exchange = sym.split(':')[0].upper()
    base = {"symbol": sym, "adjustment": "splits"}
    # Indian exchanges need session + currency
    if exchange in ('NSE', 'BSE', 'BSE_DLY', 'NSE_DLY'):
        base['session'] = 'regular'
        base['currency-id'] = 'INR'
    # ASX uses AUD implicitly — TV handles it without extra fields
    return '=' + json.dumps(base, separators=(',', ':'))

# ── Alert payload builder ──────────────────────────────────────────────────────

def build_sell_condition(resolution: str = SELL_RESOLUTION) -> dict:
    return {
        "type": "strategy",
        "frequency": "60",   # "60" = per-bar, not once
        "series": [{
            "type": "study",
            "study": "StrategyScript@tv-scripting-101",
            "pine_id": SELL_PINE_ID,
            "pine_version": "3.0",
            "inputs": SELL_INPUTS,
        }],
        "strategy_mode": "alerts",
        "cross_interval": False,
    }

def build_sell_alert_payload(sym: str, resolution: str = SELL_RESOLUTION) -> dict:
    cond = build_sell_condition(resolution)
    encoded = encode_symbol(sym)
    return {
        "uid": UID,
        "symbol": encoded,
        "resolution": resolution,
        "condition": cond,
        "conditions": [cond],
        "expiration": None,
        "auto_deactivate": False,
        "email": False,
        "sms_over_email": False,
        "mobile_push": True,
        "message": f"SELL signal: {sym} | {{{{interval}}}} | Price: {{{{close}}}}",
        "sound_file": None,
        "sound_duration": 0,
        "popup": False,
        "web_hook": None,
        "name": f"SELL {sym}",
        "kinds": ["regular"],
    }

# ── Core operations ────────────────────────────────────────────────────────────

def list_current_sell_alerts(pa: requests.Session) -> list[dict]:
    """Return all existing strategy alerts using the SELL pine_id."""
    data = pa_post(pa, 'list_alerts', {'uid': UID})
    if data.get('s') != 'ok':
        raise RuntimeError(f'list_alerts failed: {data}')
    alerts = data.get('r') or []
    sell_alerts = []
    for a in alerts:
        cond = a.get('condition') or {}
        series = cond.get('series') or []
        if series and series[0].get('pine_id') == SELL_PINE_ID:
            # Decode symbol
            sym_raw = a.get('pro_symbol') or a.get('symbol') or ''
            try:
                sym = json.loads(sym_raw[1:]).get('symbol', sym_raw) if sym_raw.startswith('=') else sym_raw
            except Exception:
                sym = sym_raw[:40]
            sell_alerts.append({
                'alert_id': a.get('alert_id'),
                'symbol': sym,
                'name': a.get('name'),
                'resolution': a.get('resolution'),
                'active': a.get('active'),
            })
    return sell_alerts

def create_sell_alert(pa: requests.Session, sym: str) -> dict:
    payload = build_sell_alert_payload(sym)
    result = pa_post(pa, 'create_alert', payload)
    if result.get('s') == 'ok':
        new_id = (result.get('r') or {}).get('alert_id', '?')
        print(f'  ✓ Created SELL alert for {sym} — id={new_id}')
    else:
        print(f'  ✗ Failed to create SELL alert for {sym}: {result}')
    return result

def delete_sell_alerts(pa: requests.Session, alert_ids: list[int]) -> None:
    if not alert_ids:
        return
    result = pa_post(pa, 'delete_alerts', {'uid': UID, 'alert_ids': alert_ids})
    if result.get('s') == 'ok':
        print(f'  ✓ Deleted alert ids: {alert_ids}')
    else:
        print(f'  ✗ Delete failed: {result}')

# ── Main commands ──────────────────────────────────────────────────────────────

def cmd_list(pa: requests.Session):
    alerts = list_current_sell_alerts(pa)
    if not alerts:
        print('No SELL alerts found.')
        return
    print(f'Current SELL alerts ({len(alerts)}):')
    for a in alerts:
        status = 'ACTIVE' if a['active'] else 'stopped'
        print(f'  [{a["alert_id"]}] {a["symbol"]:<30} res={a["resolution"]:<5} {status}')

def cmd_sync(pa: requests.Session):
    """Create SELL alerts for holdings missing one. Delete stale alerts."""
    positions = json.loads(POSITIONS_FILE.read_text())
    held_syms = {p['symbol'] for p in positions}
    print(f'Holdings ({len(held_syms)}): {sorted(held_syms)}')

    existing = list_current_sell_alerts(pa)
    existing_syms = {a['symbol'] for a in existing}
    print(f'Existing SELL alerts ({len(existing)}): {sorted(existing_syms)}')

    # Create missing
    missing = held_syms - existing_syms
    if missing:
        print(f'\nCreating {len(missing)} new SELL alerts...')
        for sym in sorted(missing):
            create_sell_alert(pa, sym)
    else:
        print('\nAll holdings already have SELL alerts.')

    # Delete stale (no longer held)
    stale = [a for a in existing if a['symbol'] not in held_syms]
    if stale:
        stale_ids = [a['alert_id'] for a in stale]
        print(f'\nDeleting {len(stale)} stale alerts: {[a["symbol"] for a in stale]}')
        delete_sell_alerts(pa, stale_ids)
    else:
        print('No stale SELL alerts to remove.')

    print('\nSync complete.')

def cmd_delete_all(pa: requests.Session):
    existing = list_current_sell_alerts(pa)
    if not existing:
        print('No SELL alerts to delete.')
        return
    ids = [a['alert_id'] for a in existing]
    syms = [a['symbol'] for a in existing]
    confirm = input(f'Delete ALL {len(existing)} SELL alerts? {syms}\nType "yes" to confirm: ')
    if confirm.strip().lower() == 'yes':
        delete_sell_alerts(pa, ids)
    else:
        print('Cancelled.')

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Manage per-holding SELL alerts')
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument('--list', action='store_true', help='List current SELL alerts')
    grp.add_argument('--sync', action='store_true', help='Sync alerts to positions.json')
    grp.add_argument('--delete-all', action='store_true', help='Delete all SELL alerts')
    args = parser.parse_args()

    try:
        print('Authenticating...')
        pa, jwt = build_session()
        print(f'Auth OK (JWT: {jwt[:20]}...)\n')
    except RuntimeError as e:
        print(f'AUTH ERROR: {e}')
        sys.exit(1)

    if args.list:
        cmd_list(pa)
    elif args.sync:
        cmd_sync(pa)
    elif args.delete_all:
        cmd_delete_all(pa)

if __name__ == '__main__':
    main()
