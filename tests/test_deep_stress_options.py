"""
Deep stress tests for OptionsAnalyzer and TradabilityFilter.

Originally a standalone script (python test_deep_stress_options.py).
Converted to pytest-compatible format so `pytest test_deep_stress_options.py`
integrates with the full test suite.
"""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tradingview"))
sys.path.insert(0, str(ROOT / "tradingview" / "options"))

from options.options_analyzer import (
    OptionsAnalyzer, StrategyMode, MODE_CONFIGS,
    _liquidity_score, _iv_score, _strike_score, _dte_score, _composite_option_score,
)


# ── shared helpers ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def analyzer():
    return OptionsAnalyzer()


@pytest.fixture(scope="module")
def cfg_bal():
    return MODE_CONFIGS[StrategyMode.BALANCED]


def make_contract(**kwargs):
    base = {
        'market': 'US', 'volume': 100, 'oi': 500, 'spread_pct': 3.0,
        'dte': 21, 'mid_price': 2.00, 'iv': 0.35, 'moneyness_pct': 0.0,
        'strike': 200.0, 'expiry': '2026-05-15', 'delta': 0.50,
        'spot_price': 200.0, 'source': 'yfinance',
    }
    base.update(kwargs)
    return base


# ── 1. Filter pass/reject ──────────────────────────────────────────────────────

def test_good_contract_passes(analyzer, cfg_bal):
    scored = analyzer._filter_and_score([make_contract()], 70.0, cfg_bal)
    assert len(scored) == 1, f"good contract should pass, got {len(scored)}"


def test_reject_low_volume(analyzer, cfg_bal):
    scored = analyzer._filter_and_score([make_contract(volume=5)], 70.0, cfg_bal)
    assert len(scored) == 0, "volume=5 should be rejected"


def test_reject_low_oi(analyzer, cfg_bal):
    scored = analyzer._filter_and_score([make_contract(oi=50)], 70.0, cfg_bal)
    assert len(scored) == 0, "oi=50 should be rejected"


def test_reject_wide_spread(analyzer, cfg_bal):
    scored = analyzer._filter_and_score([make_contract(spread_pct=12.0)], 70.0, cfg_bal)
    assert len(scored) == 0, "spread=12% should be rejected"


def test_reject_short_dte(analyzer, cfg_bal):
    """DTE too short (< max(3, dte_min-5) = 9 for BALANCED)."""
    scored = analyzer._filter_and_score([make_contract(dte=5)], 70.0, cfg_bal)
    assert len(scored) == 0, "dte=5 should be rejected"


def test_reject_long_dte(analyzer, cfg_bal):
    """DTE too long (> dte_max + 20 = 55 for BALANCED)."""
    scored = analyzer._filter_and_score([make_contract(dte=60)], 70.0, cfg_bal)
    assert len(scored) == 0, "dte=60 should be rejected"


# ── 2. Soft-pass with warnings ─────────────────────────────────────────────────

def test_after_hours_passes_with_warning(analyzer, cfg_bal):
    """volume=0 but OI present → after-hours pass."""
    scored = analyzer._filter_and_score([make_contract(volume=0, oi=500)], 70.0, cfg_bal)
    assert len(scored) == 1, "after-hours contract should pass"
    warns = scored[0].get('_contract_warnings', [])
    assert any('AFTER HOURS' in w for w in warns), f"missing AFTER HOURS warning: {warns}"


def test_india_no_oi_passes_with_warning(analyzer, cfg_bal):
    """NSE contract with oi=0 → passes with info note."""
    c = make_contract(market='NSE', volume=60, oi=0, spread_pct=5.0, dte=21)
    scored = analyzer._filter_and_score([c], 70.0, cfg_bal)
    assert len(scored) == 1, "India no-OI should pass"
    warns = scored[0].get('_contract_warnings', [])
    assert any('OI data not available' in w for w in warns), f"missing OI warning: {warns}"


def test_low_premium_passes_with_warning(analyzer, cfg_bal):
    """mid_price=0.10 → soft warning, not hard reject."""
    scored = analyzer._filter_and_score([make_contract(mid_price=0.10)], 70.0, cfg_bal)
    assert len(scored) == 1, "cheap premium should pass"
    warns = scored[0].get('_contract_warnings', [])
    assert any('LOW PREMIUM' in w for w in warns), f"missing LOW PREMIUM warning: {warns}"


