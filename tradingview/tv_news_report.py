"""
TradingView Reuters / Dow Jones News Dashboard
===============================================
Fetches authenticated news from TradingView's news API (Reuters, Dow Jones,
MarketWatch and others) and renders a scored HTML dashboard.

Capabilities:
  • Global Reuters / Dow Jones feed   — latest headlines across all markets
  • Per-symbol news                   — any TV symbol (NSE:TCS, NASDAQ:AAPL, ASX:CBA …)
  • VADER sentiment scoring           — each headline labelled Bullish / Bearish / Neutral
  • Full article text                 — extracts body from TradingView AST for first N stories
  • HTML report                       — colour-coded, sortable dashboard
  • JSON export                       — machine-readable dump for downstream enrichers

Usage:
  # Global Reuters feed (last 50 articles, HTML output)
  python tv_news_report.py

  # Symbol-specific news
  python tv_news_report.py --symbols NASDAQ:AAPL NSE:TCS ASX:CBA

  # More articles, different output path
  python tv_news_report.py --limit 100 --out reports/news_2026-04-09.html

  # Fetch full article text for first 5 stories
  python tv_news_report.py --full-text 5

  # JSON dump
  python tv_news_report.py --json-out news_dump.json

  # All providers (not just Reuters + Dow Jones)
  python tv_news_report.py --all-providers
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Resolve package roots ──────────────────────────────────────────────────────
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from tv_data.tv_news import (
        fetch_headlines,
        fetch_reuters_headlines,
        fetch_reuters_for_symbol,
        fetch_story,
        news_to_headlines,
    )
except ImportError as _e:
    sys.exit(f"ERROR: cannot import tv_data.tv_news — {_e}\n"
             "Make sure you run from the tradingview/ directory.")

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
    def _vader_compound(text: str) -> float:
        return _vader.polarity_scores(text)["compound"]
except ImportError:
    sys.exit("ERROR: pip install vaderSentiment")


# ── Constants ─────────────────────────────────────────────────────────────────
_ALL_PROVIDERS = [
    "reuters", "dow-jones", "market-watch", "quartr",
    "globenewswire", "sharecast", "dpa_afx",
    "tmx_newsfile", "trading-economics", "zawya", "invezz",
]
_DEFAULT_PROVIDERS = ["reuters", "dow-jones", "quartr", "market-watch"]
_DEFAULT_OUT = _HERE / "reports" / "tv_news_report.html"

# ── Domain-specific negative event patterns (override VADER) ─────────────────
# VADER mis-scores legal/penalty headlines because words like "agrees", "resolve",
# and "settles" are positive in general language but signal bad news in finance.
_NEGATIVE_FINANCIAL_PATTERNS = re.compile(
    r'(?:pay(?:s|ing|ment)?\s+\$?[\d.,]+\s*(?:million|billion)'
    r'|settle(?:s|d|ment)'
    r'|lawsuit'
    r'|allega(?:tion|tions)'
    r'|discriminat(?:ion|ory)'
    r'|penalt(?:y|ies)'
    r'|fine(?:d|s)?\b'
    r'|SEC\s+(?:charg|fine|sanction)'
    r'|DOJ\s+(?:charg|fine|investigat)'
    r'|class.action'
    r'|whistleblower'
    r'|misconduct'
    r'|fraud(?:ulent)?'
    r'|bribery'
    r'|violat(?:ion|ions)'
    r'|injunction'
    r'|sanction(?:s)?'
    r'|resolv\w+\s+(?:allega|charg)'
    r'|plead(?:s|ed)?\s+(?:guilty|no\s+contest)'
    r'|criminal\s+charg)',
    re.IGNORECASE,
)

def _score_headline(title: str) -> dict:
    compound = _vader_compound(title)
    # Domain correction: legal/penalty/settlement headlines are negative for the company
    # even when VADER scores them positive due to words like "agrees", "resolve", "settles".
    if _NEGATIVE_FINANCIAL_PATTERNS.search(title):
        compound = max(-1.0, compound - 0.6)
    if compound > 0.35:
        label, colour = "Very Bullish", "#0d6e2e"
    elif compound > 0.10:
        label, colour = "Bullish", "#27ae60"
    elif compound > 0.02:
        label, colour = "Slightly Bullish", "#82c891"
    elif compound >= -0.02:
        label, colour = "Neutral", "#888888"
    elif compound >= -0.10:
        label, colour = "Slightly Bearish", "#e09080"
    elif compound >= -0.35:
        label, colour = "Bearish", "#e74c3c"
    else:
        label, colour = "Very Bearish", "#922b21"
    return {"compound": round(compound, 4), "label": label, "colour": colour}


def _ts_str(unix_ts) -> str:
    if not unix_ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(unix_ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "—"


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_global(providers: list[str], limit: int) -> list[dict]:
    """Fetch global headlines across specified providers, deduplicated."""
    seen: set[str] = set()
    articles: list[dict] = []
    for prov in providers:
        items = fetch_headlines(provider=prov, limit=limit)
        for it in items:
            aid = it.get("id", "")
            if aid not in seen:
                seen.add(aid)
                it["_provider_queried"] = prov
                articles.append(it)
    # Sort newest first
    articles.sort(key=lambda x: x.get("published", 0), reverse=True)
    return articles[:limit]


def _fetch_for_symbols(symbols: list[str], providers: list[str], limit: int) -> dict[str, list[dict]]:
    """Fetch news per symbol. Returns {symbol: [article_dicts]}."""
    result: dict[str, list[dict]] = {}
    for sym in symbols:
        seen: set[str] = set()
        arts: list[dict] = []
        for prov in providers:
            items = fetch_headlines(symbol=sym, provider=prov, limit=limit)
            for it in items:
                aid = it.get("id", "")
                if aid not in seen:
                    seen.add(aid)
                    it["_symbol_queried"] = sym
                    arts.append(it)
        arts.sort(key=lambda x: x.get("published", 0), reverse=True)
        result[sym] = arts[:limit]
    return result


def _enrich_sentiment(articles: list[dict]) -> list[dict]:
    """Add sentiment score to each article dict in-place."""
    for art in articles:
        title = art.get("title", "")
        art["_sentiment"] = _score_headline(title) if title else {
            "compound": 0.0, "label": "Neutral", "colour": "#888888"}
    return articles


def _maybe_fetch_full_text(articles: list[dict], n: int) -> list[dict]:
    """Fetch full article body for the first n articles."""
    for i, art in enumerate(articles[:n]):
        story_id = art.get("id", "")
        if story_id:
            print(f"  Fetching full text [{i+1}/{n}]: {art.get('title','')[:60]}…")
            story = fetch_story(story_id)
            art["_short_desc"] = story.get("shortDescription", "")
            art["_full_text"]  = story.get("full_text", "")
            time.sleep(0.3)   # polite pacing
    return articles


# ── HTML building ─────────────────────────────────────────────────────────────

_HTML_HEADER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f0f12; color: #e0e0e0; margin: 0; padding: 16px; }}
  h1   {{ color: #f0c040; margin-bottom: 4px; }}
  .meta {{ color: #888; font-size: 13px; margin-bottom: 20px; }}
  .section-title {{ color: #a0c8ff; font-size: 18px; border-bottom: 1px solid #333;
                    padding-bottom: 4px; margin-top: 28px; margin-bottom: 12px; }}
  .summary-bar {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
  .stat {{ background: #1a1a22; border-radius: 8px; padding: 12px 20px;
           min-width: 120px; text-align: center; }}
  .stat-num {{ font-size: 24px; font-weight: bold; }}
  .stat-lbl {{ font-size: 12px; color: #888; margin-top: 4px; }}
  table  {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th     {{ background: #1e1e28; color: #a0c8ff; padding: 8px 10px; text-align: left;
            position: sticky; top: 0; }}
  td     {{ padding: 7px 10px; border-bottom: 1px solid #1e1e26; vertical-align: top; }}
  tr:hover td {{ background: #181820; }}
  .sent  {{ border-radius: 4px; padding: 2px 7px; font-size: 11px;
            font-weight: bold; color: #fff; white-space: nowrap; }}
  .prov  {{ background: #2a2a3a; border-radius: 4px; padding: 2px 6px;
            font-size: 11px; color: #aaa; }}
  .sym-tag {{ background: #1a2a3a; border-radius: 4px; padding: 2px 6px;
              font-size: 11px; color: #7ac; margin-right: 3px; }}
  .snippet {{ color: #999; font-size: 12px; margin-top: 4px; max-width: 620px;
              display: -webkit-box; -webkit-line-clamp: 2;
              -webkit-box-orient: vertical; overflow: hidden; }}
  .ts    {{ color: #666; font-size: 11px; white-space: nowrap; }}
  a      {{ color: #7ac; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .no-data {{ color: #555; font-style: italic; padding: 20px 0; }}
</style>
</head>
<body>
<h1>📰 TradingView News Dashboard</h1>
<div class="meta">Generated {generated} &nbsp;·&nbsp; Sources: {sources}</div>
"""

