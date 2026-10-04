"""Bridge a synchronous, atomic conversation turn to a POST event stream."""
from contextvars import copy_context
import json
import logging
from queue import Empty, Full, Queue
from threading import Event, Thread

from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, connections
from django.http import StreamingHttpResponse

from chatbot_core.llm.streaming import reply_stream

logger = logging.getLogger(__name__)


def _frame(event, data):
    return f"event: {event}\ndata: {json.dumps(data, cls=DjangoJSONEncoder)}\n\n"


def stream_turn(work):
    # Capture evaluation/tenant context before the view's decorators exit.
    context = copy_context()

    def events():
        queue = Queue(maxsize=64)
        disconnected = Event()

        def enqueue(event, frame):
            # Bound memory for slow readers; disconnects must not interrupt an
            # already-started business operation or leave its session unsaved.
            while not disconnected.is_set():
                try:
                    queue.put((event, frame), timeout=0.1)
                    return
                except Full:
                    continue

        def send(event, data):
            enqueue(event, _frame(event, data))

        def produce():
            try:
                close_old_connections()
                with reply_stream(send):
                    response, basket = work()
                # work() exits the session turn and commits before completion.
                event, frame = "done", _frame("done", {"response": response, "basket": basket})
            except Exception:
                logger.exception("Unhandled error in streaming chatbot turn")
                event, frame = "error", _frame("error", {
                    "error": "Something went wrong. Please check your basket before trying again.",
                })
            finally:
                connections.close_all()
            enqueue(event, frame)

        # Start only when Django consumes the body, after SessionMiddleware has
        # saved the initial cookie. The producer owns its own DB connection.
        worker = Thread(target=context.run, args=(produce,), daemon=True, name="website-chat-turn")
        worker.start()
        try:
            while True:
                try:
                    event, frame = queue.get(timeout=10)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield frame
                if event in {"done", "error"}:
                    break
        finally:
            disconnected.set()

    response = StreamingHttpResponse(events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