# ── 3. Ranking ─────────────────────────────────────────────────────────────────

def test_ranking_highest_score_first(analyzer, cfg_bal):
    c_high = make_contract(volume=5000, oi=50000, spread_pct=1.0, iv=0.25, dte=21, moneyness_pct=0.0)
    c_low  = make_contract(volume=30,   oi=110,   spread_pct=7.5, iv=0.70, dte=18, moneyness_pct=5.0)
    scored = analyzer._filter_and_score([c_low, c_high], 70.0, cfg_bal)
    assert len(scored) >= 2, f"both should pass, got {len(scored)}"
    assert scored[0]['option_score'] >= scored[1]['option_score'], "highest score should be first"


# ── 4. _build_reason ──────────────────────────────────────────────────────────

def test_build_reason_contains_mode(analyzer, cfg_bal):
    c_ann = {
        **make_contract(),
        'liq_score': 7.5, 'iv_score': 8.0, 'strike_score': 9.0,
        'dte_score': 8.0, 'option_score': 75.0, 'moneyness_pct': 0.5,
    }
    reason = analyzer._build_reason(c_ann, cfg_bal, StrategyMode.BALANCED)
    assert reason, "_build_reason returned empty string"
    assert 'BALANCED' in reason, f"mode name missing from reason: {reason!r}"


# ── 5. NSE non-F&O symbol ─────────────────────────────────────────────────────

def test_nse_non_fo_not_eligible(analyzer):
    result = analyzer.analyze('NSE:RUCHIRA', signal_score=60.0, mode=StrategyMode.BALANCED)
    assert result['fo_eligible'] is False, "RUCHIRA should not be F&O eligible"


def test_nse_non_fo_options_not_available(analyzer):
    result = analyzer.analyze('NSE:RUCHIRA', signal_score=60.0, mode=StrategyMode.BALANCED)
    assert not result['options_available'], "RUCHIRA should not have options_available"


# ── 6. Earnings risk ──────────────────────────────────────────────────────────

def test_earnings_risk_flag_set(analyzer):
    result = analyzer.analyze(
        'NASDAQ:AAPL', signal_score=70.0,
        mode=StrategyMode.BALANCED, earnings_days=3,
    )
    assert result['earnings_risk'], "earnings_risk should be True when earnings_days=3"


def test_earnings_warning_message_present(analyzer):
    result = analyzer.analyze(
        'NASDAQ:AAPL', signal_score=70.0,
        mode=StrategyMode.BALANCED, earnings_days=3,
    )
    assert any('EARNINGS IN 3 DAYS' in w for w in result['warnings']), (
        f"earnings warning message missing: {result['warnings']}"
    )


# ── 7. analyze_all_modes ──────────────────────────────────────────────────────

def test_analyze_all_modes_has_required_keys(analyzer):
    result = analyzer.analyze_all_modes('NSE:RUCHIRA', signal_score=60.0)
    for key in ('SAFE', 'BALANCED', 'AGGRESSIVE', 'meta'):
        assert key in result, f"analyze_all_modes missing key: {key}"


def test_analyze_all_modes_mode_fields_match(analyzer):
    result = analyzer.analyze_all_modes('NSE:RUCHIRA', signal_score=60.0)
    for mode in ('SAFE', 'BALANCED', 'AGGRESSIVE'):
        assert result[mode].get('mode') == mode, (
            f"mode field mismatch for {mode}: got {result[mode].get('mode')}"
        )


def test_analyze_all_modes_earnings_risk_in_meta(analyzer):
    result = analyzer.analyze_all_modes('NASDAQ:AAPL', signal_score=70.0, earnings_days=2)
    assert result.get('meta', {}).get('earnings_risk'), (
        "meta.earnings_risk should be True when earnings_days=2"
    )


# ── 8. ModeConfig sanity ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", list(MODE_CONFIGS.keys()))
def test_mode_config_dte_range(mode):
    cfg = MODE_CONFIGS[mode]
    assert cfg.dte_min < cfg.dte_max, f"{mode.value}: dte_min >= dte_max"


@pytest.mark.parametrize("mode", list(MODE_CONFIGS.keys()))
def test_mode_config_delta_range(mode):
    cfg = MODE_CONFIGS[mode]
    assert cfg.delta_min < cfg.delta_max, f"{mode.value}: delta_min >= delta_max"


