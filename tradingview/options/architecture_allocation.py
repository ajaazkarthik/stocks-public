"""Joint whole-contract portfolio research allocation, not a broker order.

Profit maximization over cash vs positions under contractual loss, collateral,
position-count and SHARED market stress budgets. Opportunity values are
MODELLED, not observed. Reject unknown stress inputs rather than zero-filling.
"""
from __future__ import annotations
import math


def finite(value, *, minimum=None):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_BUDGET")
    v = float(value)
    if not math.isfinite(v) or (minimum is not None and v < minimum):
        raise ValueError("INVALID_BUDGET_OR_RISK")
    return v


def allocate(opportunities, *, available_cash, risk_budget=1000., per_position_risk=250.,
             existing_reserved_risk=0., shared_stress_budget=None, max_positions=5,
             max_search_states=200_000):
    if shared_stress_budget is None:
        return {"status": "UNASSESSED_SHARED_STRESS", "positions": [], "broker_orders": 0,
                "modelled_net_usd": 0., "realized_net": "NOT_MEASURED"}
    cash, total, cap, reserved, shock = (
        finite(v, minimum=0) for v in
        (available_cash, risk_budget, per_position_risk, existing_reserved_risk, shared_stress_budget)
    )
    if type(max_positions) is not int or max_positions < 0:
        raise ValueError("INVALID_POSITION_LIMIT")
    candidates = sorted(opportunities, key=lambda x: x["id"])
    if len({c["id"] for c in candidates}) != len(candidates):
        raise ValueError("DUPLICATE_CANDIDATE")
    stress_ids = None
    for c in candidates:
        for field in ("conservative_net_usd", "risk_usd", "collateral_usd"):
            finite(c[field])
        if c["risk_usd"] <= 0 or c["collateral_usd"] < 0 or type(c["max_qty"]) is not int or c["max_qty"] < 0:
            raise ValueError("INVALID_CANDIDATE")
        scenario_map = c.get("stress_losses")
        if not isinstance(scenario_map, dict) or not scenario_map:
            raise ValueError("SHARED_STRESS_MISSING")
        ids = tuple(sorted(scenario_map))
        if stress_ids is not None and ids != stress_ids:
            raise ValueError("STRESS_SCENARIO_CLOCK_MISMATCH")
        stress_ids = ids
        if any(finite(val, minimum=0) < 0 for val in scenario_map.values()):
            raise ValueError("INVALID_STRESS")
    if reserved > total:
        raise ValueError("EXISTING_POSITION_BREACH")
    if len(candidates) > 12:
        # All opportunities considered. A transparent heuristic is preferable
        # to silently discarding every result as the original 12-cap did.
        picks=[];used_risk=used_collateral=0.;used_stress={k:0. for k in stress_ids};net=0.
        ordered=sorted(candidates,key=lambda c:(-c["conservative_net_usd"],c["id"]))
        for c in ordered:
            for q in range(1,min(c["max_qty"],int(cap//c["risk_usd"]))+1):
                if c["conservative_net_usd"]<=0:break
                if len({i for i,n in picks})>=max_positions and c["id"] not in {i for i,n in picks}:break
                if used_risk+c["risk_usd"]>total-reserved+1e-9:break
                if used_collateral+c["collateral_usd"]>cash+1e-9:break
                if any(used_stress[k]+c["stress_losses"][k]>shock+1e-9 for k in stress_ids):break
                picks.append((c["id"],1));used_risk+=c["risk_usd"]
                used_collateral+=c["collateral_usd"];net+=c["conservative_net_usd"]
                for k in stress_ids:used_stress[k]+=c["stress_losses"][k]
        qty={}
        for key,_ in picks:qty[key]=qty.get(key,0)+1
        return {"status":"MODEL_ONLY_HEURISTIC_NOT_PROVEN_OPTIMAL",
                "positions":[dict(id=k,qty=n) for k,n in sorted(qty.items())],
                "modelled_net_usd":round(net,6),"reserved_risk_usd":round(used_risk,6),
                "collateral_used_usd":round(used_collateral,6),
                "worst_joint_stress_usd":round(max(used_stress.values(),default=0.),6),
                "candidate_count":len(candidates),"all_candidates_considered":True,
                "cash_abstention_considered":True,"broker_orders":0,
                "realized_net":"NOT_MEASURED"}
    state_count = 0
    # When net ties, deterministic lexicographic order may choose cash. Never
    # choose a negative conservative-net position merely to use all capital.
    best = (0., (), 0., 0., 0.)
    def dfs(i, picks, positions, risk, collateral, shocks, net):
        nonlocal state_count, best
        state_count += 1
        if state_count > max_search_states:
            raise ValueError("ALLOCATION_EXACT_SEARCH_LIMIT")
        if i == len(candidates):
            chosen = tuple((c["id"], q) for c, q in zip(candidates, picks) if q)
            if net > best[0] + 1e-10 or (abs(net - best[0]) <= 1e-10 and chosen < best[1]):
                best = (net, chosen, risk, collateral, max(shocks, default=0.))
            return
        c = candidates[i]
        maximum = min(c["max_qty"], int(cap // c["risk_usd"]))
        for q in range(maximum + 1):
            new_risk = risk + q * c["risk_usd"]
            new_collateral = collateral + q * c["collateral_usd"]
            if new_risk > total - reserved + 1e-9 or new_collateral > cash + 1e-9:
                break
            if positions + (q > 0) > max_positions:
                continue
            new_shocks = tuple(s + q * c["stress_losses"][k] for s, k in zip(shocks, stress_ids))
            if any(v > shock + 1e-9 for v in new_shocks):
                continue
            dfs(i + 1, picks + (q,), positions + int(q > 0), new_risk, new_collateral,
                new_shocks, net + q * c["conservative_net_usd"])
    dfs(0, (), 0, 0., 0., (0.,) * len(stress_ids or ()), 0.)
    return {"status": "MODEL_ONLY_OPTIMUM", "positions": [dict(id=i, qty=q) for i, q in best[1]],
            "modelled_net_usd": round(best[0], 6), "reserved_risk_usd": round(best[2], 6),
            "collateral_used_usd": round(best[3], 6), "worst_joint_stress_usd": round(best[4], 6),
            "available_cash_usd": cash, "existing_reserved_risk_usd": reserved,
            "states_explored": state_count, "cash_abstention_considered": True,
            "broker_orders": 0, "realized_net": "NOT_MEASURED"}
