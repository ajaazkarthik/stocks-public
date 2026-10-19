"""
moomoo_community_scraper.py
============================
Fetches the public Moomoo community feed and official Insights posts.

The Moomoo community is a JavaScript SPA — the HTML shell returns empty content
from server-side rendering. This scraper uses Playwright (already installed for
Zerodha login) to render the page and intercept the XHR API calls that carry
the actual post data.

"""
"""
Usage:
    python moomoo_community_scraper.py            # print top 20 posts
    python moomoo_community_scraper.py --n 50     # top 50
    python moomoo_community_scraper.py --topic DRO.AU   # filter by ticker tag
    python moomoo_community_scraper.py --official        # Moomoo Insights only
    python moomoo_community_scraper.py --out reports/moomoo_community.html

Strategies used (in order):
1. Playwright browser + XHR intercept — captures the real feed JSON in-flight
2. Playwright browser + DOM parse — reads rendered HTML post elements
3. Static HTML scrape fallback (limited — SPA requires JS to hydrate)
"""
import sys, os, argparse, json, re, html, textwrap, datetime
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
OUT  = ROOT / "reports"

COMMUNITY_BASE = "https://www.moomoo.com"
COMMUNITY_FEED = "https://www.moomoo.com/community/nnq"

OFFICIAL_PROFILES = {
    "71146480":  "Moomoo Insights",
    "151064678": "Michael McCarthy CEO",
    "152343660": "Tina Teng",
    "151388429": "Jessica Amir",
    "73417671":  "Greg Boland",
}


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).strip()


def _clean(raw: str) -> str:
    text = _strip_tags(raw)
    text = re.sub(r"\s{2,}", " ", text)
    return textwrap.shorten(text, width=280, placeholder=" …")


def _ts(ms: int | str) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _normalise_item(item: dict) -> dict:
    uid   = str(item.get("userId", item.get("user_id", "")))
    name  = item.get("userName", item.get("nickname", item.get("nickName", uid)))
    body  = _clean(item.get("content", item.get("text", item.get("postContent", ""))))
    ts    = _ts(item.get("createTime", item.get("postTime", item.get("createAt", 0))))
    post_id = item.get("postId", item.get("id", item.get("feedId", "")))
    url   = f"{COMMUNITY_BASE}/community/feed/{post_id}" if post_id else COMMUNITY_FEED
    tickers = [
        t.get("stockName", t.get("code", ""))
        for t in (item.get("stockList", item.get("tagList", [])) or [])
        if t.get("stockName") or t.get("code")
    ]
    return {
        "author":   OFFICIAL_PROFILES.get(uid, name),
        "official": uid in OFFICIAL_PROFILES,
        "time":     ts,
        "text":     body,
        "url":      url,
        "tickers":  [t for t in tickers if t],
    }


# ── Strategy 1: Playwright XHR intercept ──────────────────────────────────────

