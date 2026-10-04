"""No application imports or IO: predicates consume immutable state projections.

application.v1 keys: basket.items[{item_id,variant_id,quantity,unit_price_minor}],
basket[{subtotal,fee,tax,discount,total}_minor], quote[{id,valid}],
orders/payments/addresses/effects[{tenant_id,customer_id}], payments[amount_minor],
effects[{operation_id,kind}]. Unknown/missing evidence is never empty/zero/success.
"""
from collections import Counter
from datetime import datetime
import re
import unicodedata

from .models import CHECK_VERSION, Result


class MissingEvidence(ValueError):
    pass


def at(value, path):
    for key in path.strip("/").split("/") if path.strip("/") else []:
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
            continue
        if not isinstance(value, dict) or key not in value:
            raise MissingEvidence(f"Missing state field: {path}")
        value = value[key]
    return value


def integer(value):
    if type(value) is not int:
        raise MissingEvidence("Money and quantities require integer minor units / counts")
    return value


def records(value):
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise MissingEvidence("Expected a list of state records")
    return value


def subset(actual, expected):
    if isinstance(expected, dict):
        return all(subset(at(actual, k), v) for k, v in expected.items())
    return type(actual) is type(expected) and actual == expected


def address_record(value):
    """Normalize presentation, never identity, numbers, or address semantics."""
    if not isinstance(value, dict):
        raise MissingEvidence('Expected an address record')
    value = dict(value)
    if isinstance(value.get('components'), dict):
        value['components'] = {
            key: ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
            if isinstance(text, str) else text for key, text in value['components'].items()
        }
    return value


STREET_FIELDS = ('street_address', 'house_or_flat', 'building_or_block',
                 'street_or_locality', 'sector_or_phase', 'landmark')


def valid_text_address(row):
    """Basic postal contract; street correctness is established by user confirmation."""
    components = at(row, 'components')
    if not isinstance(components, dict):
        raise MissingEvidence('Expected address components')

    def supplied(value):
        return (isinstance(value, str) and
                value.strip().casefold() not in {'', 'none', 'null', 'n/a', 'na', 'unknown', 'not provided'})

    street = components.get('street_address')
    if not supplied(street) and not any(supplied(components.get(k)) for k in STREET_FIELDS[1:]):
        return False
    for key in ('city', 'state', 'country'):
        value = at(components, key)
        if not supplied(value) or not any(c.isalpha() for c in value):
            return False
    pin = at(components, 'postal_code')
    return isinstance(pin, str) and re.fullmatch(r'[1-9][0-9]{5}', pin.strip()) is not None


def address_matches(actual, expected, *, street_policy='exact'):
    if street_policy == 'free_form':
        if not valid_text_address(actual):
            return False
        # New reviewed checks compare postal fields, identity and selection, not
        # translated street wording. Old saved specs retain exact matching.
        actual, expected = (dict(row, components={k: v for k, v in row.get('components', {}).items()
                                                if k not in STREET_FIELDS}) for row in (actual, expected))
    actual, expected = address_record(actual), address_record(expected)
    # A free-form street and legacy split components carry the same content.
    # Ignore separators/case only; retain every word, label and number.
    grouping = STREET_FIELDS
    a, e = actual.get('components', {}), expected.get('components', {})
    if isinstance(a, dict) and isinstance(e, dict) and any(key in e for key in grouping):
        def street(components):
            values = ([components['street_address']] if components.get('street_address') else
                      [components[k] for k in grouping[1:] if components.get(k)])
            if not all(isinstance(v, str) for v in values):
                return None
            return ' '.join(' '.join(values).replace(',', ' ').split())
        if street(a) and street(e) and street(a) != street(e):
            return False
        if street(a) and street(a) == street(e):
            actual = {**actual, 'components': {k: v for k, v in a.items() if k not in grouping}}
            expected = {**expected, 'components': {k: v for k, v in e.items() if k not in grouping}}
    # Missing identity or component evidence must still block the assertion.
    return subset(actual, expected)


def _basket_by_name(expected: list) -> bool:
    """Reviewed fixtures identify lines by catalog name. Runtime checks use item and variant ids."""
    return bool(expected) and all(
        isinstance(row, dict) and "name" in row and "item_id" not in row and "variant_id" not in row
        for row in expected)


def _basket_index(rows: list, by_name: bool):
    def key(row):
        if by_name:
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                raise MissingEvidence("Basket name match requires a catalog name")
            return name
        return (at(row, "item_id"), at(row, "variant_id"))

    keys = [key(row) for row in rows]
    if len(set(keys)) != len(keys):
        return None
    return {ident: integer(at(row, "quantity")) for ident, row in zip(keys, rows)}


