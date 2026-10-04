"""Inventory evidence reaches knowledge prompts without reserving stock."""
from collections import OrderedDict
from datetime import timedelta
import importlib
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from chatbot_core import knowledge_retrieval as retrieval
from chatbot_core.models import TenantInfo
from chatbot_core.runtime_configuration import RuntimeConfiguration
from commerce.knowledge_inventory import inventory_knowledge
from commerce.models import Configuration, Location, MenuSource, StockItem, Reservation
from orders.models import AddonGroup, AddonItem, MenuCategory, MenuItem, MenuItemVariant
from tests.support.commerce import Fixtures
from tests.support.llm import ProviderHarness

knowledge = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')


class InventoryKnowledgeTests(Fixtures, ProviderHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.item.name = 'Vanilla'
        self.item.save(update_fields=['name'])
        documents = [
            {'dtype': 'knowledge', 'intent': 'menu_items', 'sub_intent': 'availability',
             'payload': {'Vanilla': {'quantity': 10, 'note': 'Undated menu quantity.'}}},
            *[{'dtype': 'intent_classification', 'intent': intent, 'sub_intent': topic, 'payload': {}}
              for intent, topic in [('menu_items', 'availability'),
                                    ('information_about_the_cafe', 'overview'),
                                    ('placing_order', 'order_channels_and_modes')]],
        ]
        self.runtime = RuntimeConfiguration(str(self.tenant.pk), self.tenant.api_key, 'test', 1, documents)
        self.enterContext(patch.object(retrieval, 'get_configuration', return_value=self.runtime))
        self.enterContext(patch.object(retrieval, '_indexes', OrderedDict()))
        self.enterContext(patch.object(knowledge, 'get_intent_prompt_cache', return_value={}))
        self.enterContext(patch.object(knowledge, 'enqueue_string'))
        self.payload = 'Vanilla is in stock.'

    def evidence(self):
        return inventory_knowledge(self.tenant.pk)

    def row(self):
        return next(row for row in self.evidence()['records'] if row.get('variant_id') == str(self.variant.pk))

    def answer(self, intent='menu_items', topic='availability', profile='menu_items'):
        return knowledge.generate_response_from_knowledge(
            self.tenant.api_key, topic, 'Is vanilla in stock?', main_intent=intent, response_profile=profile)

    def test_remaining_units_and_shared_pool_variant_precedence_match_checkout(self):
        self.stock.on_hand, self.stock.reserved, self.stock.pending_consumed = 9, 2, 3
        self.stock.save()
        large = MenuItemVariant.objects.create(menu_item=self.item, size='Large', price='20')
        rows = self.evidence()['records']
        self.assertEqual({r['available_units'] for r in rows}, {4})
        self.assertEqual({r['stock_pool_id'] for r in rows}, {str(self.stock.pk)})
        specific = StockItem.objects.create(location=self.location, variant=large, on_hand=0)
        row = next(r for r in self.evidence()['records'] if r['variant_id'] == str(large.pk))
        self.assertEqual((row['status'], row['available_units'], row['stock_pool_id']),
                         ('out_of_stock', 0, str(specific.pk)))
        self.assertEqual(self.row()['available_units'], 4)

    def test_unknown_stock_never_uses_legacy_menu_quantity(self):
        self.stock.delete()
        self.assertEqual(self.item.quantity, 10)
        self.assertEqual(self.row()['status'], 'unknown')
        self.assertIsNone(self.row()['available_units'])
        self.assertEqual(self.row()['reason'], 'stock_not_configured')

    def test_disabled_untracked_and_unconfigured_inventory_are_explicitly_unknown(self):
        self.config.enabled = False
        self.config.save()
        self.assertEqual(self.evidence()['reason'], 'commerce_disabled')
        self.config.enabled = True
        self.config.policy['stock_policy'] = 'untracked'
        self.config.save()
        self.assertEqual(self.evidence()['reason'], 'inventory_untracked')
        self.config.delete()
        self.assertEqual(self.evidence()['reason'], 'inventory_not_configured')
        self.assertEqual(self.evidence()['records'], [])

    def test_availability_flags_do_not_invent_counts_or_override_strict_policy(self):
        self.stock.mode = 'availability'
        self.stock.save()
        self.assertEqual(self.row()['reason'], 'numeric_stock_required')
        self.config.policy['stock_policy'] = 'availability'
        self.config.save()
        row = self.row()
        self.assertEqual(row['status'], 'in_stock')
        self.assertIsNone(row['available_units'])
        self.stock.available = False
        self.stock.save()
        self.assertEqual(self.row()['status'], 'out_of_stock')

    def test_provider_observations_require_freshness_and_active_authority(self):
        now = timezone.now()
        self.stock.authority = self.pos
        for observed, expected in [(None, 'unknown'), (now - timedelta(seconds=301), 'unknown'),
                                   (now + timedelta(seconds=61), 'unknown'), (now, 'in_stock')]:
            with self.subTest(observed=observed), patch('commerce.knowledge_inventory.timezone.now', return_value=now):
                self.stock.observed_at = observed
                self.stock.save()
                self.assertEqual(self.row()['status'], expected)
        self.pos.active = False
        self.pos.save()
        self.assertEqual(self.row()['reason'], 'inventory_provider_unavailable')

    def test_stale_external_menu_cannot_be_overridden_by_fresh_stock(self):
        MenuSource.objects.create(tenant=self.tenant, mode='external', connection=self.pos,
                                  observed_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.evidence()['reason'], 'menu_unavailable_or_stale')
        self.assertEqual(self.evidence()['records'], [])

    def test_menu_disabled_item_variant_and_category_are_not_physical_stock_claims(self):
        category = MenuCategory.objects.create(tenant=self.tenant, name='Ice creams')
        self.item.category_fk = category
        self.item.save()
        for subject, field in [(self.item, 'is_available'), (self.variant, 'is_available'),
                               (category, 'is_active')]:
            setattr(subject, field, False)
            subject.save()
            self.assertEqual(self.row()['status'], 'unavailable')
            self.assertIsNone(self.row()['available_units'])
            setattr(subject, field, True)
            subject.save()

    def test_configured_modifier_inventory_is_identified_separately(self):
        group = AddonGroup.objects.create(tenant=self.tenant, name='Toppings')
        addon = AddonItem.objects.create(group=group, name='Sprinkles', price='1')
        StockItem.objects.create(location=self.location, addon=addon, on_hand=3, reserved=1)
        modifier = next(r for r in self.evidence()['records'] if r['kind'] == 'modifier')
        self.assertEqual((modifier['modifier_name'], modifier['group_name'], modifier['available_units']),
                         ('Sprinkles', 'Toppings', 2))

    def test_tenant_and_location_inventory_are_isolated_even_with_invalid_foreign_subject(self):
        other = TenantInfo.objects.create(display_name='Other')
        other_location = Location.objects.create(tenant=other, code='other', name='Other')
        other_item = MenuItem.objects.create(tenant=other, name='Private item')
        MenuItemVariant.objects.create(menu_item=other_item, size='Private size', price='1')
        StockItem.objects.create(location=other_location, item=other_item, on_hand=9876)
        StockItem.objects.create(location=self.location, item=other_item, on_hand=9876)
        second_location = Location.objects.create(tenant=self.tenant, code='second', name='Second')
        StockItem.objects.create(location=second_location, item=self.item, on_hand=8765)
        evidence = retrieval.encoded(self.evidence())
        for private in ('Private item', '9876', '8765'):
            self.assertNotIn(private, evidence)
        Configuration.objects.filter(pk=self.config.pk).update(location=other_location)
        self.assertEqual(self.evidence()['reason'], 'inventory_not_configured')

    def test_stock_is_in_all_knowledge_profiles_and_does_not_reserve(self):
        for intent, topic, profile in [('menu_items', 'availability', 'menu_items'),
                                        ('information_about_the_cafe', 'overview', 'cafe_information'),
                                        ('placing_order', 'order_channels_and_modes', 'ordering_information')]:
            self.answer(intent, topic, profile)
            prompt = self.requests[-1]['messages'][0]['content']
            for text in ('"inventory":', '"item_name":"Vanilla"', '"status":"in_stock"',
                         '"available_units":1', 'ahead of static menu quantities', 'do not reserve stock'):
                self.assertIn(text, prompt)
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.reserved), (1, 0))
        self.assertFalse(Reservation.objects.exists())

    def test_answer_cache_refreshes_when_stock_or_reservations_change(self):
        self.assertEqual(self.answer(), 'Vanilla is in stock.')
        self.answer()
        self.assertEqual(len(self.requests), 1)
        self.stock.reserved = 1
        self.stock.save()
        self.payload = 'Vanilla is currently sold out.'
        self.assertEqual(self.answer(), self.payload)
        self.assertEqual(len(self.requests), 2)
        self.assertIn('"status":"out_of_stock"', self.requests[-1]['messages'][0]['content'])
        self.stock.reserved, self.stock.on_hand = 0, 2
        self.stock.save()
        self.answer()
        self.assertEqual(len(self.requests), 3)
        self.assertIn('"available_units":2', self.requests[-1]['messages'][0]['content'])

    def test_provider_stock_expiry_invalidates_cached_answer_without_database_changes(self):
        observed = timezone.now()
        self.stock.authority, self.stock.observed_at = self.pos, observed
        self.stock.save()
        with patch('commerce.knowledge_inventory.timezone.now', return_value=observed):
            self.answer()
            self.answer()
        self.assertEqual(len(self.requests), 1)
        with patch('commerce.knowledge_inventory.timezone.now', return_value=observed + timedelta(seconds=301)):
            self.payload = 'I cannot confirm current stock.'
            self.assertEqual(self.answer(), self.payload)
        self.assertEqual(len(self.requests), 2)
        prompt = self.requests[-1]['messages'][0]['content']
        self.assertIn('stock_observation_missing_or_stale', prompt)
        self.assertIn('"status":"unknown"', prompt)

    def test_inventory_is_available_without_static_knowledge_documents(self):
        self.runtime.documents[:] = [d for d in self.runtime.documents if d['dtype'] != 'knowledge']
        self.answer()
        self.assertIn('"status":"in_stock"', self.requests[-1]['messages'][0]['content'])
