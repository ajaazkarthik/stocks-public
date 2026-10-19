"""
AnalysisEngine — Common analysis logic (L1–L4 scoring, TV screener, fundamentals, HTML).

This is the shared "service layer" used by both BatchAnalyzer and WebhookServer.
Think of it like a Spring @Service bean — stateless, inject into both consumers.

Layers:
  L1  Technical       — WIN strategy metrics + TV screener relative volume
  L2  Fundamental     — Valuation, profitability, growth, balance sheet (TV screener)
  L3  Sentiment       — News VADER, analyst consensus, earnings history
  L4  Market Internals — Options PCR, insider activity, 52W position
"""

from __future__ import annotations

import sys
import math
from pathlib import Path
from datetime import datetime, timezone

# ── Add parent directory (tradingview/) so signal_stack.py is importable ──────
_PARENT = Path(__file__).parent.parent
_ROOT   = _PARENT.parent                          # project root (stocks/)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from path_config import ensure_paths
ensure_paths(_PARENT)

import signal_stack as _ss  # noqa: E402 (after path setup)

try:
    from tv_screener_fundamentals import (
        fetch_tv_fundamentals      as _tv_fund_fetch,
        tv_fund_to_compat          as _tv_fund_compat,
        fetch_yield_curve          as _fetch_yield_curve,
        fetch_vix_level            as _fetch_vix_level,
        fetch_sector_etf_momentum  as _fetch_sector_etf_momentum,
    )
    _TV_FUND_OK = True
except ImportError:
    _TV_FUND_OK = False
    def _tv_fund_fetch(syms, **kw): return {}          # type: ignore[misc]
    def _tv_fund_compat(d, **kw): return d             # type: ignore[misc]
    def _fetch_yield_curve(): return {}                # type: ignore[misc]
    def _fetch_vix_level(): return None                # type: ignore[misc]
    def _fetch_sector_etf_momentum(): return {}        # type: ignore[misc]

try:
    import yfinance as yf
except ImportError as exc:
    raise ImportError("pip install yfinance") from exc


