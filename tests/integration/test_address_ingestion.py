"""Graph/ORM contracts with canned extraction; live semantics use evaluate_address_extraction.py."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import AddressComponents, ClassifiedMessages, IntentClassification
from chatbot_core.logic.cafe.workflow import graph, runner
from orders.models import CustomerAddress
from tests.support.checkout import CheckoutFixture
from tests.support.llm import ProviderHarness


class AddressIngestionTests(ProviderHarness, CheckoutFixture, TestCase):
    def setUp(self):
        CheckoutFixture.setUp(self)
        ProviderHarness.setUp(self)
        self.store = self.graph_store()
        self.tenant.meta = {'serviceable_pincodes': ['122011']}
        self.tenant.save()
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))

    def send(self, original, query, rewrite, *, fields=None, topic='add_delivery_address',
             intent='location_based', reply_to=None):
        self.payload = {key: None for key in AddressComponents.model_fields}
        self.payload.update(fields or {})
        row = IntentClassification(query=query, rephrased_sentence=rewrite,
            intent=intent, sub_intent=topic, reply_to=reply_to, clarification=None)
        with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages(
                classifications=[row], declared_constraints=[], response_language='es')):
            return runner.run_conversation(self.tenant, self.store, original, self.customer)[0]

    def test_s163_complete_address_after_pause_reaches_confirmation(self):
        initial = 'quiero delivery, torre 4 cerca de sector 56, nada más tengo eso'
        self.send(initial, initial, 'Deliver to tower 4 near sector 56; other details unknown.',
                  fields={'street_address': 'torre 4 cerca de sector 56'})
        draft = deepcopy(self.store.get_delivery_address())
        pending = self.store.get_ongoing_queries()[0][-1]
        self.assertFalse(CustomerAddress.objects.exists())

        with patch('chatbot_core.logic.cafe.intent_handler.general.generate_response_from_knowledge',
                   return_value='Tómate tu tiempo.'):
            self.send('espera, estoy buscando el código postal en el whatsapp', 'espera',
                      'Wait while I find the postal code.', intent='general', topic='wait')
        self.assertEqual(len(self.requests), 1)  # A pause must not extract or save an address.
        self.assertEqual(self.store.get_delivery_address(), draft)
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].query_id, pending.query_id)
        self.assertFalse(CustomerAddress.objects.exists())

        original = 'ya, flat 11, sector 56, gurugram 122011 State: Haryana. Country: India.'
        resolved = 'Sí, flat 11, sector 56, Gurugram 122011. Estado: Haryana. País: India.'
        rewrite = 'Yes, the address is Flat 11, Sector 56, Gurugram 122011, Haryana, India.'
        components = {'street_address': 'flat 11, torre 4 cerca de sector 56',
                      'city': 'gurugram', 'state': 'Haryana', 'country': 'India',
                      'postal_code': '122011'}
        reply = self.send(original, resolved, rewrite, fields=components,
                          reply_to=str(pending.query_id))
        # Inspect the real extraction request, not a mocked extractor's arguments.
        request = self.requests[-1]['messages'][1]['content']
        for value in (original, resolved, rewrite, draft['street_address']):
            self.assertIn(value, request)
        self.assertIn('Please confirm', reply)
        self.assertNotIn('Please share the following address details', reply)
        saved = CustomerAddress.objects.get(customer=self.customer)
        self.assertEqual(saved.components, components)
        self.assertEqual(self.store.get_delivery_address()['address_id'], str(saved.id))
        self.assertFalse(self.store.get_checklist()['location'])
        confirmation = self.store.get_ongoing_queries()[0][-1]
        self.assertEqual(confirmation.sub_intent, 'confirm_delivery_address')

        self.send('sí, esa dirección está bien', 'Confirm the delivery address',
                  'Confirm the current delivery address.', topic='confirm_delivery_address',
                  reply_to=str(confirmation.query_id))
        self.assertTrue(self.store.get_checklist()['location'])
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(CustomerAddress.objects.filter(customer=self.customer).count(), 1)
        saved.refresh_from_db()
        self.assertEqual(saved.components, components)

    def test_acknowledgment_cannot_fill_missing_fields_from_resolved_context(self):
        self.store.set_delivery_address({'street_address': 'torre 4 cerca de sector 56'})
        reply = self.send('ya', 'Flat 11, Sector 56, Gurugram, Haryana, India, 122011',
                          'Confirm the address from context.')
        self.assertIn('city, state, country, valid 6-digit pincode', reply)
        self.assertFalse(CustomerAddress.objects.exists())
        self.assertEqual(self.store.get_delivery_address(),
                         {'street_address': 'torre 4 cerca de sector 56'})
        self.assertFalse(self.store.get_checklist()['location'])
