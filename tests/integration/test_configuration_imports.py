"""Offline import parity, validation and atomicity checks."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from chatbot_core.configuration_files import CONFIGURATION_FILES, catalog_knowledge, knowledge_exports
from chatbot_core.configuration_imports import import_configuration
from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from chatbot_core.runtime_configuration import publish_configuration
from commerce.models import Configuration
from orders.models import CheckoutSettings, MenuItem, MenuItemVariant
from users.models import TenantProfile

ROOT = Path(__file__).resolve().parents[2] / 'test_data'


class ConfigurationImportTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Import cafe', approval_status='APPROVED')
        user = get_user_model().objects.create_user(username='import-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)

    def upload(self, kind, source):
        if kind == 'catalog':
            response = self.client.post(reverse('tenant:tenant_menu_ingest_json'), {'menu_items_json': source})
        else:
            if kind in {'knowledge', 'intent_classification', 'response_intents'}:
                data = json.loads(source)
                if 'document_type' not in data:
                    source = json.dumps({'document_type': kind, 'documents': data})
            response = self.client.post(reverse('tenant:upload_knowledge_prompt'), {'dtype': kind, 'json_blob': source})
        self.assertEqual(response.status_code, 302)
        return response

    def catalog(self, tenant):
        return list(MenuItemVariant.objects.filter(menu_item__tenant=tenant).order_by('menu_item__name', 'sort_order')
            .values('menu_item__name', 'menu_item__category_fk__name', 'menu_item__quantity',
                    'menu_item__is_available', 'menu_item__description', 'menu_item__meta',
                    'menu_item__catalog_meta__dietary_preferences', 'menu_item__catalog_meta__allergens',
                    'size', 'price', 'volume_ml', 'weight_grams', 'description', 'aliases', 'is_available'))

    def test_ui_import_and_publish_matches_provisioner_configuration(self):
        from evaluate.fixtures.provision import DjangoProvisioner
        imported = TenantInfo.objects.create(display_name='Provisioned cafe', approval_status='APPROVED')
        DjangoProvisioner.import_configuration(imported, ROOT)
        expected = publish_configuration(imported.pk, expected_version=0)
        for kind in ('commerce_policy', 'catalog', 'intent_classification', 'response_intents', 'checkout'):
            self.upload(kind, (ROOT / CONFIGURATION_FILES[kind]).read_text())
        for filename in ('01_cafe_knowledge.json', '02_menu_knowledge.json', '03_ordering_knowledge.json'):
            self.upload('knowledge', (ROOT / filename).read_text())
        response = self.client.post(reverse('tenant:tenant_knowledge'), {'action': 'publish', 'version': '0'})
        self.assertEqual(response.status_code, 302)
        actual = TenantRuntimeConfiguration.objects.get(tenant=self.tenant)
        self.assertEqual(actual.version, 1)
        self.assertEqual(actual.documents, expected.documents)
        self.assertEqual(len(self.catalog(self.tenant)), 26)
        self.assertEqual(self.catalog(self.tenant), self.catalog(imported))
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration,
                         CheckoutSettings.objects.get(tenant=imported).configuration)
        self.assertEqual(Configuration.objects.get(tenant=self.tenant).policy,
                         Configuration.objects.get(tenant=imported).policy)

    def test_generated_exports_match_committed_files(self):
        for filename, value in knowledge_exports(ROOT).items():
            self.assertEqual(json.loads((ROOT / filename).read_text()), value, filename)

    def test_policy_import_updates_live_limits_and_preserves_pricing_activation(self):
        from chatbot_core.logic.cafe.basket import Basket
        from chatbot_core.logic.cafe.ordering_limits import load_policy, limit_reason
        from commerce.policy import evaluation_policy
        from commerce.services import basket_quote
        item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Regular', price='100')
        basket = Basket(items=[{'item_id': str(item.pk), 'item_variant_id': str(variant.pk),
                               'quantity': 2, 'unit_price': '100'}])
        policy = evaluation_policy(taxes=[{'code': 'TAX', 'name': 'Tax', 'rate': '10'}])
        policy['ordering_limits']['max_line_quantity'] = 1
        config = import_configuration(self.tenant, 'commerce_policy', policy)
        self.assertFalse(config.local_checkout)
        self.assertFalse(config.enabled)
        self.assertIn('at most 1', limit_reason(basket.items, load_policy(tenant_id=self.tenant.pk)))
        self.assertIsNone(basket_quote(self.tenant, basket, mode='pickup'))
        for local, external in ((True, False), (False, True), (False, False)):
            with self.subTest(local=local, external=external):
                config.local_checkout, config.enabled = local, external
                config.save()
                config = import_configuration(self.tenant, 'commerce_policy', policy)
                self.assertEqual((config.local_checkout, config.enabled), (local, external))
                quote = basket_quote(self.tenant, basket, mode='pickup')
                if local or external:
                    self.assertEqual(quote['tax_minor'], 2000)
                else:
                    self.assertIsNone(quote)

    def test_reimports_are_idempotent_and_zero_quantity_is_saved(self):
        data = json.loads((ROOT / CONFIGURATION_FILES['catalog']).read_text())
        for row in data['menu_items']:
            row['availability']['quantity'] = 0
        import_configuration(self.tenant, 'catalog', data)
        item = MenuItem.objects.filter(tenant=self.tenant).first()
        MenuItem.objects.filter(pk=item.pk).update(quantity=12)
        import_configuration(self.tenant, 'catalog', data)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 0)
        self.assertTrue(item.is_available)
        self.assertEqual(MenuItem.objects.filter(tenant=self.tenant).count(), 26)
        self.assertEqual(MenuItemVariant.objects.filter(menu_item__tenant=self.tenant).count(), 26)

    def test_changed_variant_prices_reach_export_and_published_knowledge(self):
        data = json.loads((ROOT.parent / 'demo/menu_catalog.json').read_text())
        row = data['menu_items'][0]
        row['pricing']['Family Size'] = '999.50'
        self.assertEqual(data['knowledge']['pricing']['variants_by_item'][row['name']]['Family Size'], '890')
        generated = catalog_knowledge(data)['menu_items']['pricing']
        self.assertEqual(generated['variants_by_item'][row['name']]['Family Size'], '999.5')

        self.upload('catalog', json.dumps(data))
        publication = publish_configuration(self.tenant.pk, expected_version=0)
        pricing = next(doc['payload'] for doc in publication.documents
                       if doc['intent'] == 'menu_items' and doc['sub_intent'] == 'pricing')
        variant = MenuItemVariant.objects.get(menu_item__tenant=self.tenant,
            menu_item__name=row['name'], size='Family Size')
        self.assertEqual(pricing, generated)
        self.assertEqual(variant.price, Decimal('999.50'))
        self.assertEqual(pricing['items'][row['name']]['listed_price'], 380)

    def test_partial_catalog_upsert_preserves_retained_items_and_variants_in_knowledge(self):
        data = {'currency': 'INR', 'knowledge': {
            'pricing': {'variants_by_item': {}, 'note': 'Reviewed menu prices.'},
            'availability': {'live_stock_verified': False},
        }, 'menu_items': [
            {'name': 'Tea', 'menu_category': 'Drinks', 'availability': {'quantity': 5},
             'pricing': {'Regular': '10', 'Large': '20'},
             'listed_variant': 'Regular', 'listed_size': 'Regular'},
            {'name': 'Coffee', 'menu_category': 'Drinks', 'availability': {'quantity': 5},
             'pricing': {'Regular': '30', 'Large': '40'},
             'listed_variant': 'Large', 'listed_size': 'Large'},
        ]}
        import_configuration(self.tenant, 'catalog', data)
        previous_ids = set(MenuItemVariant.objects.filter(menu_item__tenant=self.tenant)
                           .values_list('pk', flat=True))
        # Items entered in the dashboard also survive an import.
        manual = MenuItem.objects.create(tenant=self.tenant, name='Cake')
        MenuItemVariant.objects.create(menu_item=manual, size='Slice', price='50')
        hidden = MenuItem.objects.create(tenant=self.tenant, name='Hidden', is_available=False)
        MenuItemVariant.objects.create(menu_item=hidden, size='Regular', price='60')
        data['menu_items'] = data['menu_items'][:1]
        data['menu_items'][0]['pricing'] = {'Regular': '11'}
        self.upload('catalog', json.dumps(data))
        publication = publish_configuration(self.tenant.pk, expected_version=0)
        knowledge = {doc['sub_intent']: doc['payload'] for doc in publication.documents
                     if doc['dtype'] == 'knowledge' and doc['intent'] == 'menu_items'}
        self.assertTrue(previous_ids <= set(MenuItemVariant.objects.filter(menu_item__tenant=self.tenant)
                                           .values_list('pk', flat=True)))
        self.assertEqual(set(knowledge['availability']['listed_items']), {'Tea', 'Coffee', 'Cake'})
        self.assertEqual(knowledge['pricing']['variants_by_item'], {
            'Tea': {'Regular': '11', 'Large': '20'},
            'Coffee': {'Regular': '30', 'Large': '40'},
            'Cake': {'Slice': '50'},
        })
        self.assertEqual(knowledge['pricing']['items']['Coffee'],
                         {'listed_price': 40, 'currency': 'INR', 'size': 'Large'})
        self.assertEqual(knowledge['pricing']['items']['Cake'],
                         {'listed_price': 50, 'currency': 'INR', 'size': None})
        self.assertEqual(set(knowledge['explore_options']['categories']['Drinks']), {'Tea', 'Coffee'})
        self.assertEqual(knowledge['menu_category']['Cake'], 'Uncategorized')
        self.assertEqual(knowledge['pricing']['note'], 'Reviewed menu prices.')
        self.assertIs(knowledge['availability']['live_stock_verified'], False)

    def test_invalid_prices_and_duplicate_names_leave_no_partial_catalog(self):
        base = json.loads((ROOT / CONFIGURATION_FILES['catalog']).read_text())
        for value in ('-10', 'NaN', 'Infinity', 'nonsense'):
            data = deepcopy(base)
            data['menu_items'][-1]['pricing']['QA standard'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                import_configuration(self.tenant, 'catalog', data)
        data = deepcopy(base)
        data['menu_items'].append(data['menu_items'][0])
        with self.assertRaises(ValueError):
            import_configuration(self.tenant, 'catalog', data)
        self.assertFalse(MenuItem.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant).exists())

    def test_knowledge_failure_rolls_back_catalog(self):
        with patch('chatbot_core.configuration_imports.import_documents', side_effect=ValueError('Invalid knowledge')):
            with self.assertRaises(ValueError):
                import_configuration(self.tenant, 'catalog', (ROOT / CONFIGURATION_FILES['catalog']).read_text())
        self.assertFalse(MenuItem.objects.filter(tenant=self.tenant).exists())

    def test_document_validation_is_shared_and_atomic(self):
        invalid = {'general': {'greeting': 'Hello', 'unknown_route': 'Invalid'}}
        with self.assertRaises(ValueError):
            import_configuration(self.tenant, 'intent_classification', invalid)
        self.upload('intent_classification', json.dumps(invalid))
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant).exists())
        for source in ('{"general": {}, "general": {}}', '{"general":{"greeting":NaN}}'):
            with self.assertRaises(ValueError):
                import_configuration(self.tenant, 'knowledge', source)

    def test_online_checkout_cannot_bypass_provider_readiness(self):
        data = json.loads((ROOT / CONFIGURATION_FILES['checkout']).read_text())
        data['online_provider'] = 'adapter'
        data['modes']['pickup']['payment_methods'] = ['online']
        with self.assertRaises(ValidationError):
            import_configuration(self.tenant, 'checkout', data)
        self.upload('checkout', json.dumps(data))
        self.assertFalse(CheckoutSettings.objects.filter(tenant=self.tenant).exists())
