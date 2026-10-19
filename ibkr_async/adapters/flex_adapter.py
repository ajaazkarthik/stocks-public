"""
FlexAdapter — IBKR Flex Query Web Service adapter.

Fetches IBKR portfolio data via the IBKR Flex Query HTTPS REST service.
Implements IBKRDataPort (read-only). Does NOT implement IBKROrderPort
because Flex Query is a reporting service, not a trading API.

Key characteristics (compared to TWSAdapter)
---------------------------------------------
✓ No TWS or IB Gateway required (pure HTTPS)
✓ Works when TWS is offline or market is closed
✓ Suitable for EOD batch processing and CGT calculations
✗ Latency: 30-300 seconds (IBKR generates statement server-side)
✗ Data freshness: T+1 (yesterday's closing prices, not live)
✗ Cannot stream real-time quotes (supports_live_quotes = False)
✗ May miss positions opened after last EOD statement generation

Flex Query flow
---------------
1. POST to SendRequest  → returns a ReferenceCode
2. Poll GetStatement with ReferenceCode → wait for XML to be ready
3. Parse FlexStatement XML → extract OpenPosition, EquitySummaryInBase

This adapter re-implements the HTTPS logic independently without importing
from cgt/ibkr-cgt/ibkr_cgt_analysis.py, keeping the module fully isolated.
"""

from __future__ import annotations

import asyncio
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from ..config import IBKRConfig
from ..exceptions import IBKRConnectionError, IBKRDataError, IBKRTimeoutError
from ..logger import get_logger
from ..models import (
    AccountSummary,
    AssetClass,
    ConnectionStatus,
    HealthStatus,
    Position,
    Quote,
)
from ..ports import IBKRDataPort

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

log = get_logger(__name__)

# ── Flex Query API endpoints ──────────────────────────────────────────────────
_FLEX_SEND_URL     = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.SendRequest"
)
_FLEX_DOWNLOAD_URL = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.GetStatement"
)

# Polling parameters
_MAX_POLL_ATTEMPTS    = 12
_POLL_INTERVAL_SECS   = 5.0


def _map_flex_sectype(sec_type: str) -> AssetClass:
    """Map IBKR Flex assetCategory to domain AssetClass."""
    _MAP = {
        "STK":  AssetClass.STOCK,
        "OPT":  AssetClass.OPTION,
        "FUT":  AssetClass.FUTURE,
        "CASH": AssetClass.FOREX,
        "IND":  AssetClass.INDEX,
        "BOND": AssetClass.BOND,
        "FUND": AssetClass.FUND,
    }
    return _MAP.get(sec_type.upper(), AssetClass.UNKNOWN)