@pytest.mark.parametrize("mode", list(MODE_CONFIGS.keys()))
def test_mode_config_spread_positive(mode):
    cfg = MODE_CONFIGS[mode]
    assert cfg.max_spread_pct > 0, f"{mode.value}: max_spread_pct <= 0"


def test_safe_stricter_spread_than_aggressive():
    safe_spread = MODE_CONFIGS[StrategyMode.SAFE].max_spread_pct
    agg_spread  = MODE_CONFIGS[StrategyMode.AGGRESSIVE].max_spread_pct
    assert safe_spread <= agg_spread, "SAFE spread should be tighter than AGGRESSIVE"


def test_safe_stricter_delta_than_aggressive():
    safe_delta = MODE_CONFIGS[StrategyMode.SAFE].delta_min
    agg_delta  = MODE_CONFIGS[StrategyMode.AGGRESSIVE].delta_min
    assert safe_delta >= agg_delta, "SAFE delta_min should be higher than AGGRESSIVE"


# ── 9. Empty contracts (null fetcher) ─────────────────────────────────────────

class _NullAnalyzer(OptionsAnalyzer):
    def _fetch_us(self, sym): return []
    def _fetch_hk(self, sym): return []
    def _fetch_india(self, sym, tok): return []


def test_empty_contracts_not_available():
    result = _NullAnalyzer().analyze(
        'NASDAQ:AAPL', signal_score=60.0, mode=StrategyMode.BALANCED,
    )
    assert not result['options_available'], "empty fetch should give options_available=False"


def test_empty_contracts_has_reason():
    result = _NullAnalyzer().analyze(
        'NASDAQ:AAPL', signal_score=60.0, mode=StrategyMode.BALANCED,
    )
    assert result['no_options_reason'], "empty fetch should populate no_options_reason"


# ── 10. HK market detection ───────────────────────────────────────────────────

def test_hkex_detected_as_hk_market(analyzer):
    result = analyzer.analyze('HKEX:700', signal_score=65.0, mode=StrategyMode.BALANCED)
    assert result['market'] == 'HK', f"HKEX:700 market should be HK, got {result['market']}"


# ── 11. Signal score contribution ────────────────────────────────────────────

def test_signal_score_contributes_to_option_score(analyzer, cfg_bal):
    s0   = analyzer._filter_and_score([make_contract()], 0.0,   cfg_bal)
    s100 = analyzer._filter_and_score([make_contract()], 100.0, cfg_bal)
    assert s0 and s100, "both score=0 and score=100 contracts should pass filter"
    diff = s100[0]['option_score'] - s0[0]['option_score']
    expected_diff = (10.0 - 0.0) * 0.30 * 10   # max trend contribution
    assert abs(diff - expected_diff) <= 1.0, (
        f"signal score contribution wrong: diff={diff:.1f}, expected~{expected_diff:.1f}"
    )


# ── 12. analyze() result keys ─────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    'symbol', 'market', 'mode', 'signal_score', 'options_available',
    'fo_eligible', 'spot_price', 'warnings', 'earnings_risk',
    'top_contracts', 'no_options_reason',
])
def test_analyze_result_has_key(analyzer, key):
    result = analyzer.analyze('NASDAQ:AAPL', signal_score=70.0, mode=StrategyMode.BALANCED)
    assert key in result, f"analyze() result missing key: {key}"


# ── 13. MAX_CONTRACTS enforced ────────────────────────────────────────────────

def test_max_contracts_enforced(analyzer, cfg_bal):
    many = [make_contract(oi=500 + i, volume=200 + i, dte=15 + i) for i in range(20)]
    scored = analyzer._filter_and_score(many, 70.0, cfg_bal)
    top = scored[:analyzer.MAX_CONTRACTS]
    assert len(top) <= analyzer.MAX_CONTRACTS, (
        f"MAX_CONTRACTS={analyzer.MAX_CONTRACTS} not enforced, got {len(top)}"
    )

# ─── Additional imports for Moneyball + DB tests ─────────────────────────────
from options.options_analyzer import _mb_percentile_rank, _mb_chain_stats, _mb_score_contract
from alert_tracker import AlertTracker


# ── 12. Moneyball unit tests ──────────────────────────────────────────────────

def test_mb_percentile_rank_empty_population():
    """12a. Empty population returns 50.0 (neutral default)."""
    assert _mb_percentile_rank(50.0, []) == 50.0


