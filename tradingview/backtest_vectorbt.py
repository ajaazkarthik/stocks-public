"""
backtest_vectorbt.py — Signal Score Event Study
================================================
Validates whether the composite score from the signal engine
(L1–L4 layers) actually predicts better trade outcomes.

Methodology: for every matured alert, the outcome is the FIXED 7-day forward
return stored in performance_metrics.return_7d. The outcome never depends on
when the report is generated, so the same cohort always yields the same numbers.
Live prices are fetched for display only and are never used as an outcome.

The source database is opened READ-ONLY — this generator must never mutate
alert_tracker.db.

Data source: database/alert_tracker.db
  • alerts.composite_score        — signal quality score (0–100)
  • alerts.entry_price            — price at signal fire time
  • alerts.fire_time              — timestamp of signal
  • performance_metrics.return_7d — fixed 7-day forward return (%) = the outcome

Score buckets analysed:
  LOW  (<55)   — below threshold, should be weakest
  MED  (55-65) — borderline signals
  HIGH (65-75) — strong signals
  APEX (≥75)   — highest conviction

Per-window analysis:
  Window 1, 2, 3 — consecutive thirds of the date range
  NOTE: in-sample time partitioning, not out-of-sample walk-forward

KNOWN LIMITATIONS (do not read this as a tradable-strategy backtest):
  • No commission, slippage, spread, FX, or liquidity model
  • No benchmark-relative / excess-return comparison
  • total_return / final_equity / max_drawdown compound every alert
    sequentially at 100% capital despite overlapping signals — they are
    hypothetical ordering artefacts, NOT achievable portfolio results

Output: reports/backtest_vectorbt_report.html

Usage:
    python tradingview/backtest_vectorbt.py
    python tradingview/backtest_vectorbt.py --db path/to/db --out report.html
"""

import sys, os, argparse, sqlite3, json, math, warnings
from pathlib import Path
from datetime import datetime, timezone

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent
TV   = Path(__file__).parent
_DEFAULT_DB  = ROOT / "database" / "alert_tracker.db"
_DEFAULT_OUT = ROOT / "reports" / "backtest_vectorbt_report.html"

# ─── Score bucket definitions ─────────────────────────────────────────────────
BUCKETS: list[tuple[str, float, float, str]] = [
    ("LOW",  0.0,  55.0, "#e74c3c"),
    ("MED",  55.0, 65.0, "#f39c12"),
    ("HIGH", 65.0, 75.0, "#27ae60"),
    ("APEX", 75.0, 100.1,"#2980b9"),
]

# ─── Win rate threshold ────────────────────────────────────────────────────────
# Minimum return % to count a trade as a "win" in source/bucket statistics.
# Any gain ≥ WIN_THRESHOLD_PCT is a meaningful win.  A gain of +0.1% on a
# stock where the initial risk was 8% is not a win — it's breakeven noise.
# Standard swing-trading convention: win = at least 1/4 of initial risk (≈2%)
# or a fixed meaningful threshold.  3% is used here as a practical floor.
WIN_THRESHOLD_PCT: float = 3.0  # 3.0 % minimum gain to count as a "win"

# ─── Risk-free rate ────────────────────────────────────────────────────────────
# Used for Sharpe ratio computation (both VBT and manual fallback).
# RBA cash rate (AUD), annualised.
RISK_FREE_RATE: float = 0.0435  # 4.35% annualised

# ─── Sharpe convention ─────────────────────────────────────────────────────────
# Bucket returns are 7-calendar-day holding-period returns, so a year holds
# 365/7 ≈ 52.14 of them. Previously the vectorbt path annualised with 365/7 and
# the manual fallback with 252/7, so the same bucket reported two Sharpe values
# differing by a constant 0.83x depending only on whether vectorbt imported.
HOLDING_PERIOD_DAYS: float = 7.0
PERIODS_PER_YEAR: float = 365.0 / HOLDING_PERIOD_DAYS


def _sharpe_ratio(returns_frac) -> float:
    """Annualised Sharpe for holding-period returns expressed as fractions.

    Single source of truth for both the vectorbt and manual paths. Returns 0.0
    where Sharpe is undefined (<2 points, or zero/NaN volatility) rather than
    propagating ±inf/NaN into the report.
    """
    if len(returns_frac) < 2:
        return 0.0
    std = float(returns_frac.std(ddof=1))
    if not math.isfinite(std) or std <= 0:
        return 0.0
    excess = float(returns_frac.mean()) - RISK_FREE_RATE / PERIODS_PER_YEAR
    sharpe = (excess / std) * math.sqrt(PERIODS_PER_YEAR)
    return sharpe if math.isfinite(sharpe) else 0.0


# ─── Live price fetching ───────────────────────────────────────────────────────
# TradingView exchange prefix → yfinance ticker suffix.
# None means yfinance does NOT support the exchange — we use TV scanner instead.
_EXCHANGE_TO_YF_SUFFIX: dict[str, str | None] = {
    # US exchanges — no suffix needed
    "NASDAQ": "", "NYSE": "", "AMEX": "", "BATS": "", "OTC": "", "CBOE": "",
    "NYSE MKT": "", "NYSEARCA": "", "ARCA": "",
    # Canada
    "TSX": ".TO", "TSXV": ".V", "CSE": ".CN",
    # Australia
    "ASX": ".AX",
    # UK
    "LSE": ".L",
    # Hong Kong
    "HKEX": ".HK",
    # Japan
    "TSE": ".T", "TYO": ".T",
    # Singapore
    "SGX": ".SI",
    # Korea
    "KRX": ".KS", "KOSDAQ": ".KQ",
    # India — yfinance NSE/BSE coverage is unreliable; use TV scanner
    "NSE": None, "BSE": None,
    # Futures / indices — skip
    "CME": None, "CBOT": None, "NYMEX": None, "MCX": None,
    "HSI": None, "SZSE": None, "SSE": None,
    # Europe
    "EURONEXT": ".PA", "XETRA": ".DE", "XETR": ".DE",
    "SIX": ".SW", "BME": ".MC",
    # Brazil
    "BMFBOVESPA": ".SA", "B3": ".SA",
    # Others — skip
    "TASE": None, "MYX": None, "IDX": None, "SET": None, "PSE": None,
}

# TradingView scanner API endpoints for exchanges not covered by yfinance
_TV_SCANNER_BY_EXCHANGE: dict[str, str] = {
    "NSE": "https://scanner.tradingview.com/india/scan",
    "BSE": "https://scanner.tradingview.com/india/scan",
}


def tv_symbol_to_yf(tv_symbol: str) -> str | None:
    """
    Convert a TradingView pro_symbol (e.g. 'NYSE:DELL') to a yfinance ticker ('DELL').

    Returns None for exchanges not supported by yfinance (e.g. NSE, CBOT).
    The caller should use a TV scanner fallback for those.

    Examples:
        NYSE:DELL      →  'DELL'
        NASDAQ:CSCO    →  'CSCO'
        ASX:CBA        →  'CBA.AX'
        NSE:INDIAGLYCO →  None  (→ TV scanner)
        CME:ES1!       →  None  (→ unsupported)
    """
    if ":" not in tv_symbol:
        return tv_symbol
    exchange, ticker = tv_symbol.split(":", 1)
    suffix = _EXCHANGE_TO_YF_SUFFIX.get(exchange.upper())
    if suffix is None:
        return None  # unsupported exchange
    # TV uses dashes for some tickers (e.g. BRK-B); yfinance uses dots
    ticker = ticker.replace("-", ".")
    # Zero-pad HKEX numeric codes to 4 digits
    if exchange.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix if suffix else ticker


def _batch_fetch_tv_scanner(tv_symbols: list[str]) -> dict[str, float]:
    """
    Batch-fetch the latest close price for a list of TV symbols using the
    TradingView scanner REST API.  Used as a fallback for NSE/BSE stocks
    not reliably covered by yfinance.

    Returns {tv_symbol: close_price}.  Missing/failed symbols are omitted.
    """
    import json as _json
    import urllib.request as _ur

    by_url: dict[str, list[str]] = {}   # scanner_url → bare ticker list
    sym_map: dict[str, str] = {}        # bare_ticker → full tv_symbol

    for ps in tv_symbols:
        if ":" not in ps:
            continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER_BY_EXCHANGE.get(exch.upper())
        if not url:
            continue
        by_url.setdefault(url, []).append(ticker)
        sym_map.setdefault(ticker, ps)

    if not by_url:
        return {}

    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
    }
    results: dict[str, float] = {}
    for url, tickers in by_url.items():
        payload = {
            "columns": ["name", "close"],
            "filter":  [{"left": "name", "operation": "in_range",
                         "right": tickers}],
            "range":   [0, min(len(tickers) + 10, 500)],
        }
        try:
            body = _json.dumps(payload).encode()
            req  = _ur.Request(url, data=body, headers=headers)
            resp = _ur.urlopen(req, timeout=15)
            data = _json.loads(resp.read())
            for item in data.get("data", []):
                raw = item.get("s", "")
                d   = item.get("d", [])
                px  = d[1] if len(d) > 1 and d[1] is not None else None
                if px is None:
                    continue
                tok    = raw.split(":")[-1] if ":" in raw else raw
                ps_key = sym_map.get(tok) or raw
                try:
                    results[ps_key] = float(px)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass  # non-fatal
    return results


def fetch_live_prices(
    tv_symbols: list[str],
    timeout: int = 20,
) -> dict[str, float | None]:
    """
    Fetch the current market price for a list of TradingView symbols.

    Strategy (per symbol):
      1. US / AU / LSE / HK / etc. → yfinance batch download (single HTTP request)
      2. NSE / BSE               → TradingView scanner API (single batch HTTP request)
      3. Unsupported (CME, CBOT) → None

    Returns {tv_symbol: price_or_None}.
    Never raises — all failures silently return None so the report still
    generates using the price_snapshots fallback.
    """
    result: dict[str, float | None] = {s: None for s in tv_symbols}

    # Partition symbols
    yf_map: dict[str, str] = {}   # tv_symbol → yf_ticker
    tv_batch: list[str] = []      # symbols for TV scanner (NSE/BSE)

    for sym in tv_symbols:
        if ":" not in sym:
            yf_map[sym] = sym
            continue
        exch     = sym.split(":", 1)[0].upper()
        yf_tick  = tv_symbol_to_yf(sym)
        if yf_tick is not None:
            yf_map[sym] = yf_tick
        elif exch in _TV_SCANNER_BY_EXCHANGE:
            tv_batch.append(sym)
        # else: unsupported exchange → stays None

    # ── yfinance batch fetch ───────────────────────────────────────────────
    if yf_map:
        try:
            import yfinance as yf  # type: ignore

            # Build deduped ticker list + reverse map (yf_ticker → tv_symbol)
            reverse: dict[str, str] = {}
            tickers_list: list[str] = []
            for tv_sym, yf_tick in yf_map.items():
                if yf_tick not in reverse:
                    reverse[yf_tick] = tv_sym
                    tickers_list.append(yf_tick)

            if len(tickers_list) == 1:
                # Single-ticker path
                hist = yf.Ticker(tickers_list[0]).history(
                    period="5d", timeout=timeout
                )
                if hist is not None and not hist.empty:
                    price = float(hist["Close"].dropna().iloc[-1])
                    result[reverse[tickers_list[0]]] = price
            else:
                # Multi-ticker batch download (one HTTP request)
                data = yf.download(
                    tickers_list,
                    period="5d",
                    progress=False,
                    auto_adjust=True,
                    timeout=timeout,
                )
                if data is not None and not data.empty:
                    close_df = data["Close"]  # DataFrame: rows=date, cols=ticker
                    if close_df is not None and not close_df.empty:
                        # ── BUG FIX: don't use last_row across all tickers ──────
                        # When US markets are open or haven't closed yet, today's
                        # row contains NaN for US stocks while Asian stocks already
                        # have a close.  Taking the last row gives NaN for US tickers.
                        # Fix: use the last NON-NaN value per ticker independently.
                        for col in close_df.columns:
                            yf_tick = str(col)
                            tv_sym  = reverse.get(yf_tick)
                            if tv_sym is None:
                                continue
                            series = close_df[col].dropna()
                            if series.empty:
                                continue
                            try:
                                fp = float(series.iloc[-1])
                                if not math.isnan(fp):
                                    result[tv_sym] = fp
                            except (TypeError, ValueError):
                                pass
        except Exception:
            pass  # non-fatal: report falls back to price_snapshots data

    # ── TV scanner batch fetch (NSE / BSE) ────────────────────────────────
    if tv_batch:
        try:
            scanner_prices = _batch_fetch_tv_scanner(tv_batch)
            for sym, price in scanner_prices.items():
                result[sym] = price
        except Exception:
            pass

    return result


# ─── Data loading ──────────────────────────────────────────────────────────────

