"""
Signal Stack — Maximum Win-Rate Alert Ranker
=============================================
4-Layer Confluence Scoring System:

  Layer 1 — Technical Quality    (30%): WIN strategy backtest metrics + indicator state
                                         + Relative Volume from TradingView Screener API
  Layer 2 — Fundamental Quality  (30%): Valuation, Profitability, Growth, Balance Sheet
  Layer 3 — Sentiment & Analysts (20%): News sentiment, Analyst consensus, Earnings beats,
                                         Earnings calendar risk
  Layer 4 — Market Internals     (20%): Options flow (PCR), Insider activity, 52W position

Composite = L1×0.30 + L2×0.30 + L3×0.20 + L4×0.20  (scaled 0–100)

TV Screener (free, no auth, batch per exchange):
  • relative_volume_10d_calc — breakout conviction filter (added to L1)
  • recommendation_mark      — analyst consensus cross-check

News Sources (ranked by signal quality):
  • TradingView Reuters    — PRIMARY wire service, all markets, authenticated
  • TradingView Dow Jones  — Premium newswire, global coverage
  • Quartr                 — IR reports, earnings call summaries, guidance updates
  • MarketWatch            — US financial media (US stocks only)
  • yfinance               — Yahoo Finance fallback (US only, when TV empty)
  • Google News RSS         — final fallback, all markets
  • VADER SentimentIntensityAnalyzer — finance-tuned lexicon
  Soros velocity boost: ≥5 headlines → news weight 25%→30% in L3 (reflexivity signal)

Usage:
  python signal_stack.py              # last 10 unique fires, threshold=75 (default)
  python signal_stack.py --n 20       # last 20 unique fires
  python signal_stack.py --n 10 --out my_report.html
  python signal_stack.py --threshold 62.5  # lower threshold for more results
  python signal_stack.py --no-dedup   # allow duplicate symbols from different timeframes
  python signal_stack.py --min-market-cap 0  # disable market-cap filter
"""

import json, re, argparse, sys, math, time, traceback, shutil, uuid
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html import escape as _html_escape
from pathlib import Path
import os as _os
from jinja2 import Environment, FileSystemLoader

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_SS_DIR = Path(__file__).parent
_OPTIONS_SNAPSHOT_PATH = Path(
    _os.environ.get("SIGNAL_OPTIONS_SNAPSHOT")
    or (_SS_DIR.parent / "reports" / "options_signals.json")
)
# Run-scoped stock payload consumed by the scheduler's current-run handoff.
# Unset (the default) keeps the historical behaviour of not writing it.
_STOCK_PAYLOAD_PATH = (
    Path(_os.environ["SIGNAL_STOCK_PAYLOAD"])
    if _os.environ.get("SIGNAL_STOCK_PAYLOAD")
    else None
)

from options.options_snapshot import (  # noqa: E402
    build_snapshot as _build_options_snapshot,
    deduplicate_contracts as _deduplicate_options_contracts,
    derive_snapshot_status as _derive_options_snapshot_status,
    normalize_badge_data as _normalize_badge_data,
    publish_snapshot as _publish_options_snapshot_file,
)

# ─── Jinja2 template environment ──────────────────────────────────────────────
_TEMPLATE_DIR = _SS_DIR.resolve().parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)

try:
    from path_config import ensure_paths as _ensure_paths
except ImportError:
    import sys as _sys_pc
    _sys_pc.path.insert(0, str(_SS_DIR.parent))
    from path_config import ensure_paths as _ensure_paths
_ensure_paths(_SS_DIR, _SS_DIR.resolve().parent)

try:
    from log_config import get_logger as _get_logger
except ImportError:
    import logging as _logging_fallback
    _get_logger = lambda n: _logging_fallback.getLogger(n)
_log = _get_logger("signal_stack")

# ─── TradingView News (Reuters / Dow Jones / Quartr / story snippets) ──────────
try:
    from tv_data.tv_news import (
        fetch_symbol_news      as _tv_fetch_symbol_news,
        fetch_headlines        as _tv_fetch_headlines,
        fetch_story_snippets   as _tv_fetch_story_snippets,
        fetch_symbol_news_items as _tv_fetch_symbol_news_items,
        article_velocity       as _tv_article_velocity,
    )
    _TV_NEWS_OK = True
except Exception as _tv_news_err:
    _TV_NEWS_OK = False
    _log.warning("tv_news_unavailable", err=str(_tv_news_err),
                 impact="TV news disabled; falling back to yfinance/Google RSS "
                        "for L3 sentiment")
    def _tv_fetch_symbol_news(symbol, **kw):    # type: ignore
        return []
    def _tv_fetch_headlines(symbol=None, **kw):  # type: ignore
        return []
    def _tv_fetch_story_snippets(items, **kw):   # type: ignore
        return []
    def _tv_fetch_symbol_news_items(symbol, **kw):  # type: ignore
        return []
    def _tv_article_velocity(items, **kw):       # type: ignore
        return 0

# ─── TradingView Earnings Scanner (all markets) ────────────────────────────────
try:
    from tv_data.tv_earnings import fetch_tv_earnings_date as _tv_fetch_earnings_date
    _TV_EARNINGS_OK = True
except Exception as _tv_earn_err:
    _TV_EARNINGS_OK = False
    _log.warning("tv_earnings_unavailable", err=str(_tv_earn_err),
                 impact="earnings dates will be None for all symbols")
    def _tv_fetch_earnings_date(symbol, **kw):  # type: ignore
        return None, None

# ─── Dependencies ───────────────────────────────────────────────────────────────
try:
    import yfinance as yf
except ImportError:
    sys.exit("ERROR: pip install yfinance")

try:
    from regime_detector import (
        classify_regime as _classify_market_regime,
        get_regime_weights as _get_regime_weights,
    )
    _REGIME_DETECTOR_OK = True
except ImportError:
    _REGIME_DETECTOR_OK = False
    def _get_regime_weights(vix_level):  # type: ignore
        return None
    def _classify_market_regime(vix_level):  # type: ignore
        return "NORMAL"

try:
    from sentiment import get_scorer as _get_sentiment_scorer
    _SENTIMENT_OK = True
except ImportError as _sentiment_err:
    _SENTIMENT_OK = False
    _log.warning("sentiment_unavailable", err=str(_sentiment_err),
                 impact="news sentiment will be neutral")
    def _get_sentiment_scorer(backend: str = "vader"):  # type: ignore
        return _neutral_sentiment


_SENTIMENT_BACKEND = "vader"   # default: VADER (fast). Use --sentiment finbert for FinBERT.
_SENTIMENT_SCORER = None
_VADER_SCORER = None
_FINBERT_SCORER = None          # cached FinBERT scorer for dual-score mode
_USE_DUAL_SCORES = False        # True when --sentiment both


def _neutral_sentiment(text: str) -> float:
    return 0.0


def _load_sentiment_scorer(backend: str):
    try:
        return _get_sentiment_scorer(backend)
    except Exception as exc:
        if backend != "vader":
            _log.warning("sentiment_backend_unavailable", backend=backend, err=str(exc),
                         fallback="vader")
            try:
                return _get_sentiment_scorer("vader")
            except Exception as fallback_exc:
                _log.warning("sentiment_backend_unavailable", backend="vader",
                             err=str(fallback_exc), fallback="neutral")
        else:
            _log.warning("sentiment_backend_unavailable", backend=backend, err=str(exc),
                         fallback="neutral")
        return _neutral_sentiment


def _primary_sentiment_scorer():
    global _SENTIMENT_SCORER
    if _SENTIMENT_SCORER is None:
        _SENTIMENT_SCORER = _load_sentiment_scorer(_SENTIMENT_BACKEND)
    return _SENTIMENT_SCORER


def _vader_sentiment_scorer():
    global _VADER_SCORER
    if _VADER_SCORER is None:
        _VADER_SCORER = _load_sentiment_scorer("vader")
    return _VADER_SCORER


def _finbert_sentiment_scorer():
    global _FINBERT_SCORER
    if _FINBERT_SCORER is None:
        _FINBERT_SCORER = _load_sentiment_scorer("finbert")
    return _FINBERT_SCORER


def _set_sentiment_backend(backend: str, *, dual_scores: bool) -> None:
    global _SENTIMENT_BACKEND, _SENTIMENT_SCORER, _USE_DUAL_SCORES
    _SENTIMENT_BACKEND = backend
    _SENTIMENT_SCORER = None
    _USE_DUAL_SCORES = dual_scores


def vader_compound(text: str) -> float:
    return _primary_sentiment_scorer()(text)

# ─── Alert Tracker (optional — records processed alerts to SQLite) ─────────────
try:
    from alert_tracker import get_tracker as _get_tracker
    _TRACKER_OK = True
except ImportError:
    _TRACKER_OK = False
    _log.warning("alert_tracker_unavailable",
                 impact="processed signals will NOT be recorded to the alert database")
    def _get_tracker():  # type: ignore
        return None

# ─── TV Screener batch fetch (relative volume + analyst) ───────────────────────
_TV_SCANNER = {
    "NYSE": "https://scanner.tradingview.com/america/scan",
    "NASDAQ": "https://scanner.tradingview.com/america/scan",
    "AMEX": "https://scanner.tradingview.com/america/scan",
    "BATS": "https://scanner.tradingview.com/america/scan",
    "CBOE": "https://scanner.tradingview.com/america/scan",
    "ASX": "https://scanner.tradingview.com/australia/scan",
    "ASX_DLY": "https://scanner.tradingview.com/australia/scan",
    "NSE": "https://scanner.tradingview.com/india/scan",
    "BSE": "https://scanner.tradingview.com/india/scan",
    "HKEX": "https://scanner.tradingview.com/hongkong/scan",
    "SGX": "https://scanner.tradingview.com/singapore/scan",
}

def fetch_tv_screener(pro_symbols: list) -> dict:
    """
    Batch-fetch relative_volume + recommendation_mark for a list of TV pro_symbols.
    Groups symbols by exchange scanner URL, sends one request per exchange.
    Returns: { 'NYSE:RCL': {'rel_vol': 2.3, 'rec_mark': 1.5}, ... }
    """
    # Group by scanner URL
    by_url = {}
    sym_to_scanner = {}  # exact scanner symbol (EXCHANGE:TICKER) → original pro_symbol
    sym_to_ticker = {}   # (scanner URL, bare ticker) → original pro_symbol
    for ps in pro_symbols:
        if ":" not in ps:
            continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER.get(exch.upper())
        if not url:
            continue
        by_url.setdefault(url, [])
        by_url[url].append(ticker)
        sym_to_scanner[f"{exch.upper()}:{ticker}"] = ps
        sym_to_ticker[(url, ticker)] = ps

    results = {}
    # SMA200, close, volume_change — free L1 alpha signals (no extra API call)
    fields = ["name", "relative_volume_10d_calc", "recommendation_mark",
              "SMA200", "close", "ADX", "RSI", "MACD.macd", "MACD.signal",
              "average_volume_10d_calc", "Volatility.D", "beta_1_year",
              "BB.upper", "BB.lower"]

    for url, tickers in by_url.items():
        try:
            payload = {
                "columns": fields,
                "filter": [{"left": "name", "operation": "in_range", "right": tickers}],
                "range": [0, len(tickers) + 5],
            }
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for item in data.get("data", []):
                raw_sym = item.get("s", "")         # e.g. "NYSE:RCL"
                d = item.get("d", [])
                ticker_only = raw_sym.split(":")[-1] if ":" in raw_sym else raw_sym
                # Match back to the original request without cross-exchange collisions.
                ps = (sym_to_scanner.get(raw_sym)
                      or sym_to_ticker.get((url, ticker_only))
                      or raw_sym)
                # Compute SMA200 distance: (close - SMA200) / SMA200 * 100
                sma200 = d[3] if len(d) > 3 and d[3] is not None else None
                close  = d[4] if len(d) > 4 and d[4] is not None else None
                sma200_pct = None
                if sma200 and close and sma200 > 0:
                    sma200_pct = round((close - sma200) / sma200 * 100, 2)
                # MACD histogram (macd - signal) — momentum confirmation
                macd_val = d[7] if len(d) > 7 and d[7] is not None else None
                macd_sig = d[8] if len(d) > 8 and d[8] is not None else None
                macd_hist = None
                if macd_val is not None and macd_sig is not None:
                    macd_hist = round(macd_val - macd_sig, 4)
                # BB width % = (upper - lower) / close × 100 (volatility compression proxy)
                bb_upper = d[12] if len(d) > 12 and d[12] is not None else None
                bb_lower = d[13] if len(d) > 13 and d[13] is not None else None
                bb_width_pct = None
                if bb_upper is not None and bb_lower is not None and close and close > 0:
                    bb_width_pct = round((bb_upper - bb_lower) / close * 100, 2)
                results[ps] = {
                    "rel_vol":    d[1] if len(d) > 1 and d[1] is not None else None,
                    "rec_mark":   d[2] if len(d) > 2 and d[2] is not None else None,
                    "sma200_pct": sma200_pct,
                    "close":      close,
                    "tv_adx":     d[5] if len(d) > 5 and d[5] is not None else None,
                    "tv_rsi":     d[6] if len(d) > 6 and d[6] is not None else None,
                    "macd_hist":  macd_hist,
                    "avg_vol":    d[9] if len(d) > 9 and d[9] is not None else None,
                    "volatility": d[10] if len(d) > 10 and d[10] is not None else None,
                    "beta":       d[11] if len(d) > 11 and d[11] is not None else None,
                    "bb_width":   bb_width_pct,
                }
        except Exception as e:
            _log.debug("tv_screener_fetch_failed", error=str(e))  # non-fatal; scores fall back to neutral

    # ── Moomoo snapshot fallback ──────────────────────────────────────────────
    # For any symbol that got no TV data (TV blocked/401/empty), attempt a
    # Moomoo get_market_snapshot() fallback for US/HK/SH/SZ symbols.
    # Provides: close, rel_vol, avg_vol, volatility + Moomoo-only extras.
    missing = [ps for ps in pro_symbols if ps not in results]
    if missing and _MOOMOO_AVAILABLE:
        try:
            from moomoo_enricher import fetch_screener_snapshot
            import concurrent.futures as _cf_fb
            _fb_ex = _cf_fb.ThreadPoolExecutor(max_workers=1)
            _fb_fut = _fb_ex.submit(fetch_screener_snapshot, missing)
            try:
                fallback = _fb_fut.result(timeout=15)
            except _cf_fb.TimeoutError:
                _log.debug("moomoo_screener_fallback_timeout", timeout=15)
                fallback = {}
            finally:
                _fb_ex.shutdown(wait=False)
            results.update(fallback)
        except Exception as exc:
            _log.debug("moomoo_screener_fallback_failed", error=str(exc))  # non-fatal

    return results


def _get_options_badge_context(item: dict) -> tuple[str, object]:
    """Return the symbol and spot price shape expected by options_badge."""
    fundamentals = item.get("fundamentals") or {}
    tv_symbol = item.get("tv_symbol") or item.get("pro_symbol") or item.get("symbol", "")
    spot_price = item.get("close") or item.get("price") or fundamentals.get("current_price")
    return tv_symbol, spot_price


_OPTIONS_BADGE_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "BATS", "CBOE", "NSE", "BSE"})


def _select_options_badge_candidates(
    ranked: list[dict],
    top_k: int,
    now_utc: datetime | None = None,
    open_markets_only: bool = False,
) -> list[dict]:
    """Return highest-ranked actionable symbols with supported option markets."""
    if top_k <= 0:
        return []
    market_is_open = None
    if open_markets_only:
        from options.run_options import _market_is_open as market_is_open
    selected = []
    for item in ranked:
        symbol = item.get("tv_symbol", "")
        exchange = symbol.split(":", 1)[0].upper() if ":" in symbol else ""
        market_open = not open_markets_only or bool(market_is_open(exchange, now_utc))
        if item.get("actionable") and exchange in _OPTIONS_BADGE_EXCHANGES and market_open:
            selected.append(item)
            if len(selected) >= top_k:
                break
    return selected


def _publish_options_snapshot(
    run_id: str,
    status: str,
    ranked: list[dict] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> None:
    """Publish the current run's normalized badge contracts atomically."""
    contracts: list[dict] = []
    collected_errors = list(errors or [])
    for item in ranked or []:
        if not item.get("_options_badge_candidate"):
            continue
        badge_data = item.get("options_data")
        if badge_data is None:
            collected_errors.append({
                "symbol": str(item.get("tv_symbol") or ""),
                "message": "options badge result missing",
            })
            continue
        normalized, item_errors = _normalize_badge_data(
            str(item.get("tv_symbol") or ""),
            item.get("composite"),
            badge_data,
        )
        contracts.extend(normalized)
        collected_errors.extend(item_errors)
    snapshot = _build_options_snapshot(
        run_id,
        _derive_options_snapshot_status(status, contracts, collected_errors),
        _deduplicate_options_contracts(contracts),
        collected_errors,
    )
    _publish_options_snapshot_file(_OPTIONS_SNAPSHOT_PATH, snapshot)


def _write_empty_stock_payload(run_id: str, *, status: str = "no_new",
                               errors: list[str] | None = None,
                               freshness_rejections: dict | None = None) -> None:
    """Publish an explicit no-new stock payload for the current run.

    A no-new cycle must be distinguishable from "stock data unavailable", so
    the scheduler can send one concise no-new summary instead of replaying
    older alerts.

    ``run_id`` is passed explicitly: this helper is module-level, so it cannot
    see the ``_analysis_run_id`` local of ``main()``.
    """
    if _STOCK_PAYLOAD_PATH is None:
        return
    if not run_id:
        print("  ⚠️  no run id available — stock payload not written", flush=True)
        return
    try:
        from tradingview.scheduler.stock_results import (
            build_current_run_payload as _build_payload,
            write_current_run_payload as _write_payload,
        )

        _write_payload(
            _STOCK_PAYLOAD_PATH,
            {**_build_payload(run_id, [], status=status),
             **({"errors": errors} if errors else {}),
             **({"freshness_rejections": dict(freshness_rejections)} if freshness_rejections else {})},
        )
    except Exception as _spe:
        print(f"  ⚠️  could not write run-scoped stock payload: {_spe}", flush=True)


# ─── Optional Moomoo L5 enrichment ────────────────────────────────────────────
try:
    from moomoo_enricher import enrich_signals, tv_to_moomoo, l5_html_badge
    _MOOMOO_AVAILABLE = True
except ImportError:
    _MOOMOO_AVAILABLE = False
    def l5_html_badge(e): return ''

# ─── Optional India L5 enrichment (NSE/BSE portfolio context) ─────────────────
try:
    import sys as _sys_india
    _sys_india.path.insert(0, str(Path(__file__).parent.parent / "india-brokers"))
    from india_enricher import IndiaEnricher as _IndiaEnricher
    _INDIA_AVAILABLE = True
    # W1: Lazy init — do NOT instantiate at import time (triggers live broker auth)
    _INDIA_ENRICHER_INSTANCE: "_IndiaEnricher | None" = None

    def _get_india_enricher() -> "_IndiaEnricher":
        """Return the shared IndiaEnricher, lazily constructed on first call."""
        global _INDIA_ENRICHER_INSTANCE
        if _INDIA_ENRICHER_INSTANCE is None:
            _INDIA_ENRICHER_INSTANCE = _IndiaEnricher()
        return _INDIA_ENRICHER_INSTANCE

    def india_l5_html_badge(e): return _get_india_enricher().html_badge(e) if e else ''
except ImportError:
    _INDIA_AVAILABLE = False
    _INDIA_ENRICHER_INSTANCE = None

    def _get_india_enricher(): return None  # type: ignore[return-value]
    def india_l5_html_badge(e): return ''

# ─── Optional IBKR L6 enrichment ─────────────────────────────────────────────
try:
    from ibkr_enricher import compute_l6_score, l6_html_badge
    _IBKR_AVAILABLE = True
except ImportError:
    _IBKR_AVAILABLE = False
    def l6_html_badge(e): return ''

# ─── Optional backtest position sizer ────────────────────────────────────────
try:
    _ensure_paths(Path(__file__).parent)
    from backtest.backtest_sizer import get_position_size as _get_position_size
    _SIZER_AVAILABLE = True
except Exception:
    _SIZER_AVAILABLE = False
    def _get_position_size(*a, **kw):
        return None

# ─── Optional L7 external signal enrichers ────────────────────────────────────
try:
    from external_signal_enricher import compute_l7_score, l7_html_badge
    _EXTERNAL_AVAILABLE = True
except ImportError:
    _EXTERNAL_AVAILABLE = False
    def compute_l7_score(tv_symbol, yf_ticker=""):
        return {}
    def l7_html_badge(e):
        return ''

# ─── Optional SEC EDGAR insider activity enricher (opt-in via --edgar) ────────
try:
    from edgar_enricher import enrich_insider_activity as _edgar_enrich
    _EDGAR_OK = True
except ImportError:
    _EDGAR_OK = False
    def _edgar_enrich(symbol, **kw):  # type: ignore[misc]
        return None

# ─── Optional Buy Vehicle Advisor (L8) ───────────────────────────────────────
try:
    from buy_advisor import (
        build_buy_advice    as _build_buy_advice,
        _buy_advisor_html,
        fetch_all_account_cash as _fetch_all_account_cash,
        build_account_cash_html as _build_account_cash_html,
        get_forex_snapshot  as _get_forex_snapshot,
        build_forex_html    as _build_forex_html,
        BUY_ADVISOR_CSS,
        FX_PANEL_CSS,
        _EXCHANGE_ACCOUNTS  as _TRADABLE_EXCHS,
    )
    _ADVISOR_AVAILABLE = True
except ImportError:
    _ADVISOR_AVAILABLE = False
    _log.warning("buy_advisor_unavailable",
                 impact="buy vehicle routing, cash summary, and forex data will be absent")
    _TRADABLE_EXCHS: dict = {}
    def _build_buy_advice(item, cash=None): return {}       # type: ignore[misc]
    def _buy_advisor_html(advice): return ''                # type: ignore[misc]
    def _fetch_all_account_cash(): return {}                # type: ignore[misc]
    def _build_account_cash_html(cash): return ''           # type: ignore[misc]
    def _get_forex_snapshot(): return []                    # type: ignore[misc]
    def _build_forex_html(forex): return ''                 # type: ignore[misc]
    BUY_ADVISOR_CSS = ''                                    # type: ignore[assignment]
    FX_PANEL_CSS    = ''                                    # type: ignore[assignment]

# ─── Optional Options Badge (L8b) ────────────────────────────────────────────
try:
    from options.options_badge import (
        compute_options_badge_data as _compute_options_badge_data,
        _options_html_badge,
        OPTIONS_BADGE_CSS,
    )
    _OPTIONS_AVAILABLE = True
except ImportError:
    _OPTIONS_AVAILABLE = False
    def _compute_options_badge_data(tv_symbol, spot_price=None): return {}   # type: ignore[misc]
    def _options_html_badge(data): return ''                                 # type: ignore[misc]
    OPTIONS_BADGE_CSS = ''                                                   # type: ignore[assignment]

# ─── Optional AI Commentary (L9) ────────────────────────────────────────────────
try:
    from ai_commentary import (
        AI_COMMENTARY_CSS,
        _AI_AVAILABLE,
        _AI_MODEL,
        build_ai_commentary,
        _ai_commentary_html,
    )
except ImportError:
    _AI_AVAILABLE      = False  # type: ignore[assignment]
    _AI_MODEL          = ""     # type: ignore[assignment]
    AI_COMMENTARY_CSS  = ""     # type: ignore[assignment]
    def build_ai_commentary(*a, **kw): pass  # type: ignore[misc]
    def _ai_commentary_html(ai): return ""   # type: ignore[misc]


def _book_features_html_badge(bf: "dict | None") -> str:
    """Render book-feature context without implying it changes the rank."""
    if not bf or not bf.get("total_bonus"):
        return ""
    rows = ""
    for k, v in bf.items():
        if k == "total_bonus":
            continue
        if isinstance(v, dict):
            score = v.get("score", 0)
            label = v.get("label", k.upper())
            if score:
                sc = "#22c55e" if score > 0 else "#ef4444"
                rows += (f'<div class="kv"><span>{label}</span>'
                         f'<b style="color:{sc}">+{score:.1f}</b></div>')
    bonus = bf.get("total_bonus", 0)
    bc = "#22c55e" if bonus > 0 else "#94a3b8"
    return (f'<div class="enrichment-panel" style="border-left:3px solid {bc}">'
            f'<div class="col-head">\U0001f4da Book Context ({bonus:+.1f}, not scored)</div>'
            f'{rows}</div>')


# ─── HTML report helpers (extracted to signal_report_html.py) ────────────────────
from signal_report_html import (
    grade, bar, hcap, fmt, colour_cell,
    _backtest_block_html,
    _build_signal_summary_panel,
    _build_coverage_warning_html,
)

# ─── Config ─────────────────────────────────────────────────────────────────────
FIRES_FILE  = Path(__file__).parent / "fires_raw.json"
OUTPUT_HTML = Path(__file__).parent.parent / "reports" / "signal_stack_report.html"

EXCHANGE_MAP = {
    "ASX": ".AX", "ASX_DLY": ".AX",
    "NSE": ".NS", "BSE": ".BO",
    "SGX": ".SI", "HKEX": ".HK",
    "TYO": ".T", "TSE": ".T",
    "KRX": ".KS", "KOSDAQ": ".KQ",
    "NYSE": "", "NASDAQ": "", "AMEX": "", "BATS": "",
}

# ─── Untradeable exchange prefixes ──────────────────────────────────────────────
# Tradable exchanges: US (NYSE/NASDAQ/AMEX/BATS/CBOE), Australia (ASX),
# Singapore (SGX), Hong Kong (HKEX/HK), India (NSE/BSE), Japan (TYO/TSE),
# Korea (KRX/KOSDAQ).  Everything else is filtered out before scoring.
_UNTRADEABLE_EXCHANGES = frozenset({
    # Crypto spot / derivatives
    "BINANCE", "COINBASE", "KRAKEN", "KUCOIN", "BYBIT", "BITFINEX", "BITGET",
    "BITMEX", "BITSTAMP", "GEMINI", "HUOBI", "GATEIO", "MEXC", "OKEX", "OKX",
    "CRYPTOCAP",        # TV crypto market-cap time-series (BTC.D etc.)
    # Forex / CFD
    "FOREXCOM", "OANDA", "FX_IDC", "FXOPEN", "FX", "FX_SPOT", "ICMARKETS",
    # Broad-market indices (can't trade directly)
    "TVC", "INDEX", "SP",
    # OTC / Pink sheets — penny stocks, delisted companies
    "OTC", "OTCMKTS", "OTCBB", "PINK",
    # Futures exchanges — cannot trade equities here
    "CME", "CBOT", "NYMEX", "COMEX", "ICE", "CBOTMINI",
    # European/other exchanges not covered by user's brokers
    "EURONEXT", "XETR", "XETRA", "BME", "SIX", "LSE",
    # Non-tradable regional exchanges
    "BMFBOVESPA", "B3", "TASE", "MYX", "MCX",
})

# ─── Symbol Helpers ─────────────────────────────────────────────────────────────
def tv_to_yf(pro_symbol: str) -> str:
    if ":" not in pro_symbol:
        return pro_symbol
    exchange, ticker = pro_symbol.split(":", 1)
    ticker = ticker.replace("-", ".").strip()
    suffix = EXCHANGE_MAP.get(exchange.upper(), "")
    if exchange.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix

# ─── WIN alert selection policy ─────────────────────────────────────────────────
# The default downloaded-WIN mode must score only the configured WIN alerts and
# only actual BUY events. Before this policy existed the selection loop consumed
# every fire in fires_raw.json's "all" array and filtered only duplicate symbols,
# so an unrelated alert or a SELL event could enter the long pipeline and receive
# ATM call badges and buy advice (review 2026-10-13, Issue 3).
#
# The IDs come from the shared tv_data/win_alerts.py module so the downloader and
# the scorer cannot drift apart.
try:
    from tv_data.win_alerts import WIN_ALERT_IDS as _WIN_ALERT_IDS
except ImportError:  # pragma: no cover - path fallback for direct execution
    import sys as _sys_wa
    _sys_wa.path.insert(0, str(_SS_DIR))
    from tv_data.win_alerts import WIN_ALERT_IDS as _WIN_ALERT_IDS

WIN_ALERT_IDS = _WIN_ALERT_IDS


def _fire_direction(message: str) -> str:
    """Return 'BUY', 'SELL', or 'UNKNOWN' from a fire message.

    Direction is the alert's message direction, which is distinct from the
    instantaneous ``BUY: false`` strategy flag: a message beginning with BUY can
    legitimately contain ``BUY: false`` and must not be rejected by default.
    Leading whitespace and case are normalized.
    """
    text = (message or "").lstrip()
    upper = text.upper()
    if upper.startswith("BUY"):
        return "BUY"
    if upper.startswith("SELL"):
        return "SELL"
    return "UNKNOWN"


def _is_eligible_win_fire(fire: dict, strict_buy_flag: bool = False) -> bool:
    """Return True when *fire* is a WIN alert BUY event eligible for scoring.

    Eligibility requires a configured WIN alert ID and an actual BUY message
    direction. Missing or unrecognized direction is never treated as BUY.
    ``strict_buy_flag`` optionally also rejects events whose instantaneous
    ``BUY: false`` flag is set (off by default, matching --require-buy-flag).
    """
    if fire.get("alert_id") not in WIN_ALERT_IDS:
        return False
    if _fire_direction(fire.get("message", "")) != "BUY":
        return False
    if strict_buy_flag:
        meta = parse_fire_meta(fire.get("message", ""))
        if str(meta.get("BUY", "")).strip().lower() == "false":
            return False
    return True


def _select_default_win_fires(all_fires: list, n: int, no_dedup: bool = False,
                              strict_buy_flag: bool = False) -> list:
    """Select eligible WIN BUY fires, deduplicating symbols, capped at *n*.

    Eligibility is applied BEFORE symbol deduplication and the --n limit, so an
    excluded event never consumes a slot. Direction is preserved on each
    selected fire under ``_direction`` for auditing.
    """
    selected: list = []
    seen: set = set()
    for fire in all_fires:
        if not _is_eligible_win_fire(fire, strict_buy_flag=strict_buy_flag):
            continue
        sym = fire.get("pro_symbol") or fire.get("symbol")
        if not no_dedup and sym in seen:
            continue
        seen.add(sym)
        selected.append({**fire, "_direction": "BUY"})
        if len(selected) >= n:
            break
    return selected

_ETF_INDEX_SUFFIXES = ("BEES", "NIFTYBEES", "BANKBEES", "HNGSNGBEES", "SETFNN50",
                       "JUNIORBEES", "GOLDBEES", "SILVERBEES", "LIQUIDBEES")

def resolve_etf_underlying(tv_sym: str) -> tuple[str | None, str | None]:
    """
    Detect whether *tv_sym* is an ETF and return its underlying context.

    Returns
    -------
    ("INDEX", "")           – index-tracking ETF; analyse the ETF symbol itself
    (base_yf, base_tv)      – leveraged ETF; use the underlying for scoring
    (None, None)            – ordinary stock; analyse as-is
    """
    ticker = tv_sym.split(":", 1)[-1].upper()
    if ticker in _ETF_INDEX_SUFFIXES or ticker.endswith("BEES"):
        return ("INDEX", "")
    # Single-stock leveraged ETFs → resolve to underlying
    from tradingview.sell.sell_actionable import (
        INDEX_FUND_TICKERS, LEVERAGED_ETF_UNDERLYING, UNDERLYING_TO_TV,
    )
    if ticker in INDEX_FUND_TICKERS or ticker.lstrip("0") in INDEX_FUND_TICKERS:
        return ("INDEX", "")
    underlying_yf = LEVERAGED_ETF_UNDERLYING.get(ticker)
    if underlying_yf:
        underlying_tv = UNDERLYING_TO_TV.get(underlying_yf) or f"NASDAQ:{underlying_yf}"
        return (underlying_yf, underlying_tv)
    return (None, None)


def parse_fire_meta(msg: str) -> dict:
    result = {}
    for key in ["WinRate", "NetProfit%", "rsi", "sqzOff", "ma_up", "ADX",
                "BUY", "LongTrade", "srBuySignal", "prev_change_percent"]:
        m = re.search(rf"{re.escape(key)}:\s*([^,\n]+)", msg)
        if m:
            val = m.group(1).strip().rstrip("%")
            try:
                result[key] = float(val)
            except ValueError:
                result[key] = val
    # +DI and -DI (DMI directional indicators)
    for di_key, di_pattern in [("+DI", r"\+DI:\s*([\d.]+)"), ("-DI", r"-DI:\s*([\d.]+)")]:
        m_di = re.search(di_pattern, msg)
        if m_di:
            try:
                result[di_key] = float(m_di.group(1))
            except ValueError:
                pass
    m = re.search(r"price:\s*([\d.]+)", msg)
    if m:
        result["trigger_price"] = float(m.group(1))
    return result


def _parse_fire_time_utc(fire_time: str) -> "datetime | None":
    """Best-effort parse for TradingView fire_time (usually ISO8601 Z)."""
    if not fire_time:
        return None
    raw = str(fire_time).strip()
    if not raw:
        return None
    # Most common format: 2026-04-10T15:00:01Z
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception as exc:
        _log.debug("fire_time_iso_parse_failed", raw=raw, error=str(exc))
    # Fallback for legacy/non-ISO formats
    for fmt_str in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt_str).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _weekday_elapsed_hours(start: datetime, end: datetime) -> float:
    """Return elapsed UTC hours excluding Saturday and Sunday."""
    if end <= start:
        return 0.0
    cursor = start
    active_seconds = 0.0
    while cursor.date() <= end.date():
        next_day = datetime.combine(
            cursor.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        segment_end = min(next_day, end)
        if cursor.weekday() < 5:
            active_seconds += max(0.0, (segment_end - cursor).total_seconds())
        cursor = next_day
    return active_seconds / 3600.0


def _weekday_active_age_hours(
    fire_time: str,
    now_utc: datetime | None = None,
) -> float | None:
    """Return the alert's age in weekday-active hours, or None if unparseable.

    Weekends are excluded so a Friday-evening fire is not treated as two days
    old on Monday morning. This is reported as context; it does not scale the
    score. L1-L7 are recomputed against live data on every run, so the analysis
    is current even when the trigger is not.
    """
    dt = _parse_fire_time_utc(fire_time)
    if not dt:
        return None
    now = now_utc or datetime.now(timezone.utc)
    return round(_weekday_elapsed_hours(dt, now), 2)


def _calendar_age_hours(fire_time: str, now_utc: datetime | None = None) -> float | None:
    dt = _parse_fire_time_utc(fire_time)
    if not dt:
        return None
    now = now_utc or datetime.now(timezone.utc)
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 2)


