"""Deep probe: inspect Webull AU page content, WebSockets, localStorage."""
import asyncio, json
from playwright.async_api import async_playwright

async def deep_probe():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str('.webull_profile'),
            headless=False,
            viewport={'width': 1280, 'height': 900}
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        
        ws_messages = []
        
        # Monitor WebSocket messages
        def on_ws(ws):
            print(f'  [WS] Connected: {ws.url}')
            ws.on('framereceived', lambda msg: ws_messages.append(msg) or print(f'  [WS-MSG] {str(msg)[:200]}'))
        page.on('websocket', on_ws)
        
        # Catch ALL requests
        all_urls = []
        async def on_resp(resp):
            url = resp.url
            if 'webull' in url or 'fintech' in url:
                ct = resp.headers.get('content-type', '')
                if 'event/json' not in url and 'sentry' not in url:
                    all_urls.append(url)
                    if 'json' in ct:
                        try:
                            body = await resp.json()
                            if 'haveAccount' in str(body):
                                print(f'  [ACCOUNT-FLAG] haveAccount = {body.get("haveAccount")}')
                                subset = {k: v for k, v in body.items()
                                          if k in ["haveAccount","uuid","email","regionId","nickName"]}
                                print(f'  [ACCOUNT-FLAG] {json.dumps(subset)}')
                        except:
                            pass
        page.on('response', on_resp)
        
        print('=== Navigating to /center/sc ===')
        await page.goto('https://www.webull.com.au/center/sc', wait_until='networkidle', timeout=30000)
        await asyncio.sleep(3)
        
        # Check page content
        print('\n=== Page content analysis ===')
        title = await page.title()
        print(f'  Title: {title}')
        
        body_text = await page.evaluate('() => document.body.innerText.substring(0, 3000)')
        print('  Body text (first 3000 chars):')
        for line in body_text.split('\n')[:50]:
            if line.strip():
                print(f'    {line.strip()}')
        
        # Check for indicators
        checks = ['open account', 'holding', 'position', 'portfolio', 'no data',
                   'stock', 'asset', 'balance', 'equity', 'cash']
        for word in checks:
            found = await page.evaluate(f'() => document.body.innerText.toLowerCase().includes("{word}")')
            if found:
                print(f'  Found "{word}" in page text')
        
        # Check localStorage
        print('\n=== localStorage (relevant keys) ===')
        ls_keys = await page.evaluate('() => Object.keys(localStorage)')
        print(f'  Total keys: {len(ls_keys)}')
        for k in sorted(ls_keys):
            kl = k.lower()
            if any(x in kl for x in ['account', 'token', 'trade', 'position', 'hold',
                                       'portfolio', 'sec', 'asset', 'auth', 'user']):
                val = await page.evaluate(f'() => localStorage.getItem("{k}")?.substring(0, 300)')
                print(f'  {k} = {val}')
        
        # Also dump ALL localStorage keys (short)
        print('\n=== ALL localStorage keys ===')
        for k in sorted(ls_keys):
            print(f'  {k}')
        
        print(f'\n=== WebSocket messages: {len(ws_messages)} ===')
        for m in ws_messages[:10]:
            print(f'  {str(m)[:200]}')
        
        print(f'\n=== Total non-telemetry API URLs: {len(all_urls)} ===')
        for u in all_urls:
            print(f'  {u}')
            
        await ctx.close()

asyncio.run(deep_probe())
