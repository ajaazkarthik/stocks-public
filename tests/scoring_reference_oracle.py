"""Independent arithmetic specification for Signal Stack Phase 3 review.

Do NOT import signal_stack or call production scoring helpers in this module.
The inputs are the published layer components, not their production totals.
This specification deliberately repeats the policy weights and transformations:
a changed production weight must change this specification by reviewed intent.
"""
from __future__ import annotations

BASE_WEIGHTS = (0.30, 0.30, 0.20, 0.20)
REGIMES = {
    "BULL_QUIET": (0.35, 0.30, 0.15, 0.20),
    "NORMAL": (0.30, 0.30, 0.20, 0.20),
    "ELEVATED": (0.25, 0.30, 0.25, 0.20),
    "CRISIS": (0.20, 0.25, 0.25, 0.30),
}
TECHNICAL_BASE = {"wr": 0.26, "np": 0.13, "rsi": 0.09,
                  "squeeze": 0.07, "adx": 0.07}
TECHNICAL_OPTIONAL = {"rel_vol": 0.09, "sma200": 0.09,
                      "macd": 0.08, "obv": 0.06, "bb_width": 0.06}


def limit(number, lower=0.0, upper=10.0):
    return min(upper, max(lower, number))


def technical(parts):
    """Independent L1 weighted reconstruction from emitted subscores."""
    missing = sum(weight for field, weight in TECHNICAL_OPTIONAL.items()
                  if parts[field] is None)
    total = 0.0
    for field, weight in TECHNICAL_BASE.items():
        if field == "wr":
            weight += missing * 0.60
        if field == "np":
            weight += missing * 0.40
        total += parts[field] * weight
    for field, weight in TECHNICAL_OPTIONAL.items():
        if parts[field] is not None:
            total += parts[field] * weight
    return round(limit(total), 2)


def fundamental(parts):
    """Independent L2 weighted reconstruction; handles absent FCF yield."""
    if parts["fcf_yield"] is None:
        return round(
            parts["valuation"] * 0.25
            + parts["profitability"] * 0.28
            + parts["growth"] * 0.25
            + parts["health"] * 0.13
            + parts["piotroski"] * 0.09, 2)
    return round(
        parts["valuation"] * 0.21
        + parts["profitability"] * 0.24
        + parts["growth"] * 0.21
        + parts["health"] * 0.17
        + parts["fcf_yield"] * 0.08
        + parts["piotroski"] * 0.09, 2)


def sentiment(parts):
    """Independent L3 bonus accounting, including article-velocity weight shift."""
    extra = min(5, max(0, parts["news_velocity"] - 5))
    analyst_weight = max(0.40, 0.45 - extra * 0.01)
    news_weight = min(0.30, 0.25 + extra * 0.01)
    beat_weight = 1.0 - analyst_weight - news_weight
    weighted = (
        parts["analyst"] * analyst_weight
        + parts["earnings_beat"] * beat_weight
        + parts["news"] * news_weight
    )
    weighted += parts["upgrade_mod"] * 0.10
    weighted += parts["earnings_risk"]
    weighted += 0.5 if parts["quartr_catalyst"] else 0.0
    weighted += parts["pead_bonus"] + parts["revision_bonus"]
    return round(limit(weighted), 2)


def internals(parts):
    """Independent L4 raw-weight and coverage-shrink reconstruction."""
    with_term = parts["vix_term_structure"] is not None
    weights = (
        (0.22, 0.16, 0.16, 0.16, 0.09, 0.11, 0.10)
        if with_term else
        (0.24, 0.18, 0.18, 0.18, 0.10, 0.12)
    )
    values = [parts["pcr"], parts["insider"], parts["w52"],
              parts["macro"], parts["sector_etf"], parts["short_interest"]]
    if with_term:
        values.append(parts["vix_term_structure"])
    raw = round(sum(weight * value for weight, value in zip(weights, values)), 2)
    return raw, round(5 + (raw - 5) * parts["coverage"], 2)


def composite(layers, *, l5=None, l6=None, l7=None, regime="NORMAL"):
    """Reproduce L1–L7 blend, L7-only bonus and 1.5x stretch independently."""
    base_weight = 1 - (0.10 if l5 is not None else 0) - (0.08 if l6 is not None else 0)
    layer_weights = REGIMES[regime]
    weighted_base = sum(score * weight for score, weight in zip(layers, layer_weights))
    weighted = weighted_base * base_weight
    if l5 is not None:
        weighted += l5 * 0.10
    if l6 is not None:
        weighted += l6 * 0.08
    bonus = max(0, l7 - 5) if l7 is not None else 0
    # The max L7 contribution is +0.5 on the 0–10 scale (= +5 on the 0–100 scale).
    linear = min((weighted + bonus * 0.10) * 10, 100)
    stretched = round(limit(50 + (linear - 50) * 1.5, 0, 100), 2)
    return {
        "weighted": round(weighted * 10, 2),
        "l7_bonus": round(bonus, 2),
        "linear": round(linear, 2),
        "stretched": stretched,
        "weights": tuple(w * base_weight for w in layer_weights)
                   + ((0.10,) if l5 is not None else ())
                   + ((0.08,) if l6 is not None else ()),
    }


def decision(layers, *, l5=None, l6=None, l7=None, regime="NORMAL",
             book=0.0, pool=0.0, basket=0.0, coverage=None):
    """Independently reproduce the final quality and decision outputs."""
    quality = round(min(100, composite(
        layers, l5=l5, l6=l6, l7=l7, regime=regime)["stretched"] + book), 2)
    confidence = 1.0 if coverage is None else max(0.85, min(1.0, coverage))
    final = round(limit(quality * confidence + pool + basket, 0, 100), 2)
    return quality, final
