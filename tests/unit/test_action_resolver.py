from copy import deepcopy
from unittest import TestCase

from chatbot_core.llm.schemas import ActionProposal, EntityReference
from chatbot_core.logic.action_resolver import (
    NeedsClarification, TerminalRejection, competing_catalog_items, resolve_action, resolve_reference,
)


ICE_CREAMS = [{'id': 'overload', 'name': 'Chocolate overload ice cream', 'aliases': []},
              {'id': 'just', 'name': 'Just chocolate ice cream', 'aliases': []},
              {'id': 'cherry', 'name': 'Cherry and chocolate ice cream', 'aliases': []},
              {'id': 'vanilla', 'name': 'Vanilla ice cream', 'aliases': []}]


def add_line(item_id):
    return ActionProposal(kind='CHANGE_BASKET', basket={
        'lines': [{'action': 'add', 'item_id': item_id, 'variant_id': 'v', 'quantity': 1,
                   'modifiers': [], 'target_number': None, 'reference': None, 'unresolved': []}],
        'unresolved': [], 'catalog_miss': False})


class CatalogAmbiguityTests(TestCase):
    def test_partial_name_shared_by_several_products_lists_them(self):
        with self.assertRaises(NeedsClarification) as caught:
            resolve_action(add_line('just'), basket=[], catalog=ICE_CREAMS, text='add a chocolate ice cream')
        message = str(caught.exception)
        for name in ('Chocolate overload ice cream', 'Just chocolate ice cream', 'Cherry and chocolate ice cream'):
            self.assertIn(name, message)
        self.assertNotIn('Vanilla', message)

    def test_full_name_or_unique_partial_name_is_accepted(self):
        for text, item_id in [('Cherry and Chocolate ice cream please', 'cherry'),
                              ('add chocolate ice cream, the cherry one', 'cherry'),
                              ('just chocolate', 'just'), ('a vanilla ice cream', 'vanilla')]:
            with self.subTest(text=text):
                resolved = resolve_action(add_line(item_id), basket=[], catalog=ICE_CREAMS, text=text)
                self.assertEqual(resolved.proposal.basket.lines[0].item_id, item_id)

    def test_no_lexical_overlap_leaves_the_proposal_undisputed(self):
        self.assertEqual(competing_catalog_items('the one you recommended', 'just', ICE_CREAMS), [])
        self.assertEqual(competing_catalog_items('चॉकलेट आइसक्रीम', 'just', ICE_CREAMS), [])

    def test_exact_name_competes_only_with_shared_alias_or_containing_name(self):
        drinks = [{'id': 'latte', 'name': 'Latte', 'aliases': []},
                  {'id': 'iced', 'name': 'Iced Latte', 'aliases': []},
                  {'id': 'choc', 'name': 'Chocolate', 'aliases': []}]
        self.assertEqual(competing_catalog_items('a latte with chocolate syrup', 'latte', drinks), [])
        self.assertEqual([row['id'] for row in competing_catalog_items('an iced latte', 'latte', drinks)], ['iced'])
        drinks[2]['aliases'] = ['latte']
        self.assertEqual([row['id'] for row in competing_catalog_items('a latte', 'latte', drinks)], ['choc'])

    def test_head_words_are_not_disputed_by_products_listing_them_as_components(self):
        gelato = [{'id': 'fig', 'name': 'Fig Orange Ice Cream', 'aliases': []},
                  {'id': 'dates', 'name': 'Dates with Fig & Orange', 'aliases': []},
                  {'id': 'vanilla', 'name': 'Old Fashion Vanilla Ice Cream', 'aliases': []}]
        self.assertEqual(competing_catalog_items('Actually change the vanilla to fig orange.', 'fig', gelato), [])
        # Naming a product by its components alone still asks.
        self.assertEqual([row['id'] for row in competing_catalog_items('fig orange', 'dates', gelato)], ['fig'])
        # A rival whose head carries the words keeps the question open.
        gelato.append({'id': 'sorbet', 'name': 'Orange Sorbet with Fig', 'aliases': []})
        self.assertEqual([row['id'] for row in competing_catalog_items('fig orange', 'fig', gelato)],
                         ['dates', 'sorbet'])

    def test_counts_and_package_notation_are_compared_after_normalization(self):
        brownies = [{'id': 'fudgy', 'name': 'Fudgy Chocolate Brownie (2pcs)', 'aliases': []},
                    {'id': 'cheesecake', 'name': 'Brownie Cheesecake', 'aliases': []},
                    {'id': 'sundae', 'name': 'Brownie With Vanilla Ice Cream & Fudge Sauce', 'aliases': []}]
        for text in ('The brownie, two pieces, and send payment.', 'Two pieces of the brownie',
                     'The brownie (2pcs)', 'The brownie, 2 pieces'):
            with self.subTest(text=text):
                self.assertEqual(competing_catalog_items(text, 'fudgy', brownies), [])
        self.assertEqual([row['id'] for row in competing_catalog_items('a brownie', 'fudgy', brownies)],
                         ['cheesecake', 'sundae'])

    def test_order_quantities_and_other_products_package_counts_do_not_resolve_ambiguity(self):
        brownies = [{'id': 'fudgy', 'name': 'Fudgy Chocolate Brownie (2pcs)', 'aliases': []},
                    {'id': 'cheesecake', 'name': 'Brownie Cheesecake', 'aliases': []},
                    {'id': 'sundae', 'name': 'Brownie With Vanilla Ice Cream & Fudge Sauce', 'aliases': []}]
        for text in ('Add two of the brownie', 'Add 2 of the brownie',
                     'Add the brownie and two coffees', 'Add the brownie and two pieces of cake',
                     'Add two pieces of cake and the brownie'):
            with self.subTest(text=text):
                with self.assertRaises(NeedsClarification) as caught:
                    resolve_action(add_line('fudgy'), basket=[], catalog=brownies, text=text)
                for row in brownies:
                    self.assertIn(row['name'], str(caught.exception))

    def test_copying_a_basket_row_and_missing_catalog_skip_the_check(self):
        copy = add_line('just')
        copy.basket.lines[0].reference = EntityReference(by='focus')
        basket = [{'item_number': 1, 'item_id': 'just', 'item_variant_id': 'v', 'name': 'Just chocolate ice cream'}]
        resolve_action(copy, basket=basket, focus=1, catalog=ICE_CREAMS, text='add another chocolate ice cream')
        resolve_action(add_line('just'), basket=[], text='add a chocolate ice cream')


