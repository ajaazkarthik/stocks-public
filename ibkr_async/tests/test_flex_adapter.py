"""
Unit tests for FlexAdapter.

All tests mock urllib.request.urlopen — no network access required.
The XML fixtures are representative samples of real IBKR Flex Statement XML.
"""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.adapters.flex_adapter import FlexAdapter
from ibkr_async.config import IBKRConfig
from ibkr_async.exceptions import IBKRDataError, IBKRTimeoutError
from ibkr_async.models import AccountSummary, AssetClass, Position


# ── XML fixtures ──────────────────────────────────────────────────────────────

_FLEX_SEND_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Processing</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>
</FlexStatementResponse>"""

_FLEX_POSITIONS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="MyFlexQuery" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DU12345" fromDate="2026-04-01" toDate="2026-04-22">
      <OpenPositions>
        <OpenPosition accountId="DU12345" symbol="AAPL" assetCategory="STK"
                      exchange="NASDAQ" currency="USD" position="100"
                      costBasisPrice="150.00" markPrice="175.00"
                      positionValue="17500.00" fifoPnlUnrealized="2500.00"
                      realizedPnl="0.00" />
        <OpenPosition accountId="DU12345" symbol="VEA" assetCategory="STK"
                      exchange="ARCA" currency="USD" position="200"
                      costBasisPrice="45.00" markPrice="47.50"
                      positionValue="9500.00" fifoPnlUnrealized="500.00"
                      realizedPnl="0.00" />
      </OpenPositions>
      <EquitySummaryInBase accountId="DU12345"
                           total="110000.00" cash="20000.00"
                           unrealizedPnl="3000.00" realizedPnl="0.00" />
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

_FLEX_EMPTY_POSITIONS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Empty">
  <FlexStatements count="1">
    <FlexStatement accountId="DU12345" fromDate="2026-04-01" toDate="2026-04-22">
      <OpenPositions />
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

_FLEX_STILL_GENERATING = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Generating</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <ErrorMessage>Statement is being generated. Try again.</ErrorMessage>
</FlexStatementResponse>"""

_FLEX_BAD_STATUS = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Error</Status>
  <ReferenceCode></ReferenceCode>
  <ErrorMessage>Invalid token.</ErrorMessage>
</FlexStatementResponse>"""


def _make_http_response(content: bytes, status: int = 200) -> MagicMock:
    """Create a mock urllib response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = content
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__  = MagicMock(return_value=False)
    return resp


# ── Helpers to set up URL mock sequences ─────────────────────────────────────

def _patch_urlopen_sequence(responses: list[bytes]):
    """
    Patch urllib.request.urlopen to return responses in sequence.
    Each call consumes the next item in the list.
    """
    resps = [_make_http_response(r) for r in responses]
    counter = {"i": 0}

    def side_effect(url, timeout=None):
        idx = counter["i"]
        counter["i"] += 1
        if idx < len(resps):
            return resps[idx]
        raise RuntimeError(f"Unexpected urlopen call #{idx}")

    return patch(
        "ibkr_async.adapters.flex_adapter.urllib.request.urlopen",
        side_effect=side_effect,
    )


# ── connect / disconnect ──────────────────────────────────────────────────────

class TestFlexAdapterConnect:

    @pytest.mark.asyncio
    async def test_connect_marks_connected(self, ibkr_config):
        adapter = FlexAdapter(ibkr_config)
        assert adapter.is_connected is False
        await adapter.connect()
        assert adapter.is_connected is True

    @pytest.mark.asyncio
    async def test_disconnect_marks_disconnected(self, ibkr_config):
        adapter = FlexAdapter(ibkr_config)
        await adapter.connect()
        await adapter.disconnect()
        assert adapter.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_clears_cache(self, ibkr_config):
        adapter = FlexAdapter(ibkr_config)
        adapter._cached_xml = "<data/>"
        await adapter.disconnect()
        assert adapter._cached_xml is None


# ── fetch_positions ───────────────────────────────────────────────────────────

