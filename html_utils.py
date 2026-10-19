"""html_utils.py — Shared HTML/ANSI/CSS infrastructure for the stocks trading system.

Consolidates duplicated code across 6+ generators per ADVANCED_MODEL_DIRECTIVE A-10/B-5/C-5/C-6/C-10.
Import target: ≤ 5ms (no heavy deps).
"""
from __future__ import annotations
from html import escape as _stdlib_escape

# ── ANSI terminal colors (consolidates 6 independent definitions per B-5) ────
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"
ANSI_GREY = "\033[90m"

# Backward-compat aliases (matches naming in main.py / global_portfolio_dashboard.py)
RESET = ANSI_RESET
BOLD = ANSI_BOLD
DIM = ANSI_DIM
RED = ANSI_RED
GREEN = ANSI_GREEN
YELLOW = ANSI_YELLOW
BLUE = ANSI_BLUE
MAGENTA = ANSI_MAGENTA
CYAN = ANSI_CYAN
WHITE = ANSI_WHITE


def html_esc(s) -> str:
    """Safe HTML escape — replaces _esc() in 5 files."""
    if s is None:
        return ""
    return _stdlib_escape(str(s), quote=True)


# ── Base CSS (shared dark theme; see ADVANCED_MODEL_DIRECTIVE A-10) ──────────
CSS_BASE = """
:root {
  --bg: #0e1116; --fg: #e6edf3; --muted: #8b949e; --card: #161b22;
  --border: #30363d; --accent: #2f81f7; --ok: #16a34a; --warn: #d97706;
  --err: #dc2626; --gold: #f4c430;
}
body { background: var(--bg); color: var(--fg); font-family: -apple-system, "SF Pro Text",
  "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left; }
th { background: var(--card); font-weight: 600; }
tr:hover { background: rgba(47, 129, 247, 0.06); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.num-b { text-align: right; font-weight: 600; font-variant-numeric: tabular-nums; }
.pos { color: var(--ok); }
.neg { color: var(--err); }
.warn { color: var(--warn); }
.muted, .dim { color: var(--muted); }
.gold { color: var(--gold); }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 12px 0; }
.kpi-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; }
.kpi-val { font-size: 24px; font-weight: 700; }
.kpi-lbl { font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.5px; }
button { background: var(--accent); color: #fff; border: 0; border-radius: 4px;
  padding: 6px 12px; cursor: pointer; font-size: 13px; }
button:hover { filter: brightness(1.15); }
@media print {
  body { background: #ffffff !important; color: #000000 !important; }
  .pos, .ok, .c-ok, .c-ok-b, .c-ok-b7 { color: #16a34a !important; }
  .neg, .err, .c-err, .c-err-b, .c-err-b7 { color: #dc2626 !important; }
  .warn, .c-warn, .c-warn-b { color: #d97706 !important; }
  .muted, .dim, .c-muted, .c-muted-sm, .c-muted-b { color: #6b7280 !important; }
  th { background: #f3f4f6 !important; color: #000 !important; }
  a { color: #0000ee; }
  [data-no-print] { display: none !important; }
}
"""

