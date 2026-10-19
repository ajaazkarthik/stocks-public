"""
Fix confirmed invalid symbols in watchlist_1000.json
Based on validation results and symbol probes from previous session.

REMOVES (7 confirmed invalid):
  BSE_DLY:GODAVARI    - not found in TV; no valid equivalent
  BSE_DLY:SENSEXBEES  - BSE version; NSE equivalent exists as NSE:SENSEXETF
  BSE_DLY:SNXT50BEES  - BSE version; already covered by NSE:JUNIORBEES in list
  SGX:564             - not a valid TV symbol
  SGX:M04             - not a valid TV symbol
  SGX:YYN             - not a valid TV symbol
  FX:USDSGD           - not in TV FX screener; FX_IDC:USDSGD works

ADDS (7 quality replacements):
  NSE:SENSEXETF        - NSE BSE Sensex ETF (replaces BSE_DLY:SENSEXBEES)
  FX_IDC:USDSGD        - USD/SGD forex (replaces FX:USDSGD)
  NSE:RVNL             - Rail Vikas Nigam (large infra company)
  NSE:CGPOWER          - CG Power & Industrial Solutions
  SGX_DLY:S68          - Singapore Exchange Ltd
  SGX_DLY:G13          - Genting Singapore
  SGX_DLY:BS6          - Yangzijiang Shipbuilding

NOTE: FX:USDINR was confirmed FOUND in direct probe — it stays in the list.
"""

import json
import re
import shutil
from pathlib import Path

BASE = Path(__file__).parent

REMOVE = {
    "BSE_DLY:GODAVARI",
    "BSE_DLY:SENSEXBEES",
    "BSE_DLY:SNXT50BEES",
    "SGX:564",
    "SGX:M04",
    "SGX:YYN",
    "FX:USDSGD",
}

REPLACEMENTS = [
    "NSE:SENSEXETF",      # replaces BSE_DLY:SENSEXBEES
    "FX_IDC:USDSGD",      # replaces FX:USDSGD
    "NSE:RVNL",           # Rail Vikas Nigam
    "NSE:CGPOWER",        # CG Power
    "SGX_DLY:S68",        # Singapore Exchange Ltd
    "SGX_DLY:G13",        # Genting Singapore
    "SGX_DLY:BS6",        # Yangzijiang Shipbuilding
]

# ─── Load watchlist ───────────────────────────────────────────────────────────
wl_path = BASE / "watchlist_1000.json"
watchlist = json.loads(wl_path.read_text(encoding="utf-8"))
print(f"Loaded {len(watchlist)} symbols from {wl_path.name}")

# ─── Remove invalid symbols ───────────────────────────────────────────────────
before = set(watchlist)
watchlist_filtered = [s for s in watchlist if s not in REMOVE]
removed = before - set(watchlist_filtered)
print(f"\nRemoved {len(removed)} invalid symbols:")
for s in sorted(removed):
    print(f"  - {s}")

missed = REMOVE - removed
if missed:
    print(f"\nWARNING: These were not in the list:")
    for s in sorted(missed):
        print(f"  ! {s}")

# ─── Add replacements (skip duplicates) ───────────────────────────────────────
existing = set(watchlist_filtered)
added = []
for sym in REPLACEMENTS:
    if sym in existing:
        print(f"  SKIP {sym} (already in list)")
    else:
        watchlist_filtered.append(sym)
        existing.add(sym)
        added.append(sym)

print(f"\nAdded {len(added)} replacement symbols:")
for s in added:
    print(f"  + {s}")

print(f"\nFinal count: {len(watchlist_filtered)} symbols")
assert len(watchlist_filtered) == 1000, f"Expected 1000, got {len(watchlist_filtered)}"

# Deduplicate preserving order
seen = set()
deduped = []
for s in watchlist_filtered:
    if s not in seen:
        seen.add(s)
        deduped.append(s)
if len(deduped) != len(watchlist_filtered):
    print(f"WARNING: Removed {len(watchlist_filtered)-len(deduped)} duplicates")
    watchlist_filtered = deduped
    print(f"Final after dedup: {len(watchlist_filtered)}")

# ─── Save watchlist_1000.json ─────────────────────────────────────────────────
shutil.copy(wl_path, BASE / "watchlist_1000.json.bak3")
wl_path.write_text(json.dumps(watchlist_filtered, indent=2), encoding="utf-8")
print(f"\nSaved {wl_path.name} (backup: watchlist_1000.json.bak3)")

# ─── Save import txt ──────────────────────────────────────────────────────────
import_path = BASE / "watchlist_1000_import.txt"
import_path.write_text("\n".join(watchlist_filtered), encoding="utf-8")
print(f"Saved {import_path.name}")

# ─── Update alerts_raw.json ───────────────────────────────────────────────────
alerts_path = BASE / "alerts_raw.json"
raw = alerts_path.read_text(encoding="utf-8")

symbols_line = json.dumps(watchlist_filtered)
pattern = r'("symbolset_data"\s*:\s*\{[^}]*?"symbols"\s*:\s*)\[[^\]]*?\]'

count = len(re.findall(pattern, raw))
updated = re.sub(pattern, r'\1' + symbols_line, raw)

alerts_path.write_text(updated, encoding="utf-8")
print(f"Updated alerts_raw.json (replaced {count} symbolset_data.symbols block(s))")

# ─── Exchange breakdown ───────────────────────────────────────────────────────
from collections import Counter
exchanges = Counter(s.split(":")[0] for s in watchlist_filtered)
print("\nExchange breakdown:")
for ex, cnt in sorted(exchanges.items(), key=lambda x: -x[1]):
    print(f"  {ex}: {cnt}")