# ─────────────────────────────────────────────────────────────────────────────
class AnalysisEngine:
    """
    Stateless analysis engine for the Signal Stack pipeline.

    All methods delegate to the parent module functions (signal_stack.py,
    tv_screener_fundamentals.py) — no logic is duplicated here.

    Example (Java analogy — imagine @Autowired AnalysisEngine engine):
        engine = AnalysisEngine()
        result = engine.analyse_signal(fire_dict)
        print(result["composite"])   # 0–100
    """

    # ─── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def tv_to_yf(pro_symbol: str) -> str:
        """Convert TV pro_symbol like NSE:TITAN → yfinance ticker TITAN.NS."""
        return _ss.tv_to_yf(pro_symbol)

    @staticmethod
    def parse_fire_meta(message: str) -> dict:
        """Extract WinRate, NetProfit%, RSI, ADX, sqzOff, trigger_price from alert message."""
        return _ss.parse_fire_meta(message)

    @staticmethod
    def resolve_etf_underlying(tv_sym: str) -> tuple[str | None, str | None]:
        """Return (underlying_ticker, underlying_tv_sym) for leveraged ETFs."""
        return _ss.resolve_etf_underlying(tv_sym)

    @staticmethod
    def composite(l1: float, l2: float, l3: float, l4: float,
                  l5: float | None = None, l6: float | None = None) -> float:
        """Weighted composite score (0–100). L1-L4 baseline; L5/L6 add optional layers."""
        return _ss.composite(l1, l2, l3, l4, l5, l6)

    @staticmethod
    def grade(score: float) -> tuple[str, str]:
        """Return (letter, hex_color) for a 0–100 composite score."""
        return _ss.grade(score)

    @staticmethod
    def build_html(ranked: list, n: int, title: str = "Signal Analysis") -> str:
        """Render the full ranked-signals HTML report string."""
        return _ss.build_html(ranked, n, title)

    # ─── L1 ────────────────────────────────────────────────────────────────────

    def score_technical(self, meta: dict, screener: dict | None = None) -> dict:
        """
        L1 — Technical quality score (0–10).
        Weights: WinRate(35–40%) + NetProfit%(20–25%) + RSI(15%) +
                 Squeeze(10%) + ADX(10%) + RelVol(10% when available).
        """
        return _ss.score_technical(meta, screener)

    # ─── L2 ────────────────────────────────────────────────────────────────────

    def score_fundamental(self, fund: dict) -> dict:
        """
        L2 — Fundamental quality score (0–10).
        Sub-scores: Valuation(25%) + Profitability(30%) + Growth(25%) + Balance(20%).
        """
        return _ss.score_fundamental(fund)

    def fetch_tv_screener(self, symbols: list[str]) -> dict:
        """
        Batch-fetch real-time TV screener data for given symbols.
        Returns {pro_symbol: {rel_vol, recommendation, close, ...}}.
        """
        return _ss.fetch_tv_screener(symbols)

    def fetch_tv_fundamentals(self, symbols: list[str],
                              use_auth: bool = False) -> dict:
        """
        Batch-fetch fundamentals from TV screener (one call per exchange group).
        Much faster than per-symbol yfinance .info() calls.
        Returns {pro_symbol: raw_tv_fields_dict}.
        """
        return _tv_fund_fetch(symbols, use_auth=use_auth)

    def tv_fund_to_compat(self, tv_fund: dict, pro_symbol: str = "",
                          current_price: float | None = None) -> dict:
        """
        Map raw TV screener fields to the canonical fund dict the scoring
        functions expect (forward_pe, roe, profit_margin, revenue_growth, …).
        Pass pro_symbol so currency is inferred from the exchange prefix.
        Pass current_price to inject live price into the returned fund dict.
        """
        return _tv_fund_compat(tv_fund, pro_symbol=pro_symbol,
                               current_price=current_price)

    # ─── L3 ────────────────────────────────────────────────────────────────────

    def score_sentiment(self, sent: dict) -> dict:
        """L3 — Sentiment + analyst score (0–10)."""
        return _ss.score_sentiment(sent)

    def fetch_sentiment_data(self, ticker_obj, company_name: str,
                             tv_symbol: str,
                             tv_fund: dict | None = None) -> dict:
        """
        Fetch news VADER sentiment, analyst consensus, upgrades/downgrades,
        earnings beat rate, earnings calendar risk for a symbol.
        ticker_obj — yfinance Ticker instance for the underlying security.
        """
        return _ss.fetch_sentiment_data(ticker_obj, company_name, tv_symbol,
                                        tv_fund=tv_fund)

    # ─── L4 ────────────────────────────────────────────────────────────────────

    def score_market_internals(self, internals: dict,
                               yc: dict | None = None,
                               vix_level: float | None = None) -> dict:
        """L4 — Market internals score (0–10): options PCR, insider, 52W position, VIX."""
        return _ss.score_market_internals(internals, yc, vix_level)

    def fetch_market_internals(self, ticker_obj, fund: dict,
                               tv_symbol: str = "") -> dict:
        """Fetch options chain PCR, insider transactions, 52W price position."""
        return _ss.fetch_market_internals(ticker_obj, fund, tv_symbol)

    def fetch_yield_curve(self) -> dict:
        """Return US 2s10s yield spread (macro regime indicator for L4)."""
        return _fetch_yield_curve()

    def fetch_vix_level(self) -> float | None:
        """Return current VIX level (market fear/greed regime, cached 30 min)."""
        return _fetch_vix_level()

    def fetch_sector_etf_momentum(self) -> dict[str, float]:
        """Return {sector_name: score_0_10} for 11 SPDR sector ETFs vs SMA-200 (cached 30 min)."""
        return _fetch_sector_etf_momentum()

    # ─── Full pipeline ─────────────────────────────────────────────────────────

    def analyse_signal(self, fire: dict,
                       screener_cache: dict | None = None,
                       tv_fund_cache: dict | None = None,
                       yc: dict | None = None,
                       use_auth: bool = False) -> dict:
        """
        Run the complete L1–L4 analysis pipeline on a single fire event.

        Accepts optional pre-fetched caches (screener_cache, tv_fund_cache, yc)
        so BatchAnalyzer can batch-fetch once for all symbols and reuse.
        When caches are None, this method fetches individually (used by webhooks).

        use_auth: pass True to use the TV session cookie for richer fundamental
        data (same quality as BatchAnalyzer). Defaults to False for backward
        compatibility with webhooks and tests that have no session cookie.

        Returns a result dict compatible with build_html() and the actionable log.
        """
        tv_sym  = fire.get("pro_symbol") or fire.get("symbol", "")
        yf_tick = self.tv_to_yf(tv_sym)
        meta    = self.parse_fire_meta(fire.get("message", ""))

        # ── ETF underlying resolution ─────────────────────────────────────────
        underlying_base, underlying_tv = self.resolve_etf_underlying(tv_sym)
        if underlying_base == "INDEX":
            is_etf, etf_label = True, "Index ETF"
            score_tv, score_yf = tv_sym, yf_tick
        elif underlying_base and underlying_tv:
            is_etf, etf_label = True, f"via {underlying_base}"
            score_tv, score_yf = underlying_tv, underlying_base
        else:
            is_etf, etf_label = False, ""
            score_tv, score_yf = tv_sym, yf_tick

        # ── L1: Technical ─────────────────────────────────────────────────────
        if screener_cache is None:
            screener_cache = self.fetch_tv_screener([tv_sym])
        l1s = self.score_technical(meta, screener=screener_cache.get(tv_sym))

        # ── L2: Fundamentals ──────────────────────────────────────────────────
        if tv_fund_cache is None:
            raw_cache = self.fetch_tv_fundamentals([score_tv], use_auth=use_auth)
        else:
            raw_cache = tv_fund_cache
        # Use TV screener close as the authoritative price — it covers all exchanges
        # (futures, XETR, EURONEXT, BMFBOVESPA, TASE, etc.) unlike yfinance.
        screener_close = (screener_cache or {}).get(tv_sym, {}).get("close")
        fund = self.tv_fund_to_compat(raw_cache.get(score_tv, {}),
                                      current_price=screener_close,
                                      pro_symbol=score_tv)
        # Fallbacks when TV screener has no data for this stock
        if not fund.get("current_price"):
            fund["current_price"] = meta.get("trigger_price")
        if not fund.get("currency"):
            exch = score_tv.split(":")[0].upper() if ":" in score_tv else ""
            fund["currency"] = {
                "NSE": "INR", "BSE": "INR", "ASX": "AUD",
                "SGX": "SGD", "HKEX": "HKD",
            }.get(exch, "USD")
        if not fund.get("company_name"):
            fund["company_name"] = fund.get("description", score_yf)
        l2s = self.score_fundamental(fund)

        # ── L3: Sentiment ─────────────────────────────────────────────────────
        ticker_obj   = yf.Ticker(score_yf)
        company_name = fund.get("company_name") or score_yf
        sent = self.fetch_sentiment_data(
            ticker_obj,
            company_name,
            score_tv,
            tv_fund=raw_cache.get(score_tv, {}),
        )
        l3s  = self.score_sentiment(sent)

        # ── L4: Market internals ──────────────────────────────────────────────
        if yc is None:
            yc = self.fetch_yield_curve()
        vix_level        = self.fetch_vix_level()
        sector_etf_scores = self.fetch_sector_etf_momentum()
        ints = self.fetch_market_internals(ticker_obj, fund, score_tv)
        ints["sector_etf_score"] = sector_etf_scores.get(fund.get("sector"))
        l4s  = self.score_market_internals(ints, yc, vix_level)

        comp = self.composite(l1s["total"], l2s["total"],
                              l3s["total"], l4s["total"])
        return {
            "tv_symbol":        tv_sym,
            "yf_ticker":        yf_tick,
            "fire_time":        fire.get("fire_time", ""),
            "resolution":       fire.get("resolution", ""),
            "meta":             meta,
            "fundamentals":     fund,
            "sentiment_data":   sent,
            "market_internals": ints,
            "l5_enrichment":    None,
            "l6_enrichment":    None,
            "yield_curve":      yc,
            "scores":           {"l1": l1s, "l2": l2s, "l3": l3s, "l4": l4s},
            "composite":        comp,
            "composite_raw":    comp,
            "in_pool":          None,
            "pool_boost":       0.0,
            "is_etf":           is_etf,
            "etf_label":        etf_label,
        }

    def evaluate_option_position(self, position: dict) -> dict:
        """
        Evaluate strategies for an existing option holding.

        Delegates to OptionPositionManager — usable pre-trade (structuring)
        or post-trade (manage, roll, hedge).

        Parameters
        ----------
        position : dict
            Required: symbol, option_type, strike, expiry,
                      entry_premium, current_premium, underlying_price.
            Optional: quantity, contract_size, iv, rsi, adx, vix.

        Returns
        -------
        dict with primary, alternative, all_strategies, action_plan, summary, warnings.
        """
        try:
            from path_config import ensure_paths as _ep
            _opts = Path(__file__).parent.parent / "options"
            _ep(_opts.parent)
            from options.option_position_manager import OptionPositionManager
            return OptionPositionManager().evaluate(position)
        except Exception as exc:
            return {
                "error":          str(exc),
                "primary":        None,
                "alternative":    None,
                "all_strategies": [],
                "summary":        f"OptionPositionManager unavailable: {exc}",
                "warnings":       [],
            }
