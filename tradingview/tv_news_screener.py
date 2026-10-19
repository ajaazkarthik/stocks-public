"""
TV News Screener — Event-Driven Signal Source
=============================================
Fetches news from TradingView's authenticated news API (Reuters, Dow Jones,
Quartr Investor Relations, MarketWatch) and extracts stocks with strong
sentiment for the 4-layer scoring pipeline.

Research justification:
  • Post-Earnings Announcement Drift (PEAD): Ball & Brown 1968 — most robust
    stock anomaly; news surprises predict multi-day returns.
  • Tetlock (2007): Media pessimism score predicts next-day stock returns with
    statistical significance.
  • Groß-Klußmann & Hautsch (2011): Reuters / Bloomberg news events cause
    30-40% abnormal returns on the arrival day.
  • Commercial validation: RavenPack, Refinitiv Eikon sell real-time news
    sentiment feeds at $100k+/year to institutions.

Provider quality (live-validated 2026-04-09):
  quartr       96% symbol-tagged  HIGH  — IR/earnings summaries, all markets
  reuters      86% symbol-tagged  HIGH  — global financial news
  dow-jones    76% symbol-tagged  HIGH  — analyst upgrades/downgrades
  market-watch 80% symbol-tagged  MED   — US financial media
  invezz       66% symbol-tagged  MED   — global finance

Signal thresholds:
  BUY  signal  → compound > +0.35   (configurable)
  BEAR signal  → compound < -0.35   → routes to put-option analysis

Usage (standalone):
  python tv_news_screener.py --bullish                  # default mode
  python tv_news_screener.py --bearish                  # put/short candidates
  python tv_news_screener.py --all                      # both directions
  python tv_news_screener.py --max-age-hours 48         # wider news window
  python tv_news_screener.py --bullish-threshold 0.4    # stricter filter
  python tv_news_screener.py --providers quartr,reuters # specific providers

Usage (imported by input_sources.py / webhook_server.py):
  from tv_news_screener import fetch_news_signals, news_to_fires, SOURCE_NEWS
"""

from __future__ import annotations

import sys
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ── VADER sentiment (optional but strongly recommended) ───────────────────────
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VADER
    _vader = _VADER()
    _VADER_OK = True
except ImportError:
    _vader = None  # type: ignore[assignment]
    _VADER_OK = False

# ── TV News API ───────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
if str(_THIS_DIR / "tv_data") not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:
    from tv_data.tv_news import fetch_headlines, fetch_story, fetch_story_snippets
    _TV_NEWS_OK = True
except ImportError:
    _TV_NEWS_OK = False

    def fetch_headlines(*a, **kw) -> list:  # type: ignore[misc]
        return []

    def fetch_story(*a, **kw) -> dict:  # type: ignore[misc]
        return {}

    def fetch_story_snippets(*a, **kw) -> list:  # type: ignore[misc]
        return []

# ── Source constant ───────────────────────────────────────────────────────────
SOURCE_NEWS = "news"

# ── Provider weights (research-backed, live-calibrated 2026-04-09) ───────────
PROVIDER_WEIGHTS: dict[str, float] = {
    "quartr":           1.5,   # IR reports, earnings summaries — highest precision
    "reuters":          1.2,   # global financial news
    "dow-jones":        1.2,   # analyst upgrades/downgrades
    "market-watch":     1.0,   # US financial media
    "invezz":           0.9,   # global finance, lower S/N
    "globenewswire":    0.8,   # press releases, noisy
    "tmx_newsfile":     0.8,   # Canadian TSX press releases
    "trading-economics": 0.7,  # macro data (less stock-specific)
    "sharecast":        0.7,   # UK/LSE
    "dpa_afx":          0.6,   # German/XETRA
    "zawya":            0.5,   # MENA, low tagging
    "stocktwits":       0.4,   # social/noisy
}

_DEFAULT_PROVIDERS = ["quartr", "reuters", "dow-jones", "market-watch", "invezz"]

# Tradable exchanges: news signals from other exchanges are discarded.
# Matches the user's broker coverage: US, AU, IN, HK, SG, JP, KR.
_TRADABLE_EXCHANGES: frozenset[str] = frozenset({
    "NYSE", "NASDAQ", "AMEX", "BATS", "CBOE",           # US
    "ASX", "ASX_DLY",                                     # Australia
    "NSE", "BSE",                                         # India
    "HKEX",                                               # Hong Kong
    "SGX",                                                # Singapore
    "TYO", "TSE",                                         # Japan
    "KRX", "KOSDAQ",                                      # Korea
})

