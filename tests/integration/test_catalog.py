"""Catalog behavior, tenant boundaries, imports, and ordering without network I/O."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import mock_open, patch

from django.contrib.auth.models import User
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from chatbot_core import knowledge_cache
from chatbot_core.logic.cafe.catalog import load_catalog, validate_selection
from chatbot_core.llm.schemas import OrderProposal
from chatbot_core.models import TenantInfo, TenantJSONDoc
from orders.models import MenuCategory, MenuItem, MenuItemVariant, MenuCatalogMeta, Customer, ChatSession, Order
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
from users.models import TenantProfile
from users.utils import generate_menu_items_json

class CatalogTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name="Cafe", approval_status="APPROVED")
        self.other = TenantInfo.objects.create(display_name="Other Cafe", approval_status="APPROVED")
        self.user = User.objects.create_user(username="owner", password="password")
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.category = MenuCategory.objects.create(tenant=self.tenant, name="Coffee", sort_order=2)
        self.item = MenuItem.objects.create(tenant=self.tenant, name="Latte", category_fk=self.category)
        self.small = MenuItemVariant.objects.create(menu_item=self.item, size="Small", price="90", sort_order=0)
        self.large = MenuItemVariant.objects.create(menu_item=self.item, size="Large", price="140", sort_order=1)
        self.addCleanup(knowledge_cache.get_item_pricing_cache().clear)
        self.addCleanup(knowledge_cache.get_knowledge_base_cache().clear)

    def post(self, name, data=None, **kwargs):
        return self.client.post(reverse("tenant:" + name, kwargs=kwargs), data or {})

    def test_create_rename_and_delete_preserve_item_and_variant_identity(self):
        response = self.post("tenant_menu_category_add", {"name": "Breakfast", "sort_order": 1, "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MenuCategory.objects.filter(tenant=self.tenant, name="Breakfast").exists())
        self.post("tenant_menu_category_update", {"name": "Hot drinks", "sort_order": 3, "is_active": "on"}, category_id=self.category.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.category_fk_id, self.category.pk)
        self.assertEqual(self.item.category_fk.name, "Hot drinks")
        self.assertEqual(list(self.item.variants.values_list("pk", flat=True)), [self.small.pk, self.large.pk])
        self.assertEqual(TenantJSONDoc.objects.get(tenant=self.tenant, sub_intent="menu_category").payload["Latte"], "Hot drinks")
        self.post("tenant_menu_category_delete", category_id=self.category.pk)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.category_fk_id)
        self.assertEqual(self.item.variants.count(), 2)
        self.assertEqual(generate_menu_items_json(self.tenant)["menu_items"]["menu_category"]["Latte"], "Uncategorized")

    def test_category_isolation_duplicate_validation_and_clear_assignment(self):
        foreign = MenuCategory.objects.create(tenant=self.other, name="Coffee")
        self.assertEqual(self.post("tenant_menu_category_update", {"name": "Stolen"}, category_id=foreign.pk).status_code, 404)
        self.assertEqual(self.post("tenant_menu_category_delete", category_id=foreign.pk).status_code, 404)
        self.assertEqual(self.post("tenant_menu_item_update", {"category_id": foreign.pk}, item_id=self.item.pk).status_code, 404)
        self.post("tenant_menu_category_add", {"name": " coffee ", "sort_order": 0, "is_active": "on"})
        self.assertEqual(MenuCategory.objects.filter(tenant=self.tenant).count(), 1)
        self.post("tenant_menu_item_update", {"name": "Latte", "category_id": "", "is_available": "on"}, item_id=self.item.pk)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.category_fk_id)
        self.assertEqual(self.client.get(reverse("tenant:tenant_menu_category_delete", args=[self.category.pk])).status_code, 405)

    def test_inactive_category_hides_items_from_both_customer_catalogs(self):
        self.post("tenant_menu_category_update", {"name": "Coffee", "sort_order": 0}, category_id=self.category.pk)
        self.assertNotIn("Latte", generate_menu_items_json(self.tenant)["menu_items"]["menu_category"])
        self.assertNotIn("Latte", knowledge_cache.generate_all_menu_payload().get(self.tenant.api_key, {}))
        self.assertNotIn("Latte", knowledge_cache.get_item_pricing_cache().get(self.tenant.api_key, {}))
        self.post("tenant_menu_category_update", {"name": "Coffee", "sort_order": 0, "is_active": "on"}, category_id=self.category.pk)
        self.assertIn("Latte", knowledge_cache.get_item_pricing_cache()[self.tenant.api_key])

    def test_custom_variants_are_ordered_validated_and_tenant_scoped(self):
        self.post("tenant_menu_variant_add", {"size": "Extra Large", "price": "160.25", "sort_order": 2}, item_id=self.item.pk)
        variant = self.item.variants.get(size="Extra Large")
        self.post("tenant_menu_variant_update", {"size": "Medium", "price": "110", "sort_order": 1}, variant_id=variant.pk)
        variant.refresh_from_db()
        self.assertEqual(variant.size, "Medium")
        self.post("tenant_menu_variant_add", {"size": " small ", "price": "1", "sort_order": 0}, item_id=self.item.pk)
        self.post("tenant_menu_variant_add", {"size": "", "price": "1", "sort_order": 0}, item_id=self.item.pk)
        self.post("tenant_menu_variant_add", {"size": "Bad", "price": "-1", "sort_order": 0}, item_id=self.item.pk)
        self.assertEqual(self.item.variants.count(), 3)
        foreign_item = MenuItem.objects.create(tenant=self.other, name="Foreign")
        foreign_variant = MenuItemVariant.objects.create(menu_item=foreign_item, size="Half", price=50)
        self.assertEqual(self.post("tenant_menu_variant_update", {"size": "Full"}, variant_id=foreign_variant.pk).status_code, 404)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MenuItemVariant.objects.create(menu_item=self.item, size="SMALL", price=1)

    def test_variant_aliases_save_and_publish(self):
        response = self.post("tenant_menu_variant_update", {
            "size": "Large", "price": "140", "sort_order": 1,
            "aliases": "Grande, 12 oz, Grande",
        }, variant_id=self.large.pk)
        self.assertEqual(response.status_code, 302)
        self.large.refresh_from_db()
        self.assertEqual(self.large.aliases, ["Grande", "12 oz"])
        item = knowledge_cache.get_item_pricing_cache()[self.tenant.api_key]["Latte"]
        variant = next(v for v in item["variants"] if v["id"] == str(self.large.pk))
        self.assertEqual(variant["aliases"], ["Grande", "12 oz"])

    def test_display_order_portions_and_allergens_do_not_depend_on_category_name(self):
        category = MenuCategory.objects.create(tenant=self.tenant, name="Sandwiches", sort_order=0)
        sandwich = MenuItem.objects.create(tenant=self.tenant, name="Club Sandwich", category_fk=category)
        MenuItemVariant.objects.create(menu_item=sandwich, size="Half", price=60, weight_grams=100, sort_order=0)
        MenuItemVariant.objects.create(menu_item=sandwich, size="Full", price=100, weight_grams=200, sort_order=1)
        MenuCatalogMeta.objects.create(menu_item=sandwich, allergens={"milk_and_dairy": True})
        knowledge = generate_menu_items_json(self.tenant)["menu_items"]
        self.assertEqual(list(knowledge["menu_category"]), ["Club Sandwich", "Latte"])
        self.assertEqual(list(knowledge["pricing"]["Club Sandwich"]), ["Half", "Full"])
        self.assertEqual(knowledge["portion_and_size"]["Club Sandwich"]["Half"]["weight_grams"], 100)
        self.assertEqual(knowledge["allergens"]["milk_and_dairy"], ["Club Sandwich"])
        self.assertEqual(knowledge["allergens"]["soy"], [])
        knowledge_cache.initialize_caches()
        catalog = load_catalog(self.tenant.api_key)
        self.assertEqual([v['name'] for v in catalog[str(sandwich.pk)]['variants']], ['Half','Full'])
        self.assertEqual([v['name'] for v in catalog[str(self.item.pk)]['variants']], ['Small','Large'])

    def test_variant_words_in_product_names_are_preserved(self):
        item = MenuItem.objects.create(tenant=self.tenant, name="Small Plate")
        MenuItemVariant.objects.create(menu_item=item, size="Large", price=10)
        knowledge_cache.initialize_caches()
        product = load_catalog(self.tenant.api_key)[str(item.pk)]
        selected = validate_selection({str(item.pk):product}, str(item.pk), product['variants'][0]['id'], 1, [])
        self.assertEqual((selected['name'],selected['size']), ('Small Plate','Large'))

    def test_custom_variant_add_and_inactive_category_at_checkout(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Guest", phone="123")
        ChatSession.objects.create(tenant=self.tenant, customer=customer, platform="telegram", session_id="catalog")
        knowledge_cache.initialize_caches()
        basket = Basket()
        intent = PlacingOrderIntent(main_query="2 large latte", sub_intent="add_to_basket", tenant=self.tenant.pk, chat_id="catalog")
        intent.platform = "telegram"
        from tests.support.ordering import seed_evaluation_policy
        seed_evaluation_policy(self.tenant)
        from tests.support.actions import resolved_change
        proposal=OrderProposal(lines=[dict(action='add',item_id=str(self.item.pk),variant_id=str(self.large.pk),
            quantity=2,modifiers=[],target_number=None,unresolved=[])],unresolved=[],catalog_miss=False)
        intent.resolved_action = resolved_change(proposal, basket)
        intent.process_query(basket, {}, {}, [], self.tenant.api_key, customer)
        self.assertEqual(basket.items[0]["size"], "Large")
        self.assertEqual(basket.items[0]["quantity"], 2)
        self.category.is_active = False
        self.category.save()
        # Deliberately retain the old cache and existing basket.
        with self.assertRaisesMessage(ValueError, "Item no longer available"):
            intent._checkout_order(basket, customer, {}, create=True)
        self.assertFalse(Order.objects.exists())

    def test_json_import_supports_arbitrary_categories_variants_and_is_idempotent(self):
        payload = {"menu_items": [{"name": "Club", "menu_category": "Sandwiches", "availability": {"quantity": 5},
                    "pricing": {"Half": "₹60", "Full": "₹100"}, "portion_and_size": {"Half": {"weight_grams": 100}}}]}
        for _ in range(2):
            self.assertEqual(self.post("tenant_menu_ingest_json", {"menu_items_json": json.dumps(payload)}).status_code, 302)
        item = MenuItem.objects.get(tenant=self.tenant, name="Club")
        self.assertEqual(item.category_fk.name, "Sandwiches")
        self.assertEqual(list(item.variants.values_list("size", flat=True)), ["Half", "Full"])
        self.assertEqual(item.variants.get(size="Half").weight_grams, 100)
        self.assertEqual(MenuCategory.objects.filter(tenant=self.tenant, name="Sandwiches").count(), 1)

    def test_invalid_import_is_atomic(self):
        payload = {"menu_items": [{"name": "Bad item", "menu_category": "Invalid category", "pricing": {"X" * 51: "10"}}]}
        self.post("tenant_menu_ingest_json", {"menu_items_json": json.dumps(payload)})
        self.assertFalse(MenuItem.objects.filter(name="Bad item").exists())
        self.assertFalse(MenuCategory.objects.filter(name="Invalid category").exists())

    def test_file_loader_uses_category_fk_and_per_item_variant_metadata(self):
        knowledge = {"menu_items": {"availability": {"all_items": ["Club"]}, "menu_category": {"Club": "Sandwiches"},
                     "pricing": {"Club": {"Half": "60", "Full": "100"}}, "portion_and_size": {"Club": {"Half": {"weight_grams": 100}}}}}
        with self.captureOnCommitCallbacks(execute=True), patch("orders.management.commands.load_menu_items.open", mock_open(read_data=json.dumps(knowledge)), create=True), patch("os.path.exists", return_value=True):
            for _ in range(2):
                call_command("load_menu_items", tenant_id=str(self.tenant.pk), slug="test", stdout=io.StringIO())
        item = MenuItem.objects.get(tenant=self.tenant, name="Club")
        self.assertEqual(item.category_fk.name, "Sandwiches")
        self.assertEqual(list(item.variants.values_list("size", flat=True)), ["Half", "Full"])
        self.assertEqual(item.variants.get(size="Half").weight_grams, 100)

    def test_file_loader_accepts_explicit_file_without_bundled_tenant(self):
        knowledge = {"menu_items": {"availability": {"all_items": ["Espresso"]},
                                   "pricing": {"Espresso": {"Regular": "2.50"}}}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'knowledge.json'
            path.write_text(json.dumps(knowledge), encoding='utf-8')
            with self.captureOnCommitCallbacks(execute=True):
                call_command('load_menu_items', tenant_id=str(self.tenant.pk), file=str(path), stdout=io.StringIO())
        item = MenuItem.objects.get(tenant=self.tenant, name='Espresso')
        self.assertEqual(str(item.variants.get().price), '2.50')
        self.assertFalse(MenuItem.objects.filter(tenant=self.other, name='Espresso').exists())

    def test_file_loader_requires_source_and_rejects_missing_file(self):
        with self.assertRaises(CommandError):
            call_command('load_menu_items', tenant_id=str(self.tenant.pk))
        with TemporaryDirectory() as directory:
            with self.assertRaisesMessage(CommandError, 'File not found'):
                call_command('load_menu_items', tenant_id=str(self.tenant.pk), file=str(Path(directory) / 'missing.json'))


    def test_dashboard_renders_category_management_and_freeform_variants(self):
        self.assertContains(self.client.get(reverse("tenant:tenant_menu")), "Add category")
        response = self.client.get(reverse("tenant:tenant_menu_item_detail", args=[self.item.pk]))
        self.assertContains(response, 'name="category_id"')
        self.assertContains(response, 'value="Small"')
        self.assertContains(response, 'name="sort_order"')
