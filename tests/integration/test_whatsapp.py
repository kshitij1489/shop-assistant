"""Signed WhatsApp messages exercise real customer, session and checkout ownership."""
from tests.support.runtime import classification_result
import hashlib
import hmac
import importlib
import json
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings

from chatbot_core.channels import whatsapp
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.session import redis_session
from chatbot_core.models import TenantInfo
from orders.checkout_config import default_checkout_config
from orders.models import ChatSession, CheckoutSettings, Customer, MenuItem, MenuItemVariant, Order
from tests.support.runtime import enable_legacy_capabilities
from tests.support.conversations import FakeRedis


@override_settings(WHATSAPP_APP_SECRET="whatsapp-test-secret")
class WhatsAppIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")

    def setUp(self):
        from tests.support.replies import install_reply_renderer
        install_reply_renderer(self)
        self.tenant = TenantInfo.objects.create(
            display_name="WhatsApp cafe", business_type="cafe", whatsapp_id="business-number",
            is_active=True, approval_status="APPROVED",
        )
        enable_legacy_capabilities(self.tenant)
        self.redis = FakeRedis()
        self.enterContext(patch.object(redis_session, "_redis", self.redis))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))
        self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected provider I/O")))

    def request(self, message="show basket", *, sender="919876543210", tenant=None, signature=None):
        value = {
            "metadata": {"phone_number_id": (tenant or self.tenant).whatsapp_id},
            "messages": [{"from": sender, "text": {"body": message}}],
        }
        body = json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
        if signature is None:
            signature = "sha256=" + hmac.new(b"whatsapp-test-secret", body, hashlib.sha256).hexdigest()
        headers = {"HTTP_X_HUB_SIGNATURE_256": signature} if signature else {}
        return RequestFactory().post("/", body, content_type="application/json", **headers)

    def send(self, message="show basket", sub_intent="check_order_cart", **kwargs):
        self.classify.return_value = classification_result([(message, "placing_order", sub_intent, None, None)])
        response = whatsapp.whatsapp_webhook(self.request(message, **kwargs))
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_first_message_creates_session_and_reuses_existing_customer(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Returning guest", phone="", whatsapp_number="919876543210")
        self.send()
        self.send()
        chat = ChatSession.objects.get()
        self.assertEqual(chat.customer, customer)
        self.assertEqual(chat.tenant, self.tenant)
        self.assertEqual(chat.platform, "whatsapp")
        self.assertEqual(chat.session_id, customer.whatsapp_number)
        self.assertEqual(Customer.objects.count(), 1)

    def test_customers_are_isolated_by_tenant_and_sender(self):
        other = TenantInfo.objects.create(
            display_name="Other cafe", business_type="cafe", whatsapp_id="other-business",
            is_active=True, approval_status="APPROVED",
        )
        enable_legacy_capabilities(other)
        self.send()
        self.send()
        self.send(tenant=other)
        self.send(sender="919876543211")
        self.assertEqual(Customer.objects.count(), 3)
        self.assertEqual(ChatSession.objects.count(), 3)
        for chat in ChatSession.objects.select_related("customer"):
            self.assertEqual(chat.customer.tenant_id, chat.tenant_id)
            self.assertEqual(chat.customer.whatsapp_number, chat.session_id)

    def test_configured_checkout_recovers_customer_after_cache_loss(self):
        config = default_checkout_config()
        config["opening_hours"] = {}
        config["modes"]["pickup"] = {**config["modes"]["pickup"], "required_fields": []}
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=config)
        self.send()
        chat = ChatSession.objects.get()
        item = MenuItem.objects.create(tenant=self.tenant, name="Coffee")
        variant = MenuItemVariant.objects.create(menu_item=item, size="Regular", price="100")
        store = redis_session.RedisSessionStore(chat.session_id, tenant_id=self.tenant.pk, platform="whatsapp")
        store.set_basket(Basket(items=[{
            "item_id": str(item.pk), "item_variant_id": str(variant.pk), "name": "Coffee",
            "size": "Regular", "quantity": 1, "unit_price": "100", "item_number": 1,
        }]))
        self.send("checkout", "order_confirmation")
        self.send("pickup", "order_confirmation")
        chat.refresh_from_db()
        self.assertTrue(chat.state["checkout"])
        self.assertFalse(Order.objects.exists())
        self.redis.data.clear()
        self.send("confirm", "order_confirmation")
        order = Order.objects.get()
        chat.refresh_from_db()
        self.assertEqual(order.customer_id, chat.customer_id)
        self.assertEqual(order.tenant_id, self.tenant.pk)
        self.assertEqual(chat.order_id, order.pk)
        self.redis.data.clear()
        self.send("confirm", "order_confirmation")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)

    def test_missing_malformed_and_incorrect_signatures_are_forbidden(self):
        for signature in ("", "sha256=short", "sha256=" + "0" * 64, "sha256=" + "é" * 64):
            with self.subTest(signature=signature), patch.object(whatsapp, "route_message_for_tenant") as route:
                response = whatsapp.whatsapp_webhook(self.request(signature=signature))
                self.assertEqual(response.status_code, 403)
                route.assert_not_called()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(self.redis.data)

    @override_settings(WHATSAPP_APP_SECRET="")
    def test_unconfigured_secret_is_forbidden(self):
        with patch.object(whatsapp, "route_message_for_tenant") as route:
            self.assertEqual(whatsapp.whatsapp_webhook(self.request()).status_code, 403)
            route.assert_not_called()
        self.assertFalse(Customer.objects.exists())

    def test_missing_sender_is_rejected_before_customer_creation(self):
        for sender in (None, "", " ", 123):
            with self.subTest(sender=sender), patch.object(whatsapp, "route_message_for_tenant") as route:
                self.assertEqual(whatsapp.whatsapp_webhook(self.request(sender=sender)).status_code, 400)
                route.assert_not_called()
        self.assertFalse(Customer.objects.exists())
