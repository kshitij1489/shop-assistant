"""Replay real captured turns with providers and application storage unavailable."""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from chatbot_core.recording.replay import ReplayTape, ReplayUnavailable
from chatbot_core.recording.replay_codec import decode
from tests.support.checkout import CheckoutFixture
from tests.support.recording import CaptureMixin


class RecordingReplayTests(CaptureMixin, CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        from orders.models import CheckoutSettings
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)

    def capture_turn(self, query='checkout', **kwargs):
        self.events.clear()
        store = self.graph_store()
        self.graph_turn(store, query, **kwargs)
        failures = [e['payload'] for e in self.events if e['event'] == 'replay.unavailable']
        self.assertEqual(failures, [])
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
        return self.tape()

    def tape(self, events=None):
        events = self.events if events is None else events
        return ReplayTape(events, tenant_id=self.tenant.pk, turn_id=events[0]['turn_id'])

    def test_turn_executes_graph_without_live_dependencies_or_writes(self):
        tape = self.capture_turn()
        original = deepcopy(self.events)
        from chatbot_core.logic.cafe.workflow import graph
        with patch.object(graph, '_call_handler', side_effect=AssertionError('live handler')), \
                patch.object(graph, 'normalize_and_classify', side_effect=AssertionError('live model')), \
                patch.object(graph, 'classification_context', side_effect=AssertionError('live context')), \
                patch.object(graph, 'render_reply', side_effect=AssertionError('live reply')), \
                patch.object(graph, 'publish_reply', side_effect=AssertionError('live delivery')):
            with self.assertNumQueries(0):
                result = tape.execute()
        self.assertEqual(result['status'], 'matched', json.dumps({k: v for k, v in result.items() if k != 'result'}, default=str))
        self.assertGreater(result['dependencies_consumed'], 0)
        self.assertEqual(len(result['steps']), len(tape.steps))
        self.assertEqual(self.events, original)
        self.assertIn('response', decode(result['result'])['state'])

    def test_each_selected_step_replays_without_earlier_turns(self):
        tape = self.capture_turn()
        for step in tape.steps:
            with self.subTest(step=step['name']), self.assertNumQueries(0):
                result = tape.execute(step_id=step['id'])
                self.assertEqual(result['status'], 'matched', json.dumps({k: v for k, v in result.items() if k != 'result'}, default=str))
                self.assertEqual(len(result['steps']), 1)

    def test_changed_dependency_input_stops_without_fallback(self):
        tape = self.capture_turn()
        from chatbot_core.logic.cafe.workflow import graph
        original = graph.classification_context
        def changed(state, runtime):
            from chatbot_core.recording.replay_capture import dependency
            return dependency('classification.context', original, ['changed'])
        changed.__name__ = 'classify_intents'
        with patch.object(graph, 'classify_intents', changed):
            step = next(s for s in tape.steps if s['name'] == 'classify_intents')
            result = tape.execute(step_id=step['id'])
        self.assertEqual(result['status'], 'mismatch')
        self.assertIn('Dependency input mismatch', result['error'])
        self.assertEqual(result['dependencies_consumed'], 0)

    def test_missing_duplicate_or_legacy_evidence_is_rejected(self):
        self.capture_turn()
        for events in (self.events[1:], [*self.events, self.events[1]]):
            with self.assertRaises(ReplayUnavailable):
                self.tape(events)
        legacy = [e for e in deepcopy(self.events) if not e['event'].startswith('replay.')]
        for index, event in enumerate(legacy, 1):
            event['sequence'] = index
        legacy[-1]['payload']['event_count'] = len(legacy)
        with self.assertRaisesMessage(ReplayUnavailable, 'predates replay checkpoints'):
            self.tape(legacy)

    def test_cross_tenant_and_unknown_step_are_rejected(self):
        tape = self.capture_turn()
        with self.assertRaises(ReplayUnavailable):
            ReplayTape(self.events, tenant_id=self.tenant.pk + 1, turn_id=self.events[0]['turn_id'])
        with self.assertRaisesMessage(ReplayUnavailable, 'does not belong'):
            tape.execute(step_id=uuid4())

    def test_followup_restores_pending_aliases_and_checkout_without_prior_turn(self):
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.events.clear()
        self.graph_turn(store, 'pickup')
        tape = self.tape()
        self.assertIn('process_followup', [s['name'] for s in tape.steps])
        for step_id in (None, *[s['id'] for s in tape.steps]):
            with self.subTest(step_id=step_id), self.assertNumQueries(0):
                report = tape.execute(step_id=step_id)
                self.assertEqual(report['status'], 'matched', report.get('error') or report.get('difference') or report['steps'])

    def test_cancellation_restores_business_mutations_without_cancelling_live_checkout(self):
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.events.clear()
        self.graph_turn(store, 'cancel checkout')
        tape = self.tape()
        self.assertIn('cancel_pending', [s['name'] for s in tape.steps])
        with self.assertNumQueries(0):
            report = tape.execute()
        self.assertEqual(report['status'], 'matched', report)

    def test_recorded_model_failure_reproduces_classifier_fallback(self):
        from chatbot_core.logic.cafe.workflow import graph, runner
        from chatbot_core.logic.cafe.prompts.normalize_and_classify import NormalizationClassificationError
        store = self.graph_store()
        with patch.object(runner, 'enqueue_string'), patch.object(graph, 'normalize_and_classify',
                side_effect=NormalizationClassificationError('bad model response')):
            runner.run_conversation(self.tenant, store, 'hello', self.customer)
        with self.assertNumQueries(0):
            report = self.tape().execute()
        self.assertEqual(report['status'], 'matched', report)
        self.assertFalse(decode(report['result'])['state']['persist'])

    def test_failed_handler_is_reproduced_without_reinvocation(self):
        from chatbot_core.logic.cafe.workflow import graph
        with patch.object(graph, '_call_handler', side_effect=RuntimeError('handler failed')):
            with self.assertRaisesMessage(RuntimeError, 'handler failed'):
                self.capture_turn()
        tape = self.tape()
        with self.assertNumQueries(0):
            report = tape.execute()
        self.assertEqual(report['status'], 'matched', report)
        self.assertEqual(report['recorded_failure'], 'handler failed')

    def test_changed_pure_step_output_reports_divergence(self):
        tape = self.capture_turn()
        from chatbot_core.logic.cafe.workflow import graph
        original = graph.choose_followup
        def changed(state):
            return {**original(state), 'response': 'A changed response'}
        changed.__name__ = 'choose_followup'
        step = next(s for s in tape.steps if s['name'] == 'choose_followup')
        with patch.object(graph, 'choose_followup', changed):
            report = tape.execute(step_id=step['id'])
        self.assertEqual(report['status'], 'diverged')
        self.assertIsNotNone(report['steps'][0]['difference'])

    def test_live_database_network_and_filesystem_access_fail_closed(self):
        import socket
        from chatbot_core.models import TenantInfo
        from chatbot_core.logic.cafe.workflow import graph
        from pathlib import Path
        tape = self.capture_turn()
        step = tape.steps[0]
        operations = [lambda: TenantInfo.objects.count(), lambda: socket.create_connection(('localhost', 9)),
                      lambda: Path('/tmp/replay-must-not-write').write_text('forbidden')]
        for operation in operations:
            def unsafe(state):
                return operation()
            unsafe.__name__ = step['name']
            with self.subTest(operation=operation), patch.object(graph, step['name'], unsafe):
                report = tape.execute(step_id=step['id'])
            self.assertEqual(report['status'], 'mismatch')
            self.assertIn('forbidden', report['error'])
        # Guard scope is reset even after a fatal replay mismatch.
        self.assertTrue(TenantInfo.objects.filter(pk=self.tenant.pk).exists())

    def persist(self):
        from chatbot_core.recording.writer import persist_batch
        persist_batch([(f'{i}-0', {'event': json.dumps(e)}) for i, e in enumerate(self.events)])

    def login(self):
        from django.contrib.auth import get_user_model
        from users.models import TenantProfile
        user = get_user_model().objects.create_user(username='replay-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        return user

    def test_dashboard_replay_is_scoped_post_only_and_csrf_protected(self):
        from django.test import Client
        from django.urls import reverse
        from chatbot_core.models import TenantInfo
        tape = self.capture_turn()
        self.persist()
        user = self.login()
        url = reverse('tenant:recording_turn_replay', args=[tape.identity['turn_id']])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)
        self.assertContains(response, 'Matched')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.post(url, {'step_id': uuid4()}).status_code, 409)
        self.assertEqual(self.client.post(url, {'step_id': 'invalid'}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(user)
        self.assertEqual(csrf.post(url).status_code, 403)
        from users.models import TenantProfile
        other = TenantInfo.objects.create(display_name='Other', approval_status='APPROVED')
        TenantProfile.objects.filter(user=user).update(tenant=other)
        self.assertEqual(self.client.post(url, {'tenant_id': self.tenant.pk}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)

    def test_export_and_management_command_are_offline_and_leave_evidence_unchanged(self):
        from io import StringIO
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from django.core.management import call_command
        from chatbot_core.recording.replay import replay_export
        tape = self.capture_turn()
        export = {'format': 'studio-desk.conversation-recording', 'format_version': 1,
                  'scope': tape.identity, 'events': [{'envelope': e} for e in self.events]}
        before = deepcopy(export)
        with self.assertNumQueries(0):
            report = replay_export(export, tenant_id=self.tenant.pk, turn_id=tape.identity['turn_id'])
        self.assertEqual(report['status'], 'matched', report)
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'turn.json'
            source.write_text(json.dumps(export))
            stdout = StringIO()
            with self.assertNumQueries(0):
                call_command('replay_conversation', tenant_id=self.tenant.pk,
                             turn_id=tape.identity['turn_id'], input=source, stdout=stdout)
            self.assertEqual(json.loads(stdout.getvalue())['status'], 'matched')
        self.assertEqual(export, before)

    def test_non_explicit_classification_and_parallel_replays(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.capture_turn('Please begin my checkout', resolved='checkout')
        self.assertTrue(any(d['start']['name'] == 'classification.model'
                            for s in first.steps for d in s['dependencies']))
        # The graph is cached, but replay state/dependency cursors are per context.
        second = self.capture_turn('Hello there', classification=('general', 'greeting'))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda tape: tape.execute(), (first, second)))
        for tape, report in zip((first, second), results):
            self.assertEqual(report['status'], 'matched', report)
            self.assertEqual(report['scope'], tape.identity)

    def test_unused_or_out_of_order_dependencies_do_not_report_success(self):
        tape = self.capture_turn()
        step = next(s for s in tape.steps if s['name'] == 'render_response')
        step['dependencies'].reverse()
        report = tape.execute(step_id=step['id'])
        self.assertEqual(report['status'], 'mismatch')
        tape = self.tape()
        step = next(s for s in tape.steps if s['name'] == 'render_response')
        step['dependencies'].append(deepcopy(step['dependencies'][-1]))
        report = tape.execute(step_id=step['id'])
        self.assertEqual(report['status'], 'mismatch')
        self.assertIn('Unused', report['error'])
