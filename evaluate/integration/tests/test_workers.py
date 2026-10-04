"""Worker-lane fixes: pump-after-capture, catalog projection, command context, preflight."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone as dj_timezone

from evaluate.contracts.models import ExecutionIdentity
from evaluate.controls.context import activate, current


def _identity():
    return ExecutionIdentity(
        run_id="test-run", scenario_id="sessions:test",
        scenario_instance_id="instance", attempt=1,
    )


class FakeOutboxDb:
    def __init__(self, pending: bool = False):
        self.pending = pending

    def execute(self, sql, params=None):
        if "event_outbox" in sql and self.pending:
            return SimpleNamespace(fetchone=lambda: (1,))
        return SimpleNamespace(fetchone=lambda: None)


class FakeWorker:
    def __init__(self, connection_pk=1, pending_outbox: bool = False):
        self.ticks = 0
        self.client = SimpleNamespace(connection=SimpleNamespace(pk=connection_pk))
        self.db = FakeOutboxDb(pending_outbox)
        self.webhook = MagicMock()
        self.flush_events = MagicMock()
        self.provider = SimpleNamespace(
            timeout_creation=False,
            provider=SimpleNamespace(
                signed_capture=MagicMock(return_value=(b"{}", "1", "sig")),
            ),
        )
        self.webhook_secret = "secret"

    def tick(self):
        self.ticks += 1
        self.db.pending = False


@override_settings(EVALUATION_ENABLED=True)
class PumpAfterCaptureTests(SimpleTestCase):
    def test_pump_until_idle_claims_until_queues_drain(self):
        from evaluate.scenarios.runtime import LocalRuntime

        runtime = LocalRuntime("/tmp/eval-workers-test")
        lease = SimpleNamespace(handle="lease-handle", scenario_instance_id="instance")
        worker = FakeWorker()
        runtime.workers = {lease.handle: {"payment": worker, "pos": FakeWorker(connection_pk=2)}}
        runtime.provisioner = SimpleNamespace(owned=lambda l: (None, {}))
        pending = {"n": 2}

        def idle(lease_arg):
            pending["n"] -= 1
            return pending["n"] < 0

        with patch.object(LocalRuntime, "command_queues_idle", side_effect=idle):
            runtime.pump_until_idle(lease, bound=8)
        self.assertGreaterEqual(worker.ticks, 1)

    def test_payment_capture_pumps_until_idle_after_flush(self):
        from evaluate.scenarios.runtime import LocalRuntime

        runtime = LocalRuntime("/tmp/eval-workers-test")
        lease = SimpleNamespace(handle="lease-handle", scenario_instance_id="instance")
        worker = FakeWorker()
        runtime.workers = {lease.handle: {"payment": worker}}
        runtime.provisioner = SimpleNamespace(owned=lambda l: (None, {}))
        payment = SimpleNamespace(
            connection_id=1, pk=uuid4(), status="pending",
            refresh_from_db=MagicMock(),
        )

        def refresh():
            payment.status = "captured"

        payment.refresh_from_db.side_effect = refresh
        pumped = []

        with patch.object(LocalRuntime, "pump", lambda self, lease: pumped.append("pump")), \
             patch.object(LocalRuntime, "pump_until_idle", lambda self, lease, bound=16: pumped.append("until_idle")), \
             patch.object(LocalRuntime, "attached_workers", lambda self, lease: runtime.workers[lease.handle]):
            runtime.payment(lease, "capture", payment)
        self.assertIn("until_idle", pumped)
        worker.flush_events.assert_called_once()


@override_settings(EVALUATION_ENABLED=True)
class CatalogProjectionTests(TestCase):
    def test_sets_none_only_for_evaluation_owned_non_finite_tenants(self):
        from chatbot_core.models import TenantInfo
        from chatbot_core import knowledge_cache
        from evaluate.fixtures.provision import OWNER_KEY
        from evaluate.integration.apps import install_catalog_projection
        from orders.models import MenuItem, MenuCategory

        install_catalog_projection()

        unknown = TenantInfo.objects.create(
            slug="eval-unknown", display_name="eval-unknown", approval_status="APPROVED",
            meta={OWNER_KEY: {"lease": "l1", "setup": {"stock": "none"},
                              "assumptions": {"stock": {"kind": "none"}}}},
        )
        finite = TenantInfo.objects.create(
            slug="eval-finite", display_name="eval-finite", approval_status="APPROVED",
            meta={OWNER_KEY: {"lease": "l2", "setup": {"stock": "finite_local"},
                              "assumptions": {"stock": {"kind": "finite_local"}}}},
        )
        plain = TenantInfo.objects.create(
            slug="plain", display_name="plain", approval_status="APPROVED", meta={},
        )
        for tenant in (unknown, finite, plain):
            category = MenuCategory.objects.create(tenant=tenant, name="Drinks")
            MenuItem.objects.create(
                tenant=tenant, category_fk=category, name="Latte",
                quantity=0, is_available=True,
            )

        payload = knowledge_cache.generate_all_menu_payload()
        self.assertIsNone(payload[unknown.api_key]["Latte"]["available_quantity"])
        self.assertIsNone(payload[finite.api_key]["Latte"]["available_quantity"])
        self.assertEqual(payload[plain.api_key]["Latte"]["available_quantity"], 0)


@override_settings(EVALUATION_ENABLED=True)
class CommandContextTests(TestCase):
    def setUp(self):
        from chatbot_core.models import TenantInfo
        from chatbot_core.scope import session_identity
        from commerce.models import Location, Connection
        from django.contrib.sessions.backends.db import SessionStore
        from orders.models import Customer, ChatSession
        from evaluate.contracts.interfaces import Lease
        from evaluate.fixtures.provision import DjangoProvisioner, OWNER_KEY

        self.provisioner = DjangoProvisioner()
        self.identity = _identity()
        self.lease = Lease(str(uuid4()), self.identity.scenario_instance_id)
        self.tenant = TenantInfo.objects.create(
            slug="eval-cmd", display_name="eval-cmd", approval_status="APPROVED")
        self.customer = Customer.objects.create(tenant=self.tenant, name="QA Guest", phone="")
        self.browser = SessionStore()
        self.browser.create()
        self.namespace = "cafe:v2:" + session_identity(str(self.tenant.pk), "website", self.browser.session_key)
        self.browser[self.namespace] = {"customer_id": str(self.customer.pk), "basket": {"items": []}}
        self.browser.save()
        self.chat = ChatSession.objects.create(
            tenant=self.tenant, customer=self.customer, platform="website",
            session_id=self.browser.session_key, state={})
        self.owner = dict(
            lease=self.lease.handle, run_id=self.identity.run_id, scenario_id=self.identity.scenario_id,
            instance_id=self.identity.scenario_instance_id, attempt=1, lifecycle="ready",
            maps={"tenant": str(self.tenant.pk), "customer:active": str(self.customer.pk),
                  "session:active": str(self.chat.pk)},
            tenant_ids=[self.tenant.pk],
            browser_sessions=[self.browser.session_key], namespace=self.namespace,
            clock={"at": "2032-01-02T12:00:00+00:00", "timezone": "UTC"},
            application_command_contexts={},
        )
        self.provisioner.save_owner(self.tenant, self.owner)
        self.location = Location.objects.create(tenant=self.tenant, code="eval", name="eval")
        self.connection = Connection.objects.create(
            location=self.location, role="payment", provider="custom",
            account_id=str(uuid4()), environment="test", active=True,
            secret_ref="managed:" + str(uuid4()),
            capabilities=["payment.create"],
        )

    def test_command_effect_active_around_business_call(self):
        from commerce.models import Command
        from evaluate.controls.commerce import command_effect, command_created
        from evaluate.controls.ownership import context_for
        from evaluate.fixtures.provision import OWNER_KEY

        ctx, _ = context_for(self.lease, self.identity, "origin-req", self.provisioner)
        command = Command.objects.create(
            connection=self.connection, kind="payment.create",
            dedupe_key="pay:" + str(uuid4()), payload={"payment_id": str(uuid4())},
            available_at=dj_timezone.now(),
        )
        with activate(ctx):
            command_created(command)
        self.tenant.refresh_from_db()
        self.assertIn(str(command.pk), self.tenant.meta[OWNER_KEY]["application_command_contexts"])

        seen = []

        def business_effect():
            seen.append(current())

        with command_effect(command):
            business_effect()
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertEqual(seen[0].request_id, "origin-req")
        self.assertEqual(seen[0].lease_id, self.lease.handle)
        self.assertEqual(seen[0].tenant_id, str(self.tenant.pk))
        self.assertIsNone(current())

    def test_send_event_activates_originating_context_around_view(self):
        from evaluate.scenarios.runtime import SignedLocalClient

        connection = SimpleNamespace(pk=uuid4(), environment="test")
        client = SignedLocalClient(connection)
        event = {"data": {"type": "payment.updated", "payment_id": str(uuid4())}}
        entered = []

        @contextmanager
        def tracking_effect(conn, payload):
            entered.append(payload)
            yield SimpleNamespace(request_id="tracked")

        fake_response = SimpleNamespace(status_code=200, content=b'{"status":"processed"}')
        with patch("evaluate.controls.commerce.originating_effect", tracking_effect), \
             patch("commerce.credentials.adapter_secret", return_value="a" * 64), \
             patch("commerce.api.signature", return_value="b" * 64), \
             patch("commerce.api.events", return_value=fake_response):
            result = client.send_event(event)
        self.assertEqual(entered, [event])
        self.assertEqual(result["status"], "processed")


@override_settings(EVALUATION_ENABLED=True, LOCATION_PROVIDER="emulator")
class PreflightTests(SimpleTestCase):
    def test_requires_postgresql_and_redis_without_isolation_urls(self):
        from evaluate.integration.preflight import run_preflight

        with patch.dict("os.environ", {}, clear=False):
            # Ensure isolation env vars are absent for this check.
            env = {k: v for k, v in __import__("os").environ.items()
                   if k not in ("EVALUATION_DATABASE_URL", "EVALUATION_REDIS_URL")}
            with patch.dict("os.environ", env, clear=True):
                report = run_preflight(
                    base_url="http://127.0.0.1:8000",
                    evidence_directory="/tmp/studio-eval-evidence",
                    skip_location=True,
                )
        self.assertIn("checks", report)
        self.assertFalse(report["checks"]["database_postgresql"]["ok"])
        self.assertFalse(report["checks"]["database_migrations"]["ok"])
        self.assertFalse(report["checks"]["cache_redis"]["ok"])
        self.assertFalse(report["valid"])
        codes = {row["code"] for row in report["blockers"]}
        self.assertIn("database_postgresql", codes)
        self.assertIn("cache_redis", codes)
        self.assertIn("adapter_https", codes)

    def test_url_presence_cannot_attest_postgresql_or_adapter(self):
        from evaluate.integration.preflight import run_preflight

        with patch.dict("os.environ", {
            "EVALUATION_DATABASE_URL": "postgres://u:p@127.0.0.1:5433/studio_eval",
            "EVALUATION_REDIS_URL": "redis://127.0.0.1:6380/0",
            "EVALUATION_ADAPTER_URL": "https://127.0.0.1:8443",
        }, clear=False):
            report = run_preflight(
                base_url="http://127.0.0.1:8000",
                evidence_directory="/tmp/studio-eval-evidence",
                skip_location=True,
            )
        # URL presence cannot attest that the configured SQLite test DB is PostgreSQL,
        # or that an HTTPS adapter/worker exists at that address.
        self.assertFalse(report["checks"]["database_postgresql"]["ok"])
        self.assertFalse(report["checks"]["adapter_https"]["ok"])
        self.assertFalse(report["valid"])

@override_settings(EVALUATION_ENABLED=True)
class RemoteEventContextTests(CommandContextTests):
    def test_inbox_processing_restores_context_without_client_context(self):
        from commerce.models import Command
        from commerce.events import receive
        from evaluate.controls.commerce import command_created
        from evaluate.controls.ownership import context_for
        from evaluate.fixtures.provision import OWNER_KEY

        payment_id = str(uuid4())
        command = Command.objects.create(connection=self.connection, kind='payment.create',
            dedupe_key='pay:' + payment_id, payload={'payment_id': payment_id}, available_at=dj_timezone.now())
        ctx, _ = context_for(self.lease, self.identity, 'remote-origin', self.provisioner)
        with activate(ctx):
            command_created(command)
        self.assertIsNone(current())
        event = {'schema_version': 1, 'event_id': str(uuid4()), 'occurred_at': dj_timezone.now().isoformat(),
                 'data': {'type': 'payment.updated', 'payment_id': payment_id, 'external_id': 'provider-receipt',
                          'sequence': 1, 'currency': 'INR', 'status': 'captured', 'captured_minor': 100}}
        following = []

        def capture_effect(connection, payload):
            self.assertEqual(current().request_id, 'remote-origin')
            row = Command.objects.create(connection=connection, kind='order.submit', dedupe_key='pos:' + payment_id,
                                         payload={}, available_at=dj_timezone.now())
            command_created(row)
            following.append(row)

        with patch('commerce.events.payment_update', capture_effect):
            inbox = receive(self.connection, event)
        self.assertEqual(inbox.status, 'processed')
        self.tenant.refresh_from_db()
        self.assertIn(str(following[0].pk), self.tenant.meta[OWNER_KEY]['application_command_contexts'])
        self.assertIsNone(current())


class ProjectionMoneyTests(SimpleTestCase):
    def test_major_units_do_not_depend_on_python_type(self):
        from decimal import Decimal
        from evaluate.controls.inspection import as_minor
        self.assertEqual([as_minor(x) for x in (460, '460', Decimal('460'), 460.0)], [46000] * 4)
        self.assertEqual(as_minor(460, exponent=0), 460)
        self.assertIsNone(as_minor(True))
