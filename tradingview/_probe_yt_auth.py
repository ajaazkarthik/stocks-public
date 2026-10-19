"""
Deep auth diagnostic for YouTube InnerTube member-only community posts.
Run: python _probe_yt_auth.py
"""
import json, hashlib, time, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from youtube_enricher import (
    _load_youtube_cookie, _extract_sapisid, _extract_posts_from_innertube,
    _COMMUNITY_PARAMS, _INNERTUBE_BROWSE_URL, EXPERT_CHANNELS,
)

try:
    import requests as _req
except ImportError:
    print("pip install requests"); sys.exit(1)

AKSHAT_ID = EXPERT_CHANNELS["Akshat Shrivastava"]
cookie_str = _load_youtube_cookie()
origin     = "https://www.youtube.com"

# Parse all cookies into dict
cdict = {}
for part in cookie_str.split(";"):
    part = part.strip()
    if "=" in part:
        k, v = part.split("=", 1)
        cdict[k.strip()] = v.strip()

print("=" * 60)
print("1. COOKIE INVENTORY")
for k in ["SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID",
          "__Secure-1PAPISID", "__Secure-3PAPISID", "SSID", "HSID",
          "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC", "LOGIN_INFO"]:
    v = cdict.get(k, "MISSING")
    flag = "OK" if v != "MISSING" else "MISSING ⚠️"
    print(f"  {k:<25} {flag}  {v[:28] if v != 'MISSING' else ''}")

def _make_auth(sapisid_val):
    ts = int(time.time())
    h  = hashlib.sha1(f"{ts} {sapisid_val} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

def _try_fetch(label, sapisid_val, extra_headers=None, gl="AU"):
    """Fetch community tab, return (posts, raw_str)."""
    session = _req.Session()
    for k, v in cdict.items():
        session.cookies.set(k, v, domain=".youtube.com", path="/")

    hdrs = {
        "Authorization":            _make_auth(sapisid_val),
        "X-Origin":                 origin,
        "Origin":                   origin,
        "Referer":                  "https://www.youtube.com/",
        "Content-Type":             "application/json",
        "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/124.0.0.0 Safari/537.36"),
        "Accept":                   "*/*",
        "Accept-Language":          "en-AU,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
        "X-Goog-AuthUser":          "0",
    }
    if extra_headers:
        hdrs.update(extra_headers)

    body = {
        "context": {
            "client": {
                "clientName": "WEB", "clientVersion": "2.20240101.00.00",
                "hl": "en", "gl": gl, "timeZone": "Australia/Sydney",
            },
            "user": {"lockedSafetyMode": False},
            "request": {"useSsl": True},
        },
        "browseId": AKSHAT_ID,
        "params":   _COMMUNITY_PARAMS,
    }

    resp = session.post(_INNERTUBE_BROWSE_URL, json=body, headers=hdrs, timeout=20)
    raw  = resp.json()
    raw_str = json.dumps(raw)
    posts = []
    _extract_posts_from_innertube(raw, posts, 50)
    signed_in  = '"isSignedIn":true' in raw_str or '"isSignedIn": true' in raw_str
    has_avatar = '"avatarViewModel"' in raw_str
    has_member = '"sponsorsOnlyBadge"' in raw_str or '"sponsorsOnlyPostRenderer"' in raw_str
    print(f"\n  [{label}]  HTTP {resp.status_code}  "
          f"posts={len(posts)}  signedIn={signed_in}  avatar={has_avatar}  memberPosts={has_member}")
    for p in posts[:5]:
        mo = " 👑MEMBER-ONLY" if p["is_member_only"] else ""
        print(f"    [{p['published_time']:25s}]{mo} {p['text'][:80]}")
    return posts, raw_str, raw

print()
print("2. AUTH METHOD COMPARISON (all use requests.Session cookie dict)")

sapisid = cdict.get("SAPISID", "")
sapisid_3p = cdict.get("__Secure-3PAPISID", "")

# A: SAPISID hash, US locale
_try_fetch("SAPISID / US", sapisid, gl="US")
# B: SAPISID hash, AU locale
posts_b, raw_b_str, raw_b = _try_fetch("SAPISID / AU", sapisid, gl="AU")
# C: __Secure-3PAPISID hash, AU locale
if sapisid_3p and sapisid_3p != sapisid:
    _try_fetch("3PAPISID / AU", sapisid_3p, gl="AU")
# D: No Authorization header (pure cookie, no SAPISIDHASH)
_try_fetch("NO auth header / AU", sapisid,
           extra_headers={"Authorization": ""}, gl="AU")

# ── 3. Inspect tabs in best result ────────────────────────────────────────────
print()
print()
print("3. CHANNEL TABS (from AU result)")
tabs = raw_b.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
for tab in tabs:
    tr = tab.get("tabRenderer", {})
    print(f"   {tr.get('title','?'):<25} selected={tr.get('selected',False)}"
          f"  params={tr.get('endpoint',{}).get('browseEndpoint',{}).get('params','')[:20]}")

