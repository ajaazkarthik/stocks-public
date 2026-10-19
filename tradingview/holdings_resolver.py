#!/usr/bin/env python3
"""
Holdings Resolver — broker positions to TradingView symbols
===========================================================
Turns the live positions written by ``global_portfolio_dashboard.py`` into
TradingView symbols the scoring pipeline can actually use.

Broker symbols are not TradingView symbols.  Feeding them straight to the
analysis engine makes every unresolved name fall back to neutral sub-scores,
which then look like real analysis.  Every position here therefore ends up with
an explicit :class:`Outcome` -- it is either resolved, or it is excluded with a
reason.  Nothing is silently neutral-scored.

Consumers:
    input_sources.holdings_to_fires()   -- option 14b Signal Stack scoring
    portfolio_score_tracker.py          -- daily L1-L4 trend capture

Usage (standalone diagnostic):
    python holdings_resolver.py
    python holdings_resolver.py --positions-file ../.cache_global_positions.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_THIS_DIR = Path(__file__).resolve().parent
ROOT = _THIS_DIR.parent

#: Written by global_portfolio_dashboard.py (option 1). Read-only here.
POSITIONS_CACHE = ROOT / ".cache_global_positions.json"

#: Broker region tag -> TradingView exchange prefix.  "US" is deliberately absent:
#: NASDAQ/NYSE/AMEX/CBOE cannot be told apart from the region alone.
_REGION_TO_TV_EXCHANGE: dict[str, str] = {
    "NSE":  "NSE",
    "BSE":  "BSE",
    "SGX":  "SGX",
    "TYO":  "TYO",
    "JP":   "TYO",
    "HK":   "HKEX",
    "ASX":  "ASX",
    "AU":   "ASX",
}


class Outcome(str, Enum):
    """What the pipeline should do with a holding."""

    EQUITY     = "equity"      # score the symbol as a normal stock
    UNDERLYING = "underlying"  # leveraged wrapper -> score the underlying equity
    FUND       = "fund"        # index/sector/commodity fund -> no equity fundamentals
    EXCLUDED   = "excluded"    # cannot be scored honestly


@dataclass(frozen=True)
class HoldingResolution:
    """One broker position mapped onto a TradingView symbol."""

    source_symbol: str          # exactly as the broker reported it
    pro_symbol: str             # TV symbol to score; empty when excluded
    broker: str
    outcome: Outcome
    detail: str = ""            # why it was excluded, or what it was mapped through

    @property
    def scoreable(self) -> bool:
        return self.outcome is not Outcome.EXCLUDED and bool(self.pro_symbol)


@lru_cache(maxsize=1)
def _etf_maps() -> tuple[dict[str, str], dict[str, str], frozenset[str]]:
    """Load ETF classification tables. Imported lazily -- sell_actionable is heavy."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tradingview.sell.sell_actionable import (
        INDEX_FUND_TICKERS,
        LEVERAGED_ETF_UNDERLYING,
        UNDERLYING_TO_TV,
    )
    return LEVERAGED_ETF_UNDERLYING, UNDERLYING_TO_TV, INDEX_FUND_TICKERS


# ── Symbol resolution ────────────────────────────────────────────────────────
def _tv_symbol_for(ticker: str, pos: dict) -> tuple[str, str]:
    """Best TradingView symbol for a broker ticker. Returns ``(pro_symbol, error)``.

    Shared by every rule: a fund needs an exchange-qualified symbol just as much
    as an equity does.  An unprefixed ticker returns no TV screener data at all,
    which is what silently produced neutral fallback scores before.
    """
    raw = str(pos.get("symbol", "")).strip().upper()
    if ":" in raw:
        return raw, ""

    prefix = _REGION_TO_TV_EXCHANGE.get(str(pos.get("exchange", "")).upper())
    if prefix:
        # Brokers report HK tickers zero-padded ("07200"); TradingView does not.
        tick = ticker.lstrip("0") if prefix == "HKEX" and ticker.isdigit() else ticker
        if not tick:
            return "", "ticker is empty once zero-padding is stripped"
        return f"{prefix}:{tick}", ""

    # US and anything else unmapped: NASDAQ/NYSE/AMEX/CBOE cannot be inferred
    # from the region, so ask TradingView which exchange lists the ticker.
    # The search is constrained to the US, or a US ticker could be answered with
    # a same-named foreign listing (US "SPAL" vs Indian "NSE:SPAL").
    from input_sources import US_TV_EXCHANGES, validate_and_normalise

    region = str(pos.get("exchange", "")).upper()
    allowed = US_TV_EXCHANGES if region == "US" else None
    result = validate_and_normalise(ticker, allowed_exchanges=allowed)
    if result.error:
        return "", f"could not resolve exchange: {result.error}"
    return result.pro_symbol, ""


# ── Resolution rules ─────────────────────────────────────────────────────────
# Each rule takes the bare ticker, the raw position dict and the ETF tables, and
# returns a HoldingResolution or None to hand over to the next rule.  Order is
# significant: wrappers must be classified before the plain-symbol rule, or a
# leveraged ETF would be scored as though it were an ordinary stock.

