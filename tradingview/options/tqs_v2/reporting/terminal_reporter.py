"""
TQS v2 Terminal Reporter
=========================
Prints a concise summary table to stdout.
"""

from __future__ import annotations
import sys
from ..ranking.models import RankedContract


_GRADE_COLORS = {
    "A+": "\033[92m",
    "A": "\033[92m",
    "B+": "\033[93m",
    "B": "\033[93m",
    "C+": "\033[91m",
    "C": "\033[91m",
    "F": "\033[90m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


def print_summary(
    ranked: list[RankedContract],
    scan_type: str,
    run_id: str,
) -> None:
    """Print a concise ranked table to stdout."""
    label = "CALL" if scan_type == "calls" else "PUT"
    print(f"\n{_BOLD}TQS v2 — {label} Scan Results  (run_id: {run_id}){_RESET}")
    print("─" * 100)
    header = (
        f"{'#':>3}  {'Symbol':<8}  {'Expiry':<12}  {'Strike':>8}  "
        f"{'DTE':>4}  {'Delta':>7}  {'IV%':>5}  {'OI':>7}  "
        f"{'Bid':>6}  {'Ask':>6}  {'Spread%':>7}  {'Score':>6}  {'Grade':>4}"
    )
    print(header)
    print("─" * 100)

    for rc in ranked:
        c = rc.contract
        exp_str = str(c.expiration)
        expiry_fmt = (
            f"{exp_str[:4]}-{exp_str[4:6]}-{exp_str[6:8]}"
            if len(exp_str) == 8 else exp_str
        )
        grade_color = _GRADE_COLORS.get(rc.grade, "")
        vol_str = str(c.volume) if c.volume is not None else "—"

        oi_str = f"{c.open_interest:>7,}" if c.open_interest is not None else f"{'—':>7}"
        print(
            f"{rc.rank_global:>3}.  "
            f"{c.root:<8}  "
            f"{expiry_fmt:<12}  "
            f"{c.strike:>8.1f}  "
            f"{c.dte:>4}  "
            f"{c.delta:>7.3f}  "
            f"{c.iv*100:>5.1f}  "
            f"{oi_str}  "
            f"{c.bid:>6.2f}  "
            f"{c.ask:>6.2f}  "
            f"{c.spread_pct:>7.1f}  "
            f"{rc.total_score:>6.1f}  "
            f"{grade_color}{_BOLD}{rc.grade:>4}{_RESET}"
        )

    print("─" * 100)
    print(f"Total: {len(ranked)} contracts | Min score: {min((r.total_score for r in ranked), default=0):.1f}")
