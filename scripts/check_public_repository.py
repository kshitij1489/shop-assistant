#!/usr/bin/env python3
"""Check tracked source for private artifacts and common credential formats.

Uses only the standard library. Reports locations, never credential values.
Deleted working-tree files are skipped so this also works before staging cleanup.
"""
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SUFFIXES = (
    '.pem', '.key', '.p12', '.pfx', '.sqlite3', '.sqlite3-journal',
    '.sqlite3-wal', '.sqlite3-shm', '.db', '.db-journal', '.db-wal',
    '.db-shm', '.sql', '.sql.gz', '.dump', '.dump.gz', '.log',
)
PRIVATE_ROOTS = (
    'nginx/certs/', 'tenants/', 'media/', 'reports/', '.test-reports/',
    'static/admin/', 'staticfiles/', 'mock_services/.state/', '.venv/', 'venv/',
)
SECRET_PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'provider token': re.compile(
        rb'\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}'
        rb'|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}'
        rb'|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}'
        rb'|xox[baprs]-[A-Za-z0-9-]{20,})'
    ),
    'Telegram bot token': re.compile(rb'\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}'),
}


def private_path(path):
    name = PurePosixPath(path).name
    private_env = (name == '.env' or name.startswith('.env.')) and not name.endswith('.example')
    return (
        private_env or name.startswith('celerybeat-schedule')
        or name in {'id_rsa', 'id_ed25519', '.DS_Store'}
        or name.endswith(PRIVATE_SUFFIXES)
        or '__pycache__' in PurePosixPath(path).parts
        or path.startswith(PRIVATE_ROOTS)
    )


def credential_locations(data):
    for number, line in enumerate(data.splitlines(), 1):
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                yield number, kind


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    findings = []
    checked = 0
    for name in filter(None, tracked):
        path = ROOT / name
        if not path.exists():
            continue
        checked += 1
        if private_path(name):
            findings.append(f'{name}: private or generated artifact')
        if path.is_symlink():
            continue
        for line, kind in credential_locations(path.read_bytes()):
            findings.append(f'{name}:{line}: possible {kind}')
    if findings:
        print('\n'.join(findings))
        raise SystemExit(1)
    print(f'Public repository checks passed ({checked} tracked files).')


if __name__ == '__main__':
    main()
