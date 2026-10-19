import requests
import re
import json
from pathlib import Path
ROOT = Path(__file__).parent.parent

session = 'tqiinu7hetfknzfo7fsp6rbr4fncdm2w'
session_sign = 'v3:hqixHMngHBdKAWBNlaF2p8pGe3HGOxBqj/sqzx4zYaE='

s = requests.Session()
s.cookies.set('sessionid', session, domain='.tradingview.com')
s.cookies.set('sessionid_sign', session_sign, domain='.tradingview.com')
s.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36'})
resp = s.get('https://www.tradingview.com/chart/', timeout=20)
m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
jwt_token = m.group(1) if m else None
uid = 57923804

pa_s = requests.Session()
pa_s.cookies.set('sessionid', session, domain='.tradingview.com')
pa_s.cookies.set('sessionid_sign', session_sign, domain='.tradingview.com')
pa_s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {jwt_token}',
    'Origin': 'https://www.tradingview.com',
    'Referer': 'https://www.tradingview.com/chart/',
})

# ---- Fetch ALL alerts ----
print("=== Fetching all alerts from TradingView ===\n")
r = pa_s.post('https://pricealerts.tradingview.com/list_alerts', 
              json={"uid": uid}, timeout=15)
data = r.json()
print(f"Status: {data.get('s')}")
alerts = data.get('r', [])
print(f"Total alerts found: {len(alerts)}\n")

# Save raw JSON
with open(ROOT / 'alerts_raw.json', 'w') as f:
    json.dump(data, f, indent=2)
print(f"Raw data saved to alerts_raw.json")

# Parse and display alerts nicely
print("\n" + "="*80)
print("TRADINGVIEW ALERTS FOR: ajaazkarthik1 (PRO PREMIUM)")
print("="*80 + "\n")

for i, alert in enumerate(alerts, 1):
    # Parse symbol
    sym_raw = alert.get('symbol', '')
    try:
        # Some symbols are JSON-encoded expressions
        sym_clean = sym_raw
        if sym_raw.startswith('='):
            sym_json = json.loads(sym_raw[1:])
            sym_clean = sym_json.get('symbol', sym_raw)
    except (json.JSONDecodeError, ValueError):
        sym_clean = sym_raw
    
    resolution = alert.get('resolution', 'N/A')
    condition = alert.get('condition', {})
    cond_type = condition.get('type', 'N/A')
    cond_freq = condition.get('frequency', 'N/A')
    name = alert.get('name', '')
    alert_id = alert.get('id', 'N/A')
    status = alert.get('status', 'N/A')
    message = alert.get('message', '')
    expiration = alert.get('expiration', '')
    
    # Look for price/indicator values
    cond_left = condition.get('leftOperand', condition.get('price', ''))
    cond_right = condition.get('rightOperand', condition.get('value', ''))
    
    print(f"Alert #{i}")
    print(f"  ID: {alert_id}")
    print(f"  Symbol: {sym_clean}")
    print(f"  Resolution: {resolution}")
    print(f"  Condition: {cond_type} | Freq: {cond_freq}")
    if cond_left or cond_right:
        print(f"  Left: {cond_left} | Right: {cond_right}")
    if name:
        print(f"  Name: {name}")
    if message:
        print(f"  Message: {message[:100]}")
    if status:
        print(f"  Status: {status}")
    if expiration:
        print(f"  Expiration: {expiration}")
    
    # Print full condition for detailed view
    print(f"  Full condition: {json.dumps(condition)[:200]}")
    print()

# Check if there are also watchlist alerts separately
print("\n=== Checking watchlist alerts ===")
r2 = pa_s.post('https://pricealerts.tradingview.com/list_alerts', 
               json={"uid": uid, "type": "watchlist"}, timeout=10)
d2 = r2.json()
print(f"Watchlist alerts: {d2.get('s')} - {len(d2.get('r', []))} items")

# Other useful endpoints found
print("\n=== Testing related endpoints ===")
for path in ['/list_fires', '/alert_fires', '/get_fires', '/list_fired']:
    r3 = pa_s.post(f'https://pricealerts.tradingview.com{path}', json={"uid": uid}, timeout=5)
    d3 = r3.json()
    code = d3.get('err', {}).get('code', d3.get('s'))
    if code != 'no_such_endpoint':
        print(f"  [{code}] POST {path}: {json.dumps(d3)[:150]}")
