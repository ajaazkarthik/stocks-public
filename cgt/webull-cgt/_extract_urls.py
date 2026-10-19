import urllib.request, re
req = urllib.request.Request('https://www.webull.com.au/', headers={'User-Agent': 'Mozilla/5.0'})
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode('utf-8', errors='replace')
pattern = 'href="(https?://[^"]+webull[^"]+)"'
urls = sorted(set(re.findall(pattern, html)))
for u in urls[:60]:
    print(u)
print('---done---')