# ── 4. Check if member posts use a different renderer key ─────────────────────
print()
print("4. ALL RENDERER TYPES IN COMMUNITY TAB RESPONSE")
all_renderers = set(re.findall(r'"([a-zA-Z]+Renderer)"\s*:', raw_b_str))
post_renderers = [r for r in sorted(all_renderers) if "post" in r.lower() or "backstage" in r.lower()]
print(f"   Post-related renderers: {post_renderers}")
print(f"   All renderers (post-related): {sorted(all_renderers)[:20]}")

# ── 5. Check onResponseReceivedActions for member signals ─────────────────────
print()
print("5. onResponseReceivedActions KEYS")
for action in raw_b.get("onResponseReceivedActions", []):
    print(f"   action keys: {list(action.keys())}")

# ── 6. Look for any locked/member post stub in raw JSON ───────────────────────
print()
print("6. LOCKED / MEMBER POST STUBS IN RAW RESPONSE")
locked_hits = re.findall(r'"([^"]*[Ll]ock[^"]*|[^"]*[Mm]ember[^"]*|[^"]*[Ss]ponsor[^"]*)"',
                          raw_b_str)
unique_hits = sorted(set(locked_hits))[:30]
for h in unique_hits:
    print(f"   {h}")

print()
print("7. TRY ALTERNATIVE PARAMS")
posts_tab_params = "EgVwb3N0c_IGBAoCSgA="  # actual Posts tab params from channel
member_params    = "EgptZW1iZXJzaGlw8gYE"   # Membership tab params

def _try_fetch_params(label, params):
    session = _req.Session()
    for k, v in cdict.items():
        session.cookies.set(k, v, domain=".youtube.com", path="/")
    ts = int(time.time())
    sha1 = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    hdrs = {
        "Authorization":            f"SAPISIDHASH {ts}_{sha1}",
        "X-Origin":                 origin,
        "Origin":                   origin,
        "Referer":                  "https://www.youtube.com/",
        "Content-Type":             "application/json",
        "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/124.0.0.0 Safari/537.36"),
        "Accept":                   "*/*",
        "Accept-Language":          "en-AU,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
        "X-Goog-AuthUser":          "0",
    }
    body = {
        "context": {
            "client": {
                "clientName": "WEB", "clientVersion": "2.20240101.00.00",
                "hl": "en", "gl": "AU", "timeZone": "Australia/Sydney",
            },
            "user": {"lockedSafetyMode": False},
            "request": {"useSsl": True},
        },
        "browseId": AKSHAT_ID,
        "params":   params,
    }
    resp = session.post(_INNERTUBE_BROWSE_URL, json=body, headers=hdrs, timeout=20)
    raw_r = resp.json()
    raw_str = json.dumps(raw_r)

    posts = []
    _extract_posts_from_innertube(raw_r, posts, 50)
    signed = '"isSignedIn":true' in raw_str or '"isSignedIn": true' in raw_str
    has_member = '"sponsorsOnlyBadge"' in raw_str or '"sponsorsOnlyPostRenderer"' in raw_str
    sponsors_renderer = '"sponsorshipsExpandedRenderer"' in raw_str or '"membershipsDetails"' in raw_str
    print(f"  [{label}]  HTTP {resp.status_code}  posts={len(posts)}"
          f"  signed={signed}  memberPosts={has_member}  sponsorsRenderer={sponsors_renderer}")
    for p in posts[:6]:
        mo = " 👑MEMBER-ONLY" if p["is_member_only"] else ""
        print(f"    [{p['published_time']:25s}]{mo} {p['text'][:80]}")
    if not posts:
        import re as _re
        renderers = set(_re.findall(r'"([a-zA-Z]+Renderer)"\s*:', raw_str))
        member_related = [r for r in sorted(renderers) if any(x in r.lower() for x in
                          ["sponsor","member","backstage","post","lock","restricted"])]
        print(f"    (no posts) Renderers: {member_related[:10]}")
        tabs = raw_r.get("contents",{}).get("twoColumnBrowseResultsRenderer",{}).get("tabs",[])
        if tabs:
            for t in tabs: print(f"      tab: {t.get('tabRenderer',{}).get('title','?')}")
        elif "header" in raw_r:
            print(f"    header keys: {list(raw_r['header'].keys())}")

_try_fetch_params("membership params", member_params)
_try_fetch_params("posts tab  params", posts_tab_params)

print()
print("=" * 60)
print("DONE")


# ── Print header structure (channel info / member perks section) ──────────
header = raw.get("header", {})
print()
print("== HEADER keys:", list(header.keys()))
if "c4TabbedHeaderRenderer" in header:
    c4 = header["c4TabbedHeaderRenderer"]
    print("   c4TabbedHeaderRenderer keys:", list(c4.keys()))
    if "badges" in c4:
        print("   badges:", c4["badges"])
