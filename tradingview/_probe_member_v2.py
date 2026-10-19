"""
Deep membership probe v2:
- Expands channel list to 25+ Indian stock experts
- Tests both AUTHENTICATED (your cookies) and UNAUTHENTICATED fetch
  → YouTube shows locked post SHELLS to non-members in unauthenticated calls
- Discovers which channels post member-only content you're missing

Run: python _probe_member_v2.py
"""
import sys, json, hashlib, time, re, requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from youtube_enricher import _load_youtube_cookie, _INNERTUBE_BROWSE_URL, MEMBER_CHANNELS

origin = "https://www.youtube.com"

# ── Extended channel roster (beyond our current 12) ──────────────────────────
# Focus: channels known for paying members, exclusive research, PMS/fund managers
ALL_CHANNELS: dict[str, str] = {
    # ── CURRENT 12 ───────────────────────────────────────────────────────────
    "Akshat Shrivastava":   "UCqW8jxh4tH1Z1sWPbkGWL4g",  # JOINED
    "SOIC":                 "UCB7GnQlJPIL6rBBqEoX87vA",
    "Parimal Ade":          "UCUWbS9qoyLKTdOlvgK1GBlA",
    "Shankar Nath":         "UCtnItzU7q_bA1eoEBjqcVrw",
    "Shyam Sekhar":         "UChvpJLpR41w08f-LE6fEcEA",
    "Pranjal Kamra":        "UCwAdQUuPT6laN-AQR17fe1g",
    "Rachana Ranade":       "UCe3qdG0A_gr-sEdat5y2twQ",  # has Join button
    "Vivek Bajaj":          "UCmkNoDHBfKzPnXWTNoYQc7A",
    "Nikhil Kamath":        "UCUZY7-p1bUidPEEYD9sF8EA",
    "ET Money":             "UCU21CybXH91lv7ay7ABezLw",
    "Warikoo":              "UCRzYN32xtBf3Yxsx5BvJWJw",
    "Groww":                "UCw5TLrz3qADabwezTEcOmgQ",
    # ── HIGH-ALPHA DEEP VALUE / COMPOUNDERS ───────────────────────────────────
    "Capitalmind":          "UCBJ4pF-0wVjxXZipNWXiNgA",  # @capitalmind  (Deepak Shenoy)
    "Deepak Shenoy":        "UCAe8lo7nCzGevSAf98KrFNw",  # @deepakshenoy  (personal channel)
    "Porinju Veliyath":     "UCwlDQNK-EQuguOlcv2tsEnQ",  # @porinju
    "Neeraj Marathe":       "UCfJkQXEUC8QH9LgPbaMFjdw",  # @NeerajMarathe
    "Safal Niveshak":       "UCcXACV7kOf0zMGIGZx0NSeg",  # @SafalNiveshak
    "Dr Vijay Malik":       "UCOi-zY1Fs_L19htF2Tap6Mw",  # @VijayMalik
    # ── PMS / INSTITUTIONAL RESEARCH ─────────────────────────────────────────
    "PMS Bazaar":           "UCoDYS49xO8Py2v9yOmS9d8Q",  # @PMSBazaar
    # ── TECHNICAL / TRADING SPECIALISTS ──────────────────────────────────────
    "Trade With Trend":     "UCPgT-N-DQ0K0H88skjaDgkA",  # @TradeWithTrend
    "Subasish Pani":        "UC20X5BKuiZq7c_Rux4GI3gw",  # @SubasishPani
    # ── FINANCIAL EDUCATORS (broader) ────────────────────────────────────────
    "Pushkar Raj Thakur":   "UC85pMpgYd3JxpQXXLueoHrQ",  # @PushkarRajThakur
    "Yadnya Investment":    "UCtrJz27bLRkCsgI_0DEXZig",  # @YadnyaInvestmentAcademy
}

# ── InnerTube helpers ─────────────────────────────────────────────────────────
_COMMUNITY_PARAMS = "EgVwb3N0c_IGBAoCSgA="

