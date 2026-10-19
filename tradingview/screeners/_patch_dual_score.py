"""One-shot patch script: add dual score (signal + option) to hedging advisor report."""
import re
import pathlib

SRC = pathlib.Path(__file__).parent / "options_hedger.py"

text = SRC.read_text(encoding="utf-8")

# ── 1. Insert _compute_option_score helper before _rank_strategies_for_call ──

HELPER = '''

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
        _oa_dir = str(_pathlib.Path(__file__).parent.parent / "options")
        if _oa_dir not in _oa_sys.path:
            _oa_sys.path.insert(0, _oa_dir)
        from options_analyzer import (  # type: ignore
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

'''

ANCHOR_BEFORE_RANK = "def _rank_strategies_for_call(\n"
assert ANCHOR_BEFORE_RANK in text, "Anchor for _compute_option_score not found"
text = text.replace(ANCHOR_BEFORE_RANK, HELPER + ANCHOR_BEFORE_RANK, 1)
print("✓ inserted _compute_option_score helper")

# ── 2. Add optional option_score_data param to _rank_strategies_for_call signature ──

OLD_SIG_END = "    expiry_str: str,\n\n\n\n\n\n\n\n) -> list:"
NEW_SIG_END = "    expiry_str: str,\n    option_score_data=None,\n) -> list:"
assert OLD_SIG_END in text, f"Sig end anchor not found, got: {repr(text[text.find('expiry_str: str'):text.find('expiry_str: str')+80])}"
text = text.replace(OLD_SIG_END, NEW_SIG_END, 1)
print("✓ added option_score_data param to _rank_strategies_for_call")

# ── 3. CLOSE strategy dict: add option_score_data key ──

OLD_CLOSE = '        "signal_score": _get_signal_score(underlying),\n\n\n\n\n\n\n\n    })'
NEW_CLOSE = '        "signal_score": _get_signal_score(underlying),\n        "option_score_data": option_score_data,\n    })'
assert OLD_CLOSE in text, "CLOSE dict anchor not found"
text = text.replace(OLD_CLOSE, NEW_CLOSE, 1)
print("✓ added option_score_data to CLOSE strategy dict")

# ── 4. BULL_SPREAD strategy dict: add signal_score + option_score_data ──

OLD_BULL = '            "tradeoff": f"Upside capped at ${best_spread[\'strike\']:.0f}",\n\n\n\n\n\n\n\n        })'
NEW_BULL = ('            "tradeoff": f"Upside capped at ${best_spread[\'strike\']:.0f}",\n'
            '            "signal_score": _get_signal_score(underlying),\n'
            '            "option_score_data": option_score_data,\n'
            '        })')
assert OLD_BULL in text, "BULL_SPREAD dict anchor not found"
text = text.replace(OLD_BULL, NEW_BULL, 1)
print("✓ added signal_score + option_score_data to BULL_SPREAD dict")

# ── 5. BUY_PUT strategy dict: add signal_score + option_score_data ──

OLD_PUT = '            "tradeoff": f"Costs {cur_sym}{best_put.get(\'put_total\',0):.0f} ({put_cost_pct:.0f}% of gain)",\n\n\n\n\n\n\n\n        })'
NEW_PUT = ('            "tradeoff": f"Costs {cur_sym}{best_put.get(\'put_total\',0):.0f} ({put_cost_pct:.0f}% of gain)",\n'
           '            "signal_score": _get_signal_score(underlying),\n'
           '            "option_score_data": option_score_data,\n'
           '        })')
assert OLD_PUT in text, "BUY_PUT dict anchor not found"
text = text.replace(OLD_PUT, NEW_PUT, 1)
print("✓ added signal_score + option_score_data to BUY_PUT dict")

# ── 6. HOLD strategy dict: add signal_score + option_score_data ──

OLD_HOLD = '        "tradeoff": f"Risk losing full {cur_sym}{total_cost:.0f} if {underlying} reverses",\n\n\n\n\n\n\n\n    })'
NEW_HOLD = ('        "tradeoff": f"Risk losing full {cur_sym}{total_cost:.0f} if {underlying} reverses",\n'
            '        "signal_score": _get_signal_score(underlying),\n'
            '        "option_score_data": option_score_data,\n'
            '    })')
assert OLD_HOLD in text, "HOLD dict anchor not found"
text = text.replace(OLD_HOLD, NEW_HOLD, 1)
print("✓ added signal_score + option_score_data to HOLD dict")

# ── 7. Compute option_score_data and pass at call site ──

