"""
Integration test for L5 badge rendering — end-to-end pipeline verification.

Tests the FULL path from raw broker API responses → IndiaEnricher → html_badge()
and from enrichment dicts → l5_html_badge() (Moomoo), generating real HTML output.

Run:
    cd d:\workspace\repo\stocks\tradingview
    python test_l5_integration.py

Outputs:
    test_l5_integration_output.html  — visual proof of badge states
    PASS/FAIL summary to stdout
"""

import sys
import os
import types
import traceback

# ── Path setup ─────────────────────────────────────────────────────────────────
THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
INDIA_DIR  = os.path.join(os.path.dirname(THIS_DIR), "india-brokers")
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, INDIA_DIR)

FAILURES = []
PASSES   = []

def check(name: str, condition: bool, got=None, expected=None):
    if condition:
        PASSES.append(name)
        print(f"  ✓ {name}")
    else:
        FAILURES.append(name)
        print(f"  ✗ {name}")
        if got is not None:
            print(f"      got:      {repr(got)[:200]}")
        if expected is not None:
            print(f"      expected: {repr(expected)[:200]}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Moomoo l5_html_badge
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Section 1: Moomoo l5_html_badge ──────────────────────────────────────")

from moomoo_enricher import l5_html_badge

# State 1: empty dict (L5 disabled / Moomoo not running at all)
b = l5_html_badge({})
check("S1-empty-dict → silent empty string", b == "", got=b)

# State 2: NSE stock (market unsupported by Moomoo)
e_nse = {"l5_score": None, "l5_error": "Market not supported by Moomoo: NSE"}
b = l5_html_badge(e_nse)
check("S2-NSE → 'market unsupported' dim badge", "market unsupported" in b, got=b)
check("S2-NSE → NOT amber warning", "⚠" not in b, got=b)

# State 3: US stock, Moomoo NOT running / no rights
e_us_down = {"l5_score": None, "l5_error": "No quote rights for US market (requires Moomoo subscription)"}
b = l5_html_badge(e_us_down)
check("S3-US-down → 'no Moomoo data' amber badge", "no Moomoo data" in b, got=b)
check("S3-US-down → has ⚠", "⚠" in b, got=b)
check("S3-US-down → has tooltip with error text", "No quote rights" in b, got=b)

# State 3b: API error (OpenD not running)
e_us_conn = {"l5_score": None, "l5_error": "OpenD connection refused"}
b = l5_html_badge(e_us_conn)
check("S3b-conn-refused → amber badge", "no Moomoo data" in b, got=b)
check("S3b-conn-refused → tooltip shows error", "OpenD connection refused" in b, got=b)

# State 4: real score data
e_scored = {"l5_score": 7.3, "l5_grade": "B", "mm_code": "US.AAPL", "l5_detail": {}, "l5_data": {}}
b = l5_html_badge(e_scored)
check("S4-scored → shows score", "7.3" in b, got=b)
check("S4-scored → shows grade B", "[B]" in b, got=b)

moomoo_badges = {
    "empty":      l5_html_badge({}),
    "nse_unsup":  l5_html_badge(e_nse),
    "us_down":    l5_html_badge(e_us_down),
    "us_scored":  l5_html_badge(e_scored),
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — India IndiaEnricher with MOCK broker data
#             Tests the full path: mock API response → _refresh() → enrich() → html_badge()
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Section 2: India IndiaEnricher pipeline ──────────────────────────────")

# ── Mock SmartApi module ──────────────────────────────────────────────────────

MOCK_HOLDINGS_ZERODHA = [
    {"tradingsymbol": "INFY",    "quantity": 50,  "average_price": 1500.0, "last_price": 1800.0, "pnl": 15000.0},
    {"tradingsymbol": "TCS",     "quantity": 10,  "average_price": 3000.0, "last_price": 3500.0, "pnl":  5000.0},
    {"tradingsymbol": "MCX",     "quantity": 5,   "average_price": 2500.0, "last_price": 2000.0, "pnl": -2500.0},
    {"tradingsymbol": "ZOMATO",  "quantity": 200, "average_price":  120.0, "last_price":   80.0, "pnl": -8000.0},
]

# Angel One returns the NEW dict format {"holdings": [...], "totalholding": {...}}
MOCK_HOLDINGS_ANGELONE_DICT_FORMAT = {
    "status": True,
    "message": "SUCCESS",
    "errorcode": "",
    "data": {
        "holdings": [
            {"tradingsymbol": "INFY",   "quantity": 25, "averageprice": 1600.0, "ltp": 1800.0, "profitandloss": 5000.0},
            {"tradingsymbol": "BEL",    "quantity": 100,"averageprice":  180.0, "ltp":  230.0, "profitandloss": 5000.0},
        ],
        "totalholding": {"totalholdingvalue": 100000.0, "totalinvvalue": 95000.0}
    }
}

# Old format — plain list (also tested to confirm backwards compat)
MOCK_HOLDINGS_ANGELONE_LIST_FORMAT = [
    {"tradingsymbol": "RELIANCE", "quantity": 5, "averageprice": 2500.0, "ltp": 2800.0, "profitandloss": 1500.0},
]

# ── Inject mock pyotp + SmartApi ──────────────────────────────────────────────

mock_pyotp = types.ModuleType("pyotp")
class _FakeTOTP:
    def __init__(self, secret): pass
    def now(self): return "123456"
mock_pyotp.TOTP = _FakeTOTP
sys.modules["pyotp"] = mock_pyotp

mock_smartapi_pkg = types.ModuleType("SmartApi")
class _FakeSmartConnect:
    def __init__(self, api_key=None): pass
    def generateSession(self, client_code, pin, totp):
        return {"status": True, "data": {"jwtToken": "fake_token"}}
    def holding(self):
        return MOCK_HOLDINGS_ANGELONE_DICT_FORMAT  # returns new dict format
mock_smartapi_pkg.SmartConnect = _FakeSmartConnect
sys.modules["SmartApi"] = mock_smartapi_pkg

# ── Inject mock kiteconnect ───────────────────────────────────────────────────

mock_kite = types.ModuleType("kiteconnect")
class _FakeKiteConnect:
    def __init__(self, api_key=None): pass
    def set_access_token(self, token): pass
    def holdings(self):
        return MOCK_HOLDINGS_ZERODHA
mock_kite.KiteConnect = _FakeKiteConnect
sys.modules["kiteconnect"] = mock_kite

# ── Set env vars so both brokers attempt login ────────────────────────────────
os.environ["ZERODHA_ACCESS_TOKEN"] = "fake_access_token"
os.environ["ANGELONE_API_KEY"]     = "fake_key"
os.environ["ANGELONE_CLIENT_CODE"] = "TESTCODE"
os.environ["ANGELONE_PIN"]         = "1234"
os.environ["ANGELONE_TOTP_SECRET"] = "FAKESECRETFAKESECRET"

# ── Import and instantiate IndiaEnricher ─────────────────────────────────────

try:
    from india_enricher import IndiaEnricher
    enricher = IndiaEnricher(verbose=True)
    print(f"  Holdings loaded: {list(enricher._holdings.keys())}")
    check("Zerodha connected",  enricher._zerodha_ok)
    check("AngelOne connected", enricher._angelone_ok)
    check("No load errors",     len(enricher._errors) == 0,
          got=enricher._errors)
except Exception as e:
    FAILURES.append("IndiaEnricher construction")
    print(f"  ✗ IndiaEnricher construction CRASHED: {e}")
    traceback.print_exc()
    sys.exit(1)

# ── Test: INFY (held by both Zerodha & AngelOne) ─────────────────────────────
print("\n  -- NSE:INFY (held by both brokers) --")
e_infy = enricher.enrich("NSE:INFY")
check("INFY: holding=True",          e_infy.get("holding") == True, got=e_infy.get("holding"))
check("INFY: total_qty=75",          e_infy.get("total_qty") == 75, got=e_infy.get("total_qty"))
check("INFY: brokers=['zerodha','angelone']", sorted(e_infy.get("brokers",[])) == ["angelone","zerodha"],
      got=sorted(e_infy.get("brokers",[])))
check("INFY: broker_status.zerodha=True",   e_infy.get("broker_status",{}).get("zerodha") == True)
check("INFY: broker_status.angelone=True",  e_infy.get("broker_status",{}).get("angelone") == True)
check("INFY: pnl_pct > 0",          e_infy.get("combined_pnl_pct",0) > 0, got=e_infy.get("combined_pnl_pct"))
badge_infy = enricher.html_badge(e_infy)
check("INFY badge: HOLDING text",    "HOLDING" in badge_infy, got=badge_infy)
check("INFY badge: shows qty 75",    "75" in badge_infy, got=badge_infy)
print(f"    Badge: {badge_infy}")

# ── Test: BEL (held by AngelOne only, not in Zerodha) ────────────────────────
print("\n  -- NSE:BEL (held by AngelOne only) --")
e_bel = enricher.enrich("NSE:BEL")
check("BEL: holding=True",          e_bel.get("holding") == True, got=e_bel.get("holding"))
check("BEL: brokers=['angelone']",  e_bel.get("brokers") == ["angelone"], got=e_bel.get("brokers"))
badge_bel = enricher.html_badge(e_bel)
check("BEL badge: HOLDING text",    "HOLDING" in badge_bel, got=badge_bel)
print(f"    Badge: {badge_bel}")

# ── Test: MCX (held by Zerodha, negative PnL) ────────────────────────────────
print("\n  -- NSE:MCX (held at -20% loss) --")
e_mcx = enricher.enrich("NSE:MCX")
check("MCX: holding=True",          e_mcx.get("holding") == True, got=e_mcx.get("holding"))
check("MCX: pnl_pct < 0",          e_mcx.get("combined_pnl_pct",0) < 0, got=e_mcx.get("combined_pnl_pct"))
badge_mcx = enricher.html_badge(e_mcx)
check("MCX badge: HOLDING text",    "HOLDING" in badge_mcx, got=badge_mcx)
print(f"    Badge: {badge_mcx}")

# ── Test: HDFC (not held by anyone — FRESH) ─────────────────────────────────
print("\n  -- NSE:HDFCBANK (not held — FRESH) --")
e_hdfc = enricher.enrich("NSE:HDFCBANK")
check("HDFCBANK: holding=False",     e_hdfc.get("holding") == False, got=e_hdfc.get("holding"))
check("HDFCBANK: has broker_status", "broker_status" in e_hdfc, got=list(e_hdfc.keys()))
badge_hdfc = enricher.html_badge(e_hdfc)
check("HDFCBANK badge: FRESH text",  "FRESH" in badge_hdfc, got=badge_hdfc)
check("HDFCBANK badge: no ⚠",        "⚠" not in badge_hdfc, got=badge_hdfc)  # both brokers ok
print(f"    Badge: {badge_hdfc}")

# ── Test: non-India symbol → empty dict ──────────────────────────────────────
print("\n  -- NASDAQ:AAPL (non-India — should be empty) --")
e_aapl = enricher.enrich("NASDAQ:AAPL")
check("AAPL: empty dict",            e_aapl == {}, got=e_aapl)
badge_aapl = enricher.html_badge(e_aapl)
check("AAPL badge: silent empty",    badge_aapl == "", got=badge_aapl)

# ── Test: both brokers down (neither_ok) ─────────────────────────────────────
print("\n  -- Simulate both brokers down --")
enricher._zerodha_ok = False
enricher._angelone_ok = False
e_down = enricher.enrich("NSE:WIPRO")  # not held — triggers neither_ok path
badge_down = enricher.html_badge(e_down)
check("Both-down badge: '⚠ Broker data N/A'", "Broker data N/A" in badge_down, got=badge_down)
check("Both-down badge: amber colour",         "d97706" in badge_down, got=badge_down)
print(f"    Badge: {badge_down}")

# ── Test: angel one old list format (backwards compat) ───────────────────────
print("\n  -- AngelOne old list format (backwards compat) --")
enricher._zerodha_ok = True
enricher._angelone_ok = True

class _FakeSmartConnectListFmt:
    def __init__(self, api_key=None): pass
    def generateSession(self, client_code, pin, totp):
        return {"status": True, "data": {"jwtToken": "fake_token"}}
    def holding(self):
        # Returns OLD format: {"data": [...plain list...]}
        return {"status": True, "data": MOCK_HOLDINGS_ANGELONE_LIST_FORMAT}
mock_smartapi_pkg.SmartConnect = _FakeSmartConnectListFmt
sys.modules["SmartApi"] = mock_smartapi_pkg

# Force re-import of _load_angelone to use new mock
enricher._last_refresh = 0  # force re-load
enricher._zerodha_ok = False
enricher._angelone_ok = False
enricher._holdings = {}
enricher._refresh()
check("List-format: AngelOne ok",          enricher._angelone_ok, got=enricher._errors)
check("List-format: RELIANCE loaded",       "RELIANCE" in enricher._holdings, got=list(enricher._holdings.keys()))
e_reliance = enricher.enrich("NSE:RELIANCE")
check("List-format: RELIANCE holding=True", e_reliance.get("holding") == True, got=e_reliance.get("holding"))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Generate verification HTML
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Section 3: Generating verification HTML ──────────────────────────────")

# Reset enricher to full dict-format data with both brokers up
class _FakeSmartConnectDictFmt:
    def __init__(self, api_key=None): pass
    def generateSession(self, client_code, pin, totp):
        return {"status": True, "data": {"jwtToken": "fake_token"}}
    def holding(self):
        return MOCK_HOLDINGS_ANGELONE_DICT_FORMAT
mock_smartapi_pkg.SmartConnect = _FakeSmartConnectDictFmt
sys.modules["SmartApi"] = mock_smartapi_pkg

enricher2 = IndiaEnricher(verbose=True)

def _row(symbol: str, label: str, badge_html: str, extra: str = "") -> str:
    return f"""
    <tr>
      <td style="padding:8px 12px;color:#94a3b8;font-family:monospace">{symbol}</td>
      <td style="padding:8px 12px;color:#64748b">{label}</td>
      <td style="padding:8px 12px">{badge_html if badge_html else '<span style="color:#444;font-size:11px">[empty — silent ✓]</span>'}</td>
      <td style="padding:8px 12px;color:#64748b;font-size:11px">{extra}</td>
    </tr>"""

rows = []

# Moomoo badges
rows.append('<tr><th colspan="4" style="padding:8px 12px;background:#1e293b;color:#7dd3fc;text-align:left">Moomoo L5 Badges (l5_html_badge)</th></tr>')
rows.append(_row("NASDAQ:AAPL",  "No enrichment (Moomoo disabled)",       l5_html_badge({}),         "Expected: silent"))
rows.append(_row("NSE:INFY",     "NSE = market unsupported by Moomoo",    l5_html_badge({"l5_score":None,"l5_error":"Market not supported by Moomoo: NSE"}), "Expected: dim grey"))
rows.append(_row("NYSE:RCL",     "US: no quote rights (OpenD down)",      l5_html_badge({"l5_score":None,"l5_error":"No quote rights for US market (requires Moomoo subscription)"}), "Expected: amber ⚠"))
rows.append(_row("NYSE:AAPL",    "US: scored 7.3 B grade",                l5_html_badge({"l5_score":7.3,"l5_grade":"B","mm_code":"US.AAPL","l5_detail":{},"l5_data":{}}), "Expected: coloured badge"))

# India badges
rows.append('<tr><th colspan="4" style="padding:8px 12px;background:#1e293b;color:#4ade80;text-align:left">India L5 Badges (IndiaEnricher.html_badge) — mock data from Zerodha + AngelOne dict format</th></tr>')

for sym, label in [
    ("NASDAQ:AAPL", "Non-India → silent"),
    ("NSE:INFY",    "Held by BOTH brokers — +20% gain"),
    ("NSE:BEL",     "Held by AngelOne only"),
    ("NSE:MCX",     "Held by Zerodha, -20% loss"),
    ("NSE:HDFCBANK","Not held — FRESH opportunity"),
    ("NSE:TCS",     "Held by Zerodha, +16% gain"),
    ("NSE:ZOMATO",  "Held by Zerodha, heavy loss -33%"),
]:
    e = enricher2.enrich(sym)
    b = enricher2.html_badge(e)
    holding_info = ""
    if e.get("holding"):
        holding_info = f"qty={e.get('total_qty')}  pnl={e.get('combined_pnl_pct',0):.1f}%  brokers={e.get('brokers')}"
    elif e:
        bs = e.get("broker_status", {})
        holding_info = f"not held  z_ok={bs.get('zerodha')} a_ok={bs.get('angelone')}"
    rows.append(_row(sym, label, b, holding_info))

# Both-brokers-down scenario
enricher2._zerodha_ok = False
enricher2._angelone_ok = False
e_wipro = enricher2.enrich("NSE:WIPRO")
badge_wipro = enricher2.html_badge(e_wipro)
rows.append(_row("NSE:WIPRO", "Both brokers DOWN — cannot determine holding", badge_wipro, "Expected: amber ⚠ Broker data N/A"))

# Partial (one broker down)
enricher2._zerodha_ok  = True
enricher2._angelone_ok = False
e_cipla = enricher2.enrich("NSE:CIPLA")
badge_cipla = enricher2.html_badge(e_cipla)
rows.append(_row("NSE:CIPLA", "AngelOne DOWN, Zerodha OK — partial data", badge_cipla, "Expected: FRESH ⚠"))


html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>L5 Integration Test — Badge Verification</title>
<style>
  body {{ background:#0a0f1e; color:#e2e8f0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; padding:24px }}
  h1 {{ color:#f8fafc; margin-bottom:8px }}
  .sub {{ color:#64748b; font-size:12px; margin-bottom:24px }}
  table {{ border-collapse:collapse; width:100%; background:#0f172a; border-radius:8px; overflow:hidden }}
  th {{ color:#94a3b8; font-size:11px; text-align:left; padding:10px 12px; background:#1e293b; text-transform:uppercase; letter-spacing:.05em }}
  td {{ border-top:1px solid #1e293b }}
  tr:hover td {{ background:#111827 }}
  .result {{ margin-top:24px; padding:16px; border-radius:8px }}
  .pass {{ background:#0d2b0d; border:1px solid #166534 }}
  .fail {{ background:#2b0d0d; border:1px solid #991b1b }}
</style>
</head>
<body>
<h1>🧪 L5 Integration Test — Badge Verification</h1>
<div class="sub">Generated by test_l5_integration.py · mock data (Zerodha + AngelOne dict format) · {len(PASSES)} pass / {len(FAILURES)} fail</div>
<table>
  <tr>
    <th style="width:180px">Symbol</th>
    <th style="width:280px">Scenario</th>
    <th style="width:300px">Badge (visual)</th>
    <th>Details</th>
  </tr>
  {"".join(rows)}
</table>

<div class="result {'pass' if not FAILURES else 'fail'}">
  <b style="color:{'#4ade80' if not FAILURES else '#f87171'}">
    {'✓ ALL CHECKS PASS' if not FAILURES else f'✗ {len(FAILURES)} TEST(S) FAILED'}
  </b>
  {"" if not FAILURES else f"<br><br><b>Failed:</b><br>{'<br>'.join(FAILURES)}"}
  <br><br>
  <b>Checks passed ({len(PASSES)}):</b><br>
  {'<br>'.join(PASSES)}
</div>
</body>
</html>"""

out_path = os.path.join(THIS_DIR, "test_l5_integration_output.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html_out)
print(f"  ✓ HTML written → {out_path}")

# ══════════════════════════════════════════════════════════════════════════════
# Final summary
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{'═'*60}")
if FAILURES:
    print(f"✗  {len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f"   • {f}")
    print(f"   {len(PASSES)} pass")
    sys.exit(1)
else:
    print(f"✓  ALL {len(PASSES)} CHECKS PASSED")
    print(f"   Open: {out_path}")
    sys.exit(0)
