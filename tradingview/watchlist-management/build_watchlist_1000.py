#!/usr/bin/env python3
"""
build_watchlist_1000.py
=======================
Uses the TradingView Screener API (Pro session cookie) to:
  1. Screen quality stocks from AU, IN, SG, JP, US, HK (+crypto +forex)
  2. Remove known low-quality / worthless NSE stocks
  3. Fix NEM → ensure NYSE:NEM (primary listing) is present
  4. Add commodity/sector ETFs & missed blue-chips
  5. Pad with screener results to reach exactly 1000 symbols

Output:
  watchlist_1000.json        — final list (1000 symbols)
  watchlist_1000_import.txt  — TradingView import format
  alerts_raw.json            — patched in-place (backup → alerts_raw.json.bak2)
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# ─── TV Screener endpoints ────────────────────────────────────────────────────
SCAN_URLS = {
    "us":   "https://scanner.tradingview.com/america/scan",
    "au":   "https://scanner.tradingview.com/australia/scan",
    "in":   "https://scanner.tradingview.com/india/scan",
    "hk":   "https://scanner.tradingview.com/hongkong/scan",
    "sg":   "https://scanner.tradingview.com/singapore/scan",
    "jp":   "https://scanner.tradingview.com/japan/scan",
    "crypto": "https://scanner.tradingview.com/crypto/scan",
    "forex":  "https://scanner.tradingview.com/forex/scan",
}

# cookie for Pro access
_COOKIE_PATH = Path(__file__).parent / "cookie"
_TV_COOKIE   = _COOKIE_PATH.read_text(encoding="utf-8").strip() if _COOKIE_PATH.exists() else None

# ─── Exchange prefix mapping (scanner → TV symbol prefix) ────────────────────
# TV screener returns symbols like "NYSE:AAPL"; we use them directly.

# ─── REMOVE LIST ──────────────────────────────────────────────────────────────
# Additional low-quality / worthless NSE stocks to remove beyond previous pass

REMOVE_ADDITIONAL = {
    # ---- Anil Ambani / stressed group companies ----
    "NSE:RELINFRA",      # already removed but ensure clean
    "NSE:RPOWER",        # already removed but ensure clean
    "NSE:RNSSOL",        # Reliance Naval – bankrupt, written off

    # ---- Persistent fundamental disasters (losses every year) ----
    "NSE:DHANI",         # already removed
    "NSE:IMAGICAA",      # already removed
    "NSE:PAISALO",       # Paisalo Digital – micro NBFC, very low liquidity
    "NSE:RELIGARE",      # Religare Enterprises – under SEBI/ED investigation, serial losses
    "NSE:EMBDL",         # Embassy Developments Ltd – no real business, penny stock history
    "NSE:POLICYBZR",     # PolicyBazaar (PB Fintech) – heavy losses, no dividend, speculative
    "NSE:PAYTM",         # One97 Communications – systemic RBI action, PAYTM Payments Bank wound down, persistent losses
    "NSE:SPANDANA",      # Spandana Sphoorty – mFin NPA crisis, regulatory headwinds

    # ---- Pure gambling / casino / low-quality entertainment ----
    "NSE:NAZARA",        # Nazara Tech – loss-making gaming, extremely expensive valuation
    "NSE:DREAMFOLKS",    # DreamFolks Services – niche airport lounge, tiny cap, no moat

    # ---- Very illiquid micro-caps with no trading traction ----
    "NSE:JISLJALEQS",    # Jain Irrigation – near-bankruptcy, debt restructuring
    "NSE:GRWRHITECH",    # Greaves Cotton Hi-Tech – tiny cap, illiquid
    "NSE:ORISSAMINE",    # Odisha Manganese – very small, illiquid
    "NSE:TIRUMALCHM",    # Tirumal Chemicals – micro-cap, negligible turnover
    "NSE:CHEMBOND",      # Chembond Chemicals – illiquid, small-cap specialty
    "NSE:POCL",          # Pocl Enterprises – micro-cap, no analyst coverage
    "NSE:PPLPHARMA",     # Praxis Labs (PPL Pharma) – small CDMO, low liquidity
    "NSE:THANGAMAYL",    # Thangamayil Jewellery – regional, illiquid
    "NSE:BOROLTD",       # Borosil Ltd – split entity, low liquidity version
    "NSE:ZUARI",         # Zuari Agro – loss-making, restructuring; ZUARIIND already in list
    "NSE:CLSEL",         # CL Educate – persistent losses, education sector difficulties
    "NSE:TMPV",          # Unconfirmed/delisted ticker
    "NSE:JASH",          # Jash Engineering – very low liquidity, niche
    "NSE:INGIAND",       # Invalid / unknown ticker
    "NSE:EUREKAFORB",    # Eureka Forbes – taken private (Advent), may be delisted
    "NSE:VIYASH",        # Viyash Ports & Logistics – micro-cap, negligible volume
    "NSE:CAPACITE",      # Capacite Infraprojects – construction, promoter pledging issues
    "NSE:SUPRIYA",       # Supriya Lifescience – small pharma, volatile
    "NSE:PROZONER",      # ProZone Intu Props – mall REIT wannabe, persistent losses
    "NSE:GOKEX",         # GO Digit Insurance (GDFL now) mapped to wrong ticker
    "NSE:AGI",           # AGI Infra – micro-cap housing
    "NSE:REFEX",         # Refex Industries – small-cap trading company, speculative
    "NSE:VENTIVE",       # Ventive Hospitality – small hotel REIT, low liquidity
    "NSE:SBCL",          # Small Building Corp – micro
    "NSE:MSTCLTD",       # MSTC Ltd – PSU e-auction company, very low volume/relevance
    "NSE:TARC",          # TARC Ltd – real estate development, micro-cap Delhi NCR
    "NSE:PREMEXPLN",     # Premier Explosives – defence micro-cap, illiquid
    "NSE:GAI",           # Garuda Aerospace – not a valid NSE ticker (unlisted)
}

# ─── MUST-HAVE additions ─────────────────────────────────────────────────────
# Important stocks/ETFs missed in previous round

MUST_ADDS = [
    # NEM primary listing fix
    "NYSE:NEM",           # Newmont Corp – world's largest gold miner (primary NYSE listing)

    # US missed blue-chips
    "NYSE:BRK.A",         # Berkshire Hathaway A (BATS:BRK.B already in list, keep ref)
    "NYSE:T",             # AT&T – large-cap telecom, high dividend yield
    "NYSE:VZ",            # Verizon – US telecom giant, 6%+ yield
    "NYSE:PFE",           # Pfizer – pharma blue-chip, post-COVID recovery
    "NYSE:ABBV",          # AbbVie – biopharma, high dividend (BATS:ABBV may already exist)
    "NYSE:BP",            # BP plc ADR – oil major

    # US sector ETFs (missed previously)
    "BATS:XLF",           # Financial Select Sector SPDR
    "BATS:XLK",           # Technology Select Sector SPDR
    "BATS:XLV",           # Health Care Select Sector SPDR
    "BATS:XLY",           # Consumer Discretionary Select Sector SPDR
    "BATS:QQQ",           # Invesco Nasdaq-100 ETF
    "BATS:SPY",           # SPDR S&P 500 ETF – benchmark
    "BATS:IWM",           # iShares Russell 2000 ETF – small-cap US

    # NSE missed quality stocks
    "NSE:ZOMATO",         # Zomato – now profitable, dominant food delivery
    "NSE:NYKAA",          # FSN E-Commerce (Nykaa) – beauty/fashion e-com leader
    "NSE:DMART",          # Avenue Supermarts (D-Mart) – India's best retailer
    "NSE:ASTERDM",        # Aster DM Healthcare – already in list; skip if duplicate
    "NSE:LICI",           # LIC of India – India's largest insurer
    "NSE:NHPC",           # NHPC – already in list; ensure included
    "NSE:MOTHERSON",      # already in list
    "NSE:TATATECH",       # Tata Technologies – Tata group IT, recent IPO
    "NSE:MAPMYINDIA",     # CE Info Systems (MapMyIndia) – maps/geospatial monopoly
    "NSE:NUVAMA",         # Nuvama Wealth Management – premium wealth platform
    "NSE:BIKAJI",         # Bikaji Foods – leading FMCG snacks brand
    "NSE:SENCO",          # already in list
    "NSE:BSOFT",          # already in list
    "NSE:RADICO",         # already in list

    # HK missed blue-chips
    "HKEX:700",           # Tencent Holdings – HK's most important internet stock
    "HKEX:9988",          # Alibaba Group – major e-commerce/cloud
    "HKEX:1299",          # AIA Group – insurance giant
    "HKEX:941",           # China Mobile – telecom giant
    "HKEX:5",             # HSBC Holdings – global banking
    "HKEX:2318",          # Ping An Insurance – China's biggest insurer
    "HKEX:1928",          # Sands China – gaming/casino
    "HKEX:2688",          # ENN Energy – natural gas distribution

    # HK ETFs (major liquid ETFs)
    "HKEX_DLY:2800",      # Tracker Fund of HK (equivalent of Nifty ETF for HangSeng)
    "HKEX_DLY:2823",      # iShares MSCI A50 China ETF
    "HKEX_DLY:2840",      # SPDR Gold Trust (HKD) – gold in HK market

    # ASX missed quality
    "ASX_DLY:CBA",        # Commonwealth Bank – Australia's largest bank (already present?)
    "ASX_DLY:ANZ",        # ANZ Bank (already in list)
    "ASX_DLY:NAB",        # National Aus Bank (already in list)
    "ASX_DLY:WBC",        # Westpac (already in list)
    "ASX_DLY:ETHI",       # BetaShares Global Sustainability Leaders ETF
    "ASX_DLY:NDQ",        # BetaShares NASDAQ 100 ETF – most popular ASX tech ETF
    "ASX_DLY:IOZ",        # iShares Core S&P/ASX 200 ETF
    "ASX_DLY:VAS",        # Vanguard Australian Shares ETF

    # SGX missed quality
    "SGX_DLY:D05",        # DBS Group Holdings – Singapore's largest bank
    "SGX_DLY:O39",        # OCBC Bank – major SG bank
    "SGX_DLY:U11",        # UOB – third major SG bank
    "SGX_DLY:Z74",        # Singapore Telecommunications (Singtel)
    "SGX_DLY:A17U",       # CapitaLand Integrated Commercial Trust REIT
    "SGX_DLY:C38U",       # CapitaLand Ascendas REIT

    # JP missed quality
    "TSE:7203",           # Toyota Motor Corp
    "TSE:9984",           # SoftBank Group
    "TSE:6758",           # Sony Group
    "TSE:8306",           # Mitsubishi UFJ Financial Group
    "TSE:6861",           # Keyence Corp
    "TSE:9432",           # NTT Corp
    "TSE:8035",           # Tokyo Electron – semiconductor equipment

    # Crypto (Binance, top liquid pairs)
    "BINANCE:BTCUSDT",    # Bitcoin / USDT
    "BINANCE:ETHUSDT",    # Ethereum / USDT
    "BINANCE:BNBUSDT",    # BNB / USDT
    "BINANCE:SOLUSDT",    # Solana / USDT
    "BINANCE:XRPUSDT",    # XRP / USDT

    # Forex (FX canonical)
    "FX:EURUSD",          # EUR/USD
    "FX:GBPUSD",          # GBP/USD
    "FX:USDJPY",          # USD/JPY
    "FX:AUDUSD",          # AUD/USD
    "FX:USDSGD",          # USD/SGD
    "FX:USDINR",          # USD/INR
    "FX:XAUUSD",          # Gold spot
    "FX:XAGUSD",          # Silver spot

    # Commodity Futures (continuous)
    "NYMEX:CL1!",         # WTI Crude Oil futures
    "COMEX:GC1!",         # Gold futures
    "COMEX:SI1!",         # Silver futures
    "NYMEX:NG1!",         # Natural Gas futures
    "CME:HG1!",           # Copper futures
]


# ─── Additional NSE ETFs missing ─────────────────────────────────────────────
NSE_MISSED_ETFS = [
    "NSE:GOLDBEES",       # already added
    "NSE:NIFTYBEES",      # already in NSE
    "NSE:MID150BEES",     # already in NSE
    "NSE:ITBEES",         # already in NSE
    "NSE:AUTOBEES",       # already in NSE
    "NSE:PHARMABEES",     # already in NSE
    "NSE:DIVOPPBEES",     # already in NSE
    "NSE:NV20BEES",       # already in NSE
    "NSE:CONSUMBEES",     # already in NSE
    "NSE:CPSE",           # CPSE ETF – PSU basket
    "NSE:LOWVOLIETF",     # Mirae Asset Nifty 50 Low Volatility ETF
    "NSE:MONQ50",         # Motilal NASDAQ 100 FOF (if listed on NSE)
    "NSE:MON100",         # Motilal Oswal NASDAQ 100 ETF
    "NSE:JUNIORBEES",     # NSE:JUNIORBEES already there
    "NSE:MAFANG",         # Mirae Asset FANG+ ETF – Meta, Apple, Netflix, Google exposure
]


# ─── TV Screener Fetch ────────────────────────────────────────────────────────
SCAN_COLS = ["name", "description", "close", "volume", "market_cap_basic",
             "RSI", "ADX", "SMA200", "change", "recommendation_mark",
             "return_on_equity", "net_margin", "price_earnings_ttm"]

def tv_scan(market: str, min_mcap_usd: float = 500e6, max_results: int = 200,
            min_vol: float = 100_000) -> list:
    """
    Fetch quality stocks from a TV scanner endpoint.
    
    Quality filters:
      - Market cap > min_mcap_usd (in USD equiv)
      - Volume > min_vol
      - Sorted by market_cap_basic desc (largest/most liquid first)
    Returns list of "EXCHANGE:TICKER" strings.
    """
    url = SCAN_URLS.get(market)
    if not url:
        return []

    filters = []

    # Market-specific size filter (market_cap_basic is always in native currency millions on TV screener)
    if market in ("us", "au", "hk", "sg", "jp", "in"):
        filters.append({
            "left": "market_cap_basic",
            "operation": "greater",
            "right": min_mcap_usd
        })
        filters.append({
            "left": "volume",
            "operation": "greater",
            "right": min_vol
        })
        # Exclude ETFs/funds for stock markets (type=stock)
        # Note: TV screener uses 'type' field — 'stock' excludes ETFs from stock scan
        if market not in ("crypto", "forex"):
            filters.append({
                "left": "type",
                "operation": "equal",
                "right": "stock"
            })

    payload = {
        "columns": SCAN_COLS,
        "filter": filters,
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, max_results],
    }
    if market == "crypto":
        payload["filter"] = [
            {"left": "exchange", "operation": "equal", "right": "BINANCE"},
            {"left": "volume", "operation": "greater", "right": 1_000_000},
        ]
        payload["sort"] = {"sortBy": "volume", "sortOrder": "desc"}

    if market == "forex":
        payload["filter"] = [
            {"left": "exchange", "operation": "equal", "right": "FX"},
        ]
        payload["sort"] = {"sortBy": "ADX", "sortOrder": "desc"}
        payload["range"] = [0, 50]

    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        symbols = []
        for item in data.get("data", []):
            sym = item.get("s", "")
            if sym:
                symbols.append(sym)
        return symbols
    except Exception as e:
        print(f"    [WARN] {market} scan error: {e}")
        return []


# ─── TradingView Watchlist API ────────────────────────────────────────────────

def tv_get_watchlist_id() -> str | None:
    """Try to get the ALL_NEW watchlist ID from TradingView API."""
    if not _TV_COOKIE:
        return None
    try:
        req = urllib.request.Request(
            "https://pine-facade.tradingview.com/pine-facade/list?filter=study&category=script",
            headers={"Cookie": _TV_COOKIE, "User-Agent": "Mozilla/5.0"}
        )
        # Actually use the watchlists endpoint
        req = urllib.request.Request(
            "https://watchlistify.tradingview.com/v1/user/watchlists",
            headers={
                "Cookie": _TV_COOKIE,
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://www.tradingview.com",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        print(f"  Watchlist API response: {json.dumps(data)[:200]}")
    except Exception as e:
        print(f"  Watchlist API error: {e}")
    return None


def tv_push_watchlist(watchlist_id: str, symbols: list) -> bool:
    """Push updated symbols to TradingView watchlist via API."""
    if not _TV_COOKIE:
        return False
    payload = json.dumps({"symbols": symbols}).encode()
    req = urllib.request.Request(
        f"https://watchlistify.tradingview.com/v1/user/watchlists/{watchlist_id}/symbols",
        data=payload,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Cookie": _TV_COOKIE,
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.tradingview.com",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        print(f"  Watchlist push result: {result}")
        return True
    except Exception as e:
        print(f"  Watchlist push error: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def update_watchlist_in_json(obj, new_symbols: list, original_count: int) -> int:
    """Recursively update symbolset_data.symbols matching original_count."""
    changes = 0
    if isinstance(obj, dict):
        if "symbolset_data" in obj and "symbols" in obj.get("symbolset_data", {}):
            if len(obj["symbolset_data"]["symbols"]) == original_count:
                obj["symbolset_data"]["symbols"] = new_symbols
                changes += 1
        for v in obj.values():
            changes += update_watchlist_in_json(v, new_symbols, original_count)
    elif isinstance(obj, list):
        for item in obj:
            changes += update_watchlist_in_json(item, new_symbols, original_count)
    return changes


def main():
    TARGET = 1000

    # Load current watchlist (from previous update round)
    src = Path("watchlist_updated.json")
    if not src.exists():
        src = Path("watchlist_current.json")
    with open(src) as f:
        current = json.load(f)
    current_set = set(current)
    print(f"Starting from: {len(current)} symbols (source: {src.name})")

    # ── Step 1: Remove additional low-quality ─────────────────────────────────
    print("\n[1] Removing low-quality symbols ...")
    removed = []
    for sym in list(REMOVE_ADDITIONAL):
        if sym in current_set:
            current_set.discard(sym)
            removed.append(sym)
    print(f"    Removed {len(removed)}: {removed}")

    # ── Step 2: Fix NEM – keep BATS:NEM, add NYSE:NEM ────────────────────────
    print("\n[2] Fixing NEM ...")
    if "BATS:NEM" in current_set:
        print("    BATS:NEM ✓ (kept – BATS feed works fine)")
    if "NYSE:NEM" not in current_set:
        current_set.add("NYSE:NEM")
        print("    NYSE:NEM ✓ added (primary NYSE listing)")
    if "ASX_DLY:NEM" in current_set:
        print("    ASX_DLY:NEM ✓ (kept – ASX cross-listing legitimate)")

    # ── Step 3: Add must-have symbols ────────────────────────────────────────
    print("\n[3] Adding must-have symbols ...")
    added_must = []
    for sym in MUST_ADDS:
        if sym not in current_set:
            current_set.add(sym)
            added_must.append(sym)
    print(f"    Added {len(added_must)} must-haves")

    # ── Step 4: Add NSE missed ETFs ──────────────────────────────────────────
    print("\n[4] Adding NSE supplementary ETFs ...")
    added_etf = []
    for sym in NSE_MISSED_ETFS:
        if sym not in current_set:
            current_set.add(sym)
            added_etf.append(sym)
    print(f"    Added {len(added_etf)} NSE ETFs")

    current_count = len(current_set)
    print(f"\n    Running count: {current_count}")

    # ── Step 5: Screen each market to fill gap to 1000 ───────────────────────
    gap = TARGET - current_count
    print(f"\n[5] Need {gap} more symbols from TV screener ...")

    # Market configs: (market_key, min_mcap, max_fetch, label)
    screen_plan = [
        # Large top-up from each market, proportional to gap
        ("us",  2_000_000_000,  150, "🇺🇸 US (mcap>$2B)"),
        ("in",    50_000_000,  120, "🇮🇳 India (mcap>₹4000Cr equiv)"),
        ("au",   500_000_000,   80, "🇦🇺 AU (mcap>A$500M)"),
        ("hk",   500_000_000,   80, "🇭🇰 HK (mcap>HKD4B)"),
        ("sg",   200_000_000,   60, "🇸🇬 SG (mcap>SGD200M)"),
        ("jp", 1_000_000_000,   80, "🇯🇵 JP (mcap>¥100B)"),
        ("crypto", 0,            30, "₿ Crypto (top Binance)"),
        ("forex",  0,            25, "💱 Forex (FX)"),
    ]

    screened_candidates = []
    for market, min_mcap, max_fetch, label in screen_plan:
        if gap <= 0:
            break
        print(f"    Scanning {label} ...", end=" ", flush=True)
        syms = tv_scan(market, min_mcap_usd=min_mcap, max_results=max_fetch)
        new_syms = [s for s in syms if s not in current_set]
        print(f"{len(syms)} fetched, {len(new_syms)} new")
        screened_candidates.extend(new_syms)
        time.sleep(0.3)  # polite pause

    # Deduplicate screened candidates while preserving order
    seen = set()
    unique_candidates = []
    for s in screened_candidates:
        if s not in seen and s not in current_set:
            seen.add(s)
            unique_candidates.append(s)

    # Fill to TARGET
    to_add = unique_candidates[:gap]
    for sym in to_add:
        current_set.add(sym)
    print(f"\n    Screener added: {len(to_add)} symbols")

    # ── Step 6: Build ordered final list ─────────────────────────────────────
    # Preserve original order for existing symbols, append new ones at end
    with open(src) as f:
        original_order = json.load(f)

    # Remove the low-quality ones first  
    ordered = [s for s in original_order if s not in REMOVE_ADDITIONAL]
    existing_set_in_order = set(ordered)

    # Append new symbols in order — MUST_ADDS first (highest priority)
    priority_adds = []
    for sym in (added_must + added_etf + to_add):
        if sym in current_set and sym not in existing_set_in_order:
            priority_adds.append(sym)
            existing_set_in_order.add(sym)

    # Ensure NYSE:NEM is in priority list (it was added to current_set in step 2,
    # not via added_must, so it needs explicit inclusion here)
    if "NYSE:NEM" in current_set and "NYSE:NEM" not in existing_set_in_order:
        priority_adds.insert(0, "NYSE:NEM")
        existing_set_in_order.add("NYSE:NEM")

    ordered.extend(priority_adds)

    # Trim/expand to exactly TARGET
    if len(ordered) > TARGET:
        print(f"    Trimming from {len(ordered)} to {TARGET}")
        ordered = ordered[:TARGET]
    
    final_count = len(ordered)
    print(f"\n{'='*65}")
    print(f"FINAL COUNT: {final_count} / {TARGET}")
    
    # ── Step 7: Save files ───────────────────────────────────────────────────
    with open("watchlist_1000.json", "w") as f:
        json.dump(ordered, f, indent=2)
    print(f"✅ watchlist_1000.json saved")

    with open("watchlist_1000_import.txt", "w", encoding="utf-8") as f:
        for s in ordered:
            f.write(s + "\n")
    print(f"✅ watchlist_1000_import.txt saved")

    # ── Step 8: Patch alerts_raw.json ────────────────────────────────────────
    alerts_path = Path("alerts_raw.json")
    bak_path    = Path("alerts_raw.json.bak2")
    with open(alerts_path) as f:
        alerts_data = json.load(f)

    import copy
    alerts_copy  = copy.deepcopy(alerts_data)
    # Original count is what's currently in alerts_raw (the previously updated one)
    old_syms = []
    def find_sym_block(obj):
        if isinstance(obj, dict):
            if "symbolset_data" in obj and "symbols" in obj.get("symbolset_data", {}):
                old_syms.extend(obj["symbolset_data"]["symbols"])
            for v in obj.values():
                find_sym_block(v)
        elif isinstance(obj, list):
            for i in obj: find_sym_block(i)
    find_sym_block(alerts_data)
    current_alerts_count = len(set(old_syms)) if old_syms else 970

    bak_path.write_text(json.dumps(alerts_data, indent=2))
    print(f"✅ Backup → alerts_raw.json.bak2")

    # Use the count from the first block found
    first_block_count = None
    def get_first_block_count(obj):
        nonlocal first_block_count
        if first_block_count is not None: return
        if isinstance(obj, dict):
            if "symbolset_data" in obj and "symbols" in obj.get("symbolset_data", {}):
                first_block_count = len(obj["symbolset_data"]["symbols"])
                return
            for v in obj.values():
                get_first_block_count(v)
        elif isinstance(obj, list):
            for i in obj: get_first_block_count(i)
    get_first_block_count(alerts_data)

    n_changed = update_watchlist_in_json(alerts_copy, ordered, first_block_count or 970)
    print(f"✅ Updated {n_changed} symbolset_data block(s) in alerts_raw.json")

    with open(alerts_path, "w") as f:
        json.dump(alerts_copy, f, indent=2)
    print(f"✅ alerts_raw.json updated")

    # ── Summary ──────────────────────────────────────────────────────────────
    from collections import Counter
    exch_counts = Counter(s.split(":")[0] for s in ordered if ":" in s)
    print(f"\n📊 Final watchlist by exchange:")
    for ex, cnt in sorted(exch_counts.items()):
        print(f"   {ex:<15} {cnt:>4} symbols")

    print(f"\n📋 REMOVED ({len(removed)} low-quality):")
    for s in removed:
        print(f"   ✂  {s}")

    print(f"\n➕ ADDED MUST-HAVES ({len(added_must)}):")
    for s in added_must:
        print(f"   +  {s}")

    if added_etf:
        print(f"\n➕ ADDED NSE ETFs ({len(added_etf)}):")
        for s in added_etf:
            print(f"   +  {s}")

    if to_add:
        print(f"\n➕ ADDED FROM SCREENER ({len(to_add)}) — first 20:")
        for s in to_add[:20]:
            print(f"   +  {s}")
        if len(to_add) > 20:
            print(f"   ... and {len(to_add)-20} more")

    print(f"\n✅ Done. Final: {final_count} symbols")
    
    if final_count < TARGET:
        print(f"\n⚠️  Still {TARGET - final_count} short of {TARGET} target.")
        print("    TV screener may have returned fewer results than expected.")
        print("    Re-run with --no-mcap-filter or check connectivity.")


if __name__ == "__main__":
    main()
