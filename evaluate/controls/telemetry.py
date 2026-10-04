"""Allowlisted metadata and redacted typed decisions; never raw model prompts."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import re
from time import perf_counter
from uuid import uuid4

from evaluate.evidence.redaction import assert_redacted, redact_text
from .context import current

logger = logging.getLogger('evaluate.telemetry')
FIELDS = frozenset({'operation', 'elapsed_ms', 'status', 'error_type', 'model',
    'provider_request_id', 'input_tokens', 'output_tokens', 'total_tokens',
    'cached_input_tokens', 'retries', 'retry_callbacks', 'cache', 'tier', 'hit',
    'injected', 'task_id', 'command_id', 'order_id', 'call_id', 'http_status'})


def emit(event, **fields):
    ctx = current()
    if ctx is None:
        return
    if set(fields) - FIELDS:
        raise ValueError('Unapproved evaluation telemetry field')
    # Mask credentials and URLs even if accidentally supplied as a model/ID.
    safe = {key: re.sub(r'https?://\S+', '[REDACTED:url]', redact_text(value))[:200]
            if isinstance(value, str) else value for key, value in fields.items()}
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in safe.values()):
        raise ValueError('Telemetry fields must be scalar')
    _record(ctx, event, safe)


def emit_classification(proposal, *, prompt_version, catalog_version):
    """Persist decisions only for authenticated evaluation requests.

    The validated schema bounds which fields may enter this journal. Credentials
    and URLs are redacted just like other saved evaluation evidence.
    """
    ctx = current()
    if ctx is None:
        return
    from chatbot_core.llm.schemas import ClassifiedMessages
    value = ClassifiedMessages.model_validate(proposal).model_dump()
    _record(ctx, 'classification.proposed', {
        'prompt_version': prompt_version, 'catalog_version': catalog_version,
        'proposal': _safe_decision(value),
    })


def _safe_decision(item):
    if isinstance(item, str):
        return re.sub(r'https?://\S+', '[REDACTED:url]', redact_text(item))
    if isinstance(item, dict):
        return {key: _safe_decision(child) for key, child in item.items()}
    if isinstance(item, (list, tuple)):
        return [_safe_decision(child) for child in item]
    return item


def emit_capability_check(*, classification_index, classified_route, effective_route,
                          action_kind, configuration_version, required_routes, unavailable_routes):
    """Record dispatch evidence, without inferring whether the proposal was correct.

    A route difference is sometimes intentional. Availability alone cannot assign
    blame to either the model or the fixture; retain both for independent review.
    """
    ctx = current()
    if ctx is None:
        return
    _record(ctx, 'capability.checked', _safe_decision({
        'classification_index': classification_index,
        'classified_route': classified_route, 'effective_route': effective_route,
        'action_kind': action_kind, 'configuration_version': configuration_version,
        'required_routes': sorted(required_routes),
        'unavailable_routes': sorted(unavailable_routes),
        'status': 'unavailable' if unavailable_routes else 'allowed',
    }))


def _record(ctx, event, fields):
    record = dict(schema_version='1.0.0', event_id=str(uuid4()),
        occurred_at=datetime.now(timezone.utc).isoformat(), event=event,
        **{k: v for k, v in ctx.identity.model_dump().items() if k != 'schema_version'},
        request_id=ctx.request_id, session_id=ctx.session_id, tenant_id=ctx.tenant_id,
        cache_mode=ctx.cache_mode, **fields)
    assert_redacted(record)
    try:
        logger.info('evaluation event', extra={'evaluation': record})
    except Exception:
        ctx.evidence_failed.set()
        raise


@contextmanager
def span(name, **fields):
    ctx = current()
    if ctx is None:
        yield
        return
    started = perf_counter()
    emit(name + '.started', **fields)
    try:
        yield
        # Business fallbacks may catch provider/callback exceptions. A failed
        # evidence write must still fail the enclosing request/task boundary.
        if ctx.evidence_failed.is_set():
            raise RuntimeError('Evaluation evidence persistence failed')
    except BaseException as exc:
        emit(name + '.completed', elapsed_ms=(perf_counter() - started) * 1000,
             status='failed', error_type=type(exc).__name__, **fields)
        raise
    else:
        emit(name + '.completed', elapsed_ms=(perf_counter() - started) * 1000,
             status='succeeded', **fields)


def observed(operation):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(operation):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.evaluation, sort_keys=True, allow_nan=False)


class EvidenceHandler(logging.Handler):
    """One process owns each journal. Merge by event_id in the integration lane."""
    def __init__(self, path, run_id):
        super().__init__()
        from evaluate.evidence.journal import JournalWriter
        self.writer, self.run_id = JournalWriter(path), run_id

    def emit(self, record):
        data = getattr(record, 'evaluation', None)
        if data is None or data['run_id'] != self.run_id:
            return
        assert_redacted(data)
        self.writer.append(data)
        self.writer.flush()

    def close(self):
        self.writer.close()
        super().close()


class RoutedEvidenceHandler(logging.Handler):
    """Route authenticated application contexts to their run's evidence directory."""
    def __init__(self, root):
        super().__init__()
        from pathlib import Path
        import socket
        import os
        self.root = Path(root)
        self.process = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.handlers = {}

    def emit(self, record):
        data = getattr(record, 'evaluation', None)
        if data is None:
            return
        run_id = data['run_id']
        # Run IDs are contract IDs, but may contain slashes. Never use them as paths unchecked.
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', run_id) or run_id in {'.', '..'}:
            raise ValueError('Run ID cannot be used as an evidence directory')
        if run_id not in self.handlers:
            directory = self.root / run_id
            directory.mkdir(parents=True, exist_ok=True)
            self.handlers[run_id] = EvidenceHandler(directory / f'application-{self.process}.jsonl', run_id)
        self.handlers[run_id].emit(record)

    def close(self):
        for handler in self.handlers.values():
            handler.close()
        super().close()
