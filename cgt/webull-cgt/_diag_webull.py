"""
Quick diagnostic: open Webull AU quote page and capture screenshots + HTML
to understand the login flow.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
GOOGLE_COOKIE_FILE = HERE / "webull"


def _parse_google_cookies():
    if not GOOGLE_COOKIE_FILE.exists():
        return []
    raw = GOOGLE_COOKIE_FILE.read_text(encoding="utf-8").strip()
    cookie_line = raw.splitlines()[0].strip()
    cookies = []
    for part in cookie_line.split(";"):
        part = part.strip()
        idx = part.find("=")
        if idx < 1:
            continue
        name  = part[:idx].strip()
        value = part[idx + 1:].strip()
        is_secure = name.startswith("__Secure-") or name.startswith("__Host-")
        cookies.append({
            "name":     name,
            "value":    value,
            "domain":   ".google.com",
            "path":     "/",
            "secure":   is_secure,
            "httpOnly": False,
            "sameSite": "None" if is_secure else "Lax",
        })
    return cookies


async def main():
    google_cookies = _parse_google_cookies()
    print(f"Loaded {len(google_cookies)} Google cookies")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        if google_cookies:
            await context.add_cookies(google_cookies)

        page = await context.new_page()

        # Capture all network requests
        api_calls = []
        async def on_response(response):
            url = response.url
            if "webull" not in url:
                return
            print(f"  [NET] {response.status} {url}")
            ct = response.headers.get("content-type", "")
            if "json" in ct or "api" in url.lower():
                try:
                    body = await response.json()
                    api_calls.append({"url": url, "status": response.status, "body": body})
                except Exception:
                    pass

        page.on("response", on_response)

        # Step 1: Navigate to homepage
        print("\n=== Step 1: Navigate to homepage ===")
        await page.goto("https://www.webull.com.au/", timeout=30_000)
        await page.wait_for_timeout(3_000)
        await page.screenshot(path=str(HERE / "_diag_1_homepage.png"))
        print(f"URL after homepage: {page.url}")

        # Check what google-related elements exist
        print("\n=== Checking [class*='google'] elements ===")
        els = await page.query_selector_all("[class*='google']")
        for el in els[:10]:
            tag = await el.evaluate("el => el.tagName")
            text = await el.evaluate("el => el.textContent.substring(0, 80)")
            cls = await el.get_attribute("class") or ""
            print(f"  {tag}: class={cls[:80]!r}, text={text!r}")

        # Step 2: Navigate to /quote
        print("\n=== Step 2: Navigate to /quote ===")
        await page.goto("https://www.webull.com.au/quote", timeout=30_000)
        await page.wait_for_timeout(4_000)
        await page.screenshot(path=str(HERE / "_diag_2_quote.png"))
        print(f"URL after /quote: {page.url}")

        # Check for login buttons on quote page
        print("\n=== Checking login-related elements on /quote ===")
        for sel in ["button", "a[href*='login']", "a[href*='signin']", "[class*='login']", "[class*='signin']", "[class*='auth']"]:
            els = await page.query_selector_all(sel)
            for el in els[:5]:
                text = await el.evaluate("el => el.textContent")
                if any(kw in (text or "").lower() for kw in ["login", "sign", "log in", "google", "account"]):
                    href = await el.get_attribute("href") or ""
                    cls = await el.get_attribute("class") or ""
                    print(f"  [{sel}] text={text[:60]!r}, href={href!r}, class={cls[:60]!r}")

        # Step 3: Save page HTML
        html = await page.content()
        (HERE / "_diag_quote_page.html").write_text(html, encoding="utf-8")
        print(f"\nSaved /quote HTML to _diag_quote_page.html ({len(html)} chars)")

        print("\n=== API calls captured ===")
        for c in api_calls:
            print(f"  {c['url']}")

        print("\nKeeping browser open for 30 seconds so you can manually inspect...")
        await page.wait_for_timeout(30_000)
        await browser.close()

asyncio.run(main())
