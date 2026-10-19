#!/usr/bin/env python3
"""
IBKR TWS Enricher — L6 data layer for signal_stack.py
======================================================
Connects to a running TWS or IB Gateway instance and retrieves:

  1. News sentiment    — reqHistoricalNews with free providers (BRFG, BRFUPDN, DJNL)
                         → VADER score on headlines
  2. Fundamental ratios— reqMktData tick 258 (PE, EPS growth, P/Book … live from IBKR)
  3. Option stats      — reqMktData tick 100+101 (option vol + OI → PCR)
  4. Market scanner    — reqScannerSubscription (TOP_PERC_GAIN, HOT_BY_VOLUME, PCR scan)
  5. conId lookup      — reqMatchingSymbols (needed for news + option calls)

Requirements:
    pip install ibapi vaderSentiment
    TWS must be running on port 7496 (or IB Gateway on 4001).
    In TWS: Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients ☑

Usage (standalone probe):
    python ibkr_enricher.py --probe AAPL MSFT NVDA

Wire-in (signal_stack.py):
    from ibkr_enricher import compute_l6_score
    l6 = compute_l6_score("AAPL")          # returns dict with l6_score (0–10) or None
"""

import os
import threading
import time
import math
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Structured logging ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from path_config import ensure_paths, ROOT
ensure_paths(ROOT / "tradingview")

try:
    from log_config import get_logger as _get_logger
    _log = _get_logger(__name__)
except Exception:
    import logging as _logging
    _log = _logging.getLogger(__name__)

# ─── Optional deps ─────────────────────────────────────────────────────────────
try:
    from ibapi.client  import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.scanner  import ScannerSubscription
    _IBAPI_AVAILABLE = True
except ImportError:
    _IBAPI_AVAILABLE = False
    # Stubs so module-level class definitions don't blow up with NameError
    class EWrapper:  # type: ignore[no-redef]
        pass
    class EClient:   # type: ignore[no-redef]
        def __init__(self, wrapper=None): pass
    class Contract:  # type: ignore[no-redef]
        pass
    class ScannerSubscription:  # type: ignore[no-redef]
        pass

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _VADER = SentimentIntensityAnalyzer()
    def vader_compound(text: str) -> float:
        return _VADER.polarity_scores(text)["compound"]
except ImportError:
    def vader_compound(text: str) -> float:
        return 0.0

# ─── Config ────────────────────────────────────────────────────────────────────
IBKR_HOST    = os.environ.get("IBKR_HOST",  "127.0.0.1")
IBKR_PORT    = int(os.environ.get("IBKR_PORT", "7496"))   # TWS live=7496, paper=7497
CONNECT_TMO  = 10    # seconds to wait for TWS handshake
REQUEST_TMO  = 12    # seconds to wait for a data response

# Free providers (enabled by default in all IBKR accounts since TWS v966)
FREE_NEWS_PROVIDERS = "BRFG:BRFUPDN:DJNL"

# ─── Process-level caches ──────────────────────────────────────────────────────
_CONID_CACHE:    dict[str, int]  = {}   # symbol → conId
_L6_CACHE:       dict[str, dict] = {}   # symbol → l6 result

# ─── Shared TWS app base class ─────────────────────────────────────────────────
class _TWSBase(EWrapper, EClient):
    """Minimal EWrapper/EClient base — subclass and override callbacks."""
    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._connected = threading.Event()
        self._next_req_id = 1

    # ── Connection handshake ───────────────────────────────────────────────────
    def nextValidId(self, orderId: int):
        self._next_req_id = orderId
        self._connected.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Suppress informational messages (codes 2104/2106/2158 are normal)
        if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
            print(f"  [IBKREnricher] error reqId={reqId} code={errorCode}: {errorString}")
            _log.warning("ibkr_tws_error", req_id=reqId, code=errorCode, msg=errorString)

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _connect_and_wait(self) -> bool:
        self.connect(IBKR_HOST, IBKR_PORT, clientId=77)
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return self._connected.wait(timeout=CONNECT_TMO)

    def _req_id(self) -> int:
        rid = self._next_req_id
        self._next_req_id += 1
        return rid


