"""Inspect logical storage and local metrics, or run bounded retention manually."""
import json

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from chatbot_core.models import SemanticCacheEntry, SemanticCacheState
from chatbot_core.vector_store.config import policy
from chatbot_core.vector_store.semantic_cache import prune, stats


class Command(BaseCommand):
    help = "Inspect the answer cache; --prune enforces retention and capacity."

    def add_arguments(self, parser):
        parser.add_argument("--prune", action="store_true")

    def handle(self, *args, **options):
        deleted = prune() if options["prune"] else 0
        rows = SemanticCacheEntry.objects.all()
        config = policy()
        self.stdout.write(json.dumps({
            "enabled": config.enabled, "embedding_id": config.embedding_id,
            "rows": rows.count(), "logical_bytes": rows.aggregate(size=Sum("size_bytes"))["size"] or 0,
            "expired_rows": rows.filter(expires_at__lte=timezone.now()).count(),
            "revision": SemanticCacheState.objects.filter(pk=1).values_list("revision", flat=True).first() or 0,
            "deleted": deleted, "process_metrics": stats(),
        }, sort_keys=True))
