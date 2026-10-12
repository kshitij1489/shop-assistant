"""Persistence, failure boundaries, tenant scope, and asynchronous provenance."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature, override_settings

from chatbot_core.models import ConversationEvent, TenantInfo
from chatbot_core.recording import emit, recording
from chatbot_core.recording.writer import persist_batch, segment_complete, Writer
from tests.support.recording import CaptureMixin
from tests.support.checkout import CheckoutFixture
from tests.support.commerce import Fixtures
from tests.integration import test_website_streaming as streaming_contracts
from tests.integration import test_website as website_contracts


def stream_rows(events):
    return [(f'{index + 1}-0', {'event': json.dumps(event)}) for index, event in enumerate(events)]


class WriterTests(CaptureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tenant = TenantInfo.objects.create(display_name='Recorded cafe')
        with recording(self.tenant, 'telegram', 'chat') as recorder:
            emit('state', basket=['coffee'])
        self.segment = recorder.segment_id
        self.rows = stream_rows(self.events)

    def complete(self, tenant=None):
        return segment_complete(tenant_id=(tenant or self.tenant).pk, segment_id=self.segment)

    def test_out_of_order_batches_duplicates_and_completion(self):
        persist_batch(self.rows[1:])
        self.assertFalse(self.complete())
        persist_batch(self.rows[:1])
        persist_batch(self.rows)
        self.assertEqual(ConversationEvent.objects.count(), 3)
        self.assertTrue(self.complete())
        other = TenantInfo.objects.create(display_name='Other')
        self.assertFalse(self.complete(other))

    def test_completion_marker_cannot_hide_missing_event(self):
        persist_batch([self.rows[0], self.rows[-1]])
        self.assertFalse(self.complete())

    def test_terminal_drop_count_prevents_complete_recording(self):
        self.events[-1]['payload']['dropped_events'] = 1
        persist_batch(stream_rows(self.events))
        self.assertFalse(self.complete())

    def test_conflicting_event_id_and_sequence_do_not_overwrite(self):
        persist_batch(self.rows)
        changed = deepcopy(self.events[1])
        changed['payload']['basket'] = ['tampered']
        with self.assertRaisesMessage(ValueError, 'Conflicting recording'):
            persist_batch(stream_rows([changed]))
        changed['event_id'] = str(uuid4())
        with self.assertRaisesMessage(ValueError, 'Conflicting recording'):
            persist_batch(stream_rows([changed]))
        self.assertEqual(ConversationEvent.objects.get(sequence=2).envelope['payload']['basket'], ['coffee'])

    def test_segment_scope_cannot_change_between_batches(self):
        persist_batch(self.rows[:1])
        other = TenantInfo.objects.create(display_name='Other')
        changed = deepcopy(self.events[1])
        changed['tenant_id'] = str(other.pk)
        with self.assertRaisesMessage(ValueError, 'segment scope'):
            persist_batch(stream_rows([changed]))
        self.assertEqual(ConversationEvent.objects.count(), 1)

    def test_sql_failure_does_not_acknowledge(self):
        redis = Mock()
        writer = Writer(redis, 'test')
        with patch.object(ConversationEvent.objects, 'bulk_create', side_effect=RuntimeError('SQL outage')):
            with self.assertRaises(RuntimeError):
                writer.save(self.rows)
        redis.pipeline.assert_not_called()

    def test_commit_before_ack_and_redelivery_after_ack_failure(self):
        redis = Mock()
        pipe = redis.pipeline.return_value.__enter__ = Mock(return_value=Mock())
        redis.pipeline.return_value.__exit__ = Mock(return_value=False)
        pipeline = pipe.return_value

        def fail_ack():
            self.assertEqual(ConversationEvent.objects.count(), 3)
            raise ConnectionError('ack outage')
        pipeline.execute.side_effect = fail_ack
        writer = Writer(redis, 'test')
        with self.assertRaises(ConnectionError):
            writer.save(self.rows)
        pipeline.execute.side_effect = None
        writer.save(self.rows)
        self.assertEqual(ConversationEvent.objects.count(), 3)
        self.assertTrue(self.complete())

    def test_reclaim_recovers_abandoned_consumer_and_scans_forward(self):
        redis = Mock()
        redis.pipeline.return_value.__enter__ = Mock(return_value=Mock())
        redis.pipeline.return_value.__exit__ = Mock(return_value=False)
        redis.xautoclaim.return_value = ['9-0', self.rows, []]
        redis.xreadgroup.return_value = []
        writer = Writer(redis, 'restarted', reclaim_ms=10)
        self.assertEqual(writer.tick(block=1), 3)
        self.assertEqual(writer.cursor, '9-0')
        self.assertTrue(self.complete())

    def test_malformed_event_remains_pending_without_blocking_valid_rows(self):
        with self.assertLogs('chatbot_core.recording.writer', 'ERROR'):
            ids = persist_batch([('bad-0', {'event': '{}'}), *self.rows])
        self.assertEqual(ids, [key for key, _ in self.rows])

    def test_conflicting_entry_does_not_block_unrelated_events(self):
        persist_batch(self.rows[:1])
        changed = deepcopy(self.events[0])
        changed['payload']['query'] = 'conflict'
        redis = Mock()
        redis.pipeline.return_value.__enter__ = Mock(return_value=Mock())
        redis.pipeline.return_value.__exit__ = Mock(return_value=False)
        with self.assertLogs('chatbot_core.recording.writer', 'ERROR'):
            self.assertEqual(Writer(redis, 'test').save([('conflict-0', {'event': json.dumps(changed)}), *self.rows[1:]]), 2)
        self.assertTrue(self.complete())


class RecordingConcurrencyTests(CaptureMixin, TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_competing_writers_cannot_assign_different_tenants_to_one_turn(self):
        tenant = TenantInfo.objects.create(display_name='One')
        other = TenantInfo.objects.create(display_name='Two')
        with recording(tenant, 'telegram', 'chat'):
            pass
        first = self.events[0]
        second = {**deepcopy(first), 'event_id': str(uuid4()), 'segment_id': str(uuid4()), 'tenant_id': str(other.pk)}

        def write(event):
            close_old_connections()
            try:
                persist_batch(stream_rows([event]))
                return True
            except ValueError:
                return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, (first, second))), [False, True])
        self.assertEqual(ConversationEvent.objects.count(), 1)


class RecordedCacheTests(CaptureMixin, TestCase):
    @override_settings(SEMANTIC_CACHE_ENABLED=True, EMBEDDING_DIMENSION=3)
    def test_semantic_miss_vector_and_subsequent_answer_hit_are_captured(self):
        from django.core.cache import cache
        from chatbot_core.vector_store import semantic_cache as service
        from chatbot_core.vector_store.faiss_index import indexes
        cache.clear()
        indexes.clear()
        self.addCleanup(indexes.clear)
        tenant = TenantInfo.objects.create(display_name='Cache cafe')
        with patch.object(service, 'get_embedding', return_value=[1., 0., 0.]):
            with recording(tenant, 'telegram', 'user'):
                hit, answer, ctx = service.lookup('model', 'scope', 'Facts', 'Question?', 60)
                self.assertFalse(hit)
                with self.captureOnCommitCallbacks(execute=True):
                    service.store(ctx, 'Answer', 60, 'model')
                self.assertEqual(service.lookup('model', 'scope', 'Facts', 'Question?', 60)[:2], (True, 'Answer'))
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
        self.assertIn('Answer', json.dumps(self.events))


class RecordedChannelTests(CaptureMixin, TestCase):
    def setUp(self):
        super().setUp()
        from chatbot_core import processor
        from orders.models import Customer
        self.processor = processor
        tenant = TenantInfo.objects.create(display_name='Channel cafe', approval_status='APPROVED',
                                          telegram_bot_token='private-telegram-token')
        customer = Customer.objects.create(tenant=tenant, name='Guest')
        adapter = Mock()
        adapter.augment_text.side_effect = lambda text, _: text
        self.adapter = adapter
        self.payload = {
            'tenant_id': str(tenant.pk), 'user_id': 'user', 'channel': 'telegram',
            'chat_id': 'user', 'text': 'Original text', 'bot_token': tenant.telegram_bot_token,
        }
        for name in ('append_message', 'touch_active_chat', 'RedisSessionStore', 'set_latest_meta'):
            self.enterContext(patch.object(processor, name))
        for name, value in (('get_adapter', adapter), ('_call_create_or_get_customer_safe', customer),
                            ('is_global_agent_enabled', True), ('is_agent_enabled', True),
                            ('route_message_for_tenant', ('Reply', []))):
            self.enterContext(patch.object(processor, name, return_value=value))

    def process(self):
        self.processor.process_payload(self.payload['tenant_id'], 'user', self.payload)

    def assert_failed_execution(self):
        from chatbot_core.recording.inspection import recorded_events, turn_summaries
        self.assertEqual(self.events[-1]['payload']['status'], 'failed')
        persist_batch(stream_rows(self.events))
        summary = turn_summaries(recorded_events(tenant_id=self.payload['tenant_id']))
        self.assertEqual(summary[self.events[0]['turn_id']]['execution_status'], 'failed')
        self.assertTrue(summary[self.events[0]['turn_id']]['complete'])

    def test_queued_channel_records_original_input_and_send_failure_without_resending(self):
        self.adapter.send_text.side_effect = RuntimeError('delivery failed')
        with self.assertLogs('chatbot_core.processor', 'ERROR'):
            self.process()
        self.adapter.send_text.assert_called_once()
        self.assertEqual(self.event('channel.input')['payload']['payload']['text'], 'Original text')
        self.assertEqual(self.event('delivery.failed')['payload']['error']['type'], 'RuntimeError')
        self.assertNotIn(self.payload['bot_token'], json.dumps(self.events))
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
        self.assert_failed_execution()

    def test_swallowed_routing_failure_marks_execution_failed(self):
        self.processor.route_message_for_tenant.side_effect = RuntimeError('routing failed')
        with self.assertLogs('chatbot_core.processor', 'ERROR'):
            self.process()
        self.assertTrue(self.event('turn.routing_failed'))
        self.adapter.send_text.assert_called_once()
        self.assert_failed_execution()

    def test_swallowed_channel_failure_marks_execution_failed(self):
        self.processor._call_create_or_get_customer_safe.side_effect = RuntimeError('customer failed')
        with self.assertLogs('chatbot_core.processor', 'ERROR'):
            self.process()
        self.assertTrue(self.event('channel.failed'))
        self.adapter.send_text.assert_called_once_with(self.payload, self.processor.DEFAULT_ERROR_MSG)
        self.assert_failed_execution()

    def test_recovered_failure_keeps_successful_execution(self):
        self.adapter.augment_text.side_effect = RuntimeError('augmentation failed')
        with self.assertLogs('chatbot_core.processor', 'ERROR'):
            self.process()
        self.assertTrue(self.event('channel.augmentation_failed'))
        self.assertEqual(self.events[-1]['payload']['status'], 'succeeded')


class RecordedCheckoutTransactionTests(CaptureMixin, CheckoutFixture, TransactionTestCase):
    def test_checkout_boundary_handoffs_run_outside_its_transaction(self):
        from orders.models import ChatSession
        handoffs = []
        saves = []
        original_save = ChatSession.save

        def enqueue(data):
            event = json.loads(data)
            if event['payload'].get('operation') == 'checkout.advance':
                handoffs.append((event['event'], connection.in_atomic_block))
            self.capture_recording(data)

        def save(session, *args, **kwargs):
            saves.append(connection.in_atomic_block)
            return original_save(session, *args, **kwargs)

        self.handoff.side_effect = enqueue
        with patch.object(ChatSession, 'save', save):
            with recording(self.tenant, 'website', 'chat'):
                self.turn('cancel')
        self.assertEqual(saves, [True])
        self.assertEqual(handoffs, [('operation.started', False), ('operation.completed', False)])

        handoffs.clear()
        with patch.object(ChatSession, 'save', side_effect=RuntimeError('save failed')):
            with self.assertRaisesMessage(RuntimeError, 'save failed'):
                with recording(self.tenant, 'website', 'chat'):
                    self.turn('cancel')
        self.assertEqual(handoffs, [('operation.started', False), ('operation.failed', False)])


class RecordedCheckoutTests(CaptureMixin, CheckoutFixture, TestCase):
    def test_whole_turn_captures_followup_and_publication_without_sql_event_writes(self):
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        names = [e['event'] for e in self.events]
        self.assertIn('turn.starting_state', names)
        self.assertIn('configuration.loaded', names)
        self.assertIn('session.publication.completed', names)
        chosen = [e for e in self.events if e['event'] == 'operation.completed'
                  and e['payload']['operation'] == 'workflow.choose_followup']
        self.assertTrue(chosen)
        self.assertIn('delivered_question', chosen[-1]['payload']['result'])
        terminal = self.events[-1]
        self.assertEqual(terminal['event'], 'recording.completed')
        self.assertEqual(terminal['payload']['dropped_events'], 0)
        self.assertEqual(ConversationEvent.objects.count(), 0)

    def test_publication_failure_does_not_erase_successful_business_steps(self):
        store = self.graph_store()
        with patch.object(store, 'publish_snapshot', side_effect=RuntimeError('publication failed')):
            with self.assertRaisesMessage(RuntimeError, 'publication failed'):
                self.graph_turn(store, 'checkout')
        self.assertEqual(self.events[-1]['payload']['status'], 'failed')
        self.assertEqual(self.events[-1]['payload']['dropped_events'], 0)
        self.assertTrue(self.event('session.publication.failed'))
        self.assertFalse(any(e['event'] == 'session.publication.completed' for e in self.events))


class RecordedCommerceTests(CaptureMixin, Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_payment_event_restores_origin_after_turn_finishes(self):
        from commerce.events import receive
        with self.captureOnCommitCallbacks(execute=True):
            with recording(self.tenant, 'telegram', 'customer-chat') as turn:
                record = self.accept()
        self.assertEqual(record.recording_context, turn.identity)
        self.assertTrue(any(e['event'] == 'commerce.order.accepted' for e in self.events))
        with self.captureOnCommitCallbacks(execute=True):
            receive(self.gateway, self.event_payload(record))
        received = self.event('commerce.event.received')
        self.assertEqual(received['turn_id'], turn.identity['turn_id'])
        self.assertNotEqual(received['segment_id'], turn.segment_id)
        self.assertEqual(self.event('commerce.payment_state')['payload']['accepted_order']['state'], 'confirmed')
        for event in self.events:
            if event['event'] == 'recording.completed':
                self.assertEqual(event['payload']['dropped_events'], 0)

    def event_payload(self, record):
        return Fixtures.event(self, record)

    def test_rolled_back_order_does_not_publish_commit_evidence(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaisesMessage(RuntimeError, 'rollback'):
                with recording(self.tenant, 'telegram', 'customer-chat'):
                    with transaction.atomic():
                        self.accept()
                        raise RuntimeError('rollback')
        self.assertFalse(any(e['event'] == 'commerce.order.accepted' for e in self.events))


class RecordedWebsiteTests(CaptureMixin, website_contracts.WebsiteIntegrationTests):
    """Run the existing real HTTP/business contract with recording enabled."""
    def tearDown(self):
        for event in self.events:
            if event['event'] == 'recording.completed':
                self.assertEqual(event['payload']['dropped_events'], 0)
        super().tearDown()


class RecordedStreamingTests(CaptureMixin, streaming_contracts.WebsiteStreamingTests):
    """Exercise producer/consumer threads, commit failures and disconnects."""
    def tearDown(self):
        self.wait_for_recordings()
        for event in self.events:
            if event['event'] == 'recording.completed':
                self.assertEqual(event['payload']['dropped_events'], 0)
        super().tearDown()

    def test_delivery_and_session_commit_share_turn_but_have_distinct_segments(self):
        self.consume(self.send())
        publication = self.event('session.publication.completed')
        observed = next(e for e in self.events if e['event'] == 'delivery.observed'
                        and e['payload']['status'] == 'server_iteration_completed')
        self.assertEqual(observed['turn_id'], publication['turn_id'])
        self.assertNotEqual(observed['segment_id'], publication['segment_id'])
        self.assertNotIn(self.client.session.session_key, json.dumps(self.events))

    def test_caught_producer_failure_marks_execution_failed(self):
        from chatbot_core.channels import website
        with patch.object(website, 'route_message_for_tenant', side_effect=RuntimeError('routing failed')):
            with self.assertLogs('chatbot_core.channels.streaming', 'ERROR'):
                self.assertEqual(self.consume(self.send())[-1][0], 'error')
        completion = next(e for e in self.events if e['event'] == 'recording.completed'
                          and e['segment_kind'] == 'turn')
        self.assertEqual(completion['payload']['status'], 'failed')
