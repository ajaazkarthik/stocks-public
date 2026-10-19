"""
India Order Executor — Zerodha Kite Connect Order Placement
============================================================
Drops into the same pattern as ibkr/order_executor.py and the Moomoo
place_moomoo_order() function in webhook_server.py.

KEY DIFFERENTIATOR vs Moomoo/IBKR:
  After placing a BUY, this executor AUTOMATICALLY places a GTT OCO order
  (stop-loss + take-profit) that PERSISTS across sessions — no manual
  stop-loss management ever needed again.

Flow:
  TV BUY alert fires (NSE symbol) 
    → webhook_server.py receives POST
    → SignalStack composite ≥ threshold
    → place_india_order(symbol, ltp, atr) is called
        → calculates position size from available margin
        → places MARKET BUY on Zerodha
        → immediately places GTT TWO-LEG (stop + target)
    → order_id + gtt_trigger_id logged to actionable report

Supports:
  NSE and BSE equity (CNC product — delivery based, held until sold)

Requirements:
  pip install kiteconnect
  ENV: ZERODHA_ACCESS_TOKEN  (mandatory)
  ENV: ZERODHA_API_KEY       (default: a9jb8rdr88cr8a9s)

Standalone test:
  python india_order_executor.py --symbol NSE:INFY --dry-run
  python india_order_executor.py --symbol NSE:INFY --amount 10000  # live
"""

from __future__ import annotations

import json
import os
import sys
import time
import argparse
import traceback
from typing import Optional

API_KEY = os.environ.get("ZERODHA_API_KEY", "")  # nocheck: J1 — fallback required, system won't connect without it, DO NOT REMOVE

# ─── Internal helpers ────────────────────────────────────────────────────────

def _get_kite():
    """Return authenticated KiteConnect instance via the common auth module."""
    from zerodha_auth import get_kite
    return get_kite()


def _resolve_exchange_and_symbol(tv_symbol: str) -> tuple[str, str]:
    """
    Convert TradingView pro_symbol to (exchange, tradingsymbol).
    NSE:INFY → ('NSE', 'INFY')
    BSE:500209 → ('BSE', '500209')
    INFY → ('NSE', 'INFY')  # bare symbol defaults to NSE
    """
    if ":" in tv_symbol:
        exchange, sym = tv_symbol.split(":", 1)
        exchange = exchange.upper()
        if exchange not in ("NSE", "BSE"):
            raise ValueError(
                f"'{exchange}' is not a supported Indian exchange. "
                "Only NSE and BSE are supported."
            )
        return exchange, sym.upper()
    return "NSE", tv_symbol.upper()


def _get_current_ltp(kite, exchange: str, symbol: str) -> float:
    """
    Fetch live LTP from Zerodha.
    Note: kite.ltp() requires Quote API subscription. Falls back to
    holdings last_price for already-held instruments, or raises.
    In real usage (webhook_server.py), current_price is always passed
    directly from the TradingView alert — this fallback is rarely needed.
    """
    # First try kite.ltp (needs Quote subscription)
    try:
        result = kite.ltp(f"{exchange}:{symbol}")
        key = f"{exchange}:{symbol}"
        if result and key in result:
            return float(result[key]["last_price"])
    except Exception:
        pass

    # Fallback: scan holdings for last_price
    try:
        holdings = kite.holdings()
        for h in holdings:
            if h.get("tradingsymbol", "").upper() == symbol.upper():
                ltp = float(h.get("last_price", 0) or 0)
                if ltp > 0:
                    return ltp
    except Exception:
        pass

    raise ValueError(
        f"Could not fetch LTP for {exchange}:{symbol}. "
        "Pass current_price explicitly (TV alert always provides it)."
    )


def _get_available_cash(kite) -> float:
    """Return available cash in the equity segment."""
    try:
        m = kite.margins(segment="equity")
        return float((m.get("available") or {}).get("cash", 0) or 0)
    except Exception:
        try:
            m = kite.margins()
            eq = m.get("equity", {})
            return float((eq.get("available") or {}).get("cash", 0) or 0)
        except Exception:
            return 0.0


