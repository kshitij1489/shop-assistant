"""Tenant isolation, faithful exports, and honest completeness for persisted evidence."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chatbot_core.models import ConversationEvent, TenantInfo
from chatbot_core.recording import inspection
from chatbot_core.recording.inspection import export_recording, recorded_events, turn_summaries
from chatbot_core.recording.writer import persist_batch, segment_complete
from users.models import TenantProfile


@override_settings(CONVERSATION_RECORDING_ENABLED=False)
class RecordingInspectionTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Recorded café', approval_status='APPROVED')
        self.other = TenantInfo.objects.create(display_name='Private café', approval_status='APPROVED')
        self.user = get_user_model().objects.create_user(username='recording-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.session_id = 'a' * 40
        self.turn_id = uuid4()
        self.segment_id = uuid4()
        self.started_at = timezone.now()
        self.events = self.make_segment(
            ('recording.started', {'query': 'Large, and when do you close?'}),
            ('turn.input', {'query': 'Large, and when do you close?'}),
            ('turn.starting_state', {'state': {'basket': ['Latte'], 'pending': ['size']}}),
            ('model.started', {'call_id': 'call-1', 'messages': ['Full prompt'], 'settings': {'temperature': 0}}),
            ('model.completed', {'call_id': 'call-1', 'response': {'raw': 'Large', 'usage': {'total_tokens': 17}}}),
            ('operation.completed', {'operation': 'workflow.choose_followup', 'operation_id': 'op-1',
                                     'result': {'delivered_question': 'Anything else?'}}),
            ('turn.result', {'response': 'हम रात 9 बजे बंद होते हैं। Anything else?', 'basket': ['Large latte']}),
            ('session.publication.completed', {'snapshot': {'basket': ['Large latte']}}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 9, 'dropped_events': 0}),
        )
        self.persist(self.events)
        # A colliding session digest in another tenant must never broaden the scope.
        self.foreign = self.make_segment(
            ('recording.started', {'query': 'FOREIGN SECRET'}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 2, 'dropped_events': 0}),
            tenant=self.other, turn_id=uuid4(), segment_id=uuid4(),
        )
        self.persist(self.foreign)

    def make_segment(self, *entries, tenant=None, turn_id=None, segment_id=None,
                     kind='turn', channel='website', session_id=None):
        return [dict(schema_version=1, event_id=str(uuid4()), tenant_id=str((tenant or self.tenant).pk),
                     channel=channel, session_id=session_id or self.session_id,
                     turn_id=str(turn_id or self.turn_id), segment_id=str(segment_id or self.segment_id),
                     segment_kind=kind, sequence=index,
                     occurred_at=(self.started_at + timedelta(milliseconds=index)).isoformat(),
                     event=name, parent_operation_id=None, payload=payload)
                for index, (name, payload) in enumerate(entries, 1)]

    def persist(self, events):
        persist_batch([(f'{index}-0', {'event': json.dumps(event)}) for index, event in enumerate(events)])

    def url(self, kind, **kwargs):
        if kind.startswith('session') and kind != 'sessions':
            kwargs = {'channel': 'website', 'session_id': self.session_id, **kwargs}
        if kind.startswith('turn'):
            kwargs = {'turn_id': self.turn_id, **kwargs}
        return reverse(f'tenant:recording_{kind}', kwargs=kwargs)

    def summary(self):
        return turn_summaries(recorded_events(tenant_id=self.tenant.pk))[str(self.turn_id)]

    def download(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])
        # Django's test client closes streaming responses after iteration, while
        # protecting the TestCase transaction from request-finished cleanup.
        result = json.loads(b''.join(response.streaming_content))
        self.assertTrue(response.closed)
        return result

    def test_browser_lists_only_authenticated_tenant_with_capture_disabled(self):
        response = self.client.get(self.url('sessions'), {'tenant_id': self.other.pk})
        self.assertContains(response, 'Capture is currently disabled')
        self.assertContains(response, self.session_id)
        self.assertContains(response, 'Recordings')
        rows = list(response.context['page'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_count'], 9)
        self.assertEqual(rows[0]['turn_count'], 1)
        self.assertIn('no-store', response['Cache-Control'])

    def test_filters_find_the_session_and_preserve_full_counts(self):
        another = self.make_segment(
            ('recording.started', {}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 2, 'dropped_events': 0}),
            turn_id=uuid4(), segment_id=uuid4(),
        )
        self.persist(another)
        for filters in ({'channel': 'website'}, {'session_id': self.session_id}, {'turn_id': self.turn_id}):
            with self.subTest(filters=filters):
                response = self.client.get(self.url('sessions'), filters)
                self.assertEqual(list(response.context['page'])[0]['event_count'], 11)
        response = self.client.get(self.url('sessions'), {'turn_id': self.foreign[0]['turn_id']})
        self.assertEqual(list(response.context['page']), [])
        for filters in ({'turn_id': 'bad'}, {'channel': 'bad'}, {'session_id': 's' * 65}):
            self.assertEqual(self.client.get(self.url('sessions'), filters).status_code, 400)

    def test_inspection_displays_snapshots_model_data_and_outcomes_without_execution(self):
        with patch('chatbot_core.recording.transport.enqueue') as enqueue, \
             patch('chatbot_core.logic.cafe.workflow.runner.run_conversation') as run:
            response = self.client.get(self.url('turn'))
        self.assertEqual(response.status_code, 200)
        for value in ('Full prompt', 'total_tokens', 'workflow.choose_followup', 'Anything else?',
                      'Large latte', 'turn.starting_state', 'Complete evidence', 'Execution: succeeded'):
            self.assertContains(response, value)
        self.assertEqual(response.context['summary']['event_count'], 9)
        session = self.client.get(self.url('session'))
        self.assertContains(session, 'Large, and when do you close?')
        self.assertContains(session, 'हम रात 9 बजे बंद होते हैं।')
        enqueue.assert_not_called()
        run.assert_not_called()

    def test_chat_id_search_uses_channel_and_tenant_scoped_digests(self):
        from chatbot_core.recording.capture import session_digest
        chat_id = '1234567890'
        entries = (('recording.started', {}),
                   ('recording.completed', {'status': 'succeeded', 'event_count': 2, 'dropped_events': 0}))
        expected = {}
        for tenant in (self.tenant, self.other):
            for channel in ('telegram', 'whatsapp', 'website'):
                digest = session_digest(tenant.pk, channel, chat_id)
                self.persist(self.make_segment(*entries, tenant=tenant, channel=channel,
                    session_id=digest, turn_id=uuid4(), segment_id=uuid4()))
                if tenant == self.tenant and channel != 'website':
                    expected[channel] = digest

        response = self.client.get(self.url('sessions'), {'session_id': chat_id})
        self.assertEqual({row['channel']: row['session_id'] for row in response.context['page']}, expected)
        for channel, digest in expected.items():
            for lookup in (chat_id, digest):
                with self.subTest(channel=channel, lookup=lookup):
                    response = self.client.get(self.url('sessions'), {'channel': channel, 'session_id': lookup})
                    self.assertEqual([row['session_id'] for row in response.context['page']], [digest])
        response = self.client.get(self.url('sessions'), {'channel': 'website', 'session_id': chat_id})
        self.assertEqual(list(response.context['page']), [])

    def test_payloads_are_escaped_and_event_filters_do_not_hide_incompleteness(self):
        extra = self.make_segment(
            ('recording.started', {}),
            ('delivery.failed', {'error': '<script>alert("x")</script>'}),
            kind='delivery', segment_id=uuid4(),
        )
        self.persist(extra)
        response = self.client.get(self.url('turn'), {'event': 'delivery.failed'})
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(')
        self.assertContains(response, 'Incomplete evidence')
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertEqual(response.context['summary']['event_count'], 11)
        self.assertEqual(self.client.get(self.url('turn'), {'segment': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.get(self.url('turn'), {'event': 'unknown'}).status_code, 400)

    def test_exports_preserve_every_envelope_and_include_unfiltered_async_segments(self):
        related = self.make_segment(
            ('recording.started', {}),
            ('commerce.payment_state', {'state': 'confirmed'}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 3, 'dropped_events': 0}),
            kind='commerce', segment_id=uuid4(),
        )
        self.persist(related)
        before = list(ConversationEvent.objects.order_by('event_id').values())
        for kind in ('turn_export', 'session_export'):
            exported = self.download(self.url(kind) + '?event=model.started&tenant_id=' + str(self.other.pk))
            self.assertEqual(exported['format_version'], 1)
            self.assertEqual(exported['scope']['tenant_id'], str(self.tenant.pk))
            self.assertEqual(exported['event_count'], 12)
            expected = sorted(self.events + related, key=lambda event: (event['turn_id'], event['segment_id'], event['sequence']))
            self.assertEqual([row['envelope'] for row in exported['events']], expected)
            self.assertTrue(all(row['recorded_at'] for row in exported['events']))
            self.assertEqual(len(exported['segments']), 2)
            self.assertTrue(all(segment['complete'] for segment in exported['segments']))
            self.assertTrue(exported['turns'][str(self.turn_id)]['complete'])
            self.assertNotIn('FOREIGN SECRET', json.dumps(exported))
        self.assertEqual(list(ConversationEvent.objects.order_by('event_id').values()), before)

    def test_session_export_includes_all_turns_and_is_channel_scoped(self):
        second = self.make_segment(('recording.started', {}), turn_id=uuid4(), segment_id=uuid4())
        telegram = self.make_segment(('recording.started', {'query': 'TELEGRAM ONLY'}),
                                     turn_id=uuid4(), segment_id=uuid4(), channel='telegram')
        self.persist(second + telegram)
        exported = self.download(self.url('session_export'))
        self.assertEqual(exported['event_count'], 10)
        self.assertNotIn('TELEGRAM ONLY', json.dumps(exported))
        self.assertEqual(self.download(self.url('turn_export'))['event_count'], 9)

    def test_cross_tenant_and_unknown_turns_and_sessions_are_not_found(self):
        for kind in ('turn', 'turn_export'):
            for turn_id in (self.foreign[0]['turn_id'], uuid4()):
                self.assertEqual(self.client.get(self.url(kind, turn_id=turn_id)).status_code, 404)
        for kind in ('session', 'session_export'):
            self.assertEqual(self.client.get(self.url(kind, session_id='unknown')).status_code, 404)
            self.assertEqual(self.client.get(self.url(kind, channel='telegram')).status_code, 404)
        # Removing our evidence cannot make the colliding foreign session visible.
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        self.assertEqual(self.client.get(self.url('session')).status_code, 404)
        self.assertEqual(self.client.get(self.url('session_export')).status_code, 404)

    def test_all_routes_require_login_and_disallow_mutations(self):
        for kind in ('sessions', 'session', 'turn', 'session_export', 'turn_export'):
            self.assertEqual(self.client.post(self.url(kind)).status_code, 405)
        self.client.logout()
        for kind in ('sessions', 'session', 'turn', 'session_export', 'turn_export'):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 302)

    def test_inactive_unapproved_and_unlinked_accounts_are_denied(self):
        for changes in ({'is_active': False}, {'approval_status': 'PENDING'}):
            TenantInfo.objects.filter(pk=self.tenant.pk).update(**changes)
            for kind in ('sessions', 'session', 'turn', 'session_export', 'turn_export'):
                self.assertEqual(self.client.get(self.url(kind)).status_code, 403)
            TenantInfo.objects.filter(pk=self.tenant.pk).update(is_active=True, approval_status='APPROVED')
        TenantProfile.objects.filter(user=self.user).delete()
        self.assertEqual(self.client.get(self.url('sessions')).status_code, 403)

    def test_master_must_explicitly_select_a_tenant(self):
        master = get_user_model().objects.create_user(username='recording-master', is_superuser=True)
        self.client.force_login(master)
        self.assertEqual(self.client.get(self.url('turn')).status_code, 403)
        session = self.client.session
        session['impersonated_tenant_id'] = self.tenant.pk
        session.save()
        self.assertEqual(self.client.get(self.url('turn')).status_code, 200)
        self.assertEqual(self.client.get(self.url('turn', turn_id=self.foreign[0]['turn_id'])).status_code, 404)

    def test_failed_execution_can_have_complete_evidence(self):
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        events = self.make_segment(
            ('recording.started', {}), ('recording.failed', {'error': 'provider failure'}),
            ('recording.completed', {'status': 'failed', 'event_count': 3, 'dropped_events': 0}),
        )
        self.persist(events)
        self.assertTrue(self.summary()['complete'])
        self.assertEqual(self.summary()['execution_status'], 'failed')
        self.assertEqual(self.summary()['failure_count'], 1)
        response = self.client.get(self.url('session'))
        self.assertContains(response, 'Complete evidence')
        self.assertContains(response, 'Execution: failed')

    def test_missing_event_drops_and_malformed_markers_cannot_claim_completeness(self):
        cases = []
        missing = deepcopy(self.events)
        del missing[3]
        cases.append(missing)
        cases.append(deepcopy(self.events[:-1]))
        cases.append(deepcopy(self.events[1:]))
        for key, value in (('dropped_events', 1), ('event_count', 99), ('event_count', '9'), ('dropped_events', False)):
            events = deepcopy(self.events)
            events[-1]['payload'][key] = value
            cases.append(events)
        duplicated = deepcopy(self.events)
        duplicated[3]['event'] = 'recording.started'
        cases.append(duplicated)
        after_completion = deepcopy(self.events)
        after_completion[-2]['event'] = 'recording.completed'
        cases.append(after_completion)
        for events in cases:
            with self.subTest(events=events[-1]['payload']):
                ConversationEvent.objects.filter(tenant=self.tenant).delete()
                self.persist(events)
                self.assertFalse(self.summary()['complete'])
                self.assertFalse(segment_complete(tenant_id=self.tenant.pk, segment_id=self.segment_id))
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        self.persist(missing)
        self.assertEqual(self.summary()['segments'][0]['missing_sequence_ranges'], [[4, 4]])

    def test_out_of_order_persistence_and_late_segments_update_completeness(self):
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        self.persist(self.events[4:])
        self.assertFalse(self.summary()['complete'])
        self.persist(self.events[:4])
        self.assertTrue(self.summary()['complete'])
        before = self.download(self.url('turn_export'))
        related = self.make_segment(
            ('recording.started', {}), ('delivery.observed', {'status': 'server_iteration_completed'}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 3, 'dropped_events': 0}),
            kind='delivery', segment_id=uuid4(),
        )
        self.persist(related[:2])
        self.assertFalse(self.summary()['complete'])
        self.persist(related[2:])
        after = self.download(self.url('turn_export'))
        self.assertTrue(self.summary()['complete'])
        self.assertEqual(before['event_count'], 9)
        self.assertEqual(after['event_count'], 12)

    def test_orphaned_related_segment_does_not_claim_a_complete_turn(self):
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        related = self.make_segment(
            ('recording.started', {}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 2, 'dropped_events': 0}),
            kind='commerce', segment_id=uuid4(),
        )
        self.persist(related)
        self.assertFalse(self.summary()['complete'])
        self.assertFalse(self.summary()['has_turn_segment'])
        self.assertEqual(self.summary()['execution_status'], 'unknown')
        self.assertContains(self.client.get(self.url('turn')), 'Original turn segment is missing')
        self.assertFalse(self.download(self.url('turn_export'))['turns'][str(self.turn_id)]['complete'])

    def test_message_previews_preserve_json_like_strings(self):
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        events = self.make_segment(('turn.input', {'query': 'null'}), ('turn.result', {'response': 'true'}))
        self.persist(events)
        session = self.client.get(self.url('session'))
        self.assertEqual(list(session.context['page'])[0]['query'], 'null')
        self.assertEqual(list(session.context['page'])[0]['reply'], 'true')
        turn = self.client.get(self.url('turn'))
        self.assertEqual(turn.context['highlights'][0]['details']['query'], 'null')
        self.assertEqual(turn.context['highlights'][1]['details']['response'], 'true')

    def test_event_pagination_is_deterministic_and_export_is_not_paginated(self):
        ConversationEvent.objects.filter(tenant=self.tenant).delete()
        entries = [('recording.started', {})] + [('model.retry', {'attempt': i}) for i in range(30)]
        entries += [('recording.completed', {'status': 'succeeded', 'event_count': 32, 'dropped_events': 0})]
        events = self.make_segment(*entries)
        # Identical timestamps still have a stable order inside a segment.
        for event in events:
            event['occurred_at'] = self.started_at.isoformat()
        self.persist(list(reversed(events)))
        first = self.client.get(self.url('turn'))
        second = self.client.get(self.url('turn'), {'page': 2})
        self.assertEqual([row.sequence for row in first.context['page']], list(range(1, 26)))
        self.assertEqual([row.sequence for row in second.context['page']], list(range(26, 33)))
        filtered = self.client.get(self.url('turn'), {'event': 'model.retry'})
        self.assertContains(filtered, 'event=model.retry&amp;page=2')
        self.assertTrue(second.context['summary']['complete'])
        self.assertEqual(self.download(self.url('turn_export') + '?page=2')['event_count'], 32)

    def test_session_and_turn_lists_are_paginated(self):
        for index in range(26):
            self.persist(self.make_segment(('recording.started', {}), turn_id=uuid4(), segment_id=uuid4()))
            self.persist(self.make_segment(('recording.started', {}), turn_id=uuid4(), segment_id=uuid4(),
                                           session_id=f'session-{index}'))
        for kind in ('sessions', 'session'):
            first = self.client.get(self.url(kind))
            second = self.client.get(self.url(kind), {'page': 2})
            self.assertEqual(len(first.context['page']), 25)
            self.assertEqual(len(second.context['page']), 2)

    def test_large_export_spools_to_disk_and_preserves_content(self):
        large = self.make_segment(('recording.started', {'query': 'é' * (1024 * 1024)}),
                                   turn_id=uuid4(), segment_id=uuid4())
        self.persist(large)
        events = recorded_events(tenant_id=self.tenant.pk).filter(turn_id=large[0]['turn_id'])
        with export_recording(events, scope={'tenant_id': str(self.tenant.pk)}) as output:
            self.assertTrue(output._rolled)
            self.assertEqual(json.load(output)['events'][0]['envelope'], large[0])

    def test_writer_arrival_during_export_cannot_change_its_summary(self):
        late = self.make_segment(
            ('recording.started', {}),
            ('recording.completed', {'status': 'succeeded', 'event_count': 2, 'dropped_events': 0}),
            kind='delivery', segment_id=uuid4(),
        )
        # More than one export cursor fetch: new rows must not sneak into a later
        # batch or a separately queried completeness summary.
        for _ in range(12):
            self.persist(self.make_segment(('recording.started', {}),
                                           turn_id=uuid4(), segment_id=uuid4()))
        events = recorded_events(tenant_id=self.tenant.pk)
        expected = events.count()
        original_metadata = inspection._metadata
        arrived = False

        def during_export(row):
            nonlocal arrived
            if not arrived:
                arrived = True
                self.persist(late)
            return original_metadata(row)

        with patch.object(inspection, '_metadata', side_effect=during_export):
            with export_recording(events, scope={'tenant_id': str(self.tenant.pk)}) as output:
                exported = json.load(output)
        self.assertEqual(events.count(), expected + 2)
        if connection.vendor == 'postgresql':
            # The production database holds the cursor's statement snapshot.
            # SQLite can see same-connection writes while stepping its cursor.
            self.assertEqual(exported['event_count'], expected)
            self.assertNotIn(late[0]['segment_id'], {row['segment_id'] for row in exported['segments']})
        self.assertEqual(len(exported['events']), exported['event_count'])
        self.assertEqual(sum(row['event_count'] for row in exported['segments']), exported['event_count'])
        self.assertEqual({row['segment_id'] for row in exported['segments']},
                         {row['envelope']['segment_id'] for row in exported['events']})
