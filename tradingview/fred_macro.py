#!/usr/bin/env python3
"""
FRED Macro Signals
===================
Provides authoritative macro signals from the Federal Reserve Economic Data (FRED) API,
with a graceful fallback to yfinance / TV scanner when the FRED_API_KEY is absent.

Value-add over current yfinance yield curve:
  1. FEDFUNDS  — actual fed funds rate (not a futures proxy like ^IRX)
  2. CPIAUCSL  — CPI momentum (not available via yfinance at all)
  3. T10YIE    — 10-year breakeven inflation (forward inflation expectations)
  4. T10Y2Y    — FRED's own 10Y-2Y spread (cleaner than ^IRX-derived spread)

All data is cached for 24 hours via diskcache (already installed).

Setup:
    1. Register free at https://fred.stlouisfed.org/docs/api/api_key.html
    2. Set environment variable: FRED_API_KEY=<your_key>
    3. Optionally: pip install fredapi   (HTTP fallback works without it)

Usage:
    from tradingview.fred_macro import fetch_macro_signals
    signals = fetch_macro_signals()
    # → {"fedfunds": 5.33, "cpi_mom": 0.3, "t10yie": 2.41,
    #    "yield_spread": 0.82, "regime": "tight", "source": "FRED"}
"""
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).parent.resolve()
_CACHE_FILE = _HERE / ".fred_macro_cache.json"
_CACHE_TTL  = 86_400   # 24 hours
_CACHE_VERSION = 2     # v2 fixes Yahoo yield units and records curve tenor


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    try:
        if not _CACHE_FILE.exists():
            return None
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if (raw.get("version") == _CACHE_VERSION
                and (datetime.now().timestamp() - raw.get("ts", 0)) < _CACHE_TTL):
            return raw["data"]
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps({"version": _CACHE_VERSION,
                        "ts": datetime.now().timestamp(), "data": data}),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── FRED HTTP fetch ──────────────────────────────────────────────────────────

def _fred_fetch(series_id: str, api_key: str, limit: int = 1) -> float | None:
    """Fetch the latest value for a FRED series via REST API."""
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        f"&sort_order=desc&limit={limit}"
    )
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "stocks-signal-stack/1.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        obs  = data.get("observations", [])
        for o in obs:
            val = o.get("value", ".")
            if val != "." and val is not None:
                return float(val)
    except Exception:
        pass
    return None


# ── yfinance fallback ────────────────────────────────────────────────────────

def _spread_bps(short_yield_pct: float, long_yield_pct: float) -> float:
    """Return long-minus-short spread when both yields are percentage values."""
    return round((float(long_yield_pct) - float(short_yield_pct)) * 100, 1)

def _yf_yield_spread() -> tuple[float | None, float | None, float | None]:
    """3M / 10Y via yfinance as fallback.  Returns (irx, tnx, spread_bps)."""
    try:
        import yfinance as yf
        df = yf.download(["^IRX", "^TNX"], period="5d", auto_adjust=True, progress=False)
        if hasattr(df.columns, "levels"):
            irx = df["Close"]["^IRX"].dropna().iloc[-1] if "^IRX" in df["Close"].columns else None
            tnx = df["Close"]["^TNX"].dropna().iloc[-1] if "^TNX" in df["Close"].columns else None
        else:
            irx = tnx = None
        if irx is not None and tnx is not None:
            spread = _spread_bps(irx, tnx)
            return round(float(irx) / 100, 4), round(float(tnx) / 100, 4), spread
    except Exception:
        pass
    return None, None, None


# ── TV scanner fallback (yield curve — already in tv_screener_fundamentals) ──

