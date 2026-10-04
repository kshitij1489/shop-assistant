"""Rendering regressions for the dashboard's JSON editors."""
import json
import re
from types import SimpleNamespace
from uuid import UUID

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings


@override_settings(ROOT_URLCONF='tests.support.urls')
class DashboardEditorTests(SimpleTestCase):
    def test_json_editors_preserve_saved_values_and_escape_markup(self):
        item_id = UUID('11111111-1111-1111-1111-111111111111')
        metadata = {'enabled': True, 'note': '</script><b>Customer text</b>'}
        catalog = {'dietary_preferences': {'vegan': False}, 'ingredients': ['Chef\'s coffee']}
        html = render_to_string('users/menu_item_detail.html', {
            'item': SimpleNamespace(id=item_id, pk=item_id, meta=metadata),
            'catmeta': SimpleNamespace(**catalog),
        })
        for source_id, expected in [
            (f'meta-json-{item_id}', metadata),
            ('catalog-json-dietary_preferences', catalog['dietary_preferences']),
            ('catalog-json-ingredients', catalog['ingredients']),
        ]:
            with self.subTest(source_id=source_id):
                match = re.search(r'<script id="' + source_id + r'" type="application/json">(.*?)</script>', html)
                self.assertIsNotNone(match)
                self.assertEqual(json.loads(match.group(1)), expected)
                self.assertIn(f'data-json-source="{source_id}"', html)
        self.assertNotIn('</script><b>Customer text</b>', html)

    def test_empty_metadata_is_an_object_not_a_json_string(self):
        item_id = UUID('11111111-1111-1111-1111-111111111111')
        html = render_to_string('users/menu_item_detail.html', {
            'item': SimpleNamespace(id=item_id, pk=item_id, meta={}),
        })
        match = re.search(r'<script id="meta-json-' + str(item_id) + r'" type="application/json">(.*?)</script>', html)
        self.assertEqual(json.loads(match.group(1)), {})
