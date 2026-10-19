"""
TQS v2 TV Stock Scanner
========================
Fetches the quality stock universe from TradingView america/scan.
Implements IStockScreener.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from ..config import TQSv2Config
from ..exceptions import TVCookieError, TVScanError
from ..interfaces.contracts import IStockScreener
from .models import StockRecord

_log = logging.getLogger("tqs_v2.market_data.tv_scanner")


def _read_tv_cookie(cookie_path: Path) -> dict[str, str]:
    """
    Parse the TradingView cookie file and return a dict
    with at least `sessionid` and `sessionid_sign`.

    Raises:
        TVCookieError: if the file is missing or malformed.
    """
    if not cookie_path.exists():
        raise TVCookieError(
            f"TV cookie file not found: {cookie_path}. "
            "Refresh your TradingView session and update the cookie file."
        )

    raw = cookie_path.read_text(encoding="utf-8").strip()
    cookies: dict[str, str] = {}

    def _parse_kv_pairs(text: str) -> None:
        """Parse semicolon-separated key=value cookie pairs into cookies dict."""
        for part in text.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, val = part.partition("=")
                key = key.strip()
                val = val.strip()
                # Skip complex JSON values (e.g. cookiesSettings={"analytics":true})
                if key and not val.startswith("{") and not val.startswith("["):
                    cookies[key] = val
                elif key and key not in cookies:
                    cookies[key] = val

    # Support Netscape cookie format, multi-line key=value, or single-line semicolon-separated
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            # Netscape format: domain\t flag\t path\t secure\t expiry\t name\t value
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()
        elif ";" in line:
            # Single-line browser cookie string: key=val; key2=val2; ...
            _parse_kv_pairs(line)
        elif "=" in line:
            key, _, val = line.partition("=")
            cookies[key.strip()] = val.strip()

    if "sessionid" not in cookies:
        raise TVCookieError(
            "TV cookie file is missing 'sessionid'. "
            "Re-export cookies from your browser."
        )

    _log.debug("TV cookie loaded with %d keys", len(cookies))
    return cookies


class TVStockScanner(IStockScreener):
    """
    Fetches quality US stocks from TradingView scanner.

    The scanner uses POST /america/scan with quality filters.
    Response format: {totalCount, data:[{d:[v1,v2,...]}]}
    """

    def __init__(self, *, cookie_path: Optional[Path] = None) -> None:
        self._cookie_path = cookie_path

    def fetch_quality_stocks(
        self,
        config: TQSv2Config,
    ) -> list[StockRecord]:
        """
        Return up to config.stock_max_results quality stocks.

        Raises:
            TVCookieError: if the cookie file is missing.
            TVScanError: if the scan API call fails.
        """
        cookie_path = self._cookie_path or config.cookie_path
        cookies = _read_tv_cookie(cookie_path)

        payload = self._build_scan_payload(config)
        headers = self._build_headers()

        for attempt in range(1, config.http_max_retries + 1):
            try:
                resp = requests.post(
                    config.tv_stock_scan_url,
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    timeout=config.http_timeout,
                )

                if resp.status_code == 401:
                    raise TVCookieError(
                        "TradingView returned 401 — session cookie has expired. "
                        "Re-export cookies from your browser."
                    )

                if resp.status_code != 200:
                    raise TVScanError(
                        f"TV stock scan returned HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                data = resp.json()
                records = self._parse_response(data, list(config.stock_scan_columns))
                _log.info(
                    "TV stock scan: %d stocks returned (totalCount=%s)",
                    len(records),
                    data.get("totalCount", "?"),
                )
                return records

            except (TVCookieError, TVScanError):
                raise
            except Exception as exc:
                if attempt < config.http_max_retries:
                    wait = config.http_retry_backoff * attempt
                    _log.warning(
                        "TV stock scan attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt,
                        config.http_max_retries,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise TVScanError(
                        f"TV stock scan failed after {config.http_max_retries} attempts: {exc}"
                    ) from exc

        return []  # unreachable, but satisfies type-checker

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_scan_payload(config: TQSv2Config) -> dict:
        # Base quality filters — always applied
        filters: list[dict] = [
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "subtype", "operation": "in_range", "right": ["common"]},
            {
                "left": "exchange",
                "operation": "in_range",
                "right": list(config.stock_exchanges),
            },
            {
                "left": "market_cap_basic",
                "operation": "greater",
                "right": config.stock_min_market_cap,
            },
            {
                "left": "close",
                "operation": "greater",
                "right": config.stock_min_price,
            },
            {
                "left": "volume",
                "operation": "greater",
                "right": config.stock_min_volume,
            },
        ]

        # Optional: relative volume — removes dead/halted stocks, keeps active movers
        if config.stock_min_relative_volume > 0:
            filters.append({
                "left": "relative_volume_10d_calc",
                "operation": "greater",
                "right": config.stock_min_relative_volume,
            })

        # Optional: RSI momentum zone — bullish for calls, bearish for puts
        if config.stock_rsi_min > 0:
            filters.append({
                "left": "RSI",
                "operation": "greater",
                "right": config.stock_rsi_min,
            })
        if config.stock_rsi_max < 100:
            filters.append({
                "left": "RSI",
                "operation": "less",
                "right": config.stock_rsi_max,
            })

        # Optional: price cap — for budget presets, expensive stocks have expensive options
        if config.stock_max_price > 0:
            filters.append({
                "left": "close",
                "operation": "less",
                "right": config.stock_max_price,
            })

        return {
            "filter": filters,
            "columns": list(config.stock_scan_columns),
            "sort": {
                "sortBy": config.stock_sort_by,   # momentum sort, not just market cap
                "sortOrder": "desc",
            },
            "range": [0, config.stock_max_results],
        }

    @staticmethod
    def _build_headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _parse_response(data: dict, columns: list[str]) -> list[StockRecord]:
        """
        Parse the TV scan response into StockRecord objects.

        TV scan response format:
          {totalCount: N, data: [{d: [v1, v2, ...]}, ...]}
        """
        records: list[StockRecord] = []
        for row in data.get("data", []):
            d = row.get("d", [])
            if not d:
                continue
            try:
                record = StockRecord.from_scan_row(d, columns)
                if record.symbol and record.close > 0:
                    records.append(record)
            except Exception as exc:
                _log.debug("Skipping malformed row: %s", exc)
        return records