def section(spec):
    return (spec.path or {"basket": "basket", "totals": "basket", "addresses": "addresses",
                         "quote_invalidated": "quote", "payment": "payments", "pos_acceptance": "pos",
                         "order_count": "orders", "duplicate_effects": "effects"}.get(spec.kind, "")).strip("/").split("/")[0]


def selected_tasks(value, match):
    rows = records(value)
    for row in rows:
        for field, expected in match.items():
            actual = at(row, field)
            if type(actual) is not type(expected) or (isinstance(actual, str) and not actual.strip()):
                raise MissingEvidence("Task selection requires explicit typed identity fields")
    selected = [row for row in rows if subset(row, match)]
    if any(type(at(row, "is_complete")) is not bool for row in selected):
        raise MissingEvidence("Task completion requires an explicit boolean is_complete")
    return selected


def pending_task_identities(value, match):
    identities = []
    for row in selected_tasks(value, match):
        if row["is_complete"]:
            identities.append(None)
            continue
        identity, operation = at(row, "query_id"), at(row, "sub_intent")
        if not (type(identity) is int and identity >= 0 or
                isinstance(identity, str) and identity.strip() and "[REDACTED:" not in identity):
            raise MissingEvidence("Pending task requires a nonempty string or nonnegative integer query_id")
        if not isinstance(operation, str) or not operation.strip():
            raise MissingEvidence("Pending task requires an explicit sub_intent")
        questions = at(row, "follow_up_question")
        if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
            raise MissingEvidence("Pending task requires a list of follow-up questions")
        # Wording/meaning is reviewed semantically; retention checks presence.
        identities.append((type(identity), identity, operation, bool(questions and questions[-1].strip())))
    return identities


