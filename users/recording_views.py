"""Authenticated inspection, downloads, and isolated replay of captured evidence."""
from functools import wraps
import json

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, JSONField, Max, Min, Q, Value, When
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_protect

from chatbot_core.recording.inspection import (
    export_recording, recorded_events, turn_previews, turn_summaries,
)
from chatbot_core.recording.capture import session_digest
from .tenant_access import authenticated_tenant


class SessionFilters(forms.Form):
    channel = forms.CharField(required=False, max_length=32)
    session_id = forms.CharField(required=False, max_length=64, label='Session digest or chat ID')
    turn_id = forms.UUIDField(required=False, label='Turn ID')


class EventFilters(forms.Form):
    event = forms.CharField(required=False, max_length=96)
    segment = forms.UUIDField(required=False)


def recording_view(view):
    @never_cache
    @login_required
    @require_GET
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        tenant = authenticated_tenant(request)
        return view(request, tenant, recorded_events(tenant_id=tenant.pk), *args, **kwargs)
    return wrapped


def _render(request, tenant, template, context, *, status=200):
    return render(request, f'users/recordings/{template}.html', {
        'tenant': tenant, 'active_page': 'recordings', **context,
    }, status=status)


def _session(events, channel, session_id):
    events = events.filter(channel=channel, session_id=session_id)
    if not events.exists():
        raise Http404('Recording session not found.')
    return events


def _turn(events, turn_id):
    events = events.filter(turn_id=turn_id)
    identity = events.values('channel', 'session_id').first()
    if identity is None:
        raise Http404('Recorded turn not found.')
    return events, identity


@recording_view
def sessions(request, tenant, events):
    form = SessionFilters(request.GET)
    channels = list(events.order_by('channel').values_list('channel', flat=True).distinct())
    form.fields['channel'] = forms.ChoiceField(required=False,
        choices=[('', 'All channels'), *[(channel, channel) for channel in channels]])
    valid = form.is_valid()
    if valid:
        if form.cleaned_data['channel']:
            events = events.filter(channel=form.cleaned_data['channel'])
        if session_id := form.cleaned_data['session_id']:
            matches = Q(session_id=session_id)
            for channel in ('telegram', 'whatsapp'):
                matches |= Q(channel=channel, session_id=session_digest(tenant.pk, channel, session_id))
            events = events.filter(matches)
        if form.cleaned_data['turn_id']:
            match = events.filter(turn_id=form.cleaned_data['turn_id']).values('channel', 'session_id').first()
            events = events.filter(**match) if match else events.none()
    else:
        events = events.none()
    rows = events.values('channel', 'session_id').annotate(
        turn_count=Count('turn_id', distinct=True), event_count=Count('event_id'),
        started_at=Min('occurred_at'), last_activity=Max('occurred_at'),
    ).order_by('-last_activity', 'channel', 'session_id')
    return _render(request, tenant, 'sessions', {
        'form': form, 'page': Paginator(rows, 25).get_page(request.GET.get('page')),
        'recording_enabled': getattr(settings, 'CONVERSATION_RECORDING_ENABLED', False),
    }, status=200 if valid else 400)


@recording_view
def session_detail(request, tenant, events, channel, session_id):
    events = _session(events, channel, session_id)
    turns = events.values('turn_id').annotate(started_at=Min('occurred_at'),
        last_activity=Max('occurred_at')).order_by('started_at', 'turn_id')
    page = Paginator(turns, 25).get_page(request.GET.get('page'))
    rows = list(page.object_list)
    selected = events.filter(turn_id__in=[row['turn_id'] for row in rows])
    summaries, previews = turn_summaries(selected), turn_previews(selected)
    for row in rows:
        row.update(summaries.get(str(row['turn_id']), {}))
        row.update(previews.get(str(row['turn_id']), {}))
    page.object_list = rows
    return _render(request, tenant, 'session', {
        'channel': channel, 'session_id': session_id, 'page': page,
    })