# Bare uppercase words to IGNORE when scanning moomoo community posts for tickers.
# Community posts mix ticker names with common uppercase abbreviations.
_MOOMOO_BARE_NOISE: frozenset[str] = frozenset({
    "AI", "EV", "US", "UK", "EU", "IT", "IN", "ON", "AT", "TO", "OR",
    "AND", "FOR", "THE", "ALL", "NEW", "BIG", "BUY", "SELL", "SHORT",
    "LONG", "HIGH", "LOW", "CEO", "CFO", "COO", "IPO", "ETF", "OTC",
    "PE", "EPS", "ATH", "ATL", "FY", "YTD", "QOQ", "YOY", "TV",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "MA", "RSI", "BB",
    # tech/platform names often mentioned but not exchange-listed tickers
    "AWS", "GCP", "API", "SDK", "SaaS", "PaaS", "IaaS",
    # crypto terms that aren't stocks
    "BTC", "ETH", "SOL", "XRP", "BNB", "USDT", "USDC",
    # financial metrics / chart terms used as nouns
    "CPM", "CPC", "CPA", "ROI", "ROE", "ROA", "FCF", "EBIT", "DCF",
    "VIX", "SPX", "NDX", "DXY", "CPI", "PPI", "GDP", "PCE",
    # common 2-3 letter fragments in product names (MI300X, RX7900, etc.)
    "MI", "RX", "GT", "GX", "FX", "DX",
    # ── Exchange/region suffixes extracted from Moomoo ticker format (NVDA.US) ──
    # These appear in post text as bare tokens and would generate bogus signals.
    "SG", "MY", "HK", "AU", "JP", "KR", "CN", "TW", "ID", "TH", "VN",
    # ── Chart / analysis abbreviations ──
    "TF",   # timeframe ("1Y TF", "4H TF")
    "VC",   # venture capital used as noun, not a ticker in this context
    "TP",   # take profit
    "SL",   # stop loss
    "BE",   # break-even
    "RR",   # risk/reward
    # ── Common English words that are also ticker symbols ──
    # These appear in free-text posts and should not trigger signals.
    "TRUE", "FALSE",  # booleans / "clicked TRUE" etc.
    "CALL", "PUTS",   # options terminology used as nouns
    "HOLD", "MOON",   # community slang
    "CASH", "BOND", "GOLD", "RISE", "FALL",
    # ── Company full names that appear in Moomoo $Company (TKR.US)$ format ──
    # The actual ticker (NVDA) is extracted separately; full name creates noise.
    "NVIDIA", "MICRON", "AMAZON", "GOOGLE", "APPLE", "TESLA", "MICROSOFT",
    # NOTE: META is intentionally NOT here — META is the real exchange ticker for
    # Meta Platforms, Inc. It must not be silenced.
})

# ── Maximum age of articles to score (avoid stale signals) ───────────────────
_MAX_AGE_SECS_DEFAULT = 24 * 3600  # 24 h


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────
class NewsSignal(NamedTuple):
    pro_symbol: str         # EXCHANGE:TICKER
    compound:   float       # weighted sentiment score in [-1, 1]
    bearish:    bool        # True → PUT option / short candidate
    articles:   int         # how many articles contributed
    providers:  list        # list[str] of contributing providers (may be list at runtime)
    titles:     list        # list[str] sample headlines for display


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────
# ── FinBERT sentiment (optional, higher accuracy for financial text) ──────────
_finbert_scorer = None   # lazy-loaded on first use

def _load_finbert():
    global _finbert_scorer
    if _finbert_scorer is None:
        try:
            _this_tv = Path(__file__).parent
            if str(_this_tv) not in sys.path:
                sys.path.insert(0, str(_this_tv))
            from sentiment import get_scorer
            _finbert_scorer = get_scorer("finbert")
        except Exception:
            _finbert_scorer = None
    return _finbert_scorer


