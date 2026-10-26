"""Frozen-vs-edge paired reference lifecycle, not a live fill simulator.

Research transitions: DECIDED, CASH, REFERENCE_ENTRY, REFERENCE_CLOSED,
UNRESOLVED, EXPIRED. No actual brokerage P/L is inferred from bid/ask crosses.
An entry quote is not an execution; broker-filled evidence is separate.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .architecture_forecast_validation import aware, number

NY = ZoneInfo("America/New_York")
HOLD_DAYS = 21
EXIT_DTE = 7
PENDING_MINUTES = 30
LEG_CLOCK_SKEW_SECONDS = 120


def start(*, cohort_id, decision_at, mode, cash, baseline, challenger,
          frozen_policy_version="us-vertical-paper-v1"):
    if mode not in ("ranking_only", "full_policy"):
        raise ValueError("UNKNOWN_STUDY_MODE")
    at = aware(decision_at)
    if number(cash, positive=True) <= 0 or not cohort_id:
        raise ValueError("INVALID_CAPITAL_OR_COHORT")
    def entry(candidate):
        if candidate is None:
            return {"state": "CASH", "candidate": None, "entry": None, "exit": None}
        if not candidate.get("legs") or len(candidate["legs"]) != 2:
            raise ValueError("EXACT_TWO_LEGS_REQUIRED")
        if any(x.get("side") not in (-1, 1) for x in candidate["legs"]):
            raise ValueError("UNSUPPORTED_SPREAD_LEG")
        if len({x["key"] for x in candidate["legs"]}) != 2:
            raise ValueError("DUPLICATE_OPTION_LEG")
        return {"state": "DECIDED", "candidate": candidate, "entry": None, "exit": None}
    return {"cohort_id": cohort_id, "decision_at": at.isoformat(),
            "frozen_policy_version": frozen_policy_version, "mode": mode,
            "cash_budget": cash, "baseline": entry(baseline), "challenger": entry(challenger),
            "actual_broker_fills": 0, "broker_realized_pnl": "NOT_MEASURED"}


def reference_cross(state, arm, *, event_at, quotes, phase, fee_per_leg=.65):
    """Use same-clock exact 2-leg reference bid/ask crosses, no fills.

    quotes: {contract_key: {'bid','ask','updated_at','basis':'reference'}}.
    Prices from different timestamps beyond leg skew are rejected.
    """
    if arm not in ("baseline", "challenger") or phase not in ("entry", "exit"):
        raise ValueError("INVALID_ARM_OR_PHASE")
    decision = aware(state["decision_at"])
    now = aware(event_at)
    if now <= decision:
        raise ValueError("NO_FUTURE_EVIDENCE")
    old = state[arm]
    if old["candidate"] is None:
        raise ValueError("CASH_DOES_NOT_OPEN_CONTRACT")
    candidate = old["candidate"]
    expiry = datetime.fromisoformat(candidate["expiry"]).date()
    dte = (expiry - now.astimezone(NY).date()).days
    if dte <= 0 or now > decision + timedelta(days=HOLD_DAYS) or (phase == "exit" and dte < EXIT_DTE):
        old["state"] = "UNRESOLVED_EXIT_WINDOW" if phase == "exit" else "EXPIRED"
        return {"status": "OUTSIDE_POLICY_TIME_WINDOW", "value": None}
    if phase == "entry" and now > decision + timedelta(minutes=PENDING_MINUTES):
        old["state"] = "UNRESOLVED_ENTRY_DEADLINE"
        return {"status": "ENTRY_WINDOW_ELAPSED", "value": None}
    if phase == "exit" and old["entry"] is not None and now <= aware(old["entry"]["at"]):
        return {"status": "EXIT_PRE_DATES_ENTRY", "value": None}
    if phase == "exit" and old["entry"] is None:
        old["state"] = "UNRESOLVED_NO_ENTRY_REFERENCE"
        return {"status": "NO_ENTRY_REFERENCE", "value": None}
    if phase == "entry" and old["entry"] is not None:
        raise ValueError("ENTRY_ALREADY_CAPTURED")
    if phase == "exit" and old["exit"] is not None:
        raise ValueError("EXIT_ALREADY_CAPTURED")
    updated, flow = [], 0.
    for leg in candidate["legs"]:
        q = quotes.get(leg["key"])
        if q is None or q.get("basis") != "reference":
            old["state"] = "UNRESOLVED_MISSING_EXACT_REFERENCE"
            return {"status": "MISSING_REFERENCE", "value": None}
        bid, ask = number(q["bid"]), number(q["ask"], positive=True)
        if bid < 0 or bid > ask:
            raise ValueError("INVALID_REFERENCE_SPREAD")
        t = aware(q["updated_at"])
        if t > now or (now - t).total_seconds() > 900:
            old["state"] = "UNRESOLVED_STALE_QUOTE"
            return {"status": "STALE_REFERENCE", "value": None}
        updated.append(t)
        side = leg["side"] if phase == "entry" else -leg["side"]
        flow += 100 * (bid if side == -1 else -ask)
    if (max(updated) - min(updated)).total_seconds() > LEG_CLOCK_SKEW_SECONDS:
        old["state"] = "UNRESOLVED_LEG_CLOCK_SKEW"
        return {"status": "LEG_SKEW", "value": None}
    record = {"at": now.isoformat(), "theoretical_cross_cash": round(flow, 4),
              "theoretical_fee": round(len(candidate["legs"]) * fee_per_leg, 4),
              "source_class": "REFERENCE_QUOTE_NOT_FILL", "actual_broker_execution": False}
    old[phase] = record
    old["state"] = "REFERENCE_ENTRY" if phase == "entry" else "REFERENCE_CLOSED"
    return {"status": old["state"], "value": record["theoretical_cross_cash"]}


def summarize(state):
    """Never mislabel crossed reference prices as actual realized P/L."""
    result = {"cohort_id": state["cohort_id"], "mode": state["mode"],
              "actual_broker_pnl": "NOT_MEASURED", "broker_fills": 0,
              "cash_alternative_usd": 0., "both_arms_comparable": False,
              "reference_only": True, "arms": {}}
    for name in ("baseline", "challenger"):
        arm = state[name]
        if arm["candidate"] is None:
            result["arms"][name] = {"status": "CASH", "hypothetical_net_usd": 0.}
        elif arm["entry"] is None or arm["exit"] is None:
            result["arms"][name] = {"status": "UNRESOLVED", "hypothetical_net_usd": None}
        else:
            e, x = arm["entry"], arm["exit"]
            value = e["theoretical_cross_cash"] + x["theoretical_cross_cash"] - e["theoretical_fee"] - x["theoretical_fee"]
            result["arms"][name] = {"status": "HYPOTHETICAL_REFERENCE_ONLY", "hypothetical_net_usd": round(value, 4)}
    a, b = result["arms"]["baseline"], result["arms"]["challenger"]
    if state["mode"] == "ranking_only":
        # Ranking-only means SAME participation/capital/quantity, opening and
        # closing observations. This does NOT establish equivalent market beta.
        ea, eb = state["baseline"], state["challenger"]
        comparable = (a["status"] == b["status"] == "HYPOTHETICAL_REFERENCE_ONLY"
                      and ea["entry"]["at"] == eb["entry"]["at"]
                      and ea["exit"]["at"] == eb["exit"]["at"]
                      and ea["candidate"].get("qty", 1) == eb["candidate"].get("qty", 1))
    else:
        comparable = (a["hypothetical_net_usd"] is not None and b["hypothetical_net_usd"] is not None)
    result["both_arms_comparable"] = comparable
    if comparable:
        result["hypothetical_difference_usd"] = round(b["hypothetical_net_usd"] - a["hypothetical_net_usd"], 4)
    else:
        result["hypothetical_difference_usd"] = None
    return result
