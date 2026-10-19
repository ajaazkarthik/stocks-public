"""
Analyse the WIN (SR_SM COMBINED) and SELL strategy Pine Script sources.
Extract BUY/SELL signal conditions and summarise what triggers fires.
Also analyse the watchlist composition.
"""
import json, re
from pathlib import Path
ROOT = Path(__file__).parent.parent

# Load sources
with open(ROOT / 'pine_sources.json') as f:
    sources = json.load(f)

# Load alerts
with open(ROOT / 'alerts_raw.json') as f:
    alerts_data = json.load(f)

# ---- Analyse WIN strategy ----
print("=" * 70)
print("WIN STRATEGY ANALYSIS: SR_SM COMBINED")
print("=" * 70)

win = sources.get('WIN_SR_SM_COMBINED', {})
win_src = win.get('source', '')
print(f"  Script   : {win.get('scriptTitle')} / {win.get('scriptName')}")
print(f"  Created  : {win.get('created')}")
print(f"  Version  : {win.get('lastVersionMaj')}")
print(f"  Size     : {len(win_src)} chars")
print()

# Extract key strategy configuration lines
print("  --- Key Strategy Parameters ---")
param_patterns = [
    r'(commission_value\s*=\s*[0-9.]+)',
    r'(default_qty_value\s*=\s*[0-9.]+)',
    r'(slippage\s*=\s*[0-9]+)',
    r'(pyramiding\s*=\s*[0-9]+)',
]
for pat in param_patterns:
    m = re.search(pat, win_src)
    if m:
        print(f"    {m.group(1)}")

# Extract BUY/SELL signal logic
print("\n  --- Signal Conditions (BUY/SELL logic) ---")
# Look for alert conditions
alert_cond_match = re.findall(r'alertcondition\s*\(([^,\)]{1,120}),\s*title\s*=\s*"([^"]+)"', win_src)
for cond, title in alert_cond_match:
    print(f"    [{title}]: alertcondition({cond.strip()})")

# Look for strategy.entry/exit
buy_entries = re.findall(r'strategy\.entry\s*\(\s*"([^"]+)"\s*,\s*strategy\.(long|short)', win_src)
print(f"\n  Strategy entries: {buy_entries}")

# Look for variables that use BUY/SELL keywords
buy_vars = re.findall(r'(\w*[Bb]uy\w*|\w*[Ll]ong\w*|\w*[Bb]ullish\w*)\s*=\s*([^\n]{1,80})', win_src)
sell_vars = re.findall(r'(\w*[Ss]ell\w*|\w*[Ss]hort\w*|\w*[Bb]ear\w*)\s*=\s*([^\n]{1,80})', win_src)
print(f"\n  BUY/LONG variable assignments ({len(buy_vars)} found):")
for var, val in buy_vars[:8]:
    print(f"    {var} = {val.strip()}")
print(f"\n  SELL/SHORT variable assignments ({len(sell_vars)} found):")
for var, val in sell_vars[:8]:
    print(f"    {var} = {val.strip()}")

# Extract MA-related logic (MA Breakout Alert)
ma_lines = [l.strip() for l in win_src.split('\n') if 'MA' in l and ('alert' in l.lower() or 'break' in l.lower() or 'cross' in l.lower())]
print(f"\n  MA Alert Logic lines ({len(ma_lines)} found):")
for l in ma_lines[:10]:
    print(f"    {l}")

# ---- Watchlist Analysis ----
print("\n" + "=" * 70)  
print("WATCHLIST ANALYSIS: ALL_NEW (WIN Strategy)")
print("=" * 70)

win_alert = next((a for a in alerts_data['r'] if a.get('name') == 'WIN' and a.get('resolution') == '120'), None)
if win_alert:
    symbols = win_alert.get('symbolset_data', {}).get('symbols', [])
    print(f"\n  Total symbols: {len(symbols)}")
    
    # Group by exchange
    exchanges = {}
    for sym in symbols:
        exch = sym.split(':')[0] if ':' in sym else 'UNKNOWN'
        exchanges[exch] = exchanges.get(exch, 0) + 1
    
    print("\n  By exchange:")
    for exch, count in sorted(exchanges.items(), key=lambda x: -x[1]):
        print(f"    {exch:15s} : {count:4d} symbols")
    
    # NSE symbols sample
    nse_syms = [s.split(':')[1] for s in symbols if s.startswith('NSE:')]
    print(f"\n  NSE symbols ({len(nse_syms)} total) — first 30:")
    for i in range(0, min(30, len(nse_syms)), 5):
        print(f"    {', '.join(nse_syms[i:i+5])}")

# ---- Alert Fire Summary ----
print("\n" + "=" * 70)
print("WIN STRATEGY ALERT FIRE SUMMARY")
print("=" * 70)
for a in alerts_data['r']:
    if a.get('name') == 'WIN':
        tf = '2H' if a.get('resolution') == '120' else '1H'
        print(f"\n  [{tf}] Alert ID {a.get('alert_id')}")
        print(f"    Created    : {a.get('create_time')}")
        print(f"    Last Fire  : {a.get('last_fire_time')} UTC")
        bar_t = a.get('last_fire_bar_time', '')
        print(f"    Last Bar   : {bar_t} UTC")
        print(f"    Active     : {a.get('active')}")
        print(f"    Stop Reason: {a.get('last_stop_reason')}")
        pd = a.get('presentation_data', {})
        studies = pd.get('studies', {})
        for k, v in studies.items():
            aconds = v.get('alert_conditions', {})
            print(f"    Strategy   : {v.get('description')}")
            print(f"    Signals    :")
            for plot_key, plot_info in aconds.items():
                print(f"      [{plot_info['title']}] {plot_info['text'][:100]}")

print()
print("NOTE: Historical fire log (which specific symbols triggered and when)")
print("      requires /list_fires API endpoint whose payload format is undocumented.")
print("      The strategy fires every 1 bar (freq=60sec) when any of the 967 symbols")
print("      generates a BUY or SELL signal per the SR_SM COMBINED logic.")
