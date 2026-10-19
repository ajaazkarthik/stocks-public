"""
options_hedger_global.py — Global broker hedge engine (US/AU/HK/SG) + Long option advisor.

Provides:
  _load_lev_map / _LEV_RATIO     — leveraged ETF underlying map
  _recommend_hedge_global        — global holding hedge rules (P&L threshold-based)
  _recommend_hedge_long_option   — long call/put position advisor (spread/roll/close)
  _get_market_outlook            — 90-day technical outlook (RSI, SMA, analyst)
  _compute_option_score          — 5-factor option quality score
  _rank_strategies_for_call      — ranked strategy list for quick-card UI
  _get_signal_score / _reset_score_provider / _prefetch_live_scores
"""
from __future__ import annotations

import pathlib as _pathlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog

from .options_hedger_constants import (
    BIG_GAIN_PCT,
    MODERATE_GAIN_PCT,
    LOSS_PCT,
    LARGE_POS_INR,
    US_OPTION_CONTRACT,
)
from .options_hedger_india import _next_expiry_us, _next_expiry_nse
from .options_hedger_quotes import _fetch_full_chain, _fetch_option_quote

ROOT = Path(__file__).parent.parent
_log = structlog.get_logger(__name__)


_LEV_MAP: dict[str, str] = {}

def _load_lev_map() -> dict[str, str]:

    """Load LEVERAGED_ETF_UNDERLYING from sell_actionable.py once at module level."""

    global _LEV_MAP

    if _LEV_MAP:

        return _LEV_MAP

    try:

        _sell_dir = ROOT / "sell"

        if str(_sell_dir) not in sys.path:

            sys.path.insert(0, str(_sell_dir))

        from sell_actionable import LEVERAGED_ETF_UNDERLYING  # type: ignore

        _LEV_MAP = dict(LEVERAGED_ETF_UNDERLYING)

    except Exception as exc:

        _log.warning("_load_lev_map: failed to import LEVERAGED_ETF_UNDERLYING", exc_info=exc)

    return _LEV_MAP


# Leverage multiplier per ETF symbol (2x, 3x, 1.5x, etc.)

# Derived from buy_advisor.py LEVERAGED_UNIVERSE and known product specs

_LEV_RATIO: dict[str, float] = {

    # Direxion 2x

    "AAPU": 1.5,  "ADBU": 2.0,  "AMUU": 2.0,  "AMZU": 1.5,  "ASMU": 2.0,

    "AVL":  2.0,  "BABU": 2.0,  "BOEU": 2.0,  "BRKU": 2.0,  "CONX": 2.0,

    "CSCL": 2.0,  "ELIL": 2.0,  "FRDU": 2.0,  "GGLL": 2.0,  "HODU": 2.0,

    "LINT": 2.0,  "LMTL": 2.0,  "METU": 2.0,  "MRVU": 2.0,  "MSFU": 2.0,

    "MUU":  2.0,  "NFXL": 2.0,  "NVDU": 1.5,  "ORCU": 2.0,  "PALU": 2.0,

    "PLTU": 2.0,  "PYPU": 2.0,  "QCMU": 2.0,  "SHPU": 2.0,  "SOFA": 2.0,

    "TSLL": 2.0,  "TSMX": 2.0,  "TXNU": 2.0,  "UNHU": 2.0,  "XOMX": 2.0,

    # Direxion Bear 1x inverse

    "AAPD": -1.0, "AMDD": -1.0, "AMZD": -1.0, "AVS": -1.0,  "BOED": -1.0,

    "BRKD": -1.0, "CSCS": -1.0, "ELIS": -1.0, "FRDD": -1.0, "GGLS": -1.0,

    "LMTS": -1.0, "METD": -1.0, "MSFD": -1.0, "MUD": -1.0,  "NFXS": -1.0,

    "NVDD": -1.0, "ORCS": -1.0, "PALD": -1.0, "PLTD": -1.0, "QCMD": -1.0,

    "SHPD": -1.0, "TSLS": -1.0, "TSMZ": -1.0, "XOMZ": -1.0,

    # GraniteShares

    "NVDL": 2.0,  "CONL": 2.0,  "SMCX": 2.0,  "GOGU": 2.0,  "AMDL": 2.0,

    "MULL": 2.0,

    # T-Rex / REX 2x

    "MSTU": 2.0,  "MSTX": 2.0,  "NVDX": 2.0,  "TSLT": 2.0,  "AAPX": 2.0,

    "GOOX": 2.0,  "MSFO": 2.0,  "MSFX": 2.0,  "PTIR": 2.0,  "OKLL": 2.0,

    "NFLU": 2.0,  "ROBN": 2.0,  "DKUP": 2.0,  "ARMU": 2.0,  "DJTU": 2.0,

    "RBLU": 2.0,  "GMEU": 2.0,  "SNOU": 2.0,  "SMUP": 2.0,  "AXUP": 2.0,

    "KTUP": 2.0,  "TTDU": 2.0,  "BKNU": 2.0,

    # T-Rex 2x Inverse

    "MSTZ": -2.0, "NVDQ": -2.0, "TSLZ": -2.0,

    # SGX DLCs (3x)

    "NVIW": 3.0,  "Z8TW": 3.0,  "Z4WW": 3.0,  "USDW": 3.0,  "USAW": 3.0,

    "NVDDU": 3.0, "AMDDU": 3.0, "TSLDBU": 3.0, "METDU": 3.0, "AAPDU": 3.0,

    "AMZDU": 3.0, "GGLDU": 3.0, "MSFDU": 3.0,

    # Broad-index leveraged (no single-stock underlying but leverage known)

    "SOXL": 3.0,  "TQQQ": 3.0,  "UPRO": 3.0,  "TECL": 3.0,  "CURE": 3.0,

    "LABU": 3.0,  "FNGU": 3.0,  "SSO": 2.0,   "QLD": 2.0,

    # APPX — T-Rex 2X Apple (AAPL)

    "APPX": 2.0,

}


