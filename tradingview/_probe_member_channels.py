"""
Deep probe: Which expert channels have YouTube MEMBERSHIP and what member-only
posts are they posting? Detects BOTH:
  - Channels where we ARE a member (full text readable)
  - Channels where we are NOT a member (locked posts detected in raw JSON)

Run: python _probe_member_channels.py
"""
import sys, json, hashlib, time, re, requests
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from youtube_enricher import (
    _load_youtube_cookie, _INNERTUBE_BROWSE_URL, _COMMUNITY_PARAMS,
    EXPERT_CHANNELS, MEMBER_CHANNELS,
)

origin = "https://www.youtube.com"

# ── Auth session ──────────────────────────────────────────────────────────────

def _make_session(cookie_str: str):
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
        "Referer":                  "https://www.youtube.com/",
        "Content-Type":             "application/json",
        "User-Agent":               ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language":          "en-AU,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
        "X-Goog-AuthUser":          "0",
    }
    return session, hdrs


# ── Raw JSON walkers ──────────────────────────────────────────────────────────

def _find_all(obj, key: str, results=None):
    """Recursively find ALL occurrences of key in nested JSON."""
    if results is None:
        results = []
    if isinstance(obj, dict):
        if key in obj:
            results.append(obj[key])
        for v in obj.values():
            _find_all(v, key, results)
    elif isinstance(obj, list):
        for item in obj:
            _find_all(item, key, results)
    return results