# ── HTML boilerplate (lang + viewport per A-2) ───────────────────────────────
HTML_HEAD_OPEN = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
"""


def html_head(title: str, extra_css: str = "") -> str:
    """Return a complete ``<head>`` block with CSS_BASE, escaped *title*, and optional CSS."""
    css = CSS_BASE + ("\n" + extra_css if extra_css else "")
    return (
        f"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        f"<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html_esc(title)}</title>\n"
        f"<style>\n{css}\n</style>\n"
        f"</head>"
    )


# ── CDN script tags with SRI integrity hashes (security best practice) ────────
CDN_CHARTJS = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>'
CDN_LWC = '<script src="https://unpkg.com/lightweight-charts@5.1.0/dist/lightweight-charts.standalone.production.js" integrity="sha384-ExNwjbclSLY2LS3S2c6aDJDjpBHGZLct23WX65fLzj0ob1bRrCh9H90WThES5wQA" crossorigin="anonymous"></script>'


# ── Reusable JS snippets (C-5 sessionStorage, C-6 CSV export) ────────────────
JS_CSV_EXPORT = """
function exportTableCSV(tableId, filename) {
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  const rows = Array.from(tbl.querySelectorAll('tr'));
  const csv = rows.map(r =>
    Array.from(r.querySelectorAll('th,td'))
      .map(c => JSON.stringify((c.innerText || '').replace(/\\n/g,' ')))
      .join(',')
  ).join('\\n');
  const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename || 'table.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
"""

JS_FILTER_PERSIST = """
function persistFilter(inputId, storageKey) {
  const el = document.getElementById(inputId);
  if (!el) return;
  el.value = sessionStorage.getItem(storageKey) || '';
  el.addEventListener('input', e => sessionStorage.setItem(storageKey, e.target.value));
  el.dispatchEvent(new Event('input'));
}
"""

JS_LWC_LAZY = """
// IntersectionObserver-based lazy chart init (A-14)
function lazyInitCharts(initFnMap) {
  const observer = new IntersectionObserver((entries, obs) => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      const sym = e.target.dataset.chartSymbol;
      const fn = initFnMap[sym];
      if (typeof fn === 'function') { fn(e.target); obs.unobserve(e.target); }
    });
  }, { rootMargin: '200px' });
  document.querySelectorAll('[data-chart-container]').forEach(el => observer.observe(el));
}
"""

JS_TABLE_SORT = """
// C-11 — click-to-sort on <th> headers (lightweight inline tablesort)
(function(){
  document.querySelectorAll('table[data-sortable]').forEach(function(tbl){
    var ths = tbl.querySelectorAll('thead th');
    ths.forEach(function(th, idx){
      th.style.cursor = 'pointer';
      th.title = 'Click to sort';
      th.addEventListener('click', function(){
        var tbody = tbl.querySelector('tbody');
        if (!tbody) return;
        var rows = Array.from(tbody.querySelectorAll('tr'));
        var asc = th.dataset.sortDir !== 'asc';
        ths.forEach(function(h){ delete h.dataset.sortDir; h.textContent = h.textContent.replace(/ [▲▼]$/, ''); });
        th.dataset.sortDir = asc ? 'asc' : 'desc';
        th.textContent += asc ? ' ▲' : ' ▼';
        rows.sort(function(a, b){
          var at = (a.cells[idx] || {}).textContent || '';
          var bt = (b.cells[idx] || {}).textContent || '';
          var an = parseFloat(at.replace(/[$₹,%,]/g, ''));
          var bn = parseFloat(bt.replace(/[$₹,%,]/g, ''));
          if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
          return asc ? at.localeCompare(bt) : bt.localeCompare(at);
        });
        rows.forEach(function(r){ tbody.appendChild(r); });
      });
    });
  });
})();
"""


def system_footer_html(elapsed_s: float | None = None) -> str:
    """C-12 — psutil-based diagnostic footer. Safe no-op if psutil missing."""
    import sys as _sys
    parts = [f"Python {_sys.version[:6]}"]
    try:
        import psutil  # type: ignore
        proc = psutil.Process()
        parts.append(f"Mem {proc.memory_info().rss // (1024**2)} MB")
    except Exception:
        pass
    if elapsed_s is not None:
        parts.append(f"Duration {elapsed_s:.1f}s")
    inner = " &middot; ".join(html_esc(p) for p in parts)
    return f"<footer data-no-print style='margin-top:24px;font-size:11px;color:var(--muted)'>{inner}</footer>"


__all__ = [
    "ANSI_RESET", "ANSI_BOLD", "ANSI_DIM", "ANSI_RED", "ANSI_GREEN", "ANSI_YELLOW",
    "ANSI_BLUE", "ANSI_MAGENTA", "ANSI_CYAN", "ANSI_WHITE", "ANSI_GREY",
    "RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN", "WHITE",
    "html_esc", "CSS_BASE", "HTML_HEAD_OPEN", "html_head",
    "CDN_CHARTJS", "CDN_LWC",
    "JS_CSV_EXPORT", "JS_FILTER_PERSIST", "JS_LWC_LAZY", "JS_TABLE_SORT",
    "system_footer_html",
]
