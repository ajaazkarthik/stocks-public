# coding: utf-8
import sys
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

import os
os.environ["ZERODHA_ACCESS_TOKEN"] = "BeyWPPZ91gZAQrRB2qtrAcSkGiYzqX0s"
os.environ["ZERODHA_API_KEY"]      = "a9jb8rdr88cr8a9s"
os.environ["ANGELONE_API_KEY"]     = "dkozr7EU"
os.environ["ANGELONE_CLIENT_CODE"] = "K51989642"
os.environ["ANGELONE_PIN"]         = "2468"
os.environ["ANGELONE_TOTP_SECRET"] = "6XZS25XVVSWCICOYQR45VDQCME"

# Add india-brokers to path
sys.path.insert(0, r"d:\workspace\repo\stocks\india-brokers")

print("=" * 70)
print("  TEST 1: India Enricher")
print("=" * 70)
from india_enricher import IndiaEnricher
enricher = IndiaEnricher(verbose=True)
print()
print(enricher.summary())

test_syms = ["NSE:CAPACITE", "NSE:BEL", "NSE:INFY", "NSE:RELIANCE",
             "NSE:CAMS", "NSE:LALPATHLAB", "NSE:CDSL", "NYSE:AAPL"]
print()
print("Per-symbol L5 scores:")
print("  " + "-" * 68)
for sym in test_syms:
    e = enricher.enrich(sym)
    if not e:
        print(f"  {sym:<28} non-India / no data")
        continue
    if e.get("holding"):
        badge = enricher.html_badge(e)
        print(f"  {sym:<28} HOLDING  qty={e['total_qty']:>4}  "
              f"pnl={e['combined_pnl_pct']:+6.1f}%  "
              f"L5={e['l5_score']:.1f}  {e['l5_reason']}")
    else:
        print(f"  {sym:<28} NOT HELD  L5={e['l5_score']:.1f}  {e['l5_reason']}")

print()
print("=" * 70)
print("  TEST 2: GTT Audit")
print("=" * 70)

from india_gtt_manager import _get_kite, audit
kite = _get_kite()
audit(kite, verbose=True)

print()
print("=" * 70)
print("  TEST 3: GTT Suggestions (dry-run, no orders placed)")
print("=" * 70)
from india_gtt_manager import suggest_and_place
suggest_and_place(kite, dry_run=True)

print()
print("=" * 70)
print("  TEST 4: Order Executor dry-run")
print("=" * 70)
from india_order_executor import IndiaOrderExecutor
executor = IndiaOrderExecutor()
result = executor.execute("NSE:INFY", current_price=1590.0, amount_inr=25000, dry_run=True)
print()
print("  Dry-run result:")
import json
print(json.dumps({k: v for k, v in result.items() if v is not None}, indent=4))
