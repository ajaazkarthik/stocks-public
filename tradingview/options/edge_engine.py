"""OFFLINE price-dependent options challenger; never a live scanner or bot.

Risk/quote gates and candidate universe are copied from frozen v1 *as a
research mirror*; frozen selection, exits and paper state remain unchanged.
Real-world scenario probabilities, error bars and side-quote clock evidence
MUST come from independently proven point-in-time sources. No such validated
model exists in this repo. Nothing in this module sends, fills or orders.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import NormalDist

from tradingview.options.paper_scanner import (
    NY, POLICY, VERSION, cash_flow, choose_candidate, contract_key,
    economics, encode, number, signal, stamp, validate_observation,
    validate_quote,
)

EDGE_VERSION = "us-options-edge-selection-research-v0"
NORM = NormalDist()
QUOTE_TICK = 0.01
# Fixed research policy, NOT optimized from past option outcomes.
RESEARCH_POLICY = dict(model_hurdle_usd=15.0, max_spread_loss_usd=250.0,
                       risk_budget_usd=1000.0, stressed_exit_spread=2.0,
                       max_path_step_minutes=15, required_future_sessions=20,
                       required_closed_per_arm=50)


def enumerate_frozen_candidates(obs, now, policy=POLICY):
    """Replicate frozen eligible *set*; surface reasons for each rejection.

    The actual baseline candidate is ALWAYS taken from original choose_candidate.
    This independent mirror must be regression tested against frozen decisions;
    it is not called by the live scanner and cannot expand allowed contracts.
    """
    validate_observation(obs,now)
    direction,rv=signal(obs,now,policy)
    rejections={}
    if direction=="FLAT":
        return [],{"NO_TREND":1}
    usable=[]
    for q in obs["quotes"]:
        try:
            validate_quote(q,now,entry=True,policy=policy)
            dte=(date.fromisoformat(q["expiry"])-now.astimezone(NY).date()).days
            if not policy["min_dte"]<=dte<=policy["max_dte"]:
                raise ValueError("DTE")
            number(q["iv"],positive=True)
            delta=number(q["delta"])
            if not ((q["option_type"]=="CALL" and 0<delta<1) or
                    (q["option_type"]=="PUT" and -1<delta<0)):
                raise ValueError("DIRECTIONAL_DELTA")
            usable.append(q)
        except (KeyError,TypeError,ValueError) as exc:
            reason=(str(exc) if str(exc) in ("DTE","DIRECTIONAL_DELTA") else
                    "INVALID_EXACT_CONTRACT_OR_QUOTE")
            rejections[reason]=rejections.get(reason,0)+1
    candidates=[]
    for anchor in usable:
        delta=abs(anchor["delta"])
        ratio=anchor["iv"]/rv
        for kind in ("DEBIT","CREDIT"):
            opt=(("CALL" if direction=="BULL" else "PUT") if kind=="DEBIT" else
                 ("PUT" if direction=="BULL" else "CALL"))
            if anchor["option_type"]!=opt:
                continue
            if kind=="DEBIT" and not (.45<=delta<=.65 and ratio<=policy["buy_iv_rv"]):
                continue
            if kind=="CREDIT" and not (.20<=delta<=.35 and ratio>=policy["sell_iv_rv"]):
                continue
            for wing in usable:
                if wing["expiry"]!=anchor["expiry"] or wing["option_type"]!=opt or wing["basis"]!=anchor["basis"]:
                    continue
                diff=(wing["strike"]-anchor["strike"])*(1 if opt=="CALL" else -1)
                if not 0<diff<=policy["max_width"]:
                    continue
                try:
                    clocks=[validate_quote(q,now,entry=True,policy=policy) for q in (anchor,wing)]
                    if (max(t[1] for t in clocks)-min(t[0] for t in clocks)).total_seconds()>policy["leg_skew_seconds"]:
                        raise ValueError("LEG_QUOTE_CLOCK_SKEW")
                    c=dict(strategy=f"{direction}_{opt}_{kind}",kind=kind,
                        direction=direction,symbol=obs["symbol"],
                        expiry=anchor["expiry"],width=diff,basis=anchor["basis"],
                        iv_rv=ratio,
                        legs=[dict(key=contract_key(anchor),side=1 if kind=="DEBIT" else -1),
                              dict(key=contract_key(wing),side=-1 if kind=="DEBIT" else 1)])
                    cash,risk,reward=economics(c,[anchor,wing],policy)
                    if risk>policy["trade_risk"]:
                        raise ValueError("PER_TRADE_RISK")
                    c.update(indicative_cash=cash,risk_per_spread=risk,
                             max_profit_per_spread=reward)
                    candidates.append(c)
                except (ValueError,TypeError,KeyError) as exc:
                    reason=str(exc) if str(exc)=="PER_TRADE_RISK" else "LEG_OR_SPREAD_REJECTED"
                    rejections[reason]=rejections.get(reason,0)+1
    # Intentionally no alternative thresholds or in-sample rank tuning.
    return candidates,rejections


def payoff_key(candidate):
    """Terminal bull call / bull put verticals at the same strikes are one exposure."""
    strikes=sorted(float(x["key"].split("|")[-2]) for x in candidate["legs"])
    return (candidate["symbol"],candidate["expiry"],candidate["direction"],
            tuple(round(k,8) for k in strikes))


def bs_option_value(spot,strike,years,vol,option_type,*,rate=0.0,dividend=0.0):
    """Theoretical per-share mark for *projected* future IV. NEVER observed quote."""
    for x in (spot,strike,vol):
        number(x,positive=True)
    if option_type not in ("CALL","PUT"):
        raise ValueError("option_type")
    for x in (years,rate,dividend):
        number(x)
    if years<0 or vol>5 or abs(rate)>1 or abs(dividend)>1:
        raise ValueError("invalid valuation inputs")
    if years==0:
        return max(spot-strike,0) if option_type=="CALL" else max(strike-spot,0)
    root=vol*math.sqrt(years)
    d1=(math.log(spot/strike)+(rate-dividend+.5*vol*vol)*years)/root
    d2=d1-root
    forward_spot=spot*math.exp(-dividend*years)
    strike_pv=strike*math.exp(-rate*years)
    if option_type=="CALL":
        return forward_spot*NORM.cdf(d1)-strike_pv*NORM.cdf(d2)
    return strike_pv*NORM.cdf(-d2)-forward_spot*NORM.cdf(-d1)


def fixed_cashflow_limit(exit_value_usd, remaining_cost_usd,
                         mean_uncertainty_usd, required_profit_usd, *, kind):
    """Illustrative fixed exit cashflow equation; NOT path-dependent price limit."""
    for x in (exit_value_usd,remaining_cost_usd,mean_uncertainty_usd,required_profit_usd):
        if number(x)<0:
            raise ValueError("negative allowance")
    if kind=="DEBIT":
        return exit_value_usd-remaining_cost_usd-mean_uncertainty_usd-required_profit_usd
    if kind=="CREDIT":
        return exit_value_usd+remaining_cost_usd+mean_uncertainty_usd+required_profit_usd
    raise ValueError("kind")


def _candidate_quotes(candidate,obs):
    keyed={contract_key(q):q for q in obs["quotes"]}
    return [keyed[leg["key"]] for leg in candidate["legs"]]


def _model_exit_cash(candidate,quotes,point,at,stress,policy):
    """Projected close proceeds minus spread/slippage for TWO legs, USD/spread."""
    if point.get("trend") not in ("BULL","BEAR","FLAT"):
        raise ValueError("scenario trend missing")
    vols=point.get("ivs")
    if not isinstance(vols,dict):
        raise ValueError("exact per-leg volatility surface missing")
    spot=number(point.get("spot"),positive=True)
    expiry=date.fromisoformat(candidate["expiry"])
    # Year fraction is always based on actual projected timestamp, not RTH count.
    years=max(0,(datetime.combine(expiry,datetime.min.time(),tzinfo=NY)
                 +timedelta(hours=16)-at.astimezone(NY)).total_seconds())/(365.25*86400)
    credit=0.0
    theoretical_credit=0.0
    for leg,q in zip(candidate["legs"],quotes):
        k=leg["key"]
        if k not in vols:
            raise ValueError("MISSING_EXACT_LEG_IV")
        sigma=number(vols[k],positive=True)
        if sigma>5:
            raise ValueError("unrealistic implied vol")
        theoretical=bs_option_value(spot,float(q["strike"]),years,sigma,q["option_type"],
                                    rate=number(point.get("rate",0)),
                                    dividend=number(point.get("dividend",0)))
        width=(number(q["ask"])-number(q["bid"]))/2*stress
        slip=policy["slippage_per_share"]*stress
        # Closing sells at downside of theoretical mid; closing buys at upside.
        side=-leg["side"]
        fill=max(0,theoretical-width-slip) if side==-1 else theoretical+width+slip
        credit-=side*fill*100
        theoretical_credit+=leg["side"]*theoretical*100
    width=candidate["width"]*100
    low,high=(0,width) if candidate["kind"]=="DEBIT" else (-width,0)
    if not low-1e-6<=theoretical_credit<=high+1e-6:
        raise ValueError("ARBITRAGE_INCONSISTENT_PROJECTED_IV_SURFACE")
    return credit


def _exit_path(candidate,quotes,path,at_decision,entry_cash,stress,policy):
    """Apply original v1 target/stop, short-leg ITM, trend and horizon exits.

    Forecast future theoretical marks are not real option bid/ask exits.
    """
    fees=4*policy["fee_per_leg"]
    entry_time=stamp(at_decision)
    expiry=date.fromisoformat(candidate["expiry"])
    exit_boundary=min(entry_time+timedelta(days=policy["max_hold_days"]),
                      datetime.combine(expiry-timedelta(days=policy["exit_dte"]),
                                       datetime.min.time(),tzinfo=NY).astimezone(timezone.utc))
    if exit_boundary<=entry_time:
        raise ValueError("no projected holding horizon")
    if not isinstance(path,list) or not path:
        raise ValueError("joint path missing")
    times=[stamp(p["at"]) for p in path]
    if any(t<=entry_time or t>=datetime.combine(expiry,datetime.min.time(),tzinfo=NY)+timedelta(days=1)
           for t in times):
        raise ValueError("path extends to decision/expired contract")
    if any(b<=a for a,b in zip(times,times[1:])):
        raise ValueError("unsorted future path")
    if times[-1]<exit_boundary:
        raise ValueError("incomplete projected holding horizon")
    coarse=False
    prev=entry_time
    pnl=None
    reason=None
    for t,p in zip(times,path):
        # Overnight gaps are permitted, but hourly/daily intraday jumps are
        # coarse, NOT eligible for faithful v1 target/stop simulation.
        gap=t-prev
        if gap>timedelta(hours=18) or (
            t.astimezone(NY).date()==prev.astimezone(NY).date() and
            gap.total_seconds()>RESEARCH_POLICY["max_path_step_minutes"]*60):
            coarse=True
        close_cash=_model_exit_cash(candidate,quotes,p,t,stress,policy)
        # Explicit projected funding and assignment-risk cash costs; never
        # silently default missing assumptions to zero.
        funding=number(p["funding_cost_usd"])
        assignment=number(p["assignment_cost_usd"])
        if min(funding,assignment)<0:
            raise ValueError("NEGATIVE_SCENARIO_COST")
        net=entry_cash+close_cash-fees-funding-assignment
        base=abs(entry_cash)
        if net<=-base*policy["debit_stop_fraction" if candidate["kind"]=="DEBIT" else "credit_stop_fraction"]:
            reason="LOSS_LIMIT"
        elif net>=base*policy["debit_target_fraction" if candidate["kind"]=="DEBIT" else "credit_target_fraction"]:
            reason="PROFIT_TARGET"
        elif (expiry-t.astimezone(NY).date()).days<=policy["exit_dte"]:
            reason="PRE_EXPIRY_EXIT"
        elif t-entry_time>=timedelta(days=policy["max_hold_days"]):
            reason="TIME_EXIT"
        # Frozen exit behavior, even where assignment proxy may trigger on gain.
        for leg,q in zip(candidate["legs"],quotes):
            if leg["side"]==-1 and (
                (q["option_type"]=="CALL" and p["spot"]>=q["strike"]) or
                (q["option_type"]=="PUT" and p["spot"]<=q["strike"])):
                reason=reason or "SHORT_LEG_ITM"
        if p["trend"]!=candidate["direction"]:
            reason=reason or "TREND_EXIT"
        pnl=net
        if reason or t>=exit_boundary:
            reason=reason or "HORIZON_MARK"
            break
        prev=t
    return pnl,reason,coarse

def validate_forecast(forecast,obs,now):
    """Reject future information; unproven forecasts can be evaluated but never alerted."""
    if not isinstance(forecast,dict) or forecast.get("engine")!=EDGE_VERSION:
        raise ValueError("FORECAST_MISSING_OR_WRONG_VERSION")
    for field in ("forecast_id","method","training_source_sha256","created_at",
                  "training_cutoff","uncertainty_method","forecast_status"):
        if not isinstance(forecast.get(field),str) or not forecast[field]:
            raise ValueError("FORECAST_PROVENANCE_INCOMPLETE")
    digest=forecast["training_source_sha256"]
    if len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("INVALID_FORECAST_SOURCE_DIGEST")
    created=stamp(forecast["created_at"])
    trained=stamp(forecast["training_cutoff"])
    observed=stamp(obs["observed_at"])
    if forecast["forecast_status"]=="REFERENCE_PROXY_UNVERIFIED":
        # Real forecast construction naturally happens AFTER quote receipt.
        # Old fixtures use their original fixed pre-observation vintages.
        if not trained<observed<=created<=stamp(now):
            raise ValueError("FORECAST_FUTURE_INFORMATION")
    elif not trained<created<=observed<=stamp(now):
        raise ValueError("FORECAST_FUTURE_INFORMATION")
    if forecast.get("underlying")!=obs["symbol"]:
        raise ValueError("FORECAST_UNDERLYING_MISMATCH")
    if forecast["forecast_status"] not in ("UNVALIDATED_SYNTHETIC","REFERENCE_PROXY_UNVERIFIED","HELDOUT_VERIFIED"):
        raise ValueError("FORECAST_STATUS_UNKNOWN")
    error=number(forecast.get("mean_estimation_error_usd"))
    if error<0:
        raise ValueError("NEGATIVE_MEAN_ESTIMATION_ERROR")
    if forecast["forecast_status"]=="HELDOUT_VERIFIED":
        if forecast.get("validation_horizon")!="FROZEN_EXIT_POLICY" or not isinstance(
            forecast.get("calibration_report_id"),str) or not forecast["calibration_report_id"]:
            raise ValueError("FORECAST_HOLDOUT_EVIDENCE_MISSING")
    scenarios=forecast.get("scenarios")
    if not isinstance(scenarios,list) or len(scenarios)<2 or len(scenarios)>1000:
        raise ValueError("AT_LEAST_TWO_JOINT_SCENARIOS_REQUIRED")
    probs=[number(s.get("probability"),positive=True) for s in scenarios]
    if not math.isclose(sum(probs),1,rel_tol=0,abs_tol=1e-9):
        raise ValueError("SCENARIO_PROBABILITIES_NOT_ONE")
    ids=[s.get("scenario_id") for s in scenarios]
    if not all(isinstance(v,str) and v for v in ids) or len(set(ids))!=len(ids):
        raise ValueError("DUPLICATE_OR_MISSING_SCENARIO_IDS")
    return error


def value_candidate(candidate,obs,forecast,now,*,entry_cash=None,
                    policy=POLICY,research_policy=RESEARCH_POLICY):
    """Entire spread distribution under fixed v1 exit and independent scenario input."""
    validate_forecast(forecast,obs,now)
    quotes=_candidate_quotes(candidate,obs)
    for q in quotes:
        validate_quote(q,now,entry=True,policy=policy)
    if entry_cash is None:
        entry_cash,risk,reward=economics(candidate,quotes,policy)
    else:
        entry_cash=number(entry_cash)
        width=candidate["width"]*100
        fees=4*policy["fee_per_leg"]
        if candidate["kind"]=="DEBIT":
            risk=-entry_cash+fees
            reward=width+entry_cash-fees
            if not 0<-entry_cash<=width*policy["max_debit_fraction"]:
                raise ValueError("ENTRY_DEBIT_OUTSIDE_FROZEN_ENVELOPE")
        elif candidate["kind"]=="CREDIT":
            risk=width-entry_cash+fees
            reward=entry_cash-fees
            if not width*policy["min_credit_fraction"]<=entry_cash<width:
                raise ValueError("ENTRY_CREDIT_OUTSIDE_FROZEN_ENVELOPE")
        else:
            raise ValueError("UNKNOWN_PAYOFF_KIND")
        if min(reward,risk)<=0:
            raise ValueError("NONPOSITIVE_PAYOFF")
    if risk>policy["trade_risk"]:
        raise ValueError("CONTRACTUAL_RISK_BUDGET_EXCEEDED")
    nominal=[]
    stressed=[]
    scenario_exits=[]
    coarse=False
    for scenario in forecast["scenarios"]:
        b,reason,coarse_b=_exit_path(candidate,quotes,scenario.get("path"),
                                     now,entry_cash,1.0,policy)
        x,reason_stress,coarse_s=_exit_path(candidate,quotes,scenario.get("path"),
                                     now,entry_cash,research_policy["stressed_exit_spread"],policy)
        # Early liquidation across stressed bid/ask can exceed the terminal
        # contractual bound. Record it as additional liquidation risk, rather
        # than incorrectly dismissing a valid projected loss as impossible.
        nominal.append(b)
        stressed.append(x)
        scenario_exits.append(dict(scenario=scenario["scenario_id"],reason=reason,
                                   stressed_exit_reason=reason_stress))
        coarse|=coarse_b or coarse_s
    probabilities=[s["probability"] for s in forecast["scenarios"]]
    exp_base=sum(w*x for w,x in zip(probabilities,nominal))
    exp_stress=sum(w*x for w,x in zip(probabilities,stressed))
    conservative=min(exp_base,exp_stress)-forecast["mean_estimation_error_usd"]
    tail=sorted(zip(stressed,probabilities))
    remain=0.10
    loss_mass=0.0
    mass=0.0
    for val,p in tail:
        take=min(remain,p)
        loss_mass+=val*take
        mass+=take
        remain-=take
        if remain<=1e-12:
            break
    return dict(expected_net_usd=round(exp_base,4),
                stressed_expected_net_usd=round(exp_stress,4),
                conservative_mean_proxy_usd=round(conservative,4),
                holdout_error_allowance_usd=forecast["mean_estimation_error_usd"],
                max_contractual_loss_usd=round(risk,4),
                projected_max_liquidation_loss_usd=round(max(0,-min(stressed)),4),
                projection_within_frozen_risk_budget=max(risk,-min(stressed))<=policy["trade_risk"],
                contractual_max_gain_usd=round(reward,4),
                stressed_worst_net_usd=round(min(stressed),4),
                stressed_lower_10pct_tail_usd=round(loss_mass/mass,4),
                scenario_exits=scenario_exits,
                path_cadence="COARSE_RESEARCH_ONLY" if coarse else "CADENCE_NOT_INDEPENDENTLY_CERTIFIED",
                assumptions="BS PROJECTED future option marks, not observed executable exit quotes",
                edge_above_hurdle=(conservative>=research_policy["model_hurdle_usd"] and
                                   max(risk,-min(stressed))<=policy["trade_risk"]),
                live_executable=False,observed_option_pnl="NOT_MEASURED")


def price_limit(candidate,obs,forecast,now,*,policy=POLICY,research_policy=RESEARCH_POLICY):
    """Cent-grid solver reevaluates path exits at each price, not fixed arithmetic.

    Nonmonotone feasible regions are flagged; quote price is per 100-share lot.
    """
    width_cents=int(round(candidate["width"]*100))
    passes=[]
    for cents in range(1,width_cents):
        entry=-float(cents) if candidate["kind"]=="DEBIT" else float(cents)
        try:
            valuation=value_candidate(candidate,obs,forecast,now,entry_cash=entry,
                                policy=policy,research_policy=research_policy)
            good=valuation["edge_above_hurdle"]
        except ValueError:
            good=False
        passes.append((cents,good))
    if not any(x for _,x in passes):
        return dict(status="NO_PRICE_WITH_CONSERVATIVE_EDGE",limit_quote=None,
                    limit_usd=None,complete_grid=True)
    usable=[c for c,yes in passes if yes]
    if candidate["kind"]=="DEBIT":
        threshold=max(usable)
        nonmonotone=any(not yes and min(usable)<c<threshold for c,yes in passes)
        name="MODEL_ONLY_PRICE_CEILING"
    else:
        threshold=min(usable)
        nonmonotone=any(not yes and threshold<c<max(usable) for c,yes in passes)
        name="MODEL_ONLY_MINIMUM_CREDIT"
    return dict(status="NONMONOTONE_LIMIT_REJECTED" if nonmonotone else name,
                limit_quote=None if nonmonotone else round(threshold/100,2),
                limit_usd=None if nonmonotone else threshold,complete_grid=True)


def compare_frozen_to_edge(obs,now,forecast=None,*,max_candidates=100,policy=POLICY):
    """One-clock matched eligible universe: change ranking only, not baseline exits."""
    try:
        frozen,reason=choose_candidate(obs,now,policy)
    except (ValueError,KeyError,TypeError):
        frozen,reason=None,"FROZEN_DATA_UNAVAILABLE"
    result=dict(engine=EDGE_VERSION,baseline=dict(status=reason,candidate=frozen),
                challenger="NO_VALIDATED_FORECAST",
                quote_basis="reference" if any(q.get("basis")=="reference" for q in obs.get("quotes",[])) else "unconfirmed",
                rejections={},considered=0,ranked=[],selection=None,
                price_limit=None,notifier_eligible=False,
                cash_alternative_usd=0.0, portfolio_factor_risk="NOT_EVALUATED_NO_JOINT_MARKET_COHORT",
                performance="NOT_MEASURED",paper_orders=0)
    try:
        candidates,rejected=enumerate_frozen_candidates(obs,now,policy)
    except (ValueError,KeyError,TypeError):
        result["challenger"]="MISSING_COMPLETED_SAME_CLOCK_OBSERVATION"
        return result
    result["rejections"]=rejected
    result["considered"]=len(candidates)
    if len(candidates)>max_candidates:
        result["challenger"]="CANDIDATE_SET_EXCEEDS_RESEARCH_LIMIT"
        return result
    if not candidates:
        result["challenger"]="NO_FROZEN_ELIGIBLE_CANDIDATES"
        return result
    if forecast is None:
        result["challenger"]="MISSING_POINT_IN_TIME_JOINT_FORECAST"
        return result
    try:
        validate_forecast(forecast,obs,now)
    except (ValueError,TypeError,KeyError) as exc:
        result["challenger"]="FORECAST_REJECTED"
        result["forecast_error_type"]=type(exc).__name__
        return result
    rows=[]
    for candidate in candidates:
        try:
            metrics=value_candidate(candidate,obs,forecast,now)
            limit=price_limit(candidate,obs,forecast,now)
            quote_cash=number(candidate["indicative_cash"])
            if candidate["kind"]=="DEBIT":
                inside=(quote_cash<0 and limit["status"]=="MODEL_ONLY_PRICE_CEILING" and
                        -quote_cash<=limit["limit_usd"])
            else:
                inside=(quote_cash>0 and limit["status"]=="MODEL_ONLY_MINIMUM_CREDIT" and
                        quote_cash>=limit["limit_usd"])
            rows.append(dict(candidate=candidate,valuation=metrics,price_limit=limit,
                             quoted_entry_cash_usd=round(quote_cash,4),
                             quote_below_price_limit=bool(inside),
                             conservative_edge_per_dollar_risk=round(
                                 metrics["conservative_mean_proxy_usd"]/candidate["risk_per_spread"],6),
                             reason="PROXY_EDGE_CLEARS_HURDLE" if inside and
                                    metrics["edge_above_hurdle"] else "NO_CONSERVATIVE_PRICE_EDGE"))
        except (ValueError,KeyError,TypeError) as exc:
            rows.append(dict(candidate=candidate,reason="VALUATION_BLOCKED",
                             error_class=type(exc).__name__))
    rows.sort(key=lambda v:(
        -v.get("conservative_edge_per_dollar_risk",float("-inf")),
        encode(v["candidate"]["legs"])))
    result["ranked"]=rows
    qualified=[x for x in rows if x["reason"]=="PROXY_EDGE_CLEARS_HURDLE"]
    result["selection"]=qualified[0] if qualified else None
    result["challenger"]=("MODEL_HYPOTHESIS_ONLY_NO_ACTIONABLE_ALERT"
                        if result["selection"] else "NO_CONSERVATIVE_PRICE_EDGE")
    result["price_limit"]=result["selection"]["price_limit"] if result["selection"] else None
    # No hardcoding a forecast from IV/RV; all observed quotes remain reference.
    # A hypothetical passed model-only limit is not a verified trade or alert.
    return result


def research_alert_preview(result,*,now):
    """Optional descriptive preview; NEVER returned as sendable trade alert."""
    choice=result.get("selection")
    if choice is None:
        return dict(status="SILENT",reason=result.get("challenger"),
                    alerts_sent=0)
    candidate=choice["candidate"]
    limit=choice["price_limit"]
    valuation=choice["valuation"]
    return dict(status="EXPERIMENTAL_MODEL_PREVIEW_ONLY",
                engine=EDGE_VERSION,never_send=True,
                exact_legs=candidate["legs"],underlying=candidate["symbol"],
                expiry=candidate["expiry"],hypothesis="FROZEN_EXPOSURE_VALUATION",
                limit_type=limit["status"],limit_price=limit["limit_quote"],
                decision_at=stamp(now).isoformat(),
                price_valid_through=(stamp(now)+timedelta(minutes=15)).isoformat(),
                model_net_expected_usd=valuation["expected_net_usd"],
                conservative_mean_proxy_usd=valuation["conservative_mean_proxy_usd"],
                max_contractual_loss_usd=valuation["max_contractual_loss_usd"],
                projected_max_liquidation_loss_usd=valuation["projected_max_liquidation_loss_usd"],
                stressed_tail_10pct_usd=valuation["stressed_lower_10pct_tail_usd"],
                model_valuation_uncertainty_usd=valuation["holdout_error_allowance_usd"],
                exit_policy="UNCHANGED us-vertical-paper-v1",
                debit_target_fraction=POLICY["debit_target_fraction"],
                debit_stop_fraction=POLICY["debit_stop_fraction"],
                credit_target_fraction=POLICY["credit_target_fraction"],
                credit_stop_fraction=POLICY["credit_stop_fraction"],
                exit_dte=POLICY["exit_dte"],max_hold_days=POLICY["max_hold_days"],
                observed_heldout_trades=0,live_quote_side_verified=False,
                alert_is_not_fill=True,alerts_sent=0)
