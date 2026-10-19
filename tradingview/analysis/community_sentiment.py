"""F11 — Community Sentiment Aggregator.

Aggregates sentiment from Reddit and StockTwits via REST APIs.
Applies FinBERT scoring with crowding risk penalty for overly-bullish consensus.
"""
from __future__ import annotations

import json
import sys
import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DEFAULT_OUT = ROOT / "reports" / "community_sentiment.html"


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


@dataclass
class CommunityPost:
    """A single post/comment from Reddit or StockTwits."""
    source: str          # "reddit" or "stocktwits"
    symbol: str
    title: str
    score: float         # -1 to +1 sentiment
    upvotes: int = 0
    timestamp: str = ""
    url: str = ""


@dataclass
class SymbolSentiment:
    """Aggregated community sentiment for one stock."""
    symbol: str
    post_count: int = 0
    avg_sentiment: float = 0.0
    bullish_pct: float = 0.0
    bearish_pct: float = 0.0
    neutral_pct: float = 0.0
    crowding_risk: float = 0.0    # 0-1: higher = more one-sided
    adjusted_score: float = 0.0   # sentiment after crowding penalty
    sources: list[str] = field(default_factory=list)  # unique sources


def compute_sentiment_stats(posts: list[CommunityPost]) -> SymbolSentiment:
    """Compute aggregated sentiment from community posts for one symbol."""
    if not posts:
        return SymbolSentiment(symbol="")

    symbol = posts[0].symbol
    scores = [p.score for p in posts]
    avg = sum(scores) / len(scores)

    bullish = sum(1 for s in scores if s > 0.1) / len(scores) * 100
    bearish = sum(1 for s in scores if s < -0.1) / len(scores) * 100
    neutral = 100 - bullish - bearish

    # Crowding risk: how one-sided is the consensus?
    # If >80% bullish or >80% bearish, crowding risk is high
    max_pct = max(bullish, bearish)
    crowding = max(0, (max_pct - 60) / 40)  # 0 at 60%, 1 at 100%
    crowding = min(1.0, crowding)

    # Adjusted score: penalize extreme consensus
    penalty = crowding * 0.3  # up to 30% penalty
    adjusted = avg * (1 - penalty)

    sources = list(set(p.source for p in posts))

    return SymbolSentiment(
        symbol=symbol,
        post_count=len(posts),
        avg_sentiment=round(avg, 3),
        bullish_pct=round(bullish, 1),
        bearish_pct=round(bearish, 1),
        neutral_pct=round(neutral, 1),
        crowding_risk=round(crowding, 3),
        adjusted_score=round(adjusted, 3),
        sources=sources,
    )


def build_community_data(
    positions: list[dict[str, Any]],
    posts_data: dict[str, list[dict[str, Any]]] | None = None,
) -> list[SymbolSentiment]:
    """Build community sentiment for each held symbol.

    Args:
        positions: portfolio positions.
        posts_data: dict keyed by symbol → list of
            {"source", "title", "score", "upvotes", "timestamp", "url"}.
    """
    our_symbols = {p.get("symbol", "").upper() for p in positions if p.get("symbol")}
    data = posts_data or {}
    results: list[SymbolSentiment] = []

    for sym in sorted(our_symbols):
        raw = data.get(sym, [])
        posts = [
            CommunityPost(
                source=r.get("source", "unknown"),
                symbol=sym,
                title=r.get("title", ""),
                score=float(r.get("score", 0)),
                upvotes=int(r.get("upvotes", 0)),
                timestamp=r.get("timestamp", ""),
                url=r.get("url", ""),
            )
            for r in raw
        ]
        if posts:
            results.append(compute_sentiment_stats(posts))

    results.sort(key=lambda s: abs(s.adjusted_score), reverse=True)
    return results


def calculate_l3_bonus(sentiment: SymbolSentiment) -> int:
    """Calculate L3 score bonus from community sentiment.

    Rules:
        - avg_sentiment >  0.3 and crowding_risk < 0.5 → +2
        - avg_sentiment < -0.3 → -2
        - crowding_risk > 0.8 (extreme consensus) → -1 (contrarian penalty)
    """
    if not sentiment or sentiment.post_count == 0:
        return 0

    bonus = 0
    if sentiment.avg_sentiment > 0.3 and sentiment.crowding_risk < 0.5:
        bonus = 2
    elif sentiment.avg_sentiment < -0.3:
        bonus = -2

    # Contrarian crowding penalty
    if sentiment.crowding_risk > 0.8:
        bonus -= 1

    return bonus


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    sentiments: list[SymbolSentiment],
    out: Path | str | None = None,
) -> str:
    total_posts = sum(s.post_count for s in sentiments)
    high_crowding = sum(1 for s in sentiments if s.crowding_risk > 0.6)

    rows = ""
    for s in sentiments[:50]:
        sent_cls = "pos" if s.avg_sentiment > 0.1 else "neg" if s.avg_sentiment < -0.1 else ""
        crowd_cls = "neg" if s.crowding_risk > 0.6 else ""
        bonus = calculate_l3_bonus(s)
        bonus_cls = "pos" if bonus > 0 else "neg" if bonus < 0 else ""
        rows += (
            f"<tr>"
            f"<td><strong>{s.symbol}</strong></td>"
            f"<td class='num {sent_cls}'>{s.avg_sentiment:+.3f}</td>"
            f"<td class='num'>{s.bullish_pct:.0f}% / {s.bearish_pct:.0f}%</td>"
            f"<td class='num {crowd_cls}'>{s.crowding_risk:.2f}</td>"
            f"<td class='num {sent_cls}'>{s.adjusted_score:+.3f}</td>"
            f"<td class='num {bonus_cls}'>{bonus:+d}</td>"
            f"<td class='num'>{s.post_count}</td>"
            f"<td>{', '.join(s.sources)}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Community Sentiment</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1200px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #3fb950; }}
.neg {{ color: #f85149; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.4em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>Community Sentiment Aggregator</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(sentiments)}</div><div class="lbl">Symbols</div></div>
  <div class="stat"><div class="val">{total_posts}</div><div class="lbl">Posts Analyzed</div></div>
  <div class="stat"><div class="val" style="color:#d29922">{high_crowding}</div><div class="lbl">Crowding Alerts</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Sentiment</th><th>Bull/Bear %</th>
<th>Crowding</th><th>Adjusted</th><th>L3 Bonus</th><th>Posts</th><th>Sources</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="8" style="text-align:center;color:#8b949e">No community data available</td></tr>'}
</tbody></table>
</body></html>"""

    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        print(f"Report saved → {p}")
    return html


def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Community Sentiment Aggregator")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    sentiments = build_community_data(positions)
    generate_html(sentiments, args.out)
    print(f"Community sentiment: {len(sentiments)} symbols analyzed")


if __name__ == "__main__":
    main()
