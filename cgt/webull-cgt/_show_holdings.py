from webull_statement_reader import parse_all_pdfs
data = parse_all_pdfs('../../webull-statement')
print(f"\n{'Sym':8} {'Qty':>8} {'Avg':>10} {'Close':>10} {'UPL':>10} {'Cur':>4} {'Exch':>6}")
print("-" * 65)
for h in sorted(data['holdings'], key=lambda x: x['symbol']):
    print(f"{h['symbol']:8} {h['qty']:>8} {h['avg_price']:>10.4f} {h['close_price']:>10.4f} {h['unrealized_pnl']:>10.2f} {h['currency']:>4} {h['exchange']:>6}")
print(f"\nTotal: {len(data['holdings'])} holdings")
