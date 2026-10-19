import json, statistics
from collections import defaultdict, Counter

fa = json.loads(open('fires_analysed.json').read())
tf1h = [f for f in fa if f['timeframe'] == '1H']
tf2h = [f for f in fa if f['timeframe'] == '2H']

# Exchange breakdown comparison
print('=== EXCHANGE BREAKDOWN ===')
print(f'{"Exchange":<12} {"1H count":<12} {"1H WR%":<12} {"2H count":<12} {"2H WR%":<12}')
ex1 = defaultdict(list)
ex2 = defaultdict(list)
for f in tf1h: ex1[f['exchange']].append(f['metrics'].get('WinRate', 0))
for f in tf2h: ex2[f['exchange']].append(f['metrics'].get('WinRate', 0))
all_ex = sorted(set(list(ex1.keys()) + list(ex2.keys())))
for ex in all_ex:
    wrs1 = ex1.get(ex, [])
    wrs2 = ex2.get(ex, [])
    wr1 = f'{statistics.mean(wrs1):.1f}' if wrs1 else '-'
    wr2 = f'{statistics.mean(wrs2):.1f}' if wrs2 else '-'
    print(f'{ex:<12} {len(wrs1):<12} {wr1:<12} {len(wrs2):<12} {wr2:<12}')

print()

# WinRate distribution buckets
print('=== WIN RATE DISTRIBUTION ===')
buckets = [(0,30,'<30%'),(30,40,'30-40%'),(40,50,'40-50%'),(50,60,'50-60%'),(60,75,'60-75%'),(75,101,'>=75%')]
print(f'{"Bucket":<12} {"1H count (%)":<20}       {"2H count (%)"}')
for lo, hi, label in buckets:
    c1 = sum(1 for f in tf1h if lo <= f['metrics'].get('WinRate',0) < hi)
    c2 = sum(1 for f in tf2h if lo <= f['metrics'].get('WinRate',0) < hi)
    print(f'{label:<12} {c1} ({100*c1/len(tf1h):.1f}%)             {c2} ({100*c2/len(tf2h):.1f}%)')

print()
# Signals where WinRate >= 50 AND LongTrade = True
q1 = [f for f in tf1h if f['metrics'].get('WinRate',0)>=50 and str(f['metrics'].get('LongTrade','')).lower()=='true']
q2 = [f for f in tf2h if f['metrics'].get('WinRate',0)>=50 and str(f['metrics'].get('LongTrade','')).lower()=='true']
print('HIGH CONFIDENCE (WR>=50 AND LongTrade=true):')
print(f'  1H: {len(q1)}/{len(tf1h)} = {100*len(q1)/len(tf1h):.1f}%  avg WR={statistics.mean([f["metrics"]["WinRate"] for f in q1]):.1f}%  avg NP={statistics.mean([f["metrics"]["NetProfit%"] for f in q1]):.1f}% avg ADX={statistics.mean([f["metrics"]["ADX"] for f in q1]):.1f}')
print(f'  2H: {len(q2)}/{len(tf2h)} = {100*len(q2)/len(tf2h):.1f}%  avg WR={statistics.mean([f["metrics"]["WinRate"] for f in q2]):.1f}%  avg NP={statistics.mean([f["metrics"]["NetProfit%"] for f in q2]):.1f}% avg ADX={statistics.mean([f["metrics"]["ADX"] for f in q2]):.1f}')

print()
# Noise analysis: signals that fired but are NOT actionable at all
noise1 = [f for f in tf1h if str(f['metrics'].get('BUY','')).lower()!='true'
           and str(f['metrics'].get('LongTrade','')).lower()!='true']
noise2 = [f for f in tf2h if str(f['metrics'].get('BUY','')).lower()!='true'
           and str(f['metrics'].get('LongTrade','')).lower()!='true']
print(f'NOISE SIGNALS (no BUY, no LongTrade):')
print(f'  1H: {len(noise1)}/{len(tf1h)} = {100*len(noise1)/len(tf1h):.1f}%')
print(f'  2H: {len(noise2)}/{len(tf2h)} = {100*len(noise2)/len(tf2h):.1f}%')