_HTML_FOOTER = """
<div style="color:#444;font-size:12px;margin-top:40px;padding-top:12px;border-top:1px solid #222">
  Data sourced from TradingView news feed (Reuters, Dow Jones, MarketWatch &amp; others).
  Sentiment scored via VADER. For informational purposes only.
</div>
</body></html>"""


def _sentiment_pill(sent: dict) -> str:
    return (f'<span class="sent" style="background:{sent["colour"]}">'
            f'{sent["label"]} ({sent["compound"]:+.2f})</span>')


def _provider_pill(prov: str) -> str:
    return f'<span class="prov">{prov}</span>'


def _sym_tags(syms: list[dict]) -> str:
    return "".join(f'<span class="sym-tag">{s.get("symbol","")}</span>'
                   for s in syms[:5])


def _article_row(art: dict, show_symbol_col: bool = False) -> str:
    sent     = art.get("_sentiment", {"compound": 0.0, "label": "Neutral", "colour": "#888"})
    title    = art.get("title", "—")
    source   = art.get("source") or art.get("_provider_queried", "")
    ts       = _ts_str(art.get("published"))
    rel_syms = art.get("relatedSymbols", [])
    path     = art.get("storyPath", "")
    link_url = f"https://www.tradingview.com{path}" if path else ""
    title_html = (f'<a href="{link_url}" target="_blank">{title}</a>'
                  if link_url else title)

    snippet_html = ""
    short_desc   = art.get("_short_desc", "")
    full_text    = art.get("_full_text", "")
    preview      = short_desc or (full_text[:280] if full_text else "")
    if preview:
        snippet_html = f'<div class="snippet">{preview}</div>'

    sym_col = f'<td>{_sym_tags(rel_syms)}</td>' if show_symbol_col else ""
    return (f"<tr>"
            f"<td class='ts'>{ts}</td>"
            f"<td>{title_html}{snippet_html}</td>"
            f"<td>{_sentiment_pill(sent)}</td>"
            f"<td>{_provider_pill(source)}</td>"
            f"{sym_col}"
            f"</tr>")


