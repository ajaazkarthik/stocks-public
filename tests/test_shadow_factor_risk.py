"""Pair only completed same-date underlying histories; not option P&L."""
import pandas as pd
import pytest
from tests.test_options_quote_forecast_evidence import synthetic_history
from tradingview.options.shadow_factor_risk import underlying_factor_reference

def synthetic_pack():
    h=synthetic_history(630)
    return {k:h.copy() for k in ("SPY","QQQ","IWM")}

def test_same_factor_reveals_not_independent_trades():
    out=underlying_factor_reference(synthetic_pack())
    assert out["status"]=="HISTORICAL_UNDERLYING_FACTOR_PROXY_ONLY"
    assert out["same_market_concentration_warning"] is True
    assert out["average_off_diagonal_correlation"]==1.0
    assert out["paired_completed_days"]==630
    assert out["observed_option_trade_pnl"]=="NOT_MEASURED"
    assert out["future_option_iv_cross_correlation_calibrated"] is False
    assert out["broker_positions_placed"]==0

def test_missing_underlying_history_blocks_instead_of_defaulting_zero():
    histories=synthetic_pack()
    histories.pop("IWM")
    with pytest.raises(ValueError,match="ALL_THREE"):
        underlying_factor_reference(histories)

def test_partial_pairing_retains_only_common_dates():
    histories=synthetic_pack()
    histories["IWM"]=histories["IWM"].iloc[25:].copy()
    out=underlying_factor_reference(histories)
    assert out["paired_completed_days"]==605
