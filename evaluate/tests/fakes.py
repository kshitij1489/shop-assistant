"""In-process fake adapters for the provisioning, controls, state and scoring ports."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from uuid import uuid4

from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.contracts.models import (
    AssertionResult, Clock, ExecutionEvent, ExecutionIdentity, Issue, ModelNames, NormalizedScenario, ReferenceTurn,
    RunConfiguration, ScenarioAction, Setup, StateSnapshot, Turn, TurnEvidence,
)
from evaluate.identity import canonical_hash
from evaluate.runner.ports import WebsiteCredential
from evaluate.tests.chat_server import ChatServer

TENANT = "qa-tenant"
API_KEY = "qa-tenant-private-api-key-0123456789"
PLAN_VERSION = "baseline-v1"


def make_config(server: ChatServer, timeout_seconds: float = 0.5, repetitions: int = 1, run_id: str = "test-run") -> RunConfiguration:
    return RunConfiguration(run_id=run_id, base_url=server.url, scenario_plan_version=PLAN_VERSION,
                            models=ModelNames(chat="chat-model", translate="translate-model", analytics="analytics-model"),
                            timeout_seconds=timeout_seconds, repetitions=repetitions)


def make_turn(original: int, user: int, text: str, **extra) -> Turn:
    return Turn(original_turn_index=original, user_turn_index=user, text=text, intent="menu", sub_intent="price",
                expected_facts=[f"fact for {text}"], must_not=["hallucinated prices"], **extra)


def make_scenario(source_id: str, texts: list[str], references: dict[int, str] | None = None,
                  answers: dict[int, int] | None = None, actions: list[ScenarioAction] | None = None,
                  setup: Setup | None = None, blockers: bool = False) -> NormalizedScenario:
    """Build a normalized scenario; `references` maps original index -> asks text."""
    references = references or {}
    answers = answers or {}
    turns, reference_turns, user_index = [], [], 0
    total = len(texts) + len(references)
    text_iter = iter(texts)
    for original in range(total):
        if original in references:
            reference_turns.append(ReferenceTurn(original_turn_index=original, text="reference reply", asks=references[original]))
            continue
        extra = {"answers_ask_from_turn": answers[user_index]} if user_index in answers else {}
        turns.append(make_turn(original, user_index, next(text_iter), **extra))
        user_index += 1
    sid = f"sessions:{source_id}"
    return NormalizedScenario(
        scenario_id=sid, source_id=source_id, namespace="sessions", source_hash="a" * 64,
        scenario_plan_version=PLAN_VERSION, summary=source_id, tags=["test"], setup_profiles=["knowledge_only"],
        clock=Clock(at="2026-09-29T14:00:00+05:30"), setup=setup or Setup(catalog="none"), setup_inputs=["k.json"],
        turns=turns, references=reference_turns, knowledge_refs=[], workflow_refs=[],
        actions=[a.model_copy(update={"scenario_id": sid}) for a in (actions or [])],
        blockers=[Issue(code="unmapped_requirement", location=sid, message="test blocker")] if blockers else [],
    )


def make_action(action_id: str, operation: dict, original_turn_index: int | None = None) -> ScenarioAction:
    return ScenarioAction(action_id=action_id, scenario_id="sessions:placeholder", original_turn_index=original_turn_index,
                          requirement_ref="/preconditions/0", requirement_hash=canonical_hash("requirement"),
                          review_ref="test", operation=operation)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeProvisioner:
    """Owns per-lease tenant credentials privately; implements `CredentialResolver` too."""

    def __init__(self, server: ChatServer, fail: bool = False, block: bool = False) -> None:
        server.state.register_tenant(TENANT, API_KEY)
        self.fail, self.block = fail, block
        self.leases: dict[str, WebsiteCredential] = {}
        self.cleaned: list[str] = []
        self.provisioned: list[ExecutionIdentity] = []
        self._lock = threading.Lock()

    def provision(self, config, scenario, identity) -> Lease:
        if self.block:
            raise Blocked("tenant fixtures unavailable in this environment")
        if self.fail:
            raise RuntimeError("database unreachable while provisioning")
        lease = Lease(handle=uuid4().hex, scenario_instance_id=identity.scenario_instance_id)
        with self._lock:
            self.leases[lease.handle] = WebsiteCredential(TENANT, API_KEY)
            self.provisioned.append(identity)
        return lease

    def resolve(self, lease: Lease) -> WebsiteCredential:
        with self._lock:
            return self.leases[lease.handle]

    def cleanup(self, lease: Lease) -> None:
        with self._lock:
            self.leases.pop(lease.handle, None)
            self.cleaned.append(lease.handle)


class FakeControls:
    def __init__(self, block_ids: set[str] | None = None, raise_ids: set[str] | None = None) -> None:
        self.block_ids, self.raise_ids = block_ids or set(), raise_ids or set()
        self.applied: list[tuple[str, str]] = []  # (instance, action_id)

    def capabilities(self) -> frozenset[str]:
        return frozenset({"freeze_clock", "seed_fixture", "payment_control", "catalog_control"})

    def apply(self, lease, identity, action) -> ExecutionEvent:
        if action.action_id in self.block_ids:
            raise Blocked(f"capability missing for {action.operation.kind}")
        if action.action_id in self.raise_ids:
            raise RuntimeError("controller crashed with Authorization: Bearer eyJabcdefghijk.lmnopqrstuvwx.yz0123456789")
        self.applied.append((identity.scenario_instance_id, action.action_id))
        return ExecutionEvent(run_id=identity.run_id, scenario_id=identity.scenario_id,
                              scenario_instance_id=identity.scenario_instance_id, attempt=identity.attempt,
                              event_id=f"evt-action-{uuid4().hex}", occurred_at=_now(), kind="action",
                              action_id=action.action_id, original_turn_index=action.original_turn_index,
                              status="succeeded", detail=f"applied {action.operation.kind}")


@dataclass
class FakeInspector:
    """Projects `evaluate-runner/v1` state; tests script pending questions and payment sequences."""
    server: ChatServer
    pending_question: str | None = None
    payment_sequence: list[str] = field(default_factory=list)  # consumed per snapshot
    chat_unavailable: bool = False
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _payment_by_lease: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def snapshot(self, lease, identity, original_turn_index, request_id, phase) -> StateSnapshot:
        with self._lock:
            self.calls += 1
            sequence = self._payment_by_lease.setdefault(lease.handle, list(self.payment_sequence))
            payment = sequence.pop(0) if len(sequence) > 1 else (sequence[0] if sequence else "none")
        state = {"projection_version": "evaluate-runner/v1", "payment": {"status": payment},
                 "chat": {"pending_question": self.pending_question}, "phase": phase}
        return StateSnapshot(run_id=identity.run_id, scenario_id=identity.scenario_id,
                             scenario_instance_id=identity.scenario_instance_id, attempt=identity.attempt,
                             event_id=f"evt-snap-{uuid4().hex}", snapshot_id=f"snap-{uuid4().hex}",
                             original_turn_index=original_turn_index, request_id=request_id, phase=phase,
                             captured_at=_now(), state=state,
                             unavailable_sections=["chat"] if self.chat_unavailable else [])


class FakeEvaluator:
    def __init__(self, criterion: str = "reply echoes the user message") -> None:
        self.criterion = criterion

    def evaluate(self, scenario, turn, evidence: TurnEvidence, snapshots) -> list[AssertionResult]:
        reply = evidence.response_text or ""
        outcome = "PASS" if reply.startswith("echo:") and "[fail]" not in reply else "FAIL"
        if evidence.http_status != 200:
            outcome = "FAIL"
        return [AssertionResult(run_id=evidence.run_id, scenario_id=evidence.scenario_id,
                                scenario_instance_id=evidence.scenario_instance_id, attempt=evidence.attempt,
                                event_id=f"evt-assert-{uuid4().hex}", assertion_id=f"assert-{uuid4().hex}",
                                original_turn_index=turn.original_turn_index, request_id=evidence.request_id,
                                evaluator="fake-echo", outcome=outcome, criterion=self.criterion,
                                explanation="echo reply observed" if outcome == "PASS" else "no echo reply",
                                evidence_ids=[evidence.event_id, *evidence.snapshot_ids])]
