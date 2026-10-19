"""
India Market Internals  — Angel One SmartAPI (FREE tier)
=========================================================
Three institutional-grade data feeds for NSE, all available at no extra cost:

  1. PCR (Put-Call Ratio)
     ─────────────────────
     NSE F&O PCR per underlying. Equivalent to the US options PCR already
     scored in signal_stack.py L4 (fetch_market_internals). Now we do the
     same for Indian stocks.
       PCR < 0.8  → Bullish (more calls, market expects up)
       PCR > 1.2  → Bearish (more puts, hedging / expectation of down)
     API: GET /marketData/v1/putCallRatio

  2. OI Buildup
     ──────────
     Classifies each F&O stock into:
       Long Buildup    → price up + OI up  → bulls adding positions
       Short Buildup   → price down + OI up → bears adding positions
       Short Covering  → price up + OI down → shorts exiting (very bullish)
       Long Unwinding  → price down + OI down → bulls exiting (bearish)
     API: POST /marketData/v1/OIBuildup

  3. FULL Market Data (batch, 50 symbols per call)
     ───────────────────────────────────────────────
     Per symbol: LTP, OHLC, 52W hi/lo, volume, buy-qty / sell-qty ratio,
     circuit limits, net %change.
     This replaces Zerodha's ltp() (which needs a paid Quote subscription)
     and powers ATR-based GTT levels.
     API: POST /market/v1/quote/  mode=FULL

  4. Price Gainers / Losers (optional, F&O universe only)
     ──────────────────────────────────────────────────────
     Top 10 NSE F&O price gainers and losers for the day — sector momentum.
     API: POST /marketData/v1/gainersLosers

All APIs use the same SmartConnect session (Angel One SmartAPI).

Usage (standalone):
  python india_market_internals.py --symbol NSE:RELIANCE
  python india_market_internals.py --pcr
  python india_market_internals.py --oi-buildup --expiry NEAR
  python india_market_internals.py --gainers

Usage (as library in signal_stack.py / india_enricher.py):
  from india_market_internals import IndiaMarketInternals
  mi = IndiaMarketInternals()
  data = mi.full_quote(["NSE:RELIANCE", "NSE:INFY"])
  pcr  = mi.pcr()
  oi   = mi.oi_buildup("Long Built Up", "NEAR")

Requirements:
  pip install smartapi-python pyotp requests
  ENV: ANGELONE_API_KEY (provided via environment)
  ENV: ANGELONE_CLIENT_CODE (provided via environment)
  ENV: ANGELONE_PIN (provided via environment)
  ENV: ANGELONE_TOTP_SECRET (provided via environment)
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import traceback
from datetime import datetime
from typing import Optional

# ─── Angel One credentials ────────────────────────────────────────────────────
AO_API_KEY      = os.environ.get("ANGELONE_API_KEY",     "")
AO_CLIENT_CODE  = os.environ.get("ANGELONE_CLIENT_CODE", "")
AO_PIN          = os.environ.get("ANGELONE_PIN",         "")
AO_TOTP_SECRET  = os.environ.get("ANGELONE_TOTP_SECRET", "")

# ─── Angel One instrument-token lookup (most common NSE equities) ──────────────
# Token IDs from Angel One instruments list. Only needed for market-data batch
# calls that require symbolToken. Extend as needed.
NSE_TOKEN_MAP: dict[str, str] = {
    "RELIANCE":    "2885",  "INFY":        "1594",  "TCS":          "11536",
    "HDFCBANK":    "1333",  "ICICIBANK":   "4963",  "SBIN":         "3045",
    "KOTAKBANK":   "1922",  "WIPRO":       "3787",  "AXISBANK":     "5900",
    "BHARTIARTL":  "10604", "HCLTECH":     "7229",  "LT":           "11483",
    "SUNPHARMA":   "3351",  "ADANIENT":    "25",    "ADANIPORTS":   "15083",
    "BAJFINANCE":  "317",   "BAJAJFINSV":  "16675", "MARUTI":       "10999",
    "NTPC":        "11630", "POWERGRID":   "14977", "ONGC":         "2475",
    "BPCL":        "526",   "IOC":         "1624",  "HINDUNILVR":   "1394",
    "NESTLEIND":   "17963", "BRITANNIA":   "547",   "DRREDDY":      "881",
    "DIVISLAB":    "10940", "CIPLA":       "694",   "APOLLOHOSP":   "1023",
    "TECHM":       "13538", "JSWSTEEL":    "11723", "TATASTEEL":    "3505",
    "HINDALCO":    "1363",  "ULTRACEMCO":  "11532", "GRASIM":       "1232",
    "SHREECEM":    "3103",  "AMBUJACEM":   "1270",  "TATAMOTORS":   "3456",
    "M&M":         "2031",  "EICHERMOT":   "910",   "HEROMOTOCO":   "1348",
    "BAJAJ-AUTO":  "16669", "TITAN":       "3506",  "ASIANPAINT":   "236",
    "PIDILITIND":  "2664",  "BERGEPAINT":  "404",   "SIEMENS":      "3273",
    "ABB":         "13",    "HAVELLS":     "1271",  "VOLTAS":       "3787",
    # india-brokers portfolio symbols
    "CAPACITE":    "18170", "SWSOLAR":     "17371", "GREAVESCOT":   "1209",
    "IFBIND":      "13979", "IMFA":        "1557",  "SHARDACROP":   "18171",
    "BEL":         "542",   "HAL":         "2303",  "IRCTC":        "13611",
    "RVNL":        "14977", "TIINDIA":     "13403",
}


def _resolve_token(symbol: str) -> Optional[str]:
    """Strip exchange prefix and return Angel One symbolToken, or None."""
    base = symbol.split(":")[-1].upper().replace("-EQ", "")
    return NSE_TOKEN_MAP.get(base)


# ─────────────────────────────────────────────────────────────────────────────
class IndiaMarketInternals:
    """
    Wraps three Angel One SmartAPI market data endpoints:
      • full_quote()   — LTP, OHLC, 52W, volume, depth, circuit
      • pcr()          — Put-Call Ratio per underlying
      • oi_buildup()   — Long/Short buildup classification
      • price_gainers_losers() — top price gainers / losers (F&O universe)

    Session is created once and reused (20-min TTL typical).
    """

    SESSION_TTL = 1200  # seconds before re-auth

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._smart  = None
        self._token  = None
        self._last_auth: float = 0.0
        self._errors: list[str] = []

    # ── Authentication ────────────────────────────────────────────────────────

    def _auth(self) -> bool:
        """Authenticate / refresh Angel One session. Returns True on success."""
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError:
            self._errors.append("pip install smartapi-python pyotp")
            return False
        import logging as _l; _l.getLogger("logzero_default").setLevel(_l.CRITICAL)
        try:
            totp  = pyotp.TOTP(AO_TOTP_SECRET).now()
            smart = SmartConnect(api_key=AO_API_KEY)
            resp  = smart.generateSession(AO_CLIENT_CODE, AO_PIN, totp)
            if resp.get("status") and resp.get("data", {}).get("jwtToken"):
                self._smart     = smart
                self._token     = resp["data"]["jwtToken"].removeprefix("Bearer ").strip()
                self._last_auth = time.time()
                return True
            self._errors.append(f"Angel One auth failed: {resp.get('message','')}")
            return False
        except Exception as exc:
            self._errors.append(f"Angel One auth exception: {exc}")
            return False

    def _ensure_auth(self) -> bool:
        if time.time() - self._last_auth > self.SESSION_TTL or self._smart is None:
            return self._auth()
        return True

    # ── Request helper (direct REST, bypasses SmartConnect SDK quirks) ────────

    def _post(self, path: str, body: dict) -> dict:
        """POST to Angel One REST API with current JWT."""
        import urllib.request
        url = f"https://apiconnect.angelone.in{path}"
        headers = {
            "X-PrivateKey":      AO_API_KEY,
            "Accept":            "application/json",
            "X-SourceID":        "WEB",
            "X-ClientLocalIP":   "127.0.0.1",
            "X-ClientPublicIP":  "127.0.0.1",
            "X-MACAddress":      "AA:BB:CC:DD:EE:FF",
            "X-UserType":        "USER",
            "Authorization":     f"Bearer {self._token}",
            "Content-Type":      "application/json",
        }
        data = json.dumps(body).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"status": False, "message": str(exc), "data": None}

    def _get(self, path: str) -> dict:
        """GET to Angel One REST API with current JWT."""
        import urllib.request
        url = f"https://apiconnect.angelone.in{path}"
        headers = {
            "X-PrivateKey":      AO_API_KEY,
            "Accept":            "application/json",
            "X-SourceID":        "WEB",
            "X-ClientLocalIP":   "127.0.0.1",
            "X-ClientPublicIP":  "127.0.0.1",
            "X-MACAddress":      "AA:BB:CC:DD:EE:FF",
            "X-UserType":        "USER",
            "Authorization":     f"Bearer {self._token}",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"status": False, "message": str(exc), "data": None}

    # ── 1. FULL Market Data ────────────────────────────────────────────────────

    def full_quote(self, tv_symbols: list[str]) -> dict[str, dict]:
        """
        Fetch FULL market data (LTP, OHLC, 52W high/low, volume, depth, circuit)
        for up to 50 symbols in one call.

        Returns dict: { "NSE:INFY": { "ltp": 1590.5, "52w_high": ..., ... }, ... }
        Missing tokens fall back to empty dict.
        """
        if not self._ensure_auth():
            return {}
        # Build token map per exchange
        nse_tokens: list[str] = []
        token_to_sym: dict[str, str] = {}
        for sym in tv_symbols:
            tok = _resolve_token(sym)
            if tok:
                nse_tokens.append(tok)
                token_to_sym[tok] = sym
        if not nse_tokens:
            return {}
        # Angel One allows 50 tokens per request
        results: dict[str, dict] = {}
        _reauthed = False
        for i in range(0, len(nse_tokens), 50):
            chunk = nse_tokens[i:i + 50]
            resp = self._post(
                "/rest/secure/angelbroking/market/v1/quote/",
                {"mode": "FULL", "exchangeTokens": {"NSE": chunk}},
            )
            if not resp.get("status"):
                msg = str(resp.get("message") or resp.get("errorcode") or resp)
                # Angel One returns "Invalid Token" when the JWT has expired server-side
                # (common after market hours or long inactivity). Force re-auth and retry once.
                if not _reauthed and any(
                    kw in msg.lower() for kw in ("invalid token", "unauthorized", "session expired", "ab1010", "ag8002")
                ):
                    print("  [auth] Session expired — re-authenticating Angel One…")
                    self._last_auth = 0  # force _ensure_auth to re-auth
                    if self._auth():
                        _reauthed = True
                        resp = self._post(
                            "/rest/secure/angelbroking/market/v1/quote/",
                            {"mode": "FULL", "exchangeTokens": {"NSE": chunk}},
                        )
                        if resp.get("status"):
                            # fall through to parse the good response below
                            pass
                        else:
                            msg = str(resp.get("message") or resp.get("errorcode") or resp)
                if not resp.get("status"):
                    self._errors.append(f"Quote API error: {msg}")
                    if self.verbose:
                        print(f"  [debug] Quote API raw response: {json.dumps(resp)[:300]}")
                    continue
            for item in (resp.get("data") or {}).get("fetched") or []:
                tok = str(item.get("symbolToken", ""))
                sym = token_to_sym.get(tok, f"NSE:{item.get('tradingSymbol','?')}")
                results[sym] = {
                    "ltp":            item.get("ltp"),
                    "open":           item.get("open"),
                    "high":           item.get("high"),
                    "low":            item.get("low"),
                    "close":          item.get("close"),        # prev close
                    "volume":         item.get("tradeVolume"),
                    "net_change":     item.get("netChange"),
                    "pct_change":     item.get("percentChange"),
                    "52w_high":       item.get("52WeekHigh"),
                    "52w_low":        item.get("52WeekLow"),
                    "upper_circuit":  item.get("upperCircuit"),
                    "lower_circuit":  item.get("lowerCircuit"),
                    "tot_buy_qty":    item.get("totBuyQuan"),
                    "tot_sell_qty":   item.get("totSellQuan"),
                    "avg_price":      item.get("avgPrice"),
                    "symbol_token":   tok,
                }
        return results

    def w52_position(self, quote: dict) -> Optional[float]:
        """Return 0–1 position within 52-week range from a full_quote dict."""
        hi  = quote.get("52w_high")
        lo  = quote.get("52w_low")
        cur = quote.get("ltp") or quote.get("close")
        if hi and lo and cur and hi > lo:
            return round((cur - lo) / (hi - lo), 3)
        return None

    def buy_sell_ratio(self, quote: dict) -> Optional[float]:
        """
        Buy quantity / sell quantity ratio from order book depth.
        > 1.0 → more buy orders → bullish order imbalance
        < 1.0 → more sell orders → bearish
        """
        b = quote.get("tot_buy_qty") or 0
        s = quote.get("tot_sell_qty") or 0
        if s > 0:
            return round(b / s, 3)
        return None

    # ── 2. PCR (Put-Call Ratio) ────────────────────────────────────────────────

    def pcr(self) -> list[dict]:
        """
        Fetch Put-Call Ratio for all NSE F&O underlyings.

        Returns list of { "symbol": "NIFTY", "pcr": 1.04 }.
        PCR < 0.8 → bullish, > 1.2 → bearish.
        """
        if not self._ensure_auth():
            return []
        resp = self._get(
            "/rest/secure/angelbroking/marketData/v1/putCallRatio"
        )
        results = []
        for item in (resp.get("data") or []):
            sym = str(item.get("tradingSymbol", "")).replace("25JAN24FUT", "").replace("FUT", "")
            # Angel One maps to futures symbol — strip expiry/FUT suffix
            # e.g. "NIFTY25JAN24FUT" → "NIFTY"
            import re
            sym = re.sub(r"\d{2}[A-Z]{3}\d{2}F?U?T?$", "", sym).strip()
            pcr_val = item.get("pcr")
            if sym and pcr_val is not None:
                results.append({
                    "symbol": sym,
                    "pcr":    round(float(pcr_val), 3),
                    "label":  (
                        "Very Bullish" if float(pcr_val) < 0.5 else
                        "Bullish"      if float(pcr_val) < 0.8 else
                        "Neutral"      if float(pcr_val) < 1.2 else
                        "Bearish"      if float(pcr_val) < 1.5 else "Very Bearish"
                    ),
                })
        return results

    def pcr_for(self, symbol: str) -> Optional[dict]:
        """Return PCR dict for a specific NSE symbol (or None if not in F&O)."""
        base = symbol.split(":")[-1].upper().replace("-EQ", "")
        for item in self.pcr():
            if item["symbol"].upper().startswith(base):
                return item
        return None

    # ── 3. OI Buildup ──────────────────────────────────────────────────────────

    BUILDUP_TYPES = ("Long Built Up", "Short Built Up", "Short Covering", "Long Unwinding")
    EXPIRY_TYPES  = ("NEAR", "NEXT", "FAR")

    def oi_buildup(self, datatype: str = "Long Built Up",
                   expirytype: str = "NEAR") -> list[dict]:
        """
        Fetch OI Buildup signal for NSE F&O stocks.
        datatype: "Long Built Up" | "Short Built Up" | "Short Covering" | "Long Unwinding"
        expirytype: "NEAR" | "NEXT" | "FAR"

        Returns list of { "symbol", "ltp", "pct_change", "oi", "oi_change",
                           "buildup_type", "bullish" }.
        """
        if datatype not in self.BUILDUP_TYPES:
            raise ValueError(f"datatype must be one of {self.BUILDUP_TYPES}")
        if expirytype not in self.EXPIRY_TYPES:
            raise ValueError(f"expirytype must be one of {self.EXPIRY_TYPES}")
        if not self._ensure_auth():
            return []
        resp = self._post(
            "/rest/secure/angelbroking/marketData/v1/OIBuildup",
            {"expirytype": expirytype, "datatype": datatype},
        )
        import re
        results = []
        for item in (resp.get("data") or []):
            raw_sym = str(item.get("tradingSymbol", ""))
            sym = re.sub(r"\d{2}[A-Z]{3}\d{2}F?U?T?$", "", raw_sym).strip()
            results.append({
                "symbol":       sym,
                "ltp":          _safe_float(item.get("ltp")),
                "pct_change":   _safe_float(item.get("percentChange")),
                "oi":           _safe_float(item.get("opnInterest")),
                "oi_change":    _safe_float(item.get("netChangeOpnInterest")),
                "buildup_type": datatype,
                # Bullish: long buildup or short covering → money flowing in / shorts exit
                "bullish":      datatype in ("Long Built Up", "Short Covering"),
            })
        return results

    def oi_buildup_for(self, symbol: str, expirytype: str = "NEAR") -> Optional[dict]:
        """
        Return OI buildup classification for a single NSE symbol.
        Checks all four buildup types and returns the first match.
        Returns None if symbol not in F&O list.
        """
        base = symbol.split(":")[-1].upper().replace("-EQ", "")
        for btype in self.BUILDUP_TYPES:
            for item in self.oi_buildup(btype, expirytype):
                if item["symbol"].upper().startswith(base):
                    return item
        return None

    # ── 4. Price Gainers / Losers (F&O universe) ──────────────────────────────

    def price_gainers_losers(self, datatype: str = "PercPriceGainers",
                             expirytype: str = "NEAR") -> list[dict]:
        """
        Top NSE F&O price gainers or losers for the day.
        datatype: "PercPriceGainers" | "PercPriceLosers"
        """
        if not self._ensure_auth():
            return []
        resp = self._post(
            "/rest/secure/angelbroking/marketData/v1/gainersLosers",
            {"datatype": datatype, "expirytype": expirytype},
        )
        import re
        results = []
        for item in (resp.get("data") or []):
            raw_sym = str(item.get("tradingSymbol", ""))
            sym = re.sub(r"\d{2}[A-Z]{3}\d{2}F?U?T?$", "", raw_sym).strip()
            results.append({
                "symbol":      sym,
                "pct_change":  _safe_float(item.get("percentChange")),
                "oi":          _safe_float(item.get("opnInterest")),
                "oi_change":   _safe_float(item.get("netChangeOpnInterest")),
            })
        return results

    # ── 5. FII / DII Flow (NSE free API, no Angel One auth needed) ──────────

    def fetch_fii_dii_flow(self) -> Optional[dict]:
        """
        Fetch today's FII & DII net buy/sell from NSE.
        Returns {fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net, regime}
        or None on failure.

        regime:
          "bullish"  — FII net buy > 0  (foreign institutions buying)
          "bearish"  — FII net sell (foreign institutions exiting)
          "neutral"  — near zero
        """
        import urllib.request
        try:
            # NSE requires a browser User-Agent and cookies from homepage
            opener = urllib.request.build_opener()
            opener.addheaders = [
                ("User-Agent",
                 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/120.0.0.0 Safari/537.36"),
                ("Accept", "application/json"),
                ("Accept-Language", "en-US,en;q=0.9"),
            ]
            # Step 1: visit NSE homepage to get cookies
            opener.open("https://www.nseindia.com", timeout=10)
            # Step 2: fetch FII/DII data
            resp = opener.open(
                "https://www.nseindia.com/api/fiidiiTradeReact", timeout=10
            )
            data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if self.verbose:
                print(f"  [debug] NSE FII/DII fetch failed: {exc}")
            return None

        # Parse the response — NSE returns a list with category rows
        fii_buy = fii_sell = dii_buy = dii_sell = 0.0
        for row in (data if isinstance(data, list) else []):
            cat = str(row.get("category", "")).upper()
            buy  = _safe_float(row.get("buyValue")) or 0.0
            sell = _safe_float(row.get("sellValue")) or 0.0
            if "FII" in cat or "FPI" in cat:
                fii_buy += buy
                fii_sell += sell
            elif "DII" in cat:
                dii_buy += buy
                dii_sell += sell

        fii_net = round(fii_buy - fii_sell, 2)
        dii_net = round(dii_buy - dii_sell, 2)

        if fii_net > 100:
            regime = "bullish"
        elif fii_net < -100:
            regime = "bearish"
        else:
            regime = "neutral"

        return {
            "fii_buy": round(fii_buy, 2), "fii_sell": round(fii_sell, 2),
            "fii_net": fii_net,
            "dii_buy": round(dii_buy, 2), "dii_sell": round(dii_sell, 2),
            "dii_net": dii_net,
            "regime": regime,
        }

    def fii_dii_modifier(self) -> float:
        """
        Return a modifier for India L4 scores based on FII/DII flow.
          FII net buy  → +0.5  (institutional tailwind)
          FII net sell → -0.5  (institutional headwind)
          Neutral / unavailable → 0.0
        """
        flow = self.fetch_fii_dii_flow()
        if flow is None:
            return 0.0
        regime = flow.get("regime", "neutral")
        if regime == "bullish":
            return 0.5
        if regime == "bearish":
            return -0.5
        return 0.0

    # ── 6. MWPL (Market-Wide Position Limit) ─────────────────────────────────

    def fetch_mwpl(self) -> list[dict]:
        """
        Fetch stocks nearing or at MWPL ban from NSE.
        MWPL > 85%  → approaching ban (short-squeeze / liquidity risk).
        MWPL = 100% → banned for new F&O positions.

        Returns list of {symbol, mwpl_pct, status} or empty list on failure.
        """
        import urllib.request
        try:
            opener = urllib.request.build_opener()
            opener.addheaders = [
                ("User-Agent",
                 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/120.0.0.0 Safari/537.36"),
                ("Accept", "application/json"),
                ("Accept-Language", "en-US,en;q=0.9"),
            ]
            opener.open("https://www.nseindia.com", timeout=10)
            resp = opener.open(
                "https://www.nseindia.com/api/live-analysis-mwpl", timeout=10
            )
            data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if self.verbose:
                print(f"  [debug] NSE MWPL fetch failed: {exc}")
            return []

        results = []
        for row in (data.get("data") if isinstance(data, dict) else data) or []:
            sym = str(row.get("tradingSymbol") or row.get("symbol") or "")
            pct = _safe_float(row.get("mwpl") or row.get("mwplPct"))
            if sym and pct is not None:
                status = (
                    "banned" if pct >= 95.0
                    else "approaching_ban" if pct >= 85.0
                    else "caution" if pct >= 70.0
                    else "normal"
                )
                results.append({"symbol": sym, "mwpl_pct": round(pct, 2),
                                "status": status})
        return results

    def mwpl_check(self, symbol: str) -> Optional[dict]:
        """
        Check MWPL status for a single NSE symbol.
        Returns {symbol, mwpl_pct, status, penalty} or None if not in MWPL list.
        penalty: -5 if approaching_ban/banned, -2 if caution, 0 if normal.
        """
        base = symbol.split(":")[-1].upper().replace("-EQ", "")
        for item in self.fetch_mwpl():
            if base in item["symbol"].upper():
                penalty = (
                    -5.0 if item["status"] in ("approaching_ban", "banned")
                    else -2.0 if item["status"] == "caution"
                    else 0.0
                )
                return {**item, "penalty": penalty}
        return None

    # ── Convenience: enrich a single symbol ──────────────────────────────────

    def enrich_symbol(self, tv_symbol: str) -> dict:
        """
        Return a summary dict for one symbol combining all three sources:
          • full_quote  → ltp, 52w_position, buy_sell_ratio
          • pcr         → pcr value + label
          • oi_buildup  → buildup type + bullish flag

        This is the hook called by india_enricher.py for market context.
        """
        result: dict = {"symbol": tv_symbol, "source": "angel_one"}
        try:
            quotes = self.full_quote([tv_symbol])
            q = quotes.get(tv_symbol, {})
            result["ltp"]            = q.get("ltp")
            result["52w_position"]   = self.w52_position(q)
            result["buy_sell_ratio"] = self.buy_sell_ratio(q)
            result["pct_change"]     = q.get("pct_change")
            result["volume"]         = q.get("volume")
            result["upper_circuit"]  = q.get("upper_circuit")
            result["lower_circuit"]  = q.get("lower_circuit")
        except Exception as exc:
            result["quote_error"] = str(exc)
        try:
            pcr_data = self.pcr_for(tv_symbol)
            if pcr_data:
                result["pcr"] = pcr_data["pcr"]
                result["pcr_label"] = pcr_data["label"]
        except Exception as exc:
            result["pcr_error"] = str(exc)
        try:
            oi_data = self.oi_buildup_for(tv_symbol)
            if oi_data:
                result["oi_buildup_type"] = oi_data["buildup_type"]
                result["oi_bullish"]      = oi_data["bullish"]
                result["oi_pct_change"]   = oi_data["pct_change"]
                result["oi_change"]       = oi_data["oi_change"]
        except Exception as exc:
            result["oi_error"] = str(exc)
        # FII/DII flow (market-wide, cached per call)
        try:
            fii_dii = self.fetch_fii_dii_flow()
            if fii_dii:
                result["fii_dii_regime"]   = fii_dii["regime"]
                result["fii_net"]          = fii_dii["fii_net"]
                result["dii_net"]          = fii_dii["dii_net"]
                result["fii_dii_modifier"] = self.fii_dii_modifier()
        except Exception as exc:
            result["fii_dii_error"] = str(exc)
        # MWPL check (symbol-specific)
        try:
            mwpl = self.mwpl_check(tv_symbol)
            if mwpl:
                result["mwpl_pct"]     = mwpl["mwpl_pct"]
                result["mwpl_status"]  = mwpl["status"]
                result["mwpl_penalty"] = mwpl["penalty"]
        except Exception as exc:
            result["mwpl_error"] = str(exc)
        return result

    def mi_score(self, symbol_data: dict) -> float:
        """
        Compute a 0-10 market-internals score from enrich_symbol() output.
        Mirrors the logic in signal_stack.py score_market_internals() but
        uses India-specific data instead of US yfinance data.

        Components (same weights as L4 PCR + 52W):
          PCR      30%: < 0.5=10, 0.5-0.8=8, 0.8-1.2=5, 1.2-1.5=3, >1.5=1
          52W pos  25%: upper range = 10, lower range = 2
          B/S ratio25%: > 1.5=10, 1.0-1.5=7, 0.7-1.0=4, < 0.7=1
          OI type  20%: Long Buildup/Short Covering +8, else neutral 5
        """
        # PCR score
        pcr = symbol_data.get("pcr")
        if pcr is None:
            s_pcr = 5.0
        elif pcr < 0.5:  s_pcr = 10.0
        elif pcr < 0.8:  s_pcr = 8.0
        elif pcr < 1.2:  s_pcr = 5.0
        elif pcr < 1.5:  s_pcr = 3.0
        else:             s_pcr = 1.0

        # 52W position score
        w52 = symbol_data.get("52w_position")
        if w52 is None:
            s_w52 = 5.0
        elif 0.50 <= w52 <= 0.85:   s_w52 = 10.0
        elif 0.85 < w52 <= 0.95:    s_w52 = 8.0
        elif w52 > 0.95:            s_w52 = 5.0
        elif 0.30 <= w52 < 0.50:    s_w52 = 7.0
        elif 0.15 <= w52 < 0.30:    s_w52 = 4.0
        else:                        s_w52 = 2.0

        # Buy/sell ratio score
        bsr = symbol_data.get("buy_sell_ratio")
        if bsr is None:
            s_bsr = 5.0
        elif bsr >= 1.5: s_bsr = 10.0
        elif bsr >= 1.0: s_bsr =  7.0
        elif bsr >= 0.7: s_bsr =  4.0
        else:             s_bsr =  1.0

        # OI buildup score
        oi_bullish = symbol_data.get("oi_bullish")
        if oi_bullish is None:
            s_oi = 5.0
        elif oi_bullish:
            s_oi = 8.0
        else:
            s_oi = 3.0

        total = round(s_pcr * 0.30 + s_w52 * 0.25 + s_bsr * 0.25 + s_oi * 0.20, 2)
        return total


# ─── Utilities ────────────────────────────────────────────────────────────────

def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _print_quote(sym: str, q: dict) -> None:
    """Pretty-print a full quote dict to stdout."""
    ltp = q.get("ltp", "N/A")
    pct = q.get("pct_change", 0) or 0
    arrow = "▲" if pct >= 0 else "▼"
    color_code = "\033[32m" if pct >= 0 else "\033[31m"
    reset = "\033[0m"
    print(f"  {sym:<25} LTP: {ltp:>10}  {color_code}{arrow} {pct:+.2f}%{reset}  "
          f"Vol: {q.get('volume','N/A'):>12}  "
          f"52W: {q.get('52w_low','?'):.1f} – {q.get('52w_high','?'):.1f}  "
          f"B/S: {q.get('tot_buy_qty','?')} / {q.get('tot_sell_qty','?')}")


def _print_pcr(items: list[dict]) -> None:
    print(f"\n{'Symbol':<15}{'PCR':>8}  {'Sentiment':>14}")
    print("─" * 40)
    for item in sorted(items, key=lambda x: x["pcr"]):
        sym = item["symbol"][:14]
        pcr = item["pcr"]
        lbl = item["label"]
        color = "\033[32m" if pcr < 0.8 else ("\033[31m" if pcr > 1.2 else "\033[33m")
        reset = "\033[0m"
        print(f"  {sym:<14}{pcr:>8.3f}  {color}{lbl}{reset}")


def _print_oi_buildup(items: list[dict], datatype: str) -> None:
    print(f"\n  OI Buildup: {datatype}")
    print(f"  {'Symbol':<15}{'LTP':>10}{'%Chg':>8}{'OI':>15}{'OI Chg':>15}")
    print("  " + "─" * 65)
    for item in items[:15]:
        print(f"  {item['symbol']:<15}"
              f"{str(item.get('ltp','?')):>10}"
              f"{str(item.get('pct_change','?')):>8}"
              f"{str(item.get('oi','?')):>15}"
              f"{str(item.get('oi_change','?')):>15}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="India Market Internals — Angel One SmartAPI"
    )
    parser.add_argument("--symbol",    type=str, default="NSE:RELIANCE",
                        help="TV symbol for full quote + PCR + OI lookup")
    parser.add_argument("--pcr",       action="store_true",
                        help="Show Put-Call Ratio for all NSE F&O stocks")
    parser.add_argument("--oi-buildup",action="store_true",
                        help="Show OI Buildup table for selected type")
    parser.add_argument("--buildup-type", type=str, default="Long Built Up",
                        help="OI buildup data type")
    parser.add_argument("--expiry",    type=str, default="NEAR",
                        choices=["NEAR", "NEXT", "FAR"])
    parser.add_argument("--gainers",   action="store_true",
                        help="Show top NSE F&O price gainers")
    parser.add_argument("--losers",    action="store_true",
                        help="Show top NSE F&O price losers")
    parser.add_argument("--all",       action="store_true",
                        help="Run all queries for the symbol")
    parser.add_argument("--debug",     action="store_true",
                        help="Print raw API responses for troubleshooting")
    args = parser.parse_args()

    mi = IndiaMarketInternals(verbose=args.debug)
    print(f"\n\033[1m\033[36mIndia Market Internals  —  {args.symbol}\033[0m")

    # Always show full quote for the symbol
    print(f"\n\033[1m[1] Live Market Data (FULL mode)\033[0m")
    quotes = mi.full_quote([args.symbol])
    q = quotes.get(args.symbol, {})
    if q:
        _print_quote(args.symbol, q)
        w52 = mi.w52_position(q)
        bsr = mi.buy_sell_ratio(q)
        print(f"       52W position: {w52 * 100:.1f}% of range" if w52 else "")
        print(f"       Buy/Sell Qty ratio: {bsr:.3f}" if bsr else "")
    else:
        base = args.symbol.split(":")[-1].upper().replace("-EQ", "")
        tok  = NSE_TOKEN_MAP.get(base)
        if not tok:
            print(f"  ⚠  '{base}' not found in NSE_TOKEN_MAP.")
            print(f"  Tip: Add '{base}: \"<token>\"' to NSE_TOKEN_MAP in india_market_internals.py")
        else:
            print(f"  ⚠  API returned no data for {args.symbol} (token {tok}).")
            # Show captured errors so the user sees the real reason
            if mi._errors:
                for e in mi._errors:
                    print(f"     Error: {e}")
                mi._errors.clear()   # already shown, don't repeat at the end
            else:
                print(f"     Tip: run with --debug to see raw API response")
                print(f"     Re-run: python india_market_internals.py --symbol {args.symbol} --debug")

    if args.pcr or args.all:
        print(f"\n\033[1m[2] Put-Call Ratio (all NSE F&O)\033[0m")
        pcr_data = mi.pcr()
        if pcr_data:
            sym_pcr = mi.pcr_for(args.symbol)
            if sym_pcr:
                print(f"  {args.symbol}  PCR={sym_pcr['pcr']:.3f}  {sym_pcr['label']}")
            print(f"  (showing top 20 by PCR):")
            _print_pcr(pcr_data[:20])
        else:
            print(f"  ⚠  PCR data unavailable")

    if args.oi_buildup or args.all:
        dtype = args.buildup_type
        print(f"\n\033[1m[3] OI Buildup — {dtype} ({args.expiry})\033[0m")
        oi = mi.oi_buildup(dtype, args.expiry)
        if oi:
            _print_oi_buildup(oi, dtype)
            # Check if the queried symbol appears
            base = args.symbol.split(":")[-1]
            sym_oi = next((x for x in oi if base.upper() in x["symbol"].upper()), None)
            if sym_oi:
                print(f"\n  ✓ {args.symbol} found in {dtype}:"
                      f" ltp={sym_oi['ltp']} pct={sym_oi['pct_change']}%"
                      f" oi={sym_oi['oi']} oi_chg={sym_oi['oi_change']}")
        else:
            print(f"  ⚠  OI Buildup data unavailable for {dtype}")

    if args.gainers or args.all:
        print(f"\n\033[1m[4] Top Price Gainers (F&O, {args.expiry})\033[0m")
        grs = mi.price_gainers_losers("PercPriceGainers", args.expiry)
        for item in grs[:10]:
            print(f"  {item['symbol']:<15} {item.get('pct_change','?'):>+6.1f}%"
                  f"  OI: {item.get('oi','?')}")

    if args.losers:
        print(f"\n\033[1m[4b] Top Price Losers (F&O, {args.expiry})\033[0m")
        lrs = mi.price_gainers_losers("PercPriceLosers", args.expiry)
        for item in lrs[:10]:
            print(f"  {item['symbol']:<15} {item.get('pct_change','?'):>+6.1f}%"
                  f"  OI: {item.get('oi','?')}")

    if args.all:
        print(f"\n\033[1m[5] Composite MI Score for {args.symbol}\033[0m")
        enriched = mi.enrich_symbol(args.symbol)
        score = mi.mi_score(enriched)
        print(f"  PCR={enriched.get('pcr','N/A')}  52W={enriched.get('52w_position','N/A')}"
              f"  B/S={enriched.get('buy_sell_ratio','N/A')}"
              f"  OI Buildup={enriched.get('oi_buildup_type','N/A')}")
        print(f"  → India MI Score: \033[1m{score:.1f} / 10\033[0m")

    if mi._errors:
        print(f"\n\033[33m⚠  Errors:\033[0m")
        for e in mi._errors:
            print(f"   {e}")


if __name__ == "__main__":
    main()
