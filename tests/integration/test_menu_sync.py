"""External menu authority, retry semantics, checkout safety and dashboard isolation."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from io import StringIO
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase, SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.db import close_old_connections, connection as database, transaction, OperationalError
from django.urls import path, include, reverse
from django.utils import timezone

from chatbot_core.models import TenantInfo
from chatbot_core.knowledge_cache import generate_all_menu_payload
from chatbot_core.runtime_configuration import RuntimeConfiguration
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.catalog import load_catalog, validate_selection
from chatbot_core.logic.cafe.checkout import basket_total
from chatbot_core.logic.cafe.db_utils import create_order
from commerce.api import signature
from commerce.credentials import adapter_secret
from commerce.models import Connection, Location, MenuSource, ExternalMapping, Configuration
from commerce.menu_sync import configure_source, import_snapshot, assert_menu_fresh
from commerce.adapters.json_menu import send_snapshot
from orders.models import MenuItem, MenuItemVariant, MenuCategory, MenuCatalogMeta, Customer, AddonItem
from users.models import TenantProfile

urlpatterns = [path('', include('users.urls')), path('commerce/', include('commerce.urls'))]


@override_settings(ROOT_URLCONF='tests.integration.test_menu_sync')
class MenuSyncTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Cafe', approval_status='APPROVED')
        self.other = TenantInfo.objects.create(display_name='Other', approval_status='APPROVED')
        self.user = User.objects.create_user(username='owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        self.connection = Connection.objects.create(location=self.location, provider='json_menu', role='pos',
            account_id='menu-1', active=True, capabilities=['catalog.write'], secret_ref='managed:menu-test')
        self.source = configure_source(self.tenant.pk, mode='external', connection=self.connection)
        self.payload = {
            'schema_version': 1, 'complete': True, 'source_generation': str(self.source.generation),
            'sequence': 1, 'revision': 'r1', 'observed_at': timezone.now().isoformat(), 'currency': 'INR',
            'categories': [{'external_id': 'drinks', 'name': 'Drinks', 'available': True}],
            'modifier_groups': [{'external_id': 'milk', 'name': 'Milk', 'options': [
                {'external_id': 'oat', 'name': 'Oat milk', 'available': True, 'price': '20.00'}]}],
            'items': [{'external_id': 'latte', 'name': 'Latte', 'available': True, 'category_id': 'drinks',
                       'variants': [{'external_id': 'regular', 'name': 'Regular', 'available': True, 'price': '150.00'}],
                       'modifier_groups': [{'group_id': 'milk', 'min_selections': 0, 'max_selections': 1}]}],
        }

    def sync(self, payload=None):
        return import_snapshot(self.connection, payload or self.payload)

    def newer(self):
        result = deepcopy(self.payload)
        result.update(sequence=2, revision='r2', observed_at=timezone.now().isoformat())
        return result

    def basket(self):
        item = MenuItem.objects.get(tenant=self.tenant, is_available=True)
        variant = item.variants.get(is_available=True)
        selection = validate_selection(load_catalog(self.tenant.api_key), str(item.pk), str(variant.pk), 1, [])
        return Basket(items=[selection])

    def post_snapshot(self, payload=None, secret=None):
        body = json.dumps(payload or self.payload)
        path = f'/commerce/v1/connections/{self.connection.pk}/catalog/snapshot/'
        stamp = str(int(time.time()))
        return self.client.post(path, body, content_type='application/json',
            HTTP_X_COMMERCE_TIMESTAMP=stamp, HTTP_X_COMMERCE_SIGNATURE=signature(
                secret or adapter_secret(self.connection), stamp, 'POST', path, body.encode()))

    def test_signed_import_populates_existing_catalog_and_preserves_identity(self):
        category = MenuCategory.objects.create(tenant=self.tenant, name='Drinks')
        self.assertEqual(self.post_snapshot().status_code, 200)
        item = MenuItem.objects.get(tenant=self.tenant)
        variant = item.variants.get()
        self.assertEqual(item.category_fk_id, category.pk)
        metadata = MenuCatalogMeta.objects.create(menu_item=item, ingredients=['coffee'])
        item.meta = {'aliases': ['my latte']}
        item.save()
        payload = self.newer()
        payload['items'][0].update(name='Renamed latte')
        payload['items'][0]['variants'][0]['price'] = '170.00'
        self.sync(payload)
        item.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(item.name, 'Renamed latte')
        self.assertEqual(variant.price, Decimal('170'))
        self.assertEqual(item.meta['aliases'], ['my latte'])
        self.assertEqual(item.catalog_meta.pk, metadata.pk)
        self.assertEqual(MenuItem.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(ExternalMapping.objects.filter(connection=self.connection).count(), 5)

    def test_retry_is_idempotent_and_does_not_refresh_observation(self):
        self.sync()
        self.source.refresh_from_db()
        original = self.source.synced_at
        self.assertEqual(self.sync()['status'], 'unchanged')
        self.source.refresh_from_db()
        self.assertEqual(self.source.synced_at, original)
        changed = deepcopy(self.payload)
        changed['items'][0]['name'] = 'Conflict'
        with self.assertRaisesMessage(ValueError, 'Sequence already used'):
            self.sync(changed)
        self.sync(self.newer())
        with self.assertRaisesMessage(ValueError, 'older menu'):
            self.sync()

    def test_no_sync_stale_sync_inactive_connection_and_replays_block_checkout(self):
        with self.assertRaisesMessage(ValueError, 'refresh'):
            assert_menu_fresh(self.tenant.pk)
        self.sync()
        basket = self.basket()
        self.assertEqual(basket_total(basket, self.tenant), Decimal('150'))
        MenuSource.objects.filter(pk=self.source.pk).update(observed_at=timezone.now() - timedelta(hours=1))
        self.sync()  # Exact replay must not restore freshness.
        with self.assertRaisesMessage(ValueError, 'refresh'):
            basket_total(basket, self.tenant)
        customer = Customer.objects.create(tenant=self.tenant, name='Customer')
        with self.assertRaisesMessage(ValueError, 'refresh'):
            create_order(self.tenant, customer, basket, None)
        self.sync(self.newer())
        self.connection.active = False
        self.connection.save()
        with self.assertRaisesMessage(ValueError, 'refresh'):
            basket_total(basket, self.tenant)

    def test_price_change_requires_review_and_never_reprices_saved_order(self):
        self.sync()
        basket = self.basket()
        customer = Customer.objects.create(tenant=self.tenant, name='Customer')
        order = create_order(self.tenant, customer, basket, None)
        line = order.items.get()
        payload = self.newer()
        payload['items'][0]['variants'][0]['price'] = '170.00'
        self.sync(payload)
        with self.assertRaisesMessage(ValueError, 'price changed'):
            basket_total(basket, self.tenant)
        with self.assertRaisesMessage(ValueError, 'price changed'):
            create_order(self.tenant, customer, basket, None)
        line.refresh_from_db()
        self.assertEqual(line.unit_price, Decimal('150'))
        self.assertEqual(line.item_name, 'Latte (Regular)')

    def test_omitted_items_variants_and_modifiers_are_disabled_without_deletion(self):
        legacy = MenuItem.objects.create(tenant=self.tenant, name='Local legacy')
        foreign = MenuItem.objects.create(tenant=self.other, name='Other cafe item')
        self.sync()
        legacy.refresh_from_db()
        foreign.refresh_from_db()
        self.assertFalse(legacy.is_available)
        self.assertTrue(foreign.is_available)
        basket = self.basket()
        item = MenuItem.objects.get(pk=basket.items[0]['item_id'])
        variant = item.variants.get()
        addon = AddonItem.objects.get(group__tenant=self.tenant)
        customer = Customer.objects.create(tenant=self.tenant, name='Customer')
        order = create_order(self.tenant, customer, basket, None)
        payload = self.newer()
        payload['items'][0]['variants'][0]['external_id'] = 'replacement'
        payload['items'][0]['modifier_groups'] = []
        payload['modifier_groups'] = []
        self.sync(payload)
        variant.refresh_from_db()
        addon.refresh_from_db()
        self.assertFalse(variant.is_available)
        self.assertFalse(addon.is_available)
        self.assertEqual(item.variants.count(), 2)
        self.assertEqual(order.items.get().variant_id, variant.pk)
        self.assertFalse(item.addon_groups.exists())
        with self.assertRaises(ValueError):
            basket_total(basket, self.tenant)
        payload.update(sequence=3, revision='empty', items=[], categories=[])
        self.sync(payload)
        item.refresh_from_db()
        self.assertFalse(item.is_available)
        self.assertEqual(load_catalog(self.tenant.api_key), {})
        self.assertEqual(order.items.get().item_id, item.pk)

    def test_bad_snapshots_cannot_partially_replace_a_good_menu(self):
        self.sync()
        variants = [
            {'currency': 'USD'}, {'complete': False}, {'complete': 1},
            {'observed_at': (timezone.now() - timedelta(hours=1)).isoformat()},
            {'observed_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            {'observed_at': '2026-09-22T10:00:00'},
        ]
        for change in variants:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.sync({**self.newer(), **change})
        for price in ('NaN', '-1', '1.001', '100000000.00'):
            payload = self.newer()
            payload['items'][0]['variants'][0]['price'] = price
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.sync(payload)
        for field, value in [('category_id', 'missing'), ('variants', []), ('external_id', '')]:
            payload = self.newer()
            payload['items'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.sync(payload)
        self.source.refresh_from_db()
        self.assertEqual(self.source.sequence, 1)
        self.assertEqual(basket_total(self.basket(), self.tenant), Decimal('150'))

    def test_mapping_to_other_tenant_rolls_back_prior_writes(self):
        self.sync()
        foreign = MenuItem.objects.create(tenant=self.other, name='Private')
        ExternalMapping.objects.filter(connection=self.connection, kind='item').update(canonical_id=str(foreign.pk))
        payload = self.newer()
        payload['categories'][0]['name'] = 'Should roll back'
        with self.assertRaisesMessage(ValueError, 'different tenant'):
            self.sync(payload)
        self.assertEqual(MenuCategory.objects.get(tenant=self.tenant).name, 'Drinks')
        self.assertTrue(MenuItem.objects.get(tenant=self.tenant).is_available)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, 'Private')

    def test_wrong_authority_and_source_generation_are_rejected(self):
        self.assertEqual(self.post_snapshot(secret='wrong').status_code, 401)
        configure_source(self.tenant.pk, mode='local')
        self.assertEqual(self.post_snapshot().status_code, 400)
        source = configure_source(self.tenant.pk, mode='external', connection=self.connection)
        self.assertNotEqual(source.generation, self.source.generation)
        self.assertEqual(self.post_snapshot().status_code, 400)
        self.payload['source_generation'] = str(source.generation)
        self.assertEqual(self.post_snapshot().status_code, 200)
        self.connection.capabilities = []
        self.connection.save()
        self.assertEqual(self.post_snapshot().status_code, 403)

    def test_same_settings_preserve_generation_and_freshness(self):
        self.sync()
        source = configure_source(self.tenant.pk, mode='external', connection=self.connection, max_age_seconds=60)
        self.assertEqual(source.generation, self.source.generation)
        self.assertEqual(source.sequence, 1)
        assert_menu_fresh(self.tenant.pk)

    def test_currency_change_requires_fresh_source_snapshot(self):
        self.sync()
        config = Configuration.objects.create(tenant=self.tenant, location=self.location)
        config.policy = {**config.policy, 'currency': 'USD'}
        config.save()
        with self.assertRaisesMessage(ValueError, 'refresh'):
            assert_menu_fresh(self.tenant.pk)
        payload = self.newer()
        payload['currency'] = 'USD'
        self.sync(payload)
        assert_menu_fresh(self.tenant.pk)

    def test_local_variant_can_be_disabled_and_reenabled(self):
        self.sync()
        variant = MenuItemVariant.objects.get(menu_item__tenant=self.tenant)
        configure_source(self.tenant.pk, mode='local')
        url = reverse('tenant:tenant_menu_variant_update', args=[variant.pk])
        data = {'size': variant.size, 'price': '150', 'sort_order': 0, 'variant_availability_present': '1'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        variant.refresh_from_db()
        self.assertFalse(variant.is_available)
        self.assertEqual(self.client.post(url, {**data, 'is_available': 'on'}).status_code, 302)
        variant.refresh_from_db()
        self.assertTrue(variant.is_available)

    def test_switching_back_to_local_import_handles_retained_names(self):
        archived = MenuItem.objects.create(tenant=self.tenant, name='Latte')
        self.sync()
        item = MenuItem.objects.get(tenant=self.tenant, is_available=True)
        old = item.variants.get()
        payload = self.newer()
        payload['items'][0]['variants'][0]['external_id'] = 'replacement'
        self.sync(payload)
        active = item.variants.get(is_available=True)
        configure_source(self.tenant.pk, mode='local')
        response = self.client.post(reverse('tenant:tenant_menu_ingest_json'), {'menu_items_json': json.dumps({
            'menu_items': [{'name': 'Latte', 'availability': {'quantity': 5}, 'pricing': {'Regular': '180'}}]})})
        self.assertEqual(response.status_code, 302)
        active.refresh_from_db()
        old.refresh_from_db()
        archived.refresh_from_db()
        self.assertEqual(active.price, Decimal('180'))
        self.assertFalse(old.is_available)
        self.assertFalse(archived.is_available)
        self.assertEqual(MenuItem.objects.filter(tenant=self.tenant).count(), 2)

    def test_local_edit_endpoints_and_import_command_are_blocked(self):
        self.sync()
        item = MenuItem.objects.get(tenant=self.tenant)
        variant = item.variants.get()
        category = item.category_fk
        group = item.addon_groups.get().group
        routes = [
            ('tenant_menu_ingest_json', {}), ('tenant_menu_item_update', {'item_id': item.pk}),
            ('tenant_menu_variant_add', {'item_id': item.pk}), ('tenant_menu_variant_update', {'variant_id': variant.pk}),
            ('tenant_menu_variant_delete', {'variant_id': variant.pk}), ('tenant_menu_category_add', {}),
            ('tenant_menu_category_update', {'category_id': category.pk}),
            ('tenant_menu_category_delete', {'category_id': category.pk}),
            ('modifiers', {}), ('modifier_edit', {'group_id': group.pk}),
            ('item_modifiers', {'item_id': item.pk}),
        ]
        for name, kwargs in routes:
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse('tenant:' + name, kwargs=kwargs), {}).status_code, 403)
        with self.assertRaisesMessage(CommandError, 'managed externally'):
            call_command('load_menu_items', tenant_id=str(self.tenant.pk), file='unused-menu.json', stdout=StringIO())
        self.assertTrue(MenuItem.objects.get(pk=item.pk).is_available)

    def test_local_enrichment_remains_editable_and_external_forms_are_disabled(self):
        self.sync()
        item = MenuItem.objects.get(tenant=self.tenant)
        self.assertEqual(self.client.post(reverse('tenant:tenant_menu_item_catalog_update', args=[item.pk]),
            {'ingredients': '["coffee"]'}).status_code, 302)
        self.assertEqual(item.catalog_meta.ingredients, ['coffee'])
        response = self.client.get(reverse('tenant:tenant_menu_item_detail', args=[item.pk]))
        self.assertContains(response, '<fieldset disabled>')
        response = self.client.get(reverse('tenant:tenant_menu'))
        self.assertNotContains(response, 'name="menu_items_json"')

    def test_source_form_is_tenant_scoped_and_local_mode_restores_edits(self):
        location = Location.objects.create(tenant=self.other, code='main', name='Other')
        foreign = Connection.objects.create(location=location, role='pos', active=True,
            provider='other', account_id='other', capabilities=['catalog.write'], secret_ref='managed:other')
        url = reverse('tenant:menu_source')
        response = self.client.post(url, {'mode': 'external', 'connection': str(foreign.pk), 'max_age_seconds': 900})
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(ValidationError):
            configure_source(self.tenant.pk, mode='external', connection=foreign)
        response = self.client.post(url, {'mode': 'local', 'connection': '', 'max_age_seconds': 900})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse('tenant:tenant_menu_category_add'), {'name': 'Local', 'sort_order': 0, 'is_active': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MenuCategory.objects.filter(tenant=self.tenant, name='Local').exists())

    def test_menu_answers_use_synced_prices_not_published_uploaded_prices(self):
        self.sync()
        configuration = RuntimeConfiguration(str(self.tenant.pk), self.tenant.api_key, self.tenant.slug, 1,
            [{'dtype': 'knowledge', 'intent': 'menu_items', 'sub_intent': 'pricing', 'payload': {'Latte': '1'}}])
        document = configuration.document('knowledge', 'menu_items', 'pricing')
        self.assertEqual(document['payload']['topic']['Latte']['Regular'], '₹150')
        self.assertIsNone(generate_all_menu_payload(self.tenant.api_key)[self.tenant.api_key]['Latte']['available_quantity'])
        payload = self.newer()
        payload['items'][0]['variants'][0]['available'] = False
        self.sync(payload)
        document = configuration.document('knowledge', 'menu_items', 'pricing')
        self.assertNotIn('Latte', document['payload']['topic'])
        self.assertEqual(document['payload']['catalog']['Latte']['variants'], [])
        MenuSource.objects.filter(pk=self.source.pk).update(observed_at=timezone.now() - timedelta(hours=1))
        self.assertIn('menu_status', configuration.document('knowledge', 'menu_items', 'pricing')['payload'])

    def test_cross_topic_retrieval_replaces_uploaded_menu_and_refreshes_synced_facts(self):
        from chatbot_core import knowledge_retrieval as retrieval
        from chatbot_core.knowledge_cache import get_knowledge_base_cache
        get_knowledge_base_cache().clear()
        self.addCleanup(get_knowledge_base_cache().clear)
        self.sync()
        configuration = RuntimeConfiguration(str(self.tenant.pk), self.tenant.api_key, self.tenant.slug, 1, [
            {'dtype': 'intent_classification', 'intent': 'information_about_the_cafe', 'sub_intent': 'overview',
             'payload': {'description': 'Cafe questions', 'required_knowledge': ['menu_items/pricing']}},
            {'dtype': 'knowledge', 'intent': 'information_about_the_cafe', 'sub_intent': 'overview',
             'payload': 'Call public support'},
            {'dtype': 'knowledge', 'intent': 'menu_items', 'sub_intent': 'pricing',
             'payload': 'Obsolete uploaded price'},
            {'dtype': 'knowledge', 'intent': 'menu_items', 'sub_intent': 'allergens',
             'payload': 'Obsolete allergen guarantee'},
        ])
        with patch.object(retrieval, 'get_configuration', return_value=configuration):
            def retrieve():
                return retrieval.retrieve_knowledge(self.tenant.api_key, 'information_about_the_cafe',
                                                    'overview', 'Latte price and category?')
            first = retrieve()
            self.assertIn('₹150', retrieval.encoded(first))
            self.assertIn('Drinks', retrieval.encoded(first))
            self.assertNotIn('Obsolete', retrieval.encoded(first))
            updated = self.newer()
            updated['items'][0]['variants'][0]['price'] = '175.00'
            self.sync(updated)
            second = retrieve()
            self.assertIn('₹175', retrieval.encoded(second))
            self.assertNotEqual(first['identity'], second['identity'])
            MenuSource.objects.filter(pk=self.source.pk).update(observed_at=timezone.now() - timedelta(hours=1))
            stale = retrieve()
            self.assertIn('menu_status', retrieval.encoded(stale))
            self.assertNotIn('₹175', retrieval.encoded(stale))
            self.assertNotIn('Obsolete', retrieval.encoded(stale))
            self.assertIn('Call public support', retrieval.encoded(stale))

    def test_retrieval_rejects_catalog_changed_during_assembly(self):
        from chatbot_core import knowledge_retrieval as retrieval
        from users.utils import generate_menu_items_json
        self.sync()
        configuration = RuntimeConfiguration(str(self.tenant.pk), self.tenant.api_key, self.tenant.slug, 1, [])

        def read_during_sync(tenant):
            result = generate_menu_items_json(tenant)
            self.sync(self.newer())
            return result

        with patch('users.utils.generate_menu_items_json', side_effect=read_during_sync):
            result = retrieval._live_menu(configuration)
        self.assertIn('menu_status', result['menu_items/status'])
        self.assertNotIn('₹150', retrieval.encoded(result))

    def test_answer_generator_receives_full_live_catalog_and_option_updates(self):
        import importlib
        from django.core.cache import cache
        from chatbot_core import knowledge_retrieval as retrieval
        from chatbot_core.knowledge_cache import get_knowledge_base_cache
        knowledge = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')
        cache.clear()
        get_knowledge_base_cache().clear()
        self.addCleanup(get_knowledge_base_cache().clear)
        self.payload['items'][0]['description'] = 'Espresso with steamed milk.'
        self.sync()
        item = MenuItem.objects.get(tenant=self.tenant, name='Latte')
        item.meta = {'aliases': ['Cafe au lait']}
        item.save(update_fields=['meta'])
        variant = item.variants.get()
        variant.aliases = ['standard cup']
        variant.save(update_fields=['aliases'])
        addon = AddonItem.objects.get(group__tenant=self.tenant, name='Oat milk')
        addon.aliases = ['oat drink']
        addon.save(update_fields=['aliases'])
        configuration = RuntimeConfiguration(str(self.tenant.pk), self.tenant.api_key, self.tenant.slug, 1, [
            {'dtype': 'intent_classification', 'intent': 'menu_items', 'sub_intent': 'pricing',
             'payload': {'description': 'Item and option prices'}},
            {'dtype': 'knowledge', 'intent': 'menu_items', 'sub_intent': 'pricing',
             'payload': 'Obsolete uploaded prices'},
        ])
        chain = Mock(invoke=Mock(return_value='Documented item and option prices.'))
        with patch.object(retrieval, 'get_configuration', return_value=configuration), \
             patch.object(knowledge, 'get_intent_prompt_cache', return_value={}), \
             patch.object(knowledge, 'enqueue_string'), \
             patch.object(knowledge, 'text_chain', return_value=chain) as factory:
            def answer():
                knowledge.generate_response_from_knowledge(self.tenant.api_key, 'pricing',
                    'Describe Cafe au lait, its sizes and milk options with prices.',
                    main_intent='menu_items', response_profile='menu_items')
                prompt = factory.call_args.args[0]
                evidence = json.loads(prompt.split('Here is the knowledge you MUST rely on:\n', 1)[1])
                return next(fragment['value']['data'] for fragment in evidence['fragments']
                            if fragment['source'] == 'menu_items/catalog')

            catalog = answer()
            self.assertEqual(catalog['currency'], 'INR')
            latte = catalog['items']['Latte']
            self.assertEqual(latte['description'], 'Espresso with steamed milk.')
            self.assertEqual(latte['aliases'], ['Cafe au lait'])
            self.assertEqual(latte['category'], 'Drinks')
            self.assertEqual(latte['variants'][0]['aliases'], ['standard cup'])
            group = latte['modifier_groups'][0]
            self.assertEqual((group['min'], group['max']), (0, 1))
            self.assertEqual(group['options'][0]['aliases'], ['oat drink'])
            self.assertEqual(group['options'][0]['price'], '20.00')
            updated = self.newer()
            updated['modifier_groups'][0]['options'][0]['price'] = '35.00'
            self.sync(updated)
            self.assertEqual(answer()['items']['Latte']['modifier_groups'][0]['options'][0]['price'], '35.00')
            updated.update(sequence=3, revision='r3', observed_at=timezone.now().isoformat())
            updated['modifier_groups'][0]['options'][0]['available'] = False
            self.sync(updated)
            self.assertEqual(answer()['items']['Latte']['modifier_groups'][0]['options'], [])
        self.assertEqual(chain.invoke.call_count, 3)


class JsonAdapterTests(SimpleTestCase):
    def test_adapter_preserves_source_revision_and_observation_on_retries(self):
        client = Mock()
        payload = {'complete': True, 'sequence': 4, 'observed_at': '2026-09-22T01:00:00Z'}
        send_snapshot(client, payload)
        client.request.assert_called_once_with('POST', 'catalog/snapshot/', payload)
        self.assertEqual(payload['observed_at'], '2026-09-22T01:00:00Z')
        with self.assertRaises(ValueError):
            send_snapshot(client, {'complete': False})

    def test_product_scope_does_not_dispatch_placeholder_handlers(self):
        from chatbot_core.logic.tenant_handlers.dispatcher import get_handler
        from users.forms import CreateTenantForm
        self.assertEqual(TenantInfo.BUSINESS_TYPES, [('cafe', 'Café / restaurant')])
        for kind in ('retail', 'bookings', 'informational', 'studio', 'solutions'):
            with self.subTest(kind=kind), self.assertRaisesMessage(NotImplementedError, 'Only café/restaurant'):
                get_handler(SimpleNamespace(business_type=kind), None, None)
        self.assertEqual(list(CreateTenantForm().fields['business_type'].choices)[-1], ('cafe', 'Café / restaurant'))


@override_settings(ROOT_URLCONF='tests.integration.test_menu_sync')
class MenuConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_order_creation_uses_the_same_lock_as_menu_import(self):
        MenuSyncTests.setUp(self)
        import_snapshot(self.connection, self.payload)
        basket = MenuSyncTests.basket(self)
        customer = Customer.objects.create(tenant=self.tenant, name='Customer')

        def create_while_locked():
            close_old_connections()
            try:
                with transaction.atomic():
                    with database.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '100ms'")
                    create_order(self.tenant, customer, basket, None)
            except OperationalError as exc:
                return 'lock timeout' in str(exc)
            finally:
                close_old_connections()
            return False

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                from commerce.menu_sync import lock_menu
                lock_menu(self.tenant.pk)
                self.assertTrue(pool.submit(create_while_locked).result(timeout=10))
        self.assertFalse(self.tenant.order_set.exists())
        order = create_order(self.tenant, customer, basket, None)
        self.assertEqual(order.total_amount, Decimal('150'))

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_imports_cannot_regress_sequence_or_duplicate_identity(self):
        MenuSyncTests.setUp(self)
        newer = deepcopy(self.payload)
        newer.update(sequence=2, revision='r2')
        newer['items'][0]['variants'][0]['price'] = '170.00'
        barrier = Barrier(2)

        def run(payload):
            close_old_connections()
            try:
                connection = Connection.objects.select_related('location').get(pk=self.connection.pk)
                barrier.wait(timeout=10)
                try:
                    return import_snapshot(connection, payload)['status']
                except ValueError as exc:
                    self.assertIn('older menu', str(exc))
                    return 'rejected'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, [self.payload, newer]))
        self.assertIn('applied', outcomes)
        self.source.refresh_from_db()
        self.assertEqual(self.source.sequence, 2)
        self.assertEqual(MenuItem.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(MenuItemVariant.objects.get(menu_item__tenant=self.tenant).price, Decimal('170'))
