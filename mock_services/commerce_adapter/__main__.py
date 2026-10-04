import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from commerce.adapter_client import AdapterClient
from .demo import run_demo
from .locking import worker_lock
from .provider import FakeProvider
from .storage import Store
from .worker import Worker


def serve(worker, port, interval):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/webhooks':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 262144:
                    raise ValueError('Invalid body length.')
                worker.webhook(self.rfile.read(length), self.headers.get('X-Fake-Timestamp', ''),
                               self.headers.get('X-Fake-Signature', ''))
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Invalid webhook')
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):
            logging.info('Webhook response: %s', args[1] if len(args) > 1 else '')

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

    # One event loop serializes receipts and observations. Loopback only for the fake backend.
    with HTTPServer(('127.0.0.1', port), WebhookHandler) as server:
        server.timeout = interval
        logging.info('Polling commands; fake webhook receiver on 127.0.0.1:%s/webhooks', port)
        while True:
            worker.tick()
            server.handle_request()


def main():
    parser = argparse.ArgumentParser(description='Reference commerce adapter (fake providers; test connections only).')
    sub = parser.add_subparsers(dest='action', required=True)
    demo = sub.add_parser('demo', help='Offline happy/duplicate/timeout walkthrough, no dependencies or credentials')
    demo.add_argument('--directory', default='/tmp/commerce-adapter-demo')
    for action in ('run', 'capture', 'inspect', 'retry-events'):
        cmd = sub.add_parser(action)
        cmd.add_argument('--connection', required=True)
        cmd.add_argument('--db', default='adapter.sqlite3')
        cmd.add_argument('--provider-db', default='provider.sqlite3')
        if action == 'run':
            cmd.add_argument('--base-url', required=True, help='HTTPS Studio Desk origin with /commerce suffix')
            cmd.add_argument('--once', action='store_true')
            cmd.add_argument('--port', type=int, default=8766)
            cmd.add_argument('--interval', type=float, default=2)
        if action == 'capture':
            cmd.add_argument('--payment-id', required=True)
            cmd.add_argument('--webhook-url', default='http://127.0.0.1:8766/webhooks')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.action == 'demo':
        run_demo(args.directory)
        return
    if args.action == 'capture':
        # Simulates the customer at the fake provider, using ONLY the provider DB.
        provider = FakeProvider(args.provider_db, args.connection)
        try:
            body, stamp, signature = provider.signed_capture(args.payment_id, os.environ['FAKE_WEBHOOK_SECRET'])
            request = Request(args.webhook_url, data=body, headers={'Content-Type': 'application/json',
                'X-Fake-Timestamp': stamp, 'X-Fake-Signature': signature})
            with urlopen(request, timeout=10) as response:
                print('Fake capture webhook:', response.status)
        finally:
            provider.close()
        return
    with worker_lock(Path(args.db)):
        store = Store(args.db, args.connection)
        try:
            if args.action == 'inspect':
                print(json.dumps(store.summary(), indent=2))
                return
            if args.action == 'retry-events':
                with store.db:
                    store.db.execute("UPDATE event_outbox SET state='pending', attempts=0, next_attempt=0 WHERE state='parked'")
                print('Parked events requeued with their original IDs and payloads.')
                return
            if args.interval <= 0:
                parser.error('--interval must be positive')
            client = AdapterClient(args.base_url, args.connection, os.environ['COMMERCE_ADAPTER_SECRET'])
            manifest = client.request('GET', 'manifest/')
            allowed = {'payment': {'payment.create', 'payment.reconcile'}, 'pos': {'order.submit', 'order.reconcile'}}
            if (manifest['environment'] != 'test' or manifest['connection_id'] != args.connection
                    or manifest['role'] not in allowed
                    or set(manifest['capabilities']) != allowed[manifest['role']]):
                parser.error('Use a test connection with exactly the documented payment or POS capabilities.')
            provider = FakeProvider(args.provider_db, args.connection)
            try:
                worker = Worker(store, provider, client, os.environ['FAKE_WEBHOOK_SECRET'])
                worker.tick() if args.once else serve(worker, args.port, args.interval)
            finally:
                provider.close()
        finally:
            store.close()


if __name__ == '__main__':
    main()
