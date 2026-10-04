"""Offline deterministic checks over saved, scoped evidence."""

from .engine import DeterministicEvaluator
from .models import CheckSpec, Result
from .plan_checks import generate_check_specs

__all__ = ["CheckSpec", "DeterministicEvaluator", "Result", "generate_check_specs"]
