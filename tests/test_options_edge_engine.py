"""Forecast/valuation challenger: synthetic fixtures ONLY, no measured alpha."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timedelta,timezone
import math

import pytest

from tradingview.options import edge_engine as e
from tradingview.options import paper_scanner as frozen
from tests.test_option_paper_scanner import NOW,observation


def fake_model(obs,now=NOW,*,up_probability=.80,error=5.0,down_spot=95.0):
    candidate,_=frozen.choose_candidate(obs,now)
    keys=[leg["key"] for leg in candidate["legs"]]
    paths=[]
    for name,p,spot in [("up",up_probability,110.0),("down",1-up_probability,down_spot)]:
        paths.append({"scenario_id":name,"probability":p,
                      "path":[{"at":(now+timedelta(days=21)).isoformat(),
                               "spot":spot,"trend":candidate["direction"],
                               "funding_cost_usd":0.0,"assignment_cost_usd":0.0,
                               "ivs":{k:.20 for k in keys}}]})
    return dict(engine=e.EDGE_VERSION,forecast_id="SYNTHETIC-TEST-ONLY",
                method="synthetic two-path joint spot/IV fixture, not trained",
                training_source_sha256="0"*64,
                created_at=(now-timedelta(minutes=2)).isoformat(),
                training_cutoff=(now-timedelta(days=1)).isoformat(),
                uncertainty_method="synthetic constant error, not a CI",
                forecast_status="UNVALIDATED_SYNTHETIC",
                underlying=obs["symbol"],mean_estimation_error_usd=error,
                scenarios=paths)


@pytest.mark.parametrize("direction,kind",[
    ("BULL","DEBIT"),("BEAR","DEBIT"),("BULL","CREDIT"),("BEAR","CREDIT")
])
def test_same_frozen_eligibility_all_four_directions(direction,kind):
    obs=observation(direction,kind)
    original=deepcopy(obs)
    candidate,status=frozen.choose_candidate(obs,NOW)
    mirror,reasons=e.enumerate_frozen_candidates(obs,NOW)
    assert status=="CANDIDATE"
    assert len(mirror)==1
    assert mirror[0]["legs"]==candidate["legs"]
    assert mirror[0]["strategy"]==candidate["strategy"]
    assert mirror[0]["indicative_cash"]==candidate["indicative_cash"]
    assert original==obs


def test_flat_trend_has_no_eligible_spreads():
    obs=observation()
    obs["features"]["ema20"]=obs["features"]["close"]
    candidates,rejected=e.enumerate_frozen_candidates(obs,NOW)
    assert candidates==[] and rejected["NO_TREND"]==1
    result=e.compare_frozen_to_edge(obs,NOW)
    assert result["challenger"]=="NO_FROZEN_ELIGIBLE_CANDIDATES"


def test_invalid_quotes_never_impute_missing_prices():
    obs=observation()
    obs["quotes"][1].pop("ask")
    candidates,reason=e.enumerate_frozen_candidates(obs,NOW)
    assert candidates==[]
    assert reason["INVALID_EXACT_CONTRACT_OR_QUOTE"]>=1


def test_user_fixed_exit_arithmetic_example_and_put_call_parity():
    assert e.fixed_cashflow_limit(220,8,22,15,kind="DEBIT")==175
    assert e.fixed_cashflow_limit(100,8,22,15,kind="CREDIT")==145
    for spot in (90,100,102,105,120):
        bull_call=(max(spot-100,0)-max(spot-105,0)-2)*100
        bull_put=(3-max(105-spot,0)+max(100-spot,0))*100
        assert bull_call==bull_put


def test_bs_call_put_parity_and_expiry_bounds():
    call=e.bs_option_value(100,100,1,.20,"CALL",rate=0.0,dividend=0.0)
    put=e.bs_option_value(100,100,1,.20,"PUT",rate=0.0,dividend=0.0)
    assert call==pytest.approx(put)
    assert call>0
    assert e.bs_option_value(120,100,0,.20,"CALL")==20
    assert e.bs_option_value(120,100,0,.20,"PUT")==0


def test_no_forecast_does_not_create_prices_or_alerts():
    result=e.compare_frozen_to_edge(observation(),NOW)
    assert result["baseline"]["status"]=="CANDIDATE"
    assert result["considered"]==1
    assert result["challenger"]=="MISSING_POINT_IN_TIME_JOINT_FORECAST"
    assert result["selection"] is None and result["price_limit"] is None
    assert result["paper_orders"]==0
    assert e.research_alert_preview(result,now=NOW)=={
        "status":"SILENT","reason":"MISSING_POINT_IN_TIME_JOINT_FORECAST","alerts_sent":0}


@pytest.mark.parametrize("field,broken",[
    ("engine","wrong"),("training_source_sha256","ABC"),
    ("created_at","2026-10-27T15:00:00+00:00"),
    ("training_cutoff","2026-10-27T15:00:00+00:00"),
    ("underlying","NASDAQ:QQQ"),("mean_estimation_error_usd",-1.0),
    ("forecast_status","made_up")
])
def test_vintages_future_labels_invalid_provenance_fail_closed(field,broken):
    obs=observation()
    f=fake_model(obs);f[field]=broken
    with pytest.raises(ValueError):
        e.validate_forecast(f,obs,NOW)
    check=e.compare_frozen_to_edge(obs,NOW,f)
    assert check["challenger"]=="FORECAST_REJECTED"
    assert check["selection"] is None
    assert check["notifier_eligible"] is False


def test_no_training_proof_or_confidence_interval_claim():
    obs=observation();f=fake_model(obs)
    f["forecast_status"]="HELDOUT_VERIFIED"
    with pytest.raises(ValueError,match="HOLDOUT"):
        e.validate_forecast(f,obs,NOW)
    f["validation_horizon"]="FROZEN_EXIT_POLICY"
    f["calibration_report_id"]="fake-reference-does-not-prove-reliability"
    e.validate_forecast(f,obs,NOW)
    # Claimed metadata does NOT automatically become a live alert.
    x=e.compare_frozen_to_edge(obs,NOW,f)
    assert x["notifier_eligible"] is False


def test_joint_scenario_probabilities_reject_broken_distribution():
    obs=observation();f=fake_model(obs)
    f["scenarios"][0]["probability"]=.90
    with pytest.raises(ValueError,match="PROBABILITIES"):
        e.validate_forecast(f,obs,NOW)
    f["scenarios"][0]["probability"]=.8
    f["scenarios"][1]["scenario_id"]=f["scenarios"][0]["scenario_id"]
    with pytest.raises(ValueError,match="DUPLICATE"):
        e.validate_forecast(f,obs,NOW)


def test_reprice_both_legs_with_joint_spot_iv_paths_and_frozen_exits():
    obs=observation();c,_=frozen.choose_candidate(obs,NOW)
    f=fake_model(obs)
    val=e.value_candidate(c,obs,f,NOW)
    assert 0<val["expected_net_usd"]<val["contractual_max_gain_usd"]
    assert val["conservative_mean_proxy_usd"]<=val["stressed_expected_net_usd"]
    assert val["max_contractual_loss_usd"]==pytest.approx(c["risk_per_spread"])
    assert val["projected_max_liquidation_loss_usd"]>=0
    assert val["path_cadence"]=="COARSE_RESEARCH_ONLY"
    assert {x["reason"] for x in val["scenario_exits"]}=={"PROFIT_TARGET","LOSS_LIMIT"}
    assert val["live_executable"] is False
    assert val["observed_option_pnl"]=="NOT_MEASURED"
    assert val["holdout_error_allowance_usd"]==5.0


def test_entry_price_is_part_of_exit_path_and_limit_solves_again():
    obs=observation();c,_=frozen.choose_candidate(obs,NOW);f=fake_model(obs)
    lim=e.price_limit(c,obs,f,NOW)
    assert lim["status"]=="MODEL_ONLY_PRICE_CEILING"
    assert lim["limit_quote"] is not None
    allowed=e.value_candidate(c,obs,f,NOW,entry_cash=-lim["limit_usd"])
    assert allowed["edge_above_hurdle"] is True
    # Above the price ceiling, inflated debit erodes edge or violates
    # the frozen 60%-of-width economic risk limit.
    with pytest.raises(ValueError):
        e.value_candidate(c,obs,f,NOW,entry_cash=-250)
    selected=e.compare_frozen_to_edge(obs,NOW,f)
    assert selected["baseline"]["candidate"]["legs"]==c["legs"]
    assert selected["selection"] is not None
    assert selected["price_limit"]==lim
    assert selected["notifier_eligible"] is False
    assert selected["paper_orders"]==0
    preview=e.research_alert_preview(selected,now=NOW)
    assert preview["status"]=="EXPERIMENTAL_MODEL_PREVIEW_ONLY"
    assert preview["never_send"] is True and preview["alerts_sent"]==0
    assert preview["max_contractual_loss_usd"]<250


def test_forecast_can_be_correct_direction_and_not_good_price():
    obs=observation();f=fake_model(obs,error=80.0)
    check=e.compare_frozen_to_edge(obs,NOW,f)
    assert check["considered"]==1
    assert check["challenger"]=="NO_CONSERVATIVE_PRICE_EDGE"
    assert check["selection"] is None and check["notifier_eligible"] is False


def test_missing_exact_leg_surface_causes_no_value_or_price_limit():
    obs=observation();f=fake_model(obs)
    f["scenarios"][0]["path"][0]["ivs"].pop(next(iter(f["scenarios"][0]["path"][0]["ivs"])))
    c,_=frozen.choose_candidate(obs,NOW)
    with pytest.raises(ValueError):
        e.value_candidate(c,obs,f,NOW)
    check=e.compare_frozen_to_edge(obs,NOW,f)
    assert check["selection"] is None
    assert check["ranked"][0]["reason"]=="VALUATION_BLOCKED"


@pytest.mark.parametrize("fault",["missing_step","bad_vol","invalid_trend","same_timestamp"])
def test_unreliable_path_is_not_a_fill(fault):
    obs=observation();f=fake_model(obs)
    point=f["scenarios"][0]["path"][0]
    if fault=="missing_step":
        f["scenarios"][0]["path"]=[]
    elif fault=="bad_vol":
        k=next(iter(point["ivs"]));point["ivs"][k]=-10
    elif fault=="invalid_trend":
        point["trend"]="UNKNOWN"
    else:
        point["at"]=NOW.isoformat()
    c,_=frozen.choose_candidate(obs,NOW)
    with pytest.raises((ValueError,KeyError)):
        e.value_candidate(c,obs,f,NOW)


def test_long_and_short_volatility_not_naively_derived_from_debit_credit():
    cand,_=frozen.choose_candidate(observation("BULL","DEBIT"),NOW)
    other,_=frozen.choose_candidate(observation("BULL","CREDIT"),NOW)
    assert e.payoff_key(cand) != e.payoff_key(other)
    # Payoff parity is for IDENTICAL strikes, not the differing selected strikes.
    assert cand["direction"]==other["direction"]=="BULL"


@pytest.mark.parametrize("direction,kind",[
    ("BULL","DEBIT"),("BEAR","DEBIT"),("BULL","CREDIT"),("BEAR","CREDIT")
])
def test_all_four_structures_project_with_same_exit_engine(direction,kind):
    obs=observation(direction,kind)
    candidate,_=frozen.choose_candidate(obs,NOW)
    forecast=fake_model(obs)
    val=e.value_candidate(candidate,obs,forecast,NOW)
    assert math.isfinite(val["expected_net_usd"])
    assert math.isfinite(val["stressed_expected_net_usd"])
    assert math.isfinite(val["stressed_worst_net_usd"])
    assert len(val["scenario_exits"])==2
    assert val["max_contractual_loss_usd"]<=250
    assert val["observed_option_pnl"]=="NOT_MEASURED"


def test_forecast_funding_and_assignment_cost_reduce_model_profit():
    obs=observation()
    c,_=frozen.choose_candidate(obs,NOW)
    base=fake_model(obs)
    val=e.value_candidate(c,obs,base,NOW)
    expensive=deepcopy(base)
    for s in expensive["scenarios"]:
        for point in s["path"]:
            point["funding_cost_usd"]=2.5
            point["assignment_cost_usd"]=3.0
    changed=e.value_candidate(c,obs,expensive,NOW)
    assert changed["expected_net_usd"]<val["expected_net_usd"]
    assert changed["stressed_expected_net_usd"]<val["stressed_expected_net_usd"]


def test_missing_explicit_funding_assignment_cost_is_blocked():
    obs=observation()
    c,_=frozen.choose_candidate(obs,NOW)
    forecast=fake_model(obs)
    forecast["scenarios"][0]["path"][0].pop("assignment_cost_usd")
    with pytest.raises((ValueError,KeyError,TypeError)):
        e.value_candidate(c,obs,forecast,NOW)


def test_projected_liquidation_loss_distinct_from_contractual_expiry_loss():
    obs=observation("BEAR","DEBIT")
    c,_=frozen.choose_candidate(obs,NOW)
    forecast=fake_model(obs)
    v=e.value_candidate(c,obs,forecast,NOW)
    assert v["max_contractual_loss_usd"]>0
    assert v["projected_max_liquidation_loss_usd"]>=0
    assert v["live_executable"] is False


def test_no_arbitrage_invalid_future_per_leg_iv_smile_rejected():
    obs=observation()
    c,_=frozen.choose_candidate(obs,NOW)
    f=fake_model(obs)
    # Impossible fair spread: strike 100 call priced less than strike 102 call.
    keys=[x["key"] for x in c["legs"]]
    f["scenarios"][0]["path"][0]["ivs"][keys[0]]=.01
    f["scenarios"][0]["path"][0]["ivs"][keys[1]]=3.0
    with pytest.raises(ValueError,match="ARBITRAGE"):
        e.value_candidate(c,obs,f,NOW)


def test_price_limit_rejects_uncertainty_that_consumes_all_edge():
    obs=observation();c,_=frozen.choose_candidate(obs,NOW)
    f=fake_model(obs,error=500)
    assert e.price_limit(c,obs,f,NOW)["status"]=="NO_PRICE_WITH_CONSERVATIVE_EDGE"
    assert e.compare_frozen_to_edge(obs,NOW,f)["selection"] is None


def test_model_preview_never_formats_as_approved_telegram_trade():
    obs=observation()
    output=e.compare_frozen_to_edge(obs,NOW,fake_model(obs))
    rendered=e.research_alert_preview(output,now=NOW)
    assert rendered["never_send"] is True
    assert rendered["alert_is_not_fill"] is True
    assert rendered["live_quote_side_verified"] is False
    assert rendered["engine"]==e.EDGE_VERSION
    assert rendered["price_valid_through"]>rendered["decision_at"]


def test_selection_is_parameter_free_unless_forecast_is_supplied():
    obs=observation()
    x=e.compare_frozen_to_edge(obs,NOW,None)
    y=e.compare_frozen_to_edge(obs,NOW,dict(engine="not a model"))
    assert x["baseline"]==y["baseline"]
    assert x["considered"]==y["considered"]
    assert x["notifier_eligible"]==y["notifier_eligible"] is False
    assert y["challenger"]=="FORECAST_REJECTED"