def _score_text(text: str, backend: str = "vader") -> float:
    """Return sentiment compound score in [-1, 1].

    backend: "vader" (fast, default), "finbert" (accurate), "both" (average).
    Falls back to VADER if FinBERT unavailable.
    """
    if not text:
        return 0.0
    vader_score = 0.0
    if _VADER_OK:
        vader_score = float(_vader.polarity_scores(text)["compound"])
    if backend == "vader" or not _VADER_OK:
        return vader_score
    # Try FinBERT
    fb = _load_finbert()
    if fb is None:
        return vader_score  # fallback
    try:
        fb_score = float(fb(text))
        if backend == "finbert":
            return fb_score
        # "both" → average
        return (vader_score + fb_score) / 2.0
    except Exception:
        return vader_score


def _is_too_old(published_unix: int | float, max_age_secs: float) -> bool:
    """Return True if the article is older than max_age_secs."""
    if not published_unix:
        return False
    age = time.time() - float(published_unix)
    return age > max_age_secs


def _extract_symbols(item: dict) -> list[str]:
    """
    Return EXCHANGE:TICKER strings from the relatedSymbols field.
    relatedSymbols format: [{'symbol': 'NASDAQ:NVDA', 'logoid': 'nvidia'}, ...]
    """
    syms: list[str] = []
    for rs in item.get("relatedSymbols") or []:
        sym = rs.get("symbol", "").strip().upper()
        if sym and ":" in sym:
            syms.append(sym)
    return syms


