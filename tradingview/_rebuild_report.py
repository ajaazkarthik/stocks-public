"""Regenerate actionable_report.html with deduplication from existing log."""
import json, sys
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

log = json.loads((BASE / "actionable_log.json").read_text(encoding="utf-8"))
ranked = sorted(log, key=lambda x: x["composite"], reverse=True)

# Deduplicate by tv_symbol — keep best composite per symbol, skip UNKNOWN
seen = {}
for entry in ranked:
    sym = entry.get("tv_symbol", "")
    if not sym or sym.upper() == "UNKNOWN":
        continue
    if sym not in seen or entry["composite"] > seen[sym]["composite"]:
        seen[sym] = entry
deduped = sorted(seen.values(), key=lambda x: x["composite"], reverse=True)

import signal_stack as ss

regime = ss.fetch_market_regime()
base = ss.build_html(deduped, len(deduped), regime=regime)

now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
banner = (
    '<div style="background:#7c3aed;color:#fff;padding:12px 28px;'
    'font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px">'
    '<span style="font-size:18px">\U0001f6a8</span>'
    '<span>ACTIONABLE SIGNALS \u2014 Composite \u2265 40 | '
    f"{len(deduped)} signal(s) | Last updated: {now_str}</span>"
    "</div>"
)
html = base.replace("<body>", "<body>" + banner, 1)
(BASE / "actionable_report.html").write_text(html, encoding="utf-8")

print(f"Done. {len(log)} log entries -> {len(deduped)} unique symbols in report")
for e in deduped:
    print(f"  {e['tv_symbol']:25s}  composite={e['composite']}")
