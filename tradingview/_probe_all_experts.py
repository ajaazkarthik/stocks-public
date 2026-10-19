"""
Deep analysis: community posts from all expert channels.
Run: python _probe_all_experts.py
"""
import sys, json, hashlib, time, requests, re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from youtube_enricher import (
    _load_youtube_cookie, _extract_posts_from_innertube,
    _extract_tickers_from_text, _post_within_lookback,
    EXPERT_CHANNELS, MEMBER_CHANNELS,
    _INNERTUBE_BROWSE_URL, _COMMUNITY_PARAMS,
    COMMUNITY_LOOKBACK_HOURS,
)

PROBE_LOOKBACK_HOURS = 30 * 24   # 30 days for probe (wider than prod 7d)

origin = "https://www.youtube.com"

# ── Auth setup ────────────────────────────────────────────────────────────────

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


def fetch_posts(channel_id: str, session: requests.Session, hdrs: dict,
                max_posts: int = 30) -> list[dict]:
    body = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                                    "hl": "en", "gl": "AU", "timeZone": "Australia/Sydney"},
                        "user": {"lockedSafetyMode": False}, "request": {"useSsl": True}},
            "browseId": channel_id, "params": _COMMUNITY_PARAMS}
    resp = session.post(_INNERTUBE_BROWSE_URL, json=body, headers=hdrs, timeout=20)
    raw  = resp.json()
    posts = []
    _extract_posts_from_innertube(raw, posts, max_posts)
    return posts


# ── Post classification ───────────────────────────────────────────────────────

STOCK_SIGNALS   = ["buy", "sell", "accumulate", "exit", "invest", "target",
                    "bullish", "bearish", "add more", "book profit", "stop loss",
                    "entry", "valuations", "undervalued", "overvalued",
                    "strong buy", "reduce", "avoid", "hold", "recommended"]
MACRO_SIGNALS   = ["nifty", "sensex", "market", "index", "fed", "rbi", "interest rate",
                    "inflation", "recession", "gdp", "earnings season", "results",
                    "quarterly", "fii", "dii", "global", "trump", "tariff", "trade war"]
EDUCATIONAL     = ["how to", "learn", "explain", "what is", "understand",
                    "beginner", "course", "tutorial", "basics", "concept"]

