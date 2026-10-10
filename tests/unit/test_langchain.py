"""Exercise real LangChain parsing through an offline HTTP transport."""
import importlib
import json

from unittest.mock import patch

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from langchain_openai import ChatOpenAI

from chatbot_core import language_utils
from chatbot_core.llm import models
from chatbot_core.llm.chains import structured_chain, text_chain
from chatbot_core.llm.schemas import AddressComponents, FollowupDecision
from chatbot_core.logic.cafe import prompt_builder
from chatbot_core.logic.cafe.prompts import rephrase
from users.analytics import prompt_builder as analytics


from tests.support.llm import ProviderHarness


class OperationTests(ProviderHarness, SimpleTestCase):
    def test_structured_output_is_a_typed_boolean(self):
        result = structured_chain(FollowupDecision, 'Return {"is_followup": true}').invoke({"input": "yes {please}"})
        self.assertIs(result.is_followup, True)
        self.assertEqual(self.requests[0]["response_format"]["type"], "json_schema")
        self.assertTrue(self.requests[0]["response_format"]["json_schema"]["strict"])
        self.assertEqual(self.requests[0]["messages"][0]["content"], 'Return {"is_followup": true}')
        self.assertEqual(self.requests[0]["messages"][1]["content"], "yes {please}")


    def test_address_preserves_free_form_street(self):
        self.payload = {key: None for key in AddressComponents.model_fields}
        self.payload.update(street_address="Flat 4, Merlin", city="Gurugram", postal_code="012001")
        result = prompt_builder.extract_address_with_gpt("Flat 4, Merlin, Gurugram, 012001")
        self.assertEqual(result["street_address"], "Flat 4, Merlin")
        self.assertEqual(result["postal_code"], "012001")
        self.assertNotIn("state", result)

    def test_empty_address_delta_is_distinct_from_extraction_failure(self):
        self.payload = {key: None for key in AddressComponents.model_fields}
        self.assertEqual(prompt_builder.extract_address_with_gpt("Confirm Flat 4, Delhi", original_text="yes"), {})
        self.status = 500
        with self.assertLogs(prompt_builder.logger, "ERROR"):
            self.assertIsNone(prompt_builder.extract_address_with_gpt("Confirm Flat 4, Delhi", original_text="yes"))

    def test_address_interpretation_keeps_original_values_and_pending_context(self):
        self.payload = {key: None for key in AddressComponents.model_fields}
        self.payload.update(street_address='टावर C, 22 DLF phase 2', city='gurugram',
                            state='Haryana', country='India', postal_code='122002')
        original = 'mil gaya, 22 DLF phase 2, gurugram, 122002 State: Haryana. Country: India.'
        rewrite = 'Complete the delivery address with the supplied street and postal fields'
        result = prompt_builder.extract_address_with_gpt(original, original_text=original,
            pending={'street_address': 'टावर C'}, rephrased_sentence=rewrite)
        prompt = self.requests[-1]['messages'][1]['content']
        for value in (original, rewrite, 'टावर C'):
            self.assertIn(value, prompt)
        self.assertEqual(result, self.payload)

    def test_text_chain_preserves_literal_input(self):
        self.payload = "Welcome!"
        self.assertEqual(text_chain("Keep {placeholders}", "Rewrite: {message}").invoke({"message": "Hello {name}"}), "Welcome!")
        self.assertEqual(self.requests[0]["messages"][1]["content"], "Rewrite: Hello {name}")

    def test_rephrase_exact_hit_skips_model(self):
        self.payload = "Welcome back!"
        self.assertEqual(rephrase.rephrase_cafe_message("Welcome"), "Welcome back!")
        self.assertEqual(rephrase.rephrase_cafe_message("Welcome"), "Welcome back!")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.factory.call_count, 1)

    def test_rephrase_failure_is_not_cached(self):
        self.status = 500
        with self.assertLogs(rephrase.logger, level="ERROR"):
            self.assertEqual(rephrase.rephrase_cafe_message("Welcome"), "Welcome")
        self.status, self.payload = 200, "Welcome back!"
        self.assertEqual(rephrase.rephrase_cafe_message("Welcome"), "Welcome back!")
        self.assertEqual(len(self.requests), 2)

    def test_sql_proposal_preserves_parameter_types_and_shape(self):
        self.payload = {
            "query": {"sql": "SELECT id FROM orders_order WHERE id = %s", "params": [42], "columns": ["id"]},
            "explanation": "Find order", "safety": {"is_safe": True, "warnings": []},
        }
        result = analytics._call_model_for_sql("order 42 {literal}", analytics.SCHEMA_WHITELIST)
        self.assertEqual(result, self.payload)
        self.assertIs(type(result["query"]["params"][0]), int)

    def test_sql_validation_still_rejects_mutation(self):
        self.payload = {
            "query": {"sql": "DELETE FROM orders_order", "params": [], "columns": []},
            "explanation": "Delete", "safety": {"is_safe": True, "warnings": []},
        }
        result = analytics.create_db_query("delete orders", "tenant-1")
        self.assertFalse(result["safety"]["is_safe"])
        self.assertIsNone(result["sql"])

    def test_invalid_sql_output_uses_domain_error(self):
        self.payload = {"query": {"sql": "SELECT 1"}}
        with self.assertRaises(analytics.QueryGenerationError):
            analytics._call_model_for_sql("hello", {})


