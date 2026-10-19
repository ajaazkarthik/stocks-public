"""
TQS v2 Liquidity Scorer
========================
Scores a contract on its trading liquidity (0-100).

Inputs (from RawContract):
  - open_interest (int, from yfinance)
  - bid, ask (from scan2)
  - volume (Optional[int], from scan2 — None when market closed)

Formula:
  oi_score    = min(OI / 5000, 1.0) × 100              weight: 40%
  spread_score = max(0, 1 - spread_pct / 30) × 100     weight: 30%
  volume_score = min(volume / 500, 1.0) × 100 else 50  weight: 20%
  bid_score   = min(bid / 2.0, 1.0) × 100              weight: 10%

  liquidity_score = 0.40×oi + 0.30×spread + 0.20×volume + 0.10×bid
"""

from __future__ import annotations
from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class LiquidityScorer(IContractScorer):
    """Scores contract liquidity (OI, spread, volume, bid size)."""

    @property
    def name(self) -> str:
        return "liquidity"

    def score(self, contract: RawContract) -> float:
        """Return liquidity score 0-100."""
        try:
            return self._compute(contract)
        except Exception:
            return 0.0

    def score_detailed(self, contract: RawContract) -> dict[str, float]:
        """Return {oi_score, spread_score, volume_score, bid_score, total}."""
        try:
            oi_s = self._oi_score(contract.open_interest)
            spread_s = self._spread_score(contract.spread_pct)
            ws_volume = getattr(contract, "ws_volume", None)
            vol_s = self._volume_score(contract.volume, ws_volume)
            bid_s = self._bid_score(contract.bid)
            total = 0.40 * oi_s + 0.30 * spread_s + 0.20 * vol_s + 0.10 * bid_s
            return {
                "oi_score": round(oi_s, 2),
                "spread_score": round(spread_s, 2),
                "volume_score": round(vol_s, 2),
                "bid_score": round(bid_s, 2),
                "total": round(total, 2),
            }
        except Exception:
            return {"oi_score": 0.0, "spread_score": 0.0, "volume_score": 0.0,
                    "bid_score": 0.0, "total": 0.0}

    def _compute(self, contract: RawContract) -> float:
        oi_s = self._oi_score(contract.open_interest)
        spread_s = self._spread_score(contract.spread_pct)
        ws_volume = getattr(contract, "ws_volume", None)
        vol_s = self._volume_score(contract.volume, ws_volume)
        bid_s = self._bid_score(contract.bid)
        return 0.40 * oi_s + 0.30 * spread_s + 0.20 * vol_s + 0.10 * bid_s

    @staticmethod
    def _oi_score(oi: int | None) -> float:
        """Higher OI = more liquid. 5000 OI = perfect score.
        None means yfinance data unavailable — score 0 for OI component."""
        if oi is None:
            return 0.0
        return min(oi / 5000.0, 1.0) * 100.0

    @staticmethod
    def _spread_score(spread_pct: float) -> float:
        """Lower spread = more liquid. 0% = 100, 12%+ = 0.
        Tightened from /30 to /12 per specialist review 2026-05-22:
        7.6% GOOG spread should score ~37 not 75."""
        return max(0.0, 1.0 - spread_pct / 12.0) * 100.0

    @staticmethod
    def _volume_score(volume: int | None, ws_volume: int | None = None) -> float:
        """Volume score — uses TV WebSocket last-session volume when scan2 returns None.

        TV scan2 returns volume=None when market is closed (after hours / weekends).
        TV WebSocket caches the last-session volume and provides it 24/7.
        With ws_volume, we get a real score instead of a neutral 50.
        """
        v = volume if volume is not None else ws_volume
        if v is None:
            return 50.0  # neutral — no volume data from any source
        return min(v / 500.0, 1.0) * 100.0

    @staticmethod
    def _bid_score(bid: float) -> float:
        """Higher bid = more premium = more interest. $2+ = perfect."""
        return min(bid / 2.0, 1.0) * 100.0
