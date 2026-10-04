"""Orchestrate evidence-only scoring. Nothing here provisions or sends chat."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from evaluate.checks.engine import DeterministicEvaluator, MissingEvidence, at, detected_claims, result_for, section, timestamp
from evaluate.checks.models import CheckSpec, Result, OUTCOMES, CHECK_VERSION, aggregate
from evaluate.checks.plan_checks import generate_check_specs
from evaluate.identity import canonical_hash
from evaluate.judges.semantic import SemanticEvaluator, PROMPT_VERSION, RUBRIC_VERSION


def counts(values):
    values = list(values)
    counter = Counter(values)
    total = len(values)
    return {"denominator": total, "counts": {k: counter[k] for k in OUTCOMES},
            "pass_rate": counter["PASS"] / total if total else None,
            "decided_denominator": counter["PASS"] + counter["FAIL"]}


def distribution(values):
    values = sorted(values)
    if not values:
        return {"samples": 0, "min": None, "mean": None, "p50": None, "p95": None, "max": None}
    import math
    return {"samples": len(values), "min": values[0], "mean": sum(values) / len(values),
            "p50": values[math.ceil(.5 * len(values)) - 1], "p95": values[math.ceil(.95 * len(values)) - 1], "max": values[-1]}


def _turn_warm_up(turn, diagnostics) -> bool:
    if getattr(turn, "warm_up", False):
        return True
    request_id = getattr(turn, "request_id", None)
    if request_id is None:
        return False
    return any(d.kind == "dispatch" and d.data.get("warm_up") is True
               and d.data.get("request_id") == request_id for d in diagnostics)


def _with_generated_checks(bundle):
    if bundle.checks:
        return bundle
    generated = generate_check_specs(bundle.scenarios)
    return bundle.model_copy(update={"checks": generated}) if generated else bundle


# Required application projection, including local records of external work.
# Optional fields (e.g. a quote or pending question) are compared when present.
_EMPTY_REJECTION_STATE = {
    "basket/items": list, "orders": list, "payments": list, "commands": list,
    "effects": list, "addresses": list, "reconciliation": list,
    "chat/id": str, "chat/completed": bool, "checkout": dict,
    "address_selection": dict, "payment/status": str, "pos/status": str,
    "pending_async": list,
}


def _accepted_empty_rejection(source, turn, scoped, conversation, scenario, checks):
    """Recognize the API's controlled blank-input no-op from saved evidence only."""
    if (not source.allow_empty_rejection or source.text.strip()
            or turn.transport_error is not None or turn.http_status != 400
            or turn.response_error != "Missing message" or turn.response_text is not None
            or turn.branch == "mismatch"):
        return False
    before = sorted((row for row in scoped if row.phase == "before"),
                    key=lambda row: (timestamp(row.captured_at), row.snapshot_id))
    after = sorted((row for row in scoped if row.phase == "after"),
                   key=lambda row: (timestamp(row.captured_at), row.snapshot_id))
    if not before or not after:
        return False
    baseline = before[-1]
    # Only knowledge-only scenarios without provider expectations can establish
    # this local no-op without external receipts. Stateful/provider checks keep
    # their evidence requirements, including checks on later turns.
    local_only = (set(scenario.setup_profiles) == {"knowledge_only"}
                  and scenario.setup.payment == "unavailable" and not scenario.actions
                  and not any(c.kind in {"payment", "pos_acceptance"}
                              or section(c) == "provider_receipts" for c in checks))
    optional_unavailable = {"provider_receipts"} if local_only else set()
    required = dict(_EMPTY_REJECTION_STATE)
    if not local_only:
        required["provider_receipts"] = list
    for row in [baseline, *after]:
        if set(row.unavailable_sections) - optional_unavailable:
            return False
        try:
            if any(type(at(row.state, path)) is not kind for path, kind in required.items()):
                return False
        except MissingEvidence:
            return False
    # Compare all saved state, including optional sections and any receipts that
    # are present. The availability exception never hides a recorded mutation.
    if any(row.state != baseline.state for row in after):
        return False
    return any(row.original_turn_index > source.original_turn_index
               and row.transport_error is None and row.branch != "mismatch"
               and row.http_status is not None and 200 <= row.http_status < 300
               and row.response_text is not None for row in conversation)