class FlexAdapter(IBKRDataPort):
    """
    IBKR Flex Query adapter — batch portfolio data retrieval via HTTPS.

    Does NOT require TWS or IB Gateway. Uses IBKR's Flex Query Web Service
    to fetch portfolio snapshots asynchronously (backing HTTP calls are
    run in threads to avoid blocking the event loop).

    Usage::

        config = IBKRConfig.from_env()
        async with FlexAdapter(config) as adapter:
            positions = await adapter.fetch_positions()
            account   = await adapter.fetch_account_summary()
    """

    def __init__(self, config: Optional[IBKRConfig] = None) -> None:
        self._config      = config or IBKRConfig.from_env()
        self._connected   = False
        self._cached_xml: Optional[str]      = None
        self._fetched_at: Optional[datetime] = None

        log.info(
            "flex_adapter_initialized",
            flex_query_id=self._config.flex_query_id,
            flex_token=self._config.to_dict()["flex_token"],
        )

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "FlexAdapter":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    # ── IBKRDataPort: Connection lifecycle ────────────────────────────────────

    async def connect(self) -> None:
        """
        For Flex Query, 'connect' means confirming IBKR's server is reachable.
        The actual statement fetch is done in fetch_positions() / fetch_account_summary().
        """
        log.info("flex_connect_start")
        # Flex is stateless HTTP — mark as connected immediately.
        # Actual error will surface when fetch_positions is called.
        self._connected = True
        log.info("flex_connected")

    async def disconnect(self) -> None:
        """Release cached XML and mark disconnected."""
        self._connected  = False
        self._cached_xml = None
        self._fetched_at = None
        log.info("flex_disconnected")

    # ── IBKRDataPort: Positions ───────────────────────────────────────────────

    async def fetch_positions(self) -> list[Position]:
        """
        Fetch open positions via Flex Query XML.

        Sends a Flex Query request, polls until the XML statement is ready,
        then parses all OpenPosition elements.

        Note: positions reflect the last EOD statement (T+1).

        Returns:
            List of Position objects.

        Raises:
            IBKRConnectionError: If IBKR Flex servers cannot be reached.
            IBKRDataError:       If XML cannot be parsed.
            IBKRTimeoutError:    If statement exceeds timeout.
        """
        log.info("flex_fetch_positions_start")
        t0 = time.perf_counter()

        xml_str = await self._get_flex_xml()

        positions: list[Position] = []
        try:
            root = ET.fromstring(xml_str)
            for el in root.iter("OpenPosition"):
                # Filter to SUMMARY rows only — avoids double-counting when
                # the Flex query includes per-lot detail alongside summaries.
                if el.get("levelOfDetail", "SUMMARY") != "SUMMARY":
                    continue
                try:
                    positions.append(self._parse_open_position(el))
                except Exception as exc:
                    log.warning(
                        "flex_position_parse_warning",
                        symbol=el.get("symbol", "?"),
                        error=str(exc),
                    )
        except ET.ParseError as exc:
            raise IBKRDataError(f"Failed to parse Flex XML: {exc}") from exc

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "flex_positions_fetched",
            count=len(positions),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return positions

    def _parse_open_position(self, el: ET.Element) -> Position:
        """Parse a single <OpenPosition> XML element into a Position."""
        return Position(
            symbol         = el.get("symbol", ""),
            exchange       = el.get("exchange", ""),
            currency       = el.get("currency", "USD"),
            asset_class    = _map_flex_sectype(el.get("assetCategory", "STK")),
            quantity       = float(el.get("position", 0)),
            avg_cost       = float(el.get("costBasisPrice", 0)),
            market_price   = float(el.get("markPrice", 0)),
            market_value   = float(el.get("positionValue", 0)),
            unrealised_pnl = float(el.get("fifoPnlUnrealized", 0)),
            realised_pnl   = float(el.get("realizedPnl", 0)),
            timestamp      = datetime.now(timezone.utc),
            account_id     = el.get("accountId", ""),
            broker         = "IBKR",
        )

    # ── IBKRDataPort: Account summary ─────────────────────────────────────────

    async def fetch_account_summary(self) -> AccountSummary:
        """
        Fetch account financial summary from Flex XML.

        Parsing strategy (in priority order):
        1. ``EquitySummaryInBase`` — present when the Flex Query is configured
           to include the "Equity Summary in Base Currency" section.
        2. ``CashReportCurrency[levelOfDetail=BaseCurrency]`` — ``endingCash``
           for cash balance; unrealised P&L summed from ``OpenPosition``
           ``fifoPnlUnrealized`` fields.  Net liquidation = cash + positionValues.
        3. Zero-fill fallback if neither section is present.

        Note: Values reflect last EOD snapshot (T+1), not intraday balance.
        """
        log.info("flex_fetch_account_start")
        xml_str = await self._get_flex_xml()

        try:
            root = ET.fromstring(xml_str)

            # ── Strategy 1: EquitySummaryInBase (preferred when present) ──────
            for el in root.iter("EquitySummaryInBase"):
                try:
                    # Some queries put a summary row; some put one per currency.
                    # Use the first row that has a non-zero total.
                    total = float(el.get("total", 0) or 0)
                    if total != 0 or el.get("cash") is not None:
                        currency = el.get("currency", "USD") or "USD"
                        # Top-level EquitySummaryInBase has no currency attr — default USD
                        if currency in ("", "BASE"):
                            currency = "USD"
                        acct_id = el.get("accountId", "")
                        summary = AccountSummary(
                            account_id      = acct_id,
                            net_liquidation = float(el.get("total", 0) or 0),
                            total_cash      = float(el.get("cash", 0) or 0),
                            unrealised_pnl  = float(el.get("unrealizedPnl", 0) or 0),
                            realised_pnl    = float(el.get("realizedPnl", 0) or 0),
                            currency        = currency,
                            timestamp       = datetime.now(timezone.utc),
                        )
                        log.info(
                            "flex_account_from_equity_summary",
                            net_liquidation=summary.net_liquidation,
                            currency=summary.currency,
                        )
                        return summary
                except Exception:
                    continue

            # ── Strategy 2: CashReportCurrency + OpenPositions ────────────────
            account_id = ""
            currency   = "USD"
            end_cash   = 0.0
            unrealised = 0.0
            net_liq    = 0.0

            # Account currency from AccountInformation
            for ai in root.iter("AccountInformation"):
                account_id = ai.get("accountId", "")
                currency   = ai.get("currency", "USD") or "USD"
                break

            # Cash from CashReport BASE_SUMMARY row
            for cr in root.iter("CashReportCurrency"):
                if cr.get("levelOfDetail") == "BaseCurrency":
                    end_cash = float(cr.get("endingCash", 0) or 0)
                    if not account_id:
                        account_id = cr.get("accountId", "")
                    break

            # Unrealised PnL + total position value from OpenPositions
            pos_value = 0.0
            for op in root.iter("OpenPosition"):
                if op.get("levelOfDetail", "SUMMARY") == "SUMMARY":
                    unrealised += float(op.get("fifoPnlUnrealized", 0) or 0)
                    pos_value  += float(op.get("positionValue", 0) or 0)

            net_liq = end_cash + pos_value

            if end_cash != 0.0 or pos_value != 0.0:
                summary = AccountSummary(
                    account_id      = account_id,
                    net_liquidation = round(net_liq, 4),
                    total_cash      = round(end_cash, 4),
                    unrealised_pnl  = round(unrealised, 4),
                    realised_pnl    = 0.0,
                    currency        = currency,
                    timestamp       = datetime.now(timezone.utc),
                )
                log.info(
                    "flex_account_from_cash_report",
                    net_liquidation=summary.net_liquidation,
                    total_cash=summary.total_cash,
                    unrealised_pnl=summary.unrealised_pnl,
                    currency=summary.currency,
                )
                return summary

            # ── Strategy 3: Zero-fill (Flex query not configured with these sections)
            log.warning("flex_account_summary_section_not_found")
            return AccountSummary(
                account_id="", net_liquidation=0.0, total_cash=0.0,
                unrealised_pnl=0.0, realised_pnl=0.0,
                timestamp=datetime.now(timezone.utc),
            )
        except ET.ParseError as exc:
            raise IBKRDataError(f"Failed to parse Flex XML for account: {exc}") from exc

    # ── IBKRDataPort: Health ──────────────────────────────────────────────────

    async def health_check(self) -> HealthStatus:
        """Check if IBKR Flex servers are reachable. Never raises."""
        t0 = time.perf_counter()
        error_msg = ""
        reachable = False

        try:
            with urllib.request.urlopen(
                "https://gdcdyn.interactivebrokers.com",
                timeout=10,
            ) as resp:
                reachable = resp.status < 500
        except Exception as exc:
            error_msg = str(exc)

        latency_ms = (time.perf_counter() - t0) * 1000

        return HealthStatus(
            is_connected  = reachable,
            status        = ConnectionStatus.CONNECTED if reachable else ConnectionStatus.DISCONNECTED,
            host          = "gdcdyn.interactivebrokers.com",
            port          = 443,
            client_id     = 0,
            latency_ms    = round(latency_ms, 2),
            error_message = error_msg,
            checked_at    = datetime.now(timezone.utc),
        )

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def supports_live_quotes(self) -> bool:
        """Flex Query cannot stream real-time quotes."""
        return False

    # ── Private: Flex XML fetch pipeline ─────────────────────────────────────

    async def _get_flex_xml(self) -> str:
        """
        Return the Flex XML statement, fetching from IBKR if not cached.

        Uses asyncio.to_thread() to run the blocking HTTP calls in a
        thread pool, keeping the event loop free.
        """
        if self._cached_xml:
            log.debug("flex_using_cached_xml", fetched_at=self._fetched_at)
            return self._cached_xml

        log.info("flex_requesting_statement", query_id=self._config.flex_query_id)
        t0 = time.perf_counter()

        try:
            ref_code, dl_base = await asyncio.to_thread(self._send_request)
        except Exception as exc:
            raise IBKRConnectionError(f"Flex SendRequest failed: {exc}") from exc

        log.info("flex_polling_statement", ref_code=ref_code)

        xml_str = await asyncio.to_thread(self._poll_statement, ref_code, dl_base)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "flex_xml_ready",
            elapsed_ms=round(elapsed_ms, 1),
            xml_size_bytes=len(xml_str),
        )

        self._cached_xml = xml_str
        self._fetched_at = datetime.now(timezone.utc)
        return xml_str

    def _send_request(self) -> tuple[str, str]:
        """
        POST the Flex Query SendRequest and return (reference_code, download_url).
        Runs synchronously in a thread.
        """
        params = urllib.parse.urlencode({
            "v": "3",
            "t": self._config.flex_token,
            "q": self._config.flex_query_id,
            "type": "AF",
        })
        url = f"{_FLEX_SEND_URL}?{params}"

        try:
            with urllib.request.urlopen(url, timeout=self._config.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise IBKRConnectionError(
                f"Cannot reach IBKR Flex endpoint: {exc}"
            ) from exc

        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise IBKRDataError(f"Flex SendRequest returned invalid XML: {exc}") from exc

        status   = (root.findtext("Status")        or "").strip().lower()
        ref_code = (root.findtext("ReferenceCode") or "").strip()
        dl_base  = (root.findtext("Url")           or _FLEX_DOWNLOAD_URL).strip()

        _VALID_STATUSES = {"success", "processing", "generating", "queued"}
        if not ref_code or status not in _VALID_STATUSES:
            raise IBKRDataError(
                f"Flex SendRequest bad response status={status!r}. "
                f"Full response: {ET.tostring(root, encoding='unicode')}"
            )

        log.debug("flex_send_request_ok", ref_code=ref_code, status=status)
        return ref_code, dl_base

    def _poll_statement(self, ref_code: str, dl_base: str) -> str:
        """
        Poll IBKR's GetStatement endpoint until the XML is ready.
        Runs synchronously in a thread; sleeps between attempts.

        Raises IBKRTimeoutError if not ready after _MAX_POLL_ATTEMPTS.
        """
        for attempt in range(1, _MAX_POLL_ATTEMPTS + 1):
            params = urllib.parse.urlencode({
                "v": "3",
                "t": self._config.flex_token,
                "q": ref_code,
                "type": "AF",
            })
            url = f"{dl_base}?{params}"

            try:
                with urllib.request.urlopen(url, timeout=self._config.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except Exception as exc:
                log.warning("flex_poll_http_error", attempt=attempt, error=str(exc))
                time.sleep(_POLL_INTERVAL_SECS)
                continue

            # Statement ready: contains <FlexStatement or <FlexStatements
            if "<FlexStatement " in body or "<FlexStatements " in body:
                log.info("flex_poll_statement_ready", attempt=attempt)
                return body

            # Still generating — log and wait
            try:
                root  = ET.fromstring(body)
                st    = (root.findtext("Status") or "").strip()
                msg   = (root.findtext("ErrorMessage") or "").strip()
                log.debug("flex_poll_waiting", attempt=attempt, status=st, message=msg)
            except Exception:
                pass

            time.sleep(_POLL_INTERVAL_SECS)

        raise IBKRTimeoutError(
            f"Flex Query statement not ready after {_MAX_POLL_ATTEMPTS} attempts "
            f"({_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_SECS:.0f}s). "
            f"IBKR may be generating a large statement; try again later."
        )
