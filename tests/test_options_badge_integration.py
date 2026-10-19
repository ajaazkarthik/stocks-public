"""
Options Badge integration smoke tests + display correctness checks.

Originally a standalone script (python test_options_badge_integration.py).
Converted to pytest-compatible format so `pytest test_options_badge_integration.py`
works and integrates with the full test suite.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tradingview"))
sys.path.insert(0, str(ROOT / "tradingview" / "options"))

from options.options_badge import (
    compute_options_badge_data, _options_html_badge, OPTIONS_BADGE_CSS,
    _parse_exchange, _exp_label, _fmt_price, _fmt_oi, _score_bar_html,
    _pick_row_html,
)


# ── shared fixture data ────────────────────────────────────────────────────────

def _make_empty_mode(mode: str) -> dict:
    return {
        'mode': mode, 'options_available': False, 'top_contracts': [],
        'no_options_reason': f'No contracts for {mode}.',
        'market': 'US', 'symbol': 'NASDAQ:MU', 'signal_score': 65.0,
        'spot_price': 420.0, 'data_source': 'yfinance',
        'earnings_risk': False, 'warnings': [], 'fo_eligible': True,
    }


_SAMPLE_CONTRACT = {
    'strike': 425, 'expiry': '2026-05-01', 'dte': 17,
    'mid_price': 12.5, 'iv': 0.45, 'delta': 0.55,
    'oi': 714, 'volume': 200, 'spread_pct': 2.1,
    'moneyness_pct': -0.5, 'market': 'US', 'option_score': 72.0,
    'liq_score': 7.0, 'iv_score': 7.0, 'strike_score': 8.0, 'dte_score': 8.0,
    'recommendation_reason': 'ATM call, good liquidity', 'source': 'yfinance',
    '_contract_warnings': [],
}

_BADGE_ACTIVE = {
    'BALANCED': {
        **_make_empty_mode('BALANCED'),
        'options_available': True,
        'top_contracts': [dict(_SAMPLE_CONTRACT)],
    },
    'AGGRESSIVE': _make_empty_mode('AGGRESSIVE'),
    'SAFE':       _make_empty_mode('SAFE'),
    'meta': {
        'market': 'US', 'symbol': 'NASDAQ:MU', 'spot_price': 427.0,
        'data_source': 'yfinance', 'earnings_risk': False, 'signal_score': 65.0,
    },
    'options_available': True, 'badge_exchange': 'NASDAQ', 'badge_ticker': 'MU',
}

_BADGE_ALL_EMPTY = {
    'BALANCED':   _make_empty_mode('BALANCED'),
    'AGGRESSIVE': _make_empty_mode('AGGRESSIVE'),
    'SAFE':       _make_empty_mode('SAFE'),
    'meta': {
        'market': 'US', 'symbol': 'NASDAQ:MU', 'spot_price': 420.0,
        'data_source': 'yfinance', 'earnings_risk': False, 'signal_score': 65.0,
    },
    'options_available': False, 'badge_exchange': 'NASDAQ', 'badge_ticker': 'MU',
}


# ── 1. Helper: _parse_exchange ─────────────────────────────────────────────────

@pytest.mark.parametrize("sym,expected", [
    ('NASDAQ:MU',  ('NASDAQ', 'MU')),
    ('NSE:INFY',   ('NSE', 'INFY')),
    ('HKEX:700',   ('HKEX', '700')),
    ('AAPL',       ('NYSE', 'AAPL')),   # bare ticker defaults to NYSE
])
def test_parse_exchange(sym, expected):
    assert _parse_exchange(sym) == expected


# ── 2. Helper: _exp_label ──────────────────────────────────────────────────────

def test_exp_label_standard_date():
    assert _exp_label('2026-04-24') == '24 Apr'


def test_exp_label_strips_leading_zero():
    assert _exp_label('2026-01-05') == '5 Jan'


def test_exp_label_none_returns_empty():
    assert _exp_label(None) == ''


# ── 3. Helper: _fmt_price ─────────────────────────────────────────────────────

def test_fmt_price_zero_returns_em_dash():
    assert _fmt_price(0) == '\u2014'


def test_fmt_price_large_no_decimal():
    assert _fmt_price(15000) == '15,000'


def test_fmt_price_mid_one_decimal():
    assert _fmt_price(150.0) == '150.0'


def test_fmt_price_small_two_decimal():
    assert _fmt_price(2.50) == '2.50'


# ── 4. Helper: _fmt_oi ────────────────────────────────────────────────────────

def test_fmt_oi_millions():
    assert _fmt_oi(2_000_000) == '2.0M'


def test_fmt_oi_thousands_banker_rounding():
    assert _fmt_oi(5_500) == '6K'      # 5.5K rounds to 6K (banker's rounding)


def test_fmt_oi_small_raw():
    assert _fmt_oi(200) == '200'


def test_fmt_oi_zero_em_dash():
    assert _fmt_oi(0) == '\u2014'


# ── 5. Helper: _score_bar_html ────────────────────────────────────────────────

def test_score_bar_structure():
    html = _score_bar_html(75.0)
    assert 'ob-score-wrap' in html
    assert 'ob-bar-fill' in html
    assert '75%' in html


def test_score_bar_green_for_high_score():
    assert '#4ade80' in _score_bar_html(75.0)   # >= 70 → green


def test_score_bar_amber_for_mid_score():
    assert '#f59e0b' in _score_bar_html(55.0)   # 50–69 → amber


def test_score_bar_red_for_low_score():
    assert '#f87171' in _score_bar_html(40.0)   # < 50 → red


# ── 6. compute_options_badge_data: unsupported markets ────────────────────────

@pytest.mark.parametrize("symbol", ['ASX:CBA', 'SGX:D05'])
def test_unsupported_market_returns_empty_dict(symbol):
    assert compute_options_badge_data(symbol, signal_score=60.0) == {}


# ── 7. compute_options_badge_data: NSE non-F&O ────────────────────────────────

def test_nse_non_fo_not_options_available():
    result = compute_options_badge_data('NSE:RUCHIRA', signal_score=55.0)
    assert not result.get('options_available')


def test_nse_non_fo_market_label():
    result = compute_options_badge_data('NSE:RUCHIRA', signal_score=55.0)
    assert result.get('market') == 'NSE'


def test_nse_non_fo_has_no_options_reason():
    result = compute_options_badge_data('NSE:RUCHIRA', signal_score=55.0)
    assert result.get('no_options_reason'), "no_options_reason must be non-empty"


# ── 8. compute_options_badge_data: legacy fields ──────────────────────────────

_LEGACY_FIELDS = [
    'market', 'symbol', 'spot_price', 'data_source', 'earnings_risk',
    'mode', 'badge_mode', 'fo_eligible', 'options_available',
    'top_contracts', 'warnings', 'no_options_reason',
    'badge_exchange', 'badge_ticker',
]

@pytest.mark.parametrize("field", _LEGACY_FIELDS)
def test_legacy_field_present(field):
    result = compute_options_badge_data('NSE:RUCHIRA', signal_score=43.0)
    assert field in result, f"Missing legacy field: {field}"


# ── 9. badge_mode score thresholds ────────────────────────────────────────────

@pytest.mark.parametrize("score,expected_mode", [
    (40.0, 'SAFE'),
    (60.0, 'BALANCED'),
    (80.0, 'AGGRESSIVE'),
])
def test_badge_mode_thresholds(score, expected_mode):
    result = compute_options_badge_data('NSE:RUCHIRA', signal_score=score)
    assert result.get('badge_mode') == expected_mode


# ── 10. _options_html_badge: empty/None inputs ────────────────────────────────

def test_html_badge_none_returns_empty():
    assert _options_html_badge(None) == ''


def test_html_badge_empty_dict_returns_empty():
    assert _options_html_badge({}) == ''


# ── 11. _options_html_badge: all modes empty → ob-no-opts ────────────────────

def test_all_empty_badge_has_ob_no_opts():
    html = _options_html_badge(_BADGE_ALL_EMPTY)
    assert 'ob-no-opts' in html


def test_all_empty_badge_no_ob_active():
    html = _options_html_badge(_BADGE_ALL_EMPTY)
    assert 'ob-active' not in html


# ── 12. _options_html_badge: badge with picks → ob-active ────────────────────

def test_active_badge_has_ob_active():
    html = _options_html_badge(_BADGE_ACTIVE)
    assert 'ob-active' in html


def test_active_badge_no_ob_no_opts():
    html = _options_html_badge(_BADGE_ACTIVE)
    assert 'ob-no-opts' not in html


# ── 13. HTML structure elements present ───────────────────────────────────────

@pytest.mark.parametrize("css_class", ['ob-tab-radio', 'ob-tab-bar', 'ob-panels', 'ob-panel-inner'])
def test_html_structure_elements(css_class):
    html = _options_html_badge(_BADGE_ACTIVE)
    assert css_class in html


# ── 14. Availability dots ─────────────────────────────────────────────────────

def test_availability_dots_ok_count():
    html = _options_html_badge(_BADGE_ACTIVE)
    assert html.count('ob-dot-ok') == 1     # only BALANCED has picks


def test_availability_dots_na_count():
    html = _options_html_badge(_BADGE_ACTIVE)
    assert html.count('ob-dot-na') == 2     # SAFE + AGGRESSIVE have no picks


# ── 15. CSS constant ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("selector", ['.options-badge', '.ob-active', '.ob-no-opts', '.ob-tab-radio'])
def test_css_constant_contains_selector(selector):
    assert len(OPTIONS_BADGE_CSS) > 100
    assert selector in OPTIONS_BADGE_CSS


# ── 16. Earnings warning banner ───────────────────────────────────────────────

def test_earnings_warning_renders():
    badge = {
        **_BADGE_ACTIVE,
        'BALANCED': {
            **_BADGE_ACTIVE['BALANCED'],
            'earnings_risk': True,
            'warnings': ['Earnings near - IV inflated'],
        },
    }
    assert 'ob-earn-warn' in _options_html_badge(badge)


# ── 17. Spread badge color classes ────────────────────────────────────────────

@pytest.mark.parametrize("spread_pct,expected_class", [
    (2.0,  'ob-liq-good'),
    (8.0,  'ob-liq-warn'),
    (20.0, 'ob-liq-bad'),
])
def test_pick_row_spread_color(spread_pct, expected_class):
    contract = {**_SAMPLE_CONTRACT, 'spread_pct': spread_pct}
    html = _pick_row_html(contract, 1, '$')
    assert expected_class in html


# ── 18. India rupee currency symbol ───────────────────────────────────────────

def test_pick_row_india_rupee_symbol():
    contract = {**_SAMPLE_CONTRACT, 'market': 'NSE'}
    html = _pick_row_html(contract, 1, '&#8377;')
    assert '&#8377;' in html
