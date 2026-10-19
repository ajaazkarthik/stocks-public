from pathlib import Path
import sys
from unittest.mock import patch, MagicMock


TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss


def test_get_options_badge_context_uses_ranked_item_shape():
    item = {
        "tv_symbol": "NSE:BHARATFORG",
        "fundamentals": {"current_price": 1798.8},
        "composite": 57.7,
    }

    tv_symbol, spot_price = ss._get_options_badge_context(item)

    assert tv_symbol == "NSE:BHARATFORG"
    assert spot_price == 1798.8


def test_get_options_badge_context_falls_back_to_legacy_keys():
    item = {
        "pro_symbol": "NASDAQ:MU",
        "close": 420.59,
    }

    tv_symbol, spot_price = ss._get_options_badge_context(item)

    assert tv_symbol == "NASDAQ:MU"
    assert spot_price == 420.59


def test_options_badge_candidates_are_actionable_supported_and_limited():
    ranked = [
        {"tv_symbol": "SGX:D05", "actionable": True, "composite": 90},
        {"tv_symbol": "NASDAQ:NVDA", "actionable": True, "composite": 80},
        {"tv_symbol": "NSE:RELIANCE", "actionable": True, "composite": 75},
        {"tv_symbol": "NYSE:LOW", "actionable": False, "composite": 70},
        {"tv_symbol": "NASDAQ:MSFT", "actionable": True, "composite": 65},
    ]

    selected = ss._select_options_badge_candidates(ranked, top_k=2)

    assert [item["tv_symbol"] for item in selected] == [
        "NASDAQ:NVDA", "NSE:RELIANCE",
    ]


def test_options_badge_candidates_keep_closed_markets_by_default():
    from datetime import datetime, timezone

    ranked = [
        {"tv_symbol": "NASDAQ:NVDA", "actionable": True, "composite": 80},
        {"tv_symbol": "NSE:RELIANCE", "actionable": True, "composite": 75},
    ]
    now = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)

    selected = ss._select_options_badge_candidates(ranked, top_k=2, now_utc=now)

    assert [item["tv_symbol"] for item in selected] == [
        "NASDAQ:NVDA", "NSE:RELIANCE",
    ]


def test_options_badge_candidates_skip_closed_markets_when_opted_in():
    from datetime import datetime, timezone

    ranked = [
        {"tv_symbol": "NASDAQ:NVDA", "actionable": True, "composite": 80},
        {"tv_symbol": "NSE:RELIANCE", "actionable": True, "composite": 75},
    ]
    now = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)

    selected = ss._select_options_badge_candidates(
        ranked, top_k=2, now_utc=now, open_markets_only=True
    )

    assert [item["tv_symbol"] for item in selected] == ["NSE:RELIANCE"]


def test_ranked_item_shape_renders_correct_india_no_options_reason():
    item = {
        "tv_symbol": "NSE:BHARATFORG",
        "fundamentals": {"current_price": 1798.8},
        "composite": 43.0,
    }

    tv_symbol, spot_price = ss._get_options_badge_context(item)
    result = ss._compute_options_badge_data(
        tv_symbol,
        signal_score=float(item["composite"]),
        spot_price=spot_price,
    )

    assert result["market"] == "NSE"
    assert result["options_available"] is False
    assert "NOT in NSE F&O eligible list" in result["no_options_reason"]


# ── Regression: ob-active must never show when all modes have no contracts ───

def _make_empty_mode_data(mode: str) -> dict:
    """Return a mode result dict where options_available=False (no contracts)."""
    return {
        "mode": mode,
        "options_available": False,
        "top_contracts": [],
        "no_options_reason": f"No contracts passed liquidity + {mode} filters.",
        "market": "US",
        "symbol": "NASDAQ:MU",
        "signal_score": 73.97,
        "spot_price": 427.0,
        "data_source": "yfinance",
        "earnings_risk": False,
        "warnings": [],
        "fo_eligible": True,
    }