def _articles_table(articles: list[dict], show_symbol_col: bool = False) -> str:
    if not articles:
        return '<p class="no-data">No articles found.</p>'
    sym_th = "<th>Related Symbols</th>" if show_symbol_col else ""
    rows = "\n".join(_article_row(a, show_symbol_col) for a in articles)
    return (f'<table><thead><tr>'
            f'<th>Published</th><th>Title</th>'
            f'<th>Sentiment</th><th>Provider</th>{sym_th}'
            f'</tr></thead><tbody>\n{rows}\n</tbody></table>')


def _summary_stats(articles: list[dict]) -> str:
    if not articles:
        return ""
    compounds = [a["_sentiment"]["compound"] for a in articles if "_sentiment" in a]
    if not compounds:
        return ""
    mean = sum(compounds) / len(compounds)
    bull = sum(1 for c in compounds if c > 0.02)
    bear = sum(1 for c in compounds if c < -0.02)
    neut = len(compounds) - bull - bear
    mean_label = (
        "Very Bullish" if mean > 0.35 else
        "Bullish"      if mean > 0.10 else
        "Slightly Bullish" if mean > 0.02 else
        "Neutral"      if mean >= -0.02 else
        "Slightly Bearish" if mean >= -0.10 else
        "Bearish"      if mean >= -0.35 else "Very Bearish"
    )
    colour = ("#27ae60" if mean > 0.10 else "#e74c3c" if mean < -0.10 else "#888")
    return (f'<div class="summary-bar">'
            f'<div class="stat"><div class="stat-num">{len(articles)}</div>'
            f'<div class="stat-lbl">Articles</div></div>'
            f'<div class="stat"><div class="stat-num" style="color:{colour}">'
            f'{mean_label}</div><div class="stat-lbl">Overall Sentiment ({mean:+.3f})</div></div>'
            f'<div class="stat"><div class="stat-num" style="color:#27ae60">{bull}</div>'
            f'<div class="stat-lbl">Bullish</div></div>'
            f'<div class="stat"><div class="stat-num" style="color:#888">{neut}</div>'
            f'<div class="stat-lbl">Neutral</div></div>'
            f'<div class="stat"><div class="stat-num" style="color:#e74c3c">{bear}</div>'
            f'<div class="stat-lbl">Bearish</div></div>'
            f'</div>')


