"""Business-state checks with scripted interpretation; no claims about live model accuracy."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.cafe.workflow import graph, runner
from chatbot_core.logic.cafe.intent_handler.information_about_the_cafe import InformationAboutCafeIntent
from chatbot_core.logic.cafe.intent_handler.order_enquiry import OrderEnquiryIntent
from chatbot_core.logic.cafe import db_utils
from tests.support.checkout import CheckoutFixture


class InterpretationResponseTests(CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.graph_store()
        self.enterContext(patch.object(graph, "enqueue_string"))
        self.enterContext(patch.object(runner, "enqueue_string"))

    def row(self, *, quantity=1, intent='placing_order', topic='add_to_basket', query='Add Coffee'):
        action = None
        if intent == 'placing_order':
            action = ActionProposal(kind='CHANGE_BASKET', basket={
                'lines': [{'action': 'add', 'item_id': str(self.item.pk), 'variant_id': str(self.variant.pk),
                           'quantity': quantity, 'modifiers': [], 'target_number': None, 'unresolved': []}],
                'unresolved': [], 'catalog_miss': False})
        return IntentClassification(query=query, intent=intent, sub_intent=topic,
                                    reply_to=None, clarification=None, action=action)

    def send(self, rows, *, language=None, query='Please add coffee'):
        result = ClassifiedMessages(classifications=rows, declared_constraints=[], response_language=language)
        with patch.object(graph, 'normalize_and_classify', return_value=result):
            return runner.run_conversation(self.tenant, self.store, query, self.customer)[0]

    def test_fractional_quantity_is_preserved_and_blocks_entire_basket_proposal(self):
        before = deepcopy(self.store.get_basket().items)
        row = self.row(quantity=1.5)
        row.action.basket.lines.insert(0, self.row(quantity=2).action.basket.lines[0])
        self.assertEqual(row.action.basket.lines[1].quantity, 1.5)
        reply = self.send([row], query='Add 2 coffees and 1.5 coffees')
        self.assertIn('whole-number', reply)
        self.assertEqual(self.store.get_basket().items, before)
        self.assertEqual(len(self.store.get_ongoing_queries()[0]), 1)

    def test_localization_is_presentation_only_and_language_survives_short_reply(self):
        with patch.object(graph, 'localize_reply', return_value=('Añadido 2 × Coffee (Regular).', '')) as localize:
            reply = self.send([self.row(quantity=2)], language='es', query='Añade dos Coffee')
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)
        self.assertEqual(self.store.get_checklist()['last_assistant_message'], reply)
        self.assertEqual(self.store.get_checklist()['response_language'], 'es')
        self.assertEqual(localize.call_args.args[2], 'es')
        with patch.object(graph, 'localize_reply', return_value=('Añadido 1 × Coffee (Regular).', '')) as localize:
            self.send([self.row()], query='Uno')
        self.assertEqual(localize.call_args.args[2], 'es')
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 4)

    def test_repeated_read_only_answers_are_emitted_once(self):
        row = self.row(intent='information_about_the_cafe', topic='location_and_hours', query='Sunday hours?')
        def answer(obj, *args, **kwargs):
            obj.response = 'Open Sunday.'
            obj.is_complete = True
            return obj.response, None
        with patch.object(InformationAboutCafeIntent, '_respond', autospec=True, side_effect=answer):
            reply = self.send([row, row.model_copy(deep=True)], query='Sunday hours and closing time?')
        self.assertEqual(reply, 'Open Sunday.')
        self.assertEqual(self.store.get_basket().items, self.basket.items)

    def test_separate_identical_mutations_retain_both_confirmations(self):
        reply = self.send([self.row(), self.row()], query='Add one coffee, then add one more')
        self.assertEqual(reply.count('Added 1 × Coffee'), 2)
        self.assertNotIn('..', reply)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)

    def test_no_customer_does_not_claim_a_lookup_and_empty_results_are_scoped(self):
        intent = OrderEnquiryIntent(main_query='My orders?', sub_intent='get_order_history',
            tenant=self.tenant.pk, chat_id='chat')
        with patch.object(db_utils, 'get_order_history') as lookup:
            reply, _ = intent.process_query(self.basket, {}, {}, [], self.tenant.api_key, None)
        lookup.assert_not_called()
        self.assertIn('verified customer record', reply)
        reply = db_utils.get_order_history(self.customer)
        self.assertIn('records available to this chat', reply)
        self.assertNotIn('your account', reply)

    def test_cart_answers_requested_quantities_and_subtotal(self):
        row = self.row()
        row.action = ActionProposal(kind='SHOW_CART')
        reply = self.send([row], query='Show quantities and total')
        self.assertIn('1 × Coffee', reply)
        self.assertIn('Item subtotal: INR 100.00', reply)