# ─────────────────────────────────────────────────────────────────────────────
# Core signal extractor
# ─────────────────────────────────────────────────────────────────────────────
def fetch_news_signals(
    providers: list[str] | None = None,
    max_age_hours: float = 24.0,
    bullish_threshold: float = 0.35,
    bearish_threshold: float = -0.35,
    min_articles: int = 1,
    fetch_body_n: int = 20,
    exchange_filter: list[str] | None = None,
    sentiment_backend: str = "vader",
) -> tuple[list[NewsSignal], list[NewsSignal]]:
    """
    Scan TradingView news across providers, score each symbol via sentiment
    analysis, and return signals separated by direction.

    Args:
        providers:          Provider list to query.  None → _DEFAULT_PROVIDERS.
        max_age_hours:      Discard articles older than this (default 24 h).
        bullish_threshold:  Compound score cutoff for BUY signals (default 0.35).
        bearish_threshold:  Compound score cutoff for BEAR signals (default -0.35).
        min_articles:       Minimum articles to include a symbol (default 1).
        fetch_body_n:       Fetch story body for the top N articles per provider
                            (enriches sentiment beyond headlines alone).
        exchange_filter:    Restrict to these exchanges.  None (default) uses
                            _TRADABLE_EXCHANGES (the user's 7 tradable markets).
        sentiment_backend:  "vader" (fast), "finbert" (accurate), or "both" (avg).

    Returns:
        (bullish_signals, bearish_signals) each sorted by |compound| descending.
    """
    prov_list: list[str] = providers if providers is not None else _DEFAULT_PROVIDERS
    max_age_secs = max_age_hours * 3600.0

    # ── Accumulate per-symbol: sum(weighted_score), sum(weights), article count ─
    # sym → {"wsum": float, "n": int, "providers": set, "titles": list[str]}
    sym_data: dict[str, dict] = {}

    for provider in prov_list:
        prov_weight = PROVIDER_WEIGHTS.get(provider, 1.0)
        items = fetch_headlines(provider=provider, limit=200)
        if not items:
            continue

        # Fetch story bodies for top fetch_body_n articles (richer VADER input)
        body_snippets: dict[str, str] = {}
        for it in items[:fetch_body_n]:
            sid = it.get("id", "")
            if sid:
                story = fetch_story(sid, timeout=8)
                desc = story.get("shortDescription", "").strip()
                if desc:
                    body_snippets[sid] = desc

        for it in items:
            published = it.get("published", 0)
            if _is_too_old(published, max_age_secs):
                continue

            syms = _extract_symbols(it)
            if not syms:
                continue

            title = it.get("title", "").strip()
            body  = body_snippets.get(it.get("id", ""), "")
            # Score headline + body; headline weighted 0.5, body weighted 0.5
            h_score = _score_text(title, backend=sentiment_backend)
            b_score = _score_text(body, backend=sentiment_backend) if body else h_score
            raw_score = (h_score + b_score) / 2.0
            weighted  = raw_score * prov_weight

            for sym in syms:
                # Optional exchange filter
                exch = sym.split(":")[0] if ":" in sym else ""
                active_filter = exchange_filter if exchange_filter is not None else _TRADABLE_EXCHANGES
                if exch not in active_filter:
                    continue
                if sym not in sym_data:
                    sym_data[sym] = {"wsum": 0.0, "wcount": 0.0, "n": 0,
                                      "providers": set(), "titles": []}
                sym_data[sym]["wsum"]   += weighted
                sym_data[sym]["wcount"] += prov_weight
                sym_data[sym]["n"]      += 1
                sym_data[sym]["providers"].add(provider)
                if title and title not in sym_data[sym]["titles"]:
                    sym_data[sym]["titles"].append(title)

    # ── Moomoo Community posts (cached, optional) ────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from moomoo_community_intel import get_community_headlines
        _moo_posts = get_community_headlines()
        if _moo_posts:
            for headline in _moo_posts:
                h_score = _score_text(headline, backend=sentiment_backend)
                if abs(h_score) < 0.1:
                    continue  # skip neutral noise
                # Extract ticker-like tokens: $AAPL, NASDAQ:AAPL, or bare AAPL
                _toks = re.findall(
                    r'\$([A-Z]{1,6})\b'          # $AAPL style
                    r'|([A-Z]{2,6}:[A-Z]{1,6})'  # NASDAQ:AAPL style
                    r'|(?<![A-Z:])([A-Z]{2,5})(?![A-Z:])',  # bare AAPL / NVDA
                    headline,
                )
                for tok in _toks:
                    bare = tok[0] or tok[1] or tok[2]
                    if not bare or bare in _MOOMOO_BARE_NOISE:
                        continue
                    # Try to match against existing sym_data keys first (boost)
                    matched_sym = None
                    for existing_sym in list(sym_data.keys()):
                        if bare.upper() in existing_sym.upper():
                            matched_sym = existing_sym
                            break

                    if matched_sym:
                        # Boost existing entry (stock already in TV news)
                        sym_data[matched_sym]["wsum"]   += h_score * 0.5
                        sym_data[matched_sym]["wcount"] += 0.5
                        sym_data[matched_sym]["n"]      += 1
                        sym_data[matched_sym]["providers"].add("moomoo-community")
                        if headline not in sym_data[matched_sym]["titles"]:
                            sym_data[matched_sym]["titles"].append(headline)
                    else:
                        # ── NEW: Discover stocks trending ONLY on moomoo community ──
                        # Determine full pro_symbol: use EXCHANGE:TICKER if already
                        # formatted, otherwise infer exchange from ticker pattern.
                        if ":" in bare:
                            # Already has exchange prefix (e.g. NASDAQ:AAPL)
                            candidate_sym = bare.upper()
                            exch = candidate_sym.split(":")[0]
                        else:
                            # Bare ticker — check tradable exchanges, default to NASDAQ
                            # for US-looking tickers (1-5 chars). ASX tickers often 3 chars.
                            ticker_upper = bare.upper()
                            # Heuristic: all-alpha, 2-4 chars with no obvious exchange hint
                            candidate_sym = f"NASDAQ:{ticker_upper}"
                            exch = "NASDAQ"

                        # Apply exchange filter (same filter used for TV news symbols)
                        active_filter = exchange_filter if exchange_filter is not None else _TRADABLE_EXCHANGES
                        if exch not in active_filter:
                            continue

                        # Add as new entry sourced exclusively from moomoo community
                        if candidate_sym not in sym_data:
                            sym_data[candidate_sym] = {
                                "wsum": 0.0, "wcount": 0.0, "n": 0,
                                "providers": set(), "titles": [],
                            }
                        sym_data[candidate_sym]["wsum"]   += h_score * 0.5
                        sym_data[candidate_sym]["wcount"] += 0.5
                        sym_data[candidate_sym]["n"]      += 1
                        sym_data[candidate_sym]["providers"].add("moomoo-community")
                        if headline not in sym_data[candidate_sym]["titles"]:
                            sym_data[candidate_sym]["titles"].append(headline)
    except Exception:
        pass  # moomoo community is supplementary — never block

    # ── Build signal lists ───────────────────────────────────────────────────
    bullish: list[NewsSignal] = []
    bearish: list[NewsSignal] = []

    for sym, d in sym_data.items():
        if d["n"] < min_articles:
            continue
        # Weighted-average compound score: Σ(weight × score) / Σ(weights)
        compound = d["wsum"] / d["wcount"] if d["wcount"] > 0 else 0.0
        compound = round(max(-1.0, min(1.0, compound)), 4)
        prov_list_finalised = sorted(d["providers"])
        titles = d["titles"][:5]   # cap display titles at 5

        if compound >= bullish_threshold:
            bullish.append(NewsSignal(
                pro_symbol=sym,
                compound=compound,
                bearish=False,
                articles=d["n"],
                providers=prov_list_finalised,
                titles=titles,
            ))
        elif compound <= bearish_threshold:
            bearish.append(NewsSignal(
                pro_symbol=sym,
                compound=compound,
                bearish=True,
                articles=d["n"],
                providers=prov_list_finalised,
                titles=titles,
            ))

    bullish.sort(key=lambda s: -s.compound)
    bearish.sort(key=lambda s:  s.compound)  # most negative first
    return bullish, bearish


