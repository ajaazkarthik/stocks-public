"""Focused probe: stay on /center/ My Account and extract all data from DOM."""
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str('.webull_profile'),
            headless=False,
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Intercept the initial HTML to check for SSR data
        html_body = {}
        async def route_handler(route):
            resp = await route.fetch()
            body = await resp.text()
            html_body['html'] = body
            await route.fulfill(response=resp)
        
        await page.route('**/center/', route_handler)
        await page.route('**/center', route_handler)

        # Also track ALL API responses
        api_data = []
        async def on_response(resp):
            url = resp.url
            if ('webull' in url or 'fintech' in url) and 'event/json' not in url and 'sentry' not in url:
                try:
                    ct = resp.headers.get('content-type', '')
                    if 'json' in ct:
                        body = await resp.json()
                        api_data.append({'url': url, 'status': resp.status, 'body': body})
                except:
                    pass
        page.on('response', on_response)

        # Navigate to My Account
        print("=== Navigating to /center/ ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        if 'passport.webull.com.au' in page.url:
            print("  Session expired! Need to re-login.")
            print("  URL:", page.url)
            # Wait for login
            print("  Please log in manually (5 min timeout)...")
            for _ in range(150):
                await asyncio.sleep(2)
                if 'passport.webull.com.au' not in page.url:
                    print(f"  Logged in! URL: {page.url}")
                    break
            else:
                print("  Timeout!")
                await ctx.close()
                return
            # Navigate to /center/ after login
            await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        await asyncio.sleep(5)
        print(f"  URL: {page.url}")
        print(f"  Title: {await page.title()}")

        # Step 1: Check SSR HTML for embedded data
        print("\n=== Step 1: Check SSR HTML ===")
        if html_body.get('html'):
            html = html_body['html']
            print(f"  HTML length: {len(html)}")
            # Search for embedded JSON data (common SSR pattern)
            for marker in ['__INITIAL_STATE__', '__NEXT_DATA__', '__APP_DATA__', 
                          'window.__initState__', 'window.__data__', 'initialState',
                          'accountValue', 'netLiquidation', 'totalAssets',
                          'positions', 'holdingList']:
                idx = html.find(marker)
                if idx >= 0:
                    print(f"  Found '{marker}' at char {idx}")
                    print(f"    Context: ...{html[max(0,idx-50):idx+200]}...")
        else:
            print("  No HTML captured via route")

        # Step 2: Full DOM text (don't click anything to avoid navigation)
        print("\n=== Step 2: Full page text ===")
        text = await page.evaluate('() => document.body.innerText')
        for line in text.split('\n'):
            if line.strip():
                print(f"    {line.strip()}")

        # Step 3: Try to read values behind *** masks
        print("\n=== Step 3: DOM element values (including hidden) ===")
        result = await page.evaluate("""() => {
            const data = [];
            // Get ALL elements and their text + data attributes
            const allEls = document.querySelectorAll('[class*="account"], [class*="value"], [class*="amount"], [class*="balance"], [class*="position"], [class*="holding"], [class*="asset"], [class*="portfolio"]');
            for (const el of allEls) {
                data.push({
                    tag: el.tagName,
                    class: el.className?.substring?.(0, 80) || '',
                    text: el.innerText?.substring(0, 200) || '',
                    data: Object.fromEntries([...el.attributes].filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value]))
                });
            }
            return data.slice(0, 50);
        }""")
        for item in (result or []):
            if item.get('text', '').strip():
                print(f"  <{item['tag']} class='{item['class'][:60]}'>")
                print(f"    text: {item['text'][:150]}")
                if item.get('data'):
                    print(f"    data: {item['data']}")

        # Step 4: Check for individual stock positions in the page
        print("\n=== Step 4: Look for stock tickers/positions ===")
        tickers = await page.evaluate("""() => {
            const body = document.body.innerHTML;
            // Look for common AU stock patterns (3-letter codes)
            const results = [];
            // Check for any table rows with numbers
            const tables = document.querySelectorAll('table, [role="grid"], [class*="table"], [class*="list"]');
            for (const t of tables) {
                results.push({type: 'table', html: t.innerHTML.substring(0, 500), text: t.innerText.substring(0, 500)});
            }
            // Check for any element that looks like it has stock data
            const stockEls = document.querySelectorAll('[class*="stock"], [class*="ticker"], [class*="symbol"], [class*="instrument"]');
            for (const el of stockEls) {
                results.push({type: 'stock-element', class: el.className?.substring?.(0, 80), text: el.innerText?.substring(0, 200)});
            }
            return results.slice(0, 20);
        }""")
        for item in (tickers or []):
            print(f"  [{item['type']}] {item.get('class', '')} - {item.get('text', '')[:200]}")

        # Step 5: Check ALL API data for account/position info
        print(f"\n=== Step 5: API calls ({len(api_data)}) ===")
        for d in api_data:
            keys = list(d['body'].keys()) if isinstance(d['body'], dict) else ['[list]']
            print(f"  {d['status']} {d['url']}")
            print(f"       keys: {keys}")
            # Look for account-related keys
            body = d['body']
            if isinstance(body, dict):
                for k in ['userAccountRels', 'secAccountId', 'accountId', 'haveAccount']:
                    if k in body:
                        print(f"       >>> {k}: {json.dumps(body[k], default=str)[:300]}")

        # Step 6: Check Service Workers
        print("\n=== Step 6: Service Workers ===")
        sw_check = await page.evaluate("""async () => {
            if ('serviceWorker' in navigator) {
                const regs = await navigator.serviceWorker.getRegistrations();
                return regs.map(r => ({scope: r.scope, active: !!r.active}));
            }
            return [];
        }""")
        print(f"  Service Workers: {json.dumps(sw_check)}")

        # Step 7: Check IndexedDB for cached data
        print("\n=== Step 7: IndexedDB databases ===")
        idb = await page.evaluate("""async () => {
            try {
                const dbs = await indexedDB.databases();
                return dbs.map(d => ({name: d.name, version: d.version}));
            } catch(e) {
                return 'Error: ' + e.message;
            }
        }""")
        print(f"  Databases: {json.dumps(idb)}")

        # Step 8: Screenshot for visual reference
        await page.screenshot(path='_account_screenshot.png', full_page=True)
        print("\n  Saved screenshot to _account_screenshot.png")

        await ctx.close()

asyncio.run(main())
