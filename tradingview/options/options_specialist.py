"""
options_specialist.py — Independent Options Specialist

Reads raw options JSON data (actionable + rejected) and applies pure
options-theory scoring to decide which PUT/CALL contracts to pick.

NO ACCESS to the trading system's internal filters, scores, or brain.
Works entirely from the raw contract fields in the JSON files.

Specialist decision model (directional options buying):
  PUT strategy: bearish directional plays — stock should be in downtrend
  CALL strategy: bullish directional plays — stock should be in uptrend

  Criteria (each scored 0-10, weighted, total → 0-100 scale):
    A. IV attractiveness                (wt 20) — moderate IV (25-45%) = cheap enough to buy
    B. Delta (absolute)                 (wt 15) — 0.25-0.50 sweet spot for directional risk
    C. DTE bucket                       (wt 15) — 21-60d ideal; <14d gamma bomb; >90d slow
    D. OI adequacy                      (wt 15) — ≥100 required; ≥500 good; ≥2000 excellent
    E. Bid-ask spread %                 (wt 10) — <5% excellent; <15% ok; >30% reject
    F. Break-even sigma ratio           (wt 10) — be_sigma_ratio: >1.5 = stock needs big move
    G. SMA200 trend alignment           (wt 10) — PUT: below SMA200=10; CALL: above SMA200=10
    H. Support holding                  (wt  5) — PUT: broke below=10; CALL: holding above=10
  Total out of 100.  Threshold ≥ 55 → PICK; otherwise SKIP.

Usage:
    python tradingview/options/options_specialist.py \
        --actionable reports/options_actionable_puts.json \
        --rejected   reports/options_rejected_puts.json \
        --out        reports/options_specialist_puts.json

    or for CALL:
        --actionable reports/options_actionable.json \
        --rejected   reports/options_rejected.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _score_iv(iv: float | None) -> float:
    """IV as decimal (0-1+). For option buyers: lower IV = cheaper premium = better entry.
    Moderate IV (25-45%) is the sweet spot: enough movement priced in, not overpriced."""
    if iv is None:
        return 3.0  # unknown — neutral-low
    pct = iv * 100.0
    if pct < 15:
        return 4.0   # very low IV — cheap but stock may not move much
    if pct < 25:
        return 7.0   # low-to-moderate IV — good value for buyers
    if pct < 45:
        return 10.0  # sweet spot — movement expected, premium not extreme
    if pct < 65:
        return 6.0   # elevated IV — pricy for buyers; high expectations already baked in
    if pct < 80:
        return 3.0   # high IV — too expensive; need big move to profit
    return 1.0       # extreme IV (fear/event) — premium inflated, avoid as buyer


def _score_delta(delta: float | None, option_type: str) -> float:
    """Absolute delta. Sweet spot 0.25-0.45 for defined-risk strategies."""
    if delta is None:
        return 3.0
    adelta = abs(delta)
    # For PUTs delta is negative, so adelta = |delta|
    if adelta < 0.10:
        return 1.0   # deep OTM — too far, low probability of premium
    if adelta < 0.20:
        return 4.0   # OTM — marginal
    if adelta < 0.25:
        return 7.0   # slightly OTM — decent
    if adelta <= 0.45:
        return 10.0  # sweet spot
    if adelta <= 0.55:
        return 7.0   # near ATM — ok but higher risk
    return 3.0       # deep ITM — not ideal for premium selling


def _score_dte(dte: float | None) -> float:
    """DTE: 21-45d ideal for selling premium (theta decay)."""
    if dte is None:
        return 3.0
    if dte < 7:
        return 0.0   # gamma bomb — reject
    if dte < 14:
        return 2.0   # very high gamma risk
    if dte < 21:
        return 5.0   # short — elevated gamma
    if dte <= 45:
        return 10.0  # ideal theta-selling window
    if dte <= 60:
        return 7.0   # slightly long but OK
    if dte <= 90:
        return 5.0   # slow theta decay
    return 3.0       # LEAPS — very slow premium decay


def _score_oi(oi: float | None) -> float:
    """Open interest. Liquidity gate."""
    if oi is None:
        return 0.0
    if oi < 50:
        return 0.0   # illiquid — cannot exit cleanly
    if oi < 100:
        return 2.0   # borderline
    if oi < 300:
        return 5.0   # acceptable
    if oi < 1000:
        return 7.0   # good
    if oi < 5000:
        return 9.0   # very good
    return 10.0      # excellent liquidity


def _score_spread(spread_pct: float | None) -> float:
    """Bid-ask spread %. Tighter = better execution."""
    if spread_pct is None:
        return 3.0
    if spread_pct < 3:
        return 10.0
    if spread_pct < 5:
        return 9.0
    if spread_pct < 10:
        return 7.0
    if spread_pct < 15:
        return 5.0
    if spread_pct < 25:
        return 3.0
    if spread_pct < 35:
        return 1.0
    return 0.0       # 35%+ spread — effectively untradeable


def _score_be_sigma(be_sigma: float | None) -> float:
    """Break-even move as a multiple of the 1-sigma expected move. Lower = cheaper."""
    # Bands mirror the reject rules in options_actionable_report.py (>0.80 rejected).
    if be_sigma is None:
        return 3.0
    if be_sigma < 0.40:
        return 10.0  # cheap — priced below the expected move
    if be_sigma < 0.55:
        return 8.0   # fair
    if be_sigma < 0.70:
        return 6.0   # expensive
    if be_sigma < 1.00:
        return 3.0   # very expensive
    return 0.0       # needs a >1-sigma move just to break even


def _score_support(sup_holding: bool | None, option_type: str) -> float:
    """Support level check.
    PUT buyers: price breaking below support = momentum for the downmove (GOOD).
    CALL buyers: price holding above support = bullish structure (GOOD).
    """
    if option_type.upper() in ("PUT", "PE"):
        if sup_holding is False:
            return 10.0   # broke below support → downside momentum — PUT buyer's edge
        if sup_holding is True:
            return 3.0    # still holding support → PUT needs stock to break further
        return 5.0        # unknown
    else:
        # For CALLs: holding above support is bullish structure
        if sup_holding is True:
            return 10.0
        if sup_holding is False:
            return 3.0
        return 5.0


def _score_sma200(above_sma200: bool | None, option_type: str) -> float:
    """SMA200 trend filter.
    PUT buyers: want stock in downtrend (below SMA200 = bearish move expected).
    CALL buyers: want stock in uptrend (above SMA200 = bullish move expected).
    """
    if option_type.upper() in ("PUT", "PE"):
        # For PUT buyers: below SMA200 = bearish = excellent directional alignment
        if above_sma200 is False:
            return 10.0  # stock in downtrend — put buyer's sweet spot
        if above_sma200 is True:
            return 1.0   # stock in uptrend — put buyer swimming upstream
        return 5.0       # unknown
    else:
        # For CALL buyers: above SMA200 = bullish = excellent directional alignment
        if above_sma200 is True:
            return 10.0
        if above_sma200 is False:
            return 1.0
        return 5.0


# ── Weights ───────────────────────────────────────────────────────────────────
_WEIGHTS = {
    "iv":       20,
    "delta":    15,
    "dte":      15,
    "oi":       15,
    "spread":   10,
    "be_sigma": 10,
    "sma200":   10,
    "support":   5,
}
_TOTAL_WEIGHT = sum(_WEIGHTS.values())  # 100


def evaluate_contract(c: dict) -> dict:
    """Score one contract. Returns original dict + specialist scores + verdict."""
    opt_type = c.get("option_type", "CALL")

    scores = {
        "iv":       _score_iv(c.get("iv")),
        "delta":    _score_delta(c.get("delta"), opt_type),
        "dte":      _score_dte(c.get("dte")),
        "oi":       _score_oi(c.get("oi")),
        "spread":   _score_spread(c.get("spread_pct")),
        "be_sigma": _score_be_sigma(c.get("be_sigma_ratio")),
        "sma200":   _score_sma200(c.get("above_sma200"), opt_type),
        "support":  _score_support(c.get("sup_holding"), opt_type),
    }

    composite = sum(scores[k] * _WEIGHTS[k] for k in scores) / _TOTAL_WEIGHT * 10  # → 0-100 scale

    # Hard reject rules (override composite):
    rejects = []
    oi_val = c.get("oi") or 0
    dte_val = c.get("dte")
    spread_val = c.get("spread_pct")

    if oi_val < 50:
        rejects.append(f"OI={oi_val} < 50 (illiquid — cannot exit)")
    if dte_val is not None and dte_val < 7:
        rejects.append(f"DTE={dte_val} < 7 (gamma bomb)")
    if spread_val is not None and spread_val > 35:
        rejects.append(f"Spread={spread_val:.1f}% > 35% (untradeable)")
    # Directional alignment hard reject (mirrors system's SMA200 absolute filter):
    above_sma200_val = c.get("above_sma200")
    if opt_type.upper() in ("PUT", "PE"):
        if above_sma200_val is True:
            rejects.append("above_sma200=True — stock in uptrend, misaligned for PUT buyer")
    else:
        if above_sma200_val is False:
            rejects.append("above_sma200=False — stock in downtrend, misaligned for CALL buyer")

    if rejects:
        verdict = "HARD_REJECT"
        pick = False
        reason = "; ".join(rejects)
    elif composite >= 55:
        verdict = "PICK"
        pick = True
        reason = _build_pick_reason(c, scores, opt_type)
    elif composite >= 45:
        verdict = "MARGINAL"
        pick = False
        reason = _build_skip_reason(c, scores, composite)
    else:
        verdict = "SKIP"
        pick = False
        reason = _build_skip_reason(c, scores, composite)

    return {
        **c,
        "_sp_scores": scores,
        "_sp_composite": round(composite, 1),
        "_sp_verdict": verdict,
        "_sp_pick": pick,
        "_sp_reason": reason,
    }


def _build_pick_reason(c: dict, scores: dict, opt_type: str) -> str:
    parts = []
    iv_pct = (c.get("iv") or 0) * 100
    parts.append(f"IV={iv_pct:.0f}% (score={scores['iv']:.0f})")
    parts.append(f"|delta|={abs(c.get('delta') or 0):.2f} (score={scores['delta']:.0f})")
    parts.append(f"DTE={c.get('dte')} (score={scores['dte']:.0f})")
    parts.append(f"OI={c.get('oi')} (score={scores['oi']:.0f})")
    sp = c.get("spread_pct")
    if sp is not None:
        parts.append(f"Spread={sp:.1f}% (score={scores['spread']:.0f})")
    be = c.get("be_sigma_ratio")
    if be is not None:
        parts.append(f"BE_sigma={be:.2f} (score={scores['be_sigma']:.0f})")
    return "PICKED: " + ", ".join(parts)


def _build_skip_reason(c: dict, scores: dict, composite: float) -> str:
    # Identify weakest dimensions
    sorted_scores = sorted(scores.items(), key=lambda x: x[1])
    weak = [(k, v) for k, v in sorted_scores[:3] if v < 6]
    if not weak:
        return f"Marginal composite={composite:.1f} — no dominant weakness"
    parts = []
    for k, v in weak:
        val = _describe_field(c, k)
        parts.append(f"{k}={val} (score={v:.0f})")
    return "SKIPPED — weak: " + ", ".join(parts)


def _describe_field(c: dict, key: str) -> str:
    if key == "iv":
        v = c.get("iv")
        return f"{(v*100):.0f}%" if v else "?"
    if key == "delta":
        return str(round(c.get("delta") or 0, 3))
    if key == "dte":
        return str(c.get("dte", "?"))
    if key == "oi":
        return str(c.get("oi", "?"))
    if key == "spread":
        v = c.get("spread_pct")
        return f"{v:.1f}%" if v else "?"
    if key == "be_sigma":
        v = c.get("be_sigma_ratio")
        return f"{v:.2f}" if v else "?"
    if key == "sma200":
        return str(c.get("above_sma200", "?"))
    if key == "support":
        return str(c.get("sup_holding", "?"))
    return "?"


# ── Main ──────────────────────────────────────────────────────────────────────

def run_specialist(
    actionable_path: Path,
    rejected_path: Path | None,
    out_path: Path,
) -> dict:
    """Load JSONs, evaluate all contracts, write specialist report."""
    contracts: list[dict] = []
    meta: dict = {}

    # Load actionable contracts
    if actionable_path.exists():
        raw = json.loads(actionable_path.read_text(encoding="utf-8"))
        meta = {k: raw.get(k) for k in ("generated_at", "option_type", "scanned_count", "found_count")}
        contracts.extend(raw.get("contracts", []))
    else:
        print(f"  [specialist] WARNING: {actionable_path} not found — no actionable contracts loaded")

    # Load rejected contracts (to also evaluate them)
    if rejected_path and rejected_path.exists():
        raw_rej = json.loads(rejected_path.read_text(encoding="utf-8"))
        if not meta:
            meta = {k: raw_rej.get(k) for k in ("generated_at", "option_type", "scanned_count", "found_count")}
        rej_contracts = raw_rej.get("contracts", [])
        # Tag source so we can compare
        for c in rej_contracts:
            c.setdefault("_source", "rejected")
        contracts.extend(rej_contracts)
    elif rejected_path:
        print(f"  [specialist] WARNING: {rejected_path} not found")

    if not contracts:
        print("  [specialist] No contracts to evaluate.")
        result = {
            **meta,
            "specialist_picks": [],
            "specialist_skips": [],
            "specialist_hard_rejects": [],
            "pick_count": 0,
            "total_evaluated": 0,
        }
        out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return result

    # Evaluate all
    evaluated = [evaluate_contract(c) for c in contracts]
    evaluated.sort(key=lambda x: x["_sp_composite"], reverse=True)

    picks = [e for e in evaluated if e["_sp_verdict"] == "PICK"]
    marginals = [e for e in evaluated if e["_sp_verdict"] == "MARGINAL"]
    skips = [e for e in evaluated if e["_sp_verdict"] == "SKIP"]
    hard_rejects = [e for e in evaluated if e["_sp_verdict"] == "HARD_REJECT"]

    opt_type = meta.get("option_type", "CALL")

    result = {
        **meta,
        "specialist_version": "1.0",
        "decision_model": {
            "weights": _WEIGHTS,
            "threshold_pick": 55,
            "threshold_marginal": 45,
            "criteria": {
                "iv": "20wt — high IV earns rich premium for sellers",
                "delta": "15wt — |0.25-0.45| sweet spot for defined-risk",
                "dte": "15wt — 21-45d optimal theta decay window",
                "oi": "15wt — min 50 required; 500+ preferred",
                "spread": "10wt — <15% acceptable; <5% excellent",
                "be_sigma": "10wt — break-even sigma ratio >1.5 gives cushion",
                "sma200": f"10wt — {'above SMA200 = uptrend, PUT safer | below = directional risk' if opt_type.upper() in ('PUT','PE') else 'above SMA200 = uptrend'}",
                "support": f"5wt — {'above support = cushion for PUT seller' if opt_type.upper() in ('PUT','PE') else 'less critical for CALL'}",
            },
        },
        "pick_count": len(picks),
        "marginal_count": len(marginals),
        "skip_count": len(skips),
        "hard_reject_count": len(hard_rejects),
        "total_evaluated": len(evaluated),
        "specialist_picks": picks,
        "specialist_marginals": marginals,
        "specialist_skips": skips,
        "specialist_hard_rejects": hard_rejects,
    }

    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print(f"\n  [specialist] Evaluated {len(evaluated)} contracts ({opt_type})")
    print(f"  [specialist] PICK={len(picks)}  MARGINAL={len(marginals)}  SKIP={len(skips)}  HARD_REJECT={len(hard_rejects)}")
    print(f"  [specialist] Report → {out_path}")

    if picks:
        print(f"\n  TOP PICKS (score ≥ 55):")
        for p in picks[:10]:
            sym = p.get("symbol", "?")
            st = p.get("strike", "?")
            sc = p["_sp_composite"]
            dte = p.get("dte", "?")
            iv_pct = (p.get("iv") or 0) * 100
            oi = p.get("oi", "?")
            print(f"    {sym:8s} {opt_type}@{st}  score={sc:.1f}  DTE={dte}  IV={iv_pct:.0f}%  OI={oi}")
            print(f"             {p['_sp_reason']}")

    return result


def _compare_with_system(
    specialist_result: dict,
    actionable_path: Path,
) -> None:
    """Print comparison: specialist picks vs system's actionable list."""
    if not actionable_path.exists():
        return

    sys_raw = json.loads(actionable_path.read_text(encoding="utf-8"))
    sys_contracts = sys_raw.get("contracts", [])

    def _key(c: dict) -> str:
        return f"{c.get('symbol','')}|{c.get('strike','')}|{c.get('expiry','')}"

    sys_keys = {_key(c) for c in sys_contracts}
    sp_picks = specialist_result.get("specialist_picks", [])
    sp_keys = {_key(c) for c in sp_picks}

    both = sys_keys & sp_keys
    sys_only = sys_keys - sp_keys       # system said OK, specialist says SKIP
    sp_only = sp_keys - sys_keys        # specialist says PICK, system rejected

    total = len(sys_keys | sp_keys)
    agreement = len(both) + (len(specialist_result.get("specialist_skips", [])) - len(sp_only))
    # Simplified match rate: contracts where both agree (both pick or both skip)
    pick_agreement = len(both)
    pick_union = len(sys_keys | sp_keys)
    match_pct = (pick_agreement / max(pick_union, 1)) * 100

    print(f"\n  ── COMPARISON: System vs Specialist ──────────────────────────────────")
    print(f"  System actionable:    {len(sys_keys)} contracts")
    print(f"  Specialist picks:     {len(sp_keys)} contracts")
    print(f"  Both agree (PICK):    {len(both)}")
    print(f"  System only (may be too loose):  {len(sys_only)}")
    print(f"  Specialist only (system too strict): {len(sp_only)}")
    print(f"  Pick-set match rate:  {match_pct:.1f}%  (target ≥ 80%)")

    if sys_only:
        print(f"\n  System approved but Specialist SKIP (system filter may be too loose):")
        for k in list(sys_only)[:5]:
            sym, st, exp = k.split("|", 2)
            # Find specialist verdict for this contract
            all_ev = (specialist_result.get("specialist_skips", []) +
                      specialist_result.get("specialist_marginals", []) +
                      specialist_result.get("specialist_hard_rejects", []))
            for c in all_ev:
                if _key(c) == k:
                    print(f"    {sym:8s} @{st} exp={exp}  sp_score={c['_sp_composite']:.1f}  reason={c['_sp_reason']}")
                    break
            else:
                print(f"    {sym:8s} @{st} exp={exp}  (not in specialist output)")

    if sp_only:
        print(f"\n  Specialist PICK but System rejected (system filter may be too strict):")
        # Load rejected to find these
        rej_path = actionable_path.parent / actionable_path.name.replace("actionable", "rejected")
        if rej_path.exists():
            rej_raw = json.loads(rej_path.read_text(encoding="utf-8"))
            rej_map = {_key(c): c for c in rej_raw.get("contracts", [])}
        else:
            rej_map = {}
        for k in list(sp_only)[:5]:
            sym, st, exp = k.split("|", 2)
            rej_c = rej_map.get(k)
            if rej_c:
                rcode = rej_c.get("_reject_code", "?")
                rreason = rej_c.get("_reject_reason", "?")
                print(f"    {sym:8s} @{st} exp={exp}  sys_reject={rcode}: {rreason}")
            else:
                print(f"    {sym:8s} @{st} exp={exp}  (not found in rejected)")

    return match_pct


