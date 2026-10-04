"""Run with Django's isolated commerce settings; includes the real coordinator."""
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from commerce.menu_schema import MenuSnapshot
from mock_services.commerce_adapter.storage import Store
from mock_services.commerce_adapter.worker import Worker
from tests.support.cases import MockServicesMixin
from tests.support.commerce import EngineClient, Fixtures
from mock_services.client import HTTPProvider, refresh_payments


@override_settings(ROOT_URLCONF='commerce.urls', COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret'})
class MockEngineTests(MockServicesMixin, Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.pos.capabilities = ['order.submit', 'order.reconcile', 'catalog.write']
        self.pos.save(update_fields=['capabilities'])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        self.mock = self.mock_services.client
        self.workers = {}
        for role, connection in (('payment', self.gateway), ('pos', self.pos)):
            store = Store(path / (role + '.sqlite3'), str(connection.pk))
            self.addCleanup(store.close)
            self.workers[role] = Worker(store, HTTPProvider(self.mock, str(connection.pk), role),
                                       EngineClient(self.client, connection), '')

    def test_checkout_through_http_payment_and_pos(self):
        self.assert_completed(self.accept())

    def assert_completed(self, record):
        payment, pos = self.workers['payment'], self.workers['pos']
        payment.tick()
        refresh_payments(payment, auto_capture=True)
        payment.flush_events()
        pos.tick()
        record.refresh_from_db()
        self.assertEqual(record.state, 'confirmed')
        self.assertEqual(record.order.payment_status, 'paid')
        self.assertEqual(record.pos_state, 'accepted')
        self.assertEqual(record.reservations.get().state, 'consumed')

    def test_generated_catalog_import_retry_and_checkout(self):
        from chatbot_core.logic.cafe.basket import Basket
        from commerce.menu_sync import configure_source
        from commerce.models import StockItem
        from commerce.services import accept_order, basket_quote
        from orders.models import MenuItem, MenuItemVariant, Order
        source = configure_source(self.tenant.pk, mode='external', connection=self.pos)
        snapshot = self.mock.request('POST', '/v1/accounts/' + str(self.pos.pk) + '/menu/snapshots',
                                     {'source_generation': str(source.generation)})
        parsed = MenuSnapshot.model_validate(snapshot)
        self.assertEqual(len(parsed.items), 26)
        client = self.workers['pos'].client
        self.assertEqual(client.request('POST', 'catalog/snapshot/', snapshot)['status'], 'applied')
        self.assertEqual(client.request('POST', 'catalog/snapshot/', snapshot)['status'], 'unchanged')
        self.assertEqual(MenuItem.objects.filter(tenant=self.tenant, is_available=True).count(), 26)
        self.assertEqual(MenuItemVariant.objects.filter(menu_item__tenant=self.tenant, is_available=True,
                                                       menu_item__is_available=True).count(), 26)
        item = MenuItem.objects.get(tenant=self.tenant, name='Old Fashion Vanilla Ice Cream')
        variant = item.variants.get(is_available=True)
        StockItem.objects.create(location=self.location, item=item, on_hand=20)
        basket = Basket(items=[dict(item_id=str(item.pk), item_variant_id=str(variant.pk), name=item.name,
            size=variant.size, quantity=1, unit_price=str(variant.price), modifiers=[])])
        pricing = basket_quote(self.tenant, basket, mode='pickup')
        self.assertEqual(pricing['total_minor'], 38000)
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse',
            total_amount=variant.price, payment_mode='online', meta={'checkout': {'mode': 'pickup', 'fields': {}}})
        self.assert_completed(accept_order(order, pricing))
