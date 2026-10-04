"""Channels forward typed addresses without GPS augmentation or location tasks."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from chatbot_core.channels.telegram import TelegramAdapterImpl
from chatbot_core.channels.telegram_webhook import telegram_webhook
from chatbot_core.channels.voice_assistant import voice_api
from chatbot_core.channels.voice_assistant_adapter import VoiceAssistantAdapter


ADDRESS = 'Blue gate behind market, Gurugram, Haryana, India, 122102'
PIN = {'latitude': 28.42, 'longitude': 77.1}


class ChannelTextAddressTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.tenant = SimpleNamespace(id='tenant', pk='tenant')
        self.enterContext(patch('chatbot_core.channels.telegram_webhook.TenantInfo.objects.get',
                                return_value=self.tenant))
        self.telegram_queue = self.enterContext(patch('chatbot_core.channels.telegram_webhook.enqueue_user_message'))
        self.reply = self.enterContext(patch('chatbot_core.channels.telegram_webhook._try_send_error_message'))

    def telegram(self, **message):
        request = self.factory.post('/telegram/?token=test-token', {
            'message': {'chat': {'id': 123}, 'from': {'id': 456}, **message},
        }, content_type='application/json')
        response = telegram_webhook(request)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_telegram_text_is_queued_unchanged_without_location(self):
        self.assertEqual(self.telegram(text=ADDRESS, location=PIN)['status'], 'queued')
        tenant, user, payload = self.telegram_queue.call_args.args
        self.assertEqual((tenant, user), ('tenant', '456'))
        self.assertEqual(payload['text'], ADDRESS)
        self.assertNotIn('location', payload)
        self.reply.assert_not_called()

    def test_telegram_pin_or_venue_requests_text_without_queuing(self):
        for message in ({'location': PIN}, {'location': PIN, 'text': '  '},
                        {'venue': {'location': PIN, 'title': 'Home', 'address': ADDRESS}}):
            with self.subTest(message=message):
                self.assertEqual(self.telegram(**message)['reason'], 'unsupported_location')
                self.assertIn('street address, city, state, country, and pincode', self.reply.call_args.args[-1])
        self.telegram_queue.assert_not_called()

    def test_telegram_media_remains_supported_without_coordinates(self):
        self.assertEqual(self.telegram(voice={'file_id': 'audio'}, location=PIN)['status'], 'queued')
        payload = self.telegram_queue.call_args.args[-1]
        self.assertEqual(payload['media'], {'id': 'audio', 'type': 'voice'})
        self.assertNotIn('location', payload)

    def test_adapters_ignore_coordinates_even_in_old_queued_messages(self):
        for adapter in (TelegramAdapterImpl(), VoiceAssistantAdapter()):
            for location in (PIN, {'latitude': 500}, 'malformed'):
                with self.subTest(adapter=adapter.name, location=location):
                    self.assertEqual(adapter.augment_text(ADDRESS, {'location': location}), ADDRESS)
                    result = adapter.augment_text('', {'location': location})
                    self.assertNotIn('latitude', result)
                    self.assertNotIn('lat=', result)

    def test_voice_api_only_enqueues_text_and_never_location(self):
        request = self.factory.post('/voice/', {'message': {'text': ADDRESS}, 'location': PIN},
                                    content_type='application/json')
        request.session = {}
        request._dont_enforce_csrf_checks = True
        with patch('users.tenant_access.authenticated_tenant', return_value=self.tenant), \
                patch('chatbot_core.channels.voice_assistant.enqueue_user_message') as queue:
            self.assertEqual(voice_api(request).status_code, 200)
        payload = queue.call_args.args[-1]
        self.assertEqual(payload['text'], ADDRESS)
        self.assertNotIn('location', payload)

    def test_voice_location_without_text_is_rejected(self):
        request = self.factory.post('/voice/', {'location': PIN}, content_type='application/json')
        request._dont_enforce_csrf_checks = True
        with patch('users.tenant_access.authenticated_tenant', return_value=self.tenant), \
                patch('chatbot_core.channels.voice_assistant.enqueue_user_message') as queue:
            self.assertEqual(voice_api(request).status_code, 400)
        queue.assert_not_called()
