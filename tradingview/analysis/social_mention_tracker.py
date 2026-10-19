"""F15 — Social Media Mention Spike Detector.

Tracks social mention velocity using configurable data sources.
Alerts on 40%+ spikes in 10-day rolling mention volume.
"""
from __future__ import annotations

import json
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DEFAULT_OUT = ROOT / "reports" / "social_mentions.html"


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
class MentionData:
    """Time series of mention counts for a symbol."""
    symbol: str
    daily_counts: list[int]   # last N days of mention counts (newest last)
    source: str = "aggregate" # "google_trends", "reddit", "stocktwits", etc.


@dataclass
class MentionSpike:
    """Detected mention velocity spike for a stock."""
    symbol: str
    current_count: int        # today's mention count
    avg_10d: float            # 10-day average
    velocity_pct: float       # (current - avg) / avg * 100
    is_spike: bool = False    # velocity_pct >= spike_threshold
    source: str = ""
    signal: str = ""          # "spike_up", "spike_down", "normal"


def compute_velocity(mention: MentionData, window: int = 10) -> MentionSpike:
    """Compute mention velocity vs rolling average.

    Args:
        mention: daily mention time series.
        window: rolling window days (default 10).
    """
    counts = mention.daily_counts
    if not counts:
        return MentionSpike(
            symbol=mention.symbol, current_count=0, avg_10d=0,
            velocity_pct=0, source=mention.source, signal="no_data",
        )

    current = counts[-1]
    lookback = counts[-window:] if len(counts) >= window else counts
    avg = sum(lookback) / len(lookback) if lookback else 0

    if avg > 0:
        velocity = ((current - avg) / avg) * 100
    elif current > 0:
        velocity = 100.0  # from 0 to any = 100% spike
    else:
        velocity = 0.0

    return MentionSpike(
        symbol=mention.symbol,
        current_count=current,
        avg_10d=round(avg, 1),
        velocity_pct=round(velocity, 1),
        source=mention.source,
    )


def detect_spikes(
    mentions: list[MentionData],
    spike_threshold: float = 40.0,
    window: int = 10,
) -> list[MentionSpike]:
    """Detect mention velocity spikes across all symbols.

    Args:
        mentions: list of per-symbol mention time series.
        spike_threshold: minimum velocity_pct to classify as spike.
        window: rolling window days.

    Returns:
        list of MentionSpike sorted by velocity_pct descending.
    """
    results: list[MentionSpike] = []
    for m in mentions:
        spike = compute_velocity(m, window)
        if spike.velocity_pct >= spike_threshold:
            spike.is_spike = True
            spike.signal = "spike_up"
        elif spike.velocity_pct <= -spike_threshold:
            spike.is_spike = True
            spike.signal = "spike_down"
        else:
            spike.signal = "normal"
        results.append(spike)

    results.sort(key=lambda s: abs(s.velocity_pct), reverse=True)
    return results


def build_mention_data(
    positions: list[dict[str, Any]],
    raw_data: dict[str, list[int]] | None = None,
) -> list[MentionData]:
    """Build mention time series from external data source.

    Args:
        positions: portfolio positions (to filter symbols we care about).
        raw_data: dict keyed by symbol → list of daily counts (last N days).
    """
    our_symbols = {p.get("symbol", "").upper() for p in positions if p.get("symbol")}
    data = raw_data or {}
    results: list[MentionData] = []

    for sym in sorted(our_symbols):
        counts = data.get(sym, [])
        if counts:
            results.append(MentionData(symbol=sym, daily_counts=counts))

    return results


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    spikes: list[MentionSpike],
    out: Path | str | None = None,
) -> str:
    active_spikes = [s for s in spikes if s.is_spike]
    up_spikes = sum(1 for s in active_spikes if s.signal == "spike_up")
    down_spikes = sum(1 for s in active_spikes if s.signal == "spike_down")

    rows = ""
    for s in spikes[:50]:
        vel_cls = "neg" if s.velocity_pct >= 40 else "pos" if s.velocity_pct <= -40 else ""
        badge = ""
        if s.signal == "spike_up":
            badge = "<span style='color:#d29922'>🔥 SPIKE</span>"
        elif s.signal == "spike_down":
            badge = "<span style='color:#58a6ff'>📉 DROP</span>"
        rows += (
            f"<tr>"
            f"<td><strong>{s.symbol}</strong> {badge}</td>"
            f"<td class='num'>{s.current_count:,}</td>"
            f"<td class='num'>{s.avg_10d:,.1f}</td>"
            f"<td class='num {vel_cls}'>{s.velocity_pct:+.1f}%</td>"
            f"<td>{s.source}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Social Mention Spike Detector</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
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
<h1>Social Mention Spike Detector</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(spikes)}</div><div class="lbl">Symbols</div></div>
  <div class="stat"><div class="val" style="color:#d29922">{up_spikes}</div><div class="lbl">Spike Up</div></div>
  <div class="stat"><div class="val" style="color:#58a6ff">{down_spikes}</div><div class="lbl">Spike Down</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Today</th><th>10D Avg</th>
<th>Velocity</th><th>Source</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="5" style="text-align:center;color:#8b949e">No mention data available</td></tr>'}
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
        parser = argparse.ArgumentParser(description="Social Mention Spike Detector")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        parser.add_argument("--threshold", type=float, default=40.0)
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    mentions = build_mention_data(positions)
    spikes = detect_spikes(mentions, args.threshold)
    generate_html(spikes, args.out)
    active = [s for s in spikes if s.is_spike]
    print(f"Social mentions: {len(active)} spikes detected out of {len(spikes)} symbols")


if __name__ == "__main__":
    main()
