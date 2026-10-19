"""Quick test to diagnose AI API errors."""
import sys
sys.path.insert(0, '.')
from signal_stack import _AI_CLIENT, _AI_MODEL, _call_ai_single

print(f"Model: {_AI_MODEL}")
print(f"Client: {'OK' if _AI_CLIENT else 'None'}")

# Simple direct test
try:
    resp = _AI_CLIENT.chat.completions.create(
        model=_AI_MODEL,
        messages=[
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": 'Classify: Symbol=NVDA, Score=72/100. Return {"verdict":"BUY_NOW","confidence":"HIGH","reason":"strong tech"}'}
        ],
        max_tokens=80,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    print("SUCCESS:", resp.choices[0].message.content)
except Exception as e:
    print(f"ERROR type: {type(e).__name__}")
    print(f"ERROR msg: {str(e)[:500]}")

# Also test _call_ai_single with a dummy item
dummy = {
    "tv_symbol": "NASDAQ:NVDA",
    "composite": 72.0,
    "scores": {"l1": {"total": 6.0}, "l2": {"total": 8.6}, "l3": {"total": 7.6}, "l4": {"total": 6.3}},
    "meta": {"rsi": 58, "adx": 32, "win_rate": 68, "net_pnl_pct": 0.42},
    "market_internals": {"vix": 19.2},
    "fundamentals": {"sector": "Technology"},
    "sentiment_data": {"analyst_consensus": "BUY", "news_score": 0.12},
}
print("\nCalling _call_ai_single...")
result = _call_ai_single(dummy)
print("Result:", result)
