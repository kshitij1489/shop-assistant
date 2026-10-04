"""Opt-in Celery Task base. Signed dispatch snapshots use real transport time."""
from celery import Task
from .context import activate, current, enabled
from .ownership import worker_ticket, resolve_ticket
from .telemetry import span

KEY = 'evaluation_context_v1'


class EvaluationTask(Task):
    abstract = True

    def apply_async(self, args=None, kwargs=None, **options):
        ctx = current()
        if ctx:
            options['headers'] = {**(options.get('headers') or {}), KEY: worker_ticket(ctx)}
        return super().apply_async(args=args, kwargs=kwargs, **options)

    def __call__(self, *args, **kwargs):
        value = (getattr(self.request, 'headers', None) or {}).get(KEY) if enabled() else None
        # An ordinary task must not inherit another task's context in eager mode.
        ctx = resolve_ticket(value, worker=True)[0] if value else None
        with activate(ctx), span('celery', task_id=str(self.request.id), operation=self.name):
            return super().__call__(*args, **kwargs)