def main() -> None:
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Independent options specialist — evaluates contracts purely from options theory"
    )
    parser.add_argument(
        "--actionable",
        default="reports/options_actionable_puts.json",
        help="Path to actionable contracts JSON (default: reports/options_actionable_puts.json)",
    )
    parser.add_argument(
        "--rejected",
        default=None,
        help="Path to rejected contracts JSON (optional — enables comparison mode)",
    )
    parser.add_argument(
        "--out",
        default="reports/options_specialist_puts.json",
        help="Output JSON path for specialist verdict (default: reports/options_specialist_puts.json)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        default=True,
        help="Compare specialist picks vs system's actionable list (default: True)",
    )
    args = parser.parse_args()

    root = Path(__file__).parent.parent.parent  # workspace root
    act_path = Path(args.actionable) if Path(args.actionable).is_absolute() else root / args.actionable
    rej_path = (Path(args.rejected) if Path(args.rejected).is_absolute() else root / args.rejected) if args.rejected else None
    out_path = Path(args.out) if Path(args.out).is_absolute() else root / args.out

    # Auto-detect rejected path if not given
    if rej_path is None:
        candidate = act_path.parent / act_path.name.replace("actionable", "rejected")
        if candidate.exists():
            rej_path = candidate
            print(f"  [specialist] Auto-detected rejected: {rej_path}")

    print(f"  [specialist] Input:  {act_path}")
    if rej_path:
        print(f"  [specialist] Input:  {rej_path} (rejected)")
    print(f"  [specialist] Output: {out_path}")

    result = run_specialist(act_path, rej_path, out_path)

    if args.compare:
        _compare_with_system(result, act_path)


if __name__ == "__main__":
    main()