def predicate(spec, state, before, previous=None):
    e, kind = spec.expected, spec.kind
    default_paths = {"basket": "basket", "totals": "basket", "addresses": "addresses",
                     "quote_invalidated": "quote", "payment": "payments",
                     "pos_acceptance": "pos", "duplicate_effects": "effects"}
    value = at(state, spec.path or default_paths.get(kind, ""))
    if kind == "equals":
        return subset(value, e["value"])
    if kind == "unchanged":
        if before is None:
            raise MissingEvidence("Unchanged state requires before and after snapshots")
        return value == at(before, spec.path)
    if kind == "tasks_complete":
        if "pending_before_turn" in e:
            if before is None or previous is None:
                raise MissingEvidence("Task resolution requires before and prior-turn after snapshots")
            pending = spec.model_copy(update={"kind": "tasks_pending", "expected": {
                "match": e["match"], "retain": True}})
            if not predicate(pending, before, previous):
                return False
        return all(row["is_complete"] for row in selected_tasks(value, e.get("match", {})))
    if kind == "tasks_pending":
        current = pending_task_identities(value, e["match"])
        previous = None
        if e.get("retain", False):
            if before is None:
                raise MissingEvidence("Retaining pending work requires before and after snapshots")
            previous = pending_task_identities(at(before, spec.path), e["match"])
        # One open choice, with its question retained even while the active prompt
        # is suppressed. A missing/duplicate/completed task cannot satisfy this.
        return (len(current) == 1 and current[0] is not None and current[0][-1]
                and (previous is None or current == previous))
    if kind == "basket":
        if "before_items" in e:
            if before is None:
                raise MissingEvidence("Basket transition requires before and after snapshots")
            initial = spec.model_copy(update={"expected": {"items": e["before_items"]}})
            if not predicate(initial, before, None):
                return False
        items = records(at(value, "items"))
        if "alternatives" in e:
            return any(predicate(spec.model_copy(update={"expected": {"items": alternative}}), state, before)
                       for alternative in e["alternatives"])
        expected = records(at(e, "items"))
        by_name = _basket_by_name(expected)
        actual_index, expected_index = _basket_index(items, by_name), _basket_index(expected, by_name)
        if expected_index is None:
            raise MissingEvidence("Expected basket contains duplicate identities")
        if actual_index is None or actual_index != expected_index or not all(q > 0 for q in actual_index.values()):
            return False
        for wanted in expected:
            if "unit_price_minor" in wanted:
                key = wanted["name"] if by_name else (wanted["item_id"], wanted["variant_id"])
                row = next(r for r in items if (r["name"] if by_name else (r["item_id"], r["variant_id"])) == key)
                if integer(at(row, "unit_price_minor")) != integer(wanted["unit_price_minor"]):
                    return False
        return True
    if kind == "totals":
        items = records(at(value, "items"))
        subtotal = sum(integer(at(row, "quantity")) * integer(at(row, "unit_price_minor")) for row in items)
        total = subtotal + integer(at(value, "fee_minor")) + integer(at(value, "tax_minor")) - integer(at(value, "discount_minor"))
        valid = integer(at(value, "subtotal_minor")) == subtotal and integer(at(value, "total_minor")) == total
        valid = valid and total >= 0 and all(integer(at(r, "quantity")) > 0 and integer(at(r, "unit_price_minor")) >= 0 for r in items)
        valid = valid and all(integer(at(value, k)) >= 0 for k in ("fee_minor", "tax_minor", "discount_minor"))
        return valid and subset(value, e)
    if kind == "ownership":
        if e == {"scope_path": "ownership"}:
            if before is None:
                raise MissingEvidence("Ownership requires a trusted pre-turn scope")
            e = at(before, "ownership")
        if not e or not set(e).issubset({"tenant_id", "customer_id"}):
            raise MissingEvidence("Ownership requires expected tenant_id and/or customer_id")
        rows = records(value) if isinstance(value, list) else [value]
        if any(not isinstance(v, (str, int)) or not str(v) for v in e.values()):
            raise MissingEvidence("Ownership identifiers must be nonempty")
        return all(subset(row, e) for row in rows)
    if kind == "addresses":
        rows, expected = records(value), records(at(e, "records"))
        policy = e.get('street_policy', 'exact')
        if policy not in {'exact', 'free_form'}:
            raise MissingEvidence('Unknown address street policy')
        if policy == 'free_form':
            if e.get('exact', True) and len(rows) != len(expected):
                return False
            # Two saved addresses may share postal fields. Match each expected
            # record to one distinct row without imposing a street spelling.
            candidates = [[i for i, row in enumerate(rows)
                           if address_matches(row, wanted, street_policy=policy)] for wanted in expected]
            assigned = {}

            def assign(wanted, visited):
                for i in candidates[wanted]:
                    if i not in visited:
                        visited.add(i)
                        if i not in assigned or assign(assigned[i], visited):
                            assigned[i] = wanted
                            return True
                return False

            return all(assign(i, set()) for i in range(len(expected)))
        matches = [sum(address_matches(row, wanted) for row in rows) for wanted in expected]
        return all(n == 1 for n in matches) and (not e.get("exact", True) or len(rows) == len(expected))
    if kind == "quote_invalidated":
        valid = at(value, "valid")
        if type(valid) is not bool:
            raise MissingEvidence("Quote valid must be a boolean")
        if before is None:
            raise MissingEvidence("Quote invalidation requires a before snapshot")
        previous = at(before, spec.path or "quote")
        old_id, new_id = at(previous, 'id'), at(value, 'id')
        retired = old_id == new_id and valid is False
        replaced = bool(new_id) and old_id != new_id and valid is True
        quote_expected = {key: item for key, item in e.items() if key != 'replacement_totals'}
        if not (at(previous, 'valid') is True and (retired or replaced) and subset(value, quote_expected)):
            return False
        if replaced and 'replacement_totals' in e:
            # Only an actual replacement asserts payable totals. Retiring a
            # quote leaves no live money projection; never invent zero fees.
            totals = spec.model_copy(update={'kind': 'totals', 'path': 'basket',
                                             'expected': e['replacement_totals']})
            return predicate(totals, state, before)
        return True
    if kind == "order_count":
        count = len(records(at(state, spec.path or "orders")))
        if "min_count" in e and "max_count" in e:
            return integer(e["min_count"]) <= count <= integer(e["max_count"])
        if "delta" in e:
            if before is None:
                raise MissingEvidence("Order delta requires a before snapshot")
            return count - len(records(at(before, spec.path or "orders"))) == integer(e["delta"])
        return count == integer(at(e, "count"))
    if kind in {"payment", "pos_acceptance"}:
        rows = [value] if isinstance(value, dict) else records(value)
        match = e.get("match", {})
        selected = [r for r in rows if subset(r, match)]
        expected = at(e, "fields")
        if not expected:
            raise MissingEvidence("Payment/POS requires expected fields")
        if "amount_minor" in expected:
            integer(expected["amount_minor"])
            for row in selected:
                integer(at(row, "amount_minor"))
        return len(selected) == integer(e.get("count", 1)) and all(subset(r, expected) for r in selected)
    if kind == "duplicate_effects":
        rows = records(value)
        if "kinds" in e:
            rows = [row for row in rows if at(row, "kind") in e["kinds"]]
        keys = e.get("keys", ["operation_id", "kind"])
        if not keys or not all(isinstance(k, str) for k in keys):
            raise MissingEvidence("Duplicate detection requires identity keys")
        # Repeated snapshots of one effect are not duplicate effects. Each list
        # is an authoritative collection of effects, not an event replay log.
        identities = [tuple(at(r, k) for k in keys) for r in rows]
        return all(n <= integer(e.get("max_per_key", 1)) for n in Counter(identities).values())
    if kind == "mutation_claim":
        if before is None:
            raise MissingEvidence("Mutation support requires before and after state")
        previous = at(before, spec.path)
        if "value" in e:
            return subset(value, e["value"])
        if "count_delta" in e:
            return len(records(value)) - len(records(previous)) == integer(e["count_delta"])
        return value != previous
    raise MissingEvidence("Unknown check kind")


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise MissingEvidence("Evidence timestamp requires an offset")
    return parsed


