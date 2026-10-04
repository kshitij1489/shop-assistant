"""Compose project selection without starting or modifying development services."""
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]


class DevelopmentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.main = runpy.run_path(str(ROOT / 'scripts/evaluate-dev'))['main']
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(sys, 'argv', ['evaluate-dev', 'up']))
        self.list_projects = self.enterContext(patch('subprocess.run', return_value=Mock(stdout='[]')))
        self.launch = self.enterContext(patch('subprocess.call', return_value=0))

    def test_new_checkout_uses_compose_default_project_resolution_from_checkout(self):
        self.assertEqual(self.main(), 0)
        command = self.launch.call_args.args[0]
        self.assertNotIn('--project-name', command)
        self.assertNotIn('studio-desk-local', command)
        self.assertEqual(self.launch.call_args.kwargs['cwd'], ROOT)
        self.assertEqual(command[-2:], ['up', '-d'])
        self.assertEqual(command[command.index('--env-file') + 1], '.env.dev')

    def test_existing_checkout_project_is_reused(self):
        self.list_projects.return_value.stdout = json.dumps([
            {'Name': 'other', 'ConfigFiles': '/another/docker-compose.dev.yml'},
            {'Name': 'existing-dev', 'ConfigFiles': f'{ROOT}/docker-compose.yml,{ROOT}/docker-compose.dev.yml'},
        ])
        self.main()
        command = self.launch.call_args.args[0]
        self.assertEqual(command[command.index('--project-name') + 1], 'existing-dev')

    def test_explicit_project_takes_precedence_and_runner_executes_in_web(self):
        os.environ['COMPOSE_PROJECT_NAME'] = 'chosen-dev'
        os.environ['APP_ENV_FILE'] = 'custom.env'
        with patch.object(sys, 'argv', ['evaluate-dev', 'preflight']):
            self.main()
        self.list_projects.assert_not_called()
        command = self.launch.call_args.args[0]
        self.assertEqual(command[command.index('--project-name') + 1], 'chosen-dev')
        self.assertEqual(command[command.index('--env-file') + 1], 'custom.env')
        self.assertEqual(command[-9:], ['exec', '-T', '-u', 'appuser', 'web', 'python', '-m', 'evaluate', 'preflight'])

    def test_multiple_matching_projects_require_explicit_selection(self):
        self.list_projects.return_value.stdout = json.dumps([
            {'Name': name, 'ConfigFiles': str(ROOT / 'docker-compose.dev.yml')}
            for name in ('one', 'two')
        ])
        with self.assertRaisesRegex(SystemExit, 'Multiple development projects'):
            self.main()
        self.launch.assert_not_called()