def test_badge_class_is_ob_no_opts_when_all_modes_empty():
    """Regression: badge must use ob-no-opts when all three modes have no contracts.

    Previously, _options_html_badge() hardcoded ob-active regardless of data.
    This caused the 9pm archive to show 'ob-active' with 3 empty panels.
    """
    from options.options_badge import _options_html_badge

    badge_data = {
        "BALANCED":   _make_empty_mode_data("BALANCED"),
        "AGGRESSIVE": _make_empty_mode_data("AGGRESSIVE"),
        "SAFE":       _make_empty_mode_data("SAFE"),
        "meta": {
            "market": "US",
            "symbol": "NASDAQ:MU",
            "spot_price": 427.0,
            "data_source": "yfinance",
            "earnings_risk": False,
            "signal_score": 73.97,
        },
        "options_available": False,
        "badge_exchange": "NASDAQ",
        "badge_ticker": "MU",
    }

    html = _options_html_badge(badge_data)

    assert "ob-no-opts" in html, "Badge must use ob-no-opts when all modes return no contracts"
    assert "ob-active" not in html, "Badge must NOT use ob-active when all modes return no contracts"


def test_badge_class_is_ob_active_when_any_mode_has_picks():
    """Badge must use ob-active when at least one mode has valid contracts."""
    from options.options_badge import _options_html_badge

    balanced_with_picks = {
        **_make_empty_mode_data("BALANCED"),
        "options_available": True,
        "top_contracts": [{"strike": 425, "expiry": "2026-05-01", "dte": 17,
                           "mid_price": 12.5, "iv": 0.45, "delta": 0.55,
                           "oi": 714, "volume": 200, "spread_pct": 2.1,
                           "moneyness_pct": -0.5, "market": "US",
                           "option_score": 72.0, "liq_score": 7.0,
                           "iv_score": 7.0, "strike_score": 8.0, "dte_score": 8.0,
                           "recommendation_reason": "ATM call, good liquidity",
                           "source": "yfinance",
                           "_contract_warnings": []}],
    }

    badge_data = {
        "BALANCED":   balanced_with_picks,
        "AGGRESSIVE": _make_empty_mode_data("AGGRESSIVE"),
        "SAFE":       _make_empty_mode_data("SAFE"),
        "meta": {
            "market": "US",
            "symbol": "NASDAQ:MU",
            "spot_price": 427.0,
            "data_source": "yfinance",
            "earnings_risk": False,
            "signal_score": 73.97,
        },
        "options_available": True,
        "badge_exchange": "NASDAQ",
        "badge_ticker": "MU",
    }

    html = _options_html_badge(badge_data)

    assert "ob-active" in html, "Badge must use ob-active when at least one mode has contracts"
    assert "ob-no-opts" not in html


def test_badge_mode_dots_consistent_with_availability():
    """ob-dot-ok must appear only when options_available=True for that mode."""
    from options.options_badge import _options_html_badge

    badge_data = {
        "BALANCED":   {**_make_empty_mode_data("BALANCED"), "options_available": True,
                       "top_contracts": []},  # has picks flag but no actual rows
        "AGGRESSIVE": _make_empty_mode_data("AGGRESSIVE"),
        "SAFE":       _make_empty_mode_data("SAFE"),
        "meta": {
            "market": "US", "symbol": "NASDAQ:MU", "spot_price": 427.0,
            "data_source": "yfinance", "earnings_risk": False, "signal_score": 73.97,
        },
        "options_available": True,
        "badge_exchange": "NASDAQ",
        "badge_ticker": "MU",
    }

    html = _options_html_badge(badge_data)

    # BALANCED has options_available=True → must have one ob-dot-ok
    assert html.count("ob-dot-ok") == 1
    # AGGRESSIVE and SAFE have options_available=False → must have two ob-dot-na
    assert html.count("ob-dot-na") == 2