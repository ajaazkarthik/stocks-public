"""
TQS v2 Orchestrator
=====================
Top-level scan coordinator. Wires together all pipeline stages:

  Stage 1:  Fetch quality stocks from TV america/scan (top 300)
  Stage 2:  Per-stock: fetch option contracts from TV scan2  [+circuit breaker]
  Stage 3:  Batch OI enrichment via yfinance
  Stage 3b: CBOE CDN OI fallback (activated when yfinance fill rate < 5%)
  Stage 4:  Optional IV term structure enrichment
  Stage 4b: Optional volatility enrichment (voi-lens PCR + vol-chart skew)
  Stage 5:  Contract filter pipeline (bid, spread, delta, OI, DTE)
  Stage 6:  Scoring (7 dimensions)
  Stage 7:  Ranking (top N per stock, top M global)
  Stage 8:  Reporting (HTML + JSON)
  Stage 9:  Persistence (SQLite)

Anti-patterns enforced:
  - SD-004: ThreadPoolExecutor never used with `with` + `fut.result(timeout=N)`
  - SD-005: Reports to REPORTS dir via config (never tradingview/reports/)
  - No import from old options modules
  - datetime.now(timezone.utc) — never datetime.utcnow()
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import TQSv2Config, DEFAULT_CONFIG
from .exceptions import OrchestratorError
from .market_data.models import StockRecord
from .market_data.tv_scanner import TVStockScanner
from .options_data.models import RawContract
from .options_data.tv_options import TVOptionsFetcher
from .options_data.yfinance_oi import YFinanceOIEnricher
from .options_data.cboe_oi import CboeOIEnricher
from .options_data.iv_structure import TVIVTermStructure, TVVoiLens, TVVolatilityChart
from .filtering.pipeline import FilterPipeline
from .scoring.engine import ScoringEngine
from .ranking.engine import RankingEngine
from .ranking.models import RankedContract
from .reporting.html_reporter import HTMLReporter
from .reporting.json_reporter import JSONReporter
from .reporting.terminal_reporter import print_summary
from .persistence.db import TQSv2Database

_log = logging.getLogger("tqs_v2.orchestrator")


# ── Correlation ID log adapter ────────────────────────────────────────────────

class _RunContextAdapter(logging.LoggerAdapter):
    """Prepends [run_id] to every log message for correlation tracing."""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        run_id = self.extra.get("run_id", "unknown")
        return f"[{run_id}] {msg}", kwargs


# ── Circuit Breaker ───────────────────────────────────────────────────────────

@dataclass
class _CircuitBreaker:
    """
    Opens when too many consecutive fetch failures occur in Stage 2.
    Prevents endless API hammering on a broken connection.
    """
    max_consecutive: int = 5

    consecutive_failures: int = field(default=0, init=False)
    total_failures: int = field(default=0, init=False)
    total_attempts: int = field(default=0, init=False)
    is_open: bool = field(default=False, init=False)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_attempts += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_attempts += 1
        if self.consecutive_failures >= self.max_consecutive:
            self.is_open = True

    @property
    def state(self) -> str:
        return "OPEN" if self.is_open else "CLOSED"


# ── Orchestrator ──────────────────────────────────────────────────────────────

class TQSv2Orchestrator:
    """
    Coordinates a full TQS v2 scan.

    Usage:
        orch = TQSv2Orchestrator(config=DEFAULT_CONFIG)
        ranked = orch.run(scan_type="calls")
    """

    def __init__(self, config: TQSv2Config = DEFAULT_CONFIG) -> None:
        self._config = config
        self._scanner = TVStockScanner(cookie_path=config.cookie_path)
        self._fetcher = TVOptionsFetcher(cookie_path=config.cookie_path)
        self._oi_enricher = YFinanceOIEnricher()
        self._cboe_enricher = CboeOIEnricher()
        self._iv_enricher = TVIVTermStructure()
        self._voi_lens = TVVoiLens()
        self._vol_chart = TVVolatilityChart()
        self._filter_pipeline = FilterPipeline()
        self._scorer = ScoringEngine()
        self._ranker = RankingEngine()
        self._html_reporter = HTMLReporter()
        self._json_reporter = JSONReporter()

    def run(self, scan_type: str) -> list[RankedContract]:
        """
        Execute a full scan for "calls" or "puts".

        Returns the final ranked list.
        Also writes HTML + JSON reports and saves to DB.
        """
        scan_type = scan_type.lower()
        if scan_type not in ("calls", "puts"):
            raise OrchestratorError(f"Invalid scan_type: '{scan_type}'")

        option_type = "call" if scan_type == "calls" else "put"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_id = f"tqs2_{scan_type}_{ts}_{uuid.uuid4().hex[:6]}"
        cfg = self._config

        # Correlation-aware log adapter — every message carries the run_id
        run_log = _RunContextAdapter(_log, {"run_id": run_id})

        run_log.info("=" * 60)
        run_log.info("TQS v2 %s scan starting", scan_type.upper())
        run_log.info("=" * 60)
        t0 = time.time()

        # ── Stage 1: Stock universe ─────────────────────────────────────────
        run_log.info("[Stage 1] Fetching quality stocks from TV screener...")
        stocks = self._scanner.fetch_quality_stocks(cfg)
        run_log.info("[Stage 1] Found %d quality stocks", len(stocks))

        # Build stock_record_map by bare ticker for catalyst enrichment
        stock_record_map: dict[str, StockRecord] = {}
        if cfg.catalyst_mode:
            stock_record_map = {s.symbol: s for s in stocks}
            run_log.info("[Stage 1] Built stock_record_map (%d symbols) for catalyst mode", len(stock_record_map))

        # ── Stage 2: Fetch option contracts per stock (parallel + circuit breaker) ─
        # SD-004 pattern: never `with ThreadPoolExecutor()` when using fut.result(timeout=N)
        # — the `with` block calls shutdown(wait=True) on __exit__ which blocks even after
        # TimeoutError.  Always use shutdown(wait=False) in a finally clause.
        n_workers = min(cfg.max_concurrent_scan2, max(1, len(stocks)))
        run_log.info(
            "[Stage 2] Fetching %s contracts for %d stocks (parallel=%d)...",
            option_type, len(stocks), n_workers,
        )
        all_raw: list[RawContract] = []
        cb = _CircuitBreaker(max_consecutive=cfg.circuit_breaker_max_consecutive)

        # Warm cookie cache in the main thread before parallel dispatch
        self._fetcher._get_cookies(cfg)

        def _fetch_one(stock: StockRecord) -> list[RawContract]:
            """Fetch contracts for one stock; per-worker sleep for rate-limiting."""
            contracts = self._fetcher.fetch_contracts(stock, option_type, cfg)
            if cfg.scan2_delay_sec > 0:
                time.sleep(cfg.scan2_delay_sec)
            return contracts

        _ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=n_workers, thread_name_prefix="tqs2_s2"
        )
        _futs: dict[concurrent.futures.Future, StockRecord] = {
            _ex.submit(_fetch_one, stock): stock for stock in stocks
        }
        _done = 0
        try:
            for _fut in concurrent.futures.as_completed(_futs, timeout=600):
                stock = _futs[_fut]
                _done += 1
                if cb.is_open:
                    break
                try:
                    contracts = _fut.result()
                    all_raw.extend(contracts)
                    cb.record_success()
                    if _done % 50 == 0:
                        run_log.info(
                            "[Stage 2] %d/%d stocks done | %d contracts so far",
                            _done, len(stocks), len(all_raw),
                        )
                except Exception as exc:
                    run_log.warning(
                        "Options fetch failed for %s: %s (consecutive=%d)",
                        stock.symbol, exc, cb.consecutive_failures + 1,
                    )
                    cb.record_failure()
        except concurrent.futures.TimeoutError:
            run_log.error("[Stage 2] Parallel fetch timed out after 600s")
        finally:
            _ex.shutdown(wait=False)  # SD-004: never block — let in-flight threads finish

        if cb.is_open:
            run_log.error(
                "[Stage 2] Circuit breaker OPEN after %d consecutive failures",
                cb.max_consecutive,
            )
        run_log.info(
            "[Stage 2] Total raw contracts fetched: %d | CB state=%s failures=%d/%d",
            len(all_raw), cb.state, cb.total_failures, cb.total_attempts,
        )

        # ── Stage 3: CBOE CDN OI (primary) ────────────────────────────────────────────────
        # One HTTP call per symbol fetches ALL expiries at once.
        # Benchmarked 2026-05-26: avg 1.5 s/sym, 72-84% full-scan fill, 93-99% near-ATM fill.
        # Beats yfinance (1 call per expiry, ~20% fill) for bulk OI enrichment.
        run_log.info("[Stage 3] CBOE CDN primary OI enrichment: %d contracts...", len(all_raw))
        all_raw = self._cboe_enricher.enrich(all_raw, cfg)
        _cboe_filled = sum(1 for c in all_raw if c.open_interest and c.open_interest > 0)
        run_log.info(
            "[Stage 3] CBOE OI: %d/%d filled (%.1f%%)",
            _cboe_filled, len(all_raw), 100.0 * _cboe_filled / max(len(all_raw), 1),
        )

        # ── Stage 3b: yfinance OI supplement ──────────────────────────────────────────────
        # Fill contracts where CBOE returned zero OI (deep-OTM, short-dated, unusual strikes).
        # Only contracts still OI=None are passed — minimises yfinance API calls.
        # Mutations propagate back to all_raw (same objects, in-place mutate).
        _missing_oi = [c for c in all_raw if c.open_interest is None]
        if _missing_oi:
            run_log.info(
                "[Stage 3b] yfinance supplement: %d contracts still missing OI...",
                len(_missing_oi),
            )
            self._oi_enricher.enrich(_missing_oi, cfg)
            _final_filled = sum(1 for c in all_raw if c.open_interest and c.open_interest > 0)
            run_log.info(
                "[Stage 3b] yfinance added %d contracts  (total OI: %d/%d = %.1f%%)",
                _final_filled - _cboe_filled,
                _final_filled, len(all_raw),
                100.0 * _final_filled / max(len(all_raw), 1),
            )
        else:
            run_log.info("[Stage 3b] All contracts have OI from CBOE — yfinance skipped")

        # TASK 5iii: Copy stock technical context to each contract
        # Done after OI enrichment so all_raw is final; O(n) dict lookup
        _stock_lookup: dict[str, StockRecord] = {s.tv_symbol: s for s in stocks}
        for _c in all_raw:
            _stock = _stock_lookup.get(_c.underlying_symbol)
            if _stock:
                _c.stock_rsi = _stock.rsi
                _c.stock_sma200 = _stock.sma200
                _c.stock_sma50 = getattr(_stock, "sma50", None)
                _c.stock_macd = getattr(_stock, "macd", None)
                _c.stock_macd_signal = getattr(_stock, "macd_signal", None)
                _c.stock_atr = getattr(_stock, "atr", None)
                _c.stock_adx = getattr(_stock, "adx", None)
                _c.stock_plus_di = getattr(_stock, "plus_di", None)   # Gap 1
                _c.stock_minus_di = getattr(_stock, "minus_di", None)  # Gap 1
                _c.stock_change_pct = getattr(_stock, "change_pct", None)  # Gap 5
                _c.stock_perf_w = getattr(_stock, "perf_w", None)        # Gap 3: weekly perf
                _c.stock_perf_1m = getattr(_stock, "perf_1m", None)      # Gap 3: monthly perf
                # Earnings proximity: days until next earnings event.
                # Used by engine.py to skip contracts spanning earnings (IV crush risk).
                _c.earnings_dte = getattr(_stock, "earnings_dte", None)
        # ── Stage 4: IV term structure enrichment (optional) ────────────────
        if cfg.enable_iv_term_structure:
            run_log.info("[Stage 4] Fetching IV term structure per stock...")
            _apply_iv_enrichment(all_raw, self._iv_enricher, cfg)
        else:
            run_log.info("[Stage 4] IV term structure disabled")

        # ── Stage 4b: Volatility enrichment — voi-lens PCR + vol-chart skew ─
        if cfg.enable_vol_enrichment:
            run_log.info(
                "[Stage 4b] Fetching voi-lens PCR + volatility-chart skew per stock..."
            )
            _apply_vol_enrichment(all_raw, self._voi_lens, self._vol_chart, cfg)
        else:
            run_log.info("[Stage 4b] Volatility enrichment disabled")

        # Stage 4c: WebSocket volume enrichment removed.
        # Volume is now fetched from yfinance alongside OI in Stage 4b.
        # yfinance option_chain() provides volume in the same API call — no
        # extra requests, no WebSocket complexity, no timeout risk.

        # ── Stage 4d: Real IV percentile enrichment (C-3 fix 2026-05-27) ──
        if cfg.enable_iv_term_structure:
            run_log.info("[Stage 4d] Fetching real IV percentile from tv_in_time_iv...")
            from .options_data.iv_structure import fetch_iv_percentile

            # Group by symbol to avoid redundant API calls
            _iv_symbols_seen: set[str] = set()
            _iv_pctile_cache: dict[str, float | None] = {}

            for _c in all_raw:
                _sym = str(getattr(_c, "root", "") or "")
                _exchange = str(getattr(_c, "exchange", "NASDAQ") or "NASDAQ")
                if _sym and _sym not in _iv_symbols_seen:
                    _iv_symbols_seen.add(_sym)
                    _cur_iv = float(getattr(_c, "iv", 0) or 0)
                    _tv_sym = f"{_exchange}:{_sym}"
                    _pctile = fetch_iv_percentile(_tv_sym, _cur_iv)
                    _iv_pctile_cache[_sym] = _pctile

            # Apply to all contracts
            _iv_enriched = 0
            for _c in all_raw:
                _sym = str(getattr(_c, "root", "") or "")
                _pctile = _iv_pctile_cache.get(_sym)
                if _pctile is not None:
                    _c.iv_percentile_real = _pctile
                    _iv_enriched += 1

            run_log.info(
                "[Stage 4d] IV percentile enriched %d/%d contracts (%d symbols)",
                _iv_enriched, len(all_raw), len(_iv_symbols_seen),
            )
        else:
            run_log.info("[Stage 4d] IV percentile skipped (IV term structure disabled)")

        # ── Stage 4e: Catalyst enrichment (catalyst_mode only) ──────────────
        if cfg.catalyst_mode:
            run_log.info("[Stage 4e] Catalyst enrichment (iv_hump, vol_skew, breakout, analyst)...")
            from types import SimpleNamespace
            from .options_data.catalyst_enricher import CatalystEnricher

            # Build iv_snapshot_map from per-contract iv_1w/iv_2w/iv_1m already attached
            iv_snapshot_map: dict[str, object] = {}
            voi_data_map: dict[str, object] = {}
            seen_syms: set[str] = set()
            for _c in all_raw:
                sym = getattr(_c, "root", None)
                if sym and sym not in seen_syms:
                    seen_syms.add(sym)
                    iv_snapshot_map[sym] = SimpleNamespace(
                        iv_1w=getattr(_c, "iv_1w", None),
                        iv_2w=getattr(_c, "iv_2w", None),
                        iv_1m=getattr(_c, "iv_1m", None),
                    )
                    pcr = getattr(_c, "voi_pcr", None) or 1.0
                    voi_data_map[sym] = SimpleNamespace(
                        call_volume=1.0 / (1.0 + pcr),
                        put_volume=pcr / (1.0 + pcr),
                    )

            _catalyst_enricher = CatalystEnricher(
                yfinance_delay_sec=cfg.yfinance_delay_sec,
            )
            all_raw = _catalyst_enricher.enrich(
                all_raw,
                iv_snapshots=iv_snapshot_map,
                voi_data=voi_data_map,
                stock_records=stock_record_map,
            )
            run_log.info("[Stage 4e] Catalyst enrichment complete")

        # ── Stage 5: Filter pipeline ─────────────────────────────────────────
        run_log.info("[Stage 5] Applying filter pipeline to %d contracts...", len(all_raw))
        filtered, filter_stats = self._filter_pipeline.apply(all_raw, cfg)
        run_log.info(
            "[Stage 5] Filter result: %d → %d contracts (%.1f%% pass)",
            filter_stats.total_in,
            filter_stats.total_out,
            filter_stats.pass_rate,
        )

        # ── Stage 6: Scoring ─────────────────────────────────────────────────
        run_log.info("[Stage 6] Scoring %d contracts...", len(filtered))
        scored = self._scorer.score_all(filtered, cfg)
        run_log.info(
            "[Stage 6] %d contracts scored above threshold %.1f",
            len(scored), cfg.min_score_for_report,
        )

        # ── Stage 7: Ranking ──────────────────────────────────────────────────
        run_log.info("[Stage 7] Ranking contracts...")
        ranked = self._ranker.rank(scored, cfg)
        run_log.info("[Stage 7] Final ranked list: %d contracts", len(ranked))

        # ── Stage 8: Reporting ────────────────────────────────────────────────
        run_log.info("[Stage 8] Generating reports...")
        # Build pipeline_stats so JSON consumers can see full funnel throughput.
        # This converts the 4 PARTIAL scrutiny items to CONFIRMED (HIGH-5 observability).
        pipeline_stats = {
            "stocks_scanned": len(stocks),
            "contracts_fetched": len(all_raw),
            "contracts_after_filter": filter_stats.total_out,
            "filter_pass_rate_pct": round(filter_stats.pass_rate, 1),
            "filter_rejections": filter_stats.rejections,  # {filter_name: count}
            "contracts_above_threshold": len(scored),
            "contracts_in_output": len(ranked),
        }
        html_path = self._html_reporter.generate(ranked, scan_type, run_id, cfg)
        json_path = self._json_reporter.generate(ranked, scan_type, run_id, cfg, pipeline_stats=pipeline_stats)
        print_summary(ranked, scan_type, run_id)
        print(f"\n  HTML report: {html_path}")
        print(f"  JSON report: {json_path}")

        # ── Stage 9: Persistence ──────────────────────────────────────────────
        run_log.info("[Stage 9] Saving to database...")
        try:
            db = TQSv2Database(cfg)
            scan_id = db.create_scan_record(run_id, scan_type)
            db.update_scan_stats(
                run_id=run_id,
                stocks_scanned=len(stocks),
                contracts_fetched=len(all_raw),
                contracts_after_filter=len(filtered),
                contracts_scored=len(scored),
                status="completed",
            )
            db.save_results(scan_id, run_id, ranked)
            db.save_stock_universe(scan_id, run_id, stocks)

            # ── Stage 9b: OI snapshot (catalyst_mode only) ────────────────────
            if cfg.catalyst_mode:
                try:
                    from .persistence.oi_snapshots import OISnapshotStore
                    oi_store = OISnapshotStore(cfg.database_dir / cfg.database_name)
                    # Group all_raw contracts by symbol for batch snapshot save
                    oi_by_symbol: dict[str, list[dict]] = {}
                    for _c in all_raw:
                        sym = getattr(_c, "root", None)
                        if sym:
                            oi_by_symbol.setdefault(sym, []).append({
                                "strike":      _c.strike,
                                "expiry":      str(_c.expiration),
                                "option_type": _c.option_type,
                                "oi":          _c.open_interest or 0,
                                "volume":      _c.volume or 0,
                            })
                    for sym, contracts_for_sym in oi_by_symbol.items():
                        oi_store.save_snapshot(sym, contracts_for_sym, source="cboe")
                    run_log.info(
                        "[Stage 9b] OI snapshots saved for %d symbols", len(oi_by_symbol)
                    )
                except Exception as exc:
                    run_log.warning("OI snapshot save failed (non-fatal): %s", exc)

            db.close()
        except Exception as exc:
            run_log.warning("DB persistence failed (non-fatal): %s", exc)

        elapsed = time.time() - t0
        run_log.info(
            "TQS v2 %s scan complete in %.1fs | %d results",
            scan_type.upper(), elapsed, len(ranked),
        )

        return ranked


def _apply_iv_enrichment(
    contracts: list[RawContract],
    iv_enricher: TVIVTermStructure,
    cfg: TQSv2Config,
) -> None:
    """
    Enrich contracts with IV term structure data.
    Groups by (symbol, exchange) to avoid repeat API calls.
    Attaches iv_term_slope, iv_1w, iv_1m, iv_3m as attributes.
    """
    seen: dict[tuple[str, str], dict] = {}

    for c in contracts:
        key = (c.root, c.exchange)
        if key not in seen:
            enrichment = iv_enricher.enrich(c.root, c.exchange, cfg)
            seen[key] = enrichment

        enrichment = seen[key]
        if enrichment:
            # Attach as instance attributes (duck-typing onto RawContract)
            # These are consumed by IVScorer.score_detailed()
            c.iv_term_slope = enrichment.get("iv_term_slope", 0.0)
            c.iv_1w = enrichment.get("iv_1w")
            c.iv_2w = enrichment.get("iv_2w")  # catalyst mode: IV hump signal
            c.iv_1m = enrichment.get("iv_1m")
            c.iv_3m = enrichment.get("iv_3m")
            # TASK 5ii: multi-slope + contango score
            c.slope_3m_1m = enrichment.get("slope_3m_1m")
            c.slope_6m_3m = enrichment.get("slope_6m_3m")
            c.iv_contango_score = enrichment.get("iv_contango_score", 50.0)
            c.curve_shape_score = enrichment.get("curve_shape_score", 100.0)


def _apply_vol_enrichment(
    contracts: list[RawContract],
    voi_lens: TVVoiLens,
    vol_chart: TVVolatilityChart,
    cfg: TQSv2Config,
) -> None:
    """
    Enrich contracts with voi-lens PCR and volatility-chart 25D skew.

    Parallelised (2026-05-23): pre-collects all unique
    (root, exchange) and (root, exchange, expiry) keys, then fetches all
    via a single ThreadPoolExecutor instead of the original serial loop.
    With cfg.max_concurrent_vol_enrichment=8 workers this reduces ~20 min
    of sequential fetches (493 calls × 2.5 s each) to ~2 min.

    SD-004 compliance: uses _ex.shutdown(wait=False) — never `with` +
    fut.result(timeout=N) which would block on TimeoutError.

    Attaches:
      voi_pcr       – aggregate put/call volume ratio from voi-lens
      iv_skew_25d   – 25D put IV minus 25D call IV from volatility-chart
      iv_skew_score – normalised skew quality score (0-100)
    """
    # ── STEP 1: Collect unique keys and first-seen spot prices ───────────────
    pcr_keys: dict[tuple[str, str], None] = {}
    skew_keys: dict[tuple[str, str, str], float | None] = {}  # key → spot_price

    for c in contracts:
        pcr_keys.setdefault((c.root, c.exchange), None)
        smile_key = (c.root, c.exchange, str(c.expiration))
        if smile_key not in skew_keys:
            skew_keys[smile_key] = c.spot_price

    n_pcr, n_skew = len(pcr_keys), len(skew_keys)
    workers = getattr(cfg, "max_concurrent_vol_enrichment", 8)
    _log.info(
        "Stage 4b: pre-fetching %d PCR + %d skew calls (%d workers) ...",
        n_pcr, n_skew, workers,
    )

    seen_pcr: dict[tuple[str, str], float | None] = {}
    seen_skew: dict[tuple[str, str, str], tuple[float | None, float | None]] = {}

    # ── STEP 2: Submit all calls to a single thread pool ────────────────────
    # SD-004: never `with ThreadPoolExecutor() as ex` — use explicit shutdown(wait=False)
    _ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)

    all_futures: dict[concurrent.futures.Future, tuple] = {}
    for root, exchange in pcr_keys:
        fut = _ex.submit(voi_lens.fetch, root, exchange, cfg)
        all_futures[fut] = ("pcr", root, exchange)

    for (root, exchange, expiry_str), spot in skew_keys.items():
        fut = _ex.submit(
            vol_chart.fetch_smile,
            root, exchange, int(expiry_str), cfg,
            spot_price=spot,
        )
        all_futures[fut] = ("skew", root, exchange, expiry_str)

    # ── STEP 3: Collect results (generous timeout: 5 min or 2× sequential) ──
    timeout_secs = max(300, (n_pcr + n_skew) * 2)
    completed = 0
    try:
        for fut in concurrent.futures.as_completed(all_futures, timeout=timeout_secs):
            info = all_futures[fut]
            if info[0] == "pcr":
                _, root, exchange = info
                key: tuple = (root, exchange)
                try:
                    seen_pcr[key] = _compute_aggregate_pcr(fut.result())
                except Exception as exc:
                    _log.debug("voi-lens failed for %s: %s", root, exc)
                    seen_pcr[key] = None
            else:  # "skew"
                _, root, exchange, expiry_str = info
                key = (root, exchange, expiry_str)
                try:
                    smile = fut.result()
                    seen_skew[key] = (
                        smile.get("skew") if smile else None,
                        smile.get("skew_score") if smile else None,
                    )
                except Exception as exc:
                    _log.debug("vol-chart failed for %s/%s: %s", root, expiry_str, exc)
                    seen_skew[key] = (None, None)
            completed += 1
            if completed % 50 == 0:
                _log.info("Stage 4b: %d/%d done", completed, n_pcr + n_skew)
    except concurrent.futures.TimeoutError:
        _log.warning(
            "Stage 4b: timed out after %gs — %d/%d fetched; filling rest with None",
            timeout_secs, completed, n_pcr + n_skew,
        )
        for info in all_futures.values():
            if info[0] == "pcr":
                seen_pcr.setdefault((info[1], info[2]), None)
            else:
                seen_skew.setdefault((info[1], info[2], info[3]), (None, None))
    finally:
        _ex.shutdown(wait=False)  # SD-004: never block after TimeoutError

    _log.info(
        "Stage 4b done — PCR hits: %d/%d, skew hits: %d/%d",
        sum(1 for v in seen_pcr.values() if v is not None),
        n_pcr,
        sum(1 for v in seen_skew.values() if v is not None and v[0] is not None),
        n_skew,
    )

    # ── STEP 4: Apply pre-fetched results to every contract ─────────────────
    for c in contracts:
        c.voi_pcr = seen_pcr.get((c.root, c.exchange))
        smile_key = (c.root, c.exchange, str(c.expiration))
        skew = seen_skew.get(smile_key, (None, None))
        c.iv_skew_25d = skew[0]
        c.iv_skew_score = skew[1]


def _compute_aggregate_pcr(voi_data: dict) -> float | None:
    """
    Aggregate put/call volume ratio across all expirations in voi-lens data.

    TASK 5v: Fixed to match the actual TVVoiLens.fetch() return structure::

        {exp_int: {strike_float: {"call_vol": N, "put_vol": N}}}

    Returns None if call volume is zero (prevents division by zero).
    Clamps result to [0.1, 5.0].
    """
    total_call: float = 0.0
    total_put: float = 0.0

    for exp_data in voi_data.values():
        if isinstance(exp_data, dict):
            for strike_data in exp_data.values():
                if isinstance(strike_data, dict):
                    total_call += strike_data.get("call_vol", 0) or 0
                    total_put += strike_data.get("put_vol", 0) or 0

    if total_call == 0:
        return None

    return max(0.1, min(5.0, total_put / total_call))
