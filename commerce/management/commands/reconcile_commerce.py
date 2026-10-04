from django.core.management.base import BaseCommand
from commerce.queue import reconcile


class Command(BaseCommand):
    help = 'Expire stock holds, retry inbox events, and enqueue provider reconciliation.'

    def handle(self, **options):
        self.stdout.write(str(reconcile()))