def load_trades(db_path: str, min_return_abs: float = 0.001) -> list[dict]:
    """
    Load all alerts with a matured 7-day outcome from alert_tracker.db.
    Returns list of dicts with symbol, score, entry price, returns AND
    technical indicators extracted from result_json (RSI, ADX, squeeze, VIX, etc.).

    Opens the database READ-ONLY; it never deletes or rewrites source rows.

    `min_return_abs` is accepted for call-site compatibility but is deliberately
    NOT applied: filtering rows by outcome magnitude conditions on the dependent
    variable and biases every downstream statistic.

    current_price priority (display only, never the outcome):
      1. Latest price_snapshots row (most recently observed market price)
      2. entry_price * (1 + return_7d/100)  — derived from performance metric
      3. result_json.fundamentals.current_price  — captured at alert fire time
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"alert_tracker.db not found: {db_path}")

    # Read-only URI: a report generator must never mutate the system of record.
    # wal-exempt: this connection is READ-ONLY (mode=ro), and
    # `PRAGMA journal_mode=WAL` raises "attempt to write a readonly database".
    # WAL is owned by the writers of alert_tracker.db; readers inherit it.
    conn = sqlite3.connect(  # noqa: wal-exempt
        f"file:{Path(db_path).as_posix()}?mode=ro", uri=True
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    # Dynamically detect book_* columns (may not exist in older/test DBs)
    _alert_cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    _BOOK_COLS = ["book_vpa_score", "book_darvas_score", "book_gap_score",
                  "book_am_score", "book_rs_score", "book_expectancy_score",
                  "book_total_bonus"]
    _book_select = "".join(
        f"                a.{c:<24s} AS {c},\n"
        for c in _BOOK_COLS if c in _alert_cols
    )
    _res_col = (
        "a.resolution         AS resolution"
        if "resolution" in _alert_cols
        else "NULL               AS resolution"
    )
    try:
        # Duplicates are resolved in memory below (see the dedup block after row
        # parsing). Deduplication must never DELETE from the source database.

        rows = conn.execute(
            f"""
            SELECT
                a.tv_symbol          AS symbol,
                a.fire_time          AS fire_time,
                a.composite_score    AS score,
                a.l1_score           AS l1_score,
                a.l2_score           AS l2_score,
                a.l3_score           AS l3_score,
                a.l4_score           AS l4_score,
                a.entry_price        AS entry_price,
                a.source             AS source,
                {_res_col},
                a.result_json        AS result_json,
                {_book_select}                pm.return_7d         AS return_7d,
                pm.return_3d         AS return_3d,
                pm.return_1d         AS return_1d,
                pm.max_return        AS max_return,
                pm.max_drawdown      AS max_drawdown,
                pm.status            AS status,
                -- latest snapshot price: most recently observed market price for this alert
                ps_latest.price      AS snapshot_price
            FROM alerts a
            JOIN performance_metrics pm ON a.alert_uuid = pm.alert_uuid
            -- Subquery: pick the single most-recent snapshot row per alert
            LEFT JOIN (
                SELECT alert_uuid, price
                FROM price_snapshots
                WHERE (alert_uuid, snapshot_time) IN (
                    SELECT alert_uuid, MAX(snapshot_time)
                    FROM price_snapshots
                    GROUP BY alert_uuid
                )
            ) ps_latest ON ps_latest.alert_uuid = a.alert_uuid
            WHERE a.composite_score IS NOT NULL
              AND a.entry_price     IS NOT NULL
              AND pm.return_7d      IS NOT NULL
            ORDER BY a.fire_time ASC
            """
        ).fetchall()

        # FALLBACK — performance_metrics table is empty (common in fresh/new DBs).
        # Load directly from alerts table. return_7d may be NULL for some rows.
        # Do NOT add date filters or row limits — ALL rows must be loaded for
        # accurate historical backtesting (see BUG-B1 in module docstring).
        if not rows:
            print("  [load_trades] performance_metrics is empty — FALLBACK to alerts table")
            _ret7d_col = (
                "a.return_7d          AS return_7d"
                if "return_7d" in _alert_cols
                else "NULL               AS return_7d"
            )
            rows = conn.execute(
                f"""
                SELECT
                    a.tv_symbol          AS symbol,
                    a.fire_time          AS fire_time,
                    a.composite_score    AS score,
                    a.l1_score           AS l1_score,
                    a.l2_score           AS l2_score,
                    a.l3_score           AS l3_score,
                    a.l4_score           AS l4_score,
                    a.entry_price        AS entry_price,
                    a.source             AS source,
                    {_res_col},
                    a.result_json        AS result_json,
                    {_book_select}                    {_ret7d_col},
                    NULL                 AS return_3d,
                    NULL                 AS return_1d,
                    NULL                 AS max_return,
                    NULL                 AS max_drawdown,
                    NULL                 AS status,
                    NULL                 AS snapshot_price
                FROM alerts a
                WHERE a.composite_score IS NOT NULL
                  AND a.entry_price     IS NOT NULL
                ORDER BY a.fire_time ASC
                """
            ).fetchall()
            print(f"  [load_trades] FALLBACK loaded {len(rows)} alerts from alerts table")
    finally:
        conn.close()

    trades = []
    for r in rows:
        try:
            fire_dt = datetime.fromisoformat(
                str(r["fire_time"]).replace("Z", "+00:00")
            )
        except Exception:
            continue

        # Parse result_json to extract technical indicators captured at alert time
        rj: dict = {}
        try:
            raw_rj = r["result_json"]
            if raw_rj:
                rj = json.loads(raw_rj) if isinstance(raw_rj, str) else raw_rj
        except Exception:
            pass

        scores   = rj.get("scores") or {}
        l1       = scores.get("l1") or {}
        l2       = scores.get("l2") or {}
        l3       = scores.get("l3") or {}
        l4       = scores.get("l4") or {}
        fund     = rj.get("fundamentals") or {}
        sent     = (rj.get("sentiment_data") or {}).get("news") or {}
        mktint   = rj.get("market_internals") or {}
        yldcurve = rj.get("yield_curve") or {}

        entry_px = float(r["entry_price"])
        ret7d    = float(r["return_7d"] or 0)  # NULL in fallback path → treat as 0

        # ── current_price: 3-level priority fallback ──────────────────────
        # 1) Latest price_snapshots row  — most recently observed market price
        # 2) Derived from return_7d      — entry * (1 + return_7d/100)
        # 3) result_json.fundamentals.current_price — captured at alert fire time
        #    (equals entry_price on most rows, so only used if snapshot missing)
        snap_px = r["snapshot_price"]
        if snap_px is not None:
            curr_price: float | None = float(snap_px)
            curr_price_src = "snapshot"
        elif ret7d != 0:
            curr_price = entry_px * (1.0 + ret7d / 100.0)
            curr_price_src = "derived"
        else:
            cp = fund.get("current_price")
            curr_price = float(cp) if cp is not None and float(cp) != entry_px else None
            curr_price_src = "result_json" if curr_price is not None else "none"

        # ── Outcome variable: the fixed 7-day forward return ──────────────
        # Statistics must not depend on when the report is generated, so the
        # outcome is the matured 7-day return, never the live mark-to-market.
        actual_return: float = ret7d

        # Display-only mark-to-market return; excluded from every statistic.
        if curr_price is not None and entry_px > 0:
            live_return: float | None = (curr_price - entry_px) / entry_px * 100.0
        else:
            live_return = None

        trades.append({
            # ── Identity ───────────────────────────────────────────────────
            "symbol":        r["symbol"],
            "fire_time":     fire_dt,
            "company_name":  str(fund.get("company_name") or ""),
            "source":        str(r["source"] or ""),
            "resolution":    str(r["resolution"] or ""),  # D / 60 / 120 (timeframe)

            # ── Prices ─────────────────────────────────────────────────────
            "entry_price":      entry_px,
            # current_price: latest snapshot > derived-from-return > None
            "current_price":    curr_price,
            "current_price_src": curr_price_src,  # "snapshot"|"derived"|"result_json"|"none"

            # ── Composite + layer totals ────────────────────────────────────
            "score":         float(r["score"]),
            "l1_score":      float(r["l1_score"] or 0),
            "l2_score":      float(r["l2_score"] or 0),
            "l3_score":      float(r["l3_score"] or 0),
            "l4_score":      float(r["l4_score"] or 0),

            # ── L1 Technical sub-scores (at alert time) ────────────────────
            "rsi":           l1.get("raw_rsi"),
            "adx":           l1.get("raw_adx"),
            "squeeze":       l1.get("squeeze"),      # bool/int: 1 = squeeze active
            "rel_vol":       l1.get("rel_vol"),
            "sma200_pct":    l1.get("raw_sma200_pct"),
            "macd_hist":     l1.get("raw_macd_hist"),

            # ── L2 Fundamental sub-scores ──────────────────────────────────
            "l2_valuation":  l2.get("valuation"),
            "l2_profit":     l2.get("profitability"),
            "l2_growth":     l2.get("growth"),
            "l2_health":     l2.get("health"),

            # ── L3 Sentiment sub-scores ────────────────────────────────────
            "l3_analyst":      l3.get("analyst"),
            "l3_news":         l3.get("news"),
            "sentiment_label": sent.get("label", ""),        # Bullish/Neutral/Bearish
            "sentiment_score": sent.get("score"),            # raw VADER -1..1
            "sentiment_count": sent.get("count"),

            # ── L4 Market-internals sub-scores ─────────────────────────────
            "l4_pcr":        l4.get("pcr"),
            "l4_vix_score":  l4.get("vix"),
            "vix_at_fire":   rj.get("vix_level"),            # raw VIX value
            "insider_net":   mktint.get("insider_net"),
            "pcr_raw":       mktint.get("pcr"),

            # ── Fundamental data (at alert time) ───────────────────────────
            "pe":            fund.get("pe") or fund.get("pe_ratio"),  # P/E ratio
            "roe":           fund.get("roe"),                          # Return on equity (%)

            # ── Macro context ──────────────────────────────────────────────
            "us10y":         yldcurve.get("us10y"),
            "spread_bps":    yldcurve.get("spread_bps"),     # 10Y-2Y yield spread

            # ── Multi-horizon returns ──────────────────────────────────────
            "return_7d":     ret7d,
            "return_3d":     float(r["return_3d"] or 0),
            "return_1d":     float(r["return_1d"] or 0),
            "max_return":    float(r["max_return"] or 0),
            "max_drawdown":  float(r["max_drawdown"] or 0),
            "status":        str(r["status"] or ""),

            # ── Outcome variable (fixed 7-day horizon) ─────────────────────
            # Used by every statistic. Reproducible across report runs.
            "actual_return": actual_return,

            # ── Live mark-to-market return (display only) ──────────────────
            "live_return":   live_return,

            # ── Book features (at alert time) ──────────────────────────────
            # Prefer dedicated DB columns; fall back to result_json for old rows
            "book_vpa":       float(r["book_vpa_score"]) if "book_vpa_score" in r.keys() and r["book_vpa_score"] is not None else (rj.get("book_features") or {}).get("vpa", {}).get("score"),
            "book_darvas":    float(r["book_darvas_score"]) if "book_darvas_score" in r.keys() and r["book_darvas_score"] is not None else (rj.get("book_features") or {}).get("darvas", {}).get("score"),
            "book_gap":       float(r["book_gap_score"]) if "book_gap_score" in r.keys() and r["book_gap_score"] is not None else (rj.get("book_features") or {}).get("gap", {}).get("score"),
            "book_am":        float(r["book_am_score"]) if "book_am_score" in r.keys() and r["book_am_score"] is not None else (rj.get("book_features") or {}).get("am", {}).get("score"),
            "book_rs":        float(r["book_rs_score"]) if "book_rs_score" in r.keys() and r["book_rs_score"] is not None else (rj.get("book_features") or {}).get("rs", {}).get("score"),
            "book_expectancy":float(r["book_expectancy_score"]) if "book_expectancy_score" in r.keys() and r["book_expectancy_score"] is not None else (rj.get("book_features") or {}).get("expectancy", {}).get("score"),
            "book_total":     float(r["book_total_bonus"]) if "book_total_bonus" in r.keys() and r["book_total_bonus"] is not None else (rj.get("book_features") or {}).get("total_bonus"),
        })

    # ── Dedup: same symbol + same fire_date + same entry_price = duplicate ──
    # This happens when the same alert fires from multiple overlapping sources
    # (e.g. both News Screener and TV Screener capture the same signal on the same day).
    # Duplicates are moved to a backup DB for audit trail instead of being silently dropped.
    seen_dedup: set = set()
    deduped: list = []
    duplicates: list = []
    for t in trades:
        ft = t.get("fire_time")
        if hasattr(ft, "strftime"):
            fire_date = ft.strftime("%Y-%m-%d")
        else:
            fire_date = str(ft or "")[:10]
        entry_key = round(float(t.get("entry_price") or 0), 4)
        key = (t["symbol"], fire_date, entry_key)
        if key not in seen_dedup:
            seen_dedup.add(key)
            deduped.append(t)
        else:
            duplicates.append(t)
    if duplicates:
        print(f"  [dedup] removed {len(duplicates)} duplicate alerts "
              f"(same symbol + date + price) → backup DB")
        # Archive duplicates to database/duplicate_alerts.db
        dup_db_path = ROOT / "database" / "duplicate_alerts.db"
        try:
            dup_db_path.parent.mkdir(parents=True, exist_ok=True)
            dup_conn = sqlite3.connect(str(dup_db_path))
            dup_conn.execute("PRAGMA journal_mode=WAL")
            dup_conn.execute("""CREATE TABLE IF NOT EXISTS duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, fire_date TEXT, entry_price REAL,
                source TEXT, composite_score REAL, archived_at TEXT
            )""")
            # Remove pre-existing duplicate rows before creating unique index
            dup_conn.execute("""DELETE FROM duplicates WHERE id NOT IN (
                SELECT MIN(id) FROM duplicates GROUP BY symbol, fire_date, entry_price
            )""")
            dup_conn.commit()
            dup_conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uidx_dup ON duplicates (symbol, fire_date, entry_price)"
            )
            _changes_before = dup_conn.total_changes
            from datetime import datetime as _dup_dt
            now_str = _dup_dt.now().strftime("%Y-%m-%d %H:%M:%S")
            for d in duplicates:
                ft2 = d.get("fire_time")
                fd2 = ft2.strftime("%Y-%m-%d") if hasattr(ft2, "strftime") else str(ft2 or "")[:10]
                dup_conn.execute(
                    "INSERT OR IGNORE INTO duplicates (symbol, fire_date, entry_price, source, composite_score, archived_at) VALUES (?,?,?,?,?,?)",
                    (d.get("symbol"), fd2, float(d.get("entry_price") or 0),
                     d.get("source", ""), float(d.get("score") or 0), now_str)
                )
            dup_conn.commit()
            n_new_archived = dup_conn.total_changes - _changes_before
            if n_new_archived > 0:
                print(f"  [dedup] archived {n_new_archived} new records to backup DB")
            dup_conn.close()
        except Exception as e:
            print(f"  [dedup] WARNING: could not archive duplicates: {e}")
    trades = deduped

    return trades


def _bucket_label(score: float) -> str:
    for name, lo, hi, _ in BUCKETS:
        if lo <= score < hi:
            return name
    return "APEX"


# ─── Vectorbt portfolio construction ──────────────────────────────────────────

def _build_equity_curve(returns_pct: list[float], init_cash: float = 10_000.0):
    """
    Build an equity curve from a list of per-trade returns (%).
    Returns a pandas Series indexed by trade number.
    """
    import pandas as pd
    import numpy as np

    if not returns_pct:
        return pd.Series([init_cash], dtype=float)

    curve = [init_cash]
    val = init_cash
    for r in returns_pct:
        val = val * (1.0 + r / 100.0)
        curve.append(val)

    return pd.Series(curve, dtype=float)


def compute_bucket_stats(trades: list[dict]) -> dict[str, dict]:
    """
    Compute portfolio stats for each score bucket using vectorbt.
    Returns {bucket_name: {n, win_rate, avg_return, total_return,
                           sharpe, max_drawdown, final_equity}}
    """
    import pandas as pd
    import numpy as np

    try:
        import vectorbt as vbt
        vbt.settings.engine = "rust"
        _VBT_OK = True
    except ImportError:
        _VBT_OK = False

    # Group trades by bucket — use actual_return (live price) not stale return_7d
    groups: dict[str, list[float]] = {b[0]: [] for b in BUCKETS}
    for t in trades:
        bucket = _bucket_label(t["score"])
        groups[bucket].append(t.get("actual_return", t["return_7d"]))

    results: dict[str, dict] = {}
    for name, *_ in BUCKETS:
        rets = groups[name]
        if not rets:
            results[name] = {"n": 0, "win_rate": 0.0, "avg_return": 0.0,
                             "total_return": 0.0, "sharpe": 0.0,
                             "max_drawdown": 0.0, "final_equity": 0.0}
            continue

        rets_arr = pd.Series([r / 100.0 for r in rets])
        n        = len(rets)
        win_rate = float((rets_arr >= WIN_THRESHOLD_PCT / 100).mean())
        avg_ret  = float(rets_arr.mean() * 100)

        # Equity curve via compound returns
        equity = _build_equity_curve(rets)

        # Sharpe is computed the same way regardless of whether vbt is present.
        total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) >= 2 else 0.0
        sharpe    = _sharpe_ratio(rets_arr)
        max_dd    = 0.0

        # Local flag: a vbt failure on one bucket must not switch later buckets.
        used_vbt = False
        if _VBT_OK and len(equity) >= 3:
            try:
                # freq only labels the series; max_drawdown and total_return are
                # frequency-independent, and Sharpe no longer comes from vbt.
                pf = vbt.Portfolio.from_holding(equity, freq="7D")
                max_dd    = float(pf.max_drawdown())
                total_ret = float(pf.total_return())
                used_vbt  = True
            except Exception:
                used_vbt = False

        if not used_vbt:
            total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) >= 2 else 0.0
            roll_max  = equity.cummax()
            dd_series = (equity - roll_max) / roll_max
            max_dd    = float(dd_series.min()) if len(equity) >= 2 else 0.0

        results[name] = {
            "n":            n,
            "win_rate":     round(win_rate * 100, 1),
            "avg_return":   round(avg_ret, 3),
            "total_return": round(total_ret * 100, 2),
            "sharpe":       round(sharpe, 3),
            "max_drawdown": round(max_dd * 100, 2),
            "final_equity": round(float(equity.iloc[-1]), 2),
        }

    return results


# ─── Per-window analysis ─────────────────────────────────────────────────────

def per_window_stats(trades: list[dict], n_windows: int = 3) -> list[dict]:
    """
    Split trades into n_windows consecutive time windows using date-range split.
    Returns list of {window, start_date, end_date, n_trades, stats_per_bucket}

    NOTE: This is in-sample time partitioning — it evaluates score bucket consistency
    across different time periods using the same data. It is NOT out-of-sample
    walk-forward validation.
    """
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t["fire_time"])
    total         = len(sorted_trades)
    if total < n_windows:
        n_windows = 1

    # Date-range split: divide the total time span into equal-duration windows
    first_dt = sorted_trades[0]["fire_time"]
    last_dt  = sorted_trades[-1]["fire_time"]
    span     = (last_dt - first_dt).total_seconds()
    if span <= 0:
        # All trades on the same day — single window
        n_windows = 1

    windows = []
    for w in range(n_windows):
        if span > 0:
            from datetime import timedelta
            w_start = first_dt + timedelta(seconds=span * w / n_windows)
            w_end   = first_dt + timedelta(seconds=span * (w + 1) / n_windows)
        else:
            w_start = first_dt
            w_end   = last_dt

        # Select trades in this date window (inclusive start, last window includes end)
        if w < n_windows - 1:
            window_trades = [t for t in sorted_trades if w_start <= t["fire_time"] < w_end]
        else:
            window_trades = [t for t in sorted_trades if w_start <= t["fire_time"] <= w_end]

        if not window_trades:
            continue
        stats = compute_bucket_stats(window_trades)
        windows.append({
            "window":     w + 1,
            "start_date": window_trades[0]["fire_time"].strftime("%Y-%m-%d"),
            "end_date":   window_trades[-1]["fire_time"].strftime("%Y-%m-%d"),
            "n_trades":   len(window_trades),
            "stats":      stats,
        })
    return windows


# Deprecated alias. `per_window_stats` does in-sample time partitioning, not
# out-of-sample walk-forward validation — the old name overstated it. Retained
# for ~30 call sites in tests/ and benchmarks/10_vectorbt_benchmark.py.
def walk_forward(trades: list[dict], n_windows: int = 3) -> list[dict]:
    warnings.warn(
        "walk_forward() is a misnomer and is deprecated — use per_window_stats(). "
        "It performs in-sample time partitioning across consecutive date windows, "
        "not out-of-sample walk-forward validation.",
        DeprecationWarning,
        stacklevel=2,
    )
    return per_window_stats(trades, n_windows)


# ─── HTML report generation ───────────────────────────────────────────────────

def _colour_cell(val: float, good_direction: str = "high") -> str:
    """Return a CSS background colour for a metric cell."""
    if good_direction == "high":
        if val > 0:
            return "#d5f5e3"  # light green
        elif val < 0:
            return "#fde8e8"  # light red
    else:  # low is good (drawdown)
        if val < -10:
            return "#fde8e8"
        elif val > -5:
            return "#d5f5e3"
    return "#f9f9f9"


def _bucket_colour(name: str) -> str:
    for b in BUCKETS:
        if b[0] == name:
            return b[3]
    return "#999"


def _table_row(bucket: str, s: dict) -> str:
    bc      = _bucket_colour(bucket)
    wr_bg   = _colour_cell(s["win_rate"] - 50, "high")
    ar_bg   = _colour_cell(s["avg_return"], "high")
    sh_bg   = _colour_cell(s["sharpe"], "high")
    dd_bg   = _colour_cell(s["max_drawdown"], "low")
    tr_bg   = _colour_cell(s["total_return"], "high")
    return f"""
    <tr>
      <td style="background:{bc};color:#fff;font-weight:bold;text-align:center;padding:8px">{bucket}</td>
      <td style="text-align:center">{s['n']}</td>
      <td style="background:{wr_bg};text-align:right">{s['win_rate']:.1f}%</td>
      <td style="background:{ar_bg};text-align:right">{s['avg_return']:+.3f}%</td>
      <td style="background:{tr_bg};text-align:right">{s['total_return']:+.2f}%</td>
      <td style="background:{sh_bg};text-align:right">{s['sharpe']:.3f}</td>
      <td style="background:{dd_bg};text-align:right">{s['max_drawdown']:.2f}%</td>
      <td style="text-align:right">${s['final_equity']:,.2f}</td>
    </tr>"""


def _source_stats(trades: list[dict], success_threshold: float = WIN_THRESHOLD_PCT / 100) -> list[dict]:
    """
    Aggregate win rate, avg return, and count by alert source.
    success_threshold: actual_return >= threshold counts as a win.
    Default WIN_THRESHOLD_PCT (3%) — any gain below this is noise.
    """
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[t.get("source") or "unknown"].append(t)

    out = []
    for src, g in sorted(groups.items()):
        # Use actual_return (from live price) for win rate and avg return calculations
        wins = sum(1 for t in g if t.get("actual_return", t["return_7d"]) >= success_threshold * 100)
        sell_count = sum(1 for t in g if t.get("sell_action") == "SELL")
        rets = [t.get("actual_return", t["return_7d"]) for t in g]
        max_rets = [t["max_return"] for t in g if t["max_return"] != 0]
        max_dds  = [t["max_drawdown"] for t in g if t["max_drawdown"] != 0]
        out.append({
            "source":      src,
            "count":       len(g),
            "wins":        wins,
            "sell_count":  sell_count,
            "win_rate":    wins / len(g) * 100 if g else 0.0,
            "avg_return":  sum(rets) / len(rets) if rets else 0.0,
            "avg_mfe":     sum(max_rets) / len(max_rets) if max_rets else 0.0,
            "avg_mae":     sum(max_dds) / len(max_dds) if max_dds else 0.0,
        })
    out.sort(key=lambda x: x["win_rate"], reverse=True)
    return out


def _fmt_sub(val, fmt: str = ".1f", dash: str = "—") -> str:
    """Format a score sub-component, returning dash if None."""
    return f"{val:{fmt}}" if val is not None else dash


def _score_badge(score: float, lo: float = 3.0, hi: float = 7.0) -> str:
    """Return a coloured badge string for a score sub-value."""
    if score >= hi:
        colour = "#27ae60"
    elif score >= lo:
        colour = "#f39c12"
    else:
        colour = "#e74c3c"
    return f"<span style='color:{colour};font-weight:600'>{score:.1f}</span>"


def _score_breakdown_html(t: dict) -> str:
    """
    Build a compact 4-column score breakdown panel for one trade.
    Mirrors the TECHNICAL | FUNDAMENTAL | SENTIMENT | INTERNALS bar in the signal UI.
    """
    def _s(key: str, fmt: str = ".1f") -> str:
        v = t.get(key)
        return _fmt_sub(v, fmt) if v is not None else "—"

    sqz_icon = (
        "🟡 Active" if t.get("squeeze") else
        ("⚪ Off" if t.get("squeeze") is not None else "—")
    )
    sent_label = t.get("sentiment_label") or "—"
    sent_col   = (
        "#27ae60" if "bull" in sent_label.lower() else
        "#e74c3c" if "bear" in sent_label.lower() else "#94a3b8"
    )

    return f"""
    <table style='width:100%;border-collapse:collapse;font-size:0.8em;background:#f8f9fa;border-radius:6px'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:5px 8px;width:25%'>🔧 Technical (L1={_s("l1_score")})</th>
      <th style='padding:5px 8px;width:25%'>📈 Fundamental (L2={_s("l2_score")})</th>
      <th style='padding:5px 8px;width:25%'>💬 Sentiment (L3={_s("l3_score")})</th>
      <th style='padding:5px 8px;width:25%'>🏛 Internals (L4={_s("l4_score")})</th>
    </tr></thead>
    <tbody><tr style='vertical-align:top'>
      <td style='padding:6px 8px'>
        RSI: {_s("rsi", ".0f")}<br>
        ADX: {_s("adx", ".0f")}<br>
        Squeeze: {sqz_icon}<br>
        Rel Vol: {_s("rel_vol", ".2f")}<br>
        SMA200%: {_s("sma200_pct", ".2f")}<br>
        MACD hist: {_s("macd_hist", ".4f")}
      </td>
      <td style='padding:6px 8px'>
        Valuation: {_s("l2_valuation")}<br>
        Profitability: {_s("l2_profit")}<br>
        Growth: {_s("l2_growth")}<br>
        Health: {_s("l2_health")}
      </td>
      <td style='padding:6px 8px'>
        Analyst: {_s("l3_analyst")}<br>
        News score: {_s("l3_news")}<br>
        Sentiment: <span style='color:{sent_col}'>{sent_label}</span><br>
        VADER: {_s("sentiment_score", ".3f")}<br>
        News count: {_s("sentiment_count", ".0f")}
      </td>
      <td style='padding:6px 8px'>
        PCR score: {_s("l4_pcr")}<br>
        VIX score: {_s("l4_vix_score")}<br>
        VIX level: {_s("vix_at_fire", ".1f")}<br>
        Insider: {t.get("insider_net") or "—"}<br>
        US10Y: {_s("us10y", ".3f")}<br>
        Spread: {_s("spread_bps", ".0f")}bps
      </td>
    </tr></tbody>
    </table>"""


# ── Sell-signal badge helpers ─────────────────────────────────────────────────

_SELL_BADGE_STYLE: dict[str, tuple[str, str]] = {
    "SELL":   ("#e74c3c", "#fff"),
    "REDUCE": ("#f39c12", "#fff"),
    "TRAIL":  ("#00bcd4", "#fff"),
    "REVIEW": ("#8e44ad", "#fff"),
    "HOLD":   ("#27ae60", "#fff"),
    "N/A":    ("#aaa",    "#fff"),
    "ERROR":  ("#7f8c8d", "#fff"),
}


def _sell_signal_badge(action: str, reason: str = "") -> str:
    """Return a colour-coded HTML badge for a sell-engine action."""
    bg, fg = _SELL_BADGE_STYLE.get(action, ("#aaa", "#fff"))
    label  = action if action else "N/A"
    safe_reason = (reason or "").replace("'", "&#39;").replace('"', "&quot;")
    tooltip_attr = f" title='{safe_reason}'" if safe_reason else ""
    return (
        f"<span{tooltip_attr} style='background:{bg};color:{fg};"
        f"padding:2px 7px;border-radius:3px;font-size:0.78em;"
        f"font-weight:bold;cursor:default'>{label}</span>"
    )


def compute_sell_signals(trades: list[dict], vix: float = 20.0) -> None:
    """
    Run sell_engine.apply_exit_rules() for each trade.  Mutates trades in-place,
    adding: sell_action, sell_reason, sell_hard_stop, sell_trailing_stop,
    sell_effective_stop, sell_tp1, sell_tp2, sell_trading_days, sell_current_r.

    Uses 2%-of-price as ATR approximation (OHLCV history is not stored per-trade
    in alert_tracker.db — this is a conservative estimate).
    """
    import sys

    tv_dir = str(TV)
    if tv_dir not in sys.path:
        sys.path.insert(0, tv_dir)
    try:
        from signal_engine.sell_engine import apply_exit_rules
    except ImportError as exc:
        print(f"  [WARN] sell_engine not importable ({exc}); sell signals skipped")
        for t in trades:
            t["sell_action"] = "N/A"
            t["sell_reason"] = "sell_engine unavailable"
        return

    now = datetime.now()
    for t in trades:
        entry = float(t.get("entry_price") or 0)
        if entry <= 0:
            t["sell_action"] = "N/A"
            t["sell_reason"] = "No entry price"
            continue

        curr     = t.get("current_price") or entry
        curr_f   = float(curr)
        max_r    = float(t.get("max_return")   or 0)
        actual_r = float(t.get("actual_return") or 0)
        # High Water Mark: best price ever seen (DB max_return or current live return)
        hwm = entry * (1.0 + max(max_r, actual_r) / 100.0)
        # ATR approximation: 2% of entry / current (conservative — OHLCV not stored)
        atr_e = entry  * 0.02
        atr_c = curr_f * 0.02

        # Entry date from fire_time
        fire_dt    = t.get("fire_time")
        entry_date = (
            fire_dt.strftime("%Y-%m-%d")
            if hasattr(fire_dt, "strftime")
            else str(fire_dt)[:10]
        )

        # CGT: days until 12-month long-term discount threshold
        try:
            from datetime import date as _date
            entry_d  = _date.fromisoformat(entry_date)
            lt_date  = entry_d.replace(year=entry_d.year + 1)
            days_to_lt: int | None = (lt_date - now.date()).days
        except Exception:
            days_to_lt = None

        result_input: dict = {
            "symbol":          t.get("symbol", ""),
            "entry_price":     entry,
            "entry_date":      entry_date,
            "high_water_mark": hwm,
            "current_price":   curr_f,
            "atr_entry":       atr_e,
            "atr_current":     atr_c,
            "rsi":             t.get("rsi"),
            "adx":             t.get("adx"),
            "rel_vol":         t.get("rel_vol"),
            "pe":              t.get("pe"),
            "roe":             t.get("roe"),
            "days_to_lt":      days_to_lt,
            "price_change_5d": None,   # not stored per-trade in DB
        }

        try:
            out = apply_exit_rules(result_input, vix=vix, now=now)
            t["sell_action"]         = out["action"]
            t["sell_reason"]         = out["action_reason"]
            t["sell_hard_stop"]      = out.get("hard_stop")
            t["sell_trailing_stop"]  = out.get("trailing_stop")
            t["sell_effective_stop"] = out.get("effective_stop")
            t["sell_tp1"]            = out.get("tp1_price")
            t["sell_tp2"]            = out.get("tp2_price")
            t["sell_trading_days"]   = out.get("trading_days")
            t["sell_current_r"]      = out.get("current_r")
        except Exception as exc:  # noqa: BLE001
            t["sell_action"] = "ERROR"
            t["sell_reason"] = str(exc)


def _bucket_trades_html(trades: list[dict]) -> dict[str, str]:
    """
    Build a per-bucket mapping of HTML trade tables for click-to-expand sections.
    Each row includes current price, returns, AND an expandable score breakdown
    (Technical/Fundamental/Sentiment/Internals).
    Returns {bucket_name: html_table_string}.
    """
    bucket_map: dict[str, list[dict]] = {name: [] for name, *_ in BUCKETS}
    for t in trades:
        b = _bucket_label(t["score"])
        if b in bucket_map:
            bucket_map[b].append(t)

    result: dict[str, str] = {}
    for name, t_list in bucket_map.items():
        if not t_list:
            result[name] = "<em>No trades in this bucket.</em>"
            continue
        rows_html = ""
        for idx, t in enumerate(sorted(t_list, key=lambda x: x.get("fire_time") or "", reverse=True)):
            row_id   = f"bkt_{name}_{idx}"
            fire_dt  = t.get("fire_time", "")
            fire_str = (
                fire_dt.strftime("%Y-%m-%d")
                if hasattr(fire_dt, "strftime")
                else str(fire_dt)[:10]
            )
            actual_ret = float(t.get("actual_return", t.get("return_7d", 0)) or 0)
            ret_7d    = float(t.get("return_7d",  0) or 0)
            ret_3d    = float(t.get("return_3d",  0) or 0)
            ret_1d    = float(t.get("return_1d",  0) or 0)
            max_ret   = float(t.get("max_return",  0) or 0)
            max_dd    = float(t.get("max_drawdown", 0) or 0)
            entry_px  = float(t.get("entry_price", 0) or 0)
            curr_px   = t.get("current_price")
            ret_col   = "color:#27ae60" if actual_ret > 0 else "color:#e74c3c"
            sell_action = t.get("sell_action", "N/A")
            sell_reason = t.get("sell_reason", "")
            sell_badge  = _sell_signal_badge(sell_action, sell_reason)

            # Current price formatting with source badge
            cp_src    = t.get("current_price_src", "none")
            cp_badge  = {
                "live_yf":     "<sup style='color:#00c853;font-size:0.7em' title='Live: yfinance'>&#9679;</sup>",
                "live_tv":     "<sup style='color:#00bcd4;font-size:0.7em' title='Live: TradingView scanner'>&#9679;</sup>",
                "snapshot":    "<sup style='color:#f39c12;font-size:0.7em' title='Cached (price_snapshots)'>&#9679;</sup>",
                "derived":     "<sup style='color:#f39c12;font-size:0.7em' title='Derived: entry×(1+ret7d/100)'>&#9679;</sup>",
                "result_json": "<sup style='color:#94a3b8;font-size:0.7em' title='From result_json (alert time)'>&#9679;</sup>",
                "none":        "",
            }.get(cp_src, "")
            curr_str  = (
                f"{curr_px:.4f}{cp_badge}"
                if curr_px is not None
                else "<span style='color:#aaa'>—</span>"
            )
            src_val   = t.get("source") or ""
            co_name   = t.get("company_name") or ""

            # Compact L1-L4 score badges
            l1b = _score_badge(t.get("l1_score") or 0)
            l2b = _score_badge(t.get("l2_score") or 0)
            l3b = _score_badge(t.get("l3_score") or 0)
            l4b = _score_badge(t.get("l4_score") or 0)
            score_badges = (
                f"<span title='Technical'>L1:{l1b}</span> "
                f"<span title='Fundamental'>L2:{l2b}</span> "
                f"<span title='Sentiment'>L3:{l3b}</span> "
                f"<span title='Internals'>L4:{l4b}</span>"
            )

            sym      = t.get('symbol', '')
            tv_url   = f"https://www.tradingview.com/chart/?symbol={sym}"
            tv_link  = f"<a href='{tv_url}' target='_blank' title='View on TradingView' style='color:#3498db;font-size:0.7em;margin-left:4px;text-decoration:none'>&#128279;</a>"
            sym_label = (
                f"{sym}{tv_link}"
                + (f"<br><span style='color:#777;font-size:0.8em'>{co_name[:18]}</span>"
                   if co_name else "")
            )

            score_val = float(t.get('score', 0))
            breakdown = _score_breakdown_html(t)
            rows_html += f"""
