"""Evaluation-only in-process worker lane using the shared mock adapter interfaces.

The turn context must enclose an in-process website request. It is deliberately
not an attestation for an independently running HTTP server. One turn per process;
parallel workers use separate processes and separate lease/provider databases.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime
from importlib import import_module
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
import ipaddress
import json
import os
import re
import secrets
import ssl
import time
from uuid import UUID

from django.test import RequestFactory
from evaluate.contracts.interfaces import Blocked
from evaluate.fixtures.definitions import address, get_fixture
from evaluate.scenarios.review import CAPABILITIES

_TURN_LOCK = Lock()
_PUMP_BOUND = 16
_LOOPBACK_NAMES = frozenset({'127.0.0.1', 'localhost', '::1'})


def _is_local_adapter_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() in _LOOPBACK_NAMES | {'adapter_https'}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class SignedLocalClient:
    """AdapterClient interface through real HMAC-authenticated Django API views.

    Provider credentials and adapter signatures never enter evaluation evidence.
    This only changes transport; signature validation and engine receipts run.
    """
    def __init__(self, connection):
        self.connection = connection

    def request(self, method, endpoint, payload=None, *, effect=None):
        from commerce import api
        from commerce.credentials import adapter_secret
        from evaluate.controls.commerce import originating_effect, effect_scope_for_command_id
        routes = {'commands/claim/': api.commands, 'events/': api.events}
        kwargs = {}
        view = routes.get(endpoint)
        if endpoint.startswith('commands/') and endpoint.endswith('/ack/'):
            view = api.ack
            kwargs['command_id'] = UUID(endpoint.split('/')[1])
        if view is None:
            raise Blocked('Unsupported local adapter endpoint')
        path = '/commerce/v1/connections/' + str(self.connection.pk) + '/' + endpoint
        body = json.dumps(payload or {}, separators=(',', ':')).encode()
        stamp = str(int(time.time()))
        secret = adapter_secret(self.connection)
        if not secret or self.connection.environment != 'test':
            raise Blocked('Test adapter credentials unavailable')
        request = RequestFactory().generic(method, path, body, content_type='application/json',
            HTTP_X_COMMERCE_TIMESTAMP=stamp,
            HTTP_X_COMMERCE_SIGNATURE=api.signature(secret, stamp, method, path, body))
        with ExitStack() as stack:
            if effect is not None:
                stack.enter_context(effect)
            elif endpoint == 'events/' and payload is not None:
                stack.enter_context(originating_effect(self.connection, payload))
            elif 'command_id' in kwargs:
                stack.enter_context(effect_scope_for_command_id(kwargs['command_id']))
            response = view(request, self.connection.pk, **kwargs)
        if response.status_code not in (200, 202):
            raise Blocked('Authenticated test adapter request was rejected')
        return json.loads(response.content)

    def claim(self):
        return self.request('POST', 'commands/claim/', {})['commands']

    def acknowledge(self, command, outcome, error_code=''):
        from evaluate.controls.commerce import effect_scope_for_command_id
        return self.request(
            'POST', 'commands/' + command['command_id'] + '/ack/',
            dict(lease_token=command['lease_token'], outcome=outcome, error_code=error_code),
            effect=effect_scope_for_command_id(command['command_id']),
        )

    def send_event(self, event):
        return self.request('POST', 'events/', event)


class SignedHttpsClient:
    """Real HTTPS adapter transport for a trusted local evaluation origin.

    Used when EVALUATION_ADAPTER_URL is an HTTPS loopback or Compose adapter_https origin. Certificate
    verification stays enabled; optionally load EVALUATION_ADAPTER_CA for a
    local CA. Other non-loopback hosts are refused.
    """
    def __init__(self, connection, base_url: str):
        self.connection = connection
        self.base_url = _validated_adapter_origin(base_url)

    def request(self, method, endpoint, payload=None, *, effect=None):
        from commerce.credentials import adapter_secret
        from commerce import api
        from evaluate.controls.commerce import originating_effect, effect_scope_for_command_id
        secret = adapter_secret(self.connection)
        if not secret or self.connection.environment != 'test':
            raise Blocked('Test adapter credentials unavailable')
        url = commerce_adapter_url(self.base_url, self.connection.pk, endpoint)
        parsed = urlsplit(url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        body = json.dumps(payload or {}, separators=(',', ':')).encode()
        stamp = str(int(time.time()))
        signature = api.signature(secret, stamp, method, path, body)
        http_request = Request(
            url, data=body if method != 'GET' else None, method=method,
            headers={
                'Content-Type': 'application/json',
                'X-Commerce-Timestamp': stamp,
                'X-Commerce-Signature': signature,
            },
        )
        context = _adapter_ssl_context(parsed.hostname)

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with ExitStack() as stack:
            if effect is not None:
                stack.enter_context(effect)
            elif endpoint == 'events/' and payload is not None:
                stack.enter_context(originating_effect(self.connection, payload))
            elif endpoint.startswith('commands/') and endpoint.endswith('/ack/'):
                stack.enter_context(effect_scope_for_command_id(UUID(endpoint.split('/')[1])))
            opener = build_opener(HTTPSHandler(context=context), NoRedirect())
            with opener.open(http_request, timeout=15) as response:
                if response.status not in (200, 202):
                    raise Blocked('Authenticated test adapter request was rejected')
                return json.load(response)

    def claim(self):
        return self.request('POST', 'commands/claim/', {})['commands']

    def acknowledge(self, command, outcome, error_code=''):
        from evaluate.controls.commerce import effect_scope_for_command_id
        return self.request(
            'POST', 'commands/' + command['command_id'] + '/ack/',
            dict(lease_token=command['lease_token'], outcome=outcome, error_code=error_code),
            effect=effect_scope_for_command_id(command['command_id']),
        )

    def send_event(self, event):
        return self.request('POST', 'events/', event)


def commerce_adapter_url(base_url: str, connection_id, endpoint: str) -> str:
    """HTTPS adapter path. Django mounts commerce routes under /commerce/."""
    root = base_url.rstrip('/') + '/commerce/v1/connections/' + quote(str(connection_id), safe='') + '/'
    return root + endpoint


def _validated_adapter_origin(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise Blocked('Evaluation adapter URL must be an HTTPS origin without credentials')
    if parsed.query or parsed.fragment:
        raise Blocked('Evaluation adapter URL must not carry query or fragment')
    if not _is_local_adapter_host(parsed.hostname):
        raise Blocked('Evaluation adapter URL must target loopback or the development adapter_https service')
    return base_url.rstrip('/')


def _adapter_ssl_context(hostname: str | None) -> ssl.SSLContext:
    """Verify peers with the default trust store; optional local CA for development."""
    context = ssl.create_default_context()
    ca_file = os.environ.get('EVALUATION_ADAPTER_CA', '').strip()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    if not _is_local_adapter_host(hostname):
        # Unrecognized services must be refused before connecting.
        raise Blocked('Evaluation adapter URL must target loopback or the development adapter_https service')
    return context


def adapter_client_for(connection):
    """Prefer SignedLocalClient; use HTTPS when EVALUATION_ADAPTER_URL is set."""
    base = os.environ.get('EVALUATION_ADAPTER_URL', '').strip()
    if not base:
        return SignedLocalClient(connection)
    return SignedHttpsClient(connection, base)


class FaultProvider:
    """Before-commit timeout is distinct from the mock's after-commit timeout."""
    def __init__(self, provider):
        self.provider = provider
        self.account = provider.account
        self.creation_fault = None

    def create(self, command, **kwargs):
        if self.creation_fault == 'before_commit' and command['type'] == 'payment.create':
            raise TimeoutError('Evaluation before-commit payment timeout')
        if self.creation_fault == 'after_commit' and command['type'] == 'payment.create':
            kwargs['timeout_after_commit'] = True
        return self.provider.create(command, **kwargs)

    def lookup(self, **kwargs):
        return self.provider.lookup(**kwargs)


