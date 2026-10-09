"""Regressions for deployed chat throttling, CSP, and Telegram delivery."""
import json
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import include, path, reverse
from django.contrib.auth.models import User

from chatbot_core.channels.telegram import TelegramAdapterImpl
from chatbot_core.models import TenantInfo
from users.models import TenantProfile


def chat_stub(request):
    return HttpResponse('accepted')


chat_urls = ([path('chatbot-api/', chat_stub, name='chatbot_api')], 'chatbot_core')
urlpatterns = [
    path('agent_core/', include(chat_urls)),
    path('alternate/', include(chat_urls, namespace='alternate')),
    path('unrelated/', chat_stub),
]


@override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=[
    'chatbot_core.middleware.ChatbotRateLimitMiddleware',
])
class ChatRateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def token(self, slug):
        return jwt.encode({'tenant_slug': slug}, settings.JWT_SECRET, algorithm='HS256')

    def test_deployed_path_rejects_requests_31_through_35(self):
        self.assertEqual(reverse('chatbot_core:chatbot_api'), '/agent_core/chatbot-api/')
        statuses = [self.client.post('/agent_core/chatbot-api/',
                    HTTP_AUTHORIZATION='Bearer ' + self.token('cafe')).status_code for _ in range(35)]
        self.assertEqual(statuses, [200] * 30 + [429] * 5)

    def test_resolved_endpoint_is_limited_under_another_mount(self):
        statuses = [self.client.post('/alternate/chatbot-api/').status_code for _ in range(31)]
        self.assertEqual(statuses, [200] * 30 + [429])

    def test_tenants_have_separate_buckets_and_window_expires(self):
        with patch('chatbot_core.middleware.time.time', return_value=1000):
            for _ in range(30):
                self.client.post('/agent_core/chatbot-api/', HTTP_AUTHORIZATION='Bearer ' + self.token('one'))
            self.assertEqual(self.client.post('/agent_core/chatbot-api/', HTTP_AUTHORIZATION='Bearer ' + self.token('one')).status_code, 429)
            self.assertEqual(self.client.post('/agent_core/chatbot-api/', HTTP_AUTHORIZATION='Bearer ' + self.token('two')).status_code, 200)
        with patch('chatbot_core.middleware.time.time', return_value=1060):
            self.assertEqual(self.client.post('/agent_core/chatbot-api/', HTTP_AUTHORIZATION='Bearer ' + self.token('one')).status_code, 200)

    def test_other_views_and_get_requests_do_not_consume_budget(self):
        for _ in range(35):
            self.assertEqual(self.client.post('/unrelated/').status_code, 200)
            self.assertEqual(self.client.get('/agent_core/chatbot-api/').status_code, 200)
        self.assertEqual(self.client.post('/agent_core/chatbot-api/').status_code, 200)

    def test_invalid_token_is_rejected(self):
        self.assertEqual(self.client.post('/agent_core/chatbot-api/', HTTP_AUTHORIZATION='Bearer invalid').status_code, 401)


def telegram_response(status=200, body=None):
    response = requests.Response()
    response.status_code = status
    response.url = 'https://api.telegram.org/botprivate-token/sendMessage'
    response._content = json.dumps({'ok': True} if body is None else body).encode()
    return response


class TelegramDeliveryTests(SimpleTestCase):
    def test_text_and_voice_require_success_and_set_timeouts(self):
        adapter = TelegramAdapterImpl()
        payload = {'bot_token': 'private-token', 'chat_id': 123}
        with tempfile.NamedTemporaryFile(suffix='.ogg') as audio:
            for method, value in [(adapter.send_text, 'hello'), (adapter.send_voice, audio.name)]:
                for status, body in [(200, {'ok': True}), (403, {'ok': False}),
                                     (200, {'ok': False}), (200, {}), (200, []), (200, {'ok': 'true'})]:
                    with self.subTest(method=method.__name__, status=status, body=body), patch(
                            'chatbot_core.channels.telegram.requests.post', return_value=telegram_response(status, body)) as post:
                        if status == 200 and body == {'ok': True}:
                            self.assertIsNone(method(payload, value))
                        else:
                            with self.assertRaises(RuntimeError) as error:
                                method(payload, value)
                            self.assertNotIn('private-token', str(error.exception))
                        self.assertEqual(post.call_args.kwargs['timeout'], (5, 30))
                        self.assertEqual(post.call_count, 1)

    def test_timeout_connection_and_malformed_response_fail_without_retry(self):
        adapter = TelegramAdapterImpl()
        payload = {'bot_token': 'private-token', 'chat_id': 123}
        bad = telegram_response()
        bad._content = b'not json'
        for result in [requests.Timeout('private-token'), requests.ConnectionError('private-token'), bad]:
            with self.subTest(result=type(result)), patch('chatbot_core.channels.telegram.requests.post') as post:
                if isinstance(result, Exception):
                    post.side_effect = result
                else:
                    post.return_value = result
                with self.assertRaises(RuntimeError) as error:
                    adapter.send_text(payload, 'hello')
                self.assertNotIn('private-token', str(error.exception))
                self.assertEqual(post.call_count, 1)


class DashboardTelegramDeliveryTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Delivery Cafe', approval_status='APPROVED', telegram_bot_token='private-token')
        user = User.objects.create_user('delivery-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)

    def test_failed_delivery_is_not_recorded_as_success(self):
        for response in [telegram_response(403, {'ok': False}), telegram_response(200, {'ok': False})]:
            with patch('chatbot_core.channels.telegram.requests.post', return_value=response), patch('users.views.append_message') as append:
                result = self.client.post(reverse('tenant:tenant_chats_send_api'),
                    json.dumps({'chat_id': '123', 'text': 'Hello'}), content_type='application/json')
                self.assertEqual(result.status_code, 500)
                self.assertFalse(result.json()['ok'])
                self.assertNotIn('private-token', result.content.decode())
                append.assert_not_called()

    def test_successful_delivery_is_recorded_once(self):
        with patch('chatbot_core.channels.telegram.requests.post', return_value=telegram_response()), patch('users.views.append_message') as append:
            result = self.client.post(reverse('tenant:tenant_chats_send_api'),
                json.dumps({'chat_id': '123', 'text': 'Hello'}), content_type='application/json')
            self.assertEqual(result.json(), {'ok': True})
            append.assert_called_once()


class ScriptTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.violations = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') != 'application/json':
            src = attrs.get('src', '')
            if not src or src.startswith(('http:', 'https:', '//')):
                self.violations.append(('script', src))
        self.violations.extend((key, value) for key, value in attrs.items() if key.startswith('on'))


class TemplateCSPTests(SimpleTestCase):
    def test_first_party_templates_need_no_inline_execution_or_external_scripts(self):
        root = Path(__file__).resolve().parents[2]
        for app in ('users', 'chatbot_core', 'commerce'):
            for template in (root / app / 'templates').rglob('*.html'):
                with self.subTest(template=str(template.relative_to(root))):
                    parser = ScriptTags()
                    parser.feed(template.read_text())
                    self.assertEqual(parser.violations, [])
