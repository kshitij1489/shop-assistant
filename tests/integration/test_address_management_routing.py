"""Address-management intents retain their operation despite selection proposals."""
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.cafe.workflow import graph, runner
from chatbot_core.runtime_configuration import RuntimeConfiguration
from orders.models import Customer, CustomerAddress
from tests.support.checkout import CheckoutFixture


class AddressManagementRoutingTests(CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.graph_store()
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))
        self.enterContext(patch.object(graph, 'render_reply', side_effect=lambda **kw:
                                     (kw['response'], kw['question'])))
        self.work = self.saved('Work', default=True)
        self.home = self.saved('Home')
        other = Customer.objects.create(tenant=self.tenant, name='Other', phone='9876543210')
        self.other_home = self.saved('Home', customer=other, default=True)

    def saved(self, label, *, customer=None, default=False):
        return CustomerAddress.objects.create(
            tenant=self.tenant, customer=customer or self.customer, label=label,
            address_line=f'{label}, 42 Main Street, Delhi, Delhi, 110001, India',
            components={'street_address': '42 Main Street', 'city': 'Delhi',
                        'state': 'Delhi', 'postal_code': '110001', 'country': 'India'},
            is_default=default)

    def send(self, text, topic, label):
        row = IntentClassification(
            query=text, rephrased_sentence=text, intent='location_based', sub_intent=topic,
            reply_to=None, clarification=None,
            action=ActionProposal(kind='SELECT_ADDRESS', reference={'by': 'name', 'value': label}))
        with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages(
                classifications=[row], declared_constraints=[])):
            reply, _ = runner.run_conversation(self.tenant, self.store, text, self.customer)
        return reply

    def address_state(self):
        return list(CustomerAddress.objects.order_by('id').values())

    def test_s36_preserves_management_operations_with_or_without_selection_capability(self):
        allows = RuntimeConfiguration.allows
        for selection_enabled in (False, True):
            with self.subTest(selection_enabled=selection_enabled):
                self.store = self.graph_store()
                self.store.set_delivery_address({})
                CustomerAddress.objects.filter(pk=self.work.pk).update(is_default=True)
                CustomerAddress.objects.filter(pk=self.home.pk).update(is_default=False)
                before = self.address_state()
                with patch.object(RuntimeConfiguration, 'allows', lambda config, intent, topic:
                                  (selection_enabled if (intent, topic) ==
                                   ('location_based', 'choose_delivery_address')
                                   else allows(config, intent, topic))):
                    reply = self.send('Delete the Work one.', 'delete_delivery_address', 'Work')
                    self.assertIn('through the app', reply)
                    self.assertEqual(self.address_state(), before)
                    self.assertFalse(self.store.get_delivery_address())
                    self.assertFalse(self.store.get_ongoing_queries()[0])

                    reply = self.send('Make Home the default for next time.',
                                      'set_default_delivery_address', 'Home')
                self.assertIn('Your default address is now', reply)
                self.assertEqual(dict(CustomerAddress.objects.filter(customer=self.customer)
                                      .values_list('label', 'is_default')), {'Work': False, 'Home': True})
                self.other_home.refresh_from_db()
                self.assertTrue(self.other_home.is_default)
                self.assertEqual(self.store.get_delivery_address()['address_id'], str(self.home.pk))
                self.assertFalse(self.store.get_ongoing_queries()[0])
                after = self.address_state()
                self.assertEqual(len(after), len(before))
                for old, new in zip(before, after):
                    for field in old.keys() - {'is_default', 'updated_at'}:
                        self.assertEqual(new[field], old[field])

    def test_management_still_requires_its_own_capability(self):
        allows = RuntimeConfiguration.allows
        for topic, label in [('delete_delivery_address', 'Work'),
                             ('set_default_delivery_address', 'Home')]:
            with self.subTest(topic=topic):
                before = self.address_state()
                with patch.object(RuntimeConfiguration, 'allows', lambda config, intent, name:
                                  (intent, name) != ('location_based', topic)
                                  and allows(config, intent, name)):
                    reply = self.send(f'Manage {label}', topic, label)
                self.assertIn('unavailable', reply)
                self.assertEqual(self.address_state(), before)
                self.assertFalse(self.store.get_delivery_address())

    def test_actual_delivery_selection_keeps_its_action_and_confirmation(self):
        before = self.address_state()
        reply = self.send('Use Home for delivery.', 'choose_delivery_address', 'Home')
        self.assertIn('confirm', reply.lower())
        self.assertEqual(self.store.get_delivery_address()['address_id'], str(self.home.pk))
        self.assertFalse(self.store.get_checklist()['location'])
        self.assertEqual(self.address_state(), before)
