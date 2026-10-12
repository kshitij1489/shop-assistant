"""Replay checkpoints and explicit external-dependency boundaries for the graph."""
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from uuid import uuid4

from .capture import current, emit
from .replay_codec import Encoder, encode

_active = ContextVar('conversation_replay', default=None)
_step = ContextVar('conversation_replay_step', default=None)
_dependency = ContextVar('conversation_replay_dependency', default=False)


def is_replaying():
    return _active.get() is not None


def checkpoint(event, **values):
    if current() is None or _active.get() is not None:
        return
    try:
        emit(event, snapshot=encode(values))
    except Exception as exc:
        emit('replay.unavailable', boundary=event, error=exc)


def dependency(name, function, *args, **kwargs):
    """Capture one call and its argument mutations, or consume its exact fixture."""
    replay = _active.get()
    if replay is not None:
        return replay.consume(name, args, kwargs)
    if current() is None or _step.get() is None or _dependency.get():
        return function(*args, **kwargs)
    call_id = str(uuid4())
    encoder = Encoder()
    try:
        before = encoder.encode({'args': args, 'kwargs': kwargs})
        emit('replay.dependency.started', step_id=_step.get(), call_id=call_id, name=name, snapshot=before)
    except Exception as exc:
        emit('replay.unavailable', boundary=name, error=exc)
    token = _dependency.set(True)
    try:
        result = function(*args, **kwargs)
    except BaseException as exc:
        try:
            emit('replay.dependency.failed', step_id=_step.get(), call_id=call_id, name=name,
                 error=exc, snapshot=encoder.encode({'args': args, 'kwargs': kwargs, 'error_args': exc.args}))
        except Exception as capture_error:
            emit('replay.unavailable', boundary=name, error=capture_error)
        raise
    else:
        try:
            emit('replay.dependency.completed', step_id=_step.get(), call_id=call_id, name=name,
                 snapshot=encoder.encode({'args': args, 'kwargs': kwargs, 'result': result}))
        except Exception as exc:
            emit('replay.unavailable', boundary=name, error=exc)
        return result
    finally:
        _dependency.reset(token)


def step(function):
    has_runtime = 'runtime' in signature(function).parameters

    @wraps(function)
    def wrapped(state, runtime=None):
        context = runtime.context if runtime is not None else None
        replay = _active.get()
        if replay is not None:
            return replay.run_step(function, state, context, has_runtime)
        if current() is None:
            return function(state, runtime) if has_runtime else function(state)
        step_id = str(uuid4())
        token = _step.set(step_id)
        emit('replay.step.started', step_id=step_id, name=function.__name__)
        checkpoint('replay.step.input', step_id=step_id, state=state, context=context)
        try:
            result = function(state, runtime) if has_runtime else function(state)
        except BaseException as exc:
            checkpoint('replay.step.error_state', step_id=step_id, state=state)
            emit('replay.step.failed', step_id=step_id, error=exc)
            raise
        else:
            checkpoint('replay.step.output', step_id=step_id, state=state, result=result)
            emit('replay.step.completed', step_id=step_id)
            return result
        finally:
            _step.reset(token)
    return wrapped
