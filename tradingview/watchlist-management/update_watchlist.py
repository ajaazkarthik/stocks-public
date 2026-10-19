#!/usr/bin/env python3
"""
Watchlist Optimizer for TradingView ALL_NEW watchlist.

Actions performed:
1. Remove BSE_DLY duplicates (same ETFs already on NSE)
2. Remove low-quality/troubled NSE stocks  
3. Add commodity ETFs (Gold, Silver, Oil) across NSE, BATS, ASX_DLY
4. Keep final count well under 1000
"""

import json
import copy
from pathlib import Path

# ─── Symbols to REMOVE ────────────────────────────────────────────────────────

# BSE_DLY ETFs that are exact duplicates of NSE ETFs (same ETF, listed on BSE)
BSE_DUPLICATES = {
    "BSE_DLY:NIFTYBEES",    # NSE:NIFTYBEES exists
    "BSE_DLY:BANKBEES",     # NSE:BANKBEES exists
    "BSE_DLY:JUNIORBEES",   # NSE:JUNIORBEES exists
    "BSE_DLY:INFRABEES",    # NSE:INFRABEES exists
    "BSE_DLY:PSUBNKBEES",   # NSE:PSUBNKBEES exists
    "BSE_DLY:NIF100BEES",   # NSE:NIF100BEES exists
    "BSE_DLY:HNGSNGBEES",   # NSE:HNGSNGBEES exists
    "BSE_DLY:SHARIABEES",   # NSE:SHARIABEES exists
    "BSE_DLY:CPSEETF",      # NSE:CPSEETF exists
    "BSE_DLY:MID150CASE",   # NSE:MID150CASE exists
    "BSE_DLY:TOP100CASE",   # NSE:TOP100CASE exists
}

# NSE stocks with severe fundamental/governance issues — not suitable for signal tracking
NSE_LOW_QUALITY = {
    "NSE:RELINFRA",   # Reliance Infrastructure – Anil Ambani group, extreme debt, governance failures, multiple defaults
    "NSE:RPOWER",     # Reliance Power – Anil Ambani group, near-bankruptcy history, regulatory violations
    "NSE:DHANI",      # Dhani Services (ex-IndiaBulls Consumer) – revenue collapse, business model failure
    "NSE:IMAGICAA",   # Imagicaaworld Entertainment – persistent losses, heavy debt, niche sector
}

# Additional BSE-only duplicates of NSE-listed companies (different BSE symbol, same company)
BSE_COMPANY_DUPLICATES = {
    "BSE_DLY:TARIL",  # Transrail Lighting Ltd on BSE (BSE:543982) – already in NSE:TRANSRAILL
}

SYMBOLS_TO_REMOVE = BSE_DUPLICATES | NSE_LOW_QUALITY | BSE_COMPANY_DUPLICATES

# ─── Symbols to ADD ───────────────────────────────────────────────────────────

# NSE commodity ETFs (Gold & Silver)
NSE_COMMODITY_ETFS = [
    "NSE:GOLDBEES",     # Nippon India ETF Gold BeES – India's most liquid gold ETF (~Rs 900 NAV)
    "NSE:GOLDCASE",     # ICICI Pru Gold ETF – second most liquid gold ETF on NSE
    "NSE:AXISGOLD",     # Axis Gold ETF – diversification within gold segment
    "NSE:SILVERBEES",   # Nippon India ETF Silver – most liquid silver ETF on NSE
    "NSE:SILVERCASE",   # ICICI Pru Silver ETF – solid alternative silver ETF
]

# US commodity ETFs (via BATS feed)
BATS_COMMODITY_ETFS = [
    "BATS:GLD",     # SPDR Gold Shares – world's largest gold ETF by AUM
    "BATS:IAU",     # iShares Gold Trust – lower-cost gold alternative
    "BATS:SLV",     # iShares Silver Trust – largest silver ETF
    "BATS:GDX",     # VanEck Gold Miners ETF – large/mid-cap gold mining companies
    "BATS:GDXJ",    # VanEck Junior Gold Miners ETF – smaller-cap gold miners
    "BATS:USO",     # United States Oil Fund – WTI crude oil price exposure
    "BATS:XLE",     # Energy Select Sector SPDR – integrated oil & gas sector ETF
    "BATS:XME",     # SPDR S&P Metals & Mining ETF – metals, mining, coal, steel
    "BATS:DBC",     # Invesco DB Commodity Index – diversified commodity basket
    "BATS:FCX",     # Freeport-McMoRan – world's largest publicly traded copper/gold miner
    "BATS:NEM",     # Newmont Corp – world's largest gold mining company (already on ASX, US listing)
]

# ASX commodity ETFs (AU market)
ASX_COMMODITY_ETFS = [
    "ASX_DLY:GOLD",    # BetaShares Gold Bullion ETF (Currency Hedged) – primary AU gold ETF
    "ASX_DLY:NUGG",    # Perth Mint Physical Gold ETF – AUD-denominated, physically backed
    "ASX_DLY:OOO",     # BetaShares Crude Oil Index ETF – WTI oil exposure in AUD
    "ASX_DLY:ETPMAG",  # ETFS Physical Silver ETF (ASX) – AU silver exposure
]

