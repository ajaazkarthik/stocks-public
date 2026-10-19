"""Extract portfolio data from the DOM + try to find positions via SPA internals."""
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

        # Track ALL API calls including non-JSON
        all_urls = []
        async def on_response(resp):
            url = resp.url
            if ('webull' in url or 'fintech' in url) and 'event/json' not in url and 'sentry' not in url:
                ct = resp.headers.get('content-type', '')
                size = resp.headers.get('content-length', '?')
                all_urls.append(f"{resp.status} [{ct[:30]}] ({size}b) {url}")
        page.on('response', on_response)
        
        # Also track WebSocket
        ws_data = []
        def on_ws(ws):
            print(f"  [WS] Connected: {ws.url}")
            def on_msg(payload):
                ws_data.append(payload)
            ws.on('framereceived', on_msg)
        page.on('websocket', on_ws)

        # Navigate to My Account
        print("=== Navigating to /center/ ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        if 'passport.webull.com.au' in page.url:
            print(f"  Session expired - need login again")
            await ctx.close()
            return

        await asyncio.sleep(3)
        print(f"  URL: {page.url}")

        # Step 1: Try to click the eye icon to show values
        print("\n=== Step 1: Try to unmask *** values ===")
        for selector in [
            'svg[class*="eye"]', 'button[class*="eye"]', '[class*="visibility"]',
            '[class*="toggle"]', '[class*="show"]', 'svg[class*="icon"]',
            '[data-testid*="eye"]', '[class*="mask"]',
        ]:
            try:
                count = await page.locator(selector).count()
                if count > 0:
                    print(f"  Found {count} elements: {selector}")
                    try:
                        await page.locator(selector).first.click()
                        await asyncio.sleep(1)
                    except:
                        pass
            except:
                pass
        
        # Also try clicking on the *** text itself
        try:
            stars = page.locator("text=***").first
            if await stars.is_visible(timeout=1000):
                await stars.click()
                await asyncio.sleep(1)
        except:
            pass

        # Step 2: Get page content after potential unmasking
        print("\n=== Step 2: Page content ===")
        text = await page.evaluate('() => document.body.innerText.substring(0, 5000)')
        for line in text.split('\n')[:60]:
            if line.strip():
                print(f"    {line.strip()}")

        # Step 3: Try to access SPA state  
        print("\n=== Step 3: SPA internal state ===")
        
        # Try React internals
        state_checks = [
            ("window.__NEXT_DATA__", "() => JSON.stringify(window.__NEXT_DATA__)?.substring(0, 500)"),
            ("window.__INITIAL_STATE__", "() => JSON.stringify(window.__INITIAL_STATE__)?.substring(0, 500)"),
            ("window.__STORE__", "() => JSON.stringify(window.__STORE__?.getState?.())?.substring(0, 500)"),
            ("Redux store via React root", """() => {
                const root = document.getElementById('root') || document.getElementById('app');
                if (!root) return 'no root';
                const key = Object.keys(root).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                if (!key) return 'no react fiber';
                let fiber = root[key];
                let state = null;
                let depth = 0;
                while (fiber && depth < 50) {
                    if (fiber.memoizedState && fiber.memoizedState.memoizedState) {
                        const s = fiber.memoizedState;
                        if (JSON.stringify(s).includes('account') || JSON.stringify(s).includes('position')) {
                            return JSON.stringify(s).substring(0, 500);
                        }
                    }
                    fiber = fiber.child || fiber.sibling || fiber.return;
                    depth++;
                }
                return 'no account state found in ' + depth + ' nodes';
            }"""),
            ("window.__wb_state__", "() => JSON.stringify(window.__wb_state__)?.substring(0, 500)"),
            ("window.wb", "() => JSON.stringify(Object.keys(window).filter(k => k.startsWith('wb') || k.startsWith('__') || k.includes('store') || k.includes('state')))"),
        ]

        for name, script in state_checks:
            try:
                result = await page.evaluate(script)
                if result and result != 'undefined' and result != 'null':
                    print(f"  {name}: {result[:300]}")
            except Exception as e:
                print(f"  {name}: error - {str(e)[:100]}")

        # Step 4: Check all window properties for account data
        print("\n=== Step 4: Window properties with account/position data ===")
        result = await page.evaluate("""() => {
            const found = [];
            for (const key of Object.keys(window)) {
                try {
                    const val = window[key];
                    if (val && typeof val === 'object') {
                        const s = JSON.stringify(val).substring(0, 200);
                        if (s.includes('position') || s.includes('holding') || 
                            s.includes('secAccount') || s.includes('portfolio') ||
                            s.includes('netLiquidation') || s.includes('totalAssets')) {
                            found.push({key: key, preview: s.substring(0, 200)});
                        }
                    }
                } catch(e) {}
            }
            return found;
        }""")
        for item in (result or []):
            print(f"  window.{item['key']}: {item['preview']}")

        # Step 5: Extract from login response data (saved in _all_traffic.json)
        print("\n=== Step 5: Check login response for account data ===")
        try:
            with open('_all_traffic.json') as f:
                traffic = json.load(f)
            for t in traffic:
                if 'login/account' in t['url']:
                    body = t['body']
                    print(f"  Login response from: {t['url']}")
                    # Print all keys and interesting values
                    for k, v in body.items():
                        if k in ['accessToken', 'refreshToken']:
                            print(f"    {k}: {str(v)[:40]}...")
                        else:
                            print(f"    {k}: {json.dumps(v, default=str)[:200]}")
        except Exception as e:
            print(f"  Error reading traffic: {e}")

        # Step 6: All URLs (including non-JSON) 
        print(f"\n=== Step 6: All requests ({len(all_urls)}) ===")
        for u in all_urls[:50]:
            print(f"  {u}")

        print(f"\n=== WebSocket data: {len(ws_data)} messages ===")
        for d in ws_data[:5]:
            print(f"  {str(d)[:200]}")

        await ctx.close()

asyncio.run(main())
