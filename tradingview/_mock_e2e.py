"""
Mock E2E test: Send real DB alerts to Telegram without running signal_stack.py.
Usage: python tradingview/_mock_e2e.py
"""
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from telegram_scheduler import AlertFormatter, _SIGNAL_SCORE_THRESHOLD, _ALERTS_DB
from telegram_alerts import send_table_v2, is_enabled

if not is_enabled():
    print("[mock-e2e] Telegram not configured — check BOT_TOKEN/CHAT_ID env vars")
    sys.exit(1)

since_iso = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
conn = sqlite3.connect(str(_ALERTS_DB))
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT tv_symbol, MAX(composite_score) AS composite_score,
           signal, fire_time, l1_score, l2_score, l3_score, l4_score,
           win_rate_pct, market_regime
    FROM alerts
    WHERE composite_score >= ? AND fire_time >= ?
      AND (is_delisted IS NULL OR is_delisted = 0)
    GROUP BY tv_symbol
    ORDER BY MAX(composite_score) DESC LIMIT 10
    """,
    (_SIGNAL_SCORE_THRESHOLD, since_iso),
).fetchall()
conn.close()

alerts = [
    {
        "symbol": r["tv_symbol"],
        "score": r["composite_score"],
        "signal": r["signal"] or "BUY",
        "l1": r["l1_score"],
        "l2": r["l2_score"],
        "l3": r["l3_score"],
        "l4": r["l4_score"],
        "win_rate": r["win_rate_pct"],
        "regime": r["market_regime"],
        "fire_time": r["fire_time"],
    }
    for r in rows
]
print(f"[mock-e2e] Loaded {len(alerts)} alerts from DB (30h window)")
for a in alerts:
    print(f"  {a['symbol']:30s} score={a['score']:.1f}")

if not alerts:
    print("[mock-e2e] No alerts found — DB may be empty or all below threshold")
    sys.exit(1)

fmt = AlertFormatter()
msg = fmt.format_stocks_table(alerts, skipped=0)
chunks = fmt.chunk(msg)
print(f"\n[mock-e2e] Message: {len(msg)} chars, {len(chunks)} chunk(s)")
print("\n--- Preview (first chunk) ---")
print(chunks[0])
print("--- End preview ---\n")

print("[mock-e2e] Sending to Telegram...")
ok = send_table_v2(msg)
result = "OK" if ok else "FAILED"
print(f"[mock-e2e] Send result: {result}")
sys.exit(0 if ok else 1)