def fetch_via_playwright(n: int = 20, official_only: bool = False,
                         topic_tag: str = "") -> list[dict]:
    """
    Launch a headless Chromium browser, navigate to the community feed,
    and intercept the XHR response that carries the post list JSON.
    Returns normalised post dicts.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    captured_posts: list[dict] = []
    api_response_bodies: list[str] = []

    url = COMMUNITY_FEED
    if topic_tag:
        url = f"{COMMUNITY_BASE}/community/nnq?keyword={topic_tag}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-AU",
        )
        page = ctx.new_page()

        def capture_response(response):
            """Intercept XHR responses with post data (Moomoo uses feed_id not postId)."""
            rurl = response.url
            if any(rurl.endswith(ext) for ext in [".js", ".css", ".png", ".jpg", ".webp", ".svg", ".ico", ".woff", ".woff2"]):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = response.text()
                if any(k in body for k in ['"feed_id"', '"feedId"', '"feedList"', '"postContent"', '"nickName"', '"userId"']):
                    api_response_bodies.append((rurl, body))
            except Exception:
                pass

        page.on("response", capture_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=35_000)
        except Exception:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
        # Extra wait for lazy-loaded content
        page.wait_for_timeout(4000)

        # Parse any captured XHR responses
        for rurl, body in api_response_bodies:
            try:
                data = json.loads(body)
                items = None
                # Navigate possible JSON structures
                for path in [
                    lambda d: d.get("data", {}).get("list"),
                    lambda d: d.get("result", {}).get("list"),
                    lambda d: d.get("list"),
                    lambda d: d.get("data"),
                ]:
                    try:
                        v = path(data)
                        if isinstance(v, list) and v:
                            items = v
                            break
                    except Exception:
                        pass
                if items:
                    for item in items[:n]:
                        try:
                            p = _normalise_item(item)
                            if p["text"] and p not in captured_posts:
                                captured_posts.append(p)
                        except Exception:
                            pass
            except json.JSONDecodeError:
                pass

        # Fallback: parse rendered page HTML if XHR intercept didn't capture enough
        if len(captured_posts) < 3:
            try:
                html_content = page.content()

                # Strategy A: __NEXT_DATA__ embedded JSON (Next.js SSR/ISR)
                next_data_el = page.query_selector("#__NEXT_DATA__")
                if next_data_el:
                    nd_text = next_data_el.inner_text()
                    try:
                        nd = json.loads(nd_text)
                        # Moomoo Next.js uses multiple possible feed paths
                        feed = (
                            nd.get("props", {}).get("pageProps", {}).get("feedList")
                            or nd.get("props", {}).get("pageProps", {}).get("data", {}).get("list")
                            or nd.get("props", {}).get("pageProps", {}).get("initialData", {}).get("list")
                            or nd.get("props", {}).get("pageProps", {}).get("postList")
                        )
                        if feed:
                            for item in feed[:n]:
                                try:
                                    p = _normalise_item(item)
                                    if p["text"]:
                                        captured_posts.append(p)
                                except Exception:
                                    pass
                    except json.JSONDecodeError:
                        pass

                # Strategy B: scan all inline <script> tags for embedded JSON arrays
                if len(captured_posts) < 3:
                    inline_scripts = re.findall(r'<script[^>]*>(.*?)</script>', html_content, re.DOTALL)
                    for script in inline_scripts:
                        if '"postId"' in script or '"feedId"' in script:
                            # Find JSON object/array boundaries
                            for m in re.finditer(r'(\{[^\{\}]{200,}\})', script):
                                try:
                                    obj = json.loads(m.group(1))
                                    p = _normalise_item(obj)
                                    if p["text"] and len(p["text"]) > 30:
                                        captured_posts.append(p)
                                except Exception:
                                    pass

                # Strategy C: use Moomoo's DOM selectors directly (Vue.js SPA structure)
                # Each post is a div[data-rep-key] > div[data-feed-type="wrapper"]
                # Structure confirmed from live page inspection
                if len(captured_posts) < 3:
                    post_wrappers = page.query_selector_all('div[data-rep-key]')
                    seen_ids: set[str] = {p.get("url", "") for p in captured_posts}
                    for wrapper in post_wrappers[:n]:
                        try:
                            feed_id = wrapper.get_attribute("data-rep-key") or wrapper.get_attribute("feed-id") or ""
                            if not feed_id:
                                continue
                            post_url = f"{COMMUNITY_BASE}/community/feed/{feed_id}"
                            if post_url in seen_ids:
                                continue
                            seen_ids.add(post_url)

                            # Get post body text from the rich-text-wrapper
                            body_el = wrapper.query_selector('[data-feed-type="rich-text"]') or wrapper.query_selector('[data-feed-type="wrapper"]')
                            post_text = body_el.inner_text().strip() if body_el else wrapper.inner_text().strip()
                            cleaned = _clean(post_text)
                            if not cleaned or len(cleaned) < 15:
                                continue

                            # Get author name — walk up to the common parent, then find .username a
                            author = "Community"
                            try:
                                author_el = wrapper.evaluate_handle(
                                    "el => el.closest('.nnq-list-item-content')?.parentElement?.querySelector('.username a')"
                                    " || el.closest('.nnq-list-item-content')?.previousElementSibling?.querySelector('a[href*=\"/community/profile/\"]')"
                                )
                                if author_el and author_el.as_element():
                                    author = (author_el.as_element().inner_text() or "Community").strip()
                            except Exception:
                                pass

                            # Get timestamp text
                            ts = "—"
                            try:
                                ts_el = wrapper.evaluate_handle(
                                    "el => el.closest('.nnq-list-item-content')?.previousElementSibling?.querySelector('.publish-time-text')"
                                )
                                if ts_el and ts_el.as_element():
                                    ts = (ts_el.as_element().inner_text() or "—").strip()
                            except Exception:
                                pass

                            # Check if official
                            user_id = wrapper.get_attribute("data-rep-s-stock_id") or ""
                            is_official = author in OFFICIAL_PROFILES.values()

                            # Extract ticker symbols from post text ($TICKER$) or stock links
                            tickers = re.findall(r'\$([A-Z]{1,5}(?:\.[A-Z]{2})?)\$', post_text)
                            stock_links = wrapper.query_selector_all('a[href*="/stock/"]')
                            for sl in stock_links[:5]:
                                href = sl.get_attribute("href") or ""
                                m = re.search(r'/stock/([A-Z0-9\.\-]+)', href)
                                if m and m.group(1) not in tickers:
                                    tickers.append(m.group(1))

                            captured_posts.append({
                                "author":   author,
                                "official": is_official,
                                "time":     ts,
                                "text":     cleaned,
                                "url":      post_url,
                                "tickers":  tickers,
                            })
                        except Exception:
                            pass

            except Exception:
                pass

        ctx.close()
        browser.close()

    if official_only:
        captured_posts = [p for p in captured_posts if p["official"]]
    return captured_posts[:n]


# ── Output renderers ───────────────────────────────────────────────────────────

def render_console(posts: list[dict]) -> None:
    if not posts:
        print("\nNo posts found.")
        print("The Moomoo community is a JavaScript SPA.")
        print("Ensure Playwright + Chromium are installed:")
        print("  pip install playwright && playwright install chromium")
        return
    for i, p in enumerate(posts, 1):
        badge    = " [★ Official]" if p["official"] else ""
        tickers  = "  Tags: " + " ".join(f"${t}$" for t in p["tickers"]) if p["tickers"] else ""
        print(f"\n{'─' * 70}")
        print(f"{i:2}. {p['author']}{badge}  {p['time']}")
        if tickers:
            print(f"   {tickers}")
        print(f"   {p['text']}")
        print(f"   {p['url']}")
    print(f"\n{'─' * 70}")
    print(f"Fetched {len(posts)} posts from Moomoo Community.")


def render_html(posts: list[dict], out_path: Path) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = ""
    for p in posts:
        badge = '<span class="badge">★ Official</span>' if p["official"] else ""
        tickers_html = "".join(
            f'<a class="ticker" href="{COMMUNITY_BASE}/stock/{t}" target="_blank">${t}$</a>'
            for t in (p["tickers"] or [])
        )
        rows += f"""
        <tr class="{'official' if p['official'] else 'community'}">
          <td class="meta"><strong>{html.escape(p['author'])}</strong> {badge}<br>
            <span class="ts">{html.escape(p['time'])}</span></td>
          <td class="content"><p>{html.escape(p['text'])}</p>{tickers_html}</td>
          <td class="link"><a href="{html.escape(p['url'])}" target="_blank">→</a></td>
        </tr>"""

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moomoo Community — {now}</title>
<style>
  body   {{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
           background:#0f1117;color:#e2e8f0;margin:0;padding:20px}}
  h1     {{color:#f97316;margin-bottom:4px}}
  .sub   {{color:#64748b;font-size:.85em;margin-bottom:20px}}
  table  {{width:100%;border-collapse:collapse}}
  th     {{background:#1e293b;color:#94a3b8;padding:10px 14px;text-align:left;font-size:.8em}}
  td     {{padding:12px 14px;border-bottom:1px solid #1e293b;vertical-align:top}}
  tr.official td{{background:#1a2035}}
  tr:hover td{{background:#1e293b}}
  .meta  {{width:180px;font-size:.85em}}
  .badge {{background:#f97316;color:#fff;font-size:.7em;padding:2px 6px;border-radius:4px;margin-left:4px}}
  .ts    {{color:#64748b}}
  .ticker{{background:#1e3a5f;color:#60a5fa;border-radius:4px;padding:2px 6px;margin-right:4px;
           font-size:.8em;text-decoration:none}}
  .link a{{color:#60a5fa;text-decoration:none;font-size:1.2em}}
  p      {{margin:0;font-size:.9em;line-height:1.5}}
</style></head>
<body>
<h1>Moomoo Community Feed</h1>
<div class="sub">Fetched {now} · {len(posts)} posts · via Playwright (headless)</div>
<table><thead><tr><th>Author</th><th>Post</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page_html, encoding="utf-8")
    print(f"Report written → {out_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(args):
    print(f"Fetching Moomoo community posts (n={args.n}"
          f"{', official only' if args.official else ''}"
          f"{f', topic: {args.topic}' if args.topic else ''})…")
    posts = fetch_via_playwright(n=args.n, official_only=args.official, topic_tag=args.topic)
    if args.out:
        render_html(posts, Path(args.out))
    else:
        render_console(posts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moomoo Community Feed Scraper (Playwright)")
    parser.add_argument("--n",        type=int,  default=20,  help="Posts to fetch (default: 20)")
    parser.add_argument("--official", action="store_true",    help="Official Moomoo strategist posts only")
    parser.add_argument("--topic",    default="",             help="Filter by ticker, e.g. DRO.AU")
    parser.add_argument("--out",      default="",             help="Output HTML path (blank = console)")
    args = parser.parse_args()
    main(args)