SYMBOLS_TO_ADD = NSE_COMMODITY_ETFS + BATS_COMMODITY_ETFS + ASX_COMMODITY_ETFS

# ─── Main Update Logic ────────────────────────────────────────────────────────

def update_watchlist_in_object(obj, updated_symbols: list, original_count: int) -> tuple:
    """Recursively find and replace symbolset_data.symbols in the JSON tree."""
    changes = 0
    if isinstance(obj, dict):
        if "symbolset_data" in obj and "symbols" in obj.get("symbolset_data", {}):
            if len(obj["symbolset_data"]["symbols"]) == original_count:
                obj["symbolset_data"]["symbols"] = updated_symbols
                changes += 1
        for v in obj.values():
            changes += update_watchlist_in_object(v, updated_symbols, original_count)
    elif isinstance(obj, list):
        for item in obj:
            changes += update_watchlist_in_object(item, updated_symbols, original_count)
    return changes


def main():
    alerts_path = Path("alerts_raw.json")
    backup_path = Path("alerts_raw.json.bak")

    # Load current watchlist
    with open("watchlist_current.json") as f:
        current_symbols = json.load(f)
    current_set = set(current_symbols)
    original_count = len(current_symbols)

    print(f"Original symbol count: {original_count}")

    # --- Validate removals ---
    actually_removed = []
    not_found_remove = []
    for sym in sorted(SYMBOLS_TO_REMOVE):
        if sym in current_set:
            actually_removed.append(sym)
        else:
            not_found_remove.append(sym)

    # --- Validate additions (skip already-existing) ---
    actually_added = []
    already_exists = []
    for sym in SYMBOLS_TO_ADD:
        if sym not in current_set:
            actually_added.append(sym)
        else:
            already_exists.append(sym)

    # --- Build new symbol list ---
    new_set = current_set - set(actually_removed)
    new_set |= set(actually_added)
    # Preserve original order for existing, append new ones at end
    new_symbols = [s for s in current_symbols if s not in SYMBOLS_TO_REMOVE]
    existing_new = set(new_symbols)
    for sym in actually_added:
        if sym not in existing_new:
            new_symbols.append(sym)

    new_count = len(new_symbols)

    # --- Report ---
    print("\n" + "="*60)
    print("📋 WATCHLIST OPTIMIZATION REPORT")
    print("="*60)
    print(f"\n✅ REMOVED ({len(actually_removed)}):")
    for s in actually_removed:
        if s in BSE_DUPLICATES:
            reason = "BSE ETF duplicate of NSE ETF"
        elif s in BSE_COMPANY_DUPLICATES:
            reason = "BSE stock duplicate of NSE-listed company"
        else:
            reason = "Low quality / troubled"
        print(f"   - {s:<30} [{reason}]")

    if not_found_remove:
        print(f"\n⚠️  Remove targets NOT in watchlist ({len(not_found_remove)}):")
        for s in not_found_remove:
            print(f"   - {s}")

    print(f"\n✅ ADDED ({len(actually_added)}):")
    cats = [
        ("NSE Gold/Silver ETFs", NSE_COMMODITY_ETFS),
        ("US Commodity ETFs (BATS)", BATS_COMMODITY_ETFS),
        ("AU Commodity ETFs (ASX)", ASX_COMMODITY_ETFS),
    ]
    for cat_name, cat_syms in cats:
        cat_added = [s for s in cat_syms if s in set(actually_added)]
        if cat_added:
            print(f"   [{cat_name}]")
            for s in cat_added:
                print(f"     + {s}")

    if already_exists:
        print(f"\n⏭️  Skipped (already in watchlist): {already_exists}")

    print(f"\n{'='*60}")
    print(f"BEFORE : {original_count} symbols")
    print(f"REMOVED: {len(actually_removed)}")
    print(f"ADDED  : {len(actually_added)}")
    print(f"AFTER  : {new_count} symbols")
    print(f"LIMIT  : 1000  |  {'✅ UNDER LIMIT' if new_count < 1000 else '❌ OVER LIMIT'}")
    print(f"{'='*60}\n")

    # --- Save updated watchlist ---
    with open("watchlist_updated.json", "w") as f:
        json.dump(new_symbols, f, indent=2)
    print(f"Saved updated symbol list → watchlist_updated.json")

    # --- Update alerts_raw.json ---
    print(f"\nBacking up alerts_raw.json → alerts_raw.json.bak ...")
    with open(alerts_path) as f:
        alerts_data = json.load(f)

    backup_path.write_text(json.dumps(alerts_data, indent=2))
    print(f"Backup saved.")

    alerts_copy = copy.deepcopy(alerts_data)
    change_count = update_watchlist_in_object(alerts_copy, new_symbols, original_count)
    print(f"Updated {change_count} symbolset_data.symbols block(s) in alerts_raw.json")

    with open(alerts_path, "w") as f:
        json.dump(alerts_copy, f, indent=2)
    print("✅ alerts_raw.json updated successfully.")

    # --- Exchange breakdown ---
    from collections import Counter
    exchange_counts = Counter(s.split(":")[0] for s in new_symbols if ":" in s)
    print("\n📊 New watchlist by exchange:")
    for ex, cnt in sorted(exchange_counts.items()):
        print(f"   {ex:<12} {cnt:>4} symbols")


if __name__ == "__main__":
    main()
