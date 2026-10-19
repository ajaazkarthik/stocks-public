"""AI commentary (L9) — verdict classification via OpenAI-compatible LLM.

Extracted from signal_stack.py to reduce file size.
"""

import json, sys, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from log_config import get_logger as _get_logger
except ImportError:
    import logging as _logging_fb
    _get_logger = lambda n: _logging_fb.getLogger(n)

_log = _get_logger("ai_commentary")

# ─── Optional OpenAI client ─────────────────────────────────────────────────────
_github_token = os.environ.get("GITHUB_TOKEN", "").strip()
try:
    from openai import OpenAI as _OpenAI
    if not _github_token:
        # No token — gracefully disable AI commentary (silent, no warning)
        _AI_AVAILABLE      = False
        _AI_CLIENT         = None  # type: ignore[assignment]
        _AI_MODEL          = ""
        _AI_MODEL_FALLBACK = ""
    else:
        _AI_CLIENT = _OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=_github_token,
            timeout=18.0,
            max_retries=1,
        )
        _AI_AVAILABLE      = True
        _AI_MODEL          = "gpt-4.1-nano"
        _AI_MODEL_FALLBACK = "gpt-4.1-mini"
except ImportError:
    _AI_AVAILABLE      = False
    _AI_CLIENT         = None  # type: ignore[assignment]
    _AI_MODEL          = ""
    _AI_MODEL_FALLBACK = ""

# ─── CSS for AI commentary block ────────────────────────────────────────────────
AI_COMMENTARY_CSS = """
.ai-block{padding:10px 20px;border-top:1px solid #1e293b;background:#080d1a}
.ai-verdict-wrap{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ai-badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:12px;text-transform:uppercase;letter-spacing:.06em}
.ai-buy-now{background:#14532d;color:#4ade80;border:1px solid #166534}
.ai-watch{background:#1e3a5f;color:#7dd3fc;border:1px solid #1d4e89}
.ai-skip{background:#3b1212;color:#fca5a5;border:1px solid #7f1d1d}
.ai-conf-high{color:#4ade80;font-size:10px;font-weight:600}
.ai-conf-med{color:#facc15;font-size:10px;font-weight:600}
.ai-conf-low{color:#f87171;font-size:10px;font-weight:600}
.ai-reason{font-size:11px;color:#94a3b8;margin-top:5px;line-height:1.5}
.ai-label{font-size:10px;color:#475569;font-style:italic}
"""

# ─── System prompt (reused across all calls) ─────────────────────────────────────
_AI_SYSTEM_PROMPT = """\
You are a strict stock-signal classifier. Apply the rules EXACTLY in order — sub-scores do NOT override composite thresholds.

HARD RULES (evaluate in order, pick first match):
  SKIP     — composite < 50  (ALWAYS skip, no exceptions, even if one sub-score looks good)
  SKIP     — backtest_units = 0  AND  composite < 60  (broken strategy on this symbol)
  BUY_NOW  — composite >= 65  AND  L3-sentiment >= 6.0  AND  EarnRisk=No
             OR composite >= 60  AND  (L1-Tech >= 7.5 OR L2-Fund >= 8.0)  AND  EarnRisk=No
  WATCH    — composite 50-64  AND at least one of: L1>=6.5, L2>=7.0, L3>=6.5, Analyst=Buy, Upgraded=Yes
  SKIP     — everything else that doesn't meet WATCH criteria

Confidence: HIGH if composite>=65 or clear BUY/SKIP signal; MED if borderline; LOW if conflicting signals.

Return ONLY a JSON object: {"verdict": "BUY_NOW|WATCH|SKIP", "confidence": "HIGH|MED|LOW", "reason": "<20 words"}"""


