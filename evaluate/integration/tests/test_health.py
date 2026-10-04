"""Offline verification of authenticated runtime probes and per-run telemetry routing."""
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core import signing
from django.http import HttpResponse
from django.test import SimpleTestCase, RequestFactory, override_settings

from evaluate.integration.health import HealthMiddleware, PATH, SALT
from evaluate.controls.telemetry import RoutedEvidenceHandler
from evaluate.evidence.journal import read_journal


@override_settings(EVALUATION_ENABLED=True)
class HealthTests(SimpleTestCase):
    def test_probe_requires_authentication_and_has_no_business_side_effects(self):
        middleware = HealthMiddleware(lambda _: HttpResponse(status=404))
        factory = RequestFactory()
        with patch('evaluate.integration.health.shared_probe', return_value={'models': {'chat': 'actual'}}) as probe:
            self.assertEqual(middleware(factory.get(PATH)).status_code, 403)
            probe.assert_not_called()
            nonce = 'a' * 32
            header = signing.dumps({'nonce': nonce}, salt=SALT)
            response = middleware(factory.get(PATH, HTTP_X_EVALUATION_HEALTH=header))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content), {'models': {'chat': 'actual'}})
            probe.assert_called_once_with(nonce)

    def test_disabled_probe_uses_normal_application_routing(self):
        with override_settings(EVALUATION_ENABLED=False):
            response = HealthMiddleware(lambda _: HttpResponse(status=404))(RequestFactory().get(PATH))
        self.assertEqual(response.status_code, 404)

    def test_journals_are_separated_by_run_and_process(self):
        with TemporaryDirectory() as directory:
            first, second = RoutedEvidenceHandler(directory), RoutedEvidenceHandler(directory)
            try:
                for handler, run in ((first, 'run-one'), (first, 'run-two'), (second, 'run-one')):
                    record = logging.LogRecord('evaluate.telemetry', logging.INFO, '', 0, '', (), None)
                    record.evaluation = {'run_id': run, 'event_id': handler.process + run, 'event': 'test'}
                    handler.emit(record)
                self.assertEqual(len(list((Path(directory) / 'run-one').glob('application-*.jsonl'))), 2)
                self.assertEqual(len(list((Path(directory) / 'run-two').glob('application-*.jsonl'))), 1)
                record.evaluation['run_id'] = '../escape'
                with self.assertRaises(ValueError):
                    first.emit(record)
            finally:
                first.close()
                second.close()
