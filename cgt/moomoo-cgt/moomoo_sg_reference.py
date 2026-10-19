"""
moomoo_sg_reference.py — Futu OpenAPI Reference for Moomoo SG Account
======================================================================
Structured catalog of supported markets, instruments, order types,
execution constraints, and capability flags for the FUTUSG security firm
accessed via Futu OpenD (port 11112).

Import this module to get structured capability data, or run directly
for a plain-text capabilities summary.

Sources calibrated against:
  • Futu OpenAPI official docs: https://openapi.moomoo.com/futu-api-doc/en/
  • moomoo-api Python SDK (pip install moomoo-api)
  • Official skills package: openapi.moomoo.com/skills/opend-skills.zip
  • Live account behaviour (FUTUSG account, April 2026)
"""

from __future__ import annotations

# ── Market Coverage ───────────────────────────────────────────────────────────

MARKETS: dict[str, dict] = {
    "SG": {
        "exchange":       "SGX (Singapore Exchange)",
        "currency":       "SGD",
        "instruments":    ["Stocks", "ETFs", "DLCs (Daily Leveraged Certs)", "Warrants", "REITs"],
        "trading_hours":  "09:00–17:20 SGT  (pre-open: 08:30–09:00; auction close: 17:00–17:10)",
        "pre_market":     False,
        "after_hours":    False,
        "short_selling":  "Contra-trading only; persistent short-selling not available for retail",
        "margin":         True,
        "fractional":     False,
        "settlement":     "T+2 (CDP settlement)",
        "min_lot":        "Standard board lot = 100 shares; DLCs/warrants = 1 unit",
        "notes": [
            "DLCs (e.g. SG.NVIW, SG.ZOGW) are leveraged products — 2× / 3× / 7×",
            "DLC air-bag mechanism triggers at ~50% NAV drop — position can be wiped to zero",
            "No MARKET orders on SGX via Futu OpenD (SGX rules require limit/auction only)",
            "No OCO orders on SGX via Futu OpenAPI — manage stop-losses manually",
            "Moomoo SG does NOT support standalone FX spot trading",
        ],
    },
    "US": {
        "exchange":       "NYSE / NASDAQ / AMEX",
        "currency":       "USD",
        "instruments":    ["Stocks", "ETFs", "Leveraged ETFs", "Options (vanilla)"],
        "trading_hours":  "09:30–16:00 ET  (pre: 04:00–09:30; after: 16:00–20:00)",
        "pre_market":     True,
        "after_hours":    True,
        "short_selling":  True,
        "margin":         True,
        "fractional":     True,
        "settlement":     "T+2 (DTCC)",
        "min_lot":        "1 share (fractional = 0.001 share)",
        "options": {
            "supported":    True,
            "styles":       "American only",
            "expiry_types": ["Weekly", "Monthly"],
            "data_source":  "Requires Moomoo US Level 1 or Level 2 quote subscription",
            "execution":    "Options approval + margin account required",
        },
        "notes": [
            "US real-time quote data requires active subscription — without it, prices are blocked",
            "PDT rule applies: accounts < $25K limited to 3 day-trades per 5 trading days",
            "Options: each contract = 100 shares; premium in USD per share",
        ],
    },
    "JP": {
        "exchange":       "TSE (Tokyo Stock Exchange) / Osaka",
        "currency":       "JPY",
        "instruments":    ["Stocks", "ETFs", "Leveraged ETFs (2× Bull / Bear)"],
        "trading_hours":  "09:00–15:30 JST  (lunch break: 11:30–12:30 JST)",
        "pre_market":     False,
        "after_hours":    False,
        "short_selling":  "Via margin account",
        "margin":         True,
        "fractional":     False,
        "settlement":     "T+3 (JASDEC)",
        "min_lot":        "100 shares typical; varies by ticker",
        "leveraged_etfs": {
            "JP.1579": "Nikkei225 2× Bull ETF (NEXT FUNDS)",
            "JP.1572": "HSCEI 2× Bull ETF (NEXT FUNDS)",
            "JP.1570": "Nikkei225 Double Inverse ETF",
            "JP.201A": "Nikkei225 Triple Bull ETF (MAXiS)",
            "JP.2865": "Global X Nasdaq 100 Covered Call ETF",
            "JP.2046": "Nikkei225 Triple Bear ETF",
        },
        "notes": [
            "Leveraged ETF prices can be very large (e.g. JP.1572 ≈ ¥8000+) — 1 unit = ¥8000",
            "Lunch break 11:30–12:30 JST: no order fills during this window",
            "JPY gains require AUD/JPY RBA conversion for Australian CGT",
            "TradingView symbol format: TYO:{ticker} (e.g. TYO:1579)",
        ],
    },
    "HK": {
        "exchange":       "HKEX (Hong Kong Exchanges and Clearing)",
        "currency":       "HKD",
        "instruments":    ["Stocks", "ETFs", "Warrants", "CBBCs", "Futures (index)"],
        "trading_hours":  "09:30–16:00 HKT  (lunch: 12:00–13:00)",
        "pre_market":     False,
        "after_hours":    False,
        "short_selling":  "Via designated margin stocks list",
        "margin":         True,
        "fractional":     False,
        "settlement":     "T+2 (CCASS)",
        "min_lot":        "Varies: 100–2000 shares depending on stock",
        "notes": [
            "Hong Kong stamp duty: 0.13% per side",
            "CBBCs have an MCE (Mandatory Call Event) level — wiped if breached",
        ],
    },
    "CRYPTO": {
        "exchange":       "MoomooX (Moomoo's internal exchange)",
        "currency":       "USDT",
        "instruments":    ["BTC/USDT", "ETH/USDT", "Major altcoins"],
        "trading_hours":  "24/7",
        "pre_market":     False,
        "after_hours":    False,
        "short_selling":  False,
        "margin":         False,
        "fractional":     True,
        "accessible_via": "Moomoo app / web UI only — NOT via Futu OpenD API",
        "notes": [
            "Crypto trading is NOT accessible via Futu OpenD Python API",
            "Requires separate MoomooX account activation in SG region",
            "Australian CGT applies to crypto: each sell/exchange/gift is a CGT event",
        ],
    },
    "FX": {
        "exchange":       "N/A — currency conversion only",
        "instruments":    ["SGD↔USD", "SGD↔JPY", "SGD↔HKD (automated for multi-currency trades)"],
        "trading_hours":  "N/A",
        "short_selling":  False,
        "margin":         False,
        "fractional":     False,
        "accessible_via": "Automatic when trading foreign-currency instruments",
        "notes": [
            "Moomoo SG does NOT support standalone FX spot trading",
            "FX conversion is implicit when buying USD/JPY/HKD instruments from an SGD account",
            "For Australian CGT: each FX conversion is a separate CGT event (ATO ruling)",
            "Use ATO or RBA published daily FX rates for AUD conversions",
        ],
    },
}