def _make_authed_session(cookie_str: str):
    cdict = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cdict[k.strip()] = v.strip()
    sapisid = cdict.get("SAPISID", "")
    ts = int(time.time())
    sha1 = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    session = requests.Session()
    for k, v in cdict.items():
        session.cookies.set(k, v, domain=".youtube.com", path="/")
    hdrs = {
        "Authorization":            f"SAPISIDHASH {ts}_{sha1}",
        "X-Origin":                 origin, "Origin": origin,
        "Content-Type":             "application/json",
        "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language":          "en-AU,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
        "X-Goog-AuthUser":          "0",
    }
    return session, hdrs

def _make_anon_session():
    """Unauthenticated session — YouTube shows locked-post SHELLS to non-members."""
    session = requests.Session()
    hdrs = {
        "Content-Type":             "application/json",
        "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language":          "en-AU,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
        "Origin":                   origin,
    }
    return session, hdrs

def _browse_body(channel_id: str) -> dict:
    return {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                                    "hl": "en", "gl": "AU"}},
            "browseId": channel_id, "params": _COMMUNITY_PARAMS}

def _fetch_raw(ch_id: str, session: requests.Session, hdrs: dict) -> dict | None:
    try:
        resp = session.post(_INNERTUBE_BROWSE_URL, json=_browse_body(ch_id), headers=hdrs, timeout=18)
        return resp.json()
    except Exception as e:
        return {"_error": str(e)}

# ── Deep JSON scanners ────────────────────────────────────────────────────────

def _find_key(obj, key: str) -> bool:
    """Is key present anywhere in the JSON tree?"""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key(v, key) for v in obj.values())
    elif isinstance(obj, list):
        return any(_find_key(x, key) for x in obj)
    return False

def _count_key(obj, key: str, n: list | None = None) -> int:
    if n is None:
        n = [0]
    if isinstance(obj, dict):
        if key in obj:
            n[0] += 1
        for v in obj.values():
            _count_key(v, key, n)
    elif isinstance(obj, list):
        for x in obj:
            _count_key(x, key, n)
    return n[0]

