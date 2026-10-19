"""
YouTube Expert Enricher — L7a signal layer for signal_stack.py
==============================================================
Fetches recent videos from expert YouTube channels (Akshat Shrivastava by default),
extracts stock ticker mentions from titles, descriptions and transcripts,
and produces a per-symbol score (0–10) representing expert endorsement strength.

Score interpretation:
  0     – symbol not mentioned
  3–5   – mentioned in 1 video, neutral/unclear sentiment
  6–8   – mentioned positively in 1 video, or mentioned in 2+ videos
  8–10  – strong positive endorsement across multiple mentions

Requirements:
    pip install google-api-python-client youtube-transcript-api vaderSentiment

Usage (standalone probe):
    python youtube_enricher.py --symbols NVDA AAPL NSE:RELIANCE

Wire-in (external_signal_enricher.py):
    from youtube_enricher import get_youtube_mentions, score_youtube
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.environ.get(
    "YOUTUBE_API_KEY",
    ""
)

# Expert channels to monitor.  Key = human name, value = YouTube channel ID.
# Channel IDs can be obtained from: https://www.youtube.com/@channelname/about
# or via: https://commentpicker.com/youtube-channel-id.php
EXPERT_CHANNELS: dict[str, str] = {
    # ── TIER 1: Member-only community posts CONFIRMED (user is subscribed) ───
    # These channels post stock views + market calls EXCLUSIVELY to paying members.
    # Akshat is the only channel (out of 23 tested) actively doing this as of April 2026.
    "Akshat Shrivastava": "UCqW8jxh4tH1Z1sWPbkGWL4g",   # @AkshatShrivastavaOfficial

    # ── TIER 2: High-signal public community posts + videos ──────────────────
    # No member-only community posts, but frequent market/stock commentary.
    "SOIC":               "UCB7GnQlJPIL6rBBqEoX87vA",   # @SOICfinance — deep value/compounders
    "Parimal Ade":        "UCUWbS9qoyLKTdOlvgK1GBlA",   # @ParimalAde — mid/small-cap focus
    "Shankar Nath":       "UCtnItzU7q_bA1eoEBjqcVrw",   # @shankarnath — data-driven
    "Vivek Bajaj":        "UCmkNoDHBfKzPnXWTNoYQc7A",   # @stockedge — daily market updates
    "Shyam Sekhar":       "UChvpJLpR41w08f-LE6fEcEA",   # @iThoughtPMS — PMS quality focus

    # ── TIER 3: Has YouTube Join button — no exclusive community posts yet ───
    # Rachana Ranade + Trade With Trend have memberships enabled but use them
    # only for badge/emoji perks, NOT for member-only community post content.
    # Worth monitoring — they may start posting exclusive content.
    "Rachana Ranade":     "UCe3qdG0A_gr-sEdat5y2twQ",   # @CArachanaranade  [Join btn, no excl posts]
    "Trade With Trend":   "UCPgT-N-DQ0K0H88skjaDgkA",   # @TradeWithTrend   [Join btn, no excl posts]

    # ── TIER 4: Public-only educators / macro commentators ───────────────────
    "Pranjal Kamra":      "UCwAdQUuPT6laN-AQR17fe1g",   # @PranjalKamra
    "Nikhil Kamath":      "UCUZY7-p1bUidPEEYD9sF8EA",   # @NikhilKamathCIO
    "ET Money":           "UCU21CybXH91lv7ay7ABezLw",   # @ETMoneyIndia
    "Warikoo":            "UCRzYN32xtBf3Yxsx5BvJWJw",   # @warikoo
    "Groww":              "UCw5TLrz3qADabwezTEcOmgQ",   # @groww
}

# Channels where the user holds a PAID MEMBERSHIP (channel IDs, not names).
#
# RESEARCH FINDINGS (April 2026, 23 channels probed via InnerTube API):
#   → Akshat Shrivastava is the ONLY top Indian stock expert who actively posts
#     member-only community posts (9/10 recent posts are member-only).
#   → Rachana Ranade + Trade With Trend have YouTube Join buttons but use them
#     only for badge/emoji perks — zero member-only community posts detected.
#   → All other channels (SOIC, Parimal Ade, Shankar Nath, Capitalmind, Porinju,
#     Dr Vijay Malik, Neeraj Marathe, Safal Niveshak, PMS Bazaar, etc.) have no
#     membership feature or don't post exclusive community content at all.
#
# WHERE OTHER EXPERTS HOST EXCLUSIVE CONTENT (not captured here):
#   • Capitalmind Premium     → capitalmind.in (paid newsletter/website)
#   • SOIC                    → Telegram channel (free, but private posts)
#   • Parimal Ade             → Telegram
#   • Finology Select         → finology.in (Pranjal Kamra's paid research)
#   • Safal Niveshak Premium  → safalniveshak.com (paid newsletter)
#
# Score multiplier for member-channel mentions: MEMBER_WEIGHT_MULTIPLIER (default 1.5×).
MEMBER_CHANNELS: set[str] = {
    "UCqW8jxh4tH1Z1sWPbkGWL4g",   # Akshat Shrivastava (paid membership — ACTIVE)
}

# Score multiplier applied when at least one mention originates from a member channel
MEMBER_WEIGHT_MULTIPLIER: float = 1.5

# How far back to look for videos (hours)
LOOKBACK_HOURS = 72
# Community posts are published less frequently (~1-2/week) so use a wider window
COMMUNITY_LOOKBACK_HOURS = 168   # 7 days

# Cache TTL — avoid re-fetching within a run session (in-memory only)
_MENTION_CACHE: dict[str, tuple[float, list[dict]]] = {}   # channel_id → (ts, posts)
_CACHE_TTL = 3600   # 1 hour

# Cookie in-memory cache: (last_load_ts, cookie_str)
# Avoids reading the cookie file from disk on every symbol check.
_cookie_cache: tuple[float, str] = (0.0, "")
_COOKIE_RELOAD_INTERVAL = 300   # re-read file at most every 5 min (picks up manual refreshes)

# Expiry check guard: only stat the file once per hour, not once per symbol.
_expiry_checked_at: float = 0.0
_EXPIRY_CHECK_INTERVAL = 3600   # warn at most once per hour

# ─── Community-post (InnerTube) config ─────────────────────────────────────────
# Path to flat cookie file for YouTube authenticated requests.
# To populate: open Chrome DevTools → Network → any youtube.com request →
#   right-click → Copy → Copy as cURL → extract the Cookie header value.
# Save that single long cookie string (no newlines) to this file.
# Without this file community posts are skipped (not an error).
COMMUNITY_COOKIE_FILE: Path = Path(__file__).parent / "youtube-cookie"

# Community tab InnerTube browse params (base64-encoded protobuf for the Community tab)
_COMMUNITY_PARAMS = "EgVwb3N0c_IGBAoCSgA="
_INNERTUBE_BROWSE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"

# ─── Ticker extraction helpers ─────────────────────────────────────────────────
# Matches: $NVDA, $RELIANCE, $NIFTY50, NSE:TITAN, BSE:TATASTEEL,
#          standalone UPPER-ALPHA words of 2-6 chars surrounded by spaces/punct
_CASHTAG_RE  = re.compile(r'\$([A-Z]{2,12})')
_EXCHANGE_RE = re.compile(r'\b(?:NSE|BSE|NYSE|NASDAQ|ASX)[:.]([A-Z0-9]{2,12})\b')
_WORD_TICKER = re.compile(r'\b([A-Z]{2,12})\b')

# Common English words that look like tickers — filtered out to reduce noise.
# Bare ALL-CAPS words of 4+ chars not in this list are treated as potential tickers.
# $CASHTAG and EXCHANGE:TICKER forms bypass this filter entirely.
_STOPWORDS = frozenset({
    # ── Articles / prepositions / conjunctions ──────────────────────────────
    "THE", "AND", "FOR", "WITH", "FROM", "THAT", "THIS", "THAN", "THEN",
    "ALSO", "BOTH", "EVEN", "ONLY", "JUST", "VERY", "MUCH", "MORE", "MOST",
    "SOME", "NONE", "SUCH", "EACH", "OVER", "UPON", "INTO", "ONTO", "LIKE",
    "WHEN", "WHAT", "WHOM", "WHICH", "WHERE", "WHILE", "WHOSE", "WHETHER",
    "SINCE", "UNTIL", "AFTER", "ABOUT", "ABOVE", "BELOW", "ALONG", "AMONG",
    "BETWEEN", "THROUGH", "WITHIN", "WITHOUT", "DURING", "BECAUSE", "BEFORE",
    "AROUND", "AGAINST", "DESPITE", "HOWEVER", "THEREFORE", "ALTHOUGH",
    # ── Pronouns / person ───────────────────────────────────────────────────
    "YOUR", "THEIR", "THEM", "THEY", "HIMSELF", "YOURSELF", "MYSELF", "ITSELF",
    "THOSE", "THESE", "THERE", "HERE", "ELSE", "EACH", "EVERY",
    # ── Common verbs (not tickers) ─────────────────────────────────────────
    "HAVE", "BEEN", "WILL", "DOES", "MAKE", "MADE", "SAID", "SAYS", "COME",
    "CAME", "GOES", "GONE", "KNOW", "KNEW", "FEEL", "FELT", "SEEM", "WANT",
    "KEEP", "KEPT", "GIVE", "GAVE", "TAKE", "TOOK", "NEED", "GETS", "PUTS",
    "CALL", "WRITE", "WROTE", "SHOW", "SEEN", "HEAR", "HOLD", "HELP",
    "SELL", "SELLING", "SOLD", "BUYING", "INVEST", "INVESTING",
    "REALIZE", "UNDERSTAND", "DECLARE", "ANALYZE", "CONSIDER",
    # ── Common adjectives / adverbs ────────────────────────────────────────
    "GOOD", "BEST", "FULL", "HIGH", "LAST", "LONG", "NEXT", "POOR", "REAL",
    "SAME", "TRUE", "WELL", "FAST", "FREE", "HARD", "EASY", "COOL", "FINE",
    "HUGE", "LIKELY", "UNLIKELY", "HONESTLY", "ROUGHLY", "TOTALLY", "REALLY",
    "STRONG", "BIGGER", "ENTIRE", "CURRENT", "RECENT", "DECENT", "COMMON",
    "ACTUAL", "ACTUALLY", "SIMPLE", "CRAZY", "DIFFERENT", "INTERESTING",
    "IMPORTANT", "SIGNIFICANT", "MASSIVE", "LEGITIMATE", "PROACTIVE",
    # ── Common nouns (non-ticker) ───────────────────────────────────────────
    "RATE", "TERM", "FUND", "BANK", "RISK", "DEBT", "CASH", "COST", "IDEA",
    "YEAR", "WEEK", "DAYS", "MONTH", "TIME", "THING", "THINGS", "STUFF",
    "GUYS", "FOLK", "FOLKS", "PEOPLE", "PERSON", "MARKET", "MARKETS",
    "WORLD", "MONEY", "POWER", "LEVEL", "VALUE", "PRICE", "STAGE", "POINT",
    "PART", "CASE", "NOTE", "FACT", "INFO", "LINK", "POST", "STEP", "ROLE",
    "FALL", "RISE", "DROP", "LOSS", "GAIN", "NEWS", "PLAN", "PATH", "VIEW",
    "DEAL", "MOVE", "AREA", "SIDE", "MODE", "TYPE", "FORM", "SORT", "PLAN",
    "TOPIC", "STORY", "TREND", "THEME", "PHASE", "ISSUE", "POINT", "SENSE",
    "ADVICE", "PERIOD", "REASON", "COURSE", "MATTER", "NUMBER", "RESULT",
    "OPTION", "SYSTEM", "MOMENT", "IMPACT", "POLICY", "TARGET", "DETAIL",
    "GROWTH", "WEALTH", "RETURN", "RETURNS", "EARNING", "EARNINGS",
    "BUDGET", "SECTOR", "REGION", "ECONOMY", "ECONOMY", "INDUSTRY",
    "PORTFOLIO", "INVESTMENT", "SITUATION", "GOVERNMENT", "RETIREMENT",
    "OPPORTUNITY", "COMMENTARY", "NARRATIVE", "MECHANISM",
    # ── Finance terms that are NOT tickers ─────────────────────────────────
    "STOCK", "STOCKS", "SHARE", "SHARES", "BOND", "BONDS", "EQUITY",
    "ASSET", "ASSETS", "INDEX", "TRADE", "TRADING", "CAGR", "XIRR",
    "SIP", "SIPS", "NFO", "IPO", "ETF", "FII", "DII", "LTCG", "STCG",
    "PNL", "NAV", "MFD", "AUM", "EMI", "EMIS", "SEBI", "NSE", "BSE",
    "MCX", "CDSL", "NSDL",
    # ── Currency / macro codes ────────────────────────────────────────────
    "INR", "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CNY", "AED",
    "EPF", "PPF", "NPS", "ELSS", "RBI", "FOMC", "OPEC",
    # ── Tech / internet non-tickers ──────────────────────────────────────
    "HTTP", "HTTPS", "HTML", "JSON", "LINK", "YOUTUBE", "TWITTER",
    "GOOGLE", "REDDIT", "OPENAI",
    # ── Single-word misc ──────────────────────────────────────────────────
    "ALSO", "BACK", "STILL", "AGAIN", "AWAY", "EVEN", "MANY", "LESS",
    "OKAY", "SURE", "DONE", "OPEN", "ZERO", "ONCE", "JUST", "ONLY",
    "GUYS", "PLEASE", "THANKS", "BUDDY",
    # ── Location names commonly used in posts ────────────────────────────
    "INDIA", "INDIAN", "DUBAI", "CHINA", "TRUMP", "IRAN", "OPEC",
    "MIDDLE", "EAST", "NORTH", "SOUTH", "WEST",
    # ── Short words (1-3 chars already excluded by length≥4 rule) ─────────
    "BUY", "SELL", "HOLD",   # kept for completeness; also filtered by len<4
    # ── Emphasis words — authors often ALL-CAPS these but they're not tickers ─
    "NOT", "YES", "NON", "BUT", "YET", "HOW", "NOW", "TOO", "OUT",
    "OWN", "NEW", "OLD", "LOW", "OFF", "TOP", "RUN", "CUT", "USE", "GET",
    "ADD", "CAN", "MAY", "DID", "HAS", "HAD", "GOT", "LET", "PUT", "SAY",
    "TRY", "WIN", "ACT", "ANY", "ALL", "KEY", "FEW", "BIG", "BAD",
    "YOU", "WAS", "ARE", "FOR", "THE", "AND", "NET", "NIT",
    # ── Financial abbreviations that aren't stock tickers ─────────────────
    "OTM", "ITM", "ATM", "GCC", "UAE", "NRI", "HNI", "XYZ", "LLP", "PVT",
    "LTD", "INC",
    # ── Financial ratios / metrics (not tickers) ──────────────────────────
    "EPS", "CFO", "FCF", "ROE", "ROI", "ROCE", "PEG", "GARP", "MPC", "ATR",
    "PPT", "PDF", "FIFO", "POV", "AVP",
    # ── Sector / macro terms (not individual stock tickers) ───────────────
    "FMCG", "GDP", "GST", "CPI", "WPI", "PSU", "PSB",
    # ── Time zones commonly used in Indian posts ──────────────────────────
    "IST", "UTC", "GMT",
    # ── Channel names / event names extracted as false tickers ────────────
    "SOIC", "SOICHOLI",
    # ── Promo codes / disease / general words capitalised in posts ────────
    "CARRYT", "COVID", "LIVE", "ONE", "API",
    # ── Long emphasis words (all-capsed for stress) ────────────────────────
    "PROBLEMATIC", "STRUCTURAL", "TECHNICAL", "OPERATIONAL", "LEGITIMATE",
    "INTERESTING", "SIGNIFICANT", "AGGRESSIVE", "REASONABLE",
})

# ─── Indian company name → NSE ticker lookup ──────────────────────────────────
# Akshat Shrivastava and Indian creators write prose like "Thermax is great",
# never "$THERMAX".  This map identifies tickers from mixed-case company names.
# Keys: lowercase name fragments.  Values: NSE ticker symbols.
# Longer keys listed first (sorted at module load) to prevent partial shadowing.
_INDIA_NAMES_TO_TICKER: dict[str, str] = {
    # ── User basket: Super Businesses Fundamental ─────────────────────────────
    "thermax":                "THERMAX",
    "rainbow children's":     "RAINBOW",
    "rainbow children":       "RAINBOW",
    "rainbow medicare":       "RAINBOW",
    "kaynes technology":      "KAYNES",
    "kaynes tech":            "KAYNES",
    "capacite infraprojects": "CAPACITE",
    "capacite":               "CAPACITE",
    # ── User basket: Green & Clean Energy ────────────────────────────────────
    "waaree energies":        "WAAREEENER",
    "waaree":                 "WAAREEENER",
    "triveni turbine":        "TRITURBINE",
    "td power systems":       "TDPOWERSYS",
    "td power":               "TDPOWERSYS",
    "boro renewables":        "BORORENEW",
    "vedanta":                "VEDL",
    "transrail":              "TRANSRAIL",
    "tata power":             "TATAPOWER",
    "skipper":                "SKIPPER",
    "power mech":             "POWERMECH",
    "syrma":                  "SYRMA",
    "avalon technologies":    "AVALON",
    "shivalik bimetal":       "SHIVALIKBIM",
    "sansera":                "SANSERA",
    "ather energy":           "ATHER",
    "ather":                  "ATHER",
    "neogen chemicals":       "NEOGEN",
    # ── Nifty 50 / large caps ─────────────────────────────────────────────────
    "reliance industries":    "RELIANCE",
    "reliance":               "RELIANCE",
    "tata consultancy":       "TCS",
    "hdfc bank":              "HDFCBANK",
    "infosys":                "INFY",
    "icici bank":             "ICICIBANK",
    "hindustan unilever":     "HINDUNILVR",
    "state bank of india":    "SBIN",
    "itc":                    "ITC",
    "sbi":                    "SBIN",
    "bajaj finance":          "BAJFINANCE",
    "bajaj finserv":          "BAJAJFINSV",
    "asian paints":           "ASIANPAINT",
    "maruti suzuki":          "MARUTI",
    "maruti":                 "MARUTI",
    "titan":                  "TITAN",
    "wipro":                  "WIPRO",
    "adani enterprises":      "ADANIENT",
    "adani ports":            "ADANIPORTS",
    "adani green":            "ADANIGREEN",
    "adani power":            "ADANIPOWER",
    "ultratech cement":       "ULTRACEMCO",
    "ultratech":              "ULTRACEMCO",
    "sun pharma":             "SUNPHARMA",
    "cipla":                  "CIPLA",
    "dr. reddy's":            "DRREDDY",
    "dr reddy":               "DRREDDY",
    "ntpc":                   "NTPC",
    "ongc":                   "ONGC",
    "power grid":             "POWERGRID",
    "bharti airtel":          "BHARTIARTL",
    "airtel":                 "BHARTIARTL",
    "kotak mahindra":         "KOTAKBANK",
    "kotak bank":             "KOTAKBANK",
    "axis bank":              "AXISBANK",
    "larsen & toubro":        "LT",
    "larsen and toubro":      "LT",
    "tata motors":            "TATAMOTORS",
    "tata steel":             "TATASTEEL",
    "jio financial":          "JIOFIN",
    "hero motocorp":          "HEROMOTOCO",
    "hindalco":               "HINDALCO",
    "jsw steel":              "JSWSTEEL",
    "grasim":                 "GRASIM",
    "divis laboratories":     "DIVISLAB",
    "divi's laboratories":    "DIVISLAB",
    "nestle india":           "NESTLEIND",
    "nestle":                 "NESTLEIND",
    "mahindra & mahindra":    "M&M",
    "bajaj auto":             "BAJAJ-AUTO",
    # ── Well-known mid/small caps ─────────────────────────────────────────────
    "persistent systems":     "PERSISTENT",
    "persistent":             "PERSISTENT",
    "polycab india":          "POLYCAB",
    "polycab":                "POLYCAB",
    "dixon technologies":     "DIXON",
    "dixon":                  "DIXON",
    "tata elxsi":             "TATAELXSI",
    "pi industries":          "PIIND",
    "five star business":     "FIVESTAR",
    "hdfc amc":               "HDFCAMC",
    "page industries":        "PAGEIND",
    "muthoot finance":        "MUTHOOTFIN",
    "muthoot":                "MUTHOOTFIN",
    "delhivery":              "DELHIVERY",
    "zomato":                 "ZOMATO",
    "irfc":                   "IRFC",
    "ireda":                  "IREDA",
    "info edge":              "NAUKRI",
    "infoedge":               "NAUKRI",
    "indiamart":              "INDIAMART",
    "india mart":             "INDIAMART",
    "coforge":                "COFORGE",
    "mphasis":                "MPHASIS",
    "ltimindtree":            "LTIM",
    "lti mindtree":           "LTIM",
    "deepak nitrite":         "DEEPAKNTR",
    "supreme industries":     "SUPREMEIND",
    "astral":                 "ASTRAL",
    "srf":                    "SRF",
    "vinati organics":        "VINATIORGA",
    "navin fluorine":         "NAVINFLUOR",
    "clean science":          "CLEANSCIEN",
    "ami organics":           "AMIORG",
    "fine organic":           "FINEORG",
    "godrej consumer":        "GODREJCP",
    "godrej properties":      "GODREJPROP",
    "angel one":              "ANGELONE",
}
# Sorted by descending key length — longer / more-specific names match first
_INDIA_NAME_KEYS_SORTED: list[str] = sorted(
    _INDIA_NAMES_TO_TICKER.keys(), key=len, reverse=True
)


def _extract_tickers_from_text(text: str) -> set[str]:
    """Extract candidate ticker symbols from a text block."""
    tickers: set[str] = set()
    if not text:
        return tickers

    text_upper = text.upper()

    # Highest confidence: $CASHTAG and EXCHANGE:TICKER (always reliable)
    for m in _CASHTAG_RE.finditer(text_upper):
        tickers.add(m.group(1))
    for m in _EXCHANGE_RE.finditer(text_upper):
        tickers.add(m.group(1))

    # Medium confidence: words already written in ALL-CAPS in the ORIGINAL text.
    # We scan the original (not uppercased) so that prose words spelled in mixed/lower
    # case are never included — only words the author explicitly capitalised (e.g. MSFT,
    # META, NIFTY, SOFI) qualify.  Min 3 chars to catch AMD/ITC; max 12 for safety.
    for m in _WORD_TICKER.finditer(text):
        w = m.group(1)            # already all-caps because the regex requires [A-Z]+
        if len(w) >= 3 and w not in _STOPWORDS:
            tickers.add(w)

    # Name-based lookup for Indian company names in mixed-case prose.
    # Akshat writes "Thermax is great", not "$THERMAX" — this catches those.
    text_lower = text.lower()
    for name_frag in _INDIA_NAME_KEYS_SORTED:
        if name_frag in text_lower:
            tickers.add(_INDIA_NAMES_TO_TICKER[name_frag])

    return tickers


# ─── Community post helpers (InnerTube + cookie) ─────────────────────────────
def _load_youtube_cookie() -> str:
    """
    Load YouTube auth cookie string from COMMUNITY_COOKIE_FILE.
    In-memory cached for _COOKIE_RELOAD_INTERVAL seconds to avoid N disk reads per run.
    Returns empty string if the file does not exist or is empty.
    """
    global _cookie_cache
    now = time.time()
    if now - _cookie_cache[0] < _COOKIE_RELOAD_INTERVAL:
        return _cookie_cache[1]
    cookie = ""
    try:
        if COMMUNITY_COOKIE_FILE.exists():
            content = COMMUNITY_COOKIE_FILE.read_text(encoding="utf-8").strip()
            if content and not content.startswith("#"):
                cookie = content
    except Exception as exc:
        logger.debug("youtube_enricher: could not load cookie file — %s", exc)
    _cookie_cache = (now, cookie)
    return cookie


_COOKIE_MAX_AGE_DAYS = 14   # Warn if cookie file is older than this


def _check_cookie_expiry() -> None:
    """
    Warn loudly if the YouTube cookie file is stale (> _COOKIE_MAX_AGE_DAYS old).
    Throttled to at most once per _EXPIRY_CHECK_INTERVAL — avoids doing a file stat
    on every symbol processed in a single signal_stack run.
    """
    global _expiry_checked_at
    now = time.time()
    if now - _expiry_checked_at < _EXPIRY_CHECK_INTERVAL:
        return   # already checked recently — skip the disk stat
    _expiry_checked_at = now
    if not COMMUNITY_COOKIE_FILE.exists():
        return
    try:
        import os
        age_days = (now - os.path.getmtime(str(COMMUNITY_COOKIE_FILE))) / 86400
        if age_days > _COOKIE_MAX_AGE_DAYS:
            logger.warning(
                "\u26a0\ufe0f  youtube_enricher: YouTube cookie is %.0f days old (limit %dd). "
                "Akshat's member-only posts may be INACCESSIBLE — exclusive stock calls "
                "could be missed.  Refresh: Chrome DevTools \u2192 Network \u2192 "
                "any youtube.com request \u2192 Copy as cURL \u2192 extract Cookie header \u2192 "
                "save to %s",
                age_days, _COOKIE_MAX_AGE_DAYS, COMMUNITY_COOKIE_FILE,
            )
        else:
            logger.debug(
                "youtube_enricher: cookie file age %.1f days (limit %dd) — OK",
                age_days, _COOKIE_MAX_AGE_DAYS,
            )
    except Exception:
        pass


def _extract_sapisid(cookie_str: str) -> str:
    """Extract SAPISID value from a semicolon-separated cookie string."""
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("SAPISID="):
            return part[len("SAPISID="):]
    return ""


def _post_within_lookback(published_time_text: str, lookback_hours: int) -> bool:
    """
    Parse YouTube relative time strings like "3 hours ago", "2 days ago",
    "1 week ago" and return True if within the lookback window.
    Returns True if the text cannot be parsed (be permissive rather than skip).
    """
    if not published_time_text:
        return True
    t = published_time_text.lower()
    import re as _re
    m = _re.search(r'(\d+)\s+(second|minute|hour|day|week|month|year)', t)
    if not m:
        return True   # can't parse → keep it
    n_val = int(m.group(1))
    unit  = m.group(2)
    unit_hours = {"second": 1/3600, "minute": 1/60, "hour": 1,
                  "day": 24, "week": 168, "month": 720, "year": 8760}
    age_hours = n_val * unit_hours.get(unit, 24)
    return age_hours <= lookback_hours


def _extract_posts_from_innertube(obj: object, posts: list, max_posts: int) -> None:
    """
    Recursively walk an InnerTube JSON response and collect postRenderer dicts.
    Stops once *max_posts* posts have been collected.
    """
    if len(posts) >= max_posts:
        return
    if isinstance(obj, dict):
        # Support both legacy postRenderer and modern backstagePostRenderer
        if "backstagePostThreadRenderer" in obj:
            _extract_posts_from_innertube(obj["backstagePostThreadRenderer"].get("post", {}), posts, max_posts)
            return
        if "postRenderer" in obj or "backstagePostRenderer" in obj:
            post = obj.get("backstagePostRenderer") or obj["postRenderer"]
            text_runs = post.get("contentText", {}).get("runs", [])
            text = "".join(r.get("text", "") for r in text_runs).strip()
            if not text:
                return  # skip attachment-only posts
            # Published time: may be runs list or simpleText
            pt_node = post.get("publishedTimeText", {})
            if isinstance(pt_node, dict):
                pt_runs = pt_node.get("runs", [])
                published = pt_runs[0].get("text", "") if pt_runs else pt_node.get("simpleText", "")
            else:
                published = ""
            is_member_only = "sponsorsOnlyBadge" in post
            posts.append({
                "post_id":        post.get("postId", ""),
                "text":           text,
                "published_time": published,
                "is_member_only": is_member_only,
            })
        else:
            for v in obj.values():
                _extract_posts_from_innertube(v, posts, max_posts)
    elif isinstance(obj, list):
        for item in obj:
            _extract_posts_from_innertube(item, posts, max_posts)


def _fetch_community_posts(
    channel_id: str,
    cookie_str: str,
    max_posts: int = 30,
    lookback_hours: int = COMMUNITY_LOOKBACK_HOURS,
) -> list[dict]:
    """
    Fetch community tab posts (including member-only) via YouTube InnerTube API.

    Requires *cookie_str* to be a valid authenticated YouTube Cookie header value
    for a Google account that is a paid member of the channel.  Without auth the
    API returns only public community posts (still useful, but no member content).

    Returns a list of dicts: {post_id, text, published_time, is_member_only}
    filtered to the *lookback_hours* window.  Returns empty list on any error.
    """
    if not cookie_str:
        return []

    # ── CACHE CHECK ─────────────────────────────────────────────────────────────
    # Community posts are channel-level (same posts regardless of which symbol we
    # are checking right now).  Cache under channel_id so the first symbol triggers
    # ONE HTTP call and all subsequent symbols in the same run reuse the result.
    now_ts = time.time()
    cached = _MENTION_CACHE.get(channel_id)
    if cached and (now_ts - cached[0]) < _CACHE_TTL:
        logger.debug("youtube_enricher: cache HIT for channel %s (%d posts)", channel_id, len(cached[1]))
        return [p for p in cached[1] if _post_within_lookback(p["published_time"], lookback_hours)]
    # ────────────────────────────────────────────────────────────────────────────

    import hashlib
    import urllib.request

    sapisid = _extract_sapisid(cookie_str)
    ts      = int(time.time())
    origin  = "https://www.youtube.com"

    headers: dict[str, str] = {
        "Cookie":                   cookie_str,
        "X-Origin":                 origin,
        "Content-Type":             "application/json",
        "User-Agent":               (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language":          "en-US,en;q=0.9",
        "X-YouTube-Client-Name":    "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
    }
    if sapisid:
        hash_input = f"{ts} {sapisid} {origin}"
        sha1 = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()
        headers["Authorization"] = f"SAPISIDHASH {ts}_{sha1}"

    body = json.dumps({
        "context": {
            "client": {
                "clientName":    "WEB",
                "clientVersion": "2.20240101.00.00",
                "hl":            "en",
                "gl":            "US",
            }
        },
        "browseId": channel_id,
        "params":   _COMMUNITY_PARAMS,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            _INNERTUBE_BROWSE_URL,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except Exception as exc:
        logger.warning(
            "youtube_enricher: InnerTube community posts error for %s — %s", channel_id, exc
        )
        return []

    all_posts: list[dict] = []
    _extract_posts_from_innertube(data, all_posts, max_posts)

    # ── CACHE WRITE ─────────────────────────────────────────────────────────────
    _MENTION_CACHE[channel_id] = (time.time(), all_posts)
    logger.debug("youtube_enricher: fetched and cached %d posts for channel %s", len(all_posts), channel_id)
    # ────────────────────────────────────────────────────────────────────────────

    # Filter to lookback window (permissive: keep posts whose age is ambiguous)
    return [p for p in all_posts if _post_within_lookback(p["published_time"], lookback_hours)]


# ─── VADER sentiment helper ─────────────────────────────────────────────────────
# Single module-level instance — SentimentIntensityAnalyzer loads a ~10k-word
# lexicon from disk on every instantiation.  Re-using one singleton avoids
# repeated lexicon loads when scoring mentions for many stocks in a single run.
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderAnalyzer
    _vader_singleton = _VaderAnalyzer()
    _VADER_OK = True
except ImportError:
    _VADER_OK = False


def _vader_compound(text: str) -> float:
    if not _VADER_OK:
        return 0.0
    return _vader_singleton.polarity_scores(text)["compound"]


def _sentiment_around_ticker(text: str, ticker: str, window: int = 100) -> float:
    """Extract sentences containing *ticker* and compute mean VADER compound score."""
    if not text or not ticker:
        return 0.0
    idx = text.upper().find(ticker.upper())
    if idx == -1:
        return 0.0
    start = max(0, idx - window)
    end   = min(len(text), idx + len(ticker) + window)
    snippet = text[start:end]
    return _vader_compound(snippet)


# ─── Public API ────────────────────────────────────────────────────────────────
def get_youtube_mentions(
    symbol: str,
    channels: dict[str, str] = None,   # kept for API compatibility; ignored
    use_transcript: bool = False,       # transcript search removed; kept for compat
) -> dict:
    """
    Scan Akshat Shrivastava's paid community posts for mentions of *symbol*.

    Only MEMBER_CHANNELS are queried — the 12-channel public video search has
    been removed because:
      • It burned YouTube Data API quota (100 units/call, 10K/day free)
      • Indian creators use company names in prose, not $CASHTAG — the old
        ALL-CAPS extraction missed most NSE mentions
      • Public video content is already covered by L3 Google News sentiment

    The Indian name lookup in _extract_tickers_from_text now catches mixed-case
    company names like "Thermax is great" → THERMAX.

    Cookie expiry is checked loudly so you know when member-only access lapses.

    Returns the same dict shape as before (external_signal_enricher.py unchanged).
    """
    bare = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()

    # Cookie load + expiry guard
    cookie_str = _load_youtube_cookie()
    _check_cookie_expiry()

    all_mentions: list[dict] = []
    community_mention_count = 0

    # Only iterate Akshat's channel (the only one with active member-only posts)
    for ch_name, ch_id in EXPERT_CHANNELS.items():
        if ch_id not in MEMBER_CHANNELS:
            continue   # skip all non-member channels

        posts = _fetch_community_posts(ch_id, cookie_str, lookback_hours=COMMUNITY_LOOKBACK_HOURS)
        if not posts:
            continue

        for p in posts:
            post_tickers = _extract_tickers_from_text(p["text"])
            if bare not in post_tickers:
                continue
            sent = _sentiment_around_ticker(p["text"], bare)
            all_mentions.append({
                "video_id":          p["post_id"],
                "title":             p["text"][:120],
                "channel":           ch_name,
                "published_at":      p["published_time"],
                "sentiment":         round(sent, 4),
                "in_title":          True,   # community post = explicit mention
                "in_transcript":     False,
                "is_member_channel": True,
                "is_community_post": True,
                "is_member_only":    p["is_member_only"],
            })
            community_mention_count += 1

    if not cookie_str:
        logger.info(
            "youtube_enricher: no youtube-cookie — only public community posts "
            "available.  Save cookie to %s to access Akshat's member-only stock calls.",
            COMMUNITY_COOKIE_FILE,
        )
    else:
        logger.debug(
            "youtube_enricher: community posts scanned (%dh window); "
            "%d mention(s) found for %s",
            COMMUNITY_LOOKBACK_HOURS, community_mention_count, bare,
        )

    mean_sent = (
        sum(m["sentiment"] for m in all_mentions) / len(all_mentions)
        if all_mentions else 0.0
    )

    return {
        "mention_count":   len(all_mentions),
        "sentiment":       round(mean_sent, 4),
        "videos":          all_mentions,
        "symbol_checked":  bare,
        "from_cache":      False,
        "community_posts": community_mention_count,
    }


def score_youtube(mention_data: dict) -> float:
    """
    Convert YouTube mention data into an L7a sub-score (0–10).

    Scoring rationale:
      - No mentions              → 0.0  (no signal)
      - 1 mention, neutral       → 3.0
      - 1 mention, positive      → 5.0
      - 1 mention, very positive → 6.5
      - 2+ mentions, positive    → 7.5
      - Title mention + positive → 8.5 (explicit endorsement)
      - Multiple title mentions  → 10.0

    Member channel multiplier:
      When at least one mention originates from a paid-member channel
      (channel ID in MEMBER_CHANNELS), the raw base score is multiplied by
      MEMBER_WEIGHT_MULTIPLIER (default 1.5×) before adding the sentiment
      modifier.  This models the higher trust placed on member content vs
      generic public videos.  Result is still clamped to [0, 10].

    Community posts:
      Each community post mention is treated as an explicit title-level mention
      (in_title=True is set by get_youtube_mentions).  Member-only community
      posts add an extra +1.0 trust bonus on top of the member multiplier,
      reflecting the highest-confidence signal (paid exclusive content).
    """
    n    = mention_data.get("mention_count", 0)
    sent = mention_data.get("sentiment", 0.0)
    vids = mention_data.get("videos", [])

    if n == 0:
        return 0.0

    title_mentions      = sum(1 for v in vids if v.get("in_title"))
    has_member_src      = any(v.get("is_member_channel") for v in vids)
    has_member_only_src = any(v.get("is_member_only") for v in vids)

    # Base: mentions count
    if title_mentions >= 2:
        base = 9.0
    elif title_mentions == 1:
        base = 7.5
    elif n >= 3:
        base = 6.0
    elif n == 2:
        base = 5.0
    else:
        base = 3.5   # 1 mention, not in title

    # Member channel multiplier — applied to base before sentiment adjust
    if has_member_src:
        base = base * MEMBER_WEIGHT_MULTIPLIER

    # Member-only post bonus: exclusive paid content = highest trust signal
    member_only_bonus = 1.0 if has_member_only_src else 0.0

    # Sentiment modifier: -1.5 (bearish) to +1.5 (bullish)
    # Only meaningful when there IS a mention — "no mention" stays 0
    if sent > 0.35:
        sent_boost = +1.5
    elif sent > 0.10:
        sent_boost = +0.8
    elif sent < -0.35:
        sent_boost = -1.5
    elif sent < -0.10:
        sent_boost = -0.8
    else:
        sent_boost = 0.0

    return round(min(10.0, max(0.0, base + member_only_bonus + sent_boost)), 2)


# ─── Standalone probe ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    ap = argparse.ArgumentParser(description="YouTube enricher standalone probe")
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "NSE:RELIANCE", "NSE:INFY"],
                    help="TradingView-style symbols to check")
    ap.add_argument("--no-transcript", action="store_true",
                    help="Skip transcript fetching (faster, less accurate)")
    args = ap.parse_args()

    cookie_loaded = COMMUNITY_COOKIE_FILE.exists()
    print(f"\nYouTube Enricher — checking {len(EXPERT_CHANNELS)} channel(s)")
    print(f"Channels: {', '.join(EXPERT_CHANNELS.keys())}")
    member_names = [n for n, cid in EXPERT_CHANNELS.items() if cid in MEMBER_CHANNELS]
    if member_names:
        print(f"Member channels (1.5× weight): {', '.join(member_names)}")
    print(f"Video lookback:          {LOOKBACK_HOURS}h")
    print(f"Community post lookback: {COMMUNITY_LOOKBACK_HOURS}h ({COMMUNITY_LOOKBACK_HOURS//24}d)")
    if cookie_loaded:
        print(f"Cookie file: {COMMUNITY_COOKIE_FILE} (community posts ENABLED)")
    else:
        print(f"Cookie file: NOT FOUND at {COMMUNITY_COOKIE_FILE} — community posts skipped")
        print("  To enable: copy your YouTube Cookie header value from Chrome DevTools into that file")
    print()

    for sym in args.symbols:
        data  = get_youtube_mentions(sym, use_transcript=not args.no_transcript)
        score = score_youtube(data)
        has_member    = any(v.get("is_member_channel") for v in data.get("videos", []))
        member_tag    = "  [MEMBER ×1.5]" if has_member else ""
        cp_count      = data.get("community_posts", 0)
        cp_tag        = f"  [+{cp_count} community post(s)]" if cp_count else ""
        print(f"  {sym:<25}  mentions={data['mention_count']}  "
              f"sentiment={data['sentiment']:+.3f}  score={score:.1f}/10{member_tag}{cp_tag}")
        for v in data["videos"]:
            if v.get("is_community_post"):
                icon = "👑📝" if v.get("is_member_only") else "📝"
            elif v.get("is_member_channel"):
                icon = "👑"
            elif v.get("in_title"):
                icon = "📌"
            else:
                icon = "📄"
            print(f"      {icon} {v['title'][:80]}  [{v['channel']}]")
    print()
