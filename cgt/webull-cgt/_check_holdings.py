#!/usr/bin/env python3
"""Quick check of parsed holdings from PDF statements."""
from webull_statement_reader import parse_all_pdfs

data = parse_all_pdfs('../../webull-statement')
print('\n=== HOLDINGS ===')
for h in sorted(data['holdings'], key=lambda x: x['symbol']):
    print(f"{h['symbol']:8} qty={h['qty']:>8} avg={h['avg_price']:>10.4f} close={h['close_price']:>10.4f} upl={h['unrealized_pnl']:>10.2f} cur={h['currency']} ex={h['exchange']} name={h.get('name','')[:40]}")
print(f"\nTotal holdings: {len(data['holdings'])}")
print(f"Warnings: {data['warnings']}")

# Also check from webull_au_live if available
try:
    from webull_au_live import get_holdings
    live = get_holdings()
    if live:
        print('\n=== LIVE HOLDINGS (webull_au_live) ===')
        for h in sorted(live, key=lambda x: x.get('symbol', '')):
            print(f"{h.get('symbol',''):8} qty={h.get('qty',0):>8} avg={h.get('avg_price',0):>10.4f} mkt={h.get('market_price',0):>10.4f} cur={h.get('currency','')}")
        print(f"Total live: {len(live)}")
except Exception as e:
    print(f"\n(Live holdings not available: {e})")