def _extract_all_post_renderers(obj, out=None):
    """Return all backstagePostRenderer / postRenderer dicts including locked ones."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        if "backstagePostThreadRenderer" in obj:
            _extract_all_post_renderers(obj["backstagePostThreadRenderer"].get("post", {}), out)
            return out
        if "backstagePostRenderer" in obj or "postRenderer" in obj:
            out.append(obj.get("backstagePostRenderer") or obj["postRenderer"])
            return out
        for v in obj.values():
            _extract_all_post_renderers(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _extract_all_post_renderers(x, out)
    return out

def _get_text(post: dict) -> str:
    runs = post.get("contentText", {}).get("runs", [])
    return "".join(r.get("text","") for r in runs).strip()

def _get_time(post: dict) -> str:
    pt = post.get("publishedTimeText", {})
    if isinstance(pt, dict):
        r = pt.get("runs", [])
        return r[0].get("text","") if r else pt.get("simpleText","")
    return ""

def _has_sponsor_badge(post: dict) -> bool:
    return "sponsorsOnlyBadge" in post

def _has_locked_renderer(post: dict) -> bool:
    return _find_key(post, "lockedPostRenderer") or _find_key(post, "paidContentOverlayRenderer")

def _get_teaser(post: dict) -> str:
    """Get teaser text from locked post if available."""
    locked = []
    def _get(obj):
        if isinstance(obj, dict):
            if "lockedPostRenderer" in obj:
                locked.append(obj["lockedPostRenderer"])
            for v in obj.values():
                _get(v)
        elif isinstance(obj, list):
            for x in obj:
                _get(x)
    _get(post)
    for ld in locked:
        runs = ld.get("teaserText", {}).get("runs", [])
        t = "".join(r.get("text","") for r in runs).strip()
        if t:
            return t
        # Try description
        runs = ld.get("description", {}).get("runs", [])
        t = "".join(r.get("text","") for r in runs).strip()
        if t:
            return t
    return ""


def analyse(ch_name: str, ch_id: str, authed_raw: dict, anon_raw: dict) -> dict:
    """Compare authed vs anon responses to understand membership picture."""

    # ── Extract posts from both responses ────────────────────────────────────
    authed_posts = _extract_all_post_renderers(authed_raw) if authed_raw else []
    anon_posts   = _extract_all_post_renderers(anon_raw)   if anon_raw  else []

    # ── Classify authed posts ──────────────────────────────────────────────
    pub_readable    = []
    mem_readable    = []  # member-only + we can read → WE ARE MEMBER
    mem_locked_auth = []  # member-badge but empty text → locked even for us (shouldn't happen if member)

    for p in authed_posts:
        text    = _get_text(p)
        t       = _get_time(p)
        is_mem  = _has_sponsor_badge(p)
        if is_mem and text:
            mem_readable.append({"time": t, "text": text[:100]})
        elif is_mem and not text:
            mem_locked_auth.append({"time": t, "teaser": _get_teaser(p)})
        elif text:
            pub_readable.append({"time": t, "text": text[:100]})

    # ── Classify anon posts — these reveal locked shells ───────────────────
    anon_pub    = []
    anon_locked = []  # posts listed but locked for anon → member-only posts

    for p in anon_posts:
        text    = _get_text(p)
        t       = _get_time(p)
        is_mem  = _has_sponsor_badge(p)
        is_lock = _has_locked_renderer(p)
        if (is_mem and not text) or is_lock:
            anon_locked.append({"time": t,
                                "teaser": _get_teaser(p),
                                "badge_keys": [k for k in p if "sponsor" in k.lower() or "member" in k.lower() or "locked" in k.lower()]})
        elif text:
            anon_pub.append({"time": t, "text": text[:100]})

    # ── Detect membership feature in raw JSON strings ─────────────────────
    raw_s       = json.dumps(anon_raw or {})
    has_join_btn = "sponsorButton" in raw_s or "joinButton" in raw_s or "SPONSORSHIPS_JOIN" in raw_s
    has_sponsor  = "sponsorPromoRenderer" in raw_s or "membershipOfferRenderer" in raw_s

    # ── Determine status ──────────────────────────────────────────────────
    if mem_readable:
        status = "JOINED"         # we can read their private posts
    elif anon_locked or mem_locked_auth:
        status = "LOCKED"         # we see locked post shells → join to unlock
    elif has_join_btn or has_sponsor:
        status = "HAS-MEMBERSHIP" # Join button present but no exclusive posts detected
    else:
        status = "NONE"

    return {
        "status":          status,
        "pub_readable":    pub_readable,
        "mem_readable":    mem_readable,
        "mem_locked_auth": mem_locked_auth,
        "anon_locked":     anon_locked,
        "anon_pub":        anon_pub,
        "has_join_btn":    has_join_btn,
        "has_sponsor":     has_sponsor,
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

print("=" * 72)
print("DEEP MEMBERSHIP RESEARCH: Indian Stock Expert YouTube Channels")
print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"Channels to probe: {len(ALL_CHANNELS)}")
print("=" * 72)

cookie    = _load_youtube_cookie()
auth_sess, auth_hdrs = _make_authed_session(cookie)
anon_sess, anon_hdrs = _make_anon_session()

results: dict[str, dict] = {}

for ch_name, ch_id in ALL_CHANNELS.items():
    we_member = ch_id in MEMBER_CHANNELS
    # Authed fetch
    authed_raw = _fetch_raw(ch_id, auth_sess, auth_hdrs)
    time.sleep(0.4)  # polite pacing
    # Anon fetch (reveals locked shells)
    anon_raw = _fetch_raw(ch_id, anon_sess, anon_hdrs)
    time.sleep(0.4)

    res = analyse(ch_name, ch_id, authed_raw, anon_raw)
    results[ch_name] = res

    st     = res["status"]
    n_pub  = len(res["pub_readable"])
    n_mr   = len(res["mem_readable"])
    n_al   = len(res["anon_locked"])

    status_icon = ("✓ JOINED"       if st == "JOINED" else
                   "🔒 LOCKED"      if st == "LOCKED" else
                   "💳 HAS-MEMB"    if st == "HAS-MEMBERSHIP" else
                   "— NONE")
    print(f"\n  {ch_name:<35}  [{status_icon}]")
    print(f"  Authed: {n_pub} public  {n_mr} member-read  |  Anon: {len(res['anon_pub'])} pub  {n_al} locked")

    if res["mem_readable"]:
        print(f"  ── MEMBER POSTS (READABLE) ({n_mr}) ──")
        for p in res["mem_readable"][:4]:
            print(f"    [{p['time']:18s}]  {p['text'][:75]}")

    if res["anon_locked"]:
        print(f"  ── LOCKED POSTS (anon view) ({n_al}) — JOIN TO UNLOCK ──")
        for p in res["anon_locked"][:4]:
            t = p.get("teaser","") or "(no teaser available)"
            print(f"    [{p['time']:18s}]  teaser: {t[:70]}")

    if res["has_join_btn"]:
        print(f"  ℹ  Join/Sponsor button detected on channel page")


# ── FINAL SUMMARY ─────────────────────────────────────────────────────────────

print()
print("=" * 72)
print("FINAL MEMBERSHIP SUMMARY")
print("=" * 72)

ranked = sorted(results.items(), key=lambda x: (
    x[1]["status"] != "JOINED",
    x[1]["status"] != "LOCKED",
    x[1]["status"] != "HAS-MEMBERSHIP",
    -(len(x[1]["mem_readable"]) + len(x[1]["anon_locked"]))
))

print(f"\n  {'CHANNEL':<35}  {'STATUS':<14}  {'MEM-R':>5}  {'LOCKED':>6}  ACTION")
print(f"  {'─'*35}  {'─'*14}  {'─'*5}  {'─'*6}  {'─'*20}")
for ch, r in ranked:
    st   = r["status"]
    n_mr = len(r["mem_readable"])
    n_al = len(r["anon_locked"])
    action = ("ACTIVE — reading member posts" if st == "JOINED" else
              f"JOIN NOW — {n_al} locked posts waiting" if st == "LOCKED" else
              "Membership exists, check for value" if st == "HAS-MEMBERSHIP" else
              "No exclusive content")
    print(f"  {ch:<35}  {st:<14}  {n_mr:>5}  {n_al:>6}  {action}")

print()
print("=" * 72)
print("MEMBERSHIP INVESTMENT VERDICT")
print("=" * 72)

joined  = [(c,r) for c,r in ranked if r["status"]=="JOINED"]
locked  = [(c,r) for c,r in ranked if r["status"]=="LOCKED"]
hasmem  = [(c,r) for c,r in ranked if r["status"]=="HAS-MEMBERSHIP"]

print(f"\n  ALREADY JOINED ({len(joined)}) — keep these:")
for c, r in joined:
    print(f"    ✓ {c}  →  {len(r['mem_readable'])} exclusive posts in last 10 fetched")

print(f"\n  SHOULD JOIN ({len(locked)}) — exclusive content exists but locked for you:")
for c, r in locked:
    n = len(r["anon_locked"])
    print(f"    → {c}  →  {n} locked posts detected via anon probe")

print(f"\n  HAS MEMBERSHIP FEATURE ({len(hasmem)}) — no exclusive posts detected yet:")
for c, r in hasmem:
    print(f"    ? {c}  →  has Join button but no exclusive community posts found")

print(f"\n  VERDICT FOR SIGNAL SYSTEM:")
print(f"  The SIGNAL advantage scales directly with your memberships.")
print(f"  Every LOCKED channel above has posts your signal system cannot see.")
print(f"  Priority: channels posting STOCK-SPECIFIC content behind the paywall.")