class LocalRuntime:
    def __init__(self, state_directory):
        self.directory = Path(state_directory).resolve()
        self.workers = {}
        self.provisioner = None

    def capabilities(self):
        return frozenset(CAPABILITIES)

    def attest(self, scenario):
        # Check imports required by the provider interface before creating rows.
        from mock_services.commerce_adapter.provider import FakeProvider  # noqa: F401
        from mock_services.commerce_adapter.worker import Worker  # noqa: F401
        from mock_services.commerce_adapter.storage import Store  # noqa: F401
        if scenario.setup.variant_name not in (None, 'QA standard'):
            raise Blocked('This dataset adapter requires QA standard')
        if scenario.setup.payment == 'fake_adapter' and scenario.setup.stock != 'finite_local':
            raise Blocked('Commerce checkout requires the reviewed finite-stock exception')
        if scenario.setup.stock == 'finite_local' and scenario.setup.payment != 'fake_adapter':
            raise Blocked('Finite stock is only reviewed for commerce checkout')
        if scenario.setup.lead_minutes not in (None, scenario.setup.preparation_minutes):
            raise Blocked('Application scheduling lead is preparation_minutes')
        for action in scenario.actions:
            if action.operation.kind not in self.capabilities():
                raise Blocked('Unsupported typed action')
            if action.operation.kind == 'seed_fixture':
                fixture = get_fixture(action.operation.fixture_id, action.operation.fixture_hash)
                labels = [address(key)['label'] for key in fixture.addresses]
                if len(labels) != len(set(labels)):
                    raise Blocked('Dataset requires duplicate saved-address labels, forbidden by the application unique constraint')

    def prepare(self, lease, binding, scenario):
        self.open_workers(lease, binding, create=True)

    def attach(self, lease):
        """Resume the same owned provider lane after an inspect/provision process exits."""
        binding = self.provisioner.binding(lease)
        if lease.handle in self.workers:
            return
        self.open_workers(lease, binding, create=False)
        _, owner = self.provisioner.owned(lease)
        worker = self.workers[lease.handle].get('payment')
        if worker:
            worker.provider.creation_fault = owner.get('payment_fault')

    def open_workers(self, lease, binding, *, create):
        from commerce.models import Connection
        from mock_services.commerce_adapter.provider import FakeProvider
        from mock_services.commerce_adapter.worker import Worker
        from mock_services.commerce_adapter.storage import Store
        folder = self.directory / str(UUID(lease.handle))
        connections = list(Connection.objects.filter(location__tenant=binding['tenant']).order_by('pk'))
        marker = {'lease': lease.handle, 'instance': lease.scenario_instance_id,
                  'connections': [str(c.pk) for c in connections]}
        if create:
            folder.mkdir(parents=True, exist_ok=False)
            (folder / 'ownership.json').write_text(json.dumps(marker))
        else:
            self.validate_files(lease)
            if not folder.is_dir() or json.loads((folder / 'ownership.json').read_text()) != marker:
                raise Blocked('Cannot attach an unowned provider directory')
        # Check every receipt before opening any worker. A failed attach must
        # never leave a partial registry that a second call mistakes for ready.
        for conn in connections:
            adapter_path = folder / (str(conn.pk) + '-adapter.sqlite3')
            provider_path = folder / (str(conn.pk) + '-provider.sqlite3')
            if not create and any(p.is_symlink() or not p.is_file() for p in (adapter_path, provider_path)):
                raise Blocked('Owned provider receipts are missing; refusing to recreate them')
        opened = {}
        try:
            with ExitStack() as pending:
                for conn in connections:
                    store = Store(folder / (str(conn.pk) + '-adapter.sqlite3'), str(conn.pk))
                    pending.callback(store.close)
                    raw_provider = FakeProvider(folder / (str(conn.pk) + '-provider.sqlite3'), str(conn.pk))
                    pending.callback(raw_provider.close)
                    provider = FaultProvider(raw_provider)
                    opened[conn.role] = Worker(store, provider, adapter_client_for(conn), secrets.token_hex(32))
                self.workers[lease.handle] = opened
                pending.pop_all()
        except BaseException:
            self.workers.pop(lease.handle, None)
            raise

    def release(self, lease):
        for worker in self.workers.pop(lease.handle, {}).values():
            worker.store.close()
            worker.provider.provider.close()
        # Provider receipts survive failures and normal release for audit. Explicit
        # cleanup_files removes only UUID directories with matching ownership.

    def validate_files(self, lease):
        folder = self.directory / str(UUID(lease.handle))
        if not folder.exists():
            return
        if folder.is_symlink() or (folder / 'ownership.json').is_symlink():
            raise Blocked('Provider directory ownership mismatch')
        marker = json.loads((folder / 'ownership.json').read_text())
        if (set(marker) != {'lease', 'instance', 'connections'} or marker['lease'] != lease.handle or
                marker['instance'] != lease.scenario_instance_id):
            raise Blocked('Provider directory ownership mismatch')
        allowed = {'ownership.json'} | {str(UUID(c)) + '-' + role + '.sqlite3' + suffix
            for c in marker['connections'] for role in ('adapter', 'provider') for suffix in ('', '-wal', '-shm')}
        for entry in folder.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.name not in allowed:
                raise Blocked('Unknown provider directory contents; cleanup refused')
        return folder

    def cleanup_files(self, lease):
        folder = self.validate_files(lease)
        if folder is None:
            return
        for entry in folder.iterdir():
            entry.unlink()
        folder.rmdir()

    def pump(self, lease):
        workers = self.attached_workers(lease)
        for worker in workers.values():
            worker.tick()
            if worker.db.execute("SELECT 1 FROM event_outbox WHERE state != 'sent'").fetchone():
                raise Blocked('Provider event has not been processed by commerce')

    def command_queues_idle(self, lease) -> bool:
        """True when this lease's POS/payment command queues have nothing to claim."""
        from commerce.models import Command
        workers = self.attached_workers(lease)
        connection_ids = [worker.client.connection.pk for worker in workers.values()]
        if Command.objects.filter(
                connection_id__in=connection_ids, status__in=('pending', 'leased')).exists():
            return False
        for worker in workers.values():
            if worker.db.execute("SELECT 1 FROM event_outbox WHERE state != 'sent'").fetchone():
                return False
        return True

    def pump_until_idle(self, lease, *, bound: int = _PUMP_BOUND) -> None:
        """Drive workers until command queues are idle or the tick bound is hit."""
        for _ in range(bound):
            if self.command_queues_idle(lease):
                return
            self.pump(lease)
        if not self.command_queues_idle(lease):
            raise Blocked('Provider command queues did not become idle')

    def payment(self, lease, operation, payment=None):
        worker = self.attached_workers(lease).get('payment')
        if worker is None:
            raise Blocked('No attached test payment worker; reconnect its durable lane first')
        if operation in {'timeout_creation', 'timeout_after_creation'}:
            worker.provider.creation_fault = 'before_commit' if operation == 'timeout_creation' else 'after_commit'
            return
        if operation == 'restore_and_reconcile':
            worker.provider.creation_fault = None
            # Lookup every ambiguous receipt before any new creation. Do not run
            # the application-wide reconcile command (it touches other tenants).
            from commerce.models import Payment
            from commerce.services import enqueue
            for row in Payment.objects.filter(connection=worker.client.connection, status='pending'):
                enqueue(row.connection, 'payment.reconcile', 'eval-reconcile:' + str(row.pk), row.accepted_order,
                        {'payment_id': str(row.pk), 'external_id': row.external_id})
            self.pump(lease)
            return
        if payment is None:
            raise Blocked('Payment event requires an active owned payment')
        if payment.connection_id != worker.client.connection.pk:
            raise Blocked('Payment does not belong to this lease provider connection')
        self.pump(lease)
        payment.refresh_from_db()
        if operation == 'capture':
            body, stamp, signature = worker.provider.provider.signed_capture(str(payment.pk), worker.webhook_secret)
            worker.webhook(body, stamp, signature)
        elif operation in ('fail', 'cancel'):
            # The task-A Provider adds reviewed terminal transitions to FakeProvider.
            from mock_services.provider import Provider
            provider = Provider(self.directory / lease.handle / (str(payment.connection_id) + '-provider.sqlite3'), str(payment.connection_id))
            try:
                observation = provider.transition_payment(str(payment.pk), 'failed' if operation == 'fail' else 'cancelled')
                with worker.db:
                    worker.observe(observation)
            finally:
                provider.close()
        else:
            raise Blocked('Unsupported payment operation')
        worker.flush_events()
        payment.refresh_from_db()
        expected = {'capture': 'captured', 'fail': 'failed', 'cancel': 'cancelled'}[operation]
        if payment.status != expected:
            raise Blocked('Authenticated provider event did not produce expected payment state')
        # Capture enqueues POS work that flush_events does not claim; drain it.
        self.pump_until_idle(lease)

    def attached_workers(self, lease):
        self.provisioner.owned(lease)
        if lease.handle not in self.workers:
            raise Blocked('Provider lane is detached; attach its durable receipts before execution')
        return self.workers[lease.handle]

    @contextmanager
    def turn(self, lease):
        if self.provisioner is None:
            raise Blocked('Runtime is not bound to the provisioning owner')
        if not _TURN_LOCK.acquire(blocking=False):
            raise Blocked('Concurrent process-wide runtime controls require separate worker processes')
        try:
            self.attached_workers(lease)
            tenant, owner = self.provisioner.owned(lease)
            business_now = datetime.fromisoformat(owner['clock']['at'])
            location = import_module('chatbot_core.logic.cafe.intent_handler.location_based')
            checkout = import_module('chatbot_core.logic.cafe.checkout')
            with ExitStack() as stack:
                stack.enter_context(patch.object(checkout, 'timezone', SimpleNamespace(now=lambda: business_now)))
                # MenuItem.quantity is a mandatory legacy integer, not the
                # commerce stock ledger. Never expose its placeholder as live
                # inventory in the evaluation runtime catalog projection.
                knowledge = import_module('chatbot_core.knowledge_cache')
                generate_menu = knowledge.generate_all_menu_payload
                def synthetic_menu(api_key=None):
                    if api_key != tenant.api_key:
                        raise Blocked('Runtime catalog hook received another tenant')
                    result = generate_menu(api_key)
                    for item in result.get(api_key, {}).values():
                        item['available_quantity'] = None
                    return result
                stack.enter_context(patch.object(knowledge, 'generate_all_menu_payload', synthetic_menu))
                def coverage(subject, pin):
                    if subject.pk != tenant.pk:
                        raise Blocked('Runtime hook received another tenant')
                    return pin in (tenant.meta.get('serviceable_pincodes') or []) if owner['lookup']['coverage'] == 'success' else None
                stack.enter_context(patch.object(location, 'verify_delivery_pincode', coverage))
                if owner['lookup']['classification'] == 'timeout':
                    classifier = import_module('chatbot_core.logic.cafe.prompts.normalize_and_classify')
                    # Bypass only this operation's cache, including warm entries.
                    # Other response caches and their telemetry stay intact.
                    stack.enter_context(patch.object(classifier, '_cached_proposal', return_value=None))
                    stack.enter_context(patch.object(classifier, 'structured_chain', side_effect=TimeoutError('Evaluation classification timeout')))
                yield
            self.pump(lease)
        finally:
            _TURN_LOCK.release()
