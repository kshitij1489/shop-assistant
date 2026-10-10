from copy import deepcopy
from datetime import datetime

from django.test import SimpleTestCase

from chatbot_core.opening_hours import DAYS, opening_hours_context
from chatbot_core.runtime_configuration import RuntimeConfiguration


class OpeningHoursTests(SimpleTestCase):
    def setUp(self):
        self.facts = {'timezone': 'Asia/Kolkata', 'opening_hours': {
            'weekly': {day: ([] if day == 'monday' else [{'opens': '12:00', 'closes': '23:30'}])
                       for day in DAYS}, 'holiday_hours': None}}

    def status(self, when, facts=None, disabled=False):
        documents = [{'dtype': 'knowledge', 'intent': 'information_about_the_cafe',
                      'sub_intent': 'location_and_hours', 'payload': self.facts if facts is None else facts}]
        if disabled:
            documents.append({'dtype': 'intent_classification', 'intent': 'information_about_the_cafe',
                              'sub_intent': 'location_and_hours', 'payload': {'enabled': False}})
        config = RuntimeConfiguration('1', 'key', 'cafe', 1, documents)
        return opening_hours_context(config, current=datetime.fromisoformat(when))

    def test_local_clock_opening_closing_and_closed_weekday(self):
        for when, expected in [('2026-10-09T06:29:00+00:00', 'closed'),
                               ('2026-10-09T06:30:00+00:00', 'open'),
                               ('2026-10-09T17:59:59+00:00', 'open'),
                               ('2026-10-09T18:00:00+00:00', 'closed'),
                               ('2026-10-12T13:00:00+05:30', 'closed')]:
            with self.subTest(when=when):
                result = self.status(when)
                self.assertEqual(result['scheduled_status'], expected)
                self.assertEqual(result['basis'], 'regular_weekly_schedule')
                self.assertFalse(result['live_status_verified'])

    def test_overnight_hours_and_explicit_date_closure(self):
        self.facts['opening_hours']['weekly']['sunday'] = [{'opens': '20:00', 'closes': '02:00'}]
        self.assertEqual(self.status('2026-10-12T01:00:00+05:30')['scheduled_status'], 'open')
        self.assertEqual(self.status('2026-10-12T02:00:00+05:30')['scheduled_status'], 'closed')
        self.facts['opening_hours']['date_overrides'] = {'2026-10-12': []}
        result = self.status('2026-10-12T01:00:00+05:30')
        self.assertEqual(result['scheduled_status'], 'closed')
        self.assertEqual(result['basis'], 'dated_override')

    def test_multiple_intervals_and_end_of_day(self):
        self.facts['opening_hours']['weekly']['friday'] = [
            {'opens': '09:00', 'closes': '12:00'}, {'opens': '17:00', 'closes': '24:00'}]
        self.assertEqual(self.status('2026-10-09T13:00:00+05:30')['scheduled_status'], 'closed')
        self.assertEqual(self.status('2026-10-09T23:59:00+05:30')['scheduled_status'], 'open')

    def test_missing_invalid_or_disabled_facts_never_establish_closed(self):
        variants = [{}, {**self.facts, 'timezone': 'Not/A_Zone'},
                    {**self.facts, 'opening_hours': {'weekly': {'friday': []}}}]
        for value in ('09:75', 'abc', None):
            facts = deepcopy(self.facts)
            facts['opening_hours']['weekly']['friday'] = [{'opens': value, 'closes': '18:00'}]
            variants.append(facts)
        facts = deepcopy(self.facts)
        facts['opening_hours']['holiday_hours'] = 'Closed on certain holidays'
        variants.append(facts)
        for facts in variants:
            with self.subTest(facts=facts):
                self.assertEqual(self.status('2026-10-09T13:00:00+05:30', facts)['scheduled_status'], 'unknown')
        self.assertEqual(self.status('2026-10-09T13:00:00+05:30', disabled=True)['scheduled_status'], 'unknown')

    def test_dst_boundary_requires_unambiguous_schedule(self):
        self.facts['timezone'] = 'America/New_York'
        self.facts['opening_hours']['weekly']['sunday'] = [{'opens': '01:30', 'closes': '03:30'}]
        self.assertEqual(self.status('2026-11-01T07:00:00+00:00')['scheduled_status'], 'unknown')
