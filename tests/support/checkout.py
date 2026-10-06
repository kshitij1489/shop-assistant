"""Checkout setup and real graph turns shared by ORM and transaction tests."""
from copy import deepcopy
from unittest.mock import patch

from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.checkout import advance_checkout
from chatbot_core.models import TenantInfo
from orders.checkout_config import default_checkout_config
from orders.models import ChatSession, Customer, MenuItem, MenuItemVariant
from tests.support.runtime import classification_result, enable_legacy_capabilities


class CheckoutFixture:
    def setUp(self):
        super().setUp()
        from tests.support.replies import install_reply_renderer
        self.renderer = install_reply_renderer(self)
        self.tenant = TenantInfo.objects.create(display_name='Checkout Cafe', approval_status='APPROVED')
        enable_legacy_capabilities(self.tenant)
        self.customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='1234567890')
        self.session = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, session_id='chat', platform='website')
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size='Regular', price='100')
        self.basket = Basket(items=[{'item_id': str(self.item.pk), 'item_variant_id': str(self.variant.pk),
            'name': 'Coffee', 'size': 'Regular', 'quantity': 1, 'unit_price': '100', 'item_number': 1}])
        self.config = default_checkout_config()
        self.config['modes']['delivery']['fee'] = '30'
        self.config['modes']['delivery']['required_fields'] = ['address']
        self.config['modes']['pickup'] = {**deepcopy(self.config['modes']['delivery']), 'required_fields': [], 'fee': '5'}
        self.config['modes']['dine_in'] = {**deepcopy(self.config['modes']['pickup']), 'required_fields': ['table_id'], 'fee': '0'}
        self.checklist = {}

    def turn(self, text, **kwargs):
        return advance_checkout(tenant=self.tenant, customer=self.customer, chat_id='chat', platform='website',
            basket=self.basket, checklist=self.checklist, text=text, configuration=kwargs.get('config', self.config),
            original_text=kwargs.get('original_text'), action=kwargs.get('action'))

    def graph_store(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
        self.addCleanup(_session_data.clear)
        store = MemorySessionStore('chat', tenant_id=self.tenant.pk, platform='website')
        store.set_basket(deepcopy(self.basket))
        return store

    def graph_turn(self, store, text, classification=('placing_order', 'order_confirmation'), *, resolved=None, action=None):
        from chatbot_core.logic.cafe.workflow import graph, runner
        pending, _ = store.get_ongoing_queries()
        candidate = next((p for p in reversed(pending) if p.basket_item.get('checkout')), None)
        self.session.refresh_from_db()
        if not (self.session.state or {}).get('checkout'):
            candidate = None
        reply_to = str(candidate.query_id) if candidate and classification[0] in {'placing_order', 'location_based'} and classification[1] != 'check_order_cart' else None
        if classification == ('general', 'cancel_and_abort') or text in {'cancel', 'cancel checkout', 'current request', 'stop this request'}:
            classification = ('general', 'cancel_and_abort')
            reply_to = str(pending[-1].query_id) if pending else None
        sentence = text if resolved is None else resolved
        row = (sentence, *classification, reply_to, None) if action is None else (
            sentence, *classification, reply_to, None, action)
        with patch.object(graph, 'enqueue_string'), patch.object(runner, 'enqueue_string'), \
                patch.object(graph, 'normalize_and_classify', return_value=classification_result([row])):
            return runner.run_conversation(self.tenant, store, text, self.customer)
