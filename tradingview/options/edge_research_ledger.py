"""Isolated options edge decision register; no options order or Telegram API.

Reads the original options_paper.db in SQLite mode=ro. Archived observations
remain ARCHIVE_DIAGNOSTIC (never prospective trades). An opt-in separate 0600
private research SQLite register stores only decision hashes and summarized
rejection states, not raw vendor quotes or authentication material.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from tradingview.options.paper_scanner import ROOT, encode, stamp
from tradingview.options.edge_engine import EDGE_VERSION,compare_frozen_to_edge

SOURCE = ROOT/"database/options_paper.db"
PRIVATE_LEDGER = Path.home()/".config/stocks-options-edge/decisions.db"
SCHEMA = """CREATE TABLE IF NOT EXISTS edge_decisions (
  observation_sha256 TEXT NOT NULL,
  observation_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  model_version TEXT NOT NULL,
  baseline_status TEXT NOT NULL,
  challenger_status TEXT NOT NULL,
  frozen_candidates INTEGER NOT NULL,
  conservative_model_only INTEGER NOT NULL,
  observation_class TEXT NOT NULL CHECK(observation_class='ARCHIVE_DIAGNOSTIC'),
  performance TEXT NOT NULL CHECK(performance='NOT_MEASURED'),
  side_quote_verification TEXT NOT NULL CHECK(side_quote_verification='UNVERIFIED'),
  decision_json TEXT NOT NULL,
  PRIMARY KEY (observation_sha256, model_version)
);
"""


def source_rows(path,*,limit=1000):
    path=Path(path)
    if not path.is_file():
        raise FileNotFoundError("paper source ledger not found")
    with sqlite3.connect("file:"+str(path.resolve())+"?mode=ro",uri=True) as c:
        run_count=c.execute("SELECT count(*) FROM option_paper_runs").fetchone()[0]
        trade_count=c.execute("SELECT count(*) FROM option_paper_trades").fetchone()[0]
        count=c.execute("SELECT count(*) FROM option_paper_observations").fetchone()[0]
        if count>limit:
            raise ValueError("archive exceeds bounded read limit")
        rows=c.execute("SELECT symbol,observed_at,payload_json FROM "
                       "option_paper_observations ORDER BY observed_at,id").fetchall()
    return (run_count,trade_count,count),rows


def audit(source=SOURCE,*,max_rows=1000):
    """Replay archived original receipts at THEIR OWN decision clocks only.

    This cannot create historical quote sides or historical forecasts. Archived
    outputs are not prospective trial sessions and contain no realized P/L.
    """
    counts,rows=source_rows(source,limit=max_rows)
    analyzed=[]
    errors=Counter()
    for symbol,at,payload in rows:
        digest=hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            obs=json.loads(payload)
            now=stamp(at)
            if obs["symbol"]!=symbol or stamp(obs["observed_at"])!=now:
                raise ValueError("archived observation identity mismatch")
            outcome=compare_frozen_to_edge(obs,now)
            baseline=outcome["baseline"]["status"]
            challenger=outcome["challenger"]
            n=outcome["considered"]
            # Saved decision excludes raw option quotes and features.
            record=dict(observation_sha256=digest,observation_at=now.isoformat(),
                        symbol=symbol,model_version=EDGE_VERSION,
                        baseline_status=baseline,challenger_status=challenger,
                        frozen_candidates=n,conservative_model_only=0,
                        observation_class="ARCHIVE_DIAGNOSTIC",
                        performance="NOT_MEASURED",side_quote_verification="UNVERIFIED",
                        decision_json=json.dumps(dict(baseline=baseline,challenger=challenger,
                            candidates=n,rejections=outcome["rejections"],
                            trade_count=0,source_receipt_hash=digest),sort_keys=True))
            analyzed.append(record)
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            errors[type(exc).__name__]+=1
    return dict(engine=EDGE_VERSION,source_run_count=counts[0],
                source_closed_trade_count=0 if counts[1]==0 else None,
                source_trade_records=counts[1],
                archive_observations=counts[2],diagnostic_decisions=len(analyzed),
                archive_errors=dict(errors),
                challenger_status_counts=dict(Counter(r["challenger_status"] for r in analyzed)),
                forward_qualified_sessions=0,independent_closed_per_arm=0,
                funded_portfolio_pnl="NOT_MEASURED",
                active_telegram_alerts=0,records=analyzed)


def store_private(ledger_path, audit_result,*,source=SOURCE):
    """Explicit write destination must be separate from any existing scanner DB."""
    path=Path(ledger_path).expanduser().resolve()
    if path in {Path(source).resolve(),SOURCE.resolve(),(ROOT/"database/alert_tracker.db").resolve()}:
        raise ValueError("would overwrite an original scanner database")
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.exists() and (path.stat().st_mode&0o077):
        raise PermissionError("edge research ledger must be private (mode 0600)")
    if not path.exists():
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        with c:
            for r in audit_result["records"]:
                keys=list(r)
                c.execute("INSERT OR IGNORE INTO edge_decisions("+",".join(keys)+")"
                          " VALUES("+",".join("?" for _ in keys)+")",
                          [r[k] for k in keys])
        total=c.execute("SELECT count(*) FROM edge_decisions").fetchone()[0]
    return dict(research_rows_total=total,path=str(path),source_original_unchanged=True)


def main(argv=None):
    p=argparse.ArgumentParser(description="Read-only audited v1 vs edge (missing forecasts fail closed)")
    p.add_argument("--source-db",type=Path,default=SOURCE)
    p.add_argument("--max-rows",type=int,default=1000)
    p.add_argument("--store-isolated",action="store_true",
                   help="explicitly store hash-only diagnostics in private separate research DB")
    p.add_argument("--research-db",type=Path,default=PRIVATE_LEDGER)
    args=p.parse_args(argv)
    if not 1<=args.max_rows<=10000:
        p.error("bounded --max-rows must be 1 to 10000")
    result=audit(args.source_db,max_rows=args.max_rows)
    rows=result.pop("records")
    if args.store_isolated:
        result["stored"]=store_private(args.research_db,dict(records=rows),source=args.source_db)
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