# ─── 1. ConId lookup ───────────────────────────────────────────────────────────
def get_conid(symbol: str, currency: str = "USD", exchange: str = "SMART") -> int | None:
    """Return the primary conId for a US stock symbol. Results are cached."""
    if not _IBAPI_AVAILABLE:
        return None
    if symbol in _CONID_CACHE:
        return _CONID_CACHE[symbol]

    result: dict = {}
    done = threading.Event()

    class _App(_TWSBase):
        def symbolSamples(self, reqId, contractDescriptions):
            for cd in contractDescriptions:
                c = cd.contract
                if (c.secType == "STK"
                        and c.symbol.upper() == symbol.upper()
                        and c.currency == currency):
                    result["conid"] = c.conId
                    break
            done.set()

        def error(inner_self, reqId, errorCode, errorString,
                  advancedOrderRejectJson=""):
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
            if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
                done.set()

    app = _App()
    try:
        if not app._connect_and_wait():
            return None
        rid = app._req_id()
        app.reqMatchingSymbols(rid, symbol)
        done.wait(timeout=REQUEST_TMO)
    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    conid = result.get("conid")
    if conid:
        _CONID_CACHE[symbol] = conid
    return conid


# ─── 2. Historical News + VADER sentiment ─────────────────────────────────────
def fetch_news_sentiment(symbol: str, conid: int) -> dict:
    """
    Fetch up to 20 headlines from free IBKR news providers for the last 5 days.
    Returns {
      "score":      float,   # VADER compound avg  (-1 to +1)
      "headlines":  list[str],
      "count":      int,
      "provider_counts": dict
    }
    """
    if not _IBAPI_AVAILABLE or conid is None:
        return {}

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S.0")
    end_str   = end.strftime("%Y-%m-%d %H:%M:%S.0")

    headlines:   list[str] = []
    prov_counts: dict[str, int] = {}
    done = threading.Event()

    class _App(_TWSBase):
        def historicalNews(self_inner, reqId, time_ts, providerCode, articleId,
                           headline, extraData):
            headlines.append(headline)
            prov_counts[providerCode] = prov_counts.get(providerCode, 0) + 1

        def historicalNewsEnd(self_inner, reqId, hasMore):
            done.set()

        def error(self_inner, reqId, errorCode, errorString,
                  advancedOrderRejectJson=""):
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
            # News not subscribed — treat as empty, don't crash
            if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
                done.set()

    app = _App()
    try:
        if not app._connect_and_wait():
            return {}
        rid = app._req_id()
        app.reqHistoricalNews(rid, conid, FREE_NEWS_PROVIDERS,
                              start_str, end_str, 20, [])
        done.wait(timeout=REQUEST_TMO)
    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    if not headlines:
        return {}

    scores = [vader_compound(h) for h in headlines]
    avg_score = sum(scores) / len(scores)
    return {
        "score":          round(avg_score, 4),
        "headlines":      headlines,
        "count":          len(headlines),
        "provider_counts": prov_counts,
    }


