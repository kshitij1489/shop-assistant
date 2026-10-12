"""At-least-once stream consumption with immutable, idempotent SQL inserts."""
import json
import logging
from hashlib import blake2b
from datetime import datetime
from typing import Literal
from uuid import UUID

from django.db import connection, IntegrityError, transaction
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import ResponseError

from chatbot_core.models import ConversationEvent
from .transport import GROUP, STREAM

logger = logging.getLogger(__name__)


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    event_id: UUID
    tenant_id: str = Field(pattern=r'^[1-9][0-9]*$')
    channel: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)
    turn_id: UUID
    segment_id: UUID
    segment_kind: str
    sequence: int = Field(gt=0)
    occurred_at: datetime
    event: str = Field(min_length=1, max_length=96)
    parent_operation_id: str | None
    payload: dict


def persist_batch(records):
    """Return only committed IDs. Conflicting redeliveries never overwrite history."""
    rows, ids = [], []
    for stream_id, fields in records:
        try:
            raw = json.loads(fields['event'])
            event = Envelope.model_validate(raw)
            if event.occurred_at.tzinfo is None:
                raise ValueError('Recording timestamp needs a timezone')
        except (ValueError, KeyError, TypeError):
            # Keep malformed entries pending for operator repair; no raw payload in logs.
            logger.error('Invalid recording envelope stream_id=%s; left pending', stream_id)
            continue
        rows.append(ConversationEvent(event_id=event.event_id, tenant_id=int(event.tenant_id),
            channel=event.channel, session_id=event.session_id, turn_id=event.turn_id,
            segment_id=event.segment_id, sequence=event.sequence, event=event.event,
            occurred_at=event.occurred_at, envelope=raw))
        ids.append(stream_id)
    if not rows:
        return []
    with transaction.atomic(durable=True):
        # Scope checks span multiple rows/batches. Serialize competing writers
        # for these IDs without locking unrelated tenants or whole tables.
        if connection.vendor == 'postgresql':
            keys = {f'turn:{row.turn_id}' for row in rows} | {f'segment:{row.segment_id}' for row in rows}
            locks = sorted({int.from_bytes(blake2b(key.encode(), digest_size=8).digest(), 'big', signed=True)
                            for key in keys})
            with connection.cursor() as cursor:
                for lock in locks:
                    cursor.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        ConversationEvent.objects.bulk_create(rows, ignore_conflicts=True)
        saved = {str(row.event_id): row.envelope for row in
                 ConversationEvent.objects.filter(event_id__in=[row.event_id for row in rows])}
        for row in rows:
            if saved.get(str(row.event_id)) != row.envelope:
                raise ValueError('Conflicting recording event ID or sequence')
        # A turn may only have one tenant/session, including later async segments.
        scopes = {}
        for row in ConversationEvent.objects.filter(turn_id__in={row.turn_id for row in rows}).values(
                'turn_id', 'tenant_id', 'channel', 'session_id').distinct():
            key = row.pop('turn_id')
            if key in scopes and scopes[key] != row:
                raise ValueError('Conflicting recording segment scope within turn')
            scopes[key] = row
        segments = {}
        for row in ConversationEvent.objects.filter(segment_id__in={row.segment_id for row in rows}).values(
                'segment_id', 'turn_id').distinct():
            if row['segment_id'] in segments and segments[row['segment_id']] != row['turn_id']:
                raise ValueError('Conflicting recording segment scope')
            segments[row['segment_id']] = row['turn_id']
    return ids


def segment_complete(*, tenant_id, segment_id):
    """Completeness is evidence continuity, independent of business success."""
    from .inspection import recorded_events, segment_summaries
    segments = segment_summaries(recorded_events(tenant_id=tenant_id).filter(segment_id=segment_id))
    return len(segments) == 1 and segments[0]['complete']


class Writer:
    def __init__(self, redis, consumer, *, batch_size=100, reclaim_ms=60000):
        if batch_size < 1 or reclaim_ms < 0:
            raise ValueError('Writer batch size must be positive and reclaim delay nonnegative')
        self.redis, self.consumer = redis, consumer
        self.batch_size, self.reclaim_ms, self.cursor = batch_size, reclaim_ms, '0-0'

    def setup(self):
        try:
            self.redis.xgroup_create(STREAM, GROUP, id='0-0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def save(self, records):
        try:
            ids = persist_batch(records)
        except (ValueError, IntegrityError):
            # Isolate poison entries so one conflict/deleted tenant cannot stop
            # unrelated turns. SQL outages still propagate without per-row retry.
            ids = []
            for record in records:
                try:
                    ids.extend(persist_batch([record]))
                except (ValueError, IntegrityError):
                    logger.error('Conflicting recording stream_id=%s; left pending', record[0])
        if ids:
            # SQL has committed. A crash here causes harmless redelivery.
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.xack(STREAM, GROUP, *ids)
                pipe.xdel(STREAM, *ids)
                pipe.execute()
        return len(ids)

    def tick(self, *, block=1000):
        claimed = self.redis.xautoclaim(STREAM, GROUP, self.consumer, self.reclaim_ms,
                                        start_id=self.cursor, count=self.batch_size)
        self.cursor, pending = claimed[:2]
        if len(claimed) > 2 and claimed[2]:
            logger.error('Recording stream entries were deleted before persistence count=%s', len(claimed[2]))
        count = self.save(pending) if pending else 0
        fresh = self.redis.xreadgroup(GROUP, self.consumer, {STREAM: '>'}, count=self.batch_size, block=block)
        for _, records in fresh:
            count += self.save(records)
        return count
