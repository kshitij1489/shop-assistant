"""Sequential and bounded-concurrency scheduling with load metrics.

Sessions launch through a single launcher loop that enforces ramp-up, pacing,
duration, session/request limits and the live-call budget. Worker threads honor
scenario exclusivity and the provider isolation limit before provisioning.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from evaluate.contracts.models import NormalizedScenario
from evaluate.evidence.records import LatencySummary, LoadReport, PhaseMetrics
from evaluate.runner.budget import BudgetExhausted, BudgetTracker
from evaluate.runner.context import RunContext
from evaluate.runner.options import EXCLUSIVE_OPERATIONS
from evaluate.runner.scenario import AttemptRequest, AttemptResult, ScenarioExecutor


@dataclass(frozen=True)
class WorkItem:
    scenario: NormalizedScenario
    scenario_instance_id: str
    first_attempt: int = 1
    repetition: int = 0
    configured: bool = True  # false for repetitions added only to fill a duration
    warm_up: bool | None = None

    @property
    def exclusive(self) -> bool:
        if any(a.operation.kind in EXCLUSIVE_OPERATIONS for a in self.scenario.actions):
            return True
        return False

    @property
    def uses_provider(self) -> bool:
        return self.scenario.setup.payment == "fake_adapter"


class ExclusivityGate:
    """Readers/writer gate: exclusive sessions run alone and are not starved."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._shared = 0
        self._exclusive_active = False
        self._exclusive_waiting = 0

    def acquire(self, exclusive: bool) -> None:
        with self._condition:
            if exclusive:
                self._exclusive_waiting += 1
                self._condition.wait_for(lambda: not self._exclusive_active and self._shared == 0)
                self._exclusive_waiting -= 1
                self._exclusive_active = True
            else:
                self._condition.wait_for(lambda: not self._exclusive_active and self._exclusive_waiting == 0)
                self._shared += 1

    def release(self, exclusive: bool) -> None:
        with self._condition:
            if exclusive:
                self._exclusive_active = False
            else:
                self._shared -= 1
            self._condition.notify_all()


class ConcurrencyMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self._weighted = 0.0
        self._last = time.monotonic()

    def _tick(self) -> None:
        now = time.monotonic()
        self._weighted += self._active * (now - self._last)
        self._last = now

    def enter(self) -> None:
        with self._lock:
            self._tick()
            self._active += 1
            self.max_active = max(self.max_active, self._active)

    def leave(self) -> None:
        with self._lock:
            self._tick()
            self._active -= 1

    def mean(self, elapsed: float) -> float:
        with self._lock:
            self._tick()
            return self._weighted / elapsed if elapsed > 0 else 0.0


@dataclass
class PhaseAccumulator:
    started: int = 0
    completed: int = 0
    requests: int = 0
    request_errors: int = 0
    session_errors: int = 0
    latencies: list[float] = field(default_factory=list)

    def absorb_attempt(self, result: AttemptResult) -> None:
        """Count requests and latency. Restarts and continuations are not extra sessions."""
        self.requests += len(result.latencies_ms)
        self.request_errors += result.request_errors
        self.latencies.extend(result.latencies_ms)

    def close_session(self, result: AttemptResult) -> None:
        self.completed += 1
        self.session_errors += result.outcome == "ERROR"

    def metrics(self) -> PhaseMetrics:
        ordered = sorted(self.latencies)
        return PhaseMetrics(sessions_started=self.started, sessions_completed=self.completed, requests_sent=self.requests,
                            request_errors=self.request_errors, session_errors=self.session_errors,
                            latency=LatencySummary(count=len(ordered), p50_ms=_percentile(ordered, 50),
                                                   p95_ms=_percentile(ordered, 95), max_ms=ordered[-1] if ordered else None))


def _percentile(ordered: list[float], percent: int) -> float | None:
    if not ordered:
        return None
    rank = max(0, min(len(ordered) - 1, round(percent / 100 * len(ordered)) - 1))
    return ordered[rank]


