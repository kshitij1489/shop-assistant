from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache as real_cache
from django.http import JsonResponse
from django.test import SimpleTestCase, TestCase, RequestFactory, override_settings
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult, ChatGeneration
from langchain_openai import ChatOpenAI

from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.contracts.models import ExecutionIdentity
from evaluate.controls.context import ControlContext, activate, current, business_now, fault_active
from evaluate.controls.cache import cache, namespace, semantic_lookup, semantic_write
from evaluate.controls.http import evaluation_request
from evaluate.controls.llm import ModelEvidence
from evaluate.controls.ownership import context_for, ticket, worker_ticket, resolve_ticket
from evaluate.controls.telemetry import EvidenceHandler, emit, logger


def context(**kwargs):
    identity = ExecutionIdentity(run_id='test-run', scenario_id='sessions:test', scenario_instance_id='instance', attempt=1)
    return ControlContext(identity, 'lease', '1', 'customer', 'chat', 'request',
        datetime(2032, 1, 2, 12, tzinfo=timezone.utc), **kwargs)


@override_settings(EVALUATION_ENABLED=True)
class ContextTests(SimpleTestCase):
    def test_capability_evidence_is_scoped_and_redacts_model_route_labels(self):
        from evaluate.controls.telemetry import emit_capability_check
        secret = 'sk-' + 'a' * 30
        fields = dict(classification_index=2,
            classified_route=(secret, 'https://example.test/private'),
            effective_route=('placing_order', 'update_order'), action_kind='CHANGE_BASKET',
            configuration_version=3,
            required_routes={('placing_order', 'add_to_basket'), ('placing_order', 'update_order')},
            unavailable_routes={('placing_order', 'update_order')})
        with patch.object(logger, 'info') as log:
            emit_capability_check(**fields)
            log.assert_not_called()
        with activate(context()), self.assertLogs('evaluate.telemetry', 'INFO') as logs:
            emit_capability_check(**fields)
        record = logs.records[0].evaluation
        self.assertEqual(record['event'], 'capability.checked')
        self.assertEqual(record['classification_index'], 2)
        self.assertEqual(record['configuration_version'], 3)
        self.assertEqual(record['required_routes'], [
            ['placing_order', 'add_to_basket'], ['placing_order', 'update_order']])
        self.assertEqual(record['unavailable_routes'], [['placing_order', 'update_order']])
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn('https://', json.dumps(record))

    def test_typed_decision_evidence_is_scoped_and_redacted(self):
        from chatbot_core.llm.schemas import ClassifiedMessages
        from evaluate.controls.telemetry import emit_classification
        secret = 'sk-' + 'a' * 30
        proposal = ClassifiedMessages(classifications=[{
            'query': secret + ' https://example.test/private', 'intent': 'placing_order',
            'sub_intent': 'order_confirmation', 'reply_to': 'checkout', 'clarification': None,
            'action': {'kind': 'CLEAR_CHECKOUT_FIELD', 'field': 'scheduled_at'},
        }], declared_constraints=[])
        with patch.object(logger, 'info') as log:
            emit_classification(proposal, prompt_version='v8', catalog_version='catalog')
            log.assert_not_called()
        with activate(context()), self.assertLogs('evaluate.telemetry', 'INFO') as logs:
            emit_classification(proposal, prompt_version='v8', catalog_version='catalog')
        record = logs.records[0].evaluation
        self.assertEqual(record['request_id'], 'request')
        self.assertEqual(record['proposal']['classifications'][0]['action']['kind'], 'CLEAR_CHECKOUT_FIELD')
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn('https://', json.dumps(record))

    def test_parallel_clocks_and_exception_cleanup(self):
        gate = Barrier(2)
        def run(year):
            ctx = replace(context(), business_at=datetime(year, 1, 1, tzinfo=timezone.utc))
            try:
                with activate(ctx):
                    gate.wait(timeout=5)
                    self.assertEqual(business_now('1').year, year)
                    self.assertNotEqual(datetime.now(timezone.utc).year, year)
                    raise RuntimeError('exit')
            except RuntimeError:
                self.assertIsNone(current())
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(run, [2032, 2033]))

    @override_settings(EVALUATION_ENABLED=False)
    def test_disabled_ignores_context_headers_and_faults(self):
        with activate(context(faults=frozenset({'coverage'}))):
            self.assertIsNone(current())
            self.assertFalse(fault_active('coverage', 'unrelated'))
            self.assertLess(abs((business_now() - datetime.now(timezone.utc)).total_seconds()), 1)
            request = RequestFactory().get('/', HTTP_X_EVALUATION_CONTEXT='forged')
            self.assertEqual(evaluation_request(lambda r: JsonResponse({'ok': True}))(request).status_code, 200)

    def test_fault_isolation_and_label(self):
        with activate(context(faults=frozenset({'coverage'}))), self.assertLogs('evaluate.telemetry') as logs:
            self.assertTrue(fault_active('coverage', '1'))
            self.assertFalse(fault_active('classification', '1'))
            with self.assertRaises(PermissionError):
                fault_active('coverage', '2')
        self.assertTrue(logs.records[0].evaluation['injected'])
        self.assertFalse(fault_active('coverage', '1'))

    def test_caught_logging_failure_still_fails_request_boundary(self):
        from evaluate.controls.telemetry import span
        ctx = context()
        with activate(ctx), self.assertRaisesRegex(RuntimeError, 'evidence persistence'):
            with span('http'):
                with patch.object(logger, 'info', side_effect=OSError('disk failure')):
                    try:
                        emit('llm.started', call_id='call')
                    except OSError:
                        pass  # The application's model fallback catches errors.
        self.assertTrue(ctx.evidence_failed.is_set())
        self.assertIsNone(current())

    def test_cache_cold_never_reads_or_writes_unrelated_and_warm_is_per_lease(self):
        real_cache.set('control-test-key', 'unrelated')
        with activate(context()):
            self.assertIsNone(cache.get('control-test-key'))
            cache.set('control-test-key', 'ignored')
            @semantic_lookup('test')
            def lookup():
                self.fail('cold lookup reached vector/database cache')
            @semantic_write
            def write(ctx, response):
                self.fail('cold write reached vector/database cache')
            self.assertEqual(lookup(), (False, None, {}))
            self.assertEqual(write({}, 'answer'), 'answer')
        with activate(context(cache_mode='warm')):
            self.assertIsNone(cache.get('control-test-key'))
            cache.set('control-test-key', 'owned')
            self.assertEqual(cache.get('control-test-key'), 'owned')
            own_key = namespace('control-test-key')
        with activate(replace(context(cache_mode='warm'), lease_id='another')):
            self.assertIsNone(cache.get('control-test-key'))
        with activate(replace(context(cache_mode='warm'), business_at=datetime(2033, 1, 1, tzinfo=timezone.utc))):
            self.assertIsNone(cache.get('control-test-key'))
        self.assertEqual(real_cache.get('control-test-key'), 'unrelated')
        real_cache.delete(own_key)
        real_cache.delete('control-test-key')

    def test_evidence_redacts_secrets_and_omits_provider_payload(self):
        callback = ModelEvidence()
        run = uuid4()
        secret = 'sk-' + 'a' * 30
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'application.jsonl'
            handler = EvidenceHandler(path, 'test-run')
            logger.addHandler(handler)
            old = logger.level
            logger.setLevel('INFO')
            try:
                with activate(context()):
                    callback.on_chat_model_start({}, [[secret]], run_id=run, invocation_params={'model': 'test-model'})
                    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content=secret,
                        response_metadata={'model_name': 'served-model', 'headers': {'x-request-id': 'req-provider', 'set-cookie': secret}},
                        usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10}))]]), run_id=run)
                    emit('test', model=secret, provider_request_id='https://pay.test/?secret=private')
                    with self.assertRaises(ValueError):
                        emit('test', prompt=secret)
            finally:
                logger.removeHandler(handler)
                handler.close()
                logger.setLevel(old)
            raw = path.read_text()
            self.assertNotIn(secret, raw)
            self.assertNotIn('https://', raw)
            records = [json.loads(line) for line in raw.splitlines()]
            call = records[1]
            self.assertEqual(call['total_tokens'], 10)
            self.assertEqual(call['provider_request_id'], 'req-provider')
            self.assertEqual(call['request_id'], 'request')
            self.assertEqual(call['session_id'], 'chat')
            self.assertIsNone(call['retries'])

    def test_real_langchain_metadata_callback_with_offline_http(self):
        callback = ModelEvidence()
        def reply(request):
            return httpx.Response(200, headers={'x-request-id': 'req-offline'}, json={
                'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 0, 'model': 'served-model',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'hello'}}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            model = ChatOpenAI(model='gpt-4.1-mini', api_key='offline', http_client=client,
                callbacks=[callback], include_response_headers=True, max_retries=0)
            with activate(context()), self.assertLogs('evaluate.telemetry') as logs:
                self.assertEqual(model.invoke('hello').content, 'hello')
        end = [r.evaluation for r in logs.records if r.evaluation['event'] == 'llm.completed'][0]
        self.assertEqual(end['total_tokens'], 6)
        self.assertEqual(end['provider_request_id'], 'req-offline')
        self.assertEqual(end['model'], 'served-model')

    def test_classification_fault_bypasses_warm_cache_without_a_model_call(self):
        from chatbot_core.logic.cafe.prompts.normalize_and_classify import (
            normalize_and_classify, NormalizationClassificationError,
        )
        module = 'chatbot_core.logic.cafe.prompts.normalize_and_classify'
        with activate(context(faults=frozenset({'classification'}), cache_mode='warm')):
            with patch(module + '._cached_proposal') as lookup, patch(module + '.structured_chain') as model:
                with self.assertRaises(NormalizationClassificationError):
                    normalize_and_classify('hello', tenant_key='1')
        lookup.assert_not_called()
        model.assert_not_called()


@override_settings(EVALUATION_ENABLED=True)
class OwnershipTests(TestCase):
    def setUp(self):
        from chatbot_core.models import TenantInfo
        from chatbot_core.scope import session_identity
        from orders.models import Customer, ChatSession
        from evaluate.fixtures.provision import DjangoProvisioner
        self.provisioner = DjangoProvisioner()
        self.identity = context().identity
        self.lease = Lease(str(uuid4()), self.identity.scenario_instance_id)
        self.tenant = TenantInfo.objects.create(slug='eval-test', display_name='eval-test', approval_status='APPROVED')
        self.customer = Customer.objects.create(tenant=self.tenant, name='QA Guest', phone='')
        self.browser = SessionStore()
        self.browser.create()
        self.namespace = 'cafe:v2:' + session_identity(str(self.tenant.pk), 'website', self.browser.session_key)
        self.browser[self.namespace] = {'customer_id': str(self.customer.pk), 'basket': {'items': []}}
        self.browser.save()
        self.chat = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, platform='website',
            session_id=self.browser.session_key, state={})
        self.owner = dict(lease=self.lease.handle, run_id=self.identity.run_id, scenario_id=self.identity.scenario_id,
            instance_id=self.identity.scenario_instance_id, attempt=1, lifecycle='ready',
            maps={'tenant': str(self.tenant.pk), 'customer:active': str(self.customer.pk), 'session:active': str(self.chat.pk)},
            browser_sessions=[self.browser.session_key], namespace=self.namespace,
            clock={'at': '2032-01-02T12:00:00+00:00', 'timezone': 'UTC'})
        self.provisioner.save_owner(self.tenant, self.owner)

    def test_forged_expired_wrong_browser_and_wrong_identity_rejected(self):
        view = evaluation_request(lambda r: JsonResponse({'year': business_now().year}))
        request = RequestFactory().get('/', HTTP_X_EVALUATION_CONTEXT='forged')
        request.session = self.browser
        self.assertEqual(view(request).status_code, 403)
        value = ticket(self.lease, self.identity, 'request', self.provisioner)
        request.META['HTTP_X_EVALUATION_CONTEXT'] = value
        with self.assertLogs('evaluate.telemetry'):
            response = view(request)
        self.assertEqual(json.loads(response.content)['year'], 2032)
        self.assertEqual(response['X-Evaluation-Request-ID'], 'request')
        request.session = SimpleNamespace(session_key='unrelated')
        self.assertEqual(view(request).status_code, 403)
        request.session = self.browser
        with patch('django.core.signing.time.time', return_value=datetime.now().timestamp() + 400):
            self.assertEqual(view(request).status_code, 403)
        with self.assertRaises(Blocked):
            context_for(self.lease, self.identity.model_copy(update={'attempt': 2}), 'request')
        self.assertIsNone(current())

    def test_worker_clock_snapshot_and_celery_propagation(self):
        from celery import Celery
        from evaluate.controls.celery import EvaluationTask, KEY
        ctx, _ = context_for(self.lease, self.identity, 'request')
        signed = worker_ticket(ctx)
        self.owner['clock']['at'] = '2033-01-01T00:00:00+00:00'
        self.provisioner.save_owner(self.tenant, self.owner)
        self.assertEqual(resolve_ticket(signed, worker=True)[0].business_at.year, 2032)
        app = Celery('control-test', broker='memory://')
        app.conf.update(task_always_eager=True, task_eager_propagates=True)
        @app.task(base=EvaluationTask)
        def year():
            return business_now().year
        with activate(ctx), self.assertLogs('evaluate.telemetry') as logs:
            self.assertEqual(year.delay().get(), 2032)
        self.assertTrue(any(r.evaluation.get('task_id') for r in logs.records))
        self.assertIsNone(current())
        with self.assertRaises(Exception):
            year.apply(headers={KEY: 'forged'}, throw=True)

    def test_scheduling_uses_same_clock_in_web_and_worker_without_freezing_jwt(self):
        from celery import Celery
        from evaluate.controls.celery import EvaluationTask
        from chatbot_core.logic.cafe.checkout import quote
        from orders.checkout_config import CheckoutPolicy
        from chatbot_core.logic.cafe.basket import Basket
        from tests.support.ordering import seed_evaluation_policy
        seed_evaluation_policy(self.tenant)
        ctx, _ = context_for(self.lease, self.identity, 'request')
        policy = CheckoutPolicy(timezone='UTC', modes={'pickup': dict(required_fields=[],
            payment_methods=['cash'], scheduling_enabled=True, preparation_minutes=30, max_advance_days=7)})
        def scheduled():
            draft = {'mode': 'pickup', 'payment_method': 'cash', 'fields': {'scheduled_at': '2032-01-02T14:00:00+00:00'}}
            with patch('chatbot_core.logic.cafe.checkout.basket_total', return_value=10), patch('commerce.services.basket_quote', return_value=None):
                result = quote(policy, draft, Basket(), self.tenant)
            return result[0]['scheduled_at']
        app = Celery('schedule-test', broker='memory://')
        app.conf.update(task_always_eager=True, task_eager_propagates=True)
        task = app.task(base=EvaluationTask)(scheduled)
        with activate(ctx):
            self.assertEqual(scheduled(), task.delay().get())
        with self.assertRaises(ValueError):
            scheduled()  # Real date is outside the 7-day horizon.

    def test_queue_scope_rejects_unrelated_browser_and_channel(self):
        from evaluate.controls.ownership import assert_session
        ctx, _ = context_for(self.lease, self.identity, 'request')
        with activate(ctx):
            assert_session(self.tenant.pk, self.browser.session_key, 'website')
            with self.assertRaises(Blocked):
                assert_session(self.tenant.pk, 'another-customer', 'website')
            with self.assertRaises(Blocked):
                assert_session(self.tenant.pk, self.browser.session_key, 'telegram')

    def test_inspection_is_scoped_read_only_and_redacted(self):
        from orders.models import Customer, CustomerAddress, Order
        from evaluate.controls.inspection import StateInspector
        other = Customer.objects.create(tenant=self.tenant, name='Do not expose', phone='')
        CustomerAddress.objects.create(tenant=self.tenant, customer=other, label='PRIVATE', address_line='PRIVATE')
        CustomerAddress.objects.create(tenant=self.tenant, customer=self.customer, label='Home', address_line='secret raw address',
            components={'postal_code': '122001', 'secret': 'never', 'phone': 'never'})
        Order.objects.create(tenant=self.tenant, customer=other, source='inhouse', total_amount=100)
        inspector = StateInspector(self.provisioner)
        before = self.browser.load()
        snapshot = inspector.snapshot(self.lease, self.identity, None, None, 'setup')
        raw = snapshot.model_dump_json()
        self.assertNotIn('PRIVATE', raw)
        self.assertNotIn('secret raw', raw)
        self.assertNotIn('never', raw)
        self.assertNotIn(self.browser.session_key, raw)
        self.assertEqual(len(snapshot.state['addresses']), 1)
        self.assertEqual(snapshot.state['orders'], [])
        self.assertEqual(before, self.browser.load())
        self.assertIn('provider_receipts', snapshot.unavailable_sections)

    def test_successful_coverage_uses_configured_service_and_fault_restores(self):
        from chatbot_core.logic.cafe.db_utils import verify_delivery_pincode
        self.tenant.meta['serviceable_pincodes'] = ['122001']
        ctx, _ = context_for(self.lease, self.identity, 'request')
        with activate(ctx):
            self.assertTrue(verify_delivery_pincode(self.tenant, '122001'))
            self.assertFalse(verify_delivery_pincode(self.tenant, '122002'))
        with activate(replace(ctx, faults=frozenset({'coverage'}))):
            self.assertIsNone(verify_delivery_pincode(self.tenant, '122001'))
        with activate(ctx):
            self.assertTrue(verify_delivery_pincode(self.tenant, '122001'))

    @override_settings(ROOT_URLCONF='evaluate.controls.tests.urls', JWT_SECRET='offline-jwt-secret-at-least-32-bytes')
    def test_real_http_session_correlation_and_zero_model_usage(self):
        from evaluate.contracts.interfaces import ChatRequest
        from evaluate.contracts.models import Turn
        from evaluate.controls.transport import ApplicationTransport
        from evaluate.controls.usage import ApplicationUsage
        from evaluate.controls.telemetry import observed
        @observed('workflow')
        def route(*args, **kwargs):
            self.assertEqual(current().request_id, 'http-request')
            self.assertEqual(business_now().year, 2032)
            return 'deterministic reply', []
        transport = ApplicationTransport(self.provisioner)
        request = ChatRequest(self.identity, 'http-request', Turn(original_turn_index=0, user_turn_index=0,
            text='hello', intent='general', sub_intent='greeting', expected_facts=[], must_not=[]))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'application.jsonl'
            handler = EvidenceHandler(path, self.identity.run_id)
            logger.addHandler(handler)
            old = logger.level
            logger.setLevel('INFO')
            try:
                with patch('chatbot_core.channels.website.route_message_for_tenant', side_effect=route):
                    response = transport.send(self.lease, request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.response_text, 'deterministic reply')
                usage = ApplicationUsage(self.provisioner, lambda lease: self.identity, [path])
                self.assertEqual(usage.usage(self.lease, 'http-request').tokens, 0)
                self.assertIsNone(usage.usage(self.lease, 'missing-request'))
                self.assertNotIn('llm.started', path.read_text())
            finally:
                logger.removeHandler(handler)
                handler.close()
                logger.setLevel(old)
                transport.close(self.lease)
        self.assertIsNone(current())

    def test_reviewed_actions_are_deduplicated_and_success_removes_fault(self):
        from evaluate.controls.actions import ApplicationControls
        from evaluate.contracts.models import ScenarioAction, ScenarioPlan, ReviewedScenario
        from evaluate.identity import canonical_hash
        actions = [ScenarioAction(action_id='action-' + str(i), scenario_id=self.identity.scenario_id,
            requirement_ref='/requirement', requirement_hash='a' * 64, review_ref='test-review',
            operation={'kind': 'lookup_control', 'service': 'coverage', 'outcome': outcome})
            for i, outcome in enumerate(['unavailable', 'success'])]
        plan = ScenarioPlan(version='test', contract_hash='a' * 64, default_clock=self.owner['clock'],
            profiles={}, scenarios={self.identity.scenario_id: ReviewedScenario(
                source_hash='a' * 64, review_ref='test', actions=actions)})
        self.owner['action_ledger'] = {}
        self.owner['plan_hash'] = canonical_hash(plan.model_dump())
        self.provisioner.save_owner(self.tenant, self.owner)
        controls = ApplicationControls(self.provisioner, plan)
        first = controls.apply(self.lease, self.identity, actions[0])
        self.assertEqual(first, controls.apply(self.lease, self.identity, actions[0]))
        self.assertIn('coverage', context_for(self.lease, self.identity, 'request')[0].faults)
        controls.apply(self.lease, self.identity, actions[1])
        self.assertFalse(context_for(self.lease, self.identity, 'request')[0].faults)
        with self.assertRaises(Blocked):
            controls.apply(self.lease, self.identity, actions[0].model_copy(update={'review_ref': 'changed'}))

    def test_commerce_command_correlation_keeps_transport_time_real(self):
        from commerce.models import Location, Connection
        from commerce.services import enqueue
        from commerce.queue import claim
        location = Location.objects.create(tenant=self.tenant, code='test', name='test')
        connection = Connection.objects.create(location=location, role='pos', provider='custom',
            account_id='test', environment='test', capabilities=['catalog.read'], secret_ref='managed:test-controls')
        ctx, _ = context_for(self.lease, self.identity, 'request')
        with activate(ctx), self.assertLogs('evaluate.telemetry'):
            command = enqueue(connection, 'catalog.read', 'test-command', None, {})
            self.assertLess(abs((command.available_at - datetime.now(timezone.utc)).total_seconds()), 2)
        with self.assertLogs('evaluate.telemetry') as logs:
            result = claim(connection)
        self.assertEqual(result[0]['command_id'], str(command.pk))
        self.assertEqual(result[0]['data'], {})
        self.assertEqual(logs.records[0].evaluation['request_id'], 'request')
        self.assertLess(abs((datetime.fromisoformat(result[0]['lease_until']) - datetime.now(timezone.utc)).total_seconds()), 125)
