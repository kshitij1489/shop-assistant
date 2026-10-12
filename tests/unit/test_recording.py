"""Recording must preserve application behavior and never retain mutable inputs."""
from concurrent.futures import ThreadPoolExecutor
import json

from django.test import SimpleTestCase, override_settings

from chatbot_core.models import TenantInfo
from chatbot_core.recording import current, emit, operation, recording
from chatbot_core.llm.chains import structured_chain, text_chain
from chatbot_core.llm.schemas import FollowupDecision
from tests.support.llm import ProviderHarness
from tests.support.recording import CaptureMixin
from tests.unit import test_streaming as streaming_contracts


class CaptureTests(CaptureMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tenant = TenantInfo(pk=1, api_key='tenant-secret-value')

    def test_immutable_snapshots_redact_credentials_and_browser_session_keys(self):
        value = {'basket': ['coffee'], 'api_key': self.tenant.api_key,
                 'nested': {'authorization': 'Bearer secret', 'password': 'secret'},
                 'text': 'literal ' + self.tenant.api_key, 'session': 'browser-credential'}
        with recording(self.tenant, 'website', 'browser-credential', query='hello'):
            emit('state', value=value)
            value['basket'].append('tea')
        saved = self.event('state')['payload']['value']
        self.assertEqual(saved['basket'], ['coffee'])
        serialized = json.dumps(self.events)
        for secret in (self.tenant.api_key, 'Bearer secret', 'browser-credential'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
        self.assertIsNone(current())

    def test_failure_records_mutated_state_and_does_not_retry(self):
        calls = []

        @operation('mutate')
        def mutate(state):
            calls.append(True)
            state['value'] = 'after'
            raise RuntimeError('failure')

        with self.assertRaisesMessage(RuntimeError, 'failure'):
            with recording(self.tenant, 'telegram', 'user'):
                mutate({'value': 'before'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.event('operation.started')['payload']['arguments']['state']['value'], 'before')
        self.assertEqual(self.event('operation.failed')['payload']['state_after']['state']['value'], 'after')
        self.assertEqual(self.events[-1]['payload']['status'], 'failed')
        self.assertIsNone(current())

    def test_enqueue_outage_is_bounded_and_terminal_marks_missing_events(self):
        def enqueue(data):
            if self.handoff.call_count == 2:
                raise ConnectionError('secret error text')
            self.events.append(json.loads(data))
        self.handoff.side_effect = enqueue
        with self.assertLogs('chatbot_core.recording.capture', 'WARNING'):
            with recording(self.tenant, 'telegram', 'user'):
                for _ in range(10):
                    emit('state', value='ok')
        self.assertEqual(self.handoff.call_count, 3)
        self.assertEqual(self.events[-1]['sequence'], 12)
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 10)

    @override_settings(CONVERSATION_RECORDING_ENABLED=False)
    def test_disabled_capture_performs_no_handoff(self):
        with recording(self.tenant, 'telegram', 'user'):
            emit('state', value=object())
        self.handoff.assert_not_called()

    def test_parallel_tenants_and_nested_scopes_do_not_mix(self):
        def work(pk):
            tenant = TenantInfo(pk=pk)
            with recording(tenant, 'telegram', 'same-user') as outer:
                with recording(tenant, 'telegram', 'same-user') as inner:
                    self.assertIs(outer, inner)
                    emit('value', tenant=pk)
            self.assertIsNone(current())
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(work, (1, 2)))
        for pk in (1, 2):
            rows = [e for e in self.events if e['tenant_id'] == str(pk)]
            self.assertEqual([e['sequence'] for e in rows], [1, 2, 3])
            self.assertEqual(rows[1]['payload']['tenant'], pk)
        self.assertEqual(len({e['session_id'] for e in self.events}), 2)

    def test_unserializable_capture_is_explicitly_incomplete(self):
        with self.assertLogs('chatbot_core.recording.capture', 'WARNING'):
            with recording(self.tenant, 'telegram', 'user'):
                emit('bad', value=object())
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 1)
        self.assertEqual(self.events[-1]['sequence'], 3)

    def test_oversized_event_does_not_suppress_subsequent_events(self):
        from chatbot_core.recording.transport import EventTooLarge
        def enqueue(data):
            if json.loads(data)['event'] == 'large':
                raise EventTooLarge()
            self.events.append(json.loads(data))
        self.handoff.side_effect = enqueue
        with self.assertLogs('chatbot_core.recording.capture', 'WARNING'):
            with recording(self.tenant, 'telegram', 'user'):
                emit('large', value='oversize')
                emit('next', value='still captured')
        self.assertEqual(self.event('next')['payload']['value'], 'still captured')
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 1)


class ModelRecordingTests(CaptureMixin, ProviderHarness, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tenant = TenantInfo(pk=1)

    def test_assembled_messages_schema_raw_response_and_parsed_output(self):
        with recording(self.tenant, 'telegram', 'user'):
            result = structured_chain(FollowupDecision, 'Literal {schema}').invoke({'input': 'yes'})
        self.assertTrue(result.is_followup)
        self.assertEqual(self.event('model.started')['payload']['messages'][0][0]['content'], 'Literal {schema}')
        self.assertIn('properties', self.event('model_chain.started')['payload']['schema'])
        raw = self.event('model.completed')['payload']['response']['generations'][0][0]['message']
        self.assertIn('is_followup', raw['content'])
        self.assertEqual(self.event('model_chain.completed')['payload']['output']['output'], {'is_followup': True})
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)

    def test_parse_failure_retains_raw_output(self):
        self.payload = 'not valid JSON'
        with recording(self.tenant, 'telegram', 'user'):
            result = structured_chain(FollowupDecision, 'Classify', include_raw=True).invoke({'input': 'yes'})
        self.assertIsNotNone(result['parsing_error'])
        self.assertIn('not valid JSON', json.dumps(self.event('model.completed')))
        self.assertIn('parsing_error', json.dumps(self.event('model_chain.completed')))
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)

    def test_provider_failure_is_recorded_once(self):
        self.status = 500
        with self.assertRaises(Exception):
            with recording(self.tenant, 'telegram', 'user'):
                text_chain('Reply').invoke({'input': 'hello'})
        self.assertEqual(len(self.requests), 1)
        self.assertIn(self.event('model.failed')['payload']['error']['type'], {'InternalServerError', 'OpenAIAPIError'})
        self.assertEqual(self.events[-1]['payload']['status'], 'failed')


class ModelStreamRecordingTests(CaptureMixin, streaming_contracts.ReplyStreamingTests):
    def test_stream_retains_complete_raw_and_parsed_text(self):
        tenant = TenantInfo(pk=1)
        with recording(tenant, 'telegram', 'user'):
            self.assertEqual(''.join(text_chain('Answer').stream({'input': 'hello'})), 'Hello café!')
        raw = self.event('model.completed')['payload']['response']['generations'][0][0]['message']
        self.assertEqual(raw['content'], 'Hello café!')
        self.assertEqual(self.event('model_chain.completed')['payload']['output']['output'], 'Hello café!')
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
