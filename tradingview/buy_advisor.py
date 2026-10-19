"""
Buy Vehicle Advisor — Layer 8 enrichment for Signal Stack
==========================================================
Answers three questions for every signal card:

1. **Which account should I use?**
   Exchange → account routing with live cash balances (Moomoo AU/SG via OpenD).

2. **What vehicle should I trade?**
   - Stock (base)
   - Leveraged ETF (2x / 3x US or SGX DLC) for high-conviction signals
   - NSE F&O (futures / options) for Indian stocks

3. **What FX impact should I be aware of?**
   - AUD base: USD exposure (IBKR/Moomoo US), INR exposure
   - SGD base: USD exposure, AUD exposure
   - FX trend warning when major pair is stretched vs 50-day MA

Usage:
    from buy_advisor import build_buy_advice, _buy_advisor_html, fetch_all_account_cash, build_account_cash_html

    cash   = fetch_all_account_cash()         # call once per run
    advice = build_buy_advice(item, cash)      # per signal
    html   = _buy_advisor_html(advice)         # embed in card
    panel  = build_account_cash_html(cash)     # global cash band

Requirements (all optional — graceful degradation):
    moomoo-api   — for Moomoo AU/SG cash balances (OpenD must be running)
    yfinance     — for forex snapshot
"""

from __future__ import annotations

import os
import sys
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

# ─── Path setup so we can import sibling modules ─────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

# ── Optional: moomoo (for live cash) ─────────────────────────────────────────
try:
    import os as _os
    _os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    from moomoo import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK  # type: ignore
    from moomoo_config import (  # type: ignore
        OPEND_HOST, OPEND_AU_PORT, OPEND_SG_PORT,
        AU_FIRM, SG_FIRM, AU_ACCOUNT_ID, SG_ACCOUNT_ID,
        is_opend_running,
    )
    _MOOMOO_OK = True
except ImportError:
    _MOOMOO_OK = False

