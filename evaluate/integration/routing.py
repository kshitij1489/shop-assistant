"""Which reviewed action the website lane applies inside the application process."""
from __future__ import annotations

APPLICATION_LOOKUPS = frozenset({
    ("classification", "timeout"),
    ("classification", "success"),
    ("coverage", "unavailable"),
    ("coverage", "success"),
})


def control_lane(kind: str, service: str | None = None) -> str:
    """Return `application` or `fixtures` for one typed operation kind.

    Clock and classifier/coverage faults change the signed request context.
    Geocoding, catalog, payment and fixture operations stay on their own adapters.
    """
    if kind == "freeze_clock":
        return "application"
    if kind == "lookup_control" and service in {"classification", "coverage"}:
        return "application"
    return "fixtures"
