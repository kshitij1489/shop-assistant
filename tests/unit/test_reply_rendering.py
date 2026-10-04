"""Offline response composition and translation contracts, independent of task cleanup."""
from django.test import SimpleTestCase

from chatbot_core.llm.replies import join_replies
from chatbot_core.llm.streaming import final_reply, invoke_reply, reply_stream
from chatbot_core.logic.cafe.reply_language import localize_reply
from chatbot_core.logic.cafe.workflow.graph import stream_original_reply
from tests.support.llm import ProviderHarness


class ReplyCompositionTests(SimpleTestCase):
    def test_join_preserves_punctuation_urls_decimals_and_multiline_text(self):
        self.assertEqual(join_replies(['Added 2 × Coffee.', '', 'Open Sunday!', 'Anything else?']),
                         'Added 2 × Coffee. Open Sunday! Anything else?')
        self.assertEqual(join_replies(['Total INR 10.50\nhttps://cafe.example/pay?id=123', 'Thank you.']),
                         'Total INR 10.50\nhttps://cafe.example/pay?id=123. Thank you.')
        self.assertEqual(join_replies(['नमस्ते।', 'Bonjour !']), 'नमस्ते। Bonjour !')

    def test_localized_turn_does_not_stream_intermediate_english_replies(self):
        state = {'checklist': {'response_language': 'es'}, 'intent_index': 0, 'classifications': [()]}
        self.assertFalse(stream_original_reply(state))
        state['checklist']['response_language'] = 'en'
        self.assertTrue(stream_original_reply(state))


class ReplyLanguageTests(ProviderHarness, SimpleTestCase):
    def test_translate_complete_reply_and_delivered_question_together(self):
        self.payload = {'response': 'Añadido 2 × Coffee (Regular). ¿Algo más?', 'question': '¿Algo más?'}
        self.assertEqual(localize_reply('Added 2 × Coffee (Regular). Anything else?', 'Anything else?', 'es'),
                         (self.payload['response'], self.payload['question']))
        self.assertEqual(len(self.requests), 1)

    def test_english_is_a_noop(self):
        self.assertEqual(localize_reply('Added 2 × Coffee.', '', 'en'), ('Added 2 × Coffee.', ''))
        self.assertEqual(self.requests, [])

    def test_translation_cannot_change_prices_links_or_add_followup_questions(self):
        for response, question in [('Total INR 200.00 https://pay.example/42', ''),
                                   ('Total INR 100.00 https://pay.example/43', ''),
                                   ('Total INR 100.00 https://pay.example/42', 'Name?')]:
            self.payload = {'response': response, 'question': question}
            original = 'Total INR 100.00 https://pay.example/42'
            with self.subTest(response=response, question=question), self.assertLogs(level='ERROR'):
                self.assertEqual(localize_reply(original, '', 'fr'), (original, ''))

    def test_provider_failure_keeps_verified_business_response(self):
        self.status = 500
        with self.assertLogs(level='ERROR'):
            self.assertEqual(localize_reply('Added 2 × Coffee.', '', 'pt'), ('Added 2 × Coffee.', ''))

    def test_stream_prefix_uses_same_punctuation_as_final_reply(self):
        from unittest.mock import Mock
        chain = Mock()
        chain.stream.return_value = iter(['Open Sunday.'])
        events = []
        with reply_stream(lambda *event: events.append(event)), final_reply(True, ['Added 2 × Coffee.']):
            self.assertEqual(invoke_reply(chain, {}), 'Open Sunday.')
        self.assertEqual(events[0], ('replace', {'text': 'Added 2 × Coffee. '}))
