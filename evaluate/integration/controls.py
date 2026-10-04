"""One ScenarioControls adapter. Application faults and fixture actions share the ledger."""
from __future__ import annotations

import logging

from evaluate.integration.location import evaluation_location_provider

from evaluate.contracts.interfaces import Blocked
from evaluate.controls.actions import ApplicationControls
from evaluate.integration.routing import APPLICATION_LOOKUPS, control_lane
from evaluate.scenarios.controls import DatasetControls

logger = logging.getLogger(__name__)


class RoutedControls(DatasetControls):
    """Clock and classifier/coverage faults update the signed context.

    Catalog, payment, reconnect, delivery-fee, seed and geocoding actions use
    the fixture adapter. Geocoding faults are sticky defaults on the location
    emulator when one was supplied (a one-shot queue is not enough for s118).
    This class does not enter `LocalRuntime.turn()`.
    """

    def __init__(self, provisioner, plan, evidence_writer=None, location=None) -> None:
        super().__init__(provisioner, plan, evidence_writer)
        self.location = location

    def capabilities(self):
        return super().capabilities() | frozenset({"freeze_clock", "lookup_control"})

    def mutate(self, lease, tenant, owner, op) -> None:
        owner.setdefault("lookup", {})
        if (evaluation_location_provider() == "openstreetmap"
                and op.kind == "lookup_control"
                and op.service in {"geocoding", "reverse_geocoding"}
                and op.outcome != "success"):
            raise Blocked("Synthetic geocoding faults require the emulator; live OpenStreetMap cannot inject them")
        if control_lane(op.kind, getattr(op, "service", None)) == "application":
            self._mutate_application(owner, op)
            return
        super().mutate(lease, tenant, owner, op)
        self._mirror_location(tenant, op)

    def evidence(self, tenant, owner):
        projected = super().evidence(tenant, owner)
        projected["application_controls"] = owner.get("application_controls", {})
        return projected

    @staticmethod
    def _mutate_application(owner, op) -> None:
        if op.kind == "lookup_control" and (op.service, op.outcome) not in APPLICATION_LOOKUPS:
            raise Blocked("This application lane does not implement that lookup control")
        ApplicationControls.mutate(None, None, None, owner, op)
        if op.kind == "freeze_clock":
            owner["clock"] = op.clock.model_dump()
            return
        owner["lookup"][op.service] = op.outcome

    def _mirror_location(self, tenant, op) -> None:
        if self.location is None or op.kind != "lookup_control":
            return
        if op.service not in {"geocoding", "reverse_geocoding"}:
            return
        # Sticky default so postal_mismatch covers every lookup until restored.
        # Success controls stay postal_code-free; the restored pin comes from fixtures.
        postal = op.postal_code if op.outcome == "postal_mismatch" else None
        try:
            self.location.set_location_default(tenant.slug, op.service, op.outcome, postal)
        except (OSError, ValueError) as exc:
            logger.warning("Location emulator rejected a reviewed lookup",
                           extra={"service": op.service, "error": type(exc).__name__})
            raise Blocked("Location emulator did not accept the reviewed lookup control") from exc
