"""Payment recovery must deliver the stored URL, not just leave valid state."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from chatbot_core.llm.schemas import ActionProposal
from chatbot_core.logic.cafe.basket import Basket
from commerce.models import AcceptedOrder, Command, Connection, Location, Payment
from orders.models import CheckoutSettings, Customer, Order
from tests.support.checkout import CheckoutFixture


class PaymentRecoveryTests(CheckoutFixture, TestCase):
    url = 'https://pay.example/checkout/abc?token=a%2Fb&reference=42'

    def setUp(self):
        super().setUp()
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.store = self.graph_store()
        self.order = Order.objects.create(tenant=self.tenant, customer=self.customer,
            payment_mode='online', total_amount='105.00')
        self.session.order = self.order
        self.session.save(update_fields=['order'])
        location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        connection = Connection.objects.create(location=location, provider='test', role='payment')
        self.record = AcceptedOrder.objects.create(order=self.order, location=location,
            currency='INR', total_minor=10500, snapshot={}, snapshot_hash='test',
            expires_at=timezone.now() + timedelta(minutes=15))
        self.payment = Payment.objects.create(accepted_order=self.record, connection=connection,
            requested_minor=10500, currency='INR', checkout_url=self.url)

    def recover(self):
        return self.graph_turn(self.store, 'Send my payment link again',
            classification=('placing_order', 'order_payment'),
            action=ActionProposal(kind='RECOVER_PAYMENT'))[0]

    def test_recovery_returns_exact_url_after_session_state_loss_without_new_effects(self):
        self.store.set_basket(Basket())
        self.store.set_checklist({})
        with patch('chatbot_core.logic.cafe.checkout.advance_checkout',
                   side_effect=AssertionError('Recovery must not replay checkout')):
            for _ in range(2):
                self.assertIn(self.url, self.recover())
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertFalse(Command.objects.exists())

    def test_pending_then_reconciled_payment_returns_link_on_next_request(self):
        self.payment.checkout_url = ''
        self.payment.save(update_fields=['checkout_url'])
        self.assertIn('prepares a secure payment link', self.recover())
        pending = self.store.get_ongoing_queries()[0][-1]
        self.assertEqual(pending.outcome, 'temporarily_blocked')
        self.assertTrue(pending.basket_item.get('payment_recovery'))
        self.payment.checkout_url = self.url
        self.payment.save(update_fields=['checkout_url'])
        self.assertIn(self.url, self.recover())
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertFalse(Command.objects.exists())

    def test_no_chat_order_does_not_create_one_or_select_another_order(self):
        self.session.order = None
        self.session.save(update_fields=['order'])
        self.assertIn('no placed order', self.recover())
        self.assertEqual(Order.objects.count(), 1)

    def test_recovery_rechecks_customer_ownership(self):
        other = Customer.objects.create(tenant=self.tenant, name='Other', phone='9999999999')
        self.order.customer = other
        self.order.save(update_fields=['customer'])
        reply = self.recover()
        self.assertIn('couldn’t verify', reply)
        self.assertNotIn(self.url, reply)

    def test_stale_checkout_reference_cannot_select_an_order(self):
        from uuid import uuid4
        self.store.set_checklist({'order_id': str(uuid4())})
        reply = self.recover()
        self.assertIn('couldn’t verify', reply)
        self.assertNotIn(self.url, reply)

    def test_unpayable_orders_do_not_return_link(self):
        for state, expected in [('paid', 'already confirmed'), ('cancelled', 'cancelled'),
                                ('review', 'do not pay again'), ('expired', 'couldn’t get'),
                                ('cash', 'Pay cash')]:
            with self.subTest(state=state):
                self.order.payment_status = Order.PaymentStatus.PAID if state == 'paid' else Order.PaymentStatus.UNPAID
                self.order.order_status = Order.Status.CANCELLED if state == 'cancelled' else Order.Status.PENDING
                self.order.payment_mode = 'cash' if state == 'cash' else 'online'
                self.order.save()
                self.record.state = 'review' if state == 'review' else 'awaiting_payment'
                self.record.expires_at = timezone.now() + timedelta(minutes=-1 if state == 'expired' else 15)
                self.record.save()
                reply = self.recover()
                self.assertIn(expected, reply)
                self.assertNotIn(self.url, reply)

    def test_invalid_provider_url_is_not_returned(self):
        self.payment.checkout_url = 'http://pay.example/insecure'
        self.payment.save(update_fields=['checkout_url'])
        reply = self.recover()
        self.assertIn('couldn’t get', reply)
        self.assertNotIn(self.payment.checkout_url, reply)
