import time
import io
import json
import tempfile
from pathlib import Path
from django.core.management import call_command, CommandError
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from chatbot_core.models import TenantInfo
from orders.models import MenuItem, MenuItemVariant, AddonGroup, AddonItem, ItemAddonGroup, CheckoutSettings
from users.models import TenantProfile
from commerce.models import Configuration, Location, Connection, StockItem
from commerce.credentials import adapter_secret
from commerce.api import signature


@override_settings(ROOT_URLCONF='tests.support.urls')
class DashboardSetupTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Cafe', approval_status='APPROVED')
        self.other = TenantInfo.objects.create(display_name='Other', approval_status='APPROVED')
        self.user = User.objects.create_user('owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Latte')
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size='Large', price=100)
        self.group = AddonGroup.objects.create(tenant=self.tenant, name='Milk')
        self.option = AddonItem.objects.create(group=self.group, name='Oat', price=20)
        self.location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        from commerce.policy import evaluation_policy
        self.config = Configuration.objects.create(tenant=self.tenant, location=self.location, policy=evaluation_policy())
        self.other_location = Location.objects.create(tenant=self.other, code='main', name='Other')

    def option_data(self, **changes):
        data = {'name': 'Milk', 'addons-TOTAL_FORMS': 1, 'addons-INITIAL_FORMS': 1,
            'addons-0-id': self.option.pk, 'addons-0-name': 'Oat milk', 'addons-0-price': '25',
            'addons-0-aliases': 'oat\noat milk', 'addons-0-min_quantity': 1,
            'addons-0-max_quantity': 3, 'addons-0-is_available': 'on'}
        data.update(changes)
        return data

    def test_group_options_create_edit_and_publish(self):
        url = reverse('tenant:modifier_edit', args=[self.group.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, self.option_data())
        self.assertEqual(response.status_code, 302)
        self.option.refresh_from_db()
        self.assertEqual(self.option.aliases, ['oat', 'oat milk'])
        self.assertEqual(self.option.max_quantity, 3)
        from chatbot_core.logic.cafe.catalog import load_catalog
        ItemAddonGroup.objects.create(tenant=self.tenant, item=self.item, group=self.group)
        catalog = load_catalog(self.tenant.api_key)
        self.assertIn('Oat milk', str(catalog))
        create = self.option_data(**{'name': 'Shots', 'addons-INITIAL_FORMS': 0, 'addons-0-id': ''})
        self.assertEqual(self.client.post(reverse('tenant:modifiers'), create).status_code, 302)
        self.assertTrue(AddonGroup.objects.filter(tenant=self.tenant, name='Shots').exists())

    def test_invalid_options_are_atomic(self):
        response = self.client.post(reverse('tenant:modifier_edit', args=[self.group.pk]),
            self.option_data(**{'name': 'Changed', 'addons-0-price': '-1', 'addons-0-max_quantity': 0}))
        self.assertEqual(response.status_code, 400)
        self.group.refresh_from_db(); self.option.refresh_from_db()
        self.assertEqual(self.group.name, 'Milk')
        self.assertEqual(self.option.name, 'Oat')

    def test_attach_variant_limits_duplicate_and_detach(self):
        url = reverse('tenant:item_modifiers', args=[self.item.pk])
        data = {'group': self.group.pk, 'min_selections': 1, 'max_selections': 1, 'variants': [self.variant.pk]}
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        link = ItemAddonGroup.objects.get(item=self.item)
        self.assertEqual(link.variant_ids, [str(self.variant.pk)])
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data['min_selections'] = 2
        self.assertEqual(self.client.post(reverse('tenant:item_modifier_edit', args=[self.item.pk, link.pk]), data).status_code, 400)
        self.assertEqual(self.client.post(reverse('tenant:item_modifier_edit', args=[self.item.pk, link.pk]), {'action': 'delete'}).status_code, 302)
        self.assertFalse(ItemAddonGroup.objects.filter(pk=link.pk).exists())

    def test_required_options_cannot_be_disabled(self):
        ItemAddonGroup.objects.create(tenant=self.tenant, item=self.item, group=self.group, min_selections=1)
        data = self.option_data(); data.pop('addons-0-is_available')
        self.assertEqual(self.client.post(reverse('tenant:modifier_edit', args=[self.group.pk]), data).status_code, 400)
        self.option.refresh_from_db(); self.assertTrue(self.option.is_available)

    def test_modifier_tenant_boundaries(self):
        group = AddonGroup.objects.create(tenant=self.other, name='Foreign')
        item = MenuItem.objects.create(tenant=self.other, name='Foreign')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Small', price=1)
        self.assertEqual(self.client.post(reverse('tenant:modifier_edit', args=[group.pk]), {'action': 'delete'}).status_code, 404)
        self.assertEqual(self.client.get(reverse('tenant:item_modifiers', args=[item.pk])).status_code, 404)
        for group_id, variants in [(group.pk, []), (self.group.pk, [variant.pk])]:
            response = self.client.post(reverse('tenant:item_modifiers', args=[self.item.pk]),
                {'group': group_id, 'variants': variants, 'min_selections': 0, 'max_selections': 1})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(ItemAddonGroup.objects.exists())
        foreign_option = AddonItem.objects.create(group=group, name='Foreign option', price=1)
        self.assertEqual(self.client.post(reverse('tenant:modifier_edit', args=[self.group.pk]),
            self.option_data(**{'addons-0-id': foreign_option.pk})).status_code, 400)
        foreign_option.refresh_from_db()
        self.assertEqual(foreign_option.name, 'Foreign option')

    def test_option_delete_preserves_order_history_and_protects_stock(self):
        stock = StockItem.objects.create(location=self.location, addon=self.option, on_hand=1)
        url = reverse('tenant:modifier_edit', args=[self.group.pk])
        self.assertEqual(self.client.post(url, self.option_data(**{'addons-0-DELETE': 'on'})).status_code, 400)
        self.assertTrue(AddonItem.objects.filter(pk=self.option.pk).exists())
        self.assertEqual(self.client.post(url, {'action': 'delete'}).status_code, 400)
        stock.delete()
        self.assertEqual(self.client.post(url, {'action': 'delete'}).status_code, 302)

    def connection_data(self, **changes):
        data = {'provider': 'custom', 'role': 'pos', 'account_id': 'cafe', 'environment': 'test',
            'capabilities': ['order.submit', 'order.reconcile'], 'active': 'on'}
        data.update(changes)
        return data

    def test_connection_creation_rotation_and_signed_authentication(self):
        response = self.client.post(reverse('commerce:connections'), self.connection_data())
        self.assertEqual(response.status_code, 200)
        connection = Connection.objects.get(location=self.location)
        secret = adapter_secret(connection)
        self.assertContains(response, secret)
        self.assertContains(response, f'action="{reverse("commerce:connection_edit", args=[connection.pk])}"')
        self.assertIn('no-store', response['Cache-Control'])
        url = reverse('commerce:connection_edit', args=[connection.pk])
        self.assertNotContains(self.client.get(url), secret)
        path = f'/commerce/v1/connections/{connection.pk}/schema/'
        stamp = str(int(time.time()))
        headers = {'HTTP_X_COMMERCE_TIMESTAMP': stamp, 'HTTP_X_COMMERCE_SIGNATURE': signature(secret, stamp, 'GET', path, b'')}
        self.assertEqual(self.client.get(path, **headers).status_code, 200)
        rotated = self.client.post(url, {'action': 'rotate'})
        connection.refresh_from_db()
        self.assertNotEqual(secret, adapter_secret(connection))
        self.assertContains(rotated, adapter_secret(connection))
        self.assertEqual(self.client.get(path, **headers).status_code, 401)

    def test_connection_role_validation_and_identity_preservation(self):
        url = reverse('commerce:connections')
        self.assertEqual(self.client.post(url, self.connection_data(capabilities=['payment.create'])).status_code, 400)
        self.client.post(url, self.connection_data())
        self.assertEqual(self.client.post(url, self.connection_data()).status_code, 400)
        connection = Connection.objects.get(location=self.location)
        self.assertEqual(self.client.post(reverse('commerce:connection_edit', args=[connection.pk]), self.connection_data(account_id='replacement')).status_code, 400)
        connection.refresh_from_db(); self.assertEqual(connection.account_id, 'cafe')

    def test_foreign_connections_and_stock_cannot_be_accessed(self):
        foreign = Connection.objects.create(location=self.other_location, role='pos', provider='custom', account_id='other')
        foreign_item = MenuItem.objects.create(tenant=self.other, name='Foreign')
        stock = StockItem.objects.create(location=self.other_location, item=foreign_item)
        for url in (reverse('commerce:connection_edit', args=[foreign.pk]), reverse('commerce:stock_edit', args=[stock.pk])):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {'action': 'rotate'}).status_code, 404)
        self.assertEqual(self.client.post(reverse('commerce:stock'), {'item': foreign_item.pk, 'mode': 'quantity', 'on_hand': 10}).status_code, 400)

    def test_stock_create_edit_cannot_reduce_reservations_or_change_subject(self):
        data = {'item': self.item.pk, 'mode': 'quantity', 'on_hand': 5, 'available': 'on'}
        self.assertEqual(self.client.post(reverse('commerce:stock'), data).status_code, 302)
        stock = StockItem.objects.get(location=self.location)
        StockItem.objects.filter(pk=stock.pk).update(reserved=3)
        url = reverse('commerce:stock_edit', args=[stock.pk])
        self.assertEqual(self.client.post(url, {**data, 'on_hand': 2}).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, 'on_hand': 8, 'item': '', 'variant': self.variant.pk}).status_code, 302)
        stock.refresh_from_db()
        self.assertEqual((stock.on_hand, stock.reserved, stock.item_id, stock.variant_id), (8, 3, self.item.pk, None))
        self.assertEqual(self.client.post(reverse('commerce:stock'), data).status_code, 400)

    def test_external_stock_cannot_be_overwritten(self):
        connection = Connection.objects.create(location=self.location, role='pos', provider='custom', account_id='shop')
        stock = StockItem.objects.create(location=self.location, item=self.item, authority=connection, on_hand=3)
        self.assertEqual(self.client.post(reverse('commerce:stock_edit', args=[stock.pk]), {'on_hand': 500, 'available': 'on'}).status_code, 400)
        stock.refresh_from_db(); self.assertEqual(stock.on_hand, 3)

    def test_provider_stock_setup_requires_matching_tenant_and_inventory_capability(self):
        connection = Connection.objects.create(location=self.location, role='pos', provider='custom', account_id='shop', capabilities=['inventory.update'])
        data = {'item': self.item.pk, 'authority': connection.pk, 'mode': 'quantity', 'on_hand': 0, 'available': 'on'}
        self.assertEqual(self.client.post(reverse('commerce:stock'), {**data, 'on_hand': 5}).status_code, 400)
        self.assertEqual(self.client.post(reverse('commerce:stock'), data).status_code, 302)
        stock = StockItem.objects.get(location=self.location)
        self.assertEqual(stock.authority_id, connection.pk)
        self.assertIsNone(stock.observed_at)

    def test_rollout_preview_apply_and_idempotency(self):
        from orders.checkout_config import default_checkout_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkout.json'
            path.write_text(json.dumps(default_checkout_config()))
            for apply in (False, True, True):
                output = io.StringIO()
                call_command('configure_commerce', tenant=[self.tenant.pk], checkout_policy=str(path), apply=apply, stdout=output)
                self.assertEqual(json.loads(output.getvalue())['applied'], apply)
                self.assertEqual(CheckoutSettings.objects.filter(tenant=self.tenant).exists(), apply)
            self.assertEqual(CheckoutSettings.objects.filter(tenant=self.tenant).count(), 1)
            self.assertFalse(CheckoutSettings.objects.filter(tenant=self.other).exists())

    def test_rollout_requires_explicit_checkout_policy_and_rolls_back_failed_enable(self):
        with self.assertRaisesMessage(CommandError, 'legacy checkout'):
            call_command('configure_commerce', tenant=[self.tenant.pk], stdout=io.StringIO())
        from orders.checkout_config import default_checkout_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkout.json'
            path.write_text(json.dumps(default_checkout_config()))
            with self.assertRaisesMessage(CommandError, 'not ready'):
                call_command('configure_commerce', tenant=[self.tenant.pk], checkout_policy=str(path), enable=True, apply=True, stdout=io.StringIO())
        self.assertFalse(CheckoutSettings.objects.filter(tenant=self.tenant).exists())
        self.config.refresh_from_db(); self.assertFalse(self.config.enabled)

    def test_readiness_blocks_activation_and_identifies_legacy_checkout(self):
        url = reverse('commerce:settings')
        self.assertContains(self.client.get(url), 'legacy')
        data = dict(enabled='on', currency='INR', packaging_minor=0, minimum_minor=0, stock_policy='strict',
            reservation_seconds=900, stock_max_age_seconds=300, taxes='[]', discounts='[]')
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.config.refresh_from_db(); self.assertFalse(self.config.enabled)
        CheckoutSettings.objects.create(tenant=self.tenant)
        self.client.post(reverse('commerce:connections'), self.connection_data())
        self.assertEqual(self.client.post(url, data).status_code, 400)
        StockItem.objects.create(location=self.location, item=self.item, on_hand=1)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.config.refresh_from_db(); self.assertTrue(self.config.enabled)
