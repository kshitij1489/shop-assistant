"""Copyable standard-library client for independently deployed adapters.

Provider credentials, native webhook verification, durable receipts and provider
I/O belong to your adapter. No Django dependency is required by this module.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit, quote
from urllib.request import Request, urlopen


class AdapterClient:
    def __init__(self, base_url, connection_id, secret, timeout=15):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an HTTPS API origin/path without credentials, query or fragment.')
        self.base_url = base_url.rstrip('/') + '/v1/connections/' + quote(str(connection_id), safe='') + '/'
        self.secret, self.timeout = secret, timeout

    def request(self, method, endpoint, payload=None):
        url = self.base_url + endpoint
        parsed = urlsplit(url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        body = b'' if payload is None else json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
        stamp = str(int(time.time()))
        message = stamp.encode() + b'\n' + method.encode() + b'\n' + path.encode() + b'\n' + body
        signature = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        request = Request(url, data=body if method != 'GET' else None, method=method,
            headers={'Content-Type': 'application/json', 'X-Commerce-Timestamp': stamp, 'X-Commerce-Signature': signature})
        # Do not follow redirects with connection credentials/signatures.
        from urllib.request import HTTPRedirectHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            return json.load(response)

    def claim(self):
        return self.request('POST', 'commands/claim/', {})['commands']

    def acknowledge(self, command, outcome, error_code=''):
        return self.request('POST', 'commands/' + quote(command['command_id'], safe='') + '/ack/',
                            {'lease_token': command['lease_token'], 'outcome': outcome, 'error_code': error_code})

    def send_event(self, persisted_event):
        # Caller stores this event before sending, and retries the exact content.
        return self.request('POST', 'events/', persisted_event)
