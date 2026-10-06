"""Offline provider replay: verifies integration, never claims model accuracy."""
from tests.support.runtime import classification_rows
from dataclasses import replace
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings
from langchain_openai import ChatOpenAI

from chatbot_core.capabilities import CONTROL_ROUTES
from chatbot_core.knowledge_cache import get_intent_classification_cache
from chatbot_core.runtime_configuration import ClassificationSchema, RuntimeConfiguration
from evaluate.controls.context import activate
from evaluate.controls.llm import callback
from evaluate.controls.tests.test_controls import context
from tests.support.llm import ProviderHarness

combined = importlib.import_module("chatbot_core.logic.cafe.prompts.normalize_and_classify")
FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/combined_classification.json").read_text())


class ContextualExpectationTests(SimpleTestCase):
    def test_preservation_expectation_rejects_removal_of_retained_or_both_items(self):
        from copy import deepcopy
        from scripts.evaluate_contextual import validate_output
        from tests.unit.test_action_resolver import add_line
        cases = json.loads((Path(__file__).parents[1] / 'fixtures/basket_preservation.json').read_text())
        case = next(c for c in cases if c['id'] == 's164-original')
        action = add_line(None).model_dump()
        action['basket']['preserved_references'] = [{'by': 'name', 'value': 'cheesecake'}]
        line = action['basket']['lines'][0]
        line.update(action='remove', quantity=None, variant_id=None, modifiers=None,
                    reference={'by': 'name', 'value': 'brownie'})
        parsed = {'declared_constraints': [], 'classifications': [{
            'reply_to': None, 'clarification': None, 'action': action}]}
        self.assertEqual(validate_output(case, parsed), [])
        line['reference']['value'] = 'cheesecake'
        self.assertIn('basket targets', validate_output(case, parsed))
        action['basket']['lines'].append(deepcopy(line))
        line['reference']['value'] = 'brownie'
        self.assertIn('typed decisions', validate_output(case, parsed))
        self.assertIn('basket targets', validate_output(case, parsed))

    def test_rewrite_checks_ignore_case_and_reject_duplicated_independent_clauses(self):
        from scripts.evaluate_contextual import validate_output
        case = {'reply_to': None, 'clarify': False, 'rewrite_checks': [
            {'unit': 0, 'contains': ['large', 'latte'], 'not_contains': ['close', 'closing']}]}
        row = {'reply_to': None, 'clarification': None,
               'rephrased_sentence': 'Use the Large variant for the pending Latte.'}
        parsed = {'classifications': [row], 'declared_constraints': []}
        self.assertEqual(validate_output(case, parsed), [])
        row['rephrased_sentence'] += ' What time does the cafe close?'
        self.assertIn('English rewrite contains another unit (unit 0)', validate_output(case, parsed))

    def test_address_expectation_rejects_missing_question_and_premature_selection(self):
        from copy import deepcopy
        from scripts.evaluate_contextual import validate_output
        cases = json.loads((Path(__file__).parents[1] / 'fixtures/basket_arbitration.json').read_text())
        case = next(c for c in cases if c['id'] == 'address-choice-independent-of-complete-add')
        parsed = {'declared_constraints': [], 'classifications': deepcopy(case['expected_units'])}
        parsed['classifications'][0]['sub_intent'] = 'add_to_basket'
        row = parsed['classifications'][1]
        row['clarification'] = 'Which address should I use: Home or Office?'
        self.assertEqual(validate_output(case, parsed), [])
        for question in (None, 'Home and Office.', 'Could you clarify?'):
            with self.subTest(question=question):
                row['clarification'] = question
                self.assertTrue(validate_output(case, parsed))
        row['clarification'] = 'Which address should I use: Home or Office?'
        row['action'] = {'kind': 'SELECT_ADDRESS', 'reference': {'by': 'name', 'value': 'Home'}}
        self.assertIn('typed decisions', validate_output(case, parsed))

    def test_ambiguous_expectation_requires_both_readings_without_action(self):
        from copy import deepcopy
        from scripts.evaluate_contextual import validate_output
        cases = json.loads((Path(__file__).parents[1] / 'fixtures/basket_arbitration.json').read_text())
        case = next(c for c in cases if c['id'] == 'ambiguous-quantity-or-map-offer')
        parsed = {'declared_constraints': [], 'classifications': deepcopy(case['expected_units'])}
        row = parsed['classifications'][0]
        for question in ('Two Pistachio Ice Cream or the map link?', '2 pista ice creams or MAP link?'):
            row['clarification'] = question
            self.assertEqual(validate_output(case, parsed), [])
        for question in ('Could you clarify?', 'Two Pistachio Ice Cream?', 'Send the map link?',
                         'Would you like pista or the map link?'):
            with self.subTest(question=question):
                row['clarification'] = question
                self.assertTrue(validate_output(case, parsed))
        row['clarification'] = 'Two Pistachio Ice Cream or the map link?'
        row['action'] = {'kind': 'CHANGE_BASKET'}
        self.assertIn('typed decisions', validate_output(case, parsed))


