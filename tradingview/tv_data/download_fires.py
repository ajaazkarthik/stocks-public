"""
Download all fires from WIN alerts and analyse which symbols triggered.
Reads TradingView session credentials from tradingview/cookie (the same file
used by watchlist_cleaner.py, tv_pro_scanner.py, tv_screener_fundamentals.py,
etc.) -- no credentials are hardcoded in this file (fixed 2026-07-06; this
previously had a stale, non-refreshing session/session_sign hardcoded here,
which would silently degrade to "Total fires: 0" once it expired, with no
diagnostic pointing at the actual cause).
"""
import requests
import re
import sys
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).parent.parent
COOKIE_FILE = ROOT / "cookie"

if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


class DownloadError(RuntimeError):
    """Raised when the fires download fails or returns a malformed payload.

    A failed download must never be mistaken for a valid empty result: the
    caller exits nonzero and leaves the existing cache untouched.
    """


def _validate_fire_records(fires):
    """Validate individual fire records before the cache is replaced.

    A structurally valid envelope (``{"s": "ok", "r": [...]}``) can still
    carry malformed records. Publishing those replaced a good cache with data
    that later raised ``KeyError('alert_id')`` during analysis — after the
    previous cache was already gone. Every record must be a JSON object
    carrying the identifiers the downstream pipeline requires.
    """
    for i, fire in enumerate(fires):
        if not isinstance(fire, dict):
            raise DownloadError(
                f"malformed fire record at index {i}: not a JSON object")
        if fire.get("alert_id") is None:
            raise DownloadError(
                f"malformed fire record at index {i}: missing 'alert_id'")
        if not (fire.get("pro_symbol") or fire.get("symbol")):
            raise DownloadError(
                f"malformed fire record at index {i}: missing symbol")
    return fires


def _validate_fires_response(status_code, body_text):
    """Validate a /list_fires HTTP response and return its fire list.

    Raises DownloadError on an HTTP failure, invalid JSON, an unsuccessful API
    status, a malformed result structure, or a malformed individual record. A
    successful response with an empty ``r`` list is a valid empty result and
    returns ``[]`` -- it is distinguishable from an error because errors raise
    instead of returning.
    """
    if status_code != 200:
        raise DownloadError(f"HTTP {status_code} from /list_fires")
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError) as exc:
        raise DownloadError(f"invalid JSON from /list_fires: {exc}") from exc
    if not isinstance(payload, dict):
        raise DownloadError("malformed /list_fires payload: not a JSON object")
    if payload.get("s") != "ok":
        raise DownloadError(
            f"unsuccessful /list_fires response (s={payload.get('s')!r})")
    fires = payload.get("r")
    if not isinstance(fires, list):
        raise DownloadError("malformed /list_fires payload: 'r' is not a list")
    return _validate_fire_records(fires)


def _publish_fires_cache(dest, all_fires, fires_win2h, fires_win1h):
    """Atomically replace the fires cache with the validated payload.

    Writes a temporary file in the destination directory and replaces the
    cache in one step, so a crash mid-write cannot leave a truncated cache.
    Preserves the established {"all", "win2h", "win1h"} schema.
    """
    dest = Path(dest)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump({'all': all_fires, 'win2h': fires_win2h, 'win1h': fires_win1h}, f, indent=2)
    os.replace(tmp, dest)
    return dest


def _read_cookie(cookie_file: Path = COOKIE_FILE) -> tuple[str, str]:
    """Parse sessionid and sessionid_sign from the TV cookie file.
    Returns (sessionid, sessionid_sign) -- both empty strings on failure.
    """
    if not cookie_file.exists():
        return "", ""
    raw = cookie_file.read_text(encoding="utf-8").strip()
    sid = sid_sign = ""
    for part in raw.split(";"):
        p = part.strip()
        if p.startswith("sessionid="):
            sid = p[len("sessionid="):]
        elif p.startswith("sessionid_sign="):
            sid_sign = p[len("sessionid_sign="):]
    return sid, sid_sign


session, session_sign = _read_cookie()
if not session or not session_sign:
    print(f"ERROR: could not read sessionid/sessionid_sign from {COOKIE_FILE}")
    print("       Update that file with a fresh TradingView session cookie and retry.")
    sys.exit(1)

