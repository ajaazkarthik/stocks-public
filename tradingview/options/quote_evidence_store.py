"""Isolated OPTION QUOTE evidence store: no broker/Telegram/scheduler mutation.

Manual research-only ingestion only. Keep raw vendor payload and credentials
outside source control. Missing per-side timestamps are forever recorded as
blocked evidence rather than inferred from snapshots or receipts.
"""
from __future__ import annotations
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from tradingview.options.quote_evidence_research import audit,_at

DEFAULT=Path.home()/".config/stocks-options-edge/quote-provenance.sqlite"
DDL="""CREATE TABLE IF NOT EXISTS quote_evidence(
    evidence_sha256 TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
    source TEXT NOT NULL, root TEXT NOT NULL, expiry TEXT NOT NULL,
    option_type TEXT NOT NULL, strike TEXT NOT NULL,
    top_status TEXT NOT NULL, evidence_status TEXT NOT NULL,
    reason TEXT NOT NULL, bid_at TEXT, ask_at TEXT,
    side_timestamp_verified INTEGER NOT NULL CHECK(side_timestamp_verified IN(0,1)),
    report_json TEXT NOT NULL
);
"""
def _private(path):
    p=Path(path).expanduser().resolve()
    if "stocks/database/" in str(p) or p.name in ("options_paper.db","alert_tracker.db"):
        raise ValueError("cannot use existing production DB")
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not p.exists():
        fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    if p.stat().st_mode&0o077:
        raise PermissionError("quote-provenance DB must be 0600")
    return p

def summarize(quote,decision_at):
    report=audit(quote,decision_at=decision_at)
    # Only authorized status metadata, never raw quote book/data or tokens.
    return {
        "decision_at":_at(decision_at).isoformat(),
        "source":str(quote.get("source","UNKNOWN"))[:120],
        "root":str(quote.get("root","UNKNOWN"))[:20],
        "expiry":str(quote.get("expiry","UNKNOWN"))[:14],
        "option_type":str(quote.get("option_type","UNKNOWN"))[:8],
        "strike":str(quote.get("strike","UNKNOWN"))[:20],
        "top_status":str(quote.get("top_status","UNKNOWN"))[:24],
        "bid_at":quote.get("bid_at") if report["side_times_verified"] else None,
        "ask_at":quote.get("ask_at") if report["side_times_verified"] else None,
        "side_timestamp_verified":bool(report["side_times_verified"]),
        "evidence_status":report["status"],
        "reason":report["reason"],
        "executable":False,
        "profitability":"NOT_MEASURED",
    }

def archive(path,quote,*,decision_at):
    """Explicitly called from an isolated research process only."""
    d=summarize(quote,decision_at)
    body=json.dumps(d,sort_keys=True,allow_nan=False)
    sha=hashlib.sha256(body.encode()).hexdigest()
    p=_private(path)
    with sqlite3.connect(p,timeout=10) as c:
        c.executescript(DDL)
        with c:
            c.execute("INSERT OR IGNORE INTO quote_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sha,d["decision_at"],d["source"],d["root"],d["expiry"],
                  d["option_type"],d["strike"],d["top_status"],d["evidence_status"],
                  str(d["reason"]),d["bid_at"],d["ask_at"],
                  int(d["side_timestamp_verified"]),body))
        total=c.execute("SELECT count(*) FROM quote_evidence").fetchone()[0]
    return {"evidence_sha256":sha,"status":d["evidence_status"],
            "reason":d["reason"],"local_evidence_records":total,
            "no_raw_vendor_prices_persisted":True,"live_orders":0}
