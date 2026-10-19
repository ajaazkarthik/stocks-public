"""
TQS v2 Configuration
====================
All tuneable constants in one place. No magic numbers elsewhere.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


# ── Root paths ──────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_TRADINGVIEW_DIR = _THIS_DIR.parent.parent   # tradingview/
_ROOT_DIR = _TRADINGVIEW_DIR.parent           # workspace root


@dataclass(frozen=True)
class TQSv2Config:
    # ── API endpoints ────────────────────────────────────────────────────────
    tv_stock_scan_url: str = "https://scanner.tradingview.com/america/scan"
    tv_options_scan2_url: str = "https://scanner.tradingview.com/options/scan2"
    tv_global_scan2_url: str = "https://scanner.tradingview.com/global/scan2"
    tv_in_time_iv_base: str = "https://options-charting.tradingview.com/v1/in-time-iv"
    tv_voi_lens_base: str = "https://options-charting.tradingview.com/v1/voi-lens"
    tv_volatility_chart_base: str = "https://options-charting.tradingview.com/v1/volatility-chart"
    tv_websocket_url: str = (
        "wss://data.tradingview.com/socket.io/websocket"
        "?&type=chart&from=options_chain&label-product=options-builder"
    )
    tv_scan2_label_product: str = "options-builder"

    # ── Cookie path ──────────────────────────────────────────────────────────
    cookie_path: Path = field(default_factory=lambda: _TRADINGVIEW_DIR / "cookie")

    # ── Stock universe filters ────────────────────────────────────────────────
    stock_exchanges: tuple[str, ...] = ("NYSE", "NASDAQ")
    stock_min_market_cap: float = 1_000_000_000    # $1B
    stock_min_price: float = 10.0
    stock_min_volume: float = 500_000
    stock_max_results: int = 300

    # ── Stock universe: technical momentum filters ────────────────────────────
    #  Default: market_cap_basic — always returns results (even on weekends).
    #  Use relative_volume_10d_calc for momentum mode BUT keep stock_min_relative_volume=0.0
    #  because TV reports RV=0 when market is closed → min filter kills all stocks on weekends.
    stock_sort_by: str = "market_cap_basic"          # Sort by market cap (most stable default)
    stock_min_relative_volume: float = 0.0           # 0.0 = disabled (never filter by RV — sorts fine without it)
    stock_rsi_min: float = 0.0                       # RSI lower bound filter (0.0 = disabled)
    stock_rsi_max: float = 100.0                     # RSI upper bound filter (100.0 = disabled)
    stock_max_price: float = 0.0                     # Max stock close price (0.0 = no limit)

    # ── Contract pre-filters ─────────────────────────────────────────────────
    contract_min_dte: int = 21             # compromise 13→21: broader universe while avoiding heavy gamma decay [2026-05-29]
    contract_max_dte: int = 60
    contract_moneyness_pct_band: float = 20.0     # ±20% of spot
    contract_min_bid: float = 0.10
    contract_max_premium: float = 8.00            # max ask price (budget cap) — e.g. $8 = max $800/contract. Fix 2026-05-27 (C-2): $4 forced OTM-only on $80+ stocks
    contract_max_spread_pct: float = 20.0          # widened 12→20%: $1-4 options naturally have wider spreads; 12% rejected 344/612 survivors [2026-05-29]

    # ── Delta filters (per option type) ──────────────────────────────────────
    call_min_delta: float = 0.10
    call_max_delta: float = 0.60
    put_min_delta: float = -0.60
    put_max_delta: float = -0.10

    # ── SMA200 alignment hard gate ──────────────────────────────────────────
    #  Calls more than X% below SMA200 → REJECT (bearish trend)
    #  Puts more than X% above SMA200 → REJECT (strong uptrend)
    #  [Added 2026-05-27 — TaskD BLOCKING: soft -20 scoring penalty only gave 2.4 composite pts]
    call_sma200_reject_below_pct: float = 0.03   # 3% below SMA200 → reject CALL
    put_sma200_reject_above_pct: float = 0.10    # 10% above SMA200 → reject PUT (widened 5→10%: bull market kills all puts at 5%)

    # ── OI / volume filter (yfinance) ───────────────────────────────────────
    min_open_interest: int = 500   # lowered from 1500: too aggressive, killed 99% of contracts [2026-05-29]
    min_option_volume: int = 10    # lowered from 50: too aggressive with $4 premium cap [2026-05-29]

    # ── Earnings proximity filter ────────────────────────────────────────────
    earnings_buffer_days: int = 21         # reject contracts if earnings within N days of expiry (IV crush risk) [2026-05-28]

    # ── Catalyst mode ────────────────────────────────────────────────────────
    catalyst_mode: bool = False                   # activates catalyst scoring + pipeline changes
    catalyst_max_picks: int = 10                  # max top-N for catalyst scan
    catalyst_earnings_window_days: int = 21        # keep stocks with earnings within this window
    catalyst_min_rel_volume: float = 1.5          # catalyst stocks need elevated activity
    catalyst_price_min_pct_change: float = 0.0    # min recent % move (0=disabled)

    # ── Scoring thresholds ───────────────────────────────────────────────────
    min_score_for_report: float = 55.0   # lowered 60→55: with $4 premium cap, need wider net; user filters by grade in report [2026-05-29]
    pop_score_min: float = 10.0          # Min pop_score sub-score = abs(delta)*100 (must match call_min_delta*100)
    top_n_per_stock: int = 3
    top_n_global: int = 20
    top_n_per_expiry: int = 15  # CRITICAL-4 fix 2026-05-26: max contracts per expiry date (expiry diversification)
                                # Raised 5→15 (2026-05-28): with 25 scored all on one OPEX, cap of 5 → only 3 final
    max_per_sector: int = 10    # M-3 fix 2026-05-27: sector diversification. Raised 3→5→10 (2026-05-28): user wants top 10
                                # Raised 3→5 (2026-05-28): allow top 10 final results across sectors

    # ── HTTP settings ─────────────────────────────────────────────────────────
    http_timeout: float = 30.0
    http_max_retries: int = 3
    http_retry_backoff: float = 2.0      # seconds

    # ── Rate limiting ─────────────────────────────────────────────────────────
    scan2_delay_sec: float = 0.5         # delay between per-stock scan2 calls
    yfinance_delay_sec: float = 0.3      # delay between yfinance calls

    # ── ThreadPoolExecutor ────────────────────────────────────────────────────
    max_concurrent_scan2: int = 8        # parallel Stage-2 TV scan2 fetches (2026-05-23)
    max_concurrent_yfinance: int = 5
    max_concurrent_vol_enrichment: int = 8  # parallel Stage-4b PCR + skew fetches
    fetch_timeout_sec: float = 30.0      # timeout per external call

    # ── Output paths ──────────────────────────────────────────────────────────
    reports_dir: Path = field(default_factory=lambda: _ROOT_DIR / "reports")
    database_dir: Path = field(default_factory=lambda: _ROOT_DIR / "database")
    logs_dir: Path = field(default_factory=lambda: _ROOT_DIR / "logs" / "tqs_v2")

    call_html_report: str = "tqs_v2_calls.html"
    call_json_report: str = "tqs_v2_calls.json"
    put_html_report: str = "tqs_v2_puts.html"
    put_json_report: str = "tqs_v2_puts.json"
    database_name: str = "tqs_v2.db"
    log_name: str = "tqs_v2"

    # ── Preset identification (metadata only) ────────────────────────────────
    preset_name: str = ""    # e.g. "catalyst_calls"
    preset_option_type: str = ""  # e.g. "call" or "put"

    # ── Feature flags ─────────────────────────────────────────────────────────
    enable_iv_term_structure: bool = True    # fetch IV term structure from options-charting
    enable_websocket_enrichment: bool = True   # TV WS provides last-session volume when scan2=None  # OPRA WS (requires websocket-client pkg)
    enable_vol_enrichment: bool = True       # fetch voi-lens PCR + volatility-chart skew
    circuit_breaker_max_consecutive: int = 5  # abort scan after N consecutive fetch failures

    # ── Options scan2 fields to request ──────────────────────────────────────
    scan2_columns: tuple[str, ...] = (
        "ask", "bid", "iv", "delta", "gamma", "theta", "vega", "rho",
        "bid_iv", "ask_iv", "expiration", "strike", "option-type",
        "volume", "theoPrice", "underlying_symbol", "update_time",
        "atm", "root", "pricescale", "currency", "change", "change_abs", "minmov",
    )

    # ── Stock scan fields to request ─────────────────────────────────────────
    stock_scan_columns: tuple[str, ...] = (
        "name", "close", "change", "volume", "market_cap_basic",
        "relative_volume_10d_calc", "RSI", "SMA200",
        # Technical momentum — CONFIRMED STABLE on america/scan (2026-05-23)
        "SMA50", "MACD.macd", "MACD.signal", "ATR", "ADX",
        "ADX+DI", "ADX-DI",  # Gap 1: DI+/DI- polarity for directional confirmation
        "Perf.W", "Perf.1M",
        "earnings_release_next_date",   # days until next earnings (for IV crush gate)
        "price_earnings_ttm", "return_on_equity", "debt_to_equity",
        "recommendation_mark",
        "sector", "industry", "gross_margin", "operating_margin",
        "current_ratio", "earnings_per_share_diluted_ttm",
        "earnings_per_share_diluted_yoy_growth_ttm",
    )


# ── Singleton default config ──────────────────────────────────────────────────
DEFAULT_CONFIG = TQSv2Config()