# --- Auth ---
s = requests.Session()
s.cookies.set('sessionid', session, domain='.tradingview.com')
s.cookies.set('sessionid_sign', session_sign, domain='.tradingview.com')
s.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36'})
resp = s.get('https://www.tradingview.com/chart/', timeout=30)
m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
jwt_token = m.group(1) if m else None
print(f"JWT obtained: {bool(jwt_token)}")
if not jwt_token:
    print("ERROR: could not extract auth_token from tradingview.com/chart/ response.")
    print("       Your session cookie is likely expired -- refresh tradingview/cookie and retry.")
    sys.exit(1)

pa_base = 'https://pricealerts.tradingview.com'
headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {jwt_token}',
    'Origin': 'https://www.tradingview.com',
    'Referer': 'https://www.tradingview.com/chart/',
}
cookies = {'sessionid': session, 'sessionid_sign': session_sign}

def list_fires(payload_params=None):
    """Call /list_fires with the correct {"payload": ...} wrapper.

    Raises DownloadError on any HTTP failure, unsuccessful API response,
    invalid JSON, or malformed result structure. A valid empty result
    ({"s": "ok", "r": []}) returns [] and is NOT an error.
    """
    body = {"payload": payload_params or {}}
    r = requests.post(f'{pa_base}/list_fires', 
                      headers=headers, cookies=cookies,
                      json=body, timeout=15)
    return _validate_fires_response(r.status_code, r.text)

print("\n=== Downloading fires for WIN alerts ===")
# Canonical WIN alert IDs live in tv_data/win_alerts.py so the downloader and
# signal_stack.py share one definition (review 2026-10-13, Issue 3).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tv_data.win_alerts import WIN_2H_ID, WIN_1H_ID

# Single call fetches everything -- win2h/win1h are derived by filtering this
# result client-side instead of making 2 redundant alert-scoped API calls.
#
# Do not add pagination here. Probed 2026-08-19 across 25 parameter shapes:
# /list_fires is hard-capped server-side at 100 fires per alert (200 total for
# these two). `limit` only caps downward; limit>=300 still returns 200 and
# limit=5000 is rejected. `offset`/`skip`/`start`/`from`, every cursor spelling
# (before_fire_id, from_fire_id, next_fire_id, from_id, last_fire_id, cursor,
# before_id, max_fire_id), every time-window spelling, and `alert_id` scoping
# are all *silently ignored* -- they return the identical 200 rows, which is why
# the 2026-07-06 probes looked inconclusive. Older fires are unreachable via the
# API; database/fires_ledger.py is what preserves them.
try:
    all_fires = list_fires({"limit": 1000})
except DownloadError as exc:
    print(f"ERROR: fires download failed — {exc}")
    print("       Existing fires_raw.json cache left unchanged.")
    sys.exit(1)
print(f"All fires (limit=1000): {len(all_fires)} fires")

fires_win2h = [f for f in all_fires if f.get('alert_id') == WIN_2H_ID]
fires_win1h = [f for f in all_fires if f.get('alert_id') == WIN_1H_ID]
print(f"  WIN 2H (alert_id={WIN_2H_ID}): {len(fires_win2h)} fires")
print(f"  WIN 1H (alert_id={WIN_1H_ID}): {len(fires_win1h)} fires")

# Save raw — atomically, and only after the payload has been validated above.
# A failed download raises DownloadError before reaching here, so the previous
# cache is preserved byte-for-byte instead of being replaced with empty arrays.
_publish_fires_cache(ROOT / 'fires_raw.json', all_fires, fires_win2h, fires_win1h)
print("Saved fires_raw.json")

# fires_raw.json is overwritten every run and TradingView drops anything past
# its newest 100 per alert, so the ledger is the only surviving history.
try:
    from database.fires_ledger import count as _ledger_count, record_fires

    _new = record_fires(all_fires, source="download")
    print(f"Ledger: {_new} new fires recorded ({_ledger_count()} total known)")
except Exception as exc:  # noqa: BLE001 - ledger must never block a download
    print(f"WARNING: could not update fires ledger: {exc}")

