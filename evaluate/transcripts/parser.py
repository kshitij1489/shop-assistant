"""Read both source files through the existing dataset parser and setup plan."""
from pathlib import Path

from evaluate.contracts.models import NormalizedScenario
from evaluate.scenarios.plan import load_plan

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "test_data"


def parse_test_cases(dataset: str | Path = DEFAULT_DATASET) -> tuple[list[NormalizedScenario], list[NormalizedScenario]]:
    """Return (sessions, sanity); sanity cases each contain one user query.

    Normalized scenarios preserve setup actions and original turn indexes. Their
    ``turns`` contain only user queries; assistant references are never sent.
    Counts and ordering come from the JSON, not its descriptive count metadata.
    """
    _, bundle = load_plan(Path(dataset))
    sessions = [case for case in bundle.scenarios if case.namespace == "sessions"]
    sanity = [case for case in bundle.scenarios if case.namespace == "qa"]
    return sessions, sanity


def suite_names(values: list[str]) -> list[str]:
    """Accept sessions, sanity, session sanity, and quoted combinations."""
    selected = []
    for value in values:
        for name in value.split():
            name = "sessions" if name == "session" else name
            if name not in {"sessions", "sanity"}:
                raise ValueError(f"Unknown suite {name!r}; choose sessions or sanity")
            if name not in selected:
                selected.append(name)
    if not selected:
        raise ValueError("Select sessions, sanity, or both")
    return selected
