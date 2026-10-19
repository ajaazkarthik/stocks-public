"""
TradingView Economic Calendar — fetch high-impact macro events.

Endpoint: economic-calendar.tradingview.com/events
Returns central bank decisions, CPI, PPI, NFP, GDP and other
high-impact economic events for the next N days.

Usage (standalone):
  python tv_economic_calendar.py
  python tv_economic_calendar.py --days 14 --currencies USD,EUR,JPY

Usage (library):
  from tradingview.tv_data.tv_economic_calendar import fetch_economic_events
  events = fetch_economic_events(days=7, currencies=["USD","EUR"])
"""
from __future__ import annotations

import sys
import json
import argparse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TV_DIR = Path(__file__).parent.parent  # tradingview/

# ─── Calendar endpoint ───────────────────────────────────────────────────────
_CALENDAR_URL = "https://economic-calendar.tradingview.com/events"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

# Impact levels: 0=holiday, 1=low, 2=medium, 3=high
_IMPACT_LABELS = {0: "Holiday", 1: "Low", 2: "Medium", 3: "High"}

# Key central bank event keywords for warnings
_CB_KEYWORDS = [
    "interest rate", "rate decision", "fomc", "monetary policy",
    "fed", "ecb", "boj", "rba", "boe", "rbnz", "boc", "snb",
]

_VOLATILITY_KEYWORDS = [
    "non-farm", "nonfarm", "nfp", "cpi", "ppi", "gdp",
    "unemployment", "retail sales", "pmi", "ism",
]


# ─── Public API ───────────────────────────────────────────────────────────────

def fetch_economic_events(
    days: int = 7,
    currencies: Optional[list[str]] = None,
    min_impact: int = 2,
    timeout: int = 10,
) -> list[dict]:
    """
    Fetch upcoming economic events from TradingView calendar.

    Args:
        days: Number of days ahead to look (default 7).
        currencies: Filter by currency codes (e.g. ["USD","EUR"]).
                   None = all major currencies.
        min_impact: Minimum impact level (0-3). Default 2 (medium+high).
        timeout: HTTP timeout in seconds.

    Returns:
        List of event dicts sorted by date, each with:
          date, time, title, country, currency, impact, impact_label,
          forecast, previous, actual, is_central_bank, is_high_volatility
    """
    now = datetime.now(timezone.utc)
    from_date = now.strftime("%Y-%m-%dT00:00:00.000Z")
    to_date = (now + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59.999Z")

    params = f"from={from_date}&to={to_date}"
    url = f"{_CALENDAR_URL}?{params}"

    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read())
    except Exception as exc:
        print(f"  [warn] Economic calendar fetch failed: {exc}")
        return []

    if not isinstance(raw, dict):
        # API may return a list directly
        items = raw if isinstance(raw, list) else []
    else:
        items = raw.get("result", raw.get("data", raw.get("events", [])))
        if not isinstance(items, list):
            items = []

    events = []
    for item in items:
        impact = _safe_int(item.get("importance", item.get("impact")))
        if impact < min_impact:
            continue

        currency = (item.get("currency", item.get("country", "")) or "").upper()
        if currencies and currency not in [c.upper() for c in currencies]:
            continue

        title = item.get("title", item.get("event", item.get("name", ""))) or ""
        title_lower = title.lower()

        # Parse date
        date_str = item.get("date", item.get("time", ""))
        event_dt = _parse_date(date_str)
        if event_dt is None:
            continue

        days_away = (event_dt.date() - now.date()).days

        events.append({
            "date": event_dt.strftime("%Y-%m-%d"),
            "time": event_dt.strftime("%H:%M UTC"),
            "title": title,
            "country": item.get("country", currency),
            "currency": currency,
            "impact": impact,
            "impact_label": _IMPACT_LABELS.get(impact, f"Level {impact}"),
            "forecast": item.get("forecast"),
            "previous": item.get("previous", item.get("prior")),
            "actual": item.get("actual"),
            "days_away": days_away,
            "is_central_bank": any(k in title_lower for k in _CB_KEYWORDS),
            "is_high_volatility": any(k in title_lower for k in _VOLATILITY_KEYWORDS),
        })

    events.sort(key=lambda e: e["date"])
    return events


