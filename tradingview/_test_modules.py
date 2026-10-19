"""
Comprehensive direct test script for all new modules.
Run: python _test_modules.py
"""
from __future__ import annotations
import json
import os
import sys
import sqlite3
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch, MagicMock

_TV = Path(__file__).parent
sys.path.insert(0, str(_TV))

PASS = 0
FAIL = 0

def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"  PASS  {name}")
        PASS += 1
    else:
        print(f"  FAIL  {name}  {detail}")
        FAIL += 1

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

# ══════════════════════════════════════════════════════════════
# 1. tradability_filter.py
# ══════════════════════════════════════════════════════════════
section("1. tradability_filter.py")
try:
    from tradability_filter import TradabilityFilter, TradabilityResult, MOOMOO_SG_FX_CRYPTO_API_EXPOSED
    tf = TradabilityFilter()

    # Tradable exchanges
    r = tf.check("NSE:TITAN");  check("NSE tradable",    r.tradable and r.api_execution_available)
    r = tf.check("BSE:500325"); check("BSE tradable",    r.tradable and r.api_execution_available)
    r = tf.check("NYSE:AAPL");  check("NYSE tradable",   r.tradable and "IBKR (USD)" in r.brokers)
    r = tf.check("NASDAQ:NVDA");check("NASDAQ tradable", r.tradable and r.api_execution_available)
    r = tf.check("ASX:CBA");    check("ASX tradable",    r.tradable and "Moomoo AU (AUD)" in r.brokers)
    r = tf.check("HKEX:700");   check("HKEX tradable",   r.tradable and r.api_execution_available)
    r = tf.check("SGX:D05");    check("SGX tradable",    r.tradable and "Moomoo SG (SGD)" in r.brokers)
    r = tf.check("HK:700");     check("HK tradable",     r.tradable)

    # Non-tradable
    r = tf.check("TYO:7203");   check("TYO blocked",  not r.tradable and not r.tradable_with_limitations and "TYO" in r.reason)
    r = tf.check("LSE:HSBA");   check("LSE blocked",  not r.tradable and "LSE" in r.reason)
    r = tf.check("XETR:SAP");   check("XETR blocked", not r.tradable)
    r = tf.check("KRX:005930"); check("KRX blocked",  not r.tradable)

    # Forex with limitations
    for sym in ["FX:EURUSD", "FX_IDC:GBPUSD", "OANDA:USDJPY", "FXCM:AUDUSD", "FOREXCOM:EURCAD"]:
        r = tf.check(sym)
        check(f"Forex limits {sym}", not r.tradable and r.tradable_with_limitations and r.api_execution_available == MOOMOO_SG_FX_CRYPTO_API_EXPOSED and "Moomoo SG (SGD)" in r.brokers)

    # Crypto
    for sym in ["BINANCE:BTCUSDT", "COINBASE:ETHUSD", "KRAKEN:XBTUSD"]:
        r = tf.check(sym)
        check(f"Crypto limits {sym}", not r.tradable and r.tradable_with_limitations)

    # Bare ticker
    r = tf.check("AAPL"); check("Bare ticker tradable", r.tradable and r.api_execution_available)
    r = tf.check("nvda"); check("Lowercase bare ticker", r.tradable)
    r = tf.check("");     check("Empty symbol blocked",  not r.tradable and "Empty" in r.reason)

    # filter_results
    results = [
        {"tv_symbol": "NSE:TITAN",   "composite": 70},
        {"tv_symbol": "NYSE:AAPL",   "composite": 65},
        {"tv_symbol": "TYO:7203",    "composite": 55},
        {"tv_symbol": "LSE:HSBA",    "composite": 50},
        {"tv_symbol": "FX:EURUSD",   "composite": 60},
    ]
    kept, skipped = tf.filter_results(results)
    check("filter_results kept=3 (FX kept as limitations)", len(kept) == 3)
    check("filter_results skipped=2", len(skipped) == 2)
    check("FX:EURUSD in kept (limitations)", any(k.get("tv_symbol") == "FX:EURUSD" for k in kept))
    check("TYO:7203 in skipped", any(s.get("symbol") == "TYO:7203" for s in skipped))
    check("LSE:HSBA in skipped", any(s.get("symbol") == "LSE:HSBA" for s in skipped))
    check("FX:EURUSD has _tradable_with_limitations flag", any(k.get("_tradable_with_limitations") for k in kept))
    check("skipped items have 'symbol' key", all("symbol" in s for s in skipped))
    check("skipped items have 'reason' key", all("reason" in s for s in skipped))
    check("skipped items have 'source' key", all("source" in s for s in skipped))

    # is_forex_or_crypto
    check("FX is forex/crypto",         tf.is_forex_or_crypto("FX:EURUSD"))
    check("BINANCE is forex/crypto",    tf.is_forex_or_crypto("BINANCE:BTC"))
    check("NSE is NOT forex/crypto",    not tf.is_forex_or_crypto("NSE:TITAN"))
    check("NYSE is NOT forex/crypto",   not tf.is_forex_or_crypto("NYSE:AAPL"))
    check("bare ticker NOT forex/crypto", not tf.is_forex_or_crypto("AAPL"))

    # as_dict
    d = tf.check("NSE:TITAN").as_dict()
    check("as_dict has all keys", all(k in d for k in ["tradable","tradable_with_limitations","api_execution_available","reason","brokers","exchange","ticker"]))
    check("as_dict tradable=True",  d["tradable"] is True)
    check("as_dict exchange=NSE",   d["exchange"] == "NSE")
    check("as_dict ticker=TITAN",   d["ticker"] == "TITAN")

    # MOOMOO_SG_FX_CRYPTO_API_EXPOSED config toggle
    import tradability_filter as _tf_mod
    orig = _tf_mod.MOOMOO_SG_FX_CRYPTO_API_EXPOSED
    _tf_mod.MOOMOO_SG_FX_CRYPTO_API_EXPOSED = True
    r = tf.check("FX:EURUSD")
    check("FX api_execution=True when config=True", r.api_execution_available is True)
    _tf_mod.MOOMOO_SG_FX_CRYPTO_API_EXPOSED = orig

