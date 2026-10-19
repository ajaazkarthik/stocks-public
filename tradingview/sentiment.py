"""
Centralized Sentiment Scoring Engine
=====================================
Provides a unified interface for sentiment analysis with two backends:
  • VADER  — fast, lightweight, default (social-media-tuned lexicon)
  • FinBERT — +26.7% more accurate on financial text, opt-in via use_finbert=True

Usage:
    from sentiment import get_scorer
    score = get_scorer()          # default VADER
    score = get_scorer("finbert") # FinBERT backend

    compound = score("Apple reports record revenue")  # float in [-1.0, 1.0]
"""

import sys
import logging
from functools import lru_cache

_log = logging.getLogger(__name__)

# ── VADER Backend ──────────────────────────────────────────────────────────────
def _vader_scorer():
    """Return a function(text) -> float (compound score in [-1, 1])."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()

    def score(text: str) -> float:
        return _vader.polarity_scores(text)["compound"]

    return score


# ── FinBERT Backend ────────────────────────────────────────────────────────────
def _finbert_scorer():
    """Return a function(text) -> float (compound score in [-1, 1]).

    Lazy-loads ProsusAI/finbert via transformers pipeline.
    First call takes ~11s (model load), subsequent calls ~177ms/headline.
    """
    _log.info("Loading FinBERT model (first call ~11s)...")
    import warnings as _w
    _w.filterwarnings("ignore", message=".*token.*")
    _w.filterwarnings("ignore", message=".*HF_TOKEN.*")
    import os as _os
    _os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from transformers import pipeline
    _pipe = pipeline(
        "text-classification",
        model="ProsusAI/finbert",
        truncation=True,
        max_length=512,
    )
    _log.info("FinBERT model ready")

    def score(text: str) -> float:
        result = _pipe(text)[0]
        raw = result["score"]
        label = result["label"]
        if label == "negative":
            return -raw
        elif label == "neutral":
            return 0.0
        return raw  # positive

    return score


# ── Batch scoring (FinBERT only) ──────────────────────────────────────────────
def _finbert_batch_scorer():
    """Return a function(texts) -> list[float] for batch processing."""
    _log.info("Loading FinBERT for batch mode...")
    from transformers import pipeline
    _pipe = pipeline(
        "text-classification",
        model="ProsusAI/finbert",
        truncation=True,
        max_length=512,
    )
    _log.info("FinBERT batch scorer ready")

    def batch_score(texts: list) -> list:
        if not texts:
            return []
        results = _pipe(texts, batch_size=8)
        scores = []
        for r in results:
            raw = r["score"]
            label = r["label"]
            if label == "negative":
                scores.append(-raw)
            elif label == "neutral":
                scores.append(0.0)
            else:
                scores.append(raw)
        return scores

    return batch_score


# ── Public API ─────────────────────────────────────────────────────────────────
_BACKENDS = {
    "vader": _vader_scorer,
    "finbert": _finbert_scorer,
}

_cached_scorers = {}


def get_scorer(backend: str = "vader"):
    """Get a sentiment scorer function: text -> float in [-1, 1].

    Args:
        backend: "vader" (default, fast) or "finbert" (accurate, slow first call)

    Returns:
        Callable[[str], float] — compound sentiment score
    """
    backend = backend.lower()
    if backend not in _BACKENDS:
        raise ValueError(f"Unknown sentiment backend: {backend!r}. "
                         f"Available: {list(_BACKENDS.keys())}")
    if backend not in _cached_scorers:
        _cached_scorers[backend] = _BACKENDS[backend]()
    return _cached_scorers[backend]


def get_batch_scorer(backend: str = "finbert"):
    """Get a batch scorer: list[str] -> list[float].

    Only available for 'finbert'. VADER doesn't benefit from batching.
    """
    if backend == "vader":
        scorer = get_scorer("vader")
        return lambda texts: [scorer(t) for t in texts]
    elif backend == "finbert":
        key = "finbert_batch"
        if key not in _cached_scorers:
            _cached_scorers[key] = _finbert_batch_scorer()
        return _cached_scorers[key]
    else:
        raise ValueError(f"Unknown backend: {backend!r}")


def get_batch_sentiment_parallel(
    headlines: "list[str]",
    backend: str = "finbert",
    max_workers: int = 3,
) -> "list[float]":
    """
    Score multiple headlines efficiently, choosing the best strategy per backend:

    • FinBERT  — uses the transformer pipeline's native batch_size=8 batching
                 (far faster than per-headline calls; no thread overhead needed
                 since the GPU/CPU is already saturated during a forward pass).
                 Falls back to ThreadPoolExecutor if batch scorer unavailable.

    • VADER    — already microseconds per headline; scores sequentially
                 (threading overhead would exceed the actual compute time).

    Args:
        headlines:   List of text strings to score
        backend:     "finbert" (default) or "vader"
        max_workers: Threads for the ThreadPoolExecutor fallback path only
                     (FinBERT batch path ignores this)

    Returns:
        List of float scores aligned to ``headlines``, values in [-1.0, 1.0].
        Failed/unavailable items default to 0.0 (neutral).

    Performance:
        250 headlines × 177ms sequential  ≈ 44.3s
        250 headlines  via batch_size=8   ≈  5-10s  (3.5-8× speedup)
    """
    if not headlines:
        return []

    if backend == "vader":
        # VADER is already sub-millisecond; sequential is fine
        scorer = get_scorer("vader")
        return [scorer(h) for h in headlines]

    # FinBERT path: prefer native pipeline batching
    try:
        batch_scorer = get_batch_scorer("finbert")
        return batch_scorer(headlines)
    except Exception as exc:
        _log.warning("finbert_batch_scorer_failed: %s — falling back to sequential threads", exc)

    # Fallback: thread-per-headline (for FinBERT scorer only — keeps model in memory)
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        single_scorer = get_scorer("finbert")
        scores_map: "dict[int, float]" = {}
        workers = min(max_workers, len(headlines))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(single_scorer, h): i
                for i, h in enumerate(headlines)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    scores_map[idx] = future.result(timeout=60)
                except Exception:
                    scores_map[idx] = 0.0

        return [scores_map.get(i, 0.0) for i in range(len(headlines))]

    except Exception as exc2:
        _log.warning("finbert_parallel_failed: %s — returning neutral scores", exc2)
        return [0.0] * len(headlines)


def score_both(text: str) -> dict:
    """Score a single text with BOTH FinBERT and VADER.

    Returns:
        {"finbert": float, "vader": float}  — both in [-1.0, 1.0]

    Both scorers are lazily loaded and cached on first use.
    """
    return {
        "finbert": get_scorer("finbert")(text),
        "vader":   get_scorer("vader")(text),
    }

