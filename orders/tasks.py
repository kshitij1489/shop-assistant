# orders/tasks.py
from __future__ import annotations

from evaluate.controls.celery import EvaluationTask

from celery import shared_task
from django.db import transaction

from chatbot_core.models import TenantInfo


@shared_task(base=EvaluationTask, bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def task_sync_tenant_from_folder(self, tenant_id: int):
    """Refresh tenant knowledge from its configured source folder."""
    from evaluate.controls.context import assert_scope
    assert_scope(tenant_id)
    from orders.services.tenant_sync import sync_tenant_for
    with transaction.atomic():
        tenant = TenantInfo.objects.select_for_update(of=("self",)).get(pk=tenant_id)
    return sync_tenant_for(tenant)
