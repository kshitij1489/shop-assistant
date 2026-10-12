"""Capture detached JSON at execution boundaries; never defer reads of live state."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from functools import wraps
import inspect
import json
import logging
import re
from threading import RLock
from time import perf_counter
from uuid import UUID, uuid4

from django.conf import settings
from django.db.models import Model
from django.utils.crypto import salted_hmac
from pydantic import BaseModel, SecretStr
from .replay_codec import ReplaySnapshot

logger = logging.getLogger(__name__)
_active = ContextVar('conversation_recording', default=None)
_parent = ContextVar('recording_operation', default=None)
_SECRET_KEY = re.compile(r'(^|_)(api_key|secret|password|authorization|cookie|credential|token|access_token|bot_token|lease_token|session_key|headers)(_|$)', re.I)
_TEXT_SECRET = re.compile(r'(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|https?://[^\s/@]+:[^\s/@]+@)')


def current():
    return _active.get()


def session_digest(tenant_id, channel, session_id):
    return salted_hmac('recording.session', f'{tenant_id}:{channel}:{session_id}').hexdigest()


def mark_failed():
    """Preserve an unrecovered failure even when the channel sends a fallback."""
    recorder = current()
    if recorder:
        with recorder.lock:
            recorder.failed = True


def snapshot(value, secrets=()):
    """Explicit serializers only: no repr(), lazy relation access, or credentials."""
    if isinstance(value, ReplaySnapshot):
        return value.redacted(secrets)
    if isinstance(value, SecretStr):
        return '[REDACTED]'
    if isinstance(value, Enum):
        return snapshot(value.value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '[REDACTED]')
        return _TEXT_SECRET.sub('[REDACTED]', value)
    if isinstance(value, (UUID, Decimal, date, datetime)):
        return str(value)
    if isinstance(value, BaseException):
        return {'type': type(value).__name__, 'message': snapshot(str(value), secrets)}
    if isinstance(value, BaseModel):
        return snapshot(value.model_dump(mode='python'), secrets)
    if isinstance(value, type) and issubclass(value, BaseModel):
        return snapshot(value.model_json_schema(), secrets)
    if isinstance(value, Model):
        # Never fetch deferred fields or traverse foreign keys while recording.
        if value._meta.label_lower == 'chatbot_core.tenantinfo':
            return {'id': str(value.pk), 'type': value._meta.label}
        return snapshot({f.attname: value.__dict__[f.attname] for f in value._meta.concrete_fields
                         if f.attname in value.__dict__}, secrets)
    if hasattr(value, 'to_dict'):
        data = value.to_dict()
        if hasattr(value, 'resolved_action'):
            data = {**data, 'resolved_action': value.resolved_action}
        return snapshot(data, secrets)
    if is_dataclass(value):
        return snapshot({f.name: getattr(value, f.name) for f in fields(value)}, secrets)
    if isinstance(value, dict):
        return {snapshot(str(k), secrets): '[REDACTED]' if _SECRET_KEY.search(str(k)) else snapshot(v, secrets)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [snapshot(v, secrets) for v in value]
    if type(value).__module__.split('.')[0] == 'numpy' and hasattr(value, 'tolist'):
        return snapshot(value.tolist(), secrets)
    raise TypeError(f'Unsupported recording value: {type(value).__name__}')


class Recording:
    def __init__(self, tenant, channel, session_id, *, origin=None, kind='turn'):
        self.identity = origin or {
            'tenant_id': str(tenant.pk), 'channel': str(channel),
            # A browser session key is an authentication credential.
            'session_id': session_digest(tenant.pk, channel, session_id),
            'turn_id': str(uuid4()),
        }
        if str(self.identity['tenant_id']) != str(tenant.pk):
            raise ValueError('Recording tenant mismatch')
        self.segment_id, self.kind = str(uuid4()), kind
        self.sequence, self.dropped = 0, 0
        self.transport_failed = False
        self.failed = False
        self.closed = False
        self.lock = RLock()
        self.secrets = tuple(sorted({str(v) for v in (
            getattr(tenant, 'api_key', None), getattr(tenant, 'telegram_bot_token', None),
            getattr(settings, 'OPENAI_API_KEY', None), getattr(settings, 'SECRET_KEY', None),
            getattr(settings, 'JWT_SECRET', None), session_id if channel == 'website' else None,
        ) if v}, key=len, reverse=True))

    def emit(self, event, **payload):
        # Sequence allocation and enqueue order also cover LangChain worker threads.
        with self.lock:
            if self.closed:
                return
            self.sequence += 1
            if self.transport_failed and event != 'recording.completed':
                self.dropped += 1
                return
            try:
                envelope = dict(schema_version=1, event_id=str(uuid4()), **self.identity,
                    segment_id=self.segment_id, segment_kind=self.kind, sequence=self.sequence,
                    occurred_at=datetime.now(timezone.utc).isoformat(), event=event,
                    parent_operation_id=_parent.get(), payload=snapshot(payload, self.secrets))
                encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            except Exception as exc:
                self.dropped += 1
                # Do not log the exception text: provider/transport errors may contain secrets.
                logger.warning('Recording capture failed segment=%s sequence=%s error_type=%s',
                               self.segment_id, self.sequence, type(exc).__name__)
                return
            try:
                from .transport import enqueue
                enqueue(encoded)
            except Exception as exc:
                from .transport import EventTooLarge
                self.dropped += 1
                self.transport_failed = not isinstance(exc, EventTooLarge)
                logger.warning('Recording handoff failed segment=%s sequence=%s error_type=%s',
                               self.segment_id, self.sequence, type(exc).__name__)

    def finish(self, status):
        with self.lock:
            if not self.closed:
                self.emit('recording.completed', status='failed' if self.failed else status, event_count=self.sequence + 1,
                          dropped_events=self.dropped)
                self.closed = True

    def related(self, event, *, kind='delivery', **payload):
        """Delivery may be observed after the producer finishes, or on another thread."""
        child = object.__new__(Recording)
        child.identity, child.secrets = dict(self.identity), self.secrets
        child.segment_id, child.kind = str(uuid4()), kind
        child.sequence, child.dropped, child.closed = 0, 0, False
        child.transport_failed = False
        child.failed = False
        child.lock = RLock()
        child.emit('recording.started')
        child.emit(event, **payload)
        child.finish('succeeded')


def emit(event, **payload):
    recorder = current()
    if recorder:
        recorder.emit(event, **payload)


@contextmanager
def recording(tenant, channel, session_id, *, query=None, origin=None, kind='turn'):
    existing = current()
    if existing is not None:
        if existing.identity['tenant_id'] != str(tenant.pk):
            raise ValueError('Recording tenant mismatch')
        if origin is None and (existing.identity['channel'] != str(channel) or
                existing.identity['session_id'] != session_digest(tenant.pk, channel, session_id)):
            raise ValueError('Recording session mismatch')
        yield existing
        return
    if not getattr(settings, 'CONVERSATION_RECORDING_ENABLED', False):
        yield None
        return
    recorder = Recording(tenant, channel, session_id, origin=origin, kind=kind)
    token = _active.set(recorder)
    parent_token = _parent.set(None)
    status = 'succeeded'
    try:
        recorder.emit('recording.started', query=query)
        yield recorder
    except BaseException as exc:
        status = 'failed'
        recorder.emit('recording.failed', error=exc)
        raise
    finally:
        recorder.finish(status)
        _parent.reset(parent_token)
        _active.reset(token)


def operation(name, *, exclude=()):
    """Record arguments, output and mutations around one Python operation."""
    def decorate(fn):
        signature = inspect.signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            if current() is None:
                return fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = {k: v for k, v in bound.arguments.items()
                         if k not in {'self', 'runtime', 'request', 'session', *exclude}}
            operation_id, started = str(uuid4()), perf_counter()
            emit('operation.started', operation=name, operation_id=operation_id, arguments=arguments)
            token = _parent.set(operation_id)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                emit('operation.failed', operation=name, operation_id=operation_id,
                     error=exc, state_after=arguments, elapsed_ms=(perf_counter() - started) * 1000)
                raise
            else:
                emit('operation.completed', operation=name, operation_id=operation_id,
                     result=result, state_after=arguments, elapsed_ms=(perf_counter() - started) * 1000)
                return result
            finally:
                _parent.reset(token)
        return wrapped
    return decorate