# ── India broker credential defaults (mirrors india_portfolio_dashboard.py) ───
os.environ.setdefault("ZERODHA_API_KEY",      "")
os.environ.setdefault("ANGELONE_API_KEY",     "")
os.environ.setdefault("ANGELONE_CLIENT_CODE", "")
os.environ.setdefault("ANGELONE_PIN",         "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

# ── Optional: yfinance (for forex snapshot) ───────────────────────────────────
try:
    import yfinance as yf  # type: ignore
    _YF_OK = True
except ImportError:
    _YF_OK = False

# ── Optional: NSE F&O list ────────────────────────────────────────────────────
try:
    from options.nse_fo_list import NSE_FO_LOT_SIZES, is_fo_eligible  # type: ignore
    _FO_OK = True
except ImportError:
    NSE_FO_LOT_SIZES: dict[str, int] = {}
    def is_fo_eligible(sym: str) -> bool:
        return False
    _FO_OK = False


# ─── Exchange → Account Routing ──────────────────────────────────────────────
# Lists which accounts CAN trade each exchange prefix.
# First entry is the recommended/preferred account.
_EXCHANGE_ACCOUNTS: dict[str, list[str]] = {
    "NYSE":    ["IBKR (USD)", "IBKR (AUD)", "Moomoo AU (AUD)", "Moomoo SG (SGD)"],
    "NASDAQ":  ["IBKR (USD)", "IBKR (AUD)", "Moomoo AU (AUD)", "Moomoo SG (SGD)"],
    "AMEX":    ["IBKR (USD)", "IBKR (AUD)", "Moomoo AU (AUD)", "Moomoo SG (SGD)"],
    "BATS":    ["IBKR (USD)", "IBKR (AUD)", "Moomoo AU (AUD)", "Moomoo SG (SGD)"],
    "CBOE":    ["IBKR (USD)", "IBKR (AUD)", "Moomoo AU (AUD)"],
    "HK":      ["Moomoo SG (SGD)", "Moomoo AU (AUD)"],
    "HKEX":    ["Moomoo SG (SGD)", "Moomoo AU (AUD)"],
    "SGX":     ["Moomoo SG (SGD)"],
    "ASX":     ["Moomoo AU (AUD)"],
    "ASX_DLY": ["Moomoo AU (AUD)"],
    "NSE":     ["Zerodha (INR)", "Angel One (INR)"],
    "BSE":     ["Zerodha (INR)", "Angel One (INR)"],
    "NSE_DLY": ["Zerodha (INR)", "Angel One (INR)"],
    "BSE_DLY": ["Zerodha (INR)", "Angel One (INR)"],
    # Japan — Moomoo SG supports Japanese equities
    "TYO":    ["Moomoo SG (SGD)"],
    "TSE":    ["Moomoo SG (SGD)"],
    # Korea — Moomoo SG supports Korean equities
    "KRX":    ["Moomoo SG (SGD)"],
    "KOSDAQ": ["Moomoo SG (SGD)"],
}

# Accounts that hold net AUD base (FX exposure for non-AUD trades)
_AUD_ACCOUNTS = {"Moomoo AU (AUD)", "IBKR (AUD)"}
# Accounts that hold net USD base
_USD_ACCOUNTS = {"IBKR (USD)"}
# Accounts that hold net SGD base
_SGD_ACCOUNTS = {"Moomoo SG (SGD)"}


# ─── Leveraged Instrument Catalogue ──────────────────────────────────────────
# Maps 'base symbol' (TV ticker base, uppercase) to available leveraged products.
# Each key: {lev, desc, market, decay, note}
# decay: "daily_reset" = standard leveraged ETF (path dependency, hold ≤ weeks)
#        "extreme"     = SGX DLC (up to 7x, daily reset AND barrier risk)
#        "none"        = futures/margin (no daily decay but margin calls)
LEVERAGED_UNIVERSE: dict[str, list[dict]] = {
    # ── US Broad Market ────────────────────────────────────────────────────────
    "SPY":  [{"sym": "SSO",   "lev": 2.0, "desc": "2x S&P 500",     "market": "US", "decay": "daily_reset"},
             {"sym": "UPRO",  "lev": 3.0, "desc": "3x S&P 500",     "market": "US", "decay": "daily_reset"},
             {"sym": "SPDBU", "lev": 3.0, "desc": "3x S&P 500 Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "QQQ":  [{"sym": "QLD",   "lev": 2.0, "desc": "2x QQQ",         "market": "US", "decay": "daily_reset"},
             {"sym": "TQQQ",  "lev": 3.0, "desc": "3x QQQ",         "market": "US", "decay": "daily_reset"},
             {"sym": "NQDBU", "lev": 3.0, "desc": "3x NASDAQ-100 Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "IWM":  [{"sym": "TNA",   "lev": 3.0, "desc": "3x Russell 2000","market": "US", "decay": "daily_reset"}],
    "DIA":  [{"sym": "UDOW",  "lev": 3.0, "desc": "3x DJIA",        "market": "US", "decay": "daily_reset"}],
    # ── HK / China Indices — SGX DLCs ──────────────────────────────────────────
    # DLC tickers from SocGen (SGX-listed). Tickers reset after barrier events —
    # verify current codes on Moomoo SG or sgx.com before trading.
    "HSI":  [{"sym": "HSIDB3","lev": 3.0, "desc": "3x HSI Bull DLC","market": "SG", "decay": "extreme",
              "note": "SGX DLC (SocGen) — Hang Seng, barrier risk, Moomoo SG only"},
             {"sym": "HSIDC3","lev": 3.0, "desc": "3x HSI Bear DLC","market": "SG", "decay": "extreme",
              "note": "SGX DLC (SocGen) — inverse/short HSI, barrier risk, Moomoo SG only"}],
    "NKY":  [{"sym": "NKYDB3","lev": 3.0, "desc": "3x Nikkei Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) — Nikkei 225, barrier risk, Moomoo SG only"}],
    "NI225":[{"sym": "NKYDB3","lev": 3.0, "desc": "3x Nikkei Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) — Nikkei 225, barrier risk, Moomoo SG only"}],
    # ── HK-listed Chinese Tech — available as SGX DLCs (SocGen) ───────────────
    # TV symbols for HK stocks: e.g. HKEX:700, HKEX:9988
    "700":  [{"sym": "700DBU","lev": 3.0, "desc": "3x Tencent Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) on Tencent 0700.HK, Moomoo SG only"}],
    "9988": [{"sym": "9988DBU","lev": 3.0,"desc": "3x BABA(HK) Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) on Alibaba 9988.HK, Moomoo SG only"}],
    "BIDU": [{"sym": "BDIDBU","lev": 3.0, "desc": "3x Baidu Bull DLC","market": "SG","decay": "extreme",
              "note": "SGX DLC (SocGen) on Baidu, Moomoo SG only"}],
    # ── US Sector ETFs ─────────────────────────────────────────────────────────
    "XLK":  [{"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",        "market": "US", "decay": "daily_reset"}],
    "XLV":  [{"sym": "RXL",   "lev": 2.0, "desc": "2x Healthcare",  "market": "US", "decay": "daily_reset"},
             {"sym": "CURE",  "lev": 3.0, "desc": "3x Healthcare",  "market": "US", "decay": "daily_reset"}],
    "XLF":  [{"sym": "FAS",   "lev": 3.0, "desc": "3x Financials",  "market": "US", "decay": "daily_reset"}],
    "XLE":  [{"sym": "ERX",   "lev": 2.0, "desc": "2x Energy",      "market": "US", "decay": "daily_reset"}],
    "XLB":  [{"sym": "URTY",  "lev": 3.0, "desc": "3x Materials",   "market": "US", "decay": "daily_reset"}],
    "GLD":  [{"sym": "UGL",   "lev": 2.0, "desc": "2x Gold",        "market": "US", "decay": "daily_reset"}],
    "USO":  [{"sym": "UCO",   "lev": 2.0, "desc": "2x Crude Oil",   "market": "US", "decay": "daily_reset"}],
    "SMH":  [{"sym": "SOXL",  "lev": 3.0, "desc": "3x Semiconductors","market": "US","decay": "daily_reset"}],
    # ── US Individual Stocks w/ single-stock leveraged ETFs ───────────────────
    "NVDA":  [{"sym": "NVDU",  "lev": 1.5, "desc": "1.5x NVDA",     "market": "US", "decay": "daily_reset"},
              {"sym": "NVDL",  "lev": 2.0, "desc": "2x NVDA",       "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis (NVDA-heavy)", "market": "US", "decay": "daily_reset"},
              {"sym": "NVDDU", "lev": 3.0, "desc": "3x NVDA Bull DLC","market": "SG", "decay": "extreme",
               "note": "SGX DLC — barrier risk, extreme decay, Moomoo SG only"}],
    "AMD":   [{"sym": "AMDL",  "lev": 2.0, "desc": "2x AMD",        "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"},
              {"sym": "AMDDU", "lev": 3.0, "desc": "3x AMD Bull DLC","market": "SG", "decay": "extreme",
               "note": "SGX DLC — barrier risk, extreme decay, Moomoo SG only"}],
    "TSLA":  [{"sym": "TSLL",  "lev": 2.0, "desc": "2x Tesla",      "market": "US", "decay": "daily_reset"},
              {"sym": "TSLDBU","lev": 3.0, "desc": "3x TSLA Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC — barrier risk, extreme decay, Moomoo SG only"}],
    "AAPL":  [{"sym": "AAPU",  "lev": 1.5, "desc": "1.5x AAPL",    "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "AAPDU", "lev": 3.0, "desc": "3x AAPL Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC — barrier risk, extreme decay, Moomoo SG only"}],
    "META":  [{"sym": "METU",  "lev": 2.0, "desc": "2x META",       "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "METDU", "lev": 3.0, "desc": "3x META Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC — barrier risk, extreme decay, Moomoo SG only"}],
    "AMZN":  [{"sym": "AMZU",  "lev": 1.5, "desc": "1.5x Amazon",   "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "FNGU",  "lev": 3.0, "desc": "3x FANG+",      "market": "US", "decay": "daily_reset"},
              {"sym": "AMZDU", "lev": 3.0, "desc": "3x AMZN Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "GOOGL": [{"sym": "GGLL",  "lev": 2.0, "desc": "2x Alphabet",   "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "FNGU",  "lev": 3.0, "desc": "3x FANG+",      "market": "US", "decay": "daily_reset"},
              {"sym": "GGLDU", "lev": 3.0, "desc": "3x GOOGL Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "GOOG":  [{"sym": "GGLL",  "lev": 2.0, "desc": "2x Alphabet",   "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "FNGU",  "lev": 3.0, "desc": "3x FANG+",      "market": "US", "decay": "daily_reset"},
              {"sym": "GGLDU", "lev": 3.0, "desc": "3x GOOGL Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "MSFT":  [{"sym": "MSFU",  "lev": 2.0, "desc": "2x Microsoft",  "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"},
              {"sym": "FNGU",  "lev": 3.0, "desc": "3x FANG+",      "market": "US", "decay": "daily_reset"},
              {"sym": "MSFDU", "lev": 3.0, "desc": "3x MSFT Bull DLC","market": "SG","decay": "extreme",
               "note": "SGX DLC (SocGen) — barrier risk, extreme decay, Moomoo SG only"}],
    "NFLX":  [{"sym": "NFXL",  "lev": 2.0, "desc": "2x Netflix",    "market": "US", "decay": "daily_reset"},
              {"sym": "FNGU",  "lev": 3.0, "desc": "3x FANG+",      "market": "US", "decay": "daily_reset"}],
    "SMCI":  [{"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "MU":    [{"sym": "MUU",   "lev": 2.0, "desc": "2x Micron",     "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "INTC":  [{"sym": "LINT",  "lev": 2.0, "desc": "2x Intel",      "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "AVGO":  [{"sym": "AVL",   "lev": 2.0, "desc": "2x Broadcom",   "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "QCOM":  [{"sym": "QCMU",  "lev": 2.0, "desc": "2x Qualcomm",   "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "ARM":   [{"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "AMAT":  [{"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "LRCX":  [{"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "MRVL":  [{"sym": "MRVU",  "lev": 2.0, "desc": "2x Marvell",    "market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "TSM":   [{"sym": "TSMX",  "lev": 2.0, "desc": "2x Taiwan Semi","market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "TXN":   [{"sym": "TXNU",  "lev": 2.0, "desc": "2x Texas Instruments","market": "US", "decay": "daily_reset"},
              {"sym": "SOXL",  "lev": 3.0, "desc": "3x Semis",      "market": "US", "decay": "daily_reset"}],
    "CSCO":  [{"sym": "CSCL",  "lev": 2.0, "desc": "2x Cisco",      "market": "US", "decay": "daily_reset"}],
    "ADBE":  [{"sym": "ADBU",  "lev": 2.0, "desc": "2x Adobe",      "market": "US", "decay": "daily_reset"},
              {"sym": "TECL",  "lev": 3.0, "desc": "3x Tech",       "market": "US", "decay": "daily_reset"}],
    "ASML":  [{"sym": "ASMU",  "lev": 2.0, "desc": "2x ASML",       "market": "US", "decay": "daily_reset"}],
    "ORCL":  [{"sym": "ORCU",  "lev": 2.0, "desc": "2x Oracle",     "market": "US", "decay": "daily_reset"}],
    "PANW":  [{"sym": "PALU",  "lev": 2.0, "desc": "2x Palo Alto",  "market": "US", "decay": "daily_reset"}],
    "PLTR":  [{"sym": "PLTU",  "lev": 2.0, "desc": "2x Palantir",   "market": "US", "decay": "daily_reset"}],
    "SHOP":  [{"sym": "SHPU",  "lev": 2.0, "desc": "2x Shopify",    "market": "US", "decay": "daily_reset"}],
    "SOFI":  [{"sym": "SOFA",  "lev": 2.0, "desc": "2x SoFi",       "market": "US", "decay": "daily_reset"}],
    "HOOD":  [{"sym": "HODU",  "lev": 2.0, "desc": "2x Robinhood",  "market": "US", "decay": "daily_reset"}],
    "COIN":  [{"sym": "CONX",  "lev": 2.0, "desc": "2x Coinbase",   "market": "US", "decay": "daily_reset"}],
    "PYPL":  [{"sym": "PYPU",  "lev": 2.0, "desc": "2x PayPal",     "market": "US", "decay": "daily_reset"}],
    "BA":    [{"sym": "BOEU",  "lev": 2.0, "desc": "2x Boeing",     "market": "US", "decay": "daily_reset"}],
    "LMT":   [{"sym": "LMTL",  "lev": 2.0, "desc": "2x Lockheed Martin","market": "US", "decay": "daily_reset"}],
    "BABA":  [{"sym": "BABU",  "lev": 2.0, "desc": "2x Alibaba",    "market": "US", "decay": "daily_reset"}],
    "BRKB":  [{"sym": "BRKU",  "lev": 2.0, "desc": "2x Berkshire",  "market": "US", "decay": "daily_reset"},
              {"sym": "FAS",   "lev": 3.0, "desc": "3x Financials", "market": "US", "decay": "daily_reset"}],
    "F":     [{"sym": "FRDU",  "lev": 2.0, "desc": "2x Ford",       "market": "US", "decay": "daily_reset"}],
    "LLY":   [{"sym": "ELIL",  "lev": 2.0, "desc": "2x Eli Lilly",  "market": "US", "decay": "daily_reset"},
              {"sym": "CURE",  "lev": 3.0, "desc": "3x Healthcare", "market": "US", "decay": "daily_reset"}],
    "JPM":   [{"sym": "FAS",   "lev": 3.0, "desc": "3x Financials", "market": "US", "decay": "daily_reset"}],
    "BAC":   [{"sym": "FAS",   "lev": 3.0, "desc": "3x Financials", "market": "US", "decay": "daily_reset"}],
    "GS":    [{"sym": "FAS",   "lev": 3.0, "desc": "3x Financials", "market": "US", "decay": "daily_reset"}],
    "XOM":   [{"sym": "XOMX",  "lev": 2.0, "desc": "2x ExxonMobil","market": "US", "decay": "daily_reset"},
              {"sym": "ERX",   "lev": 2.0, "desc": "2x Energy",     "market": "US", "decay": "daily_reset"}],
    "CVX":   [{"sym": "ERX",   "lev": 2.0, "desc": "2x Energy",     "market": "US", "decay": "daily_reset"}],
    "UNH":   [{"sym": "UNHU",  "lev": 2.0, "desc": "2x UnitedHealth","market": "US", "decay": "daily_reset"},
              {"sym": "CURE",  "lev": 3.0, "desc": "3x Healthcare", "market": "US", "decay": "daily_reset"}],
    "PFE":   [{"sym": "LABU",  "lev": 3.0, "desc": "3x Biotech",    "market": "US", "decay": "daily_reset"}],
    "MRNA":  [{"sym": "LABU",  "lev": 3.0, "desc": "3x Biotech",    "market": "US", "decay": "daily_reset"}],
    # Already leveraged ETFs — note this in advice
    "SOXL":  [],   # already 3x, signal itself is the trade
    "TQQQ":  [],
    "UPRO":  [],
    "TECL":  [],
    "CURE":  [],
    "LABU":  [],
    "FNGU":  [],
}

# ─── Composite threshold thresholds ──────────────────────────────────────────
_STRONG_SIGNAL = 80   # suggest leverage / bigger position
_GOOD_SIGNAL   = 60   # normal position
_WEAK_SIGNAL   = 45   # reduce / skip

# ─── Kelly Criterion Position Sizing (Idea 6) ─────────────────────────────────
# Legacy constants retained for pure Kelly math tests only; live advice never
# uses them unless model-versioned outcomes supply matching payoff inputs.
#   Win Rate = 44.7%, Avg Win = +30.54%, Avg Loss = -17.86%
#   Full Kelly = (WR × avg_W - (1-WR) × avg_L) / avg_W = 12.36%
#   Half-Kelly = 6.18% (conservative — industry standard for volatile assets)
# Score-based scaling: STRONG → 100% of Half-Kelly, GOOD → 75%, WEAK → 50%
_KELLY_WR_DEFAULT       = 0.447
_KELLY_AVG_WIN  = 0.3054
_KELLY_AVG_LOSS = 0.1786
_KELLY_SCALE    = {"STRONG": 1.0, "GOOD": 0.75, "WEAK": 0.50, "SKIP": 0.0}


@lru_cache(maxsize=8)
def _live_kelly_inputs(
    db_path: Path | None = None,
    min_samples: int = 100,
) -> tuple[float, float, float, int] | None:
    """Return versioned (win rate, avg win, avg loss, n) or None when unvalidated."""
    try:
        _root = Path(__file__).resolve().parent.parent
        _db = db_path or (_root / "database" / "alert_tracker.db")
        if not _db.exists():
            return None
        import sqlite3
        with sqlite3.connect(str(_db), timeout=5) as conn:
            model_row = conn.execute(
                "SELECT json_extract(parameters_json, '$.model_version') "
                "FROM signal_engine_outputs "
                "WHERE json_extract(parameters_json, '$.model_version') IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not model_row or not model_row[0]:
                return None
            rows = conn.execute(
                """
                WITH candidates AS (
                    SELECT seo.id, pe.return_pct,
                           ROW_NUMBER() OVER (
                               PARTITION BY seo.id
                               ORDER BY ABS(pe.hours_elapsed - 168.0)
                           ) AS rn
                    FROM signal_engine_outputs seo
                    JOIN price_evolution pe ON pe.output_id = seo.id
                    WHERE json_extract(seo.parameters_json, '$.model_version') = ?
                      AND pe.hours_elapsed BETWEEN 144.0 AND 192.0
                      AND pe.return_pct IS NOT NULL
                      AND (pe.return_pct >= 3.0 OR pe.return_pct <= -5.0)
                )
                SELECT return_pct FROM candidates WHERE rn = 1
                """,
                (model_row[0],),
            ).fetchall()
        returns = [float(row[0]) for row in rows]
        if len(returns) < min_samples:
            return None
        wins = [value for value in returns if value >= 3.0]
        losses = [value for value in returns if value <= -5.0]
        if not wins or not losses:
            return None
        win_rate = len(wins) / len(returns)
        avg_win = sum(wins) / len(wins) / 100.0
        avg_loss = abs(sum(losses) / len(losses)) / 100.0
        return win_rate, avg_win, avg_loss, len(returns)
    except Exception:
        return None


def _live_kelly_wr(db_path: Path | None = None, min_samples: int = 100) -> float | None:
    inputs = _live_kelly_inputs(db_path=db_path, min_samples=min_samples)
    return inputs[0] if inputs else None


def _compute_half_kelly(
    wr: float,
    avg_win: float = _KELLY_AVG_WIN,
    avg_loss: float = _KELLY_AVG_LOSS,
) -> float:
    if avg_win <= 0:
        return 0.0
    full = (wr * avg_win - (1 - wr) * avg_loss) / avg_win
    return max(full / 2.0, 0.0)

# Already-leveraged ETF symbols — if these appear as base, don't suggest more leverage
_ALREADY_LEVERAGED = frozenset({
    # ── Broad-index leveraged ETFs ─────────────────────────────────────────────
    "SOXL", "TQQQ", "UPRO", "TECL", "CURE", "LABU", "FNGU",
    "SSO", "QLD", "UDOW", "TNA", "FAS", "ERX", "UGL", "UCO",
    "BULL3", "BULL5",
    # ── SGX DLCs ───────────────────────────────────────────────────────────────
    "NVDDU", "AMDDU", "TSLDBU", "METDU", "AAPDU",
    "AMZDU", "GGLDU", "MSFDU", "BDIDBU",
    "SPDBU", "NQDBU",
    "HSIDB3", "HSIDC3", "NKYDB3",
    "700DBU", "9988DBU",
    # ── Direxion Bull 2X single-stock ──────────────────────────────────────────
    "AAPU", "ADBU", "AMUU", "AMZU", "ASMU", "AVL", "BABU", "BOEU",
    "BRKU", "CONX", "CSCL", "ELIL", "FRDU", "GGLL", "HODU", "LINT",
    "LMTL", "METU", "MRVU", "MSFU", "MUU", "NFXL", "NVDU", "ORCU",
    "PALU", "PLTU", "PYPU", "QCMU", "SHPU", "SOFA", "TSLL", "TSMX",
    "TXNU", "UNHU", "XOMX",
    # ── Direxion Bear 1X single-stock (inverse) ───────────────────────────────
    "AAPD", "AMDD", "AMZD", "AVS", "BOED", "BRKD", "CSCS", "ELIS",
    "FRDD", "GGLS", "LMTS", "METD", "MSFD", "MUD", "NFXS", "NVDD",
    "ORCS", "PALD", "PLTD", "QCMD", "SHPD", "TSLS", "TSMZ", "XOMZ",
    # ── GraniteShares single-stock ─────────────────────────────────────────────
    "NVDL", "AMDL", "CONL", "SMCX", "GOGU", "MULL",
    # ── T-Rex / REX Shares 2X single-stock ─────────────────────────────────────
    "MSTU", "MSTX", "NVDX", "TSLT", "AAPX", "GOOX", "MSFO", "MSFX",
    "PTIR", "OKLL", "NFLU", "ROBN", "DKUP", "ARMU", "DJTU", "RBLU",
    "GMEU", "SNOU", "SMUP", "AXUP", "KTUP", "TTDU", "BKNU",
    # ── T-Rex / REX Shares 2X Inverse single-stock ────────────────────────────
    "MSTZ", "NVDQ", "TSLZ",
})


# ─── Forex signal interpretation ─────────────────────────────────────────────
# For each pair: (label, border_color, signal_note) when rate is above or below MA50.
# Threshold: |vs_ma_pct| > 0.5% triggers a signal (otherwise neutral/monitoring).
_FX_SIGNAL_RULES: dict[str, dict] = {
    "AUDUSD=X": {
        "above": ("🟢 AUD STRONG — BUY USD assets", "#22c55e",
                  "AUD above MA50: favorable FX to convert AUD→USD (IBKR / US stocks)"),
        "below": ("🔴 AUD WEAK — DEFER USD buys",   "#ef4444",
                  "AUD below MA50: costly to buy USD; consider waiting for AUD recovery"),
    },
    "USDSGD=X": {
        "above": ("🔴 USD COSTLY in SGD",  "#ef4444",
                  "USD/SGD above MA50: USD expensive; delay converting SGD→USD"),
        "below": ("🟢 USD CHEAP in SGD",   "#22c55e",
                  "USD/SGD below MA50: good time to buy USD-denominated assets with SGD"),
    },
    "USDINR=X": {
        "above": ("⚠️ USD COSTLY — BUY Indian equities",  "#f59e0b",
                  "USD expensive vs INR: deploy INR into NSE stocks now; defer USD repatriation until INR recovers"),
        "below": ("🟢 USD CHEAP — repatriate INR→USD",   "#22c55e",
                  "USD cheap vs INR: favorable window to convert INR profits to USD or buy USD-denominated assets from India"),
    },
    "EURUSD=X": {
        "above": ("🟢 EUR STRONG — risk-on", "#22c55e",
                  "EUR/USD above MA50: risk-on sentiment; equities broadly supportive"),
        "below": ("🔴 EUR WEAK — risk-off",  "#ef4444",
                  "EUR/USD below MA50: risk-off; favour defensive positioning"),
    },
    "USDJPY=X": {
        "above": ("🟢 JPY WEAK — BUY JPY assets cheap", "#22c55e",
                  "USD/JPY above MA50: JPY weak = Japanese stocks (1579, 2865) are AUD-cheap; risk-on globally"),
        "below": ("🔴 JPY STRONG — SELL JPY assets / hold cash", "#ef4444",
                  "USD/JPY below MA50: JPY safe-haven rally = risk-off; avoid new JPY-denom buys; consider trimming"),
    },
    "AUDSGD=X": {
        "above": ("🟢 AUD STRONG vs SGD — SGX assets cheap", "#22c55e",
                  "AUD/SGD above MA50: AUD strong; good time to convert AUD→SGD for Moomoo SG or SGX stocks"),
        "below": ("🔴 AUD WEAK vs SGD — defer SGX buys",   "#ef4444",
                  "AUD/SGD below MA50: AUD weak; SGX stocks expensive in AUD terms; wait for AUD/SGD recovery"),
    },
}

# ─── Forex pairs for snapshot ─────────────────────────────────────────────────
_FOREX_PAIRS: list[dict] = [
    {"sym": "AUDUSD=X",  "label": "AUD/USD", "base": "AUD", "quote": "USD",
     "note": "Sells AUD to buy USD for IBKR/US trades"},
    {"sym": "USDSGD=X",  "label": "USD/SGD", "base": "USD", "quote": "SGD",
     "note": "SGD cost of USD-denominated holdings"},
    {"sym": "USDINR=X",  "label": "USD/INR", "base": "USD", "quote": "INR",
     "note": "USD/INR for repatriation planning"},
    {"sym": "EURUSD=X",  "label": "EUR/USD", "base": "EUR", "quote": "USD",
     "note": "Broad risk-on indicator"},
    {"sym": "USDJPY=X",  "label": "USD/JPY", "base": "USD", "quote": "JPY",
     "note": "Risk-on proxy — JPY weakens in bull markets"},
    {"sym": "AUDSGD=X",  "label": "AUD/SGD", "base": "AUD", "quote": "SGD",
     "note": "Cost of AUD→SGD conversion for Moomoo SG / SGX positions"},
]


# ─── Cash Fetching ────────────────────────────────────────────────────────────

# Per-currency breakdown columns in Moomoo accinfo_query DataFrame.
# Each tuple: (column_name_in_df, currency_label)
_MOOMOO_AU_CURRENCIES: list[tuple[str, str]] = [("au_cash", "AUD"), ("us_cash", "USD")]
_MOOMOO_SG_CURRENCIES: list[tuple[str, str]] = [("sg_cash", "SGD"), ("us_cash", "USD"), ("jp_cash", "JPY")]


def _fetch_moomoo_cash_multi(
    port: int, firm, acc_id: int,
    currencies: list[tuple[str, str]],
) -> dict[str, float]:
    """
    Return per-currency available cash from Moomoo OpenD.

    Reads the per-currency sub-wallet fields from accinfo_query (au_cash,
    us_cash, sg_cash, jp_cash, etc.) and returns only non-zero balances.
    Falls back to the aggregate 'cash' field if no per-currency columns are
    populated.

    Returns {} on any error or if OpenD is not running.
    """
    if not _MOOMOO_OK:
        return {}
    try:
        if not is_opend_running(port):
            return {}
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE,
            host=OPEND_HOST,
            port=port,
            security_firm=firm,
        )
        ret, df = ctx.accinfo_query(
            trd_env=TrdEnv.REAL,
            acc_id=acc_id,
            refresh_cache=True,
        )
        ctx.close()
        if ret != RET_OK or df is None or df.empty:
            return {}
        row = df.iloc[0]
        result: dict[str, float] = {}
        for col, cur in currencies:
            val = row.get(col)
            if val is not None:
                try:
                    result[cur] = round(float(val), 2)
                except (ValueError, TypeError):
                    pass
        # Fallback: per-currency columns not populated — use aggregate cash
        if not result:
            aggregate = float(row.get("cash", row.get("power", 0)) or 0)
            frozen = float(row.get("frozen_cash", 0) or 0)
            available = round(max(0.0, aggregate - frozen), 2)
            if available > 0:
                primary_cur = currencies[0][1]  # primary currency for this account
                result[primary_cur] = available
        return result
    except Exception:
        return {}


def _fetch_zerodha_cash() -> Optional[float]:
    """
    Fetch Zerodha INR available cash using the zerodha_auth singleton.
    Uses get_kite() which returns the module-level singleton — avoids creating
    multiple KiteConnect sessions (each call to generate_session invalidates the
    previous token). Falls back gracefully if credentials unavailable.
    """
    try:
        import sys
        _india = _ROOT / "india-brokers"
        if str(_india) not in sys.path:
            sys.path.insert(0, str(_india))
        from zerodha_auth import get_kite  # type: ignore
        kite = get_kite(silent=True)
        margins = kite.margins()
        eq = margins.get("equity", {})
        avail = eq.get("available", {})
        cash = float(avail.get("live_balance", avail.get("cash", 0)) or 0)
        return round(cash, 2) if cash >= 0 else None
    except Exception:
        return None


def _fetch_angelone_cash() -> Optional[float]:
    """
    Try to fetch Angel One INR available cash via SmartAPI rmsLimit.
    Requires ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, ANGELONE_PIN,
    and ANGELONE_TOTP_SECRET env vars.
    Returns None if credentials are absent or any error occurs.
    """
    try:
        from SmartApi import SmartConnect  # type: ignore
        import pyotp  # type: ignore
        api_key   = os.environ.get("ANGELONE_API_KEY", "")
        client    = os.environ.get("ANGELONE_CLIENT_CODE", "")
        pin       = os.environ.get("ANGELONE_PIN", "")
        totp_sec  = os.environ.get("ANGELONE_TOTP_SECRET", "")
        if not all([api_key, client, pin, totp_sec]):
            return None
        totp = pyotp.TOTP(totp_sec).now()
        angel = SmartConnect(api_key)
        angel.generateSession(client, pin, totp)
        rms = angel.rmsLimit()
        if rms and rms.get("status"):
            cash = float(rms.get("data", {}).get("availablecash", 0) or 0)
            return round(cash, 2) if cash >= 0 else None
        return None
    except Exception:
        return None


def _fetch_ibkr_cash() -> dict[str, float]:
    """
    Read IBKR cash balances from the Flex Query XML cache (no TWS required).
    Parses CashReportCurrency nodes from cgt/ibkr-cgt/_last_flex.xml.
    Returns dict like {"USD": 162.10, "AUD": 0.0} or {} if unavailable.
    """
    import xml.etree.ElementTree as ET
    _flex_path = _ROOT / "cgt" / "ibkr-cgt" / "_last_flex.xml"
    if not _flex_path.exists():
        return {}
    try:
        tree = ET.parse(str(_flex_path))
        result: dict[str, float] = {}
        for node in tree.iter("CashReportCurrency"):
            cur = node.get("currency", "")
            if cur in ("AUD", "USD"):
                val_str = node.get("endingCash", "0") or "0"
                val = round(float(val_str), 2)
                result[cur] = val
        return result
    except Exception:
        return {}


def fetch_all_account_cash() -> dict[str, Optional[float]]:
    """
    Fetch available cash from all accounts.

    Returns a flat dict mapping ``"Account (CCY)"`` keys to cash amounts.
    Moomoo AU/SG are fetched live via OpenD and may emit multiple currency
    keys per account (e.g. ``"Moomoo AU (AUD)"`` and ``"Moomoo AU (USD)"``).
    Zerodha and Angel One are fetched if their env-var credentials are set.
    IBKR always returns None (requires a separate session).
    """
    cash: dict[str, Optional[float]] = {}

    if _MOOMOO_OK:
        # ── Moomoo AU: AUD primary + USD sub-wallet ───────────────────────────
        au_wallets = _fetch_moomoo_cash_multi(OPEND_AU_PORT, AU_FIRM, AU_ACCOUNT_ID,
                                              _MOOMOO_AU_CURRENCIES)
        if au_wallets:
            for cur, amt in au_wallets.items():
                cash[f"Moomoo AU ({cur})"] = amt
        else:
            cash["Moomoo AU (AUD)"] = None

        # ── Moomoo SG: SGD primary + USD + JPY sub-wallets ───────────────────
        sg_wallets = _fetch_moomoo_cash_multi(OPEND_SG_PORT, SG_FIRM, SG_ACCOUNT_ID,
                                              _MOOMOO_SG_CURRENCIES)
        if sg_wallets:
            for cur, amt in sg_wallets.items():
                cash[f"Moomoo SG ({cur})"] = amt
        else:
            cash["Moomoo SG (SGD)"] = None
    else:
        cash["Moomoo AU (AUD)"] = None
        cash["Moomoo SG (SGD)"] = None

    # ── IBKR: try live fetch via ibapi (TWS/IB Gateway on port 7496) ────────────
    ibkr_wallets = _fetch_ibkr_cash()
    if ibkr_wallets:
        for cur, amt in ibkr_wallets.items():
            cash[f"IBKR ({cur})"] = amt
    else:
        cash["IBKR (USD)"] = None
        cash["IBKR (AUD)"] = None

    # ── Zerodha: try live fetch via kiteconnect ───────────────────────────────
    cash["Zerodha (INR)"] = _fetch_zerodha_cash()

    # ── Angel One: try live fetch via SmartAPI ────────────────────────────────
    cash["Angel One (INR)"] = _fetch_angelone_cash()

    return cash


# ─── Leveraged Alternatives ───────────────────────────────────────────────────

def get_leveraged_alternatives(tv_symbol: str) -> list[dict]:
    """
    Given a TV symbol like 'NASDAQ:NVDA' or 'NYSE:SPY', return list of
    leveraged ETF/DLC alternatives from LEVERAGED_UNIVERSE.
    Returns [] if none found or if the stock itself is already leveraged.
    """
    base = tv_symbol.split(":")[-1].upper() if ":" in tv_symbol else tv_symbol.upper()
    if base in _ALREADY_LEVERAGED:
        return []
    return LEVERAGED_UNIVERSE.get(base, [])


def get_fo_info(tv_symbol: str) -> Optional[dict]:
    """
    For NSE/BSE symbols, return F&O eligibility dict, or None for non-India stocks.
    """
    exch = tv_symbol.split(":")[0].upper() if ":" in tv_symbol else ""
    if exch not in ("NSE", "BSE", "NSE_DLY", "BSE_DLY"):
        return None
    base = tv_symbol.split(":")[-1].upper() if ":" in tv_symbol else tv_symbol.upper()
    eligible = is_fo_eligible(base)
    lot_size = NSE_FO_LOT_SIZES.get(base)
    return {
        "eligible":  eligible,
        "lot_size":  lot_size,
        "symbol":    base,
    }


# ─── Core Advice Builder ──────────────────────────────────────────────────────

def build_buy_advice(item: dict, account_cash: dict | None = None) -> dict:
    """
    Build a buy advice dict for a single signal card item.
    Returns a dict consumed by _buy_advisor_html().

    Fields:
      recommended_account: str  — e.g. "IBKR (USD)"
      all_accounts: list[str]   — all eligible
      cash: dict                — {account: amount_or_None}
      leverage_alts: list[dict] — leveraged ETF alternatives
      fo_info: dict|None        — F&O info for India stocks
      conviction: str           — "STRONG" / "GOOD" / "WEAK"
      already_leveraged: bool   — True if base symbol is already a leveraged product
      suggestion: str           — plain-English advice
      fx_warning: str           — FX exposure note
    """
    tv_sym   = item.get("tv_symbol", "")
    comp     = item.get("composite", 0)
    exch     = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
    base_sym = tv_sym.split(":")[-1].upper() if ":" in tv_sym else tv_sym.upper()

    # ── Account routing ───────────────────────────────────────────────────────
    eligible_accounts = _EXCHANGE_ACCOUNTS.get(
        exch,
        _EXCHANGE_ACCOUNTS.get("NYSE", ["Unknown"])  # default to US
    )
    recommended = eligible_accounts[0] if eligible_accounts else "Unknown"
    cash = account_cash or {}

    # Read model evidence once (cached) before suggesting leverage or sizing.
    _kelly_inputs = _live_kelly_inputs()

    # ── Conviction level ──────────────────────────────────────────────────────
    if item.get("actionability_reasons"):
        conviction = "SKIP"
    elif _kelly_inputs is None:
        conviction = "UNVALIDATED"
    elif comp >= _STRONG_SIGNAL:
        conviction = "STRONG"
    elif comp >= _GOOD_SIGNAL:
        conviction = "GOOD"
    elif comp >= _WEAK_SIGNAL:
        conviction = "WEAK"
    else:
        conviction = "SKIP"

    # ── Leveraged alternatives ────────────────────────────────────────────────
    already_lev = base_sym in _ALREADY_LEVERAGED
    lev_alts: list[dict] = (
        get_leveraged_alternatives(tv_sym)
        if _kelly_inputs is not None and not already_lev and conviction != "SKIP"
        else []
    )

    # ── F&O info (India only) ─────────────────────────────────────────────────
    fo_info = get_fo_info(tv_sym)

    # ── Kelly Criterion position sizing (Idea 6) ─────────────────────────────
    _half_kelly = (
        _compute_half_kelly(_kelly_inputs[0], _kelly_inputs[1], _kelly_inputs[2])
        if _kelly_inputs else 0.0
    )
    kelly_pct = round(_half_kelly * _KELLY_SCALE.get(conviction, 0.0) * 100, 1)
    near_earnings = item.get("near_earnings", False)
    if near_earnings:
        kelly_pct = round(kelly_pct * 0.5, 1)  # halve size around earnings

    # ── Plain-English suggestion ──────────────────────────────────────────────
    suggestion = _build_suggestion(base_sym, comp, conviction, lev_alts,
                                   already_lev, fo_info if _kelly_inputs else None,
                                   recommended, cash,
                                   kelly_pct=kelly_pct, near_earnings=near_earnings)

    # ── FX warning ───────────────────────────────────────────────────────────
    fx_warning = _build_fx_warning(exch, recommended)

    return {
        "tv_symbol":          tv_sym,
        "base_symbol":        base_sym,
        "exchange":           exch,
        "recommended_account": recommended,
        "all_accounts":       eligible_accounts,
        "cash":               cash,
        "leverage_alts":      lev_alts,
        "fo_info":            fo_info,
        "conviction":         conviction,
        "already_leveraged":  already_lev,
        "suggestion":         suggestion,
        "fx_warning":         fx_warning,
        "kelly_pct":          kelly_pct,
        "kelly_validated":    _kelly_inputs is not None,
        "kelly_sample_count": _kelly_inputs[3] if _kelly_inputs else 0,
        "near_earnings":      near_earnings,
    }


def _build_suggestion(
    base: str, comp: float, conviction: str,
    lev_alts: list[dict], already_lev: bool,
    fo_info: Optional[dict], account: str,
    cash: dict,
    kelly_pct: float = 0.0,
    near_earnings: bool = False,
) -> str:
    parts: list[str] = []

    if conviction == "SKIP":
        return "Quality gate failed — research only; no position sizing."
    if conviction == "UNVALIDATED":
        return (
            f"Model awaiting 100 validated 7-day outcomes — review {base} stock only; "
            "no leverage, F&O, or portfolio sizing."
        )

    # Kelly position sizing line
    if kelly_pct > 0:
        kelly_note = f"📊 Kelly suggests {kelly_pct:.1f}% of portfolio per position."
        if near_earnings:
            kelly_note += "  ⚠ Halved for nearby earnings."
        parts.append(kelly_note)

    # Available cash note
    acct_cash = cash.get(account)
    if acct_cash is not None:
        parts.append(f"{account}: {_fmt_cash(acct_cash)} available.")

    if already_lev:
        parts.append(f"{base} is already a leveraged product — trade directly.")
        if conviction == "STRONG":
            parts.append("🔥 Strong signal — consider adding 2 units (see backtest above).")
        return "  ".join(parts)

    # Primary vehicle
    if conviction == "STRONG":
        parts.append(f"🔥 Score {comp:.0f}/100 — High conviction.")
        if lev_alts:
            us_alts = [a for a in lev_alts if a["market"] == "US"]
            sg_alts = [a for a in lev_alts if a["market"] == "SG"]
            if us_alts:
                best = us_alts[0]
                parts.append(
                    f"Consider {best['sym']} ({best['desc']}) for amplified upside "
                    f"— {best['lev']}x daily reset ETF."
                )
            if sg_alts and "Moomoo SG" in account:
                dlc = sg_alts[0]
                parts.append(
                    f"SGX DLC: {dlc['sym']} ({dlc['desc']}) — EXTREME caution: "
                    f"barrier risk + {dlc['lev']}x decay."
                )
    elif conviction == "GOOD":
        parts.append(f"Score {comp:.0f}/100 — Good signal.")
        parts.append(f"Trade {base} direct in {account}.")
        if lev_alts:
            best = lev_alts[0]
            parts.append(
                f"Optional: {best['sym']} ({best['desc']}) if regime is bullish."
            )
    else:
        parts.append(f"Score {comp:.0f}/100 — Weak signal — prefer stock only, 1 unit.")

    # F&O note
    if fo_info and fo_info.get("eligible"):
        lot = fo_info.get("lot_size", "?")
        parts.append(
            f"F&O eligible: {fo_info['symbol']} futures · lot {lot} shares. "
            f"Use Zerodha NFO for leverage without daily ETF decay."
        )

    return "  ".join(parts)


def _build_fx_warning(exch: str, account: str) -> str:
    """Return FX exposure warning string."""
    if exch in ("NYSE", "NASDAQ", "AMEX", "BATS", "CBOE"):
        if account in _AUD_ACCOUNTS:
            return "⚠ FX: Buying USD stock from AUD account — AUD/USD rate affects your P&L."
        if account in _SGD_ACCOUNTS:
            return "⚠ FX: Buying USD stock from SGD account — USD/SGD rate affects your P&L."
    if exch in ("HK", "HKEX"):
        if account in _AUD_ACCOUNTS:
            return "⚠ FX: HKD stock from AUD base — AUD/HKD exposure."
        if account in _SGD_ACCOUNTS:
            return "⚠ FX: HKD stock from SGD base — SGD/HKD exposure (both HKD-pegged, low risk)."
    if exch == "ASX":
        return ""   # AUD account, AUD stock = no FX risk
    if exch in ("NSE", "BSE", "NSE_DLY", "BSE_DLY"):
        return ""   # INR account, INR stock = no FX risk
    return ""


def _fmt_cash(amount: Optional[float]) -> str:
    if amount is None:
        return "N/A"
    if amount >= 1_000_000:
        return f"{amount/1_000_000:.2f}M"
    if amount >= 1_000:
        return f"{amount:,.0f}"
    return f"{amount:.2f}"


# ─── Forex Snapshot ───────────────────────────────────────────────────────────

def get_forex_snapshot() -> list[dict]:
    """
    Fetch current FX rates + 50-day MA trend for all _FOREX_PAIRS.
    Returns list of dicts; each dict has:
      label, rate, ma50, vs_ma_pct, trend_label, trend_color, note
    Returns [] if yfinance unavailable.
    """
    if not _YF_OK:
        return []

    results = []
    for pair in _FOREX_PAIRS:
        try:
            import pandas as pd  # type: ignore
            ticker = yf.Ticker(pair["sym"])
            hist = ticker.history(period="100d", interval="1d", auto_adjust=True)
            if hist is None or hist.empty or len(hist) < 20:
                continue
            close = hist["Close"].dropna()
            rate = float(close.iloc[-1])
            ma50 = float(close.tail(50).mean()) if len(close) >= 50 else float(close.mean())
            vs_ma = (rate - ma50) / ma50 * 100
            if vs_ma > 3:
                trend_label = "Above MA50 ↑ (strong)"
                trend_color = "#f59e0b"  # amber — FX headwind for buyers
            elif vs_ma > 1:
                trend_label = "Slightly above MA50"
                trend_color = "#94a3b8"
            elif vs_ma < -3:
                trend_label = "Below MA50 ↓ (weak)"
                trend_color = "#4ade80"  # green — FX tailwind for buyers
            elif vs_ma < -1:
                trend_label = "Slightly below MA50"
                trend_color = "#94a3b8"
            else:
                trend_label = "At MA50 (neutral)"
                trend_color = "#94a3b8"

            # ── FX actionable signal ──────────────────────────────────────────
            rules = _FX_SIGNAL_RULES.get(pair["sym"])
            if rules and abs(vs_ma) > 0.5:
                direction = "above" if vs_ma > 0 else "below"
                sig_label, sig_border, sig_note = rules[direction]
            else:
                sig_label  = "— Monitoring"
                sig_border = "#1e293b"
                sig_note   = ""

            results.append({
                "label":        pair["label"],
                "rate":         round(rate, 5),
                "ma50":         round(ma50, 5),
                "vs_ma_pct":    round(vs_ma, 2),
                "trend_label":  trend_label,
                "trend_color":  trend_color,
                "note":         pair["note"],
                "signal_label": sig_label,
                "signal_border":sig_border,
                "signal_note":  sig_note,
            })
        except Exception:
            continue
    return results


# ─── HTML Generation — Per Card ───────────────────────────────────────────────

def _buy_advisor_html(advice: dict | None) -> str:
    """Return HTML block for embedding in a signal card. Returns '' if advice is None."""
    if not advice:
        return ""

    rec_account   = advice.get("recommended_account", "Unknown")
    all_accounts  = advice.get("all_accounts", [])
    cash          = advice.get("cash", {})
    lev_alts      = advice.get("leverage_alts", [])
    fo_info       = advice.get("fo_info")
    conviction    = advice.get("conviction", "GOOD")
    already_lev   = advice.get("already_leveraged", False)
    suggestion    = advice.get("suggestion", "")
    fx_warn       = advice.get("fx_warning", "")

    # ── Conviction badge ──────────────────────────────────────────────────────
    conv_conf: dict[str, tuple[str, str]] = {
        "STRONG": ("🔥 STRONG",  "#b45309"),
        "GOOD":   ("✓ GOOD",    "#1e3a5f"),
        "WEAK":   ("~ WEAK",    "#292524"),
        "SKIP":   ("✗ SKIP",    "#450a0a"),
        "UNVALIDATED": ("UNVALIDATED", "#334155"),
    }
    conv_label, conv_bg = conv_conf.get(conviction, ("INFO", "#1e293b"))

    # ── Account rows ──────────────────────────────────────────────────────────
    acct_rows = ""
    for acct in all_accounts[:4]:
        amt = cash.get(acct)
        is_rec = (acct == rec_account)
        amt_str = _fmt_cash(amt) if amt is not None else "N/A"
        amt_col = "#22c55e" if (amt and amt > 5000) else ("#eab308" if (amt and amt > 0) else "#475569")
        star = " ★" if is_rec else ""
        acct_rows += (
            f'<div class="ba-acct-row">'
            f'<span class="ba-acct-name{"  ba-acct-rec" if is_rec else ""}">{acct}{star}</span>'
            f'<span class="ba-acct-cash" style="color:{amt_col}">{amt_str}</span>'
            f'</div>'
        )

    # ── Leveraged ETF pills ───────────────────────────────────────────────────
    lev_pills = ""
    for alt in lev_alts[:5]:
        decay_col = "#ef4444" if alt.get("decay") == "extreme" else "#f59e0b"
        decay_tag = (
            f'<span class="ba-decay-extreme">⚠ EXTREME DECAY</span>'
            if alt.get("decay") == "extreme"
            else f'<span class="ba-decay-daily">daily reset</span>'
        )
        note = f'<span class="ba-lev-note">{alt["note"]}</span>' if alt.get("note") else ""
        lev_pills += (
            f'<div class="ba-lev-pill">'
            f'<span class="ba-lev-sym">{alt["sym"]}</span>'
            f'<span class="ba-lev-desc">{alt["desc"]}</span>'
            f'{decay_tag}{note}'
            f'</div>'
        )

    already_lev_note = ""
    if already_lev:
        already_lev_note = (
            '<div class="ba-already-lev">⚡ This symbol is already a leveraged product — '
            'no additional leverage needed.</div>'
        )

    # ── F&O info (India only) ─────────────────────────────────────────────────
    fo_html = ""
    if fo_info is not None:
        if fo_info.get("eligible"):
            lot = fo_info.get("lot_size", "?")
            fo_html = (
                f'<div class="ba-fo-row ba-fo-eligible">'
                f'🇮🇳 F&O Eligible — lot size: <b>{lot} shares</b> · '
                f'Trade on Zerodha NFO for leverage (no ETF decay)</div>'
            )
        else:
            fo_html = '<div class="ba-fo-row ba-fo-none">🇮🇳 F&O: Not eligible on NSE</div>'

    # ── FX warning ────────────────────────────────────────────────────────────
    fx_html = ""
    if fx_warn:
        fx_html = f'<div class="ba-fx-warn">{fx_warn}</div>'

    # ── Suggestion ────────────────────────────────────────────────────────────
    suggestion_html = (
        f'<div class="ba-suggestion">{suggestion}</div>' if suggestion else ""
    )

    # ── No leveraged alts message ─────────────────────────────────────────────
    no_lev_msg = ""
    if not lev_alts and not already_lev:
        no_lev_msg = '<div class="ba-no-lev">No mapped leveraged ETF for this symbol — trade stock directly.</div>'

    return f"""
<div class="ba-block">
  <div class="ba-header">
    <div class="ba-title">🎯 Buy Vehicle Advisor</div>
    <span class="ba-conviction" style="background:{conv_bg}">{conv_label}</span>
  </div>
  <div class="ba-body">
    <!-- Account routing -->
    <div class="ba-section-head">🏦 Account &amp; Cash</div>
    <div class="ba-acct-grid">{acct_rows}</div>
    <!-- Leveraged alternatives -->
    <div class="ba-section-head">⚡ Leveraged Alternatives</div>
    {already_lev_note}
    {lev_pills if lev_pills else no_lev_msg}
    <!-- F&O info -->
    {fo_html}
    <!-- FX warning -->
    {fx_html}
    <!-- Main suggestion -->
    {suggestion_html}
  </div>
</div>"""


# ─── HTML Generation — Global Cash Panel ──────────────────────────────────────

def build_account_cash_html(cash: dict[str, Optional[float]]) -> str:
    """
    Return an HTML strip showing all account cash balances.
    Designed to be inserted below the main page header in signal_stack reports.

    Keys in *cash* are of the form ``"Account Name (CCY)"``; the currency is
    parsed from the key so no hard-coded currency map is needed.
    """
    if not cash:
        return ""

    cells = ""
    for account, amount in cash.items():
        # Parse currency from key, e.g. "Moomoo AU (AUD)" → "AUD"
        currency = ""
        if "(" in account and account.endswith(")"):
            currency = account.rsplit("(", 1)[-1].rstrip(")")

        if amount is not None:
            amt_str = f"{currency} {_fmt_cash(amount)}"
            amt_col = "#22c55e" if amount > 5000 else ("#eab308" if amount > 0 else "#ef4444")
            status = "live"
        else:
            amt_str = "N/A"
            amt_col = "#475569"
            status = "offline"

        cells += (
            f'<div class="cash-cell">'
            f'<div class="cash-acct">{account}</div>'
            f'<div class="cash-amt" style="color:{amt_col}">{amt_str}</div>'
            f'<div class="cash-status">{status}</div>'
            f'</div>'
        )

    return f"""<div class="cash-panel">
  <div class="cash-panel-title">💰 Available Cash by Account</div>
  <div class="cash-cells">{cells}</div>
  <div class="cash-note">Moomoo live via OpenD (AUD+USD / SGD+USD+JPY sub-wallets) · Zerodha/Angel One: auto-fetched when credentials set · IBKR: run separately</div>
</div>"""


# ─── CSS Styles ───────────────────────────────────────────────────────────────

BUY_ADVISOR_CSS = """
/* ── Buy Advisor block ─────────────────────────────────────────────────────── */
.ba-block{padding:12px 20px;border-top:1px solid #1e293b;background:#0a0f1e}
.ba-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.ba-title{font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em}
.ba-conviction{font-size:11px;font-weight:700;padding:2px 10px;border-radius:10px;color:#fff}
.ba-body{display:flex;flex-direction:column;gap:6px}
.ba-section-head{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em;margin-top:4px}
.ba-acct-grid{display:flex;flex-wrap:wrap;gap:4px}
.ba-acct-row{background:#111827;border:1px solid #1e293b;border-radius:6px;padding:4px 10px;font-size:11px;display:flex;gap:8px;align-items:center}
.ba-acct-name{color:#94a3b8}
.ba-acct-rec{color:#38bdf8;font-weight:600}
.ba-acct-cash{font-family:monospace;font-weight:600}
.ba-lev-pill{background:#111827;border:1px solid #1e293b;border-radius:6px;padding:4px 10px;font-size:11px;display:inline-flex;gap:6px;align-items:center;margin:2px}
.ba-lev-sym{color:#f59e0b;font-weight:700;font-family:monospace}
.ba-lev-desc{color:#94a3b8}
.ba-decay-extreme{color:#ef4444;font-size:10px;font-weight:600}
.ba-decay-daily{color:#475569;font-size:10px}
.ba-lev-note{color:#64748b;font-size:10px;font-style:italic}
.ba-already-lev{font-size:11px;color:#38bdf8;padding:4px 0}
.ba-no-lev{font-size:11px;color:#475569;font-style:italic}
.ba-fo-row{font-size:11px;padding:4px 0}
.ba-fo-eligible{color:#4ade80}
.ba-fo-none{color:#64748b}
.ba-fx-warn{font-size:11px;color:#f59e0b;padding:4px 0}
.ba-suggestion{font-size:11px;color:#94a3b8;line-height:1.5;padding:6px 10px;background:#111827;border-left:3px solid #38bdf8;border-radius:0 6px 6px 0;margin-top:4px}
/* ── Cash Panel ─────────────────────────────────────────────────────────────── */
.cash-panel{padding:10px 24px;background:#0f172a;border-bottom:1px solid #1e293b;display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.cash-panel-title{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;min-width:max-content}
.cash-cells{display:flex;flex-wrap:wrap;gap:8px}
.cash-cell{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:6px 12px;text-align:center;min-width:110px}
.cash-acct{font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:.04em}
.cash-amt{font-size:13px;font-weight:700;font-family:monospace;margin:2px 0}
.cash-status{font-size:9px;color:#334155}
.cash-note{font-size:10px;color:#334155;align-self:center}
"""


# ─── Forex Panel HTML ─────────────────────────────────────────────────────────

def build_forex_html(forex: list[dict]) -> str:
    """Build an HTML strip for FX snapshot. Returns '' if forex list is empty."""
    if not forex:
        return ""

    cells = ""
    for f in forex:
        sig_label  = f.get("signal_label", "")
        sig_border = f.get("signal_border", "#1e293b")
        sig_note   = f.get("signal_note", "")
        is_signal  = sig_label and sig_label != "— Monitoring"
        signal_html = (
            f'<div class="fx-signal" style="color:{sig_border}">{sig_label}</div>'
            f'<div class="fx-signal-note">{sig_note}</div>'
        ) if is_signal else (
            f'<div class="fx-signal fx-signal-neutral">{sig_label}</div>'
        )
        border_style = f"border-color:{sig_border};border-width:2px" if is_signal else ""
        cells += (
            f'<div class="fx-cell" style="{border_style}">'
            f'<div class="fx-label">{f["label"]}</div>'
            f'<div class="fx-rate">{f["rate"]:.4f}</div>'
            f'<div class="fx-trend" style="color:{f["trend_color"]}">'
            f'{f["trend_label"]} ({f["vs_ma_pct"]:+.1f}%)</div>'
            f'{signal_html}'
            f'<div class="fx-note">{f["note"]}</div>'
            f'</div>'
        )

    return f"""<div class="fx-panel">
  <div class="fx-title">🌐 FX Snapshot (vs 50-day MA)</div>
  <div class="fx-cells">{cells}</div>
</div>"""


FX_PANEL_CSS = """
/* ── FX Panel ────────────────────────────────────────────────────────────── */
.fx-panel{padding:10px 24px;background:#060c18;border-bottom:1px solid #1e293b;display:flex;flex-wrap:wrap;align-items:flex-start;gap:10px}
.fx-title{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;min-width:max-content;align-self:center}
.fx-cells{display:flex;flex-wrap:wrap;gap:8px}
.fx-cell{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:6px 12px;min-width:150px;transition:border-color .2s}
.fx-label{font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:.04em}
.fx-rate{font-size:14px;font-weight:700;font-family:monospace;color:#f8fafc;margin:2px 0}
.fx-trend{font-size:10px;font-weight:600}
.fx-signal{font-size:10px;font-weight:700;margin-top:4px;letter-spacing:.02em}
.fx-signal-neutral{color:#475569}
.fx-signal-note{font-size:9px;color:#64748b;margin-top:1px;line-height:1.3}
.fx-note{font-size:9px;color:#334155;margin-top:3px}
"""