class CombinedClassificationTests(ProviderHarness, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.finish_reason = "stop"
        self.refusal = None
        self.schema = ClassificationSchema(version=1)
        self.schema.update({"general": {"greeting": "Greet"}})
        self.schema_lookup = self.enterContext(patch.object(
            combined, "get_intent_classification_cache", return_value=self.schema))
        self.payload = {"declared_constraints": [], "classifications": [
            {"query": "Hello!", "rephrased_sentence": "Hello!", "intent": "general", "sub_intent": "greeting", "reply_to": None, "clarification": None},
        ]}

    def respond(self, request):
        response = super().respond(request)
        if response.status_code == 200:
            data = json.loads(response.content)
            data["choices"][0]["finish_reason"] = self.finish_reason
            data["choices"][0]["message"]["refusal"] = self.refusal
            data["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            return httpx.Response(200, json=data)
        return response

    def call(self, message="helo", previous_question="", previous_user="", **kwargs):
        return classification_rows(combined.normalize_and_classify(
            message, previous_question, previous_user, tenant_key=kwargs.pop("tenant_key", "1"), **kwargs))

    def test_one_structured_invocation_then_exact_cache_hit(self):
        for _ in range(2):
            self.assertEqual(self.call(), [("Hello!", "general", "greeting", None, None)])
        self.assertEqual(len(self.requests), 1)
        self.factory.assert_called_once_with(model="gpt-6-luna")
        request = self.requests[0]
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        system = request["messages"][0]["content"]
        self.assertTrue(system.startswith(combined.SYSTEM_PROMPT))
        schema = json.loads(system[len(combined.SYSTEM_PROMPT):])
        self.assertEqual(schema["general"]["greeting"], "Greet")
        self.assertEqual({(intent, topic) for intent, topics in schema.items() for topic in topics},
                         CONTROL_ROUTES | {("general", "greeting")})
        self.assertEqual(self.schema, {"general": {"greeting": "Greet"}})
        self.assertEqual(json.loads(request["messages"][1]["content"]), {
            "new_user_message": "helo", "prev_system_message": "", "prev_user_sentence": "",
        })

    def test_fractional_quantity_and_reply_script_survive_provider_and_cache(self):
        from tests.unit.test_action_resolver import add_line
        action = add_line('coffee')
        action.basket.lines[0].quantity = 1.5
        self.schema['placing_order'] = {'add_to_basket': 'Add menu units'}
        self.payload = {'declared_constraints': [], 'response_language': 'hi-Latn', 'classifications': [{
            'query': '1.5 Coffee add karo', 'rephrased_sentence': 'Add 1.5 Coffee', 'intent': 'placing_order', 'sub_intent': 'add_to_basket',
            'reply_to': None, 'clarification': None, 'action': action.model_dump()}]}
        for _ in range(2):
            result = combined.normalize_and_classify('1.5 Coffee add karo', tenant_key='1')
            self.assertEqual(result.response_language, 'hi-Latn')
            self.assertEqual(result.classifications[0].rephrased_sentence, 'Add 1.5 Coffee')
            self.assertEqual(result.classifications[0].query, '1.5 Coffee add karo')
            self.assertEqual(result.classifications[0].action.basket.lines[0].quantity, 1.5)
        self.assertEqual(len(self.requests), 1)

    def test_cache_isolates_tenant_context_model_message_prompt_and_schema_versions(self):
        self.call()
        for kwargs in ({"tenant_key": "2"}, {"previous_question": "Which size?"},
                       {"previous_user": "Add coffee"}, {"model": "another-model"},
                       {"message": "Helo"}):
            self.call(**kwargs)
        with patch.object(combined, "SYSTEM_PROMPT", combined.SYSTEM_PROMPT + "\n"):
            self.call()
        self.schema.version = 2
        self.call()
        self.schema["general"]["greeting"] = "Updated tenant instruction"
        self.call()
        self.assertEqual(len(self.requests), 9)
        self.call()
        self.assertEqual(len(self.requests), 9)
        self.schema_lookup.assert_called_with("1")

    def test_completed_context_reaches_provider_and_is_part_of_cache_identity(self):
        context = {'has_placed_order': True, 'last_completed_request': {
            'intent_type': 'order_enquiry', 'sub_intent': 'order_status_tracking',
            'main_query': 'Order ABC123 status', 'response': 'Order ABC123 is preparing.',
        }}
        self.call(message='Change that', conversation_context=context)
        self.assertEqual(json.loads(self.requests[-1]['messages'][1]['content'])['conversation_context'], context)
        self.call(message='Change that', conversation_context=context)
        self.assertEqual(len(self.requests), 1)
        self.call(message='Change that')
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('conversation_context', json.loads(self.requests[-1]['messages'][1]['content']))

    def test_tenant_schema_rejects_another_tenants_route(self):
        self.call(tenant_key="1")
        self.schema_lookup.return_value = {"general": {"thanks": "Thank"}}
        with self.assertLogs(combined.logger, "ERROR"), self.assertRaises(combined.NormalizationClassificationError):
            self.call(tenant_key="2")
        self.assertEqual(len(self.requests), 2)

    def test_missing_or_invalid_english_rewrite_rejects_entire_turn(self):
        good = self.payload['classifications'][0]
        missing = {key: value for key, value in good.items() if key != 'rephrased_sentence'}
        for invalid in (missing, *({**good, 'rephrased_sentence': value} for value in (None, '', ' \n', 2))):
            self.payload = {'declared_constraints': [], 'classifications': [good, invalid]}
            with self.subTest(invalid=invalid), patch.object(combined.cache, 'set') as save:
                with self.assertLogs(combined.logger, 'ERROR'), self.assertRaises(combined.NormalizationClassificationError):
                    self.call()
                save.assert_not_called()

    def test_old_cached_proposal_without_rewrite_is_not_reused(self):
        old = {'declared_constraints': [], 'classifications': [
            {key: value for key, value in self.payload['classifications'][0].items() if key != 'rephrased_sentence'}]}
        with patch.object(combined.cache, 'get', return_value=old), self.assertLogs(combined.logger, 'WARNING'):
            result = combined.normalize_and_classify('helo', tenant_key='1')
        self.assertEqual(result.classifications[0].rephrased_sentence, 'Hello!')
        self.assertEqual(len(self.requests), 1)

    def test_requires_tenant_identity_before_provider_or_cache(self):
        for tenant in (None, "", " "):
            with self.assertRaises(ValueError):
                self.call(tenant_key=tenant)
        self.factory.assert_not_called()

    def test_all_rows_validate_before_any_cache_write(self):
        good = self.payload["classifications"][0]
        invalid = [
            {"declared_constraints": [], "classifications": []}, {"declared_constraints": [], "classifications": [good, {**good, "query": " \n"}]},
            {"declared_constraints": [], "classifications": [good, {**good, "intent": "invented"}]},
            {"declared_constraints": [], "classifications": [{**good, "sub_intent": "thanks"}]},
            {"declared_constraints": [], "classifications": [{**good, "query": 5}]},
            {"declared_constraints": [], "classifications": [{**good, "extra": True}]},
            {"declared_constraints": [], "classifications": [good], "extra": True},
            {"declared_constraints": [], "classifications": [{"query": "Hello!"}]}, "{truncated",
            {"declared_constraints": [], "classifications": [{**good, "intent": "insufficient_information", "sub_intent": "greeting", "reply_to": None, "clarification": None}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(combined.cache, "set") as save:
                self.payload = payload
                with self.assertLogs(combined.logger, "ERROR"), self.assertRaises(combined.NormalizationClassificationError):
                    self.call()
                save.assert_not_called()
        self.payload = {"declared_constraints": [], "classifications": [good]}
        self.assertEqual(self.call()[0][0], "Hello!")
        self.assertEqual(len(self.requests), len(invalid) + 1)

    def test_provider_refusal_truncation_filter_and_error_are_not_cached(self):
        for status, finish, refusal in [(500, "stop", None), (200, "length", None),
                                        (200, "content_filter", None), (200, "stop", "Cannot comply")]:
            with self.subTest(status=status, finish=finish, refusal=refusal):
                self.status, self.finish_reason, self.refusal = status, finish, refusal
                with patch.object(combined.cache, "set") as save, self.assertLogs(combined.logger, "ERROR"), \
                        self.assertRaises(combined.NormalizationClassificationError):
                    self.call()
                save.assert_not_called()
        self.status, self.finish_reason, self.refusal = 200, "stop", None
        self.call()
        self.assertEqual(len(self.requests), 5)

    def test_control_routes_are_supplied_and_accepted_without_tenant_documents(self):
        for intent, topic in sorted(CONTROL_ROUTES):
            with self.subTest(intent=intent, topic=topic):
                self.schema_lookup.return_value = ClassificationSchema(version=7)
                self.payload = {"declared_constraints": [], "classifications": [{"query": "reply", "rephrased_sentence": "Reply", "intent": intent, "sub_intent": topic, "reply_to": None, "clarification": None}]}
                count = len(self.requests)
                for _ in range(2):
                    self.assertEqual(self.call(message=topic), [("reply", intent, topic, None, None)])
                self.assertEqual(len(self.requests) - count, 1)
                system = self.requests[-1]["messages"][0]["content"]
                schema = json.loads(system[len(combined.SYSTEM_PROMPT):])
                self.assertIn(topic, schema[intent])
                self.assertEqual(self.schema_lookup.return_value, {})
                self.assertEqual(self.schema_lookup.return_value.version, 7)

    def test_existing_control_descriptions_survive_even_when_marked_disabled(self):
        docs = [{"dtype": "intent_classification", "intent": intent, "sub_intent": topic,
                 "payload": {"description": "Tenant control wording", "enabled": False}}
                for intent, topic in CONTROL_ROUTES]
        configuration = RuntimeConfiguration("1", "key", "cafe", 8, docs)
        with patch("chatbot_core.runtime_configuration.get_configuration", return_value=configuration):
            self.schema_lookup.return_value = get_intent_classification_cache("1")
        for intent, topic in sorted(CONTROL_ROUTES):
            self.payload = {"declared_constraints": [], "classifications": [{"query": "reply", "rephrased_sentence": "Reply", "intent": intent, "sub_intent": topic, "reply_to": None, "clarification": None}]}
            self.assertEqual(self.call(message=topic), [("reply", intent, topic, None, None)])
            system = self.requests[-1]["messages"][0]["content"]
            schema = json.loads(system[len(combined.SYSTEM_PROMPT):])
            self.assertEqual(schema[intent][topic]["description"], "Tenant control wording")

    def test_invalid_cache_entry_is_replaced_after_fresh_validation(self):
        with patch.object(combined.cache, "get", return_value={"declared_constraints": [], "classifications": []}), \
                self.assertLogs(combined.logger, "WARNING"):
            self.call()
        self.assertEqual(len(self.requests), 1)

    def test_timeout_is_not_cached_and_next_turn_can_retry(self):
        with patch.object(self.client, "send", side_effect=httpx.ReadTimeout("offline timeout")), \
                patch.object(combined.cache, "set") as save, self.assertLogs(combined.logger, "ERROR"), \
                self.assertRaises(combined.NormalizationClassificationError):
            self.call()
        save.assert_not_called()
        self.assertEqual(self.call()[0][0], "Hello!")

    def test_cache_outage_does_not_repeat_valid_provider_invocation(self):
        with patch.object(combined.cache, "get", side_effect=OSError), \
                patch.object(combined.cache, "set", side_effect=OSError), self.assertLogs(combined.logger, "WARNING"):
            self.assertEqual(self.call()[0][0], "Hello!")
        self.assertEqual(len(self.requests), 1)

    def test_historical_outputs_remain_readable_without_inventing_english_rewrites(self):
        from chatbot_core.llm.schemas import ClassifiedMessages
        # Historical provider evidence predates the required English field.
        # Read it as history, never relabel it as a fresh v17 provider response.
        for case in FIXTURE["cases"]:
            with self.subTest(source=case["source"], case=case["id"]):
                proposal = ClassifiedMessages.model_validate(case["response"])
                rows = classification_rows(proposal)
                self.assertEqual(rows, [(row["query"], row["intent"], row["sub_intent"], row["reply_to"], row["clarification"])
                                        for row in case["response"]["classifications"]])
                self.assertTrue(all(row.rephrased_sentence is None for row in proposal.classifications))
                for literal in case["preserve"]:
                    self.assertIn(literal, "\n".join(row[0] for row in rows))
        self.assertEqual(self.requests, [])

    @override_settings(EVALUATION_ENABLED=True)
    def test_evaluation_cold_warm_fault_and_usage_attribution(self):
        self.factory.return_value = ChatOpenAI(
            model="gpt-4.1-mini", api_key="offline-tests-only", http_client=self.client,
            max_retries=0, callbacks=[callback], include_response_headers=True, cache=False,
        )
        with self.assertLogs("evaluate.telemetry", "INFO") as logs:
            with activate(context()):
                self.call()
                self.call()
            with activate(context(cache_mode="warm")):
                self.call()
                self.call()
            with activate(context(cache_mode="warm", faults=frozenset({"classification"}))), \
                    self.assertRaises(combined.NormalizationClassificationError):
                self.call()
            with activate(replace(context(cache_mode="warm"), lease_id="other")):
                self.call()
        self.assertEqual(len(self.requests), 4)
        events = [r.evaluation for r in logs.records]
        completed = [e for e in events if e["event"] == "llm.completed"]
        self.assertEqual(len(completed), 4)
        self.assertTrue(all(e["total_tokens"] == 15 and e["tenant_id"] == "1" for e in completed))
        self.assertTrue(any(e["event"] == "fault.injected" for e in events))

    def test_pending_id_must_belong_to_supplied_open_requests(self):
        row = self.payload['classifications'][0]
        row['reply_to'] = 'another-session'
        with patch.object(combined.cache, 'set') as save:
            with self.assertLogs(combined.logger, 'ERROR'), self.assertRaises(combined.NormalizationClassificationError):
                self.call(conversation_context={'open_requests':[{'id':'pending-17'}]})
            save.assert_not_called()
        row['reply_to'] = 'pending-17'
        self.assertEqual(self.call(conversation_context={'open_requests':[{'id':'pending-17'}]})[0][3], 'pending-17')

    def test_missing_routing_fields_and_late_invalid_id_reject_entire_turn(self):
        good = self.payload['classifications'][0].copy()
        for invalid in ({key:value for key,value in good.items() if key != 'clarification'},
                        {**good,'reply_to':'foreign'}, {**good,'clarification':' '}):
            self.payload={'declared_constraints': [], 'classifications':[good,invalid]}
            with patch.object(combined.cache,'set') as save:
                with self.assertLogs(combined.logger,'ERROR'), self.assertRaises(combined.NormalizationClassificationError):
                    self.call()
                save.assert_not_called()

    def test_clarification_preserves_known_route_and_original_provider_text(self):
        self.payload['classifications'][0].update(query='Change the item quantity to two', clarification='Which item?')
        rows = self.call(message='make it two')
        self.assertEqual(rows[0][4], 'Which item?')
        self.assertEqual(json.loads(self.requests[-1]['messages'][1]['content'])['new_user_message'],'make it two')

    def test_declared_requirements_survive_exact_cache_and_must_validate(self):
        self.payload['declared_constraints'] = ['I have a milk allergy.']
        for _ in range(2):
            result = combined.normalize_and_classify('I have a milk allergy.', tenant_key='1')
            self.assertEqual(result.declared_constraints, ['I have a milk allergy.'])
        self.assertEqual(len(self.requests), 1)
        for requirements in (None, 'vegan', [''], ['   '], [1]):
            self.payload['declared_constraints'] = requirements
            with self.subTest(requirements=requirements), patch.object(combined.cache, 'set') as save:
                with self.assertLogs(combined.logger, 'ERROR'), self.assertRaises(combined.NormalizationClassificationError):
                    self.call(message='uncached declaration')
                save.assert_not_called()
