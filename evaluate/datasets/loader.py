"""Read and normalize datasets. This module cannot provision or execute anything."""
from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from pydantic import TypeAdapter, ValidationError

from evaluate.contracts.models import (
    Clock, Issue, NormalizedScenario, ReferenceTurn, ScenarioPlan,
    Setup, Turn,
)
from evaluate.datasets.source import QA, SourceDataset
from evaluate.identity import canonical_hash, file_hash, scenario_id

PROFILE_NAMES = {"knowledge_only", "catalog_sandbox", "address_sandbox", "checkout_sandbox"}


class DatasetError(ValueError):
    """Invalid data; safe diagnostic without echoing raw source values."""


def read_json(path: Path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DatasetError("duplicate JSON object key")
            result[key] = value
        return result

    def constant(_):
        raise DatasetError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise DatasetError("non-finite JSON number")
        return number

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant, parse_float=finite_float)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read valid JSON: {path.name}") from exc


def validate_model(model, value, location):
    try:
        return model.model_validate(value)
    except (ValidationError, ValueError, KeyError) as exc:
        # ValidationError includes input values by default; do not serialize it.
        fields = ""
        if isinstance(exc, ValidationError):
            fields = "; fields: " + ", ".join("/".join(map(str, e["loc"])) for e in exc.errors(include_input=False))
        raise DatasetError(f"invalid structure at {location}{fields}") from exc


def contained(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise DatasetError("setup/reference file is missing or escapes dataset directory")
    return path


def resolve_pointer(root: Path, ref: str):
    filename, sep, pointer = ref.partition("#")
    if not sep or not pointer.startswith("/"):
        raise DatasetError("knowledge reference requires a JSON pointer")
    current = read_json(contained(root, filename))
    try:
        for part in pointer[1:].split("/"):
            if re.search(r"~(?![01])", part):
                raise ValueError("invalid pointer escape")
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list) and not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("invalid array index")
            current = current[int(key)] if isinstance(current, list) else current[key]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise DatasetError("knowledge reference does not resolve") from exc
    return current


def resolve_profiles(names: list[str], plan: ScenarioPlan, overrides: Setup | None = None):
    """Inheritance is explicit. Unrelated conflicting defaults require an override.

    checkout inherits catalog; it always wins regardless of source list order.
    Session overrides are applied last. None means inherit, not deletion.
    """
    ordered, visiting = [], set()

    def visit(name):
        if name in visiting:
            raise DatasetError("profile inheritance cycle")
        if name not in plan.profiles:
            raise DatasetError("profile lacks a reviewed definition")
        if name in ordered:
            return
        visiting.add(name)
        for parent in plan.profiles[name].extends:
            visit(parent)
        visiting.remove(name)
        ordered.append(name)

    for name in sorted(names):
        visit(name)

    def inherits(child, parent):
        return parent in plan.profiles[child].extends or any(inherits(p, parent) for p in plan.profiles[child].extends)

    values, owners, conflicts = {}, {}, set()
    for name in ordered:
        for key, value in plan.profiles[name].defaults.model_dump(exclude_none=True).items():
            if key in values and value != values[key] and not inherits(name, owners[key]):
                conflicts.add(key)
            values[key], owners[key] = value, name
    explicit = overrides.model_dump(exclude_none=True) if overrides else {}
    conflicts -= explicit.keys()
    if conflicts:
        raise DatasetError("contradictory profile defaults: " + ", ".join(sorted(conflicts)))
    values.update(explicit)
    return Setup.model_validate(values), ordered


@dataclass(frozen=True)
class DatasetBundle:
    scenarios: list[NormalizedScenario]
    counts: dict[str, int]
    dataset_hashes: dict[str, str]
    warnings: list[Issue]

    @property
    def blocked(self):
        return [s for s in self.scenarios if s.blockers]


