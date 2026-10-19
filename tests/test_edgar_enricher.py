"""Tests for edgar_enricher.py Form 4 transaction type filtering (H-01)."""
import pytest
from unittest.mock import MagicMock


def test_form4_purchase_check_returns_bool():
    """_has_purchase_in_form4 must return a bool."""
    from tradingview.edgar_enricher import _has_purchase_in_form4
    mock_session = MagicMock()
    mock_session.get.side_effect = Exception("connection refused")
    result = _has_purchase_in_form4("0001234567890", "0001234-26-000001", mock_session)
    assert isinstance(result, bool)
    assert result is False  # fail-safe


def test_form4_purchase_detected_in_xml():
    """_has_purchase_in_form4 correctly identifies 'P' transaction code."""
    from tradingview.edgar_enricher import _has_purchase_in_form4
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<XML><transactionCode>P</transactionCode></XML>'
    mock_session.get.return_value = mock_resp
    result = _has_purchase_in_form4("0001234567890", "0001234-26-000001", mock_session)
    assert result is True


def test_form4_sale_not_counted_as_purchase():
    """Form 4 with transactionCode 'S' (sale) must return False from purchase check."""
    from tradingview.edgar_enricher import _has_purchase_in_form4
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<XML><transactionCode>S</transactionCode></XML>'
    mock_session.get.return_value = mock_resp
    result = _has_purchase_in_form4("0001234567890", "0001234-26-000001", mock_session)
    assert result is False


def test_enricher_has_purchase_function():
    """edgar_enricher.py must expose _has_purchase_in_form4 function."""
    import tradingview.edgar_enricher as ee
    assert hasattr(ee, "_has_purchase_in_form4"), (
        "edgar_enricher must have _has_purchase_in_form4 for H-01 fix"
    )
