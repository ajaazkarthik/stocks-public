"""Test improved AI prompt with calibration cases."""
import sys
sys.path.insert(0, '.')
from signal_stack import _call_ai_single

test_cases = [
    # Should fire BUY_NOW: score=72, strong tech/sentiment, great backtest
    {'tv_symbol': 'NASDAQ:NVDA', 'composite': 72.0,
     'scores': {'l1':{'total':7.2},'l2':{'total':8.5},'l3':{'total':6.8},'l4':{'total':5.5}},
     'meta': {'rsi':58,'adx':34,'win_rate':68,'net_pnl_pct':0.45,'relative_volume':2.1},
     'buy_advisor': {'units': 3},
     'sentiment_data': {'analyst_consensus':'BUY','news_score':0.12}},
    # Should fire WATCH: score=58, mixed signals, 1-unit backtest
    {'tv_symbol': 'NASDAQ:INTC', 'composite': 58.0,
     'scores': {'l1':{'total':5.8},'l2':{'total':6.2},'l3':{'total':5.5},'l4':{'total':5.5}},
     'meta': {'rsi':52,'adx':22,'win_rate':55,'net_pnl_pct':0.12,'relative_volume':1.1},
     'buy_advisor': {'units': 1},
     'sentiment_data': {'analyst_consensus':'HOLD','news_score':-0.01}},
    # Should fire SKIP: score=50, AVOID backtest, negative news
    {'tv_symbol': 'NYSE:F', 'composite': 50.0,
     'scores': {'l1':{'total':4.5},'l2':{'total':5.0},'l3':{'total':4.8},'l4':{'total':5.7}},
     'meta': {'rsi':42,'adx':18,'win_rate':42,'net_pnl_pct':-0.08,'relative_volume':0.8},
     'buy_advisor': {'units': 0},
     'sentiment_data': {'analyst_consensus':'SELL','news_score':-0.22}},
]

for tc in test_cases:
    r = _call_ai_single(tc)
    symbol = tc['tv_symbol']
    score = tc['composite']
    verdict = r.get('verdict', 'ERR')
    conf = r.get('confidence', '?')
    reason = r.get('reason', r.get('error', ''))[:80]
    latency = r.get('latency_s', 0)
    print(f"{symbol} ({score}): [{verdict}/{conf}] {reason} [{latency}s]")
