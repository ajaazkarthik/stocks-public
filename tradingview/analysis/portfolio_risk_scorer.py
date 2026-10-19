"""
portfolio_risk_scorer.py — Portfolio-Level Risk Assessment
=============================================================
Computes concentration risk, broker balance, and sector diversity
from a list of positions. Returns a risk score (0-10 per dimension)
plus an overall risk verdict (LOW / MEDIUM / HIGH).

Usage:
    from tradingview.analysis.portfolio_risk_scorer import PortfolioRiskScorer
    scorer = PortfolioRiskScorer()
    result = scorer.score(positions)

CLI:
    python tradingview/analysis/portfolio_risk_scorer.py --positions-file cache.json
"""

import sys, os, argparse, json
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]


class PortfolioRiskScorer:
    """Score portfolio-level risk across concentration, broker balance, and sector diversity."""

    def score(self, positions: list[dict]) -> dict:
        """
        Analyse a list of position dicts and return risk metrics.

        Each position dict should have at minimum:
            symbol, market_val, broker

        Optional fields: sector, pnl

        Returns:
            {
                "concentration_score": float 0-10 (10 = worst concentration),
                "broker_balance_score": float 0-10 (10 = worst imbalance),
                "sector_diversity_score": float 0-10 (10 = worst diversity),
                "overall_risk": "LOW" | "MEDIUM" | "HIGH",
                "top3_pct": float (top 3 positions as % of portfolio),
                "broker_breakdown": {broker: pct},
                "sector_breakdown": {sector: pct},
                "position_count": int,
            }
        """
        if not positions:
            return self._empty_result()

        # Filter to positions with positive market value
        valid = [p for p in positions if abs(float(p.get("market_val", 0))) > 0]
        if not valid:
            return self._empty_result()

        total_val = sum(abs(float(p.get("market_val", 0))) for p in valid)

        concentration = self._concentration_risk(valid, total_val)
        broker_balance = self._broker_balance_risk(valid, total_val)
        sector_diversity = self._sector_diversity_risk(valid, total_val)

        # Overall risk: weighted average
        avg = (concentration["score"] * 0.4
               + broker_balance["score"] * 0.3
               + sector_diversity["score"] * 0.3)

        if avg >= 7:
            verdict = "HIGH"
        elif avg >= 4:
            verdict = "MEDIUM"
        else:
            verdict = "LOW"

        return {
            "concentration_score": round(concentration["score"], 1),
            "broker_balance_score": round(broker_balance["score"], 1),
            "sector_diversity_score": round(sector_diversity["score"], 1),
            "overall_risk": verdict,
            "top3_pct": round(concentration["top3_pct"], 1),
            "broker_breakdown": broker_balance["breakdown"],
            "sector_breakdown": sector_diversity["breakdown"],
            "position_count": len(valid),
        }

    def _concentration_risk(self, positions: list[dict], total_val: float) -> dict:
        """Score concentration risk based on position size distribution."""
        vals = sorted(
            [abs(float(p.get("market_val", 0))) for p in positions],
            reverse=True,
        )
        top1_pct = (vals[0] / total_val * 100) if total_val > 0 else 0
        top3_pct = (sum(vals[:3]) / total_val * 100) if total_val > 0 else 0

        # Scoring: single position >30% → HIGH (8-10), top3 >60% → MEDIUM (5-7)
        if top1_pct > 50:
            score = 10.0
        elif top1_pct > 30:
            score = 8.0
        elif top3_pct > 70:
            score = 7.0
        elif top3_pct > 50:
            score = 5.0
        elif top3_pct > 35:
            score = 3.0
        else:
            score = 1.0

        return {"score": score, "top3_pct": top3_pct}

    def _broker_balance_risk(self, positions: list[dict], total_val: float) -> dict:
        """Score broker concentration risk. Pre-seeds all 6 known brokers."""
        broker_vals: dict[str, float] = {b: 0.0 for b in _KNOWN_BROKERS}
        for p in positions:
            b = p.get("broker", "Unknown")
            broker_vals.setdefault(b, 0.0)
            broker_vals[b] += abs(float(p.get("market_val", 0)))

        breakdown = {}
        for b in _KNOWN_BROKERS:
            pct = (broker_vals[b] / total_val * 100) if total_val > 0 else 0
            breakdown[b] = round(pct, 1)
        # Include unknown brokers
        for b, v in broker_vals.items():
            if b not in breakdown:
                breakdown[b] = round((v / total_val * 100) if total_val > 0 else 0, 1)

        max_pct = max(breakdown.values()) if breakdown else 0

        if max_pct > 80:
            score = 9.0
        elif max_pct > 60:
            score = 7.0
        elif max_pct > 40:
            score = 4.0
        elif max_pct > 25:
            score = 2.0
        else:
            score = 1.0

        return {"score": score, "breakdown": breakdown}

    def _sector_diversity_risk(self, positions: list[dict], total_val: float) -> dict:
        """Score sector concentration risk."""
        sector_vals: dict[str, float] = {}
        for p in positions:
            sector = p.get("sector", "Unknown") or "Unknown"
            sector_vals.setdefault(sector, 0.0)
            sector_vals[sector] += abs(float(p.get("market_val", 0)))

        breakdown = {}
        for s, v in sector_vals.items():
            breakdown[s] = round((v / total_val * 100) if total_val > 0 else 0, 1)

        n_sectors = len([s for s, pct in breakdown.items() if pct > 1.0])
        max_sector_pct = max(breakdown.values()) if breakdown else 0

        # Score: 1 sector or >60% in one = HIGH, <3 sectors = MEDIUM
        if n_sectors <= 1 or max_sector_pct > 60:
            score = 8.0
        elif n_sectors <= 2 or max_sector_pct > 40:
            score = 5.0
        elif n_sectors <= 3:
            score = 3.0
        else:
            score = 1.0

        return {"score": score, "breakdown": breakdown}

    def _empty_result(self) -> dict:
        return {
            "concentration_score": 0.0,
            "broker_balance_score": 0.0,
            "sector_diversity_score": 0.0,
            "overall_risk": "LOW",
            "top3_pct": 0.0,
            "broker_breakdown": {b: 0.0 for b in _KNOWN_BROKERS},
            "sector_breakdown": {},
            "position_count": 0,
        }


