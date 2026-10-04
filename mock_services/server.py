"""Threaded loopback HTTP service with deterministic fault injection."""
import json
import logging
import sqlite3
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from .faults import Faults
from .location import LocationEmulator
from .location import routes as location_routes
from .provider import Provider


def make_server(address, database, catalog):
    if address[0] not in ('127.0.0.1', 'localhost', '0.0.0.0', '::'):
        raise ValueError('Mock services must bind to loopback or all-interfaces for containers.')
    faults = Faults()
    emulator = LocationEmulator(database)
    # Initialize tables before serving concurrent requests.
    with closing(Provider(database, '00000000-0000-0000-0000-000000000000')):
        pass

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            logging.debug('Mock HTTP: %s', args[1] if len(args) > 1 else '')

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 < length <= 262144:
                raise ValueError('Expected a JSON body of at most 256 KiB.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object.')
            return payload

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            try:
                self.route()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except KeyError as exc:
                self.reply(400, {'error': 'Missing field: ' + str(exc)})
            except LookupError as exc:
                self.reply(404, {'error': str(exc)})
            except (ValueError, TypeError, AttributeError) as exc:
                self.reply(400, {'error': str(exc)})
            except sqlite3.OperationalError:
                self.reply(503, {'error': 'mock_database_busy'})

        def route(self):
            url = urlsplit(self.path)
            parts = url.path.strip('/').split('/')
            if self.command == 'GET' and url.path == '/health':
                return self.reply(200, {'status': 'ok', 'environment': 'test'})
            if location_routes.is_location_path(parts):
                decision = location_routes.route(emulator, self.command, parts, url.query, self.body)
                time.sleep(decision.wait_seconds)
                return self.reply(decision.status, decision.payload)
            if parts[:1] == ['admin']:
                return self.admin(parts)
            if self.command == 'GET' and url.path == '/v1/menu':
                fail, _ = faults.take('menu')
                return self.reply(503 if fail else 200, {'error': 'injected_failure'} if fail else catalog)
            if len(parts) < 4 or parts[:2] != ['v1', 'accounts']:
                return self.reply(404, {'error': 'unknown_endpoint'})
            account = str(UUID(parts[2]))
            resource = parts[3]
            service = {'payments': 'payment', 'orders': 'pos', 'menu': 'menu'}.get(resource)
            if service is None:
                return self.reply(404, {'error': 'unknown_resource'})
            creating = self.command == 'POST' and len(parts) == 4 and resource != 'menu'
            fail, lost = faults.take(service, creating, account)
            if fail:
                return self.reply(503, {'error': 'injected_failure'})
            with closing(Provider(database, account)) as provider:
                if self.command == 'POST' and parts[3:] == ['menu', 'snapshots']:
                    payload = self.body()
                    return self.reply(200, provider.export_menu(catalog, payload['source_generation'], payload.get('after_sequence', 0)))
                if creating:
                    command = self.body()
                    expected = 'payment.create' if resource == 'payments' else 'order.submit'
                    if command.get('type') != expected or command.get('schema_version') != 1:
                        raise ValueError('Wrong command type or schema version.')
                    if not isinstance(command.get('idempotency_key'), str) or not command['idempotency_key']:
                        raise ValueError('idempotency_key is required.')
                    data = command['data']
                    if expected == 'payment.create':
                        UUID(data['payment_id'])
                        if type(data['amount_minor']) is not int or data['amount_minor'] <= 0:
                            raise ValueError('amount_minor must be a positive integer.')
                        if (not isinstance(data['currency'], str) or len(data['currency']) != 3
                                or not data['currency'].isascii() or not data['currency'].isalpha()
                                or not data['currency'].isupper()):
                            raise ValueError('currency must be a three-letter uppercase code.')
                    try:
                        result = provider.create(command, timeout_after_commit=lost)
                    except TimeoutError:
                        return self.reply(504, {'error': 'timeout_after_commit'})
                    except ValueError as exc:
                        return self.reply(409, {'error': str(exc)})
                    return self.reply(200, result)
                if self.command == 'GET' and len(parts) == 4 and resource != 'menu':
                    query = parse_qs(url.query)
                    if not query:
                        return self.reply(200, {'resources': provider.resources(service if service == 'payment' else 'order')})
                    result = provider.lookup(key=query.get('key', [None])[0], external_id=query.get('external_id', [None])[0])
                    if result and result['data']['type'] != ('payment.updated' if resource == 'payments' else 'order.updated'):
                        result = None
                    return self.reply(200, {'resource': result})
                if self.command == 'POST' and len(parts) == 6 and resource == 'payments':
                    expected = self.body()
                    status = {'capture': 'captured', 'fail': 'failed', 'cancel': 'cancelled'}.get(parts[5])
                    if status is None:
                        raise ValueError('Unknown payment action.')
                    try:
                        return self.reply(200, provider.transition_payment(parts[4], status, expected=expected))
                    except ValueError as exc:
                        return self.reply(409, {'error': str(exc)})
            return self.reply(404, {'error': 'unknown_endpoint'})

        def admin(self, parts):
            """Process-local fault controls: global (legacy, shared) or per mock account."""
            if parts == ['admin', 'state'] and self.command == 'GET':
                return self.reply(200, faults.summary())
            if parts == ['admin', 'faults'] and self.command == 'POST':
                faults.configure(self.body())
                return self.reply(200, faults.summary())
            if len(parts) == 4 and parts[1] == 'accounts':
                account = str(UUID(parts[2]))
                if parts[3] == 'faults' and self.command == 'POST':
                    faults.configure(self.body(), account=account)
                    return self.reply(200, faults.summary(account))
                if parts[3] in ('faults', 'state') and self.command == 'GET':
                    summary = faults.summary(account)
                    if parts[3] == 'state':
                        with closing(Provider(database, account)) as provider:
                            summary['resources'] = provider.resource_summary()
                    return self.reply(200, summary)
            return self.reply(404, {'error': 'unknown_endpoint'})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128

    return Server(address, Handler)
