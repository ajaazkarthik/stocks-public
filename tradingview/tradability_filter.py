"""
TradabilityFilter — Classify TV symbols by broker tradability
=============================================================
Checks whether a TradingView EXCHANGE:TICKER symbol can be traded
via any of the 5 supported brokers:
  Moomoo AU (ASX), Moomoo SG (SGX + HK), IBKR (US/HK),
  Zerodha (NSE/BSE), Angel One (NSE/BSE)

Usage:
    from tradability_filter import TradabilityFilter

    tf = TradabilityFilter()
    result = tf.check("NSE:TITAN")
    # TradabilityResult(tradable=True, brokers=["Zerodha (INR)", "Angel One (INR)"], ...)

    result = tf.check("TYO:7203")
    # TradabilityResult(tradable=False, reason="TYO not supported by any of the 5 brokers")

    result = tf.check("FX:EURUSD")
    # TradabilityResult(tradable_with_limitations=True, api_execution_available=False, ...)
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Windows: force UTF-8 console ──────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ── Path setup ────────────────────────────────────────────────────────────────
_THIS = Path(__file__).parent
_ROOT = _THIS.parent
sys.path.insert(0, str(_THIS))

# ── Config flag — set to True once Moomoo SG exposes FX/Crypto API ───────────
# FIXME: re-validate Moomoo SG Forex/Crypto API availability
MOOMOO_SG_FX_CRYPTO_API_EXPOSED = False

# ── Exchange patterns ─────────────────────────────────────────────────────────
_FOREX_PREFIXES: frozenset[str] = frozenset({
    "FX", "FX_IDC", "OANDA", "FXCM", "FOREXCOM",
})

_CRYPTO_PREFIXES: frozenset[str] = frozenset({
    "BINANCE", "COINBASE", "KRAKEN", "BITSTAMP",
    "BINANCEUS", "BYBIT", "KUCOIN", "GEMINI",
})


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class TradabilityResult:
    """
    Result of a tradability check for a single TV symbol.

    Fields
    ------
    tradable                 : True if the exchange is supported by at least one broker.
    tradable_with_limitations: True for Forex/Crypto — tradable via Moomoo SG UI,
                               but API execution is not exposed.
    api_execution_available  : False when tradable_with_limitations is True.
    reason                   : Human-readable explanation when not fully tradable.
    brokers                  : List of supported broker accounts (empty if not tradable).
    exchange                 : Extracted exchange prefix.
    ticker                   : Extracted ticker (bare symbol without exchange prefix).
    """
    tradable: bool = False
    tradable_with_limitations: bool = False
    api_execution_available: bool = False
    reason: str = ""
    brokers: list[str] = field(default_factory=list)
    exchange: str = ""
    ticker: str = ""

    def as_dict(self) -> dict:
        return {
            "tradable":                  self.tradable,
            "tradable_with_limitations": self.tradable_with_limitations,
            "api_execution_available":   self.api_execution_available,
            "reason":                    self.reason,
            "brokers":                   self.brokers,
            "exchange":                  self.exchange,
            "ticker":                    self.ticker,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TradabilityFilter
# ─────────────────────────────────────────────────────────────────────────────
class TradabilityFilter:
    """
    Classify TradingView symbols by broker tradability.

    Tradable exchange set is loaded lazily from buy_advisor._EXCHANGE_ACCOUNTS.
    Falls back to a hardcoded set if buy_advisor is unavailable.
    """

    # ── Hardcoded fallback (mirrors buy_advisor._EXCHANGE_ACCOUNTS keys) ──────
    _FALLBACK_EXCHANGES: frozenset[str] = frozenset({
        "NYSE", "NASDAQ", "AMEX", "BATS", "CBOE",
        "HK", "HKEX", "SGX",
        "ASX", "ASX_DLY",
        "NSE", "BSE", "NSE_DLY", "BSE_DLY",
    })

    def __init__(self) -> None:
        self._tradable_exchanges: frozenset[str] = self._load_exchanges()
        self._exchange_accounts: dict[str, list[str]] = self._load_exchange_accounts()

    # ── Load exchange→account mapping from buy_advisor ────────────────────────
    def _load_exchanges(self) -> frozenset[str]:
        try:
            from buy_advisor import _EXCHANGE_ACCOUNTS  # type: ignore
            exch = frozenset(_EXCHANGE_ACCOUNTS.keys())
            logger.debug("TradabilityFilter: loaded %d exchanges from buy_advisor", len(exch))
            return exch
        except ImportError:
            logger.warning(
                "TradabilityFilter: buy_advisor not importable — using fallback exchange set"
            )
            return self._FALLBACK_EXCHANGES

    def _load_exchange_accounts(self) -> dict[str, list[str]]:
        try:
            from buy_advisor import _EXCHANGE_ACCOUNTS  # type: ignore
            return dict(_EXCHANGE_ACCOUNTS)
        except ImportError:
            return {k: [] for k in self._FALLBACK_EXCHANGES}

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, tv_symbol: str) -> TradabilityResult:
        """
        Check tradability for a single TradingView symbol.

        Parameters
        ----------
        tv_symbol : str
            TV format, e.g. "NSE:TITAN", "NYSE:AAPL", "FX:EURUSD".
            Bare tickers without exchange prefix are treated as US stocks (tradable).

        Returns
        -------
        TradabilityResult
        """
        if not tv_symbol or not tv_symbol.strip():
            return TradabilityResult(reason="Empty symbol")

        tv_symbol = tv_symbol.strip().upper()

        # ── Extract exchange prefix ───────────────────────────────────────────
        if ":" in tv_symbol:
            exchange, ticker = tv_symbol.split(":", 1)
        else:
            # Bare ticker — assume US stock (NYSE/NASDAQ) → tradable
            exchange = ""
            ticker   = tv_symbol

        result = TradabilityResult(exchange=exchange, ticker=ticker)

        # ── Bare ticker: assume US stock ──────────────────────────────────────
        if not exchange:
            result.tradable              = True
            result.api_execution_available = True
            result.brokers               = self._exchange_accounts.get(
                "NYSE", ["IBKR (USD)", "Moomoo AU (AUD)", "Moomoo SG (SGD)"]
            )
            result.reason                = "Bare ticker — assumed US stock (NYSE/NASDAQ)"
            return result

        # ── Forex check ───────────────────────────────────────────────────────
        if exchange in _FOREX_PREFIXES:
            logger.debug(
                "TradabilityFilter [FOREX] %s — Moomoo SG manual, API not exposed (MOOMOO_SG_FX_CRYPTO_API_EXPOSED=%s)",
                tv_symbol, MOOMOO_SG_FX_CRYPTO_API_EXPOSED,
            )
            result.tradable_with_limitations = True
            result.api_execution_available   = MOOMOO_SG_FX_CRYPTO_API_EXPOSED
            result.brokers                   = ["Moomoo SG (SGD)"]
            result.reason = (
                "Tradable via Moomoo SG but API not exposed — verify periodically"
            )
            return result

        # ── Crypto check ─────────────────────────────────────────────────────
        if exchange in _CRYPTO_PREFIXES:
            logger.debug(
                "TradabilityFilter [CRYPTO] %s — Moomoo SG manual, API not exposed (MOOMOO_SG_FX_CRYPTO_API_EXPOSED=%s)",
                tv_symbol, MOOMOO_SG_FX_CRYPTO_API_EXPOSED,
            )
            result.tradable_with_limitations = True
            result.api_execution_available   = MOOMOO_SG_FX_CRYPTO_API_EXPOSED
            result.brokers                   = ["Moomoo SG (SGD)"]
            result.reason = (
                "Tradable via Moomoo SG but API not exposed — verify periodically"
            )
            return result

        # ── Supported exchange check ──────────────────────────────────────────
        if exchange in self._tradable_exchanges:
            result.tradable              = True
            result.api_execution_available = True
            result.brokers               = self._exchange_accounts.get(exchange, [])
            result.reason                = ""
            return result

        # ── Not supported ─────────────────────────────────────────────────────
        result.tradable              = False
        result.api_execution_available = False
        result.reason = (
            f"{exchange} not supported by any of the 5 brokers "
            "(Moomoo AU, Moomoo SG, IBKR, Zerodha, Angel One)"
        )
        return result

    def is_forex_or_crypto(self, tv_symbol: str) -> bool:
        """Return True if the symbol is Forex or Crypto (never auto-remove)."""
        if ":" not in tv_symbol:
            return False
        exchange = tv_symbol.split(":", 1)[0].upper()
        return exchange in _FOREX_PREFIXES or exchange in _CRYPTO_PREFIXES

    def filter_results(
        self,
        results: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """
        Filter a list of analysis result dicts by tradability.

        Parameters
        ----------
        results : list[dict]
            Each dict must have a "tv_symbol" key.

        Returns
        -------
        (kept, skipped_non_tradable)
            kept                 — results where tradable=True or tradable_with_limitations=True
            skipped_non_tradable — results filtered out (tradable=False, limitations=False)
                                   Each entry: {symbol, reason, source, result}
        """
        kept: list[dict] = []
        skipped: list[dict] = []

        for r in results:
            sym = r.get("tv_symbol", r.get("symbol", ""))
            check = self.check(sym)

            if check.tradable:
                r["_tradability"] = check.as_dict()
                kept.append(r)

            elif check.tradable_with_limitations:
                # Keep but flag for UI badge
                r["_tradability"]              = check.as_dict()
                r["_tradable_with_limitations"] = True
                kept.append(r)

            else:
                source = r.get("_source", r.get("input_source", "unknown"))
                logger.info(
                    "SKIP %s: not tradable via any supported broker — %s",
                    sym, check.reason,
                )
                skipped.append({
                    "symbol": sym,
                    "reason": check.reason,
                    "source": source,
                    "result": r,
                })

        return kept, skipped
