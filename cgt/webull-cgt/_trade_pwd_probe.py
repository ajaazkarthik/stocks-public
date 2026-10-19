"""Try clicking Verify and entering trading password to unlock account data."""
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

        # Track API calls
        api_data = []
        async def on_response(resp):
            url = resp.url
            if ('webull' in url or 'fintech' in url) and 'event/json' not in url and 'sentry' not in url:
                ct = resp.headers.get('content-type', '')
                if 'json' in ct:
                    try:
                        body = await resp.json()
                        api_data.append({'url': url, 'status': resp.status, 'body': body})
                        # Print ALL API calls
                        keys = list(body.keys()) if isinstance(body, dict) else ['[list]']
                        print(f"  [API] {resp.status} {url}")
                        print(f"         keys: {keys}")
                    except:
                        pass
        page.on('response', on_response)

        # Navigate
        print("=== Navigating to /center/ ===")
        await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        if 'passport.webull.com.au' in page.url:
            print("  Session expired! Waiting for login (5 min)...")
            for _ in range(150):
                await asyncio.sleep(2)
                if 'passport.webull.com.au' not in page.url:
                    print(f"  Logged in! URL: {page.url}")
                    break
            else:
                await ctx.close()
                return
            await asyncio.sleep(3)
            await page.goto('https://www.webull.com.au/center/', wait_until='networkidle', timeout=30000)
        
        await asyncio.sleep(3)

        # Check if we're on the trading password screen
        body_text = await page.evaluate('() => document.body.innerText')
        if 'trading password' in body_text.lower():
            print("\n=== Trading password screen detected ===")
            
            # Click Verify button
            print("  Clicking Verify button...")
            try:
                await page.click('text=Verify', timeout=5000)
                await asyncio.sleep(2)
                
                # Take screenshot to see what appeared
                await page.screenshot(path='_verify_dialog.png')
                print("  Screenshot saved: _verify_dialog.png")
                
                # Check for any input fields (password entry)
                inputs = await page.evaluate("""() => {
                    const results = [];
                    const inputs = document.querySelectorAll('input');
                    for (const inp of inputs) {
                        results.push({
                            type: inp.type,
                            name: inp.name,
                            id: inp.id,
                            placeholder: inp.placeholder,
                            class: inp.className?.substring(0, 80),
                            maxLength: inp.maxLength,
                            visible: inp.offsetParent !== null
                        });
                    }
                    return results;
                }""")
                print(f"\n  Input fields found: {len(inputs)}")
                for inp in inputs:
                    print(f"    type={inp['type']} name={inp['name']} id={inp['id']} placeholder={inp['placeholder']} maxLength={inp['maxLength']} visible={inp['visible']}")
                
                # Check for dialog/modal
                dialog_text = await page.evaluate("""() => {
                    // Check for modals/dialogs
                    const modals = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="popup"], [class*="overlay"]');
                    const results = [];
                    for (const m of modals) {
                        results.push({
                            class: m.className?.substring(0, 80),
                            text: m.innerText?.substring(0, 500),
                            visible: m.offsetParent !== null || m.style.display !== 'none'
                        });
                    }
                    return results;
                }""")
                print(f"\n  Dialogs/Modals: {len(dialog_text)}")
                for d in dialog_text:
                    print(f"    class={d['class']}")
                    print(f"    text={d['text'][:200]}")
                    print(f"    visible={d['visible']}")
                
                # Get full page text after clicking Verify
                new_text = await page.evaluate('() => document.body.innerText')
                if new_text != body_text:
                    print(f"\n  Page text changed! New content:")
                    for line in new_text.split('\n')[:30]:
                        if line.strip():
                            print(f"    {line.strip()}")
                
                # Try entering password in any visible input
                visible_inputs = [i for i in inputs if i['visible']]
                if visible_inputs:
                    print(f"\n  Visible inputs: {len(visible_inputs)}")
                    inp = visible_inputs[0]
                    # Try the known passwords
                    for pwd in ['09061993', '090619', 'K@rthik99']:
                        print(f"\n  Trying password: {'*' * len(pwd)}")
                        # Clear and type
                        if inp['type'] == 'password' or inp['type'] == 'tel' or inp['type'] == 'text':
                            selector = f"input[type='{inp['type']}']"
                            if inp['id']:
                                selector = f"#{inp['id']}"
                            try:
                                await page.fill(selector, pwd)
                                await asyncio.sleep(0.5)
                                
                                # Try submitting
                                # Look for confirm/submit button
                                for btn_text in ['Confirm', 'Submit', 'OK', 'Verify', 'Enter']:
                                    try:
                                        btn = page.locator(f'button:has-text("{btn_text}")').first
                                        if await btn.is_visible(timeout=500):
                                            await btn.click()
                                            await asyncio.sleep(3)
                                            
                                            # Check if it worked
                                            current_text = await page.evaluate('() => document.body.innerText')
                                            if 'trading password' not in current_text.lower():
                                                print(f"  SUCCESS! Account data should be loading...")
                                                await asyncio.sleep(5)
                                                break
                                            else:
                                                # Check for error messages
                                                error = await page.evaluate("""() => {
                                                    const errs = document.querySelectorAll('[class*="error"], [class*="warning"], [class*="hint"]');
                                                    return [...errs].map(e => e.innerText).filter(t => t).join(' | ');
                                                }""")
                                                if error:
                                                    print(f"  Error: {error}")
                                    except:
                                        pass
                            except Exception as e:
                                print(f"  Error filling: {e}")
                else:
                    # Maybe individual digit inputs (PIN-style)
                    all_visible = await page.evaluate("""() => {
                        const inputs = document.querySelectorAll('input');
                        return [...inputs].filter(i => i.offsetParent !== null).map(i => ({
                            type: i.type, maxLength: i.maxLength, 
                            rect: i.getBoundingClientRect(),
                            class: i.className?.substring(0, 50)
                        }));
                    }""")
                    print(f"\n  All visible inputs after click: {len(all_visible)}")
                    for i in all_visible:
                        print(f"    {i}")
                    
            except Exception as e:
                print(f"  Error clicking Verify: {e}")
        else:
            print("  No trading password screen! Account may already be unlocked.")
        
        # Final state - dump all API data
        print(f"\n=== Final API data ({len(api_data)} calls) ===")
        for d in api_data:
            keys = list(d['body'].keys()) if isinstance(d['body'], dict) else ['[list]']
            print(f"  {d['status']} {d['url']}")
            print(f"       keys: {keys}")
        
        # Save all data
        with open('_trade_pwd_probe.json', 'w') as f:
            json.dump(api_data, f, indent=2, default=str)
        
        await ctx.close()

asyncio.run(main())
