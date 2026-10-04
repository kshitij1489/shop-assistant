"""Atomic session publication, stale readers, and whole-turn serialization."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from chatbot_core.logic.cafe.session import memory, redis_session
from chatbot_core.logic.cafe.session.django import DjangoSessionStore
from tests.support.conversations import FakeRedis
from tests.support.checkout import CheckoutFixture


class SessionTurnTests(TestCase):
    def setUp(self):
        self.enterContext(patch.object(memory, '_session_data', {}))
        self.redis = FakeRedis()
        self.enterContext(patch.object(redis_session, '_redis', self.redis))

    def stores(self):
        yield memory.MemorySessionStore('turn', tenant_id=1, platform='website')
        yield redis_session.RedisSessionStore('turn', tenant_id=1, platform='website')
        browser = SessionStore()
        browser.create()
        yield DjangoSessionStore(SimpleNamespace(session=browser), tenant_id=1)

    def test_setter_and_serialization_failures_publish_no_fields(self):
        for store in self.stores():
            with self.subTest(store=type(store).__name__):
                with store.turn():
                    store.set_delivery_address({'city': 'Before'})
                before = deepcopy(store.read_snapshot())
                with patch.object(store, 'set_delivery_address', side_effect=RuntimeError('write failed')):
                    with self.assertRaisesMessage(RuntimeError, 'write failed'):
                        with store.turn():
                            store.increment_counter()
                            store.set_history([{'new': 'history'}])
                            store.set_checklist({'order': True})
                            store.set_delivery_address({'city': 'After'})
                self.assertEqual(store.read_snapshot(), before)
                with self.assertRaises(TypeError):
                    with store.turn():
                        store.increment_counter()
                        store.set_checklist({'unserializable': object()})
                self.assertEqual(store.read_snapshot(), before)

    def test_concurrent_turns_read_after_previous_commit(self):
        for cls in (memory.MemorySessionStore, redis_session.RedisSessionStore):
            with self.subTest(store=cls.__name__):
                first = cls('concurrent', tenant_id=1, platform='website')
                second = cls('concurrent', tenant_id=1, platform='website')
                loaded, attempted, entered, release = Event(), Event(), Event(), Event()

                def turn_one():
                    with first.turn():
                        counter = first.get_counter()
                        loaded.set()
                        if not release.wait(3):
                            raise AssertionError('Test failed to release first turn')
                        first.increment_counter()
                        first.set_history([{'counter': counter + 1}])

                def turn_two():
                    attempted.set()
                    with second.turn():
                        entered.set()
                        self.assertEqual(second.get_counter(), 1)
                        second.increment_counter()
                        second.set_history(second.get_history() + [{'counter': 2}])

                with ThreadPoolExecutor(max_workers=2) as pool:
                    one = pool.submit(turn_one)
                    self.assertTrue(loaded.wait(2))
                    two = pool.submit(turn_two)
                    self.assertTrue(attempted.wait(2))
                    try:
                        self.assertFalse(entered.wait(.05))
                    finally:
                        release.set()
                    one.result(timeout=3)
                    two.result(timeout=3)
                self.assertEqual(first.get_counter(), 2)
                self.assertEqual(first.get_history(), [{'counter': 1}, {'counter': 2}])

    def test_expired_redis_owner_cannot_publish(self):
        store = redis_session.RedisSessionStore('lease', tenant_id=1, platform='website')
        before = deepcopy(store.read_snapshot())
        with self.assertRaisesMessage(RuntimeError, 'lock was lost'):
            with store.turn():
                store.increment_counter()
                self.redis.data[redis_session._lock_key(store._storage_id)] = 'new-owner'
        self.assertEqual(store.read_snapshot(), before)

    def test_browser_reload_and_middleware_do_not_overwrite_newer_turn(self):
        browser = SessionStore()
        browser.create()
        first = DjangoSessionStore(SimpleNamespace(session=browser), tenant_id=1)
        stale_browser = SessionStore(browser.session_key)
        second = DjangoSessionStore(SimpleNamespace(session=stale_browser), tenant_id=1)
        self.assertEqual(second.get_counter(), 0)
        with first.turn():
            first.increment_counter()
        with second.turn():
            self.assertEqual(second.get_counter(), 1)
            second.increment_counter()
        browser.save()  # Late SessionMiddleware save for the earlier request.
        third = DjangoSessionStore(SimpleNamespace(session=SessionStore(browser.session_key)), tenant_id=1)
        self.assertEqual(third.get_counter(), 2)

    def test_browser_save_failure_restores_request_and_database(self):
        browser = SessionStore()
        browser.create()
        store = DjangoSessionStore(SimpleNamespace(session=browser), tenant_id=1)
        with patch.object(browser, 'save', side_effect=RuntimeError('save failed')):
            with self.assertRaisesMessage(RuntimeError, 'save failed'):
                with store.turn():
                    store.increment_counter()
                    store.set_history([{'new': 'history'}])
        self.assertEqual(store.get_counter(), 0)
        restored = DjangoSessionStore(SimpleNamespace(session=SessionStore(browser.session_key)), tenant_id=1)
        self.assertEqual(restored.get_counter(), 0)
        self.assertEqual(restored.get_history(), [])


class BrowserTurnConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_advisory_lock_serializes_browser_turns(self):
        browser = SessionStore()
        browser.create()
        loaded, attempted, entered, release = Event(), Event(), Event(), Event()

        def turn(first):
            close_old_connections()
            try:
                store = DjangoSessionStore(SimpleNamespace(session=SessionStore(browser.session_key)), tenant_id=1)
                if not first:
                    attempted.set()
                with store.turn():
                    if first:
                        loaded.set()
                        if not release.wait(3):
                            raise AssertionError('First turn was not released')
                    else:
                        entered.set()
                        self.assertEqual(store.get_counter(), 1)
                    store.increment_counter()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(turn, True)
            self.assertTrue(loaded.wait(2))
            two = pool.submit(turn, False)
            self.assertTrue(attempted.wait(2))
            try:
                self.assertFalse(entered.wait(.05))
            finally:
                release.set()
            one.result(timeout=3)
            two.result(timeout=3)
        store = DjangoSessionStore(SimpleNamespace(session=SessionStore(browser.session_key)), tenant_id=1)
        self.assertEqual(store.get_counter(), 2)


class BrowserCheckoutTransactionTests(CheckoutFixture, TransactionTestCase):
    """Real browser store and checkout; transaction boundaries are not test-wrapped."""
    def setUp(self):
        from orders.models import CheckoutSettings
        super().setUp()
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.browser = SessionStore()
        self.browser.create()
        self.session.session_id = self.browser.session_key
        self.session.save()
        self.store = DjangoSessionStore(SimpleNamespace(session=self.browser), tenant_id=self.tenant.pk)
        with self.store.turn():
            self.store.set_basket(deepcopy(self.basket))

    def send(self, text):
        return self.graph_turn(self.store, text)

    def test_confirmed_order_survives_later_failure_and_retry(self):
        from django.db import connection, transaction
        from orders.models import Order, ChatSession
        from chatbot_core.models import TenantInfo
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent

        def assert_rows_unlocked():
            close_old_connections()
            try:
                with transaction.atomic():
                    TenantInfo.objects.select_for_update(nowait=True).get(pk=self.tenant.pk)
                    ChatSession.objects.select_for_update(nowait=True).get(pk=self.session.pk)
                    Order.objects.select_for_update(nowait=True).get(pk=self.session.order_id)
            finally:
                close_old_connections()

        for failure in ('response', 'save'):
            with self.subTest(failure=failure):
                self.session.order = None
                self.session.state = {}
                self.session.save()
                Order.objects.all().delete()
                with self.store.turn():
                    self.store.set_checklist({})
                    self.store.set_ongoing_queries([], None)
                    self.store.set_basket(deepcopy(self.basket))
                self.send('checkout')
                self.send('pickup')
                original = PlacingOrderIntent.configured_checkout

                def checkout_then_fail(intent, *args, **kwargs):
                    reply = original(intent, *args, **kwargs)
                    self.assertFalse(connection.in_atomic_block)
                    self.session.refresh_from_db()
                    self.assertIsNotNone(self.session.order_id)
                    if connection.vendor == 'postgresql':
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(assert_rows_unlocked).result(timeout=3)
                    if failure == 'response':
                        raise RuntimeError('response failed after checkout')
                    return reply

                with patch.object(PlacingOrderIntent, 'configured_checkout', checkout_then_fail):
                    if failure == 'save':
                        with patch.object(self.browser, 'save', side_effect=RuntimeError('save failed')):
                            with self.assertRaisesMessage(RuntimeError, 'save failed'):
                                self.send('confirm')
                    else:
                        with self.assertRaisesMessage(RuntimeError, 'response failed'):
                            self.send('confirm')
                self.session.refresh_from_db()
                self.assertEqual(Order.objects.count(), 1)
                order_id = self.session.order_id
                self.store = DjangoSessionStore(
                    SimpleNamespace(session=SessionStore(self.browser.session_key)), tenant_id=self.tenant.pk)
                self.browser = self.store._request_session
                self.assertIn('Pay cash', self.send('confirm')[0])
                self.session.refresh_from_db()
                self.assertEqual(self.session.order_id, order_id)
                self.assertEqual(Order.objects.count(), 1)
