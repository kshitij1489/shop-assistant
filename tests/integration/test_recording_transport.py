"""Opt-in real Redis + SQL recovery contract; owns only its uniquely named stream."""
import os
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
from redis import Redis

from chatbot_core.models import ConversationEvent, TenantInfo
from chatbot_core.recording import recording, emit
from chatbot_core.recording import transport
from chatbot_core.recording.writer import Writer, persist_batch, segment_complete


@skipUnless(os.environ.get('RECORDING_TEST_REDIS_URL'), 'Set RECORDING_TEST_REDIS_URL to run real stream tests')
@override_settings(CONVERSATION_RECORDING_ENABLED=True)
class RecordingTransportTests(TransactionTestCase):
    def setUp(self):
        self.redis = Redis.from_url(os.environ['RECORDING_TEST_REDIS_URL'], decode_responses=True,
                                   socket_timeout=5)
        self.addCleanup(self.redis.close)
        self.stream = 'recording-test:' + uuid4().hex
        self.addCleanup(self.redis.delete, self.stream)
        self.enterContext(patch('chatbot_core.recording.transport.STREAM', self.stream))
        self.enterContext(patch('chatbot_core.recording.writer.STREAM', self.stream))
        self.enterContext(patch('chatbot_core.recording.transport.client', return_value=self.redis))
        self.tenant = TenantInfo.objects.create(display_name='Stream test')
        self.writer = Writer(self.redis, 'first', reclaim_ms=0)
        self.writer.setup()
        with recording(self.tenant, 'telegram', 'user') as turn:
            emit('state', basket=['coffee'])
        self.segment_id = turn.segment_id

    def test_crash_after_sql_commit_before_ack_is_recovered_by_new_consumer(self):
        records = self.redis.xreadgroup(transport.GROUP, 'crashed', {self.stream: '>'}, count=100)[0][1]
        persist_batch(records)
        self.assertEqual(self.redis.xpending(self.stream, transport.GROUP)['pending'], 3)
        restarted = Writer(self.redis, 'restarted', reclaim_ms=0)
        self.assertEqual(restarted.tick(block=1), 3)
        self.assertEqual(ConversationEvent.objects.count(), 3)
        self.assertEqual(self.redis.xpending(self.stream, transport.GROUP)['pending'], 0)
        self.assertEqual(self.redis.xlen(self.stream), 0)
        self.assertTrue(segment_complete(tenant_id=self.tenant.pk, segment_id=self.segment_id))

    def test_failed_sql_commit_keeps_entries_pending_and_recoverable(self):
        with patch('chatbot_core.recording.writer.persist_batch', side_effect=RuntimeError('SQL offline')):
            with self.assertRaisesMessage(RuntimeError, 'SQL offline'):
                self.writer.tick(block=1)
        self.assertEqual(self.redis.xpending(self.stream, transport.GROUP)['pending'], 3)
        self.assertEqual(ConversationEvent.objects.count(), 0)
        self.writer.tick(block=1)
        self.assertEqual(ConversationEvent.objects.count(), 3)
        self.assertEqual(self.redis.xlen(self.stream), 0)

    def test_same_envelope_enqueued_twice_has_one_persisted_event(self):
        first = self.redis.xrange(self.stream, count=1)[0][1]['event']
        self.redis.xadd(self.stream, {'event': first})
        self.assertEqual(self.writer.tick(block=1), 4)
        self.assertEqual(ConversationEvent.objects.count(), 3)
        self.assertTrue(segment_complete(tenant_id=self.tenant.pk, segment_id=self.segment_id))
