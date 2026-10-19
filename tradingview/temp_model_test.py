"""Test gpt-4.1-nano for stock signal classification."""
import sys, json
sys.path.insert(0, '.')
from signal_stack import _AI_CLIENT

# Test gpt-4.1-nano with json_object response format
try:
    resp = _AI_CLIENT.chat.completions.create(
        model='gpt-4.1-nano',
        messages=[
            {'role': 'system', 'content': 'You are a stock signal classifier. Return strict JSON only.'},
            {'role': 'user', 'content': 'Symbol: NVDA, Score: 72/100, Tech=6.0, Fund=8.6, Sent=7.6, RSI=58, ADX=32, Sector=Technology. Verdict choices: BUY_NOW, WATCH, SKIP. Return {"verdict":"...", "confidence":"HIGH|MED|LOW", "reason":"..."}'}
        ],
        max_tokens=120,
        temperature=0.1,
        response_format={'type': 'json_object'},
    )
    print('Model: gpt-4.1-nano')
    print('Response:', resp.choices[0].message.content)
    data = json.loads(resp.choices[0].message.content)
    print('Parsed:', data)
    print()
    print('SUCCESS - gpt-4.1-nano works with JSON mode')
except Exception as e:
    print(f'gpt-4.1-nano error: {e}')
    # Try gpt-4.1-mini
    try:
        resp2 = _AI_CLIENT.chat.completions.create(
            model='gpt-4.1-mini',
            messages=[
                {'role': 'system', 'content': 'You are a stock signal classifier. Return strict JSON only.'},
                {'role': 'user', 'content': 'Symbol: NVDA, Score: 72/100. Classify. Return {"verdict":"BUY_NOW", "confidence":"HIGH", "reason":"strong"}'}
            ],
            max_tokens=80,
            temperature=0.1,
            response_format={'type': 'json_object'},
        )
        print('gpt-4.1-mini works:', resp2.choices[0].message.content)
    except Exception as e2:
        print(f'gpt-4.1-mini error: {e2}')