# ─────────────────────────────────────────────────────────────────────────────
# Fire dict factory — plugs into the 4-layer scoring pipeline
# ─────────────────────────────────────────────────────────────────────────────
def _make_news_fire(signal: NewsSignal) -> dict:
    """Build a fire dict from a NewsSignal, compatible with analyse_fire()."""
    now_iso = datetime.now(timezone.utc).isoformat()
    direction = "BEAR" if signal.bearish else "BUY"
    # Message is parse_fire_meta-compatible (L1 technical scoring reads it)
    message = (
        f"NEWS_{direction}:1, sentiment:{signal.compound:+.3f}, "
        f"articles:{signal.articles}, providers:{','.join(signal.providers)}"
    )
    fire: dict = {
        "fire_id":        int(time.time() * 1000),
        "alert_id":       0,
        "symbol":         signal.pro_symbol,
        "pro_symbol":     signal.pro_symbol,
        "resolution":     "D",
        "sound_file":     None,
        "sound_duration": 0,
        "popup":          False,
        "cross_interval": False,
        "message":        message,
        "fire_time":      now_iso,
        "bar_time":       now_iso,
        "name":           f"news_{signal.pro_symbol}",
        "kinds":          [SOURCE_NEWS],
        "close":          None,
        "_source":        SOURCE_NEWS,
        "_bearish":       signal.bearish,
        # News-specific payload (for display, not analysis layers)
        "_news_compound":  signal.compound,
        "_news_articles":  signal.articles,
        "_news_providers": signal.providers,
        "_news_titles":    signal.titles,
    }
    return fire


def news_to_fires(
    providers: list[str] | None = None,
    max_age_hours: float = 24.0,
    bullish_threshold: float = 0.35,
    bearish_threshold: float = -0.35,
    min_articles: int = 1,
    fetch_body_n: int = 20,
    exchange_filter: list[str] | None = None,
    sentiment_backend: str = "vader",
) -> tuple[list[dict], list[dict]]:
    """
    High-level entry point: fetch news signals and return fire dicts ready for
    the 4-layer scoring pipeline.

    Returns:
        (bullish_fires, bearish_fires) — lists of fire dicts.
        Bullish fires have _bearish=False; bearish fires have _bearish=True.
        Both lists are deduplicated (one fire per symbol).
    """
    bullish_sigs, bearish_sigs = fetch_news_signals(
        providers=providers,
        max_age_hours=max_age_hours,
        bullish_threshold=bullish_threshold,
        bearish_threshold=bearish_threshold,
        min_articles=min_articles,
        fetch_body_n=fetch_body_n,
        exchange_filter=exchange_filter,
        sentiment_backend=sentiment_backend,
    )

    seen: set[str] = set()
    bullish_fires: list[dict] = []
    bearish_fires: list[dict] = []

    for sig in bullish_sigs:
        if sig.pro_symbol not in seen:
            seen.add(sig.pro_symbol)
            bullish_fires.append(_make_news_fire(sig))

    for sig in bearish_sigs:
        if sig.pro_symbol not in seen:
            seen.add(sig.pro_symbol)
            bearish_fires.append(_make_news_fire(sig))

    return bullish_fires, bearish_fires