def _call_ai_single(item: dict, model: str = "", _retry: int = 0) -> dict:
    """Call the AI model for a single signal item.  Returns commentary dict or {error}.

    Retry policy: up to 2 retries on RateLimitReached with exponential back-off
    (2s, 4s). On persistent rate limit, auto-falls back through model chain:
      gpt-4.1-nano → gpt-4.1-mini → gpt-4o-mini
    """
    if not _AI_CLIENT:
        return {}
    _model = model or _AI_MODEL
    _FALLBACK_CHAIN = [_AI_MODEL, _AI_MODEL_FALLBACK, "gpt-4o-mini"]
    try:
        tv_sym     = item.get("tv_symbol", "")
        comp       = item.get("composite", 0)
        ls         = item.get("scores", {})
        l1         = ls.get("l1", {}).get("total", 0)
        l2         = ls.get("l2", {}).get("total", 0)
        l3         = ls.get("l3", {}).get("total", 0)
        l4         = ls.get("l4", {}).get("total", 0)
        meta       = item.get("meta", {})
        fund       = item.get("fundamentals", {})
        sent       = item.get("sentiment_data", {})
        rsi        = meta.get("rsi") or meta.get("RSI") or "N/A"
        adx        = meta.get("adx") or meta.get("ADX") or "N/A"
        wr         = meta.get("WinRate") or "N/A"
        np_pct     = meta.get("NetProfit%") or "N/A"
        vix        = item.get("market_internals", {}).get("vix_level") or "N/A"
        sector     = fund.get("sector") or "Unknown"
        consensus  = sent.get("analyst", {}).get("consensus") or "N/A"
        news_score = sent.get("news", {}).get("score", 0.0) or 0.0
        news_label = ("Positive" if news_score > 0.05 else
                      "Negative" if news_score < -0.05 else "Neutral")
        rel_vol      = item.get("scores", {}).get("l1", {}).get("raw_rv") or "N/A"
        _bt_raw  = item.get("backtest", {}).get("units")
        if _bt_raw is None:
            _bt_raw = item.get("buy_advisor", {}).get("units")
        bt_units = _bt_raw if _bt_raw is not None else "N/A"
        earn_risk    = "Yes" if item.get("has_earn_risk") else "No"
        upgraded     = "Yes" if item.get("upgraded") else "No"
        downgraded   = "Yes" if item.get("downgraded") else "No"

        user_text = (
            f"Symbol: {tv_sym}  Sector: {sector}\n"
            f"Composite: {comp:.1f}/100 | L1-Tech={l1:.1f} L2-Fund={l2:.1f} L3-Sent={l3:.1f} L4-Intl={l4:.1f}\n"
            f"RSI={rsi}  ADX={adx}  RelVol={rel_vol}x  VIX={vix}\n"
            f"Backtest: units={bt_units}  WinRate={wr}%  NetPnL={np_pct}%\n"
            f"Analyst: {consensus}  News: {news_label}({news_score:+.3f})\n"
            f"EarnRisk={earn_risk}  Upgraded={upgraded}  Downgraded={downgraded}\n"
            "Classify using the rules above."
        )
        _t0 = time.monotonic()
        resp = _AI_CLIENT.chat.completions.create(
            model=_model,
            messages=[
                {"role": "system", "content": _AI_SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
            max_tokens=60,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        latency = time.monotonic() - _t0
        raw = (resp.choices[0].message.content or "").strip()
        verdict = conf = reason = ""
        try:
            _js     = json.loads(raw)
            verdict = str(_js.get("verdict", "")).upper().strip()
            conf    = str(_js.get("confidence", "")).upper().strip()
            reason  = str(_js.get("reason", "")).strip()
        except Exception as exc:
            _log.debug("ai_verdict_json_parse_failed", raw=raw[:200], error=str(exc))
        if verdict not in {"BUY_NOW", "WATCH", "SKIP"}:
            verdict = "WATCH"
        if conf not in {"HIGH", "MED", "LOW"}:
            conf = "MED"
        return {
            "verdict":    verdict,
            "confidence": conf,
            "reason":     reason,
            "raw":        raw,
            "latency_s":  round(latency, 2),
            "model":      _model,
        }
    except Exception as e:
        err_str = str(e)
        is_rate_limit = any(kw in err_str for kw in ("RateLimitReached", "rate_limit", "429"))
        is_timeout    = any(kw in err_str for kw in ("timeout", "Timeout", "timed out"))

        if is_rate_limit and _retry < 2:
            time.sleep(2 ** (_retry + 1))
            return _call_ai_single(item, model=_model, _retry=_retry + 1)

        if is_rate_limit and _retry >= 2:
            _cur_idx = _FALLBACK_CHAIN.index(_model) if _model in _FALLBACK_CHAIN else -1
            _next_idx = _cur_idx + 1
            if _next_idx < len(_FALLBACK_CHAIN) and _FALLBACK_CHAIN[_next_idx] != _model:
                _next_model = _FALLBACK_CHAIN[_next_idx]
                print(f"\n  \u26a0\ufe0f  AI rate-limit on {_model} \u2192 retrying with {_next_model}",
                      end=" ", flush=True)
                time.sleep(1)
                return _call_ai_single(item, model=_next_model, _retry=0)

        if is_timeout and _retry == 0:
            time.sleep(2)
            return _call_ai_single(item, model=_model, _retry=1)

        short_err = err_str.split("\n")[0][:120]
        print(f"\n  \u2717 AI error [{tv_sym}] {_model}: {short_err}", file=sys.stderr)
        return {"error": err_str[:200]}


def build_ai_commentary(ranked: list, workers: int = 5, top_k: int = 0, model: str = "") -> None:
    """Concurrently enrich ranked items with AI commentary (in-place).

    If top_k > 0, only top_k symbols are sent to the LLM to reduce latency/cost.
    If model is specified, overrides the global _AI_MODEL.
    """
    _use_model = model or _AI_MODEL
    targets = ranked[:top_k] if top_k and top_k > 0 else ranked
    if top_k and top_k > 0:
        for item in ranked[top_k:]:
            item["ai_commentary"] = {"skipped": True}
    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for item in targets:
            futures[ex.submit(_call_ai_single, item, _use_model)] = item
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                item["ai_commentary"] = fut.result()
            except Exception as e:
                item["ai_commentary"] = {"error": str(e)}


def _ai_commentary_html(ai: "dict | None") -> str:
    """Render AI commentary dict as an HTML block. Returns '' if no data."""
    if not ai:
        return ""
    if ai.get("skipped"):
        return ""
    if ai.get("error"):
        return f'<div class="ai-block"><span class="ai-label">AI: error \u2014 {ai["error"][:60]}</span></div>'
    verdict    = ai.get("verdict", "").upper()
    conf       = ai.get("confidence", "").upper()
    reason     = ai.get("reason", "")
    latency    = ai.get("latency_s", "")

    verdict_class = {"BUY_NOW": "ai-buy-now", "WATCH": "ai-watch", "SKIP": "ai-skip"}.get(verdict, "ai-watch")
    conf_class    = {"HIGH": "ai-conf-high", "MED": "ai-conf-med", "LOW": "ai-conf-low"}.get(conf, "ai-conf-med")
    verdict_icon  = {"BUY_NOW": "\U0001f7e2", "WATCH": "\U0001f7e1", "SKIP": "\U0001f534"}.get(verdict, "\u26aa")

    lat_str = f" <span class='ai-label'>({latency}s)</span>" if latency else ""
    return f"""<div class="ai-block">
  <div class="ai-verdict-wrap">
    <span class="ai-badge {verdict_class}">{verdict_icon} {verdict.replace('_', ' ')}</span>
    <span class="{conf_class}">\u26a1 {conf} confidence</span>
    <span class="ai-label">L9 AI{lat_str}</span>
  </div>
  {f'<div class="ai-reason">{reason}</div>' if reason else ''}
</div>"""