def evaluate_run(bundle, judge=None):
    bundle = _with_generated_checks(bundle)
    deterministic, semantic = DeterministicEvaluator(), SemanticEvaluator(judge)
    assertions, turns, sessions = [], [], []
    warm_up_turns, warm_up_sessions = [], []
    accepted_rejection_ids = set()
    scenarios = {s.scenario_id: s for s in bundle.scenarios}
    instances = {(r.scenario_id, r.scenario_instance_id, r.attempt) for r in
                 [*bundle.instances, *bundle.turns, *bundle.snapshots, *bundle.events]}
    for diagnostic in bundle.diagnostics:
        row = diagnostic.data
        if row.get("scenario_id") and row.get("scenario_instance_id") and type(row.get("attempt")) is int:
            instances.add((row["scenario_id"], row["scenario_instance_id"], row["attempt"]))
    for scenario_id in scenarios:
        if not any(i[0] == scenario_id for i in instances):
            instances.add((scenario_id, f"unexecuted:{scenario_id}", 1))
    for scenario_id, instance, attempt in sorted(instances):
        scenario = scenarios[scenario_id]
        scenario_checks = [c for c in bundle.checks if c.scenario_id == scenario_id]
        session_results = []
        saved = [t for t in bundle.turns if (t.scenario_id, t.scenario_instance_id, t.attempt) == (scenario_id, instance, attempt)]
        state = [s for s in bundle.snapshots if (s.scenario_id, s.scenario_instance_id, s.attempt) == (scenario_id, instance, attempt)]
        events = [e for e in bundle.events if (e.scenario_id, e.scenario_instance_id, e.attempt) == (scenario_id, instance, attempt)]
        diagnostics = [d for d in bundle.diagnostics if d.kind == "integrity" or
                       (d.data.get("scenario_instance_id") in {None, instance} and d.data.get("attempt") in {None, attempt})]
        session_warm_up = ((bool(saved) and all(_turn_warm_up(t, bundle.diagnostics) for t in saved))
                           or any(d.kind == "attempt" and d.data.get("warm_up") is True
                                  and d.data.get("scenario_instance_id") == instance
                                  and d.data.get("attempt") == attempt for d in diagnostics))
        for source in scenario.turns:
            matches = [t for t in saved if t.original_turn_index == source.original_turn_index]
            if not matches:
                criteria = [("execution", None, "Turn execution evidence is required.")]
                criteria += [(kind, n, item) for kind in ("expected_facts", "must_not") for n, item in enumerate(getattr(source, kind))]
                criteria += [("check", None, c.criterion) for c in bundle.checks if (c.scenario_id, c.original_turn_index) == (scenario_id, source.original_turn_index)]
                rows = [Result(assertion_id=f"{instance}:{attempt}:{source.original_turn_index}:missing:{n}",
                               scenario_id=scenario_id, scenario_instance_id=instance, attempt=attempt,
                               original_turn_index=source.original_turn_index, request_id=None, evaluator=CHECK_VERSION,
                               category="execution" if kind == "execution" else "semantic" if kind in {"expected_facts", "must_not"} else "state",
                               criterion=item, outcome="BLOCKED", explanation="No execution evidence was saved for this planned turn.",
                               evidence_ids=[e.event_id for e in events], item_kind=kind, item_index=index)
                        for n, (kind, index, item) in enumerate(criteria)]
                request_id = None
                turn_is_warm_up = session_warm_up
            else:
                rows = []
                turn_is_warm_up = all(_turn_warm_up(t, bundle.diagnostics) for t in matches)
                for turn in matches:
                    request_id = turn.request_id
                    scoped = [s for s in state if s.original_turn_index == source.original_turn_index and
                              (s.request_id == request_id or (s.phase == "before" and s.request_id is None))]
                    accepted_rejection = _accepted_empty_rejection(source, turn, scoped, saved, scenario, scenario_checks)
                    if accepted_rejection:
                        accepted_rejection_ids.add(request_id)
                    specs = [c for c in bundle.checks if (c.scenario_id, c.original_turn_index) == (scenario_id, source.original_turn_index)]
                    not_dispatched = {d.data.get("request_id") for d in diagnostics if d.kind == "dispatch" and d.data.get("status") == "not_dispatched"}
                    if len([t for t in matches if t.request_id not in not_dispatched]) > 1:
                        rows.append(result_for(turn, f"{request_id}:repeated_request", "Repeated requests require effect reconciliation.", "NEEDS_REVIEW",
                                               "Multiple potentially dispatched requests were recorded; inspect duplicate-effect assertions.", [t.event_id for t in matches]))
                    if turn.sent_message and turn.sent_message != source.text:
                        before_binding = next((s.state.get("message_bindings", {}) for s in scoped if s.phase == "before"), {})
                        bound_text = source.text
                        for source_id, target_id in before_binding.items():
                            bound_text = bound_text.replace(source_id, target_id)
                        if bound_text != turn.sent_message:
                            rows.append(result_for(turn, f"{request_id}:unreviewed_binding", "Fixture substitutions match saved scope.",
                                                   "BLOCKED", "Actual HTTP message has an unreviewed substitution.", [turn.event_id]))
                    if turn.message != source.text:
                        rows.append(result_for(turn, f"{request_id}:input_difference", "Saved input matches the normalized scenario.", "NEEDS_REVIEW",
                                               "Input differs, possibly due to evidence redaction; review the saved input.", [turn.event_id, f"scenario:{scenario_id}"]))
                    missing_refs = set(turn.snapshot_ids) - {s.snapshot_id for s in scoped}
                    if missing_refs:
                        rows.append(result_for(turn, f"{request_id}:missing_state", "Referenced snapshots must be available.", "BLOCKED",
                                               "Some referenced snapshots are absent.", [turn.event_id]))
                    if turn.transport_error or turn.http_status is None or not 200 <= turn.http_status < 300 or turn.branch == "mismatch":
                        if accepted_rejection:
                            rows.append(result_for(turn, f"{request_id}:execution", "Successful transport and matching conversation branch.", "PASS",
                                                   "Blank input received the expected controlled rejection, preserved state, and a later valid turn succeeded.",
                                                   [turn.event_id, *[s.snapshot_id for s in scoped]], "execution"))
                        else:
                            rows.append(result_for(turn, f"{request_id}:execution", "Successful transport and matching conversation branch.", "BLOCKED",
                                                   "HTTP/transport error or branch mismatch; see saved evidence.", [turn.event_id], "execution"))
                    if any(p != "knowledge_only" for p in scenario.setup_profiles) and not any(
                            c.kind not in {"ownership", "duplicate_effects"} and c.path != "address_selection/authorized" for c in specs):
                        rows.append(result_for(turn, f"{request_id}:state_coverage", "Reviewed structured state assertions are supplied.", "BLOCKED",
                                               "No structured state expectations were supplied for this stateful turn.", [turn.event_id]))
                    for spec in specs:
                        rows.append(deterministic.evaluate(spec, turn, state, events))
                    for n, (claim, path) in enumerate(detected_claims(turn.response_text)):
                        spec = CheckSpec(check_id=f"auto-claim-{n}", scenario_id=scenario_id, original_turn_index=source.original_turn_index,
                                         kind="mutation_claim", path=path, criterion=f"Mutation claim has saved state support: {claim}",
                                         expected={"count_delta": 1} if path == "orders" else {})
                        claim_result = deterministic.evaluate(spec, turn, scoped, events)
                        if claim_result.outcome == "PASS":
                            # Some state changing is necessary but does not establish
                            # that the specific claimed item/address was persisted.
                            claim_result = claim_result.model_copy(update={"outcome": "NEEDS_REVIEW",
                                "explanation": "A state change was recorded; review whether it supports this specific mutation claim."})
                        elif claim_result.outcome == "FAIL":
                            # Reconfirming an already-persisted mutation may be
                            # legitimate. Lack of a new effect alone cannot refute it.
                            after = sorted((s for s in scoped if s.phase == "after"), key=lambda s: timestamp(s.captured_at))
                            try:
                                existing = at(after[0].state, "basket/items" if path == "basket" else path)
                                if existing:
                                    claim_result = claim_result.model_copy(update={"outcome": "NEEDS_REVIEW",
                                        "explanation": "Existing state may support an idempotent acknowledgment; review the specific mutation claim."})
                            except (MissingEvidence, IndexError):
                                claim_result = claim_result.model_copy(update={"outcome": "BLOCKED",
                                    "explanation": "The claimed effect's authoritative collection is unavailable."})
                        rows.append(claim_result)
                    conversation = sorted([t for t in saved if t.original_turn_index <= source.original_turn_index], key=lambda t: (t.original_turn_index, t.request_id))
                    context_state = [s for s in state if s.phase != "cleanup" and
                                     (s.original_turn_index is None or s.original_turn_index <= source.original_turn_index)]
                    context_events = [e for e in events if e.kind != "cleanup" and
                                      (e.original_turn_index is None or e.original_turn_index <= source.original_turn_index)]
                    if turn.response_text is None and accepted_rejection:
                        for kind in ("expected_facts", "must_not"):
                            for index, item in enumerate(getattr(source, kind)):
                                rows.append(result_for(turn, f"{request_id}:{kind}:{index}", item, "PASS",
                                                       "The controlled blank-input rejection is allowed, state is unchanged, and the conversation recovers.",
                                                       [turn.event_id, *[s.snapshot_id for s in scoped]], "semantic",
                                                       item_kind=kind, item_index=index))
                    elif turn.response_text is None:
                        for kind in ("expected_facts", "must_not"):
                            for index, item in enumerate(getattr(source, kind)):
                                rows.append(result_for(turn, f"{request_id}:{kind}:{index}", item, "BLOCKED", "No assistant response was saved.",
                                                       [turn.event_id], "semantic", item_kind=kind, item_index=index))
                    else:
                        rows.extend(semantic.evaluate(scenario, source, turn, conversation, context_state,
                                                     [k.model_dump() for k in bundle.knowledge], context_events))
                    if not rows:
                        rows.append(result_for(turn, f"{request_id}:empty_criteria", "Evaluation criteria are supplied.", "BLOCKED", "No assertions were defined.", [turn.event_id]))
            assertions.extend(rows)
            session_results.extend(rows)
            turn_row = {"scenario_id": scenario_id, "scenario_instance_id": instance, "attempt": attempt,
                        "original_turn_index": source.original_turn_index, "request_ids": [t.request_id for t in matches],
                        "intent": source.intent, "sub_intent": source.sub_intent, "outcome": aggregate(r.outcome for r in rows),
                        "assertion_ids": [r.assertion_id for r in rows], "assertions": counts(r.outcome for r in rows),
                        "warm_up": turn_is_warm_up}
            (warm_up_turns if turn_is_warm_up else turns).append(turn_row)
        # Provision/cleanup/crash failures are visible even if every reply passed.
        evaluation_crash_requests = {d.data.get("request_id") for d in diagnostics
                                     if d.kind == "crash" and d.data.get("phase") == "evaluate"}
        failures = [e for e in events if e.status in {"failed", "blocked"}
                    and e.kind not in {"response", "assertion"}
                    and not (e.request_id in evaluation_crash_requests and "evaluate" in e.detail)]
        for e in failures:
            row = Result(assertion_id=f"{e.event_id}:execution", scenario_id=scenario_id, scenario_instance_id=instance,
                         attempt=attempt, original_turn_index=e.original_turn_index, request_id=e.request_id,
                         evaluator=CHECK_VERSION, category="execution", criterion="Execution completed without infrastructure failures.",
                         outcome="BLOCKED", explanation="Saved execution failure; see event/log/crash evidence.", evidence_ids=[e.event_id])
            assertions.append(row)
            session_results.append(row)
        for diagnostic in diagnostics:
            if diagnostic.kind not in {"integrity", "crash"}:
                continue
            row = Result(assertion_id=f"{instance}:{attempt}:{diagnostic.evidence_id}", scenario_id=scenario_id,
                         scenario_instance_id=instance, attempt=attempt, original_turn_index=diagnostic.data.get("original_turn_index"),
                         request_id=diagnostic.data.get("request_id"), evaluator=CHECK_VERSION, category="execution",
                         criterion="Execution evidence is complete and free of unhandled crashes.",
                         outcome="NEEDS_REVIEW" if diagnostic.data.get("phase") == "evaluate" else "BLOCKED",
                         explanation="Saved crash or damaged journal requires investigation.", evidence_ids=[diagnostic.evidence_id])
            assertions.append(row)
            session_results.append(row)
        session_row = {"scenario_id": scenario_id, "scenario_instance_id": instance, "attempt": attempt,
                       "profiles": scenario.setup_profiles, "priority": scenario.priority, "dataset": scenario.namespace,
                       "outcome": aggregate(r.outcome for r in session_results), "assertion_ids": [r.assertion_id for r in session_results],
                       "assertions": counts(r.outcome for r in session_results), "warm_up": session_warm_up}
        (warm_up_sessions if session_warm_up else sessions).append(session_row)
    # Include execution/crash assertions in the affected turn's aggregation too.
    for turn in [*turns, *warm_up_turns]:
        applicable = [r for r in assertions if r.scenario_instance_id == turn["scenario_instance_id"] and r.attempt == turn["attempt"]
                      and r.original_turn_index in {None, turn["original_turn_index"]}]
        turn.update(outcome=aggregate(r.outcome for r in applicable), assertion_ids=[r.assertion_id for r in applicable],
                    assertions=counts(r.outcome for r in applicable))
    evidence = {r.event_id: r.model_dump() for r in [*bundle.turns, *bundle.events]}
    evidence.update({s.snapshot_id: s.model_dump() for s in bundle.snapshots})
    evidence.update({k.evidence_id: k.model_dump() for k in bundle.knowledge})
    evidence.update({f"scenario:{s.scenario_id}": s.model_dump() for s in bundle.scenarios})
    evidence.update({d.evidence_id: {**d.data, "diagnostic_kind": d.kind, "source_file": d.source} for d in bundle.diagnostics})
    coverage = {}
    for name in ("profile", "priority", "dataset", "intent"):
        groups = defaultdict(list)
        population = turns if name == "intent" else sessions
        for row in population:
            keys = row["profiles"] or ["unspecified"] if name == "profile" else [row[name]]
            for key in keys:
                groups[key].append(row["outcome"])
        coverage[name] = {k: {"unit": "turn" if name == "intent" else "session_attempt", **counts(v)} for k, v in sorted(groups.items())}
    warm_sessions = {(s["scenario_instance_id"], s["attempt"]) for s in warm_up_sessions}
    warm_assertions = [r for r in assertions if (r.scenario_instance_id, r.attempt) in warm_sessions]
    measured_assertions = [r for r in assertions if (r.scenario_instance_id, r.attempt) not in warm_sessions]
    scored_turns = [t for t in bundle.turns if not _turn_warm_up(t, bundle.diagnostics)]
    warm_turns = [t for t in bundle.turns if _turn_warm_up(t, bundle.diagnostics)]
    scored_request_ids = {t.request_id for t in scored_turns}
    scored_measurements = [m for m in bundle.measurements if m.request_id in scored_request_ids]
    workload = {}
    for name in ("input_tokens", "output_tokens"):
        values = [getattr(m, name) for m in scored_measurements if getattr(m, name) is not None]
        workload[name] = {"total": sum(values) if values else None, "measured_requests": len(values), "request_denominator": len(scored_turns)}
    costs = defaultdict(list)
    for m in scored_measurements:
        if m.cost is not None:
            costs[m.currency or "unspecified"].append(m.cost)
    workload["cost"] = {currency: {"total": sum(values), "measured_requests": len(values)} for currency, values in costs.items()}
    workload.update({"latency_ms": distribution(t.elapsed_ms for t in scored_turns),
                     "planned_turns": len(turns),
                     "recorded_turns": len({(t.scenario_instance_id, t.attempt, t.original_turn_index) for t in scored_turns}),
                     "manual_judgments_pending": sum(r.explanation == 'Semantic judgment unavailable: manual_review_pending' for r in measured_assertions),
                     "http_status_counts": dict(Counter(str(t.http_status) for t in scored_turns)),
                     "http_errors": sum(t.http_status is not None and t.http_status >= 400
                                        and t.request_id not in accepted_rejection_ids for t in scored_turns),
                     "transport_errors": sum(t.transport_error is not None for t in scored_turns),
                     "api_errors": (sum(m.api_errors for m in scored_measurements if m.api_errors is not None)
                                    if any(m.api_errors is not None for m in scored_measurements) else None),
                     "api_error_measured_requests": sum(m.api_errors is not None for m in scored_measurements), "request_denominator": len(scored_turns),
                     "async_completion_ms": distribution(r.elapsed_to_completion_ms for r in measured_assertions if r.elapsed_to_completion_ms is not None)})
    workload["runner_reports"] = {d.source: d.data for d in bundle.diagnostics if d.kind == "runner_report"}
    workload["warm_up"] = {
        "latency_ms": distribution(t.elapsed_ms for t in warm_turns),
        "http_errors": sum(t.http_status is not None and t.http_status >= 400
                           and t.request_id not in accepted_rejection_ids for t in warm_turns),
        "request_denominator": len(warm_turns),
        "turns": counts(t["outcome"] for t in warm_up_turns),
        "sessions": counts(s["outcome"] for s in warm_up_sessions),
        "assertions": counts(r.outcome for r in warm_assertions),
        "async_completion_ms": distribution(r.elapsed_to_completion_ms for r in warm_assertions
                                             if r.elapsed_to_completion_ms is not None),
    }
    # Keep warm-up evidence in the report lists, but exclude it from scored rates.
    return {"evaluation_schema_version": "1.0.0", "evaluation_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(), "run_id": bundle.manifest.run_id,
            "provenance": {"manifest": bundle.manifest.model_dump(), "input_hash": canonical_hash(bundle),
                           "checks_hash": canonical_hash([c.model_dump() for c in bundle.checks]),
                           "scenario_hashes": {s.scenario_id: canonical_hash(s) for s in bundle.scenarios},
                           "knowledge_hash": canonical_hash([k.model_dump() for k in bundle.knowledge]),
                           "judge_config": {"model": semantic.judge.model,
                                            "settings": semantic.judge.config.model_dump() if hasattr(semantic.judge, "config") else None},
                           "checker_version": CHECK_VERSION, "rubric_version": RUBRIC_VERSION, "prompt_version": PROMPT_VERSION},
            "overall": {"outcome": aggregate(s["outcome"] for s in sessions), "assertions": counts(r.outcome for r in measured_assertions),
                        "turns": counts(t["outcome"] for t in turns), "sessions": counts(s["outcome"] for s in sessions)},
            "assertions": [r.model_dump() for r in assertions],
            "turns": turns + warm_up_turns, "sessions": sessions + warm_up_sessions,
            "coverage": coverage, "workload_metrics": workload, "judge": {"calls": semantic.audit},
            "manual_review": semantic.pending, "evidence": evidence}