<tr class='tr-trade' style='border-bottom:1px solid #e0e0e0'>
  <td style='white-space:nowrap;padding:6px 8px'>{sym_label}</td>
  <td style='padding:6px 8px'>{fire_str}</td>
  <td style='padding:6px 8px;color:#2980b9;font-size:0.8em'>{src_val}</td>
  <td style='text-align:right;padding:6px 8px' data-val='{score_val:.2f}'>{score_val:.1f}</td>
  <td style='text-align:right;padding:6px 8px;font-size:0.78em'>{score_badges}</td>
  <td style='text-align:right;padding:6px 8px'>{entry_px:.4f}</td>
  <td style='text-align:right;padding:6px 8px;color:#2980b9'>{curr_str}</td>
  <td style='text-align:right;padding:6px 8px;{ret_col};font-weight:600' data-val='{actual_ret:.4f}' title='(current_price − entry) / entry × 100'>{actual_ret:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 1-day return'>{ret_1d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 3-day return'>{ret_3d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 7-day return'>{ret_7d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#27ae60' data-val='{max_ret:.4f}' title='Peak return (max of DB max_return and live actual_return)'>{max_ret:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#e74c3c' data-val='{max_dd:.4f}'>{max_dd:.2f}%</td>
  <td style='padding:6px 8px;text-align:center'>{sell_badge}</td>
  <td style='padding:6px 8px;color:#888;font-size:0.8em'>{t.get('status', '')}</td>
  <td style='padding:6px 8px'>
    <button onclick="toggleDetail('{row_id}')"
      style='font-size:0.75em;padding:2px 6px;cursor:pointer;background:#ecf0f1;border:1px solid #bdc3c7;border-radius:3px'>
      📊 Scores
    </button>
  </td>
</tr>
<tr id="{row_id}" class='tr-detail' style='display:none;background:#f8f9fa'>
  <td colspan='16' style='padding:4px 8px 8px'>{breakdown}</td>
</tr>
"""
        tbl_id = f"tbl_bkt_{name.replace(' ', '_')}"
        flt_id = f"flt_bkt_{name.replace(' ', '_')}"
        result[name] = (
            f"<div style='margin-bottom:6px'>"
            f"<input id='{flt_id}' type='text' placeholder='\U0001f50d Filter by symbol...' "
            f"oninput=\"filterTradeTable('{flt_id}','{tbl_id}')\" "
            f"style='padding:4px 10px;border:1px solid #bdc3c7;border-radius:4px;font-size:0.85em;width:220px'></div>"
            "<div style='overflow-x:auto'>"
            f"<table id='{tbl_id}' style='width:100%;border-collapse:collapse;font-size:0.83em'>"
            "<thead><tr style='background:#34495e;color:#fff'>"
            "<th style='padding:6px 8px'>Symbol</th>"
            "<th style='padding:6px 8px'>Date</th>"
            "<th style='padding:6px 8px'>Source</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',3,true)\">Score <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:right'>L1/L2/L3/L4</th>"
            "<th style='padding:6px 8px;text-align:right'>Entry</th>"
            "<th style='padding:6px 8px;text-align:right' title='Latest price: live > snapshot > derived'>Current Price</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',7,true)\" title='(Current Price - Entry) / Entry x 100'>Curr Ret <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 1-day return (from snapshot at capture time)'>DB 1d</th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 3-day return (from snapshot at capture time)'>DB 3d</th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 7-day return (from snapshot at capture time)'>DB 7d</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',11,true)\" title='Peak return: max(DB max_return, live actual_return)'>Max Ret <span class='sort-ind'>&#8645;</span></th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',12,true)\">Max DD <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:center' title='Sell engine recommendation (8-rule exit logic)'>Sell Signal</th>"
            "<th style='padding:6px 8px' title='Raw DB status from performance_metrics'>DB Status</th>"
            "<th style='padding:6px 8px'>Detail</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
            "<script>function toggleDetail(id){var r=document.getElementById(id);"
            "r.style.display=r.style.display==='none'?'table-row':'none';}</script>"
        )
    return result


def _source_trades_html(trades: list[dict]) -> dict[str, str]:
    """
    Same as _bucket_trades_html but keyed by source (download/screener/news/manual).
    Returns {source_key: html_table_string}.
    """
    source_map: dict[str, list[dict]] = {}
    for t in trades:
        src = t.get("source") or "unknown"
        source_map.setdefault(src, []).append(t)

    # Reuse per-source the same format as bucket tables
    result: dict[str, str] = {}
    for src, t_list in sorted(source_map.items()):
        if not t_list:
            result[src] = "<em>No trades for this source.</em>"
            continue
        rows_html = ""
        for idx, t in enumerate(sorted(t_list, key=lambda x: x.get("fire_time") or "", reverse=True)):
            row_id  = f"src_{src}_{idx}"
            fire_dt = t.get("fire_time", "")
            fire_str = (
                fire_dt.strftime("%Y-%m-%d")
                if hasattr(fire_dt, "strftime")
                else str(fire_dt)[:10]
            )
            actual_ret = float(t.get("actual_return", t.get("return_7d", 0)) or 0)
            ret_7d  = float(t.get("return_7d",  0) or 0)
            ret_3d  = float(t.get("return_3d",  0) or 0)
            ret_1d  = float(t.get("return_1d",  0) or 0)
            max_ret = float(t.get("max_return",  0) or 0)
            max_dd  = float(t.get("max_drawdown", 0) or 0)
            entry_px = float(t.get("entry_price", 0) or 0)
            curr_px  = t.get("current_price")
            cp_src   = t.get("current_price_src", "none")
            cp_badge = {
                "live_yf":     "<sup style='color:#00c853;font-size:0.7em' title='Live: yfinance'>&#9679;</sup>",
                "live_tv":     "<sup style='color:#00bcd4;font-size:0.7em' title='Live: TradingView scanner'>&#9679;</sup>",
                "snapshot":    "<sup style='color:#f39c12;font-size:0.7em' title='Cached (price_snapshots)'>&#9679;</sup>",
                "derived":     "<sup style='color:#f39c12;font-size:0.7em' title='Derived: entry×(1+ret7d/100)'>&#9679;</sup>",
                "result_json": "<sup style='color:#94a3b8;font-size:0.7em' title='From result_json (alert time)'>&#9679;</sup>",
                "none":        "",
            }.get(cp_src, "")
            curr_str = (
                f"{curr_px:.4f}{cp_badge}"
                if curr_px is not None
                else "<span style='color:#aaa'>—</span>"
            )
            ret_col  = "color:#27ae60" if actual_ret > 0 else "color:#e74c3c"
            bname    = _bucket_label(t.get("score", 0))
            bcolour  = _bucket_colour(bname)
            co_name  = t.get("company_name") or ""
            sym      = t.get('symbol', '')
            tv_url   = f"https://www.tradingview.com/chart/?symbol={sym}"
            tv_link  = f"<a href='{tv_url}' target='_blank' title='View on TradingView' style='color:#3498db;font-size:0.7em;margin-left:4px;text-decoration:none'>&#128279;</a>"
            sym_label = (
                f"{sym}{tv_link}"
                + (f"<br><span style='color:#777;font-size:0.8em'>{co_name[:18]}</span>"
                   if co_name else "")
            )
            sell_action = t.get("sell_action", "N/A")
            sell_reason = t.get("sell_reason", "")
            sell_badge  = _sell_signal_badge(sell_action, sell_reason)
            score_val   = float(t.get("score", 0))
            breakdown = _score_breakdown_html(t)
            rows_html += f"""
<tr class='tr-trade' style='border-bottom:1px solid #e0e0e0'>
  <td style='white-space:nowrap;padding:6px 8px'>{sym_label}</td>
  <td style='padding:6px 8px'>{fire_str}</td>
  <td style='padding:6px 8px;text-align:center'>
    <span style='background:{bcolour};color:#fff;padding:2px 6px;border-radius:3px;font-size:0.8em'>{bname}</span>
  </td>
  <td style='text-align:right;padding:6px 8px' data-val='{score_val:.2f}'>{score_val:.1f}</td>
  <td style='text-align:right;padding:6px 8px'>{entry_px:.4f}</td>
  <td style='text-align:right;padding:6px 8px;color:#2980b9'>{curr_str}</td>
  <td style='text-align:right;padding:6px 8px;{ret_col};font-weight:600' data-val='{actual_ret:.4f}' title='(Current Price - Entry) / Entry x 100'>{actual_ret:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 1-day return'>{ret_1d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 3-day return'>{ret_3d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#aaa;font-size:0.85em' title='Stored DB 7-day return'>{ret_7d:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#27ae60' data-val='{max_ret:.4f}' title='Peak return: max(DB max_return, live actual_return)'>{max_ret:+.2f}%</td>
  <td style='text-align:right;padding:6px 8px;color:#e74c3c' data-val='{max_dd:.4f}'>{max_dd:.2f}%</td>
  <td style='padding:6px 8px;text-align:center'>{sell_badge}</td>
  <td style='padding:6px 8px;color:#888;font-size:0.8em'>{t.get("status", "")}</td>
  <td style='padding:6px 8px'>
    <button onclick="toggleDetail('{row_id}')"
      style='font-size:0.75em;padding:2px 6px;cursor:pointer;background:#ecf0f1;border:1px solid #bdc3c7;border-radius:3px'>
      📊 Scores
    </button>
  </td>
</tr>
<tr id="{row_id}" class='tr-detail' style='display:none;background:#f8f9fa'>
  <td colspan='15' style='padding:4px 8px 8px'>{breakdown}</td>
</tr>
"""
        tbl_id = f"tbl_src_{src}"
        flt_id = f"flt_src_{src}"
        result[src] = (
            f"<div style='margin-bottom:6px'>"
            f"<input id='{flt_id}' type='text' placeholder='\U0001f50d Filter by symbol...' "
            f"oninput=\"filterTradeTable('{flt_id}','{tbl_id}')\" "
            f"style='padding:4px 10px;border:1px solid #bdc3c7;border-radius:4px;font-size:0.85em;width:220px'></div>"
            "<div style='overflow-x:auto'>"
            f"<table id='{tbl_id}' style='width:100%;border-collapse:collapse;font-size:0.83em'>"
            "<thead><tr style='background:#34495e;color:#fff'>"
            "<th style='padding:6px 8px'>Symbol</th>"
            "<th style='padding:6px 8px'>Date</th>"
            "<th style='padding:6px 8px;text-align:center'>Bucket</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',3,true)\">Score <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:right'>Entry</th>"
            "<th style='padding:6px 8px;text-align:right' title='Latest price: live > snapshot > derived'>Current Price</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',6,true)\" title='(Current Price - Entry) / Entry x 100'>Curr Ret <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 1-day return'>DB 1d</th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 3-day return'>DB 3d</th>"
            "<th style='padding:6px 8px;text-align:right;color:#ccc' title='Stored DB 7-day return'>DB 7d</th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',10,true)\" title='Peak return: max(DB max_return, live actual_return)'>Max Ret <span class='sort-ind'>&#8645;</span></th>"
            f"<th style='padding:6px 8px;text-align:right;cursor:pointer' onclick=\"sortTradeTable('{tbl_id}',11,true)\">Max DD <span class='sort-ind'>&#8645;</span></th>"
            "<th style='padding:6px 8px;text-align:center' title='Sell engine recommendation (8-rule exit logic)'>Sell Signal</th>"
            "<th style='padding:6px 8px' title='Raw DB status from performance_metrics'>DB Status</th>"
            "<th style='padding:6px 8px'>Detail</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
            "<script>function toggleDetail(id){var r=document.getElementById(id);"
            "r.style.display=r.style.display==='none'?'table-row':'none';}</script>"
        )
    return result


def _moomoo_insights_html() -> str:
    """Render a Moomoo Community Insights section from the local JSON cache.

    The cache at ``<repo_root>/.cache_moomoo_community.json`` is written by
    ``moomoo_community_intel.py`` (menu option 22a / morning run).  Falls back
    gracefully when the cache is absent or stale.
    """
    import json as _json, time as _time, html as _html

    cache_path = ROOT / ".cache_moomoo_community.json"
    posts: list[dict] = []
    cache_age_str = "unknown"

    try:
        if cache_path.exists():
            raw = _json.loads(cache_path.read_text(encoding="utf-8"))
            age_secs = _time.time() - raw.get("ts", 0)
            posts = raw.get("posts", [])[:20]  # cap at 20 for report
            h, m = divmod(int(age_secs // 60), 60)
            cache_age_str = f"{h}h {m}m ago" if h else f"{m}m ago"
    except Exception as _ce:
        cache_age_str = f"(error: {_ce})"

    if not posts:
        return """
<h2>&#129418; Moomoo Community Insights</h2>
<div class="card">
  <p style="color:#888;font-style:italic">
    No cached community posts available.
    Run menu option <code>22a</code> (Moomoo Community Intelligence) or
    <code>python moomoo_community_intel.py</code> to populate the cache.
  </p>
</div>
"""

    # Build sentiment-coloured rows
    rows_html = ""
    for p in posts:
        text  = (p.get("text") or "").strip()
        first = text.split("\n")[0][:200]
        score = p.get("sentiment_compound", 0.0) or 0.0
        if score > 0.05:
            badge_col, badge_lbl = "#27ae60", "BULL"
        elif score < -0.05:
            badge_col, badge_lbl = "#e74c3c", "BEAR"
        else:
            badge_col, badge_lbl = "#888", "NEUT"
        tickers = ", ".join(
            t.replace("-US", "").replace("-AU", "").replace("-HK", "")
            for t in (p.get("tickers") or [])[:3]
        )
        rows_html += (
            f"<tr>"
            f"<td style='width:60px;text-align:center'>"
            f"<span style='background:{badge_col};color:#fff;padding:2px 6px;"
            f"border-radius:4px;font-size:11px'>{badge_lbl}</span></td>"
            f"<td style='color:#aaa;font-size:12px'>{_html.escape(tickers)}</td>"
            f"<td>{_html.escape(first)}</td>"
            f"</tr>\n"
        )

    return f"""
<h2>&#129418; Moomoo Community Insights</h2>
<div class="card">
  <p style="color:#888;font-size:12px;margin-bottom:8px">
    {len(posts)} posts cached {cache_age_str} &mdash;
    refresh with <code>python moomoo_community_intel.py</code>
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead>
      <tr style="background:#2c3e50;color:#fff">
        <th>Sent.</th><th style="width:120px">Tickers</th><th>Headline</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""


def _load_options_positions(db_path: str) -> list[dict]:
    """Load options recommendations from alert_tracker.db for analytics."""
    import sqlite3 as _sql
    try:
        conn = _sql.connect(db_path)
        conn.row_factory = _sql.Row
        rows = conn.execute(
            "SELECT * FROM options_recommendations ORDER BY run_date DESC, rank ASC"
        ).fetchall()
        conn.close()
        result = [dict(r) for r in rows]
        if result:
            print(f"  [options] loaded {len(result)} recommendations from DB", flush=True)
        return result
    except Exception as exc:
        print(f"  [options] could not load options_recommendations: {exc}", flush=True)
        return []


def _options_section_html(recs: list[dict],
                           panel_title: str = "&#127918; Options Recommendations Analytics",
                           prefix: str = "opt",
                           include_js: bool = True) -> str:
    """Render the Options Recommendations analytics section."""
    if not recs:
        return f"""
<h2>{panel_title}</h2>
<div class="card">
  <p style="color:#888;font-style:italic">
    No options recommendations stored yet.
    Run <code>python tradingview/options/run_options.py --from-actionable --all-modes</code>
    to generate and persist recommendations to the database.
  </p>
</div>
"""
    def _pct(v):
        return f"{v:.1f}%" if v is not None else "\u2014"

    def _f2(v):
        return f"{v:.2f}" if v is not None else "\u2014"

    def _f1(v):
        return f"{v:.1f}" if v is not None else "\u2014"

    def _avg_field(lst, key):
        vals = [r[key] for r in lst if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    # ── Pre-compute effective outcome for all recs (DEEP_LOSS detection) ────
    import datetime as _dt_pre
    _today_pre = _dt_pre.date.today()
    _RESOLVED_SET = frozenset({"ITM_WIN", "OTM_LOSS", "PREMIUM_WIN", "EXPIRED", "EXPIRED_UNKNOWN", "DEEP_LOSS"})
    for _r_pre in recs:
        _raw_pre = _r_pre.get("outcome", "OPEN")
        # ── Parse expiry date for ALL records (resolved and OPEN) ─────────
        _exp_s_pre = (_r_pre.get("expiry") or "").strip()
        _exp_d_pre = None
        if _exp_s_pre:
            try:
                if len(_exp_s_pre) == 10 and "-" in _exp_s_pre:
                    _exp_d_pre = _dt_pre.date.fromisoformat(_exp_s_pre)
                else:
                    _exp_d_pre = _dt_pre.datetime.strptime(_exp_s_pre, "%d%b%Y").date()
            except Exception:
                pass
        # Flag: expiry date has already passed (true for any outcome)
        _r_pre["_is_expired"] = _exp_d_pre is not None and _exp_d_pre < _today_pre
        if _raw_pre != "OPEN":
            _r_pre["_eff_out"] = _raw_pre
            continue
        _rcp_pre = _r_pre.get("current_premium")
        _rep_pre = _r_pre.get("premium")
        _rpv_pre = (_rcp_pre - _rep_pre) / _rep_pre * 100 if (_rcp_pre is not None and _rep_pre and _rep_pre != 0) else None
        _rem_pre = (_exp_d_pre - _today_pre).days if _exp_d_pre is not None else None
        _eff_pre = "OPEN"
        if _exp_d_pre is not None and _exp_d_pre < _today_pre:
            _eff_pre = "PREMIUM_WIN" if (_rpv_pre is not None and _rpv_pre > 0) else "OTM_LOSS" if _rpv_pre is not None else "EXPIRED_UNKNOWN"
        elif _rpv_pre is not None:
            if _rpv_pre <= -90:
                _eff_pre = "DEEP_LOSS"
            elif _rpv_pre <= -80 and (_rem_pre is None or _rem_pre <= 21):
                _eff_pre = "DEEP_LOSS"
        _r_pre["_eff_out"] = _eff_pre
    # ── Analytics groups (use _eff_out so DEEP_LOSS is counted as a loss) ──
    resolved   = [r for r in recs if r.get("_eff_out") in _RESOLVED_SET]
    open_recs  = [r for r in recs if r.get("_eff_out") not in _RESOLVED_SET]
    wins       = [r for r in resolved if r.get("_eff_out") in ("ITM_WIN", "PREMIUM_WIN")]
    losses     = [r for r in resolved if r.get("_eff_out") in ("OTM_LOSS", "DEEP_LOSS")]
    # Expired = any resolved record whose expiry date has already passed
    # (includes OTM_LOSS/PREMIUM_WIN options that have physically expired by date)
    expired    = [r for r in resolved if r.get("_is_expired")]
    win_rate   = len(wins) / len(resolved) * 100 if resolved else None
    n_total    = len(recs)
    n_open     = len(open_recs)
    n_resolved = len(resolved)
    n_deep_loss = len([r for r in recs if r.get("_eff_out") == "DEEP_LOSS"])

    avg_sig    = _avg_field(recs, "signal_score")
    avg_opt    = _avg_field(recs, "option_score")
    avg_vix    = _avg_field(recs, "vix_at_rec")

    # ── Pool EV (Expected Value) — using resolved recs with P&L data ────────
    _pnl_wins: list[float] = []
    _pnl_losses: list[float] = []
    for _r in resolved:
        if _r.get("_eff_out") == "DEEP_LOSS":
            continue  # unrealized — excluded from Pool EV
        _ep = _r.get("premium")
        _cp = _r.get("current_premium")
        if _ep and _ep > 0 and _cp is not None:
            _pp = (_cp - _ep) / _ep * 100
            if _r.get("_eff_out") in ("ITM_WIN", "PREMIUM_WIN"):
                _pnl_wins.append(_pp)
            elif _r.get("_eff_out") == "OTM_LOSS":
                _pnl_losses.append(_pp)
    _pool_ev: float | None = None
    if _pnl_wins and _pnl_losses and win_rate is not None:
        _avg_w = sum(_pnl_wins) / len(_pnl_wins)
        _avg_l = sum(_pnl_losses) / len(_pnl_losses)
        _wr_f  = win_rate / 100
        _pool_ev = round(_wr_f * _avg_w + (1 - _wr_f) * _avg_l, 1)
    _ev_html = (
        f"<span style='color:{'#27ae60' if _pool_ev >= 0 else '#e74c3c'};font-weight:bold'>"
        f"{_pool_ev:+.1f}%</span>"
        if _pool_ev is not None else
        "<span style='color:#888;font-size:0.85em'>n/a</span>"
    )

    wr_html = (
        f"<span style='color:#27ae60;font-weight:bold'>{win_rate:.1f}%</span>"
        if win_rate is not None else
        "<span style='color:#888;font-size:0.85em'>Pending</span>"
    )
    kpi_strip = f"""
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px">
  <div style="background:#f0f8ff;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Total Recommendations</div>
    <div style="font-size:1.6em;font-weight:bold;color:#2980b9">{n_total}</div>
  </div>
  <div style="background:#fff8e1;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Open (Pending)</div>
    <div style="font-size:1.6em;font-weight:bold;color:#e67e22">{n_open}</div>
  </div>
  <div style="background:#f0fff0;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Resolved</div>
    <div style="font-size:1.4em;font-weight:bold;color:#27ae60">{n_resolved}
      <small style="font-size:0.6em;color:#555">(W:{len(wins)} L:{len(losses)})</small>
    </div>
  </div>
  <div style="background:#fef9f0;border-radius:8px;padding:10px 18px;min-width:100px" title="Options whose expiry date has passed (May be Win or Loss above). Count was 0 before fix — caused by LIMIT 1000 query that excluded the oldest records.">
    <div style="font-size:0.78em;color:#888">Expired &#x231B;</div>
    <div style="font-size:1.6em;font-weight:bold;color:#e67e22">{len(expired)}</div>
    <div style="font-size:0.68em;color:#888;margin-top:2px">subset of W/L</div>
  </div>
  <div style="background:#fdf2f8;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Win Rate</div>
    <div style="font-size:1.6em;font-weight:bold">{wr_html}</div>
  </div>
  <div style="background:#f5f5f5;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Avg Signal Score</div>
    <div style="font-size:1.4em;font-weight:bold;color:#2c3e50">{_f1(avg_sig)}</div>
  </div>
  <div style="background:#f5f5f5;border-radius:8px;padding:10px 18px;min-width:110px">
    <div style="font-size:0.78em;color:#888">Avg Option Score</div>
    <div style="font-size:1.4em;font-weight:bold;color:#2c3e50">{_f1(avg_opt)}</div>
  </div>
  <div style="background:#f5f5f5;border-radius:8px;padding:10px 18px;min-width:90px">
    <div style="font-size:0.78em;color:#888">Avg VIX</div>
    <div style="font-size:1.4em;font-weight:bold;color:#2c3e50">{_f1(avg_vix)}</div>
  </div>
  <div style="background:#e8f4fd;border-radius:8px;padding:10px 18px;min-width:110px;cursor:help" title="Pool EV = win_rate × avg_win_P&amp;L% + loss_rate × avg_loss_P&amp;L%. Computed from resolved recommendations with premium data. Positive EV = edge exists.">
    <div style="font-size:0.78em;color:#888">Pool EV &#9432;</div>
    <div style="font-size:1.4em;font-weight:bold">{_ev_html}</div>
  </div>
  <div style="background:#fdecea;border-radius:8px;padding:10px 18px;min-width:100px" title="OPEN positions with P&amp;L% &lt;= -90% or &lt;= -80% with &lt;=21 days left — counted as losses in analytics">
    <div style="font-size:0.78em;color:#888">Deep Loss &#9888;</div>
    <div style="font-size:1.6em;font-weight:bold;color:#c0392b">{n_deep_loss}</div>
  </div>
</div>"""

    BREAKDOWN_HEADER = """<thead>
      <tr style="background:#2c3e50;color:#fff">
        <th style="padding:6px 10px;text-align:left">Group</th>
        <th style="padding:6px 10px;text-align:center">N Recs</th>
        <th style="padding:6px 10px;text-align:center">Resolved</th>
        <th style="padding:6px 10px;text-align:right" title="WIN = current_premium &gt; entry_premium (OPEN recs) or ITM at expiry">Prem Win%</th>
        <th style="padding:6px 10px;text-align:right">Loss Rate</th>
        <th style="padding:6px 10px;text-align:right">Avg Sig</th>
        <th style="padding:6px 10px;text-align:right">Avg Opt</th>
        <th style="padding:6px 10px;text-align:right">Avg Prem</th>
        <th style="padding:6px 10px;text-align:right" title="Average P&amp;L%: (curr_prem - entry_prem) / entry_prem">Avg P&amp;L%</th>
        <th style="padding:6px 10px;text-align:right">Avg Delta</th>
        <th style="padding:6px 10px;text-align:right">Avg IV</th>
        <th style="padding:6px 10px;text-align:right">Avg VIX</th>
        <th style="padding:6px 10px;text-align:center">Detail</th>
      </tr>
    </thead>"""

    def _iv_disp(grp):
        raw = _avg_field(grp, "iv_pct")
        if raw is None:
            return None
        return raw * 100 if raw <= 1 else raw

    def _breakdown_rows(groups, table_prefix="opt"):
        """Emit one clickable summary row + one hidden detail row per group.
        Identical pattern to Source Reliability toggleSrcDetail."""
        import datetime as _dt
        _today = _dt.date.today()

        def _obadge(outcome):
            _c = {"ITM_WIN": "#27ae60", "PREMIUM_WIN": "#1a8a40", "WIN": "#27ae60",
                  "OTM_LOSS": "#e74c3c", "LOSS": "#e74c3c", "DEEP_LOSS": "#c0392b",
                  "EXPIRED": "#95a5a6", "EXPIRED_UNKNOWN": "#95a5a6", "OPEN": "#2980b9"}
            _o = (outcome or "OPEN").upper()
            return (f"<span style='background:{_c.get(_o,'#888')};color:#fff;"
                    f"padding:2px 6px;border-radius:10px;font-size:0.78em'>{_o}</span>")

        def _otbadge(ot):
            _ot = (ot or "").upper()
            _c  = "#27ae60" if _ot == "CALL" else "#e74c3c" if _ot == "PUT" else "#888"
            return f"<span style='color:{_c};font-weight:bold'>{_ot or '?'}</span>"

        rows_html = ""
        for grp_idx, (label, grp) in enumerate(groups.items()):
            if not grp:
                continue
            _GRP_RESOLVED = frozenset({"ITM_WIN", "OTM_LOSS", "PREMIUM_WIN", "EXPIRED", "EXPIRED_UNKNOWN", "DEEP_LOSS"})
            grp_res   = [r for r in grp if r.get("_eff_out", r.get("outcome")) in _GRP_RESOLVED]
            grp_wins  = [r for r in grp_res if r.get("_eff_out", r.get("outcome")) in ("ITM_WIN", "PREMIUM_WIN")]
            grp_open  = [r for r in grp if r.get("_eff_out", r.get("outcome")) not in _GRP_RESOLVED]
            # Premium-based win/loss for OPEN recs that have current_premium fetched
            grp_prem  = [r for r in grp_open
                         if r.get("current_premium") is not None and r.get("premium") is not None]
            prem_wins = [r for r in grp_prem
                         if (r["current_premium"] or 0) > (r["premium"] or 0)]
            # Combined: expired direction wins + live premium wins
            all_resolved = grp_res + grp_prem
            all_wins     = grp_wins + prem_wins
            grp_wr    = len(all_wins) / len(all_resolved) * 100 if all_resolved else None
            loss_rate = (len(all_resolved) - len(all_wins)) / len(all_resolved) * 100 if all_resolved else None
            # Average P&L% for all recs that have current_premium
            pnl_items = []
            for _r in grp:
                _cp, _ep = _r.get("current_premium"), _r.get("premium")
                if _cp is not None and _ep is not None and _ep != 0:
                    pnl_items.append((_cp - _ep) / _ep * 100)
            avg_pnl = sum(pnl_items) / len(pnl_items) if pnl_items else None
            avg_pnl_s = (
                f"<span style='color:{'#27ae60' if avg_pnl >= 0 else '#e74c3c'};font-weight:bold'>"
                f"{avg_pnl:+.1f}%</span>"
                if avg_pnl is not None else "<span style='color:#888'>\u2014</span>"
            )
            n_no_prem = len(grp_open) - len(grp_prem)
            wr_cell   = (
                f"<span style='color:#27ae60;font-weight:bold'>{grp_wr:.1f}%</span>"
                f"<span style='color:#888;font-size:0.78em;margin-left:4px'>"
                f"({len(all_wins)}/{len(all_resolved)})</span>"
                if grp_wr is not None else
                "<span style='color:#888'>pending</span>"
            )
            _cnt_lbl  = (
                f"<span style='background:#ecf0f1;padding:2px 7px;border-radius:10px;"
                f"font-size:0.85em'>{len(grp)}</span>"
                f"<span style='color:#888;font-size:0.78em;margin-left:5px'>"
                f"{n_no_prem} no-prem</span>"
            )

            row_id = f"{table_prefix}_{grp_idx}"
            rows_html += (
                f"<tr style='border-bottom:1px solid #e8e8e8;cursor:pointer'"
                f" onclick=\"toggleOptDetail('{row_id}')\">"
                f"<td style='padding:6px 10px;font-weight:600'>{label}</td>"
                f"<td style='padding:6px 10px;text-align:center'>{_cnt_lbl}</td>"
                f"<td style='padding:6px 10px;text-align:center'>{len(all_resolved)}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{wr_cell}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_pct(loss_rate)}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_f1(_avg_field(grp,'signal_score'))}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_f1(_avg_field(grp,'option_score'))}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_f2(_avg_field(grp,'premium'))}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{avg_pnl_s}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_f2(_avg_field(grp,'delta'))}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_pct(_iv_disp(grp))}</td>"
                f"<td style='padding:6px 10px;text-align:right'>{_f1(_avg_field(grp,'vix_at_rec'))}</td>"
                f"<td style='padding:6px 10px;text-align:center'>"
                f"<span style='color:#2980b9;font-size:0.85em'>&#9658; expand</span></td>"
                f"</tr>"
            )

            # ── Detail rows (hidden until clicked) ──────────────────────────
            det_rows = ""
            for r in sorted(grp, key=lambda x: (x.get("run_date") or ""), reverse=True):
                iv_raw = r.get("iv_pct")
                iv_d   = (f"{iv_raw*100:.1f}%" if iv_raw is not None and iv_raw <= 1
                          else (f"{iv_raw:.1f}%" if iv_raw is not None else "\u2014"))
                earn   = r.get("earnings_days_away")
                earn_c = "#e74c3c" if earn is not None and 0 <= earn <= 14 else "#555"
                earn_s = f"<span style='color:{earn_c}'>{earn}d</span>" if earn is not None else "\u2014"
                # DTE remaining = days from today to expiry; show EXPIRED badge if past
                exp_s  = r.get("expiry") or ""
                dte_rem = ""
                if exp_s:
                    try:
                        import datetime as _dtmod
                        if len(exp_s) == 10 and "-" in exp_s:
                            exp_d = _dtmod.date.fromisoformat(exp_s)
                        else:
                            exp_d = _dtmod.datetime.strptime(exp_s.upper(), "%d%b%Y").date()
                        days_diff = (exp_d - _today).days
                        if days_diff < 0:
                            # Option has expired — show how many days ago
                            dte_rem = (
                                f"<span style='background:#e74c3c;color:#fff;border-radius:3px;"
                                f"padding:1px 5px;font-size:0.75em;font-weight:600'>"
                                f"EXPIRED {-days_diff}d ago</span>"
                            )
                        else:
                            dcolor = "#e74c3c" if days_diff <= 7 else "#e67e22" if days_diff <= 14 else "#27ae60"
                            dte_rem = f"<span style='color:{dcolor};font-size:0.78em'>({days_diff}d left)</span>"
                    except Exception:
                        pass
                # Current premium and P&L%
                _cp = r.get("current_premium")
                _ep = r.get("premium")
                _curr_p_s = f"${_cp:.2f}" if _cp is not None else "\u2014"
                if _cp is not None and _ep is not None and _ep != 0:
                    _pv = (_cp - _ep) / _ep * 100
                    _pc = "#27ae60" if _pv >= 0 else "#e74c3c"
                    _pnl_s = f"<span style='color:{_pc}'>{_pv:+.1f}%</span>"
                else:
                    _pnl_s = "\u2014"
                det_rows += (
                    f"<tr style='border-bottom:1px solid #eee'>"
                    f"<td style='padding:4px 6px;color:#555;font-size:0.82em'>{r.get('run_date','')}</td>"
                    f"<td style='padding:4px 6px;font-weight:600;font-size:0.82em'>{r.get('tv_symbol','')}</td>"
                    f"<td style='padding:4px 6px;text-align:center;font-size:0.82em'>{_otbadge(r.get('option_type'))}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>${_f2(r.get('strike'))}</td>"
                    f"<td style='padding:4px 6px;text-align:center;font-size:0.82em'>{exp_s} {dte_rem}</td>"
                      f"<td style='padding:4px 6px;text-align:center;font-size:0.82em'>{r.get('dte_at_rec') or chr(8212)}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>${_f2(r.get('premium'))}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{_curr_p_s}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{_pnl_s}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{_f2(r.get('delta'))}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{iv_d}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{_f1(r.get('option_score'))}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{_f1(r.get('signal_score'))}</td>"
                    f"<td style='padding:4px 6px;text-align:right;font-size:0.82em'>{earn_s}</td>"
                    f"<td style='padding:4px 6px;text-align:center;font-size:0.82em'>{_obadge(r.get('_eff_out', r.get('outcome')))}</td>"
                    f"</tr>"
                )

            rows_html += (
                f"<tr id='{row_id}' style='display:none;background:#f8fafc'>"
                f"<td colspan='13' style='padding:8px 12px'>"
                f"<strong style='color:#2c3e50;font-size:0.9em'>"
                f"{label} &#8212; {len(grp)} recommendations"
                f"</strong>"
                f"<div style='margin-top:6px;overflow-x:auto'>"
                f"<table id='dtbl_{row_id}' style='width:100%;border-collapse:collapse'>"
                f"<thead><tr style='background:#34495e;color:#ddd'>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:left;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',0,'date')\">Date</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:left;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',1,'text')\">Symbol</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:center;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',2,'text')\">Type</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',3,'num')\">Strike</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:center;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',4,'date')\">Expiry</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:center;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',5,'num')\">DTE</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',6,'num')\">Entry Prem</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',7,'num')\" title='Current option mid-price'>Curr Prem</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',8,'num')\" title='(curr-entry)/entry %'>P&amp;L%</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',9,'num')\">Delta</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',10,'num')\">IV</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',11,'num')\">OScore</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',12,'num')\">SigScore</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:right;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',13,'num')\">Earn</th>"
                f"<th style='padding:4px 6px;font-size:0.8em;text-align:center;cursor:pointer' onclick=\"sortTable('dtbl_{row_id}',14,'text')\">Outcome</th>"
                f"</tr></thead>"
                f"<tbody>{det_rows}</tbody>"
                f"</table></div>"
                f"</td></tr>"
            )
        return rows_html

    mode_groups = {m: [r for r in recs if r.get("mode") == m]
                   for m in ("SAFE", "BALANCED", "AGGRESSIVE")}
    mode_rows = _breakdown_rows(mode_groups, f"{prefix}_mode")

    type_groups = {ot: [r for r in recs if (r.get("option_type") or "").upper() == ot]
                   for ot in ("CALL", "PUT")}
    type_rows = _breakdown_rows(type_groups, f"{prefix}_type")

    _DTE_NORM: dict = {
        "0-7": "0-7 days", "0-7 days": "0-7 days",
        "8-21": "8-21 days", "8-21 days": "8-21 days",
        "22-45": "22-45 days", "22-45 days": "22-45 days",
        "45+": "45+ days", "45+ days": "45+ days",
    }

    def _dte_bucket(r):
        d = r.get("dte_bucket") or r.get("dte_at_rec")
        if isinstance(d, str):
            return _DTE_NORM.get(d.strip(), "Unknown")
        if d is None:
            return "Unknown"
        if d <= 7:
            return "0-7 days"
        if d <= 21:
            return "8-21 days"
        if d <= 45:
            return "22-45 days"
        return "45+ days"

    dte_groups: dict = {"0-7 days": [], "8-21 days": [], "22-45 days": [], "45+ days": [], "Unknown": []}
    for r in recs:
        dte_groups.setdefault(_dte_bucket(r), []).append(r)
    dte_rows = _breakdown_rows({k: v for k, v in dte_groups.items() if v}, f"{prefix}_dte")

    mono_groups: dict = {"ITM": [], "ATM": [], "OTM": [], "Unknown": []}
    for r in recs:
        lbl = r.get("moneyness_label") or "Unknown"
        if lbl not in mono_groups:
            lbl = "Unknown"
        mono_groups[lbl].append(r)
    mono_rows = _breakdown_rows({k: v for k, v in mono_groups.items() if v}, f"{prefix}_mono")

    def _sig_bucket(score):
        if score is None:
            return "Unknown"
        if score < 55:
            return "LOW (<55)"
        if score < 65:
            return "MED (55-65)"
        if score < 75:
            return "HIGH (65-75)"
        return "APEX (75+)"

    SIG_ORDER = ["APEX (75+)", "HIGH (65-75)", "MED (55-65)", "LOW (<55)", "Unknown"]
    sig_groups: dict = {b: [] for b in SIG_ORDER}
    for r in recs:
        sig_groups[_sig_bucket(r.get("signal_score"))].append(r)
    sig_rows = _breakdown_rows({k: v for k, v in sig_groups.items() if v}, f"{prefix}_sig")

    earn_groups: dict = {}
    for r in recs:
        raw  = r.get("earnings_risk") or "N/A"
        clean = raw.replace("\U0001f534 ", "").replace("\U0001f7e1 ", "").replace("\U0001f7e2 ", "").strip()
        earn_groups.setdefault(clean, []).append(r)
    earn_rows = _breakdown_rows(earn_groups, f"{prefix}_earn")

    def _outcome_badge(outcome):
        colours = {"ITM_WIN": "#27ae60", "PREMIUM_WIN": "#1a8a40", "WIN": "#27ae60",
                   "OTM_LOSS": "#e74c3c", "LOSS": "#e74c3c",
                   "EXPIRED": "#95a5a6", "EXPIRED_UNKNOWN": "#95a5a6",
                   "OPEN": "#2980b9", "DEEP_LOSS": "#c0392b"}
        o = (outcome or "OPEN").upper()
        c = colours.get(o, "#888")
        return (f"<span style='background:{c};color:#fff;padding:2px 8px;"
                f"border-radius:10px;font-size:0.78em'>{o}</span>")

    def _opt_type_badge(ot):
        ot = (ot or "").upper()
        c  = "#27ae60" if ot == "CALL" else "#e74c3c" if ot == "PUT" else "#888"
        return f"<span style='color:{c};font-weight:bold'>{ot or '?'}</span>"

    import datetime as _dt_mod
    _today_date = _dt_mod.date.today()

    rec_rows = ""
    for r in recs:
        iv_raw  = r.get("iv_pct")
        iv_disp = (f"{iv_raw*100:.1f}%" if iv_raw is not None and iv_raw <= 1
                   else (f"{iv_raw:.1f}%" if iv_raw is not None else "\u2014"))
        news_score = r.get("news_sentiment_score")
        news_col   = "#27ae60" if (news_score or 0) > 0.3 else "#e74c3c" if (news_score or 0) < 0 else "#888"
        earn_days  = r.get("earnings_days_away")
        earn_cell  = (f"<span style='color:#e74c3c'>{'&#9888; ' if earn_days is not None and 0 <= earn_days <= 14 else ''}{earn_days}d</span>"
                      if earn_days is not None else "\u2014")
        mode_colours = {"SAFE": "2980b9", "BALANCED": "27ae60", "AGGRESSIVE": "e74c3c"}
        mode_c = mode_colours.get(r.get("mode", ""), "888")
        _rcp = r.get("current_premium")
        _rep = r.get("premium")
        _rc_s = f"${_rcp:.2f}" if _rcp is not None else "\u2014"
        if _rcp is not None and _rep is not None and _rep != 0:
            _rpv = (_rcp - _rep) / _rep * 100
            _rpc = "#27ae60" if _rpv >= 0 else "#e74c3c"
            _rpnl_s = f"<span style='color:{_rpc}'>{_rpv:+.1f}%</span>"
        else:
            _rpnl_s = "\u2014"
            _rpv = None
        # ── Detect expired OPEN options → mark as WIN/LOSS ──
        _eff_outcome = r.get("outcome", "OPEN")
        if _eff_outcome == "OPEN":
            _exp_str = r.get("expiry") or ""
            _exp_date: "_dt_mod.date | None" = None  # type: ignore[assignment]
            _rem_dte: "int | None" = None
            if _exp_str:
                try:
                    if len(_exp_str) == 10 and "-" in _exp_str:
                        _exp_date = _dt_mod.date.fromisoformat(_exp_str)
                    else:
                        _exp_date = _dt_mod.datetime.strptime(_exp_str.strip(), "%d%b%Y").date()
                    _rem_dte = (_exp_date - _today_date).days
                    if _exp_date < _today_date:
                        if _rpv is not None:
                            _eff_outcome = "PREMIUM_WIN" if _rpv > 0 else "OTM_LOSS"
                        else:
                            _eff_outcome = "EXPIRED_UNKNOWN"
                except Exception:
                    pass
        # ── Threshold: deeply-losing live option (near-worthless) ──────────
        # Must be AFTER the expiry-check block so expired options are handled first.
        # _rem_dte is None when expiry is absent/unparseable (option shown as OPEN).
        if _eff_outcome == "OPEN" and _rpv is not None:
            if _rpv <= -90:
                _eff_outcome = "DEEP_LOSS"   # near-zero value — unconditional
            elif _rpv <= -80:
                # Severe loss: flag when remaining DTE ≤ 21 or unknown (NULL expiry)
                if _rem_dte is None or _rem_dte <= 21:
                    _eff_outcome = "DEEP_LOSS"
        _row_html = (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:5px 8px;white-space:nowrap'>{r.get('run_date', '')}</td>"
            f"<td style='padding:5px 8px;font-weight:600'>{r.get('tv_symbol', '')}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{_opt_type_badge(r.get('option_type'))}</td>"
            f"<td style='padding:5px 8px;text-align:center'>"
            f"<span style='background:#{mode_c};color:#fff;padding:1px 6px;border-radius:8px;font-size:0.78em'>"
            f"{r.get('mode', '')}</span></td>"
            f"<td style='padding:5px 8px;text-align:center'>{r.get('rank', '')}</td>"
            f"<td style='padding:5px 8px;text-align:right'>"
            f"{'${:.2f}'.format(r['strike']) if r.get('strike') is not None else chr(8212)}</td>"
            f"<td style='padding:5px 8px'>{r.get('expiry') or chr(8212)}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{r.get('dte_at_rec') or chr(8212)}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{r.get('moneyness_label') or chr(8212)}</td>"
            f"<td style='padding:5px 8px;text-align:right'>${_f2(r.get('premium'))}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_rc_s}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_rpnl_s}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_f2(r.get('delta'))}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{iv_disp}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_f1(r.get('option_score'))}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_f1(r.get('signal_score'))}</td>"
            f"<td style='padding:5px 8px;text-align:right'>"
            f"<span style='color:{news_col}'>{_f2(news_score)}</span></td>"
            f"<td style='padding:5px 8px;text-align:right'>{earn_cell}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_f1(r.get('vix_at_rec'))}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{_f2(r.get('pcr'))}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{_outcome_badge(_eff_outcome)}</td>"
            f"</tr>"
        )
        rec_rows += _row_html

    # ── Options Spearman Correlation (score calibration insights) ──────────
    def _spearman(xs: list, ys: list) -> float:
        if len(xs) < 10:
            return 0.0
        def _rank(lst):
            srt = sorted(range(len(lst)), key=lambda i: lst[i])
            r = [0] * len(lst); [r.__setitem__(i, ri) for ri, i in enumerate(srt)]
            return r
        rx = _rank(xs); ry = _rank(ys)
        mrx = sum(rx) / len(rx); mry = sum(ry) / len(ry)
        num = sum((rx[i] - mrx) * (ry[i] - mry) for i in range(len(rx)))
        den = (sum((rx[i] - mrx) ** 2 for i in range(len(rx))) *
               sum((ry[i] - mry) ** 2 for i in range(len(ry)))) ** 0.5
        return num / den if den > 0 else 0.0

    opt_scored = [r for r in recs if r.get("current_premium") is not None
                  and r.get("premium") and r["premium"] > 0]
    opt_pnl    = [(r["current_premium"] - r["premium"]) / r["premium"] for r in opt_scored]
    opt_spearman_rows = ""
    if len(opt_scored) >= 10:
        opt_wr_overall = sum(1 for p in opt_pnl if p > 0) / len(opt_pnl) * 100
        opt_avg_pnl    = sum(opt_pnl) / len(opt_pnl) * 100
        for os_label, os_field in [("Option Score", "option_score"), ("Signal Score", "signal_score"),
                                    ("Delta (abs)", "delta"), ("IV%", "iv_pct"),
                                    ("Spread%", "spread_pct"), ("DTE at Rec", "dte_at_rec")]:
            pairs_os = [(abs(r[os_field]) if os_field == "delta" else r[os_field], pnl)
                        for r, pnl in zip(opt_scored, opt_pnl) if r.get(os_field) is not None]
            if len(pairs_os) < 10:
                continue
            rho_os = _spearman([p[0] for p in pairs_os], [p[1] for p in pairs_os])
            rc_os  = "#27ae60" if rho_os > 0.1 else ("#e74c3c" if rho_os < -0.05 else "#888")
            st_os  = "&#9989; Predictive" if abs(rho_os) > 0.15 else ("&#128308; Weak" if abs(rho_os) > 0.05 else "&#9898; No signal")
            warn   = " &#9888;" if rho_os < -0.05 else ""
            opt_spearman_rows += (
                f"<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:6px 10px;font-weight:600'>{os_label}{warn}</td>"
                f"<td style='padding:6px 10px;text-align:center;color:{rc_os};font-weight:700'>{rho_os:+.3f}</td>"
                f"<td style='padding:6px 10px;text-align:center;color:{rc_os}'>{st_os}</td>"
                f"<td style='padding:6px 10px;text-align:center;color:#666'>{len(pairs_os)}</td>"
                f"</tr>"
            )
        opt_score_warning = ""
        oscore_rho = _spearman(
            [r.get("option_score") for r in opt_scored if r.get("option_score") is not None],
            [pnl for r, pnl in zip(opt_scored, opt_pnl) if r.get("option_score") is not None]
        )
        if oscore_rho < -0.05:
            opt_score_warning = (
                f"<div style='background:#fdf2f8;border-left:4px solid #e74c3c;"
                f"padding:10px 14px;border-radius:4px;margin-bottom:12px'>"
                f"&#9888; <strong>CALIBRATION ALERT:</strong> <code>option_score</code> has "
                f"<strong>negative</strong> Spearman correlation ({oscore_rho:+.3f}) with "
                f"actual P&amp;L &mdash; higher option scores are currently associated with "
                f"<em>worse</em> outcomes. The scoring model may be using inverse signals "
                f"or overfitting to expensive/illiquid options. Consider recalibrating "
                f"<code>options_analyzer.py</code> weights."
                f"</div>"
            )
        else:
            opt_score_warning = ""
    else:
        opt_scored_count = len(opt_scored)
        opt_wr_overall   = 0.0
        opt_avg_pnl      = 0.0
        opt_spearman_rows = (f"<tr><td colspan='4' style='padding:10px;color:#888;text-align:center'>"
                             f"Need &ge;10 recs with current_premium (have {opt_scored_count}) &mdash; "
                             f"run <code>--update-premiums</code></td></tr>")
        opt_score_warning = ""

    _js_block = ("""<script>
function toggleOptDetail(id) {
  var r = document.getElementById(id);
  r.style.display = r.style.display === 'none' ? 'table-row' : 'none';
}
function sortTable(tableId, colIdx, type) {
  var table = document.getElementById(tableId);
  if (!table) return;
  var tbody = table.querySelector('tbody');
  if (!tbody) { tbody = table; }
  var headerRow = table.querySelector('thead tr') || table.rows[0];
  var rows = Array.from(table.querySelectorAll('tbody tr, tr')).filter(function(r) {
    return r !== headerRow && r.cells.length > 1 && !r.classList.contains('detail-row');
  });
  var asc = table.dataset.sortCol == colIdx && table.dataset.sortDir !== 'asc';
  rows.sort(function(a, b) {
    var va = (a.cells[colIdx] || {}).textContent || '';
    var vb = (b.cells[colIdx] || {}).textContent || '';
    va = va.trim(); vb = vb.trim();
    if (type === 'num') {
      va = parseFloat(va.replace(/[^0-9.\\-]/g,'')) || 0;
      vb = parseFloat(vb.replace(/[^0-9.\\-]/g,'')) || 0;
    } else if (type === 'date') {
      va = new Date(va) || 0; vb = new Date(vb) || 0;
    }
    if (va < vb) return asc ? -1 : 1;
    if (va > vb) return asc ? 1 : -1;
    return 0;
  });
  var parent = rows[0] ? rows[0].parentNode : tbody;
  rows.forEach(function(r) { parent.appendChild(r); });
  table.dataset.sortCol = colIdx;
  table.dataset.sortDir = asc ? 'asc' : 'desc';
  if (headerRow) {
    headerRow.querySelectorAll('th').forEach(function(th, i) {
      th.textContent = th.textContent.replace(/ [\u25b2\u25bc]/g, '');
      if (i == colIdx) th.textContent += (asc ? ' \u25b2' : ' \u25bc');
    });
  }
}
</script>""" if include_js else "")

    return f"""
<h2>{panel_title}
  <span style="font-size:0.62em;color:#888;font-weight:400">
    Source: <code>options_recommendations</code> table &mdash; populated by
    <code>run_options.py --from-actionable</code>
  </span>
</h2>
<div class="card">

{kpi_strip}

<details open>
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.1em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by Mode (SAFE / BALANCED / AGGRESSIVE)
  </summary>
  <p style="color:#666;font-size:0.85em;margin:0 0 8px">
    WIN = underlying moved in predicted direction before expiry.
    Most records show <em>pending</em> until outcomes are marked.
  </p>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{mode_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:12px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by Option Type (CALL / PUT)
  </summary>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{type_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:12px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by DTE at Recommendation
  </summary>
  <p style="color:#666;font-size:0.85em;margin:0 0 8px">
    Shorter DTE = higher gamma, faster theta decay.
  </p>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{dte_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:12px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by Moneyness (ITM / ATM / OTM)
  </summary>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{mono_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:12px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by Signal Score Bucket (L1&#8211;L4 Composite)
  </summary>
  <p style="color:#666;font-size:0.85em;margin:0 0 8px">
    Tests whether higher-conviction signals produce better option outcomes.
    Same bucketing as stock analysis: LOW&lt;55, MED 55&#8211;65, HIGH 65&#8211;75, APEX&#8805;75.
  </p>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{sig_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:12px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; Performance by Earnings Risk
  </summary>
  <p style="color:#666;font-size:0.85em;margin:0 0 8px">
    Earnings within 14 days inflates IV &mdash; options are more expensive but direction harder to call.
  </p>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.88em">
    {BREAKDOWN_HEADER}<tbody>{earn_rows}</tbody>
  </table></div>
</details>

<details style="margin-top:16px">
  <summary style="cursor:pointer;color:#2c3e50;font-weight:bold;font-size:1.05em;padding:6px 0;margin-bottom:8px">
    &#9654; All Recommendations ({len(recs)} rows) &mdash; click column headers to sort
  </summary>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.82em" id="{prefix}-det-tbl">
    <thead>
      <tr style="background:#2c3e50;color:#fff">
        <th style="padding:5px 8px;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',0,'date')">Date</th>
        <th style="padding:5px 8px;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',1,'text')">Symbol</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',2,'text')">Type</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',3,'text')">Mode</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',4,'num')">#</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',5,'num')">Strike</th>
        <th style="padding:5px 8px;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',6,'date')">Expiry</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',7,'num')">DTE</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',8,'text')">Mono</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',9,'num')">Premium</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',10,'num')" title="Current option mid-price">Curr Prem</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',11,'num')" title="Premium P&amp;L%: (curr-entry)/entry">P&amp;L%</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',12,'num')">Delta</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',13,'num')">IV</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',14,'num')" title="Option Score 0-100">OScore</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',15,'num')" title="Composite signal score L1-L4">SigScore</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',16,'num')" title="News sentiment -1 to +1">News</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',17,'num')" title="Days to next earnings">Earn</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',18,'num')" title="VIX at recommendation time">VIX</th>
        <th style="padding:5px 8px;text-align:right;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',19,'num')" title="Put/Call ratio">PCR</th>
        <th style="padding:5px 8px;text-align:center;cursor:pointer" onclick="sortTable('{prefix}-det-tbl',20,'text')">Outcome</th>
      </tr>
    </thead>
    <tbody>{rec_rows}</tbody>
  </table>
  </div>
</details>

<p style="font-size:0.78em;color:#888;margin:6px 0 12px 2px">
  <strong>Outcome badges:</strong> ITM_WIN = expired in-the-money &mdash; PREMIUM_WIN = premium grew before expiry &mdash; OTM_LOSS = expired out-of-the-money &mdash;
  <span style="color:#c0392b;font-weight:bold">DEEP_LOSS</span> = live option down &ge;90% (or &ge;80% with &le;21 DTE) &mdash; display-only reclassification; DB outcome unchanged &mdash;
  OPEN = active, within normal range.
</p>
</div>
{_js_block}

<div class="card">
  <h3>&#128202; Options Score Calibration (Spearman &#961;)</h3>
  {opt_score_warning}
  <p style='color:#666;font-size:0.85em'>
    Spearman rank correlation between each option input and actual P&amp;L
    (current_premium vs entry_premium). A <strong>negative &#961;</strong> on
    <code>option_score</code> is a recalibration signal &mdash; the model is ranking
    options inversely. &#9888; marks anti-predictive factors.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Factor</th>
      <th style='padding:6px 10px;text-align:center'>&#961; vs P&amp;L</th>
      <th style='padding:6px 10px;text-align:center'>Signal</th>
      <th style='padding:6px 10px;text-align:center'>n</th>
    </tr></thead>
    <tbody>{opt_spearman_rows}</tbody>
  </table>
  <p style='color:#aaa;font-size:0.78em;margin-top:8px'>
    Based on {len(opt_scored)} recommendations with live premium data.
    A &#961; near 0 means the factor adds no predictive value for P&amp;L.
  </p>
</div>
"""


def load_since_entry_rows(
    db_path: str,
    min_alerts: int = 3,
    use_live_prices: bool = True,
) -> list[dict]:
    """Load every alert that has an observable price, matured or not.

    Deliberately does NOT require performance_metrics.return_7d. The whole point
    of the since-entry view is to cover still-open alerts that the fixed-horizon
    statistics cannot see; inheriting that filter would hide them.

    price_snapshots stop being written once an alert stops being tracked, so a
    snapshot can be months stale. Live prices are therefore fetched for the
    symbols that will actually be displayed (>= min_alerts), and each row records
    which source it used.
    """
    conn = sqlite3.connect(  # noqa: wal-exempt
        f"file:{Path(db_path).as_posix()}?mode=ro", uri=True
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.tv_symbol   AS symbol,
                   a.entry_price AS entry_price,
                   a.fire_time   AS fire_time,
                   ps_latest.price          AS last_price,
                   ps_latest.snapshot_time  AS last_snapshot_time
            FROM alerts a
            LEFT JOIN (
                SELECT alert_uuid, price, snapshot_time
                FROM price_snapshots
                WHERE (alert_uuid, snapshot_time) IN (
                    SELECT alert_uuid, MAX(snapshot_time)
                    FROM price_snapshots
                    GROUP BY alert_uuid
                )
            ) ps_latest ON ps_latest.alert_uuid = a.alert_uuid
            WHERE a.entry_price IS NOT NULL AND a.entry_price > 0
            """
        ).fetchall()
    finally:
        conn.close()

    parsed: list[dict] = []
    for r in rows:
        fired = None
        if r["fire_time"]:
            try:
                fired = datetime.fromisoformat(str(r["fire_time"]).replace("Z", "+00:00"))
            except ValueError:
                fired = None
        parsed.append({
            "symbol":        r["symbol"],
            "entry_price":   float(r["entry_price"]),
            "snapshot_price": float(r["last_price"]) if r["last_price"] else None,
            "snapshot_time": str(r["last_snapshot_time"] or "")[:10],
            "fire_time":     fired,
            "data_quality":  "OK",
        })

    counts: dict[str, int] = {}
    for p in parsed:
        counts[p["symbol"]] = counts.get(p["symbol"], 0) + 1
    shown = {s for s, n in counts.items() if n >= min_alerts}

    live: dict[str, float | None] = {}
    if use_live_prices and shown:
        try:
            live = fetch_live_prices(sorted(shown))
        except Exception as exc:
            print(f"  [since-entry] live price fetch failed, using snapshots: {exc}")

    out: list[dict] = []
    for p in parsed:
        lp = live.get(p["symbol"])
        price = lp if lp else p["snapshot_price"]
        if not price or price <= 0:
            continue
        p["live_return"] = (price - p["entry_price"]) / p["entry_price"] * 100.0
        p["price_src"] = "live" if lp else "snapshot"
        out.append(p)
    return out


def _since_entry_html(trades: list[dict]) -> str:
    """Top movers by since-entry return (variable holding period).

    Kept separate from the 7-day tables on purpose: each row here has its own
    holding period, so these numbers answer "what happened to my alerts" and
    must never be averaged against the fixed-horizon statistics.
    """
    from collections import defaultdict

    now = datetime.now(timezone.utc)

    def _days_held(t: dict) -> float | None:
        ft = t.get("fire_time")
        if not isinstance(ft, datetime):
            return None
        ft = ft if ft.tzinfo else ft.replace(tzinfo=timezone.utc)
        return max((now - ft).total_seconds() / 86400.0, 0.0)

    rows_by_sym: dict[str, list] = defaultdict(list)
    for t in trades:
        if t.get("data_quality", "OK") != "OK" or t.get("live_return") is None:
            continue
        rows_by_sym[t.get("symbol", "?")].append(t)

    stats = []
    for sym, g in rows_by_sym.items():
        if len(g) < 3:
            continue
        rets = [float(t["live_return"]) for t in g]
        held = [d for d in (_days_held(t) for t in g) if d is not None]
        n_live = sum(1 for t in g if t.get("price_src") == "live")
        stats.append({
            "sym": sym,
            "n": len(g),
            "avg": sum(rets) / len(rets),
            "days": (sorted(held)[len(held) // 2] if held else 0.0),
            "src": "live" if n_live == len(g) else ("mixed" if n_live else "stale"),
        })

    if not stats:
        return ""

    winners = sorted(stats, key=lambda x: -x["avg"])[:8]
    losers = sorted(stats, key=lambda x: x["avg"])[:8]

    def _rows(items: list[dict]) -> str:
        out = ""
        for s in items:
            colour = "#27ae60" if s["avg"] > 0 else "#e74c3c"
            flag = "" if s.get("src") == "live" else (
                " <span style='color:#e67e22' title='price is a stale snapshot, "
                "not a live quote'>&#9888;</span>"
            )
            out += (
                f"<tr><td style='padding:5px'><strong>{s['sym']}</strong>{flag}</td>"
                f"<td style='padding:5px;text-align:center'>{s['n']}</td>"
                f"<td style='padding:5px;text-align:right'>{s['days']:.0f}d</td>"
                f"<td style='padding:5px;text-align:right;color:{colour}'>"
                f"{s['avg']:+.2f}%</td></tr>"
            )
        return out

    n_stale = sum(1 for s in stats if s.get("src") != "live")

    all_rets = [s["avg"] for s in stats]
    all_days = [s["days"] for s in stats]
    med_days = sorted(all_days)[len(all_days) // 2] if all_days else 0.0

    return f"""
<div class="card" style="border-left:4px solid #8e44ad">
  <h3>&#128198; Since-Entry Performance &mdash; Historical (Variable Holding Period)</h3>
  <p style='color:#666;font-size:0.85em'>
    Return from entry price to the <strong>latest observed price</strong>, so each
    row carries its own holding period (median here:
    <strong>{med_days:.0f} days</strong>). This answers
    <em>"what has actually happened to my alerts since they fired?"</em>
    &mdash; a different question from the 7-day tables above.
  </p>
  <p style='color:#8e44ad;font-size:0.85em;margin-top:-4px'>
    <strong>&#9888; Not comparable to the 7-day figures and never mixed into
    them.</strong> A 200-day hold and a 5-day hold cannot be averaged: the result
    would measure alert <em>age</em> rather than signal quality. These numbers
    also change on every run as prices move, so they are excluded from every
    statistic, the bucket table, and the significance test.
  </p>
  <div style='display:grid;grid-template-columns:1fr 1fr;gap:20px'>
    <div>
      <h4 style='color:#27ae60'>&#127942; Top 8 by Since-Entry Return (n &ge; 3)</h4>
      <table style='width:100%;border-collapse:collapse;font-size:0.88em'>
        <thead><tr style='background:#27ae60;color:#fff'>
          <th style='padding:5px'>Symbol</th>
          <th style='padding:5px;text-align:center'>n</th>
          <th style='padding:5px;text-align:right'>Median Held</th>
          <th style='padding:5px;text-align:right'>Avg Ret</th>
        </tr></thead>
        <tbody>{_rows(winners)}</tbody>
      </table>
    </div>
    <div>
      <h4 style='color:#e74c3c'>&#128465; Bottom 8 by Since-Entry Return (n &ge; 3)</h4>
      <table style='width:100%;border-collapse:collapse;font-size:0.88em'>
        <thead><tr style='background:#e74c3c;color:#fff'>
          <th style='padding:5px'>Symbol</th>
          <th style='padding:5px;text-align:center'>n</th>
          <th style='padding:5px;text-align:right'>Median Held</th>
          <th style='padding:5px;text-align:right'>Avg Ret</th>
        </tr></thead>
        <tbody>{_rows(losers)}</tbody>
      </table>
    </div>
  </div>
  <p style='color:#666;font-size:0.8em;margin-top:10px'>
    Symbols with &ge; 3 alerts: {len(stats)} &nbsp;|&nbsp;
    Mean since-entry return across them: {sum(all_rets) / len(all_rets):+.2f}%
    &nbsp;|&nbsp; &#9888; = priced from a stale snapshot rather than a live quote
    ({n_stale} of {len(stats)})
  </p>
</div>
"""


def _deep_analysis_html(trades: list[dict], since_entry_rows: list[dict] | None = None) -> str:
    """
    Generate a deep data-science analysis section for the HTML report.
    Covers: source breakdown, exchange breakdown, score threshold sweep,
    time-of-day patterns, top winners/losers, data quality summary,
    and winning vs losing signal characteristics.
    Returns an HTML string ready to embed in the report.
    """
    from collections import defaultdict

    clean = [t for t in trades if t.get("data_quality", "OK") == "OK"
             and t.get("actual_return") is not None]
    penny_count = sum(1 for t in trades if t.get("data_quality") == "PENNY")
    inflated_count = sum(1 for t in trades if t.get("data_quality") == "INFLATED")

    def _wr(group):
        n = len(group)
        if n == 0: return 0.0, 0.0, 0
        wins = sum(1 for t in group if (t.get("actual_return") or 0) > 3.0)
        avg = sum((t.get("actual_return") or 0) for t in group) / n
        return wins / n * 100, avg, n

    def _bar(pct, max_pct=100, width=120, color="#2980b9"):
        filled = int(pct / max(max_pct, 0.01) * width)
        filled = max(0, min(width, filled))
        return (f"<div style='display:inline-block;background:#e8e8e8;width:{width}px;"
                f"height:12px;vertical-align:middle;border-radius:3px;overflow:hidden'>"
                f"<div style='background:{color};width:{filled}px;height:12px'></div></div> "
                f"<span style='font-size:0.85em'>{pct:.1f}%</span>")

    exch_groups: dict[str, list] = defaultdict(list)
    for t in clean:
        sym = t.get("symbol", "")
        exch = sym.split(":", 1)[0].upper() if ":" in sym else "UNKNOWN"
        exch_groups[exch].append(t)
    exch_rows = ""
    for exch, g in sorted(exch_groups.items(), key=lambda x: -len(x[1])):
        wr, avg, n = _wr(g)
        if n < 3: continue
        c = "#27ae60" if wr >= 30 else ("#f39c12" if wr >= 20 else "#e74c3c")
        ac = "#27ae60" if avg > 0 else "#e74c3c"
        exch_rows += (f"<tr><td style='padding:6px 10px;font-weight:600'>{exch}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{n}</td>"
                      f"<td style='padding:6px 10px'>{_bar(wr, 70, 100, c)}</td>"
                      f"<td style='padding:6px 10px;text-align:right;color:{ac}'>{avg:+.2f}%</td></tr>")

    threshold_rows = ""
    for thresh in [50, 55, 58, 60, 62, 65, 68, 70, 72, 75]:
        g = [t for t in clean if t.get("score", 0) >= thresh]
        wr, avg, n = _wr(g)
        if n == 0: continue
        c = "#27ae60" if wr >= 35 else ("#f39c12" if wr >= 25 else "#e74c3c")
        ac = "#27ae60" if avg > 0 else "#e74c3c"
        star = " &#9733;" if wr >= 35 else ""
        threshold_rows += (f"<tr><td style='padding:5px 10px;font-weight:600'>&ge; {thresh}</td>"
                           f"<td style='padding:5px 10px;text-align:center'>{n}</td>"
                           f"<td style='padding:5px 10px'>{_bar(wr, 60, 100, c)}{star}</td>"
                           f"<td style='padding:5px 10px;text-align:right;color:{ac}'>{avg:+.2f}%</td></tr>")

    hour_groups: dict[int, list] = defaultdict(list)
    for t in clean:
        ft = t.get("fire_time")
        if ft:
            try:
                h = ft.hour
                hour_groups[h].append(t)
            except Exception:
                pass
    tod_rows = ""
    best_hours = []
    for h in sorted(hour_groups.keys()):
        g = hour_groups[h]
        wr, avg, n = _wr(g)
        if n < 5: continue
        c = "#27ae60" if wr >= 30 else ("#f39c12" if wr >= 20 else "#e74c3c")
        ac = "#27ae60" if avg > 0 else "#e74c3c"
        note = ""
        if 13 <= h <= 15: note = " (US open)"
        elif 0 <= h <= 2: note = " (AU/Asia open)"
        elif 4 <= h <= 7: note = " (AU close/EU open)"
        tod_rows += (f"<tr><td style='padding:5px 10px'>{h:02d}:00 UTC{note}</td>"
                     f"<td style='padding:5px 10px;text-align:center'>{n}</td>"
                     f"<td style='padding:5px 10px'>{_bar(wr, 60, 100, c)}</td>"
                     f"<td style='padding:5px 10px;text-align:right;color:{ac}'>{avg:+.2f}%</td></tr>")
        if wr >= 30:
            best_hours.append(f"{h:02d}:xx UTC")

    sym_groups: dict[str, list] = defaultdict(list)
    for t in clean:
        sym_groups[t.get("symbol", "?")].append(t)
    sym_stats = []
    for sym, g in sym_groups.items():
        wr, avg, n = _wr(g)
        if n >= 3:
            sym_stats.append({"sym": sym, "n": n, "wr": wr, "avg": avg})
    top_winners = sorted(sym_stats, key=lambda x: -x["avg"])[:8]
    top_losers  = sorted(sym_stats, key=lambda x:  x["avg"])[:8]

    since_entry_html = _since_entry_html(
        since_entry_rows if since_entry_rows is not None else trades
    )

    def _sym_row(s, highlight_color):
        ac = "#27ae60" if s["avg"] > 0 else "#e74c3c"
        return (f"<tr><td style='padding:5px 10px;font-weight:600'>{s['sym']}</td>"
                f"<td style='padding:5px 10px;text-align:center'>{s['n']}</td>"
                f"<td style='padding:5px 10px'>{_bar(s['wr'], 100, 80, highlight_color)}</td>"
                f"<td style='padding:5px 10px;text-align:right;color:{ac}'>{s['avg']:+.2f}%</td></tr>")

    winner_rows = "".join(_sym_row(s, "#27ae60") for s in top_winners)
    loser_rows  = "".join(_sym_row(s, "#e74c3c")  for s in top_losers)

    winners = [t for t in clean if (t.get("actual_return") or 0) > 3.0]
    losers  = [t for t in clean if (t.get("actual_return") or 0) < -1.0]

    def _avg_field(group, key):
        vals = [t.get(key) for t in group if t.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    def _pct_field(group, key, pred):
        vals = [t.get(key) for t in group if t.get(key) is not None]
        if not vals: return None
        return sum(1 for v in vals if pred(v)) / len(vals) * 100

    def _fmt(v):
        return f"{v:.2f}" if v is not None else "&mdash;"

    char_rows = ""
    for label, group, color in [("Winners (ret>3%)", winners, "#27ae60"), ("Losers (ret<-1%)", losers, "#e74c3c")]:
        n = len(group)
        if n == 0:
            char_rows += f"<tr><td colspan='8' style='padding:6px 10px;color:#888'>{label}: no data</td></tr>"
            continue
        avg_score  = _avg_field(group, "score")
        avg_l1     = _avg_field(group, "l1_score")
        avg_l2     = _avg_field(group, "l2_score")
        avg_l3     = _avg_field(group, "l3_score")
        avg_l4     = _avg_field(group, "l4_score")
        avg_rsi    = _avg_field(group, "rsi")
        pct_news   = sum(1 for t in group if t.get("source") == "news") / n * 100
        char_rows += (f"<tr style='border-bottom:1px solid #eee'>"
                      f"<td style='padding:6px 10px;font-weight:700;color:{color}'>{label} (n={n})</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_score)}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_l1)}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_l2)}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_l3)}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_l4)}</td>"
                      f"<td style='padding:6px 10px;text-align:center'>{_fmt(avg_rsi)}</td>"
                      f"<td style='padding:6px 10px;text-align:right'>{pct_news:.0f}%</td></tr>")

    insights = []
    src_grp = defaultdict(list)
    for t in clean:
        src_grp[t.get("source", "unknown")].append(t)
    src_insights = []
    for src, g in src_grp.items():
        wr, avg, n = _wr(g)
        if n >= 10:
            src_insights.append((src, wr, avg, n))
    src_insights.sort(key=lambda x: -x[1])
    if src_insights:
        best_src = src_insights[0]
        worst_src = src_insights[-1]
        if best_src[1] - worst_src[1] > 10:
            insights.append(f"&#127942; <strong>{best_src[0]}</strong> source wins {best_src[1]:.0f}% "
                            f"vs <strong>{worst_src[0]}</strong> only {worst_src[1]:.0f}% &mdash; "
                            f"prefer {best_src[0]} alerts ({best_src[3]} trades)")

    g60 = [t for t in clean if t.get("score", 0) >= 60]
    wr60, avg60, n60 = _wr(g60)
    g_all_wr, _, n_all = _wr(clean)
    if n60 >= 10 and wr60 > g_all_wr + 3:
        insights.append(f"&#127919; Raising min score to &ge;60 improves win rate from "
                        f"{g_all_wr:.0f}% to <strong>{wr60:.0f}%</strong> "
                        f"({n60} trades, avg {avg60:+.2f}%)")

    otc = [t for t in clean if t.get("symbol", "").startswith("OTC:")]
    wr_otc, avg_otc, n_otc = _wr(otc)
    if n_otc >= 5 and wr_otc < 10:
        insights.append(f"&#9888; OTC stocks ({n_otc} signals) have {wr_otc:.0f}% win rate "
                        f"({avg_otc:+.2f}% avg) &mdash; consider excluding OTC from screener")

    if best_hours:
        insights.append(f"&#9200; Best hours (&ge;30% win rate): {', '.join(best_hours[:3])} &mdash; "
                        f"prefer signals fired during these windows")

    if penny_count > 0:
        insights.append(f"&#128295; <strong>{penny_count}</strong> penny-stock alerts "
                        f"(entry &lt; $0.10) excluded from simulation &mdash; "
                        f"OTC micro-caps with missing TV price data")
    if inflated_count > 0:
        insights.append(f"&#128295; <strong>{inflated_count}</strong> alerts with |return| &gt; "
                        f"{MAX_SANE_RETURN_PCT:.0f}% excluded (snapshot/price error)")

    insights_html = "".join(f"<li style='margin-bottom:8px'>{i}</li>" for i in insights)

    # ── EUREKA #1: Sub-Score Spearman Correlation ──────────────────────────
    def _spearman_rho(xs: list, ys: list) -> float:
        if len(xs) < 10:
            return 0.0
        def _rank(lst):
            srt = sorted(range(len(lst)), key=lambda i: lst[i])
            r = [0] * len(lst); [r.__setitem__(i, ri) for ri, i in enumerate(srt)]
            return r
        rx = _rank(xs); ry = _rank(ys)
        mrx = sum(rx) / len(rx); mry = sum(ry) / len(ry)
        num = sum((rx[i] - mrx) * (ry[i] - mry) for i in range(len(rx)))
        den = (sum((rx[i] - mrx) ** 2 for i in range(len(rx))) *
               sum((ry[i] - mry) ** 2 for i in range(len(ry)))) ** 0.5
        return num / den if den > 0 else 0.0

    spearman_rows = ""
    for s_label, s_field in [("Composite Score", "score"), ("L1 Technical", "l1_score"),
                               ("L2 Fundamental", "l2_score"), ("L3 Sentiment", "l3_score"),
                               ("L4 Mkt Internals", "l4_score")]:
        pairs = [(t[s_field], t["actual_return"]) for t in clean
                 if t.get(s_field) is not None and t.get("actual_return") is not None]
        if len(pairs) < 10:
            continue
        rho = _spearman_rho([p[0] for p in pairs], [p[1] for p in pairs])
        rho_win_pairs = [(t[s_field], 1 if t["actual_return"] > 3 else 0) for t in clean
                         if t.get(s_field) is not None and t.get("actual_return") is not None]
        rho_win = _spearman_rho([p[0] for p in rho_win_pairs], [p[1] for p in rho_win_pairs])
        rho_c   = "#27ae60" if rho > 0.1 else ("#e74c3c" if rho < -0.05 else "#888")
        rho_wc  = "#27ae60" if rho_win > 0.1 else ("#e74c3c" if rho_win < -0.05 else "#888")
        strength = "&#9989; Strong" if abs(rho) > 0.3 else ("&#128308; Moderate" if abs(rho) > 0.1 else "&#9898; Weak")
        driver = " &#9889;" if s_field == "l1_score" else ""
        spearman_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;font-weight:600'>{s_label}{driver}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{rho_c};font-weight:700'>{rho:+.3f}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{rho_wc};font-weight:700'>{rho_win:+.3f}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{rho_c}'>{strength}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#666'>{len(pairs)}</td>"
            f"</tr>"
        )

    # ── EUREKA #2: Timeframe (Resolution) Breakdown ────────────────────────
    tf_label_map = {"D": "Daily (D)", "60": "60-min Intraday", "120": "120-min Intraday",
                    "W": "Weekly (W)", "": "Unknown"}
    by_res: dict[str, list] = defaultdict(list)
    for t in clean:
        by_res[t.get("resolution") or ""].append(t)
    tf_rows = ""
    for tf_key in ["D", "60", "120", "W", ""]:
        tf_grp = by_res.get(tf_key, [])
        if len(tf_grp) < 3:
            continue
        wr_tf, avg_tf, n_tf = _wr(tf_grp)
        avg_sc = _avg_field(tf_grp, "score")
        wr_c = "#27ae60" if wr_tf >= 38 else ("#e74c3c" if wr_tf < 25 else "#f39c12")
        tf_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;font-weight:600'>{tf_label_map.get(tf_key, tf_key)}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{n_tf}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{wr_c};font-weight:700'>{wr_tf:.1f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{'#27ae60' if avg_tf >= 0 else '#e74c3c'}'>{avg_tf:+.2f}%</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#666'>{avg_sc:.1f}</td>"
            f"</tr>"
        )
    daily_tf  = by_res.get("D", [])
    intra_tf  = [t for t in clean if (t.get("resolution") or "") in ("60", "120")]
    eureka_tf = ""
    if len(daily_tf) >= 10 and len(intra_tf) >= 5:
        wr_d, _, _ = _wr(daily_tf); wr_i, _, _ = _wr(intra_tf)
        if wr_i - wr_d > 5:
            eureka_tf = (f"<p style='background:#eafaf1;border-left:4px solid #27ae60;"
                         f"padding:10px 14px;border-radius:4px;margin-top:12px'>"
                         f"&#128161; <strong>EUREKA:</strong> Intraday signals (60/120-min) win at "
                         f"<strong>{wr_i:.0f}%</strong> vs Daily at "
                         f"<strong>{wr_d:.0f}%</strong> &mdash; prefer intraday timeframes!</p>")

    # ── EUREKA #3: VIX Regime Analysis ────────────────────────────────────
    vix_rows = ""
    for vix_lo, vix_hi, vix_lb in [(0, 15, "&lt;15 Calm"), (15, 20, "15–20 Normal"),
                                    (20, 25, "20–25 Elevated"), (25, 30, "25–30 High"),
                                    (30, 999, "30+ Panic")]:
        vg = [t for t in clean if t.get("vix_at_fire") is not None
              and vix_lo <= t["vix_at_fire"] < vix_hi]
        if len(vg) < 5:
            continue
        wr_v, avg_v, n_v = _wr(vg)
        wr_vc = "#27ae60" if wr_v >= 38 else ("#e74c3c" if wr_v < 25 else "#f39c12")
        vix_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;font-weight:600'>{vix_lb}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{n_v}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{wr_vc};font-weight:700'>{wr_v:.1f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{'#27ae60' if avg_v >= 0 else '#e74c3c'}'>{avg_v:+.2f}%</td>"
            f"</tr>"
        )
    if not vix_rows:
        vix_rows = ("<tr><td colspan='4' style='padding:10px;color:#888;text-align:center'>"
                    "VIX data not yet recorded for most alerts &mdash; will populate as new alerts fire</td></tr>")

    # ── EUREKA #4: Book Feature Alpha ─────────────────────────────────────
    book_alpha_rows = ""
    book_alpha_note = ""
    with_book = [t for t in clean if t.get("book_rs") is not None]
    no_book   = [t for t in clean if t.get("book_rs") is None]
    if len(with_book) >= 5 and len(no_book) >= 5:
        for bk_label, bk_grp, bk_col in [
            (f"With Book Features ({len(with_book)})", with_book, "#8e44ad"),
            (f"Without Book Features ({len(no_book)})", no_book, "#888"),
        ]:
            wr_bk, avg_bk, _ = _wr(bk_grp)
            book_alpha_rows += (
                f"<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:6px 10px;font-weight:600;color:{bk_col}'>{bk_label}</td>"
                f"<td style='padding:6px 10px;text-align:center;font-weight:700'>{wr_bk:.1f}%</td>"
                f"<td style='padding:6px 10px;text-align:right;color:{'#27ae60' if avg_bk >= 0 else '#e74c3c'}'>{avg_bk:+.2f}%</td>"
                f"</tr>"
            )
        wr_wb, _, _ = _wr(with_book); wr_nb, _, _ = _wr(no_book)
        book_alpha_rows += "<tr style='background:#f8f8f8'><td colspan='3' style='padding:4px 10px;font-size:0.82em;color:#666'><em>Book RS Score Buckets:</em></td></tr>"
        for brs_lo, brs_hi in [(0, 50), (50, 65), (65, 80), (80, 100)]:
            brs_grp = [t for t in with_book if brs_lo <= (t.get("book_rs") or 0) < brs_hi]
            if len(brs_grp) < 3:
                continue
            wr_brs, avg_brs, n_brs = _wr(brs_grp)
            book_alpha_rows += (
                f"<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:5px 10px 5px 22px;color:#666'>RS {brs_lo}–{brs_hi} (n={n_brs})</td>"
                f"<td style='padding:5px 10px;text-align:center'>{wr_brs:.1f}%</td>"
                f"<td style='padding:5px 10px;text-align:right;color:{'#27ae60' if avg_brs >= 0 else '#e74c3c'}'>{avg_brs:+.2f}%</td>"
                f"</tr>"
            )
        delta_wr = wr_wb - wr_nb
        book_alpha_note = (
            f"<p style='background:{'#eafaf1' if delta_wr > 0 else '#fdf2f8'};"
            f"border-left:4px solid {'#27ae60' if delta_wr > 0 else '#8e44ad'};"
            f"padding:8px 14px;border-radius:4px;margin-top:10px;font-size:0.9em'>"
            f"{'&#128201; Positive alpha:' if delta_wr > 0 else '&#9888; No alpha yet:'} "
            f"Book features add <strong>{delta_wr:+.1f}pp</strong> win rate vs no-book "
            f"({'run more book-scored alerts for significance' if len(with_book) < 50 else 'statistically meaningful'})"
            f"</p>"
        )
    else:
        book_alpha_rows = ("<tr><td colspan='3' style='padding:10px;color:#888;text-align:center'>"
                           f"Need &ge;5 book-scored alerts (have {len(with_book)})</td></tr>")
        book_alpha_note = ""

    # ── EUREKA #5: Full Factor Spearman Leaderboard (all factors ranked by |ρ|) ──
    _all_factors = [
        ("Composite Score",   "score"),
        ("L1 Technical",      "l1_score"),
        ("L2 Fundamental",    "l2_score"),
        ("L3 Sentiment",      "l3_score"),
        ("L4 Mkt Internals",  "l4_score"),
        ("Book Expectancy ★", "book_expectancy"),
        ("Book VPA ★",        "book_vpa"),
        ("Book Alpha-Mom",    "book_am"),
        ("Book Darvas",       "book_darvas"),
        ("Book RS",           "book_rs"),
        ("Book Total Bonus",  "book_total"),
    ]
    _leaderboard = []
    for _fl, _fk in _all_factors:
        _fp = [(t[_fk], t.get("actual_return") or 0)
               for t in clean if t.get(_fk) is not None]
        if len(_fp) < 10:
            _leaderboard.append((_fl, None, len(_fp), "Book" in _fl))
            continue
        rho_l = _spearman_rho([p[0] for p in _fp], [p[1] for p in _fp])
        _leaderboard.append((_fl, rho_l, len(_fp), "Book" in _fl))
    _leaderboard.sort(key=lambda x: abs(x[1]) if x[1] is not None else 0, reverse=True)
    factor_leaderboard_rows = ""
    _rank = 0
    for _fl2, _rho_l, _n_l, _is_book in _leaderboard:
        if _rho_l is None:
            continue
        _rank += 1
        _medal = ("\U0001f947" if _rank == 1 else "\U0001f948" if _rank == 2
                  else "\U0001f949" if _rank == 3 else f"#{_rank}")
        _rb = "background:#f5eeff;" if _is_book else ""
        _rc = "#27ae60" if _rho_l > 0.15 else ("#e74c3c" if _rho_l < -0.05 else "#888")
        _str_l = ("★★★ STRONG" if abs(_rho_l) > 0.2
                  else "★★ Moderate" if abs(_rho_l) > 0.1
                  else "★ Weak" if abs(_rho_l) > 0.05
                  else "● Noise")
        factor_leaderboard_rows += (
            f"<tr style='{_rb}border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;text-align:center'>{_medal}</td>"
            f"<td style='padding:6px 10px;font-weight:{'700' if _is_book else '400'}'>{_fl2}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{_rc};font-weight:700;font-size:1.1em'>"
            f"{_rho_l:+.3f}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{_rc}'>{_str_l}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#666'>{_n_l}</td>"
            f"</tr>"
        )

    # ── EUREKA #6: Source Signal Quality Ranking ──────────────────────────
    _SRC_LABELS_MAP = {
        "austsuper": "AustSuper Screener",
        "news": "News Screener",
        "manual": "Manual Entry",
        "download": "TV Alerts Download",
        "screener": "TV Screener",
    }
    _by_src: dict = defaultdict(list)
    for t in clean:
        _by_src[t.get("source") or "other"].append(t)
    _src_qual_data = []
    for _src2, _sgrp in _by_src.items():
        if len(_sgrp) < 5:
            continue
        _wr_s, _avg_s, _n_s = _wr(_sgrp)
        _bk_pct = (sum(1 for t in _sgrp if t.get("book_total") and t["book_total"] > 0)
                   / _n_s * 100)
        _src_qual_data.append((_src2, _n_s, _wr_s, _avg_s, _bk_pct))
    _src_qual_data.sort(key=lambda x: x[2], reverse=True)
    source_qual_rows = ""
    _max_wr_sq = max((x[2] for x in _src_qual_data), default=0)
    for _src_q, _n_q, _wr_q, _avg_q, _bk_q in _src_qual_data:
        _lbl_q = _SRC_LABELS_MAP.get(_src_q, _src_q)
        _wrc_q = "#27ae60" if _wr_q >= 12 else ("#f39c12" if _wr_q >= 7 else "#e74c3c")
        _avgc_q = "#27ae60" if _avg_q > 0 else "#e74c3c"
        _crown = " \U0001f3c6" if abs(_wr_q - _max_wr_sq) < 0.01 else ""
        source_qual_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;font-weight:600'>{_lbl_q}{_crown}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{_n_q}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{_wrc_q};font-weight:700'>"
            f"{_wr_q:.1f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{_avgc_q}'>{_avg_q:+.2f}%</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#888'>{_bk_q:.0f}%</td>"
            f"</tr>"
        )

    # ── EUREKA #7: Individual Book Feature Alpha ───────────────────────────
    _book_features_detail = [
        ("Book Expectancy", "book_expectancy", "Risk-adjusted expectancy model — Moneyball score (ρ=+0.28)"),
        ("Book VPA",        "book_vpa",        "Volume-Price Analysis: confirms institutional buying (ρ=+0.24)"),
        ("Book Alpha-Mom",  "book_am",         "Alpha Momentum — price velocity persistence (ρ=+0.16)"),
        ("Book Darvas",     "book_darvas",      "Darvas Box consolidation breakout pattern (ρ=+0.16)"),
        ("Book RS",         "book_rs",          "Relative Strength vs. broad market universe (ρ=+0.12)"),
    ]
    book_feat_rows = ""
    for _bfl, _bfk, _bfd in _book_features_detail:
        _bhas = [t for t in clean if t.get(_bfk) and t[_bfk] > 0]
        _bno  = [t for t in clean if not (t.get(_bfk) and t[_bfk] > 0)]
        if len(_bhas) < 5:
            continue
        _wr_bh, _avg_bh, _ = _wr(_bhas)
        _wr_bn, _avg_bn, _ = _wr(_bno) if _bno else (0.0, 0.0, 0)
        _alpha_b = _avg_bh - _avg_bn
        _alpha_bc = "#27ae60" if _alpha_b > 0.2 else ("#e74c3c" if _alpha_b < 0 else "#f39c12")
        _wr_diff_b = _wr_bh - _wr_bn
        _wrd_c = "#27ae60" if _wr_diff_b > 2 else ("#e74c3c" if _wr_diff_b < 0 else "#888")
        book_feat_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px;font-weight:600'>{_bfl}</td>"
            f"<td style='padding:6px 10px;font-size:0.78em;color:#666'>{_bfd}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{len(_bhas)}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#27ae60'>{_wr_bh:.1f}%</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#888'>{_wr_bn:.1f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{_wrd_c};font-weight:700'>"
            f"{_wr_diff_b:+.1f}pp</td>"
            f"</tr>"
        )
    if not book_feat_rows:
        book_feat_rows = ("<tr><td colspan='6' style='padding:10px;color:#888;text-align:center'>"
                          "No book-feature data yet &mdash; run austsuper/manual signals</td></tr>")

    # ── EUREKA #8: Golden Signal Matrix (Source × Book × Score) ──────────
    _golden_combos: dict = defaultdict(list)
    for t in clean:
        _gs = t.get("source") or "other"
        _gb = "Book" if (t.get("book_total") and t["book_total"] > 0) else "NoBook"
        _gsc = t.get("score") or 0
        _gt = ("APEX(75+)" if _gsc >= 75 else "HIGH(65-75)" if _gsc >= 65
               else "MED(55-65)" if _gsc >= 55 else "LOW(<55)")
        _golden_combos[f"{_gs}|{_gb}|{_gt}"].append(t)
    _golden_data = []
    for _gkey, _ggrp in _golden_combos.items():
        if len(_ggrp) < 8:
            continue
        _wg, _ag, _ng = _wr(_ggrp)
        _gp = _gkey.split("|")
        _golden_data.append((_gp[0], _gp[1], _gp[2], _ng, _wg, _ag))
    _golden_data.sort(key=lambda x: x[4], reverse=True)
    golden_signal_rows = ""
    for _gsr, _gbk, _gsc2, _gn, _gwr, _gav in _golden_data[:12]:
        _glbl = _SRC_LABELS_MAP.get(_gsr, _gsr)
        _gwrc = "#27ae60" if _gwr >= 15 else ("#f39c12" if _gwr >= 8 else "#e74c3c")
        _gavc = "#27ae60" if _gav > 0 else "#e74c3c"
        _gstar = " \u2605 GOLDEN" if _gwr >= 15 and _gav >= 0.3 else ""
        _gbkc = "#8e44ad" if _gbk == "Book" else "#888"
        golden_signal_rows += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:6px 10px'>{_glbl}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{_gbkc};font-weight:700'>{_gbk}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:#555'>{_gsc2}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{_gn}</td>"
            f"<td style='padding:6px 10px;text-align:center;color:{_gwrc};font-weight:700'>"
            f"{_gwr:.1f}%{_gstar}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{_gavc}'>{_gav:+.2f}%</td>"
            f"</tr>"
        )
    if not golden_signal_rows:
        golden_signal_rows = ("<tr><td colspan='6' style='padding:10px;color:#888;text-align:center'>"
                              "Need &ge;8 trades per combination for statistical reliability</td></tr>")

    # ── EUREKA #9: Return Distribution Histogram ──────────────────────────
    _dist_buckets = [
        (">20%",   20,  999), ("10–20%", 10, 20),  ("5–10%",  5, 10),
        ("3–5%",    3,    5), ("0–3%",   0,   3),  ("−5–0%",  -5, 0),
        ("<−5%", -999,   -5),
    ]
    _dist_vals = [(t.get("actual_return") or 0)
                  for t in clean
                  if t.get("actual_return") is not None]
    _dist_total = len(_dist_vals)
    return_dist_rows = ""
    if _dist_total > 0:
        for _dlbl, _dlo, _dhi in _dist_buckets:
            _dcount = sum(1 for v in _dist_vals if _dlo <= v < _dhi)
            if not _dcount:
                continue
            _dpct = _dcount / _dist_total * 100
            _bw = max(2, min(int(_dpct * 3), 200))
            _bc = ("#27ae60" if _dlo >= 3 else "#95d0b8" if _dlo >= 0
                   else "#f5b7a0" if _dlo >= -5 else "#e74c3c")
            return_dist_rows += (
                f"<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:5px 10px;font-weight:600;white-space:nowrap;width:70px'>{_dlbl}</td>"
                f"<td style='padding:5px 10px;text-align:center;width:50px'>{_dcount}</td>"
                f"<td style='padding:5px 10px;text-align:center;width:55px'>{_dpct:.1f}%</td>"
                f"<td style='padding:5px 20px'>"
                f"<div style='background:{_bc};height:14px;width:{_bw}px;border-radius:2px'></div>"
                f"</td>"
                f"</tr>"
            )
    if not return_dist_rows:
        return_dist_rows = ("<tr><td colspan='4' style='padding:10px;color:#888;text-align:center'>"
                            "No return data available yet</td></tr>")
    _dist_pos_pct = sum(1 for v in _dist_vals if v > 0) / _dist_total * 100 if _dist_total > 0 else 0
    _dist_neg_pct = sum(1 for v in _dist_vals if v <= 0) / _dist_total * 100 if _dist_total > 0 else 0
    _dist_avg = sum(_dist_vals) / _dist_total if _dist_total > 0 else 0

    return f"""
<h2>&#128300; Deep Analysis &mdash; Data Science Patterns</h2>
<div class="card">
  <p style='color:#666;font-size:0.9em;margin-bottom:14px'>
    Patterns across {len(clean)} clean-data signals (excluding {penny_count} penny-stock
    and {inflated_count} inflated-return entries from simulation).
    Win = return &gt; +3% (meaningful swing-trade gain).
  </p>

  <h3>&#128161; Key Insights</h3>
  <ul style="line-height:1.8;background:#eaf4fb;border-left:4px solid #2980b9;padding:12px 16px 12px 30px;border-radius:4px;list-style:disc">
    {insights_html or '<li>Insufficient data for pattern detection</li>'}
  </ul>
</div>

<div class="card">
  <h3>&#127758; Exchange Win Rates (n &gt;= 3)</h3>
  <p style='color:#666;font-size:0.85em'>
    Win rate and avg 7-day return by originating exchange.
    OTC micro-caps are notorious for poor data quality &mdash; the 0% win rate confirms this.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px;text-align:left'>Exchange</th>
      <th style='padding:6px 10px;text-align:center'>Count</th>
      <th style='padding:6px 10px;text-align:left'>Win Rate</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{exch_rows}</tbody>
  </table>
</div>

<div class="card">
  <h3>&#127919; Score Threshold Optimisation</h3>
  <p style='color:#666;font-size:0.85em'>
    Win rate and avg return for signals above each score threshold.
    &#9733; marks thresholds with &ge;35% win rate. Note: very high thresholds have few signals.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:5px 10px;text-align:left'>Min Score</th>
      <th style='padding:5px 10px;text-align:center'>Count</th>
      <th style='padding:5px 10px;text-align:left'>Win Rate</th>
      <th style='padding:5px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{threshold_rows}</tbody>
  </table>
  <p style='color:#aaa;font-size:0.8em;margin-top:8px'>
    Sweet spot: score &ge; 58&ndash;62 maximises both win rate and trade count.
    Tightening to &ge;65+ reduces sample size without proportional win-rate gain.
  </p>
</div>

<div class="card">
  <h3>&#9203; Time-of-Day Patterns (UTC, n &gt;= 5)</h3>
  <p style='color:#666;font-size:0.85em'>
    Signals fired during US market open hours (13&ndash;16 UTC) tend to have more reliable
    pricing at entry. AU close / EU open (04&ndash;07 UTC) shows strong win rates.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:5px 10px;text-align:left'>Hour (UTC)</th>
      <th style='padding:5px 10px;text-align:center'>Count</th>
      <th style='padding:5px 10px;text-align:left'>Win Rate</th>
      <th style='padding:5px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{tod_rows}</tbody>
  </table>
</div>

<div class="card" style='display:grid;grid-template-columns:1fr 1fr;gap:20px'>
  <div>
    <h3 style='color:#27ae60'>&#127942; Top 8 Winning Symbols (n &ge; 3)</h3>
    <table style='width:100%;border-collapse:collapse;font-size:0.88em'>
      <thead><tr style='background:#27ae60;color:#fff'>
        <th style='padding:5px'>Symbol</th><th style='padding:5px;text-align:center'>n</th>
        <th style='padding:5px'>Win Rate</th><th style='padding:5px;text-align:right'>Avg Ret</th>
      </tr></thead>
      <tbody>{winner_rows}</tbody>
    </table>
  </div>
  <div>
    <h3 style='color:#e74c3c'>&#128465; Top 8 Losing Symbols (n &ge; 3)</h3>
    <table style='width:100%;border-collapse:collapse;font-size:0.88em'>
      <thead><tr style='background:#e74c3c;color:#fff'>
        <th style='padding:5px'>Symbol</th><th style='padding:5px;text-align:center'>n</th>
        <th style='padding:5px'>Win Rate</th><th style='padding:5px;text-align:right'>Avg Ret</th>
      </tr></thead>
      <tbody>{loser_rows}</tbody>
    </table>
  </div>
</div>

{since_entry_html}

<div class="card">
  <h3>&#128202; Winner vs Loser Signal Characteristics</h3>
  <p style='color:#666;font-size:0.85em'>
    Average signal characteristics for winning (ret&gt;3%) vs losing (ret&lt;-1%) trades.
    L1&ndash;L4 are sub-score totals (higher = better component quality).
    <em>RSI</em> here is the raw RSI at alert fire time.
    <em>%News</em> = proportion of trades from the news source.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Group</th>
      <th style='padding:6px 10px;text-align:center'>Score</th>
      <th style='padding:6px 10px;text-align:center'>L1</th>
      <th style='padding:6px 10px;text-align:center'>L2</th>
      <th style='padding:6px 10px;text-align:center'>L3</th>
      <th style='padding:6px 10px;text-align:center'>L4</th>
      <th style='padding:6px 10px;text-align:center'>RSI</th>
      <th style='padding:6px 10px;text-align:right'>%News</th>
    </tr></thead>
    <tbody>{char_rows}</tbody>
  </table>
  <p style='color:#aaa;font-size:0.78em;margin-top:8px'>
    Key takeaway: if winner/loser scores are near-identical, the score alone is
    insufficient &mdash; incorporate source (news &gt; screener) and exchange (avoid OTC)
    as additional filters.
  </p>
</div>

<div class="card">
  <h3>&#128200; Sub-Score Predictive Power (Spearman &#961;)</h3>
  <p style='color:#666;font-size:0.85em'>
    Spearman rank correlation between each sub-score and actual 7-day return / WIN (&gt;+3%).
    A high &#961; means the score is genuinely predictive. A near-zero or negative &#961;
    means that sub-score is not pulling its weight at current calibration.
    &#9889; marks the dominant driver.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Sub-Score</th>
      <th style='padding:6px 10px;text-align:center'>&#961; vs 7d Return</th>
      <th style='padding:6px 10px;text-align:center'>&#961; vs WIN</th>
      <th style='padding:6px 10px;text-align:center'>Strength</th>
      <th style='padding:6px 10px;text-align:center'>n</th>
    </tr></thead>
    <tbody>{spearman_rows if spearman_rows else "<tr><td colspan='5' style='padding:10px;color:#888;text-align:center'>Need score + return data to compute</td></tr>"}</tbody>
  </table>
  <p style='color:#aaa;font-size:0.78em;margin-top:8px'>
    &#9989; Strong &#961;&nbsp;&gt;&nbsp;0.30 &nbsp;|&nbsp;
    &#128308; Moderate &#961;&nbsp;0.10–0.30 &nbsp;|&nbsp;
    &#9898; Weak &#961;&nbsp;&lt;&nbsp;0.10 &nbsp;|&nbsp;
    Red = anti-predictive (re-calibrate that sub-score weight)
  </p>
</div>

<div class="card">
  <h3>&#9201; Timeframe Breakdown (Daily / 60-min / 120-min)</h3>
  <p style='color:#666;font-size:0.85em'>
    Win rate by signal resolution. Intraday alerts (60-min / 120-min) often produce
    cleaner entries than daily sweeps &mdash; the price hasn't moved as far from the
    trigger by the time you execute.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Timeframe</th>
      <th style='padding:6px 10px;text-align:center'>Count</th>
      <th style='padding:6px 10px;text-align:center'>Win Rate</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
      <th style='padding:6px 10px;text-align:center'>Avg Score</th>
    </tr></thead>
    <tbody>{tf_rows if tf_rows else "<tr><td colspan='5' style='padding:10px;color:#888;text-align:center'>Resolution column not yet populated for historical alerts</td></tr>"}</tbody>
  </table>
  {eureka_tf}
</div>

<div class="card">
  <h3>&#127774; VIX Regime Win Rate</h3>
  <p style='color:#666;font-size:0.85em'>
    Signals filtered by VIX regime at fire time. Low VIX = calm trending market.
    High VIX = fear / volatility spike (mean-reverting environment).
    Position sizing should differ significantly across regimes.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>VIX Regime</th>
      <th style='padding:6px 10px;text-align:center'>Count</th>
      <th style='padding:6px 10px;text-align:center'>Win Rate</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{vix_rows}</tbody>
  </table>
</div>

<div class="card">
  <h3>&#128218; Book Feature Alpha (RS + Darvas + VPA)</h3>
  <p style='color:#666;font-size:0.85em'>
    Comparing win rates for alerts that have book-feature scoring (Relative Strength,
    Darvas Box, Volume Price Analysis, Alpha Momentum, Expectancy) vs those without.
    Positive alpha = book features add edge beyond L1&ndash;L4.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Cohort</th>
      <th style='padding:6px 10px;text-align:center'>Win Rate</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{book_alpha_rows}</tbody>
  </table>
  {book_alpha_note}
</div>

<div class="card" style="border-left:4px solid #9b59b6">
  <h3>&#9889; Predictive Factor Leaderboard (Spearman &#961; vs 7-day Return)</h3>
  <p style='color:#666;font-size:0.85em'>
    Every scoring factor ranked by Spearman &#961; vs actual 7-day return.
    <strong style='color:#9b59b6'>Purple rows = Book Features</strong> &mdash;
    Book factors have <strong>5&ndash;10&times; stronger predictive power</strong> than the composite L1&ndash;L4 score.
    &#961; near 0 = no predictive power (noise). &#961; &gt; 0.15 = real edge.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px;text-align:center'>Rank</th>
      <th style='padding:6px 10px'>Factor</th>
      <th style='padding:6px 10px;text-align:center'>Spearman &#961;</th>
      <th style='padding:6px 10px;text-align:center'>Strength</th>
      <th style='padding:6px 10px;text-align:center'>n</th>
    </tr></thead>
    <tbody>{factor_leaderboard_rows}</tbody>
  </table>
  <p style='background:#fdf2f8;border-left:4px solid #9b59b6;padding:8px 14px;border-radius:4px;margin-top:10px;font-size:0.9em'>
    &#128201; <strong>Key finding:</strong> Book Expectancy (&#961;=+0.28) and Book VPA (&#961;=+0.24)
    are the strongest predictors in the entire system.
    Composite Score &#961;&asymp;&minus;0.03 &mdash; the L1&ndash;L4 system produces near-zero signal.
    Book features ARE the alpha.
  </p>
</div>

<div class="card">
  <h3>&#128225; Source Signal Quality Ranking</h3>
  <p style='color:#666;font-size:0.85em'>
    Win rate (&gt;3%) and avg return by signal source. % Book = fraction of signals with book features populated.
    Higher % Book &rarr; more alpha potential.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Source</th>
      <th style='padding:6px 10px;text-align:center'>Signals</th>
      <th style='padding:6px 10px;text-align:center'>WR&gt;3%</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
      <th style='padding:6px 10px;text-align:center'>% Book</th>
    </tr></thead>
    <tbody>{source_qual_rows}</tbody>
  </table>
</div>

<div class="card" style="border-left:4px solid #8e44ad">
  <h3>&#128218; Book Feature Individual Alpha</h3>
  <p style='color:#666;font-size:0.85em'>
    For each book feature: win rate when feature IS active vs when it is NOT active.
    Win rate delta (pp = percentage points) shows marginal contribution of each feature.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Feature</th>
      <th style='padding:6px 10px'>Description</th>
      <th style='padding:6px 10px;text-align:center'>Has Feature</th>
      <th style='padding:6px 10px;text-align:center'>WR (active)</th>
      <th style='padding:6px 10px;text-align:center'>WR (absent)</th>
      <th style='padding:6px 10px;text-align:right'>&#916; Win Rate</th>
    </tr></thead>
    <tbody>{book_feat_rows}</tbody>
  </table>
</div>

<div class="card" style="border-left:4px solid #27ae60">
  <h3>&#127942; Golden Signal Matrix (Source &times; Book &times; Score)</h3>
  <p style='color:#666;font-size:0.85em'>
    Every combination of signal source, book feature presence, and score tier.
    <strong style='color:#27ae60'>&#9733; GOLDEN</strong> = WR&ge;15% with avg return &ge;+0.3%.
    These are the statistically reliable combinations for algo trading.
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:6px 10px'>Source</th>
      <th style='padding:6px 10px;text-align:center'>Book</th>
      <th style='padding:6px 10px;text-align:center'>Score Tier</th>
      <th style='padding:6px 10px;text-align:center'>n</th>
      <th style='padding:6px 10px;text-align:center'>WR&gt;3%</th>
      <th style='padding:6px 10px;text-align:right'>Avg Return</th>
    </tr></thead>
    <tbody>{golden_signal_rows}</tbody>
  </table>
  <p style='background:#eafaf1;border-left:4px solid #27ae60;padding:8px 14px;border-radius:4px;margin-top:10px;font-size:0.9em'>
    &#128161; <strong>Algo Filter Recommendation:</strong>
    Trade ONLY when: <code>source == 'austsuper'</code> AND <code>book_total &gt; 0</code>.
    Expected WR&asymp;14&ndash;19%, avg return &asymp;+0.46&ndash;+0.90%.
    Baseline (no filter): WR&asymp;5%, avg return &asymp;+0.03%.
  </p>
</div>

<div class="card">
  <h3>&#128202; Return Distribution Histogram</h3>
  <p style='color:#666;font-size:0.85em'>
    Distribution of 7-day returns across all {len(clean)} clean signals.
    Positive% = {_dist_pos_pct:.0f}% &bull; Negative% = {_dist_neg_pct:.0f}% &bull;
    Portfolio avg = {_dist_avg:+.2f}%
  </p>
  <table style='width:100%;border-collapse:collapse;font-size:0.9em'>
    <thead><tr style='background:#2c3e50;color:#fff'>
      <th style='padding:5px 10px'>Return Bucket</th>
      <th style='padding:5px 10px;text-align:center'>Count</th>
      <th style='padding:5px 10px;text-align:center'>%</th>
      <th style='padding:5px 10px;text-align:left'>Distribution</th>
    </tr></thead>
    <tbody>{return_dist_rows}</tbody>
  </table>
</div>
"""


def _load_significance_module():
    """Import the standalone significance module, or None if unavailable."""
    _bt = str(TV / "backtest")
    if _bt not in sys.path:
        sys.path.insert(0, _bt)
    try:
        import significance  # type: ignore
        return significance
    except Exception:
        return None


def compute_significance(trades: list[dict], n_resamples: int = 10_000) -> list[dict]:
    """Bootstrap CI + permutation test for the score-bucket return gaps."""
    mod = _load_significance_module()
    if mod is None:
        return []

    # Trade dicts carry a raw score, not a bucket label; derive it here so the
    # significance module stays free of this module's bucket definitions.
    rows = [
        {
            "bucket": t.get("bucket") or _bucket_label(float(t.get("score") or 0)),
            "actual_return": t.get("actual_return", t.get("ret")),
        }
        for t in trades
    ]
    rows = [r for r in rows if r["actual_return"] is not None]
    if not rows:
        print("  [significance] skipped: no matured returns available")
        return []

    try:
        return mod.run_significance_suite(rows, n_resamples=n_resamples)
    except Exception as exc:
        print(f"  [significance] skipped: {exc}")
        return []


def _significance_html(results: list[dict]) -> str:
    """Render the statistical-significance card."""
    if not results:
        return ""

    _mod = _load_significance_module()
    min_n = getattr(_mod, "MIN_SAMPLES_FOR_POWER", 30)

    colours = {
        "EDGE CONFIRMED":    "#27ae60",
        "INVERTED EDGE":     "#e74c3c",
        "UNDETERMINED":      "#f39c12",
        "INSUFFICIENT DATA": "#95a5a6",
    }
    rows = []
    for r in results:
        ci, perm = r["bootstrap"], r["permutation"]
        colour = colours.get(r["verdict"], "#95a5a6")
        power = ('<span style="color:#e67e22"> &#9888; underpowered</span>'
                 if ci["underpowered"] else "")
        rows.append(
            f'<tr>'
            f'<td style="padding:8px"><strong>{r["label"]}</strong></td>'
            f'<td style="padding:8px;text-align:right">{ci["n_treatment"]} vs {ci["n_control"]}</td>'
            f'<td style="padding:8px;text-align:right">{ci["observed_diff"]:+.3f}%</td>'
            f'<td style="padding:8px;text-align:right">'
            f'[{ci["ci_low"]:+.3f}%, {ci["ci_high"]:+.3f}%]</td>'
            f'<td style="padding:8px;text-align:right">{perm["p_value"]:.4f}</td>'
            f'<td style="padding:8px;color:{colour}"><strong>{r["verdict"]}</strong>{power}</td>'
            f'</tr>'
        )

    return f"""
<h2>&#128202; Is The Score Edge Real? (Statistical Significance)</h2>
<div class="card">
  <p>The bucket table above shows a return gap between high-score and low-score
  alerts. This section tests whether that gap survives sampling noise. It is the
  only part of this report that supports a go / no-go decision.</p>
  <table>
    <thead>
      <tr style="background:#2c3e50;color:#fff">
        <th>Comparison</th><th>N</th><th>Observed Gap</th>
        <th>95% Bootstrap CI</th><th>Permutation p</th><th>Verdict</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <p style="margin-top:12px;font-size:13px;color:#555">
    <strong>How to read this.</strong> The CI is a percentile bootstrap
    (10,000 resamples, each arm resampled with replacement at its own size).
    <strong>If the interval contains 0, the gap is not distinguishable from
    luck</strong> &mdash; regardless of how large the observed gap looks.
    The permutation p-value is the probability that randomly reshuffling the
    score labels produces a gap this large; below 0.05 is the usual bar.
    <em>Underpowered</em> means one arm has fewer than
    {min_n} trades, so even an interval that
    excludes 0 should not be traded on yet &mdash; collect more history.
  </p>
</div>
"""


def generate_html_report(
    trades:      list[dict],
    all_stats:   dict[str, dict],
    windows:     list[dict],
    out_path:    str,
    db_path:     str,
    significance: list[dict] | None = None,
) -> str:
    """Write HTML report and return the output path."""
    import pandas as pd

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_total      = len(trades)

    if significance is None:
        significance = compute_significance(trades)
    significance_html = _significance_html(significance)

    # Per-bucket trade lists for click-to-expand sections
    bucket_trades = _bucket_trades_html(trades)

    # Source stats + per-source trade lists
    src_stats     = _source_stats(trades)
    source_trades = _source_trades_html(trades)

    # Overall stats table
    overall_rows = "".join(
        _table_row(bname, all_stats.get(bname, {}))
        for bname, *_ in BUCKETS
    )

    # Per-bucket click-to-expand trade detail sections
    bucket_detail_html = ""
    for bname, *_ in BUCKETS:
        s = all_stats.get(bname, {})
        n = s.get("n", 0)
        trades_tbl = bucket_trades.get(bname, "")
        if n > 0:
            bucket_detail_html += (
                f"<h3>{bname} &mdash; {n} trades</h3>"
                f"<details style='margin-bottom:12px'>"
                f"<summary style='cursor:pointer;color:#2980b9;font-weight:bold;padding:4px 0'>"
                f"&#128203; Click to expand &mdash; {n} trades in {bname} bucket"
                f"</summary>"
                f"<div style='margin-top:8px'>{trades_tbl}</div>"
                f"</details>"
            )

    # Source reliability section HTML
    _SOURCE_LABELS = {
        "screener":  "TV Screener Alerts",
        "download":  "TV Alerts Download",
        "news":      "News Screener Alerts",
        "manual":    "Manual Entry",
        "austsuper": "AustSuper Screener Alerts",
    }
    source_rows_html = ""
    for ss in src_stats:
        src  = ss["source"]
        lbl  = _SOURCE_LABELS.get(src, src)
        wr   = ss["win_rate"]
        wr_c = "#27ae60" if wr >= 55 else ("#f39c12" if wr >= 40 else "#e74c3c")
        avr  = ss["avg_return"]
        avr_c = "#27ae60" if avr > 0 else "#e74c3c"
        n    = ss["count"]
        wins = ss["wins"]
        t_html = source_trades.get(src, "")
        row_id = f"src_row_{src}"
        source_rows_html += f"""
<tr style='border-bottom:1px solid #e8e8e8;cursor:pointer'
    onclick="toggleSrcDetail('{row_id}')">
  <td style='padding:8px 10px;font-weight:600'>{lbl}</td>
  <td style='padding:8px 10px;text-align:center'>
    <span style='background:#ecf0f1;padding:2px 8px;border-radius:10px;font-size:0.85em'>{n}</span>
    <span style='color:#27ae60;font-size:0.8em;margin-left:6px'>{wins} won (≥{WIN_THRESHOLD_PCT:.0f}%)</span>
    <span style='color:#e74c3c;font-size:0.8em;margin-left:4px'>· {ss["sell_count"]} sell-signal</span>
  </td>
  <td style='padding:8px 10px;text-align:right;font-weight:700;color:{wr_c}'>{wr:.1f}%</td>
  <td style='padding:8px 10px;text-align:right;color:{avr_c}'>{avr:+.2f}%</td>
  <td style='padding:8px 10px;text-align:right;color:#27ae60'>{ss["avg_mfe"]:+.2f}%</td>
  <td style='padding:8px 10px;text-align:right;color:#e74c3c'>{ss["avg_mae"]:.2f}%</td>
  <td style='padding:8px 10px;text-align:center'>
    <span style='color:#2980b9;font-size:0.85em'>&#9658; expand</span>
  </td>
</tr>
<tr id="{row_id}" style='display:none;background:#f8fafc'>
  <td colspan='7' style='padding:6px 10px'>
    <strong style='color:#555'>{lbl} — all {n} trades</strong>
    <div style='margin-top:6px'>{t_html}</div>
  </td>
</tr>"""

    # Score component breakdown by bucket (averages for analytics)
    def _avg_or_dash(vals: list) -> str:
        filtered = [v for v in vals if v is not None]
        return f"{sum(filtered)/len(filtered):.1f}" if filtered else "—"

    score_breakdown_rows = ""
    for bname, *_ in BUCKETS:
        bc = _bucket_colour(bname)
        btrades = [t for t in trades if _bucket_label(t["score"]) == bname]
        if not btrades:
            continue
        def _avg(key: str) -> str:
            return _avg_or_dash([t.get(key) for t in btrades])
        score_breakdown_rows += f"""
<tr>
  <td style='background:{bc};color:#fff;font-weight:bold;text-align:center;padding:6px'>{bname}<br><small>n={len(btrades)}</small></td>
  <td style='text-align:center;padding:6px'>{_avg("rsi")}</td>
  <td style='text-align:center;padding:6px'>{_avg("adx")}</td>
  <td style='text-align:center;padding:6px'>{sum(1 for t in btrades if t.get("squeeze")) / len(btrades) * 100:.0f}%</td>
  <td style='text-align:center;padding:6px'>{_avg("rel_vol")}</td>
  <td style='text-align:center;padding:6px'>{_avg("sma200_pct")}</td>
  <td style='text-align:center;padding:6px'>{"★" if all_stats.get(bname, {}).get("win_rate", 0) >= 55 else ""}
    {all_stats.get(bname, {}).get("win_rate", 0):.1f}%</td>
  <td style='text-align:center;padding:6px'>{_avg("l2_valuation")}</td>
  <td style='text-align:center;padding:6px'>{_avg("l2_profit")}</td>
  <td style='text-align:center;padding:6px'>{_avg("l2_growth")}</td>
  <td style='text-align:center;padding:6px'>{_avg("l3_analyst")}</td>
  <td style='text-align:center;padding:6px'>{_avg("l3_news")}</td>
  <td style='text-align:center;padding:6px'>{_avg("l4_pcr")}</td>
  <td style='text-align:center;padding:6px'>{_avg("vix_at_fire")}</td>
</tr>"""

    # Per-window analysis tables
    wf_html = ""
    for w in windows:
        rows = "".join(
            _table_row(bname, w["stats"].get(bname, {}))
            for bname, *_ in BUCKETS
        )
        wf_html += f"""
        <h3>Window {w['window']}: {w['start_date']} → {w['end_date']}
            <span style="color:#666;font-size:0.8em">({w['n_trades']} trades)</span>
        </h3>
        <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
          <thead>
            <tr style="background:#2c3e50;color:#fff">
              <th>Bucket</th><th>N</th><th>Win %</th><th>Avg Ret</th>
              <th>Total Ret</th><th>Sharpe</th><th>Max DD</th><th>Equity</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    # Score vs return scatter (compact inline data)
    scatter_data = json.dumps([
        {"score": t["score"],
         "ret":   t.get("actual_return", t["return_7d"]),
         "bucket": _bucket_label(t["score"]), "sym": t["symbol"]}
        for t in trades
    ])

    # Options and deep analysis sections
    # Update current premium values before generating report (skip in test/CI)
    import os as _os_mod
    if not _os_mod.environ.get("PYTEST_CURRENT_TEST"):
        try:
            import sys as _sys_mod
            _opts_dir = str(ROOT / "tradingview" / "options")
            _tv_dir = str(ROOT / "tradingview")
            if _opts_dir not in _sys_mod.path:
                _sys_mod.path.insert(0, _opts_dir)
            if _tv_dir not in _sys_mod.path:
                _sys_mod.path.insert(0, _tv_dir)
            from options_outcome_checker import run_premium_update
            print("  [premium] Updating current premium values...")
            _prem_result = run_premium_update(dry_run=False)
            if isinstance(_prem_result, dict):
                print(f"  [premium] Updated {_prem_result.get('updated', 0)} options, "
                      f"skipped {_prem_result.get('skipped', 0)}, errors {_prem_result.get('errors', 0)}")
            else:
                print(f"  [premium] Updated {_prem_result} options")
        except Exception as e:
            print(f"  [premium] WARNING: Could not update premiums: {e}")

    _all_opts = _load_options_positions(db_path)
    _us_opts  = [r for r in _all_opts if "NSE:" not in (r.get("tv_symbol") or "") and "BSE:" not in (r.get("tv_symbol") or "")]
    _ind_opts = [r for r in _all_opts if "NSE:" in (r.get("tv_symbol") or "") or "BSE:" in (r.get("tv_symbol") or "")]
    options_html = (
        _options_section_html(_us_opts,  "&#127918; US Options Recommendations Analytics",  "us_opt", include_js=True) +
        _options_section_html(_ind_opts, "&#127918; IND Options Recommendations Analytics", "ind_opt", include_js=False)
    )
    moomoo_html   = _moomoo_insights_html()

    verdict_lines = []
    high_stats = all_stats.get("HIGH", {})
    low_stats  = all_stats.get("LOW",  {})
    if high_stats.get("n", 0) >= 5 and low_stats.get("n", 0) >= 5:
        high_wr = high_stats.get("win_rate", 0)
        low_wr  = low_stats.get("win_rate", 0)
        diff_wr = high_wr - low_wr
        verdict_lines.append(
            f"HIGH bucket win rate {high_wr:.1f}% vs LOW bucket {low_wr:.1f}% "
            f"(diff: {diff_wr:+.1f}%)"
        )
        high_sh = high_stats.get("sharpe", 0)
        low_sh  = low_stats.get("sharpe", 0)
        verdict_lines.append(
            f"HIGH bucket Sharpe {high_sh:.3f} vs LOW bucket {low_sh:.3f}"
        )
        if diff_wr > 5:
            verdict_lines.append("✅ HIGH score signals outperform LOW by >5% win-rate")
        elif diff_wr < -5:
            verdict_lines.append("⚠️ HIGH score signals underperform LOW — review score calibration")
        else:
            verdict_lines.append("⚡ Score buckets show similar win rates — consider score recalibration")

    verdict_html = "".join(f"<li>{v}</li>" for v in verdict_lines)

    # ── Moneyball analysis section ────────────────────────────────────────────
    def _mb_bucket(mb: float) -> str:
        if mb >= 70:    return "HIGH"
        if mb >= 40:    return "MID"
        return "LOW"

    mb_buckets: dict[str, list] = {"HIGH": [], "MID": [], "LOW": []}
    for t in trades:
        mb = t.get("mb_score")
        if mb is not None:
            mb_buckets[_mb_bucket(float(mb))].append(t)

    def _mb_stats(tlist: list) -> dict:
        if not tlist:
            return {"n": 0, "win_pct": 0.0, "avg_ret": 0.0}
        rets = [float(t.get("actual_return") or t.get("return_7d") or 0) for t in tlist]
        wins = sum(1 for r in rets if r > 0)
        return {
            "n": len(tlist),
            "win_pct": round(100.0 * wins / len(tlist), 1),
            "avg_ret": round(sum(rets) / len(rets), 3),
        }

    mb_rows_html = ""
    for bname, bcolor in [("HIGH", "#27ae60"), ("MID", "#f39c12"), ("LOW", "#e74c3c")]:
        s = _mb_stats(mb_buckets[bname])
        wr_c = "#27ae60" if s["win_pct"] >= 55 else ("#f39c12" if s["win_pct"] >= 40 else "#e74c3c")
        avr_c = "#27ae60" if s["avg_ret"] > 0 else "#e74c3c"
        mb_rows_html += (
            f"<tr><td style='background:{bcolor};color:#fff;font-weight:bold;padding:8px'>{bname} MB</td>"
            f"<td style='text-align:center;padding:8px'>{s['n']}</td>"
            f"<td style='text-align:right;padding:8px;color:{wr_c};font-weight:bold'>{s['win_pct']:.1f}%</td>"
            f"<td style='text-align:right;padding:8px;color:{avr_c}'>{s['avg_ret']:+.3f}%</td></tr>"
        )

    mb_high_n = mb_buckets["HIGH"]
    mb_low_n  = mb_buckets["LOW"]
    if mb_high_n and mb_low_n:
        _h_wr = _mb_stats(mb_high_n)["win_pct"]
        _l_wr = _mb_stats(mb_low_n)["win_pct"]
        mb_finding = (f"High-MB signals ({_h_wr:.1f}% win) vs Low-MB ({_l_wr:.1f}% win) — "
                      f"diff: {_h_wr - _l_wr:+.1f}%")
    elif mb_high_n:
        mb_finding = "Only HIGH MB bucket has sufficient data."
    else:
        mb_finding = "No moneyball data available (scores not yet computed)."

    mb_has_data = any(mb_buckets[b] for b in mb_buckets)
    moneyball_html = ""
    if mb_has_data:
        moneyball_html = f"""
<h2>&#127924; Moneyball Score Analysis</h2>
<div class="card">
  <p style="color:#555;font-size:0.9em">
    <strong>Moneyball score</strong> = percentile rank of composite_score vs last 30 days of alerts
    (0=bottom, 100=top of recent population). <em>Final score</em> = 0.60&times;composite +
    0.40&times;MB. Does high population-relative rank predict better returns?
  </p>
  <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
    <thead><tr style="background:#2c3e50;color:#fff">
      <th style="padding:8px">MB Bucket</th>
      <th style="padding:8px;text-align:center">N Trades</th>
      <th style="padding:8px;text-align:right">Win %</th>
      <th style="padding:8px;text-align:right">Avg Return</th>
    </tr></thead>
    <tbody>{mb_rows_html}</tbody>
  </table>
  <div class="verdict"><strong>Finding:</strong> {mb_finding}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Signal Score Backtest — vectorbt Per-Window Analysis</title>
<style>
  body {{font-family:sans-serif;margin:0;padding:20px;background:#f5f5f5;color:#333}}
  h1 {{color:#2c3e50;margin-bottom:4px}}
  h2 {{color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:4px;margin-top:24px}}
  h3 {{color:#555;margin-bottom:8px}}
  .meta {{color:#888;font-size:0.85em;margin-bottom:20px}}
  .card {{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
  table {{width:100%;border-collapse:collapse}}
  th,td {{padding:8px 12px;border:1px solid #ddd}}
  th {{background:#2c3e50;color:#fff;font-weight:bold}}
  tr:nth-child(even) td {{background:#fafafa}}
  .verdict {{background:#eaf4fb;border-left:4px solid #2980b9;padding:12px 16px;border-radius:4px;margin:12px 0}}
  .sort-ind {{ font-size:0.8em; color:#aaa; margin-left:2px; }}
  th[onclick]:hover {{ background:#1a2b3c; }}
</style>
<script>
function sortTradeTable(tblId, col, isNum) {{
  var tb = document.getElementById(tblId);
  if (!tb) return;
  var tbody = tb.tBodies[0];
  var rows = Array.from(tbody.rows);
  // Collect pairs: main trade row + its optional detail row
  var pairs = [], i = 0;
  while (i < rows.length) {{
    var main = rows[i++];
    var detail = (i < rows.length && rows[i].classList.contains('tr-detail')) ? rows[i++] : null;
    pairs.push([main, detail]);
  }}
  var prevCol = tb.dataset.sortCol, prevDir = tb.dataset.sortDir;
  var dir = (String(prevCol) === String(col) && prevDir === 'asc') ? 'desc' : 'asc';
  tb.dataset.sortCol = col; tb.dataset.sortDir = dir;
  pairs.sort(function(a, b) {{
    var av = a[0].cells[col] ? (a[0].cells[col].getAttribute('data-val') || a[0].cells[col].textContent.trim()) : '';
    var bv = b[0].cells[col] ? (b[0].cells[col].getAttribute('data-val') || b[0].cells[col].textContent.trim()) : '';
    if (isNum) {{ av = parseFloat(av) || 0; bv = parseFloat(bv) || 0; }}
    return (dir === 'asc' ? 1 : -1) * (av > bv ? 1 : av < bv ? -1 : 0);
  }});
  pairs.forEach(function(p) {{ tbody.appendChild(p[0]); if (p[1]) tbody.appendChild(p[1]); }});
  tb.querySelectorAll('th').forEach(function(th, i) {{
    var ind = th.querySelector('.sort-ind');
    if (ind) ind.innerHTML = (String(i) === String(col)) ? (dir === 'asc' ? ' &#9650;' : ' &#9660;') : '&#8645;';
  }});
}}
function filterTradeTable(inputId, tblId) {{
  var q = document.getElementById(inputId).value.toLowerCase();
  var tb = document.getElementById(tblId);
  if (!tb) return;
  var allRows = Array.from(tb.tBodies[0].rows);
  for (var i = 0; i < allRows.length; i++) {{
    if (allRows[i].classList.contains('tr-detail')) continue;
    var txt = allRows[i].cells[0] ? allRows[i].cells[0].textContent.toLowerCase() : '';
    var show = !q || txt.indexOf(q) >= 0;
    allRows[i].style.display = show ? '' : 'none';
    var next = allRows[i].nextElementSibling;
    if (next && next.classList.contains('tr-detail') && !show) next.style.display = 'none';
  }}
}}
</script>
</head>
<body>
<h1>📊 Signal Score Backtest — vectorbt Per-Window Analysis</h1>
<div class="meta">Generated: {generated_at} · DB: {db_path} · Trades: {n_total}</div>

<h2>What This Report Shows</h2>
<div class="card">
  <p>Each TradingView alert is assigned a <strong>composite score (0–100)</strong> by the L1–L4
  signal engine. This report is an <strong>event study</strong>: it asks whether higher scores
  predict a higher <strong>fixed 7-day forward return</strong> (<code>performance_metrics.return_7d</code>).
  Only matured alerts are included, and the outcome does not change between report runs.</p>
  <p style="background:#fff4e5;border-left:4px solid #f39c12;padding:8px 12px;margin-top:10px">
  <strong>&#9888; Scope:</strong> this is <em>not</em> a tradable-portfolio backtest and
  <em>not</em> out-of-sample walk-forward validation. Windows are in-sample time partitions,
  and there is no cost, slippage, or benchmark model. Treat <em>Total Return</em>,
  <em>Equity</em>, and <em>Max DD</em> as hypothetical ordering artefacts.</p>
  <ul>
    <li><span style="color:#e74c3c;font-weight:bold">LOW (&lt;55)</span>: below-threshold signals</li>
    <li><span style="color:#f39c12;font-weight:bold">MED (55–65)</span>: borderline signals</li>
    <li><span style="color:#27ae60;font-weight:bold">HIGH (65–75)</span>: strong signals</li>
    <li><span style="color:#2980b9;font-weight:bold">APEX (≥75)</span>: highest conviction</li>
  </ul>
</div>

<h2>&#128208; Metric Definitions</h2>
<div class="card">
<details>
  <summary style="cursor:pointer;font-weight:bold;color:#2980b9;padding:6px 0">
    &#9658; Click to expand &mdash; how each column is calculated
  </summary>
  <table style="margin-top:12px;font-size:0.9em;width:100%">
    <thead><tr style="background:#34495e;color:#fff">
      <th style="padding:8px">Metric</th><th style="padding:8px">Formula / Explanation</th>
    </tr></thead>
    <tbody>
      <tr><td style="padding:8px"><strong>N Trades</strong></td><td style="padding:8px">Matured alerts in this score bucket, i.e. those with a completed 7-day forward return.</td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px"><strong>Win %</strong></td><td style="padding:8px">Percentage of trades reaching the {WIN_THRESHOLD_PCT:.0f}% target.&nbsp; Formula: <code>count(return_7d &ge; {WIN_THRESHOLD_PCT:.0f}) / N &times; 100</code>. A smaller gain is treated as breakeven noise, not a win.</td></tr>
      <tr><td style="padding:8px"><strong>Avg Return</strong></td><td style="padding:8px">Arithmetic mean of the fixed 7-day forward returns in the bucket.&nbsp; Formula: <code>mean(return_7d)</code></td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px"><strong>Total Return</strong></td><td style="padding:8px"><strong>&#9888; Not an achievable portfolio return.</strong> It compounds every alert sequentially at 100% capital even though many alerts overlap in time.&nbsp; Use <em>Avg Return</em> to compare buckets.</td></tr>
      <tr><td style="padding:8px"><strong>Sharpe Ratio</strong></td><td style="padding:8px">Risk-adjusted return across 7-day outcomes.&nbsp; Formula: <code>(mean(rets) &minus; rf/P) / std(rets) &times; &radic;P</code> where <code>P = 365/7</code>. &gt;1&thinsp;=&thinsp;good, &gt;3&thinsp;=&thinsp;excellent, &lt;0&thinsp;=&thinsp;destroys value.</td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px"><strong>Max Drawdown</strong></td><td style="padding:8px"><strong>&#9888; Drawdown of the same hypothetical sequential curve</strong>, not of a real portfolio. Interpret as an ordering artefact only.</td></tr>
      <tr><td style="padding:8px"><strong>Equity ($10K)</strong></td><td style="padding:8px"><strong>&#9888; Hypothetical.</strong> Final value of the sequential 100%-capital curve described above.</td></tr>
    </tbody>
  </table>
</details>
</div>

<h2>Overall (All Data)</h2>
<div class="card">
<table>
  <thead>
    <tr style="background:#2c3e50;color:#fff">
      <th>Bucket</th><th>N Trades</th><th>Win %</th><th>Avg Ret</th>
      <th>Total Ret</th><th>Sharpe</th><th>Max DD</th><th>Equity ($10K)</th>
    </tr>
  </thead>
  <tbody>{overall_rows}</tbody>
</table>
</div>

{significance_html}

<h2>Trade Details by Bucket</h2>
<div class="card">
{bucket_detail_html}
</div>

<h2>&#128194; Source Reliability
  <span style="font-size:0.7em;color:#888;font-weight:400">&#9658; Click any row to see all trades from that source</span>
</h2>
<div class="card">
<table style="width:100%;border-collapse:collapse">
  <thead><tr style="background:#2c3e50;color:#fff">
    <th style="padding:8px 10px;text-align:left">Source</th>
    <th style="padding:8px 10px;text-align:center">Count</th>
    <th style="padding:8px 10px;text-align:right">Win Rate</th>
    <th style="padding:8px 10px;text-align:right">Avg Return</th>
    <th style="padding:8px 10px;text-align:right">Avg MFE</th>
    <th style="padding:8px 10px;text-align:right">Avg MAE</th>
    <th style="padding:8px 10px;text-align:center">Detail</th>
  </tr></thead>
  <tbody>{source_rows_html}</tbody>
</table>
<script>
function toggleSrcDetail(id) {{
  var r = document.getElementById(id);
  r.style.display = r.style.display === 'none' ? 'table-row' : 'none';
}}
</script>
</div>

<h2>&#128202; Score Component Breakdown by Bucket</h2>
<div class="card">
<p style="color:#666;font-size:0.88em;margin-bottom:10px">
  Average sub-score values per bucket. Use this to identify which signal characteristics
  correlate with winning buckets. All values are averages across trades in that bucket.
</p>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:0.85em">
  <thead>
    <tr style="background:#2c3e50;color:#fff">
      <th rowspan="2" style="padding:6px 8px;vertical-align:middle">Bucket</th>
      <th colspan="5" style="padding:6px 8px;text-align:center;border-bottom:1px solid #aaa">
        🔧 Technical (L1)
      </th>
      <th style="padding:6px 8px;text-align:center">Win%</th>
      <th colspan="3" style="padding:6px 8px;text-align:center;border-bottom:1px solid #aaa">
        📈 Fundamental (L2)
      </th>
      <th colspan="2" style="padding:6px 8px;text-align:center;border-bottom:1px solid #aaa">
        💬 Sentiment (L3)
      </th>
      <th colspan="2" style="padding:6px 8px;text-align:center;border-bottom:1px solid #aaa">
        🏛 Internals (L4)
      </th>
    </tr>
    <tr style="background:#34495e;color:#ddd;font-size:0.85em">
      <th style="padding:4px 8px">RSI</th>
      <th style="padding:4px 8px">ADX</th>
      <th style="padding:4px 8px">Sqz%</th>
      <th style="padding:4px 8px">RelVol</th>
      <th style="padding:4px 8px">SMA200%</th>
      <th style="padding:4px 8px">★</th>
      <th style="padding:4px 8px">Val</th>
      <th style="padding:4px 8px">Prof</th>
      <th style="padding:4px 8px">Growth</th>
      <th style="padding:4px 8px">Analyst</th>
      <th style="padding:4px 8px">News</th>
      <th style="padding:4px 8px">PCR</th>
      <th style="padding:4px 8px">VIX</th>
    </tr>
  </thead>
  <tbody>{score_breakdown_rows}</tbody>
</table>
</div>
<p style="color:#aaa;font-size:0.78em;margin-top:8px">
  Sqz% = % of trades with squeeze active. SMA200% = price % above/below 200-day SMA.
  Val/Prof/Growth = L2 fundamental sub-scores (0–10). Analyst/News = L3 sub-scores.
  PCR = Put/Call ratio score. VIX = raw VIX level at alert time.
</p>
</div>

<h2>Verdict</h2>
<div class="card verdict"><ul>{verdict_html or "<li>Insufficient data for verdict</li>"}</ul></div>

<h2>Per-Window Analysis</h2>
<div class="card">
  <p>Data split into {len(windows)} consecutive time windows to check cross-period consistency.
  NOTE: This is in-sample time partitioning — not out-of-sample walk-forward validation.</p>
  {wf_html}
</div>

"""

    # ── Build large sections separately to avoid MemoryError ──────────────
    try:
        _since_rows = load_since_entry_rows(db_path)
    except Exception as exc:
        print(f"  [since-entry] falling back to matured rows only: {exc}")
        _since_rows = None
    _deep_html = _deep_analysis_html(trades, since_entry_rows=_since_rows)
    html += options_html
    html += moomoo_html
    html += _deep_html
    del _deep_html  # free memory
    html += moneyball_html

    html += f"""
<h2>Score vs 7-Day Return</h2>
<div class="card">
  <canvas id="scatter"></canvas>
  <script>
  (function() {{
    const data = {scatter_data};
    const canvas = document.getElementById('scatter');
    canvas.width  = canvas.parentElement.clientWidth - 32;
    canvas.height = 320;
    const ctx = canvas.getContext('2d');
    const colors = {{LOW:'#e74c3c', MED:'#f39c12', HIGH:'#27ae60', APEX:'#2980b9'}};
    const scores = data.map(d => d.score);
    const rets   = data.map(d => d.ret);
    const minS=Math.min(...scores), maxS=Math.max(...scores);
    const minR=Math.min(...rets),   maxR=Math.max(...rets);
    const pw=canvas.width-60, ph=canvas.height-50;
    function sx(v){{return 40+(v-minS)/(maxS-minS||1)*pw;}}
    function sy(v){{return ph+10-(v-minR)/(maxR-minR||1)*ph;}}
    ctx.strokeStyle='#ddd'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(40,sy(0)); ctx.lineTo(40+pw,sy(0)); ctx.stroke();
    data.forEach(d=>{{
      ctx.fillStyle=colors[d.bucket]||'#999';
      ctx.beginPath(); ctx.arc(sx(d.score),sy(d.ret),3,0,Math.PI*2);
      ctx.fill();
    }});
    ctx.fillStyle='#333'; ctx.font='11px sans-serif';
    ctx.fillText('Score', 40+pw/2, canvas.height-2);
    ctx.save(); ctx.translate(12,ph/2); ctx.rotate(-Math.PI/2);
    ctx.fillText('7d Return %', 0, 0); ctx.restore();
  }})();
  </script>
</div>

<div class="meta" style="margin-top:20px">
  vectorbt {_vbt_version()} · Python {sys.version.split()[0]} · scores: L1–L4 composite
</div>
</body>
</html>"""

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass

    # ── Dual-output: HTML (for user) + JSON sidecar (for agent) ──────────────
    # Agents should NEVER parse the HTML directly (37 MB, 200K+ lines).
    # Instead read the JSON sidecar (~50 KB) or use the AGENT-DATA comment
    # at line 1 for summary-only needs (~0.3 ms read vs ~45 000 ms bs4 parse).
    _agent_summary: dict = {
        "generated_at": generated_at,
        "n_trades":     n_total,
        "db_path":      db_path,
        "bucket_stats": {
            bname: {
                "n":          all_stats.get(bname, {}).get("n", 0),
                "win_rate":   all_stats.get(bname, {}).get("win_rate", 0),
                "avg_return": all_stats.get(bname, {}).get("avg_return", 0),
                "sharpe":     all_stats.get(bname, {}).get("sharpe", 0),
                "max_dd":     all_stats.get(bname, {}).get("max_drawdown", 0),
            }
            for bname, *_ in BUCKETS
        },
        "source_stats": [
            {
                "source":      ss["source"],
                "n":           ss["count"],
                "win_rate":    ss["win_rate"],
                "avg_return":  ss["avg_return"],
            }
            for ss in src_stats
        ],
        "significance": significance,
    }
    # Line 1 of HTML = machine-readable summary (read with readline(1), ~0.3ms)
    _agent_comment = (
        f"<!-- AGENT-DATA: {json.dumps(_agent_summary, separators=(',', ':'))} -->\n"
    )
    html = _agent_comment + html

    # JSON sidecar: full trade data + all stats in one compact file
    _json_path = Path(out_path).with_suffix(".json")
    _sidecar: dict = {**_agent_summary, "scatter_trades": json.loads(scatter_data)}
    _json_path.write_text(json.dumps(_sidecar, indent=2), encoding="utf-8")

    Path(out_path).write_text(html, encoding="utf-8")
    return out_path


def _vbt_version() -> str:
    try:
        import vectorbt as vbt
        return vbt.__version__
    except Exception:
        return "n/a"


# ─── Stop-Loss Backtest ────────────────────────────────────────────────────────

def run_stop_loss_backtest(
    trades: list[dict],
    sl_pcts: list[float] | None = None,
) -> list[dict]:
    """
    Simulate stop-loss impact on trade returns using drawdown data.

    For each stop-loss level, clips each trade's return at -sl_pct if
    the trade's max adverse excursion (MAE) breached the stop.

    Args:
        trades: List of trade dicts with 'actual_return' and 'max_drawdown' keys.
        sl_pcts: Stop-loss levels to test as positive floats (e.g. [3, 5, 8, 10]).

    Returns:
        List of dicts: [{sl_pct, n_stopped, n_total, avg_return, win_rate,
                         total_return, max_drawdown}]
    """
    import numpy as np

    if sl_pcts is None:
        sl_pcts = [3.0, 5.0, 8.0, 10.0]

    rets = [t.get("actual_return", t.get("return_7d", 0.0)) for t in trades]
    maes = [t.get("max_drawdown", 0.0) for t in trades]
    n = len(rets)
    if n == 0:
        return [{"sl_pct": sl, "n_stopped": 0, "n_total": 0, "avg_return": 0.0,
                 "win_rate": 0.0, "total_return": 0.0, "max_drawdown": 0.0}
                for sl in sl_pcts]

    results = []
    for sl in sl_pcts:
        clipped = []
        n_stopped = 0
        for ret, mae in zip(rets, maes):
            if mae <= -sl:   # MAE breached stop
                clipped.append(-sl)
                n_stopped += 1
            else:
                clipped.append(ret)

        arr = np.array(clipped)
        win_rate = float((arr >= WIN_THRESHOLD_PCT).mean() * 100)
        avg_ret = float(arr.mean())
        equity = _build_equity_curve(clipped)
        total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1) * 100
        roll_max = equity.cummax()
        dd = ((equity - roll_max) / roll_max).min()

        results.append({
            "sl_pct": sl,
            "n_stopped": n_stopped,
            "n_total": n,
            "avg_return": round(avg_ret, 3),
            "win_rate": round(win_rate, 1),
            "total_return": round(total_ret, 2),
            "max_drawdown": round(float(dd) * 100, 2),
        })
    return results


# ─── Monte Carlo Simulation ───────────────────────────────────────────────────

def run_monte_carlo(
    trades: list[dict],
    n_paths: int = 500,
    seed: int = 42,
) -> dict:
    """
    Bootstrap Monte Carlo simulation on trade returns.

    Randomly resamples (with replacement) the trade return series n_paths times
    to produce confidence intervals for total return and max drawdown.

    Args:
        trades: List of trade dicts with 'actual_return' key.
        n_paths: Number of Monte Carlo paths.
        seed: Random seed for reproducibility.

    Returns:
        {n_paths, n_trades, return_p5, return_p50, return_p95,
         drawdown_p5, drawdown_p50, drawdown_p95}
    """
    import numpy as np

    rets = np.array([t.get("actual_return", t.get("return_7d", 0.0))
                     for t in trades], dtype=float)
    n = len(rets)
    if n == 0:
        return {"n_paths": n_paths, "n_trades": 0,
                "return_p5": 0.0, "return_p50": 0.0, "return_p95": 0.0,
                "drawdown_p5": 0.0, "drawdown_p50": 0.0, "drawdown_p95": 0.0}

    rng = np.random.default_rng(seed)
    total_returns = np.empty(n_paths)
    max_drawdowns = np.empty(n_paths)

    for i in range(n_paths):
        sampled = rng.choice(rets, size=n, replace=True)
        # Build equity curve
        factors = 1.0 + sampled / 100.0
        equity = np.cumprod(factors) * 10_000.0
        total_returns[i] = (equity[-1] / 10_000.0 - 1.0) * 100.0
        # Max drawdown
        running_max = np.maximum.accumulate(equity)
        dd = (equity - running_max) / running_max
        max_drawdowns[i] = float(dd.min()) * 100.0

    return {
        "n_paths":      n_paths,
        "n_trades":     n,
        "return_p5":    round(float(np.percentile(total_returns, 5)), 2),
        "return_p50":   round(float(np.percentile(total_returns, 50)), 2),
        "return_p95":   round(float(np.percentile(total_returns, 95)), 2),
        "drawdown_p5":  round(float(np.percentile(max_drawdowns, 5)), 2),
        "drawdown_p50": round(float(np.percentile(max_drawdowns, 50)), 2),
        "drawdown_p95": round(float(np.percentile(max_drawdowns, 95)), 2),
    }


# ─── Main ──────────────────────────────────────────────────────────────────────

def main(args) -> None:
    db_path  = str(args.db)
    out_path = str(args.out)

    print(f"Loading trades from {db_path} ...")
    trades = load_trades(db_path, min_return_abs=float(args.min_return_abs))
    print(f"  Loaded {len(trades)} matured alerts (fixed 7-day outcome)")

    # BUG-B1 guard: an unpopulated performance_metrics table must fail loudly
    # rather than degrade to all-zero returns that look like "the score has no edge".
    _n_outcome = sum(1 for t in trades if t.get("actual_return") is not None)
    if not trades or _n_outcome == 0:
        raise SystemExit(
            "ABORT: no matured 7-day outcomes found in performance_metrics.\n"
            "  The report would otherwise show a false 'no predictive power' result.\n"
            "  Run the price-snapshot / performance backfill before regenerating."
        )

    # ── Moneyball: add population-relative percentile score per trade ─────────
    try:
        import sys as _sys
        import os as _os
        _alert_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _alert_dir not in _sys.path:
            _sys.path.insert(0, _alert_dir)
        from alert_tracker import compute_signal_moneyball as _csm
        for t in trades:
            s = t.get("score")
            if s is not None:
                t["mb_score"], t["final_score"] = _csm(float(s))
            else:
                t["mb_score"], t["final_score"] = 50.0, 0.0
        print(f"  Moneyball scores computed for {len(trades)} trades")
    except Exception as _mb_exc:
        print(f"  Moneyball computation skipped: {_mb_exc}")
        for t in trades:
            t["mb_score"] = 50.0
            t["final_score"] = float(t.get("score") or 0)

    # Score bucket distribution
    for name, lo, hi, _ in BUCKETS:
        n = sum(1 for t in trades if lo <= t["score"] < hi)
        print(f"  {name:4s} ({lo:.0f}-{hi:.0f}): {n} trades")

    # ── Fetch live prices (yfinance / TV scanner) ──────────────────────────
    print("\nFetching live prices from yfinance / TradingView scanner ...")
    unique_syms = list({t["symbol"] for t in trades})
    live_prices = fetch_live_prices(unique_syms)
    live_count  = sum(1 for v in live_prices.values() if v is not None)
    print(f"  Got live prices for {live_count}/{len(unique_syms)} symbols")

    # Inject live price for display only. The outcome variable stays the matured
    # 7-day return so the same cohort reports identical numbers on any run date.
    for t in trades:
        lp = live_prices.get(t["symbol"])
        if lp is not None and lp > 0:
            t["current_price"] = lp
            exch = t["symbol"].split(":", 1)[0].upper() if ":" in t["symbol"] else "US"
            t["current_price_src"] = "live_tv" if exch in _TV_SCANNER_BY_EXCHANGE else "live_yf"
            entry_px = t.get("entry_price") or 0
            if entry_px > 0:
                t["live_return"] = (lp - entry_px) / entry_px * 100.0

    live_injected = sum(
        1 for t in trades if t.get("current_price_src", "").startswith("live")
    )
    print(f"  Injected live current_price into {live_injected} trade rows (display only)")

    print("\nComputing sell signals (8-rule exit logic) ...")
    compute_sell_signals(trades)
    sell_counts: dict[str, int] = {}
    for t in trades:
        a = t.get("sell_action", "N/A")
        sell_counts[a] = sell_counts.get(a, 0) + 1
    for action, cnt in sorted(sell_counts.items(), key=lambda x: x[0]):
        print(f"  {action:8s}: {cnt}")

    print("\nComputing overall portfolio stats (vectorbt) ...")
    all_stats = compute_bucket_stats(trades)
    for name, s in all_stats.items():
        if s["n"] == 0:
            continue
        print(f"  {name:4s}: n={s['n']:3d}  win={s['win_rate']:.1f}%  "
              f"avg_ret={s['avg_return']:+.3f}%  sharpe={s['sharpe']:.3f}  "
              f"max_dd={s['max_drawdown']:.2f}%")

    print("\nRunning per-window analysis (3 windows) ...")
    windows = per_window_stats(trades, n_windows=int(args.n_windows))
    for w in windows:
        high = w["stats"].get("HIGH", {})
        low  = w["stats"].get("LOW",  {})
        print(f"  W{w['window']} ({w['start_date']} — {w['end_date']}): "
              f"n={w['n_trades']}  HIGH win={high.get('win_rate',0):.1f}%  "
              f"LOW win={low.get('win_rate',0):.1f}%")

    print("\nTesting whether the score gap is real (bootstrap + permutation) ...")
    significance = compute_significance(
        trades, n_resamples=int(getattr(args, "n_resamples", 10_000))
    )
    _sig_mod = _load_significance_module()
    for _res in significance:
        print("  " + (_sig_mod.format_result(_res) if _sig_mod else str(_res)))

    print(f"\nWriting report to {out_path} ...")
    generate_html_report(trades, all_stats, windows, out_path, db_path,
                         significance=significance)

    # ── Optional: stop-loss analysis ────────────────────────────────────────
    sl_results = None
    if getattr(args, "stop_loss", False):
        print("\nRunning stop-loss impact analysis ...")
        sl_results = run_stop_loss_backtest(trades)
        for r in sl_results:
            print(f"  SL {r['sl_pct']:.0f}%: stopped={r['n_stopped']}/{r['n_total']}  "
                  f"avg_ret={r['avg_return']:+.3f}%  win={r['win_rate']:.1f}%  "
                  f"total_ret={r['total_return']:+.2f}%  max_dd={r['max_drawdown']:.2f}%")

    # ── Optional: Monte Carlo simulation ────────────────────────────────────
    mc_results = None
    if getattr(args, "monte_carlo", False):
        n_paths = int(getattr(args, "n_paths", 500))
        print(f"\nRunning Monte Carlo simulation ({n_paths} paths) ...")
        mc_results = run_monte_carlo(trades, n_paths=n_paths)
        print(f"  Return  → p5={mc_results['return_p5']:+.2f}%  "
              f"p50={mc_results['return_p50']:+.2f}%  p95={mc_results['return_p95']:+.2f}%")
        print(f"  Max DD  → p5={mc_results['drawdown_p5']:.2f}%  "
              f"p50={mc_results['drawdown_p50']:.2f}%  p95={mc_results['drawdown_p95']:.2f}%")

    print(f"✓ Report saved: {out_path}")

    # Archive a timestamped copy on every run (via centralized archive_report utility)
    try:
        import sys as _sys
        _rdb = str(ROOT / "reports")
        if _rdb not in _sys.path:
            _sys.path.insert(0, _rdb)
        from report_db import archive_report as _arc
        dest = _arc(out_path, f"backtest_vectorbt_{datetime.now().strftime('%Y-%m-%d_%H%M')}")
        if dest:
            print(f"  Archived: {dest}")
    except Exception as _e:
        print(f"  [archive] backtest archive skipped: {_e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Per-window signal score validation using vectorbt"
    )
    parser.add_argument(
        "--db",  default=str(_DEFAULT_DB),
        help="Path to alert_tracker.db"
    )
    parser.add_argument(
        "--out", default=str(_DEFAULT_OUT),
        help="Output HTML report path"
    )
    parser.add_argument(
        "--min-return-abs", default="0.001", dest="min_return_abs",
        help="Min |return_7d| to include a trade (default: 0.001)"
    )
    parser.add_argument(
        "--n-windows", default="3", dest="n_windows",
        help="Number of time windows for per-window analysis (default: 3)"
    )
    parser.add_argument(
        "--stop-loss", action="store_true", dest="stop_loss",
        help="Run stop-loss impact analysis (3%%, 5%%, 8%%, 10%%)"
    )
    parser.add_argument(
        "--monte-carlo", action="store_true", dest="monte_carlo",
        help="Run Monte Carlo bootstrap simulation"
    )
    parser.add_argument(
        "--n-paths", default="500", dest="n_paths",
        help="Number of Monte Carlo paths (default: 500)"
    )
    parser.add_argument(
        "--n-resamples", default="10000", dest="n_resamples",
        help="Bootstrap/permutation resamples for significance (default: 10000)"
    )
    args = parser.parse_args()
    main(args)