def _find_key_exists(obj, key: str) -> bool:
    """Return True if key exists anywhere in nested JSON."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key_exists(v, key) for v in obj.values())
    elif isinstance(obj, list):
        return any(_find_key_exists(item, key) for item in obj)
    return False


def _extract_all_posts_raw(obj, results=None):
    """
    Extract ALL backstagePostRenderer objects — including locked/empty ones.
    Returns list of raw post dicts (may have empty contentText).
    """
    if results is None:
        results = []
    if isinstance(obj, dict):
        if "backstagePostThreadRenderer" in obj:
            inner = obj["backstagePostThreadRenderer"].get("post", {})
            _extract_all_posts_raw(inner, results)
            return results
        if "backstagePostRenderer" in obj or "postRenderer" in obj:
            post = obj.get("backstagePostRenderer") or obj["postRenderer"]
            results.append(post)
            return results
        for v in obj.values():
            _extract_all_posts_raw(v, results)
    elif isinstance(obj, list):
        for item in obj:
            _extract_all_posts_raw(item, results)
    return results


def _get_post_text(post: dict) -> str:
    runs = post.get("contentText", {}).get("runs", [])
    return "".join(r.get("text", "") for r in runs).strip()


def _get_published_time(post: dict) -> str:
    pt = post.get("publishedTimeText", {})
    if isinstance(pt, dict):
        runs = pt.get("runs", [])
        if runs:
            return runs[0].get("text", "")
        return pt.get("simpleText", "")
    return ""


def fetch_raw(channel_id: str, session: requests.Session, hdrs: dict) -> dict:
    body = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                                    "hl": "en", "gl": "AU", "timeZone": "Australia/Sydney"},
                        "user": {"lockedSafetyMode": False}, "request": {"useSsl": True}},
            "browseId": channel_id, "params": _COMMUNITY_PARAMS}
    resp = session.post(_INNERTUBE_BROWSE_URL, json=body, headers=hdrs, timeout=20)
    return resp.json()


# ── Channel membership analysis ───────────────────────────────────────────────

def analyse_channel(ch_name: str, ch_id: str, session: requests.Session, hdrs: dict) -> dict:
    """
    Full membership analysis for one channel.
    Returns a dict with all membership-related findings.
    """
    try:
        raw = fetch_raw(ch_id, session, hdrs)
    except Exception as e:
        return {"error": str(e)}

    # ── Extract ALL post objects (including locked/invisible ones) ────────────
    all_posts_raw = _extract_all_posts_raw(raw)

    # ── Check channel-level membership indicators ─────────────────────────────
    # 1. joinButton / sponsorButton in channel page
    has_membership_button = (
        _find_key_exists(raw, "sponsorButton") or
        _find_key_exists(raw, "joinButton") or
        _find_key_exists(raw, "subscriberCountWithSubscribeButton")
    )

    # 2. Membership shelf / badge perks
    has_member_perks   = _find_key_exists(raw, "membershipOfferRenderer")
    has_sponsor_promo  = _find_key_exists(raw, "sponsorPromoRenderer")
    has_badges         = _find_key_exists(raw, "badges") and _find_key_exists(raw, "sponsorsOnlyBadge")

    # ── Classify each post ────────────────────────────────────────────────────
    public_readable    = []   # Public post, has text
    member_readable    = []   # Member-only post, we can read (we're a member!)
    member_locked      = []   # Member-only post, LOCKED (we're NOT a member)
    attachment_only    = []   # Post with no text, just image/poll/video

    for post in all_posts_raw:
        text        = _get_post_text(post)
        published   = _get_published_time(post)
        is_member   = "sponsorsOnlyBadge" in post
        has_lock    = _find_key_exists(post, "lockedPostRenderer") or \
                      _find_key_exists(post, "membersOnlyPostRenderer") or \
                      "paidContentOverlayRenderer" in str(post)[:500]
        has_content_lock = not text and is_member   # member badge but no text

        rec = {"published": published, "text": text[:120], "full_text": text, "raw_keys": list(post.keys())[:8]}

        if is_member and text:
            member_readable.append(rec)
        elif is_member and not text:
            # Locked — check for any hint of what's inside
            # Check for locked/teaser patterns
            teaser = ""
            locked_data = _find_all(post, "lockedPostRenderer")
            if locked_data:
                for ld in locked_data:
                    teaser_runs = ld.get("teaserText", {}).get("runs", [])
                    teaser = "".join(r.get("text","") for r in teaser_runs)
            rec["teaser"] = teaser
            rec["raw_keys"] = list(post.keys())[:12]
            member_locked.append(rec)
        elif not is_member and text:
            public_readable.append(rec)
        elif text == "" and not is_member:
            attachment_only.append(rec)

    # ── Check membership perks in channel header ──────────────────────────────
    perks = []
    for perk in _find_all(raw, "membershipOfferRenderer"):
        perk_texts = _find_all(perk, "simpleText")
        perks.extend(perk_texts[:3])

    # ── Extract channel's own mention of membership/perks ────────────────────
    # Look in raw text strings for membership hints
    raw_str = json.dumps(raw)
    member_keywords = re.findall(
        r'"((?:Member|Sponsor|Exclusive|Private|Early access)[^"]{0,80})"',
        raw_str, re.IGNORECASE
    )
    member_hints = sorted(set(member_keywords))[:6]

    return {
        "total_posts_found":    len(all_posts_raw),
        "public_readable":      public_readable,
        "member_readable":      member_readable,
        "member_locked":        member_locked,
        "attachment_only":      attachment_only,
        "has_membership_button":has_membership_button,
        "has_member_perks":     has_member_perks,
        "has_sponsor_promo":    has_sponsor_promo,
        "has_badges":           has_badges,
        "perks":                perks,
        "member_hints":         member_hints,
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

WIDE_LOOKBACK = 90 * 24  # 90 days — look deep into history to find member posts even if rare

print("=" * 72)
print("MEMBERSHIP ANALYSIS: Who posts member-only content?")
print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("=" * 72)
print()
print("Legend:")
print("  [JOINED]  = You are a member — can read their private posts")
print("  [LOCKED]  = Channel HAS member posts but you're NOT a member")
print("  [NONE]    = Channel has NO member-only posts / no membership")
print()

cookie = _load_youtube_cookie()
session, hdrs = _make_session(cookie)

summary_rows = []

for ch_name, ch_id in EXPERT_CHANNELS.items():
    we_are_member = ch_id in MEMBER_CHANNELS
    print(f"{'─'*72}")
    print(f"  {ch_name}  (id={ch_id})")

    result = analyse_channel(ch_name, ch_id, session, hdrs)

    if "error" in result:
        print(f"  ERROR: {result['error']}")
        summary_rows.append((ch_name, "ERROR", 0, 0, 0, ""))
        continue

    n_pub   = len(result["public_readable"])
    n_m_r   = len(result["member_readable"])    # readable = we ARE a member
    n_m_l   = len(result["member_locked"])      # locked   = we are NOT a member
    n_total = result["total_posts_found"]

    # Determine status
    if n_m_r > 0:
        status = "JOINED"
    elif n_m_l > 0:
        status = "LOCKED"
    elif result["has_badges"] or result["has_sponsor_promo"] or result["has_member_perks"]:
        status = "HAS-MEMBERSHIP"    # membership feature enabled, but no posts seen
    else:
        status = "NONE"

    tag = ("YOU ARE MEMBER ✓" if status == "JOINED" else
           "LOCKED — join to unlock !" if status == "LOCKED" else
           "has membership (no posts visible)" if status == "HAS-MEMBERSHIP" else
           "no member content")

    print(f"  Status   : [{status}]  {tag}")
    print(f"  Posts    : {n_total} total | {n_pub} public | {n_m_r} member-readable | {n_m_l} member-locked")

    if result["member_hints"]:
        print(f"  Hints    : {result['member_hints'][:3]}")
    if result["perks"]:
        print(f"  Perks    : {result['perks'][:3]}")

    # Show readable member posts
    if result["member_readable"]:
        print(f"\n  ── MEMBER POSTS YOU CAN READ ({n_m_r}) ──")
        for p in result["member_readable"][:5]:
            print(f"  [{p['published']:18s}]  {p['text'][:85]}")

    # Show locked posts (prove they exist — you should join!)
    if result["member_locked"]:
        print(f"\n  ── LOCKED MEMBER POSTS ({n_m_l}) — you can't read these without joining ──")
        for p in result["member_locked"][:5]:
            teaser = p.get('teaser', '')
            keys   = p.get('raw_keys', [])
            print(f"  [{p['published']:18s}]  keys={keys[:5]}")
            if teaser:
                print(f"  teaser: {teaser[:80]}")

    # Show public posts
    if result["public_readable"]:
        print(f"\n  ── PUBLIC POSTS ({n_pub}) (sample) ──")
        for p in result["public_readable"][:3]:
            print(f"  [{p['published']:18s}]  {p['text'][:85]}")

    summary_rows.append((ch_name, status, n_pub, n_m_r, n_m_l, "; ".join(result["member_hints"][:2])))
    print()


print()
print("=" * 72)
print("SUMMARY TABLE")
print("=" * 72)
print(f"  {'CHANNEL':<30}  {'STATUS':<16}  {'PUB':>4}  {'MEM-R':>5}  {'MEM-L':>5}  NOTES")
print(f"  {'─'*30}  {'─'*16}  {'─'*4}  {'─'*5}  {'─'*5}  {'─'*20}")
for ch, st, pub, mr, ml, notes in sorted(summary_rows, key=lambda x: (x[1]!="JOINED", x[1]!="LOCKED", -x[3]-x[4])):
    flag = "  ← JOIN!"  if st == "LOCKED" else ("  ← ACTIVE" if st == "JOINED" else "")
    print(f"  {ch:<30}  {st:<16}  {pub:>4}  {mr:>5}  {ml:>5}  {notes[:25]}{flag}")

print()
print("ACTIONABLE:")
joined_ch  = [(c, s, mr) for c, s, pub, mr, ml, n in summary_rows if s == "JOINED"]
locked_ch  = [(c, s, ml) for c, s, pub, mr, ml, n in summary_rows if s == "LOCKED"]
has_mem_ch = [(c, s, 0)  for c, s, pub, mr, ml, n in summary_rows if s == "HAS-MEMBERSHIP"]

print(f"\n  CHANNELS YOU'RE ALREADY A MEMBER OF ({len(joined_ch)}):")
for c, s, ct in joined_ch:
    print(f"    ✓ {c}  ({ct} member posts readable)")

print(f"\n  CHANNELS WITH LOCKED MEMBER POSTS — CONSIDER JOINING ({len(locked_ch)}):")
for c, s, ct in locked_ch:
    print(f"    → {c}  ({ct} visible locked posts — exclusive content you're missing)")

print(f"\n  CHANNELS WITH MEMBERSHIP ENABLED (no posts detected) ({len(has_mem_ch)}):")
for c, s, ct in has_mem_ch:
    print(f"    ? {c}")

print(f"\n  CHANNELS WITH NO MEMBER-ONLY CONTENT ({sum(1 for _,s,*_ in summary_rows if s=='NONE')}):")
for c, s, *_ in summary_rows:
    if s == "NONE":
        print(f"    - {c}")