class TranslationTests(ProviderHarness, SimpleTestCase):
    def test_local_detection_skips_model(self):
        with patch.object(language_utils, "_cheap_local_lang_guess", return_value=("en", 0.99)):
            self.assertEqual(language_utils.translate_with_detection("hello"), ("hello", "en", 0.99))
        self.factory.assert_not_called()

    def test_detect_translate_returns_existing_tuple_and_zero_confidence(self):
        self.payload = {"source_lang": "hi", "confidence": 0.0, "translated": "hello 123"}
        with patch.object(language_utils, "_cheap_local_lang_guess", return_value=("hi", 0.5)):
            self.assertEqual(language_utils.translate_with_detection("namaste 123"), ("hello 123", "hi", 0.0))
        self.assertEqual(self.factory.call_args.kwargs["task"], "translation")

    def test_invalid_confidence_uses_translation_fallback(self):
        self.payload = {"source_lang": "hi", "confidence": 1.5, "translated": "bad"}
        with patch.object(language_utils, "_cheap_local_lang_guess", return_value=("hi", 0.5)), \
             patch.object(language_utils, "translate", return_value="hello") as fallback, \
             self.assertLogs(language_utils.log, level="ERROR"):
            self.assertEqual(language_utils.translate_with_detection("namaste"), ("hello", "hi", 0.5))
        fallback.assert_called_once_with("namaste", source_lang="hi", target_lang="en")

    def test_plain_translation_returns_string(self):
        self.payload = "hello 123"
        self.assertEqual(language_utils.translate("namaste 123", "hi", "en"), "hello 123")