def test_mb_percentile_rank_single_value():
    """12b. Single-element population returns 50.0 (no meaningful rank)."""
    assert _mb_percentile_rank(50.0, [50.0]) == 50.0


def test_mb_percentile_rank_90th_percentile():
    """12c. Percentile rank of 90 in [10, 20, ..., 100].

    Math: values < 90 = 8, values == 90 = 1
          rank = 8 + 0.5*1 = 8.5
          result = round(100.0 * 8.5 / 10, 1) = 85.0
    """
    pop = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert _mb_percentile_rank(90.0, pop) == 85.0


def test_mb_chain_stats_returns_expected_keys():
    """12d. _mb_chain_stats returns dict with keys 'vols', 'ois', 'ivs', 'spreads'."""
    contracts = [
        make_contract(volume=100, oi=500, iv=0.35, spread_pct=3.0),
        make_contract(volume=200, oi=800, iv=0.40, spread_pct=4.0),
    ]
    stats = _mb_chain_stats(contracts)
    for key in ('vols', 'ois', 'ivs', 'spreads'):
        assert key in stats, f"_mb_chain_stats missing key: {key}"


def test_mb_chain_stats_filters_zero_and_none():
    """12e. _mb_chain_stats excludes zero-volume/oi/spread and None IV contracts."""
    contracts = [
        make_contract(volume=0, oi=0, iv=None, spread_pct=0),
        make_contract(volume=100, oi=500, iv=0.35, spread_pct=3.0),
    ]
    stats = _mb_chain_stats(contracts)
    assert 0    not in stats['vols'],    "zero volume must be excluded from vols"
    assert 0    not in stats['ois'],     "zero OI must be excluded from ois"
    assert None not in stats['ivs'],     "None IV must be excluded from ivs"
    assert 0    not in stats['spreads'], "zero spread must be excluded from spreads"
    assert stats['vols']    == [100],    f"vols should be [100], got {stats['vols']}"
    assert stats['ois']     == [500],    f"ois should be [500], got {stats['ois']}"
    assert stats['ivs']     == [0.35],   f"ivs should be [0.35], got {stats['ivs']}"
    assert stats['spreads'] == [3.0],    f"spreads should be [3.0], got {stats['spreads']}"


def test_mb_score_contract_range():
    """12f. _mb_score_contract returns a value in [0, 100] for every contract in a pool."""
    contracts = [
        make_contract(volume=100,  oi=500,   iv=0.35, spread_pct=3.0),
        make_contract(volume=200,  oi=1000,  iv=0.30, spread_pct=2.0),
        make_contract(volume=50,   oi=200,   iv=0.50, spread_pct=6.0),
    ]
    stats = _mb_chain_stats(contracts)
    for c in contracts:
        score = _mb_score_contract(c, stats)
        assert 0.0 <= score <= 100.0, f"score {score} out of range [0, 100]"


def test_mb_score_high_quality_beats_mid_quality():
    """12g. High-quality contract (high vol/OI, low IV, low spread) scores higher
    than mid-quality in the same chain.

    Computed expected values:
      high_c: 83.3   mid_c: 50.0
    """
    low_c  = make_contract(volume=50,   oi=200,   iv=0.70, spread_pct=8.0)
    mid_c  = make_contract(volume=200,  oi=800,   iv=0.40, spread_pct=4.0)
    high_c = make_contract(volume=5000, oi=20000, iv=0.20, spread_pct=1.0)
    stats = _mb_chain_stats([low_c, mid_c, high_c])
    score_high = _mb_score_contract(high_c, stats)
    score_mid  = _mb_score_contract(mid_c,  stats)
    assert score_high > score_mid, (
        f"high-quality score ({score_high}) should beat mid-quality ({score_mid})"
    )


def test_mb_iv_inversion():
    """12h. Lower IV earns a higher IV sub-score because IV rank is inverted.

    Pool: [iv=0.20, iv=0.70]
      low_iv  rank=25.0 → iv_s = (100-25)/10 = 7.5 → total score 56.2
      high_iv rank=75.0 → iv_s = (100-75)/10 = 2.5 → total score 43.8
    """
    low_iv  = make_contract(volume=100, oi=500, iv=0.20, spread_pct=3.0)
    high_iv = make_contract(volume=100, oi=500, iv=0.70, spread_pct=3.0)
    stats = _mb_chain_stats([low_iv, high_iv])
    score_low_iv  = _mb_score_contract(low_iv,  stats)
    score_high_iv = _mb_score_contract(high_iv, stats)
    assert score_low_iv > score_high_iv, (
        f"lower IV ({score_low_iv:.1f}) should score higher than high IV ({score_high_iv:.1f})"
    )


