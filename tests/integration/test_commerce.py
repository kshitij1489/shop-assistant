import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from django.core.exceptions import ValidationError
from chatbot_core.models import TenantInfo
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.checkout import advance_checkout
from orders.models import MenuItem, Order, ChatSession
from orders.checkout_config import default_checkout_config
from commerce.models import Location, Configuration, Connection, StockItem, AcceptedOrder, Payment, Command, Inbox
from commerce.pricing import calculate, allocate, digest
from commerce.policy import default_policy
from commerce.services import basket_quote, accept_order, expire_reservations, cancel_order
from commerce.events import receive
from commerce.queue import claim, acknowledge, reconcile
from commerce.schemas import Acknowledgement
from commerce.api import signature
from commerce.credentials import adapter_secret


from tests.support.commerce import Fixtures


class PricingTests(TestCase):
    def test_modifier_prices_are_added_once_before_discount_and_tax(self):
        selections = [dict(item_id='latte', quantity=2, unit_price='150', modifiers=[
            dict(option_id='oat', quantity=1, unit_price='30'),
            dict(option_id='shot', quantity=2, unit_price='15'),
            dict(option_id='no-sugar', quantity=1, unit_price='0'),
        ])]
        original = deepcopy(selections)
        policy = default_policy()
        policy.update(taxes=[{'code': 'VAT', 'name': 'VAT', 'rate': '10'}],
                      discounts=[{'code': 'SAVE', 'percent': '10'}])
        result = calculate(selections, policy, mode='pickup', discount_code='SAVE')
        line = result['lines'][0]
        self.assertEqual(line['unit_price'], '150')
        self.assertEqual(line['unit_minor'], 15000)
        self.assertEqual([m['subtotal_minor'] for m in line['modifiers']], [6000, 6000, 0])
        self.assertEqual(result['subtotal_minor'], 42000)
        self.assertEqual(result['discount_minor'], 4200)
        self.assertEqual(result['tax_minor'], 3780)
        self.assertEqual(result['total_minor'], 41580)
        self.assertEqual(selections, original)

    def test_modifier_rounding_and_quantity_validation(self):
        selection = dict(item_id='a', quantity=2, unit_price='0.035',
                         modifiers=[dict(quantity=3, unit_price='0.015')])
        result = calculate([selection], default_policy(), mode='pickup')
        self.assertEqual(result['subtotal_minor'], 20)
        for quantity in (True, 0, -1, '2', 1.5, 10001):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                selection['modifiers'][0]['quantity'] = quantity
                calculate([selection], default_policy(), mode='pickup')

    def test_rounding_and_allocation_conserve_money(self):
        policy = default_policy()
        policy.update(taxes=[{'code': 'VAT', 'name': 'VAT', 'rate': '20', 'inclusive': True}], discounts=[{'code': 'SAVE', 'fixed_minor': 1}])
        lines = [dict(item_id=str(i), quantity=1, unit_price='0.035', modifiers=[]) for i in range(3)]
        result = calculate(lines, policy, mode='pickup', discount_code='SAVE')
        self.assertEqual(result['subtotal_minor'], 12)
        self.assertEqual(result['discount_minor'], 1)
        self.assertEqual(result['total_minor'], 11)
        self.assertEqual(sum(x['net_minor'] + x['tax_minor'] for x in result['lines']), 11)
        self.assertEqual(allocate(2, [1, 1, 1]), [1, 1, 0])

    def test_tax_fees_exclusive_discount_eligibility(self):
        policy = default_policy()
        policy.update(packaging_minor=100, taxes=[{'code': 'VAT', 'name': 'VAT', 'rate': '10', 'tax_fees': True}], discounts=[{'code': 'SAVE', 'percent': '10', 'item_ids': ['a'], 'modes': ['pickup']}])
        result = calculate([dict(item_id='a', quantity=2, unit_price='10')], policy, mode='pickup', fee='2', discount_code='SAVE')
        self.assertEqual(result['total_minor'], 2310)
        self.assertEqual(result['tax_minor'], 210)
        with self.assertRaises(ValueError):
            calculate([dict(item_id='b', quantity=1, unit_price='10')], policy, mode='pickup', discount_code='SAVE')

    def test_ordering_limits_are_explicit_and_cannot_exceed_ceilings(self):
        from pydantic import ValidationError as SchemaError
        from commerce.forms import CommerceSettingsForm
        from commerce.policy import MAX_ITEM_QUANTITY, OrderingLimits, Policy, evaluation_policy
        stored = Policy.model_validate(default_policy())
        self.assertEqual(stored.schema_version, 2)
        self.assertIsNone(stored.ordering_limits)
        seeded = Policy.model_validate(evaluation_policy())
        self.assertEqual(seeded.currency, 'INR')
        self.assertEqual(seeded.exponent, 2)
        self.assertEqual(seeded.ordering_limits.max_line_quantity, 20)
        self.assertEqual(seeded.ordering_limits.max_item_quantity, 30)
        self.assertEqual(seeded.ordering_limits.max_basket_units, 60)
        self.assertEqual(seeded.ordering_limits.max_basket_lines, 20)
        self.assertEqual(seeded.ordering_limits.max_subtotal_minor, 500000)
        self.assertEqual(seeded.ordering_limits.max_payable_minor, 600000)
        limits = dict(max_line_quantity=20, max_item_quantity=30, max_basket_units=60,
                      max_basket_lines=20, max_subtotal_minor=500000, max_payable_minor=600000)
        with self.assertRaises(SchemaError):
            OrderingLimits(**{**limits, 'max_line_quantity': True})
        with self.assertRaises(SchemaError):
            OrderingLimits(**{**limits, 'max_line_quantity': MAX_ITEM_QUANTITY + 1})
        with self.assertRaises(SchemaError):
            OrderingLimits(**{**limits, 'max_subtotal_minor': 600001, 'max_payable_minor': 600000})
        base = dict(currency='INR', packaging_minor=0, minimum_minor=0, stock_policy='strict',
                    reservation_seconds=900, stock_max_age_seconds=300, taxes='[]', discounts='[]')
        empty = CommerceSettingsForm(base)
        self.assertTrue(empty.is_valid(), empty.errors)
        self.assertIsNone(empty.policy['ordering_limits'])
        partial = CommerceSettingsForm({**base, 'max_line_quantity': 20})
        self.assertFalse(partial.is_valid())
        complete = CommerceSettingsForm({**base, **limits})
        self.assertTrue(complete.is_valid(), complete.errors)
        self.assertEqual(complete.policy['ordering_limits']['max_payable_minor'], 600000)

    def test_bad_currency_tax_and_nonfinite_rejected(self):
        for config in ({'currency': 'JPY', 'exponent': 2}, {'currency': 'KWD', 'exponent': 2}, {'taxes': [{'code': 'x', 'name': 'x', 'rate': 'NaN'}]}, {'taxes': [{'code': 'x', 'name': 'x', 'rate': '5', 'inclusive': True}, {'code': 'y', 'name': 'y', 'rate': '5'}]}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                calculate([dict(item_id='a', quantity=1, unit_price='1')], {**default_policy(), **config}, mode='pickup')


@override_settings(ROOT_URLCONF='commerce.urls', COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret'})
class CommerceTests(Fixtures, TestCase):
    def setUp(self):
        self.seed()

    def test_variant_stock_takes_precedence_without_reserving_item_stock(self):
        variant_stock = StockItem.objects.create(location=self.location, variant=self.variant, on_hand=1)
        self.stock.on_hand = 0
        self.stock.save()
        record = self.accept()
        self.assertEqual(list(record.reservations.values_list('stock_id', 'quantity')), [(variant_stock.pk, 1)])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.reserved, 0)
        variant_stock.refresh_from_db()
        self.assertEqual(variant_stock.reserved, 1)
        cancel_order(record.pk)
        variant_stock.on_hand = 0
        variant_stock.reserved = 0
        variant_stock.save()
        self.stock.on_hand = 10
        self.stock.save()
        with self.assertRaisesMessage(ValueError, 'sold out'):
            self.accept()  # An exhausted variant must not fall back to item stock.

    def test_stock_observation_cannot_erase_holds_or_unacknowledged_consumption(self):
        self.stock.authority = self.pos
        self.stock.observed_at = timezone.now()
        self.stock.save()
        record = self.accept()
        def event(key, sequence, acknowledged=()):
            return {'schema_version': 1, 'event_id': key, 'occurred_at': timezone.now().isoformat(),
                    'data': {'type': 'inventory.updated', 'stock_id': str(self.stock.pk),
                             'sequence': sequence, 'on_hand': 0, 'available': True,
                             'observed_at': timezone.now().isoformat(),
                             'acknowledged_reservation_ids': list(acknowledged)}}
        self.assertEqual(receive(self.pos, event('held-shortfall', 1)).status, 'failed')
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.reserved, self.stock.sequence), (1, 1, 0))
        self.assertEqual(receive(self.gateway, self.event(record)).status, 'processed')
        self.assertEqual(receive(self.pos, event('consumed-shortfall', 2)).status, 'failed')
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.pending_consumed), (1, 1))
        hold = record.reservations.get()
        self.assertEqual(receive(self.pos, event('acknowledged', 3, [str(hold.pk)])).status, 'processed')
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.pending_consumed), (0, 0))

    def test_consume_shortfall_records_capture_and_refund_without_negative_stock(self):
        self.gateway.capabilities.append('payment.refund')
        self.gateway.save()
        record = self.accept()
        StockItem.objects.filter(pk=self.stock.pk).update(on_hand=0)
        self.assertEqual(receive(self.gateway, self.event(record)).status, 'processed')
        record.refresh_from_db()
        record.order.refresh_from_db()
        self.stock.refresh_from_db()
        self.assertEqual(record.state, 'review')
        self.assertEqual(record.order.payment_status, 'unpaid')
        self.assertEqual(record.payments.get().captured_minor, 1005)
        self.assertEqual((self.stock.on_hand, self.stock.reserved), (0, 0))
        self.assertTrue(record.issues.filter(code='stock_commitment_shortfall').exists())
        self.assertTrue(record.commands.filter(kind='payment.refund').exists())
        self.assertFalse(record.commands.filter(kind='order.submit').exists())

    def test_accepted_snapshot_keeps_base_and_modifier_prices_separate(self):
        from orders.models import AddonGroup, AddonItem, ItemAddonGroup
        from chatbot_core.logic.cafe.catalog import load_catalog, validate_selection
        from chatbot_core.logic.cafe.db_utils import create_order
        group = AddonGroup.objects.create(tenant=self.tenant, name='Extras')
        addon = AddonItem.objects.create(group=group, name='Shot', price='1.25', max_quantity=2)
        ItemAddonGroup.objects.create(tenant=self.tenant, item=self.item, group=group)
        self.stock.on_hand = 2
        self.stock.save()
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.item.pk), str(self.variant.pk), 2,
            [dict(group_id=str(group.pk), option_id=str(addon.pk), quantity=2)])
        basket = Basket(items=[selection])
        pricing = basket_quote(self.tenant, basket, mode='pickup')
        order = create_order(self.tenant, self.customer, basket, 'user', payment_mode='cash')
        record = accept_order(order, pricing)
        line = record.snapshot['pricing']['lines'][0]
        self.assertEqual(Decimal(line['unit_price']), Decimal('10.05'))
        self.assertEqual(line['unit_minor'], 1005)
        self.assertEqual(line['modifiers'][0]['unit_minor'], 125)
        self.assertEqual(line['modifiers'][0]['subtotal_minor'], 500)
        self.assertEqual(record.total_minor, 2510)
        self.assertEqual(order.total_amount, Decimal('25.10'))
        self.assertEqual(record.commands.get(kind='order.submit').payload['snapshot'], record.snapshot)

    def test_reserve_last_item_and_atomic_failure(self):
        record = self.accept()
        with self.assertRaisesMessage(ValueError, 'sold out'):
            self.accept()
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.reserved), (1, 1))
        self.assertEqual(AcceptedOrder.objects.count(), 1)
        self.assertEqual(Command.objects.count(), 1)
        self.assertEqual(accept_order(record.order, record.snapshot['pricing']).pk, record.pk)

    def test_expiry_releases_and_late_payment_is_recorded_not_submitted(self):
        record = self.accept()
        AcceptedOrder.objects.filter(pk=record.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_reservations(), 1)
        self.assertEqual(expire_reservations(), 0)
        receive(self.gateway, self.event(record))
        record.refresh_from_db(); self.stock.refresh_from_db()
        self.assertEqual(record.state, 'review')
        self.assertEqual((self.stock.on_hand, self.stock.reserved), (1, 0))
        self.assertEqual(record.payments.get().captured_minor, 1005)
        self.assertFalse(record.commands.filter(kind='order.submit').exists())
        self.assertTrue(record.issues.filter(code='late_payment').exists())
        self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'unpaid')
        self.assertFalse(record.commands.filter(kind='payment.refund').exists())

    def test_unfulfillable_captures_are_unpaid_and_refunded_when_supported(self):
        for supports_refund in (False, True):
            self.gateway.capabilities = ['payment.create', 'payment.reconcile'] + (['payment.refund'] if supports_refund else [])
            self.gateway.save()
            for scenario in ('expired', 'unswept_expiry', 'cancelled', 'review', 'underpaid', 'overpaid'):
                with self.subTest(scenario=scenario, supports_refund=supports_refund):
                    record = self.accept()
                    amount = {'underpaid': 100, 'overpaid': 1100}.get(scenario, 1005)
                    if scenario in ('expired', 'unswept_expiry'):
                        AcceptedOrder.objects.filter(pk=record.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
                        if scenario == 'expired':
                            expire_reservations()
                    elif scenario == 'cancelled':
                        cancel_order(record.pk)
                    elif scenario == 'review':
                        AcceptedOrder.objects.filter(pk=record.pk).update(state='review')
                    event_id = str(record.pk)
                    event = self.event(record, event_id, captured_minor=amount, external_id=event_id)
                    self.assertEqual(receive(self.gateway, event).status, 'processed')
                    receive(self.gateway, event)
                    receive(self.gateway, self.event(record, event_id + '-new', sequence=2, captured_minor=amount, external_id=event_id))
                    record.refresh_from_db(); self.stock.refresh_from_db()
                    self.assertEqual(record.state, 'review')
                    self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'unpaid')
                    self.assertEqual(record.payments.get().captured_minor, amount)
                    self.assertEqual(record.reservations.get().state, 'released')
                    self.assertEqual((self.stock.on_hand, self.stock.reserved), (1, 0))
                    self.assertFalse(record.commands.filter(kind='order.submit').exists())
                    self.assertEqual(record.commands.filter(kind='payment.refund').count(), int(supports_refund))
                    if supports_refund:
                        refund = record.commands.get(kind='payment.refund')
                        self.assertEqual(refund.connection_id, self.gateway.pk)
                        self.assertEqual(refund.payload, {
                            'payment_id': str(record.payments.get().pk), 'external_id': event_id,
                            'currency': 'INR', 'exponent': 2, 'target_refunded_minor': amount,
                        })

    def test_refund_progress_does_not_duplicate_commands_or_fulfill_order(self):
        self.gateway.capabilities.append('payment.refund'); self.gateway.save()
        record = self.accept()
        cancel_order(record.pk)
        receive(self.gateway, self.event(record, captured_minor=500, refunded_minor=100))
        receive(self.gateway, self.event(record, 'refund-progress', sequence=2, captured_minor=500, refunded_minor=200))
        self.assertEqual(record.commands.filter(kind='payment.refund').count(), 1)
        receive(self.gateway, self.event(record, 'more-capture', sequence=3, refunded_minor=200))
        self.assertCountEqual(record.commands.filter(kind='payment.refund').values_list('payload', flat=True), [
            {'payment_id': str(record.payments.get().pk), 'external_id': 'provider-payment',
             'currency': 'INR', 'exponent': 2, 'target_refunded_minor': amount} for amount in (500, 1005)
        ])
        receive(self.gateway, self.event(record, 'fully-refunded', sequence=4, status='refunded', refunded_minor=1005))
        self.assertEqual(record.commands.filter(kind='payment.refund').count(), 2)
        self.assertFalse(record.commands.filter(kind='order.submit').exists())
        self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'unpaid')

    def test_already_refunded_capture_needs_no_refund_command(self):
        self.gateway.capabilities.append('payment.refund'); self.gateway.save()
        record = self.accept()
        receive(self.gateway, self.event(record, status='refunded', refunded_minor=1005))
        record.refresh_from_db()
        self.assertEqual(record.state, 'review')
        self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'unpaid')
        self.assertFalse(record.commands.filter(kind__in=['payment.refund', 'order.submit']).exists())

    def test_refund_enqueue_failure_rolls_back_capture_and_retries(self):
        self.gateway.capabilities.append('payment.refund'); self.gateway.save()
        record = self.accept()
        event = self.event(record, captured_minor=1100)
        with patch('commerce.events.refund_capture', side_effect=ValueError('temporary')):
            self.assertEqual(receive(self.gateway, event).status, 'failed')
        record.refresh_from_db()
        self.assertEqual(record.state, 'awaiting_payment')
        self.assertEqual(record.payments.get().captured_minor, 0)
        self.assertEqual(record.reservations.get().state, 'held')
        self.assertFalse(record.issues.exists())
        self.assertEqual(receive(self.gateway, event).status, 'processed')
        self.assertEqual(record.commands.filter(kind='payment.refund').count(), 1)

    def test_duplicate_and_out_of_order_payments_do_not_double_consume(self):
        record = self.accept()
        event = self.event(record, sequence=2)
        receive(self.gateway, event); receive(self.gateway, event)
        receive(self.gateway, self.event(record, 'old', sequence=1, status='failed', captured_minor=0))
        self.stock.refresh_from_db(); record.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.reserved), (0, 0))
        self.assertEqual(record.state, 'confirmed')
        self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'paid')
        self.assertEqual(record.payments.get().captured_minor, 1005)
        self.assertEqual(record.commands.filter(kind='order.submit').count(), 1)
        self.assertEqual(Inbox.objects.count(), 2)
        with self.assertRaisesMessage(ValueError, 'reused'):
            changed = deepcopy(event); changed['data']['captured_minor'] = 999
            receive(self.gateway, changed)

    def test_snapshot_immutable_and_independent_of_catalog(self):
        record = self.accept()
        self.item.name = 'Changed'; self.item.save()
        self.variant.price = '999'; self.variant.save()
        record.refresh_from_db()
        self.assertEqual(record.snapshot['pricing']['lines'][0]['name'], 'Coffee')
        self.assertEqual(record.snapshot_hash, digest(record.snapshot))
        record.snapshot['pricing']['total_minor'] = 1
        with self.assertRaises(ValidationError):
            record.save()

    def test_provider_amount_or_currency_cannot_change_accepted_total(self):
        record = self.accept()
        receive(self.gateway, self.event(record, currency='EUR'))
        self.assertEqual(record.payments.get().captured_minor, 0)
        self.assertTrue(record.issues.filter(code='payment_identity_mismatch').exists())
        receive(self.gateway, self.event(record, 'wrong-amount', captured_minor=100))
        record.refresh_from_db()
        self.assertEqual(record.state, 'review')
        self.assertFalse(record.commands.filter(kind='order.submit').exists())

    def test_failed_event_persisted_and_retry_is_atomic(self):
        record = self.accept()
        event = self.event(record)
        with patch('commerce.events.pos_submit', side_effect=ValueError('temporary')):
            row = receive(self.gateway, event)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(record.payments.get().captured_minor, 0)
        row = receive(self.gateway, event)
        self.assertEqual(row.status, 'processed')
        self.assertEqual(record.payments.get().captured_minor, 1005)

    def test_missing_pos_does_not_lose_successful_payment(self):
        record = self.accept()
        self.pos.active = False; self.pos.save()
        receive(self.gateway, self.event(record))
        record.refresh_from_db()
        self.assertEqual(record.state, 'confirmed')
        self.assertEqual(record.pos_state, 'unconfigured')
        self.assertEqual(record.payments.get().captured_minor, 1005)

    def test_external_stock_does_not_resurrect_unacknowledged_sales(self):
        self.stock.authority = self.pos; self.stock.observed_at = timezone.now(); self.stock.save()
        record = self.accept('cash')
        hold = record.reservations.get()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.pending_consumed, 1)
        def stock_event(key, seq, count, ack=None):
            return {'schema_version': 1, 'event_id': key, 'occurred_at': timezone.now().isoformat(), 'data': {'type': 'inventory.updated', 'stock_id': str(self.stock.pk), 'sequence': seq, 'on_hand': count, 'available': True, 'observed_at': timezone.now().isoformat(), 'acknowledged_reservation_ids': ack or []}}
        receive(self.pos, stock_event('stale-count', 1, 1))
        with self.assertRaises(ValueError):
            self.accept('cash')
        receive(self.pos, stock_event('applied-sale', 2, 0, [str(hold.pk)]))
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.on_hand, self.stock.pending_consumed), (0, 0))

    def test_stale_and_availability_stock_policy(self):
        self.stock.authority = self.pos; self.stock.observed_at = timezone.now() - timedelta(hours=1); self.stock.save()
        with self.assertRaisesMessage(ValueError, 'stale'):
            self.accept()
        self.stock.authority = None; self.stock.mode = 'availability'; self.stock.save()
        with self.assertRaisesMessage(ValueError, 'Exact stock'):
            self.accept()
        self.config.policy['stock_policy'] = 'availability'; self.config.save()
        self.accept()

    def test_cancel_and_untracked_expiry(self):
        record = self.accept(); cancel_order(record.pk); cancel_order(record.pk)
        self.stock.refresh_from_db(); self.assertEqual(self.stock.reserved, 0)
        self.config.policy['stock_policy'] = 'untracked'; self.config.save()
        record = self.accept()
        AcceptedOrder.objects.filter(pk=record.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_reservations(), 1)

    def test_command_lease_recovery_retry_and_unknown(self):
        record = self.accept()
        first = claim(self.gateway)[0]
        self.assertEqual(claim(self.gateway), [])
        Command.objects.filter(pk=first['command_id']).update(lease_until=timezone.now() - timedelta(seconds=1))
        second = claim(self.gateway)[0]
        self.assertEqual(first['command_id'], second['command_id'])
        with self.assertRaises(ValueError):
            acknowledge(self.gateway, first['command_id'], Acknowledgement(lease_token=first['lease_token'], outcome='succeeded'))
        acknowledge(self.gateway, second['command_id'], Acknowledgement(lease_token=second['lease_token'], outcome='unknown'))
        self.assertEqual(claim(self.gateway), [])
        self.assertTrue(record.issues.filter(code='adapter_unknown').exists())

    @override_settings(COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret', 'other-key': 'other-secret'})
    def test_authenticated_api_replay_tenant_isolation_and_schema(self):
        record = self.accept()
        def signed(path, body, stamp=None, secret='test-adapter-secret'):
            stamp = str(stamp or int(time.time()))
            return self.client.post(path, data=body, content_type='application/json', HTTP_X_COMMERCE_TIMESTAMP=stamp,
                HTTP_X_COMMERCE_SIGNATURE=signature(secret, stamp, 'POST', path, body))
        path = f'/v1/connections/{self.gateway.pk}/events/'
        body = json.dumps(self.event(record)).encode()
        self.assertEqual(self.client.post(path, data=body, content_type='application/json').status_code, 401)
        self.assertEqual(signed(path, body, int(time.time()) - 1000).status_code, 401)
        self.assertEqual(signed(path, body).status_code, 200)
        foreign = Connection.objects.create(location=Location.objects.create(tenant=TenantInfo.objects.create(display_name='Other'), code='other', name='Other'), provider='custom', role='payment', active=True, account_id='other', secret_ref='other-key', capabilities=['payment.create'])
        self.assertEqual(signed(f'/v1/connections/{foreign.pk}/events/', body).status_code, 401)
        response = signed(f'/v1/connections/{foreign.pk}/events/', body, secret='other-secret')
        self.assertEqual(response.json()['status'], 'failed')
        self.assertEqual(record.payments.get().captured_minor, 1005)
        schema_path = f'/v1/connections/{self.pos.pk}/schema/'
        stamp = str(int(time.time()))
        response = self.client.get(schema_path, HTTP_X_COMMERCE_TIMESTAMP=stamp, HTTP_X_COMMERCE_SIGNATURE=signature(adapter_secret(self.pos), stamp, 'GET', schema_path, b''))
        self.assertEqual(response.status_code, 200)
        self.assertIn('event', response.json())

    def test_pos_rejection_preserves_payment_and_marks_review(self):
        self.gateway.capabilities.append('payment.refund'); self.gateway.save()
        record = self.accept()
        receive(self.gateway, self.event(record))
        receive(self.pos, {'schema_version': 1, 'event_id': 'reject', 'occurred_at': timezone.now().isoformat(), 'data': {'type': 'order.updated', 'accepted_order_id': str(record.pk), 'external_id': 'pos-order', 'sequence': 1, 'status': 'rejected'}})
        record.refresh_from_db()
        self.assertEqual(record.state, 'review')
        self.assertEqual(record.payments.get().captured_minor, 1005)
        self.assertEqual(record.pos_state, 'rejected')
        receive(self.gateway, self.event(record, 'capture-repeated', sequence=2))
        self.assertEqual(Order.objects.get(pk=record.order_id).payment_status, 'paid')
        self.assertFalse(record.commands.filter(kind='payment.refund').exists())
        self.assertFalse(record.issues.filter(code='late_payment').exists())

    def test_real_checkout_uses_shared_pricing_and_reservations(self):
        session = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, platform='website', session_id='test')
        config = default_checkout_config()
        config['modes'] = {'pickup': {'required_fields': [], 'payment_methods': ['cash'], 'fee': '2'}}
        self.config.policy.update(packaging_minor=100, taxes=[{'code': 'tax', 'name': 'Tax', 'rate': '10', 'tax_fees': True}]); self.config.save()
        args = dict(tenant=self.tenant, customer=self.customer, chat_id='test', platform='website', basket=self.basket, checklist={}, configuration=config)
        reply, _, _ = advance_checkout(text='checkout', **args)
        self.assertIn('total: 14.36', reply)
        _, order, _ = advance_checkout(text='confirm', **args)
        self.assertEqual(order.total_amount, Decimal('14.36'))
        self.assertEqual(order.commerce_record.total_minor, 1436)
        self.assertEqual(order.tax_amount, Decimal('1.31'))
        self.assertEqual(order.commerce_record.reservations.get().state, 'consumed')

    def test_no_payment_command_after_expiry(self):
        record = self.accept()
        AcceptedOrder.objects.filter(pk=record.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(claim(self.gateway), [])
        self.assertEqual(record.commands.get(kind='payment.create').status, 'failed')

    def test_reconciliation_recovers_missing_pos_and_queries_uncertain_payment(self):
        record = self.accept()
        self.pos.active = False; self.pos.save()
        receive(self.gateway, self.event(record))
        self.pos.active = True; self.pos.save()
        reconcile(); reconcile()
        self.assertEqual(record.commands.filter(kind='order.submit').count(), 1)
        self.assertFalse(record.issues.filter(code='pos_unconfigured', resolved_at__isnull=True).exists())
        self.stock.on_hand = 1; self.stock.save()
        second = self.accept()
        Payment.objects.filter(accepted_order=second).update(updated_at=timezone.now() - timedelta(minutes=10))
        reconcile(); reconcile()
        self.assertEqual(second.commands.filter(kind='payment.reconcile').count(), 1)

    def test_mapping_subject_scope_and_immutable_identity(self):
        from commerce.api import assert_mapping_subject
        from orders.models import MenuItem
        other = TenantInfo.objects.create(display_name='Unrelated')
        item = MenuItem.objects.create(tenant=other, name='Foreign')
        with self.assertRaises(ValueError):
            assert_mapping_subject(self.pos, 'item', str(item.pk))
        record = self.accept()
        assert_mapping_subject(self.pos, 'order_line', f'{record.order_id}:1')
        with self.assertRaises(ValueError):
            assert_mapping_subject(self.pos, 'order_line', f'{record.order_id}:99')
        path = f'/v1/connections/{self.pos.pk}/mappings/'
        def send(external_id):
            body = json.dumps({'kind': 'item', 'canonical_id': str(self.item.pk), 'external_id': external_id}).encode()
            stamp = str(int(time.time()))
            return self.client.post(path, data=body, content_type='application/json', HTTP_X_COMMERCE_TIMESTAMP=stamp,
                HTTP_X_COMMERCE_SIGNATURE=signature(adapter_secret(self.pos), stamp, 'POST', path, body))
        self.assertEqual(send('external-1').status_code, 200)
        self.assertEqual(send('external-1').status_code, 200)
        self.assertEqual(send('external-2').status_code, 400)

    @override_settings(ROOT_URLCONF='tests.support.urls')
    def test_dashboard_policy_is_validated_and_tenant_scoped(self):
        from django.contrib.auth.models import User
        from users.models import TenantProfile
        from orders.models import CheckoutSettings
        user = User.objects.create_user(username='commerce-owner', password='test')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        CheckoutSettings.objects.create(tenant=self.tenant)
        other = TenantInfo.objects.create(display_name='Other commerce')
        other_location = Location.objects.create(tenant=other, code='other', name='Other')
        other_config = Configuration.objects.create(tenant=other, location=other_location)
        values = dict(enabled='on', currency='EUR', packaging_minor=10, minimum_minor=0,
                      stock_policy='strict', reservation_seconds=900, stock_max_age_seconds=300,
                      taxes='[]', discounts='[]')
        self.assertEqual(self.client.post('/commerce/settings/', values).status_code, 302)
        self.config.refresh_from_db(); other_config.refresh_from_db()
        self.assertEqual(self.config.policy['currency'], 'EUR')
        self.assertEqual(other_config.policy['currency'], 'INR')
        values['packaging_minor'] = -1
        self.assertEqual(self.client.post('/commerce/settings/', values).status_code, 400)
        self.config.refresh_from_db(); self.assertEqual(self.config.policy['packaging_minor'], 10)

    def test_postgres_trigger_rejects_bulk_snapshot_rewrite(self):
        from django.conf import settings
        from django.db import connection, IntegrityError
        if connection.vendor != 'postgresql' or settings.MIGRATION_MODULES.get('commerce', '') is None:
            self.skipTest('Requires PostgreSQL and the real commerce migrations.')
        record = self.accept()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcceptedOrder.objects.filter(pk=record.pk).update(total_minor=1)


class ConcurrentReservationTests(Fixtures, TransactionTestCase):
    def setUp(self):
        self.seed()

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_customers_one_item(self):
        barrier = Barrier(2)
        def checkout():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    self.accept()
                return 'accepted'
            except ValueError:
                return 'sold_out'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: checkout(), range(2)))
        self.assertCountEqual(results, ['accepted', 'sold_out'])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.reserved, 1)
        self.assertEqual(AcceptedOrder.objects.count(), 1)
