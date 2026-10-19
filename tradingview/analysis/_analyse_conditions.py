"""
Extract the BUY and SELL signal conditions from both strategy scripts.
Find the exact condition logic.
"""
import json, re
from pathlib import Path
ROOT = Path(__file__).parent.parent

with open(ROOT / 'pine_sources.json') as f:
    sources = json.load(f)

for key in ['WIN_SR_SM_COMBINED', 'SELL_STRATEGY']:
    src = sources.get(key, {}).get('source', '')
    title = sources.get(key, {}).get('scriptTitle', key)
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")

    # Extract all alertcondition calls
    ac = re.findall(r'alertcondition\s*\([^)]{1,200}\)', src)
    print(f"\n  alertcondition() calls ({len(ac)}):")
    for c in ac:
        print(f"    {c.strip()}")

    # BUY signal variable
    buy_match = re.search(r'buyCondition\s*=\s*([^\n]+)', src)
    if buy_match:
        print(f"\n  buyCondition = {buy_match.group(1).strip()}")

    sell_match = re.search(r'sellCondition\s*=\s*([^\n]+)', src)
    if sell_match:
        print(f"  sellCondition = {sell_match.group(1).strip()}")

    # BUY signal variable  
    for var in ['Long', 'BUY', 'buySignal', 'longSignal']:
        m = re.search(rf'\b{var}\b\s*=\s*([^\n]{{1,100}})', src)
        if m and '>' not in m.group(1) and 'strategy' not in m.group(1).lower():
            print(f"  {var} = {m.group(1).strip()}")

    # SELL signal variable
    for var in ['Short', 'SELL', 'sellSignal', 'shortSignal', 'BearCon']:
        m = re.search(rf'\b{var}\b\s*=\s*([^\n]{{1,100}})', src)
        if m and '>' not in m.group(1) and 'strategy' not in m.group(1).lower():
            print(f"  {var} = {m.group(1).strip()}")

    # RSI condition
    rsi_m = re.search(r'rsi\s*(?:>=|<=|>|<|==)\s*\d+', src)
    if rsi_m:
        print(f"\n  RSI filter: {rsi_m.group(0)}")

    # MACD condition  
    macd_m = re.search(r'macdAboveSignal\s*=\s*([^\n]+)', src)
    if macd_m:
        print(f"  MACD filter: macdAboveSignal = {macd_m.group(1).strip()}")

    # Volume condition
    vol_m = re.search(r'volume\s*(?:>=|<=|>|<|==)\s*[^\n]+', src, re.IGNORECASE)
    if vol_m:
        print(f"  Volume condition: {vol_m.group(0).strip()}")

    # Squeeze momentum
    sqz_m = re.search(r'sqz\w*\s*=\s*([^\n]{1,80})', src)
    if sqz_m:
        print(f"  Squeeze: {sqz_m.group(0).strip()}")

    # Strategy entry/exit conditions
    entries = re.findall(r'strategy\.(?:entry|close)\s*\([^)]{1,100}\)', src)
    print(f"\n  Strategy orders ({len(entries)}):")
    for e in entries[:6]:
        print(f"    {e.strip()}")

# SR_SM_V2 Enhanced analysis (the IBKR integration script)
print(f"\n{'='*60}")
print("SR_SM_V2 ENHANCED (IBKR Integration Strategy)")
print(f"{'='*60}")
v2 = sources.get('SR_SM_V2_ENHANCED', {})
v2_src = v2.get('source', '')
print(f"  Size: {len(v2_src)} chars")

# Find webhook/IBKR related section
webhook_m = re.search(r'(enableWebhook.{0,2000})', v2_src, re.DOTALL)
if webhook_m:
    lines = webhook_m.group(1).split('\n')[:30]
    print('\n  Webhook/IBKR section:')
    for l in lines:
        print(f"    {l}")

# Find alert message format
alert_msg = re.findall(r'alert\s*\([^)]{1,300}\)', v2_src)
print(f"\n  Alert calls ({len(alert_msg)}):")
for am in alert_msg[:5]:
    print(f"    {am[:200]}")
