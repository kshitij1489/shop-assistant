"""Ordering fixtures, offline proposals, and deterministic evaluation limits."""
from copy import deepcopy
from unittest.mock import Mock, patch

from chatbot_core.logic.cafe import basket
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.catalog import load_catalog
from chatbot_core.logic.cafe.intent_handler import base, placing_order as placing
from chatbot_core.models import TenantInfo
from orders.models import ChatSession, Customer, MenuItem, MenuItemVariant
from commerce.models import Configuration, Location
from commerce.policy import Policy, evaluation_policy


def seed_evaluation_policy(tenant, *, commerce_enabled=False):
    """Attach schema v2 evaluation limits without turning them into a product default."""
    location, _ = Location.objects.get_or_create(
        tenant=tenant, code='ordering',
        defaults={'name': getattr(tenant, 'display_name', None) or 'Ordering'})
    policy = evaluation_policy()
    config = Configuration.objects.filter(tenant=tenant).first()
    if config is None:
        return Configuration.objects.create(
            tenant=tenant, location=location, enabled=commerce_enabled, policy=policy)
    stored = dict(config.policy)
    stored['schema_version'] = policy['schema_version']
    stored['ordering_limits'] = policy['ordering_limits']
    config.policy = Policy.model_validate(stored).model_dump(mode='json')
    if commerce_enabled:
        config.enabled = True
    config.save()
    return config


