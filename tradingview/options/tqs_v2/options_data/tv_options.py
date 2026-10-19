"""
TQS v2 TV Options Fetcher
==========================
Fetches raw option contracts from TradingView options/scan2 for a single stock.
Implements IOptionsFetcher.

CRITICAL: Must use `index_filters` for underlying_symbol (NOT `filter`).
          Fields `open_interest`, `dte`, `moneyness_pct`, `spot_price` are INVALID
          in scan2 → HTTP 400. They are computed client-side or enriched separately.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from ..config import TQSv2Config
from ..exceptions import TVCookieError, TVOptionsScan2Error
from ..interfaces.contracts import IOptionsFetcher
from ..market_data.models import StockRecord
from ..market_data.tv_scanner import _read_tv_cookie
from .models import RawContract

_log = logging.getLogger("tqs_v2.options_data.tv_options")

# Maximum contracts to request per scan2 call
_SCAN2_MAX_RANGE = 500


class TVOptionsFetcher(IOptionsFetcher):
    """
    Fetches option contracts from TV scan2 for a given underlying stock.

    One scan2 call is made per (stock, option_type) combination.
    Client-side filtering is applied for DTE and moneyness band.
    """

    def __init__(self, *, cookie_path: Optional[Path] = None) -> None:
        self._cookie_path = cookie_path
        self._cookies: Optional[dict[str, str]] = None

    def _get_cookies(self, config: TQSv2Config) -> dict[str, str]:
        """Lazy-load and cache cookies."""
        if self._cookies is None:
            path = self._cookie_path or config.cookie_path
            self._cookies = _read_tv_cookie(path)
        return self._cookies

    def fetch_contracts(
        self,
        stock: StockRecord,
        option_type: str,    # "call" or "put"
        config: TQSv2Config,
    ) -> list[RawContract]:
        """
        Fetch option contracts for the given stock and option type.

        Returns filtered contracts meeting DTE and moneyness criteria.
        OI is NOT enriched here — handled by YFinanceOIEnricher separately.

        Raises:
            TVOptionsScan2Error: on API failure after all retries.
        """
        option_type = option_type.lower()
        cookies = self._get_cookies(config)
        payload = self._build_payload(stock.tv_symbol, option_type, config)
        headers = self._build_headers()
        url = (
            f"{config.tv_options_scan2_url}"
            f"?label-product={config.tv_scan2_label_product}"
        )

        raw_data: Optional[dict] = None
        for attempt in range(1, config.http_max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    timeout=config.http_timeout,
                )

                if resp.status_code == 401:
                    raise TVCookieError(
                        "TV cookie expired — cannot fetch options chain."
                    )

                if resp.status_code == 400:
                    _log.warning(
                        "scan2 HTTP 400 for %s %s: %s",
                        stock.symbol,
                        option_type,
                        resp.text[:300],
                    )
                    return []  # Bad request for this stock — skip gracefully

                if resp.status_code != 200:
                    raise TVOptionsScan2Error(
                        f"scan2 HTTP {resp.status_code} for {stock.symbol}: {resp.text[:200]}"
                    )

                raw_data = resp.json()
                break

            except (TVCookieError, TVOptionsScan2Error):
                raise
            except Exception as exc:
                if attempt < config.http_max_retries:
                    wait = config.http_retry_backoff * attempt
                    _log.warning(
                        "scan2 attempt %d/%d for %s %s failed: %s — retrying in %.1fs",
                        attempt,
                        config.http_max_retries,
                        stock.symbol,
                        option_type,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise TVOptionsScan2Error(
                        f"scan2 failed for {stock.symbol} {option_type} "
                        f"after {config.http_max_retries} attempts: {exc}"
                    ) from exc

        if raw_data is None:
            return []

        contracts = self._parse_response(
            raw_data,
            stock=stock,
            option_type=option_type,
            config=config,
        )

        _log.debug(
            "scan2 %s %s: %d/%s contracts after DTE+moneyness filter",
            stock.symbol,
            option_type.upper(),
            len(contracts),
            raw_data.get("totalCount", "?"),
        )
        return contracts

    # ── Payload builder ──────────────────────────────────────────────────────

    @staticmethod
    def _build_payload(
        tv_symbol: str,
        option_type: str,
        config: TQSv2Config,
    ) -> dict:
        """
        Build scan2 request payload.

        CRITICAL: underlying_symbol MUST be in `index_filters`, NOT in `filter`.
                  `filter` with underlying_symbol returns HTTP 400.
        """
        return {
            "columns": list(config.scan2_columns),
            "ignore_unknown_fields": False,
            "index_filters": [
                {"name": "underlying_symbol", "values": [tv_symbol]}
            ],
            "filter2": {
                "operator": "and",
                "operands": [
                    {
                        "expression": {
                            "left": "type",
                            "operation": "equal",
                            "right": "option",
                        }
                    },
                    {
                        "expression": {
                            "left": "option-type",
                            "operation": "equal",
                            "right": option_type,
                        }
                    },
                ],
            },
            "options": {"lang": "en"},
            "range": [0, _SCAN2_MAX_RANGE],
        }

    @staticmethod
    def _build_headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/options/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _parse_response(
        data: dict,
        stock: StockRecord,
        option_type: str,
        config: TQSv2Config,
    ) -> list[RawContract]:
        """
        Parse scan2 response into RawContract objects.

        scan2 response format:
          {totalCount: N, fields: [...], symbols: [{s: "OPRA:...", f: [...]}, ...]}

        Applies client-side DTE and moneyness filters.
        """
        fields: list[str] = data.get("fields", [])
        symbols: list[dict] = data.get("symbols", [])
        today = date.today()

        contracts: list[RawContract] = []

        for sym_entry in symbols:
            opra_code: str = sym_entry.get("s", "")
            f: list = sym_entry.get("f", [])

            try:
                contract = RawContract.from_scan2_row(
                    opra_code=opra_code,
                    f=f,
                    fields=fields,
                    stock_close=stock.close,
                    stock_exchange=stock.exchange,
                )
            except Exception as exc:
                _log.debug("Skipping malformed scan2 row %s: %s", opra_code, exc)
                continue

            # ── Client-side DTE filter ────────────────────────────────────
            if not (config.contract_min_dte <= contract.dte <= config.contract_max_dte):
                continue

            # ── Client-side moneyness band filter ─────────────────────────
            # For CALL: we want slightly OTM contracts (spot < strike × 1.20)
            # For PUT: we want slightly OTM contracts (spot > strike × 0.80)
            if stock.close > 0:
                band = config.contract_moneyness_pct_band / 100.0
                if abs(contract.moneyness_pct) > config.contract_moneyness_pct_band:
                    continue

            contracts.append(contract)

        return contracts