# ── Order Types ───────────────────────────────────────────────────────────────

ORDER_TYPES: dict[str, str] = {
    "NORMAL":          "Limit order (standard) — specify price and qty",
    "MARKET":          "Market order — fills at best available price (US/JP/HK only)",
    "ABSOLUTE_LIMIT":  "Reject order if exact price not matched (SGX only)",
    "AUCTION":         "Market order during open/close auction session",
    "AUCTION_LIMIT":   "Limit order for open/close auction session",
    "STOP":            "Stop-loss triggered market order",
    "STOP_LIMIT":      "Stop-loss triggered limit order",
    "TRAILING_STOP":   "Trailing stop (fixed $ or % distance from high-water-mark)",
}

ORDER_TYPES_BY_MARKET: dict[str, list[str]] = {
    "SG":     ["NORMAL", "ABSOLUTE_LIMIT", "AUCTION", "AUCTION_LIMIT"],
    "US":     ["NORMAL", "MARKET", "STOP", "STOP_LIMIT", "TRAILING_STOP"],
    "JP":     ["NORMAL", "MARKET", "AUCTION", "AUCTION_LIMIT"],
    "HK":     ["NORMAL", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP", "STOP_LIMIT"],
    "CRYPTO": [],   # not via OpenD
    "FX":     [],   # not tradeable
}


# ── API Capability Flags ──────────────────────────────────────────────────────
# Values: True = fully supported, False = not supported,
#         "subscription_required" = works but needs paid plan,
#         "margin_required" = needs margin account approval,
#         "limited" = partial support

CAPABILITY_FLAGS: dict[str, dict[str, object]] = {
    #                                   SG      US                          JP      HK      CRYPTO  FX
    "real_time_quotes":         {"SG": True,  "US": "subscription_req",  "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "historical_quotes":        {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "level2_order_book":        {"SG": True,  "US": "subscription_req",  "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "place_orders":             {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "modify_orders":            {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "cancel_orders":            {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "position_query":           {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "account_balance":          {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "historical_deals":         {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "options_chain":            {"SG": False, "US": True,                "JP": False, "HK": False, "CRYPTO": False, "FX": False},
    "short_selling":            {"SG": False, "US": True,                "JP": "margin_req", "HK": "limited", "CRYPTO": False, "FX": False},
    "fractional_shares":        {"SG": False, "US": True,                "JP": False, "HK": False, "CRYPTO": True,  "FX": False},
    "market_screener":          {"SG": True,  "US": "subscription_req",  "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "news_feed":                {"SG": True,  "US": True,                "JP": True,  "HK": True,  "CRYPTO": False, "FX": False},
    "capital_flow_data":        {"SG": True,  "US": True,                "JP": False, "HK": True,  "CRYPTO": False, "FX": False},
    "institutional_flow":       {"SG": False, "US": True,                "JP": False, "HK": True,  "CRYPTO": False, "FX": False},
    "warrant_data":             {"SG": True,  "US": False,               "JP": False, "HK": True,  "CRYPTO": False, "FX": False},
    "futures_trading":          {"SG": False, "US": False,               "JP": False, "HK": "index_futures_only", "CRYPTO": False, "FX": False},
    "pre_market_trading":       {"SG": False, "US": True,                "JP": False, "HK": False, "CRYPTO": True,  "FX": False},
    "after_hours_trading":      {"SG": False, "US": True,                "JP": False, "HK": False, "CRYPTO": True,  "FX": False},
}


# ── Execution Constraints ─────────────────────────────────────────────────────

EXECUTION_CONSTRAINTS: list[dict[str, str]] = [
    # SGX-specific
    {
        "market":     "SG",
        "constraint": "No MARKET orders",
        "detail":     "SGX requires limit or auction orders. Futu OpenD does not allow TrdSide.MARKET on SGX.",
        "impact":     "CRITICAL — must set explicit limit price; slippage risk on fast-moving DLCs",
    },
    {
        "market":     "SG",
        "constraint": "No OCO orders",
        "detail":     "Futu OpenAPI does not support One-Cancels-Other on SGX. Must manage stop-loss separately.",
        "impact":     "HIGH — no automated stop-loss + take-profit on DLCs/warrants",
    },
    {
        "market":     "SG",
        "constraint": "DLC air-bag mechanism",
        "detail":     "Daily Leveraged Certificates trigger air-bag at ~50% NAV drawdown. Position partially reset; effective value near zero.",
        "impact":     "CRITICAL — leveraged ETF positions can be wiped in a single session",
    },
    {
        "market":     "SG",
        "constraint": "T+2 settlement; no same-day re-use of proceeds",
        "detail":     "Proceeds from a sale are available T+2. Cannot immediately redeploy full capital.",
        "impact":     "MODERATE",
    },
    # US-specific
    {
        "market":     "US",
        "constraint": "US quote subscription required for real-time data",
        "detail":     "Without active Level 1/2 subscription: get_market_snapshot() and screener calls return empty or delayed data.",
        "impact":     "CRITICAL for screeners / options analysis",
    },
    {
        "market":     "US",
        "constraint": "Pattern Day Trader (PDT) rule",
        "detail":     "Accounts < $25,000 USD are limited to 3 round-trip day trades per rolling 5-business-day period.",
        "impact":     "HIGH for intraday/swing trading strategies",
    },
    {
        "market":     "US",
        "constraint": "Options require margin account + approval",
        "detail":     "Options trading level must be approved in account settings before using options API endpoints.",
        "impact":     "MODERATE — one-time account setup",
    },
    # JP-specific
    {
        "market":     "JP",
        "constraint": "Lunch break trading gap",
        "detail":     "TSE pauses 11:30–12:30 JST. No fills during this window; GTC orders queue until afternoon session.",
        "impact":     "LOW — affects intraday fill timing only",
    },
    {
        "market":     "JP",
        "constraint": "No CGT currency tracking in API",
        "detail":     "All JP deals returned in JPY. AUD/JPY conversion for Australian CGT must be done manually using RBA rates.",
        "impact":     "MODERATE — post-trade tax calculation requires external FX data",
    },
    # All markets
    {
        "market":     "ALL",
        "constraint": "OpenD: one instance per SecurityFirm",
        "detail":     "AU account (FUTUAU) and SG account (FUTUSG) each require a separate OpenD process on different ports.",
        "impact":     "OPERATIONAL — both must be running for combined reports",
    },
    {
        "market":     "ALL",
        "constraint": "API history: up to ~2 years per query; earliest depends on account creation",
        "detail":     "history_deal_list_query is paginated. Use start_time='2018-01-01' and the API returns from account creation date.",
        "impact":     "LOW — handled transparently by cgt_analysis.py fetch loop",
    },
    {
        "market":     "ALL",
        "constraint": "Live prices unavailable for market-closed sessions",
        "detail":     "When market is closed, position_list_query returns last_price=0. Unrealised P&L falls back to API unrealized_pl field.",
        "impact":     "LOW — CGT report uses fallback correctly; display shows 0.0 for last_price",
    },
]


# ── CGT / Tax Considerations (Australian residents) ───────────────────────────

CGT_NOTES: list[str] = [
    "All gains and losses on Moomoo SG are CGT events for Australian tax residents (ATO TR 2014/8)",
    "Foreign currency trades (SGD, USD, JPY, HKD) must be converted to AUD at the RBA published rate on each trade date",
    "50% CGT discount applies to gains on assets held ≥ 12 months by individuals and eligible trusts",
    "Capital losses can only offset capital gains — they cannot offset ordinary income",
    "FIFO (First In, First Out) is one ATO-accepted cost-basis method; parcel-by-parcel is another",
    "DLC positions: typically short-term (DLCs have effective maturity < 5 years; most held < 12 months)",
    "Options: buy cost = cost base; lapsed options = capital loss equal to premium paid",
    "Crypto gains are a CGT event per ATO (each sale, exchange, or gift triggers CGT)",
    "FX conversion on buying/selling foreign stocks: each conversion may also trigger a forex CGT event",
    "Australian FY runs 1 July – 30 June; tax lodgement deadline is typically 31 October",
    "Tax agent review recommended for: FX conversion events, crypto, expired options, and ACAT transfers",
]


# ── API Context Mapping ───────────────────────────────────────────────────────

API_CONTEXTS: dict[str, str] = {
    "SG trades":   "OpenSecTradeContext(filter_trdmarket=TrdMarket.SG, security_firm=SecurityFirm.FUTUSG, host='127.0.0.1', port=11112)",
    "US trades":   "OpenSecTradeContext(filter_trdmarket=TrdMarket.US, security_firm=SecurityFirm.FUTUSG, host='127.0.0.1', port=11112)",
    "JP trades":   "OpenSecTradeContext(filter_trdmarket=TrdMarket.JP, security_firm=SecurityFirm.FUTUSG, host='127.0.0.1', port=11112)",
    "All trades":  "OpenSecTradeContext(filter_trdmarket=TrdMarket.NONE, security_firm=SecurityFirm.FUTUSG, host='127.0.0.1', port=11112)",
    "Quotes":      "OpenQuoteContext(host='127.0.0.1', port=11112)",
}

# TradingView exchange prefix for each Moomoo market code
TV_PREFIX: dict[str, str] = {
    "AU": "ASX",
    "US": "NASDAQ",   # fallback; NYSE for known NYSE tickers
    "HK": "HKEX",
    "SG": "SGX",
    "JP": "TYO",      # Tokyo Stock Exchange
    "KR": "KRX",
    "CA": "TSX",
    "UK": "LON",
    "CN": "SSE",      # Shanghai; SZSE for Shenzhen
}


# ── Plain-text summary ────────────────────────────────────────────────────────

def capabilities_summary() -> str:
    """Return a compact plain-text summary of Moomoo SG API capabilities."""
    lines = [
        "Moomoo SG Account (FUTUSG) — API Capability Summary",
        "=" * 54,
        "",
        "Markets supported via Futu OpenD (port 11112):",
        "  SGX  ✓  SGD   Stocks / ETFs / DLCs / Warrants / REITs",
        "  US   ✓  USD   Stocks / ETFs / Options  [sub req. for quotes]",
        "  JP   ✓  JPY   Stocks / ETFs / Leveraged ETFs",
        "  HK   ✓  HKD   Stocks / ETFs / Warrants / CBBCs",
        "  Crypto  ✗     MoomooX only — NOT accessible via OpenD",
        "  FX Spot ✗     Currency conversion only (implicit, not tradeable)",
        "",
        "Order types:",
        "  SG   → Limit, Absolute-Limit, Auction (NO market orders)",
        "  US   → Limit, Market, Stop, Stop-Limit, Trailing-Stop",
        "  JP   → Limit, Market, Auction",
        "  HK   → Limit, Market, Stop, Stop-Limit, Auction",
        "",
        "Key constraints:",
        "  [CRITICAL]  US real-time data requires active quote subscription",
        "  [CRITICAL]  No OCO orders on SGX (manual stop-loss management required)",
        "  [CRITICAL]  DLC air-bag: position can be wiped at ~50% NAV drawdown",
        "  [HIGH]      PDT rule applies on US accounts < $25K",
        "  [MODERATE]  Live prices = 0 when market closed (API limitation)",
        "  [LOW]       API history: paginated from account creation date",
        "",
        "Australian CGT (FUTUSG account holders):",
        "  ✓  FIFO cost-basis matching (ATO accepted method)",
        "  ✓  50% discount for gains held ≥ 12 months",
        "  ⚠  Foreign currency (SGD/USD/JPY/HKD) → must convert to AUD at RBA rate",
        "  ⚠  FX conversions themselves may trigger additional CGT events",
        "  ⚠  Consult a registered tax agent before lodging",
    ]
    return "\n".join(lines)


def market_detail(market: str) -> str:
    """Return a formatted detail block for a specific market (e.g. 'JP')."""
    m = MARKETS.get(market.upper())
    if not m:
        return f"Unknown market: {market!r}"
    lines = [f"  Market: {market.upper()}  |  Exchange: {m['exchange']}  |  Currency: {m.get('currency', '—')}",
             f"  Hours:  {m.get('trading_hours', '—')}",
             f"  Supported order types: {', '.join(ORDER_TYPES_BY_MARKET.get(market.upper(), ['—']))}",
             f"  Instruments: {', '.join(m.get('instruments', []))}",
             "  Notes:"]
    lines += [f"    • {n}" for n in m.get("notes", [])]
    return "\n".join(lines)


if __name__ == "__main__":
    print(capabilities_summary())
    print()
    for mkt in ("SG", "US", "JP", "HK"):
        print(market_detail(mkt))
        print()
