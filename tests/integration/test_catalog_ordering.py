"""Catalog-backed ordering contracts; provider responses are deliberately untrusted."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from chatbot_core import knowledge_cache
from chatbot_core.llm.schemas import OrderProposal
from chatbot_core.logic.cafe import order_interpreter
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.catalog import load_catalog, positive_integer, validate_selection
from chatbot_core.logic.cafe.db_utils import create_order
from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
from chatbot_core.logic.cafe.intent_handler.insufficient_information import InsufficientInformationIntent
from tests.support.actions import execute_change
from chatbot_core.models import TenantInfo
from orders.models import MenuItem, MenuItemVariant, AddonGroup, AddonItem, ItemAddonGroup, Customer, Order


class CatalogOrderingTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(slug="catalog-orders", display_name="Cafe")
        self.other = TenantInfo.objects.create(slug="other-cafe", display_name="Other")
        self.customer = Customer.objects.create(tenant=self.tenant, phone="123")
        self.latte = MenuItem.objects.create(tenant=self.tenant, name="Latte", meta={"aliases": ["cafe latte"]})
        self.small = MenuItemVariant.objects.create(menu_item=self.latte, size="Small", price="100", volume_ml=200)
        self.large = MenuItemVariant.objects.create(menu_item=self.latte, size="Large", price="150", aliases=["12 oz"])
        self.cap = MenuItem.objects.create(tenant=self.tenant, name="Cappuccino")
        self.cap_small = MenuItemVariant.objects.create(menu_item=self.cap, size="Small", price="110")
        self.milk = AddonGroup.objects.create(tenant=self.tenant, name="Milk")
        self.milk_link = ItemAddonGroup.objects.create(tenant=self.tenant, item=self.latte, group=self.milk, max_selections=1)
        self.oat = AddonItem.objects.create(group=self.milk, name="Oat milk", price="30", aliases=["oat"])
        self.soy = AddonItem.objects.create(group=self.milk, name="Soy milk", price="20")
        self.sweet = AddonGroup.objects.create(tenant=self.tenant, name="Sweetness")
        ItemAddonGroup.objects.create(tenant=self.tenant, item=self.latte, group=self.sweet)
        self.no_sugar = AddonItem.objects.create(group=self.sweet, name="No sugar", price="0")
        self.shots = AddonGroup.objects.create(tenant=self.tenant, name="Extra shots")
        ItemAddonGroup.objects.create(tenant=self.tenant, item=self.latte, group=self.shots)
        self.shot = AddonItem.objects.create(group=self.shots, name="Extra shot", price="15", max_quantity=3)
        self.refresh()
        from tests.support.ordering import seed_evaluation_policy
        seed_evaluation_policy(self.tenant)
        seed_evaluation_policy(self.other)
        self.basket = Basket()
        self.chain = self.enterContext(patch.object(order_interpreter, "structured_chain"))
        self.chain.return_value.invoke.side_effect = self.scripted_proposal

    def scripted_proposal(self, payload):
        """Explicit offline proposals. These fixtures do not test language understanding."""
        text = json.loads(payload['input'])['message']
        additions = {
            'Latte Large': self.line(), 'large latte': self.line(),
            'small latte': self.line(variant=self.small), 'Small latte': self.line(variant=self.small),
            '12 oz latte': self.line(), '200ml cafe latte': self.line(variant=self.small),
            'small cappuccino': self.line(item=self.cap, variant=self.cap_small),
            'Cappuccino': self.line(item=self.cap, variant=self.cap_small),
            '2 large latte': self.line(quantity=2), 'grande latte': self.line(),
            'two large latte with oat milk and no sugar': self.line(quantity=2, modifiers=[self.choice(self.oat), self.choice(self.no_sugar)]),
            'large latte with oat milk': self.line(modifiers=[self.choice(self.oat)]),
            'large oat latte': self.line(modifiers=[self.choice(self.oat)]),
        }
        if text in additions:
            return OrderProposal.model_validate(self.proposal(additions[text]))
        if text in ('Latte', 'latte', 'two latte'):
            line = self.line(quantity=2 if text == 'two latte' else 1)
            line['variant_id'] = None
            return OrderProposal.model_validate(self.proposal(line))
        if text == 'large':
            return OrderProposal.model_validate(self.proposal(self.line(quantity=2)))
        changes = {
            'cancel the latte': self.line(action='remove', quantity=None, target=1),
            'Remove item 1 from my basket.': self.line(action='remove', quantity=None, target=1),
            'Remove item 2.': self.line(action='remove', quantity=None, target=2),
            'make latte 3': self.line(action='update', quantity=3, target=1),
            'change latte to small': self.line(action='update', variant=self.small, quantity=3, target=1),
            'remove latte': self.line(action='remove', quantity=None, target=1),
        }
        if text in changes:
            return OrderProposal.model_validate(self.proposal(changes[text]))
        raise AssertionError('No offline proposal configured for ' + text)

    def refresh(self):
        knowledge_cache._ITEM_PRICING_CACHE.clear()
        knowledge_cache._ITEM_PRICING_CACHE.update(knowledge_cache.generate_all_menu_payload())
        self.addCleanup(knowledge_cache._ITEM_PRICING_CACHE.clear)

    def intent(self, text, action="add_to_basket"):
        obj = PlacingOrderIntent(main_query=text, sub_intent=action, tenant=self.tenant.pk, chat_id="user")
        obj.platform = "telegram"
        return obj

    def scripted(self, text):
        return self.chain.return_value.invoke({'input': json.dumps({'message': text})})

    def run_intent(self, obj):
        from tests.support.actions import resolved_change
        proposal = self.scripted(obj.main_query)
        obj.resolved_action = resolved_change(proposal, self.basket)
        return obj.process_query(self.basket, {}, {}, [], self.tenant.api_key, self.customer)[0]

    def follow(self, obj, text):
        from tests.support.actions import resolved_change
        with patch.object(base, "get_intent", return_value=PlacingOrderIntent):
            obj = base.BaseIntent.from_dict(json.loads(json.dumps(obj.to_dict())))
        incoming = self.intent(text, "customize_confirmation")
        proposal = self.follow_proposal(obj, text)
        incoming.resolved_action = resolved_change(proposal, self.basket)
        reply = obj.process_followup(incoming, self.basket, {}, {}, [], self.tenant.api_key, self.customer)[0]
        return obj, reply

    def follow_proposal(self, obj, text):
        stored = obj.basket_item.get('proposal')
        if stored and text.strip().isdigit():
            proposal = deepcopy(stored)
            for line in proposal['lines']:
                if line.get('action') != 'add':
                    line['reference'] = {'by': 'id', 'value': text.strip()}
                    line['target_number'] = int(text)
                    line['unresolved'] = []
            proposal['unresolved'] = []
            return proposal
        return self.scripted(text)

    def choice(self, addon, qty=1):
        return {"group_id": str(addon.group_id), "option_id": str(addon.pk), "quantity": qty}

    def line(self, *, action="add", item=None, variant=None, quantity=1, modifiers=None, target=None):
        return {"action": action, "item_id": str((item or self.latte).pk),
                "variant_id": str((variant or self.large).pk), "quantity": quantity,
                "modifiers": modifiers or [], "target_number": target, "unresolved": []}

    def proposal(self, *lines, unresolved=None):
        return {"lines": list(lines), "unresolved": unresolved or [], "catalog_miss": False}

    def respond(self, proposal):
        self.chain.return_value.invoke.side_effect = None
        self.chain.return_value.invoke.return_value = OrderProposal.model_validate(proposal)

    def test_named_cancel_removes_only_named_catalog_item_with_mocked_proposal(self):
        self.run_intent(self.intent('Latte Large'))
        self.run_intent(self.intent('Cappuccino'))
        reply = self.run_intent(self.intent('cancel the latte', 'delete_entry'))
        self.assertIn('Removed Latte', reply)
        self.assertEqual([row['name'] for row in self.basket.items], ['Cappuccino'])


    def test_removal_quantity_decrements_and_over_removal_is_atomic(self):
        self.respond(self.proposal(self.line(quantity=3)))
        self.run_intent(self.intent('3 large latte'))
        self.respond(self.proposal(self.line(action='remove', quantity=1, target=1)))
        self.assertIn('quantity to 2', self.run_intent(self.intent('Remove one latte','delete_entry')))
        before = deepcopy(self.basket.to_dict())
        self.respond(self.proposal(self.line(action='remove', quantity=3, target=1)))
        self.run_intent(self.intent('Remove three lattes','delete_entry'))
        self.assertEqual(self.basket.to_dict(),before)

    def test_requirements_are_disclosed_on_additions_and_silent_on_removals(self):
        self.respond(self.proposal(self.line(quantity=3)))
        self.run_intent(self.intent('3 large latte'))
        removal = self.line(action='remove', quantity=1, target=1)
        addition = self.line(item=self.cap, variant=self.cap_small)
        reply = execute_change(self.proposal(removal, addition), self.basket, self.tenant.api_key,
                               declared_constraints=['Milk allergy.'])
        self.assertIn('Added 1 × Cappuccino', reply)
        self.assertIn('You mentioned: Milk allergy.', reply)
        self.assertIn('café staff', reply)
        self.assertEqual([row['quantity'] for row in self.basket.items], [2, 1])
        reply = execute_change(self.proposal(self.line(action='remove', quantity=1, target=1)),
                               self.basket, self.tenant.api_key, declared_constraints=['Milk allergy.'])
        self.assertNotIn('staff', reply)
        self.assertEqual(self.basket.items[0]['quantity'], 1)

    def test_single_item_removal_requires_a_reference(self):
        for reference in ({'by': 'focus'}, {'by': 'name', 'value': 'latte'}, {'by': 'id', 'value': '1'}):
            with self.subTest(reference=reference):
                basket = Basket()
                basket.add_validated(validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(self.large.pk), 1, []), api_key=self.tenant.api_key)
                line = self.line(action='remove')
                line['reference'] = reference
                execute_change(self.proposal(line), basket, self.tenant.api_key)
                self.assertEqual(basket.items, [])
        basket = Basket()
        basket.add_validated(validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(self.large.pk), 1, []), api_key=self.tenant.api_key)
        with self.assertRaisesMessage(ValueError, 'Which entry'):
            execute_change(self.proposal(self.line(action='remove')), basket, self.tenant.api_key)
        self.assertEqual(len(basket.items), 1)

    def test_numbered_edits_use_the_selected_row_across_cafe_catalogs(self):
        from chatbot_core.logic.cafe.item_parser import parse_order_text

        tea = MenuItem.objects.create(tenant=self.other, name='Masala chai')
        cup = MenuItemVariant.objects.create(menu_item=tea, size='Cup', price='45')
        pot = MenuItemVariant.objects.create(menu_item=tea, size='Pot', price='90')
        references = ('item 2', 'ITEM 2', 'item #2', 'item number 2', 'entry 2', 'line 2', '#2')
        for tenant, item, variants in ((self.tenant, self.latte, (self.small, self.large)),
                                       (self.other, tea, (cup, pot))):
            catalog = load_catalog(tenant.api_key)
            for reference in references:
                with self.subTest(tenant=tenant.slug, reference=reference):
                    basket = Basket()
                    for variant in variants:
                        basket.add_validated(validate_selection(catalog, str(item.pk), str(variant.pk), 1, []), api_key=tenant.api_key)
                    first = deepcopy(basket.items[0])
                    for text, action in ((f'Change {reference} quantity to 3.', 'update_order'),
                                         (f'Remove {reference} from my basket.', 'delete_entry')):
                        proposed = {'action': 'update' if action == 'update_order' else 'remove',
                                    'item_id': str(item.pk), 'variant_id': None,
                                    'quantity': 3 if action == 'update_order' else None,
                                    'modifiers': None, 'target_number': 2, 'unresolved': []}
                        self.respond(self.proposal(proposed))
                        parsed = parse_order_text(tenant.api_key, text, basket=basket.items, action=action)
                        execute_change(parsed['proposal'], basket, tenant.api_key)
                        self.assertEqual(basket.items[0], first)
                        if action == 'update_order':
                            self.assertEqual(basket.items[1]['quantity'], 3)
                            self.assertEqual(basket.items[1]['item_variant_id'], str(variants[1].pk))
                    self.assertEqual(basket.items, [first])
        self.assertTrue(self.chain.called)

    def test_numbered_removal_uses_stable_entry_numbers_after_a_deletion(self):
        self.run_intent(self.intent('small latte'))
        self.run_intent(self.intent('large latte'))
        self.assertIn('Removed', self.run_intent(self.intent('Remove item 1 from my basket.', 'delete_entry')))
        self.assertEqual(self.basket.items[0]['item_number'], 2)
        self.assertIn('Removed', self.run_intent(self.intent('Remove item 2.', 'delete_entry')))
        self.assertEqual(self.basket.items, [])

    def test_unknown_entry_id_does_not_fall_back_to_another_row(self):
        self.run_intent(self.intent('small latte'))
        before = deepcopy(self.basket.to_dict())
        for action in ('remove', 'update'):
            with self.subTest(action=action):
                proposal = self.proposal(self.line(action=action, target=9, quantity=3))
                with self.assertRaises(ValueError):
                    execute_change(proposal, self.basket, self.tenant.api_key)
                self.assertEqual(self.basket.to_dict(), before)

    def test_model_number_cannot_override_a_different_named_item(self):
        self.run_intent(self.intent('small latte'))
        self.run_intent(self.intent('small cappuccino'))
        before = deepcopy(self.basket.to_dict())
        with self.assertRaises(ValueError):
            execute_change(self.proposal(self.line(action='remove', target=2)),
                           self.basket, self.tenant.api_key)
        self.assertEqual(self.basket.to_dict(), before)

    def test_model_reference_selects_one_of_identically_named_customized_rows(self):
        self.run_intent(self.intent('large latte'))
        self.run_intent(self.intent('large latte with oat milk'))
        first = deepcopy(self.basket.items[0])
        line = self.line(action='remove', target=2)
        line['item_id'] = None
        execute_change(self.proposal(line), self.basket, self.tenant.api_key)
        self.assertEqual(self.basket.items, [first])


    def test_ambiguous_name_does_not_authorize_model_target(self):
        self.run_intent(self.intent('small latte'))
        self.run_intent(self.intent('large latte'))
        before = deepcopy(self.basket.to_dict())
        line = self.line(action='remove', target=1)
        line['reference'] = {'by': 'name', 'value': 'latte'}
        proposal = self.proposal(line)
        with self.assertRaisesMessage(ValueError, 'Which entry'):
            execute_change(proposal, self.basket, self.tenant.api_key)
        self.assertEqual(self.basket.to_dict(), before)

    def test_model_multi_line_edit_is_atomic_when_a_number_is_invalid(self):
        self.run_intent(self.intent('small latte'))
        self.run_intent(self.intent('large latte'))
        before = deepcopy(self.basket.to_dict())
        proposal = self.proposal(self.line(action='remove', target=1), self.line(action='remove', target=9))
        with self.assertRaises(ValueError):
            execute_change(proposal, self.basket, self.tenant.api_key)
        self.assertEqual(self.basket.to_dict(), before)

    def test_numbered_and_named_edits_can_share_one_proposal(self):
        self.run_intent(self.intent('small latte'))
        self.run_intent(self.intent('small cappuccino'))
        update = self.line(action='update', item=self.cap, variant=self.cap_small, quantity=3, target=2)
        proposal = self.proposal(self.line(action='remove', target=1), update)
        execute_change(proposal, self.basket, self.tenant.api_key)
        self.assertEqual(len(self.basket.items), 1)
        self.assertEqual(self.basket.items[0]['item_number'], 2)
        self.assertEqual(self.basket.items[0]['name'], 'Cappuccino')
        self.assertEqual(self.basket.items[0]['quantity'], 3)

    def test_basket_size_update_requires_modifiers_even_for_legacy_or_empty_selections(self):
        self.milk_link.min_selections = 1
        self.milk_link.variant_ids = [str(self.large.pk)]
        self.milk_link.save()
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(self.small.pk), 1, [])
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                basket = Basket()
                basket.add_validated(selection, api_key=self.tenant.api_key)
                if legacy:
                    basket.items[0].pop('modifiers')
                before = deepcopy(basket.to_dict())
                self.assertFalse(basket.update_item(1, self.tenant.api_key, size='Large', quantity=2))
                self.assertEqual(basket.to_dict(), before)

    def test_basket_update_validates_current_catalog_and_keeps_failed_changes_atomic(self):
        self.basket.add_validated(validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(self.small.pk), 1, []), api_key=self.tenant.api_key)
        self.basket.items[0].pop('modifiers')
        self.large.price = Decimal('175')
        self.large.save()
        self.assertTrue(self.basket.update_item(1, self.tenant.api_key, size='Large', quantity='2'))
        self.assertEqual(self.basket.items[0]['item_variant_id'], str(self.large.pk))
        self.assertEqual(self.basket.items[0]['unit_price'], '175.00')
        self.assertEqual(self.basket.items[0]['quantity'], 2)
        before = deepcopy(self.basket.to_dict())
        for kwargs in ({'size': 'Unknown'}, {'quantity': 0}):
            self.assertFalse(self.basket.update_item(1, self.tenant.api_key, **kwargs))
            self.assertEqual(self.basket.to_dict(), before)
        self.assertFalse(self.basket.update_item(1, self.other.api_key, quantity=3))
        self.assertEqual(self.basket.to_dict(), before)
        self.milk_link.min_selections = 1
        self.milk_link.save()
        self.assertFalse(self.basket.update_item(1, self.tenant.api_key, quantity=3))
        self.assertEqual(self.basket.to_dict(), before)
        self.latte.is_available = False
        self.latte.save()
        self.assertFalse(self.basket.update_item(1, self.tenant.api_key, quantity=3))
        self.assertEqual(self.basket.to_dict(), before)

    def test_pronoun_removal_with_multiple_entries_remains_ambiguous(self):
        for variant in (self.small, self.large):
            self.basket.add_validated(validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(variant.pk), 1, []), api_key=self.tenant.api_key)
        before = deepcopy(self.basket.items)
        line = self.line(action='remove')
        line['reference'] = {'by': 'focus'}
        with self.assertRaisesMessage(ValueError, 'Which entry'):
            execute_change(self.proposal(line), self.basket, self.tenant.api_key)
        self.assertEqual(self.basket.items, before)

    def test_names_aliases_volumes_and_variant_ids_resolve_with_mocked_proposal(self):
        for text, variant in (("Small latte", self.small), ("12 oz latte", self.large),
                              ("200ml cafe latte", self.small)):
            self.run_intent(self.intent(text))
            self.assertEqual(self.basket.most_recent()["item_variant_id"], str(variant.pk))

    def test_production_parser_actions_use_real_cache_with_mocked_proposal_for_ordinary_edits(self):
        from chatbot_core.logic.cafe.item_parser import parse_order_text
        self.assertIn('variants', knowledge_cache.get_item_pricing_cache()[self.tenant.api_key]['Latte'])
        for text, action in [('2 large latte', 'add_to_basket'), ('make latte 3', 'update_order'),
                             ('change latte to small', 'update_order'), ('remove latte', 'delete_entry')]:
            parsed = parse_order_text(self.tenant.api_key, text, basket=self.basket.items, action=action)
            execute_change(parsed['proposal'], self.basket, self.tenant.api_key)
            if action == 'update_order':
                self.assertEqual(self.basket.items[0]['quantity'], 3)
        self.assertTrue(self.basket.is_empty())
        self.assertTrue(self.chain.called)

    def test_single_item_size_clarification_uses_mocked_proposal(self):
        obj = self.intent('two latte')
        self.assertIn('size', self.run_intent(obj))
        obj, reply = self.follow(obj, 'large')
        self.assertIn('Added 2', reply)
        self.assertEqual(self.basket.items[0]['quantity'], 2)

    def test_known_modifier_addition_uses_mocked_proposal(self):
        self.assertIn('Added 2', self.run_intent(self.intent('two large latte with oat milk and no sugar')))
        self.assertEqual({m['option_id'] for m in self.basket.items[0]['modifiers']},
                         {str(self.oat.pk), str(self.no_sugar.pk)})

    def test_distributed_customizations_are_three_distinct_lines_and_prices(self):
        proposal = self.proposal(self.line(modifiers=[self.choice(self.oat)]), self.line(),
                                 self.line(item=self.cap, variant=self.cap_small))
        self.respond(proposal)
        reply = self.run_intent(self.intent("Two large lattes, one with oat milk, and a small cappuccino"))
        self.assertIn("Oat milk", reply)
        self.assertEqual([x["quantity"] for x in self.basket.items], [1, 1, 1])
        self.assertEqual([x["unit_price"] for x in self.basket.items], ["150.00", "150.00", "110.00"])
        shown = self.basket.summary(currency="INR", exponent=2)
        self.assertEqual([x["line_total_minor"] for x in shown], [18000, 15000, 11000])
        self.assertTrue(all(x["currency"] == "INR" and x["exponent"] == 2 and "price" not in x for x in shown))
        restored = Basket.from_dict(json.loads(json.dumps(self.basket.to_dict())))
        self.assertEqual(restored.summary(currency="INR", exponent=2), shown)
        self.respond(self.proposal(self.line()))
        self.run_intent(self.intent("large latte"))
        self.assertEqual([x["quantity"] for x in self.basket.items], [1, 2, 1])

    def test_foreign_ids_and_invalid_combinations_are_atomic(self):
        foreign = MenuItem.objects.create(tenant=self.other, name="Other")
        invalid = [self.line(item=foreign), self.line(variant=self.cap_small),
                   self.line(modifiers=[self.choice(self.oat), self.choice(self.soy)]),
                   self.line(modifiers=[self.choice(self.shot, 4)]), self.line(quantity=0)]
        for line in invalid:
            with self.subTest(line=line):
                self.respond(self.proposal(self.line(), line))
                obj = self.intent("two different large custom lattes")
                self.run_intent(obj)
                self.assertFalse(obj.is_complete)
                self.assertEqual(self.basket.items, [])

    def test_required_and_variant_restricted_modifiers(self):
        self.milk_link.min_selections = 1
        self.milk_link.variant_ids = [str(self.large.pk)]
        self.milk_link.save()
        catalog = load_catalog(self.tenant.api_key)
        with self.assertRaisesRegex(ValueError, "Milk"):
            validate_selection(catalog, str(self.latte.pk), str(self.large.pk), 1, [])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_selection(catalog, str(self.latte.pk), str(self.small.pk), 1, [self.choice(self.oat)])
        row = validate_selection(catalog, str(self.latte.pk), str(self.large.pk), 2,
                                 [self.choice(self.oat), self.choice(self.shot, 2)])
        self.assertEqual(row["unit_price"], "150.00")

    def test_special_request_refers_to_store_even_with_matching_modifier(self):
        self.run_intent(self.intent("large latte"))
        self.respond(self.proposal(self.line(action="update", target=1, modifiers=[self.choice(self.no_sugar)])))
        before = deepcopy(self.basket.to_dict())
        self.assertIn("Please contact the store", self.run_intent(self.intent("No sugar", "special_requests")))
        self.assertEqual(self.basket.to_dict(), before)

    def test_ambiguous_reference_does_not_trust_model_target_and_number_resolves(self):
        self.run_intent(self.intent("small latte"))
        self.run_intent(self.intent("small cappuccino"))
        line = self.line(action="update", target=1)
        line["item_id"] = None
        line["reference"] = {"by": "focus"}
        self.respond(self.proposal(line))
        obj = self.intent("Make that large", "update_order")
        self.assertIn("Which entry", self.run_intent(obj))
        self.assertEqual(self.basket.items[0]["size"], "Small")
        obj, reply = self.follow(obj, "1")
        self.assertIn("Updated", reply)
        self.assertEqual(self.basket.items[0]["size"], "Large")

    def test_partial_order_clarification_survives_session_and_retains_all_lines(self):
        proposal = self.proposal(self.line(quantity=2), self.line(item=self.cap, variant=self.cap_small))
        proposal["lines"][0]["variant_id"] = None
        self.respond(proposal)
        obj = self.intent("two lattes and a small cappuccino")
        self.assertIn("size", self.run_intent(obj))
        self.assertFalse(self.basket.items)
        self.assertEqual(obj.basket_item["proposal"]["lines"][0]["quantity"], 2)
        self.assertIn("size", obj.get_followup_question())
        proposal["lines"][0]["variant_id"] = str(self.large.pk)
        self.respond(proposal)
        obj, reply = self.follow(obj, "large")
        self.assertTrue(obj.is_complete)
        self.assertEqual([x["quantity"] for x in self.basket.items], [2, 1])

    def test_two_clarifications_then_terminal_no_mutation(self):
        self.respond(self.proposal(unresolved=["Which size: Small or Large?"]))
        obj = self.intent("grande latte")
        self.assertIn("Which size", self.run_intent(obj))
        obj, reply = self.follow(obj, "unsure")
        self.assertFalse(obj.is_complete)
        obj, reply = self.follow(obj, "still unsure")
        self.assertTrue(obj.is_complete)
        self.assertIn("unchanged", reply)
        self.assertEqual(obj.basket_item, {})
        self.assertEqual(obj.get_followup_question(), "")
        self.assertEqual(self.basket.items, [])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.meta["aliases"], ["cafe latte"])

    def test_generic_unclear_reply_consumes_same_clarification_budget(self):
        obj = self.intent("latte")
        self.run_intent(obj)
        unclear = InsufficientInformationIntent(main_query="huh", sub_intent="insufficient_information", tenant=self.tenant.pk, chat_id="user")
        unclear.platform = "telegram"
        for _ in range(2):
            obj.process_followup(unclear, self.basket, {}, {}, [], self.tenant.api_key, self.customer)
        self.assertTrue(obj.is_complete)
        self.assertFalse(self.basket.items)

    def test_missing_action_cannot_mutate(self):
        obj = self.intent("surprise custom latte")
        self.assertIsNone(obj.resolved_action)
        reply, _ = obj.process_query(self.basket, {}, {}, [], self.tenant.api_key, self.customer)
        self.assertIn("item, size", reply)
        self.assertFalse(self.basket.items)

    def test_validation_rechecks_availability_after_interpretation(self):
        self.respond(self.proposal(self.line(modifiers=[self.choice(self.oat)])))
        self.oat.is_available = False
        self.oat.save()
        self.run_intent(self.intent("large oat latte"))
        self.assertFalse(self.basket.items)

    def test_checkout_persists_modifiers_variants_and_surcharges(self):
        from chatbot_core.logic.cafe.checkout import basket_total
        self.respond(self.proposal(self.line(quantity=2, modifiers=[self.choice(self.oat), self.choice(self.shot, 2)])))
        self.run_intent(self.intent("two large oat lattes with two extra shots each"))
        shown = self.basket.summary(currency='INR', exponent=2)[0]
        self.assertEqual(shown['unit_price_minor'], 21000)
        self.assertEqual(shown['line_total_minor'], 42000)
        self.assertNotIn('price', shown)
        self.assertEqual(basket_total(self.basket, self.tenant), 420)
        order = create_order(self.tenant, self.customer, self.basket, "user")
        self.assertEqual(str(order.total_amount), "420.00")
        row = order.items.get()
        self.assertEqual(row.variant_id, self.large.pk)
        self.assertEqual(row.unit_price, 150)
        self.assertEqual(row.total_price, 300)
        self.assertEqual(row.addons.count(), 2)
        self.assertEqual(sum(x.total_price for x in row.addons.all()), 120)
        self.assertEqual(row.total_price + sum(x.total_price for x in row.addons.all()), order.total_amount)

    def test_checkout_rejects_stale_modifier_or_price_without_partial_order(self):
        from chatbot_core.logic.cafe.checkout import basket_total
        self.respond(self.proposal(self.line(modifiers=[self.choice(self.oat)])))
        self.run_intent(self.intent("large oat latte"))
        self.oat.price = 40
        self.oat.save()
        with self.assertRaisesRegex(ValueError, 'price changed'):
            basket_total(self.basket, self.tenant)
        with self.assertRaisesRegex(ValueError, "price changed"):
            create_order(self.tenant, self.customer, self.basket, "user")
        self.assertFalse(Order.objects.exists())

    def test_merging_refreshes_modifier_prices(self):
        catalog = load_catalog(self.tenant.api_key)
        self.basket.add_validated(validate_selection(catalog, str(self.latte.pk), str(self.large.pk), 1, [self.choice(self.oat)]), api_key=self.tenant.api_key)
        self.oat.price = 40
        self.oat.save()
        self.basket.add_validated(validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk),
                                                    str(self.large.pk), 1, [self.choice(self.oat)]), api_key=self.tenant.api_key)
        self.assertEqual(self.basket.summary(currency='INR', exponent=2)[0]['line_total_minor'], 38000)
        self.assertEqual(create_order(self.tenant, self.customer, self.basket, 'user').total_amount, 380)

    def test_offsetting_modifier_price_changes_still_require_review(self):
        from chatbot_core.logic.cafe.checkout import basket_total
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk), str(self.large.pk), 1,
                                       [self.choice(self.oat), self.choice(self.shot)])
        self.basket.add_validated(selection, api_key=self.tenant.api_key)
        self.oat.price = 40
        self.oat.save()
        self.shot.price = 5
        self.shot.save()
        with self.assertRaisesRegex(ValueError, 'price changed'):
            basket_total(self.basket, self.tenant)
        with self.assertRaisesRegex(ValueError, 'price changed'):
            create_order(self.tenant, self.customer, self.basket, 'user')
        self.assertFalse(Order.objects.exists())

    def test_cart_explains_component_changes_without_repricing_or_consent(self):
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk),
                                       str(self.large.pk), 2, [self.choice(self.oat), self.choice(self.shot, 2)])
        self.basket.add_validated(selection, api_key=self.tenant.api_key)
        before = deepcopy(self.basket.to_dict())
        # Offsetting changes still require review even when the line total is unchanged.
        self.large.price = 160
        self.large.save()
        self.oat.price = 40
        self.oat.save()
        self.shot.price = 5
        self.shot.save()
        obj = self.intent('Show what changed before I agree.', 'check_order_cart')
        reply = obj.check_order_cart(self.basket, obj.main_query, api_key=self.tenant.api_key)
        self.assertIn('INR 150.00 → INR 160.00', reply)
        self.assertIn('INR 30.00 → INR 40.00', reply)
        self.assertIn('Extra shot (2 per item)', reply)
        self.assertIn('INR 15.00 → INR 5.00', reply)
        self.assertIn('explicitly update', reply)
        self.assertEqual(self.basket.to_dict(), before)
        self.assertFalse(Order.objects.exists())

    def test_cart_does_not_claim_current_prices_for_unavailable_or_stale_catalog(self):
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk),
                                       str(self.large.pk), 1, [])
        self.basket.add_validated(selection, api_key=self.tenant.api_key)
        before = deepcopy(self.basket.to_dict())
        obj = self.intent('Review prices', 'check_order_cart')
        self.large.is_available = False
        self.large.save()
        reply = obj.check_order_cart(self.basket, obj.main_query, api_key=self.tenant.api_key)
        self.assertIn('couldn’t verify', reply)
        with patch('commerce.menu_sync.assert_menu_fresh', side_effect=ValueError('Menu is stale')):
            reply = obj.check_order_cart(self.basket, obj.main_query, api_key=self.tenant.api_key)
        self.assertIn('Menu is stale', reply)
        self.assertNotIn('Current catalog price changes', reply)
        self.assertEqual(self.basket.to_dict(), before)

    def test_cart_for_placed_order_does_not_compare_new_catalog_prices(self):
        selection = validate_selection(load_catalog(self.tenant.api_key), str(self.latte.pk),
                                       str(self.large.pk), 1, [])
        self.basket.add_validated(selection, api_key=self.tenant.api_key)
        obj = self.intent('Show cart', 'check_order_cart')
        with patch('commerce.menu_sync.assert_menu_fresh') as fresh:
            reply, _ = obj.process_query(self.basket, {}, {'order': True}, [],
                                         self.tenant.api_key, self.customer)
        fresh.assert_not_called()
        self.assertIn('INR 150.00', reply)
        self.assertNotIn('price changes', reply)

    def test_configured_grande_alias_is_local_not_global(self):
        self.large.aliases = ["grande"]
        self.large.save()
        self.run_intent(self.intent("grande latte"))
        self.assertEqual(self.basket.items[0]["item_variant_id"], str(self.large.pk))

    def test_resolved_variant_id_is_applied(self):
        self.respond(self.proposal(self.line()))
        self.assertIn("Added", self.run_intent(self.intent("grande latte")))
        self.assertEqual(self.basket.items[0]["item_variant_id"], str(self.large.pk))

    def test_model_cannot_pick_an_item_id_for_ambiguous_that(self):
        self.run_intent(self.intent("small latte"))
        self.run_intent(self.intent("small cappuccino"))
        line = self.line(action="update", target=1)
        line["reference"] = {"by": "focus"}
        self.respond(self.proposal(line))
        self.assertIn("Which entry", self.run_intent(self.intent("make that large", "update_order")))
        self.assertEqual(self.basket.items[0]["size"], "Small")

    def test_catalog_miss_does_not_apply_partial_lines(self):
        proposal = self.proposal(self.line())
        proposal["catalog_miss"] = True
        self.respond(proposal)
        self.run_intent(self.intent("a large latte and something unavailable"))
        self.assertFalse(self.basket.items)

    def test_duplicate_modifier_options_and_bad_quantities_do_not_mutate(self):
        catalog = load_catalog(self.tenant.api_key)
        for modifiers in ([self.choice(self.oat), self.choice(self.oat)],
                          [self.choice(self.oat, True)], [self.choice(self.shot, -1)]):
            with self.assertRaises(ValueError):
                validate_selection(catalog, str(self.latte.pk), str(self.large.pk), 1, modifiers)
        for quantity in (True, 1.5, "1.5", -1, 0):
            with self.assertRaises(ValueError):
                validate_selection(catalog, str(self.latte.pk), str(self.large.pk), quantity, [])

    def test_changed_variant_revalidates_preserved_modifiers(self):
        self.milk_link.variant_ids = [str(self.large.pk)]
        self.milk_link.save()
        self.respond(self.proposal(self.line(modifiers=[self.choice(self.oat)])))
        self.run_intent(self.intent("large oat latte"))
        before = deepcopy(self.basket.items)
        line = self.line(action="update", variant=self.small, target=1)
        line["modifiers"] = None
        self.respond(self.proposal(line))
        self.run_intent(self.intent("make that small", "update_order"))
        self.assertEqual(self.basket.items, before)

    def test_shared_alias_is_ambiguous(self):
        self.cap.meta = {"aliases": ["cafe latte"]}
        self.cap.save()
        catalog = load_catalog(self.tenant.api_key)
        self.assertEqual(len(order_interpreter.exact_candidates("cafe latte", catalog)), 2)


class CandidateLookupTests(SimpleTestCase):
    def test_catalog_miss_broadens_once(self):
        catalog = {str(i): {"item_id": str(i), "name": f"Product {i}", "aliases": [], "variants": [], "modifier_groups": []} for i in range(80)}
        miss = OrderProposal(lines=[], unresolved=["missing"], catalog_miss=True)
        with patch.object(order_interpreter, "structured_chain") as chain:
            chain.return_value.invoke.return_value = miss
            order_interpreter.interpret_order("something new", catalog, [], {}, "", "add_to_basket")
        self.assertEqual(chain.return_value.invoke.call_count, 2)
        sizes = [len(json.loads(c.args[0]["input"])["catalog"]) for c in chain.return_value.invoke.call_args_list]
        self.assertEqual(sizes, [25, 80])


class OrderingLimitTests(TestCase):
    """Evaluation caps are test inputs. Each boundary is clear of the other caps."""

    def setUp(self):
        from tests.support.ordering import seed_evaluation_policy
        self.tenant = TenantInfo.objects.create(slug="limit-cafe", display_name="Limits")
        self.seed_policy = seed_evaluation_policy
        self.config = seed_evaluation_policy(self.tenant)
        self.item = MenuItem.objects.create(tenant=self.tenant, name="Line")
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size="Cup", price="1.00")
        knowledge_cache._ITEM_PRICING_CACHE.clear()
        knowledge_cache._ITEM_PRICING_CACHE.update(knowledge_cache.generate_all_menu_payload())
        self.addCleanup(knowledge_cache._ITEM_PRICING_CACHE.clear)

    def _selection(self, item, variant, quantity):
        return validate_selection(load_catalog(self.tenant.api_key), str(item.pk), str(variant.pk), quantity, [])

    def _add(self, basket, item, variant, quantity):
        basket.add_validated(self._selection(item, variant, quantity), api_key=self.tenant.api_key)

    def _reject(self, basket, item, variant, quantity):
        before = deepcopy(basket.to_dict())
        with self.assertRaises(ValueError):
            self._add(basket, item, variant, quantity)
        self.assertEqual(basket.to_dict(), before)

    def test_each_cap_accepts_the_exact_boundary_and_rejects_one_above(self):
        from commerce.models import Configuration
        for enabled in (False, True):
            with self.subTest(commerce_enabled=enabled):
                Configuration.objects.filter(pk=self.config.pk).update(enabled=enabled)
                self._assert_line_item_unit_line_and_subtotal_caps()

    def _assert_line_item_unit_line_and_subtotal_caps(self):
        line = Basket()
        self._add(line, self.item, self.variant, 20)
        self.assertEqual(line.items[0]["quantity"], 20)
        self._reject(line, self.item, self.variant, 1)

        item = MenuItem.objects.create(tenant=self.tenant, name="Split")
        small = MenuItemVariant.objects.create(menu_item=item, size="Small", price="1.00")
        large = MenuItemVariant.objects.create(menu_item=item, size="Large", price="1.00")
        split = Basket()
        self._add(split, item, small, 15)
        self._add(split, item, large, 15)
        self.assertEqual(sum(row["quantity"] for row in split.items), 30)
        self._reject(split, item, large, 1)

        units = Basket()
        priced = []
        for name in ("One", "Two", "Three", "Four"):
            product = MenuItem.objects.create(tenant=self.tenant, name=name)
            variant = MenuItemVariant.objects.create(menu_item=product, size="Cup", price="1.00")
            priced.append((product, variant))
        for product, variant in priced[:3]:
            self._add(units, product, variant, 20)
        self.assertEqual(sum(row["quantity"] for row in units.items), 60)
        self._reject(units, *priced[3], 1)

        lines = Basket()
        products = []
        for index in range(21):
            product = MenuItem.objects.create(tenant=self.tenant, name=f"Row {index}")
            variant = MenuItemVariant.objects.create(menu_item=product, size="Cup", price="1.00")
            products.append((product, variant))
        for product, variant in products[:20]:
            self._add(lines, product, variant, 1)
        self.assertEqual(len(lines.items), 20)
        self._reject(lines, *products[20], 1)

        cheap = MenuItem.objects.create(tenant=self.tenant, name="Boundary")
        exact = MenuItemVariant.objects.create(menu_item=cheap, size="Cup", price="5000.00")
        money = Basket()
        self._add(money, cheap, exact, 1)
        shown = money.summary(currency="INR", exponent=2)[0]
        self.assertEqual(shown["line_total_minor"], 500000)
        self.assertEqual(shown["currency"], "INR")
        self.assertNotIn("price", shown)
        over = MenuItemVariant.objects.create(menu_item=cheap, size="Bowl", price="5000.01")
        self._reject(Basket(), cheap, over, 1)

    def test_unreadable_policy_does_not_enable_ordering(self):
        from commerce.models import Configuration
        from chatbot_core.logic.cafe.ordering_limits import load_policy
        Configuration.objects.filter(pk=self.config.pk).update(policy={"schema_version": 1, "currency": "INR", "exponent": 2})
        self.assertIsNone(load_policy(tenant_id=self.tenant.pk))
        basket = Basket()
        self.assertFalse(basket.add_item("Line", self.tenant.api_key, "Cup", 1))
        self.assertEqual(basket.items, [])

    def test_accumulated_additions_stop_at_the_line_cap(self):
        basket = Basket()
        for _ in range(20):
            self.assertTrue(basket.add_item("Line", self.tenant.api_key, "Cup", 1))
        before = deepcopy(basket.to_dict())
        self.assertFalse(basket.add_item("Line", self.tenant.api_key, "Cup", 1))
        self.assertEqual(basket.to_dict(), before)
        self.assertIn("20", basket.rejection)

    def test_malformed_quantities_do_not_clamp_or_mutate(self):
        basket = Basket()
        self.assertTrue(basket.add_item("Line", self.tenant.api_key, "Cup", 5))
        before = deepcopy(basket.to_dict())
        for quantity in (True, False, 1.5, "1.5", 0, -1, "1e2", " 1.0 ", "9" * 100, 10 ** 20):
            with self.subTest(quantity=quantity):
                self.assertIsNone(positive_integer(quantity))
                self.assertFalse(basket.add_item("Line", self.tenant.api_key, "Cup", quantity))
                self.assertEqual(basket.to_dict(), before)
        with self.assertRaises(ValueError):
            validate_selection(load_catalog(self.tenant.api_key), str(self.item.pk), str(self.variant.pk), 10 ** 9, [])
        self.assertEqual(basket.items[0]["quantity"], 5)

    def test_over_limit_proposal_is_atomic(self):
        proposal = {"lines": [
            {"action": "add", "item_id": str(self.item.pk), "variant_id": str(self.variant.pk),
             "quantity": 10, "modifiers": [], "target_number": None, "unresolved": []},
            {"action": "add", "item_id": str(self.item.pk), "variant_id": str(self.variant.pk),
             "quantity": 11, "modifiers": [], "target_number": None, "unresolved": []},
        ], "unresolved": [], "catalog_miss": False}
        basket = Basket()
        with self.assertRaises(ValueError):
            execute_change(proposal, basket, self.tenant.api_key)
        self.assertEqual(basket.items, [])

    def test_tightened_policy_allows_reductions_and_blocks_growth(self):
        from commerce.policy import evaluation_policy
        loose = evaluation_policy()["ordering_limits"]
        loose["max_line_quantity"] = 25
        self.config.policy = evaluation_policy(ordering_limits=loose)
        self.config.save()
        basket = Basket()
        self._add(basket, self.item, self.variant, 25)
        tight = dict(loose)
        tight["max_line_quantity"] = 20
        self.config.policy = evaluation_policy(ordering_limits=tight)
        self.config.save()
        self._reject(basket, self.item, self.variant, 1)
        self.assertTrue(basket.update_item(1, self.tenant.api_key, quantity=24))
        self.assertEqual(basket.items[0]["quantity"], 24)
        from chatbot_core.logic.cafe.ordering_limits import assert_checkout
        with self.assertRaises(ValueError):
            assert_checkout(basket.items, self.tenant, 2400)
        self.assertTrue(basket.update_item(1, self.tenant.api_key, quantity=20))
        assert_checkout(basket.items, self.tenant, 2000)
        self.assertTrue(basket.remove_item(1))
        self.assertEqual(basket.items, [])
