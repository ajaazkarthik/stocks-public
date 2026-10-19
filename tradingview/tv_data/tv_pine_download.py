"""
Download all user Pine Scripts and their source code.
Also try /list_fires with API version header.
"""
import requests, re, json
from pathlib import Path
ROOT = Path(__file__).parent.parent

SESSION_ID = 'tqiinu7hetfknzfo7fsp6rbr4fncdm2w'
SESSION_SIGN = 'v3:hqixHMngHBdKAWBNlaF2p8pGe3HGOxBqj/sqzx4zYaE='
USER_ID = 57923804

s = requests.Session()
s.cookies.set('sessionid', SESSION_ID, domain='.tradingview.com')
s.cookies.set('sessionid_sign', SESSION_SIGN, domain='.tradingview.com')
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://www.tradingview.com/',
})

resp = s.get('https://www.tradingview.com/chart/', timeout=20)
m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
jwt = m.group(1)

# ---- Get all saved scripts ----
print("=== All Saved Pine Scripts ===\n")
r = s.get('https://pine-facade.tradingview.com/pine-facade/list/?filter=saved', timeout=15)
scripts = r.json()
print(f"Total saved scripts: {len(scripts)}\n")
for sc in scripts:
    print(f"  {sc.get('scriptIdPart')}  v{sc.get('version')} [{sc.get('extra',{}).get('kind','?')}]")
    print(f"    Name  : {sc.get('scriptName')} / {sc.get('scriptTitle')}")
    print(f"    Modified: {sc.get('modified')}")
    print()

with open(ROOT / 'pine_scripts_list.json', 'w') as f:
    json.dump(scripts, f, indent=2)
print("Saved to pine_scripts_list.json\n")

# ---- Download Pine Script sources ----
PINE_IDS = {
    'WIN_SR_SM_COMBINED': ('USER;91cbee5a4fdd4f2f9b1f2dd32c51ce11', '19'),
    'SELL_STRATEGY':      ('USER;34152e1cd8524db59808220de501a9aa', '3'),
}

# Also download any other scripts from saved list
for sc in scripts:
    pid = sc.get('scriptIdPart')
    ver = sc.get('lastVersionMaj', sc.get('version', '')).split('.')[0]
    name_key = sc.get('scriptName', 'unknown').replace(' ', '_').upper()
    if pid not in [v[0] for v in PINE_IDS.values()]:
        PINE_IDS[name_key] = (pid, ver)

print("=== Pine Script Sources ===\n")
pine_sources = {}
for key, (pid, ver) in PINE_IDS.items():
    r = s.get(f'https://pine-facade.tradingview.com/pine-facade/get/{pid}/{ver}/', timeout=15)
    if r.status_code == 200:
        data = r.json()
        src = data.get('source', '')
        pine_sources[key] = data
        print(f"--- {key} ---")
        print(f"  ID      : {pid} v{data.get('lastVersionMaj', ver)}")
        print(f"  Title   : {data.get('scriptTitle')} / {data.get('scriptName')}")
        print(f"  Created : {data.get('created')}")
        print(f"  Source  : {len(src)} chars")
        # Show first 20 lines
        lines = src.split('\n')[:25]
        for line in lines:
            print(f"    {line}")
        print("    ...")
        print()
    else:
        print(f"  ERROR {r.status_code} for {pid}")

with open(ROOT / 'pine_sources.json', 'w') as f:
    json.dump(pine_sources, f, indent=2)
print("\nSaved to pine_sources.json")

# ---- One more shot at /list_fires using Tradingview's TV-Authorization header ----
print("\n=== Last attempt on /list_fires with TV-Authorization ===")
pa = requests.Session()
pa.cookies.set('sessionid', SESSION_ID, domain='.tradingview.com')
pa.cookies.set('sessionid_sign', SESSION_SIGN, domain='.tradingview.com')
pa.headers.update({
    'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
    'Content-Type': 'application/json', 'Origin': 'https://www.tradingview.com',
    'Authorization': f'Bearer {jwt}',
})

ALERT_IDS = [2381963449, 2381961922]

alt_headers = [
    {'TV-Authorization': jwt},
    {'X-TV-Authorization': jwt},
    {'X-TV-Token': jwt},
    {'TV-Token': jwt},
    {'X-Auth-Token': jwt},
]

for h in alt_headers:
    r = pa.post('https://pricealerts.tradingview.com/list_fires',
                json={"uid": USER_ID, "alert_id": ALERT_IDS[0]},
                headers=h, timeout=10)
    try:
        b = r.json()
        code = b.get('err', {}).get('code', 'ok')
        if b.get('s') == 'ok':
            print(f"  OK with header {list(h.keys())[0]}: {json.dumps(b.get('r',''))[:300]}")
        elif code != 'no_such_endpoint':
            print(f"  {code} with extra header {list(h.keys())[0]}")
    except Exception:
        print(f"  HTTP {r.status_code} with header {list(h.keys())[0]}")

print("\nDone")
