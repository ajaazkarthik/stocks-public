"""
WebhookServer — HTTP listener that receives TradingView alerts, runs L1-L4
analysis via AnalysisEngine, and maintains a live actionable report.

Java analogy: this is like a Spring Boot @RestController + @Scheduled worker
combined — it handles POST /webhook requests and processes them in a background
thread (like an @Async method).

Endpoints:
  POST /webhook          — receive TV alert, trigger background analysis
  GET  /health           — server status + actionable count
  GET  /actionable       — serve the actionable HTML report

Usage:
  from signal_engine import AnalysisEngine, WebhookServer
  engine = AnalysisEngine()
  server = WebhookServer(engine, port=8765, threshold=55)
  server.start()          # blocks until Ctrl+C
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .analysis_engine import AnalysisEngine

_PARENT      = Path(__file__).parent.parent
_ROOT        = _PARENT.parent                          # project root (stocks/)
_REPORTS_DIR = _PARENT.parent / "reports"
_LOGS_DIR    = _PARENT.parent / "logs"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SAFE_SYMBOL = re.compile(r"^[A-Z0-9:._\-]{1,30}$", re.IGNORECASE)

# Exchange → currency (for fallback when TV screener has no data)
_EXCH_CURRENCY = {
    "NSE": "INR", "BSE": "INR",
    "ASX": "AUD", "ASX_DLY": "AUD",
    "SGX": "SGD",
    "HKEX": "HKD", "HKEX_DLY": "HKD",
}

try:
    from path_config import ensure_paths
    ensure_paths(_PARENT)
    from tradability_filter import TradabilityFilter as _TradabilityFilter
    _TRADABILITY_OK = True
except ImportError:
    _TRADABILITY_OK = False
    _TradabilityFilter = None  # type: ignore


class WebhookServer:
    """
    Runs a local HTTP server that receives TradingView webhooks and processes
    them through the full 4-layer Signal Stack analysis pipeline.

    Parameters
    ----------
    engine : AnalysisEngine
        Shared analysis engine for scoring.
    port : int
        TCP port to listen on (default 8765).
    threshold : int
        Composite ≥ threshold → signal is actionable (default 55).
    fires_file : Path, optional
        Where to append incoming fires (default fires_raw.json).
    actionable_log : Path, optional
        JSON file for persistent actionable history.
    actionable_report : Path, optional
        HTML file rebuilt after each actionable fire (reports/actionable_report.html).
    stack_report : Path, optional
        Full HTML report rebuilt after EVERY fire, no filter (reports/signal_stack_report.html).
    analysis_log : Path, optional
        Plain-text log of every analysed fire (debug / audit trail).
    """

    def __init__(
        self,
        engine: AnalysisEngine | None = None,
        port: int = 8765,
        threshold: int = 55,
        fires_file: Path | None = None,
        actionable_log: Path | None = None,
        actionable_report: Path | None = None,
        stack_report: Path | None = None,
        analysis_log: Path | None = None,
    ) -> None:
        self.engine           = engine or AnalysisEngine()
        self.port             = port
        self.threshold        = threshold
        self.fires_file       = fires_file or (_PARENT / "fires_raw.json")
        # Ensure output dirs exist
        _REPORTS_DIR.mkdir(exist_ok=True)
        _LOGS_DIR.mkdir(exist_ok=True)
        self.actionable_log    = actionable_log    or (_REPORTS_DIR / "actionable_log.json")
        self.actionable_report = actionable_report or (_REPORTS_DIR / "signal_report_actionable_webhook.html")
        self.stack_report      = stack_report      or (_REPORTS_DIR / "signal_report_full_webhook.html")
        # Analysis log — one line per fire, scores visible after restart
        self.analysis_log      = analysis_log      or (_LOGS_DIR / "webhook_analysis.log")

        self._lock            = threading.Lock()
        self._actionable:  list[dict] = []
        self._all_results: list[dict] = []   # ALL fires analysed (no filter) for stack_report
        self._processing:  set[str]   = set()
        self._http_server: HTTPServer | None = None

        # Tradability filter — one instance per server
        self._tradability_filter = _TradabilityFilter() if _TRADABILITY_OK else None

        self._load_state()

    # ─── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the HTTP server (blocking). Use Ctrl+C to stop."""
        handler = self._make_handler()
        self._http_server = HTTPServer(("", self.port), handler)
        self._http_server.threshold = self.threshold  # type: ignore[attr-defined]
        self._http_server.webhook_server = self       # type: ignore[attr-defined]
        print(f"🚀 Signal Stack Webhook Server listening on port {self.port}")
        print(f"   Actionable threshold: {self.threshold}")
        print(f"   Full stack report:    {self.stack_report}")
        print(f"   Actionable report:    {self.actionable_report}")
        print(f"   Analysis log:         {self.analysis_log}")
        print(f"   Loaded {len(self._actionable)} previous actionable signals  |  {len(self._all_results)} total.")
        try:
            self._http_server.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 Server stopped.")
        finally:
            self._http_server.server_close()

    def stop(self) -> None:
        """Stop the HTTP server (call from a separate thread)."""
        if self._http_server:
            self._http_server.shutdown()

    # ─── Payload parsing (static) ─────────────────────────────────────────────

    @staticmethod
    def parse_payload(body: bytes, query: dict) -> dict:
        """
        Parse a raw TradingView webhook POST into a fire-record dict.

        Handles:
          • JSON body {"s":"NSE:TITAN","r":"60","msg":"BUY ..."}
          • URL query params ?s={{exchange}}:{{ticker}}&r={{interval}}
          • Pine strategy body text fallback ("BUY TICKER price:…")

        TV strategy alerts do NOT substitute {{exchange}}/{{ticker}} in the URL
        — only the message body is substituted.  Unresolved template literals
        are silently discarded and the body-text parser is used instead.
        """
        text   = body.decode("utf-8", errors="replace").strip()
        parsed: dict = {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"msg": text}

        # ── Symbol resolution (6-stage fallback chain) ────────────────────────
        symbol = parsed.get("s") or ""
        if not symbol:
            exch = parsed.get("exchange", "")
            tick = parsed.get("ticker") or parsed.get("symbol", "")
            symbol = f"{exch}:{tick}" if (exch and tick) else tick

        if not symbol:
            raw = query.get("s", [""])[0]
            symbol = "" if ('{' in raw or '}' in raw) else raw

        if not symbol:
            q_exch = query.get("exchange", [""])[0]
            q_tick = query.get("ticker", [""])[0] or query.get("symbol", [""])[0]
            if '{' in q_exch or '{' in q_tick:
                q_exch = q_tick = ""
            symbol = f"{q_exch}:{q_tick}" if (q_exch and q_tick) else (q_tick or q_exch)

        if not symbol:
            # Pine fallback: "BUY TICKER price:..."
            m = re.search(r'\b(?:BUY|SELL)\s+([A-Z][A-Z0-9]{1,19})\s+price:',
                          text, re.IGNORECASE)
            if m:
                bare  = m.group(1).upper()
                q_exch = query.get("exchange", [""])[0].upper()
                if not q_exch or '{' in q_exch:
                    q_exch = WebhookServer._resolve_exchange(bare)
                symbol = f"{q_exch}:{bare}" if q_exch else bare
            else:
                m2 = re.search(r'\b([A-Z]+:[A-Z0-9]+)\b', text, re.IGNORECASE)
                symbol = m2.group(1) if m2 else "UNKNOWN"

        symbol = symbol.upper().strip()
        if '{' in symbol or '}' in symbol:
            raise ValueError(
                "TV template variables were not substituted. "
                "Include the symbol directly in the Pine alert message body."
            )
        if not _SAFE_SYMBOL.match(symbol):
            raise ValueError(f"Invalid symbol: {symbol!r}")
        if symbol == "UNKNOWN":
            raise ValueError("Could not extract EXCHANGE:TICKER from payload")

        # ── Resolution ────────────────────────────────────────────────────────
        resolution = str(
            parsed.get("r") or parsed.get("resolution") or
            query.get("r", [""])[0] or
            query.get("resolution", ["60"])[0]
        )

        # ── Message ───────────────────────────────────────────────────────────
        if parsed.get("msg") or parsed.get("message"):
            message = str(parsed.get("msg") or parsed.get("message"))
        else:
            parts = []
            for src, out in [("winrate", "WinRate"), ("win_rate", "WinRate"),
                             ("netprofit", "NetProfit%"), ("rsi", "rsi"),
                             ("adx", "ADX"), ("sqzoff", "sqzOff")]:
                val = parsed.get(src) or parsed.get(out)
                if val not in (None, ""):
                    parts.append(f"{out}:{val}")
            strategy = parsed.get("strategy") or parsed.get("action") or ""
            message = (strategy + " " + ", ".join(parts)).strip() if parts else text

        # ── Close price (from JSON field; TV Pine body uses "price: X.XX") ────
        close_raw = parsed.get("close") or parsed.get("price") or parsed.get("last") or ""
        try:
            close_price: float | None = float(close_raw) if close_raw else None
        except (ValueError, TypeError):
            close_price = None

        return {
            "fire_id":        int(time.time() * 1000),
            "alert_id":       0,
            "symbol":         symbol,
            "pro_symbol":     symbol,
            "resolution":     resolution,
            "sound_file":     None,
            "sound_duration": 0,
            "popup":          False,
            "cross_interval": False,
            "message":        message,
            "fire_time":      datetime.now(timezone.utc).isoformat(),
            "bar_time":       datetime.now(timezone.utc).isoformat(),
            "name":           f"webhook_{symbol}",
            "kinds":          ["webhook"],
            "close":          close_price,
        }

    # ─── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _resolve_exchange(bare_ticker: str) -> str:
        """Look up exchange for a bare ticker via fires_raw.json then TV search API."""
        fires_path = _PARENT / "fires_raw.json"
        try:
            if fires_path.exists():
                data = json.loads(fires_path.read_text(encoding="utf-8"))
                for fire in data.get("all", []):
                    ps = fire.get("pro_symbol", "")
                    if ":" in ps:
                        exch, tick = ps.split(":", 1)
                        if tick.upper() == bare_ticker.upper():
                            return exch.upper()
        except Exception:
            pass
        try:
            params = urllib.parse.urlencode({
                "text": bare_ticker, "hl": "false", "exchange": "",
                "lang": "en", "search_type": "undefined", "domain": "production",
            })
            url = f"https://symbol-search.tradingview.com/symbol_search/v3/?{params}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer":    "https://www.tradingview.com/",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                hits = json.loads(resp.read().decode("utf-8"))
            for item in (hits if isinstance(hits, list) else hits.get("symbols", [])):
                if item.get("symbol", "").upper() == bare_ticker.upper():
                    return item.get("exchange", "").upper()
        except Exception:
            pass
        return ""

    def _load_state(self) -> None:
        if self.actionable_log.exists():
            try:
                data = json.loads(self.actionable_log.read_text(encoding="utf-8"))
                self._actionable = data if isinstance(data, list) else []
            except Exception:
                self._actionable = []

    def _save_state(self) -> None:
        trimmed = self._actionable[-100:]
        self.actionable_log.write_text(
            json.dumps(trimmed, indent=2, default=str), encoding="utf-8"
        )

    def _append_fire(self, fire: dict) -> None:
        """Prepend fire to fires_raw.json (picked up by BatchAnalyzer)."""
        with self._lock:
            data: dict = {"all": []}
            if self.fires_file.exists():
                try:
                    data = json.loads(self.fires_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data.setdefault("all", [])
            data["all"].insert(0, fire)
            self.fires_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _log_analysis(self, sym: str, comp: float, l1: float, l2: float,
                      l3: float, l4: float, actionable: bool) -> None:
        """Append one line to webhook_analysis.log so scores are visible later."""
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = "ACTIONABLE" if actionable else "scored"
        line = (f"{ts}  {sym:25s}  {tag:10s}  "
                f"comp={comp:5.1f}  L1={l1:.1f}  L2={l2:.1f}  L3={l3:.1f}  L4={l4:.1f}\n")
        try:
            with open(self.analysis_log, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    def _rebuild_stack_report(self) -> None:
        """Rebuild signal_stack_report.html from ALL received results (no threshold filter)."""
        with self._lock:
            all_copy = list(self._all_results)
        seen: dict[str, dict] = {}
        for e in all_copy:
            sym = e.get("tv_symbol", "")
            if not sym:
                continue
            if sym not in seen or e["composite"] > seen[sym]["composite"]:
                seen[sym] = e
        ranked = sorted(seen.values(), key=lambda x: x["composite"], reverse=True)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        html    = self.engine.build_html(ranked, len(ranked), title="Signal Stack — All Signals")
        banner  = (
            f'<div style="background:#0369a1;color:#fff;padding:12px 28px;'
            f'font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px">'
            f'<span style="font-size:18px">📊</span>'
            f'<span>All signals — {len(ranked)} symbol(s) | Threshold: {self.threshold} | '
            f'Last updated: {now_str}</span>'
            f'</div>'
        )
        html = html.replace("<body>", "<body>" + banner, 1)
        self.stack_report.write_text(html, encoding="utf-8")

    def _rebuild_report(self, ranked: list[dict]) -> None:
        """Rebuild actionable_report.html from the in-memory actionable list."""
        seen: dict[str, dict] = {}
        for e in ranked:
            sym = e.get("tv_symbol", "")
            if not sym or sym.upper() == "UNKNOWN":
                continue
            if sym not in seen or e["composite"] > seen[sym]["composite"]:
                seen[sym] = e
        deduped  = sorted(seen.values(), key=lambda x: x["composite"], reverse=True)
        base     = self.engine.build_html(deduped, len(deduped), title="Actionable Signals")
        now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        banner   = (
            f'<div style="background:#7c3aed;color:#fff;padding:12px 28px;'
            f'font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px">'
            f'<span style="font-size:18px">🚨</span>'
            f'<span>ACTIONABLE — Composite ≥ {self.threshold} | '
            f'{len(deduped)} signal(s) | Last updated: {now_str}</span>'
            f'</div>'
        )
        html = base.replace("<body>", "<body>" + banner, 1)
        self.actionable_report.write_text(html, encoding="utf-8")

    def _process_fire(self, fire: dict) -> None:
        """Background thread: analyse → log → rebuild report if actionable."""
        sym = fire.get("pro_symbol", "?")
        ts  = datetime.now().strftime("%H:%M:%S")
        try:
            print(f"[{ts}] ⏳ Analysing {sym} …", flush=True)
            result = self.engine.analyse_signal(fire)
        except Exception as exc:
            print(f"[{ts}] ⚠️  Analysis failed for {sym}: {exc}", flush=True)
            traceback.print_exc()
            return
        finally:
            with self._lock:
                self._processing.discard(sym)

        comp  = result["composite"]
        sc    = result["scores"]
        g, _  = self.engine.grade(comp)
        ts    = datetime.now().strftime("%H:%M:%S")
        actionable = comp >= self.threshold

        # ── Tradability check (before adding to actionable) ───────────────────
        if self._tradability_filter is not None:
            trad = self._tradability_filter.check(sym)
            if not trad.tradable and not trad.tradable_with_limitations:
                print(
                    f"  ⛔ SKIP {sym}: not tradable via any supported broker — {trad.reason}",
                    flush=True,
                )
                # Run watchlist cleanup for this single skipped symbol
                skipped = [{"symbol": sym, "reason": trad.reason,
                            "source": fire.get("_source", "webhook")}]
                try:
                    from watchlist_cleaner import WatchlistCleaner
                    WatchlistCleaner().process_skipped(skipped)
                except ImportError:
                    pass
                return   # do not add to reports
            result["_tradability"] = trad.as_dict()
            if trad.tradable_with_limitations:
                result["_tradable_with_limitations"] = True

        print(
            f"[{ts}] {sym:22s}  Composite={comp:.1f} ({g})  "
            f"L1={sc['l1']['total']:.1f}  L2={sc['l2']['total']:.1f}  "
            f"L3={sc['l3']['total']:.1f}  L4={sc['l4']['total']:.1f}",
            end="", flush=True,
        )

        self._log_analysis(sym, comp,
                           sc["l1"]["total"], sc["l2"]["total"],
                           sc["l3"]["total"], sc["l4"]["total"],
                           actionable)

        # Always: add to full results list and rebuild the unfiltered stack report
        with self._lock:
            self._all_results.append(result)
            del self._all_results[:-200]
        self._rebuild_stack_report()

        if actionable:
            result["actionable_at"]  = datetime.now(timezone.utc).isoformat()
            result["trigger_source"] = "webhook"
            with self._lock:
                self._actionable.append(result)
                del self._actionable[:-100]
                self._save_state()
                ranked = sorted(self._actionable, key=lambda x: x["composite"], reverse=True)
            self._rebuild_report(ranked)
            print(f"  🚨 ACTIONABLE → {self.actionable_report.name}", flush=True)
        else:
            print(f"  (score {comp:.1f} < threshold {self.threshold})", flush=True)

    def _make_handler(self):
        """Return a BaseHTTPRequestHandler subclass bound to this WebhookServer instance."""
        server_ref = self   # capture self for closure

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # suppress default access log
                pass

            def _json(self, code: int, data: dict) -> None:
                body = json.dumps(data, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _html(self, html: str) -> None:
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/health":
                    with server_ref._lock:
                        count = len(server_ref._actionable)
                    self._json(200, {
                        "status":           "ok",
                        "actionable_count": count,
                        "threshold":        server_ref.threshold,
                        "time":             datetime.now(timezone.utc).isoformat(),
                    })
                elif path == "/actionable":
                    if server_ref.actionable_report.exists():
                        self._html(server_ref.actionable_report.read_text(encoding="utf-8"))
                    else:
                        self._html("<p style='font-family:sans-serif;padding:2rem'>"
                                   "No actionable signals yet.</p>")
                else:
                    self._json(200, {
                        "service":   "Signal Stack Webhook Server",
                        "endpoints": {
                            "POST /webhook": "Receive TradingView alert",
                            "GET  /health":  "Status + actionable count",
                            "GET  /actionable": "Live actionable report (HTML)",
                        },
                    })

            def do_POST(self):
                if urllib.parse.urlparse(self.path).path != "/webhook":
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length > 0 else b""
                query  = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

                print(f"[WEBHOOK] path={self.path!r}  body={body[:120]!r}", flush=True)

                try:
                    fire = WebhookServer.parse_payload(body, query)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return

                sym = fire["pro_symbol"]
                with server_ref._lock:
                    if sym in server_ref._processing:
                        self._json(202, {
                            "status":  "debounced",
                            "symbol":  sym,
                            "message": f"{sym} already being analysed — duplicate skipped.",
                        })
                        return
                    server_ref._processing.add(sym)

                server_ref._append_fire(fire)
                t = threading.Thread(
                    target=server_ref._process_fire,
                    args=(fire,),
                    daemon=True,
                )
                t.start()

                self._json(200, {
                    "status":    "accepted",
                    "symbol":    sym,
                    "fire_time": fire["fire_time"],
                    "message":   "Analysis running — check /actionable for results.",
                })

        return _Handler
