import argparse
import json
import logging
import os
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError

from commerce.adapter_client import AdapterClient
from commerce.adapters.json_menu import send_snapshot
from .catalog import DEFAULT_SEED, load_catalog
from .client import HTTPProvider, MockClient, refresh_payments
from .commerce_adapter.locking import worker_lock
from .commerce_adapter.storage import Store
from .commerce_adapter.worker import Worker
from .server import make_server


def validate_manifest(manifest, connection, role=None):
    if manifest['environment'] != 'test' or manifest['connection_id'] != connection:
        raise ValueError('A matching test connection is required.')
    if role:
        required = {'payment': {'payment.create', 'payment.reconcile'},
                    'pos': {'order.submit', 'order.reconcile'}}[role]
        allowed = required | ({'catalog.write'} if role == 'pos' else set())
        capabilities = set(manifest['capabilities'])
        if manifest['role'] != role or not required <= capabilities or capabilities - allowed:
            raise ValueError('Connection has unsupported role/capabilities; see mock_services/README.md.')
    elif ('catalog.write' not in manifest['capabilities'] or not manifest['menu_source']['is_authority']):
        raise ValueError('The test connection must be the external catalog.write authority.')


def run_adapter(args):
    client = AdapterClient(args.base_url, args.connection, os.environ['COMMERCE_ADAPTER_SECRET'])
    validate_manifest(client.request('GET', 'manifest/'), args.connection, args.role)
    provider = HTTPProvider(MockClient(args.mock_url, args.timeout), args.connection, args.role)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with worker_lock(args.db), closing(Store(args.db, args.connection)) as store:
        worker = Worker(store, provider, client, '')
        while True:
            try:
                worker.tick()
                refresh_payments(worker, args.auto_capture)
                worker.flush_events()
            except (OSError, URLError) as exc:
                logging.warning('Adapter cycle interrupted: %s', type(exc).__name__)
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(args.interval)


def sync_menu(args):
    client = AdapterClient(args.base_url, args.connection, os.environ['COMMERCE_ADAPTER_SECRET'])
    manifest = client.request('GET', 'manifest/')
    validate_manifest(manifest, args.connection)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    # Save before delivery. A failed/ambiguous request retries the exact payload.
    pending = args.state_dir / (args.connection + '-menu-pending.json')
    with worker_lock(pending):
        if pending.exists():
            snapshot = json.loads(pending.read_text())
            if snapshot['source_generation'] != manifest['menu_source']['generation']:
                raise ValueError('Menu authority changed; archive the pending snapshot before exporting again.')
        else:
            snapshot = MockClient(args.mock_url, args.timeout).request('POST',
                '/v1/accounts/' + args.connection + '/menu/snapshots',
                dict(source_generation=manifest['menu_source']['generation'],
                     after_sequence=manifest['menu_source']['sequence']))
            temporary = pending.with_suffix('.tmp')
            with temporary.open('w') as output:
                json.dump(snapshot, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(pending)
        result = send_snapshot(client, snapshot)
        if result.get('status') not in ('applied', 'unchanged'):
            raise ValueError('Unexpected menu response; pending snapshot retained: ' + str(result))
        pending.replace(args.state_dir / (args.connection + '-menu-last.json'))
        print(json.dumps(result, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local mock menu, payment and POS integrations (test only).')
    sub = parser.add_subparsers(dest='action', required=True)
    serve = sub.add_parser('serve', help='Start all mock provider HTTP endpoints')
    serve.add_argument('--port', type=int, default=9080)
    serve.add_argument(
        '--bind', default='127.0.0.1',
        help='Listen address (default loopback; use 0.0.0.0 inside Compose)',
    )
    serve.add_argument('--db', type=Path, default=Path('mock_services/.state/provider.sqlite3'))
    serve.add_argument('--seed', type=Path, default=DEFAULT_SEED)
    for name in ('adapter', 'sync-menu'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--base-url', required=True, help='HTTPS Studio Desk URL ending in /commerce')
        cmd.add_argument('--connection', required=True)
        cmd.add_argument('--mock-url', default='http://127.0.0.1:9080')
        cmd.add_argument('--timeout', type=float, default=15)
        if name == 'adapter':
            cmd.add_argument('--role', choices=['payment', 'pos'], required=True)
            cmd.add_argument('--db', type=Path, required=True, help='Separate persistent DB for each connection')
            cmd.add_argument('--auto-capture', action='store_true')
            cmd.add_argument('--interval', type=float, default=1)
            cmd.add_argument('--once', action='store_true')
        else:
            cmd.add_argument('--state-dir', type=Path, default=Path('mock_services/.state'))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        if args.action == 'serve':
            catalog = load_catalog(args.seed)
            args.db.parent.mkdir(parents=True, exist_ok=True)
            with make_server((args.bind, args.port), args.db, catalog) as server:
                logging.info(
                    'Mock menu/payment/POS at http://%s:%s (%s items)',
                    args.bind, server.server_port, len(catalog['items']),
                )
                server.serve_forever()
        else:
            from uuid import UUID
            args.connection = str(UUID(args.connection))
            if args.timeout <= 0 or not args.timeout < float('inf'):
                raise ValueError('--timeout must be finite and positive.')
            if args.action == 'adapter':
                if args.interval <= 0 or not args.interval < float('inf'):
                    raise ValueError('--interval must be finite and positive.')
                if args.auto_capture and args.role != 'payment':
                    raise ValueError('--auto-capture is only valid for the payment adapter.')
                run_adapter(args)
            else:
                sync_menu(args)
    except KeyboardInterrupt:
        pass
    except (ValueError, KeyError, OSError, URLError) as exc:
        parser.exit(1, 'Mock service error: %s\n' % exc)


if __name__ == '__main__':
    main()
