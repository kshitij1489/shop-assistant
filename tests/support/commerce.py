"""Commerce scenarios and signed in-process adapter transport shared by tests."""
import json
import time
from urllib.error import HTTPError

from django.utils import timezone

from commerce.adapter_client import AdapterClient
from commerce.api import signature
from commerce.command_schemas import ClaimResponse
from commerce.credentials import adapter_secret
from commerce.models import Location, Configuration, Connection, StockItem
from commerce.policy import evaluation_policy
from commerce.services import accept_order, basket_quote
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.models import TenantInfo
from orders.models import Customer, MenuItem, MenuItemVariant, Order


class Fixtures:
    def seed(self):
        self.tenant = TenantInfo.objects.create(display_name='Commerce cafe', approval_status='APPROVED')
        self.customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='12345678')
        self.location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        self.config = Configuration.objects.create(
            tenant=self.tenant, location=self.location, enabled=True, policy=evaluation_policy())
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size='Regular', price='10.05')
        self.stock = StockItem.objects.create(location=self.location, item=self.item, on_hand=1)
        self.pos = Connection.objects.create(location=self.location, provider='custom', role='pos', active=True, account_id='shop', secret_ref='managed:pos-test', capabilities=['order.submit', 'order.reconcile', 'inventory.update', 'catalog.read'])
        self.gateway = Connection.objects.create(location=self.location, provider='custom', role='payment', active=True, account_id='merchant', secret_ref='test-key', capabilities=['payment.create', 'payment.reconcile'])
        self.basket = Basket(items=[dict(item_id=str(self.item.pk), item_variant_id=str(self.variant.pk), name='Coffee', size='Regular', quantity=1, unit_price='10.05', modifiers=[])])

    def accept(self, mode='online'):
        pricing = basket_quote(self.tenant, self.basket, mode='pickup')
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse', total_amount='10.05', payment_mode=mode, meta={'checkout': {'mode': 'pickup', 'fields': {}}})
        return accept_order(order, pricing)

    def event(self, record, event_id='payment-event', **changes):
        payment = record.payments.get()
        data = dict(type='payment.updated', payment_id=str(payment.pk), external_id='provider-payment', sequence=1, currency='INR', status='captured', captured_minor=1005, refunded_minor=0)
        data.update(changes)
        return {'schema_version': 1, 'event_id': event_id, 'occurred_at': timezone.now().isoformat(), 'data': data}


class EngineClient(AdapterClient):
    """Only replace the network transport; use real routing/auth/claim/ack/events."""
    def __init__(self, test_client, connection):
        super().__init__('https://testserver', str(connection.pk), adapter_secret(connection))
        self.http = test_client

    def request(self, method, endpoint, payload=None):
        path = self.base_url.removeprefix('https://testserver') + endpoint
        body = b'' if payload is None else json.dumps(payload).encode()
        stamp = str(int(time.time()))
        response = self.http.generic(method, path, data=body, content_type='application/json',
            HTTP_X_COMMERCE_TIMESTAMP=stamp,
            HTTP_X_COMMERCE_SIGNATURE=signature(self.secret, stamp, method, path, body))
        if response.status_code >= 400:
            raise HTTPError(self.base_url, response.status_code, 'engine error', {}, None)
        result = response.json()
        if endpoint == 'commands/claim/':
            ClaimResponse.model_validate(result)
        return result