# ─────────────────────────────────────────────────────────────────────────────
# Metadata helper (mirrors tv_screener_to_fires meta dict shape)
# ─────────────────────────────────────────────────────────────────────────────
def news_scan_meta(
    bullish_fires: list[dict],
    bearish_fires: list[dict],
    providers: list[str] | None,
    max_age_hours: float,
    bullish_threshold: float,
    bearish_threshold: float,
) -> dict:
    """Build a metadata dict summarising the scan (for /news-scan endpoint)."""
    return {
        "providers_queried":  providers or _DEFAULT_PROVIDERS,
        "max_age_hours":      max_age_hours,
        "bullish_threshold":  bullish_threshold,
        "bearish_threshold":  bearish_threshold,
        "bullish_signals":    len(bullish_fires),
        "bearish_signals":    len(bearish_fires),
        "total_signals":      len(bullish_fires) + len(bearish_fires),
        "vader_available":    _VADER_OK,
        "tv_news_available":  _TV_NEWS_OK,
        "generated_at":       datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML Report generation
# ─────────────────────────────────────────────────────────────────────────────
def _build_screener_html_report(
    bullish_fires: list[dict],
    bearish_fires: list[dict],
    mode: str,           # "bullish" | "bearish" | "all"
    meta: dict,
    providers: list[str] | None,
    max_age_hours: float,
) -> Path:
    """Build a self-contained dark-themed HTML report for news-screener signals.

    Returns the path the report was written to.
    """
    import html as _html_lib

    reports_dir = _THIS_DIR.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "tv_news_screener_report.html"

    prov_str  = ", ".join(providers or _DEFAULT_PROVIDERS)
    scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vader_ok  = meta.get("vader_available", False)
    tv_ok     = meta.get("tv_news_available", False)

    def _score_bar(compound: float) -> str:
        pct = min(100, max(0, int((compound + 1) * 50)))  # -1→0%, 0→50%, +1→100%
        color = "#00ff87" if compound > 0 else "#ff4444"
        return (f'<div style="background:#1e2a3a;border-radius:4px;height:8px;width:160px;display:inline-block">'
                f'<div style="background:{color};width:{pct}%;height:100%;border-radius:4px"></div></div>')

    def _signal_cards(fires: list[dict], direction: str) -> str:
        if not fires:
            return f'<div class="empty-state">No {direction} signals found above threshold</div>'
        color = "#00e676" if direction == "bullish" else "#ff5252"
        badge = "BUY" if direction == "bullish" else "PUT/SHORT"
        rows = []
        for f in fires:
            sym       = _html_lib.escape(f.get("pro_symbol", "?"))
            compound  = f.get("_news_compound", 0.0)
            articles  = f.get("_news_articles", 0)
            providers_ = f.get("_news_providers") or []
            titles    = f.get("_news_titles") or []
            prov_badges = "".join(
                f'<span class="badge">{_html_lib.escape(p)}</span>'
                for p in providers_[:4]
            )
            title_html = "".join(
                f'<div class="headline">» {_html_lib.escape(t[:100])}</div>'
                for t in titles[:3]
            )
            rows.append(f"""
<div class="signal-card">
  <div class="card-header">
    <span class="sym">{sym}</span>
    <span class="signal-badge" style="background:{color}22;color:{color};border:1px solid {color}44">{badge}</span>
    <span class="score" style="color:{color}">{compound:+.3f}</span>
    {_score_bar(compound)}
    <span class="art-count">{articles} article{'s' if articles != 1 else ''}</span>
  </div>
  <div class="providers">{prov_badges}</div>
  <div class="headlines">{title_html}</div>
</div>""")
        return "\n".join(rows)

    bull_section = ""
    bear_section = ""
    if mode in ("bullish", "all"):
        bull_section = f"""
<section>
  <h2 class="section-title bull">&#x1F4C8; Bullish Signals — BUY Candidates
    <span class="count">{len(bullish_fires)}</span>
  </h2>
  {_signal_cards(bullish_fires, "bullish")}
</section>"""

    if mode in ("bearish", "all"):
        bear_section = f"""
<section>
  <h2 class="section-title bear">&#x1F4C9; Bearish Signals — PUT/Short Candidates
    <span class="count">{len(bearish_fires)}</span>
  </h2>
  {_signal_cards(bearish_fires, "bearish")}
</section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV News Screener — {mode.title()} Signals</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #c9d1d9; padding: 24px; }}
h1   {{ color: #e6edf3; font-size: 1.5rem; margin-bottom: 4px; }}
.subtitle {{ color: #8b949e; font-size: 0.85rem; margin-bottom: 24px; }}
.meta-bar {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 28px;
             background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 14px 20px; font-size: 0.82rem; color: #8b949e; }}
.meta-item span {{ color: #c9d1d9; font-weight: 600; }}
.section-title {{ font-size: 1.1rem; margin: 0 0 16px; padding: 10px 16px;
                  border-radius: 6px; display: flex; align-items: center; gap: 12px; }}
.section-title.bull {{ background: #0d2318; color: #00e676; border-left: 3px solid #00e676; }}
.section-title.bear {{ background: #2d0d0d; color: #ff5252; border-left: 3px solid #ff5252; }}
.count {{ font-size: 0.8rem; background: #30363d; color: #8b949e;
          border-radius: 10px; padding: 2px 8px; font-weight: 400; }}
.signal-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                padding: 14px 18px; margin-bottom: 12px; }}
.signal-card:hover {{ border-color: #58a6ff; }}
.card-header {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 8px; }}
.sym  {{ font-size: 1rem; font-weight: 700; color: #e6edf3; min-width: 160px; }}
.signal-badge {{ font-size: 0.7rem; font-weight: 700; padding: 2px 8px;
                 border-radius: 4px; letter-spacing: 0.5px; }}
.score {{ font-size: 0.95rem; font-weight: 600; min-width: 62px; }}
.art-count {{ color: #8b949e; font-size: 0.8rem; }}
.providers {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }}
.badge {{ background: #21262d; color: #8b949e; font-size: 0.72rem; padding: 2px 7px;
          border-radius: 4px; border: 1px solid #30363d; }}
.headline {{ color: #8b949e; font-size: 0.82rem; margin-top: 3px; padding-left: 4px; }}
.empty-state {{ color: #484f58; text-align: center; padding: 40px; font-size: 0.9rem;
                background: #161b22; border-radius: 8px; border: 1px dashed #30363d; }}
section {{ margin-bottom: 36px; }}
.footer {{ color: #484f58; font-size: 0.75rem; text-align: center;
           border-top: 1px solid #21262d; margin-top: 32px; padding-top: 16px; }}
</style>
</head>
<body>
<h1>TV News Screener</h1>
<div class="subtitle">News-driven signal extraction → 4-layer scoring pipeline</div>

<div class="meta-bar">
  <div class="meta-item">Scan time: <span>{scan_time}</span></div>
  <div class="meta-item">Providers: <span>{_html_lib.escape(prov_str)}</span></div>
  <div class="meta-item">Age window: <span>{max_age_hours}h</span></div>
  <div class="meta-item">VADER: <span>{'✓ active' if vader_ok else '✗ fallback'}</span></div>
  <div class="meta-item">TV News API: <span>{'✓ live' if tv_ok else '✗ offline'}</span></div>
  <div class="meta-item">Total signals: <span>{meta.get('total_signals', 0)}</span></div>
</div>

{bull_section}
{bear_section}

<div class="footer">
  Generated by tv_news_screener.py &mdash; research basis: Tetlock (2007), Ball &amp; Brown (1968), Gro&szlig;-Klu&szlig;mann &amp; Hautsch (2011)
</div>
</body>
</html>"""

    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────
def _cli() -> None:
    if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    import argparse

    parser = argparse.ArgumentParser(
        description="TV News Screener — extract stock signals from TradingView news"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--bullish",  action="store_true",  help="Show bullish (BUY) signals only")
    g.add_argument("--bearish",  action="store_true",  help="Show bearish (PUT) signals only")
    g.add_argument("--all",      action="store_true",  help="Show both bullish and bearish")

    parser.add_argument("--providers",          default=None,
                        help="Comma-separated providers, e.g. quartr,reuters")
    parser.add_argument("--max-age-hours",      type=float, default=24.0,
                        help="Ignore articles older than N hours (default 24)")
    parser.add_argument("--bullish-threshold",  type=float, default=0.35,
                        help="Min VADER compound for BUY signal (default 0.35)")
    parser.add_argument("--bearish-threshold",  type=float, default=-0.35,
                        help="Max VADER compound for BEAR signal (default -0.35)")
    parser.add_argument("--min-articles",       type=int,   default=1,
                        help="Minimum articles per symbol (default 1)")
    parser.add_argument("--fetch-body-n",       type=int,   default=20,
                        help="Fetch story body for top N articles per provider (default 20)")
    parser.add_argument("--exchange-filter",    default=None,
                        help="Comma-separated exchanges, e.g. NASDAQ,NYSE  (default: all)")
    parser.add_argument("--sentiment",          choices=["vader", "finbert", "both"],
                        default="vader",
                        help="Sentiment backend: vader (fast), finbert (accurate), both (avg)")
    args = parser.parse_args()

    prov_list  = [p.strip() for p in args.providers.split(",") if p.strip()] \
                 if args.providers else None
    exch_filter = [e.strip().upper() for e in args.exchange_filter.split(",") if e.strip()] \
                  if args.exchange_filter else None

    print(f"\nScanning TradingView news "
          f"(providers={prov_list or _DEFAULT_PROVIDERS}, "
          f"max_age={args.max_age_hours}h) ...\n")

    bullish_fires, bearish_fires = news_to_fires(
        providers=prov_list,
        max_age_hours=args.max_age_hours,
        bullish_threshold=args.bullish_threshold,
        bearish_threshold=args.bearish_threshold,
        min_articles=args.min_articles,
        fetch_body_n=args.fetch_body_n,
        exchange_filter=exch_filter,
        sentiment_backend=args.sentiment,
    )

    SEP = "─" * 70

    if args.bullish or args.all:
        print(f"{SEP}")
        print(f"  BULLISH signals ({len(bullish_fires)} symbols)")
        print(f"{SEP}")
        if not bullish_fires:
            print("  (none above threshold)")
        for f in bullish_fires:
            score_str  = f"{f['_news_compound']:+.3f}"
            prov_str   = ",".join(f.get("_news_providers") or [])
            art_str    = f"articles={f['_news_articles']}"
            print(f"  {f['pro_symbol']:<30s}  compound={score_str}  {art_str}  [{prov_str}]")
            for t in (f.get("_news_titles") or [])[:2]:
                print(f"    » {t[:80]}")
        print()

    if args.bearish or args.all:
        print(f"{SEP}")
        print(f"  BEARISH / PUT candidates ({len(bearish_fires)} symbols)")
        print(f"{SEP}")
        if not bearish_fires:
            print("  (none below threshold)")
        for f in bearish_fires:
            score_str = f"{f['_news_compound']:+.3f}"
            prov_str  = ",".join(f.get("_news_providers") or [])
            art_str   = f"articles={f['_news_articles']}"
            print(f"  {f['pro_symbol']:<30s}  compound={score_str}  {art_str}  [{prov_str}]")
            for t in (f.get("_news_titles") or [])[:2]:
                print(f"    » {t[:80]}")
        print()

    meta = news_scan_meta(
        bullish_fires, bearish_fires,
        prov_list, args.max_age_hours,
        args.bullish_threshold, args.bearish_threshold,
    )
    print(f"  VADER available : {meta['vader_available']}")
    print(f"  TV News API     : {meta['tv_news_available']}")
    print(f"  Total signals   : {meta['total_signals']}  "
          f"({meta['bullish_signals']} BUY  +  {meta['bearish_signals']} BEAR)")
    if meta["total_signals"] == 0 and meta.get("tv_news_available"):
        print(
            "\n  WARNING: TV News returned 0 scored signals across all providers."
            "\n     Possible causes:"
            "\n       - Expired TV cookie (tradingview/cookie) — refresh sessionid + sessionid_sign"
            "\n       - No recent articles within --max-age-hours window"
            "\n     Refresh: Browser DevTools -> Network -> any TV request -> Copy Cookie header"
        )

    # ── Generate HTML report ────────────────────────────────────────────────────
    mode_str    = "all" if args.all else ("bullish" if args.bullish else "bearish")
    report_path = None
    try:
        report_path = _build_screener_html_report(
            bullish_fires, bearish_fires, mode_str, meta, prov_list, args.max_age_hours
        )
        print(f"\n  Report written  : {report_path}")
    except Exception as _e:
        print(f"\n  [report] Could not generate HTML: {_e}")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "reports"))
        from report_db import save_news_screener_snapshot, archive_report as _arc
        save_news_screener_snapshot(bullish_fires, bearish_fires, meta)
        if report_path is not None:
            _arc(report_path)
    except Exception as _e:
        print(f"  [report_db] news_screener snapshot skipped: {_e}")
    print()


if __name__ == "__main__":
    _cli()
