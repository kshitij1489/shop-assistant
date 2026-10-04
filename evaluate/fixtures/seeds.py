"""Reviewed state fixtures. No state is inferred from user claims or replies."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from django.db import transaction
from django.contrib.sessions.backends.db import SessionStore
from orders import models as om
from commerce import models as cm
from evaluate.contracts.interfaces import Blocked
from evaluate.fixtures.definitions import address
from evaluate.fixtures.provision import (
    FOREIGN_ADDRESS_ID, OWNER_KEY,
)


def basket(owner, requested):
    items = []
    for index, (name, quantity) in enumerate(requested.items(), 1):
        ids = owner['maps']['catalog'].get(name)
        if not ids or quantity < 1:
            raise Blocked('Fixture item or quantity is invalid')
        variant = om.MenuItemVariant.objects.get(pk=ids['variant'], menu_item_id=ids['item'])
        items.append(dict(name=name, size=variant.size, quantity=quantity, item_number=index,
                          item_id=ids['item'], item_variant_id=ids['variant'], unit_price=str(variant.price),
                          modifiers=[]))
    return {'items': items, 'counter': len(items) + 1}


def cache_state(owner, updates):
    store = SessionStore(session_key=owner['browser_sessions'][0])
    current = store.get(owner['namespace'], {})
    store[owner['namespace']] = {**current, **deepcopy(updates)}
    store.save()


def auxiliary(provisioner, tenant, owner, *, foreign_tenant=False):
    from chatbot_core.models import TenantInfo
    if foreign_tenant:
        tenant = TenantInfo.objects.create(slug='eval-canary-' + uuid4().hex,
            display_name='Evaluation canary', approval_status='APPROVED',
            meta={OWNER_KEY: {'lease': owner['lease'], 'instance_id': owner['instance_id']}})
        owner['tenant_ids'].append(tenant.pk)
        owner['maps']['tenant:canary'] = str(tenant.pk)
    customer = om.Customer.objects.create(tenant=tenant, name='CANARY-' + uuid4().hex, phone='0000000001')
    owner['maps']['customer:canary'] = str(customer.pk)
    return tenant, customer


def _seed_foreign_canary(provisioner, lease, owner):
    """Create a real foreign address in the application's DB, unique to this lease."""
    if owner['maps']['addresses'].get('foreign'):
        return
    other, stranger = auxiliary(provisioner, None, owner, foreign_tenant=True)
    row = om.CustomerAddress.objects.create(
        id=uuid4(), tenant=other, customer=stranger, label='CANARY',
        address_line=stranger.name + ' address',
        components={'house_or_flat': 'CANARY', 'city': 'Test City', 'postal_code': '122002'},
        location_coordinates={'lat': 28.0, 'lng': 77.0})
    owner['maps']['addresses']['foreign'] = str(row.pk)
    owner.setdefault('message_bindings', {})[FOREIGN_ADDRESS_ID] = str(row.pk)


def apply_fixture(provisioner, lease, tenant, owner, fixture):
    customer = om.Customer.objects.get(pk=owner['maps']['customer:active'], tenant=tenant)
    chat = om.ChatSession.objects.get(pk=owner['maps']['session:active'], tenant=tenant, customer=customer)
    if fixture.kind == 'catalog_assertion':
        variants = om.MenuItemVariant.objects.filter(menu_item__tenant=tenant)
        if variants.count() != 26 or variants.exclude(size='QA standard', volume_ml=None, weight_grams=None, aliases=[]).exists():
            raise Blocked('Synthetic catalog contract is not satisfied')
        if cm.StockItem.objects.filter(location__tenant=tenant).exists():
            raise Blocked('Basket-only precondition forbids finite stock')
    elif fixture.kind == 'branch_policy':
        # Check the fresh context and register a runner obligation; never fabricate
        # the reference question or add a basket item to force a continuation.
        store = SessionStore(session_key=owner['browser_sessions'][0])
        state = store.get(owner['namespace'], {})
        if state.get('ongoing_query_queue'):
            raise Blocked('Branch fixture requires a fresh pending-question context')
        owner['branch_policies'].append({'policy': fixture.branch,
            'on_mismatch': 'record_branch_mismatch', 'seed_reference_question': False})
    elif fixture.kind == 'settings_assertion':
        policy = om.CheckoutSettings.objects.get(tenant=tenant).configuration
        modes = policy['modes']
        if fixture.setting == 'scheduling':
            pickup = modes['pickup']
            if not (pickup['scheduling_enabled'] and pickup['max_advance_days'] == 7 and
                    pickup['preparation_minutes'] == 30 and 'scheduled_at' not in pickup['required_fields']):
                raise Blocked('Scheduling override not applied')
        elif fixture.setting == 'dine_in':
            dine = modes.get('dine_in', {})
            if (dine.get('required_fields') != ['name', 'phone', 'table_id'] or
                    dine.get('payment_methods') != ['cash'] or dine.get('scheduling_enabled') or
                    Decimal(dine.get('fee', '-1')) != 0 or Decimal(dine.get('minimum_order', '-1')) != 0):
                raise Blocked('Dine-in override not applied')
        elif 'dine_in' in modes:
            raise Blocked('Dine-in must be disabled')
    elif fixture.kind in ('addresses', 'foreign_address'):
        if customer.addresses.exists():
            raise Blocked('Saved-address fixture requires initially empty addresses')
        for key in fixture.addresses:
            row = om.CustomerAddress.objects.create(tenant=tenant, customer=customer, **address(key))
            owner['maps']['addresses'][key] = str(row.pk)
        if fixture.kind == 'foreign_address':
            _seed_foreign_canary(provisioner, lease, owner)
    elif fixture.kind == 'basket':
        cache_state(owner, {'basket': basket(owner, fixture.items)})
    elif fixture.kind == 'foreign_order':
        if om.Order.objects.filter(tenant=tenant).exists():
            raise Blocked('Active tenant must have no orders')
        other, stranger = auxiliary(provisioner, tenant, owner, foreign_tenant=True)
        row = om.Order.objects.create(tenant=other, customer=stranger, source='website',
            external_order_id='DN-QA-OTHER', total_amount='123.45', meta={'canary': stranger.name})
        owner['maps']['orders']['DN-QA-OTHER'] = str(row.pk)
    elif fixture.kind in ('foreign_draft', 'foreign_pending'):
        other, stranger = auxiliary(provisioner, tenant, owner)
        from chatbot_core.scope import session_identity
        browser = SessionStore()
        browser.create()
        namespace = 'cafe:v2:' + session_identity(str(tenant.pk), 'website', browser.session_key)
        owner['browser_sessions'].append(browser.session_key)
        owner['browser_namespaces'][browser.session_key] = namespace
        state = {'checkout': {'fields': {'name': stranger.name, 'phone': stranger.phone,
                  'address': stranger.name + ' address', 'postal_code': '122102'},
                  'payment_url': 'https://payments.example.test/canary-' + uuid4().hex,
                  'basket': basket(owner, fixture.items), 'mode': 'delivery'}} if fixture.kind == 'foreign_draft' else {}
        other_chat = om.ChatSession.objects.create(tenant=other, customer=stranger, platform='website',
            session_id=browser.session_key, state=state)
        if fixture.kind == 'foreign_pending':
            from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
            pending = PlacingOrderIntent(main_query='Pistachio Ice Cream', sub_intent='add_to_basket',
                tenant=tenant.pk, chat_id=other_chat.session_id,
                basket_item={'name': 'Pistachio Ice Cream'}, follow_up_question=['How many Pistachio Ice Cream?'])
            pending.platform = 'website'
            other_chat.state = {'ongoing_query_queue': [pending.to_dict()], 'awaiting_followup_index': 0}
            other_chat.save(update_fields=['state'])
        browser[namespace] = {'customer_id': str(stranger.pk), **deepcopy(other_chat.state)}
        browser.save()
        owner['maps']['session:canary'] = str(other_chat.pk)
        if chat.state or chat.order_id:
            raise Blocked('Active customer B must have no authorized draft or order')
    elif fixture.kind == 'terminal_order':
        seed_terminal_order(provisioner, lease, fixture)
        _, refreshed = provisioner.owned(lease)
        owner.update(refreshed)
    else:
        raise Blocked('Unsupported fixture kind')


