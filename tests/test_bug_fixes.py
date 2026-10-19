"""Regression tests for bug fixes from bug_report_2026-04-28.md

Tests verify that all 7 bug fixes remain applied and don't regress.
"""
import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ── BUG-004: main.py subprocess.run encoding ──────────────────────────
def test_bug004_subprocess_run_has_encoding():
    """main.py subprocess.run else-branch must have encoding='utf-8'."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find subprocess.run calls
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
        ):
            # Check if this is subprocess.run (has cmd as first arg and 'check' kwarg)
            kw_names = [kw.arg for kw in node.keywords]
            if "check" in kw_names:
                assert "encoding" in kw_names, (
                    "subprocess.run() call missing encoding='utf-8' keyword"
                )


# ── BUG-003: signal_stack kv-panel uses l1s raw values ────────────────
def test_bug003_kv_panel_uses_l1s_not_meta():
    """signal_stack.py kv panel must use l1s.get('raw_rsi') not meta.get('rsi')."""
    src = (ROOT / "tradingview" / "signal_stack.py").read_text(encoding="utf-8")
    # The old pattern (bug): meta.get('rsi')  in a fmt() call
    assert "fmt(meta.get('rsi')" not in src, (
        "BUG-003 regression: kv panel still uses meta.get('rsi') instead of l1s.get('raw_rsi')"
    )
    assert "fmt(meta.get('ADX')" not in src, (
        "BUG-003 regression: kv panel still uses meta.get('ADX') instead of l1s.get('raw_adx')"
    )
    # The fix must be present
    assert "l1s.get('raw_rsi')" in src, "raw_rsi reference missing from signal_stack.py"
    assert "l1s.get('raw_adx')" in src, "raw_adx reference missing from signal_stack.py"


# ── BUG-006: yfinance calls use redirect_stdout ──────────────────────
def test_bug006_yfinance_redirect_stdout_sell_actionable():
    """sell_actionable.py yfinance calls must redirect stdout alongside stderr."""
    src = (ROOT / "tradingview" / "sell" / "sell_actionable.py").read_text(encoding="utf-8")
    # Every redirect_stderr must be paired with redirect_stdout
    stderr_count = src.count("redirect_stderr")
    stdout_count = src.count("redirect_stdout")
    assert stdout_count >= stderr_count, (
        f"sell_actionable.py has {stderr_count} redirect_stderr but only {stdout_count} redirect_stdout"
    )


def test_bug006_yfinance_redirect_stdout_sell_strategy():
    """sell_strategy.py yfinance calls must redirect stdout alongside stderr."""
    src = (ROOT / "tradingview" / "sell" / "sell_strategy.py").read_text(encoding="utf-8")
    stderr_count = src.count("redirect_stderr")
    stdout_count = src.count("redirect_stdout")
    assert stdout_count >= stderr_count, (
        f"sell_strategy.py has {stderr_count} redirect_stderr but only {stdout_count} redirect_stdout"
    )


# ── BUG-009: --no-india guards India FY loading ──────────────────────
def test_bug009_no_india_guards_fy_block():
    """combined_cgt_report.py India FY loading must be guarded by args.no_india."""
    src = (ROOT / "cgt" / "combined_cgt_report.py").read_text(encoding="utf-8")
    # Find the India FY loading block
    fy_idx = src.find("Load India FY P&L summaries")
    assert fy_idx >= 0, "India FY loading comment not found"
    # The guard must appear BEFORE the for loop that loads FY data
    guard_region = src[fy_idx:fy_idx + 300]
    assert "args.no_india" in guard_region, (
        "BUG-009 regression: India FY loading block not guarded by args.no_india"
    )


# ── BUG-008: console win rate includes expired ───────────────────────
def test_bug008_console_win_rate_includes_expired():
    """alert_tracker.py console win rate denominator must include expired trades."""
    src = (ROOT / "tradingview" / "alert_tracker.py").read_text(encoding="utf-8")
    # The fix adds: expired = perf.get("expired", 0)
    # and: total_closed = succ + fail + expired
    assert 'perf.get("expired"' in src, (
        "BUG-008 regression: console path doesn't read expired count from perf"
    )
    assert "total_closed" in src, (
        "BUG-008 regression: total_closed variable not found (should be succ+fail+expired)"
    )


# ── BUG-011: layer names match signal-pipeline spec ──────────────────
def test_bug011_console_layer_names_match_spec():
    """alert_tracker.py console layer names must match signal-pipeline spec."""
    src = (ROOT / "tradingview" / "alert_tracker.py").read_text(encoding="utf-8")
    # Correct names per signal-pipeline.instructions.md
    assert '"L1 Technical"' in src, "L1 should be 'L1 Technical' not 'L1 Trend'"
    assert '"L2 Fundamental"' in src, "L2 should be 'L2 Fundamental' not 'L2 Momentum'"
    assert '"L3 Sentiment"' in src, "L3 should be 'L3 Sentiment' not 'L3 Volume'"
    assert '"L4 Market Macro"' in src, "L4 should be 'L4 Market Macro' not 'L4 Fundamental'"
    # Old names must NOT be present (except as part of other strings)
    assert '"L1 Trend"' not in src, "Old name 'L1 Trend' still present"
    assert '"L2 Momentum"' not in src, "Old name 'L2 Momentum' still present"
    assert '"L3 Volume"' not in src, "Old name 'L3 Volume' still present"


# ── BUG-012: CGT title is dynamic ────────────────────────────────────
def test_bug012_cgt_title_is_dynamic():
    """combined_cgt_report.py title must use {title_sources} placeholder."""
    src = (ROOT / "cgt" / "combined_cgt_report.py").read_text(encoding="utf-8")
    # Static title should NOT be present
    assert "IBKR + Moomoo + India</title>" not in src, (
        "BUG-012 regression: static title still hardcoded"
    )
    # Dynamic placeholder must be present
    assert "{title_sources}" in src, (
        "BUG-012 regression: {title_sources} placeholder missing from template"
    )
    # title_sources must be computed and passed to .format()
    assert "title_sources=" in src, (
        "BUG-012 regression: title_sources not passed to .format() kwargs"
    )


# ── H-01: edgar_enricher uses accurate activity key names ─────────────
def test_h01_edgar_enricher_uses_activity_key_names():
    """edgar_enricher.py must use 'insider_activity_30d' / 'has_insider_activity' /
    'latest_activity_date' — NOT the misleading 'buying' names which counted ALL
    Form 4 filings (sells, option exercises, gifts) not just purchases."""
    src = (ROOT / "tradingview" / "edgar_enricher.py").read_text(encoding="utf-8")

    # New accurate key names must be present
    assert "insider_activity_30d" in src, (
        "H-01 regression: 'insider_activity_30d' key missing from edgar_enricher.py"
    )
    assert "has_insider_activity" in src, (
        "H-01 regression: 'has_insider_activity' key missing from edgar_enricher.py"
    )
    assert "latest_activity_date" in src, (
        "H-01 regression: 'latest_activity_date' key missing from edgar_enricher.py"
    )

    # Old misleading key names must NOT be present (except in documentation files)
    assert '"insider_buys_30d"' not in src, (
        "H-01 regression: misleading key 'insider_buys_30d' still in edgar_enricher.py"
    )
    assert '"has_insider_buying"' not in src, (
        "H-01 regression: misleading key 'has_insider_buying' still in edgar_enricher.py"
    )
    assert '"latest_buy_date"' not in src, (
        "H-01 regression: misleading key 'latest_buy_date' still in edgar_enricher.py"
    )


def test_h01_edgar_enricher_return_dict_keys():
    """enrich_insider_activity() return dict must contain the correct activity key names."""
    import ast as _ast
    src = (ROOT / "tradingview" / "edgar_enricher.py").read_text(encoding="utf-8")

    tree = _ast.parse(src)

    # Find the result dict initialisation inside enrich_insider_activity
    func_node = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "enrich_insider_activity":
            func_node = node
            break

    assert func_node is not None, "enrich_insider_activity function not found"

    # Collect all string constants used as dict keys in assignments inside the function
    keys_assigned = set()
    for node in _ast.walk(func_node):
        if isinstance(node, _ast.Dict):
            for k in node.keys:
                if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                    keys_assigned.add(k.value)

    assert "insider_activity_30d" in keys_assigned, (
        "H-01: result dict missing 'insider_activity_30d' key"
    )
    assert "has_insider_activity" in keys_assigned, (
        "H-01: result dict missing 'has_insider_activity' key"
    )
    assert "latest_activity_date" in keys_assigned, (
        "H-01: result dict missing 'latest_activity_date' key"
    )
    assert "insider_buys_30d" not in keys_assigned, (
        "H-01: misleading 'insider_buys_30d' key still in result dict"
    )
    assert "has_insider_buying" not in keys_assigned, (
        "H-01: misleading 'has_insider_buying' key still in result dict"
    )
    assert "latest_buy_date" not in keys_assigned, (
        "H-01: misleading 'latest_buy_date' key still in result dict"
    )


# ── BUG-LOG-001: tv_sector_heatmap --exchange all (2026-05-02 log) ────
def test_buglog001_sector_heatmap_accepts_exchange_all():
    """argparse choices in tv_sector_heatmap.py must include 'all' to prevent
    'invalid choice: all' argparse error when morning run uses --exchange all."""
    src = (ROOT / "tradingview" / "analysis" / "tv_sector_heatmap.py").read_text(
        encoding="utf-8"
    )
    # The choices list must include "all"
    assert '"all"' in src or "'all'" in src, (
        "BUG-LOG-001 regression: 'all' not in argparse choices for --exchange"
    )
    # The choices must be in the argparse add_argument section (not just a docstring)
    assert '+ ["all"]' in src or "+ ['all']" in src, (
        "BUG-LOG-001 regression: 'all' not added to _SCANNER_URLS choices in argparse"
    )


def test_buglog001_build_sector_data_handles_all_exchange():
    """build_sector_data('all') must handle the 'all' key without crashing.
    When exchange='all', it should trigger the combination branch, not fall
    through to _SCANNER_URLS.get() which would return None."""
    src = (ROOT / "tradingview" / "analysis" / "tv_sector_heatmap.py").read_text(
        encoding="utf-8"
    )
    # The BUG-008 fix block must be present
    assert 'exchange == "all"' in src, (
        "BUG-LOG-001 regression: 'all' exchange guard missing from build_sector_data()"
    )
    # The fix must appear BEFORE the _SCANNER_URLS.get() call (not after)
    all_guard_idx = src.find('exchange == "all"')
    scanner_get_idx = src.find("_SCANNER_URLS.get(exchange)")
    assert all_guard_idx < scanner_get_idx, (
        "BUG-LOG-001 regression: 'all' guard must appear before _SCANNER_URLS.get() call"
    )


# ── BUG-LOG-002: webull get_live_positions → get_holdings (2026-05-02 log) ──
def test_buglog002_global_dashboard_uses_get_holdings_not_get_live_positions():
    """global_portfolio_dashboard.py must import get_holdings() from webull_au_live,
    not the non-existent get_live_positions() which caused ImportError in production."""
    src = (ROOT / "global_portfolio_dashboard.py").read_text(encoding="utf-8")
    # The wrong import must NOT be present
    assert "get_live_positions" not in src, (
        "BUG-LOG-002 regression: get_live_positions import still present in "
        "global_portfolio_dashboard.py — this function does not exist in webull_au_live.py"
    )
    # The correct import must be present
    assert "get_holdings" in src, (
        "BUG-LOG-002 regression: get_holdings import missing from "
        "global_portfolio_dashboard.py"
    )


def test_buglog002_fetch_webull_live_maps_get_holdings_fields():
    """_fetch_webull_live() must map get_holdings() field names:
    'ticker' (not 'symbol'), 'costPrice' (not 'avg_cost'), 'mktValue' (not 'current_price')."""
    src = (ROOT / "global_portfolio_dashboard.py").read_text(encoding="utf-8")
    # After the BUG-LOG-002 fix, the field mapping must handle get_holdings() names
    assert '"ticker"' in src, (
        "BUG-LOG-002 regression: 'ticker' field lookup missing from _fetch_webull_live()"
    )
    assert '"costPrice"' in src, (
        "BUG-LOG-002 regression: 'costPrice' field lookup missing from _fetch_webull_live()"
    )
    assert '"mktValue"' in src, (
        "BUG-LOG-002 regression: 'mktValue' field lookup missing from _fetch_webull_live()"
    )
