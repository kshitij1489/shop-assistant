"""Offline regressions for the integration defects found in review."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluate.checks.engine import DeterministicEvaluator, predicate, MissingEvidence
from evaluate.checks.models import CheckSpec
from evaluate.checks.plan_checks import generate_check_specs
from evaluate.contracts.models import ReferenceTurn, StateSnapshot
from evaluate.integration.command import _runner_options, _score_evidence, _render_report, _apply_workload
from evaluate.judges import ManualJudge
from evaluate.reports.artifacts import _ingest_application_rows, Diagnostic
from evaluate.reports.example import synthetic_run as example_bundle
from evaluate.reports.scoring import evaluate_run
from evaluate.runner.defaults import SnapshotBranchOracle, ProjectionSettlementPolicy
from evaluate.runner.branching import check_branch
from evaluate.runner.options import RunnerOptions
from evaluate.scenarios.plan import load_plan

ROOT = Path(__file__).resolve().parents[2]


def snapshot(state):
    return StateSnapshot(run_id='r', scenario_id='sessions:test', scenario_instance_id='i', attempt=1,
        event_id='e', snapshot_id='s', original_turn_index=0, request_id=None, phase='before',
        captured_at='2026-09-29T08:30:00+00:00', state=state, unavailable_sections=[])


class ReviewRegressions(unittest.TestCase):
    def test_branch_uses_unique_open_task_identity_across_languages_and_pauses(self):
        reference = ReferenceTurn(original_turn_index=1, text='reference', asks='Is this correct?',
            pending={'intent_type': 'location_based', 'sub_intents': ['confirm_delivery_address']})
        task = {'intent_type': 'location_based', 'sub_intent': 'confirm_delivery_address',
                'is_complete': False, 'query_id': 'address', 'follow_up_question': ['Confirme la dirección.']}
        oracle = SnapshotBranchOracle()
        state = {'chat': {'awaiting_followup_index': None, 'pending_question': None,
                         'ongoing_query_queue': [task]}}
        self.assertEqual(oracle.pending_question(snapshot(state), reference, None), 'matched')
        for queue, expected in (([], 'mismatch'), ([{**task, 'is_complete': True}], 'mismatch'),
                                ([{**task, 'sub_intent': 'add_delivery_address'}], 'mismatch'),
                                ([{**task, 'is_complete': 'false'}], 'unknown'),
                                ([{**task, 'follow_up_question': []}], 'mismatch'),
                                ([{**task, 'query_id': None}], 'unknown'),
                                ([task, task], 'unknown'), ([{}], 'unknown')):
            state['chat']['ongoing_query_queue'] = queue
            self.assertEqual(oracle.pending_question(snapshot(state), reference, None), expected)

    def test_branch_prefers_awaited_task_over_suspended_equivalent_tasks(self):
        reference = ReferenceTurn(original_turn_index=1, text='reference', asks='Home or Office?',
            pending={'intent_type': 'location_based', 'sub_intents': ['choose_delivery_address'],
                     'equivalent_tasks': [{'intent_type': 'placing_order',
                                           'sub_intents': ['add_to_basket']}]})
        suspended = {'intent_type': 'placing_order', 'sub_intent': 'add_to_basket',
                     'is_complete': False, 'query_id': 'basket',
                     'follow_up_question': ['Which saved address should I use?']}
        live = {'intent_type': 'placing_order', 'sub_intent': 'order_payment',
                'is_complete': False, 'query_id': 'payment',
                'follow_up_question': ['How would you like to pay?']}
        oracle = SnapshotBranchOracle()

        def outcome(index):
            state = {'chat': {'awaiting_followup_index': index,
                              'ongoing_query_queue': [suspended, live]}}
            return oracle.pending_question(snapshot(state), reference, None)

        self.assertEqual(outcome(0), 'matched')
        self.assertEqual(outcome(1), 'mismatch')
        self.assertEqual(outcome(None), 'matched')
        self.assertEqual(outcome(2), 'unknown')
        self.assertEqual(outcome(True), 'unknown')

        # An active matching row is decisive even when another incomplete row
        # is a second equivalent representation of the same capability.
        primary = {**suspended, 'intent_type': 'location_based',
                   'sub_intent': 'choose_delivery_address', 'query_id': 'address'}
        state = {'chat': {'awaiting_followup_index': 1,
                          'ongoing_query_queue': [suspended, primary]}}
        self.assertEqual(oracle.pending_question(snapshot(state), reference, None), 'matched')
        state['chat']['awaiting_followup_index'] = None
        self.assertEqual(oracle.pending_question(snapshot(state), reference, None), 'unknown')

        # s17 has the same queue shape with insufficient_information as the
        # equivalent suspended route: it must not override a different live ask.
        s17 = self.case('s17_')
        s17_reference = next(row for row in s17.references if row.pending is not None)
        insufficient = {**suspended, 'intent_type': 'insufficient_information',
                        'sub_intent': 'insufficient_information', 'query_id': 'reorder'}
        state = {'chat': {'awaiting_followup_index': 1,
                          'ongoing_query_queue': [insufficient, live]}}
        self.assertEqual(oracle.pending_question(snapshot(state), s17_reference, None), 'mismatch')
        state['chat']['awaiting_followup_index'] = 0
        self.assertEqual(oracle.pending_question(snapshot(state), s17_reference, None), 'matched')

    def test_reviewed_branch_contracts_use_required_conversation_state(self):
        context = SimpleNamespace(components=SimpleNamespace(branch_oracle=SnapshotBranchOracle()))

        def decision(prefix, index, queue):
            scenario = self.case(prefix)
            turn = next(row for row in scenario.turns if row.original_turn_index == index)
            state = {'chat': {'ongoing_query_queue': queue, 'awaiting_followup_index': 0 if queue else None,
                              'pending_question': (queue[0]['follow_up_question'][-1] if queue else None)}}
            return check_branch(context, scenario, turn, snapshot(state), None)

        def task(intent, sub_intent, question):
            return {'intent_type': intent, 'sub_intent': sub_intent, 'is_complete': False,
                    'query_id': 'open', 'follow_up_question': [question]}

        # These next utterances are independently meaningful. Their links to
        # reference replies remain review context, not execution prerequisites.
        for prefix, index in (('s14_', 2), ('s21_', 2), ('s26_', 2), ('s48_', 2)):
            with self.subTest(prefix=prefix):
                self.assertEqual(decision(prefix, index, []), 'not_applicable')

        # Equivalent item/address clarifications satisfy the capability even
        # when the application records them under a different internal route.
        self.assertEqual(decision('s17_', 2, [task('insufficient_information',
            'insufficient_information', 'What would you like to order again?')]), 'matched')
        self.assertEqual(decision('s42_', 2, [task('placing_order',
            'add_to_basket', 'Which dessert would you like to add?')]), 'matched')
        self.assertEqual(decision('s48_', 4, [task('placing_order',
            'add_to_basket', 'Which saved address should I use: Home or Office?')]), 'matched')

        # Scheduling is now a terminal store referral, with no pending slot question.
        self.assertEqual(decision('s22_', 2, [task('general',
            'cancel_and_abort', 'What would you like to cancel?')]), 'not_applicable')
        # The existing store-support referral likewise collects no order reference.
        self.assertEqual(decision('s46_', 2, []), 'not_applicable')

    def test_price_then_add_flows_do_not_require_optional_reference_tasks(self):
        context = SimpleNamespace(components=SimpleNamespace(branch_oracle=SnapshotBranchOracle()))
        empty = snapshot({'chat': {'ongoing_query_queue': [], 'awaiting_followup_index': None,
                                   'pending_question': None}})
        for prefix in ('s15_', 's152_', 's162_', 's172_', 's182_', 's192_'):
            with self.subTest(prefix=prefix):
                scenario = self.case(prefix)
                turn = next(row for row in scenario.turns if row.original_turn_index == 2)
                self.assertEqual(check_branch(context, scenario, turn, empty, None), 'not_applicable')

    def test_historical_payment_is_preserved_by_new_order_checks(self):
        payment = {'id': 'historical', 'status': 'captured', 'amount_minor': 92000}
        checks = [c for c in generate_check_specs([self.case('s136_')])
                  if c.path == 'payments' and c.kind == 'unchanged']
        self.assertEqual(len(checks), 3)
        before = {'payments': [payment]}
        for check in checks:
            self.assertTrue(predicate(check, deepcopy(before), before))
            self.assertFalse(predicate(check, {'payments': []}, before))
            self.assertFalse(predicate(check, {'payments': [payment, {'id': 'new'}]}, before))

    @classmethod
    def setUpClass(cls):
        cls.plan, cls.bundle = load_plan(ROOT / 'test_data')

    def case(self, prefix):
        return next(s for s in self.bundle.scenarios if s.source_id.startswith(prefix))

    def s102_results(self, state, *, before=None, unavailable=(), before_unavailable=(), index=0):
        return self.reviewed_results('s102_', state, before=before, unavailable=unavailable,
                                     before_unavailable=before_unavailable, index=index)

    def reviewed_results(self, prefix, state, *, before=None, unavailable=(), before_unavailable=(),
                         index=0, previous=None, previous_unavailable=()):
        scenario = self.case(prefix)
        sample = example_bundle()
        turn = sample.turns[0].model_copy(update={
            'scenario_id': scenario.scenario_id, 'original_turn_index': index})
        snapshots = [s.model_copy(update={
            'scenario_id': scenario.scenario_id, 'original_turn_index': index,
            'state': before if s.phase == 'before' else state,
            'unavailable_sections': list(before_unavailable if s.phase == 'before' else unavailable),
        }) for s in sample.snapshots[:2] if s.phase != 'before' or before is not None]
        if previous is not None:
            snapshots.append(sample.snapshots[1].model_copy(update={
                'scenario_id': scenario.scenario_id, 'original_turn_index': 4,
                'snapshot_id': 'prior-after', 'request_id': 'prior-request',
                'captured_at': '2026-09-28T06:29:59+00:00', 'state': previous,
                'unavailable_sections': list(previous_unavailable),
            }))
        return {c.kind + ':' + c.path: DeterministicEvaluator().evaluate(c, turn, snapshots)
                for c in generate_check_specs([scenario])
                if ':reviewed:' in c.check_id and c.original_turn_index == index}

    def s102_safe_state(self):
        return {'basket': {'items': []}, 'orders': [], 'payments': [], 'commands': [],
                'chat': {'ongoing_query_queue': []}}

    def test_s102_safe_rejection_requires_all_state_checks_on_every_turn(self):
        state = self.s102_safe_state()
        for index in (0, 2, 4):
            with self.subTest(index=index):
                results = self.s102_results(state, before=deepcopy(state), index=index)
                self.assertEqual(set(results), {'basket:', 'unchanged:basket', 'order_count:',
                    'unchanged:payments', 'equals:commands', 'tasks_complete:chat/ongoing_query_queue'})
                self.assertEqual({r.outcome for r in results.values()}, {'PASS'})

    def test_s102_huge_or_silently_clamped_quantity_fails(self):
        before = self.s102_safe_state()
        for quantity in (999999999999999999999999999999, 20, 1):
            with self.subTest(quantity=quantity):
                state = deepcopy(before)
                state['basket']['items'] = [{'item_id': 'pistachio', 'variant_id': 'standard',
                    'name': 'Pistachio Ice Cream', 'quantity': quantity, 'unit_price_minor': 46000}]
                results = self.s102_results(state, before=before)
                self.assertEqual(results['basket:'].outcome, 'FAIL')
                self.assertEqual(results['unchanged:basket'].outcome, 'FAIL')

    def test_s102_orders_payments_and_queued_commands_fail_independently(self):
        before = self.s102_safe_state()
        for field, check in (('orders', 'order_count:'), ('payments', 'unchanged:payments'),
                             ('commands', 'equals:commands')):
            with self.subTest(field=field):
                state = deepcopy(before)
                state[field] = [{'id': 'unexpected', 'kind': 'payment.create', 'status': 'pending'}]
                self.assertEqual(self.s102_results(state, before=before)[check].outcome, 'FAIL')

    def test_s102_checks_dormant_tasks_even_without_an_active_followup(self):
        before = self.s102_safe_state()
        key = 'tasks_complete:chat/ongoing_query_queue'
        for complete, outcome in ((False, 'FAIL'), (True, 'PASS'), (None, 'BLOCKED'), ('true', 'BLOCKED')):
            with self.subTest(complete=complete):
                state = deepcopy(before)
                state['chat'] = {'awaiting_followup_index': None, 'pending_question': None,
                    'ongoing_query_queue': [{'intent_type': 'placing_order', 'sub_intent': 'add_to_basket',
                                             'is_complete': complete}]}
                self.assertEqual(self.s102_results(state, before=before)[key].outcome, outcome)
        state['chat']['ongoing_query_queue'] = [{'intent_type': 'menu_items', 'is_complete': False}]
        self.assertEqual(self.s102_results(state, before=before)[key].outcome, 'PASS')

    def test_s102_missing_or_unavailable_evidence_never_passes(self):
        before = self.s102_safe_state()
        for field, check in (('basket', 'basket:'), ('orders', 'order_count:'),
                             ('payments', 'unchanged:payments'), ('commands', 'equals:commands'),
                             ('chat', 'tasks_complete:chat/ongoing_query_queue')):
            with self.subTest(field=field):
                state = deepcopy(before)
                del state[field]
                self.assertEqual(self.s102_results(state, before=before)[check].outcome, 'BLOCKED')
                self.assertEqual(self.s102_results(before, before=before, unavailable=[field])[check].outcome, 'BLOCKED')
        self.assertEqual(self.s102_results(before)['unchanged:basket'].outcome, 'BLOCKED')
        self.assertEqual(self.s102_results(before, before=before, before_unavailable=['basket'])[
            'unchanged:basket'].outcome, 'BLOCKED')
        for queue in (None, {}, [None], [{'sub_intent': 'add_to_basket', 'completed': True}],
                      [{'intent_type': 'placing_order'}], [{'intent_type': None, 'is_complete': True}],
                      [{'intent_type': '', 'is_complete': True}]):
            with self.subTest(queue=queue):
                state = deepcopy(before)
                state['chat']['ongoing_query_queue'] = queue
                result = self.s102_results(state, before=before)['tasks_complete:chat/ongoing_query_queue']
                self.assertEqual(result.outcome, 'BLOCKED')

    def test_s102_cannot_hide_an_earlier_mutation_by_clearing_the_basket(self):
        state = self.s102_safe_state()
        before = deepcopy(state)
        before['basket']['items'] = [{'item_id': 'existing', 'variant_id': 'standard', 'quantity': 1}]
        self.assertEqual(self.s102_results(state, before=before, index=2)['unchanged:basket'].outcome, 'FAIL')

    def s102_report_bundle(self):
        scenario = self.case('s102_').model_copy(deep=True)
        # Exercise the first defect independently of scripted continuation.
        scenario.turns = scenario.turns[:1]
        scenario.references = scenario.references[:1]
        sample = example_bundle()
        turn = sample.turns[0].model_copy(update={
            'scenario_id': scenario.scenario_id, 'message': scenario.turns[0].text,
            'response_text': 'That quantity exceeds the ordering limits. Please request a smaller quantity. '
                             'Your basket is unchanged; no order or payment was created.'})
        state = {**self.s102_safe_state(), 'address_selection': {'authorized': True},
                 'ownership': {'tenant_id': 'tenant', 'customer_id': 'customer'},
                 'addresses': [], 'effects': []}
        sample.scenarios = [scenario]
        sample.manifest.scenario_ids = [scenario.scenario_id]
        sample.turns = [turn]
        sample.snapshots = [s.model_copy(update={'scenario_id': scenario.scenario_id, 'state': deepcopy(state)})
                            for s in sample.snapshots[:2]]
        sample.events = []
        sample.checks = []
        sample.measurements = []
        return type(sample).model_validate(sample.model_dump())

    def test_s102_report_requires_rejection_review_and_preserves_state_failures(self):
        bundle = self.s102_report_bundle()
        report = evaluate_run(bundle)
        self.assertEqual(report['overall']['outcome'], 'NEEDS_REVIEW')
        rejection = next(row for row in report['manual_review']
                         if 'Explicitly reject the excessive quantity' in row['context']['item'])
        self.assertEqual(rejection['verdict']['outcome'], 'NEEDS_REVIEW')
        self.assertTrue(all(row['outcome'] == 'PASS' for row in report['assertions']
                            if row['category'] != 'semantic'))

        def synthetic_decisions(report):
            # Test report aggregation with injected verdicts; these are not live quality evidence.
            return ManualJudge([{**row, 'reviewer': 'synthetic-test',
                'verdict': {'outcome': 'PASS', 'reason': 'Synthetic aggregation test verdict.',
                            'evidence_ids': [bundle.turns[0].event_id]}}
                for row in report['manual_review']])

        self.assertEqual(evaluate_run(bundle, synthetic_decisions(report))['overall']['outcome'], 'PASS')
        bundle.snapshots[1].state['basket']['items'] = [
            {'item_id': 'pistachio', 'variant_id': 'standard', 'quantity': 999999999999999999999999999999}]
        report = evaluate_run(bundle)
        self.assertEqual(evaluate_run(bundle, synthetic_decisions(report))['overall']['outcome'], 'FAIL')

    def test_s102_report_exposes_missing_task_evidence(self):
        bundle = self.s102_report_bundle()
        del bundle.snapshots[1].state['chat']['ongoing_query_queue']
        report = evaluate_run(bundle)
        row = next(row for row in report['assertions'] if 'tasks_complete' in row['assertion_id'])
        self.assertEqual(row['outcome'], 'BLOCKED')
        self.assertIn('Missing state field', row['explanation'])
        self.assertEqual(report['overall']['outcome'], 'BLOCKED')

    def s110_state(self, index):
        return {
            'basket': {'items': [] if index < 6 else [{
                'name': 'Fudgy Chocolate Brownie (2pcs)', 'item_id': 'brownie',
                'variant_id': 'standard', 'quantity': 1, 'unit_price_minor': 32000}]},
            'orders': [], 'payments': [], 'commands': [],
            'chat': {'ongoing_query_queue': [{
                'query_id': 1, 'intent_type': 'placing_order', 'sub_intent': 'add_to_basket',
                'is_complete': False, 'follow_up_question': ['Which brownie would you like?'],
            }] if index < 6 else [],
            'awaiting_followup_index': 0 if index == 0 else None,
            'pending_question': 'Which brownie would you like?' if index == 0 else None},
        }

    def test_s110_complete_timeline_requires_choice_retention_and_cleared_tasks(self):
        scenario = self.case('s110_')
        self.assertEqual([t.original_turn_index for t in scenario.turns], [0, 2, 4, 6, 8])
        for index in (0, 2, 4, 6, 8):
            with self.subTest(index=index):
                results = self.reviewed_results('s110_', self.s110_state(index),
                    before=self.s110_state(max(0, index - 2)), index=index,
                    previous=self.s110_state(4) if index == 6 else None)
                expected = {'basket:', 'order_count:', 'unchanged:payments', 'equals:commands'}
                if index != 6:
                    expected.add('unchanged:basket')
                if index < 6:
                    expected.add('tasks_pending:chat/ongoing_query_queue')
                else:
                    expected.update({'tasks_complete:chat/ongoing_query_queue',
                                     'equals:chat/awaiting_followup_index', 'equals:chat/pending_question'})
                self.assertEqual(set(results), expected)
                self.assertEqual({r.outcome for r in results.values()}, {'PASS'})

    def test_s110_existing_basket_oracle_rejects_early_duplicate_and_wrong_additions(self):
        for index, quantity in ((0, 1), (2, 1), (4, 1), (6, 0), (6, 2), (8, 2)):
            with self.subTest(index=index, quantity=quantity):
                state = self.s110_state(index)
                state['basket'] = self.s110_state(6)['basket']
                state['basket']['items'][0]['quantity'] = quantity
                results = self.reviewed_results('s110_', state, before=self.s110_state(4), index=index)
                self.assertEqual(results['basket:'].outcome, 'FAIL')
        state = self.s110_state(6)
        state['basket']['items'][0]['name'] = 'Brownie With Vanilla Ice Cream & Fudge Sauce'
        self.assertEqual(self.reviewed_results('s110_', state, before=self.s110_state(4),
                                              index=6)['basket:'].outcome, 'FAIL')

    def test_s110_cannot_hide_prior_addition_by_clearing_basket_on_detour(self):
        for index in (0, 2, 4):
            results = self.reviewed_results('s110_', self.s110_state(index),
                                           before=self.s110_state(6), index=index)
            self.assertEqual(results['unchanged:basket'].outcome, 'FAIL')

    def test_s110_lost_completed_duplicate_or_questionless_choice_fails(self):
        task = self.s110_state(0)['chat']['ongoing_query_queue'][0]
        for queue in ([], [{**task, 'is_complete': True}], [task, task],
                      [task, {**task, 'is_complete': True}],
                      [{**task, 'follow_up_question': []}],
                      [{**task, 'follow_up_question': ['Which brownie?', ' ']}],
                      [{**task, 'intent_type': 'information_about_the_cafe'}]):
            for index in (0, 2, 4):
                with self.subTest(queue=queue, index=index):
                    state = self.s110_state(index)
                    state['chat']['ongoing_query_queue'] = deepcopy(queue)
                    results = self.reviewed_results('s110_', state,
                        before=self.s110_state(0), index=index)
                    self.assertEqual(results['tasks_pending:chat/ongoing_query_queue'].outcome, 'FAIL')

    def test_s110_detour_must_preserve_task_identity_and_operation(self):
        for field, value in (('query_id', 2), ('query_id', '1'), ('sub_intent', 'remove_from_basket')):
            for index in (2, 4):
                with self.subTest(field=field, value=value, index=index):
                    state = self.s110_state(index)
                    state['chat']['ongoing_query_queue'][0][field] = value
                    results = self.reviewed_results('s110_', state,
                        before=self.s110_state(0), index=index)
                    self.assertEqual(results['tasks_pending:chat/ongoing_query_queue'].outcome, 'FAIL')
        # Queue positions and question wording are not task identities.
        state = self.s110_state(4)
        state['chat']['ongoing_query_queue'][0]['follow_up_question'] = ['Please choose a brownie.']
        state['chat']['ongoing_query_queue'].insert(0, {'intent_type': 'information_about_the_cafe'})
        self.assertEqual(self.reviewed_results('s110_', state, before=self.s110_state(2), index=4)
                         ['tasks_pending:chat/ongoing_query_queue'].outcome, 'PASS')

    def test_s110_missing_malformed_or_unavailable_task_evidence_blocks(self):
        key = 'tasks_pending:chat/ongoing_query_queue'
        for field, bad in (('query_id', None), ('query_id', True), ('query_id', -1),
                           ('query_id', ''), ('query_id', '[REDACTED:jwt]'),
                           ('sub_intent', ''), ('intent_type', None), ('is_complete', 'false'),
                           ('follow_up_question', 'Which brownie?'), ('follow_up_question', [None])):
            for missing in (False, True):
                with self.subTest(field=field, value=bad, missing=missing):
                    state = self.s110_state(2)
                    row = state['chat']['ongoing_query_queue'][0]
                    if missing:
                        row.pop(field)
                    else:
                        row[field] = bad
                    self.assertEqual(self.reviewed_results('s110_', state,
                        before=self.s110_state(0), index=2)[key].outcome, 'BLOCKED')
        for bad in (None, {}, [None]):
            state = self.s110_state(2)
            state['chat']['ongoing_query_queue'] = bad
            self.assertEqual(self.reviewed_results('s110_', state,
                before=self.s110_state(0), index=2)[key].outcome, 'BLOCKED')
        for kwargs in ({}, {'before': self.s110_state(0), 'before_unavailable': ['chat']},
                       {'before': self.s110_state(0), 'unavailable': ['chat']}):
            self.assertEqual(self.reviewed_results('s110_', self.s110_state(2), index=2,
                                                   **kwargs)[key].outcome, 'BLOCKED')
        before = self.s110_state(0)
        del before['chat']['ongoing_query_queue'][0]['query_id']
        self.assertEqual(self.reviewed_results('s110_', self.s110_state(2),
            before=before, index=2)[key].outcome, 'BLOCKED')

    def test_s110_completion_rejects_dormant_tasks_and_stale_prompt_pointers(self):
        for index in (6, 8):
            for field, value, kind in (
                    ('ongoing_query_queue', self.s110_state(0)['chat']['ongoing_query_queue'], 'tasks_complete'),
                    ('awaiting_followup_index', 0, 'equals'),
                    ('pending_question', 'Which brownie?', 'equals')):
                state = self.s110_state(index)
                state['chat'][field] = value
                results = self.reviewed_results('s110_', state, index=index,
                    before=self.s110_state(4), previous=self.s110_state(4))
                self.assertEqual(results[f'{kind}:chat/{field}'].outcome, 'FAIL')
                del state['chat'][field]
                results = self.reviewed_results('s110_', state, index=index,
                    before=self.s110_state(4), previous=self.s110_state(4))
                self.assertEqual(results[f'{kind}:chat/{field}'].outcome, 'BLOCKED')
        state = self.s110_state(6)
        state['chat']['ongoing_query_queue'] = self.s110_state(0)['chat']['ongoing_query_queue']
        state['chat']['ongoing_query_queue'][0]['is_complete'] = True
        self.assertEqual(self.reviewed_results('s110_', state, index=6,
            before=self.s110_state(4), previous=self.s110_state(4))
                         ['tasks_complete:chat/ongoing_query_queue'].outcome, 'PASS')

    def test_s110_selection_requires_empty_basket_and_same_pending_choice_beforehand(self):
        after, prior = self.s110_state(6), self.s110_state(4)
        key = 'tasks_complete:chat/ongoing_query_queue'
        for before, expected in ((None, 'BLOCKED'), (after, 'FAIL')):
            results = self.reviewed_results('s110_', after, before=before, previous=prior, index=6)
            self.assertEqual(results['basket:'].outcome, expected)
            self.assertEqual(results[key].outcome, expected)
        for field, value in (('query_id', 2), ('sub_intent', 'remove_from_basket'),
                             ('is_complete', True), ('follow_up_question', [])):
            before = deepcopy(prior)
            before['chat']['ongoing_query_queue'][0][field] = value
            self.assertEqual(self.reviewed_results('s110_', after, before=before,
                previous=prior, index=6)[key].outcome, 'FAIL')
        for kwargs in ({}, {'previous': prior, 'previous_unavailable': ['chat']},
                       {'previous': prior, 'before_unavailable': ['chat']}):
            self.assertEqual(self.reviewed_results('s110_', after, before=prior,
                index=6, **kwargs)[key].outcome, 'BLOCKED')
        self.assertEqual(self.reviewed_results('s110_', after, before=prior, previous=prior,
            before_unavailable=['basket'], index=6)['basket:'].outcome, 'BLOCKED')
        # The previous turn must also have exactly one open choice.
        for target in ('before', 'previous'):
            states = {'before': deepcopy(prior), 'previous': deepcopy(prior)}
            queue = states[target]['chat']['ongoing_query_queue']
            queue.append({**queue[0], 'is_complete': True})
            self.assertEqual(self.reviewed_results('s110_', after, index=6,
                **states)[key].outcome, 'FAIL')

    def test_s110_forbids_orders_payments_and_queued_commands_on_every_turn(self):
        for index in (0, 2, 4, 6, 8):
            for field, key in (('orders', 'order_count:'), ('payments', 'unchanged:payments'),
                               ('commands', 'equals:commands')):
                state = self.s110_state(index)
                state[field] = [{'id': 'unexpected'}]
                self.assertEqual(self.reviewed_results('s110_', state, index=index, before=self.s110_state(index))[key].outcome, 'FAIL')
                del state[field]
                self.assertEqual(self.reviewed_results('s110_', state, index=index, before=self.s110_state(index))[key].outcome, 'BLOCKED')

    def test_s110_changed_source_hash_does_not_receive_reviewed_checks(self):
        changed = self.case('s110_').model_copy(update={'source_hash': '0' * 64})
        self.assertFalse(any(':reviewed:' in c.check_id for c in generate_check_specs([changed])))

    def test_s110_report_keeps_task_failures_and_evidence_gaps_despite_semantic_passes(self):
        scenario = self.case('s110_').model_copy(deep=True)
        sample = example_bundle()
        template_turn, templates = sample.turns[0], sample.snapshots[:2]
        sample.scenarios = [scenario]
        sample.manifest.scenario_ids = [scenario.scenario_id]
        sample.turns, sample.snapshots = [], []
        sample.events, sample.checks, sample.measurements = [], [], []
        for source in scenario.turns:
            index = source.original_turn_index
            request = f'request-s110-{index}'
            sample.turns.append(template_turn.model_copy(update={
                'event_id': f'turn-s110-{index}', 'request_id': request,
                'scenario_id': scenario.scenario_id, 'original_turn_index': index,
                'user_turn_index': source.user_turn_index, 'message': source.text,
                'response_text': 'Synthetic response for report aggregation.',
                'snapshot_ids': [f'after-s110-{index}'],
            }))
            for template in templates:
                state = self.s110_state(max(0, index - 2) if template.phase == 'before' else index)
                state.update(address_selection={'authorized': True},
                             ownership={'tenant_id': 'tenant', 'customer_id': 'customer'},
                             addresses=[], effects=[])
                sample.snapshots.append(template.model_copy(update={
                    'event_id': f'event-{template.phase}-s110-{index}',
                    'snapshot_id': f'{template.phase}-s110-{index}', 'request_id': request,
                    'scenario_id': scenario.scenario_id, 'original_turn_index': index, 'state': state,
                    'captured_at': f'2026-09-28T06:30:{index + int(template.phase == "after"):02d}+00:00',
                }))
        sample = type(sample).model_validate(sample.model_dump())
        initial = evaluate_run(sample)
        self.assertEqual(initial['overall']['outcome'], 'NEEDS_REVIEW')
        self.assertTrue(all(row['outcome'] == 'PASS' for row in initial['assertions']
                            if row['category'] != 'semantic'))
        for mode, expected in (('valid', 'PASS'), ('lost', 'FAIL'), ('missing', 'BLOCKED'),
                               ('no_before_selection', 'BLOCKED'), ('already_added', 'FAIL'),
                               ('replaced_before_selection', 'FAIL')):
            with self.subTest(mode=mode):
                bundle = sample.model_copy(deep=True)
                detour = next(s for s in bundle.snapshots
                              if s.phase == 'after' and s.original_turn_index == 4)
                if mode == 'lost':
                    detour.state['chat']['ongoing_query_queue'] = []
                elif mode == 'missing':
                    del detour.state['chat']['ongoing_query_queue'][0]['query_id']
                elif mode == 'no_before_selection':
                    bundle.snapshots = [s for s in bundle.snapshots
                                        if not (s.phase == 'before' and s.original_turn_index == 6)]
                elif mode in {'already_added', 'replaced_before_selection'}:
                    selection = next(s for s in bundle.snapshots
                                     if s.phase == 'before' and s.original_turn_index == 6)
                    if mode == 'already_added':
                        selection.state['basket'] = self.s110_state(6)['basket']
                        selection.state['chat'] = self.s110_state(6)['chat']
                    else:
                        selection.state['chat']['ongoing_query_queue'][0]['query_id'] = 2
                report = evaluate_run(bundle)
                # Injected verdicts exercise aggregation, not actual reply quality.
                judge = ManualJudge([{**row, 'reviewer': 'synthetic-test', 'verdict': {
                    'outcome': 'PASS', 'reason': 'Synthetic aggregation test verdict.',
                    'evidence_ids': [row['context']['conversation'][-1]['event_id']],
                }} for row in report['manual_review']])
                self.assertEqual(evaluate_run(bundle, judge)['overall']['outcome'], expected)

    def test_every_bundled_stateful_turn_has_reviewed_business_expectations(self):
        specs = generate_check_specs(self.bundle.scenarios)
        covered = {(s.scenario_id, s.original_turn_index) for s in specs
                   if s.kind not in {'ownership', 'duplicate_effects'} and s.path != 'address_selection/authorized'}
        missing = [(s.scenario_id, t.original_turn_index) for s in self.bundle.scenarios
                   if any(p != 'knowledge_only' for p in s.setup_profiles) for t in s.turns
                   if (s.scenario_id, t.original_turn_index) not in covered]
        self.assertEqual(missing, [])
        changed = self.case('s121_').model_copy(update={'source_hash': '0' * 64})
        self.assertFalse(any(':reviewed:' in c.check_id for c in generate_check_specs([changed])))

    def test_s123_contextual_yes_places_one_order_and_later_turns_cannot_duplicate_it(self):
        checks = generate_check_specs([self.case('s123_')])
        for index in (10, 12, 14, 16, 18, 20):
            count = next(c for c in checks if c.original_turn_index == index and c.kind == 'order_count')
            with self.subTest(index=index):
                orders = [] if index == 10 else [{'id': 'one'}]
                self.assertTrue(predicate(count, {'orders': orders}, {}))
                wrong = [{'id': 'early'}] if index == 10 else []
                self.assertFalse(predicate(count, {'orders': wrong}, {}))
                self.assertFalse(predicate(count, {'orders': [{'id': 'one'}, {'id': 'two'}]}, {}))

    def test_pickup_order_and_duplicate_are_not_covered_by_presence_checks(self):
        scenario = self.case('s121_')
        checks = generate_check_specs([scenario])
        after_confirm = [c for c in checks if c.original_turn_index == 14]
        count = next(c for c in after_confirm if c.kind == 'order_count')
        self.assertTrue(predicate(count, {'orders': [{'id': 'one'}]}, {}))
        self.assertFalse(predicate(count, {'orders': [{'id': 'one'}, {'id': 'two'}]}, {}))
        self.assertTrue(any(c.kind == 'pos_acceptance' for c in after_confirm))
        basket = next(c for c in checks if c.kind == 'basket' and c.original_turn_index == 0)
        self.assertFalse(predicate(basket, {'basket': {'items': []}}, {}))

    def test_ownership_compares_trusted_scope_and_rejects_foreign_ids(self):
        check = CheckSpec(check_id='owned', scenario_id='sessions:test', original_turn_index=0,
            kind='ownership', path='orders', criterion='owned', expected={'scope_path': 'ownership'})
        before = {'ownership': {'tenant_id': 'tenant', 'customer_id': 'customer'}}
        self.assertFalse(predicate(check, {'orders': [{'tenant_id': 'wrong', 'customer_id': 'wrong'}]}, before))
        self.assertTrue(predicate(check, {'orders': [{'tenant_id': 'tenant', 'customer_id': 'customer'}]}, before))
        with self.assertRaises(MissingEvidence):
            predicate(check, {'orders': []}, None)

    def test_all_command_kinds_are_unique_per_operation(self):
        specs = generate_check_specs([self.case('s121_')])
        check = next(c for c in specs if c.check_id.endswith(':duplicate-effects'))
        for kind in ('payment.create', 'order.submit', 'payment.reconcile', 'payment.refund', 'order.reconcile'):
            with self.subTest(kind=kind):
                row = {'order_id': 'o1', 'operation_id': 'op1', 'kind': kind}
                self.assertTrue(predicate(check, {'effects': [row]}, {}))
                self.assertFalse(predicate(check, {'effects': [row, dict(row)]}, {}))
                # A distinct reconciliation/refund operation is allowed on the same order.
                self.assertTrue(predicate(check, {'effects': [row, {**row, 'operation_id': 'op2'}]}, {}))
        per_order = next(c for c in specs if c.check_id.endswith(':duplicate-order-effects'))
        for kind in ('payment.create', 'order.submit'):
            rows = [{'order_id': 'o1', 'operation_id': op, 'kind': kind} for op in ('op1', 'op2')]
            self.assertFalse(predicate(per_order, {'effects': rows}, {}))

    def test_corrected_address_excludes_the_wrong_saved_address(self):
        from evaluate.fixtures.definitions import address
        specs = generate_check_specs([self.case('s31_')])
        for check in [c for c in specs if c.kind == 'addresses' and c.original_turn_index >= 4]:
            with self.subTest(turn=check.original_turn_index):
                corrected = {'components': address('flat18')['components']}
                wrong = {'components': address('flat2')['components']}
                self.assertTrue(predicate(check, {'addresses': [corrected]}, {}))
                self.assertFalse(predicate(check, {'addresses': [wrong, corrected]}, {}))

    def test_price_change_keeps_basket_prices_until_explicit_review(self):
        specs = generate_check_specs([self.case('s129_')])
        changes = [c for c in specs if c.kind == 'quote_invalidated' and c.original_turn_index == 12]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].expected, {'replacement_totals': {'currency': 'INR'}})
        self.assertFalse(any(c.kind == 'totals' and c.original_turn_index == 12 for c in specs))
        for check in [c for c in specs if c.kind == 'basket' and c.original_turn_index >= 12]:
            with self.subTest(turn=check.original_turn_index):
                item = {'name': 'Pistachio Ice Cream', 'quantity': 2, 'unit_price_minor': 46000}
                self.assertTrue(predicate(check, {'basket': {'items': [item]}}, {}))
                for price in (47000, 1):
                    self.assertFalse(predicate(check, {'basket': {'items': [{**item, 'unit_price_minor': price}]}}, {}))
                del item['unit_price_minor']
                with self.assertRaises(MissingEvidence):
                    predicate(check, {'basket': {'items': [item]}}, {})

    def test_pending_link_without_future_capture_does_not_wait_but_pos_does(self):
        scenario = self.case('s132_')
        state = snapshot({'payment': {'status': 'pending'}, 'pending_async': ['payment', 'pos']})
        self.assertEqual(ProjectionSettlementPolicy().unexpected_pending_sections(state, scenario, 14), ['pos'])
        paid_scenario = self.case('s122_')
        self.assertEqual(ProjectionSettlementPolicy().unexpected_pending_sections(state, paid_scenario, 20), ['payment', 'pos'])

    def test_provider_creation_is_observed_without_waiting_for_customer_payment(self):
        policy = ProjectionSettlementPolicy()
        # Both a future capture and a scenario that never pays must observe
        # queued provider work before issuing a follow-up request.
        for prefix, index in [('s122_', 16), ('s135_', 12)]:
            scenario = self.case(prefix)
            for status in ('pending', 'leased', 'succeeded', 'unknown', 'failed'):
                state = snapshot({'payment': {'status': 'pending'},
                    'pending_async': ['payment'],
                    'commands': [{'kind': 'payment.create', 'status': status}]})
                with self.subTest(scenario=prefix, status=status):
                    self.assertEqual(policy.unexpected_pending_sections(state, scenario, index),
                        ['payment_creation'] if status in {'pending', 'leased'} else [])

    def test_online_confirmation_checks_allow_provider_creation_after_reply(self):
        for prefix, index in [('s122_', 16), ('s135_', 12)]:
            checks = generate_check_specs([self.case(prefix)])
            payment = next(c for c in checks if c.kind == 'payment' and c.original_turn_index == index)
            self.assertEqual(payment.timing, 'eventual')
            self.assertEqual(payment.deadline_ms, 20000)
            state = {'payments': [{'status': 'pending', 'amount_minor': 102000 if index == 16 else 92000,
                                  'currency': 'INR', 'provider_created': True}]}
            self.assertTrue(predicate(payment, state, None))
            for mutation in ({'provider_created': False}, {'status': 'captured'}, {'amount_minor': 1}):
                changed = deepcopy(state)
                changed['payments'][0].update(mutation)
                self.assertFalse(predicate(payment, changed, None))
            state['payments'] *= 2
            self.assertFalse(predicate(payment, state, None))

    def test_legacy_wording_differences_need_reviewed_task_expectations(self):
        reference = ReferenceTurn(original_turn_index=1, text='reference', asks='How many would you like?')
        oracle = SnapshotBranchOracle()
        self.assertEqual(oracle.pending_question(snapshot({'chat': {'pending_question': 'What quantity would you like?'}}), reference, None), 'unknown')
        self.assertEqual(oracle.pending_question(snapshot({'chat': {'pending_question': 'What is your phone number?'}}), reference, None), 'unknown')

    def test_cost_budget_requires_a_rate_and_enforces_it(self):
        from evaluate.runner.budget import BudgetTracker, BudgetExhausted
        with self.assertRaises(ValueError):
            _runner_options(SimpleNamespace(max_estimated_cost=1))
        options = _runner_options(SimpleNamespace(max_estimated_cost=1, cost_per_million_tokens_minor=1000,
                                                  estimated_tokens_per_request=1000))
        tracker = BudgetTracker(options.budget, options.load)
        tracker.reserve_request()
        with self.assertRaises(BudgetExhausted):
            tracker.reserve_request()

    def test_telemetry_counts_complete_model_calls_only_and_deduplicates(self):
        data = {'turns': [{'request_id': 'r'}]}
        start = {'event_id': 'start', 'event': 'llm.started', 'request_id': 'r', 'call_id': 'call'}
        end = {**start, 'event_id': 'end', 'event': 'llm.completed', 'status': 'failed',
               'error_type': 'Timeout', 'input_tokens': 3, 'output_tokens': 0}
        workflow = {'event_id': 'flow', 'event': 'workflow.completed', 'request_id': 'r', 'status': 'failed'}
        _ingest_application_rows(data, [start, end, workflow], 'one')
        _ingest_application_rows(data, [start, end], 'two')
        self.assertEqual(data['measurements'][0]['api_errors'], 1)
        self.assertEqual(data['measurements'][0]['input_tokens'], 3)
        self.assertEqual(len(data['diagnostics']), 3)
        incomplete = {'turns': [{'request_id': 'r'}]}
        _ingest_application_rows(incomplete, [end, workflow], 'one')
        self.assertIsNone(incomplete['measurements'][0]['api_errors'])
        self.assertNotIn('input_tokens', incomplete['measurements'][0])

    def test_warmup_assertions_and_crash_only_attempts_are_excluded(self):
        bundle = example_bundle()
        baseline = evaluate_run(bundle)
        first = bundle.turns[0]
        warm_ids = {r['assertion_id'] for r in baseline['assertions']
                    if (r['scenario_instance_id'], r['attempt']) == (first.scenario_instance_id, first.attempt)}
        bundle.turns[0] = first.model_copy(update={'warm_up': True})
        report = evaluate_run(bundle)
        self.assertEqual(report['overall']['assertions']['denominator'],
                         baseline['overall']['assertions']['denominator'] - len(warm_ids))
        bundle.turns = [t for t in bundle.turns if t.scenario_instance_id != first.scenario_instance_id]
        bundle.diagnostics.append(Diagnostic(evidence_id='attempt:warm', kind='attempt', source='attempts.jsonl',
            data={'scenario_id': first.scenario_id, 'scenario_instance_id': first.scenario_instance_id,
                  'attempt': first.attempt, 'warm_up': True}))
        report = evaluate_run(bundle)
        self.assertTrue(next(s for s in report['sessions'] if s['scenario_instance_id'] == first.scenario_instance_id)['warm_up'])

    def test_score_then_render_uses_created_evaluation_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = example_bundle().model_dump(mode='json')
            for key in ('manifest', 'scenarios', 'turns', 'snapshots', 'events', 'checks', 'knowledge'):
                (root / f'{key}.json').write_text(json.dumps(bundle[key]))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(_score_evidence(root), 0)
                self.assertEqual(_render_report(root), 0)
            pointer = json.loads((root / 'latest-report.json').read_text())
            self.assertTrue((root / pointer['path']).is_file())

    def test_smoke_includes_location_cash_and_online(self):
        selected = _apply_workload('smoke', self.bundle.scenarios, RunnerOptions())
        self.assertTrue({'s06_save_address', 's121_pickup_cash_end_to_end', 's122_delivery_online_end_to_end'} <= {s.source_id for s in selected})
