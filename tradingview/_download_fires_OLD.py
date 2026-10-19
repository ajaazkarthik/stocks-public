"""
Download all fires from WIN alerts and analyse which symbols triggered.
Now that we know the correct payload format: {"payload": {...}}
"""
import requests
import re
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

session = 'tgtpkiksyndf44la35iy6ga2wcviq23n'
session_sign = 'v3:tXGZpkq4cLGB9QZ6aAXA/tgs1tVfIYe1p/vvlXyCYu4='

# --- Auth ---
s = requests.Session()
s.cookies.set('sessionid', session, domain='.tradingview.com')
s.cookies.set('sessionid_sign', session_sign, domain='.tradingview.com')
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
resp = s.get('https://www.tradingview.com/chart/', timeout=30)
m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
jwt_token = m.group(1) if m else None
print(f"JWT obtained: {bool(jwt_token)}")

pa_base = 'https://pricealerts.tradingview.com'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {jwt_token}',
    'Origin': 'https://www.tradingview.com',
    'Referer': 'https://www.tradingview.com/chart/',
}
cookies = {'sessionid': session, 'sessionid_sign': session_sign}

def list_fires(payload_params=None):
    """Call /list_fires with the correct {"payload": ...} wrapper"""
    body = {"payload": payload_params or {}}
    r = requests.post(f'{pa_base}/list_fires', 
                      headers=headers, cookies=cookies,
                      json=body, timeout=15)
    d = r.json()
    if d.get('s') == 'ok':
        return d.get('r', [])
    else:
        print(f"  ERROR: {d}")
        return []

# --- Explore pagination ---
print("\n=== Exploring pagination for /list_fires ===")

# First call - get initial batch
fires = list_fires({})
print(f"Default call returns: {len(fires)} fires")
if fires:
    print(f"  First fire: fire_id={fires[0]['fire_id']}, alert_id={fires[0]['alert_id']}, symbol={fires[0]['symbol']}")
    print(f"  Last fire:  fire_id={fires[-1]['fire_id']}, alert_id={fires[-1]['alert_id']}, symbol={fires[-1]['symbol']}")

# Try with limit
fires100 = list_fires({"limit": 100})
print(f"\nWith limit=100: {len(fires100)} fires")

fires200 = list_fires({"limit": 200})
print(f"With limit=200: {len(fires200)} fires")

fires500 = list_fires({"limit": 500})
print(f"With limit=500: {len(fires500)} fires")

# Try with offset
fires_p2 = list_fires({"limit": 50, "offset": 50})
print(f"\nWith limit=50, offset=50: {len(fires_p2)} fires")
if fires_p2:
    print(f"  First: fire_id={fires_p2[0]['fire_id']}, symbol={fires_p2[0]['symbol']}")

# Try cursor-based with from_id / from_fire_id
if fires:
    last_id = fires[-1]['fire_id']
    fires_next = list_fires({"from_fire_id": last_id})
    print(f"\nWith from_fire_id={last_id}: {len(fires_next)} fires")
    
    fires_next2 = list_fires({"before_fire_id": last_id})
    print(f"With before_fire_id={last_id}: {len(fires_next2)} fires")
    
    fires_next3 = list_fires({"next_fire_id": last_id})
    print(f"With next_fire_id={last_id}: {len(fires_next3)} fires")

    fires_next4 = list_fires({"from_id": last_id})
    print(f"With from_id={last_id}: {len(fires_next4)} fires")

print("\n\n=== Downloading all fires for WIN alerts ===")
WIN_2H_ID = 2381963449
WIN_1H_ID = 2381961922

# Try getting fires for specific alert IDs  
fires_win2h = list_fires({"alert_id": WIN_2H_ID, "limit": 1000})
print(f"\nWIN 2H (alert_id={WIN_2H_ID}): {len(fires_win2h)} fires")
fires_win1h = list_fires({"alert_id": WIN_1H_ID, "limit": 1000})
print(f"WIN 1H (alert_id={WIN_1H_ID}): {len(fires_win1h)} fires")

# Get all fires regardless of alert
all_fires = list_fires({"limit": 1000})
print(f"\nAll fires (limit=1000): {len(all_fires)} fires")

# Use the best result
fires_to_analyze = all_fires if len(all_fires) > max(len(fires_win2h), len(fires_win1h)) else fires_win2h + fires_win1h
print(f"\nAnalyzing {len(fires_to_analyze)} fires total")

# Save raw
with open('fires_raw.json', 'w') as f:
    json.dump({'all': all_fires, 'win2h': fires_win2h, 'win1h': fires_win1h}, f, indent=2)
print("Saved fires_raw.json")

# --- Analyse ---
print("\n" + "="*80)
print("FIRE ANALYSIS")
print("="*80)

win_fires = [f for f in all_fires if f['alert_id'] in (WIN_2H_ID, WIN_1H_ID)]
print(f"\nWIN alert fires: {len(win_fires)}")

# Group by symbol
by_symbol = defaultdict(list)
for fire in win_fires:
    sym = fire.get('pro_symbol') or fire.get('symbol', 'UNKNOWN')
    by_symbol[sym].append(fire)

print(f"\nDistinct symbols that fired: {len(by_symbol)}")
print("\nTop symbols fired:")
for sym, fires_list in sorted(by_symbol.items(), key=lambda x: -len(x)):
    # Parse direction from message
    directions = []
    for f in fires_list:
        msg = f.get('message', '')
        if msg.startswith('BUY') or 'BUY: true' in msg:
            directions.append('BUY')
        elif msg.startswith('SELL') or 'SELL: true' in msg:
            directions.append('SELL')
        else:
            directions.append('?')
    dir_counts = Counter(directions)
    print(f"  {sym}: {len(fires_list)} fires | {dict(dir_counts)}")

# Show detailed for last 20 fires
print("\n=== Last 20 Fire Events ===")
recent = sorted(all_fires, key=lambda x: x.get('fire_id', 0), reverse=True)[:20]
for fire in recent:
    sym = fire.get('pro_symbol') or fire.get('symbol', '?')
    aid = fire.get('alert_id')
    tf = fire.get('resolution', '?')
    msg = fire.get('message', '')[:120]
    fid = fire.get('fire_id', 0)
    # Try to determine direction
    direction = 'BUY' if msg.startswith('BUY') else ('SELL' if msg.startswith('SELL') else '?')
    print(f"  [{fid}] alert={aid} tf={tf}m {direction} {sym}")
    print(f"    msg: {msg[:100]}")

print("\n\n=== BUY Signal Summary ===")
buy_fires = [f for f in win_fires if f.get('message', '').startswith('BUY')]
sell_fires = [f for f in win_fires if f.get('message', '').startswith('SELL')]
print(f"BUY signals: {len(buy_fires)}")
print(f"SELL signals: {len(sell_fires)}")

print("\nBUY signals by symbol:")
buy_syms = Counter(f.get('pro_symbol') or f.get('symbol') for f in buy_fires)
for sym, count in buy_syms.most_common(20):
    print(f"  {sym}: {count}")

print("\nSELL signals by symbol:")
sell_syms = Counter(f.get('pro_symbol') or f.get('symbol') for f in sell_fires)
for sym, count in sell_syms.most_common(20):
    print(f"  {sym}: {count}")

# Parse first BUY fire message for field names
if buy_fires:
    print("\n\n=== Sample BUY Fire Message ===")
    sample = buy_fires[0]
    print(f"Symbol: {sample.get('pro_symbol')}")
    print(f"Alert: {sample.get('alert_id')}")
    print(f"TF: {sample.get('resolution')}m")
    print(f"Message:\n{sample.get('message', '')}")