def _recommend_hedge_global(

    holding: dict,

    big_gain_pct: float = BIG_GAIN_PCT,

    loss_pct:     float = LOSS_PCT,

) -> dict:

    """

    Rule-based hedge recommendation for non-India (global) broker holdings.

    Uses leveraged ETF map to identify underlying stocks and generate specific

    option contract recommendations with P&L scenario analysis.

    Returns same structure as _recommend_hedge() for consistent HTML rendering.

    """

    pnl_pct  = holding.get("combined_pnl_pct", 0)

    val      = holding.get("combined_val", 0)

    sym      = holding.get("symbol", "?")

    qty      = holding.get("total_qty", 0)

    avg      = holding.get("combined_avg", 0)

    ltp      = holding.get("combined_ltp", 0)

    exchange = holding.get("exchange", "US")

    currency = holding.get("currency", "USD")

    broker   = holding.get("broker", "")

    cur_sym  = {"AUD": "A$", "HKD": "HK$", "SGD": "S$", "USD": "$",

                "JPY": "¥", "GBP": "£"}.get(currency, currency + " ")

    # Strip exchange prefix (e.g. "SGX:NVIW" → "NVIW", "ASX:MND" → "MND")

    raw_ticker = sym.split(":")[-1].upper()

    # ── Leveraged ETF identification ──────────────────────────────────────────

    lev_map    = _load_lev_map()

    underlying = lev_map.get(raw_ticker)   # e.g. METU → "META"

    lev_ratio  = _LEV_RATIO.get(raw_ticker, 0.0)   # 0.0 = not a leveraged ETF

    is_lev_etf = bool(underlying and lev_ratio != 0.0)

    # ── Options venue guidance per market ─────────────────────────────────────

    if exchange in ("US", "") and is_lev_etf and underlying:

        # Use underlying's options (much more liquid than leveraged ETF options)

        hedge_ticker = underlying

        venue = f"{underlying} options via IBKR"

    elif exchange == "TYO":

        hedge_ticker = "EWJ"   # iShares MSCI Japan ETF (US-listed, has options)

        venue = "EWJ options via IBKR (Japan ETF proxy — TYO direct options illiquid for retail)"

    elif exchange == "ASX":

        hedge_ticker = raw_ticker

        venue = f"{raw_ticker} options via ASX / IBKR"

    elif exchange == "HKEX":

        hedge_ticker = raw_ticker

        venue = "HKEX options (via Moomoo)"

    elif exchange == "SGX":

        hedge_ticker = raw_ticker

        venue = "SGX options / DLC expiry (via Moomoo)"

    else:

        hedge_ticker = raw_ticker

        venue = "US options via IBKR"

    strategy  = "HOLD"

    urgency   = "LOW"

    rationale: list[str] = []

    details: dict = {}

    # ── Helper: effective exposure considering leverage ────────────────────────

    abs_lev   = abs(lev_ratio) if is_lev_etf else 1.0

    eff_qty   = int(qty * abs_lev)         # economic exposure in underlying shares

    eff_val   = val * abs_lev              # adjusted exposure value

    # Option lots for US contracts (1 lot = 100 shares)

    if exchange in ("US", "") or is_lev_etf:

        opt_lots = max(1, round(eff_qty / US_OPTION_CONTRACT))

    else:

        opt_lots = max(1, round(qty / 100))

    # ── Leveraged ETF context string ─────────────────────────────────────────

    lev_context = ""

    if is_lev_etf:

        direction = "Bull" if lev_ratio > 0 else "Bear"

        lev_context = (f"⚡ {sym} = {abs_lev:.0f}x {direction} on {underlying} "

                       f"(effective exposure: {cur_sym}{eff_val:,.0f})")

    # ── Expiry dates ─────────────────────────────────────────────────────────

    expiry = _next_expiry_us()   # 3rd Friday (US monthly options)

    # ── P&L scenario builder ─────────────────────────────────────────────────

    def _scenarios_html(strategy_type: str, option_strike: float, opt_premium: float) -> str:

        """Generate HTML P&L scenario table for the recommended option strategy."""

        if opt_premium <= 0 or option_strike <= 0 or ltp <= 0:

            return ""

        total_prem = opt_premium * opt_lots * US_OPTION_CONTRACT

        rows: list[tuple[str, str, str, str, str, str]] = []  # label,move,pos,opt,net,css

        if strategy_type == "COVERED_CALL":

            # Sold call: receive premium, upside capped at strike

            for label, pct, css in [("Normal / flat", 0.0, "sc-normal"),

                                     ("Bull (+10%)",   0.10, "sc-bull"),

                                     ("Bear (−10%)",  -0.10, "sc-bear"),

                                     ("Crash (−30%)", -0.30, "sc-crash")]:

                new_price   = ltp * (1 + pct)

                pos_pnl     = (new_price - ltp) * qty   # stock position move

                # Call expires OTM if new_price < strike → keep full premium

                # Call ITM if new_price > strike → give up gains above strike

                if new_price <= option_strike:

                    opt_pnl = total_prem   # keep premium

                else:

                    opt_pnl = -(new_price - option_strike) * opt_lots * US_OPTION_CONTRACT + total_prem

                net  = pos_pnl + opt_pnl

                rows.append((label, f"{pct:+.0%}", f"{cur_sym}{pos_pnl:+,.0f}",

                             f"{cur_sym}{opt_pnl:+,.0f}", f"{cur_sym}{net:+,.0f}", css))

        elif strategy_type == "PROTECTIVE_PUT":

            # Bought put: pay premium, downside floored below strike

            for label, pct, css in [("Bull (+15%)",   0.15, "sc-bull"),

                                     ("Normal (flat)", 0.0,  "sc-normal"),

                                     ("Bear (−10%)",  -0.10, "sc-bear"),

                                     ("Crash (−30%)", -0.30, "sc-crash")]:

                new_price   = ltp * (1 + pct)

                pos_pnl     = (new_price - ltp) * qty

                # Put pays if new_price < strike; otherwise expires worthless

                if new_price < option_strike:

                    opt_pnl = (option_strike - new_price) * opt_lots * US_OPTION_CONTRACT - total_prem

                else:

                    opt_pnl = -total_prem

                net  = pos_pnl + opt_pnl

                rows.append((label, f"{pct:+.0%}", f"{cur_sym}{pos_pnl:+,.0f}",

                             f"{cur_sym}{opt_pnl:+,.0f}", f"{cur_sym}{net:+,.0f}", css))

        elif strategy_type == "COLLAR":

            # Buy put + sell call; net premium ≈ 0

            call_s, put_s = option_strike, option_strike * 0.90 / option_strike * option_strike

            put_s = round(ltp * 0.95, 2)

            call_s = option_strike

            net_prem = 0.0  # collar ≈ cost-neutral

            for label, pct, css in [("Bull (+15%)",  0.15, "sc-bull"),

                                     ("Normal (flat)", 0.0, "sc-normal"),

                                     ("Bear (−10%)", -0.10, "sc-bear"),

                                     ("Crash (−30%)",-0.30, "sc-crash")]:

                new_price = ltp * (1 + pct)

                pos_pnl   = (new_price - ltp) * qty

                # Call caps upside above call_s; put floors below put_s

                if new_price > call_s:

                    opt_pnl = -(new_price - call_s) * opt_lots * US_OPTION_CONTRACT

                elif new_price < put_s:

                    opt_pnl = (put_s - new_price) * opt_lots * US_OPTION_CONTRACT

                else:

                    opt_pnl = 0.0

                net = pos_pnl + opt_pnl

                rows.append((label, f"{pct:+.0%}", f"{cur_sym}{pos_pnl:+,.0f}",

                             f"{cur_sym}{opt_pnl:+,.0f}", f"{cur_sym}{net:+,.0f}", css))

        else:

            return ""

        if not rows:

            return ""

        hdr = ("<table class='scenarios'>"

               "<tr><th>Scenario</th><th>Stock</th><th>Position P&amp;L</th>"

               "<th>Option P&amp;L</th><th>Net Result</th></tr>")

        body = "".join(

            f"<tr class='{r[5]}'><td>{r[0]}</td><td>{r[1]}</td>"

            f"<td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td></tr>"

            for r in rows

        )

        return hdr + body + "</table>"

    # ─────────────────────────────────────────────────────────────────────────

    # Decision logic

    # ─────────────────────────────────────────────────────────────────────────

    if pnl_pct < -30.0:

        strategy = "EXIT"

        urgency  = "HIGH"

        rationale.append(f"Severe drawdown: {pnl_pct:.1f}% — immediate stop-loss review")

        if is_lev_etf and underlying:

            rationale.append(lev_context)

            details["stop_loss_note"] = (

                f"{raw_ticker} tracks {underlying} at {abs_lev:.0f}× leverage. "

                f"Monitor {underlying} price for stop. Use price-action stop or exit via {broker}."

            )

        else:

            details["stop_loss_note"] = (

                f"No OI signals for {exchange}; use price-action stop or exit to free capital"

            )

    elif pnl_pct < loss_pct:

        strategy = "EXIT"

        urgency  = "MEDIUM"

        rationale.append(f"P&L = {pnl_pct:.1f}% (below {loss_pct:.0f}%)")

        if is_lev_etf and underlying:

            rationale.append(lev_context)

            details["stop_loss_note"] = (

                f"{raw_ticker} = {abs_lev:.0f}x {underlying}. "

                f"Sell {raw_ticker} via {broker} OR buy {underlying} puts on IBKR "

                f"to limit leveraged downside."

            )

        else:

            details["stop_loss_note"] = (

                f"Consider stop exit via {broker}. "

                f"No single-stock F&O OI signals for {exchange}."

            )

    elif pnl_pct >= big_gain_pct:

        # ── PROTECTIVE PUT ────────────────────────────────────────────────────

        # Guard: ASX (and other non-US) single stocks — verify listed options exist.

        # yfinance is the ground truth: if .options is empty, the stock has no

        # listed options and any premium estimates we show are fabricated.

        if exchange == "ASX" and not is_lev_etf:

            try:

                import yfinance as _yf_ao

                _asx_has_opts = bool(_yf_ao.Ticker(raw_ticker + ".AX").options)

            except Exception as exc:

                _log.warning("ASX options check failed", ticker=raw_ticker, exc_info=exc)

                _asx_has_opts = False  # conservative: treat as no options

            if not _asx_has_opts:

                rationale.append(

                    f"Gain: {pnl_pct:.1f}% on {cur_sym}{val:,.0f} — strong profit, but "

                    f"{raw_ticker} has NO listed options on ASX"

                )

                details["no_options_note"] = (

                    f"⚠️ {raw_ticker} has no listed options on ASX. "

                    f"Option hedge recommendations cannot be generated for this stock. "

                    f"Alternatives: (1) trim the position to lock partial profit, "

                    f"(2) hedge via a broad ASX ETF put (VAS / IOZ via IBKR), "

                    f"or (3) hold and set a price alert at "

                    f"{cur_sym}{ltp * 0.90:.2f} (−10% trailing stop)."

                )

                return {

                    "strategy": "HOLD", "urgency": "LOW",

                    "rationale": rationale, "details": details,

                    "pcr": None, "oi_type": None,

                    "regime": f"N/A ({exchange})", "india_vix": None,

                }

        strategy = "PROTECTIVE PUT"

        urgency  = "MEDIUM" if val >= LARGE_POS_INR else "LOW"

        rationale.append(

            f"Gain: {pnl_pct:.1f}% on {cur_sym}{val:,.0f} — protect profits"

        )

        if is_lev_etf and underlying:

            rationale.append(lev_context)

            rationale.append(

                f"Use {underlying} puts (more liquid than {raw_ticker} options)"

            )

        # Specific contract

        put_strike  = round(ltp * 0.95 * abs_lev, 2) if is_lev_etf else round(ltp * 0.95, 2)

        # For US stocks round to nearest $0.50 / $1

        put_strike  = round(put_strike * 2) / 2

        # Premium estimate: ~1.8% of underlying price for 5% OTM 1-month put

        prem_per_share = round((hedge_ticker and ltp or ltp) * abs_lev * 0.018, 2)

        total_cost  = prem_per_share * opt_lots * US_OPTION_CONTRACT

        details["put_contract"] = (

            f"Buy {opt_lots}× {hedge_ticker} {expiry} ${put_strike:.2f}P "

            f"(5% OTM, {opt_lots} contracts = {opt_lots*US_OPTION_CONTRACT} shares)"

        )

        details["put_cost_est"] = (

            f"Est. cost: ~{cur_sym}{prem_per_share:.2f}/share "

            f"= {cur_sym}{total_cost:,.0f} total "

            f"(~{total_cost/val*100:.1f}% of position/month)"

        )

        details["put_note"] = _scenarios_html("PROTECTIVE_PUT", put_strike, prem_per_share)

    elif pnl_pct >= MODERATE_GAIN_PCT:

        # ── COVERED CALL ──────────────────────────────────────────────────────

        if exchange == "ASX" and not is_lev_etf:

            try:

                import yfinance as _yf_cc

                _asx_has_opts_cc = bool(_yf_cc.Ticker(raw_ticker + ".AX").options)

            except Exception:

                _asx_has_opts_cc = False

            if not _asx_has_opts_cc:

                rationale.append(

                    f"Gain: {pnl_pct:.1f}% on {cur_sym}{val:,.0f} — "

                    f"{raw_ticker} has NO listed options on ASX (covered-call not available)"

                )

                details["no_options_note"] = (

                    f"⚠️ {raw_ticker} has no listed options on ASX. "

                    f"Covered-call recommendations cannot be generated. "

                    f"Consider holding and letting the position grow, or "

                    f"set a price alert at {cur_sym}{ltp * 0.90:.2f} (−10% trailing stop)."

                )

                return {

                    "strategy": "HOLD", "urgency": "LOW",

                    "rationale": rationale, "details": details,

                    "pcr": None, "oi_type": None,

                    "regime": f"N/A ({exchange})", "india_vix": None,

                }

        strategy = "COVERED CALL"

        urgency  = "LOW"

        rationale.append(

            f"Moderate gain: {pnl_pct:.1f}% — generate income while holding"

        )

        if is_lev_etf and underlying:

            rationale.append(lev_context)

            rationale.append(

                f"Sell {underlying} covered calls to hedge {raw_ticker} position"

            )

        # Specific contract

        call_strike = round(ltp * 1.10 * abs_lev, 2) if is_lev_etf else round(ltp * 1.10, 2)

        call_strike = round(call_strike * 2) / 2

        # Try live bid/ask mid from yfinance; fall back to rule-of-thumb

        real_c, c_src = _fetch_option_quote(hedge_ticker, expiry, call_strike, "C")

        if real_c and real_c > 0:

            prem_per_share = real_c

            prem_label = f"{cur_sym}{prem_per_share:.2f}/sh \u2022 {c_src} quote"

        else:

            prem_per_share = round(ltp * 0.012, 2)

            prem_label = f"~{cur_sym}{prem_per_share:.2f}/sh \u2022 estimated"

        total_income   = prem_per_share * opt_lots * US_OPTION_CONTRACT

        details["call_contract"] = (

            f"Sell {opt_lots}\u00d7 {hedge_ticker} {expiry} ${call_strike:.2f}C "

            f"(10% OTM, {opt_lots} contract{'s' if opt_lots > 1 else ''} = {opt_lots*US_OPTION_CONTRACT} shares)"

        )

        details["call_income_est"] = (

            f"Income: {prem_label} = {cur_sym}{total_income:,.0f}/month "

            f"({total_income/val*100:.1f}% yield)"

        )

        details["scenario_table"] = _scenarios_html("COVERED_CALL", call_strike, prem_per_share)

        details["call_risk_note"] = (

            f"Max upside capped at {cur_sym}{call_strike:.2f} (+{(call_strike/ltp-1)*100:.0f}% "

            f"from current); losses not protected (only premium cushion)."

        )

        prem_per_share = round((ltp * abs_lev if is_lev_etf else ltp) * 0.012, 2)

        total_income   = prem_per_share * opt_lots * US_OPTION_CONTRACT

        details["call_contract"] = (

            f"Sell {opt_lots}× {hedge_ticker} {expiry} ${call_strike:.2f}C "

            f"(10% OTM, {opt_lots} contract{'s' if opt_lots > 1 else ''} = {opt_lots*US_OPTION_CONTRACT} shares)"

        )

        details["call_income_est"] = (

            f"Est. income: ~{cur_sym}{prem_per_share:.2f}/share "

            f"= {cur_sym}{total_income:,.0f}/month "

            f"({total_income/val*100:.1f}% yield)"

        )

        details["scenario_table"] = _scenarios_html("COVERED_CALL", call_strike, prem_per_share)

        details["call_risk_note"] = (

            f"Max upside capped at {cur_sym}{call_strike:.2f} (+{(call_strike/ltp-1)*100:.0f}% "

            f"from current); losses not protected (only premium cushion)."

        )

    else:

        if pnl_pct < 0:

            rationale.append(f"P&L = {pnl_pct:.1f}% — within acceptable range; monitor")

        else:

            rationale.append(f"P&L = {pnl_pct:.1f}% — no immediate hedge needed")

        if is_lev_etf and underlying:

            rationale.append(lev_context)

    return {

        "strategy":  strategy,

        "urgency":   urgency,

        "rationale": rationale,

        "details":   details,

        "pcr":       None,

        "oi_type":   None,

        "regime":    f"N/A ({exchange})",

        "india_vix": None,

    }


