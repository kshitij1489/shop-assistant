"""Scoped text streaming and publication of validated, composed turn replies."""
from contextlib import contextmanager
from contextvars import ContextVar
from .replies import join_replies, reply_separator


_sink = ContextVar("reply_stream_sink", default=None)
_prefix = ContextVar("reply_stream_prefix", default=None)


@contextmanager
def reply_stream(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def publish_reply(text):
    """Expose composed text only after structured reply validation has finished."""
    sink = _sink.get()
    if sink is not None:
        sink('replace', {'text': text})


@contextmanager
def final_reply(enabled, replies):
    # Use exactly the same separator as the final assembled reply.
    prefix = join_replies(replies)
    if prefix:
        prefix += reply_separator(prefix)
    token = _prefix.set(prefix if enabled else None)
    try:
        yield
    finally:
        _prefix.reset(token)


def invoke_reply(chain, values):
    """Keep complete text for validation/cache; expose only answer text chunks."""
    sink, prefix = _sink.get(), _prefix.get()
    if sink is None or prefix is None:
        return chain.invoke(values)
    sink("replace", {"text": prefix})
    chunks = []
    stream = chain.stream(values)
    try:
        for chunk in stream:
            if chunk:
                chunks.append(chunk)
                sink("delta", {"text": chunk})
    except Exception:
        # The caller may return a fallback. Do not leave failed partial text up.
        sink("replace", {"text": prefix})
        raise
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return "".join(chunks)
