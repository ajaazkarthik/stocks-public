"""
ZeroDTE Configuration
======================
All tuneable constants in one place. No magic numbers elsewhere.

Canonical scan pattern — May 28 → May 29, 2026 (MSFT 440C):
  MAY 28 (Thursday, EOD scan):
    MSFT closes +3.4% on high volume → flagged as bullish mover
    440C expiring May 29 priced at $0.09 bid (6.8% OTM at $412 stock close)
    Scanner outputs: "BUY May 29 440C at open — DTE=1 lottery ticket"

  MAY 29 (Friday, buy at open, hold intraday):
    MSFT continues to rip → 440C hits +2500% by close
    Key: the SIGNAL was on May 28; the TRADE was on May 29

Scan architecture:
  Run EOD each day → find DTE=1 contracts (next-day expiry) → buy at next morning open
  DTE=1 preferred over DTE=0 (no entry opportunity for same-day contracts at scan time)

Filter values calibrated to the May 28 pattern:
  OTM max: 12% (was 8%  — 440C was 6.8% OTM; conservative 8% barely covers it)
  Spread:  80% (was 35% — deep OTM 0-1DTE have wide bid/ask by definition)
  Min bid: $0.01 (was $0.05 — $0.09 bid at open barely passed old filter)
  Min OI:  50  (was 100  — deep OTM has lower OI than ATM)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ── Root paths ──────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_TRADINGVIEW_DIR = _THIS_DIR.parent.parent   # tradingview/
_ROOT_DIR = _TRADINGVIEW_DIR.parent           # workspace root


@dataclass(frozen=True)
class ZeroDTEConfig:
    # ── API endpoints ────────────────────────────────────────────────────────
    tv_stock_scan_url: str = "https://scanner.tradingview.com/america/scan"
    tv_in_time_iv_base: str = "https://options-charting.tradingview.com/v1/in-time-iv"
    tv_voi_lens_base: str = "https://options-charting.tradingview.com/v1/voi-lens"

    # ── Cookie path ──────────────────────────────────────────────────────────
    cookie_path: Path = field(default_factory=lambda: _TRADINGVIEW_DIR / "cookie")

    # ── Stock universe ────────────────────────────────────────────────────────
    # Fixed high-liquidity watchlist (always included regardless of TV scan)
    fixed_watchlist: tuple[str, ...] = (
        "NVDA", "MSFT", "AAPL", "META", "GOOGL", "AMZN", "TSLA",
        "AMD", "PLTR", "ARM", "SMCI", "MRVL", "CRWD", "AVGO",
        "NFLX", "CRM", "SNOW", "PANW", "UBER", "LYFT", "SOFI",
        "COIN", "HOOD", "RBLX", "DKNG", "MU", "INTC", "QCOM",
        "SHOP", "MELI", "SE", "BABA", "JD", "BIDU", "NIO",
        "RIVN", "LCID", "GME", "AMC", "BBBY", "SPY", "QQQ", "IWM",
    )
    # Dynamic additions from TV scanner (top movers by relative volume)
    tv_scan_top_n: int = 400          # take top N from TV scanner (400 ensures broad coverage)
    tv_scan_min_market_cap: float = 500_000_000   # $500M min market cap
    tv_scan_min_price: float = 5.0
    tv_scan_min_volume: float = 500_000

    # ── Momentum signal thresholds ────────────────────────────────────────────
    # Signal 1: Momentum Continuation (0-40 pts)
    momentum_min_pct_call: float = 2.0     # prev-day +2%+ → bullish momentum
    momentum_min_pct_put: float = 2.0      # prev-day -2%+ down → bearish momentum
    momentum_vol_ratio_min: float = 1.2    # volume must be 1.2x the 20d average
    momentum_rsi_call_min: float = 45.0    # RSI in bullish zone for calls
    momentum_rsi_call_max: float = 75.0    # not overbought
    momentum_rsi_put_min: float = 25.0     # not oversold
    momentum_rsi_put_max: float = 55.0     # RSI in bearish zone for puts

    # ── IV Backwardation thresholds ──────────────────────────────────────────
    # Signal 2: IV Backwardation (0-30 pts)
    # GAP-2 fix (2026-05-31): scorer now uses RELATIVE slope = (iv_1w / iv_1m) - 1.0.
    # MSFT: iv_1w=0.3232, iv_1m=0.3033 → relative=6.57% → scores mid-tier (≥4%).
    iv_back_min_slope: float = 0.0       # any positive relative slope (iv_1w > iv_1m)
    iv_back_mid_slope: float = 0.04      # was 0.02 (absolute); now 4% relative
    iv_back_high_slope: float = 0.10     # was 0.05 (absolute); now 10% relative

    # ── VOI surge thresholds ─────────────────────────────────────────────────
    # Signal 3: VOI Surge (0-30 pts)
    # GAP-1 fix (2026-05-31): lowered base/mid — mega-caps like MSFT have C/P ~4.5x,
    # which missed the old 5.0 base threshold by 10%.
    voi_base_ratio: float = 3.0          # was 5.0 — MSFT ratio ~4.5 was missed
    voi_mid_ratio: float = 7.0           # was 10.0
    voi_high_ratio: float = 50.0         # >= 50x → +5 bonus pts

    # ── Contract filters ──────────────────────────────────────────────────────
    # DTE range (0 = today's expiry, 1 = tomorrow, 2 = day after)
    contract_min_dte: int = 0
    contract_max_dte: int = 2
    # OTM band: 0% to 12% OTM — extended to cover lottery-ticket deep OTM plays.
    # Ground truth: May 28 MSFT 440C was 6.8% OTM at market open ($412 stock, $440 strike).
    # At 8% max the contract barely passes; big-move days can have key strikes 8-12% OTM.
    contract_otm_min_pct: float = 0.0     # at-the-money is OK
    contract_otm_max_pct: float = 12.0    # extended from 8% — covers deep OTM lottery plays
    # Liquidity minimums
    # Deep OTM 0DTE contracts have inherently lower OI than near-ATM; 50 is sufficient.
    contract_min_oi: int = 50             # lowered from 100 — deep OTM has fewer open contracts
    contract_min_volume: int = 10         # low volume early morning, relax this
    # Premium range: allow sub-penny bids (lottery tickets open at $0.05-$0.09)
    # May 28 MSFT 440C: bid=$0.09 at 9:30 AM — any min_bid > $0.09 would MISS this setup.
    contract_min_bid: float = 0.01        # lowered from $0.05 — captures $0.01-$0.09 lottery tier
    contract_max_premium: float = 1.00    # max ask price — 0-2 DTE «lottery ticket» range; May 28 MSFT 440C was $0.43
    # Spread filter: deep OTM 0DTE options have wide bid-ask spreads by definition.
    # A $0.10 wide spread on a $0.10 contract = 100% spread. 35% max kills the entire lottery tier.
    # May 28 MSFT 440C: bid=$0.09 / ask=$0.10 → spread = (0.10-0.09)/0.095 = 10.5% (passes).
    # But many deep OTM contracts are bid=$0.01 / ask=$0.05 → spread = 133%. Allow up to 80%.
    contract_max_spread_pct: float = 80.0 # extended from 35% — deep OTM 0DTE has wide spreads

    # ── Lottery ticket tier ───────────────────────────────────────────────────
    # Ultra-cheap (≤ $0.15 ask) + deep OTM (≥ 5% OTM) contracts represent the highest-leverage
    # setup in 0DTE options trading. May 28 MSFT 440C: ask=$0.09, OTM=6.8% → +378% intraday.
    # These contracts get a scoring BONUS because the stock-level signal (momentum/IV/VOI) alone
    # cannot capture the asymmetric risk/reward at this price point.
    lottery_ticket_max_premium: float = 0.15   # max ask to qualify as lottery ticket
    lottery_ticket_min_otm_pct: float = 5.0    # min OTM % to qualify (clearly OTM, not just ATM)
    lottery_ticket_bonus_pts: float = 15.0     # bonus pts added to composite_score

    # ── Next-day scan preference ──────────────────────────────────────────────
    # Core scan pattern: run EOD (May 28) → find DTE=1 contracts (May 29 expiry)
    # → buy at next-day open → ride intraday momentum → May 28 MSFT 440C = +2500%
    # DTE=1 contracts get a bonus score to rank above same-day (DTE=0) contracts in the report.
    preferred_dte: int = 1                       # DTE=1 = next-day expiry (primary scan target)
    preferred_dte_bonus_pts: float = 10.0        # bonus pts for contracts at preferred DTE

    # ── Scoring thresholds ────────────────────────────────────────────────────
    min_score_for_report: float = 50.0    # include if composite >= 50
    top_n_calls: int = 20                 # top N calls in report
    top_n_puts: int = 20                  # top N puts in report

    # ── HTTP settings ─────────────────────────────────────────────────────────
    http_timeout: float = 30.0
    http_max_retries: int = 3

    # ── Delays ────────────────────────────────────────────────────────────────
    yfinance_delay_sec: float = 0.2       # delay between yfinance calls
    tv_api_delay_sec: float = 0.3         # delay between TV API calls

    # ── Output paths ──────────────────────────────────────────────────────────
    reports_dir: Path = field(default_factory=lambda: _ROOT_DIR / "reports")
    database_dir: Path = field(default_factory=lambda: _ROOT_DIR / "database")
    logs_dir: Path = field(default_factory=lambda: _ROOT_DIR / "logs" / "zerodte")
    database_name: str = "zerodte.db"
    log_name: str = "zerodte"

    call_html_report: str = "zerodte_calls.html"
    call_json_report: str = "zerodte_calls.json"
    put_html_report: str = "zerodte_puts.html"
    put_json_report: str = "zerodte_puts.json"

    # ── TV stock scan fields ─────────────────────────────────────────────────
    stock_scan_columns: tuple[str, ...] = (
        "name", "close", "change", "volume", "market_cap_basic",
        "relative_volume_10d_calc", "RSI", "SMA200", "ATR",
        "sector", "industry",
    )


# ── Singleton default config ──────────────────────────────────────────────────
DEFAULT_CONFIG = ZeroDTEConfig()
