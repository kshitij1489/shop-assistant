"""Internal development adapter DNS keeps TLS verification and origin limits."""
import ssl
import unittest
from unittest.mock import patch

from evaluate.contracts.interfaces import Blocked
from evaluate.integration.adapter_https import validated_upstream
from evaluate.integration.preflight import _https_local_adapter
from evaluate.scenarios.runtime import _adapter_ssl_context, _validated_adapter_origin


class AdapterNetworkTests(unittest.TestCase):
    def test_accepts_development_service_and_loopback(self):
        for origin in ('https://adapter_https:8443', 'https://127.0.0.1:8443'):
            with self.subTest(origin=origin):
                self.assertTrue(_https_local_adapter(origin))
                self.assertEqual(_validated_adapter_origin(origin), origin)
        self.assertEqual(validated_upstream('http://web:8000'), 'http://web:8000')

    def test_rejects_external_or_credentialed_adapter_origins(self):
        for origin in ('https://example.com', 'http://adapter_https:8443',
                       'https://user:password@adapter_https:8443',
                       'https://adapter_https:8443?redirect=elsewhere'):
            with self.subTest(origin=origin):
                self.assertFalse(_https_local_adapter(origin))
                with self.assertRaises(Blocked):
                    _validated_adapter_origin(origin)

    def test_service_dns_requires_certificate_and_hostname_verification(self):
        with patch.dict('os.environ', {'EVALUATION_ADAPTER_CA': ''}):
            context = _adapter_ssl_context('adapter_https')
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
