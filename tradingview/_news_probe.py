"""Quick probe: validate news API symbol-tagging quality."""
import sys
sys.path.insert(0, '.')
from tv_data.tv_news import fetch_headlines, fetch_story
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
v = SentimentIntensityAnalyzer()

PROVIDERS = ["reuters", "dow-jones", "quartr", "market-watch", "invezz", "globenewswire"]

total_sym_tagged = 0
total_articles = 0
for prov in PROVIDERS:
    items = fetch_headlines(provider=prov, limit=30)
    sym_tagged = [it for it in items if it.get('relatedSymbols')]
    total_articles += len(items)
    total_sym_tagged += len(sym_tagged)
    print(f"\n[{prov}] {len(items)} articles, {len(sym_tagged)} symbol-tagged ({100*len(sym_tagged)//max(len(items),1)}%)")
    for it in sym_tagged[:3]:
        syms = [s['symbol'] for s in it.get('relatedSymbols', [])]
        score = v.polarity_scores(it['title'])['compound']
        print(f"  [{score:+.2f}] {it['title'][:65]}")
        print(f"         -> {syms[:5]}")

print(f"\n=== TOTAL: {total_sym_tagged}/{total_articles} symbol-tagged ({100*total_sym_tagged//max(total_articles,1)}%) ===")

# Probe: what does the symbol field look like exactly?
print("\n=== Symbol format inspection ===")
items = fetch_headlines(provider='reuters', limit=10)
for it in items:
    rs = it.get('relatedSymbols', [])
    if rs:
        print(f"  relatedSymbols sample: {rs[:3]}")
        break

# Probe: can we fetch by category / topic?
print("\n=== Story body quality ===")
items = fetch_headlines(provider='quartr', limit=5)
for it in items[:2]:
    story = fetch_story(it.get('id', ''))
    short = story.get('shortDescription', '')
    full = story.get('full_text', '')
    syms = [s['symbol'] for s in it.get('relatedSymbols', [])]
    score = v.polarity_scores(short or it['title'])['compound']
    print(f"\n  [{score:+.2f}] {it['title'][:70]}")
    print(f"  Symbols: {syms[:5]}")
    print(f"  Short desc (100c): {short[:100]}")
    print(f"  Full text len: {len(full)} chars")
