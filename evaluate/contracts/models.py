"""Canonical v1 contracts; export JSON Schemas with `python -m evaluate schemas`."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$", min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Index = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(ge=1)]
Text = Annotated[str, Field(min_length=1)]
ProfileName = Literal["knowledge_only", "catalog_sandbox", "address_sandbox", "checkout_sandbox"]
# Legacy single status used by EvaluationSummary and existing callers.
Outcome = Literal["PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED", "COMPLETED", "NEEDS_REVIEW", "INTERRUPTED"]
# Split axes: how far the script ran vs how scoring judged it.
ExecutionStatus = Literal["COMPLETED", "BLOCKED", "ERROR", "INTERRUPTED", "SKIPPED"]
EvaluationVerdict = Literal["PASS", "FAIL", "BLOCKED", "NEEDS_REVIEW"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class Artifact(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"


class Clock(StrictModel):
    at: str
    timezone: str = "Asia/Kolkata"

    @model_validator(mode="after")
    def valid_clock(self):
        value = datetime.fromisoformat(self.at)
        zone = ZoneInfo(self.timezone)
        if value.tzinfo is None or value.utcoffset() != value.astimezone(zone).utcoffset():
            raise ValueError("clock requires an explicit offset matching its timezone")
        return self


class ModelNames(StrictModel):
    chat: ID
    translate: ID
    analytics: ID
    judge: ID | None = None


class RunConfiguration(Artifact):
    run_id: ID
    base_url: str = "http://127.0.0.1:8000"
    dataset_directory: Text = "test_data"
    scenario_plan_version: ID
    models: ModelNames
    seed: Index = 0
    repetitions: Positive = 1
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    isolation: Literal["disposable_tenant"] = "disposable_tenant"

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("base_url must be an HTTP origin/path without credentials, query or fragment")
        return value.rstrip("/")


class ApplicationRevision(StrictModel):
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40,64}$")]
    working_tree_fingerprint: Digest
    dirty: bool
    fingerprint_policy: Literal["git-tracked-and-unignored-v1"] = "git-tracked-and-unignored-v1"


class RunManifest(Artifact):
    run_id: ID
    application: ApplicationRevision
    dataset_hashes: dict[str, Digest]
    configuration_hash: Digest
    scenario_plan_hash: Digest
    scenario_plan_version: ID
    models: ModelNames
    scenario_ids: list[ID]
    created_at: str

    @field_validator("created_at")
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("timestamp must have an offset")
        return value


class Setup(StrictModel):
    """Partial overrides. None means inherit; false/zero/empty lists are explicit."""
    catalog: Literal["none", "published_synthetic"] | None = None
    variant_name: Text | None = None
    stock: Literal["none", "finite_local"] | None = None
    customer: Literal["browser_guest", "authenticated_synthetic"] | None = None
    address_lookup: Literal["unavailable", "complete_supplied_only"] | None = None
    payment: Literal["unavailable", "fake_adapter"] | None = None
    payment_methods: list[Literal["cash", "online"]] | None = None
    modes: list[Literal["pickup", "delivery", "dine_in"]] | None = None
    scheduling: bool | None = None
    horizon_days: Index | None = None
    lead_minutes: Index | None = None
    preparation_minutes: Index | None = None
    delivery_minimum_minor: Index | None = None
    delivery_fee_minor: Index | None = None
    pickup_minimum_minor: Index | None = None
    pickup_fee_minor: Index | None = None
    tax_basis_points: Index | None = None
    discounts: bool | None = None
    modifiers: bool | None = None
    currency: Literal["INR"] | None = None
    hours: Literal["tuesday_sunday_12_00_23_30"] | None = None
    contact_fixture: Literal["qa_guest_dummy_no_notifications"] | None = None
    required_pickup: list[Literal["name", "phone"]] | None = None
    required_delivery: list[Literal["name", "phone", "address", "postal_code"]] | None = None
    allowed_postal_codes: list[Annotated[str, Field(pattern=r"^\d{6}$")]] | None = None


class ProfileDefinition(StrictModel):
    source_hash: Digest
    review_ref: Text
    extends: list[ProfileName] = Field(default_factory=list)
    defaults: Setup
    required_capabilities: list[ID] = Field(default_factory=list)


class FreezeClock(StrictModel):
    kind: Literal["freeze_clock"]
    clock: Clock


class LookupControl(StrictModel):
    kind: Literal["lookup_control"]
    service: Literal["coverage", "geocoding", "reverse_geocoding", "classification"]
    outcome: Literal["unavailable", "success", "postal_mismatch", "timeout"]
    postal_code: Annotated[str, Field(pattern=r"^\d{6}$")] | None = None

    @model_validator(mode="after")
    def mismatch_requires_postal(self):
        if self.outcome == "postal_mismatch" and (self.service != "geocoding" or self.postal_code is None):
            raise ValueError("postal mismatch requires geocoding and a postal code")
        return self


class CatalogControl(StrictModel):
    kind: Literal["catalog_control"]
    item_name: Text
    variant_name: Text
    price_minor: Index | None = None
    available: bool | None = None

    @model_validator(mode="after")
    def changed_field(self):
        if self.price_minor is None and self.available is None:
            raise ValueError("catalog_control requires a price or availability")
        return self


class PaymentControl(StrictModel):
    kind: Literal["payment_control"]
    operation: Literal["capture", "fail", "cancel", "timeout_creation", "timeout_after_creation", "restore_and_reconcile"]
    target: Literal["active_order"] = "active_order"
    amount_minor: Positive | None = None
    currency: Literal["INR"] = "INR"

    @model_validator(mode="after")
    def capture_requires_amount(self):
        if self.operation == "capture" and self.amount_minor is None:
            raise ValueError("capture requires an expected amount")
        return self


class Reconnect(StrictModel):
    kind: Literal["reconnect"]
    evict_session_cache: Literal[True] = True
    retain_database_and_identity: Literal[True] = True


class SetDeliveryFee(StrictModel):
    kind: Literal["set_delivery_fee"]
    amount_minor: Index


class SeedFixture(StrictModel):
    kind: Literal["seed_fixture"]
    fixture_id: ID
    fixture_hash: Digest


Operation = Annotated[
    FreezeClock | LookupControl | CatalogControl | PaymentControl | Reconnect | SetDeliveryFee | SeedFixture,
    Field(discriminator="kind"),
]


class ScenarioAction(Artifact):
    action_id: ID
    scenario_id: ID
    original_turn_index: Index | None = None  # None = before the first user turn
    requirement_ref: Text  # JSON pointer into source; never executable text
    requirement_hash: Digest
    review_ref: Text
    operation: Operation


class ReviewedScenario(StrictModel):
    source_hash: Digest
    review_ref: Text
    overrides: Setup = Field(default_factory=Setup)
    actions: list[ScenarioAction] = Field(default_factory=list)
    # Each natural-language requirement explicitly maps to one or more action IDs.
    requirements: dict[str, Annotated[list[ID], Field(min_length=1)]] = Field(default_factory=dict)


class ScenarioPlan(Artifact):
    version: ID
    contract_hash: Digest
    default_clock: Clock
    profiles: dict[ProfileName, ProfileDefinition]
    scenarios: dict[ID, ReviewedScenario] = Field(default_factory=dict)
    supported_capabilities: list[ID] = Field(default_factory=list)
    fixture_hashes: dict[ID, Digest] = Field(default_factory=dict)


class Turn(StrictModel):
    original_turn_index: Index
    user_turn_index: Index
    text: str  # Empty input probes are valid dataset entries.
    intent: Text
    sub_intent: Text
    turn_kind: str = "question"
    expected_facts: list[str]
    must_not: list[str]
    parts: list[dict[str, str]] = Field(default_factory=list)
    answers_ask_from_turn: Index | None = None
    ignores_ask_from_turn: Index | None = None
    branch_dependency: Literal["required", "context_only"] = "required"
    allow_empty_rejection: bool = False


class PendingTaskExpectation(StrictModel):
    """One reviewed internal representation of a clarification capability."""
    intent_type: Text
    sub_intents: list[Text] = Field(min_length=1)


class PendingExpectation(PendingTaskExpectation):
    """Reviewed clarification capability; question phrasing is not an execution contract."""
    equivalent_tasks: list[PendingTaskExpectation] = Field(default_factory=list)


class ReferenceTurn(StrictModel):
    original_turn_index: Index
    text: str
    asks: str | None = None
    pending: PendingExpectation | None = None
    reference_only: Literal[True] = True


class Issue(StrictModel):
    code: ID
    location: str
    message: str


class NormalizedScenario(Artifact):
    scenario_id: ID
    source_id: ID
    namespace: Literal["sessions", "qa"]
    source_hash: Digest
    scenario_plan_version: ID
    priority: Literal["P0", "P1", "P2"] = "P1"
    summary: str
    tags: list[str]
    setup_profiles: list[ProfileName]
    clock: Clock
    setup: Setup
    setup_inputs: list[str]
    turns: Annotated[list[Turn], Field(min_length=1)]
    references: list[ReferenceTurn]
    knowledge_refs: list[str]
    workflow_refs: list[str]
    actions: list[ScenarioAction]
    blockers: list[Issue]

    @model_validator(mode="after")
    def coherent_identity(self):
        if self.scenario_id != f"{self.namespace}:{self.source_id}":
            raise ValueError("scenario ID must use its source namespace")
        indexes = [t.original_turn_index for t in self.turns]
        all_indexes = indexes + [t.original_turn_index for t in self.references]
        if (indexes != sorted(indexes) or len(set(all_indexes)) != len(all_indexes)
                or sorted(all_indexes) != list(range(len(all_indexes)))
                or [t.user_turn_index for t in self.turns] != list(range(len(self.turns)))):
            raise ValueError("turn indexes must preserve the original sequence")
        action_ids = [a.action_id for a in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("duplicate actions")
        for action in self.actions:
            if action.scenario_id != self.scenario_id or (action.original_turn_index is not None and action.original_turn_index not in indexes):
                raise ValueError("action must target this scenario and a user turn")
        return self


class ExecutionIdentity(Artifact):
    run_id: ID
    scenario_id: ID
    scenario_instance_id: ID
    attempt: Positive


class ExecutionEvent(ExecutionIdentity):
    event_id: ID
    occurred_at: str
    kind: Literal["provision", "action", "request", "response", "snapshot", "assertion", "branch_mismatch", "cleanup", "error"]
    original_turn_index: Index | None = None
    user_turn_index: Index | None = None
    request_id: ID | None = None
    action_id: ID | None = None
    status: Literal["started", "succeeded", "failed", "blocked"]
    detail: str  # Redacted diagnostic, never raw exception/header serialization.

    _timestamp = field_validator("occurred_at")(RunManifest.timestamp.__func__)

    @model_validator(mode="after")
    def request_correlation(self):
        if self.kind in {"request", "response"} and (self.request_id is None or self.original_turn_index is None or self.user_turn_index is None):
            raise ValueError("request/response events require request and turn identity")
        if self.kind == "action" and self.action_id is None:
            raise ValueError("action events require action_id")
        return self


class TurnEvidence(ExecutionIdentity):
    event_id: ID
    original_turn_index: Index
    user_turn_index: Index
    request_id: ID
    message: str  # Logical dataset input, before recorded fixture bindings.
    sent_message: str | None = None  # Actual HTTP message, if fixture IDs were bound.
    response_text: str | None
    response_error: str | None = None  # Sanitized HTTP error body; never an assistant reply.
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    elapsed_ms: Annotated[float, Field(ge=0)]
    snapshot_ids: list[ID]
    branch: Literal["matched", "mismatch", "not_applicable"]
    transport_error: Literal["timeout", "connection", "invalid_response"] | None = None
    warm_up: bool = False


class StateSnapshot(ExecutionIdentity):
    event_id: ID
    snapshot_id: ID
    original_turn_index: Index | None
    request_id: ID | None
    phase: Literal["before", "after", "setup", "cleanup"]
    captured_at: str
    # Redacted application-owned projections; never ORM dumps or credentials.
    state: dict[str, JsonValue]
    unavailable_sections: list[str]

    _timestamp = field_validator("captured_at")(RunManifest.timestamp.__func__)


class AssertionResult(ExecutionIdentity):
    event_id: ID
    assertion_id: ID
    original_turn_index: Index | None
    request_id: ID | None
    evaluator: ID
    outcome: Outcome
    criterion: str
    explanation: str
    evidence_ids: list[ID]
    execution_status: ExecutionStatus | None = None
    evaluation_verdict: EvaluationVerdict | None = None


class ScenarioResult(StrictModel):
    scenario_id: ID
    scenario_instance_id: ID
    attempt: Positive
    outcome: Outcome
    assertion_ids: list[ID]
    execution_status: ExecutionStatus | None = None
    evaluation_verdict: EvaluationVerdict | None = None


class EvaluationSummary(Artifact):
    run_id: ID
    results: list[ScenarioResult]
    counts: dict[Outcome, Index]

    @model_validator(mode="after")
    def counts_match(self):
        expected = {key: sum(r.outcome == key for r in self.results) for key in self.counts}
        if expected != self.counts or sum(self.counts.values()) != len(self.results):
            raise ValueError("summary counts must match results")
        keys = [(r.scenario_instance_id, r.attempt) for r in self.results]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate scenario instance/attempt")
        return self


SCHEMAS = {
    "run_configuration": RunConfiguration, "run_manifest": RunManifest,
    "normalized_scenario": NormalizedScenario, "scenario_action": ScenarioAction,
    "execution_event": ExecutionEvent, "turn_evidence": TurnEvidence,
    "state_snapshot": StateSnapshot, "assertion_result": AssertionResult,
    "evaluation_summary": EvaluationSummary, "scenario_plan": ScenarioPlan,
}
