"""Quick backtest summary for sell strategy analysis."""
import yfinance as yf, pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')

STOCKS = [
    ('NVDA','NVDA','1d','5y'),
    ('AAPL','AAPL','1d','5y'),
    ('AMD','AMD','1d','5y'),
    ('ORCL','ORCL','1d','5y'),
    ('UBER','UBER','1d','5y'),
    ('JNJ','JNJ','1d','5y'),
]

ATR_S=2.0; ATR_T=1.5; ATR_WIDE=2.5; PROFIT_TIER_PCT=10.0; RSI_E=50; RSI_DECAY_BARS=2

def atr(h,lo,c,p=14):
    pc=c.shift(1)
    tr=pd.concat([h-lo,(h-pc).abs(),(lo-pc).abs()],axis=1).max(axis=1)
    return tr.ewm(com=p-1,min_periods=p).mean()

def rsi(c,p=14):
    d=c.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(com=p-1,min_periods=p).mean()
    al=l.ewm(com=p-1,min_periods=p).mean().replace(0,np.nan)
    return 100-100/(1+ag/al)

all_trades=[]
print(f"{'Symbol':12} {'#':>3} {'WR%':>5} {'AvgWin':>7} {'AvgLoss':>8} {'PF':>5}  Top exit reasons")
print('-'*100)

for tv,yt,iv,pr in STOCKS:
    df=yf.download(yt,interval=iv,period=pr,auto_adjust=True,progress=False,multi_level_index=False).dropna()
    c=df.Close; h=df.High; lo=df.Low
    a=atr(h,lo,c)
    r=rsi(c)
    ma=c.rolling(20).mean(); ma_up=ma>ma.shift(3)
    ema12=c.ewm(span=12,adjust=False).mean(); ema26=c.ewm(span=26,adjust=False).mean()
    macd=ema12-ema26; sig_m=macd.ewm(span=9,adjust=False).mean()
    up=h-h.shift(1); dn=lo.shift(1)-lo
    pdi=100*pd.Series(np.where((up>dn)&(up>0),up,0.),index=h.index).ewm(com=13,min_periods=14).mean()/a
    mdi=100*pd.Series(np.where((dn>up)&(dn>0),dn,0.),index=h.index).ewm(com=13,min_periods=14).mean()/a
    buy_raw=(macd>sig_m)&(r>=60)&ma_up&(pdi>mdi)&(pdi>30)
    buy_e=buy_raw&~buy_raw.shift(1).fillna(False)
    
    in_t=False; ep=hwm=eb=None; trades=[]; rsi_below_cnt=0
    for i in range(40, len(df)):
        cc=float(c.iloc[i]); ca=float(a.iloc[i]); cr=float(r.iloc[i])
        # Improved: simplified MA slope (not strict edge-triggered)
        ma_slope_break=(ma.iloc[i]<ma.iloc[i-3]) and (c.iloc[i]<ma.iloc[i])
        # Improved: MACD cross + histogram confirmation (negative AND declining)
        hist_cur=float(macd.iloc[i]-sig_m.iloc[i]); hist_prv=float(macd.iloc[i-1]-sig_m.iloc[i-1])
        mxu=(macd.iloc[i]<sig_m.iloc[i]) and (macd.iloc[i-1]>=sig_m.iloc[i-1]) and hist_cur<0 and hist_cur<hist_prv
        # Improved: RSI decay requires RSI_DECAY_BARS consecutive bars below RSI_E
        rsi_below_cnt = (rsi_below_cnt+1) if (cr<RSI_E and cr<float(r.iloc[i-1])) else 0
        rsi_decay = rsi_below_cnt >= RSI_DECAY_BARS
        if not in_t:
            rsi_below_cnt=0
            if buy_e.iloc[i]: in_t=True; ep=cc; hwm=cc; eb=i
        else:
            hwm=max(hwm,cc)
            unrealised_pct=(cc-ep)/ep*100.0
            # Adaptive trail: widen when in big profit
            eff_trail = ATR_WIDE if unrealised_pct >= PROFIT_TIER_PCT else ATR_T
            reason=None
            if cc < ep - ATR_S*ca:               reason='ATR_HARD'
            elif cc < hwm - eff_trail*ca:         reason='ATR_TRAIL' if unrealised_pct<PROFIT_TIER_PCT else 'ATR_WIDE'
            elif ma_slope_break:                  reason='MA_BREAK'
            elif mxu:                             reason='MACD_CROSS'
            elif rsi_decay:                       reason='RSI_DECAY'
            if reason:
                pnl=(cc-ep)/ep*100; hold=i-eb
                left=(hwm-cc)/ep*100
                trades.append({'sym':tv,'pnl':pnl,'reason':reason,'hold':hold,'left':left,'ep':ep,'hwm':hwm,'cc':cc})
                in_t=False; rsi_below_cnt=0

    wins=[t for t in trades if t['pnl']>0]
    losses=[t for t in trades if t['pnl']<=0]
    rc={}
    for t in trades: rc[t['reason']]=rc.get(t['reason'],0)+1
    wr=len(wins)/len(trades)*100 if trades else 0
    aw=float(np.mean([t['pnl'] for t in wins])) if wins else 0
    al_=float(np.mean([t['pnl'] for t in losses])) if losses else 0
    pf=abs(sum(t['pnl'] for t in wins))/abs(sum(t['pnl'] for t in losses)) if losses else 999
    top_r=sorted(rc.items(),key=lambda x:-x[1])[:3]
    top_str=" | ".join(f"{r2}:{cnt}" for r2,cnt in top_r)
    print(f'{tv:12} {len(trades):3d} {wr:5.0f}% {aw:+7.1f}% {al_:+8.1f}%  {pf:5.2f}x  {top_str}')
    all_trades.extend(trades)