class OrderingFixture:
    def setUp(self):
        super().setUp()
        from tests.support.replies import install_reply_renderer
        install_reply_renderer(self)
        self.tenant = TenantInfo.objects.create(slug="ordering", display_name="Cafe")
        from tests.support.runtime import enable_legacy_capabilities
        enable_legacy_capabilities(self.tenant)
        self.customer = Customer.objects.create(tenant=self.tenant, name="User", phone="123",
                                                location_coordinates={"lat": 12, "lng": 77})
        self.chat = ChatSession.objects.create(tenant=self.tenant, customer=self.customer,
                                               platform="telegram", session_id="user")
        menu = {}
        for name, sizes in (("Vanilla", ["mini tub", "family"]), ("Brownie", ["per_quantity"])):
            item = MenuItem.objects.create(tenant=self.tenant, name=name)
            variants = [MenuItemVariant.objects.create(menu_item=item, size=size, price=100) for size in sizes]
            menu[name] = {"name": name, "item_id": str(item.pk), "tags": [],
                          "item_variant_map": {v.size: str(v.pk) for v in variants},
                          "pricing": {str(v.pk): "100" for v in variants}}
        self.menu = menu
        self.enterContext(patch.object(basket, "get_item_pricing_cache", return_value={self.tenant.api_key: menu}))
        self.extract = Mock(side_effect=self.proposal)
        self.provider = self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected LLM")))
        self.payment = self.enterContext(patch.object(placing, "initiate_payment", return_value={"payment_url": "/payment/?a=1&b=2"}))
        self.basket = Basket()
        self.checklist = {}
        self.address = {}

    @staticmethod
    def proposal(api_key, text, **kwargs):
        """Offline model proposals for business tests, not a semantic evaluation."""
        catalog = load_catalog(api_key)
        names = {item['name']: item for item in catalog.values()}
        vanilla = names.get('Vanilla', {})
        brownie = names.get('Brownie', {})
        def line(item, size=None, qty=1, action='add', target=None):
            variants = item.get('variants', [])
            variant = next((v['id'] for v in variants if v['name'] == size), None)
            if size is None and len(variants) == 1: variant = variants[0]['id']
            reference = None
            if action != 'add':
                reference = ({'by': 'id', 'value': str(target)} if target is not None
                             else {'by': 'name', 'value': item.get('name')})
            return dict(action=action,item_id=item.get('item_id'),variant_id=variant,quantity=qty,
                        modifiers=[] if action == 'add' else None,target_number=None,
                        reference=reference,unresolved=[])
        fixtures = {
            '2 Vanilla mini tub': line(vanilla,'mini tub',2),
            'Vanilla mini tub': line(vanilla,'mini tub'),
            'Vanilla family': line(vanilla,'family'),
            '2 Vanilla': line(vanilla,None,2),
            'Brownie': line(brownie), '2 Brownie': line(brownie,qty=2),
            '0 Vanilla mini tub': line(vanilla,'mini tub',0),
            'make Vanilla 3': line(vanilla,None,3,'update'),
            'Vanilla quantity': line(vanilla,None,None,'update'),
            'remove Vanilla': line(vanilla,None,None,'remove'),
            'remove Brownie': line(brownie,None,None,'remove',None),
            'cancel the Brownie': line(brownie,None,None,'remove',None),
            'Vanilla quantity 4': line(vanilla,None,4,'update',None),
            'Vanilla quantity -2': line(vanilla,None,-2,'update',None),
            'Vanilla quantity 3': line(vanilla,None,3,'update',None),
            'make it 4': line(vanilla,None,4,'update'),
            'change Vanilla quantity': line(vanilla,None,None,'update'),

        }
        if text == 'Vanilla family' and kwargs.get('action') == 'update_order':
            fixtures[text] = line(vanilla,'family',None,'update')
        stored = kwargs.get('pending') or {}
        proposal = deepcopy(stored.get('proposal') or (stored.get('action_proposal') or {}).get('basket') or {})
        question = kwargs.get('question', '')
        if proposal.get('lines') and text in {'family', 'mini tub', '3', '4', '2', '1'}:
            if text in {'family','mini tub'}:
                proposal['lines'][0]['variant_id'] = next(v['id'] for v in vanilla['variants'] if v['name']==text)
            elif any(word in question.casefold() for word in ('entry', 'which')):
                proposal['lines'][0]['reference'] = {'by': 'id', 'value': str(int(text))}
                proposal['lines'][0]['target_number'] = int(text)
                proposal['lines'][0]['unresolved'] = []
                proposal['unresolved'] = []
            else:
                proposal['lines'][0]['quantity'] = int(text)
                proposal['unresolved'] = []
            return {'proposal':proposal}
        if text == 'change Vanilla quantity':
            return {'proposal':dict(lines=[fixtures[text]], unresolved=['What is the new quantity?'], catalog_miss=False)}
        if text in {'remove unicorn', 'unicorn quantity 3'}:
            return {'proposal':dict(lines=[],unresolved=['Which item?'],catalog_miss=False)}
        if text in fixtures:
            return {'proposal':dict(lines=[fixtures[text]],unresolved=[],catalog_miss=False)}
        return {'proposal':dict(lines=[],unresolved=['Please specify one menu item, size and quantity.'],catalog_miss=False)}

    def intent(self, sub="add_to_basket", query="2 Vanilla mini tub", **kw):
        obj = placing.PlacingOrderIntent(main_query=query, sub_intent=sub, tenant=self.tenant.pk,
                                         chat_id="user", **kw)
        obj.platform = "telegram"
        return obj

    def bind_change(self, obj, proposal):
        from pydantic import ValidationError
        from tests.support.actions import resolved_change
        try:
            obj.resolved_action = resolved_change(proposal, self.basket)
        except ValidationError:
            obj.resolved_action = resolved_change(
                {'lines': [], 'unresolved': ['Please specify the item, size and customizations.'],
                 'catalog_miss': False}, self.basket)

    def run_intent(self, obj, *, proposal=None):
        if proposal is not None:
            self.bind_change(obj, proposal)
        elif (obj.resolved_action is None and isinstance(obj.sub_intent, str)
              and obj.sub_intent in placing.PlacingOrderIntent.ITEM_ACTIONS):
            self.bind_change(obj, self.proposal(self.tenant.api_key, obj.main_query, action=obj.sub_intent))
        return obj.process_query(self.basket, self.address, self.checklist, [], self.tenant.api_key, self.customer)[0]

    def turn_action(self, text, intent_name, topic, *, pending_item=None, question=''):
        from tests.support.actions import change_action, checkout_action, show_cart_action
        if intent_name == 'placing_order' and topic == 'check_order_cart':
            return show_cart_action()
        if intent_name == 'placing_order' and topic in {'order_confirmation', 'order_payment'}:
            return checkout_action(text)
        if topic == 'customize_confirmation' and text.strip().lower() in {
                'yes', 'yes please', 'ok', 'okay', 'sure', 'confirm', 'proceed', 'pay', 'ready'}:
            return None
        if intent_name != 'placing_order' or topic not in placing.PlacingOrderIntent.ITEM_ACTIONS | {'customize_confirmation'}:
            return None
        if self.extract.side_effect is None and self.extract.return_value:
            raw = self.extract.return_value
        else:
            raw = self.extract(self.tenant.api_key, text, pending=pending_item or {}, question=question)
        return change_action(raw)

    def follow(self, pending, text, sub="customize_confirmation"):
        # Force a real serialized turn boundary for every clarification.
        with patch.object(base, "get_intent", return_value=placing.PlacingOrderIntent):
            pending = base.BaseIntent.from_dict(deepcopy(pending.to_dict()))
        incoming = self.intent(sub, text)
        self.bind_change(incoming, self.proposal(
            self.tenant.api_key, text, pending=pending.basket_item, question=pending.get_followup_question()))
        response, _ = pending.process_followup(incoming, self.basket, self.address,
                                             self.checklist, [], self.tenant.api_key, self.customer)
        return pending, response
