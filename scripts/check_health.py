"""Probe Django through the internal listener using the configured public origin."""
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

origin = urlsplit(os.environ.get('PUBLIC_URL', 'https://localhost'))
request = Request('http://127.0.0.1:8000/health', headers={
    'Host': origin.netloc, 'X-Forwarded-Proto': origin.scheme,
})
with urlopen(request, timeout=5) as response:
    if response.status != 200 or json.load(response) != {'status': 'ok'}:
        raise SystemExit('Application health check failed')
