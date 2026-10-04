"""Replay recorded model proposals against real state; no live model in this suite."""
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ClassifiedMessages
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.workflow import graph, runner
from orders.models import CheckoutSettings, Order
from tests.support.checkout import CheckoutFixture


EVIDENCE = json.loads((Path(__file__).parents[1] / 'fixtures/state_transition_evidence.json').read_text())


class SavedTransitionTests(CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.config['modes']['delivery']['required_fields'] = ['name', 'phone', 'address', 'postal_code']
        self.config['modes']['pickup']['required_fields'] = ['name', 'phone']
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.item.name = 'Pistachio Ice Cream'
        self.item.save()
        self.variant.size = 'QA standard'
        self.variant.save()
        self.store = self.graph_store()
        self.store.set_basket(Basket())
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))

    def replay(self, scenario, turn, *, proposal=None, message=None):
        row = next(r for r in EVIDENCE['turns'] if r['scenario'].startswith(scenario + '_')
                   and r['user_turn_index'] == turn)
        payload = deepcopy(proposal or row['proposal'])
        for classification in payload['classifications']:
            action = classification.get('action') or {}
            for line in (action.get('basket') or {}).get('lines', []):
                if line.get('item_id'):
                    line['item_id'] = str(self.item.pk)
                # Preserve the corrupted UUID actually emitted on s135 turn 0.
                if line.get('variant_id') and line['variant_id'] != 'bb450214-31da-45f7-a27b-288b9bb6dbe6':
                    line['variant_id'] = str(self.variant.pk)
        with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages.model_validate(payload)):
            result = runner.run_conversation(self.tenant, self.store, message or row['message'], self.customer)
        # Re-open the store on every turn to exercise persistence, not object identity.
        self.store = type(self.store)('chat', tenant_id=self.tenant.pk, platform='website')
        return result[0]

    def test_s149_repeated_removal_is_terminal_and_checkout_reports_empty_basket(self):
        self.item.name = 'Tiramisu'
        self.item.save()
        self.replay('s149', 0)
        self.replay('s149', 1)
        self.assertTrue(self.store.get_basket().is_empty())
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertIn('empty', self.replay('s149', 2))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertIn('empty', self.replay('s149', 3))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertFalse(Order.objects.exists())

    def test_impossible_removal_with_model_clarification_is_still_terminal(self):
        row = next(r for r in EVIDENCE['turns'] if r['scenario'].startswith('s149_')
                   and r['user_turn_index'] == 2)
        payload = deepcopy(row['proposal'])
        payload['classifications'][0]['clarification'] = 'Which entry do you mean?'
        self.assertIn('empty', self.replay('s149', 2, proposal=payload))
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_legacy_impossible_removal_is_pruned_before_classification(self):
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        pending = PlacingOrderIntent(main_query='Remove Tiramisu again', sub_intent='delete_entry',
            tenant=self.tenant.pk, chat_id='chat', query_id='stale', follow_up_question=['Which entry?'])
        pending.platform = 'website'
        self.store.set_ongoing_queries([pending], 0)
        self.assertIn('empty', self.replay('s149', 3))
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_s135_name_and_pickup_cannot_complete_size_or_disappear(self):
        self.assertIn('size', self.replay('s135', 0))
        pending = self.store.get_ongoing_queries()[0][0]
        self.assertTrue(self.store.get_basket().is_empty())
        self.replay('s135', 1)
        self.replay('s135', 2)
        self.replay('s135', 3)
        self.assertTrue(self.store.get_basket().is_empty())
        self.assertIn(pending.query_id, [p.query_id for p in self.store.get_ongoing_queries()[0]])
        self.session.refresh_from_db()
        draft = self.session.state['checkout']
        self.assertEqual(draft['mode'], 'pickup')
        self.assertEqual(draft['fields']['name'], 'QA Guest')
        self.assertIsNone(draft.get('quote'))
        self.assertFalse(Order.objects.exists())
        # The same valid selection becomes executable only when the user supplies it.
        self.replay('s135', 3, message='QA standard')
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
        reply, _ = self.graph_turn(self.store, 'checkout')
        self.assertIn('phone', reply)
        self.assertEqual(self.store.get_checklist()['checkout']['fields']['name'], 'QA Guest')

    def test_s122_address_clarification_preserves_fields(self):
        self.assert_address_recovery('s122')

    def test_s143_address_and_postcode_clarifications_preserve_fields(self):
        self.assert_address_recovery('s143')

    def assert_address_recovery(self, scenario):
        self.session.state = {}
        self.session.save()
        self.store.set_basket(Basket())
        self.store.set_checklist({})
        self.store.set_ongoing_queries([], None)
        for turn in range(7):
            self.replay(scenario, turn)
        self.session.refresh_from_db()
        fields = self.session.state['checkout']['fields']
        self.assertEqual(fields, {'name': 'QA Guest', 'phone': '0000000000',
            'address': 'Flat 8, Ramgarh Road, Sector 64, Gurugram', 'postal_code': '122102'})
        self.assertEqual(self.store.get_checklist()['checkout']['fields'], fields)
        quote = self.session.state['checkout']['quote']
        self.assertIsNotNone(quote)
        # Recovery from the durable draft must retain every supplied field.
        self.store.set_checklist({})
        self.store.set_ongoing_queries([], None)
        self.graph_turn(self.store, 'checkout')
        self.assertEqual(self.store.get_checklist()['checkout']['fields'], fields)
        self.graph_turn(self.store, 'postal code: 122103')
        self.assertNotEqual(self.store.get_checklist()['checkout']['quote']['fingerprint'], quote['fingerprint'])


    def test_s135_restart_unfinished_draft_is_durable_after_cache_failure(self):
        self.store.set_basket(deepcopy(self.basket))
        self.graph_turn(self.store, 'delivery')
        with patch.object(self.store, 'publish_snapshot', side_effect=ConnectionError('cache unavailable')):
            with self.assertRaises(ConnectionError):
                self.replay('s135', 9)
        self.graph_turn(self.store, 'show basket', ('placing_order', 'check_order_cart'))
        self.assertTrue(self.store.get_basket().is_empty())
        self.assertFalse(self.store.get_checklist().get('checkout'))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.session.refresh_from_db()
        self.assertFalse(self.session.state['checkout'])
        self.assertFalse(Order.objects.exists())

    def test_s155_saved_bad_classifications_preserve_task_and_corrected_quantity_resumes_it(self):
        self.replay('s155', 0)
        pending = self.store.get_ongoing_queries()[0][0]
        self.replay('s155', 1)
        self.replay('s155', 2)
        # The original run misclassified both replies, but did not lose this task.
        self.assertEqual(self.store.get_ongoing_queries()[0][0].query_id, pending.query_id)
        self.assertTrue(self.store.get_basket().is_empty())
        row = next(r for r in EVIDENCE['turns'] if r['scenario'].startswith('s155_') and r['user_turn_index'] == 2)
        fixed = deepcopy(row['proposal'])
        classification = fixed['classifications'][0]
        classification.update(clarification=None, query='Pistachio Ice Cream ki quantity 2 kar do.')
        body = classification['action']['basket']
        body['unresolved'] = []
        body['lines'][0].update(quantity=2, unresolved=[])
        self.replay('s155', 2, proposal=fixed)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
        self.assertFalse(self.store.get_ongoing_queries()[0])