@recording_view
def turn_detail(request, tenant, events, turn_id):
    events, identity = _turn(events, turn_id)
    summary = turn_summaries(events)[str(turn_id)]
    form = EventFilters(request.GET)
    form.fields['event'] = forms.ChoiceField(required=False, choices=[('', 'All events'), *[
        (name, name) for name in events.order_by('event').values_list('event', flat=True).distinct()]])
    valid = form.is_valid()
    selected = events
    if valid:
        if form.cleaned_data['event']:
            selected = selected.filter(event=form.cleaned_data['event'])
        if form.cleaned_data['segment']:
            selected = selected.filter(segment_id=form.cleaned_data['segment'])
    else:
        selected = selected.none()
    page = Paginator(selected.order_by('occurred_at', 'segment_id', 'sequence', 'event_id'), 25).get_page(request.GET.get('page'))
    for row in page.object_list:
        row.pretty_envelope = json.dumps(row.envelope, ensure_ascii=False, indent=2)
    # Compact evidence highlights; full snapshots remain in the event timeline.
    highlights = list(events.filter(event__in=(
        'turn.input', 'turn.result', 'session.publication.completed', 'session.publication.failed',
        'delivery.observed', 'delivery.failed',
    )).annotate(details=Case(
        When(event__in=('turn.input', 'turn.result', 'delivery.observed', 'delivery.failed'),
             then=F('envelope__payload')), default=Value(None), output_field=JSONField(),
    )).order_by('occurred_at', 'segment_id', 'sequence').values('event', 'details'))
    return _render(request, tenant, 'turn', {
        **identity, 'turn_id': turn_id, 'summary': summary, 'form': form,
        'page': page, 'highlights': highlights,
        'replay_steps': list(events.filter(event='replay.step.started').order_by('sequence')
                             .values_list('envelope__payload', flat=True)),
    }, status=200 if valid else 400)


def _download(events, tenant, identity, *, turn_id=None):
    scope = {'tenant_id': str(tenant.pk), **identity}
    if turn_id is not None:
        scope['turn_id'] = str(turn_id)
    output = export_recording(events, scope=scope)
    filename = f'recording-{"turn-" + str(turn_id) if turn_id else "session-" + identity["session_id"]}.json'
    response = FileResponse(output, as_attachment=True, filename=filename, content_type='application/json')
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@recording_view
def session_export(request, tenant, events, channel, session_id):
    return _download(_session(events, channel, session_id), tenant,
                     {'channel': channel, 'session_id': session_id})


@recording_view
def turn_export(request, tenant, events, turn_id):
    events, identity = _turn(events, turn_id)
    return _download(events, tenant, identity, turn_id=turn_id)


class ReplayForm(forms.Form):
    step_id = forms.UUIDField(required=False)


@never_cache
@login_required
@require_POST
@csrf_protect
def turn_replay(request, turn_id):
    from chatbot_core.recording.replay import ReplayTape, ReplayUnavailable
    from chatbot_core.recording.replay_codec import CodecError

    tenant = authenticated_tenant(request)
    events, identity = _turn(recorded_events(tenant_id=tenant.pk), turn_id)
    form = ReplayForm(request.POST)
    report = None
    error = None
    status = 200
    if not form.is_valid():
        error, status = 'Invalid replay step ID.', 400
    else:
        try:
            tape = ReplayTape(list(events.values_list('envelope', flat=True)),
                              tenant_id=tenant.pk, turn_id=turn_id)
            report = tape.execute(step_id=form.cleaned_data['step_id'])
        except (ReplayUnavailable, CodecError, KeyError, TypeError, ValueError) as exc:
            error, status = str(exc), 409
    return _render(request, tenant, 'replay', {
        **identity, 'turn_id': turn_id, 'report': report, 'error': error,
        'pretty_report': json.dumps(report, ensure_ascii=False, indent=2) if report else None,
    }, status=status)
