"""
Parse and analyse fires_raw.json to produce a structured report.
"""
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).parent.parent

with open(ROOT / 'fires_raw.json') as f:
    raw = json.load(f)

all_fires = raw['all']
WIN_IDS = {2381963449, 2381961922}
TF_MAP = {'60': '1H', '120': '2H', '240': '4H', 'D': '1D', '30': '30m'}

def parse_message(msg):
    """Extract KV pairs from fire message string"""
    result = {}
    for key in ['WinRate', 'NetProfit%', 'rsi', 'sqzOff', 'ma_up', 'ADX', 'BUY', 'LongTrade', 'srBuySignal', 'prev_change_percent', 'timeframe']:
        m = re.search(rf'{key}:\s*([^,\n]+)', msg)
        if m:
            val = m.group(1).strip().rstrip('%')
            try:
                result[key] = float(val)
            except (ValueError, TypeError):
                result[key] = val
    # Get price 
    m = re.search(r'price:\s*([\d.]+)', msg)
    if m:
        result['price'] = float(m.group(1))
    return result

win_fires = [f for f in all_fires if f['alert_id'] in WIN_IDS]
sell_fires = [f for f in all_fires if f['alert_id'] not in WIN_IDS]

print(f"Total fires: {len(all_fires)}")
print(f"WIN alert fires: {len(win_fires)}")
print(f"Other alert fires: {len(sell_fires)}")

# Parse messages
for fire in win_fires:
    fire['_parsed'] = parse_message(fire.get('message', ''))
    sym = fire.get('pro_symbol') or fire.get('symbol', '')
    fire['_exchange'] = sym.split(':')[0] if ':' in sym else 'UNKNOWN'
    fire['_ticker'] = sym.split(':')[1] if ':' in sym else sym
    fire['_tf_label'] = TF_MAP.get(fire.get('resolution', ''), fire.get('resolution', '?'))

# Exchange breakdown
exch_counts = Counter(f['_exchange'] for f in win_fires)
print("\n=== WIN Fires by Exchange ===")
for exch, count in exch_counts.most_common():
    print(f"  {exch}: {count} fires")

# Resolution breakdown
tf_counts = Counter(f['_tf_label'] for f in win_fires)
print("\n=== WIN Fires by Timeframe ===")
for tf, count in tf_counts.most_common():
    print(f"  {tf}: {count} fires")

# Top symbols  
sym_counts = Counter(f.get('pro_symbol') or f.get('symbol') for f in win_fires)
print("\n=== Top Symbols (most frequently triggered) ===")
for sym, count in sym_counts.most_common(20):
    exch = sym.split(':')[0]
    ticker = sym.split(':')[1] if ':' in sym else sym
    fires_for_sym = [f for f in win_fires if (f.get('pro_symbol') or f.get('symbol')) == sym]
    tfs = Counter(f['_tf_label'] for f in fires_for_sym)
    print(f"  {sym}: {count}x | TF: {dict(tfs)}")

# Statistics from parsed messages
winrates = [f['_parsed'].get('WinRate') for f in win_fires if 'WinRate' in f['_parsed']]
profits = [f['_parsed'].get('NetProfit%') for f in win_fires if 'NetProfit%' in f['_parsed']]
rsi_vals = [f['_parsed'].get('rsi') for f in win_fires if 'rsi' in f['_parsed']]
sqz_off = [f['_parsed'].get('sqzOff') for f in win_fires if 'sqzOff' in f['_parsed']]

print(f"\n=== Signal Quality Metrics ===")
if winrates:
    print(f"  WinRate avg: {sum(winrates)/len(winrates):.1f}% | min: {min(winrates):.1f}% | max: {max(winrates):.1f}%")
if profits:
    print(f"  NetProfit% avg: {sum(profits)/len(profits):.1f}% | min: {min(profits):.1f}% | max: {max(profits):.1f}%")
if rsi_vals:
    print(f"  RSI avg: {sum(rsi_vals)/len(rsi_vals):.1f} | min: {min(rsi_vals):.1f} | max: {max(rsi_vals):.1f}")

sqz_true = sum(1 for v in sqz_off if str(v).lower() in ('1.0', 'true'))
sqz_false = sum(1 for v in sqz_off if str(v).lower() in ('0.0', 'false'))
print(f"  Squeeze OFF (momentum): {sqz_true}/{len(sqz_off)}")

# High quality signals (WinRate > 50% AND NetProfit% > 15%)
hq = [f for f in win_fires if f['_parsed'].get('WinRate', 0) > 50 and f['_parsed'].get('NetProfit%', 0) > 15]
print(f"\n=== HIGH QUALITY Signals (WinRate>50% AND NetProfit>15%) ===")
print(f"  Count: {len(hq)}")
for fire in sorted(hq, key=lambda x: -x['_parsed'].get('NetProfit%', 0))[:15]:
    sym = fire.get('pro_symbol') or fire.get('symbol')
    p = fire['_parsed']
    tf = fire['_tf_label']
    print(f"  {sym} [{tf}] WinRate={p.get('WinRate'):.0f}% NetProfit={p.get('NetProfit%'):.1f}% RSI={p.get('rsi', 0):.1f} sqzOff={p.get('sqzOff')}")

# Most recent fires (last 10)
recent = sorted(win_fires, key=lambda x: -x.get('fire_id', 0))[:10]
print(f"\n=== Most Recent WIN Fires ===")
for fire in recent:
    sym = fire.get('pro_symbol') or fire.get('symbol')
    p = fire['_parsed']
    tf = fire['_tf_label']
    price = p.get('price', '?')
    winrate = p.get('WinRate', '?')
    print(f"  {sym} [{tf}] price={price} WinRate={winrate}% RSI={p.get('rsi', '?')}")

# NSE specific
nse_fires = [f for f in win_fires if f['_exchange'] == 'NSE']
print(f"\n=== NSE Fires ({len(nse_fires)}) ===")
nse_syms = Counter(f['_ticker'] for f in nse_fires)
for sym, count in nse_syms.most_common():
    fires_for = [f for f in nse_fires if f['_ticker'] == sym]
    p = fires_for[0]['_parsed']
    print(f"  NSE:{sym} ({count}x) WinRate={p.get('WinRate', '?')}% NetProfit={p.get('NetProfit%', '?'):.1f}% price={p.get('price', '?')}")

# ASX specific
asx_fires = [f for f in win_fires if f['_exchange'] == 'ASX']
print(f"\n=== ASX Fires ({len(asx_fires)}) ===")
for fire in sorted(asx_fires, key=lambda x: -x.get('fire_id', 0)):
    sym = fire['_ticker']
    p = fire['_parsed']
    tf = fire['_tf_label']
    print(f"  ASX:{sym} [{tf}] WinRate={p.get('WinRate', '?')}% NetProfit={p.get('NetProfit%', '?')}% price={p.get('price', '?')}")

# Save analysed data
analysed = []
for fire in sorted(win_fires, key=lambda x: -x.get('fire_id', 0)):
    analysed.append({
        'fire_id': fire.get('fire_id'),
        'alert_id': fire.get('alert_id'),
        'symbol': fire.get('pro_symbol') or fire.get('symbol'),
        'timeframe': fire['_tf_label'],
        'exchange': fire['_exchange'],
        'message': fire.get('message', ''),
        'metrics': fire['_parsed'],
    })
with open(ROOT / 'fires_analysed.json', 'w') as f:
    json.dump(analysed, f, indent=2)
print(f"\nSaved {len(analysed)} analysed fires to fires_analysed.json")