def _calc_qty(amount_inr: float, ltp: float) -> int:
    """
    Compute quantity for a given spend amount.
    Always buys whole lots; minimum 1.
    """
    if ltp <= 0:
        return 0
    return max(1, int(amount_inr / ltp))


def _calc_gtt_levels(avg_price: float, ltp: float, atr: Optional[float] = None) -> dict:
    """
    Re-uses the same ATR/percentage logic from india_gtt_manager.
    Placed here too for standalone use without importing that file.
    """
    pct_stop   = 0.08 * avg_price
    pct_target = 0.12 * avg_price

    atr_stop   = 2.0 * atr if atr else 0
    atr_target = 3.0 * atr if atr else 0

    stop_gap   = max(atr_stop,   pct_stop)
    target_gap = max(atr_target, pct_target)

    stop_price   = round(ltp      - stop_gap,   1)
    target_price = round(avg_price + target_gap, 1)

    stop_price   = min(stop_price,   ltp * 0.95)
    target_price = max(target_price, ltp * 1.05)

    return {
        "stop_price":    stop_price,
        "stop_limit":    round(stop_price   * 0.995, 1),
        "target_price":  target_price,
        "target_limit":  round(target_price * 1.005, 1),
        "rr_ratio":      round((target_price - avg_price) / (avg_price - stop_price), 2)
                         if avg_price > stop_price else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
class IndiaOrderExecutor:
    """
    Drop-in order executor for NSE/BSE stocks via Zerodha Kite Connect.

    After every successful BUY, automatically places a GTT OCO (stop + target),
    giving complete set-and-forget risk management.

    Usage in webhook_server.py:
        from india_brokers.india_order_executor import place_india_order
        result = place_india_order(tv_sym, current_price, amount_inr=10000)
    """

    def __init__(self, kite=None):
        self._kite = kite  # injected or lazy-loaded

    @property
    def kite(self):
        if self._kite is None:
            self._kite = _get_kite()
        return self._kite

    # ── Main public method ───────────────────────────────────────────────────

    def execute(
        self,
        tv_symbol: str,
        current_price: Optional[float] = None,
        amount_inr: float = 10_000,
        atr: Optional[float] = None,
        place_gtt: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """
        Execute a BUY and auto-set GTT stop/target.

        Args:
            tv_symbol:     TradingView pro_symbol, e.g. 'NSE:INFY'
            current_price: LTP (fetched live if None)
            amount_inr:    Capital to deploy (₹). Position size = floor(amount / ltp)
            atr:           ATR value for stop/target sizing (uses % fallback if None)
            place_gtt:     Whether to auto-place GTT OCO after buy
            dry_run:       If True, simulate only — no real orders placed

        Returns dict with keys:
            success, order_id, qty, ltp, amount_spent,
            gtt_trigger_id, gtt_stop, gtt_target, error
        """
        result: dict = {
            "success":       False,
            "order_id":      None,
            "qty":           0,
            "ltp":           0.0,
            "exchange":      None,
            "symbol":        None,
            "amount_spent":  0.0,
            "gtt_trigger_id": None,
            "gtt_stop":      None,
            "gtt_target":    None,
            "error":         None,
            "dry_run":       dry_run,
        }

        # ── Watchdog gate: refuse live orders when Zerodha is offline ──
        if not dry_run:
            try:
                _wd_dir = str(Path(__file__).resolve().parent.parent / "tradingview")
                if _wd_dir not in sys.path:
                    sys.path.insert(0, _wd_dir)
                from platform_watchdog import is_healthy
                if not is_healthy("Zerodha"):
                    result["error"] = "Zerodha watchdog health check failed — refusing live order"
                    print(f"  [india_executor] ⛔ {result['error']}", flush=True)
                    return result
            except ImportError:
                pass  # watchdog not available — proceed without gate

        try:
            exchange, symbol = _resolve_exchange_and_symbol(tv_symbol)
            result["exchange"] = exchange
            result["symbol"]   = symbol

            # ── Get LTP ────────────────────────────────────────────────────
            if current_price and current_price > 0:
                ltp = float(current_price)
            else:
                try:
                    ltp = _get_current_ltp(self.kite, exchange, symbol)
                except (ValueError, Exception):
                    if dry_run:
                        ltp = 100.0  # dry-run fallback when broker offline
                        print(f"  [india_executor] ⚠ dry-run: LTP unavailable, using ₹{ltp:.2f}", flush=True)
                    else:
                        raise
            result["ltp"] = ltp

            # ── CF3: Drift gate — re-quote and reject if price moved too much ─
            _MAX_DRIFT_PCT = float(os.environ.get("INDIA_MAX_DRIFT_PCT", "1.5"))
            if not dry_run:
                live_ltp = _get_current_ltp(self.kite, exchange, symbol)
                drift_pct = abs(live_ltp - ltp) / ltp * 100
                if drift_pct > _MAX_DRIFT_PCT:
                    result["error"] = (
                        f"Price drifted {drift_pct:.2f}% "
                        f"(alert={ltp:.2f} live={live_ltp:.2f}) — "
                        f"order blocked (limit {_MAX_DRIFT_PCT:.1f}%)"
                    )
                    print(f"  [india_executor] ⛔ Drift gate: {result['error']}", flush=True)
                    return result
                ltp = live_ltp
                result["ltp"] = ltp

            # ── Check available cash ────────────────────────────────────────
            available_cash = _get_available_cash(self.kite)
            result["available_cash"] = round(available_cash, 2)

            # ── Regime gate: scale position size by market regime ─────────
            # BUG-NEW-5 fix (2026-05-01): india_regime.py had zero production
            # callers — wire it here so BEARISH regimes shrink position size.
            regime_multiplier = 1.0
            try:
                _regime_dir = str(Path(__file__).resolve().parent)
                if _regime_dir not in sys.path:
                    sys.path.insert(0, _regime_dir)
                from india_regime import get_regime  # type: ignore
                _regime = get_regime()
                regime_multiplier = _regime.get("size_multiplier", 1.0)
                _regime_name = _regime.get("regime", "UNKNOWN")
                if _regime_name == "BEARISH":
                    print(
                        f"  [india_executor] ⚠️  India regime BEARISH "
                        f"(score={_regime.get('score', 0):.1f}, "
                        f"threshold_adj={_regime.get('threshold_adj', 0):+d}) — "
                        f"position size scaled by {regime_multiplier:.1f}×",
                        flush=True,
                    )
                elif _regime_name == "BULLISH":
                    print(
                        f"  [india_executor] ✅ India regime BULLISH "
                        f"(score={_regime.get('score', 0):.1f}) — "
                        f"position size scaled by {regime_multiplier:.1f}×",
                        flush=True,
                    )
            except Exception as _re:
                # Never block an order on regime failure — fail-open
                pass

            # Cap spend at 95% of available cash
            actual_spend = min(amount_inr * regime_multiplier, available_cash * 0.95)
            if actual_spend < ltp:
                result["error"] = (
                    f"Insufficient cash: need ₹{ltp:,.2f} for 1 share, "
                    f"have ₹{available_cash:,.2f} available"
                )
                return result

            qty = _calc_qty(actual_spend, ltp)
            result["qty"]          = qty
            result["amount_spent"] = round(ltp * qty, 2)

            print(
                f"  [india_executor] {'DRY RUN ' if dry_run else ''}"
                f"{exchange}:{symbol}  ltp=₹{ltp:,.2f}  "
                f"cash=₹{available_cash:,.0f}  spend=₹{actual_spend:,.0f}  qty={qty}",
                flush=True,
            )

            # ── Place BUY order ─────────────────────────────────────────────
            if not dry_run:
                _order_kwargs = dict(
                    variety   = self.kite.VARIETY_REGULAR,
                    exchange  = exchange,
                    tradingsymbol = symbol,
                    transaction_type = self.kite.TRANSACTION_TYPE_BUY,
                    quantity  = qty,
                    product   = self.kite.PRODUCT_CNC,        # delivery
                    order_type= self.kite.ORDER_TYPE_MARKET,
                )
                # kiteconnect >=5.2.0: auto-split large orders at NSE freeze limits
                NSE_FREEZE_LIMIT = 100_000
                if qty > NSE_FREEZE_LIMIT and hasattr(self.kite, "place_autoslice_order"):
                    order_id = self.kite.place_autoslice_order(**_order_kwargs)
                else:
                    order_id = self.kite.place_order(**_order_kwargs)
                result["order_id"] = str(order_id)
                result["success"]  = True
                print(f"  [india_executor] ✅ BUY order placed — order_id={order_id}", flush=True)

                # ── CF4: Poll for fill confirmation before placing GTT ──────
                if place_gtt:
                    _FILL_POLL_TIMEOUT = int(os.environ.get("INDIA_FILL_POLL_SECS", "30"))
                    filled_qty = 0
                    order_status = ""
                    for _attempt in range(_FILL_POLL_TIMEOUT):
                        time.sleep(1)
                        try:
                            for _o in (self.kite.orders() or []):
                                if str(_o.get("order_id")) == str(order_id):
                                    filled_qty   = int(_o.get("filled_quantity") or 0)
                                    order_status = _o.get("status", "")
                                    break
                        except Exception:
                            pass
                        if order_status in ("COMPLETE", "REJECTED", "CANCELLED"):
                            break
                        if filled_qty >= qty:
                            break

                    print(
                        f"  [india_executor] Fill poll: "
                        f"status={order_status} filled={filled_qty}/{qty} "
                        f"(polled {_attempt + 1}s)",
                        flush=True,
                    )
                    if order_status in ("REJECTED", "CANCELLED") or filled_qty == 0:
                        result["error"] = (
                            f"Order {order_id} status={order_status} "
                            f"filled_qty=0 — GTT skipped"
                        )
                        print(f"  [india_executor] ⛔ {result['error']}", flush=True)
                        return result  # position not opened — nothing to protect
                    if filled_qty < qty:
                        print(
                            f"  [india_executor] ⚠️  Partial fill "
                            f"({filled_qty}/{qty}) — GTT qty adjusted to actual fill",
                            flush=True,
                        )
                        qty = filled_qty
                        result["qty"] = qty
            else:
                result["order_id"] = "DRY_RUN"
                result["success"]  = True
                print(f"  [india_executor] 🧪 DRY RUN — would buy {qty}×{symbol} @ ₹{ltp:,.2f}",
                      flush=True)

            # ── Place GTT OCO ───────────────────────────────────────────────
            if place_gtt:
                levels = _calc_gtt_levels(ltp, ltp, atr)  # avg_price = ltp on entry
                result["gtt_stop"]   = levels["stop_price"]
                result["gtt_target"] = levels["target_price"]

                # F3 (audit finding): Validate trigger range before GTT placement
                # Prevents silent GTT rejections by NSE/BSE exchange validation
                try:
                    _tr_range = self.kite.trigger_range(exchange, symbol, "BUY")
                    _stop  = levels["stop_price"]
                    _lower = _tr_range.get("lower", 0)
                    _upper = _tr_range.get("upper", float("inf"))
                    if _lower > 0 and not (_lower <= _stop <= _upper):
                        print(
                            f"  [india_executor] ⚠️  GTT stop ₹{_stop:,.2f} outside "
                            f"trigger_range [{_lower}, {_upper}] — adjusting to boundary",
                            flush=True,
                        )
                        levels["stop_price"]  = max(_lower, min(_stop, _upper))
                        levels["stop_limit"]  = levels["stop_price"] * 0.995
                except Exception as _tr_exc:
                    print(f"  [india_executor] ⚠️  trigger_range check skipped: {_tr_exc}", flush=True)

                print(
                    f"  [india_executor] GTT OCO → stop=₹{levels['stop_price']:,.2f}  "
                    f"target=₹{levels['target_price']:,.2f}  R:R={levels['rr_ratio']}",
                    flush=True,
                )

                if not dry_run:
                    try:
                        gtt_id = self.kite.place_gtt(
                            trigger_type  = self.kite.GTT_TYPE_TWO_LEG,
                            tradingsymbol = symbol,
                            exchange      = exchange,
                            trigger_values= [levels["stop_price"], levels["target_price"]],
                            last_price    = ltp,
                            orders = [
                                {
                                    "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                                    "quantity":   qty,
                                    "order_type": self.kite.ORDER_TYPE_LIMIT,
                                    "product":    self.kite.PRODUCT_CNC,
                                    "price":      levels["stop_limit"],
                                },
                                {
                                    "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                                    "quantity":   qty,
                                    "order_type": self.kite.ORDER_TYPE_LIMIT,
                                    "product":    self.kite.PRODUCT_CNC,
                                    "price":      levels["target_limit"],
                                },
                            ],
                        )
                        result["gtt_trigger_id"] = gtt_id
                        print(f"  [india_executor] ✅ GTT OCO placed — trigger_id={gtt_id}", flush=True)
                    except Exception as gtt_exc:
                        # GTT failure is non-fatal — order already placed
                        result["gtt_error"] = str(gtt_exc)
                        print(f"  [india_executor] ⚠️  GTT placement failed (order still live): {gtt_exc}",
                              flush=True)
                        # Try single-leg stop-loss as fallback
                        try:
                            gtt_id = self.kite.place_gtt(
                                trigger_type  = self.kite.GTT_TYPE_SINGLE,
                                tradingsymbol = symbol,
                                exchange      = exchange,
                                trigger_values= [levels["stop_price"]],
                                last_price    = ltp,
                                orders = [{
                                    "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                                    "quantity":   qty,
                                    "order_type": self.kite.ORDER_TYPE_LIMIT,
                                    "product":    self.kite.PRODUCT_CNC,
                                    "price":      levels["stop_limit"],
                                }],
                            )
                            result["gtt_trigger_id"] = gtt_id
                            print(f"  [india_executor] ✅ Fallback stop-only GTT placed — id={gtt_id}",
                                  flush=True)
                        except Exception as gtt2_exc:
                            print(f"  [india_executor] ✗ Fallback GTT also failed: {gtt2_exc}",
                                  flush=True)
                else:
                    result["gtt_trigger_id"] = "DRY_RUN"
                    print("  [india_executor] 🧪 DRY RUN — GTT OCO would be placed", flush=True)

        except Exception as exc:
            result["error"] = str(exc)
            traceback.print_exc()

        return result

    # ── Sell (close position) ────────────────────────────────────────────────

    def close_position(
        self,
        tv_symbol: str,
        qty: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Sell a position (full or partial). Also cancels active GTTs for the symbol.
        """
        result: dict = {"success": False, "order_id": None, "error": None}
        try:
            exchange, symbol = _resolve_exchange_and_symbol(tv_symbol)

            # Determine quantity from holdings if not specified
            if qty is None:
                holdings = self.kite.holdings()
                for h in holdings:
                    if h.get("tradingsymbol", "").upper() == symbol:
                        qty = int(h.get("quantity", 0) or 0)
                        break
            if not qty or qty <= 0:
                result["error"] = f"No holdings found for {symbol}"
                return result

            ltp = _get_current_ltp(self.kite, exchange, symbol)
            result["ltp"] = ltp
            result["qty"] = qty

            print(f"  [india_executor] {'DRY RUN ' if dry_run else ''}SELL {qty}×{symbol} "
                  f"@ ~₹{ltp:,.2f}", flush=True)

            if not dry_run:
                # ── CF4: Place SELL first; only cancel GTTs after confirmed submission
                _order_kwargs = dict(
                    variety  = self.kite.VARIETY_REGULAR,
                    exchange = exchange,
                    tradingsymbol = symbol,
                    transaction_type = self.kite.TRANSACTION_TYPE_SELL,
                    quantity  = qty,
                    product   = self.kite.PRODUCT_CNC,
                    order_type= self.kite.ORDER_TYPE_MARKET,
                )
                # kiteconnect >=5.2.0: auto-split large orders at NSE freeze limits
                NSE_FREEZE_LIMIT = 100_000
                if qty > NSE_FREEZE_LIMIT and hasattr(self.kite, "place_autoslice_order"):
                    order_id = self.kite.place_autoslice_order(**_order_kwargs)
                else:
                    order_id = self.kite.place_order(**_order_kwargs)
                result["order_id"] = str(order_id)
                result["success"]  = True
                print(f"  [india_executor] ✅ SELL order placed — order_id={order_id}", flush=True)

                # Cancel GTTs now that SELL has been submitted
                try:
                    gtts = self.kite.get_gtts() or []
                    for g in gtts:
                        if g.get("condition", {}).get("tradingsymbol", "").upper() == symbol:
                            self.kite.delete_gtt(g["id"])
                            print(f"  [india_executor] Cancelled GTT {g['id']} for {symbol}",
                                  flush=True)
                except Exception as _gtt_del_err:
                    # Non-fatal: SELL succeeded; log, but don't fail the result
                    print(f"  [india_executor] ⚠️  GTT cancellation error (SELL still placed): "
                          f"{_gtt_del_err}", flush=True)
            else:
                result["order_id"] = "DRY_RUN"
                result["success"]  = True

        except Exception as exc:
            result["error"] = str(exc)

        return result


# ─── Module-level function (matches webhook_server.py pattern) ────────────────

_executor: Optional[IndiaOrderExecutor] = None

def place_india_order(
    tv_sym: str,
    current_price: Optional[float],
    amount_inr: float = 10_000,
    atr: Optional[float] = None,
    dry_run: bool = False,
    kite=None,
) -> dict:
    """
    Module-level convenience function — mirrors place_moomoo_order() signature.
    Called from webhook_server.py for NSE/BSE symbols.

    Returns dict with success/order_id/gtt_trigger_id/error keys.
    """
    global _executor
    if _executor is None or kite is not None:
        _executor = IndiaOrderExecutor(kite=kite)
    return _executor.execute(
        tv_symbol=tv_sym,
        current_price=current_price,
        amount_inr=amount_inr,
        atr=atr,
        dry_run=dry_run,
    )


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  pass  # cleanup

    parser = argparse.ArgumentParser(
        description="India Order Executor — place BUY + GTT on Zerodha"
    )
    parser.add_argument("--symbol",  required=True,
                        help="TradingView symbol, e.g. NSE:INFY or BSE:500209")
    parser.add_argument("--amount",  type=float, default=10000,
                        help="Amount in INR to deploy (default: ₹10,000)")
    parser.add_argument("--atr",     type=float, default=None,
                        help="ATR value for stop/target sizing (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate only — no real orders placed")
    parser.add_argument("--sell",    action="store_true",
                        help="Close (sell) the position instead of buying")
    parser.add_argument("--qty",     type=int, default=None,
                        help="Quantity for sell (auto-detects from holdings if not set)")
    args = parser.parse_args()

    executor = IndiaOrderExecutor()

    if args.sell:
        result = executor.close_position(args.symbol, qty=args.qty, dry_run=args.dry_run)
    else:
        result = executor.execute(
            tv_symbol=args.symbol,
            amount_inr=args.amount,
            atr=args.atr,
            dry_run=args.dry_run,
        )

    print()
    print("Result:", json.dumps(result, indent=2, default=str))
