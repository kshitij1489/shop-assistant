from django.test import SimpleTestCase
from chatbot_core.recording.capture import snapshot
from chatbot_core.recording.replay_codec import CodecError, Encoder, decode, encode
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import get_intent
from chatbot_core.logic.outcomes import TaskOutcome


class ReplayCodecTests(SimpleTestCase):
    def test_shared_intent_basket_and_turn_local_handoff_survive(self):
        intent = get_intent('general')(main_query='hello', sub_intent='greeting', tenant=1, chat_id='chat')
        intent.handoff_to = 'placing_order'
        intent.handoff_overrides = {'sub_intent': 'check_order_cart'}
        intent.outcome = TaskOutcome.NEEDS_CLARIFICATION
        basket = Basket()
        restored = decode(encode({'pending': [intent], 'previous': intent, 'question': intent,
                                  'basket': basket, 'also_basket': basket}))
        self.assertIs(restored['pending'][0], restored['previous'])
        self.assertIs(restored['previous'], restored['question'])
        self.assertIs(restored['basket'], restored['also_basket'])
        self.assertEqual(restored['previous'].handoff_to, 'placing_order')
        self.assertEqual(restored['previous'].outcome, TaskOutcome.NEEDS_CLARIFICATION)

    def test_secret_subtrees_are_not_retained_as_orphan_nodes(self):
        import json
        document = encode({'headers': {'anything': 'hidden-credential'}, 'api_key': 'private-key',
                           'chat_id': 'chat', 'text': 'private-key in text'})
        redacted = snapshot(document, secrets=('private-key', 'chat'))
        encoded = json.dumps(redacted)
        self.assertNotIn('hidden-credential', encoded)
        self.assertNotIn('private-key', encoded)
        restored = decode(redacted)
        self.assertEqual(restored['chat_id'], '[REDACTED]')
        self.assertEqual(restored['headers'], '[REDACTED]')

    def test_codec_rejects_unknown_objects_and_types(self):
        with self.assertRaises(CodecError):
            encode(object())
        with self.assertRaises(CodecError):
            decode({'version': 99})
        document = encode([])
        document['nodes']['0']['kind'] = 'os.system'
        with self.assertRaises(CodecError):
            decode(document)

    def test_reapplying_mutations_preserves_existing_tuple_and_list_aliases(self):
        contents = []
        pair = (contents,)
        before = {'args': (pair,), 'kwargs': {}}
        encoder = Encoder()
        encoded = encoder.encode(before)
        contents.append('new')
        after = encoder.encode({'args': before['args'], 'kwargs': before['kwargs'], 'result': pair})
        restored = decode(encoded)
        replay_encoder = Encoder()
        replay_encoder.encode(restored)
        old_pair = restored['args'][0]
        result = decode(after, existing=replay_encoder.objects)
        self.assertIs(result['result'], old_pair)
        self.assertEqual(old_pair[0], ['new'])