# ── 13. final_score integration ───────────────────────────────────────────────

def test_single_contract_option_score_equals_current_score(analyzer, cfg_bal):
    """13a. Single-contract chain: Moneyball skipped — option_score == current_score."""
    scored = analyzer._filter_and_score([make_contract()], 70.0, cfg_bal)
    assert len(scored) == 1, "good contract should pass filter"
    c = scored[0]
    assert c['option_score'] == c['current_score'], (
        f"single-contract: option_score ({c['option_score']}) != current_score ({c['current_score']})"
    )


def test_multi_contract_option_score_equals_final_score(analyzer, cfg_bal):
    """13b. Multi-contract chain: option_score == final_score (Moneyball blended)."""
    contracts = [
        make_contract(volume=200 + i * 100, oi=500 + i * 200, dte=20 + i)
        for i in range(3)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    assert len(scored) >= 2, f"expected >=2 contracts to pass, got {len(scored)}"
    for c in scored:
        assert c['option_score'] == c['final_score'], (
            f"option_score ({c['option_score']}) != final_score ({c['final_score']})"
        )


def test_multi_contract_final_score_formula(analyzer, cfg_bal):
    """13c. final_score = round(0.60 * current_score + 0.40 * moneyball_score, 1)."""
    contracts = [
        make_contract(volume=100 + i * 50, oi=500 + i * 100, dte=18 + i)
        for i in range(3)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    assert len(scored) >= 2, f"expected >=2 contracts, got {len(scored)}"
    for c in scored:
        expected = round(0.60 * c['current_score'] + 0.40 * c['moneyball_score'], 1)
        assert c['final_score'] == expected, (
            f"final_score formula mismatch: "
            f"0.60*{c['current_score']} + 0.40*{c['moneyball_score']} = {expected}, "
            f"got {c['final_score']}"
        )


def test_multi_contract_sorted_by_option_score(analyzer, cfg_bal):
    """13d. _filter_and_score output is sorted by option_score descending."""
    contracts = [
        make_contract(volume=100 + i * 50, oi=500 + i * 100, dte=18 + i)
        for i in range(4)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    assert len(scored) >= 2, f"expected >=2 contracts, got {len(scored)}"
    for i in range(len(scored) - 1):
        assert scored[i]['option_score'] >= scored[i + 1]['option_score'], (
            f"contracts not sorted descending: scored[{i}]={scored[i]['option_score']} "
            f"< scored[{i+1}]={scored[i+1]['option_score']}"
        )


def test_moneyball_score_key_exists(analyzer, cfg_bal):
    """13e. moneyball_score key is present in every scored contract."""
    contracts = [
        make_contract(volume=200 + i * 100, oi=500 + i * 200)
        for i in range(2)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    for c in scored:
        assert 'moneyball_score' in c, "moneyball_score key missing from scored contract"


def test_final_score_key_exists(analyzer, cfg_bal):
    """13f. final_score key is present in every scored contract."""
    contracts = [
        make_contract(volume=200 + i * 100, oi=500 + i * 200)
        for i in range(2)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    for c in scored:
        assert 'final_score' in c, "final_score key missing from scored contract"


def test_current_score_equals_pre_moneyball_composite(analyzer, cfg_bal):
    """13g. current_score exists and equals the raw composite score (before Moneyball blend).

    current_score is set to composite before the Moneyball blend so callers can
    always inspect what the absolute-threshold scorer alone decided.
    """
    contracts = [
        make_contract(volume=200 + i * 100, oi=500 + i * 200)
        for i in range(2)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    for c in scored:
        assert 'current_score' in c, "current_score key missing"
        # Recompute pre-Moneyball composite from the stored sub-scores.
        # signal_score=70 → trend_s = 70/100 * 10 = 7.0 (within [0,10], no clamping)
        expected = _composite_option_score(
            7.0, c['liq_score'], c['iv_score'], c['strike_score'], c['dte_score'],
        )
        assert c['current_score'] == expected, (
            f"current_score ({c['current_score']}) != pre-Moneyball composite ({expected})"
        )


# ── 14. DB schema guardrail ───────────────────────────────────────────────────

def test_save_options_recommendation_persists_moneyball_and_final_score(tmp_path):
    """14a. save_options_recommendation stores moneyball_score and final_score."""
    import sqlite3 as _sqlite3
    db_path = str(tmp_path / "test_options.db")
    tracker = AlertTracker(db_path=db_path)
    rec = {
        "run_date":        "2026-05-05",
        "tv_symbol":       "NASDAQ:AAPL",
        "mode":            "BALANCED",
        "option_type":     "CALL",
        "strike":          200.0,
        "expiry":          "2026-06-20",
        "rank":            1,
        "option_score":    72.5,
        "moneyball_score": 68.0,
        "final_score":     70.7,
        "current_score":   73.5,
        "liq_score":       7.0,
        "iv_score":        6.5,
        "strike_score":    8.0,
        "dte_score":       7.5,
        "signal_score":    70.0,
        "market":          "US",
    }
    tracker.save_options_recommendation(rec)
    with _sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT moneyball_score, final_score FROM options_recommendations LIMIT 1"
        ).fetchone()
    assert row is not None, "No row saved to options_recommendations"
    assert row[0] == 68.0, f"moneyball_score persisted wrong: expected 68.0, got {row[0]}"
    assert row[1] == 70.7, f"final_score persisted wrong: expected 70.7, got {row[1]}"


def test_options_recommendations_schema_has_moneyball_and_final_score(tmp_path):
    """14b. DB schema for options_recommendations includes moneyball_score and final_score."""
    import sqlite3 as _sqlite3
    db_path = str(tmp_path / "test_schema.db")
    AlertTracker(db_path=db_path)   # creates schema
    with _sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(options_recommendations)")}
    assert "moneyball_score" in cols, (
        "options_recommendations table missing 'moneyball_score' column"
    )
    assert "final_score" in cols, (
        "options_recommendations table missing 'final_score' column"
    )


# ── 15. Ranking uses final_score ──────────────────────────────────────────────

def test_ranking_uses_final_score(analyzer, cfg_bal):
    """15a. After Moneyball blending, the best contract has final_score >= second best."""
    contracts = [
        make_contract(volume=5000, oi=50000, spread_pct=1.0, iv=0.25, dte=21, moneyness_pct=0.0),
        make_contract(volume=100,  oi=300,   spread_pct=7.5, iv=0.70, dte=18, moneyness_pct=5.0),
        make_contract(volume=300,  oi=1000,  spread_pct=4.0, iv=0.40, dte=25, moneyness_pct=1.0),
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    assert len(scored) >= 2, f"expected >=2 contracts, got {len(scored)}"
    assert scored[0]['final_score'] >= scored[1]['final_score'], (
        f"top contract final_score ({scored[0]['final_score']}) < "
        f"second contract final_score ({scored[1]['final_score']})"
    )


# ── 16. Guardrail: analyze() top_contracts contain all Moneyball keys ─────────

@pytest.mark.parametrize("key", [
    "option_score", "current_score", "moneyball_score", "final_score",
    "liq_score", "iv_score", "strike_score", "dte_score",
    "mb_active",
])
def test_top_contract_has_key(analyzer, key):
    """16. GUARDRAIL: top_contracts[0] must have all Moneyball scoring keys.

    Prevents silent feature loss: if a future refactor drops any of these from
    the scored dict, the DB will silently store None and consumers silently
    use the pre-Moneyball score without any test failure.
    """
    result = analyzer.analyze("NASDAQ:AAPL", signal_score=70.0, mode=StrategyMode.BALANCED)
    contracts = result.get("top_contracts", [])
    assert contracts, "analyze() returned no top_contracts — expected at least one"
    assert key in contracts[0], (
        f"top_contracts[0] missing key '{key}' — Moneyball feature may be broken"
    )

# ─── Additional imports for display tests ─────────────────────────────────────
from options.options_report import _contract_card
from options.options_badge import _pick_row_html


def _make_full_contract(**kwargs):
    """Minimal contract dict suitable for _contract_card and _pick_row_html."""
    base = {
        'strike': 200.0, 'expiry': '2026-06-20', 'dte': 21,
        'mid_price': 2.0, 'iv': 0.35, 'delta': 0.50, 'theta': -0.05,
        'moneyness_pct': 0.0, 'spot_price': 200.0, 'volume': 500,
        'oi': 2000, 'spread_pct': 3.0,
        'liq_score': 7.0, 'iv_score': 6.5, 'strike_score': 8.0, 'dte_score': 7.5,
        'option_score': 72.0, 'current_score': 70.0,
        'moneyball_score': 75.0, 'final_score': 72.0,
        'recommendation_reason': 'Good contract',
        '_contract_warnings': [],
        'mb_active': False,
    }
    base.update(kwargs)
    return base


# ── 17. mb_active flag behavior ────────────────────────────────────────────────

def test_mb_active_true_for_multi_contract_chain(analyzer, cfg_bal):
    """17a. Chains with >=2 scored contracts set mb_active=True on each contract."""
    contracts = [
        make_contract(volume=200 + i * 100, oi=500 + i * 200, dte=20 + i)
        for i in range(3)
    ]
    scored = analyzer._filter_and_score(contracts, 70.0, cfg_bal)
    assert len(scored) >= 2, f"expected >=2 contracts to pass filter, got {len(scored)}"
    for c in scored:
        assert c.get("mb_active") is True, (
            f"mb_active should be True for multi-contract chain, got {c.get('mb_active')!r}"
        )


def test_mb_active_false_for_single_contract(analyzer, cfg_bal):
    """17b. A single-contract chain sets mb_active=False."""
    scored = analyzer._filter_and_score([make_contract()], 70.0, cfg_bal)
    assert len(scored) == 1, f"expected exactly 1 contract, got {len(scored)}"
    assert scored[0].get("mb_active") is False, (
        f"mb_active should be False for single-contract chain, got {scored[0].get('mb_active')!r}"
    )


# ── 18. Trio block in options_report._contract_card() ─────────────────────────

def test_trio_block_shown_when_mb_active():
    """18a. 'Blended (60/40)' appears in the card HTML when mb_active=True."""
    c = _make_full_contract(mb_active=True, current_score=70.0,
                            moneyball_score=75.0, final_score=72.0)
    html = _contract_card(c, 1, "US")
    assert "Blended (60/40)" in html, (
        "trio block must contain 'Blended (60/40)' when mb_active=True, "
        "but it was not found in the rendered card"
    )


def test_trio_block_hidden_when_not_mb_active():
    """18b. 'Blended' does NOT appear in the card HTML when mb_active=False."""
    c = _make_full_contract(mb_active=False, current_score=70.0,
                            moneyball_score=70.0, final_score=70.0)
    html = _contract_card(c, 1, "US")
    assert "Blended" not in html, (
        "trio block must be hidden when mb_active=False, "
        "but 'Blended' was found in the rendered card"
    )


# ── 19. options_badge._pick_row_html() Gate·MB display ────────────────────────

def test_badge_shows_gate_mb_when_mb_active():
    """19a. When mb_active=True, rendered HTML contains Gate·MB label."""
    c = {
        'strike': 200.0, 'expiry': '2026-06-20', 'mid_price': 2.0,
        'iv': 0.35, 'delta': 0.50, 'oi': 500, 'dte': 21,
        'option_score': 65.0, 'current_score': 65.0, 'moneyball_score': 78.0,
        'mb_active': True, 'recommendation_reason': '', 'spread_pct': 3.0,
        'moneyness_pct': 0.0,
    }
    html = _pick_row_html(c, 1, "$")
    # Unicode: \u00a0 = non-breaking space, \u00b7 = middle dot
    expected = "Gate\u00a065\u00a0\u00b7\u00a0MB\u00a078"
    assert expected in html, (
        f"Expected Gate.MB label {expected!r} in badge HTML when mb_active=True, "
        f"not found. HTML snippet: {html[:500]}"
    )


def test_badge_hides_gate_mb_when_not_mb_active():
    """19b. When mb_active=False, Gate.MB label is absent from badge HTML."""
    import re as _re
    c = {
        'strike': 200.0, 'expiry': '2026-06-20', 'mid_price': 2.0,
        'iv': 0.35, 'delta': 0.50, 'oi': 500, 'dte': 21,
        'option_score': 65.0, 'current_score': 65.0, 'moneyball_score': None,
        'mb_active': False, 'recommendation_reason': '', 'spread_pct': 3.0,
        'moneyness_pct': 0.0,
    }
    html = _pick_row_html(c, 1, "$")
    assert 'Gate ' not in html, (
        "Gate.MB label must NOT appear when mb_active=False, "
        f"but found in: {html[:500]}"
    )

