import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fire_measurement():
    tree = ast.parse((ROOT / "tradingview/signal_stack.py").read_text())
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_fire_outcome_measurement"
    )
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "signal_stack", "exec"), namespace)
    return namespace["_fire_outcome_measurement"]


def _fire(source="download", **overrides):
    fire = {
        "fire_id": 7,
        "_source": source,
        "fire_time": "2026-01-01T12:00:00+00:00",
        "bar_time": "2026-01-01T12:00:00+00:00",
        "message": "BUY price: 100.50",
    }
    fire.update(overrides)
    return fire


ANALYSIS_AT = "2026-01-01T12:00:03+00:00"


def test_verified_message_price_is_timestamped_at_fire(fire_measurement):
    result = fire_measurement(_fire(), ANALYSIS_AT)
    assert result["provenance_status"] == "valid"
    assert result["entry_price"] == 100.5
    assert result["entry_price_at"] == "2026-01-01T12:00:00+00:00"
    assert result["analysis_at"] == ANALYSIS_AT


def test_structured_price_precedes_message_price(fire_measurement):
    result = fire_measurement(
        _fire(price=101.25, bar_time="2026-01-01T12:00:00+00:00"), ANALYSIS_AT
    )
    assert result["provenance_status"] == "valid"
    assert result["entry_price"] == 101.25
    assert result["entry_price_source"] == "structured_bar_price"


@pytest.mark.parametrize("source", ["screener", "news", "holdings", "manual"])
def test_non_contract_sources_remain_unknown(fire_measurement, source):
    result = fire_measurement(_fire(source=source), ANALYSIS_AT)
    assert result["provenance_status"] == "unknown"
    assert result["provenance_reason"]
    assert "entry_price" not in result


def test_missing_price_is_unknown_with_reason(fire_measurement):
    result = fire_measurement(_fire(message="BUY"), ANALYSIS_AT)
    assert result["provenance_status"] == "unknown"
    assert result["provenance_reason"] == "no structured or verified message price"


@pytest.mark.parametrize("message", ["BUY price: 100 price: 101", "BUY price: nan"])
def test_ambiguous_or_malformed_price_is_invalid(fire_measurement, message):
    result = fire_measurement(_fire(message=message), ANALYSIS_AT)
    assert result["provenance_status"] == "invalid"
    assert result["provenance_reason"]


def test_naive_fire_timestamp_is_invalid(fire_measurement):
    result = fire_measurement(
        _fire(fire_time="2026-01-01T12:00:00", bar_time="2026-01-01T12:00:00"),
        ANALYSIS_AT,
    )
    assert result["provenance_status"] == "invalid"
    assert "timezone-aware" in result["provenance_reason"]


def test_each_verified_source_type_can_be_valid(fire_measurement):
    for source in ("download", "webhook"):
        result = fire_measurement(_fire(source=source), ANALYSIS_AT)
        assert result["provenance_status"] == "valid"


def test_naive_analysis_timestamp_is_invalid(fire_measurement):
    result = fire_measurement(_fire(), "2026-01-01T12:00:03")
    assert result["provenance_status"] == "invalid"
    assert "timezone-aware" in result["provenance_reason"]