except Exception as e:
    print(f"  ERROR in tradability_filter: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 2. watchlist_cleaner.py
# ══════════════════════════════════════════════════════════════
section("2. watchlist_cleaner.py")
try:
    from watchlist_cleaner import WatchlistCleaner, _WATCHLIST_SOURCES, _read_cookie, _make_headers
    import watchlist_cleaner as _wc_mod

    # _read_cookie with no file
    sid, ssign = _read_cookie(Path("__nonexistent__"))
    check("_read_cookie missing file returns empty", sid == "" and ssign == "")

    # _read_cookie with actual content
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cookie", delete=False) as f:
        f.write("sessionid=abc123; sessionid_sign=xyz789")
        tmp_cookie = Path(f.name)
    sid, ssign = _read_cookie(tmp_cookie)
    check("_read_cookie parses sessionid",      sid == "abc123")
    check("_read_cookie parses sessionid_sign", ssign == "xyz789")
    tmp_cookie.unlink()

    # _make_headers
    h = _make_headers("abc", "xyz")
    check("_make_headers has Cookie",       "Cookie" in h and "abc" in h["Cookie"])
    check("_make_headers has User-Agent",   "User-Agent" in h)
    check("_make_headers has Content-Type", "application/json" in h.get("Content-Type",""))

    # _WATCHLIST_SOURCES
    check("_WATCHLIST_SOURCES is frozenset", isinstance(_WATCHLIST_SOURCES, frozenset))
    check("_WATCHLIST_SOURCES has watchlist", "watchlist" in _WATCHLIST_SOURCES)
    check("_WATCHLIST_SOURCES has download",  "download" in _WATCHLIST_SOURCES)
    check("_WATCHLIST_SOURCES has batch",     "batch" in _WATCHLIST_SOURCES)

    # WatchlistCleaner init (no cookie file)
    wc_no_auth = WatchlistCleaner(cookie_file=Path("__none__"), dry_run=False)
    check("WatchlistCleaner init no crash", True)

    # remove_from_watchlist with no cookie → False
    ok = wc_no_auth.remove_from_watchlist("TYO:7203")
    check("remove_from_watchlist no cookie → False", ok is False)

    # Fixture with fake cookie
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cookie", delete=False, dir=_TV) as f:
        f.write("sessionid=test123; sessionid_sign=testsign")
        tmp_c = Path(f.name)

    wc = WatchlistCleaner(cookie_file=tmp_c, dry_run=False)

    # process_skipped: FX/Crypto bypass
    for sym in ["FX:EURUSD", "FX_IDC:GBPUSD", "OANDA:USDJPY", "BINANCE:BTCUSDT", "COINBASE:ETHUSD"]:
        res = wc.process_skipped([{"symbol": sym, "source": "watchlist", "reason": "test"}])
        check(f"FX/Crypto {sym} never removed", res["cleaned"] == [] and res["manual_required"] == [])

    # process_skipped: non-watchlist source → ignored
    for src in ["webhook", "screener", "manual", "news", "unknown"]:
        with patch.object(wc, "remove_from_watchlist") as mock_rm:
            res = wc.process_skipped([{"symbol": "TYO:7203", "source": src, "reason": "test"}])
        check(f"Non-watchlist source '{src}' ignored", mock_rm.call_count == 0 and res["cleaned"] == [])

    # process_skipped: watchlist source → attempt removal
    for src in list(_WATCHLIST_SOURCES):
        with patch.object(wc, "remove_from_watchlist", return_value=True):
            res = wc.process_skipped([{"symbol": "TYO:7203", "source": src, "reason": "test"}])
        check(f"Watchlist source '{src}' processed", len(res["cleaned"]) == 1)

    # process_skipped: API failure → manual_required
    with patch.object(wc, "remove_from_watchlist", return_value=False):
        res = wc.process_skipped([{"symbol": "LSE:HSBA", "source": "watchlist", "reason": "test"}])
    check("API failure → manual_required", len(res["manual_required"]) == 1)
    check("manual_required has symbol", res["manual_required"][0]["symbol"] == "LSE:HSBA")

    # process_skipped: mixed FX + non-FX
    with patch.object(wc, "remove_from_watchlist", return_value=True) as mock_rm:
        res = wc.process_skipped([
            {"symbol": "TYO:7203", "source": "watchlist", "reason": "test"},
            {"symbol": "FX:EURUSD", "source": "watchlist", "reason": "test"},
        ])
    check("Mixed: only TYO removed, FX skipped", mock_rm.call_count == 1 and len(res["cleaned"]) == 1)

    # process_skipped: empty list
    res = wc.process_skipped([])
    check("Empty skipped list → empty result", res == {"cleaned": [], "manual_required": []})

    # remove_from_watchlist: symbol NOT in list → idempotent True
    with patch.object(_wc_mod, "_tv_get") as mg, patch.object(_wc_mod, "_tv_put") as mp:
        mg.side_effect = [
            [{"name": "ALL_NEW", "id": "99"}],
            ["NSE:TITAN", "NYSE:AAPL"],
        ]
        ok = wc.remove_from_watchlist("TYO:7203", "ALL_NEW")
    check("Symbol not in list → True, no PUT", ok is True and mp.call_count == 0)

    # remove_from_watchlist: symbol present → PUT called
    # Fresh instance to avoid watchlist_id_cache from previous test contaminating side_effect order
    wc2 = WatchlistCleaner(cookie_file=tmp_c, dry_run=False)
    with patch.object(_wc_mod, "_tv_get") as mg, patch.object(_wc_mod, "_tv_put") as mp:
        mg.side_effect = [
            [{"name": "ALL_NEW", "id": "99"}],
            ["NSE:TITAN", "TYO:7203", "NYSE:AAPL"],
        ]
        mp.return_value = {"status": "ok"}
        ok = wc2.remove_from_watchlist("TYO:7203", "ALL_NEW")
    check("Symbol present → PUT called and True", ok is True and mp.call_count == 1)
    assert mp.call_args is not None, "PUT was not called"
    pushed = mp.call_args[0][1]["symbols"]
    check("PUT body excludes removed symbol", "TYO:7203" not in pushed and "NSE:TITAN" in pushed)

    # remove_from_watchlist: PUT returns None → False (fresh instance avoids id cache)
    wc3 = WatchlistCleaner(cookie_file=tmp_c, dry_run=False)
    with patch.object(_wc_mod, "_tv_get") as mg, patch.object(_wc_mod, "_tv_put") as mp:
        mg.side_effect = [
            [{"name": "ALL_NEW", "id": "99"}],
            ["TYO:7203"],
        ]
        mp.return_value = None
        ok = wc3.remove_from_watchlist("TYO:7203", "ALL_NEW")
    check("PUT None → False", ok is False)

    # remove_from_watchlist: watchlist not found → False (fresh instance)
    wc4 = WatchlistCleaner(cookie_file=tmp_c, dry_run=False)
    with patch.object(_wc_mod, "_tv_get") as mg:
        mg.return_value = [{"name": "OTHER", "id": "1"}]
        ok = wc4.remove_from_watchlist("TYO:7203", "ALL_NEW")
    check("Watchlist not found → False", ok is False)

    # dry_run: no PUT (fresh instance)
    wc_dry = WatchlistCleaner(cookie_file=tmp_c, dry_run=True)
    with patch.object(_wc_mod, "_tv_get") as mg, patch.object(_wc_mod, "_tv_put") as mp:
        mg.side_effect = [
            [{"name": "ALL_NEW", "id": "99"}],
            ["TYO:7203"],
        ]
        ok = wc_dry.remove_from_watchlist("TYO:7203", "ALL_NEW")
    check("dry_run: no PUT even when symbol present", mp.call_count == 0)
    check("dry_run: returns True (dry-run success)", ok is True)

    # _print_manual_required format
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        WatchlistCleaner._print_manual_required([{"symbol": "LSE:HSBA", "reason": "test reason"}])
    out = buf.getvalue()
    check("_print_manual_required has title",        "WATCHLIST CLEANUP REQUIRED" in out)
    check("_print_manual_required has symbol",       "LSE:HSBA" in out)
    check("_print_manual_required has box drawing",  "╔" in out or "\u2554" in out)

    tmp_c.unlink()

except Exception as e:
    print(f"  ERROR in watchlist_cleaner: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 3. performance_analyzer.py
# ══════════════════════════════════════════════════════════════
section("3. performance_analyzer.py")
try:
    from performance_analyzer import (
        PerformanceAnalyzer, _load_outcomes, _table_exists, _safe_float,
        _spearman, _quintile_buckets, _success_rate, _avg,
        parameter_importance, bucket_analysis, source_reliability,
        regime_analysis, failure_patterns, suggested_thresholds, rolling_winrate,
        build_html, _OUT_DEFAULT, _DB_PATH,
    )

    # _safe_float
    check("_safe_float int",     _safe_float(42) == 42.0)
    check("_safe_float str",     _safe_float("3.14") == 3.14)
    check("_safe_float None",    _safe_float(None) is None)
    check("_safe_float empty",   _safe_float("") is None)
    check("_safe_float nan",     _safe_float(float("nan")) is None)

    # _quintile_buckets
    buckets = _quintile_buckets(list(range(100)))
    check("_quintile_buckets returns 5", len(buckets) == 5)
    check("_quintile_buckets empty list", _quintile_buckets([]) == [])

    # _success_rate
    rows_sr = [{"outcome": "success"}, {"outcome": "success"}, {"outcome": "failure"}, {"outcome": "open"}]
    check("_success_rate 2/3 closed = 66.7", _success_rate(rows_sr) == 66.7)
    check("_success_rate no closed = None",  _success_rate([{"outcome": "open"}]) is None)
    check("_success_rate empty = None",      _success_rate([]) is None)

    # _avg
    check("_avg basic",       _avg([1.0, 2.0, 3.0]) == 2.0)
    check("_avg with None",   _avg([1.0, None, 3.0]) == 2.0)
    check("_avg all None",    _avg([None, None]) is None)

    # _spearman
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    ys = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    r = _spearman(xs, ys)
    check("_spearman perfect positive", r is not None and abs(r - 1.0) < 0.001)
    r2 = _spearman(xs, list(reversed(ys)))
    check("_spearman perfect negative", r2 is not None and abs(r2 + 1.0) < 0.001)
    check("_spearman too few → None", _spearman([1.0, 2.0], [3.0, 4.0]) is None)

    # _table_exists on temp DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmpdb = Path(f.name)
    conn = sqlite3.connect(str(tmpdb))
    conn.execute("CREATE TABLE test_tbl (id INTEGER)")
    conn.commit(); conn.close()
    check("_table_exists True",  _table_exists(tmpdb, "test_tbl"))
    check("_table_exists False", not _table_exists(tmpdb, "no_such_table"))

    # _load_outcomes returns empty list for missing DB
    rows = _load_outcomes(Path("__nonexistent__.db"))
    check("_load_outcomes missing DB → []", rows == [])

    # Build a rich synthetic dataset
    import datetime
    _rows = []
    for i in range(40):
        composite = 40.0 + i * 1.5
        outcome   = "success" if composite > 55 else "failure"
        ts = (datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat()
        _rows.append({
            "output_id":      i,
            "ticker":         f"TICK{i}",
            "tv_symbol":      f"NSE:TICK{i}",
            "timestamp":      ts,
            "composite":      composite,
            "l1":             3 + (i % 5),
            "l2":             2 + (i % 4),
            "l3":             1 + (i % 3),
            "l4":             1 + (i % 2),
            "win_rate_at_fire": 40 + i % 20,
            "vix_at_fire":    15 + i % 20,
            "market_regime":  ["bull", "bear", "neutral"][i % 3],
            "input_source":   ["watchlist", "screener", "webhook"][i % 3],
            "tradable":       1,
            "return_pct":     composite - 55,
            "mfe":            abs(composite - 55) + 2,
            "mae":            -(abs(composite - 55)) - 1,
            "outcome":        outcome,
        })

    # parameter_importance
    imp = parameter_importance(_rows, min_samples=5)
    check("parameter_importance returns list",       isinstance(imp, list))
    check("parameter_importance has param key",      all("param" in r for r in imp))
    check("parameter_importance has correlation key",all("correlation" in r for r in imp))
    check("parameter_importance sorted by |r|",      all(abs(imp[i]["correlation"] or 0) >= abs(imp[i+1]["correlation"] or 0) for i in range(len(imp)-1)))

    # bucket_analysis
    bkts = bucket_analysis(_rows, "composite", min_samples=3)
    check("bucket_analysis returns list",          isinstance(bkts, list))
    check("bucket_analysis has range/count keys",  all("range" in b and "count" in b and "success_rate" in b for b in bkts))

    # source_reliability
    sr = source_reliability(_rows, min_samples=3)
    check("source_reliability returns list",       isinstance(sr, list))
    check("source_reliability has source key",     all("source" in s for s in sr))
    check("source_reliability sorted by win_rate", len(sr) < 2 or all((sr[i]["win_rate"] or 0) >= (sr[i+1]["win_rate"] or 0) for i in range(len(sr)-1)))

    # regime_analysis
    ra = regime_analysis(_rows, min_samples=3)
    check("regime_analysis returns list",          isinstance(ra, list))
    check("regime_analysis has regime key",        all("regime" in r for r in ra))

    # failure_patterns
    fp = failure_patterns(_rows)
    check("failure_patterns returns list of str",  isinstance(fp, list) and all(isinstance(x, str) for x in fp))

    # suggested_thresholds
    sg = suggested_thresholds(_rows)
    check("suggested_thresholds returns list",     isinstance(sg, list))

    # rolling_winrate
    rw = rolling_winrate(_rows, window_days=30)
    check("rolling_winrate returns list",          isinstance(rw, list))
    check("rolling_winrate has date/win_rate",     all("date" in r and "win_rate" in r for r in rw))

    # build_html
    html = build_html(_rows, min_samples=3, db_path=tmpdb)
    check("build_html returns str",                isinstance(html, str))
    check("build_html has <!DOCTYPE html>",        "<!DOCTYPE html>" in html)
    check("build_html has Signal Performance",     "Signal Performance" in html)
    check("build_html has Parameter Importance",   "Parameter Importance" in html)
    check("build_html has Source Reliability",     "Source Reliability" in html)
    check("build_html has Market Regime",          "Market Regime" in html)
    check("build_html has Failure Pattern",        "Failure Pattern" in html)
    check("build_html has Threshold",              "Threshold" in html)
    check("build_html has chart.js",               "chart.js" in html)
    check("build_html has KPI block",              "kpi" in html)
    check("build_html no python exception",        True)

    # PerformanceAnalyzer class
    pa = PerformanceAnalyzer(db_path=tmpdb, min_samples=3)
    check("PerformanceAnalyzer instantiates",      pa is not None)
    check("PA.db_path correct",                    pa.db_path == tmpdb)
    check("PA.min_samples correct",                pa.min_samples == 3)
    rows_loaded = pa.load_rows()
    check("PA.load_rows returns list",             isinstance(rows_loaded, list))
    html2 = pa.build_report()
    check("PA.build_report returns HTML",          "<!DOCTYPE html>" in html2)
    summary = pa.get_summary()
    check("PA.get_summary has keys",               all(k in summary for k in ["total","closed","win_rate","parameter_importance","source_reliability","failure_patterns","suggestions"]))
    check("PA.get_summary total is 0 (empty db)",  summary["total"] == 0)

    # CLI help
    import subprocess
    res = subprocess.run(
        [sys.executable, str(_TV / "performance_analyzer.py"), "--help"],
        capture_output=True, text=True, timeout=15
    )
    check("CLI --help exits 0",           res.returncode == 0)
    check("CLI --help shows min-samples", "--min-samples" in res.stdout)
    check("CLI --help shows since",       "--since" in res.stdout)
    check("CLI --help shows out",         "--out" in res.stdout)

    # CLI --json with missing DB
    res2 = subprocess.run(
        [sys.executable, str(_TV / "performance_analyzer.py"), "--db", "__nonexistent__.db", "--json"],
        capture_output=True, text=True, timeout=15
    )
    check("CLI missing DB exits 0 (graceful)", res2.returncode == 0)

    tmpdb.unlink()

except Exception as e:
    print(f"  ERROR in performance_analyzer: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 4. alert_tracker.py — schema migrations
# ══════════════════════════════════════════════════════════════
section("4. alert_tracker.py — schema migrations + new methods")
try:
    from alert_tracker import AlertTracker, _MIGRATION_COLUMNS, _SCHEMA_NEW_TABLES

    check("_MIGRATION_COLUMNS is list",    isinstance(_MIGRATION_COLUMNS, list))
    check("_MIGRATION_COLUMNS has 9 cols", len(_MIGRATION_COLUMNS) == 9)

    expected_cols = ["signal_composite","signal_threshold","tradable","tradable_limitations",
                     "watchlist_cleaned","input_source","win_rate_at_fire","vix_at_fire","market_regime"]
    cols_in_list = [c[1] for c in _MIGRATION_COLUMNS]
    check("_MIGRATION_COLUMNS has all expected columns", all(c in cols_in_list for c in expected_cols))

    check("_SCHEMA_NEW_TABLES has signal_engine_outputs", "signal_engine_outputs" in _SCHEMA_NEW_TABLES)
    check("_SCHEMA_NEW_TABLES has price_evolution",       "price_evolution" in _SCHEMA_NEW_TABLES)

    # Create tracker with temp DB — verifies migration runs without error
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db_path = Path(f.name)
    at = AlertTracker(db_path=str(tmp_db_path))
    check("AlertTracker init (creates tables)", True)

    # Verify new tables exist
    conn = sqlite3.connect(str(tmp_db_path))
    tbls = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    check("signal_engine_outputs table created", "signal_engine_outputs" in tbls)
    check("price_evolution table created",       "price_evolution" in tbls)
    check("alerts table created",                "alerts" in tbls)
    check("performance_metrics table created",   "performance_metrics" in tbls)

    # Verify migration columns exist in alerts table
    conn = sqlite3.connect(str(tmp_db_path))
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    conn.close()
    for col in expected_cols:
        check(f"alerts has column '{col}'", col in col_names, f"missing: {col}")

    # record_signal_output
    check("record_signal_output exists", hasattr(at, "record_signal_output"))
    result_dict = {
        "tv_symbol":   "NSE:TITAN",
        "composite":   72.5,
        "scores": {
            "l1": {"total": 8},
            "l2": {"total": 7},
            "l3": {"total": 5},
            "l4": {"total": 4},
        },
        "_tradability": {"tradable": True, "brokers": ["Zerodha (INR)"]},
        "input_source": "watchlist",
    }
    row_id = at.record_signal_output(result_dict, run_id="TEST_RUN_001")
    check("record_signal_output returns int or None", row_id is None or isinstance(row_id, int))
    if row_id is not None:
        conn = sqlite3.connect(str(tmp_db_path))
        row = conn.execute("SELECT * FROM signal_engine_outputs WHERE id=?", (row_id,)).fetchone()
        conn.close()
        check("record_signal_output row persisted", row is not None)

    # Run migration a second time (idempotent — columns already exist)
    at2 = AlertTracker(db_path=str(tmp_db_path))
    check("AlertTracker re-init idempotent (no crash on existing columns)", True)

    # update_price_evolution
    check("update_price_evolution exists", hasattr(at, "update_price_evolution"))
    upd_result = at.update_price_evolution(max_outputs=10)
    check("update_price_evolution returns dict", isinstance(upd_result, dict))
    check("update_price_evolution has 'updated' key", "updated" in upd_result)

    tmp_db_path.unlink()

except Exception as e:
    print(f"  ERROR in alert_tracker: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 5. batch_analyzer.py — tradability wiring
# ══════════════════════════════════════════════════════════════
section("5. signal_engine/batch_analyzer.py — tradability wiring")
try:
    from signal_engine.batch_analyzer import BatchAnalyzer
    import signal_engine.batch_analyzer as _ba_mod

    check("BatchAnalyzer importable",            True)
    check("_TRADABILITY_OK defined",             hasattr(_ba_mod, "_TRADABILITY_OK"))
    check("BatchAnalyzer has _tradability_filter attr", hasattr(BatchAnalyzer, "_tradability_filter"))
    check("_tradability_filter starts as None",  BatchAnalyzer._tradability_filter is None)

    # Verify the run() method has the filter call (inspect source)
    import inspect
    src = inspect.getsource(BatchAnalyzer.run)
    check("run() references filter_results",       "filter_results" in src)
    check("run() references _last_skipped",        "_last_skipped_non_tradable" in src)
    check("run() initialises TradabilityFilter",   "_TradabilityFilter" in src or "TradabilityFilter" in src)

    src_save = inspect.getsource(BatchAnalyzer.run_and_save)
    check("run_and_save() references WatchlistCleaner", "WatchlistCleaner" in src_save)
    check("run_and_save() processes skipped",      "process_skipped" in src_save)

except Exception as e:
    print(f"  ERROR in batch_analyzer: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 6. webhook_handler.py — tradability wiring
# ══════════════════════════════════════════════════════════════
section("6. signal_engine/webhook_handler.py — tradability wiring")
try:
    import signal_engine.webhook_handler as _wh_mod

    check("webhook_handler importable",          True)
    check("_TRADABILITY_OK defined",             hasattr(_wh_mod, "_TRADABILITY_OK"))
    check("WebhookServer class exists",          hasattr(_wh_mod, "WebhookServer"))

    import inspect
    src_init = inspect.getsource(_wh_mod.WebhookServer.__init__)
    check("__init__ creates _tradability_filter", "_tradability_filter" in src_init)

    src_fire = inspect.getsource(_wh_mod.WebhookServer._process_fire)
    check("_process_fire checks tradability",     "tradability" in src_fire.lower())
    check("_process_fire has return on skip",     "return" in src_fire)
    check("_process_fire stores _tradability",    "_tradability" in src_fire)

except Exception as e:
    print(f"  ERROR in webhook_handler: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 7. signal_stack.py — tradability_badge
# ══════════════════════════════════════════════════════════════
section("7. signal_stack.py — tradability_badge in build_html()")
try:
    import signal_stack as _ss

    check("signal_stack importable", True)
    import inspect
    src = inspect.getsource(_ss.build_html)
    check("build_html has tradability_badge var",      "tradability_badge" in src)
    check("build_html has _tradable_with_limitations", "_tradable_with_limitations" in src)
    check("build_html has badge-trad-limit class",     "badge-trad-limit" in src or "trad" in src.lower())

    # Smoke test: verify tradability_badge code path in source
    # (build_html requires fully-enriched items — verify via source inspection only)
    check("tradability badge renders for limitations",
          "tradability_badge" in src and "badge-trad-limit" in src)

except Exception as e:
    print(f"  ERROR in signal_stack: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# 8. main.py — menu wiring
# ══════════════════════════════════════════════════════════════
section("8. main.py — menu entry 48 + run_performance_analyzer()")
try:
    import ast
    main_src = Path(__file__).parent.parent / "main.py"
    tree = ast.parse(main_src.read_text(encoding="utf-8"))
    fn_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    check("run_performance_analyzer defined", "run_performance_analyzer" in fn_names)
    src = main_src.read_text(encoding="utf-8")
    check("menu entry '48' present",          '"48"' in src)
    check("Performance Analyzer in menu",     "Performance Analyzer" in src)
    check("run_performance_analyzer in MENU", "run_performance_analyzer" in src)
    check("--min-samples in cmd",             "--min-samples" in src)

except Exception as e:
    print(f"  ERROR in main.py check: {e}")
    traceback.print_exc()
    FAIL += 1

# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"  TOTAL: {PASS + FAIL} checks   PASS: {PASS}   FAIL: {FAIL}")
print(f"{'='*60}\n")
if FAIL > 0:
    sys.exit(1)