class CacheTests(ProviderHarness, SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.answers = importlib.import_module("chatbot_core.logic.cafe.prompts.answer_from_knowledge")
        cls.knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.answers, "enqueue_string"))
        self.enterContext(patch.object(self.knowledge, "enqueue_string"))

    def test_knowledge_hit_bypasses_chain(self):
        with patch.object(self.answers, "kb_lookup", return_value=(True, "Cached", {})):
            self.assertEqual(self.answers.answer_from_knowledge("knowledge", "question", tenant_key="tenant-1"), "Cached")
        self.factory.assert_not_called()

    def test_knowledge_miss_saves_text_result(self):
        self.payload = "The café opens at nine."
        with patch.object(self.answers, "kb_lookup", return_value=(False, None, {"sig": "key"})), \
             patch.object(self.answers, "kb_create", side_effect=lambda ctx, answer, ttl, model: answer) as store:
            self.assertEqual(self.answers.answer_from_knowledge({"hours": "9"}, "When?", tenant_key="tenant-1"), self.payload)
        self.assertEqual(store.call_args.args[1], self.payload)

    def test_menu_cache_accounts_for_model_prompt_and_context(self):
        args = ("tenant", "menu", "What is available?", {"payload": ["latte"]}, False)
        keys = {
            self.knowledge._kb_sig(*args),
            self.knowledge._kb_sig(*args, model="different-model"),
            self.knowledge._kb_sig(*args, prompt_info={"payload": "New instructions"}),
            self.knowledge._kb_sig(*args, system_log_message="Different context"),
        }
        self.assertEqual(len(keys), 4)

    def test_menu_cache_bypasses_model_after_first_response(self):
        self.payload = "We have lattes."
        with patch.object(self.knowledge, "get_knowledge_base_cache", return_value={("tenant", "general", "menu"): {"payload": ["latte"]}}), \
             patch.object(self.knowledge, "get_intent_prompt_cache", return_value={}):
            for _ in range(2):
                self.assertEqual(self.knowledge.generate_response_from_knowledge("tenant", "menu", "What is available?"), self.payload)
        self.assertEqual(len(self.requests), 1)

    @override_settings(SEMANTIC_CACHE_ENABLED=True)
    def test_live_cafe_knowledge_uses_scoped_semantic_service(self):
        from chatbot_core.vector_store import semantic_cache
        self.payload = "Pets are welcome outside."
        args = ("tenant", "amenities", "Are pets welcome?")
        with patch.object(self.knowledge, "retrieve_knowledge", return_value={"payload": {"pets": "outside"}, "identity": "v1"}), \
                patch.object(self.knowledge, "get_intent_prompt_cache", return_value={}), \
                patch.object(semantic_cache, "lookup", return_value=(False, None, {"scope": "prepared"})) as lookup, \
                patch.object(semantic_cache, "store", side_effect=lambda ctx, answer, ttl, model: answer) as store:
            self.assertEqual(self.knowledge.generate_response_from_knowledge(*args,
                main_intent="information_about_the_cafe", response_profile="cafe_information"), self.payload)
            self.assertEqual(store.call_args.args[1], self.payload)
            self.assertIn("system_prompt", lookup.call_args.args[2])
            lookup.return_value = (True, "Cached public facts", {})
            self.assertEqual(self.knowledge.generate_response_from_knowledge(*args,
                main_intent="information_about_the_cafe", response_profile="cafe_information"), "Cached public facts")
        self.assertEqual(len(self.requests), 1)

    @override_settings(SEMANTIC_CACHE_ENABLED=True)
    def test_menu_hours_and_contextual_answers_do_not_use_semantic_reuse(self):
        from chatbot_core.vector_store import semantic_cache
        self.payload = "Verified response"
        with patch.object(self.knowledge, "retrieve_knowledge", return_value={"payload": {"facts": "known"}}), \
                patch.object(self.knowledge, "get_intent_prompt_cache", return_value={}), \
                patch.object(semantic_cache, "lookup", side_effect=AssertionError("Unsafe admission")):
            for main, topic, previous in (("menu_items", "allergens", None),
                                          ("information_about_the_cafe", "location_and_hours", None),
                                          ("information_about_the_cafe", "amenities", "Earlier question")):
                self.assertEqual(self.knowledge.generate_response_from_knowledge("tenant", topic, "Question",
                    main_intent=main, previous_user_message=previous), self.payload)
        self.assertEqual(len(self.requests), 3)

    def test_exact_cache_outage_preserves_successful_answer(self):
        self.payload = "We have lattes."
        with patch.object(self.knowledge, "get_knowledge_base_cache", return_value={("tenant", "general", "menu"): {"payload": ["latte"]}}), \
                patch.object(self.knowledge, "get_intent_prompt_cache", return_value={}), \
                patch.object(self.knowledge.cache, "get", side_effect=ConnectionError), \
                patch.object(self.knowledge.cache, "set", side_effect=ConnectionError):
            with self.assertLogs(self.knowledge.logger, "WARNING"):
                self.assertEqual(self.knowledge.generate_response_from_knowledge("tenant", "menu", "What is available?"), self.payload)

    def test_knowledge_exact_cache_separates_tenant_user_and_platform(self):
        scopes = [("one", "user", "telegram"), ("two", "user", "telegram"),
                  ("one", "user", "whatsapp"), ("one", "other-user", "telegram")]
        with patch.object(self.answers, "kb_create", wraps=self.answers.kb_create) as store:
            for tenant, user, platform in scopes:
                self.payload = f"Answer for {tenant}/{user}/{platform}"
                for _ in range(2):
                    self.assertEqual(self.answers.answer_from_knowledge(
                        "same knowledge", "same question", tenant_key=tenant,
                        user_id=user, platform=platform,
                    ), self.payload)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(len({call.args[0]["scope"] for call in store.call_args_list}), 4)

    def test_disabled_semantic_cache_generates_without_encoder_or_database(self):
        self.payload = "Fresh answer"
        with override_settings(SEMANTIC_CACHE_ENABLED=False):
            self.assertEqual(self.answers.answer_from_knowledge(
                "knowledge", "question", tenant_key="one"), "Fresh answer")
        self.assertEqual(len(self.requests), 1)

    def test_unscoped_model_operations_fail_before_cache_or_provider_access(self):
        for kwargs in ({}, {"tenant_key": None}, {"tenant_key": ""}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    self.answers.answer_from_knowledge("knowledge", "question", **kwargs)
        with self.assertRaises(ValueError):
            self.answers.answer_from_knowledge("knowledge", "question", tenant_key="one", user_id="user")
        self.factory.assert_not_called()


class ModelConfigurationTests(SimpleTestCase):
    def test_luna_defaults_serialize_text_and_structured_requests(self):
        models._configured_model.cache_clear()
        self.addCleanup(models._configured_model.cache_clear)
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            content = '{"is_followup": true}' if "response_format" in body else "Hello"
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": content,
                }}],
            })

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with patch.object(models, "ChatOpenAI", side_effect=lambda **kwargs: ChatOpenAI(
                **kwargs, http_client=client,
            )):
                for task in ("cafe", "translation", "analytics"):
                    with self.subTest(task=task):
                        self.assertEqual(text_chain("Reply briefly.", task=task).invoke({"input": "Hi"}), "Hello")
                        result = structured_chain(FollowupDecision, "Classify.", task=task).invoke({"input": "Yes"})
                        self.assertTrue(result.is_followup)

        self.assertEqual(len(requests), 6)
        for body in requests:
            self.assertEqual(body["model"], "gpt-6-luna")
            self.assertEqual(body["reasoning_effort"], "none")
            self.assertEqual(body["temperature"], 0.0)

    @override_settings(LLM_MODEL="custom-cafe", LLM_TRANSLATE_MODEL="custom-translation", LLM_ANALYTICS_MODEL="custom-analytics",
                       LLM_TIMEOUT=7, LLM_MAX_RETRIES=1, LLM_MAX_TOKENS=300)
    def test_shared_models_use_settings_and_allow_overrides(self):
        models._configured_model.cache_clear()
        self.addCleanup(models._configured_model.cache_clear)
        first = models.get_chat_model()
        self.assertIs(first, models.get_chat_model())
        self.assertEqual(first.model_name, "custom-cafe")
        self.assertIsNone(first.reasoning_effort)
        self.assertEqual(first.request_timeout, 7)
        self.assertEqual(first.max_retries, 1)
        self.assertEqual(first.max_tokens, 300)
        self.assertEqual(models.get_chat_model(task="translation").model_name, "custom-translation")
        self.assertEqual(models.get_chat_model(task="analytics").model_name, "custom-analytics")
        explicit = models.get_chat_model(model="explicit", max_tokens=60, timeout=3)
        self.assertEqual((explicit.model_name, explicit.max_tokens, explicit.request_timeout), ("explicit", 60, 3))
