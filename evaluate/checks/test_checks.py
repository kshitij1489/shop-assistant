from copy import deepcopy
import unittest

from evaluate.checks.engine import DeterministicEvaluator, detected_claims
from evaluate.checks.models import CheckSpec
from evaluate.reports.example import synthetic_run


class DeterministicTests(unittest.TestCase):
    def test_address_presentation_differences_do_not_hide_changed_identity(self):
        from evaluate.checks.engine import address_matches
        expected = {'id': 'A', 'components': {'house_or_flat': 'Flat 8',
                    'sector_or_phase': 'DLF Phase 2', 'city': 'Gurugram', 'postal_code': '122002'}}
        actual = {'id': 'A', 'components': {'house_or_flat': 'flat 8', 'street_or_locality': 'DLF',
                  'sector_or_phase': 'phase 2', 'city': 'gurugram', 'postal_code': '122002'}}
        self.assertTrue(address_matches(actual, expected))
        for field, value in (('house_or_flat', 'flat 9'), ('postal_code', '122003')):
            changed = deepcopy(actual)
            changed['components'][field] = value
            self.assertFalse(address_matches(changed, expected))
        self.assertFalse(address_matches({**actual, 'id': 'a'}, expected))

    def test_free_form_street_matches_legacy_without_losing_tower_or_flat(self):
        from evaluate.checks.engine import address_matches
        expected = {'components': {'house_or_flat': 'Flat 11', 'building_or_block': 'Tower 4',
                                  'sector_or_phase': 'Sector 56', 'postal_code': '122011'}}
        actual = {'components': {'street_address': 'flat 11, Tower 4, sector 56', 'postal_code': '122011'}}
        self.assertTrue(address_matches(actual, expected))
        for text in ('Flat 11, Sector 56', 'Flat 4, Tower 11, Sector 56', 'Flat 11, Tower 5, Sector 56'):
            self.assertFalse(address_matches({'components': {**actual['components'], 'street_address': text}}, expected))

    def test_reviewed_street_policy_accepts_user_confirmed_wording(self):
        postal = {'city': 'Gurugram', 'state': 'Haryana', 'country': 'India', 'postal_code': '122011'}
        expected = {'records': [{'components': postal}], 'street_policy': 'free_form'}
        for street in ('flat 11, torre 4 cerca de sector 56', 'Flat 11, Tower 4, Sector 56',
                       'Blue gate behind the market'):
            with self.subTest(street=street):
                state = {'addresses': [{'components': {**postal, 'street_address': street}}]}
                self.assertEqual(self.check('addresses', expected, state=state).outcome, 'PASS')

    def test_reviewed_street_policy_still_requires_basic_fields_and_identity(self):
        postal = {'city': 'Gurugram', 'state': 'Haryana', 'country': 'India', 'postal_code': '122011'}
        row = {'id': 'A', 'customer_id': 'customer', 'tenant_id': 'tenant',
               'components': {**postal, 'street_address': 'Blue gate'}}
        expected = {'records': [{**row, 'components': postal}], 'street_policy': 'free_form'}
        for key, value in (('street_address', ''), ('street_address', 'unknown'), ('street_address', False),
                           ('city', '123'), ('state', ''), ('country', 'unknown'), ('postal_code', '12201'),
                           ('postal_code', '122012'), ('postal_code', '१२२०११')):
            with self.subTest(key=key, value=value):
                actual = {**row, 'components': {**row['components'], key: value}}
                self.assertEqual(self.check('addresses', expected, state={'addresses': [actual]}).outcome, 'FAIL')
        for key in ('city', 'state', 'country', 'postal_code'):
            actual = deepcopy(row)
            del actual['components'][key]
            self.assertEqual(self.check('addresses', expected, state={'addresses': [actual]}).outcome, 'BLOCKED')
        for key in ('id', 'customer_id', 'tenant_id'):
            self.assertEqual(self.check('addresses', expected, state={'addresses': [{**row, key: 'wrong'}]}).outcome, 'FAIL')
        for rows in ([], [row, row]):
            self.assertEqual(self.check('addresses', expected, state={'addresses': rows}).outcome, 'FAIL')

    def test_free_form_addresses_with_shared_postal_fields_match_distinct_rows(self):
        postal = {'city': 'Gurugram', 'state': 'Haryana', 'country': 'India', 'postal_code': '122011'}
        state = {'addresses': [{'id': 'A', 'components': {**postal, 'street_address': 'Home'}},
                               {'id': 'B', 'components': {**postal, 'street_address': 'Work'}}]}
        expected = {'records': [{'components': postal}, {'id': 'A', 'components': postal}],
                    'street_policy': 'free_form'}
        self.assertEqual(self.check('addresses', expected, state=state).outcome, 'PASS')

    def test_s153_and_s163_reviewed_checks_keep_confirmation_separate_from_street(self):
        from types import SimpleNamespace
        from evaluate.checks.reviewed import address_checks
        from evaluate.checks.engine import predicate
        for number, pin, street in ((153, '122002', 'tower C, cyber hub ke paas, 22 DLF phase 2'),
                                    (163, '122011', 'flat 11, torre 4 cerca de sector 56')):
            scenario = SimpleNamespace(scenario_id=f'sessions:s{number}',
                                       turns=[SimpleNamespace(original_turn_index=6)])
            saved, confirmation = address_checks(scenario, number)
            self.assertNotIn('street_address', saved.expected['records'][0]['components'])
            state = {'addresses': [{'components': {'street_address': street, 'city': 'Gurugram',
                     'state': 'Haryana', 'country': 'India', 'postal_code': pin}}],
                     'address_selection': {'confirmed': False}}
            self.assertTrue(predicate(saved, state, None))
            self.assertFalse(predicate(confirmation, state, None))
            state['address_selection']['confirmed'] = True
            self.assertTrue(predicate(confirmation, state, None))

    def setUp(self):
        bundle = synthetic_run()
        self.turn = bundle.turns[0]
        self.before, self.after = bundle.snapshots[:2]
        self.events = bundle.events[:1]
        self.engine = DeterministicEvaluator()

    def check(self, kind, expected, *, state=None, before=None, **kwargs):
        spec = CheckSpec(check_id="test", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind=kind, criterion="Synthetic assertion", expected=expected, **kwargs)
        snapshots = [self.before.model_copy(update={"state": before}) if before is not None else self.before,
                     self.after.model_copy(update={"state": state}) if state is not None else self.after]
        return self.engine.evaluate(spec, self.turn, snapshots, self.events)

    def test_basket_matches_reviewed_catalog_names(self):
        expected = {"items": [{"name": "Pistachio Ice Cream", "quantity": 2}]}
        state = {"basket": {"items": [{
            "name": "Pistachio Ice Cream", "item_id": "generated", "variant_id": "generated",
            "quantity": 2, "unit_price_minor": 100,
        }]}}
        self.assertEqual(self.check("basket", expected, state=state).outcome, "PASS")
        state["basket"]["items"][0]["quantity"] = 1
        self.assertEqual(self.check("basket", expected, state=state).outcome, "FAIL")

    def test_pending_task_spec_requires_path_selection_and_typed_retention(self):
        from pydantic import ValidationError
        for path, expected in (('', {'match': {'intent_type': 'placing_order'}}),
                               ('chat/ongoing_query_queue', {}),
                               ('chat/ongoing_query_queue', {'match': {}}),
                               ('chat/ongoing_query_queue', {'match': [], 'retain': True}),
                               ('chat/ongoing_query_queue', {'match': {'intent_type': 'placing_order'}, 'retain': 'true'})):
            with self.subTest(path=path, expected=expected), self.assertRaises(ValidationError):
                CheckSpec(check_id='pending', scenario_id='sessions:test', original_turn_index=0,
                          kind='tasks_pending', path=path, criterion='Retain a choice.', expected=expected)

    def test_task_resolution_requires_scoped_unambiguous_prior_evidence(self):
        spec = CheckSpec(check_id='resolve', scenario_id=self.turn.scenario_id, original_turn_index=6,
            kind='tasks_complete', path='chat/queue', criterion='Resolve the earlier choice.',
            expected={'match': {'intent_type': 'placing_order'}, 'pending_before_turn': 4})
        turn = self.turn.model_copy(update={'original_turn_index': 6})
        pending = {'chat': {'queue': [{'query_id': 1, 'intent_type': 'placing_order',
            'sub_intent': 'add_to_basket', 'is_complete': False, 'follow_up_question': ['Which?']}]}}
        before = self.before.model_copy(update={'original_turn_index': 6, 'state': pending})
        after = self.after.model_copy(update={'original_turn_index': 6, 'state': {'chat': {'queue': []}}})
        prior = self.after.model_copy(update={'original_turn_index': 4, 'snapshot_id': 'prior',
            'request_id': 'prior-request', 'state': deepcopy(pending),
            'captured_at': '2026-09-28T06:29:59+00:00'})
        result = self.engine.evaluate(spec, turn, [before, after, prior])
        self.assertEqual(result.outcome, 'PASS')
        self.assertIn('prior', result.evidence_ids)
        for field, value in (('run_id', 'other-run'), ('scenario_id', 'qa:other'),
                             ('scenario_instance_id', 'other-instance'), ('attempt', 2),
                             ('original_turn_index', 2), ('phase', 'before')):
            with self.subTest(field=field):
                wrong = prior.model_copy(update={field: value})
                self.assertEqual(self.engine.evaluate(spec, turn, [before, after, wrong]).outcome, 'BLOCKED')
        conflict = prior.model_copy(update={'snapshot_id': 'conflict', 'state': after.state})
        self.assertEqual(self.engine.evaluate(spec, turn, [before, after, prior, conflict]).outcome,
                         'NEEDS_REVIEW')
        other_request = prior.model_copy(update={'snapshot_id': 'other', 'request_id': 'other-request'})
        self.assertEqual(self.engine.evaluate(spec, turn, [before, after, prior, other_request]).outcome,
                         'NEEDS_REVIEW')
        late = prior.model_copy(update={'captured_at': after.captured_at})
        self.assertEqual(self.engine.evaluate(spec, turn, [before, after, late]).outcome, 'NEEDS_REVIEW')

    def test_task_resolution_spec_requires_an_earlier_turn_and_match(self):
        from pydantic import ValidationError
        for index in (-1, 6, 8, True, '4'):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                CheckSpec(check_id='resolve', scenario_id='sessions:test', original_turn_index=6,
                    kind='tasks_complete', path='chat/queue', criterion='Resolve a choice.',
                    expected={'match': {'intent_type': 'placing_order'}, 'pending_before_turn': index})

    def test_setup_fixture_does_not_assert_pre_turn_basket_after_user_mutation(self):
        from evaluate.checks.plan_checks import generate_check_specs
        from evaluate.tests.fakes import make_action, make_scenario
        action = make_action("seed-basket", {"kind": "seed_fixture", "fixture_id": "vanilla-lamington", "fixture_hash": "b" * 64})
        scenario = make_scenario("unreviewed", ["remove vanilla"], actions=[action])
        scenario = scenario.model_copy(update={"setup_profiles": ["catalog_sandbox"]})
        specs = generate_check_specs([scenario])
        self.assertFalse(any(spec.kind == "basket" for spec in specs))
        self.assertTrue(any(spec.kind == "ownership" for spec in specs))

    def test_basket_exact_quantities_and_no_extra_lines(self):
        expected = {"items": [{"item_id": "soup", "variant_id": "standard", "quantity": 1}]}
        self.assertEqual(self.check("basket", expected).outcome, "PASS")
        for quantity in (0, 2):
            state = deepcopy(self.after.state)
            state["basket"]["items"][0]["quantity"] = quantity
            self.assertEqual(self.check("basket", expected, state=state).outcome, "FAIL")
        state["basket"]["items"] *= 2
        self.assertEqual(self.check("basket", expected, state=state).outcome, "FAIL")

    def test_totals_minor_units_and_arithmetic(self):
        self.assertEqual(self.check("totals", {"total_minor": 15000, "currency": "INR"}).outcome, "PASS")
        for key, value, outcome in (("total_minor", 15001, "FAIL"), ("fee_minor", -1, "FAIL"),
                                    ("total_minor", 15000.0, "BLOCKED"), ("currency", "USD", "FAIL")):
            state = deepcopy(self.after.state)
            state["basket"][key] = value
            self.assertEqual(self.check("totals", {"currency": "INR"}, state=state).outcome, outcome)

    def test_ownership_both_tenant_and_customer(self):
        expected = {"tenant_id": "tenant-a", "customer_id": "customer-a"}
        state = {"orders": [{"id": "o1", **expected}]}
        self.assertEqual(self.check("ownership", expected, state=state, path="orders").outcome, "PASS")
        for field in expected:
            bad = deepcopy(state)
            bad["orders"][0][field] = "other"
            self.assertEqual(self.check("ownership", expected, state=bad, path="orders").outcome, "FAIL")
        self.assertEqual(self.check("ownership", expected, state={"orders": [{}]}, path="orders").outcome, "BLOCKED")
        self.assertEqual(self.check("ownership", {}, state=state, path="orders").outcome, "BLOCKED")
        self.assertEqual(self.check("ownership", {}, state={"orders": [{"id": "o1"}]}, path="orders").outcome, "BLOCKED")

    def test_saved_addresses_require_exact_record_and_owner(self):
        address = {"id": "a1", "customer_id": "c1", "postal_code": "122102"}
        self.assertEqual(self.check("addresses", {"records": [address]}, state={"addresses": [address]}).outcome, "PASS")
        self.assertEqual(self.check("addresses", {"records": [address]}, state={"addresses": []}).outcome, "FAIL")
        self.assertEqual(self.check("addresses", {"records": [address]}, state={"addresses": [address, address]}).outcome, "FAIL")

    def test_address_normalization_preserves_missing_evidence(self):
        expected = {'id': 'a1', 'components': {'city': 'Gurugram', 'postal_code': '122102'}}
        actual = {'id': 'a1', 'components': {'city': 'gurugram'}}
        self.assertEqual(self.check('addresses', {'records': [expected]},
                                   state={'addresses': [actual]}).outcome, 'BLOCKED')

    def test_quote_can_be_retired_or_replaced_but_never_reused(self):
        before = {"quote": {"id": "q1", "valid": True}}
        state = {"quote": {"id": "q1", "valid": False}}
        self.assertEqual(self.check("quote_invalidated", {}, state=state, before=before).outcome, "PASS")
        state["quote"]["valid"] = True
        self.assertEqual(self.check("quote_invalidated", {}, state=state, before=before).outcome, "FAIL")
        state["quote"] = {"id": "q2", "valid": True}
        self.assertEqual(self.check("quote_invalidated", {}, state=state, before=before).outcome, "PASS")
        state["quote"]["valid"] = False
        self.assertEqual(self.check("quote_invalidated", {}, state=state, before=before).outcome, "FAIL")
        before["quote"]["valid"] = False
        state["quote"]["id"] = "q1"
        self.assertEqual(self.check("quote_invalidated", {}, state=state, before=before).outcome, "FAIL")

    def test_order_count_and_delta(self):
        state = {"orders": [{"id": "o1"}, {"id": "o2"}]}
        self.assertEqual(self.check("order_count", {"count": 2}, state=state).outcome, "PASS")
        self.assertEqual(self.check("order_count", {"delta": 1}, state=state, before={"orders": [{"id": "o1"}]}).outcome, "PASS")
        self.assertEqual(self.check("order_count", {"delta": 1}, state=state).outcome, "FAIL")

    def test_quote_retirement_needs_no_money_but_replacement_requires_complete_totals(self):
        expected = {'replacement_totals': {'currency': 'INR'}}
        before = {'quote': {'id': 'old', 'valid': True}}
        state = {'quote': {'id': 'old', 'valid': False}}
        self.assertEqual(self.check('quote_invalidated', expected, state=state, before=before).outcome, 'PASS')
        state['quote'] = {'id': 'new', 'valid': True}
        self.assertEqual(self.check('quote_invalidated', expected, state=state, before=before).outcome, 'BLOCKED')
        state['basket'] = {'items': [{'quantity': 2, 'unit_price_minor': 47000}],
                           'subtotal_minor': 94000, 'fee_minor': 0, 'tax_minor': 0,
                           'discount_minor': 0, 'total_minor': 94000, 'currency': 'INR'}
        self.assertEqual(self.check('quote_invalidated', expected, state=state, before=before).outcome, 'PASS')
        state['basket']['total_minor'] = 92000
        self.assertEqual(self.check('quote_invalidated', expected, state=state, before=before).outcome, 'FAIL')
        del state['basket']['fee_minor']
        self.assertEqual(self.check('quote_invalidated', expected, state=state, before=before).outcome, 'BLOCKED')
        for quote in ({'id': 'old', 'valid': True}, {'id': 'unrelated', 'valid': False}):
            self.assertEqual(self.check('quote_invalidated', expected, state={'quote': quote},
                                        before=before).outcome, 'FAIL')
        self.assertEqual(self.check('quote_invalidated', expected, state={}, before=before).outcome, 'BLOCKED')

    def test_quote_replacement_cannot_use_unavailable_money_projection(self):
        spec = CheckSpec(check_id='replacement', scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind='quote_invalidated', criterion='Review replacement totals',
                         expected={'replacement_totals': {}})
        before = self.before.model_copy(update={'state': {'quote': {'id': 'old', 'valid': True}}})
        after = self.after.model_copy(update={'state': {'quote': {'id': 'new', 'valid': True}},
                                             'unavailable_sections': ['basket']})
        result = self.engine.evaluate(spec, self.turn, [before, after])
        self.assertEqual(result.outcome, 'BLOCKED')
        self.assertIn('money evidence is unavailable', result.explanation)

    def test_payment_amount_currency_status_and_cardinality(self):
        payment = {"order_id": "o1", "amount_minor": 15000, "currency": "INR", "status": "captured"}
        expected = {"match": {"order_id": "o1"}, "fields": {"amount_minor": 15000, "currency": "INR", "status": "captured"}}
        self.assertEqual(self.check("payment", expected, state={"payments": [payment]}).outcome, "PASS")
        for field, value in (("amount_minor", 1), ("currency", "USD"), ("status", "pending")):
            self.assertEqual(self.check("payment", expected, state={"payments": [{**payment, field: value}]}).outcome, "FAIL")
        self.assertEqual(self.check("payment", expected, state={"payments": [payment, payment]}).outcome, "FAIL")

    def test_pos_acceptance_is_receipt_not_order_creation(self):
        expected = {"match": {"order_id": "o1"}, "fields": {"status": "accepted"}}
        self.assertEqual(self.check("pos_acceptance", expected, state={"pos": [{"order_id": "o1", "status": "accepted"}]}).outcome, "PASS")
        self.assertEqual(self.check("pos_acceptance", expected, state={"pos": []}).outcome, "FAIL")

    def test_duplicate_effect_identity(self):
        effects = [{"id": "a", "operation_id": "op", "kind": "capture"}]
        self.assertEqual(self.check("duplicate_effects", {}, state={"effects": effects}).outcome, "PASS")
        effects.append({"id": "b", "operation_id": "op", "kind": "capture"})
        self.assertEqual(self.check("duplicate_effects", {}, state={"effects": effects}).outcome, "FAIL")

    def test_mutation_claim_requires_actual_change(self):
        self.assertEqual(self.check("mutation_claim", {}, path="basket").outcome, "PASS")
        self.assertEqual(self.check("mutation_claim", {}, path="basket", before=self.after.state).outcome, "FAIL")
        self.assertTrue(list(detected_claims("Your order has been placed.")))
        self.assertFalse(list(detected_claims("Your order has not been placed.")))
        self.assertFalse(list(detected_claims("Once your order is placed, we will notify you.")))

    def test_cross_customer_snapshot_is_never_used(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind="order_count", criterion="No order", expected={"count": 0})
        foreign = self.after.model_copy(update={"scenario_instance_id": "another-customer"})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [foreign]).outcome, "BLOCKED")

    def test_unavailable_section_overrides_stale_present_data(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind="totals", criterion="Consistent totals")
        after = self.after.model_copy(update={"unavailable_sections": ["basket"]})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [after]).outcome, "BLOCKED")

    def test_immediate_cannot_be_repaired_by_later_state(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind="equals", path="status", expected={"value": "accepted"}, criterion="Immediate acceptance")
        first = self.after.model_copy(update={"state": {"status": "pending"}})
        later = self.after.model_copy(update={"snapshot_id": "later", "captured_at": "2026-09-28T06:30:02+00:00", "state": {"status": "accepted"}})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [later, first], self.events).outcome, "FAIL")
        eventual = spec.model_copy(update={"timing": "eventual", "deadline_ms": 1000.0})
        result = self.engine.evaluate(eventual, self.turn, [later, first], self.events)
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(result.elapsed_to_completion_ms, 1000.0)

    def test_eventual_deadline_partial_absent_and_late(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0, kind="equals", path="status",
                         expected={"value": "accepted"}, criterion="Eventual acceptance", timing="eventual", deadline_ms=1000.0)
        first = self.after.model_copy(update={"state": {"status": "pending"}})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [first], self.events).outcome, "NEEDS_REVIEW")
        self.assertEqual(self.engine.evaluate(spec, self.turn, [first], []).outcome, "BLOCKED")
        late = self.after.model_copy(update={"snapshot_id": "late", "captured_at": "2026-09-28T06:30:03+00:00", "state": {"status": "accepted"}})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [first, late], self.events).outcome, "NEEDS_REVIEW")

    def test_saved_transition_can_prove_completion_between_polls(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0, kind="equals", path="status",
                         expected={"value": "accepted"}, criterion="Eventual acceptance", timing="eventual", deadline_ms=1000.0,
                         transition_at_path="accepted_at")
        after = self.after.model_copy(update={"captured_at": "2026-09-28T06:30:03+00:00",
                    "state": {"status": "accepted", "accepted_at": "2026-09-28T06:30:01.500+00:00"}})
        result = self.engine.evaluate(spec, self.turn, [after], self.events)
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(result.elapsed_to_completion_ms, 500.0)
        after.state["accepted_at"] = "2026-09-28T06:30:02.500+00:00"
        self.assertEqual(self.engine.evaluate(spec, self.turn, [after], self.events).outcome, "FAIL")
        after.state["accepted_at"] = "2026-09-28T06:30:04+00:00"
        self.assertEqual(self.engine.evaluate(spec, self.turn, [after], self.events).outcome, "NEEDS_REVIEW")

    def test_pre_dispatch_snapshot_without_request_id_is_usable(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind="order_count", criterion="No new orders", expected={"delta": 0})
        before = self.before.model_copy(update={"request_id": None})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [before, self.after]).outcome, "PASS")

    def test_ambiguous_timestamp_is_review(self):
        spec = CheckSpec(check_id="x", scenario_id=self.turn.scenario_id, original_turn_index=0,
                         kind="order_count", criterion="No order", expected={"count": 0})
        other = self.after.model_copy(update={"snapshot_id": "conflict", "state": {"orders": [{"id": "o1"}]}})
        self.assertEqual(self.engine.evaluate(spec, self.turn, [self.after, other]).outcome, "NEEDS_REVIEW")


if __name__ == "__main__":
    unittest.main()
