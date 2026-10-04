"""Exercise the deployment gate without Docker or an application database."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support.paths import REPOSITORY_ROOT


class ProductionStartTests(unittest.TestCase):
    def run_start(self, audit_status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'calls.jsonl'
            docker = root / 'docker'
            docker.write_text(
                '#!/usr/bin/env python3\n'
                'import json, os, sys\n'
                'with open(os.environ["DOCKER_CALL_LOG"], "a") as log:\n'
                '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                'if "audit_tenant_ownership" in sys.argv:\n'
                '    sys.exit(int(os.environ["AUDIT_STATUS"]))\n'
            )
            docker.chmod(0o755)
            script = REPOSITORY_ROOT / 'scripts/start_production.sh'
            result = subprocess.run(
                ['bash', str(script), '--force-recreate'], cwd=directory,
                env={**os.environ, 'PATH': f'{directory}:{os.environ["PATH"]}',
                     'DOCKER_CALL_LOG': str(log), 'AUDIT_STATUS': str(audit_status)},
                capture_output=True, text=True,
            )
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_failed_audit_prevents_starting_services(self):
        result, calls = self.run_start(1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-9:], [
            'run', '--rm', '--no-deps', '--entrypoint', 'python', 'web',
            'manage.py', 'audit_tenant_ownership', '--fail'])

    def test_successful_audit_starts_same_deployment(self):
        result, calls = self.run_start(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn('audit_tenant_ownership', calls[0])
        self.assertEqual(calls[0][:5], calls[1][:5])
        self.assertEqual(calls[1][5:], ['up', '-d', '--remove-orphans', '--force-recreate'])