def build_html(
    global_articles: list[dict],
    symbol_articles: dict[str, list[dict]],
    providers: list[str],
    title: str,
) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    sources   = ", ".join(providers)

    html_parts = [_HTML_HEADER.format(
        title=title, generated=generated, sources=sources)]

    # ── Global section ────────────────────────────────────────────────────────
    if global_articles:
        html_parts.append('<div class="section-title">🌍 Global Feed</div>')
        html_parts.append(_summary_stats(global_articles))
        html_parts.append(_articles_table(global_articles, show_symbol_col=True))

    # ── Per-symbol sections ───────────────────────────────────────────────────
    for sym, arts in symbol_articles.items():
        html_parts.append(f'<div class="section-title">🔍 {sym}</div>')
        if arts:
            html_parts.append(_summary_stats(arts))
            html_parts.append(_articles_table(arts, show_symbol_col=False))
        else:
            html_parts.append('<p class="no-data">No articles found for this symbol.</p>')

    html_parts.append(_HTML_FOOTER)
    return "\n".join(html_parts)


# ── JSON export ───────────────────────────────────────────────────────────────

def _to_json_export(global_articles: list[dict], symbol_articles: dict[str, list[dict]]) -> dict:
    def _clean(arts: list[dict]) -> list[dict]:
        out = []
        for a in arts:
            out.append({
                "id":             a.get("id"),
                "title":          a.get("title"),
                "source":         a.get("source") or a.get("_provider_queried"),
                "published":      a.get("published"),
                "published_str":  _ts_str(a.get("published")),
                "storyPath":      a.get("storyPath"),
                "sentiment":      a.get("_sentiment", {}),
                "relatedSymbols": [s.get("symbol") for s in a.get("relatedSymbols", [])],
                "shortDesc":      a.get("_short_desc", ""),
                "fullText":       a.get("_full_text", ""),
            })
        return out

    return {
        "generated": datetime.now().isoformat(),
        "global":    _clean(global_articles),
        "symbols":   {sym: _clean(arts) for sym, arts in symbol_articles.items()},
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cli():
    p = argparse.ArgumentParser(
        description="Fetch TradingView Reuters/Dow Jones news and generate a scored dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--symbols", nargs="*", default=[],
                   help="TV symbols to fetch news for, e.g. NASDAQ:AAPL NSE:TCS")
    p.add_argument("--limit", type=int, default=50,
                   help="Max articles per provider/symbol (default: 50)")
    p.add_argument("--out", default=str(_DEFAULT_OUT),
                   help="HTML output path (default: reports/tv_news_report.html)")
    p.add_argument("--json-out", default="",
                   help="Also write a JSON export to this path")
    p.add_argument("--full-text", type=int, default=0, metavar="N",
                   help="Fetch and show full article body for first N stories")
    p.add_argument("--all-providers", action="store_true",
                   help="Use all available providers instead of just Reuters + Dow Jones")
    p.add_argument("--providers", nargs="*", default=None,
                   help="Explicit provider list, e.g. --providers reuters dow-jones market-watch")
    p.add_argument("--no-global", action="store_true",
                   help="Skip global feed; only fetch per-symbol news")
    p.add_argument("--from-actionable", action="store_true",
                   help="Auto-load symbols from actionable_log.json — same BUY signals as options analysis")
    args = p.parse_args()

    # ── Load symbols from actionable_log if requested ─────────────────────────
    if args.from_actionable and not args.symbols:
        _log_candidates = [
            _HERE / "actionable_log.json",
            _HERE / "reports" / "actionable_log.json",
        ]
        _existing = [p for p in _log_candidates if p.exists()]
        if _existing:
            _log_path = max(_existing, key=lambda p: p.stat().st_mtime)
            try:
                _data = json.loads(_log_path.read_text(encoding="utf-8"))
                _entries = _data if isinstance(_data, list) else _data.get("signals", [])
                _seen: set[str] = set()
                for _e in _entries:
                    _sym = (_e.get("tv_symbol") or _e.get("symbol") or
                            _e.get("pro_symbol") or "").strip()
                    if _sym and _sym.upper() not in _seen:
                        args.symbols.append(_sym)
                        _seen.add(_sym.upper())
                print(f"  Loaded {len(args.symbols)} symbols from {_log_path.name}")
            except Exception as _exc:
                print(f"  Warning: could not load actionable_log.json: {_exc}")
        else:
            print("  Warning: actionable_log.json not found — run the webhook server first")

    providers = (
        _ALL_PROVIDERS if args.all_providers else
        args.providers if args.providers else
        _DEFAULT_PROVIDERS
    )

    print(f"TradingView News Dashboard")
    print(f"  providers : {', '.join(providers)}")
    print(f"  symbols   : {args.symbols or '(global feed only)'}")
    print(f"  limit     : {args.limit}")
    print()

    # ── Global feed ──────────────────────────────────────────────────────────
    global_articles: list[dict] = []
    if not args.no_global:
        print("Fetching global feed…")
        global_articles = _fetch_global(providers, args.limit)
        _enrich_sentiment(global_articles)
        print(f"  → {len(global_articles)} articles")

        if args.full_text > 0 and global_articles:
            print(f"Fetching full text for first {args.full_text} global articles…")
            _maybe_fetch_full_text(global_articles, args.full_text)

    # ── Per-symbol ────────────────────────────────────────────────────────────
    symbol_articles: dict[str, list[dict]] = {}
    if args.symbols:
        print("Fetching per-symbol news…")
        symbol_articles = _fetch_for_symbols(args.symbols, providers, args.limit)
        for sym, arts in symbol_articles.items():
            _enrich_sentiment(arts)
            print(f"  {sym}: {len(arts)} articles")

        if args.full_text > 0:
            for sym, arts in symbol_articles.items():
                print(f"Fetching full text for {sym}…")
                _maybe_fetch_full_text(arts, args.full_text)

    # ── Write HTML ────────────────────────────────────────────────────────────
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html(
        global_articles, symbol_articles, providers,
        title=f"TV News — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    )
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    print(f"\n✓ HTML report written: {out_path}")

    # ── Archive report ───────────────────────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "reports"))
        from report_db import archive_report as _arc
        _arc(out_path)
    except Exception as _e:
        print(f"  [archive] tv_news_report archive skipped: {_e}")

    # ── Write JSON ────────────────────────────────────────────────────────────
    if args.json_out:
        jp = Path(args.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        export = _to_json_export(global_articles, symbol_articles)
        jp.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"✓ JSON export written: {jp}")

    # ── Terminal summary ──────────────────────────────────────────────────────
    all_arts = global_articles + [a for arts in symbol_articles.values() for a in arts]
    if all_arts:
        compounds = [a["_sentiment"]["compound"] for a in all_arts if "_sentiment" in a]
        mean = sum(compounds) / len(compounds) if compounds else 0.0
        bull = sum(1 for c in compounds if c > 0.02)
        bear = sum(1 for c in compounds if c < -0.02)
        print(f"\n  Total articles : {len(all_arts)}")
        print(f"  Mean sentiment : {mean:+.4f}")
        print(f"  Bullish        : {bull}")
        print(f"  Bearish        : {bear}")
        print(f"  Neutral        : {len(compounds) - bull - bear}")


if __name__ == "__main__":
    _cli()
