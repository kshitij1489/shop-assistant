import json
from threading import Condition
from unittest.mock import patch
from django.test import override_settings


class CaptureMixin:
    """Capture the actual serialized queue payload without Redis or SQL writes."""
    def setUp(self):
        super().setUp()
        self.events = []
        self.recording_changed = Condition()
        self.enterContext(override_settings(CONVERSATION_RECORDING_ENABLED=True))
        self.handoff = self.enterContext(patch('chatbot_core.recording.transport.enqueue',
            side_effect=self.capture_recording))

    def capture_recording(self, data):
        with self.recording_changed:
            self.events.append(json.loads(data))
            self.recording_changed.notify_all()

    def wait_for_recordings(self):
        def finished():
            started = {e['segment_id'] for e in self.events if e['event'] == 'recording.started'}
            ended = {e['segment_id'] for e in self.events if e['event'] == 'recording.completed'}
            return started == ended
        with self.recording_changed:
            self.assertTrue(self.recording_changed.wait_for(finished, timeout=5), 'Recording producer did not finish')

    def event(self, name):
        return next(e for e in self.events if e['event'] == name)
