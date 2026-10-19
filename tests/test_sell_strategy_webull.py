"""Webull live API and PDF fallback tests for sell_strategy."""

import sys
from unittest.mock import MagicMock, patch

from tradingview.sell import sell_strategy as ss


def test_live_api_positions_are_authoritative():
    api_positions = [{"symbol": "NASDAQ:AAPL", "shares": 10}]
    with patch.object(ss, "_fetch_webull_positions_api", return_value=api_positions), \
         patch.object(ss, "_fetch_webull_positions_pdf") as fetch_pdf:
        result = ss._fetch_webull_positions()

    assert result == api_positions
    fetch_pdf.assert_not_called()


def test_successful_empty_api_response_does_not_use_pdf():
    with patch.object(ss, "_fetch_webull_positions_api", return_value=[]), \
         patch.object(ss, "_fetch_webull_positions_pdf") as fetch_pdf:
        result = ss._fetch_webull_positions()

    assert result == []
    fetch_pdf.assert_not_called()


def test_api_unavailable_uses_pdf_fallback():
    pdf_positions = [{"symbol": "ASX:CBA", "shares": 5}]
    with patch.object(ss, "_fetch_webull_positions_api", return_value=None), \
         patch.object(ss, "_fetch_webull_positions_pdf", return_value=pdf_positions) as fetch_pdf:
        result = ss._fetch_webull_positions()

    assert result == pdf_positions
    fetch_pdf.assert_called_once_with()


def test_live_api_empty_status_maps_to_authoritative_empty_list():
    api_module = MagicMock()
    api_module.is_configured.return_value = True
    api_module.get_holdings.return_value = ([], "Live API — 0 positions")

    with patch.dict(sys.modules, {"webull_au_api": api_module}):
        result = ss._fetch_webull_positions_api()

    assert result == []


def test_api_error_status_maps_to_unavailable():
    api_module = MagicMock()
    api_module.is_configured.return_value = True
    api_module.get_holdings.return_value = ([], "API error: timeout")

    with patch.dict(sys.modules, {"webull_au_api": api_module}):
        result = ss._fetch_webull_positions_api()

    assert result is None
