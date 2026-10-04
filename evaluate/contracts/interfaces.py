"""Synchronous component boundaries. No Django, network, or model imports."""
from dataclasses import dataclass
from typing import Protocol

from .models import (
    AssertionResult, EvaluationSummary, ExecutionEvent, ExecutionIdentity,
    NormalizedScenario, RunConfiguration, RunManifest, ScenarioAction,
    StateSnapshot, Turn, TurnEvidence,
)


class Blocked(RuntimeError):
    """A prerequisite/capability is missing. Diagnostic must be safe to persist."""


@dataclass(frozen=True)
class Lease:
    """Opaque process-local resource handle; never serialized as configuration.

    Implementations own cookie jars, tenant credentials and cleanup ownership in
    private storage keyed by handle. A lease must identify newly created resources.
    """
    handle: str
    scenario_instance_id: str


@dataclass(frozen=True)
class ChatRequest:
    identity: ExecutionIdentity
    request_id: str
    turn: Turn


@dataclass(frozen=True)
class ChatResponse:
    status_code: int | None
    response_text: str | None
    elapsed_ms: float
    transport_error: str | None = None


class Provisioner(Protocol):
    def provision(self, config: RunConfiguration, scenario: NormalizedScenario,
                  identity: ExecutionIdentity) -> Lease:
        """Create isolated owned resources; verify requirements or raise Blocked.

        Roll back partial creation on failure. Never adopt an existing tenant.
        """
        ...

    def cleanup(self, lease: Lease) -> None:
        """Idempotently remove only resources created by this lease."""
        ...


class ScenarioControls(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    def apply(self, lease: Lease, identity: ExecutionIdentity,
              action: ScenarioAction) -> ExecutionEvent:
        """Execute a reviewed typed action at its boundary, verify and log it.

        Deduplicate by instance/attempt/action_id. No evaluation of source text.
        Unsupported operations raise Blocked before any mutation.
        """
        ...


class HTTPTransport(Protocol):
    def send(self, lease: Lease, request: ChatRequest) -> ChatResponse:
        """Send precisely one user message, preserving this lease's cookie jar.

        Never automatically retry an ambiguous POST: the website API has no
        request idempotency guarantee. request_id is evaluator-side correlation.
        """
        ...

    def close(self, lease: Lease) -> None: ...


class StateInspector(Protocol):
    def snapshot(self, lease: Lease, identity: ExecutionIdentity,
                 original_turn_index: int | None, request_id: str | None,
                 phase: str) -> StateSnapshot:
        """Read scoped authoritative state; unavailable sections are explicit.

        Project basket, addresses, draft/order/payment and provider receipts.
        Never infer persisted state from response text or mutate to force a pass.
        """
        ...


class EvidenceWriter(Protocol):
    def write(self, artifact: RunManifest | ExecutionEvent | TurnEvidence |
              StateSnapshot | AssertionResult | EvaluationSummary) -> None:
        """Validate and redact before durable append; identical IDs deduplicate.

        Changed content under an existing event ID is an error. Reject secrets,
        credentials, cookie/header material and unredacted personal data.
        """
        ...

    def flush(self) -> None:
        """Durability boundary; failure aborts execution, never drops evidence."""
        ...


class Evaluator(Protocol):
    def evaluate(self, scenario: NormalizedScenario, turn: Turn,
                 evidence: TurnEvidence, snapshots: list[StateSnapshot]) -> list[AssertionResult]:
        """Assess expected facts and must_not with explicit supporting evidence.

        Missing required state => BLOCKED. Reference replies are never exact-match
        targets. Model-backed judges require a separate explicitly authorized run.
        """
        ...