def get_upcoming_warnings(
    days: int = 7,
    currencies: Optional[list[str]] = None,
) -> list[str]:
    """
    Return human-readable warning strings for upcoming high-impact events.
    Suitable for embedding in report headers or console output.

    Example: "⚠️ Fed Rate Decision in 2 days (USD) — elevated volatility expected"
    """
    events = fetch_economic_events(days=days, currencies=currencies, min_impact=3)
    warnings = []
    for e in events:
        if e["is_central_bank"] or e["is_high_volatility"]:
            days_text = (
                "TODAY" if e["days_away"] == 0
                else "TOMORROW" if e["days_away"] == 1
                else f"in {e['days_away']} days"
            )
            icon = "🏦" if e["is_central_bank"] else "⚠️"
            warnings.append(
                f"{icon} {e['title']} {days_text} ({e['currency']}) "
                f"— elevated volatility expected"
            )
    return warnings


def build_calendar_html_fragment(events: list[dict]) -> str:
    """
    Build an HTML fragment (not a full page) for embedding in report headers.
    Shows upcoming high-impact events as a compact warning bar.
    """
    if not events:
        return ""

    high_impact = [e for e in events if e["impact"] >= 3]
    if not high_impact:
        return ""

    rows = ""
    for e in high_impact[:10]:  # cap at 10
        icon = "🏦" if e["is_central_bank"] else "📊" if e["is_high_volatility"] else "📅"
        impact_color = "#ef4444" if e["impact"] >= 3 else "#f59e0b"
        days_badge = (
            '<span style="background:#ef4444;color:#fff;padding:1px 6px;border-radius:3px;font-size:0.75rem">TODAY</span>'
            if e["days_away"] == 0 else
            f'<span style="color:#94a3b8;font-size:0.8rem">{e["days_away"]}d</span>'
        )
        forecast = f"F: {e['forecast']}" if e["forecast"] is not None else ""
        previous = f"P: {e['previous']}" if e["previous"] is not None else ""
        meta = " | ".join(filter(None, [forecast, previous]))

        rows += (
            f'<div style="display:flex;align-items:center;gap:8px;padding:3px 0;'
            f'border-bottom:1px solid #334155">'
            f'<span>{icon}</span>'
            f'<span style="min-width:70px;color:#94a3b8;font-size:0.8rem">{e["date"]}</span>'
            f'<span style="color:{impact_color};font-weight:600;flex:1">{e["title"]}</span>'
            f'<span style="color:#94a3b8;font-size:0.8rem;min-width:45px">{e["currency"]}</span>'
            f'{days_badge}'
            f'<span style="color:#64748b;font-size:0.75rem;min-width:100px">{meta}</span>'
            f'</div>\n'
        )

    return (
        f'<div style="background:#1e1b4b;border:1px solid #4338ca;border-radius:8px;'
        f'padding:10px 16px;margin:8px 0">'
        f'<div style="font-weight:600;color:#a5b4fc;margin-bottom:6px;font-size:0.9rem">'
        f'📅 Economic Calendar — Next 7 Days ({len(high_impact)} high-impact event{"s" if len(high_impact) != 1 else ""})</div>'
        f'{rows}</div>'
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _parse_date(s) -> Optional[datetime]:
    """Parse various date formats returned by TV calendar API."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(str(s), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    currencies = [c.strip().upper() for c in args.currencies.split(",") if c.strip()] if args.currencies else None
    events = fetch_economic_events(days=args.days, currencies=currencies, min_impact=args.min_impact)

    print(f"\n  Economic Calendar — next {args.days} days")
    print(f"  {'─' * 50}")

    if not events:
        print("  No events found (API may be unavailable)")
        if getattr(args, "out", ""):
            # Write an empty-state page so the file exists
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(build_full_html_page([], args.days, args.min_impact), encoding="utf-8")
            print(f"  Empty calendar page → {out}")
        return

    for e in events:
        icon = "🏦" if e["is_central_bank"] else "📊" if e["is_high_volatility"] else "📅"
        impact_bar = "█" * e["impact"] + "░" * (3 - e["impact"])
        forecast_str = f"  F={e['forecast']}" if e["forecast"] is not None else ""
        prev_str = f"  P={e['previous']}" if e["previous"] is not None else ""
        print(
            f"  {icon} {e['date']} {e['time']}  [{impact_bar}]  "
            f"{e['currency']:4s}  {e['title']}{forecast_str}{prev_str}"
        )

    # Print warnings
    warnings = get_upcoming_warnings(days=args.days, currencies=currencies)
    if warnings:
        print(f"\n  {'─' * 50}")
        for w in warnings:
            print(f"  {w}")

    print(f"\n  Total: {len(events)} events (impact ≥ {args.min_impact})")

    # Write HTML report if --out was specified
    if getattr(args, "out", ""):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_full_html_page(events, args.days, args.min_impact), encoding="utf-8")
        print(f"  HTML report → {out}")


def build_full_html_page(events: list[dict], days: int, min_impact: int) -> str:
    """Build a standalone HTML report page for the economic calendar."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_html = ""
    for e in events:
        icon = "🏦" if e["is_central_bank"] else "📊" if e["is_high_volatility"] else "📅"
        impact_color = "#ef4444" if e["impact"] >= 3 else "#f59e0b" if e["impact"] == 2 else "#94a3b8"
        impact_bars = "█" * e["impact"] + "░" * (3 - e["impact"])
        forecast_val = str(e["forecast"]) if e["forecast"] is not None else "—"
        previous_val = str(e["previous"]) if e["previous"] is not None else "—"
        today_badge = '<span style="background:#ef4444;color:#fff;padding:1px 6px;border-radius:3px;font-size:0.75rem;font-weight:700">TODAY</span>' if e["days_away"] == 0 else f'<span style="color:#94a3b8">{e["days_away"]}d</span>'
        row_bg = "background:#1e1b2a" if e["days_away"] == 0 else ""
        rows_html += f"""
    <tr style="{row_bg}">
      <td>{e["date"]}</td>
      <td style="color:#94a3b8">{e["time"]}</td>
      <td style="font-weight:600;color:{impact_color}">{icon} {e["currency"]}</td>
      <td>{e["title"]}</td>
      <td style="color:{impact_color};font-family:monospace">{impact_bars}</td>
      <td style="color:#94a3b8">{forecast_val}</td>
      <td style="color:#94a3b8">{previous_val}</td>
      <td>{today_badge}</td>
    </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Economic Calendar</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;margin:0;padding:16px}}
  h1{{color:#58a6ff;font-size:1.4rem;margin-bottom:4px}}
  .meta{{color:#8b949e;font-size:0.82rem;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:0.88rem}}
  th{{background:#161b22;color:#8b949e;text-align:left;padding:8px 10px;border-bottom:2px solid #30363d;position:sticky;top:0}}
  td{{padding:7px 10px;border-bottom:1px solid #21262d}}
  tr:hover td{{background:#161b22}}
  .badge-high{{color:#ef4444;font-weight:700}}
  .count{{color:#8b949e;font-size:0.82rem;margin-top:12px}}
</style>
</head>
<body>
<h1>📅 Economic Calendar</h1>
<div class="meta">Next {days} days | Impact ≥ {min_impact} | Generated {now}</div>
<table>
  <thead>
    <tr>
      <th>Date</th><th>Time</th><th>Currency</th><th>Event</th>
      <th>Impact</th><th>Forecast</th><th>Previous</th><th>When</th>
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>
<p class="count">{len(events)} event(s) shown</p>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TradingView Economic Calendar — upcoming macro events"
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Number of days ahead (default: 7)")
    parser.add_argument("--currencies", default="",
                        help="Comma-separated currency filter (e.g. USD,EUR,JPY)")
    parser.add_argument("--min-impact", type=int, default=2,
                        help="Minimum impact level 0-3 (default: 2)")
    parser.add_argument("--out", default="",
                        help="Write full HTML report to this path")
    main(parser.parse_args())