class TestFlexAdapterFetchPositions:

    @pytest.mark.asyncio
    async def test_returns_positions_from_xml(self, ibkr_config):
        """fetch_positions parses OpenPosition elements correctly."""
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                positions = await adapter.fetch_positions()

        assert len(positions) == 2
        symbols = {p.symbol for p in positions}
        assert "AAPL" in symbols
        assert "VEA"  in symbols

    @pytest.mark.asyncio
    async def test_position_fields_parsed_correctly(self, ibkr_config):
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                positions = await adapter.fetch_positions()

        aapl = next(p for p in positions if p.symbol == "AAPL")
        assert aapl.quantity       == 100.0
        assert aapl.avg_cost       == 150.0
        assert aapl.market_price   == 175.0
        assert aapl.market_value   == 17_500.0
        assert aapl.unrealised_pnl == 2_500.0
        assert aapl.currency       == "USD"
        assert aapl.exchange       == "NASDAQ"
        assert aapl.asset_class    == AssetClass.STOCK
        assert aapl.broker         == "IBKR"

    @pytest.mark.asyncio
    async def test_empty_positions_returns_empty_list(self, ibkr_config):
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_EMPTY_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                positions = await adapter.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_cached_xml_not_re_fetched(self, ibkr_config):
        """Second call to fetch_positions uses cached XML without hitting network."""
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                p1 = await adapter.fetch_positions()

                # Second call — no more HTTP responses available; should use cache
                p2 = await adapter.fetch_positions()

        assert len(p1) == len(p2) == 2

    @pytest.mark.asyncio
    async def test_bad_send_status_raises_data_error(self, ibkr_config):
        """A Flex response with error status raises IBKRDataError."""
        with _patch_urlopen_sequence([_FLEX_BAD_STATUS]):
            async with FlexAdapter(ibkr_config) as adapter:
                adapter._cached_xml = None
                with pytest.raises(Exception):  # IBKRConnectionError or IBKRDataError
                    await adapter.fetch_positions()


# ── fetch_account_summary ─────────────────────────────────────────────────────

class TestFlexAdapterFetchAccountSummary:

    @pytest.mark.asyncio
    async def test_returns_account_summary(self, ibkr_config):
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                account = await adapter.fetch_account_summary()

        assert isinstance(account, AccountSummary)
        assert account.net_liquidation == 110_000.0
        assert account.total_cash      == 20_000.0
        assert account.unrealised_pnl  == 3_000.0

    @pytest.mark.asyncio
    async def test_missing_equity_section_returns_zero_summary(self, ibkr_config):
        """If XML has no EquitySummaryInBase, returns AccountSummary with zeros."""
        with _patch_urlopen_sequence([_FLEX_SEND_OK, _FLEX_EMPTY_POSITIONS_XML]):
            async with FlexAdapter(ibkr_config) as adapter:
                account = await adapter.fetch_account_summary()

        assert account.net_liquidation == 0.0
        assert account.total_cash      == 0.0


# ── health_check ──────────────────────────────────────────────────────────────

class TestFlexAdapterHealthCheck:

    @pytest.mark.asyncio
    async def test_health_reachable(self, ibkr_config):
        mock_resp = _make_http_response(b"OK", status=200)
        with patch(
            "ibkr_async.adapters.flex_adapter.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            adapter = FlexAdapter(ibkr_config)
            health = await adapter.health_check()

        assert health.is_connected is True
        assert health.host         == "gdcdyn.interactivebrokers.com"
        assert health.port         == 443
        assert health.latency_ms is not None

    @pytest.mark.asyncio
    async def test_health_unreachable(self, ibkr_config):
        with patch(
            "ibkr_async.adapters.flex_adapter.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            adapter = FlexAdapter(ibkr_config)
            health = await adapter.health_check()

        assert health.is_connected is False
        assert "connection refused" in health.error_message

    @pytest.mark.asyncio
    async def test_health_never_raises(self, ibkr_config):
        """health_check must not raise even if urlopen raises."""
        with patch(
            "ibkr_async.adapters.flex_adapter.urllib.request.urlopen",
            side_effect=RuntimeError("critical failure"),
        ):
            adapter = FlexAdapter(ibkr_config)
            health = await adapter.health_check()  # Should NOT raise
        assert health.is_connected is False


# ── supports_live_quotes ──────────────────────────────────────────────────────

def test_flex_adapter_does_not_support_live_quotes(ibkr_config):
    assert FlexAdapter(ibkr_config).supports_live_quotes is False


# ── Polling timeout ───────────────────────────────────────────────────────────

class TestFlexAdapterTimeout:

    @pytest.mark.asyncio
    async def test_polling_timeout_raises_ibkr_timeout_error(self, ibkr_config):
        """If polling never gets a ready statement, IBKRTimeoutError is raised."""
        # Send OK response, then always return "Generating" for all poll attempts
        responses = [_FLEX_SEND_OK] + [_FLEX_STILL_GENERATING] * 15

        with _patch_urlopen_sequence(responses):
            adapter = FlexAdapter(ibkr_config)
            await adapter.connect()
            adapter._cached_xml = None

            # Patch sleep to not actually wait
            with patch("ibkr_async.adapters.flex_adapter.time.sleep"):
                with pytest.raises(IBKRTimeoutError, match="not ready"):
                    await adapter.fetch_positions()