def render_risk_html(result: dict) -> str:
    """Render portfolio risk result as an HTML section."""
    risk = result["overall_risk"]
    color = {"LOW": "#27ae60", "MEDIUM": "#f39c12", "HIGH": "#e74c3c"}.get(risk, "#999")

    broker_rows = ""
    for b, pct in sorted(result.get("broker_breakdown", {}).items()):
        bar_w = min(pct, 100)
        broker_rows += (
            f"<tr><td>{b}</td>"
            f"<td><div style='background:#eee;border-radius:4px;height:18px;width:200px'>"
            f"<div style='background:{color};height:18px;border-radius:4px;width:{bar_w}%'></div>"
            f"</div></td>"
            f"<td style='text-align:right'>{pct:.1f}%</td></tr>\n"
        )

    return f"""
<div class="card" style="margin-top:20px">
  <h3>Portfolio Risk Assessment</h3>
  <div style="display:flex;gap:30px;align-items:center;margin:15px 0">
    <div style="background:{color};color:#fff;padding:12px 24px;border-radius:8px;
                font-size:1.3em;font-weight:bold">{risk} RISK</div>
    <div>
      <span style="margin-right:20px">Concentration: <b>{result['concentration_score']}/10</b></span>
      <span style="margin-right:20px">Broker Balance: <b>{result['broker_balance_score']}/10</b></span>
      <span>Sector Diversity: <b>{result['sector_diversity_score']}/10</b></span>
    </div>
  </div>
  <p>Top 3 positions = <b>{result['top3_pct']:.1f}%</b> of portfolio
     ({result['position_count']} total positions)</p>
  <table style="width:auto;border-collapse:collapse;margin-top:10px">
    <thead><tr style="background:#34495e;color:#fff">
      <th style="padding:6px 12px">Broker</th>
      <th style="padding:6px 12px">Allocation</th>
      <th style="padding:6px 12px">%</th>
    </tr></thead>
    <tbody>{broker_rows}</tbody>
  </table>
</div>
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio Risk Scorer")
    parser.add_argument(
        "--positions-file",
        default=str(Path(__file__).parent.parent.parent / ".cache_global_positions.json"),
        help="Path to JSON positions file",
    )
    args = parser.parse_args()

    path = Path(args.positions_file)
    if not path.exists():
        print(f"Positions file not found: {path}")
        print("Run global_portfolio_dashboard.py first to generate cached positions.")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        positions = json.load(f)

    scorer = PortfolioRiskScorer()
    result = scorer.score(positions)

    print(f"\nPortfolio Risk Assessment ({result['position_count']} positions)")
    print(f"{'='*50}")
    print(f"  Overall Risk:      {result['overall_risk']}")
    print(f"  Concentration:     {result['concentration_score']}/10")
    print(f"  Broker Balance:    {result['broker_balance_score']}/10")
    print(f"  Sector Diversity:  {result['sector_diversity_score']}/10")
    print(f"  Top 3 Positions:   {result['top3_pct']:.1f}% of portfolio")
    print(f"\n  Broker Breakdown:")
    for b, pct in sorted(result.get("broker_breakdown", {}).items()):
        print(f"    {b:12s}: {pct:.1f}%")
    print(f"\n  Sector Breakdown:")
    for s, pct in sorted(result.get("sector_breakdown", {}).items(), key=lambda x: -x[1]):
        if pct > 0:
            print(f"    {s:20s}: {pct:.1f}%")
