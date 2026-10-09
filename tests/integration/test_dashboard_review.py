"""Regressions for operator controls, historical facts, and dashboard navigation."""
import json
import re
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import Mock, patch
from contextlib import ExitStack
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils.timezone import now
from redis.exceptions import ConnectionError

from chatbot_core.active_chats import append_message, get_messages, is_global_agent_enabled, set_latest_meta
from chatbot_core.models import TenantInfo
from commerce.models import AcceptedOrder, Location, StockItem
from orders.models import AddonGroup, AddonItem, ChatSession, Customer, MenuItem, MenuItemVariant, Order, OrderItem, OrderItemAddon
from users.catalog_fields import CATALOG_FIELDS
from users.models import TenantProfile


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.urls.append(dict(attrs).get('href', ''))


class DashboardReviewTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Review Cafe', approval_status='APPROVED')
        self.user = User.objects.create_user('review-owner', password='review-password')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Latte')
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size='Large', price='5')
        self.customer = Customer.objects.create(tenant=self.tenant, name='Customer', phone='1234567890')

    def test_toggle_failures_return_503_and_success_requires_persistence(self):
        for route, method, payload in (
            ('tenant:tenant_chats_toggle_api', 'hset', {'chat_id': '123'}),
            ('tenant:tenant_chats_toggle_global_api', 'set', {}),
        ):
            with self.subTest(route=route), patch('chatbot_core.active_chats._r') as redis:
                getattr(redis, method).side_effect = ConnectionError('Unavailable')
                with self.assertLogs('users.views', level='ERROR'):
                    response = self.client.post(reverse(route), json.dumps({**payload, 'enabled': False}), content_type='application/json')
                self.assertEqual(response.status_code, 503)
                self.assertIs(response.json()['ok'], False)
                getattr(redis, method).side_effect = None
                response = self.client.post(reverse(route), json.dumps({**payload, 'enabled': False}), content_type='application/json')
                self.assertEqual(response.json(), {'ok': True, 'enabled': False})

    def test_identical_messages_get_distinct_stable_ids(self):
        with patch('chatbot_core.active_chats._r') as redis:
            for _ in range(2):
                append_message(str(self.tenant.pk), 'voice', 'test', direction='out', text='Same reply', ts=123)
            raw = [call.args[1] for call in redis.rpush.call_args_list]
            redis.lrange.return_value = raw
            first = get_messages(str(self.tenant.pk), 'voice', 'test')
            second = get_messages(str(self.tenant.pk), 'voice', 'test')
        self.assertNotEqual(first[0]['id'], first[1]['id'])
        self.assertEqual(first, second)

    def test_owner_delivery_reports_transcript_write_failure(self):
        self.tenant.telegram_bot_token = 'test-bot-token'
        self.tenant.save()
        adapter = Mock()
        with patch('users.views.get_adapter', return_value=adapter), patch('chatbot_core.active_chats._r') as redis:
            redis.rpush.side_effect = ConnectionError('Unavailable')
            with self.assertLogs('users.views', level='ERROR'):
                response = self.client.post(reverse('tenant:tenant_chats_send_api'),
                    json.dumps({'chat_id': '123', 'text': 'Hello'}), content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])
        self.assertTrue(response.json()['delivered'])
        adapter.send_text.assert_called_once()

    def test_message_write_errors_propagate(self):
        with patch('chatbot_core.active_chats._r') as redis:
            redis.rpush.side_effect = ConnectionError('Unavailable')
            with self.assertRaises(ConnectionError):
                append_message(str(self.tenant.pk), 'voice', 'test', direction='in', text='Hello')

    def test_global_status_read_failure_is_not_reported_as_on(self):
        with patch('chatbot_core.active_chats._r') as redis:
            redis.get.side_effect = ConnectionError('Unavailable')
            with self.assertRaises(ConnectionError):
                is_global_agent_enabled(str(self.tenant.pk), 'telegram')
            for route in ('tenant:tenant_chats_global_status_api', 'tenant:tenant_chats_list_api'):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('enabled', response.json())
            redis.get.side_effect = None
            for value, enabled in ((None, True), (b'0', False), (b'1', True)):
                redis.get.return_value = value
                self.assertEqual(is_global_agent_enabled(str(self.tenant.pk), 'telegram'), enabled)

    def test_omitted_basket_does_not_write_metadata_but_empty_basket_does(self):
        with patch('chatbot_core.active_chats._r') as redis:
            set_latest_meta(str(self.tenant.pk), 'voice', 'test', None)
            redis.hset.assert_not_called()
            set_latest_meta(str(self.tenant.pk), 'voice', 'test', [])
            self.assertEqual(redis.hset.call_args.kwargs['mapping'], {'latest_meta': '[]'})

    def test_processor_preserves_basket_when_route_omits_it_or_fails(self):
        from chatbot_core import processor
        tenant = SimpleNamespace(id=2)
        customer = SimpleNamespace(id=3, name='Customer', phone='123')
        payload = {'tenant_id': '2', 'user_id': 'user', 'channel': 'voice', 'chat_id': 'chat', 'text': 'hi'}
        for result in (None, [], [{'name': 'Latte'}], ValueError('Route failed')):
            with self.subTest(result=result), ExitStack() as stack:
                adapter = Mock()
                adapter.augment_text.side_effect = lambda text, _: text
                stack.enter_context(patch.object(processor.TenantInfo.objects, 'get', return_value=tenant))
                for name, value in (('get_adapter', adapter), ('_call_create_or_get_customer_safe', customer),
                                    ('is_global_agent_enabled', True), ('is_agent_enabled', True)):
                    stack.enter_context(patch.object(processor, name, return_value=value))
                stack.enter_context(patch.object(processor, 'append_message'))
                stack.enter_context(patch.object(processor, 'touch_active_chat'))
                stack.enter_context(patch.object(processor, 'RedisSessionStore'))
                meta = stack.enter_context(patch.object(processor, 'set_latest_meta'))
                route = stack.enter_context(patch.object(processor, 'route_message_for_tenant'))
                if isinstance(result, Exception):
                    route.side_effect = result
                    stack.enter_context(self.assertLogs('chatbot_core.processor', level='ERROR'))
                else:
                    route.return_value = ('Reply', result)
                processor.process_payload('2', 'user', dict(payload))
                if isinstance(result, list):
                    meta.assert_called_once_with('2', 'voice', 'chat', result)
                else:
                    meta.assert_not_called()

    def test_transcript_outage_does_not_resend_a_delivered_bot_reply(self):
        from chatbot_core import processor
        adapter = Mock()
        adapter.augment_text.side_effect = lambda text, _: text
        with ExitStack() as stack:
            for name, value in (('get_adapter', adapter), ('_call_create_or_get_customer_safe', self.customer),
                                ('is_global_agent_enabled', True), ('is_agent_enabled', True),
                                ('route_message_for_tenant', ('Reply', None))):
                stack.enter_context(patch.object(processor, name, return_value=value))
            stack.enter_context(patch.object(processor, 'append_message', side_effect=ConnectionError('Unavailable')))
            stack.enter_context(patch.object(processor, 'touch_active_chat'))
            stack.enter_context(patch.object(processor, 'RedisSessionStore'))
            stack.enter_context(self.assertLogs('chatbot_core.processor', level='ERROR'))
            processor.process_payload(str(self.tenant.pk), 'user', {
                'tenant_id': str(self.tenant.pk), 'user_id': 'user', 'channel': 'voice', 'chat_id': 'chat', 'text': 'hi',
            })
        adapter.send_text.assert_called_once()
        self.assertEqual(adapter.send_text.call_args.args[1], 'Reply')

    def test_processor_does_not_route_when_global_agent_status_is_unavailable(self):
        from chatbot_core import processor
        adapter = Mock()
        adapter.augment_text.side_effect = lambda text, _: text
        with ExitStack() as stack:
            stack.enter_context(patch.object(processor, 'get_adapter', return_value=adapter))
            stack.enter_context(patch.object(processor, '_call_create_or_get_customer_safe', return_value=self.customer))
            stack.enter_context(patch.object(processor, 'append_message'))
            stack.enter_context(patch.object(processor, 'touch_active_chat'))
            stack.enter_context(patch.object(processor, 'is_global_agent_enabled', side_effect=ConnectionError('Unavailable')))
            route = stack.enter_context(patch.object(processor, 'route_message_for_tenant'))
            stack.enter_context(self.assertLogs('chatbot_core.processor', level='ERROR'))
            processor.process_payload(str(self.tenant.pk), 'user', {
                'tenant_id': str(self.tenant.pk), 'user_id': 'user', 'channel': 'voice', 'chat_id': 'chat', 'text': 'hi',
            })
        route.assert_not_called()
        adapter.send_text.assert_not_called()

    def test_protected_variant_redirects_with_actionable_feedback(self):
        location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        StockItem.objects.create(location=location, variant=self.variant)
        with patch('users.views._publish_menu') as publish:
            response = self.client.post(reverse('tenant:tenant_menu_variant_delete', args=[self.variant.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Variant Not Deleted')
        self.assertContains(response, 'Disable it instead of deleting it.')
        self.assertTrue(MenuItemVariant.objects.filter(pk=self.variant.pk).exists())
        publish.assert_not_called()

    def test_average_session_time_uses_elapsed_time_and_handles_empty_and_zero(self):
        url = reverse('tenant:tenant_analytics')
        self.assertIsNone(self.client.get(url).context['summary']['avg_duration'])
        start = now() - timedelta(days=3)
        for index, minutes in enumerate((2, 8)):
            session = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, session_id=str(index), platform='website')
            ChatSession.objects.filter(pk=session.pk).update(created_at=start, last_interaction_at=start + timedelta(minutes=minutes))
        response = self.client.get(url)
        self.assertEqual(response.context['summary']['avg_duration'], timedelta(minutes=5))
        self.assertContains(response, '0:05:00')
        ChatSession.objects.update(last_interaction_at=start - timedelta(seconds=1))
        self.assertContains(self.client.get(url), '0:00:00')

    def test_orders_use_accepted_currency_and_snapshot_customizations_after_catalog_changes(self):
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse', total_amount='12')
        location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        AcceptedOrder.objects.create(order=order, location=location, currency='USD', exponent=2, total_minor=1200,
            expires_at=now() + timedelta(minutes=5), snapshot_hash='test', snapshot={'pricing': {'lines': [
                {'name': 'Original Latte', 'size': 'Original Large', 'quantity': 2, 'unit_price': '5',
                 'modifiers': [{'name': 'Original Oat Milk', 'quantity': 1, 'unit_price': '1'}]},
            ]}})
        self.item.name = 'Renamed Latte'
        self.item.save()
        response = self.client.get(reverse('tenant:tenant_orders'))
        for text in ('USD 12.00', 'Original Latte', 'Original Large', 'Original Oat Milk', 'USD 5.00', 'USD 1.00 ea'):
            self.assertContains(response, text)
        self.assertNotContains(response, '₹')

    def test_legacy_orders_render_variants_addons_and_saved_currency(self):
        order = Order.objects.create(tenant=self.tenant, source='inhouse', total_amount='600', meta={'currency': 'JPY', 'exponent': 0})
        line = OrderItem.objects.create(order=order, item=self.item, variant=self.variant, item_name='Latte', quantity=1,
                                       unit_price='500', total_price='500')
        group = AddonGroup.objects.create(tenant=self.tenant, name='Milk')
        addon = AddonItem.objects.create(group=group, name='Oat', price='100')
        OrderItemAddon.objects.create(order_item=line, addon=addon, addon_name='Oat', quantity=2, unit_price='100', total_price='200')
        addon.name = 'Almond'
        addon.save()
        self.variant.size = 'Grande'
        self.variant.save()
        line.item_name = 'Latte (Large)'
        line.save()
        response = self.client.get(reverse('tenant:tenant_orders'))
        for text in ('JPY 600', 'Latte (Large)', 'Oat × 2 per item (JPY 100 ea)'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'Grande')
        self.assertNotContains(response, 'Almond')
        self.assertNotContains(response, 'JPY 600.00')
        addon.delete()
        self.assertContains(self.client.get(reverse('tenant:tenant_orders')), 'Oat × 2 per item')

    def test_unknown_historical_modifier_names_do_not_imply_removal(self):
        order = Order.objects.create(tenant=self.tenant, source='inhouse', total_amount='6')
        line = OrderItem.objects.create(order=order, item=self.item, variant=self.variant, item_name='Latte', quantity=1,
                                       unit_price='5', total_price='5')
        OrderItemAddon.objects.create(order_item=line, quantity=1, unit_price='1', total_price='1')
        response = self.client.get(reverse('tenant:tenant_orders'))
        self.assertContains(response, 'Customization (original name unavailable)')
        self.assertNotContains(response, 'Removed option')

    def test_login_retains_requested_destination_and_rejects_external_redirect(self):
        self.client.logout()
        destination = reverse('tenant:tenant_orders')
        response = self.client.get(reverse('login'), {'next': destination})
        self.assertContains(response, f'name="next" value="{destination}"')
        self.assertEqual(response.content.decode().count('<main'), 1)
        for name in ('username', 'password'):
            match = re.search(rf'<input\b[^>]*\bname="{name}"[^>]*>', response.content.decode())
            self.assertIsNotNone(match)
            self.assertNotIn('style=', match.group(0))
        for next_url, expected in ((destination, destination), ('https://example.org/steal', '/accounts/profile/')):
            response = self.client.post(reverse('login'), {'username': self.user.username, 'password': 'review-password', 'next': next_url})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, expected)
            self.client.logout()

    def test_catalog_editors_share_server_defaults_and_menu_navigation(self):
        response = self.client.get(reverse('tenant:tenant_menu_item_detail', args=[self.item.pk]))
        self.assertEqual(response.context['active_page'], 'menu')
        self.assertContains(response, f'href="{reverse("tenant:tenant_menu")}" class="active" aria-current="page"')
        fields = response.context['catalog_fields']
        self.assertEqual([field['name'] for field in fields], [field[0] for field in CATALOG_FIELDS])
        for field in fields:
            self.assertContains(response, f'name="{field["name"]}"')
            if field['is_json']:
                self.assertContains(response, f'data-json-empty="{field["empty"]}"')
        for route, args in (('tenant:modifiers', []), ('tenant:item_modifiers', [self.item.pk]), ('tenant:menu_source', [])):
            self.assertEqual(self.client.get(reverse(route, args=args)).context['active_page'], 'menu')
        prices = re.findall(r'<input\b[^>]*\bname="price"[^>]*>', response.content.decode())
        self.assertEqual(len(prices), 2)
        for tag in prices:
            self.assertIn('min="0"', tag)
            self.assertIn('step="0.01"', tag)
        self.assertNotContains(response, 'variant_description')
        self.assertEqual(response.content.decode().count('name="variant_availability_present"'), 2)

    def test_shared_variant_fields_save_description_and_keep_legacy_name(self):
        payload = {
            'size': 'Large', 'price': '5.00', 'sort_order': 0,
            'variant_availability_present': '1', 'is_available': 'on',
        }
        update = reverse('tenant:tenant_menu_variant_update', args=[self.variant.pk])
        self.assertEqual(self.client.post(update, {**payload, 'description': 'Twelve ounce'}).status_code, 302)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.description, 'Twelve ounce')
        self.assertEqual(self.client.post(update, {**payload, 'variant_description': 'Legacy note'}).status_code, 302)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.description, 'Legacy note')

    def test_all_pagination_links_preserve_filters_and_other_paginators(self):
        request = RequestFactory().get('/commerce/operations/', {'status': 'resolved', 'page': '2', 'commands_page': '2', 'inbox_page': '2', 'search': 'a & b'})
        request.session = {}
        page = Paginator([{}] * 151, 50).get_page(2)
        html = render_to_string('commerce/operations.html', {
            'issues': [], 'commands': page, 'inbox': page, 'show_resolved': True,
        }, request=request)
        links = [parse_qs(urlsplit(url).query) for url in Links(html).urls if 'commands_page=' in url or 'inbox_page=' in url]
        self.assertEqual(len(links), 4)
        for query in links:
            self.assertEqual(query['status'], ['resolved'])
            self.assertEqual(query['page'], ['2'])
            self.assertEqual(query['search'], ['a & b'])
            self.assertEqual(sum(query[key] != ['2'] for key in ('commands_page', 'inbox_page')), 1)
