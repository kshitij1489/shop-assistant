"""Small synthetic run demonstrating all four outcomes, without an API."""
from pathlib import Path

from evaluate.contracts.models import (Clock, ExecutionEvent, ModelNames, NormalizedScenario,
                                      RunManifest, Setup, StateSnapshot, Turn, TurnEvidence)
from evaluate.checks.models import CheckSpec
from evaluate.judges import ManualJudge
from .artifacts import Knowledge, Measurement, RunArtifacts
from .render import dumps, write_report
from .scoring import evaluate_run


def synthetic_run():
    scenarios, turns, snapshots, events, checks = [], [], [], [], []
    for n, label in enumerate(("pass", "fail", "blocked", "review")):
        sid, request, instance = f"qa:synthetic-{label}", f"request-{label}", f"instance-{label}"
        source = Turn(original_turn_index=0, user_turn_index=0, text="Add one soup.", intent="placing_order",
                      sub_intent="add_to_basket", expected_facts=["Acknowledges one soup in the basket."],
                      must_not=["Claims payment has been captured."])
        scenario = NormalizedScenario(scenario_id=sid, source_id=f"synthetic-{label}", namespace="qa", source_hash=str(n) * 64,
                                      scenario_plan_version="synthetic-v1", priority="P0" if n < 2 else "P1", summary="Synthetic saved evidence example.",
                                      tags=["synthetic"], setup_profiles=["catalog_sandbox"], clock=Clock(at="2026-09-28T12:00:00+05:30"),
                                      setup=Setup(currency="INR"), setup_inputs=["menu"], turns=[source], references=[],
                                      knowledge_refs=["menu"], workflow_refs=[], actions=[], blockers=[])
        scenarios.append(scenario)
        identity = dict(run_id="synthetic-run", scenario_id=sid, scenario_instance_id=instance, attempt=1)
        turn = TurnEvidence(**identity, event_id=f"turn-{label}", original_turn_index=0, user_turn_index=0, request_id=request,
                            message=source.text, response_text="One soup is in your basket." + (' <script>alert("unsafe")</script>' if label == "fail" else ""),
                            http_status=200, elapsed_ms=100.0 + n * 25, snapshot_ids=[f"after-{label}"], branch="not_applicable")
        turns.append(turn)
        for phase in ("before", "after"):
            quantity = 1 if label != "fail" else 2
            state = {"projection_version": "commerce-evaluation-v1", "basket": {"items": [] if phase == "before" else
                     [{"item_id": "soup", "variant_id": "standard", "quantity": quantity, "unit_price_minor": 15000}],
                     "subtotal_minor": 0 if phase == "before" else quantity * 15000, "total_minor": 0 if phase == "before" else quantity * 15000,
                     "fee_minor": 0, "tax_minor": 0, "discount_minor": 0, "currency": "INR"},
                     "orders": [], "addresses": [], "payments": [], "effects": []}
            unavailable = []
            if label == "blocked" and phase == "after":
                del state["basket"]
                unavailable = ["basket"]
            snapshots.append(StateSnapshot(**identity, event_id=f"snapshot-event-{phase}-{label}", snapshot_id=f"{phase}-{label}",
                                           original_turn_index=0, request_id=request, phase=phase,
                                           captured_at=f"2026-09-28T06:30:0{0 if phase == 'before' else 1}+00:00", state=state,
                                           unavailable_sections=unavailable))
        events.append(ExecutionEvent(**identity, event_id=f"response-{label}", occurred_at="2026-09-28T06:30:01+00:00", kind="response",
                                     request_id=request, original_turn_index=0, user_turn_index=0, status="succeeded", detail="Synthetic response log."))
        checks.append(CheckSpec(check_id=f"basket-{label}", scenario_id=sid, original_turn_index=0, kind="basket",
                                criterion="Basket contains exactly one soup.", expected={"items": [{"item_id": "soup", "variant_id": "standard", "quantity": 1}]}))
        checks.append(CheckSpec(check_id=f"total-{label}", scenario_id=sid, original_turn_index=0, kind="totals", category="money",
                                criterion="Basket total is INR 150 with consistent arithmetic.", expected={"total_minor": 15000, "currency": "INR"}))
    manifest = RunManifest(run_id="synthetic-run", application={"commit": "0" * 40, "working_tree_fingerprint": "0" * 64, "dirty": False},
                           dataset_hashes={"synthetic.json": "1" * 64}, configuration_hash="2" * 64, scenario_plan_hash="3" * 64,
                           scenario_plan_version="synthetic-v1", models=ModelNames(chat="synthetic", translate="synthetic", analytics="synthetic"),
                           scenario_ids=[s.scenario_id for s in scenarios], created_at="2026-09-28T06:30:00+00:00")
    return RunArtifacts(manifest=manifest, scenarios=scenarios, turns=turns, snapshots=snapshots, events=events, checks=checks,
                        knowledge=[Knowledge(evidence_id="knowledge:menu", source="menu", content={"soup": {"price_minor": 15000, "currency": "INR"}})],
                        measurements=[Measurement(request_id=t.request_id, input_tokens=50, output_tokens=20, cost=.001, currency="USD") for t in turns])


def example_report(bundle):
    initial = evaluate_run(bundle)
    decisions = []
    for row in initial["manual_review"]:
        if row["assertion_id"].startswith("request-review:"):
            continue
        decisions.append({"assertion_id": row["assertion_id"], "context_hash": row["context_hash"], "reviewer": "synthetic-example-reviewer",
                          "verdict": {"outcome": "PASS", "reason": "Synthetic wording acknowledges soup and makes no payment claim; state is checked independently.",
                                      "evidence_ids": [row["context"]["conversation"][-1]["event_id"]]}})
    report = evaluate_run(bundle, ManualJudge(decisions))
    report["evaluation_id"] = "synthetic-example"
    report["created_at"] = "2026-09-28T06:31:00+00:00"
    return report, decisions


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    bundle = synthetic_run()
    report, decisions = example_report(bundle)
    # Exclusive writes also protect the example's input evidence.
    for filename, value in (("artifacts.json", bundle.model_dump()), ("manual-decisions.json", decisions)):
        with (args.output / filename).open("x", encoding="utf-8") as stream:
            stream.write(dumps(value))
    print(write_report(report, args.output))


if __name__ == "__main__":
    main()
