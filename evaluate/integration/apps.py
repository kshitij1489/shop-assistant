"""Install the process journal and catalog projection when evaluation is enabled."""
from pathlib import Path

from django.apps import AppConfig


class IntegrationConfig(AppConfig):
    name = "evaluate.integration"
    label = "evaluate_integration"

    def ready(self) -> None:
        from django.conf import settings
        if getattr(settings, "EVALUATION_ENABLED", False) is True:
            install_catalog_projection()
            allow_evaluation_emulator_hostname()
            from .health import runtime_configuration
            runtime_configuration()
        root = getattr(settings, "EVALUATION_EVIDENCE_ROOT", "")
        if root and getattr(settings, "EVALUATION_ENABLED", False):
            import logging
            from evaluate.controls.telemetry import RoutedEvidenceHandler
            logger = logging.getLogger("evaluate.telemetry")
            if not any(isinstance(h, RoutedEvidenceHandler) for h in logger.handlers):
                logger.addHandler(RoutedEvidenceHandler(root))
                logger.setLevel(logging.INFO)
                logger.propagate = False


def allow_evaluation_emulator_hostname() -> None:
    """Permit Compose service DNS for LOCATION_EMULATOR_URL under evaluation.

    Emulator clients normally require a loopback origin. Docker Desktop cannot
    use host networking, so website/worker reach the emulator by service name.
    Host-side runners keep publishing 127.0.0.1:9080.
    """
    from urllib.parse import urlsplit

    from django.conf import settings

    host = urlsplit(getattr(settings, "LOCATION_EMULATOR_URL", "") or "").hostname
    if not host or host in {"127.0.0.1", "localhost", "::1"}:
        return

    import mock_services.client as mock_client

    original_init = mock_client.MockClient.__init__
    if getattr(original_init, "_evaluation_compose_host", False):
        return

    def patched_init(self, base_url="http://127.0.0.1:9080", timeout=15):
        parsed = urlsplit(base_url)
        allowed = {"127.0.0.1", "localhost", host}
        if (parsed.scheme != "http" or parsed.hostname not in allowed
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Use a loopback HTTP mock origin, such as http://127.0.0.1:9080.")
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    patched_init._evaluation_compose_host = True  # type: ignore[attr-defined]
    mock_client.MockClient.__init__ = patched_init  # type: ignore[method-assign]


def install_catalog_projection() -> None:
    """Serve available_quantity=None for evaluation-owned unknown inventory.

    MenuItem.quantity is a legacy placeholder (often 0). The HTTP lane never
    enters LocalRuntime.turn(), so patch the same generate_all_menu_payload
    boundary that turn() uses. Synthetic stock is never a public fact.
    """
    from importlib import import_module

    knowledge = import_module("chatbot_core.knowledge_cache")
    if getattr(knowledge.generate_all_menu_payload, "_evaluation_catalog_hook", False):
        return
    original = knowledge.generate_all_menu_payload

    def projected(api_key=None):
        result = original(api_key)
        from chatbot_core.models import TenantInfo
        from evaluate.fixtures.provision import OWNER_KEY

        keys = [api_key] if api_key is not None else list(result)
        for key in keys:
            items = result.get(key)
            if not items:
                continue
            tenant = TenantInfo.objects.filter(api_key=key).first()
            if tenant is None:
                continue
            owner = (tenant.meta or {}).get(OWNER_KEY)
            if not owner:
                continue
            for item in items.values():
                item["available_quantity"] = None
        return result

    projected._evaluation_catalog_hook = True  # type: ignore[attr-defined]
    projected._evaluation_catalog_original = original  # type: ignore[attr-defined]
    knowledge.generate_all_menu_payload = projected
