"""Runner-side protocols.

Every port is synchronous and process-local. None accepts or returns a
serializable credential container.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from evaluate.contracts.interfaces import Lease
from evaluate.contracts.models import NormalizedScenario, ReferenceTurn, ScenarioAction, StateSnapshot, Turn

BranchState = Literal["matched", "mismatch", "unknown"]


@dataclass(frozen=True, repr=False)
class WebsiteCredential:
    """Tenant slug plus private API key. Never logged, hashed or serialized."""
    tenant_slug: str
    api_key: str

    def __repr__(self) -> str:
        return f"WebsiteCredential(tenant_slug={self.tenant_slug!r}, api_key=****)"


class CredentialResolver(Protocol):
    def resolve(self, lease: Lease) -> WebsiteCredential:
        """Return the tenant credential owned by this lease or raise `Blocked`."""
        ...


class BranchOracle(Protocol):
    def pending_question(self, snapshot: StateSnapshot, reference: ReferenceTurn,
                         previous_reply: str | None) -> BranchState:
        """Decide whether the documented clarification capability is currently open.

        Return "unknown" when the evidence cannot decide; never guess "matched".
        """
        ...


class SettlementPolicy(Protocol):
    def pending_sections(self, snapshot: StateSnapshot) -> list[str]:
        """Name asynchronous sections (payment, POS...) that have not settled."""
        ...

    def fingerprint(self, snapshot: StateSnapshot) -> str:
        """Stable summary of the asynchronous sections used to detect transitions."""
        ...


class ContinuationPlanner(Protocol):
    def plan(self, scenario: NormalizedScenario, turn: Turn,
             reference: ReferenceTurn) -> ScenarioAction | None:
        """Return a reviewed `seed_fixture` action establishing the pending state, or None."""
        ...


@dataclass(frozen=True)
class Usage:
    tokens: int | None
    cost_minor: int | None
    currency: Literal["INR", "USD"] | None = None


class UsageSource(Protocol):
    def usage(self, lease: Lease, request_id: str) -> Usage | None:
        """Actual provider usage for one request when the application exposes it."""
        ...