def result_for(turn, assertion_id, criterion, outcome, explanation, evidence_ids,
               category="state", evaluator=CHECK_VERSION, **kwargs):
    return Result(assertion_id=assertion_id, scenario_id=turn.scenario_id,
                  scenario_instance_id=turn.scenario_instance_id, attempt=turn.attempt,
                  original_turn_index=turn.original_turn_index, request_id=turn.request_id,
                  evaluator=evaluator, category=category, criterion=criterion,
                  outcome=outcome, explanation=explanation, evidence_ids=evidence_ids, **kwargs)


class DeterministicEvaluator:
    def evaluate(self, spec, turn, snapshots, events=()):
        ident = f"{turn.request_id}:check:{spec.check_id}"
        def done(outcome, reason, ids, elapsed=None):
            return result_for(turn, ident, spec.criterion, outcome, reason, ids,
                              spec.category, elapsed_to_completion_ms=elapsed)
        scoped = [s for s in snapshots if (s.run_id, s.scenario_id, s.scenario_instance_id, s.attempt,
                   s.original_turn_index) == (turn.run_id, turn.scenario_id,
                   turn.scenario_instance_id, turn.attempt, turn.original_turn_index)
                   and (s.request_id == turn.request_id or (s.phase == "before" and s.request_id is None))]
        scoped.sort(key=lambda s: (timestamp(s.captured_at), s.snapshot_id))
        before = [s for s in scoped if s.phase == "before"]
        after = [s for s in scoped if s.phase == "after"]
        ids = [turn.event_id] + [s.snapshot_id for s in before[-1:]]
        if not after:
            return done("BLOCKED", "No scoped after snapshot was saved.", ids)
        for left, right in zip(after, after[1:]):
            if timestamp(left.captured_at) == timestamp(right.captured_at) and left.state != right.state:
                return done("NEEDS_REVIEW", "Conflicting state snapshots have the same timestamp.", ids + [left.snapshot_id, right.snapshot_id])
        if before and timestamp(before[-1].captured_at) > timestamp(after[0].captured_at):
            return done("NEEDS_REVIEW", "Before/after timestamps contradict their boundaries.", ids + [after[0].snapshot_id])
        anchor = None
        responses = [ev for ev in events if ev.run_id == turn.run_id and ev.scenario_id == turn.scenario_id
                     and ev.request_id == turn.request_id and ev.kind == "response"
                     and ev.scenario_instance_id == turn.scenario_instance_id and ev.attempt == turn.attempt]
        if len(responses) == 1 and timestamp(after[0].captured_at) < timestamp(responses[0].occurred_at):
            return done("NEEDS_REVIEW", "After snapshot predates the response event.", ids + [responses[0].event_id, after[0].snapshot_id])
        if spec.timing == "eventual":
            if len(responses) != 1:
                return done("BLOCKED", "Eventual checks require one timestamped response event as their start.", ids)
            anchor = timestamp(responses[0].occurred_at)
            ids.append(responses[0].event_id)
        missing = []
        observed = False
        completion = None
        failed_at_deadline = False
        late_transition = False
        for snap in after if anchor else after[:1]:
            elapsed = (timestamp(snap.captured_at) - anchor).total_seconds() * 1000 if anchor else None
            if anchor and elapsed < 0:
                return done("NEEDS_REVIEW", "After snapshot predates the response event.", ids + [snap.snapshot_id])
            ids.append(snap.snapshot_id)
            completion = elapsed
            if anchor and elapsed > spec.deadline_ms and spec.transition_at_path is None:
                continue
            try:
                root = section(spec)
                needs_before = (spec.kind in {"quote_invalidated", "mutation_claim", "unchanged"}
                    or (spec.kind == "order_count" and "delta" in spec.expected)
                    or (spec.kind == "basket" and "before_items" in spec.expected)
                    or (spec.kind == "tasks_complete" and "pending_before_turn" in spec.expected)
                    or (spec.kind == "tasks_pending" and spec.expected.get("retain", False)))
                if root in snap.unavailable_sections or (needs_before and before and root in before[-1].unavailable_sections):
                    raise MissingEvidence(f"State section {root} is declared unavailable")
                if (spec.kind == 'quote_invalidated' and 'replacement_totals' in spec.expected
                        and at(snap.state, (spec.path or 'quote') + '/valid') is True
                        and 'basket' in snap.unavailable_sections):
                    raise MissingEvidence('Replacement quote money evidence is unavailable')
                previous = None
                if spec.kind == "tasks_complete" and "pending_before_turn" in spec.expected:
                    prior = [s for s in snapshots if s.phase == "after" and
                        (s.run_id, s.scenario_id, s.scenario_instance_id, s.attempt, s.original_turn_index) ==
                        (turn.run_id, turn.scenario_id, turn.scenario_instance_id, turn.attempt,
                         spec.expected["pending_before_turn"])]
                    if not prior:
                        raise MissingEvidence("Task resolution requires a prior-turn after snapshot")
                    prior.sort(key=lambda s: (timestamp(s.captured_at), s.snapshot_id))
                    last = prior[-1]
                    ids.append(last.snapshot_id)
                    if any(s.request_id != last.request_id or
                           (timestamp(s.captured_at) == timestamp(last.captured_at) and
                            (s.state != last.state or s.unavailable_sections != last.unavailable_sections))
                           for s in prior):
                        return done("NEEDS_REVIEW", "Ambiguous prior-turn task evidence.", ids)
                    if before and timestamp(last.captured_at) > timestamp(before[-1].captured_at):
                        return done("NEEDS_REVIEW", "Prior-turn snapshot postdates the before boundary.", ids)
                    if root in last.unavailable_sections:
                        raise MissingEvidence(f"Prior-turn state section {root} is declared unavailable")
                    previous = last.state
                passed = predicate(spec, snap.state, before[-1].state if before else None, previous)
                observed = True
                if passed and spec.transition_at_path:
                    transition = timestamp(at(snap.state, spec.transition_at_path))
                    if transition > timestamp(snap.captured_at) or transition < anchor:
                        return done("NEEDS_REVIEW", "Transition timestamp contradicts the observation window.", ids)
                    elapsed = (transition - anchor).total_seconds() * 1000
                    passed = elapsed <= spec.deadline_ms
                    late_transition = not passed
            except (MissingEvidence, TypeError, KeyError, ValueError) as exc:
                missing.append(str(exc) if isinstance(exc, MissingEvidence) else "Malformed or ambiguous state projection")
                continue
            if passed:
                return done("PASS", "Saved state satisfies the structured assertion.", ids, elapsed)
            if not anchor:
                return done("FAIL", "Immediate saved state contradicts the structured assertion.", ids)
            failed_at_deadline = failed_at_deadline or completion == spec.deadline_ms
        if missing:
            return done("BLOCKED", "; ".join(sorted(set(missing))), ids)
        if anchor and (completion is None or completion < spec.deadline_ms):
            return done("NEEDS_REVIEW", "Observation ended before the eventual deadline; outcome is unresolved.", ids)
        if failed_at_deadline or late_transition:
            return done("FAIL", "State at the deadline or the recorded transition establishes a missed deadline.", ids)
        return done("NEEDS_REVIEW" if observed else "BLOCKED", "Polling leaves the state at the deadline ambiguous; no timely transition timestamp was saved." if observed else "No usable state inside the required observation window.", ids)


# Deliberately narrow language recognition. Other languages/paraphrases remain
# individual semantic review items; these patterns never establish a PASS.
CLAIMS = (
    (r"\b(?:i(?:'ve| have)?\s+)?(?:successfully\s+)?saved\s+(?:your\s+|the\s+)?address\b", "addresses"),
    (r"\b(?:your\s+)?order\s+(?:has been\s+|was\s+|is\s+)?(?:successfully\s+)?(?:placed|created)\b", "orders"),
    (r"\b(?:i(?:'ve| have)?\s+)?(?:successfully\s+)?added\s+.+?\s+to\s+(?:your\s+|the\s+)?(?:basket|cart)\b", "basket"),
)


def detected_claims(text):
    for pattern, path in CLAIMS:
        for match in re.finditer(pattern, text or "", re.I):
            prefix = (text or "")[max(0, match.start() - 35):match.start()].lower()
            if not re.search(r"\b(?:not|never|haven't|hasn't|isn't|wasn't|cannot|can't|if|when|once|will|would)\b", prefix):
                yield match.group(0), path