class ActionResolverTests(TestCase):
    def test_preservation_only_requires_valid_unambiguous_references(self):
        basket = [{'item_number': 7, 'item_id': 'coffee', 'name': 'Coffee', 'quantity': 2}]
        action = ActionProposal(kind='CHANGE_BASKET', basket={
            'preserved_references': [{'by': 'name', 'value': 'Coffee'}],
            'lines': [], 'unresolved': [], 'catalog_miss': False})
        before = deepcopy(basket)
        resolved = resolve_action(action, basket=basket)
        self.assertEqual(resolved.basket_targets, ())
        self.assertEqual(resolved.preserved_basket_targets, (7,))
        self.assertEqual(basket, before)
        with self.assertRaises(NeedsClarification):
            resolve_action(action, basket=basket + [{**basket[0], 'item_number': 8}])
        with self.assertRaises(TerminalRejection):
            resolve_action(action, basket=[])
        for fields in ({'preserved_references': []}, {'unresolved': ['Which coffee?']},
                       {'catalog_miss': True}):
            with self.subTest(fields=fields), self.assertRaises(NeedsClarification):
                invalid = action.model_copy(deep=True)
                invalid.basket = invalid.basket.model_copy(update=fields)
                resolve_action(invalid, basket=basket)

    def test_preservation_rejects_conflicting_changes_and_ambiguous_exceptions(self):
        basket = [
            {'item_number': 7, 'item_id': 'coffee', 'name': 'Coffee', 'quantity': 2},
            {'item_number': 12, 'item_id': 'tea', 'name': 'Green Tea', 'quantity': 3},
        ]
        before = deepcopy(basket)
        action = add_line(None)
        line = action.basket.lines[0]
        line.action = 'remove'
        line.quantity = None
        line.reference = EntityReference(by='name', value='Coffee')
        action.basket.preserved_references = [EntityReference(by='name', value='Tea')]
        self.assertEqual(resolve_action(action, basket=basket).basket_targets, (7,))
        for operation in ('remove', 'update', 'replace'):
            with self.subTest(operation=operation), self.assertRaises(NeedsClarification):
                conflict = line.model_copy(deep=True)
                conflict.action = operation
                conflict.reference = EntityReference(by='id', value='12')
                action.basket.lines = [line, conflict]
                resolve_action(action, basket=basket)
        action.basket.lines = [line]
        duplicate = {'item_number': 18, 'item_id': 'black-tea', 'name': 'Black Tea', 'quantity': 1}
        with self.assertRaises(NeedsClarification):
            resolve_action(action, basket=basket + [duplicate])
        action.basket.preserved_references = [EntityReference(by='id', value='99')]
        with self.assertRaises(TerminalRejection):
            resolve_action(action, basket=basket)
        self.assertEqual(basket, before)

    def test_partial_names_are_unambiguous_across_different_catalogs(self):
        for names, target in [(['Classic Lamington', 'Tiramisu'], 'lamington'),
                              (['Linen Notebook', 'Fountain Pen'], 'notebook')]:
            entities = [{'id': str(i), 'name': name} for i, name in enumerate(names)]
            self.assertEqual(resolve_reference(EntityReference(by='name', value=target), entities), '0')

    def test_ambiguity_unknown_ids_and_stale_focus_never_pick_first_entry(self):
        entities = [{'id': '7', 'name': 'Chocolate cake'}, {'id': '12', 'name': 'Chocolate bar'}]
        for reference, focus in [(EntityReference(by='name', value='chocolate'), None),
                                 (EntityReference(by='id', value='1'), None),
                                 (EntityReference(by='focus'), None),
                                 (EntityReference(by='focus'), '99')]:
            with self.subTest(reference=reference, focus=focus), self.assertRaises(NeedsClarification):
                resolve_reference(reference, entities, focus=focus)
        self.assertEqual(resolve_reference(EntityReference(by='focus'), entities, focus='12'), '12')

    def test_basket_binding_ignores_guessed_number_and_does_not_mutate_inputs(self):
        proposal = ActionProposal(kind='CHANGE_BASKET', basket={
            'lines': [{'action': 'remove', 'item_id': None, 'variant_id': None,
                       'quantity': None, 'modifiers': None, 'target_number': 1,
                       'reference': {'by': 'name', 'value': 'notebook'}, 'unresolved': []}],
            'unresolved': [], 'catalog_miss': False})
        basket = [{'item_number': 8, 'item_id': 'book', 'name': 'Linen Notebook', 'quantity': 1}]
        before = deepcopy(basket)
        resolved = resolve_action(proposal, basket=basket)
        self.assertEqual(resolved.basket_targets, (8,))
        self.assertEqual(resolved.proposal.basket.lines[0].item_id, 'book')
        self.assertEqual(proposal.basket.lines[0].target_number, 1)
        self.assertEqual(basket, before)

    def test_payment_continuation_cannot_become_confirmation(self):
        action = resolve_action(ActionProposal(kind='CONTINUE_CHECKOUT'), basket=[],
                                checkout={'awaiting': 'payment_method'})
        self.assertIsNone(action.quote_fingerprint)
        for checkout in ({}, {'quote': None}, {'quote': {'fingerprint': 'q'}, 'awaiting': 'address'}):
            with self.subTest(checkout=checkout), self.assertRaises(NeedsClarification):
                resolve_action(ActionProposal(kind='CONFIRM_ORDER'), basket=[], checkout=checkout)
        action = resolve_action(ActionProposal(kind='CONFIRM_ORDER'), basket=[],
                                checkout={'quote': {'fingerprint': 'q'}})
        self.assertEqual(action.quote_fingerprint, 'q')

    def test_address_selection_uses_scoped_entities_and_rejects_duplicate_labels(self):
        action = ActionProposal(kind='SELECT_ADDRESS', reference=EntityReference(by='name', value='Home'))
        addresses = [{'id': 'home', 'name': 'Home'}, {'id': 'office', 'name': 'Office'}]
        self.assertEqual(resolve_action(action, basket=[], addresses=addresses).target_id, 'home')
        addresses.append({'id': 'second-home', 'name': 'Home'})
        with self.assertRaises(NeedsClarification):
            resolve_action(action, basket=[], addresses=addresses)
        with self.assertRaises(TerminalRejection):
            resolve_action(action, basket=[], addresses=addresses, placed=True)

    def test_mixed_action_parameters_are_rejected(self):
        with self.assertRaises(NeedsClarification):
            resolve_action(ActionProposal(kind='CONTINUE_CHECKOUT', value='cash'), basket=[])
