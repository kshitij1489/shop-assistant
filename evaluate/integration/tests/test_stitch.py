"""Django checks for credentials, the control router and the pending question."""
import json
from uuid import uuid4

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, override_settings

from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.contracts.models import Clock, ExecutionIdentity, FreezeClock, LookupControl


def _identity():
    return ExecutionIdentity(
        run_id="test-run", scenario_id="sessions:test", scenario_instance_id="instance", attempt=1)


@override_settings(EVALUATION_ENABLED=True, LOCATION_PROVIDER="emulator", EVALUATION_LOCATION_PROVIDER="emulator")
class StitchTests(TestCase):
    def setUp(self):
        from chatbot_core.models import TenantInfo
        from chatbot_core.scope import session_identity
        from orders.models import ChatSession, Customer
        from evaluate.fixtures.provision import DjangoProvisioner

        self.provisioner = DjangoProvisioner()
        self.identity = _identity()
        self.lease = Lease(str(uuid4()), self.identity.scenario_instance_id)
        self.tenant = TenantInfo.objects.create(
            slug="eval-stitch", display_name="eval-stitch", approval_status="APPROVED")
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
            browser_namespaces={self.browser.session_key: self.namespace},
            clock={"at": "2032-01-02T12:00:00+00:00", "timezone": "UTC"}, lookup={},
            dataset_hash="a" * 64, plan_hash="b" * 64, assumptions={}, setup={},
            branch_policies=[], action_ledger={})
        self.provisioner.save_owner(self.tenant, self.owner)
        from chatbot_core.models import TenantRuntimeConfiguration
        TenantRuntimeConfiguration.objects.create(tenant=self.tenant, version=1, documents=[])

    def test_credentials_stay_out_of_the_inspect_artifact(self):
        from evaluate.integration.credentials import LeaseCredentials

        secret = self.tenant.api_key
        credentials = LeaseCredentials(self.provisioner)
        resolved = credentials.resolve(self.lease)
        self.assertEqual(resolved.api_key, secret)
        self.assertEqual(credentials.session_key(self.lease), self.browser.session_key)
        self.assertNotIn(secret, repr(resolved))
        self.assertNotIn(secret, json.dumps(self.provisioner.inspect(self.lease)))
        self.assertNotIn(self.browser.session_key, json.dumps(self.provisioner.inspect(self.lease)))

    def test_router_splits_application_faults_from_geocoding(self):
        from evaluate.integration.controls import RoutedControls

        class Location:
            def __init__(self):
                self.calls = []

            def set_location_default(self, account, service, outcome, postal_code=None):
                self.calls.append((account, service, outcome, postal_code))

        location = Location()
        controls = RoutedControls(self.provisioner, plan=None, location=location)
        clock = FreezeClock(kind="freeze_clock", clock=Clock(at="2032-06-01T09:00:00+00:00", timezone="UTC"))
        controls.mutate(self.lease, self.tenant, self.owner, clock)
        self.assertEqual(self.owner["application_controls"]["clock"]["at"], "2032-06-01T09:00:00+00:00")
        self.assertEqual(self.owner["clock"]["at"], "2032-06-01T09:00:00+00:00")

        classification = LookupControl(kind="lookup_control", service="classification", outcome="timeout")
        controls.mutate(self.lease, self.tenant, self.owner, classification)
        self.assertEqual(self.owner["application_controls"]["faults"], ["classification"])
        self.assertEqual(location.calls, [])

        geocoding = LookupControl(kind="lookup_control", service="geocoding", outcome="unavailable")
        controls.mutate(self.lease, self.tenant, self.owner, geocoding)
        self.assertEqual(location.calls, [("eval-stitch", "geocoding", "unavailable", None)])
        self.assertNotIn("geocoding", self.owner["application_controls"].get("faults", []))

        reverse = LookupControl(kind="lookup_control", service="reverse_geocoding", outcome="success")
        with self.assertRaises(Blocked):
            controls.mutate(self.lease, self.tenant, self.owner, reverse)
        self.assertEqual(len(location.calls), 1)

    def test_snapshot_exposes_the_open_question(self):
        from evaluate.controls.inspection import StateInspector

        self.browser[self.namespace] = {
            "customer_id": str(self.customer.pk),
            "basket": {"items": []},
            "awaiting_followup_index": 0,
            "ongoing_query_queue": [{"follow_up_question": ["How many would you like?"]}],
        }
        self.browser.save()
        snapshot = StateInspector(self.provisioner).snapshot(self.lease, self.identity, None, None, "before")
        self.assertEqual(snapshot.state["chat"]["pending_question"], "How many would you like?")
        self.assertNotIn('query_id', snapshot.state['chat']['ongoing_query_queue'][0])
        self.assertNotIn(self.browser.session_key, snapshot.model_dump_json())

    def test_snapshot_preserves_real_task_completion_without_private_payloads(self):
        from evaluate.controls.inspection import StateInspector

        queue = [{'query_id': identity, 'intent_type': 'placing_order', 'sub_intent': 'add_to_basket',
                  'is_complete': complete, 'main_query': 'private-original-message',
                  'basket_item': {'private': 'payload'}, 'follow_up_question': []}
                 for identity, complete in ((7, False), ('task-8', True))]
        self.browser[self.namespace] = {
            'customer_id': str(self.customer.pk), 'basket': {'items': []},
            'awaiting_followup_index': None, 'ongoing_query_queue': queue,
        }
        self.browser.save()
        snapshot = StateInspector(self.provisioner).snapshot(self.lease, self.identity, 0, 'request', 'after')
        projected = snapshot.state['chat']['ongoing_query_queue']
        self.assertEqual([row['is_complete'] for row in projected], [False, True])
        self.assertEqual([row['intent_type'] for row in projected], ['placing_order'] * 2)
        self.assertEqual([row['query_id'] for row in projected], [7, 'task-8'])
        self.assertNotIn('main_query', projected[0])
        self.assertNotIn('basket_item', projected[0])
        self.assertNotIn('private-original-message', snapshot.model_dump_json())

    def test_snapshot_does_not_turn_unknown_queues_or_baskets_into_empty_state(self):
        from evaluate.controls.inspection import StateInspector

        for field, values in (('ongoing_query_queue', (None, {}, [None])),
                              ('basket', (None, {}, {'items': None}, {'items': [None]}))):
            for value in values:
                with self.subTest(field=field, value=value):
                    data = {'customer_id': str(self.customer.pk), 'basket': {'items': []},
                            'ongoing_query_queue': []}
                    data[field] = value
                    self.browser[self.namespace] = data
                    self.browser.save()
                    snapshot = StateInspector(self.provisioner).snapshot(
                        self.lease, self.identity, 0, 'request', 'after')
                    if field == 'basket':
                        self.assertNotIn('basket', snapshot.state)
                        self.assertIn('basket', snapshot.unavailable_sections)
                    else:
                        self.assertNotIn(field, snapshot.state['chat'])
                        self.assertNotIn('pending_question', snapshot.state['chat'])
        self.browser[self.namespace] = {'customer_id': str(self.customer.pk)}
        self.browser.save()
        snapshot = StateInspector(self.provisioner).snapshot(self.lease, self.identity, 0, 'request', 'after')
        self.assertNotIn('ongoing_query_queue', snapshot.state['chat'])
        self.assertNotIn('awaiting_followup_index', snapshot.state['chat'])
        self.assertNotIn('pending_question', snapshot.state['chat'])
        self.assertIn('basket', snapshot.unavailable_sections)