def load_dataset(root: Path, plan: ScenarioPlan) -> DatasetBundle:
    root = Path(root)
    raw = read_json(root / "session_query_sets.json")
    data = validate_model(SourceDataset, raw, "sessions")
    try:
        qa_raw = read_json(root / "qa_test_cases.json")
        qa = TypeAdapter(list[QA]).validate_python(qa_raw, strict=True)
    except ValidationError as exc:
        raise DatasetError("invalid structure at QA cases") from exc
    if set(data.setup_profiles) != PROFILE_NAMES:
        raise DatasetError("unknown or missing dataset profile names")
    for items in (data.sessions, qa):
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise DatasetError("duplicate source IDs within namespace")
    source_ids = {s.id for s in data.sessions}
    all_ids = {scenario_id("sessions", s.id) for s in data.sessions} | {scenario_id("qa", q.id) for q in qa}
    if set(plan.scenarios) - all_ids:
        raise DatasetError("plan references unknown scenario IDs")
    hashes = {name: file_hash(contained(root, name)) for name in ["session_query_sets.json", "qa_test_cases.json", *data.source_knowledge]}
    if len(data.source_knowledge) != len(set(data.source_knowledge)):
        raise DatasetError("duplicate setup inputs")
    records = set()
    if not data.source_knowledge:
        raise DatasetError("knowledge setup inputs are required")
    for name in data.source_knowledge:
        document = read_json(contained(root, name))
        if not isinstance(document, dict) or not document or name in {"session_query_sets.json", "qa_test_cases.json"}:
            raise DatasetError("knowledge inputs must be knowledge objects, not cases")
        for intent, payloads in document.items():
            if not isinstance(payloads, dict) or not payloads:
                raise DatasetError("knowledge intent must contain sub-intent objects")
            for sub_intent, payload in payloads.items():
                if not isinstance(payload, dict):
                    raise DatasetError("knowledge record payload must be an object")
                if (intent, sub_intent) in records:
                    raise DatasetError("overlapping knowledge setup inputs")
                records.add((intent, sub_intent))
    for table in (data.knowledge_coverage, data.menu_item_coverage):
        for refs in table.values():
            if not refs or len(refs) != len(set(refs)) or set(refs) - source_ids:
                raise DatasetError("coverage references unknown/duplicate sessions or is empty")
    for ref in data.knowledge_coverage:
        resolve_pointer(root, ref)
        filename = ref.partition("#")[0]
        hashes[filename] = file_hash(contained(root, filename))
    try:
        menu = read_json(contained(root, "02_menu_knowledge.json"))["menu_items"]["pricing"]["items"]
        if not isinstance(menu, dict):
            raise TypeError("menu items must be keyed by name")
        menu_names = set(menu)
    except (KeyError, TypeError) as exc:
        raise DatasetError("invalid knowledge menu structure") from exc
    if len(menu_names) != len(menu) or set(data.menu_item_coverage) - menu_names:
        raise DatasetError("menu coverage references unknown items or duplicate menu names")
    warnings = []
    counts = {
        "sessions": len(data.sessions), "qa_cases": len(qa),
        "scenarios": len(data.sessions) + len(qa),
        "session_user_turns": sum(t.speaker == "user" for s in data.sessions for t in s.turns),
        "reference_turns": sum(t.speaker == "assistant" for s in data.sessions for t in s.turns),
        "knowledge_documents": len(data.source_knowledge), "menu_items": len(menu),
        "knowledge_records": len(records),
    }
    counts["user_turns"] = counts["session_user_turns"] + len(qa)
    for declared, computed, label in (
        (data.session_count, counts["sessions"], "session_count"),
        (data.user_turns, counts["session_user_turns"], "user_turns"),
        (data.priority_counts, dict(Counter(s.priority for s in data.sessions)), "priority_counts"),
        (data.shape_counts, dict(Counter(s.shape for s in data.sessions)), "shape_counts"),
        (data.tag_counts, dict(Counter(t for s in data.sessions for t in s.tags)), "tag_counts"),
    ):
        if declared != computed:
            warnings.append(Issue(code="stale_count", location=label, message="Declared count differs from JSON-derived count."))
    for name, profile in plan.profiles.items():
        if profile.source_hash != canonical_hash(data.setup_profiles[name]):
            raise DatasetError("stale profile review hash")
    # Validate all profile inheritance, even unused entries.
    for name in plan.profiles:
        resolve_profiles([name], plan)
    if plan.contract_hash != canonical_hash(data.evaluation_contract):
        raise DatasetError("stale evaluation contract review hash")
    scenarios = []
    entries = [("sessions", s, raw["sessions"][i]) for i, s in enumerate(data.sessions)]
    entries += [("qa", q, qa_raw[i]) for i, q in enumerate(qa)]
    for namespace, source, original in entries:
        sid = scenario_id(namespace, source.id)
        session = namespace == "sessions"
        profiles = source.setup_profiles if session else ["knowledge_only"]
        if set(profiles) - PROFILE_NAMES:
            raise DatasetError("unknown profile name")
        if "knowledge_only" in profiles and any(n in profiles for n in ("catalog_sandbox", "checkout_sandbox", "address_sandbox")):
            raise DatasetError("contradictory knowledge-only and sandbox setup")
        review = plan.scenarios.get(sid)
        digest = canonical_hash(original)
        if review and review.source_hash != digest:
            raise DatasetError("stale scenario review hash")
        blockers = []

        def block(code, location, message):
            blockers.append(Issue(code=code, location=location, message=message))

        missing = set(profiles) - plan.profiles.keys()
        if missing:
            block("unreviewed_profile", sid, "Profile has no typed reviewed definition.")
            setup, effective_profiles = Setup(), []
        else:
            setup, effective_profiles = resolve_profiles(profiles, plan, review.overrides if review else None)
        for name in effective_profiles:
            if set(plan.profiles[name].required_capabilities) - set(plan.supported_capabilities):
                block("unsupported_setup", sid, f"Required capabilities are unavailable for {name}.")
        if setup.payment == "unavailable" and "online" in (setup.payment_methods or []):
            raise DatasetError("online payment requires a fake adapter")
        if setup.catalog == "none" and (setup.variant_name or setup.stock == "finite_local"):
            raise DatasetError("catalog setup contradicts variant or stock setup")
        if setup.scheduling is False and setup.horizon_days is not None:
            raise DatasetError("scheduling horizon supplied while scheduling is disabled")
        if setup.stock == "none" and "requires_finite_stock" in plan.supported_capabilities:
            block("stock_mismatch", sid, "Provider requires finite stock but the scenario forbids stock records.")
        if setup.variant_name and "mock_standard_test_variant" in plan.supported_capabilities and setup.variant_name != "Standard (test)":
            block("variant_mismatch", sid, "Mock variant name differs from the required scenario variant.")
        if setup.customer == "authenticated_synthetic" and "authenticated_website_customer" not in plan.supported_capabilities:
            block("identity_adapter_missing", sid, "Website JWT authenticates the tenant; reviewed customer binding is required.")
        clock = validate_model(Clock, {"at": source.clock, "timezone": plan.default_clock.timezone}, sid + "/clock") if session and source.clock else plan.default_clock
        requirements = {}
        if session:
            requirements.update({f"/preconditions/{i}": (text, None) for i, text in enumerate(source.preconditions)})
            requirements.update({f"/before_turn/{i}/action": (action.action, action.turn_index) for i, action in enumerate(source.before_turn)})
        actions = review.actions if review else []
        mapped = review.requirements if review else {}
        if set(mapped) - requirements.keys():
            raise DatasetError("unknown requirement reference")
        action_ids = [a.action_id for a in actions]
        if len(set(action_ids)) != len(action_ids):
            raise DatasetError("duplicate action IDs")
        referenced = [action for ids in mapped.values() for action in ids]
        if len(referenced) != len(set(referenced)) or set(referenced) != set(action_ids):
            raise DatasetError("missing, duplicate or orphan action reference")
        for ref, (text, turn_index) in requirements.items():
            if ref not in mapped:
                block("unmapped_requirement", sid + ref, "Natural-language requirement needs a reviewed typed mapping.")
        for action in actions:
            ref = action.requirement_ref
            if (action.scenario_id != sid or ref not in requirements or action.action_id not in mapped.get(ref, [])):
                raise DatasetError("action points to wrong scenario or requirement")
            text, turn_index = requirements[ref]
            if action.requirement_hash != canonical_hash(text) or action.original_turn_index != turn_index:
                raise DatasetError("stale action text or incorrect original turn index")
            if action.operation.kind not in plan.supported_capabilities:
                block("unsupported_action", sid + ref, "No control implementation declares this action capability.")
            if action.operation.kind == "seed_fixture" and plan.fixture_hashes.get(action.operation.fixture_id) != action.operation.fixture_hash:
                raise DatasetError("missing or stale fixture definition")
        turns, references = [], []
        if session:
            for index, turn in enumerate(source.turns):
                if turn.speaker == "assistant":
                    references.append(ReferenceTurn(original_turn_index=index, text=turn.text, asks=turn.asks, pending=turn.pending))
                else:
                    if turn.turn_kind not in data.turn_kinds:
                        raise DatasetError("unknown turn kind")
                    for ref in (turn.answers_ask_from_turn, turn.ignores_ask_from_turn):
                        if ref is not None and not source.turns[ref].asks:
                            block("undefined_question", f"{sid}/turns/{index}", "Referenced assistant entry has no documented pending question; do not infer one.")
                    turns.append(Turn(original_turn_index=index, user_turn_index=len(turns), **turn.model_dump(include={"text", "intent", "sub_intent", "turn_kind", "expected_facts", "must_not", "parts", "answers_ask_from_turn", "ignores_ask_from_turn", "branch_dependency", "allow_empty_rejection"})))
                    for part in [(turn.intent, turn.sub_intent), *((p.intent, p.sub_intent) for p in turn.parts)]:
                        if part[1] not in data.intent_coverage.get(part[0], []):
                            raise DatasetError("turn intent/sub-intent missing from coverage")
            for ref in source.knowledge_refs:
                resolve_pointer(root, ref)
                filename = ref.partition("#")[0]
                hashes[filename] = file_hash(contained(root, filename))
            if set(source.workflow_refs) - set(data.workflow_references):
                raise DatasetError("unknown workflow reference")
        else:
            turns = [Turn(original_turn_index=0, user_turn_index=0, text=source.question, intent=source.intent, sub_intent=source.sub_intent, expected_facts=source.expected_facts, must_not=source.must_not)]
        scenarios.append(NormalizedScenario(
            scenario_id=sid, source_id=source.id, namespace=namespace, source_hash=digest,
            scenario_plan_version=plan.version, priority=source.priority if session else "P1",
            summary=source.summary if session else source.question, tags=source.tags if session else ["standalone_qa"],
            setup_profiles=profiles, clock=clock, setup=setup, setup_inputs=data.source_knowledge,
            turns=turns, references=references, knowledge_refs=source.knowledge_refs if session else [],
            workflow_refs=source.workflow_refs if session else [], actions=actions, blockers=blockers,
        ))
    return DatasetBundle(scenarios, counts, hashes, warnings)
