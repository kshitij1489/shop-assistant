"""Offline response composition and translation contracts, independent of task cleanup."""
from django.test import SimpleTestCase

from chatbot_core.llm.replies import join_replies
from chatbot_core.llm.streaming import final_reply, invoke_reply, reply_stream
from chatbot_core.logic.cafe.reply_language import localize_reply
from chatbot_core.logic.cafe.reply_renderer import render_reply
from chatbot_core.logic.cafe.workflow.graph import stream_original_reply
from tests.support.llm import ProviderHarness


class ReplyCompositionTests(SimpleTestCase):
    def test_join_preserves_punctuation_urls_decimals_and_multiline_text(self):
        self.assertEqual(join_replies(['Added 2 × Coffee.', '', 'Open Sunday!', 'Anything else?']),
                         'Added 2 × Coffee. Open Sunday! Anything else?')
        self.assertEqual(join_replies(['Total INR 10.50\nhttps://cafe.example/pay?id=123', 'Thank you.']),
                         'Total INR 10.50\nhttps://cafe.example/pay?id=123. Thank you.')
        self.assertEqual(join_replies(['नमस्ते।', 'Bonjour !']), 'नमस्ते। Bonjour !')

    def test_handler_wording_is_not_streamed_before_final_composition(self):
        for language in ('es', 'en'):
            with self.subTest(language=language):
                state = {'checklist': {'response_language': language}, 'intent_index': 0,
                         'classifications': [()]}
                self.assertFalse(stream_original_reply(state))


class ReplyRendererTests(ProviderHarness, SimpleTestCase):
    """Exercise the real composer independently of graph presentation stubs."""

    def render(self, response='Added 2 × Coffee at INR 100.00.', question=''):
        return render_reply(query='Two coffees', previous_message='', previous_question='',
                            facts=[{'verified_result': response, 'basket_changed': True}],
                            response=response, question=question, followup={}, language='es')

    def test_composes_verified_facts_in_one_presentation_call(self):
        import json
        self.payload = {'response': 'Añadido 2 × Coffee a INR 100.00.', 'question': ''}
        self.assertEqual(self.render(), (self.payload['response'], ''))
        self.assertEqual(len(self.requests), 1)
        context = json.loads(self.requests[0]['messages'][1]['content'])
        self.assertEqual(context['language'], 'es')
        self.assertEqual(context['verified_reply'], 'Added 2 × Coffee at INR 100.00.')
        self.assertTrue(context['workflow_results'][0]['basket_changed'])
        self.assertIsNone(context['permitted_followup'])

    def test_translates_selected_question_and_keeps_it_at_end_of_reply(self):
        question = 'How many Coffee would you like?'
        self.payload = {'response': '¿Cuántos Coffee quieres?', 'question': '¿Cuántos Coffee quieres?'}
        self.assertEqual(self.render(question, question),
                         (self.payload['response'], self.payload['question']))

    def test_invalid_composition_falls_back_without_retry(self):
        for response, question in (
                ('Added 3 × Coffee at INR 100.00.', ''),
                ('Added 2 × Coffee at INR 90.00.', ''),
                ('Added 2 × Coffee at INR 100.00. Name?', 'Name?')):
            with self.subTest(response=response):
                self.payload = {'response': response, 'question': question}
                before = len(self.requests)
                with self.assertLogs('chatbot_core.logic.cafe.reply_renderer', 'ERROR'):
                    self.assertEqual(self.render(), ('Added 2 × Coffee at INR 100.00.', ''))
                self.assertEqual(len(self.requests) - before, 1)

    def test_provider_failure_preserves_verified_reply_and_question(self):
        self.status = 500
        with self.assertLogs('chatbot_core.logic.cafe.reply_renderer', 'ERROR'):
            self.assertEqual(self.render('Which size?', 'Which size?'), ('Which size?', 'Which size?'))
        self.assertEqual(len(self.requests), 1)


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