def _recommend_hedge_long_option(holding: dict, opt_info: dict) -> dict:

    """

    Hedge recommendation for a long call or put option position.

    Risk is already CAPPED at premium paid — the goal is:

      1. Lock in unrealised gains (sell higher-strike call → bull spread)

      2. Decide whether to hold for more upside, roll to next expiry, or close

    For a LONG CALL with profit:

      • CLOSE (near expiry, profit): sell the call NOW — take profit before theta kills it

      • ROLL  (near expiry, profit): close April, open May same strike — keep exposure

      • LOCK PROFIT (bull spread): sell higher-strike call, collect premium

          → that premium is received TODAY regardless of future price moves

          → worst-case outcome = premium received – original cost (much better than full loss)

          → max-locked profit = spread width × contracts × 100 – net cost

    """

    underlying  = opt_info["underlying"]

    expiry_date = opt_info["expiry_date"]

    expiry_str  = opt_info["expiry_str"]

    opt_type    = opt_info["option_type"]   # "C" or "P"

    strike      = opt_info["strike"]

    contracts        = holding.get("total_qty", 1)

    combined_avg     = holding.get("combined_avg", 0)    # total cost paid per contract

    combined_ltp     = holding.get("combined_ltp", 0)    # per-share option premium now

    combined_val     = holding.get("combined_val", 0)    # total current value

    pnl_pct          = holding.get("combined_pnl_pct", 0)

    total_cost       = combined_avg * contracts           # total amount paid

    currency         = holding.get("currency", "USD")

    cur_sym          = {"AUD": "A$", "HKD": "HK$", "SGD": "S$", "USD": "$"}.get(currency, currency + " ")

    # Per-share costs (Moomoo stores combined_avg as total contract cost, ltp as per-share premium)

    cost_per_share    = combined_avg / 100 if combined_avg > 0 else 0

    current_per_share = combined_ltp

    today          = datetime.now()

    days_to_expiry = max(0, (expiry_date - today).days)

    near_expiry    = days_to_expiry <= 7

    mod_expiry     = days_to_expiry <= 21

    strategy  = "HOLD"

    urgency   = "LOW"

    rationale: list[str] = []

    details:   dict      = {}

    opt_label = "CALL" if opt_type == "C" else "PUT"

    rationale.append(f"{underlying} ${strike:,.2f}{opt_type} @ {expiry_str}")

    rationale.append(

        f"Option {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% "

        f"(paid {cur_sym}{cost_per_share:.2f}/sh → now {cur_sym}{current_per_share:.2f}/sh)"

    )

    # ── Fetch underlying spot price for ITM/OTM status + scenarios ───────────

    # Primary: reuse from chain data (already fetched, uses .history() which is NaN-safe)

    # chain_data is fetched below (after _opt_scenario_table is defined) so we do

    # a lightweight spot-only fetch here; chain_data will override if it has spot.

    spot: Optional[float] = None

    try:

        import yfinance as yf

        _t_spot = yf.Ticker(underlying)

        _h = _t_spot.history(period="2d", auto_adjust=True)

        if not _h.empty:

            _v = float(_h["Close"].iloc[-1])

            if _v > 0:

                spot = _v

    except Exception:

        pass

    if spot and spot > 0:

        moneyness = (spot - strike) if opt_type == "C" else (strike - spot)

        if moneyness > 0:

            rationale.append(f"{underlying} ~{cur_sym}{spot:.2f} — {opt_label} is ITM by {cur_sym}{moneyness:.2f} ({moneyness/strike*100:.1f}%)")

        elif moneyness < 0:

            rationale.append(f"{underlying} ~{cur_sym}{spot:.2f} — {opt_label} is OTM by {cur_sym}{abs(moneyness):.2f} ({abs(moneyness)/strike*100:.1f}%)")

        else:

            rationale.append(f"{underlying} ~{cur_sym}{spot:.2f} — At-the-Money")

    # ── Expiry urgency ────────────────────────────────────────────────────────

    if near_expiry:

        rationale.append(f"🔴 ONLY {days_to_expiry} DAYS TO EXPIRY — theta decay is critical")

    elif mod_expiry:

        rationale.append(f"⚠ {days_to_expiry} days to expiry — act before theta accelerates")

    else:

        rationale.append(f"📅 {days_to_expiry} days to expiry")

    # ── Helper: build P&L scenario table ─────────────────────────────────────

    def _opt_scenario_table(spread_width: float, short_prem_est: float, short_strike: float) -> str:

        if not spot or spot <= 0:

            # Fallback: strike-relative table

            rows = [

                (f"OTM: stock ≤ ${strike:,.2f}",              0.0),

                (f"At strike (${strike:,.2f})",                0.0),

                (f"+{spread_width:.0f}% of spread → ${strike+spread_width:.2f}", spread_width),

                (f"At spread cap (${short_strike:,.2f}+)",     spread_width),

            ]

            hdr = ("<table class='opt-pnl-table'>"

                   "<tr><th>Scenario</th>"

                   "<th>Hold Naked P&amp;L</th>"

                   "<th>Bull Spread P&amp;L (lock)</th></tr>")

            body = ""

            for lbl, delta in rows:

                intrinsic  = delta * 100 * contracts

                naked_pnl  = intrinsic - total_cost

                spread_pnl = min(intrinsic, spread_width * 100 * contracts) - total_cost + (short_prem_est * 100 * contracts)

                nc = "opt-profit" if naked_pnl  > 0 else ("opt-loss" if naked_pnl  < 0 else "opt-neutral")

                sc = "opt-profit" if spread_pnl > 0 else ("opt-loss" if spread_pnl < 0 else "opt-neutral")

                body += (f"<tr class='{sc}'><td>{lbl}</td>"

                         f"<td class='{nc}'>{cur_sym}{naked_pnl:+,.0f}</td>"

                         f"<td class='{sc}'>{cur_sym}{spread_pnl:+,.0f}</td></tr>")

            return hdr + body + "</table>"

        # Full spot-price scenarios

        scenarios_data = [

            (f"Crash −30% → {cur_sym}{spot*0.70:.2f}",  spot * 0.70),

            (f"Bear  −15% → {cur_sym}{spot*0.85:.2f}",  spot * 0.85),

            (f"Flat       → {cur_sym}{spot:.2f}",        spot),

            (f"Bull  +10% → {cur_sym}{spot*1.10:.2f}",   spot * 1.10),

            (f"Bull  +20% → {cur_sym}{spot*1.20:.2f}",   spot * 1.20),

        ]

        hdr = ("<table class='opt-pnl-table'>"

               "<tr><th>Scenario (underlying at expiry)</th>"

               "<th>Hold Naked P&amp;L</th>"

               "<th>Bull Spread P&amp;L (lock)</th></tr>")

        body = ""

        for lbl, new_spot in scenarios_data:

            long_intr   = max(0.0, new_spot - strike) * 100 * contracts

            short_intr  = max(0.0, new_spot - short_strike) * 100 * contracts

            naked_pnl   = long_intr - total_cost

            spread_pnl  = long_intr - short_intr - total_cost + (short_prem_est * 100 * contracts)

            nc = "opt-profit" if naked_pnl  > 0 else ("opt-loss" if naked_pnl  < 0 else "opt-neutral")

            sc = "opt-profit" if spread_pnl > 0 else ("opt-loss" if spread_pnl < 0 else "opt-neutral")

            body += (f"<tr class='{sc}'><td>{lbl}</td>"

                     f"<td class='{nc}'>{cur_sym}{naked_pnl:+,.0f}</td>"

                     f"<td class='{sc}'>{cur_sym}{spread_pnl:+,.0f}</td></tr>")

        return hdr + body + "</table>"

    # ── Fetch full OTM chain for multi-strike spread analysis ─────────────────

    chain_data  = _fetch_full_chain(underlying, expiry_str)

    chain_calls = chain_data["calls"] if chain_data else None   # full calls DataFrame

    if chain_data and chain_data.get("spot"):

        spot = chain_data["spot"]  # chain fetch used .history() — more reliable

    # ── Helpers: mid from chain row ───────────────────────────────────────────

    def _row_mid(row: "object") -> float:

        b = float(row["bid"] or 0)

        a = float(row["ask"] or 0)

        l = float(row["lastPrice"] or 0)

        if b > 0 and a > 0:

            return (b + a) / 2.0

        return l

    # ── Build spread-candidate list (OTM calls for a LONG CALL) ──────────────

    spread_candidates: list[dict] = []

    if opt_type == "C" and chain_calls is not None:

        otm_calls = chain_calls[

            (chain_calls["strike"] > strike) &

            (chain_calls["strike"] <= strike + 30)

        ].copy()

        for _, row in otm_calls.iterrows():

            k2   = float(row["strike"])

            prem = _row_mid(row)

            if prem <= 0:

                continue

            prem_total    = prem * 100 * contracts

            spread_width  = k2 - strike

            floor         = prem_total - total_cost          # both OTM worst case

            max_pnl       = spread_width * 100 * contracts + prem_total - total_cost

            bep_vs_naked  = k2 + prem   # naked beats spread above this spot

            ratio = floor / max_pnl if max_pnl > 0 else -999.0

            spread_candidates.append({

                "strike": k2, "prem": prem, "prem_total": prem_total,

                "spread_width": spread_width, "floor": floor,

                "max_pnl": max_pnl, "bep": bep_vs_naked, "ratio": ratio,

                "src": "live" if (float(row.get("bid", 0) or 0) > 0 and float(row.get("ask", 0) or 0) > 0) else "last",

            })

    # Same for LONG PUT (bear spreads: sell lower-strike put)

    if opt_type == "P" and chain_data is not None:

        chain_puts = chain_data.get("puts")

        if chain_puts is not None:

            itm_puts = chain_puts[

                (chain_puts["strike"] < strike) &

                (chain_puts["strike"] >= strike - 30)

            ].copy().iloc[::-1]  # descending strike

            for _, row in itm_puts.iterrows():

                k2   = float(row["strike"])

                prem = _row_mid(row)

                if prem <= 0:

                    continue

                prem_total   = prem * 100 * contracts

                spread_width = strike - k2

                floor        = prem_total - total_cost

                max_pnl      = spread_width * 100 * contracts + prem_total - total_cost

                ratio = floor / max_pnl if max_pnl > 0 else -999.0

                spread_candidates.append({

                    "strike": k2, "prem": prem, "prem_total": prem_total,

                    "spread_width": spread_width, "floor": floor,

                    "max_pnl": max_pnl, "bep": 0.0, "ratio": ratio, "src": "live",

                })

    # ── Fetch real PUT option chain from yfinance ────────────────────────────

    # We need PUTS on the underlying stock (not the call chain we own).

    # yfinance is the reliable source; chain_data holds calls for our position.

    _put_chain_df = None   # pandas DataFrame or None

    if opt_type == "C" and spot and spot > 0:

        try:

            import yfinance as _yf_mod

            _t = _yf_mod.Ticker(underlying)

            _exps = _t.options  # list of expiry strings "YYYY-MM-DD"

            if _exps:

                from datetime import datetime as _dt

                try:

                    _target_yfmt = _dt.strptime(expiry_str, "%d %b %Y").strftime("%Y-%m-%d")

                except ValueError:

                    _target_yfmt = None

                _chosen_exp = None

                if _target_yfmt and _target_yfmt in _exps:

                    _chosen_exp = _target_yfmt

                elif _target_yfmt:

                    try:

                        _t_obj = _dt.strptime(_target_yfmt, "%Y-%m-%d")

                        _close = min(_exps, key=lambda e: abs((_dt.strptime(e, "%Y-%m-%d") - _t_obj).days))

                        if abs((_dt.strptime(_close, "%Y-%m-%d") - _t_obj).days) <= 21:

                            _chosen_exp = _close

                    except Exception:

                        pass

                if _chosen_exp is None and _exps:

                    _chosen_exp = _exps[0]

                if _chosen_exp:

                    _chain_obj = _t.option_chain(_chosen_exp)

                    _put_chain_df = _chain_obj.puts

        except Exception:

            pass

    # ── Validate a put quote: must be >0 and plausible vs intrinsic value ──

    def _validate_put_prem(put_k: float, raw_lp: float, raw_bid: float, raw_ask: float) -> tuple:

        """Returns (validated_price, source_tag) or (0.0, '') if unusable."""

        intrinsic = max(0.0, put_k - (spot or put_k))

        if raw_bid > 0 and raw_ask > 0 and raw_ask > raw_bid:

            mid = (raw_bid + raw_ask) / 2.0

            if intrinsic <= mid <= put_k:

                return mid, "live"

        lp = raw_lp

        if lp <= 0 or lp > put_k:

            return 0.0, ""

        if lp < max(intrinsic * 0.5, 0.01):

            return 0.0, ""

        return lp, "last"

    # ── Build put-buying candidates from real put chain ───────────────────────

    put_buy_candidates: list[dict] = []

    if opt_type == "C" and spot and spot > 0:

        puts_df = _put_chain_df

        if puts_df is None:

            raw_fb = chain_data.get("puts") if chain_data else None

            if raw_fb is not None and len(raw_fb) > 0:

                puts_df = raw_fb

        if puts_df is not None and len(puts_df) > 0:

            lo = spot * 0.88

            hi = spot * 1.02

            rel = puts_df[(puts_df["strike"] >= lo) & (puts_df["strike"] <= hi)].copy()

            cur_pnl_now = holding.get("combined_pnl", combined_val - total_cost)

            for _, row in rel.iterrows():

                put_k   = float(row.get("strike", 0))

                lp      = float(row.get("lastPrice", 0) or 0)

                bid     = float(row.get("bid", 0) or 0)

                ask     = float(row.get("ask", 0) or 0)

                put_prem, src_tag = _validate_put_prem(put_k, lp, bid, ask)

                if put_prem <= 0:

                    continue

                put_total = put_prem * contracts * US_OPTION_CONTRACT

                net_cost  = total_cost + put_total

                bep       = strike + net_cost / (contracts * US_OPTION_CONTRACT)

                max_loss  = -(net_cost)

                efficiency = cur_pnl_now / put_total if put_total > 0 else 0.0

                put_buy_candidates.append({

                    "strike":     put_k,

                    "prem":       put_prem,

                    "put_total":  put_total,

                    "net_cost":   net_cost,

                    "max_loss":   max_loss,

                    "bep":        bep,

                    "moneyness":  put_k - spot,

                    "src":        src_tag,

                    "efficiency": efficiency,

                })

        put_buy_candidates.sort(key=lambda r: abs(r["moneyness"]))

    # ── Select only the best 3 puts ────────────────────────────────────────────

    def _best_puts_selection() -> list[dict]:

        if not put_buy_candidates:

            return []

        selected: list[dict] = []

        seen: set[float] = set()

        atm = put_buy_candidates[0]

        selected.append(atm)

        seen.add(atm["strike"])

        otm_puts = [c for c in put_buy_candidates if c["moneyness"] < -0.5 and c["strike"] not in seen]

        if otm_puts:

            cheapest = min(otm_puts, key=lambda r: r["prem"])

            selected.append(cheapest)

            seen.add(cheapest["strike"])

        eff_puts = [c for c in put_buy_candidates if c["strike"] not in seen]

        if eff_puts:

            best_eff = max(eff_puts, key=lambda r: r["efficiency"])

            selected.append(best_eff)

            seen.add(best_eff["strike"])

        selected.sort(key=lambda r: r["strike"], reverse=True)

        return selected

    # ── Fetch real collar prices from put/call chains ──────────────────────────

    def _get_collar_prices() -> tuple:

        if not (spot and spot > 0):

            return (0.0, 0.0, 0.0, 0.0)

        tgt_put_k  = round(spot * 0.97)

        tgt_call_k = round(spot * 1.05)

        c_put_k, c_put_prem   = float(tgt_put_k),  spot * 0.010

        c_call_k, c_call_prem = float(tgt_call_k), spot * 0.010

        if _put_chain_df is not None and len(_put_chain_df) > 0:

            _pdf = _put_chain_df.copy()

            _pdf["_dist"] = abs(_pdf["strike"] - tgt_put_k)

            nearest = _pdf.nsmallest(1, "_dist").iloc[0]

            pk = float(nearest["strike"])

            pp, _ = _validate_put_prem(

                pk,

                float(nearest.get("lastPrice", 0) or 0),

                float(nearest.get("bid", 0) or 0),

                float(nearest.get("ask", 0) or 0),

            )

            if pp > 0:

                c_put_k, c_put_prem = pk, pp

        if spread_candidates:

            best_c = min(spread_candidates, key=lambda r: abs(r["strike"] - tgt_call_k))

            c_call_k, c_call_prem = best_c["strike"], best_c["prem"]

        return c_put_k, c_put_prem, c_call_k, c_call_prem

    # ── Build HTML: top-3 buy-put summary ─────────────────────────────────────

    def _build_buy_put_candidates_html() -> str:

        picks = _best_puts_selection()

        if not picks or not spot:

            return ""

        cur_pnl_now = holding.get("combined_pnl", combined_val - total_cost)

        labels = {0: "ATM (strongest protection)", 1: "Cheapest OTM", 2: "Best efficiency"}

        rows_html = ""

        for idx, cand in enumerate(picks):

            k        = cand["strike"]

            prem     = cand["prem"]

            put_tot  = cand["put_total"]

            bep      = cand["bep"]

            max_loss = cand["max_loss"]

            mono     = cand["moneyness"]

            src      = cand["src"]

            label    = labels.get(idx, "")

            mono_lbl = "ATM" if abs(mono) < 1 else (f"ITM ${mono:+.2f}" if mono > 0 else f"OTM ${mono:.2f}")

            mono_col = "#68d391" if mono >= 0 else ("#e3b341" if abs(mono) < 3 else "#8b949e")

            cost_pct = put_tot / cur_pnl_now * 100 if cur_pnl_now > 0 else 0

            protected = combined_val - put_tot

            prot_pct  = protected / combined_val * 100 if combined_val > 0 else 0

            rows_html += (

                f"<tr>"

                f"<td style='color:#e3b341;font-size:.78rem'>{label}</td>"

                f"<td>${k:.0f}P <span style='color:#8b949e;font-size:.70rem'>[{src}]</span></td>"

                f"<td style='color:{mono_col}'>{mono_lbl}</td>"

                f"<td>{cur_sym}{prem:.2f}/sh = {cur_sym}{put_tot:.0f} total"

                f"<br><small style='color:#8b949e'>{cost_pct:.1f}% of unrealised P&amp;L</small></td>"

                f"<td style='color:#f85149'>{cur_sym}{max_loss:+,.0f}</td>"

                f"<td style='color:#3fb950'>Unlimited above ${bep:.2f}</td>"

                f"<td style='color:#68d391'>{prot_pct:.0f}% of position protected"

                f"<br><small style='color:#8b949e'>Min value {cur_sym}{protected:.0f} if hedged today</small></td>"

                f"</tr>"

            )

        return (

            "<div style='margin-top:10px;background:#0a0d12;border:1px solid #238636;"

            "border-radius:6px;padding:10px 14px'>"

            f"<div style='color:#68d391;font-size:.82rem;font-weight:700;margin-bottom:4px'>"

            f"Shield Strategy 5: Buy Put on {underlying} — Top 3 Best Picks (Real Prices)"

            f"<span style='color:#8b949e;font-weight:400;margin-left:8px;font-size:.75rem'>"

            f"spot ${spot:.2f} | call paid {cur_sym}{total_cost:.0f} | now {cur_sym}{combined_val:.0f}"

            f"</span></div>"

            f"<div style='color:#8b949e;font-size:.76rem;margin-bottom:6px'>"

            f"You own {contracts}x {underlying} ${strike:.0f}C. Adding a PUT creates a floor while call "

            f"keeps UNLIMITED upside. Max loss = call premium + put premium (never more). "

            f"All prices fetched live from yfinance option chain."

            f"</div>"

            "<table class='opt-pnl-table'>"

            "<tr><th>Rank</th><th>Put Strike</th><th>vs Spot ${:.2f}</th>"

            "<th>Put Cost</th><th>Max Loss at Expiry</th>"

            "<th>Upside (Unlimited)</th><th>Today's Gain Protected</th></tr>".format(spot)

            + rows_html + "</table></div>"

        )

    # ── Build HTML: all-strategies comprehensive comparison ────────────────────

    def _build_all_strategies_html() -> str:

        if not (spot and spot > 0):

            return ""

        cur_pnl   = holding.get("combined_pnl", combined_val - total_cost)

        best_puts = _best_puts_selection()

        best_put  = best_puts[0] if best_puts else None

        best_sp   = best_spread

        roll_debit_est = round(current_per_share * 0.30 * contracts * US_OPTION_CONTRACT, 0)

        collar_put_k, collar_put_prem, collar_call_k, collar_call_prem = _get_collar_prices()

        collar_net_debit = round((collar_put_prem - collar_call_prem) * contracts * US_OPTION_CONTRACT, 0)

        collar_max_loss  = -(total_cost + max(0.0, float(collar_net_debit)))

        def fmt(v: float) -> str:

            sign = "+" if v >= 0 else ""

            return f"{cur_sym}{sign}{v:,.0f}"

        rows = []

        rows.append({

            "name":    "1. Hold Naked Call",

            "icon":    "Lightning",

            "color":   "#e3b341",

            "cost":    "No new cost",

            "max_loss": fmt(-total_cost),

            "max_gain": "Unlimited",

            "downside": f"Lose {cur_sym}{total_cost:.0f} if {underlying} falls below ${strike:.0f} at expiry",

            "upside":   f"100% of any rally above ${strike:.0f}. Current gain {fmt(cur_pnl)}",

            "best_for": f"Expecting strong rally; comfortable risking {cur_sym}{total_cost:.0f}",

            "action":   "Do nothing",

        })

        rows.append({

            "name":    "2. Close Now",

            "icon":    "Lock",

            "color":   "#68d391",

            "cost":    "No new cost",

            "max_loss": "Zero — profit locked",

            "max_gain": fmt(cur_pnl),

            "downside": "None — closed immediately",

            "upside":   f"None — exit with {fmt(cur_pnl)} guaranteed",

            "best_for": "Expecting pullback or satisfied with gain",

            "action":   f"Sell {contracts}x {underlying} ${strike:.0f}C @ {cur_sym}{current_per_share:.2f}/sh",

        })

        rows.append({

            "name":    "3. Roll to Next Month",

            "icon":    "Roll",

            "color":   "#63b3ed",

            "cost":    f"~{cur_sym}{roll_debit_est:.0f} debit",

            "max_loss": fmt(-(total_cost + roll_debit_est)),

            "max_gain": "Unlimited",

            "downside": f"Total risk {cur_sym}{total_cost + roll_debit_est:.0f}; same ${strike:.0f} strike",

            "upside":   "Full unlimited upside with 30 extra days",

            "best_for": "Bullish near expiry; willing to pay for more time",

            "action":   f"Close {expiry_str} ${strike:.0f}C then Open next month ${strike:.0f}C",

        })

        if best_sp:

            sp_floor = best_sp["floor"]

            sp_max   = best_sp["max_pnl"]

            sp_k2    = best_sp["strike"]

            sp_prem  = best_sp["prem_total"]

            rows.append({

                "name":    f"4. Bull Spread (sell ${sp_k2:.0f}C)",

                "icon":    "Spread",

                "color":   "#9f7aea",

                "cost":    f"Receive {cur_sym}{sp_prem:.0f}",

                "max_loss": fmt(sp_floor) + (" (guaranteed profit floor)" if sp_floor > 0 else ""),

                "max_gain": fmt(sp_max),

                "downside": f"Floor at {fmt(sp_floor)} guaranteed",

                "upside":   f"Capped at ${sp_k2:.0f} (max {fmt(sp_max)})",

                "best_for": f"Moderate rally max ${sp_k2:.0f}; want guaranteed floor",

                "action":   f"Sell {contracts}x {underlying} ${sp_k2:.0f}C @ {cur_sym}{best_sp['prem']:.2f}/sh",

            })

        else:

            rows.append({

                "name":    "4. Bull Spread",

                "icon":    "Spread",

                "color":   "#9f7aea",

                "cost":    "Receive premium",

                "max_loss": "Floored (see spread table above)",

                "max_gain": "Capped at short strike",

                "downside": "Guaranteed floor",

                "upside":   "Capped",

                "best_for": "Moderate rally; guaranteed floor",

                "action":   "Sell OTM call — see spread table above",

            })

        if best_put:

            bp_k    = best_put["strike"]

            bp_prem = best_put["prem"]

            bp_tot  = best_put["put_total"]

            bp_loss = best_put["max_loss"]

            bp_bep  = best_put["bep"]

            cost_pct = bp_tot / cur_pnl * 100 if cur_pnl > 0 else 0

            rows.append({

                "name":    f"5. Buy Put ${bp_k:.0f}P — FLOOR + UNLIMITED",

                "icon":    "Shield",

                "color":   "#68d391",

                "cost":    f"Pay {cur_sym}{bp_tot:.0f} ({cost_pct:.1f}% of gain)",

                "max_loss": fmt(bp_loss),

                "max_gain": "UNLIMITED",

                "downside": f"Below ${bp_k:.0f}: put gains offset loss; max risk = {cur_sym}{-bp_loss:.0f}",

                "upside":   f"UNLIMITED above ${bp_bep:.2f} BEP — call gains above ${strike:.0f}",

                "best_for": f"WANT FLOOR + UNLIMITED. Pay {cur_sym}{bp_tot:.0f} for protection",

                "action":   f"Buy {contracts}x {underlying} ${bp_k:.0f}P @ {cur_sym}{bp_prem:.2f}/sh = {cur_sym}{bp_tot:.0f}",

            })

        else:

            rows.append({

                "name":    "5. Buy Put — FLOOR + UNLIMITED",

                "icon":    "Shield",

                "color":   "#68d391",

                "cost":    "Pay put premium (~1.5% spot est.)",

                "max_loss": fmt(-(total_cost + round(spot * 0.015 * contracts * 100, 0))),

                "max_gain": "UNLIMITED",

                "downside": "Floored below put strike",

                "upside":   "UNLIMITED — call gains above strike",

                "best_for": "Floor + unlimited upside (no live puts — estimate only)",

                "action":   f"Buy {contracts}x ~${spot * 0.97:.0f}P (no live quotes)",

            })

        net_lbl = f"~{cur_sym}{abs(collar_net_debit):.0f} {'debit' if collar_net_debit > 0 else 'credit'} net"

        rows.append({

            "name":    f"6. Collar (sell ${collar_call_k:.0f}C / buy ${collar_put_k:.0f}P)",

            "icon":    "Collar",

            "color":   "#d29922",

            "cost":    net_lbl,

            "max_loss": fmt(collar_max_loss),

            "max_gain": f"Capped at ${collar_call_k:.0f}",

            "downside": f"Protected below ${collar_put_k:.0f}; call premium offsets put cost",

            "upside":   f"Capped at ${collar_call_k:.0f}",

            "best_for": f"Near-zero cost hedge; sacrifice gains above ${collar_call_k:.0f}",

            "action":   f"Sell {contracts}x ${collar_call_k:.0f}C @ {cur_sym}{collar_call_prem:.2f} + Buy {contracts}x ${collar_put_k:.0f}P @ {cur_sym}{collar_put_prem:.2f}",

        })

        icon_map = {

            "Lightning": "&#9889;",

            "Lock":      "&#128274;",

            "Roll":      "&#128260;",

            "Spread":    "&#128202;",

            "Shield":    "&#128737;",

            "Collar":    "&#127919;",

        }

        rows_html = ""

        for r in rows:

            hl = ("background:rgba(35,134,54,0.18);border-left:3px solid #238636;"

                  if "5." in r["name"] else

                  ("background:rgba(56,139,253,0.06);" if "2." in r["name"] else ""))

            ico = icon_map.get(r["icon"], r["icon"])

            rows_html += (

                f"<tr style='{hl}'>"

                f"<td style='color:{r['color']};font-weight:600;white-space:nowrap'>{ico} {r['name']}</td>"

                f"<td style='color:#8b949e'>{r['cost']}</td>"

                f"<td style='color:#f85149'>{r['max_loss']}</td>"

                f"<td style='color:#3fb950;font-weight:600'>{r['max_gain']}</td>"

                f"<td style='color:#8b949e;font-size:.79rem'>{r['downside']}</td>"

                f"<td style='color:#3fb950;font-size:.79rem'>{r['upside']}</td>"

                f"<td style='color:#e3b341;font-size:.77rem'>{r['best_for']}</td>"

                f"<td style='font-family:monospace;font-size:.76rem;color:#63b3ed'>{r['action']}</td>"

                f"</tr>"

            )

        return (

            "<div style='margin-top:14px;background:#0d1117;border:1px solid #30363d;"

            "border-radius:8px;padding:12px 16px'>"

            "<div style='color:#fff;font-size:.87rem;font-weight:700;margin-bottom:4px'>"

            f"ALL Strategy Permutations for {underlying} ${strike:.0f}C "

            f"<span style='color:#8b949e;font-weight:400'>"

            f"(paid {cur_sym}{total_cost:.0f} / now {cur_sym}{combined_val:.0f} / "

            f"{pnl_pct:+.1f}% / spot ${spot:.2f})</span>"

            "</div>"

            "<div style='color:#8b949e;font-size:.76rem;margin-bottom:8px'>"

            f"Six complete strategies. "

            f"<span style='color:#68d391;font-weight:600'>Strategy 5 (Buy Put)</span>"

            f" = floor + unlimited upside using real chain prices. "

            f"<span style='color:#9f7aea;font-weight:600'>Strategy 4 (Bull Spread)</span>"

            f" = guaranteed floor, capped upside."

            "</div>"

            "<div style='overflow-x:auto'><table class='opt-pnl-table'>"

            "<tr><th>Strategy</th><th>Net Cost</th><th>Max Loss</th><th>Max Gain</th>"

            "<th>Downside Protection</th><th>Upside</th><th>Best For</th><th>Exact Action</th></tr>"

            + rows_html +

            "</table></div></div>"

        )

    # ── Pick 3 best spreads for highlight ────────────────────────────────────

    best_ratio_cand = max(spread_candidates, key=lambda r: r["ratio"]) if spread_candidates else None

    best_floor_cand = max(spread_candidates, key=lambda r: r["floor"])  if spread_candidates else None

    best_max_cand   = max(spread_candidates, key=lambda r: r["max_pnl"]) if spread_candidates else None

    # ── Build HTML: full spread-comparison table ──────────────────────────────

    def _build_spread_comparison_html() -> str:

        if not spread_candidates:

            return ""

        dir_lbl  = "call" if opt_type == "C" else "put"

        rows_html = ""

        for cand in spread_candidates:

            # Highlight the three best picks

            tags = []

            if best_ratio_cand and cand["strike"] == best_ratio_cand["strike"]:

                tags.append("⭐ BEST RATIO")

            if best_floor_cand and cand["strike"] == best_floor_cand["strike"] and best_floor_cand["strike"] != (best_ratio_cand["strike"] if best_ratio_cand else None):

                tags.append("🔒 BEST FLOOR")

            if best_max_cand and cand["strike"] == best_max_cand["strike"]:

                tags.append("🚀 BEST MAX")

            tag_html = " ".join(f"<span style='background:#1f3a1f;color:#68d391;padding:1px 5px;border-radius:3px;font-size:.70rem'>{t}</span>" for t in tags)

            floor_cls = "opt-profit" if cand["floor"] > 0 else "opt-loss"

            src_badge = f"<span style='color:#8b949e;font-size:.70rem'> [{cand['src']}]</span>"

            bep_html  = f">${cand['bep']:.2f}" if cand["bep"] > 0 else "—"

            rows_html += (

                f"<tr class='{floor_cls}'>"

                f"<td>${cand['strike']:.0f}{dir_lbl.upper()[0]}{src_badge}</td>"

                f"<td>{cur_sym}{cand['prem']:.2f}/sh ({cur_sym}{cand['prem_total']:.0f})</td>"

                f"<td class='{'opt-profit' if cand['floor'] > 0 else 'opt-loss'}'>{cur_sym}{cand['floor']:+,.0f} ({cand['floor']/total_cost*100:+.0f}%)</td>"

                f"<td class='opt-profit'>{cur_sym}{cand['max_pnl']:+,.0f} ({cand['max_pnl']/total_cost*100:+.0f}%)</td>"

                f"<td>{cand['ratio']:.2f}</td>"

                f"<td>{bep_html}</td>"

                f"<td>{tag_html}</td>"

                f"</tr>"

            )

        return (

            "<div style='margin-top:8px'>"

            f"<div style='color:#9f7aea;font-size:.78rem;font-weight:600;margin-bottom:3px'>"

            f"📊 All OTM {'call' if opt_type == 'C' else 'put'} strikes — spread P&amp;L comparison"

            f"&nbsp;<span style='color:#8b949e;font-weight:400'>(Floor = worst-case if {underlying} stays below ${strike:.0f})</span></div>"

            "<table class='opt-pnl-table'>"

            f"<tr><th>Sell Strike</th><th>Premium received</th>"

            f"<th>Floor (worst)</th><th>Max P&amp;L (best)</th>"

            f"<th>Floor/Max</th><th>Naked beats spread at</th><th></th></tr>"

            + rows_html + "</table></div>"

        )

    # ── Build HTML: roll-forward candidates ──────────────────────────────────

    def _build_roll_candidates_html() -> str:

        if not (spot and spot > 0):

            return ""

        # Find expiry_str as YYYY-MM-DD

        try:

            ed = datetime.strptime(expiry_str, "%d %b %Y")

            ed_ymd = ed.strftime("%Y-%m-%d")

        except ValueError:

            return ""

        try:

            import yfinance as yf

            t = yf.Ticker(underlying)

            all_exps = [e for e in (t.options or []) if e > ed_ymd]

        except Exception:

            return ""

        rows_html = ""

        for roll_exp_ymd in all_exps[:4]:

            try:

                import yfinance as yf

                rc = yf.Ticker(underlying).option_chain(roll_exp_ymd)

                rcalls = rc.calls

                near = rcalls[

                    (rcalls["strike"] >= strike) & (rcalls["strike"] <= strike + 5)

                ]

                if near.empty:

                    continue

                row    = near.iloc[0]

                k_r    = float(row["strike"])

                r_mid  = _row_mid(row)

                if r_mid <= 0:

                    continue

                roll_cost_total = (r_mid - current_per_share) * 100 * contracts

                sign = "+" if roll_cost_total > 0 else ""

                roll_date = datetime.strptime(roll_exp_ymd, "%Y-%m-%d")

                extra_days = (roll_date.date() - datetime.now().date()).days - days_to_expiry

                rows_html += (

                    f"<tr>"

                    f"<td>{roll_date.strftime('%d %b %Y')}</td>"

                    f"<td>${k_r:.0f}{opt_type}</td>"

                    f"<td>{cur_sym}{r_mid:.2f}/sh</td>"

                    f"<td style='color:{'#f85149' if roll_cost_total > 0 else '#3fb950'}'>"

                    f"{sign}{cur_sym}{roll_cost_total:.0f} ({'debit—costs cash' if roll_cost_total > 0 else 'credit—gets cash'})</td>"

                    f"<td>+{extra_days} more days</td>"

                    f"</tr>"

                )

            except Exception:

                continue

        if not rows_html:

            return ""

        return (

            "<div style='margin-top:6px'>"

            "<div style='color:#63b3ed;font-size:.78rem;font-weight:600;margin-bottom:3px'>"

            f"🔄 Roll-forward options (close Apr, open further expiry at same ${strike:.0f}{opt_type})</div>"

            "<table class='opt-pnl-table'>"

            "<tr><th>Expiry</th><th>Strike</th><th>Mid</th>"

            "<th>Roll cost (vs current position)</th><th>Extra time bought</th></tr>"

            + rows_html + "</table></div>"

        )

    # ── Build HTML: decision matrix ───────────────────────────────────────────

    def _build_decision_matrix_html(best: "Optional[dict]") -> str:

        if not (spot and spot > 0):

            return ""

        otm_dist = strike - spot if opt_type == "C" else spot - strike

        otm_pct  = otm_dist / strike * 100 if strike else 0

        otm_lbl  = f"{'OTM' if otm_dist > 0 else 'ITM'} by {cur_sym}{abs(otm_dist):.2f} ({abs(otm_pct):.1f}%)"

        close_pnl = holding.get("combined_pnl", 0)

        rows = [

            ("If you expect a strong ≥3% rally", "Hold naked call",

             "Full unlimited upside; current value %s" % (f"{cur_sym}{combined_val:,.0f}"),

             "#3fb950"),

            ("If you want a guaranteed profit floor", f"Sell ${best['strike']:.0f}{opt_type} @ {cur_sym}{best['prem']:.2f}" if best else "Sell nearest OTM call",

             f"Floor={cur_sym}{best['floor']:+,.0f}  Max={cur_sym}{best['max_pnl']:+,.0f}" if best else "Locks in partial profit",

             "#68d391"),

            ("If you want certainty now", f"Close @ {cur_sym}{current_per_share:.2f}/sh",

             f"Lock {cur_sym}{close_pnl:+,.0f} ({pnl_pct:+.1f}%) — DONE",

             "#e3b341"),

            ("If you want more time for rally", "Roll to next expiry (debit)",

             "Preserves exposure; costs extra premium",

             "#63b3ed"),

        ]

        rows_html = "".join(

            f"<tr><td style='color:{c}'>{intention}</td>"

            f"<td style='font-family:monospace;color:#e3b341'>{action}</td>"

            f"<td style='color:#8b949e'>{outcome}</td></tr>"

            for intention, action, outcome, c in rows

        )

        theta_color = "#f85149" if days_to_expiry <= 7 else ("#d29922" if days_to_expiry <= 14 else "#8b949e")

        return (

            f"<div style='margin-top:8px;background:#050e1a;border:1px solid #30363d;"

            f"border-radius:5px;padding:8px 12px'>"

            f"<div style='color:#9f7aea;font-size:.80rem;font-weight:600;margin-bottom:5px'>"

            f"📋 Decision Matrix — {underlying} at {cur_sym}{spot:.2f} ({otm_lbl}), "

            f"<span style='color:{theta_color}'>{days_to_expiry} days to expiry</span></div>"

            "<table class='opt-pnl-table'>"

            "<tr><th>Your view / goal</th><th>Action</th><th>Outcome</th></tr>"

            + rows_html + "</table></div>"

        )

    # ──  Pick the recommended spread (best ratio with positive floor if possible) ──

    best_spread = None

    positive_floor_cands = [c for c in spread_candidates if c["floor"] > 0]

    if positive_floor_cands:

        best_spread = max(positive_floor_cands, key=lambda r: r["ratio"])

    elif spread_candidates:

        best_spread = max(spread_candidates, key=lambda r: r["ratio"])

    # ── Decision logic ────────────────────────────────────────────────────────

    if near_expiry and pnl_pct > 0:

        # Expiry is imminent and we have profit — urgent action needed

        strategy = "CLOSE"

        urgency  = "HIGH"

        rationale.append(f"Take profit NOW ({cur_sym}{combined_val:,.0f}) before time-value evaporates")

        next_m = _next_expiry_us()

        details["close_action"] = (

            f"Sell {contracts}× {underlying} {expiry_str} ${strike:,.2f}{opt_type} "

            f"→ Realise {cur_sym}{holding.get('combined_pnl', 0):+,.0f} profit immediately"

        )

        details["roll_action"] = (

            f"ROLL alternative: close this + open {underlying} {next_m} ${strike:,.2f}{opt_type} "

            f"(usually costs {cur_sym}{cost_per_share*0.3:.2f}–{cur_sym}{cost_per_share*0.5:.2f}/sh debit to roll; "

            f"preserves exposure if you expect {underlying} to keep moving)"

        )

        details["spread_comparison_table"] = _build_spread_comparison_html()

        details["decision_matrix_html"]    = _build_decision_matrix_html(best_spread)

        details["buy_put_candidates_html"] = _build_buy_put_candidates_html()

        details["all_strategies_html"]     = _build_all_strategies_html()

    elif pnl_pct < -50.0:

        strategy = "CLOSE"

        urgency  = "MEDIUM"

        rationale.append(f"Option lost over half its value — cut to free capital")

        details["close_action"] = (

            f"Sell {contracts}× {underlying} {expiry_str} ${strike:,.2f}{opt_type} "

            f"→ Accept {cur_sym}{holding.get('combined_pnl', 0):+,.0f} loss and exit"

        )

    elif pnl_pct >= 20.0 and opt_type == "C":

        # Long call with good profit — lock minimum gain via bull spread

        strategy = "LOCK PROFIT"

        urgency  = "HIGH" if mod_expiry else "MEDIUM"

        rationale.append(f"Lock profit by selling higher-strike call (bull spread)")

        rationale.append(f"Premium received TODAY instantly reduces cost basis")

        if best_spread:

            short_strike   = best_spread["strike"]

            short_prem_est = best_spread["prem"]

            prem_src       = best_spread["src"]

            spread_width   = best_spread["spread_width"]

            locked_floor   = best_spread["floor"]

            max_spread_pnl = best_spread["max_pnl"]

            prem_total_rcv = best_spread["prem_total"]

            sp_prem_label  = f"{cur_sym}{short_prem_est:.2f}/sh • {prem_src} quote"

        else:

            # Fallback to estimate

            spread_width   = max(5.0, round(strike * 0.015))

            short_strike   = round((strike + spread_width) * 2) / 2

            real_sp, sp_src = _fetch_option_quote(underlying, expiry_str, short_strike, "C")

            if real_sp and real_sp > 0:

                short_prem_est = real_sp

                sp_prem_label  = f"{cur_sym}{short_prem_est:.2f}/sh • {sp_src} quote"

            else:

                short_prem_est = round(current_per_share * 0.60, 2)

                sp_prem_label  = f"~{cur_sym}{short_prem_est:.2f}/sh • estimated"

            prem_total_rcv = short_prem_est * 100 * contracts

            locked_floor   = round(prem_total_rcv - total_cost, 2)

            max_spread_pnl = round(spread_width * 100 * contracts + prem_total_rcv - total_cost, 2)

        details["spread_contract"] = (

            f"Sell {contracts}× {underlying} {expiry_str} ${short_strike:,.2f}C "

            f"({sp_prem_label} = {cur_sym}{prem_total_rcv:,.0f} total received TODAY)"

        )

        floor_word = "profit" if locked_floor > 0 else "reduced loss"

        details["spread_locked_floor"] = (

            f"Worst-case floor: {cur_sym}{locked_floor:+,.0f} {floor_word} "

            f"(if {underlying} falls below ${strike:,.2f} and both calls expire OTM)"

        )

        details["spread_max_profit"] = (

            f"Max profit if {underlying} → ${short_strike:,.2f}+: {cur_sym}{max_spread_pnl:+,.0f} "

            f"(spread width ${spread_width:.0f} × {contracts} contract × 100 shares)"

        )

        if best_spread:

            details["naked_beats_spread"] = (

                f"Naked call only beats this spread if {underlying} > ${best_spread['bep']:.2f} "

                f"({(best_spread['bep'] - (spot or strike)) / (spot or strike) * 100:.1f}% rally needed)"

                if spot and spot > 0 else

                f"Naked beats spread only above ${best_spread['bep']:.2f}"

            )

        details["hold_naked_note"] = (

            f"ALTERNATIVE — hold naked call for unlimited upside if {underlying} rallies hard. "

            f"Current value {cur_sym}{combined_val:,.0f}. "

            f"Set stop: sell if option falls to {cur_sym}{combined_val*0.65:,.0f} (−35%) to protect gains."

        )

        details["scenario_table"]          = _opt_scenario_table(spread_width, short_prem_est, short_strike)

        details["spread_comparison_table"] = _build_spread_comparison_html()

        details["roll_candidates_html"]    = _build_roll_candidates_html()

        details["decision_matrix_html"]    = _build_decision_matrix_html(best_spread)

        details["buy_put_candidates_html"] = _build_buy_put_candidates_html()

        details["all_strategies_html"]     = _build_all_strategies_html()

    elif pnl_pct >= 20.0 and opt_type == "P":

        # Long put with profit — convert to bear spread

        strategy = "LOCK PROFIT"

        urgency  = "HIGH" if mod_expiry else "MEDIUM"

        rationale.append(f"Lock profit by selling lower-strike put (bear spread)")

        if best_spread:

            short_strike   = best_spread["strike"]

            short_prem_est = best_spread["prem"]

            spread_width   = best_spread["spread_width"]

            locked_floor   = best_spread["floor"]

            max_spread_pnl = best_spread["max_pnl"]

        else:

            spread_width   = max(5.0, round(strike * 0.015))

            short_strike   = round((strike - spread_width) * 2) / 2

            short_prem_est = round(current_per_share * 0.60, 2)

            locked_floor   = round(short_prem_est * 100 * contracts - total_cost, 2)

            max_spread_pnl = round(spread_width * 100 * contracts + short_prem_est * 100 * contracts - total_cost, 2)

        details["spread_contract"] = (

            f"Sell {contracts}× {underlying} {expiry_str} ${short_strike:,.2f}P "

            f"(~{cur_sym}{short_prem_est:.2f}/sh = {cur_sym}{short_prem_est*100*contracts:,.0f} total received)"

        )

        details["spread_locked_floor"] = (

            f"Worst-case floor: {cur_sym}{locked_floor:+,.0f} if {underlying} stays above ${strike:,.2f}"

        )

        details["spread_max_profit"] = (

            f"Max profit if {underlying} → ${short_strike:,.2f} or lower: {cur_sym}{max_spread_pnl:+,.0f}"

        )

        details["spread_comparison_table"] = _build_spread_comparison_html()

        details["decision_matrix_html"]    = _build_decision_matrix_html(best_spread)

        details["buy_put_candidates_html"] = _build_buy_put_candidates_html()

        details["all_strategies_html"]     = _build_all_strategies_html()

    elif near_expiry and pnl_pct >= 0:

        strategy = "CLOSE"

        urgency  = "HIGH"

        rationale.append(f"Near expiry with small profit — close to realise gain before theta kills it")

        details["close_action"] = (

            f"Sell {contracts}× {underlying} {expiry_str} ${strike:,.2f}{opt_type} "

            f"→ Realise {cur_sym}{holding.get('combined_pnl', 0):+,.0f}"

        )

        details["spread_comparison_table"] = _build_spread_comparison_html()

        details["decision_matrix_html"]    = _build_decision_matrix_html(best_spread)

        details["buy_put_candidates_html"] = _build_buy_put_candidates_html()

        details["all_strategies_html"]     = _build_all_strategies_html()

    else:

        rationale.append(f"No immediate action — monitor closely")

        if spot and spot > 0 and opt_type == "C" and spot < strike:

            gap = strike - spot

            rationale.append(

                f"⚠ {underlying} needs to rise {cur_sym}{gap:.2f} ({gap/spot*100:.1f}%) "

                f"to reach breakeven (strike ${strike:,.2f})"

            )

        details["spread_comparison_table"] = _build_spread_comparison_html()

    # ── Quick-recommendation engine: market outlook + ranked strategies ──────

    if opt_type == "C" and spot and spot > 0:

        details["market_outlook"]    = _get_market_outlook(underlying, spot)

        top_puts_ranked              = _best_puts_selection()

        # Compute option score for the existing held call position
        _pos_row = None
        if chain_calls is not None:
            try:
                _matches = chain_calls[abs(chain_calls["strike"] - strike) < 0.01]
                if not _matches.empty:
                    _pos_row = _matches.iloc[0]
            except Exception:
                pass
        _pos_iv  = (float(_pos_row["impliedVolatility"]) * 100
                    if _pos_row is not None and float(_pos_row.get("impliedVolatility") or 0) > 0
                    else None)
        _pos_vol = float(_pos_row["volume"] or 0)       if _pos_row is not None else None
        _pos_oi  = float(_pos_row["openInterest"] or 0) if _pos_row is not None else None
        _pos_bid = float(_pos_row.get("bid") or 0)      if _pos_row is not None else 0.0
        _pos_ask = float(_pos_row.get("ask") or 0)      if _pos_row is not None else 0.0
        _pos_mid = (_pos_bid + _pos_ask) / 2.0 if (_pos_bid > 0 and _pos_ask > 0) else current_per_share
        _pos_spread_pct = (_pos_ask - _pos_bid) / _pos_mid * 100 if (_pos_mid > 0 and _pos_row is not None) else None
        _opt_score_data = _compute_option_score(
            underlying, strike, spot, days_to_expiry,
            iv_pct=_pos_iv, volume=_pos_vol, oi=_pos_oi, spread_pct=_pos_spread_pct,
        )

        details["ranked_strategies"] = _rank_strategies_for_call(

            outlook           = details["market_outlook"],

            pnl_pct           = pnl_pct,

            days_to_expiry    = days_to_expiry,

            spread_candidates = spread_candidates,

            put_candidates    = top_puts_ranked,

            total_cost        = total_cost,

            combined_val      = combined_val,

            contracts         = contracts,

            strike            = strike,

            current_per_share = current_per_share,

            underlying        = underlying,

            cur_sym           = cur_sym,

            expiry_str        = expiry_str,
            option_score_data = _opt_score_data,
        )

    return {

        "strategy":  strategy,

        "urgency":   urgency,

        "rationale": rationale,

        "details":   details,

        "pcr":       None,

        "oi_type":   None,

        "regime":    "N/A (Option Position)",

        "india_vix": None,

    }