# --- Analyse ---
print("\n" + "="*80)
print("FIRE ANALYSIS")
print("="*80)

win_fires = [f for f in all_fires if f['alert_id'] in (WIN_2H_ID, WIN_1H_ID)]
print(f"\nWIN alert fires: {len(win_fires)}")

# Group by symbol
by_symbol = defaultdict(list)
for fire in win_fires:
    sym = fire.get('pro_symbol') or fire.get('symbol', 'UNKNOWN')
    by_symbol[sym].append(fire)

print(f"\nDistinct symbols that fired: {len(by_symbol)}")
print("\nTop symbols fired:")
for sym, fires_list in sorted(by_symbol.items(), key=lambda x: -len(x)):
    # Parse direction from message
    directions = []
    for f in fires_list:
        msg = f.get('message', '')
        if msg.startswith('BUY') or 'BUY: true' in msg:
            directions.append('BUY')
        elif msg.startswith('SELL') or 'SELL: true' in msg:
            directions.append('SELL')
        else:
            directions.append('?')
    dir_counts = Counter(directions)
    print(f"  {sym}: {len(fires_list)} fires | {dict(dir_counts)}")

# Show detailed for last 20 fires
print("\n=== Last 20 Fire Events ===")
recent = sorted(all_fires, key=lambda x: x.get('fire_id', 0), reverse=True)[:20]
for fire in recent:
    sym = fire.get('pro_symbol') or fire.get('symbol', '?')
    aid = fire.get('alert_id')
    tf = fire.get('resolution', '?')
    msg = fire.get('message', '')[:120]
    fid = fire.get('fire_id', 0)
    # Try to determine direction
    direction = 'BUY' if msg.startswith('BUY') else ('SELL' if msg.startswith('SELL') else '?')
    print(f"  [{fid}] alert={aid} tf={tf}m {direction} {sym}")
    print(f"    msg: {msg[:100]}")

print("\n\n=== BUY Signal Summary ===")
buy_fires = [f for f in win_fires if f.get('message', '').startswith('BUY') or 'BUY: true' in f.get('message', '')]
sell_fires = [f for f in win_fires if f.get('message', '').startswith('SELL') or 'SELL: true' in f.get('message', '')]
print(f"BUY signals: {len(buy_fires)}")
print(f"SELL signals: {len(sell_fires)}")

print("\nBUY signals by symbol:")
buy_syms = Counter(f.get('pro_symbol') or f.get('symbol') for f in buy_fires)
for sym, count in buy_syms.most_common(20):
    print(f"  {sym}: {count}")

print("\nSELL signals by symbol:")
sell_syms = Counter(f.get('pro_symbol') or f.get('symbol') for f in sell_fires)
for sym, count in sell_syms.most_common(20):
    print(f"  {sym}: {count}")

# Parse first BUY fire message for field names
if buy_fires:
    print("\n\n=== Sample BUY Fire Message ===")
    sample = buy_fires[0]
    print(f"Symbol: {sample.get('pro_symbol')}")
    print(f"Alert: {sample.get('alert_id')}")
    print(f"TF: {sample.get('resolution')}m")
    print(f"Message:\n{sample.get('message', '')}")

# ─── CONVICTION SUMMARY (trader-facing) ──────────────────────────────────────
print("\n" + "=" * 60)
print("  FIRE DOWNLOAD COMPLETE")
print("=" * 60)
print(f"  Total fires cached   : {len(all_fires)}")
print(f"  WIN alert fires      : {len(win_fires)}")
print(f"  Unique symbols       : {len(by_symbol)}")
print(f"  BUY signals          : {len(buy_fires)}")
print(f"  SELL signals         : {len(sell_fires)}")
high_conv = [(sym, cnt) for sym, cnt in buy_syms.most_common() if cnt >= 3]
print(f"  High conviction (3+x): {len(high_conv)}")
if high_conv:
    print(f"  -> {', '.join(f'{sym}({n})' for sym, n in high_conv[:8])}")
print(f"\n  -> Run Signal Stack (option 14) to rank and score these symbols.")
print("=" * 60)
