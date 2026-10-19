#!/usr/bin/env python3
"""
IBKR Client Portal REST API Adapter
=====================================
Lightweight adapter for IBKR Client Portal Web API (port 5000).
NO TWS or IB Gateway required — uses the Client Portal REST endpoint.

This serves as a *tertiary* data source for IBKR data:
  Priority: TWS (ibkr_async TWSAdapter) > Flex Query (FlexAdapter) > Client Portal (this)

The Client Portal REST API is useful when:
  - TWS/IB Gateway is not running
  - After-hours data is needed
  - Simpler setup than TWS (browser-based auth, no Java)

Requirements:
  - IBKR Client Portal Gateway running on localhost:5000
    OR ibind library (pip install ibind) for headless OAuth 1.0a
  - Valid IBKR credentials

NOTE: The ibkr_async module (TWSAdapter/FlexAdapter) is the PRIMARY recommended
      path. This module provides an alternative when TWS is unavailable.

Usage (standalone):
    python ibkr_client_portal.py --positions
    python ibkr_client_portal.py --accounts
    python ibkr_client_portal.py --market-data AAPL MSFT

Usage (as library):
    from ibkr_client_portal import IBKRClientPortal
    cp = IBKRClientPortal()
    positions = cp.get_positions()
    accounts = cp.get_portfolio_accounts()
"""

import sys
import os
import json
import argparse
import ssl
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ─── Windows UTF-8 fix ────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from path_config import ensure_paths, ROOT
ensure_paths(ROOT / "tradingview")

try:
    from log_config import get_logger as _get_logger
    _log = _get_logger(__name__)
except Exception:
    import logging as _logging
    _log = _logging.getLogger(__name__)

# ─── ibind availability ───────────────────────────────────────────────────────
try:
    from ibind import IbkrClient  # type: ignore
    _IBIND_AVAILABLE = True
except ImportError:
    _IBIND_AVAILABLE = False

# ─── Known broker constants ──────────────────────────────────────────────────
_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

# ─── Config ──────────────────────────────────────────────────────────────────
CP_BASE_URL = os.environ.get("IBKR_CP_URL", "https://localhost:5000/v1/api")
CP_TIMEOUT  = int(os.environ.get("IBKR_CP_TIMEOUT", "15"))