def _timestamp_age_hours(value: str, now_utc: datetime | None = None) -> float | None:
    dt = _parse_fire_time_utc(value)
    if not dt:
        return None
    now = now_utc or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _is_pool_fresh(generated_at: str, max_age_hours: float = 72.0,
                   now_utc: datetime | None = None) -> bool:
    age = _timestamp_age_hours(generated_at, now_utc)
    return age is not None and age <= max_age_hours

# ─── NEWS / SENTIMENT ───────────────────────────────────────────────────────────
def fetch_yf_news(ticker_obj) -> list[str]:
    """Return list of headline strings from yfinance (new v0.2.x format)."""
    headlines = []
    try:
        for item in (ticker_obj.news or []):
            content = item.get("content", item)           # new format has content{}
            title   = content.get("title", "")
            summary = content.get("summary", "")
            if title:
                headlines.append(title)
                if summary:
                    headlines.append(summary[:300])
    except Exception as exc:
        _log.debug("yf_news_fetch_failed", error=str(exc))
    return headlines[:20]

def fetch_google_news(ticker_sym: str, company_name: str = "") -> list[str]:
    """Fetch recent Google News RSS headlines for a ticker. No API key needed."""
    query = company_name if company_name else ticker_sym.split(":")[1] if ":" in ticker_sym else ticker_sym
    q = urllib.parse.quote(f"{query} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            txt = resp.read().decode("utf-8", errors="ignore")
        titles = re.findall(r"<title>(?!Google News)(.*?)</title>", txt)
        return [re.sub(r"<[^>]+>", "", t).strip() for t in titles if t.strip()][:15]
    except Exception:
        return []

def compute_news_sentiment(headlines: list[str]) -> dict:
    """Score headlines with the primary scorer; include VADER dual-score when available."""
    if not headlines:
        return {"score": 0.0, "label": "Neutral", "count": 0}
    scores = [vader_compound(h) for h in headlines]
    mean   = sum(scores) / len(scores)
    pos    = sum(1 for s in scores if s > 0.05)
    neg    = sum(1 for s in scores if s < -0.05)
    label  = ("Very Bullish" if mean > 0.35 else
              "Bullish"      if mean > 0.10 else
              "Slightly Bullish" if mean > 0.02 else
              "Neutral"      if mean >= -0.02 else
              "Slightly Bearish" if mean >= -0.10 else
              "Bearish"      if mean >= -0.35 else
              "Very Bearish")
    result = {"score": round(mean, 4), "label": label, "count": len(headlines),
              "pos_count": pos, "neg_count": neg, "headlines": headlines[:8]}
    # Dual-score: compute reference score from the opposite backend
    if _USE_DUAL_SCORES:
        if _SENTIMENT_BACKEND == "vader":
            # VADER is primary → compute FinBERT as reference
            try:
                fb_scorer  = _finbert_sentiment_scorer()
                fb_scores  = [fb_scorer(h) for h in headlines]
                result["finbert_score"] = round(sum(fb_scores) / len(fb_scores), 4)
            except Exception:
                pass  # FinBERT unavailable; dual score silently omitted
        else:
            # FinBERT is primary → compute VADER as reference
            vader_scorer = _vader_sentiment_scorer()
            vader_scores = [vader_scorer(h) for h in headlines]
            result["vader_score"] = round(sum(vader_scores) / len(vader_scores), 4)
    return result

# ─── LAYER 1: TECHNICAL QUALITY ─────────────────────────────────────────────────
def _score_number(value, *, low=None, high=None):
    """Parse a numeric signal; reject booleans, NaN, infinities and invalid ranges.

    Missing and invalid observations both become None, never a perfect score.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if low is not None and number < low:
        return None
    if high is not None and number > high:
        return None
    return number


def _score_observed(value) -> bool:
    """Only finite numeric values or explicit booleans count as measured data."""
    return isinstance(value, bool) or _score_number(value) is not None


def score_technical(meta: dict, screener: dict = None) -> dict:
    """
    Score the WIN strategy signal quality from the fire message.
    Core: WinRate(26%) + NetProfit%(13%) + RSI(9%) + Squeeze(7%) + ADX(7%).
    Optional: RelVol(9%) + SMA200(9%) + MACD(8%) + OBV(6%) + BB width(6%).
    
    SMA200 and MACD come from TV Screener (free batch call, all markets).
    When signals are unavailable, weights redistribute to WinRate/NetProfit.
    """
    def norm10(v, good, bad):
        """Linear scale: good→10, bad→0; missing/invalid→neutral."""
        v = _score_number(v)
        if v is None: return 5.0
        if v >= good: return 10.0
        if v <= bad:  return 0.0
        return round(10 * (v - bad) / (good - bad), 2)

    meta = meta or {}
    sc = dict(screener or {})
    for key in ("tv_rsi", "tv_adx", "rel_vol", "sma200_pct", "macd_hist",
                "close", "volatility", "obv_divergence", "bb_width"):
        sc[key] = _score_number(sc.get(key))

    # WinRate: 80% → 10pts, 20% → 0 (None → neutral 5.0)
    wr      = _score_number(meta.get("WinRate"), low=0, high=100)
    s_wr    = norm10(wr, 80, 20)

    # NetProfit%: 50 → 10pts, 0 → 0 (None → neutral 5.0)
    np_     = _score_number(meta.get("NetProfit%"))
    s_np    = norm10(np_, 50, 0)

    # RSI zone: smooth piecewise-linear to avoid cliff-edge scoring.
    # Old code had RSI 59.9→5.0, RSI 60.0→10.0 (100% discontinuity).
    # New: continuous ramp so a 0.1-point RSI move can't swing the score by 5pts.
    # For news/screener sources the alert message has no rsi field; fall back to
    # tv_rsi fetched via the TV Screener batch call (sc dict).
    rsi = _score_number(meta.get("rsi"), low=0, high=100)
    if rsi is None:
        rsi = _score_number(sc.get("tv_rsi"), low=0, high=100)
    if rsi is None:
        s_rsi = 5.0
    elif rsi <= 40:
        s_rsi = max(1.0, 1.0 + (rsi - 20) / 20 * 2.0)   # 20→1.0, 40→3.0
    elif rsi <= 55:
        s_rsi = 3.0 + (rsi - 40) / 15 * 2.0              # 40→3.0, 55→5.0
    elif rsi <= 60:
        s_rsi = 5.0 + (rsi - 55) / 5  * 5.0              # 55→5.0, 60→10.0 (steep)
    elif rsi <= 70:
        s_rsi = 10.0                                       # 60–70 = ideal zone
    elif rsi <= 80:
        s_rsi = 10.0 - (rsi - 70) / 10 * 3.0             # 70→10.0, 80→7.0
    elif rsi <= 90:
        s_rsi = 7.0  - (rsi - 80) / 10 * 4.0             # 80→7.0,  90→3.0
    else:
        s_rsi = max(1.0, 3.0 - (rsi - 90) / 10 * 2.0)   # 90→3.0, 100→1.0
    s_rsi = round(min(10.0, max(1.0, s_rsi)), 2)

    # Squeeze Momentum: sqzOff=1 means squeeze energy just released → bullish
    sqz     = meta.get("sqzOff")
    sqz_val = str(sqz).strip().lower() if sqz is not None else ""
    if sqz_val in ("1", "1.0", "true"):
        s_sqz = 10.0
    elif sqz_val in ("0", "0.0", "false"):
        s_sqz = 4.0
    else:
        s_sqz = 5.0  # missing/invalid ≠ negative breakout evidence

    # ADX: >30 = strong trend, good for breakout
    # For news/screener sources, fall back to tv_adx from TV Screener batch data.
    adx = _score_number(meta.get("ADX"), low=0, high=100)
    if adx is None:
        adx = _score_number(sc.get("tv_adx"), low=0, high=100)
    s_adx = norm10(adx, 35, 15)  # missing→5.0, without inventing raw ADX=20

    # Relative Volume from TV Screener (live, real-time)
    rv = sc.get("rel_vol")
    if rv is not None:
        if   rv >= 3.0: s_rv = 10.0
        elif rv >= 2.0: s_rv = 8.0
        elif rv >= 1.5: s_rv = 6.5
        elif rv >= 1.0: s_rv = 5.0
        elif rv >= 0.7: s_rv = 3.0
        else:           s_rv = 1.0
    else:
        s_rv = None

    # SMA200 trend — price above SMA200 = uptrend confirmation (free from TV screener)
    # >10% above = strong uptrend, 0-10% = healthy, <0% = below trend = caution
    sma200_pct = sc.get("sma200_pct")
    if sma200_pct is not None:
        if   sma200_pct > 20:  s_sma = 9.0   # far above — strong trend
        elif sma200_pct > 10:  s_sma = 10.0   # ideal sweet spot
        elif sma200_pct > 3:   s_sma = 8.0    # safely above
        elif sma200_pct > 0:   s_sma = 6.0    # just above — watch for breakdown
        elif sma200_pct > -5:  s_sma = 3.0    # below SMA200 — caution
        else:                  s_sma = 1.0    # deep below — bearish structure
    else:
        s_sma = None

    # MACD histogram — normalised by expected daily move to compare across price levels.
    # Without normalisation: NSE ₹100 stocks have hist ±0.05 (scores 4.0 always),
    # while US $300 stocks have hist ±1.5 (scores 8–10 routinely) — systematic bias.
    # Fix: divide hist by (close × daily_vol%) so all markets use the same scale.
    macd_hist = sc.get("macd_hist")
    if macd_hist is not None:
        close_px = sc.get("close")
        vol_d    = sc.get("volatility")   # Volatility.D (daily %, e.g. 1.5 = 1.5%)
        if close_px and close_px > 0 and vol_d and vol_d > 0:
            daily_move = close_px * vol_d / 100.0   # expected price move per day
            macd_norm  = macd_hist / daily_move       # hist as fraction of daily move
        else:
            # Can't normalise — use sign only to avoid penalising NSE/ASX stocks
            macd_norm = 0.08 if macd_hist > 0 else (-0.08 if macd_hist < 0 else 0.0)
        if   macd_norm > 0.12:   s_macd = 10.0   # hist > 12% of daily range = strong bullish
        elif macd_norm > 0.06:   s_macd = 8.0
        elif macd_norm > 0.0:    s_macd = 6.5    # mild bullish
        elif macd_norm > -0.06:  s_macd = 4.0    # mild bearish
        else:                    s_macd = 1.5    # strong bearish
    else:
        s_macd = None

    # ── OBV divergence confirmation (volume confirms price trend)
    # When price makes new high but OBV doesn't → bearish divergence (weak)
    # NOTE: intentionally unpopulated — TV Screener's "OBV" column returns raw
    # cumulative volume, not a divergence signal, and computing real divergence
    # needs multi-bar OHLCV history (not a single scanner snapshot). No current
    # data source provides this; this sub-score always falls back to None and
    # its 6% weight redistributes to WR/NP (verified 2026-07-06, see BB width
    # below which WAS fixed the same way once a real per-symbol data source —
    # BB.upper/BB.lower — was confirmed available).
    obv_signal = sc.get("obv_divergence")
    if obv_signal is not None:
        # obv_divergence: positive = confirming trend (bullish), negative = diverging
        if   obv_signal > 0.5:   s_obv = 9.0
        elif obv_signal > 0:     s_obv = 7.0
        elif obv_signal > -0.5:  s_obv = 4.0
        else:                    s_obv = 2.0
    else:
        s_obv = None

    # ── Bollinger Band bandwidth (volatility compression → breakout potential)
    # BB width = (upper - lower) / close × 100 (close used as basis proxy —
    # TV Screener doesn't expose a separate BB.basis column; BB.upper/BB.lower
    # are fetched in fetch_tv_screener() and this was always None before
    # 2026-07-06 since those two columns were never requested from TV).
    # Narrow bands (<5%) = squeeze building, wide bands (>20%) = overextended
    bb_width = sc.get("bb_width")
    if bb_width is not None:
        if   bb_width < 3:    s_bb = 9.0   # extreme compression → imminent breakout
        elif bb_width < 6:    s_bb = 8.0   # tight squeeze
        elif bb_width < 10:   s_bb = 6.0   # normal
        elif bb_width < 15:   s_bb = 4.0   # widening, volatility expanding
        else:                 s_bb = 2.0   # overextended, breakout already happened
    else:
        s_bb = None

    # Dynamic weighting: distribute available signal weights
    # Base allocation: WR 26%, NP 13%, RSI 9%, Sqz 7%, ADX 7% = 62% (core)
    # Optional: RV 9%, SMA200 9%, MACD 8%, OBV 5%, BB 5% = 36% (optional, may be missing)
    # Missing weight → redistributed to WR (60%) and NP (40%)
    # When all signals present, total = 98% (rounding tolerance)
    components = [
        ("wr",      s_wr,   0.26),
        ("np",      s_np,   0.13),
        ("rsi",     s_rsi,  0.09),
        ("squeeze", s_sqz,  0.07),
        ("adx",     s_adx,  0.07),
    ]
    optional = [
        ("rel_vol", s_rv,    0.09),
        ("sma200",  s_sma,   0.09),
        ("macd",    s_macd,  0.08),
        ("obv",     s_obv,   0.06),
        ("bb_width", s_bb,   0.06),
    ]
    # Redistribute missing optional weights to WR and NP (most predictive)
    available = [(n, s, w) for n, s, w in optional if s is not None]
    missing_weight = sum(w for _, s, w in optional if s is None)
    redistribute_wr = missing_weight * 0.60
    redistribute_np = missing_weight * 0.40
    # Adjust core weights
    adj_components = []
    for name, score, weight in components:
        if name == "wr":
            adj_components.append((name, score, weight + redistribute_wr))
        elif name == "np":
            adj_components.append((name, score, weight + redistribute_np))
        else:
            adj_components.append((name, score, weight))
    all_items = adj_components + available
    total = round(min(10.0, max(0.0, sum(s * w for _, s, w in all_items))), 2)
    # Coverage is measured separately; neutral substitutions do not count as data.
    # Only measurements actually used in the weighted calculation are counted.
    availability = {
        "wr": wr is not None, "np": np_ is not None,
        "rsi": rsi is not None, "squeeze": sqz_val in
            ("1", "1.0", "true", "0", "0.0", "false"),
        "adx": adx is not None,
    }
    availability.update({name: score is not None for name, score, _ in optional})
    observed_weight = sum(w for name, _, w in all_items if availability.get(name, False))
    coverage = round(min(1.0, max(0.0, observed_weight)), 3)

    return {
        "total":   total,
        "coverage": coverage,
        "wr":      s_wr,
        "np":      s_np,
        "rsi":     s_rsi,
        "squeeze": s_sqz,
        "adx":     s_adx,
        "rel_vol": s_rv,
        "sma200":  s_sma,
        "macd":    s_macd,
        "obv":     s_obv,
        "bb_width": s_bb,
        "raw_wr":  wr,
        "raw_np":  np_,
        "raw_rsi": rsi,
        "raw_adx": adx,
        "raw_rv":  rv,
        "raw_sma200_pct": sma200_pct,
        "raw_macd_hist":  macd_hist,
    }

# ─── LAYER 2: FUNDAMENTAL QUALITY ───────────────────────────────────────────────
def fetch_fundamentals(ticker: str) -> dict:
    try:
        t   = yf.Ticker(ticker)
        inf = t.info or {}
    except Exception as e:
        return {"error": str(e)}

    def g(key, default=None):
        v = inf.get(key)
        return v if v not in (None, "N/A") else default

    def pct(key):
        v = g(key)
        return round(v * 100, 2) if v is not None else None

    return {
        "company_name":  g("longName", g("shortName", ticker)),
        "sector":        g("sector",   "N/A"),
        "industry":      g("industry", "N/A"),
        "market_cap":    g("marketCap"),
        "currency":      g("currency", ""),
        # Valuation
        "forward_pe":    g("forwardPE"),
        "peg_ratio":     g("pegRatio"),
        "price_to_book": g("priceToBook"),
        "price_to_sales":g("priceToSalesTrailing12Months"),
        "ev_ebitda":     g("enterpriseToEbitda"),
        "ev_revenue":    g("enterpriseToRevenue"),
        # Profitability
        "roe":           pct("returnOnEquity"),
        "roa":           pct("returnOnAssets"),
        "profit_margin": pct("profitMargins"),
        "gross_margin":  pct("grossMargins"),
        # Growth
        "revenue_growth":  pct("revenueGrowth"),
        "earnings_growth": pct("earningsGrowth"),
        # Health
        "debt_to_equity": g("debtToEquity"),
        "current_ratio":  g("currentRatio"),
        "free_cashflow":  g("freeCashflow"),
        # Div / Price
        "dividend_yield":  pct("dividendYield"),
        "current_price":   g("currentPrice", g("regularMarketPrice")),
        "52w_high":        g("fiftyTwoWeekHigh"),
        "52w_low":         g("fiftyTwoWeekLow"),
        "beta":            g("beta"),
        "analyst_target":  g("targetMeanPrice"),
        # Sub-signal enrichments (L2 improvement #1)
        "total_revenue":   g("totalRevenue"),
        "total_debt":      g("totalDebt"),
        "total_cash":      g("totalCash"),
        "operating_cashflow": g("operatingCashflow"),
        "net_income":      g("netIncomeToCommon"),
        "total_assets":    g("totalAssets"),
    }

def score_fundamental(fund: dict) -> dict:
    # Prevent non-finite vendor fields propagating into scores or comparisons.
    fund = dict(fund or {})
    for key in ("forward_pe", "peg_ratio", "price_to_sales", "ev_revenue",
                "ev_ebitda", "price_to_book", "roe", "profit_margin",
                "gross_margin", "earnings_growth", "revenue_growth",
                "debt_to_equity", "current_ratio", "free_cashflow",
                "market_cap", "roa", "returnOnAssets"):
        fund[key] = _score_number(fund.get(key))

    def val(v, good, bad, *, zero_valid=False):
        """Lower is better; valuation multiples <=0 invalid, but zero debt is ideal."""
        if v is None: return 5.0
        if v < 0 or (v == 0 and not zero_valid): return 0.0
        if v <= good: return 10.0
        if v >= bad:  return 0.0
        return round(10 * (bad - v) / (bad - good), 2)

    def hi(v, good, bad):
        """Higher is better, with missing/invalid observations neutral."""
        if v is None: return 5.0
        if v >= good: return 10.0
        if v <= bad:  return 0.0
        return round(10 * (v - bad) / (good - bad), 2)

    s_fpe  = val(fund.get("forward_pe"),    10, 40)
    s_peg_raw = fund.get("peg_ratio")
    s_peg  = val(s_peg_raw, 1, 4)

    # P/S and P/B: NOT available via TV Screener for non-US markets (NSE/ASX/HKEX/SGX).
    # TV Screener reports price_to_sales_ttm / price_to_book_fwd as HTTP 400 INVALID.
    # Fallback strategy:
    #   • P/S missing → try EV/Revenue first (same concept, just uses EV numerator)
    #     then EV/EBITDA as last resort.
    #     EV/Revenue: <2 = very cheap, >8 = expensive.  Aligns with P/S scale.
    #     EV/EBITDA: <8 = very cheap, >25 = expensive.
    #   • P/B missing → no clean substitute; redistribute its 20% weight to P/E + PEG.
    # This prevents 45% of the valuation sub-score from being stuck at 5.0 for NSE/BSE stocks.
    ps_raw = fund.get("price_to_sales")
    s_ps: float | None = val(ps_raw, 1, 10) if ps_raw is not None else None
    if s_ps is None:
        ev_rev = fund.get("ev_revenue")
        if ev_rev is not None:
            s_ps = val(ev_rev, 2, 8)    # EV/Revenue: <2=cheap=10, >8=expensive=0
        else:
            ev = fund.get("ev_ebitda")
            if ev is not None:
                s_ps = val(ev, 8, 25)   # EV/EBITDA remapped: <8=cheap=10, >25=expensive=0

    pb_raw = fund.get("price_to_book")
    s_pb: float | None = val(pb_raw, 1, 5) if pb_raw is not None else None

    # Dynamic valuation weighting — redistribute missing weights to P/E (60%) and PEG (40%)
    # Base: P/E 35%, P/S 25%, PEG 20%, P/B 20%
    _val_base = [("pe", s_fpe, 0.35), ("peg", s_peg, 0.20)]
    _val_opt  = [("ps", s_ps,  0.25), ("pb",  s_pb,  0.20)]
    _miss_w   = sum(w for _, s, w in _val_opt if s is None)
    _adj_val  = [
        ("pe",  s_fpe, 0.35 + _miss_w * 0.60),
        ("peg", s_peg, 0.20 + _miss_w * 0.40),
    ]
    _avail_opt = [(n, s, w) for n, s, w in _val_opt if s is not None]
    s_val = round(sum(s * w for _, s, w in _adj_val + _avail_opt), 2)

    s_roe  = hi(fund.get("roe"),          25, 0)   # ROE<0% = bad, >25% = great
    s_pm   = hi(fund.get("profit_margin"),20,  -5)
    s_gm   = hi(fund.get("gross_margin"), 50,  10)
    s_prof = round(s_roe*0.4 + s_pm*0.4 + s_gm*0.2, 2)

    # Revenue growth is unavailable from the TV screener, so fall back to EPS
    # growth alone rather than blending against a permanently neutral 5.0.
    s_eg   = hi(fund.get("earnings_growth"), 20, -20)
    if fund.get("revenue_growth") is None:
        s_grow = s_eg
    else:
        s_rg   = hi(fund.get("revenue_growth"),  20, -10)
        s_grow = round(s_rg*0.5 + s_eg*0.5, 2)

    de     = fund.get("debt_to_equity")
    cr     = fund.get("current_ratio")
    fcf    = fund.get("free_cashflow")
    s_de   = val(de, 30, 200, zero_valid=True)
    s_cr   = hi(cr, 2.5, 0.5) if cr is not None else 5.0
    s_fcf  = 10.0 if (fcf and fcf > 0) else (0.0 if (fcf and fcf < 0) else 5.0)
    s_hlth = round(s_de*0.4 + s_cr*0.3 + s_fcf*0.3, 2)

    # ── FCF Yield — free cashflow / market cap (strongest single fundamental signal)
    fcf_yield = None
    mcap = fund.get("market_cap")
    if fcf and mcap and mcap > 0:
        fcf_yield = round(fcf / mcap * 100, 2)  # as percentage
    s_fcf_yield = hi(fcf_yield, 8, -2) if fcf_yield is not None else None

    # ── Piotroski F-Score (simplified 6-point version from available data)
    # Academic 9-point score is ideal, but requires historical comparisons.
    # Our 6-point version uses current snapshot data available from yfinance:
    #   1. FCF > 0
    #   2. ROA > 0
    #   3. ROE > 10% (profitability)
    #   4. Current ratio > 1.0 (liquidity)
    #   5. Gross margin > 30%
    #   6. Revenue growth > 0
    # Distinguish an observed failed criterion from one we could not measure.
    # Scale sparse evidence to neutral via its fraction of the six criteria:
    # zero observed→5, all six observed→original 0–10 score.
    roa_val = fund.get("roa")
    if roa_val is None:
        roa_val = fund.get("returnOnAssets")
    rg_val = fund.get("revenue_growth")
    if rg_val is None:
        rg_val = fund.get("earnings_growth")
    criteria = (
        (fcf, 0), (roa_val, 0), (fund.get("roe"), 10),
        (fund.get("current_ratio"), 1),
        (fund.get("gross_margin"), 30), (rg_val, 0),
    )
    piotroski_observed = sum(value is not None for value, _ in criteria)
    piotroski = sum(value is not None and value > threshold
                    for value, threshold in criteria)
    piotroski_coverage = round(piotroski_observed / 6.0, 3)
    s_piotroski = round(5.0 + (10.0 * piotroski -
                             5.0 * piotroski_observed) / 6.0, 2)

    # ── Enhanced total: add FCF yield (8%) and Piotroski (7%) to the mix
    # If FCF yield unavailable, its weight goes to profitability (strongest base)
    # Base: val 25%, prof 30%, grow 25%, hlth 20% = 100%
    # New:  val 21%, prof 24%, grow 21%, hlth 17%, fcf_yield 8%, piotroski 9% = 100%
    if s_fcf_yield is not None:
        total = round(
            s_val * 0.21 + s_prof * 0.24 + s_grow * 0.21 +
            s_hlth * 0.17 + s_fcf_yield * 0.08 + s_piotroski * 0.09,
            2)
    else:
        # FCF yield missing: redistribute to base weights
        total = round(
            s_val * 0.25 + s_prof * 0.28 + s_grow * 0.25 +
            s_hlth * 0.13 + s_piotroski * 0.09,
            2)

    return {"total":total, "valuation":s_val, "profitability":s_prof,
            "growth":s_grow, "health":s_hlth,
            "fcf_yield": s_fcf_yield, "piotroski": s_piotroski,
            "piotroski_coverage": piotroski_coverage,
            "piotroski_observed": piotroski_observed,
            "raw_fcf_yield": fcf_yield, "raw_piotroski": piotroski}

# ─── EXCHANGE DETECTION ─────────────────────────────────────────────────────────
# yfinance quoteSummary (recommendations, upgrades, earnings_history, calendar)
# only works reliably for US-listed stocks.  NSE / BSE / ASX / HKEX / SGX / TSE
# etc. return HTTP 404 "No fundamentals data found" for those endpoints.
# TV Screener fundamentals (pre-fetched in tv_fund_cache) cover ALL markets.
_YF_QUOTESUMMARY_EXCHANGES = frozenset({
    "NYSE", "NASDAQ", "AMEX", "BATS", "CBOE", "ARCA",    # US equities & ETFs
    "TSX", "TSXV",                                         # Canada
})

def _yf_has_quotesummary(tv_symbol: str) -> bool:
    """True only for exchanges where yfinance quoteSummary endpoints work."""
    exch = tv_symbol.split(":")[0].upper() if ":" in tv_symbol else "NYSE"
    return exch in _YF_QUOTESUMMARY_EXCHANGES


# TV's recommendation_mark does not follow the documented 1=Strong Buy … 5=Strong
# Sell scale: observed values span ~1.05 (a real Strong Buy) to ~1.74 (a real
# Sell), so treating it as 1-5 collapses every market into "Strong Buy". These
# constants are an OLS fit of mark against yfinance bull_ratio over the symbols
# carrying both sources (n=11, R²=0.92, max residual 0.15), which puts TV-sourced
# and yfinance-sourced names on one comparable 0-2 distribution.
_TV_MARK_BULL_INTERCEPT = 2.4835
_TV_MARK_BULL_SLOPE     = -1.1896


def _bull_ratio_from_tv_mark(rec_mark) -> float:
    """Map a TV recommendation_mark onto the yfinance 0-2 bull_ratio scale."""
    raw = _TV_MARK_BULL_INTERCEPT + _TV_MARK_BULL_SLOPE * float(rec_mark)
    return round(max(0.0, min(2.0, raw)), 3)


def _consensus_label(bull_ratio: float) -> str:
    """Shared bands so a label means the same thing whatever the source."""
    if   bull_ratio > 1.5: return "Strong Buy"
    elif bull_ratio > 0.9: return "Buy"
    elif bull_ratio > 0.4: return "Hold"
    else:                  return "Sell"


# yfinance supplies the period as a ``period`` COLUMN with a plain RangeIndex,
# so the index carries no period information and ``iloc[-1]`` picked an
# arbitrary (often stale) row. The period column is authoritative when present;
# the index is used only as a legacy fallback.
_CURRENT_PERIOD_KEYS = ("0m", "0", "current", "now")
# Index-based matching must not treat a bare RangeIndex position as a period
# label (index 0 would otherwise collide with the "0" key).
_CURRENT_PERIOD_INDEX_KEYS = ("0m", "current", "now")


def _period_offset(label, require_suffix: bool = False):
    """Return the absolute month offset for a period label, or ``None``.

    Accepts yfinance period labels ("0m", "-1m", "-3m") and, unless
    ``require_suffix`` is set, bare integers ("0", "-1"). ``require_suffix``
    is used for index-based lookups so a plain RangeIndex is never mistaken
    for a set of period offsets.
    """
    if label is None:
        return None
    pat = r"\s*(-?\d+)\s*m\s*" if require_suffix else r"\s*(-?\d+)\s*m?\s*"
    m = re.fullmatch(pat, str(label), re.IGNORECASE)
    return abs(int(m.group(1))) if m else None


def _select_current_recommendation_row(rec):
    """Return the current-period row of a yfinance recommendations frame.

    Prefers the explicit ``period`` column (yfinance's actual shape), then a
    period-encoded index, then the newest parseable period offset, then the
    last row. Returns ``None`` when no row is usable.
    """
    if rec is None or getattr(rec, "empty", True):
        return None

    # 1. Period column (yfinance ≥0.2): authoritative, order-independent.
    if "period" in getattr(rec, "columns", []):
        periods = list(rec["period"])
        for key in _CURRENT_PERIOD_KEYS:
            for pos, val in enumerate(periods):
                if str(val).strip().lower() == key:
                    return rec.iloc[pos]
        best_pos, best_off = None, None
        for pos, val in enumerate(periods):
            off = _period_offset(val)
            if off is not None and (best_off is None or off < best_off):
                best_pos, best_off = pos, off
        if best_pos is not None:
            return rec.iloc[best_pos]
        return rec.iloc[-1]

    # 2. Legacy: period encoded in the index.
    idx = list(rec.index)
    for key in _CURRENT_PERIOD_INDEX_KEYS:
        for i in idx:
            if str(i).strip().lower() == key:
                return rec.loc[i]
    best_i, best_off = None, None
    for i in idx:
        off = _period_offset(i, require_suffix=True)
        if off is not None and (best_off is None or off < best_off):
            best_i, best_off = i, off
    if best_i is not None:
        return rec.loc[best_i]
    # 3. Deterministic last-resort fallback.
    return rec.iloc[-1]


def _analyst_row_is_valid(row) -> bool:
    """True when a recommendations row carries a usable analyst breakdown."""
    if row is None:
        return False
    try:
        total = sum(int(row.get(k, 0) or 0)
                    for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    except (TypeError, ValueError):
        return False
    return total > 0


# ── Analyst rating-event classification (shared by the 30d and 60d windows) ──
# The provider's explicit Action is authoritative. Maintenance/reiteration
# events must never be counted as upgrades or downgrades, and initiations are
# tracked separately rather than treated as revisions.
_MAINTAIN_ACTIONS = frozenset({
    "main", "maintain", "maintained", "maintains", "maintaining",
    "reit", "reiterated", "reiterate", "reiterates", "reiterating",
    "reaffirm", "reaffirmed", "reaffirms", "reaffirming",
    "repeat", "repeated", "repeats", "no change", "unchanged", "hold",
})
_UPGRADE_ACTIONS = frozenset({
    "up", "upgrade", "upgraded", "upgrades", "upgrading",
    "raise", "raised", "raises", "raising",
    "increase", "increased", "increases", "increasing",
})
_DOWNGRADE_ACTIONS = frozenset({
    "down", "downgrade", "downgraded", "downgrades", "downgrading",
    "lower", "lowered", "lowers", "lowering",
    "decrease", "decreased", "decreases", "decreasing", "cut",
})
_INIT_ACTIONS = frozenset({
    "init", "initiate", "initiated", "initiates", "initiating", "initiation",
})

_BULLISH_GRADE_TOKENS = (
    "strong buy", "buy", "outperform", "overweight", "positive",
    "accumulate", "add", "top pick", "conviction",
)
_BEARISH_GRADE_TOKENS = (
    "strong sell", "sell", "underperform", "underweight", "negative", "reduce",
)
_NEUTRAL_GRADE_TOKENS = (
    "hold", "neutral", "market perform", "sector perform", "equal weight",
    "equal-weight", "in-line", "inline", "peer perform", "market weight",
)


def _grade_direction(grade):
    """Rank a grade on a 3-level scale.

    Returns +1 (bullish), 0 (neutral), -1 (bearish), or ``None`` when the grade
    is missing or unrecognized.
    """
    g = str(grade or "").strip().lower()
    if not g:
        return None
    # Bearish tokens are checked first so "underperform" isn't matched by "perform".
    if any(t in g for t in _BEARISH_GRADE_TOKENS):
        return -1
    if any(t in g for t in _BULLISH_GRADE_TOKENS):
        return 1
    if any(t in g for t in _NEUTRAL_GRADE_TOKENS):
        return 0
    return None


def _classify_rating_event(action, from_grade=None, to_grade=None) -> str:
    """Classify an analyst rating event.

    Returns one of ``"upgrade"``, ``"downgrade"``, ``"initiation"``,
    ``"maintain"``, or ``"unknown"``.

    The provider's explicit ``Action`` is authoritative. Maintenance and
    reiteration events are never counted as upgrades or downgrades. Grade
    comparison is a documented fallback used only when the action is missing or
    unrecognized.
    """
    a = str(action or "").strip().lower()
    if a in _MAINTAIN_ACTIONS:
        return "maintain"
    if a in _INIT_ACTIONS:
        return "initiation"
    if a in _UPGRADE_ACTIONS:
        return "upgrade"
    if a in _DOWNGRADE_ACTIONS:
        return "downgrade"
    # Fallback: compare grades only when the action is unknown.
    fd, td = _grade_direction(from_grade), _grade_direction(to_grade)
    if fd is not None and td is not None and td != fd:
        return "upgrade" if td > fd else "downgrade"
    return "unknown"


def _summarize_rating_events(frame):
    """Summarize a ratings frame.

    Returns ``(has_upgrade, has_downgrade, ups, downs, details)``. Maintenance,
    reiteration, and initiation events are excluded from the up/down counts.
    """
    has_up = has_down = False
    ups = downs = 0
    details: list[str] = []
    for _, row in frame.iterrows():
        kind = _classify_rating_event(
            row.get("Action", ""), row.get("FromGrade", ""), row.get("ToGrade", ""))
        if kind == "upgrade":
            has_up = True
            ups += 1
            _ts = row.name
            _label = _ts.strftime("%m/%d") if hasattr(_ts, "strftime") else str(_ts)
            details.append(f"{_label} {row.get('Firm', '')} → {row.get('ToGrade', '')}")
        elif kind == "downgrade":
            has_down = True
            downs += 1
    return has_up, has_down, ups, downs, details


# ── Catalyst direction detection (Issue 8) ───────────────────────────────────
# The old catalyst check treated any earnings/guidance keyword as a positive
# catalyst, so "misses earnings and lowered guidance" earned the same +0.5
# bonus as "beats estimates and raises guidance". Direction is now explicit:
# only clearly positive evidence earns the bonus; negative, negated, or mixed
# evidence earns nothing.
_CATALYST_BULLISH = (
    "beat", "beats", "exceed", "exceeds", "exceeded", "surpass", "surpassed",
    "raised guidance", "raises guidance", "raise guidance", "raised outlook",
    "raises outlook", "increased guidance", "increases guidance",
    "record revenue", "record earnings", "record profit", "record sales",
    "revenue growth", "profit growth", "earnings growth", "strong results",
    "better than expected", "above expectations", "tops estimates",
    "beat estimates", "beats estimates", "raised", "raises", "buyback",
    "share repurchase", "dividend increase", "raises dividend",
)
_CATALYST_BEARISH = (
    "miss", "misses", "missed", "below expectations", "worse than expected",
    "lowered guidance", "lowers guidance", "lower guidance", "lowered outlook",
    "lowers outlook", "cut guidance", "cuts guidance", "reduced guidance",
    "reduces guidance", "guidance cut", "profit warning", "revenue decline",
    "profit decline", "earnings decline", "weak results", "disappointing",
    "shortfall", "warns", "warning", "impairment", "write-down", "writedown",
    "layoffs", "restructuring charge", "dividend cut", "cuts dividend",
    "suspended dividend", "bankruptcy", "default", "investigation", "lawsuit",
)
_CATALYST_NEGATORS = (
    "not", "no", "never", "without", "fails to", "failed to", "unable to",
    "did not", "does not", "doesn't", "didn't", "won't", "will not",
    "cannot", "can't",
)


def _phrase_present(text: str, phrase: str) -> bool:
    """Word-boundary phrase match (so 'beat' doesn't match 'beatnik')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _is_negated(text: str, phrase: str) -> bool:
    """True when *phrase* is preceded by a negation within a short window."""
    m = re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text)
    if not m:
        return False
    window = text[max(0, m.start() - 30):m.start()]
    return any(_phrase_present(window, neg) for neg in _CATALYST_NEGATORS)


def _catalyst_signals(text: str) -> tuple[bool, bool]:
    """Return ``(has_bullish, has_bearish)`` evidence for one text.

    Kept separate from ``_catalyst_direction`` so aggregation can see BOTH
    signals in a single mixed headline instead of losing the bearish half when
    the headline collapses to neutral (review 2026-10-13, Issue 5 follow-up).
    """
    if not text or not isinstance(text, str):
        return False, False
    t = text.lower()
    bull = any(_phrase_present(t, p) and not _is_negated(t, p)
               for p in _CATALYST_BULLISH)
    bear = any(_phrase_present(t, p) for p in _CATALYST_BEARISH)
    return bull, bear


def _catalyst_direction(text: str) -> str:
    """Classify a headline/snippet as ``"bullish"``, ``"bearish"``, or ``"neutral"``.

    Negated bullish phrases (e.g. "did not beat estimates") are not bullish.
    Mixed evidence (both bullish and bearish signals) is treated conservatively
    as neutral so it earns no bonus.
    """
    bull, bear = _catalyst_signals(text)
    if bull and bear:
        return "neutral"   # mixed evidence → conservative
    if bull:
        return "bullish"
    if bear:
        return "bearish"
    return "neutral"


def _detect_catalyst(headlines) -> tuple[bool, str]:
    """Return ``(is_bullish_catalyst, direction)`` for a list of headlines.

    ``is_bullish_catalyst`` is True only when at least one headline is clearly
    bullish and none is bearish. Mixed evidence — a bullish headline alongside
    a bearish one, or a single headline carrying both signals — is reported as
    ``"mixed"`` and earns no bonus, so the conservative policy applies whether
    the conflict is within one headline or across several (review 2026-10-13,
    Issue 5 follow-up).
    """
    has_bull = has_bear = False
    for h in (headlines or []):
        b, s = _catalyst_signals(h)
        has_bull = has_bull or b
        has_bear = has_bear or s
    if has_bull and has_bear:
        return False, "mixed"
    if has_bear:
        return False, "bearish"
    if has_bull:
        return True, "bullish"
    return False, "neutral"


# ─── LAYER 3: SENTIMENT & ANALYST INTELLIGENCE ──────────────────────────────────
def fetch_sentiment_data(ticker_obj, company_name: str, tv_symbol: str,
                          tv_fund: dict | None = None,
                          prefetched: dict | None = None) -> dict:
    """
    Fetch analyst recs, upgrades, earnings beat rate, earnings calendar, news.

    PRIMARY source:  TV Screener batch data (tv_fund) — works for ALL markets
                     with no extra HTTP calls (data already in tv_fund_cache).
    FALLBACK source: yfinance — attempted ONLY for US-listed stocks where
                     quoteSummary API actually returns data.  Non-US symbols
                     (NSE, BSE, ASX, HKEX, SGX …) skip yfinance entirely
                     to avoid HTTP 404 errors.
    """
    result = {}
    tf     = tv_fund or {}
    is_us  = _yf_has_quotesummary(tv_symbol)

    # ── 3a. Analyst Consensus ──────────────────────────────────────────────────
    # PRIMARY: TV Screener recommendation_mark (1=Strong Buy → 5=Strong Sell)
    #          or Recommend.All (-1=Strong Sell → +1=Strong Buy).
    #          Both cover NSE/BSE/ASX/HKEX and all global markets.
    rec_mark = tf.get("recommendation_mark")
    rec_all  = tf.get("Recommend.All")

    tv_analyst: dict | None = None
    if rec_mark is not None:
        bull_ratio = _bull_ratio_from_tv_mark(rec_mark)
        tv_analyst = {
            "strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0,
            "total": 0,     # TV gives aggregate, not individual count breakdown
            "bull_ratio": bull_ratio,
            "consensus": _consensus_label(bull_ratio),
            "source": "TV",
        }
    # Recommend.All is a *technical indicator* rating, not analyst consensus.
    # It must not masquerade as analyst evidence inside the sentiment layer.
    # Keep rec_all available elsewhere for technical studies, not here.

    # FALLBACK: yfinance full count breakdown (US only — overrides TV if richer)
    yf_analyst: dict | None = None
    if is_us:
        try:
            rec = _pf_get(prefetched, "recommendations",
                          lambda: ticker_obj.recommendations)
            if rec is not None and not rec.empty:
                # Select the CURRENT period explicitly — row order is not
                # guaranteed, so iloc[-1] could pick a stale period.
                row = _select_current_recommendation_row(rec)
                if _analyst_row_is_valid(row):
                    sb  = int(row.get("strongBuy",  0))
                    b   = int(row.get("buy",        0))
                    h   = int(row.get("hold",       0))
                    s   = int(row.get("sell",       0))
                    ss  = int(row.get("strongSell", 0))
                    total_analysts = sb + b + h + s + ss
                    bull_score = (sb * 2 + b * 1) / max(total_analysts, 1)
                    yf_analyst = {
                        "strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss,
                        "total": total_analysts,
                        "bull_ratio": round(bull_score, 3),
                        "consensus": _consensus_label(bull_score),
                        "source": "yfinance",
                        # yfinance carries the period as a COLUMN with a plain
                        # RangeIndex, so row.name is a numeric position. Record
                        # the period label ("0m") when available and fall back
                        # to the index only for the legacy index-encoded shape.
                        "period": str(row.get("period", row.name)),
                    }
        except Exception as exc:
            _log.debug("yf_analyst_fetch_failed", error=str(exc))

    # Prefer a valid yfinance breakdown; otherwise use the TV aggregate. An
    # invalid/empty yfinance row must never erase a valid TV aggregate.
    if yf_analyst is not None:
        result["analyst"] = yf_analyst
    elif tv_analyst is not None:
        result["analyst"] = tv_analyst
    else:
        result["analyst"] = {"bull_ratio": None, "consensus": "N/A", "total": 0}

    # ── 3b. Recent Upgrades/Downgrades (last 30 days) ─────────────────────────
    # yfinance only — TV Screener has no upgrade/downgrade event feed.
    # Skipped entirely for non-US symbols (no quoteSummary available).
    result["recent_upgrade"]   = False
    result["recent_downgrade"] = False
    result["upgrade_detail"]   = []
    result["revision_velocity"] = 0   # Idea 4: net upgrade count (ups_30d - downs_30d)
    if is_us:
        try:
            ud = _pf_get(prefetched, "upgrades_downgrades",
                         lambda: ticker_obj.upgrades_downgrades)
            if ud is not None and not ud.empty:
                ud = ud.copy()
                ud.index = (ud.index.tz_localize(None)
                            if ud.index.tz is not None else ud.index)
                cutoff = datetime.now() - timedelta(days=60)
                recent = ud[ud.index > cutoff]
                cutoff_30d = datetime.now() - timedelta(days=30)
                recent_30d = ud[ud.index > cutoff_30d]
                # Shared classification: maintenance/reiteration events are
                # never counted as upgrades or downgrades; initiations are
                # tracked separately. Both windows use the same helper.
                has_up, has_down, _ups60, _downs60, details = _summarize_rating_events(recent)
                result["recent_upgrade"]   = has_up
                result["recent_downgrade"] = has_down
                result["upgrade_detail"]   = details
                _has_up30, _has_down30, ups_30d, downs_30d, _ = _summarize_rating_events(recent_30d)
                result["revision_velocity"] = ups_30d - downs_30d
        except Exception as exc:
            _log.debug("yf_upgrades_fetch_failed", error=str(exc))

    # ── 3c. Earnings Beat Rate ─────────────────────────────────────────────────
    # yfinance only — TV Screener exposes no historical earnings surprise series.
    # Skipped entirely for non-US symbols.
    result["earnings_beats"] = {"beat_rate": 0.5, "beats": 0, "quarters": 0, "detail": [],
                                "last_surprise_pct": None, "days_since_report": None}
    if is_us:
        try:
            eh = _pf_get(prefetched, "earnings_history",
                         lambda: ticker_obj.earnings_history)
            if eh is not None and not eh.empty and "surprisePercent" in eh.columns:
                beats  = (eh["surprisePercent"] > 0).sum()
                total  = len(eh)
                detail = []
                for q, row in eh.tail(4).iterrows():
                    sp = row.get("surprisePercent", 0)
                    detail.append(f"{q}: {'✓' if sp > 0 else '✗'} {sp*100:+.1f}%")
                # PEAD: most recent quarter surprise + recency
                last_sp = float(eh["surprisePercent"].iloc[-1]) if not eh.empty else None
                try:
                    last_date = eh.index[-1]
                    if hasattr(last_date, "to_pydatetime"):
                        last_date = last_date.to_pydatetime().replace(tzinfo=None)
                    days_since = (datetime.now() - last_date).days
                except Exception:
                    days_since = None
                result["earnings_beats"] = {
                    "beat_rate": round(beats / max(total, 1), 3),
                    "beats": int(beats), "quarters": int(total),
                    "detail": detail,
                    "last_surprise_pct": round(last_sp, 4) if last_sp is not None else None,
                    "days_since_report": days_since,
                }
        except Exception as exc:
            _log.debug("yf_earnings_history_failed", error=str(exc))

    # ── 3d. Earnings Calendar Risk ────────────────────────────────────────────
    # PRIMARY:  TV Screener earnings_release_next_date (Unix ts, all markets).
    # FALLBACK: yfinance .calendar (US only, only if TV has no date).
    result["earnings_days_away"] = None
    result["earnings_risk"]      = "Unknown"

    ts_next = tf.get("earnings_release_next_date")
    if ts_next:
        try:
            next_dt   = datetime.fromtimestamp(float(ts_next), tz=timezone.utc)
            today     = datetime.now(tz=timezone.utc).date()
            days_away = (next_dt.date() - today).days
            if days_away >= -2:          # allow 2-day slip for timezone differences
                result["earnings_days_away"] = days_away
                result["earnings_date_str"]  = next_dt.strftime("%Y-%m-%d")
                result["earnings_risk"] = (
                    "🔴 High (<14D)"   if days_away < 14 else
                    "🟡 Medium (<28D)" if days_away < 28 else
                    "🟢 Low (>28D)")
        except Exception as exc:
            _log.debug("tv_earnings_date_parse_failed", error=str(exc))

    if result["earnings_days_away"] is None and is_us:      # US-only fallback
        try:
            cal = ticker_obj.calendar
            if cal is not None and "Earnings Date" in cal:
                ed = cal["Earnings Date"]
                if isinstance(ed, list):
                    ed = ed[0]
                if hasattr(ed, "strftime"):
                    days_away = (ed - datetime.now().date()).days
                    result["earnings_days_away"] = days_away
                    result["earnings_date_str"]  = ed.strftime("%Y-%m-%d")
                    result["earnings_risk"] = (
                        "🔴 High (<14D)"   if days_away < 14 else
                        "🟡 Medium (<28D)" if days_away < 28 else
                        "🟢 Low (>28D)")
        except Exception as exc:
            _log.debug("yf_earnings_calendar_failed", error=str(exc))

    # TV Scanner fallback: covers non-US markets (NSE, ASX, HKEX, SGX, etc.)
    # when TV screener batch lacks earnings_release_next_date AND yfinance can't help.
    if result["earnings_days_away"] is None and not is_us and _TV_EARNINGS_OK:
        try:
            ts_fb, date_fb = _tv_fetch_earnings_date(tv_symbol)
            if ts_fb and date_fb:
                next_dt   = datetime.fromtimestamp(float(ts_fb), tz=timezone.utc)
                today     = datetime.now(tz=timezone.utc).date()
                days_away = (next_dt.date() - today).days
                if days_away >= -2:
                    result["earnings_days_away"] = days_away
                    result["earnings_date_str"]  = date_fb
                    result["earnings_risk"] = (
                        "🔴 High (<14D)"   if days_away < 14 else
                        "🟡 Medium (<28D)" if days_away < 28 else
                        "🟢 Low (>28D)")
        except Exception as exc:
            _log.debug("tv_earnings_scanner_failed", error=str(exc))

    # ── 3e. News Sentiment ────────────────────────────────────────────────────
    # Tier 1 — Hard news: Reuters + Dow Jones (authenticated, all markets)
    # Tier 2 — IR/Earnings: Quartr (earnings call summaries, guidance, all markets)
    # Tier 3 — US media: MarketWatch (US stocks only — adds color to hard numbers)
    # Fallback — yfinance (US only) + Google RSS (all markets)
    tv_headlines:      list[str] = []
    quartr_headlines:  list[str] = []
    mwatch_headlines:  list[str] = []
    # Optional-source accumulators are initialized BEFORE the availability
    # branch so a missing optional module (e.g. tv_news) can never raise
    # UnboundLocalError when the merge below references them.
    tv_story_snippets: list[str] = []
    # Article-level items (with id + published timestamp) used for velocity.
    # Snippets are part of their parent article, so they are NOT separate items.
    article_items:     list[dict] = []

    if _TV_NEWS_OK:
        # Tier 1: Reuters + Dow Jones — primary wire services
        # Fetch raw items (with id) so we can also pull story body snippets.
        wire_providers = ["reuters", "dow-jones"]
        wire_items:    list[dict] = []
        try:
            for _prov in wire_providers:
                wire_items.extend(_tv_fetch_headlines(
                    symbol=tv_symbol, provider=_prov, limit=15))
            tv_headlines = list(dict.fromkeys(
                it.get("title", "").strip()
                for it in wire_items if it.get("title")))
            article_items.extend(wire_items)
            # Story snippets: fetch ~300-char shortDescription body for top 3 articles.
            # Full body text gives VADER 3-5× more signal than the headline alone.
            tv_story_snippets = _tv_fetch_story_snippets(wire_items[:6], n=3)
        except Exception:
            tv_headlines = []
            tv_story_snippets = []

        # Tier 2: Quartr — IR reports, earnings transcripts, guidance (all markets)
        try:
            _quartr_items = _tv_fetch_symbol_news_items(
                tv_symbol, providers=["quartr"], limit=10)
            quartr_headlines = [it.get("title", "").strip()
                                for it in _quartr_items if it.get("title")]
            article_items.extend(_quartr_items)
        except Exception:
            quartr_headlines = []

        # Tier 3: MarketWatch — US commentary only (skip for non-US to avoid noise)
        if is_us:
            try:
                _mwatch_items = _tv_fetch_symbol_news_items(
                    tv_symbol, providers=["market-watch"], limit=10)
                mwatch_headlines = [it.get("title", "").strip()
                                    for it in _mwatch_items if it.get("title")]
                article_items.extend(_mwatch_items)
            except Exception:
                mwatch_headlines = []

    # Quartr catalyst detection — IR statements containing earnings/guidance
    # keywords. Direction matters: only clearly positive evidence is a catalyst.
    quartr_catalyst, quartr_catalyst_direction = _detect_catalyst(quartr_headlines)

    # Tier 4 — Moomoo Community: social intelligence (cached, all markets)
    moomoo_headlines: list[str] = []
    try:
        from moomoo_community_intel import get_community_headlines
        _raw = get_community_headlines()
        # Filter to headlines mentioning this stock's ticker
        _bare = tv_symbol.split(":")[-1].upper() if ":" in tv_symbol else tv_symbol.upper()
        ticker_pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(_bare)}(?![A-Z0-9])")
        moomoo_headlines = [h for h in _raw if ticker_pattern.search(h.upper())]
    except Exception:
        moomoo_headlines = []

    # Merge unique text inputs for sentiment scoring:
    # wire headlines → wire story bodies → quartr → marketwatch → moomoo → fallbacks
    all_tv = list(dict.fromkeys(
        tv_headlines + tv_story_snippets + quartr_headlines + mwatch_headlines + moomoo_headlines))
    yf_headlines     = fetch_yf_news(ticker_obj) if (is_us and not all_tv) else []
    google_headlines = fetch_google_news(tv_symbol, company_name) if not all_tv else []
    all_headlines    = list(dict.fromkeys(all_tv + yf_headlines + google_headlines))

    news_result = compute_news_sentiment(all_headlines)
    news_result["source"] = (
        "TV-Reuters+Quartr" if (tv_headlines and quartr_headlines) else
        "TV-Reuters"        if tv_headlines else
        "Moomoo-Community"  if moomoo_headlines else
        "yfinance"          if yf_headlines else
        "Google-RSS"        if google_headlines else "none")
    news_result["quartr_catalyst"]  = quartr_catalyst
    news_result["quartr_catalyst_direction"] = quartr_catalyst_direction
    news_result["quartr_count"]     = len(quartr_headlines)
    news_result["wire_count"]       = len(tv_headlines)
    news_result["mwatch_count"]     = len(mwatch_headlines)
    news_result["moomoo_count"]     = len(moomoo_headlines)
    # Article velocity: distinct articles published in the trailing 24h.
    # This is deliberately separate from the sentiment text-input count
    # (news_result["count"]), which includes snippets and undated items.
    news_result["article_velocity"] = _tv_article_velocity(article_items)
    news_result["article_count"]    = len(article_items)
    news_result["text_input_count"] = news_result.get("count", 0)
    result["news"] = news_result
    return result

def score_sentiment(sent: dict) -> dict:
    # ── 3a Analyst score (0–10): bull_ratio 0→0, 1→5, 2→10 ──
    # Smooth mapping instead of hard cap: ratio × 5 with diminishing returns
    sent = sent or {}
    analyst = sent.get("analyst") or {}
    br = _score_number(analyst.get("bull_ratio"), low=0, high=2)
    total_analysts = _score_number(analyst.get("total"), low=0)
    has_analyst = br is not None and (
        (total_analysts is not None and total_analysts > 0)
        or analyst.get("source") == "TV"
    )
    s_analyst = round(br * 5, 2) if has_analyst else 5.0

    # ── 3b Recent upgrade/downgrade modifier ──
    # Expanded to 60-day window; scale by recency
    s_upgrade = (2.0 if sent.get("recent_upgrade") else
                 (-2.0 if sent.get("recent_downgrade") else 0.0))

    # ── 3c Earnings beat rate — weight recent beats more ──
    eb = sent.get("earnings_beats") or {}
    beat_rate = _score_number(eb.get("beat_rate"), low=0, high=1)
    quarters = _score_number(eb.get("quarters"), low=0)
    beat_rate = 0.5 if beat_rate is None else beat_rate
    quarters = 0 if quarters is None else quarters
    # Confidence scaling: more quarters = more reliable
    # With only 1-2 quarters of data, pull toward neutral (5.0)
    raw_beats = round(beat_rate * 10, 2)
    if quarters < 3:
        # Shrink toward 5.0 when data is sparse
        s_beats = round(5.0 + (raw_beats - 5.0) * (quarters / 3.0), 2)
    else:
        s_beats = raw_beats

    # ── 3d Earnings risk penalty — softer gradient ──
    days = _score_number(sent.get("earnings_days_away"))
    if days is not None and days >= 0:
        if   days < 7:  s_earnings_risk = -2.5   # was -3.0: too harsh
        elif days < 14: s_earnings_risk = -1.5
        elif days < 21: s_earnings_risk = -0.5
        else:           s_earnings_risk = 0.0
    else:
        s_earnings_risk = 0.0

    # ── 3e News sentiment: compound -1→+1, map to 0–10 ──
    news_data  = sent.get("news") or {}
    nc = _score_number(news_data.get("score"), low=-1, high=1)
    nc = 0.0 if nc is None else nc
    s_news_raw = round((nc + 1) * 5, 2)   # -1→0, 0→5, +1→10

    # Soros reflexivity — news velocity boost
    # More distinct articles in 24h = narrative is accelerating = reflexivity
    # loop active. Uses article_velocity (distinct dated articles), NOT the
    # sentiment text-input count, so snippets and undated text can't inflate it.
    # When ≥5 articles reach us, shift 5% weight from analyst→news.
    news_count     = news_data.get("article_velocity")
    if news_count is None:
        # Backward-compatible fallback for callers that only supply "count".
        news_count = news_data.get("count", 0)
    news_count = _score_number(news_count, low=0)
    news_count = 0 if news_count is None else news_count
    velocity_boost = min(5, max(0, news_count - 5))   # 0 at ≤5, caps at 5 extra
    # Weight shift: each extra headline beyond 5 shifts 1% from analyst→news
    # analyst base: 45%, news base: 25%; max shift = 5% → analyst 40%, news 30%
    w_analyst = max(0.40, 0.45 - velocity_boost * 0.01)
    w_news    = min(0.30, 0.25 + velocity_boost * 0.01)
    w_beats   = 1.0 - w_analyst - w_news   # remainder (≈ 0.30)
    s_news    = s_news_raw

    # Quartr IR catalyst bonus: +0.5 only for a clearly POSITIVE earnings/
    # guidance catalyst. Negative or mixed evidence earns no bonus.
    quartr_bonus = 0.5 if news_data.get("quartr_catalyst") else 0.0

    # ── Idea 3: PEAD bonus — Post-Earnings Announcement Drift ──
    # Stocks that beat/miss estimates show directional drift for 1-3 months.
    # Only applies to US stocks where earnings_history is available (<90 days old).
    pead_bonus = 0.0
    last_sp  = _score_number(eb.get("last_surprise_pct"))
    days_rep = _score_number(eb.get("days_since_report"))
    if last_sp is not None and days_rep is not None and 0 <= days_rep <= 90:
        if   last_sp >  0.10:  pead_bonus =  1.0   # big beat  → strong drift signal
        elif last_sp >  0.02:  pead_bonus =  0.5   # small beat
        elif last_sp < -0.10:  pead_bonus = -1.0   # big miss  → negative drift
        elif last_sp < -0.02:  pead_bonus = -0.5   # small miss

    # ── Idea 4: Revision Momentum bonus — net analyst upgrade velocity ──
    # Counting net upgrades/downgrades in 30 days (not just boolean flag).
    # Reflects analyst conviction building: 3+ upgrades → sector momentum confirms.
    rev_vel = _score_number(sent.get("revision_velocity"))
    rev_vel = 0 if rev_vel is None else rev_vel
    if   rev_vel >= 3:  revision_bonus =  1.5
    elif rev_vel == 2:  revision_bonus =  1.0
    elif rev_vel == 1:  revision_bonus =  0.5
    elif rev_vel == -1: revision_bonus = -0.5
    elif rev_vel == -2: revision_bonus = -1.0
    elif rev_vel <= -3: revision_bonus = -1.5
    else:               revision_bonus =  0.0

    # Weighted total (clamped 0–10)
    base  = (s_analyst * w_analyst
           + s_beats   * w_beats
           + s_news    * w_news)
    total = base + s_upgrade * 0.10 + s_earnings_risk + quartr_bonus + pead_bonus + revision_bonus
    total = max(0.0, min(10.0, round(total, 2)))
    return {
        "total":           total,
        "coverage": round(
            w_analyst * float(has_analyst) +
            w_beats * float(quarters > 0 and _score_number(eb.get("beat_rate"), low=0, high=1) is not None) +
            w_news * float((_score_number(news_data.get("count"), low=0) or 0) > 0
                           and _score_number(news_data.get("score"), low=-1, high=1) is not None),
            3,
        ),
        "analyst":         s_analyst,
        "upgrade_mod":     s_upgrade,
        "earnings_beat":   s_beats,
        "earnings_risk":   s_earnings_risk,
        "news":            s_news,
        "news_velocity":   news_count,
        "article_velocity": news_data.get("article_velocity"),
        "text_input_count": news_data.get("text_input_count", news_data.get("count", 0)),
        "quartr_catalyst": news_data.get("quartr_catalyst", False),
        "quartr_catalyst_direction": news_data.get("quartr_catalyst_direction", "neutral"),
        "pead_bonus":      pead_bonus,
        "revision_bonus":  revision_bonus,
        "w_analyst":       round(w_analyst, 2),
        "w_news":          round(w_news, 2),
    }

# ─── LAYER 4: MARKET INTERNALS ───────────────────────────────────────────────────
def _select_pcr_expiry(dates: list[str], today: datetime | None = None) -> str | None:
    """Prefer a 21-60 DTE expiry to reduce weekly options-flow noise."""
    if not dates:
        return None
    anchor = (today or datetime.now(timezone.utc)).date()
    parsed: list[tuple[int, str]] = []
    for value in dates:
        try:
            expiry = datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        parsed.append(((expiry - anchor).days, value))
    medium = [item for item in parsed if 21 <= item[0] <= 60]
    if medium:
        return min(medium, key=lambda item: item[0])[1]
    future = [item for item in parsed if item[0] >= 7]
    if future:
        return min(future, key=lambda item: abs(item[0] - 30))[1]
    return dates[0]


# ─── Per-symbol yfinance prefetch ────────────────────────────────────────────
# L3 and L4 request the same handful of yfinance endpoints for every symbol, one
# at a time, and none of them reads another's output. Fetching them concurrently
# leaves the request count unchanged — only the waiting overlaps.
_YF_PREFETCH_WORKERS = 7
_YF_PREFETCH_TIMEOUT = 30


def _pcr_chain_from_ticker(tk):
    """Return (expiry, chain) for the PCR expiry, or None when unavailable."""
    dates = tk.options
    if not dates:
        return None
    expiry = _select_pcr_expiry(list(dates)) or dates[0]
    return (expiry, tk.option_chain(expiry))


def _fetch_pcr_option_chain(yf_ticker: str):
    """Prefetch variant: needs its own Ticker so threads don't share one."""
    return _pcr_chain_from_ticker(yf.Ticker(yf_ticker))


def _prefetch_yf(yf_ticker: str, is_us: bool, want_history: bool) -> dict:
    """Concurrently fetch the yfinance fields every symbol needs.

    Only unconditional fields are included — anything fetched behind a runtime
    condition (``.calendar``, ``.news``) stays lazy so this never adds a request.
    Each task builds its own Ticker: yfinance caches lazily on the instance, so
    sharing one across threads would race.
    """
    tasks: dict = {}
    if is_us:
        tasks["recommendations"]      = lambda: yf.Ticker(yf_ticker).recommendations
        tasks["upgrades_downgrades"]  = lambda: yf.Ticker(yf_ticker).upgrades_downgrades
        tasks["earnings_history"]     = lambda: yf.Ticker(yf_ticker).earnings_history
        tasks["insider_transactions"] = lambda: yf.Ticker(yf_ticker).insider_transactions
        tasks["info"]                 = lambda: yf.Ticker(yf_ticker).info
        tasks["option_chain"]         = lambda: _fetch_pcr_option_chain(yf_ticker)
    if want_history:
        tasks["history"] = lambda: yf.Ticker(yf_ticker).history(period="6mo", interval="1d")
    if not tasks:
        return {}

    # SD-004: explicit executor + shutdown(wait=False); a stuck endpoint must not
    # hold the loop. One shared deadline bounds the whole prefetch, not each task.
    ex = ThreadPoolExecutor(max_workers=min(_YF_PREFETCH_WORKERS, len(tasks)))
    futures = {name: ex.submit(fn) for name, fn in tasks.items()}
    deadline = time.monotonic() + _YF_PREFETCH_TIMEOUT
    out: dict = {}
    try:
        for name, fut in futures.items():
            try:
                out[name] = fut.result(timeout=max(0.1, deadline - time.monotonic()))
            except Exception as exc:
                _log.debug("yf_prefetch_failed", ticker=yf_ticker, field=name, error=str(exc))
                out[name] = None
    finally:
        ex.shutdown(wait=False)
    return out


def _pf_get(prefetched: dict | None, field: str, getter):
    """Return the prefetched value when present, otherwise fetch it now."""
    if prefetched is not None and field in prefetched:
        return prefetched[field]
    return getter()


def fetch_market_internals(ticker_obj, fund: dict, tv_symbol: str = "",
                           prefetched: dict | None = None) -> dict:
    """
    4a Options PCR and 4b Insider are yfinance endpoints that only work for
    US-listed stocks (NYSE/NASDAQ/AMEX/BATS).  Non-US symbols (NSE, BSE, ASX,
    HKEX, SGX, BINANCE/crypto, forex futures) return HTTP 404 or empty results.
    Guard these calls with _yf_has_quotesummary; 4c (52-week position) uses
    fund dict data only and runs for all markets.
    """
    result = {}
    is_us  = _yf_has_quotesummary(tv_symbol)

    # ── 4a Options PCR (US only) ──────────────────────────────────────────────
    result["pcr"] = None
    result["pcr_available"] = False
    result["pcr_label"] = "N/A"
    if is_us:
        try:
            _oc = _pf_get(prefetched, "option_chain",
                          lambda: _pcr_chain_from_ticker(ticker_obj))
            if _oc:
                pcr_expiry, chain = _oc
                result["pcr_expiry"] = pcr_expiry
                calls_vol   = chain.calls["volume"].fillna(0).sum()
                puts_vol    = chain.puts["volume"].fillna(0).sum()
                pcr = puts_vol / calls_vol if calls_vol > 0 else None
                if pcr is not None:
                    result["pcr"] = round(pcr, 3)
                    result["pcr_available"] = True
                    result["pcr_label"] = (
                        "Very Bullish" if pcr < 0.5 else
                        "Bullish"      if pcr < 0.8 else
                        "Neutral"      if pcr < 1.2 else
                        "Bearish"      if pcr < 1.5 else "Very Bearish")
        except Exception as exc:
            _log.debug("yf_options_pcr_failed", error=str(exc))

    # ── 4b Insider Activity — last 90 days (US only) ──────────────────────────
    result["insider_net"] = "Unknown"
    result["insider_score_raw"] = 5.0
    result["insider_available"] = False
    if is_us:
        try:
            ins = _pf_get(prefetched, "insider_transactions",
                          lambda: ticker_obj.insider_transactions)
            if ins is not None and not ins.empty:
                ins = ins.copy()
                buy_mask  = ins["Text"].str.contains("Purchase|Buy", case=False, na=False)
                sell_mask = ins["Text"].str.contains("Sale|Sell", case=False, na=False)
                buy_val   = ins.loc[buy_mask,  "Value"].fillna(0).sum()
                sell_val  = ins.loc[sell_mask, "Value"].fillna(0).sum()
                total_val = buy_val + sell_val
                if total_val > 0:
                    buy_ratio = buy_val / total_val
                elif buy_val > 0:
                    buy_ratio = 1.0
                elif sell_val > 0:
                    buy_ratio = 0.0
                else:
                    buy_ratio = 0.5
                result["insider_buy_val"]  = int(buy_val)
                result["insider_sell_val"] = int(sell_val)
                result["insider_buy_ratio"] = round(buy_ratio, 3)
                result["insider_net"] = ("Net Buying" if buy_ratio > 0.6 else
                                         "Mixed"      if buy_ratio > 0.3 else "Net Selling")
                result["insider_score_raw"] = round(buy_ratio * 10, 2)
                result["insider_available"] = True
        except Exception as exc:
            _log.debug("yf_insider_transactions_failed", error=str(exc))

    # ── 4c 52-Week Position (momentum without extension) ─────────────────────
    result["w52_position"] = None
    result["w52_label"] = "N/A"
    try:
        hi_  = fund.get("52w_high")
        lo_  = fund.get("52w_low")
        cur  = fund.get("current_price")
        if hi_ and lo_ and cur and hi_ > lo_:
            pos = (cur - lo_) / (hi_ - lo_)
            result["w52_position"] = round(pos, 3)
            result["w52_label"] = (
                "Near High (extension risk)" if pos > 0.85 else
                "Upper Range (momentum)"     if pos > 0.60 else
                "Mid Range"                  if pos > 0.40 else
                "Lower Range"                if pos > 0.20 else "Near Low (reversal?)")
    except Exception as exc:
        _log.debug("w52_position_calc_failed", error=str(exc))

    # ── 4d Short Interest (US only) ───────────────────────────────────────────
    # Boehmer, Huszar, Jordan (2010): stocks with LOW short interest (days-to-cover
    # ratio) earn extraordinary positive abnormal returns. High short interest signals
    # informed bearish conviction. SHORT RATIO = sharesShort / avg daily volume.
    # Source: yfinance Ticker.info["shortRatio"] — only valid for US via quoteSummary.
    result["short_ratio"]       = None
    result["short_pct_float"]   = None
    result["short_label"]       = "N/A"
    result["short_score_raw"]   = 5.0   # neutral default
    result["short_available"]   = False
    if is_us:
        try:
            info = _pf_get(prefetched, "info", lambda: ticker_obj.info) or {}
            sr   = info.get("shortRatio")        # days to cover
            spf  = info.get("shortPercentOfFloat")  # fraction e.g. 0.04 = 4%
            if sr is not None and sr >= 0:
                result["short_ratio"] = round(float(sr), 2)
            if spf is not None and spf >= 0:
                result["short_pct_float"] = round(float(spf) * 100, 2)  # convert to %
            # Score: low days-to-cover = bullish; high = bearish
            # Academic thresholds from Boehmer 2010 calibrated to decile breakpoints:
            # <1.0d  → nearly no shorts → very bullish signal (score 9.0)
            # 1-3d   → low short interest → bullish (score 7.5)
            # 3-7d   → neutral (score 5.0)
            # 7-15d  → elevated shorts → mild bearish (score 3.0)
            # >15d   → high short squeeze risk from bearish side → score 1.5
            if result["short_ratio"] is not None:
                result["short_available"] = True
                sr_v = result["short_ratio"]
                if   sr_v < 1.0:  result["short_score_raw"] = 9.0
                elif sr_v < 3.0:  result["short_score_raw"] = 7.5
                elif sr_v < 7.0:  result["short_score_raw"] = 5.0
                elif sr_v < 15.0: result["short_score_raw"] = 3.0
                else:              result["short_score_raw"] = 1.5
                result["short_label"] = (
                    "Very Low SI"  if sr_v < 1.0  else
                    "Low SI"       if sr_v < 3.0  else
                    "Moderate SI"  if sr_v < 7.0  else
                    "High SI"      if sr_v < 15.0 else "Very High SI")
        except Exception as exc:
            _log.debug("yf_short_interest_failed", error=str(exc))

    return result

def score_market_internals(internals: dict, yc: dict = None,
                           vix_level: float = None) -> dict:
    internals = internals or {}
    # Sanitize vendor numbers before any thresholds, weights or availability flags.
    # Preserve L4's existing conservative coverage shrinkage until calibrated.
    pcr = _score_number(internals.get("pcr"), low=0)
    insider_raw = _score_number(internals.get("insider_score_raw"), low=0, high=10)
    pos = _score_number(internals.get("w52_position"), low=0, high=1)
    spread = _score_number((yc or {}).get("spread_bps"))
    vix_level = _score_number(vix_level, low=0)
    vix3m_vix = _score_number(internals.get("vix3m_vix_ratio"), low=0)
    raw_sector_score = _score_number(internals.get("sector_etf_score"), low=0, high=10)
    short_raw = _score_number(internals.get("short_score_raw"), low=0, high=10)
    # ── 4a PCR: <0.5=10, 0.5-0.8=8, 0.8-1.2=5, 1.2-1.5=3, >1.5=1 ──
    if pcr is None:
        s_pcr = 5.0
    elif pcr < 0.5:  s_pcr = 10.0
    elif pcr < 0.8:  s_pcr = 8.0
    elif pcr < 1.2:  s_pcr = 5.0
    elif pcr < 1.5:  s_pcr = 3.0
    else:             s_pcr = 1.0

    # ── 4b Insider score 0–10 from buy_ratio ──
    s_insider = insider_raw if insider_raw is not None else 5.0

    # ── 4c 52W position: momentum stocks near highs continue, buy-the-dip at 30-50% ──
    if pos is None:
        s_w52 = 5.0
    elif 0.50 <= pos <= 0.85:  s_w52 = 10.0  # momentum sweet spot
    elif 0.85 < pos <= 0.95:   s_w52 = 8.0   # strong trend, slight extension
    elif pos > 0.95:            s_w52 = 5.0   # at ceiling — could break out or reverse
    elif 0.30 <= pos < 0.50:   s_w52 = 7.0   # lower range, breakout potential
    elif 0.15 <= pos < 0.30:   s_w52 = 4.0   # weak but could be base-building
    else:                       s_w52 = 2.0   # near 52W low — falling knife risk

    # ── 4d Yield curve (macro regime) — structural signal ──
    if spread is None:
        s_yc = 5.0   # neutral when data unavailable
    elif spread < -25:
        s_yc = 1.0   # deeply inverted = strong recession risk
    elif spread < 0:
        s_yc = 3.0   # inverted = caution
    elif spread < 25:
        s_yc = 6.0   # flat = mild caution
    elif spread < 75:
        s_yc = 8.0   # healthy steepening
    else:
        s_yc = 10.0  # steep = strong macro backdrop

    # ── 4e VIX market regime — real-time fear/greed indicator ──
    # ^VIX < 20 = buy environment; > 30 = avoid new longs.
    # Acts as a global risk-off proxy even for non-US markets.
    if vix_level is None:
        s_vix = 5.0   # neutral when unavailable
    elif vix_level < 15:   s_vix = 9.0   # extreme calm — very bullish backdrop
    elif vix_level < 20:   s_vix = 8.0   # calm, favorable
    elif vix_level < 25:   s_vix = 6.0   # normal market
    elif vix_level < 30:   s_vix = 4.0   # elevated fear — reduce confidence
    elif vix_level < 40:   s_vix = 2.0   # high fear — risky environment
    else:                  s_vix = 1.0   # crisis (VIX ≥ 40)

    # Combine YC + VIX into macro regime score (VIX is faster/more current)
    s_macro = round(s_vix * 0.60 + s_yc * 0.40, 2)

    # ── 4e+ VIX3M/VIX term structure ratio (contango vs backwardation)
    # VIX3M/VIX > 1.0 = contango = normal, calm markets
    # VIX3M/VIX < 1.0 = backwardation = fear, short-term risk exceeds long-term
    # This is a leading indicator — backwardation often precedes sell-offs
    if vix3m_vix is not None:
        if   vix3m_vix > 1.15:   s_vix_ts = 9.0   # strong contango — very bullish backdrop
        elif vix3m_vix > 1.05:   s_vix_ts = 8.0   # normal contango — favorable
        elif vix3m_vix > 0.98:   s_vix_ts = 6.0   # flat — neutral
        elif vix3m_vix > 0.90:   s_vix_ts = 3.0   # backwardation — elevated near-term fear
        else:                    s_vix_ts = 1.0   # deep backwardation — risk-off
    else:
        s_vix_ts = None

    # ── 4f Sector ETF momentum (US sectors via SPDR ETFs vs SMA-200) ──────────
    # Non-US symbols (NSE, ASX) have no matching sector ETF → neutral 5.0.
    s_sector_etf = raw_sector_score if raw_sector_score is not None else 5.0

    # ── 4g Short Interest (US only, Boehmer 2010) ─────────────────────────────
    # Low short interest = extraordinary positive abnormal returns.
    # Non-US stocks default to neutral (5.0) since data is unavailable.
    s_short = short_raw if short_raw is not None else 5.0

    # Weights: PCR 24%, Insider 18%, 52W 18%, Macro(VIX+YC) 18%, SectorETF 10%, ShortInt 12%
    # With VIX term structure: PCR 22%, Insider 16%, 52W 16%, Macro 16%, SectorETF 9%, ShortInt 11%, VIX_TS 10%
    # If VIX_TS unavailable, fall back to original weights
    pcr_available = bool(internals.get("pcr_available", pcr is not None)) and pcr is not None
    insider_available = bool(internals.get(
        "insider_available", "insider_score_raw" in internals
    )) and insider_raw is not None
    w52_available = pos is not None
    macro_available = vix_level is not None or spread is not None
    sector_available = raw_sector_score is not None
    short_available = bool(internals.get(
        "short_available", internals.get("short_ratio") is not None
    )) and short_raw is not None

    if s_vix_ts is not None:
        raw_total = round(
            s_pcr        * 0.22 +
            s_insider    * 0.16 +
            s_w52        * 0.16 +
            s_macro      * 0.16 +
            s_sector_etf * 0.09 +
            s_short      * 0.11 +
            s_vix_ts     * 0.10,
            2)
        observed_weight = (
            (0.22 if pcr_available else 0.0) +
            (0.16 if insider_available else 0.0) +
            (0.16 if w52_available else 0.0) +
            (0.16 if macro_available else 0.0) +
            (0.09 if sector_available else 0.0) +
            (0.11 if short_available else 0.0) +
            0.10
        )
    else:
        raw_total = round(
            s_pcr        * 0.24 +
            s_insider    * 0.18 +
            s_w52        * 0.18 +
            s_macro      * 0.18 +
            s_sector_etf * 0.10 +
            s_short      * 0.12,
            2)
        observed_weight = (
            (0.24 if pcr_available else 0.0) +
            (0.18 if insider_available else 0.0) +
            (0.18 if w52_available else 0.0) +
            (0.18 if macro_available else 0.0) +
            (0.10 if sector_available else 0.0) +
            (0.12 if short_available else 0.0)
        )
    coverage = round(min(1.0, max(0.0, observed_weight)), 3)
    total = round(5.0 + (raw_total - 5.0) * coverage, 2)
    return {"total": total, "raw_total": raw_total, "coverage": coverage,
            "pcr": s_pcr, "insider": s_insider, "w52": s_w52,
            "yc": s_yc, "vix": s_vix, "macro": s_macro, "sector_etf": s_sector_etf,
            "short_interest": s_short, "vix_term_structure": s_vix_ts}

# ─── COMPOSITE SCORE ────────────────────────────────────────────────────────────
def composite(l1, l2, l3, l4, l5=None, l6=None, l7=None, regime_weights: dict | None = None) -> float:
    """Weighted composite.
    L1–L4 baseline: 0.30 / 0.30 / 0.20 / 0.20
    L3 reduced from 0.30→0.20 (VADER sentiment r=+0.24, too noisy at 30%).
    Freed 10% redistributed to L1 (+5%) and L2 (+5%) — stronger alpha signals.
    With L5 (Moomoo):   +10% taken proportionally from L1–L4
    With L6 (IBKR):     +8%  taken proportionally from L1–L4
    L7 (External):      PURE ADDITIVE BONUS — never penalises.
        A stock not in your basket or not mentioned by experts scores the same
        as if L7 didn't exist.  Only positive expert signal (l7 > 5.0) adds
        bonus points — up to +5 on the 0–100 scale (at l7 = 10.0).
        This models conviction: "I will allocate more when experts agree."
    Combinations: L5 and L6 still take weight proportionally from L1–L4.
    regime_weights: Optional dict from regime_detector.get_regime_weights(vix).
        Keys: "l1", "l2", "l3", "l4" (floats summing to 1.0).
        If None (default), uses hardcoded 30/30/20/20."""
    return _composite_components(l1, l2, l3, l4, l5, l6, l7, regime_weights)["stretched"]


def _composite_components(l1, l2, l3, l4, l5=None, l6=None, l7=None,
                          regime_weights: dict | None = None) -> dict:
    """Single source of truth for the composite: the value plus its workings.

    ``composite()`` returns only ``stretched``; the report renders the rest so a
    published score can be reconciled without re-deriving the formula.
    """
    # L5 / L6 reduce the L1-L4 weight (they have their own data sources)
    l5w = 0.10 if l5 is not None else 0.0
    l6w = 0.08 if l6 is not None else 0.0
    base_w = 1.0 - l5w - l6w
    # Distribute base_w proportionally across L1-L4 ratios
    if regime_weights is not None:
        w1 = regime_weights.get("l1", 0.30)
        w2 = regime_weights.get("l2", 0.30)
        w3 = regime_weights.get("l3", 0.20)
        w4 = regime_weights.get("l4", 0.20)
    else:
        w1, w2, w3, w4 = 0.30, 0.30, 0.20, 0.20
    raw = (l1*w1 + l2*w2 + l3*w3 + l4*w4) * base_w
    layers = [
        {"key": "L1", "label": "Technical",   "score": l1, "weight": w1 * base_w},
        {"key": "L2", "label": "Fundamental", "score": l2, "weight": w2 * base_w},
        {"key": "L3", "label": "Sentiment",   "score": l3, "weight": w3 * base_w},
        {"key": "L4", "label": "Internals",   "score": l4, "weight": w4 * base_w},
    ]
    if l5 is not None:
        raw += l5 * l5w
        layers.append({"key": "L5", "label": "Institutional", "score": l5, "weight": l5w})
    if l6 is not None:
        raw += l6 * l6w
        layers.append({"key": "L6", "label": "IBKR", "score": l6, "weight": l6w})
    for layer in layers:
        layer["weight"] = round(layer["weight"], 4)
        layer["contribution"] = round(layer["score"] * layer["weight"] * 10, 2)
    weighted = round(raw * 10, 2)
    # L7: additive bonus only — no penalty for absence.
    # raw is on 0–10 scale (×10 below → 0–100).
    # Max bonus = +0.5 raw units = +5 pts on 100-scale when l7 = 10.0.
    l7_bonus = 0.0
    if l7 is not None and l7 > 5.0:
        bonus_fraction = (l7 - 5.0) / 5.0   # 0.0 at l7=5.0 → 1.0 at l7=10.0
        l7_bonus = bonus_fraction * 0.5       # +0 to +0.5 raw → +0 to +5 pts
        raw += l7_bonus
    linear = min(raw * 10, 100.0)
    # ── Score contrast stretch (widen observed distribution) ──────────────────
    # Sub-scores regress to 5.0 (neutral) when data is missing, compressing the
    # composite range to ~46–76.  A linear stretch around the neutral midpoint
    # (50 = all layers exactly neutral) with k=1.5 corrects this:
    #   old 46 → 44  |  old 62 (typical) → 68  |  old 76 → 89
    # Threshold of 60 now meaningfully separates "above average" from "average".
    spread = round(min(100.0, max(0.0, 50.0 + (linear - 50.0) * 1.5)), 2)
    return {
        "layers": layers,
        "weighted": weighted,
        "l7": l7,
        "l7_bonus": round(l7_bonus * 10, 2),
        "linear": round(linear, 2),
        "stretch_k": 1.5,
        "stretched": spread,
    }


WATCH_MIN_COMPOSITE = 50.0
WATCH_MAX_COMPOSITE = 60.0
SCORE_MODEL_VERSION = "signal-stack-2026.08.19.1"

# Providers report funds/ETFs with null equity fundamentals, so L2 scores them on
# a neutral fallback that reads as an average business instead of missing data.
# Deliberately excludes "investment managers" / "investment banks": those are real
# operating equities (MS, C) with genuine fundamentals.
_FUND_LIKE_INDUSTRIES = {
    "investment trusts/mutual funds",
    "closed end funds",
}

# Confidence scales the decision score instead of hard-gating it. The floor keeps
# a thin-data name rankable while stopping it from outranking a fully-observed one.
CONFIDENCE_DEMOTION_FLOOR = 0.85

# Days-to-earnings inside which a fresh entry is a coin flip on the print.
EARNINGS_BLACKOUT_DAYS = 2


def _is_fund_like(fund: dict | None) -> bool:
    """True for ETFs/trusts, whose equity fundamentals are structurally absent."""
    industry = str((fund or {}).get("industry") or "").strip().lower()
    return industry in _FUND_LIKE_INDUSTRIES


def _compute_decision_scores(
    l1: float,
    l2: float,
    l3: float,
    l4: float,
    institutional_l5: float | None,
    regime_weights: dict | None = None,
    l6: float | None = None,
    l7: float | None = None,
    book_bonus: float = 0.0,
    pool_boost: float = 0.0,
    basket_boost: float = 0.0,
    data_confidence: float | None = None,
) -> tuple[float, float]:
    """Return quality and confidence-adjusted decision scores.

    Quality = weighted L1-L7 composite plus the book-feature bonus.
    Decision = quality after confidence demotion, plus pool/basket conviction
    boosts.

    Signal age no longer scales the score. The multiplier sat on its 0.80 floor
    for every actionable row, so it never reordered anything -- it only shifted
    the gate by a constant. Age is published as context instead.
    """
    quality = round(min(100.0, composite(
        l1, l2, l3, l4,
        l5=institutional_l5,
        l6=l6,
        l7=l7,
        regime_weights=regime_weights,
    ) + book_bonus), 2)
    confidence_factor = 1.0 if data_confidence is None else max(
        CONFIDENCE_DEMOTION_FLOOR, min(1.0, float(data_confidence))
    )
    decision = round(
        min(100.0, max(
            0.0,
            quality * confidence_factor + pool_boost + basket_boost,
        )),
        2,
    )
    return quality, decision


def _score_breakdown(
    l1: float, l2: float, l3: float, l4: float,
    institutional_l5: float | None,
    age_hours: float | None = None,
    regime_weights: dict | None = None,
    l6: float | None = None,
    l7: float | None = None,
    book_bonus: float = 0.0,
    pool_boost: float = 0.0,
    basket_boost: float = 0.0,
    data_confidence: float | None = None,
) -> dict:
    """Return the ordered arithmetic that turns L1-L7 into the decision score.

    Mirrors ``_compute_decision_scores`` step for step so every published score
    can be reconciled from the report alone.
    """
    parts = _composite_components(l1, l2, l3, l4, institutional_l5, l6, l7, regime_weights)
    quality = round(min(100.0, parts["stretched"] + book_bonus), 2)
    confidence_factor = 1.0 if data_confidence is None else max(
        CONFIDENCE_DEMOTION_FLOOR, min(1.0, float(data_confidence))
    )

    steps: list[dict] = []
    running = parts["weighted"]
    weight_detail = "  ".join(
        f"{lay['key']} {lay['score']:.1f}×{lay['weight'] * 100:.0f}%"
        for lay in parts["layers"]
    )
    steps.append({
        "label": "Weighted layers",
        "detail": weight_detail,
        "op": "=",
        "value": running,
    })
    if parts["l7_bonus"]:
        running = parts["linear"]
        steps.append({
            "label": "L7 conviction bonus",
            "detail": f"expert score {parts['l7']:.2f} > 5.0",
            "op": "+",
            "amount": parts["l7_bonus"],
            "value": running,
        })
    running = parts["stretched"]
    steps.append({
        "label": "Contrast stretch",
        "detail": f"50 + (x − 50) × {parts['stretch_k']}",
        "op": "×",
        "value": running,
    })
    if book_bonus:
        running = quality
        steps.append({
            "label": "Book context",
            "detail": "open-position / watchlist context",
            "op": "+",
            "amount": round(book_bonus, 2),
            "value": running,
        })
    steps.append({"label": "Quality score", "op": "=", "value": quality, "emphasis": True})

    # _compute_decision_scores multiplies the whole post-quality chain in a single
    # unrounded expression and rounds once, so carry full precision and round only
    # for display; rounding each step drifts the published total by ~0.01.
    running = quality
    if age_hours is not None:
        steps.append({
            "label": "Signal age",
            "detail": f"{age_hours:.1f}h weekday-active — context only, not applied",
            "op": "·",
            "note": True,
        })
    if data_confidence is not None and confidence_factor < 0.999:
        running *= confidence_factor
        floored = confidence_factor > float(data_confidence)
        detail = f"×{confidence_factor:.2f} — observed data coverage {float(data_confidence) * 100:.0f}%"
        if floored:
            detail += f" (floored at {CONFIDENCE_DEMOTION_FLOOR:.2f})"
        steps.append({
            "label": "Confidence demotion",
            "detail": detail,
            "op": "×",
            "value": round(running, 2),
        })
    if pool_boost:
        running += pool_boost
        steps.append({
            "label": "Pre-screen pool boost",
            "detail": "in this morning's quality screener pool",
            "op": "+",
            "amount": round(pool_boost, 2),
            "value": round(running, 2),
        })
    if basket_boost:
        running += basket_boost
        steps.append({
            "label": "Smallcase basket boost",
            "detail": "in a subscribed expert basket",
            "op": "+",
            "amount": round(basket_boost, 2),
            "value": round(running, 2),
        })

    decision = round(min(100.0, max(0.0, running)), 2)
    if decision != round(running, 2):
        steps.append({"label": "Clamped to 0-100", "op": "=", "value": decision})
    steps.append({"label": "Decision score", "op": "=", "value": decision, "emphasis": True})

    return {
        "model_version": SCORE_MODEL_VERSION,
        "layers": parts["layers"],
        "steps": steps,
        "quality": quality,
        "decision": decision,
        "age_hours": age_hours,
        "confidence_factor": round(confidence_factor, 4),
    }


def _data_confidence(meta: dict, fund: dict, sent: dict, internals: dict,
                     screener: dict | None = None) -> dict:
    """Return per-layer observed-data coverage without changing the score."""
    sc = screener or {}

    meta, fund, sent, internals = (meta or {}), (fund or {}), (sent or {}), (internals or {})

    def coverage(values: list[object]) -> float:
        return round(sum(_score_observed(value) for value in values) / len(values), 3)

    rsi = _score_number(meta.get("rsi"), low=0, high=100)
    adx = _score_number(meta.get("ADX"), low=0, high=100)
    sqz = meta.get("sqzOff")
    sqz_valid = str(sqz).strip().lower() in ("1", "1.0", "true", "0", "0.0", "false")
    l1 = coverage([
        _score_number(meta.get("WinRate"), low=0, high=100),
        _score_number(meta.get("NetProfit%")),
        rsi if rsi is not None else _score_number(sc.get("tv_rsi"), low=0, high=100),
        adx if adx is not None else _score_number(sc.get("tv_adx"), low=0, high=100),
        True if sqz_valid else None,
        sc.get("rel_vol"), sc.get("sma200_pct"),
        sc.get("macd_hist"), sc.get("bb_width"),
    ])
    l2 = coverage([
        fund.get("forward_pe"), fund.get("roe"), fund.get("profit_margin"),
        fund.get("earnings_growth"), fund.get("debt_to_equity"),
        fund.get("free_cashflow"),
    ])
    analyst = sent.get("analyst") or {}
    earnings = sent.get("earnings_beats") or {}
    news = sent.get("news") or {}
    br = _score_number(analyst.get("bull_ratio"), low=0, high=2)
    count = _score_number(analyst.get("total"), low=0)
    analyst_observed = br is not None and (
        (count is not None and count > 0) or analyst.get("source") == "TV"
    )
    l3 = coverage([
        br if analyst_observed else None,
        earnings.get("beat_rate") if (_score_number(earnings.get("quarters"), low=0) or 0) > 0 else None,
        news.get("score") if (_score_number(news.get("count"), low=0) or 0) > 0 else None,
        sent.get("earnings_days_away"),
    ])
    l4 = coverage([
        internals.get("pcr"), internals.get("insider_buy_ratio"),
        internals.get("w52_position"), internals.get("short_ratio"),
        internals.get("sector_etf_score"), internals.get("vix_level"),
        internals.get("yc_spread"), internals.get("vix3m_vix_ratio"),
    ])
    overall = round(l1 * 0.35 + l2 * 0.25 + l3 * 0.20 + l4 * 0.20, 3)
    return {"overall": overall, "l1": l1, "l2": l2, "l3": l3, "l4": l4}


def _actionability_reasons(
    item: dict,
    threshold: float,
    require_trigger: bool = False,
    require_buy_flag: bool = False,
) -> list[str]:
    """Return hard reasons a ranked item is not eligible for action.

    Score quality is carried by the decision score; L1 strength and data
    confidence demote rather than block. The remaining vetoes are situational,
    not quality judgements, and fire only when the data needed to evaluate them
    is present, so a sparse item is never blocked by silence.

    The trigger and BUY-flag vetoes are opt-in: both read the strategy's
    instantaneous state rather than the signal, so they reject names on
    sub-1% drift hours after a valid fire.
    """
    reasons: list[str] = []
    score = float(item.get("composite", 0) or 0)
    if score < threshold:
        reasons.append(f"decision score {score:.1f} < {threshold:.1f}")

    fund = item.get("fundamentals") or {}
    meta = item.get("meta") or {}

    if _is_fund_like(fund):
        reasons.append("ETF/fund — equity fundamentals not applicable")

    price = fund.get("current_price")
    target = fund.get("analyst_target")
    if price is not None and target:
        if float(price) >= float(target):
            reasons.append(
                f"price {float(price):.2f} at/above consensus target {float(target):.2f}"
            )

    trigger = meta.get("trigger_price")
    if require_trigger and price is not None and trigger:
        if float(price) < float(trigger):
            reasons.append(
                f"below trigger {float(trigger):.2f} (last {float(price):.2f})"
            )

    if require_buy_flag and str(meta.get("BUY", "")).strip().lower() == "false":
        reasons.append("strategy BUY flag is false")

    earnings_days = (item.get("sentiment_data") or {}).get("earnings_days_away")
    if earnings_days is not None and 0 <= float(earnings_days) <= EARNINGS_BLACKOUT_DAYS:
        reasons.append(f"earnings in {float(earnings_days):.0f}d")

    return reasons


def _compute_tradable_with_limitations(tv_symbol: str, tradability_filter) -> bool:
    """
    Return True if tv_symbol is tradable ONLY via the Moomoo-SG UI (Forex/Crypto),
    with no API execution available -- drives the "Moomoo-SG (manual)" HTML badge.

    tradability_filter: a TradabilityFilter instance (or None if unavailable --
    returns False in that case, matching the pre-2026-07-06 always-False behavior
    for callers without the module installed).
    """
    if tradability_filter is None:
        return False
    try:
        return bool(tradability_filter.check(tv_symbol).tradable_with_limitations)
    except Exception:
        return False


def _select_actionable_report_items(
    ranked: list,
    threshold: float,
    require_trigger: bool = False,
    require_buy_flag: bool = False,
) -> tuple[list, bool]:
    """Return real actionables, or WATCH candidates if actionables are empty.

    Eligibility must match the per-symbol gate policy. Each ranked item carries
    an authoritative ``actionable`` flag computed with the same threshold and
    strict options; that flag is preferred. When it is absent (e.g. a caller
    passing raw rows), eligibility is recomputed with the propagated gate
    policy rather than silently disabling the strict options.
    """
    def _is_actionable(item: dict) -> bool:
        if "actionable" in item:
            return bool(item["actionable"])
        return not _actionability_reasons(
            item, threshold,
            require_trigger=require_trigger,
            require_buy_flag=require_buy_flag,
        )

    actionable = [r for r in ranked if _is_actionable(r)]
    if actionable:
        return actionable, False

    watch: list = []
    watch_lower = min(WATCH_MIN_COMPOSITE, threshold - 10.0)  # compute once outside loop
    for item in ranked:
        try:
            score = float(item.get("composite", 0))
        except (TypeError, ValueError):
            continue
        if score >= watch_lower:
            watch.append({**item, "_watch_candidate": True})
    return watch, bool(watch)


def build_html(ranked: list, n: int, title: str = "Signal Analysis",
               account_cash: "dict | None" = None,
               forex_data: "list | None" = None,
               coverage: "dict | None" = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cards = ""
    for rank, item in enumerate(ranked, 1):
        fund     = item["fundamentals"]
        meta     = item["meta"]
        sent     = item["sentiment_data"]
        intern   = item["market_internals"]
        l1s      = item["scores"]["l1"]
        l2s      = item["scores"]["l2"]
        l3s      = item["scores"]["l3"]
        l4s      = item["scores"]["l4"]
        comp     = item["composite"]
        g_letter, g_color = grade(comp)

        age_hours = item.get("age_hours")
        calendar_age_hours = item.get("calendar_age_hours")
        if age_hours is not None:
            if age_hours <= 6:
                fresh_cls = "badge-fresh-new"
            elif age_hours <= 24:
                fresh_cls = "badge-fresh-ok"
            else:
                fresh_cls = "badge-fresh-stale"
            fresh_badge = (f'<span class="{fresh_cls}" '
                          f'title="Weekday-active age {age_hours:.1f}h; calendar age '
                          f'{calendar_age_hours:.1f}h — context only, not scored">'
                          f'⏱ {age_hours:.1f}h</span>')
        else:
            fresh_badge = ''

        # News headlines
        news_html = ""
        for h in sent.get("news", {}).get("headlines", [])[:5]:
            nc = vader_compound(h)
            hc = "#4ade80" if nc > 0.05 else ("#f87171" if nc < -0.05 else "#94a3b8")
            news_html += f'<div class="news-item" style="border-left:3px solid {hc}">{_html_escape(str(h)[:110])}</div>'

        # Upgrade detail
        up_html = ""
        for ud in sent.get("upgrade_detail", [])[:3]:
            up_html += f'<div class="up-item">↑ {_html_escape(str(ud))}</div>'

        # Earnings detail
        earn_html = ""
        for eq in sent.get("earnings_beats", {}).get("detail", []):
            earn_html += f'<div class="earn-item">{_html_escape(str(eq))}</div>'

        # 52W bar
        w52 = intern.get("w52_position")
        w52_bar = ""
        if w52 is not None:
            w52_bar = (f'<div class="w52-outer"><div class="w52-inner" style="width:{w52*100:.0f}%"></div>'
                       f'<div class="w52-thumb" style="left:calc({w52*100:.0f}% - 5px)"></div></div>'
                       f'<div class="w52-label">{w52*100:.0f}% of 52W range — {_html_escape(str(intern.get("w52_label", "")))}</div>')

        updown_badge = ""
        if sent.get("recent_upgrade"):
            updown_badge = '<span class="badge-up">↑ Upgraded</span>'
        elif sent.get("recent_downgrade"):
            updown_badge = '<span class="badge-dn">↓ Downgraded</span>'

        earn_risk_color = ("#ef4444" if "High" in sent.get("earnings_risk","") else
                           "#eab308" if "Medium" in sent.get("earnings_risk","") else "#22c55e")

        # Relative volume HTML row
        raw_rv = l1s.get("raw_rv")
        if raw_rv is not None:
            rv_cls  = "green" if raw_rv >= 2.0 else ("red" if raw_rv < 0.7 else "")
            rv_text = f"{raw_rv:.2f}x"
        else:
            rv_cls, rv_text = "", "N/A"
        rv_html = f'<div class="kv"><span>Rel Volume</span><b class="{rv_cls}">{rv_text}</b></div>'

        # SMA200 distance HTML row
        raw_sma = l1s.get("raw_sma200_pct")
        if raw_sma is not None:
            sma_cls  = "green" if raw_sma > 3 else ("red" if raw_sma < 0 else "")
            sma_text = f"{raw_sma:+.1f}%"
        else:
            sma_cls, sma_text = "", "N/A"
        sma_html = f'<div class="kv"><span>SMA200</span><b class="{sma_cls}">{sma_text}</b></div>'

        # MACD histogram HTML row
        raw_macd = l1s.get("raw_macd_hist")
        if raw_macd is not None:
            macd_cls  = "green" if raw_macd > 0 else "red"
            macd_text = f"{raw_macd:+.4f}"
        else:
            macd_cls, macd_text = "", "N/A"
        macd_html = f'<div class="kv"><span>MACD Hist</span><b class="{macd_cls}">{macd_text}</b></div>'

        # Morning pre-screen pool badge
        in_pool     = item.get("in_pool")
        pool_boost  = item.get("pool_boost", 0.0)
        pool_is_fresh = item.get("pool_is_fresh", False)
        comp_raw    = item.get("composite_raw", comp)
        if in_pool is True:
            pool_state = "fresh" if pool_is_fresh else "stale"
            pool_badge = (f'<span class="badge-pool-yes" title="Morning screen context ({pool_state}); not scored">'
                         f'✅ Pre-Screened ({pool_state})</span>')
        elif in_pool is False:
            pool_badge = '<span class="badge-pool-no" title="Not in morning quality screener pool">❌ Not Screened</span>'
        else:
            pool_badge = ''   # no pool file present

        # Tradability badge
        _tradability = item.get("_tradability", {})
        if item.get("_tradable_with_limitations"):
            tradability_badge = (
                '<span class="badge-trad-limit" '
                'title="Tradable via Moomoo-SG (manual only — API not exposed)" '
                'style="background:#7c3aed22;color:#a78bfa;padding:2px 8px;'
                'border-radius:10px;font-size:0.7rem;border:1px solid #7c3aed55">'
                '&#9888;&#xFE0F; Moomoo-SG (manual)</span>'
            )
        else:
            tradability_badge = ''

        # Smallcase basket badge
        sc_in_basket = item.get("sc_in_basket", False)
        sc_boost     = item.get("sc_boost", 0.0)
        if sc_in_basket:
            _sc_baskets = ", ".join(
                (item.get("l7_enrichment") or {}).get("smallcase", {}).get("basket_names", [])
            ) or "basket"
            sc_badge = (f'<span class="badge-sc-yes" title="In Smallcase basket: {_html_escape(_sc_baskets, quote=True)}; preference context, not scored">'
                        f'🧺 Smallcase context</span>')
        else:
            sc_badge = ''

        watch_badge = (
            '<span class="badge-watch" title="Near-actionable candidate: composite 50-59; below buy threshold">'
            'WATCH</span>'
            if item.get("_watch_candidate") else ''
        )
        confidence = (item.get("data_confidence") or {}).get("overall")
        confidence_badge = (
            f'<span style="background:#172554;color:#93c5fd;border:1px solid #1d4ed8;'
            f'border-radius:4px;padding:2px 7px;font-size:10px" '
            f'title="Observed data coverage; neutral defaults do not count">'
            f'Data {confidence * 100:.0f}%</span>'
            if confidence is not None else ''
        )
        gate_reasons = item.get("actionability_reasons") or []
        gate_title = _html_escape(
            "; ".join(str(reason) for reason in gate_reasons), quote=True
        )
        gate_badge = (
            f'<span style="background:#450a0a;color:#fca5a5;border:1px solid #991b1b;'
            f'border-radius:4px;padding:2px 7px;font-size:10px" '
            f'title="{gate_title}">Not actionable</span>'
            if gate_reasons else
            '<span style="background:#052e16;color:#86efac;border:1px solid #166534;'
            'border-radius:4px;padding:2px 7px;font-size:10px">Quality gate passed</span>'
        )

        # ── Score derivation strip ────────────────────────────────────────────
        breakdown = item.get("score_breakdown") or {}
        bd_steps  = breakdown.get("steps") or []
        conf_factor = breakdown.get("confidence_factor", 1.0)
        _eq_parts = [f"Quality {comp_raw:.1f}"]
        if conf_factor < 0.999:
            _eq_parts.append(f"× {conf_factor:.2f} conf")
        if pool_boost:
            _eq_parts.append(f"+ {pool_boost:.0f} pool")
        if sc_boost:
            _eq_parts.append(f"+ {sc_boost:.0f} basket")
        comp_equation = " ".join(_eq_parts)

        if bd_steps:
            _rows = ""
            for step in bd_steps:
                _cls = " bd-emph" if step.get("emphasis") else ""
                _amt = step.get("amount")
                _opcell = f"{_amt:+.2f}" if _amt is not None else step.get("op", "=")
                _val = "" if step.get("note") else f'{step.get("value", 0):.2f}'
                _rows += (
                    f'<div class="bd-row{_cls}">'
                    f'<span class="bd-op">{_opcell}</span>'
                    f'<span class="bd-label">{step.get("label","")}</span>'
                    f'<span class="bd-detail">{step.get("detail","")}</span>'
                    f'<span class="bd-value">{_val}</span>'
                    f'</div>'
                )
            breakdown_html = (
                '<details class="breakdown" open>'
                f'<summary>How this {comp:.1f} was calculated &mdash; {comp_equation}</summary>'
                f'<div class="bd-table">{_rows}</div>'
                '</details>'
            )
        else:
            breakdown_html = ''

        # Short-interest HTML rows (US only; non-US stays neutral/N/A)
        short_ratio = intern.get("short_ratio")
        if short_ratio is not None:
            short_cls = "green" if short_ratio < 3.0 else ("red" if short_ratio >= 7.0 else "")
            short_text = f"{short_ratio:.2f}d"
        else:
            short_cls, short_text = "", "N/A"
        short_label = _html_escape(str(intern.get("short_label", "N/A")))
        short_html = f'<div class="kv"><span>Short Interest</span><b class="{short_cls}">{short_text} — {short_label}</b></div>'
        short_float_html = (
            f'<div class="kv"><span>Short % Float</span><b>{fmt(intern.get("short_pct_float"), "%", 2)}</b></div>'
            if intern.get("short_pct_float") is not None else ""
        )

        # Derive exchange bucket for filter buttons
        _raw_exch = item['tv_symbol'].split(':')[0].upper() if ':' in item['tv_symbol'] else 'US'
        _exch_btn = 'US' if _raw_exch in ('NYSE', 'NASDAQ', 'AMEX', 'BATS') else _raw_exch

        # Derive unit-size bucket for filter buttons
        _bd_item   = item.get('backtest_data') or {}
        _sb_item   = _bd_item.get('size_units')
        _units_btn = 'NA' if _sb_item is None else ('AVOID' if _sb_item == 0 else f'{_sb_item}U')

        # ── Pre-compute VIX / YC / Sector-ETF display rows ───────────────────
        _vix_disp = intern.get("vix_level")
        if _vix_disp is not None:
            _vix_regime = "Fear" if _vix_disp > 30 else ("Volatile" if _vix_disp > 20 else "Normal")
            _vix_cls = "red" if _vix_disp > 30 else ("yellow" if _vix_disp > 20 else "green")
            vix_row = (f'<div class="kv"><span>VIX</span>'
                       f'<b class="{_vix_cls}">{_vix_disp:.1f} ({_vix_regime}) '
                       f'→ {l4s.get("vix", 5.0):.1f}/10</b></div>')
        else:
            vix_row = ""

        _yc_disp = intern.get("yc_spread")
        if _yc_disp is not None:
            _yc_cls = "red" if _yc_disp < 0 else ("yellow" if _yc_disp < 30 else "green")
            yc_row = (f'<div class="kv"><span>Yield Curve</span>'
                      f'<b class="{_yc_cls}">{_yc_disp:.0f} bps '
                      f'→ {l4s.get("yc", 5.0):.1f}/10</b></div>')
        else:
            yc_row = ""

        _etf_t = intern.get("sector_etf_ticker", "")
        _etf_p = intern.get("sector_etf_pct")
        if _etf_t and _etf_p is not None:
            _etf_cls = "green" if _etf_p > 0 else "red"
            _etf_arr = "▲" if _etf_p > 0 else "▼"
            etf_row = (f'<div class="kv"><span>Sector ETF</span>'
                       f'<b class="{_etf_cls}">{_html_escape(str(_etf_t))} {_etf_arr} {_etf_p:+.1f}% vs SMA200 '
                       f'→ {l4s.get("sector_etf", 5.0):.1f}/10</b></div>')
        else:
            etf_row = ""

        # L4 sub-score pills
        _l4_pill_parts = []
        for _plab, _pkey in [("PCR","pcr"),("Ins","insider"),("52W","w52"),
                              ("Mac","macro"),("ETF","sector_etf"),("Shrt","short_interest")]:
            _pv = l4s.get(_pkey, 5.0)
            if _pv is None:
                _pv = 5.0
            _pv = float(_pv)
            _pc = "#22c55e" if _pv >= 7 else ("#eab308" if _pv >= 4 else "#ef4444")
            _l4_pill_parts.append(
                f'<span style="background:{_pc}22;color:{_pc};padding:1px 7px;'
                f'border-radius:10px;font-size:0.7rem;border:1px solid {_pc}55">'
                f'{_plab}:{_pv:.0f}</span>')
        l4_pills = f'<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">{"".join(_l4_pill_parts)}</div>'

        # Per-layer sub-score breakdown strips
        def _mini_scores(layer_dict: dict, keys: list) -> str:
            parts = []
            for _lab, _key in keys:
                _sv = layer_dict.get(_key, 5.0)
                if _sv is None:
                    _sv = 5.0
                _sv = float(_sv)
                _sc = "#22c55e" if _sv >= 7 else ("#eab308" if _sv >= 4 else "#ef4444")
                parts.append(f'<span style="color:{_sc};font-size:0.68rem">{_lab}:{_sv:.0f}</span>')
            return (f'<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:2px;opacity:0.85">'
                    f'{"".join(parts)}</div>')

        l1_sub = _mini_scores(l1s, [("WR","wr"),("RSI","rsi"),("ADX","adx"),
                                     ("Sqz","squeeze"),("RV","rel_vol"),("SMA","sma200"),("MACD","macd")])
        l2_sub = _mini_scores(l2s, [("Val","valuation"),("Prof","profitability"),("Gro","growth"),("Hlth","health")])
        l3_sub = _mini_scores(l3s, [("Ana","analyst"),("Earn","earnings_beat"),("Risk","earnings_risk"),("News","news")])
        l4_sub = _mini_scores(l4s, [("PCR","pcr"),("Ins","insider"),("52W","w52"),
                                     ("Mac","macro"),("ETF","sector_etf"),("Shrt","short_interest")])

        _sector_attr = _html_escape(fund.get('sector') or '')
        _tv_symbol = _html_escape(item['tv_symbol'])
        _company_name = _html_escape(fund.get('company_name', ''))
        _sector_name = _html_escape(fund.get('sector', ''))
        _currency = _html_escape(fund.get('currency', ''))
        _resolution = _html_escape(str(item['resolution']))
        _analyst_consensus = _html_escape(str(sent.get('analyst', {}).get('consensus', 'N/A')))
        _earnings_risk = _html_escape(str(sent.get('earnings_risk', 'Unknown')))
        _earnings_date = _html_escape(str(sent.get('earnings_date_str', 'N/A')))
        _news_label = _html_escape(str(sent.get('news', {}).get('label', 'N/A')))
        _pcr_label = _html_escape(str(intern.get('pcr_label', 'N/A')))
        _insider_net = _html_escape(str(intern.get('insider_net', 'N/A')))

        cards += f"""
<div class="card {'card-top' if rank <= 3 else ''}" data-exchange="{_exch_btn}" data-units="{_units_btn}" data-grade="{g_letter}" data-composite="{comp:.2f}" data-sector="{_sector_attr}">
  <div class="card-header">
    <div class="rank-badge" style="background:{g_color}">{g_letter}</div>
    <div class="card-title">
            <div class="ticker">#{rank} {_tv_symbol}</div>
            <div class="company">{_company_name}</div>
      <div class="tags">
                <span class="tag-sector">{_sector_name}</span>
        <span class="tag-tf">{_resolution}m</span>
        {watch_badge}
                {fresh_badge}
        {updown_badge}
        {pool_badge}
        {sc_badge}
        {confidence_badge}
        {gate_badge}
        {tradability_badge}
      </div>
    </div>
    <div class="composite-block">
      <div class="comp-score" style="color:{g_color}">{comp:.1f}</div>
    <div class="comp-label">Decision / 100</div>
    <div class="comp-boost" title="Full derivation is in the breakdown strip below the layer scores">{comp_equation}</div>
    </div>
  </div>

  <div class="card-body">
    <!-- Layer scores strip -->
    <div class="layers">
      <div class="layer-item">
        <div class="layer-title">Technical</div>
        {bar(l1s['total'])}
        {l1_sub}
      </div>
      <div class="layer-item">
        <div class="layer-title">Fundamental</div>
        {bar(l2s['total'])}
        {l2_sub}
      </div>
      <div class="layer-item">
        <div class="layer-title">Sentiment</div>
        {bar(l3s['total'])}
        {l3_sub}
      </div>
      <div class="layer-item">
        <div class="layer-title">Internals</div>
        {bar(l4s['total'])}
        {l4_sub}
      </div>
    </div>

    {breakdown_html}

    <div class="detail-grid">
      <!-- COL 1: Technical -->
      <div class="col">
        <div class="col-head">⚡ Technical (L1)</div>
        <div class="kv"><span>Win Rate</span><b>{fmt(meta.get('WinRate'), '%', 1)}</b></div>
        <div class="kv"><span>Net Profit%</span><b>{fmt(meta.get('NetProfit%'), '%', 1)}</b></div>
        <div class="kv"><span>RSI</span><b>{fmt(l1s.get('raw_rsi'), '', 1)}</b></div>
        <div class="kv"><span>ADX</span><b>{fmt(l1s.get('raw_adx'), '', 1)}</b></div>
        <div class="kv"><span>Squeeze</span><b>{'✓ Released' if str(meta.get('sqzOff','')).lower() in ('1.0','true') else '⊘ Inactive'}</b></div>
        {rv_html}
        {sma_html}
        {macd_html}
                <div class="kv"><span>Active Age</span><b>{f'{age_hours:.1f}h' if age_hours is not None else 'N/A'}</b></div>
                <div class="kv"><span>Calendar Age</span><b>{f'{calendar_age_hours:.1f}h' if calendar_age_hours is not None else 'N/A'}</b></div>
                <div class="kv"><span>Signal Age</span><b>{f'{age_hours:.1f}h active' if age_hours is not None else 'N/A'}</b></div>
        <div class="kv"><span>Trigger</span><b>{fmt(meta.get('trigger_price'))} {_currency}</b></div>
        <div class="kv"><span>Current</span><b>{fmt(fund.get('current_price'))} {_currency}</b></div>
      </div>

      <!-- COL 2: Fundamental -->
      <div class="col">
        <div class="col-head">📊 Fundamental (L2)</div>
        <div class="kv"><span>Mkt Cap</span><b>{hcap(fund.get('market_cap'))}</b></div>
        <div class="kv"><span>Fwd P/E</span><b>{fmt(fund.get('forward_pe'))}</b></div>
        <div class="kv"><span>PEG</span><b>{fmt(fund.get('peg_ratio'))}</b></div>
        <div class="kv"><span>ROE</span><b class="{'green' if (fund.get('roe') or 0)>15 else 'red' if (fund.get('roe') or 0)<0 else ''}">{fmt(fund.get('roe'), '%')}</b></div>
        <div class="kv"><span>Net Margin</span><b class="{'green' if (fund.get('profit_margin') or 0)>10 else 'red' if (fund.get('profit_margin') or 0)<0 else ''}">{fmt(fund.get('profit_margin'), '%')}</b></div>
        <div class="kv"><span>EPS Growth</span><b class="{'green' if (fund.get('earnings_growth') or 0)>5 else 'red' if (fund.get('earnings_growth') or 0)<0 else ''}">{fmt(fund.get('earnings_growth'), '%')}</b></div>
        <div class="kv"><span>D/E Ratio</span><b>{fmt(fund.get('debt_to_equity'))}</b></div>
      </div>

      <!-- COL 3: Sentiment -->
      <div class="col">
        <div class="col-head">🎯 Sentiment & Analysts (L3)</div>
        <div class="kv"><span>Analyst</span><b>{_analyst_consensus} ({sent.get('analyst',{}).get('total',0)} analysts)</b></div>
        <div class="kv"><span>SB/Buy/Hold</span><b>{sent.get('analyst',{}).get('strongBuy',0)}/{sent.get('analyst',{}).get('buy',0)}/{sent.get('analyst',{}).get('hold',0)}</b></div>
        <div class="kv"><span>Beat Rate</span><b>{fmt(sent.get('earnings_beats',{}).get('beat_rate',0)*100, '%', 0)}</b></div>
        <div class="kv"><span>Earnings</span><b style="color:{earn_risk_color}">{_earnings_risk}</b></div>
        <div class="kv"><span>Earn Date</span><b>{_earnings_date}</b></div>
        <div class="kv"><span>News Tone</span><b>{_news_label} ({sent.get('news',{}).get('score',0):+.3f} {'🔵VADER' if _SENTIMENT_BACKEND=='vader' else '🧠FinBERT'})</b></div>
        {'<div class="kv"><span>🧠 FinBERT Ref</span><b style="color:#7c5cbb">' + f"{sent.get('news',{}).get('finbert_score',None):+.3f}" + '</b></div>' if sent.get('news',{}).get('finbert_score') is not None else ''}
        {'<div class="kv"><span>🔵 VADER Check</span><b style="color:#2266cc">' + f"{sent.get('news',{}).get('vader_score',None):+.3f}" + '</b></div>' if sent.get('news',{}).get('vader_score') is not None else ''}
        <div class="kv"><span>Headlines</span><b>{sent.get('news',{}).get('count',0)} analysed</b></div>
        {f'<div class="upgrades">{up_html}</div>' if up_html else ''}
        {f'<div class="earnings-hist">{earn_html}</div>' if earn_html else ''}
      </div>

      <!-- COL 4: Internals -->
      <div class="col">
        <div class="col-head">🔬 Market Internals (L4)</div>
        <div class="kv"><span>Options PCR</span><b>{fmt(intern.get('pcr'))} — {_pcr_label}</b></div>
        <div class="kv"><span>Insider</span><b>{_insider_net}</b></div>
        {'<div class="kv"><span>Ins Buy$</span><b class="green">'+hcap(intern.get('insider_buy_val'))+'</b></div>' if intern.get('insider_buy_val') else ''}
        {'<div class="kv"><span>Ins Sell$</span><b class="red">'+hcap(intern.get('insider_sell_val'))+'</b></div>' if intern.get('insider_sell_val') else ''}
        {short_html}
        {short_float_html}
        <div class="kv"><span>52W Hi</span><b>{fmt(fund.get('52w_high'))} {_currency}</b></div>
        <div class="kv"><span>52W Lo</span><b>{fmt(fund.get('52w_low'))} {_currency}</b></div>
        {w52_bar}
        {vix_row}
        {yc_row}
        {etf_row}
        {l4_pills}
      </div>
    </div>

    <!-- News block -->
    <div class="news-block">
      <div class="col-head">📰 Recent News Sentiment</div>
      {news_html if news_html else '<div class="news-item" style="border-left:3px solid #475569">No headlines found</div>'}
    </div>

    <!-- L5 Moomoo Institutional Flow -->
    {l5_html_badge(item.get('l5_enrichment') or {})}
    <!-- L5 India Portfolio Context (NSE/BSE) -->
    {india_l5_html_badge(item.get('india_l5_enrichment') or {})}
    <!-- L6 IBKR Enrichment -->
    {l6_html_badge(item.get('l6_enrichment') or {})}
    <!-- L7 External Expert Signals (YouTube + Smallcase + Twitter) -->
    {l7_html_badge(item.get('l7_enrichment') or {})}
    <!-- Backtest Position Sizing -->
    {_backtest_block_html(item.get('backtest_data'))}
    <!-- Book Features -->
    {_book_features_html_badge(item.get('book_features'))}
    <!-- Buy Vehicle Advisor (L8) -->
    {_buy_advisor_html(item.get('buy_advice'))}
    <!-- Options Chain Badge (L8b) -->
    {_options_html_badge(item.get('options_data'))}
    <!-- AI Commentary (L9) -->
    {_ai_commentary_html(item.get('ai_commentary'))}
  </div>
</div>
"""

    tmpl = _jinja_env.get_template("signal_report.html")
    regime_weights = (ranked[0].get("regime_weights") if ranked else None) or {
        "l1": 0.30, "l2": 0.30, "l3": 0.20, "l4": 0.20,
    }
    weight_summary = " / ".join(
        f"{key.upper()} {value * 100:.0f}%" for key, value in regime_weights.items()
    )
    return tmpl.render(
        title=title,
        n=n,
        now=now,
        model_version=SCORE_MODEL_VERSION,
        weight_summary=weight_summary,
        buy_advisor_css=BUY_ADVISOR_CSS,
        fx_panel_css=FX_PANEL_CSS,
        options_badge_css=OPTIONS_BADGE_CSS,
        ai_commentary_css=AI_COMMENTARY_CSS,
        coverage_warning=_build_coverage_warning_html(coverage or {}),
        account_cash_html=_build_account_cash_html(account_cash or {}),
        forex_html=_build_forex_html(forex_data or []),
        signal_summary_panel=_build_signal_summary_panel(ranked),
        cards=cards,
    )


def _report_signal_row(item: dict, rank: int) -> dict:
    """Return the structured, score-reproducible JSON representation of a row."""
    scores = item.get("scores") or {}
    options_contracts, options_errors = ([], [])
    if item.get("_options_badge_candidate") and item.get("options_data") is not None:
        options_contracts, options_errors = _normalize_badge_data(
            str(item.get("tv_symbol") or ""),
            item.get("composite"),
            item.get("options_data") or {},
        )
    return {
        "rank": rank,
        "symbol": item.get("tv_symbol"),
        "yf_ticker": item.get("yf_ticker"),
        "fire_time": item.get("fire_time"),
        "resolution": item.get("resolution"),
        "run_id": item.get("run_id"),
        "model_version": item.get("score_model_version"),
        "quality_score": round(float(item.get("quality_score", 0) or 0), 2),
        "decision_score": round(float(item.get("composite", 0) or 0), 2),
        "score": round(float(item.get("composite", 0) or 0), 2),
        "l1": round(float((scores.get("l1") or {}).get("total", 0) or 0), 2),
        "l2": round(float((scores.get("l2") or {}).get("total", 0) or 0), 2),
        "l3": round(float((scores.get("l3") or {}).get("total", 0) or 0), 2),
        "l4": round(float((scores.get("l4") or {}).get("total", 0) or 0), 2),
        "scores": scores,
        "freshness": {
            "active_age_hours": item.get("age_hours"),
            "calendar_age_hours": item.get("calendar_age_hours"),
        },
        "regime": {
            "name": item.get("market_regime"),
            "weights": item.get("regime_weights"),
        },
        "data_confidence": item.get("data_confidence"),
        "score_breakdown": item.get("score_breakdown"),
        "actionable": bool(item.get("actionable")),
        "actionability_reasons": item.get("actionability_reasons") or [],
        "meta": item.get("meta") or {},
        "fundamentals": item.get("fundamentals") or {},
        "sentiment_data": item.get("sentiment_data") or {},
        "market_internals": item.get("market_internals") or {},
        "enrichments": {
            "moomoo_l5": item.get("l5_enrichment"),
            "india_portfolio_context": item.get("india_l5_enrichment"),
            "ibkr_l6_context": item.get("l6_enrichment"),
            "external_l7_context": item.get("l7_enrichment"),
        },
        "context": {
            "book": item.get("book_features"),
            "book_score": item.get("book_context_score"),
            "pool_member": item.get("in_pool"),
            "pool_is_fresh": item.get("pool_is_fresh"),
            "pool_age_hours": item.get("pool_age_hours"),
            "smallcase_member": item.get("sc_in_basket"),
            "smallcase_preference_score": item.get("sc_preference_score"),
        },
        "tradability": item.get("_tradability") or {},
        "options_contracts": options_contracts,
        "options_errors": options_errors,
    }


def _embed_agent_data(html: str, comment: str) -> str:
    """Place AGENT-DATA after the doctype so strict HTML parsers stay happy."""
    first_line, separator, remainder = html.partition("\n")
    if first_line.lstrip().lower().startswith("<!doctype html") and separator:
        return f"{first_line}\n{comment}{remainder}"
    return f"{comment}{html}"


# ─── ARCHIVE ─────────────────────────────────────────────────────────────────────
def _rebuild_archive_index(archive_root: Path) -> None:
    """Regenerate archive/index.html — a browsable table of all archived days/runs."""
    days = sorted(
        [d for d in archive_root.iterdir() if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", d.name)],
        reverse=True,
    )
    rows = []
    for day_dir in days:
        files = sorted(day_dir.iterdir(), reverse=True)
        reports     = [f for f in files if f.name.startswith("report_")]
        actionables = [f for f in files if f.name.startswith("actionable_")]
        if not reports and not actionables:
            continue
        # Pair up runs by timestamp suffix
        timestamps = sorted({f.stem.split("_", 1)[1] for f in reports + actionables}, reverse=True)
        for ts in timestamps:
            time_pretty = f"{ts[:2]}:{ts[2:4]}:{ts[4:]}" if len(ts) == 6 else ts
            r_file = day_dir / f"report_{ts}.html"
            a_file = day_dir / f"actionable_{ts}.html"
            r_link = (f'<a href="{day_dir.name}/report_{ts}.html">All signals</a>'
                      if r_file.exists() else "<span style='color:#666'>—</span>")
            a_link = (f'<a href="{day_dir.name}/actionable_{ts}.html">Actionable</a>'
                      if a_file.exists() else "<span style='color:#666'>—</span>")
            rows.append(
                f"<tr><td>{day_dir.name}</td><td>{time_pretty}</td>"
                f"<td>{r_link}</td><td>{a_link}</td></tr>"
            )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Stack — Archive Index</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0f172a; color:#e2e8f0; padding:2rem; }}
  h1 {{ color:#f8fafc; margin-bottom:1.5rem; font-size:1.5rem; }}
  table {{ border-collapse:collapse; width:100%; max-width:700px; }}
  th {{ background:#1e293b; color:#94a3b8; text-align:left; padding:.6rem 1rem; font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; }}
  td {{ padding:.55rem 1rem; border-bottom:1px solid #1e293b; font-size:.875rem; }}
  tr:hover td {{ background:#1e293b; }}
  a {{ color:#38bdf8; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .empty {{ color:#64748b; font-style:italic; padding:2rem 0; }}
</style>
</head>
<body>
<h1>📦 Signal Stack — Archive</h1>
<p style="color:#64748b;font-size:.85rem;margin-bottom:1.5rem">
  All runs, most recent first. Times are local machine time.
</p>
<table>
  <thead><tr><th>Date</th><th>Time</th><th>All Signals</th><th>Actionable</th></tr></thead>
  <tbody>
{"".join(rows) if rows else '<tr><td colspan="4" class="empty">No archived reports yet.</td></tr>'}
  </tbody>
</table>
</body>
</html>"""
    (archive_root / "index.html").write_text(html, encoding="utf-8")


def _archive_reports(out_path: Path, actionable_path: Path) -> None:
    """
    Copy the freshly-written reports into archive/YYYY-MM-DD/ with an HHMMSS timestamp.
    Keeps full run history; daily folders keep the listing clean.
    Also regenerates archive/index.html.
    Archives both HTML and JSON sidecar files.
    """
    try:
        _rdb = str(out_path.parent)
        _ensure_paths(_rdb)
        from report_db import archive_report as _arc
        now     = datetime.now()
        day_str = now.strftime("%Y-%m-%d")
        archived = []
        if dest := _arc(out_path, "report"):
            archived.append(dest.name)
        # Archive JSON sidecar for all-signals report
        json_out = out_path.with_suffix(".json")
        if json_out.exists():
            if dest := _arc(json_out, "report"):
                archived.append(dest.name)
        if dest := _arc(actionable_path, "actionable"):
            archived.append(dest.name)
        # Archive JSON sidecar for actionable report
        json_act = actionable_path.with_suffix(".json")
        if json_act.exists():
            if dest := _arc(json_act, "actionable"):
                archived.append(dest.name)
        if archived:
            print(f"📦 Archived  → archive/{day_str}/  ({', '.join(archived)})")
    except Exception:
        # Fallback: direct copy so archiving is never silently skipped
        now      = datetime.now()
        day_str  = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")
        archive_day = out_path.parent / "archive" / day_str
        archive_day.mkdir(parents=True, exist_ok=True)
        archived = []
        if out_path.exists():
            dest = archive_day / f"report_{time_str}.html"
            shutil.copy2(out_path, dest)
            archived.append(dest.name)
        # Fallback: JSON for all-signals
        json_out = out_path.with_suffix(".json")
        if json_out.exists():
            dest = archive_day / f"report_{time_str}.json"
            shutil.copy2(json_out, dest)
            archived.append(dest.name)
        if actionable_path.exists():
            dest = archive_day / f"actionable_{time_str}.html"
            shutil.copy2(actionable_path, dest)
            archived.append(dest.name)
        # Fallback: JSON for actionable
        json_act = actionable_path.with_suffix(".json")
        if json_act.exists():
            dest = archive_day / f"actionable_{time_str}.json"
            shutil.copy2(json_act, dest)
            archived.append(dest.name)
        if archived:
            print(f"📦 Archived  → archive/{day_str}/  ({', '.join(archived)})")

    _rebuild_archive_index(out_path.parent / "archive")

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def _fire_outcome_measurement(fire: dict, analysis_at: str) -> dict:
    """Return verified entry provenance, or a reasoned unknown/invalid record."""
    import math as _math
    import re as _re
    from datetime import datetime as _dt

    def _state(status, reason):
        return {
            "provenance_status": status,
            "provenance_reason": reason,
            "origin": "fire",
            "analysis_at": analysis_at,
        }

    source = str(fire.get("_source") or "download").strip().lower()
    if source not in {"download", "webhook"}:
        return _state("unknown", f"source {source or 'missing'} has no trusted entry price contract")
    if fire.get("fire_id") in (None, ""):
        return _state("invalid", "fire_id is missing")

    try:
        analysis_dt = _dt.fromisoformat(str(analysis_at).replace("Z", "+00:00"))
        fire_dt = _dt.fromisoformat(str(fire.get("fire_time") or "").replace("Z", "+00:00"))
        if analysis_dt.tzinfo is None or analysis_dt.utcoffset() is None:
            return _state("invalid", "analysis_at must be timezone-aware")
        if fire_dt.tzinfo is None or fire_dt.utcoffset() is None:
            return _state("invalid", "fire_time must be timezone-aware")
    except (TypeError, ValueError):
        return _state("invalid", "fire_time or analysis_at is malformed")

    structured = []
    for price_key, time_key, label in (
        ("entry_price", "entry_price_at", "structured_entry_price"),
        ("price", "bar_time", "structured_bar_price"),
        ("close", "bar_time", "structured_bar_close"),
    ):
        if fire.get(price_key) is not None and fire.get(time_key) is not None:
            structured.append((fire.get(price_key), fire.get(time_key), label))
    message_matches = _re.findall(
        r"\bprice\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        str(fire.get("message") or ""),
        flags=_re.IGNORECASE,
    )
    if len(message_matches) > 1:
        return _state("invalid", "multiple price: values are ambiguous")
    if not structured and not message_matches and _re.search(r"\bprice\s*:", str(fire.get("message") or ""), flags=_re.IGNORECASE):
        return _state("invalid", "price: value is malformed")
    if structured:
        raw_price, raw_time, label = structured[0]
    elif message_matches:
        raw_price, raw_time, label = message_matches[0], fire.get("fire_time"), "tradingview_alert_message_price"
    else:
        return _state("unknown", "no structured or verified message price")

    try:
        price = float(str(raw_price).replace(",", ""))
        if not _math.isfinite(price) or price <= 0:
            return _state("invalid", "entry price must be finite and positive")
        price_dt = _dt.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        if price_dt.tzinfo is None or price_dt.utcoffset() is None:
            return _state("invalid", "entry price timestamp must be timezone-aware")
    except (TypeError, ValueError):
        return _state("invalid", "entry price or timestamp is malformed")

    result = _state("valid", None)
    result.update({
        "entry_price": price,
        "entry_price_source": label,
        "entry_price_at": price_dt.isoformat(),
        "entry_available_at": fire_dt.isoformat(),
    })
    return result


def _scheduler_stock_rows(results: list[dict], *, screener: bool = False,
                          env: dict | None = None) -> list[dict]:
    """Build the scheduler handoff from engine-actionable results only."""
    env = _os.environ if env is None else env
    if screener and str(env.get("SIGNAL_SCANNER_SCOPE") or "production").lower() == "test":
        # Isolated test-scoped scanner runs may score and persist their own
        # evidence, but they never become recipient delivery work.
        return []
    market = str(env.get("SIGNAL_SCANNER_MARKET") or "").upper()
    rows = []
    for item in results:
        if not isinstance(item, dict) or item.get("actionable") is not True:
            continue
        pro_symbol = str(item.get("tv_symbol") or "").strip()
        if not pro_symbol:
            continue
        try:
            score = float(item.get("composite"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or not 0 <= score <= 100:
            continue
        exchange, _, symbol = pro_symbol.partition(":")
        row = {
            "pro_symbol": pro_symbol,
            "exchange": exchange or "US",
            "symbol": symbol or pro_symbol,
            "score": round(score, 1),
            "signal": str(item.get("signal") or "SCR")[:4],
            "fire_time": str(item.get("fire_time") or ""),
            "resolution": str(item.get("resolution") or ""),
            "actionable": True,
        }
        if item.get("instrument_type") is not None:
            row["instrument_type"] = item["instrument_type"]
        if screener and market:
            row.update({
                "source": "scanner", "market": market,
                "session_date": str(env.get("SIGNAL_SCANNER_SESSION_DATE") or ""),
                "timestamp": str(env.get("SIGNAL_SCANNER_TIMESTAMP") or ""),
                "direction": "SELL" if str(item.get("signal") or "").upper().startswith("SELL") else "BUY",
            })
            for key in ("bar_time", "bar_end_time", "bar_interval_minutes", "quote_source",
                        "quote_symbol", "provider_close", "source_price_delta_pct",
                        "freshness_assurance", "configured_provider_delay_minutes"):
                if item.get(key) is not None:
                    row[key] = item[key]
        rows.append(row)
    return rows


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception as exc:
            _log.debug("win32_utf8_reconfigure_failed", error=str(exc))
    # ── Moomoo daemon threads: prevent SDK threads from blocking process exit ──
    try:
        from moomoo import SysConfig
        SysConfig.set_all_thread_daemon(True)
    except ImportError:
        pass
    # Suppress yfinance HTTP 404 noise (ETF fundamentals / unsupported endpoints)
    import logging as _logging
    _logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
    _logging.getLogger("peewee").setLevel(_logging.CRITICAL)
    parser = argparse.ArgumentParser(description="4-Layer signal stack for fired alerts")
    parser.add_argument("--n",       type=int, default=10, help="Number of recent unique fires")
    parser.add_argument("--new-only", action="store_true",
                        help="Score only fires never put through the pipeline before "
                             "(tracked in database/fires_ledger.py). Ignores --n.")
    parser.add_argument("--holdings", action="store_true",
                        help="Score the positions currently held across all brokers "
                             "instead of fired alerts. Ignores --n and --new-only.")
    parser.add_argument("--positions-file", type=str, default=None,
                        help="Positions cache for --holdings "
                             "(default: .cache_global_positions.json written by option 1)")
    parser.add_argument("--out",     type=str, default=str(OUTPUT_HTML))
    parser.add_argument("--actionable-out", type=str, default="",
                        help="Explicit actionable report path for run-scoped handoffs")
    parser.add_argument("--threshold", type=float, default=75.0,
                        help="Decision score >= N written to actionable report (default: 75)")
    parser.add_argument("--require-trigger", action="store_true",
                        help="Veto signals trading below their strategy trigger price "
                             "(off by default: normal drift after a valid fire)")
    parser.add_argument("--require-buy-flag", action="store_true",
                        help="Veto signals whose strategy BUY flag is currently false "
                             "(off by default: reflects live state, not the fire)")
    parser.add_argument("--no-dedup", action="store_true")
    parser.add_argument("--min-market-cap", type=float, default=100.0,
                        help="Minimum market cap in $M USD to include symbol (default: 100). "
                             "Filters OTC penny stocks and micro-caps. Use 0 to disable.")
    parser.add_argument("--no-exchange-filter", action="store_true",
                        help="Disable automatic filtering of crypto/forex/OTC exchanges")
    parser.add_argument("--no-moomoo", action="store_true",
                        help="Disable L5 Moomoo institutional flow layer")
    parser.add_argument("--no-options", action="store_true",
                        help="Disable L8b options chain badge (call/put quotes)")
    parser.add_argument("--options-top-k", type=int, default=20,
                        help="Fetch option badges only for top K quality-gated US/India signals (default: 20)")
    parser.add_argument("--options-open-only", action="store_true",
                        help="Fetch option badges only while the exchange session is open "
                             "(off by default: last-session quotes are still useful)")
    parser.add_argument("--backtest-sizer", action="store_true",
                        help="Enable legacy daily backtest sizing diagnostic. Off by default because "
                             "download fires are 1H/2H and the daily strategy is not comparable.")
    parser.add_argument("--no-india", action="store_true",
                        help="Disable India L5 NSE/BSE portfolio context enrichment")
    parser.add_argument("--ibkr", action="store_true",
                        help="Enable L6 IBKR enrichment (news, fundamentals, PCR via TWS)")
    parser.add_argument("--ibkr-port", type=int, default=7496,
                        help="TWS port for L6 IBKR enrichment (default 7496)")
    parser.add_argument("--no-external", action="store_true",
                        help="Disable L7 external expert signals (YouTube + Smallcase + Twitter)")
    parser.add_argument("--screener", metavar="CLASSES", default=None,
                        help='Fetch fires from TV Screener (not fires_raw.json), e.g. --screener stocks_us  '
                             'or --screener stocks_us,stocks_in. Uses predefined BULLISH filters.')
    parser.add_argument("--news", metavar="DIRECTION", default=None,
                        choices=["bullish", "bearish", "all"],
                        help='Fetch fires from TradingView news screener (bullish|bearish|all).'
                             ' Runs full L1-L7 multi-layer analysis on news-driven signals.')
    parser.add_argument("--austsuper", action="store_true",
                        help='Mode: process only AustralianSuper Member Direct BUY signals '
                             '(source="austsuper_screener") from fires_raw.json. '
                             'Output → signal_report_actionable_austsuper.html.')
    parser.add_argument("--source-filter", metavar="SOURCE", default=None,
                        help='Filter fires_raw.json by source field (e.g. "us_market", "india_market", "sg_market"). '
                             'Output → signal_report_actionable_{SOURCE}.html.')
    parser.add_argument("--news-max-age", type=float, default=24.0,
                        help='Max age in hours for news articles (default 24, used with --news)')
    parser.add_argument("--news-threshold", type=float, default=0.35,
                        help='VADER compound threshold for news signals (default 0.35, used with --news)')
    parser.add_argument("--ai", action="store_true",
                        help="Enable L9 AI commentary via GitHub Models API")
    parser.add_argument("--ai-model", type=str, default="",
                        help="Override AI model (e.g. gpt-4o-mini, gpt-4o, gpt-4.1-nano). Default: use _AI_MODEL constant.")
    parser.add_argument("--ai-top-k", type=int, default=0,
                        help="Only run AI for top K ranked signals (0=all, default 0)")
    parser.add_argument("--ai-workers", type=int, default=5,
                        help="Concurrent workers for AI calls (default 5)")
    parser.add_argument("--morning", action="store_true",
                        help="Non-interactive morning mode: n=50, threshold=50, "
                             "all enrichers enabled, AI enabled. Zero prompts.")
    parser.add_argument("--edgar", action="store_true",
                        help="Enable SEC EDGAR insider activity enrichment (Form 4 filings). "
                             "US-listed symbols only. Adds ~400ms per symbol.")
    parser.add_argument("--no-book-context", action="store_true",
                        help="Disable book-derived VPA/Darvas/RS scoring bonus (requires per-symbol daily history)")
    parser.add_argument("--sentiment", choices=["vader", "finbert", "both"], default="vader",
                        help="Sentiment backend: 'vader' (default, fast), 'finbert' (accurate, ~11s cold start), "
                             "'both' (VADER primary + FinBERT reference score shown side-by-side)")
    parser.add_argument("--insights", action="store_true",
                        help="Research/insights mode: score signals L1-L4 only, print to console. "
                             "NO database write, NO HTML report generated. Safe for ad-hoc research.")
    args   = parser.parse_args()
    _scanner_freshness_rejections = {}
    _scanner_freshness_errors = {}
    _scanner_persistence_failures = {}
    _analysis_run_id = _os.environ.get("SIGNAL_RUN_ID", "").strip() or (
        f"ss_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    )
    if _os.environ.get("SIGNAL_SCANNER_MARKET"):
        try:
            from tradingview.scanner_store import normalize_scope as _normalize_scanner_scope
            _scanner_scope = _normalize_scanner_scope(
                _os.environ.get("SIGNAL_SCANNER_SCOPE", "production")
            )
            _os.environ["SIGNAL_SCANNER_SCOPE"] = _scanner_scope
        except ValueError:
            if not args.insights:
                _write_empty_stock_payload(
                    _analysis_run_id, status="failed",
                    errors=["scanner scope must be production or test"],
                )
            print("  ⚠️ invalid scanner scope; scoring stopped before persistence")
            return
    _analysis_timestamp = datetime.now(timezone.utc).isoformat()
    _scanner_test_scope = bool(_os.environ.get("SIGNAL_SCANNER_MARKET")) and (
        _os.environ.get("SIGNAL_SCANNER_SCOPE") == "test"
    )
    if _scanner_test_scope and not args.insights:
        args.no_options = True
        # Test scans may publish isolated diagnostic artifacts, never ledger or
        # analytics records. Require explicit output isolation before any work.
        _test_root_raw = _os.environ.get("SIGNAL_REPORT_DIR")
        if not _test_root_raw:
            raise ValueError("test scanner requires an isolated SIGNAL_REPORT_DIR")
        _test_root = Path(_test_root_raw).resolve()
        _production_root = Path(__file__).resolve().parent.parent
        if _test_root in {_production_root, _production_root / "reports", _production_root / "database"}:
            raise ValueError("test scanner output directory must be isolated")
        for _output in (args.out, args.actionable_out, _STOCK_PAYLOAD_PATH, _OPTIONS_SNAPSHOT_PATH):
            if not _output or not Path(_output).resolve().is_relative_to(_test_root):
                raise ValueError("test scanner outputs must be inside SIGNAL_REPORT_DIR")
    if args.no_options and not args.insights:
        _publish_options_snapshot(_analysis_run_id, "disabled")
    # --morning overrides: sensible daily-run defaults
    if args.morning:
        args.n = args.n if args.n != 10 else 50          # default override
        args.threshold = args.threshold if args.threshold != 75.0 else 62.5
        args.ai = True
        args.no_external = False
    if args.sentiment == "finbert":
        _set_sentiment_backend("finbert", dual_scores=False)
        print("🧠  FinBERT sentiment engine (--sentiment finbert, 76.7% accuracy, ~11s cold start)")
    elif args.sentiment == "both":
        _set_sentiment_backend("vader", dual_scores=True)
        print("📊  Dual sentiment: 🔵VADER primary + 🧠FinBERT reference (--sentiment both)")
    else:  # vader (default)
        _set_sentiment_backend("vader", dual_scores=False)
        print("⚡  VADER sentiment engine (default, fast — use --sentiment finbert or --sentiment both for FinBERT)")
    use_moomoo    = (not args.no_moomoo) and _MOOMOO_AVAILABLE
    use_india     = (not args.no_india) and _INDIA_AVAILABLE
    use_ibkr      = args.ibkr and _IBKR_AVAILABLE
    use_external  = (not args.no_external) and _EXTERNAL_AVAILABLE
    use_edgar     = getattr(args, "edgar", False)
    use_ai        = args.ai and _AI_AVAILABLE
    _ai_model_override = (args.ai_model or "").strip() if hasattr(args, "ai_model") else ""
    _effective_ai_model = _ai_model_override or _AI_MODEL
    if use_ai:
        _scope = f"top {args.ai_top_k}" if args.ai_top_k and args.ai_top_k > 0 else "all"
        print(f"🤖  AI L9 enabled — {_effective_ai_model} via GitHub Models API ({_scope}, workers={args.ai_workers})")
    elif args.ai and not _AI_AVAILABLE:
        print("⚠️  --ai requested but openai package not installed — L9 skipped")
    if use_ibkr:
        import ibkr_enricher as _ibkr_mod
        _ibkr_mod.IBKR_PORT = args.ibkr_port
        print(f"🏛️  IBKR L6 enabled — TWS port {args.ibkr_port}")

    # ── Load morning screener pool (optional — run morning_screener.py first) ──
    _pool_file = Path(__file__).parent / "screener_pool.json"
    _pool_tickers: set[str] = set()   # base tickers, e.g. {"NVDA", "AAPL"}
    _pool_meta: dict = {}
    _pool_is_fresh = False
    _pool_age_hours: float | None = None
    if _pool_file.exists():
        try:
            _pd = json.loads(_pool_file.read_text(encoding="utf-8"))
            _pool_tickers = {c.split(".")[-1] for c in _pd.get("codes", [])}
            _pool_meta = {"generated_at": _pd.get("generated_at",""), "count": _pd.get("count",0),
                          "thresholds": _pd.get("thresholds","")}
            _pool_age_hours = _timestamp_age_hours(_pool_meta.get("generated_at", ""))
            _pool_is_fresh = _is_pool_fresh(_pool_meta.get("generated_at", ""))
            print(f"📋 Screener pool loaded: {len(_pool_tickers)} stocks  ({(_pool_meta.get('generated_at') or '')[:16]})")
            print(f"   Criteria: {_pool_meta.get('thresholds','')}")
            if not _pool_is_fresh:
                age_label = f"{_pool_age_hours / 24:.1f} days" if _pool_age_hours is not None else "unknown age"
                print(f"   ⚠ Pool is stale ({age_label}); membership shown for context, +3 boost disabled")
        except Exception as _pe:
            print(f"⚠️  Could not load screener pool: {_pe}")
    else:
        print(f"💡 No screener pool found. Run: python morning_screener.py  (optional quality pre-filter)")

    if args.screener:
        # Scanner mode is a standalone source. Never read the WIN fire cache:
        # it may be absent or malformed and must not affect this scan.
        all_fires = []
        print("TradingView scanner mode — fires_raw.json is not used")
    else:
        print(f"Loading {FIRES_FILE} ...")
        if Path(FIRES_FILE).exists():
            with open(FIRES_FILE) as f:
                data = json.load(f)
            all_fires = data.get("all", [])
            print(f"  Total fires: {len(all_fires)}")
        else:
            all_fires = []
            print(f"  (fires_raw.json not found — proceeding in screener mode)")

    _new_only_batch: list = []
    if args.new_only and not all_fires and not args.holdings:
        if not args.insights:
            _publish_options_snapshot(
                _analysis_run_id,
                "disabled" if args.no_options else "no_new_signals",
            )
            _write_empty_stock_payload(_analysis_run_id)
        print("\n  No new alerts since the last run — nothing to score.", flush=True)
        return
    if args.new_only and all_fires and not args.holdings:
        # __main__ ends with os._exit(0), which drops buffered stdout, so every
        # message on a path that returns early has to be flushed explicitly.
        try:
            from database.fires_ledger import unseen_fires as _unseen_fires
        except ImportError as _le:
            print(f"  ⚠️  --new-only needs database/fires_ledger.py ({_le})", flush=True)
            return
        _before = len(all_fires)
        all_fires = _unseen_fires(all_fires)
        print(f"  Never scored before: {len(all_fires)} of {_before}", flush=True)
        if not all_fires:
            if not args.insights:
                _publish_options_snapshot(
                    _analysis_run_id,
                    "disabled" if args.no_options else "no_new_signals",
                )
                _write_empty_stock_payload(_analysis_run_id)
            print("\n  No new alerts since the last run — nothing to score.", flush=True)
            return
        # Preserve eligibility context; being considered is not scoring success.
        # Only fires producing a completed result below may be marked scored.
        _new_only_batch = list(all_fires)
        args.n = len(all_fires)

    # ── Determine which source mode we are in ────────────────────────────────
    _fire_source_mode = "download"   # default: fires_raw.json webhook/download fires

    # ── Screener mode: pull fires from TV Screener instead of fires_raw.json ──
    if args.screener:
        try:
            from input_sources import tv_screener_to_fires as _screener_to_fires
        except ImportError:
            print("  ⚠️  input_sources.py not found — cannot use --screener mode.")
            return
        _classes = [c.strip() for c in args.screener.split(",") if c.strip()]
        print(f"\n  Fetching TV Screener: {_classes}")
        print(f"  Using predefined BULLISH filters: RSI 50–75, ADX ≥ 20\n")
        selected, _screener_meta = _screener_to_fires(
            classes=_classes if _classes else None,
            max_per_class=args.n if _os.environ.get("SIGNAL_SCANNER_MARKET") else None,
        )
        selected = selected[:args.n]
        _scanner_freshness_rejections = {}
        if _os.environ.get("SIGNAL_SCANNER_MARKET"):
            try:
                from tradingview.scheduler.freshness import validate_scanner_fires as _validate_scanner
                _scanner_observed_at = datetime.fromisoformat(
                    str(_os.environ.get("SIGNAL_SCANNER_TIMESTAMP", "")).replace("Z", "+00:00")
                )
                selected, _scanner_freshness_rejections = _validate_scanner(
                    selected,
                    market=_os.environ["SIGNAL_SCANNER_MARKET"],
                    session_date=_os.environ.get("SIGNAL_SCANNER_SESSION_DATE", ""),
                    observed_at=_scanner_observed_at,
                )
                _scanner_freshness_errors = getattr(
                    _scanner_freshness_rejections, "provider_errors", {},
                )
            except Exception as _freshness_error:
                _scanner_freshness_rejections = {"batch": str(_freshness_error)}
                _scanner_freshness_errors = dict(_scanner_freshness_rejections)
                selected = []
        if not selected:
            if not args.insights:
                _publish_options_snapshot(_analysis_run_id, "disabled" if args.no_options else "no_new_signals")
                if _scanner_freshness_errors:
                    _write_empty_stock_payload(
                        _analysis_run_id, status="failed",
                        errors=[
                            "scanner freshness rejected candidate(s): "
                            + "; ".join(
                                f"{symbol}: {reason}"
                                for symbol, reason in sorted(_scanner_freshness_errors.items())
                            )[:500]
                        ], freshness_rejections=_scanner_freshness_rejections,
                    )
                else:
                    _write_empty_stock_payload(
                        _analysis_run_id, freshness_rejections=_scanner_freshness_rejections,
                    )
            print("  (valid empty TradingView screener result)")
            return
        print(f"  Got {len(selected)} screener candidates → running through signal pipeline...\n")
        _fire_source_mode = "screener"

    # ── News mode: pull fires from TradingView news screener ──────────────────
    elif args.news:
        try:
            from tv_news_screener import news_to_fires as _news_to_fires
        except ImportError:
            print("  ⚠️  tv_news_screener.py not found — cannot use --news mode.")
            return
        _direction = args.news   # bullish | bearish | all
        _bull_thr  = args.news_threshold
        _bear_thr  = -abs(args.news_threshold)
        print(f"\n  Fetching TV news signals: direction={_direction}, "
              f"max_age={args.news_max_age}h, threshold=±{_bull_thr}")
        _bull_fires, _bear_fires = _news_to_fires(
            max_age_hours=args.news_max_age,
            bullish_threshold=_bull_thr,
            bearish_threshold=_bear_thr,
        )
        if _direction == "bullish":
            selected = _bull_fires
        elif _direction == "bearish":
            selected = _bear_fires
        else:   # "all"
            selected = _bull_fires + _bear_fires
        selected = selected[:args.n]
        if not selected:
            print("  (no news signals found above threshold — cannot continue)")
            return
        print(f"  Got {len(selected)} news signals "
              f"({len(_bull_fires)} bullish + {len(_bear_fires)} bearish) "
              f"→ running through full signal pipeline...\n")
        _fire_source_mode = "news"

    # ── AustralianSuper mode: filter fires tagged source="austsuper_screener" ──
    elif args.austsuper:
        selected = [
            f for f in all_fires
            if f.get("source") == "austsuper_screener"
        ][:args.n]
        if not selected:
            print("  (no austsuper_screener fires found in fires_raw.json — run option 53 first)")
            return
        print(f"  Found {len(selected)} AustralianSuper fires → running through full pipeline...\n")
        _fire_source_mode = "austsuper"

    # ── Holdings mode: score the positions we already own ────────────────────
    elif args.holdings:
        # os._exit(0) at __main__ drops buffered stdout, so anything printed on a
        # path that returns early has to be flushed explicitly.
        try:
            from input_sources import holdings_to_fires as _holdings_to_fires
        except ImportError:
            print("  ⚠️  input_sources.py not found — cannot use --holdings mode.", flush=True)
            return
        print("\n  Reading live holdings from the global portfolio cache...", flush=True)
        selected, _excluded_holdings = _holdings_to_fires(args.positions_file)
        if _excluded_holdings:
            print(f"  Excluded {len(_excluded_holdings)} position(s) that cannot be scored:", flush=True)
            for _ex in _excluded_holdings:
                print(f"    • {_ex['symbol']} ({_ex['broker']}) — {_ex['reason']}", flush=True)
        if not selected:
            print("  (no scoreable holdings found — run menu option 1 to refresh positions)", flush=True)
            return
        print(f"  Got {len(selected)} unique holdings → running through full pipeline...\n", flush=True)
        _fire_source_mode = "holdings"

    # ── Generic source filter mode (US/India/SG market screeners etc.) ──
    elif args.source_filter:
        _src = args.source_filter
        selected = [f for f in all_fires if f.get("source") == _src][:args.n]
        if not selected:
            print(f"  (no fires with source='{_src}' found — run the market screener first)")
            return
        print(f"  Found {len(selected)} '{_src}' fires → running through full pipeline...\n")
        _fire_source_mode = "source_filter"

    else:
        # Default downloaded-WIN mode: restrict to the configured WIN alerts and
        # actual BUY direction before deduplication and the --n limit. Other
        # source modes (screener/news/holdings/source_filter) keep their own
        # eligibility rules above.
        selected = _select_default_win_fires(
            all_fires, args.n,
            no_dedup=args.no_dedup,
            strict_buy_flag=args.require_buy_flag,
        )

    print(f"\nSelected {len(selected)} fires. Fetching data...\n")

    # ── Batch-fetch TV screener data for all symbols at once ──────────────────
    all_symbols = [f.get("pro_symbol") or f.get("symbol", "") for f in selected]
    print(f"Fetching TV screener (rel_vol) for {len(all_symbols)} symbols ...", flush=True)
    screener_cache = fetch_tv_screener(all_symbols)
    matched = sum(1 for v in screener_cache.values() if v.get("rel_vol") is not None)
    print(f"  Got rel_vol for {matched}/{len(all_symbols)} symbols")

    # ── Moomoo connectivity check (hard-capped at 10s) ────────────────
    # Golden Rule #16: OpenQuoteContext can block indefinitely on TCP connect.
    if _MOOMOO_AVAILABLE:
        try:
            from moomoo_market_tools import get_global_state as _mm_global_state
            import concurrent.futures as _cf_mm
            _mm_ex = _cf_mm.ThreadPoolExecutor(max_workers=1)
            _mm_fut = _mm_ex.submit(_mm_global_state)
            try:
                _gs = _mm_fut.result(timeout=10)
            except _cf_mm.TimeoutError:
                _gs = {}
                print("  Moomoo: ⚠ connectivity check timed out (10s)")
            finally:
                _mm_ex.shutdown(wait=False)
            _qot = "✅" if _gs.get("qot_logined", False) else "⚠️"
            _trd = "✅" if _gs.get("trd_logined", False) else "⚠️"
            print(f"  Moomoo: qot={_qot}  trd={_trd}")
        except Exception as exc:
            _log.debug("moomoo_connectivity_check_failed", error=str(exc))

    # Batch-fetch fundamentals via TV Screener (replaces per-symbol yfinance .info calls)
    # Fixes: ASX 404, ETF missing data, slow sequential fetches
    from tv_screener_fundamentals import (
        fetch_tv_fundamentals      as _tv_fund_fetch,
        tv_fund_to_compat          as _tv_fund_compat,
        fetch_yield_curve,
        fetch_vix_level,
        fetch_vix3m_level,
        fetch_sector_etf_momentum,
        get_sector_etf_detail,
        get_sector_etf_score,
    )
    print(f"Fetching TV Screener fundamentals (L2) for {len(all_symbols)} symbols ...", flush=True)
    tv_fund_cache = _tv_fund_fetch(all_symbols, use_auth=True)
    print(f"  Got fundamentals for {len(tv_fund_cache)}/{len(all_symbols)} symbols")

    # ── Moomoo basicinfo fallback (HK / SG / SH / SZ symbols) ────────────────
    # TV Screener does not cover HK/SG/SH/SZ reliably.
    # fetch_basicinfo() uses get_market_snapshot() to fill PE, lot_size, NAV.
    if _MOOMOO_AVAILABLE:
        try:
            from moomoo_market_tools import fetch_basicinfo as _mm_basicinfo
            from moomoo_enricher import tv_to_moomoo as _tv_to_mm_sym
            import concurrent.futures as _cf_bi
            _hkhk_markets = {"HKEX", "SH", "SZ", "SGX"}
            _mm_missing = [
                s for s in all_symbols
                if s.split(":")[0].upper() in _hkhk_markets
                and not tv_fund_cache.get(s, {}).get("pe_ratio")
            ]
            if _mm_missing:
                _mm_codes = [c for c in (_tv_to_mm_sym(s) for s in _mm_missing) if c]
                # Golden Rule #16: hard timeout on Moomoo call
                _bi_ex = _cf_bi.ThreadPoolExecutor(max_workers=1)
                _bi_fut = _bi_ex.submit(_mm_basicinfo, _mm_codes)
                try:
                    _bi = _bi_fut.result(timeout=20)
                except _cf_bi.TimeoutError:
                    print("  ⚠ Moomoo basicinfo timed out (20s) — skipping HK/SG fills")
                    _bi = {}
                finally:
                    _bi_ex.shutdown(wait=False)
                # Map back to TV symbols and inject into tv_fund_cache
                _mm_to_tv_map = {_tv_to_mm_sym(s): s for s in _mm_missing if _tv_to_mm_sym(s)}
                _injected = 0
                for mm_code, bi_data in _bi.items():
                    tv_sym = _mm_to_tv_map.get(mm_code)
                    if tv_sym and bi_data.get("pe_ttm"):
                        tv_fund_cache.setdefault(tv_sym, {})
                        _entry = tv_fund_cache[tv_sym]
                        _entry.setdefault("pe_ratio",    bi_data["pe_ttm"])
                        _entry.setdefault("pb_ratio",    bi_data["pb"])
                        _entry.setdefault("eps_ttm",     bi_data["eps_ttm"])
                        _entry.setdefault("market_cap",  bi_data["market_val"])
                        _entry.setdefault("lot_size",    bi_data["lot_size"])
                        _entry.setdefault("current_price", bi_data["last_price"])
                        _entry["_moomoo_basicinfo"] = True
                        _injected += 1
                if _injected:
                    print(f"  Moomoo basicinfo injected for {_injected}/{len(_mm_missing)} HK/SG/SH/SZ symbols")
        except Exception as exc:
            _log.debug("moomoo_basicinfo_inject_failed", error=str(exc))

    # ── FRED macro signals (Idea 2): authoritative yield curve + CPI regime ──
    # fred_macro falls back to TV scanner / yfinance when FRED_API_KEY is absent.
    # spread_bps key is compatible with existing score_market_internals() logic.
    try:
        from fred_macro import fetch_macro_signals as _fetch_macro
        yc = _fetch_macro()
    except Exception:
        yc = fetch_yield_curve()
    _spread = yc.get("spread_bps")
    _s_str = f"{_spread:+.0f}bps{'  ⚠️ INVERTED' if _spread < 0 else ''}" if _spread is not None else "N/A"
    _macro_src = yc.get("source", "TV")
    _ff_str = f"  FEDFUNDS={yc.get('fedfunds'):.2f}%" if yc.get("fedfunds") else ""
    _cpi_str = f"  CPI-MoM={yc.get('cpi_mom'):+.3f}%" if yc.get("cpi_mom") else ""
    _curve_tenor = yc.get("curve_tenor") or "curve"
    _short_label = "US2Y" if _curve_tenor == "2s10s" else "US3M"
    _short_value = yc.get("us2y") if _curve_tenor == "2s10s" else yc.get("us3m")
    print(f"  Yield curve {_curve_tenor}: {_s_str}  ({_short_label}={_short_value} US10Y={yc.get('us10y')})  [{_macro_src}]{_ff_str}{_cpi_str}")

    vix_level = fetch_vix_level()
    _vix_str = f"{vix_level:.1f}" if vix_level is not None else "N/A"
    _vix_regime = ("Extreme Calm (<15)" if vix_level and vix_level < 15 else
                   "Calm (<20)"          if vix_level and vix_level < 20 else
                   "Normal (<25)"        if vix_level and vix_level < 25 else
                   "Elevated (<30)"      if vix_level and vix_level < 30 else
                   "High Fear (<40)"     if vix_level and vix_level < 40 else
                   "Crisis (≥40)"        if vix_level else "N/A")
    print(f"  VIX: {_vix_str}  ({_vix_regime})")

    vix3m_level = fetch_vix3m_level()
    vix3m_vix_ratio = (round(vix3m_level / vix_level, 4)
                       if (vix3m_level is not None and vix_level not in (None, 0))
                       else None)
    if vix3m_vix_ratio is not None:
        _ts_regime = ("Contango (favorable)" if vix3m_vix_ratio > 1.05 else
                      "Flat"                 if vix3m_vix_ratio > 0.98 else
                      "Backwardation (risk-off)")
        print(f"  VIX3M/VIX term structure: {vix3m_vix_ratio:.3f}  ({_ts_regime})")
    else:
        print(f"  VIX3M/VIX term structure: N/A")

    sector_etf_scores = fetch_sector_etf_momentum()
    print(f"  Sector ETF momentum: {len(sector_etf_scores)}/11 sectors scored\n")

    # ── Batch RS Rating computation (O'Neil cross-sectional ranking) ─────────
    # Must be done before the per-ticker loop — RS is a relative measure that
    # requires all tickers' price histories at once to assign 1-99 percentiles.
    _rs_ratings: dict = {}
    try:
        from analysis.rs_rating import compute_rs_rating as _crs
        _yf_ticks_for_rs = [
            tv_to_yf(f.get("pro_symbol") or f.get("symbol", ""))
            for f in selected
        ]
        _yf_ticks_for_rs = [t for t in _yf_ticks_for_rs if t]
        if _yf_ticks_for_rs:
            print(f"  Fetching 1y price history for RS Rating ({len(_yf_ticks_for_rs)} tickers)...", flush=True)
            _rs_raw = yf.download(_yf_ticks_for_rs, period="1y", progress=False, auto_adjust=True)
            if not _rs_raw.empty:
                if len(_yf_ticks_for_rs) == 1:
                    _t = _yf_ticks_for_rs[0]
                    _close_df = _rs_raw[["Close"]].rename(columns={"Close": _t})
                else:
                    _close_df = _rs_raw["Close"]
                _histories = {t: _close_df[t].dropna() for t in _close_df.columns if t in _yf_ticks_for_rs}
                _rs_ratings = _crs(_histories)
                print(f"  RS Rating: computed for {len(_rs_ratings)}/{len(_yf_ticks_for_rs)} tickers")
    except Exception as _rse:
        print(f"  (RS Rating batch fetch failed: {_rse})")

    results = []
    _completed_scoring_fires = []
    _unretained_scoring_fires = []
    _scoring_failures = {}
    _filtered_fires = {}
    _skipped_exchange = 0
    _skipped_mcap     = 0
    # Tradability classifier — reused per-symbol below (Forex/Crypto: tradable
    # via Moomoo SG UI only, no API execution). Was previously never wired up,
    # so the "_tradable_with_limitations" HTML badge (Moomoo-SG manual warning)
    # never appeared in any report (fixed 2026-07-06).
    try:
        from tradability_filter import TradabilityFilter as _TradabilityFilter
        _tradability_filter = _TradabilityFilter()
    except Exception as _tfe:
        _log.debug("tradability_filter_unavailable", error=str(_tfe))
        _tradability_filter = None
    for i, fire in enumerate(selected, 1):
        tv_sym   = fire.get("pro_symbol") or fire.get("symbol", "")
        yf_tick  = tv_to_yf(tv_sym)
        meta     = parse_fire_meta(fire.get("message", ""))
        print(f"[{i}/{len(selected)}] {tv_sym} → {yf_tick}", flush=True)

        # ── Exchange filter: skip crypto / forex / OTC / indices ──────────────
        if not args.no_exchange_filter:
            _exch_pfx = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
            if _exch_pfx in _UNTRADEABLE_EXCHANGES:
                print(f"  ⏭  SKIP — untradeable exchange ({_exch_pfx}): not in tradable markets")
                _skipped_exchange += 1
                from database.fires_ledger import fire_key as _filtered_fire_key
                _filtered_fires[_filtered_fire_key(fire)] = f"untradeable exchange: {_exch_pfx}"
                continue

        try:
            ticker_obj = yf.Ticker(yf_tick)

            # For leveraged ETFs / SGX DLCs, resolve to the underlying equity so
            # L2 fundamentals are scored against the underlying (e.g. SGX:Z8TW → NASDAQ:AVGO).
            # resolve_etf_underlying() returns (None, None) for plain equities.
            _underlying_yf, _underlying_tv = resolve_etf_underlying(tv_sym)
            _fund_tv_sym = _underlying_tv if _underlying_tv else tv_sym
            if _underlying_yf and _underlying_yf != "INDEX":
                ticker_obj = yf.Ticker(_underlying_yf)
                print(f"  DLC/ETF → underlying: {tv_sym} → {_fund_tv_sym}", end="  ", flush=True)

            # L2 fundamentals — TV Screener batch (pre-fetched above, no yfinance .info call)
            # Use underlying TV symbol for DLCs/leveraged ETFs so screener data is populated.
            fund         = _tv_fund_compat(
                tv_fund_cache.get(_fund_tv_sym) or tv_fund_cache.get(tv_sym, {}),
                pro_symbol=_fund_tv_sym,
            )
            # Fallback: when TV screener has no data for this stock (e.g. small-caps
            # not in TV's india scanner), use the close price from the fire event
            # and derive currency from the exchange prefix.
            if not fund.get("current_price"):
                fund["current_price"] = meta.get("trigger_price")
            if not fund.get("currency"):
                _exch = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
                fund["currency"] = {"NSE": "INR", "BSE": "INR", "ASX": "AUD",
                                    "SGX": "SGD", "HKEX": "HKD"}.get(_exch, "USD")

            # ── Market-cap floor (TV screener market_cap_basic is in USD) ─────
            _mcap_floor_usd = args.min_market_cap * 1e6
            if _mcap_floor_usd > 0 and not _os.environ.get("SIGNAL_SCANNER_MARKET"):
                _mcap = fund.get("market_cap")
                if _mcap is not None and _mcap < _mcap_floor_usd:
                    _mcap_m = _mcap / 1e6
                    print(f"  ⏭  SKIP — market cap ${_mcap_m:.0f}M < ${args.min_market_cap:.0f}M floor")
                    _skipped_mcap += 1
                    from database.fires_ledger import fire_key as _filtered_fire_key
                    _filtered_fires[_filtered_fire_key(fire)] = "below market-cap floor"
                    continue
                # Note: if _mcap is None, we proceed (unknown ≠ filtered out)

            l2s          = score_fundamental(fund)
            company_name = fund.get("company_name", "") or fund.get("description", "")

            # L1 technical (+ real-time rel_vol from TV screener)
            l1s    = score_technical(meta, screener=screener_cache.get(tv_sym))

            # L3 sentiment + analyst
            # For DLC/leveraged ETFs, use underlying TV symbol so is_us is evaluated
            # against the underlying exchange (e.g. NASDAQ:NVDA not SGX:NVIW).
            _sent_tv_sym = _fund_tv_sym if _underlying_yf else tv_sym
            _prefetched = _prefetch_yf(
                ticker_obj.ticker,
                _yf_has_quotesummary(_sent_tv_sym),
                want_history=not args.no_book_context,
            )
            print(f"  fetching news/analysts ...", end=" ", flush=True)
            sent_data = fetch_sentiment_data(ticker_obj, company_name, _sent_tv_sym,
                                              tv_fund=tv_fund_cache.get(_fund_tv_sym, {}),
                                              prefetched=_prefetched)
            l3s = score_sentiment(sent_data)

            # L4 market internals
            print(f"options/insider ...", end=" ", flush=True)
            int_data = fetch_market_internals(ticker_obj, fund, _sent_tv_sym,
                                              prefetched=_prefetched)
            int_data["sector_etf_score"] = get_sector_etf_score(fund.get("sector"))
            int_data["vix_level"]        = vix_level
            int_data["vix3m_vix_ratio"]  = vix3m_vix_ratio
            # Enrich with displayable sector ETF detail + macro context.
            # get_sector_etf_detail normalizes the raw TV sector name, so the
            # displayed ETF always matches the ETF used for scoring.
            _etf_d = get_sector_etf_detail(fund.get("sector", ""))
            int_data["sector_etf_ticker"] = _etf_d.get("ticker", "")
            int_data["sector_etf_pct"]    = _etf_d.get("pct")
            int_data["yc_spread"]         = (yc or {}).get("spread_bps")
            l4s      = score_market_internals(int_data, yc, vix_level)

            # L5 Moomoo institutional flow (optional)
            l5_enrichment = None
            l5_score_val  = None
            if use_moomoo:
                print(f"moomoo L5 ...", end=" ", flush=True)
                try:
                    mm_code = tv_to_moomoo(tv_sym)
                    from moomoo_enricher import compute_l5_score
                    import concurrent.futures as _cf_l5
                    _l5_ex = _cf_l5.ThreadPoolExecutor(max_workers=1)
                    _l5_fut = _l5_ex.submit(compute_l5_score, mm_code)
                    try:
                        l5_enrichment = _l5_fut.result(timeout=15)
                    except _cf_l5.TimeoutError:
                        print(f"(L5 timeout 15s)", end=" ")
                        l5_enrichment = None
                    finally:
                        _l5_ex.shutdown(wait=False)
                    l5_score_val  = (l5_enrichment or {}).get("l5_score")   # None if no data
                except Exception as le:
                    print(f"(L5 err: {le})", end=" ")

            # L5 India portfolio context (NSE/BSE only, report context — not alpha)
            india_l5_enrichment = None
            india_l5_score_val  = None
            _exch_upper = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
            if use_india and _exch_upper in ("NSE", "BSE", "NSE_DLY", "BSE_DLY"):
                print(f"india L5 ...", end=" ", flush=True)
                try:
                    india_l5_enrichment = _get_india_enricher().enrich(tv_sym)
                    india_l5_score_val  = india_l5_enrichment.get("l5_score")
                except Exception as ie:
                    print(f"(India L5 err: {ie})", end=" ")

            # L6 IBKR enrichment (optional — only US stocks, TWS must be running)
            l6_enrichment = None
            l6_score_val  = None
            if use_ibkr:
                print(f"ibkr L6 ...", end=" ", flush=True)
                try:
                    # IBKR only supports US stocks via this module
                    _exch = tv_sym.split(":")[0] if ":" in tv_sym else ""
                    _base = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
                    if _exch in ("", "NYSE", "NASDAQ", "AMEX", "BATS"):
                        l6_enrichment = compute_l6_score(_base)
                        l6_score_val  = l6_enrichment.get("l6_score")   # None if no data
                except Exception as l6e:
                    print(f"(L6 err: {l6e})", end=" ")

            # L7 External Expert Signals (YouTube + Smallcase + Twitter)
            l7_enrichment = None
            l7_score_val  = None
            if use_external:
                print(f"external L7 ...", end=" ", flush=True)
                try:
                    l7_enrichment = compute_l7_score(tv_sym, yf_tick)
                    l7_score_val  = l7_enrichment.get("l7_score")   # None if all sources failed
                except Exception as l7e:
                    print(f"(L7 err: {l7e})", end=" ")

            # SEC EDGAR Form 4 insider activity (opt-in via --edgar)
            edgar_data = None
            if use_edgar and _EDGAR_OK:
                try:
                    edgar_data = _edgar_enrich(tv_sym.split(":")[-1])
                except Exception as _ee:
                    print(f"(EDGAR err: {_ee})", end=" ")

            # ── Book features (VPA, Darvas, Gap, AM, RS) ──────────────────────
            _bf_bonus = 0  # stays 0 if scoring fails or disabled
            book_features = None
            if not args.no_book_context:
                try:
                    from analysis.book_features_scorer import score_book_features as _sbf
                    _ohlcv = _pf_get(
                        _prefetched, "history",
                        lambda: ticker_obj.history(period="6mo", interval="1d"),
                    )
                    if _ohlcv is not None and len(_ohlcv) >= 20:
                        book_features = _sbf(
                            _ohlcv.tail(100),
                            fund_dict=fund,
                            rs_value=_rs_ratings.get(yf_tick),
                        )
                        _bf_bonus = book_features.get("total_bonus", 0)
                        if _bf_bonus:
                            print(f"  Book={_bf_bonus:+d}", end=" ", flush=True)
                except Exception as _bfe:
                    print(f"(book err: {_bfe})", end=" ")

            # ── Moomoo pre-screen pool check ──────────────────────────────────
            ticker_base = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
            in_pool = ticker_base in _pool_tickers if _pool_tickers else None

            # +3 decision boost for stocks that pass the morning quality screen;
            # a stale pool is shown for context only and earns no boost.
            pool_boost = 3.0 if in_pool and _pool_is_fresh else 0.0

            # Smallcase basket boost (+5 to +10) — direct priority bonus for
            # expert-curated basket stocks, independent of the combined L7 blend,
            # which absent YouTube/Twitter data can otherwise drag below 5.0.
            sc_boost = 0.0
            sc_preference_score = 0.0
            sc_in_basket = False
            if l7_enrichment:
                sc_data = l7_enrichment.get("smallcase", {})
                if sc_data.get("in_subscription"):
                    sc_in_basket = True
                    sc_preference_score = round(float(sc_data.get("smallcase_score", 5.0)), 2)
                    sc_boost = round(min(10.0, max(5.0, sc_preference_score)), 2)

            age_hours = _weekday_active_age_hours(fire.get("fire_time", ""))
            calendar_age_hours = _calendar_age_hours(fire.get("fire_time", ""))
            regime_weights = _get_regime_weights(vix_level)
            confidence = _data_confidence(
                meta, fund, sent_data, int_data, screener_cache.get(tv_sym)
            )
            quality_score, comp = _compute_decision_scores(
                l1s["total"], l2s["total"], l3s["total"], l4s["total"],
                institutional_l5=l5_score_val,
                regime_weights=regime_weights,
                l6=l6_score_val,
                l7=l7_score_val,
                book_bonus=_bf_bonus,
                pool_boost=pool_boost,
                basket_boost=sc_boost,
                data_confidence=confidence.get("overall"),
            )
            score_breakdown = _score_breakdown(
                l1s["total"], l2s["total"], l3s["total"], l4s["total"],
                institutional_l5=l5_score_val,
                age_hours=age_hours,
                regime_weights=regime_weights,
                l6=l6_score_val,
                l7=l7_score_val,
                book_bonus=_bf_bonus,
                pool_boost=pool_boost,
                basket_boost=sc_boost,
                data_confidence=confidence.get("overall"),
            )

            l5_label  = f"  L5={l5_score_val:.1f}" if l5_score_val is not None else ""
            india_l5_label = f"  PortfolioCtx={india_l5_score_val:.1f}" if india_l5_score_val is not None else ""
            l6_label  = f"  L6={l6_score_val:.1f}" if l6_score_val is not None else ""
            l7_label  = f"  L7={l7_score_val:.2f}" if l7_score_val is not None else ""
            pool_label = f"  Pool=✓(+{pool_boost:.0f})" if in_pool else ("  Pool=✗" if in_pool is False else "")
            sc_label  = f"  Smallcase=✓(+{sc_boost:.0f})" if sc_in_basket else ""
            fresh_label = f"  Age={age_hours:.0f}h" if age_hours is not None else ""
            print(f"  Decision={comp:.1f}  Quality={quality_score:.1f}  L1={l1s['total']:.1f}  L2={l2s['total']:.1f}  L3={l3s['total']:.1f}  L4={l4s['total']:.1f}{l5_label}{india_l5_label}{l6_label}{l7_label}{fresh_label}{pool_label}{sc_label}")

            # ── Backtest position-sizing recommendation ───────────────────────
            backtest_data = None
            if args.backtest_sizer and _SIZER_AVAILABLE:
                print(f"  backtest sizer ...", end=" ", flush=True)
                try:
                    backtest_data = _get_position_size(
                        tv_sym, yf_tick,
                        interval="1d", period="5y",
                        use_cache=True,
                    )
                    if backtest_data and not backtest_data.get("error"):
                        _sz = backtest_data["size_units"]
                        _lb = backtest_data["size_label"]
                        print(f"→ {_sz}u ({_lb[:40]})")
                    else:
                        print(f"→ N/A")
                except Exception as _be:
                    print(f"(err: {_be})")

            _res_exch = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
            _tradable_with_limitations = _compute_tradable_with_limitations(tv_sym, _tradability_filter)
            result_item = {
                "tv_symbol":       tv_sym,
                "yf_ticker":       yf_tick,
                "fire_time":       fire.get("fire_time", ""),
                "resolution":      fire.get("resolution", ""),
                "meta":            meta,
                "fundamentals":    fund,
                # Preserve the entry candidate's own scored price so tracker
                # repair after a later scan cannot fall back to today's quote.
                "entry_price":     fund.get("current_price") or fire.get("close"),
                "sentiment_data":  {**sent_data},
                "market_internals":int_data,
                "l5_enrichment":   l5_enrichment,
                "india_l5_enrichment": india_l5_enrichment,
                "l6_enrichment":   l6_enrichment,
                "l7_enrichment":   l7_enrichment,
                "edgar_enrichment": edgar_data,
                "book_features":   book_features,
                "scores":          {"l1": l1s, "l2": l2s, "l3": l3s, "l4": l4s},
                "quality_score":   quality_score,
                "composite":       comp,
                "composite_raw":   quality_score,
                "composite_pre_decay": quality_score,
                "score_breakdown": score_breakdown,
                "score_model_version": SCORE_MODEL_VERSION,
                "run_id":           _analysis_run_id,
                "analysis_timestamp": _analysis_timestamp,
                "market_regime":    _classify_market_regime(vix_level),
                "regime_weights":  regime_weights,
                "data_confidence": confidence,
                "age_hours":       age_hours,
                "calendar_age_hours": calendar_age_hours,
                "in_pool":         in_pool,
                "pool_is_fresh":   _pool_is_fresh,
                "pool_age_hours":  _pool_age_hours,
                "pool_boost":      pool_boost,
                "sc_in_basket":    sc_in_basket,
                "sc_boost":        sc_boost,
                "sc_preference_score": sc_preference_score,
                "book_context_score": _bf_bonus,
                "backtest_data":   backtest_data,
                "near_earnings":   (lambda _d: 999 if _d is None else _d)(sent_data.get("earnings_days_away")) <= 7,
                # Tradability: True only when exchange is in user's broker routing table.
                # alert_tracker.record_alert() reads this to persist tradable=0/1 in DB,
                # which makes --tradable-only filtering in performance_analyzer work.
                "_tradability":    {"tradable": bool(_TRADABLE_EXCHS) and _res_exch in _TRADABLE_EXCHS},
                # Forex/Crypto: tradable via Moomoo SG UI only, no API execution.
                # Drives the "⚠️ Moomoo-SG (manual)" HTML badge (was always dead
                # before 2026-07-06 — this key was read but never set).
                "_tradable_with_limitations": _tradable_with_limitations,
            }
            if args.screener and _os.environ.get("SIGNAL_SCANNER_MARKET"):
                _screener_data = fire.get("_screener_data") or {}
                if str(_screener_data.get("type") or "").strip().lower() == "stock":
                    result_item["instrument_type"] = "equity"
                result_item.update({
                    "source": "scanner",
                    "market": _os.environ["SIGNAL_SCANNER_MARKET"].upper(),
                    "scope": _os.environ.get("SIGNAL_SCANNER_SCOPE", "production"),
                    "session_date": _os.environ.get("SIGNAL_SCANNER_SESSION_DATE", ""),
                    "timestamp": _os.environ.get("SIGNAL_SCANNER_TIMESTAMP", _analysis_timestamp),
                    "direction": "BUY",
                })
                # Provider provenance is carried only when supplied by the
                # screener/freshness preflight. Never infer bar time from scan time.
                for _field in ("bar_time", "bar_end_time", "bar_interval_minutes", "quote_source",
                               "quote_symbol", "provider_close", "source_price_delta_pct",
                               "freshness_assurance", "configured_provider_delay_minutes"):
                    if fire.get(_field) is not None:
                        result_item[_field] = fire[_field]
            result_item["actionability_reasons"] = _actionability_reasons(
                result_item, args.threshold,
                require_trigger=args.require_trigger,
                require_buy_flag=args.require_buy_flag,
            )
            result_item["actionable"] = not result_item["actionability_reasons"]
            results.append(result_item)
            # Completion means scoring produced a result, not actionability or
            # successful downstream tracker persistence (which is independent).
            #
            # A fire is only added to `_completed_scoring_fires` once its
            # recoverable payload has been durably retained. Otherwise a later
            # batch `persist_run` + `mark_scored` would suppress the fire while
            # no scoring payload exists to recover its notification from.
            _durable_result = False
            if not args.insights and not _scanner_test_scope:
                try:
                    if result_item.get("source") == "scanner":
                        from database.fires_ledger import _conn as _inc_conn
                        from tradingview.scanner_store import record_observation as _record_scanner
                        with _inc_conn() as _inc_db:
                            _scanner_setup_id, _scanner_setup_created, _scanner_first_result = _record_scanner(
                                _inc_db, result_item, run_id=_analysis_run_id,
                                scope=_os.environ.get("SIGNAL_SCANNER_SCOPE", "production"),
                            )
                        # Keep the first scored entry stable for tracker repair.
                        # record_alert's natural UUID then inserts once even if a
                        # process crashed after setup persistence.
                        fire["_scanner_setup_id"] = _scanner_setup_id
                        fire["_scanner_first_result"] = _scanner_first_result
                    else:
                        from database.fires_ledger import _conn as _inc_conn
                        from tradingview.outcome_store import persist_completed as _persist_done
                        with _inc_conn() as _inc_db:
                            # The recoverable payload and the completion evidence are
                            # written in the same transaction, so completion can never
                            # suppress a fire whose result was not retained.
                            _persist_done(_inc_db, fire, _analysis_run_id, result_item)
                    _durable_result = True
                except Exception as _ie:
                    _log.debug("attempt_incremental_failed", symbol=tv_sym, error=str(_ie))
                    print(
                        f"  ⚠️  could not retain scoring result for {tv_sym}: {_ie}",
                        flush=True,
                    )
                    if result_item.get("source") == "scanner":
                        _scanner_persistence_failures[tv_sym] = type(_ie).__name__
            elif args.insights:
                # Research-only runs never mutate the ledger.
                _durable_result = True

            if _durable_result:
                if result_item.get("source") == "scanner":
                    result_item["durably_persisted"] = True
                    result_item["scanner_setup_id"] = fire.get("_scanner_setup_id")
                    result_item["run_id"] = _analysis_run_id
                _completed_scoring_fires.append(dict(fire))
            else:
                if result_item.get("source") == "scanner":
                    result_item["durably_persisted"] = False
                _unretained_scoring_fires.append(dict(fire))
                if result_item.get("source") == "scanner":
                    # The scheduler must not act on an actionable score whose
                    # setup and recoverable result were not durably retained.
                    result_item["actionable"] = False
                    result_item["actionability_reasons"] = [
                        *result_item.get("actionability_reasons", []),
                        "scanner setup persistence failed",
                    ]
            _scanner_tracker_allowed = (
                results[-1].get("source") != "scanner"
                or (results[-1].get("actionable") is True
                    and str(_os.environ.get("SIGNAL_SCANNER_SCOPE", "production")).lower() == "production")
            )
            _tracker_result_is_durable = _durable_result or results[-1].get("source") != "scanner"
            if _TRACKER_OK and _tracker_result_is_durable and _scanner_tracker_allowed:
                try:
                    # Determine source: use the fire's own _source if set, otherwise
                    # the mode-level source (screener/news/download).
                    _src = fire.get("_source") or _fire_source_mode
                    # Include parsed meta in fire dict so record_alert can read WinRate/RSI/ADX
                    _fire_for_tracker = {**fire, "_source": _src, "meta": meta}
                    _tracker_result = results[-1]
                    if results[-1].get("source") == "scanner":
                        _first = fire.get("_scanner_first_result") or results[-1]
                        _fire_for_tracker["fire_time"] = _first.get("fire_time") or fire.get("fire_time")
                        _fire_for_tracker["close"] = _first.get("entry_price") or fire.get("close")
                        _fire_for_tracker["_scanner_setup_id"] = fire.get("_scanner_setup_id")
                        _tracker_result = _first
                        _tracker_result = {**_tracker_result, "_scanner_setup_id": fire.get("_scanner_setup_id")}
                    _result_for_tracker = {
                        **_tracker_result,
                        "actionable_at": _tracker_result["actionable"],
                        "threshold_used": args.threshold,
                        "input_source": _src,
                    }
                    if results[-1].get("source") == "scanner":
                        _result_for_tracker["_scanner_setup_id"] = fire.get("_scanner_setup_id")
                    _om = _fire_outcome_measurement(
                        _fire_for_tracker, _tracker_result.get("analysis_timestamp") or _analysis_timestamp)
                    if _om:
                        _result_for_tracker["outcome_measurement"] = _om
                    if not args.insights:   # --insights mode skips DB write (research only)
                        tracker = _get_tracker()
                        tracker.record_alert(_fire_for_tracker, _result_for_tracker)
                        tracker.record_signal_output(
                            _result_for_tracker,
                            run_id=(
                                _tracker_result.get("run_id") or _analysis_run_id
                                if results[-1].get("source") == "scanner"
                                else _analysis_run_id
                            ),
                        )
                except Exception as exc:
                    _log.debug("alert_tracker_record_failed", symbol=tv_sym, error=str(exc))
        except Exception as e:
            if _os.environ.get("SIGNAL_SCANNER_MARKET") and not args.insights and not _scanner_test_scope:
                try:
                    from database.fires_ledger import _conn as _scanner_conn
                    from tradingview.scanner_store import record_observation as _record_failed_scanner
                    _failed_observation = {
                        "tv_symbol": tv_sym,
                        "market": _os.environ.get("SIGNAL_SCANNER_MARKET", "").upper(),
                        "session_date": _os.environ.get("SIGNAL_SCANNER_SESSION_DATE", ""),
                        "timestamp": _os.environ.get("SIGNAL_SCANNER_TIMESTAMP", ""),
                        "direction": "SELL" if "SELL" in str(fire.get("message", "")).upper() else "BUY",
                        "source": "scanner", "actionable": False,
                        "observation_status": "scoring_failed",
                        "failure_reason": type(e).__name__,
                    }
                    for _field in ("bar_time", "bar_end_time", "bar_interval_minutes", "quote_source",
                                   "quote_symbol", "provider_close", "source_price_delta_pct",
                                   "freshness_assurance", "configured_provider_delay_minutes"):
                        if fire.get(_field) is not None:
                            _failed_observation[_field] = fire[_field]
                    with _scanner_conn() as _scanner_db:
                        _record_failed_scanner(
                            _scanner_db, _failed_observation, run_id=_analysis_run_id,
                            scope=_os.environ.get("SIGNAL_SCANNER_SCOPE", "production"),
                        )
                except Exception as _observation_error:
                    _log.debug("scanner_failure_observation_failed", error=str(_observation_error))
            from database.fires_ledger import fire_key as _attempt_fire_key
            _scoring_failures[_attempt_fire_key(fire)] = str(e) or type(e).__name__
            print(f"\n  ⚠️ ERROR for {tv_sym}: {e}")
            traceback.print_exc()

        time.sleep(0.2)   # be polite to Yahoo Finance

    if _skipped_exchange or _skipped_mcap:
        print(f"\n  🚫 Filtered out: {_skipped_exchange} untradeable-exchange + "
              f"{_skipped_mcap} below-market-cap symbols")

    if not args.insights and (_new_only_batch or selected) and not _os.environ.get("SIGNAL_SCANNER_MARKET"):
        try:
            from database.fires_ledger import _conn as _attempt_conn
            from tradingview.outcome_store import persist_run as _persist_scoring_run
            with _attempt_conn() as _attempt_db:
                # Only fires whose recoverable payload was durably retained may
                # be recorded as COMPLETED. A fire whose incremental persistence
                # failed is recorded as FAILED so it is retried rather than
                # suppressed with no recoverable result.
                _persist_scoring_run(_attempt_db, _new_only_batch or list(selected), selected,
                                     _completed_scoring_fires, _scoring_failures,
                                     _analysis_run_id, filtered=_filtered_fires)
                if _unretained_scoring_fires:
                    from database.fires_ledger import fire_key as _fk
                    from tradingview.outcome_store import persist_attempts as _persist_attempts
                    from tradingview.outcome_logic import (
                        FireAttempt as _FireAttempt,
                        FireDisposition as _FireDisposition,
                        FireIdentity as _FireIdentity,
                    )
                    from tradingview.outcome_store import attempt_id_for as _attempt_id_for
                    from datetime import datetime as _dt, timezone as _tz
                    _now = _dt.now(_tz.utc)
                    _persist_attempts(_attempt_db, [
                        _FireAttempt(
                            _attempt_id_for(_analysis_run_id, _fk(_f)),
                            _FireIdentity(*_fk(_f)),
                            _FireDisposition.FAILED,
                            reason="scoring result was not durably retained",
                            considered_at=_now,
                        )
                        for _f in _unretained_scoring_fires
                        if all(_fk(_f))
                    ])
        except Exception as _pe:
            # Evidence loss must never block report generation.
            print(f"  ⚠️  could not persist attempt history: {_pe}", flush=True)

    # Failed, filtered, unselected and deduplicated fires remain unscored.
    # Research-only runs must never mutate the scoring ledger.
    if args.new_only and not args.insights and _new_only_batch and _completed_scoring_fires and not _os.environ.get("SIGNAL_SCANNER_MARKET"):
        try:
            from database.fires_ledger import mark_scored as _mark_scored

            print(f"  Ledger: marked {_mark_scored(_completed_scoring_fires)} fires as scored", flush=True)
        except Exception as _me:
            print(f"  ⚠️  could not mark fires as scored: {_me}", flush=True)

    if _unretained_scoring_fires:
        print(
            f"  ⚠️  {len(_unretained_scoring_fires)} fire(s) scored but not durably "
            "retained — left unscored for retry",
            flush=True,
        )

    # ── Run-scoped stock payload for the scheduler's current-run handoff ─────
    # Written only when the scheduler asks for it (SIGNAL_STOCK_PAYLOAD), so
    # manual runs keep their existing behaviour. The payload carries the engine's
    # own actionability verdict; it is not a second score-only gate.
    if _STOCK_PAYLOAD_PATH is not None and not args.insights:
        try:
            from tradingview.scheduler.stock_results import (
                build_current_run_payload as _build_payload,
                write_current_run_payload as _write_payload,
            )

            _actionable_rows = _scheduler_stock_rows(
                results, screener=bool(args.screener), env=_os.environ,
            )
            _payload_status = (
                "failed" if args.screener and selected and not results and _scoring_failures
                else "success" if _actionable_rows else "no_new"
            )
            if _os.environ.get("SIGNAL_SCANNER_MARKET") and _scanner_freshness_errors:
                _payload_status = "partial" if results else "failed"
            if _os.environ.get("SIGNAL_SCANNER_MARKET") and _scanner_persistence_failures:
                _payload_status = "failed"
            _payload = _build_payload(
                _analysis_run_id, _actionable_rows, status=_payload_status,
            )
            _payload["freshness_rejections"] = dict(_scanner_freshness_rejections)
            if _scanner_freshness_errors:
                _payload["errors"] = [
                    "scanner freshness rejected candidate(s): "
                    + "; ".join(
                        f"{symbol}: {reason}"
                        for symbol, reason in sorted(_scanner_freshness_errors.items())
                    )[:500]
                ]
            elif _scanner_persistence_failures:
                _payload["errors"] = [
                    "scanner setup persistence failed for candidate(s): "
                    + ", ".join(sorted(_scanner_persistence_failures))[:300]
                ]
            elif _payload_status == "failed":
                _payload["errors"] = ["all selected TradingView scanner candidates failed during signal scoring"]
            elif args.screener and _scoring_failures:
                _payload["errors"] = [f"{len(_scoring_failures)} scanner candidate(s) failed during signal scoring"]
            _write_payload(_STOCK_PAYLOAD_PATH, _payload)
            print(
                f"  Stock payload: {len(_actionable_rows)} actionable row(s) → "
                f"{_STOCK_PAYLOAD_PATH}",
                flush=True,
            )
        except Exception as _spe:
            print(f"  ⚠️  could not write run-scoped stock payload: {_spe}", flush=True)

    ranked = sorted(results, key=lambda x: x["composite"], reverse=True)

    # ── Sector ETF coverage report ────────────────────────────────────────────
    # Surfaces how many ranked symbols resolved to a scored sector ETF. A low
    # count usually means TV sector names aren't covered by the alias table.
    _sector_resolved = sum(
        1 for r in ranked
        if (r.get("market_internals") or {}).get("sector_etf_score") is not None
    )
    _sector_unresolved = [
        (r.get("fundamentals") or {}).get("sector")
        for r in ranked
        if (r.get("market_internals") or {}).get("sector_etf_score") is None
    ]
    _sector_unresolved = sorted({s for s in _sector_unresolved if s and s != "N/A"})
    print(f"  Sector ETF coverage: {_sector_resolved}/{len(ranked)} ranked symbols "
          f"resolved to a scored sector ETF")
    if _sector_unresolved:
        print(f"    unresolved sector names: {', '.join(_sector_unresolved[:10])}"
              + (" ..." if len(_sector_unresolved) > 10 else ""))

    # ── Buy Advisor: fetch cash once and annotate each ranked item ────────────
    account_cash: dict = {}
    forex_data:   list = []
    if _ADVISOR_AVAILABLE and not args.no_moomoo:
        print("  buy advisor: fetching account cash ...", end=" ", flush=True)
        try:
            # Golden Rule #16: NEVER use `with ThreadPoolExecutor` + result(timeout=N).
            # The context manager calls shutdown(wait=True) on __exit__ which blocks
            # until the worker finishes EVEN AFTER TimeoutError — causing multi-minute hangs.
            _tp1 = ThreadPoolExecutor(max_workers=1)
            _fut1 = _tp1.submit(_fetch_all_account_cash)
            try:
                account_cash = _fut1.result(timeout=20)
            except Exception:
                account_cash = {}
            finally:
                _tp1.shutdown(wait=False)
            au = account_cash.get("Moomoo AU (AUD)")
            sg = account_cash.get("Moomoo SG (SGD)")
            print(f"AU={'AUD '+str(int(au)) if au else 'offline'}  "
                  f"SG={'SGD '+str(int(sg)) if sg else 'offline'}")
        except Exception as _ae:
            print(f"(err: {_ae})")
        try:
            _tp2 = ThreadPoolExecutor(max_workers=1)
            _fut2 = _tp2.submit(_get_forex_snapshot)
            try:
                forex_data = _fut2.result(timeout=12)
            except Exception:
                forex_data = []
            finally:
                _tp2.shutdown(wait=False)
        except Exception as exc:
            _log.debug("forex_snapshot_fetch_failed", error=str(exc))
        for _it in ranked:
            _it["buy_advice"] = _build_buy_advice(_it, account_cash)

    # ── US scanner-origin options producer (strict, feature-gated) ───────────
    _scanner_us_options_enabled = (
        str(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_ENABLED", "false")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    _scanner_us_options_active = (
        _scanner_us_options_enabled
        and bool(args.screener)
        and str(_os.environ.get("SIGNAL_SCANNER_MARKET") or "").upper() == "US"
        and str(_os.environ.get("SIGNAL_SCANNER_SCOPE") or "production").lower() == "production"
        and not getattr(args, "no_options", False)
        and not args.insights
    )
    if _scanner_us_options_active:
        from tradingview.options.options_badge import compute_options_badge_data as _scanner_badge_call
        from tradingview.options.us_scanner_producer import produce_us_scanner_options
        from tradingview.scheduler.market_calendar import market_session_bounds
        from tradingview.scheduler.us_options_policy import USOptionsPolicy

        _session_date = str(_os.environ.get("SIGNAL_SCANNER_SESSION_DATE") or "")
        _session_open, _session_close = market_session_bounds("US", _session_date)
        _policy = USOptionsPolicy(
            max_underlyings=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_UNDERLYINGS", "5")),
            max_contracts_per_symbol=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_CONTRACTS_PER_SYMBOL", "1")),
            min_dte=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MIN_DTE", "14")),
            max_dte=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_DTE", "45")),
            min_open_interest=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MIN_OPEN_INTEREST", "100")),
            min_volume=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MIN_VOLUME", "10")),
            max_spread_pct=float(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_SPREAD_PCT", "10")),
            max_quote_age_seconds=int(_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_QUOTE_AGE_SECONDS", "120")),
            quote_mode=_os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE", "strict").strip().lower(),
        )
        _scanner_snapshot = produce_us_scanner_options(
            ranked,
            snapshot_path=_OPTIONS_SNAPSHOT_PATH,
            run_id=_analysis_run_id,
            session_date=_session_date,
            session_open=_session_open,
            session_close=_session_close,
            policy=_policy,
            parent_threshold=float(args.threshold),
            option_threshold=float(_os.environ.get("SCHEDULER_SCORE_THRESHOLD", "70")),
            max_parent_bar_age_minutes=int(_os.environ.get("SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES", "45")),
            configured_provider_delay_minutes=int(_os.environ.get("SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES", "15")),
            badge_call=lambda _symbol, _score: _scanner_badge_call(
                _symbol, signal_score=_score, scanner_all_candidates=True,
            ),
        )
        print(
            f"  US scanner options: {_scanner_snapshot['status']} "
            f"({len(_scanner_snapshot['contracts'])} validated contract(s), "
            f"{len(_scanner_snapshot['errors'])} provider/policy issue(s))",
            flush=True,
        )

    # ── Options Badge (L8b): legacy report badge path ────────────────────────
    elif _OPTIONS_AVAILABLE and not getattr(args, "no_options", False) and not args.insights:
        import concurrent.futures as _cf_badge
        _BADGE_TIMEOUT = 15  # seconds per symbol — hard cap (Golden Rule #16)
        _option_candidates = _select_options_badge_candidates(
            ranked,
            args.options_top_k,
            open_markets_only=args.options_open_only,
        )
        for _it in _option_candidates:
            _it["_options_badge_candidate"] = True
        print(f"  options: fetching ATM call quotes for {len(_option_candidates)}/{len(ranked)} quality-gated signals ...", end=" ", flush=True)
        _opts_ok, _opts_skip, _opts_err = 0, 0, 0
        for _it in _option_candidates:
            _tv_sym, _spot = _get_options_badge_context(_it)
            try:
                _badge_ex = _cf_badge.ThreadPoolExecutor(max_workers=1)
                _badge_fut = _badge_ex.submit(
                    _compute_options_badge_data,
                    _tv_sym,
                    signal_score=float(_it.get("composite", 50.0)),
                    spot_price=_spot,
                )
                try:
                    _opts = _badge_fut.result(timeout=_BADGE_TIMEOUT)
                except _cf_badge.TimeoutError:
                    _opts = {"error": f"timeout ({_BADGE_TIMEOUT}s)"}
                finally:
                    _badge_ex.shutdown(wait=False)
                _it["options_data"] = _opts
                if _opts.get("options_available"):
                    _opts_ok += 1
                elif not _opts:  # unsupported market (ASX/SGX) → empty dict
                    _opts_skip += 1
                else:
                    _opts_skip += 1
                    if _opts.get("error"):
                        print(f"\n    [opts] {_tv_sym}: {_opts['error']}", end="")
            except Exception as _oe:
                _it["options_data"] = {}
                _opts_err += 1
                print(f"\n    [opts] {_tv_sym} EXCEPTION: {_oe}", end="")
        print(f"\n  ✓ {_opts_ok} with options  {_opts_skip} skipped/no-options  {_opts_err} errors")

        _option_status = "success"
        if _opts_err or any(
            isinstance(_it.get("options_data"), dict)
            and _it["options_data"].get("error")
            for _it in _option_candidates
        ):
            _option_status = "partial" if _opts_ok else "unavailable"
        _publish_options_snapshot(_analysis_run_id, _option_status, ranked)
    elif not getattr(args, "no_options", False) and not args.insights:
        _publish_options_snapshot(
            _analysis_run_id,
            "unavailable" if not _OPTIONS_AVAILABLE else "empty",
            ranked,
        )

    # ── AI Commentary: concurrent gpt-4o-mini calls for all ranked items ─────
    if use_ai:
        _target_n = args.ai_top_k if args.ai_top_k and args.ai_top_k > 0 else len(ranked)
        _target_n = min(_target_n, len(ranked))
        print(f"\n  🤖 AI L9: calling {_effective_ai_model} for {_target_n}/{len(ranked)} signals ...", end=" ", flush=True)
        try:
            build_ai_commentary(ranked, workers=max(1, args.ai_workers), top_k=args.ai_top_k, model=_effective_ai_model)
            _ai_done = sum(1 for r in ranked if r.get("ai_commentary")
                           and not r["ai_commentary"].get("error")
                           and not r["ai_commentary"].get("skipped"))
            _ai_err  = sum(1 for r in ranked if r.get("ai_commentary") and r["ai_commentary"].get("error"))
            _ai_skip = sum(1 for r in ranked if r.get("ai_commentary") and r["ai_commentary"].get("skipped"))
            # Verdict breakdown
            _buy  = sum(1 for r in ranked if r.get("ai_commentary", {}).get("verdict") == "BUY_NOW")
            _skip = sum(1 for r in ranked if r.get("ai_commentary", {}).get("verdict") == "SKIP")
            _wtch = sum(1 for r in ranked if r.get("ai_commentary", {}).get("verdict") == "WATCH")
            print(f"✓ {_ai_done} OK  {_ai_err} errors  {_ai_skip} skipped  [BUY={_buy} WATCH={_wtch} SKIP={_skip}]")
        except Exception as _aie:
            print(f"(AI err: {_aie})")

    print(f"\n{'='*70}")
    print("FINAL RANKING")
    # Conviction summary (based on backtest position sizer)
    _uc = {0: 0, 1: 0, 2: 0, 3: 0}
    for _it in ranked:
        _bd = _it.get("backtest_data")
        if _bd and not _bd.get("error"):
            _u = _bd.get("size_units", 1)
            _uc[_u] = _uc.get(_u, 0) + 1
    if any(_uc.values()):
        print(f"  Conviction: {_uc[3]} x 3-unit | {_uc[2]} x 2-unit | "
              f"{_uc[1]} x 1-unit | {_uc[0]} AVOID")
    print(f"{'='*70}")
    print(f"{'#':<4} {'Symbol':<22} {'Score':>7} {'L1-Tech':>8} {'L2-Fund':>8} {'L3-Sent':>8} {'L4-Int':>8}  Notes")
    print("-"*70)
    for rank, it in enumerate(ranked, 1):
        ls = it["scores"]
        notes = []
        if it["sentiment_data"].get("recent_upgrade"):   notes.append("↑Upgraded")
        if it["sentiment_data"].get("recent_downgrade"): notes.append("↓Dngraded")
        er = it["sentiment_data"].get("earnings_risk", "")
        if "High" in er:   notes.append("🔴EarnRisk")
        if "Medium" in er: notes.append("🟡EarnRisk")
        pcr = it["market_internals"].get("pcr")
        if pcr and pcr < 0.5: notes.append(f"PCR={pcr:.2f}✓")
        if pcr and pcr > 1.5: notes.append(f"PCR={pcr:.2f}✗")
        ins = it["market_internals"].get("insider_net","")
        if ins == "Net Buying": notes.append("Ins.Buy✓")
        # Pool badge
        in_pool = it.get("in_pool")
        if in_pool is True:
            notes.append("Pool✓(ctx)")
        elif in_pool is False:
            notes.append("Pool✗")
        # Smallcase basket badge
        if it.get("sc_in_basket"):
            notes.append("SC✓(ctx)")
        if it.get("actionability_reasons"):
            notes.append("GATED:" + ";".join(it["actionability_reasons"]))
        print(f"#{rank:<3} {it['tv_symbol']:<22} {it['composite']:>7.1f} "
              f"{ls['l1']['total']:>8.1f} {ls['l2']['total']:>8.1f} "
              f"{ls['l3']['total']:>8.1f} {ls['l4']['total']:>8.1f}  {' '.join(notes)}")

    # ── Report title based on source mode ────────────────────────────────────
    if _fire_source_mode == "news":
        _dir_label = args.news.title() if args.news else "All"
        _report_title       = f"News Signals — {_dir_label}"
        _actionable_title   = f"News Quality-Gated (decision ≥{args.threshold:.0f}) — {_dir_label}"
    elif _fire_source_mode == "screener":
        _report_title       = "TV Screener Signals"
        _actionable_title   = f"TV Screener Quality-Gated (decision ≥{args.threshold:.0f})"
    elif _fire_source_mode == "austsuper":
        _report_title       = "AustralianSuper Member Direct Signals"
        _actionable_title   = f"AustralianSuper Quality-Gated (decision ≥{args.threshold:.0f})"
    elif _fire_source_mode == "holdings":
        _report_title       = "Current Holdings Signals"
        _actionable_title   = f"Holdings Worth Adding To (decision ≥{args.threshold:.0f})"
    elif _fire_source_mode == "source_filter":
        _label = args.source_filter.replace("_", " ").title()
        _report_title       = f"{_label} Signals"
        _actionable_title   = f"{_label} Quality-Gated (decision ≥{args.threshold:.0f})"
    else:
        _report_title       = "All Signals"
        _actionable_title   = f"Quality-Gated (decision ≥{args.threshold:.0f})"

    # Auto-select source-specific output paths when --out was not explicitly overridden.
    # This prevents different input sources from overwriting each other's reports.
    _default_out = str(OUTPUT_HTML)
    _out_dir = Path(args.out).parent
    if args.out == _default_out and _fire_source_mode != "download":
        if _fire_source_mode == "news":
            _src_suffix = f"news_{args.news}"
        elif _fire_source_mode == "austsuper":
            _src_suffix = "austsuper"
        elif _fire_source_mode == "holdings":
            _src_suffix = "holdings"
        elif _fire_source_mode == "source_filter":
            _src_suffix = args.source_filter
        else:  # screener
            _src_suffix = "screener"
        out_path        = _out_dir / f"signal_stack_report_{_src_suffix}.html"
        actionable_path = _out_dir / f"signal_report_actionable_{_src_suffix}.html"
    else:
        out_path        = Path(args.out)
        actionable_path = (
            Path(args.actionable_out)
            if args.actionable_out
            else out_path.parent / "actionable_report.html"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _cov = {"india": use_india, "moomoo": use_moomoo, "external": use_external, "ibkr": use_ibkr}
    # Archive previous version before overwriting (prevents data loss on re-run)
    _abw = None  # initialise before try so actionable block can reference safely
    if args.insights:
        # --insights mode: console summary only — no file I/O, no DB writes
        print(f"\n{'─'*60}")
        print(f"💡 INSIGHTS MODE — top {len(ranked)} scored signals (no DB write, no HTML)")
        print(f"{'─'*60}")
        for _r in ranked[:20]:
            _sym = _r.get("tv_symbol", "?")
            _sc  = _r.get("composite", 0)
            _l1  = (_r.get("scores") or {}).get("l1", {}).get("total", 0)
            _l2  = (_r.get("scores") or {}).get("l2", {}).get("total", 0)
            _l3  = (_r.get("scores") or {}).get("l3", {}).get("total", 0)
            print(f"  {_sym:<15} score={_sc:5.1f}  L1={_l1:4.1f}  L2={_l2:4.1f}  L3={_l3:4.1f}")
        print(f"\n  Total scored: {len(ranked)}  |  Above threshold {args.threshold:.0f}: {sum(1 for r in ranked if r.get('composite',0) >= args.threshold)}")
        return
    try:
        # out_path is already inside the reports/ dir — add it to sys.path so
        # report_db.py (which lives alongside signal_stack_report.html) is importable.
        _rdb_root = str(out_path.parent)
        _ensure_paths(_rdb_root)
        import sys as _sys
        if _rdb_root not in _sys.path:
            _sys.path.insert(0, _rdb_root)
        if _scanner_test_scope:
            _abw = lambda *a, **k: None
        else:
            from report_db import archive_before_write as _abw
        _abw(out_path, label="all_signals")
        # Archive existing JSON before overwrite
        _json_out = out_path.with_suffix(".json")
        if _json_out.exists():
            _abw(_json_out, label="all_signals_json")
    except Exception as _ex:
        _log.debug("report_db_archive_skipped", error=str(_ex))
    _ss_html = build_html(ranked, len(ranked), title=_report_title,
                          account_cash=account_cash, forex_data=forex_data,
                          coverage=_cov)
    _ss_ts   = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _full_rows = [_report_signal_row(row, rank) for rank, row in enumerate(ranked, 1)]
    _ss_agent = {
        "generated_at":      _ss_ts,
        "run_id":            _analysis_run_id,
        "model_version":     SCORE_MODEL_VERSION,
        "source":            _fire_source_mode,
        "n_total":           len(ranked),
        "n_above_threshold": sum(1 for r in ranked if r.get("composite", 0) >= args.threshold),
        "n_actionable":      sum(1 for r in ranked if r.get("actionable")),
        "threshold":         args.threshold,
        "coverage":          _cov,
        "scoring_policy": {
            "quality": "regime-weighted L1-L4, Moomoo L5, IBKR L6, external L7 bonus, plus book-feature bonus",
            "decision": "quality_score * confidence_factor + pool_boost + smallcase_boost",
            "confidence_factor": f"clamp(data_confidence.overall, {CONFIDENCE_DEMOTION_FLOOR}, 1.0)",
            "per_signal_derivation": "signals[].score_breakdown.steps",
            "context_only": ["india_portfolio"],
            "actionability": [
                f"decision >= {args.threshold}",
            ],
        },
        "macro_context": yc,
        "screener_pool": {**_pool_meta, "age_hours": _pool_age_hours, "is_fresh": _pool_is_fresh},
        "top_signals": _full_rows[:20],
        "signals": _full_rows,
    }
    _ss_comment_data = {key: value for key, value in _ss_agent.items() if key != "signals"}
    _ss_comment_data["top_signals"] = [
        {key: row[key] for key in ("rank", "symbol", "score", "quality_score", "l1", "l2", "l3", "l4")}
        for row in _full_rows[:20]
    ]
    _ss_comment = f"<!-- AGENT-DATA: {json.dumps(_ss_comment_data, separators=(',', ':'), default=str)} -->\n"
    out_path.write_text(_embed_agent_data(_ss_html, _ss_comment), encoding="utf-8")
    out_path.with_suffix(".json").write_text(json.dumps(_ss_agent, indent=2, default=str), encoding="utf-8")
    print(f"\n✅ All-signals report  → {out_path}")

    # Actionable report — use WATCH fallback when nothing meets threshold.
    # Gate policy is propagated so report selection matches per-symbol eligibility.
    actionable, _is_watch = _select_actionable_report_items(
        ranked, args.threshold,
        require_trigger=args.require_trigger,
        require_buy_flag=args.require_buy_flag,
    )
    if _is_watch:
        _actionable_title = f"WATCH Candidates (near-actionable, below threshold {args.threshold:.0f})"
    try:
        if _abw is not None:
            _abw(actionable_path, label="actionable")
            _json_act = actionable_path.with_suffix(".json")
            if _json_act.exists():
                _abw(_json_act, label="actionable_json")
    except Exception as _ex:
        _log.debug("actionable_archive_skipped", error=str(_ex))
    _act_html = build_html(actionable, len(actionable),
                           title=_actionable_title,
                           account_cash=account_cash, forex_data=forex_data,
                           coverage=_cov)
    _act_agent = {
        "status":        "complete",
        "generated_at": _ss_ts,
        "run_id":       _analysis_run_id,
        "model_version": SCORE_MODEL_VERSION,
        "source":       _fire_source_mode,
        "n_total":      len(ranked),
        "n_actionable": 0 if _is_watch else len(actionable),
        "n_reported":   len(actionable),
        "is_watch":     bool(_is_watch),
        "threshold":    args.threshold,
        "coverage":     _cov,
        "actionable_signals": [] if _is_watch else [
            _report_signal_row(row, rank) for rank, row in enumerate(actionable, 1)
        ],
        "watch_signals": [
            _report_signal_row(row, rank) for rank, row in enumerate(actionable, 1)
        ] if _is_watch else [],
    }
    _act_comment_data = {**_act_agent, "actionable_signals": [
        {key: row[key] for key in ("rank", "symbol", "score", "quality_score", "l1", "l2", "l3", "l4")}
        for row in _act_agent["actionable_signals"]
    ]}
    _act_comment = f"<!-- AGENT-DATA: {json.dumps(_act_comment_data, separators=(',', ':'), default=str)} -->\n"
    _act_json = actionable_path.with_suffix(".json")
    _act_tmp = _act_json.with_name(f".{_act_json.name}.{uuid.uuid4().hex}.tmp")
    _act_tmp.write_text(json.dumps(_act_agent, indent=2, default=str), encoding="utf-8")
    _os.replace(_act_tmp, _act_json)
    _html_tmp = actionable_path.with_name(f".{actionable_path.name}.{uuid.uuid4().hex}.tmp")
    _html_tmp.write_text(_embed_agent_data(_act_html, _act_comment), encoding="utf-8")
    _os.replace(_html_tmp, actionable_path)
    if _is_watch:
        print(f"\u26a0\ufe0f  No actionable symbols found. WATCH report \u2192 {actionable_path}  ({len(actionable)} near-actionable candidates)")
    else:
        print(f"\u2705 Quality-gated report \u2192 {actionable_path}  ({len(actionable)} symbols passed)")

    # Archive both reports with timestamp for history
    if not _scanner_test_scope:
        _archive_reports(out_path, actionable_path)

    # ── Persist to report_analytics.db ───────────────────────────────────────
    if not _scanner_test_scope:
        try:
            _ensure_paths(Path(__file__).parent.parent / "reports")
            from report_db import save_signal_stack_snapshot
            save_signal_stack_snapshot(ranked, actionable, _fire_source_mode, args.threshold)
        except Exception as _e:
            print(f"  [report_db] signal_stack snapshot skipped: {_e}")

if __name__ == "__main__":
    main()
    # Force-exit to kill any Moomoo SDK non-daemon background threads
    # that prevent clean process exit (same pattern as run_options.py)
    import os as _os_exit
    _os_exit._exit(0)