def seed_terminal_order(provisioner, lease, fixture):
    """Commit historical payment creation before the separate adapter claims it."""
    from commerce.pricing import calculate
    from commerce.services import accept_order

    with transaction.atomic():
        tenant, owner = provisioner.owned(lease, lock=True)
        customer = om.Customer.objects.get(pk=owner['maps']['customer:active'], tenant=tenant)
        chat = om.ChatSession.objects.get(pk=owner['maps']['session:active'], tenant=tenant, customer=customer)
        if chat.order_id or om.Order.objects.filter(tenant=tenant).exists():
            raise Blocked('Terminal-history fixture requires an empty order history')
        selected = basket(owner, fixture.items)
        config = cm.Configuration.objects.get(tenant=tenant)
        price = calculate(selected['items'], config.policy, mode='pickup')
        price['location_id'] = str(config.location_id)
        order = om.Order.objects.create(tenant=tenant, customer=customer, source='website',
            payment_mode='online', total_amount=Decimal(price['total_minor']) / 100,
            meta={'checkout': {'mode': 'pickup', 'fields': {'name': 'QA Guest', 'phone': '0000000000'}}})
        for item in selected['items']:
            om.OrderItem.objects.create(order=order, item_id=item['item_id'], variant_id=item['item_variant_id'],
                item_name=item['name'], quantity=item['quantity'], unit_price=item['unit_price'],
                total_price=Decimal(item['unit_price']) * item['quantity'])
        record = accept_order(order, price)
        payment = record.payments.get()
        owner['maps']['orders']['previous_terminal'] = str(order.pk)
        provisioner.save_owner(tenant, owner)

    # The adapter may access both these rows and the owner's provenance. Do not
    # keep either transaction or tenant lock open while it claims/acks commands.
    provisioner.runtime.payment(lease, 'capture', payment)
    # Settle historical POS acceptance before marking fulfillment delivered.
    provisioner.runtime.pump_until_idle(lease)

    with transaction.atomic():
        tenant, owner = provisioner.owned(lease, lock=True)
        order.refresh_from_db()
        if order.payment_status != om.Order.PaymentStatus.PAID:
            raise Blocked('Historical fixture payment lacks authenticated capture evidence')
        # Delivery is explicitly historical setup; payment success was an event.
        order.order_status = om.Order.Status.DELIVERED
        order.save(update_fields=['order_status'])
        record.refresh_from_db()
        record.pos_state = 'delivered'
        record.save(update_fields=['pos_state'])
        chat.order = order
        chat.state = {'checkout': {'basket': selected, 'mode': 'pickup', 'fields': {},
                                  'payment_url': 'https://payments.example.test/stale-evaluation'}}
        chat.save(update_fields=['order', 'state'])
        cache_state(owner, {'basket': selected, 'checklist': {'order': True, 'payment': True,
            'order_id': str(order.pk), 'checkout': chat.state['checkout']}})
        provisioner.save_owner(tenant, owner)