# ─── 3. Fundamental Ratios (tick 258) ─────────────────────────────────────────
def fetch_fundamental_ratios(symbol: str, conid: int) -> dict:
    """
    Fetch Fundamental Ratios via reqMktData genericTickList "258".
    Returns a dict with fields like pe_ratio, eps, div_yield, book_value, etc.

    These are Bloomberg-sourced ratios embedded in the TWS market data feed
    and do NOT require a separate Reuters/Fundamentals subscription.
    """
    if not _IBAPI_AVAILABLE or conid is None:
        return {}

    ratios: dict = {}
    done = threading.Event()

    class _App(_TWSBase):
        def tickString(self_inner, tickerId, tickType, value):
            # tickType 47 = Fundamental Ratios (type code in ibapi EWrapper)
            if tickType == 47 and value:
                # value is semicolon-separated key=value pairs
                # e.g. "NPRICE=150.5;NHIG=200;NLOW=100;PDATE=20260405;..."
                for kv in value.split(";"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        try:
                            ratios[k] = float(v)
                        except ValueError:
                            ratios[k] = v
                done.set()

        def tickSnapshotEnd(self_inner, reqId):
            done.set()

        def error(self_inner, reqId, errorCode, errorString,
                  advancedOrderRejectJson=""):
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
            if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
                done.set()

    contract = Contract()
    contract.conId    = conid
    contract.secType  = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.symbol   = symbol

    app = _App()
    try:
        if not app._connect_and_wait():
            return {}
        rid = app._req_id()
        # snapshot=True = one-time, no subscription (won't count against L1 limits)
        app.reqMktData(rid, contract, "258", True, False, [])
        done.wait(timeout=REQUEST_TMO)
        try:
            app.cancelMktData(rid)
        except Exception:
            pass
    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    return ratios


# ─── 4. Option Volume / OI → PCR (tick 100, 101) ─────────────────────────────
def fetch_option_stats(symbol: str, conid: int) -> dict:
    """
    Fetch option call/put volume + open interest via reqMktData ticks 100, 101.
    Returns {
      "call_option_volume": float,
      "put_option_volume":  float,
      "call_open_interest": float,
      "put_open_interest":  float,
      "pcr_volume":         float,  # put/call ratio by volume
      "pcr_oi":             float,  # put/call ratio by OI
    }
    PCR < 0.7 = bullish (more calls than puts)
    PCR > 1.3 = bearish (more puts than calls)
    """
    if not _IBAPI_AVAILABLE or conid is None:
        return {}

    ticks: dict = {}
    done = threading.Event()

    class _App(_TWSBase):
        def tickSize(self_inner, tickerId, tickType, size):
            # Tick types: 27=callOI, 28=putOI, 100=callOptVol, 101=putOptVol (Python ibapi)
            ticks[tickType] = size

        def tickSnapshotEnd(self_inner, reqId):
            done.set()

        def error(self_inner, reqId, errorCode, errorString,
                  advancedOrderRejectJson=""):
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
            if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
                done.set()

    contract = Contract()
    contract.conId    = conid
    contract.secType  = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.symbol   = symbol

    app = _App()
    try:
        if not app._connect_and_wait():
            return {}
        rid = app._req_id()
        app.reqMktData(rid, contract, "100,101", True, False, [])
        done.wait(timeout=REQUEST_TMO)
        try:
            app.cancelMktData(rid)
        except Exception:
            pass
    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    call_vol = ticks.get(100, 0)
    put_vol  = ticks.get(101, 0)
    call_oi  = ticks.get(27, 0)
    put_oi   = ticks.get(28, 0)

    result = {
        "call_option_volume": call_vol,
        "put_option_volume":  put_vol,
        "call_open_interest": call_oi,
        "put_open_interest":  put_oi,
    }
    if call_vol > 0:
        result["pcr_volume"] = round(put_vol / call_vol, 3)
    if call_oi > 0:
        result["pcr_oi"] = round(put_oi / call_oi, 3)
    return result


# ─── 5. Market Scanner ─────────────────────────────────────────────────────────
def run_scanner(
    scan_code:  str   = "HOT_BY_VOLUME",
    instrument: str   = "STK",
    location:   str   = "STK.US.MAJOR",
    above_price: float = 5.0,
    above_volume: int  = 500_000,
    market_cap_above: int = 500_000_000,
) -> list[str]:
    """
    Run an IBKR market scanner. Returns a list of ticker symbols.
    Max 50 results per scan (IBKR limit). Max 10 concurrent scans.

    Popular scan_codes:
        HOT_BY_VOLUME            — High relative volume
        TOP_PERC_GAIN            — Top % gainers
        TOP_OPEN_PERC_GAIN       — Top % gainers at open
        HIGH_OPT_VOLUME_PUT_CALL_RATIO  — High PCR (bearish signal for market)
        STRONG_VOLUME_SURGE      — Volume surge
        HIGH_VS_13W_HL           — Near 13-week high
        HIGH_VS_26W_HL           — Near 26-week high
        MOST_ACTIVE              — Most active by $ volume
    """
    if not _IBAPI_AVAILABLE:
        return []

    symbols: list[str] = []
    done = threading.Event()

    class _App(_TWSBase):
        def scannerData(self_inner, reqId, rank, contractDetails, distance,
                        benchmark, projection, legsStr):
            sym = contractDetails.contract.symbol
            symbols.append(sym)

        def scannerDataEnd(self_inner, reqId):
            done.set()

        def error(self_inner, reqId, errorCode, errorString,
                  advancedOrderRejectJson=""):
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
            if errorCode not in (2104, 2106, 2107, 2108, 2158, 10197):
                done.set()

    sub = ScannerSubscription()
    sub.instrument         = instrument
    sub.locationCode       = location
    sub.scanCode           = scan_code
    sub.abovePrice         = above_price
    sub.aboveVolume        = above_volume

    app = _App()
    try:
        if not app._connect_and_wait():
            return []
        rid = app._req_id()
        # Use TagValues for market cap filter (requires TWS v973+)
        from ibapi.tag_value import TagValue
        filters = [TagValue("usdMarketCapAbove", str(market_cap_above))]
        app.reqScannerSubscription(rid, sub, [], filters)
        done.wait(timeout=REQUEST_TMO)
        app.cancelScannerSubscription(rid)
    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    return symbols


# ─── 6. L6 composite score ─────────────────────────────────────────────────────
def compute_l6_score(symbol: str, force: bool = False) -> dict:
    """
    Compute the L6 IBKR enrichment score (0–10) for a US stock.

    Combines:
      • News sentiment   (VADER on BRFG/BRFUPDN/DJNL headlines) — 40%
      • PE ratio health  (tick 258 fundamentals)                  — 30%
      • Option PCR       (put/call ratio from tick 100/101)       — 30%

    Returns dict:
        {
          "l6_score":     float | None,  # 0–10 or None if no data
          "news":         dict,
          "ratios":       dict,
          "options":      dict,
          "conid":        int | None,
        }
    None means IBKR is unavailable or no data was returned.
    """
    if symbol in _L6_CACHE and not force:
        return _L6_CACHE[symbol]

    if not _IBAPI_AVAILABLE:
        return {"l6_score": None, "error": "ibapi not installed"}

    # ── Step 1: get conId ──────────────────────────────────────────────────────
    conid = get_conid(symbol)
    if conid is None:
        result = {"l6_score": None, "conid": None, "error": "conId not found"}
        _L6_CACHE[symbol] = result
        return result

    # ── Step 2: fetch data in parallel (each opens/closes own connection) ─────
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_news    = pool.submit(fetch_news_sentiment, symbol, conid)
        f_ratios  = pool.submit(fetch_fundamental_ratios, symbol, conid)
        f_options = pool.submit(fetch_option_stats, symbol, conid)
    news_data    = f_news.result()
    ratios_data  = f_ratios.result()
    options_data = f_options.result()

    scores_used = []

    # ── News component ──────────────────────────────────────────────────────────
    news_score = None
    if news_data.get("count", 0) >= 1:
        compound  = news_data["score"]   # -1 to +1
        # Map to 0–10: neutral (-0.05..+0.05) → 5, strong positive → 10
        news_score = round(5.0 + compound * 5.0, 2)
        news_score = max(0.0, min(10.0, news_score))
        scores_used.append(("news", news_score, 0.40))

    # ── PE ratio component ──────────────────────────────────────────────────────
    pe_score = None
    pe_val   = ratios_data.get("NPTMCEM") or ratios_data.get("PR") or ratios_data.get("TTMPRICE")
    # Try common Fundamental Ratios keys for PE
    # IBKR tick-258 returns keys like: NPRICE, NHIG, NLOW, PR (P/E), EPS12M, NPTMCEM...
    pe_raw   = ratios_data.get("PR")   # trailing P/E ratio
    if pe_raw is None:
        pe_raw = ratios_data.get("NPTMCEM")  # alternative key
    if pe_raw is not None and pe_raw > 0:
        # Good PE: 10–25 → score 8–10, stretched >50 → score 3, negative → score 2
        if pe_raw <= 0:
            pe_score = 2.0
        elif pe_raw <= 15:
            pe_score = 9.0
        elif pe_raw <= 25:
            pe_score = 8.0
        elif pe_raw <= 40:
            pe_score = 6.5
        elif pe_raw <= 60:
            pe_score = 4.0
        else:
            pe_score = 2.5
        scores_used.append(("pe", pe_score, 0.30))

    # ── PCR component ───────────────────────────────────────────────────────────
    pcr_score = None
    pcr_vol   = options_data.get("pcr_volume")
    if pcr_vol is not None:
        # PCR < 0.7 = bullish (calls dominate) → high score
        # PCR 0.7–1.0 = neutral
        # PCR > 1.3 = bearish (puts dominate) → low score
        if pcr_vol < 0.5:
            pcr_score = 9.0
        elif pcr_vol < 0.7:
            pcr_score = 8.0
        elif pcr_vol < 1.0:
            pcr_score = 6.5
        elif pcr_vol < 1.3:
            pcr_score = 4.5
        elif pcr_vol < 1.8:
            pcr_score = 3.0
        else:
            pcr_score = 1.5
        scores_used.append(("pcr", pcr_score, 0.30))

    # ── Compute weighted average ────────────────────────────────────────────────
    l6_score = None
    if scores_used:
        total_weight = sum(w for _, _, w in scores_used)
        weighted_sum = sum(s * w for _, s, w in scores_used)
        l6_score     = round(weighted_sum / total_weight, 2)

    result = {
        "l6_score":    l6_score,
        "conid":       conid,
        "news":        news_data,
        "ratios":      ratios_data,
        "options":     options_data,
        "components":  {name: score for name, score, _ in scores_used},
    }
    _L6_CACHE[symbol] = result
    return result


# ─── HTML badge for signal_stack.py ───────────────────────────────────────────
def l6_html_badge(enrichment: dict) -> str:
    """Return HTML snippet for the L6 badge in signal_stack cards."""
    if not enrichment:
        return ""
    l6 = enrichment.get("l6_score")
    if l6 is None:
        return ""

    color  = "#22c55e" if l6 >= 7 else ("#eab308" if l6 >= 5 else "#ef4444")
    news   = enrichment.get("news", {})
    opts   = enrichment.get("options", {})
    ratios = enrichment.get("ratios", {})

    news_info  = f"{news.get('count', 0)} headlines, avg VADER {news.get('score', 0):+.2f}" if news else ""
    pcr_info   = f"PCR {opts['pcr_volume']:.2f}" if opts.get("pcr_volume") is not None else ""
    pe_info    = f"P/E {ratios['PR']:.1f}" if ratios.get("PR") is not None else ""
    detail_str = " · ".join(x for x in [news_info, pcr_info, pe_info] if x)

    return (
        f'<div class="l6-block" style="margin-top:8px;padding:6px 10px;'
        f'border-radius:6px;background:#0f172a;border:1px solid #334155">'
        f'<span style="font-weight:700;color:{color}">L6 IBKR {l6:.1f}/10</span>'
        f'<span style="color:#94a3b8;font-size:0.78em;margin-left:8px">{detail_str}</span>'
        f'</div>'
    )


# ─── Standalone probe ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Enricher probe")
    parser.add_argument("--probe",  nargs="+", metavar="SYM",
                        help="Probe L6 score for symbols (e.g. AAPL MSFT)")
    parser.add_argument("--scan",   metavar="CODE", default=None,
                        help="Run a market scanner (e.g. HOT_BY_VOLUME)")
    parser.add_argument("--port",   type=int, default=IBKR_PORT,
                        help=f"TWS port (default {IBKR_PORT})")
    args = parser.parse_args()

    # Allow port override
    if args.port != IBKR_PORT:
        import ibkr_enricher as _self
        _self.IBKR_PORT = args.port
        IBKR_PORT = args.port

    if not _IBAPI_AVAILABLE:
        print("ERROR: ibapi not installed. Run: pip install ibapi")
        raise SystemExit(1)

    if args.scan:
        print(f"\nRunning IBKR scanner: {args.scan}")
        syms = run_scanner(scan_code=args.scan)
        print(f"Results ({len(syms)}): {syms}")

    if args.probe:
        for sym in args.probe:
            print(f"\n{'='*50}")
            print(f"Probing L6 for {sym} ...")
            t0  = time.time()
            res = compute_l6_score(sym, force=True)
            elapsed = time.time() - t0

            l6 = res.get("l6_score")
            print(f"  conId     : {res.get('conid')}")
            print(f"  L6 score  : {l6}")
            print(f"  Components: {res.get('components', {})}")
            news = res.get("news", {})
            if news:
                print(f"  News      : {news.get('count')} headlines, "
                      f"avg VADER {news.get('score', 0):+.3f}")
                for h in news.get("headlines", [])[:3]:
                    print(f"    · {h[:90]}")
            opts = res.get("options", {})
            if opts:
                print(f"  Options   : call_vol={opts.get('call_option_volume')}, "
                      f"put_vol={opts.get('put_option_volume')}, "
                      f"PCR={opts.get('pcr_volume')}")
            ratios = res.get("ratios", {})
            if ratios:
                key_ratios = {k: ratios[k] for k in
                              ["PR", "NPTMCEM", "EPS12M", "YIELD"]
                              if k in ratios}
                print(f"  Ratios    : {key_ratios}")
            print(f"  Time      : {elapsed:.1f}s")
