"""
BatchAnalyzer — Reads all fired alerts from fires_raw.json, ranks them
using the AnalysisEngine (L1-L4 scoring), and generates the HTML report.

Java analogy: think of this as a @Service class that has a run() method you
call like a batch job — reads input, processes each record, writes output.

Flow:
  1. load_fires()           → read fires_raw.json, deduplicate, select top-N
  2. run()                  → batch-fetch TV data, call engine per fire, rank
  3. save_report()          → write HTML + optional actionable-subset HTML
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .analysis_engine import AnalysisEngine

_PARENT = Path(__file__).parent.parent
_ROOT   = _PARENT.parent                          # project root (stocks/)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from path_config import ensure_paths
ensure_paths(_PARENT)
import signal_stack as _ss  # noqa: E402

try:
    from tradability_filter import TradabilityFilter as _TradabilityFilter
    _TRADABILITY_OK = True
except ImportError:
    _TRADABILITY_OK = False
    _TradabilityFilter = None  # type: ignore

try:
    from tv_data.tv_economic_calendar import (
        fetch_economic_events as _fetch_econ_events,
        build_calendar_html_fragment as _build_econ_html,
    )
    _ECON_CAL_OK = True
except Exception:
    _ECON_CAL_OK = False

class BatchAnalyzer:
    """
    Batch analysis pipeline: fires_raw.json → ranked HTML report.

    Parameters
    ----------
    engine : AnalysisEngine
        Shared analysis engine (scoring + fetching).
    fires_file : Path, optional
        Path to fires_raw.json. Defaults to ../../fires_raw.json relative to
        this file (i.e. tradingview/fires_raw.json).
    n : int
        Maximum number of unique symbols to analyse (default 50).
    threshold : int
        Composite ≥ threshold → included in actionable report (default 50).
    deduplicate : bool
        When True (default), only the first fire per symbol is kept.
    """

    DEFAULT_FIRES_FILE = _PARENT / "fires_raw.json"
    DEFAULT_REPORT_OUT = _ROOT / "reports" / "signal_report_full_batch.html"

    # Tradability filter — instantiated lazily once per BatchAnalyzer
    _tradability_filter: object | None = None
    DEFAULT_ACTIONABLE = _ROOT / "reports" / "signal_report_actionable_batch.html"

    def __init__(
        self,
        engine: AnalysisEngine | None = None,
        fires_file: Path | None = None,
        n: int = 50,
        threshold: int = 50,
        deduplicate: bool = True,
    ) -> None:
        self.engine      = engine or AnalysisEngine()
        self.fires_file  = Path(fires_file) if fires_file else self.DEFAULT_FIRES_FILE
        self.n           = n
        self.threshold   = threshold
        self.deduplicate = deduplicate

    # ─── Public API ────────────────────────────────────────────────────────────

    def load_fires(self) -> list[dict]:
        """
        Read fires_raw.json, apply deduplication, return up to self.n fires.
        fires_raw.json has the shape: {"all": [...], "win2h": [...], ...}
        The "all" bucket is used.
        """
        with open(self.fires_file, encoding="utf-8") as fh:
            data = json.load(fh)
        all_fires = data.get("all", [])
        print(f"  Total fires in file: {len(all_fires)}")
        selected: list[dict] = []
        seen: set[str] = set()
        for fire in all_fires:
            sym = fire.get("pro_symbol") or fire.get("symbol", "")
            if self.deduplicate and sym in seen:
                continue
            seen.add(sym)
            selected.append(fire)
            if len(selected) >= self.n:
                break
        print(f"  Selected {len(selected)} fires (dedup={self.deduplicate})")
        return selected

    def run(self) -> list[dict]:
        """
        Execute the full batch analysis pipeline.

        Returns the ranked list of result dicts (highest composite first).
        Each result dict contains: tv_symbol, composite, scores, fundamentals, …
        """
        fires      = self.load_fires()
        symbols    = [f.get("pro_symbol") or f.get("symbol", "") for f in fires]

        # ── Batch-fetch shared data (one HTTP call per exchange group) ─────────
        print(f"Fetching TV screener (rel_vol) for {len(symbols)} symbols …", flush=True)
        screener_cache = self.engine.fetch_tv_screener(symbols)
        matched = sum(1 for v in screener_cache.values() if v.get("rel_vol") is not None)
        print(f"  Got rel_vol for {matched}/{len(symbols)} symbols")

        print(f"Fetching TV Screener fundamentals for {len(symbols)} symbols …", flush=True)
        tv_fund_cache = self.engine.fetch_tv_fundamentals(symbols, use_auth=True)
        print(f"  Got fundamentals for {len(tv_fund_cache)}/{len(symbols)} symbols")

        yc      = self.engine.fetch_yield_curve()
        spread  = yc.get("spread_bps")
        yc_str  = f"{spread:+.0f}bps" if spread is not None else "N/A"
        print(f"  Yield curve 2s10s: {yc_str}\n")

        # ── Per-fire analysis ─────────────────────────────────────────────────
        results: list[dict] = []
        for i, fire in enumerate(fires, 1):
            tv_sym = fire.get("pro_symbol") or fire.get("symbol", "")
            print(f"[{i}/{len(fires)}] {tv_sym}", flush=True)
            try:
                result = self.engine.analyse_signal(
                    fire,
                    screener_cache = screener_cache,
                    tv_fund_cache  = tv_fund_cache,
                    yc             = yc,
                )
                comp = result["composite"]
                sc   = result["scores"]
                print(
                    f"  Composite={comp:.1f}  "
                    f"L1={sc['l1']['total']:.1f}  L2={sc['l2']['total']:.1f}  "
                    f"L3={sc['l3']['total']:.1f}  L4={sc['l4']['total']:.1f}"
                )
                results.append(result)
            except Exception as exc:
                print(f"  ⚠️  ERROR: {exc}")
                traceback.print_exc()
            time.sleep(0.5)   # polite to free APIs

        ranked = sorted(results, key=lambda x: x["composite"], reverse=True)

        # ── Tradability filter ────────────────────────────────────────────────
        skipped_non_tradable: list[dict] = []
        if _TRADABILITY_OK and _TradabilityFilter is not None:
            if self._tradability_filter is None:
                self._tradability_filter = _TradabilityFilter()
            ranked, skipped_non_tradable = self._tradability_filter.filter_results(ranked)
            if skipped_non_tradable:
                print(f"\n  ⛔ {len(skipped_non_tradable)} symbol(s) filtered (not tradable via any broker):")
                for skip in skipped_non_tradable:
                    print(f"     SKIP {skip['symbol']}: {skip['reason']}")

        # Attach skipped list so callers (watchlist cleaner) can act on it
        self._last_skipped_non_tradable = skipped_non_tradable
        return ranked

    def save_report(
        self,
        ranked: list[dict],
        output_path: Path | None = None,
        title: str = "Signal Stack — Full Analysis",
    ) -> Path:
        """
        Write the full HTML report for all ranked signals.
        Returns the path where the report was written.
        """
        out = Path(output_path) if output_path else self.DEFAULT_REPORT_OUT
        html = self.engine.build_html(ranked, len(ranked), title=title)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            import sys as _s
            _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
            if _rpt not in _s.path: _s.path.insert(0, _rpt)
            from report_db import archive_before_write as _abw
            _abw(out)
        except Exception:
            pass
        out.write_text(html, encoding="utf-8")
        print(f"✅ Full report  → {out}")
        return out

    def save_actionable_report(
        self,
        ranked: list[dict],
        output_path: Path | None = None,
    ) -> tuple[Path, int]:
        """
        Write an actionable-only HTML report (composite ≥ self.threshold).
        Returns (path, count_of_actionable_signals).
        """
        out        = Path(output_path) if output_path else self.DEFAULT_ACTIONABLE
        actionable = [r for r in ranked if r["composite"] >= self.threshold]
        html       = self.engine.build_html(actionable, len(actionable),
                                            title="Actionable Signals")
        out.parent.mkdir(parents=True, exist_ok=True)
        now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        banner = (
            f'<div style="background:#16a34a;color:#fff;padding:10px 24px;'
            f'font-weight:600;font-size:13px">'
            f'✅ ACTIONABLE (≥{self.threshold}) — {len(actionable)} signal(s) | '
            f'Last updated: {now_str}</div>'
        )
        # Economic calendar warning bar (best-effort, non-blocking)
        econ_bar = ""
        if _ECON_CAL_OK:
            try:
                econ_events = _fetch_econ_events(days=7, min_impact=3)
                econ_bar = _build_econ_html(econ_events)
            except Exception:
                pass
        html = html.replace("<body>", "<body>" + banner + econ_bar, 1)
        try:
            import sys as _s
            _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
            if _rpt not in _s.path: _s.path.insert(0, _rpt)
            from report_db import archive_before_write as _abw
            _abw(out)
        except Exception:
            pass
        out.write_text(html, encoding="utf-8")
        print(f"✅ Actionable   → {out}  ({len(actionable)} signals above {self.threshold})")
        return out, len(actionable)

    def run_and_save(
        self,
        report_out: Path | None = None,
        actionable_out: Path | None = None,
    ) -> list[dict]:
        """
        Convenience method: run() then save both HTML reports.
        Returns the ranked list.
        """
        ranked = self.run()
        self.save_report(ranked, report_out)
        self.save_actionable_report(ranked, actionable_out)

        # ── Watchlist cleanup for skipped non-tradable symbols ────────────────
        skipped = getattr(self, "_last_skipped_non_tradable", [])
        if skipped:
            try:
                from watchlist_cleaner import WatchlistCleaner
                WatchlistCleaner().process_skipped(skipped)
            except ImportError:
                pass   # watchlist_cleaner optional

        return ranked