def compare_runs(previous, current):
    a, b = previous["provenance"], current["provenance"]
    reasons = []
    for key in ("dataset_hashes", "configuration_hash", "scenario_plan_hash", "scenario_plan_version", "scenario_ids"):
        if a["manifest"].get(key) != b["manifest"].get(key):
            reasons.append(key)
    for key in ("checks_hash", "checker_version", "rubric_version", "prompt_version", "scenario_hashes", "knowledge_hash", "judge_config"):
        if a.get(key) != b.get(key):
            reasons.append(key)
    # run_id is identity, not behavioral configuration; never treat it as incompatible.
    def grouped(report):
        rows = defaultdict(Counter)
        for s in report["sessions"]:
            if s.get("warm_up"):
                continue
            rows[s["scenario_id"]][s["outcome"]] += 1
        return {k: dict(v) for k, v in rows.items()}
    old, new = grouped(previous), grouped(current)
    return {"previous_evaluation_id": previous["evaluation_id"], "current_evaluation_id": current["evaluation_id"],
            "compatible": not reasons, "incompatibilities": reasons,
            "changes": [{"scenario_id": key, "previous": old.get(key), "current": new.get(key)}
                        for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)],
            "latency_p95_ms": {"previous": previous["workload_metrics"]["latency_ms"]["p95"],
                               "current": current["workload_metrics"]["latency_ms"]["p95"]}}
