"""
ZeroDTE Orchestrator
=====================
Main pipeline that coordinates all ZeroDTE scanning steps:

  1. Build universe (fixed watchlist + TV scanner top movers)
  2. For each stock: fetch momentum data via yfinance
  3. For qualified movers: fetch IV term structure + VOI from TV
  4. Compute composite signal scores for both CALL and PUT directions
  5. For signals above threshold: select 0-2DTE contracts via yfinance options
  6. Write HTML + JSON reports, archive any previous reports
  7. Write to SQLite database for backtesting
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import ZeroDTEConfig, DEFAULT_CONFIG
from .models import (
    MoverCandidate, IVStructure, VOIData,
    SignalScore, ZeroDTEContract, ZeroDTERunResult,
)
from .fetcher import (
    _load_tv_cookies, _make_tv_headers,
    fetch_tv_top_movers, fetch_stock_data,
    fetch_iv_structure, fetch_voi_data,
    batch_filter_symbols_with_dte_options,
)
from .scorer import compute_signal_score
from .contract_filter import select_contracts_for_signal
from .database import init_db, write_contracts
from .html_reporter import write_html_report
from .json_reporter import write_json_report

_log = logging.getLogger("zerodte.orchestrator")


class ZeroDTEOrchestrator:
    """
    Coordinates the full ZeroDTE scanning pipeline.
    Designed to be called once per run.
    """

    def __init__(self, config: ZeroDTEConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self._cookies: dict = {}
        self._tv_headers: dict = {}
        self._effective_threshold: float = config.min_score_for_report

    def _load_auth(self) -> None:
        """Load TV cookies for authenticated API calls."""
        self._cookies = _load_tv_cookies(self.config.cookie_path)
        self._tv_headers = _make_tv_headers(self._cookies)
        if not self._cookies:
            _log.warning(
                "No TV cookies found at %s — IV and VOI data will be unavailable",
                self.config.cookie_path,
            )

    def _prefilter_dte_universe(self, universe: list[str]) -> list[str]:
        """
        Batch-query TV scan2 to keep only symbols that actually have 0-2DTE option
        contracts available right now.

        This dramatically cuts the yfinance workload:
          • Sunday  :  333 → ~11 symbols (only ETF weeklies have Sunday expiries)
          • Monday  :  333 → ~30-50 symbols
          • Thursday:  333 → ~100-200 symbols (Friday weekly cycle)

        Falls back to full universe if TV cookies are missing, so the pipeline
        still works in unauthenticated mode.
        """
        filtered = batch_filter_symbols_with_dte_options(universe, self.config)

        reduction = len(universe) - len(filtered)
        if reduction > 0:
            print(
                f"\n   ⚡ DTE pre-filter: {len(universe)} → {len(filtered)} stocks "
                f"({reduction} filtered — no 0-{self.config.contract_max_dte}DTE contracts)"
            )
        else:
            _log.info(
                "DTE pre-filter: no reduction (all %d symbols have 0-%dDTE options "
                "or cookies unavailable)",
                len(universe), self.config.contract_max_dte,
            )
        return filtered

    def _build_universe(self) -> list[str]:
        """Combine fixed watchlist + TV top movers, deduplicated."""
        universe: list[str] = list(self.config.fixed_watchlist)

        # Add TV scanner movers
        try:
            tv_movers = fetch_tv_top_movers(self.config)
            for sym in tv_movers:
                if sym not in universe:
                    universe.append(sym)
        except Exception as exc:
            _log.warning("TV scanner failed, using fixed watchlist only: %s", exc)

        _log.info("Universe: %d stocks total", len(universe))
        return universe

    def _screen_movers(self, universe: list[str]) -> list[MoverCandidate]:
        """
        Fetch stock data for each symbol in the universe.
        Returns candidates sorted by abs(prev_day_change_pct) descending.
        """
        candidates: list[MoverCandidate] = []
        errors = 0

        for i, sym in enumerate(universe):
            _log.debug("Fetching stock data: %s (%d/%d)", sym, i + 1, len(universe))
            candidate = fetch_stock_data(sym, self.config)
            if candidate is not None:
                candidates.append(candidate)
            else:
                errors += 1

        _log.info(
            "Stock screening: %d candidates, %d errors (of %d total)",
            len(candidates), errors, len(universe),
        )
        # Sort by absolute move (best movers first)
        candidates.sort(key=lambda c: abs(c.prev_day_change_pct), reverse=True)
        return candidates

    def _qualify_candidate(self, candidate: MoverCandidate, direction: str) -> bool:
        """Check if candidate meets minimum momentum threshold for a given direction."""
        change = candidate.prev_day_change_pct
        if direction == "call":
            # Post-earnings gap override: if stock moved ≥4% upward prev day, skip RSI max cap
            # These overbought stocks are exactly the ones we want for 0DTE continuation plays
            rsi_max = 100.0 if change >= 4.0 else self.config.momentum_rsi_call_max
            return (
                change >= self.config.momentum_min_pct_call
                and candidate.volume_ratio >= self.config.momentum_vol_ratio_min * 0.8  # 20% grace
                and self.config.momentum_rsi_call_min <= candidate.rsi <= rsi_max
            )
        else:  # put
            return (
                change <= -self.config.momentum_min_pct_put
                and candidate.volume_ratio >= self.config.momentum_vol_ratio_min * 0.8
                and self.config.momentum_rsi_put_min <= candidate.rsi <= self.config.momentum_rsi_put_max
            )

    def run_direction(
        self,
        candidates: list[MoverCandidate],
        direction: str,
        result: ZeroDTERunResult,
    ) -> list[ZeroDTEContract]:
        """
        Run the full pipeline for one direction (call or put).
        Returns sorted list of qualifying contracts.
        """
        _log.info("Running %s direction scan...", direction.upper())
        all_contracts: list[ZeroDTEContract] = []
        signals_found = 0

        for candidate in candidates:
            # Pre-screen by momentum
            if not self._qualify_candidate(candidate, direction):
                _log.debug(
                    "Skipping %s for %s: change=%.1f%%, vol_ratio=%.1f, RSI=%.0f",
                    candidate.symbol, direction,
                    candidate.prev_day_change_pct, candidate.volume_ratio, candidate.rsi,
                )
                continue

            _log.info("Evaluating %s for %s signal...", candidate.symbol, direction.upper())

            # Fetch IV term structure and VOI from TV
            iv = fetch_iv_structure(candidate.symbol, self._tv_headers, self.config)
            voi = fetch_voi_data(candidate.symbol, self._tv_headers, self.config)

            # Compute composite signal score
            signal = compute_signal_score(candidate, iv, voi, direction, self.config)

            if signal.composite_score < self._effective_threshold:
                _log.debug(
                    "Score too low: %s %s = %.1f (threshold %.0f)",
                    candidate.symbol, direction, signal.composite_score, self._effective_threshold,
                )
                continue

            signals_found += 1
            _log.info(
                "Signal found: %s %s, composite=%.1f",
                candidate.symbol, direction.upper(), signal.composite_score,
            )

            # Fetch option contracts
            contracts = select_contracts_for_signal(signal, self.config)
            _log.info(
                "%s %s: %d contracts found for %d qualifying signals",
                candidate.symbol, direction.upper(), len(contracts), signals_found,
            )
            all_contracts.extend(contracts)

        # Update result counts
        if direction == "call":
            result.signals_found_calls = signals_found
        else:
            result.signals_found_puts = signals_found

        # Sort by composite score desc, then premium asc (cheapest best-scoring first)
        all_contracts.sort(key=lambda c: (-c.composite_score, c.ask))
        return all_contracts

    def run(
        self,
        scan_directions: list[str] | None = None,
    ) -> ZeroDTERunResult:
        """
        Full ZeroDTE scan pipeline.

        scan_directions: list of "call" / "put" or None = both
        Returns ZeroDTERunResult with contract lists and report paths.
        """
        if scan_directions is None:
            scan_directions = ["call", "put"]

        now_utc = datetime.now(timezone.utc)
        result = ZeroDTERunResult(
            run_at=now_utc.isoformat(),
            run_date=now_utc.strftime("%Y-%m-%d"),
        )

        # Init database — stored in database/ not reports/ (reports/ is for HTML/JSON only)
        db_path = self.config.database_dir / self.config.database_name
        try:
            init_db(db_path)
        except Exception as exc:
            _log.error("DB init failed: %s", exc)
            result.errors.append(f"DB init failed: {exc}")

        # Load TV auth
        self._load_auth()

        # Auto-scale threshold when IV/VOI are unavailable (no cookies)
        _iv_voi_available = bool(self._cookies)
        _max_score = 40.0 + (30.0 if _iv_voi_available else 0.0) + (30.0 if _iv_voi_available else 0.0)
        self._effective_threshold = self.config.min_score_for_report * (_max_score / 100.0)
        if not _iv_voi_available:
            print(f"\n[ZeroDTE WARNING] No TV cookies found at {self.config.cookie_path}")
            print(f"[ZeroDTE WARNING] IV/VOI data unavailable — max achievable score = {_max_score:.0f}/100pts.")
            if self.config.min_score_for_report > _max_score:
                print(f"[ZeroDTE WARNING] Threshold auto-scaled: {self.config.min_score_for_report:.0f} → {self._effective_threshold:.0f}pts to produce results.")
            print("[ZeroDTE WARNING] Add tradingview/cookie for full 100pt scoring.\n")

        # Build universe, DTE pre-filter, then screen movers
        universe = self._build_universe()
        universe = self._prefilter_dte_universe(universe)
        candidates = self._screen_movers(universe)
        result.candidates_evaluated = len(candidates)

        if not candidates:
            _log.warning("No candidates fetched — check yfinance connectivity")
            result.errors.append("No candidates fetched from yfinance")

        reports_dir = self.config.reports_dir

        # ── CALL scan ─────────────────────────────────────────────────────────
        if "call" in scan_directions:
            call_contracts = self.run_direction(candidates, "call", result)
            result.contracts_calls = call_contracts

            # Write DB
            write_contracts(call_contracts, "call", self.config)

            # Write reports
            call_html = reports_dir / self.config.call_html_report
            call_json = reports_dir / self.config.call_json_report
            try:
                write_html_report(result, "call", call_contracts, call_html)
                write_json_report(result, "call", call_contracts, call_json)
                result.call_html_path = str(call_html)
                result.call_json_path = str(call_json)
                _log.info("Call report → %s", call_html)
            except Exception as exc:
                _log.error("Call report write failed: %s", exc)
                result.errors.append(f"Call report write failed: {exc}")

        # ── PUT scan ──────────────────────────────────────────────────────────
        if "put" in scan_directions:
            put_contracts = self.run_direction(candidates, "put", result)
            result.contracts_puts = put_contracts

            # Write DB
            write_contracts(put_contracts, "put", self.config)

            # Write reports
            put_html = reports_dir / self.config.put_html_report
            put_json = reports_dir / self.config.put_json_report
            try:
                write_html_report(result, "put", put_contracts, put_html)
                write_json_report(result, "put", put_contracts, put_json)
                result.put_html_path = str(put_html)
                result.put_json_path = str(put_json)
                _log.info("Put report → %s", put_html)
            except Exception as exc:
                _log.error("Put report write failed: %s", exc)
                result.errors.append(f"Put report write failed: {exc}")

        return result
