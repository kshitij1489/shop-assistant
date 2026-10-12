"""Read persisted evidence without loading sessions or invoking the conversation runtime."""
from itertools import groupby
import json
from tempfile import SpooledTemporaryFile

from django.db.models import Case, F, JSONField, Value, When
from django.utils import timezone

from chatbot_core.models import ConversationEvent


def recorded_events(*, tenant_id):
    """Every reader starts with an explicit tenant scope."""
    return ConversationEvent.objects.filter(tenant_id=tenant_id)


def _metadata(row):
    envelope = row.envelope
    payload = envelope.get('payload', {})
    return {
        'turn_id': row.turn_id, 'segment_id': row.segment_id,
        'sequence': row.sequence, 'event': row.event, 'occurred_at': row.occurred_at,
        'envelope__segment_kind': envelope.get('segment_kind'),
        'completion': payload if row.event == 'recording.completed' else None,
    }


def _segment(rows):
    """Consume one segment in sequence order, keeping ranges instead of huge gap lists."""
    count = starts = completions = failures = previous = 0
    gaps, kinds = [], set()
    first = last = started_at = ended_at = None
    for row in rows:
        if first is None:
            first = row
        last = row
        count += 1
        sequence = row['sequence']
        if sequence > previous + 1:
            gaps.append([previous + 1, sequence - 1])
        previous = sequence
        starts += row['event'] == 'recording.started'
        completions += row['event'] == 'recording.completed'
        failures += row['event'].endswith(('.failed', '_failed'))
        kinds.add(row['envelope__segment_kind'])
        occurred = row['occurred_at']
        started_at = min(started_at, occurred) if started_at else occurred
        ended_at = max(ended_at, occurred) if ended_at else occurred

    terminal = last['event'] == 'recording.completed'
    completion = last['completion'] or {}
    expected = completion.get('event_count') if terminal else None
    dropped = completion.get('dropped_events') if terminal else None
    status = completion.get('status') if terminal else None
    issues = []
    if starts != 1 or first['event'] != 'recording.started' or first['sequence'] != 1:
        issues.append('Missing or invalid start marker.')
    if completions != 1 or not terminal:
        issues.append('Missing or misplaced completion marker; capture may still be in progress.')
    if terminal and (type(expected) is not int or expected != count or expected != previous):
        issues.append('Completion event count does not match stored events.')
    if type(expected) is int and expected > previous:
        gaps.append([previous + 1, expected])
    if gaps:
        issues.append('Sequence gaps in stored events.')
    if terminal and (type(dropped) is not int or dropped != 0):
        issues.append('Capture reported dropped events or an invalid drop count.')
    if len(kinds) != 1:
        issues.append('Inconsistent segment kinds.')
    return {
        'turn_id': str(first['turn_id']), 'segment_id': str(first['segment_id']),
        'kind': first['envelope__segment_kind'], 'event_count': count,
        'first_sequence': first['sequence'], 'last_sequence': previous,
        'expected_event_count': expected, 'dropped_events': dropped,
        'missing_sequence_ranges': gaps, 'complete': not issues, 'issues': issues,
        'execution_status': status if status in ('succeeded', 'failed') else 'unknown',
        'failure_count': failures, 'started_at': started_at.isoformat(),
        'ended_at': ended_at.isoformat(),
    }


def segment_summaries(events):
    # Project only metadata, not multi-megabyte prompts and state snapshots.
    # Read the terminal object as JSON to preserve types on SQLite as well as
    # PostgreSQL (scalar JSON transforms can coerce strings such as "9" to ints).
    rows = events.annotate(completion=Case(
        When(event='recording.completed', then=F('envelope__payload')),
        default=Value(None), output_field=JSONField(),
    )).order_by('turn_id', 'segment_id', 'sequence').values(
        'turn_id', 'segment_id', 'sequence', 'event', 'occurred_at',
        'envelope__segment_kind', 'completion',
    ).iterator(chunk_size=500)
    return [_segment(group) for _, group in groupby(rows, key=lambda row: row['segment_id'])]


def turn_summary(segments):
    primary = [segment for segment in segments if segment['kind'] == 'turn']
    statuses = {segment['execution_status'] for segment in primary}
    return {
        'segments': segments, 'event_count': sum(segment['event_count'] for segment in segments),
        'complete': bool(primary) and all(segment['complete'] for segment in segments),
        'has_turn_segment': bool(primary),
        'execution_status': ('failed' if 'failed' in statuses else
                             'succeeded' if statuses == {'succeeded'} else 'unknown'),
        'failure_count': sum(segment['failure_count'] for segment in segments),
    }


def turn_summaries(events):
    grouped = {}
    for segment in segment_summaries(events):
        grouped.setdefault(segment['turn_id'], []).append(segment)
    return {turn_id: turn_summary(segments) for turn_id, segments in grouped.items()}


def turn_previews(events):
    """Small message previews for a page of turns; the timeline retains full text."""
    previews = {}
    rows = events.filter(event__in=('recording.started', 'turn.input', 'turn.result')).order_by(
        'occurred_at', 'segment_id', 'sequence',
    ).values('turn_id', 'event', 'envelope__payload').iterator(chunk_size=100)
    for row in rows:
        preview = previews.setdefault(str(row['turn_id']), {'query': None, 'reply': None})
        payload = row['envelope__payload']
        query, reply = payload.get('query'), payload.get('response')
        if isinstance(query, str) and preview['query'] is None:
            preview['query'] = query[:240]
        if row['event'] == 'turn.result' and isinstance(reply, str):
            preview['reply'] = reply[:240]
    return previews


def export_recording(events, *, scope):
    """Build a complete JSON file from one SQL cursor, spilling large exports to disk.

    Completeness is calculated from the same rows written to the file. A later
    writer commit cannot appear only in the summary or only in the event array.
    The caller owns the returned file (FileResponse closes it after delivery).
    """
    output = SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')

    def write(value):
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    try:
        header = {
            'format': 'studio-desk.conversation-recording', 'format_version': 1,
            'scope': scope, 'export_started_at': timezone.now().isoformat(),
            'event_order': ['turn_id', 'segment_id', 'sequence'],
            'completeness_scope': 'Persisted events visible to this export query. Later delivery '
                                  'and commerce segments may arrive; execution status is separate '
                                  'from evidence completeness.',
        }
        output.write(b'{')
        for key, value in header.items():
            write(key)
            output.write(b':')
            write(value)
            output.write(b',')
        output.write(b'"events":[')
        segments, count = [], 0
        rows = events.order_by('turn_id', 'segment_id', 'sequence').iterator(chunk_size=10)

        def capture(group):
            nonlocal count
            for row in group:
                if count:
                    output.write(b',')
                write({'recorded_at': row.recorded_at.isoformat(), 'envelope': row.envelope})
                count += 1
                yield _metadata(row)

        for _, group in groupby(rows, key=lambda row: row.segment_id):
            segments.append(_segment(capture(group)))
        output.write(b'],"segments":')
        write(segments)
        turns = {}
        for segment in segments:
            turns.setdefault(segment['turn_id'], []).append(segment)
        output.write(b',"turns":')
        write({turn_id: {key: value for key, value in turn_summary(group).items() if key != 'segments'}
               for turn_id, group in turns.items()})
        output.write(b',"event_count":')
        write(count)
        output.write(b',"export_finished_at":')
        write(timezone.now().isoformat())
        output.write(b'}')
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