PRECOMPUTE = (
    '        # Compute option score for the existing held call position\n'
    '        _pos_row = None\n'
    '        if chain_calls is not None:\n'
    '            try:\n'
    '                _matches = chain_calls[abs(chain_calls["strike"] - strike) < 0.01]\n'
    '                if not _matches.empty:\n'
    '                    _pos_row = _matches.iloc[0]\n'
    '            except Exception:\n'
    '                pass\n'
    '        _pos_iv  = (float(_pos_row["impliedVolatility"]) * 100\n'
    '                    if _pos_row is not None and float(_pos_row.get("impliedVolatility") or 0) > 0\n'
    '                    else None)\n'
    '        _pos_vol = float(_pos_row["volume"] or 0)       if _pos_row is not None else None\n'
    '        _pos_oi  = float(_pos_row["openInterest"] or 0) if _pos_row is not None else None\n'
    '        _pos_bid = float(_pos_row.get("bid") or 0)      if _pos_row is not None else 0.0\n'
    '        _pos_ask = float(_pos_row.get("ask") or 0)      if _pos_row is not None else 0.0\n'
    '        _pos_mid = (_pos_bid + _pos_ask) / 2.0 if (_pos_bid > 0 and _pos_ask > 0) else current_per_share\n'
    '        _pos_spread_pct = (_pos_ask - _pos_bid) / _pos_mid * 100 if (_pos_mid > 0 and _pos_row is not None) else None\n'
    '        _opt_score_data = _compute_option_score(\n'
    '            underlying, strike, spot, days_to_expiry,\n'
    '            iv_pct=_pos_iv, volume=_pos_vol, oi=_pos_oi, spread_pct=_pos_spread_pct,\n'
    '        )\n'
    '\n'
)

OLD_CALL = '        details["ranked_strategies"] = _rank_strategies_for_call(\n'
NEW_CALL = PRECOMPUTE + '        details["ranked_strategies"] = _rank_strategies_for_call(\n'
assert OLD_CALL in text, "Call site anchor not found"
text = text.replace(OLD_CALL, NEW_CALL, 1)
print("✓ inserted _opt_score_data computation before call site")

# Add option_score_data kwarg to the call
OLD_KWARG = '            expiry_str        = expiry_str,\n\n\n\n\n\n\n\n        )'
NEW_KWARG = '            expiry_str        = expiry_str,\n            option_score_data = _opt_score_data,\n        )'
assert OLD_KWARG in text, "expiry_str kwarg anchor not found"
text = text.replace(OLD_KWARG, NEW_KWARG, 1)
print("✓ passed option_score_data kwarg at call site")

# ── 8. Fix signal badge: show on ALL strategies, not just CLOSE ──

OLD_BADGE_FILTER = '                _sig_data = s.get("signal_score") if s.get("id") == "CLOSE" else None\n'
NEW_BADGE_FILTER = '                _sig_data = s.get("signal_score")  # Show on all strategies\n'
assert OLD_BADGE_FILTER in text, "signal badge filter anchor not found"
text = text.replace(OLD_BADGE_FILTER, NEW_BADGE_FILTER, 1)
print("✓ removed CLOSE-only filter from signal score badge")

# Also update the nearby comment
OLD_BADGE_COMMENT = '                # Signal score badge — only for Close Now strategy\n'
NEW_BADGE_COMMENT = '                # Signal score badge — shown on all strategies\n'
text = text.replace(OLD_BADGE_COMMENT, NEW_BADGE_COMMENT, 1)

# ── 9. Add option score badge after _score_badge = "" ──

OLD_SCORE_EMPTY = '                    _score_badge = ""\n\n\n\n\n\n\n\n                strat_rows_html +='
NEW_SCORE_EMPTY = (
    '                    _score_badge = ""\n'
    '\n'
    '                # Option score badge\n'
    '                _opt_data = s.get("option_score_data") or {}\n'
    '                _os = _opt_data.get("score")\n'
    '                if _os is not None:\n'
    '                    _os_color = "#3fb950" if _os >= 70 else ("#f85149" if _os < 50 else "#e3b341")\n'
    '                    _liq_s = _opt_data.get("liq", "\u2014")\n'
    '                    _iv_s  = _opt_data.get("iv",  "\u2014")\n'
    '                    _str_s = _opt_data.get("strike", "\u2014")\n'
    '                    _dte_s = _opt_data.get("dte", "\u2014")\n'
    '                    _opt_score_badge = (\n'
    '                        f"<div style=\'font-size:.73rem;color:{_os_color};margin-top:2px;font-weight:600\'>"\n'
    '                        f"\U0001f3af Option score {_os}/100"\n'
    '                        f" <span style=\'font-weight:400;color:#8b949e\'>"\n'
    '                        f"(Liq {_liq_s} \xb7 IV {_iv_s} \xb7 K-fit {_str_s} \xb7 DTE {_dte_s})</span></div>"\n'
    '                    )\n'
    '                else:\n'
    '                    _opt_score_badge = ""\n'
    '\n'
    '                strat_rows_html +='
)
assert OLD_SCORE_EMPTY in text, "option badge insertion anchor not found"
text = text.replace(OLD_SCORE_EMPTY, NEW_SCORE_EMPTY, 1)
print("✓ inserted option score badge code")

# ── 10. Update template injection: add {_opt_score_badge} ──

OLD_TEMPLATE_LINE = '{_score_badge}  <div style="font-size:.73rem;color:#8b949e;margin-top:2px">⚠ Tradeoff: {s[\'tradeoff\']}</div>\n'
NEW_TEMPLATE_LINE = '{_score_badge}{_opt_score_badge}  <div style="font-size:.73rem;color:#8b949e;margin-top:2px">⚠ Tradeoff: {s[\'tradeoff\']}</div>\n'
assert OLD_TEMPLATE_LINE in text, "template injection anchor not found"
text = text.replace(OLD_TEMPLATE_LINE, NEW_TEMPLATE_LINE, 1)
print("✓ updated template to inject both score badges")

# ── Write back ──
SRC.write_text(text, encoding="utf-8")
print(f"\nAll patches applied successfully to {SRC.name}")
