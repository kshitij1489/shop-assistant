import json
from django.test import SimpleTestCase

from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import BaseIntent
from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
from chatbot_core.logic.cafe.workflow.pending import resumable_pending
from chatbot_core.logic.outcomes import TaskOutcome


class TaskOutcomeTests(SimpleTestCase):
    def intent(self, topic='add_to_basket', **kwargs):
        return PlacingOrderIntent(main_query='Coffee', sub_intent=topic,
                                  tenant=1, chat_id='chat', **kwargs)

    def test_every_outcome_survives_json_and_legacy_boolean_remains_consistent(self):
        for outcome in TaskOutcome:
            with self.subTest(outcome=outcome):
                intent = self.intent()
                intent.set_outcome(outcome, 'Reply')
                restored = BaseIntent.from_dict(json.loads(json.dumps(intent.to_dict())))
                self.assertEqual(restored.outcome, outcome)
                self.assertEqual(restored.is_complete, not outcome.resumable)
                self.assertEqual(resumable_pending(restored, {'basket': Basket(), 'checklist': {}}),
                                 outcome.resumable)
        for complete in (True, False):
            saved = self.intent(is_complete=complete).to_dict()
            saved.pop('outcome')
            restored = BaseIntent.from_dict(saved)
            self.assertEqual(restored.outcome, TaskOutcome.COMPLETED if complete else TaskOutcome.NEEDS_CLARIFICATION)

    def test_empty_legacy_removal_retires_but_unfinished_addition_remains(self):
        state = {'basket': Basket(), 'checklist': {}}
        removal = self.intent('delete_entry', follow_up_question=['Which entry?'])
        self.assertFalse(resumable_pending(removal, state))
        self.assertEqual(removal.outcome, TaskOutcome.TERMINAL_REJECTION)
        self.assertTrue(resumable_pending(self.intent(follow_up_question=['Which size?']), state))

    def test_legacy_store_only_requests_are_not_resumed(self):
        for topic in PlacingOrderIntent.STORE_CONTACT_REPLIES:
            with self.subTest(topic=topic):
                intent = self.intent(topic, follow_up_question=['Please provide details.'])
                self.assertFalse(resumable_pending(intent, {'basket': Basket(), 'checklist': {}}))
                self.assertEqual(intent.outcome, TaskOutcome.TERMINAL_REJECTION)
                self.assertFalse(intent.follow_up_question)

    def test_restored_removal_with_missing_target_retires_despite_unanswered_quantity(self):
        proposal = {'lines': [{'action': 'remove', 'item_id': None, 'variant_id': None,
            'quantity': None, 'modifiers': None, 'target_number': None,
            'reference': {'by': 'id', 'value': '99'}, 'unresolved': ['How many?']}],
            'unresolved': ['How many?'], 'catalog_miss': False}
        for details in ({'proposal': proposal}, {'action_proposal': {'kind': 'CHANGE_BASKET', 'basket': proposal}}):
            with self.subTest(details=details):
                intent = BaseIntent.from_dict(self.intent('delete_entry', basket_item=details).to_dict())
                state = {'basket': Basket(items=[{'item_number': 1, 'name': 'Coffee'}]), 'checklist': {}}
                self.assertFalse(resumable_pending(intent, state))
                self.assertEqual(intent.outcome, TaskOutcome.TERMINAL_REJECTION)

    def test_placement_retires_checkout_and_basket_tasks_without_touching_order_records(self):
        state = {'basket': Basket(), 'checklist': {'order': True, 'order_id': 'saved', 'payment': False}}
        for intent in (self.intent(), self.intent('order_confirmation', basket_item={'checkout': True})):
            self.assertFalse(resumable_pending(intent, state))
        retry = self.intent('order_payment', basket_item={'payment_recovery': True})
        retry.set_outcome(TaskOutcome.TEMPORARILY_BLOCKED, 'Try payment again')
        self.assertTrue(resumable_pending(retry, state))
        self.assertEqual(state['checklist'], {'order': True, 'order_id': 'saved', 'payment': False})