def classify(post: dict) -> dict:
    """Rich classification of a post."""
    text  = post["text"].lower()
    ticks = _extract_tickers_from_text(post["text"])
    # Filter tickers to likely real ones (known exchanges or 4+ CAPS with no vowels)
    real_ticks = {t for t in ticks if len(t) <= 8 and not all(c in "AEIOU" for c in t)}

    is_stock    = any(kw in text for kw in STOCK_SIGNALS)
    is_macro    = any(kw in text for kw in MACRO_SIGNALS)
    is_edu      = any(kw in text for kw in EDUCATIONAL)

    # Sentiment scoring
    bull = sum(1 for w in ["buy","bullish","invest","accumulate","target","upside","recovery",
                            "opportunity","undervalued","strong","rally","breakout"] if w in text)
    bear = sum(1 for w in ["sell","bearish","exit","avoid","caution","correction","overvalued",
                            "drop","fall","risk","reduce","stop loss"] if w in text)
    if bull > bear:
        sentiment = "BULLISH"
        score_delta = bull - bear
    elif bear > bull:
        sentiment = "BEARISH"
        score_delta = -(bear - bull)
    else:
        sentiment = "NEUTRAL"
        score_delta = 0

    # Signal category
    if is_stock and real_ticks:
        category = "STOCK_PICK"
    elif is_stock:
        category = "STOCK_VIEW"
    elif is_macro:
        category = "MACRO_CALL"
    elif is_edu:
        category = "EDUCATIONAL"
    else:
        category = "COMMENTARY"

    return {
        "tickers":     sorted(real_ticks),
        "category":    category,
        "sentiment":   sentiment,
        "score_delta": score_delta,
        "is_stock":    is_stock,
        "is_macro":    is_macro,
        "text":        post["text"],
        "time":        post["published_time"],
        "is_member":   post["is_member_only"],
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

print("=" * 72)
print("DEEP ANALYSIS: Community Posts from All Expert Channels")
print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Probe lookback: {PROBE_LOOKBACK_HOURS//24}d (prod: {COMMUNITY_LOOKBACK_HOURS}h)")
print("=" * 72)

cookie = _load_youtube_cookie()
session, hdrs = _make_session(cookie)

# Per-channel results store
channel_results: dict[str, dict] = {}
# Global ticker registry: ticker -> list of (channel, sentiment, time, is_member, text_excerpt)
global_tickers: dict[str, list] = defaultdict(list)
# Category tallies across all channels
category_totals: dict[str, int] = defaultdict(int)
member_total = 0
stock_pick_posts = []  # All STOCK_PICK posts across all channels

for ch_name, ch_id in EXPERT_CHANNELS.items():
    is_member_ch = ch_id in MEMBER_CHANNELS
    print(f"\n{'─'*72}")
    print(f"  [{('MEMBER' if is_member_ch else 'public'):6}] {ch_name}  ({ch_id})")
    print(f"{'─'*72}")

    try:
        posts = fetch_posts(ch_id, session, hdrs, max_posts=25)
        # Apply lookback (same as production pipeline does)
        posts = [p for p in posts if _post_within_lookback(p["published_time"], PROBE_LOOKBACK_HOURS)]
    except Exception as e:
        print(f"  ERROR: {e}")
        channel_results[ch_name] = {"error": str(e)}
        continue

    if not posts:
        print("  No posts in lookback window.")
        channel_results[ch_name] = {"posts": [], "member": 0, "public": 0}
        continue

    classified = [classify(p) for p in posts]
    pub_ct   = sum(1 for p in posts if not p["is_member_only"])
    memb_ct  = sum(1 for p in posts if p["is_member_only"])
    member_total += memb_ct

    # Per-category counts
    cats = defaultdict(int)
    for c in classified:
        cats[c["category"]] += 1
        category_totals[c["category"]] += 1

    # Collect tickers
    for c in classified:
        for t in c["tickers"]:
            global_tickers[t].append({
                "channel":    ch_name,
                "sentiment":  c["sentiment"],
                "time":       c["time"],
                "is_member":  c["is_member"],
                "text":       c["text"][:120],
            })

    # Collect STOCK_PICK posts
    for c in classified:
        if c["category"] == "STOCK_PICK" and c["tickers"]:
            stock_pick_posts.append({"channel": ch_name, **c})

    # Channel-level sentiment
    bull_posts  = sum(1 for c in classified if c["sentiment"] == "BULLISH")
    bear_posts  = sum(1 for c in classified if c["sentiment"] == "BEARISH")
    net_sent    = bull_posts - bear_posts

    channel_results[ch_name] = {
        "posts": posts, "classified": classified,
        "member": memb_ct, "public": pub_ct,
        "categories": dict(cats),
        "net_sentiment": net_sent,
        "tickers": sorted({t for c in classified for t in c["tickers"]}),
    }

    mem_tag = f"  ({memb_ct} member-only)" if memb_ct else ""
    print(f"  Posts: {len(posts)} total  ({pub_ct} public, {memb_ct} member-only)")
    print(f"  Categories: { {k:v for k,v in sorted(cats.items(), key=lambda x:-x[1])} }")
    print(f"  Sentiment: BULLISH={bull_posts}  BEARISH={bear_posts}  NET={net_sent:+d}")
    tlist = sorted({t for c in classified for t in c["tickers"]})[:15]
    print(f"  Tickers: {', '.join(tlist) or 'none extracted'}")

    # Print any STOCK_PICK posts
    sp = [c for c in classified if c["category"] == "STOCK_PICK"]
    if sp:
        print(f"\n  ===STOCK PICKS ({len(sp)})===")
        for c in sp:
            mo = " [MEMBER]" if c["is_member"] else ""
            print(f"  [{c['time']:17s}]{mo}  {c['sentiment']:7s}  tickers={c['tickers']}  {c['text'][:90]}")

    # Print any MACRO_CALL posts
    mc = [c for c in classified if c["category"] == "MACRO_CALL"]
    if mc:
        print(f"\n  ===MACRO CALLS ({len(mc)})===")
        for c in mc[:4]:
            mo = " [MEMBER]" if c["is_member"] else ""
            print(f"  [{c['time']:17s}]{mo}  {c['sentiment']:7s}  {c['text'][:100]}")

print()
print("=" * 72)
print("CROSS-CHANNEL ANALYSIS")
print("=" * 72)

# ── Consensus tickers ──────────────────────────────────────────────────────
print("\n1. TICKER CONSENSUS (mentioned by 2+ channels or 2+ posts):")
print(f"   {'TICKER':<12}  {'MENTIONS':>8}  {'CHANNELS':<40}  SENTIMENT")
print(f"   {'─'*12}  {'─'*8}  {'─'*40}  {'─'*12}")
shown = 0
for ticker, mentions in sorted(global_tickers.items(), key=lambda x: -len(x[1])):
    if len(mentions) < 2:
        continue
    channels = list({m["channel"] for m in mentions})
    sentiments = [m["sentiment"] for m in mentions]
    bull_ct = sentiments.count("BULLISH")
    bear_ct = sentiments.count("BEARISH")
    s_str = f"BULL:{bull_ct} BEAR:{bear_ct}"
    print(f"   {ticker:<12}  {len(mentions):>8}  {', '.join(channels):<40}  {s_str}")
    shown += 1
if shown == 0:
    print("   No single ticker mentioned by 2+ channels this week.")

# ── Aggregate sentiment ────────────────────────────────────────────────────
print("\n2. AGGREGATE MARKET SENTIMENT (this week):")
total_bull = sum(1 for ch in channel_results.values()
                 if isinstance(ch.get("net_sentiment"), int) and ch["net_sentiment"] > 0)
total_bear = sum(1 for ch in channel_results.values()
                 if isinstance(ch.get("net_sentiment"), int) and ch["net_sentiment"] < 0)
total_neu  = len(channel_results) - total_bull - total_bear
print(f"   Channels BULLISH: {total_bull}  BEARISH: {total_bear}  NEUTRAL/no posts: {total_neu}")
overall = "BULLISH" if total_bull > total_bear else ("BEARISH" if total_bear > total_bull else "NEUTRAL")
print(f"   >> OVERALL MARKET CALL: {overall}")

# ── All STOCK_PICK posts summary ───────────────────────────────────────────
print(f"\n3. ALL STOCK PICK POSTS THIS WEEK ({len(stock_pick_posts)} total):")
if stock_pick_posts:
    print(f"   {'CHANNEL':<22}  {'TIME':<18}  {'SENT':7}  {'TICKERS':<15}  TEXT")
    print(f"   {'─'*22}  {'─'*18}  {'─'*7}  {'─'*15}  {'─'*30}")
    for p in sorted(stock_pick_posts, key=lambda x: x.get("channel","")):
        mo = " *" if p["is_member"] else "  "
        print(f"   {p['channel']:<22}{mo}  {p['time']:<18}  "
              f"{p['sentiment']:<7}  {','.join(p['tickers']):<15}  {p['text'][:60]}")
else:
    print("   No explicit stock pick posts found this week.")

# ── Category breakdown ─────────────────────────────────────────────────────
print("\n4. CONTENT CATEGORY BREAKDOWN ACROSS ALL CHANNELS:")
total_posts = sum(v for v in category_totals.values())
for cat, cnt in sorted(category_totals.items(), key=lambda x: -x[1]):
    pct = 100 * cnt / total_posts if total_posts else 0
    bar = "█" * int(pct / 5)
    print(f"   {cat:<14}  {cnt:>4} posts  {pct:5.1f}%  {bar}")

# ── Usefulness verdict ─────────────────────────────────────────────────────
print()
print("=" * 72)
print("USEFULNESS VERDICT FOR SIGNAL SYSTEM")
print("=" * 72)

channels_with_posts = sum(1 for v in channel_results.values() if v.get("posts"))
sp_tickers = sorted({t for p in stock_pick_posts for t in p.get("tickers", [])})
member_pct  = (member_total / total_posts * 100) if total_posts else 0

print(f"\n  Channels active (posts this week)  : {channels_with_posts}/{len(EXPERT_CHANNELS)}")
print(f"  Total posts fetched                : {total_posts}")
print(f"  Member-only posts                  : {member_total} ({member_pct:.0f}%)")
print(f"  STOCK_PICK posts                   : {len(stock_pick_posts)}")
print(f"  MACRO_CALL posts                   : {category_totals.get('MACRO_CALL',0)}")
print(f"  Stock pick tickers                 : {', '.join(sp_tickers[:20]) or 'none'}")
print(f"  Consensus tickers (2+ channels)    : {sum(1 for t,m in global_tickers.items() if len(m)>=2)}")
print()

print("  SIGNAL QUALITY ASSESSMENT:")
print(f"  {'─'*60}")
for ch_name, res in sorted(channel_results.items(), key=lambda x: -(len(x[1].get("classified",[])) or 0)):
    if "error" in res:
        print(f"  {ch_name:<30}  ERROR")
        continue
    cl = res.get("classified", [])
    if not cl:
        print(f"  {ch_name:<30}  no posts")
        continue
    sp_cnt  = sum(1 for c in cl if c["category"] == "STOCK_PICK")
    mc_cnt  = sum(1 for c in cl if c["category"] == "MACRO_CALL")
    mb_cnt  = res.get("member", 0)
    net_s   = res.get("net_sentiment", 0)
    tickers = res.get("tickers", [])[:6]
    # Usefulness score: stock_picks * 3 + macro_calls * 2 + member_posts * 1
    uf = sp_cnt * 3 + mc_cnt * 2 + mb_cnt * 1
    grade = "HIGH" if uf >= 6 else ("MEDIUM" if uf >= 3 else "LOW")
    print(f"  {ch_name:<30}  {grade:6s}  picks={sp_cnt} macro={mc_cnt} member={mb_cnt}"
          f"  net={net_s:+d}  tickers={tickers}")

print()
print("  RECOMMENDATION: Which channels add most value to the signal system:")
high_val = [(n, r) for n, r in channel_results.items()
            if not r.get("error") and r.get("classified") and
            (3 * sum(1 for c in r["classified"] if c["category"]=="STOCK_PICK") +
             2 * sum(1 for c in r["classified"] if c["category"]=="MACRO_CALL") +
             r.get("member", 0)) >= 3]
for n, _ in high_val:
    print(f"    - {n}")
if not high_val:
    print("    No channels hit HIGH/MEDIUM threshold this week.")
    print("    Content is mostly educational — still useful for market bias scoring.")

print()