def _tv_yield_spread() -> tuple[float | None, float | None, float | None]:
    """TV global scanner 2Y / 10Y as fallback."""
    try:
        import json as _json
        import urllib.request as _req
        payload = {
            "columns": ["close"],
            "symbols": {"tickers": ["TVC:US02Y", "TVC:US10Y"]},
            "range": [0, 2],
        }
        r = _req.Request(
            "https://scanner.tradingview.com/global/scan",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        data = _json.loads(_req.urlopen(r, timeout=8).read())
        yields = {item["s"]: item["d"][0] for item in data.get("data", []) if item["d"][0] is not None}
        us2y  = yields.get("TVC:US02Y")
        us10y = yields.get("TVC:US10Y")
        if us2y and us10y:
            return round(us2y / 100, 4), round(us10y / 100, 4), round((us10y - us2y) * 100, 1)
    except Exception:
        pass
    return None, None, None


# ── Regime classification ─────────────────────────────────────────────────────

def _classify_regime(fedfunds: float | None, cpi_mom: float | None,
                      t10yie: float | None, spread: float | None) -> str:
    """
    Simple macro regime label used for signal weighting.
    Returns one of: "easy", "neutral", "tight", "crisis".
    """
    tightening_signals = 0
    if fedfunds is not None and fedfunds >= 4.5:  tightening_signals += 1
    if cpi_mom  is not None and cpi_mom  >= 0.4:  tightening_signals += 1
    if t10yie   is not None and t10yie   >= 2.5:  tightening_signals += 1
    if spread   is not None and spread   < 0:      tightening_signals += 1   # inverted = crisis risk

    if tightening_signals >= 3: return "tight"
    if tightening_signals >= 2: return "neutral"
    if spread is not None and spread < -50: return "crisis"
    return "easy"


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_macro_signals() -> dict:
    """
    Fetch macro signals.  Priority:
      1. 24h cache (any source)
      2. FRED API (when FRED_API_KEY env var is set)
            3. TV scanner fallback (true 2Y/10Y yield curve)
            4. yfinance fallback (3M/10Y curve only, no CPI/breakeven)

    Returns
    -------
    dict with keys:
        fedfunds       float | None  — Fed Funds rate (%)
        cpi_mom        float | None  — CPI month-over-month % change
        t10yie         float | None  — 10Y breakeven inflation (%)
        us2y           float | None  — 2Y Treasury yield (decimal)
        us3m           float | None  — 13-week Treasury yield (decimal)
        us10y          float | None  — 10Y Treasury yield (decimal)
        yield_spread   float | None  — long-minus-short spread in basis points
        curve_tenor    str | None    — "2s10s" or "3m10y"
        regime         str           — "easy" | "neutral" | "tight" | "crisis"
        source         str           — "FRED" | "yfinance" | "TV-scanner" | "cache"
    """
    cached = _load_cache()
    if cached is not None:
        cached["source"] = "cache"
        return cached

    api_key   = os.environ.get("FRED_API_KEY", "").strip()
    result: dict = {
        "fedfunds":     None,
        "cpi_mom":      None,
        "t10yie":       None,
        "us2y":         None,
        "us3m":         None,
        "us10y":        None,
        "yield_spread": None,
        "spread_bps":   None,   # alias for score_market_internals() compatibility
        "curve_tenor":  None,
        "regime":       "neutral",
        "source":       "none",
    }

    if api_key:
        # ── FRED primary path ──
        try:
            ff    = _fred_fetch("FEDFUNDS",  api_key)
            cpi_2 = _fred_fetch("CPIAUCSL",  api_key, limit=2)
            cpi_1 = _fred_fetch("CPIAUCSL",  api_key, limit=1)
            t10ie = _fred_fetch("T10YIE",    api_key)
            sp    = _fred_fetch("T10Y2Y",    api_key)
            us2y  = _fred_fetch("DGS2",      api_key)
            us10y = _fred_fetch("DGS10",     api_key)

            cpi_mom = None
            # CPI MoM requires two observations
            if cpi_1 is not None and cpi_2 is not None and cpi_2 != 0:
                cpi_mom = round((cpi_1 / cpi_2 - 1) * 100, 3)

            result.update({
                "fedfunds":     ff,
                "cpi_mom":      cpi_mom,
                "t10yie":       t10ie,
                "us2y":         round(us2y  / 100, 4) if us2y  is not None else None,
                "us10y":        round(us10y / 100, 4) if us10y is not None else None,
                "yield_spread": round(sp * 100, 1)    if sp    is not None else None,
                "spread_bps":   round(sp * 100, 1)    if sp    is not None else None,
                "curve_tenor":  "2s10s",
                "source":       "FRED",
            })
            result["regime"] = _classify_regime(ff, cpi_mom, t10ie, result["yield_spread"])
            _save_cache(result)
            return result
        except Exception:
            pass   # fall through to public yield-curve sources

    # ── TV scanner fallback: true 2Y/10Y tenors ──
    us2y, us10y, spread = _tv_yield_spread()
    if us2y is not None:
        result.update({
            "us2y":         us2y,
            "us10y":        us10y,
            "yield_spread": spread,
            "spread_bps":   spread,
            "curve_tenor":  "2s10s",
            "source":       "TV-scanner",
        })
        result["regime"] = _classify_regime(None, None, None, spread)
        _save_cache(result)
        return result

    # ── yfinance fallback: ^IRX is 13-week, not 2-year ──
    us3m, us10y, spread = _yf_yield_spread()
    if us3m is not None:
        result.update({
            "us3m":         us3m,
            "us10y":        us10y,
            "yield_spread": spread,
            "spread_bps":   spread,
            "curve_tenor":  "3m10y",
            "source":       "yfinance",
        })
        result["regime"] = _classify_regime(None, None, None, spread)
        _save_cache(result)

    return result


if __name__ == "__main__":
    import sys as _sys
    if _sys.platform == "win32":
        if hasattr(_sys.stdout, "reconfigure"):
            _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data = fetch_macro_signals()
    print(json.dumps(data, indent=2))
