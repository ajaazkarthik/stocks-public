"""F4 — Earnings Surprise Tracker.

Sources: TV screener fields earnings_per_share_surprise_fq, earnings_per_share_diluted_yoy_growth_fq.
Adds +3 L2 bonus for recent beats. Shows "NVDA: beat 4 consecutive quarters".
When TV data unavailable, can also track manually added earnings.
"""
from __future__ import annotations

import json
import sys
import argparse
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
DEFAULT_OUT = ROOT / "reports" / "earnings_surprise.html"


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load cached positions JSON."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


@dataclass
class EarningsRecord:
    """Earnings surprise data for one symbol."""
    symbol: str
    surprise_pct: float | None = None     # % surprise (positive = beat)
    eps_growth_yoy: float | None = None   # YoY EPS growth %
    consecutive_beats: int = 0
    streak_type: str = "unknown"          # "beat", "miss", "mixed"
    l2_bonus: int = 0                     # scoring bonus
    broker: str = ""


def classify_surprise(surprise_pct: float | None) -> str:
    """Classify a single quarter's surprise."""
    if surprise_pct is None:
        return "unknown"
    if surprise_pct > 0:
        return "beat"
    if surprise_pct < 0:
        return "miss"
    return "inline"


def calculate_l2_bonus(consecutive_beats: int, surprise_pct: float | None) -> int:
    """Calculate L2 scoring bonus for earnings surprises.

    Rules:
    - 1 beat: +1
    - 2 consecutive beats: +2
    - 3+ consecutive beats: +3
    - Big beat (>10% surprise): additional +1
    - Miss: -2
    """
    if surprise_pct is None:
        return 0
    if surprise_pct < 0:
        return -2  # miss penalty
    if consecutive_beats >= 3:
        bonus = 3
    elif consecutive_beats >= 2:
        bonus = 2
    elif consecutive_beats >= 1:
        bonus = 1
    else:
        bonus = 0
    if surprise_pct > 10:
        bonus += 1
    return bonus


def build_earnings_data(
    positions: list[dict[str, Any]],
    screener_data: dict[str, dict[str, Any]] | None = None,
) -> list[EarningsRecord]:
    """Build earnings records from positions + optional TV screener data.

    Args:
        positions: list of position dicts.
        screener_data: optional dict keyed by symbol with TV screener fields:
            - earnings_per_share_surprise_fq
            - earnings_per_share_diluted_yoy_growth_fq

    Returns:
        list of EarningsRecord sorted by l2_bonus descending.
    """
    seen: set[str] = set()
    records: list[EarningsRecord] = []
    tv = screener_data or {}

    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        tv_data = tv.get(sym, {})
        surprise = tv_data.get("earnings_per_share_surprise_fq")
        eps_growth = tv_data.get("earnings_per_share_diluted_yoy_growth_fq")
        consec = tv_data.get("consecutive_beats", 0)

        if surprise is not None:
            try:
                surprise = float(surprise)
            except (ValueError, TypeError):
                surprise = None

        if eps_growth is not None:
            try:
                eps_growth = float(eps_growth)
            except (ValueError, TypeError):
                eps_growth = None

        streak = classify_surprise(surprise)
        bonus = calculate_l2_bonus(consec, surprise)

        records.append(EarningsRecord(
            symbol=sym,
            surprise_pct=surprise,
            eps_growth_yoy=eps_growth,
            consecutive_beats=consec,
            streak_type=streak,
            l2_bonus=bonus,
            broker=pos.get("broker", ""),
        ))

    records.sort(key=lambda r: r.l2_bonus, reverse=True)
    return records


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    records: list[EarningsRecord],
    out: Path | str | None = None,
) -> str:
    """Build an HTML earnings surprise report."""
    beats = sum(1 for r in records if r.streak_type == "beat")
    misses = sum(1 for r in records if r.streak_type == "miss")

    rows = ""
    for r in records:
        cls_surprise = ""
        if r.surprise_pct is not None:
            cls_surprise = "pos" if r.surprise_pct >= 0 else "neg"
        surprise_str = f"{r.surprise_pct:+.1f}%" if r.surprise_pct is not None else "—"
        eps_str = f"{r.eps_growth_yoy:+.1f}%" if r.eps_growth_yoy is not None else "—"

        streak_badge = ""
        if r.consecutive_beats >= 3:
            streak_badge = f"<span style='color:#3fb950'>🔥 {r.consecutive_beats}Q streak</span>"
        elif r.consecutive_beats >= 1:
            streak_badge = f"<span style='color:#3fb950'>{r.consecutive_beats}Q beat</span>"
        elif r.streak_type == "miss":
            streak_badge = "<span style='color:#f85149'>miss</span>"

        bonus_cls = "pos" if r.l2_bonus > 0 else ("neg" if r.l2_bonus < 0 else "")
        rows += (
            f"<tr>"
            f"<td><strong>{r.symbol}</strong></td>"
            f"<td>{r.broker}</td>"
            f"<td class='num {cls_surprise}'>{surprise_str}</td>"
            f"<td class='num'>{eps_str}</td>"
            f"<td>{streak_badge}</td>"
            f"<td class='num {bonus_cls}'>{r.l2_bonus:+d}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Earnings Surprise Tracker</title>
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
<h1>Earnings Surprise Tracker</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(records)}</div><div class="lbl">Symbols Tracked</div></div>
  <div class="stat"><div class="val pos">{beats}</div><div class="lbl">Beat</div></div>
  <div class="stat"><div class="val neg">{misses}</div><div class="lbl">Miss</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Broker</th><th>Surprise</th>
<th>EPS Growth YoY</th><th>Streak</th><th>L2 Bonus</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#8b949e">No earnings data available</td></tr>'}
</tbody></table>
</body></html>"""

    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        print(f"Report saved → {p}")
    return html


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Earnings Surprise Tracker")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    records = build_earnings_data(positions)
    generate_html(records, args.out)

    beats = sum(1 for r in records if r.streak_type == "beat")
    misses = sum(1 for r in records if r.streak_type == "miss")
    print(f"\nEarnings Surprise: {len(records)} symbols — {beats} beats, {misses} misses")


if __name__ == "__main__":
    main()
