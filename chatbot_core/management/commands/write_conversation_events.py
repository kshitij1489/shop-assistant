import logging
import os
import signal
import socket
from threading import Event
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from chatbot_core.recording.transport import client
from chatbot_core.recording.writer import Writer

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Persist conversation Redis Stream events; acknowledge only after SQL commit.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--batch-size', type=int, default=100)
        parser.add_argument('--reclaim-ms', type=int, default=60000)

    def handle(self, *args, **options):
        stopped = Event()
        consumer = f'{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}'
        writer = Writer(client(writer=True), consumer, batch_size=options['batch_size'],
                        reclaim_ms=options['reclaim_ms'])
        if not options['once']:
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
        while not stopped.is_set():
            try:
                close_old_connections()
                writer.setup()
                writer.tick(block=1 if options['once'] else 1000)
            except Exception as exc:
                logger.error('Recording writer failed error_type=%s; entries remain pending', type(exc).__name__)
                if options['once']:
                    raise
                stopped.wait(2)
            if options['once']:
                break
