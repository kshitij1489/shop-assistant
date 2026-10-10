"""Description fixture contracts; no model calls or scenario execution."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.intent_definitions import STANDARD_INTENTS
from evaluate.datasets.loader import classification_documents, DatasetError
from tests.support.paths import REPOSITORY_ROOT


class IntentFixtureTests(SimpleTestCase):
    def test_fixture_covers_every_registered_route(self):
        documents = classification_documents(REPOSITORY_ROOT / 'test_data')
        self.assertEqual({(doc['intent'], doc['sub_intent']) for doc in documents},
                         {(intent, topic) for intent, cap in CAPABILITIES.items()
                          for topic in cap.sub_intents})
        self.assertTrue(all(doc['payload']['enabled'] for doc in documents))
        self.assertEqual({(intent, topic) for intent, topics in STANDARD_INTENTS.items() for topic in topics},
                         {(intent, topic) for intent, cap in CAPABILITIES.items() for topic in cap.sub_intents})

    def test_selection_preserves_descriptions_examples_and_enabled_flag(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {'description': 'Ask about pets.', 'examples': ['Dogs allowed?'], 'enabled': False}
            (root / 'intent_classification.json').write_text(json.dumps({
                'information_about_the_cafe': {'pet_policy': payload},
                'general': {'greeting': 'A greeting.'}}))
            docs = classification_documents(root, {('information_about_the_cafe', 'pet_policy')})
            self.assertEqual(docs, [{'dtype': 'intent_classification',
                'intent': 'information_about_the_cafe', 'sub_intent': 'pet_policy', 'payload': payload}])
            with self.assertRaisesRegex(DatasetError, 'missing classification descriptions'):
                classification_documents(root, {('general', 'thanks')})

    def test_invalid_or_obsolete_descriptions_fail_without_label_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for source in (
                {'location_based': {'read_addresses': 'Read addresses.'}},
                {'general': {'greeting': ''}},
                {'general': {'greeting': {'description': 'Greeting', 'examples': [3]}}},
                {'general': {'greeting': {'description': 'Greeting', 'enabled': 'false'}}},
            ):
                with self.subTest(source=source):
                    (root / 'intent_classification.json').write_text(json.dumps(source))
                    with self.assertRaises(DatasetError):
                        classification_documents(root)
