from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
from chatbot_core.logic.cafe.workflow.order_context import classification_context
from chatbot_core.logic.cafe.prompts.normalize_and_classify import NormalizationClassificationError


class OrderContextTests(SimpleTestCase):
    def context(self, **kwargs):
        return classification_context([], {}, tenant_id=1, chat_id='user', platform='website', **kwargs)

    def test_pending_budget_keeps_active_and_explicitly_named_older_request(self):
        pending = [PlacingOrderIntent(main_query=f'Product {i}', sub_intent='add_to_basket',
                   tenant=1, chat_id='user', query_id=str(i)) for i in range(20)]
        for request in pending:
            request.platform = 'website'
        context = self.context(pending=pending, active_pending=pending[0], query='Product 1')
        self.assertEqual(len(context['open_requests']), 12)
        self.assertEqual(context['other_open_request_count'], 8)
        self.assertEqual(context['active_pending_id'], '0')
        self.assertIn('1', [row['id'] for row in context['open_requests']])
        pending[2].tenant = 2
        with self.assertRaises(ValueError):
            self.context(pending=pending)

    def test_oversized_context_fails_instead_of_truncating_conditions(self):
        with self.assertRaises(NormalizationClassificationError):
            classification_context([], {'last_assistant_question': 'x' * 64001},
                                   tenant_id=1, chat_id='user', platform='website')

    def test_pending_identity_original_and_missing_fields_survive_reload(self):
        pending = PlacingOrderIntent(main_query='Add latte', sub_intent='add_to_basket', tenant=1,
                                     chat_id='user', query_id='pending-17', follow_up_question=['Which size?'])
        pending.platform = 'website'
        pending.original_query = 'that coffee please'
        pending.missing_fields = ['variant']
        restored = pending.from_dict(pending.to_dict())
        context = self.context(pending=[restored])
        self.assertEqual(context['open_requests'][0]['id'], 'pending-17')
        self.assertEqual(context['open_requests'][0]['original_message'], 'that coffee please')
        self.assertEqual(context['open_requests'][0]['missing_fields'], ['variant'])
        restored.chat_id = 'another'
        with self.assertRaises(ValueError):
            self.context(pending=[restored])

    def test_large_catalog_keeps_basket_pending_recommendations_and_changes_identity(self):
        catalog = {str(i): {'item_id':str(i), 'name':f'Product {i}', 'variants':[], 'modifier_groups':[]}
                   for i in range(90)}
        basket = Basket()
        basket.items = [{'item_id':'80', 'name':'Product 80', 'quantity':2, 'unit_price':'10'}]
        tenant = SimpleNamespace(api_key='store')
        with patch('chatbot_core.logic.cafe.catalog.load_catalog', return_value=catalog):
            context = classification_context([], {'recommended_item_ids':['89','foreign']}, tenant_id=1,
                chat_id='user', platform='website', tenant=tenant, basket=basket)
            self.assertLessEqual(len(context['catalog']),27)
            self.assertTrue({'80','89'} <= {row['item_id'] for row in context['catalog']})
            self.assertEqual(context['recommendations'],[{'item_id':'89','name':'Product 89'}])
            self.assertNotIn('unit_price',context['basket'][0])
            catalog['85']['name']='Changed outside candidates'
            changed=self.context(tenant=tenant)
            self.assertNotEqual(context['catalog_version'],changed['catalog_version'])

    def test_delivered_question_and_exchange_are_scoped(self):
        row={'query_obj':{'tenant':1,'chat_id':'user','platform':'website','is_complete':True},
             'exchange':{'user':'hours?','assistant':'We close at six.'}}
        context=classification_context([row], {'last_assistant_question':'Which size?'}, tenant_id=1,
                                       chat_id='user',platform='website')
        self.assertEqual(context['recent_exchange'],[row['exchange']])
        self.assertEqual(context['last_assistant_question'],'Which size?')
        row['query_obj']['tenant']=2
        self.assertFalse(classification_context([row],{},tenant_id=1,chat_id='user',platform='website')['recent_exchange'])