class Scheduler:
    def __init__(self, context: RunContext, budget: BudgetTracker, executor: ScenarioExecutor,
                 on_result: Callable[[AttemptResult], None]) -> None:
        self.context, self.budget, self.executor, self.on_result = context, budget, executor, on_result
        self.load = context.options.load
        self.gate = ExclusivityGate()
        self.provider_slots = threading.Semaphore(self.load.max_provider_sessions)
        self.meter = ConcurrencyMeter()
        self.warm_up, self.measured = PhaseAccumulator(), PhaseAccumulator()
        self._metrics_lock = threading.Lock()
        self.stop_reason = "completed"

    # -- launching --------------------------------------------------------------------
    def run(self, items: list[WorkItem], extend: Callable[[int], list[WorkItem]] | None = None,
            first_extra_repetition: int = 0) -> LoadReport:
        """Launch `items`, then extra repetitions while a duration limit still has time.

        Configured items that never launch are recorded as SKIPPED. Extra
        repetitions exist only to fill `duration_seconds` and are not recorded
        when the clock runs out before they start.
        """
        started = time.monotonic()
        futures: list[Future] = []
        queue = list(items)
        cursor = 0
        repetition = first_extra_repetition
        with ThreadPoolExecutor(max_workers=self.load.concurrency) as pool:
            try:
                while True:
                    if cursor >= len(queue):
                        if not self._duration_open(started) or extend is None:
                            break
                        queue.extend(extend(repetition))
                        repetition += 1
                        if cursor >= len(queue):
                            break
                    if not self._admit(cursor, started, futures):
                        self._skip_unstarted(queue[cursor:])
                        break
                    item = queue[cursor]
                    warm_up = item.warm_up if item.warm_up is not None else cursor < self.load.warm_up_sessions
                    (self.warm_up if warm_up else self.measured).started += 1
                    futures.append(pool.submit(self._run_item, item, warm_up))
                    cursor += 1
            except KeyboardInterrupt:
                self.stop_reason = "interrupted"
                self.context.stop_event.set()
            try:
                wait(futures)
            except KeyboardInterrupt:
                # Let in-flight sessions finish their current request and record evidence.
                self.stop_reason = "interrupted"
                self.context.stop_event.set()
                wait(futures)
        for future in futures:
            future.result()  # surface programming errors instead of hiding them
        return self._report(time.monotonic() - started)

    def _duration_open(self, started: float) -> bool:
        limit = self.load.duration_seconds
        if limit is None or self.context.stop_event.is_set() or self.budget.exhausted:
            return False
        if time.monotonic() - started >= limit:
            self.stop_reason = "duration_elapsed"
            return False
        return self.stop_reason == "completed"

    def _skip_unstarted(self, items: list[WorkItem]) -> None:
        if self.stop_reason == "interrupted":
            return  # No final decisions for work a resumed run still needs to launch.
        reason = f"not started: {self.stop_reason.replace('_', ' ')}"
        for item in items:
            if item.configured:
                self.on_result(self.executor.note_skip(item, reason, warm_up=bool(item.warm_up)))

    def _admit(self, index: int, started: float, futures: list[Future]) -> bool:
        """Block until a session may launch; False stops launching with a recorded reason.

        Limits are checked after waiting for a slot so that work finishing during
        the wait (for example exhausting the request budget) prevents the launch.
        """
        self._wait_for_pacing(index, started)
        self._wait_for_ramp(started, futures)
        if self.context.stop_event.is_set():
            self.stop_reason = "interrupted"
            return False
        if self.load.duration_seconds is not None and time.monotonic() - started >= self.load.duration_seconds:
            self.stop_reason = "duration_elapsed"
            return False
        if self.budget.exhausted:
            self.stop_reason = "request_limit" if "request" in (self.budget.exhausted_reason or "") else "budget_exhausted"
            return False
        try:
            self.budget.reserve_session()
        except BudgetExhausted:
            self.stop_reason = "session_limit"
            return False
        return True

    def _wait_for_pacing(self, index: int, started: float) -> None:
        if self.load.pacing_seconds and index:
            target = started + index * self.load.pacing_seconds
            _sleep_until(target, self.context.stop_event)

    def _wait_for_ramp(self, started: float, futures: list[Future]) -> None:
        while not self.context.stop_event.is_set():
            active = sum(not f.done() for f in futures)
            if active < self._allowed_workers(time.monotonic() - started):
                return
            time.sleep(0.01)

    def _allowed_workers(self, elapsed: float) -> int:
        if not self.load.ramp_up_seconds:
            return self.load.concurrency
        fraction = min(1.0, elapsed / self.load.ramp_up_seconds)
        return 1 + int(fraction * (self.load.concurrency - 1))

    # -- executing ----------------------------------------------------------------------
    def _run_item(self, item: WorkItem, warm_up: bool) -> None:
        self.gate.acquire(item.exclusive)
        if item.uses_provider:
            self.provider_slots.acquire()
        self.meter.enter()
        try:
            self._run_attempts(item, warm_up)
        finally:
            self.meter.leave()
            if item.uses_provider:
                self.provider_slots.release()
            self.gate.release(item.exclusive)

    def _run_attempts(self, item: WorkItem, warm_up: bool) -> None:
        attempt = item.first_attempt
        max_attempts = self.context.options.recovery.max_attempts
        final: AttemptResult | None = None
        while True:
            result = self.executor.run_attempt(AttemptRequest(item.scenario, item.scenario_instance_id, attempt, warm_up=warm_up))
            self._account(result)
            final = result
            if not result.restartable or attempt >= max_attempts or self.context.stop_event.is_set():
                break
            attempt += 1  # fresh identity, same instance; earlier evidence is retained
        if final is not None:
            self._close_session(final)
            self._continue(item, final, warm_up)

    def _continue(self, item: WorkItem, result: AttemptResult, warm_up: bool) -> None:
        plan = result.continuation
        if plan is None or self.context.stop_event.is_set():
            return
        try:
            self.budget.reserve_session()
        except BudgetExhausted:
            skipped = self.executor.note_skip(
                WorkItem(item.scenario, plan.scenario_instance_id, configured=False),
                "continuation withheld: session limit", warm_up=warm_up)
            self.on_result(skipped)
            return
        phase = self.warm_up if warm_up else self.measured
        with self._metrics_lock:
            phase.started += 1
        request = AttemptRequest(item.scenario, plan.scenario_instance_id, 1, plan.start_user_turn_index,
                                 [plan.setup_action], warm_up, plan.parent_instance_id)
        continued = self.executor.run_attempt(request)
        self._account(continued)
        self._close_session(continued)

    def _account(self, result: AttemptResult) -> None:
        with self._metrics_lock:
            (self.warm_up if result.warm_up else self.measured).absorb_attempt(result)
        self.on_result(result)

    def _close_session(self, result: AttemptResult) -> None:
        with self._metrics_lock:
            (self.warm_up if result.warm_up else self.measured).close_session(result)

    def _report(self, elapsed: float) -> LoadReport:
        if self.context.stop_event.is_set():
            self.stop_reason = "interrupted"
        if self.budget.exhausted and self.stop_reason == "completed":
            self.stop_reason = "budget_exhausted"
        return LoadReport(
            run_id=self.context.config.run_id, configured_concurrency=self.load.concurrency,
            achieved_concurrency_max=self.meter.max_active, achieved_concurrency_mean=round(self.meter.mean(elapsed), 3),
            ramp_up_seconds=self.load.ramp_up_seconds, pacing_seconds=self.load.pacing_seconds,
            duration_seconds=self.load.duration_seconds, elapsed_seconds=round(elapsed, 3), stop_reason=self.stop_reason,
            warm_up=self.warm_up.metrics(), measured=self.measured.metrics(),
        )


def _sleep_until(target: float, stop: threading.Event) -> None:
    while not stop.is_set():
        remaining = target - time.monotonic()
        if remaining <= 0:
            return
        stop.wait(min(remaining, 0.05))
