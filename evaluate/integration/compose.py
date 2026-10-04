"""Build the website-lane adapters. Does not send a chat turn."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from evaluate.integration.location import evaluation_location_provider

from evaluate.contracts.interfaces import Blocked, ChatRequest, Lease
from evaluate.controls.actions import configure_cache
from evaluate.controls.inspection import StateInspector
from evaluate.controls.ownership import HEADER
from evaluate.integration.controls import RoutedControls
from evaluate.integration.credentials import LeaseCredentials
from evaluate.integration.usage import JournalUsage
from evaluate.runner.context import Components
from evaluate.runner.defaults import ProjectionSettlementPolicy, SnapshotBranchOracle
from evaluate.runner.transport import WebsiteChatResponse, WebsiteTransport

logger = logging.getLogger(__name__)


class CacheConfiguringProvisioner:
    """Delegates ownership and records the private cache mode after provision."""

    def __init__(self, provisioner, cache_mode: str = "cold") -> None:
        if cache_mode not in {"cold", "warm"}:
            raise Blocked("Unknown evaluation cache mode")
        self._provisioner = provisioner
        self.cache_mode = cache_mode

    def __getattr__(self, name):
        return getattr(self._provisioner, name)

    def provision(self, config, scenario, identity):
        lease = self._provisioner.provision(config, scenario, identity)
        try:
            configure_cache(self._provisioner, lease, identity, self.cache_mode)
        except Exception:
            logger.warning("Evaluation cache setup failed", extra={"error": "configure_cache"})
            self._release(lease)
            raise
        return lease

    def _release(self, lease) -> None:
        try:
            self._provisioner.cleanup(lease, force=True)
        except Exception:
            logger.warning("Evaluation lease cleanup failed after cache setup", extra={"lease": lease.handle})


class PumpingWebsiteTransport:
    """HTTP transport with an explicit, separately invoked provider progress hook."""

    def __init__(self, transport: WebsiteTransport, provisioner) -> None:
        self._transport = transport
        self._provisioner = provisioner

    def __getattr__(self, name):
        return getattr(self._transport, name)

    def send(self, lease: Lease, request: ChatRequest) -> WebsiteChatResponse:
        _, owner = self._provisioner.owned(lease)
        message = request.turn.text
        for source, target in owner.get("message_bindings", {}).items():
            message = message.replace(source, target)
        bound = replace(request, turn=request.turn.model_copy(update={"text": message}))
        response = self._transport.send(lease, bound)
        return replace(response, sent_message=message)

    def advance(self, lease: Lease) -> None:
        """Drive providers only after the runner has durably saved the response."""
        runtime = getattr(self._provisioner, "runtime", None)
        if runtime is not None and lease.handle in getattr(runtime, "workers", {}):
            runtime.pump(lease)


def build_components(config, evidence_directory: Path, cache_mode: str = "cold") -> Components:
    """Wire provisioner, routed controls, website transport, inspector and usage.

    Scoring stays offline. `evaluator` is unset so a finished chat is recorded
    as `COMPLETED` without a verdict until offline scoring reads it.
    """
    from evaluate.fixtures.provision import DjangoProvisioner
    from evaluate.scenarios.plan import load_plan
    from evaluate.scenarios.runtime import LocalRuntime

    plan, _bundle = load_plan(config.dataset_directory)
    runtime = LocalRuntime(Path(evidence_directory) / "providers")
    provisioner = CacheConfiguringProvisioner(DjangoProvisioner(runtime), cache_mode)
    return Components(
        provisioner=provisioner,
        controls=RoutedControls(provisioner, plan, location=_location_controls()),
        transport=website_transport(config.base_url, config.timeout_seconds, provisioner),
        inspector=StateInspector(provisioner),
        branch_oracle=SnapshotBranchOracle(),
        settlement=ProjectionSettlementPolicy(),
        usage=JournalUsage(provisioner, evidence_directory),
    )


def website_transport(base_url: str, timeout_seconds: float, provisioner) -> PumpingWebsiteTransport:
    """Send the owned session cookie and a fresh `X-Evaluation-Context` ticket."""
    from django.conf import settings
    from evaluate.controls.ownership import ticket

    credentials = LeaseCredentials(provisioner)

    def cookie(lease):
        return credentials.session_key(lease)

    def header(lease, request):
        return ticket(lease, request.identity, request.request_id, provisioner)

    inner = WebsiteTransport(
        base_url, credentials, timeout_seconds,
        session_cookie=(settings.SESSION_COOKIE_NAME, cookie),
        evaluation_header=(HEADER, header),
    )
    return PumpingWebsiteTransport(inner, provisioner)


def _location_controls():
    from django.conf import settings
    if evaluation_location_provider() != "emulator":
        return None
    from mock_services.client import MockClient
    from mock_services.controls import ProviderControls
    url = getattr(settings, "LOCATION_EMULATOR_URL", "http://127.0.0.1:9080")
    return ProviderControls(MockClient(url))
