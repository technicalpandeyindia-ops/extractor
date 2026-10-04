import re
import urllib.request
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def fix_url(url: str) -> str:
    url = url.strip()
    # If it's an Appx static CDN URL
    if 'appx.co.in' in url:
        url = re.sub(r'https?://[^/]+\.appx\.co\.in', 'https://appx-content-v2.classx.co.in', url)
        if '?' in url and ('URLPrefix=' in url or 'Expires=' in url or 'KeyName=' in url or 'Signature=' in url):
            url = url.split('?')[0]
    return url

with open('raw_list.txt', 'r', encoding='utf-8') as f:
    lines = f.readlines()

fixed_lines = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    if ':' in line:
        idx = line.find('http')
        if idx != -1:
            title = line[:idx].rstrip(':').strip()
            url = line[idx:].strip()
            fixed_u = fix_url(url)
            fixed_lines.append(f"{title}:{fixed_u}")
        else:
            fixed_lines.append(line)
    else:
        fixed_lines.append(line)

with open('fixed_list.txt', 'w', encoding='utf-8') as f:
    for fl in fixed_lines:
        f.write(fl + '\n')

print(f"Processed {len(fixed_lines)} lines.")
