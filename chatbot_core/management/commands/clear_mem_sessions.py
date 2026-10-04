# core/management/commands/clear_mem_sessions.py
from django.core.management.base import BaseCommand
from chatbot_core.logic.cafe.session import memory

class Command(BaseCommand):
    help = "Clear in-process memory session store (DEV ONLY)."
    def handle(self, *args, **opts):
        memory._session_data.clear()
        self.stdout.write(self.style.SUCCESS("Cleared _session_data"))