print()
print('='*60)
print('OVERALL SELL STRATEGY ANALYSIS')
print('='*60)
wins=[t for t in all_trades if t['pnl']>0]
losses=[t for t in all_trades if t['pnl']<=0]
print(f'Total trades:   {len(all_trades)}')
print(f'Overall WR:     {len(wins)/len(all_trades)*100:.1f}%')
print(f'Avg Win:        {float(np.mean([t["pnl"] for t in wins])):+.2f}%')
print(f'Avg Loss:       {float(np.mean([t["pnl"] for t in losses])):+.2f}%')
pf=abs(sum(t['pnl'] for t in wins))/abs(sum(t['pnl'] for t in losses)) if losses else 0
print(f'Profit Factor:  {pf:.2f}x')
print()
print('EXIT REASON ANALYSIS:')
print(f"{'Reason':14} {'Count':>5} {'%':>4} {'WR':>5} {'AvgPnL':>8} {'AvgLeft':>9}  Diagnosis")
print('-'*80)
rc_all={}
for t in all_trades: rc_all[t['reason']]=rc_all.get(t['reason'],0)+1
for r2,cnt in sorted(rc_all.items(),key=lambda x:-x[1]):
    sub=[t for t in all_trades if t['reason']==r2]
    sw=[t for t in sub if t['pnl']>0]
    avg_pnl=float(np.mean([t['pnl'] for t in sub])) if sub else 0
    avg_left=float(np.mean([t['left'] for t in sub])) if sub else 0
    wr2=len(sw)/len(sub)*100
    pct=cnt/len(all_trades)*100
    
    if r2=='ATR_HARD':
        diag='Risk control - fires when price drops fast. Check: are we exiting too late?'
    elif r2=='ATR_TRAIL':
        diag='Most common exit. left>2%: may exit too early on volatile stocks'
    elif r2=='MA_BREAK':
        diag='Trend reversal signal. Good WR expected. Confirm vs holding time'
    elif r2=='MACD_CROSS':
        diag='Early momentum exit. Often fires before real reversal'
    elif r2=='RSI_DECAY':
        diag='Momentum decay. Reliable but can be noisy in consolidation'
    else:
        diag=''
    
    print(f'{r2:14} {cnt:5d} {pct:4.0f}% {wr2:5.0f}% {avg_pnl:+8.2f}% {avg_left:+9.2f}%  {diag[:45]}')

print()
print('TOP 10 BEST TRADES:')
best=sorted(all_trades,key=lambda x:-x['pnl'])[:10]
for t in best:
    print(f'  {t["sym"]:8} +{t["pnl"]:.1f}%  hold={t["hold"]}d  exit={t["reason"]}')

print()
print('TOP 10 WORST TRADES:')
worst=sorted(all_trades,key=lambda x:x['pnl'])[:10]
for t in worst:
    print(f'  {t["sym"]:8} {t["pnl"]:.1f}%  hold={t["hold"]}d  exit={t["reason"]}')

print()
print('SELL IMPROVEMENT RECOMMENDATIONS:')
# Calc specific insights
trail_wins=[t for t in all_trades if t['reason']=='ATR_TRAIL' and t['pnl']>0]
trail_losses=[t for t in all_trades if t['reason']=='ATR_TRAIL' and t['pnl']<=0]
macd_wins=[t for t in all_trades if t['reason']=='MACD_CROSS' and t['pnl']>0]
macd_losses=[t for t in all_trades if t['reason']=='MACD_CROSS' and t['pnl']<=0]
rsi_wins=[t for t in all_trades if t['reason']=='RSI_DECAY' and t['pnl']>0]
rsi_losses=[t for t in all_trades if t['reason']=='RSI_DECAY' and t['pnl']<=0]

trail=[t for t in all_trades if t['reason']=='ATR_TRAIL']
if trail:
    tw_pct=len(trail_wins)/len(trail)*100
    avg_left_trail=float(np.mean([t['left'] for t in trail]))
    if tw_pct>60 and avg_left_trail>2:
        print(f'1. ATR Trail ({tw_pct:.0f}% wins, avg {avg_left_trail:.1f}% left on table):')
        print(f'   -> Wide winners being cut. Consider: trail_mult=2.0x when PnL>+5%')

macd_t=[t for t in all_trades if t['reason']=='MACD_CROSS']
if macd_t:
    mw_pct=len(macd_wins)/len(macd_t)*100
    avg_pnl_macd=float(np.mean([t['pnl'] for t in macd_t]))
    print(f'2. MACD Cross ({mw_pct:.0f}% wins, avg PnL {avg_pnl_macd:+.1f}%):')
    if mw_pct<50:
        print(f'   -> Low win rate. Add guard: only exit if RSI also <55 OR volume declining')
    else:
        print(f'   -> Solid. Consider keeping as is or requiring histogram negative for 2 bars')

rsi_t=[t for t in all_trades if t['reason']=='RSI_DECAY']
if rsi_t:
    rw_pct=len(rsi_wins)/len(rsi_t)*100
    avg_pnl_rsi=float(np.mean([t['pnl'] for t in rsi_t]))
    print(f'3. RSI Decay ({rw_pct:.0f}% wins, avg PnL {avg_pnl_rsi:+.1f}%):')
    if rw_pct<45:
        print(f'   -> Unreliable on daily. Tighten: require RSI<42 for 3 bars before exit')
    else:
        print(f'   -> Reasonable. Consider RSI_EXIT=40 to reduce false signals in bull markets')
