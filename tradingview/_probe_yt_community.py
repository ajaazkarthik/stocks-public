"""
End-to-end test probe for YouTube community posts via InnerTube API.
Run: python _probe_yt_community.py
"""
import json
import hashlib
import time
import re
import urllib.request
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from youtube_enricher import (
    _load_youtube_cookie, _extract_sapisid,
    _fetch_community_posts, _extract_posts_from_innertube,
    get_youtube_mentions, score_youtube,
    EXPERT_CHANNELS, MEMBER_CHANNELS,
    _COMMUNITY_PARAMS, _INNERTUBE_BROWSE_URL,
    LOOKBACK_HOURS,
)

AKSHAT_ID = EXPERT_CHANNELS["Akshat Shrivastava"]

# ── 1. Cookie check ──────────────────────────────────────────────────────────
cookie = _load_youtube_cookie()
sapisid = _extract_sapisid(cookie)
print("=" * 60)
print("1. COOKIE CHECK")
print(f"   Loaded: {'YES' if cookie else 'NO'}  ({len(cookie)} chars)")
print(f"   SAPISID: {'YES' if sapisid else 'MISSING'}")
if not cookie:
    print("ERROR: youtube-cookie file is empty or missing")
    sys.exit(1)

# ── 2. Raw InnerTube request ─────────────────────────────────────────────────
print()
print("2. RAW INNERTUBE REQUEST (community tab)")
ts     = int(time.time())
origin = "https://www.youtube.com"
sha1   = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()

headers = {
    "Cookie":                   cookie,
    "Authorization":            f"SAPISIDHASH {ts}_{sha1}",
    "X-Origin":                 origin,
    "Content-Type":             "application/json",
    "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language":          "en-US,en;q=0.9",
    "X-YouTube-Client-Name":    "1",
    "X-YouTube-Client-Version": "2.20240101.00.00",
}
body = json.dumps({
    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                            "hl": "en", "gl": "US"}},
    "browseId": AKSHAT_ID,
    "params":   _COMMUNITY_PARAMS,
}).encode()

req = urllib.request.Request(_INNERTUBE_BROWSE_URL, data=body, headers=headers, method="POST")
with urllib.request.urlopen(req, timeout=20) as r:
    raw = json.loads(r.read())

print(f"   HTTP 200 OK  — response keys: {list(raw.keys())}")

# ── 3. Auth check ────────────────────────────────────────────────────────────
print()
print("3. AUTH CHECK")
raw_str = json.dumps(raw)

# visitorData is always present; check responseContext for logged-in signals
rc = raw.get("responseContext", {})
visitor_data = rc.get("visitorData", "N/A")
print(f"   visitorData prefix: {visitor_data[:30]}")

# Check for accountMenuRenderer — only present when authenticated
has_account = '"accountMenuRenderer"' in raw_str
has_signedin = '"isSignedIn":true' in raw_str or '"isSignedIn": true' in raw_str
print(f"   accountMenuRenderer present: {has_account}")
print(f"   isSignedIn:true found:       {has_signedin}")

# Look for any member/sponsor/subscriber badge keys
member_keys = set(re.findall(
    r'"((?:[a-zA-Z]*[Mm]ember|[Ss]ponsor|membershipBadge)[a-zA-Z]*)"', raw_str
))
print(f"   Membership-related keys:     {member_keys or 'none found'}")

# ── 4. Posts extraction (all, no window) ─────────────────────────────────────
print()
print("4. ALL POSTS EXTRACTED (no lookback filter)")
all_posts = []
_extract_posts_from_innertube(raw, all_posts, max_posts=50)
print(f"   Total postRenderer items found: {len(all_posts)}")
for i, p in enumerate(all_posts):
    tag = "MEMBER-ONLY" if p["is_member_only"] else "PUBLIC    "
    print(f"   [{i+1:02d}] {tag} | {p['published_time']:30s} | {p['text'][:90]}")

