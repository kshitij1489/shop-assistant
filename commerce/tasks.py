from evaluate.controls.celery import EvaluationTask
from celery import shared_task


@shared_task(base=EvaluationTask, name='commerce.tasks.reconcile_commerce', acks_late=True)
def reconcile_commerce():
    from evaluate.controls.context import current
    if current():
        from evaluate.contracts.interfaces import Blocked
        raise Blocked('Global reconciliation is not an owned evaluation operation')
    from .queue import reconcile
    return reconcile()
