"""Read-only fail-closed option quote evidence. No trading or orders.

A bid/ask snapshot, last trade timestamp, generic update_time, or receipt_at
does not prove independent bid/ask event-time freshness.
"""
from __future__ import annotations
from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo
import math

NY=ZoneInfo("America/New_York")
IDENTITY=("root","expiry","option_type","strike","currency","multiplier","deliverable","trading_class")
STRICT_SEC=30
MAX_SKEW_SEC=3

def _at(value):
    if not isinstance(value,str):raise ValueError("MISSING_TIMESTAMP")
    try:t=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError:raise ValueError("INVALID_TIMESTAMP") from None
    if t.tzinfo is None:raise ValueError("UNZONED_TIMESTAMP")
    return t.astimezone(timezone.utc)

def _positive(value):
    if isinstance(value,bool):raise ValueError("NOT_NUMERIC")
    try:x=float(value)
    except (TypeError,ValueError):raise ValueError("NOT_NUMERIC") from None
    if not math.isfinite(x) or x<=0:raise ValueError("BAD_NONPOSITIVE_PRICE")
    return x

def audit(quote,*,decision_at):
    """No status proves fillability; qualified side times are not fills."""
    result=dict(status="BLOCKED",reason=None,executable=False,
                source=quote.get("source","UNKNOWN"),
                identity_verified=False,side_times_verified=False,
                asof_event_gap_seconds=None,no_orders=True)
    try:
        now=_at(decision_at)
        for key in IDENTITY:
            if key not in quote:raise ValueError("MISSING_CONTRACT_IDENTITY")
        root=quote["root"]
        if (not isinstance(root,str) or not root.isalpha() or
            quote["trading_class"]!=root or quote["currency"]!="USD" or
            quote["multiplier"]!=100 or quote["deliverable"]!="REGULAR" or
            quote["option_type"] not in ("CALL","PUT")):
            raise ValueError("NONSTANDARD_OR_AMBIGUOUS_CONTRACT")
        _positive(quote["strike"])
        expiry=date.fromisoformat(quote["expiry"])
        if expiry<now.astimezone(NY).date():raise ValueError("EXPIRED_CONTRACT")
        result["identity_verified"]=True
        bid=_positive(quote["bid"])
        ask=_positive(quote["ask"])
        if ask<bid:raise ValueError("CROSSED_MARKET")
        if quote.get("top_status")!="REALTIME":
            raise ValueError("NOT_REALTIME_EVIDENCE")
        if (quote.get("timestamp_kind")!="exchange_side_event" or
            quote.get("provenance_independently_verified") is not True):
            raise ValueError("SIDE_EVENT_PROVENANCE_UNVERIFIED")
        b=_at(quote.get("bid_at"))
        a=_at(quote.get("ask_at"))
        for t in (a,b):
            age=(now-t).total_seconds()
            if age<0 or age>STRICT_SEC:raise ValueError("SIDE_EVENT_NOT_FRESH")
        skew=abs((a-b).total_seconds())
        if skew>MAX_SKEW_SEC:raise ValueError("SIDES_NOT_SYNCHRONIZED")
        if quote.get("market_session_verified") is not True:
            raise ValueError("MARKET_SESSION_UNVERIFIED")
        result["side_times_verified"]=True
        result["status"]="TIMESTAMP_QUALIFIED_NOT_A_FILL"
        result["reason"]="PROVENANCE_AND_SIDE_CLOCKS_PASSED"
        result["asof_event_gap_seconds"]=skew
        # Even independently timestamped sides do not prove a multi-leg fill.
        result["executable"]=False
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        result["reason"]=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    return result

def classify_ibkr_snapshot(option_identity,raw,*,at):
    """IBKR returned bid/ask but no per-side event times: strict block."""
    status=(raw.get("top-status") or {}).get("status")
    ba=raw.get("bid-ask") or {}
    q=dict(option_identity,source="IBKR Client Portal snapshot",top_status=status,
           bid=ba.get("bid"),ask=ba.get("ask"),
           timestamp_kind="snapshot_receipt_not_side_event",
           provenance_independently_verified=False,
           market_session_verified=False,bid_at=None,ask_at=None)
    return audit(q,decision_at=at)