# ── 5. Filtered posts (inside lookback window) ────────────────────────────────
from youtube_enricher import _post_within_lookback
in_window = [p for p in all_posts if _post_within_lookback(p["published_time"], LOOKBACK_HOURS)]
print()
print(f"5. POSTS WITHIN {LOOKBACK_HOURS}h WINDOW: {len(in_window)}")

# ── 6. Try wider window (7 days) ─────────────────────────────────────────────
in_7d = _fetch_community_posts(AKSHAT_ID, cookie, max_posts=30, lookback_hours=168)
print()
print(f"6. _fetch_community_posts (7-day window): {len(in_7d)} posts")
member_7d = [p for p in in_7d if p["is_member_only"]]
public_7d  = [p for p in in_7d if not p["is_member_only"]]
print(f"   Member-only: {len(member_7d)}   Public: {len(public_7d)}")

# ── 7. Full integration: get_youtube_mentions ─────────────────────────────────
print()
print("7. get_youtube_mentions() INTEGRATION TEST")
# Test with a keyword that appears in any recent post
for sym in ["NSE:RELIANCE", "NSE:THERMAX", "NSE:INFY", "NVDA"]:
    data  = get_youtube_mentions(sym, use_transcript=False)
    score = score_youtube(data)
    cp    = data.get("community_posts", 0)
    print(f"   {sym:<20} mentions={data['mention_count']}  "
          f"community_posts={cp}  yt_score={score:.1f}")

# ── 8. Show any stock tickers found in community posts ───────────────────────
print()
print("8. TICKER MENTIONS IN COMMUNITY POSTS (last 7 days)")
from youtube_enricher import _extract_tickers_from_text
ticker_map = {}
for p in in_7d:
    tickers = _extract_tickers_from_text(p["text"])
    for t in tickers:
        ticker_map.setdefault(t, []).append(p["text"][:80])
# Show top tickers (exclude very common English words — already done by stopwords)
for t in sorted(ticker_map, key=lambda x: -len(ticker_map[x]))[:15]:
    tag = "MEMBER" if any(p["is_member_only"] for p in in_7d if t in _extract_tickers_from_text(p["text"])) else ""
    print(f"   {t:<15} {len(ticker_map[t])} mention(s) {tag}")

print()
print("9. RAW STRUCTURE INSPECTION (auth + member-only detection)")
# Re-fetch with X-Goog-AuthUser header and AU locale
ts2    = int(time.time())
sha1_2 = hashlib.sha1(f"{ts2} {sapisid} {origin}".encode()).hexdigest()
headers2 = dict(headers)
headers2["Authorization"]  = f"SAPISIDHASH {ts2}_{sha1_2}"
headers2["X-Goog-AuthUser"] = "0"

body2 = json.dumps({
    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                            "hl": "en", "gl": "AU"}},
    "browseId": AKSHAT_ID,
    "params":   _COMMUNITY_PARAMS,
}).encode()

req2 = urllib.request.Request(_INNERTUBE_BROWSE_URL, data=body2, headers=headers2, method="POST")
with urllib.request.urlopen(req2, timeout=20) as r2:
    raw2 = json.loads(r2.read())
raw2_str = json.dumps(raw2)

_SIGNED_IN = '"isSignedIn":true'
_ACCOUNT_MENU = '"accountMenuRenderer"'
_SPONSORS_POST = '"sponsorsOnlyPostRenderer"'
_SPONSORS_BADGE = '"sponsorsOnlyBadge"'
_MEMBERSHIP_BADGE = '"membershipBadge"'
_IS_LOCKED = '"isLocked"'
_EMAIL = '"email"'