class IBKRClientPortal:
    """
    REST client for IBKR Client Portal Web API.

    Two modes of operation:
    1. Direct REST (urllib) — when Client Portal Gateway is running on localhost:5000
    2. ibind library — when ibind is installed (supports OAuth 1.0a headless auth)

    Falls back gracefully if neither is available.
    """

    def __init__(self, base_url: str = CP_BASE_URL, timeout: int = CP_TIMEOUT,
                 use_ibind: bool = True):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._ibind_client: Optional[object] = None
        self._available = False

        # Prefer ibind if available and enabled
        if use_ibind and _IBIND_AVAILABLE:
            try:
                self._ibind_client = IbkrClient()
                self._available = True
                _log.info("ibkr_cp_init", mode="ibind")
            except Exception as exc:
                _log.warning("ibkr_cp_ibind_failed", error=str(exc))

        # Test direct REST as fallback
        if not self._available:
            if self._health_check_direct():
                self._available = True
                _log.info("ibkr_cp_init", mode="direct_rest")
            else:
                _log.info("ibkr_cp_unavailable",
                          msg="Client Portal not reachable — will return empty data")

    @property
    def is_available(self) -> bool:
        return self._available

    # ── Direct REST helpers ────────────────────────────────────────────────────

    def _make_request(self, path: str, method: str = "GET",
                      body: Optional[dict] = None) -> Optional[dict]:
        """Make a REST request to Client Portal Gateway. Returns parsed JSON or None."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        data = None
        if body:
            data = json.dumps(body).encode("utf-8")

        # Client Portal uses self-signed certs
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            _log.debug("ibkr_cp_request_failed", url=url, error=str(exc))
            return None
        except json.JSONDecodeError:
            return None

    def _health_check_direct(self) -> bool:
        """Check if Client Portal Gateway is reachable."""
        result = self._make_request("/iserver/auth/status")
        return result is not None

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """
        Fetch all open positions across all accounts.

        Returns list of dicts compatible with global_portfolio_dashboard.py:
        [{"broker": "IBKR", "symbol": "AAPL", "qty": 100, "avg_cost": 150.0,
          "market_price": 155.0, "market_value": 15500.0, "unrealized_pnl": 500.0,
          "currency": "USD"}]
        """
        if not self._available:
            return []

        positions = []

        if self._ibind_client:
            try:
                resp = self._ibind_client.portfolio_positions()  # type: ignore
                raw = resp.data if hasattr(resp, "data") else resp
                if isinstance(raw, list):
                    for pos in raw:
                        positions.append(self._normalize_position(pos))
                return positions
            except Exception as exc:
                _log.warning("ibkr_cp_positions_ibind_error", error=str(exc))
                return []

        # Direct REST path
        # First get account IDs
        accounts = self._make_request("/portfolio/accounts")
        if not accounts or not isinstance(accounts, list):
            return []

        for acct in accounts:
            acct_id = acct.get("accountId", "")
            if not acct_id:
                continue
            page = 0
            while True:
                acct_positions = self._make_request(
                    f"/portfolio/{acct_id}/positions/{page}"
                )
                if not acct_positions or not isinstance(acct_positions, list):
                    break
                if len(acct_positions) == 0:
                    break
                for pos in acct_positions:
                    positions.append(self._normalize_position(pos))
                page += 1
                if len(acct_positions) < 30:
                    break

        return positions

    def get_portfolio_accounts(self) -> list[dict]:
        """Fetch portfolio account summary data."""
        if not self._available:
            return []

        if self._ibind_client:
            try:
                resp = self._ibind_client.portfolio_accounts()  # type: ignore
                raw = resp.data if hasattr(resp, "data") else resp
                return raw if isinstance(raw, list) else []
            except Exception as exc:
                _log.warning("ibkr_cp_accounts_ibind_error", error=str(exc))
                return []

        result = self._make_request("/portfolio/accounts")
        return result if isinstance(result, list) else []

    def get_live_orders(self) -> list[dict]:
        """Fetch current live orders."""
        if not self._available:
            return []

        if self._ibind_client:
            try:
                resp = self._ibind_client.live_orders()  # type: ignore
                raw = resp.data if hasattr(resp, "data") else resp
                return raw.get("orders", []) if isinstance(raw, dict) else []
            except Exception as exc:
                _log.warning("ibkr_cp_orders_ibind_error", error=str(exc))
                return []

        result = self._make_request("/iserver/account/orders")
        if isinstance(result, dict):
            return result.get("orders", [])
        return result if isinstance(result, list) else []

    def get_market_data(self, conids: list[int],
                        fields: Optional[list[str]] = None) -> list[dict]:
        """
        Fetch snapshot market data for given contract IDs.

        Default fields: last price, bid, ask, volume, change %.
        """
        if not self._available or not conids:
            return []

        default_fields = ["31", "84", "85", "86", "87", "7283"]
        field_str = ",".join(fields or default_fields)
        conid_str = ",".join(str(c) for c in conids)

        if self._ibind_client:
            try:
                resp = self._ibind_client.marketdata_snapshot(  # type: ignore
                    conids=conids, fields=default_fields
                )
                raw = resp.data if hasattr(resp, "data") else resp
                return raw if isinstance(raw, list) else []
            except Exception as exc:
                _log.warning("ibkr_cp_mktdata_ibind_error", error=str(exc))
                return []

        result = self._make_request(
            f"/iserver/marketdata/snapshot?conids={conid_str}&fields={field_str}"
        )
        return result if isinstance(result, list) else []

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_position(raw: dict) -> dict:
        """Normalize a Client Portal position dict to our standard format."""
        return {
            "broker":         "IBKR",
            "symbol":         raw.get("contractDesc", raw.get("ticker", "")),
            "qty":            raw.get("position", raw.get("pos", 0)),
            "avg_cost":       raw.get("avgCost", 0.0),
            "market_price":   raw.get("mktPrice", 0.0),
            "market_value":   raw.get("mktValue", 0.0),
            "unrealized_pnl": raw.get("unrealizedPnl", raw.get("unrealizedPL", 0.0)),
            "currency":       raw.get("currency", "USD"),
            "asset_class":    raw.get("assetClass", "STK"),
            "account":        raw.get("acctId", raw.get("accountId", "")),
        }


# ─── Standalone CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IBKR Client Portal REST API probe"
    )
    parser.add_argument("--positions", action="store_true",
                        help="Fetch and display positions")
    parser.add_argument("--accounts", action="store_true",
                        help="Fetch and display account info")
    parser.add_argument("--orders", action="store_true",
                        help="Fetch and display live orders")
    parser.add_argument("--market-data", nargs="+", type=int, metavar="CONID",
                        help="Fetch market data for contract IDs")
    parser.add_argument("--url", default=CP_BASE_URL,
                        help=f"Client Portal base URL (default: {CP_BASE_URL})")
    args = parser.parse_args()

    cp = IBKRClientPortal(base_url=args.url)

    if not cp.is_available:
        print("WARNING: IBKR Client Portal is not available.")
        print("  Ensure Client Portal Gateway is running on localhost:5000")
        print("  OR install ibind: pip install ibind")

    if args.positions:
        positions = cp.get_positions()
        print(f"\nPositions ({len(positions)}):")
        for p in positions:
            print(f"  {p['symbol']:12s} qty={p['qty']:>8} "
                  f"price={p['market_price']:>10.2f} "
                  f"pnl={p['unrealized_pnl']:>+10.2f} "
                  f"({p['currency']})")

    if args.accounts:
        accounts = cp.get_portfolio_accounts()
        print(f"\nAccounts ({len(accounts)}):")
        for a in accounts:
            print(f"  {json.dumps(a, indent=2)}")

    if args.orders:
        orders = cp.get_live_orders()
        print(f"\nLive Orders ({len(orders)}):")
        for o in orders:
            print(f"  {json.dumps(o, indent=2)}")

    if args.market_data:
        data = cp.get_market_data(args.market_data)
        print(f"\nMarket Data ({len(data)}):")
        for d in data:
            print(f"  {json.dumps(d, indent=2)}")
