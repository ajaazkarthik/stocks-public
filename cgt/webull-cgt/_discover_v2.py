"""Navigate to the CORRECT account page /center/ and intercept all holdings traffic."""
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

        captured_token = {}
        all_traffic = []
        request_headers_dump = []

        async def on_request(req):
            url = req.url
            if ('webull' in url or 'fintech' in url) and 'event/json' not in url and 'sentry' not in url:
                hdrs = req.headers
                if 'access_token' in hdrs:
                    if not captured_token.get('token'):
                        captured_token['token'] = hdrs['access_token']
                        captured_token['did'] = hdrs.get('did', '')
                        print(f"  [TOKEN] Captured access_token")
                    # Dump ALL headers from a working request (first 3)
                    if len(request_headers_dump) < 3:
                        request_headers_dump.append({
                            'url': url,
                            'method': req.method,
                            'headers': dict(hdrs)
                        })

        async def on_response(resp):
            url = resp.url
            if 'webull' not in url and 'fintech' not in url:
                return
            if 'event/json' in url or 'sentry' in url:
                return
            try:
                ct = resp.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                body = await resp.json()
                entry = {"url": url, "status": resp.status, "body": body}
                all_traffic.append(entry)
                
                # Highlight important finds
                body_str = json.dumps(body)
                for key in ['positions', 'holdingList', 'holdings', 'secAccountId',
                            'accountId', 'assetAccount', 'portfolio', 'balance',
                            'userAccountRels', 'netLiquidation', 'totalAssets',
                            'equity', 'marketValue', 'costBasis', 'stockPosition']:
                    if key in body_str:
                        print(f"  *** FOUND '{key}' in {url} ***")
                        # Save the full response
                        fname = f"_probe_{key}.json"
                        with open(fname, 'w') as f:
                            json.dump(entry, f, indent=2, default=str)
                        break
            except:
                pass

        page.on('request', on_request)
        page.on('response', on_response)

        # Step 1: Login
        print("=== Step 1: Navigate to /center/ (My Account) ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        if 'passport.webull.com.au' in page.url:
            print(f"  On login page. Waiting for login (5 min)...")
            for i in range(150):
                await asyncio.sleep(2)
                if 'passport.webull.com.au' not in page.url:
                    print(f"  Login detected! Now at: {page.url}")
                    break
            else:
                print("  TIMEOUT!")
                await ctx.close()
                return
            await asyncio.sleep(5)
        else:
            print(f"  Already logged in: {page.url}")
        
        # Step 2: Navigate to My Account page specifically
        print(f"\n=== Step 2: Navigate to /center/ ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        await asyncio.sleep(5)
        
        print(f"  URL: {page.url}")
        title = await page.title()
        print(f"  Title: {title}")
        
        # Page text
        text = await page.evaluate('() => document.body.innerText.substring(0, 3000)')
        print(f"\n  Page content:")
        for line in text.split('\n')[:50]:
            if line.strip():
                print(f"    {line.strip()}")
        
        # Step 3: Try different account pages
        print(f"\n=== Step 3: Try more specific pages ===")
        for path in ['/center/home', '/my/account', '/my/stocks', '/center/stocks',
                     '/center/holdings', '/account/center', '/center/overview',
                     '/center/asset', '/my/portfolio']:
            url = f'https://www.webull.com.au{path}'
            resp = await page.goto(url, wait_until='networkidle', timeout=10000)
            actual = page.url
            print(f"  {path} → {actual} (title: {await page.title()})")
            await asyncio.sleep(2)
        
        # Step 4: Navigate BACK to /center/ and look for clickable items
        print(f"\n=== Step 4: Check /center/ navigation ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=15000)
        await asyncio.sleep(3)
        
        # Find all links on the page
        links = await page.evaluate('''() => {
            return [...document.querySelectorAll('a[href]')].map(a => ({
                text: a.innerText?.trim().substring(0, 60),
                href: a.href
            })).filter(l => l.href.includes('webull.com.au') && l.text);
        }''')
        print(f"  Links on page:")
        for l in links:
            print(f"    [{l['text']}] → {l['href']}")
        
        # Step 5: Dump ALL request headers from working calls
        print(f"\n=== Step 5: Request headers from working calls ===")
        for rh in request_headers_dump:
            print(f"\n  URL: {rh['url']}")
            print(f"  Method: {rh['method']}")
            for k, v in sorted(rh['headers'].items()):
                if k.startswith(':'):
                    continue
                print(f"    {k}: {v[:100]}")
        
        # Step 6: All API traffic
        print(f"\n=== Step 6: All API traffic ({len(all_traffic)} calls) ===")
        for t in all_traffic:
            keys = list(t['body'].keys()) if isinstance(t['body'], dict) else ['[list]']
            print(f"  {t['status']} {t['url']}")
            print(f"       keys: {keys}")
        
        # Save all traffic
        with open('_all_traffic.json', 'w') as f:
            json.dump(all_traffic, f, indent=2, default=str)
        print(f"\n  Saved all traffic to _all_traffic.json")
        
        await ctx.close()

asyncio.run(main())
