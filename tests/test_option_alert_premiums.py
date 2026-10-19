from datetime import datetime, timedelta, timezone

import pytest

from tradingview.options.options_snapshot import (
    build_snapshot, normalize_contract, publish_snapshot,
)
from tradingview.scheduler.cycle import _options_result_from_snapshot
from tradingview.scheduler.formatting import option_card


def test_premium_survives_snapshot_and_both_alert_consumers(tmp_path, monkeypatch):
    import report_scheduler

    now = datetime.now(timezone.utc)
    contract = normalize_contract("NYSE:RTX", 63.2, "US", "BALANCED", {
        "option_type": "CALL", "strike": 195,
        "expiry": (now + timedelta(days=21)).date().isoformat(), "dte": 21,
        "option_score": 74.9, "mid_price": 4.25, "bid": 4.1, "ask": 4.4,
        "last_price": 4.2,
    }, reference_now=now)
    snapshot = build_snapshot("premium-test", "success", [contract])
    path = tmp_path / "options.json"
    publish_snapshot(path, snapshot)
    modern = _options_result_from_snapshot(path, "premium-test", 70)
    monkeypatch.setattr(report_scheduler, "OPTIONS_SNAPSHOT", path)
    legacy = report_scheduler._extract_options_alerts(70, "premium-test")
    for result in (modern, legacy):
        row = result["options"][0]
        assert row["mid_price"] == 4.25
        assert row["premium_currency"] == "USD"
        text = option_card(row)
        assert "Premium USD 4.25/unit · mid" in text
        assert "Bid 4.10 · Ask 4.40" in text
        assert "Score 74.9/100" in text
        assert "<pre>" not in text


@pytest.mark.parametrize("quotes,expected", [
    ({}, "Premium unavailable"),
    ({"mid_price": 0, "last_price": float("nan")}, "Premium unavailable"),
    ({"mid_price": True, "ask": float("inf")}, "Premium unavailable"),
    ({"bid": 5, "ask": 4, "last_price": 4.2}, "Premium USD 4.20/unit · last trade"),
    ({"mid_price": 4.25}, "Premium USD 4.25/unit · reference"),
    ({"market": "NSE", "mid_price": 125}, "Premium INR 125.00/unit · reference"),
])
def test_premium_fallbacks_are_explicit(quotes, expected):
    text = option_card({"market": "US", "exchange": "NYSE", "symbol": "RTX",
                        "score": 75, **quotes})
    assert expected in text
    assert "Bid " not in text


def test_reference_scanner_card_disclaims_freshness():
    text = option_card({
        "source": "scanner", "market": "US", "exchange": "NASDAQ", "symbol": "AAPL",
        "option_type": "CALL", "strike": 200, "expiry": "2026-11-20",
        "bid": 1.9, "ask": 2.1, "mid_price": 2.0, "premium_currency": "USD",
        "freshness_status": "reference_unverified", "tv_update_time": "2026-10-19T14:59:00+00:00",
    })
    assert "Premium USD 2.00/unit · reference" in text
    assert "Freshness unverified" in text
    assert "not a bid/ask timestamp" in text
    assert "Bid quote" not in text
