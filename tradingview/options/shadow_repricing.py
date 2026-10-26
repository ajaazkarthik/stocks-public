"""Forward paired REFERENCE-only price repricing for baseline vs edge shadow.

This is NOT trading P&L. A later pair of actual broker executions/ACKs would
be needed to establish real returns. A reference bid/ask cross is a theoretical
counterfactual, not a fill. Never write to the source paper/broker database.
"""
from __future__ import annotations
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from tradingview.options.paper_scanner import (
    POLICY,stamp,contract_key,cash_flow,validate_quote,validate_observation,
)
from tradingview.options.existing_data_shadow import DEFAULT_SHADOW

SCHEMA="""CREATE TABLE IF NOT EXISTS shadow_reference_reprices (
  id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
  followup_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  baseline_status TEXT NOT NULL, edge_status TEXT NOT NULL,
  baseline_reference_net_usd REAL,
  edge_reference_net_usd REAL,
  observed_fill_status TEXT NOT NULL CHECK(observed_fill_status='NO_BROKER_FILL_EVIDENCE'),
  evidence_class TEXT NOT NULL CHECK(evidence_class='HYPOTHETICAL_REFERENCE_QUOTE_CROSS'),
  UNIQUE(decision_id,followup_at)
);"""

def one_arm(candidate, obs, now):
    if candidate is None:
        return "CASH_ABSTAIN",0.0
    try:
        by_key={contract_key(q):q for q in obs["quotes"]}
        exact=[by_key[leg["key"]] for leg in candidate["legs"]]
        if any(q.get("basis")!="reference" for q in exact):
            return "OTHER_BASIS_NOT_COMPARABLE",None
        for q in exact:
            validate_quote(q,now,entry=False)
        # Assumes crossing the TWO original quoted sides in and out; this
        # is a stress proxy, NOT a price available in one book at one time.
        exit_cash=cash_flow(candidate["legs"],exact,opening=False)
        fees=4*POLICY["fee_per_leg"]
        assumed=round(candidate["indicative_cash"]+exit_cash-fees,4)
        return "UNVERIFIED_REFERENCE_MARK",assumed
    except (KeyError,ValueError,TypeError):
        return "EXACT_FUTURE_QUOTE_UNAVAILABLE",None

def reconcile(shadow_db, obs, now, *,max_decisions=100):
    """Read prior REFERENCE shadow decisions, then optionally save paired marks.

    No fabricated 21d held fills; no claims that the prior price limit executed.
    """
    p=Path(shadow_db).expanduser().resolve()
    if not p.exists():
        return {"status":"NO_PREVIOUS_SHADOW_DECISIONS","compared":0,"stored":0}
    if p.stat().st_mode&0o077:
        raise PermissionError("PRIVATE_RESEARCH_STATE_MUST_BE_0600")
    moment=stamp(now)
    validate_observation(obs,moment)
    at=stamp(obs["observed_at"])
    with sqlite3.connect(p) as c:
        c.executescript(SCHEMA)
        rows=c.execute("SELECT decision_id,payload_json FROM shadow_decisions "
                       "WHERE symbol=? AND observed_at<? ORDER BY observed_at DESC LIMIT ?",
                       (obs["symbol"],at.isoformat(),max_decisions)).fetchall()
        results=[]
        for rid,payload in rows:
            earlier=json.loads(payload)
            old=stamp(earlier["observed_at"])
            if at<=old:
                continue
            baseline_status,baseline_pnl=one_arm(earlier.get("baseline_reference_candidate"),obs,moment)
            edge_status,edge_pnl=one_arm(earlier.get("edge_reference_candidate"),obs,moment)
            mid=hashlib.sha256((rid+"|"+obs["observed_at"]).encode()).hexdigest()
            results.append((mid,rid,at.isoformat(),obs["symbol"],
                            baseline_status,edge_status,baseline_pnl,edge_pnl,
                            "NO_BROKER_FILL_EVIDENCE","HYPOTHETICAL_REFERENCE_QUOTE_CROSS"))
        with c:
            c.executemany("INSERT OR IGNORE INTO shadow_reference_reprices "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)",results)
        n=c.execute("SELECT COUNT(*) FROM shadow_reference_reprices").fetchone()[0]
    return {"status":"REFERENCE_ONLY_NOT_TRADING_PERFORMANCE",
            "compared":len(results),"stored":n,
            "with_both_reference_marks":sum(r[6] is not None and r[7] is not None for r in results),
            "cash_alternative_usd":0.0,
            "actual_broker_fills":0,"net_realized_return":"NOT_MEASURED"}