def _rule_index_fund(ticker, pos, maps) -> HoldingResolution | None:
    _, _, index_funds = maps
    # Brokers report HK tickers zero-padded ("07200"); the tables store them bare.
    if ticker not in index_funds and ticker.lstrip("0") not in index_funds:
        return None
    pro_symbol, error = _tv_symbol_for(ticker, pos)
    if error:
        return HoldingResolution(
            source_symbol=pos["symbol"],
            pro_symbol="",
            broker=pos.get("broker", ""),
            outcome=Outcome.EXCLUDED,
            detail=error,
        )
    return HoldingResolution(
        source_symbol=pos["symbol"],
        pro_symbol=pro_symbol,
        broker=pos.get("broker", ""),
        outcome=Outcome.FUND,
        detail="index/sector fund — equity fundamentals not applicable",
    )


def _rule_leveraged_wrapper(ticker, pos, maps) -> HoldingResolution | None:
    leveraged, underlying_to_tv, _ = maps
    underlying = leveraged.get(ticker)
    if not underlying:
        return None
    tv_underlying = underlying_to_tv.get(underlying)
    if not tv_underlying:
        return HoldingResolution(
            source_symbol=pos["symbol"],
            pro_symbol="",
            broker=pos.get("broker", ""),
            outcome=Outcome.EXCLUDED,
            detail=f"underlying {underlying} has no TradingView mapping",
        )
    return HoldingResolution(
        source_symbol=pos["symbol"],
        pro_symbol=tv_underlying,
        broker=pos.get("broker", ""),
        outcome=Outcome.UNDERLYING,
        detail=f"leveraged wrapper — scored via {underlying}",
    )


def _rule_plain_symbol(ticker, pos, maps) -> HoldingResolution | None:
    """Ordinary equity — resolve it to an exchange-qualified TradingView symbol."""
    pro_symbol, error = _tv_symbol_for(ticker, pos)
    if error:
        return HoldingResolution(
            source_symbol=pos["symbol"],
            pro_symbol="",
            broker=pos.get("broker", ""),
            outcome=Outcome.EXCLUDED,
            detail=error,
        )
    return HoldingResolution(
        source_symbol=pos["symbol"],
        pro_symbol=pro_symbol,
        broker=pos.get("broker", ""),
        outcome=Outcome.EQUITY,
    )


_RULES = (
    _rule_index_fund,
    _rule_leveraged_wrapper,
    _rule_plain_symbol,
)


def resolve_position(pos: dict) -> HoldingResolution:
    """Map a single broker position onto a TradingView symbol."""
    raw = str(pos.get("symbol", "")).strip()
    if not raw:
        return HoldingResolution("", "", pos.get("broker", ""),
                                 Outcome.EXCLUDED, "empty symbol")

    ticker = raw.split(":", 1)[-1].upper()
    maps = _etf_maps()
    for rule in _RULES:
        outcome = rule(ticker, {**pos, "symbol": raw}, maps)
        if outcome is not None:
            return outcome

    return HoldingResolution(raw, "", pos.get("broker", ""),
                             Outcome.EXCLUDED, "no resolution rule matched")


def resolve_holdings(positions: list[dict]) -> list[HoldingResolution]:
    """Resolve every position, collapsing duplicates onto the first TV symbol seen.

    Two brokers holding the same name, or NVDL and NVDU both mapping to NVDA,
    must only be scored once.
    """
    resolutions: list[HoldingResolution] = []
    seen: set[str] = set()
    for pos in positions:
        res = resolve_position(pos)
        if res.scoreable:
            if res.pro_symbol in seen:
                continue
            seen.add(res.pro_symbol)
        resolutions.append(res)
    return resolutions


# ── Positions cache access ───────────────────────────────────────────────────
def load_positions(path: Path | str | None = None) -> list[dict]:
    """Read the positions written by option 1. Returns [] when unusable."""
    cache = Path(path) if path else POSITIONS_CACHE
    try:
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh).get("positions", []) or []
    except (OSError, ValueError):
        return []


def cache_age_seconds(path: Path | str | None = None) -> float | None:
    """Age of the positions cache. None when it is missing or unreadable."""
    cache = Path(path) if path else POSITIONS_CACHE
    try:
        with open(cache, encoding="utf-8") as fh:
            saved_at = float(json.load(fh).get("saved_at", 0))
    except (OSError, ValueError, TypeError):
        return None
    if saved_at <= 0:
        return None
    return max(0.0, time.time() - saved_at)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose holdings symbol resolution")
    parser.add_argument("--positions-file", default=str(POSITIONS_CACHE))
    args = parser.parse_args()

    positions = load_positions(args.positions_file)
    if not positions:
        print(f"No positions in {args.positions_file}")
        return

    age = cache_age_seconds(args.positions_file)
    print(f"{len(positions)} positions, cache age "
          f"{'unknown' if age is None else f'{age / 60:.1f} min'}\n")

    resolutions = resolve_holdings(positions)
    width = max(len(r.source_symbol) for r in resolutions)
    for res in resolutions:
        target = res.pro_symbol or "—"
        print(f"  {res.source_symbol:<{width}}  {res.outcome.value:<10} "
              f"{target:<16} {res.detail}")

    counts: dict[str, int] = {}
    for res in resolutions:
        counts[res.outcome.value] = counts.get(res.outcome.value, 0) + 1
    print(f"\n  {dict(sorted(counts.items()))}")
    print(f"  scoreable: {sum(1 for r in resolutions if r.scoreable)}")


if __name__ == "__main__":
    _main()