print()
# Full signal quality scoring
print('=== COMPOSITE SIGNAL SCORE (by alert fire) ===')
def score(m):
    s = 0
    if m.get('WinRate', 0) >= 50: s += 2
    elif m.get('WinRate', 0) >= 40: s += 1
    if str(m.get('BUY', '')).lower() == 'true': s += 3
    if str(m.get('LongTrade', '')).lower() == 'true': s += 2
    if str(m.get('sqzOff', '')).lower() == 'true': s += 1
    if m.get('ADX', 0) >= 25: s += 1
    if m.get('rsi', 50) >= 55: s += 1
    return s

scores1 = [score(f['metrics']) for f in tf1h]
scores2 = [score(f['metrics']) for f in tf2h]
print(f'  1H: mean score={statistics.mean(scores1):.2f}/10  score>=5: {sum(1 for s in scores1 if s>=5)}/{len(scores1)} ({100*sum(1 for s in scores1 if s>=5)/len(scores1):.1f}%)  score>=7: {sum(1 for s in scores1 if s>=7)}/{len(scores1)}')
print(f'  2H: mean score={statistics.mean(scores2):.2f}/10  score>=5: {sum(1 for s in scores2 if s>=5)}/{len(scores2)} ({100*sum(1 for s in scores2 if s>=5)/len(scores2):.1f}%)  score>=7: {sum(1 for s in scores2 if s>=7)}/{len(scores2)}')

print()
# Net profit outlier > 50% vs typical
print('=== NET PROFIT BREAKDOWN ===')
np1_big = [f['metrics']['NetProfit%'] for f in tf1h if f['metrics'].get('NetProfit%',0) > 50]
np2_big = [f['metrics']['NetProfit%'] for f in tf2h if f['metrics'].get('NetProfit%',0) > 50]
np1_typ = [f['metrics']['NetProfit%'] for f in tf1h if f['metrics'].get('NetProfit%',0) <= 50]
np2_typ = [f['metrics']['NetProfit%'] for f in tf2h if f['metrics'].get('NetProfit%',0) <= 50]
print(f'  1H >50% NP: {len(np1_big)} stocks  | typical (<50%) mean={statistics.mean(np1_typ):.1f}%')
print(f'  2H >50% NP: {len(np2_big)} stocks  | typical (<50%) mean={statistics.mean(np2_typ):.1f}%')

print()
# NSE vs non-NSE analysis
nse1 = [f for f in tf1h if f['exchange'] == 'NSE']
nse2 = [f for f in tf2h if f['exchange'] == 'NSE']
non1 = [f for f in tf1h if f['exchange'] != 'NSE']
non2 = [f for f in tf2h if f['exchange'] != 'NSE']
print(f'=== NSE specific ===')
print(f'  NSE 1H: {len(nse1)} fires  WR={statistics.mean([f["metrics"]["WinRate"] for f in nse1]):.1f}%  NP={statistics.mean([f["metrics"]["NetProfit%"] for f in nse1]):.1f}%' if nse1 else '  NSE 1H: 0')
print(f'  NSE 2H: {len(nse2)} fires  WR={statistics.mean([f["metrics"]["WinRate"] for f in nse2]):.1f}%  NP={statistics.mean([f["metrics"]["NetProfit%"] for f in nse2]):.1f}%' if nse2 else '  NSE 2H: 0')
print(f'  Non-NSE 1H: {len(non1)} fires  WR={statistics.mean([f["metrics"]["WinRate"] for f in non1]):.1f}%  NP={statistics.mean([f["metrics"]["NetProfit%"] for f in non1]):.1f}%' if non1 else '  Non-NSE 1H: 0')
print(f'  Non-NSE 2H: {len(non2)} fires  WR={statistics.mean([f["metrics"]["WinRate"] for f in non2]):.1f}%  NP={statistics.mean([f["metrics"]["NetProfit%"] for f in non2]):.1f}%' if non2 else '  Non-NSE 2H: 0')
