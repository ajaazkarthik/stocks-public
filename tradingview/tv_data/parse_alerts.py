import json
from pathlib import Path
ROOT = Path(__file__).parent.parent

with open(ROOT / 'alerts_raw.json') as f:
    data = json.load(f)

alerts = data['r']
print(f'Total alerts: {len(alerts)}')
print()

for i, a in enumerate(alerts, 1):
    print(f'=== ALERT {i} ===')
    print(f'  alert_id   : {a.get("alert_id")}')
    print(f'  name       : {a.get("name")}')
    print(f'  type       : {a.get("type")}')
    sym = a.get("symbol", "")
    print(f'  symbol     : {sym[:80]}')
    print(f'  resolution : {a.get("resolution")} min')
    print(f'  active     : {a.get("active")}')
    print(f'  message    : {a.get("message")}')
    print(f'  mobile_push: {a.get("mobile_push")}')
    print(f'  email      : {a.get("email")}')
    print(f'  web_hook   : {a.get("web_hook")}')
    print(f'  expiration : {a.get("expiration")}')
    print(f'  create_time: {a.get("create_time")}')
    print(f'  last_fire  : {a.get("last_fire_time")}')
    print(f'  stop_reason: {a.get("last_stop_reason")}')
    print(f'  complexity : {a.get("complexity")}')
    print(f'  kinds      : {a.get("kinds")}')
    cond = a.get('condition', {})
    print(f'  cond.type  : {cond.get("type")}')
    print(f'  cond.freq  : {cond.get("frequency")}')
    series = cond.get('series', [])
    for s in series:
        if s.get('type') == 'study':
            print(f'  pine_id    : {s.get("pine_id")}')
            print(f'  pine_ver   : {s.get("pine_version")}')
        if s.get('type') == 'value':
            print(f'  cross_val  : {s.get("value")}')
    pd = a.get('presentation_data', {})
    if 'watchlist' in pd:
        wl = pd['watchlist']
        print(f'  watchlist  : {wl.get("name")} ({wl.get("symbols_count")} symbols)')
    if 'studies' in pd:
        for k, v in pd['studies'].items():
            print(f'  study_desc : {v.get("description")} / {v.get("short_description")}')
            ac = v.get('alert_conditions', {})
            for ak, av in ac.items():
                print(f'    alert_cond [{ak}]: {av.get("title")} -> {av.get("text","")[:80]}')
    ss = a.get('symbolset_data', {})
    if ss.get('symbols'):
        print(f'  watchlist_symbols: {len(ss["symbols"])} symbols')
    print()
