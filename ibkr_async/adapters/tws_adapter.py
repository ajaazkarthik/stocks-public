"""
TWSAdapter — Live IBKR data and orders via ib_async TWS socket.

This adapter connects to a running TWS or IB Gateway instance via the
ib_async library and provides real-time portfolio data.

Key advantages over FlexAdapter
---------------------------------
- Latency    : ~100-500ms vs 30-300 seconds (300x faster to first data)
- Freshness  : Real-time live positions vs T+1 (yesterday's data)
- Completeness: Includes intraday fills immediately; Flex may miss them
- Live quotes : Can stream bid/ask/last prices; Flex has no quote data
- Order ops  : Can place/cancel orders; Flex is read-only

Requirements
------------
- pip install ib_async
- TWS or IB Gateway running on host:port with API enabled
  Configure → API → Settings → Enable ActiveX and Socket Clients
- Unique client_id per simultaneous connection

IBKR Port conventions:
  7496 — TWS live
  7497 — TWS paper
  4001 — IB Gateway live
  4002 — IB Gateway paper
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from ..config import IBKRConfig
from ..exceptions import (
    IBKRConnectionError,
    IBKRDataError,
    IBKRNotAvailableError,
    IBKRTimeoutError,
)
from ..logger import get_logger
from ..models import (
    AccountSummary,
    AssetClass,
    ConnectionStatus,
    HealthStatus,
    Position,
    Quote,
)
from ..ports import IBKRDataPort, IBKROrderPort

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

log = get_logger(__name__)

# ── Lazy ib_async import ──────────────────────────────────────────────────────
# ib_async is an optional dependency. We import it lazily so the module can be
# imported and tested without ib_async installed.

_IB_CLASS: Optional[type] = None


def _get_ib_class() -> type:
    """
    Return the ib_async.IB class, importing ib_async on first call.
    Raises IBKRNotAvailableError if ib_async is not installed.
    """
    global _IB_CLASS
    if _IB_CLASS is not None:
        return _IB_CLASS
    try:
        from ib_async import IB  # type: ignore
        _IB_CLASS = IB
        return _IB_CLASS
    except ImportError as exc:
        raise IBKRNotAvailableError(
            "ib_async is not installed. Install with: pip install ib_async\n"
            f"Original error: {exc}"
        ) from exc


def _map_sectype(sec_type: str) -> AssetClass:
    """Map ib_async security type string to our domain AssetClass."""
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


# ── Adapter implementation ────────────────────────────────────────────────────

class TWSAdapter(IBKRDataPort, IBKROrderPort):
    """
    Live IBKR adapter using ib_async for real-time positions and orders.

    Implements both IBKRDataPort (read) and IBKROrderPort (write).

    Usage — async context manager (recommended)::

        config = IBKRConfig.from_env()
        async with TWSAdapter(config) as adapter:
            positions = await adapter.fetch_positions()
            account   = await adapter.fetch_account_summary()
            health    = await adapter.health_check()

    Usage — explicit lifecycle::

        adapter = TWSAdapter(config)
        await adapter.connect()
        try:
            positions = await adapter.fetch_positions()
        finally:
            await adapter.disconnect()
    """

    def __init__(self, config: Optional[IBKRConfig] = None) -> None:
        self._config   = config or IBKRConfig.from_env()
        self._ib: Optional[object] = None
        self._status   = ConnectionStatus.DISCONNECTED
        self._account_id: str = ""
        self._connected_at: Optional[datetime] = None

        log.info(
            "tws_adapter_initialized",
            host=self._config.host,
            port=self._config.port,
            client_id=self._config.client_id,
            paper=self._config.paper_trading,
            readonly=self._config.readonly,
        )

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "TWSAdapter":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    # ── IBKRDataPort: Connection lifecycle ────────────────────────────────────

    async def connect(self) -> None:
        """
        Establish async connection to TWS/IB Gateway.

        Raises:
            IBKRNotAvailableError: ib_async not installed.
            IBKRConnectionError:  Connection refused.
            IBKRTimeoutError:     Connection timed out.
        """
        IB = _get_ib_class()
        self._status = ConnectionStatus.CONNECTING
        self._ib = IB()

        log.info(
            "tws_connecting",
            host=self._config.host,
            port=self._config.port,
            client_id=self._config.client_id,
        )

        try:
            await asyncio.wait_for(
                self._ib.connectAsync(
                    self._config.host,
                    self._config.port,
                    clientId=self._config.client_id,
                    readonly=self._config.readonly,
                ),
                timeout=float(self._config.timeout),
            )
        except asyncio.TimeoutError as exc:
            self._status = ConnectionStatus.FAILED
            self._ib = None
            log.error(
                "tws_connection_timeout",
                host=self._config.host,
                port=self._config.port,
                timeout_s=self._config.timeout,
            )
            raise IBKRTimeoutError(
                f"Connection to {self._config.host}:{self._config.port} "
                f"timed out after {self._config.timeout}s. "
                f"Is TWS running?"
            ) from exc
        except ConnectionRefusedError as exc:
            self._status = ConnectionStatus.FAILED
            self._ib = None
            log.error(
                "tws_connection_refused",
                host=self._config.host,
                port=self._config.port,
            )
            raise IBKRConnectionError(
                f"TWS/IB Gateway refused connection at "
                f"{self._config.host}:{self._config.port}. "
                f"Ensure TWS is running and API is enabled."
            ) from exc
        except Exception as exc:
            self._status = ConnectionStatus.FAILED
            self._ib = None
            log.error(
                "tws_connection_error",
                host=self._config.host,
                port=self._config.port,
                error=str(exc),
            )
            raise IBKRConnectionError(
                f"Failed to connect to TWS: {exc}"
            ) from exc

        # Successfully connected
        self._status = ConnectionStatus.CONNECTED
        self._connected_at = datetime.now(timezone.utc)

        # Identify primary account
        accounts = self._ib.managedAccounts()
        self._account_id = accounts[0] if accounts else ""

        # Configure market data type to avoid subscription errors
        self._ib.reqMarketDataType(self._config.market_data_type)

        log.info(
            "tws_connected",
            account_id=self._account_id,
            market_data_type=self._config.market_data_type,
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect from TWS."""
        if self._ib is not None:
            try:
                self._ib.disconnect()
                log.info(
                    "tws_disconnected",
                    account_id=self._account_id,
                    uptime_s=self._uptime_seconds(),
                )
            except Exception as exc:
                log.warning("tws_disconnect_error", error=str(exc))
            finally:
                self._ib = None
        self._status = ConnectionStatus.DISCONNECTED

    # ── IBKRDataPort: Positions ───────────────────────────────────────────────

    async def fetch_positions(self) -> list[Position]:
        """
        Fetch all open positions from TWS in real time.

        Uses ib_async portfolio() which returns PortfolioItem objects with
        live market prices and P&L (unlike Flex Query which is T+1).

        Returns:
            List of Position objects. Empty if no open positions.

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If portfolio data cannot be parsed.
        """
        self._require_connected("fetch_positions")
        log.debug("tws_fetching_positions", account_id=self._account_id)
        t0 = time.perf_counter()

        try:
            portfolio_items = self._ib.portfolio(self._account_id)
            positions: list[Position] = []

            for item in portfolio_items:
                try:
                    positions.append(self._item_to_position(item))
                except Exception as exc:
                    symbol = getattr(getattr(item, "contract", None), "symbol", "?")
                    log.warning(
                        "tws_position_parse_warning",
                        symbol=symbol,
                        error=str(exc),
                    )

        except Exception as exc:
            log.error("tws_fetch_positions_error", error=str(exc))
            raise IBKRDataError(f"fetch_positions failed: {exc}") from exc

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "tws_positions_fetched",
            count=len(positions),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return positions

    def _item_to_position(self, item: object) -> Position:
        """Convert ib_async PortfolioItem to our domain Position model."""
        c = item.contract
        return Position(
            symbol         = c.symbol,
            exchange       = c.exchange or getattr(c, "primaryExch", "") or "",
            currency       = c.currency,
            asset_class    = _map_sectype(c.secType),
            quantity       = float(item.position),
            avg_cost       = float(item.averageCost or 0.0),
            market_price   = float(item.marketPrice or 0.0),
            market_value   = float(item.marketValue or 0.0),
            unrealised_pnl = float(item.unrealizedPNL or 0.0),
            realised_pnl   = float(item.realizedPNL or 0.0),
            timestamp      = datetime.now(timezone.utc),
            account_id     = self._account_id,
            broker         = "IBKR",
        )

    # ── IBKRDataPort: Account summary ─────────────────────────────────────────

    async def fetch_account_summary(self) -> AccountSummary:
        """
        Fetch account-level financial summary (near real-time).

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If account data cannot be parsed.
        """
        self._require_connected("fetch_account_summary")
        log.debug("tws_fetching_account_summary", account_id=self._account_id)
        t0 = time.perf_counter()

        try:
            # Use accountValues() — reads from the TWS subscription cache
            # (populated automatically after connect, same as portfolio())
            # without triggering a new network request or util.run() call.
            # Falls back to accountSummary() in environments where
            # accountValues() is not available.
            if hasattr(self._ib, "accountValues"):
                items = self._ib.accountValues(self._account_id)
            else:
                items = self._ib.accountSummary(self._account_id)

            tag_map: dict[str, float] = {}
            currency = "USD"
            for it in items:
                try:
                    tag_map[it.tag] = float(it.value)
                except (ValueError, AttributeError):
                    if getattr(it, "tag", "") == "Currency":
                        currency = it.value

            elapsed_ms = (time.perf_counter() - t0) * 1000
            result = AccountSummary(
                account_id       = self._account_id,
                net_liquidation  = tag_map.get("NetLiquidation", 0.0),
                total_cash       = tag_map.get("TotalCashValue", 0.0),
                unrealised_pnl   = tag_map.get("UnrealizedPnL", 0.0),
                realised_pnl     = tag_map.get("RealizedPnL", 0.0),
                equity_with_loan = tag_map.get("EquityWithLoanValue", 0.0),
                available_funds  = tag_map.get("AvailableFunds", 0.0),
                excess_liquidity = tag_map.get("ExcessLiquidity", 0.0),
                currency         = currency,
                timestamp        = datetime.now(timezone.utc),
            )

            log.info(
                "tws_account_summary_fetched",
                account_id=self._account_id,
                net_liquidation=result.net_liquidation,
                elapsed_ms=round(elapsed_ms, 1),
            )
            return result

        except Exception as exc:
            log.error("tws_fetch_account_error", error=str(exc))
            raise IBKRDataError(f"fetch_account_summary failed: {exc}") from exc

    # ── IBKRDataPort: Live quotes ─────────────────────────────────────────────

    async def fetch_quotes(
        self,
        symbols: list[str],
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> list[Quote]:
        """
        Request snapshot market quotes for a list of stock symbols.

        Uses reqMktData with snapshot=True. Waits 2 seconds for data to
        arrive from IBKR. Symbols that return no data are silently skipped.

        Requires market data subscription for real-time; falls back to
        delayed data per market_data_type config (default: delayed).

        Args:
            symbols:  List of ticker symbols, e.g. ["AAPL", "MSFT"]
            exchange: Routing exchange (default: "SMART" for auto-routing)
            currency: Currency denomination (default: "USD")

        Returns:
            List of Quote objects for symbols that returned data.

        Raises:
            IBKRConnectionError:    If not connected.
            IBKRNotAvailableError:  If ib_async is not installed.
        """
        self._require_connected("fetch_quotes")

        try:
            from ib_async import Stock  # type: ignore
        except ImportError as exc:
            raise IBKRNotAvailableError("ib_async not installed") from exc

        log.debug("tws_fetching_quotes", symbols=symbols, count=len(symbols))
        t0 = time.perf_counter()

        tickers = []
        for sym in symbols:
            contract = Stock(sym, exchange, currency)
            ticker = self._ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
            tickers.append((sym, ticker))

        # Allow time for snapshot data to arrive
        await asyncio.sleep(2.0)

        quotes: list[Quote] = []
        for sym, ticker in tickers:
            bid  = float(ticker.bid  or 0.0)
            ask  = float(ticker.ask  or 0.0)
            last = float(ticker.last or 0.0)
            if bid or ask or last:
                quotes.append(Quote(
                    symbol    = sym,
                    exchange  = exchange,
                    currency  = currency,
                    bid       = bid,
                    ask       = ask,
                    last      = last,
                    bid_size  = float(ticker.bidSize  or 0.0),
                    ask_size  = float(ticker.askSize  or 0.0),
                    volume    = float(ticker.volume   or 0.0),
                    timestamp = datetime.now(timezone.utc),
                ))

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "tws_quotes_fetched",
            requested=len(symbols),
            received=len(quotes),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return quotes

    # ── IBKRDataPort: Health ──────────────────────────────────────────────────

    async def health_check(self) -> HealthStatus:
        """Return connectivity health. Never raises."""
        t0 = time.perf_counter()
        error_msg = ""

        try:
            is_conn = bool(self._ib is not None and self._ib.isConnected())
        except Exception as exc:
            is_conn = False
            error_msg = str(exc)

        latency_ms = (time.perf_counter() - t0) * 1000

        return HealthStatus(
            is_connected  = is_conn,
            status        = ConnectionStatus.CONNECTED if is_conn else ConnectionStatus.DISCONNECTED,
            host          = self._config.host,
            port          = self._config.port,
            client_id     = self._config.client_id,
            latency_ms    = round(latency_ms, 2),
            account_id    = self._account_id,
            error_message = error_msg,
            checked_at    = datetime.now(timezone.utc),
        )

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self._ib is not None and self._ib.isConnected())
        except Exception:
            return False

    @property
    def supports_live_quotes(self) -> bool:
        return True

    # ── IBKROrderPort ─────────────────────────────────────────────────────────

    async def fetch_open_orders(self) -> list[dict]:
        """
        Return all currently open (unfilled) orders.

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If order data cannot be retrieved.
        """
        self._require_connected("fetch_open_orders")

        try:
            trades = self._ib.openTrades()
            return [
                {
                    "order_id":   t.order.orderId,
                    "symbol":     t.contract.symbol,
                    "action":     t.order.action,
                    "quantity":   float(t.order.totalQuantity),
                    "order_type": t.order.orderType,
                    "lmt_price":  getattr(t.order, "lmtPrice", None),
                    "status":     t.orderStatus.status,
                }
                for t in trades
            ]
        except Exception as exc:
            log.error("tws_fetch_orders_error", error=str(exc))
            raise IBKRDataError(f"fetch_open_orders failed: {exc}") from exc

    async def cancel_order(self, order_id: int) -> bool:
        """
        Cancel an open order. Returns False in readonly mode.

        Raises:
            IBKRConnectionError: If not connected.
        """
        self._require_connected("cancel_order")

        if self._config.readonly:
            log.warning("tws_cancel_blocked_readonly", order_id=order_id)
            return False

        try:
            trades = self._ib.openTrades()
            target = next(
                (t for t in trades if t.order.orderId == order_id), None
            )
            if target is None:
                log.warning("tws_cancel_order_not_found", order_id=order_id)
                return False

            self._ib.cancelOrder(target.order)
            log.info("tws_cancel_order_requested", order_id=order_id)
            return True
        except Exception as exc:
            log.error("tws_cancel_order_error", order_id=order_id, error=str(exc))
            return False

    # ── Private helpers ───────────────────────────────────────────────────────

    def _require_connected(self, operation: str = "") -> None:
        """Raise IBKRConnectionError if not connected."""
        if not self.is_connected:
            raise IBKRConnectionError(
                f"TWSAdapter is not connected (operation={operation!r}). "
                f"Call `await adapter.connect()` first."
            )

    def _uptime_seconds(self) -> Optional[float]:
        if self._connected_at is None:
            return None
        return (datetime.now(timezone.utc) - self._connected_at).total_seconds()