def _get_market_outlook(underlying: str, spot) -> dict:

    """

    Fast market outlook using yfinance 90-day signals.

    Returns: {"direction":"BULLISH"|"NEUTRAL"|"BEARISH", "confidence":int, "signals":[str], "score":int}

    """

    result: dict = {"direction": "NEUTRAL", "confidence": 45,

                    "signals": ["Technical analysis unavailable"], "score": 0}

    try:

        import yfinance as _yf_out

        t    = _yf_out.Ticker(underlying)

        hist = t.history(period="90d", auto_adjust=True)

        if hist.empty or len(hist) < 20:

            return result

        close = hist["Close"]

        spx   = float(spot) if spot else float(close.iloc[-1])

        # RSI(14)

        delta = close.diff()

        gain  = delta.clip(lower=0).rolling(14).mean()

        loss  = (-delta.clip(upper=0)).rolling(14).mean()

        rs    = gain / loss

        rsi   = float(100 - 100 / (1 + rs.iloc[-1]))

        # SMA trend

        sma20 = float(close.rolling(20).mean().iloc[-1])

        sma50 = float(close.rolling(min(50, len(close))).mean().iloc[-1])

        # 52-wk high proximity

        high_52w     = float(close.max())

        pct_frm_high = (spx - high_52w) / high_52w * 100 if high_52w > 0 else 0

        # Analyst recommendation

        analyst_score = 0

        analyst_label = ""

        try:

            fi      = t.fast_info

            rec_str = (getattr(fi, "recommendation_key", None) or "").lower()

            analyst_score = {"strongbuy": 2, "buy": 1, "hold": 0,

                             "sell": -1, "strongsell": -2}.get(rec_str, 0)

            analyst_label = rec_str.replace("strong", "STRONG ") if rec_str else ""

        except Exception:

            pass

        score   = 0

        signals = []

        # RSI signal

        if rsi >= 75:

            score -= 25; signals.append(f"RSI {rsi:.0f} — Overbought ⚠ (bearish)")

        elif rsi >= 60:

            score += 8;  signals.append(f"RSI {rsi:.0f} — Bullish momentum")

        elif rsi <= 30:

            score += 25; signals.append(f"RSI {rsi:.0f} — Oversold (bullish reversal)")

        elif rsi <= 40:

            score -= 8;  signals.append(f"RSI {rsi:.0f} — Weakening momentum")

        else:

            signals.append(f"RSI {rsi:.0f} — Neutral zone")

        # Trend

        if spx > sma20 and sma20 > sma50:

            score += 20; signals.append(f"Uptrend: price above SMA20 & SMA50")

        elif spx < sma20 and sma20 < sma50:

            score -= 20; signals.append(f"Downtrend: price below SMA20 & SMA50")

        elif spx > sma20:

            score += 8;  signals.append(f"Short-term bullish: above SMA20 (${sma20:.2f})")

        else:

            score -= 8;  signals.append(f"Short-term bearish: below SMA20 (${sma20:.2f})")

        # 52-wk high proximity

        if pct_frm_high >= -2:

            score -= 10; signals.append(f"Near 52-week high — limited near-term catalyst")

        elif pct_frm_high <= -20:

            score += 10; signals.append(f"{abs(pct_frm_high):.0f}% below 52-week high — recovery potential")

        # Analyst consensus

        if analyst_score >= 1:

            score += 15; signals.append(f"Analysts: {analyst_label or 'BUY'} rating")

        elif analyst_score <= -1:

            score -= 15; signals.append(f"Analysts: {analyst_label or 'SELL'} rating")

        # Determine direction + confidence

        if score >= 20:

            direction, confidence = "BULLISH",  min(90, 50 + score)

        elif score <= -20:

            direction, confidence = "BEARISH",  min(90, 50 + abs(score))

        else:

            direction, confidence = "NEUTRAL",  max(40, 50 - abs(score) // 2)

        result = {"direction": direction, "confidence": int(confidence),

                  "signals": signals, "score": score}

    except Exception:

        pass

    return result


# === INSERTED BY wire_score_provider.py ===================================

# Signal scoring is now handled by HedgerScoreProvider (SOLID architecture).

# Priority: DB composite (L1+L2+L3+L4) > Live AnalysisEngine > L1 partial

# See: tradingview/screeners/hedger_score_provider.py

# ==========================================================================


_SCORE_PROVIDER = None   # HedgerScoreProvider | None (lazy, per-run singleton)

def _get_score_provider():

    global _SCORE_PROVIDER

    if _SCORE_PROVIDER is None:

        try:

            import sys as _sp_sys

            _sp_dir = str(_pathlib.Path(__file__).parent)

            if _sp_dir not in _sp_sys.path:

                _sp_sys.path.insert(0, _sp_dir)

            from hedger_score_provider import HedgerScoreProvider

            _SCORE_PROVIDER = HedgerScoreProvider.default()

        except Exception:

            _SCORE_PROVIDER = None

    return _SCORE_PROVIDER

def _reset_score_provider() -> None:

    global _SCORE_PROVIDER

    _SCORE_PROVIDER = None

def _prefetch_live_scores(underlyings: list) -> None:

    provider = _get_score_provider()

    if provider is None:

        return

    try:

        import sys as _pf_sys

        _pf_dir = str(_pathlib.Path(__file__).parent)

        if _pf_dir not in _pf_sys.path:

            _pf_sys.path.insert(0, _pf_dir)

        from hedger_score_provider import resolve_tv_symbols_from_db

        tv_sym_map = resolve_tv_symbols_from_db(underlyings)

        provider.prefetch(underlyings, tv_symbol_map=tv_sym_map)

    except Exception:

        pass

def _get_db_signal_score(underlying: str):

    provider = _get_score_provider()

    if provider is None:

        return None

    try:

        import sys as _db_sys

        _db_dir = str(_pathlib.Path(__file__).parent)

        if _db_dir not in _db_sys.path:

            _db_sys.path.insert(0, _db_dir)

        from hedger_score_provider import DbScoreSource

        record = DbScoreSource().get_score(underlying, tv_symbol=f"NASDAQ:{underlying}")

        return record.to_dict() if record is not None else None

    except Exception:

        return None

def _get_signal_score(underlying: str):

    provider = _get_score_provider()

    if provider is None:

        return None

    try:

        import sys as _gs_sys

        _gs_dir = str(_pathlib.Path(__file__).parent)

        if _gs_dir not in _gs_sys.path:

            _gs_sys.path.insert(0, _gs_dir)

        from hedger_score_provider import resolve_tv_symbols_from_db

        tv_sym_map = resolve_tv_symbols_from_db([underlying])

        record = provider.get(underlying, tv_symbol=tv_sym_map.get(underlying))

        return record.to_dict() if record is not None else None

    except Exception:

        return None

def _compute_option_score(
    underlying: str,
    strike: float,
    spot,
    days_to_expiry: int,
    iv_pct=None,
    volume=None,
    oi=None,
    spread_pct=None,
) -> dict:
    """Compute option score for an existing held position using the same 5-factor
    formula as options_analyzer.py.  Falls back to neutral (5.0) for any missing
    market data so the score is still meaningful via strike-fit + DTE + signal."""
    try:
        import sys as _oa_sys
        _oa_dir = str(_pathlib.Path(__file__).parent.parent)  # tradingview/
        if _oa_dir not in _oa_sys.path:
            _oa_sys.path.insert(0, _oa_dir)
        from options.options_analyzer import (  # type: ignore
            _composite_option_score, _iv_score, _strike_score,
            _dte_score, _liquidity_score, MODE_CONFIGS, StrategyMode,
        )
    except Exception:
        return {}

    try:
        sig_data = _get_signal_score(underlying)
        raw_sig  = (sig_data.get("score", 50) if isinstance(sig_data, dict) else 50) or 50
        sig      = float(raw_sig) / 10.0   # 0–100 → 0–10

        cfg = MODE_CONFIGS[StrategyMode.AGGRESSIVE]

        moneyness_pct = (strike - spot) / spot * 100 if (spot and spot > 0) else 0.0
        str_s = _strike_score(moneyness_pct, cfg)
        dte_s = _dte_score(days_to_expiry, cfg)

        if iv_pct is not None:
            iv_s, _ = _iv_score(iv_pct)
        else:
            iv_s = 5.0  # neutral

        if volume is not None and oi is not None and spread_pct is not None:
            liq_s = _liquidity_score(volume, oi, spread_pct, "US", cfg)
        else:
            liq_s = 5.0  # neutral

        composite = _composite_option_score(sig, liq_s, iv_s, str_s, dte_s)
        return {
            "score":  composite,
            "liq":    round(liq_s, 1),
            "iv":     round(iv_s,  1),
            "strike": round(str_s, 1),
            "dte":    round(dte_s, 1),
            "signal": round(raw_sig, 1),
        }
    except Exception:
        return {}


def _rank_strategies_for_call(

    *,

    outlook: dict,

    pnl_pct: float,

    days_to_expiry: int,

    spread_candidates: list,

    put_candidates: list,

    total_cost: float,

    combined_val: float,

    contracts: int,

    strike: float,

    current_per_share: float,

    underlying: str,

    cur_sym: str,

    expiry_str: str,
    option_score_data=None,
) -> list:

    """

    Score all available strategies and return the top 3 for the quick-card.

    Scoring: higher = better given current outlook + position state.

    """

    direction  = outlook.get("direction", "NEUTRAL")

    confidence = outlook.get("confidence", 50)

    cur_pnl    = combined_val - total_cost

    near_exp   = days_to_expiry <= 7

    mod_exp    = days_to_expiry <= 21

    best_spread = None

    pos_floor   = [c for c in spread_candidates if c.get("floor", 0) > 0]

    if pos_floor:

        best_spread = max(pos_floor, key=lambda r: r.get("ratio", 0))

    elif spread_candidates:

        best_spread = max(spread_candidates, key=lambda r: r.get("ratio", 0))

    best_put = put_candidates[0] if put_candidates else None

    strategies: list = []

    # ── 1. CLOSE NOW ──────────────────────────────────────────────────────────

    sc = 0

    if near_exp:              sc += 45

    elif mod_exp:             sc += 20

    if direction == "BEARISH":   sc += 35

    elif direction == "NEUTRAL": sc += 15

    if pnl_pct >= 200:  sc += 20

    elif pnl_pct >= 100: sc += 10

    why = []

    if near_exp: why.append(f"only {days_to_expiry}d left — theta risk is severe")

    if direction == "BEARISH": why.append("system outlook: BEARISH")

    if pnl_pct >= 100: why.append(f"{pnl_pct:.0f}% gain is strong — lock it")

    strategies.append({

        "id": "CLOSE", "icon": "🔒", "score": sc,

        "name": "Close Now — Lock ALL profit",

        "action": f"Sell {contracts}× {underlying} ${strike:.0f}C @ {cur_sym}{current_per_share:.2f}/sh",

        "floor_label": "Profit locked",  "floor_val": cur_pnl,

        "gain_label":  "You receive",    "gain_val":  cur_pnl,

        "cost_label":  "Cost",           "cost_val":  0.0,

        "cost_display": "—",

        "why": " · ".join(why) if why else "Safest: lock entire profit immediately",

        "tradeoff": "Zero future upside after closing",

        "signal_score": _get_signal_score(underlying),
        "option_score_data": option_score_data,
    })

    # ── 2. BULL SPREAD (sell OTM call) ────────────────────────────────────────

    if best_spread:

        sc = 45

        if direction in ("NEUTRAL", "BEARISH"): sc += 25

        if near_exp:   sc += 20

        elif mod_exp:  sc += 10

        if pnl_pct >= 50: sc += 10

        if direction == "BULLISH" and confidence >= 70: sc -= 20

        floor_pct = best_spread.get("floor", 0) / cur_pnl * 100 if cur_pnl > 0 else 0

        why = [f"locks {floor_pct:.0f}% of current gain as guaranteed floor"]

        if direction in ("NEUTRAL", "BEARISH"):

            why.append(f"{direction.lower()} outlook — smart to cap upside and lock floor")

        strategies.append({

            "id": "BULL_SPREAD", "icon": "📊", "score": sc,

            "name": f"Sell ${best_spread['strike']:.0f}C — Guaranteed Floor",

            "action": f"Sell {contracts}× {underlying} ${best_spread['strike']:.0f}C @ {cur_sym}{best_spread['prem']:.2f}/sh",

            "floor_label": "Guaranteed floor",   "floor_val": best_spread.get("floor", 0),

            "gain_label":  "Max gain (capped)",  "gain_val":  best_spread.get("max_pnl"),

            "cost_label":  "Receive",            "cost_val":  best_spread.get("prem_total", 0),

            "cost_display": f"+{cur_sym}{best_spread.get('prem_total',0):.0f} credit",

            "why": " · ".join(why),

            "tradeoff": f"Upside capped at ${best_spread['strike']:.0f}",
            "signal_score": _get_signal_score(underlying),
            "option_score_data": option_score_data,
        })

    # ── 3. BUY PUT — floor + unlimited upside ─────────────────────────────────

    # Guard: only show if put cost ≤ original call premium. Paying more than the

    # original call premium to hedge it adds more fresh capital at risk — that is

    # the OPPOSITE of hedging and should never be recommended.

    if best_put and best_put.get("put_total", 0) <= total_cost:

        sc = 30

        if direction == "BULLISH":   sc += 45

        elif direction == "NEUTRAL": sc += 20

        elif direction == "BEARISH": sc -= 5

        if near_exp:   sc -= 25

        elif mod_exp:  sc -= 5

        if pnl_pct >= 50: sc += 15

        if pnl_pct >= 200: sc += 10

        put_cost_pct = best_put.get("put_total", 0) / cur_pnl * 100 if cur_pnl > 0 else 0

        why = [f"floor + UNLIMITED upside if {underlying} keeps rallying"]

        if direction == "BULLISH":

            why.append("system is BULLISH — keep unlimited gains, add downside protection")

        why.append(f"costs {put_cost_pct:.0f}% of current unrealised gain")

        strategies.append({

            "id": "BUY_PUT", "icon": "🛡", "score": sc,

            "name": f"Buy ${best_put['strike']:.0f}P — Floor + Unlimited",

            "action": f"Buy {contracts}× {underlying} ${best_put['strike']:.0f}P @ {cur_sym}{best_put['prem']:.2f}/sh = {cur_sym}{best_put['put_total']:.0f}",

            "floor_label": "Max loss",   "floor_val": best_put.get("max_loss"),

            "gain_label":  "Upside",     "gain_val":  None,

            "cost_label":  "Put cost",   "cost_val":  best_put.get("put_total", 0),

            "cost_display": f"−{cur_sym}{best_put.get('put_total',0):.0f} debit",

            "why": " · ".join(why),

            "tradeoff": f"Costs {cur_sym}{best_put.get('put_total',0):.0f} ({put_cost_pct:.0f}% of gain)",
            "signal_score": _get_signal_score(underlying),
            "option_score_data": option_score_data,
        })

    # ── 4. HOLD NAKED CALL (last resort unless strongly bullish) ──────────────

    sc = 15

    if direction == "BULLISH" and confidence >= 70: sc += 45

    elif direction == "BULLISH":                    sc += 25

    elif direction == "BEARISH":                    sc -= 30

    if near_exp:        sc -= 40

    elif mod_exp:       sc -= 10

    if pnl_pct >= 200:  sc -= 15

    why = []

    if direction == "BULLISH": why.append("system is BULLISH — ride the momentum")

    if days_to_expiry > 21: why.append(f"{days_to_expiry}d remaining — time on your side")

    strategies.append({

        "id": "HOLD", "icon": "⚡", "score": sc,

        "name": "Hold Naked Call — Full upside",

        "action": "Do nothing — hold current position",

        "floor_label": "Max loss",  "floor_val": -total_cost,

        "gain_label":  "Upside",    "gain_val":  None,

        "cost_label":  "Cost",      "cost_val":  0.0,

        "cost_display": "—",

        "why": " · ".join(why) if why else "Full risk, full reward — no hedge",

        "tradeoff": f"Risk losing full {cur_sym}{total_cost:.0f} if {underlying} reverses",
        "signal_score": _get_signal_score(underlying),
        "option_score_data": option_score_data,
    })

    # Drop strategies with a negative score — they are actively harmful in context

    strategies = [s for s in strategies if s["score"] > 0]

    strategies.sort(key=lambda s: s["score"], reverse=True)

    rank_labels = ["🥇 Top Pick", "🥈 Alternative", "🥉 Also Consider"]

    for i, s in enumerate(strategies[:3]):

        s["rank_label"] = rank_labels[i] if i < len(rank_labels) else ""

    return strategies[:3]

