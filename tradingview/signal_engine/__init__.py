"""
signal_engine — Refactored 3-class architecture for Signal Stack

Java-style breakdown:
  AnalysisEngine  → common.py equivalent: all L1-L4 scoring + data fetching
  BatchAnalyzer   → reads fires_raw.json, ranks, generates full report
  WebhookServer   → HTTP listener, analyses live TV alerts, generates actionable report

Usage:
  from signal_engine import AnalysisEngine, BatchAnalyzer, WebhookServer
"""

from .analysis_engine import AnalysisEngine
from .batch_analyzer  import BatchAnalyzer
from .webhook_handler import WebhookServer
from .sell_engine     import SellEngine

__all__ = ["AnalysisEngine", "BatchAnalyzer", "WebhookServer", "SellEngine"]