# Auth signals
print(f"   isSignedIn:true          -> {'YES' if _SIGNED_IN in raw2_str else 'NO'}")
print(f"   accountMenuRenderer      -> {'YES' if _ACCOUNT_MENU in raw2_str else 'NO'}")
print(f"   sponsorsOnlyPostRenderer -> {'YES' if _SPONSORS_POST in raw2_str else 'NO'}")
print(f"   sponsorsOnlyBadge        -> {'YES' if _SPONSORS_BADGE in raw2_str else 'NO'}")
print(f"   membershipBadge          -> {'YES' if _MEMBERSHIP_BADGE in raw2_str else 'NO'}")
print(f"   isLocked                 -> {'YES' if _IS_LOCKED in raw2_str else 'NO'}")
print(f"   email                    -> {'YES' if _EMAIL in raw2_str else 'NO'}")

# Find any key with member/sponsor/signed in it
member_keys = set()
for key in re.findall(r'"([a-zA-Z]{3,60})":', raw2_str):
    lk = key.lower()
    if any(x in lk for x in ("member", "sponsor", "signed", "auth", "lock", "subscription")):
        member_keys.add(key)
print(f"   Membership/auth keys: {sorted(member_keys)}")

# Dump raw items to see first post's full structure
all_posts2 = []
_extract_posts_from_innertube(raw2, all_posts2, max_posts=50)
print(f"   Posts (AU locale): {len(all_posts2)}")

# Dump the raw tabs/content structure to see what postRenderer types exist
import json as _json
tabs = raw2.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
if tabs:
    for tab in tabs:
        tab_r = tab.get("tabRenderer", {})
        if "community" in tab_r.get("title", "").lower() or tab_r.get("selected"):
            content = tab_r.get("content", {})
            section = content.get("sectionListRenderer", {})
            items = section.get("contents", [])
            print(f"   Community tab items: {len(items)}")
            for item in items[:2]:
                for sub in item.get("itemSectionRenderer", {}).get("contents", []):
                    keys = list(sub.keys())
                    print(f"     Post renderer type: {keys}")
                    if "postRenderer" in sub:
                        pr = sub["postRenderer"]
                        pr_keys = list(pr.keys())
                        print(f"     postRenderer keys: {pr_keys}")

print()
print("10. ALL POSTS + TICKERS (no time filter — full scan)")
all_recent = _fetch_community_posts(AKSHAT_ID, cookie, max_posts=30, lookback_hours=200*24)
print(f"    Total posts: {len(all_recent)}")
print()
for p in all_recent:
    tickers = sorted(_extract_tickers_from_text(p["text"]))
    meaningful = [t for t in tickers if len(t) >= 3][:12]
    tag = " [MEMBER-ONLY]" if p["is_member_only"] else ""
    print(f"  [{p['published_time']}]{tag}")
    print(f"  {p['text'][:180]}")
    print(f"  TICKERS FOUND: {meaningful}")
    print()

print()
print("11. DETECTION DEMO — 180-day community window (proves pipeline works)")
import youtube_enricher as _yt_mod
_orig = _yt_mod.COMMUNITY_LOOKBACK_HOURS
_yt_mod.COMMUNITY_LOOKBACK_HOURS = 180 * 24

for sym in ["NVDA", "AMD", "BTC", "CHINA"]:
    data  = get_youtube_mentions(sym, use_transcript=False)
    score = score_youtube(data)
    cp    = data.get("community_posts", 0)
    if data["mention_count"] > 0:
        label = "(community)" if cp > 0 else "(video)"
        print(f"  {sym:<10} mentions={data['mention_count']} {label} score={score:.1f}/10")
        for v in data["videos"]:
            src  = "COMMUNITY POST" if v.get("is_community_post") else "VIDEO"
            mo   = " [MEMBER-ONLY]" if v.get("is_member_only") else ""
            print(f"    [{src}]{mo} {v['title'][:100]}")
    else:
        print(f"  {sym:<10} no mentions in 180-day window")

_yt_mod.COMMUNITY_LOOKBACK_HOURS = _orig

print()
print("=" * 60)
print("DONE")
