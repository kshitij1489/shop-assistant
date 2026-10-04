"""Historical setup with adapter requests on a separate database connection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, connections
from django.test import TransactionTestCase

from commerce import models as cm
from evaluate.contracts.interfaces import Blocked
from evaluate.contracts.models import ExecutionIdentity, ModelNames, RunConfiguration
from evaluate.fixtures.provision import DjangoProvisioner
from evaluate.scenarios.controls import DatasetControls
from evaluate.scenarios.plan import load_plan
from evaluate.scenarios.runtime import LocalRuntime, SignedLocalClient


class TerminalOrderTests(TransactionTestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.runtime = LocalRuntime(directory)
        self.provisioner = DjangoProvisioner(self.runtime)
        dataset = Path(__file__).resolve().parents[3] / "test_data"
        plan, bundle = load_plan(dataset)
        self.case = next(case for case in bundle.scenarios if case.source_id == "s136_new_order_after_terminal")
        self.controls = DatasetControls(self.provisioner, plan)
        config = RunConfiguration(run_id="terminal-test", scenario_plan_version=plan.version,
                                  dataset_directory=str(dataset), models=ModelNames(chat="unused", translate="unused", analytics="unused"))
        self.identity = ExecutionIdentity(run_id=config.run_id, scenario_id=self.case.scenario_id,
                                          scenario_instance_id=str(uuid4()), attempt=1)
        executor = self.enterContext(ThreadPoolExecutor(max_workers=1))

        class SeparateConnectionClient(SignedLocalClient):
            def request(client, *args, **kwargs):
                def request():
                    try:
                        return super(SeparateConnectionClient, client).request(*args, **kwargs)
                    finally:
                        connections.close_all()
                return executor.submit(request).result(timeout=10)

        self.enterContext(patch("evaluate.scenarios.runtime.adapter_client_for", SeparateConnectionClient))
        # This fixture tests payment history, with no address lookup or chat calls.
        self.lease = self.provisioner.provision(config, self.case, self.identity)
        self.addCleanup(self.runtime.release, self.lease)

    def test_capture_sees_committed_payment_and_preserves_callback_writes(self):
        payment = self.runtime.payment

        def capture(*args):
            self.assertFalse(connection.in_atomic_block)
            payment(*args)
            tenant, owner = self.provisioner.owned(self.lease)
            owner["callback_marker"] = "retained"
            self.provisioner.save_owner(tenant, owner)

        with patch.object(self.runtime, "payment", side_effect=capture):
            events = self.controls.before_turn(self.lease, self.identity, self.case, None)
        binding = self.provisioner.binding(self.lease)
        binding["chat"].refresh_from_db()
        order = binding["chat"].order
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.order_status, "delivered")
        self.assertEqual(order.commerce_record.pos_state, "delivered")
        self.assertTrue(cm.Inbox.objects.filter(connection__location__tenant=binding["tenant"],
                                               status="processed", event_type="payment.updated").exists())
        _, owner = self.provisioner.owned(self.lease)
        self.assertEqual(owner["callback_marker"], "retained")
        self.assertEqual(owner["maps"]["orders"]["previous_terminal"], str(order.pk))
        self.assertTrue(all(event.status == "succeeded" for event in events))
        self.assertEqual(self.controls.before_turn(self.lease, self.identity, self.case, None), events)

    def test_failed_capture_preserves_history_and_prevents_replay(self):
        with patch.object(self.runtime, "payment", side_effect=RuntimeError("provider unavailable")):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                self.controls.before_turn(self.lease, self.identity, self.case, None)
        tenant, owner = self.provisioner.owned(self.lease)
        self.assertEqual(owner["lifecycle"], "failed_preserved")
        self.assertIn("previous_terminal", owner["maps"]["orders"])
        self.assertEqual(cm.Payment.objects.filter(connection__location__tenant=tenant).count(), 1)
        with self.assertRaisesRegex(Blocked, "incomplete or failed"):
            self.controls.before_turn(self.lease, self.identity, self.case, None)
