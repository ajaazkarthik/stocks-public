"""Extract and analyze AI verdicts from the archived HTML report."""
import re

with open('../temp/bench_ai_best_report.html', encoding='utf-8', errors='replace') as f:
    html = f.read()

# Count verdicts by CSS class
ai_blocks = html.count('class="ai-block"')
buy_now_class = html.count('ai-buy-now')
watch_class_raw = html.count('ai-watch')
skip_class = html.count('ai-skip')

buy_now_count = buy_now_class
watch_count = watch_class_raw - buy_now_class - skip_class  # ai-watch appears in verdict badge only

print(f"AI verdict distribution (top 30 from 166 stocks):")
print(f"  Total AI blocks in HTML: {ai_blocks}")
print(f"  BUY NOW: {buy_now_count}")
print(f"  WATCH:   {watch_count}")
print(f"  SKIP:    {skip_class}")
print()

# Extract reason texts
reason_pattern = r'<div class="ai-reason">(.*?)</div>'
reasons = re.findall(reason_pattern, html, re.DOTALL)
print(f"AI reasons found: {len(reasons)}")
print()

# Extract verdict + symbol correlation
# Look for card headers with symbol names near AI blocks
card_pattern = r'<h3[^>]*>(.*?)</h3>.*?(?:<div class="ai-block">.*?</div>)?'
# Simpler: find the card symbol from the report's rank list
# Extract top 10 symbols with their AI verdict
blocks = re.findall(r'<div class="ai-block">(.*?)</div>\s*</div>', html, re.DOTALL)
print(f"AI block content samples:")
for i, b in enumerate(blocks[:15], 1):
    verdict_m = re.search(r'(BUY NOW|WATCH|SKIP)', b)
    conf_m = re.search(r'([A-Z]{2,4}) confidence', b)
    reason_m = re.search(r'<div class="ai-reason">(.*?)</div>', b, re.DOTALL)
    v = verdict_m.group(1) if verdict_m else '?'
    c = conf_m.group(1) if conf_m else '?'
    r = reason_m.group(1)[:80].strip() if reason_m else '(no reason)'
    print(f"  #{i}: {v} ({c}) — {r}")
