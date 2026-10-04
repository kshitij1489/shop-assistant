"""Generate reviewed deterministic CheckSpecs from normalized scenario structure.

Uses only actions, setup, and preconditions already present on the scenario.
Does not invent semantic rubrics or free-form expected reply criteria.
knowledge_only scenarios receive no state checks.
"""
from evaluate.checks.models import CheckSpec


def _stateful(scenario) -> bool:
    profiles = list(scenario.setup_profiles or [])
    return bool(profiles) and any(p != "knowledge_only" for p in profiles)


def _payment_status(operation: str) -> str | None:
    return {"capture": "captured", "fail": "failed", "cancel": "cancelled"}.get(operation)


def generate_check_specs(scenarios) -> list[CheckSpec]:
    """Build CheckSpecs for basket/totals, payment, ownership/privacy, and duplicate effects."""
    specs: list[CheckSpec] = []
    for scenario in scenarios:
        if not _stateful(scenario):
            continue
        turns = [t.original_turn_index for t in scenario.turns]
        if not turns:
            continue
        first = turns[0]
        currency = scenario.setup.currency
        # Structural coverage for every stateful turn: no duplicate durable effects.
        for index in turns:
            specs.append(CheckSpec(
                check_id=f"{scenario.scenario_id}:turn-{index}:selected-address-owner",
                scenario_id=scenario.scenario_id, original_turn_index=index,
                kind="equals", category="privacy", path="address_selection/authorized",
                criterion="The selected address, if any, belongs to the active tenant/customer.",
                expected={"value": True}))
            specs.append(CheckSpec(
                check_id=f"{scenario.scenario_id}:turn-{index}:duplicate-effects",
                scenario_id=scenario.scenario_id, original_turn_index=index,
                kind="duplicate_effects", category="state",
                criterion="Durable commerce effects remain unique per operation identity.",
                expected={"keys": ["operation_id", "kind"]}))
            specs.append(CheckSpec(
                check_id=f"{scenario.scenario_id}:turn-{index}:duplicate-order-effects",
                scenario_id=scenario.scenario_id, original_turn_index=index,
                kind="duplicate_effects", category="state",
                criterion="Payment creation and order submission occur at most once per order.",
                expected={"keys": ["order_id", "kind"], "kinds": ["payment.create", "order.submit"]}))
            for path in ("orders", "payments", "addresses"):
                specs.append(CheckSpec(
                    check_id=f"{scenario.scenario_id}:turn-{index}:ownership:{path}",
                    scenario_id=scenario.scenario_id, original_turn_index=index,
                    kind="ownership", category="privacy", path=path,
                    criterion=f"Projected {path} records stay scoped to the owned tenant/customer.",
                    expected={"scope_path": "ownership"}))

        for action in scenario.actions:
            op = action.operation
            index = first if action.original_turn_index is None else action.original_turn_index
            if index not in turns:
                continue
            if op.kind == "payment_control":
                status = _payment_status(op.operation)
                if status is None:
                    continue
                fields: dict = {"status": status, "currency": op.currency}
                if op.amount_minor is not None:
                    fields["amount_minor"] = op.amount_minor
                specs.append(CheckSpec(
                    check_id=f"{action.action_id}:payment",
                    scenario_id=scenario.scenario_id, original_turn_index=index,
                    kind="payment", category="money",
                    criterion=f"Payment control {op.operation} matches saved payment fields.",
                    expected={"fields": fields, "count": 1},
                    timing="eventual", deadline_ms=30_000.0))
                _append_totals(specs, scenario, index, currency)
            elif op.kind in {"catalog_control", "set_delivery_fee"} and (
                    op.kind == "set_delivery_fee" or op.price_minor is not None):
                specs.append(CheckSpec(
                    check_id=f"{action.action_id}:quote-invalidated",
                    scenario_id=scenario.scenario_id, original_turn_index=index,
                    kind="quote_invalidated", category="money",
                    criterion="Retire the prior quote after a price/fee change; any replacement quote must have consistent money totals.",
                    expected={'replacement_totals': {'currency': currency} if currency else {}}))
    from .reviewed import reviewed_checks
    for scenario in scenarios:
        specs.extend(reviewed_checks(scenario))
    return specs


def _append_totals(specs: list[CheckSpec], scenario, index: int, currency: str | None) -> None:
    if not currency:
        return
    check_id = f"{scenario.scenario_id}:turn-{index}:totals"
    if any(spec.check_id == check_id for spec in specs):
        return
    specs.append(CheckSpec(
        check_id=check_id, scenario_id=scenario.scenario_id, original_turn_index=index,
        kind="totals", category="money",
        criterion="Basket money fields use consistent integer minor-unit arithmetic.",
        expected={"currency": currency}))
