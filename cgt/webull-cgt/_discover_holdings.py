"""Probe: First login via setup, then use in-browser fetch to discover holdings endpoints."""
import asyncio, json
from playwright.async_api import async_playwright

_PORTFOLIO_URL = "https://www.webull.com.au/center/sc"

# Potential trade API base URLs to try (based on AU region pattern a1u*)
_TRADE_BASES = [
    "https://a1utrade.webullfintech.com",
    "https://austrade.webullfintech.com",
    "https://tradeapi.webullfintech.com",
    "https://trade.webullfintech.com",
]

# Known from browser: a1uuser.webullfintech.com/api/user/v1/
_USER_BASE = "https://a1uuser.webullfintech.com/api/user/v1"

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str('.webull_profile'),
            headless=False,
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Capture access_token and all API traffic
        captured_token = {}
        all_traffic = []

        async def on_request(req):
            url = req.url
            if ('webull' in url or 'fintech' in url):
                hdrs = req.headers
                if 'access_token' in hdrs and not captured_token.get('token'):
                    captured_token['token'] = hdrs['access_token']
                    captured_token['did'] = hdrs.get('did', '')
                    # Also capture ALL headers for later use
                    captured_token['headers'] = dict(hdrs)
                    print(f"  [TOKEN] Captured from request headers")

        async def on_response(resp):
            url = resp.url
            if 'webull' not in url and 'fintech' not in url:
                return
            if 'event/json' in url or 'sentry' in url:
                return
            try:
                body = await resp.json()
                all_traffic.append({"url": url, "status": resp.status, "body": body})
                # Check for holdings-like data
                body_str = json.dumps(body)
                for key in ['positions', 'holdingList', 'secAccountId', 'accountId', 'assetAccount']:
                    if key in body_str:
                        print(f"  [HIT] Found '{key}' in response from {url}")
                        with open('_probe_hit.json', 'w') as f:
                            json.dump({"url": url, "body": body}, f, indent=2)
            except:
                pass

        page.on('request', on_request)
        page.on('response', on_response)

        # Navigate to portfolio - this will redirect to login
        print("=== Step 1: Navigate to portfolio ===")
        await page.goto(_PORTFOLIO_URL, wait_until='networkidle', timeout=30000)
        current_url = page.url

        if 'passport.webull.com.au' in current_url:
            print(f"  On login page. Waiting for manual login (5 min timeout)...")
            # Wait for redirect away from login
            for i in range(150):
                await asyncio.sleep(2)
                url = page.url
                if 'passport.webull.com.au' not in url:
                    print(f"  Login detected! Now at: {url}")
                    break
            else:
                print("  TIMEOUT waiting for login!")
                await ctx.close()
                return
            
            # Wait for page to settle
            await asyncio.sleep(5)
        else:
            print(f"  Already logged in at: {current_url}")

        print(f"\n=== Step 2: Checking page state ===")
        title = await page.title()
        print(f"  Title: {title}")
        
        # Get visible text
        text = await page.evaluate('() => document.body.innerText.substring(0, 2000)')
        for line in text.split('\n')[:30]:
            if line.strip():
                print(f"    {line.strip()}")

        # Wait for any more API calls
        await asyncio.sleep(3)

        print(f"\n=== Step 3: Check for navigation elements ===")
        # Find clickable elements that might lead to positions/holdings
        nav_elements = await page.evaluate('''() => {
            const results = [];
            const links = document.querySelectorAll('a, button, [role="tab"], [role="button"]');
            for (const el of links) {
                const text = el.innerText?.trim() || '';
                if (text && text.length < 50) {
                    results.push({tag: el.tagName, text: text, href: el.href || ''});
                }
            }
            return results;
        }''')
        for el in nav_elements:
            print(f"  [{el['tag']}] {el['text']}" + (f" → {el['href']}" if el['href'] else ""))

        # Look specifically for Account/Portfolio/Positions-related elements
        print(f"\n=== Step 4: Try clicking on portfolio/account tabs ===")
        for selector in [
            'text=Account', 'text=Positions', 'text=Holdings', 'text=Portfolio',
            'text=Stocks', 'text=Assets', 'text=Balance',
            '[data-testid*="account"]', '[data-testid*="position"]',
            '[class*="account"]', '[class*="position"]',
        ]:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=500):
                    print(f"  Found visible: {selector}")
                    await el.click()
                    await asyncio.sleep(3)
                    print(f"  After click, URL: {page.url}")
            except:
                pass

        # Step 5: Try in-browser fetch to discover endpoints
        print(f"\n=== Step 5: In-browser fetch discovery ===")
        if captured_token.get('token'):
            token = captured_token['token']
            did = captured_token['did']
            
            # First: get secAccountId via user API
            result = await page.evaluate(f'''async () => {{
                try {{
                    const resp = await fetch("{_USER_BASE}/account/querySecAccountList", {{
                        method: 'GET',
                        headers: {{
                            'Accept': 'application/json',
                            'Content-Type': 'application/json',
                            'access_token': '{token}',
                            'did': '{did}',
                            'platform': 'web',
                            'app': 'global',
                        }}
                    }});
                    const data = await resp.json();
                    return JSON.stringify(data);
                }} catch(e) {{
                    return 'ERROR: ' + e.message;
                }}
            }}''')
            print(f"  querySecAccountList → {result[:500]}")

            # Try other account endpoints
            for ep in [
                f"{_USER_BASE}/userInfo/queryUserInfo/v2",
                f"{_USER_BASE}/account/list",
                "https://a1utrade.webullfintech.com/api/trade/v1/getPositions",
                "https://a1utrade.webullfintech.com/api/trading/v1/au/positions",
                "https://austrade.webullfintech.com/api/trade/v1/getPositions",
            ]:
                result = await page.evaluate(f'''async () => {{
                    try {{
                        const resp = await fetch("{ep}", {{
                            method: 'GET',
                            headers: {{
                                'Accept': 'application/json',
                                'Content-Type': 'application/json',
                                'access_token': '{token}',
                                'did': '{did}',
                                'platform': 'web',
                                'app': 'global',
                            }}
                        }});
                        return resp.status + ': ' + (await resp.text()).substring(0, 300);
                    }} catch(e) {{
                        return 'ERROR: ' + e.message;
                    }}
                }}''')
                print(f"  {ep}")
                print(f"    → {result[:300]}")

        # Step 6: Dump all traffic
        print(f"\n=== All API traffic ({len(all_traffic)} calls) ===")
        for t in all_traffic:
            keys = list(t['body'].keys()) if isinstance(t['body'], dict) else ['[list]']
            print(f"  {t['status']} {t['url']}")
            print(f"       keys: {keys}")

        await ctx.close()

asyncio.run(main())
