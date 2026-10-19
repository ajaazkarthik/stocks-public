"""Smoke test — no network calls needed."""
import pandas as pd
import numpy as np

from leverage.regime_score import (
    RegimeEngine, score_volatility, score_trend,
    score_momentum, leverage_from_score, regime_label,
    compute_regime_score_from_components,
)
from leverage.signals import MultiAssetScanner, annual_decay_drag, score_asset_volatility
from leverage.sell_strategy import LeverageSellEngine, LeveragePosition
from leverage.portfolio import PortfolioAllocator, PortfolioContext


def run():
    errors = []

    # --- regime_score ---
    assert score_volatility(11) == 9.5,  "vix=11 should give 9.5"
    assert score_volatility(14) == 9.0,  "vix=14 should give 9.0"
    assert score_volatility(18) == 8.0,  "vix=18 should give 8.0"
    assert score_volatility(22) == 6.5,  "vix=22 should give 6.5"
    assert score_volatility(25) == 4.5,  "vix=25 should give 4.5"
    assert score_volatility(30) == 2.5,  "vix=30 should give 2.5"
    assert score_volatility(35) == 1.0,  "vix=35 should give 1.0"
    assert score_volatility(45) == 0.0,  "vix=45 should give 0.0"
    print("[1] score_volatility: OK")

    np.random.seed(42)
    prices = pd.Series(100.0 + np.cumsum(np.random.randn(250) * 0.5))
    st, _ = score_trend(prices)
    assert 0 <= st <= 10, f"score_trend out of range: {st}"
    sm, md = score_momentum(prices)
    assert 0 <= sm <= 10, f"score_momentum out of range: {sm}"
    assert "rsi" in md
    print(f"[2] score_trend={st:.2f}  score_momentum={sm:.2f}  rsi={md['rsi']:.1f}")

    assert leverage_from_score(9.0) == 3.0
    assert leverage_from_score(7.5) == 2.0
    assert leverage_from_score(6.5) == 1.5
    assert leverage_from_score(5.0) == 1.0
    assert leverage_from_score(3.0) == 0.5
    print("[3] leverage_from_score: OK")

    score = compute_regime_score_from_components(8, 7, 6, 5)
    assert 0 <= score <= 10, f"composite score OOB: {score}"
    lbl = regime_label(score)
    assert isinstance(lbl, str)
    print(f"[4] compute_regime_score_from_components={score:.2f}  label='{lbl}'")

    # Backtest-mode without yfinance
    engine = RegimeEngine()
    result = engine.compute_from_series(prices, vix=18.0)
    assert hasattr(result, "score")
    assert hasattr(result, "leverage_multiplier")
    assert hasattr(result, "is_leverage_safe")
    print(f"[5] RegimeEngine.compute_from_series: score={result.score:.2f}  lev={result.leverage_multiplier}x  safe={result.is_leverage_safe}  label={result.regime_label}")

    # --- signals (no network) ---
    drag = annual_decay_drag(3, 0.20)
    # L*(L-1)/2 * σ²  = 3*2/2 * 0.04 = 0.12
    assert abs(drag - 0.12) < 1e-9, f"decay drag wrong: {drag}"
    print(f"[6] annual_decay_drag(3x, 20%): {drag:.2%}  OK")

    vs = score_asset_volatility(15)
    assert 0 <= vs <= 10
    print(f"[7] score_asset_volatility(15%): {vs:.2f}  OK")

    # --- sell_strategy (no network, synthetic position) ---
    sell_engine = LeverageSellEngine()
    pos = LeveragePosition(
        ticker="TQQQ",
        underlying_ticker="QQQ",
        entry_price=50.0,
        current_units=100,
        entry_leverage=3.0,
        entry_date="2024-01-15",
        hwm=52.0,              # HWM
        hold_days=10,
        instrument_type="etf_3x",
        market="US",
    )
    # Inject cached state so no yfinance calls fire
    sell_engine._vix        = 18.0
    sell_engine._regime     = result          # score=6.56 > 4.0 thresholds, safe
    sell_engine._spy_break  = (False, {})     # SPY not below 200-SMA
    sell_engine._vix_spike  = (False, {})     # no VIX spike
    sell_engine._macd_xu    = (False, {})     # no weekly MACD crossunder
    # Monkeypatch _fetch_current_price to return 48.0 (7.7% from HWM=52, below 8% hard exit)
    LeverageSellEngine._fetch_current_price = staticmethod(lambda ticker: 48.0)
    decision = sell_engine.evaluate(pos)
    print(f"[8] sell_engine.evaluate: action={decision.action}  tier={decision.tier}  rule={decision.triggered_rule}")
    assert decision.action in {"HARD_EXIT", "SCALE_DOWN", "TIME_REVIEW", "HOLD"}

    # --- portfolio (no network — sizing_for_single only) ---
    ctx = PortfolioContext(total_portfolio_value=100_000)
    allocator = PortfolioAllocator(ctx)

    sizing = allocator.sizing_for_single(ticker="TQQQ", price=50.0, allocation_pct=0.10, leverage=3.0)
    assert sizing["units"] == 200             # 100_000 * 0.10 / 50 = 200
    assert sizing["notional"] == 10_000.0
    assert sizing["gross_exposure"] == 30_000.0
    assert sizing["max_loss_at_8pct_dd"] == 800.0
    print(f"[9] PortfolioAllocator.sizing_for_single: units={sizing['units']}  notional=${sizing['notional']:,.0f}  decay_drag_yr=${sizing['est_decay_drag_yr']:.0f}  OK")

    print("\n=== ALL SMOKE TESTS PASSED ===")


if __name__ == "__main__":
    run()
