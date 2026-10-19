"""
tv_pro_scanner.py
=================
TradingView Pro Multi-Asset Scanner — Unlocks ALL Pro features not in tv_screener_fundamentals.py

Covers:
  • Forex pairs           (6,407 symbols from FX/FX_IDC)
  • Futures contracts     (53,905: NSE F&O, CME gold/oil/silver/gas, index futures)
  • Crypto coins          (56,371 from all CEX)
  • CFD / Total Market    (650 instruments including market-cap totals)
  • Global stock screener (526,696 symbols across all exchanges)

This is SEPARATE from tv_screener_fundamentals.py (which does per-exchange stock fundamentals).
Think of this file as the "alternative assets screener".

Usage:
  from tradingview.tv_pro_scanner import TVProScanner

  scanner = TVProScanner()

  # Top NSE F&O futures by open interest
  fo = scanner.scan_futures(exchange="NSE", sort_by="open_interest", n=20)

  # Top forex pairs by momentum
  fx = scanner.scan_forex(sort_by="Volatility.D", n=20)

  # Top crypto by market cap
  crypto = scanner.scan_crypto(sort_by="market_cap_calc", n=20)

  # Major global commodities/indices snapshot
  snap = scanner.major_snapshot()

  # NSE F&O with bullish signals (RSI > 55, positive change)
  fo_bullish = scanner.scan_fo_signals(market="NSE")

  # Actionable report
  report = scanner.generate_report()
  print(report)

All methods return list[dict] with normalised keys.
Errors are soft-caught; empty list returned on failure.

CONFIRMED WORKING fields per asset class (tested 2026-04-14):
  Futures: name, close, change, high, low, volume, RSI, Recommend.All, ATR,
           SMA200, MACD.macd, open_interest, description, exchange,
           Volatility.D, relative_volume_10d_calc
  Forex:   name, close, change, high, low, volume, RSI, Recommend.All, ATR,
           SMA200, Volatility.D, relative_volume_10d_calc, MACD.macd, bid, ask
  Crypto:  name, close, change, high, low, volume, RSI, Recommend.All, ATR,
           SMA200, market_cap_calc, circulating_supply, Volatility.D,
           relative_volume_10d_calc, MACD.macd
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Scanner endpoints ────────────────────────────────────────────────────────
_FUTURES_URL = "https://scanner.tradingview.com/futures/scan"
_FOREX_URL   = "https://scanner.tradingview.com/forex/scan"
_CRYPTO_URL  = "https://scanner.tradingview.com/crypto/scan"
_CFD_URL     = "https://scanner.tradingview.com/cfd/scan"
_GLOBAL_URL  = "https://scanner.tradingview.com/global/scan"

# ─── Confirmed-valid columns per asset class ─────────────────────────────────
_FUTURES_COLS = [
    "name", "close", "change", "high", "low", "volume",
    "RSI", "Recommend.All", "ATR", "SMA200", "MACD.macd",
    "open_interest", "description", "exchange",
    "Volatility.D", "relative_volume_10d_calc",
]

_FOREX_COLS = [
    "name", "close", "change", "high", "low", "volume",
    "RSI", "Recommend.All", "ATR", "SMA200", "Volatility.D",
    "relative_volume_10d_calc", "MACD.macd", "bid", "ask",
]

_CRYPTO_COLS = [
    "name", "close", "change", "high", "low", "volume",
    "RSI", "Recommend.All", "ATR", "SMA200",
    "market_cap_calc", "circulating_supply",
    "Volatility.D", "relative_volume_10d_calc", "MACD.macd",
]

# ─── Major instruments to always include in snapshot ─────────────────────────
MAJOR_FUTURES = {
    # Indian index futures (all confirmed working — included in NSE data plan)
    "NSE:NIFTY1!":      "Nifty 50",
    "NSE:BANKNIFTY1!":  "BankNifty",
    "NSE:FINNIFTY1!":   "FinNifty",
    "NSE:MIDCPNIFTY1!": "MidCapNifty",
    # US equity index futures (CME - confirmed working)
    "CME:ES1!":         "S&P 500 E-mini",
    "CME:NQ1!":         "Nasdaq 100 E-mini",
    # NOTE: CME:GC1!(Gold), CME:CL1!(Oil), CME:SI1!(Silver) require separate CME data
    # subscription — NOT included here. Use yfinance (GC=F, CL=F, SI=F) as fallback.
    # Energy (grains/gas — confirmed working without extra subscription)
    "NYMEX:NG1!":       "Natural Gas",
    # Grains/soft commodities (CBOT — confirmed working)
    "CBOT:ZW1!":        "Wheat",
    "CBOT:ZC1!":        "Corn",
    "CBOT:ZS1!":        "Soybeans",
    # Currencies (CME FX futures — confirmed working)
    "CME:6E1!":         "EUR/USD Futures",
    "CME:6J1!":         "JPY/USD Futures",
    "CME:6A1!":         "AUD/USD Futures",
    "CME:6B1!":         "GBP/USD Futures",
    "CME:6C1!":         "CAD/USD Futures",
    # Crypto futures (CME — confirmed working)
    "CME:BTC1!":        "Bitcoin CME Futures",
    "CME:ETH1!":        "Ethereum CME Futures",
    # VIX / Volatility (CBOE — confirmed working)
    "CBOE:VX1!":        "VIX Futures",
    # SGX Nifty
    "SGX:IN1!":         "SGX Nifty",
}

# Commodities that require CME data subscription — fetched via yfinance fallback
COMMODITY_YFINANCE_TICKERS = {
    "GC=F":  ("Gold",         "CME:GC1!"),
    "SI=F":  ("Silver",       "CME:SI1!"),
    "CL=F":  ("Crude Oil WTI","CME:CL1!"),
    "BZ=F":  ("Brent Crude",  "CME:BZ1!"),
    "HG=F":  ("Copper",       "CME:HG1!"),
    "PL=F":  ("Platinum",     "CME:PL1!"),
}

MAJOR_FOREX = [
    "FX:EURUSD", "FX:GBPUSD", "FX:USDJPY", "FX:USDCHF",
    "FX:AUDUSD", "FX:USDCAD", "FX:NZDUSD", "FX:EURGBP",
    "FX:EURJPY", "FX:GBPJPY", "FX:USDINR", "FX:AUDINR",
    "FX:USDSGD", "FX:GBPAUD", "FX:EURAUD",
]

MAJOR_CRYPTO = [
    "BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT",
    "BINANCE:BNBUSDT", "BINANCE:XRPUSDT", "BINANCE:ADAUSDT",
    "BINANCE:DOGEUSDT", "BINANCE:AVAXUSDT", "BINANCE:DOTUSDT",
]

# ─── Cookie path ─────────────────────────────────────────────────────────────
_COOKIE_PATH = Path(__file__).parent / "cookie"


class TVProScanner:
    """
    Multi-asset scanner using TradingView Pro authenticated scanner endpoints.
    All methods return list[dict] with normalised keys (snake_case).
    """

    def __init__(self, cookie: Optional[str] = None):
        if cookie:
            self._cookie = cookie
        elif _COOKIE_PATH.exists():
            self._cookie = _COOKIE_PATH.read_text(encoding="utf-8").strip()
        else:
            self._cookie = None

        self._headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
        }
        if self._cookie:
            self._headers["Cookie"] = self._cookie

    # ─── internal HTTP helper ─────────────────────────────────────────────────

    def _post(self, url: str, payload: dict, timeout: int = 15) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}", "data": []}
        except Exception as e:
            return {"error": str(e), "data": []}

    def _build_payload(self, cols: list, filters: list = None,
                       sort_by: str = "volume", n: int = 50,
                       tickers: list = None) -> dict:
        payload: dict = {
            "options": {"lang": "en"},
            "columns": cols,
            "sort": {"sortBy": sort_by, "sortOrder": "desc"},
            "range": [0, n],
        }
        if tickers:
            payload["symbols"] = {"tickers": tickers}
        else:
            payload["filter"] = filters or []
            payload["symbols"] = {}
        return payload

    def _parse_rows(self, data: dict, cols: list) -> list[dict]:
        """Convert raw scanner data to list of normalised dicts."""
        results = []
        for row in data.get("data", []):
            vals = row.get("d", [])
            d = {"symbol": row.get("s", ""), "total_count": data.get("totalCount", 0)}
            for i, col in enumerate(cols):
                key = col.replace(".", "_").lower()
                d[key] = vals[i] if i < len(vals) else None
            results.append(d)
        return results

    # ─── PUBLIC: Futures ──────────────────────────────────────────────────────

    def scan_futures(
        self,
        exchange: Optional[str] = None,
        sort_by: str = "open_interest",
        n: int = 30,
        min_oi: Optional[float] = None,
        min_volume: Optional[float] = None,
    ) -> list[dict]:
        """Scan futures. exchange='NSE' for NSE F&O, None for all."""
        filters = []
        if exchange:
            filters.append({"left": "exchange", "operation": "equal", "right": exchange})
        if min_oi:
            filters.append({"left": "open_interest", "operation": "greater",
                             "right": min_oi})
        if min_volume:
            filters.append({"left": "volume", "operation": "greater",
                             "right": min_volume})
        payload = self._build_payload(_FUTURES_COLS, filters, sort_by, n)
        data = self._post(_FUTURES_URL, payload)
        if "error" in data and not data.get("data"):
            return []
        return self._parse_rows(data, _FUTURES_COLS)

    def scan_fo_signals(
        self,
        market: str = "NSE",
        min_rsi: float = 50.0,
        min_change: float = 0.0,
        n: int = 20,
    ) -> list[dict]:
        """
        NSE/BANKNIFTY F&O futures with bullish momentum signals.
        Returns contracts where RSI > min_rsi AND daily change > min_change.
        """
        raw = self.scan_futures(exchange=market, sort_by="open_interest", n=200)
        signals = []
        for r in raw:
            rsi = r.get("rsi") or 0
            chg = r.get("change") or 0
            oi  = r.get("open_interest") or 0
            if rsi >= min_rsi and chg >= min_change and oi > 0:
                r["signal"] = "BULLISH" if r.get("recommend_all", 0) > 0 else "NEUTRAL"
                signals.append(r)
        signals.sort(key=lambda x: x.get("open_interest") or 0, reverse=True)
        return signals[:n]

    def major_snapshot(self) -> list[dict]:
        """
        Snapshot of all major global futures: indices, commodities, metals, energy, currencies.
        Returns data for ~25 major instruments.
        """
        tickers = list(MAJOR_FUTURES.keys())
        payload = self._build_payload(_FUTURES_COLS, tickers=tickers, n=len(tickers))
        data = self._post(_FUTURES_URL, payload)
        rows = self._parse_rows(data, _FUTURES_COLS)
        # Add human name
        for r in rows:
            r["label"] = MAJOR_FUTURES.get(r["symbol"], r.get("description", ""))
        # Sort by change magnitude
        rows.sort(key=lambda x: abs(x.get("change") or 0), reverse=True)
        return rows

    # ─── PUBLIC: Forex ────────────────────────────────────────────────────────

    def scan_forex(
        self,
        sort_by: str = "Volatility.D",
        n: int = 20,
        min_rsi: Optional[float] = None,
        max_rsi: Optional[float] = None,
    ) -> list[dict]:
        """Scan all forex pairs. Optionally filter by RSI range."""
        payload = self._build_payload(_FOREX_COLS, [], sort_by, n)
        data = self._post(_FOREX_URL, payload)
        rows = self._parse_rows(data, _FOREX_COLS)
        if min_rsi is not None:
            rows = [r for r in rows if (r.get("rsi") or 0) >= min_rsi]
        if max_rsi is not None:
            rows = [r for r in rows if (r.get("rsi") or 0) <= max_rsi]
        return rows

    def major_forex_snapshot(self) -> list[dict]:
        """Snapshot of ~15 major forex pairs."""
        payload = self._build_payload(_FOREX_COLS, tickers=MAJOR_FOREX, n=len(MAJOR_FOREX))
        data = self._post(_FOREX_URL, payload)
        rows = self._parse_rows(data, _FOREX_COLS)
        rows.sort(key=lambda x: abs(x.get("change") or 0), reverse=True)
        return rows

    def scan_forex_momentum(self, n: int = 15) -> list[dict]:
        """
        High-momentum forex pairs: RSI 55-75, bullish TV recommendation,
        high volatility (opportunity window).
        """
        raw = self.scan_forex(sort_by="Volatility.D", n=200)
        results = []
        for r in raw:
            rsi  = r.get("rsi") or 0
            rec  = r.get("recommend_all") or 0
            vol  = r.get("volatility_d") or 0
            macd = r.get("macd_macd") or 0
            if 50 <= rsi <= 75 and rec > 0.2 and vol > 0.5:
                r["momentum_score"] = round(rsi * 0.4 + rec * 30 + min(vol, 3) * 10, 2)
                results.append(r)
        results.sort(key=lambda x: x.get("momentum_score", 0), reverse=True)
        return results[:n]

    # ─── PUBLIC: Crypto ───────────────────────────────────────────────────────

    def scan_crypto(
        self,
        sort_by: str = "market_cap_calc",
        n: int = 20,
        min_market_cap: Optional[float] = None,
    ) -> list[dict]:
        """Scan top crypto coins."""
        payload = self._build_payload(_CRYPTO_COLS, [], sort_by, n)
        data = self._post(_CRYPTO_URL, payload)
        rows = self._parse_rows(data, _CRYPTO_COLS)
        if min_market_cap:
            rows = [r for r in rows if (r.get("market_cap_calc") or 0) >= min_market_cap]
        return rows

    def major_crypto_snapshot(self) -> list[dict]:
        """Snapshot of 9 major crypto pairs."""
        payload = self._build_payload(_CRYPTO_COLS, tickers=MAJOR_CRYPTO, n=len(MAJOR_CRYPTO))
        data = self._post(_CRYPTO_URL, payload)
        rows = self._parse_rows(data, _CRYPTO_COLS)
        rows.sort(key=lambda x: abs(x.get("change") or 0), reverse=True)
        return rows

    # ─── PUBLIC: Report ───────────────────────────────────────────────────────

    def generate_report(self, include_crypto: bool = False) -> str:
        """
        Generate a text report of all high-opportunity instruments across
        major global markets. Useful as a morning briefing.
        """
        lines = [
            "=" * 70,
            f"  TV PRO MULTI-ASSET REPORT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 70,
        ]

        # Major global futures
        lines.append("\n─── MAJOR GLOBAL FUTURES ─────────────────────────────────────────")
        for r in self.major_snapshot():
            sym   = r.get("symbol", "")
            label = r.get("label", r.get("description", ""))
            chg   = r.get("change") or 0
            close = r.get("close") or 0
            rsi   = r.get("rsi") or 0
            oi    = r.get("open_interest") or 0
            sign  = "+" if chg > 0 else ("-" if chg < 0 else "=")
            oi_str = f" OI={oi:,.0f}" if oi else ""
            lines.append(
                f"  {sign} {sym:20s} {label:25s} {close:>12.3f}  {chg:>+7.3f}%  RSI={rsi:>5.1f}{oi_str}"
            )

        # NSE F&O signals
        lines.append("\n─── NSE F&O — TOP BY OPEN INTEREST ──────────────────────────────")
        fo = self.scan_futures(exchange="NSE", sort_by="open_interest", n=15)
        for r in fo:
            sym  = r.get("symbol", "")
            desc = (r.get("description") or "")[:28]
            chg  = r.get("change") or 0
            close = r.get("close") or 0
            oi   = r.get("open_interest") or 0
            rsi  = r.get("rsi") or 0
            sign = "+" if chg > 0 else ("-" if chg < 0 else "=")
            lines.append(
                f"  {sign} {sym:25s} {desc:28s} {close:>10.2f}  {chg:>+7.3f}%  "
                f"RSI={rsi:>5.1f}  OI={oi:>15,.0f}"
            )

        # Major forex
        lines.append("\n─── MAJOR FOREX PAIRS ────────────────────────────────────────────")
        for r in self.major_forex_snapshot():
            sym   = r.get("symbol", "")
            chg   = r.get("change") or 0
            close = r.get("close") or 0
            rsi   = r.get("rsi") or 0
            vol   = r.get("volatility_d") or 0
            sign  = "+" if chg > 0 else ("-" if chg < 0 else "=")
            lines.append(
                f"  {sign} {sym:20s} {close:>12.5f}  {chg:>+7.3f}%  RSI={rsi:>5.1f}  Vol%={vol:.3f}"
            )

        # Forex momentum screen
        lines.append("\n─── FOREX MOMENTUM SIGNALS (RSI 50–75, Bullish) ─────────────────")
        fx_mom = self.scan_forex_momentum(n=10)
        if fx_mom:
            for r in fx_mom:
                sym   = r.get("symbol", "")
                chg   = r.get("change") or 0
                close = r.get("close") or 0
                rsi   = r.get("rsi") or 0
                score = r.get("momentum_score", 0)
                lines.append(
                    f"  + {sym:25s} {close:>12.5f}  {chg:>+7.3f}%  RSI={rsi:>5.1f}  Score={score}"
                )
        else:
            lines.append("  (no signals)")

        if include_crypto:
            lines.append("\n─── TOP CRYPTO (by market cap) ───────────────────────────────────")
            for r in self.major_crypto_snapshot():
                sym   = r.get("symbol", "")
                chg   = r.get("change") or 0
                close = r.get("close") or 0
                rsi   = r.get("rsi") or 0
                mcap  = r.get("market_cap_calc") or 0
                sign  = "+" if chg > 0 else ("-" if chg < 0 else "=")
                mcap_str = f"${mcap/1e9:.1f}B" if mcap >= 1e9 else f"${mcap/1e6:.0f}M"
                lines.append(
                    f"  {sign} {sym:30s} {close:>12.2f}  {chg:>+7.3f}%  RSI={rsi:>5.1f}  MCap={mcap_str}"
                )

        lines.append("\n" + "=" * 70)
        return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    # Windows console UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(description="TV Pro Multi-Asset Scanner")
    ap.add_argument("--futures",      action="store_true", help="NSE F&O futures")
    ap.add_argument("--forex",        action="store_true", help="Top forex pairs")
    ap.add_argument("--crypto",       action="store_true", help="Top crypto coins")
    ap.add_argument("--major",        action="store_true", help="Major global snapshot (default)")
    ap.add_argument("--report",       action="store_true", help="Full report (default)")
    ap.add_argument("--exchange",     default="NSE",       help="Exchange filter for futures")
    ap.add_argument("--n",            type=int, default=20, help="Max results")
    ap.add_argument("--min-rsi",      type=float, default=None)
    ap.add_argument("--max-rsi",      type=float, default=None)
    ap.add_argument("--include-crypto", action="store_true")
    args = ap.parse_args()

    scanner = TVProScanner()

    if args.futures:
        rows = scanner.scan_futures(exchange=args.exchange, n=args.n)
        print(f"Futures ({args.exchange}): {len(rows)} results")
        for r in rows:
            print(f"  {r['symbol']:25s}  close={r['close']}  OI={r['open_interest']}  RSI={r['rsi']}")
    elif args.forex:
        rows = scanner.scan_forex(n=args.n, min_rsi=args.min_rsi, max_rsi=args.max_rsi)
        print(f"Forex: {len(rows)} results")
        for r in rows:
            print(f"  {r['symbol']:25s}  close={r['close']}  RSI={r['rsi']}  vol={r['volatility_d']}")
    elif args.crypto:
        rows = scanner.scan_crypto(n=args.n)
        print(f"Crypto: {len(rows)} results")
        for r in rows:
            print(f"  {r['symbol']:30s}  close={r['close']}  RSI={r['rsi']}")
    else:
        # Default: full report
        print(scanner.generate_report(include_crypto=args.include_crypto))
