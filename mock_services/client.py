"""HTTP provider boundary; production commerce transport remains HTTPS-only."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MockClient:
    def __init__(self, base_url='http://127.0.0.1:9080', timeout=15):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Use a loopback HTTP mock origin, such as http://127.0.0.1:9080.')
        self.base_url, self.timeout = base_url.rstrip('/'), timeout

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base_url + path, method=method, data=body,
                          headers={'Content-Type': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            return json.load(response)


class HTTPProvider:
    """Duck-types the reference Worker's provider, with actual HTTP calls."""
    def __init__(self, client, account, role):
        if role not in ('payment', 'pos'):
            raise ValueError('Unsupported provider role.')
        self.client, self.account, self.role = client, str(UUID(account)), role
        self.path = '/v1/accounts/' + self.account + ('/payments' if role == 'payment' else '/orders')

    def request(self, method, path, payload=None):
        try:
            return self.client.request(method, path, payload)
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise ValueError('Mock provider rejected request (HTTP %s).' % exc.code) from exc
            raise TimeoutError('Mock provider outcome is unknown.') from exc
        except (OSError, URLError) as exc:
            raise TimeoutError('Mock provider outcome is unknown.') from exc

    def create(self, command, *, timeout_after_commit=False):
        if timeout_after_commit:
            raise ValueError('Configure timeout_after_commit_next through /admin/faults.')
        return self.request('POST', self.path, command)

    def lookup(self, *, key=None, external_id=None):
        query = {'key': key} if key else {'external_id': external_id}
        return self.request('GET', self.path + '?' + urlencode(query))['resource']

    def capture(self, payment_id):
        return self.request('POST', self.path + '/' + str(UUID(payment_id)) + '/capture', {})


def refresh_payments(worker, auto_capture=False):
    """Poll known pending resources after restart, then persist normalized events.

    Provider state survives a lost response. Each observation uses the reference
    worker's identity validation, sequence deduplication and durable event outbox.
    """
    if worker.provider.role != 'payment':
        return
    rows = worker.db.execute('SELECT observation FROM object_state').fetchall()
    for row in rows:
        previous = json.loads(row[0])
        if previous['data']['status'] != 'pending':
            continue
        observation = worker.provider.lookup(external_id=previous['data']['external_id'])
        if observation and auto_capture and observation['data']['status'] == 'pending':
            observation = worker.provider.capture(observation['data']['payment_id'])
        if observation:
            with worker.db:
                worker.observe(observation)
