"""Evaluate published schedules against a trusted clock, never claim live status."""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
ROUTE = ('information_about_the_cafe', 'location_and_hours')


def _minute(value, *, closing=False):
    if closing and value == '24:00':
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Invalid schedule time')
    hours, minutes = map(int, value.split(':'))
    return hours * 60 + minutes


def _intervals(value):
    if not isinstance(value, list):
        raise ValueError('Schedule intervals must be a list')
    intervals = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {'opens', 'closes'}:
            raise ValueError('Invalid schedule interval')
        start, end = _minute(row['opens']), _minute(row['closes'], closing=True)
        if start == end:
            raise ValueError('Equal times do not establish all-day opening')
        intervals.append((start, end if end > start else end + 1440))
    return intervals


def _instant(day, minutes, zone):
    naive = datetime.combine(day, time()) + timedelta(minutes=minutes)
    aware = naive.replace(tzinfo=zone)
    # A schedule alone cannot disambiguate repeated/nonexistent DST wall times.
    if (aware.utcoffset() != aware.replace(fold=1).utcoffset()
            or aware.astimezone(dt_timezone.utc).astimezone(zone).replace(tzinfo=None) != naive):
        raise ValueError('Ambiguous schedule boundary')
    return aware.astimezone(dt_timezone.utc)


def opening_hours_context(configuration, *, current=None):
    current = current if current is not None else timezone.now()
    current = current.replace(second=0, microsecond=0)  # Published boundaries have minute precision.
    result = {'evaluated_at': current.isoformat(), 'scheduled_status': 'unknown',
              'live_status_verified': False}
    if configuration is None or not configuration.allows(*ROUTE):
        return {**result, 'reason': 'No permitted published hours'}
    facts = next((doc['payload'] for doc in configuration.documents if
                  (doc['dtype'], doc['intent'], doc['sub_intent']) == ('knowledge', *ROUTE)), None)
    if not isinstance(facts, dict):
        return {**result, 'reason': 'No structured published hours'}
    try:
        zone = ZoneInfo(facts['timezone'])
        local = current.astimezone(zone)
        result.update(local_time=local.isoformat(), timezone=facts['timezone'])
        if isinstance(facts.get('as_of'), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', facts['as_of']):
            result['schedule_as_of'] = facts['as_of']
        hours = facts['opening_hours']
        weekly = hours['weekly']
        if not isinstance(weekly, dict) or set(weekly) != set(DAYS):
            raise ValueError('Incomplete weekly schedule')
        weekly = {day: _intervals(rows) for day, rows in weekly.items()}
        overrides = hours.get('date_overrides', {})
        if not isinstance(overrides, dict):
            raise ValueError('Invalid date overrides')
        overrides = {date.fromisoformat(day): _intervals(rows) for day, rows in overrides.items()}
        today = local.date()
        # Free-form holiday rules are not machine-verifiable overrides.
        if hours.get('holiday_hours') not in (None, {}, []) and today not in overrides:
            raise ValueError('Holiday hours need dated overrides')
        windows = []
        for day in (today - timedelta(days=1), today):
            rows = overrides.get(day, weekly[DAYS[day.weekday()]])
            for start, end in rows:
                windows.append((_instant(day, start, zone), _instant(day, end, zone)))
        # An explicit date override owns that whole date, including a closure
        # after a prior day's regular overnight session.
        if today in overrides:
            windows = [(_instant(today, start, zone), _instant(today, end, zone))
                       for start, end in overrides[today]]
        now_utc = current.astimezone(dt_timezone.utc)
        result.update(scheduled_status='open' if any(start <= now_utc < end for start, end in windows) else 'closed',
                      basis='dated_override' if today in overrides else 'regular_weekly_schedule',
                      qualification='According to the published schedule; actual opening is not live-verified. '
                                    'Holiday or exceptional closures may differ.')
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        result.update(scheduled_status='unknown', reason='Published hours or timezone cannot establish the current schedule')
    return result
