"""HTTP -> tenant/customer/session -> real graph and business handlers, offline."""
from tests.support.runtime import classification_result
import importlib
from datetime import timedelta
from unittest.mock import patch

import jwt
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from chatbot_core.channels import website
from chatbot_core.channels.utils import generate_tenant_jwt
from chatbot_core.logic.cafe import basket
from chatbot_core.models import TenantInfo
from orders.models import ChatSession, Customer, MenuItem, MenuItemVariant, Order


urlpatterns = [
    path("chatbot-api/", website.chatbot_api),
    path("token/", website.public_jwt_token),
]


@override_settings(
    ROOT_URLCONF=__name__,
    JWT_SECRET="website-tests-only-secret-at-least-32-characters",
    SESSION_ENGINE="django.contrib.sessions.backends.db",
    MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware"],
)
class WebsiteIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")

    def setUp(self):
        cache.clear()
        self.tenant = TenantInfo.objects.create(slug="website", display_name="Website cafe", business_type="cafe", approval_status="APPROVED")
        from tests.support.runtime import enable_legacy_capabilities
        enable_legacy_capabilities(self.tenant)
        self.other = TenantInfo.objects.create(slug="other", display_name="Other cafe", business_type="cafe", approval_status="APPROVED")
        enable_legacy_capabilities(self.other)
        item = MenuItem.objects.create(tenant=self.tenant, name="Vanilla")
        variants = [MenuItemVariant.objects.create(menu_item=item, size=size, price="100.50")
                    for size in ("mini tub", "family")]
        menu = {"Vanilla": {"name": "Vanilla", "item_id": str(item.pk), "tags": [],
                            "item_variant_map": {v.size: str(v.pk) for v in variants},
                            "pricing": {str(v.pk): "100.50" for v in variants}}}
        self.enterContext(patch.object(basket, "get_item_pricing_cache", return_value={self.tenant.api_key: menu}))
        self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected provider I/O")))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))

    def send(self, message="show basket", sub_intent="check_order_cart", *, client=None, tenant=None, form=False, extra=None):
        tenant = tenant or self.tenant
        def classify(*args, **kwargs):
            from tests.support.actions import change_action, implied_action
            from tests.support.ordering import OrderingFixture
            pending = kwargs['conversation_context']['open_requests']
            reply_to = pending[-1]['id'] if pending and sub_intent == 'customize_confirmation' else None
            action = implied_action(message, 'placing_order', sub_intent)
            if action is None:
                item = pending[-1]['details'] if pending else None
                question = pending[-1].get('question', '') if pending else ''
                action = change_action(OrderingFixture.proposal(
                    tenant.api_key, message, pending=item or {}, question=question))
            return classification_result([(message, 'placing_order', sub_intent, reply_to, None, action)])
        self.classify.side_effect = classify
        kwargs = {} if form else {"content_type": "application/json"}
        return (client or self.client).post(
            "/chatbot-api/", {"message": message, **(extra or {})},
            HTTP_AUTHORIZATION="Bearer " + generate_tenant_jwt(tenant.slug), **kwargs,
        )

    def test_default_graph_returns_text_and_persists_basket_across_http_turns(self):
        response = self.send("2 Vanilla mini tub", "add_to_basket")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Added 2", response.json()["response"])
        line = response.json()["basket"][0]
        self.assertEqual(line["quantity"], 2)
        self.assertEqual(line["currency"], "INR")
        self.assertEqual(line["exponent"], 2)
        self.assertEqual(line["unit_price_minor"], 10050)
        self.assertEqual(line["line_total_minor"], 20100)
        self.assertNotIn("price", line)
        followup = self.send(form=True)
        self.assertEqual(followup.status_code, 200)
        self.assertIsInstance(followup.json()["response"], str)
        self.assertEqual(followup.json()["basket"], response.json()["basket"])
        chat = ChatSession.objects.get()
        self.assertEqual(chat.platform, "website")
        self.assertEqual(chat.session_id, self.client.session.session_key)
        self.assertEqual(chat.customer.tenant, self.tenant)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(self.classify.call_count, 2)

    def test_rename_preserves_existing_tokens_public_links_and_runtime_configuration(self):
        from chatbot_core.runtime_configuration import get_configuration

        original_slug = self.tenant.slug
        token = generate_tenant_jwt(original_slug)
        before = get_configuration(tenant_id=self.tenant.pk)
        self.tenant.display_name = 'Website Roasters'
        self.tenant.save(update_fields=['display_name'])
        # Reusing the former name must not take over the original public link.
        successor = TenantInfo.objects.create(display_name='Website cafe')
        self.assertNotEqual(successor.slug, original_slug)

        response = self.client.get('/token/', {'tenant': original_slug}, HTTP_X_API_KEY=self.tenant.api_key)
        self.assertEqual(response.status_code, 200)
        with patch.object(website, 'route_message_for_tenant', return_value=('Still connected', [])) as route:
            response = self.client.post('/chatbot-api/', {'message': 'Hello'}, content_type='application/json',
                                        HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(route.call_args.args[0].pk, self.tenant.pk)
        self.assertEqual(route.call_args.args[0].display_name, 'Website Roasters')
        after = get_configuration(api_key=self.tenant.api_key)
        self.assertEqual((after.tenant_id, after.slug, after.version, after.documents),
                         (before.tenant_id, before.slug, before.version, before.documents))

    def test_pending_item_followup_survives_browser_session_serialization(self):
        first = self.send("2 Vanilla", "add_to_basket")
        self.assertIn("size", first.json()["response"])
        second = self.send("family", "customize_confirmation")
        self.assertEqual(second.status_code, 200)
        self.assertIn("Added 2", second.json()["response"])
        self.assertEqual(second.json()["basket"][0]["size"], "family")
        self.assertEqual(Customer.objects.count(), 1)

    def test_guest_customer_and_basket_are_isolated_by_browser_and_tenant(self):
        existing = Customer.objects.create(tenant=self.tenant, name="Existing customer", phone="123")
        self.send("2 Vanilla mini tub", "add_to_basket", extra={"customer_id": str(existing.pk), "phone": "123"})
        self.assertEqual(self.send(client=Client()).json()["basket"], [])
        self.assertEqual(self.send(tenant=self.other).json()["basket"], [])
        self.assertEqual(self.send().json()["basket"][0]["quantity"], 2)
        chats = list(ChatSession.objects.all())
        self.assertEqual(len(chats), 3)
        self.assertEqual(len({chat.customer_id for chat in chats}), 3)
        self.assertNotIn(existing.pk, {chat.customer_id for chat in chats})
        for chat in chats:
            self.assertEqual(chat.customer.tenant_id, chat.tenant_id)

    def test_checkout_links_order_to_browser_customer_and_chat(self):
        self.send("2 Vanilla mini tub", "add_to_basket")
        response = self.send("checkout", "order_confirmation")
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        chat = ChatSession.objects.get()
        self.assertEqual(order.customer_id, chat.customer_id)
        self.assertEqual(order.tenant_id, self.tenant.pk)
        self.assertEqual(chat.order_id, order.pk)

    def test_completed_chat_keeps_guest_identity_for_next_conversation(self):
        self.send()
        first = ChatSession.objects.get()
        first.is_completed = True
        first.save(update_fields=["is_completed"])
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(ChatSession.objects.filter(customer=first.customer).count(), 2)

    def test_invalid_bodies_return_400_before_creating_customer(self):
        for body in ('{', '[]', 'null', '{"message": null}', '{"message": 42}', '{"message": " "}', '{}'):
            with self.subTest(body=body):
                response = self.client.post("/chatbot-api/", body, content_type="application/json",
                                            HTTP_AUTHORIZATION="Bearer " + generate_tenant_jwt(self.tenant.slug))
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Customer.objects.exists())
        self.classify.assert_not_called()

    def test_authentication_and_unknown_tenant_fail_before_graph(self):
        from django.conf import settings
        expired = jwt.encode({"tenant_slug": self.tenant.slug, "exp": timezone.now() - timedelta(minutes=1)},
                             settings.JWT_SECRET, algorithm="HS256")
        for token, status in ((None, 401), ("", 401), ("invalid", 401), (expired, 401),
                              (generate_tenant_jwt("missing"), 404)):
            with self.subTest(token=token):
                headers = {} if token is None else {"HTTP_AUTHORIZATION": "Bearer " + token}
                response = self.client.post("/chatbot-api/", {"message": "hello"}, **headers)
                self.assertEqual(response.status_code, status)
        self.assertFalse(Customer.objects.exists())
        self.classify.assert_not_called()

    def test_public_token_can_authenticate_website_chat(self):
        denied = self.client.get("/token/", {"tenant": self.tenant.slug}, HTTP_X_API_KEY="wrong")
        self.assertEqual(denied.status_code, 403)
        token = self.client.get("/token/", {"tenant": self.tenant.slug}, HTTP_X_API_KEY=self.tenant.api_key)
        self.assertEqual(token.status_code, 200)
        self.classify.return_value = classification_result([("show basket", "placing_order", "check_order_cart", None, None)])
        response = self.client.post("/chatbot-api/", {"message": "show basket"},
                                    HTTP_AUTHORIZATION="Bearer " + token.json()["token"])
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["response"], str)

    def test_failed_turn_does_not_retry_routing_and_returns_json_error(self):
        with patch.object(website, "route_message_for_tenant", side_effect=RuntimeError("private failure")) as route, \
             self.assertLogs(website.logger, level="ERROR"):
            response = self.send()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Internal Server Error"})
        route.assert_called_once()
