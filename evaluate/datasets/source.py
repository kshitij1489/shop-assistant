"""Strict source shapes. Dataset metadata is descriptive, never executable."""
from typing import Literal
from pydantic import Field, model_validator
from evaluate.contracts.models import ID, Index, StrictModel, PendingExpectation


class Part(StrictModel):
    text: str
    intent: str
    sub_intent: str
    role_in_turn: str


class SourceTurn(StrictModel):
    speaker: Literal["user", "assistant"]
    text: str
    reference_only: bool = False
    turn_kind: str = "question"
    intent: str | None = None
    sub_intent: str | None = None
    expected_facts: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    asks: str | None = None
    pending: PendingExpectation | None = None
    answers_ask_from_turn: Index | None = None
    ignores_ask_from_turn: Index | None = None
    branch_dependency: Literal["required", "context_only"] = "required"
    allow_empty_rejection: bool = False
    parts: list[Part] = Field(default_factory=list)

    @model_validator(mode="after")
    def role_fields(self):
        if self.pending is not None and (self.speaker != 'assistant' or not self.asks):
            raise ValueError('pending expectations require an assistant reference question')
        if self.speaker == "assistant" and not self.reference_only:
            raise ValueError("assistant entries must be reference_only")
        if self.speaker == "user" and (self.reference_only or not self.intent or not self.sub_intent):
            raise ValueError("user entries require intent/sub_intent and cannot be reference_only")
        if self.speaker == "user" and not {"expected_facts", "must_not"} <= self.model_fields_set:
            raise ValueError("user entries require explicit assertion lists")
        if (self.branch_dependency == "context_only"
                and self.answers_ask_from_turn is None and self.ignores_ask_from_turn is None):
            raise ValueError("context_only requires an assistant reference")
        if self.allow_empty_rejection and (self.speaker != "user" or self.text.strip()):
            raise ValueError("allow_empty_rejection requires blank user input")
        return self


class BeforeTurn(StrictModel):
    turn_index: Index
    action: str = Field(min_length=1)


class Session(StrictModel):
    id: ID
    shape: str
    patterns: list[str]
    summary: str
    turns: list[SourceTurn] = Field(min_length=1)
    setup_profiles: list[str] = Field(min_length=1)
    priority: Literal["P0", "P1", "P2"]
    tags: list[str]
    preconditions: list[str]
    knowledge_refs: list[str]
    workflow_refs: list[str] = Field(default_factory=list)
    clock: str | None = None
    before_turn: list[BeforeTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        if not any(t.speaker == "user" for t in self.turns):
            raise ValueError("session contains no executable user turns")
        if len(self.setup_profiles) != len(set(self.setup_profiles)):
            raise ValueError("duplicate profile names")
        for i, turn in enumerate(self.turns):
            for ref in (turn.answers_ask_from_turn, turn.ignores_ask_from_turn):
                if ref is not None and (ref >= i or self.turns[ref].speaker != "assistant"):
                    raise ValueError("question reference must point to an earlier assistant reference")
        for action in self.before_turn:
            if action.turn_index >= len(self.turns) or self.turns[action.turn_index].speaker != "user":
                raise ValueError("before_turn must reference a user turn in original indexing")
        return self


class QA(StrictModel):
    id: ID
    synthetic: Literal[True]
    intent: str
    sub_intent: str
    question: str
    expected_facts: list[str]
    must_not: list[str]


class SourceDataset(StrictModel):
    dataset: str
    synthetic: Literal[True]
    as_of: str
    cafe: str
    source_knowledge: list[str]
    description: str
    turn_kinds: dict[str, str]
    shape_counts: dict[str, Index]
    user_turns: Index
    ordering_address_or_status_turns: Index
    intent_coverage: dict[str, list[str]]
    sessions: list[Session]
    workflow_references: list[str]
    evaluation_contract: dict[str, str | dict[str, str]]
    setup_profiles: dict[str, list[str]]
    session_count: Index
    priority_counts: dict[str, Index]
    tag_counts: dict[str, Index]
    knowledge_coverage: dict[str, list[str]]
    menu_item_coverage: dict[str, list[str]]
    review_notes: list[str]
